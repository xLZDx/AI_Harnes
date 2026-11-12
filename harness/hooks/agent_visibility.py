#!/usr/bin/env python3
"""Running-agent visibility (S7-T6). Prints one line per agent START and STOP and keeps the active-agents file that the
status line reads.

Contract (GPT-PM plan rounds 4-5):
  * The line carries only: agent type, model, effort, ledger task id (or "active"), duration on STOP. NEVER the prompt text,
    not even truncated, in the UI, the state file or the metrics.
  * States by proof level: START_OBSERVED (start seen, younger than UNKNOWN_AFTER_S), STATUS_UNKNOWN (no STOP for longer),
    FINISHED_CONFIRMED (STOP seen), ACTIVE_CONFIRMED (a later event of that agent was recorded in `activity`, younger than
    UNKNOWN_AFTER_S). Entries are never deleted by age; only a STOP finishes one. The list is capped to the newest 60 entries.
  * Claude shows hook output with {"systemMessage": ...}. Codex has no such field: the line goes to stderr, which is NOT
    claimed to be visible to the user.
  * Any error prints nothing and exits 0: this hook only prints.
Used two ways: imported by agent_model_gate.py (announce_start after an allowed spawn) and run as a SubagentStop hook.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

UNKNOWN_AFTER_S = 600
CAP = 60


def _state_file() -> Path:
    import _agentio
    return _agentio.state_dir("agents") / "active.json"


def load() -> dict:
    try:
        d = json.loads(_state_file().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and isinstance(d.get("agents"), list) else {"agents": []}
    except Exception:
        return {"agents": []}


def save(d: dict) -> None:
    import _agentio
    d["agents"] = d["agents"][-CAP:]
    _agentio.atomic_write_text(_state_file(), json.dumps(d))


def state_of(entry: dict, now: float | None = None) -> str:
    now = now or time.time()
    if entry.get("stop"):
        return "FINISHED_CONFIRMED"
    fresh = now - max(entry.get("start", 0), entry.get("activity", 0)) < UNKNOWN_AFTER_S
    if not fresh:
        return "STATUS_UNKNOWN"
    return "ACTIVE_CONFIRMED" if entry.get("activity") else "START_OBSERVED"


def task_id(cwd: str) -> str:
    """Id of the ledger task that is in progress for the project at cwd, else 'active'."""
    try:
        p = Path(cwd or ".").resolve()
        for d in [p, *p.parents]:
            f = d / ".harness" / "ledger.json"
            if f.is_file():
                led = json.loads(f.read_text(encoding="utf-8"))
                for sp in led.get("sprints", []):
                    for t in sp.get("tasks", []):
                        if t.get("state") in ("in_progress", "started", "active"):
                            return str(t.get("id"))
                return "active"
            if (d / ".git").exists():
                break
    except Exception:
        pass
    return "active"


def _emit(line: str, client: str) -> None:
    if client == "codex":
        sys.stderr.write(line + "\n")
    else:
        sys.stdout.write(json.dumps({"systemMessage": line}, ensure_ascii=False))


def announce_start(data: dict, client: str, model: str = "", effort: str = "") -> None:
    try:
        ti = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
        name = str(ti.get("subagent_type") or ti.get("agent_type") or "agent").strip()[:60]
        model = (model or str(ti.get("model") or "") or "policy").strip()[:40]
        effort = (effort or str(ti.get("reasoning_effort") or ti.get("effort") or "") or "policy").strip()[:20]
        task = task_id(str(data.get("cwd") or ""))
        d = load()
        d["agents"].append({"type": name, "model": model, "effort": effort, "task": task, "start": time.time(),
                            "session": str(data.get("session_id") or "")[:64], "tool_use_id": str(data.get("tool_use_id") or "")[:64]})
        save(d)
        _emit(f"[agent START] {name} | model {model} | effort {effort} | task {task}", client)
    except Exception:
        pass


def touch(data: dict) -> None:
    """A hook ran for a tool call made BY a subagent (the payload carries its agent_type): that proves it is alive now.
    Sets `activity` on the oldest unfinished entry of that type, so the state becomes ACTIVE_CONFIRMED. Silent on any error."""
    try:
        name = str(data.get("agent_type") or "").strip()[:60]
        if not name:
            return
        d = load()
        for e in d["agents"]:
            if e.get("type") == name and not e.get("stop"):
                if time.time() - e.get("activity", 0) < 20:
                    return  # at most one write per 20 s per agent
                e["activity"] = time.time()
                save(d)
                return
    except Exception:
        pass


def announce_stop(data: dict, client: str) -> None:
    try:
        name = str(data.get("agent_type") or "agent").strip()[:60]
        d = load()
        hit = None
        for e in d["agents"]:  # oldest unfinished entry of the same type (and session when known)
            if e.get("type") == name and not e.get("stop") and (not data.get("session_id") or e.get("session") in ("", str(data.get("session_id"))[:64])):
                hit = e
                break
        secs = 0
        if hit:
            hit["stop"] = time.time()
            secs = int(hit["stop"] - hit["start"])
            save(d)
        _emit(f"[agent STOP] {name} | {secs} s" + ("" if hit else " | start was not observed"), client)
        if hit:
            try:
                import hook_metrics
                hook_metrics.record("agent_visibility.py", "SubagentStop", "allow", "agent-stop", secs * 1000,
                                    str(data.get("session_id") or ""), client, str(data.get("cwd") or ""), name)
            except Exception:
                pass
    except Exception:
        pass


def main() -> int:
    try:
        import _agentio
        data = _agentio.read_stdin()
        client = _agentio.client(data)
        event = data.get("hook_event_name") or ""
        if event == "SubagentStop":
            announce_stop(data, client)
        elif event == "SubagentStart":
            announce_start({"tool_input": {"subagent_type": data.get("agent_type")}, "cwd": data.get("cwd"),
                            "session_id": data.get("session_id")}, client)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
