"""Criterion 6: --install-project and real git hooks (sh scripts run by Git for Windows), contract at 6a6de90."""
import importlib.util
import json
import shutil
import sys
from pathlib import Path

_s = importlib.util.spec_from_file_location("hv2", Path(__file__).resolve().parent / "hv2.py")
h = importlib.util.module_from_spec(_s)
sys.modules["hv2"] = h
_s.loader.exec_module(h)

PYENV = {"HARNESS_PYTHON": h.PY}
BUILD = str(h.TOOLS / "build.py")


def _inst(r, *extra):
    return h.run([h.PY, "-I", BUILD, "--install-project", str(r), *extra], h.SRC)


def _install(name):
    r = h.mk_repo(name)
    res = _inst(r)
    assert res.rc == 0, res.all
    return r


def _itool(r, script, *args, env=None):
    return h.run([h.PY, "-I", str(r / ".harness-tools" / script), *args], r, env)


def _hooks_dir(r):
    return (r / ".harness-tools" / "githooks").as_posix()


def test_install_sets_absolute_hookspath_copies_tools_and_creates_ledger():
    r = _install("in_basic")
    got = h.git(r, "config", "core.hooksPath").out.strip()
    assert got == _hooks_dir(r) and Path(got).is_absolute(), got
    for f in ("ledger.py", "route.py", "report.py", "worktree_gc.py", "harness/routing.json", "harness/manifest.json",
              "githooks/post-commit", "githooks/pre-push", "githooks/post-merge"):
        assert (r / ".harness-tools" / f).is_file(), f
    led = json.loads((r / ".harness" / "ledger.json").read_text(encoding="utf-8"))
    assert led["sprints"] == [] and led["approval"] is None
    d = r / "t.diff"
    d.write_text(h.diff_text(["src/a.py"]), encoding="utf-8")
    res = _itool(r, "route.py", "--diff", str(d))
    assert res.rc == 0 and '"local"' in res.out, res.all
    assert _itool(r, "route.py", "--diff", str(d), "--request", "security-reviewer").rc == 0, "installed router must know the agents"
    assert _itool(r, "route.py", "--diff", str(d), "--request", "bogus-agent").rc != 0


def test_install_is_idempotent_and_keeps_existing_ledger():
    r = _install("in_again")
    assert _itool(r, "ledger.py", "sprint-add", "--id", "S1", "--title", "t").rc == 0
    again = _inst(r)
    assert again.rc == 0, again.all
    assert len(h.load_ledger(r)["sprints"]) == 1, "re-install must not wipe the ledger"


def test_install_refuses_non_git_target():
    p = h.fresh("in_nogit")
    res = _inst(p)
    assert res.rc != 0 and not (p / ".harness-tools").exists()


def test_install_refuses_foreign_hookspath_unless_force():
    r = h.mk_repo("in_foreign")
    assert h.git(r, "config", "core.hooksPath", ".githooks").rc == 0
    res = _inst(r)
    assert res.rc != 0 and "--force" in res.out, res.all
    assert h.git(r, "config", "core.hooksPath").out.strip() == ".githooks"
    assert not (r / ".harness-tools").exists()
    assert _inst(r, "--force").rc == 0
    assert h.git(r, "config", "core.hooksPath").out.strip() == _hooks_dir(r)


def test_install_refuses_when_git_hooks_has_own_hooks_unless_force():
    r = h.mk_repo("in_ownhooks")
    hook = r / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    res = _inst(r)
    assert res.rc != 0 and "--force" in res.out, res.all
    assert h.git(r, "config", "core.hooksPath").rc != 0, "hooksPath must stay unset"
    assert _inst(r, "--force").rc == 0
    assert h.git(r, "config", "core.hooksPath").out.strip() == _hooks_dir(r)


def test_hook_files_have_lf_endings():
    for f in (h.TOOLS / "githooks").iterdir():
        assert b"\r" not in f.read_bytes(), f"{f.name} has CR; sh would fail on Windows checkout"


def _post_commit_flow(env, name):
    r = _install(name)
    assert _itool(r, "ledger.py", "sprint-add", "--id", "S1", "--title", "One").rc == 0
    assert _itool(r, "ledger.py", "task-add", "--title", "first").rc == 0
    assert not (r / ".harness" / "report.html").exists()
    assert not (r / "reports").exists()
    sha = h.commit(r, "src/a.py", "x = 1\n", "Implement first", "Task: S1-T1", env=env)
    t = h.task(r, "S1-T1")
    assert t["state"] == "implemented" and t["commits"] == [sha], t  # post-commit only records the commit on the ledger
    reps = sorted((r / "reports").glob("*_PROGRAM_STATUS_LIVE.ru.html"))
    assert len(reps) == 1, "pre-commit hook did not write the report"
    assert not (r / ".harness" / "report.html").exists(), "old report location must not be used"
    html = reps[0].read_text(encoding="utf-8")
    assert "<!-- harness-report v1 -->" in html[:400] and "S1-T1" in html
    rel = reps[0].relative_to(r).as_posix()
    assert rel in h.git(r, "show", "--name-only", "--format=", sha).out.split(), "report must be staged into the commit itself"
    h.commit(r, "src/b.py", "y = 1\n", "No task here", env=env)  # a commit without a Task line keeps working
    assert h.task(r, "S1-T1")["commits"] == [sha]
    assert not (r / ".harness" / "hook-error.log").exists()


def test_post_commit_hook_updates_ledger_and_report_with_pinned_python():
    _post_commit_flow(PYENV, "in_commit_pinned")


def test_post_commit_hook_updates_ledger_and_report_with_default_python_lookup():
    _post_commit_flow(None, "in_commit_default")


def test_post_commit_hook_unknown_task_is_visible_not_silent():
    r = _install("in_commit_unknown")
    _itool(r, "ledger.py", "sprint-add", "--id", "S1", "--title", "One")
    res = h.git(r, "commit", "-q", "--allow-empty", "-m", "x", "-m", "Task: S5-T5", env=PYENV)
    assert res.rc == 0, res.all
    assert "HARNESS HOOK ERROR" in res.err, res.all
    assert "S5-T5" in (r / ".harness" / "hook-error.log").read_text(encoding="utf-8")
    assert _itool(r, "report.py").rc == 0  # post-commit does not render; the next report run shows the error
    reps = sorted((r / "reports").glob("*_PROGRAM_STATUS_LIVE.ru.html"))
    assert len(reps) == 1 and "Ошибки хуков" in reps[0].read_text(encoding="utf-8")


def test_post_commit_hook_failure_is_logged_and_does_not_fail_the_commit():
    r = _install("in_commit_badpy")
    res = h.git(r, "commit", "-q", "--allow-empty", "-m", "x", env={"HARNESS_PYTHON": str(r / "no-such-python.exe")})
    assert res.rc == 0, res.all
    log = (r / ".harness" / "hook-error.log").read_text(encoding="utf-8")
    assert "post-commit" in log, log
    assert "HARNESS HOOK ERROR" in res.err, res.all


def test_hooks_work_from_a_linked_worktree_and_update_the_main_ledger():
    r = _install("in_linked")
    _itool(r, "ledger.py", "sprint-add", "--id", "S1", "--title", "One")
    _itool(r, "ledger.py", "task-add", "--title", "first")
    wt = h.fresh("in_linked_wt") / "wt"
    assert h.git(r, "worktree", "add", "-q", "-b", "feat", str(wt)).rc == 0
    sha = h.commit(wt, "w.py", "w = 1\n", "from worktree", "Task: S1-T1", env=PYENV)
    assert h.task(r, "S1-T1")["commits"] == [sha] and h.task(r, "S1-T1")["state"] == "implemented"
    assert not (wt / ".harness").exists()


def test_pre_push_refuses_without_approval_then_allows_after_approve():
    r = _install("in_push")
    bare = h.fresh("in_push_remote")
    assert h.git(bare, "init", "-q", "--bare", "-b", "main").rc == 0
    assert h.git(r, "remote", "add", "origin", str(bare)).rc == 0
    blocked = h.git(r, "push", "origin", "main", env=PYENV)
    assert blocked.rc != 0 and "push blocked" in blocked.all, blocked.all
    assert "approved: none" in blocked.all
    assert h.git(bare, "rev-parse", "--verify", "-q", "main").rc != 0, "ref reached remote despite block"
    assert _itool(r, "ledger.py", "approve", env=PYENV).rc != 0, "approve needs --evidence"
    assert _itool(r, "ledger.py", "approve", "--evidence", "reply 1", env=PYENV).rc == 0
    ok = h.git(r, "push", "origin", "main", env=PYENV)
    assert ok.rc == 0, ok.all
    head = h.head(r)
    assert h.git(bare, "rev-parse", "main").out.strip() == head
    h.commit(r, "src/n.py", "z = 1\n", "later code", env=PYENV)  # stale for code changes
    again = h.git(r, "push", "origin", "main", env=PYENV)
    assert again.rc != 0, again.all
    assert h.git(bare, "rev-parse", "main").out.strip() == head
    h.git(r, "reset", "-q", "--hard", head)
    h.commit(r, "NOTES.md", "docs\n", "docs only", env=PYENV)  # docs-only descendant of the approved sha
    doc = h.git(r, "push", "origin", "main", env=PYENV)
    assert doc.rc == 0, doc.all
    assert h.git(bare, "rev-parse", "main").out.strip() == h.head(r)


def test_pre_push_blocks_when_any_pushed_ref_is_unapproved():
    r = _install("in_push_multi")
    bare = h.fresh("in_push_multi_remote")
    h.git(bare, "init", "-q", "--bare", "-b", "main")
    h.git(r, "remote", "add", "origin", str(bare))
    assert _itool(r, "ledger.py", "approve", "--evidence", "e", env=PYENV).rc == 0
    h.git(r, "checkout", "-q", "-b", "feature")
    h.commit(r, "src/f.py", "f = 1\n", "feature code", env=PYENV)
    h.git(r, "checkout", "-q", "main")
    res = h.git(r, "push", "origin", "main", "feature", env=PYENV)
    assert res.rc != 0, "feature tip is not covered by the approval"
    assert h.git(bare, "rev-parse", "--verify", "-q", "main").rc != 0, "push must be atomic in effect: nothing reaches the remote"


def test_pre_push_allows_deleting_a_remote_branch():
    r = _install("in_push_delete")
    bare = h.fresh("in_push_delete_remote")
    h.git(bare, "init", "-q", "--bare", "-b", "main")
    h.git(r, "remote", "add", "origin", str(bare))
    assert _itool(r, "ledger.py", "approve", "--evidence", "e", env=PYENV).rc == 0
    assert h.git(r, "push", "origin", "main", "main:old", env=PYENV).rc == 0
    h.commit(r, "src/n.py", "z = 1\n", "unapproved code", env=PYENV)
    res = h.git(r, "push", "origin", ":old", env=PYENV)
    assert res.rc == 0, f"deleting a remote branch must not need approval: {res.all}"


def test_pre_push_skip_env_allows_push_and_records_event():
    r = _install("in_push_skip")
    bare = h.fresh("in_push_skip_remote")
    h.git(bare, "init", "-q", "--bare", "-b", "main")
    h.git(r, "remote", "add", "origin", str(bare))
    ok = h.git(r, "push", "origin", "main", env={**PYENV, "HARNESS_SKIP_APPROVAL": "1"})
    assert ok.rc == 0, ok.all
    assert any(e["kind"] == "approval-skipped" for e in h.load_ledger(r)["events"])


def test_pre_push_refuses_when_tools_are_missing():
    r = _install("in_push_notools")
    bare = h.fresh("in_push_notools_remote")
    h.git(bare, "init", "-q", "--bare", "-b", "main")
    h.git(r, "remote", "add", "origin", str(bare))
    (r / ".harness-tools" / "ledger.py").unlink()
    res = h.git(r, "push", "origin", "main", env=PYENV)
    assert res.rc != 0 and "tools not found" in res.all, res.all
    assert h.git(bare, "rev-parse", "--verify", "-q", "main").rc != 0
    assert "pre-push" in (r / ".harness" / "hook-error.log").read_text(encoding="utf-8")


def test_post_merge_hook_removes_merged_scratch_worktree():
    r = _install("in_merge")
    scratch = h.fresh("in_merge_scratch")
    wt = scratch / "feat"
    assert h.git(r, "worktree", "add", "-q", "-b", "feat", str(wt)).rc == 0
    h.commit(wt, "f.txt", "f\n", "feature work", env=PYENV)
    env = {**PYENV, "HARNESS_SCRATCH_ROOT": str(scratch), "HARNESS_WORKTREE_MIN_AGE_H": "0"}
    res = h.git(r, "merge", "-q", "--ff-only", "feat", env=env)
    assert res.rc == 0, res.all
    assert not wt.exists(), f"post-merge did not clean the merged scratch worktree: {res.all}"
    kept = scratch / "other"
    assert h.git(r, "worktree", "add", "-q", "-b", "other", str(kept)).rc == 0
    h.commit(kept, "o.txt", "o\n", "unmerged", env=PYENV)
    h.git(r, "branch", "side")
    h.commit(r, "m.txt", "m\n", "main moves", env=PYENV)
    assert h.git(r, "merge", "-q", "--no-edit", "side", env=env).rc == 0  # triggers post-merge again
    assert kept.exists(), "unmerged worktree must stay"


if __name__ == "__main__":
    sys.exit(h.run_all(globals()))
