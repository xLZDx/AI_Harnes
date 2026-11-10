#!/usr/bin/env python3
"""
Session IR — the IDE-agnostic intermediate representation.

Every collector (Claude Code today; Windsurf/Cursor/etc. later) emits this
exact shape. Generators consume *only* this — they never touch disk or know
which IDE produced the logs. This is the contract that makes the renderer
multi-IDE: add an IDE == add a collector, the generator is untouched.

Schema (plain JSON-able dicts):

session = {
  "title": str,
  "title_source": str,            # custom-title | ai-title | project-name fallback
  "source_ide": str,              # e.g. "claude-code"
  "model": str | None,            # primary model id seen in the main transcript
  "started": str | None,          # ISO timestamp
  "ended": str | None,            # ISO timestamp
  "turns": [Turn, ...],           # main-conversation timeline
  "agent_count": int,             # total sub-agents discovered (incl. nested)
  "tokens": Tokens,               # totals for the MAIN transcript only
  "workflow_usage": WorkflowUsage, # per-workflow-run attribution (usage_attribution.py)
}

WorkflowUsage = {                 # see usage_attribution.attribute()
  "runs": [Run, ...],             # one per detected invocation, in log order
  "workflows": [WorkflowAgg, ...],# per-workflow aggregate, costliest first
  "unattributed": Bucket,         # spend belonging to no workflow run
  "totals": Bucket,               # runs + unattributed (reconciled)
  "reconciled": bool,
}
Run = {"seq": int, "workflow": str, "run": int, "source": str,
       "tokens": Tokens, "cost_usd": float | None,
       "headline_tokens": int, "events": int,
       "detail": [Detail, ...]}      # where the money went inside the run
WorkflowAgg = Run-shaped, with "runs": int instead of seq/run/source
Bucket = Run-shaped, without the run identity fields
Detail = {"kind": "main" | "agent",  # the run's own share, or one delegate
          "label": str | None,       # agent type
          "desc": str | None,        # what the agent was sent to do
          "model": str | None, "turns": int | None,
          "run": int,                # which run of the workflow (aggregates only)
          "tokens": Tokens, "cost_usd": float | None,
          "headline_tokens": int, "events": int}

Turn = {
  "role": "user" | "assistant",
  "ts": str | None,
  "blocks": [Block, ...],         # only assistant turns carry tool/agent blocks
  "is_summary": bool,             # True for the resumed-session continuation summary
}

Block (one of):
  {"t": "text", "text": str}
  {"t": "tool", "name": str, "summary": str, "command": str | None,
   "input": str | None,            # full tool input, pretty-printed JSON, capped at max_output
   "input_truncated": bool,
   "output": str | None, "output_truncated": bool, "offloaded_path": str | None,
   "lines": int,                   # approx lines written (Write/Edit/MultiEdit, and Bash
                                   #   heredocs under SHELL_EXT); absent/0 otherwise
   "question": {"text": str, "options": str, "answer": str} | None}
  {"t": "agent", "agent": Agent}

Agent = {
  "id": str,                      # agent-<hash>
  "type": str,                    # agentType from meta.json
  "desc": str,                    # description from meta.json
  "ts": str | None,
  "duration_s": float | None,
  "model": str | None,
  "tokens": Tokens,
  "turns": [Turn, ...],           # the sub-agent's own timeline (recursive)
  "cost_usd": float | None,       # None when the model's pricing is unknown
}

Tokens = {"input": int, "output": int, "cache_read": int, "cache_creation": int}
  headline()  -> fresh input (input + cache_creation) + output
"""


# The lines_by_ext bucket for lines authored through the shell rather than the
# write-family tools. Not a file extension: a shell command does not reliably
# name its target -- matching a redirect against a path also matches
# `subprocess.run(` and `re.sub(` -- so these carry one honest bucket instead of
# a per-extension split guessed from command text. Lives here rather than in a
# collector because every surface that renders lines has to recognise it, and
# generator_html is deliberately collector-agnostic.
SHELL_EXT = "(shell)"


def empty_tokens():
    return {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}


def add_tokens(acc, usage):
    acc["input"] += usage.get("input_tokens", 0) or 0
    acc["output"] += usage.get("output_tokens", 0) or 0
    acc["cache_read"] += usage.get("cache_read_input_tokens", 0) or 0
    acc["cache_creation"] += usage.get("cache_creation_input_tokens", 0) or 0


def add_tokens_from(acc, tokens):
    """Fold one Tokens dict into another (add_tokens folds a raw *host* usage
    dict; this folds an already-normalized IR Tokens dict). Used by the
    workflow-attribution pass, which sums Tokens many times over."""
    for field in ("input", "output", "cache_read", "cache_creation"):
        acc[field] += tokens.get(field, 0) or 0


def headline_tokens(tokens) -> int:
    """The intuitive 'size' of a turn/agent: fresh input + output.
    Cache reads are excluded from the headline (they re-read the same context
    every turn and would inflate the number ~100x); they still count in cost."""
    return tokens["input"] + tokens["cache_creation"] + tokens["output"]


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(n)


def fmt_cost(usd) -> str:
    if usd is None:
        return ""
    if usd < 0.01:
        return "<$0.01"
    return f"${usd:,.2f}"


def fmt_duration(seconds) -> str:
    if seconds is None:
        return ""
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"
