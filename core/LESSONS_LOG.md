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
