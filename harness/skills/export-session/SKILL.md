---
name: export-session
description: Export a Claude Code (or VS Code Copilot / Devin) session to one self-contained offline HTML file with a chat transcript, collapsible sub-agent cards and a token/cost summary. Use when the operator asks to export, archive or share a session, or to see its token cost.
---

# export-session

Imported from the Agentic Studio kit (`adk-export-session`), 2026-10-14. Origin: `D:\Downloads\core(2)\core\skills\adk-export-session`. Scripts are stdlib-only Python; no ADK runtime is required.

## Apply when
The operator asks to export/archive a session, or wants per-session token and cost numbers.

## Critical
Never read the session log yourself (it can be megabytes). Always go through the bundled script, which prints only a short summary.

## Method
`SCRIPT` = `~/.claude\skills\export-session\scripts\export_session.py`. Use the project interpreter, e.g. `C:\Python314\python.exe`.

1. Detect sub-agents:
   `python SCRIPT "<session-id | current | path>" --detect`
   `current` resolves to the newest transcript for the working directory. It prints `subagents=<N>`, `title=...`, `title_source=...` and one `agent` line per sub-agent.
2. If `title_source` is `first-prompt` or `project-name fallback`, pass a clean `--title "..."` on export.
3. If `subagents` > 0, include them by default (`--include-subagents`) unless the operator asked for the main conversation only (`--no-subagents`).
4. Export:
   `python SCRIPT "<id|current|path>" --output-dir "<dir>" --include-subagents`
   Options: `--name`, `--title`, `--max-output N` (default 4000 chars per tool output), `--format md|both` only if explicitly asked.
5. Report the absolute path, title, rounds and number of embedded sub-agents. Do not read the generated HTML back.

Other IDEs: `--collector vscode` or `--collector devin` (or `ADK_EXPORT_COLLECTOR`). See `references/architecture.md`.

## Known limits
- `scripts/pricing.py` is a hand-maintained price table (last verified 2026-07-20 by its authors); its Sonnet entry carries an introductory rate. Treat cost numbers as estimates and refresh the table before relying on them.
- ADK-specific features (cue sheets, `skill_registry`, `ADK_ROOT`, `ADK_EXPORT_AGENTS_YAML`) are optional; without those variables they resolve nothing and the export still runs.
- Not yet run on this machine; first use should be `--detect` on `current`.

## Do not
Read the session log inline; create a second output file unasked; publish the HTML anywhere (it contains the full conversation).
