# MIT License
#
# Copyright (c) 2019 Jason Dixon
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from __future__ import print_function

import os
import sys
import math
import types
import pickle
import marshal
try: # Python 2
    import copy_reg as copyreg
    STD_TYPES = [k for k,v in pickle.Pickler.dispatch.items() if v.__name__ not in ("save_global", "save_inst")]
except ImportError:
    import copyreg
    xrange = range
    STD_TYPES = [k for k,v in pickle._Pickler.dispatch.items() if v.__name__ not in ("save_global", "save_type")]

SEQ_TYPES = (list, tuple, set)
FUNC_TYPES = (types.FunctionType, types.MethodType, types.LambdaType, types.BuiltinFunctionType)
TRACEBACK_TYPES = (types.TracebackType, types.FrameType, types.CodeType)

def init(pickler=None, depth=3, include_source=True, limit=None): # Prepare traceback pickle functionality
    """
        Prep traceback for pickling. Run this to allow pickling of traceback types.

        Args:
            pickler (Callable):
                * If set to None. tracebacks are pickled in "conservative" mode. Classes are mocked, and objects replaced
                  with representations, such that the traceback can be inspected regardless of environment.
                * If set to a pickler callable (ie pickle.dumps / dill.dumps etc), objects will be pickled by the pickler,
                  and only if it fails will they be mocked.
                  This has the advantage of including real functional objects in the traceback,
                  at the cost of requiring the original environment to unpickle.
            depth (int):
                * How far to fan out from the traceback before returning representations of everything.
                  The higher the value, the more you can inspect at the cost of extra pickle size.
                  A value of -1 means no limit. Fan out forever and grab everything.
            include_source (bool):
                * Include source code in pickle, and reconstruct on unpickle for debugging. On by default.
                  Recommended, though it will eat up extra space if there are numerous traceback dumps.
            limit (int):
                * Reduce the depth traversed in the traceback (different from depth setting above
                  which handles depth from traceback). By default this is set high enough to sit just
                  below the recursion limit.
        Returns:
            None:
    """
    def prepare_traceback(trace):
        clean_trace = _Cleaner(pickler, limit).clean(trace, depth) # Make traceback pickle friendly
        if include_source:
            files = _snapshot_source_files(trace) # Take a snapshot of all the source files
            return cache_files, (clean_trace, files)
        return clean_trace.func, clean_trace.args

    @_savePickle
    def cache_files(trace, files):
        import linecache # Add source files to linecache for debugger to see them.
        for name, data in files.items():
            lines = [line + "\n" for line in data.splitlines()]
            linecache.cache[name] = (len(data), None, lines, name)
        return trace

    copyreg.pickle(types.TracebackType, prepare_traceback)

# There is a bug in python 2.* pickle (not cPickle) that struggles to handle
# recursive reduction objects. If using python 2.*, try to always pickle with cPickle
# to be safe.
class _call(object):
    """ Basic building block in pickle """
    def __reduce__(self):
        return self.func, self.args
    def __init__(self, func, *args):
        self.__dict__.update(locals())
    def __call__(self):
        self.func(*(arg() if isinstance(arg, Call) else arg for arg in self.args))

class _import(_call):
    def __init__(self, name):
        super(_import, self).__init__(__import__, name)

class _from_import(_call):
    def __init__(self, module, name):
        super(_from_import, self).__init__(getattr, _import(module), name)

def _savePickle(func):
    """ Save function directly in pickle """
    typeMap = {types.FunctionType: "FunctionType", types.LambdaType: "LambdaType"}
    funcType = typeMap.get(type(func))
    FunctionType = _from_import("types", funcType)
    code = _call(marshal.loads, marshal.dumps(func.__code__))
    scoped_call = type(func.__name__, (_call, ), {"__call__": lambda _, *a, **k: func(*a, **k)})
    return scoped_call(FunctionType, code, {"__builtins__": _from_import("types", "__builtins__")})

_mock = _call(type, "mock", (object, ), {
    "__init__": _savePickle(lambda s, d: s.__setattr__("__dict__", d)), # We cannot lose reference to this dict.
    "__class__": _call(property, _savePickle(lambda s: s._mock)), # pretend to be this
    "__repr__": _savePickle(lambda s: s._repr)}) # and look like this

def _snapshot_source_files(trace):
    """ Grab all source file information from traceback """
    files = {}
    while trace:
        frame = trace.tb_frame
        while frame:
            filename = os.path.abspath(frame.f_code.co_filename)
            if filename not in files:
                try:
                    with open(filename) as f:
                        files[filename] = f.read()
                except IOError:
                    files[filename] = "Couldn't locate '%s' during dump." % frame.f_code.co_filename
            frame = frame.f_back
        trace = trace.tb_next
    return files

@_savePickle
def _stub(*_, **__):
    """ Replacement for sanitized functions """
    raise UserWarning("This is a stub function. The original was not serialized.")

@_savePickle
def _safe_restore(data, rep):
    """ Safely restore pickled data, resorting to representation on fail """
    import pickle
    try:
        return pickle.loads(data)
    except Exception:
        return rep

class _Cleaner(object):
    """ Clean up pickleable objects """
    def __init__(self, pickler=None, limit=None):
        self.pickler = pickler
        self.limit = limit or int(math.sqrt(sys.getrecursionlimit())) # Max depth we can traverse before recursion error
        self.seen = {}

    def clean(self, obj, depth):
        depth -= 1

        obj_id = id(obj)
        if obj_id in self.seen: # If we have processed object, skip
            return self.seen[obj_id]

        try:
            obj_type = type(obj)
            if depth == -1: # We have reached our limit. Just make a basic representation
                self.seen[obj_id] = result = repr(obj)
            elif obj_type in TRACEBACK_TYPES:
                result = self.clean_traceback_types(obj, depth)
            elif self.pickler:
                try: # Try to see if we can just pickle straight up
                    self.seen[obj_id] = result = _call(_safe_restore, self.pickler(obj), repr(obj))
                except Exception: # Otherwise fallback to mocks/stubs
                    result = self.clean_fallback(obj, depth)
            else:
                result = self.clean_fallback(obj, depth)
            return result
        except Exception as err:
            self.seen[obj_id] = result = "Failed to serialize object: %s" % err
            return result

    def clean_traceback_types(self, obj, depth):
        """ Clean traceback related objects in a special way """
        obj_id = id(obj)
        obj_type = type(obj)
        if obj_type == types.TracebackType:
            trace = obj
            last_trace = {}
            for _ in xrange(self.limit): # Loop here for less recursive calls to clean
                if not trace:
                    break
                trace_id = id(trace)
                if trace_id in self.seen:
                    last_trace["tb_next"] = self.seen[trace_id]
                    break
                dct = {"_repr": repr(trace), "_mock": _from_import("types", "TracebackType")}
                self.seen[trace_id] = last_trace["tb_next"] = _call(_mock, dct) # Preload to stop recursive cycles
                dct.update((at, getattr(trace, at)) for at in dir(trace) if at.startswith("tb_"))
                dct["tb_frame"] = self.clean(trace.tb_frame, depth+1)
                trace = trace.tb_next
                last_trace = dct
            last_trace["tb_next"] = None
            result = self.seen[obj_id]

        elif obj_type == types.FrameType:
            frame = obj
            last_frame = {}
            while frame: # Loop here for less recursive calls to clean
                frame_id = id(frame)
                if frame_id in self.seen:
                    last_frame["f_back"] = self.seen[frame_id]
                    break
                dct = {"_repr": repr(frame), "_mock": _from_import("types", "FrameType")}
                self.seen[frame_id] = last_frame["f_back"] = _call(_mock, dct) # Preload to stop recursive cycles
                dct.update((at, getattr(frame, at)) for at in dir(frame) if at.startswith("f_"))
                dct["f_builtins"] = _from_import("types", "__builtins__") # Load builtins at unpickle time
                dct["f_globals"] = {k: self.clean(v, depth) for k, v in frame.f_globals.items() if not k.startswith("__")}
                dct["f_locals"] = {k: self.clean(v, depth) for k,v in frame.f_locals.items()}
                dct["f_trace"] = self.clean(frame.f_trace, depth)
                dct["f_code"] = self.clean(frame.f_code, depth+1)
                frame = frame.f_back
                last_frame = dct
            result = self.seen[obj_id]

        else: # obj_type == types.CodeType:
            dct = {"_repr": repr(obj), "_mock": _from_import("types", "CodeType")}
            self.seen[obj_id] = result = _call(_mock, dct) # Preload to stop recursive cycles
            dct.update((at, getattr(obj, at)) for at in dir(obj) if at.startswith("co_"))
            dct["co_consts"] = self.clean(obj.co_consts, depth+2)
            dct["co_filename"] = os.path.abspath(obj.co_filename)
        return result

    def clean_fallback(self, obj, depth):
        """ Fallback to mocking and stubbing everything, for representation purpose only. """
        obj_id = id(obj)
        obj_type = type(obj)
        if obj_type == dict:
            self.seen[obj_id] = result = {self.clean(k, depth): self.clean(v, depth) for k, v in obj.items()}
        elif obj_type in SEQ_TYPES:
            self.seen[obj_id] = result = obj_type(self.clean(o, depth) for o in obj)
        elif obj_type in FUNC_TYPES:
            self.seen[obj_id] = result = _stub
        elif obj_type in STD_TYPES:
            self.seen[obj_id] = result = obj
        elif obj_type == types.ModuleType:
            # TODO: This check is not true!
            if not hasattr(obj, "__file__") or obj.__file__.startswith(os.path.dirname(types.__file__)):
                self.seen[obj_id] = result = _import(obj.__name__) # Standard library stuff. Safe to import this.
            else:
                self.seen[obj_id] = result = repr(obj) # Otherwise sanitize it!
        else:
            try: # Create a mock object as a fake representation of the original for inspection
                dct ={"_repr": repr(obj), "_mock": object}
                self.seen[obj_id] = result = _call(_mock, dct) # Preload to stop recursive cycles
                dct.update((at, self.clean(getattr(obj, at), depth)) for at in dir(obj) if not at.startswith("__"))
            except Exception: # Failing that, just get the representation of the thing...
                self.seen[obj_id] = result = repr(obj)
        return result
