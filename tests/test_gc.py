"""Criterion 7: worktree_gc (contract at 6a6de90)."""
import importlib.util
import os
import shutil
import stat
import sys
from pathlib import Path

_s = importlib.util.spec_from_file_location("hv2", Path(__file__).resolve().parent / "hv2.py")
h = importlib.util.module_from_spec(_s)
sys.modules["hv2"] = h
_s.loader.exec_module(h)

AGE0 = "0"


def _env(scratch, **kw):
    return {"HARNESS_SCRATCH_ROOT": str(scratch), "HARNESS_WORKTREE_MIN_AGE_H": AGE0, **kw}


def _gc(main, scratch, **kw):
    return h.tool(main, "worktree_gc.py", env=_env(scratch, **kw))


def _setup(name):
    main = h.mk_repo(name, with_commit=False)
    h.commit(main, ".gitignore", "*.log\n", "ignore logs")
    h.commit(main, "a.txt", "a\n", "a")
    return main, h.fresh(name + "_scratch"), h.fresh(name + "_outside")


def _wt(main, path, branch):
    r = h.git(main, "worktree", "add", "-q", "-b", branch, str(path))
    assert r.rc == 0, r.all


def test_gc_removes_only_merged_clean_worktrees_under_scratch_root():
    main, scratch, outside = _setup("gc_all")
    merged, unmerged, ignored, out = scratch / "merged", scratch / "unmerged", scratch / "ignored", outside / "wt"
    for p, b in ((merged, "b-merged"), (unmerged, "b-unmerged"), (ignored, "b-ignored"), (out, "b-out")):
        _wt(main, p, b)
    h.commit(unmerged, "new.txt", "n\n", "unmerged work")
    (ignored / "junk.log").write_text("x", encoding="utf-8")
    res = _gc(main, scratch)
    assert res.rc == 0, res.all
    assert not merged.exists(), "merged clean worktree must be removed"
    assert unmerged.exists() and "not merged" in res.out
    assert ignored.exists() and "ignored" in res.out and (ignored / "junk.log").exists()
    assert out.exists() and (out / "a.txt").exists(), "worktree outside the root was touched"
    listing = h.git(main, "worktree", "list").out
    assert "b-merged" not in listing and "b-unmerged" in listing and "b-out" in listing


def test_gc_keeps_worktree_with_uncommitted_change():
    main, scratch, _ = _setup("gc_dirty")
    wt = scratch / "dirty"
    _wt(main, wt, "b-dirty")
    (wt / "a.txt").write_text("changed\n", encoding="utf-8")
    res = _gc(main, scratch)
    assert res.rc == 0 and wt.exists() and (wt / "a.txt").read_text(encoding="utf-8") == "changed\n", res.all


def test_gc_kill_switch_off_removes_nothing():
    main, scratch, _ = _setup("gc_off")
    wt = scratch / "m"
    _wt(main, wt, "b-m")
    assert _gc(main, scratch, HARNESS_WORKTREE_GC="off").rc == 0 and wt.exists()


def test_gc_never_removes_the_main_checkout_even_if_root_is_its_parent():
    main, _, _ = _setup("gc_main")
    res = _gc(main, main.parent)
    assert res.rc == 0 and (main / "a.txt").exists() and (main / ".git").exists(), res.all


def test_gc_keeps_fresh_worktree_at_default_branch_tip_by_default():
    main, scratch, _ = _setup("gc_young")
    wt = scratch / "young"
    _wt(main, wt, "b-young")
    res = h.tool(main, "worktree_gc.py", env={"HARNESS_SCRATCH_ROOT": str(scratch)})  # default min age 2 h
    assert res.rc == 0 and wt.exists() and "younger" in res.out, res.all
    assert _gc(main, scratch).rc == 0 and not wt.exists(), "with min age 0 the same worktree is removed"


def test_gc_removes_young_worktree_that_has_own_merged_commits():
    main, scratch, _ = _setup("gc_own")
    wt = scratch / "own"
    _wt(main, wt, "b-own")
    h.commit(wt, "own.txt", "o\n", "own work")
    assert h.git(main, "merge", "-q", "--no-ff", "-m", "merge own", "b-own").rc == 0
    h.commit(main, "later.txt", "l\n", "tip moves on")  # worktree HEAD is now an ancestor, not the tip
    res = h.tool(main, "worktree_gc.py", env={"HARNESS_SCRATCH_ROOT": str(scratch)})  # default min age
    assert res.rc == 0 and not wt.exists(), res.all


def test_gc_skips_missing_folder_without_crashing():
    main, scratch, _ = _setup("gc_missing")
    gone, ok = scratch / "gone", scratch / "ok"
    _wt(main, gone, "b-gone")
    _wt(main, ok, "b-ok")

    def _rw(fn, p, *_):
        os.chmod(p, stat.S_IWRITE)
        fn(p)
    shutil.rmtree(gone, onexc=_rw)
    res = _gc(main, scratch)
    assert res.rc == 0, res.all
    assert "Traceback" not in res.all and not ok.exists(), res.all


def test_gc_returns_1_when_a_git_call_fails():
    main, scratch, _ = _setup("gc_gitfail")
    wt = scratch / "broken"
    _wt(main, wt, "b-broken")
    gitfile = wt / ".git"
    os.chmod(gitfile, stat.S_IWRITE)
    gitfile.unlink()
    gitfile.write_text("gitdir: D:/nonexistent/gitdir\n", encoding="utf-8")
    res = _gc(main, scratch)
    assert res.rc == 1, f"expected exit 1 for a failed git status, got {res.rc}: {res.all}"
    assert wt.exists()
    notrepo = h.fresh("gc_notrepo")
    assert h.tool(notrepo, "worktree_gc.py", env=_env(scratch)).rc == 1


def test_gc_default_branch_master_fallback():
    main = h.fresh("gc_master")
    assert h.git(main, "init", "-q", "-b", "master").rc == 0
    h.git(main, "config", "user.name", "t")
    h.git(main, "config", "user.email", "t@t")
    h.commit(main, "a.txt", "a\n", "a")
    scratch = h.fresh("gc_master_scratch")
    wt = scratch / "m"
    _wt(main, wt, "b-m")
    res = _gc(main, scratch)
    assert res.rc == 0 and not wt.exists(), res.all


def test_gc_default_branch_from_origin_head_wins_over_main():
    main = h.mk_repo("gc_origin")
    bare = h.fresh("gc_origin_remote")
    assert h.git(bare, "init", "-q", "--bare", "-b", "main").rc == 0
    h.git(main, "remote", "add", "origin", str(bare))
    assert h.git(main, "checkout", "-q", "-b", "develop").rc == 0
    h.commit(main, "dev.txt", "d\n", "develop only")
    assert h.git(main, "push", "-q", "origin", "main", "develop").rc == 0
    assert h.git(main, "fetch", "-q", "origin").rc == 0
    assert h.git(main, "remote", "set-head", "origin", "develop").rc == 0
    assert h.git(main, "checkout", "-q", "main").rc == 0
    scratch = h.fresh("gc_origin_scratch")
    wt = scratch / "dev"
    _wt(main, wt, "b-dev")  # branches from main, so it is merged into main already
    h.git(wt, "reset", "-q", "--hard", "develop")  # now merged only into develop / origin/develop
    res = _gc(main, scratch)
    assert res.rc == 0 and not wt.exists(), f"origin/HEAD (develop) must be the default branch: {res.all}"


def test_gc_without_any_default_branch_exits_1_and_removes_nothing():
    main = h.fresh("gc_nodefault")
    assert h.git(main, "init", "-q", "-b", "trunk").rc == 0
    h.git(main, "config", "user.name", "t")
    h.git(main, "config", "user.email", "t@t")
    h.commit(main, "a.txt", "a\n", "a")
    scratch = h.fresh("gc_nodefault_scratch")
    wt = scratch / "w"
    _wt(main, wt, "b-w")
    res = _gc(main, scratch)
    assert res.rc == 1 and wt.exists(), res.all


if __name__ == "__main__":
    sys.exit(h.run_all(globals()))
