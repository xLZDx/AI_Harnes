---
description: Plan a sprint - write its tasks with acceptance criteria into the ledger and show the plan once.
argument-hint: <sprint goal>
---
Follow skill `harness-flow`, step /plan.

1. Read the project's kickoff documents (requirements, design, definition of done). If the
   project has none yet, run `product-owner` and `technical-writer` (kickoff mode) first.
2. Split the goal "$ARGUMENTS" into an ordered list of tasks. Each task: id `S<n>-T<m>`,
   title, 1-5 acceptance criteria that a test can check, risk level
   (trivial | local | multi-module | risky) and whether it has a user journey.
3. Save: `python .harness-tools/ledger.py sprint-add --id S<n> --title "..."` then one
   `python .harness-tools/ledger.py task-add ...` per task (see `python .harness-tools/ledger.py -h`).
4. Show the operator the plan as one table (id, title, criteria, risk) and the counter from
   `python .harness-tools/ledger.py status`. Do not start coding in this command.
