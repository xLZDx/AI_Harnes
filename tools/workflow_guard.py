#!/usr/bin/env python3
"""pre-push helper (recovery plan R1): refuse a push whose tip commit carries a GitHub workflow that CI would start
automatically. Reads git's pre-push lines on stdin (`<local ref> <local sha> <remote ref> <remote sha>`), looks at the tree
of each pushed tip commit (`git ls-tree -z`, `git show <sha>:<path>`), and exits 1 when a file under .github/workflows has an
`on:` trigger containing push, pull_request, pull_request_target or schedule, or has no readable top-level `on:` key (fail
closed). A git call that exceeds 60 s also blocks the push. Stdlib only (no YAML library)."""
from __future__ import annotations

import re
import subprocess
import sys

UNPARSABLE = "<unparsable on: value>"
BAD = ("push", "pull_request", "pull_request_target", "schedule", UNPARSABLE)
ON_RE = re.compile(r"""^(?:on|"on"|'on')\s*:(.*)$""", re.IGNORECASE)
# a top-level line the guard can read: a plain or simply quoted key (no escapes) followed by `:` and a space or the end
PLAIN_KEY_RE = re.compile(r"""^(?:[A-Za-z_][A-Za-z0-9_.\-]*|"[^"\\]*"|'[^']*')\s*:(?:\s|$)""")


NAME_RE = re.compile(r"^(?=.*[A-Za-z0-9])[A-Za-z0-9_.\-/]+$")  # an event name has at least one letter or digit


def _unquote(tok: str) -> str:
    """Strip one layer of matching single or double quotes (and stray quote characters at the edges)."""
    tok = tok.strip()
    while len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
        tok = tok[1:-1].strip()
    return tok.strip("\"'") if tok[:1] in "\"'" and tok[-1:] in "\"'" else tok


def git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)


def triggers(text: str) -> set[str]:
    """Event names in the top-level `on:` key of a workflow file (inline, list, mapping and block forms).

    No top-level `on:` key, more than one (duplicate keys, any case), a top-level line that is not a plain `key:` line
    (escaped, tagged, anchored or explicit key, flow-style top level, a second YAML document), or a value that cannot be
    read safely gives {UNPARSABLE}, which callers count as a hit (fail closed). Event names are lower-cased."""
    lines = text.lstrip("\ufeff").splitlines()
    def lead(l: str) -> bool:  # a document start marker or directive before any content
        return l.rstrip() == "---" or l.startswith("%")
    base = next((len(l) - len(l.lstrip(" ")) for l in lines
                 if l.strip() and not l.lstrip().startswith("#") and not lead(l)), 0)
    # Every top-level line must be a plain `key:` line. An escaped, tagged, anchored or explicit (`? `) key, a flow-style
    # top level, a second YAML document, or a line left of the base indent could hide another `on` key: fail closed.
    content = False
    for l in lines:
        if not l.strip() or l.lstrip().startswith("#") or (not content and lead(l)):
            continue
        ind = len(l) - len(l.lstrip(" "))
        if ind < base or (ind == base and not PLAIN_KEY_RE.match(l[base:])):
            return {UNPARSABLE}
        content = True
    keys = [(i, m) for i, line in enumerate(lines)
            if len(line) - len(line.lstrip(" ")) == base and (m := ON_RE.match(line[base:]))]
    if len(keys) > 1:
        # duplicate top-level `on:` keys (or a second YAML document): which one a parser keeps is not ours to guess
        return {UNPARSABLE}
    for i, m in keys:
        raw = m.group(1)
        if "#" in raw and ("'" in raw or '"' in raw):
            return {UNPARSABLE}  # the comment strip is not quote-aware: a quoted ' #' could cut real events off
        rest = re.sub(r"\s+#.*$", "", raw).strip()
        if rest[:1] in ("[", "{"):  # flow style may span lines: accumulate until the brackets balance
            acc, k = rest, i
            while acc.count("[") + acc.count("{") > acc.count("]") + acc.count("}"):
                k += 1
                if k >= len(lines):
                    return {UNPARSABLE}
                nxt = lines[k]
                if nxt.strip() and len(nxt) - len(nxt.lstrip(" ")) <= base:
                    return {UNPARSABLE}  # the flow value would swallow the next top-level key
                if "#" in nxt and ("'" in nxt or '"' in nxt):
                    return {UNPARSABLE}
                acc += " " + re.sub(r"(^|\s)#.*$", "", nxt).strip()
            rest = acc
        if rest:
            names = {_unquote(t).lower() for t in re.split(r"[\s,\[\]{}:]+", rest) if t}
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
            if ind <= base:
                break
            if indent is None:
                indent = ind
            if ind != indent:
                continue
            body = nxt.strip()
            body = body[1:].strip() if body.startswith("-") else body
            name = _unquote(re.split(r"[\s:]", body, maxsplit=1)[0])
            if name:
                events.add(name.lower() if NAME_RE.match(name) else UNPARSABLE)
        return events or {UNPARSABLE}  # an `on:` with no readable value counts as a hit
    return {UNPARSABLE}  # no top-level `on:` key found: a workflow with no readable trigger counts as a hit


def main() -> int:
    refs = [l.split() for l in sys.stdin.read().splitlines() if l.strip()]
    shas = []
    for l in refs:
        if len(l) == 4 and set(l[1]) != {"0"} and l[1] not in shas:
            shas.append(l[1])
    try:
        return _check(shas)
    except subprocess.TimeoutExpired as exc:
        print(f"push blocked: git {' '.join(str(a) for a in exc.cmd[1:3])} timed out after {exc.timeout}s; cannot check .github/workflows.",
              file=sys.stderr)
        return 1


def _check(shas: list[str]) -> int:
    bad: list[tuple[str, str, list[str]]] = []
    for sha in shas:
        r = git("ls-tree", "-r", "-z", "--name-only", sha, "--", ".github/workflows")
        if r.returncode != 0:
            print(f"push blocked: git ls-tree failed for {sha[:12]} ({r.stderr.strip()[:200]}); cannot check .github/workflows.",
                  file=sys.stderr)
            return 1
        for path in (p for p in r.stdout.split("\0") if p):
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
