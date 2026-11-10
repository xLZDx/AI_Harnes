---
name: html-report
description: Use when the operator says "дай репорт"/"give me a report" or requests sprint/program status; create the evidence-backed RU+EN dashboard with progress bars and the active-sprint task table.
---

# HTML report — front door

## Harness v2 build rule (overrides the hand-authoring instructions below)

- The report is built by `python .harness-tools/report.py` from `.harness/ledger.json` and git; never hand-write the HTML. Read the ledger and git for every number.
- After a commit only the changed section (the affected sprint bar and task row) is redrawn; do not rebuild the whole report.
- Hooks, `report_conform.py` and `project_plan.py` from the old setup are not part of this harness; use only `.harness-tools/report.py`.

Invoke whenever the operator asks "дай репорт", "покажи отчёт", "give me a report", or equivalent. Treat that wording as an explicit request for the current project’s Russian and English HTML report; infer the active project and sprint from current in-scope state instead of asking which report. A routine status question that does not ask for a report may be answered in chat unless an accepted project requirement requires an artifact.

Required output:
- reports/<NAME>.ru.html — Russian, published/linked to the operator.
- reports/<NAME>.html — English durable in-repo record.
- Same structure/content, localized language.
- Real <header> element, project/folder/git provenance immediately below it, and a copy-whole-page control.
- Every planned task shows Planned / Definition of Done / Status / remaining Tail if partial.

## Plain-language sprint and task statuses

- Keep sprint/gate status separate from child-task status. A sprint marked `IN_PROGRESS` does not mean every task is in progress. Show the sprint/gate count separately from task fractions.
- When exact task states are evidenced, show counts with their denominator, for example `11/73 delivered · 1/73 in progress · 61/73 not started`, and explain why later tasks are not started when the ordered queue proves it.
- When commits identify only a sprint or gate, show a numeric lower bound such as `≥1/N tasks with linked Git changes (minimum; exact task IDs not linked)`. Explain that the number is not an exact task count. Do not label it exactly `1/N in progress`, infer which task changed, or call the rest not started without queue evidence. Make the bar and legend show that only the minimum is confirmed.
- For source-only work, use plain language such as `N/N tasks have source changes; runtime/acceptance evidence is separate`. Never make source presence look like accepted closure.
- Use `NOT_STARTED` only when the ordered task queue supports it. If neither task-ID evidence nor queue evidence determines a task state, say in plain language that its individual status is not evidenced; do not replace that with a fabricated count or a bare technical label.
- Explain every status in the report legend or nearby note. Keep gate acceptance, task delivery, source changes, runtime evidence, and unverified task mapping as distinct measures.

## Pushed-closure percentage (operator 2026-10-30, mandatory in every report)

- Every report, including the chat summary, states one headline number first: `X% of the project is closed and pushed` = tasks delivered AND present on the remote (pushed) / total tasks in the accepted plan, with numerator, denominator, the remote ref and its commit hash, and the as-of date.
- "Closed" here means the task commit is reachable from the remote tip (verify with `git fetch` then `git merge-base --is-ancestor <task commit> <remote ref>` or `git branch -r --contains`). Committed-but-unpushed, source-only or in-progress work is shown as separate categories and never enters the numerator.
- If the push state cannot be checked (no remote, offline), write that the pushed percentage is not verified instead of estimating it.
- Show this number in the top hero, next to the overall bar; keep the gate-registry percentage separate and labelled as gates.

## Golden reference (operator 2026-10-30) — always compare against it

Reference pair, saved from the operator-approved PDCC program status report (source `PDCC_PROGRAM_STATUS_2026-10-30`, anchor `#gate-charts`):

- `golden/GOLDEN_PROGRAM_STATUS.ru.html` (in this skill folder) (primary, Russian)
- `golden/GOLDEN_PROGRAM_STATUS.html` (English)

Before writing any report, open the golden pair and match its structure: top `<header>` hero "whole program", overall bar, sprint bars (`sprint-charts`), gate bars (`gate-charts`), the full active-sprint task table with exact-ID Git window and line shares (code/docs/tests/other), then gate details. After writing, compare section order and columns against it; any deviation needs a stated reason. Copy the layout and the kinds of evidence, never its numbers, dates, task IDs or revisions. Known defect NOT to copy: it still uses bare `IN_PROGRESS; ` labels; always translate them into plain text with a numerator and denominator. Do not edit the golden files without the operator's explicit request.

## Legend, formulas and denominators (required in every report)

- Put a visible colour legend directly beside each chart, and repeat it before the detailed tables if the chart is far away: green = accepted/delivered; dark green = source complete but acceptance still pending; blue = currently in progress; amber = explicitly deferred/blocked; neutral gray = confirmed not started/open in the ordered queue; hatched gray = individual task state not evidenced. State in text what each colour counts. Do not reuse a colour for a different state within the same report.
- Program closure formula: `accepted tasks / total planned tasks × 100%`. Count a whole sprint rollup in the numerator only when the source of truth explicitly says `CLOSED_REGISTRY`; label that basis. Never count partial, in-progress, source-only, or unverified tasks as accepted. If the complete task inventory is unavailable, do not invent a task denominator: show registry gate counts as a separate metric.
- Sprint task segments use one common denominator: `each evidenced task-state count / accepted-plan task total`. Show delivered, in progress, not started, source-only, deferred, and unverified as separate segments as applicable. Counts must reconcile to the denominator; hatch only the unverified portion. Use `0/N started` only when the ordered queue proves that state and no source/Git evidence contradicts it.
- Gate formula: `passed gates / registered gates × 100%`; never substitute a gate denominator for a task denominator. Explain which registry supplies the count.
- State the meaning of every denominator, the as-of date, source revision, and timezone. Do not copy numbers from reference screenshots.
- A lower-bound bar (`≥1/N`) must be hatched or labelled "minimum" so it cannot read as exact progress. Name exact task IDs only when commit/evidence linkage proves them.
- Separate four evidence levels in the legend: implemented, reviewer-checked, confirmed by a run, and `NOT_RUN`. Implementation or source work is never shown as gate acceptance.
- For duration, use exact-ID commit history only: `first tagged committer timestamp` to `last tagged committer timestamp`; for active work, `as-of timestamp − first tagged timestamp`. For completed work, `last − first`. Display timezone. These are Git activity windows, not labour hours; never infer timestamps from untagged parent commits.
- For work-share ratios, classify added+deleted lines from commits explicitly tagged with the exact task/gate ID into mutually exclusive `code`, `documentation`, `tests`, and `other` buckets. Formula per bucket: `(bucket added lines + bucket deleted lines) / total classified added+deleted lines × 100%`. Show `NR` only when no exact linked diff supports a value; otherwise show the measured ratio and raw counts in detail/tooltips. These ratios are diff-line proxies, not time spent or productivity.

## Program and sprint dashboard order

For a report covering a multi-sprint program, use this order before explanatory prose:

1. A real top-of-page `<header>` hero with the heading `Whole program — overall closure and work status`, a visible program-level progress bar, numerator, denominator, percentage, status distribution, as-of date, and source revision. Use a task denominator only when the complete program task inventory is evidenced. If only a gate registry is available, label it explicitly as gates (never as tasks), show its state distribution separately, and mark its registry snapshot stale when appropriate. Partial/in-progress work is not fully closed and must not enter the closure numerator.
2. A task progress bar and task total for every sprint, with the denominator derived by summing that sprint's task counts in the accepted plan. For a sprint explicitly marked `CLOSED_REGISTRY`, show the sprint-level rollup as `N/N CLOSED` (the accepted plan's full task count); label it as registry-based sprint closure and preserve any closure-basis caveat. This rollup does not claim each child's separate DoD was independently recorded. Keep `CLOSED_SOURCE_ONLY` distinct from full acceptance (for example `SOURCE_CLOSED_ONLY / N`). For `IN_PROGRESS`, show the sprint itself in blue and state that this is sprint/gate status, not every child-task status. When task-level evidence is exact, show numeric counts such as `11/N delivered · 1/N in progress · 61/N not started`. When only gate/sprint-linked Git activity exists, show a lower bound such as `≥1/N tasks with linked Git changes (minimum; exact task IDs not linked)`; explain that the exact task count and identities are unknown, and hatch only the portion whose individual states are unverified. Do not use display labels such as “partial source”, “task states not itemized”, or bare `IN_PROGRESS; statuses not split`. Show gate counts separately and explain that source/doc changes do not equal gate acceptance. Use amber `0/N started` only when registry/ordered-queue evidence supports it and there is no contrary source/Git activity. Use `NOT_RECORDED / N` only for an actual missing task-status record, not as a substitute for a known closed sprint or recorded work. Keep gate progress separately.
3. Immediately below the final sprint bar, one full task table for the currently active sprint. Include every task in that sprint with its concrete status, acceptance meaning, evidence, remaining acceptance, dependency, owner, next action, first and last committer timestamps for commits explicitly tagged with that exact task ID, the Git timestamp window, and the code/docs/tests/other changed-line share. State timezone and the classification basis. These are commit-history intervals and change ratios, not continuous labor hours. Do not infer a task timestamp from an untagged parent commit. For a queued task, show NOT_STARTED only when the ordered queue proves it; for a task with no exact task-ID commits and no queue proof, show `NO_TASK_ID_COMMITS` and no fabricated duration/share.
4. All remaining program gates, evidence details, definitions, caveats, and historical context.

Keep the active-sprint table adjacent to the sprint chart so the operator sees it before lower report details. Derive the active sprint and its task set from current in-scope project records, and label the source revision/as-of date. When the active sprint changes, rebuild this section for the new sprint and remove the former sprint from the active position; retain it only in historical details if useful. Do not leave stale task statuses presented as current.

The provenance/copy block is produced by `.harness-tools/report.py`.

## Reusable report-generation prompt

Use this prompt for Codex, Claude, or another report-writing agent:

> Build or refresh the project's self-contained Russian and English HTML program dashboard from current in-scope source records. Start with a real `<header>` hero titled “Whole program — overall closure and work status”, program closure bar, evidence-backed numerator/denominator/percentage, status distribution, as-of date, and source revision. Use `accepted tasks / total planned tasks × 100%`; partial, active, source-only, and unverified work is not closed. Count a sprint rollup only when the source explicitly says `CLOSED_REGISTRY`, and label that basis. If the task inventory is incomplete, do not invent its total; report registered gates separately as `passed / registered × 100%`. Next show a bar for every sprint using one accepted-plan task denominator. Put a visible legend beside the charts: green accepted/delivered, dark green source-only, blue in progress, amber explicitly deferred/blocked, gray confirmed not started/open, hatched gray unverified. State what each color counts and use the same mapping throughout. Show exact task fractions when evidenced; otherwise label only a lower bound such as `≥1/N tasks with linked Git changes (minimum; exact task IDs not linked)`, hatch the unverified part, and do not invent task identities/statuses. Use `0/N started` only when the ordered queue proves it and no Git/source evidence contradicts it. Keep gate counts separate from task counts and explain each denominator. Immediately below the final sprint bar, show every task in the active sprint with ID, acceptance, concrete status, evidence, remaining acceptance, dependency, owner, next action, exact-ID first/last committer timestamps, Git activity window, and code/docs/tests/other changed-line shares. For active duration use `as-of − first exact-ID commit`; for completed duration use `last − first exact-ID commit`; state timezone and that this is not labor time. Calculate each work-share bucket as `(added + deleted lines in bucket) / (total classified added + deleted lines) × 100%`; classify exact-ID commits into mutually exclusive code/docs/tests/other buckets and retain raw counts in details. Use `NR` only when no exact linked diff supports a value. Do not use untagged parent commits to infer task dates or work. Keep gates, source changes, acceptance, runtime evidence, and `NOT_RUN` distinct. Never copy screenshot numbers or invent times, approvals, task counts, or test results. Keep RU/EN reports equivalent and preserve provenance/copy controls; put gate details after the active-sprint task table.

Handover:
- show only the Russian artifact/link plus its full local path and a short substance summary;
- keep the English file as the durable repository record;
- PM/orchestrator mode: report is a checkpoint, continue the authorized program;
- program mode may defer intermediate reports when the operator explicitly authorized one final report.

Read references/full-2026-10-02.md only for edge cases, legacy mode semantics, hook kill-switch history, or exact historical rationale.
