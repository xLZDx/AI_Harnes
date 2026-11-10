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
        sys.stderr.write("codex_spawn_shim: usage: <project root> <interpreter> <hook script> [args...]; spawn denied\n")
        return 2  # a broken install fails closed, like a missing project hook would under Claude
    root, interpreter, script, *rest = argv
    raw = sys.stdin.buffer.read().decode("utf-8-sig", errors="replace")
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = None
    payload = json.dumps(to_claude(data), ensure_ascii=True) if isinstance(data, dict) else raw
    env = dict(os.environ, CLAUDE_PROJECT_DIR=root, PYTHONUTF8="1")
    try:
        proc = subprocess.run([interpreter, script, *rest], input=payload.encode("utf-8"), capture_output=True,
                              env=env, cwd=root, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        sys.stderr.write(f"codex_spawn_shim: project hook {script} timed out after {TIMEOUT}s; spawn denied\n")
        return 2
    except OSError as e:
        sys.stderr.write(f"codex_spawn_shim: cannot run project hook {script} with {interpreter}: {e}; spawn denied\n")
        return 2
    pre_tool = isinstance(data, dict) and data.get("hook_event_name") == "PreToolUse"
    sys.stdout.buffer.write(codex_safe(proc.stdout) if pre_tool else proc.stdout)
    sys.stderr.buffer.write(proc.stderr)
    return proc.returncode


TIMEOUT = 20  # below the 30 s build.py writes for project spawn hooks


def codex_safe(out: bytes) -> bytes:
    """Codex fails the whole hook run (and lets the call continue) on output fields it does not support. Keep a
    deny; turn 'ask' into a deny; drop allow / updatedInput / systemMessage (allow is the default anyway)."""
    try:
        obj = json.loads(out.decode("utf-8-sig")) if out.strip() else None
    except ValueError:
        return out
    if not isinstance(obj, dict):
        return out
    hso = obj.get("hookSpecificOutput") if isinstance(obj.get("hookSpecificOutput"), dict) else {}
    decision = hso.get("permissionDecision")
    if decision in ("deny", "ask") or obj.get("decision") == "block":
        reason = hso.get("permissionDecisionReason") or obj.get("reason") or "denied by the project spawn hook"
        return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                  "permissionDecisionReason": str(reason)}}).encode("utf-8")
    ctx = hso.get("additionalContext")
    if ctx:
        return json.dumps({"hookSpecificOutput": {"hookEventName": hso.get("hookEventName") or "PreToolUse",
                                                  "additionalContext": str(ctx)}}).encode("utf-8")
    return b""


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
