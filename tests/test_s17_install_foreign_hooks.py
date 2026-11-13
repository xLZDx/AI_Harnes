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
    src, home, root = mk(b"{}")
    mine = "python " + str(home / "hooks" / "keep.py")  # under <home>/hooks: a harness hook
    old = {"PreToolUse": [CHROME_A, {"matcher": "Bash", "hooks": [{"type": "command", "command": mine}]}, CHROME_B]}
    (home / "settings.json").write_bytes(old_text(old, permissions={"allow": ["Bash(ls)"]}))
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
        {"hooks": [{"type": "command", "command": "python C:/Users/u/.claude/hooks/keep.py"}]},     # /.claude/hooks/ segment
        {"hooks": [{"type": "command", "command": "python " + str(home / "hooks" / "retired.py")}]},  # under <home>/hooks
        {"hooks": [{"type": "command", "command": "python " + upper}]},                    # upper-case backslash path
        {"hooks": [{"type": "command", "command": "python", "args": [str(home / "hooks" / "retired.py")]}]},  # args form
        CHROME_A]}
    (home / "settings.json").write_bytes(old_text(old))
    rc, out, _ = install(home, src, root)
    assert rc == 0, out
    assert hooks_of(home)["PreToolUse"] == NEW_GROUPS + [CHROME_A], hooks_of(home)["PreToolUse"]
    assert "1 foreign hooks kept" in out, out


def test_run_hook_py_group_is_harness():
    cmd = "py D:/x/RUN_HOOK.PY --harness-client=claude"
    src, home, root = mk(old_text({"Stop": [{"hooks": [{"type": "command", "command": cmd}]}]}))
    rc, out, _ = install(home, src, root)
    assert rc == 0, out
    assert "Stop" not in hooks_of(home), hooks_of(home)
    assert "0 foreign hooks kept" in out, out


# ---------------------------------------------------------------- criterion 3
def test_second_install_does_not_duplicate_and_names_count():
    src, home, root = mk(old_text({"PreToolUse": [CHROME_A, CHROME_B]}))
    rc, out1, _ = install(home, src, root, "snap1")
    assert rc == 0, out1
    assert "2 foreign hooks kept" in out1, out1
    first = (home / "settings.json").read_bytes()
    rc, out2, _ = install(home, src, root, "snap2")
    assert rc == 0, out2
    assert "2 foreign hooks kept" in out2, out2
    assert hooks_of(home)["PreToolUse"] == NEW_GROUPS + [CHROME_A, CHROME_B]
    assert (home / "settings.json").read_bytes() == first, "second install changed settings.json"


def test_identical_foreign_group_twice_in_old_settings_kept_once():
    src, home, root = mk(old_text({"PreToolUse": [CHROME_A, dict(CHROME_A)]}))
    rc, out, _ = install(home, src, root)
    assert rc == 0, out
    assert hooks_of(home)["PreToolUse"] == NEW_GROUPS + [CHROME_A]
    assert "1 foreign hooks kept" in out, out
    assert out.count("kept (not a harness hook):") == 1, out


# ---------------------------------------------------------------- S17 review fix: per-hook ownership by location
def kept_lines(out):
    return [ln for ln in out.splitlines() if ln.startswith("kept (not a harness hook):")]


def test_mixed_group_keeps_matcher_and_only_its_foreign_hook():
    src, home, root = mk(b"{}")
    mixed = {"matcher": "Bash", "hooks": [{"type": "command", "command": "python " + str(home / "hooks" / "keep.py")},
                                         {"type": "command", "command": "myaudit --all"}]}
    (home / "settings.json").write_bytes(old_text({"PreToolUse": [mixed]}))
    rc, out, _ = install(home, src, root)
    assert rc == 0, out
    expect = {"matcher": "Bash", "hooks": [{"type": "command", "command": "myaudit --all"}]}
    assert hooks_of(home)["PreToolUse"] == NEW_GROUPS + [expect], hooks_of(home)["PreToolUse"]
    assert "1 foreign hooks kept" in out, out
    assert kept_lines(out) == ["kept (not a harness hook): PreToolUse: myaudit --all"], out


def test_same_named_script_outside_hook_folders_is_foreign_and_kept():
    src, home, root = mk(b"{}")
    other = {"hooks": [{"type": "command", "command": "python D:/operator/tools/keep.py"}]}  # name equals a harness hook
    other2 = {"hooks": [{"type": "command", "command": "python", "args": ["D:/operator/tools/retired.py"]}]}
    (home / "settings.json").write_bytes(old_text({"PreToolUse": [other, other2]}))
    rc, out, _ = install(home, src, root)
    assert rc == 0, out
    assert hooks_of(home)["PreToolUse"] == NEW_GROUPS + [other, other2], hooks_of(home)["PreToolUse"]
    assert "2 foreign hooks kept" in out, out
    assert len(kept_lines(out)) == 2 and "D:/operator/tools/keep.py" in out, out


def test_userprofile_claude_hooks_retired_hook_is_dropped():
    src, home, root = mk(b"{}")
    cmds = [r"python %USERPROFILE%\.claude\hooks\af_cli.py", "python ~/.claude/hooks/af_cli.py",
            r"python C:\USERS\U\.CLAUDE\HOOKS\af_cli.py"]
    old = {"Stop": [{"hooks": [{"type": "command", "command": c}]} for c in cmds]}
    (home / "settings.json").write_bytes(old_text(old))
    rc, out, _ = install(home, src, root)
    assert rc == 0, out
    assert "Stop" not in hooks_of(home), hooks_of(home)
    assert "0 foreign hooks kept" in out and not kept_lines(out), out
    assert "no longer called" in out and "af_cli.py" in out, out


def test_kept_foreign_hook_never_listed_as_no_longer_called():
    src, home, root = mk(b"{}")
    old = {"Stop": [{"hooks": [{"type": "command", "command": "python " + str(home / "hooks" / "retired.py")}]},
                    {"hooks": [{"type": "command", "command": "python D:/operator/tools/mine_only.py"}]}]}
    (home / "settings.json").write_bytes(old_text(old))
    rc, out, _ = install(home, src, root)
    assert rc == 0, out
    line = [ln for ln in out.splitlines() if ln.startswith("no longer called")]
    assert len(line) == 1 and "retired.py" in line[0] and "mine_only.py" not in line[0], out
    assert "mine_only.py" in "".join(kept_lines(out)), out


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
