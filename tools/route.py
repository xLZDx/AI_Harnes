"""Deterministic reviewer router: changed files + diff text -> 0..4 reviewers with reasons.

No model calls. Global rules in harness/routing.json; a project adds or overrides them in
.harness/profile.json (same keys; "domain_specialists" holds the project's own agents and
"security_triggers" turns on automatic security-reviewer for named paths).
Usage:
  python .harness-tools/route.py --task S1-T2              # diff built from the task's commits
  python .harness-tools/route.py --diff file.diff [--risk risky] [--request security-reviewer]
  python .harness-tools/route.py --history 50              # dry run over the last 50 commits
The declared task risk can only raise the automatic risk, never lower it.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ledger  # noqa: E402

CORE = ("code-reviewer", "silent-failure-hunter")
ORDER = {r: i for i, r in enumerate(ledger.RISKS)}


class DiffError(ValueError):
    pass


def load_rules(project_root: Path) -> dict:
    src = HERE / "harness" / "routing.json"  # installed copy (.harness-tools/harness/)
    if not src.exists():
        src = HERE.parent / "harness" / "routing.json"  # harness source repository
    rules = json.loads(src.read_text(encoding="utf-8"))
    prof = project_root / ".harness" / "profile.json"
    if prof.exists():
        for k, v in json.loads(prof.read_text(encoding="utf-8")).items():
            rules[k] = {**rules[k], **v} if isinstance(v, dict) and isinstance(rules.get(k), dict) else v
    return rules


def known_agents() -> set[str] | None:
    for m in (HERE / "harness" / "manifest.json", HERE.parent / "harness" / "manifest.json"):
        if m.exists():
            return {a["name"] for a in json.loads(m.read_text(encoding="utf-8"))["agents"]}
    return None


def is_doc(rules: dict, f: str) -> bool:
    if any(re.search(p, f, re.I) for p in rules.get("doc_exclude_patterns", [])):
        return False
    return any(re.search(p, f, re.I) for p in rules["doc_only_patterns"])


def read_diff(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    if b"\x00" in raw[:4096]:
        raise DiffError(f"{path} looks binary or UTF-16 without BOM; write it with `git diff --output=<file>`")
    return raw.decode("utf-8", errors="replace")


def _unquote(p: str) -> str:
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1].encode("latin-1", "backslashescape").decode("unicode_escape").encode("latin-1").decode("utf-8", "replace")
    return p


def parse_diff(text: str) -> tuple[list[str], str]:
    """Files from the ---/+++ headers (handles spaces and quoted names); body = changed lines of hunks."""
    files, body, in_hunk = [], [], False
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            in_hunk = False
            continue
        if not in_hunk and line.startswith(("--- ", "+++ ")):
            p = _unquote(line[4:].rstrip("\r"))
            if p != "/dev/null":
                p = p[2:] if p[:2] in ("a/", "b/") else p
                if p not in files:
                    files.append(p)
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if in_hunk and line[:1] in ("+", "-"):
            body.append(line[1:])
    if text.strip() and not files:
        raise DiffError("diff text present but no file headers parsed")
    return files, "\n".join(body)


def first_match(patterns, items) -> str | None:
    for pat in patterns:
        for it in items:
            if re.search(pat, it, re.I):
                return f"{pat!r} in {it[:60]!r}"
    return None


def classify(rules, files, body) -> tuple[str, str]:
    if not files:
        return "trivial", "no files changed"
    code = [f for f in files if not is_doc(rules, f)]
    if not code:
        return "trivial", "documentation only"
    hit = first_match(rules["risky"]["paths"], code) or first_match(rules["risky"]["diff_keywords"], [body])
    if hit:
        return "risky", f"risky trigger {hit}"
    areas = {f.split("/")[0] if "/" in f else "" for f in code}
    if len(code) > 3 or len(areas) > 1:
        return "multi-module", f"{len(code)} code files in {len(areas)} areas"
    return "local", f"{len(code)} code file(s) in one area"


def route(rules, files, body, declared=None, requested=()) -> dict:
    auto, why = classify(rules, files, body)
    risk = max(auto, declared or auto, key=ORDER.get)
    reason = why if risk == auto else f"declared {declared} (auto: {auto}, {why})"
    picks: list[tuple[str, str]] = []
    if risk == "local":
        picks.append(("code-reviewer", "local change"))
    elif risk in ("multi-module", "risky"):
        picks += [(a, f"{risk} change") for a in CORE]
        for pat, agent in rules["language_reviewers"].items():
            if any(re.search(pat, f, re.I) for f in files):
                picks.append((agent, f"language: files match {pat}"))
                break
    if risk == "risky":
        cands = list(rules.get("domain_specialists", []))
        if rules.get("security_triggers"):
            cands.append({"agent": "security-reviewer", **rules["security_triggers"]})
        cands += rules["specialists"]
        for sp in cands:
            if len(picks) >= rules["max_reviewers"]:
                break
            hit = first_match(sp.get("paths", []), files) or first_match(sp.get("diff_keywords", []), [body])
            if hit and sp["agent"] not in [p[0] for p in picks]:
                picks.append((sp["agent"], f"trigger {hit}"))
                break  # one specialist per task
    picks = picks[: rules["max_reviewers"]]
    known = known_agents()
    for agent in requested:  # operator requests are never cut by the cap
        if known is not None and agent not in known and agent not in [d["agent"] for d in rules.get("domain_specialists", [])]:
            raise SystemExit(f"route: unknown agent {agent!r}")
        if agent not in [p[0] for p in picks]:
            picks.append((agent, "operator request"))
    return {"risk": risk, "risk_reason": reason, "files": files, "reviewers": [{"agent": a, "reason": r} for a, r in picks]}


def task_diff(t: dict) -> str:
    if not t["commits"]:
        raise DiffError(f"{t['id']} has no commits; nothing to review")
    return "\n".join(ledger.git("show", "--format=", "--no-color", sha) for sha in t["commits"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--diff")
    ap.add_argument("--task")
    ap.add_argument("--risk", choices=ledger.RISKS)
    ap.add_argument("--request", action="append", default=[], help="agent the operator asked for")
    ap.add_argument("--history", type=int, help="dry run over the last N commits")
    a = ap.parse_args(argv)
    rules = load_rules(ledger.main_root())
    try:
        if a.history:
            tally: dict[str, int] = {}
            for sha in ledger.git("log", f"-{a.history}", "--format=%h").split():
                files, body = parse_diff(ledger.git("show", "--format=", "--no-color", sha))
                r = route(rules, files, body)
                names = [x["agent"] for x in r["reviewers"]]
                for n in names:
                    tally[n] = tally.get(n, 0) + 1
                print(f"{sha} {r['risk']:12} {', '.join(names) or '-'}")
            print("totals:", json.dumps(dict(sorted(tally.items(), key=lambda x: -x[1]))))
            return 0
        if not (a.diff or a.task):
            ap.error("--task, --diff or --history is required")
        declared, t = a.risk, None
        if a.task:
            _, t = ledger.find_task(ledger.load(), a.task)
            declared = declared or t.get("risk")
        text = read_diff(Path(a.diff)) if a.diff else task_diff(t)
        if not text.strip():
            raise DiffError("empty diff; nothing to review")
        files, body = parse_diff(text)
    except (DiffError, ledger.GitError) as e:
        print(f"route: {e}", file=sys.stderr)
        return 2
    r = route(rules, files, body, declared, a.request)
    if a.task:
        with ledger.locked() as d:
            _, tt = ledger.find_task(d, a.task)
            tt["routing"] = r
            ledger.event(d, "route", task=a.task, reviewers=[x["agent"] for x in r["reviewers"]])
    print(json.dumps(r, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
