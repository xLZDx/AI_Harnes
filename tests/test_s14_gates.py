"""S14 backlog-gate tests: dangerous_command_gate (T1), rules_gate (T2) and ledger hook-error handling (T4).
Written from the S14 acceptance criteria in .harness/ledger.json and core/DECISION_LOG.md ('S14-T1', 'S14-T2', 'S14-T4'), not from the code.
Hooks run as real subprocesses with JSON on stdin; every deny has an allowed twin. Ledgers are temp files under D:\\tmp\\harness_v2_tests
reached through HARNESS_LEDGER; no real project ledger is touched.
Run: C:\\Python314\\python.exe -I tests/test_s14_gates.py"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hv2  # noqa: E402
import test_s13_enforcement as enf  # noqa: E402  (gate())

PY = hv2.PY
HOOKS = hv2.SRC / "harness" / "hooks"
REVIEWER = "functional-test-reviewer"


def check(failures):
    assert not failures, "\n" + "\n".join(failures)


# ------------------------------------------------------------------ T1: dangerous_command_gate
def _verdict(r):
    assert r.rc == 0, r.all
    if not r.out.strip():
        return False
    return (json.loads(r.out).get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"


def dcg(cmd):
    """Verdict of dangerous_command_gate.py run alone."""
    state = hv2.fresh("s14-dcg")
    payload = {"hook_event_name": "PreToolUse", "session_id": "s14", "cwd": str(state), "tool_name": "Bash", "tool_input": {"command": cmd}}
    r = hv2.run([PY, "-I", "-s", "-X", "utf8", str(HOOKS / "dangerous_command_gate.py")], state,
                {"CLAUDE_HOOK_STATE_DIR": str(state / "claude_state")}, json.dumps(payload))
    return _verdict(r), r


def spg(cmd):
    """Verdict of the combined shell_policy_gate through run_hook."""
    state = hv2.fresh("s14-spg")
    payload = {"hook_event_name": "PreToolUse", "session_id": "s14", "cwd": str(state), "tool_name": "Bash", "tool_input": {"command": cmd}}
    r = hv2.run([PY, "-I", "-s", "-X", "utf8", str(HOOKS / "run_hook.py"), "shell_policy_gate.py", "--harness-client=claude"],
                state, {"HARNESS_HOOK_STATE": str(state), "CLAUDE_HOOK_STATE_DIR": str(state / "claude_state")}, json.dumps(payload))
    return _verdict(r), r


T1_DENY = [
    r'cd "C:\proj\" && rm -rf build',
    'wsl bash -c "x; rm -rf /y"',
    'docker exec c sh -c "rm -rf /data"',
    "podman exec c rm -rf /data",
    "kubectl exec p -- rm -rf /data",
    "ssh host rm -rf /y",
    'ssh host "rm -rf /y"',
    "wsl -e rm -rf /y",
    'su -c "rm -rf /y"',
    "runuser -u bob -- rm -rf /y",
    "chroot /mnt rm -rf /y",
    "bash <<EOF\nrm -rf /y\nEOF",
]
T1_ALLOW = [
    'git commit -m "format the disk report; done"',
    'echo "rm -rf x; ok"',
    "docker exec c ls",
    'ssh host "echo rm -rf later"',
    "kubectl exec p -- ls",
    'git commit -m "docker exec c rm -rf x"',
    "cat <<EOF\nrm -rf /y\nEOF",
]


def test_t1_dangerous_gate_denies_destructive_commands_behind_quotes_and_exec_wrappers():
    fails = []
    for cmd in T1_DENY:
        d, r = dcg(cmd)
        if not d:
            fails.append(f"NOT denied: {cmd!r} out={r.out!r} err={r.err!r}")
    check(fails)


def test_t1_dangerous_gate_allows_destructive_words_inside_data():
    fails = []
    for cmd in T1_ALLOW:
        d, r = dcg(cmd)
        if d:
            fails.append(f"wrongly denied: {cmd!r} out={r.out!r}")
    check(fails)


def test_t1_dangerous_gate_and_shell_policy_gate_give_the_same_verdict():
    fails = []
    for cmd in T1_DENY + T1_ALLOW:
        a, _ = dcg(cmd)
        b, _ = spg(cmd)
        if a != b:
            fails.append(f"MISMATCH dangerous={a} policy={b}: {cmd!r}")
    check(fails)


# ------------------------------------------------------------------ T2: rules_gate
FULL_RUNS = ["flutter test", "dart test", "go test", "go test ./...", "cargo test", "npx playwright test", "dotnet test",
             "mvn test", "gradle test", "./gradlew test"]
NAMED_RUNS = ["flutter test test/x_test.dart", "go test x_test.go", "cargo test --test integ", "playwright test tests/x.spec.ts"]
NOT_TESTS = ["go build ./...", "cargo build"]
SUBST_DENY = ["echo $(npm test)", "x=`npm test`", 'echo "$(npm test)"', "case $a in x) npm test;; esac"]
SUBST_ALLOW = ['echo "npm test"', "echo '$(npm test)'", 'git commit -m "run npm test later"', "echo $(date)"]


def test_t2_full_runs_denied_for_main_session_and_test_agents():
    fails = []
    for cmd in FULL_RUNS:
        for agent in ("", REVIEWER):
            d, reason, r = enf.gate(cmd, "claude", agent)
            if not d:
                fails.append(f"[{agent or 'main'}] NOT denied: {cmd!r} out={r.out!r}")
            elif "rule 4" not in reason.lower():
                fails.append(f"[{agent or 'main'}] reason lacks 'rule 4': {cmd!r} -> {reason[:120]!r}")
    check(fails)


def test_t2_named_runs_denied_for_main_session_allowed_for_test_agent():
    fails = []
    for cmd in NAMED_RUNS:
        d, reason, r = enf.gate(cmd, "claude", "")
        if not d:
            fails.append(f"main session NOT denied: {cmd!r}")
        d, reason, r = enf.gate(cmd, "claude", REVIEWER)
        if d:
            fails.append(f"[{REVIEWER}] wrongly denied: {cmd!r}: {reason[:160]}")
    check(fails)


def test_t2_builds_are_not_test_runs():
    fails = []
    for cmd in NOT_TESTS:
        for agent in ("", REVIEWER):
            d, reason, r = enf.gate(cmd, "claude", agent)
            if d:
                fails.append(f"[{agent or 'main'}] wrongly denied: {cmd!r}: {reason[:160]}")
    check(fails)


def test_t2_substitution_backticks_and_case_branches_are_classified():
    fails = []
    for cmd in SUBST_DENY:
        for agent in ("", REVIEWER):
            d, reason, r = enf.gate(cmd, "claude", agent)
            if not d:
                fails.append(f"[{agent or 'main'}] NOT denied: {cmd!r} out={r.out!r}")
    check(fails)


def test_t2_quoted_text_and_harmless_substitution_stay_data():
    fails = []
    for cmd in SUBST_ALLOW:
        for agent in ("", REVIEWER):
            d, reason, r = enf.gate(cmd, "claude", agent)
            if d:
                fails.append(f"[{agent or 'main'}] wrongly denied: {cmd!r}: {reason[:160]}")
    check(fails)


# ------------------------------------------------------------------ T4: ledger hook errors
def led_env(path):
    return {"HARNESS_LEDGER": str(path)}


def make_ledger(name, task_ids, errors):
    """(repo, ledger path). errors: list of messages recorded as hook-error events, in order."""
    repo = hv2.mk_repo(name)
    path = hv2.fresh(name + "-led") / "ledger.json"
    tasks = [{"id": t, "title": t, "state": "todo", "risk": "local", "commits": []} for t in task_ids]
    events = [{"at": "2026-11-12T00:00:00+00:00", "kind": "hook-error", "message": m} for m in errors]
    path.write_text(json.dumps({"version": 1, "project": name, "approval": None, "events": events,
                                "sprints": [{"id": "S1", "title": "s", "state": "open", "tasks": tasks}] if tasks else []}), encoding="utf-8")
    return repo, path


def status(repo, path):
    r = hv2.ledger(repo, "status", env=led_env(path))
    assert r.rc == 0, r.all
    return r.out


def events(path):
    return json.loads(path.read_text(encoding="utf-8"))["events"]


def test_t4_unknown_task_error_hidden_once_the_task_exists():
    repo, path = make_ledger("s14-t4-resolved", ["S1-T1"], ["commit abc123def456 names unknown task S1-T1"])
    assert "WARNING" not in status(repo, path)
    repo2, path2 = make_ledger("s14-t4-open", ["S1-T1"], ["commit abc123def456 names unknown task S1-T9"])
    out = status(repo2, path2)  # twin: the id is not in the ledger, so the warning stays and counts it
    assert "WARNING: 1 hook error(s)" in out and "S1-T9" in out, out


def test_t4_ci_off_error_is_hidden_and_unknown_ids_are_counted():
    repo, path = make_ledger("s14-t4-cioff", ["S1-T1"], ["commit aaa111 names unknown task CI-OFF"])
    assert "WARNING" not in status(repo, path)
    repo, path = make_ledger("s14-t4-count", ["S1-T1"], [
        "commit aaa111 names unknown task CI-OFF", "commit bbb222 names unknown task X-1", "commit ccc333 names unknown task Y-2",
        "commit ddd444 names unknown task S1-T1"])
    out = status(repo, path)
    assert "WARNING: 2 hook error(s)" in out and "Y-2" in out, out


def test_t4_non_unknown_task_error_is_not_hidden_by_resolution():
    repo, path = make_ledger("s14-t4-other", ["S1-T1"], ["hook crashed while handling S1-T1"])
    assert "WARNING: 1 hook error(s)" in status(repo, path)


def test_t4_ack_errors_hides_warning_keeps_history_and_new_error_shows_again():
    repo, path = make_ledger("s14-t4-ack", ["S1-T1"], ["commit a1 names unknown task Q-1", "boom one"])
    assert "WARNING: 2 hook error(s)" in status(repo, path)
    r = hv2.ledger(repo, "ack-errors", env=led_env(path))
    assert r.rc != 0, "ack-errors without --reason must be refused"
    assert [e["kind"] for e in events(path)] == ["hook-error", "hook-error"], "refused ack must not write"
    r = hv2.ledger(repo, "ack-errors", "--reason", "old noise from before S14", env=led_env(path))
    assert r.rc == 0, r.all
    evs = events(path)
    acks = [e for e in evs if e["kind"] == "hook-error-ack"]
    assert len(acks) == 1 and acks[0]["reason"] == "old noise from before S14", evs
    assert [e["message"] for e in evs if e["kind"] == "hook-error"] == ["commit a1 names unknown task Q-1", "boom one"], "history kept"
    assert "WARNING" not in status(repo, path)
    # a new error after the ack shows again, and only that one is counted
    data = json.loads(path.read_text(encoding="utf-8"))
    data["events"].append({"at": "2026-11-12T01:00:00+00:00", "kind": "hook-error", "message": "boom two"})
    path.write_text(json.dumps(data), encoding="utf-8")
    out = status(repo, path)
    assert "WARNING: 1 hook error(s)" in out and "boom two" in out, out


def test_t4_commit_hook_records_housekeeping_commit_for_ci_off_and_still_errors_for_unknown_ids():
    repo, path = make_ledger("s14-t4-commit", ["S1-T1"], [])
    sha = hv2.commit(repo, "a.txt", "a\n", "disable ci", "Task: CI-OFF")
    r = hv2.ledger(repo, "commit-hook", env=led_env(path))
    assert r.rc == 0, r.all
    kinds = [(e["kind"], e.get("task")) for e in events(path)]
    assert ("housekeeping-commit", "CI-OFF") in kinds and not any(k == "hook-error" for k, _ in kinds), kinds
    assert [e for e in events(path) if e["kind"] == "housekeeping-commit"][0]["sha"] == sha
    assert "WARNING" not in status(repo, path)
    # twin: an unknown real-looking id is still an error
    hv2.commit(repo, "b.txt", "b\n", "other", "Task: ZZ-9")
    r = hv2.ledger(repo, "commit-hook", env=led_env(path))
    assert any(e["kind"] == "hook-error" and "ZZ-9" in e["message"] for e in events(path)), (r.all, events(path))
    assert "WARNING: 1 hook error(s)" in status(repo, path)


ALERTS = """
import json, sys
sys.path.insert(0, sys.argv[1])
import ledger, report
from pathlib import Path
d = ledger.load()
print(json.dumps(report.alerts_of(d, report.Git(Path(sys.argv[2])), {"head": "", "short": ""}), ensure_ascii=False))
"""


def alerts(repo, path):
    r = hv2.run([PY, "-I", "-c", ALERTS, str(hv2.TOOLS), str(repo)], repo, led_env(path))
    assert r.rc == 0, r.all
    return json.loads(r.out.strip().splitlines()[-1])


def test_t4_report_alerts_use_the_same_open_set():
    repo, path = make_ledger("s14-t4-rep-hidden", ["S1-T1"], ["commit a names unknown task S1-T1", "commit b names unknown task CI-OFF"])
    assert alerts(repo, path) == [], "hidden errors must be absent from the alerts"
    repo, path = make_ledger("s14-t4-rep-open", ["S1-T1"], ["commit a names unknown task S1-T1", "commit b names unknown task X-7"])
    al = alerts(repo, path)
    assert len(al) == 1 and ": 1," in al[0] and "X-7" in al[0], al  # twin: the open one is reported, the resolved one is not
    hv2.ledger(repo, "ack-errors", "--reason", "noise", env=led_env(path))
    assert alerts(repo, path) == []


def test_t4_report_alerts_stay_clean_for_errors_resolved_after_the_commit_hook_logged_them():
    # commit-hook writes the message to ledger-dir/hook-error.log as well as the ledger; a resolved error must not come back from the log
    repo, path = make_ledger("s14-t4-rep-log", ["S1-T1"], [])
    hv2.commit(repo, "a.txt", "a\n", "work", "Task: S1-T2")
    r = hv2.ledger(repo, "commit-hook", env=led_env(path))
    assert any(e["kind"] == "hook-error" for e in events(path)), r.all
    assert len(alerts(repo, path)) == 1  # still unknown: warned
    data = json.loads(path.read_text(encoding="utf-8"))
    data["sprints"][0]["tasks"].append({"id": "S1-T2", "title": "t", "state": "todo", "risk": "local", "commits": []})
    path.write_text(json.dumps(data), encoding="utf-8")
    assert "WARNING" not in status(repo, path)
    assert alerts(repo, path) == [], "resolved error resurfaced from hook-error.log"


def _with_log(name, task_ids, ledger_errors, log_lines, ack_at=None):
    repo, path = make_ledger(name, task_ids, ledger_errors)
    if ack_at:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["events"].append({"at": ack_at, "kind": "hook-error-ack", "reason": "noise", "count": 0})
        path.write_text(json.dumps(data), encoding="utf-8")
    (path.parent / "hook-error.log").write_text("".join(l + "\n" for l in log_lines), encoding="utf-8")
    return repo, path


def test_t4_log_lines_already_in_the_ledger_are_not_raised_again():
    resolved = "commit a names unknown task S1-T1"
    acked = "commit b names unknown task X-7"
    repo, path = _with_log("s14-t4-log-known", ["S1-T1"], [resolved, acked],
                           ["2026-11-12T05:00:00+00:00 " + resolved, "2026-11-12T05:00:01+00:00 " + acked], ack_at="2026-11-12T03:00:00+00:00")
    assert alerts(repo, path) == [], "resolved/acknowledged ledger errors came back from the log"
    # twin: the same text is still reported when the ledger has it open and no ack exists
    repo, path = _with_log("s14-t4-log-open", ["S1-T1"], [acked], ["2026-11-12T05:00:01+00:00 " + acked])
    al = alerts(repo, path)
    assert len(al) == 1 and ": 1," in al[0] and "X-7" in al[0], al


def test_t4_log_only_line_shown_only_when_newer_than_the_last_ack():
    ack = "2026-11-12T03:00:00+00:00"
    repo, path = _with_log("s14-t4-log-new", ["S1-T1"], [], ["2026-11-12T05:00:00+00:00 shell level crash NEW"], ack_at=ack)
    al = alerts(repo, path)
    assert len(al) == 1 and "shell level crash NEW" in al[0], al
    repo, path = _with_log("s14-t4-log-old", ["S1-T1"], [], ["2026-11-12T01:00:00+00:00 shell level crash OLD"], ack_at=ack)
    assert alerts(repo, path) == [], "log line older than the ack must be hidden"
    repo, path = _with_log("s14-t4-log-noack", ["S1-T1"], [], ["2026-11-12T01:00:00+00:00 shell level crash NOACK"])
    assert len(alerts(repo, path)) == 1  # without any ack an old log-only line is shown
    repo, path = _with_log("s14-t4-log-mixed", ["S1-T1"], [], ["2026-11-12T01:00:00+00:00 old one", "2026-11-12T05:00:00+00:00 new one"], ack_at=ack)
    al = alerts(repo, path)
    assert len(al) == 1 and ": 1," in al[0] and "new one" in al[0] and "old one" not in al[0], al


def test_t4_open_unknown_task_error_still_appears_next_to_log_lines():
    repo, path = _with_log("s14-t4-log-unknown", ["S1-T1"], ["commit c names unknown task Q-5"],
                           ["2026-11-12T05:00:00+00:00 commit c names unknown task Q-5"])
    al = alerts(repo, path)
    assert len(al) == 1 and ": 1," in al[0] and "Q-5" in al[0], al


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
