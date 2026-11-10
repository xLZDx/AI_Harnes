# git-worktree-ops: failure modes

Load on demand. Each line is a defect class seen in practice.

- Working in the main checkout while the real work lives in a linked worktree.
- `git add -A` sweeping in another session's files.
- Worktree removal following a junction into live data.
- Same branch checked out in two places, one silently stale.
