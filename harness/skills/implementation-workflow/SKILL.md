---
name: implementation-workflow
description: Implement scoped changes with minimal compatible diffs and evidence. Use for an approved feature, fix or refactor whose requirements are known.
---

# implementation-workflow

## Apply when
Implementing an approved, scoped change (feature, bug fix, refactor).

## Method
1. Restate acceptance criteria as checkable statements; read callers, tests and the existing contract first.
2. Pick the nearest working pattern; make the smallest compatible diff; no drive-by refactors.
3. Bug fix: write the regression test first and see it fail for the right reason. Behavior change: add behavior-level verification.
4. Run targeted checks, then the project's required gate; inspect `git diff` for stray edits and EOL/encoding changes.
5. Report exactly what ran and what was not verified.

## Required output
What changed (paths); evidence (exact commands + results); unverified items; follow-ups left out of scope.

## Do not
Delete files/data/resources, rewrite history, commit/push or widen scope without explicit approval; call a change done on a green but unrelated suite.

Typical failure modes: `references/failure-modes.md` (read only when the work hits one of them).
