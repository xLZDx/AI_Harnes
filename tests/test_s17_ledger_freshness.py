"""S17-T4 tests (B30): tools/ledger.py commit classification. Written from the S17-T4 acceptance criteria, not from the code.
1. A git failure while a commit is classified is not cached: the failing call returns False (counts as code), the next call
   for the same sha classifies again.
2. A successful answer is cached: git is not called twice for the same sha.
3. safe_non_code_path('.harness/ledger.json') is True; a docs-only commit that also changes .harness/ledger.json keeps
   verify/review fresh (temp repo, style of test_s16_ledger_closed.py).
4. The S16 classification is unchanged.
Run: C:\\Python314\\python.exe -I tests/test_s17_ledger_freshness.py"""
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


def _load_ledger(tag):
    """A fresh copy of tools/ledger.py, so the per-process classification cache starts empty in every test."""
    spec = importlib.util.spec_from_file_location("ledger_" + tag, h.TOOLS / "ledger.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["ledger_" + tag] = m
    spec.loader.exec_module(m)
    m.safe_non_code_path("docs/x.md")  # loads the route rules now, so a later fake git sees only classification calls
    return m


class FakeGit:
    """Replacement for ledger.git: each scripted item is a string (returned) or an Exception (raised)."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = 0

    def __call__(self, *args, **kw):
        self.calls += 1
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item


# ---------------------------------------------------------------- criterion 1
def test_c1_git_failure_is_not_cached_and_returns_false():
    L = _load_ledger("c1")
    g = FakeGit(L.GitError("transient"), "docs/a.md\n")
    L.git = g
    assert L._docs_or_tests_only("abc") is False  # the failure counts as code
    assert g.calls == 1
    assert L._docs_or_tests_only("abc") is True  # classified again, and now correctly
    assert g.calls == 2


def test_c1_repeated_failures_keep_retrying():
    L = _load_ledger("c1b")
    g = FakeGit(L.GitError("x"))
    L.git = g
    assert L._docs_or_tests_only("abc") is False
    assert L._docs_or_tests_only("abc") is False
    assert g.calls == 2


def test_c1_failure_for_one_sha_does_not_poison_another():
    L = _load_ledger("c1c")
    L.git = FakeGit(L.GitError("x"), "docs/a.md\n")
    assert L._docs_or_tests_only("s1") is False
    assert L._docs_or_tests_only("s2") is True


# ---------------------------------------------------------------- criterion 2
def test_c2_success_is_cached_git_called_once():
    L = _load_ledger("c2")
    g = FakeGit("docs/a.md\ncore/b.md\n")
    L.git = g
    assert L._docs_or_tests_only("abc") is True
    assert L._docs_or_tests_only("abc") is True
    assert g.calls == 1


def test_c2_code_answer_is_cached_too():
    L = _load_ledger("c2b")
    g = FakeGit("tools/a.py\n")
    L.git = g
    assert L._docs_or_tests_only("abc") is False
    assert L._docs_or_tests_only("abc") is False
    assert g.calls == 1


def test_c2_different_shas_are_classified_separately():
    L = _load_ledger("c2c")
    g = FakeGit("docs/a.md\n")
    L.git = g
    L._docs_or_tests_only("a1")
    L._docs_or_tests_only("b2")
    assert g.calls == 2


def test_c2_empty_file_list_is_code():
    L = _load_ledger("c2d")
    L.git = FakeGit("\n")
    assert L._docs_or_tests_only("abc") is False


# ---------------------------------------------------------------- criterion 3
def test_c3_ledger_json_is_safe_non_code_path():
    L = _load_ledger("c3")
    assert L.safe_non_code_path(".harness/ledger.json") is True
    assert L.safe_non_code_path(".harness\\ledger.json") is True


def test_c3_only_the_ledger_file_itself_is_exempt():
    L = _load_ledger("c3b")
    assert L.safe_non_code_path(".harness/other.json") is False
    assert L.safe_non_code_path("sub/.harness/ledger.json") is False


def _env(repo):
    return {"HARNESS_HOOK_STATE": str(repo.parent / (repo.name + "-state"))}


def _seed_runs(repo):
    st = Path(_env(repo)["HARNESS_HOOK_STATE"]) / "agents"
    st.mkdir(parents=True, exist_ok=True)
    now = time.time()
    agents = [{"type": t, "start": now, "stop": now + 1, "project": str(repo.resolve()), "session": "s", "sources": ["pre"]}
              for t in (TESTER, REVIEWER)]
    (st / "active.json").write_text(json.dumps({"agents": agents}), encoding="utf-8")


def _hook(repo):
    r = h.ledger(repo, "commit-hook", env=_env(repo))
    assert r.rc == 0, r.all


def _done_task(name):
    r = h.mk_repo(name)
    h.setup_tasks(r, 1)
    h.commit(r, "tools/a.py", "x = 1\n", "feat a", "Task: S1-T1")
    _hook(r)
    _seed_runs(r)
    assert h.ledger(r, "verify", "S1-T1", "--result", "pass", "--evidence", "ran", "--by", TESTER, env=_env(r)).rc == 0
    assert h.ledger(r, "review", "S1-T1", "--result", "accepted", "--by", REVIEWER, env=_env(r)).rc == 0
    assert h.task(r, "S1-T1")["state"] == "done"
    return r


def _commit_with_ledger(r, rel, content):
    """A commit that changes rel AND .harness/ledger.json, as the harness does when it records state."""
    f = r / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content, encoding="utf-8", newline="\n")
    lf = r / ".harness" / "ledger.json"
    lf.write_text(lf.read_text(encoding="utf-8") + "\n", encoding="utf-8", newline="\n")  # make the file differ
    assert h.git(r, "add", "-f", rel, ".harness/ledger.json").rc == 0
    res = h.git(r, "commit", "-q", "-m", "later", "-m", "Task: S1-T1")
    assert res.rc == 0, res.all
    files = h.git(r, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").out.split()
    assert ".harness/ledger.json" in files and rel in files, files
    _hook(r)
    return h.task(r, "S1-T1")["state"]


def test_c3_docs_commit_that_also_changes_ledger_json_keeps_done():
    r = _done_task("s17t4_docs_ledger")
    assert _commit_with_ledger(r, "core/LOG.md", "a\n") == "done"


def test_c3_code_commit_that_also_changes_ledger_json_makes_implemented():
    r = _done_task("s17t4_code_ledger")
    assert _commit_with_ledger(r, "tools/x.py", "q = 1\n") == "implemented"


# ---------------------------------------------------------------- criterion 4 (S16 classification unchanged)
def test_c4_code_like_paths_stay_code():
    L = _load_ledger("c4")
    for f in ("docs/build.py", "requirements.txt", "tests/fixture.json"):
        assert L.safe_non_code_path(f) is False, f


def test_c4_docs_path_stays_safe():
    L = _load_ledger("c4b")
    assert L.safe_non_code_path("docs/План.md") is True


if __name__ == "__main__":
    sys.exit(h.run_all(globals()))
