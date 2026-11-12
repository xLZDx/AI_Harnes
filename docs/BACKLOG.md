# Harness_v2 backlog

Operator rule 9: every unresolved question goes here and is reviewed at sprint end if it blocks other work, or at the
end of the planned pass. Each item: what, why it is open, what closes it.

| # | Item | Why open | Closes when |
|---|---|---|---|
| B1 | DONE 2026-11-12 (59aff27): Roadmap placeholder sprint titles are upper-cased (`S05-Title` -> `S05 TITLE`) by tools/plan_ledger.py | Cosmetic, found in S11 | Title keeps its case in the report; test in test_s11_plan_import |
| B2 | S14-T1: dangerous_command_gate splits segments without reading quotes (shell_policy_gate was fixed in S13) | Same class as the S13 shell_policy findings | Quote-aware split shared by both gates, tested with the S13 tables |
| B3 | S14-T2 (full suites are already prohibited, no operator decision needed): rules_gate does not classify `flutter test`, `go test`, `playwright test`, `cargo test` as full suites | Not in the operator's prohibited list yet | Operator confirms the list; gate and tests extended |
| B4 | S14-T2: rules_gate does not analyse `$(...)`/backtick substitution and `case` blocks | Reviewer note, S13 | Gate parses them or refuses them; tests |
| B5 | S14-T4: ledger commit-hook warns "names unknown task" for commits that predate the synced ledger (ERP 7, Fitness 25, PDCC 1) | History, not a live defect | Old warnings archived or matched to plan ids |
| B6 | S14-T3: ci_triggers_off cannot run in a repo with a stopped session's uncommitted workflow or log edits; S13 used a one-off scratch method (temp worktree, update-ref, 3-way merge) for ERP_MCP and PDCC | The method is not a harness tool yet; it dropped intent-to-add marks once | Method moved into the tool with an index snapshot (git ls-files --stage) and tests, or the case stays manual |
| B7 | Leftover temp branches ci-off/ERP_MCP-20261112 and ci-off/ERP_MCP-20261112-183553 in ERP_MCP | Force branch delete is operator-only | Operator deletes them, or they are left as harmless history |
| B8 | Scratch worktrees not merged (maps1, chat1, rel223, w2, aeve-*, s00, s01-*, Virtual_scrum_team-s5) and skin1 with uncommitted changes | Their branches are not merged; section 9 forbids removal | Their branches merge, then the sweep removes them |
| B9 | CLOSED 2026-11-12 by the OS lock (1791aee), no stale lock can exist: A plan lock whose creator crashed between create and write is never taken over (safe) | Needs a manual delete in that rare case | A creation-time-only lock format, or a documented manual step |
| B10 | GitHub "Preview deploy" (PDCC) and deploy/migrate workflows stay enabled by decision | Deploys are not CI checks; operator may want them off too | Operator decision recorded |
| B11 | Merge PRs 1 and 2 (S9, S10) and the S11+S13 PR | Merge is the operator's step per project rules | Merged |
| B12 | PM Bridge state/messages.jsonl and the agent_visibility state are local files an agent could write, so the S14-T6/T7 checks stop a typed claim but not a deliberate file forgery | Needs a signed record (PM Bridge daemon signature) or a gate that denies agent writes to those files | Gate or signature in place, tested |
| B13 | AEVE s20-plan commits 584eaf0..c191a86 and the S20 plan statuses (T01 ACCEPTED, T02-T07 BLOCKED_OPERATOR) were written and pushed by a runaway citation-judge child | Unreviewed; the plan states are claims | AEVE's S20 review (test agents, reviewers, GPT-PM) covers them, or they are reverted by a new commit |
| B14 | rules_gate python-heredoc exemption names only os.system/os.popen/subprocess/shell=True; other ways to start a shell (from os import system, __import__, os.exec*/spawn*, pty, ctypes) keep the substitution neutralised | Single-reviewer MINOR (silent-failure-hunter round 3), deliberate spelling needed | Broader match or no neutralising when the body holds a substitution and any exec/spawn/system word; test with from os import system |
| B15 | session_close_gate interim-notice check searches the whole notification (result text too) and does not remember finished ids, so a finished agent can count as running for up to 30 minutes | Single-reviewer MINOR (silent-failure-hunter, be12863); bounded by the 30-minute limit | Match only the <summary> element and keep a done set; tests |
| B16 | ledger recompute makes verify/review stale on any task commit, while require_agent_run ignores docs-only and tests-only commits | Every log or test commit forces a re-record | recompute uses the same code-commit rule |
