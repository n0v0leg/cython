from Cython.Compiler.Visitor import VisitorTransform, temp_name_handle, CythonTransform
from Cython.Compiler.ModuleNode import ModuleNode
from Cython.Compiler.Nodes import *
from Cython.Compiler.ExprNodes import *
from Cython.Compiler.TreeFragment import TreeFragment
from Cython.Compiler.StringEncoding import EncodedString
from Cython.Compiler.Errors import CompileError
import Interpreter
import PyrexTypes

try:
    set
except NameError:
    from sets import Set as set

import textwrap

# Code cleanup ideas:
# - One could be more smart about casting in some places
# - Start using CCodeWriters to generate utility functions
# - Create a struct type per ndim rather than keeping loose local vars


def dedent(text, reindent=0):
    text = textwrap.dedent(text)
    if reindent > 0:
        indent = " " * reindent
        text = '\n'.join([indent + x for x in text.split('\n')])
    return text

class IntroduceBufferAuxiliaryVars(CythonTransform):

    #
    # Entry point
    #

    buffers_exists = False

    def __call__(self, node):
        assert isinstance(node, ModuleNode)
        self.max_ndim = 0
        result = super(IntroduceBufferAuxiliaryVars, self).__call__(node)
        if self.buffers_exists:
            use_py2_buffer_functions(node.scope)
            use_empty_bufstruct_code(node.scope, self.max_ndim)
        return result


    #
    # Basic operations for transforms
    #
    def handle_scope(self, node, scope):
        # For all buffers, insert extra variables in the scope.
        # The variables are also accessible from the buffer_info
        # on the buffer entry
        bufvars = [entry for name, entry
                   in scope.entries.iteritems()
                   if entry.type.is_buffer]
        if len(bufvars) > 0:
            self.buffers_exists = True


        if isinstance(node, ModuleNode) and len(bufvars) > 0:
            # for now...note that pos is wrong 
            raise CompileError(node.pos, "Buffer vars not allowed in module scope")
        for entry in bufvars:
            name = entry.name
            buftype = entry.type
            if buftype.ndim > self.max_ndim:
                self.max_ndim = buftype.ndim

            # Declare auxiliary vars
            cname = scope.mangle(Naming.bufstruct_prefix, name)
            bufinfo = scope.declare_var(name="$%s" % cname, cname=cname,
                                        type=PyrexTypes.c_py_buffer_type, pos=node.pos)
            if entry.is_arg:
                bufinfo.used = True # otherwise, NameNode will mark whether it is used

            def var(prefix, idx, initval):
                cname = scope.mangle(prefix, "%d_%s" % (idx, name))
                result = scope.declare_var("$%s" % cname, PyrexTypes.c_py_ssize_t_type,
                                         node.pos, cname=cname, is_cdef=True)

                result.init = initval
                if entry.is_arg:
                    result.used = True
                return result
            

            stridevars = [var(Naming.bufstride_prefix, i, "0") for i in range(entry.type.ndim)]
            shapevars = [var(Naming.bufshape_prefix, i, "0") for i in range(entry.type.ndim)]            
            mode = entry.type.mode
            if mode == 'full':
                suboffsetvars = [var(Naming.bufsuboffset_prefix, i, "-1") for i in range(entry.type.ndim)]
            else:
                suboffsetvars = None

            entry.buffer_aux = Symtab.BufferAux(bufinfo, stridevars, shapevars, suboffsetvars)
            
        scope.buffer_entries = bufvars
        self.scope = scope

    def visit_ModuleNode(self, node):
        self.handle_scope(node, node.scope)
        self.visitchildren(node)
        return node

    def visit_FuncDefNode(self, node):
        self.handle_scope(node, node.local_scope)
        self.visitchildren(node)
        return node

#
# Analysis
#
buffer_options = ("dtype", "ndim", "mode") # ordered!
buffer_defaults = {"ndim": 1, "mode": "full"}
buffer_positional_options_count = 1 # anything beyond this needs keyword argument

ERR_BUF_OPTION_UNKNOWN = '"%s" is not a buffer option'
ERR_BUF_TOO_MANY = 'Too many buffer options'
ERR_BUF_DUP = '"%s" buffer option already supplied'
ERR_BUF_MISSING = '"%s" missing'
ERR_BUF_MODE = 'Only allowed buffer modes are: "c", "fortran", "full", "strided" (as a compile-time string)'
ERR_BUF_NDIM = 'ndim must be a non-negative integer'
ERR_BUF_DTYPE = 'dtype must be "object", numeric type or a struct'

def analyse_buffer_options(globalpos, env, posargs, dictargs, defaults=None, need_complete=True):
    """
    Must be called during type analysis, as analyse is called
    on the dtype argument.

    posargs and dictargs should consist of a list and a dict
    of tuples (value, pos). Defaults should be a dict of values.

    Returns a dict containing all the options a buffer can have and
    its value (with the positions stripped).
    """
    if defaults is None:
        defaults = buffer_defaults
    
    posargs, dictargs = Interpreter.interpret_compiletime_options(posargs, dictargs, type_env=env)
    
    if len(posargs) > buffer_positional_options_count:
        raise CompileError(posargs[-1][1], ERR_BUF_TOO_MANY)

    options = {}
    for name, (value, pos) in dictargs.iteritems():
        if not name in buffer_options:
            raise CompileError(pos, ERR_BUF_OPTION_UNKNOWN % name)
        options[name.encode("ASCII")] = value
    
    for name, (value, pos) in zip(buffer_options, posargs):
        if not name in buffer_options:
            raise CompileError(pos, ERR_BUF_OPTION_UNKNOWN % name)
        if name in options:
            raise CompileError(pos, ERR_BUF_DUP % name)
        options[name] = value

    # Check that they are all there and copy defaults
    for name in buffer_options:
        if not name in options:
            try:
                options[name] = defaults[name]
            except KeyError:
                if need_complete:
                    raise CompileError(globalpos, ERR_BUF_MISSING % name)

    dtype = options.get("dtype")
    if dtype and dtype.is_extension_type:
        raise CompileError(globalpos, ERR_BUF_DTYPE)

    ndim = options.get("ndim")
    if ndim and (not isinstance(ndim, int) or ndim < 0):
        raise CompileError(globalpos, ERR_BUF_NDIM)

    mode = options.get("mode")
    if mode and not (mode in ('full', 'strided', 'c', 'fortran')):
        raise CompileError(globalpos, ERR_BUF_MODE)

    return options
    

#
# Code generation
#


def get_flags(buffer_aux, buffer_type):
    flags = 'PyBUF_FORMAT'
    mode = buffer_type.mode
    if mode == 'full':
        flags += '| PyBUF_INDIRECT'
    elif mode == 'strided':
        flags += '| PyBUF_STRIDES'
    elif mode == 'c':
        flags += '| PyBUF_C_CONTIGUOUS'
    elif mode == 'fortran':
        flags += '| PyBUF_F_CONTIGUOUS'
    else:
        assert False
    if buffer_aux.writable_needed: flags += "| PyBUF_WRITABLE"
    return flags
        
def used_buffer_aux_vars(entry):
    buffer_aux = entry.buffer_aux
    buffer_aux.buffer_info_var.used = True
    for s in buffer_aux.shapevars: s.used = True
    for s in buffer_aux.stridevars: s.used = True
    if buffer_aux.suboffsetvars:
        for s in buffer_aux.suboffsetvars: s.used = True

def put_unpack_buffer_aux_into_scope(buffer_aux, mode, code):
    # Generate code to copy the needed struct info into local
    # variables.
    bufstruct = buffer_aux.buffer_info_var.cname

    varspec = [("strides", buffer_aux.stridevars),
               ("shape", buffer_aux.shapevars)]
    if mode == 'full':
        varspec.append(("suboffsets", buffer_aux.suboffsetvars))

    for field, vars in varspec:
        code.putln(" ".join(["%s = %s.%s[%d];" %
                             (s.cname, bufstruct, field, idx)
                             for idx, s in enumerate(vars)]))

def put_acquire_arg_buffer(entry, code, pos):
    code.globalstate.use_utility_code(acquire_utility_code)
    buffer_aux = entry.buffer_aux
    getbuffer_cname = get_getbuffer_code(entry.type.dtype, code)
    # Acquire any new buffer
    code.putln(code.error_goto_if("%s((PyObject*)%s, &%s, %s, %d) == -1" % (
        getbuffer_cname,
        entry.cname,
        entry.buffer_aux.buffer_info_var.cname,
        get_flags(buffer_aux, entry.type),
        entry.type.ndim), pos))
    # An exception raised in arg parsing cannot be catched, so no
    # need to care about the buffer then.
    put_unpack_buffer_aux_into_scope(buffer_aux, entry.type.mode, code)

#def put_release_buffer_normal(entry, code):
#    code.putln("if (%s != Py_None) PyObject_ReleaseBuffer(%s, &%s);" % (
#        entry.cname,
#        entry.cname,
#        entry.buffer_aux.buffer_info_var.cname))

def get_release_buffer_code(entry):
    return "__Pyx_SafeReleaseBuffer(&%s)" % entry.buffer_aux.buffer_info_var.cname

def put_assign_to_buffer(lhs_cname, rhs_cname, buffer_aux, buffer_type,
                         is_initialized, pos, code):
    """
    Generate code for reassigning a buffer variables. This only deals with getting
    the buffer auxiliary structure and variables set up correctly, the assignment
    itself and refcounting is the responsibility of the caller.

    However, the assignment operation may throw an exception so that the reassignment
    never happens.
    
    Depending on the circumstances there are two possible outcomes:
    - Old buffer released, new acquired, rhs assigned to lhs
    - Old buffer released, new acquired which fails, reaqcuire old lhs buffer
      (which may or may not succeed).
    """

    code.globalstate.use_utility_code(acquire_utility_code)
    bufstruct = buffer_aux.buffer_info_var.cname
    flags = get_flags(buffer_aux, buffer_type)

    getbuffer = "%s((PyObject*)%%s, &%s, %s, %d)" % (get_getbuffer_code(buffer_type.dtype, code),
                                          # note: object is filled in later (%%s)
                                          bufstruct,
                                          flags,
                                          buffer_type.ndim)

    if is_initialized:
        # Release any existing buffer
        code.putln('__Pyx_SafeReleaseBuffer(&%s);' % bufstruct)
        # Acquire
        retcode_cname = code.funcstate.allocate_temp(PyrexTypes.c_int_type)
        code.putln("%s = %s;" % (retcode_cname, getbuffer % rhs_cname))
        code.putln('if (%s) ' % (code.unlikely("%s < 0" % retcode_cname)))
        # If acquisition failed, attempt to reacquire the old buffer
        # before raising the exception. A failure of reacquisition
        # will cause the reacquisition exception to be reported, one
        # can consider working around this later.
        code.begin_block()
        type, value, tb = [code.funcstate.allocate_temp(PyrexTypes.py_object_type)
                           for i in range(3)]
        code.putln('PyErr_Fetch(&%s, &%s, &%s);' % (type, value, tb))
        code.put('if (%s) ' % code.unlikely("%s == -1" % (getbuffer % lhs_cname)))
        code.begin_block()
        code.putln('Py_XDECREF(%s); Py_XDECREF(%s); Py_XDECREF(%s);' % (type, value, tb))
        code.globalstate.use_utility_code(raise_buffer_fallback_code)
        code.putln('__Pyx_RaiseBufferFallbackError();')
        code.putln('} else {')
        code.putln('PyErr_Restore(%s, %s, %s);' % (type, value, tb))
        for t in (type, value, tb):
            code.funcstate.release_temp(t)
        code.end_block()
        # Unpack indices
        code.end_block()
        put_unpack_buffer_aux_into_scope(buffer_aux, buffer_type.mode, code)
        code.putln(code.error_goto_if_neg(retcode_cname, pos))
        code.funcstate.release_temp(retcode_cname)
    else:
        # Our entry had no previous value, so set to None when acquisition fails.
        # In this case, auxiliary vars should be set up right in initialization to a zero-buffer,
        # so it suffices to set the buf field to NULL.
        code.putln('if (%s) {' % code.unlikely("%s == -1" % (getbuffer % rhs_cname)))
        code.putln('%s = %s; Py_INCREF(Py_None); %s.buf = NULL;' %
                   (lhs_cname,
                    PyrexTypes.typecast(buffer_type, PyrexTypes.py_object_type, "Py_None"),
                    bufstruct))
        code.putln(code.error_goto(pos))
        code.put('} else {')
        # Unpack indices
        put_unpack_buffer_aux_into_scope(buffer_aux, buffer_type.mode, code)
        code.putln('}')


def put_buffer_lookup_code(entry, index_signeds, index_cnames, options, pos, code):
    """
    Generates code to process indices and calculate an offset into
    a buffer. Returns a C string which gives a pointer which can be
    read from or written to at will (it is an expression so caller should
    store it in a temporary if it is used more than once).

    As the bounds checking can have any number of combinations of unsigned
    arguments, smart optimizations etc. we insert it directly in the function
    body. The lookup however is delegated to a inline function that is instantiated
    once per ndim (lookup with suboffsets tend to get quite complicated).

    """
    bufaux = entry.buffer_aux
    bufstruct = bufaux.buffer_info_var.cname

    if options['boundscheck']:
        # Check bounds and fix negative indices.
        # We allocate a temporary which is initialized to -1, meaning OK (!).
        # If an error occurs, the temp is set to the dimension index the
        # error is occuring at.
        tmp_cname = code.funcstate.allocate_temp(PyrexTypes.c_int_type)
        code.putln("%s = -1;" % tmp_cname)
        for dim, (signed, cname, shape) in enumerate(zip(index_signeds, index_cnames,
                                                         bufaux.shapevars)):
            if signed != 0:
                # not unsigned, deal with negative index
                code.putln("if (%s < 0) {" % cname)
                code.putln("%s += %s;" % (cname, shape.cname))
                code.putln("if (%s) %s = %d;" % (
                    code.unlikely("%s < 0" % cname), tmp_cname, dim))
                code.put("} else ")
            # check bounds in positive direction
            code.putln("if (%s) %s = %d;" % (
                code.unlikely("%s >= %s" % (cname, shape.cname)),
                tmp_cname, dim))
        code.globalstate.use_utility_code(raise_indexerror_code)
        code.put("if (%s) " % code.unlikely("%s != -1" % tmp_cname))
        code.begin_block()
        code.putln('__Pyx_RaiseBufferIndexError(%s);' % tmp_cname)
        code.putln(code.error_goto(pos))
        code.end_block()
        code.funcstate.release_temp(tmp_cname)
    else:
        # Only fix negative indices.
        for signed, cname, shape in zip(index_signeds, index_cnames,
                                        bufaux.shapevars):
            if signed != 0:
                code.putln("if (%s < 0) %s += %s;" % (cname, cname, shape.cname))
        
    # Create buffer lookup and return it
    # This is done via utility macros/inline functions, which vary
    # according to the access mode used.
    params = []
    nd = entry.type.ndim
    mode = entry.type.mode
    if mode == 'full':
        for i, s, o in zip(index_cnames, bufaux.stridevars, bufaux.suboffsetvars):
            params.append(i)
            params.append(s.cname)
            params.append(o.cname)
        funcname = "__Pyx_BufPtrFull%dd" % nd
        funcgen = buf_lookup_full_code
    else:
        if mode == 'strided':
            funcname = "__Pyx_BufPtrStrided%dd" % nd
            funcgen = buf_lookup_strided_code
        elif mode == 'c':
            funcname = "__Pyx_BufPtrCContig%dd" % nd
            funcgen = buf_lookup_c_code
        elif mode == 'fortran':
            funcname = "__Pyx_BufPtrFortranContig%dd" % nd
            funcgen = buf_lookup_fortran_code
        else:
            assert False
        for i, s in zip(index_cnames, bufaux.stridevars):
            params.append(i)
            params.append(s.cname)
        
    # Make sure the utility code is available
    code.globalstate.use_generated_code(funcgen, name=funcname, nd=nd)

    ptr_type = entry.type.buffer_ptr_type
    ptrcode = "%s(%s, %s.buf, %s)" % (funcname,
                                      ptr_type.declaration_code(""),
                                      bufstruct,
                                      ", ".join(params))
    return ptrcode


def use_empty_bufstruct_code(env, max_ndim):
    code = dedent("""
        Py_ssize_t __Pyx_zeros[] = {%s};
        Py_ssize_t __Pyx_minusones[] = {%s};
    """) % (", ".join(["0"] * max_ndim), ", ".join(["-1"] * max_ndim))
    env.use_utility_code([code, ""], "empty_bufstruct_code")


def buf_lookup_full_code(proto, defin, name, nd):
    """
    Generates a buffer lookup function for the right number
    of dimensions. The function gives back a void* at the right location.
    """
    # _i_ndex, _s_tride, sub_o_ffset
    macroargs = ", ".join(["i%d, s%d, o%d" % (i, i, i) for i in range(nd)])
    proto.putln("#define %s(type, buf, %s) (type)(%s_imp(buf, %s))" % (name, macroargs, name, macroargs))

    funcargs = ", ".join(["Py_ssize_t i%d, Py_ssize_t s%d, Py_ssize_t o%d" % (i, i, i) for i in range(nd)])
    proto.putln("static INLINE void* %s_imp(void* buf, %s);" % (name, funcargs))
    defin.putln(dedent("""
        static INLINE void* %s_imp(void* buf, %s) {
          char* ptr = (char*)buf;
        """) % (name, funcargs) + "".join([dedent("""\
          ptr += s%d * i%d;
          if (o%d >= 0) ptr = *((char**)ptr) + o%d; 
        """) % (i, i, i, i) for i in range(nd)]
        ) + "\nreturn ptr;\n}")

def buf_lookup_strided_code(proto, defin, name, nd):
    """
    Generates a buffer lookup function for the right number
    of dimensions. The function gives back a void* at the right location.
    """
    # _i_ndex, _s_tride
    args = ", ".join(["i%d, s%d" % (i, i) for i in range(nd)])
    offset = " + ".join(["i%d * s%d" % (i, i) for i in range(nd)])
    proto.putln("#define %s(type, buf, %s) (type)((char*)buf + %s)" % (name, args, offset))

def buf_lookup_c_code(proto, defin, name, nd):
    """
    Similar to strided lookup, but can assume that the last dimension
    doesn't need a multiplication as long as.
    Still we keep the same signature for now.
    """
    if nd == 1:
        proto.putln("#define %s(type, buf, i0, s0) ((type)buf + i0)" % name)
    else:
        args = ", ".join(["i%d, s%d" % (i, i) for i in range(nd)])
        offset = " + ".join(["i%d * s%d" % (i, i) for i in range(nd - 1)])
        proto.putln("#define %s(type, buf, %s) ((type)((char*)buf + %s) + i%d)" % (name, args, offset, nd - 1))

def buf_lookup_fortran_code(proto, defin, name, nd):
    """
    Like C lookup, but the first index is optimized instead.
    """
    if nd == 1:
        proto.putln("#define %s(type, buf, i0, s0) ((type)buf + i0)" % name)
    else:
        args = ", ".join(["i%d, s%d" % (i, i) for i in range(nd)])
        offset = " + ".join(["i%d * s%d" % (i, i) for i in range(1, nd)])
        proto.putln("#define %s(type, buf, %s) ((type)((char*)buf + %s) + i%d)" % (name, args, offset, 0))

#
# Utils for creating type string checkers
#
def mangle_dtype_name(dtype):
    # Use prefixes to seperate user defined types from builtins
    # (consider "typedef float unsigned_int")
    if dtype.is_pyobject:
        return "object"
    elif dtype.is_ptr:
        return "ptr"
    else:
        if dtype.typestring is None:
            prefix = "nn_"
        else:
            prefix = ""
        return prefix + dtype.declaration_code("").replace(" ", "_")

def get_ts_check_item(dtype, writer):
    # See if we can consume one (unnamed) dtype as next item
    # Put native types and structs in seperate namespaces (as one could create a struct named unsigned_int...)
    name = "__Pyx_BufferTypestringCheck_item_%s" % mangle_dtype_name(dtype)
    if not writer.globalstate.has_utility_code(name):
        char = dtype.typestring
        if char is not None:
            # Can use direct comparison
            code = dedent("""\
                if (*ts == '1') ++ts;
                if (*ts != '%s') {
                  PyErr_Format(PyExc_ValueError, "Buffer datatype mismatch (expected '%s', got '%%s')", ts);
                  return NULL;
                } else return ts + 1;
            """, 2) % (char, char)
        else:
            # Cannot trust declared size; but rely on int vs float and
            # signed/unsigned to be correctly declared
            ctype = dtype.declaration_code("")
            code = dedent("""\
                int ok;
                if (*ts == '1') ++ts;
                switch (*ts) {""", 2)
            if dtype.is_int:
                types = [
                    ('b', 'char'), ('h', 'short'), ('i', 'int'),
                    ('l', 'long'), ('q', 'long long')
                ]
            elif dtype.is_float:
                types = [('f', 'float'), ('d', 'double'), ('g', 'long double')]
            else:
                assert dtype.is_error
                return name
            if dtype.signed == 0:
                code += "".join(["\n    case '%s': ok = (sizeof(%s) == sizeof(%s) && (%s)-1 > 0); break;" %
                                 (char.upper(), ctype, against, ctype) for char, against in types])
            else:
                code += "".join(["\n    case '%s': ok = (sizeof(%s) == sizeof(%s) && (%s)-1 < 0); break;" %
                                 (char, ctype, against, ctype) for char, against in types])
            code += dedent("""\
                default: ok = 0;
                }
                if (!ok) {
                    PyErr_Format(PyExc_ValueError, "Buffer datatype mismatch (rejecting on '%s')", ts);
                    return NULL;
                } else return ts + 1;
                """, 2)
            

        writer.globalstate.use_utility_code([dedent("""\
            static const char* %s(const char* ts); /*proto*/
        """) % name, dedent("""
            static const char* %s(const char* ts) {
            %s
            }
        """) % (name, code)], name=name)

    return name

def get_getbuffer_code(dtype, code):
    """
    Generate a utility function for getting a buffer for the given dtype.
    The function will:
    - Call PyObject_GetBuffer
    - Check that ndim matched the expected value
    - Check that the format string is right
    - Set suboffsets to all -1 if it is returned as NULL.
    """

    name = "__Pyx_GetBuffer_%s" % mangle_dtype_name(dtype)
    if not code.globalstate.has_utility_code(name):
        code.globalstate.use_utility_code(acquire_utility_code)
        itemchecker = get_ts_check_item(dtype, code)
        utilcode = [dedent("""
        static int %s(PyObject* obj, Py_buffer* buf, int flags, int nd); /*proto*/
        """) % name, dedent("""
        static int %(name)s(PyObject* obj, Py_buffer* buf, int flags, int nd) {
          const char* ts;
          if (obj == Py_None) {
            __Pyx_ZeroBuffer(buf);
            return 0;
          }
          buf->buf = NULL;
          if (__Pyx_GetBuffer(obj, buf, flags) == -1) goto fail;
          if (buf->ndim != nd) {
            __Pyx_BufferNdimError(buf, nd);
            goto fail;
          }
          ts = buf->format;
          ts = __Pyx_ConsumeWhitespace(ts);
          if (!ts) goto fail;
          ts = %(itemchecker)s(ts);
          if (!ts) goto fail;
          ts = __Pyx_ConsumeWhitespace(ts);
          if (!ts) goto fail;
          if (*ts != 0) {
            PyErr_Format(PyExc_ValueError,
              "Expected non-struct buffer data type (expected end, got '%%s')", ts);
            goto fail;
          }
          if (buf->suboffsets == NULL) buf->suboffsets = __Pyx_minusones;
          return 0;
        fail:;
          __Pyx_ZeroBuffer(buf);
          return -1;
        }""") % locals()]
        code.globalstate.use_utility_code(utilcode, name)
    return name

def buffer_type_checker(dtype, code):
    # Creates a type checker function for the given type.
    if dtype.is_struct_or_union:
        assert False
    elif dtype.is_int or dtype.is_float:
        # This includes simple typedef-ed types
        funcname = get_getbuffer_code(dtype, code)
    else:
        assert False
    return funcname

def use_py2_buffer_functions(env):
    # Emulation of PyObject_GetBuffer and PyBuffer_Release for Python 2.
    # For >= 2.6 we do double mode -- use the new buffer interface on objects
    # which has the right tp_flags set, but emulation otherwise.
    codename = "PyObject_GetBuffer" # just a representative unique key

    # Search all types for __getbuffer__ overloads
    types = []
    def find_buffer_types(scope):
        for m in scope.cimported_modules:
            find_buffer_types(m)
        for e in scope.type_entries:
            t = e.type
            if t.is_extension_type:
                release = get = None
                for x in t.scope.pyfunc_entries:
                    if x.name == u"__getbuffer__": get = x.func_cname
                    elif x.name == u"__releasebuffer__": release = x.func_cname
                if get:
                    types.append((t.typeptr_cname, get, release))

    find_buffer_types(env)

    code = dedent("""
        #if PY_MAJOR_VERSION < 3
        static int __Pyx_GetBuffer(PyObject *obj, Py_buffer *view, int flags) {
          #if PY_VERSION_HEX >= 0x02060000
          if (Py_TYPE(obj)->tp_flags & Py_TPFLAGS_HAVE_NEWBUFFER)
              return PyObject_GetBuffer(obj, view, flags);
          #endif
    """)
    if len(types) > 0:
        clause = "if"
        for t, get, release in types:
            code += "  %s (PyObject_TypeCheck(obj, %s)) return %s(obj, view, flags);\n" % (clause, t, get)
            clause = "else if"
        code += "  else {\n"
    code += dedent("""\
        PyErr_Format(PyExc_TypeError, "'%100s' does not have the buffer interface", Py_TYPE(obj)->tp_name);
        return -1;
    """, 2)
    if len(types) > 0: code += "  }"
    code += dedent("""
        }

        static void __Pyx_ReleaseBuffer(Py_buffer *view) {
          PyObject* obj = view->obj;
          if (obj) {
    """)
    if len(types) > 0:
        clause = "if"
        for t, get, release in types:
            if release:
                code += "%s (PyObject_TypeCheck(obj, %s)) %s(obj, view);" % (clause, t, release)
                clause = "else if"
    code += dedent("""
            Py_DECREF(obj);
            view->obj = NULL;
          }
        }

        #endif
    """)
                   
    env.use_utility_code([dedent("""\
        #if PY_MAJOR_VERSION < 3
        static int __Pyx_GetBuffer(PyObject *obj, Py_buffer *view, int flags);
        static void __Pyx_ReleaseBuffer(Py_buffer *view);
        #else
        #define __Pyx_GetBuffer PyObject_GetBuffer
        #define __Pyx_ReleaseBuffer PyBuffer_Release
        #endif
    """), code], codename)

#
# Static utility code
#


# Utility function to set the right exception
# The caller should immediately goto_error
raise_indexerror_code = [
"""\
static void __Pyx_RaiseBufferIndexError(int axis); /*proto*/
""","""\
static void __Pyx_RaiseBufferIndexError(int axis) {
  PyErr_Format(PyExc_IndexError,
     "Out of bounds on buffer access (axis %d)", axis);
}

"""]

#
# Buffer type checking. Utility code for checking that acquired
# buffers match our assumptions. We only need to check ndim and
# the format string; the access mode/flags is checked by the
# exporter.
#
acquire_utility_code = ["""\
static INLINE void __Pyx_SafeReleaseBuffer(Py_buffer* info);
static INLINE void __Pyx_ZeroBuffer(Py_buffer* buf); /*proto*/
static INLINE const char* __Pyx_ConsumeWhitespace(const char* ts); /*proto*/
static void __Pyx_BufferNdimError(Py_buffer* buffer, int expected_ndim); /*proto*/
""", """
static INLINE void __Pyx_SafeReleaseBuffer(Py_buffer* info) {
  if (info->buf == NULL) return;
  if (info->suboffsets == __Pyx_minusones) info->suboffsets = NULL;
  __Pyx_ReleaseBuffer(info);
}

static INLINE void __Pyx_ZeroBuffer(Py_buffer* buf) {
  buf->buf = NULL;
  buf->obj = NULL;
  buf->strides = __Pyx_zeros;
  buf->shape = __Pyx_zeros;
  buf->suboffsets = __Pyx_minusones;
}

static INLINE const char* __Pyx_ConsumeWhitespace(const char* ts) {
  while (1) {
    switch (*ts) {
      case '@':
      case 10:
      case 13:
      case ' ':
        ++ts;
        break;
      case '=':
      case '<':
      case '>':
      case '!':
        PyErr_SetString(PyExc_ValueError, "Buffer acquisition error: Only native byte order, size and alignment supported.");
        return NULL;               
      default:
        return ts;
    }
  }
}

static void __Pyx_BufferNdimError(Py_buffer* buffer, int expected_ndim) {
  PyErr_Format(PyExc_ValueError,
               "Buffer has wrong number of dimensions (expected %d, got %d)",
               expected_ndim, buffer->ndim);
}

"""]

raise_buffer_fallback_code = ["""
static void __Pyx_RaiseBufferFallbackError(void); /*proto*/
""","""
static void __Pyx_RaiseBufferFallbackError(void) {
  PyErr_Format(PyExc_ValueError,
     "Buffer acquisition failed on assignment; and then reacquiring the old buffer failed too!");
}

"""]

