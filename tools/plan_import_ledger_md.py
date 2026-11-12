#!/usr/bin/env python3
"""Import a markdown task ledger (docs/sdlc/sNN-task-ledger.md) into PROJECT_PLAN.md as a '## Sprint <id> - <title>' section.

The ledger rows look like `| ST-XXX-NN.NN #k | STATUS | Title | Basis |`. Statuses are mapped to the plan vocabulary
(HISTORICAL_ONLY, SUPERSEDED* -> SUPERSEDED; SOURCE_UNCOMMITTED, SOURCE_PRESENT, SOURCE_ONLY, PARTIAL -> PARTIAL;
BLOCKED -> BLOCKED_EXTERNAL when the row says EXTERNAL, else BLOCKED_OPERATOR; ACCEPTED -> ACCEPTED). An unknown status stops the
import. A repeated task id keeps the LAST row. Idempotent: a second run gives a byte-identical plan. Atomic write, UTF-8, LF.
Stdlib only.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

STATUS_MAP = {
    "ACCEPTED": "ACCEPTED", "PARTIAL": "PARTIAL", "SOURCE_UNCOMMITTED": "PARTIAL", "SOURCE_PRESENT": "PARTIAL", "SOURCE_ONLY": "PARTIAL",
    "SUPERSEDED": "SUPERSEDED", "SUPERSEDED_OPERATOR_SCOPE": "SUPERSEDED", "HISTORICAL_ONLY": "SUPERSEDED",
    "BLOCKED": "BLOCKED", "NOT_STARTED": "NOT_STARTED", "TODO": "NOT_STARTED", "IN_PROGRESS": "IN_PROGRESS", "IN_REVIEW": "IN_REVIEW",
}
EVIDENCE_MAX = 300
HEX_RE = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{8,40}\b")
HEAD = "| Task | Title | Acceptance | Status | Evidence | Owner | Depends | Commit |"
SEP = "|---|---|---|---|---|---|---|---|"


def split_row(line: str) -> list[str]:
    return [c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def map_status(raw: str, row_text: str, task: str) -> str:
    m = re.match(r"[A-Za-z][A-Za-z_]*", raw.strip())
    word = m.group(0).upper() if m else ""
    if word not in STATUS_MAP:
        raise ValueError(f"task {task}: unknown ledger status {raw!r}; known: {', '.join(sorted(STATUS_MAP))}")
    status = STATUS_MAP[word]
    if status == "BLOCKED":
        status = "BLOCKED_EXTERNAL" if "EXTERNAL" in row_text else "BLOCKED_OPERATOR"
    return status


def cell(text: str, limit: int | None = None) -> str:
    text = re.sub(r"\s+", " ", text.replace("\\|", "|")).strip()
    if limit and len(text) > limit:
        text = text[: limit - 4].rstrip() + " ..."
    return text.replace("|", "\\|")


def parse_ledger(text: str) -> tuple[list[dict], int]:
    """Rows of every table whose header has a task column and a Status column (the 'next step' tables are skipped)."""
    rows: list[dict] = []
    idx: dict[str, int] | None = None
    total = 0
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = split_row(s)
        low = [c.lower() for c in cells]
        if not re.match(r"^\|\s*:?-{2,}", s) and not re.match(r"^ST-[\w.-]+", cells[0]):  # a header row; tables without Status end the scope
            idx = None
        if "status" in low and any("task" in c for c in low) and not re.match(r"^ST-[\w.-]+", cells[0]):
            idx = {"task": next(i for i, c in enumerate(low) if "task" in c), "status": low.index("status")}
            rest = [i for i in range(len(cells)) if i not in idx.values()]
            idx["title"] = rest[0] if rest else -1
            idx["basis"] = rest[1] if len(rest) > 1 else -1
            continue
        if re.match(r"^\|\s*:?-{2,}", s) or idx is None or not re.match(r"^ST-[\w.-]+", cells[0]):
            continue
        total += 1
        get = lambda k: cells[idx[k]] if 0 <= idx[k] < len(cells) else ""  # noqa: E731
        task = re.sub(r"\s+#", "#", get("task"))
        rows.append({"task": task, "status_raw": get("status"), "title": get("title"), "basis": get("basis"), "text": s})
    return rows, total


def build_rows(rows: list[dict]) -> tuple[list[dict], int]:
    last: dict[str, dict] = {}
    for r in rows:
        last.pop(r["task"], None)  # the LAST row of a repeated id wins and takes the last position
        last[r["task"]] = r
    out = []
    for r in last.values():
        status = map_status(r["status_raw"], r["text"], r["task"])
        commit = HEX_RE.search(r["basis"])
        out.append({"task": r["task"], "title": r["title"], "status": status, "evidence": r["basis"],
                    "commit": commit.group(0) if commit and status == "ACCEPTED" else ""})
    return out, len(rows) - len(last)


def render_section(sid: str, title: str, goal: str, tasks: list[dict]) -> list[str]:
    lines = [f"## Sprint {sid} - {title}"]
    if goal:
        lines.append(goal)
    lines += ["", HEAD, SEP]
    for t in tasks:
        lines.append(f"| {cell(t['task'])} | {cell(t['title'], 200)} | {cell(t['title'], 200)} | {t['status']} | "
                     f"{cell(t['evidence'], EVIDENCE_MAX)} |  |  | {t['commit']} |")
    return lines


def roadmap_row(lines: list[str], sid: str) -> dict:
    for ln in lines:
        c = split_row(ln) if ln.lstrip().startswith("|") else []
        if len(c) >= 4 and c[0] == sid:
            return {"title": c[1], "goal": c[3]}
    return {}


def apply(plan_text: str, sid: str, title: str | None, tasks: list[dict]) -> str:
    lines = plan_text.split("\n")
    head = re.compile(r"^##\s+Sprint\s+" + re.escape(sid) + r"(\s|$|[-:])", re.I)
    start = next((i for i, ln in enumerate(lines) if head.match(ln)), None)
    road = roadmap_row(lines, sid)
    if start is not None:
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        old = lines[start + 1:end]
        goal = next((ln for ln in old if ln.startswith("Goal:")), "")
        if not title:
            m = re.match(r"^##\s+Sprint\s+\S+\s*[-—–:]*\s*(.*)$", lines[start])
            title = (m.group(1).strip() if m else "") or road.get("title") or f"Sprint {sid}"
    else:
        goal = ""
        title = title or road.get("title") or f"Sprint {sid}"
    if not goal:
        goal = f"Goal: {len(tasks)} ledger tasks imported from the markdown task ledger (statuses mapped to the plan vocabulary)."
    section = render_section(sid, title, goal, tasks)
    if start is None:
        body = "\n".join(lines).rstrip("\n")
        return body + "\n\n" + "\n".join(section) + "\n"
    before = "\n".join(lines[:start]).rstrip("\n")
    after = "\n".join(lines[end:]).strip("\n")
    return before + "\n\n" + "\n".join(section) + "\n" + ("\n" + after + "\n" if after else "")


def atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
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
    a = ap.parse_args(argv)
    try:
        ledger_text = Path(a.ledger).read_text(encoding="utf-8-sig")
        plan_path = Path(a.plan)
        plan_text = plan_path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
        rows, total = parse_ledger(ledger_text)
        tasks, repeated = build_rows(rows)
        if not tasks:
            raise ValueError(f"no task rows found in {a.ledger}")
        new_text = apply(plan_text, a.sprint, a.title, tasks)
    except (OSError, ValueError) as e:
        print(f"plan_import_ledger_md: {e}", file=sys.stderr)
        return 1
    counts = Counter(t["status"] for t in tasks)
    print(f"ledger rows: {total}; repeated ids dropped (last row kept): {repeated}; tasks written: {len(tasks)}")
    print("status counts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if new_text == plan_text:
        print("plan unchanged (already up to date)")
    elif a.dry_run:
        print(f"dry-run: {plan_path} not modified")
    else:
        atomic_write(plan_path, new_text)
        print(f"wrote {plan_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
