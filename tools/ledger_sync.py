#!/usr/bin/env python3
"""Rebuild a project's .harness/ledger.json so it carries the WHOLE program from the project plan (recovery task R6).

Plan: <repo>/PROJECT_PLAN.md, else the path in .harness/profile.json key "program_plan" (may be outside the repo), else --plan FILE.
A plan kept as a Russian markdown master plan is converted in memory by tools/plan_import_master_md.py.
Program = merge_plan_and_ledger(plan, genuine ledger records):
  * a ledger task is GENUINE when it has commits, or a verify/review record written by `ledger.py` (those carry `task_commit`);
    imported copies of plan rows (no commits, evidence text only) are not genuine and are replaced by the plan row;
  * a sprint with at least one genuine task is kept whole: its non-genuine tasks that the plan also has are refreshed from the plan
    (in place), plan tasks of the same sprint that the ledger lacks are appended; ledger-only tasks with no plan row are kept;
  * a sprint with no genuine task keeps only its ledger-only tasks (no plan row); everything else comes from the plan;
  * ids are compared with spaces removed ('ST-DEL-01.01 #1' == 'ST-DEL-01.01#1'); a kept ledger task takes the plan's spelling of its id.
A non-genuine task that carries ledger-written evidence (`blocked`, `tokens`, real `routing`, state `in_progress`) keeps those fields
on its refreshed plan row. A write that would shrink the ledger is refused unless --allow-shrink.
Approval and events of the old ledger are kept. Write = unique backup to D:\\Backup\\ledger-sync-<project>-<YYYYMMDD-HHMMSS-us>-<pid>.json, then an
atomic replace under the ledger lock. Idempotent: no timestamps are written, a second run reports 'unchanged'.
Usage: ledger_sync.py --project DIR [--plan FILE] [--dry-run] | --all [--root D:\\Repo] [--dry-run]
Virtual_marketing_company is never touched. Stdlib only.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import datetime as dt
import json
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger  # noqa: E402
import plan_ledger  # noqa: E402
import plan_import_master_md  # noqa: E402
import plan_import_ledger_md  # noqa: E402
import report  # noqa: E402

SKIP = {"virtual_marketing_company"}
BACKUP_DIR = Path(r"D:\Backup")
COPY_MARKERS = ("canonical plan row", "reconciled from committed sprint ledger")


def norm(i) -> str:
    return re.sub(r"\s+", "", str(i or ""))


def main_root_of(d: Path) -> Path:
    try:
        common = Path(ledger.git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=str(d)))
    except ledger.GitError:
        return d
    return common.parent if common.name == ".git" else d


def find_plan(root: Path, d: Path, override: str | None) -> Path | None:
    if override:
        return Path(override)
    for base in (d, root):
        if (base / "PROJECT_PLAN.md").is_file():
            return base / "PROJECT_PLAN.md"
    for base in (d, root):
        prof = base / ".harness" / "profile.json"
        if prof.is_file():
            try:
                v = json.loads(prof.read_text(encoding="utf-8-sig")).get("program_plan")
            except (OSError, ValueError):
                v = None
            if v:
                p = Path(v)
                return p if p.is_absolute() else base / p
    return None


def load_plan(path: Path, project: str, root: Path | None = None) -> dict:
    text = path.read_text(encoding="utf-8-sig")
    if plan_import_master_md.is_master_md(text) and not re.search(r"^##\s+Sprint\s", text, re.M | re.I):
        text, _ = plan_import_master_md.convert(text, project)
    d = plan_ledger.parse(text)
    d["project"] = d["project"] or project
    if root is not None:
        expand_from_task_ledgers(d, root)
    return d


def expand_from_task_ledgers(plan: dict, root: Path) -> None:
    """A not-expanded plan sprint with docs/sdlc/<sid>-task-ledger.md gets its real tasks (parsed in memory with
    plan_import_ledger_md, nothing is written); UNVERIFIED placeholders fill up to the roadmap count when fewer rows were imported."""
    for i, sp in enumerate(plan["sprints"]):
        if sp.get("expanded") is not False:
            continue
        f = root / "docs" / "sdlc" / f"{sp['id'].lower()}-task-ledger.md"
        if not f.is_file():
            continue
        rows, _, _ = plan_import_ledger_md.parse_ledger(f.read_text(encoding="utf-8-sig"))
        tasks, _, _ = plan_import_ledger_md.build_rows(rows)
        if not tasks:
            continue
        sec = chr(10).join(plan_import_ledger_md.render_section(sp["id"], sp.get("title") or f"Sprint {sp['id']}", "", tasks)) + chr(10)
        real = plan_ledger.parse(sec)["sprints"][0]
        n_plan = len(sp["tasks"])
        used = {t["id"] for t in real["tasks"]}
        rm = sp.get("roadmap") or {}
        done_left = max(0, rm.get("done", 0) - sum(1 for t in real["tasks"] if t["state"] == "done"))
        impl_left = max(0, rm.get("impl", 0) - sum(1 for t in real["tasks"] if t["state"] == "implemented"))
        for j in range(len(real["tasks"]) + 1, n_plan + 1):
            st = "done" if done_left else "implemented" if impl_left else "todo"
            done_left, impl_left = max(0, done_left - (st == "done")), max(0, impl_left - (st == "implemented"))
            t = plan_ledger.placeholder_task(sp["id"], j, n_plan, st, f"not in {f.name}")
            if st == "todo":
                t["title"] = f"UNVERIFIED: planned placement {j} of {n_plan} not in {f.name}"
            while t["id"] in used:
                t["id"] += "x"
            used.add(t["id"])
            real["tasks"].append(t)
        kept = [t for t in real["tasks"] if t["state"] != "superseded"]
        real["state"] = "closed" if kept and all(t["state"] == "done" for t in kept) else "open"
        real["title"] = sp.get("title") or real["title"]
        plan["sprints"][i] = real


def from_plan(t: dict) -> dict:
    """A plan row copied into the ledger. `plan_commits` remembers the commits the plan itself named, so a later run does not
    mistake them for commits recorded by ledger.py."""
    t = copy.deepcopy(t)
    t["plan_commits"] = list(t.get("commits") or [])
    return t


def is_genuine(t: dict) -> bool:
    if set(t.get("commits") or []) - set(t.get("plan_commits") or []):
        return True
    for k in ("verify", "review"):
        r = t.get(k) or {}
        if r.get("task_commit"):
            return True
    return False


def looks_like_copy(t: dict) -> bool:
    txt = json.dumps([t.get("verify"), t.get("routing")], ensure_ascii=False).lower()
    return any(m in txt for m in COPY_MARKERS)


CARRIED = ("blocked", "tokens", "routing")  # fields only ledger.py writes (block / tokens / route)


def evidence_fields(t: dict, p: dict | None) -> dict:
    """Ledger-written evidence of a non-genuine task that the plan row cannot reproduce: `blocked`, `tokens`, a real `routing`
    (not an imported-copy note), and state `in_progress` (ledger.py start) where the plan row is still `todo`."""
    out = {k: copy.deepcopy(t[k]) for k in CARRIED if t.get(k) and not (k == "routing" and looks_like_copy({"routing": t[k]}))}
    if "blocked" in out and not (out["blocked"] or {}).get("at"):
        del out["blocked"]  # plan_ledger builds `blocked` (at: null) from BLOCKED_* / PARKED plan statuses; only ledger.py block sets `at`
    if t.get("state") == "in_progress" and (p is None or p.get("state") == "todo"):
        out["state"] = "in_progress"
    return out


def build(plan: dict, old: dict) -> tuple[dict, dict]:
    pidx: dict[str, tuple[str, dict]] = {}
    for sp in plan["sprints"]:
        if sp.get("expanded") is False:
            continue  # announced placeholders are not plan rows; merge_plan_and_ledger regenerates them
        for t in sp["tasks"]:
            pidx[norm(t["id"])] = (sp["id"], t)
    kept_sprints: list[dict] = []
    stats = {"genuine_sprints": [], "genuine_tasks": 0, "ledger_only": 0, "replaced": 0}
    for sp in old.get("sprints", []):
        tasks = sp.get("tasks", [])
        gen = [t for t in tasks if is_genuine(t)]
        genuine_sprint = bool(gen)
        out_tasks = []
        for t in tasks:
            p = pidx.get(norm(t["id"]))
            if not is_genuine(t) and "-PLACEHOLDER-" in str(t["id"]):
                continue
            if is_genuine(t):
                t2 = copy.deepcopy(t)
                if p:
                    t2["id"] = p[1]["id"]  # plan spelling, so the report's own plan merge does not count it twice
                out_tasks.append(t2)
                stats["genuine_tasks"] += 1
            elif p and evidence_fields(t, p[1]):
                row = from_plan(p[1])  # plan row, with the fields ledger.py wrote copied onto it; kept even in a non-genuine sprint
                row.update(evidence_fields(t, p[1]))
                out_tasks.append(row)
                stats["carried"] = stats.get("carried", 0) + 1
            elif p:
                stats["replaced"] += 1
                if genuine_sprint:
                    out_tasks.append(from_plan(p[1]))  # refreshed from the plan, same place
            elif looks_like_copy(t) and not evidence_fields(t, None):
                stats["replaced"] += 1  # an imported copy of a plan row the plan no longer spells the same way: the plan's count rules
            else:
                out_tasks.append(copy.deepcopy(t))  # ledger-only task (planned in the ledger, no plan row)
                stats["ledger_only"] += 1
        if out_tasks:
            sp2 = {k: v for k, v in sp.items() if k != "tasks"}
            sp2["tasks"] = out_tasks
            kept_sprints.append(sp2)
            if genuine_sprint:
                stats["genuine_sprints"].append(sp["id"])
    have = {norm(t["id"]) for sp in kept_sprints for t in sp["tasks"]}
    by_id = {sp["id"]: sp for sp in kept_sprints}
    plan2 = copy.deepcopy(plan)
    sprints2 = []
    for sp in plan2["sprints"]:
        if sp.get("expanded") is False:
            sprints2.append(sp)  # placeholders: merge_plan_and_ledger decides
            continue
        rest = [t for t in sp["tasks"] if norm(t["id"]) not in have]
        tgt = by_id.get(sp["id"])
        if tgt is not None:
            tgt["tasks"].extend(from_plan(t) for t in rest)
            if tgt.get("state") == "closed" and any(t["state"] != "done" for t in tgt["tasks"] if t["state"] != "superseded"):
                tgt["state"] = "open"
            rest = []
        if rest:
            sprints2.append({**sp, "tasks": [from_plan(t) for t in rest]})
    plan2["sprints"] = sprints2
    led = {**{k: v for k, v in old.items() if k != "sprints"}, "sprints": kept_sprints}
    led.setdefault("version", 1)
    led.setdefault("approval", None)
    led.setdefault("events", [])
    merged = report.merge_plan_and_ledger(plan2, led)
    merged.pop("source", None)
    for sp in merged["sprints"]:  # a sprint that gained planned (unverified) tasks is no longer closed
        if sp.get("state") == "closed" and any(t.get("state") not in ("done", "superseded") for t in sp["tasks"]):
            sp["state"] = "open"
    merged.setdefault("project", plan.get("project") or old.get("project"))
    return merged, stats


def counts(d: dict) -> dict:
    ts = [t for sp in d.get("sprints", []) for t in sp.get("tasks", [])]
    return {"tasks": len(ts), "accepted": sum(1 for t in ts if t.get("state") == "done"),
            "superseded": sum(1 for t in ts if t.get("state") == "superseded"),
            "placeholders": sum(1 for t in ts if "-PLACEHOLDER-" in str(t.get("id"))),
            "states": {sp["id"]: {st: n for st, n in sorted(Counter(t.get("state") for t in sp.get("tasks", [])).items())} for sp in d.get("sprints", [])},
            "sizes": [(sp["id"], len(sp.get("tasks", []))) for sp in d.get("sprints", [])]}


def make_backup(path: Path, project: str) -> Path:
    """Unique backup (microseconds + pid, exclusive create), checked readable and equal in size before the ledger is replaced."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    data = path.read_bytes()
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = BACKUP_DIR / f"ledger-sync-{project}-{stamp}-{os.getpid()}.json"
    with open(backup, "xb") as f:  # FileExistsError (an OSError) if the name is taken: never overwrite another backup
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    try:
        if backup.stat().st_size != len(data) or json.loads(backup.read_text(encoding="utf-8")) is None:
            raise RuntimeError(f"backup {backup} failed verification (size mismatch)")
    except (OSError, ValueError) as e:
        raise RuntimeError(f"backup {backup} is not readable: {e}")
    return backup


def write(path: Path, old_exists: bool, old: dict, new: dict, project: str) -> str | None:
    """Backup + atomic replace under the ledger lock. Returns the backup path."""
    backup = None
    prev = os.environ.get("HARNESS_LEDGER")
    os.environ["HARNESS_LEDGER"] = str(path)
    try:
        with ledger.locked() as cur:
            if not old_exists and path.exists():
                raise RuntimeError("the ledger was created while the sync ran; nothing written, run again")
            if old_exists and cur != old:
                raise RuntimeError("the ledger changed while the sync ran; nothing written, run again")
            if old_exists:
                backup = make_backup(path, project)
            cur.clear()
            cur.update(new)
    finally:
        if prev is None:
            os.environ.pop("HARNESS_LEDGER", None)
        else:
            os.environ["HARNESS_LEDGER"] = prev
    return str(backup) if backup else None


def sync_project(d: Path, dry: bool, plan_override: str | None = None, allow_shrink: bool = False) -> dict:
    d = d.resolve()
    root = main_root_of(d)
    name = root.name
    res: dict = {"project": name, "status": "", "plan": None}
    lpath = root / ".harness" / "ledger.json"
    old_exists = lpath.is_file()
    try:
        old = json.loads(lpath.read_text(encoding="utf-8")) if old_exists else {"version": 1, "project": name, "sprints": [], "approval": None, "events": []}
    except (OSError, ValueError) as e:
        res["status"] = f"ERROR ledger unreadable: {e}"
        return res
    res["old"] = counts(old)
    pf = find_plan(root, d, plan_override)
    if pf is None:
        res.update(status="no plan (ledger left as is)", new=res["old"], plan_tasks=0)
        return res
    res["plan"] = str(pf)
    try:
        plan = load_plan(pf, name, root)
    except (OSError, ValueError) as e:
        res["status"] = f"ERROR plan: {e}"
        return res
    res["plan_tasks"] = sum(len(sp["tasks"]) for sp in plan["sprints"])
    new, stats = build(plan, old)
    res["new"] = counts(new)
    res["stats"] = stats
    # invariant: nothing of the plan is lost and nothing is duplicated
    ids = [norm(t["id"]) for sp in new["sprints"] for t in sp["tasks"]]
    if len(ids) != len(set(ids)):
        res["status"] = "ERROR duplicate task ids after merge: " + ", ".join(sorted({i for i in ids if ids.count(i) > 1})[:5])
        return res
    if new == old:
        res["status"] = "unchanged"
        return res
    old_ids = {norm(t["id"]): t["id"] for sp in old.get("sprints", []) for t in sp.get("tasks", [])}
    dropped = [v for k, v in old_ids.items() if k not in set(ids)]
    if res["new"]["tasks"] < res["old"]["tasks"] and not allow_shrink:
        res["status"] = (f"ERROR new ledger is smaller ({res['new']['tasks']} < {res['old']['tasks']} tasks); nothing written; "
                         f"dropped ids ({len(dropped)}): {', '.join(dropped[:20])}{', ...' if len(dropped) > 20 else ''}; pass --allow-shrink to write")
        return res
    verb = "would change" if old_exists else "would create"
    if dry:
        res["status"] = verb + " (dry-run)"
        return res
    try:
        res["backup"] = write(lpath, old_exists, old, new, name)
    except (OSError, RuntimeError, SystemExit) as e:
        res["status"] = f"ERROR write: {e}"
        return res
    res["status"] = "changed" if old_exists else "created"
    return res


def discover(root: Path) -> list[Path]:
    out, seen = [], set()
    for p in sorted(root.iterdir()):
        if not p.is_dir() or p.name.lower() in SKIP:
            continue
        if (p / ".harness-tools").is_dir() or (p / ".harness" / "ledger.json").is_file():
            mr = main_root_of(p)
            if mr.name.lower() in SKIP or mr in seen:
                continue
            seen.add(mr)
            out.append(p)
    return out


def states_line(r: dict) -> str:
    return " ".join(f"{k}[" + ",".join(f"{s}={n}" for s, n in v.items()) + "]" for k, v in r["new"].get("states", {}).items())


def detail(r: dict) -> str:
    c = r.get("new") or {}
    return f"{r['project']}: " + " ".join(f"{k}:{n}" for k, n in c.get("sizes", [])) if c else r["project"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--project")
    g.add_argument("--all", action="store_true")
    ap.add_argument("--root", default=r"D:\Repo")
    ap.add_argument("--plan", help="plan file override (single project only), e.g. a converted master plan")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-shrink", action="store_true", help="write even when the new ledger has fewer tasks than the old one")
    ap.add_argument("--states", action="store_true", help="print per-sprint state counts for every project")
    a = ap.parse_args(argv)
    if a.project:
        if Path(a.project).resolve().name.lower() in SKIP or main_root_of(Path(a.project).resolve()).name.lower() in SKIP:
            print("ledger_sync: Virtual_marketing_company is never touched", file=sys.stderr)
            return 2
        projects = [Path(a.project)]
    else:
        if a.plan:
            ap.error("--plan works with --project only")
        projects = discover(Path(a.root))
    results = [sync_project(p, a.dry_run, a.plan, a.allow_shrink) for p in projects]
    hdr = ["project", "plan tasks", "old ledger", "new ledger", "accepted", "superseded", "placeholders", "genuine sprints", "status"]
    rows = []
    for r in results:
        o, n, s = r.get("old") or {}, r.get("new") or {}, r.get("stats") or {}
        rows.append([r["project"], str(r.get("plan_tasks", "-")), str(o.get("tasks", "-")), str(n.get("tasks", "-")), str(n.get("accepted", "-")),
                     str(n.get("superseded", "-")), str(n.get("placeholders", "-")), ",".join(s.get("genuine_sprints", [])) or "-",
                     r["status"] if len(r["status"]) <= 48 else r["status"][:45] + "..."])
    w = [max(len(hdr[i]), *(len(x[i]) for x in rows)) for i in range(len(hdr))]
    line = lambda cells: "| " + " | ".join(c.ljust(w[i]) for i, c in enumerate(cells)) + " |"
    print(line(hdr))
    print("|" + "|".join("-" * (x + 2) for x in w) + "|")
    for x in rows:
        print(line(x))
    print()
    for r in results:
        if r.get("new"):
            print("sprints  " + detail(r))
            if r["project"] in ("Personal_Decision_Command_Center", "ERP", "Fitness_App") or a.states:
                print("  states   " + states_line(r))
        if len(r["status"]) > 48:
            print(f"{r['project']}: {r['status']}")
        if r.get("plan"):
            print(f"  plan: {r['plan']}")
        if r.get("backup"):
            print(f"  backup: {r['backup']}")
        s = r.get("stats")
        if s:
            print(f"  genuine tasks kept {s['genuine_tasks']}, ledger-only kept {s['ledger_only']}, ledger copies replaced by the plan {s['replaced']}")
    return 1 if any(r["status"].startswith("ERROR") for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
