# Heal and triage playbook

Adapted from the Agentic Studio kit (`adk-test-engineer`: healer, triage and planner prompts), 2026-10-15. Tool- and tracker-specific parts were removed; the method is generic for Playwright-style browser tests and similar suites. Load only when healing a failing test or deciding whether a case can be automated now.

## 1. Classify the failure before touching code

Read the failure output first. Classification before debugging avoids wasted browser sessions.

| Type | Symptoms | Fix path |
|---|---|---|
| A. Test data / fixture | "user/entity/profile not found"; error before any login or navigation; stack trace points at the data catalog, builders or schema validation | No browser needed. Find the closest matching catalog entry and substitute its identifier. If none matches, do not hand-author data: mark the test skipped-with-reason (`test.fixme`) and report the gap |
| B. Runtime / browser | error after login, during navigation, interaction or assertion; locator lookups, visibility checks, timeouts | Standard debugging, causes in priority order below |
| C. Product defect | the test is right and the product behavior changed or is broken | Not a test fix. Report as a product bug with the failing assertion |
| D. Flake | passes and fails without any change | Find the race; never retry until green |

Type B causes, in priority order:
1. **Stale selector**: the registry or page-object entry no longer matches the DOM. Fix it in the single place selectors live, not in the test body.
2. **Timing**: element not ready. Wait for a specific condition inside the page-object method. Never wait for network idle and never use fixed sleeps.
3. **Assertion drift**: expected text or value changed. Confirm against the requirement; update the expected value only if the product change is intentional, and record it.
4. **Precondition mismatch**: the state the test assumed no longer exists by default. Fix the setup, not the assertion.

## 2. Heal rules

- Maximum 3 attempts per failing test. After the third, mark it skipped-with-reason (`FIXME: heal exhausted after 3 attempts - <reason>. Last error: <message>`) and report it as unresolved.
- Never weaken or delete an assertion to make a test pass.
- Healing is tests-only. Never modify production code.
- Selectors live in one registry; tests and page-object methods consume them. No ad-hoc locators scattered through tests.
- Import the test fixture from the project's configured fixture module, not directly from the framework.
- Re-run only the single healed test after each attempt, then the owning spec once at the end.
- Output per run: healed (test, type, attempts), skipped-with-reason (test, reason), data gaps, one-line summary.

## 3. Automation triage: can this case be automated now?

Evaluate each scenario separately; one case at a time, never batch decisions.

| Check | Pass criterion | If failed |
|---|---|---|
| Steps are atomic | one action and one expected outcome per step | needs-rewrite |
| No mixed positive and negative verification | one happy, error or boundary path per case | needs-rewrite (split) |
| Preconditions stated | explicit, or inferable from the data catalog | needs-rewrite |
| Data identities exist | required role/tags/layout match an existing catalog entry | data-gap |
| Page-object coverage buildable | existing methods cover the steps, or the gap fits current patterns. Default to BUILDABLE; BLOCKER only if it needs a cross-module refactor that cannot be scoped to this change | if BLOCKER: page-object-gap |
| No exotic interaction | no drag and drop, multi-window, filesystem or async flow that cannot be automated reliably | not-automatable |

Outcome is `ready` or `blocked` with the reason. A blocked case is left for a human to rewrite; do not change its status yourself. If a case is probably automatable but uncertain, mark it ready with a needs-review note; a wrong guess is cheap because review catches it.

## 4. Splitting heuristics for existing manual cases

- Mixed positive and negative verifications: split.
- Several independent features in one case: split.
- Orthogonal data dimensions: one scenario per dimension value.
- Otherwise keep as one case, optionally reworded for clarity.
- Each scenario records: id, title, linked acceptance criterion, risk level, automation candidate, preconditions, steps, parameters/datasets, notes.
- Risk to priority: HIGH=1, MEDIUM=2, LOW=3 unless overridden.

## 5. Planner constraints

- Never execute tests while planning; never touch page objects or spec files in the plan stage.
- Never invent test data; record the missing profile in the scenario notes and continue.
- Greenfield with no test framework present: stop and report the missing framework instead of scaffolding from an unknown source.

## Tag vocabulary (optional, for trackers that support tags)
`triaged`, `needs-rewrite`, `not-automatable`, `data-gap`, `page-object-gap`, `needs-review`.
