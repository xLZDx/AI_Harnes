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

## 2026-11-10 Codex project agents were left behind
- Kind: FAILURE (author). Facts: the project profile work rewrote .claude/agents in 6 projects but not their .codex/agents; I reported Codex as done because ~/.codex was converted. The operator's question "Для кодекса тоже всё переделал?" exposed it; a file count showed ERP 14 vs 8, PDCC 28 vs 6, db-test 33 vs 6.
- Root cause: the conversion was a one-off script for the global folder only; no tool produced Codex files from the project sources.
- Layer that missed it: author and reviewers (nobody compared the two trees).
- Rule: every Claude-side agent change ends with a Claude-vs-Codex name comparison in the same task; Codex files are generated by build.py, never by hand.

## 2026-11-11 S3-T1 review findings
- Kind: FAILURE (author). Facts: my generator used Unicode \w for an ASCII-only name rule (tester failure, 3 reviewers), compared names case-sensitively on NTFS, accepted multi-line descriptions by truncating them, and moved files before writing new ones with no failure report. My first strict frontmatter fix then rejected 5 real db-test agents that carry a nested hooks: map; the real-project smoke run caught it.
- Root cause: wrote the parser for the one file shape I had looked at; Python's \w is Unicode by default.
- Rule: a character rule in a spec becomes an explicit character class, never \w; run new parsers on every real input before review.
- Kind: FAILURE (author). Facts: both S3-T1 commits put "Task: S3-T1" at the end of the subject line; ledger commit-hook only reads a "Task:" line of its own, so it recorded nothing and verify was refused. The two unpushed commits were squashed into one with a proper trailer.
- Rule: the Task trailer is its own line (`-m "<summary>" -m "Task: <ID>"`), and after every commit check that commit-hook printed "ledger: <ID> <- <sha>".

## 2026-11-11 Codex parity scope
- Kind: SHORTCOMING (author). Facts: I first read "Для кодекса тоже всё переделал?" as "the agents" and started an agents-only fix; the operator meant the whole harness. The gap was large: Codex ran 3 of 27 Claude hook entries, with a stale 100-name matcher.
- Rule: when the operator asks about parity, list every layer (rules, agents, skills, commands, hooks, project files) with counts on both sides before choosing what to fix.
- Kind: SHORTCOMING. Facts: removing personal paths from the hooks (public repo) silently changed behavior: the supervisor lost its skip list, PM mode read as off, the plan tool path did not exist for Codex. Caught by reading both porting agents' reports before install.
- Rule: a portability change moves machine values into a local config with the same values, never into defaults that differ.

## 2026-11-11 S3-T2 hook port review
- Kind: FAILURE (author). Facts: I guessed the client from an environment variable (CODEX_HOME), took the SubagentStop exclusion list from "agents that can write" instead of the old matcher, kept only the first path of a multi-file patch, and left git timeouts longer than the hook timeout. Five reviewers and the tester found 5 MAJOR plus about 15 MINOR in one pass; the tester also hit a stray backspace byte (a shell turned `\b` into 0x08) in its own regex.
- Root cause: ported by reading the code, not by diffing the live Claude settings against the generated ones, and no check compared timeouts with subprocess budgets.
- Layer that missed it: author; found by reviewers and tests.
- Rule: a port starts with a table of the live registry (event, matcher, timeout) and the generated one, diffed by a script; the client is told by the installed command, never guessed.
- Kind: SUCCESS. Facts: the operator's one-line rule (Codex agents only on the latest Luna, medium) was checked against ~/.codex/models_cache.json before use (gpt-6-luna vs gpt-5.6-luna) and turned into a gate plus a test.
- Rule: resolve "latest" from the installed model list, not from memory.
- Kind: SHORTCOMING. Facts: three of the subagents hit their turn limit before reporting (two reviewers, the tester); each had to be resumed or its file run by hand.
- Rule: give a reviewer of a 10k-line diff a turn budget in the brief and tell it which files to read first.

## 2026-11-10 Parallel profile agents
- Kind: SUCCESS. Facts: 7 Sonnet implementation agents, one per project, one shared brief file; each snapshotted before moving, validated JSON and dry-ran the router; 3 of them caught their own JSON escaping error from a shell heredoc.
- Rule: shared brief file + one agent per independent repo; write JSON with a file tool, never through a shell heredoc.

## 2026-11-11 S3-T3 review fixes
- Kind: FAILURE. Facts: a parity test passed only because the word config.toml appeared in a comment of the generated restore script; it checked the wrong thing after the restore design changed. Layer missed: tests (a text-contains check on a whole file). Rule: assert on the lines that matter, and re-read old expectations when a design decision flips.
- Kind: SUCCESS. Facts: the silent-failure review found 12 items, 10 of them were already fixed by the earlier batch; the rest became a printout of replaced hook entries and temp-file cleanup. Rule: send the reviewer the diff after the fix batch, not before.

## 2026-11-11 GPT-PM round 1 findings on the Codex spawn gate
- Kind: FAILURE. Facts: the Luna rule was enforced only for values that were present; an absent model fell through to "allow", and the error handler failed closed for one client only. Root cause: the gate was written as a deny-list of wrong values instead of a proof of the right one. Layer missed: author and three reviewers; only the sprint-level GPT-PM saw it. Rule: a policy gate must prove the required value and fail closed on unknown, and every client branch needs the same error handling.

## 2026-11-11 Claude hooks were left on the old set while Codex was built
- Kind: FAILURE. Facts: the day-one install put agents, skills and commands into ~/.claude but left settings.json hooks on the old system (35 entries, 3 af_cli calls), and no report said so; Codex was then built from the shared source first. The operator found the gap. Root cause: I filed the live switch as a "separate decision" although the hook set was already chosen. Layer missed: author (reporting), no gate. Rule: when an install is reported done, list what was NOT switched; never turn an already-made decision into a new question.
- Kind: SUCCESS. Facts: the live switch was proven by a probe that runs the real installed hook commands with a dangerous payload, not by reading the settings file. Rule: prove a protection by trying to break it.

## 2026-11-11 Probe that could not fail
- Kind: FAILURE. Facts: my own proof script printed a list and exited 0 whatever the list held. Root cause: written as a demo, not as a check. Layer missed: author; the tester only saw the installer, and only GPT-PM read the probe. Rule: every proof script exits nonzero when the property does not hold, and has a test for the failing case.
- Kind: SHORTCOMING. Facts: the same probe needed a second fix round because the first fix closed only the cases the reviewer named (empty result) and left the matcher test as a text search. Rule: when fixing a check, list every way it can say OK wrongly (input selection, execution, result reading) and close all of them in one batch.

## 2026-11-11 Decision-refs check false positives
- Kind: SHORTCOMING. Facts: the Stop check treated any bracketed letters+digits as a decision code and blocked five of my replies that contained a sprint code or a commit hash. Root cause: the pattern was written for known codes but matched everything. Layer missed: author of the hook, tests. Rule: a text-pattern gate needs a list of known false-positive shapes (hashes, ids) and a test for each.
- Kind: SUCCESS. Facts: two backlog fixes were delegated with exact behaviors to assert, then tested by a separate agent: 61 checks passed on the first run. Rule: the implementer lists the behaviors a tester must assert; the tester writes the tests.
- Kind: SUCCESS. Facts: a tiny env-var seam made a half-way install failure testable end to end, including a real restore.ps1 run. Rule: add a test seam instead of mocking inside the tool.
- Kind: SHORTCOMING. Facts: the Codex shell response shape is still unverified; the code now fails safe (unknown shape = no success recorded) instead of guessing. Rule: when a payload shape is unverified, make the unknown case the safe case and say so in the log.

## 2026-11-11 Tests that used the live folder as the oracle
- Kind: FAILURE. Facts: after I installed the new hooks into ~/.claude, 27 parity tests failed because they read the original hooks from that same live folder; I misread the first red run as a product regression until I read the traceback. Layer missed: author of the tests (oracle tied to a mutable live path) and me (installed before checking what else reads the folder). Rule: a test oracle is a frozen snapshot, not a live folder; before overwriting a live folder, grep the tests for it.

## 2026-11-11 Report format work
- Kind: SHORTCOMING. Facts: I could not look at the rendered page (browser refused local files and localhost), so the check was structural: classes used vs defined, section order, private words. Found one missing class (headline) and a Windows timezone name in the text. Rule: when the visual check is impossible, say so and compare structure mechanically against the template.
- Kind: SUCCESS. Facts: reading the asset before reuse caught a defect in the template's own script (the copy button copied one table). Rule: read generic assets before copying them into a shared tool.
- Kind: SUCCESS. Facts: the hook test makes a real git commit with the installed hook and shows the report inside that commit; two failure variants (reports path is a file, corrupt ledger) still create the commit. Rule: test a never-block hook by making it fail on purpose.
- Kind: SUCCESS. Facts: the template held private data; I kept it as a local frozen copy and put only generic assets into the public repo, with a test that the output never reads the template. Rule: a template that came from a private project goes to a local config path, never into a public repo.

## 2026-11-11 Report tool: three defects found only by GPT-PM
- Kind: FAILURE. Facts: the tool that writes into git history could replace a good report with an empty one, hid its own failures behind exit 0, and printed the remote URL with embedded credentials. Root cause: I treated "never block the commit" as "never fail", and I did not ask what a generated file puts into history. Layer missed: author, my tester (cases came from my own spec), no security review (the tool was not on the trust-boundary list). Rule: a tool that writes into history gets a "what could it leak or destroy" check, and "do not block" is implemented in the caller, not by hiding the exit code.
- Kind: SHORTCOMING. Facts: I fixed the URL leak for the URL form the reviewer named and left the second form (SCP-style) open, so it cost another review round. Rule: when sanitizing input, enumerate every syntax the input can take (here: scheme URL, SCP-style, user:pw@host) before writing the fix, and test each.

## 2026-11-12 Sprint S7
- Kind: FAILURE. Facts: at S6 I reported "0 failing" for my own list of test files while tests/test_report.py (11), tests/test_install_hooks.py (3) and tests/test_ledger.py (1) already failed on a clean HEAD; found at S7 by running the whole own set once. Root cause: I chose the files to run from memory instead of globbing tests/. Layer that missed it: author. Rule: the sprint-end run is `tests/*.py` by glob, and the report names every file.
- Kind: FAILURE. Facts: the operator received the old AEVE_PROJECT_PLAN.ru.html after the "all projects, one format" order, because I installed the hook only in the 7 projects the operator had listed earlier and never listed repos with PROJECT_PLAN.md (10 found, 9 without Harness). Rule: an order about "all projects" starts with a listing of every repository and its state, not with the last list I was given.
- Kind: FAILURE. Facts: literal backspace characters (a "\b" typed inside a normal string) had sat in two production hooks and one new module; a test agent found it in the new module (English money words never matched). Rule: after writing regexes into a file, check the file for control characters.
- Kind: SUCCESS. Facts: asking each test agent for the observed defects instead of green tests exposed 11 real defects (backspace regex, ExitPlanMode result not checked, wrong-shape ledger, read-only tools tying a session to a project, a call without result counted as proof) before any review. Rule: brief test agents to report defects with the failing input.
- Kind: SHORTCOMING. Facts: the plan review took six rounds because I sent plan text with unproven mechanisms (ownership by owner field, ACTIVE_CONFIRMED) that the code later did not implement as written. Rule: write the plan from what the code can read today.

- Kind: SUCCESS. Facts: S7-T6 tests assert the exact START line and that a fake secret in the prompt appears nowhere (UI, state, metrics). Rule: test privacy claims with a planted fake secret.

- Kind: SUCCESS. Facts: the runner output is byte-identical to a direct hook run for deny, allow and exit-2 cases (tests), found one real bug first (a dropped TextIOWrapper closed its buffer). Rule: keep references to wrapped streams.

- Kind: FAILURE. Facts: a literal backspace in the money regex (a  typed in a normal string) made every English money word unmatched; found by a test agent, not by me. Rule: scan new files for control characters.

- Kind: SUCCESS. Facts: the test agent drove 3 real defects out of the gate (English money words, a file listing counted as a failed try, a call without result counted as proof) by running the incident asks. Rule: test a gate with the incident text itself.

- Kind: SHORTCOMING. Facts: I first accepted an ExitPlanMode plan without looking at its result, so a plan the operator rejected counted as presented; the test agent caught it. Rule: every proof of presentation reads the tool result, not only the call.

- Kind: SUCCESS. Facts: the first render of the AEVE plan matched the plan counts exactly (14 accepted rows of 34 tasks). Rule: compare an adapter output with an independent count before wiring it in.

- Kind: FAILURE. Facts: two production hooks carried literal backspace characters in word-boundary regexes for weeks (English done/committed/pushed never matched), so the keep-going release in English never worked. Root cause: a non-raw string with backslash-b; no test used an English closing message. Layer that missed it: tests. Rule: every regex gets one positive test per alternative group.

- Kind: FAILURE. Facts: the S7 gates shipped with unlocked read-modify-write counters, a metrics call that could run icacls and a purge on every hook path, and a client flag read after the hook had removed it from argv; three independent reviewers found them, my own tests did not. Root cause: tests covered single calls, not two concurrent hooks or the time cost of bookkeeping. Layer that missed it: author and tests. Rule: every shared state file gets a lock test, and every bookkeeping step on the hook path gets a time budget.

- Kind: SHORTCOMING. Facts: secret_guard recorded evidence only for PostToolUse, but Claude Code sends a non-zero Bash exit to PostToolUseFailure, so failed credential checks were never recorded. Rule: read the event table of the host before wiring a hook to success only.

- Kind: SUCCESS. Facts: running the strict plan parser on all nine real PROJECT_PLAN.md files before committing showed ERP_MCP uses about a dozen own statuses; they were mapped explicitly instead of defaulting to todo. Rule: run a stricter parser on every real input before it ships.

- Kind: FAILURE. Facts: GPT-PM found that rotation evidence was matched by service only, so one verified key could unlock deleting another; my tests had one rotation at a time. Root cause: no concurrent-transaction case. Layer that missed it: author and tests. Rule: test two parallel instances of every transaction-shaped state.

- Kind: SHORTCOMING. Facts: the plan-first text proof used any shell call as the first change although the PreToolUse gate used is_mutation; the two gates disagreed. Rule: one predicate for one concept, shared by all gates.

- Kind: SUCCESS. Facts: the test agent found two further defects while writing the round 1 tests (add_record returned None so every write logged a false failure; a verification could attach to the wrong open rotation); both fixed before the commit. Rule: have the test author read the call sites of every changed helper.

- Kind: FAILURE. Facts: my round 1 fix let a single open rotation authorize deleting any bare key id, and a test pinned that unsafe behavior; GPT-PM called it a BLOCKER. Root cause: I treated uniqueness as identity. Layer that missed it: author and test agent. Rule: an irreversible action needs proof of identity of the target, never "only one candidate".

- Kind: FAILURE. Facts: my round 2 fix matched the delete against the credential the command authenticates with, so --key-file=a.json could unlock deleting key B; the test I had written pinned it. Root cause: two meanings of "credential" in one function. Layer that missed it: author, test agent. Rule: name authentication and target separately in any function that gates an irreversible action.

- Kind: FAILURE. Facts: I wrote "target id is the first 6 characters" as a known limit in the log and still shipped it; GPT-PM rejected it as a BLOCKER (two keys sharing a 6-character prefix). Root cause: a limit I recorded myself was a safety hole, not a convenience. Layer that missed it: author. Rule: a limitation noted in the log on an irreversible-action gate is a defect to fix before review, not a note.

- Kind: FAILURE. Facts: my own S7 tests wrote about 100 fixture rows into the operator's real metrics file, and I first read them as 24 real plan-first exceptions. Root cause: hook tests built their environment by hand without the metrics folder; the metrics tool had no way to tell test rows from real ones. Layer that missed it: author and tests. Rule: any test that runs a hook sets every output folder to a sink, and before calling a number in a metric a defect, look at the session ids and projects of the rows behind it.

- Kind: SHORTCOMING. Facts: two ledger tasks were committed together under one Task trailer, so the ledger could not verify the first. Rule: one commit per task, or add the second task by a note commit before verify.

- Kind: FAILURE. Facts: a path with a TAB (D: TAB mp) shipped in four test files; my own end-to-end test passed because it only counted fixture rows in the real folder. Root cause: escapes in a generated patch script; no check of the written bytes. Layer that missed it: author; GPT-PM found it. Rule: after any scripted edit of a path, grep the written bytes, and test that an isolation path is absolute.

- Kind: SUCCESS. Facts: S9-T1 put the self-service ladder in CORE.md, the one source that build.py copies into both the Claude CLAUDE.md and the Codex AGENTS.md. Why it worked: one source file, no per-client copy. Rule: put shared behaviour rules in CORE.md only.

- Kind: SUCCESS. Facts: S9-T2 added the /unblock command to both clients with one file, checked by listing the build output of both clients. Rule: check a shared source by listing both outputs.

- Kind: SHORTCOMING. Facts: S9-T3 as planned assumed Codex wiring and prose asks were missing; reading registry.json and out/codex/hooks.json showed both exist. Root cause: the plan was written before reading the gate. Layer that missed it: author. Rule: read the existing code before splitting a task.

- Kind: SHORTCOMING. Facts: S9-T4 helper was committed without being run once; only the tester will run it in /verify. Rule: the tester run decides, but read the code path of concurrent create once more before verify.

- Kind: SHORTCOMING. Facts: four S9 commits were made without the ledger post-commit hook installed (core.hooksPath empty in Harness_v2), so verify refused them. Root cause: the harness repo itself is not installed as a project. Layer that missed it: author. Rule: after the first commit of a task run ledger.py status and check the commit was recorded.

- Kind: SUCCESS. Facts: closed the OPERATOR loophole in the ledger by a fixed case list instead of free text. Rule: a gate on a free-text reason is not a gate; require a value from a closed list.

- Kind: SUCCESS. Facts: tester wrote 18 cases from the criteria and all passed first run with an isolated ledger. Rule: tests for a state file always point it at a temp path.

- Kind: FAILURE. Facts: secret_store and the ledger quote check shipped with a crash window and a one-apostrophe loophole. Root cause: I wrote them without running them once and without a failure-half-way thought. Layer that missed it: author and tests; python-reviewer found it. Rule: for any create-then-write step, answer "what if it dies in between" before commit.

- Kind: SUCCESS. Facts: three independent reviewers found the same two defects and the fix was confirmed by 48 tests. Rule: run reviewers on risky create-then-write code before the tester, not after.

- Kind: FAILURE. Facts: I ended the S9 turn with "Not verified and not done" plus a permission question for steps that were allowed, and used ledger block EXTERNAL to wait for my own agents; the operator pointed it out twice in the day. Root cause: the safe-looking habit of asking, no rule against it. Layer that missed it: author; no gate catches it. Rule: allowed means do it; ask only the operator-only cases.

- Kind: SHORTCOMING. Facts: an earlier message of mine asked the operator to approve hooks via /hooks, a command that does not exist in 0.167. Root cause: I answered from memory, not from the binary. Rule: before naming a CLI command to the operator, run --help or search the binary.

- Kind: FAILURE. Facts: a scripted edit silently did not apply and I reported the fix as done. Root cause: no grep after the replace. Layer that missed it: author; the tester caught it. Rule: after a scripted replace, grep the result before saying it is fixed.

- Kind: FAILURE. Facts: two turns after writing the stop-and-ask rule I ended a turn with a blocker table waiting for my own agent. Root cause: the rule was text only and I did not re-read it before ending the turn. Layer that missed it: author; no gate. Rule: re-read CORE.md anti-pattern list before every final message.

- Kind: FAILURE. Facts: two comments of mine in secret_store.py claimed no-overwrite and "no work tree to leak into" without checking POSIX rename semantics or the git-missing case; GPT-PM found both. Root cause: I wrote platform claims from memory, only Windows was run. Layer that missed it: author, three reviewers and the tester; GPT-PM caught it. Rule: a comment that claims a platform guarantee needs a test that forces that path.

- Kind: SUCCESS. Facts: forcing the no-link and no-git paths in child processes proved both GPT-PM findings fixed. Rule: force the platform path a comment claims, in a child process.

- Kind: FAILURE. Facts: my stale-lock recovery created a new race (paused owner), and my test asserted the fail-open git behaviour as correct. Root cause: I fixed the symptom GPT-PM named, not the whole class (any automatic recovery needs owner proof); the test copied my assumption. Layer that missed it: author, tester; GPT-PM round 2 found it. Rule: when a fix adds recovery or a fallback, list who else can hold the resource before choosing it, and never let a test assert the weak branch.

- Kind: SUCCESS. Facts: the tests now assert the strict contract in both findings and the old fail-open test was removed. Rule: when a review changes a contract, rewrite the test that encoded the old one first.

- Kind: SHORTCOMING. Facts: my round-2 exception for the default folder repeated the round-1 mistake (an assumed-safe location without proof). Root cause: I patched the named input, not the class "git unavailable". Layer that missed it: author and tester; GPT-PM round 3. Rule: when a check cannot run, refuse for the whole class, never add an exception.

- Kind: SUCCESS. Facts: the exact failing input from the reviewer is now a test. Rule: every review finding becomes a test with the reviewer's own input.

- Kind: FAILURE. Facts: S6 report source choice was either-or (ledger or plan), so a project with both showed 2 of 2 tasks as the whole program; the operator found it on the ERP report. Root cause: the S6 tests used projects with only one source. Layer that missed it: tests, then reviewers. Rule: when two sources can both exist, test the case where both exist.
- Kind: SHORTCOMING. Facts: the installer kept telling the operator to use /hooks after S9 had recorded that the command does not exist. Root cause: I changed the decision log but not the text printed by the tool. Rule: when a fact is corrected, grep the whole tree for the old claim in the same change.

## 2026-11-12 S10-T1: Fix /hooks reference in install_codex
- Kind: SUCCESS. Facts: removed /hooks command reference from install_codex.py NEXT line and docstring; two failing tests (test_t1_next_line_says_trust_all_and_not_hooks_command and test_t1_no_slash_hooks_command_in_tools_or_harness) now pass; all 10 tests in test_s10_report.py pass.
- Why it worked: the criterion was clear (no /hooks references), the test confirmed the defect, and the fix was straightforward text replacement.
- Rule: when tests clearly identify a defect, fix it directly without adding extra scaffolding.
- Kind: SHORTCOMING. Facts: my first S10 commit put the second Task: line in a separate paragraph and the ledger saw only one task. Rule: all Task: trailers go in one block.
