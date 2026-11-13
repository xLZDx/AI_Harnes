"""S16-T3 tests: a SubagentStop is paired with its own start by agent_id (harness/hooks/agent_visibility.py).
Written from the S16-T3 acceptance criteria, not from the code. The hook runs as a real subprocess with a JSON payload on
stdin; HARNESS_HOOK_STATE points to a temp folder, so the real ~/.harness-hooks is never touched.
Run: python -I tests/test_s16_agent_stop_pairing.py"""
import importlib.util
import json
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
    r = h.run([h.PY, "-I", "-s", "-X", "utf8", str(HOOK), "--harness-client=claude"], state, {"HARNESS_HOOK_STATE": str(state)},
              json.dumps(payload))
    assert r.rc == 0, r.all
    return r


def _agents(state):
    f = state / "agents" / "active.json"
    return json.loads(f.read_text(encoding="utf-8"))["agents"] if f.exists() else []


def _seed(state, entries):
    d = state / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "active.json").write_text(json.dumps({"agents": entries}), encoding="utf-8")


def _e(start_ago, aid=None, typ=T, sid="s1", sources=("start",), tuid=""):
    e = {"type": typ, "model": "m", "effort": "e", "task": "active", "start": time.time() - start_ago, "project": "",
         "session": sid, "tool_use_id": tuid, "sources": list(sources)}
    if aid:
        e["agent_id"] = aid
    return e


def _start(state, aid=None, typ=T, sid="s1"):
    p = {"hook_event_name": "SubagentStart", "session_id": sid, "cwd": str(state), "agent_type": typ}
    if aid:
        p["agent_id"] = aid
    return _fire(state, p)


def _stop(state, aid=None, typ=T, sid="s1"):
    p = {"hook_event_name": "SubagentStop", "session_id": sid, "cwd": str(state), "agent_type": typ}
    if aid:
        p["agent_id"] = aid
    return _fire(state, p)


def _pre(state, typ=T, sid="s1", tuid="t1"):
    return _fire(state, {"hook_event_name": "PreToolUse", "session_id": sid, "tool_use_id": tuid, "cwd": str(state),
                         "tool_name": "Agent", "tool_input": {"subagent_type": typ}})


def _stopped(a):
    return [bool(e.get("stop")) for e in a]


# ---------------------------------------------------------------- criterion 1
def test_c1_subagent_start_records_agent_id_on_new_entry():
    st = h.fresh("s16t3_c1a")
    r = _start(st, "A1")
    a = _agents(st)
    assert len(a) == 1 and a[0]["agent_id"] == "A1" and a[0]["type"] == T, a
    assert "[agent START]" in r.all


def test_c1_subagent_start_records_agent_id_on_claimed_pre_entry():
    st = h.fresh("s16t3_c1b")
    _pre(st)
    assert "agent_id" not in _agents(st)[0]
    r = _start(st, "A1")
    a = _agents(st)
    assert len(a) == 1 and a[0]["agent_id"] == "A1", a
    assert "[agent START]" not in r.all  # claimed, not announced twice


# ---------------------------------------------------------------- criterion 2
def test_c2_incident_newer_stop_never_finishes_older_dead_run():
    st = h.fresh("s16t3_c2")
    _seed(st, [_e(5000, "OLD"), _e(10, "NEW")])  # OLD was killed by an app restart and never stopped
    _stop(st, "NEW")
    a = _agents(st)
    assert [e["agent_id"] for e in a] == ["OLD", "NEW"] and _stopped(a) == [False, True], a


def test_c2_incident_end_to_end_through_start_and_stop_events():
    st = h.fresh("s16t3_c2e")
    _start(st, "OLD")  # never stopped
    _start(st, "NEW")
    _stop(st, "NEW")
    a = _agents(st)
    assert len(a) == 2 and _stopped(a) == [False, True] and a[1]["agent_id"] == "NEW", a


def test_c2_stop_prints_duration_line_for_matched_entry():
    st = h.fresh("s16t3_c2p")
    _seed(st, [_e(5000, "OLD"), _e(10, "NEW")])
    r = _stop(st, "NEW")
    assert "[agent STOP] tester" in r.all and "start was not observed" not in r.all, r.all


# ---------------------------------------------------------------- criterion 3
def test_c3_unmatched_stop_never_finishes_entry_with_other_agent_id():
    st = h.fresh("s16t3_c3a")
    _seed(st, [_e(100, "A"), _e(50, "B")])
    r = _stop(st, "ZZZ")
    a = _agents(st)
    assert _stopped(a) == [False, False], a
    assert "start was not observed" in r.all, r.all


def test_c3_unmatched_stop_with_id_finishes_nothing_even_id_less_entry():
    st = h.fresh("s16t3_c3b")
    _seed(st, [_e(300, "A"), _e(200), _e(100)])  # entries 1 and 2 have no id
    r = _stop(st, "ZZZ")
    assert _stopped(_agents(st)) == [False, False, False]
    assert "start was not observed" in r.all, r.all


def test_c3_matched_id_wins_over_older_entry_without_id():
    st = h.fresh("s16t3_c3c")
    _seed(st, [_e(300), _e(100, "B")])
    _stop(st, "B")
    assert _stopped(_agents(st)) == [False, True]


def test_c3_duplicate_stop_finishes_nothing_with_older_id_less_entry_present():
    st = h.fresh("s16t3_c3d")
    _seed(st, [_e(300), _e(100, "A")])  # entry 0: id-less, unfinished, same type and session
    _stop(st, "A")
    assert _stopped(_agents(st)) == [False, True]
    first_stop = _agents(st)[1]["stop"]
    r = _stop(st, "A")  # duplicate stop
    a = _agents(st)
    assert _stopped(a) == [False, True] and a[1]["stop"] == first_stop, a
    assert "start was not observed" in r.all


# ---------------------------------------------------------------- criterion 4
def test_c4_stop_without_id_finishes_oldest_unfinished_of_type_and_session():
    st = h.fresh("s16t3_c4a")
    _seed(st, [_e(300, sid="other"), _e(200), _e(100), _e(50, typ="python-reviewer")])
    _stop(st)
    assert _stopped(_agents(st)) == [False, True, False, False]


def test_c4_stop_without_id_may_finish_entry_that_has_id():
    st = h.fresh("s16t3_c4b")
    _seed(st, [_e(200, "A"), _e(100, "B")])
    _stop(st)
    assert _stopped(_agents(st)) == [True, False]


def test_c4_start_without_id_claims_recent_pre_entry_by_source():
    st = h.fresh("s16t3_c4c")
    _pre(st)
    r = _start(st)
    a = _agents(st)
    assert len(a) == 1 and sorted(a[0]["sources"]) == ["pre", "start"] and "agent_id" not in a[0], a
    assert "[agent START]" not in r.all


def test_c4_start_without_id_does_not_claim_entry_older_than_120s():
    st = h.fresh("s16t3_c4d")
    _seed(st, [_e(500, sources=("pre",))])
    r = _start(st)
    assert len(_agents(st)) == 2 and "[agent START]" in r.all


def test_c4_same_tool_use_id_is_deduped():
    st = h.fresh("s16t3_c4e")
    r1 = _pre(st, tuid="same")
    r2 = _pre(st, tuid="same")
    assert len(_agents(st)) == 1 and "[agent START]" in r1.all and "[agent START]" not in r2.all


# ---------------------------------------------------------------- criterion 5
def test_c5_second_start_with_same_agent_id_adds_nothing_and_prints_nothing():
    st = h.fresh("s16t3_c5a")
    r1 = _start(st, "A")
    r2 = _start(st, "A")
    assert len(_agents(st)) == 1 and "[agent START]" in r1.all and "[agent START]" not in r2.all, _agents(st)


def test_c5_start_with_id_b_never_claims_entry_with_id_a():
    st = h.fresh("s16t3_c5b")
    _seed(st, [_e(10, "A", sources=("pre",))])  # recent, other source: claimable if it had no id
    r = _start(st, "B")
    a = _agents(st)
    assert [e.get("agent_id") for e in a] == ["A", "B"] and "[agent START]" in r.all, a
    assert a[0]["sources"] == ["pre"], a


def test_c5_start_with_id_does_not_claim_entry_of_other_session():
    st = h.fresh("s16t3_c5c")
    _seed(st, [_e(10, sid="other", sources=("pre",))])
    _start(st, "B", sid="s1")
    a = _agents(st)
    assert len(a) == 2 and "agent_id" not in a[0] and a[1]["agent_id"] == "B", a


def test_c5_start_with_id_already_on_a_finished_entry_adds_nothing():
    st = h.fresh("s16t3_c5d")
    _start(st, "A")
    _stop(st, "A")
    r = _start(st, "A")  # late duplicate start after the stop
    a = _agents(st)
    assert len(a) == 1 and _stopped(a) == [True] and "[agent START]" not in r.all, a


# ---------------------------------------------------------------- criterion 6
def test_c6_start_claims_newest_matching_entry_not_dead_runs_older_one():
    st = h.fresh("s16t3_c6")
    _seed(st, [_e(60, sources=("pre",), tuid="d"), _e(10, sources=("pre",), tuid="l")])  # D (dead run), L (live run)
    r = _start(st, "L")
    a = _agents(st)
    assert len(a) == 2 and "agent_id" not in a[0] and a[1]["agent_id"] == "L", a
    assert "[agent START]" not in r.all
    _stop(st, "L")
    a = _agents(st)
    assert _stopped(a) == [False, True], a


# ------------------------------------------- criterion 3 (refined): unrecorded id, PreToolUse-only fallback
def test_c3r_unrecorded_id_finishes_newest_pre_only_idless_entry_and_stamps_id():
    st = h.fresh("s16t3_c3r1")
    _seed(st, [_e(300, sources=("pre",), tuid="a"), _e(100, sources=("pre",), tuid="b")])
    _stop(st, "X")
    a = _agents(st)
    assert _stopped(a) == [False, True] and a[1]["agent_id"] == "X" and "agent_id" not in a[0], a


def test_c3r_incident_dead_run_reported_by_start_is_never_a_fallback_target():
    st = h.fresh("s16t3_c3r2")
    _seed(st, [_e(5000, sources=("pre", "start"), tuid="dead")])  # dead run: start seen, no id
    r = _stop(st, "X")
    a = _agents(st)
    assert _stopped(a) == [False] and "agent_id" not in a[0], a
    assert "start was not observed" in r.all


def test_c3r_fallback_skips_start_entry_but_takes_pre_only_one_even_if_older():
    st = h.fresh("s16t3_c3r3")
    _seed(st, [_e(300, sources=("pre",), tuid="live"), _e(100, sources=("pre", "start"), tuid="dead")])
    _stop(st, "X")
    a = _agents(st)
    assert _stopped(a) == [True, False] and a[0]["agent_id"] == "X", a


def test_c3r_fallback_ignores_other_type_other_session_and_entries_with_other_id():
    st = h.fresh("s16t3_c3r4")
    _seed(st, [_e(300, sources=("pre",), typ="python-reviewer"), _e(200, sources=("pre",), sid="other"),
               _e(100, "A", sources=("pre",))])
    _stop(st, "X")
    assert _stopped(_agents(st)) == [False, False, False]


def test_c3r_stamped_fallback_makes_a_second_stop_a_duplicate():
    st = h.fresh("s16t3_c3r5")
    _seed(st, [_e(300, sources=("pre",), tuid="a"), _e(100, sources=("pre",), tuid="b")])
    _stop(st, "X")
    _stop(st, "X")  # duplicate: must not take the older pre-only entry
    assert _stopped(_agents(st)) == [False, True]


# ---------------------------------------------------------------- retried PreToolUse
def test_c6b_retried_pre_finds_its_own_entry_and_never_claims_another_run():
    st = h.fresh("s16t3_c6b")
    _start(st, "aid1")
    _pre(st, tuid="T")  # claims A
    _start(st, "aid2")  # B: new entry
    a = _agents(st)
    assert len(a) == 2 and a[0]["agent_id"] == "aid1" and a[1]["agent_id"] == "aid2", a
    r = _pre(st, tuid="T")  # retry of A's PreToolUse
    a = _agents(st)
    assert len(a) == 2 and "[agent START]" not in r.all, a
    assert a[1]["agent_id"] == "aid2" and a[1]["sources"] == ["start"] and a[1]["tool_use_id"] == "", a
    _pre(st, tuid="U")  # B's own PreToolUse claims B
    a = _agents(st)
    assert len(a) == 2 and a[1]["tool_use_id"] == "U" and sorted(a[1]["sources"]) == ["pre", "start"], a


if __name__ == "__main__":
    sys.exit(h.run_all(globals()))
