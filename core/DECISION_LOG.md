# Decision log

## 2026-11-10 Harness v2 core
- Built from measurements in D:\Backup\claude-harness-2026-11-10-copy (series 1-7): 45 -> 21 global agents, 48 -> 24 skills.
- Operator decisions: keep and improve the operator's own agents; launch agents per task, not all at once; security-reviewer only locally or on request; all agents Sonnet medium; GPT-PM once per sprint until APPROVE; 0-4 reviewers with consensus.
- Reviewer choice is a script (tools/route.py), not a model decision; the choice and reason go to the ledger.
- 2026-11-10: all text files are stored with LF; git hooks are executable in the index.

## 2026-11-10 Review fixes
- security-reviewer has no global automatic triggers; a project enables it with security_triggers in .harness/profile.json, or the operator uses --request (router dry run over 40 ERP commits: 10 security picks before, 0 after).
- The ledger lives in the main checkout (git common dir) and the hooks path is absolute, so worktrees share one ledger and run the hooks.
- pre-push checks every pushed sha: the approved commit, its ancestor, or a docs-only descendant (lets /close documents through without a new GPT round).
- approve requires --evidence (GPT-PM verdict reference); HARNESS_SKIP_APPROVAL is recorded as an event and shown in the report.

## 2026-11-10 Publication
- Operator decision: publish to the public repo without real project code; bench/realbench, bench/trbench_hidden, their keys and scripts, and html-report/golden stay local (gitignored). The public history is one squashed commit so the earlier commits that held them are not published.

- 2026-11-10: a push that only deletes remote refs passes the approval check (nothing is published).

- 2026-11-10: tests/ are plain Python scripts run with python -I tests/<file>.py (pytest is not used by operator rule).
