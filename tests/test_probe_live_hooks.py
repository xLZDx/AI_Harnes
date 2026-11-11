"""Tests for tools/probe_live_hooks.py. Expected values come from the module docstring.
Only temp dirs and tiny fake hook scripts are used; the real ~/.claude is never touched."""
import contextlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "probe_live_hooks", Path(__file__).resolve().parent.parent / "tools" / "probe_live_hooks.py")
plh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plh)

PY = "C:/Python314/python.exe"
REQ = "shell_policy_gate.py"
_TMPS = []
_fails = []

DENY_EXIT2 = "import sys; sys.exit(2)"
DENY_HSO = ('import sys; sys.stdin.read(); '
            'print(\'{"hookSpecificOutput":{"permissionDecision":"deny"}}\')')
DENY_BLOCK = "import sys; sys.stdin.read(); print('{\"decision\":\"block\"}')"
ALLOW = "import sys; sys.stdin.read()"
TEXT_DENY = "import sys; sys.stdin.read(); print('I would deny this command')"
BAD_JSON = "import sys; sys.stdin.read(); print('{\"decision\": \"blo')"
SLEEP = "import time; time.sleep(6)"
EXIT1 = "import sys; sys.exit(1)"


def workdir():
    d = Path(tempfile.mkdtemp(prefix="plh_"))
    _TMPS.append(d)
    return d


def make(code, name=REQ, matcher="Bash", interpreter=PY, d=None):
    """Write a hook script + settings.json; return the settings path."""
    d = d or workdir()
    script = d / name
    script.write_text(code, encoding="utf-8")
    s = d / "settings.json"
    s.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": matcher, "hooks": [
        {"type": "command", "command": interpreter, "args": [str(script)]}]}]}}), encoding="utf-8")
    return s


def run_main(settings, *extra):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = plh.main(["--settings", str(settings), *extra])
    return rc, buf.getvalue()


def check(name, cond, detail=""):
    if cond:
        print("PASS", name)
    else:
        print("FAIL", name, detail)
        _fails.append(name)


def expect_ok(name, settings):
    ok, msg = plh.probe(settings, REQ, 30)
    rc, out = run_main(settings)
    check(name, ok is True and rc == 0 and "PROBE OK" in out, f"ok={ok} msg={msg} rc={rc}")


def expect_fail(name, settings, text=None, timeout=30):
    ok, msg = plh.probe(settings, REQ, timeout)
    rc, out = run_main(settings, "--timeout", str(timeout))
    good = ok is False and rc == 1 and "PROBE FAILED" in out
    if text:
        good = good and text in msg
    check(name, good, f"ok={ok} msg={msg} rc={rc}")


expect_ok("01 exit 2 blocks", make(DENY_EXIT2))
expect_ok("02 JSON permissionDecision deny", make(DENY_HSO))
expect_ok("03 JSON decision block", make(DENY_BLOCK))

# 04 required hook missing, another Bash hook present (allowing)
expect_fail("04 required hook missing", make(DENY_EXIT2, name="other_gate.py"), "not configured")

expect_fail("05 allow (empty stdout)", make(ALLOW), "allow")
expect_fail("06 'deny' in non-JSON text is unknown", make(TEXT_DENY), "unknown")
expect_fail("07 malformed JSON", make(BAD_JSON))
expect_fail("08 timeout", make(SLEEP), "timed out", timeout=1)
expect_fail("09 interpreter does not exist", make(DENY_EXIT2, interpreter="C:/no/such/python.exe"))

# 10 settings problems
d = workdir()
expect_fail("10a settings missing", d / "nope.json")
bad = d / "bad.json"
bad.write_text("{not json", encoding="utf-8")
expect_fail("10b settings invalid JSON", bad)
nopre = d / "nopre.json"
nopre.write_text(json.dumps({"hooks": {"PostToolUse": []}}), encoding="utf-8")
expect_fail("10c no PreToolUse", nopre)
nohooks = d / "nohooks.json"
nohooks.write_text(json.dumps({"model": "x"}), encoding="utf-8")
expect_fail("10d no hooks key", nohooks)

expect_fail("11 hook exits 1", make(EXIT1), "unknown")
expect_fail("12 matcher excludes Bash", make(DENY_EXIT2, matcher="Edit|Write"), "not configured")

# 13 other Bash-matched hook denies, required one absent
d13 = workdir()
other = d13 / "other_gate.py"
other.write_text(DENY_EXIT2, encoding="utf-8")
s13 = d13 / "settings.json"
s13.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
    {"type": "command", "command": PY, "args": [str(other)]}]}]}}), encoding="utf-8")
expect_fail("13 other denying hook does not satisfy requirement", s13, "not configured")

# 14 (extra) custom --require is honoured
s14 = make(DENY_EXIT2, name="custom_gate.py")
rc, out = run_main(s14, "--require", "custom_gate.py")
check("14 --require custom name", rc == 0 and "PROBE OK" in out, f"rc={rc} out={out}")

for t in _TMPS:
    shutil.rmtree(t, ignore_errors=True)
if _fails:
    print(f"FAILED: {len(_fails)} failing")
    sys.exit(1)
print("OK: 0 failing")
