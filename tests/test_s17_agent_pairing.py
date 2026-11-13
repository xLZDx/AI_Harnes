"""S17-T3 tests (B33, B34): harness/hooks/agent_visibility.py. Written from the S17-T3 acceptance criteria, not from the code.
1. A PreToolUse retried after its run stopped (same tool_use_id on a finished entry) adds no entry and prints no START line.
2. A stop without agent_id counts EVERY unfinished entry of its type and session (with or without an id) for ambiguity and
   finishes only a single candidate that has no agent_id. One candidate that carries an id: nothing finished, 'not paired: 1'.
   Two or more: nothing finished, 'not paired: N'. Zero: nothing finished, 'start was not observed'.
3. The not-paired STOP line has no duration (no ' s |').
The hook runs as a real subprocess with JSON on stdin; HARNESS_HOOK_STATE is a temp folder.
Run: C:\\Python314\\python.exe -I tests/test_s17_agent_pairing.py"""
import importlib.util
import json
import re
import sys
import time
from pathlib import Path

_s = importlib.util.spec_from_file_location("hv2", Path(__file__).resolve().parent / "hv2.py")
h = importlib.util.module_from_spec(_s)
sys.modules["hv2"] = h
_s.loader.exec_module(h)

HOOK = h.SRC / "harness" / "hooks" / "agent_visibility.py"
T = "tester"


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


def _e(start_ago, aid=None, typ=T, sid="s1", tuid="", stop=None):
    e = {"type": typ, "model": "m", "effort": "e", "task": "active", "start": time.time() - start_ago, "project": "",
         "session": sid, "tool_use_id": tuid, "sources": ["start"]}
    if aid:
        e["agent_id"] = aid
    if stop:
        e["stop"] = stop
    return e


def _pre(state, tuid="t1", typ=T, sid="s1"):
    return _fire(state, {"hook_event_name": "PreToolUse", "session_id": sid, "tool_use_id": tuid, "cwd": str(state),
                         "tool_name": "Agent", "tool_input": {"subagent_type": typ}})


def _stop(state, aid=None, typ=T, sid="s1"):
    p = {"hook_event_name": "SubagentStop", "session_id": sid, "cwd": str(state), "agent_type": typ}
    if aid:
        p["agent_id"] = aid
    return _fire(state, p)


def _stopped(a):
    return [bool(e.get("stop")) for e in a]


# ---------------------------------------------------------------- criterion 1
def test_c1_pretooluse_retry_after_stop_adds_no_entry_and_no_start_line():
    st = h.fresh("s17t3_c1a")
    r1 = _pre(st, "t1")
    assert "[agent START]" in r1.all
    _stop(st)  # one id-less candidate: finished
    before = _agents(st)
    assert _stopped(before) == [True], before
    r2 = _pre(st, "t1")  # the retry
    assert _agents(st) == before, _agents(st)
    assert "[agent START]" not in r2.all, r2.all


def test_c1_retry_after_stop_of_seeded_finished_entry():
    st = h.fresh("s17t3_c1b")
    _seed(st, [_e(100, tuid="t9", stop=time.time() - 10)])
    r = _pre(st, "t9")
    assert len(_agents(st)) == 1 and "[agent START]" not in r.all, (_agents(st), r.all)


def test_c1_other_tool_use_id_after_stop_is_a_new_run():
    st = h.fresh("s17t3_c1c")
    _seed(st, [_e(100, tuid="t9", stop=time.time() - 10)])
    r = _pre(st, "t10")
    a = _agents(st)
    assert len(a) == 2 and a[1]["tool_use_id"] == "t10" and not a[1].get("stop") and "[agent START]" in r.all, (a, r.all)


# ---------------------------------------------------------------- criterion 2
def test_c2_one_idless_candidate_is_finished():
    st = h.fresh("s17t3_c2a")
    _seed(st, [_e(200)])
    r = _stop(st)
    assert _stopped(_agents(st)) == [True], _agents(st)
    assert "not paired" not in r.all and "start was not observed" not in r.all, r.all


def test_c2_one_idless_finished_when_other_types_and_sessions_hold_entries():
    st = h.fresh("s17t3_c2a2")
    _seed(st, [_e(300, "A", sid="s2"), _e(250, typ="other"), _e(200), _e(100, "B", sid="s2")])
    r = _stop(st)  # only the s1 'tester' entry is a candidate
    assert _stopped(_agents(st)) == [False, False, True, False], _agents(st)
    assert "not paired" not in r.all, r.all


def test_c2_idless_stop_counts_id_bearing_entries_for_ambiguity_nothing_finished():
    st = h.fresh("s17t3_c2b")
    _seed(st, [_e(300, "A"), _e(200), _e(100), _e(50, "B")])  # 2 id-less + 2 id-bearing = 4 candidates
    r = _stop(st)
    assert _stopped(_agents(st)) == [False] * 4, _agents(st)
    assert "not paired: 4 unfinished tester runs and no agent id" in r.all, r.all


def test_c2_one_idless_next_to_two_id_bearing_is_ambiguous_nothing_finished():
    st = h.fresh("s17t3_c2b2")
    _seed(st, [_e(300, "A"), _e(200), _e(100, "B")])
    r = _stop(st)
    assert _stopped(_agents(st)) == [False, False, False], _agents(st)
    assert "not paired: 3 unfinished" in r.all, r.all


def test_c2_three_idless_candidates_not_paired_three():
    st = h.fresh("s17t3_c2c")
    _seed(st, [_e(300), _e(200), _e(100)])
    r = _stop(st)
    assert _stopped(_agents(st)) == [False] * 3 and "not paired: 3" in r.all, (_agents(st), r.all)


def test_c2_two_id_bearing_entries_idless_stop_not_paired_two():
    st = h.fresh("s17t3_c2d")
    _seed(st, [_e(200, "A"), _e(100, "B")])
    r = _stop(st)
    assert _stopped(_agents(st)) == [False, False], _agents(st)
    assert "not paired: 2" in r.all and "start was not observed" not in r.all, r.all


def test_c2_single_id_bearing_entry_is_not_finished_by_idless_stop_not_paired_one():
    st = h.fresh("s17t3_c2e")
    _seed(st, [_e(100, "A")])
    r = _stop(st)
    assert _stopped(_agents(st)) == [False], _agents(st)
    assert "not paired: 1" in r.all and "start was not observed" not in r.all, r.all
    assert not [e for e in _agents(st) if e.get("stop")], _agents(st)


def test_c2_dead_idless_plus_live_id_bearing_idless_stop_finishes_neither():
    st = h.fresh("s17t3_c2h")
    _seed(st, [_e(900), _e(30, "LIVE")])  # a dead id-less run (never stopped) and a live run that has an id
    r = _stop(st)
    assert _stopped(_agents(st)) == [False, False], _agents(st)  # the dead one must not take the live run's stop
    assert "not paired: 2 unfinished tester runs and no agent id" in r.all, r.all
    assert not re.search(r"\d+ s\b", json.loads(r.out)["systemMessage"]), r.all


def test_c2_zero_candidates_empty_state_says_start_not_observed():
    st = h.fresh("s17t3_c2f")
    r = _stop(st)
    assert "start was not observed" in r.all and "not paired" not in r.all, r.all


def test_c2_id_bearing_stop_still_finishes_its_own_entry():
    st = h.fresh("s17t3_c2g")
    _seed(st, [_e(200), _e(100, "A")])
    _stop(st, "A")
    assert _stopped(_agents(st)) == [False, True], _agents(st)
    r = _stop(st)  # now the id-less entry is the only unfinished candidate
    assert _stopped(_agents(st)) == [True, True] and "not paired" not in r.all, (_agents(st), r.all)


# ---------------------------------------------------------------- criterion 3
def test_c3_not_paired_line_has_no_duration():
    st = h.fresh("s17t3_c3a")
    _seed(st, [_e(300), _e(200)])
    line = json.loads(_stop(st).out)["systemMessage"]
    assert line.startswith("[agent STOP] tester | not paired: 2"), line
    assert " s |" not in line and not re.search(r"\d+ s\b", line), line


def test_c3_paired_stop_line_still_has_duration():
    st = h.fresh("s17t3_c3b")
    _seed(st, [_e(100)])
    line = json.loads(_stop(st).out)["systemMessage"]
    assert line.startswith("[agent STOP] tester | ") and re.search(r"\d+ s\b", line) and "not paired" not in line, line


if __name__ == "__main__":
    sys.exit(h.run_all(globals()))
