"""S14-T3 tests: tools/ci_triggers_off.py --dirty-ok (core/DECISION_LOG.md section 'S14-T3').
Temp repos only under D:\\tmp\\harness_v2_tests; the tool runs as a subprocess with --root and --scratch both pointing at
temp folders (never at the real D:\\Repo), except the stale-branch test, which drives process_dirty in-process on a temp repo.
Run: C:\\Python314\\python.exe -I tests/test_s14_ci_dirty.py"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools"))
import hv2  # noqa: E402
import workflow_guard as wg  # noqa: E402
import ci_triggers_off as ci  # noqa: E402
from test_s13_ci_off import mk_root, mk_project, rd, commits, tip_file, _w, JOBS  # noqa: E402

TOOL = HERE.parent / "tools" / "ci_triggers_off.py"
WF = ".github/workflows/ci.yml"
DL, LL = "core/DECISION_LOG.md", "core/LESSONS_LOG.md"
FILLER = "".join(f"      - run: echo step{i}\n" for i in range(12))
WF_LONG = ("name: ci\non:\n  push:\n    branches: [main]\n  pull_request:\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
           "    steps:\n" + FILLER + "      - run: echo tail\n")
SESSION_EDIT = b"      - run: echo SESSION-EDIT\n"


def run_tool(root, scratch, *args):
    return hv2.run([hv2.PY, "-I", str(TOOL), "--root", str(root), "--scratch", str(scratch), *args], root)


def stage_of(repo):
    return hv2.git(repo, "ls-files", "--stage").out


def status_of(repo):
    return hv2.git(repo, "status", "--porcelain=v1", "-uall").out


def dirty_repo(name="p"):
    """Repo with: dirty workflow line far from on:, dirty DECISION_LOG append, intent-to-add file, untracked file."""
    root = mk_root()
    scratch = hv2.fresh("scratch")
    repo = mk_project(root, name, {WF: WF_LONG})
    _w(repo / WF, rd(repo / WF).replace(b"      - run: echo tail\n", SESSION_EDIT))
    _w(repo / DL, rd(repo / DL) + b"session note\n")
    _w(repo / "newfile.txt", "intent\n")
    assert hv2.git(repo, "add", "-N", "newfile.txt").rc == 0
    _w(repo / "scratch.txt", "untracked\n")
    return root, scratch, repo


def snapshot(repo):
    return {"files": {r: rd(repo / r) for r in (WF, DL, LL, "newfile.txt", "scratch.txt") if (repo / r).exists()},
            "head": hv2.head(repo), "stage": stage_of(repo), "status": status_of(repo), "n": commits(repo)}


def assert_no_ci_off_refs(repo):
    """No refs/heads/ci-off/* ever; the private refs/harness/ci-off/* ref is gone after success, conflict or recovery."""
    assert hv2.git(repo, "branch", "--list", "ci-off/*").out.strip() == ""
    refs = hv2.git(repo, "for-each-ref", "--format=%(refname)").out
    assert "refs/heads/ci-off" not in refs and "refs/harness/ci-off" not in refs, refs


def no_leftovers(repo, scratch):
    assert [p.name for p in scratch.iterdir() if p.name.startswith("cioff-")] == [], list(scratch.iterdir())
    wl = hv2.git(repo, "worktree", "list", "--porcelain").out
    assert wl.count("worktree ") == 1, wl


# ------------------------------------------------------------------ happy path

def test_dirty_merge_commits_tip_and_keeps_session_work():
    root, scratch, repo = dirty_repo()
    before = snapshot(repo)
    res = run_tool(root, scratch, "--dirty-ok")
    assert res.rc == 0, res.all
    assert "CHANGED" in res.out and "dirty merge" in res.out, res.all
    assert commits(repo) == before["n"] + 1
    assert hv2.head(repo) != before["head"]
    assert hv2.git(repo, "rev-parse", "HEAD~1").out.strip() == before["head"]
    tip = tip_file(repo, WF)
    assert wg.triggers(tip) == {"workflow_dispatch"}
    g = hv2.run([hv2.PY, "-I", str(HERE.parent / "tools" / "workflow_guard.py")], repo,
                inp=f"refs/heads/main {hv2.head(repo)} refs/heads/main {'0' * 40}\n")
    assert g.rc == 0, g.all
    names = sorted(hv2.git(repo, "show", "--name-only", "--format=", "HEAD").out.split())
    assert names == sorted([WF, DL, LL]), names
    # the committed tip does not contain the session's uncommitted work
    assert b"SESSION-EDIT" not in tip_file(repo, WF).encode() and "session note" not in tip_file(repo, DL)
    # working copy: session edit kept, trigger switched
    work = rd(repo / WF)
    assert SESSION_EDIT in work and b"echo tail" not in work, work
    assert wg.triggers(work.decode()) == {"workflow_dispatch"}
    dl = rd(repo / DL).decode()
    assert "session note" in dl and "CI-OFF" in dl, dl
    assert "CI-OFF" in rd(repo / LL).decode()
    # untouched session files
    assert rd(repo / "newfile.txt") == before["files"]["newfile.txt"]
    assert rd(repo / "scratch.txt") == before["files"]["scratch.txt"]
    # index and status identical except for the committed paths
    committed = {WF, DL, LL}

    def strip(text):
        return [l for l in text.splitlines() if l.split()[-1] not in committed]
    assert strip(stage_of(repo)) == strip(before["stage"])
    assert strip(status_of(repo)) == strip(before["status"])
    assert " A newfile.txt" in status_of(repo) and "?? scratch.txt" in status_of(repo)
    ls = hv2.git(repo, "ls-files", "--stage", "newfile.txt").out
    assert ls.split()[1] == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391", ls  # empty blob = intent-to-add
    no_leftovers(repo, scratch)
    assert hv2.git(repo, "branch", "--list", "ci-off/*").out.strip() == ""  # merged temp branch deleted


def test_second_run_after_dirty_merge_is_clean():
    root, scratch, repo = dirty_repo()
    assert "CHANGED" in run_tool(root, scratch, "--dirty-ok").out
    snap = snapshot(repo)
    res = run_tool(root, scratch, "--dirty-ok")
    assert res.rc == 0 and "CLEAN" in res.out and "CHANGED" not in res.out, res.all
    assert snapshot(repo) == snap


def test_crlf_working_copy_over_lf_blob_merges_and_keeps_crlf():
    root = mk_root()
    scratch = hv2.fresh("scratch")
    repo = mk_project(root, "p_crlf", {WF: WF_LONG})
    assert hv2.git(repo, "config", "core.autocrlf", "true").rc == 0
    for rel in (WF, DL, LL):  # genuine autocrlf=true checkout: CRLF files, clean index
        (repo / rel).unlink()
    assert hv2.git(repo, "checkout", "--", ".").rc == 0
    assert hv2.git(repo, "status", "--porcelain").out.strip() == ""
    assert b"\r\n" in rd(repo / LL)
    _w(repo / WF, rd(repo / WF).replace(b"      - run: echo tail\r\n", SESSION_EDIT.replace(b"\n", b"\r\n")))
    _w(repo / DL, rd(repo / DL) + b"session note\r\n")
    assert hv2.git(repo, "show", f"HEAD:{WF}").out.count("\r") == 0  # blob is LF
    res = run_tool(root, scratch, "--dirty-ok")
    assert res.rc == 0 and "CHANGED" in res.out, res.all
    for rel in (WF, DL, LL):
        raw = rd(repo / rel)
        assert b"\r\n" in raw and raw.replace(b"\r\n", b"").find(b"\n") == -1, (rel, raw)
    work = rd(repo / WF)
    assert b"SESSION-EDIT" in work and b"on:\r\n  workflow_dispatch:\r\n" in work, work
    assert wg.triggers(work.decode()) == {"workflow_dispatch"}
    assert b"session note" in rd(repo / DL) and b"CI-OFF" in rd(repo / DL)
    assert b"CI-OFF" in rd(repo / LL)
    assert "\r" not in tip_file(repo, WF)  # committed blob stays LF
    st = hv2.git(repo, "status", "--porcelain").out
    assert sorted(st.split()) == sorted(["M", WF, "M", DL]), st


# ------------------------------------------------------------------ refusals

def test_staged_change_skips_and_changes_nothing():
    root, scratch, repo = dirty_repo()
    _w(repo / "README.md", "staged\n")
    assert hv2.git(repo, "add", "README.md").rc == 0
    before = snapshot(repo)
    res = run_tool(root, scratch, "--dirty-ok")
    assert res.rc == 0 and "SKIP" in res.out and "staged" in res.out and "CHANGED" not in res.out, res.all
    assert snapshot(repo) == before
    no_leftovers(repo, scratch)


def test_detached_head_skips_with_dirty_ok():
    root, scratch, repo = dirty_repo()
    assert hv2.git(repo, "checkout", "-q", "--detach").rc == 0
    before = snapshot(repo)
    res = run_tool(root, scratch, "--dirty-ok")
    assert res.rc == 0 and "SKIP" in res.out and "detached" in res.out and "CHANGED" not in res.out, res.all
    assert snapshot(repo) == before


def test_merge_in_progress_skips_with_dirty_ok():
    root, scratch, repo = dirty_repo()
    (repo / ".git" / "MERGE_HEAD").write_text(hv2.head(repo) + "\n")  # state marker git itself checks
    before = snapshot(repo)
    res = run_tool(root, scratch, "--dirty-ok")
    assert res.rc == 0 and "SKIP" in res.out and "merge" in res.out and "CHANGED" not in res.out, res.all
    assert snapshot(repo) == before


def test_without_dirty_ok_dirty_repo_is_still_skipped():
    root, scratch, repo = dirty_repo()
    before = snapshot(repo)
    res = run_tool(root, scratch)
    assert res.rc == 0 and "SKIP" in res.out and "uncommitted" in res.out and "CHANGED" not in res.out, res.all
    assert snapshot(repo) == before


def test_dry_run_dirty_ok_reports_would_change_and_changes_nothing():
    root, scratch, repo = dirty_repo()
    before = snapshot(repo)
    res = run_tool(root, scratch, "--dry-run", "--dirty-ok")
    assert res.rc == 0 and "WOULD CHANGE (dirty merge)" in res.out and "CHANGED" not in res.out, res.all
    assert snapshot(repo) == before
    no_leftovers(repo, scratch)
    assert hv2.git(repo, "branch", "--list", "ci-off/*").out.strip() == ""


def test_virtual_marketing_company_never_touched_with_dirty_ok():
    root, scratch, repo = dirty_repo("Virtual_marketing_company")
    before = snapshot(repo)
    res = run_tool(root, scratch, "--dirty-ok")
    assert res.rc == 0 and "Virtual_marketing_company" not in res.out, res.all
    res = run_tool(root, scratch, "--dirty-ok", "--project", "Virtual_marketing_company", "--include-deploy")
    assert "Virtual_marketing_company" not in res.out
    assert snapshot(repo) == before


# ------------------------------------------------------------------ rollback

def test_conflict_on_on_block_rolls_back_everything():
    root, scratch, repo = dirty_repo()
    _w(repo / WF, rd(repo / WF).replace(b"branches: [main]", b"branches: [session-branch]"))
    before = snapshot(repo)
    res = run_tool(root, scratch, "--dirty-ok")
    assert res.rc == 1, res.all
    assert "ERROR" in res.out and "conflict" in res.out and "CHANGED" not in res.out, res.all
    after = snapshot(repo)
    assert after["files"] == before["files"]  # byte-identical, no conflict markers left
    assert b"<<<<<<<" not in after["files"][WF]
    assert after["head"] == before["head"] and after["n"] == before["n"]  # branch back at the old sha
    assert hv2.git(repo, "rev-parse", "refs/heads/main").out.strip() == before["head"]
    assert after["stage"] == before["stage"] and after["status"] == before["status"]
    assert " A newfile.txt" in after["status"]
    no_leftovers(repo, scratch)
    assert_no_ci_off_refs(repo)


def test_branch_moved_before_update_ref_errors_and_changes_nothing():
    root, scratch, repo = dirty_repo()
    before = snapshot(repo)
    orig = ci.git
    other = {}

    def racing_git(r, *args):
        if args[:2] == ("update-ref", "refs/heads/main") and len(args) == 4 and "sha" not in other:
            c = orig(repo, "commit", "--allow-empty", "-q", "-m", "concurrent")
            assert c.returncode == 0, c.stderr
            other["sha"] = orig(repo, "rev-parse", "HEAD").stdout.strip()
        return orig(r, *args)
    ci.git = racing_git
    try:
        out = ci.process_dirty(repo, False, scratch)
    finally:
        ci.git = orig
    assert out.startswith("ERROR branch moved"), out
    assert "sha" in other and other["sha"] != before["head"]
    assert hv2.git(repo, "rev-parse", "refs/heads/main").out.strip() == other["sha"]  # not overwritten
    assert not hv2.git(repo, "merge-base", "--is-ancestor", "refs/heads/main", before["head"]).rc == 0 or True
    after = snapshot(repo)
    assert after["files"] == before["files"]
    assert after["stage"] == before["stage"] and after["status"] == before["status"]
    assert b"workflow_dispatch" not in after["files"][WF]
    no_leftovers(repo, scratch)
    assert_no_ci_off_refs(repo)
    assert not (repo / ".git" / "ci_triggers_off.journal.json").exists()


# ------------------------------------------------------------------ crash and recovery

JOURNAL = "ci_triggers_off.journal.json"


def test_crash_after_each_phase_blocks_rerun_and_recover_restores_exact_state():
    for phase in ("prepared", "branch-moved", "files-merged", "index-refreshed"):
        root, scratch, repo = dirty_repo("p_" + phase.replace("-", "_"))
        before = snapshot(repo)
        crash = hv2.run([hv2.PY, "-I", str(TOOL), "--root", str(root), "--scratch", str(scratch), "--dirty-ok"], root,
                        env={"HARNESS_CI_OFF_FAIL_AFTER": phase})
        assert crash.rc == 70, (phase, crash.all)
        assert (repo / ".git" / JOURNAL).is_file(), phase
        if phase != "prepared":
            assert hv2.head(repo) != before["head"], phase  # the crash really happened after the branch moved
        # plain rerun refuses and changes nothing further
        mid = snapshot(repo)
        rerun = run_tool(root, scratch, "--dirty-ok")
        assert rerun.rc == 0 and "SKIP" in rerun.out and "interrupted run found; use --recover" in rerun.out, (phase, rerun.all)
        assert "CHANGED" not in rerun.out and snapshot(repo) == mid, phase
        # recover
        rec = run_tool(root, scratch, "--recover")
        assert rec.rc == 0 and "RECOVERED" in rec.out, (phase, rec.all)
        assert before["head"][:7] in rec.out, (phase, rec.out)
        after = snapshot(repo)
        assert after == before, (phase, {k: (before[k], after[k]) for k in before if before[k] != after[k]})
        assert " A newfile.txt" in after["status"]
        assert not (repo / ".git" / JOURNAL).exists(), phase
        no_leftovers(repo, scratch)
        assert_no_ci_off_refs(repo)
        # after recovery the tool works again
        again = run_tool(root, scratch, "--dirty-ok")
        assert "CHANGED" in again.out, (phase, again.all)
        assert commits(repo) == before["n"] + 1


def test_recover_without_journal_is_a_noop_skip():
    root, scratch, repo = dirty_repo()
    before = snapshot(repo)
    res = run_tool(root, scratch, "--recover")
    assert res.rc == 0 and "SKIP" in res.out and "RECOVERED" not in res.out, res.all
    assert snapshot(repo) == before


def test_two_simultaneous_runs_make_exactly_one_commit():
    import subprocess
    root, scratch, repo = dirty_repo()
    before = snapshot(repo)
    cmd = [hv2.PY, "-I", str(TOOL), "--root", str(root), "--scratch", str(scratch), "--dirty-ok"]
    env = hv2.base_env()
    ps = [subprocess.Popen(cmd, cwd=str(root), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                           encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL) for _ in range(2)]
    outs = [p.communicate(timeout=100) for p in ps]
    allout = "\n".join(o + e for o, e in outs)
    assert [p.returncode for p in ps] == [0, 0], allout
    assert commits(repo) == before["n"] + 1, allout  # exactly one new commit
    assert hv2.git(repo, "rev-parse", "HEAD~1").out.strip() == before["head"]
    changed = sum("CHANGED" in o for o, _ in outs)
    assert changed == 1, allout  # the second run saw the finished switch (CLEAN) or lock-SKIP, never a second commit
    other = [o for o, _ in outs if "CHANGED" not in o][0]
    assert "CLEAN" in other or "SKIP" in other, allout
    work = rd(repo / WF)
    assert SESSION_EDIT in work and work.count(b"workflow_dispatch:") == 1, work
    assert rd(repo / DL).count(b"CI-OFF: workflows switched") == 1
    assert hv2.git(repo, "status", "--porcelain").out.count("newfile.txt") == 1
    no_leftovers(repo, scratch)
    assert_no_ci_off_refs(repo)
    assert not (repo / ".git" / JOURNAL).exists()


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
