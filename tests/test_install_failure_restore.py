"""Failure-injection tests for tools/install_claude.py, tools/install_codex.py and tools/probe_live_hooks.py.

Seam: env HARNESS_INSTALL_FAIL_AFTER=<n> (digits) makes install() raise OSError after the n-th write step
(one copied file each; the written temp file settings.json.tmp / hooks.json.tmp is the last step).
Installers run as real subprocesses (CLI boundary, no module-global state leaks between cases).
Fake source: Claude 3 hook files -> steps 1..3, tmp = step 4. Codex 5 files (AGENTS.md, agent, skill, prompt, hook)
-> steps 1..5, tmp = step 6. Temp dirs only; the real ~/.claude and ~/.codex are never touched.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
PS = shutil.which("powershell")
_TMPS = []
SKIPS = []


def tmp():
    d = Path(tempfile.mkdtemp(prefix="ifr_"))
    _TMPS.append(d)
    return d


def tree(p):
    return {str(f.relative_to(p)): f.read_bytes() for f in sorted(p.rglob("*"))
            if f.is_file() and "replaced-by-restore" not in str(f.relative_to(p))}


def aside(p):
    return [str(f.relative_to(p)) for f in sorted(p.rglob("*")) if "replaced-by-restore" in f.name]


def run_cli(script, home, snap, src, fail_after=None):
    env = {k: v for k, v in os.environ.items() if k != "HARNESS_INSTALL_FAIL_AFTER"}
    if fail_after is not None:
        env["HARNESS_INSTALL_FAIL_AFTER"] = fail_after
    r = subprocess.run([sys.executable, "-I", str(TOOLS / script), "--home", str(home), "--snapshot", str(snap),
                        "--source", str(src)], capture_output=True, env=env, timeout=120)
    return r.returncode, r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")


def restore(snap):
    r = subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(snap / "restore.ps1")],
                       capture_output=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr


# ---------------- Claude fixture ----------------
C_NEW = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
    {"type": "command", "command": "python", "args": ["C:/h/hooks/keep.py"]},
    {"type": "command", "command": "python C:/h/hooks/fresh.py"}]}]}}
C_OLD = {"permissions": {"allow": ["Bash(ls)"]}, "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python x.py"}]}]}}


def claude_env():
    root = tmp()
    src, home = root / "src", root / "home"
    (src / "hooks").mkdir(parents=True)
    (src / "settings.hooks.json").write_text(json.dumps(C_NEW), encoding="utf-8")
    for n, c in (("keep.py", "NEW keep"), ("fresh.py", "NEW fresh"), ("cfg.json", '{"n": 1}')):
        (src / "hooks" / n).write_text(c, encoding="utf-8")
    (home / "hooks").mkdir(parents=True)
    (home / "hooks" / "keep.py").write_text("OLD keep", encoding="utf-8")
    (home / "settings.json").write_text(json.dumps(C_OLD), encoding="utf-8")
    return src, home, root / "snap"


def claude_fail(n):
    src, home, snap = claude_env()
    before = tree(home)
    settings_before = (home / "settings.json").read_bytes()
    rc, out = run_cli("install_claude.py", home, snap, src, str(n))
    assert rc == 1, out
    assert "install FAILED half-way" in out, out
    assert str(snap / "restore.ps1") in out, out
    assert not (home / "settings.json.tmp").exists(), "tmp file left behind"
    assert (home / "settings.json").read_bytes() == settings_before, "settings.json changed"
    return src, home, snap, before


def t_claude_fail_after_step1():
    claude_fail(1)


def t_claude_fail_before_replace():
    claude_fail(4)


def _claude_restore(n):
    if not PS:
        SKIPS.append(f"claude restore n={n}")
        print("SKIP claude restore: powershell not available")
        return
    src, home, snap, before = claude_fail(n)
    restore(snap)
    assert tree(home) == before, "home differs from the pre-install state after restore"
    if n >= 3:  # fresh.py and cfg.json were copied in, so restore must move them aside
        a = aside(home)
        assert any(x.startswith("hooks") and "fresh.py.replaced-by-restore-" in x for x in a), a
        assert not (home / "hooks" / "fresh.py").exists()


def t_claude_restore_after_step1():
    _claude_restore(1)


def t_claude_restore_after_tmp_step():
    _claude_restore(4)


def t_claude_n_too_large_succeeds():
    src, home, snap = claude_env()
    rc, out = run_cli("install_claude.py", home, snap, src, "99")
    assert rc == 0, out
    assert json.loads((home / "settings.json").read_text(encoding="utf-8"))["hooks"] == C_NEW["hooks"]
    assert not (home / "settings.json.tmp").exists()


def t_claude_non_numeric_succeeds():
    for v in ("abc", "-1", "1x"):
        src, home, snap = claude_env()
        rc, out = run_cli("install_claude.py", home, snap, src, v)
        assert rc == 0, (v, out)
        assert (home / "hooks" / "fresh.py").read_text() == "NEW fresh"


# ---------------- Codex fixture ----------------
X_NEW = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "py C:/h/hooks/h.py"}]}]}}
X_OLD = {"custom": {"k": 1}, "hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": "py end.py"}]}]}}


def codex_env():
    root = tmp()
    src, home = root / "src", root / "home"
    for rel, c in (("AGENTS.md", "NEW agents"), ("agents/a.toml", "new a"), ("skills/s/SKILL.md", "new skill"),
                   ("prompts/p.md", "new prompt"), ("hooks/h.py", "new hook")):
        f = src / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(c, encoding="utf-8")
    (src / "hooks.json").write_text(json.dumps(X_NEW), encoding="utf-8")
    for rel, c in (("config.toml", "[features]\nhooks = true\n"), ("AGENTS.md", "OLD agents"),
                   ("agents/old.toml", "old agent"), ("skills/mine/SKILL.md", "mine"), ("hooks/old.py", "old hook"),
                   ("hooks.json", json.dumps(X_OLD))):
        f = home / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(c, encoding="utf-8")
    return src, home, root / "snap"


def codex_fail(n):
    src, home, snap = codex_env()
    before = tree(home)
    rc, out = run_cli("install_codex.py", home, snap, src, str(n))
    assert rc == 1, out
    assert "install FAILED half-way" in out, out
    assert str(snap / "restore.ps1") in out, out
    assert "config.toml was not touched" in out, out
    assert not (home / "hooks.json.tmp").exists(), "tmp file left behind"
    assert (home / "hooks.json").read_bytes() == before["hooks.json"], "hooks.json changed"
    assert (home / "config.toml").read_bytes() == before["config.toml"]
    return src, home, snap, before


def t_codex_fail_after_step1():
    codex_fail(1)


def t_codex_fail_before_replace():
    src, home, snap, before = codex_fail(6)
    assert (home / "hooks" / "h.py").read_text() == "new hook", "all copy steps should have run before the failure"


def _codex_restore(n):
    if not PS:
        SKIPS.append(f"codex restore n={n}")
        print("SKIP codex restore: powershell not available")
        return
    src, home, snap, before = codex_fail(n)
    restore(snap)
    after = tree(home)
    assert after == before, sorted(set(after) ^ set(before))
    if n == 6:  # items were replaced, so restore keeps the replaced copies aside
        a = aside(home)
        assert any(x.startswith("hooks.replaced-by-restore-") for x in a) or \
            any("hooks.replaced-by-restore-" in x for x in a), a
        assert any(p.name.startswith("hooks.replaced-by-restore-") for p in home.iterdir())


def t_codex_restore_after_step1():
    _codex_restore(1)


def t_codex_restore_after_tmp_step():
    _codex_restore(6)


def t_codex_n_too_large_succeeds():
    src, home, snap = codex_env()
    rc, out = run_cli("install_codex.py", home, snap, src, "99")
    assert rc == 0, out
    assert (home / "hooks" / "h.py").read_text() == "new hook"
    m = json.loads((home / "hooks.json").read_text(encoding="utf-8"))
    assert "SessionEnd" in m["hooks"] and "Stop" in m["hooks"], m
    assert not (home / "hooks.json.tmp").exists()


def t_codex_non_numeric_succeeds():
    for v in ("abc", "-1", "2.5"):
        src, home, snap = codex_env()
        rc, out = run_cli("install_codex.py", home, snap, src, v)
        assert rc == 0, (v, out)
        assert (home / "AGENTS.md").read_text() == "NEW agents"


def t_unset_env_succeeds_both():
    src, home, snap = claude_env()
    assert run_cli("install_claude.py", home, snap, src)[0] == 0
    src, home, snap = codex_env()
    assert run_cli("install_codex.py", home, snap, src)[0] == 0


# ---------------- probe ----------------
_spec = importlib.util.spec_from_file_location("probe_live_hooks", TOOLS / "probe_live_hooks.py")
pl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pl)
EXE = sys.executable.replace("\\", "/")


def probe_case(hook, exit_code, subdir=""):
    d = tmp() / subdir if subdir else tmp()
    d.mkdir(parents=True, exist_ok=True)
    gate = d / "shell_policy_gate.py"
    gate.write_text(f"import sys\nsys.stdin.read()\nsys.exit({exit_code})\n", encoding="utf-8")
    settings = d / "settings.json"
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [hook(gate)]}]}}),
                        encoding="utf-8")
    return pl.probe(settings, "shell_policy_gate.py", 30)


def t_probe_one_string_command_denies():
    ok, msg = probe_case(lambda g: {"type": "command", "command": f"{EXE} {g}"}, 2)
    assert ok, msg


def t_probe_one_string_quoted_path_with_space():
    ok, msg = probe_case(lambda g: {"type": "command", "command": f'{EXE} "{g}"'}, 2, subdir="dir with space")
    assert ok, msg


def t_probe_one_string_allow_is_failure():
    ok, msg = probe_case(lambda g: {"type": "command", "command": f"{EXE} {g}"}, 0)
    assert not ok and "did not deny" in msg, msg


def t_probe_command_args_form():
    ok, msg = probe_case(lambda g: {"type": "command", "command": sys.executable, "args": [str(g)]}, 2)
    assert ok, msg
    ok, msg = probe_case(lambda g: {"type": "command", "command": sys.executable, "args": [str(g)]}, 0)
    assert not ok, msg


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
