"""S23 acceptance tests (neutral name; final home tests/test_s23_redirect_and_status.py).

A. go_gate.bash_needs_go: a write-redirect target ends at a shell separator (`;`, `&`, `|`, whitespace), so a harmless redirect
   to a throwaway file followed by `; echo hi` / ` && echo hi` / ` | cat` / `; wc -l f` / `2>&1` / `cd tmp && cmd > f; next`
   needs NO GO. Every unsafe twin (git repo, "..", $HOME, "*", relative path, quoted ";" in the path, sensitive folder or
   receipts file, heredoc) still ASKS.
B. tools/plan_ledger.parse: BLOCKED_PROHIBITED is a known status -> state todo + `blocked` field; a qualified form parses;
   an invented status still raises ValueError("unknown status").
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
_DEFAULT_ROOT = Path(r"D:\tmp\scratch_worktrees\hv2-s23")
ROOT = Path(os.environ.get("HV2_ROOT") or _HERE.parent)
if not (ROOT / "harness" / "hooks").is_dir():
    ROOT = _DEFAULT_ROOT
HOOKS = ROOT / "harness" / "hooks"
sys.path.insert(0, str(HOOKS))
sys.path.insert(0, str(ROOT / "tools"))
_spec = importlib.util.spec_from_file_location("go_gate_under_test", HOOKS / "go_gate.py")
gg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gg)
import plan_ledger  # noqa: E402

TMPROOT = Path(tempfile.mkdtemp(prefix="s23t_")).resolve()
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


def needs(cmd, cwd=None):
    return gg.bash_needs_go(cmd, str(cwd or TMPROOT), is_powershell=False)


def assert_free(cmd, cwd=None):
    r = needs(cmd, cwd)
    assert r[0] is False, f"expected NO GO, got {r} for {cmd!r}"


def assert_go(cmd, cwd=None):
    r = needs(cmd, cwd)
    assert r[0] is True, f"expected GO required, got {r} for {cmd!r}"


# ------------------------------------------------------------------------------------------------ A
def test_a_redirect_then_semicolon_is_free_and_same_in_git_repo_asks():
    f = fresh() / "o.txt"
    assert_free(f"git diff > {f.as_posix()}; echo hi")
    assert_free(f"echo x > {f.as_posix()};echo hi")  # glued with no space after
    g = git_repo() / "o.txt"
    assert_go(f"git diff > {g.as_posix()}; echo hi")


def test_a_redirect_then_and_and_is_free_and_git_twin_asks():
    f = fresh() / "o.txt"
    assert_free(f"echo x > {f.as_posix()} && echo hi")
    assert_free(f"echo x > {f.as_posix()}&& echo hi")
    g = git_repo() / "o.txt"
    assert_go(f"echo x > {g.as_posix()} && echo hi")


def test_a_redirect_then_pipe_is_free_and_git_twin_asks():
    f = fresh() / "o.txt"
    assert_free(f"echo x > {f.as_posix()} | cat")
    g = git_repo() / "o.txt"
    assert_go(f"echo x > {g.as_posix()} | cat")


def test_a_redirect_then_wc_of_same_file_is_free_and_git_twin_asks():
    f = fresh() / "o.txt"
    assert_free(f"echo x > {f.as_posix()}; wc -l {f.as_posix()}")
    g = git_repo() / "o.txt"
    assert_go(f"echo x > {g.as_posix()}; wc -l {g.as_posix()}")


def test_a_redirect_with_trailing_stderr_merge_is_free_and_git_twin_asks():
    f = fresh() / "o.txt"
    assert_free(f"echo x > {f.as_posix()} 2>&1")
    assert_free(f"echo x > {f.as_posix()} 2>&1; echo hi")
    g = git_repo() / "o.txt"
    assert_go(f"echo x > {g.as_posix()} 2>&1")


def test_a_cd_to_tempdir_then_redirect_then_next_is_free_and_git_twin_asks():
    d = fresh()
    f = d / "o.txt"
    assert_free(f"cd {d.as_posix()} && echo x > {f.as_posix()}; echo next")
    repo = git_repo()
    assert_go(f"cd {repo.as_posix()} && echo x > {(repo / 'o.txt').as_posix()}; echo next")


def test_a_dotdot_variable_wildcard_and_relative_targets_still_ask():
    d = fresh()
    (d / "sub").mkdir()
    assert_free(f"echo x > {d.as_posix()}/o.txt; echo hi")  # twin
    assert_go(f"echo x > {d.as_posix()}/sub/../o.txt; echo hi")
    assert_go("echo x > $HOME/o.txt; echo hi")
    assert_go(f"echo x > {d.as_posix()}/*.txt; echo hi")
    assert_go("echo x > a.txt; echo next", cwd=d)
    assert_go("echo x > a.txt && echo next", cwd=d)


def test_a_quoted_target_with_semicolon_inside_still_asks():
    d = fresh()
    assert_free(f'echo x > "{d.as_posix()}/ok.txt"; echo hi')  # twin: quoted, no ";" inside
    assert_go(f'echo x > "{d.as_posix()}/a;b.txt"; echo hi')
    assert_go(f"echo x > '{d.as_posix()}/a;b.txt'; echo hi")


def test_a_sensitive_targets_glued_to_semicolon_still_ask():
    d = fresh()
    assert_free(f"echo x > {d.as_posix()}/plain.txt; echo hi")  # twin
    (d / "claude_x").mkdir()
    assert_go(f"echo x > {d.as_posix()}/claude_x/a.txt; echo hi")
    assert_go(f"echo x > {d.as_posix()}/receipts.jsonl; echo hi")
    assert_go(f"echo x > {d.as_posix()}/claude_x/a.txt;echo hi")


def test_a_heredoc_still_asks():
    f = fresh() / "o.txt"
    assert_go(f"cat <<EOF > {f.as_posix()}; echo hi\nline\nEOF")
    assert_go(f"cat > {f.as_posix()} <<EOF\nline\nEOF")


def test_a_multiple_redirects_one_unsafe_asks():
    d = fresh()
    f = d / "o.txt"
    g = git_repo() / "p.txt"
    assert_free(f"echo x > {f.as_posix()}; echo y > {(d / 'q.txt').as_posix()}; echo hi")  # twin
    assert_go(f"echo x > {f.as_posix()}; echo y > {g.as_posix()}; echo hi")


def test_a_noclobber_override_after_harmless_redirect_asks():
    d = fresh()
    t = d / "t.txt"
    g = git_repo() / "real.txt"
    assert_free(f"echo x > {t.as_posix()}; echo y > {(d / 'u.txt').as_posix()}")  # twin: plain `>` second
    assert_go(f"echo x > {t.as_posix()}; echo y >| {g.as_posix()}")
    assert_go(f"echo x > {t.as_posix()}; echo y >|{g.as_posix()}")  # glued
    assert_go(f"echo x >| {t.as_posix()}")  # operator form has no readable target: conservative ask
    assert_go(f"echo x >| {g.as_posix()}")


def test_a_chained_redirects_second_in_repo_asks_and_ampersand_forms():
    d = fresh()
    t = d / "t.txt"
    g = git_repo() / "real.txt"
    assert_go(f"echo x > {t.as_posix()} >> {g.as_posix()}")
    assert_go(f"echo x > {t.as_posix()}; echo y > {g.as_posix()}")
    assert_free(f"echo x > {t.as_posix()} >> {(d / 'u.txt').as_posix()}")  # twin
    assert_free(f"echo x &> {t.as_posix()}; echo y")
    assert_free(f"echo x > {t.as_posix()} 2>&1")
    assert_go(f"echo x &> {t.as_posix()}; echo y &> {g.as_posix()}")  # twin of the &> form


def test_a_several_harmless_redirects_need_no_go():
    d = fresh()
    t1, t2, t3 = (d / n for n in ("t1.txt", "t2.txt", "t3.txt"))
    assert_free(f"echo z > {t1.as_posix()}; echo a > {t2.as_posix()}; echo b >> {t3.as_posix()}")
    g = git_repo() / "real.txt"
    assert_go(f"echo z > {t1.as_posix()}; echo a > {t2.as_posix()}; echo b >> {g.as_posix()}")


# ------------------------------------------------------------------------------------------------ B
def _plan(status: str) -> str:
    return ("## Sprint S23 - Test\nGoal: g\n\n| Task | Title | Status | Owner |\n|---|---|---|---|\n"
            f"| S23-T1 | One | {status} | the operator |\n")


def test_b_blocked_prohibited_parses_to_todo_with_blocked_field():
    t = plan_ledger.parse(_plan("BLOCKED_PROHIBITED"))["sprints"][0]["tasks"][0]
    assert t["state"] == "todo"
    assert t["blocked"]["class"] == "BLOCKED_PROHIBITED"


def test_b_blocked_prohibited_with_qualifier_parses():
    t = plan_ledger.parse(_plan("BLOCKED_PROHIBITED; operator rule 4"))["sprints"][0]["tasks"][0]
    assert t["state"] == "todo"
    assert t["blocked"]["class"] == "BLOCKED_PROHIBITED"


def test_b_twin_not_started_has_no_blocked_field_and_unknown_status_raises():
    t = plan_ledger.parse(_plan("NOT_STARTED"))["sprints"][0]["tasks"][0]
    assert t["state"] == "todo" and "blocked" not in t
    try:
        plan_ledger.parse(_plan("TOTALLY_UNKNOWN"))
    except ValueError as exc:
        assert "unknown status" in str(exc), str(exc)
    else:
        raise AssertionError("TOTALLY_UNKNOWN must raise ValueError")


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
