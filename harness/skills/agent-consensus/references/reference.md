# Agent Consensus — Reference Material

Loaded on demand from `SKILL.md`. Contains the full Round 0 JSON schema, output artifact template, anti-patterns, and detailed cost notes.

## Round 0 JSON schema (full)

The recon agent MUST return a single JSON block matching this schema, no preamble:

```json
{
  "plan_tier": "T2" | "T3" | "T4",
  "tier_rationale": "<one sentence>",
  "domains_touched": ["python", "ml", "risk", "data", "security", "ops", "ui", "..."],
  "files_real": ["<actual paths the plan modifies>"],
  "files_referenced": ["<paths it reads but does not modify>"],
  "relevant_agents": [
    {"name": "security-reviewer", "tier": "A", "reason": "<why>"},
    {"name": "python-reviewer",   "tier": "B", "reason": "<why>"}
  ],
  "skip_agents": [
    {"name": "flutter-reviewer", "reason": "no Flutter surface"}
  ],
  "known_objection_matches": ["KO-010", "KO-012"],
  "cross_project_boundaries": ["VPS", "Hetzner", "Vast.ai"]
}
```

## Output artifact — final report template

```
# Agent Consensus Report — <plan title>
Date: <UTC date>
Status: consensus | partial_consensus
Rounds executed: N / 4

## Plan summary
<one-paragraph recap of the plan>

## Agent roster consulted (N agents)
- agent-name-1: <one-line specialty framing>
- agent-name-2: ...
... (all of them, no curated subset)

## Round-by-round audit trail

### Round 1 — Discovery
| Agent | BLOCKER | MAJOR | MINOR | NIT |
|---|---|---|---|---|
| ... | ... | ... | ... | ... |

### Round 2 — Cross-Review
| Agent | AGREE | DISAGREE | REFINE | NEW |
|---|---|---|---|---|
| ... | ... | ... | ... | ... |

### Round 3+ — Convergence
| Agent | Declaration | Remaining concerns |
|---|---|---|
| ... | consensus reached | — |
| ... | remaining concerns | <text> |

## Consensus items (confirmed by >=quorum agents)
1. <finding> — confirmed by: <agent list>
2. ...

## Unresolved items (if partial_consensus)
1. <finding> — positions:
   - agent-A: <position>
   - agent-B: <position>

## Recommendation for operator
<final action items + confidence level>
```

## Cost notes (after Phase 1+2 optimizations)

| Tier | Typical calls | Worst case |
|---|---|---|
| T1 | 0 (skip) | 0 |
| T2 | 3 | 3 |
| T3 | 5-10 | 14 |
| T4 | 17-23 | 60 |

Stacked reduction levers:
- **Recon-first:** -30-50% triage (eliminates "just-in-case" agents).
- **Tiering:** -95% on non-T4 plans.
- **Skip-round-2 gate:** -100% round 2 calls on ~60% of plans.
- **Narrowed round 2:** -50% of round 2 calls on the remaining 40%.
- **Brevity instruction:** -2-3× tokens per round 2-3 call.
- **Findings cache (KNOWN_OBJECTIONS.md):** -30-50% round 1 verbosity.

## Anti-patterns

- Round 1 only, then present to operator. Defeats the purpose.
- Hiding round-2 disagreements from operator in the final report.
- Stopping early because "agents seem to agree" without all three stop conditions met.
- Re-using a prior consensus report for a different plan (consensus is plan-scoped, not session-scoped).
- Letting one agent's silence count as AGREE. Silence = unresolved; re-prompt or mark `contested`.
- Subjectively second-guessing the Round 0 recon roster (add/drop agents). If recon got it wrong, fix the prompt and re-run round 0.
- Spawning the full round-1 roster again in round 2. Narrow to overlapping-domain agents only.

## Cross-reference

Mandated by Multi-Agent Plan Review to Consensus rule:
- `~/.claude/CLAUDE.md` — user-home global (canonical)
- `<your-projects-volume>/CLAUDE.md` — volume-level rules for a group of projects

Findings cache:
- `<project>/core/KNOWN_OBJECTIONS.md` — pre-confirmed objections (KO-001..KO-060+); fed to round-1 agents to skip re-derivation.

Memory:
- `~/.claude/projects/<your-project>/memory/feedback_agent_consensus_loop.md`
