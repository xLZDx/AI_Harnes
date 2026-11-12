#!/usr/bin/env python3
"""Hook runner (S7-T7): `python run_hook.py <hook script name> [args]`.

Runs the named hook in this process exactly as `python <script>` would (stdin, stdout, stderr, exit code and
`__name__ == "__main__"` behave the same), and afterwards records ONE metrics row: hook, event, decision, duration.
The row holds no prompt, command text or secret (see hook_metrics.py). Recording happens after the hook finished and its
output was forwarded; any error in the runner's own bookkeeping is swallowed and never changes the hook's result.

decision: "deny" for exit code 2 or a PreToolUse/PermissionRequest deny, "block" for a Stop-style {"decision":"block"},
"error" for an uncaught exception, "allow" otherwise.
"""
from __future__ import annotations

import io
import json
import os
import runpy
import sys
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUDGET_S = 1.0  # hard limit for the runner's own bookkeeping after the hook result is already forwarded


def classify(code: int, out: str, crashed: bool) -> str:
    if crashed:
        return "error"
    if code == 2:
        return "deny"
    try:
        o = json.loads(out)
    except Exception:
        return "allow" if code == 0 else "error"
    if isinstance(o, dict):
        if o.get("decision") == "block":
            return "block"
        h = o.get("hookSpecificOutput")
        if isinstance(h, dict) and (h.get("permissionDecision") == "deny" or
                                     (isinstance(h.get("decision"), dict) and h["decision"].get("behavior") == "deny")):
            return "deny"
    return "allow" if code == 0 else "error"


def main() -> int:
    if len(sys.argv) < 2:
        sys.stderr.write("run_hook: missing hook script name\n")
        return 1
    name = os.path.basename(sys.argv[1])
    script = HERE / name
    if not script.is_file() or script.name == Path(__file__).name:
        sys.stderr.write(f"run_hook: no such hook script {name}\n")
        return 1
    raw = sys.stdin.buffer.read()
    if os.environ.get("HARNESS_HOOK_CHILD") == "1":
        # A helper model call started by a hook (citation_judge) is a text-only child, not a working session: the hooks
        # that STEER a session (Stop, SubagentStop, prompt and session-start context) must not drive it (a Stop hook once
        # made such a child implement and commit sprint tasks). Tool gates (PreToolUse, PermissionRequest) keep running,
        # so a leaked HARNESS_HOOK_CHILD can never switch a deny gate off. Every skip is logged.
        try:
            ev = str(json.loads(raw.decode("utf-8", "replace") or "{}").get("hook_event_name") or "")
        except Exception:
            ev = ""
        if ev in ("Stop", "SubagentStop", "UserPromptSubmit", "SessionStart", "PreCompact", "Notification"):
            try:
                import _agentio
                with open(_agentio.state_dir("run_hook") / "child_skips.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "hook": name, "event": ev}) + "\n")
            except Exception:
                pass
            return 0
    real_out, real_err = sys.stdout, sys.stderr
    out_buf, err_buf = io.BytesIO(), io.BytesIO()
    sys.stdin = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8", errors="replace")
    out_w = io.TextIOWrapper(out_buf, encoding="utf-8", write_through=True)  # keep references: a dropped wrapper closes its buffer
    err_w = io.TextIOWrapper(err_buf, encoding="utf-8", write_through=True)
    sys.stdout, sys.stderr = out_w, err_w
    client = "codex" if any(a == "--harness-client=codex" for a in sys.argv[2:]) else "claude"  # before a hook strips its own argv
    sys.argv = [str(script), *sys.argv[2:]]
    code, crashed = 0, False
    t0 = time.monotonic()
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as e:
        if e.code is None:
            code = 0
        elif isinstance(e.code, int):
            code = e.code
        else:
            err_buf.write((str(e.code) + "\n").encode("utf-8"))
            code = 1
    except BaseException:
        crashed, code = True, 1
        err_buf.write(traceback.format_exc().encode("utf-8", "replace"))
    ms = int((time.monotonic() - t0) * 1000)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    sys.stdout, sys.stderr = real_out, real_err
    out_bytes, err_bytes = out_buf.getvalue(), err_buf.getvalue()
    try:
        real_out.buffer.write(out_bytes)
        real_out.buffer.flush()
        real_err.buffer.write(err_bytes)
        real_err.buffer.flush()
    except Exception:
        pass
    decision = classify(code, out_bytes.decode("utf-8", "replace"), crashed)

    def bookkeeping() -> None:
        try:
            sys.path.insert(0, str(HERE))
            import hook_metrics
            try:
                data = json.loads(raw.decode("utf-8-sig", "replace") or "{}")
                data = data if isinstance(data, dict) else {}
            except Exception:
                data = {}
            if data.get("agent_type") and name != "agent_visibility.py":
                try:
                    import agent_visibility
                    agent_visibility.touch(data)  # a subagent's own tool call: proof the agent is active
                except Exception:
                    pass
            event = str(data.get("hook_event_name") or ("PreToolUse" if "tool_input" in data else "Stop"))
            hook_metrics.record(name, event, decision, "exit" + str(code), ms, str(data.get("session_id") or ""), client,
                                str(data.get("cwd") or ""), str(data.get("agent_type") or ""))
        except Exception:
            pass

    # bookkeeping last, with a hard time budget: it never changes the result and never delays it by more than BUDGET_S
    th = threading.Thread(target=bookkeeping, daemon=True)
    th.start()
    th.join(BUDGET_S)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
