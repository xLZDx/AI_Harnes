#!/usr/bin/env python3
"""Plan-first gate (S7-T3, operator 2026-11-12: "I do not see the work plan before the start").

PreToolUse on file edits and mutating shell commands: the first change of a turn is denied until the turn holds a PRESENTED,
valid plan. Stop: a turn that changed things without a presented plan is blocked once.

Presented (GPT-PM plan round 3, R3-1), from the transcript, not from the agent's claim:
  (a) a plan tool call (update_plan / TodoWrite / TaskCreate / mcp update_plan) whose tool RESULT is a success, with a valid
      step list; or
  (b) the assistant's own text of this turn, written before the first change, holds the steps as a numbered/bulleted list that
      passes the same content check.
Content check: >= 2 steps, each >= 12 characters, no duplicates, at least one step of >= 3 words, at least one verification step
(test|verify|check|run|проверк|тест|провер).
Bounded: after MAX_DENIES denies in one turn the change is allowed and logged as `plan-first-exception` (never as a pass).
Skipped: subagents, a turn whose operator message ends with "?", scratchpad/tmp edits, CLAUDE_PLAN_FIRST_GATE=off.
Fails open on any error. Codex: tool names Bash / apply_patch; same rules.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _agentio  # noqa: E402
from _turn import read_turn  # noqa: E402

MAX_DENIES = 3
PLAN_TOOLS = ("update_plan", "TodoWrite", "TaskCreate", "ExitPlanMode")
VERIFY_RE = re.compile(r"test|verif|check|\brun\b|проверк|тест|провер", re.I)
LIST_RE = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+(.{3,})$", re.M)
MUTATING_SHELL_RE = re.compile(
    r"(?:^|[;&|(]\s*|&&\s*)(?:git\s+(?:commit|push|add|merge|rebase|checkout|switch|tag|cherry-pick|stash|apply|am|mv|rm)\b|"
    r"(?:rm|mv|cp|mkdir|touch|rmdir|del|ren|copy|move|Remove-Item|Move-Item|Copy-Item|New-Item|Set-Content|Add-Content|Out-File)\b|"
    r"(?:npm|pnpm|yarn|pip|pip3|uv|cargo|go)\s+(?:install|add|remove|uninstall|build|publish)\b|"
    r"gh\s+pr\s+(?:create|merge)\b|docker\s+(?:run|build|compose)\b|terraform\s+(?:apply|destroy|import)\b)|"
    r"python[\w.]*\s+(?:-\S+\s+)*\S*(?:ledger|build|install_\w+)\.py\s+(?:start|verify|review|approve|sprint|task-add|block|commit-hook|--install|--force)|"
    r"(?<![<>0-9&])>{1,2}\s*(?!/dev/null|&|NUL\b)\S", re.I | re.M)
SAFE_PATH_RE = re.compile(r"[\\/](?:scratchpad|AppData[\\/]Local[\\/]Temp|tmp|temp)[\\/]", re.I)


def log(rec: dict) -> None:
    try:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "hook": "plan_first_gate", **rec}
        with (_agentio.state_dir("plan_first") / "plan_first.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def steps_of(inp: dict) -> list[str]:
    """Step strings out of a plan tool input (items / todos / plan / steps)."""
    out: list[str] = []
    for key in ("items", "todos", "plan", "steps"):
        v = inp.get(key)
        if isinstance(v, list):
            for it in v:
                if isinstance(it, str):
                    out.append(it)
                elif isinstance(it, dict):
                    s = it.get("content") or it.get("step") or it.get("title") or it.get("subject") or ""
                    if s:
                        out.append(str(s))
    if not out and isinstance(inp.get("subject"), str):
        out.append(inp["subject"])
    return out


def valid_steps(steps: list[str]) -> bool:
    steps = [s.strip() for s in steps if s and s.strip()]
    if len(steps) < 2 or len(set(x.lower() for x in steps)) != len(steps):
        return False
    if any(len(s) < 12 for s in steps):
        return False
    if not any(len(s.split()) >= 3 for s in steps):
        return False
    return any(VERIFY_RE.search(s) for s in steps)


def is_mutation(data: dict) -> bool:
    name = _agentio.tool_name(data)
    if _agentio.is_edit(data):
        paths = _agentio.edited_paths(data)
        return not (paths and all(SAFE_PATH_RE.search(p) for p in paths))
    if _agentio.is_shell(data):
        return bool(MUTATING_SHELL_RE.search(_agentio.shell_command(data) or ""))
    return False


def plan_presented(turn: dict) -> tuple[bool, str]:
    """(ok, how) from the current turn's transcript."""
    task_steps: list[str] = []
    for c in turn["calls"]:
        n = c["name"]
        if n in PLAN_TOOLS or n.endswith("__update_plan"):
            if c["is_error"] is True or not c["result"]:
                continue  # recorded, not presented (also: a plan the operator rejected in ExitPlanMode)
            if n == "ExitPlanMode":
                plan = c["input"].get("plan")
                if isinstance(plan, str) and valid_steps([m.group(1) for m in LIST_RE.finditer(plan)]):
                    return True, "exit-plan-mode"
                continue
            if n == "TaskCreate":  # one step per call: the plan is the set of created tasks
                task_steps += steps_of(c["input"])
                continue
            if valid_steps(steps_of(c["input"])):
                return True, "plan-tool"
    if valid_steps(task_steps):
        return True, "task-create"
    # (b) the plan written as text before the first change
    first_change = next((i for i, c in enumerate(turn["calls"]) if c["name"] not in PLAN_TOOLS and
                         (c["name"] in _agentio.EDIT_TOOLS or c["name"] in ("Bash", "PowerShell", "shell", "shell_command"))), None)
    limit = turn["calls"][first_change]["texts_before"] if first_change is not None else len(turn["assistant_texts"])
    for t in turn["assistant_texts"][:max(limit, 0)] if first_change is not None else turn["assistant_texts"]:
        if valid_steps([m.group(1) for m in LIST_RE.finditer(t)]):
            return True, "text-list"
    return False, ""


def deny(reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}, ensure_ascii=False))


DENY_MSG = ("PLAN FIRST (operator 2026-11-12): the operator must see the work plan before the first change. Call the plan tool "
            "(update_plan) now with at least 2 concrete steps (12+ characters each, one of them a verification step such as tests or "
            "a check), or write the numbered plan in your message, then continue. Denied %d of %d.")


def counters(session: str, marker: str) -> tuple[Path, dict]:
    f = _agentio.state_dir("plan_first") / "denies.json"
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    return f, d if isinstance(d, dict) else {}


def on_pre(data: dict) -> None:
    if not is_mutation(data):
        return
    turn = read_turn(data.get("transcript_path") or "")
    if not turn["calls"] and not turn["human_text"]:
        _release(data, "fail-open-empty-turn")  # transcript unreadable: fail open, but visibly
        return
    if turn["human_text"].strip().endswith("?"):
        return
    ok, how = plan_presented(turn)
    if ok:
        return
    key = f"{data.get('session_id')}|{turn['marker']}"
    f = _agentio.state_dir("plan_first") / "denies.json"
    n = None
    with _agentio.file_lock(f, wait=1.0, stale=10.0) as got:  # two parallel tool calls must not both read the same count
        if got:
            f, d = counters(str(data.get("session_id")), turn["marker"])
            n = int(d.get(key, 0)) + 1
            d = {k: v for k, v in list(d.items())[-40:]}
            d[key] = n
            try:
                _agentio.atomic_write_text(f, json.dumps(d))
            except OSError as e:
                log({"event": "counter-write-failed", "error": type(e).__name__})
                n = None
    if n is None:  # the counter cannot be kept: a gate that cannot count would deny for ever, so release it and say so
        _release(data, "release-counter-unavailable")
        return
    if n > MAX_DENIES:
        log({"event": "plan-first-exception", "session": str(data.get("session_id"))[:8], "denies": n - 1})
        sys.stderr.write("plan_first_gate: released after %d denies (plan-first-exception, logged)\n" % (n - 1))
        _release(data, "plan-first-exception", write_log=False)
        return
    log({"event": "deny", "session": str(data.get("session_id"))[:8], "n": n})
    deny(DENY_MSG % (n, MAX_DENIES))


def _release(data: dict, reason: str, write_log: bool = True) -> None:
    """A gate that lets the call through without a verdict says so: log line and a metrics row with decision `release`."""
    if write_log:
        log({"event": reason, "session": str(data.get("session_id"))[:8]})
    try:
        import hook_metrics
        hook_metrics.record("plan_first_gate.py", str(data.get("hook_event_name") or "PreToolUse"), "release", reason, 0,
                            str(data.get("session_id") or ""), _agentio.client(data), str(data.get("cwd") or ""))
    except Exception:
        pass


def on_stop(data: dict) -> None:
    if data.get("stop_hook_active"):
        return
    turn = read_turn(data.get("transcript_path") or "")
    if turn["human_text"].strip().endswith("?"):
        return
    changed = False
    for c in turn["calls"]:
        probe = {"tool_name": c["name"], "tool_input": c["input"]}
        if is_mutation(probe):
            changed = True
            break
    if not changed or plan_presented(turn)[0]:
        return
    key = f"stop|{data.get('session_id')}|{turn['marker']}"
    f, d = counters("", "")
    if d.get(key):
        return
    d[key] = 1
    try:
        _agentio.atomic_write_text(f, json.dumps({k: v for k, v in list(d.items())[-40:]}))
    except OSError:
        pass
    log({"event": "stop-block", "session": str(data.get("session_id"))[:8]})
    print(json.dumps({"decision": "block", "reason": "PLAN FIRST: this turn changed things but no presented plan exists. "
                      "Call update_plan with the steps you took and still plan (2+ concrete steps, one verification step) so the operator "
                      "sees the plan, then finish."}, ensure_ascii=False))


def main() -> int:
    if os.environ.get("CLAUDE_PLAN_FIRST_GATE", "").lower() in {"off", "0", "false"}:
        return 0
    data = _agentio.read_stdin()
    if data.get("agent_type") or data.get("agent_id"):
        return 0
    ev = data.get("hook_event_name") or ("PreToolUse" if "tool_input" in data else "Stop")
    if ev == "PreToolUse":
        on_pre(data)
    elif ev == "Stop":
        on_stop(data)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:
        log({"event": "exception", "error": type(e).__name__})
        raise SystemExit(0)  # fail open, logged
