#!/usr/bin/env python3
"""Stop hook: unexplained refs (D-N, S-N, G-N, T-N, E-N, etc.) must be clarified in-message.

Operator request: when an answer mentions (D-1), (S-5), (G-3), (T-7), (E-2), (F-4), (R-1), (U-3), etc.,
each one must be explained inline — what it refers to, why it matters, what changes.
Without explanation, the answer is ambiguous and blocks the reader.

Covers: Decisions, Sprints, Gates, Tasks, Epics, Findings, Risks, UX choices, and other tracked items.

Kill switch: CLAUDE_DECISION_EXPLAIN_GATE=off. Stdlib only; fails open.
Decisions are logged under _agentio.state_dir("citation_gate").
One file for Claude Code and Codex (same Stop stdin, same {"decision": "block"} output).
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
# Match ANY tracked-item code in brackets: (LETTERS+DIGITS+LETTERS?)
# Flexible to match future codes as they evolve: (D-1), (S00), (E-2b), (NEW-LOAD-LEAK), etc.
# Pattern: one or more letters, optional separators, digits, optional letters
DECISION_REF_RE = re.compile(
    r'\('
    r'[A-Z]'  # at least one letter
    r'[A-Z0-9\-_]*'  # any letters, digits, dashes, underscores
    r'\d'  # at least one digit
    r'[A-Z0-9\-_]*'  # any trailing letters, digits, dashes, underscores
    r'\)',
    re.IGNORECASE
)
GIT_HASH_RE = re.compile(r'\([0-9a-f]{7,40}\)', re.IGNORECASE)  # "(dc47d8a)" is a commit hash, not a decision code


def is_decision_ref(ref: str) -> bool:
    """False for a git commit hash in brackets (7-40 hex characters); True for every other bracketed code."""
    return not GIT_HASH_RE.fullmatch(ref)
EXPLANATION_WINDOW = 200  # chars after (D-N) to look for explanation


def _looks_explained(text: str, match_pos: int, ref: str) -> bool:
    """Check if this (D-N) ref is explained nearby (next ~200 chars or sentence)."""
    window = text[match_pos:match_pos + EXPLANATION_WINDOW]
    number = ref.strip('()')

    # Look for patterns that indicate explanation:
    # - "Decision X:" / "Решение X:"
    # - "Option X:" / "Вариант X:"
    # - "D-N это" / "D-N means"
    # - "D-N -" / "D-N —"
    # - A sentence with detailed description (not just a label)

    explanation_patterns = [
        rf'{number}\s*(?:это|is|means)',
        rf'{number}\s*(?::|—|-)',
        r'(?:решение|decision|option|вариант|выбор)\s+\w+',
        r'[A-Z][a-zа-я]{10,}[,.]',  # substantive explanation clause
    ]

    return any(re.search(pat, window, re.IGNORECASE) for pat in explanation_patterns)


def state_path() -> Path:
    return _agentio.state_dir("citation_gate") / "decision_explain_decisions.jsonl"


def log(rec: dict) -> None:
    try:
        with state_path().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


SELF_CHECK = (
    "DECISION REFS UNEXPLAINED: your message contains (D-N) decision references but does not explain what each one means. "
    "For each (D-1), (D-2), etc.: write inline what decision/option it names, why it matters, and what the choice affects. "
    "Examples:\n"
    "  ❌ 'PR #5 should go to phase2/main (D-2).' — what is (D-2)?\n"
    "  ✅ 'PR #5 should go to phase2/main (D-2: where to merge — recommend phase2 over merge-into-Phase-1-flags).' — clear.\n"
    "Use this format: (D-N: <what decision> — <your recommendation or the two/three options>)."
)


def main() -> int:
    if os.environ.get("CLAUDE_DECISION_EXPLAIN_GATE", "").lower() in {"off", "0", "false"}:
        return 0
    data = json.loads(sys.stdin.read() or "{}")
    if data.get("agent_type"):
        return 0
    message = data.get("last_assistant_message") or ""
    if not message and _agentio.client(data) == "codex":  # Codex may send null here; read the transcript instead
        message = _agentio.last_assistant_text(data)
    if len(message) < MIN_CHARS:
        return 0

    refs = [(m.group(), m.start()) for m in DECISION_REF_RE.finditer(message) if is_decision_ref(m.group())]
    if not refs:
        return 0

    unexplained = []
    for ref, pos in refs:
        if not _looks_explained(message, pos, ref):
            unexplained.append(ref)

    if not unexplained:
        log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "verdict": "pass", "refs_found": len(refs)})
        return 0

    log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "verdict": "block", "unexplained": unexplained, "total_refs": len(refs)})
    print(json.dumps({"decision": "block", "reason": SELF_CHECK}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "verdict": "hook-error", "traceback": traceback.format_exc()})
        raise SystemExit(0)
