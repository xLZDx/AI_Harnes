"""closing_claim_gate (Stop hook, commit 00eb532) behavior tests. Written from the acceptance criteria in core/DECISION_LOG.md
('2026-11-12 closing_claim_gate') and the operator complaint, not from the code. The real hook script runs as a subprocess through
harness/hooks/run_hook.py with Stop JSON on stdin; repos are real git repos with .harness/ledger.json under D:\\tmp\\harness_v2_tests;
the loop-counter state folder is private to every call group (HARNESS_HOOK_STATE) and the machine config is switched off.
Every blocked input has an allowed twin so a gate that blocks everything fails too.
Run: C:\\Python314\\python.exe -I tests/test_s13_closing_claim.py"""
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hv2  # noqa: E402

PY = hv2.PY
RUNNER = hv2.SRC / "harness" / "hooks" / "run_hook.py"
CLAIM = "Выполнимых задач не осталось"
EN_CLAIM = "No doable tasks left"
REAL_LINE = ("ERP_MCP и PDCC: в файлах workflow остался автозапуск CI. "
             "Там незаконченная работа остановленных сессий, её я не трогаю.")
LAUNCH = "Async agent launched successfully.\nagentId: abc123 (internal ID - do not mention to user)\nThe agent is working in the background."


def check(failures):
    assert not failures, "\n" + "\n".join(failures)


def now_ts(minus_s=0):
    return datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() - minus_s, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ fixtures
def make_repo(name, tasks, sprint_state="open"):
    """Real git repo with a committed README and an untracked .harness/ledger.json. tasks: list of (id, state, blocked?)."""
    repo = hv2.mk_repo(name)
    ts = []
    for tid, state, blocked in tasks:
        t = {"id": tid, "title": "t " + tid, "state": state}
        if blocked:
            t["blocked"] = {"class": "OPERATOR", "cause": "needs operator"}
        ts.append(t)
    led = {"version": 1, "project": name, "sprints": [{"id": "S1", "title": "s", "state": sprint_state, "tasks": ts}],
           "approval": None, "events": []}
    (repo / ".harness").mkdir()
    (repo / ".harness" / "ledger.json").write_text(json.dumps(led), encoding="utf-8")
    return repo


def clean_repo(name="cc-clean"):
    return make_repo(name, [("S1-T1", "verified", False)])


def write_transcript(rows):
    p = hv2.fresh("cc-tr") / "t.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    return p


def user_row(text, cwd=None, ts=None):
    r = {"type": "user", "timestamp": ts or now_ts(), "message": {"role": "user", "content": text}}
    if cwd:
        r["cwd"] = str(cwd)
    return r


def launch_row(ts=None):
    return {"type": "user", "timestamp": ts or now_ts(), "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu1", "content": LAUNCH}]}}


def notification_row(agent_id="abc123"):
    return user_row(f"<task-notification><task-id>{agent_id}</task-id><status>completed</status></task-notification>")


def edit_row(path):
    return {"type": "assistant", "timestamp": now_ts(), "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "e1", "name": "Edit", "input": {"file_path": str(path), "old_string": "a", "new_string": "b"}}]}}


def claude_payload(message, repo, rows=None, **extra):
    rows = rows if rows is not None else [user_row("go", cwd=repo)]
    p = {"hook_event_name": "Stop", "session_id": "cc-" + uuid.uuid4().hex[:8], "cwd": str(repo),
         "transcript_path": str(write_transcript(rows)), "stop_hook_active": False, "last_assistant_message": message}
    p.update(extra)
    return p


def stop(payload, client="claude", env=None, state=None):
    """(blocked, reason, raw). Own state dir per call unless one is passed so the loop counter is isolated."""
    e = {"HARNESS_HOOK_STATE": str(state or hv2.fresh("cc-state")), "HARNESS_HOOK_CONFIG": str(hv2.TMP / "no-such-config.json")}
    e.update(env or {})
    r = hv2.run([PY, "-s", "-X", "utf8", str(RUNNER), "closing_claim_gate.py", f"--harness-client={client}"], hv2.TMP,
                env=e, inp=json.dumps(payload, ensure_ascii=False))
    assert r.rc == 0, (r.rc, r.all)
    if not r.out.strip():
        return False, "", r
    o = json.loads(r.out)
    return o.get("decision") == "block", o.get("reason", ""), r


# ------------------------------------------------------------------ 1. the complaint
def test_real_complaint_message_is_blocked_and_names_the_line():
    repo = clean_repo("cc-real")
    msg = f"Сделано всё остальное.\n{REAL_LINE}\n{CLAIM}"
    blocked, reason, r = stop(claude_payload(msg, repo))
    fails = []
    if not blocked:
        fails.append(f"NOT blocked: out={r.out!r} err={r.err!r}")
    elif "ERP_MCP и PDCC" not in reason or "не трогаю" not in reason:
        fails.append(f"reason does not quote the offending line: {reason[:300]!r}")
    check(fails)


def test_same_message_without_the_open_line_is_allowed():
    repo = clean_repo("cc-real-twin")
    blocked, reason, r = stop(claude_payload(f"Сделано всё остальное.\n{CLAIM}", repo))
    assert not blocked and not r.out.strip(), (reason, r.out, r.err)


def test_block_output_has_russian_and_english_reason_and_exit_zero():
    repo = clean_repo("cc-shape")
    blocked, reason, r = stop(claude_payload(f"{REAL_LINE}\n{CLAIM}", repo))
    assert blocked and r.rc == 0
    assert "Ты написал" in reason and "You wrote" in reason, reason


# ------------------------------------------------------------------ 2. open-item phrases in the message
OPEN_LINES = ["Релиз только в новой сессии.", "Тест не зелён, чинить позже.", "Прогон: NOT_RUN, нет окружения.",
              "Миграция not verified on staging.", "Остальное - next session.", "Это ещё не сделано.",
              "Один пункт не трогаю."]


def test_each_open_phrase_in_the_claim_message_blocks_and_names_its_line():
    repo = clean_repo("cc-phrases")
    fails = []
    for line in OPEN_LINES:
        blocked, reason, r = stop(claude_payload(f"Итог.\n{line}\n{CLAIM}", repo))
        if not blocked:
            fails.append(f"NOT blocked: {line!r} out={r.out!r}")
        elif line not in reason:
            fails.append(f"line not named: {line!r} -> {reason[:200]!r}")
    check(fails)


def test_english_claim_with_open_phrase_blocks_and_without_allows():
    repo = clean_repo("cc-en")
    b1, _, r1 = stop(claude_payload(f"Two items are NOT_RUN.\n{EN_CLAIM}", repo))
    b2, _, r2 = stop(claude_payload(f"All checks passed.\n{EN_CLAIM}", repo))
    assert b1, r1.out
    assert not b2, r2.out


def test_claim_is_matched_case_insensitively():
    repo = clean_repo("cc-case")
    b, _, r = stop(claude_payload("выполнимых задач не осталось. Но NOT_RUN остался.", repo))
    assert b, r.out


def test_claim_phrase_alone_is_not_an_open_phrase():
    """'не осталось' inside the claim must not trip the open phrase 'осталось'."""
    repo = clean_repo("cc-self")
    fails = []
    for msg in (CLAIM, f"Готово.\n{CLAIM}.", f"Готово. {CLAIM}", f"1. Сделано\n2. Проверено\n\n{CLAIM}"):
        b, reason, r = stop(claude_payload(msg, repo))
        if b:
            fails.append(f"wrongly blocked {msg!r}: {reason[:200]}")
    check(fails)


def test_open_word_on_the_same_line_as_the_claim_still_blocks():
    repo = clean_repo("cc-sameline")
    b, reason, r = stop(claude_payload(f"{CLAIM}, но осталось три пункта", repo))
    assert b, r.out
    assert "осталось три пункта" in reason, reason


def test_no_claim_means_not_checked_even_with_open_phrases_and_open_tasks():
    repo = make_repo("cc-noclaim", [("S1-T1", "pending", False)])
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    b, _, r = stop(claude_payload("Не трогаю это, NOT_RUN, next session.", repo))
    assert not b and not r.out.strip(), r.out


# ------------------------------------------------------------------ 3. operator-only lines
OPERATOR_ONLY = [
    "NOT_RUN: нужно подтвердить доверие к хуку (Codex hook trust approval) - платформа не даёт сделать это агенту.",
    "NOT_RUN: нужны real money и live trading, это операторское решение.",
    "Остался шаг: ввод own password оператора, я его не знаю.",
    "NOT_RUN: нужен CAPTCHA, пройти может только человек.",
    "Остался шаг: creating an account на сервисе, это делает оператор.",
    "NOT_RUN: нужно unrecoverable deletion старой ветки, без вашего слова не делаю.",
    "Остался force-push, это только оператор.",
    "NOT_RUN: нужна production data, к ней доступа нет.",
]


def test_operator_only_lines_with_open_words_are_allowed():
    repo = clean_repo("cc-opo")
    fails = []
    for line in OPERATOR_ONLY:
        b, reason, r = stop(claude_payload(f"Итог.\n{line}\n{CLAIM}", repo))
        if b:
            fails.append(f"operator-only line wrongly blocked: {line!r} -> {reason[:200]}")
    check(fails)


def test_operator_only_exception_is_per_line_not_per_message():
    repo = clean_repo("cc-opo-mix")
    msg = f"NOT_RUN: нужен CAPTCHA.\nИтог: релиз only next session, не трогаю.\n{CLAIM}"
    b, reason, r = stop(claude_payload(msg, repo))
    assert b, r.out
    assert "релиз only next session" in reason and "CAPTCHA" not in reason, reason


# ------------------------------------------------------------------ 4. Блокеры / Blockers
def test_blockers_ending_without_claim_is_not_checked():
    repo = make_repo("cc-blk", [("S1-T1", "pending", False)])
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    fails = []
    for msg in ("Не трогаю остальное, NOT_RUN.\nБлокеры: OPERATOR - нужен пароль", "Remaining work is not verified.\nBlockers: EXTERNAL - API down"):
        b, reason, r = stop(claude_payload(msg, repo))
        if b:
            fails.append(f"wrongly blocked: {msg!r} -> {reason[:200]}")
    check(fails)


# ------------------------------------------------------------------ 5. ledger
def test_open_ledger_task_without_running_agent_blocks_and_names_task():
    repo = make_repo("cc-led", [("S1-T1", "verified", False), ("S1-T2", "in_progress", False)])
    b, reason, r = stop(claude_payload(CLAIM, repo))
    assert b, r.out
    assert "S1-T2" in reason and "S1-T1" not in reason, reason


def test_pending_task_blocks_too():
    repo = make_repo("cc-led-pend", [("S1-T1", "pending", False)])
    b, reason, r = stop(claude_payload(CLAIM, repo))
    assert b and "S1-T1" in reason, (r.out, r.err)


def test_closed_states_do_not_block():
    fails = []
    for name, tasks, sprint in (("verified", [("S1-T1", "verified", False)], "open"),
                                ("done", [("S1-T1", "done", False)], "open"),
                                ("blocked-flag", [("S1-T1", "in_progress", True)], "open"),
                                ("closed-sprint", [("S1-T1", "pending", False)], "closed")):
        repo = make_repo("cc-led-" + name, tasks, sprint)
        b, reason, r = stop(claude_payload(CLAIM, repo))
        if b:
            fails.append(f"{name}: wrongly blocked: {reason[:200]}")
    check(fails)


def test_ledger_of_a_repo_the_session_did_not_work_in_is_ignored():
    mine = clean_repo("cc-mine")
    other = make_repo("cc-other", [("S1-T9", "pending", False)])
    b, reason, r = stop(claude_payload(CLAIM, mine))
    assert not b and "S1-T9" not in r.out, r.out
    assert other.is_dir()


def test_repo_reached_by_an_edit_call_is_checked_even_when_cwd_is_elsewhere():
    elsewhere = hv2.fresh("cc-nogit")
    repo = make_repo("cc-edited", [("S1-T3", "pending", False)])
    rows = [user_row("go", cwd=elsewhere), edit_row(repo / "README.md")]
    b, reason, r = stop(claude_payload(CLAIM, elsewhere, rows))
    assert b and "S1-T3" in reason, (r.out, r.err)
    # twin: the same session without the Edit call never touched that repo
    b2, _, r2 = stop(claude_payload(CLAIM, elsewhere, [user_row("go", cwd=elsewhere)]))
    assert not b2, r2.out


# ------------------------------------------------------------------ 6. running own agents
def test_open_task_covered_by_running_agent_named_with_task_id_is_allowed():
    repo = make_repo("cc-agent", [("S1-T1", "in_progress", False)])
    rows = [user_row("go", cwd=repo), launch_row()]
    msg = f"Запущен functional-test-reviewer для задачи S1-T1, он работает в фоне.\n{CLAIM}"
    b, reason, r = stop(claude_payload(msg, repo, rows))
    assert not b, reason


def test_same_open_task_with_no_running_agent_blocks():
    repo = make_repo("cc-agent-none", [("S1-T1", "in_progress", False)])
    msg = f"Запущен functional-test-reviewer для задачи S1-T1, он работает в фоне.\n{CLAIM}"
    b, reason, r = stop(claude_payload(msg, repo))  # transcript has no launch
    assert b and "S1-T1" in reason, (r.out, r.err)


def test_running_agent_but_message_does_not_name_the_task_blocks():
    repo = make_repo("cc-agent-noid", [("S1-T1", "in_progress", False)])
    rows = [user_row("go", cwd=repo), launch_row()]
    b, reason, r = stop(claude_payload(f"Агент работает в фоне.\n{CLAIM}", repo, rows))
    assert b and "S1-T1" in reason, (r.out, r.err)


def test_running_agent_covers_only_the_task_it_is_named_with():
    repo = make_repo("cc-agent-two", [("S1-T1", "in_progress", False), ("S1-T2", "pending", False)])
    rows = [user_row("go", cwd=repo), launch_row()]
    b, reason, r = stop(claude_payload(f"Агент tdd-guide работает над S1-T1.\n{CLAIM}", repo, rows))
    assert b, r.out
    assert "S1-T2" in reason and "S1-T1" not in reason, reason


def test_finished_agent_does_not_cover_the_task():
    repo = make_repo("cc-agent-done", [("S1-T1", "in_progress", False)])
    rows = [user_row("go", cwd=repo), launch_row(), notification_row("abc123")]
    b, reason, r = stop(claude_payload(f"Агент для S1-T1 завершён.\n{CLAIM}", repo, rows))
    assert b and "S1-T1" in reason, (r.out, r.err)


def test_stale_agent_launch_older_than_30_minutes_does_not_cover_the_task():
    repo = make_repo("cc-agent-stale", [("S1-T1", "in_progress", False)])
    rows = [user_row("go", cwd=repo), launch_row(ts=now_ts(minus_s=3 * 3600))]
    b, reason, r = stop(claude_payload(f"Агент для S1-T1 работает.\n{CLAIM}", repo, rows))
    assert b and "S1-T1" in reason, (r.out, r.err)


def test_task_id_that_is_only_a_prefix_of_another_named_id_is_not_covered():
    """Naming S1-T10 must not cover the different open task S1-T1."""
    repo = make_repo("cc-agent-prefix", [("S1-T1", "in_progress", False), ("S1-T10", "in_progress", False)])
    rows = [user_row("go", cwd=repo), launch_row()]
    b, reason, r = stop(claude_payload(f"Агент работает над S1-T10.\n{CLAIM}", repo, rows))
    assert b, f"S1-T1 was silently treated as covered: {r.out!r}"
    assert "S1-T1 " in reason or "S1-T1 (" in reason, reason


# ------------------------------------------------------------------ 7. uncommitted tracked changes
def test_uncommitted_tracked_change_blocks_and_clean_tree_allows():
    repo = clean_repo("cc-dirty")
    b0, _, r0 = stop(claude_payload(CLAIM, repo))
    assert not b0, r0.out
    (repo / "README.md").write_text("edited\n", encoding="utf-8")
    b, reason, r = stop(claude_payload(CLAIM, repo))
    assert b, r.out
    assert "README.md" in reason and "uncommitted" in reason, reason


def test_staged_change_blocks():
    repo = clean_repo("cc-staged")
    (repo / "README.md").write_text("edited\n", encoding="utf-8")
    assert hv2.git(repo, "add", "README.md").rc == 0
    b, reason, r = stop(claude_payload(CLAIM, repo))
    assert b and "README.md" in reason, (r.out, r.err)


def test_untracked_file_alone_does_not_block():
    repo = clean_repo("cc-untracked")
    (repo / "new.txt").write_text("x\n", encoding="utf-8")
    b, reason, r = stop(claude_payload(CLAIM, repo))
    assert not b, reason


def test_changes_under_harness_dir_and_live_reports_are_ignored_but_other_files_count():
    repo = clean_repo("cc-ignored")
    hv2.commit(repo, ".harness/state.json", "{}\n", "harness state")
    hv2.commit(repo, "reports/STATUS_LIVE.html", "<p>a</p>\n", "live report")
    hv2.commit(repo, "reports/final.html", "<p>a</p>\n", "final report")
    (repo / ".harness" / "state.json").write_text('{"a":1}\n', encoding="utf-8")
    (repo / "reports" / "STATUS_LIVE.html").write_text("<p>b</p>\n", encoding="utf-8")
    b1, reason1, r1 = stop(claude_payload(CLAIM, repo))
    assert not b1, reason1
    (repo / "reports" / "final.html").write_text("<p>b</p>\n", encoding="utf-8")
    b2, reason2, r2 = stop(claude_payload(CLAIM, repo))
    assert b2 and "final.html" in reason2, (r2.out, r2.err)


# ------------------------------------------------------------------ 8. skips, kill switch, loop guard
def test_subagent_is_skipped():
    repo = make_repo("cc-sub", [("S1-T1", "pending", False)])
    msg = f"{REAL_LINE}\n{CLAIM}"
    b0, _, r0 = stop(claude_payload(msg, repo))
    b1, _, r1 = stop(claude_payload(msg, repo, agent_type="tdd-guide"))
    assert b0, r0.out
    assert not b1 and not r1.out.strip(), r1.out


def test_kill_switch_off_disables_the_gate():
    repo = make_repo("cc-kill", [("S1-T1", "pending", False)])
    msg = f"{REAL_LINE}\n{CLAIM}"
    fails = []
    for val in ("off", "OFF", "0", "false"):
        b, _, r = stop(claude_payload(msg, repo), env={"HARNESS_CLOSING_CLAIM_GATE": val})
        if b:
            fails.append(f"kill switch {val!r} ignored")
    b_on, _, r_on = stop(claude_payload(msg, repo), env={"HARNESS_CLOSING_CLAIM_GATE": "on"})
    if not b_on:
        fails.append(f"value 'on' must keep the gate active: {r_on.out!r}")
    check(fails)


def test_same_blocked_message_is_released_on_the_third_try():
    repo = clean_repo("cc-loop")
    state = hv2.fresh("cc-loop-state")
    payload = claude_payload(f"{REAL_LINE}\n{CLAIM}", repo)
    results = [stop(payload, state=state)[0] for _ in range(4)]
    assert results == [True, True, False, False], results


def test_loop_counter_is_per_session_and_a_changed_message_starts_over():
    repo = clean_repo("cc-loop2")
    state = hv2.fresh("cc-loop2-state")
    p1 = claude_payload(f"{REAL_LINE}\n{CLAIM}", repo)
    assert [stop(p1, state=state)[0] for _ in range(3)] == [True, True, False]
    p_other_session = dict(p1, session_id="cc-other-session")
    assert stop(p_other_session, state=state)[0] is True
    p_changed = dict(p1, last_assistant_message=f"Релиз только в новой сессии.\n{CLAIM}")
    assert stop(p_changed, state=state)[0] is True


def test_clean_pass_resets_the_loop_counter():
    repo = clean_repo("cc-loop3")
    state = hv2.fresh("cc-loop3-state")
    bad = claude_payload(f"{REAL_LINE}\n{CLAIM}", repo)
    good = dict(bad, last_assistant_message=CLAIM)
    assert [stop(bad, state=state)[0] for _ in range(2)] == [True, True]
    assert stop(good, state=state)[0] is False
    assert stop(bad, state=state)[0] is True  # counter restarted, not released on what would be try 3


# ------------------------------------------------------------------ 9. Codex payload
def codex_rows(repo, final_text):
    def msg(role, text):
        return {"type": "response_item", "timestamp": now_ts(),
                "payload": {"type": "message", "role": role, "content": [{"type": "output_text" if role == "assistant" else "input_text", "text": text}]}}
    return [msg("user", "сделай"), msg("assistant", final_text)]


def test_codex_null_last_message_reads_transcript_and_blocks():
    repo = clean_repo("cc-codex")
    rows = codex_rows(repo, f"{REAL_LINE}\n{CLAIM}")
    p = claude_payload(None, repo, rows, turn_id="t1")
    b, reason, r = stop(p, client="codex")
    assert b, (r.out, r.err)
    assert "ERP_MCP и PDCC" in reason, reason


def test_codex_null_last_message_with_clean_transcript_message_allows():
    repo = clean_repo("cc-codex-ok")
    rows = codex_rows(repo, f"Готово.\n{CLAIM}")
    b, reason, r = stop(claude_payload(None, repo, rows, turn_id="t1"), client="codex")
    assert not b, reason


def test_codex_null_last_message_without_claim_in_transcript_allows():
    repo = make_repo("cc-codex-noclaim", [("S1-T1", "pending", False)])
    rows = codex_rows(repo, "Не трогаю это, NOT_RUN.")
    b, reason, r = stop(claude_payload(None, repo, rows, turn_id="t1"), client="codex")
    assert not b, reason


def test_codex_open_ledger_task_blocks():
    repo = make_repo("cc-codex-led", [("S1-T1", "pending", False)])
    rows = codex_rows(repo, CLAIM)
    b, reason, r = stop(claude_payload(None, repo, rows, turn_id="t1"), client="codex")
    assert b and "S1-T1" in reason, (r.out, r.err)


# ------------------------------------------------------------------ 10. robustness
def test_empty_or_broken_input_fails_open():
    fails = []
    for raw in ("", "{}", "not json", '{"last_assistant_message": 5}'):
        r = hv2.run([PY, "-s", "-X", "utf8", str(RUNNER), "closing_claim_gate.py", "--harness-client=claude"], hv2.TMP,
                    env={"HARNESS_HOOK_STATE": str(hv2.fresh("cc-state")), "HARNESS_HOOK_CONFIG": str(hv2.TMP / "none.json")}, inp=raw)
        if r.rc != 0 or "block" in r.out:
            fails.append(f"{raw!r}: rc={r.rc} out={r.out!r} err={r.err[:200]!r}")
    check(fails)


def test_missing_transcript_still_checks_message_and_cwd_repo():
    repo = make_repo("cc-notr", [("S1-T1", "pending", False)])
    p = claude_payload(CLAIM, repo)
    p["transcript_path"] = str(hv2.TMP / "does-not-exist.jsonl")
    b, reason, r = stop(p)
    assert b and "S1-T1" in reason, (r.out, r.err)


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
