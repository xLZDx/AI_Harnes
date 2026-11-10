---
name: functional-testing
description: Design or review behavioral tests that can falsify the changed contract. Use when writing tests or judging whether tests prove a change.
---

# functional-testing

## Apply when
Writing or reviewing behavior tests for a changed contract.

## Method
1. Name the contract: inputs -> observable outcome and state change. Expected values come only from examples in the spec or an independent formula, never computed in your head or copied from the code under test.
2. Cover the matrix that applies: happy, alternate, boundary, invalid, permission, repeated/retry, concurrent, state after restart.
3. Every assertion must fail if the behavior breaks - mutate or revert the code (mentally or for real) to check.
4. Keep tests isolated: no order dependence, no shared mutable fixtures, deterministic clocks and ids.
5. Use the real boundary where a mock would hide the defect.

## Required output
Coverage map contract -> tests; ranked gaps; for each weak assertion the mutation that survives it.

## Do not
Count 'did not raise' as proof; assert implementation details instead of outcomes; accept coverage percentage as evidence.

Typical failure modes: `references/failure-modes.md` (read only when the work hits one of them).
