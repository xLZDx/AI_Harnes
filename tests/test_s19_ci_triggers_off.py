"""S19-T2 tests: tools/ci_triggers_off.py kills the whole git process tree on a timeout (Windows Job Object, System32 taskkill
fallback that says so), leaves a deliberate background process alone after a normal exit, has an overall --budget, reports
cleanup leftovers without losing the main result (journal removed last), restores files after a commit timeout only when it
is provably safe, prints the rows computed so far on Ctrl-C (return code 130, --recover hint) and keeps the note text when
restore() itself fails.
Written from the S19-T2 acceptance criteria. Temp repos only under D:\\tmp\\harness_v2_tests. The tool is called in-process;
git commands that must misbehave are replaced at the ci.git seam, every other git call is real.
Run: C:\\Python314\\python.exe -I tests\\test_s19_ci_triggers_off.py"""
import contextlib
import io
import os
import subprocess
import sys
import time
import types
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools"))
import hv2  # noqa: E402
import ci_triggers_off as ci  # noqa: E402
import workflow_guard as wg  # noqa: E402
from test_s13_ci_off import mk_root, mk_project, rd, commits, _w  # noqa: E402
from test_s14_ci_dirty import WF, DL, LL, JOURNAL  # noqa: E402

WF_BLOCK_TXT = "name: ci\non:\n  push:\n    branches: [main]\n  pull_request:\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
NT = os.name == "nt"
SLEEPER = "import time; time.sleep(120)"


def _alive(pid):
    if NT:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                             timeout=30).stdout
        return f'"{pid}"' in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _wait_dead(pid, seconds=8.0):
    end = time.time() + seconds
    while time.time() < end:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return not _alive(pid)


def _force_kill(pid):
    if pid and _alive(pid):
        if NT:
            subprocess.run([ci.TASKKILL, "/F", "/PID", str(pid)], capture_output=True, timeout=30)
        else:
            os.kill(pid, 9)


def _read_pid(path, seconds=10.0):
    end = time.time() + seconds
    while time.time() < end:
        try:
            txt = Path(path).read_text().strip()
            if txt:
                return int(txt)
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    return None


def _tree_code(pidfile):
    """A child that starts a grandchild sleeper (like a git hook would), records its pid in a file and sleeps too."""
    return ("import subprocess,sys,time;"
            f"g=subprocess.Popen([sys.executable,'-I','-s','-c',{SLEEPER!r}],stdin=subprocess.DEVNULL);"
            f"open({str(pidfile)!r},'w').write(str(g.pid));time.sleep(120)")


# ---------------------------------------------------------------- Job Object: the whole tree dies on a timeout
def test_gittimeout_is_an_oserror_and_not_a_timeouterror_with_a_kill_flag():
    assert issubclass(ci.GitTimeout, OSError)
    assert not issubclass(ci.GitTimeout, TimeoutError)  # process() maps TimeoutError to "lock busy"
    assert ci.GitTimeout("x").kill_confirmed is False, "the default must be the safe one: not confirmed"
    assert ci.GitTimeout("x", kill_confirmed=True).kill_confirmed is True


def test_timeout_kills_the_child_and_its_grandchild_and_confirms_it():
    if not NT:
        print("SKIP job object tree kill: not Windows")
        return
    d = hv2.fresh("jobtree")
    pidfile = d / "grand.pid"
    grand = None
    try:
        with mock.patch.object(ci, "GIT_TIMEOUT", 8):
            try:
                ci._run([hv2.PY, "-I", "-s", "-c", _tree_code(pidfile)])
                raise AssertionError("no GitTimeout raised")
            except ci.GitTimeout as e:
                err = e
        grand = _read_pid(pidfile, 1)
        assert grand, "setup: the child must have started the grandchild before the timeout"
        assert err.kill_confirmed is True, err
        assert isinstance(err, OSError) and not isinstance(err, TimeoutError)
        assert "no Job Object" not in str(err), str(err)  # the job path was usable: no fallback text
        assert "could not be confirmed dead" not in str(err), str(err)
        assert _wait_dead(grand), f"grandchild {grand} survived the timeout kill"
    finally:
        _force_kill(grand)


# ---------------------------------------------------------------- Job Object assignment fails: System32 taskkill fallback
def _job_assign_fails_factory():
    real = ci._win_job_api()  # built before the patch is active

    class K:
        def __getattr__(self, name):
            return getattr(real.k, name)

        def AssignProcessToJobObject(self, *a):
            return 0  # the Win32 failure value
    fake = types.SimpleNamespace(ctypes=real.ctypes, k=K(), Extended=real.Extended)
    return lambda: fake


def test_job_assignment_failure_falls_back_to_system32_taskkill_and_says_so():
    if not NT:
        print("SKIP taskkill fallback: not Windows")
        return
    cmds, procs = [], []
    real_run, real_popen = subprocess.run, subprocess.Popen

    def run_spy(cmd, *a, **kw):
        cmds.append(cmd)
        return real_run(cmd, *a, **kw)

    class PopenSpy(real_popen):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            procs.append(self)
    with mock.patch.object(ci, "_win_job_api", _job_assign_fails_factory()),mock.patch.object(ci, "GIT_TIMEOUT", 1.5), \
            mock.patch.object(ci.subprocess, "run", run_spy), mock.patch.object(ci.subprocess, "Popen", PopenSpy):
        try:
            ci._run([hv2.PY, "-I", "-s", "-c", SLEEPER])
            raise AssertionError("no GitTimeout raised")
        except ci.GitTimeout as e:
            err = e
    try:
        assert cmds and cmds[0][0] == ci.TASKKILL and cmds[0][1:4] == ["/T", "/F", "/PID"], cmds
        assert os.path.basename(os.path.dirname(cmds[0][0])).lower() == "system32", cmds[0][0]
        assert cmds[0][4] == str(procs[0].pid), (cmds, procs[0].pid)
        msg = str(err)
        assert "no Job Object" in msg and "AssignProcessToJobObject failed" in msg, msg
        assert "fell back to taskkill /T /F" in msg and "can miss a process started by a git hook" in msg, msg
        assert err.kill_confirmed is True, msg
        assert procs[0].poll() is not None, "the child must be dead after the fallback kill"
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()


# ---------------------------------------------------------------- normal exit: a deliberate background process survives
def test_a_background_process_started_by_a_normally_finished_child_is_not_killed():
    d = hv2.fresh("bgsurvive")
    pidfile = d / "bg.pid"
    code = ("import subprocess,sys;"
            f"g=subprocess.Popen([sys.executable,'-I','-s','-c',{SLEEPER!r}],stdin=subprocess.DEVNULL,"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
            f"open({str(pidfile)!r},'w').write(str(g.pid))")
    bg = None
    try:
        r = ci._run([hv2.PY, "-I", "-s", "-c", code])
        assert r.returncode == 0, r
        bg = _read_pid(pidfile)
        assert bg, "setup: the child must have started the background process"
        time.sleep(1.0)  # a close of the job handle would already have killed it
        assert _alive(bg), "the deliberate background process of the child was killed after a normal exit"
    finally:
        _force_kill(bg)


# ---------------------------------------------------------------- overall budget
def _fake_projects(root, names):
    for n in names:
        (root / n / ".git").mkdir(parents=True)
        (root / n / ".github" / "workflows").mkdir(parents=True)


def _run_main(root, scratch, *args, process):
    out = io.StringIO()
    with mock.patch.object(ci, "process", process), mock.patch.object(sys, "argv",
                                                                       ["ci_triggers_off.py", "--root", str(root), "--scratch", str(scratch), *args]):
        with contextlib.redirect_stdout(out):
            rc = ci.main()
    return rc, out.getvalue()


def _table(text):
    rows = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.split(" | ")]
        if len(cells) == 5 and cells[0] not in ("repo",) and not set(cells[0]) <= set("-+"):
            rows[cells[0]] = cells[4]
    return rows


def _slow(seconds, seen):
    def process(d, include_deploy, dry, dirty_ok, scratch, recover_run):
        seen.append(d.name)
        time.sleep(seconds)
        return {"repo": d.name, "branch": "main", "files": "w.yml", "trig": "w.yml: push", "status": "CHANGED abc1234"}
    return process


def test_budget_spent_gives_every_remaining_project_an_error_row_and_none_is_missing():
    root, scratch, seen = hv2.fresh("budget"), hv2.fresh("budget_scratch"), []
    _fake_projects(root, ["a1", "b2", "c3", "d4"])
    rc, out = _run_main(root, scratch, "--budget", "1", process=_slow(1.3, seen))
    rows = _table(out)
    assert seen == ["a1"], seen  # only the first project fitted into the budget; the others were never started
    assert set(rows) == {"a1", "b2", "c3", "d4"}, (rows, out)  # none silently missing
    assert rows["a1"].startswith("CHANGED"), rows
    for n in ("b2", "c3", "d4"):
        assert rows[n].startswith("ERROR") and "budget of 1s is spent" in rows[n], (n, rows[n])
    assert rc == 1, rc  # an ERROR row makes the run fail


def test_budget_zero_means_no_limit_and_the_same_slow_run_with_a_small_budget_is_cut_short():
    root, scratch = hv2.fresh("budget0"), hv2.fresh("budget0_scratch")
    _fake_projects(root, ["a1", "b2", "c3"])
    seen = []
    rc, out = _run_main(root, scratch, "--budget", "0.1", process=_slow(0.4, seen))  # control: the work exceeds it
    assert seen == ["a1"] and "budget of 0.1s is spent" in out, (seen, out)
    seen = []
    rc, out = _run_main(root, scratch, "--budget", "0", process=_slow(0.4, seen))
    rows = _table(out)
    assert seen == ["a1", "b2", "c3"], seen
    assert set(rows.values()) == {"CHANGED abc1234"} and "budget" not in out, (rows, out)
    assert rc == 0, rc


# ---------------------------------------------------------------- cleanup leftovers
def _wt_fixture(name):
    repo = hv2.mk_repo(name)
    head = hv2.head(repo)
    wt = hv2.fresh(name + "_wt") / "w"
    assert hv2.git(repo, "worktree", "add", "-q", "--detach", str(wt), head).rc == 0
    ref = f"refs/harness/ci-off/{name}"
    assert hv2.git(repo, "update-ref", ref, head).rc == 0
    work = hv2.fresh(name + "_work")
    bdir, tdir, jp = work / "backup", work / "merge", work / JOURNAL
    for dd in (bdir, tdir):
        dd.mkdir()
        (dd / "f").write_bytes(b"x")
    jp.write_text("{}", encoding="utf-8")
    j = {"worktree": str(wt), "ref": ref, "backup_dir": str(bdir), "tmpdir": str(tdir)}
    return repo, wt, ref, bdir, tdir, jp, j


def _git_failing(fail, events=None):
    """ci.git stand-in: a call whose args start with a key of `fail` returns that failure (or raises it); others are real."""
    real = ci.git

    def g(repo, *args):
        if events is not None:
            events.append(("git", args[:2]))
        for prefix, act in fail.items():
            if args[:len(prefix)] == prefix:
                if isinstance(act, BaseException):
                    raise act
                return subprocess.CompletedProcess(["git", *args], 1, "", act)
        return real(repo, *args)
    return g


def _ref_exists(repo, ref):
    return hv2.git(repo, "rev-parse", "-q", "--verify", ref).rc == 0


def test_cleanup_run_clean_success_returns_empty_and_removes_everything_journal_last():
    repo, wt, ref, bdir, tdir, jp, j = _wt_fixture("cl_ok")
    events = []
    real_unlink, real_rmtree = Path.unlink, ci.shutil.rmtree

    def unlink(self, *a, **k):
        if self == jp:
            events.append(("unlink", "journal"))
        return real_unlink(self, *a, **k)

    def rmtree(p, *a, **k):
        events.append(("rmtree", Path(p).name))
        return real_rmtree(p, *a, **k)
    with mock.patch.object(ci, "git", _git_failing({}, events)), mock.patch.object(Path, "unlink", unlink), \
            mock.patch.object(ci.shutil, "rmtree", rmtree):
        out = ci.cleanup_run(repo, j, jp)
    assert out == "", out
    assert not wt.exists() and not bdir.exists() and not tdir.exists() and not jp.exists()
    assert not _ref_exists(repo, ref)
    assert events[-1] == ("unlink", "journal"), events  # the journal goes last
    assert {("rmtree", "backup"), ("rmtree", "merge")} <= set(events), events


def test_failing_worktree_remove_and_ref_delete_are_reported_rmtree_fallback_runs_and_journal_goes_last():
    repo, wt, ref, bdir, tdir, jp, j = _wt_fixture("cl_fail")
    events = []
    real_unlink, real_rmtree = Path.unlink, ci.shutil.rmtree

    def unlink(self, *a, **k):
        if self == jp:
            events.append(("unlink", "journal"))
        return real_unlink(self, *a, **k)

    def rmtree(p, *a, **k):
        events.append(("rmtree", Path(p).name))
        return real_rmtree(p, *a, **k)
    fail = {("worktree", "remove"): "boom-remove", ("update-ref", "-d"): "boom-ref"}
    with mock.patch.object(ci, "git", _git_failing(fail, events)), mock.patch.object(Path, "unlink", unlink), \
            mock.patch.object(ci.shutil, "rmtree", rmtree):
        out = ci.cleanup_run(repo, j, jp)
    assert out.startswith("; cleanup incomplete: "), out
    assert "worktree remove: git exit 1: boom-remove" in out, out
    assert f"private ref {ref}: git exit 1: boom-ref" in out, out
    assert not wt.exists(), "the shutil.rmtree fallback must remove the leftover worktree directory"
    assert ("rmtree", "w") in events, events
    assert _ref_exists(repo, ref), "the failed ref delete really left the ref"
    assert not bdir.exists() and not tdir.exists(), "later steps run after a failed one"
    assert not jp.exists() and events[-1] == ("unlink", "journal"), events


def test_failing_rmtree_fallback_is_reported_and_later_steps_still_run():
    repo, wt, ref, bdir, tdir, jp, j = _wt_fixture("cl_rmfail")
    real_rmtree = ci.shutil.rmtree

    def rmtree(p, *a, **k):
        if Path(p) == wt:
            raise PermissionError(5, "directory in use")
        return real_rmtree(p, *a, **k)
    with mock.patch.object(ci, "git", _git_failing({("worktree", "remove"): "boom"})), mock.patch.object(ci.shutil, "rmtree", rmtree):
        out = ci.cleanup_run(repo, j, jp)
    assert "leftover directory" in out and "directory in use" in out and "delete it by hand" in out, out
    assert wt.exists(), "the stub kept the directory"
    assert not _ref_exists(repo, ref) and not bdir.exists() and not tdir.exists() and not jp.exists(), out


def test_a_git_timeout_in_one_cleanup_step_does_not_skip_the_others():
    repo, wt, ref, bdir, tdir, jp, j = _wt_fixture("cl_tmo")
    hang = ci.GitTimeout("timeout after 1s: git worktree remove", kill_confirmed=True)
    with mock.patch.object(ci, "git", _git_failing({("worktree", "remove"): hang})):
        out = ci.cleanup_run(repo, j, jp)
    assert "cleanup incomplete" in out and "worktree remove: timeout after 1s" in out, out
    assert not wt.exists() and not _ref_exists(repo, ref) and not bdir.exists() and not jp.exists(), out


def test_an_undeletable_journal_is_warned_about_and_the_other_steps_were_done_first():
    repo, wt, ref, bdir, tdir, jp, j = _wt_fixture("cl_jfail")
    real_unlink = Path.unlink

    def unlink(self, *a, **k):
        if self == jp:
            raise PermissionError(13, "journal locked")
        return real_unlink(self, *a, **k)
    with mock.patch.object(Path, "unlink", unlink):
        out = ci.cleanup_run(repo, j, jp)
    assert "WARNING journal" in out and "journal locked" in out and "--recover" in out, out
    assert not wt.exists() and not _ref_exists(repo, ref) and not bdir.exists()
    assert jp.exists()


def test_process_dirty_keeps_the_computed_result_and_appends_cleanup_incomplete():
    repo, wt, ref, bdir, tdir, jp, j = _wt_fixture("cl_pd")
    scratch = hv2.fresh("cl_pd_scratch")

    def run(r, include_deploy, scr, st):
        st["wt"], st["ref"], st["jp"] = wt, ref, jp
        return "CHANGED abc1234 (dirty merge into x)"
    fail = {("worktree", "remove"): "boom-remove", ("update-ref", "-d"): "boom-ref"}
    with mock.patch.object(ci, "_dirty_run", run), mock.patch.object(ci, "git", _git_failing(fail)):
        out = ci.process_dirty(repo, False, scratch)
    assert out.startswith("CHANGED abc1234 (dirty merge into x); cleanup incomplete: "), out
    assert "boom-remove" in out and "boom-ref" in out, out
    assert not wt.exists(), "the rmtree fallback removed the directory"


# ---------------------------------------------------------------- commit timeout: when files are restored
def _commit_hook(kind, kill_confirmed, reset_fails=False, log=None):
    """ci.git stand-in for process(): the `git commit` call hangs ('hang') or really lands and then hangs ('landed')."""
    real = ci.git
    st = {"timed_out": False}

    def g(repo, *args):
        if log is not None:
            log.append(("git", args))
        if args[:1] == ("commit",):
            if kind == "landed":
                real(repo, *args)
            st["timed_out"] = True
            raise ci.GitTimeout("timeout after 1s: git commit", kill_confirmed=kill_confirmed)
        if reset_fails and st["timed_out"] and args[:2] == ("reset", "-q"):
            return subprocess.CompletedProcess(["git", *args], 1, "", "fatal: Unable to create index.lock")
        return real(repo, *args)
    return g, st


def _timeout_case(name, kind, kill_confirmed, reset_fails=False):
    root = mk_root()
    repo = mk_project(root, name, {WF: WF_BLOCK_TXT})
    before = {rel: rd(repo / rel) for rel in (WF, DL, LL)}
    head0, n = hv2.head(repo), commits(repo)
    g, _ = _commit_hook(kind, kill_confirmed, reset_fails)
    with mock.patch.object(ci, "git", g):
        row = ci.process(repo, False, False)
    return repo, row, before, head0, n


def test_commit_timeout_with_confirmed_kill_unchanged_head_and_ok_reset_restores_the_files():
    repo, row, before, head0, n = _timeout_case("rs_ok", "hang", True)
    st = row["status"]
    assert st.startswith("ERROR timeout after 1s"), row  # an ERROR row, never "SKIP lock busy" and never success
    assert "HEAD unchanged, no commit landed" in st and "; files restored" in st, st
    assert "files kept as written" not in st and "not confirmed killed" not in st, st
    for rel, data in before.items():
        assert rd(repo / rel) == data, rel
    assert hv2.head(repo) == head0 and commits(repo) == n
    assert hv2.git(repo, "status", "--porcelain").out.strip() == ""


def test_commit_timeout_without_a_confirmed_kill_keeps_the_files_as_written_and_says_why():
    repo, row, before, head0, n = _timeout_case("rs_nokill", "hang", False)
    st = row["status"]
    assert st.startswith("ERROR timeout after 1s"), row
    assert "the timed-out git was not confirmed killed, it may still write" in st, st
    assert "files kept as written" in st and "files restored" not in st, st
    assert "HEAD unchanged, no commit landed" in st, st  # the other conditions held; the kill flag alone decides
    assert wg.triggers(rd(repo / WF).decode()) == {"workflow_dispatch"}, "the rewritten workflow stays"
    assert b"CI-OFF" in rd(repo / DL) and b"CI-OFF" in rd(repo / LL), "the log entries stay"
    assert hv2.head(repo) == head0


def test_commit_timeout_when_the_commit_landed_keeps_the_files_even_with_a_confirmed_kill():
    repo, row, before, head0, n = _timeout_case("rs_landed", "landed", True)
    st = row["status"]
    head = hv2.head(repo)
    assert head != head0 and commits(repo) == n + 1, "setup: the commit really landed"
    assert st.startswith("ERROR") and f"HEAD moved to {head[:12]}, the commit landed" in st, st
    assert "files kept as written" in st and "files restored" not in st, st
    assert rd(repo / WF) == hv2.git(repo, "show", f"HEAD:{WF}").out.encode("utf-8"), "the tree equals the new commit"
    assert hv2.git(repo, "status", "--porcelain").out.strip() == ""


def test_commit_timeout_with_a_failed_index_reset_keeps_the_files():
    repo, row, before, head0, n = _timeout_case("rs_reset", "hang", True, reset_fails=True)
    st = row["status"]
    assert st.startswith("ERROR timeout after 1s"), row
    assert "index reset failed" in st and "touched paths may stay staged" in st, st
    assert "files kept as written" in st and "files restored" not in st, st
    assert wg.triggers(rd(repo / WF).decode()) == {"workflow_dispatch"}


def test_head0_is_read_before_the_first_file_is_written():
    root = mk_root()
    repo = mk_project(root, "order", {WF: WF_BLOCK_TXT})
    head0 = hv2.head(repo)
    events, real_write = [], Path.write_bytes
    real_git = ci.git

    def git_spy(r, *args):
        res = real_git(r, *args)
        if args == ("rev-parse", "HEAD"):
            events.append(("head", res.stdout.strip()))
        return res

    def write_spy(self, data):
        if self.name in ("ci.yml", "DECISION_LOG.md", "LESSONS_LOG.md"):
            events.append(("write", self.name))
        return real_write(self, data)
    with mock.patch.object(ci, "git", git_spy), mock.patch.object(Path, "write_bytes", write_spy):
        row = ci.process(repo, False, False)
    assert row["status"].startswith("CHANGED"), row
    kinds = [k for k, _ in events]
    assert kinds[0] == "head" and events[0][1] == head0, events  # read first, and it is the pre-run HEAD
    assert kinds.index("head") < kinds.index("write"), events
    assert [v for k, v in events if k == "write"][0] == "ci.yml", events


# ---------------------------------------------------------------- restore() itself fails
def test_a_restore_oserror_keeps_the_timeout_note_and_adds_files_not_fully_restored():
    root = mk_root()
    repo = mk_project(root, "rs_fail", {WF: WF_BLOCK_TXT})
    before = {rel: rd(repo / rel) for rel in (WF, DL, LL)}
    g, st = _commit_hook("hang", True)
    real_write = Path.write_bytes

    def write(self, data):
        if st["timed_out"] and self.name == "ci.yml":
            raise PermissionError(13, "ci.yml is locked")
        return real_write(self, data)
    with mock.patch.object(ci, "git", g), mock.patch.object(Path, "write_bytes", write):
        row = ci.process(repo, False, False)
    s = row["status"]
    assert s.startswith("ERROR timeout after 1s"), s
    assert "HEAD unchanged, no commit landed" in s, s  # the note text is kept
    assert "files NOT fully restored" in s and "ci.yml is locked" in s and "check git status" in s, s
    assert "; files restored" not in s, s
    # one failing file does not stop the others
    assert rd(repo / DL) == before[DL] and rd(repo / LL) == before[LL]
    assert wg.triggers(rd(repo / WF).decode()) == {"workflow_dispatch"}


def test_a_restore_oserror_after_a_plain_commit_failure_keeps_the_original_error_text():
    root = mk_root()
    repo = mk_project(root, "rs_fail2", {WF: WF_BLOCK_TXT})
    real, real_write, flag = ci.git, Path.write_bytes, {"failed": False}

    def g(r, *args):
        if args[:1] == ("commit",):
            flag["failed"] = True
            return subprocess.CompletedProcess(["git", *args], 1, "", "hook rejected the commit")
        return real(r, *args)

    def write(self, data):
        if flag["failed"] and self.name == "ci.yml":
            raise PermissionError(13, "ci.yml is locked")
        return real_write(self, data)
    with mock.patch.object(ci, "git", g), mock.patch.object(Path, "write_bytes", write):
        row = ci.process(repo, False, False)
    s = row["status"]
    assert s.startswith("ERROR git commit failed") and "hook rejected the commit" in s, s
    assert "files NOT fully restored" in s and "ci.yml is locked" in s, s


# ---------------------------------------------------------------- Ctrl-C mid-run
def _interrupt_run(root, scratch, repo_with_journal):
    """projects a1, b2, c3: a1 finishes, b2 is interrupted by Ctrl-C (a journal exists when asked), c3 is never reached."""
    seen = []

    def process(d, include_deploy, dry, dirty_ok, scr, recover_run):
        seen.append(d.name)
        if d.name == "b2":
            if repo_with_journal:
                (d / ".git" / JOURNAL).write_text("{}", encoding="utf-8")
            raise KeyboardInterrupt()
        return {"repo": d.name, "branch": "main", "files": "w.yml", "trig": "w.yml: push", "status": "CHANGED abc1234"}
    rc, out = _run_main(root, scratch, process=process)
    return rc, out, seen


def _ctrl_c_root():
    root, scratch = hv2.fresh("ctrlc"), hv2.fresh("ctrlc_scratch")
    _fake_projects(root, ["a1", "c3"])
    (root / "b2" / ".github" / "workflows").mkdir(parents=True)
    assert hv2.git(root / "b2", "init", "-q", "-b", "main").rc == 0  # a real repo: journal_path asks git for the common dir
    return root, scratch


def test_ctrl_c_prints_the_rows_so_far_marks_the_interrupted_one_with_a_recover_hint_and_returns_130():
    root, scratch = _ctrl_c_root()
    rc, out, seen = _interrupt_run(root, scratch, repo_with_journal=True)
    rows = _table(out)
    assert rc == 130, rc
    assert seen == ["a1", "b2"], seen
    assert rows["a1"] == "CHANGED abc1234", (rows, out)  # computed before the interrupt: still printed
    assert rows["b2"] == "ERROR interrupted (Ctrl-C); journal kept, run --recover --project b2", (rows, out)
    assert "c3" not in rows, rows
    assert "Interrupted: 2 of 3 project(s) have a row above; the rest were not processed." in out, out


def test_ctrl_c_without_a_journal_has_no_recover_hint():
    root, scratch = _ctrl_c_root()
    rc, out, seen = _interrupt_run(root, scratch, repo_with_journal=False)
    rows = _table(out)
    assert rc == 130 and rows["b2"] == "ERROR interrupted (Ctrl-C)", (rc, rows, out)
    assert "--recover" not in out, out


def test_ctrl_c_hint_survives_a_failing_journal_lookup():
    root, scratch = _ctrl_c_root()
    with mock.patch.object(ci, "journal_path", mock.Mock(side_effect=RuntimeError("git exploded"))):
        rc, out, seen = _interrupt_run(root, scratch, repo_with_journal=False)
    assert rc == 130 and _table(out)["b2"] == "ERROR interrupted (Ctrl-C)", (rc, out)


def main():
    fails = 0
    for name in [n for n, f in list(globals().items()) if n.startswith("test_") and callable(f) and f.__module__ == __name__]:
        try:
            globals()[name]()
            print(f"PASS {name}")
        except BaseException as e:
            fails += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    print("OK: 0 failing" if not fails else f"FAILED: {fails} failing")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
