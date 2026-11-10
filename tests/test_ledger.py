"""Criteria 1 and 2: ledger state machine, location/lock, approval binding (contract at 6a6de90)."""
import importlib.util
import re
import subprocess
import sys
import time
from pathlib import Path

_s = importlib.util.spec_from_file_location("hv2", Path(__file__).resolve().parent / "hv2.py")
h = importlib.util.module_from_spec(_s)
sys.modules["hv2"] = h
_s.loader.exec_module(h)


def _popen(r, *args):
    return subprocess.Popen([h.PY, "-I", str(h.TOOLS / "ledger.py"), *args], cwd=str(r), env=h.base_env(),
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _implemented(name, risk="local"):
    r = h.mk_repo(name)
    h.setup_tasks(r, 2, risk)
    h.commit(r, "a.py", "x=1\n", "x", "Task: S1-T1")
    h.ledger(r, "commit-hook")
    assert h.task(r, "S1-T1")["state"] == "implemented"
    return r


def _pushcheck(r, *shas, env=None):
    zero = "0" * 40
    lines = "".join(f"refs/heads/main {s} refs/heads/main {zero}\n" for s in shas)
    return h.ledger(r, "check-approval", inp=lines, env=env)


# ---- criterion 1 ---------------------------------------------------------------

def test_task_ids_sequential_and_duplicates_refused():
    r = h.mk_repo("led_ids")
    assert h.ledger(r, "sprint-add", "--id", "S1", "--title", "One").rc == 0
    assert h.ledger(r, "task-add", "--title", "a").rc == 0
    assert h.ledger(r, "task-add", "--title", "b").rc == 0
    ids = [t["id"] for t in h.load_ledger(r)["sprints"][0]["tasks"]]
    assert ids == ["S1-T1", "S1-T2"], ids
    assert h.ledger(r, "task-add", "--id", "S1-T1", "--title", "dup").rc != 0, "duplicate task id accepted"
    assert len(h.load_ledger(r)["sprints"][0]["tasks"]) == 2
    assert h.ledger(r, "sprint-add", "--id", "S1", "--title", "again").rc != 0, "duplicate sprint accepted"
    assert len(h.load_ledger(r)["sprints"]) == 1


def test_task_add_without_sprint_refused():
    r = h.mk_repo("led_nosprint")
    assert h.ledger(r, "task-add", "--title", "a").rc != 0


def test_task_add_unknown_sprint_refused_and_run_field_stored():
    r = h.mk_repo("led_sprint_unknown")
    h.ledger(r, "sprint-add", "--id", "S1", "--title", "t")
    bad = h.ledger(r, "task-add", "--sprint", "S9", "--title", "x")
    assert bad.rc != 0 and h.load_ledger(r)["sprints"][0]["tasks"] == []
    assert h.ledger(r, "task-add", "--sprint", "S1", "--title", "x", "--run", "python app.py --serve").rc == 0
    assert h.task(r, "S1-T1")["run"] == "python app.py --serve"


def test_start_refuses_second_task_while_one_in_progress():
    r = h.mk_repo("led_start")
    h.setup_tasks(r)
    assert h.ledger(r, "start", "S1-T1").rc == 0
    assert h.task(r, "S1-T1")["state"] == "in_progress"
    assert h.ledger(r, "start", "S1-T2").rc != 0, "second task started while S1-T1 in progress"
    assert h.task(r, "S1-T2")["state"] == "todo"
    assert h.task(r, "S1-T1")["state"] == "in_progress"


def test_start_allowed_again_after_first_leaves_in_progress():
    r = h.mk_repo("led_start2")
    h.setup_tasks(r)
    h.ledger(r, "start", "S1-T1")
    h.commit(r, "a.py", "x=1\n", "work", "Task: S1-T1")
    h.ledger(r, "commit-hook")
    assert h.task(r, "S1-T1")["state"] == "implemented"
    assert h.ledger(r, "start", "S1-T2").rc == 0


def test_start_refuses_verified_and_done_tasks():
    r = _implemented("led_start_done")
    h.ledger(r, "verify", "S1-T1", "--result", "pass", "--evidence", "e")
    assert h.ledger(r, "start", "S1-T1").rc != 0, "verified"
    assert h.task(r, "S1-T1")["state"] == "verified"
    h.ledger(r, "review", "S1-T1", "--result", "accepted")
    assert h.ledger(r, "start", "S1-T1").rc != 0, "done"
    assert h.task(r, "S1-T1")["state"] == "done"


def test_commit_hook_task_line_moves_to_implemented_and_records_sha():
    r = h.mk_repo("led_commit")
    h.setup_tasks(r)
    sha = h.commit(r, "a.py", "x=1\n", "Implement thing", "Task: S1-T1")
    out = h.ledger(r, "commit-hook")
    assert out.rc == 0, out.all
    t = h.task(r, "S1-T1")
    assert t["state"] == "implemented", t["state"]
    assert t["commits"] == [sha], t["commits"]
    assert h.task(r, "S1-T2")["state"] == "todo"
    h.ledger(r, "commit-hook")  # twice must not duplicate the sha
    assert h.task(r, "S1-T1")["commits"] == [sha]


def test_commit_hook_no_task_line_changes_nothing():
    r = h.mk_repo("led_commit_none")
    h.setup_tasks(r)
    h.commit(r, "a.py", "x=1\n", "Plain commit")
    assert h.ledger(r, "commit-hook").rc == 0
    assert h.task(r, "S1-T1")["state"] == "todo" and h.task(r, "S1-T1")["commits"] == []
    h.commit(r, "b.py", "x=1\n", "mentions Task: S1-T1 inline only")
    assert h.ledger(r, "commit-hook").rc == 0
    assert h.task(r, "S1-T1")["state"] == "todo", "inline mention must not count as a Task: line"


def test_commit_hook_unknown_task_records_visible_hook_error():
    r = h.mk_repo("led_commit_unknown")
    h.setup_tasks(r)
    h.commit(r, "a.py", "x=1\n", "x", "Task: S9-T9")
    res = h.ledger(r, "commit-hook")
    assert "HARNESS HOOK ERROR" in res.err, res.all
    assert [t["id"] for t in h.load_ledger(r)["sprints"][0]["tasks"]] == ["S1-T1", "S1-T2"]
    for tid in ("S1-T1", "S1-T2"):
        assert h.task(r, tid)["state"] == "todo" and h.task(r, tid)["commits"] == []
    assert any(e["kind"] == "hook-error" and "S9-T9" in e["message"] for e in h.load_ledger(r)["events"])
    assert "S9-T9" in (r / ".harness" / "hook-error.log").read_text(encoding="utf-8")
    assert "hook error" in h.ledger(r, "status").out.lower()


def test_verify_pass_plus_review_accepted_is_done_either_order():
    r = _implemented("led_done_a")
    assert h.ledger(r, "verify", "S1-T1", "--result", "pass", "--evidence", "e").rc == 0
    assert h.task(r, "S1-T1")["state"] == "verified"
    assert h.ledger(r, "review", "S1-T1", "--result", "accepted").rc == 0
    assert h.task(r, "S1-T1")["state"] == "done"
    r2 = _implemented("led_done_b")
    h.ledger(r2, "review", "S1-T1", "--result", "accepted")
    assert h.task(r2, "S1-T1")["state"] != "done", "review alone must not complete a task"
    h.ledger(r2, "verify", "S1-T1", "--result", "pass", "--evidence", "e")
    assert h.task(r2, "S1-T1")["state"] == "done"


def test_verify_pass_alone_not_done_for_local_but_done_for_trivial():
    r = _implemented("led_alone")
    h.ledger(r, "verify", "S1-T1", "--result", "pass", "--evidence", "e")
    assert h.task(r, "S1-T1")["state"] == "verified"
    rt = _implemented("led_trivial", risk="trivial")
    h.ledger(rt, "verify", "S1-T1", "--result", "pass", "--evidence", "e")
    assert h.task(rt, "S1-T1")["state"] == "done"


def test_verify_pass_with_review_changes_is_not_done():
    r = _implemented("led_changes")
    h.ledger(r, "review", "S1-T1", "--result", "changes", "--findings", "2")
    h.ledger(r, "verify", "S1-T1", "--result", "pass", "--evidence", "e")
    assert h.task(r, "S1-T1")["state"] == "verified"


def test_verify_fail_is_failed_even_after_accepted_review():
    r = _implemented("led_fail")
    h.ledger(r, "review", "S1-T1", "--result", "accepted")
    assert h.ledger(r, "verify", "S1-T1", "--result", "fail", "--evidence", "red").rc == 0
    assert h.task(r, "S1-T1")["state"] == "failed"
    assert h.task(r, "S1-T1")["verify"]["evidence"] == "red"


def test_verify_not_run_is_not_run_state_not_failed():
    r = _implemented("led_notrun")
    assert h.ledger(r, "verify", "S1-T1", "--result", "NOT_RUN", "--evidence", "no env").rc == 0
    assert h.task(r, "S1-T1")["state"] == "not_run"
    h.ledger(r, "review", "S1-T1", "--result", "accepted")
    assert h.task(r, "S1-T1")["state"] == "not_run", "an accepted review must not complete untested work"
    assert h.ledger(r, "start", "S1-T1").rc == 0, "a not_run task can be picked up again"


def test_verify_and_review_require_a_task_commit():
    r = h.mk_repo("led_nocommit")
    h.setup_tasks(r)
    assert h.ledger(r, "verify", "S1-T1", "--result", "pass", "--evidence", "e").rc != 0
    assert h.ledger(r, "review", "S1-T1", "--result", "accepted").rc != 0
    t = h.task(r, "S1-T1")
    assert t["state"] == "todo" and t["verify"] is None and t["review"] is None, t


def test_new_task_commit_after_evidence_returns_task_to_implemented():
    r = _implemented("led_stale")
    h.ledger(r, "verify", "S1-T1", "--result", "pass", "--evidence", "e")
    assert h.task(r, "S1-T1")["state"] == "verified"
    h.commit(r, "b.py", "b = 1\n", "more", "Task: S1-T1")
    h.ledger(r, "commit-hook")
    assert h.task(r, "S1-T1")["state"] == "implemented", "verify belongs to the old commit"
    h.ledger(r, "verify", "S1-T1", "--result", "pass", "--evidence", "e2")
    h.ledger(r, "review", "S1-T1", "--result", "accepted")
    assert h.task(r, "S1-T1")["state"] == "done"
    h.commit(r, "c.py", "c = 1\n", "again", "Task: S1-T1")
    h.ledger(r, "commit-hook")
    assert h.task(r, "S1-T1")["state"] == "implemented"
    assert len(h.task(r, "S1-T1")["commits"]) == 3
    h.ledger(r, "verify", "S1-T1", "--result", "pass", "--evidence", "e3")
    assert h.task(r, "S1-T1")["state"] == "verified", "the accepted review is for an older commit"
    h.ledger(r, "review", "S1-T1", "--result", "accepted")
    assert h.task(r, "S1-T1")["state"] == "done"


def test_review_changes_on_done_task_returns_to_verified():
    r = _implemented("led_done_changes")
    h.ledger(r, "verify", "S1-T1", "--result", "pass", "--evidence", "e")
    h.ledger(r, "review", "S1-T1", "--result", "accepted")
    assert h.task(r, "S1-T1")["state"] == "done"
    h.ledger(r, "review", "S1-T1", "--result", "changes", "--findings", "1")
    assert h.task(r, "S1-T1")["state"] == "verified"


def test_trivial_task_new_commit_needs_new_verify():
    r = _implemented("led_trivial_stale", risk="trivial")
    h.ledger(r, "verify", "S1-T1", "--result", "pass", "--evidence", "e")
    assert h.task(r, "S1-T1")["state"] == "done"
    h.commit(r, "b.py", "b = 1\n", "more", "Task: S1-T1")
    h.ledger(r, "commit-hook")
    assert h.task(r, "S1-T1")["state"] == "implemented"


def test_status_prints_done_n_of_m():
    r = _implemented("led_status")
    out = h.ledger(r, "status").out
    assert re.search(r"done 0 of 2", out), out
    h.ledger(r, "verify", "S1-T1", "--result", "pass", "--evidence", "e")
    h.ledger(r, "review", "S1-T1", "--result", "accepted")
    out = h.ledger(r, "status").out
    assert re.search(r"done 1 of 2", out), out
    assert "S1-T1" in out and "S1-T2" in out


# ---- location and lock -----------------------------------------------------------

def test_harness_ledger_env_redirects_storage():
    r = h.mk_repo("led_env")
    alt = h.fresh("led_env_alt") / "x" / "ledger.json"
    assert h.ledger(r, "sprint-add", "--id", "S1", "--title", "t", env={"HARNESS_LEDGER": str(alt)}).rc == 0
    assert alt.exists() and not (r / ".harness" / "ledger.json").exists()


def test_ledger_default_location_is_shared_by_linked_worktree():
    r = h.mk_repo("led_wt")
    h.setup_tasks(r, 1)
    wt = h.fresh("led_wt_linked") / "wt"
    assert h.git(r, "worktree", "add", "-q", "-b", "feat", str(wt)).rc == 0
    st = h.ledger(wt, "status")
    assert st.rc == 0 and "S1-T1" in st.out, st.all
    assert h.ledger(wt, "task-add", "--title", "from worktree").rc == 0
    assert not (wt / ".harness").exists(), "worktree must not get its own ledger"
    assert [t["id"] for t in h.load_ledger(r)["sprints"][0]["tasks"]] == ["S1-T1", "S1-T2"]
    sha = h.commit(wt, "w.py", "w = 1\n", "in worktree", "Task: S1-T2")
    assert h.ledger(wt, "commit-hook").rc == 0
    assert h.task(r, "S1-T2")["commits"] == [sha]


def test_concurrent_writers_do_not_lose_updates_and_lock_is_released():
    r = h.mk_repo("led_conc")
    h.ledger(r, "sprint-add", "--id", "S1", "--title", "t")
    procs = [_popen(r, "task-add", "--title", f"t{i}") for i in range(8)]
    outs = [p.communicate(timeout=90) for p in procs]
    rcs = [p.returncode for p in procs]
    assert rcs == [0] * 8, (rcs, [o[1].decode("utf-8", "replace")[-400:] for o in outs if o[1]])
    ids = sorted(t["id"] for t in h.load_ledger(r)["sprints"][0]["tasks"])
    assert ids == sorted(f"S1-T{i}" for i in range(1, 9)), ids
    assert not (r / ".harness" / "ledger.lock").exists()


def test_writer_waits_for_held_lock_file():
    r = h.mk_repo("led_lock")
    h.ledger(r, "sprint-add", "--id", "S1", "--title", "t")
    lock = r / ".harness" / "ledger.lock"
    lock.write_text("999999", encoding="utf-8")
    p = _popen(r, "task-add", "--title", "x")
    time.sleep(2.0)
    assert p.poll() is None, "writer ignored the held lock"
    assert len(h.load_ledger(r)["sprints"][0]["tasks"]) == 0
    lock.unlink()
    assert p.wait(timeout=30) == 0
    assert len(h.load_ledger(r)["sprints"][0]["tasks"]) == 1 and not lock.exists()


# ---- criterion 2: approval -------------------------------------------------------

def test_approve_binds_head_and_goes_stale_after_new_commit():
    r = h.mk_repo("led_approve")
    head = h.head(r)
    nap = h.ledger(r, "check-approval")
    assert nap.rc != 0 and "approved: none" in nap.err, nap.all
    assert h.ledger(r, "approve").rc != 0, "approve without --evidence must be refused"
    assert h.ledger(r, "approve", "--evidence", "gpt-pm reply 17").rc == 0
    ap = h.load_ledger(r)["approval"]
    assert ap["head"] == head and ap["evidence"] == "gpt-pm reply 17", ap
    ok = h.ledger(r, "check-approval")
    assert ok.rc == 0, ok.all
    h.commit(r, "more.py", "m = 1\n", "one more")
    assert h.ledger(r, "check-approval").rc != 0, "approval must not carry over to a new code HEAD"


def test_approve_head_resolves_short_sha_and_rejects_unknown():
    r = h.mk_repo("led_approve_head")
    first = h.head(r)
    h.commit(r, "more.py", "m = 1\n", "second")
    assert h.ledger(r, "approve", "--head", first[:8], "--evidence", "e").rc == 0
    assert h.load_ledger(r)["approval"]["head"] == first, "short sha must be stored as the full sha"
    assert h.ledger(r, "approve", "--head", "deadbeefdeadbeef", "--evidence", "e").rc != 0
    assert h.load_ledger(r)["approval"]["head"] == first, "failed approve must not change the approval"


def test_check_approval_stdin_ancestor_descendant_and_docs_only():
    r = h.mk_repo("led_stdin")
    c1 = h.head(r)
    c2 = h.commit(r, "a.py", "a = 1\n", "c2")
    assert h.ledger(r, "approve", "--evidence", "e").rc == 0  # approved = c2
    c3_doc = h.commit(r, "NOTES.md", "n\n", "docs only")
    c4_code = h.commit(r, "b.py", "b = 1\n", "code")
    assert _pushcheck(r, c2).rc == 0, "the approved sha itself"
    assert _pushcheck(r, c1).rc == 0, "ancestor of the approved sha"
    assert _pushcheck(r, c3_doc).rc == 0, "docs-only descendant"
    blocked = _pushcheck(r, c4_code)
    assert blocked.rc != 0 and "push blocked" in blocked.err, blocked.all
    assert _pushcheck(r, c2, c4_code).rc != 0, "one bad ref among good ones must block"
    assert _pushcheck(r, c1, c2, c3_doc).rc == 0
    assert h.ledger(r, "check-approval").rc != 0, "no stdin checks HEAD (c4_code is not covered)"


def test_check_approval_agent_rule_files_and_mixed_diffs_are_not_docs():
    r = h.mk_repo("led_stdin_agents")
    assert h.ledger(r, "approve", "--evidence", "e").rc == 0
    ok = h.commit(r, "notes/readme.md", "x\n", "plain md")
    assert _pushcheck(r, ok).rc == 0
    bad = h.commit(r, "harness/agents/new.md", "---\nname: x\n---\n", "agent file")
    assert _pushcheck(r, bad).rc != 0, "harness/agents/*.md changes behavior, it is not documentation"
    for d in ("skills", "commands", "rules"):
        sha = h.commit(r, f"harness/{d}/x.md", "x\n", f"{d} file")
        assert _pushcheck(r, sha).rc != 0, d
    h.commit(r, "c.md", "c\n", "md")
    h.commit(r, "d.py", "d = 1\n", "code")
    assert _pushcheck(r, h.head(r)).rc != 0


def test_check_approval_checks_every_commit_not_the_net_diff():
    r = h.mk_repo("led_stdin_revert")
    a = h.head(r)
    assert h.ledger(r, "approve", "--evidence", "e").rc == 0
    b = h.commit(r, "src/payment.py", "pay = 1\n", "B: code change")
    assert _pushcheck(r, b).rc != 0
    assert h.git(r, "revert", "--no-edit", b).rc == 0  # C reverts B: net code diff is empty
    c = h.head(r)
    assert h.git(r, "diff", "--name-only", f"{a}..{c}").out.strip() == "", "precondition: net diff of A..C is empty"
    assert _pushcheck(r, c).rc != 0, "C still carries B's code change in its history"
    d = h.commit(r, "README.md", "docs\n", "D: docs")
    assert h.git(r, "diff", "--name-only", f"{a}..{d}").out.split() == ["README.md"], "precondition: net diff is docs only"
    blocked = _pushcheck(r, d)
    assert blocked.rc == 1 and "push blocked" in blocked.err, blocked.all


def test_check_approval_pure_docs_chain_descendant_still_passes():
    r = h.mk_repo("led_stdin_docschain")
    assert h.ledger(r, "approve", "--evidence", "e").rc == 0
    h.commit(r, "README.md", "one\n", "doc 1")
    d2 = h.commit(r, "docs/guide.md", "two\n", "doc 2")
    h.commit(r, "NOTES.md", "three\n", "doc 3")
    assert _pushcheck(r, d2).rc == 0
    assert _pushcheck(r, h.head(r)).rc == 0


def test_check_approval_unrelated_branch_is_blocked():
    r = h.mk_repo("led_stdin_side")
    c1 = h.head(r)
    h.commit(r, "a.py", "a = 1\n", "c2")
    assert h.ledger(r, "approve", "--evidence", "e").rc == 0
    assert h.git(r, "checkout", "-q", "-b", "side", c1).rc == 0
    side = h.commit(r, "s.md", "s\n", "side doc commit")
    h.git(r, "checkout", "-q", "main")
    assert _pushcheck(r, side).rc != 0, "neither ancestor nor descendant of the approved sha"


def test_check_approval_branch_delete_only_push_is_not_blocked():
    r = h.mk_repo("led_stdin_delete")
    h.commit(r, "a.py", "a = 1\n", "unapproved head")
    zero = "0" * 40
    res = h.ledger(r, "check-approval", inp=f"(delete) {zero} refs/heads/old {h.head(r)}\n")
    assert res.rc == 0, f"deleting a remote branch has nothing to approve: {res.all}"


def test_skip_approval_env_exits_zero_and_records_event():
    r = h.mk_repo("led_skip")
    assert h.ledger(r, "check-approval").rc != 0
    res = h.ledger(r, "check-approval", env={"HARNESS_SKIP_APPROVAL": "1"})
    assert res.rc == 0, res.all
    assert any(e["kind"] == "approval-skipped" for e in h.load_ledger(r)["events"])
    assert h.ledger(r, "check-approval", env={"HARNESS_SKIP_APPROVAL": "0"}).rc != 0


if __name__ == "__main__":
    sys.exit(h.run_all(globals()))
