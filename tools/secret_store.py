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
import subprocess
import sys
import tempfile
from pathlib import Path

NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def valid_name(name: str) -> bool:
    return bool(NAME_RE.fullmatch(name)) and name.upper() not in RESERVED


def store_dir() -> Path:
    return Path(os.environ.get("HARNESS_SECRET_DIR") or (Path.home() / ".harness-secrets")).resolve()


def inside_git(p: Path) -> bool:
    """True when p is inside a git work tree. Fails closed: any git error other than "not a git repository" counts as inside."""
    probe = p if p.exists() else p.parent
    while not probe.exists():
        probe = probe.parent
    try:
        r = subprocess.run(["git", "-C", str(probe), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True)
    except OSError:
        return False  # git is not installed, so there is no work tree to leak into
    if r.returncode == 0:
        return r.stdout.strip() == "true"
    return "not a git repository" not in (r.stderr or "").lower()


def put(name: str, value: str) -> str:
    d = store_dir()
    if inside_git(d):
        raise SystemExit("refused: the secret folder is inside a git work tree")
    if not value.strip():
        raise SystemExit("refused: empty value")
    if d.exists() and not d.is_dir():
        raise SystemExit("refused: the secret path is not a folder")
    d.mkdir(parents=True, exist_ok=True)
    target = d / name
    if target.exists():
        return "exists"
    # Write the complete value to a private temp file, then publish it without overwriting: a crash never leaves an empty secret,
    # and of two concurrent runs only one link/rename succeeds.
    fd, tmp = tempfile.mkstemp(prefix=".put-", dir=str(d))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(value.encode("utf-8"))
        try:
            os.link(tmp, target)
        except FileExistsError:
            return "exists"
        except OSError:
            try:
                os.rename(tmp, target)  # no-overwrite on Windows; where the link is unsupported
                tmp = ""
            except FileExistsError:
                return "exists"
        return "created"
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def log_line(name: str, repo: Path) -> None:
    f = repo / "core" / "DECISION_LOG.md"
    if not f.is_file():
        raise SystemExit("refused: core/DECISION_LOG.md not found in the current folder")
    day = datetime.date.today().isoformat()
    line = f"- {day} secret stored: name {name}, in the user secret store outside git (value not logged)."
    if line in f.read_text(encoding="utf-8").splitlines():
        return  # the same name on the same day is logged once; a retry adds nothing
    with open(f, "a", encoding="utf-8", newline="\n") as fh:
        fh.write("\n" + line + "\n")


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
    if len(argv) < 2 or not valid_name(argv[1]):
        print("refused: NAME must match [A-Za-z_][A-Za-z0-9_]{0,63} and not be a Windows device name")
        return 2
    name = argv[1]
    if cmd == "has":
        return 0 if (store_dir() / name).exists() else 1
    if cmd == "log":
        if not (store_dir() / name).exists():
            print("refused: NAME is not stored")
            return 2
        log_line(name, Path.cwd())
        return 0
    print(put(name, sys.stdin.buffer.read().decode("utf-8-sig").rstrip("\r\n")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
