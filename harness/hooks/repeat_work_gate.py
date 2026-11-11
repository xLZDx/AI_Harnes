#!/usr/bin/env python3
"""PreToolUse (Bash|PowerShell) gate against repeated or unverified work (operator 2026-10-29).

Incident: an agent spent two days on the same test scaffolding (5 launches, 21 commits, 11 documents,
no result of the check itself) although history already held the answer. The gate is not tied to any
tool or domain; it compares what is about to be committed with the history of ALL branches:

  * on `git commit`, take the files being committed and look at the last LOOKBACK_DAYS days of commits
    on every branch (`git log --all`);
  * a prior commit is "similar" when it shares at least 2 and 80% of these files, or its subject is close
    to the new subject;
  * WARN_AT or more similar commits: deny ONCE per file set, listing them. The agent must read those
    commits/records, say what they already produced and why another change is needed;
  * ESCALATE_AT or more: deny until the operator writes. The agent must ask the operator with
    AskUserQuestion; keep_going_gate lets the turn end with that question (`tripped_and_unanswered`).

Shared log, plan and plan-report files touched by every commit are ignored. Stdlib only; fails open.
Kill switch: CLAUDE_REPEAT_WORK_GATE=off. State: _agentio.state_dir("repeat_work_gate") (override CLAUDE_REPEAT_WORK_STATE).
Works for Claude Code and Codex: shell calls and human-turn counting go through _agentio. Codex has no
AskUserQuestion, so the escalation text asks for a plain-text question there.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import traceback
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _agentio as agentio  # noqa: E402

STATE = Path(os.environ.get("CLAUDE_REPEAT_WORK_STATE") or agentio.state_dir("repeat_work_gate"))
STATE_FILE = STATE / "state.json"
LOOKBACK_DAYS = 21
MAX_COMMITS = 400
WARN_AT = 3
ESCALATE_AT = 8
OVERLAP = 0.8
MIN_SHARED = 2  # a single shared file is not repeated work
HOT_SHARE = 0.3  # files in more than this share of the window's commits (central modules) are ignored
HOT_MIN_COMMITS = 10
SUBJECT_JACCARD = 0.6
COMMIT_RE = re.compile(r"\bgit\b[^\n;&|]*?\bcommit\b", re.I)
ALL_RE = re.compile(r"(?:^|\s)-[a-zA-Z]*a[a-zA-Z]*\b|--all\b")
SHARED_NAMES = {"decision_log.md", "lessons_log.md", "document_registry.md", "changelog.md", "package-lock.json",
                "pubspec.lock", "poetry.lock", "yarn.lock", "pnpm-lock.yaml", "cargo.lock", "project_plan.md"}
SHARED_SUFFIXES = ("_project_plan.html", "_project_plan.ru.html")  # rendered plan reports (operator 2026-11-09)

DENY_WARN = (
    "Repeat-work gate (operator 2026-10-29): this commit touches the same files as %d earlier commits "
    "(last %d days, all branches):\n%s\n"
    "Before committing again: read those commits and their records, state what they already produced "
    "(results, not intent) and why another change is needed. If the earlier work never produced its result, "
    "fix the cause instead of adding more around it. Commit again after that; this warning is shown once."
)
DENY_ESCALATE = (
    "Repeat-work gate (operator 2026-10-29): %d earlier commits already touched these files "
    "(last %d days, all branches):\n%s\n"
    "This is repeated work. Stop committing. Ask the operator with AskUserQuestion (at least 2 options, "
    "recommended first, concrete consequence each): is another attempt worth it, or should the approach "
    "change or the task close? Do not recommend simply continuing. Ending the turn with that question is allowed."
)


def tail_rows(path, max_bytes=40_000_000):
    rows = []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
        lines = data.split(b"\n")
        if size > max_bytes:
            lines = lines[1:]
        for ln in lines:
            if ln.strip():
                try:
                    rows.append(json.loads(ln))
                except Exception:
                    pass
    except Exception:
        pass
    return rows


def is_human(row) -> bool:
    if row.get("isMeta") or row.get("isSidechain") or row.get("isCompactSummary"):
        return False
    if row.get("type") != "user" or "toolUseResult" in row or row.get("turnOrigin") not in (None, "human"):
        return False
    c = (row.get("message") or {}).get("content")
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return False
        c = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(c, str) or not c.strip():
        return False
    return not c.lstrip().startswith(("<task-notification>", "<system-reminder>", "[SYSTEM NOTIFICATION",
                                      "[Request interrupted", "Caveat:"))


def human_count(path) -> int:
    rows = tail_rows(path)
    n = sum(1 for r in rows if is_human(r))
    # Codex transcript rows: genuine user messages (injected context is filtered by _agentio)
    n += sum(1 for r in rows if r.get("type") == "response_item"
             for rec in agentio._codex_records(r) if rec["kind"] == "user" and rec.get("text", "").strip())
    return n


def git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=20)
    return r.stdout if r.returncode == 0 else ""


def meaningful(files):
    return {f for f in files if f and Path(f).name.lower() not in SHARED_NAMES
            and not Path(f).name.lower().endswith(SHARED_SUFFIXES)}


def tokens(subject: str) -> set:
    return {t for t in re.findall(r"[\w]+", subject.lower()) if len(t) >= 4}


def similar_commits(cwd: str, staged: set, subject: str):
    out = git(cwd, "log", "--all", "--no-merges", f"--since={LOOKBACK_DAYS}.days.ago", f"-n{MAX_COMMITS}",
              "--name-only", "--format=\x01%h\x02%ad\x02%s\x02%D", "--date=short")
    window = []
    for block in out.split("\x01"):
        if not block.strip():
            continue
        head, _, files_text = block.partition("\n")
        parts = head.split("\x02")
        if len(parts) >= 4:
            window.append((parts[0], parts[1], parts[2], parts[3], meaningful(files_text.split("\n"))))
    hot = set()
    if len(window) >= HOT_MIN_COMMITS:
        counts = {}
        for *_, files in window:
            for f in files:
                counts[f] = counts.get(f, 0) + 1
        hot = {f for f, n in counts.items() if n / len(window) > HOT_SHARE}
    staged = staged - hot
    subj_tokens = tokens(subject)
    found = []
    for sha, date, subj, refs, files in window:
        shared = len(staged & (files - hot))
        by_files = bool(staged) and shared >= MIN_SHARED and shared / len(staged) >= OVERLAP
        t = tokens(subj)
        by_subject = bool(subj_tokens and t) and len(subj_tokens & t) / len(subj_tokens | t) >= SUBJECT_JACCARD
        if by_files or by_subject:
            found.append((sha, date, subj, refs))
    return found


def subject_of(command: str) -> str:
    m = re.search(r"-m\s+(?:\"([^\"]*)\"|'([^']*)')", command)
    return (m.group(1) or m.group(2) or "") if m else ""


def load_state() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}  # corrupt content that still parses -> empty state
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        agentio.atomic_write_text(STATE_FILE, json.dumps(state))
    except OSError:
        pass


def tripped_and_unanswered(data: dict) -> bool:
    """True when this session's gate escalated and the operator has not written since (used by keep_going_gate)."""
    try:
        entry = load_state().get(str(data.get("session_id") or ""), {})
        if "tripped_at_humans" not in entry:
            return False
        return human_count(data.get("transcript_path") or "") == entry["tripped_at_humans"]
    except Exception:
        return False


def deny(reason: str) -> int:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                              "permissionDecisionReason": reason}}, ensure_ascii=False))
    return 0


def listing(matches):
    return "\n".join(f"  {sha} {date} {subj[:90]}{' [' + refs + ']' if refs else ''}" for sha, date, subj, refs in matches[:12])


def main() -> int:
    if os.environ.get("CLAUDE_REPEAT_WORK_GATE", "").lower() in {"off", "0", "false"}:
        return 0
    data = json.loads(sys.stdin.read() or "{}")
    command = agentio.shell_command(data)
    if not COMMIT_RE.search(command) or "--dry-run" in command:
        return 0
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from _commit_detect import is_commit_command
        if not is_commit_command(command):
            return 0  # `git commit` only mentioned inside a string or heredoc
    except ImportError:
        return 0
    cwd = data.get("cwd") or os.getcwd()
    if not git(cwd, "rev-parse", "--git-dir").strip():
        return 0
    if ALL_RE.search(command.split("commit", 1)[-1]):
        staged = set(git(cwd, "diff", "HEAD", "--name-only").split("\n"))
    else:
        staged = set(git(cwd, "diff", "--cached", "--name-only").split("\n"))
    staged = meaningful(staged)
    if not staged:
        return 0
    matches = similar_commits(cwd, staged, subject_of(command))
    n = len(matches)
    if n < WARN_AT:
        return 0
    sid = str(data.get("session_id") or "")
    state = load_state()
    entry = state.setdefault(sid, {"warned": []})
    humans = human_count(data.get("transcript_path") or "")
    if n >= ESCALATE_AT:
        tripped = entry.get("tripped_at_humans")
        if tripped is None or humans == tripped:
            entry["tripped_at_humans"] = humans
            save_state(state)
            msg = DENY_ESCALATE % (n, LOOKBACK_DAYS, listing(matches))
            if agentio.client(data) == "codex":
                msg = msg.replace("Ask the operator with AskUserQuestion", "Ask the operator in a plain message")
            return deny(msg)
        entry.pop("tripped_at_humans", None)  # the operator wrote since the trip: allow, re-arm
        save_state(state)
        return 0
    sig = hashlib.sha1("\n".join(sorted(staged)).encode()).hexdigest()[:12]
    if sig in entry["warned"]:
        return 0
    entry["warned"].append(sig)
    save_state(state)
    return deny(DENY_WARN % (n, LOOKBACK_DAYS, listing(matches)))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        sys.stderr.write(traceback.format_exc())
        raise SystemExit(0)
