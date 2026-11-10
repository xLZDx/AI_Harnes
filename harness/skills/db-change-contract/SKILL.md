---
name: db-change-contract
description: Review or design schema, SQL, migration and backfill changes so invariants, locks, plans and rollback are proven. Use when a task touches DDL, queries, indexes, transactions, row-level security, data or format migrations.
---

# db-change-contract

## Apply when
Schema/DDL, SQL, migrations, indexes, transactions, locks, row-level security, backfills and format conversions.

## Method - schema and SQL
1. Invariants are expressed as constraints (PK, FK, unique, check, not null), not only in application code.
2. Multi-statement invariants: transaction scope, isolation level, lock order, long-lock risk.
3. Query plans (EXPLAIN) at representative cardinality; account for index write cost.
4. Rollout is backward compatible while old and new code overlap (expand/contract).
5. Check RLS/permissions and backfill volume; tie every performance claim to a plan or workload.

## Method - migrations and backfills
1. Expand -> backfill -> verify -> contract; never combine them in one step.
2. Batch with explicit, resumable checkpoint state; reruns are idempotent.
3. Check locking and online-ness; plan for partial deployment (old and new code together).
4. Verification queries (counts, checksums, sampling) before and after; dry-run on a copy.
5. Prove the rollback or restore path before running; expose progress and rate.

## Required output
Findings with evidence (DDL/SQL/plan) and a rollout order. For migrations, a runbook: preconditions, steps, verification queries, abort/rollback, ownership.

## Do not
Judge performance without a plan; accept a migration with no lock or rollback analysis; trust ORM defaults for concurrency; run destructive steps (drop, truncate, delete) without separate operator approval and a proven restore path.

## References
- `references/failure-modes.md` - schema/SQL defect classes (read only when the work hits one).
- `references/migration-failure-modes.md` - migration and backfill defect classes.
- For SQL tuning measurement discipline and baseline/diff verdicts see `../performance-testing/references/sql-perf-tuning-loop.md`.
