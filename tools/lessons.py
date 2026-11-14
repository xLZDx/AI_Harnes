#!/usr/bin/env python3
"""Global lessons CLI (S20): thin wrapper over harness/hooks/_lessons.py.

  lessons.py add --kind K --project P --rule "..." [--tags a,b] [--actions x,y] [--repo R] [--sha S] [--date D]
  lessons.py search --action A [--words "..."] [--limit 5]
  lessons.py show L0001 [L0002 ...]
  lessons.py rebuild
  lessons.py restore
  lessons.py import --repo <path> [--repo <path> ...] [--all <root>] [--sha S]   (never reads Virtual_marketing_company or VMC_*)

Store: HARNESS_LESSONS_DIR or ~/.harness-hooks/lessons; backups: HARNESS_LESSONS_BACKUP (default D:\\Backup\\lessons).
The module is taken from harness/hooks next to this tool, or from the installed hooks folder (~/.claude/hooks, ~/.codex/hooks,
or HARNESS_HOOKS_DIR). Exit code 0 unless the arguments are wrong.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _load():
    here = Path(__file__).resolve().parent
    cands = [Path(os.environ["HARNESS_HOOKS_DIR"])] if os.environ.get("HARNESS_HOOKS_DIR") else []
    cands += [here.parent / "harness" / "hooks", Path.home() / ".claude" / "hooks", Path.home() / ".codex" / "hooks"]
    for c in cands:
        if (c / "_lessons.py").is_file():
            sys.path.insert(0, str(c))
            import _lessons  # noqa: E402
            return _lessons
    print("lessons: _lessons.py not found (looked in: " + ", ".join(str(c) for c in cands) + ")", file=sys.stderr)
    sys.exit(2)


L = _load()


def _csv(s: str) -> list:
    return [x.strip().lower() for x in (s or "").split(",") if x.strip()]


def cmd_add(a) -> int:
    tags = _csv(a.tags) or L.derive_tags(a.rule)
    actions = _csv(a.actions) or L.derive_actions(tags)
    res = L.add(a.rule, a.kind, a.project, tags=tags, actions=actions, date=a.date or "",
                source={"repo": a.repo or "", "sha": a.sha or ""})
    print(json.dumps(res) if res else "lessons: add failed (see errors.jsonl)")
    return 0


def cmd_search(a) -> int:
    for r in L.search(a.action, a.words or "", a.limit):
        print(f"{r['id']} | {','.join(r['actions'])} | {r['rule']}")
    return 0


def cmd_show(a) -> int:
    for o in L.show(a.ids):
        print(json.dumps(o, ensure_ascii=False, indent=2))
    return 0


def cmd_rebuild(_a) -> int:
    print("rebuilt" if L.rebuild() else "rebuild failed (see errors.jsonl)")
    return 0


def cmd_restore(_a) -> int:
    used = L.restore()
    print(f"restored from {used}" if used else "no backup found or restore failed")
    return 0


def import_repo(repo: Path, sha: str) -> dict:
    counts = {"added": 0, "merged": 0, "duplicate": 0, "skipped-no-rule": 0, "skipped-neutral": 0, "skipped-secret": 0,
              "queued": 0, "failed": 0}
    log = repo / "core" / "LESSONS_LOG.md"
    if not log.is_file():
        counts["failed"] = -1
        return counts
    parsed = L.parse_log(log.read_text(encoding="utf-8", errors="replace"))
    counts["skipped-no-rule"] = parsed["no_rule"]
    counts["skipped-neutral"] = parsed["neutral"]
    items = []
    for e in parsed["entries"]:
        tags = L.derive_tags(e["title"], e["rule"])
        items.append({"rule": e["rule"], "kind": e["kind"], "project": repo.name, "date": e["date"], "tags": tags,
                      "actions": L.derive_actions(tags), "source": {"repo": repo.name, "sha": sha},
                      "entry_text": e["text"]})
    results = L.add_many(items)
    if len(results) != len(items):
        counts["failed"] = len(items)
        return counts
    for r in results:
        st = r["status"]
        if st == "secret":
            counts["skipped-secret"] += 1
        elif st in counts:
            counts[st] += 1
        else:
            counts["failed"] += 1
    return counts


def cmd_import(a) -> int:
    repos = [Path(r) for r in (a.repo or [])]
    if a.all:
        root = Path(a.all)
        repos += sorted(p for p in root.iterdir() if p.is_dir() and (p / "core" / "LESSONS_LOG.md").is_file())
    seen, total = set(), {}
    for r in repos:
        r = r.resolve()
        if r in seen:
            continue
        seen.add(r)
        if L.EXCLUDED_PROJECT_RE.match(r.name):
            print(f"{r.name}: excluded")
            continue
        c = import_repo(r, a.sha)
        if c["failed"] == -1:
            print(f"{r.name}: no core/LESSONS_LOG.md")
            continue
        print(f"{r.name}: " + " ".join(f"{k}={v}" for k, v in c.items()))
        for k, v in c.items():
            total[k] = total.get(k, 0) + v
    if len(seen) > 1:
        print("TOTAL: " + " ".join(f"{k}={v}" for k, v in total.items()))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="lessons.py", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("add")
    s.add_argument("--kind", required=True, choices=L.KINDS)
    s.add_argument("--project", required=True)
    s.add_argument("--rule", required=True)
    s.add_argument("--tags", default="")
    s.add_argument("--actions", default="")
    s.add_argument("--repo", default="")
    s.add_argument("--sha", default="")
    s.add_argument("--date", default="")
    s.set_defaults(fn=cmd_add)
    s = sub.add_parser("search")
    s.add_argument("--action", required=True)
    s.add_argument("--words", default="")
    s.add_argument("--limit", type=int, default=5)
    s.set_defaults(fn=cmd_search)
    s = sub.add_parser("show")
    s.add_argument("ids", nargs="+")
    s.set_defaults(fn=cmd_show)
    sub.add_parser("rebuild").set_defaults(fn=cmd_rebuild)
    sub.add_parser("restore").set_defaults(fn=cmd_restore)
    s = sub.add_parser("import")
    s.add_argument("--repo", action="append")
    s.add_argument("--all", default="")
    s.add_argument("--sha", default="import")
    s.set_defaults(fn=cmd_import)
    a = p.parse_args(argv)
    if a.cmd == "import" and not (a.repo or a.all):
        p.error("import needs --repo or --all")
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
