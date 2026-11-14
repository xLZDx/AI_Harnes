#!/usr/bin/env python3
"""PreToolUse hook (S20-T4): recalls global lessons just before an action that repeated mistakes came from.

A cheap prefilter maps the tool call to an action BEFORE _lessons is imported or any file is read:
  shell `git commit` / `git push` / `gh pr ...` -> commit / push / pr;  agent spawn -> agent;
  edit under harness/hooks/, tools/*guard*, .github/workflows/ -> edit-hook / edit-guard;
  PM Bridge send tool (mcp__pm-bridge__gpt_send*) -> gpt-pm.
No action: exit 0 at once (a plain `ls` costs one stdin read and a few regexes). Otherwise _lessons.search(action, words,
limit=5) gives FAILURE/SHORTCOMING rules; ids already shown in this session for this action are dropped (marker file
<state>/lessons/sessions/<session_id>.json, files older than 7 days are removed on the way) and the rest go out as
additionalContext `Global lessons for <action>: L0042 <rule>; ...`.

Output shape: {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": ...}} (Claude Code documents it).
Codex: acceptance of PreToolUse additionalContext is NOT proven by a real Codex run; the repo only shows that
codex_spawn_shim.py forwards it and tests/test_hooks_dual.py treats it as an allowed Codex field. The hook emits it for
Codex too; if Codex ignores it the lesson is simply not shown there (no failure), and the fallback is the CORE.md rule plus
GLOBAL_LESSONS.md / `tools/lessons.py search`.

Never blocks: always exit 0, errors go to <state>/lessons/errors.jsonl (via _lessons, or written directly when _lessons
cannot be imported). The search path never takes a lock and never rebuilds: a stale INDEX.md is served as it is. A call without
a session id has no dedup marker. The JSON is written (ASCII-escaped) before the session marker is saved.
Kill switch: HARNESS_LESSONS_RECALL=off. Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import sys


LIMIT = 5
SESSION_KEEP_S = 7 * 24 * 3600
PM_TOOL_RE = re.compile(r"^mcp__pm-bridge__gpt_send")
GIT_RE = re.compile(r"\bgit\b[^;&|\n]*?\s(commit|push)\b")
GH_PR_RE = re.compile(r"\bgh\s+pr\b")
WORD_RE = re.compile(r"[a-z0-9-]+")


SHELL_TOOLS = {"Bash", "PowerShell", "exec_command", "shell", "local_shell"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"}
AGENT_TOOLS = {"Agent", "Task", "spawn_agent"}
PATCH_PATH_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$|^\*\*\* Move to: (.+?)\s*$", re.M)


def _read_stdin() -> dict:
    """Same tolerant read as _agentio.read_stdin, without importing it (the prefilter must stay light)."""
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig", errors="replace")
        data = json.loads(raw) if raw.strip() else {}
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _actions(data: dict) -> tuple[list, str]:
    """(actions, words) for the call; ([], '') when nothing to recall. Only json/re/sys are needed."""
    name = str(data.get("tool_name") or "")
    ti = data.get("tool_input")
    ti = ti if isinstance(ti, dict) else {}
    if PM_TOOL_RE.match(name):
        return ["gpt-pm"], "gpt-pm pm bridge"
    if name in AGENT_TOOLS:
        return ["agent"], "agent " + str(ti.get("subagent_type") or ti.get("agent_type") or data.get("agent_type") or "")
    if name in SHELL_TOOLS:
        cmd = ti.get("command", ti.get("cmd", ""))
        if isinstance(cmd, list):
            cmd = " ".join(str(x) for x in cmd)
        cmd = str(cmd or "")
        acts = []
        for m in GIT_RE.finditer(cmd):
            if m.group(1) not in acts:
                acts.append(m.group(1))
        if GH_PR_RE.search(cmd):
            acts.append("pr")
        return acts, cmd[:300]
    if name in EDIT_TOOLS:
        if name == "apply_patch":
            patch = str(ti.get("command") or ti.get("input") or ti.get("patch") or "")
            paths = [x or y for x, y in PATCH_PATH_RE.findall(patch)]
        else:
            p = ti.get("file_path") or ti.get("notebook_path")
            paths = [str(p)] if p else []
        acts, words = [], []
        for p in paths:
            q = p.replace("\\", "/").lower()
            hit = False
            if "harness/hooks/" in q:
                acts.append("edit-hook"); hit = True
            if re.search(r"(?:^|/)tools/[^/]*guard[^/]*$", q) or re.search(r"(?:^|/)harness/hooks/[^/]*guard[^/]*$", q):
                acts.append("edit-guard"); hit = True
            if ".github/workflows/" in q:
                acts.append("edit-guard"); words.append("workflow ci"); hit = True
            if hit:
                words.append(q[-120:])
        return list(dict.fromkeys(acts)), " ".join(words)
    return [], ""


def _log(where: str, ex) -> None:
    """errors.jsonl line for this hook's own failure; direct write when _lessons is not importable. Never raises."""
    try:
        import _lessons
        _lessons._log_error(where, ex)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        import time
        d = os.environ.get("HARNESS_LESSONS_DIR")
        if d:
            base = d
        else:
            base = os.path.join(os.environ.get("HARNESS_HOOK_STATE") or os.path.join(os.path.expanduser("~"), ".harness-hooks"),
                                "lessons")
        os.makedirs(base, exist_ok=True)
        rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "where": where, "error": f"{type(ex).__name__}: {ex}"[:300]}
        with open(os.path.join(base, "errors.jsonl"), "ab") as fh:
            fh.write((json.dumps(rec, ensure_ascii=True) + "\n").encode("ascii"))
    except Exception:  # noqa: BLE001
        pass


def _marker(session: str, _agentio):
    import _lessons  # the store folder honours HARNESS_LESSONS_DIR too
    d = _lessons.store_dir() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    import time
    cutoff = time.time() - SESSION_KEEP_S
    try:
        for f in d.glob("*.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass
    except OSError:
        pass
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session)[:80]
    return d / (safe + ".json")


def main() -> int:
    if os.environ.get("HARNESS_LESSONS_RECALL", "").lower() == "off":
        return 0
    try:
        data = _read_stdin()
        actions, words = _actions(data)
        if not actions:
            return 0
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import _agentio as ae  # heavy imports only after an action matched
        import _lessons
        found = []
        for a in actions:
            for r in _lessons.search(a, WORD_RE.findall(words.lower()), limit=LIMIT):
                found.append((a, r))
        if not found:
            return 0
        session = str(data.get("session_id") or "").strip()
        mk = _marker(session, ae) if session else None
        shown = {}
        if mk is not None:
            try:
                shown = json.loads(mk.read_bytes().decode("utf-8-sig"))
                if not isinstance(shown, dict):
                    shown = {}
            except (OSError, ValueError):
                shown = {}
        picked, ids = [], set()
        for a, r in found:
            if r["id"] in ids or r["id"] in shown.get(a, []):
                continue
            ids.add(r["id"])
            picked.append((a, r))
            if len(picked) >= LIMIT:
                break
        if not picked:
            return 0
        label = "/".join(dict.fromkeys(a for a, _ in picked))
        ctx = f"Global lessons for {label}: " + "; ".join(f"{r['id']} {r['rule']}" for _, r in picked)
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": ctx}},
                                    ensure_ascii=True))
        sys.stdout.flush()
        if mk is not None:  # the marker is saved only after the lessons were actually written out
            for a, r in picked:
                shown.setdefault(a, []).append(r["id"])
            try:
                ae.atomic_write_text(mk, json.dumps(shown))
            except OSError as ex:
                _log("recall:marker", ex)
    except Exception as ex:  # never block the tool call
        _log("recall", ex)
        try:
            sys.stderr.write(f"lessons_recall: {type(ex).__name__}: {ex}\n")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
