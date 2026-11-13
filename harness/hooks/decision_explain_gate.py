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


REAL_DECISION_RE = re.compile(r'(?:D-?\d+[A-Z]?|DEC-\d+[A-Z]?)', re.IGNORECASE)  # D-1, D1, D-2b, DEC-7
# Task / sprint / finding ids: S00, S11-T1, S13-R4, ST-SEC-01.01#1 ... are never decision refs.
TASK_ID_RE = re.compile(r'(?:S\d+(?:[-.][A-Z]*\d+[A-Z]?)*|ST-[A-Z0-9.\-#]+|[A-Z]+-[A-Z]+-\d[\w.\-]*|.*#.*)', re.IGNORECASE)
LOG_CODE_RE = re.compile(r'^\s*(?:[#>*\-|]+\s*)*\**([A-Z][A-Z0-9]*-\d+[A-Z]?)\**\s*(?:[:.)|—–\-]|$|\s)', re.IGNORECASE)


def project_codes(cwd: str) -> frozenset:
    """Codes that the project's DECISION_LOG.md defines as decisions (a log line or heading that starts with the code)."""
    try:
        p = Path(cwd or ".").resolve()
        for d in [p, *p.parents][:8]:
            for cand in (d / "core" / "DECISION_LOG.md", d / "DECISION_LOG.md"):
                if cand.is_file():
                    out = set()
                    for ln in cand.read_text(encoding="utf-8", errors="replace").splitlines():
                        m = LOG_CODE_RE.match(ln)
                        if m and not TASK_ID_RE.fullmatch(m.group(1)):
                            out.add(m.group(1).upper())
                    return frozenset(out)
    except (OSError, ValueError):
        pass
    return frozenset()


def is_decision_ref(ref: str, codes=frozenset()) -> bool:
    """True only for a real decision ref: D-<n>, D<n>, DEC-<n>, or a code DECISION_LOG.md defines.
    False for a git hash, a task/sprint id such as (S11-T1), (S00), (S13-R4), (ST-SEC-01.01#1), and any other bracketed code."""
    if GIT_HASH_RE.fullmatch(ref):
        return False
    code = ref.strip('()').strip()
    if TASK_ID_RE.fullmatch(code):
        return False
    return bool(REAL_DECISION_RE.fullmatch(code)) or code.upper() in codes
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


MAX_SAME_BLOCKS = 2


def _loop(data: dict, sig: str, reset: bool = False) -> int:
    """Consecutive blocks of the same signature (unexplained refs) in this session; 0 after a reset. Fails to 1 on error."""
    path = _agentio.state_dir("citation_gate") / "decision_explain_loop.json"
    key = str(data.get("session_id") or "default")
    try:
        try:
            st = json.loads(path.read_text(encoding="utf-8"))
            st = st if isinstance(st, dict) else {}
        except (OSError, ValueError):
            st = {}
        if reset:
            st.pop(key, None)
            n = 0
        else:
            rec = st.get(key) or {}
            n = int(rec.get("n", 0)) + 1 if rec.get("sig") == sig else 1
            st[key] = {"sig": sig, "n": n}
        st = dict(list(st.items())[-40:])
        _agentio.atomic_write_text(path, json.dumps(st))
        return n
    except (OSError, ValueError, TypeError):
        return 1


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

    codes = project_codes(str(data.get("cwd") or ""))
    refs = [(m.group(), m.start()) for m in DECISION_REF_RE.finditer(message) if is_decision_ref(m.group(), codes)]
    if not refs:
        return 0

    unexplained = []
    for ref, pos in refs:
        if not _looks_explained(message, pos, ref):
            unexplained.append(ref)

    if not unexplained:
        _loop(data, "", reset=True)
        log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "verdict": "pass", "refs_found": len(refs)})
        return 0

    if _loop(data, "|".join(sorted(set(unexplained)))) > MAX_SAME_BLOCKS:
        log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "verdict": "loop-release", "unexplained": unexplained, "cap": MAX_SAME_BLOCKS})
        return 0  # loop guard: the same message signature is never blocked more than MAX_SAME_BLOCKS times in a row

    log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "verdict": "block", "unexplained": unexplained, "total_refs": len(refs)})
    print(json.dumps({"decision": "block", "reason": SELF_CHECK}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "verdict": "hook-error", "traceback": traceback.format_exc()})
        raise SystemExit(0)
