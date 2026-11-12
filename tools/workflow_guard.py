#!/usr/bin/env python3
"""pre-push helper (recovery plan R1): refuse a push whose tip commit carries a GitHub workflow that CI would start
automatically. Reads git's pre-push lines on stdin (`<local ref> <local sha> <remote ref> <remote sha>`), looks at the tree
of each pushed tip commit (`git ls-tree`, `git show <sha>:<path>`), and exits 1 when a file under .github/workflows has an
`on:` trigger containing push, pull_request, pull_request_target or schedule. Stdlib only (no YAML library)."""
from __future__ import annotations

import re
import subprocess
import sys

UNPARSABLE = "<unparsable on: value>"
BAD = ("push", "pull_request", "pull_request_target", "schedule", UNPARSABLE)
ON_RE = re.compile(r"""^(?:on|"on"|'on')\s*:(.*)$""")


NAME_RE = re.compile(r"^[A-Za-z0-9_.\-/]+$")


def _unquote(tok: str) -> str:
    """Strip one layer of matching single or double quotes (and stray quote characters at the edges)."""
    tok = tok.strip()
    while len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
        tok = tok[1:-1].strip()
    return tok.strip("\"'") if tok[:1] in "\"'" and tok[-1:] in "\"'" else tok


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
        if rest[:1] in ("[", "{"):  # flow style may span lines: accumulate until the brackets balance
            acc, k = rest, i
            while acc.count("[") + acc.count("{") > acc.count("]") + acc.count("}"):
                k += 1
                if k >= len(lines):
                    return {UNPARSABLE}
                acc += " " + re.sub(r"(^|\s)#.*$", "", lines[k]).strip()
            rest = acc
        if rest:
            names = {_unquote(t) for t in re.split(r"[\s,\[\]{}:]+", rest) if t}
            names.discard("")
            if not names or not all(NAME_RE.match(n) for n in names):
                return {UNPARSABLE}  # alias, tag, block scalar or unbalanced quote: cannot be read safely
            return names
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
            name = _unquote(re.split(r"[\s:]", body, maxsplit=1)[0])
            if name:
                events.add(name if NAME_RE.match(name) else UNPARSABLE)
        return events or {UNPARSABLE}  # an `on:` with no readable value counts as a hit
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
            print(f"push blocked: git ls-tree failed for {sha[:12]} ({r.stderr.strip()[:200]}); cannot check .github/workflows.",
                  file=sys.stderr)
            return 1
        for path in r.stdout.splitlines():
            if not path.lower().endswith((".yml", ".yaml")):
                continue
            s = git("show", f"{sha}:{path}")
            if s.returncode != 0:
                print(f"push blocked: git show {sha[:12]}:{path} failed ({s.stderr.strip()[:200]}); cannot check the workflow.",
                      file=sys.stderr)
                return 1
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
