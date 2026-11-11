#!/usr/bin/env python3
"""Stop hook: a prohibition blocks only the prohibited action, never the rest of the development work.

Operator order 2026-10-14: a ban on tests / checks / migrations blocks exactly those actions.  While permitted
code, documentation, contract, backlog or safe local-commit steps remain, the turn must not end with
recommendations or a final status.

Truth about "is anything permitted left" cannot be judged mechanically, but the typical failure can be spotted:
a final message that defers work (recommendations, "next steps", "remaining", "if you want", blockers).  Such a
message is blocked ONCE with the self-check below; it is re-blocked on every attempt until the message explicitly says
done + committed + pushed (safety valve: released after MAX_BLOCKS consecutive blocks).  Subagents and short replies are never blocked.  Stdlib only; fails
open.  Kill switch: CLAUDE_KEEP_GOING_GATE=off.  Decisions are logged under _agentio.state_dir("citation_gate").
One file for Claude Code and Codex: both send the same Stop stdin; a Stop {"decision": "block", "reason"} makes
Codex continue the turn with the reason as the new prompt, which is exactly what this gate wants.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _agentio  # noqa: E402  shared Claude Code / Codex adapter

MIN_CHARS = 120
DEFERRAL_RE = re.compile(
    r"(рекоменд|следующ\w+\s+шаг|следующим\s+шагом|дальше\s+(?:нужно|можно|стоит)|осталось|остаётся|остается|"
    r"не\s+выполнял|не\s+запускал|заблокирован|блокер|если\s+хотите|если\s+нужно|хотите,?\s+чтобы|"
    r"могу\s+(?:также|дальше|продолжить)|остановил(?:ся|ась|ись)[,:\s-]+(?:потому|так\s+как|поскольку)|"
    r"stopped\s+(?:because|since)|"
    r"остал(?:ись|ась|ось|ся|ите)|остаются|остальн|не\s+проверен|не\s+проверял|не\s+подтвержд|не\s+сделан|"
    r"не\s+закрыт|не\s+завершен|не\s+завершён|не\s+реализован|не\s+исправлен|хвост|на\s+вас\b|"
    r"известные\s+ограничения|ограничени[яй]|по\s+вашему\s+слову|только\s+по\s+(?:вашему|твоему)|"
    r"требует\s+(?:вашего|оператор|решения)|нужно\s+(?:ваше|ваш\b|от\s+вас)|ждёт|ждет\s|"
    r"позже|отдельн\w+\s+(?:задач|спринт|гейт)|в\s+следующем\s+(?:спринте|гейте|pr)|"
    r"todo|not\s+verified|unverified|not\s+tested|not\s+done|left\s+to|pending|follow-?up|out\s+of\s+scope|"
    r"known\s+limit|waiting\s+for|"
    r"next\s+steps?|remaining|recommend|blocked|if\s+you\s+(?:want|like)|i\s+can\s+also|let\s+me\s+know)",
    re.IGNORECASE)

MIN_STATUS_CHARS = 400  # any message this long must end with an explicit closing line
CLOSING_RE = re.compile(
    r"(выполнимых\s+задач\s+не\s+осталось|nothing\s+doable\s+remains|"
    r"^\W*блокеры\s*:[^\n]*(?:operator|external|prohibited|оператор|внешн|запрещ))",
    re.IGNORECASE | re.MULTILINE)
MAX_BLOCKS = 8  # consecutive blocks per session before the safety valve releases the turn
DONE_RE = re.compile(r"(заверш|выполнен|сделан|готов|все\s+задачи|всё\s+сделано|all\s+done|complete[d]?)", re.IGNORECASE)
COMMIT_RE = re.compile(r"(закоммич|закомич|коммит\w*\s+(?:создан|сделан)|committed)", re.IGNORECASE)
PUSH_RE = re.compile(r"(запуш|pushed|отправлен\w*\s+в\s+(?:origin|remote|репозитор)|pr\s+(?:открыт|создан|opened|created))", re.IGNORECASE)


def _counts(data: dict, reset: bool = False) -> int:
    """Consecutive block counter per session (safety valve only)."""
    path = _agentio.state_dir("citation_gate") / "keep_going_counts.json"
    key = str(data.get("session_id") or "default")
    try:
        counts = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(counts, dict):  # truncated/corrupt content that still parses (e.g. a bare number)
            counts = {}
    except (OSError, ValueError):  # missing, unreadable or corrupt (UnicodeDecodeError is a ValueError)
        counts = {}
    counts[key] = 0 if reset else counts.get(key, 0) + 1
    try:
        _agentio.atomic_write_text(path, json.dumps(counts))
    except OSError:
        pass
    return counts[key]


SELF_CHECK = (
    "KEEP-GOING CHECK (skill task-status-continue): write a status table first (Item | State | Blocker class OPERATOR/EXTERNAL/PROHIBITED | Verified cause | Next action). A prohibition (tests, checks, migrations) blocks only that action, not development. "
    "Before finishing, classify every open item as DONE / DOABLE-NOW / BLOCKED (with the verified cause). "
    "Is there at least one permitted code or documentation step left (source, docs, contracts, backlog, safe "
    "local commit)? If yes, do it now instead of giving a final status or recommendations. If a dependent part "
    "is blocked, record it and continue the independent parts. End the answer with the line 'Выполнимых задач не осталось' or 'Блокеры: <OPERATOR|EXTERNAL|PROHIBITED> - <verified cause>'. Stop only when every remaining item is BLOCKED by "
    "the operator, external settings/apps or a prohibited action - and say so explicitly."
)


def log(rec: dict) -> None:
    try:
        with (_agentio.state_dir("citation_gate") / "keep_going_decisions.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def main() -> int:
    if os.environ.get("CLAUDE_KEEP_GOING_GATE", "").lower() in {"off", "0", "false"}:
        return 0
    data = json.loads(sys.stdin.read() or "{}")
    if data.get("agent_type"):
        return 0
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from repeat_work_gate import tripped_and_unanswered
        if tripped_and_unanswered(data):
            log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "verdict": "repeat-work-trip-release"})
            return 0  # repeat-work gate asked the operator; waiting for the answer is not stalling
    except Exception:
        pass
    message = data.get("last_assistant_message") or ""
    if not message and _agentio.client(data) == "codex":  # Codex may send null here; read the transcript instead
        message = _agentio.last_assistant_text(data)
    match =DEFERRAL_RE.search(message)
    substantive = len(message) >= MIN_STATUS_CHARS
    if not ((match and len(message) >= MIN_CHARS) or substantive):
        _counts(data, reset=True)
        return 0
    if CLOSING_RE.search(message):
        _counts(data, reset=True)
        return 0  # explicit closing line: nothing doable remains / blockers with an allowed class
    if DONE_RE.search(message) and COMMIT_RE.search(message) and PUSH_RE.search(message):
        _counts(data, reset=True)
        return 0  # explicit statement: everything finished, committed and pushed
    if _counts(data) > MAX_BLOCKS:
        log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "verdict": "cap-release", "cap": MAX_BLOCKS})
        return 0  # safety valve: never trap a session forever
    log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "verdict": "block", "marker": match.group(0) if match else "no-closing-line", "chars": len(message)})
    print(json.dumps({"decision": "block", "reason": SELF_CHECK}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "verdict": "hook-error", "traceback": traceback.format_exc()})
        raise SystemExit(0)
