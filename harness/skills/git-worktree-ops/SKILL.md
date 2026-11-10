---
name: git-worktree-ops
description: Operate safely across repositories and worktrees. Use before editing when several checkouts or concurrent sessions exist.
---

# git-worktree-ops

## Apply when
Any edit or git operation where worktrees, sibling projects or concurrent sessions exist.

## Method
1. Resolve `git rev-parse --show-toplevel` and `--git-common-dir`, branch, HEAD and dirty files before editing.
2. Assume another session may be editing: never stage or commit blind - inspect the diff of exactly the intended paths.
3. Verify the exact outbound range before any push; one child project per session.
4. A worktree's `.claude` copy is not a separate agent set; do not index it as one.

## Required output
Pre/post snapshot (branch, head, status) and the exact paths touched.

## Do not
Run `git reset --hard`, `git clean -f*`, force-push or delete branches/refs without separate operator approval.

## Removing a worktree
Remove a worktree only after its branch is merged, after checking that no running process uses its folder, that `git status --ignored` is clean, and that it has no junctions pointing outside it.

Typical failure modes: `references/failure-modes.md` (read only when the work hits one of them).
