#!/usr/bin/env python3
"""Russian markdown master plan -> PROJECT_PLAN.md format (the one tools/plan_ledger.py reads).

Built for D:\\Repo\\reports\\FITNESS_MASTER_PLAN_2026-10-29.ru.md. Input shape: sprint sections `### Спринт <id> — <title>` that hold a table
`| № | Статус | Задача | Источник | DoD |`; one row = one task. Status words (prefix of the Статус cell):
  ЗАКРЫТО -> ACCEPTED, ГОТОВО -> PARTIAL (code ready, acceptance pending), В РАБОТЕ -> IN_PROGRESS,
  ОТЛОЖЕНО -> BLOCKED_EXTERNAL (deferred, waiting for a gate or condition), ОТКРЫТО -> NOT_STARTED.
Task ids are copied from the plan, never invented. The operator-approved counting rule (the plan's own report builder
reports/build_fitness_program_report.py) takes ids `<letters+digits>.<digits>` with an optional suffix `a` only, so ids with another
suffix letter (M10.37b, M10.43c, M10.43d) are NOT written unless --include-suffixed is given; they are listed as unmapped rows.
Sprint titles and task text stay in the source language (no machine translation).
Usage: plan_import_master_md.py <master.md> --out <PROJECT_PLAN.md> [--project NAME] [--include-suffixed]
Stdlib only. Writes only the --out file.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

STATUS = [("ЗАКРЫТО", "ACCEPTED"), ("ГОТОВО", "PARTIAL"), ("В РАБОТЕ", "IN_PROGRESS"), ("ОТЛОЖЕНО", "BLOCKED_EXTERNAL"), ("ОТКРЫТО", "NOT_STARTED")]
ID_RE = re.compile(r"^([A-Z]+[0-9]*)\.([0-9]+)([a-z]*)$")
SPRINT_RE = re.compile(r"^###\s+Спринт\s+(\S+)\s*[—–-]\s*(.*)$")
MERGED_SHA_RE = re.compile(r"(?:влит\w*|слит\w*)\s+(?:в\s+master\s+)?`?([0-9a-f]{7,40})\b")


def is_master_md(text: str) -> bool:
    return bool(re.search(r"^###\s+Спринт\s+\S+\s*[—–-]", text, re.M))


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("|", "/")).strip()


def convert(text: str, project: str, include_suffixed: bool = False) -> tuple[str, dict]:
    """Return (PROJECT_PLAN.md text, stats). Stats: sprints, rows, per-status counts, unmapped [(line, reason, text)]."""
    lines = text.replace("\r\n", "\n").split("\n")
    sprints: list[dict] = []
    cur: dict | None = None
    unmapped: list[tuple[int, str, str]] = []
    seen: dict[str, int] = {}
    for n, ln in enumerate(lines, 1):
        s = SPRINT_RE.match(ln)
        if s:
            cur = {"id": s.group(1), "title": _clean(s.group(2)), "tasks": []}
            sprints.append(cur)
            continue
        if ln.startswith("## "):
            cur = None  # a later top-level section (Phase 2, registers) is not a sprint of this plan
            continue
        if cur is None or not ln.startswith("|"):
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split(" | ")]
        first = cells[0].strip("| ")
        if first in ("№", "") or re.fullmatch(r"[-: |]+", first):
            continue
        m = ID_RE.match(first)
        if not m:
            unmapped.append((n, "first cell is not a task id", ln[:140]))
            continue
        if len(cells) < 3:
            unmapped.append((n, "fewer than 3 cells", ln[:140]))
            continue
        status = next((en for ru, en in STATUS if cells[1].startswith(ru)), None)
        if status is None:
            unmapped.append((n, f"unknown status {cells[1][:40]!r}", ln[:140]))
            continue
        if m.group(3) not in ("", "a") and not include_suffixed:
            unmapped.append((n, f"id suffix {m.group(3)!r} is outside the approved counting rule (only 'a')", f"{first} {status} {cells[2][:100]}"))
            continue
        if first in seen:
            unmapped.append((n, f"duplicate id (first seen on line {seen[first]})", ln[:140]))
            continue
        seen[first] = n
        desc = _clean(cells[2])
        title = desc if len(desc) <= 160 else desc[:157].rstrip() + "..."
        dod = _clean(" — ".join(c for c in cells[4:] if c)) if len(cells) > 4 else ""
        sha = MERGED_SHA_RE.search(ln)
        cur["tasks"].append({"id": first, "title": title, "status": status, "acceptance": dod, "commit": sha.group(1) if sha else "",
                             "owner": "operator" if status == "BLOCKED_EXTERNAL" and "оператор" in ln.lower() else ""})
    out = ["---", f"project: {project}", "source: converted from the Russian master plan by tools/plan_import_master_md.py", "---", "",
           f"# {project} - project plan", ""]
    counts: dict[str, int] = {}
    total = 0
    for sp in sprints:
        if not sp["tasks"]:
            continue
        out += [f"## Sprint {sp['id']} - {sp['title']}", "", "| Task | Title | Status | Acceptance | Commit | Owner |", "|---|---|---|---|---|---|"]
        for t in sp["tasks"]:
            out.append(f"| {t['id']} | {t['title']} | {t['status']} | {t['acceptance']} | {t['commit']} | {t['owner']} |")
            counts[t["status"]] = counts.get(t["status"], 0) + 1
            total += 1
        out.append("")
    stats = {"sprints": len([s for s in sprints if s["tasks"]]), "rows": total, "by_status": counts, "unmapped": unmapped,
             "sprint_sizes": {s["id"]: len(s["tasks"]) for s in sprints if s["tasks"]}}
    return "\n".join(out) + "\n", stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("src")
    ap.add_argument("--out", required=True)
    ap.add_argument("--project", default="Fitness_App")
    ap.add_argument("--include-suffixed", action="store_true", help="also write ids with suffix letters other than 'a' (outside the approved count)")
    a = ap.parse_args(argv)
    text = Path(a.src).read_text(encoding="utf-8-sig")
    if not is_master_md(text):
        print(f"import: {a.src} has no '### Спринт <id> — <title>' sections", file=sys.stderr)
        return 1
    plan, st = convert(text, a.project, a.include_suffixed)
    if not st["rows"]:
        print("import: 0 task rows mapped; nothing written", file=sys.stderr)
        return 1
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(plan, encoding="utf-8", newline="\n")
    b = st["by_status"]
    print(f"import: wrote {out}")
    print(f"import: sprints {st['sprints']}, plan task rows {st['rows']}")
    print(f"import: ACCEPTED (closed) {b.get('ACCEPTED', 0)}, PARTIAL (code ready, acceptance pending) {b.get('PARTIAL', 0)}, "
          f"IN_PROGRESS {b.get('IN_PROGRESS', 0)}, BLOCKED_EXTERNAL (deferred) {b.get('BLOCKED_EXTERNAL', 0)}, NOT_STARTED (open) {b.get('NOT_STARTED', 0)}")
    print("import: sprint sizes " + ", ".join(f"{k}:{v}" for k, v in st["sprint_sizes"].items()))
    print(f"import: unmapped rows {len(st['unmapped'])}")
    for n, why, t in st["unmapped"]:
        print(f"  line {n}: {why}: {t}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
