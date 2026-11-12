"""S14 incident-hardening tests: T5 (hook-child isolation), T6 (approve only from a real PM Bridge APPROVE, reopen),
T7 (verify pass / review accepted need a recorded agent run). Written from the S14 acceptance criteria, not from the code.
Every refused case has an accepted twin. All state is temp: a temp git repo, and HARNESS_LEDGER, HARNESS_HOOK_STATE and
HARNESS_PM_BRIDGE_MESSAGES point at temp files under D:\\tmp\\harness_v2_tests. No real claude is run.
Run: C:\\Python314\\python.exe -I tests/test_s14_incident.py"""
import calendar
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
        d = self.state / "agents"
        d.mkdir(parents=True, exist_ok=True)
        (d / "active.json").write_text(json.dumps({"agents": list(runs)}), encoding="utf-8")

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


def test_t5_ask_judge_builds_a_toolless_settingless_child_in_a_temp_dir():
    sys.path.insert(0, str(HOOKS))
    import citation_judge as cj
    seen = {}

    class Done:
        stdout = '[{"id": 1, "verdict": "supports", "why": "x"}]'

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = list(argv), kw
        return Done()

    old_run, old_cmd = cj.subprocess.run, cj.CLAUDE_CMD
    cj.subprocess.run, cj.CLAUDE_CMD = fake_run, r"C:\fake\claude.exe"
    try:
        out = cj.ask_judge("claim", [{"id": 1, "raw": "a.py:1", "path": "a.py", "line": 1, "window": "1> x"}])
    finally:
        cj.subprocess.run, cj.CLAUDE_CMD = old_run, old_cmd
    assert out and out[0]["verdict"] == "supports", out  # the parsed reply is returned
    argv = seen["argv"]

    def after(flag):
        return argv[argv.index(flag) + 1]

    assert "-p" in argv
    assert after("--tools") == "" and after("--setting-sources") == "" and after("--max-turns") == "1"
    for flag in ("--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"):
        assert flag in argv, (flag, argv)
    assert seen["kw"]["env"]["HARNESS_HOOK_CHILD"] == "1"
    cwd = Path(seen["kw"]["cwd"]).resolve()
    assert cwd == Path(tempfile.gettempdir()).resolve() and cwd != Path.cwd().resolve(), cwd


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


def test_t6_head_taken_from_the_outbound_request_when_the_reply_names_none():
    p = Proj("t6-out")
    h = p.head()
    msg(p, "outbound", f"Review request\nHEAD: {h}\n")
    msg(p, "inbound", "APPROVE\n")
    assert approve(p).rc == 0 and p.data()["approval"]["head"] == h
    q = Proj("t6-out-bad")  # twin: the request names another head
    msg(q, "outbound", f"Review request\nHEAD: {'b' * 40}\n")
    msg(q, "inbound", "APPROVE\n")
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
    repo = enf.make_gate_repo("s14-t6-rg", "head", [("done", False)])
    led_file = repo / ".harness" / "ledger.json"
    denied, reason, r = enf.gate("git push", cwd=repo)
    assert denied and "request id" in reason.lower(), (denied, reason)
    d = json.loads(led_file.read_text(encoding="utf-8"))
    d["approval"].update(request_id=RID, reply_id="r1")
    led_file.write_text(json.dumps(d), encoding="utf-8")
    denied, reason, r = enf.gate("git push", cwd=repo)
    assert not denied, reason  # twin


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
