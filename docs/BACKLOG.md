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
