#!/usr/bin/env python3
"""pre-push helper (recovery plan R1): refuse a push whose tip commit carries a GitHub workflow that CI would start
automatically. Reads git's pre-push lines on stdin (`<local ref> <local sha> <remote ref> <remote sha>`), looks at the tree
of each pushed tip commit (`git ls-tree`, `git show <sha>:<path>`), and exits 1 when a file under .github/workflows has an
`on:` trigger containing push, pull_request, pull_request_target or schedule. Stdlib only (no YAML library)."""
from __future__ import annotations

import re
import subprocess
import sys

BAD = ("push", "pull_request", "pull_request_target", "schedule")
ON_RE = re.compile(r"""^(?:on|"on"|'on')\s*:(.*)$""")


def git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")


def triggers(text: str) -> set[str]:
    """Event names in the top-level `on:` key of a workflow file (inline, list, mapping and block forms)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = ON_RE.match(line)
        if not m:
            continue
        rest = re.sub(r"\s+#.*$", "", m.group(1)).strip()
        if rest:
            return {t for t in re.split(r"[\s,\[\]{}:]+", rest) if t}
        events: set[str] = set()
        indent = None
        for nxt in lines[i + 1:]:
            if not nxt.strip() or nxt.lstrip().startswith("#"):
                continue
            ind = len(nxt) - len(nxt.lstrip())
            if ind == 0:
                break
            if indent is None:
                indent = ind
            if ind != indent:
                continue
            body = nxt.strip()
            body = body[1:].strip() if body.startswith("-") else body
            name = re.split(r"[\s:]", body, 1)[0].strip("\"'")
            if name:
                events.add(name)
        return events
    return set()


def main() -> int:
    refs = [l.split() for l in sys.stdin.read().splitlines() if l.strip()]
    shas = []
    for l in refs:
        if len(l) == 4 and set(l[1]) != {"0"} and l[1] not in shas:
            shas.append(l[1])
    bad: list[tuple[str, str, list[str]]] = []
    for sha in shas:
        r = git("ls-tree", "-r", "--name-only", sha, "--", ".github/workflows")
        if r.returncode != 0:
            continue
        for path in r.stdout.splitlines():
            if not path.lower().endswith((".yml", ".yaml")):
                continue
            s = git("show", f"{sha}:{path}")
            if s.returncode != 0:
                continue
            hit = sorted(triggers(s.stdout) & set(BAD))
            if hit:
                bad.append((sha, path, hit))
    if not bad:
        return 0
    for sha, path, hit in bad:
        print(f"push blocked: {path} (in {sha[:12]}) starts CI on {', '.join(hit)}. CI is prohibited: switch the trigger to "
              "`workflow_dispatch` only, commit, and push again.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
