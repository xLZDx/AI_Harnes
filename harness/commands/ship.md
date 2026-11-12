---
description: Review the task by risk, then at sprint end get GPT-PM approval, push and open the sprint PR.
argument-hint: [task id]
---
Follow skills `harness-flow`, `agent-consensus`, `gpt-pm-gate`.

1. `python .harness-tools/route.py --task <id>` builds the diff from the task's commits, writes it to
   `.harness/diff/<id>.diff`, prints `diff_path`, the reviewers and why, and stores the choice in the
   ledger. Launch exactly those reviewers in parallel with that `diff_path`, the `files` list and the
   task's criteria.
2. Apply the consensus rule, fix accepted BLOCKER/MAJOR findings, re-run only affected tests,
   record: `python .harness-tools/ledger.py review <id> --result accepted --findings <n> --by <reviewer roles, comma separated>` (refused unless those reviewer agents finished after the task commit).
3. Intermediate task (other tasks of the sprint are not done yet): stop here, no GPT-PM, no push;
   go to `/next`.
4. Final task (every other task of the sprint is done): one GPT-PM review of the whole sprint diff
   (skill `gpt-pm-gate`), rounds until APPROVE.
   `python .harness-tools/ledger.py approve --request-id <PM Bridge request id>` (checked against the real GPT-PM reply: bare APPROVE for this HEAD). A new commit removes the approval.
5. `git push` (the pre-push hook checks the approval), then create or update the sprint's one PR.
