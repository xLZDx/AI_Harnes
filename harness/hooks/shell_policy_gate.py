#!/usr/bin/env python3
"""Combined PreToolUse policy gate for Bash and PowerShell (Claude Code) and the shell tool (Codex).

Works for both clients through _agentio (Codex runs PowerShell under the "Bash" tool name on Windows).
The deny output (hookSpecificOutput.permissionDecision "deny") is supported by both.

Policies:
1. block high-confidence destructive/external-side-effect command shapes;
2. restrict shell-capable review-only subagents to a narrow role-aware allowlist.

Important Claude Code contract: command PreToolUse hooks are *not* intrinsically fail-closed.
Exit 1 and hook timeouts are non-blocking. This script therefore converts sibling-import and
runtime policy errors to exit 2 once the script itself has started. A failure to launch this
script, a syntax error in this file itself, or a timeout is still fail-open by Claude Code's
documented hook semantics; static deny rules remain defense in depth, not a complete sandbox.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback

_IMPORT_ERROR: str | None = None
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import dangerous_command_gate as dangerous
    import reviewer_shell_gate as reviewer
    import _agentio as agentio
except BaseException:
    dangerous = None  # type: ignore[assignment]
    reviewer = None  # type: ignore[assignment]
    _IMPORT_ERROR = traceback.format_exc()


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }, ensure_ascii=False))


_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][\w.-]*)\1")
_WRAPPER_WORDS = {"sh", "bash", "zsh", "ksh", "dash", "pwsh", "powershell", "powershell.exe", "cmd", "cmd.exe", "eval", "source", ".", "xargs",
                  "wsl", "wsl.exe", "ssh", "su", "runuser", "docker", "podman", "kubectl", "nsenter", "chroot"}
# A quoted span right after a -c / -e / -Command style flag is code for another shell: keep it raw.
_CODE_FLAG_RE = re.compile(r"(?:^|\s)(?:-[A-Za-z]*[ce]|--command|-Command|-EncodedCommand|/[ckCK])\s+$")


def _is_wrapper(raw: str) -> bool:
    """True when the segment runs its quoted argument as shell code (bash -c "...", cmd /c "...", eval "...", bash <<EOF):
    its quoted text IS a command and must stay visible to the destructive-command checks."""
    seg = raw.strip()
    while seg:  # same prefix/leader stripping as dangerous.segments()
        nxt = dangerous.LEADERS.sub("", dangerous.PREFIXES.sub("", seg, count=1).strip(), count=1).strip()
        if nxt == seg:
            break
        seg = nxt
    if dangerous.SHELL_C.match(seg) or dangerous.EVAL_WRAPPER.match(seg):
        return True
    first = seg.split(None, 1)[0].lower().rsplit("/", 1)[-1].rsplit("\\", 1)[-1] if seg.split() else ""
    return first in _WRAPPER_WORDS


def _sanitize(cmd: str) -> str:
    """The command with DATA made inert, so a destructive word counts only as the command word of a pipeline segment.
    Outside quotes nothing changes. Inside a quoted string the separators ; | & and newlines become spaces (the string stays one
    argument, an echo/printf/git commit -m text can no longer be split into fake "commands"); a double-quoted string that holds
    $( ) or a backtick is kept raw (it really runs code). Heredoc bodies are dropped. A segment that runs its argument as shell
    code (bash -c, cmd /c, eval, bash <<EOF) is kept raw, quotes and heredoc body included. Any error returns the raw command."""
    try:
        segs: list[list] = []  # [raw, san, body, separator]
        raw: list[str] = []
        san: list[str] = []
        body: list[str] = []
        pending: list[str] = []
        i, n = 0, len(cmd)

        def flush(sep: str) -> None:
            segs.append(["".join(raw), "".join(san), "".join(body), sep])
            raw.clear(); san.clear(); body.clear()

        while i < n:
            ch = cmd[i]
            if ch == "\\" and i + 1 < n:
                raw.append(cmd[i:i + 2]); san.append(cmd[i:i + 2]); i += 2
                continue
            if ch in "'\"":
                # The span ends at the first matching quote, backslash or not: `"C:\proj\" && git clean -fdx` must not swallow the
                # command. A backslash-escaped quote could only keep a string open that already closes, so when in doubt the span
                # stays short and the old quote-blind scan still sees what follows.
                j = cmd.find(ch, i + 1)
                if j < 0:
                    j = n
                span = cmd[i:j + 1] if j < n else cmd[i:]
                i = j + 1 if j < n else n
                keep = (ch == '"' and ("$(" in span or "`" in span)) or bool(_CODE_FLAG_RE.search("".join(raw)))
                raw.append(span); san.append(span if keep else re.sub(r"[;|&\r\n]", " ", span))
                continue
            if cmd.startswith("<<", i) and not cmd.startswith("<<<", i):
                m = _HEREDOC_RE.match(cmd, i)
                if m:
                    pending.append(m.group(2))
                    raw.append(m.group(0)); san.append(m.group(0)); i = m.end()
                    continue
            if ch == "\n" and pending:
                i += 1
                for delim in pending:
                    while i < n:
                        end = cmd.find("\n", i)
                        line = cmd[i:] if end < 0 else cmd[i:end]
                        i = n if end < 0 else end + 1
                        if line.strip() == delim:
                            break
                        body.append(line + "\n")
                pending.clear()
                flush("\n")
                continue
            m = dangerous.SEPARATORS.match(cmd, i)
            if m and m.group():
                flush(m.group()); i = m.end()
                continue
            raw.append(ch); san.append(ch); i += 1
        flush("")
        out = []
        for r, s, b, sep in segs:
            out.append((r + ("\n" + b if b else "") if _is_wrapper(r) else s) + sep)
        return "".join(out)
    except Exception:  # noqa: BLE001  never weaken the gate by failing: fall back to the raw command
        return cmd


def _evaluate(data: dict) -> int:
    if _IMPORT_ERROR is not None:
        raise RuntimeError("shell policy sibling module failed to import:\n" + _IMPORT_ERROR)
    # Claude: Bash / PowerShell. Codex: Bash (PowerShell on Windows), exec_command, shell.
    if not agentio.is_shell(data):
        return 0
    tool_name = agentio.tool_name(data)
    if tool_name not in {"Bash", "PowerShell"}:
        # reviewer allowlist keys on the two Claude tool names; Codex shell calls map to the shell kind
        tool_name = "PowerShell" if agentio.shell_kind(data) == "powershell" else "Bash"
    command = agentio.shell_command(data) or agentio.tool_input(data).get("script") or ""
    if not isinstance(command, str) or not command.strip():
        return 0

    hits = dangerous.find_hits(_sanitize(command))
    if hits or os.environ.get("CLAUDE_SHELL_POLICY_AUDIT", "").lower() in {"1", "true", "on"}:
        dangerous.log({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "tool": tool_name,
            "agent_type": data.get("agent_type"),
            "cwd": data.get("cwd"),
            "command": command[:600],
            "hits": hits,
            "verdict": "deny" if hits else "pass",
        })
    if hits:
        detail = "; ".join(f"{h['reason']} -> `{h['segment']}`" for h in hits)
        deny(
            "Blocked by shell_policy_gate: " + detail + ". "
            "Use a non-destructive alternative, or obtain explicit operator authorization for the exact destructive action."
        )
        return 0

    agent_type = str(data.get("agent_type") or "")
    if agent_type in reviewer.REVIEWERS and not reviewer.allowed(command, tool_name, agent_type):
        dangerous.log({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "tool": tool_name,
            "agent_type": agent_type,
            "cwd": data.get("cwd"),
            "command": command[:600],
            "hits": [{"segment": command[:300], "reason": "review-only shell policy"}],
            "verdict": "deny",
        })
        deny(
            f"Review-only agent `{agent_type}` may use {tool_name} only for role-relevant inspection, "
            "tests/static checks, and narrowly allowed read-only database/infra/profiling commands. "
            "This command is outside that policy; hand it to the parent/implementer if required."
        )
    return 0


def main() -> int:
    try:
        data = json.loads(sys.stdin.read() or "{}")
        if not isinstance(data, dict):
            raise ValueError("hook input must be a JSON object")
        return _evaluate(data)
    except Exception:
        # For a policy hook, exit 2 is the documented process-level blocking signal. Use it
        # for internal errors rather than relying on a JSON decision that might never be emitted.
        traceback.print_exc(file=sys.stderr)
        try:
            if dangerous is not None:
                dangerous.log({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "verdict": "hook-error-block",
                    "traceback": traceback.format_exc(),
                })
        except Exception:
            pass
        print("Shell policy hook failed internally; blocking this shell call until the hook is fixed.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
