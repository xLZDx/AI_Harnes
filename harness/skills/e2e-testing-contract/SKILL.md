---
name: e2e-testing-contract
description: Verify critical user journeys through real integration boundaries. Use when creating, running or stabilizing end-to-end tests.
---

# e2e-testing-contract

## Apply when
End-to-end journeys through real browser/API/DB boundaries, and failing or flaky E2E suites.

## Method
1. Choose a few high-value journeys (money, auth, data loss, core flow) over broad coverage.
2. Use real boundaries with seeded deterministic data and isolated state per test.
3. Wait on observable conditions, never fixed sleeps.
4. Classify each failure with artifacts (trace, screenshot, log, request ids): product bug / test defect / environment / flake.
5. Flake: reproduce N times; quarantine only with an owner, a ticket and an expiry. Verify cleanup.

## Required output
Journey list; run evidence (command, N runs, pass rate); classification of every failure.

## Do not
Retry until green; delete or skip a failing test to pass; widen waits to mask a product race.

Typical failure modes: `references/failure-modes.md` (read only when the work hits one of them).

Playbook for healing failing tests and deciding whether a case is automatable now: `references/heal-and-triage-playbook.md`.
