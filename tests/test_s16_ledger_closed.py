"""S16-T1 tests: a late commit on a closed sprint's task, and evidence freshness across docs-only / tests-only commits.
Written from the S16-T1 acceptance criteria, not from the code. Real tools/ledger.py in temp git repos; the agent-run
state (what verify pass / review accepted require) is a temp HARNESS_HOOK_STATE, never the real ~/.harness-hooks.
Run: C:\\Python314\\python.exe -I tests/test_s16_ledger_closed.py"""
import importlib.util
import json
import sys
import time
from pathlib import Path

_s = importlib.util.spec_from_file_location("hv2", Path(__file__).resolve().parent / "hv2.py")
h = importlib.util.module_from_spec(_s)
sys.modules["hv2"] = h
_s.loader.exec_module(h)

TESTER, REVIEWER = "functional-test-reviewer", "python-reviewer"


def _env(repo):
    return {"HARNESS_HOOK_STATE": str(repo.parent / (repo.name + "-state"))}


def _seed_runs(repo):
    """Finished test and review agent runs for this repo, as the agent_visibility hook would have recorded them."""
    st = Path(_env(repo)["HARNESS_HOOK_STATE"]) / "agents"
    st.mkdir(parents=True, exist_ok=True)
    now = time.time()
    proj = str(repo.resolve())
    agents = [{"type": t, "start": now, "stop": now + 1, "project": proj, "session": "s", "sources": ["pre"]}
              for t in (TESTER, REVIEWER)]
    (st / "active.json").write_text(json.dumps({"agents": agents}), encoding="utf-8")


def _hook(repo):
    r = h.ledger(repo, "commit-hook", env=_env(repo))
    assert r.rc == 0, r.all
    return r


def _done_task(name):
    """Repo with sprint S1 (one task, risk local) whose task S1-T1 is done at its first product-code commit."""
    r = h.mk_repo(name)
    h.setup_tasks(r, 1)
    h.commit(r, "tools/a.py", "x = 1\n", "feat a", "Task: S1-T1")
    _hook(r)
    _seed_runs(r)
    v = h.ledger(r, "verify", "S1-T1", "--result", "pass", "--evidence", "ran", "--by", TESTER, env=_env(r))
    assert v.rc == 0, v.all
    assert h.task(r, "S1-T1")["state"] == "verified"
    rv = h.ledger(r, "review", "S1-T1", "--result", "accepted", "--by", REVIEWER, env=_env(r))
    assert rv.rc == 0, rv.all
    assert h.task(r, "S1-T1")["state"] == "done"
    return r


# ---------------------------------------------------------------- criterion 1
def _closed_sprint_repo(name, make_done):
    if make_done:
        r = _done_task(name)
    else:
        r = h.mk_repo(name)
        h.setup_tasks(r, 1)
        h.commit(r, "tools/a.py", "x = 1\n", "feat a", "Task: S1-T1")
        _hook(r)
    c = h.ledger(r, "sprint-close", "S1", "--force", env=_env(r))
    assert c.rc == 0, c.all
    assert h.ledger(r, "sprint-add", "--id", "S2", "--title", "next", env=_env(r)).rc == 0
    return r


def _closed_snapshot(r):
    led = h.load_ledger(r)
    s1 = next(s for s in led["sprints"] if s["id"] == "S1")
    t = s1["tasks"][0]
    return {"state": t["state"], "commits": list(t["commits"]), "verify": t["verify"], "review": t["review"],
            "sprint_state": s1["state"], "closed": s1.get("closed")}


def test_late_commit_on_closed_sprint_task_leaves_state_and_records_after_close():
    for make_done in (True, False):
        r = _closed_sprint_repo(f"s16c_closed_{make_done}", make_done)
        before = _closed_snapshot(r)
        late = h.commit(r, "tools/late.py", "y = 2\n", "late code", "Task: S1-T1")
        res = _hook(r)
        after = _closed_snapshot(r)
        assert after == before, (before, after)
        assert after["sprint_state"] == "closed"
        t = h.task(r, "S1-T1")
        assert t["after_close_commits"] == [late], t.get("after_close_commits")
        assert late not in t["commits"]
        ev = [e for e in h.load_ledger(r)["events"] if e.get("kind") == "commit-after-close"]
        assert len(ev) == 1 and ev[0]["task"] == "S1-T1" and ev[0]["sha"] == late, ev
        assert "WARNING" in res.out and "S1-T1" in res.out, res.all


def test_late_docs_commit_on_done_task_of_closed_sprint_keeps_done():
    r = _closed_sprint_repo("s16c_closed_docs", True)
    late = h.commit(r, "core/NOTE.md", "n\n", "docs", "Task: S1-T1")
    _hook(r)
    t = h.task(r, "S1-T1")
    assert t["state"] == "done" and t["after_close_commits"] == [late] and late not in t["commits"]


def test_after_close_commits_not_duplicated_and_open_sprint_unaffected():
    r = _closed_sprint_repo("s16c_closed_dup", False)
    assert h.ledger(r, "task-add", "--title", "new", env=_env(r)).rc == 0  # S2-T1, open sprint
    late = h.commit(r, "tools/late.py", "y = 2\n", "late", "Task: S1-T1")
    _hook(r)
    _hook(r)  # same commit seen twice (retry)
    assert h.task(r, "S1-T1")["after_close_commits"] == [late]
    new = h.commit(r, "tools/n.py", "z = 3\n", "new work", "Task: S2-T1")
    _hook(r)
    t2 = h.task(r, "S2-T1")
    assert t2["commits"] == [new] and t2["state"] == "implemented" and not t2.get("after_close_commits")


# ---------------------------------------------------------------- criterion 2
def _state_after(r, rel, content, msg="Task: S1-T1"):
    h.commit(r, rel, content, "later work", msg)
    _hook(r)
    return h.task(r, "S1-T1")["state"]


def test_docs_only_commits_keep_done():
    r = _done_task("s16c_docs")
    assert _state_after(r, "core/LOG.md", "a\n") == "done"
    assert _state_after(r, "docs/guide.md", "b\n") == "done"
    assert len(h.task(r, "S1-T1")["commits"]) == 3


def test_tests_only_commit_keeps_done():
    r = _done_task("s16c_tests")
    assert _state_after(r, "tests/test_a.py", "def test_a(): pass\n") == "done"


def test_verified_task_stays_verified_across_docs_commit():
    r = h.mk_repo("s16c_verified")
    h.setup_tasks(r, 1)
    h.commit(r, "tools/a.py", "x = 1\n", "feat", "Task: S1-T1")
    _hook(r)
    _seed_runs(r)
    assert h.ledger(r, "verify", "S1-T1", "--result", "pass", "--evidence", "ran", "--by", TESTER, env=_env(r)).rc == 0
    assert _state_after(r, "core/LOG.md", "a\n") == "verified"


def test_product_code_commit_after_evidence_makes_state_implemented():
    r = _done_task("s16c_code")
    assert _state_after(r, "tools/x.py", "q = 1\n") == "implemented"


def test_mixed_docs_and_code_commit_counts_as_code():
    r = _done_task("s16c_mixed")
    assert _state_after(r, "core/LOG.md", "a\n") == "done"
    (r / "core" / "B.md").write_text("b\n", encoding="utf-8")
    (r / "tools" / "m.py").write_text("m = 1\n", encoding="utf-8")
    assert h.git(r, "add", "-A").rc == 0
    assert h.git(r, "commit", "-q", "-m", "mixed", "-m", "Task: S1-T1").rc == 0
    _hook(r)
    assert h.task(r, "S1-T1")["state"] == "implemented"


def test_code_commit_then_later_docs_commit_stays_implemented():
    r = _done_task("s16c_codedocs")
    assert _state_after(r, "tools/x.py", "q = 1\n") == "implemented"
    assert _state_after(r, "core/LOG.md", "a\n") == "implemented"  # evidence predates the code commit


def test_empty_file_list_commit_counts_as_code():
    r = _done_task("s16c_empty")
    assert h.git(r, "commit", "-q", "--allow-empty", "-m", "empty", "-m", "Task: S1-T1").rc == 0
    _hook(r)
    assert h.task(r, "S1-T1")["state"] == "implemented"


def test_reverify_after_code_commit_restores_done_then_docs_keep_it():
    r = _done_task("s16c_reverify")
    assert _state_after(r, "tools/x.py", "q = 1\n") == "implemented"
    _seed_runs(r)
    assert h.ledger(r, "verify", "S1-T1", "--result", "pass", "--evidence", "again", "--by", TESTER, env=_env(r)).rc == 0
    assert h.ledger(r, "review", "S1-T1", "--result", "accepted", "--by", REVIEWER, env=_env(r)).rc == 0
    assert h.task(r, "S1-T1")["state"] == "done"
    assert _state_after(r, "core/LOG2.md", "z\n") == "done"


# ---------------------------------------------------------------- criterion 2 (strict non-code rule) and 1 (code flag)
NON_CODE = ["docs/План.md", "core/notes.txt", "docs/a.rst", "reports/x.html", ".harness/report.html", "tests/helper.py",
            "tests/ui/case.spec.ts", "tests/test_x.py", "src/pkg/tests/helpers.py"]
CODE_LIKE = ["docs/build.py", "requirements.txt", "core/requirements-dev.txt", "api.spec.json", "tests/fixture.json",
             "docs/diagram.svg", ".claude/agents/a.md", ".codex/notes.md", ".agents/s.md", "harness/agents/a.md",
             "harness/skills/s/SKILL.md", "harness/commands/c.md", "harness/rules/r.md", "tools/test_gate.py"]


def test_non_code_paths_keep_done():
    for i, rel in enumerate(NON_CODE):
        r = _done_task(f"s16c_nc{i}")
        assert _state_after(r, rel, "x\n") == "done", rel


def test_code_like_paths_make_state_implemented():
    for i, rel in enumerate(CODE_LIKE):
        r = _done_task(f"s16c_cl{i}")
        assert _state_after(r, rel, "x\n") == "implemented", rel


def test_late_commit_warning_flags_product_code_only_when_code():
    r = _closed_sprint_repo("s16c_flag_code", False)
    h.commit(r, "tools/late.py", "y = 2\n", "late", "Task: S1-T1")
    res = _hook(r)
    assert "CHANGES PRODUCT CODE" in res.out, res.all
    ev = [e for e in h.load_ledger(r)["events"] if e.get("kind") == "commit-after-close"]
    assert ev[-1]["code"] is True, ev
    r2 = _closed_sprint_repo("s16c_flag_doc", False)
    h.commit(r2, "core/N.md", "n\n", "late doc", "Task: S1-T1")
    res2 = _hook(r2)
    assert "WARNING" in res2.out and "CHANGES PRODUCT CODE" not in res2.out, res2.all
    ev2 = [e for e in h.load_ledger(r2)["events"] if e.get("kind") == "commit-after-close"]
    assert ev2[-1]["code"] is False, ev2


def test_require_agent_run_same_rule_docs_commit_ok_code_like_commit_makes_run_stale():
    # the agent run is seeded right after commit A; commit B is dated far in the future. If B is non-code the run is
    # still judged against A and verify passes; if B is code-like (docs/build.py) the run predates B and is refused.
    for rel, ok in (("core/LOG.md", True), ("docs/build.py", False)):
        r = h.mk_repo("s16c_rar_" + ("doc" if ok else "code"))
        h.setup_tasks(r, 1)
        h.commit(r, "tools/a.py", "x = 1\n", "feat", "Task: S1-T1")
        _hook(r)
        _seed_runs(r)
        env = {"GIT_COMMITTER_DATE": "2099-01-01T00:00:00 +0000", "GIT_AUTHOR_DATE": "2099-01-01T00:00:00 +0000"}
        h.commit(r, rel, "z\n", "later", "Task: S1-T1", env=env)
        _hook(r)
        v = h.ledger(r, "verify", "S1-T1", "--result", "pass", "--evidence", "e", "--by", TESTER, env=_env(r))
        assert (v.rc == 0) == ok, (rel, v.all)


if __name__ == "__main__":
    sys.exit(h.run_all(globals()))
