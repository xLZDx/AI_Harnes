#!/usr/bin/env python3
"""Stop + PostToolUse hook: close a session only when the project plan has no open tasks; owe a program report after every PR.

Source of truth is PROJECT_PLAN.md in the project folder (see tools/project_plan.py). Two jobs:

  Stop:
    * Find PROJECT_PLAN.md by walking up from the session cwd. No plan -> log "no-plan" and allow.
    * Active sprint has OPEN tasks (NOT_STARTED / IN_PROGRESS / IN_REVIEW / PARTIAL) -> block (approved plans, live mode)
      or log "would-block" (everything else). Tasks blocked by the operator / an external party / a prohibition must be
      BLOCKED_* in the plan, so they never count. A plan that cannot be trusted (parse problems, empty active sprint,
      unknown status) is reported, never treated as "closed".
    * Enforcement is progress-based, NOT the old "8 blocks then silent release": the no-progress counter resets whenever
      the number of open tasks or git HEAD changes. After MAX_NO_PROGRESS blocks without progress the hook ESCALATEs
      (logged + systemMessage, allowed). It does not treat stop_hook_active as permission to stop.
    * Active sprint closed -> for an approved plan the plan document and its HTML must be committed and pushed
      (operator 2026-11-01: all documents live in the project folder, committed and pushed); the same counter/ESCALATE
      applies so a push that is impossible can never trap the session. The next sprint needs the operator's approval.
    * After `gh pr create|merge` (PostToolUse) a whole-program report is owed: Stop wants a fresh
      reports/*PROJECT_PLAN*.ru.html (or *PROGRAM_STATUS*.ru.html) newer than the PR event; blocks once per PR
      (approved plans, live mode).

Modes (env SESSION_CLOSE_GATE_MODE): live (default), dry-run (log only), off.
Blocking applies only to plans whose front matter says `operator_approved: true` (the operator confirmed the plan);
every other plan is only logged. Fails open on any error, but never silently: state-write failures, unverifiable git
state and ESCALATE are logged and shown with a systemMessage. Stdlib only.
Log and state: _agentio.state_dir("session_close") (session_close_gate.jsonl, session_close_state.json).
Plan tool: tools/project_plan.py next to the hooks folder, or the path in SESSION_CLOSE_PLAN_TOOL; a missing tool is
logged ("no-tool") and the session is not gated.
One file for Claude Code and Codex: shell commands are read through _agentio.shell_command (the Codex shell is the
"Bash" tool name too); where Claude shows a systemMessage, Codex gets the same text on stderr (Codex rejects
unsupported output fields); the Stop block form is identical in both.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _agentio  # noqa: E402  shared Claude Code / Codex adapter

_STATE_DIR = _agentio.state_dir("session_close")
LOG = _STATE_DIR / "session_close_gate.jsonl"
STATE = _STATE_DIR / "session_close_state.json"
TOOL = Path(os.environ.get("SESSION_CLOSE_PLAN_TOOL") or (Path(__file__).resolve().parent.parent / "tools" / "project_plan.py"))
_CODEX = False  # set in main() from the hook payload
MAX_NO_PROGRESS = 5
GIT_TIMEOUT = 5
PR_RE = re.compile(r"(?:^|[;&|(]\s*|&&\s*)(?:\w+=\S+\s+)*gh\s+pr\s+(?:create|merge)\b", re.M)


def mode() -> str:
    return os.environ.get("SESSION_CLOSE_GATE_MODE", "live").strip().lower()


def log(rec: dict) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "mode": mode(), **rec}
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def warn(text: str) -> None:
    """Visible (non-blocking) message for the operator (Codex: stderr, it has no systemMessage output)."""
    if _CODEX:
        sys.stderr.write("session_close_gate: " + text + "\n")
        return
    print(json.dumps({"systemMessage": "session_close_gate: " + text}, ensure_ascii=False))


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError as exc:  # only a file that was read but is not valid JSON is "corrupt"
        log({"event": "state-corrupt", "error": str(exc)[:200]})
        try:  # keep the broken file for diagnosis instead of overwriting it silently
            if STATE.is_file():
                os.replace(STATE, STATE.with_suffix(".corrupt"))
        except OSError:
            pass
        return {}
    except OSError as exc:  # unreadable (permissions, a directory in the way): report, never rename
        log({"event": "state-unreadable", "error": str(exc)[:200]})
        return {}


def save_state(state: dict) -> bool:
    """Atomic write (temp file + replace). Returns False when the state could not be saved."""
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, STATE)
        return True
    except OSError as exc:
        log({"event": "state-unwritable", "error": str(exc)[:200]})
        return False


def find_plan_dir(cwd: str) -> Path | None:
    p = Path(cwd or ".").resolve()
    for _ in range(8):
        if (p / "PROJECT_PLAN.md").is_file():
            return p
        if p.parent == p:
            break
        p = p.parent
    return None


def load_tool():
    spec = importlib.util.spec_from_file_location("project_plan_tool", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git(project: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(project), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT)


def git_head(project: Path) -> str:
    try:
        return git(project, "rev-parse", "HEAD").stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


PLAN_PATHS = ["PROJECT_PLAN.md", "reports/*PROJECT_PLAN*"]


def docs_unsynced(project: Path) -> str:
    """'' = synced; 'uncommitted: ...' / 'committed but not pushed ...' = not synced; '?reason' = cannot be verified."""
    try:
        st = git(project, "status", "--porcelain", "-uall", "--", *PLAN_PATHS)
        if st.returncode != 0:
            return "?git status failed: " + (st.stderr.strip()[:120] or str(st.returncode))
        dirty = [ln for ln in st.stdout.splitlines() if ln.strip()]
        if dirty:
            return "uncommitted: " + ", ".join(ln[3:] for ln in dirty[:4])
        up = git(project, "rev-parse", "--abbrev-ref", "@{upstream}")
        if up.returncode != 0:
            return "?no upstream branch, push state unverifiable"
        ahead = git(project, "log", "--oneline", "@{upstream}..HEAD", "--", *PLAN_PATHS)
        if ahead.returncode != 0:
            return "?git log failed: " + ahead.stderr.strip()[:120]
        if ahead.stdout.strip():
            return f"committed but not pushed ({len(ahead.stdout.splitlines())} commit(s))"
    except (OSError, subprocess.SubprocessError) as exc:
        return "?git unavailable: " + str(exc)[:120]
    return ""


def block(reason: str) -> None:
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))


def on_post_tool_use(data: dict) -> None:
    cmd = _agentio.shell_command(data)
    if not cmd or not PR_RE.search(cmd):
        return
    resp = data.get("tool_response")
    if isinstance(resp, dict) and (resp.get("is_error") or resp.get("interrupted")):
        return
    plan_dir = find_plan_dir(data.get("cwd") or "")
    if not plan_dir:
        log({"event": "pr-event", "verdict": "no-plan", "cmd": cmd[:120]})
        return
    state = load_state()
    state.setdefault("pr", {})[str(plan_dir)] = {"ts": time.time(), "cmd": cmd[:160], "blocked": False}
    if save_state(state):
        log({"event": "pr-event", "verdict": "recorded", "project": str(plan_dir), "cmd": cmd[:120]})
    else:
        warn("could not record the PR event (state file not writable); the program report will not be enforced for this PR")


def fresh_report(plan_dir: Path, since: float) -> bool:
    rep = plan_dir / "reports"
    if not rep.is_dir():
        return False
    for pattern in ("*PROJECT_PLAN*.ru.html", "*PROGRAM_STATUS*.ru.html"):
        for f in rep.glob(pattern):
            if f.stat().st_mtime > since:
                return True
    return False


def no_progress(state: dict, key: str, sig: str) -> int:
    """Consecutive blocks with an unchanged signature (1 on the first block / after progress)."""
    rec = state.setdefault("tasks", {}).get(key, {})
    count = rec.get("n", 0) + 1 if rec.get("sig") == sig else 1
    state["tasks"][key] = {"sig": sig, "n": count}
    return count


def on_stop(data: dict) -> None:
    plan_dir = find_plan_dir(data.get("cwd") or "")
    if not plan_dir:
        log({"event": "stop", "verdict": "no-plan", "cwd": data.get("cwd")})
        return
    if not TOOL.is_file():
        log({"event": "stop", "verdict": "no-tool", "tool": str(TOOL)})
        return
    tool = load_tool()
    plan = tool.load(plan_dir)
    meta = plan["meta"]
    approval = str(meta.get("operator_approved", "")).strip().lower()
    approved = approval == "true"
    live = mode() == "live"
    enforce = live and approved
    state = load_state()
    msg = data.get("last_assistant_message") or ""
    if not msg and _CODEX:  # Codex may send null here; read the transcript instead
        msg = _agentio.last_assistant_text(data)
    msg = msg.rstrip()
    base = {"event": "stop", "project": str(plan_dir), "approved": approved}
    if approval not in ("", "true", "false"):
        log({**base, "verdict": "approval-invalid", "value": approval[:40]})
        warn(f"operator_approved has an unrecognised value {approval[:40]!r} in {plan_dir}\\PROJECT_PLAN.md (use true or false); treated as false")

    # 1. report owed after a PR
    pr = (state.get("pr") or {}).get(str(plan_dir))
    if pr and not pr.get("done"):
        if fresh_report(plan_dir, pr["ts"]):
            pr["done"] = True
            save_state(state)
        elif not pr.get("blocked"):
            pr["blocked"] = True
            if not save_state(state):
                warn("could not persist the PR report debt; not blocking")
            else:
                log({**base, "verdict": "block" if enforce else "would-block", "why": "report-after-pr"})
                if enforce:
                    block(f"PROGRAM REPORT OWED after the PR: run `python {TOOL.as_posix()} render <project>` and hand over the "
                          "Russian reports/*_PROJECT_PLAN.ru.html (whole-program status, not just the finished sprint).")
                    return

    # 2. open tasks in the active sprint
    n_open, text = tool.check(plan_dir)
    if n_open < 0:
        log({**base, "verdict": "plan-unreadable", "detail": text})
        if approved:
            warn(f"the approved plan in {plan_dir} cannot be trusted ({text}); the session close is NOT gated until it is fixed")
        return
    if n_open == 0:
        why = docs_unsynced(plan_dir) if approved else ""
        if why.startswith("?"):
            log({**base, "verdict": "docs-unverifiable", "detail": why})
            warn(f"plan documents could not be verified as committed and pushed ({why[1:]})")
            return
        if not why:
            log({**base, "verdict": "sprint-closed", "active": meta.get("active_sprint"), "approved_sprints": meta.get("approved_sprints")})
            return
        count = no_progress(state, f"docs|{plan_dir}", f"{why}|{git_head(plan_dir)}")
        if not save_state(state):
            warn("state file not writable; not blocking on unsynced plan documents")
            return
        if count > MAX_NO_PROGRESS:
            log({**base, "verdict": "ESCALATE", "why": "docs-unsynced", "detail": why})
            warn(f"plan documents in {plan_dir} still not synced after {count - 1} blocks ({why}); releasing the session")
            return
        log({**base, "verdict": "block" if enforce else "would-block", "why": "docs-unsynced", "docs": why, "no_progress": count})
        if enforce:
            block(f"PROJECT PLAN DOCUMENTS NOT SYNCED ({why}). Commit PROJECT_PLAN.md and reports/*PROJECT_PLAN*.html in the project folder and push them. "
                  f"Sprint {meta.get('active_sprint')} is closed: ask the operator before starting the next sprint.")
        return
    if msg.endswith("?"):
        log({**base, "verdict": "allow-question", "open": n_open})
        return
    key = f"{data.get('session_id') or 'default'}|{plan_dir}"
    count = no_progress(state, key, f"{n_open}|{git_head(plan_dir)}")
    if not save_state(state):
        warn("state file not writable; not blocking on open tasks (the no-progress counter cannot be kept)")
        return
    if count > MAX_NO_PROGRESS:
        log({**base, "verdict": "ESCALATE", "open": n_open, "no_progress_blocks": count - 1, "detail": text})
        warn(f"{n_open} open task(s) in {plan_dir} and no progress after {count - 1} blocks; releasing the session ({text})")
        return
    log({**base, "verdict": "block" if enforce else "would-block", "open": n_open, "no_progress": count, "detail": text})
    if enforce:
        block(f"PROJECT PLAN HAS OPEN TASKS ({text}). Continue the next open task of the active sprint now; mark a task BLOCKED_OPERATOR / "
              f"BLOCKED_EXTERNAL in PROJECT_PLAN.md only with a verified cause. Do not start another sprint without operator approval.")


def main() -> int:
    global _CODEX
    if mode() == "off":
        return 0
    data = json.loads(sys.stdin.read() or "{}")
    _CODEX = _agentio.client(data) == "codex"
    if data.get("agent_type"):
        return 0
    event = data.get("hook_event_name") or ("PostToolUse" if "tool_name" in data else "Stop")
    if event == "PostToolUse":
        on_post_tool_use(data)
    elif event == "Stop":
        on_stop(data)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        log({"event": "error", "traceback": traceback.format_exc()[-1500:]})
        raise SystemExit(0)
