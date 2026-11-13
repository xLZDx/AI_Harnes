"""S18-T4 tests (and backlog B20 coverage): tools/ci_triggers_off.py gives every git call a timeout (GitTimeout), reports
an undeletable journal and undeletable temp files, refreshes the index on a refused rollback, escapes globs in temp-file
names, narrows the parked-copy exemption to untracked files, and keeps the recover() KEPT_NOTE / _retire_kept /
'.cioff-park-*' behaviour covered.
Written from the S18-T4 acceptance criteria. Temp repos only under D:\\tmp\\harness_v2_tests; the tool runs either in-process
on those temp repos or as a subprocess with --root and --scratch pointing at temp folders (never at the real D:\\Repo).
Run: C:\\Python314\\python.exe -I tests\\test_s18_ci_triggers_off.py"""
import contextlib
import io
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools"))
import hv2  # noqa: E402
import ci_triggers_off as ci  # noqa: E402
import workflow_guard as wg  # noqa: E402
from test_s13_ci_off import mk_root, mk_project, rd, commits, _w  # noqa: E402
from test_s14_ci_dirty import (dirty_repo, snapshot, run_tool, no_leftovers, assert_no_ci_off_refs, _crash_at, _crash_helper,  # noqa: E402
                               WF, WF_LONG, DL, SESSION_EDIT, JOURNAL, FILES3, TOOL)

WF_BLOCK_TXT = "name: ci\non:\n  push:\n    branches: [main]\n  pull_request:\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
BRACKET_WF = ".github/workflows/w[1].yml"
DECOY_TMP = ".github/workflows/.w1.yml.cioff-999.tmp"      # temp file of ANOTHER workflow, w1.yml: an unescaped glob matches it
DECOY_PARK = ".github/workflows/.w1.yml.cioff-park-zz"


class _FakeTimedOutProc:
    """Stands in for a git process that never finishes: communicate(timeout) raises TimeoutExpired once, nothing is spawned.
    The pid is odd, so it can never be a real Windows pid (taskkill then reports it as not found)."""
    pid, returncode = 7654321, None

    def __init__(self, cmd):
        self.cmd, self.calls = cmd, 0

    def communicate(self, timeout=None, **kw):
        self.calls += 1
        if self.calls == 1:
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        return b"", b""

    def poll(self):
        return None

    def kill(self):
        pass

    def wait(self, timeout=None):
        return -9


def timeout_on(match):
    """A subprocess.Popen stand-in: really starts everything except git commands for which match(cmd) is true, which hang."""
    real = subprocess.Popen

    def fake(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd and cmd[0] == "git" and match(cmd):
            return _FakeTimedOutProc(cmd)
        return real(cmd, *a, **kw)
    return fake


def spying_popen(seen):
    """A Popen subclass that records (cmd, timeout given to communicate) for every command the tool runs."""
    class Spy(subprocess.Popen):
        def __init__(self, cmd, *a, **kw):
            self._cmd = cmd
            super().__init__(cmd, *a, **kw)

        def communicate(self, input=None, timeout=None):
            seen.append((self._cmd, timeout))
            return super().communicate(input, timeout)
    return Spy


def has(*words):
    return lambda cmd: all(w in cmd for w in words)


def unlink_fails_for(pred, real_unlink=Path.unlink):
    def fake(self, *a, **k):
        if pred(self):
            raise PermissionError(13, "file is locked by another process")
        return real_unlink(self, *a, **k)
    return fake


def is_journal(p):
    return p.name == JOURNAL


def bracket_repo(name="pb"):
    """Dirty repo whose workflow file is named w[1].yml (a glob special character), plus decoys named like w1.yml's leftovers."""
    root = mk_root()
    scratch = hv2.fresh("scratch")
    repo = mk_project(root, name, {BRACKET_WF: WF_LONG, DECOY_TMP: "decoy tmp of w1.yml\n", DECOY_PARK: "decoy park of w1.yml\n"})
    _w(repo / BRACKET_WF, rd(repo / BRACKET_WF).replace(b"      - run: echo tail\n", SESSION_EDIT))
    _w(repo / DL, rd(repo / DL) + b"session note\n")
    return root, scratch, repo


def snap_files(repo, *rels):
    return {r: rd(repo / r) for r in rels}


# ---------------------------------------------------------------- criterion 1: git timeout
def test_gittimeout_is_an_oserror_and_not_a_timeouterror():
    assert issubclass(ci.GitTimeout, OSError)
    assert not issubclass(ci.GitTimeout, TimeoutError)  # process() maps TimeoutError to "lock busy"
    assert isinstance(ci.GIT_TIMEOUT, (int, float)) and ci.GIT_TIMEOUT > 0


def test_a_really_slow_command_raises_gittimeout_after_the_shortened_limit():
    t0 = time.time()
    with mock.patch.object(ci, "GIT_TIMEOUT", 0.5):
        try:
            ci._run([hv2.PY, "-I", "-c", "import time; time.sleep(30)"])
            raise AssertionError("no GitTimeout raised")
        except ci.GitTimeout as e:
            assert "timeout after 0.5s" in str(e), str(e)
            assert "could not be confirmed dead" not in str(e), str(e)  # the slow process (tree) was really stopped
    assert time.time() - t0 < 20, "the limit was not applied"


def test_every_git_call_of_a_dirty_run_carries_the_timeout_including_merge_file():
    root, scratch, repo = dirty_repo("tmo_all")
    seen = []
    with mock.patch.object(ci, "GIT_TIMEOUT", 77), mock.patch.object(ci.subprocess, "Popen", spying_popen(seen)):
        out = ci.process_dirty(repo, False, scratch)
    assert out.startswith("CHANGED"), out
    git_calls = [(c, t) for c, t in seen if c and c[0] == "git"]
    assert len(git_calls) > 10, len(git_calls)
    assert {t for _, t in git_calls} == {77}, {(c[3] if len(c) > 3 else c[1], t) for c, t in git_calls if t != 77}
    assert any("merge-file" in c for c, _ in git_calls), "merge-file must be among the timed calls"
    assert any("cat-file" in c or "show" in c for c, _ in git_calls)  # raw_git (bytes) calls are timed too


def test_clean_run_commit_timeout_is_an_error_row_never_success_and_state_is_restored_and_described():
    root = mk_root()
    repo = mk_project(root, "tmo_clean", {WF: WF_BLOCK_TXT})
    before_wf, before_head, n = rd(repo / WF), hv2.head(repo), commits(repo)
    with mock.patch.object(ci.subprocess, "Popen", timeout_on(has("commit"))):
        row = ci.process(repo, False, False)
    st = row["status"]
    assert st.startswith("ERROR"), row
    assert "timeout after" in st, row
    assert "CHANGED" not in st and "SKIP" not in st, row
    assert "HEAD unchanged, no commit landed" in st and "files restored" in st, st
    assert "files kept as written" not in st, st
    assert rd(repo / WF) == before_wf, "the workflow must be restored when the commit timed out"
    assert hv2.head(repo) == before_head and commits(repo) == n
    assert hv2.git(repo, "status", "--porcelain").out.strip() == "", "the touched paths must be unstaged again"
    assert hv2.git(repo, "diff", "--cached", "--name-only").out.strip() == ""


def test_commit_timeout_after_the_commit_landed_says_so():
    root = mk_root()
    repo = mk_project(root, "tmo_landed", {WF: WF_BLOCK_TXT})
    real_popen = subprocess.Popen

    def landed_then_hang(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd[0] == "git" and "commit" in cmd:
            real_popen(cmd, *a, **kw).communicate()  # the commit really happens, but the tool is told it hung
            return _FakeTimedOutProc(cmd)
        return real_popen(cmd, *a, **kw)
    with mock.patch.object(ci.subprocess, "Popen", landed_then_hang):
        row = ci.process(repo, False, False)
    st = row["status"]
    assert st.startswith("ERROR") and "HEAD moved to " in st and "files kept as written" in st, row
    assert "files restored" not in st and "CHANGED" not in st, st
    head = hv2.head(repo)
    assert f"HEAD moved to {head[:12]}" in st, (head, st)
    # the tree equals the new commit: nothing was rolled back under it
    assert hv2.git(repo, "status", "--porcelain").out.strip() == "", hv2.git(repo, "status", "--porcelain").out
    assert wg.triggers(rd(repo / WF).decode()) == {"workflow_dispatch"}
    assert rd(repo / WF) == hv2.git(repo, "show", f"HEAD:{WF}").out.encode("utf-8")


def test_commit_timeout_with_unreadable_head_keeps_the_files():
    root = mk_root()
    repo = mk_project(root, "tmo_nohead", {WF: WF_BLOCK_TXT})
    real_git, seen = ci.git, {"n": 0}

    def git_wrapper(r, *args):
        if args[:2] == ("rev-parse", "HEAD"):
            seen["n"] += 1
            if seen["n"] > 1:  # the first call is the head0 read before the commit; later ones fail
                return subprocess.CompletedProcess(["git"], 128, "", "fatal: cannot read HEAD")
        return real_git(r, *args)
    with mock.patch.object(ci, "git", git_wrapper), mock.patch.object(ci.subprocess, "Popen", timeout_on(has("commit"))):
        row = ci.process(repo, False, False)
    st = row["status"]
    assert st.startswith("ERROR") and "HEAD unreadable" in st and "files kept as written" in st, row
    assert "files restored" not in st and "CHANGED" not in st, st
    assert wg.triggers(rd(repo / WF).decode()) == {"workflow_dispatch"}, "the rewritten workflow must stay as written"


def test_taskkill_is_the_absolute_system32_path_and_the_timeout_kill_uses_it():
    assert os.path.isabs(ci.TASKKILL), ci.TASKKILL
    assert ci.TASKKILL.lower().endswith(os.path.join("system32", "taskkill.exe")), ci.TASKKILL
    real, cmds = subprocess.run, []

    def spy(cmd, *a, **kw):
        cmds.append(cmd)
        return real(cmd, *a, **kw)
    with mock.patch.object(ci, "GIT_TIMEOUT", 0.5), mock.patch.object(ci.subprocess, "run", spy):
        try:
            ci._run([hv2.PY, "-I", "-c", "import time; time.sleep(30)"])
            raise AssertionError("no GitTimeout raised")
        except ci.GitTimeout:
            pass
    if os.name == "nt":
        assert cmds and cmds[0][0] == ci.TASKKILL and cmds[0][1:4] == ["/T", "/F", "/PID"], cmds


def test_keyboard_interrupt_in_a_dirty_run_still_removes_the_private_ref_and_worktree_then_reraises():
    root, scratch, repo = dirty_repo("kbd")
    before = snapshot(repo)

    def interrupted(r, include_deploy, scr, st):
        old = hv2.head(r)
        ref, wt = "refs/harness/ci-off/kbd-test", scr / "cioff-kbd-test"
        st["ref"], st["wt"] = ref, wt
        assert hv2.git(r, "update-ref", ref, old).rc == 0
        assert hv2.git(r, "worktree", "add", "-q", "--detach", str(wt), old).rc == 0
        assert wt.is_dir() and "refs/harness/ci-off/kbd-test" in hv2.git(r, "for-each-ref", "--format=%(refname)").out
        raise KeyboardInterrupt()
    with mock.patch.object(ci, "_dirty_run", interrupted):
        try:
            ci.process_dirty(repo, False, scratch)
            raise AssertionError("KeyboardInterrupt must be re-raised")
        except KeyboardInterrupt:
            pass
    assert_no_ci_off_refs(repo)
    assert not (scratch / "cioff-kbd-test").exists()
    assert hv2.git(repo, "worktree", "list", "--porcelain").out.count("worktree ") == 1
    assert snapshot(repo) == before


def test_error_result_with_a_journal_present_points_to_recover_and_other_results_do_not():
    root, scratch, repo = dirty_repo("jhint")
    jp = repo / ".git" / JOURNAL
    jp.write_text("{}", encoding="utf-8")

    def fake(result):
        def run(r, include_deploy, scr, st):
            st["jp"] = jp
            return result
        return run
    with mock.patch.object(ci, "_dirty_run", fake("ERROR boom")):
        assert ci.process_dirty(repo, False, scratch) == "ERROR boom; journal kept, run --recover"
    with mock.patch.object(ci, "_dirty_run", fake("CHANGED abc1234 (dirty merge into x)")):
        assert ci.process_dirty(repo, False, scratch) == "CHANGED abc1234 (dirty merge into x)"
    with mock.patch.object(ci, "_dirty_run", fake("SKIP something")):
        assert ci.process_dirty(repo, False, scratch) == "SKIP something"
    jp.unlink()
    with mock.patch.object(ci, "_dirty_run", fake("ERROR boom")):
        assert ci.process_dirty(repo, False, scratch) == "ERROR boom"  # no journal: nothing to recover


def test_a_real_failed_run_that_leaves_its_journal_ends_with_the_recover_hint():
    root, scratch, repo = dirty_repo("jhint_real")
    real, st = ci.refresh_index, {"n": 0}

    def boom_twice(r, ita):  # forward refresh fails, and so does the rollback's: the journal must stay
        st["n"] += 1
        if st["n"] <= 2:
            raise RuntimeError("injected boom")
        return real(r, ita)
    with mock.patch.object(ci, "refresh_index", boom_twice):
        out = ci.process_dirty(repo, False, scratch)
    assert out.startswith("ERROR injected boom; rollback incomplete, journal kept (use --recover)"), out
    assert out.endswith("; journal kept, run --recover"), out
    assert (repo / ".git" / JOURNAL).is_file()
    assert ci.recover(repo).startswith("RECOVERED")


def test_commit_timeout_reports_a_stale_index_lock():
    root = mk_root()
    repo = mk_project(root, "tmo_lockfile", {WF: WF_BLOCK_TXT})
    (repo / ".git" / "index.lock").write_bytes(b"")  # what a killed git leaves behind (the fake commit never runs)
    real = ci.git

    def git_no_lock_effect(r, *args):
        return real(r, *args)
    with mock.patch.object(ci.subprocess, "Popen", timeout_on(lambda c: "commit" in c or "add" in c)):
        row = ci.process(repo, False, False)
    assert row["status"].startswith("ERROR"), row
    assert "stale .git/index.lock exists" in row["status"] or "index reset failed" in row["status"], row
    assert "CHANGED" not in row["status"]


def test_timeout_outside_the_lock_propagates_as_oserror_and_main_exits_1():
    root, scratch, repo = dirty_repo("tmo_main")
    before = snapshot(repo)
    with mock.patch.object(ci.subprocess, "Popen", timeout_on(has("symbolic-ref"))):
        try:
            ci.process(repo, False, False, True, scratch)
            raise AssertionError("GitTimeout must propagate out of process(), not become a SKIP row")
        except ci.GitTimeout as e:
            assert not isinstance(e, TimeoutError)
    out, argv = io.StringIO(), ["ci_triggers_off", "--root", str(root), "--scratch", str(scratch), "--dirty-ok"]
    with mock.patch.object(ci.subprocess, "Popen", timeout_on(has("symbolic-ref"))), \
            mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
        rc = ci.main()
    assert rc == 1, (rc, out.getvalue())
    assert "ERROR GitTimeout" in out.getvalue() and "holds the lock" not in out.getvalue(), out.getvalue()
    assert snapshot(repo) == before


def test_timeout_inside_the_lock_is_an_error_row_not_lock_busy_and_main_exits_1():
    root, scratch, repo = dirty_repo("tmo_lock")
    before = snapshot(repo)
    with mock.patch.object(ci.subprocess, "Popen", timeout_on(has("diff", "--cached"))):
        row = ci.process(repo, False, False, True, scratch)
    assert row["status"].startswith("ERROR") and "timeout after" in row["status"], row
    assert "holds the lock" not in row["status"] and "SKIP" not in row["status"], row
    assert snapshot(repo) == before
    out, argv = io.StringIO(), ["ci_triggers_off", "--root", str(root), "--scratch", str(scratch), "--dirty-ok"]
    with mock.patch.object(ci.subprocess, "Popen", timeout_on(has("diff", "--cached"))), \
            mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
        rc = ci.main()
    assert rc == 1, (rc, out.getvalue())
    assert "ERROR timeout after" in out.getvalue() and "holds the lock" not in out.getvalue(), out.getvalue()
    assert snapshot(repo) == before
    again = run_tool(root, scratch, "--dirty-ok")  # the timeout left no lock, journal, worktree or ref behind
    assert again.rc == 0 and "CHANGED" in again.out, again.all
    no_leftovers(repo, scratch)
    assert_no_ci_off_refs(repo)


def test_cleanup_timeout_is_appended_and_neither_hides_the_result_nor_skips_the_other_cleanup_steps():
    root, scratch, repo = dirty_repo("tmo_cleanup")
    with mock.patch.object(ci.subprocess, "Popen", timeout_on(has("worktree", "remove"))):
        out = ci.process_dirty(repo, False, scratch)
    assert out.startswith("CHANGED"), out  # the computed result is kept
    assert "cleanup incomplete: worktree remove: timeout after" in out, out
    assert_no_ci_off_refs(repo)  # the later steps (prune, private ref) still ran


def test_setup_timeout_after_the_private_ref_exists_still_removes_the_ref():
    root, scratch, repo = dirty_repo("tmo_setup")
    with mock.patch.object(ci.subprocess, "Popen", timeout_on(has("worktree", "add"))):
        out = ci.process_dirty(repo, False, scratch)
    assert out.startswith("ERROR") and "timeout after" in out, out
    assert_no_ci_off_refs(repo)
    assert not (repo / ".git" / JOURNAL).exists()


def test_merge_file_timeout_in_a_dirty_run_is_an_error_row_and_changes_nothing():
    root, scratch, repo = dirty_repo("tmo_merge")
    before = snapshot(repo)
    with mock.patch.object(ci.subprocess, "Popen", timeout_on(has("merge-file"))):
        out = ci.process_dirty(repo, False, scratch)
    assert out.startswith("ERROR") and "timeout after" in out, out
    assert snapshot(repo) == before
    assert not (repo / ".git" / JOURNAL).exists()
    no_leftovers(repo, scratch)
    assert_no_ci_off_refs(repo)


# ---------------------------------------------------------------- criterion 2: an undeletable journal is reported
def test_recover_of_a_completed_run_with_an_undeletable_journal_is_an_error_row():
    root, scratch, repo = dirty_repo("jr_done")
    _crash_at(root, scratch, "done")
    jp = repo / ".git" / JOURNAL
    with mock.patch.object(Path, "unlink", unlink_fails_for(is_journal)):
        msg = ci.recover(repo)
    assert msg.startswith("ERROR run had completed at"), msg
    assert "WARNING journal" in msg and "could not be deleted" in msg and "--recover" in msg, msg
    assert jp.is_file()
    assert ci.process(repo, False, False, True, scratch)["status"].endswith(ci.INTERRUPTED)  # still refuses, as announced
    ok = ci.recover(repo)
    assert ok.startswith("RECOVERED run had completed"), ok
    assert not jp.exists()


def test_recover_of_an_interrupted_run_with_an_undeletable_journal_is_an_error_row_but_files_are_restored():
    root, scratch, repo = dirty_repo("jr_mid")
    before = snapshot(repo)
    _crash_at(root, scratch, "files-merged")
    jp = repo / ".git" / JOURNAL
    with mock.patch.object(Path, "unlink", unlink_fails_for(is_journal)):
        msg = ci.recover(repo)
    assert msg.startswith("ERROR from phase files-merged"), msg
    assert "WARNING journal" in msg and "could not be deleted" in msg, msg
    assert "RECOVERED" not in msg, msg
    assert jp.is_file()
    assert snapshot(repo)["files"] == before["files"] and hv2.head(repo) == before["head"]  # the restore itself worked
    ok = ci.recover(repo)  # idempotent second pass, journal now deletable
    assert ok.startswith("RECOVERED") and not jp.exists(), ok
    assert snapshot(repo) == before


def test_success_path_reports_a_warning_when_the_journal_cannot_be_deleted():
    root, scratch, repo = dirty_repo("jr_ok")
    n = commits(repo)
    with mock.patch.object(Path, "unlink", unlink_fails_for(is_journal)):
        out = ci.process_dirty(repo, False, scratch)
    assert out.startswith("CHANGED"), out  # the commit is valid
    assert "WARNING journal" in out and "could not be deleted" in out and "run --recover" in out, out
    assert commits(repo) == n + 1
    jp = repo / ".git" / JOURNAL
    assert jp.is_file()
    assert ci.process(repo, False, False, True, scratch)["status"].endswith(ci.INTERRUPTED)
    assert ci.recover(repo).startswith("RECOVERED run had completed")
    assert not jp.exists()
    assert commits(repo) == n + 1, "recovering a completed run must not undo the commit"


def test_failed_run_that_rolls_back_cleanly_still_warns_about_an_undeletable_journal():
    root, scratch, repo = dirty_repo("jr_fail")
    before = snapshot(repo)
    real, st = ci.refresh_index, {"n": 0}

    def once_boom(r, ita):
        st["n"] += 1
        if st["n"] == 1:
            raise RuntimeError("injected boom")
        return real(r, ita)
    with mock.patch.object(ci, "refresh_index", once_boom), mock.patch.object(Path, "unlink", unlink_fails_for(is_journal)):
        out = ci.process_dirty(repo, False, scratch)
    assert out.startswith("ERROR injected boom; working files restored, branch back at"), out
    assert "WARNING journal" in out and "could not be deleted" in out, out
    assert out.endswith("; journal kept, run --recover"), out
    assert snapshot(repo)["files"] == before["files"] and hv2.head(repo) == before["head"]
    assert ci.recover(repo).startswith("RECOVERED") and not (repo / ".git" / JOURNAL).exists()


# ---------------------------------------------------------------- criterion 3: refused rollback refreshes the index
def test_refused_rollback_refreshes_the_index_and_still_prints_the_refusal():
    root, scratch, repo = dirty_repo("rf_idx")
    _crash_at(root, scratch, "files-merged")
    merged = rd(repo / WF)
    _w(repo / WF, merged + b"\nLATER-EDIT-AFTER-CRASH\n")
    real, calls = ci.git, []

    def spy(r, *args):
        calls.append(args)
        return real(r, *args)
    with mock.patch.object(ci, "git", spy):
        msg = ci.recover(repo)
    assert ("update-index", "-q", "--refresh") in calls, calls
    assert msg.startswith("ERROR recovery incomplete, journal kept"), msg
    assert f"{WF} changed after the interrupted run" in msg and "nothing restored" in msg, msg
    assert rd(repo / WF) == merged + b"\nLATER-EDIT-AFTER-CRASH\n"
    # the refresh happens before the refusal is returned: no restore step ran after it
    assert ("update-ref",) not in [c[:1] for c in calls] and not any(c[:1] == ("reset",) for c in calls), calls
    cli = run_tool(root, scratch, "--recover")  # and the command line prints the refusal and exits 1
    assert cli.rc == 1 and "changed after the interrupted run" in cli.out and WF in cli.out, cli.all


def test_clean_rollback_does_not_take_the_refusal_branch():
    root, scratch, repo = dirty_repo("rf_ok")
    _crash_at(root, scratch, "files-merged")
    real, calls = ci.git, []

    def spy(r, *args):
        calls.append(args)
        return real(r, *args)
    with mock.patch.object(ci, "git", spy):
        msg = ci.recover(repo)
    assert msg.startswith("RECOVERED"), msg
    assert ("update-index", "-q", "--refresh") not in calls, calls


# ---------------------------------------------------------------- criterion 4: globs are escaped, undeletable temp reported
def test_temp_file_of_a_workflow_named_with_brackets_is_cleaned_and_a_lookalike_is_left_alone():
    root, scratch, repo = bracket_repo("gl_tmp")
    before = snap_files(repo, BRACKET_WF, DL, DECOY_TMP, DECOY_PARK)
    res = _crash_helper(root, scratch, repo, "run", BRACKET_WF, "before-aside")
    assert res.rc == 70, res.all
    p = repo / BRACKET_WF
    tmps = [t for t in p.parent.iterdir() if t.name.startswith(".w[1].yml.cioff-") and t.name.endswith(".tmp")]
    assert len(tmps) == 1, list(p.parent.iterdir())  # the crash really left the temp file of w[1].yml
    rec = run_tool(root, scratch, "--recover")
    assert rec.rc == 0 and "RECOVERED" in rec.out, rec.all
    assert not tmps[0].exists(), "temp file of w[1].yml was not cleaned: the glob was not escaped"
    assert rd(repo / DECOY_TMP) == before[DECOY_TMP], "temp file of w1.yml must not be touched by w[1].yml's cleanup"
    assert rd(repo / DECOY_PARK) == before[DECOY_PARK]
    assert rd(repo / BRACKET_WF) == before[BRACKET_WF] and rd(repo / DL) == before[DL]
    assert not (repo / ".git" / JOURNAL).exists()


def test_undeletable_temp_file_is_reported_and_the_journal_is_kept():
    root, scratch, repo = dirty_repo("gl_lock")
    _crash_helper(root, scratch, repo, "run", WF, "before-aside")
    p = repo / WF
    tmps = list(p.parent.glob(f".{p.name}.cioff-*.tmp"))
    assert len(tmps) == 1, tmps
    with mock.patch.object(Path, "unlink", unlink_fails_for(lambda s: s.name == tmps[0].name)):
        msg = ci.recover(repo)
    assert msg.startswith("ERROR recovery incomplete, journal kept"), msg
    assert f"could not delete temp file {tmps[0].name}" in msg and "delete it by hand" in msg and "locked" in msg, msg
    assert (repo / ".git" / JOURNAL).is_file() and tmps[0].exists()
    ok = ci.recover(repo)  # once the file can be deleted the recovery completes
    assert ok.startswith("RECOVERED") and not tmps[0].exists(), ok


def _merged_state_with_aside(name):
    """Crash after files-merged, then give the bracket workflow a parked copy that holds its (known) merged bytes."""
    root, scratch, repo = bracket_repo(name)
    before = snapshot_bracket(repo)
    _crash_at(root, scratch, "files-merged")
    p = repo / BRACKET_WF
    a = ci.aside_path(p)
    a.write_bytes(rd(p))
    return root, scratch, repo, p, a, before


def snapshot_bracket(repo):
    return snap_files(repo, BRACKET_WF, DL, DECOY_TMP, DECOY_PARK) | {"head": hv2.head(repo)}


def test_park_copy_that_is_a_second_name_of_the_parked_file_is_cleaned_by_recover():
    root, scratch, repo, p, a, before = _merged_state_with_aside("pk_same")
    t = p.with_name(f".{p.name}.cioff-park-deadbeef")
    os.link(a, t)  # POSIX _park crashed after moving p to t: t is only a second name of the parked copy
    msg = ci.recover(repo)
    assert msg.startswith("RECOVERED"), msg
    assert not t.exists(), "'.cioff-park-*' second name must be removed by rollback"
    assert not a.exists() or "-kept-" not in a.name
    assert rd(p) == before[BRACKET_WF], "pre-run bytes must be back"
    assert rd(repo / DECOY_PARK) == before[DECOY_PARK], "the lookalike park file of w1.yml must be left alone"
    assert rd(repo / DECOY_TMP) == before[DECOY_TMP]
    assert not (repo / ".git" / JOURNAL).exists()


def test_park_copy_with_different_bytes_is_never_deleted_and_blocks_the_recovery():
    root, scratch, repo, p, a, before = _merged_state_with_aside("pk_diff")
    t = p.with_name(f".{p.name}.cioff-park-cafe0001")
    t.write_bytes(b"a newer edit made during the run\n")
    msg = ci.recover(repo)
    assert msg.startswith("ERROR recovery incomplete, journal kept"), msg
    assert f"{t.name} may hold a newer edit made during the run; compare and merge by hand" in msg, msg
    assert rd(t) == b"a newer edit made during the run\n", "bytes of a possible newer edit must survive"
    assert (repo / ".git" / JOURNAL).is_file()
    t.unlink()  # the person merged it by hand
    ok = ci.recover(repo)
    assert ok.startswith("RECOVERED"), ok
    assert rd(p) == before[BRACKET_WF]


# ---------------------------------------------------------------- criterion 5: parked-copy exemption needs the copy untracked
def _kept_run(name, track_after_refresh):
    """process_dirty where every parked copy is kept (retired to a -kept- name and listed in LEFTOVER_ASIDES).
    With track_after_refresh the user stages (git add -N) one of those copies just before the final status check."""
    root, scratch, repo = dirty_repo(name)
    before = snapshot(repo)
    real_drop, real_refresh = ci._drop_parked, ci.refresh_index
    tracked = []

    def keep(a, *args, **k):
        return "kept" if Path(a).name.endswith(".cioff-aside") else real_drop(a, *args, **k)

    def hook(r, *a, **k):
        res = real_refresh(r, *a, **k)
        if track_after_refresh and not tracked and ci.LEFTOVER_ASIDES:
            rel = Path(ci.LEFTOVER_ASIDES[0]).relative_to(repo).as_posix()
            assert hv2.git(repo, "add", "-N", "--", rel).rc == 0
            tracked.append(rel)
        return res
    with mock.patch.object(ci, "_drop_parked", keep), mock.patch.object(ci, "refresh_index", hook):
        out = ci.process_dirty(repo, False, scratch)
    return out, before, repo, tracked


def test_exemption_applies_to_an_untracked_parked_copy_control():
    out, before, repo, _ = _kept_run("ex_ctl", track_after_refresh=False)
    assert out.startswith("CHANGED") and "NOTE kept parked copies" in out and "-kept-" in out, out


def test_a_parked_copy_that_became_tracked_is_a_status_change_error():
    out, before, repo, tracked = _kept_run("ex_trk", track_after_refresh=True)
    assert len(tracked) == 1, "the test hook must have staged one kept copy"
    assert out.startswith("ERROR"), out
    assert f"status changed for {tracked[0]}" in out, out
    assert "NOTE kept parked copies" in out, out
    assert snapshot(repo)["files"] == before["files"], "rolled back to the pre-run bytes"


# ---------------------------------------------------------------- criterion 6: B20 coverage
def test_recover_appends_the_kept_note_naming_the_kept_copies():
    root, scratch, repo = dirty_repo("b20_note")
    before = snapshot(repo)
    res = _crash_helper(root, scratch, repo, "run", WF, "after-place")  # new bytes placed, pre-run copy still parked
    assert res.rc == 70, res.all
    aside = ci.aside_path(repo / WF)
    assert aside.is_file() and rd(aside) == before["files"][WF]
    merged_before = rd(repo / WF)
    real_drop = ci._drop_parked
    with mock.patch.object(ci, "_drop_parked", lambda a, *x, **k: "kept" if Path(a).name.endswith(".cioff-aside") else real_drop(a, *x, **k)):
        msg = ci.recover(repo)
    assert msg.startswith("RECOVERED"), msg
    assert "; NOTE kept parked copies of known old bytes" in msg and "check and delete by hand: " in msg, msg
    kept = sorted(aside.parent.glob(aside.name + "-kept-*"))
    # two copies are kept: the pre-run bytes (parked by the crashed run) and the merged bytes (parked while restoring)
    assert len(kept) == 2 and all(str(k) in msg for k in kept), (kept, msg)
    assert {rd(k) for k in kept} == {before["files"][WF], merged_before}, "kept copies must hold the two known states"
    assert not aside.exists() and ci.LEFTOVER_ASIDES == [], "the fixed parked name is free and the list is cleared"
    assert rd(repo / WF) == before["files"][WF]


def test_recover_without_kept_copies_has_no_note():
    root, scratch, repo = dirty_repo("b20_nonote")
    _crash_at(root, scratch, "files-merged")
    msg = ci.recover(repo)
    assert msg.startswith("RECOVERED") and "NOTE" not in msg, msg


def test_retire_kept_moves_to_a_unique_kept_name_without_overwriting():
    d = hv2.fresh("retire")
    a = d / ".f.txt.cioff-aside"
    a.write_bytes(b"known old bytes\n")
    k = ci._retire_kept(a)
    assert k != a and k.parent == a.parent and k.name.startswith(a.name + "-kept-"), k
    assert not a.exists() and k.read_bytes() == b"known old bytes\n"
    a.write_bytes(b"second parked copy\n")
    k2 = ci._retire_kept(a)
    assert k2 != k and k.read_bytes() == b"known old bytes\n" and k2.read_bytes() == b"second parked copy\n"


def test_retire_kept_move_failure_keeps_the_copy_at_its_name():
    d = hv2.fresh("retire_fail")
    a = d / ".f.txt.cioff-aside"
    a.write_bytes(b"known old bytes\n")
    with mock.patch.object(ci, "_place_noclobber", mock.Mock(side_effect=PermissionError(32, "in use by another process"))):
        assert ci._retire_kept(a) == a
    assert a.read_bytes() == b"known old bytes\n"
    with mock.patch.object(ci, "_place_noclobber", mock.Mock(return_value=False)):  # target name already taken
        assert ci._retire_kept(a) == a
    assert a.read_bytes() == b"known old bytes\n"


def test_swap_in_reports_the_parked_copy_itself_when_it_can_neither_be_deleted_nor_moved():
    d = hv2.fresh("retire_swap")
    p = d / "f.txt"
    p.write_bytes(b"old\n")
    import hashlib
    old = hashlib.sha256(b"old\n").hexdigest()
    real_place = ci._place_noclobber

    def place(src, dst):
        if "-kept-" in Path(dst).name:
            raise PermissionError(32, "in use")
        return real_place(src, dst)
    ci.LEFTOVER_ASIDES.clear()
    try:
        with mock.patch.object(ci, "_drop_parked", lambda *a, **k: "kept"), mock.patch.object(ci, "_place_noclobber", place):
            err = ci.swap_in(p, b"new\n", old)
        assert err is None, err
        assert p.read_bytes() == b"new\n"
        assert ci.LEFTOVER_ASIDES == [str(ci.aside_path(p))], ci.LEFTOVER_ASIDES
        assert ci.aside_path(p).read_bytes() == b"old\n"
    finally:
        ci.LEFTOVER_ASIDES.clear()


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
