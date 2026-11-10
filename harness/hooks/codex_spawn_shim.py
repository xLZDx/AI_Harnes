#!/usr/bin/env python3
"""Run a project's Claude subagent-spawn hook for Codex without changing the project script.

Usage (in <project>/.codex/hooks.json, written by tools/build.py --codex-project):
    python -s codex_spawn_shim.py [--timeout=SECONDS] <project root> <interpreter> <hook script> [args...]

Codex calls a subagent spawn "spawn_agent" with tool_input.agent_type and tool_input.message; Claude calls it
"Agent" with tool_input.subagent_type and tool_input.prompt. The shim rewrites a Codex PreToolUse payload into the
Claude shape, sets CLAUDE_PROJECT_DIR, runs the project hook, and passes its stdout, stderr and exit code through.
A Codex `apply_patch` payload becomes a Claude `Edit` payload (tool_input.file_path = first patched file,
tool_input.files = all of them, tool_input.patch = the patch text). A Codex shell call keeps tool_name "Bash" with
tool_input.command. Any other payload (UserPromptSubmit, other tools) is passed through unchanged.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys


PATCH_PATH_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$|^\*\*\* Move to: (.+?)\s*$", re.M)


def to_claude(data: dict) -> dict:
    if data.get("tool_name") == "apply_patch":
        ti = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
        patch = str(ti.get("command") or ti.get("input") or ti.get("patch") or "")
        files = [a or b for a, b in PATCH_PATH_RE.findall(patch)]
        return {**data, "tool_name": "Edit",
                "tool_input": {"file_path": files[0] if files else "", "files": files, "patch": patch}}
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
    timeout = TIMEOUT
    if argv and argv[0].startswith("--timeout="):
        try:
            timeout = max(1, int(float(argv[0].split("=", 1)[1])))
        except ValueError:
            pass
        argv = argv[1:]
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
                              env=env, cwd=root, timeout=timeout)
    except subprocess.TimeoutExpired:
        sys.stderr.write(f"codex_spawn_shim: project hook {script} timed out after {timeout}s; spawn denied\n")
        return 2
    except OSError as e:
        sys.stderr.write(f"codex_spawn_shim: cannot run project hook {script} with {interpreter}: {e}; spawn denied\n")
        return 2
    pre_tool = isinstance(data, dict) and data.get("hook_event_name") == "PreToolUse"
    sys.stdout.buffer.write(codex_safe(proc.stdout) if pre_tool else proc.stdout)
    sys.stderr.buffer.write(proc.stderr)
    return proc.returncode


TIMEOUT = 30  # default when the command carries no --timeout=


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
