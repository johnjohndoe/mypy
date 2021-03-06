"""Generate CPython API wrapper functions for native functions.

The wrapper functions are used by the CPython runtime when calling
native functions from interpreted code, and when the called function
can't be determined statically in compiled code. They validate, match,
unbox and type check function arguments, and box return values as
needed. All wrappers accept and return 'PyObject *' (boxed) values.

The wrappers aren't used for most calls between two native functions
or methods in a single compilation unit.
"""

from typing import List, Optional

from mypy.nodes import ARG_POS, ARG_OPT, ARG_NAMED_OPT, ARG_NAMED, ARG_STAR, ARG_STAR2

from mypyc.common import PREFIX, NATIVE_PREFIX, DUNDER_PREFIX, USE_VECTORCALL
from mypyc.codegen.emit import Emitter
from mypyc.ir.rtypes import (
    RType, is_object_rprimitive, is_int_rprimitive, is_bool_rprimitive, object_rprimitive
)
from mypyc.ir.func_ir import FuncIR, RuntimeArg, FUNC_STATICMETHOD
from mypyc.ir.class_ir import ClassIR
from mypyc.namegen import NameGenerator


# Generic vectorcall wrapper functions (Python 3.7+)
#
# A wrapper function has a signature like this:
#
# PyObject *fn(PyObject *self, PyObject *const *args, Py_ssize_t nargs, PyObject *kwnames)
#
# The function takes a self object, pointer to an array of arguments,
# the number of positional arguments, and a tuple of keyword argument
# names (that are stored starting in args[nargs]).
#
# It returns the returned object, or NULL on an exception.
#
# These are more efficient than legacy wrapper functions, since
# usually no tuple or dict objects need to be created for the
# arguments. Vectorcalls also use pre-constructed str objects for
# keyword argument names and other pre-computed information, instead
# of processing the argument format string on each call.


def wrapper_function_header(fn: FuncIR, names: NameGenerator) -> str:
    """Return header of a vectorcall wrapper function.

    See comment above for a summary of the arguments.
    """
    return (
        'PyObject *{prefix}{name}('
        'PyObject *self, PyObject *const *args, size_t nargs, PyObject *kwnames)').format(
            prefix=PREFIX,
            name=fn.cname(names))


def generate_traceback_code(fn: FuncIR,
                            emitter: Emitter,
                            source_path: str,
                            module_name: str) -> str:
    # If we hit an error while processing arguments, then we emit a
    # traceback frame to make it possible to debug where it happened.
    # Unlike traceback frames added for exceptions seen in IR, we do this
    # even if there is no `traceback_name`. This is because the error will
    # have originated here and so we need it in the traceback.
    globals_static = emitter.static_name('globals', module_name)
    traceback_code = 'CPy_AddTraceback("%s", "%s", %d, %s);' % (
        source_path.replace("\\", "\\\\"),
        fn.traceback_name or fn.name,
        fn.line,
        globals_static)
    return traceback_code


def make_arg_groups(args: List[RuntimeArg]) -> List[List[RuntimeArg]]:
    """Group arguments by kind."""
    return [[arg for arg in args if arg.kind == k] for k in range(ARG_NAMED_OPT + 1)]


def reorder_arg_groups(groups: List[List[RuntimeArg]]) -> List[RuntimeArg]:
    """Reorder argument groups to match their order in a format string."""
    return groups[ARG_POS] + groups[ARG_OPT] + groups[ARG_NAMED_OPT] + groups[ARG_NAMED]


def make_static_kwlist(args: List[RuntimeArg]) -> str:
    arg_names = ''.join('"{}", '.format(arg.name) for arg in args)
    return 'static const char * const kwlist[] = {{{}0}};'.format(arg_names)


def make_format_string(func_name: str, groups: List[List[RuntimeArg]]) -> str:
    """Return a format string that specifies the accepted arguments.

    The format string is an extended subset of what is supported by
    PyArg_ParseTupleAndKeywords(). Only the type 'O' is used, and we
    also support some extensions:

    - Required keyword-only arguments are introduced after '@'
    - If the function receives *args or **kwargs, we add a '%' prefix

    Each group requires the previous groups' delimiters to be present
    first.

    These are used by both vectorcall and legacy wrapper functions.
    """
    main_format = ''
    if groups[ARG_STAR] or groups[ARG_STAR2]:
        main_format += '%'
    main_format += 'O' * len(groups[ARG_POS])
    if groups[ARG_OPT] or groups[ARG_NAMED_OPT] or groups[ARG_NAMED]:
        main_format += '|' + 'O' * len(groups[ARG_OPT])
    if groups[ARG_NAMED_OPT] or groups[ARG_NAMED]:
        main_format += '$' + 'O' * len(groups[ARG_NAMED_OPT])
    if groups[ARG_NAMED]:
        main_format += '@' + 'O' * len(groups[ARG_NAMED])
    return '{}:{}'.format(main_format, func_name)


def generate_wrapper_function(fn: FuncIR,
                              emitter: Emitter,
                              source_path: str,
                              module_name: str) -> None:
    """Generate a CPython-compatible vectorcall wrapper for a native function.

    In particular, this handles unboxing the arguments, calling the native function, and
    then boxing the return value.
    """
    emitter.emit_line('{} {{'.format(wrapper_function_header(fn, emitter.names)))

    # If fn is a method, then the first argument is a self param
    real_args = list(fn.args)
    if fn.class_name and not fn.decl.kind == FUNC_STATICMETHOD:
        arg = real_args.pop(0)
        emitter.emit_line('PyObject *obj_{} = self;'.format(arg.name))

    # Need to order args as: required, optional, kwonly optional, kwonly required
    # This is because CPyArg_ParseStackAndKeywords format string requires
    # them grouped in that way.
    groups = make_arg_groups(real_args)
    reordered_args = reorder_arg_groups(groups)

    emitter.emit_line(make_static_kwlist(reordered_args))
    fmt = make_format_string(fn.name, groups)
    # Define the arguments the function accepts (but no types yet)
    emitter.emit_line('static CPyArg_Parser parser = {{"{}", kwlist, 0}};'.format(fmt))

    for arg in real_args:
        emitter.emit_line('PyObject *obj_{}{};'.format(
                          arg.name, ' = NULL' if arg.optional else ''))

    cleanups = ['CPy_DECREF(obj_{});'.format(arg.name)
                for arg in groups[ARG_STAR] + groups[ARG_STAR2]]

    arg_ptrs = []  # type: List[str]
    if groups[ARG_STAR] or groups[ARG_STAR2]:
        arg_ptrs += ['&obj_{}'.format(groups[ARG_STAR][0].name) if groups[ARG_STAR] else 'NULL']
        arg_ptrs += ['&obj_{}'.format(groups[ARG_STAR2][0].name) if groups[ARG_STAR2] else 'NULL']
    arg_ptrs += ['&obj_{}'.format(arg.name) for arg in reordered_args]

    if fn.name == '__call__' and USE_VECTORCALL:
        nargs = 'PyVectorcall_NARGS(nargs)'
    else:
        nargs = 'nargs'
    emitter.emit_lines(
        'if (!CPyArg_ParseStackAndKeywords(args, {}, kwnames, &parser{})) {{'.format(
            nargs, ''.join(', ' + n for n in arg_ptrs)),
        'return NULL;',
        '}')
    traceback_code = generate_traceback_code(fn, emitter, source_path, module_name)
    generate_wrapper_core(fn, emitter, groups[ARG_OPT] + groups[ARG_NAMED_OPT],
                          cleanups=cleanups,
                          traceback_code=traceback_code)

    emitter.emit_line('}')


# Legacy generic wrapper functions
#
# These take a self object, a Python tuple of positional arguments,
# and a dict of keyword arguments. These are a lot slower than
# vectorcall wrappers, especially in calls involving keyword
# arguments.


def legacy_wrapper_function_header(fn: FuncIR, names: NameGenerator) -> str:
    return 'PyObject *{prefix}{name}(PyObject *self, PyObject *args, PyObject *kw)'.format(
        prefix=PREFIX,
        name=fn.cname(names))


def generate_legacy_wrapper_function(fn: FuncIR,
                                     emitter: Emitter,
                                     source_path: str,
                                     module_name: str) -> None:
    """Generates a CPython-compatible legacy wrapper for a native function.

    In particular, this handles unboxing the arguments, calling the native function, and
    then boxing the return value.
    """
    emitter.emit_line('{} {{'.format(legacy_wrapper_function_header(fn, emitter.names)))

    # If fn is a method, then the first argument is a self param
    real_args = list(fn.args)
    if fn.class_name and not fn.decl.kind == FUNC_STATICMETHOD:
        arg = real_args.pop(0)
        emitter.emit_line('PyObject *obj_{} = self;'.format(arg.name))

    # Need to order args as: required, optional, kwonly optional, kwonly required
    # This is because CPyArg_ParseTupleAndKeywords format string requires
    # them grouped in that way.
    groups = make_arg_groups(real_args)
    reordered_args = reorder_arg_groups(groups)

    emitter.emit_line(make_static_kwlist(reordered_args))
    for arg in real_args:
        emitter.emit_line('PyObject *obj_{}{};'.format(
                          arg.name, ' = NULL' if arg.optional else ''))

    cleanups = ['CPy_DECREF(obj_{});'.format(arg.name)
                for arg in groups[ARG_STAR] + groups[ARG_STAR2]]

    arg_ptrs = []  # type: List[str]
    if groups[ARG_STAR] or groups[ARG_STAR2]:
        arg_ptrs += ['&obj_{}'.format(groups[ARG_STAR][0].name) if groups[ARG_STAR] else 'NULL']
        arg_ptrs += ['&obj_{}'.format(groups[ARG_STAR2][0].name) if groups[ARG_STAR2] else 'NULL']
    arg_ptrs += ['&obj_{}'.format(arg.name) for arg in reordered_args]

    emitter.emit_lines(
        'if (!CPyArg_ParseTupleAndKeywords(args, kw, "{}", kwlist{})) {{'.format(
            make_format_string(fn.name, groups), ''.join(', ' + n for n in arg_ptrs)),
        'return NULL;',
        '}')
    traceback_code = generate_traceback_code(fn, emitter, source_path, module_name)
    generate_wrapper_core(fn, emitter, groups[ARG_OPT] + groups[ARG_NAMED_OPT],
                          cleanups=cleanups,
                          traceback_code=traceback_code)

    emitter.emit_line('}')


# Specialized wrapper functions


def generate_dunder_wrapper(cl: ClassIR, fn: FuncIR, emitter: Emitter) -> str:
    """Generates a wrapper for native __dunder__ methods to be able to fit into the mapping
    protocol slot. This specifically means that the arguments are taken as *PyObjects and returned
    as *PyObjects.
    """
    input_args = ', '.join('PyObject *obj_{}'.format(arg.name) for arg in fn.args)
    name = '{}{}{}'.format(DUNDER_PREFIX, fn.name, cl.name_prefix(emitter.names))
    emitter.emit_line('static PyObject *{name}({input_args}) {{'.format(
        name=name,
        input_args=input_args,
    ))
    generate_wrapper_core(fn, emitter)
    emitter.emit_line('}')

    return name


RICHCOMPARE_OPS = {
    '__lt__': 'Py_LT',
    '__gt__': 'Py_GT',
    '__le__': 'Py_LE',
    '__ge__': 'Py_GE',
    '__eq__': 'Py_EQ',
    '__ne__': 'Py_NE',
}


def generate_richcompare_wrapper(cl: ClassIR, emitter: Emitter) -> Optional[str]:
    """Generates a wrapper for richcompare dunder methods."""
    # Sort for determinism on Python 3.5
    matches = sorted([name for name in RICHCOMPARE_OPS if cl.has_method(name)])
    if not matches:
        return None

    name = '{}_RichCompare_{}'.format(DUNDER_PREFIX, cl.name_prefix(emitter.names))
    emitter.emit_line(
        'static PyObject *{name}(PyObject *obj_lhs, PyObject *obj_rhs, int op) {{'.format(
            name=name)
    )
    emitter.emit_line('switch (op) {')
    for func in matches:
        emitter.emit_line('case {}: {{'.format(RICHCOMPARE_OPS[func]))
        method = cl.get_method(func)
        assert method is not None
        generate_wrapper_core(method, emitter, arg_names=['lhs', 'rhs'])
        emitter.emit_line('}')
    emitter.emit_line('}')

    emitter.emit_line('Py_INCREF(Py_NotImplemented);')
    emitter.emit_line('return Py_NotImplemented;')

    emitter.emit_line('}')

    return name


def generate_get_wrapper(cl: ClassIR, fn: FuncIR, emitter: Emitter) -> str:
    """Generates a wrapper for native __get__ methods."""
    name = '{}{}{}'.format(DUNDER_PREFIX, fn.name, cl.name_prefix(emitter.names))
    emitter.emit_line(
        'static PyObject *{name}(PyObject *self, PyObject *instance, PyObject *owner) {{'.
        format(name=name))
    emitter.emit_line('instance = instance ? instance : Py_None;')
    emitter.emit_line('return {}{}(self, instance, owner);'.format(
        NATIVE_PREFIX,
        fn.cname(emitter.names)))
    emitter.emit_line('}')

    return name


def generate_hash_wrapper(cl: ClassIR, fn: FuncIR, emitter: Emitter) -> str:
    """Generates a wrapper for native __hash__ methods."""
    name = '{}{}{}'.format(DUNDER_PREFIX, fn.name, cl.name_prefix(emitter.names))
    emitter.emit_line('static Py_ssize_t {name}(PyObject *self) {{'.format(
        name=name
    ))
    emitter.emit_line('{}retval = {}{}{}(self);'.format(emitter.ctype_spaced(fn.ret_type),
                                                        emitter.get_group_prefix(fn.decl),
                                                        NATIVE_PREFIX,
                                                        fn.cname(emitter.names)))
    emitter.emit_error_check('retval', fn.ret_type, 'return -1;')
    if is_int_rprimitive(fn.ret_type):
        emitter.emit_line('Py_ssize_t val = CPyTagged_AsSsize_t(retval);')
    else:
        emitter.emit_line('Py_ssize_t val = PyLong_AsSsize_t(retval);')
    emitter.emit_dec_ref('retval', fn.ret_type)
    emitter.emit_line('if (PyErr_Occurred()) return -1;')
    # We can't return -1 from a hash function..
    emitter.emit_line('if (val == -1) return -2;')
    emitter.emit_line('return val;')
    emitter.emit_line('}')

    return name


def generate_bool_wrapper(cl: ClassIR, fn: FuncIR, emitter: Emitter) -> str:
    """Generates a wrapper for native __bool__ methods."""
    name = '{}{}{}'.format(DUNDER_PREFIX, fn.name, cl.name_prefix(emitter.names))
    emitter.emit_line('static int {name}(PyObject *self) {{'.format(
        name=name
    ))
    emitter.emit_line('{}val = {}{}(self);'.format(emitter.ctype_spaced(fn.ret_type),
                                                   NATIVE_PREFIX,
                                                   fn.cname(emitter.names)))
    emitter.emit_error_check('val', fn.ret_type, 'return -1;')
    # This wouldn't be that hard to fix but it seems unimportant and
    # getting error handling and unboxing right would be fiddly. (And
    # way easier to do in IR!)
    assert is_bool_rprimitive(fn.ret_type), "Only bool return supported for __bool__"
    emitter.emit_line('return val;')
    emitter.emit_line('}')

    return name


# Helpers


def generate_wrapper_core(fn: FuncIR, emitter: Emitter,
                          optional_args: Optional[List[RuntimeArg]] = None,
                          arg_names: Optional[List[str]] = None,
                          cleanups: Optional[List[str]] = None,
                          traceback_code: Optional[str] = None) -> None:
    """Generates the core part of a wrapper function for a native function.

    This expects each argument as a PyObject * named obj_{arg} as a precondition.
    It converts the PyObject *s to the necessary types, checking and unboxing if necessary,
    makes the call, then boxes the result if necessary and returns it.
    """

    optional_args = optional_args or []
    cleanups = cleanups or []
    use_goto = bool(cleanups or traceback_code)
    error_code = 'return NULL;' if not use_goto else 'goto fail;'

    arg_names = arg_names or [arg.name for arg in fn.args]
    for arg_name, arg in zip(arg_names, fn.args):
        # Suppress the argument check for *args/**kwargs, since we know it must be right.
        typ = arg.type if arg.kind not in (ARG_STAR, ARG_STAR2) else object_rprimitive
        generate_arg_check(arg_name, typ, emitter, error_code, arg in optional_args)
    native_args = ', '.join('arg_{}'.format(arg) for arg in arg_names)
    if fn.ret_type.is_unboxed or use_goto:
        # TODO: The Py_RETURN macros return the correct PyObject * with reference count handling.
        #       Are they relevant?
        emitter.emit_line('{}retval = {}{}({});'.format(emitter.ctype_spaced(fn.ret_type),
                                                        NATIVE_PREFIX,
                                                        fn.cname(emitter.names),
                                                        native_args))
        emitter.emit_lines(*cleanups)
        if fn.ret_type.is_unboxed:
            emitter.emit_error_check('retval', fn.ret_type, 'return NULL;')
            emitter.emit_box('retval', 'retbox', fn.ret_type, declare_dest=True)

        emitter.emit_line('return {};'.format('retbox' if fn.ret_type.is_unboxed else 'retval'))
    else:
        emitter.emit_line('return {}{}({});'.format(NATIVE_PREFIX,
                                                    fn.cname(emitter.names),
                                                    native_args))
        # TODO: Tracebacks?

    if use_goto:
        emitter.emit_label('fail')
        emitter.emit_lines(*cleanups)
        if traceback_code:
            emitter.emit_lines(traceback_code)
        emitter.emit_lines('return NULL;')


def generate_arg_check(name: str, typ: RType, emitter: Emitter,
                       error_code: str, optional: bool = False) -> None:
    """Insert a runtime check for argument and unbox if necessary.

    The object is named PyObject *obj_{}. This is expected to generate
    a value of name arg_{} (unboxed if necessary). For each primitive a runtime
    check ensures the correct type.
    """
    if typ.is_unboxed:
        # Borrow when unboxing to avoid reference count manipulation.
        emitter.emit_unbox('obj_{}'.format(name), 'arg_{}'.format(name), typ,
                           error_code, declare_dest=True, borrow=True, optional=optional)
    elif is_object_rprimitive(typ):
        # Object is trivial since any object is valid
        if optional:
            emitter.emit_line('PyObject *arg_{};'.format(name))
            emitter.emit_line('if (obj_{} == NULL) {{'.format(name))
            emitter.emit_line('arg_{} = {};'.format(name, emitter.c_error_value(typ)))
            emitter.emit_lines('} else {', 'arg_{} = obj_{}; '.format(name, name), '}')
        else:
            emitter.emit_line('PyObject *arg_{} = obj_{};'.format(name, name))
    else:
        emitter.emit_cast('obj_{}'.format(name), 'arg_{}'.format(name), typ,
                          declare_dest=True, optional=optional)
        if optional:
            emitter.emit_line('if (obj_{} != NULL && arg_{} == NULL) {}'.format(
                              name, name, error_code))
        else:
            emitter.emit_line('if (arg_{} == NULL) {}'.format(name, error_code))
