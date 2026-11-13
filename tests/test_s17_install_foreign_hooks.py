"""S17-T1 tests: tools/install_claude.py keeps settings.json hook groups the harness does not own.
Written from the S17-T1 acceptance criteria. The installer runs as a real subprocess (CLI boundary) against a fake
home and a fake build folder in a temp dir; the real ~/.claude is never touched and no environment variable is changed.
Run: C:\\Python314\\python.exe -I tests\\test_s17_install_foreign_hooks.py"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
PS = shutil.which("powershell")
_TMPS = []

ECHO = 'echo {"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}'
CHROME_A = {"matcher": "mcp__claude-in-chrome__.*", "hooks": [{"type": "command", "command": ECHO}]}
CHROME_B = {"matcher": "mcp__claude-in-chrome__javascript_tool", "hooks": [{"type": "command", "command": ECHO}]}
NEW_GROUPS = [{"matcher": "Bash", "hooks": [{"type": "command", "command": "python C:/h/hooks/keep.py"},
                                           {"type": "command", "command": "python C:/h/hooks/fresh.py"}]}]
NEW = {"hooks": {"PreToolUse": NEW_GROUPS}}


def mk(old_settings_bytes):
    root = Path(tempfile.mkdtemp(prefix="s17t1_")).resolve()
    _TMPS.append(root)
    src, home = root / "src", root / "home"
    (src / "hooks").mkdir(parents=True)
    (src / "settings.hooks.json").write_text(json.dumps(NEW), encoding="utf-8")
    for n in ("keep.py", "fresh.py"):
        (src / "hooks" / n).write_text("NEW " + n, encoding="utf-8")
    (home / "hooks").mkdir(parents=True)
    (home / "hooks" / "keep.py").write_text("OLD keep", encoding="utf-8")
    (home / "hooks" / "retired.py").write_text("retired hook", encoding="utf-8")
    (home / "settings.json").write_bytes(old_settings_bytes)
    return src, home, root


def install(home, src, root, name="snap"):
    snap = root / name
    r = subprocess.run([sys.executable, "-I", str(TOOLS / "install_claude.py"), "--home", str(home), "--snapshot", str(snap),
                        "--source", str(src)], capture_output=True, timeout=120)
    return r.returncode, r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace"), snap


def hooks_of(home):
    return json.loads((home / "settings.json").read_text(encoding="utf-8"))["hooks"]


def old_text(hooks, **extra):
    return json.dumps({**extra, "hooks": hooks}, indent=4).encode("utf-8")


# ---------------------------------------------------------------- criterion 1
def test_foreign_chrome_groups_kept_in_order_after_harness_groups():
    old = {"PreToolUse": [CHROME_A, {"matcher": "Bash", "hooks": [{"type": "command", "command": "python C:/h/hooks/keep.py"}]},
                          CHROME_B]}
    src, home, root = mk(old_text(old, permissions={"allow": ["Bash(ls)"]}))
    rc, out, _ = install(home, src, root)
    assert rc == 0, out
    assert hooks_of(home)["PreToolUse"] == NEW_GROUPS + [CHROME_A, CHROME_B], hooks_of(home)["PreToolUse"]
    assert json.loads((home / "settings.json").read_text(encoding="utf-8"))["permissions"] == {"allow": ["Bash(ls)"]}


def test_foreign_group_under_event_the_new_set_lacks_is_kept():
    src, home, root = mk(old_text({"Notification": [CHROME_A]}))
    rc, out, _ = install(home, src, root)
    assert rc == 0, out
    h = hooks_of(home)
    assert h["Notification"] == [CHROME_A] and h["PreToolUse"] == NEW_GROUPS, h


# ---------------------------------------------------------------- criterion 2
def test_harness_groups_replaced_current_retired_and_uppercase_backslash():
    src, home, root = mk(b"{}")
    upper = str(home / "hooks").upper().replace("/", "\\") + "\\GONE_FOR_GOOD.PY"
    old = {"PreToolUse": [
        {"hooks": [{"type": "command", "command": "python C:/old/place/keep.py"}]},        # current built file
        {"hooks": [{"type": "command", "command": "python C:/old/place/retired.py"}]},     # retired, still in <home>/hooks
        {"hooks": [{"type": "command", "command": "python " + upper}]},                    # upper-case backslash path
        {"hooks": [{"type": "command", "command": "python", "args": ["C:/old/place/retired.py"]}]},  # args form
        CHROME_A]}
    (home / "settings.json").write_bytes(old_text(old))
    rc, out, _ = install(home, src, root)
    assert rc == 0, out
    assert hooks_of(home)["PreToolUse"] == NEW_GROUPS + [CHROME_A], hooks_of(home)["PreToolUse"]
    assert "1 foreign hook groups kept" in out, out


def test_run_hook_py_group_is_harness():
    cmd = "py D:/x/RUN_HOOK.PY --harness-client=claude"
    src, home, root = mk(old_text({"Stop": [{"hooks": [{"type": "command", "command": cmd}]}]}))
    rc, out, _ = install(home, src, root)
    assert rc == 0, out
    assert "Stop" not in hooks_of(home), hooks_of(home)
    assert "0 foreign hook groups kept" in out, out


# ---------------------------------------------------------------- criterion 3
def test_second_install_does_not_duplicate_and_names_count():
    src, home, root = mk(old_text({"PreToolUse": [CHROME_A, CHROME_B]}))
    rc, out1, _ = install(home, src, root, "snap1")
    assert rc == 0, out1
    assert "2 foreign hook groups kept" in out1, out1
    first = (home / "settings.json").read_bytes()
    rc, out2, _ = install(home, src, root, "snap2")
    assert rc == 0, out2
    assert "2 foreign hook groups kept" in out2, out2
    assert hooks_of(home)["PreToolUse"] == NEW_GROUPS + [CHROME_A, CHROME_B]
    assert (home / "settings.json").read_bytes() == first, "second install changed settings.json"


def test_identical_foreign_group_twice_in_old_settings_kept_once():
    src, home, root = mk(old_text({"PreToolUse": [CHROME_A, dict(CHROME_A)]}))
    rc, out, _ = install(home, src, root)
    assert rc == 0, out
    assert hooks_of(home)["PreToolUse"] == NEW_GROUPS + [CHROME_A]
    assert "1 foreign hook groups kept" in out, out


# ---------------------------------------------------------------- criterion 4
def test_snapshot_settings_is_byte_identical_and_restore_brings_it_back():
    raw = (b'{\r\n  "model":   "x",\r\n\t"hooks": {"PreToolUse": [{"matcher":"mcp__claude-in-chrome__.*",'
           b'"hooks":[{"type":"command","command":"echo hi"}]}]}\r\n}')
    src, home, root = mk(raw)
    rc, out, snap = install(home, src, root)
    assert rc == 0, out
    assert (snap / "settings.json").read_bytes() == raw
    assert (home / "settings.json").read_bytes() != raw  # control: the install did change the live file
    if not PS:
        print("SKIP restore.ps1 run: powershell not available")
        return
    r = subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(snap / "restore.ps1")],
                       capture_output=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (home / "settings.json").read_bytes() == raw


def main():
    fails = 0
    for name in [n for n, f in list(globals().items()) if n.startswith("test_") and callable(f)]:
        try:
            globals()[name]()
            print(f"PASS {name}")
        except BaseException as e:
            fails += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    for r in _TMPS:
        shutil.rmtree(r, ignore_errors=True)
    print("OK: 0 failing" if not fails else f"FAILED: {fails} failing")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
