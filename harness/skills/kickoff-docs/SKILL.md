---
name: kickoff-docs
description: Turn an idea into testable requirements, a sprint task list with acceptance criteria and a definition of done, and a one-page technical design. Use at project or sprint kickoff, before /plan, or when a non-trivial change needs agreement before coding.
---

# kickoff-docs

Two planning levels only: sprint and tasks. Epics are labels that group tasks, never a unit of work. No stories. These are the only kickoff documents; do not create more after kickoff, and do not rewrite settled documents.

## Requirements
1. Read existing docs, code and decision logs first; extract current behavior as FACT with file references.
2. Identify goal, scope and non-goals; separate FACT, ASSUMPTION and UNKNOWN.
3. Write each requirement as one testable statement with id `REQ-nnn`, source and priority; give non-functional requirements numbers.
4. List open questions with an owner; resolve routine ones from evidence instead of asking.
5. Check: no requirement is ambiguous, duplicated or untestable; each maps to an acceptance criterion.

## Sprint tasks and definition of done
1. Split requirements into tasks small enough for one commit and one test run, each tracing to `REQ` ids. An optional epic label groups related tasks.
2. Give every task numbered Given/When/Then criteria `AC-n` that a test can falsify. These are what `/verify` tests are written from.
3. Definition of Ready: requirement traced, criteria testable, design inputs available, dependencies known.
4. Definition of Done: code committed with a `Task: <ID>` line, the task's own tests green (written by the test agents), reviewer consensus, GPT-PM APPROVE for the HEAD once per sprint, documents updated at `/close`.
5. Order tasks by value, risk and dependency and state the rule. One sprint goal; one PR per sprint.
6. Enter the sprint and tasks into the ledger with `python .harness-tools/ledger.py` (see `harness-flow`).

## Technical design (non-trivial changes only)
1. State problem, constraints and current behavior with file references.
2. List real options (at least two) and the decisive criterion between them.
3. Specify the chosen design: interfaces, data/state ownership, concurrency, failure modes, compatibility, migration and rollback.
4. Map the verification plan to the risks; list open decisions with an owner.
5. Output is a one-page note: Goal, Non-goals, Design, Failure modes, Rollout/rollback, Test plan, Unknowns.

## Required output
Requirements list, sprint goal, ordered tasks with acceptance criteria, DoR/DoD, and the design note when needed. All text committed to git is English.

## Do not
Invent requirements; write solutions as requirements; hide unknowns or uncertainty; design beyond the approved scope; add tasks outside the sprint goal; reorder silently.

Design failure modes: `references/design-failure-modes.md` (read only when the work hits one of them).
