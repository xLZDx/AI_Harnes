<!-- Verbatim copy of the pre-2026-10-02 SKILL.md; the thin front door is SKILL.md. -->

---
name: codex-second-opinion (reference for agent-consensus)
description: Optional read-only Codex second opinion for unresolved major findings, high-risk changes, or explicit operator requests. Not a mandatory commit/push gate.
---

# Codex Second Opinion

Use only when independent-model evidence can change a decision. Default to one round; allow one rebuttal round for a still-contested BLOCKER/MAJOR, then surface the disagreement instead of looping.

## Invocation

Run the maintained wrapper from the installed Claude config tree with the available Python launcher, for example on Windows:

```text
py -3 "$HOME/.claude/tools/codex_review.py" --cwd <repo> --uncommitted
```

Other supported scopes are `--base <branch>` and `--commit <sha>`. One sequential call only -- no parallel/fan-out mode exists (removed 2026-08-21, operator instruction): Codex is a single reviewer having one flat exchange with Claude, never multiple concurrent instances of itself.

## Manual mode (added 2026-08-20, accessibility accommodation)

The default above stays automatic for every gate in every session -- this does not replace it.
For an operator who wants to drive one specific round by hand (typing/clicking is
difficult right now; they'd rather paste into their own already-open chat tab than wait
on `codex exec`), the same wrapper has a two-step manual path that writes the identical
receipt shape, so `codex_review_gate.py` accepts it exactly like an automated round:

```text
py -3 "$HOME/.claude/tools/codex_review.py" --cwd <repo> --uncommitted --print-prompt
#   -> prompt lands on the clipboard. Operator pastes it into their own chat, sends it.
py -3 "$HOME/.claude/tools/codex_review.py" --cwd <repo> --uncommitted --ingest-response --final
#   -> operator copies the reply first; this reads the clipboard, parses it, writes the receipt.
```

`--response-file <path>` reads the reply from a file instead of the clipboard, when that's
easier. This is a human-triggered, one-message-at-a-time path -- there is no automated
send and no automated fetch anywhere in it, which is exactly the line that keeps it
legitimate (see `~/.claude/CLAUDE.md` "Codex Consensus Review" §13): a person decides
every single send. Do not wire `--print-prompt`/`--ingest-response` into anything that
loops without a human in between -- that recreates the automated bridge that was
explicitly declined.

Confirmed 2026-08-21 from the operator's own OpenAI usage screen: "Usage is shared across
Codex, Work, Workspace Agents, and ChatGPT for Excel. It doesn't include Chat conversations."
Chat (the tab the operator pastes into by hand) draws from a separate pool than Codex CLI --
this manual round-trip does not compete with or exhaust the Codex quota. That is a usage-pool
fact, not a Terms-of-Use one: it says nothing about whether scripting the Chat tab itself would
be permitted, and does not reopen or weaken the automated-bridge decline above.

## Rules

- Codex is not a panel seat (removed 2026-08-21, operator instruction): never feed it another reviewer's or agent panel's findings to react to. It reviews the diff cold, on its own; reconcile its findings with Claude's own view afterward, one-on-one -- this rule is about Codex specifically and does not restrict Claude's own internal agent-review panels.
- Codex is read-only; the wrapper uses Codex sandbox `read-only` and never edits or authorizes Git operations.
- Verify every accepted finding against the cited file/line yourself.
- Label unsupported citations `CONFABULATION`; do not silently discard them.
- `SILENT` is not agreement and majority is not proof.
- If Codex is unavailable, report `skipped(<reason>)`; do not block unrelated work.
- Do not spend more rounds on settled MINOR/NIT items.
- If Codex's response is its own usage-limit message ("You've hit your usage limit... try again at `<date>`"), the wrapper auto-detects this and forces the receipt final on that same call -- do not retry in this session. Report `skipped (usage limit, resets <date>)` and move on; retrying before the stated date just reproduces the same message.
