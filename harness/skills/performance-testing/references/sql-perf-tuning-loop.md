# SQL performance tuning loop

Adapted from the Agentic Studio kit (`adk-tune-sql-query` and `adk-data-engineer/perf-tuning`), 2026-10-15. Vendor tooling (their MCP tools, run directories, ticket system) was removed; the measurement discipline and the baseline/diff verdict model are kept. Load when tuning a SQL query or stored routine against a real database. Deep schema/lock/index design stays with `database-reviewer`.

## Phases

0. **Scenario**: a versioned scenario file describing the target (schema, routine or SELECT), the binds/sweep cells and a per-cell SLA. Default SLA 60 s only if unspecified, marked "confirm with team".
1. **Validate**: the target is in the scenario allowlist; compute the scenario file sha256.
2. **Production gate**: any run that touches production (even read-only metadata) needs explicit operator approval once per session. Non-production runs of an unchanged, already-approved scenario need no prompt.
3. **Capture**: run cells one at a time, serially.
4. **Analyze**: per cell, from the captured artifacts.
5. **Trial**: verify each hint or index recommendation by measurement, at most 3 rounds per cell.
6. **Diff against a baseline** (optional).
7. **Recommend**: write a recommendation-only patch; never apply it automatically.
8. **Save baseline** (optional).

## Capture rules

- Serial only. Parallel cells contend for parallel-query slots and produce starvation plans that hide the real signal.
- Print progress per cell (`cell i/N, SLA`, then status, elapsed, query id, plan hash).
- Resume-safe: a cell with an existing result file is skipped.
- A failed cell does not stop the run. Three consecutive timeouts stop it: the timeout is probably mis-set. A credential or driver-init failure on the first cell aborts immediately; a bind mismatch aborts without looping.
- Capture the ACTUAL plan in the same execution as the rows. Never execute twice to get a plan.

## Analyze rules

- Read the whole artifact (full monitor XML, actual-plan XML or cursor plan). Do not sample.
- Bottleneck statements must carry per-operator evidence: operator, starts, actual vs estimated rows, bytes read, time. "Full scan at op N" without numbers means the data-extraction step was skipped.
- Pull physical metadata just in time: table stats freshness, index definitions and fragmentation, partition strategy, last DDL, missing-index groups, query history. If a privileged variant is denied, fall back to the unprivileged one; if both fail, record the missing grant and continue with partial evidence (`succeeded_partial`), never abort the cell.
- Prefer the executed plan over the estimated plan; prefer the plan artifact over a text cursor plan when both exist.
- SQL Server specifics to extract from the actual plan: physical and logical operators, estimated vs actual rows (summed across threads), seeks vs scans, key-lookup storms, warnings (spill to tempdb, plan-affecting convert, missing statistics, memory grant), granted vs used memory, degree of parallelism, parameter sniffing (compiled vs runtime value). Grants needed: SHOWPLAN and VIEW SERVER STATE.
- Pattern: a nested-loops join with very high starts, forced by existing hints, with a cardinality misestimate on the inner side suggests replacing the forced loop with an explicit join order and hash join; measure it in the trial loop before recommending it.

## Trial rules

- At most 3 rounds per cell. A recommendation without a trial is labelled INFERRED, not MEASURED.
- A hint that changes semantics, or any DDL (including in-memory or compression changes), is a hard gate: recommendation only, applied by the owner.
- An aggregate recommendation not covered by a direct trial gets one verification trial per uncovered bucket, with operator confirmation of the scope.

## Baseline and diff model

- A baseline records: tag, run id, scenario hash, scenario name, capture time, git sha (null if not a repo), environment.
- Refuse to diff if the scenario hash differs between baseline and candidate. Match cells on (scenario hash, cell id); unmatched cells are reported but never block.
- Per matched pair, one verdict: IMPROVED, REGRESSED, NEUTRAL or PLAN-CHANGED-VERIFY, computed from elapsed delta, plan hash equality, bottleneck/hint changes, pruning, parallelism and starvation transitions.
- Overall verdict and exit code: 0 PROMOTE (no REGRESSED, no unverified plan change); 1 DO-NOT-PROMOTE (any REGRESSED); 2 PROMOTE-WITH-CAUTION (no REGRESSED but an unverified plan change); 3 ERROR (missing baseline, hash mismatch, malformed baseline, missing notes).
- Diff reads only the per-cell notes, never the raw artifacts again, so it is deterministic and fast.
- Saving a baseline refuses to overwrite an existing tag without an explicit overwrite flag.

## Gate memoization

- Plan approval: store the scenario sha256 and approval time per scenario in a committed state file; an unchanged hash skips the prompt. The team-shared memo only works if the directory is not git-ignored.
- Production approval: a session-scoped flag that dies with the shell. A stale approval must never authorize a later production run.

## Per-engine differences (summary)

| Aspect | Oracle | Redshift | SQL Server |
|---|---|---|---|
| Scenario kind | table function with call wrapper | plain SELECT | plain SELECT |
| Plan source | monitor XML plus cursor plan | EXPLAIN of the query text | actual-plan XML, captured in the same round trip |
| Recommendations | hints (LEADING, INDEX, FULL, USE_NL, PARALLEL) | distribution/sort keys, vacuum, analyze, compression | OPTION hints (RECOMPILE, HASH/LOOP JOIN, MAXDOP, FORCE ORDER, OPTIMIZE FOR), table hints, index and statistics DDL |
| Explain safety | EXPLAIN PLAN | analyze blocked in production | SHOWPLAN estimate only, never executes |

## Required output
MEASURED/INFERRED-labelled table per cell (before, after, SLA, plan hash), the overall verdict with exit code, and the recommendation-only patch.

## Do not
Run cells in parallel; analyze a sample of a plan; apply a patch or DDL automatically; compare runs of different scenario hashes; reuse a production approval across sessions.
