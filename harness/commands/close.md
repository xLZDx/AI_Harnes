---
description: Close the sprint - update documents, write the retrospective and rebuild the HTML report.
---
Follow skills `harness-flow` and `sprint-close`.

1. Ask the tester agents to run their whole own test set once; record the result.
2. `technical-writer` (update mode) updates the existing documents to match the code. No new
   documents.
3. Add the retrospective to `core/LESSONS_LOG.md`: what failed, what worked, which layer caught
   it, the rule for next time. Add a short PR description to `core/DECISION_LOG.md`.
4. `python .harness-tools/ledger.py sprint-close <id>` and `python .harness-tools/report.py`.
5. Give the operator the report path and the next sprint's first step.
