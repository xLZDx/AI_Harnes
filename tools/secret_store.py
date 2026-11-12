#!/usr/bin/env python3
"""Secret store outside git for keys the agent issued itself (S9-T4).

  secret_store.py put NAME     read the value from stdin, save it only if NAME is absent (exclusive create, so two
                               concurrent runs or a retry never end with two different values); prints `created` or `exists`
  secret_store.py has NAME     exit 0 if NAME exists, 1 if not
  secret_store.py names        list stored names, never values
  secret_store.py log NAME     append one value-free line to core/DECISION_LOG.md of the current repo

The folder is HARNESS_SECRET_DIR or ~/.harness-secrets. It is refused when it lies inside a git work tree. The value is never
printed and never taken from the command line (a command line is logged by the shell and the hooks).
"""
from __future__ import annotations

import datetime
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def store_dir() -> Path:
    return Path(os.environ.get("HARNESS_SECRET_DIR") or (Path.home() / ".harness-secrets")).resolve()


def inside_git(p: Path) -> bool:
    probe = p if p.exists() else p.parent
    while not probe.exists():
        probe = probe.parent
    r = subprocess.run(["git", "-C", str(probe), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True)
    return r.returncode == 0 and r.stdout.strip() == "true"


def put(name: str, value: str) -> str:
    d = store_dir()
    if inside_git(d):
        raise SystemExit("refused: the secret folder is inside a git work tree")
    if not value:
        raise SystemExit("refused: empty value")
    d.mkdir(parents=True, exist_ok=True)
    target = d / name
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    except FileExistsError:
        return "exists"
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(value)
    return "created"


def log_line(name: str, repo: Path) -> None:
    f = repo / "core" / "DECISION_LOG.md"
    day = datetime.date.today().isoformat()
    with open(f, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(f"\n- {day} secret stored: name {name}, in the user secret store outside git (value not logged).\n")


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in {"put", "has", "names", "log"}:
        print(__doc__)
        return 2
    cmd = argv[0]
    if cmd == "names":
        d = store_dir()
        for p in sorted(d.iterdir()) if d.is_dir() else []:
            print(p.name)
        return 0
    if len(argv) < 2 or not NAME_RE.match(argv[1]):
        print("refused: NAME must match [A-Za-z_][A-Za-z0-9_]{0,63}")
        return 2
    name = argv[1]
    if cmd == "has":
        return 0 if (store_dir() / name).exists() else 1
    if cmd == "log":
        log_line(name, Path.cwd())
        return 0
    print(put(name, sys.stdin.read().rstrip("\r\n")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
