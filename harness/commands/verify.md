---
description: Verify the current task with independent tests written from its acceptance criteria.
argument-hint: [task id]
---
Follow skill `harness-flow`, step /verify.

1. Take the task's acceptance criteria and run instructions from the ledger. Do not give the
   tester the diff.
2. `functional-test-reviewer` (or `tdd-guide` for logic and calculations) writes and runs the
   task's tests once. If the task has a user journey, `e2e-runner` adds one scenario per
   criterion. You never run tests yourself.
3. A failing test that is a product bug goes back to `implementation-engineer`; a wrong test
   goes back to the tester. Re-run only the affected tests.
4. Record: `python .harness-tools/ledger.py verify <id> --result pass|fail --by <test agent role> --evidence "<command and summary>"` (a pass is refused unless a finished run of that test agent after the task commit is recorded).
   Anything not run is `NOT_RUN` with the reason.
5. Prohibited: full suites, lint, typecheck, verify scripts, mutation runs.
