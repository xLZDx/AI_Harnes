---
name: harness-flow
description: The one development flow of this harness - sprint and tasks, /plan, /next, /verify, /ship, /close, with evidence rules and a status line after every action. Use when planning a sprint, picking up the next task, verifying, shipping, closing, or when asked for status or "continue".
---

# harness-flow

Two planning levels only: a sprint and the tasks inside it. Epics exist only as labels that group tasks; they are never a unit of work. One task at a time, in ledger order. State lives in `.harness/ledger.json`, changed only through `python .harness-tools/ledger.py`.

## /plan
1. Read the project manifest and existing documents; do not rewrite settled ones.
2. Write the sprint and its ordered tasks, each with acceptance criteria, into the ledger with `python .harness-tools/ledger.py`.
3. Show the plan to the operator once. After that, work without asking again; resolve ordinary technical questions yourself and put non-blocking items in the backlog.

## /next
1. Pick the next pending task from the ledger. Show a 3-7 step plan for it.
2. Implement with `implementation-engineer`: the whole task, no tests, no documentation.
3. Commit. The message is at most two sentences plus a last line `Task: <ID>`. Commit before any push.

## /verify
1. `functional-test-reviewer` writes and runs tests from the task's acceptance criteria and run instructions, not from the diff. Expected values come only from spec examples or an independent formula.
2. `e2e-runner` adds one scenario per acceptance criterion when the task has a user journey.
3. The task's own tests run once per task. The whole own set (and the whole e2e set) runs once at sprint end.
4. Never run full suites, lint, typecheck or verify scripts. Never weaken a test to get green; fix the code.
5. A bug found by a test gets a regression test and a fix, then the task's own tests run again.

## /ship
1. Run `python .harness-tools/route.py --task <id>` to choose reviewers (0-4 by risk). Launch them as real agents with the diff file, the changed-file list and the acceptance criteria. Apply `agent-consensus`.
2. Fix every accepted BLOCKER/MAJOR in one batch and record `python .harness-tools/ledger.py review <id> --result accepted --findings <n>`.
3. Intermediate task (other tasks of the sprint are not done yet): stop here, no GPT-PM, no push; go to `/next`.
4. Final task (every other task of the sprint is done): run `gpt-pm-gate` once for the whole sprint diff, rounds until APPROVE bound to the HEAD commit.
5. Only after APPROVE, push the sprint branch and open or update the single PR for the sprint. A new commit invalidates the approval.

## /close
1. `technical-writer` updates existing documents to match the code.
2. Add a retrospective entry to `core/LESSONS_LOG.md`: kind (FAILURE / SHORTCOMING / SUCCESS), facts, root cause, which layer caught it, the rule for next time.
3. Run `python .harness-tools/report.py` to build the report (see `html-report`).

## Evidence rule
- Never say done, fixed or passes without a command run in this same run and its output.
- Record exact command, git head and result; keep implemented, reviewed, runtime-proven and `NOT_RUN` separate.
- Anything not run is `NOT_RUN` with the reason. Attempted is not succeeded; skipped is not passed.
- Evidence must be able to fail: a broken behavior has to turn it red. Never infer absence from truncated output; a reviewer's claim is not your measurement.
- Before declaring a task closed, check every acceptance criterion against the real diff, the test output and git state.

## Status line after each action
`Task <ID> | state | evidence (command, head, result) | what remains | next step`

## Continuing
- Classify open items: DONE, DOABLE-NOW, or BLOCKED. BLOCKED only for: the operator must decide or act, something external is unavailable (name the verified symptom), or the item itself needs a forbidden action. Uncertainty, size or "needs review" is DOABLE-NOW.
- While any DOABLE-NOW item exists, do it; do not stop to hand over recommendations. A blocked part is recorded and independent parts continue.
- Irreversible actions (unrecoverable deletion, real money) are the only operator-only gates.

## Do not
Start the next task or sprint in parallel; add extra phases, approval tokens or state machines; let an implementer approve its own change; simulate agents in chat.
