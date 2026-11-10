"""One reader for hook input from Claude Code and Codex, so every hook works in both.

Claude Code and Codex send almost the same hook JSON on stdin. They differ in four places:
  * shell tool name: Claude "Bash" or "PowerShell"; Codex "Bash" (also for exec_command);
  * file edits: Claude Edit/Write/MultiEdit/NotebookEdit with tool_input.file_path;
    Codex "apply_patch" with the patch text in tool_input.command (or tool_input.input);
  * subagent spawn: Claude "Agent" / "Task" with tool_input.subagent_type; Codex "spawn_agent" with agent_type;
  * transcript JSONL: Claude {"type": "user"|"assistant", "message": {...}};
    Codex {"type": "response_item", "payload": {"type": "message"|"function_call"|...}}.
Hooks call these helpers instead of reading the raw fields. Stdlib only; never raises on bad input.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

def load_machine_config() -> None:
    """Machine-specific settings (paths, project lists) live outside git in ~/.harness-hooks/config.json
    (or HARNESS_HOOK_CONFIG): a flat {"ENV_NAME": "value"} map. Each value becomes an environment default, so a
    real environment variable still wins. See config.example.json for the keys."""
    path = Path(os.environ.get("HARNESS_HOOK_CONFIG") or Path.home() / ".harness-hooks" / "config.json")
    try:
        cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return
    if isinstance(cfg, dict):
        for k, v in cfg.items():
            # a value still holding a template placeholder ("<...>") is ignored, never used as a literal path
            if isinstance(k, str) and k.isupper() and isinstance(v, str) and v.strip() and not v.lstrip().startswith("<"):
                os.environ.setdefault(k, os.path.expandvars(os.path.expanduser(v)))


load_machine_config()


def _client_from_argv() -> str:
    """build.py writes `--harness-client=claude|codex` into every hook command; it is removed from sys.argv so the
    hook's own argument handling never sees it."""
    import sys
    for i, a in enumerate(list(sys.argv[1:]), start=1):
        if a.startswith("--harness-client="):
            del sys.argv[i]
            return a.split("=", 1)[1].strip().lower()
    return ""


CLIENT = _client_from_argv()

SHELL_TOOLS = {"Bash", "PowerShell", "exec_command", "shell", "local_shell"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"}
AGENT_TOOLS = {"Agent", "Task", "spawn_agent"}
QUESTION_TOOLS = {"AskUserQuestion", "request_user_input"}
PATCH_PATH_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$|^\*\*\* Move to: (.+?)\s*$", re.M)


def read_stdin() -> dict:
    import sys
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig", errors="replace")
        data = json.loads(raw) if raw.strip() else {}
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def client(data: dict) -> str:
    """'codex' or 'claude'. The installed command says which (--harness-client); without it only positive evidence
    in the payload counts (a Codex transcript path or a turn_id), never the environment."""
    if CLIENT in ("claude", "codex"):
        return CLIENT
    tp = str(data.get("transcript_path") or "").replace("\\", "/").lower()
    if "/.codex/" in tp or data.get("turn_id"):
        return "codex"
    return "claude"


def tool_name(data: dict) -> str:
    return str(data.get("tool_name") or "")


def tool_input(data: dict) -> dict:
    ti = data.get("tool_input")
    return ti if isinstance(ti, dict) else {}


def is_shell(data: dict) -> bool:
    return tool_name(data) in SHELL_TOOLS


def shell_command(data: dict) -> str:
    """The shell command text, or '' when the call is not a shell call."""
    if not is_shell(data):
        return ""
    ti = tool_input(data)
    cmd = ti.get("command", ti.get("cmd", ""))
    if isinstance(cmd, list):
        cmd = " ".join(str(x) for x in cmd)
    return str(cmd or "")


def shell_kind(data: dict) -> str:
    """'powershell' or 'bash' (Codex on Windows runs PowerShell under the Bash tool name)."""
    if tool_name(data) == "PowerShell":
        return "powershell"
    if client(data) == "codex" and os.name == "nt":
        return "powershell"
    return "bash"


def is_edit(data: dict) -> bool:
    return tool_name(data) in EDIT_TOOLS


def edited_paths(data: dict) -> list[str]:
    """Files an edit call writes: Claude file_path / notebook_path, Codex patch headers."""
    if not is_edit(data):
        return []
    ti = tool_input(data)
    if tool_name(data) == "apply_patch":
        patch = str(ti.get("command") or ti.get("input") or ti.get("patch") or "")
        return [a or b for a, b in PATCH_PATH_RE.findall(patch)]
    p = ti.get("file_path") or ti.get("notebook_path")
    return [str(p)] if p else []


def is_agent_spawn(data: dict) -> bool:
    return tool_name(data) in AGENT_TOOLS


def agent_type(data: dict) -> str:
    ti = tool_input(data)
    return str(ti.get("subagent_type") or ti.get("agent_type") or data.get("agent_type") or "")


def is_question(data: dict) -> bool:
    return tool_name(data) in QUESTION_TOOLS


def _text_of(content) -> str:
    """Text inside a message/tool-output content value of either client."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        for k in ("text", "content", "output"):
            if k in content:
                return _text_of(content[k])
        return ""
    if isinstance(content, list):
        return "\n".join(t for t in (_text_of(c) for c in content) if t)
    return ""


def _claude_records(obj: dict):
    t = obj.get("type")
    if t not in ("user", "assistant"):
        return
    msg = obj.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        yield {"kind": t, "text": content}
        return
    for b in content or []:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text":
            yield {"kind": t, "text": b.get("text") or ""}
        elif bt == "tool_use":
            yield {"kind": "tool_use", "name": b.get("name") or "", "input": b.get("input") or {}}
        elif bt == "tool_result":
            yield {"kind": "tool_result", "text": _text_of(b.get("content"))}


CODEX_INJECTED_RE = re.compile(r"^\s*<(environment_context|user_instructions|turn_aborted|subagent_notification"
                               r"|codex_internal_context)\b"
                               r"|^\s*# AGENTS\.md instructions", re.I)


def _codex_records(obj: dict):
    if obj.get("type") != "response_item":
        return
    p = obj.get("payload") or {}
    pt = p.get("type")
    if pt == "message":
        role = p.get("role")
        text = _text_of(p.get("content"))
        if role == "user" and not CODEX_INJECTED_RE.search(text):
            yield {"kind": "user", "text": text}
        elif role == "assistant":
            yield {"kind": "assistant", "text": text}
    elif pt in ("function_call", "custom_tool_call", "local_shell_call"):
        args = p.get("arguments", p.get("input", p.get("action", {})))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {"input": args}
        yield {"kind": "tool_use", "name": p.get("name") or pt, "input": args if isinstance(args, dict) else {}}
    elif pt in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
        yield {"kind": "tool_result", "text": _text_of(p.get("output"))}


def records(transcript_path) -> list[dict]:
    """Every record of a transcript as {kind: user|assistant|tool_use|tool_result, text|name|input}.
    A Claude user message that only carries tool results is not a 'user' record."""
    out: list[dict] = []
    try:
        path = Path(transcript_path)
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                try:
                    out.extend(_codex_records(obj) if obj.get("type") == "response_item" else _claude_records(obj))
                except (AttributeError, TypeError, ValueError):
                    continue  # one malformed row must not hide the rest of the transcript
    except (OSError, TypeError, ValueError):
        return []
    return out


def current_turn(transcript_path) -> list[dict]:
    """Records after the last genuine user message (the user message itself included first)."""
    recs = records(transcript_path)
    for i in range(len(recs) - 1, -1, -1):
        if recs[i]["kind"] == "user" and recs[i].get("text", "").strip():
            return recs[i:]
    return recs


def turn_tool_output(transcript_path) -> str:
    return "\n".join(r.get("text", "") for r in current_turn(transcript_path) if r["kind"] == "tool_result")


def turn_tool_calls(transcript_path) -> list[dict]:
    return [r for r in current_turn(transcript_path) if r["kind"] == "tool_use"]


def last_user_text(transcript_path) -> str:
    for r in reversed(records(transcript_path)):
        if r["kind"] == "user" and r.get("text", "").strip():
            return r["text"]
    return ""


def last_assistant_text(data: dict) -> str:
    msg = data.get("last_assistant_message")
    if isinstance(msg, str) and msg:
        return msg
    for r in reversed(current_turn(data.get("transcript_path") or "")):
        if r["kind"] == "assistant":
            return r.get("text", "")
    return ""


def is_writer(data: dict) -> bool:
    """True for a subagent whose report the SubagentStop evidence hooks skip (names in evidence_skip.json next to
    this file, written by build.py from registry.json "subagent_evidence_skip"). A missing or broken file means
    'check it'."""
    name = str(data.get("agent_type") or "")
    try:
        names = json.loads((Path(__file__).resolve().parent / "evidence_skip.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(names, list) and name in names


class file_lock:
    """Cross-process lock for a read-modify-write of a shared state file (Claude and Codex sessions share state).
    `with file_lock(path) as got:` waits up to `wait` seconds; a lock older than `stale` seconds is taken over.
    got=False means the lock was not obtained; the caller decides (hooks fail open and log it)."""

    def __init__(self, path: Path, wait: float = 3.0, stale: float = 30.0):
        self.lock = Path(str(path) + ".lock")
        self.wait, self.stale, self.got = wait, stale, False

    def __enter__(self) -> bool:
        import time
        end = time.monotonic() + self.wait
        while True:
            try:
                fd = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self.got = True
                return True
            except FileExistsError:
                try:
                    if time.time() - self.lock.stat().st_mtime > self.stale:
                        self.lock.unlink()
                        continue
                except OSError:
                    pass
            except PermissionError:  # Windows: the lock file is being deleted by its owner
                pass
            except OSError:
                return False
            if time.monotonic() >= end:
                return False
            time.sleep(0.05)

    def __exit__(self, *exc) -> None:
        if self.got:
            for _ in range(20):
                try:
                    self.lock.unlink()
                    return
                except FileNotFoundError:
                    return
                except PermissionError:
                    import time
                    time.sleep(0.05)
                except OSError:
                    return


def state_dir(name: str) -> Path:
    """Per-hook state folder; HARNESS_HOOK_STATE overrides the default under the user's home."""
    base = Path(os.environ.get("HARNESS_HOOK_STATE") or Path.home() / ".harness-hooks")
    d = base / name
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d
