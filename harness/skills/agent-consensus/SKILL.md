---
name: agent-consensus
description: Evidence-based review by the few reviewers that .harness-tools/route.py selects, with a fixed rule for accepting findings and one dispute round, plus an optional Codex second opinion. Use at /ship after tests and before GPT-PM, or when a risky decision needs independent challenge.
---

# agent-consensus - evidence, not voting

Goal: find independent failure modes with the smallest useful roster. Agreement count is not truth; evidence is.

## Selecting reviewers
- Run `python .harness-tools/route.py --task <id>` for each task. It returns 0-4 reviewers by risk; never launch more than 4 and never add roles for coverage's sake.
- 0 reviewers means no review round; if one agent can settle a question with primary evidence, stop there.
- Launch real agents, never in-chat role play. Use only the allowed reviewer models.

## Round 1 (independent, parallel)
1. Give each reviewer only: the diff file, the changed-file list, the task acceptance criteria and the output schema. Not other reviewers' opinions.
2. Finding schema: `ID | severity | claim | evidence (file:line actually read) | failure scenario | impact | required change | acceptance test`. Severity: BLOCKER, MAJOR, MINOR (NIT omitted).
3. Deduplicate by root cause; verify each load-bearing file:line yourself; separate fact from preference.

## Acceptance rule
- A BLOCKER/MAJOR is accepted when a second reviewer independently confirms it, or when there is a verifiable code fact (a reproducible run, or a line that demonstrably does what the finding says).
- A finding raised by one reviewer only, without a verifiable fact, is never silently dropped. Keep it as a separate item and send it to GPT-PM (`gpt-pm-gate`) marked "single-reviewer, unconfirmed".
- Rejected findings are listed with the reason. Evidence order: measured behavior > reproducible test > primary docs > code inference > preference > vote count.

## Dispute round (one only)
If reviewers contradict on a BLOCKER/MAJOR, run exactly one round: the owning specialist and one counter-reviewer address that specific disagreement. If it is still open, classify it ACCEPT / MODIFY / REJECT / UNKNOWN and pass it to GPT-PM as an open item. Do not loop and do not manufacture consensus.

## Remediation
Fix every accepted BLOCKER/MAJOR in one batch; re-check only those fixes and regressions they caused. Unrelated MINOR items go to the backlog.

## Output
Reviewers used (and model/effort actually run); accepted BLOCKER/MAJOR first; single-reviewer items; rejected findings with reason; unresolved decisions; required changes with acceptance tests. No transcripts.

## Variant - Codex second opinion (optional)
Use only when an independent model can change a decision (an unresolved major finding, a high-risk change, or the operator asks). Never a mandatory gate and never a panel seat.
- Run `py -3 "$HOME/.claude/tools/codex_review.py" --cwd <repo> --uncommitted` (or `--base <branch>`, `--commit <sha>`): one sequential read-only call.
- It reviews the diff cold: never feed it other reviewers' findings; reconcile afterwards one-on-one.
- Verify every accepted finding at the cited file:line; label unsupported citations `CONFABULATION`. Silence is not agreement; majority is not proof.
- One round, one rebuttal for a still-contested BLOCKER/MAJOR, then surface the disagreement. If Codex is unavailable or hits its usage limit, report `skipped(<reason>)` and do not retry or block.

## Do not
Use roster size as a quality metric; let an implementer approve its own change; spend rounds on settled MINOR items.

References: `references/full-procedure.md` (full original procedure), `references/reference.md` (Round 0 schema and report template), `references/codex-full-procedure.md` (Codex manual mode).
