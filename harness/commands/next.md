---
description: Take the next pending task, show its short plan, implement it and commit.
argument-hint: [task id]
---
Follow skill `harness-flow`, step /next.

1. `python .harness-tools/ledger.py next` (or the id in "$ARGUMENTS") gives the task and its criteria.
   `python .harness-tools/ledger.py start <id>`.
2. Show a 3-7 step plan for this task only, then act on it without waiting.
3. Delegate the code to `implementation-engineer` (task, criteria, files to touch). For
   unfamiliar code run `code-explorer` first with one concrete question.
4. Inspect the diff yourself. Commit: message at most two sentences, last line `Task: <id>`,
   plus a one-line entry in `core/LESSONS_LOG.md`.
5. Report: task, state, evidence (commit sha), what remains, next step (`/verify`).
