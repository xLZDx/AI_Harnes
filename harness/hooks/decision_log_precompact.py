#!/usr/bin/env python3
"""PreCompact hook -- reminds (never blocks) to bring core/DECISION_LOG.md
up to date right before the conversation gets summarized, whether triggered
manually (/compact) or by auto-compact. Operator GO, 2026-08-13.

Compaction is exactly the moment the "look back and understand why" problem
core/DECISION_LOG.md exists for gets hardest -- reasoning not yet on disk is
what the summary is about to compress away. There is intentionally no per-prompt decision-log reminder; this is the one low-frequency reminder point.

Never blocks: a hook that stalls compaction risks the exact context-overflow
situation auto-compact exists to prevent.

2026-09-30: this hook used to emit `hookSpecificOutput.hookEventName: "PreCompact"` with
`additionalContext` -- confirmed against the primary Claude Code hooks doc
(https://code.claude.com/docs/en/hooks.md) that this was never a valid PreCompact output shape.
`additionalContext` is documented only for SessionStart/SubagentStart, UserPromptSubmit/
UserPromptExpansion, PreToolUse/PostToolUse/PostToolBatch, Stop/SubagentStop, and PostModelSwitch --
PreCompact's only documented output is the universal fields (`systemMessage` among them) plus
blocking via exit code 2 or a top-level `decision`. There is no valid JSON shape that injects
context into the model before compaction from this hook event -- fixed to emit `{"systemMessage":
...}` instead, which is shown to the operator (not guaranteed to reach the model as context) and
keeps this hook's "never blocks" contract. This is a narrowing of what the hook can do, not a
restoration of its original intent -- that intent is not achievable via PreCompact at all.

Codex: the same reminder is written to stderr instead of a systemMessage JSON field, because Codex fails a
hook run on output fields it does not support (and it may not fire PreCompact at all; then this is a no-op).

Kill switch: CLAUDE_DECISION_LOG_PRECOMPACT=off
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _agentio  # noqa: E402  shared Claude Code / Codex adapter


def main():
    if os.environ.get("CLAUDE_DECISION_LOG_PRECOMPACT", "").lower() == "off":
        sys.exit(0)

    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            sys.exit(0)
    except Exception:
        sys.exit(0)

    cwd = payload.get("cwd") or os.getcwd()

    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode != 0:
            sys.exit(0)
        repo_root = result.stdout.strip()
    except Exception:
        sys.exit(0)

    log_path = Path(repo_root) / "core" / "DECISION_LOG.md"
    if not log_path.is_file():
        sys.exit(0)

    trigger = payload.get("trigger") or payload.get("matcher") or "unknown"
    context = (
        f"[Decision log reminder -- PreCompact, trigger={trigger}] This conversation is about to "
        "be summarized. core/DECISION_LOG.md already exists in this repo. Before the summary happens: if "
        "this session covered a non-trivial decision, piece of evidence, or refusal not yet logged, "
        "add it NOW -- reasoning not yet on disk is exactly what compaction is about to compress "
        "away, and the next session (or your post-compaction self) can only recover what's written "
        "down, not what was only in this context window."
    )
    # FIXED 2026-09-30 (confirmed against the primary Claude Code hooks doc,
    # https://code.claude.com/docs/en/hooks.md, not assumed): `hookSpecificOutput` with
    # `hookEventName: "PreCompact"` and `additionalContext` was never a valid PreCompact output
    # shape -- `additionalContext` is documented only for SessionStart/SubagentStart,
    # UserPromptSubmit/UserPromptExpansion, PreToolUse/PostToolUse/PostToolBatch, Stop/SubagentStop,
    # and PostModelSwitch. PreCompact's only documented JSON output is the universal fields
    # (`continue`/`stopReason`/`suppressOutput`/`systemMessage`/`terminalSequence`) plus blocking via
    # exit code 2 or a top-level `decision`. It cannot inject additionalContext into the model's
    # context at all -- there is no valid JSON shape that does what this hook was trying to do.
    # `systemMessage` is the closest available field (shown to the operator, not guaranteed to reach
    # the model as context) and keeps this hook's explicit "never blocks" contract intact -- this is
    # a narrowing of what the hook can actually do, not a bug fix that restores the original intent.
    if _agentio.client(payload) == "codex":
        sys.stderr.write(context + "\n")
    else:
        print(json.dumps({"systemMessage": context}))
    sys.exit(0)


if __name__ == "__main__":
    main()
