"""Remove scratch worktrees whose branch is merged into the default branch. Called by post-merge.

A worktree is removed only when all hold: it lives under the scratch root; its folder exists; its HEAD
is reachable from the default branch; it has its own commits (tip differs from the default branch)
or is older than HARNESS_WORKTREE_MIN_AGE_H hours (default 2); `git status --ignored --porcelain`
succeeds and is empty; `git worktree remove` (no --force) succeeds. Windows refuses to delete a
folder a running process uses, so a busy worktree stays. Every skip is printed with its reason;
the exit code is 1 when a git call failed. Kill switch: HARNESS_WORKTREE_GC=off.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

SCRATCH = Path(os.environ.get("HARNESS_SCRATCH_ROOT", r"D:\tmp\scratch_worktrees")).resolve()
MIN_AGE_H = float(os.environ.get("HARNESS_WORKTREE_MIN_AGE_H", "2"))
ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def git(*args, cwd=None):
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd, env=ENV)


def default_branch() -> str | None:
    r = git("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    for b in ("main", "master"):
        if git("rev-parse", "--verify", "-q", b).returncode == 0:
            return b
    return None


def main() -> int:
    if os.environ.get("HARNESS_WORKTREE_GC") == "off":
        return 0
    base = default_branch()
    if not base:
        print("worktree_gc: no default branch (origin/HEAD, main or master); nothing removed", file=sys.stderr)
        return 1
    base_tip = git("rev-parse", base).stdout.strip()
    git("worktree", "prune")
    lst = git("worktree", "list", "--porcelain")
    if lst.returncode != 0:
        print(f"worktree_gc: git worktree list failed: {lst.stderr.strip()}", file=sys.stderr)
        return 1
    items, cur = [], {}
    for line in lst.stdout.splitlines() + [""]:
        if not line:
            if cur:
                items.append(cur)
            cur = {}
        else:
            k, _, v = line.partition(" ")
            cur[k] = v
    rc = 0
    for w in items:
        path = Path(w.get("worktree", "")).resolve()
        if SCRATCH not in path.parents:
            continue
        try:
            if not path.is_dir():
                print(f"keep {path}: folder missing")
                continue
            sha = w.get("HEAD", "")
            if git("merge-base", "--is-ancestor", sha, base).returncode != 0:
                print(f"keep {path}: not merged into {base}")
                continue
            age_h = (time.time() - path.stat().st_mtime) / 3600
            if sha == base_tip and age_h < MIN_AGE_H:
                print(f"keep {path}: no own commits and younger than {MIN_AGE_H} h (may be in use)")
                continue
            st = git("status", "--ignored", "--porcelain", cwd=path)
            if st.returncode != 0:
                print(f"keep {path}: git status failed: {st.stderr.strip()}")
                rc = 1
                continue
            if st.stdout.strip():
                print(f"keep {path}: has changed or ignored files")
                continue
            r = git("worktree", "remove", str(path))
            print(f"removed {path}" if r.returncode == 0 else f"keep {path}: {r.stderr.strip()}")
        except OSError as e:
            print(f"keep {path}: {e}")
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
