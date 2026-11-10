#!/usr/bin/env python3
"""Worktree policy hook (operator 2026-10-21): NO LOCAL WORKTREES. One script, three events.

  PreToolUse (Bash|PowerShell): `git worktree add` is DENIED, except a temporary scratch worktree whose path is under
      SCRATCH_ROOT (default D:\\tmp\\scratch_worktrees). Allowed scratch worktrees are registered in a ledger with the
      creating session, path and starting HEAD. Commands that run on a remote host (ssh ...) are not covered.
  Stop: a registered scratch worktree created by THIS session whose branch has own commits and is already merged into
      the default branch must be removed before the turn ends (blocks once per message).
  UserPromptSubmit: non-blocking reminder about scratch worktrees (any session) that are merged or older than 24 h.

Removal itself stays with the assistant, after the checks in CLAUDE.md section 9 (merged, clean incl. ignored, no process
in the folder, no junctions). This hook never deletes anything.
Claude Code and Codex (shell calls via _agentio; Codex has the same Stop / UserPromptSubmit events).
State lives under _agentio.state_dir("worktree_policy"); scratch root defaults to a sibling "scratch_worktrees"
folder there (override: CLAUDE_WORKTREE_SCRATCH_ROOT, CLAUDE_WORKTREE_POLICY_STATE).
Kill switch: CLAUDE_WORKTREE_POLICY=off. Stdlib only. Blocking only for `git worktree add` and the merged-but-present Stop check.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _agentio as agentio  # noqa: E402

STATE = Path(os.environ.get("CLAUDE_WORKTREE_POLICY_STATE") or agentio.state_dir("worktree_policy"))
# Set CLAUDE_WORKTREE_SCRATCH_ROOT to put scratch worktrees elsewhere (for example a short path on another drive).
SCRATCH_ROOT = os.environ.get("CLAUDE_WORKTREE_SCRATCH_ROOT") or str(STATE.parent / "scratch_worktrees")
LEDGER = STATE / "scratch.jsonl"
ADD_RE = re.compile(r"\bgit\b(?P<pre>[^;&|\n]*?)\bworktree\s+add\b(?P<rest>[^;&|\n]*)", re.I)
OPT_WITH_VALUE = {"-b", "-B", "--reason"}


def norm(p):
    p = str(p).strip().strip("\"'")
    m = re.match(r"^/([a-zA-Z])/(.*)$", p)          # git-bash style /d/tmp/x
    if m:
        p = f"{m.group(1).upper()}:/{m.group(2)}"
    return os.path.normcase(os.path.normpath(p))


def under(path, root):
    path, root = norm(path), norm(root)
    return path == root or path.startswith(root + os.sep)


def parse_add(cmd, cwd):
    """Return (target_path or None, found) for the first `git worktree add` in cmd."""
    m = ADD_RE.search(cmd)
    if not m:
        return None, False
    base = cwd or ""
    c = re.search(r"(?:^|\s)-C\s+(\"[^\"]+\"|'[^']+'|\S+)", m.group("pre"))
    if c:
        cdir = c.group(1).strip("\"'")
        base = cdir if os.path.isabs(cdir) or re.match(r"^/[a-zA-Z]/", cdir) else os.path.join(base, cdir)
    toks = re.findall(r"\"[^\"]+\"|'[^']+'|\S+", m.group("rest"))
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in OPT_WITH_VALUE:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        target = t.strip("\"'")
        if os.path.isabs(target) or re.match(r"^/[a-zA-Z]/", target):
            return target, True
        return (os.path.join(base, target) if base else None), True
    return None, True


def git(args, cwd=None):
    try:
        r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=20)
        return r.returncode, r.stdout.strip()
    except Exception:
        return 1, ""


def ledger_rows():
    rows = []
    try:
        for ln in LEDGER.read_text(encoding="utf-8").splitlines():
            rows.append(json.loads(ln))
    except Exception:
        pass
    return rows


def ledger_add(row):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        row["ts"] = datetime.now(timezone.utc).isoformat()
        with LEDGER.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def default_ref(path):
    rc, out = git(["symbolic-ref", "-q", "refs/remotes/origin/HEAD"], cwd=path)
    if rc == 0 and out:
        return out.replace("refs/remotes/", "")
    for ref in ("origin/main", "origin/master", "main", "master"):
        if git(["rev-parse", "--verify", "-q", ref], cwd=path)[0] == 0:
            return ref
    return None


def merged_present(row):
    """True if the scratch worktree still exists, has own commits, and its HEAD is already in the default branch."""
    path = row.get("path")
    if not path or not os.path.isdir(path):
        return False
    rc, head = git(["rev-parse", "HEAD"], cwd=path)
    if rc != 0 or not head or head == row.get("start_head"):
        return False
    ref = default_ref(path)
    return bool(ref) and git(["merge-base", "--is-ancestor", head, ref], cwd=path)[0] == 0


def deny(msg):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": msg}}, ensure_ascii=False))
    sys.exit(0)


DENY_MSG = """NO LOCAL WORKTREES (operator 2026-10-21, CLAUDE.md section 9): `git worktree add` is not allowed here.
Work in the project's own checkout on a branch. If a TEMPORARY scratch worktree is really needed, create it under
%s (e.g. %s\\<short-name>). It is registered, and after its branch is merged it MUST be removed (this session will be
stopped from finishing until it is). Remote hosts (ssh) are not covered by this rule.
Reason for this denial: %s"""


def pre(data):
    cmd = agentio.shell_command(data)
    if not cmd or not ADD_RE.search(cmd):
        return
    if re.match(r"^\s*ssh\b", cmd):
        return
    target, _ = parse_add(cmd, data.get("cwd"))
    if target is None:
        deny(DENY_MSG % (SCRATCH_ROOT, SCRATCH_ROOT, "the target path could not be determined; use an absolute path"))
    if not under(target, SCRATCH_ROOT):
        deny(DENY_MSG % (SCRATCH_ROOT, SCRATCH_ROOT, f"target {target} is outside the scratch root"))
    rc, head = git(["rev-parse", "HEAD"], cwd=data.get("cwd"))
    ledger_add({"sid": str(data.get("session_id")), "path": norm(target), "start_head": head if rc == 0 else "",
                "cmd": cmd[:200]})


STOP_MSG = """Scratch worktree already merged but still present (operator rule 2026-10-21: a temporary worktree MUST be
removed after its merge): %s
Remove it now, after the checks from CLAUDE.md section 9: branch merged (verified), `git status --ignored` clean,
no running process with that folder as cwd, no junctions pointing outside it. If any check fails, ask the operator
with AskUserQuestion instead of removing."""


def stop(data):
    if data.get("agent_type") or data.get("stop_hook_active"):
        return
    sid = str(data.get("session_id"))
    due = [r["path"] for r in ledger_rows() if r.get("sid") == sid and merged_present(r)]
    if not due:
        return
    key = hashlib.sha1(((data.get("last_assistant_message") or "") + "|".join(due)).encode("utf-8", "replace")).hexdigest()[:16]
    seen_file = STATE / "stop_seen.json"
    try:
        seen = json.loads(seen_file.read_text(encoding="utf-8"))
    except Exception:
        seen = []
    if key in seen:
        return
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        seen_file.write_text(json.dumps((seen + [key])[-50:]), encoding="utf-8")
    except Exception:
        pass
    print(json.dumps({"decision": "block", "reason": STOP_MSG % ", ".join(due)}, ensure_ascii=False))
    sys.exit(0)


def prompt(data):
    notes = []
    now = datetime.now(timezone.utc)
    for r in ledger_rows():
        path = r.get("path")
        if not path or not os.path.isdir(path):
            continue
        try:
            age_h = (now - datetime.fromisoformat(r["ts"])).total_seconds() / 3600
        except Exception:
            age_h = 0
        if merged_present(r):
            notes.append(f"{path} (merged, still present)")
        elif age_h > 24:
            notes.append(f"{path} (scratch worktree older than 24 h)")
    if notes:
        ctx = ("[Worktree policy] Scratch worktrees that must be removed after their merge: " + "; ".join(notes[:10]) +
               ". Remove them after the CLAUDE.md section 9 checks.")
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx}},
                         ensure_ascii=False))
        sys.exit(0)


def main():
    if os.environ.get("CLAUDE_WORKTREE_POLICY", "").lower() == "off":
        return
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return
    ev = data.get("hook_event_name") or ("PreToolUse" if "tool_input" in data else
                                         "UserPromptSubmit" if "prompt" in data else "Stop")
    if ev == "PreToolUse" and agentio.is_shell(data):
        pre(data)
    elif ev == "Stop":
        stop(data)
    elif ev == "UserPromptSubmit":
        prompt(data)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        pass  # fail open on internal errors
    sys.exit(0)
