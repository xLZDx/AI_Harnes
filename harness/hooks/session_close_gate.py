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
    return _agentio.run_git(["-C", str(project), *args], timeout=GIT_TIMEOUT)  # never raises: failure = returncode != 0


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
    if _agentio.command_succeeded(data) is not True:  # failed or unprovable (unknown Codex shape): record nothing
        log({"event": "pr-event", "verdict": "not-succeeded", "cmd": cmd[:120]})
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


LEDGER_MAX_BLOCKS = 5          # no-progress blocks before the release
LEDGER_MAX_SECONDS = 30 * 60   # or this long since the first block of the streak
LEDGER_GAP_SECONDS = 5 * 60    # a pause longer than this between two stops starts a new streak
MINE_RE = re.compile(r"ledger\.py\s+(?:start|verify|review|tokens|block)\s+['\"]?([A-Za-z0-9_.-]+)|Task:\s*([A-Za-z0-9_.-]+)")
OPEN_STATES_SKIP = ("done",)


def _ledger_root_of(path: str) -> Path | None:
    try:
        p = Path(path)
        if not p.is_absolute():
            return None
        p = p.resolve()
        for d in [p, *p.parents][:12]:
            if (d / ".harness" / "ledger.json").is_file():
                return d
    except (OSError, ValueError):
        pass
    return None


def ledger_projects(data: dict) -> tuple[dict, set]:
    """({root: worked}, my task ids). Priority (GPT-PM plan round 1, MAJOR-05): the git/ledger root of the session cwd, then the
    ledger roots of ABSOLUTE paths in this session's Edit/Write/NotebookEdit calls. Shell text is used only to find task ids
    this session started or reported (ledger.py start|verify|review <ID>, `Task: <ID>` trailers), never to pick a project."""
    roots: dict[Path, bool] = {}
    mine: set[str] = set()
    cwd_root = _ledger_root_of(data.get("cwd") or "")
    if cwd_root:
        roots[cwd_root] = True
    tp = data.get("transcript_path") or ""
    for r in (_agentio.records(tp) if tp else []):
        if r.get("kind") != "tool_use":
            continue
        inp = r.get("input") if isinstance(r.get("input"), dict) else {}
        if r.get("name") not in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
            inp = {k: v for k, v in inp.items() if k == "command"}  # reading a file does not tie the session to its project
        for key in ("file_path", "notebook_path"):
            if inp.get(key):
                root = _ledger_root_of(str(inp[key]))
                if root:
                    roots[root] = True
        cmd = str(inp.get("command") or "")
        for m in MINE_RE.finditer(cmd):
            mine.add(m.group(1) or m.group(2))
    return roots, mine


def ledger_open_tasks(root: Path, mine: set) -> list[tuple[str, str]] | None:
    """Open tasks of the first non-closed sprint that this session should finish; None = ledger unreadable."""
    try:
        led = json.loads((root / ".harness" / "ledger.json").read_text(encoding="utf-8"))
        sprint = next((s for s in led["sprints"] if s.get("state") != "closed"), None)
        if sprint is None:
            return []
        out = []
        for t in sprint["tasks"]:
            if t.get("state") in OPEN_STATES_SKIP or t.get("blocked"):
                continue
            if t.get("state") != "todo" and t["id"] not in mine and mine:
                continue  # started/verified by another session (this one worked on other tasks): not this session's to finish
            if t.get("state") != "todo" and not mine:
                log({"event": "stop", "verdict": "ownership-unknown", "task": str(t["id"]), "state": str(t["state"])})  # kept, bounded release
            out.append((str(t["id"]), str(t["state"])))
        return out
    except (OSError, ValueError, KeyError, TypeError, AttributeError, StopIteration):
        return None


def ledger_stop(data: dict, msg: str) -> bool:
    """Tasks-left check on the Harness v2 ledger (S7-T2). True = handled (blocked or released), caller stops."""
    roots, mine = ledger_projects(data)
    if not roots:
        log({"event": "stop", "verdict": "no-ledger-project", "cwd": data.get("cwd")})
        return False
    state = load_state()
    streaks = state.setdefault("ledger", {})
    for root in roots:
        tasks = ledger_open_tasks(root, mine)
        key = str(root)
        if tasks is None:
            log({"event": "stop", "verdict": "ledger-unreadable", "project": key})
            warn(f"the ledger of {root} cannot be read; the tasks-left check is skipped (fix .harness/ledger.json)")
            continue
        key = f"{data.get('session_id') or 'nosession'}|{key}"  # a streak belongs to ONE session in ONE project
        for k in [k for k, v in streaks.items() if time.time() - float((v or {}).get("last", 0)) > 24 * 3600]:
            streaks.pop(k, None)  # old streaks of other sessions do not pile up
        if not tasks:
            streaks.pop(key, None)
            continue
        if msg.endswith("?"):
            log({"event": "stop", "verdict": "allow-question", "project": key, "open": len(tasks)})
            continue
        sig = "|".join(f"{i}:{s}" for i, s in sorted(tasks))
        rec = streaks.get(key) or {}
        now_ts = time.time()
        if rec.get("sig") == sig and now_ts - float(rec.get("last", now_ts)) <= LEDGER_GAP_SECONDS:
            rec["n"] = rec.get("n", 0) + 1
        else:  # progress (signature changed) or a long pause (the operator came back): a fresh streak
            rec = {"sig": sig, "n": 1, "first": now_ts}
        rec["last"] = now_ts
        streaks[key] = rec
        if not save_state(state):
            warn("state file not writable; not blocking on open ledger tasks")
            continue
        listing = ", ".join(f"{i} ({s})" for i, s in tasks)
        if rec["n"] > LEDGER_MAX_BLOCKS or now_ts - rec["first"] > LEDGER_MAX_SECONDS:
            reason = "no-progress-blocks" if rec["n"] > LEDGER_MAX_BLOCKS else "30-minutes"
            log({"event": "stop", "verdict": "ESCALATE", "why": "ledger-release", "release_reason": reason, "project": key, "open": listing})
            try:
                import hook_metrics
                hook_metrics.record("session_close_gate.py", "Stop", "release", "ledger-" + reason, 0, str(data.get("session_id") or ""),
                                    _agentio.client(data), str(root))
            except Exception:
                pass
            warn(f"{len(tasks)} open task(s) in {root} and no progress ({reason}); releasing the session: {listing}")
            continue
        log({"event": "stop", "verdict": "block" if mode() == "live" else "would-block", "why": "ledger-open-tasks",
             "project": key, "open": listing, "no_progress": rec["n"]})
        if mode() == "live":
            block(f"TASKS LEFT in {root.name}: {listing}. Continue with the next open task now. A task that cannot proceed is marked with "
                  f"`ledger.py block <ID> --class OPERATOR|EXTERNAL|PROHIBITED --cause \"<verified cause>\"` (a cause you checked), never by "
                  f"stopping. (Release after {LEDGER_MAX_BLOCKS} blocks without progress or 30 minutes.)")
            return True
    return False


def on_stop(data: dict) -> None:
    msg0 = data.get("last_assistant_message") or ""
    if not msg0 and _CODEX:
        msg0 = _agentio.last_assistant_text(data)
    if ledger_stop(data, msg0.rstrip()):
        return
    plan_dir = find_plan_dir(data.get("cwd") or "")
    if not plan_dir:
        log({"event": "stop", "verdict": "no-plan", "cwd": data.get("cwd")})
        return
    if not TOOL.is_file():
        log({"event": "stop", "verdict": "no-tool", "tool": str(TOOL)})
        warn(f"plan tool {TOOL} not found, so the project plan in {plan_dir} is NOT checked; "
             "set SESSION_CLOSE_PLAN_TOOL in ~/.harness-hooks/config.json")
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
                    block("PROGRAM REPORT OWED after the PR: run `python .harness-tools/report.py` (or tools/report.py in Harness_v2) and hand over "
                          "reports/<PROJECT>_PROGRAM_STATUS_LIVE.ru.html, the ONE global report format (operator order 2026-11-11/12). "
                          "Do not use project_plan.py render or any other layout.")
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


def warn_config_once() -> None:
    """One stderr warning line when the machine config names missing paths; repeated only when the set changes.
    Informational: never blocks and never raises."""
    try:
        problems = _agentio.config_path_problems()
        sig = "|".join(problems)
        state = load_state()
        if state.get("config_warned", "") == sig:
            return
        if problems:
            sys.stderr.write("session_close_gate: config path problems: " + "; ".join(problems) + "\n")
        state["config_warned"] = sig
        save_state(state)
    except Exception:  # noqa: BLE001  a self-check must never break the gate
        pass


def main() -> int:
    global _CODEX
    if mode() == "off":
        return 0
    data = json.loads(sys.stdin.read() or "{}")
    _CODEX = _agentio.client(data) == "codex"
    if data.get("agent_type"):
        return 0
    event = data.get("hook_event_name") or ("PostToolUse" if "tool_name" in data else "Stop")
    # Claude and Codex sessions share one state file: serialize the load-change-save cycle
    with _agentio.file_lock(STATE) as got:
        if not got:
            log({"event": event, "verdict": "lock-timeout"})
        if event == "PostToolUse":
            on_post_tool_use(data)
        elif event == "Stop":
            warn_config_once()
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
