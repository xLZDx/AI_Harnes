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
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
HARNESS = HERE.parent
sys.path.insert(0, str(HERE))

import workflow_guard  # noqa: E402

HOME = Path.home()
SKIP = {"virtual_marketing_company"}
COLS = ["hooks_path", "tools_current", "prepush_current", "ci_local", "ci_github", "ledger_whole", "gates_live", "client_cfg"]
TOOL_FILES = ["ledger.py", "report.py", "workflow_guard.py", "plan_ledger.py"]
BAD = set(workflow_guard.BAD)
AUDIT_TMP = Path(r"D:\tmp\harness_audit")
DENY_CMDS = ["npm test", "npx vitest", "npm run lint", "tsc --noEmit", "npm run verify", "pytest", "gh workflow run ci.yml", "git push"]
ALLOW_CMDS = ["git status"]
RULES_BLOCK = "Operator development rules"

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


def chk_ci_github(d: Path, enabled: bool):
    if not enabled:
        return NA, "skipped (--no-github)"
    slug = origin_slug(d)
    if not slug:
        return NA, "no GitHub remote"
    r = run(["gh", "workflow", "list", "--repo", slug, "--all", "--json", "name,state,path"], timeout=20)
    if r is None:
        return FAIL, f"{slug}: gh timed out or missing"
    if r.returncode != 0:
        return FAIL, f"{slug}: gh failed: {(r.stderr or r.stdout).strip()[:120]}"
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
        local = d / path
        if local.is_file():
            text = local.read_text(encoding="utf-8", errors="replace")
        else:
            a = run(["gh", "api", f"repos/{slug}/contents/{path}", "--jq", ".content"], timeout=20)
            if a is not None and a.returncode == 0:
                try:
                    text = base64.b64decode(a.stdout.strip()).decode("utf-8", "replace")
                except Exception:
                    text = None
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
    AUDIT_TMP.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="g_", dir=str(AUDIT_TMP)))
    deny = list(DENY_CMDS)
    skipped = ""
    if is_project_dir and approval_on_head(cwd):
        deny.remove("git push")
        skipped = "git push skipped (ledger approval is on HEAD)"
    todo = [(c, True) for c in deny] + [(c, False) for c in ALLOW_CMDS]
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(lambda t: run_gate(handlers, t[0], cwd, tmp, client), todo))
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
        return FAIL, "; ".join(msgs) + extra
    return OK, (skipped or f"{len(deny)} denied, git status allowed") + (f" [hook notes: {'; '.join(sorted(set(notes))[:3])}]" if notes else "")


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


def chk_codex_cfg():
    hp = HOME / ".codex" / "hooks.json"
    cfg = load_json(hp)
    if cfg is None:
        return FAIL, "hooks.json unreadable"
    probs = []
    raw = json.dumps(cfg)
    for s in ("rules_gate.py", "agent_visibility.py"):
        if s not in raw:
            probs.append(f"{s} not in hooks.json")
    try:
        if RULES_BLOCK not in (HOME / ".codex" / "AGENTS.md").read_text(encoding="utf-8", errors="replace"):
            probs.append(f"AGENTS.md lacks '{RULES_BLOCK}'")
    except OSError:
        probs.append("AGENTS.md unreadable")
    expected = []
    for ev, groups in (cfg.get("hooks") or {}).items():
        for gi, g in enumerate(groups):
            for hi, _ in enumerate(g.get("hooks", [])):
                expected.append(f"{norm_path(hp)}:{snake(ev)}:{gi}:{hi}")
    trusted = set()
    try:
        import tomllib
        toml = tomllib.loads((HOME / ".codex" / "config.toml").read_text(encoding="utf-8"))
        for k, v in ((toml.get("hooks") or {}).get("state") or {}).items():
            if isinstance(v, dict) and v.get("trusted_hash"):
                trusted.add(norm_path(k.rsplit(":", 3)[0]) + ":" + ":".join(k.rsplit(":", 3)[1:]))
    except Exception as e:
        probs.append(f"config.toml unreadable: {e}")
    untrusted = [e for e in expected if e not in trusted]
    if untrusted:
        probs.append(f"untrusted {len(untrusted)} of {len(expected)} hook handlers")
    return (FAIL, "; ".join(probs)) if probs else (OK, f"{len(expected)} handlers, all trusted")


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
    cells["client_cfg"] = (NA, "")
    return {"project": d.name, "cells": cells}


def audit_client(name: str) -> dict:
    cells = {c: (NA, "") for c in COLS}
    if name == "claude-home":
        cells["gates_live"] = chk_gates_live(HARNESS, HOME / ".claude" / "settings.json", "claude", False)
        cells["client_cfg"] = chk_claude_cfg()
    else:
        cells["gates_live"] = chk_gates_live(HARNESS, HOME / ".codex" / "hooks.json", "codex", False)
        cells["client_cfg"] = chk_codex_cfg()
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
    for d in discover(Path(a.root)):
        if names is None or d.name.lower() in names:
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
