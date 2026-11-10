---
name: reliability-contract
description: Prove retries, timeouts, queues, workers and recovery behave under partial failure, and that failures are observable in production. Use when a task touches retries, queues, locks, background jobs, async delivery, logs, metrics, traces or alerts.
---

# reliability-contract

## Apply when
Retries, timeouts, queues, workers, async delivery, restart/recovery, partial-failure semantics, and behavior that must be diagnosable in production.

## Method - reliability
1. Every external call has a timeout; retries are bounded with backoff and jitter and apply only to idempotent operations.
2. At-least-once delivery means idempotency keys or deduplication.
3. For each step ask what state remains if it fails, and who completes or rolls back.
4. Queues: poison messages, dead-letter handling, backpressure, bounded concurrency.
5. Kill the process between each pair of steps; check graceful shutdown and resource exhaustion (memory, fds, connections, disk).
6. Silent loss is a defect: failures must be operator-visible.

## Method - observability
1. Structured logs at failure-prone boundaries with correlation/request ids; no secrets or PII.
2. Metrics: rate, errors, latency, saturation, queue depth and age of the oldest item.
3. Traces across async hops; detection of stuck work, not only errors.
4. Operationally critical failures get an alert with an owner and a runbook.
5. Verify by forcing a failure and locating it from telemetry alone.

## Required output
Failure matrix: step x failure -> resulting state -> recovery -> test.
Signal table: failure -> signal -> alert -> owner/runbook; gaps.

## Do not
Add retries to non-idempotent writes; swallow errors to stay up; call a design resilient without a crash-window analysis; log secrets/PII; alert on noise; call coverage "observable" without the forced-failure check.

## References
- `references/failure-modes.md` and `references/observability-failure-modes.md` - defect classes (read only when the work hits one).
- `references/rca-template.md` - RCA report template for incident write-ups.
