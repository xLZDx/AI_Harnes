# Harness recovery plan, 2026-11-12

The operator stopped every session on every project until the harness itself is fixed and tested.
All items below are harness-wide: one mechanism, installed into every project and both clients
(Claude Code and Codex). Nothing is fixed per project by hand.

## Operator complaints (what went wrong)

| # | Complaint | Verified cause |
|---|---|---|
| C1 | Agents ask the operator for things they can do themselves (keys, stacks, access); worst in Codex | Ladder rule exists (S9) but Codex reads AGENTS.md only at session start and its new hooks are untrusted; ask_last_gate also blocks legitimate permission questions |
| C2 | Program reports use the wrong data and do not show the whole program | report.py chose the ledger over the plan; plans with roadmap-only sprints were not counted; superseded rows dropped; ledgers rebuilt from markdown rows (child and repeated rows) |
| C3 | Ledgers must carry the whole program with the old, correct statuses | No tool syncs a ledger with the project plan; each agent rebuilt ledgers its own way |
| C4 | Agent START/STOP and agent name are not visible | SubagentStart was never registered in settings.json; STOP is only a hook message the app may not show; status line works only in the CLI |
| C5 | CI ran on a pull request although CI is prohibited | Rule existed only as text; 15 repositories started CI on push and pull request; no push guard; agent read CI results |
| C6 | Prohibited checks (full npm test, vitest, lint, typecheck, verify) are not blocked | Only reviewer agents are restricted; main sessions and Codex are not |
| C7 | The operator's nine development rules are not enforced | Rules partly in text, no hooks for one PR per sprint, tests only at sprint end, commit detail file, profile-only reviewers, doc gates auto-close, backlog |
| C8 | Agents stop and wait instead of working; hooks loop on false positives | decision_explain_gate treats task ids in brackets as decision refs; project plan gate checks the repo the shell happens to be in; waiting for an own agent is treated as a stop |
| C9 | Ledger commit hook says "unknown task" and commits are not counted | Commits name tasks the ledger does not have (ledger not synced with the plan) |
| C10 | Wrong statements to the operator (Codex /hooks) | Negative claim made from one help screen |

## What I (Claude) missed

- Reported agent visibility as done without checking the registration or the live screen.
- Said twice that Codex has no /hooks and locked the wrong text into a test.
- S10 fixed the report only half way; the first S11 merge repeated the bug.
- Wrote the no-CI rule without checking repository workflow triggers.
- Ended many turns with "Blockers: EXTERNAL" while only waiting for my own agents.
- S9 and S10 are pushed but not merged; S11 was uncommitted when the operator stopped work.

## Done already (2026-11-12)

- CI off on GitHub: 28 check workflows disabled in 13 repositories (`gh workflow disable`); before-list and
  disabled-list in D:/Backup; rollback with `gh workflow enable`. Left on: dependabot, deploy and migrate
  workflows, the marketing repository.
- Report generator: canonical sprint sizes, superseded rows kept, bounded acceptance not counted as done,
  strict roadmap parsing, safe sprint-table importer (S11, commit 4093d03, tests green).

## Plan (one recovery sprint, one PR, one GPT-PM review)

| Task | Fixes | Mechanism (all projects, both clients) | Proof |
|---|---|---|---|
| R1 | C5, C6 | New hook `rules_gate.py` (PreToolUse, Claude and Codex): denies full npm test, vitest, lint, typecheck, verify, full pytest, gh workflow run/enable, gh run rerun/watch, gh pr checks; test agents may run named test files only. Project pre-push hook refuses a push whose commits contain a workflow started by push, pull_request or schedule. | Hook fed each prohibited command in every project: denied; push with an auto workflow: refused |
| R2 | C7 (push and PR) | `git push` and `gh pr create` allowed only when the ledger approval is bound to HEAD and every sprint task is verified; both clients and the git hook | Fed push/PR without approval: denied; with approval: allowed |
| R3 | C7 (all nine rules) | The operator's nine rules verbatim in CORE.md, built into CLAUDE.md and AGENTS.md of every project and the Codex home; commit detail rule checked by the DECISION_LOG gate in both clients | Audit lists the rules block and the gate in every project |
| R4 | C4 | Register SubagentStart and PreToolUse(Agent) for agent_visibility; agent description must name the agent role; check in a new session | Registration lines in settings.json; screenshot of START and STOP |
| R5 | C1, C8 | ask_last_gate lets permission questions through; decision_explain_gate ignores task ids; project plan gate checks only the repo of the active task; waiting for an own running agent is a valid end of turn without a blocker line | Each false positive fed to its hook: allowed; real cases still blocked |
| R6 | C2, C3, C9 | `tools/ledger_sync.py`: rebuild every project ledger from its plan (canonical counts, historical statuses, placeholders marked UNVERIFIED, genuine harness-flow sprints kept), with backup and idempotent re-run; master-plan importer for plans kept outside PROJECT_PLAN.md | Table per project: plan tasks = ledger tasks = report denominator |
| R7 | C1 (Codex) | Codex: AGENTS.md with the rules in every project, installer prints the real hook review route, new sessions | Installer output; operator approves hooks once |
| R8 | all | `tools/audit_projects.py`: one table over all projects and both clients (hooks installed, rules present, CI triggers off, gates deny the prohibited cases) | Table shown to the operator |

Only operator step: approve the Codex hooks once (Codex app: Settings -> Hooks or Review hooks; CLI: /hooks)
and start new sessions after R8 is green.
