#!/usr/bin/env python3
"""Run a project's Claude subagent-spawn hook for Codex without changing the project script.

Usage (in <project>/.codex/hooks.json, written by tools/build.py --codex-project):
    python -s codex_spawn_shim.py <project root> <interpreter> <hook script> [args...]

Codex calls a subagent spawn "spawn_agent" with tool_input.agent_type and tool_input.message; Claude calls it
"Agent" with tool_input.subagent_type and tool_input.prompt. The shim rewrites a Codex PreToolUse payload into the
Claude shape, sets CLAUDE_PROJECT_DIR, runs the project hook, and passes its stdout, stderr and exit code through.
Any other payload (UserPromptSubmit, other tools) is passed through unchanged.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys


def to_claude(data: dict) -> dict:
    if data.get("tool_name") != "spawn_agent":
        return data
    ti = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    new_ti = {k: v for k, v in ti.items() if k not in ("agent_type", "message")}
    if "agent_type" in ti:
        new_ti["subagent_type"] = ti["agent_type"]
    if "message" in ti:
        new_ti["prompt"] = ti["message"]
    return {**data, "tool_name": "Agent", "tool_input": new_ti}


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        sys.stderr.write("codex_spawn_shim: usage: <project root> <interpreter> <hook script> [args...]\n")
        return 1  # non-blocking error: a broken install must not block every spawn silently, it is logged by Codex
    root, interpreter, script, *rest = argv
    raw = sys.stdin.buffer.read().decode("utf-8-sig", errors="replace")
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = None
    payload = json.dumps(to_claude(data), ensure_ascii=False) if isinstance(data, dict) else raw
    env = dict(os.environ, CLAUDE_PROJECT_DIR=root)
    proc = subprocess.run([interpreter, script, *rest], input=payload.encode("utf-8"), capture_output=True,
                          env=env, cwd=root)
    sys.stdout.buffer.write(proc.stdout)
    sys.stderr.buffer.write(proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
