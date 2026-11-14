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

Never blocks: always exit 0, errors go once per call to <state>/lessons/errors.jsonl (via _lessons) or stderr.
Kill switch: HARNESS_LESSONS_RECALL=off. Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

LIMIT = 5
SESSION_KEEP_S = 7 * 24 * 3600
PM_TOOL_RE = re.compile(r"^mcp__pm-bridge__gpt_send")
GIT_RE = re.compile(r"\bgit\b[^;&|\n]*?\s(commit|push)\b")
GH_PR_RE = re.compile(r"\bgh\s+pr\b")
WORD_RE = re.compile(r"[a-z0-9-]+")


def _actions(data: dict, ae) -> tuple[list, str]:
    """(actions, words) for the call; ([], '') when nothing to recall."""
    name = ae.tool_name(data)
    if PM_TOOL_RE.match(name):
        return ["gpt-pm"], "gpt-pm pm bridge"
    if ae.is_agent_spawn(data):
        return ["agent"], "agent " + ae.agent_type(data)
    cmd = ae.shell_command(data)
    if cmd:
        acts = []
        for m in GIT_RE.finditer(cmd):
            if m.group(1) not in acts:
                acts.append(m.group(1))
        if GH_PR_RE.search(cmd):
            acts.append("pr")
        return acts, cmd[:300]
    if ae.is_edit(data):
        acts, words = [], []
        for p in ae.edited_paths(data):
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


def _marker(session: str) -> Path:
    import _agentio
    d = _agentio.state_dir("lessons") / "sessions"
    d.mkdir(parents=True, exist_ok=True)
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
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session)[:80] or "nosession"
    return d / (safe + ".json")


def main() -> int:
    if os.environ.get("HARNESS_LESSONS_RECALL", "").lower() == "off":
        return 0
    try:
        import _agentio as ae
        data = ae.read_stdin()
        actions, words = _actions(data, ae)
        if not actions:
            return 0
        import _lessons
        found = []
        for a in actions:
            for r in _lessons.search(a, WORD_RE.findall(words.lower()), limit=LIMIT):
                found.append((a, r))
        if not found:
            return 0
        mk = _marker(str(data.get("session_id") or ""))
        try:
            shown = json.loads(mk.read_text(encoding="utf-8"))
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
        for a, r in picked:
            shown.setdefault(a, []).append(r["id"])
        try:
            ae.atomic_write_text(mk, json.dumps(shown))
        except OSError as ex:
            sys.stderr.write(f"lessons_recall: marker not saved: {ex}\n")
        label = "/".join(dict.fromkeys(a for a, _ in picked))
        ctx = f"Global lessons for {label}: " + "; ".join(f"{r['id']} {r['rule']}" for _, r in picked)
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": ctx}},
                                    ensure_ascii=False))
    except Exception as ex:  # never block the tool call
        try:
            sys.stderr.write(f"lessons_recall: {type(ex).__name__}: {ex}\n")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
