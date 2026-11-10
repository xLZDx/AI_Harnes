---
name: agent-evaluation
description: Measure agent quality and token efficiency with regression evidence. Use when changing agent prompts, routing or rosters, or claiming token savings.
---

# agent-evaluation

## Apply when
Changing an agent prompt, routing rule or roster, or claiming a token saving.

## Method
1. Build fixtures from real past defects with expected severity (seeded defects).
2. Run the roster before and after; record tokens/run, latency, confirmed BLOCKER/MAJOR, false positives, duplicates, unique findings, escaped seeded defects, unnecessary invocations.
3. Accept a change only if recall on seeded defects does not drop; prefer the cheapest roster that keeps detection.
4. Use the scripts in `bench/`: seeded-defect cases, the answer key and the scorer. Measure per run: defects found, severity of each, and tokens used.

## Required output
Metrics before/after and a keep/revert decision.

## Do not
Measure by the number of agents run; slim a prompt without a recall check; start separately billed model runs without operator authorization.

Typical failure modes: `references/failure-modes.md` (read only when the work hits one of them).
