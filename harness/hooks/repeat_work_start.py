#!/usr/bin/env python3
"""UserPromptSubmit hook: compare a new task with git history BEFORE work starts (operator 2026-10-29).

Companion of repeat_work_gate.py (which checks at commit time). Commit subjects and paths are usually
English while the operator writes Russian, so only Latin tokens of the prompt are used: identifiers,
file names, ticket/story ids (T04, W2, probe_demo_endpoints.py ...). If WARN_AT or more commits of the last
LOOKBACK_DAYS days on ANY branch match, their list is added to the agent's context with the instruction to
read what they produced before doing the task again. Advisory only: never blocks.

One file for Claude Code and Codex: both send `prompt` and `cwd` on UserPromptSubmit and accept
hookSpecificOutput.additionalContext. Stdlib only; fails open. Kill switch: CLAUDE_REPEAT_WORK_START=off.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _agentio  # noqa: E402  shared Claude Code / Codex adapter

LOOKBACK_DAYS = 21
MAX_COMMITS = 400
WARN_AT = 3
MIN_PROMPT = 20
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{1,}")
GENERIC = {"the", "and", "for", "with", "from", "that", "this", "git", "file", "files", "test", "tests", "fix",
           "add", "commit", "push", "check", "make", "hook", "hooks", "run", "use", "not", "are", "you", "all",
           "claude", "code", "true", "false", "none", "http", "https", "www", "com", "md", "py", "js", "json",
           "main", "master", "branch", "error", "docs", "doc", "new", "old", "yes", "stop", "go", "ok"}
AUTOMATED = ("<task-notification>", "<system-reminder>", "[SYSTEM NOTIFICATION", "[Request interrupted", "Caveat:",
             "AUTONOMOUS", "/")
SHARED_NAMES = {"decision_log.md", "lessons_log.md", "document_registry.md", "changelog.md", "package-lock.json",
                "pubspec.lock", "poetry.lock", "yarn.lock", "pnpm-lock.yaml", "cargo.lock"}


def git(cwd, *args):
    r = _agentio.run_git(list(args), cwd=cwd)
    return r.stdout if r.returncode == 0 else ""


def id_like(tok: str) -> bool:
    return bool(re.search(r"\d|_|\.|-", tok)) or bool(re.search(r"[a-z][A-Z]", tok))


def prompt_tokens(prompt: str) -> set:
    toks = set()
    for t in TOKEN_RE.findall(prompt):
        t = t.strip(".-_").lower()
        if len(t) >= 2 and t not in GENERIC and (len(t) >= 4 or id_like(t)):
            toks.add(t)
    return toks


def matching_commits(cwd: str, toks: set):
    out = git(cwd, "log", "--all", "--no-merges", f"--since={LOOKBACK_DAYS}.days.ago", f"-n{MAX_COMMITS}",
              "--name-only", "--format=\x01%h\x02%ad\x02%s\x02%D", "--date=short")
    found = []
    for block in out.split("\x01"):
        if not block.strip():
            continue
        head, _, files_text = block.partition("\n")
        parts = head.split("\x02")
        if len(parts) < 4:
            continue
        files = [f for f in files_text.split("\n") if f and Path(f).name.lower() not in SHARED_NAMES]
        hay = (parts[2] + " " + " ".join(files)).lower()
        hit = {t for t in toks if t in hay}
        if any(id_like(t) for t in hit) or len(hit) >= 2:
            found.append((parts[0], parts[1], parts[2], parts[3], hit))
    return found


def main() -> int:
    if os.environ.get("CLAUDE_REPEAT_WORK_START", "").lower() in {"off", "0", "false"}:
        return 0
    data = _agentio.read_stdin()
    prompt = str(data.get("prompt") or "").strip()
    if len(prompt) < MIN_PROMPT or prompt.startswith(AUTOMATED):
        return 0
    cwd = data.get("cwd") or os.getcwd()
    if not git(cwd, "rev-parse", "--git-dir").strip():
        return 0
    toks = prompt_tokens(prompt)
    if not toks:
        return 0
    found = matching_commits(cwd, toks)
    if len(found) < WARN_AT:
        return 0
    used = sorted({t for *_, hit in found for t in hit})[:8]
    lines = "\n".join(f"  {sha} {date} {subj[:90]}{' [' + refs + ']' if refs else ''}"
                      for sha, date, subj, refs, _ in found[:10])
    ctx = (f"[Repeat-work check] {len(found)} commits in the last {LOOKBACK_DAYS} days (all branches) already touch "
           f"what this task names ({', '.join(used)}):\n{lines}\n"
           "Before starting: read those commits and their records, and say what they actually PRODUCED (a result, "
           "not intent). Do not redo or wrap more scaffolding around work whose result never appeared; if the "
           "same thing was attempted several times, say so to the operator before a new attempt.")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx}},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        sys.stderr.write(traceback.format_exc())
        raise SystemExit(0)
