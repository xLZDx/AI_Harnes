"""S14 incident-hardening tests: T5 (hook-child isolation), T6 (approve only from a real PM Bridge APPROVE, reopen),
T7 (verify pass / review accepted need a recorded agent run). Written from the S14 acceptance criteria, not from the code.
Every refused case has an accepted twin. All state is temp: a temp git repo, and HARNESS_LEDGER, HARNESS_HOOK_STATE and
HARNESS_PM_BRIDGE_MESSAGES point at temp files under D:\\tmp\\harness_v2_tests. No real claude is run.
Run: C:\\Python314\\python.exe -I tests/test_s14_incident.py"""
import calendar
import shutil
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hv2  # noqa: E402
import test_s13_enforcement as enf  # noqa: E402  (make_gate_repo, gate)

PY = hv2.PY
HOOKS = hv2.SRC / "harness" / "hooks"
RUNNER = HOOKS / "run_hook.py"
T0 = calendar.timegm((2026, 1, 1, 0, 0, 0))  # epoch of the code commit; independent of the code under test


def git_date(epoch):
    return {"GIT_AUTHOR_DATE": f"{epoch} +0000", "GIT_COMMITTER_DATE": f"{epoch} +0000"}


class Proj:
    """Temp repo + temp ledger/agent-state/messages files + one open sprint S1 with task S1-T1."""

    def __init__(self, name):
        self.repo = hv2.mk_repo(name)
        self.root = hv2.fresh(name + "-state")
        self.ledger_file = self.root / "ledger.json"
        self.state = self.root / "state"
        self.messages = self.root / "messages.jsonl"
        self.env = {"HARNESS_LEDGER": str(self.ledger_file), "HARNESS_HOOK_STATE": str(self.state),
                    "HARNESS_PM_BRIDGE_MESSAGES": str(self.messages)}
        self.led("sprint-add", "--id", "S1", "--title", "s")
        self.led("task-add", "--title", "t1")
        self.messages.write_text("", encoding="utf-8")

    def led(self, *args, ok=True, inp=None):
        r = hv2.ledger(self.repo, *args, env=self.env, inp=inp)
        if ok:
            assert r.rc == 0, r.all
        return r

    def commit(self, rel, content, epoch, task="S1-T1"):
        hv2.commit(self.repo, rel, content, "work", f"Task: {task}", env=git_date(epoch))
        self.led("commit-hook")

    def data(self):
        return json.loads(self.ledger_file.read_text(encoding="utf-8"))

    def task(self):
        return self.data()["sprints"][0]["tasks"][0]

    def agents(self, *runs):
        """Write the agent state; an entry without a "project" key gets this repo (the hook records it since S14-T7)."""
        d = self.state / "agents"
        d.mkdir(parents=True, exist_ok=True)
        full = [{"project": str(self.repo.resolve()), **r} for r in runs]
        (d / "active.json").write_text(json.dumps({"agents": full}), encoding="utf-8")

    def head(self):
        return hv2.head(self.repo)


def run_entry(typ, start, stop="auto"):
    return {"type": typ, "start": start, "stop": (start + 100) if stop == "auto" else stop}


def refused(r):
    return r.rc != 0


# ------------------------------------------------------------------ T5
def stop_call(child):
    root = hv2.fresh("s14-t5-stop")
    proj = root / "proj"
    (proj / ".harness").mkdir(parents=True)
    task = {"id": "S9-T1", "title": "open task", "criteria": [], "run": "", "risk": "local", "journey": False, "state": "todo",
            "commits": [], "verify": None, "review": None, "routing": None, "tokens": None}
    led = {"version": 1, "project": "proj", "approval": None, "events": [],
           "sprints": [{"id": "S9", "title": "s", "branch": "b", "state": "open", "opened": "2026-11-12T00:00:00+02:00", "tasks": [task]}]}
    (proj / ".harness" / "ledger.json").write_text(json.dumps(led), encoding="utf-8")
    payload = {"hook_event_name": "Stop", "session_id": "s14t5", "cwd": str(proj),
               "last_assistant_message": "I finished the part I could do and I am stopping here."}
    env = {"HARNESS_HOOK_STATE": str(root / "state"), "HARNESS_HOOK_CONFIG": str(root / "none.json"),
           "SESSION_CLOSE_GATE_MODE": "live"}
    if child is not None:
        env["HARNESS_HOOK_CHILD"] = child
    return hv2.run([PY, "-I", "-s", "-X", "utf8", str(RUNNER), "session_close_gate.py"], root, env, json.dumps(payload))


def test_t5_run_hook_without_child_env_still_blocks_a_stop_with_open_tasks():
    r = stop_call(None)
    assert r.rc == 0, r.all
    assert json.loads(r.out.strip().splitlines()[-1]).get("decision") == "block", (r.out, r.err)


def test_t5_run_hook_with_child_env_runs_no_hook():
    r = stop_call("1")
    assert r.rc == 0 and r.out.strip() == "", (r.rc, r.out, r.err)
    # twin: any other value is not a child, the same payload blocks again
    r = stop_call("0")
    assert r.rc == 0 and '"block"' in r.out, (r.out, r.err)


class FakeProc:
    """Stands in for subprocess.Popen: records argv/kwargs, returns canned output or raises TimeoutExpired once."""
    seen = {}
    out = '[{"id": 1, "verdict": "supports", "why": "x"}]'
    err = ""
    rc = 0
    timeout_first = False
    pid = 4242

    def __init__(self, argv, **kw):
        FakeProc.seen["argv"], FakeProc.seen["kw"] = list(argv), kw
        self.returncode = FakeProc.rc
        self.calls = 0

    def communicate(self, inp=None, timeout=None):
        self.calls += 1
        if FakeProc.timeout_first and self.calls == 1:
            FakeProc.seen["timeout"] = timeout
            raise subprocess.TimeoutExpired("claude", timeout)
        return FakeProc.out, FakeProc.err

    def kill(self):
        FakeProc.seen["killed"] = True


def _judge(out=None, rc=0, err="", timeout_first=False):
    sys.path.insert(0, str(HOOKS))
    import citation_judge as cj
    FakeProc.seen = {}
    FakeProc.out, FakeProc.rc, FakeProc.err, FakeProc.timeout_first = (
        out if out is not None else '[{"id": 1, "verdict": "supports", "why": "x"}]', rc, err, timeout_first)
    taskkills = []

    def fake_run(argv, **kw):
        taskkills.append(list(argv))
        class D:
            returncode = 0
        return D()

    old = (cj.subprocess.Popen, cj.subprocess.run, cj.CLAUDE_CMD)
    cj.subprocess.Popen, cj.subprocess.run, cj.CLAUDE_CMD = FakeProc, fake_run, r"C:ake\claude.exe"
    try:
        res = cj.ask_judge("claim", [{"id": 1, "raw": "a.py:1", "path": "a.py", "line": 1, "window": "1> x"}])
    finally:
        cj.subprocess.Popen, cj.subprocess.run, cj.CLAUDE_CMD = old
    return cj, res, taskkills


def test_t5_ask_judge_builds_a_toolless_settingless_child_in_a_temp_dir():
    cj, out, _ = _judge()
    assert out and out[0]["verdict"] == "supports", out  # the parsed reply is returned
    argv = FakeProc.seen["argv"]

    def after(flag):
        return argv[argv.index(flag) + 1]

    assert "-p" in argv
    assert after("--tools") == "" and after("--setting-sources") == "" and after("--max-turns") == "1"
    for flag in ("--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"):
        assert flag in argv, (flag, argv)
    assert FakeProc.seen["kw"]["env"]["HARNESS_HOOK_CHILD"] == "1"
    cwd = Path(FakeProc.seen["kw"]["cwd"]).resolve()
    assert cwd == Path(tempfile.gettempdir()).resolve() and cwd != Path.cwd().resolve(), cwd
    assert cj.LAST_FAIL == {}, cj.LAST_FAIL  # a good call leaves no failure note


def test_t5_ask_judge_timeout_kills_the_process_tree_and_records_why():
    cj, out, kills = _judge(timeout_first=True)
    assert out is None, out
    assert FakeProc.seen["timeout"] == cj.CALL_TIMEOUT, FakeProc.seen
    if os.name == "nt":
        assert kills and kills[0][:3] == ["taskkill", "/T", "/F"] and "4242" in kills[0], kills
    else:
        assert FakeProc.seen.get("killed"), FakeProc.seen
    assert "timeout" in cj.LAST_FAIL.get("error", ""), cj.LAST_FAIL


def test_t5_ask_judge_nonzero_exit_records_exit_code_and_stderr():
    cj, out, kills = _judge(out="", rc=3, err="boom: no auth")
    assert out is None and not kills, (out, kills)
    e = cj.LAST_FAIL.get("error", "")
    assert "exit 3" in e and "boom: no auth" in e, cj.LAST_FAIL
    # twin: the next good call clears the failure note
    cj2, out2, _ = _judge()
    assert out2 and cj2.LAST_FAIL == {}, (out2, cj2.LAST_FAIL)


def test_t5_evaluate_puts_the_judge_failure_into_the_call_failed_log():
    sys.path.insert(0, str(HOOKS))
    import citation_judge as cj
    snip = [{"id": 1, "raw": "a.py:1", "path": "a.py", "line": 1, "window": "1> x"}]
    old = (cj.build_snippets, cj.ask_judge)

    def failing(claim, snippets):
        cj.LAST_FAIL.clear()
        cj.LAST_FAIL.update(error="exit 3: boom")
        return None

    cj.build_snippets = lambda gate, message, cwd: snip
    cj.ask_judge = failing
    try:
        reason, info = cj.evaluate(None, {"last_assistant_message": "a.py:1 sets x", "cwd": "."})
        assert reason is None and info.get("judge") == "call-failed" and info.get("error") == "exit 3: boom", (reason, info)
        # twin: a working judge says "supports" and the log carries no failure text
        cj.ask_judge = lambda claim, snippets: [{"id": 1, "verdict": "supports", "why": "ok"}]
        reason, info = cj.evaluate(None, {"last_assistant_message": "a.py:1 sets x", "cwd": "."})
        assert reason is None and info.get("judge") != "call-failed" and "error" not in info, (reason, info)
    finally:
        cj.build_snippets, cj.ask_judge = old


def _pending(rows):
    sys.path.insert(0, str(HOOKS))
    import importlib
    scg = importlib.import_module("session_close_gate")
    f = hv2.fresh("s14-t5-transcript") / "t.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8", newline="\n")
    return [a for a, _ in scg.pending_agents_aged(str(f))]


def _stamp(secs_ago):
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - secs_ago))


def _result_row(text, ts=None):
    row = {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "u1", "content": text}]}}
    if ts:
        row["timestamp"] = ts
    return row


LAUNCH = "Async agent launched successfully.\nagentId: agentX1 (internal ID)"


def test_t5_repeated_untimed_launch_row_keeps_the_agent_pending():
    timed, untimed = _result_row(LAUNCH, _stamp(120)), _result_row(LAUNCH)
    assert _pending([timed, untimed]) == ["agentX1"]
    # twins: the untimed row alone proves nothing; a notification clears the agent
    assert _pending([untimed]) == []
    note = {"type": "user", "timestamp": _stamp(60), "message": {"role": "user", "content": "<task-notification><task-id>agentX1</task-id><status>completed</status></task-notification>"}}
    assert _pending([timed, untimed, note]) == []
    # a notification for another agent does not clear it
    other = json.loads(json.dumps(note).replace("agentX1", "agentZ9"))
    assert _pending([timed, untimed, other]) == ["agentX1"]


def test_t5_resumed_agent_is_pending_again_after_its_notification():
    timed, untimed = _result_row(LAUNCH, _stamp(300)), _result_row(LAUNCH)
    note = {"type": "user", "timestamp": _stamp(200), "message": {"role": "user", "content": "<task-notification><task-id>agentX1</task-id></task-notification>"}}
    resume = _result_row('{"success":true,"resumedAgentId":"agentX1"}', _stamp(100))
    assert _pending([timed, untimed, note, resume]) == ["agentX1"]
    assert _pending([timed, untimed, note]) == []  # twin: without the resume it stays finished
    # a resume row without a timestamp cannot restart the clock
    assert _pending([timed, untimed, note, _result_row('{"success":true,"resumedAgentId":"agentX1"}')]) == []


# ------------------------------------------------------------------ T7
def _notice(summary, ts, agent="agentX1"):
    text = f"<task-notification><task-id>{agent}</task-id><status>completed</status><summary>{summary}</summary></task-notification>"
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}}


def test_t5_interim_still_running_notice_keeps_the_agent_pending():
    timed = _result_row(LAUNCH, _stamp(600))
    interim = _notice('Agent "y" is waiting on its own background work', _stamp(5))
    assert _pending([timed, interim]) == ["agentX1"]
    # twin: a final notice clears it
    assert _pending([timed, _notice('Agent "y" finished', _stamp(5))]) == []
    # an interim notice older than 30 minutes no longer counts
    assert _pending([_result_row(LAUNCH, _stamp(4000)), _notice('Agent "y" is waiting on its own background work', _stamp(31 * 60))]) == []
    # an interim notice for another agent does not revive this one
    assert _pending([timed, _notice('Agent "y" finished', _stamp(30)), _notice('Agent "z" is waiting on its own background work', _stamp(5), "agentZ9")]) == ["agentZ9"]


def test_t7_verify_pass_needs_by_a_test_role():
    p = Proj("t7-by")
    p.commit("a.py", "x=1\n", T0)
    p.agents(run_entry("functional-test-reviewer", T0 + 100), run_entry("code-reviewer", T0 + 100),
             run_entry("implementation-engineer", T0 + 100))
    r = p.led("verify", "S1-T1", "--result", "pass", "--evidence", "e", ok=False)
    assert refused(r) and p.task()["verify"] is None, r.all
    for bad in ("code-reviewer", "implementation-engineer", "general-purpose"):
        r = p.led("verify", "S1-T1", "--result", "pass", "--evidence", "e", "--by", bad, ok=False)
        assert refused(r) and p.task()["verify"] is None, (bad, r.all)
    for good in ("functional-test-reviewer",):
        p.led("verify", "S1-T1", "--result", "pass", "--evidence", "e", "--by", good)
        v = p.task()["verify"]
        assert v["result"] == "pass" and v["by"] == good, v


def test_t7_verify_pass_each_test_role_accepted_with_its_own_run():
    for role in ("tdd-guide", "e2e-runner"):
        p = Proj("t7-role-" + role)
        p.commit("a.py", "x=1\n", T0)
        p.agents(run_entry(role, T0 + 100))
        p.led("verify", "S1-T1", "--result", "pass", "--evidence", "e", "--by", role)
        assert p.task()["verify"]["result"] == "pass"


def test_t7_verify_pass_needs_a_finished_run_of_that_type_after_the_commit():
    cases = {
        "no state file": None,
        "run of another type": [run_entry("tdd-guide", T0 + 100)],
        "unfinished run (no stop)": [run_entry("functional-test-reviewer", T0 + 100, stop=None)],
        "run started before the commit": [run_entry("functional-test-reviewer", T0 - 1000)],
    }
    for label, runs in cases.items():
        p = Proj("t7-run")
        p.commit("a.py", "x=1\n", T0)
        if runs is not None:
            p.agents(*runs)
        r = p.led("verify", "S1-T1", "--result", "pass", "--evidence", "e", "--by", "functional-test-reviewer", ok=False)
        assert refused(r) and p.task()["verify"] is None, (label, r.all)
        # twin: add a finished run of the right type after the commit and the same call is accepted
        p.agents(*(runs or []), run_entry("functional-test-reviewer", T0 + 100))
        p.led("verify", "S1-T1", "--result", "pass", "--evidence", "e", "--by", "functional-test-reviewer")
        assert p.task()["verify"]["result"] == "pass", label


def test_t7_verify_fail_and_not_run_need_neither_by_nor_a_run():
    for res in ("fail", "NOT_RUN"):
        p = Proj("t7-" + res)
        p.commit("a.py", "x=1\n", T0)
        p.led("verify", "S1-T1", "--result", res, "--evidence", "e")
        assert p.task()["verify"]["result"] == res
    p = Proj("t7-nocommit")
    r = p.led("verify", "S1-T1", "--result", "fail", "--evidence", "e", ok=False)
    assert refused(r), r.all  # no commit: no verify at all


def test_t7_review_accepted_needs_reviewer_roles_with_runs():
    p = Proj("t7-review")
    p.commit("a.py", "x=1\n", T0)
    p.agents(*[run_entry(x, T0 + 100) for x in ("implementation-engineer", "general-purpose", "functional-test-reviewer",
                                                 "tdd-guide", "e2e-runner", "code-reviewer", "python-reviewer")])
    r = p.led("review", "S1-T1", "--result", "accepted", ok=False)
    assert refused(r) and p.task()["review"] is None, r.all
    for bad in ("implementation-engineer", "general-purpose", "functional-test-reviewer", "tdd-guide", "e2e-runner",
                "code-reviewer,implementation-engineer"):
        r = p.led("review", "S1-T1", "--result", "accepted", "--by", bad, ok=False)
        assert refused(r) and p.task()["review"] is None, (bad, r.all)
    p.led("review", "S1-T1", "--result", "accepted", "--by", "code-reviewer")
    assert p.task()["review"]["result"] == "accepted" and p.task()["review"]["by"] == "code-reviewer"


def test_t7_review_accepted_needs_a_run_for_every_named_reviewer():
    p = Proj("t7-review-multi")
    p.commit("a.py", "x=1\n", T0)
    p.agents(run_entry("code-reviewer", T0 + 100), run_entry("python-reviewer", T0 - 1000),
             run_entry("security-reviewer", T0 + 100, stop=None))
    r = p.led("review", "S1-T1", "--result", "accepted", "--by", "code-reviewer,python-reviewer", ok=False)
    assert refused(r), r.all  # python-reviewer ran before the commit
    r = p.led("review", "S1-T1", "--result", "accepted", "--by", "code-reviewer,security-reviewer", ok=False)
    assert refused(r), r.all  # security-reviewer never finished
    p.agents(run_entry("code-reviewer", T0 + 100), run_entry("python-reviewer", T0 + 150))
    p.led("review", "S1-T1", "--result", "accepted", "--by", "code-reviewer,python-reviewer")
    assert p.task()["review"]["result"] == "accepted"


def test_t7_review_changes_needs_no_by_and_no_run():
    p = Proj("t7-review-changes")
    p.commit("a.py", "x=1\n", T0)
    p.led("review", "S1-T1", "--result", "changes", "--findings", "2")
    r = p.task()["review"]
    assert r["result"] == "changes" and r["findings"] == 2, r


def test_t7_docs_only_commit_after_the_run_keeps_the_run_valid_but_a_code_commit_does_not():
    # code commit at T0, docs-only commits (core/*.md, docs/*.md) later; the run started after the code commit
    p = Proj("t7-docs")
    p.commit("a.py", "x=1\n", T0)
    p.commit("core/LOG.md", "note\n", T0 + 2000)
    p.commit("docs/x.md", "note\n", T0 + 3000)
    assert len(p.task()["commits"]) == 3
    p.agents(run_entry("functional-test-reviewer", T0 + 100), run_entry("code-reviewer", T0 + 100))
    p.led("verify", "S1-T1", "--result", "pass", "--evidence", "e", "--by", "functional-test-reviewer")
    p.led("review", "S1-T1", "--result", "accepted", "--by", "code-reviewer")
    assert p.task()["verify"]["result"] == "pass" and p.task()["review"]["result"] == "accepted"
    # a run that started before the last CODE commit is still refused even with docs commits after it
    p.agents(run_entry("functional-test-reviewer", T0 - 1000))
    r = p.led("verify", "S1-T1", "--result", "pass", "--evidence", "e", "--by", "functional-test-reviewer", ok=False)
    assert refused(r), r.all
    # twin: the same run is stale when the later commit is code
    q = Proj("t7-docs-code")
    q.commit("a.py", "x=1\n", T0)
    q.commit("b.py", "y=1\n", T0 + 2000)
    q.agents(run_entry("functional-test-reviewer", T0 + 100))
    r = q.led("verify", "S1-T1", "--result", "pass", "--evidence", "e", "--by", "functional-test-reviewer", ok=False)
    assert refused(r), r.all
    q.agents(run_entry("functional-test-reviewer", T0 + 2100))
    q.led("verify", "S1-T1", "--result", "pass", "--evidence", "e", "--by", "functional-test-reviewer")


def test_t7_a_commit_mixing_docs_and_code_counts_as_code():
    p = Proj("t7-mixed")
    p.commit("a.py", "x=1\n", T0)
    (p.repo / "core").mkdir(exist_ok=True)
    (p.repo / "core" / "N.md").write_text("n\n", encoding="utf-8")
    (p.repo / "c.py").write_text("z=1\n", encoding="utf-8")
    assert hv2.git(p.repo, "add", "-A").rc == 0
    assert hv2.git(p.repo, "commit", "-q", "-m", "mixed", "-m", "Task: S1-T1", env=git_date(T0 + 2000)).rc == 0
    p.led("commit-hook")
    p.agents(run_entry("functional-test-reviewer", T0 + 100))
    r = p.led("verify", "S1-T1", "--result", "pass", "--evidence", "e", "--by", "functional-test-reviewer", ok=False)
    assert refused(r), r.all


def test_t7_a_run_recorded_for_another_project_does_not_count():
    p = Proj("t7-proj")
    p.commit("a.py", "x=1\n", T0)
    other = str(hv2.mk_repo("t7-proj-other").resolve())
    d = p.state / "agents"
    d.mkdir(parents=True, exist_ok=True)
    runs = [run_entry("functional-test-reviewer", T0 + 100) | {"project": other},
            run_entry("code-reviewer", T0 + 100) | {"project": ""},
            run_entry("tdd-guide", T0 + 100)]  # an old entry with no project key at all
    (d / "active.json").write_text(json.dumps({"agents": runs}), encoding="utf-8")
    r = p.led("verify", "S1-T1", "--result", "pass", "--evidence", "e", "--by", "functional-test-reviewer", ok=False)
    assert refused(r) and p.task()["verify"] is None, r.all
    r = p.led("verify", "S1-T1", "--result", "pass", "--evidence", "e", "--by", "tdd-guide", ok=False)
    assert refused(r) and p.task()["verify"] is None, r.all
    r = p.led("review", "S1-T1", "--result", "accepted", "--by", "code-reviewer", ok=False)
    assert refused(r) and p.task()["review"] is None, r.all
    # twin: the same runs recorded for this project are accepted (spelled in upper case: normcase on Windows)
    mine = str(p.repo.resolve())
    spelled = mine.upper() if os.name == "nt" else mine
    runs = [run_entry("functional-test-reviewer", T0 + 100) | {"project": spelled},
            run_entry("code-reviewer", T0 + 100) | {"project": mine}]
    (d / "active.json").write_text(json.dumps({"agents": runs}), encoding="utf-8")
    p.led("verify", "S1-T1", "--result", "pass", "--evidence", "e", "--by", "functional-test-reviewer")
    p.led("review", "S1-T1", "--result", "accepted", "--by", "code-reviewer")
    assert p.task()["verify"]["result"] == "pass" and p.task()["review"]["result"] == "accepted"


def test_t7_tests_only_commit_after_the_run_keeps_it_valid_but_a_code_commit_does_not():
    for rel in ("tests/test_a.py", "src/test_b.py", "web/app.test.ts", "web/app.spec.js"):
        p = Proj("t7-tests")
        p.commit("a.py", "x=1\n", T0)
        p.commit(rel, "t\n", T0 + 2000)  # the test agent commits its tests after its run
        p.agents(run_entry("functional-test-reviewer", T0 + 100))
        p.led("verify", "S1-T1", "--result", "pass", "--evidence", "e", "--by", "functional-test-reviewer")
        assert p.task()["verify"]["result"] == "pass", rel
    # twin: a file that only looks like a test (wrong folder and name) is code and makes the run stale
    for rel in ("src/contest.py", "latest.py", "src/attest.js"):
        q = Proj("t7-looks")
        q.commit("a.py", "x=1\n", T0)
        q.commit(rel, "t\n", T0 + 2000)
        q.agents(run_entry("functional-test-reviewer", T0 + 100))
        r = q.led("verify", "S1-T1", "--result", "pass", "--evidence", "e", "--by", "functional-test-reviewer", ok=False)
        assert refused(r), (rel, r.all)


def test_t7_review_by_allow_list():
    names_ok = ["code-reviewer", "python-reviewer", "security-reviewer", "silent-failure-hunter", "architect", "code-architect",
                "type-design-analyzer", "comment-analyzer", "pr-test-analyzer", "a11y-architect"]
    names_bad = ["functional-test-reviewer", "planner", "doc-updater", "build-error-resolver", "reviewer-bot", "code-reviewer-x"]
    p = Proj("t7-allow")
    p.commit("a.py", "x=1\n", T0)
    p.agents(*[run_entry(n, T0 + 100) for n in names_ok + names_bad])
    for n in names_bad:
        r = p.led("review", "S1-T1", "--result", "accepted", "--by", n, ok=False)
        assert refused(r) and p.task()["review"] is None, (n, r.all)
    for n in names_ok:
        p.led("review", "S1-T1", "--result", "accepted", "--by", n)
        assert p.task()["review"]["by"] == n, n


def test_t7_agent_visibility_project_root_and_400_run_cap():
    sys.path.insert(0, str(HOOKS))
    import agent_visibility as av
    assert av.CAP == 400
    repo = hv2.mk_repo("t7-av")
    sub = repo / "pkg"
    sub.mkdir()
    assert av.project_root(str(sub)) == str(repo.resolve())  # git common-dir root of a subfolder
    assert av.project_root(str(hv2.fresh("t7-av-nogit"))) == ""  # twin: not a repo
    os.environ["HARNESS_HOOK_STATE"] = str(hv2.fresh("t7-av-state"))
    try:
        av.save({"agents": [{"type": "x", "start": i, "stop": i + 1} for i in range(450)]})
        got = av.load()["agents"]
        assert len(got) == 400 and got[0]["start"] == 50, (len(got), got[0])
    finally:
        os.environ.pop("HARNESS_HOOK_STATE", None)


# ------------------------------------------------------------------ T6
RID = "11111111-2222-4333-8444-555555555555"
OTHER_RID = "99999999-2222-4333-8444-555555555555"


def msg(p, direction, text, rid=RID, correlated=True, project=None, mid=None):
    row = {"direction": direction, "reviewRequestId": rid, "project": project or p.repo.name, "text": text,
           "messageId": mid or uuid.uuid4().hex}
    if direction == "inbound":
        row["correlated"] = correlated
        row["replyId"] = mid or "reply-" + uuid.uuid4().hex[:6]
    with open(p.messages, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def approve(p, rid=RID, *extra):
    return p.led("approve", "--request-id", rid, *extra, ok=False)


def test_t6_approve_accepts_correlated_bare_approve_for_this_head():
    p = Proj("t6-ok")
    h = p.head()
    msg(p, "outbound", "please review")
    msg(p, "inbound", f"Looks good.\nHEAD: {h}\nAPPROVE\n", mid="reply-ok")
    r = approve(p)
    assert r.rc == 0, r.all
    ap = p.data()["approval"]
    assert ap["head"] == h and ap["request_id"] == RID and ap["reply_id"] == "reply-ok", ap
    assert any(e["kind"] == "approve" for e in p.data()["events"])


def test_t6_approve_without_request_id_or_with_a_malformed_one_is_refused():
    p = Proj("t6-noreq")
    h = p.head()
    msg(p, "inbound", f"HEAD: {h}\nAPPROVE\n")
    r = p.led("approve", ok=False)
    assert refused(r) and p.data()["approval"] is None, r.all
    r = approve(p, "not-a-uuid")
    assert refused(r) and p.data()["approval"] is None, r.all
    assert approve(p).rc == 0  # twin: the valid id is accepted


def test_t6_negative_verdicts_are_refused():
    for label, text in {"RESEND": "RESEND: please send the diff again\nAPPROVE", "REVISE": "REVISE",
                        "REQUEST_CHANGES": "VERDICT: REQUEST_CHANGES", "findings": "Fix the lock.\nREVISE\nAPPROVE",
                        "no verdict line": "Looks fine to me, approve it later"}.items():
        p = Proj("t6-neg")
        msg(p, "inbound", f"HEAD: {p.head()}\n{text}\n")
        r = approve(p)
        assert refused(r) and p.data()["approval"] is None, (label, r.all)
    p = Proj("t6-neg-twin")
    msg(p, "inbound", f"HEAD: {p.head()}\nVERDICT: APPROVE\n")
    assert approve(p).rc == 0


def test_t6_approve_for_another_head_is_refused():
    p = Proj("t6-head")
    other = "a" * 40
    msg(p, "inbound", f"HEAD: {other}\nAPPROVE\n")
    r = approve(p)
    assert refused(r) and p.data()["approval"] is None, r.all
    p.commit("a.py", "x=1\n", T0)  # HEAD moves on: the reply for the old HEAD is now stale
    p2 = Proj("t6-head-old")
    old = p2.head()
    msg(p2, "inbound", f"HEAD: {old}\nAPPROVE\n")
    p2.commit("a.py", "x=1\n", T0)
    assert refused(approve(p2))
    msg(p2, "inbound", f"HEAD: {p2.head()}\nAPPROVE\n")
    assert approve(p2).rc == 0  # twin: a reply for the new HEAD is accepted


def test_t6_uncorrelated_other_project_or_other_request_replies_are_refused():
    p = Proj("t6-corr")
    h = p.head()
    msg(p, "inbound", f"HEAD: {h}\nAPPROVE\n", correlated=False)
    assert refused(approve(p))
    msg(p, "inbound", f"HEAD: {h}\nAPPROVE\n", project="some-other-project")
    assert refused(approve(p))
    msg(p, "inbound", f"HEAD: {h}\nAPPROVE\n", rid=OTHER_RID)
    assert refused(approve(p))
    assert p.data()["approval"] is None
    msg(p, "inbound", f"HEAD: {h}\nAPPROVE\n")  # twin
    assert approve(p).rc == 0


def test_t6_missing_message_log_is_refused():
    p = Proj("t6-nolog")
    p.messages.unlink()
    r = approve(p)
    assert refused(r) and p.data()["approval"] is None, r.all


def test_t6_head_only_in_our_outbound_request_is_refused_the_reply_must_name_it():
    p = Proj("t6-out")
    h = p.head()
    msg(p, "outbound", f"Review request\nHEAD: {h}\n")
    msg(p, "inbound", "APPROVE\n")
    r = approve(p)
    assert refused(r) and p.data()["approval"] is None, r.all
    # twin: a reply that names the HEAD is accepted
    msg(p, "inbound", f"HEAD: {h}\nAPPROVE\n")
    assert approve(p).rc == 0 and p.data()["approval"]["head"] == h
    q = Proj("t6-out-bad")  # a reply naming another head stays refused whatever the request said
    msg(q, "outbound", f"Review request\nHEAD: {q.head()}\n")
    msg(q, "inbound", f"HEAD: {'b' * 40}\nAPPROVE\n")
    assert refused(approve(q)) and q.data()["approval"] is None


def test_t6_only_the_newest_reply_decides():
    p = Proj("t6-newest")
    h = p.head()
    msg(p, "inbound", f"HEAD: {h}\nAPPROVE\n", mid="reply-1")
    msg(p, "inbound", f"HEAD: {h}\nREVISE\nFix the lock.\n", mid="reply-2")
    r = approve(p)
    assert refused(r) and p.data()["approval"] is None, r.all
    # twin: REVISE first, a later APPROVE for the same HEAD is accepted and its reply id is recorded
    q = Proj("t6-newest-twin")
    h = q.head()
    msg(q, "inbound", f"HEAD: {h}\nREVISE\n", mid="reply-1")
    msg(q, "inbound", f"HEAD: {h}\nAPPROVE\n", mid="reply-2")
    assert approve(q).rc == 0 and q.data()["approval"]["reply_id"] == "reply-2"


def test_t6_records_without_a_matching_project_are_skipped_even_if_newest():
    p = Proj("t6-proj")
    h = p.head()
    msg(p, "inbound", f"HEAD: {h}\nAPPROVE\n", mid="reply-mine")
    # a newer REVISE that names another project must not veto this project's approval
    msg(p, "inbound", f"HEAD: {h}\nREVISE\n", project="some-other-project", mid="reply-other")
    assert approve(p).rc == 0 and p.data()["approval"]["reply_id"] == "reply-mine"
    # a record with no project field at all is skipped too
    q = Proj("t6-proj-none")
    h = q.head()
    row = {"direction": "inbound", "reviewRequestId": RID, "correlated": True, "text": f"HEAD: {h}\nAPPROVE\n",
           "messageId": "m1", "replyId": "r-noproj"}
    with open(q.messages, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    assert refused(approve(q)) and q.data()["approval"] is None


def push_stdin(sha):
    return f"refs/heads/main {sha} refs/heads/main {'0' * 40}\n"


def test_t6_check_approval_refuses_an_approval_without_request_id():
    p = Proj("t6-chk")
    h = p.head()
    d = p.data()
    d["approval"] = {"head": h, "by": "GPT-PM", "at": "2026-11-12T00:00:00+00:00", "evidence": "typed"}
    p.ledger_file.write_text(json.dumps(d), encoding="utf-8")
    r = p.led("check-approval", ok=False, inp=push_stdin(h))
    assert r.rc == 1 and "push blocked" in r.err, (r.rc, r.all)
    d["approval"].update(request_id=RID, reply_id="r1")
    p.ledger_file.write_text(json.dumps(d), encoding="utf-8")
    r = p.led("check-approval", ok=False, inp=push_stdin(h))
    assert r.rc == 0 and "approval ok" in r.out, (r.rc, r.all)
    # other head with a request id is still refused
    d["approval"]["head"] = "c" * 40
    p.ledger_file.write_text(json.dumps(d), encoding="utf-8")
    assert p.led("check-approval", ok=False, inp=push_stdin(h)).rc == 1


def test_t6_approve_then_check_approval_end_to_end():
    p = Proj("t6-e2e")
    h = p.head()
    msg(p, "inbound", f"HEAD: {h}\nAPPROVE\n")
    assert approve(p).rc == 0
    assert p.led("check-approval", ok=False, inp=push_stdin(h)).rc == 0


def test_t6_rules_gate_push_check_reports_an_approval_without_request_id():
    repo = enf.make_gate_repo("s14-t6-rg", "typed", [("done", False)])
    led_file = repo / ".harness" / "ledger.json"
    denied, reason, r = enf.gate("git push", cwd=repo)
    assert denied and "request id" in reason.lower(), (denied, reason)
    d = json.loads(led_file.read_text(encoding="utf-8"))
    d["approval"].update(request_id=RID, reply_id="r1")
    led_file.write_text(json.dumps(d), encoding="utf-8")
    denied, reason, r = enf.gate("git push", cwd=repo)
    assert not denied, reason  # twin


def test_t6_rules_gate_push_denied_when_head_cannot_be_resolved():
    repo = hv2.mk_repo("s14-t6-nohead", with_commit=False)  # a repo with no commit: git rev-parse HEAD fails
    assert hv2.git(repo, "rev-parse", "--verify", "HEAD").rc != 0
    (repo / ".harness-tools").mkdir()
    shutil.copy2(hv2.TOOLS / "ledger.py", repo / ".harness-tools" / "ledger.py")
    ap = {"head": "d" * 40, "by": "GPT-PM", "at": "2026-11-12T00:00:00+00:00", "evidence": "t", "request_id": RID, "reply_id": "r1"}
    led = {"version": 1, "project": "s14-t6-nohead", "approval": ap, "events": [],
           "sprints": [{"id": "S1", "title": "s", "state": "open", "tasks": [{"id": "S1-T1", "title": "t", "state": "done"}]}]}
    (repo / ".harness").mkdir()
    (repo / ".harness" / "ledger.json").write_text(json.dumps(led), encoding="utf-8")
    denied, reason, r = enf.gate("git push", cwd=repo)
    assert denied and "head cannot be resolved" in reason.lower(), (denied, reason)
    # twin: a normal repo whose approval (with request id) is bound to HEAD and whose tasks are all done is allowed
    ok = enf.make_gate_repo("s14-t6-withhead", "head", [("done", False)])
    denied, reason, r = enf.gate("git push", cwd=ok)
    assert not denied, reason
    # twin 2: the same normal repo with the approval bound to an older commit is still denied (the comparison runs)
    bad = enf.make_gate_repo("s14-t6-stale", "stale", [("done", False)])
    denied, reason, r = enf.gate("git push", cwd=bad)
    assert denied and "not to the current head" in reason.lower(), (denied, reason)


def closed_sprint_project(name, approval):
    p = Proj(name)
    p.commit("a.py", "x=1\n", T0)
    sha = p.head()
    d = p.data()
    t = d["sprints"][0]["tasks"][0]
    t["verify"] = {"result": "pass", "task_commit": sha, "by": "tdd-guide"}
    t["review"] = {"result": "accepted", "task_commit": sha, "by": "code-reviewer"}
    t["state"] = "done"
    d["sprints"][0].update(state="closed", closed="2026-11-12T00:00:00+00:00")
    d["approval"] = {"head": sha, "request_id": RID, "reply_id": "r"} if approval else None
    p.ledger_file.write_text(json.dumps(d), encoding="utf-8")
    return p


def test_t6_reopen_reopens_clears_verify_review_and_approval():
    p = closed_sprint_project("t6-reopen", True)
    r = p.led("reopen", "S1", "--reason", "wrong close")
    assert "approval dropped=True" in r.out, r.out
    d = p.data()
    s, t = d["sprints"][0], d["sprints"][0]["tasks"][0]
    assert s["state"] == "open" and not s.get("closed"), s
    assert t["verify"] is None and t["review"] is None and t["state"] == "implemented", t
    assert d["approval"] is None
    ev = [e for e in d["events"] if e["kind"] == "sprint-reopen"]
    assert len(ev) == 1 and ev[0]["sprint"] == "S1" and ev[0]["reason"] == "wrong close" and ev[0]["approval_dropped"] is True, ev
    # twin: without an approval nothing is reported dropped
    q = closed_sprint_project("t6-reopen-noap", False)
    q.led("reopen", "S1", "--reason", "again")
    ev = [e for e in q.data()["events"] if e["kind"] == "sprint-reopen"][0]
    assert ev["approval_dropped"] is False


def test_t6_reopen_needs_a_reason_and_a_known_sprint():
    p = closed_sprint_project("t6-reopen-bad", True)
    assert refused(p.led("reopen", "S1", ok=False))
    assert refused(p.led("reopen", "NOPE", "--reason", "x", ok=False))
    d = p.data()
    assert d["sprints"][0]["state"] == "closed" and d["approval"] is not None  # nothing changed by the refusals
    assert p.led("reopen", "S1", "--reason", "x").rc == 0


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
