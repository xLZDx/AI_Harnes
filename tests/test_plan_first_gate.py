"""Tests for harness/hooks/plan_first_gate.py (S7-T3, plan MAJOR-02 + R3-1). The hook runs as a subprocess against Claude JSONL
transcripts built here. Run: C:\\Python314\\python.exe -I tests/test_plan_first_gate.py. State lives in temp HARNESS_HOOK_STATE folders."""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hv2  # noqa: E402

HOOK = HERE.parent / "harness" / "hooks" / "plan_first_gate.py"
PY = sys.executable

GOOD_STEPS = ["Edit the parser module in src/app.py", "Run the unit tests for the parser"]
EDIT_PATH = r"D:\Repo\proj\src\app.py"


# ---------- transcript builders ----------
def human(text, ts="2026-11-12T10:00:00Z"):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}}


def asst(*blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def text(t):
    return {"type": "text", "text": t}


def use(tid, name, inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def result(tid, content="ok", is_error=None):
    b = {"type": "tool_result", "tool_use_id": tid, "content": content}
    if is_error is not None:
        b["is_error"] = is_error
    return {"type": "user", "message": {"role": "user", "content": [b]}}


def todo_input(steps):
    return {"todos": [{"content": s, "status": "pending", "activeForm": s} for s in steps]}


def plan_input(steps):
    return {"plan": [{"step": s, "status": "pending"} for s in steps]}


def write_transcript(d, rows):
    p = d / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8", newline="\n")
    return str(p)


class Env:
    """One isolated hook state folder + transcript folder."""

    def __init__(self, name):
        self.dir = hv2.fresh(name)
        self.state = self.dir / "state"

    def call(self, payload, extra_env=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("HARNESS_") and k != "CLAUDE_PLAN_FIRST_GATE"}
        env["HARNESS_HOOK_STATE"] = str(self.state)
        env.setdefault("HARNESS_METRICS_DIR", r"D:	mp\harness_v2_tests\sink\metrics")  # S8-T1
        env["PYTHONIOENCODING"] = "utf-8"
        env.update(extra_env or {})
        p = subprocess.run([PY, "-I", str(HOOK)], input=json.dumps(payload), capture_output=True, text=True,
                           encoding="utf-8", env=env, timeout=60)
        return p

    def log(self):
        f = self.state / "plan_first" / "plan_first.jsonl"
        if not f.is_file():
            return []
        return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]


def pre(tp, tool="Edit", inp=None, sid="s1", **extra):
    if inp is None:
        inp = {"file_path": EDIT_PATH, "old_string": "a", "new_string": "b"} if tool != "Bash" else {"command": "git status"}
    return {"hook_event_name": "PreToolUse", "session_id": sid, "transcript_path": tp, "tool_name": tool, "tool_input": inp, **extra}


def stop(tp, sid="s1", **extra):
    return {"hook_event_name": "Stop", "session_id": sid, "transcript_path": tp, **extra}


def denied(p):
    try:
        d = json.loads(p.stdout)
    except ValueError:
        return False
    return d.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def blocked(p):
    try:
        return json.loads(p.stdout).get("decision") == "block"
    except ValueError:
        return False


def allowed(p):
    return p.returncode == 0 and p.stdout.strip() == ""


def check(cond, msg):
    assert cond, msg


def edit_denied(rows, name, tool="Edit", inp=None):
    e = Env(name)
    p = e.call(pre(write_transcript(e.dir, rows), tool, inp))
    return e, p


# ---------- tests: deny / pass ----------
def test_first_edit_without_plan_is_denied():
    e, p = edit_denied([human("Fix the parser bug in app.py")], "nop")
    check(denied(p), f"expected deny, got rc={p.returncode} out={p.stdout!r} err={p.stderr!r}")
    check("PLAN FIRST" in p.stdout, p.stdout)
    check(any(r.get("event") == "deny" for r in e.log()), "deny not logged")


def test_plan_tool_with_success_result_passes():
    for name, inp in (("TodoWrite", todo_input(GOOD_STEPS)), ("update_plan", plan_input(GOOD_STEPS))):
        rows = [human("Fix the parser bug"), asst(use("p1", name, inp)), result("p1", "Plan shown", False)]
        e, p = edit_denied(rows, "ok-" + name)
        check(allowed(p), f"{name}: expected pass, got out={p.stdout!r} err={p.stderr!r}")


def test_plan_call_without_result_is_not_presented():
    rows = [human("Fix the parser bug"), asst(use("p1", "update_plan", plan_input(GOOD_STEPS)))]
    e, p = edit_denied(rows, "noresult")
    check(denied(p), f"plan without result must not pass: {p.stdout!r}")


def test_plan_call_with_error_result_is_not_presented():
    rows = [human("Fix the parser bug"), asst(use("p1", "update_plan", plan_input(GOOD_STEPS))),
            result("p1", "Error: tool failed", True)]
    e, p = edit_denied(rows, "errresult")
    check(denied(p), f"plan with is_error result must not pass: {p.stdout!r}")


def test_plan_call_with_empty_result_is_not_presented():
    rows = [human("Fix the parser bug"), asst(use("p1", "TodoWrite", todo_input(GOOD_STEPS))), result("p1", "", False)]
    e, p = edit_denied(rows, "emptyresult")
    check(denied(p), f"plan with empty result must not pass: {p.stdout!r}")


def _plan_case(steps, name, tool="update_plan"):
    inp = plan_input(steps)
    rows = [human("Fix the parser bug"), asst(use("p1", tool, inp)), result("p1", "Plan shown", False)]
    return edit_denied(rows, name)[1]


def test_one_step_plan_denied():
    check(denied(_plan_case(["Run the unit tests for the parser"], "onestep")), "1-step plan passed")


def test_one_line_text_plan_denied():
    rows = [human("Fix the parser bug"), asst(text("Plan:\n1. Edit the parser and run the tests afterwards"))]
    check(denied(edit_denied(rows, "oneline")[1]), "one-line plan in text passed")


def test_short_steps_denied_and_12_char_boundary():
    # "Run tests!!" = 11 chars (denied), "Run tests!!!" = 12 chars (accepted)
    check(len("Run tests!!") == 11 and len("Run tests!!!") == 12, "fixture lengths")
    check(denied(_plan_case(["Edit the parser module file", "Run tests!!"], "short11")), "11-char step passed")
    check(allowed(_plan_case(["Edit the parser module file", "Run tests!!!"], "short12")), "12-char step denied")
    check(denied(_plan_case(["Edit a.py", "Run tests"], "short-both")), "short steps passed")


def test_plan_without_verification_step_denied():
    check(denied(_plan_case(["Edit the parser module file", "Update the changelog entry now"], "noverify")),
          "plan without verification step passed")


def test_duplicate_steps_denied():
    check(denied(_plan_case(["Run the unit tests please", "Run the unit tests please"], "dups")), "duplicate steps passed")


def test_plan_as_numbered_text_before_first_change_passes():
    rows = [human("Fix the parser bug"),
            asst(text("Plan:\n1. Edit the parser module in src/app.py\n2. Run the unit tests for the parser"))]
    e, p = edit_denied(rows, "textplan")
    check(allowed(p), f"numbered text plan must pass: {p.stdout!r} {p.stderr!r}")


def test_bulleted_text_plan_passes():
    rows = [human("Fix the parser bug"),
            asst(text("Steps:\n- Edit the parser module in src/app.py\n- Verify with the unit tests"))]
    check(allowed(edit_denied(rows, "bullets")[1]), "bulleted text plan must pass")


def test_text_plan_without_verification_denied():
    rows = [human("Fix the parser bug"),
            asst(text("1. Edit the parser module in src/app.py\n2. Update the changelog entry now"))]
    check(denied(edit_denied(rows, "textnoverify")[1]), "text plan without verification passed")


def test_plan_from_previous_turn_does_not_count():
    rows = [human("Fix the parser bug", "2026-11-12T10:00:00Z"), asst(use("p1", "update_plan", plan_input(GOOD_STEPS))),
            result("p1", "Plan shown", False), asst(text("done")),
            human("Now also change the other module", "2026-11-12T11:00:00Z")]
    check(denied(edit_denied(rows, "oldplan")[1]), "plan of an earlier turn was accepted")


def test_exit_plan_mode_rejected_by_user_is_not_presented():
    """R3-1: a plan tool call needs a successful RESULT. ExitPlanMode returning an error (plan rejected) must not pass."""
    plan = "1. Edit the parser module in src/app.py\n2. Run the unit tests for the parser"
    rows = [human("Fix the parser bug"), asst(use("p1", "ExitPlanMode", {"plan": plan})),
            result("p1", "The user doesn't want to proceed with this plan", True)]
    check(denied(edit_denied(rows, "exitplan-err")[1]), "ExitPlanMode with is_error result passed")


def test_exit_plan_mode_with_success_passes():
    plan = "1. Edit the parser module in src/app.py\n2. Run the unit tests for the parser"
    rows = [human("Fix the parser bug"), asst(use("p1", "ExitPlanMode", {"plan": plan})), result("p1", "Plan approved", False)]
    check(allowed(edit_denied(rows, "exitplan-ok")[1]), "ExitPlanMode with success result denied")


# ---------- bounded release ----------
def test_three_denies_then_exception_release():
    e = Env("cap")
    tp = write_transcript(e.dir, [human("Fix the parser bug")])
    outs = [e.call(pre(tp)) for _ in range(4)]
    check([denied(p) for p in outs[:3]] == [True, True, True], [p.stdout for p in outs[:3]])
    check(allowed(outs[3]), f"4th change must be allowed, got {outs[3].stdout!r}")
    check("plan-first-exception" in outs[3].stderr, "release is not visible on stderr: " + outs[3].stderr)
    events = [r.get("event") for r in e.log()]
    check(events.count("deny") == 3 and events.count("plan-first-exception") == 1, str(events))
    check("plan-first-release" not in events and "pass" not in events, "release must not be logged as a pass: " + str(events))
    check(allowed(e.call(pre(tp))), "5th change must stay allowed within the turn")


def test_deny_counter_is_per_turn():
    e = Env("percap")
    tp = write_transcript(e.dir, [human("Fix the parser bug", "2026-11-12T10:00:00Z")])
    for _ in range(4):
        e.call(pre(tp))
    tp2 = write_transcript(e.dir, [human("Fix the parser bug", "2026-11-12T10:00:00Z"), asst(text("ok")),
                                   human("Another task entirely", "2026-11-12T12:00:00Z")])
    check(denied(e.call(pre(tp2))), "a new turn must start with a fresh deny budget")


def test_deny_counter_is_per_session():
    e = Env("persess")
    tp = write_transcript(e.dir, [human("Fix the parser bug")])
    for _ in range(4):
        e.call(pre(tp, sid="A"))
    check(denied(e.call(pre(tp, sid="B"))), "another session shares the release")


# ---------- skipped cases ----------
def test_subagent_payload_untouched():
    for extra in ({"agent_id": "ag1"}, {"agent_type": "implementation-engineer"}):
        e = Env("sub")
        tp = write_transcript(e.dir, [human("Fix the parser bug")])
        p = e.call(pre(tp, **extra))
        check(allowed(p) and not e.log(), f"{extra}: out={p.stdout!r} log={e.log()}")


def test_question_turn_untouched():
    e = Env("q")
    tp = write_transcript(e.dir, [human("Why does the parser fail on empty input?")])
    check(allowed(e.call(pre(tp))), "question turn must not be gated")
    e2 = Env("q2")  # trailing spaces after '?'
    tp2 = write_transcript(e2.dir, [human("Why does the parser fail?  \n")])
    check(allowed(e2.call(pre(tp2))), "question with trailing whitespace must not be gated")
    e3 = Env("q3")  # '?' in the middle does not exempt
    tp3 = write_transcript(e3.dir, [human("Why? Just fix it now.")])
    check(denied(e3.call(pre(tp3))), "a '?' in the middle must not exempt the turn")


def test_scratchpad_and_tmp_edits_untouched():
    safe = [r"C:\Users\u\AppData\Local\Temp\claude\abc\scratchpad\note.py", "/tmp/x.txt",
            r"C:\Users\u\AppData\Local\Temp\foo.txt"]
    for path in safe:
        e = Env("safe")
        tp = write_transcript(e.dir, [human("Fix the parser bug")])
        check(allowed(e.call(pre(tp, "Write", {"file_path": path, "content": "x"}))), f"temp path gated: {path}")
    e = Env("notsafe")
    tp = write_transcript(e.dir, [human("Fix the parser bug")])
    check(denied(e.call(pre(tp, "Write", {"file_path": r"D:\Repo\tmpfoo\a.py", "content": "x"}))),
          "folder 'tmpfoo' must not count as tmp")


def test_read_only_shell_untouched():
    for cmd in ("git status", "git log --oneline -5", "git diff HEAD~1", "ls -la", "cat src/app.py | grep def",
                "git status 2>&1", "ls > /dev/null"):
        e = Env("ro")
        tp = write_transcript(e.dir, [human("Fix the parser bug")])
        check(allowed(e.call(pre(tp, "Bash", {"command": cmd}))), f"read-only command gated: {cmd}")


def test_mutating_shell_denied():
    for cmd in ("git commit -m 'x'", "echo hi > out.txt", "git add -A", "rm -rf build", "git push origin main",
                "cat a.txt >> b.txt", "python tools/ledger.py start S7-T3", "cd proj && git commit -am x"):
        e = Env("mut")
        tp = write_transcript(e.dir, [human("Fix the parser bug")])
        check(denied(e.call(pre(tp, "Bash", {"command": cmd}))), f"mutating command not gated: {cmd}")


def test_mutating_shell_passes_with_plan():
    rows = [human("Fix the parser bug"), asst(use("p1", "update_plan", plan_input(GOOD_STEPS))), result("p1", "Plan shown", False)]
    e, p = edit_denied(rows, "mutplan", "Bash", {"command": "git commit -m x"})
    check(allowed(p), f"commit with a presented plan denied: {p.stdout!r}")


def test_read_tools_untouched():
    e = Env("readtool")
    tp = write_transcript(e.dir, [human("Fix the parser bug")])
    check(allowed(e.call(pre(tp, "Read", {"file_path": EDIT_PATH}))), "Read gated")
    check(allowed(e.call(pre(tp, "Grep", {"pattern": "x"}))), "Grep gated")


def test_kill_switch_off():
    e = Env("kill")
    tp = write_transcript(e.dir, [human("Fix the parser bug")])
    check(allowed(e.call(pre(tp), {"CLAUDE_PLAN_FIRST_GATE": "off"})), "kill switch did not disable PreToolUse")
    e2 = Env("kill2")
    tp2 = write_transcript(e2.dir, [human("Fix it"), asst(use("e1", "Edit", {"file_path": EDIT_PATH}))])
    check(allowed(e2.call(stop(tp2), {"CLAUDE_PLAN_FIRST_GATE": "off"})), "kill switch did not disable Stop")


def test_unreadable_transcript_fails_open():
    e = Env("noscript")
    check(allowed(e.call(pre(str(e.dir / "missing.jsonl")))), "missing transcript must fail open")
    check(allowed(e.call(pre(""))), "empty transcript path must fail open")


def test_garbage_stdin_fails_open():
    e = Env("garbage")
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARNESS_")}
    env["HARNESS_HOOK_STATE"] = str(e.state)
    env.setdefault("HARNESS_METRICS_DIR", r"D:	mp\harness_v2_tests\sink\metrics")  # S8-T1
    p = subprocess.run([PY, "-I", str(HOOK)], input="not json {{", capture_output=True, text=True, env=env, timeout=60)
    check(p.returncode == 0 and p.stdout.strip() == "", f"rc={p.returncode} out={p.stdout!r}")


# ---------- Stop backup ----------
def _changed_rows(extra=None):
    rows = [human("Fix the parser bug"), asst(use("e1", "Edit", {"file_path": EDIT_PATH, "old_string": "a", "new_string": "b"})),
            result("e1", "edited", False), asst(text("Done."))]
    return rows + (extra or [])


def test_stop_blocks_once_for_changes_without_plan():
    e = Env("stop1")
    tp = write_transcript(e.dir, _changed_rows())
    p1 = e.call(stop(tp))
    check(blocked(p1), f"first Stop must block: {p1.stdout!r}")
    check("PLAN FIRST" in p1.stdout, p1.stdout)
    p2 = e.call(stop(tp))
    check(allowed(p2), f"second Stop in the same turn must pass: {p2.stdout!r}")
    check(any(r.get("event") == "stop-block" for r in e.log()), "stop-block not logged")


def test_stop_block_resets_on_new_turn():
    e = Env("stop2")
    e.call(stop(write_transcript(e.dir, _changed_rows())))
    rows = _changed_rows()
    rows[0] = human("A different request entirely", "2026-11-12T15:00:00Z")
    check(blocked(e.call(stop(write_transcript(e.dir, rows)))), "new turn must be blockable again")


def test_stop_passes_when_plan_presented():
    e = Env("stop3")
    rows = [human("Fix the parser bug"), asst(use("p1", "update_plan", plan_input(GOOD_STEPS))), result("p1", "Plan shown", False)]
    rows += _changed_rows()[1:]
    check(allowed(e.call(stop(write_transcript(e.dir, rows)))), "Stop blocked although a plan was presented")


def test_stop_passes_for_read_only_turn():
    e = Env("stop4")
    rows = [human("Look at the parser"), asst(use("r1", "Read", {"file_path": EDIT_PATH})), result("r1", "code", False), asst(text("It is fine."))]
    check(allowed(e.call(stop(write_transcript(e.dir, rows)))), "Stop blocked a read-only turn")


def test_stop_ignores_plan_written_only_after_the_change():
    e = Env("stop5")
    rows = _changed_rows([asst(text("1. Edit the parser module in src/app.py\n2. Run the unit tests for the parser"))])
    check(blocked(e.call(stop(write_transcript(e.dir, rows)))), "plan text written after the change must not count")


def test_stop_question_turn_and_subagent_untouched():
    e = Env("stop6")
    rows = _changed_rows()
    rows[0] = human("Why is the parser slow?")
    check(allowed(e.call(stop(write_transcript(e.dir, rows)))), "question turn blocked at Stop")
    e2 = Env("stop7")
    check(allowed(e2.call(stop(write_transcript(e2.dir, _changed_rows()), agent_id="ag1"))), "subagent blocked at Stop")


def test_stop_scratchpad_only_change_untouched():
    e = Env("stop8")
    rows = [human("Fix the parser bug"),
            asst(use("e1", "Write", {"file_path": r"C:\Users\u\AppData\Local\Temp\claude\x\scratchpad\n.txt", "content": "x"})),
            result("e1", "ok", False), asst(text("Done."))]
    check(allowed(e.call(stop(write_transcript(e.dir, rows)))), "scratchpad-only turn blocked at Stop")


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
