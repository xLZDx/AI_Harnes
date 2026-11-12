#!/usr/bin/env python3
"""Import a markdown task ledger (docs/sdlc/sNN-task-ledger.md) into PROJECT_PLAN.md as a '## Sprint <id> - <title>' section.

The ledger rows look like `| ST-XXX-NN.NN #k | STATUS | Title | Basis |`. Statuses are mapped to the plan vocabulary
(HISTORICAL_ONLY, SUPERSEDED* -> SUPERSEDED; SOURCE_UNCOMMITTED, SOURCE_PRESENT, SOURCE_ONLY, PARTIAL -> PARTIAL;
BLOCKED -> BLOCKED_EXTERNAL when the Basis cell says EXTERNAL, else BLOCKED_OPERATOR (counted as 'defaulted to OPERATOR' when
no class word is given); ACCEPTED -> ACCEPTED; ACCEPTED qualified with BOUNDED -> ACCEPTED_BOUNDED, which the plan reader treats
as verified-not-done). An unknown or unlisted qualified status stops the import. A repeated task id keeps the LAST row. Every
table row of a status table that is not imported is counted ('skipped rows: K'); K>0 is an error unless --allow-skipped.
Idempotent: a second run gives a byte-identical plan. Atomic write (fsync, retry on Windows PermissionError), UTF-8, LF.
Stdlib only.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

STATUS_MAP = {
    "ACCEPTED": "ACCEPTED", "PARTIAL": "PARTIAL", "SOURCE_UNCOMMITTED": "PARTIAL", "SOURCE_PRESENT": "PARTIAL", "SOURCE_ONLY": "PARTIAL",
    "SUPERSEDED": "SUPERSEDED", "SUPERSEDED_OPERATOR_SCOPE": "SUPERSEDED", "HISTORICAL_ONLY": "SUPERSEDED",
    "BLOCKED": "BLOCKED", "BLOCKED_OPERATOR": "BLOCKED_OPERATOR", "BLOCKED_EXTERNAL": "BLOCKED_EXTERNAL",
    "NOT_STARTED": "NOT_STARTED", "TODO": "NOT_STARTED", "IN_PROGRESS": "IN_PROGRESS", "IN_REVIEW": "IN_REVIEW",
}
QUALIFIABLE = {"ACCEPTED", "PARTIAL", "BLOCKED"}  # only these may carry a qualifier such as "(BOUNDED RUNTIME)"
EVIDENCE_MAX = 300
MAX_LISTED_IDS = 20
HEX_RE = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{8,40}\b")
TASK_ID_RE = re.compile(r"^ST-[\w.-]+")
SEP_RE = re.compile(r"^\|\s*:?-{2,}")
GOAL_RE = re.compile(r"^\s*(?:\*\*)?Goal:(?:\*\*)?")
HEAD = "| Task | Title | Acceptance | Status | Evidence | Owner | Depends | Commit |"
SEP = "|---|---|---|---|---|---|---|---|"


class PlanChangedError(Exception):
    """The plan file changed on disk between the first read and the replace."""


def split_row(line: str) -> list[str]:
    return [c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def map_status(raw: str, basis: str, task: str) -> tuple[str, bool]:
    """(plan status, blocked_defaulted). A qualifier after the status word is allowed only where listed in QUALIFIABLE."""
    text = raw.strip()
    m = re.match(r"[A-Za-z][A-Za-z_]*", text)
    word = m.group(0).upper() if m else ""
    if word not in STATUS_MAP:
        raise ValueError(f"task {task}: unknown ledger status {raw!r}; known: {', '.join(sorted(STATUS_MAP))}")
    rest = text[m.end():].strip() if m else ""
    status = STATUS_MAP[word]
    if rest:
        if word not in QUALIFIABLE:
            raise ValueError(f"task {task}: qualified status {raw!r} is not listed; only {', '.join(sorted(QUALIFIABLE))} may carry a qualifier")
        if word == "ACCEPTED":
            if "BOUNDED" not in rest.upper():
                raise ValueError(f"task {task}: qualified status {raw!r} is not listed; an ACCEPTED qualifier must say BOUNDED")
            status = "ACCEPTED_BOUNDED"
    defaulted = False
    if status == "BLOCKED":
        b = basis.upper()
        if re.search(r"\bEXTERNAL\b", b):
            status = "BLOCKED_EXTERNAL"
        else:
            status = "BLOCKED_OPERATOR"
            defaulted = not re.search(r"\bOPERATOR\b", b)
    return status, defaulted


def cell(text: str, limit: int | None = None) -> str:
    text = re.sub(r"\s+", " ", text.replace("\\|", "|")).strip()
    if limit and len(text) > limit:
        text = text[: limit - 4].rstrip() + " ..."
    return text.replace("|", "\\|")


def header_index(cells: list[str]) -> dict[str, int] | None:
    """Column indexes of a status-table header (task/status/title/basis by NAME), or None when the row is no such header."""
    low = [c.lower() for c in cells]
    if "status" not in low:
        return None
    task = next((i for i, c in enumerate(low) if re.search(r"\b(task|id|story)\b", c) and not re.search(r"title|description", c)), None)
    if task is None:
        return None
    status = low.index("status")

    def find(*names: str) -> int:
        for n in names:
            for i, c in enumerate(low):
                if i not in (task, status) and n in c:
                    return i
        return -1

    title = find("title", "description", "criterion", "criteria", "acceptance")
    basis = find("basis", "evidence")
    if title < 0 and basis < 0:
        raise ValueError(f"status table header {cells!r} has neither a Title/Description nor a Basis/Evidence column")
    return {"task": task, "status": status, "title": title, "basis": basis}


def parse_ledger(text: str) -> tuple[list[dict], int, list[str]]:
    """Rows of every status table (header with a Task/ID/Story column and a Status column; 'next step' tables are skipped).
    Returns (rows, imported count, skipped table rows). A table header is a table row directly followed by a separator row.
    The column map is replaced only by another header; it is NOT reset by a body row or by prose, because the real ledgers
    continue a table after attempt-log paragraphs. Every body row of the active status table that is not imported is counted."""
    rows: list[dict] = []
    skipped: list[str] = []
    idx: dict[str, int] | None = None
    total = 0
    lines = [ln.strip() for ln in text.replace("\r\n", "\n").split("\n")]
    for n, s in enumerate(lines):
        if not s.startswith("|") or SEP_RE.match(s):
            continue
        cells = split_row(s)
        if n + 1 < len(lines) and SEP_RE.match(lines[n + 1]):  # a header row: starts a new table
            idx = header_index(cells)
            continue
        if idx is None:
            continue
        get = lambda k: cells[idx[k]] if 0 <= idx[k] < len(cells) else ""  # noqa: E731
        task = re.sub(r"\s+#", "#", get("task"))
        if not TASK_ID_RE.match(task):
            skipped.append(s)
            continue
        total += 1
        rows.append({"task": task, "status_raw": get("status"), "title": get("title"), "basis": get("basis"), "text": s})
    return rows, total, skipped


def build_rows(rows: list[dict]) -> tuple[list[dict], int, int]:
    last: dict[str, dict] = {}
    for r in rows:
        last.pop(r["task"], None)  # the LAST row of a repeated id wins and takes the last position
        last[r["task"]] = r
    out = []
    defaulted = 0
    for r in last.values():
        status, dflt = map_status(r["status_raw"], r["basis"], r["task"])
        defaulted += dflt
        commit = HEX_RE.search(r["basis"])
        out.append({"task": r["task"], "title": r["title"], "status": status, "evidence": r["basis"],
                    "commit": commit.group(0) if commit and status in ("ACCEPTED", "ACCEPTED_BOUNDED") else ""})
    return out, len(rows) - len(last), defaulted


def render_section(sid: str, title: str, goal: str, tasks: list[dict], pre: list[str] | None = None, tail: list[str] | None = None) -> list[str]:
    lines = [f"## Sprint {sid} - {title}"]
    if goal:
        lines.append(goal)
    if pre:
        lines += [""] + pre
    lines += ["", HEAD, SEP]
    for t in tasks:
        lines.append(f"| {cell(t['task'])} | {cell(t['title'], 200)} | {cell(t['title'], 200)} | {t['status']} | "
                     f"{cell(t['evidence'], EVIDENCE_MAX)} |  |  | {t['commit']} |")
    if tail:
        lines += [""] + tail
    return lines


def roadmap_row(lines: list[str], sid: str) -> dict:
    for ln in lines:
        c = split_row(ln) if ln.lstrip().startswith("|") else []
        if len(c) >= 4 and c[0].strip("*[] ").upper() == sid:
            return {"title": c[1], "goal": c[3]}
    return {}


def apply(plan_text: str, sid: str, title: str | None, tasks: list[dict]) -> tuple[str, dict]:
    lines = plan_text.split("\n")
    head = re.compile(r"^##\s+Sprint\s+" + re.escape(sid) + r"(\s|$|[-:])", re.I)
    starts = [i for i, ln in enumerate(lines) if head.match(ln)]
    start = starts[0] if starts else None
    info: dict = {"old_ids": [], "sections": len(starts)}
    road = roadmap_row(lines, sid)
    pre: list[str] = []
    tail: list[str] = []
    if start is not None:
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        old = lines[start + 1:end]
        goal = next((ln for ln in old if GOAL_RE.match(ln)), "")
        t0 = next((k for k, ln in enumerate(old) if ln.lstrip().startswith("|")), None)
        if t0 is None:
            pre = [ln for ln in old if ln.strip() and ln != goal]
        else:
            t1 = next((k for k in range(t0, len(old)) if not old[k].lstrip().startswith("|")), len(old))
            for ln in old[t0:t1]:
                c = split_row(ln)
                if SEP_RE.match(ln.strip()) or re.fullmatch(r"(task|id|story)", c[0].strip().lower()):
                    continue
                info["old_ids"].append(c[0])
            pre = [ln for ln in old[:t0] if ln.strip() and ln != goal]
            tail = [ln for ln in old[t1:] if ln != goal]
            while tail and not tail[0].strip():
                tail.pop(0)
            while tail and not tail[-1].strip():
                tail.pop()
        if not title:
            m = re.match(r"^##\s+Sprint\s+\S+\s*[-—–:]*\s*(.*)$", lines[start])
            title = (m.group(1).strip() if m else "") or road.get("title") or f"Sprint {sid}"
    else:
        goal = ""
        title = title or road.get("title") or f"Sprint {sid}"
    if not goal:
        goal = f"Goal: {len(tasks)} ledger tasks imported from the markdown task ledger (statuses mapped to the plan vocabulary)."
    section = render_section(sid, title, goal, tasks, pre, tail)
    if start is None:
        body = "\n".join(lines).rstrip("\n")
        return body + "\n\n" + "\n".join(section) + "\n", info
    before = "\n".join(lines[:start]).rstrip("\n")
    after = "\n".join(lines[end:]).strip("\n")
    return before + "\n\n" + "\n".join(section) + "\n" + ("\n" + after + "\n" if after else ""), info


def atomic_write(path: Path, text: str, expected: bytes | None = None, retries: int = 5) -> None:
    """Temp file in the target folder, flushed and fsynced, original mode kept, replaced with a short retry on Windows
    PermissionError. With `expected`, the target bytes are compared again right before the replace (PlanChangedError)."""
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            shutil.copymode(path, tmp)
        except OSError:
            pass  # no original mode to keep (new file) or the OS has none
        if expected is not None and path.read_bytes() != expected:
            raise PlanChangedError(f"{path} changed on disk since it was read; nothing was written, run the import again")
        for attempt in range(retries):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == retries - 1:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ledger", required=True, help="markdown task ledger, e.g. docs/sdlc/s01-task-ledger.md")
    ap.add_argument("--plan", default="PROJECT_PLAN.md")
    ap.add_argument("--sprint", required=True, help="sprint id, e.g. S01")
    ap.add_argument("--title", help="sprint title (default: the existing section or Roadmap title)")
    ap.add_argument("--dry-run", action="store_true", help="print the counts, write nothing")
    ap.add_argument("--allow-skipped", action="store_true", help="do not fail when status-table rows were not imported")
    a = ap.parse_args(argv)
    sid = a.sprint.strip().strip("*[] ").upper()
    try:
        ledger_text = Path(a.ledger).read_text(encoding="utf-8-sig")
        plan_path = Path(a.plan)
        plan_bytes = plan_path.read_bytes()
        plan_text = plan_bytes.decode("utf-8-sig").replace("\r\n", "\n")
        rows, total, skipped = parse_ledger(ledger_text)
        tasks, repeated, defaulted = build_rows(rows)
        if not tasks:
            raise ValueError(f"no task rows found in {a.ledger}")
        new_text, info = apply(plan_text, sid, a.title, tasks)
    except (OSError, ValueError) as e:
        print(f"plan_import_ledger_md: {e}", file=sys.stderr)
        return 1
    counts = Counter(t["status"] for t in tasks)
    print(f"ledger rows: {total}; repeated ids dropped (last row kept): {repeated}; tasks written: {len(tasks)}")
    print("status counts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"defaulted to OPERATOR: {defaulted}")
    print(f"skipped rows: {len(skipped)}")
    for s in skipped[:MAX_LISTED_IDS]:
        print(f"  skipped: {s[:160]}")
    old = info["old_ids"]
    print(f"old rows removed: {len(old)}" + (f" (ids {', '.join(old[:MAX_LISTED_IDS])}{', ...' if len(old) > MAX_LISTED_IDS else ''})" if old else ""))
    if info["sections"] > 1:
        print(f"warning: the plan has {info['sections']} sections for sprint {sid}; only the first one is replaced", file=sys.stderr)
    if skipped and not a.allow_skipped:
        print(f"plan_import_ledger_md: {len(skipped)} status-table rows were not imported; fix the ledger or pass --allow-skipped",
              file=sys.stderr)
        return 1
    if new_text == plan_text:
        print("plan unchanged (already up to date)")
    elif a.dry_run:
        print(f"dry-run: {plan_path} not modified")
    else:
        try:
            atomic_write(plan_path, new_text, expected=plan_bytes)
        except PlanChangedError as e:
            print(f"plan_import_ledger_md: {e}", file=sys.stderr)
            return 1
        except OSError as e:
            print(f"plan_import_ledger_md: cannot write {plan_path}: {e}", file=sys.stderr)
            return 1
        print(f"wrote {plan_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
