#!/usr/bin/env python3
"""Recovery plan R8: one table over every harness project and both clients (read-only).

Rows: every top-level dir under --root with a .harness-tools folder or .harness/ledger.json (Virtual_marketing_company is
skipped) plus 'claude-home' and 'codex-home'. Cells: OK / FAIL / n.a., reasons in a details list under the table.
Nothing is written to projects, ~/.claude, ~/.codex or GitHub; hooks run with a temp metrics and state dir under
D:\\tmp\\harness_audit. Exit code 0 only when every cell is OK or n.a.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import queue
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
HARNESS = HERE.parent
sys.path.insert(0, str(HERE))

import workflow_guard  # noqa: E402

HOME = Path.home()
SKIP = {"virtual_marketing_company"}
COLS = ["hooks_path", "tools_current", "prepush_current", "ci_local", "ci_github", "ledger_whole", "gates_live", "client_cfg", "kill_switch"]
TOOL_FILES = ["ledger.py", "report.py", "workflow_guard.py", "plan_ledger.py"]
BAD = set(workflow_guard.BAD)
AUDIT_TMP = Path(r"D:\tmp\harness_audit")
DENY_CMDS = ["npm test", "npx vitest", "npm run lint", "tsc --noEmit", "npm run verify", "pytest", "gh workflow run ci.yml", "git push"]
ALLOW_CMDS = ["git status"]
RULES_BLOCK = "Operator development rules"
CODEX_HOOKS_BY_CWD: dict[str, dict] = {}
CODEX_HOOKS_ERROR: str | None = None
CODEX_EXE_NOTE: str | None = None
CODEX_CLIENT_FLAG = "--harness-client=codex"
KILL_VALUES = {"off", "0", "false"}
# A kill switch is an environment variable a hook compares (lower-cased) with "off" / {"off","0","false"}; each name keeps
# the values its hook really honours ("== 'off'" honours only off). Mode variables whose "off" disables the gate are listed
# in KILL_MODE_OFF (they are read into a local first, so the line scan cannot see them). Opt-in variables (default off,
# enabled by on/1/true) are not kill switches: CLAUDE_ROSETTA_GATE, CLAUDE_SHELL_POLICY_AUDIT.
KILL_MODE_VARS = {"CLAUDE_ROSETTA_GATE", "CLAUDE_SHELL_POLICY_AUDIT"}
KILL_MODE_OFF = {"SESSION_SUPERVISOR_MODE": {"off"}, "SESSION_CLOSE_GATE_MODE": {"off"}}
REG_NO_MORE_ITEMS = 259
KILL_LINE_RE = re.compile(r"\.lower\(\)\s*(?:==|!=|in)\s*(?:\{[^}]*[\"']off[\"'][^}]*\}|[\"']off[\"'])")
KILL_NAME_RE = re.compile(r"[\"']([A-Z][A-Z0-9_]{3,})[\"']")
KILL_OFF_CALL_RE = re.compile(r"\boff\(\s*[\"']([A-Z][A-Z0-9_]{3,})[\"']\s*\)")  # evidence_gate.off(var): indirect name
REG_SCOPES = [("user", r"Environment"), ("machine", r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")]

OK, FAIL, NA = "OK", "FAIL", "n.a."


def run(cmd, cwd=None, timeout=20, env=None, stdin=None):
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=env, input=stdin)
    except subprocess.TimeoutExpired:
        return None
    except OSError:
        return None


def git(d: Path, *args, timeout=20):
    return run(["git", "-C", str(d), *args], timeout=timeout)


def norm_path(p) -> str:
    return os.path.normcase(os.path.normpath(str(p))).replace("\\", "/")


def codex_hook_event(event: str) -> str:
    parts = re.split(r"[_\W]+", event)
    if len(parts) > 1:
        return parts[0].lower() + "".join(part.capitalize() for part in parts[1:])
    return event[:1].lower() + event[1:]


def codex_hook_identity(event: str, matcher, hook: dict) -> tuple:
    command = hook.get("commandWindows") or hook.get("command") or ""
    normalized_command = re.sub(r"[\\/]+", lambda _match: chr(92), str(command)).casefold()
    return (codex_hook_event(event), matcher, hook.get("type", "command"), normalized_command)


def listed_hook_identity(hook: dict) -> tuple:
    command = re.sub(r"[\\/]+", lambda _match: chr(92), str(hook.get("command") or "")).casefold()
    return (hook.get("eventName"), hook.get("matcher"), hook.get("handlerType"), command)


# ---- discovery ---------------------------------------------------------------------------------------------------------

def discover(root: Path) -> list[Path]:
    out = []
    for p in sorted(root.iterdir(), key=lambda x: x.name.lower()):
        if not p.is_dir() or p.name.lower() in SKIP:
            continue
        if (p / ".harness-tools").is_dir() or (p / ".harness" / "ledger.json").is_file():
            out.append(p)
    return out


# ---- per-project checks (each returns (status, reason)) ------------------------------------------------------------------

def chk_hooks_path(d: Path):
    r = git(d, "config", "--get", "core.hooksPath")
    val = (r.stdout.strip() if r is not None else "")
    if not val:
        return FAIL, "core.hooksPath not set"
    p = Path(val)
    if not p.is_absolute():
        top = git(d, "rev-parse", "--show-toplevel")
        p = Path(top.stdout.strip() if top is not None and top.stdout.strip() else d) / val
    want = d / ".harness-tools" / "githooks"
    if norm_path(p) != norm_path(want):
        return FAIL, f"points to {val}, expected {want}"
    if not (want / "pre-push").is_file():
        return FAIL, "pre-push missing in .harness-tools/githooks"
    return OK, ""


def chk_tools_current(d: Path):
    t = d / ".harness-tools"
    missing, stale = [], []
    for n in TOOL_FILES:
        f = t / n
        if not f.is_file():
            missing.append(n)
        elif f.read_bytes() != (HERE / n).read_bytes():
            stale.append(n)
    if missing or stale:
        return FAIL, "; ".join(x for x in (("missing " + ", ".join(missing)) if missing else "", ("stale " + ", ".join(stale)) if stale else "") if x)
    return OK, ""


def chk_prepush_current(d: Path):
    f = d / ".harness-tools" / "githooks" / "pre-push"
    if not f.is_file():
        return FAIL, "project pre-push missing"
    if f.read_bytes() != (HERE / "githooks" / "pre-push").read_bytes():
        return FAIL, "pre-push differs from tools/githooks/pre-push"
    return OK, ""


def workflow_files(d: Path):
    wd = d / ".github" / "workflows"
    if not wd.is_dir():
        return []
    return sorted(p for p in wd.iterdir() if p.is_file() and p.suffix.lower() in (".yml", ".yaml"))


def chk_ci_local(d: Path):
    files = workflow_files(d)
    if not files:
        return NA, "no workflow files"
    bad = []
    for f in files:
        try:
            hit = sorted(workflow_guard.triggers(f.read_text(encoding="utf-8", errors="replace")) & BAD)
        except OSError:
            continue
        if hit:
            bad.append(f"{f.name} ({','.join(hit)})")
    if bad:
        return FAIL, "auto triggers: " + "; ".join(bad)
    return OK, f"{len(files)} workflow file(s), dispatch only"


def origin_slug(d: Path):
    r = git(d, "config", "--get", "remote.origin.url")
    url = r.stdout.strip() if r is not None else ""
    m = re.search(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def github_slugs(d: Path) -> list[str]:
    """Every GitHub repository among the project's remotes (origin first): CI can run on any of them."""
    r = git(d, "remote", "-v")
    out = []
    for ln in (r.stdout.splitlines() if r is not None and r.returncode == 0 else []):
        m = re.search(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?\s", ln + " ")
        if m:
            s = f"{m.group(1)}/{m.group(2)}"
            if s not in out:
                out.append(s)
    o = origin_slug(d)
    return ([o] if o else []) + [s for s in out if s != o]


def chk_ci_github(d: Path, enabled: bool):
    if not enabled:
        return NA, "skipped (--no-github)"
    slugs = github_slugs(d)
    if not slugs:
        return NA, "no GitHub remote"
    results = [_ci_github_one(d, s, i == 0) for i, s in enumerate(slugs)]
    fails = [m for st, m in results if st == FAIL]
    if fails:
        return FAIL, " | ".join(fails)
    oks = [m for st, m in results if st == OK]
    return (OK if oks else NA), " | ".join(m for _, m in results)


def _ci_github_one(d: Path, slug: str, is_origin: bool):
    r = run(["gh", "workflow", "list", "--repo", slug, "--all", "--json", "name,state,path"], timeout=20)
    if r is None:
        return FAIL, f"{slug}: gh timed out or missing"
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip()
        if re.search(r"HTTP 404|Could not resolve to a Repository", err):
            return NA, f"{slug}: not on GitHub (404), no CI can run there"
        return FAIL, f"{slug}: gh failed: {err[:120]}"
    try:
        items = json.loads(r.stdout or "[]")
    except ValueError:
        return FAIL, f"{slug}: unreadable gh output"
    bad, skipped, active = [], [], 0
    for w in items:
        if str(w.get("state", "")).lower() != "active":
            continue
        path = w.get("path") or ""
        if not path.startswith(".github/workflows/"):
            skipped.append(f"{w.get('name')} (dynamic)")
            continue
        active += 1
        text = None
        a = run(["gh", "api", f"repos/{slug}/contents/{path}", "--jq", ".content"], timeout=20)  # what GitHub runs
        if a is not None and a.returncode == 0:
            try:
                text = base64.b64decode(a.stdout.strip()).decode("utf-8", "replace")
            except Exception:
                text = None
        local = d / path
        if text is None and is_origin and local.is_file():
            text = local.read_text(encoding="utf-8", errors="replace")
        if text is None:
            bad.append(f"{path} (content unreadable)")
            continue
        hit = sorted(workflow_guard.triggers(text) & BAD)
        if hit:
            if re.search(r"deploy|migrat", path, re.I):
                skipped.append(f"{path} (deploy/migrate: {','.join(hit)})")
            else:
                bad.append(f"{path} ({','.join(hit)})")
    note = ("; reported n.a.: " + ", ".join(skipped)) if skipped else ""
    if bad:
        return FAIL, f"{slug}: active auto workflows: " + "; ".join(bad) + note
    if not active and skipped:
        return NA, f"{slug}{note}"
    return OK, f"{slug}: {active} active workflow(s), none auto{note}"


def chk_ledger_whole(d: Path):
    import ledger_sync  # lazy: pulls in ledger, report, plan importers
    try:
        res = ledger_sync.sync_project(d, True)
    except BaseException as e:  # SystemExit from ledger helpers included
        return FAIL, f"ledger_sync error: {e}"
    st = res.get("status", "")
    if st.startswith("ERROR"):
        return FAIL, st
    if res.get("plan") is None:
        return NA, "no plan found"
    plan_n = res.get("plan_tasks", 0)
    led_n = (res.get("old") or {}).get("tasks", 0)
    if plan_n == led_n:
        return OK, f"plan {plan_n} = ledger {led_n}" + (f" ({st})" if st and st != "unchanged" else "")
    if st == "unchanged" and led_n > plan_n:
        # the sync keeps genuine harness-flow tasks the plan does not list (Fitness_App VOICE): the ledger is the plan plus them
        return OK, f"plan {plan_n} + {led_n - plan_n} genuine ledger task(s) = ledger {led_n} (in sync)"
    return FAIL, f"plan {plan_n} tasks, ledger {led_n} tasks ({st})"


# ---- live gates -------------------------------------------------------------------------------------------------------------

def load_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def registered_handlers(cfg: dict, event: str, tool: str | None):
    """Handlers of `event` whose matcher fits `tool` (matcher absent or empty matches all)."""
    out = []
    for g in (cfg or {}).get("hooks", {}).get(event, []) or []:
        m = g.get("matcher")
        if tool is not None and m not in (None, "", "*"):
            try:
                if not re.fullmatch(m, tool):
                    continue
            except re.error:
                continue
        for h in g.get("hooks", []):
            cmd = (h.get("commandWindows") if os.name == "nt" else None) or h.get("command")
            if cmd:
                args = h.get("args") if isinstance(h.get("args"), list) else []
                out.append((subprocess.list2cmdline([cmd, *map(str, args)]) if args else cmd, int(h.get("timeout") or 30)))
    return out


def handler_text(h: dict) -> str:
    """Command line of one hook handler: Claude keeps the script name in an `args` list, Codex in the command string."""
    args = h.get("args") if isinstance(h.get("args"), list) else []
    return " ".join([str(h.get("command", "")), str(h.get("commandWindows", "")), *map(str, args)])


def split_cmd(cmd: str) -> list[str]:
    parts = shlex.split(cmd, posix=False)
    return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]


def hook_name(cmd: str) -> str:
    m = re.search(r"([\w.-]+\.py)(?!.*\.py)", cmd.replace("--harness-client", " "))
    names = re.findall(r"([\w-]+\.py)", cmd)
    names = [n for n in names if n != "run_hook.py"]
    return names[0] if names else (m.group(1) if m else cmd[:30])


def hook_denies(code: int, out: str) -> bool:
    if code == 2:
        return True
    try:
        o = json.loads(out)
    except ValueError:
        return False
    if isinstance(o, dict):
        if o.get("decision") == "block":
            return True
        h = o.get("hookSpecificOutput")
        if isinstance(h, dict) and (h.get("permissionDecision") == "deny"
                                     or (isinstance(h.get("decision"), dict) and h["decision"].get("behavior") == "deny")):
            return True
    return False


def approval_on_head(d: Path) -> bool:
    led = load_json(d / ".harness" / "ledger.json")
    ap = ((led or {}).get("approval") or {}).get("head")
    r = git(d, "rev-parse", "HEAD")
    head = r.stdout.strip() if r is not None else ""
    return bool(ap and head and ap == head)


def make_unapproved_probe_repo(parent: Path) -> Path:
    """Create an isolated harness repo with no remote or approval for client-level hook probes."""
    probe = parent / "unapproved-probe"
    probe.mkdir()
    init = run(["git", "init", "-q"], cwd=probe)
    if init is None or init.returncode != 0:
        raise RuntimeError("git init failed for disposable no-approval probe")
    ledger_dir = probe / ".harness"
    ledger_dir.mkdir()
    ledger = {"version": 1, "project": "audit-probe", "approval": None,
              "sprints": [{"id": "AUDIT", "state": "open", "tasks": [{"id": "AUDIT-T1", "state": "todo"}]}]}
    (ledger_dir / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8", newline="\n")
    return probe


def run_gate(handlers, command: str, cwd: Path, tmp: Path, client: str):
    """Returns (denied_by hook name or None, notes list)."""
    payload = json.dumps({
        "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command},
        "cwd": str(cwd), "session_id": "audit-" + uuid.uuid4().hex[:8], "transcript_path": "",
        "tool_use_id": "toolu_audit_" + uuid.uuid4().hex[:8],
    })
    env = dict(os.environ, HARNESS_METRICS_DIR=str(tmp / "metrics"), HARNESS_HOOK_STATE=str(tmp / "state"),
               CLAUDE_PROJECT_DIR=str(cwd), PYTHONIOENCODING="utf-8")
    notes = []
    for cmd, to in handlers:
        r = run(split_cmd(cmd), cwd=str(cwd), timeout=min(max(to, 5), 45) + 5, env=env, stdin=payload)
        name = hook_name(cmd)
        if r is None:
            notes.append(f"{name} timed out/not runnable")
            continue
        if hook_denies(r.returncode, r.stdout or ""):
            return name, notes
        if r.returncode not in (0, 2):
            notes.append(f"{name} exit {r.returncode}")
    return None, notes


def chk_gates_live(cwd: Path, cfg_path: Path, client: str, is_project_dir: bool):
    cfg = load_json(cfg_path)
    if cfg is None:
        return FAIL, f"{cfg_path} unreadable"
    handlers = registered_handlers(cfg, "PreToolUse", "Bash")
    if not handlers:
        return FAIL, "no PreToolUse Bash hooks registered"
    try:
        AUDIT_TMP.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="g_", dir=str(AUDIT_TMP)))
    except OSError as exc:
        return FAIL, f"could not create temporary audit directory: {exc}"
    probe_cwd = cwd
    result = None
    probe_error = None
    try:
        if not is_project_dir:
            try:
                probe_cwd = make_unapproved_probe_repo(tmp)
            except (OSError, RuntimeError) as exc:
                result = (FAIL, f"could not create disposable no-approval probe repo: {exc}")
        if result is None:
            deny = list(DENY_CMDS)
            skipped = ""
            if is_project_dir and approval_on_head(probe_cwd):
                deny.remove("git push")
                skipped = "git push skipped (ledger approval is on HEAD)"
            todo = [(c, True) for c in deny] + [(c, False) for c in ALLOW_CMDS]
            with ThreadPoolExecutor(max_workers=6) as ex:
                results = list(ex.map(lambda t: run_gate(handlers, t[0], probe_cwd, tmp, client), todo))
            not_denied, wrongly_denied, notes = [], [], []
            for (c, want_deny), (by, nts) in zip(todo, results):
                notes += nts
                if want_deny and by is None:
                    not_denied.append(c)
                if not want_deny and by is not None:
                    wrongly_denied.append(f"{c} (by {by})")
            reg = [hook_name(c) for c, _ in handlers]
            msgs = []
            if not_denied:
                msgs.append("not denied: " + ", ".join(not_denied))
            if wrongly_denied:
                msgs.append("should be allowed but denied: " + ", ".join(wrongly_denied))
            if "rules_gate.py" not in reg:
                msgs.append("rules_gate.py not registered")
            if msgs:
                extra = ("; " + skipped) if skipped else ""
                result = (FAIL, "; ".join(msgs) + extra)
            else:
                detail = skipped or f"{len(deny)} denied, git status allowed"
                if notes:
                    detail += f" [hook notes: {'; '.join(sorted(set(notes))[:3])}]"
                result = (OK, detail)
    except Exception as exc:
        probe_error = exc
    finally:
        try:
            if tmp.parent.resolve() != AUDIT_TMP.resolve():
                raise RuntimeError("temporary audit directory escaped its configured parent")
            shutil.rmtree(tmp)
        except (OSError, RuntimeError) as exc:
            cleanup_error = exc
        else:
            cleanup_error = None
    if probe_error is not None:
        if cleanup_error is not None:
            raise RuntimeError(f"gate probe failed ({type(probe_error).__name__}) and temporary audit cleanup failed: {cleanup_error}") from probe_error
        raise probe_error
    if cleanup_error is not None:
        detail = result[1] if result is not None else "gate probe did not complete"
        return FAIL, f"{detail}; temporary audit cleanup failed: {cleanup_error}"
    return result


# ---- client rows --------------------------------------------------------------------------------------------------------------

def snake(ev: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", ev).lower()


def chk_claude_cfg():
    cfg = load_json(HOME / ".claude" / "settings.json")
    if cfg is None:
        return FAIL, "settings.json unreadable"
    probs = []

    def has(event, script, matcher_part=None):
        for g in (cfg.get("hooks", {}).get(event) or []):
            if matcher_part and matcher_part not in (g.get("matcher") or "").split("|"):
                continue
            if any(script in handler_text(h) for h in g.get("hooks", [])):
                return True
        return False

    if not has("PreToolUse", "rules_gate.py"):
        probs.append("rules_gate.py not in PreToolUse")
    for ev in ("SubagentStart", "SubagentStop"):
        if not has(ev, "agent_visibility.py"):
            probs.append(f"agent_visibility.py not in {ev}")
    if not (has("PreToolUse", "agent_visibility.py", "Agent") or has("PreToolUse", "agent_visibility.py", "Task")):
        probs.append("agent_visibility.py not in PreToolUse Agent|Task")
    try:
        if RULES_BLOCK not in (HOME / ".claude" / "CLAUDE.md").read_text(encoding="utf-8", errors="replace"):
            probs.append(f"CLAUDE.md lacks '{RULES_BLOCK}'")
    except OSError:
        probs.append("CLAUDE.md unreadable")
    return (FAIL, "; ".join(probs)) if probs else (OK, "rules_gate, agent_visibility (3 events), rules block present")


def _codex_key(p) -> str:
    """One comparable form for a path sent to or returned by app-server: no \\\\?\\ prefix, resolved, normalised."""
    s = str(p or "")
    if s.startswith("\\\\?\\"):
        s = s[4:]
    if not s:
        return ""
    try:
        s = str(Path(s).resolve())
    except OSError:
        pass
    return norm_path(s)


def _codex_executable() -> str | None:
    """Find the installed Codex executable without depending on its package-manager layout."""
    found = shutil.which("codex.exe") or shutil.which("codex")
    if found and os.name == "nt" and Path(found).suffix.lower() not in (".exe", ".com"):
        # B28a: the npm shim (codex / codex.cmd) starts node, which starts the real codex.exe; killing the shim leaves the
        # real app-server alive. The npm package keeps the vendor binary at
        # <shim dir>/node_modules/@openai/codex/node_modules/@openai/codex-<platform>/vendor/<triple>/bin/codex.exe.
        base = Path(found).resolve().parent / "node_modules" / "@openai" / "codex"
        machine = platform.machine().lower()
        # B43/B41: match the Rust target triple directory (<vendor>/<triple>/bin/codex.exe) exactly, never a substring
        want = ("aarch64-pc-windows-msvc" if machine in ("arm64", "aarch64")
                else "x86_64-pc-windows-msvc" if machine in ("amd64", "x86_64", "x64") else None)
        for pattern in ("node_modules/@openai/codex-*/vendor/*/bin/codex.exe", "vendor/*/bin/codex.exe"):
            vendor = sorted(base.glob(pattern))
            if vendor:
                for cand in vendor:
                    if want is not None and cand.parent.parent.name.lower() == want:
                        return str(cand)
                global CODEX_EXE_NOTE
                CODEX_EXE_NOTE = f"no vendor codex.exe matches machine {platform.machine()}; used first entry {vendor[0]}"
                return str(vendor[0])
    return found


def _kill_process_tree(proc) -> list[str]:
    """Stop the app-server and everything it started. On Windows `taskkill /T /F` also covers a shim fallback.
    Never raises; returns cleanup warnings."""
    warns: list[str] = []
    try:
        if proc.poll() is None and os.name == "nt":
            try:
                # B43: the system taskkill.exe by absolute path, not through PATH or the cwd (as ci_triggers_off.py does)
                taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
                r = subprocess.run([taskkill, "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=15)
                # B41: exit 128 is "process not found"; when a re-poll shows the process already exited, that is success
                if r.returncode != 0 and not (r.returncode == 128 and proc.poll() is not None):
                    warns.append(f"taskkill for app-server pid {proc.pid} exited {r.returncode}")
            except (OSError, subprocess.SubprocessError) as exc:
                warns.append(f"taskkill for app-server pid {proc.pid} failed: {type(exc).__name__}: {exc}")
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception as exc:
                warns.append(f"kill of app-server pid {proc.pid} failed: {type(exc).__name__}: {exc}")
        try:
            proc.wait(timeout=5)
        except Exception as exc:
            warns.append(f"wait for app-server pid {proc.pid} failed: {type(exc).__name__}: {exc}")
        if proc.poll() is None:
            warns.append(f"app-server pid {proc.pid} still running")
    except Exception as exc:
        warns.append(f"cleanup of app-server failed: {type(exc).__name__}: {exc}")
    return warns


def _codex_rpc_response(proc, output_queue, request_id: int, timeout: int) -> dict:
    """Read JSONL app-server output until the response with the requested id arrives."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timed out waiting for app-server response {request_id}")
        try:
            line = output_queue.get(timeout=remaining)
        except queue.Empty as exc:
            raise TimeoutError(f"timed out waiting for app-server response {request_id}") from exc
        if line is None:
            raise RuntimeError(f"Codex app-server exited before response {request_id} (exit {proc.poll()})")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Codex app-server returned invalid JSON: {exc.msg}") from exc
        if not isinstance(message, dict):
            raise RuntimeError("Codex app-server returned a JSON-RPC message that is not an object")
        if message.get("id") == request_id:
            if "error" in message:
                raise RuntimeError(f"Codex app-server RPC error: {message['error']}")
            return message


def codex_hooks_list(cwds: list[Path]) -> tuple[dict[str, dict], str | None]:
    """Ask the running Codex executable for authoritative hook trust per project cwd. Never raises; cleanup warnings
    (taskkill/kill/wait failures, app-server still running) are appended to the error so the codex client cell is FAIL."""
    cleanup: list[str] = []
    try:
        by_cwd, err = _codex_hooks_list_run(cwds, cleanup)
    except Exception as exc:
        by_cwd, err = {}, f"Codex app-server hooks/list crashed: {type(exc).__name__}: {exc}"
    if cleanup:
        err = "; ".join(([err] if err else []) + ["app-server cleanup: " + "; ".join(cleanup)])
    return by_cwd, err


def _codex_hooks_list_run(cwds: list[Path], cleanup: list[str]) -> tuple[dict[str, dict], str | None]:
    exe = _codex_executable()
    if not exe:
        return {}, "Codex executable not found on PATH"
    try:
        proc = subprocess.Popen([exe, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1)
    except OSError as exc:
        return {}, f"could not start Codex app-server: {type(exc).__name__}: {exc}"

    output_queue: queue.Queue = queue.Queue()

    def read_output():
        try:
            for line in proc.stdout:
                output_queue.put(line)
        finally:
            output_queue.put(None)

    reader = threading.Thread(target=read_output, name="codex-app-server-output", daemon=True)
    reader.start()

    def send(message: dict):
        proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "harness-audit", "title": "Harness audit", "version": "1.0"}}})
        _codex_rpc_response(proc, output_queue, 1, 20)
        send({"jsonrpc": "2.0", "method": "initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "hooks/list",
              "params": {"cwds": [str(p.resolve()) for p in cwds]}})
        response = _codex_rpc_response(proc, output_queue, 2, 30)
        result = response.get("result")
        if not isinstance(result, dict):
            return {}, "Codex app-server hooks/list response has no result object"
        data = result.get("data")
        if not isinstance(data, list):
            return {}, "Codex app-server hooks/list response has no result.data list"
        by_cwd = {}
        for item in data:
            if not isinstance(item, dict) or not item.get("cwd"):
                return {}, "Codex app-server hooks/list returned an invalid cwd entry"
            by_cwd[_codex_key(item["cwd"])] = item
        return by_cwd, None
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return {}, f"Codex app-server hooks/list failed: {exc}"
    finally:
        cleanup.extend(_kill_process_tree(proc))


def kill_switch_values() -> dict[str, set[str]]:
    """Kill-switch variable name -> the values that switch its hook off, from harness/hooks/*.py: names on a line that
    tests .lower() against off (a set literal with "0" means off/0/false, else only off), the literal arguments of
    evidence_gate.off('NAME') (off/0/false), and the mode variables of KILL_MODE_OFF."""
    vals: dict[str, set[str]] = {}
    for f in sorted((HARNESS / "harness" / "hooks").glob("*.py")):
        text = f.read_text(encoding="utf-8", errors="replace")
        for ln in text.splitlines():
            m = KILL_LINE_RE.search(ln)
            if m:
                honoured = set(KILL_VALUES) if re.search(r"[\"']0[\"']", m.group(0)) else {"off"}
                for n in KILL_NAME_RE.findall(ln):
                    vals.setdefault(n, set()).update(honoured)
        for n in KILL_OFF_CALL_RE.findall(text):
            vals.setdefault(n, set()).update(KILL_VALUES)
    for n, v in KILL_MODE_OFF.items():
        vals.setdefault(n, set()).update(v)
    return {n: v for n, v in vals.items() if n not in KILL_MODE_VARS}


def kill_switch_names() -> set[str]:
    return set(kill_switch_values())


def chk_kill_switch():
    """Report only kill-switch variables set to off/0/false, in the process, user and machine environments."""
    honoured = kill_switch_values()
    names = set(honoured)
    upper = {n.upper(): n for n in names}  # Windows variable names are case-insensitive
    if not names:
        return NA, "no kill-switch names found in harness/hooks/*.py"
    found, errors = [], []
    reg: dict[str, dict[str, str]] = {n: {} for n in names}  # name -> scope -> raw registry value
    skipped = ""
    if os.name != "nt":
        skipped = "registry scopes not read (not Windows): only the process environment was checked"
    else:
        import winreg
        for scope, sub in REG_SCOPES:
            root = winreg.HKEY_CURRENT_USER if scope == "user" else winreg.HKEY_LOCAL_MACHINE
            try:
                with winreg.OpenKey(root, sub) as key:
                    i = 0
                    while True:
                        try:
                            n, v, _t = winreg.EnumValue(key, i)
                        except OSError as exc:
                            if REG_NO_MORE_ITEMS in (getattr(exc, "winerror", None), getattr(exc, "errno", None)):
                                break  # the end of the list
                            raise  # a real read error: reported below, never taken as "nothing set"
                        i += 1
                        canon = upper.get(str(n).upper())
                        if canon and isinstance(v, str):
                            reg[canon][scope] = v
            except FileNotFoundError:
                continue  # key absent: nothing set in this scope
            except OSError as exc:
                errors.append(f"{scope} registry unreadable: {exc}")
    for n in sorted(names):
        # a newly started client sees the user scope over the machine scope; a running session keeps its inherited value
        scopes = reg[n]
        eff_scope = "user" if "user" in scopes else ("machine" if "machine" in scopes else None)
        eff = scopes.get(eff_scope) if eff_scope else None
        if eff is not None and eff.strip().lower() in honoured[n]:
            shadow = [f"{s}={scopes[s]}" for s in scopes if s != eff_scope and scopes[s] != eff]
            found.append(f"{n}={eff} ({eff_scope}{'; overrides ' + ', '.join(shadow) if shadow else ''})")
        proc_v = os.environ.get(n)
        if proc_v is not None and proc_v != eff and proc_v.strip().lower() in honoured[n]:
            found.append(f"{n}={proc_v} (this process)")
    if found:
        return FAIL, ", ".join(found) + (("; " + "; ".join(errors)) if errors else "")
    if errors:  # a scope that could not be read is a failed check, not "nothing set" (the audit exit code must show it)
        return FAIL, "; ".join(errors)
    if skipped:
        return NA, skipped
    return OK, f"{len(names)} kill switches checked, none off"


def codex_commands_lacking_flag(cfg, where: str) -> list[str]:
    bad = []
    groups = (cfg.get("hooks") or {}) if isinstance(cfg, dict) else {}
    for event, gl in (groups.items() if isinstance(groups, dict) else []):
        for g in (gl if isinstance(gl, list) else []):
            for h in (g.get("hooks", []) if isinstance(g, dict) else []):
                if not isinstance(h, dict):
                    continue
                for k in ("command", "commandWindows"):
                    c = h.get(k)
                    # only harness hooks (started through run_hook.py) must name the client; a foreign hook the
                    # installer keeps (e.g. the operator's own completion_gate.py) is not the harness's to judge
                    if c and "run_hook.py" in str(c) and CODEX_CLIENT_FLAG not in str(c):
                        bad.append(f"{where} {event} {k}: {hook_name(str(c))}")
    return bad


def codex_source_keys(hooks_json: Path) -> set[str]:
    """B40: the hooks/list sourcePath values that count as the same layer as `hooks_json`: the hooks.json itself and the
    config.toml beside it (Codex reports inline [hooks] tables with that sourcePath). Any other path stays foreign."""
    return {_codex_key(hooks_json), _codex_key(hooks_json.parent / "config.toml")}


def chk_codex_cfg(cwd: Path | None = None, home: bool = False):
    hp = HOME / ".codex" / "hooks.json"
    cfg = load_json(hp)
    if cfg is None:
        return FAIL, "hooks.json unreadable"
    probs = []
    if home:
        probs += [f"{m} lacks {CODEX_CLIENT_FLAG}" for m in codex_commands_lacking_flag(cfg, "~/.codex/hooks.json")]
    raw = json.dumps(cfg)
    for s in ("rules_gate.py", "agent_visibility.py"):
        if s not in raw:
            probs.append(f"{s} not in hooks.json")
    try:
        if RULES_BLOCK not in (HOME / ".codex" / "AGENTS.md").read_text(encoding="utf-8", errors="replace"):
            probs.append(f"AGENTS.md lacks '{RULES_BLOCK}'")
    except OSError:
        probs.append("AGENTS.md unreadable")
    target = cwd or HARNESS
    if CODEX_HOOKS_ERROR:
        probs.append(CODEX_HOOKS_ERROR)
        return FAIL, "; ".join(probs)
    item = CODEX_HOOKS_BY_CWD.get(_codex_key(target))
    if item is None:
        probs.append(f"Codex app-server hooks/list returned no entry for {target}")
        return FAIL, "; ".join(probs)

    hooks = item.get("hooks")
    warnings = item.get("warnings", [])
    errors = item.get("errors", [])
    if not isinstance(hooks, list) or not isinstance(warnings, list) or not isinstance(errors, list):
        probs.append("Codex app-server hooks/list returned invalid hooks, warnings, or errors")
        return FAIL, "; ".join(probs)
    if any(not isinstance(hook, dict) for hook in hooks):
        probs.append("Codex app-server hooks/list returned an invalid hook entry")
        return FAIL, "; ".join(probs)
    untrusted = [h for h in hooks if h.get("source") in ("user", "project") and h.get("trustStatus") != "trusted"]
    for hook in untrusted:
        probs.append("untrusted hook: " + ", ".join(str(hook.get(k) or "-") for k in ("source", "eventName", "statusMessage", "sourcePath")))
    if warnings:
        probs.append("Codex app-server warnings: " + json.dumps(warnings, ensure_ascii=False))
    if errors:
        probs.append("Codex app-server errors: " + json.dumps(errors, ensure_ascii=False))

    if home:
        # B28b: every harness hook (a command that runs run_hook.py) configured in ~/.codex/hooks.json must be reported
        # by hooks/list for that home as loaded, and hooks/list must not report it as disabled (field "enabled").
        user_groups = (cfg.get("hooks") or {}) if isinstance(cfg, dict) else {}
        listed_user = [h for h in hooks if h.get("source") == "user"
                       and _codex_key(h.get("sourcePath", "")) in codex_source_keys(hp)]
        loaded_ids = Counter(listed_hook_identity(h) for h in listed_user)
        for event, groups in (user_groups.items() if isinstance(user_groups, dict) else []):
            for group in (groups if isinstance(groups, list) else []):
                for handler in (group.get("hooks", []) if isinstance(group, dict) else []):
                    if not isinstance(handler, dict):
                        continue
                    command = handler.get("commandWindows") or handler.get("command") or ""
                    if "run_hook.py" not in str(command):
                        continue
                    ident = codex_hook_identity(event, group.get("matcher"), handler)
                    if loaded_ids[ident] <= 0:
                        probs.append(f"~/.codex/hooks.json harness hook not loaded by hooks/list: {event}/{group.get('matcher') or '*'}: {hook_name(str(command))}")
                    else:
                        loaded_ids[ident] -= 1
        for h in listed_user:
            if "run_hook.py" in str(h.get("command") or "") and h.get("enabled") is False:
                probs.append(f"~/.codex/hooks.json harness hook disabled in hooks/list: {h.get('eventName')}/{h.get('matcher') or '*'}: {hook_name(str(h.get('command')))}")

    project_hooks_path = target / ".codex" / "hooks.json"
    if project_hooks_path.is_file():
        project_cfg = load_json(project_hooks_path)
        if project_cfg is None:
            probs.append(f"project hooks.json unreadable: {project_hooks_path}")
        else:
            probs += [f"{m} lacks {CODEX_CLIENT_FLAG}" for m in codex_commands_lacking_flag(project_cfg, ".codex/hooks.json")]
            project_groups = (project_cfg.get("hooks") or {}) if isinstance(project_cfg, dict) else None
            if not isinstance(project_groups, dict) or any(
                    not isinstance(groups, list) or any(not isinstance(group, dict) or not isinstance(group.get("hooks", []), list)
                                                       for group in groups)
                    for groups in project_groups.values()):
                probs.append(f"project hooks.json has invalid hooks structure: {project_hooks_path}")
                project_groups = {}
            expected = Counter(
                codex_hook_identity(event, group.get("matcher"), handler)
                for event, groups in project_groups.items()
                for group in groups
                for handler in group.get("hooks", [])
                if isinstance(handler, dict)
            )
            loaded = Counter(
                listed_hook_identity(hook)
                for hook in hooks
                if hook.get("source") == "project"
                and _codex_key(hook.get("sourcePath", "")) in codex_source_keys(project_hooks_path)
            )
            missing = expected - loaded
            if missing:
                labels = []
                for identity, count in missing.items():
                    event, matcher, _kind, command = identity
                    command_names = re.findall(r"[\w.-]+\.[A-Za-z0-9]{1,10}", command)
                    command_name = command_names[-1] if command_names else "command"
                    labels.extend(f"{event}/{matcher or '*'}: {command_name}" for _ in range(count))
                probs.append(f"project hooks.json handlers missing from hooks/list: {', '.join(labels)} ({project_hooks_path})")

    if probs:
        return FAIL, "; ".join(probs)
    note = f"; {CODEX_EXE_NOTE}" if CODEX_EXE_NOTE else ""
    return OK, f"{len(hooks)} hooks listed; warnings=[]; errors=[]{note}"


# ---- orchestration ---------------------------------------------------------------------------------------------------------------

def audit_project(d: Path, github: bool) -> dict:
    cells = {}

    def put(col, fn, *a):
        try:
            cells[col] = fn(*a)
        except Exception as e:  # a broken check must not hide the other cells
            cells[col] = (FAIL, f"check crashed: {type(e).__name__}: {e}")

    put("hooks_path", chk_hooks_path, d)
    put("tools_current", chk_tools_current, d)
    put("prepush_current", chk_prepush_current, d)
    put("ci_local", chk_ci_local, d)
    put("ci_github", chk_ci_github, d, github)
    put("ledger_whole", chk_ledger_whole, d)
    put("gates_live", chk_gates_live, d, HOME / ".claude" / "settings.json", "claude", True)
    put("client_cfg", chk_codex_cfg, d)
    cells["kill_switch"] = (NA, "")
    return {"project": d.name, "cells": cells}


def audit_client(name: str) -> dict:
    cells = {c: (NA, "") for c in COLS}

    def put(col, fn, *a):
        try:
            cells[col] = fn(*a)
        except Exception as e:  # a broken check must not abort the whole audit
            cells[col] = (FAIL, f"check crashed: {type(e).__name__}: {e}")

    if name == "claude-home":
        put("gates_live", chk_gates_live, HARNESS, HOME / ".claude" / "settings.json", "claude", False)
        put("client_cfg", chk_claude_cfg)
        put("kill_switch", chk_kill_switch)
    else:
        put("gates_live", chk_gates_live, HARNESS, HOME / ".codex" / "hooks.json", "codex", False)
        put("client_cfg", chk_codex_cfg, HARNESS, True)
        put("kill_switch", chk_kill_switch)
    return {"project": name, "cells": cells}


def render(rows: list[dict]) -> str:
    hdr = ["project"] + COLS
    body = [[r["project"]] + [r["cells"][c][0] for c in COLS] for r in rows]
    w = [max(len(hdr[i]), *(len(b[i]) for b in body)) for i in range(len(hdr))]
    line = lambda cells: "| " + " | ".join(c.ljust(w[i]) for i, c in enumerate(cells)) + " |"
    out = [line(hdr), "|" + "|".join("-" * (x + 2) for x in w) + "|"] + [line(b) for b in body]
    out += ["", "Details (FAIL and n.a. cells with a reason; OK cells have none):"]
    for r in rows:
        for c in COLS:
            st, why = r["cells"][c]
            if st == FAIL or (st == NA and why):
                out.append(f"- {r['project']}.{c} {st}: {why}")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=r"D:\Repo")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-github", action="store_true")
    ap.add_argument("--project", help="audit one row by name (project dir name, claude-home or codex-home)")
    a = ap.parse_args(argv)
    names = {a.project.lower()} if a.project else None
    rows = []
    projects = [d for d in discover(Path(a.root)) if names is None or d.name.lower() in names]
    codex_cwds = [d for d in projects]
    if names is None or "codex-home" in names:
        codex_cwds.append(HARNESS)
    global CODEX_HOOKS_BY_CWD, CODEX_HOOKS_ERROR
    if codex_cwds:
        try:
            CODEX_HOOKS_BY_CWD, CODEX_HOOKS_ERROR = codex_hooks_list(codex_cwds)
        except BaseException as exc:  # the audit must still print its table; every client cell then reads FAIL
            if isinstance(exc, KeyboardInterrupt):
                raise
            CODEX_HOOKS_BY_CWD, CODEX_HOOKS_ERROR = {}, f"codex hooks/list crashed: {type(exc).__name__}: {exc}"
    for d in projects:
        rows.append(audit_project(d, not a.no_github))
    for c in ("claude-home", "codex-home"):
        if names is None or c in names:
            rows.append(audit_client(c))
    if a.project and not rows:
        print(f"audit_projects: no row named {a.project}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps([{"project": r["project"], "cells": {c: {"status": s, "detail": w} for c, (s, w) in r["cells"].items()}}
                          for r in rows], indent=2))
    else:
        print(render(rows))
    return 0 if all(s in (OK, NA) for r in rows for s, _ in r["cells"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
