#!/usr/bin/env python3
"""UserPromptSubmit hook (S19-T5, backlog B38): tells the model which harness agents are running.

The Claude desktop app shows neither the statusLine nor hook systemMessage lines, so the operator never sees the running
agents. This hook puts one short line into additionalContext, e.g.
`Running harness agents: functional-test-reviewer, code-reviewer`, so the assistant can say it in its reply.

Source: agents/active.json in the hook state folder (HARNESS_HOOK_STATE or ~/.harness-hooks), written by agent_visibility.py.
Only entries without a `stop` that belong to the current project (entry `project` equals the main checkout of the prompt's
cwd) or to the current session (entry `session` equals the payload session_id) are listed. A name followed by `?` has had no
event for 10+ minutes (STATUS_UNKNOWN: it may be dead). Nothing is printed when no agent runs. Any error is logged to stderr
only; the hook always exits 0 and never blocks the prompt. Work is one small JSON read and at most one git call.
One file for Claude Code and Codex: both send the same UserPromptSubmit stdin and accept the same JSON output.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

MAX_NAMES = 10  # the line stays short even if many entries are open
GIT_TIMEOUT_S = 3  # the hook has a 10 s budget; a slow git must not eat it
DROP_AFTER_S = 24 * 3600  # an entry with no event for a day is a dead run, never listed


def _log_error(msg: str) -> None:
    """One line to the hook state folder (like run_hook's child_skips.jsonl); never raises."""
    try:
        import _agentio
        with open(_agentio.state_dir("agents_context") / "errors.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "error": msg[:300]}) + "\n")
    except Exception:
        pass


def _read_entries() -> list:
    """Entries of agents/active.json; a missing file means no agent ever ran. Any other failure is logged and gives []."""
    import agent_visibility as av
    try:
        d = json.loads(av._state_file().read_text(encoding="utf-8"))
        agents = d.get("agents") if isinstance(d, dict) else None
        if not isinstance(agents, list):
            raise ValueError("agents list missing")
        return agents
    except FileNotFoundError:
        return []
    except Exception as ex:
        _log_error(f"read active.json: {type(ex).__name__}: {ex}")
        return []


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p)) if p else ""


def running_names(data: dict) -> list[str]:
    import agent_visibility as av
    now = time.time()
    session = str(data.get("session_id") or "")[:64]
    cwd = str(data.get("cwd") or "")
    project = _norm(av.project_root(cwd, timeout=GIT_TIMEOUT_S)) if cwd else ""
    live, unknown = [], []
    for e in _read_entries():
        try:
            if not isinstance(e, dict) or e.get("stop"):
                continue
            last = max(float(e.get("start") or 0), float(e.get("activity") or 0))
            if now - last > DROP_AFTER_S:
                continue
            same_project = bool(project) and _norm(str(e.get("project") or "")) == project
            same_session = bool(session) and e.get("session") == session
            if not (same_project or same_session):
                continue
            name = str(e.get("type") or "agent")[:60]
            if av.state_of(e, now) == "STATUS_UNKNOWN":
                unknown.append((last, name + "?"))
            else:
                live.append((last, name))
        except Exception:
            continue  # one malformed entry never hides the others
    # live runs first, then the unknown ones; newest first inside each group, so the name cap keeps the live ones
    names = [n for _, n in sorted(live, key=lambda x: -x[0])] + [n for _, n in sorted(unknown, key=lambda x: -x[0])]
    return names


def main() -> int:
    try:
        import _agentio
        data = _agentio.read_stdin()
        names = running_names(data)
        if not names:
            return 0
        shown = names[:MAX_NAMES]
        more = f" (+{len(names) - len(shown)} more)" if len(names) > len(shown) else ""
        line = "Running harness agents: " + ", ".join(shown) + more
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                            "additionalContext": line}}))
    except Exception as ex:  # never fail the prompt
        _log_error(f"{type(ex).__name__}: {ex}")
        try:
            sys.stderr.write(f"agents_context: {type(ex).__name__}: {ex}\n")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
