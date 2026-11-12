#!/usr/bin/env python3
"""PROJECT_PLAN.md -> ledger-shaped dict, so tools/report.py renders the ONE global program status format for projects that
keep their plan in PROJECT_PLAN.md and have no .harness/ledger.json (operator order 2026-11-12: every project, same report).

Read-only: it never writes a plan or a ledger. Status mapping (PROJECT_PLAN.md -> ledger state):
  ACCEPTED -> done; IN_REVIEW -> verified; IN_PROGRESS -> in_progress; PARTIAL -> implemented; NOT_STARTED -> todo;
  BLOCKED_OPERATOR / BLOCKED_EXTERNAL / PARKED -> todo with a `blocked` field; SUPERSEDED -> left out (the plan's own
  formula excludes it too). A sprint is `closed` when every kept task is done, else `open`.
Stdlib only.
"""
from __future__ import annotations

import re
from pathlib import Path

MAP = {"ACCEPTED": "done", "IN_REVIEW": "verified", "IN_PROGRESS": "in_progress", "PARTIAL": "implemented", "NOT_STARTED": "todo",
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


def parse(text: str) -> dict:
    lines = text.splitlines()
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
    sprints: list[dict] = []
    while i < len(lines):
        s = re.match(r"^##\s+Sprint\s+(\S+)\s*[-—–:]*\s*(.*)$", lines[i], re.I)
        if not s:
            i += 1
            continue
        sid, title = s.group(1).rstrip(":"), s.group(2).strip()
        rows, i = _table(lines, i + 1)
        tasks = []
        for r in rows:
            status = re.sub(r"[^A-Z_]", "", (r.get("status") or "").upper()) or "NOT_STARTED"
            if status == "SUPERSEDED":
                continue
            commits = HEX_RE.findall(r.get("commit", "") or "")
            t = {"id": r.get("task") or r.get("id") or "?", "title": r.get("title", ""),
                 "criteria": [c for c in [r.get("acceptance") or r.get("acceptance criteria") or ""] if c],
                 "run": "", "risk": "local", "journey": False, "state": MAP.get(status, "todo"), "commits": commits,
                 "verify": None, "review": None, "routing": None, "tokens": None}
            if status.startswith("BLOCKED") or status == "PARKED":
                t["blocked"] = {"class": status, "cause": r.get("owner", "") or status, "at": None}
            if status == "ACCEPTED":
                ev = r.get("evidence", "") or "accepted in PROJECT_PLAN.md"
                last = commits[-1] if commits else None
                t["verify"] = {"result": "pass", "evidence": ev, "at": None, "task_commit": last}
                t["review"] = {"result": "accepted", "findings": 0, "at": None, "task_commit": last}
            tasks.append(t)
        done = bool(tasks) and all(t["state"] == "done" for t in tasks)
        sprints.append({"id": sid, "title": title, "branch": "", "state": "closed" if done else "open",
                        "opened": None, "closed": None, "tasks": tasks})
    return {"version": 1, "project": meta.get("project") or "", "sprints": sprints, "approval": None, "events": [],
            "source": "PROJECT_PLAN.md"}


def load(plan_file: Path) -> dict:
    d = parse(plan_file.read_text(encoding="utf-8-sig"))
    if not d["project"]:
        d["project"] = plan_file.resolve().parent.name
    return d
