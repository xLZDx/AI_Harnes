# Lessons log

## 2026-11-10 Harness v2 core
- Kind: SUCCESS. Facts: reviewer quality was measured before choosing (194 agent runs, bench/); general-purpose cost 3-3.6x more without better results; domain agents matched code-reviewer on domain bugs at the same cost.
- Why it worked: decisions came from seeded and real defects, not from agent descriptions.
- Rule: change an agent only with a bench run before and after (skill agent-evaluation).
- Kind: SHORTCOMING. Facts: an earlier blind-read check used empty output files and looked like a pass; caught by re-checking transcripts.
- Rule: check that a measuring instrument can fail before trusting its pass.
- Kind: SHORTCOMING. Facts: first commit stored git hooks without an LF rule, so a CRLF checkout would break sh. Fix: .gitattributes eol=lf and executable bit. Rule: add .gitattributes before the first commit of a repo with shell hooks.

## 2026-11-10 Fix review and test findings in harness tools
- Kind: FAILURE. Facts: 3 reviewers (code-reviewer, silent-failure-hunter, python-reviewer) found 8+7+7 MAJOR; tester failed 2 of 51 tests (route.py dropped an operator-requested reviewer at the cap; hooks did nothing when `py -3` points to a missing Python and `|| true` hid it).
- Root cause: tools written in one pass without exercising failure paths (empty or UTF-16 diff became trivial, git errors became an empty HEAD that passed the approval check, task state never went down after a new commit, pre-push checked HEAD instead of pushed refs).
- Caught by: reviewers and the tester; missed by the author.
- Rule: every gate must fail closed on a tool error and leave a visible record (hook-error.log, report banner); every state must be recomputed from evidence so it can go down as well as up.

## 2026-11-10 Keep private project code out of the public repository
- Kind: SHORTCOMING. Facts: bench/ held code copied from ERP, Fitness and AI_trading and html-report golden files held a private project report; the target GitHub repo is public. Operator chose to publish without them.
- Root cause: benchmark artifacts were added to git before the publish target was known.
- Rule: before the first push, check repo visibility and grep for copied project code, personal paths and secrets.

## 2026-11-10 Regression fixes from the second test pass
- Kind: FAILURE. Facts: tester run on 2064576: delete-only push was blocked (empty sha list fell back to HEAD); 1 of 8 concurrent task-add writers exited 1 once (Windows PermissionError on the lock file was not treated as busy).
- Root cause: the fallback did not distinguish no stdin from stdin with only deletes; the lock loop caught only FileExistsError.
- Caught by: tester (test_pre_push_allows_deleting_a_remote_branch, test_concurrent_writers_do_not_lose_updates_and_lock_is_released).
- Rule: on Windows treat PermissionError on a lock file as busy, and retry its removal.

## 2026-11-10 Functional tests for harness tools
- Kind: SUCCESS. Facts: tester wrote 101 tests in 6 files (plain scripts, no pytest); final runs: test_ledger 30 and test_install_hooks 17 OK at f499bc6; test_route 27, test_report 10, test_build_gc 6, test_gc 11 OK at 2064576 (f499bc6 changed only ledger.py lock and delete-only push).
- Why it worked: tests were written from acceptance criteria, not from the code, and caught 4 real defects across two passes.
- Rule: give the tester the contract changes as a list after every fix batch.

## 2026-11-10 GPT-PM round 1 findings
- Kind: FAILURE. Facts: GPT-PM (reply 21dd1b83) found 2 MAJOR: pre-push docs-only check used the net diff, so a code change later reverted passed; report ignored shell-level hook errors written only to hook-error.log.
- Root cause: checked the end state instead of every commit; two error channels but the report read one.
- Caught by: GPT-PM; missed by the author, 3 internal reviewers and the tester.
- Rule: approval checks inspect each commit in the range; every error sink must be read by the place that shows errors.

## 2026-11-10 GPT-PM round 3 (full project review) findings
- Kind: FAILURE. Facts: GPT-PM reply 70669cba, 3 MAJOR: project security_triggers were checked only on risky changes; /ship promised a diff file that route.py never wrote; /ship command and harness-flow skill disagreed on when GPT-PM runs.
- Root cause: tools and prompt text were changed in different passes and never checked against each other.
- Caught by: GPT-PM full review (operator-granted exception to review documents); missed by 3 internal reviewers and the tester, who saw only code.
- Rule: when a tool CLI changes, grep the commands and skills for every place that describes it in the same commit.

## 2026-11-10 Regression tests for GPT-PM rounds 2 and 3
- Kind: SUCCESS. Facts: tester added tests for every GPT-PM finding; at 855f67c test_route 35, test_ledger 32, test_report 11, test_consistency 3 all OK (one run each).
- Why it worked: each finding came with a concrete failure scenario that became a test.
- Rule: turn every reviewer failure scenario into one regression test before the next review round.

## 2026-11-10 Sprint 1 push
- Kind: SUCCESS. Facts: 4 GPT-PM rounds (1 resend for format, 2 with findings, 1 APPROVE); internal reviewers and the tester caught 26 defects before GPT, GPT caught 5 more, 3 of them in prompt text only a full-project review could see.
- Rule: give GPT-PM the prompts and commands at least once per new harness, not only the code.

## 2026-11-10 Install
- Kind: SUCCESS. Facts: snapshot + restore script were written and hash-checked before any file moved; old files were moved, not deleted.
- Rule: every global install ships with its rollback script in the snapshot folder.

## 2026-11-10 Project profiles: the dry run found router defects
- Kind: SHORTCOMING. Facts: running route.py --history on 7 real projects showed that keyword triggers also match documentation lines (docs-only commits got security-reviewer or "risky"), and that --request rejects deliberately unrouted project agents. 35 tests on synthetic diffs did not catch either.
- Root cause: tests used code-only diffs; nobody ran the router on real mixed history before shipping.
- Layer that missed it: tests and reviewers (synthetic inputs only); the dry run caught it.
- Rule: before shipping a router change, dry-run it on at least one real project's history and read the per-agent totals.

## 2026-11-10 S2-T1 implementation
- Kind: SUCCESS. Facts: the dry run on real history found a third case the backlog missed (generated HTML reports were not documentation); found before the commit by re-running the ERP history.
- Rule: after a router fix, re-run the same dry run that found the bug and read the exact commits again.

## 2026-11-10 S2-T1 review findings
- Kind: FAILURE (author). Facts: my first fix used an unanchored pattern (reports?/ at any depth), ignored git's trailing TAB on names with spaces, and attributed a rename hunk only to the new path. Reviewers found all three by reading; the TAB case was reproduced with real git.
- Root cause: I fixed the observed commit (ERP 0f4cdc37) instead of the class of inputs; a pattern that turns code into "docs" removes reviewers silently.
- Rule: any rule that can make a change "documentation" must be anchored as narrowly as the evidence, and must keep code on either side of a rename.

## 2026-11-10 S2-T1 tests
- Kind: SUCCESS. Facts: functional-test-reviewer added 18 tests from the acceptance criteria (test_route now 53, "OK: 0 failing" at 5a8a0a1); its two failures on the way were test bugs (cap and one-specialist rule; old rename expectation), not product defects.
- Rule: when a fix commit lands while tests are being written, send the tester the new rules as explicit expected values, not the diff.

## 2026-11-10 S2 GPT-PM round 1
- Kind: SHORTCOMING. Facts: the diff parser has relied on ---/+++ headers since sprint 1; a pure rename or mode-only commit crashed routing. Three internal reviewers and 53 tests (all hand-built diffs with hunks) missed it; GPT-PM found it by reasoning about git's output formats.
- Root cause: parser tests never used real `git show` output for header-only entries.
- Rule: parser tests include real git output for rename, copy, mode-only and binary entries.

## 2026-11-10 S2 round-1 regression tests
- Kind: SUCCESS. Facts: 8 tests on real `git show` output (pure rename code->code, code->docs with a space, docs->docs, mode-only .py/.md, binary, copy -C -C, --history over them); each fixture asserts its git shape first; test_route "OK: 0 failing" at 78698ee + tests.
- Rule: a fixture asserts the shape it claims (e.g. no ---/+++ lines) so the test cannot pass on the wrong input.

## 2026-11-10 Sprint 2 delivered
- Kind: SUCCESS. Facts: 2 GPT-PM rounds (1 MAJOR, then APPROVE); defects found before GPT: 3 by real-project dry runs, 7 by internal reviewers; 26 new regression tests.
- Rule: dry-run the router on real history at sprint start, not only after shipping.

## 2026-11-10 Parallel profile agents
- Kind: SUCCESS. Facts: 7 Sonnet implementation agents, one per project, one shared brief file; each snapshotted before moving, validated JSON and dry-ran the router; 3 of them caught their own JSON escaping error from a shell heredoc.
- Rule: shared brief file + one agent per independent repo; write JSON with a file tool, never through a shell heredoc.
