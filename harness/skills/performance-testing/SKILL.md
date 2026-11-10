---
name: performance-testing
description: Measure performance before optimizing and guard regressions. Use for performance claims, hot-path changes and benchmark/profile work.
---

# performance-testing

## Apply when
A performance claim, a regression risk or a hot-path change.

## Method
1. Define workload and SLO from the product budget (percentiles, throughput, memory, I/O).
2. Baseline before changing anything on representative data; separate warm from cold; same machine.
3. Profile to find the dominant cost (algorithm, N+1, copies, locks, serialization) before proposing a fix.
4. Re-measure after with variance (N runs, report spread). Test saturation, soak and recovery for critical paths.
5. Guard with a regression benchmark or threshold in CI where it is stable.

## Required output
Baseline vs after table (percentile, sample size, method); every claim labelled MEASURED or INFERRED.

## Do not
Optimize cold code; claim a speedup from one noisy run; trade correctness or ordering for speed without tests.

Typical failure modes: `references/failure-modes.md` (read only when the work hits one of them).

SQL query tuning loop (capture, analyze, trial, baseline/diff verdicts, gates): `references/sql-perf-tuning-loop.md`.
