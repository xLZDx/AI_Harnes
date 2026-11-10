# Harness v2

One source for the Claude Code and Codex harness used across the operator's projects.
Nothing here is installed automatically: `tools/build.py` writes `out/claude` and `out/codex`,
and installing them into `~/.claude` / `~/.codex` is a separate operator decision.

## Layout

| Path | What it is |
|---|---|
| `harness/rules/CORE.md` | The only global rules file (becomes `CLAUDE.md` and `AGENTS.md`) |
| `harness/manifest.json` | Every agent and skill, where it came from, what changed, what was dropped |
| `harness/agents/` | 21 agents, all Sonnet, effort medium |
| `harness/skills/` | 24 skills, loaded by agents through `skills:` only |
| `harness/commands/` | `/plan`, `/next`, `/verify`, `/ship`, `/close` |
| `harness/routing.json` | Rules for choosing 0-4 reviewers per task |
| `tools/ledger.py` | Sprint and task ledger (`.harness/ledger.json` in each project) |
| `tools/route.py` | Deterministic reviewer router; `--history N` dry-runs it over past commits |
| `tools/report.py` | HTML report from the ledger and git; only changed sections are replaced |
| `tools/build.py` | Validates the source and builds `out/claude` and `out/codex`; `--install-project` wires a project |
| `tools/githooks/` | post-commit (ledger + report), pre-push (GPT-PM approval), post-merge (worktree cleanup) |
| `tools/worktree_gc.py` | Removes merged, clean scratch worktrees |
| `bench/` | Agent quality benchmarks: seeded defects, real pre-fix bugs, tester mutants, domain cases |

## Use in a project

```
python tools/build.py --install-project D:\Repo\<project>
python .harness-tools/ledger.py sprint-add --id S1 --title "..."
```

Project-specific reviewers and triggers go into `<project>/.harness/profile.json`
(same keys as `harness/routing.json`, plus `domain_specialists`).

## Kill switches

`HARNESS_SKIP_APPROVAL=1` (pre-push check), `HARNESS_WORKTREE_GC=off` (post-merge cleanup).
