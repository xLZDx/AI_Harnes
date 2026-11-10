# Export Session — architecture, capture support & branded output

> Developer reference for `adk-export-session`. The runbook lives in `SKILL.md`;
> this file documents the three-stage pipeline, per-target capture support, the
> branded output format, and how to add a collector for a new IDE.

## Architecture (extending to other IDEs)

A three-stage pipeline, so a new IDE only needs a new collector:

```
collector_<ide>.py  reads the IDE's logs  ─┐
  detect() / collect() / store_root()      ├─►  IR (ir.py)  ─►  generator_html.py  ─► .html
  / resolve_session()                      │    IDE-agnostic     generator_md.py    ─► .md
                                           ─┘    JSON model
```

- `ir.py` — the IDE-agnostic Session IR contract (**frozen**; the schema is
  documented at the top of the module).
- `generator_html.py` / `generator_md.py` — consume only the IR; never touch
  disk or IDE specifics (**frozen**). Add an IDE == add a collector; the
  generators are reused unchanged.
- `collector_claude_code.py` (+ `cc_parse.py`) — the Claude Code store → IR.
  Builds the recursive sub-agent tree, computes per-agent tokens, and owns
  `store_root()` + `resolve_session()` for its store.
- `usage_attribution.py` — per-workflow-run attribution (see below). One shared
  algorithm; each collector supplies its own boundary-detection adapter.

Edit those modules to tweak behavior — not this SKILL.md.

## Per-workflow usage attribution

Session-grain usage cannot answer "what did this workflow run cost", so each
collector also segments its own entry stream at the skill/workflow invocation
boundaries its host records, and the IR carries the result as `workflow_usage`.
Studio Live's `/api/usage` passes it straight through (it already imports this
collector), and the Backstage Usage tab IS that view: one table grouped by
workflow, each master row opening to show the run's own share plus every agent
it delegated to.

The split is deliberate: `usage_attribution.py` owns the algorithm, each
collector owns only the marker knowledge. An adapter emits an ordered stream of
`boundary(workflow)` / `usage(tokens, model, cost)` events; the shared pass folds
usage into whichever segment is open, aggregates per run and per workflow, and
raises `ReconciliationError` unless `sum(runs) + unattributed` equals the session
totals token-for-token and cost-for-cost. Adding a host means adding an adapter.

Properties worth preserving: segmentation is by POSITION in the log, never by
time (multi-day and interleaved sessions both fall out for free); each
invocation opens its own run, so two runs of one skill are never merged; spend
attributable to no invocation lands in a positioned ad-hoc segment of its own;
and a delegate's spend is charged to the run that delegated it. Fully
deterministic — no model in the path.

A run ENDS one of two ways: the flow declares itself done — a `completes_run`
glob in the skill's Studio Live cue sheet matching a file the run wrote — or the
next invocation opens. There is deliberately no idle rule: a run left open
because the user stopped for the night is still that workflow's spend.

**Governed workflows measure exactly; ad-hoc skills estimate.** Only a skill
that ships a cue sheet can declare its own end, so a non-kit skill's run closes
at the next invocation and may carry spend that happened after its real finish.
That asymmetry is expected — it is the difference between a workflow the kit
governs and one it merely observes.

Cost is priced PER ENTRY at the model that entry used, never at the session's
first model: sessions switch models, and pricing an aggregate at one rate
overstated one real transcript's main thread by 18.9%. Each main-conversation
detail row is therefore split per model, so every detail row can be recomputed
at the rate it names.

Skill names, where a detector needs to recognize one, come from
`core/agents.yaml` via `skill_registry.py` — never a list embedded in this
feature, so a new skill is detectable with no code change here. That file is not
copied into any plugin payload, so a runtime without the checkout and without
`$ADK_ROOT` simply falls back to each host's own evidence rather than to a stale
embedded list; `skill_registry.py` documents which detector relies on it and why.

The markers each collector detects, and the real-log evidence for them, are
documented at the top of the collector that owns them.

## Capture support per target

The store path for each supported target is resolved by its collector, not
named here (see "never hardcode a store path" above).

| Target | Collector | Support | Notes |
|---|---|---|---|
| Claude Code | `claude-code` | Yes | Full: usage, cost, and recursive sub-agents. |
| VS Code (Copilot) | `vscode` | Yes | Flat transcript + tool calls. The store carries no token usage (cost shows "—") and no sub-agents. |
| Windsurf | — | No (not feasible) | Its chat store is an opaque/encrypted binary with no published schema; no readable per-session transcript. |
| Devin | `devin` | Yes (local ACP) | Local desktop app writes a real per-session transcript (`~/.../Devin/User/acp-events/*.ndjson`, ACP NDJSON). Flat transcript + tool calls; no token usage (cost shows "—") and no sub-agents. |
| Codex | — | No (not feasible) | No per-session transcripts are written locally. |

Collectors emit the same frozen IR, so any store that yields a per-session
transcript can be added later by writing a sibling collector (which owns its
own store resolution) — the generators are reused unchanged.

## Branded output (Raymond James)

The HTML opens with a **usage summary header** so an export reads like a report,
not a raw log: a navy masthead (Agentic Studio lockup, title, model, time span,
session id) that condenses to a 44px pinned bar on scroll;
**metric cards** with whole-session totals (main transcript plus every
sub-agent) — tokens, input, output, cache reads/writes, and estimated list-price
cost; a **meta strip** (model, duration, rounds, sub-agent count); an **output
produced** section (lines written, files created/edited, Bash commands,
side-effects); and a **per-row breakdown** that reconciles to the totals, each
sub-agent row deep-linking to its card. Brand assets (Source Sans Pro woff2 +
the Agentic Studio lockup and glyph) are embedded from `scripts/brand_assets.py`
so the file stays self-contained; if that module is absent the export still
renders with system fonts and no mark. That module is *generated* — edit
`tools/brand-kit/` and run `scripts/generate_brand_assets.py`, never the module
itself.

Caveats: thinking tokens are billed as `output_tokens` with no separable
breakdown; "lines written" is a gross proxy; dollar cost is shown only for
models whose list price is known (others show "—").
