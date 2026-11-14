#!/usr/bin/env python3
"""UserPromptSubmit hook -- reminds (never blocks) that this project tracks
a decision/evidence/refusal log, whenever core/DECISION_LOG.md exists in the
current repo. Complements decision_log_gate.py (commit-time hard check) for
turns that produce no commit at all -- cannot save a turn that ends with no
further prompt ever arriving; only mitigates "moved on and forgot" within an
ongoing session. Operator GO, 2026-08-09.

Session-repo fallback: a session opened at a multi-project container folder (not a git repo itself)
reports that folder as its cwd for every UserPromptSubmit event, so the reminder would never fire while real
work happens in a child repo. decision_log_gate.py (PreToolUse) writes
<state dir>/decision_log_gate/session_repo/<session_id>.json (a repo_root) the last time THIS session ran a
`git commit`; this hook reads it, but only when the direct cwd lookup fails and never over a real repo found
at the reported cwd. Session-scoped, so parallel sessions on sibling repos stay correct.

One file for Claude Code and Codex: both send the same UserPromptSubmit stdin and accept the same
hookSpecificOutput.additionalContext output. The marker folder is _agentio.state_dir("decision_log_gate").

Kill switch: CLAUDE_DECISION_LOG_REMINDER=off
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _agentio  # noqa: E402  shared Claude Code / Codex adapter

SESSION_REPO_DIR = _agentio.state_dir("decision_log_gate") / "session_repo"


def session_repo_fallback(session_id):
    if not session_id:
        return None
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(session_id))
    marker = SESSION_REPO_DIR / f"{safe}.json"
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        repo_root = data.get("repo_root")
        return repo_root if repo_root and Path(repo_root).is_dir() else None
    except Exception:
        return None


def main():
    if os.environ.get("CLAUDE_DECISION_LOG_REMINDER", "").lower() == "off":
        sys.exit(0)

    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            sys.exit(0)
    except Exception:
        sys.exit(0)

    cwd = payload.get("cwd") or os.getcwd()

    repo_root = None
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode == 0:
            repo_root = result.stdout.strip()
    except Exception:
        pass

    if repo_root is None:
        repo_root = session_repo_fallback(payload.get("session_id"))
        if repo_root is None:
            sys.exit(0)

    log_path = Path(repo_root) / "core" / "DECISION_LOG.md"
    if not log_path.is_file():
        sys.exit(0)

    context = (
        "[Decision log reminder] This project tracks decisions/evidence/refusals in "
        "core/DECISION_LOG.md (decision / evidence / refusal log per project). If your previous turn included a non-trivial "
        "decision, piece of evidence, or refusal not yet logged, add it now -- in the "
        "same commit as the related code where a commit applies."
    )

    # Lessons log (operator rule, global): a soft reminder only (never blocks,
    # per the NO SECONDARY BLOCKERS rule). Kill switch: CLAUDE_LESSONS_LOG_REMINDER=off
    if os.environ.get("CLAUDE_LESSONS_LOG_REMINDER", "").lower() != "off":
        lessons_path = Path(repo_root) / "core" / "LESSONS_LOG.md"
        if lessons_path.is_file():
            context += (
                " [Lessons log reminder] This project also keeps core/LESSONS_LOG.md "
                "(lessons log rule): every commit carries an entry in "
                "the same commit -- FAILURE / SHORTCOMING / SUCCESS with facts, root "
                "cause (or why it worked), which layer missed it (author, tests, "
                "reviewers, gate) and the rule to apply next time. Record your OWN "
                "mistakes at once, without being asked, and any defect a test, "
                "reviewer or the operator found. A FAILURE/SHORTCOMING entry with a Rule "
                "also reaches the global lessons store at commit and is then shown "
                "to every project before the matching action."
            )
        else:
            context += (
                " [Lessons log reminder] This project has no core/LESSONS_LOG.md yet: "
                "create it with the first dated entry (lessons log rule, every project)."
            )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
