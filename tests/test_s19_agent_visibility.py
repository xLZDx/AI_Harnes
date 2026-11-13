"""S19-T1 tests (B44, B40, B41): harness/hooks/agent_visibility.py. Written from the S19-T1 acceptance criteria, not from the code.
1. Six or more directories of ONE repository bind to that repository; directories under an already found repository root cost
   no extra git call (calls counted by wrapping the git runner); a nested repository (a .git inside the found root) still
   gets its own git call.
2. These give '': a new repository root, a git timeout, the deadline forced negative, the match limit (more than 50 hits).
3. BIND_DEADLINE_S <= 8 (the hook budget is 10 s).
4. A single unrecorded candidate older than UNKNOWN_AFTER_S prints a 'too old to pair' line and finishes nothing.
5. An unquoted path with a space ('<tmp>\\My Project\\x.py') binds to the 'My Project' repository, never to the parent; a
   non-existent extension keeps the plain path.
Stop tests run the hook as a real subprocess (HARNESS_HOOK_STATE = temp folder); project_from_text runs in-process, and
the time bounds are set here in-process (git is slow on this machine).
Run: C:\\Python314\\python.exe -I -s tests/test_s19_agent_visibility.py"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_s = importlib.util.spec_from_file_location("hv2", Path(__file__).resolve().parent / "hv2.py")
h = importlib.util.module_from_spec(_s)
sys.modules["hv2"] = h
_s.loader.exec_module(h)

HOOK = h.SRC / "harness" / "hooks" / "agent_visibility.py"
T = "tester"
sys.path.insert(0, str(h.SRC / "harness" / "hooks"))
import _agentio  # noqa: E402
import agent_visibility as av  # noqa: E402

SHIPPED_DEADLINE = av.BIND_DEADLINE_S  # read before the test overrides it
SHIPPED_UNKNOWN_AFTER = av.UNKNOWN_AFTER_S
SHIPPED_GIT_TIMEOUT = av.BIND_GIT_TIMEOUT_S
# the machine may be loaded: the production deadline and git limit would turn a slow git start into a spurious ''.
av.BIND_DEADLINE_S = 120.0
av.BIND_GIT_TIMEOUT_S = 60


def _norm(p):
    return os.path.normcase(str(Path(p).resolve()))


def _bind(*paths):
    return av.project_from_text({"prompt": "work on " + " ".join(str(p) for p in paths)})


def _dirs(parent, n, prefix="d"):
    out = []
    for i in range(n):
        d = parent / f"{prefix}{i}"
        d.mkdir(exist_ok=True)
        out.append(d)
    return out


class _Recorder:
    """Wraps _agentio.run_git: records every cwd; optional forced return code for a cwd that ends with a given name."""

    def __init__(self, force=None):
        self.calls, self.force, self.orig = [], force or {}, _agentio.run_git

    def __enter__(self):
        def fake(args, cwd=None, timeout=None):
            self.calls.append(str(cwd))
            for key, rc in self.force.items():
                if str(cwd).lower().endswith(key.lower()):
                    return subprocess.CompletedProcess(["git"], rc, "", "forced")
            return self.orig(args, cwd=cwd, timeout=timeout)
        _agentio.run_git = fake
        return self

    def __exit__(self, *e):
        _agentio.run_git = self.orig


# ---------------------------------------------------------------- criterion 1: one root, no extra git calls
def test_six_dirs_of_one_repo_bind_to_it():
    a = h.mk_repo("s19t1_six")
    assert _norm(_bind(*_dirs(a, 6))) == _norm(a)


def test_dirs_under_a_found_root_cost_no_extra_git_call():
    a = h.mk_repo("s19t1_calls")
    deep = a / "x" / "y" / "z"
    deep.mkdir(parents=True)
    ds = _dirs(a, 8) + [deep]
    with _Recorder() as rec:
        got = _bind(*ds)
    assert _norm(got) == _norm(a), got
    assert len(rec.calls) == 1, rec.calls  # one call finds the root; the other eight directories lie under it


def test_dirs_under_a_found_root_cost_no_call_even_with_forced_timeout_on_later_dirs():
    # twin of the count test at the outcome level: a git that would time out on every later directory is never asked
    a = h.mk_repo("s19t1_nocall")
    ds = _dirs(a, 7)
    with _Recorder(force={"d3": 124, "d4": 124, "d5": 124, "d6": 124}):
        assert _norm(_bind(*ds)) == _norm(a)


def test_nested_real_repository_gets_its_own_git_call():
    a = h.mk_repo("s19t1_nested")
    inner = a / "inner"
    inner.mkdir()
    assert h.git(inner, "init", "-q", "-b", "main").rc == 0
    d0 = _dirs(a, 1)[0]
    with _Recorder() as rec:
        got = _bind(d0, inner)
    assert any(c.lower().endswith("inner") for c in rec.calls), rec.calls  # not skipped as 'under the found root'
    assert len(rec.calls) == 2, rec.calls
    assert got == "", got  # the nested repository is a second root: ambiguous, fail closed


def test_dir_with_a_dot_git_marker_inside_the_root_is_not_skipped():
    a = h.mk_repo("s19t1_marker")
    plain, marked = _dirs(a, 1)[0], _dirs(a, 1, prefix="m")[0]
    (marked / ".git").write_text("", encoding="utf-8")  # an invalid gitfile: git finds no root there
    sub = marked / "sub"
    sub.mkdir()
    with _Recorder() as rec:
        got = _bind(plain, sub)
    assert len(rec.calls) == 2 and rec.calls[1].lower().endswith("sub"), rec.calls  # .git between sub and the root
    assert _norm(got) == _norm(a), got


# ---------------------------------------------------------------- criterion 2: these give ''
def test_new_repository_root_gives_empty():
    a, b = h.mk_repo("s19t1_ra"), h.mk_repo("s19t1_rb")
    assert _bind(*_dirs(a, 6), b) == ""
    assert _norm(_bind(*_dirs(a, 6))) == _norm(a)  # twin


def test_git_timeout_gives_empty():
    a = h.mk_repo("s19t1_to")
    d = _dirs(a, 1)[0]
    assert _norm(_bind(d)) == _norm(a)  # twin: no timeout
    with _Recorder(force={"d0": 124}):
        assert _bind(d) == ""


def test_deadline_forced_negative_gives_empty():
    a = h.mk_repo("s19t1_dl")
    old = av.BIND_DEADLINE_S
    av.BIND_DEADLINE_S = -1.0
    try:
        assert _bind(*_dirs(a, 2)) == ""
    finally:
        av.BIND_DEADLINE_S = old
    assert _norm(_bind(*_dirs(a, 2))) == _norm(a)  # twin


def test_match_limit_gives_empty():
    a = h.mk_repo("s19t1_ml")
    ds = _dirs(a, 51)
    assert _norm(_bind(*ds[:50])) == _norm(a), "50 hits are within the limit"
    assert _bind(*ds) == "", "the 51st hit passes the match limit of 50"
    # the limit is a parameter: a tighter one trips on the 4th hit
    assert av.project_from_text({"prompt": " ".join(str(d) for d in ds[:3])}, max_matches=3) != ""
    assert av.project_from_text({"prompt": " ".join(str(d) for d in ds[:4])}, max_matches=3) == ""


# ---------------------------------------------------------------- criterion 3: the deadline stays inside the hook budget
def test_bind_deadline_is_at_most_8_seconds():
    assert SHIPPED_DEADLINE <= 8, SHIPPED_DEADLINE
    assert SHIPPED_DEADLINE > 0, SHIPPED_DEADLINE
    # the deadline plus one last git call must still fit the 10 s hook budget
    assert SHIPPED_DEADLINE + SHIPPED_GIT_TIMEOUT <= 10, (SHIPPED_DEADLINE, SHIPPED_GIT_TIMEOUT)


# ---------------------------------------------------------------- criterion 4: a too-old single candidate
def _fire(state, payload):
    r = h.run([h.PY, "-I", "-s", "-X", "utf8", str(HOOK), "--harness-client=claude"], state,
              {"HARNESS_HOOK_STATE": str(state)}, json.dumps(payload))
    assert r.rc == 0, r.all
    return r


def _agents(state):
    f = state / "agents" / "active.json"
    return json.loads(f.read_text(encoding="utf-8"))["agents"] if f.exists() else []


def _seed(state, entries):
    d = state / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "active.json").write_text(json.dumps({"agents": entries}), encoding="utf-8")


def _e(start_ago, aid=None, sources=("pre",), typ=T, sid="s1"):
    e = {"type": typ, "model": "m", "effort": "e", "task": "active", "start": time.time() - start_ago, "project": "",
         "session": sid, "tool_use_id": "", "sources": list(sources)}
    if aid:
        e["agent_id"] = aid
    return e


def _stop(state, aid="NEWID"):
    return _fire(state, {"hook_event_name": "SubagentStop", "session_id": "s1", "cwd": str(state), "agent_type": T,
                         "agent_id": aid})


def _msg(r):
    return json.loads(r.out)["systemMessage"]


def test_single_old_unrecorded_candidate_prints_too_old_and_finishes_nothing():
    st = h.fresh("s19t1_old")
    _seed(st, [_e(SHIPPED_UNKNOWN_AFTER + 100)])
    line = _msg(_stop(st))
    a = _agents(st)
    assert len(a) == 1 and not a[0].get("stop") and not a[0].get("agent_id"), a
    assert "too old to pair" in line, line
    assert not re.search(r"\d+ s\b", line), line  # no duration: nothing was finished


def test_single_young_unrecorded_candidate_is_finished_twin():
    st = h.fresh("s19t1_young")
    _seed(st, [_e(SHIPPED_UNKNOWN_AFTER - 30)])
    line = _msg(_stop(st))
    a = _agents(st)
    assert a[0].get("stop") and a[0].get("agent_id") == "NEWID", a
    assert "too old" not in line and re.search(r"\d+ s\b", line), line


def test_two_old_candidates_are_ambiguous_not_too_old():
    st = h.fresh("s19t1_old2")
    _seed(st, [_e(SHIPPED_UNKNOWN_AFTER + 200), _e(SHIPPED_UNKNOWN_AFTER + 100)])
    line = _msg(_stop(st))
    assert "not paired: 2" in line and "too old" not in line, line
    assert not any(e.get("stop") for e in _agents(st)), _agents(st)


def test_no_candidate_still_says_start_was_not_observed():
    st = h.fresh("s19t1_none")
    _seed(st, [])
    line = _msg(_stop(st))
    assert "start was not observed" in line and "too old" not in line, line


# ---------------------------------------------------------------- criterion 5: unquoted path with a space
def _spaced_pair(name):
    """(parent repo, nested 'My Project' repo, file x.py inside it); the parent is a repository too, so a wrong binding to the
    parent is observable."""
    parent = h.mk_repo(name)
    proj = parent / "My Project"
    proj.mkdir()
    assert h.git(proj, "init", "-q", "-b", "main").rc == 0
    f = proj / "x.py"
    f.write_text("x = 1\n", encoding="utf-8")
    return parent, proj, f


def test_unquoted_path_with_a_space_binds_to_the_spaced_repository_not_the_parent():
    parent, proj, f = _spaced_pair("s19t1_sp")
    got = av.project_from_text({"prompt": "please edit " + str(f) + " now"})
    assert _norm(got) == _norm(proj), got
    assert _norm(got) != _norm(parent), got


def test_unquoted_path_with_a_space_and_forward_slashes_binds_to_the_spaced_repository():
    parent, proj, f = _spaced_pair("s19t1_spf")
    got = av.project_from_text({"prompt": "please edit " + f.as_posix() + " now"})
    assert _norm(got) == _norm(proj), got


def test_unquoted_space_path_at_the_end_of_the_text_binds():
    parent, proj, f = _spaced_pair("s19t1_spe")
    got = av.project_from_text({"prompt": "please edit " + str(f)})
    assert _norm(got) == _norm(proj), got


def test_non_existent_extension_keeps_the_plain_path():
    a = h.mk_repo("s19t1_plain")
    sub = a / "sub"
    sub.mkdir()
    with _Recorder() as rec:
        got = av.project_from_text({"prompt": "look at " + str(sub) + " nonexistent words here"})
    assert _norm(got) == _norm(a), got
    assert len(rec.calls) == 1 and _norm(rec.calls[0]) == _norm(sub), rec.calls  # asked about the plain path, not an invented one


def test_missing_file_under_a_spaced_directory_binds_to_the_spaced_repository():
    # b348825: '...\My Project\ghost.py' does not exist, but the directory '...\My Project' does: bind to it, never the parent
    parent, proj, f = _spaced_pair("s19t1_ghost")
    ghost = proj / "ghost.py"
    assert not ghost.exists()
    got = av.project_from_text({"prompt": "please edit " + str(ghost) + " now"})
    assert _norm(got) == _norm(proj), got
    assert _norm(got) != _norm(parent), got


def test_plain_path_without_spaces_to_a_missing_file_still_binds():
    a = h.mk_repo("s19t1_plainmiss")
    sub = a / "sub"
    sub.mkdir()
    got = av.project_from_text({"prompt": "please edit " + str(sub / "ghost.py") + " now"})
    assert _norm(got) == _norm(a), got
    got2 = av.project_from_text({"prompt": "please edit " + str(a / "nodir" / "ghost.py") + " now"})
    assert _norm(got2) == _norm(a), got2


def test_missing_file_under_a_spaced_directory_outside_any_repository_gives_empty():
    base = h.fresh("s19t1_norepo") / "My Project"
    base.mkdir()
    assert av.project_root(str(base)) == "", "fixture: the folder must lie outside any repository"
    got = av.project_from_text({"prompt": "please edit " + str(base / "ghost.py") + " now"})
    assert got == "", got


if __name__ == "__main__":
    sys.exit(h.run_all(globals()))
