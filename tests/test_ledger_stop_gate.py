"""Tests for the Harness v2 ledger "tasks left" Stop gate (S7-T2: harness/hooks/session_close_gate.py ledger_stop) and the
`block` / `start` commands of tools/ledger.py. The hook runs as a subprocess on fixture projects under D:\\tmp\\harness_v2_tests with
a temp HARNESS_HOOK_STATE. Run: C:\\Python314\\python.exe -I tests/test_ledger_stop_gate.py"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hv2  # noqa: E402

HOOK = HERE.parent / "harness" / "hooks" / "session_close_gate.py"
PY = sys.executable
PROSE = "I finished the part I could do and I am stopping here."


# ---------- fixtures ----------
def mk_task(tid, state="todo", **kw):
    t = {"id": tid, "title": "title " + tid, "criteria": [], "run": "", "risk": "local", "journey": False, "state": state,
         "commits": [], "verify": None, "review": None, "routing": None, "tokens": None}
    t.update(kw)
    return t


def write_ledger(proj, sprints):
    f = proj / ".harness" / "ledger.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"version": 1, "project": proj.name, "sprints": sprints, "approval": None, "events": []}, indent=1),
                 encoding="utf-8", newline="\n")


def sprint(sid="S9", tasks=(), state="open"):
    return {"id": sid, "title": "sprint " + sid, "branch": "sprint/" + sid.lower(), "state": state, "opened": "2026-11-12T00:00:00+02:00",
            "tasks": list(tasks)}


class Ctx:
    """A project folder + isolated hook state folder."""

    def __init__(self, name, tasks=None, sprints=None):
        self.root = hv2.fresh(name)
        self.proj = self.root / "proj"
        self.proj.mkdir()
        self.state = self.root / "state"
        write_ledger(self.proj, sprints if sprints is not None else [sprint(tasks=tasks or [])])

    def set_tasks(self, tasks, sid="S9"):
        write_ledger(self.proj, [sprint(sid, tasks)])

    def call(self, msg=PROSE, cwd=None, transcript=None, mode="live", extra=None, session="sess1", env_extra=None):
        payload = {"hook_event_name": "Stop", "session_id": session, "cwd": str(cwd if cwd is not None else self.proj),
                   "last_assistant_message": msg}
        if transcript:
            payload["transcript_path"] = str(transcript)
        payload.update(extra or {})
        env = {k: v for k, v in os.environ.items() if not k.startswith("HARNESS_") and not k.startswith("SESSION_CLOSE")}
        env["HARNESS_METRICS_DIR"] = str(hv2.SINK / "metrics")  # S8-T1
        env.update({"HARNESS_HOOK_STATE": str(self.state), "HARNESS_HOOK_CONFIG": str(self.root / "no_config.json"),
                    "SESSION_CLOSE_GATE_MODE": mode, "PYTHONIOENCODING": "utf-8"})
        env.update(env_extra or {})
        return subprocess.run([PY, "-I", str(HOOK)], input=json.dumps(payload), capture_output=True, text=True,
                              encoding="utf-8", env=env, timeout=60)

    def state_file(self):
        return self.state / "session_close" / "session_close_state.json"

    def streak(self):
        d = json.loads(self.state_file().read_text(encoding="utf-8"))
        vals = list((d.get("ledger") or {}).values())
        return vals[0] if len(vals) == 1 else None

    def age_streak(self, seconds):
        d = json.loads(self.state_file().read_text(encoding="utf-8"))
        for v in d["ledger"].values():
            v["first"] = time.time() - seconds
        self.state_file().write_text(json.dumps(d), encoding="utf-8")

    def log(self):
        f = self.state / "session_close" / "session_close_gate.jsonl"
        return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()] if f.is_file() else []


def decision(p):
    for line in p.stdout.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("decision"):
            return d
    return {}


def is_block(p):
    return decision(p).get("decision") == "block"


def sysmsg(p):
    for line in p.stdout.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("systemMessage"):
            return d["systemMessage"]
    return ""


def transcript(dirpath, rows, name="t.jsonl"):
    p = dirpath / name
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8", newline="\n")
    return p


def human(t):
    return {"type": "user", "timestamp": "2026-11-12T10:00:00Z", "message": {"role": "user", "content": t}}


def tool_use(name, inp, tid="u1"):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def check(cond, msg):
    assert cond, msg


# ---------- basic verdicts ----------
def test_open_task_blocks_and_names_it():
    c = Ctx("open", [mk_task("S9-T1", "done"), mk_task("S9-T2", "todo")])
    p = c.call()
    check(p.returncode == 0, p.stderr)
    check(is_block(p), f"expected block, out={p.stdout!r} err={p.stderr!r}")
    check("S9-T2" in decision(p)["reason"] and "todo" in decision(p)["reason"], decision(p))
    check("S9-T1" not in decision(p)["reason"], "done task must not be listed")


def test_each_non_done_state_is_open():
    for st in ("todo", "in_progress", "implemented", "not_run", "verified", "failed"):
        c = Ctx("st-" + st, [mk_task("S9-T1", st)])
        check(is_block(c.call()), f"state {st} must count as open")


def test_all_done_no_block():
    c = Ctx("alldone", [mk_task("S9-T1", "done"), mk_task("S9-T2", "done")])
    p = c.call()
    check(not is_block(p) and p.returncode == 0, p.stdout)


def test_blocked_task_does_not_block():
    c = Ctx("blk", [mk_task("S9-T1", "todo", blocked={"class": "OPERATOR", "cause": "waiting for the operator answer", "at": "x"})])
    check(not is_block(c.call()), "blocked task blocked the stop")
    c.set_tasks([mk_task("S9-T1", "todo", blocked={"class": "EXTERNAL", "cause": "service is down since monday", "at": "x"}),
                 mk_task("S9-T2", "todo")])
    p = c.call()
    check(is_block(p) and "S9-T2" in decision(p)["reason"] and "S9-T1" not in decision(p)["reason"], p.stdout)


def test_closed_sprint_no_block():
    c = Ctx("closed", sprints=[sprint("S8", [mk_task("S8-T1", "todo")], state="closed")])
    check(not is_block(c.call()), "closed sprint with unfinished task blocked")


def test_first_non_closed_sprint_is_the_active_one():
    c = Ctx("twosprints", sprints=[sprint("S8", [mk_task("S8-T1", "todo")], state="closed"), sprint("S9", [mk_task("S9-T1", "todo")])])
    p = c.call()
    check(is_block(p) and "S9-T1" in decision(p)["reason"] and "S8-T1" not in decision(p)["reason"], p.stdout)


def test_question_message_no_block():
    c = Ctx("q", [mk_task("S9-T1")])
    check(not is_block(c.call("Which option do you prefer?")), "question blocked")
    check(not is_block(c.call("Which option do you prefer?  \n")), "question with trailing whitespace blocked")
    check(is_block(c.call("Which option? I will pick A.")), "mid-text '?' must not release")


def test_dry_run_and_off_modes_do_not_block():
    c = Ctx("modes", [mk_task("S9-T1")])
    p = c.call(mode="dry-run")
    check(not is_block(p), "dry-run blocked")
    check(any(r.get("verdict") == "would-block" for r in c.log()), "dry-run must log would-block: " + str(c.log()))
    check(not is_block(c.call(mode="off")), "off blocked")


def test_subagent_payload_no_block():
    c = Ctx("sub", [mk_task("S9-T1")])
    check(not is_block(c.call(extra={"agent_type": "reviewer"})), "subagent blocked")


def test_corrupt_ledger_no_block_with_visible_warning():
    c = Ctx("corrupt", [mk_task("S9-T1")])
    (c.proj / ".harness" / "ledger.json").write_text("{ this is not json", encoding="utf-8")
    p = c.call()
    check(p.returncode == 0 and not is_block(p), f"corrupt ledger must allow: {p.stdout!r}")
    check("ledger" in sysmsg(p).lower(), f"no visible warning, out={p.stdout!r}")
    check(any(r.get("verdict") == "ledger-unreadable" for r in c.log()), str(c.log()))
    (c.proj / ".harness" / "ledger.json").write_text(json.dumps({"version": 1, "sprints": "oops"}), encoding="utf-8")
    p2 = c.call()
    check(not is_block(p2) and "ledger" in sysmsg(p2).lower(), "wrong-shape ledger: " + p2.stdout)


def test_corrupt_ledger_is_not_treated_as_closed_sprint():
    """Unreadable must be reported, never silently equal to 'nothing to do'."""
    c = Ctx("corrupt2", [mk_task("S9-T1")])
    (c.proj / ".harness" / "ledger.json").write_bytes(b"")
    p = c.call()
    check(sysmsg(p) != "" and not is_block(p), p.stdout)


# ---------- project identity ----------
def test_cwd_parent_with_edit_inside_project_blocks():
    c = Ctx("parent", [mk_task("S9-T1")])
    tp = transcript(c.root, [human("go"), tool_use("Edit", {"file_path": str(c.proj / "src" / "a.py"), "old_string": "a", "new_string": "b"})])
    p = c.call(cwd=c.root, transcript=tp)
    check(is_block(p) and "S9-T1" in decision(p)["reason"], f"out={p.stdout!r} log={c.log()}")
    tp2 = transcript(c.root, [human("go"), tool_use("Write", {"file_path": str(c.proj / "n.txt"), "content": "x"})], "t2.jsonl")
    check(is_block(c.call(cwd=c.root, transcript=tp2, session="sess2")), "Write inside project must tie the session")


def test_cwd_parent_without_edit_does_not_block():
    c = Ctx("parent2", [mk_task("S9-T1")])
    tp = transcript(c.root, [human("go"), tool_use("Bash", {"command": "echo hello"})])
    check(not is_block(c.call(cwd=c.root, transcript=tp)), "no edit in project but blocked")
    check(not is_block(c.call(cwd=c.root)), "no transcript at all but blocked")
    check(not is_block(c.call(cwd=c.root, transcript=c.root / "missing.jsonl")), "missing transcript but blocked")


def test_relative_edit_path_does_not_pick_project():
    c = Ctx("relpath", [mk_task("S9-T1")])
    tp = transcript(c.root, [human("go"), tool_use("Edit", {"file_path": "proj/src/a.py", "old_string": "a", "new_string": "b"})])
    check(not is_block(c.call(cwd=c.root, transcript=tp)), "relative path must not pick a project")


def test_edit_outside_any_ledger_project_does_not_block():
    c = Ctx("outside", [mk_task("S9-T1")])
    other = c.root / "other"
    other.mkdir()
    tp = transcript(c.root, [human("go"), tool_use("Edit", {"file_path": str(other / "a.py"), "old_string": "a", "new_string": "b"})])
    check(not is_block(c.call(cwd=c.root, transcript=tp)), "edit outside the ledger project blocked")


def test_read_only_tool_in_project_does_not_tie_session():
    """MAJOR-05: the project is picked from Edit/Write/NotebookEdit calls only. A session that merely READ a file in the
    project (Read/Grep/Glob) from a parent folder must not be trapped by that project's open tasks."""
    c = Ctx("readonly", [mk_task("S9-T1")])
    for name, inp in (("Read", {"file_path": str(c.proj / "src" / "a.py")}), ("Grep", {"pattern": "x", "path": str(c.proj)}),
                      ("Glob", {"pattern": "*.py", "path": str(c.proj)})):
        tp = transcript(c.root, [human("look around"), tool_use(name, inp)])
        p = c.call(cwd=c.root, transcript=tp, session="s-" + name)
        check(not is_block(p), f"{name} of a project file tied the session: {p.stdout!r}")


def test_shell_text_naming_project_does_not_pick_it():
    c = Ctx("shelltxt", [mk_task("S9-T1")])
    tp = transcript(c.root, [human("go"), tool_use("Bash", {"command": f"cd {c.proj} && git commit -m x"})])
    check(not is_block(c.call(cwd=c.root, transcript=tp)), "shell text picked the project")


def test_several_projects_each_with_open_tasks_counts():
    c = Ctx("multi", [mk_task("S9-T1")])
    p2 = c.root / "proj2"
    p2.mkdir()
    write_ledger(p2, [sprint("S5", [mk_task("S5-T1", "in_progress")])])
    tp = transcript(c.root, [human("go"), tool_use("Edit", {"file_path": str(c.proj / "a.py")}, "u1"),
                             tool_use("Edit", {"file_path": str(p2 / "b.py")}, "u2")])
    p = c.call(cwd=c.root, transcript=tp)
    check(is_block(p) and "S9-T1" in decision(p)["reason"], p.stdout)
    c.set_tasks([mk_task("S9-T1", "done")])
    p = c.call(cwd=c.root, transcript=tp)
    check(is_block(p) and "S5-T1" in decision(p)["reason"], "second project with open tasks must still block: " + p.stdout)


# ---------- ownership ----------
def test_in_progress_started_by_another_session_is_not_blocked_on():
    c = Ctx("owner", [mk_task("S9-T1", "in_progress"), mk_task("S9-T2", "in_progress"), mk_task("S9-T3", "todo")])
    tp = transcript(c.root, [human("go"), tool_use("Bash", {"command": "python .harness-tools/ledger.py start S9-T2"})])
    p = c.call(transcript=tp)
    r = decision(p).get("reason", "")
    check(is_block(p) and "S9-T2" in r, p.stdout)
    check("S9-T1" not in r, "another session's in_progress task is listed: " + r)
    check("S9-T3" not in r, "a todo task outside this session's own ids must not hold the session: " + r)


def test_own_task_done_and_foreign_todo_does_not_block():
    c = Ctx("owner_done_todo", [mk_task("S9-T1", "done"), mk_task("S9-T2", "todo")])
    tp = transcript(c.root, [human("go"), tool_use("Bash", {"command": "python .harness-tools/ledger.py start S9-T1"})])
    p = c.call(transcript=tp)
    check(not is_block(p), "blocked by a todo task this session never started: " + p.stdout)
    # control: the same session owning T2 is blocked by it
    tp2 = transcript(c.root, [human("go"), tool_use("Bash", {"command": "python .harness-tools/ledger.py start S9-T2"})], "t2.jsonl")
    p2 = c.call(transcript=tp2, session="sess2")
    check(is_block(p2) and "S9-T2" in decision(p2)["reason"], p2.stdout)


def test_only_foreign_in_progress_task_does_not_block():
    c = Ctx("owner2", [mk_task("S9-T1", "in_progress"), mk_task("S9-T2", "done")])
    tp = transcript(c.root, [human("go"), tool_use("Bash", {"command": "python .harness-tools/ledger.py start S9-T2"})])
    check(not is_block(c.call(transcript=tp)), "blocked on a task another session is working on")


# ---------- release ----------
def test_release_after_five_no_progress_blocks():
    c = Ctx("rel5", [mk_task("S9-T1")])
    outs = [c.call() for _ in range(5)]
    check(all(is_block(p) for p in outs), [p.stdout for p in outs])
    p6 = c.call()
    check(not is_block(p6), f"6th call must be released: {p6.stdout!r}")
    check("no-progress" in sysmsg(p6) and "S9-T1" in sysmsg(p6), "release is not visible: " + p6.stdout)
    esc = [r for r in c.log() if r.get("verdict") == "ESCALATE"]
    check(len(esc) == 1 and esc[0].get("release_reason") == "no-progress-blocks", str(c.log()))
    check(not is_block(c.call()), "7th call must stay released")


def test_release_after_30_minutes():
    c = Ctx("rel30", [mk_task("S9-T1")])
    check(is_block(c.call()), "first call")
    c.age_streak(29 * 60)
    check(is_block(c.call()), "29 minutes must still block")
    c.age_streak(31 * 60)
    p = c.call()
    check(not is_block(p), f"31 minutes must release: {p.stdout!r}")
    check("30-minutes" in sysmsg(p), "release reason not visible: " + p.stdout)
    esc = [r for r in c.log() if r.get("verdict") == "ESCALATE"]
    check(len(esc) == 1 and esc[0].get("release_reason") == "30-minutes", str(c.log()))


def test_progress_resets_the_streak():
    c = Ctx("reset", [mk_task("S9-T1"), mk_task("S9-T2")])
    for _ in range(3):
        check(is_block(c.call()), "pre-progress block")
    c.set_tasks([mk_task("S9-T1", "done"), mk_task("S9-T2")])  # progress: a task became done
    outs = [c.call() for _ in range(5)]
    check(all(is_block(p) for p in outs), "after progress 5 more blocks are due (3+5 > 5 would have released without a reset): "
          + str([p.stdout[:60] for p in outs]))
    check(not is_block(c.call()), "release expected only after 5 no-progress blocks following the progress")


def test_state_change_without_done_resets_the_streak():
    c = Ctx("reset2", [mk_task("S9-T1")])
    for _ in range(4):
        c.call()
    c.set_tasks([mk_task("S9-T1", "in_progress")])
    check(c.streak() is not None, "streak must exist")
    outs = [c.call() for _ in range(5)]
    check(all(is_block(p) for p in outs), "state change is progress and must reset the counter")


def test_reset_renews_the_30_minute_clock():
    c = Ctx("reset3", [mk_task("S9-T1"), mk_task("S9-T2")])
    c.call()
    c.age_streak(40 * 60)
    c.set_tasks([mk_task("S9-T1", "done"), mk_task("S9-T2")])
    check(is_block(c.call()), "after progress the 30-minute clock must restart")


def test_head_change_alone_is_not_progress():
    repo = hv2.mk_repo("headchg")
    write_ledger(repo, [sprint(tasks=[mk_task("S9-T1")])])
    state = hv2.fresh("headchg-state")
    c = Ctx("headchg-unused", [mk_task("S9-T1")])
    c.proj, c.state = repo, state
    results = []
    for i in range(5):
        results.append(c.call())
        hv2.commit(repo, f"f{i}.txt", "x\n", f"commit {i}")  # HEAD moves between the stops
    check(all(is_block(p) for p in results), "first five calls must block")
    head_before = hv2.head(repo)
    p6 = c.call()
    check(not is_block(p6), f"HEAD changes must not reset the streak; 6th call should release: {p6.stdout!r}")
    check(hv2.head(repo) == head_before, "fixture sanity")


def test_question_does_not_advance_or_reset_the_streak():
    c = Ctx("qstreak", [mk_task("S9-T1")])
    for _ in range(2):
        c.call()
    n = c.streak()["n"]
    c.call("Which option?")
    check(c.streak()["n"] == n, f"a question stop changed the streak {n} -> {c.streak()['n']}")


def test_corrupt_state_file_still_blocks_and_recovers():
    c = Ctx("badstate", [mk_task("S9-T1")])
    c.call()
    c.state_file().write_text("{broken", encoding="utf-8")
    p = c.call()
    check(is_block(p), f"corrupt hook state must not disable the gate: {p.stdout!r} {p.stderr!r}")


# ---------- Codex payload ----------
def test_codex_stop_payload_blocks_and_reads_transcript_message():
    c = Ctx("codex", [mk_task("S9-T1")])
    cdir = c.root / ".codex" / "sessions"
    cdir.mkdir(parents=True)
    rows = [{"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                  "content": [{"type": "output_text", "text": "Should I continue with the next task?"}]}}]
    tp = transcript(cdir, rows, "rollout.jsonl")
    p = c.call(msg=None, transcript=tp, extra={"turn_id": "t-1"})
    check(not is_block(p), f"Codex question in transcript must not block: {p.stdout!r}")
    p = c.call(msg=PROSE, transcript=tp, extra={"turn_id": "t-1"})
    check(is_block(p) and "S9-T1" in decision(p)["reason"], f"Codex block form: {p.stdout!r}")


def test_codex_release_message_goes_to_stderr_not_stdout_json():
    c = Ctx("codexrel", [mk_task("S9-T1")])
    for _ in range(5):
        c.call(extra={"turn_id": "t-1"})
    p = c.call(extra={"turn_id": "t-1"})
    check(not is_block(p) and sysmsg(p) == "", "Codex must not get systemMessage: " + p.stdout)
    check("S9-T1" in p.stderr, "release must be visible on stderr: " + p.stderr)


# ---------- tools/ledger.py block / start ----------
def mk_ledger_repo(name, n=2):
    repo = hv2.mk_repo(name)
    hv2.setup_tasks(repo, n)
    return repo


GOOD_CAUSE = "service endpoint returns 503 since 09:00"


def test_ledger_block_requires_class_and_cause():
    repo = mk_ledger_repo("lb1")
    check(hv2.ledger(repo, "block", "S1-T1", "--cause", GOOD_CAUSE).rc != 0, "missing --class accepted")
    check(hv2.ledger(repo, "block", "S1-T1", "--class", "OPERATOR").rc != 0, "missing --cause accepted")
    check(hv2.ledger(repo, "block", "S1-T1", "--class", "NOPE", "--cause", GOOD_CAUSE).rc != 0, "unknown class accepted")
    check(hv2.ledger(repo, "block", "S1-T1", "--class", "operator", "--cause", GOOD_CAUSE).rc != 0, "lowercase class accepted")
    check("blocked" not in hv2.task(repo, "S1-T1"), "failed block attempts must not write the field")


def test_ledger_block_cause_length_boundary():
    repo = mk_ledger_repo("lb2")
    for bad in ("123456789", "   short   ", "         ", ""):
        r = hv2.ledger(repo, "block", "S1-T1", "--class", "EXTERNAL", "--cause", bad)
        check(r.rc != 0, f"cause {bad!r} accepted: {r.all}")
        check("blocked" not in hv2.task(repo, "S1-T1"), f"cause {bad!r} wrote the field")
    r = hv2.ledger(repo, "block", "S1-T1", "--class", "EXTERNAL", "--cause", "1234567890")
    check(r.rc == 0, "10-char cause rejected: " + r.all)
    b = hv2.task(repo, "S1-T1")["blocked"]
    check(b["class"] == "EXTERNAL" and b["cause"] == "1234567890" and b.get("at"), str(b))


def test_ledger_block_writes_field_and_event_only_for_that_task():
    repo = mk_ledger_repo("lb3")
    r = hv2.ledger(repo, "block", "S1-T1", "--class", "OPERATOR", "--case", "money", "--cause", GOOD_CAUSE)
    check(r.rc == 0, r.all)
    b = hv2.task(repo, "S1-T1")["blocked"]
    check(b["class"] == "OPERATOR" and b["cause"] == GOOD_CAUSE, str(b))
    check("blocked" not in hv2.task(repo, "S1-T2"), "other task touched")
    ev = [e for e in hv2.load_ledger(repo)["events"] if e["kind"] == "block"]
    check(len(ev) == 1 and ev[0]["task"] == "S1-T1" and ev[0]["klass"] == "OPERATOR", str(ev))
    check(hv2.task(repo, "S1-T1")["state"] == "todo", "block must not change the state")


def test_ledger_block_unknown_and_done_task_rejected():
    repo = mk_ledger_repo("lb4")
    r = hv2.ledger(repo, "block", "S1-T9", "--class", "OPERATOR", "--cause", GOOD_CAUSE)
    check(r.rc != 0 and "not found" in r.all, r.all)
    led = hv2.load_ledger(repo)
    led["sprints"][0]["tasks"][0]["state"] = "done"
    (repo / ".harness" / "ledger.json").write_text(json.dumps(led), encoding="utf-8")
    r = hv2.ledger(repo, "block", "S1-T1", "--class", "OPERATOR", "--cause", GOOD_CAUSE)
    check(r.rc != 0 and "blocked" not in hv2.task(repo, "S1-T1"), "done task was blocked: " + r.all)


def test_ledger_start_removes_block_and_status_still_works():
    repo = mk_ledger_repo("lb5")
    hv2.ledger(repo, "block", "S1-T1", "--class", "PROHIBITED", "--cause", GOOD_CAUSE)
    st = hv2.ledger(repo, "status")
    check(st.rc == 0 and "S1-T1" in st.out and "S1-T2" in st.out, "status with a blocked task: " + st.all)
    r = hv2.ledger(repo, "start", "S1-T1")
    check(r.rc == 0, r.all)
    t = hv2.task(repo, "S1-T1")
    check("blocked" not in t and t["state"] == "in_progress", str(t))
    st = hv2.ledger(repo, "status")
    check(st.rc == 0 and "S1-T1" in st.out, st.all)
    nx = hv2.ledger(repo, "next")
    check(nx.rc == 0, nx.all)


def test_gate_follows_ledger_block_and_start_end_to_end():
    repo = mk_ledger_repo("e2e", 1)
    c = Ctx("e2e-unused", [mk_task("X")])
    c.proj, c.state = repo, hv2.fresh("e2e-state")
    check(is_block(c.call()), "open task must block")
    hv2.ledger(repo, "block", "S1-T1", "--class", "EXTERNAL", "--cause", GOOD_CAUSE)
    check(not is_block(c.call()), "ledger.py block must release the gate")
    hv2.ledger(repo, "start", "S1-T1")
    p = c.call()
    check(is_block(p) and "S1-T1" in decision(p)["reason"], "ledger.py start must re-arm the gate: " + p.stdout)


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
