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
import re
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

UNKNOWN_AFTER_S = 600
CAP = 400  # shared by every project on the machine; ledger.py looks runs up here, so keep enough history


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


def mutate(fn) -> bool:
    """load -> fn(d) -> save under one cross-process lock (parallel SubagentStop hooks and spawns would lose updates otherwise).
    fn returns False when it changed nothing. False result = lock timeout or nothing to save."""
    import _agentio
    with _agentio.file_lock(_state_file(), wait=1.0, stale=10.0) as got:
        if not got:
            return False
        d = load()
        if fn(d) is False:
            return False
        save(d)
        return True


def state_of(entry: dict, now: float | None = None) -> str:
    now = now or time.time()
    if entry.get("stop"):
        return "FINISHED_CONFIRMED"
    fresh = now - max(entry.get("start", 0), entry.get("activity", 0)) < UNKNOWN_AFTER_S
    if not fresh:
        return "STATUS_UNKNOWN"
    return "ACTIVE_CONFIRMED" if entry.get("activity") else "START_OBSERVED"


def project_root(cwd: str) -> str:
    """Main checkout of the git repo at cwd (same as ledger.py main_root), '' when not a repo. ledger.py counts a test or
    review run only for the project it ran in."""
    try:
        import _agentio
        r = _agentio.run_git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=cwd or ".")
        if r.returncode != 0:
            return ""
        common = Path(r.stdout.strip())
        return str((common.parent if common.name == ".git" else common).resolve())
    except Exception:
        return ""


# a drive-letter path, or a POSIX path with one leading slash; never a URL tail (://host) or a UNC path (\\host, //host),
# whose exists() would make a network lookup with no timeout
ABS_PATH_RE = re.compile(r"(?<![\w/\\:])(?:[A-Za-z]:[\\/](?![\\/])|/(?![/\\]))[^\s\"'`<>|*?,;()\[\]]+")
BIND_DEADLINE_S = 3.0  # the whole search stays well inside the 10 s hook budget
BIND_GIT_TIMEOUT_S = 2


def project_from_text(tool_input: dict, max_dirs: int = 5, max_matches: int = 50) -> str:
    """B26: when the session cwd is not a repository (a Codex thread in Documents\\Codex, a Claude session reset to
    D:\\Repo), take the project from the absolute paths in the agent's own task text that lie inside a git repo (a
    worktree maps to its main checkout). The run is bound only when all such paths name ONE repository; paths in two
    different repositories, a git timeout or no repository at all give '' (the ledger then refuses the run, fail
    closed), so a run is never credited to a project it only mentioned."""
    try:
        import _agentio
        deadline = time.monotonic() + BIND_DEADLINE_S
        text = " ".join(str(v) for v in (tool_input or {}).values() if isinstance(v, str))
        tried = set()
        roots = set()
        for i, m in enumerate(ABS_PATH_RE.finditer(text)):
            if time.monotonic() > deadline or i >= max_matches:
                return ""
            raw = m.group(0).rstrip(".:")
            if raw.startswith(("\\\\", "//")) or (os.name == "nt" and raw.startswith("/")):
                continue
            p = Path(raw)
            while not p.exists() and p != p.parent:
                p = p.parent
            d = p if p.is_dir() else p.parent
            key = str(d).lower()
            if key in tried or d == d.parent:
                continue
            tried.add(key)
            r = _agentio.run_git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=str(d),
                                 timeout=BIND_GIT_TIMEOUT_S)
            if r.returncode in (124, 127):
                # git timed out or cannot start: stop rather than bind to a later, possibly unrelated path
                return ""
            if r.returncode == 0 and r.stdout.strip():
                common = Path(r.stdout.strip())
                roots.add(str((common.parent if common.name == ".git" else common).resolve()))
                if len(roots) > 1:
                    return ""
            if len(tried) >= max_dirs:
                # cap reached: a later path could name a second repository, so unexamined text means no binding
                return roots.pop() if len(roots) == 1 and not any(True for _ in ABS_PATH_RE.finditer(text, m.end())) else ""
        return roots.pop() if len(roots) == 1 else ""
    except Exception:
        pass
    return ""


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


CLAIM_WINDOW_S = 120  # a SubagentStart event claims a start line the PreToolUse hook printed at most this long ago


def announce_start(data: dict, client: str, model: str = "", effort: str = "", source: str = "pre",
                   agent_id: str = "") -> None:
    """Print one START line per agent. Two hooks can report the same start (PreToolUse of the Agent/Task tool and the
    SubagentStart event); the line is printed once: the same tool_use_id is skipped, and a start of the other `source`
    claims an unclaimed recent entry of the same type and session instead of adding a second one."""
    try:
        ti = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
        name = str(ti.get("subagent_type") or ti.get("agent_type") or "agent").strip()[:60]
        model = (model or str(ti.get("model") or "") or "policy").strip()[:40]
        effort = (effort or str(ti.get("reasoning_effort") or ti.get("effort") or "") or "policy").strip()[:20]
        task = task_id(str(data.get("cwd") or ""))
        session = str(data.get("session_id") or "")[:64]
        tuid = str(data.get("tool_use_id") or "")[:64]
        entry = {"type": name, "model": model, "effort": effort, "task": task, "start": time.time(),
                 "project": project_root(str(data.get("cwd") or "")) or project_from_text(ti),
                 "session": session, "tool_use_id": tuid, "sources": [source]}
        aid = str(agent_id or "")[:64]
        if aid:
            entry["agent_id"] = aid
        res = {"dup": False}

        def apply(d: dict):
            now = time.time()
            if aid and any(e.get("agent_id") == aid for e in d["agents"]):
                res["dup"] = True  # the same agent reported twice, also after its stop: never a second entry
                return False
            if tuid:  # a retried PreToolUse first finds its own entry, before any claim of another run's entry
                for e in d["agents"]:
                    if e.get("type") == name and not e.get("stop") and e.get("tool_use_id") == tuid:
                        res["dup"] = True
                        if not e.get("project") and entry["project"]:
                            e["project"] = entry["project"]
                            return True
                        return False
            for e in reversed(d["agents"]):  # newest first: a fresh start never claims a dead run's older entry
                if e.get("stop") or e.get("type") != name:
                    continue
                if aid and e.get("agent_id"):
                    continue  # that entry already belongs to another agent: never claim it
                if tuid and e.get("tool_use_id"):
                    continue  # a PreToolUse never claims another PreToolUse's entry
                if (source not in e.get("sources", ["pre"]) and now - e.get("start", 0) < CLAIM_WINDOW_S
                        and e.get("session") in ("", session)):
                    e.setdefault("sources", ["pre"]).append(source)
                    if aid:
                        e["agent_id"] = aid
                    if tuid and not e.get("tool_use_id"):
                        e["tool_use_id"] = tuid  # a retry of this PreToolUse must find this entry, not claim another
                    if not e.get("project") and entry["project"]:
                        e["project"] = entry["project"]
                    res["dup"] = True
                    return True
            d["agents"].append(entry)
        mutate(apply)
        if not res["dup"]:
            # role named explicitly: the Agent description may not say which role runs
            _emit(f"[agent START] {name} | role {name} | model {model} | effort {effort} | task {task}", client)
    except Exception:
        pass


def touch(data: dict) -> None:
    """A hook ran for a tool call made BY a subagent (the payload carries its agent_type): that proves it is alive now.
    Sets `activity` on the oldest unfinished entry of that type, so the state becomes ACTIVE_CONFIRMED. Silent on any error."""
    try:
        name = str(data.get("agent_type") or "").strip()[:60]
        if not name:
            return
        for e in load()["agents"]:  # cheap unlocked pre-check: at most one write per 20 s per agent
            if e.get("type") == name and not e.get("stop"):
                if time.time() - e.get("activity", 0) < 20:
                    return
                break
        else:
            return

        def apply(d: dict):
            for e in d["agents"]:
                if e.get("type") == name and not e.get("stop"):
                    e["activity"] = time.time()
                    return True
            return False
        mutate(apply)
    except Exception:
        pass


def announce_stop(data: dict, client: str) -> None:
    try:
        name = str(data.get("agent_type") or "agent").strip()[:60]
        res = {"secs": 0, "hit": False}

        aid = str(data.get("agent_id") or "")[:64]

        def finish(e: dict) -> bool:
            e["stop"] = time.time()
            res["secs"], res["hit"] = int(e["stop"] - e["start"]), True
            return True

        def apply(d: dict):
            if aid:
                # a stop that names its agent finishes that agent's entry; a duplicate stop finishes nothing. It never
                # falls back to an entry a SubagentStart already reported (a run killed by an app restart would
                # otherwise take a later run's stop and the ledger would credit a run that never finished).
                for e in d["agents"]:
                    if e.get("agent_id") == aid:
                        return False if e.get("stop") else finish(e)
                # the id was never recorded (SubagentStart not delivered): only an entry that came from PreToolUse
                # alone, carries no id and has the same type and session can be this run; newest first
                for e in reversed(d["agents"]):
                    if (e.get("type") == name and not e.get("stop") and not e.get("agent_id")
                            and "start" not in e.get("sources", ["pre"])
                            and (not data.get("session_id") or e.get("session") in ("", str(data.get("session_id"))[:64]))):
                        e["agent_id"] = aid
                        return finish(e)
                return False
            # no agent id in the payload: finish the unfinished entry of the same type (and session when known) only
            # when it is the only candidate; with two or more, a dead run could take this run's stop, so nothing is
            # finished (fail closed: the ledger refuses the run instead of crediting a run that never finished)
            cands = [e for e in d["agents"] if e.get("type") == name and not e.get("stop")
                     and (not data.get("session_id") or e.get("session") in ("", str(data.get("session_id"))[:64]))]
            if len(cands) == 1:
                return finish(cands[0])
            res["ambiguous"] = len(cands)
            return False
        mutate(apply)
        hit, secs = res["hit"], res["secs"]
        if res.get("ambiguous"):
            tail = f" | not paired: {res['ambiguous']} unfinished {name} runs and no agent id"
        else:
            tail = "" if hit else " | start was not observed"
        _emit(f"[agent STOP] {name} | {secs} s" + tail, client)
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
                            "session_id": data.get("session_id")}, client, source="start",
                           agent_id=str(data.get("agent_id") or ""))
        elif event == "PreToolUse" and _agentio.is_agent_spawn(data):
            # fallback START line when SubagentStart is not delivered; deduped against it and against agent_model_gate
            announce_start(data, client)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
