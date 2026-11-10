---
description: Review the task by risk, then at sprint end get GPT-PM approval, push and open the sprint PR.
argument-hint: [task id]
---
Follow skills `harness-flow`, `agent-consensus`, `gpt-pm-gate`.

1. `python .harness-tools/route.py --task <id>` builds the diff from the task's commits, prints the reviewers and
   why, and stores the choice in the ledger. Launch exactly those reviewers in parallel with the
   diff path, changed files and criteria.
2. Apply the consensus rule, fix accepted BLOCKER/MAJOR findings, re-run only affected tests,
   record: `python .harness-tools/ledger.py review <id> --result accepted --findings <n>`.
3. When this is not the last task of the sprint, stop here and go to `/next`.
4. Last task: one GPT-PM review of the whole sprint diff, rounds until APPROVE.
   `python .harness-tools/ledger.py approve --evidence "<GPT-PM verdict reference>"`. A new commit removes the approval.
5. `git push` (the pre-push hook checks the approval), then create or update the sprint's one PR.
