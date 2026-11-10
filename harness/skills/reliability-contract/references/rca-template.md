# RCA template

Used by incident triage to render two outputs per run:

- `<project>/.triage/<ref>/rca.md` — Markdown for local review.
- `<project>/.triage/<ref>/rca.html` — HTML for a ticket system (and Confluence-friendly if needed).

## Template (Markdown source)

```
# RCA — <INC number or short slug>: <short_description>

**Incident:** [<INC number>](<servicenow-link>)
**State at triage time:** <state>
**Reported:** <opened_at>  ·  **Triaged:** <now>
**Affected service:** <cmdb_ci.name or "(not set)">
**Project:** <project>

## Symptoms
- bullet list

## Timeline
| Time (UTC) | Source | Event |
| ---------- | ------ | ----- |
| ...        | ...    | ...   |

## Root Cause
<one paragraph — the candidate_root_cause from triage, refined>

## Contributing Factors
- bullet list of secondary causes (config drift, missing alert, etc.)

## Suspect Changes
| Type   | Reference      | Confidence | Summary |
| ------ | -------------- | ---------- | ------- |
| commit | [<sha>](...)   | high       | ...     |
| deploy | [<build>](...) | medium     | ...     |

## Recommended Remediation
- ordered list of concrete actions, owner-suggested where possible

## Prevention
- bullet list of forward-looking measures (alerts, tests, runbook updates)

## Evidence
- Clickable links to log queries used (Datadog / Dynatrace / Splunk)
- Reference to cached log files in `<project>/.triage/<ref>/work/logs/`

## Gaps
- bullet list of what couldn't be confirmed (only if non-empty)
```

## HTML rendering

For the `.html` variant, render the same structure with `<h1>/<h2>/<table>/<ul>` — no Confluence-specific macros. Keeping the HTML neutral lets the same body work in a ticket system and Confluence storage format without per-target conversion.

## Link conventions

For the markdown variant, render all external references as **clickable markdown links** per the project link rules — commits, log-system queries, the ticket record and work items.

## Sectioning rules

- Omit `## Gaps` entirely if `triage-result.gaps` is empty.
- Omit `## Contributing Factors` if there are no secondary causes; do not leave the heading with just "(none)".
- The 5-line summary printed to the user before the human gate is composed from: `Incident`, `Root Cause` (one-liner), top `Suspect Changes` row, top `Recommended Remediation` bullet, top `Prevention` bullet.
