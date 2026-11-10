---
name: sprint-close
description: Close a sprint with accurate documents, release notes and a measured sprint summary taken from git and the ledger. Use at /close, when a sprint report or retrospective input is needed.
---

# sprint-close

## Apply when
The sprint PR is pushed or the sprint ends, and documents, release notes or a sprint summary are needed. Documentation never blocks commit, push or merge.

## Documents
1. Take facts only from code, the ledger's accepted tasks and their acceptance evidence; verify commands, paths, flags and endpoints against the source.
2. Update existing documents only where the code made them stale. TDD: requirement -> task -> acceptance -> test map; design summary; interfaces; failure modes; rollout/rollback.
3. ADR when a decision needs one: context, decision, consequences, alternatives rejected.
4. Release notes: what users can now do, changes in behavior, known limits, migration steps.
5. Write for the stated reader. All text committed to git is English.

## Sprint summary
1. Take the changed set from git (log, diff stat, branch, HEAD) and task state from `.harness/ledger.json`, not from chat history.
2. Report: goal vs outcome; tasks done/carried/rejected; commits and the PR; test evidence (exact commands, results, `NOT_RUN` reasons); review findings by severity with duplicates merged; agents and models actually run; open risks and blockers with verified cause.
3. Label every number MEASURED (from a tool result) or INFERRED; never present a reviewer claim as your own measurement.
4. Build the report with `python .harness-tools/report.py` (see `html-report`); the report is the single layout for program and sprint status.

## Retrospective
Add an entry to `core/LESSONS_LOG.md` per `harness-flow` /close: kind, facts with evidence, root cause, which layer caught it, rule for next time. At most three concrete process actions; apply process fixes to skills or agents, not to chat memory.

## Required output
Changed document paths; stale documents found but not changed, with reason; sprint table: task, state, evidence, remaining, next action.

## Do not
Document unimplemented behavior as done; invent commands; rewrite settled documents without cause; count agents or tokens as quality; report green without the test result; hide failed or skipped items.
