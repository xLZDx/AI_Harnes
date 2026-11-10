#!/usr/bin/env python3
"""PreToolUse hook -- blocks `git commit` (via Bash/PowerShell) unless
core/DECISION_LOG.md will be part of the resulting commit. Operator GO,
2026-08-09.

Scope, honestly (see ~/.claude/CLAUDE.md "Continuous Decision/Evidence/
Refusal Log Per Project" -> Write authority):
- Catches git commit invoked through Claude Code's Bash/PowerShell tools.
  Does NOT catch commits made outside Claude Code (not a concern for this
  operator -- confirmed all commits go through Claude Code), nor
  git merge / rebase --continue / cherry-pick (no literal "git commit" in
  the command), nor a command that `cd`s into another directory first
  (repo root is resolved from the hook's own reported cwd).
- Fails open on any internal error (git missing, timeout, bad stdin) --
  logged, never silent, never crashes the session.

Staging check handles the realistic combined-command case
(`git add file1 core/DECISION_LOG.md && git commit -m "..."`) where the
PreToolUse hook fires BEFORE any part of the chain has run: allows when the
file is already staged, OR the command's own `git add` portion explicitly
names it, OR a broad add (`-A`/`--all`/`.`/`-u`) or `git commit -a` is used
together with the file actually differing from HEAD in the working tree.

Kill switch: CLAUDE_DECISION_LOG_GATE=off
Works for Claude Code and Codex (the shell command is read through _agentio).
State folder: _agentio.state_dir("decision_log_gate") (HARNESS_HOOK_STATE override).
Decision log: <state folder>/decisions.jsonl

Session-repo marker (2026-08-16 addendum): whenever this hook resolves a real
repo_root for a `git commit` command, it also drops
<state folder>/session_repo/<session_id>.json --
{"repo_root": ..., "ts": ...}. This is the only hook that derives repo_root
from the command's own `cd <dir> && ...` prefix rather than trusting the
session's static reported cwd (see resolve_effective_cwd -- a naive
cwd-only version fail-opened on exactly this pattern). decision_log_reminder.py
(UserPromptSubmit) has no command to parse a `cd` prefix out of and falls back
to reading this marker when the session's own cwd is not itself a git repo --
e.g. a session opened at a container folder root rather than inside one
child project.
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _agentio as agentio  # noqa: E402

STATE_DIR = agentio.state_dir("decision_log_gate")
LOG_FILE = STATE_DIR / "decisions.jsonl"
SESSION_REPO_DIR = STATE_DIR / "session_repo"

COMMIT_RE = re.compile(r"\bgit\s+commit\b", re.IGNORECASE)
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _commit_detect import is_commit_command  # `git commit` really executed, not just mentioned in text
except Exception:  # fail open to the old text match if the helper is missing
    def is_commit_command(command):
        return True
SKIP_RE = re.compile(r"--help\b|(?<!\S)-h\b|--dry-run\b", re.IGNORECASE)
NAMED_ADD_RE = re.compile(r"git\s+add\b[^&|;]*DECISION_LOG\.md", re.IGNORECASE)
BROAD_ADD_RE = re.compile(r"git\s+add\s+(-A\b|--all\b|\.\s|\.$|-u\b)", re.IGNORECASE)
COMMIT_ALL_RE = re.compile(r"git\s+commit\s+[^&|;]*(-a\b|--all\b)", re.IGNORECASE)
CD_RE = re.compile(r'^\s*cd\s+(?P<path>"[^"]+"|\'[^\']+\'|\S+)\s*$')

# Git-Bash spells drives as `/d/Repo/x`. Windows Python does NOT read that as absolute --
# `os.path.isabs('/d/Repo/x')` is False here -- so the old resolver fell through to
# `Path(base_cwd) / path`, and pathlib keeps the LEFT side's drive for a rooted-but-driveless
# right-hand side: `Path('D:/Repo') / '/d/Repo/x'` -> `D:\d\Repo\x`. That directory does not
# exist, `git rev-parse` failed, and this gate fail-opened silently on every command issued
# through the Bash tool with a POSIX path.
#
# Measured 2026-08-16 before the fix, from this hook's own decisions log: 13 fail-opens for
# `D:\d\Repo\db-test-tool-analysis`, 9 for `_wt-formcoach`, 6 for `test 2\AI trading assistance`.
# A gate that never runs is worse than no gate: it reports nothing while looking enforced.
_GITBASH_DRIVE_RE = re.compile(r"^/([A-Za-z])(?=/|$)")


def normalise_shell_path(path):
    """`/d/Repo/x` -> `D:\\Repo\\x`; everything else unchanged."""
    m = _GITBASH_DRIVE_RE.match(path)
    if not m:
        return path
    rest = path[2:].replace("/", "\\")
    return f"{m.group(1).upper()}:{rest or chr(92)}"


def resolve_effective_cwd(command, base_cwd):
    """The hook's reported `cwd` is the session's static working directory --
    it does NOT track a `cd <dir> && ...` prefix inside the command itself.
    That prefix is the dominant real-world pattern (proven live: a commit
    inside `cd <repo> && git commit ...` fail-opened because the hook checked
    the session cwd, found no git repo there, and let the commit through
    unguarded). Walk the &&/;-joined parts and apply any `cd` segments."""
    cwd = base_cwd
    for part in re.split(r"&&|;", command):
        m = CD_RE.match(part.strip())
        if not m:
            continue
        path = normalise_shell_path(m.group("path").strip("\"'"))
        if os.path.isabs(path) or re.match(r"^[A-Za-z]:", path) or path.startswith(("/", "\\")):
            cwd = path
        else:
            cwd = str(Path(cwd) / path)
    return cwd


def find_decision_log(cwd, repo_root):
    """Locate core/DECISION_LOG.md for the project this commit belongs to. The
    literal 'core' dirname convention (Plan Persistence rule) is per-PROJECT,
    but a project root is not always the git repo root -- a repo can hold one
    project nested a level (or more) below its top-level. Proven live:
    repo_root=D:/test 2/db-test-tool-analysis, log at
    db-test-tool-analysis/db-testing-tool/core/DECISION_LOG.md -- the old
    hardcoded `repo_root/core/DECISION_LOG.md` assumption never matched that
    layout, so the gate saw the file as permanently 'missing' no matter what
    was staged. Walk up from the effective cwd to repo_root looking for an
    existing core/DECISION_LOG.md; if none exists yet anywhere in that chain
    (first-ever log for this project), default to cwd's own core/DECISION_LOG.md
    so a brand-new log gets created next to the project being worked on, not
    forced to the repo root. Returns (abs_path, path_relative_to_repo_root,
    posix-style)."""
    cur = Path(cwd).resolve()
    root = Path(repo_root).resolve()
    chain = [cur]
    p = cur
    while p != root and p.parent != p:
        p = p.parent
        chain.append(p)
        if p == root:
            break
    for candidate_dir in chain:
        candidate = candidate_dir / "core" / "DECISION_LOG.md"
        if candidate.is_file():
            try:
                rel = candidate.relative_to(root)
            except ValueError:
                continue
            return candidate, str(rel).replace("\\", "/")
    default = cur / "core" / "DECISION_LOG.md"
    try:
        rel = default.relative_to(root)
    except ValueError:
        rel = Path("core") / "DECISION_LOG.md"
    return default, str(rel).replace("\\", "/")


def log_decision(entry):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        entry["ts"] = datetime.now(timezone.utc).isoformat()
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def write_session_repo_marker(session_id, repo_root):
    if not session_id:
        return
    try:
        SESSION_REPO_DIR.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(session_id))
        entry = {"repo_root": repo_root, "ts": datetime.now(timezone.utc).isoformat()}
        with open(SESSION_REPO_DIR / f"{safe}.json", "w", encoding="utf-8") as f:
            json.dump(entry, f)
    except Exception:
        pass


def allow():
    sys.exit(0)


def deny(reason, extra=None):
    log_decision({"decision": "deny", "reason": reason, **(extra or {})})
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def git(args, cwd=None, timeout=10):
    return subprocess.run(
        ["git"] + (["-C", cwd] if cwd else []) + args,
        capture_output=True, text=True, timeout=timeout,
        creationflags=_NO_WINDOW,
    )


def main():
    if os.environ.get("CLAUDE_DECISION_LOG_GATE", "").lower() == "off":
        allow()

    try:
        payload = json.loads(sys.stdin.read())
    except Exception as e:
        log_decision({"decision": "fail-open", "reason": f"bad stdin: {e}"})
        allow()

    command = agentio.shell_command(payload)
    if not COMMIT_RE.search(command) or not is_commit_command(command) or SKIP_RE.search(command):
        allow()

    raw_cwd = payload.get("cwd") or os.getcwd()
    cwd = resolve_effective_cwd(command, raw_cwd)

    try:
        toplevel = git(["rev-parse", "--show-toplevel"], cwd=cwd)
        if toplevel.returncode != 0:
            log_decision({"decision": "fail-open", "reason": "not a git repo or git error",
                          "cwd": cwd, "stderr": toplevel.stderr.strip()[:300]})
            allow()
        repo_root = toplevel.stdout.strip()
    except Exception as e:
        log_decision({"decision": "fail-open", "reason": f"git rev-parse failed: {e}", "cwd": cwd})
        allow()

    write_session_repo_marker(payload.get("session_id"), repo_root)

    log_path, log_rel = find_decision_log(cwd, repo_root)

    try:
        staged = git(["diff", "--cached", "--name-only"], cwd=repo_root)
        if staged.returncode != 0:
            log_decision({"decision": "fail-open", "reason": "git diff --cached failed",
                          "repo": repo_root, "stderr": staged.stderr.strip()[:300]})
            allow()
        staged_norm = {p.replace("\\", "/") for p in staged.stdout.strip().splitlines()}
    except Exception as e:
        log_decision({"decision": "fail-open", "reason": f"git diff --cached failed: {e}", "repo": repo_root})
        allow()

    if log_rel in staged_norm:
        log_decision({"decision": "allow", "reason": "already staged", "repo": repo_root,
                      "log_rel": log_rel, "command": command[:200]})
        allow()

    if NAMED_ADD_RE.search(command):
        log_decision({"decision": "allow", "reason": "named in git add", "repo": repo_root,
                      "log_rel": log_rel, "command": command[:200]})
        allow()

    if BROAD_ADD_RE.search(command) or COMMIT_ALL_RE.search(command):
        # git diff HEAD only shows tracked-modified/staged-new files -- it misses a
        # brand-new untracked file, which `git add -A`/`-A` would still pick up.
        # git status --porcelain covers modified-tracked AND untracked-new alike.
        try:
            status = git(["status", "--porcelain", "--", log_rel], cwd=repo_root)
            touched = bool(status.stdout.strip())
        except Exception:
            touched = False
        if touched:
            log_decision({"decision": "allow", "reason": "broad add + file touched in working tree",
                          "repo": repo_root, "log_rel": log_rel, "command": command[:200]})
            allow()

    exists = log_path.is_file()
    reason = (
        f"DECISION_LOG gate: {log_rel} is "
        f"{'not staged for' if exists else 'missing from'} this commit in {repo_root}. "
        f"Per ~/.claude/CLAUDE.md 'Continuous Decision/Evidence/Refusal Log Per Project': "
        + ("add a dated entry covering this commit's work" if exists
           else f"create {log_rel} with a first dated entry covering this commit's work")
        + f", `git add {log_rel}`, then retry. "
        f"CLAUDE_DECISION_LOG_GATE=off is a session/process-level kill switch, NOT a "
        f"per-command one: it must be set in the environment BEFORE this Claude Code "
        f"session/process starts, and it then disables the gate for every commit in "
        f"the session, not just this one. Setting it inline in this Bash/PowerShell "
        f"command has no effect -- this hook runs as its own process and has already "
        f"decided before that command ever executes."
    )
    deny(reason, {"repo": repo_root, "log_rel": log_rel, "existed": exists, "command": command[:200]})


if __name__ == "__main__":
    main()
