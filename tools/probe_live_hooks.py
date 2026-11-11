"""Prove that the installed Claude hooks really block a dangerous command (read-only: the payload is only piped to
the hook commands, nothing is executed).

  python tools/probe_live_hooks.py [--settings ~/.claude/settings.json] [--require shell_policy_gate.py] [--timeout 60]

Exit 0 only when the required hook is configured for the Bash tool, runs within the timeout and denies the payload
(exit code 2, or a JSON answer with hookSpecificOutput.permissionDecision == "deny", or decision == "block").
Exit 1 otherwise: hook missing, did not run, timed out, allowed the payload, or answered with output that is not a
recognizable allow/deny answer.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

PAYLOAD = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": "D:/Repo/Harness_v2", "session_id": "probe",
           "tool_input": {"command": "rm -rf /d/tmp/probe_nothing"}}


def verdict(returncode: int, stdout: str) -> str:
    """'deny', 'allow' or 'unknown' from one hook run, read structurally."""
    if returncode == 2:
        return "deny"
    if returncode != 0:
        return "unknown"
    text = stdout.strip()
    if not text:
        return "allow"
    try:
        obj = json.loads(text)
    except ValueError:
        return "unknown"
    if not isinstance(obj, dict):
        return "unknown"
    hso = obj.get("hookSpecificOutput")
    if isinstance(hso, dict) and hso.get("permissionDecision") == "deny":
        return "deny"
    if obj.get("decision") == "block":
        return "deny"
    return "allow"


def matcher_applies(matcher: str, tool: str) -> bool:
    """Would a hook group with this matcher run for the tool? Empty and '*' mean every tool; otherwise the matcher is
    a regular expression that must match the whole tool name (strict on purpose: fewer matches = the probe fails).
    Raises re.error for an invalid expression."""
    m = matcher.strip()
    if m in ("", "*"):
        return True
    return re.fullmatch(m, tool) is not None


def hook_argv(h: dict) -> list[str]:
    """argv of one hook. command + args form: used as given. A single command string without args is split like a
    Windows command line (shlex, posix=False) and surrounding quotes are stripped from every part."""
    command = str(h.get("command", ""))
    if h.get("args"):
        return [command, *map(str, h["args"])]
    try:
        parts = shlex.split(command, posix=False)
    except ValueError:
        return [command]
    return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts] or [command]


def probe(settings_path: Path, require: str, timeout: int) -> tuple[bool, str]:
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        groups = settings["hooks"]["PreToolUse"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        return False, f"cannot read PreToolUse hooks from {settings_path}: {e}"
    found = False
    for g in groups if isinstance(groups, list) else []:
        if not isinstance(g, dict):
            continue
        try:
            applies = matcher_applies(str(g.get("matcher") or ""), "Bash")
        except re.error as e:
            return False, f"invalid matcher {g.get('matcher')!r} in a PreToolUse group: {e}"
        if not applies:
            continue
        for h in g.get("hooks") or []:
            if not isinstance(h, dict):
                continue
            argv = hook_argv(h)
            if not any(Path(a).name == require for a in argv):
                continue
            found = True
            try:
                r = subprocess.run(argv, input=json.dumps(PAYLOAD).encode(), capture_output=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                return False, f"{require} timed out after {timeout}s"
            except OSError as e:
                return False, f"{require} could not run: {e}"
            v = verdict(r.returncode, r.stdout.decode("utf-8", "replace"))
            if v != "deny":
                return False, f"{require} did not deny the dangerous command (result: {v}, exit {r.returncode})"
    if not found:
        return False, f"{require} is not configured as a PreToolUse hook for Bash in {settings_path}"
    return True, f"{require} denied the dangerous command"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--settings", type=Path, default=Path.home() / ".claude" / "settings.json")
    ap.add_argument("--require", default="shell_policy_gate.py")
    ap.add_argument("--timeout", type=int, default=60)
    a = ap.parse_args(argv)
    ok, msg = probe(a.settings, a.require, a.timeout)
    try:
        s = json.loads(a.settings.read_text(encoding="utf-8-sig"))["hooks"]
        print("live hook entries:", sum(len(g.get("hooks", [])) for v in s.values() for g in v))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    print(("PROBE OK: " if ok else "PROBE FAILED: ") + msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
