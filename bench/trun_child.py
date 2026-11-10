"""Child process: run every test_* function in one agent-written test file against one module directory. Prints JSON."""
import importlib.util, json, sys, types
test_file, mod_dir = sys.argv[1], sys.argv[2]
sys.path.insert(0, mod_dir)
shim = types.ModuleType("pytest")  # tolerate a stray pytest import without running pytest itself


class _Raises:
    def __init__(self, exc, *a, **k): self.exc = exc
    def __enter__(self): return self
    def __exit__(self, t, v, tb):
        if t is None: raise AssertionError("did not raise")
        return issubclass(t, self.exc)


shim.raises = _Raises
sys.modules.setdefault("pytest", shim)
res = {}
try:
    spec = importlib.util.spec_from_file_location("agent_tests", test_file)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
except Exception as e:
    print(json.dumps({"__import_error__": repr(e)})); sys.exit(0)
for name in sorted(n for n in dir(m) if n.startswith("test_") and callable(getattr(m, n))):
    try:
        getattr(m, name)(); res[name] = True
    except Exception:
        res[name] = False
print(json.dumps(res))
