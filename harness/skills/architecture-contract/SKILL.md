---
name: architecture-contract
description: Review system architecture with consistent boundaries and operational criteria. Use for boundary, ownership, consistency or scalability decisions.
---

# architecture-contract

## Apply when
System- or module-level review/design: ownership, dependency direction, consistency, failure domains.

## Method
1. Map who owns each piece of state (single writer) and where invariants are enforced.
2. Check dependency direction against existing layering and CI-enforced rules; CI rules outrank opinion.
3. Check consistency and concurrency: ordering, idempotency, transactions, retries.
4. Check failure domains / blast radius and scalability (measured, or a stated assumption).
5. Check deployability, migration and rollback. Prefer existing boundaries; a new abstraction needs evidence of at least two real users.

## Required output
Findings as: severity | basis | claim | evidence | failure scenario | required change | acceptance test.

## Do not
Restyle or propose rewrites for taste; duplicate what CI already enforces; cite a pattern name instead of a concrete failure.

Typical failure modes: `references/failure-modes.md` (read only when the work hits one of them).
