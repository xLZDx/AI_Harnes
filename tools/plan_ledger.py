#!/usr/bin/env python3
"""PROJECT_PLAN.md -> ledger-shaped dict, so tools/report.py renders the ONE global program status format for projects that
keep their plan in PROJECT_PLAN.md and have no .harness/ledger.json (operator order 2026-11-12: every project, same report).

Read-only: it never writes a plan or a ledger. Status mapping (PROJECT_PLAN.md -> ledger state):
  ACCEPTED -> done; IN_REVIEW -> verified; IN_PROGRESS -> in_progress; PARTIAL -> implemented; NOT_STARTED -> todo;
  BLOCKED_OPERATOR / BLOCKED_EXTERNAL / PARKED -> todo with a `blocked` field; SUPERSEDED -> state `superseded` (kept: a sprint's
  size is its canonical placement count, the report shows these as their own category). A sprint is `closed` when it has at
  least one non-superseded task and every non-superseded task is done, else `open`.
Stdlib only.
"""
from __future__ import annotations

import re
from pathlib import Path

MAP = {"SUPERSEDED": "superseded", "ACCEPTED": "done", "IN_REVIEW": "verified", "IN_PROGRESS": "in_progress", "PARTIAL": "implemented", "NOT_STARTED": "todo",
       # ERP_MCP's own statuses (checked in its PROJECT_PLAN.md 2026-11-12): approved for a bounded scope with product cases still
       # NOT_RUN is "verified", never "done"; git activity or a hand-off without approval is work in progress; the rest is not started
       "BOUNDED_SCOPE_COMPLETE": "verified", "CLOSED_BOUNDED_SPRINT": "verified", "ACCEPTED_BOUNDED": "verified",
       "APPROVED_PUSHED": "verified", "DONE_SPRINT_LEVEL": "verified",
       "GIT_ACTIVITY_NOT_CLOSED": "in_progress", "HANDOFF_NO_APPROVE": "in_progress", "ACTIVE": "in_progress",
       "QUEUED": "todo", "UNCONFIRMED": "todo", "NOT_RUN": "todo", "FOLLOW_ON": "todo",
       "BLOCKED_OPERATOR": "todo", "BLOCKED_EXTERNAL": "todo", "PARKED": "todo"}
HEX_RE = re.compile(r"\b[0-9a-f]{7,40}\b")


def _table(lines: list[str], start: int) -> tuple[list[dict], int]:
    i = start
    while i < len(lines) and not lines[i].lstrip().startswith("|"):
        if lines[i].startswith("## "):
            return [], i
        i += 1
    if i + 1 >= len(lines):
        return [], i
    head = [c.strip().lower() for c in lines[i].strip().strip("|").split("|")]
    i += 2 if re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1]) else 1
    rows = []
    while i < len(lines) and lines[i].lstrip().startswith("|"):
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", lines[i].strip().strip("|"))]
        cells += [""] * (len(head) - len(cells))
        rows.append({head[k]: cells[k].replace("\\|", "|") for k in range(len(head))})
        i += 1
    return rows, i


PLACEMENT_RE = re.compile(r"(?<![0-9,.\-–—/])([0-9]+)\s+(?:task\s+)?placements?\b", re.I)
NOT_EXPANDED_RE = re.compile(r"not\s+(?:yet\s+)?expanded", re.I)
MAX_PLACEMENTS = 1000


def _norm_id(raw: str) -> str:
    return (raw or "").strip().strip("*[] ").upper()


def _roadmap(lines: list[str]) -> dict[str, dict]:
    """Roadmap rows that announce task placements -> {sprint id: {"n": N, "title": ...}}. A row that mentions 'placement' or
    'not expanded' but cannot be read completely (bad id, no count, ambiguous count, count above 1000) is a ValueError."""
    start = next((n for n, ln in enumerate(lines) if re.match(r"^##\s+Roadmap", ln, re.I)), None)
    if start is None:
        return {}
    rows, _ = _table(lines, start + 1)
    out: dict[str, dict] = {}
    for r in rows:
        goal = r.get("goal") or ""
        if "placement" not in goal.lower() and not NOT_EXPANDED_RE.search(goal):
            continue
        sid = _norm_id(r.get("sprint"))
        label = f"roadmap row {(r.get('sprint') or '').strip()!r}: {goal.strip()[:120]!r}"
        if not re.fullmatch(r"S[0-9]+", sid):
            raise ValueError(f"{label}: sprint id must look like S<number>")
        counts = {int(x) for x in PLACEMENT_RE.findall(goal)}
        if len(counts) != 1:
            raise ValueError(f"{label}: expected exactly one whole number of task placements, found {sorted(counts) or 'none'}")
        n = counts.pop()
        if n > MAX_PLACEMENTS:
            raise ValueError(f"{label}: {n} placements is above the limit {MAX_PLACEMENTS}")
        if sid in out:
            raise ValueError(f"{label}: sprint {sid} appears twice in the roadmap")
        out[sid] = {"n": n, "title": (r.get("title") or "").strip()}
    return out


def _placeholder_sprints(announced: dict[str, dict], have: set[str]) -> list[dict]:
    """Roadmap rows that announce N placements and have no '## Sprint <id>' section become a sprint of N placeholder tasks
    (state todo). Sprint state is 'planned' (not 'closed'/'open'): ledger.active_sprint still takes the first sprint that is not
    closed, and merged reports list ledger sprints first."""
    out = []
    for sid, a in announced.items():
        if sid in have or a["n"] <= 0:
            continue
        n = a["n"]
        tasks = [{"id": f"{sid}-PLACEHOLDER-{k}", "title": f"Planned placement {k} of {n} (task list not expanded in PROJECT_PLAN.md)",
                  "criteria": [], "run": "", "risk": "local", "journey": False, "state": "todo", "commits": [],
                  "verify": None, "review": None, "routing": None, "tokens": None} for k in range(1, n + 1)]
        out.append({"id": sid, "title": a["title"], "branch": "", "state": "planned", "opened": None,
                    "closed": None, "tasks": tasks, "expanded": False})
        have.add(sid)
    return out


def parse(text: str) -> dict:
    lines = text.replace("\r\n", "\n").split("\n")
    meta: dict[str, str] = {}
    i = 0
    if lines and lines[0].strip() == "---":
        i = 1
        while i < len(lines) and lines[i].strip() != "---":
            if ":" in lines[i]:
                k, v = lines[i].split(":", 1)
                meta[k.strip()] = re.sub(r"\s+#.*$", "", v).strip().strip("\"'")
            i += 1
        i += 1
    announced = _roadmap(lines)
    sprints: list[dict] = []
    while i < len(lines):
        s = re.match(r"^##\s+Sprint\s+(\S+)\s*[-—–:]*\s*(.*)$", lines[i], re.I)
        if not s:
            i += 1
            continue
        raw = _norm_id(s.group(1).rstrip(":"))
        lead = re.match(r"S[0-9]+", raw)  # "S05-Title" -> S05
        sid = lead.group(0) if lead else raw
        title = ((raw[lead.end():].lstrip("-—–:_ ") + " ") if lead and raw[lead.end():].strip("-—–:_ ") else "") + s.group(2).strip()
        title = title.strip()
        rows, i = _table(lines, i + 1)
        if rows and not ({"task", "id"} & set(rows[0]) and "status" in rows[0]):
            raise ValueError(f"Sprint {sid}: the task table needs a Task (or ID) column and a Status column, found {sorted(rows[0])}")
        tasks = []
        for r in rows:
            raw = re.split(r"[;(,/—–]", (r.get("status") or "").strip(), maxsplit=1)[0]  # "CLOSED_BOUNDED_SPRINT; PR #43 merged" -> first part
            status = re.sub(r"[^A-Z_]", "", re.sub(r"[\s-]+", "_", raw.strip().upper())) or "NOT_STARTED"
            if status not in MAP:  # a qualified form of a known compound status ("ACCEPTED BOUNDED TEST DELTA"); never a prefix of plain ACCEPTED
                base = next((k for k in sorted(MAP, key=len, reverse=True) if k != "ACCEPTED" and status.startswith(k + "_")), None)
                if base:
                    status = base
            if status not in MAP:
                raise ValueError(f"Sprint {sid}, task {r.get('task') or r.get('id') or '?'}: unknown status {r.get('status')!r}; "
                                 f"allowed: {', '.join(sorted(MAP))}")
            commits = HEX_RE.findall(r.get("commit", "") or "")
            t = {"id": r.get("task") or r.get("id") or "?", "title": r.get("title", ""),
                 "criteria": [c for c in [r.get("acceptance") or r.get("acceptance criteria") or ""] if c],
                 "run": "", "risk": "local", "journey": False, "state": MAP.get(status, "todo"), "commits": commits,
                 "verify": None, "review": None, "routing": None, "tokens": None}
            if status.startswith("BLOCKED") or status == "PARKED":
                t["blocked"] = {"class": status, "cause": r.get("owner", "") or status, "at": None}
            # no synthesized verify/review: the plan only says ACCEPTED, so the report must not show a test run or a review that
            # nobody recorded (the formula text names the plan as the source)
            tasks.append(t)
        if not tasks and announced.get(sid, {}).get("n", 0) > 0:
            raise ValueError(f"Sprint {sid}: the roadmap announces {announced[sid]['n']} placements but the section has 0 parsed tasks")
        kept = [t for t in tasks if t["state"] != "superseded"]
        done = bool(kept) and all(t["state"] == "done" for t in kept)
        sprints.append({"id": sid, "title": title, "branch": "", "state": "closed" if done else "open",
                        "opened": None, "closed": None, "tasks": tasks, "expanded": True})
    if not sprints:
        raise ValueError("no '## Sprint <id>' sections found in the plan")
    sprints.extend(_placeholder_sprints(announced, {sp["id"] for sp in sprints}))
    return {"version": 1, "project": meta.get("project") or "", "sprints": sprints, "approval": None, "events": [],
            "source": "PROJECT_PLAN.md"}


def load(plan_file: Path) -> dict:
    d = parse(plan_file.read_text(encoding="utf-8-sig"))
    if not d["project"]:
        d["project"] = plan_file.resolve().parent.name
    return d
