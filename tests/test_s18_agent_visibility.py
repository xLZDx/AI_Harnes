"""S18-T1 tests (B37, B26, B38): harness/hooks/agent_visibility.py. Written from the S18-T1 acceptance criteria, not from the code.
1. A stop whose agent_id is on no entry: exactly one PreToolUse-only id-less entry of the type and session is finished and
   stamped with the id; two or more: nothing finished, 'not paired: N', no duration.
2. State lock not taken: 'stop was not recorded: the state is busy' (lock file held by another party), nothing recorded.
3. project_from_text past the 5-directory cap (temp git repos): duplicates and non-repo paths keep the binding; a new
   repository root gives ''; more than 10 new directories give ''; a git timeout (returncode 124) gives ''.
4. A quoted path with spaces binds (double quotes with backslashes, single quotes with forward slashes); a quoted UNC or URL
   path is never probed.
Stop tests run the hook as a real subprocess (HARNESS_HOOK_STATE = temp folder); project_from_text runs in-process.
Run: C:\\Python314\\python.exe -I tests/test_s18_agent_visibility.py"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from io import StringIO
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

# the machine may be loaded (other test runs): the production 3 s search deadline and 2 s git limit would turn a slow
# git start into a spurious '' answer. Tests of the git-timeout path force the return code instead of relying on time.
av.BIND_DEADLINE_S = 120.0
av.BIND_GIT_TIMEOUT_S = 60


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


def _stop(state, aid="NEWID", typ=T, sid="s1"):
    return _fire(state, {"hook_event_name": "SubagentStop", "session_id": sid, "cwd": str(state), "agent_type": typ,
                         "agent_id": aid})


def _msg(r):
    return json.loads(r.out)["systemMessage"]


# ---------------------------------------------------------------- criterion 1
def test_c1_one_pretooluse_only_idless_entry_is_finished_and_stamped_with_the_id():
    st = h.fresh("s18t1_c1a")
    _seed(st, [_e(100)])
    r = _stop(st)
    a = _agents(st)
    assert len(a) == 1 and a[0].get("stop") and a[0].get("agent_id") == "NEWID", a
    line = _msg(r)
    assert re.search(r"\d+ s\b", line) and "not paired" not in line and "start was not observed" not in line, line


def test_c1_two_pretooluse_only_idless_entries_nothing_finished_not_paired_two():
    st = h.fresh("s18t1_c1b")
    _seed(st, [_e(200), _e(100)])
    line = _msg(_stop(st))
    a = _agents(st)
    assert not any(e.get("stop") for e in a) and not any(e.get("agent_id") for e in a), a
    assert "not paired: 2" in line, line
    assert " s |" not in line and not re.search(r"\d+ s\b", line), line


def test_c1_three_entries_not_paired_three():
    st = h.fresh("s18t1_c1c")
    _seed(st, [_e(300), _e(200), _e(100)])
    line = _msg(_stop(st))
    assert "not paired: 3" in line and not any(e.get("stop") for e in _agents(st)), line


def test_c1_entry_reported_by_subagentstart_is_not_a_candidate():
    st = h.fresh("s18t1_c1d")
    _seed(st, [_e(100, sources=("pre", "start"))])  # SubagentStart already reported it: never taken by an unknown id
    line = _msg(_stop(st))
    assert not any(e.get("stop") for e in _agents(st)), _agents(st)
    assert "start was not observed" in line and "not paired" not in line, line


def test_c1_entry_of_another_session_or_type_is_not_a_candidate():
    st = h.fresh("s18t1_c1e")
    _seed(st, [_e(100, sid="other"), _e(100, typ="other-type")])
    line = _msg(_stop(st))
    assert not any(e.get("stop") for e in _agents(st)), _agents(st)
    assert "start was not observed" in line, line


def test_c1_known_id_still_finishes_its_own_entry():
    st = h.fresh("s18t1_c1f")
    _seed(st, [_e(200), _e(100, "A", sources=("pre", "start"))])
    _stop(st, "A")
    assert [bool(e.get("stop")) for e in _agents(st)] == [False, True], _agents(st)


# ---------------------------------------------------------------- criterion 2
def test_c2_busy_state_lock_says_stop_was_not_recorded():
    st = h.fresh("s18t1_c2a")
    _seed(st, [_e(100)])
    lock = st / "agents" / "active.json.lock"
    lock.write_text("99999", encoding="utf-8")  # a fresh lock held by "another process" (younger than the 10 s stale limit)
    t0 = time.time()
    r = _stop(st)
    assert time.time() - t0 < 9, "the hook must give up after its short wait"
    assert "stop was not recorded: the state is busy" in _msg(r), r.all
    assert not any(e.get("stop") for e in _agents(st)), _agents(st)
    assert lock.exists(), "a lock this hook did not take must not be removed"


def test_c2_free_lock_twin_records_the_stop():
    st = h.fresh("s18t1_c2b")
    _seed(st, [_e(100)])
    line = _msg(_stop(st))
    assert "state is busy" not in line and _agents(st)[0].get("stop"), line


def test_c2_busy_lock_in_process_with_patched_file_lock():
    class NoLock:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return False

        def __exit__(self, *e):
            return None

    st = h.fresh("s18t1_c2c")
    old_env, old_lock = os.environ.get("HARNESS_HOOK_STATE"), _agentio.file_lock
    os.environ["HARNESS_HOOK_STATE"] = str(st)
    _agentio.file_lock = NoLock
    old_out, buf = sys.stdout, StringIO()
    sys.stdout = buf
    try:
        av.announce_stop({"agent_type": T, "agent_id": "Z", "session_id": "s1"}, "claude")
    finally:
        sys.stdout = old_out
        _agentio.file_lock = old_lock
        if old_env is None:
            os.environ.pop("HARNESS_HOOK_STATE", None)
        else:
            os.environ["HARNESS_HOOK_STATE"] = old_env
    assert "stop was not recorded: the state is busy" in json.loads(buf.getvalue())["systemMessage"], buf.getvalue()


# ---------------------------------------------------------------- helpers for criteria 3 and 4
def _norm(p):
    return os.path.normcase(str(Path(p).resolve()))


def _repo(name):
    return h.mk_repo(name)


def _dirs(repo, n, prefix="d"):
    out = []
    for i in range(n):
        d = repo / f"{prefix}{i}"
        d.mkdir(exist_ok=True)
        out.append(d)
    return out


def _bind(*paths):
    return av.project_from_text({"prompt": "work on " + " ".join(str(p) for p in paths)})


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


# ---------------------------------------------------------------- criterion 3
def test_c3_baseline_five_dirs_in_one_repo_bind():
    a = _repo("s18t1_c3base")
    assert _norm(_bind(*_dirs(a, 5))) == _norm(a)


def test_c3_duplicate_dirs_after_the_cap_keep_the_binding():
    a = _repo("s18t1_c3dup")
    ds = _dirs(a, 5)
    got = _bind(*ds, *ds, *ds)  # many more mentions of already-tried dirs
    assert _norm(got) == _norm(a), got


def test_c3_non_repo_path_after_the_cap_keeps_the_binding():
    a = _repo("s18t1_c3non")
    plain = h.fresh("s18t1_c3plain")
    assert av.project_root(str(plain)) == "", "fixture: the plain folder must lie outside any repository"
    got = _bind(*_dirs(a, 5), plain)
    assert _norm(got) == _norm(a), got


def test_c3_new_repository_root_after_the_cap_gives_empty():
    a, b = _repo("s18t1_c3ra"), _repo("s18t1_c3rb")
    assert _bind(*_dirs(a, 5), b) == ""
    assert _norm(_bind(*_dirs(a, 5))) == _norm(a)  # twin: without the foreign repo it binds


def test_c3_more_than_ten_new_dirs_after_the_cap_gives_empty():
    a = _repo("s18t1_c3many")
    first = _dirs(a, 5)
    ten = _dirs(a, 10, prefix="x")
    assert _norm(_bind(*first, *ten)) == _norm(a), "exactly 10 new dirs after the cap still bind"
    eleven = _dirs(a, 11, prefix="y")
    assert _bind(*first, *eleven) == ""


def test_c3_git_timeout_after_the_cap_gives_empty():
    a = _repo("s18t1_c3to")
    ds = _dirs(a, 6)
    assert _norm(_bind(*ds)) == _norm(a)  # twin: no timeout
    with _Recorder(force={"d5": 124}):
        assert _bind(*ds) == ""


def test_c3_git_cannot_start_after_the_cap_gives_empty():
    a = _repo("s18t1_c3nogit")
    ds = _dirs(a, 6)
    with _Recorder(force={"d5": 127}):
        assert _bind(*ds) == ""


# ---------------------------------------------------------------- criterion 4
def _spaced_repo(name):
    base = h.fresh(name) / "John Doe" / "proj"
    base.mkdir(parents=True)
    assert h.git(base, "init", "-q", "-b", "main").rc == 0
    return base


def test_c4_double_quoted_path_with_spaces_and_backslashes_binds():
    r = _spaced_repo("s18t1_c4a")
    got = av.project_from_text({"prompt": 'please edit "' + str(r) + '\\x.py" now'})
    assert _norm(got) == _norm(r), got


def test_c4_single_quoted_forward_slash_path_with_spaces_binds():
    r = _spaced_repo("s18t1_c4b")
    got = av.project_from_text({"prompt": "please edit '" + r.as_posix() + "/x.py' now"})
    assert _norm(got) == _norm(r), got


def test_c4_unquoted_path_with_spaces_does_not_bind_twin():
    r = _spaced_repo("s18t1_c4c")
    assert av.project_from_text({"prompt": "please edit " + str(r) + "\\x.py now"}) == ""


def test_c4_quoted_unc_and_url_paths_are_never_probed():
    r = _repo("s18t1_c4d")
    texts = ['read "\\\\fileserver\\share\\proj\\x.py"', 'read "//fileserver/share/proj/x.py"',
             'read "https://example.com/a b/x.py"', "read 'file://fileserver/share/x.py'", "read https://example.com/x.py"]
    with _Recorder() as rec:
        for t in texts:
            assert av.project_from_text({"prompt": t}) == "", t
        assert rec.calls == [], rec.calls
        # twin: the same UNC text next to a real repo path binds that repo and probes only the repo
        got = av.project_from_text({"prompt": 'read "\\\\fileserver\\share\\x.py" and "' + str(r) + '\\README.md"'})
        assert _norm(got) == _norm(r), got
        assert rec.calls and all("fileserver" not in c.lower() for c in rec.calls), rec.calls


# ================================================================ review-fix round (commits b2b9f87, de2ddab)
# ---------------------------------------------------------------- project_from_text: whole-text scan, caps
def test_fix_one_quoted_span_holding_paths_of_two_repos_gives_empty():
    a, b = _repo("s18fix_q2a"), _repo("s18fix_q2b")
    two = av.project_from_text({"prompt": 'compare "' + str(a) + "\\x.py " + str(b) + '\\y.py" please'})
    assert two == "", two
    # twin: both paths of ONE repo inside one quoted span still bind
    (a / "sub").mkdir()
    one = av.project_from_text({"prompt": 'compare "' + str(a) + "\\x.py " + str(a / "sub") + '\\y.py" please'})
    assert _norm(one) == _norm(a), one


def test_fix_one_quoted_span_two_repos_forward_slashes_single_quotes_gives_empty():
    a, b = _repo("s18fix_q2c"), _repo("s18fix_q2d")
    got = av.project_from_text({"prompt": "compare '" + a.as_posix() + "/x.py " + b.as_posix() + "/y.py' please"})
    assert got == "", got


def test_fix_quoted_backslash_path_with_spaces_next_to_other_repo_unquoted_gives_empty():
    r = _spaced_repo("s18fix_q3a")
    other = _repo("s18fix_q3b")
    text = 'edit "' + str(r) + '\\x.py" and ' + str(other) + "\\README.md"
    assert av.project_from_text({"prompt": text}) == ""
    # twin: the spaced repo alone binds
    assert _norm(av.project_from_text({"prompt": 'edit "' + str(r) + '\\x.py"'})) == _norm(r)


def test_fix_text_is_capped_at_64kb_a_foreign_repo_after_the_cap_is_never_seen():
    a, b = _repo("s18fix_cap64a"), _repo("s18fix_cap64b")
    pad = "word " * 14000  # 70000 chars, no path characters
    assert len(pad) > 65536
    late = av.project_from_text({"prompt": f"work in {a} " + pad + f" and {b}"})
    assert _norm(late) == _norm(a), late
    # twin: the same foreign repo mentioned inside the first 64 KB gives ''
    early = av.project_from_text({"prompt": f"work in {a} and {b} " + pad})
    assert early == "", early


def test_fix_text_cap_applies_to_the_joined_values_not_per_value():
    a, b = _repo("s18fix_cap64c"), _repo("s18fix_cap64d")
    got = av.project_from_text({"prompt": f"work in {a} " + "word " * 14000, "message": f"and also {b}"})
    assert _norm(got) == _norm(a), got


def test_fix_quoted_path_longer_than_260_chars_is_not_a_quoted_candidate():
    r = _spaced_repo("s18fix_q260")
    short = av.project_from_text({"prompt": 'edit "' + str(r) + "\\" + "a" * 100 + '"'})
    assert _norm(short) == _norm(r), short  # twin: short quoted path with spaces binds
    long_ = av.project_from_text({"prompt": 'edit "' + str(r) + "\\" + "a" * 300 + '"'})
    assert long_ == "", long_  # no quoted match; the unquoted scan stops at the space inside 'John Doe'


def test_fix_search_deadline_exceeded_gives_empty_fail_closed():
    a = _repo("s18fix_deadline")
    old = av.BIND_DEADLINE_S
    av.BIND_DEADLINE_S = -1.0
    try:
        assert _bind(*_dirs(a, 2)) == ""
    finally:
        av.BIND_DEADLINE_S = old
    assert _norm(_bind(*_dirs(a, 2))) == _norm(a)  # twin


# ---------------------------------------------------------------- stop fallback: age limit
def test_fix_single_pre_only_entry_older_than_unknown_after_is_not_finished():
    st = h.fresh("s18fix_old")
    _seed(st, [_e(700)])  # UNKNOWN_AFTER_S = 600
    line = _msg(_stop(st))
    a = _agents(st)
    assert not a[0].get("stop") and not a[0].get("agent_id"), a
    assert "start was not observed" in line and "not paired" not in line, line


def test_fix_single_pre_only_entry_just_younger_than_unknown_after_is_finished():
    st = h.fresh("s18fix_young")
    _seed(st, [_e(570)])
    _stop(st)
    a = _agents(st)
    assert a[0].get("stop") and a[0].get("agent_id") == "NEWID", a


# ---------------------------------------------------------------- stop messages
def test_fix_ambiguous_unrecorded_id_stop_says_the_agent_id_was_never_recorded():
    st = h.fresh("s18fix_amb")
    _seed(st, [_e(200), _e(100)])
    line = _msg(_stop(st))
    assert "not paired: 2" in line and "the agent id was never recorded" in line, line
    assert "no agent id" not in line, line


def test_fix_ambiguous_idless_stop_keeps_the_old_wording_no_agent_id():
    st = h.fresh("s18fix_amb2")
    _seed(st, [_e(200), _e(100)])
    r = _fire(st, {"hook_event_name": "SubagentStop", "session_id": "s1", "cwd": str(st), "agent_type": T})
    line = _msg(r)
    assert "not paired: 2" in line and "no agent id" in line and "never recorded" not in line, line


def test_fix_repeated_stop_for_a_finished_agent_id_prints_duplicate_stop_ignored():
    st = h.fresh("s18fix_dup")
    _seed(st, [_e(100, "A", sources=("pre", "start"))])
    first = _msg(_stop(st, "A"))
    assert re.search(r"\d+ s\b", first) and "duplicate" not in first, first
    stop_time = _agents(st)[0]["stop"]
    second = _msg(_stop(st, "A"))
    assert "duplicate stop ignored" in second, second
    assert not re.search(r"\d+ s\b", second), second
    assert len(_agents(st)) == 1 and _agents(st)[0]["stop"] == stop_time, _agents(st)


def test_fix_busy_lock_on_start_says_start_not_recorded_the_state_is_busy():
    st = h.fresh("s18fix_busy_start")
    _seed(st, [])
    lock = st / "agents" / "active.json.lock"
    lock.write_text("99999", encoding="utf-8")
    t0 = time.time()
    r = _fire(st, {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_use_id": "tu1", "cwd": str(st),
                   "tool_name": "Agent", "tool_input": {"subagent_type": T, "prompt": "x"}})
    assert time.time() - t0 < 9
    line = _msg(r)
    assert "START not recorded: the state is busy" in line, r.all
    assert _agents(st) == [] and lock.exists(), _agents(st)


def test_fix_free_lock_start_prints_the_normal_start_line_twin():
    st = h.fresh("s18fix_free_start")
    r = _fire(st, {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_use_id": "tu1", "cwd": str(st),
                   "tool_name": "Agent", "tool_input": {"subagent_type": T, "prompt": "x"}})
    line = _msg(r)
    assert "[agent START]" in line and "not recorded" not in line and len(_agents(st)) == 1, line


class _Boom:
    """Replaces av.mutate: raises the given exception."""

    def __init__(self, exc):
        self.exc, self.orig = exc, av.mutate

    def __enter__(self):
        def boom(fn):
            raise self.exc
        av.mutate = boom
        return self

    def __exit__(self, *e):
        av.mutate = self.orig


def _capture(fn, *args):
    st = h.fresh("s18fix_cap")
    old_env = os.environ.get("HARNESS_HOOK_STATE")
    os.environ["HARNESS_HOOK_STATE"] = str(st)
    old_out, buf = sys.stdout, StringIO()
    sys.stdout = buf
    try:
        fn(*args)
    finally:
        sys.stdout = old_out
        if old_env is None:
            os.environ.pop("HARNESS_HOOK_STATE", None)
        else:
            os.environ["HARNESS_HOOK_STATE"] = old_env
    return json.loads(buf.getvalue())["systemMessage"]


def test_fix_exception_in_mutate_on_stop_names_the_exception():
    with _Boom(RuntimeError("disk gone")):
        line = _capture(av.announce_stop, {"agent_type": T, "agent_id": "Z", "session_id": "s1"}, "claude")
    assert "[agent STOP]" in line and "stop was not recorded: RuntimeError" in line, line
    assert "disk gone" not in line and "state is busy" not in line, line


def test_fix_exception_in_mutate_on_start_names_the_exception():
    with _Boom(PermissionError("denied")):
        line = _capture(av.announce_start, {"tool_input": {"subagent_type": T}, "cwd": "", "session_id": "s1",
                                            "tool_use_id": "t9"}, "claude")
    assert "[agent START]" in line and "START not recorded: PermissionError" in line, line
    assert "role " not in line, line


def test_fix_busy_lock_in_process_on_start_patched_file_lock():
    class NoLock:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return False

        def __exit__(self, *e):
            return None

    old = _agentio.file_lock
    _agentio.file_lock = NoLock
    try:
        line = _capture(av.announce_start, {"tool_input": {"subagent_type": T}, "cwd": "", "session_id": "s1",
                                            "tool_use_id": "t9"}, "claude")
    finally:
        _agentio.file_lock = old
    assert "START not recorded: the state is busy" in line, line


if __name__ == "__main__":
    sys.exit(h.run_all(globals()))
