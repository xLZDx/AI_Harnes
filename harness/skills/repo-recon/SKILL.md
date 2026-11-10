---
name: repo-recon
description: Build a fast, deterministic repository context before reasoning. Use before design, review or edits in an unfamiliar repo or area.
---

# repo-recon

## Apply when
Starting work in a repo/area you have not read this session, before any design or verdict.

## Method
1. Resolve `git rev-parse --show-toplevel`, branch, HEAD and `git status --short`; note linked worktrees and files that are already dirty (never overwrite those blind).
2. Read the nearest instruction files (CLAUDE.md, AGENTS.md, `.claude/rules`) and the project's source-of-truth index; find the pinned interpreter/toolchain and the canonical test command.
3. Locate entrypoints, module boundaries, tests, CI definitions and migrations; find the nearest existing analogue of the requested change.
4. Record unknowns explicitly instead of guessing.

## Required output
Fact sheet: root / branch / head / dirty files; stack and commands; boundaries; analogue path; unknowns. Facts and paths only.

## Do not
Propose architecture or verdicts during recon; infer facts from sibling repos that share a parent folder; trust a status doc over git/CI state.

Typical failure modes: `references/failure-modes.md` (read only when the work hits one of them).
