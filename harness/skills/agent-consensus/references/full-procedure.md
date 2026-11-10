<!-- Verbatim copy of the pre-2026-10-02 SKILL.md; the thin front door is SKILL.md. -->

---
name: agent-consensus
description: Cost-aware evidence-based specialist review for non-trivial plans/changes. Selects the minimum relevant agents, runs one independent discovery round, reopens only contested BLOCKER/MAJOR items, and optionally uses an adjudicator. Use when the operator asks for agent review or when a high-risk cross-domain decision genuinely needs independent challenge.
---

# Agent Consensus — Evidence, not voting

The goal is to discover independent failure modes with the **smallest useful roster**. Agreement count is not truth; evidence is.

## 1. Classify before spawning

- **R0 trivial:** typo/comment/format/obvious one-line config -> 0 agents.
- **R1 focused, one domain:** 1 specialist.
- **R2 cross-file/cross-domain:** 2–4 specialists in parallel.
- **R3 high risk:** architecture, financial correctness, tenant isolation, security, destructive migration, production reliability -> every specialist whose domain the change actually touches, in parallel. That is typically 3–5, but the selector is domain intersection, not a target count; add one Devil's Advocate or adjudicator only after discovery.

Never use roster size as a quality metric. If one agent can settle the question with primary evidence, stop there.

## 2. Recon

Inspect the real files/scope first. Select agents whose domain intersects the actual change. Do not spawn agents merely because they exist.

For each selected agent pass only:
- task/decision under review;
- exact scope/files/artifacts;
- known constraints and accepted decisions;
- required output schema.

Do **not** include other agents' opinions in round 1.

## 3. Round 1 — independent discovery

Run selected agents in parallel. Require only actionable findings:

`ID | severity | claim | evidence | failure scenario | impact | required change | acceptance test`

Severities: `BLOCKER / MAJOR / MINOR / NIT`.

After results return:
1. verify load-bearing file:line or primary-source evidence;
2. reject/confidence-downgrade unsupported claims;
3. merge duplicates by root cause;
4. separate facts from hypotheses/preferences.

## 4. Round 2 — only where uncertainty remains

Reopen only:
- contested BLOCKER/MAJOR findings;
- a new BLOCKER/MAJOR whose evidence conflicts with another specialist;
- a cross-domain decision with materially different failure tradeoffs.

For each contested item, ask at most the owning specialist plus one relevant counter-role to review the **specific evidence and disagreement**. Do not respawn the full roster.

Default max = 2 rounds. A third round requires explicit operator request or genuinely new evidence.

## 5. Adjudication

Use one high-reasoning adjudicator when multiple serious findings remain. The adjudicator must:
- re-open the decisive evidence;
- classify each item `ACCEPT / MODIFY / REJECT / UNKNOWN`;
- explain conflicts without manufacturing consensus;
- preserve unresolved decisions for the operator.

Evidence order: measured behavior / authoritative artifact > primary docs > reproducible test > code inference > expert preference > agent vote count.

## 6. External second opinion

Codex or another independent model is an **escalation**, not mandatory per commit. Use it when:
- the operator explicitly asks;
- a BLOCKER/MAJOR remains unresolved;
- the change is high-risk and a genuinely independent model adds value.

One review round is the default. One rebuttal round is enough unless new evidence appears.

## 7. Final output

Return:
- risk class and agents used;
- verified BLOCKER/MAJOR findings first;
- accepted MINOR findings only if actionable;
- rejected/confabulated findings with reason;
- unresolved decisions;
- exact changes/acceptance tests required before GO.

Do not dump every agent transcript unless requested. Preserve raw findings in an artifact when an audit trail is needed.
