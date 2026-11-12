#!/usr/bin/env python3
"""Question policy hook (operator 2026-10-21). One script, three events:

  PreToolUse  (AskUserQuestion): every question must carry >=2 options with a "(Recommended)" option placed
              first. If the operator has been silent for `idle_minutes` when the question is asked, the
              recommended option is accepted automatically (updatedInput.answers) and the decision is logged.
              Irreversible/destructive questions are never auto-accepted (they stay with the operator,
              CLAUDE.md section 3) unless config `include_destructive` is true.
  Stop:       a turn must not end with an open question to the operator written as plain prose; it has to go
              through AskUserQuestion. Blocks at most once per message.
  UserPromptSubmit: tells the assistant which questions were auto-answered while the operator was away.
  PermissionRequest (AskUserQuestion), operator 2026-11-01: a question that is STILL on screen after `pending_minutes`
              is answered with the recommended option of EVERY question at once, in order. Two phases: (1) the hook fires
              when the question is shown and records it; (2) it polls the transcript and, at the deadline, re-checks that
              nothing was answered meanwhile before answering. `pending_mode`: off | probe | live. `probe` (default until
              the behaviour is verified) only logs what the event delivers and, if `probe_sleep_seconds` > 0, runs a short
              auto-answer test; the Claude Code docs do not say whether this hook runs while the dialog is displayed.
  Notification: logged in probe mode (permission_prompt / idle_prompt timing).

Replaces ask_routing_gate.py (which routed operator questions to GPT-PM and denied them).
Kill switch: CLAUDE_QUESTION_POLICY=off. Config: <state folder>/config.json (state folder: CLAUDE_QUESTION_POLICY_STATE,
else _agentio.state_dir("question_policy")). Stdlib only; fails open.

Codex: there is no AskUserQuestion tool and no Notification event, so only Stop and UserPromptSubmit run there.
PreToolUse / PermissionRequest / Notification branches act on the Claude tool name AskUserQuestion only. On Codex the
Stop gate still blocks (once per message) an open prose question, but its text asks for numbered options with the
recommended one first, and a reply that already carries a "(Recommended)" option counts as compliant.
"""
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _agentio as agentio  # noqa: E402

STATE = Path(os.environ.get("CLAUDE_QUESTION_POLICY_STATE") or agentio.state_dir("question_policy"))
CONFIG = STATE / "config.json"
LEDGER = STATE / "auto_answers.jsonl"
DEFAULTS = {"idle_minutes": 10, "auto_accept": True, "include_destructive": False,
            "only_sessions": None, "stop_gate": True,
            "pending_mode": "live", "pending_minutes": 10, "probe_sleep_seconds": 0, "poll_seconds": 5}
# S7-T1 (operator 2026-11-12): a question that stays on screen is answered with the recommended option after
# pending_minutes. Risk-class questions are never auto-answered (GPT-PM plan round 2, BLOCKER-01).
RISK_RE = re.compile(
    r"delet|remov|drop\b|wipe|purge|destroy|force|reset|rotat|revoke|credential|secret|token|api[- ]?key|password|"
    r"access|permission|grant|payment|money|trade|trading|production|prod\b|deploy|account|"
    r"удал|сн[оё]с|уничтож|очист|ротац|ключ|токен|секрет|парол|доступ|разрешен|права|деньги|платёж|платеж|"
    r"боев|продакшн|аккаунт|учётн|учетн", re.I)
REC_RE = re.compile(r"\(\s*(recommended|рекомендую|рекомендуется|рекомендовано)\s*\)\s*$", re.I)
AUTOMATED = ("<task-notification>", "<system-reminder>", "[SYSTEM NOTIFICATION", "[Request interrupted", "Caveat:")

try:
    sys.path.insert(0, str(Path(__file__).parent))
    from ask_routing_gate import DESTRUCTIVE_RE, collect_text  # the old gate stays on disk as a library
except Exception:  # pragma: no cover - fallback if the old file is ever removed
    DESTRUCTIVE_RE = re.compile(r"delet|remov|drop|wipe|purge|force[- ]?push|reset --hard|удал|сн[оё]с|уничтож|очист", re.I)

    def collect_text(ti):
        out = []
        for q in ti.get("questions") or []:
            out += [str(q.get("question", "")), str(q.get("header", ""))]
            for o in q.get("options") or []:
                out += [str(o.get("label", "")), str(o.get("description", ""))]
        return "\n".join(out)


def cfg():
    c = dict(DEFAULTS)
    try:
        c.update(json.loads(CONFIG.read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    except Exception as exc:
        c["_config_error"] = str(exc)[:120]
    return c


def session_in_scope(c, sid):
    only = c.get("only_sessions")
    return True if not only else any(str(sid).startswith(p) for p in only)


def log_ledger(entry):
    """Append to the ledger; returns False when it could not be written (callers must not auto-answer undisclosed)."""
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        entry["ts"] = datetime.now(timezone.utc).isoformat()
        with LEDGER.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def tail_rows(path, max_bytes=6_000_000):
    rows = []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
        lines = data.split(b"\n")
        if size > max_bytes:
            lines = lines[1:]
        for ln in lines:
            if ln.strip():
                try:
                    rows.append(json.loads(ln))
                except Exception:
                    pass
    except Exception:
        pass
    return rows


def is_human(row):
    if row.get("isMeta") or row.get("isSidechain") or row.get("isCompactSummary"):
        return False
    t = row.get("type")
    if t == "user":
        if "toolUseResult" in row:
            return False
        if row.get("turnOrigin") not in (None, "human"):  # scheduled/system turns are not the operator
            return False
        c = (row.get("message") or {}).get("content")
        if isinstance(c, list):
            if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                return False
            c = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
        if not isinstance(c, str) or not c.strip():
            return False
        return not c.lstrip().startswith(AUTOMATED)
    if t == "attachment":
        a = row.get("attachment") or {}
        return a.get("type") == "queued_command" and (a.get("origin") or {}).get("kind") == "human"
    return False


def is_activity(row, gpt_ids=None):
    """Only the operator's own messages reset the silence clock (operator 2026-10-22). Machine traffic -- PM Bridge (GPT)
    replies and errors, scheduled/cron prompts, notifications, tool results -- does not: during a long bridge polling
    loop it used to keep the clock near zero while the operator had been away for hours."""
    return is_human(row)


def _row_time(row):
    try:
        return datetime.fromisoformat(str(row.get("timestamp")).replace("Z", "+00:00"))
    except Exception:
        return None


def idle_minutes(transcript_path):
    """Minutes since the operator's last own message; None if it cannot be determined.

    Only the tail of a large transcript is read. If that tail holds no operator message at all, the operator has been
    silent at least as long as the tail spans, so the age of its first row is returned as a lower bound."""
    rows = tail_rows(transcript_path)
    now = datetime.now(timezone.utc)
    for row in reversed(rows):
        if is_activity(row):
            ts = _row_time(row)
            return None if ts is None else (now - ts).total_seconds() / 60.0
    for row in rows:
        ts = _row_time(row)
        if ts is not None:
            return (now - ts).total_seconds() / 60.0
    return None


def asked_this_turn(transcript_path):
    # Codex transcripts (response_item rows): use the shared reader
    try:
        if any(r.get("type") == "response_item" for r in tail_rows(transcript_path)[-5:]):
            return any(c.get("name") in agentio.QUESTION_TOOLS for c in agentio.turn_tool_calls(transcript_path))
    except Exception:
        pass
    for row in reversed(tail_rows(transcript_path)):
        if is_human(row):
            return False
        msg = row.get("message") or {}
        if msg.get("role") == "assistant" and isinstance(msg.get("content"), list):
            for b in msg["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "AskUserQuestion":
                    return True
    return False


def out_json(obj):
    print(json.dumps(obj, ensure_ascii=False))
    sys.exit(0)


FORMAT_MSG = """Question policy (operator 2026-10-21): every question to the operator must be an AskUserQuestion with
options and a recommendation. Fix and ask again:
  - each question has at least 2 options;
  - exactly one option is your recommendation, it is the FIRST option, and its label ends with "(Recommended)";
  - the question says in words what is being decided, and each option says the concrete consequence.
If the operator does not answer, the recommended option is accepted, so recommend what you would do anyway.
Problems found: %s"""


def destructive_scope(qs):
    """Text the destructive-word check looks at: the `header` chip (operator 2026-10-25, a feature name in a neutral
    question is not an action) and, since S7-T1, the question text and the RECOMMENDED option's label/description,
    because that option is what an auto-answer would execute. Other options are not scanned."""
    parts = []
    for q in qs:
        parts += [str(q.get("header", "")), str(q.get("question", ""))]
        for o in q.get("options") or []:
            if isinstance(o, dict) and REC_RE.search(str(o.get("label", "")).strip()):
                parts += [str(o.get("label", "")), str(o.get("description", ""))]
                break
    return " | ".join(parts)


def risk_class(qs):
    text = destructive_scope(qs)
    return bool(DESTRUCTIVE_RE.search(text) or RISK_RE.search(text))


def pre(data, c):
    ti = data.get("tool_input") or {}
    qs = ti.get("questions")
    if not isinstance(qs, list) or not qs:
        return
    problems = []
    for i, q in enumerate(qs, 1):
        opts = [o for o in (q.get("options") or []) if isinstance(o, dict)]
        rec = [k for k, o in enumerate(opts) if REC_RE.search(str(o.get("label", "")).strip())]
        if len(opts) < 2:
            problems.append(f"question {i}: fewer than 2 options")
        if not rec:
            problems.append(f"question {i}: no option labelled (Recommended)")
        elif len(rec) > 1 and not q.get("multiSelect"):
            problems.append(f"question {i}: more than one (Recommended) option")
        elif rec[0] != 0:
            problems.append(f"question {i}: the recommended option must be first")
    if problems:
        out_json({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                         "permissionDecisionReason": FORMAT_MSG % "; ".join(problems)}})
    if not c["auto_accept"]:
        return
    idle = idle_minutes(data.get("transcript_path") or "")
    if idle is None or idle < float(c["idle_minutes"]):
        return  # operator is around: show the question normally
    if str(c.get("pending_mode", "probe")).lower() == "live":
        return  # operator 2026-11-01: keep the question on screen for pending_minutes; the PermissionRequest handler answers
    text = destructive_scope(qs)
    if risk_class(qs) and not c["include_destructive"]:
        log_ledger({"sid": str(data.get("session_id")), "action": "kept-for-operator-destructive", "q": text[:200]})
        return
    answers = {}
    for q in qs:
        opts = [o for o in (q.get("options") or []) if isinstance(o, dict)]
        answers[q.get("question", "")] = next(str(o["label"]) for o in opts if REC_RE.search(str(o.get("label", "")).strip()))
    log_ledger({"sid": str(data.get("session_id")), "action": "auto-accepted", "idle_min": round(idle, 1),
                "answers": answers})
    out_json({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "allow",
        "permissionDecisionReason": f"Operator silent for {idle:.0f} min (limit {c['idle_minutes']}): recommended options accepted.",
        "updatedInput": {**ti, "answers": answers}}})


def recommended_answers(qs):
    answers = {}
    for q in qs:
        opts = [o for o in (q.get("options") or []) if isinstance(o, dict)]
        rec = [str(o["label"]) for o in opts if REC_RE.search(str(o.get("label", "")).strip())]
        if not rec:
            return None
        answers[q.get("question", "")] = rec[0]
    return answers


def answered_since(transcript_path, since_ts, tool_use_id=None):
    """True when the question was dealt with after `since_ts`: an operator message arrived, or the tool_result of THIS
    question (matched by tool_use_id when known) appeared. Sidechain (subagent) rows and unrelated tool results are ignored."""
    for row in tail_rows(transcript_path, 2_000_000):
        if row.get("isSidechain"):
            continue
        t = _row_time(row)
        if t is None or t.timestamp() <= since_ts:
            continue
        if is_human(row):
            return True
        msg = row.get("message") or {}
        if row.get("type") == "user" and isinstance(msg.get("content"), list):
            for b in msg["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result" and (not tool_use_id or b.get("tool_use_id") == tool_use_id):
                    return True
    return False


def perm(data, c):
    ti = data.get("tool_input") or {}
    qs = ti.get("questions")
    mode = str(c.get("pending_mode", "probe")).lower()
    if mode not in ("probe", "live") or not isinstance(qs, list) or not qs:
        return  # "off" and any typo disable the handler; live must be spelled out
    sid = str(data.get("session_id"))
    start = time.time()
    log_ledger({"sid": sid, "action": "pending-seen", "mode": mode, "n_questions": len(qs),
                "payload_keys": sorted(data.keys()), "tool_use_id": data.get("tool_use_id"),
                "perm_mode": data.get("permission_mode")})
    answers = recommended_answers(qs)
    if answers is None:
        log_ledger({"sid": sid, "action": "pending-skip-no-recommended"})
        return
    if mode == "probe":
        wait = float(c.get("probe_sleep_seconds") or 0)
        if wait <= 0:
            return
    else:
        wait = float(c["pending_minutes"]) * 60.0
    if risk_class(qs) and not c["include_destructive"]:
        log_ledger({"sid": sid, "action": "pending-kept-for-operator-destructive"})
        return
    tp = data.get("transcript_path") or ""
    if mode == "live" and not (tp and os.path.exists(tp)):
        log_ledger({"sid": sid, "action": "pending-skip-no-transcript"})  # cannot re-check, so never answer blindly
        return
    tuid = data.get("tool_use_id")
    poll = max(0.2, float(c["poll_seconds"]))
    deadline = start + wait
    while time.time() < deadline:
        time.sleep(min(poll, max(0.2, deadline - time.time())))
        if answered_since(tp, start, tuid):  # phase 2 re-check: the operator already dealt with it
            log_ledger({"sid": sid, "action": "pending-answered-by-operator", "after_s": round(time.time() - start, 1)})
            return
    if answered_since(tp, start, tuid):
        log_ledger({"sid": sid, "action": "pending-answered-by-operator", "after_s": round(time.time() - start, 1)})
        return
    if not log_ledger({"sid": sid, "action": "auto-accepted", "via": "pending-" + mode, "waited_s": round(time.time() - start, 1),
                       "answers": answers}):
        return  # the operator could not be told about the auto-answer: leave the question to them
    out_json({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                     "decision": {"behavior": "allow", "updatedInput": {**ti, "answers": answers}}}})


def notification(data, c):
    if str(c.get("pending_mode", "probe")).lower() not in ("probe", "live"):
        return
    log_ledger({"sid": str(data.get("session_id")), "action": "notification",
                "type": data.get("notification_type"), "message": str(data.get("message"))[:120]})


QUESTION_TRIGGERS = re.compile(
    r"(скажите|подтвердите|выберите|решите|ответьте|нужно ваше решение|ваш выбор|как поступить|что выбрать|"
    r"хотите ли|хотите,? чтобы|согласны|давать ли|осталось в вашей части|"
    r"do you want|should i\b|shall i\b|let me know|please confirm|your call)", re.I)

STOP_MSG = """Question policy (operator 2026-10-21): your reply ends with an open question to the operator written as plain
text. Questions to the operator must go through AskUserQuestion with options and a recommendation (recommended option
first, label ends with "(Recommended)"; each option states its consequence). Call AskUserQuestion now instead of
asking in prose. If the operator stays silent, the recommended options are accepted, so choose them with that in mind.
Irreversible actions (deletion, history rewrite, production, real money) still wait for the operator."""


STOP_MSG_CODEX = """Question policy: your reply ends with an open question to the operator written as plain prose. Rewrite
it as a numbered list of at least 2 options, the recommended one first with "(Recommended)" at the end of its label,
each option stating its concrete consequence. Irreversible actions (deletion, history rewrite, production, real money)
still wait for the operator."""


def stop(data, c):
    if not c["stop_gate"] or data.get("agent_type") or data.get("stop_hook_active"):
        return
    msg = data.get("last_assistant_message") or ""
    tail = msg[-700:]
    lines = [ln.strip() for ln in tail.strip().splitlines() if ln.strip()]
    last = lines[-1] if lines else ""
    if not (last.rstrip("»)\"'` ").endswith("?") or QUESTION_TRIGGERS.search(tail)):
        return
    codex = agentio.client(data) == "codex"
    if codex and re.search(r"\((recommended|рекомендую|рекомендуется|рекомендовано)\)", msg, re.I):
        return  # options with a recommendation are already in the reply
    if asked_this_turn(data.get("transcript_path") or ""):
        return
    key =hashlib.sha1(msg.encode("utf-8", "replace")).hexdigest()[:16]
    seen_file = STATE / "stop_seen.json"
    try:
        seen = json.loads(seen_file.read_text(encoding="utf-8"))
    except Exception:
        seen = []
    if key in seen:
        return
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        seen_file.write_text(json.dumps((seen + [key])[-50:]), encoding="utf-8")
    except Exception:
        pass
    out_json({"decision": "block", "reason": STOP_MSG_CODEX if codex else STOP_MSG})


def prompt(data, c):
    sid = str(data.get("session_id"))
    marker = STATE / f"reported_{sid}.txt"
    try:
        last = marker.read_text(encoding="utf-8").strip()
    except Exception:
        last = ""
    new = []
    try:
        for ln in LEDGER.read_text(encoding="utf-8").splitlines():
            e = json.loads(ln)
            if e.get("sid") == sid and e.get("action") == "auto-accepted" and e.get("ts", "") > last:
                new.append(e)
    except Exception:
        return
    if not new:
        return
    try:
        marker.write_text(new[-1]["ts"], encoding="utf-8")
    except Exception:
        pass
    lines = []
    for e in new:
        for q, a in e["answers"].items():
            lines.append(f"- {q[:120]} -> {a}")
    ctx = ("[Question policy] While the operator was silent, these questions were auto-answered with the recommended "
           "option. Say so in your next reply and revert if the operator disagrees:\n" + "\n".join(lines))
    out_json({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx}})


def main():
    if os.environ.get("CLAUDE_QUESTION_POLICY", "").lower() == "off":
        return
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return
    c = cfg()
    if not session_in_scope(c, data.get("session_id")):
        return
    ev = data.get("hook_event_name") or ("PreToolUse" if "tool_input" in data else
                                         "UserPromptSubmit" if "prompt" in data else "Stop")
    if ev == "PermissionRequest" and data.get("tool_name") == "AskUserQuestion":
        perm(data, c)
    elif ev == "Notification":
        notification(data, c)
    elif ev == "PreToolUse" and data.get("tool_name") == "AskUserQuestion":
        pre(data, c)
    elif ev == "Stop":
        stop(data, c)
    elif ev == "UserPromptSubmit":
        prompt(data, c)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        pass  # fail open
    sys.exit(0)
