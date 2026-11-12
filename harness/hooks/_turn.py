"""Current-turn reader shared by the S7 gates (plan_first_gate, ask_last_gate, secret_guard).

Unlike _agentio.records(), it pairs each tool call with its RESULT by id (Claude transcripts) so a gate can judge what a command
actually returned: {name, input, id, result_text, is_error, text_before} per call, plus the assistant text written in the turn.
Claude format is parsed exactly; for Codex (`response_item` rows) calls are paired in order, is_error is None (unknown).
Pure stdlib, never raises (an unreadable transcript yields an empty turn).
"""
from __future__ import annotations

import json
from pathlib import Path

AUTOMATED = ("<task-notification>", "<system-reminder>", "[SYSTEM NOTIFICATION", "[Request interrupted", "Caveat:")
TAIL_BYTES = 8_000_000


def _rows(path: str) -> list[dict]:
    out: list[dict] = []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - TAIL_BYTES))
            data = fh.read()
        lines = data.split(b"\n")
        if size > TAIL_BYTES:
            lines = lines[1:]
        for ln in lines:
            if ln.strip():
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    pass
    except (OSError, TypeError):
        pass
    return out


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _is_human_claude(row: dict) -> bool:
    if row.get("type") != "user" or row.get("isMeta") or row.get("isSidechain") or row.get("isCompactSummary"):
        return False
    if "toolUseResult" in row or row.get("turnOrigin") not in (None, "human"):
        return False
    c = (row.get("message") or {}).get("content")
    if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
        return False
    t = _text(c).strip()
    return bool(t) and not t.startswith(AUTOMATED)


def read_turn(path: str) -> dict:
    """{'human_text','marker','calls','assistant_texts'} for the turn after the last genuine human message."""
    rows = _rows(path)
    if any(r.get("type") == "response_item" for r in rows[-8:]):
        return _read_codex(rows)
    start = 0
    human, marker = "", ""
    for i in range(len(rows) - 1, -1, -1):
        if _is_human_claude(rows[i]):
            start, human, marker = i, _text((rows[i].get("message") or {}).get("content")), str(rows[i].get("timestamp") or "")
            break
    else:
        # no genuine human row in the tail (a very long turn pushed it out, or the file is unreadable): the turn start is unknown,
        # so return an EMPTY turn - a gate then fails open visibly instead of judging a made-up turn
        return {"human_text": "", "marker": "", "calls": [], "assistant_texts": []}
    calls: list[dict] = []
    by_id: dict[str, dict] = {}
    texts: list[str] = []
    for r in rows[start + 1:]:
        if r.get("isSidechain"):
            continue
        content = (r.get("message") or {}).get("content")
        if r.get("type") == "assistant" and isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text"):
                    texts.append(str(b["text"]))
                    if calls:
                        calls[-1]["_after_text"] = True
                elif b.get("type") == "tool_use":
                    c = {"name": b.get("name") or "", "input": b.get("input") or {}, "id": b.get("id") or "",
                         "result": "", "is_error": None, "texts_before": len(texts)}
                    calls.append(c)
                    by_id[c["id"]] = c
        elif r.get("type") == "user" and isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in by_id:
                    c = by_id[b["tool_use_id"]]
                    c["result"] = _text(b.get("content")) if not isinstance(b.get("content"), str) else b["content"]
                    c["is_error"] = bool(b.get("is_error"))
    return {"human_text": human, "marker": marker, "calls": calls, "assistant_texts": texts}


def _read_codex(rows: list[dict]) -> dict:
    start = 0
    human, marker = "", ""
    for i in range(len(rows) - 1, -1, -1):
        p = rows[i].get("payload") or {}
        if rows[i].get("type") == "response_item" and p.get("type") == "message" and p.get("role") == "user":
            t = _text(p.get("content") if isinstance(p.get("content"), list) else [])
            t = " ".join(str(b.get("text", "")) for b in p.get("content") or [] if isinstance(b, dict))
            if t.strip() and not t.lstrip().startswith("<"):
                start, human, marker = i, t, str(rows[i].get("timestamp") or "")
                break
    else:
        return {"human_text": "", "marker": "", "calls": [], "assistant_texts": []}
    calls, texts = [], []
    pending = None
    for r in rows[start + 1:]:
        p = r.get("payload") or {}
        if r.get("type") != "response_item":
            continue
        if p.get("type") == "function_call":
            args = p.get("arguments")
            try:
                args = json.loads(args) if isinstance(args, str) else (args or {})
            except ValueError:
                args = {"raw": str(args)[:2000]}
            pending = {"name": p.get("name") or "", "input": args if isinstance(args, dict) else {}, "id": p.get("call_id") or "",
                       "result": "", "is_error": None, "texts_before": len(texts)}
            calls.append(pending)
        elif p.get("type") == "function_call_output" and calls:
            for c in calls:
                if c["id"] == p.get("call_id"):
                    c["result"] = str(p.get("output") or "")
        elif p.get("type") == "message" and p.get("role") == "assistant":
            t = " ".join(str(b.get("text", "")) for b in p.get("content") or [] if isinstance(b, dict))
            if t:
                texts.append(t)
    return {"human_text": human, "marker": marker, "calls": calls, "assistant_texts": texts}
