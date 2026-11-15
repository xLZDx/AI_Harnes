"""S24 acceptance tests, part 1 (neutral name; final home tests/test_s24_fd_redirect.py).

go_gate.bash_needs_go: a write redirect with a file-descriptor prefix (`2> f`, `1>> f`, `2>f`) is judged exactly like a plain `>`:
a throwaway target outside any git repo needs NO GO, a target in a git repo / relative / `>|` ASKS. fd duplication (`2>&1`, `>&2`, `2>&-`)
and the null devices (/dev/null, NUL, `$null` in PowerShell) need no GO. Heredoc still asks.
"""
from __future__ import annotations

import atexit
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DEFAULT_ROOT = Path(r"D:\tmp\scratch_worktrees\hv2-s24")
ROOT = Path(os.environ.get("HV2_ROOT") or _HERE.parent)
if not (ROOT / "harness" / "hooks").is_dir():
    ROOT = _DEFAULT_ROOT
HOOKS = ROOT / "harness" / "hooks"
sys.path.insert(0, str(HOOKS))
sys.path.insert(0, str(ROOT / "tools"))
_spec = importlib.util.spec_from_file_location("go_gate_under_test", HOOKS / "go_gate.py")
gg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gg)

TMPROOT = Path(tempfile.mkdtemp(prefix="s24t_")).resolve()
atexit.register(lambda: shutil.rmtree(TMPROOT, ignore_errors=True))
_counter = [0]


def fresh(name="d") -> Path:
    _counter[0] += 1
    p = TMPROOT / f"{name}{_counter[0]}"
    p.mkdir(parents=True)
    return p


def git_repo() -> Path:
    p = fresh("repo")
    cp = subprocess.run(["git", "init", "-q", str(p)], capture_output=True, text=True)
    assert cp.returncode == 0, cp.stderr
    return p


def needs(cmd, cwd=None, ps=False):
    return gg.bash_needs_go(cmd, str(cwd or TMPROOT), is_powershell=ps)


def assert_free(cmd, cwd=None, ps=False):
    r = needs(cmd, cwd, ps)
    assert r[0] is False, f"expected NO GO, got {r} for {cmd!r}"


def assert_go(cmd, cwd=None, ps=False):
    r = needs(cmd, cwd, ps)
    assert r[0] is True, f"expected GO required, got {r} for {cmd!r}"


def test_stderr_to_throwaway_is_free_and_repo_twin_asks():
    f = fresh() / "e.txt"
    g = git_repo() / "e.txt"
    assert_free(f"cmd 2> {f.as_posix()}")
    assert_free(f"cmd 2>{f.as_posix()}")
    assert_go(f"cmd 2> {g.as_posix()}")
    assert_go(f"cmd 2>{g.as_posix()}")


def test_stdout_append_with_fd_one_throwaway_free_repo_asks():
    f = fresh() / "o.txt"
    g = git_repo() / "o.txt"
    assert_free(f"cmd 1>> {f.as_posix()}")
    assert_free(f"cmd 1> {f.as_posix()}")
    assert_go(f"cmd 1>> {g.as_posix()}")
    assert_go(f"cmd 1> {g.as_posix()}")


def test_fd_redirect_relative_path_asks():
    d = fresh()
    assert_go("cmd 2>rel.txt", cwd=d)
    assert_go("cmd 2> rel.txt", cwd=d)
    assert_go("cmd 1>>rel.txt", cwd=d)


def test_harmless_redirect_then_repo_fd_redirect_asks():
    f = fresh() / "t.txt"
    g = git_repo() / "real.txt"
    assert_free(f"echo x > {f.as_posix()}; cmd 2>{(f.parent / 'u.txt').as_posix()}")  # twin
    assert_go(f"echo x > {f.as_posix()}; cmd 2>{g.as_posix()}")
    assert_go(f"echo x > {f.as_posix()}; cmd 2> {g.as_posix()}")


def test_fd_duplication_needs_no_go():
    assert_free("cmd 2>&1")
    assert_free("pytest 2>&1 | tail -3")
    assert_free("cmd >&2")
    assert_free("cmd 2>&-")
    assert_free("cmd 1>&2")


def test_bash_mode_only_dev_null_is_free():
    assert_go("cmd > NUL", cwd=fresh())
    assert_go("cmd > nul", cwd=fresh())
    assert_go("cmd > $null", cwd=fresh())
    assert_go("cmd 2> $null", cwd=fresh())


def test_powershell_mode_nul_and_null_are_free():
    assert_free("cmd > NUL", ps=True)
    assert_free("cmd > nul", ps=True)
    assert_free("cmd 2> $null", ps=True)
    assert_free("cmd > /dev/null", ps=True)


def test_null_devices_need_no_go():
    assert_free("cmd > /dev/null")
    assert_free("cmd 2>/dev/null")
    assert_free("cmd 2> /dev/null")
    assert_free("cmd > /dev/null 2>&1")
    assert_free("cmd &> /dev/null")
    assert_free("cmd > /dev/null 2>/dev/null")


def test_null_device_does_not_launder_a_repo_target():
    g = git_repo() / "real.txt"
    assert_free(f"cmd > /dev/null >> {fresh().as_posix()}/ok.txt")  # twin: both harmless
    assert_go(f"cmd > /dev/null >> {g.as_posix()}")
    assert_go(f"cmd 2>/dev/null > {g.as_posix()}")
    assert_go(f"cmd &> /dev/null; echo y 2> {g.as_posix()}")


def test_powershell_null_and_repo_target():
    g = git_repo() / "real.txt"
    f = fresh() / "o.txt"
    assert_free("cmd 2> $null", ps=True)
    assert_free("cmd > $null", ps=True)
    assert_go(f"cmd > {g.as_posix()}", ps=True)
    assert_go(f"cmd 2> {g.as_posix()}", ps=True)
    assert_free(f"cmd > {f.as_posix()}", ps=True)  # twin: throwaway outside git


def test_heredoc_still_asks():
    f = fresh() / "o.txt"
    assert_go(f"cat <<EOF > {f.as_posix()}\nline\nEOF")
    assert_go(f"cat 2> {f.as_posix()} <<EOF\nline\nEOF")


def test_noclobber_after_harmless_redirect_asks():
    d = fresh()
    t = d / "t.txt"
    g = git_repo() / "real.txt"
    assert_free(f"echo x > {t.as_posix()}; echo y > {(d / 'u.txt').as_posix()}")  # twin
    assert_go(f"echo x > {t.as_posix()}; echo y >| {g.as_posix()}")
    assert_go(f"echo x > {t.as_posix()}; echo y >|{g.as_posix()}")


def run_all(g) -> int:
    fails = 0
    for name in [n for n in g if n.startswith("test_")]:
        try:
            g[name]()
            print(f"PASS {name}")
        except BaseException:
            fails += 1
            print(f"FAIL {name}")
            traceback.print_exc(limit=4, file=sys.stdout)
    print(f"{'FAILED' if fails else 'OK'}: {fails} failing")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run_all(globals()))
