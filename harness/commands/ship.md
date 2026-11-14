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
   (skill `gpt-pm-gate`), rounds until APPROVE. If PM Bridge does not answer: turn it on yourself
   (`pm_bridge_mode_on`), retry once, then keep the work as local commits, start the next sprint task and return
   to this review when `pm_bridge_health` says OK. Never push without the real APPROVE.
   `python .harness-tools/ledger.py approve --request-id <PM Bridge request id>` (checked against the real GPT-PM reply: bare APPROVE for this HEAD). A new commit removes the approval.
5. `git push` (the pre-push hook checks the approval), then create or update the sprint's one PR.
6. Merge it yourself, without asking the operator (operator 2026-11-14), only when the ledger approval is bound to the PR head (`gh pr view --json headRefOid`
   equals the approved HEAD). A newer head, a conflict or no approval means no merge: review again first. Never enable auto-merge; never force-push or
   delete remote branches. Procedure, every command's exit status is checked:
   a. `gh pr merge <N> --merge --match-head-commit <APPROVED_SHA>`; if GitHub refuses because the head moved, stop and get a new review.
   b. `gh pr view <N> --json state,autoMergeRequest`, up to 3 reads. `state` MERGED is the goal: only then update the main checkout, install and close the sprint.
   c. If an auto-merge request is pending (merge-queue branch): run `gh pr merge <N> --disable-auto`, check its exit status, read the PR again; the cancel is
      confirmed only when `autoMergeRequest` is empty (or `state` is MERGED at the approved head). Until it is confirmed do not install or close the sprint.
   d. If the PR is neither MERGED nor free of a pending request after 3 reads, or the cancel cannot be confirmed: this is an unresolved safety condition.
      Run `ledger.py block <ID> --class OPERATOR --cause "merge of PR <N> pending or auto-merge not cancelled"` and tell the operator at once; install and close nothing.
