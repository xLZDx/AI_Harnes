"""S18-T5 tests (B14, B15): harness/hooks/rules_gate.py and session_close_gate.py.
1. rules_gate: the body of a python heredoc that holds a $(...) substitution keeps it live (the gate sees `npm test` inside)
   for each spelling that starts a shell or process; a harmless body (print only) stays neutralised.
2. session_close_gate.pending_agents_aged: the interim phrase counts only in <summary>/<status>, not in <result>; a finished
   id never counts as running again (also across calls, via finished_agents.json {id: time} next to the state file, never in
   the shared state file); a block with neither <summary> nor <status> is malformed and keeps the agent pending; an explicit
   resumedAgentId takes the id out of the finished set; the file keeps the 500 newest, is written only when changed and a
   corrupt file loads as empty.
3. rules_gate.PY_SHELLS_OUT_RE (review fix round): the spellings that start a process match, plain prose does not.
Both modules run in-process; the gate state lives in a temp folder.
Run: C:\\Python314\\python.exe -I tests/test_s18_gates.py"""
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

_s = importlib.util.spec_from_file_location("hv2", Path(__file__).resolve().parent / "hv2.py")
h = importlib.util.module_from_spec(_s)
sys.modules["hv2"] = h
_s.loader.exec_module(h)

_STATE = h.fresh("s18t5_state")
os.environ["HARNESS_HOOK_STATE"] = str(_STATE)  # before the gate modules read it at import time
sys.path.insert(0, str(h.SRC / "harness" / "hooks"))
import rules_gate as rg  # noqa: E402
import session_close_gate as scg  # noqa: E402

CWD = str(h.fresh("s18t5_cwd"))
_REAL_STATE, _REAL_FINISHED = scg.STATE, scg.FINISHED  # the values the module chose at import (before any test redirects them)


# ---------------------------------------------------------------- rules_gate
SPELLINGS = {
    "from os import system": "from os import system",
    "__import__('os')": "__import__('os').system('true')",
    "os.execv": "import os\nos.execv('/bin/sh', ['sh'])",
    "os.spawnl": "import os\nos.spawnl(os.P_WAIT, 'x', 'x')",
    "pty.spawn": "import pty\npty.spawn('sh')",
    "ctypes": "import ctypes",
    "importlib.import_module": "import importlib\nimportlib.import_module('os')",
    "exec(": "exec('x = 1')",
    "eval(": "eval('1')",
}


def _heredoc(body_lines):
    return "python - <<'EOF'\n" + "\n".join(body_lines) + "\nEOF"


def _kinds(cmd):
    return [k for k, _d, _c in rg.analyze(cmd, CWD)]


def test_each_shell_start_spelling_keeps_the_substitution_live():
    for name, line in SPELLINGS.items():
        cmd = _heredoc([line, 'print("$(npm test)")'])
        kept = rg.strip_heredocs(cmd)
        assert "$(npm test)" in kept, (name, kept)
        assert "full-test" in _kinds(cmd), (name, _kinds(cmd))


def test_harmless_body_stays_neutralised():
    cmd = _heredoc(['print("$(npm test)")'])
    kept = rg.strip_heredocs(cmd)
    assert "$(npm test)" not in kept and "$ (npm test)" in kept, kept
    assert "full-test" not in _kinds(cmd), _kinds(cmd)


def test_spelling_without_a_substitution_is_not_denied():
    for name, line in SPELLINGS.items():
        cmd = _heredoc([line, 'print("hello")'])
        assert not _kinds(cmd), (name, _kinds(cmd))


def test_older_spellings_still_keep_the_substitution():
    for line in ('import os; os.system("true")', 'import subprocess; subprocess.run("true", shell=True)'):
        assert "full-test" in _kinds(_heredoc([line, 'print("$(npm test)")'])), line


SHELLS_OUT_POSITIVE = [
    "from os import *", "from os import system", "from os import (system)", "os.posix_spawn('/bin/sh', [], {})",
    "execl('/bin/sh', 'sh')", "execv('/bin/sh', ['sh'])", "import os as o", "import posix", "pty.spawn(x)", "import pty",
    "commands.getoutput('ls')", "import pexpect", "import sh", "from sh import ls", "import plumbum", "import runpy",
    "import ctypes", "os.system('x')", "exec(x)", "os.popen('x')", "os.execvp('a', ['a'])", "subprocess.run(['a'])",
]
SHELLS_OUT_NEGATIVE = [
    "commands", "pty", "print(1)", "the commands to run are listed below", "a pty is a pseudo terminal",
    "x = 1 + 2", "import json", "import os", "from os import path", "import shutil", "shell script", "plumbing",
]


def test_shells_out_regex_matches_every_process_starting_spelling():
    miss = [t for t in SHELLS_OUT_POSITIVE if not rg.PY_SHELLS_OUT_RE.search(t)]
    assert not miss, miss


def test_shells_out_regex_ignores_plain_prose_and_harmless_code():
    hit = [t for t in SHELLS_OUT_NEGATIVE if rg.PY_SHELLS_OUT_RE.search(t)]
    assert not hit, hit


def test_heredoc_with_from_os_import_star_keeps_the_substitution_live_end_to_end():
    cmd = _heredoc(["from os import *", 'print("$(npm test)")'])
    assert "$(npm test)" in rg.strip_heredocs(cmd)
    assert "full-test" in _kinds(cmd), _kinds(cmd)
    twin = _heredoc(["from os import path", 'print("$(npm test)")'])  # twin: a harmless import is neutralised
    assert "$(npm test)" not in rg.strip_heredocs(twin) and "full-test" not in _kinds(twin)


def test_heredoc_with_each_new_spelling_keeps_the_substitution_live():
    for line in ("import os as o", "import posix", "execl('/bin/sh', 'sh')", "import pexpect", "from sh import ls",
                 "import plumbum", "import runpy", "commands.getoutput('x')", "os.posix_spawn('a', [], {})"):
        kept = rg.strip_heredocs(_heredoc([line, 'print("$(npm test)")']))
        assert "$(npm test)" in kept, (line, kept)


def test_heredoc_whose_body_only_mentions_commands_or_pty_in_prose_stays_neutralised():
    cmd = _heredoc(["# the commands run in a pty later", 'print("$(npm test)")'])
    kept = rg.strip_heredocs(cmd)
    assert "$(npm test)" not in kept and "full-test" not in _kinds(cmd), kept


# ---------------------------------------------------------------- session_close_gate
def _stamp(secs_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - secs_ago))


def _result_row(text, ts=None):
    row = {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "u1", "content": text}]}}
    if ts:
        row["timestamp"] = ts
    return row


def _launch(agent="agentX1", ago=600):
    return _result_row("Async agent launched successfully.\nagentId: " + agent + " (internal ID)", _stamp(ago))


def _notice(summary, ago, agent="agentX1", result=None):
    text = ("<task-notification><task-id>" + agent + "</task-id><status>completed</status><summary>" + summary + "</summary>"
            + ("<result>" + result + "</result>" if result is not None else "") + "</task-notification>")
    return {"type": "user", "timestamp": _stamp(ago), "message": {"role": "user", "content": text}}


def _raw_notice(inner, ago, agent="agentX1"):
    text = "<task-notification><task-id>" + agent + "</task-id>" + inner + "</task-notification>"
    return {"type": "user", "timestamp": _stamp(ago), "message": {"role": "user", "content": text}}


def _resume(ago, agent="agentX1"):
    return _result_row('{"success":true,"resumedAgentId":"' + agent + '"}', _stamp(ago))


INTERIM = "Agent y is waiting on its own background work"


def _use_state(name):
    d = h.fresh("s18t5_" + name)
    scg.STATE = d / "session_close_state.json"
    scg.FINISHED = d / "finished_agents.json"
    return d


def _write_transcript(d, rows, fname="t.jsonl"):
    f = d / fname
    f.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8", newline="\n")
    return f


def _pending(rows, finished=None, name="t"):
    """Run pending_agents_aged on a temp transcript with fresh saved state (optionally preloaded finished ids:
    a list is stamped 'now - 1 h', a dict is {id: time})."""
    d = _use_state(name)
    if finished is not None:
        m = finished if isinstance(finished, dict) else {a: time.time() - 3600 for a in finished}
        scg.FINISHED.write_text(json.dumps(m), encoding="utf-8")
    return [a for a, _ in scg.pending_agents_aged(str(_write_transcript(d, rows)))]


def _saved_finished():
    return json.loads(scg.FINISHED.read_text(encoding="utf-8"))


def test_finished_file_lives_next_to_the_state_file_and_is_named_finished_agents_json():
    assert _REAL_FINISHED.name == "finished_agents.json", _REAL_FINISHED
    assert _REAL_FINISHED.parent == _REAL_STATE.parent, (_REAL_FINISHED, _REAL_STATE)


def test_interim_phrase_only_in_result_does_not_count_as_running():
    rows = [_launch(), _notice("Agent y finished", 5, result="it said: " + INTERIM)]
    assert _pending(rows, name="res") == []


def test_interim_phrase_in_summary_counts_as_running():
    assert _pending([_launch(), _notice(INTERIM, 5)], name="sum") == ["agentX1"]


def test_interim_phrase_in_summary_with_unrelated_result_counts():
    assert _pending([_launch(), _notice(INTERIM, 5, result="partial output")], name="sum2") == ["agentX1"]


def test_interim_phrase_in_status_tag_counts_as_running():
    row = _raw_notice("<status>" + INTERIM + "</status>", 5)
    assert _pending([_launch(), row], name="status") == ["agentX1"]


def test_finished_id_never_counts_as_running_after_a_later_interim_notice():
    rows = [_launch(), _notice("Agent y finished", 60), _notice(INTERIM, 5)]
    assert _pending(rows, name="fin") == []
    assert "agentX1" in _saved_finished(), _saved_finished()


def test_finished_file_holds_id_to_unix_time_pairs():
    t0 = time.time()
    _pending([_launch(), _notice("done", 60)], name="shape")
    saved = _saved_finished()
    assert list(saved) == ["agentX1"] and isinstance(saved["agentX1"], (int, float)), saved
    assert t0 - 5 <= saved["agentX1"] <= time.time() + 5, saved


def test_finished_id_stays_finished_across_calls_even_when_only_the_interim_notice_is_left():
    assert _pending([_launch(), _notice("Agent y finished", 60)], name="fin2") == []
    f = _write_transcript(scg.STATE.parent, [_launch(), _notice(INTERIM, 5)], "t2.jsonl")  # later transcript tail
    assert scg.pending_agents_aged(str(f)) == []


def test_interim_notice_of_another_agent_is_not_affected_by_a_finished_one():
    rows = [_launch(), _launch("agentZ9"), _notice("done", 60), _notice(INTERIM, 5, "agentZ9")]
    assert _pending(rows, name="two") == ["agentZ9"]


def test_status_without_summary_that_is_not_interim_marks_finished():
    assert _pending([_launch(), _raw_notice("<status>completed</status>", 5)], name="stat_fin") == []
    assert "agentX1" in _saved_finished()


def test_summary_that_is_not_interim_marks_finished():
    assert _pending([_launch(), _raw_notice("<summary>All done</summary>", 5)], name="sum_fin") == []
    assert "agentX1" in _saved_finished()


def test_malformed_block_without_summary_or_status_keeps_the_agent_pending():
    for i, inner in enumerate(("", "<result>text only</result>", "<summary>unclosed", "garbage")):
        rows = [_launch(), _raw_notice(inner, 5)]
        assert _pending(rows, name="mal%d" % i) == ["agentX1"], inner
        assert not scg.FINISHED.exists(), (inner, scg.FINISHED.read_text(encoding="utf-8"))


def test_malformed_block_before_a_good_one_does_not_prevent_the_finish():
    rows = [_launch(), _raw_notice("", 30), _notice("done", 5)]
    assert _pending(rows, name="mal_then_good") == []
    assert "agentX1" in _saved_finished()


def test_resumed_agent_id_is_removed_from_the_finished_set():
    rows = [_launch(ago=900), _resume(100)]
    assert _pending(rows, finished=["agentX1"], name="res1") == ["agentX1"]  # preloaded as finished, then resumed
    assert "agentX1" not in _saved_finished(), _saved_finished()  # the id left the file too
    assert _pending([_launch(ago=900)], finished=["agentX1"], name="res2") == []  # twin: no resume, still finished
    assert "agentX1" in _saved_finished()


def test_resume_then_later_interim_notice_counts_again():
    rows = [_launch(ago=900), _notice("done", 600), _resume(300), _notice(INTERIM, 5)]
    assert _pending(rows, name="res3") == ["agentX1"]


def test_finished_file_keeps_the_500_newest_and_evicts_the_oldest_by_time():
    now = time.time()
    old = {"a%04d" % i: now - 100000 + i for i in range(600)}  # a0000 oldest ... a0599 newest, all older than 'zz'
    assert _pending([_launch("zz", 300), _notice("done", 60, "zz")], finished=old, name="cap") == []
    saved = _saved_finished()
    assert len(saved) == 500, len(saved)
    assert "zz" in saved and "a0599" in saved and "a0100" not in saved and "a0000" not in saved, sorted(saved)[:3]
    assert "a0101" in saved  # 601 ids, the 101 oldest dropped: a0000..a0100 gone, a0101 is the oldest kept


def test_finished_file_below_the_cap_keeps_every_id():
    old = ["a%04d" % i for i in range(10)]
    _pending([_launch("zz", 300), _notice("done", 60, "zz")], finished=old, name="cap2")
    assert set(_saved_finished()) == set(old) | {"zz"}


def test_finished_file_is_written_only_when_it_changed():
    d = _use_state("once")
    calls = []
    orig = scg._save_finished
    scg._save_finished = lambda m: (calls.append(dict(m)), orig(m))[1]
    try:
        f = _write_transcript(d, [_launch(), _notice("done", 60)])
        scg.pending_agents_aged(str(f))
        assert len(calls) == 1, calls
        first = scg.FINISHED.read_text(encoding="utf-8")
        scg.pending_agents_aged(str(f))  # same transcript again: nothing new
        scg.pending_agents_aged(str(f))
        assert len(calls) == 1, calls
        assert scg.FINISHED.read_text(encoding="utf-8") == first
        d2 = _use_state("once2")  # a transcript with nothing finished never creates the file
        scg.pending_agents_aged(str(_write_transcript(d2, [_launch(), _notice(INTERIM, 5)])))
        assert len(calls) == 1 and not scg.FINISHED.exists(), calls
    finally:
        scg._save_finished = orig


def test_finished_ids_are_never_written_into_the_shared_state_file():
    d = _use_state("noshare")
    scg.save_state({"marker": 7})
    scg.pending_agents_aged(str(_write_transcript(d, [_launch(), _notice("done", 60)])))
    state = json.loads(scg.STATE.read_text(encoding="utf-8"))
    assert state == {"marker": 7}, state  # untouched, no finished_agents key
    assert "agentX1" in _saved_finished()


def test_old_finished_agents_key_in_the_state_file_is_ignored():
    d = _use_state("legacy")
    scg.save_state({"finished_agents": ["agentX1"]})
    got = [a for a, _ in scg.pending_agents_aged(str(_write_transcript(d, [_launch(), _notice(INTERIM, 5)])))]
    assert got == ["agentX1"], got


def test_corrupt_finished_file_loads_as_empty_and_is_replaced_on_the_next_change():
    for i, junk in enumerate(("{not json", "", "[1, 2, 3]", '"text"', '{"a": "not-a-number"}', "null")):
        d = _use_state("corrupt%d" % i)
        scg.FINISHED.write_text(junk, encoding="utf-8")
        got = [a for a, _ in scg.pending_agents_aged(str(_write_transcript(d, [_launch()])))]
        assert got == ["agentX1"], junk
    d = _use_state("corrupt_fix")
    scg.FINISHED.write_text("{not json", encoding="utf-8")
    assert scg.pending_agents_aged(str(_write_transcript(d, [_launch(), _notice("done", 5)]))) == []
    assert list(_saved_finished()) == ["agentX1"]


def test_unwritable_finished_file_does_not_break_the_answer():
    d = _use_state("ro")
    scg.FINISHED.mkdir()  # a directory where the file should go: the write fails
    got = scg.pending_agents_aged(str(_write_transcript(d, [_launch(), _notice("done", 5)])))
    assert got == [], got  # the transcript replay alone still decides


if __name__ == "__main__":
    sys.exit(h.run_all(globals()))
