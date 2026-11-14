#!/usr/bin/env python3
"""Global lessons CLI (S20): thin wrapper over harness/hooks/_lessons.py.

  lessons.py add --kind K --project P --rule "..." [--tags a,b] [--actions x,y] [--repo R] [--sha S] [--date D]
  lessons.py search --action A [--words "..."] [--limit 5]
  lessons.py show L0001 [L0002 ...]
  lessons.py rebuild
  lessons.py restore [--list] [--from <backup file>]   (default: the newest backup whose lines all parse)
  lessons.py import --repo <path> [--repo <path> ...] [--all <root>] [--sha S]   (never reads Virtual_marketing_company or VMC_*)
  lessons.py import --repo <path> --commit <sha>   (post-commit writer: only entries that commit added or changed in the log)

Store: HARNESS_LESSONS_DIR or ~/.harness-hooks/lessons; backups: HARNESS_LESSONS_BACKUP (default <store>/backup when the
store was moved by HARNESS_LESSONS_DIR or HARNESS_HOOK_STATE, else D:\\Backup\\lessons).
`add` exits 1 when the add failed, 3 when it was refused (secret, excluded, invalid).
The module is taken from harness/hooks next to this tool, or from the installed hooks folder (~/.claude/hooks, ~/.codex/hooks,
or HARNESS_HOOKS_DIR). Exit code 0 unless the arguments are wrong, an import failed (1), an entry was queued because the
store lock timed out (2), or a restore failed (1). A backup that failed or was skipped is printed, never fatal.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def _load():
    here = Path(__file__).resolve().parent
    cands = [Path(os.environ["HARNESS_HOOKS_DIR"])] if os.environ.get("HARNESS_HOOKS_DIR") else []
    cands += [here, here.parent / "harness" / "hooks", Path.home() / ".claude" / "hooks", Path.home() / ".codex" / "hooks"]
    for c in cands:
        if (c / "_lessons.py").is_file():
            sys.path.insert(0, str(c))
            import _lessons  # noqa: E402
            return _lessons
    print("lessons: _lessons.py not found (looked in: " + ", ".join(str(c) for c in cands) + ")", file=sys.stderr)
    sys.exit(2)


L = _load()

COUNT_KEYS = ("added", "merged", "duplicate", "updated", "skipped-no-rule", "skipped-neutral", "skipped-secret", "queued",
              "unparsed", "failed")


def _csv(s: str) -> list:
    return [x.strip().lower() for x in (s or "").split(",") if x.strip()]


def _backup_note() -> None:
    bk = L.last_backup()
    if bk["status"] in ("failed", "skipped"):
        print(f"lessons: backup {bk['status']}: {bk['detail']}")


def cmd_add(a) -> int:
    tags = _csv(a.tags) or L.derive_tags(a.rule)
    actions = _csv(a.actions) or L.derive_actions(tags)
    res = L.add(a.rule, a.kind, a.project, tags=tags, actions=actions, date=a.date or "",
                source={"repo": a.repo or "", "sha": a.sha or ""})
    print(json.dumps(res) if res else "lessons: add failed (see errors.jsonl)")
    _backup_note()
    st = res.get("status") if res else None
    if not res or st == "failed":
        return 1
    if st in ("secret", "excluded", "invalid"):
        return 3
    return 2 if st == "queued" else 0


def cmd_search(a) -> int:
    for r in L.search(a.action, a.words or "", a.limit, refresh=True):
        print(f"{r['id']} | {','.join(r['actions'])} | {r['rule']}")
    return 0


def cmd_show(a) -> int:
    for o in L.show(a.ids):
        print(json.dumps(o, ensure_ascii=False, indent=2))
    return 0


def cmd_rebuild(_a) -> int:
    rep = L.rebuild_report()
    print("rebuilt" if rep["ok"] else "rebuild failed (see errors.jsonl)")
    bk = rep["backup"]
    if bk["status"] in ("failed", "skipped"):
        print(f"lessons: backup {bk['status']}: {bk['detail']}")
    return 0


def cmd_restore(a) -> int:
    if a.list:
        for b in L.list_backups():
            print(f"{b['name']} | records={b['valid']} | unparsable={b['bad']}")
        return 0
    rep = L.restore_report(a.from_file or None)
    if rep["ok"]:
        print(f"restored from {rep['path']}")
        _backup_note()
        return 0
    if rep["reason"] == "no backup":
        print("no backup found")
        return 0
    print(f"restore failed: {rep['reason']}")
    return 1


def _git(repo: Path, *args):
    """(returncode, stdout); returncode -1 when git itself could not run."""
    try:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError:
        return -1, ""
    return r.returncode, r.stdout


def _git_out(repo: Path, *args) -> str:
    rc, out = _git(repo, *args)
    return out if rc == 0 else ""


def _git_show(repo: Path, rev: str, path: str):
    """(state, text): state is "ok", "absent" (the path does not exist in that commit) or "failed" (git failed)."""
    rc, out = _git(repo, "ls-tree", "--name-only", rev, "--", path)
    if rc != 0:
        return "failed", ""
    if not out.strip():
        return "absent", ""
    rc, out = _git(repo, "show", f"{rev}:{path}")
    return ("ok", out) if rc == 0 else ("failed", "")


def project_name(repo: Path) -> str:
    """Folder name of the main checkout (a worktree resolves through the git common dir)."""
    common = _git_out(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    if common:
        c = Path(common)
        return (c.parent if c.name == ".git" else c).name
    return repo.name


def _tally(counts: dict, results: list) -> None:
    for r in results:
        st = r["status"]
        k = "skipped-secret" if st == "secret" else st
        counts[k if k in counts else "failed"] += 1


def _items(parsed: dict, project: str, sha: str, only_new_vs=None) -> list:
    items = []
    for e in parsed["entries"]:
        if only_new_vs is not None and e["text"] in only_new_vs:
            continue
        tags = L.derive_tags(e["title"], e["rule"])
        items.append({"rule": e["rule"], "kind": e["kind"], "project": project, "date": e["date"], "tags": tags,
                      "actions": L.derive_actions(tags), "source": {"repo": project, "sha": sha}, "entry_text": e["text"],
                      "title": e["title"], "ordinal": e["ordinal"], "block": e["block"]})
    return items


def import_repo(repo: Path, sha: str, project: str = "") -> dict:
    counts = dict.fromkeys(COUNT_KEYS, 0)
    log = repo / "core" / "LESSONS_LOG.md"
    if not log.is_file():
        counts["failed"] = -1
        return counts
    project = project or project_name(repo)
    try:
        text = log.read_bytes().decode("utf-8-sig", errors="replace")
    except OSError:
        counts["failed"] = 1
        return counts
    parsed = L.parse_log(text)
    counts["skipped-no-rule"] = parsed["no_rule"]
    counts["skipped-neutral"] = parsed["neutral"]
    counts["unparsed"] = parsed["unparsed"]
    items = _items(parsed, project, sha)
    results = L.add_many(items)
    if len(results) != len(items):
        counts["failed"] = len(items)
        return counts
    _tally(counts, results)
    return counts


def import_commit(repo: Path, sha: str, project: str = "") -> dict:
    """Import only the entries that `sha` added or changed in core/LESSONS_LOG.md (compared with its first parent).
    `parsed` is the number of entries found in the new log, so a hook can see a changed log that parsed to nothing."""
    counts = dict.fromkeys(COUNT_KEYS, 0)
    counts["parsed"] = 0
    full = _git_out(repo, "rev-parse", sha).strip()
    if not full:
        counts["failed"] = 1
        return counts
    project = project or project_name(repo)
    if L.EXCLUDED_PROJECT_RE.match(project):
        print(f"{project}: excluded")
        return counts
    st_new, text_new = _git_show(repo, full, "core/LESSONS_LOG.md")
    rc, parents = _git(repo, "rev-list", "--parents", "-n", "1", full)
    if st_new == "failed" or rc != 0:
        counts["failed"] = 1
        return counts
    if len(parents.split()) > 1:
        st_old, text_old = _git_show(repo, full + "^", "core/LESSONS_LOG.md")
        if st_old == "failed":
            counts["failed"] = 1
            return counts
    else:  # a root commit has nothing before it
        text_old = ""
    new = L.parse_log(text_new)
    old = L.parse_log(text_old)
    before = {e["text"] for e in old["entries"]}
    counts["parsed"] = new["total"]
    counts["skipped-neutral"] = max(0, new["neutral"] - old["neutral"])
    counts["skipped-no-rule"] = len(set(new["no_rule_texts"]) - set(old["no_rule_texts"]))
    counts["unparsed"] = max(0, new["unparsed"] - old["unparsed"])
    items = _items(new, project, full, only_new_vs=before)
    results = L.add_many(items) if items else []
    if len(results) != len(items):
        counts["failed"] = len(items)
        return counts
    _tally(counts, results)
    return counts


def cmd_import(a) -> int:
    if a.commit:
        if not a.repo or len(a.repo) != 1:
            print("import --commit needs exactly one --repo")
            return 2
        c = import_commit(Path(a.repo[0]).resolve(), a.commit)
        print("commit import: " + " ".join(f"{k}={v}" for k, v in c.items()))
        _backup_note()
        return 1 if c.get("failed") else (2 if c.get("queued") else 0)
    repos = [Path(r) for r in (a.repo or [])]
    if a.all:
        root = Path(a.all)
        if not root.is_dir():
            print(f"lessons: --all root not found: {root}", file=sys.stderr)
            return 2
        repos += sorted(p for p in root.iterdir() if p.is_dir() and (p / "core" / "LESSONS_LOG.md").is_file())
    seen, total, rc = set(), {}, 0
    for r in repos:
        r = r.resolve()
        if r in seen:
            continue
        seen.add(r)
        name = project_name(r)
        if L.EXCLUDED_PROJECT_RE.match(name) or L.EXCLUDED_PROJECT_RE.match(r.name):
            print(f"{r.name}: excluded")
            continue
        c = import_repo(r, a.sha, name)
        if c["failed"] == -1:
            print(f"{name}: no core/LESSONS_LOG.md")
            continue
        print(f"{name}: " + " ".join(f"{k}={v}" for k, v in c.items()))
        if c["failed"]:
            rc = 1
        elif c["queued"] and rc == 0:
            rc = 2
        for k, v in c.items():
            total[k] = total.get(k, 0) + v
    if len(seen) > 1:
        print("TOTAL: " + " ".join(f"{k}={v}" for k, v in total.items()))
    _backup_note()
    return rc


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
    s = sub.add_parser("restore")
    s.add_argument("--list", action="store_true", help="list backups with their record counts")
    s.add_argument("--from", dest="from_file", default="", help="backup file (path or name in the backup folder)")
    s.set_defaults(fn=cmd_restore)
    s = sub.add_parser("import")
    s.add_argument("--repo", action="append")
    s.add_argument("--all", default="")
    s.add_argument("--sha", default="import")
    s.add_argument("--commit", default="")
    s.set_defaults(fn=cmd_import)
    a = p.parse_args(argv)
    if a.cmd == "import" and not (a.repo or a.all):
        p.error("import needs --repo or --all")
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
