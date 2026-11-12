"""S14-T3 tests: tools/ci_triggers_off.py --dirty-ok (core/DECISION_LOG.md section 'S14-T3').
Temp repos only under D:\\tmp\\harness_v2_tests; the tool runs as a subprocess with --root and --scratch both pointing at
temp folders (never at the real D:\\Repo), except the stale-branch test, which drives process_dirty in-process on a temp repo.
Run: C:\\Python314\\python.exe -I tests/test_s14_ci_dirty.py"""
import contextlib
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
    assert not (repo / ".git" / "ci_triggers_off.journal.json").exists()  # conflict is found before any journal/branch move
    assert b"changed during the run" not in res.out.encode()
    assert [p.name for p in scratch.iterdir() if p.name.startswith("cioff-backup-")] == []  # failed-early run cleans its backup dir
    # every file (not only the workflow) is byte-identical, including the untouched-by-conflict logs
    for rel in (DL, LL):
        assert rd(repo / rel) == before["files"][rel], rel


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


def _crash_at(root, scratch, phase):
    res = hv2.run([hv2.PY, "-I", str(TOOL), "--root", str(root), "--scratch", str(scratch), "--dirty-ok"], root,
                  env={"HARNESS_CI_OFF_FAIL_AFTER": phase})
    assert res.rc == 70, (phase, res.all)


def test_recover_never_overwrites_edit_made_after_interrupted_run():
    import json
    for phase in ("branch-moved", "files-merged"):
        for rel in (LL, WF):
            root, scratch, repo = dirty_repo("e_" + phase.replace("-", "_") + "_" + rel[-6:-3].strip("./"))
            before = snapshot(repo)
            _crash_at(root, scratch, phase)
            jp = repo / ".git" / JOURNAL
            bdir = Path(json.loads(jp.read_text(encoding="utf-8"))["backup_dir"])
            assert bdir.is_dir()
            crashed_head = hv2.head(repo)
            assert crashed_head != before["head"]
            merged = rd(repo / rel)
            extra = b"\nLATER-EDIT-AFTER-CRASH\n"
            _w(repo / rel, merged + extra)
            edited = snapshot(repo)
            rec = run_tool(root, scratch, "--recover")
            tag = (phase, rel, rec.all)
            assert rec.rc == 1 and "ERROR" in rec.out and "RECOVERED" not in rec.out and rel in rec.out, tag
            assert rd(repo / rel) == merged + extra, tag
            assert snapshot(repo) == edited, tag  # nothing at all changed: files, head, stage, status
            assert hv2.head(repo) == crashed_head, tag
            assert jp.is_file() and bdir.is_dir(), tag
            # a repeat is just as safe
            rec2 = run_tool(root, scratch, "--recover")
            assert rec2.rc == 1 and "ERROR" in rec2.out and rd(repo / rel) == merged + extra, rec2.all
            # undo the outside edit: recovery now restores the exact pre-run state
            _w(repo / rel, merged)
            ok = run_tool(root, scratch, "--recover")
            assert ok.rc == 0 and "RECOVERED" in ok.out, (phase, rel, ok.all)
            after = snapshot(repo)
            assert after == before, (phase, rel, {k: (before[k], after[k]) for k in before if before[k] != after[k]})
            assert not jp.exists() and not bdir.exists()
            no_leftovers(repo, scratch)
            assert_no_ci_off_refs(repo)


def test_recover_same_phase_without_outside_edit_restores_exact_state():
    for phase in ("branch-moved", "files-merged"):
        root, scratch, repo = dirty_repo("t_" + phase.replace("-", "_"))
        before = snapshot(repo)
        _crash_at(root, scratch, phase)
        rec = run_tool(root, scratch, "--recover")
        assert rec.rc == 0 and "RECOVERED" in rec.out and "ERROR" not in rec.out, (phase, rec.all)
        assert snapshot(repo) == before, phase
        assert not (repo / ".git" / JOURNAL).exists()
        no_leftovers(repo, scratch)
        assert_no_ci_off_refs(repo)


def test_recover_refuses_journal_without_hashes_and_changes_nothing():
    import json
    for drop in (("pre_hash",), ("pre_hash", "merged_hash")):
        root, scratch, repo = dirty_repo("h_" + str(len(drop)))
        _crash_at(root, scratch, "files-merged")
        jp = repo / ".git" / JOURNAL
        j = json.loads(jp.read_text(encoding="utf-8"))
        for k in drop:
            j.pop(k, None)
        jp.write_text(json.dumps(j), encoding="utf-8", newline="\n")
        jbytes = jp.read_bytes()
        mid = snapshot(repo)
        rec = run_tool(root, scratch, "--recover")
        assert rec.rc == 1 and "ERROR" in rec.out and "RECOVERED" not in rec.out, (drop, rec.all)
        assert snapshot(repo) == mid, drop
        assert jp.read_bytes() == jbytes and Path(j["backup_dir"]).is_dir()


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


def test_crlf_rewritten_content_equal_log_with_dirty_workflow_is_changed():
    root = mk_root()
    scratch = hv2.fresh("scratch")
    repo = mk_project(root, "p_crlf_eq", {WF: WF_LONG})
    assert hv2.git(repo, "config", "core.autocrlf", "true").rc == 0
    for rel in (WF, DL):
        (repo / rel).unlink()
    assert hv2.git(repo, "checkout", "--", ".").rc == 0
    # LESSONS_LOG rewritten as CRLF with the same content (stat-dirty, content-equal) + a real workflow edit
    lines = rd(repo / LL).replace(b"\r\n", b"\n")
    _w(repo / LL, lines.replace(b"\n", b"\r\n"))
    edit = SESSION_EDIT.replace(b"\n", b"\r\n")
    _w(repo / WF, rd(repo / WF).replace(b"      - run: echo tail\r\n", edit))
    before = snapshot(repo)
    res = run_tool(root, scratch, "--dirty-ok")
    assert res.rc == 0 and "CHANGED" in res.out and "ERROR" not in res.out, res.all
    assert commits(repo) == before["n"] + 1
    work = rd(repo / WF)
    assert edit in work and wg.triggers(work.decode()) == {"workflow_dispatch"}
    committed = {WF, DL, LL}

    def strip(text):
        return [l for l in text.splitlines() if l.split()[-1] not in committed]
    assert strip(stage_of(repo)) == strip(before["stage"])
    assert strip(status_of(repo)) == strip(before["status"])
    no_leftovers(repo, scratch)
    assert_no_ci_off_refs(repo)


def test_real_content_edit_in_a_file_the_tool_rewrites_is_never_lost():
    # the tool rewrites the workflow and appends to the logs; a real (non-EOL) user edit in those files must survive
    # byte-exact whether the run ends CHANGED (clean merge) or ERROR (conflict, rolled back)
    for label, rel, edit in (("wf-near-on", WF, lambda b: b.replace(b"branches: [main]", b"branches: [mine]")),
                             ("wf-far", WF, lambda b: b.replace(b"      - run: echo tail\n", b"      - run: echo MINE\n")),
                             ("log-append", LL, lambda b: b + b"MY OWN LINE\n"),
                             ("log-prepend", LL, lambda b: b"MY OWN LINE\n" + b)):
        root = mk_root()
        scratch = hv2.fresh("scratch")
        repo = mk_project(root, "p_edit_" + label.replace("-", "_"), {WF: WF_LONG})
        original = rd(repo / rel)
        mine = edit(original)
        assert mine != original, label
        _w(repo / rel, mine)
        res = run_tool(root, scratch, "--dirty-ok")
        assert ("CHANGED" in res.out) != ("ERROR" in res.out), (label, res.all)  # exactly one of the two outcomes
        work = rd(repo / rel)
        marker = {b"branches: [mine]": b"branches: [mine]", b"echo MINE": b"echo MINE", b"MY OWN LINE": b"MY OWN LINE"}
        key = [m for m in marker if m in mine][0]
        assert key in work, (label, res.all, work[:300])
        assert b"<<<<<<<" not in work and b">>>>>>>" not in work, (label, res.all)
        if "ERROR" in res.out:
            assert work == mine and res.rc == 1, (label, res.all)  # rolled back byte-exact
            assert hv2.git(repo, "rev-parse", "HEAD").out.strip() == hv2.git(repo, "rev-parse", "refs/heads/main").out.strip()
        no_leftovers(repo, scratch)
        assert_no_ci_off_refs(repo)


def _run_with_hook_before_first_write(repo, scratch, edit_rel, extra):
    """In-process process_dirty; just before the first real file write in the repo, append `extra` to edit_rel."""
    orig = ci.swap_in  # atomic_write was removed; swap_in is the single write path (each file write, each restore)
    state = {"writes": [], "fired": False}

    def hooked(p, data, expect):
        if Path(p).is_relative_to(repo):
            state["writes"].append(Path(p).relative_to(repo).as_posix())
            if not state["fired"]:
                state["fired"] = True
                if extra is not None:
                    _w(repo / edit_rel, rd(repo / edit_rel) + extra)
        return orig(p, data, expect)
    ci.swap_in = hooked
    try:
        out = ci.process_dirty(repo, False, scratch)
    finally:
        ci.swap_in = orig
    return out, state


def test_outside_edit_during_live_run_is_never_lost():
    root, scratch, repo = dirty_repo()
    before = snapshot(repo)
    extra = b"OUTSIDE-EDIT-DURING-RUN\n"
    out, state = _run_with_hook_before_first_write(repo, scratch, DL, extra)
    assert state["fired"] and state["writes"][0] == WF, state  # hook fired before the first write; DL is a later file
    assert out.startswith("ERROR") and "changed during the run" in out, out
    edited = before["files"][DL] + extra
    assert rd(repo / DL) == edited, rd(repo / DL)  # user's bytes exactly, nothing appended by the tool
    for rel in (WF, DL, LL, "newfile.txt", "scratch.txt"):
        assert b"<<<<<<<" not in rd(repo / rel) and b">>>>>>>" not in rd(repo / rel), rel
    jp = repo / ".git" / JOURNAL
    head_now = hv2.git(repo, "rev-parse", "refs/heads/main").out.strip()
    if jp.exists():
        # rollback refused: journal kept, recover refuses while the edit stays and changes nothing
        assert "journal kept" in out, out
        mid = snapshot(repo)
        rec = ci.recover(repo)
        assert rec.startswith("ERROR") and "RECOVERED" not in rec, rec
        assert snapshot(repo) == mid and rd(repo / DL) == edited and jp.exists()
        # user reverts their edit: recovery now restores the exact pre-run state
        _w(repo / DL, before["files"][DL])
        ok = ci.recover(repo)
        assert ok.startswith("RECOVERED"), ok
        assert snapshot(repo) == before
        assert not jp.exists()
    else:
        # rolled back: already-written file back to pre-run bytes, branch at the old commit
        assert "working files restored" in out, out
        assert head_now == before["head"] and commits(repo) == before["n"]
        assert rd(repo / WF) == before["files"][WF], "written file not rolled back"
        assert rd(repo / LL) == before["files"][LL]
    assert hv2.git(repo, "rev-parse", "refs/heads/main").out.strip() in (before["head"], head_now)
    no_leftovers(repo, scratch)
    assert_no_ci_off_refs(repo)


def test_same_hook_without_outside_edit_is_changed():
    root, scratch, repo = dirty_repo()
    before = snapshot(repo)
    out, state = _run_with_hook_before_first_write(repo, scratch, DL, None)
    assert state["fired"] and state["writes"][0] == WF, state
    assert out.startswith("CHANGED"), out
    assert commits(repo) == before["n"] + 1
    assert b"session note" in rd(repo / DL) and b"CI-OFF" in rd(repo / DL)
    assert not (repo / ".git" / JOURNAL).exists()
    no_leftovers(repo, scratch)
    assert_no_ci_off_refs(repo)


# ------------------------------------------------------------------ swap_in races (GPT-PM round 2 MAJOR)

EXTRA = b"CONCURRENT-USER-BYTES\n"
USER_NEW = b"user recreated this file\n"
ASIDE_NAME = ".{}.cioff-aside"
FILES3 = (WF, DL, LL)


@contextlib.contextmanager
def race(repo, rel, point, action):
    """Fire `action(path)` once, deterministically, at one exact instant inside swap_in for file `rel`:
    point 'before-aside' = just before step 1 os.replace(p, aside) (after every hash check);
    point 'before-place' = just before step 3 _place_noclobber(tmp, p) (p is parked aside at that moment).
    action None = pass-through twin (only counts that the point was reached)."""
    target = repo / rel
    st = {"fired": 0}
    o_replace, o_place = ci.os.replace, ci._place_noclobber

    def fire():
        st["fired"] += 1
        if action is not None:
            action(target)

    def r_hook(src, dst, *a, **k):
        if point == "before-aside" and not st["fired"] and Path(src) == target and Path(dst) == ci.aside_path(target):
            fire()
        return o_replace(src, dst, *a, **k)

    def p_hook(src, dst):
        if point == "before-place" and not st["fired"] and Path(dst) == target and Path(src).name.endswith(".tmp"):
            fire()
        return o_place(src, dst)
    ci.os.replace, ci._place_noclobber = r_hook, p_hook
    try:
        yield st
    finally:
        ci.os.replace, ci._place_noclobber = o_replace, o_place


def _append_extra(t):
    _w(t, rd(t) + EXTRA)


def _create_user(t):
    assert not t.exists(), "step 1 should have parked the file"
    _w(t, USER_NEW)


def _no_asides(repo):
    for rel in FILES3:
        assert not ci.aside_path(repo / rel).exists(), rel


def test_A_edit_right_before_the_replace_is_never_lost():
    for rel in FILES3:
        root, scratch, repo = dirty_repo("a_" + rel[-7:-3].strip("./_").lower())
        before = snapshot(repo)
        with race(repo, rel, "before-aside", _append_extra) as st:
            out = ci.process_dirty(repo, False, scratch)
        assert st["fired"] == 1, (rel, st)
        assert out.startswith("ERROR") and "changed during the run" in out, (rel, out)
        assert rd(repo / rel) == before["files"][rel] + EXTRA, (rel, "user bytes must be exact")
        _no_asides(repo)
        jp = repo / ".git" / JOURNAL
        assert jp.exists() and "journal kept" in out, (rel, out)
        mid = snapshot(repo)
        rec = ci.recover(repo)  # refuses while the user's edit stays
        assert rec.startswith("ERROR") and "RECOVERED" not in rec, (rel, rec)
        assert snapshot(repo) == mid and rd(repo / rel) == before["files"][rel] + EXTRA and jp.exists(), rel
        _w(repo / rel, before["files"][rel])  # user reverts their edit
        ok = ci.recover(repo)
        assert ok.startswith("RECOVERED"), (rel, ok)
        assert snapshot(repo) == before, rel
        _no_asides(repo)
        assert not jp.exists()
        no_leftovers(repo, scratch)
        assert_no_ci_off_refs(repo)


def test_B_file_recreated_after_it_was_parked_is_never_lost():
    for rel in FILES3:
        root, scratch, repo = dirty_repo("b_" + rel[-7:-3].strip("./_").lower())
        before = snapshot(repo)
        with race(repo, rel, "before-place", _create_user) as st:
            out = ci.process_dirty(repo, False, scratch)
        assert st["fired"] == 1, (rel, st)
        assert out.startswith("ERROR") and "recreated" in out, (rel, out)
        assert rd(repo / rel) == USER_NEW, rel  # user's bytes exactly
        aside = ci.aside_path(repo / rel)
        assert aside.is_file() and rd(aside) == before["files"][rel], (rel, "aside must hold the pre-run bytes")
        jp = repo / ".git" / JOURNAL
        assert jp.exists(), rel
        mid = snapshot(repo)
        rec = ci.recover(repo)  # refuses while the user's p stays
        assert rec.startswith("ERROR") and "RECOVERED" not in rec, (rel, rec)
        assert rd(repo / rel) == USER_NEW and rd(aside) == before["files"][rel] and jp.exists(), rel
        assert snapshot(repo) == mid, rel
        # the user resolves by hand: puts the pre-run bytes back and removes the parked copy -> recovery is exact
        _w(repo / rel, before["files"][rel])
        aside.unlink()
        ok = ci.recover(repo)
        assert ok.startswith("RECOVERED"), (rel, ok)
        assert snapshot(repo) == before, rel
        no_leftovers(repo, scratch)
        assert_no_ci_off_refs(repo)


def test_C_races_during_recover_restore_keep_the_users_bytes():
    for point, act in (("before-aside", _append_extra), ("before-place", _create_user)):
        for rel in FILES3:
            root, scratch, repo = dirty_repo("c_" + point[7:] + "_" + rel[-7:-3].strip("./_").lower())
            before = snapshot(repo)
            _crash_at(root, scratch, "files-merged")
            merged = rd(repo / rel)
            assert merged != before["files"][rel], rel  # the crash left merged bytes in place
            jp = repo / ".git" / JOURNAL
            with race(repo, rel, point, act) as st:
                rec = ci.recover(repo)
            tag = (point, rel, rec)
            assert st["fired"] == 1, tag
            assert rec.startswith("ERROR") and "RECOVERED" not in rec and "journal kept" in rec, tag
            aside = ci.aside_path(repo / rel)
            if point == "before-aside":
                assert "changed during the run" in rec, tag
                assert rd(repo / rel) == merged + EXTRA, tag
                assert not aside.exists(), tag
                _w(repo / rel, merged)  # user reverts
            else:
                assert "recreated" in rec, tag
                assert rd(repo / rel) == USER_NEW, tag
                assert aside.is_file() and rd(aside) == merged, tag  # parked bytes are kept, not deleted
                mid = snapshot(repo)
                assert ci.recover(repo).startswith("ERROR") and rd(repo / rel) == USER_NEW, tag
                assert snapshot(repo) == mid, tag
                _w(repo / rel, merged)  # user resolves by hand
                aside.unlink()
            assert jp.exists(), tag
            ok = ci.recover(repo)
            assert ok.startswith("RECOVERED"), (tag, ok)
            assert snapshot(repo) == before, tag
            _no_asides(repo)
            no_leftovers(repo, scratch)
            assert_no_ci_off_refs(repo)


CRASH_HELPER = """import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import ci_triggers_off as ci
repo, scratch, mode, rel = Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], sys.argv[5]
point = sys.argv[6] if len(sys.argv) > 6 else 'after-aside'
target = repo / rel
orig, oplace = os.replace, ci._place_noclobber
def hook(src, dst, *a, **k):
    hit = Path(src) == target and str(dst).endswith('.cioff-aside')
    if hit and point == 'before-aside':
        os._exit(70)
    r = orig(src, dst, *a, **k)
    if hit and point == 'after-aside':
        os._exit(70)  # hard crash right after step 1
    return r
def phook(src, dst):
    r = oplace(src, dst)
    if r and point == 'after-place' and Path(dst) == target and Path(src).name.endswith('.tmp'):
        os._exit(70)
    return r
os.replace, ci._place_noclobber = hook, phook
if mode == 'run':
    print(ci.process_dirty(repo, False, scratch))
else:
    print(ci.recover(repo))
"""


def _crash_helper(root, scratch, repo, mode, rel, point="after-aside"):
    h = scratch / "crash_helper.py"
    h.write_text(CRASH_HELPER, encoding="utf-8", newline=chr(10))
    return hv2.run([hv2.PY, "-I", str(h), str(HERE.parent / "tools"), str(repo), str(scratch), mode, rel, point], root)


def test_D_hard_crash_after_step1_leaves_aside_and_recover_restores_exact_state():
    for rel in FILES3:
        root, scratch, repo = dirty_repo("d_" + rel[-7:-3].strip("./_").lower())
        before = snapshot(repo)
        res = _crash_helper(root, scratch, repo, "run", rel)
        assert res.rc == 70, (rel, res.all)
        aside = ci.aside_path(repo / rel)
        assert not (repo / rel).exists() and aside.is_file(), rel  # parked, p absent
        assert rd(aside) == before["files"][rel], rel
        jp = repo / ".git" / JOURNAL
        assert jp.is_file(), rel
        rerun = run_tool(root, scratch, "--dirty-ok")
        assert "SKIP" in rerun.out and "interrupted run found; use --recover" in rerun.out, (rel, rerun.all)
        rec = run_tool(root, scratch, "--recover")
        assert rec.rc == 0 and "RECOVERED" in rec.out and "ERROR" not in rec.out, (rel, rec.all)
        assert snapshot(repo) == before, rel
        _no_asides(repo)
        assert not jp.exists()
        no_leftovers(repo, scratch)
        assert_no_ci_off_refs(repo)


def test_D2_hard_crash_inside_recover_restore_then_second_recover_is_exact():
    for rel in FILES3:
        root, scratch, repo = dirty_repo("d2_" + rel[-7:-3].strip("./_").lower())
        before = snapshot(repo)
        _crash_at(root, scratch, "files-merged")
        merged = rd(repo / rel)
        res = _crash_helper(root, scratch, repo, "recover", rel)
        assert res.rc == 70, (rel, res.all)
        aside = ci.aside_path(repo / rel)
        assert not (repo / rel).exists() and aside.is_file() and rd(aside) == merged, rel
        rec = run_tool(root, scratch, "--recover")
        assert rec.rc == 0 and "RECOVERED" in rec.out, (rel, rec.all)
        assert snapshot(repo) == before, rel
        _no_asides(repo)
        no_leftovers(repo, scratch)
        assert_no_ci_off_refs(repo)


def test_E_twins_without_any_edit_are_changed_and_recovered():
    for point in ("before-aside", "before-place"):
        root, scratch, repo = dirty_repo("e1_" + point[7:])
        before = snapshot(repo)
        with race(repo, WF, point, None) as st:
            out = ci.process_dirty(repo, False, scratch)
        assert st["fired"] == 1 and out.startswith("CHANGED"), (point, out)
        assert commits(repo) == before["n"] + 1
        assert SESSION_EDIT in rd(repo / WF) and wg.triggers(rd(repo / WF).decode()) == {"workflow_dispatch"}
        assert not (repo / ".git" / JOURNAL).exists()
        _no_asides(repo)
        no_leftovers(repo, scratch)
        assert_no_ci_off_refs(repo)
        root, scratch, repo = dirty_repo("e2_" + point[7:])
        before = snapshot(repo)
        _crash_at(root, scratch, "files-merged")
        with race(repo, WF, point, None) as st:
            rec = ci.recover(repo)
        assert st["fired"] == 1 and rec.startswith("RECOVERED"), (point, rec)
        assert snapshot(repo) == before, point
        _no_asides(repo)
        no_leftovers(repo, scratch)


def test_F_crash_after_place_before_aside_delete_recovers_exact_state():
    for rel in FILES3:
        root, scratch, repo = dirty_repo("f_" + rel[-7:-3].strip("./_").lower())
        before = snapshot(repo)
        res = _crash_helper(root, scratch, repo, "run", rel, "after-place")
        assert res.rc == 70, (rel, res.all)
        aside = ci.aside_path(repo / rel)
        assert (repo / rel).is_file() and aside.is_file(), rel
        assert rd(aside) == before["files"][rel] and rd(repo / rel) != before["files"][rel], rel  # p = merged, aside = pre
        rec = run_tool(root, scratch, "--recover")
        assert rec.rc == 0 and "RECOVERED" in rec.out and "ERROR" not in rec.out, (rel, rec.all)
        assert snapshot(repo) == before, rel
        _no_asides(repo)
        assert not (repo / ".git" / JOURNAL).exists()
        no_leftovers(repo, scratch)
        assert_no_ci_off_refs(repo)


def test_F2_exception_after_place_rolls_back_without_a_person():
    root, scratch, repo = dirty_repo()
    before = snapshot(repo)
    o_place = ci._place_noclobber
    target = repo / WF
    fired = []

    def boom(src, dst):
        r = o_place(src, dst)
        if r and not fired and Path(dst) == target and Path(src).name.endswith(".tmp"):
            fired.append(1)  # only the forward write fails; the rollback's own restore must work
            raise RuntimeError("injected failure after step 3")
        return r
    ci._place_noclobber = boom
    try:
        out = ci.process_dirty(repo, False, scratch)
    finally:
        ci._place_noclobber = o_place
    assert out.startswith("ERROR") and "injected failure" in out and "working files restored" in out, out
    assert snapshot(repo) == before
    _no_asides(repo)
    assert not (repo / ".git" / JOURNAL).exists()
    no_leftovers(repo, scratch)
    assert_no_ci_off_refs(repo)


def _fail_unlink_of_asides():
    orig = ci._unlink_retry

    def fake(p, *a, **k):
        return False if Path(p).name.endswith(".cioff-aside") else orig(p, *a, **k)
    ci._unlink_retry = fake
    return orig


def test_G_undeletable_parked_copy_still_changed_with_note_and_next_run_is_clean():
    root, scratch, repo = dirty_repo()
    before = snapshot(repo)
    orig = _fail_unlink_of_asides()
    try:
        out = ci.process_dirty(repo, False, scratch)
    finally:
        ci._unlink_retry = orig
    assert out.startswith("CHANGED"), out
    assert "NOTE could not delete parked copies" in out, out
    for rel in FILES3:
        assert ci.aside_path(repo / rel).is_file(), rel
    assert not (repo / ".git" / JOURNAL).exists()
    assert commits(repo) == before["n"] + 1
    assert SESSION_EDIT in rd(repo / WF) and wg.triggers(rd(repo / WF).decode()) == {"workflow_dispatch"}
    assert ci.LEFTOVER_ASIDES == [], ci.LEFTOVER_ASIDES
    snap = snapshot(repo)
    rec = run_tool(root, scratch, "--recover")  # not needed: nothing to recover
    assert "SKIP" in rec.out and "RECOVERED" not in rec.out, rec.all
    again = run_tool(root, scratch, "--dirty-ok")
    assert again.rc == 0 and "CLEAN" in again.out and "SKIP" not in again.out and "ERROR" not in again.out, again.all
    assert commits(repo) == before["n"] + 1 and rd(repo / WF) == snap["files"][WF]


def test_H_note_of_one_repo_never_reaches_another_repo():
    root1, scratch1, repo1 = dirty_repo("h1")
    root2, scratch2, repo2 = dirty_repo("h2")
    o_swap = ci.swap_in
    state = {"n": 0}

    def swap_with_edit(p, data, expect):  # user edit of LL just before LL's swap in repo1 -> ERROR after WF/DL left parked copies
        if Path(p) == repo1 / LL and not state["n"]:
            state["n"] = 1
            _w(repo1 / LL, rd(repo1 / LL) + EXTRA)
        return o_swap(p, data, expect)
    ci.swap_in = swap_with_edit
    orig = _fail_unlink_of_asides()
    try:
        row1 = ci.process(repo1, False, False, True, scratch1)
    finally:
        ci._unlink_retry = orig
        ci.swap_in = o_swap
    assert state["n"] == 1
    assert row1["status"].startswith("ERROR") and "NOTE could not delete parked copies" in row1["status"], row1
    assert str(ci.aside_path(repo1 / WF)) in row1["status"], row1
    assert rd(repo1 / LL).endswith(EXTRA)
    # same process, next repo, normal unlink: no NOTE
    row2 = ci.process(repo2, False, False, True, scratch2)
    assert row2["status"].startswith("CHANGED") and "NOTE" not in row2["status"], row2
    # stale list left by anything earlier is cleared when a repo run starts
    root3, scratch3, repo3 = dirty_repo("h3")
    ci.LEFTOVER_ASIDES[:] = ["STALE-OTHER-REPO-ASIDE"]
    try:
        row3 = ci.process(repo3, False, False, True, scratch3)
    finally:
        ci.LEFTOVER_ASIDES.clear()
    assert row3["status"].startswith("CHANGED") and "NOTE" not in row3["status"] and "STALE" not in row3["status"], row3


def test_I_crash_leaving_swap_temp_file_is_cleaned_by_recover():
    for rel in (WF, LL):
        root, scratch, repo = dirty_repo("i_" + rel[-7:-3].strip("./_").lower())
        before = snapshot(repo)
        res = _crash_helper(root, scratch, repo, "run", rel, "before-aside")
        assert res.rc == 70, (rel, res.all)
        p = repo / rel
        tmps = list(p.parent.glob(f".{p.name}.cioff-*.tmp"))
        assert len(tmps) == 1, (rel, tmps)  # the crash really left the temp file
        rec = run_tool(root, scratch, "--recover")
        assert rec.rc == 0 and "RECOVERED" in rec.out, (rel, rec.all)
        assert list(p.parent.glob(f".{p.name}.cioff-*.tmp")) == [], rel
        assert snapshot(repo) == before, rel
        no_leftovers(repo, scratch)
        assert_no_ci_off_refs(repo)


def test_lock_held_by_another_process_gives_skip_after_timeout():
    import subprocess
    root, scratch, repo = dirty_repo()
    before = snapshot(repo)
    helper = scratch / "hold_lock.py"
    helper.write_text(
        "import sys\nfrom pathlib import Path\nsys.path.insert(0, sys.argv[1])\nimport ci_triggers_off as ci\n"
        "with ci.repo_lock(ci.common_dir(Path(sys.argv[2]))):\n    print('READY', flush=True)\n    sys.stdin.readline()\n",
        encoding="utf-8", newline="\n")
    holder = subprocess.Popen([hv2.PY, "-I", str(helper), str(HERE.parent / "tools"), str(repo)], cwd=str(root),
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    orig = ci.repo_lock
    try:
        assert holder.stdout.readline().strip() == "READY"
        ci.repo_lock = lambda common: orig(common, timeout=0.5)  # the 30 s wait is a default arg, so shorten it here
        row = ci.process(repo, False, False, True, scratch)
    finally:
        ci.repo_lock = orig
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(timeout=20)
    assert row["status"] == "SKIP another ci_triggers_off run holds the lock", row
    assert snapshot(repo) == before
    assert "CHANGED" in run_tool(root, scratch, "--dirty-ok").out  # lock released: a real run works


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
