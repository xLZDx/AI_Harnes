#!/usr/bin/env python3
"""Stop hook: a substantive final answer must PROVE itself with evidence the hook can check.

Operator order 2026-10-09: an answer made from assumption (for example "this cannot be done / does not exist")
must not pass.  Truth cannot be judged mechanically, but PROOF can be checked:

  * A final message of MIN_CHARS or more must contain at least one evidence line:
        Evidence: `verbatim excerpt of a tool result from this turn` [, `another excerpt`]
    (also accepted: "Доказательство:" / "Proof:", and «...» quotes instead of backticks).
  * Every quoted excerpt (MIN_QUOTE chars or more) must occur verbatim, whitespace-normalised, in the tool
    results of the CURRENT turn (everything after the last human message).  A made-up or remembered quote fails.
  * A substantive answer in a turn with no tool call at all fails: there was nothing to quote.
  * A CLAIM word makes even a short answer (CLAIM_MIN_CHARS or more) need proof, in both directions: denial,
    refusal, absence ("не могу", "нельзя", "не существует", "нет доступа", "cannot" ...) and affirmation of
    existence, working or completion ("существует", "работает", "готово", "слито", "passed", "exists" ...).

Block once with an exact explanation; the loop guard (`stop_hook_active`) lets the corrected second attempt
through.  Short replies (< MIN_CHARS), subagents and errors are never blocked.  Stdlib only; fails open.
Kill switch: CLAUDE_UNVERIFIED_CLAIM_GATE=off.  Decisions are logged under _agentio.state_dir("citation_gate").

One file for Claude Code and Codex.  Claude transcripts are read as before.  A Codex transcript is read through
_agentio: tool calls often appear as one `exec` custom tool call, so the evidence is the text of every tool OUTPUT
of the current turn, and a turn counts as "made a tool call" when it has a tool call or a tool output.
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

MIN_CHARS = 200
CLAIM_MIN_CHARS = 6
MIN_QUOTE = 6
# Claim words make even a short answer need proof, in BOTH directions: denial / refusal / absence ...
NEGATIVE_RE = re.compile(
    r"(не\s+могу|не\s+смогу|не\s+сделаю|не\s+буду|нельзя|невозможно|нет\s+доступа|нет\s+такого|нет\s+данных|"
    r"не\s+существует|не\s+найден|не\s+нашёл|не\s+нашел|отсутствует|не\s+поддерживается|не\s+предусмотрен|"
    r"не\s+работает|не\s+умеет|не\s+подтвержд|"
    r"cannot|can't|won't\s+do|no\s+access|does\s+not\s+exist|doesn't\s+exist|not\s+found|not\s+supported|"
    r"is\s+impossible|not\s+working|i\s+do\s+not\s+have)", re.IGNORECASE)
# ... and affirmation of existence / working / completion.
POSITIVE_RE = re.compile(
    r"(существует|имеется|есть\s+доступ|работает|заработал|готово|выполнено|исправлено|сделано|слит[оаы]?\b|"
    r"проходит|прошл[иао]|подтвержд[её]н|проверено|доказано|"
    r"\b(?:exists|is\s+working|works|done|fixed|merged|passes|passed|verified|confirmed)\b)", re.IGNORECASE)
EVIDENCE_LINE_RE = re.compile(r"^\s*(?:[*_>\-]+\s*)?(?:\*\*)?(?:Evidence|Proof|Доказательств[ао])(?:\*\*)?\s*[:\-—]\s*(.+)$",
                              re.IGNORECASE | re.MULTILINE)
QUOTE_RE = re.compile(r"`([^`\n]+)`|«([^»\n]+)»")


def state_path() -> Path:
    return _agentio.state_dir("citation_gate") / "unverified_claim_decisions.jsonl"


def log(rec: dict) -> None:
    try:
        with state_path().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _is_human_turn_start(rec: dict) -> bool:
    if rec.get("type") != "user":
        return False
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list) or not content:
        return False
    only_results = all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    has_text = any(isinstance(b, dict) and b.get("type") == "text" for b in content)
    origin = rec.get("origin") or {}
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return False
    return has_text and not only_results


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return ""


def _codex_turn_evidence(transcript: Path) -> tuple[int, str] | None:
    """Codex variant of turn_evidence, built on the shared adapter."""
    if not transcript.is_file():
        return None
    turn = _agentio.current_turn(transcript)
    calls = sum(1 for r in turn if r["kind"] == "tool_use")
    results = [r.get("text", "") for r in turn if r["kind"] == "tool_result"]
    return max(calls, len(results)), norm("\n".join(results))


def turn_evidence(transcript: Path, client: str = "claude") -> tuple[int, str] | None:
    """(tool call count, normalised concatenation of all tool results) for the current turn."""
    if client == "codex":
        return _codex_turn_evidence(transcript)
    try:
        records = [json.loads(line) for line in transcript.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        return None
    start = 0
    for index, rec in enumerate(records):
        if _is_human_turn_start(rec):
            start = index
    calls, parts = 0, []
    for rec in records[start + 1 :]:
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if rec.get("type") == "assistant" and block.get("type") == "tool_use":
                calls += 1
            elif rec.get("type") == "user" and block.get("type") == "tool_result":
                parts.append(_result_text(block))
    return calls, norm("\n".join(parts))


def evidence_quotes(message: str) -> tuple[bool, list[str]]:
    lines = EVIDENCE_LINE_RE.findall(message)
    quotes = []
    for line in lines:
        for match in QUOTE_RE.finditer(line):
            quote = norm(match.group(1) or match.group(2) or "")
            if len(quote) >= MIN_QUOTE:
                quotes.append(quote)
    return bool(lines), quotes


FORMAT_HELP = (
    "Add a line of this exact form to your answer, quoting output you actually received this turn: "
    "`Evidence: `<verbatim excerpt of a tool result>``. The hook checks the excerpt against this turn's tool results."
)


def main() -> int:
    if os.environ.get("CLAUDE_UNVERIFIED_CLAIM_GATE", "").lower() in {"off", "0", "false"}:
        return 0
    data = json.loads(sys.stdin.read() or "{}")
    if data.get("stop_hook_active") or data.get("agent_type"):
        return 0
    kind = _agentio.client(data)
    message = data.get("last_assistant_message") or ""
    if not message and kind == "codex":  # Codex may send null here; read the transcript instead
        message = _agentio.last_assistant_text(data)
    claim =NEGATIVE_RE.search(message) or POSITIVE_RE.search(message)
    if len(message) < (CLAIM_MIN_CHARS if claim else MIN_CHARS):
        return 0
    transcript = data.get("transcript_path")
    turn = turn_evidence(Path(transcript), kind) if transcript else None
    if turn is None:
        return 0  # cannot inspect the transcript: fail open
    calls, results = turn
    has_line, quotes = evidence_quotes(message)
    reasons = []
    if calls == 0:
        reasons.append("this turn made no tool call, so nothing was checked and there is nothing to quote; "
                       "verify with a tool first")
    elif not has_line or not quotes:
        reasons.append("the answer has no evidence line with a quoted excerpt")
    missing = [q for q in quotes if q not in results]
    if missing and calls:
        reasons.append("these quoted excerpts do not occur verbatim in any tool result of this turn: "
                       + "; ".join(repr(q[:70]) for q in missing[:5]))
    verdict = "block" if reasons else "pass"
    log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": data.get("hook_event_name"), "chars": len(message),
         "tool_calls": calls, "quotes": len(quotes), "missing": len(missing), "verdict": verdict})
    if not reasons:
        return 0
    text = "UNPROVEN ANSWER: " + " | ".join(reasons) + ". " + FORMAT_HELP
    print(json.dumps({"decision": "block", "reason": text}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "verdict": "hook-error", "traceback": traceback.format_exc()})
        raise SystemExit(0)
