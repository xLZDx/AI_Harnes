"""S24 acceptance tests, part 2 (neutral name; final home tests/test_s24_autosync.py).

The installed pre-commit hook (tools/githooks/pre-commit) keeps .harness/ledger.json in step with PROJECT_PLAN.md through ledger_sync.py:
runs in the main checkout only, only when the plan exists and is newer than the ledger, never blocks a commit, logs failures to
.harness/hook-error.log, and never shrinks the ledger. Real git in temp repositories, real --install-project.
Note: ledger_sync writes its safety backup to D:\\Backup (module constant, not overridable from a hook subprocess).
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DEFAULT_ROOT = Path(r"D:\tmp\scratch_worktrees\hv2-s24")
ROOT = Path(os.environ.get("HV2_ROOT") or _HERE.parent)
if not (ROOT / "tools" / "build.py").is_file():
    ROOT = _DEFAULT_ROOT
PY = r"C:\Python314\python.exe"

TMPROOT = Path(tempfile.mkdtemp(prefix="s24a_")).resolve()
atexit.register(lambda: shutil.rmtree(TMPROOT, ignore_errors=True))
_counter = [0]
IDENT = ["-c", "user.name=t", "-c", "user.email=t@t"]


EXTRA_ENV = {}


def _env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARNESS_")}
    env.update(EXTRA_ENV)
    env["HARNESS_PYTHON"] = PY
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run(cmd, cwd):
    return subprocess.run(cmd, cwd=str(cwd), env=_env(), capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=300)


def git(cwd, *args):
    return run(["git", *IDENT, *args], cwd)


def fresh(name="r") -> Path:
    _counter[0] += 1
    p = TMPROOT / f"{name}{_counter[0]}"
    p.mkdir(parents=True)
    return p


def plan_text(n_tasks, sprints=2, bad_status=None) -> str:
    out = ["---", "project: demo", "---", "# Plan", ""]
    per = [min(3, n_tasks), max(0, n_tasks - 3)]  # S01 always 3 tasks, the rest in S02: ids stay stable as the plan grows
    first = True
    for si, cnt in enumerate(per, 1):
        if cnt == 0:
            continue
        out += [f"## Sprint S{si:02d} - Part {si}", "Goal: g", "", "| Task | Title | Status | Owner |", "|---|---|---|---|"]
        for k in range(1, cnt + 1):
            st = bad_status if (bad_status and first) else "NOT_STARTED"
            first = False
            out.append(f"| S{si:02d}-T{k} | Task {k} | {st} | the operator |")
        out.append("")
    return "\n".join(out) + "\n"


def write(p: Path, text: str):
    p.write_text(text, encoding="utf-8", newline="\n")


_BUILT = [False]


def ensure_built():
    if not _BUILT[0]:
        cp = run([PY, "-s", "tools/build.py"], ROOT)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        _BUILT[0] = True


def mk_project(plan=None) -> Path:
    ensure_built()
    r = fresh("proj")
    assert git(r, "init", "-q", "-b", "main").returncode == 0
    write(r / "README.md", "hello\n")
    assert git(r, "add", "README.md").returncode == 0
    cp = git(r, "commit", "-q", "-m", "init")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    cp = run([PY, "-s", "tools/build.py", "--install-project", str(r), "--force"], ROOT)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    if plan is not None:
        write(r / "PROJECT_PLAN.md", plan)
    return r


def commit(r: Path, *paths, msg="c"):
    cp = git(r, "add", *paths)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    cp = git(r, "commit", "-q", "-m", msg)
    assert cp.returncode == 0, "commit failed: " + cp.stdout + cp.stderr
    return cp


def ledger_path(r: Path) -> Path:
    return r / ".harness" / "ledger.json"


def count(r: Path) -> int:
    led = json.loads(ledger_path(r).read_text(encoding="utf-8"))
    return sum(len(s["tasks"]) for s in led["sprints"])


def errlog(r: Path) -> str:
    p = r / ".harness" / "hook-error.log"
    return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""


def sync_errors(r: Path) -> str:
    return "\n".join(ln for ln in errlog(r).splitlines() if "ledger_sync" in ln)


def tick():
    time.sleep(1.2)  # the hook compares file mtimes; keep them apart


# -------------------------------------------------------------------------------------------------
def test_installed_tools_include_sync_modules():
    r = mk_project()
    for f in ("ledger_sync.py", "plan_import_master_md.py", "plan_import_ledger_md.py"):
        assert (r / ".harness-tools" / f).is_file(), f


def test_commit_with_plan_syncs_6_then_9_tasks():
    r = mk_project(plan_text(6))
    commit(r, "PROJECT_PLAN.md", msg="plan 6")
    assert ledger_path(r).is_file(), "ledger missing after commit; errors: " + errlog(r)
    assert count(r) == 6, (count(r), errlog(r))
    led = json.loads(ledger_path(r).read_text(encoding="utf-8"))
    assert len(led["sprints"]) == 2
    tick()
    write(r / "PROJECT_PLAN.md", plan_text(9))
    commit(r, "PROJECT_PLAN.md", msg="plan 9")
    assert count(r) == 9, (count(r), errlog(r))
    assert sync_errors(r) == "", sync_errors(r)


def test_commit_not_touching_plan_runs_no_sync():
    r = mk_project(plan_text(6))
    commit(r, "PROJECT_PLAN.md", msg="plan 6")
    assert count(r) == 6
    tick()
    # plan content changes to 12 tasks but its mtime is forced OLDER than the ledger: the hook must not sync it
    p = r / "PROJECT_PLAN.md"
    write(p, plan_text(12))
    led_stat = ledger_path(r).stat()
    old = led_stat.st_mtime - 100
    os.utime(p, (old, old))
    before = ledger_path(r).read_bytes()
    before_ns = ledger_path(r).stat().st_mtime_ns
    write(r / "other.txt", "x\n")
    commit(r, "other.txt", msg="other")
    assert ledger_path(r).read_bytes() == before
    # mtime is not asserted: another hook step (report.py --stage) may touch the file; unchanged bytes and the count (6, not the
    # plan's 12) prove that ledger_sync did not run
    assert count(r) == 6


def test_stale_ledger_with_newer_plan_does_sync_on_unrelated_commit():
    # twin of the previous case: a plan newer than the ledger is synced even when the commit does not include the plan
    r = mk_project(plan_text(6))
    commit(r, "PROJECT_PLAN.md", msg="plan 6")
    tick()
    write(r / "PROJECT_PLAN.md", plan_text(8))
    write(r / "other.txt", "x\n")
    commit(r, "other.txt", msg="other")
    assert count(r) == 8, (count(r), errlog(r))


def test_project_without_plan_commits_fine_without_sync_error():
    r = mk_project()
    write(r / "a.txt", "a\n")
    commit(r, "a.txt", msg="no plan")
    assert sync_errors(r) == "", sync_errors(r)


def test_unparsable_plan_does_not_block_commit_and_is_logged():
    r = mk_project(plan_text(6))
    commit(r, "PROJECT_PLAN.md", msg="plan 6")
    assert count(r) == 6
    tick()
    write(r / "PROJECT_PLAN.md", plan_text(6, bad_status="TOTALLY_INVENTED"))
    commit(r, "PROJECT_PLAN.md", msg="bad plan")  # commit() asserts rc == 0
    log = errlog(r)
    assert log.strip(), "hook-error.log is empty after a failed sync"
    assert "ledger_sync" in log, log
    assert "error" in log.lower().replace("ledger_sync", "").replace("hook-error", ""), "log line lacks the sync's ERROR text: " + log
    assert "unknown status" in log or "ERROR" in log, log
    assert count(r) == 6, "a failed parse must leave the ledger as it was"


def test_backup_lands_in_harness_backup_dir():
    r = mk_project(plan_text(6))
    commit(r, "PROJECT_PLAN.md", msg="plan 6")  # first write: no old ledger content worth backing up is not guaranteed
    bk = fresh("bk")
    tick()
    EXTRA_ENV["HARNESS_BACKUP_DIR"] = str(bk)
    try:
        write(r / "PROJECT_PLAN.md", plan_text(9))
        commit(r, "PROJECT_PLAN.md", msg="plan 9")
    finally:
        EXTRA_ENV.pop("HARNESS_BACKUP_DIR", None)
    assert count(r) == 9, (count(r), errlog(r))
    files = list(bk.glob("ledger-sync-*.json"))
    assert files, f"no backup in {bk}; log: {errlog(r)}"
    assert json.loads(files[0].read_text(encoding="utf-8"))["sprints"], "backup is not the previous ledger"


def test_commit_from_worktree_does_not_run_sync():
    r = mk_project(plan_text(6))
    commit(r, "PROJECT_PLAN.md", msg="plan 6")
    assert count(r) == 6
    wt = TMPROOT / "wt_checkout"
    cp = git(r, "worktree", "add", "-q", str(wt), "-b", "side")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    tick()
    write(wt / "PROJECT_PLAN.md", plan_text(12))  # newer than the main ledger, bigger
    commit(wt, "PROJECT_PLAN.md", msg="plan 12 in worktree")
    assert count(r) == 6, (count(r), errlog(r))
    assert sync_errors(r) == "", sync_errors(r)
    assert not (wt / ".harness" / "ledger.json").exists()
    # twin: the same edit committed in the main checkout does sync
    tick()
    write(r / "PROJECT_PLAN.md", plan_text(12))
    commit(r, "PROJECT_PLAN.md", msg="plan 12 in main")
    assert count(r) == 12, (count(r), errlog(r))


def test_plan_that_would_shrink_the_ledger_does_not_shrink_it():
    r = mk_project(plan_text(9))
    commit(r, "PROJECT_PLAN.md", msg="plan 9")
    assert count(r) == 9
    tick()
    write(r / "PROJECT_PLAN.md", plan_text(3))
    commit(r, "PROJECT_PLAN.md", msg="plan 3")  # commit still succeeds
    assert count(r) == 9, (count(r), errlog(r))


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
