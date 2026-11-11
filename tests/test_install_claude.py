"""Tests for tools/install_claude.py. Expected values come from the module docstring / task criteria.
Only temp dirs are used; the real ~/.claude is never touched."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "install_claude", Path(__file__).resolve().parent.parent / "tools" / "install_claude.py")
ic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ic)

NEW_HOOKS = {"PreToolUse": [{"matcher": "Bash", "hooks": [
    {"type": "command", "command": "python", "args": ["C:/h/hooks/keep.py"]},
    {"type": "command", "command": "python C:/h/hooks/fresh.py"}]}]}
OLD_HOOKS = {"PreToolUse": [{"hooks": [
    {"type": "command", "command": "python C:/old/hooks/keep.py"},
    {"type": "command", "command": "python C:/old/hooks/af_cli.py"}]}]}
OLD_SETTINGS = {"permissions": {"allow": ["Bash(ls)"]}, "env": {"A": "1"}, "model": "x", "hooks": OLD_HOOKS}
_TMPS = []


def env(home_name="home", settings=OLD_SETTINGS, with_home_settings=True):
    root = Path(tempfile.mkdtemp(prefix="icl_"))
    _TMPS.append(root)
    src = root / "src"
    (src / "hooks").mkdir(parents=True)
    (src / "settings.hooks.json").write_text(json.dumps({"hooks": NEW_HOOKS}), encoding="utf-8")
    (src / "hooks" / "keep.py").write_text("NEW keep", encoding="utf-8")
    (src / "hooks" / "fresh.py").write_text("NEW fresh", encoding="utf-8")
    (src / "hooks" / "cfg.json").write_text('{"new": 1}', encoding="utf-8")
    home = root / home_name
    (home / "hooks").mkdir(parents=True)
    (home / "hooks" / "keep.py").write_text("OLD keep", encoding="utf-8")
    (home / "hooks" / "af_cli.py").write_text("OLD af", encoding="utf-8")
    (home / "hooks" / "only_home.py").write_text("HOME ONLY", encoding="utf-8")
    if with_home_settings:
        (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    return root, src, home, root / "snap"


def run(home, snap, src):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = ic.install(home, snap, src)
    return rc, buf.getvalue()


def tree(p):
    return {str(f.relative_to(p)): f.read_bytes() for f in sorted(p.rglob("*")) if f.is_file()}


def t_happy():
    _, src, home, snap = env()
    rc, _ = run(home, snap, src)
    s = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert rc == 0
    assert s["hooks"] == NEW_HOOKS, s["hooks"]
    for k in ("permissions", "env", "model"):
        assert s[k] == OLD_SETTINGS[k], k
    assert set(s) == {"permissions", "env", "model", "hooks"}


def t_snapshot():
    _, src, home, snap = env(home_name="o'brien")
    assert run(home, snap, src)[0] == 0
    assert json.loads((snap / "settings.json").read_text(encoding="utf-8")) == OLD_SETTINGS
    assert (snap / "hooks" / "keep.py").read_text() == "OLD keep"
    ps = snap / "restore.ps1"
    raw = ps.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "no BOM"
    text = raw.decode("utf-8-sig")
    assert "o''brien" in text, text
    assert "o'brien" not in text.replace("o''brien", "")


def t_copy():
    _, src, home, snap = env()
    assert run(home, snap, src)[0] == 0
    assert (home / "hooks" / "keep.py").read_text() == "NEW keep"
    assert (home / "hooks" / "fresh.py").read_text() == "NEW fresh"
    assert (home / "hooks" / "cfg.json").read_text() == '{"new": 1}'
    assert (home / "hooks" / "only_home.py").read_text() == "HOME ONLY"
    assert (home / "hooks" / "af_cli.py").read_text() == "OLD af"


def t_rerun():
    root, src, home, snap = env()
    assert run(home, snap, src)[0] == 0
    before = tree(root)
    rc, out = run(home, snap, src)
    assert rc == 1, out
    assert tree(root) == before


def _unchanged_fail(root, src, home, snap):
    before = tree(home)
    rc, out = run(home, snap, src)
    assert rc == 1, out
    assert not snap.exists(), "snapshot created"
    assert tree(home) == before, "home changed"


def t_invalid_json():
    root, src, home, snap = env()
    (home / "settings.json").write_text("{not json", encoding="utf-8")
    _unchanged_fail(root, src, home, snap)


def t_hooks_list():
    root, src, home, snap = env(settings={"hooks": [], "env": {}})
    _unchanged_fail(root, src, home, snap)


def t_built_no_hooks():
    root, src, home, snap = env()
    (src / "settings.hooks.json").write_text(json.dumps({"model": "x"}), encoding="utf-8")
    _unchanged_fail(root, src, home, snap)
    (src / "settings.hooks.json").write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    _unchanged_fail(root, src, home, snap)


def t_no_py():
    root, src, home, snap = env()
    for f in (src / "hooks").glob("*.py"):
        f.unlink()
    _unchanged_fail(root, src, home, snap)


def t_missing_hook_file():
    root, src, home, snap = env()
    cfg = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python ghost_hook.py"}]}]}}
    (src / "settings.hooks.json").write_text(json.dumps(cfg), encoding="utf-8")
    before = tree(home)
    rc, out = run(home, snap, src)
    assert rc == 1 and "ghost_hook.py" in out, out
    assert not snap.exists() and tree(home) == before
    # a file present only in the home satisfies the check
    cfg["hooks"]["Stop"][0]["hooks"][0]["command"] = "python only_home.py"
    (src / "settings.hooks.json").write_text(json.dumps(cfg), encoding="utf-8")
    assert run(home, snap, src)[0] == 0


def t_no_home_settings():
    _, src, home, snap = env(with_home_settings=False)
    rc, out = run(home, snap, src)
    assert rc == 0, out
    s = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert s == {"hooks": NEW_HOOKS}


def t_retired():
    _, src, home, snap = env()
    rc, out = run(home, snap, src)
    assert rc == 0
    line = [l for l in out.splitlines() if "af_cli.py" in l]
    assert line, out
    assert "keep.py" not in line[0] and "fresh.py" not in line[0], line[0]


def t_readonly():
    _, src, home, snap = env()
    target = home / "hooks" / "keep.py"
    os.chmod(target, stat.S_IREAD)
    try:
        rc, out = run(home, snap, src)
        assert rc == 0, out
        assert target.read_text() == "NEW keep"
    finally:
        os.chmod(target, stat.S_IWRITE | stat.S_IREAD)


def t_no_tmp():
    _, src, home, snap = env()
    assert run(home, snap, src)[0] == 0
    assert not (home / "settings.json.tmp").exists()


def main():
    fails = 0
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("t_") and callable(f)):
        try:
            fn()
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
