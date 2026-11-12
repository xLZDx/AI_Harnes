"""S13-R8 tests for tools/audit_projects.py. Temp projects and a fake home only under D:\\tmp\\harness_v2_tests.
The audit has --root (projects) but NO override for the home folders (module constant HOME) nor for its scratch dir (AUDIT_TMP),
so tests run main() in-process with those two module attributes patched to temp folders. The gates are the real
harness/hooks/rules_gate.py run through the audit's own entry point (main -> audit_project/audit_client -> chk_gates_live).
Run: C:\\Python314\\python.exe -I tests/test_s13_audit.py"""
import contextlib
import io
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools"))
import hv2  # noqa: E402
import audit_projects as au  # noqa: E402
import ledger_sync  # noqa: E402

TOOLS = HERE.parent / "tools"
HOOKS = HERE.parent / "harness" / "hooks"
PYX = hv2.PY.replace("\\", "/")
RUN_HOOK = str(HOOKS / "run_hook.py").replace("\\", "/")
NOOP = None

PLAN = ("---\nproject: demo\n---\n# Plan\n\n## Roadmap\n| Sprint | Title | Status | Goal |\n|---|---|---|---|\n"
        "| S00 | Zero | NOT_STARTED | expanded below |\n\n"
        "## Sprint S00 - Zero\n\n| Task | Title | Status |\n|---|---|---|\n"
        "| ST-A#1 | a | ACCEPTED |\n| ST-A#2 | b | NOT_STARTED |\n| ST-A#3 | c | NOT_STARTED |\n")
EMPTY_LEDGER = {"version": 1, "project": "demo", "approval": None, "events": [], "sprints": []}
WF_DISPATCH = "name: ci\non:\n  workflow_dispatch:\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"


def _w(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


# ---------- fixtures ----------
def _hook_cmd(script):
    return f"{PYX} -s -X utf8 {RUN_HOOK} {script} --harness-client=claude"


def _noop_cmd():
    global NOOP
    if NOOP is None:
        NOOP = hv2.fresh("noop") / "noop.py"
        _w(NOOP, "import sys\nsys.stdin.read()\nsys.exit(0)\n")
    return f"{PYX} {str(NOOP).replace(chr(92), '/')}"


def _handler(cmd, timeout=20):
    return {"type": "command", "command": cmd, "commandWindows": cmd, "timeout": timeout}


def _claude_home(rules_gate=True, full=True, rules_block=True, pre_cmds=None):
    home = hv2.fresh("home")
    cmds = pre_cmds if pre_cmds is not None else ([_hook_cmd("rules_gate.py")] if rules_gate else [_noop_cmd()])
    hooks = {"PreToolUse": [{"matcher": "Bash|PowerShell", "hooks": [_handler(c) for c in cmds]}]}
    if full:
        hooks["PreToolUse"].append({"matcher": "Agent|Task", "hooks": [_handler(_hook_cmd("agent_visibility.py"))]})
        hooks["SubagentStart"] = [{"hooks": [_handler(_hook_cmd("agent_visibility.py"))]}]
        hooks["SubagentStop"] = [{"hooks": [_handler(_hook_cmd("agent_visibility.py"))]}]
    _w(home / ".claude" / "settings.json", json.dumps({"hooks": hooks}))
    _w(home / ".claude" / "CLAUDE.md", "# rules\n## Operator development rules (x)\n" if rules_block else "# nothing\n")
    return home


def _codex_home(trusted=True, rules_block=True):
    home = hv2.fresh("home")
    cmd = _hook_cmd("rules_gate.py").replace("claude", "codex")
    hooks = {"PreToolUse": [{"matcher": "Bash", "hooks": [_handler(cmd)]}],
             "SubagentStart": [{"hooks": [_handler(_hook_cmd("agent_visibility.py"))]}]}
    hp = home / ".codex" / "hooks.json"
    _w(hp, json.dumps({"hooks": hooks}))
    toml = ""
    if trusted:
        for ev, n in (("pre_tool_use", 1), ("subagent_start", 1)):
            toml += f'[hooks.state."{hp}:{ev}:0:0"]\ntrusted_hash = "sha256:abc"\n'.replace("\\", "/")
    _w(home / ".codex" / "config.toml", toml or "x = 1\n")
    _w(home / ".codex" / "AGENTS.md", "## Operator development rules\n" if rules_block else "nothing\n")
    return home


def _mk_project(root, name, current=True, tools="current", prepush="current", workflow="dispatch", ledger="plan", hooks_path=True):
    p = root / name
    p.mkdir(parents=True)
    hv2.git(p, "init", "-q", "-b", "main")
    hv2.git(p, "config", "user.name", "t")
    hv2.git(p, "config", "user.email", "t@t")
    _w(p / "README.md", "hello\n")
    hv2.git(p, "add", "README.md")
    assert hv2.git(p, "commit", "-q", "-m", "init").rc == 0
    t = p / ".harness-tools"
    t.mkdir()
    if tools != "none":
        for n in au.TOOL_FILES:
            shutil.copyfile(TOOLS / n, t / n)
        if tools == "stale":
            (t / "ledger.py").write_bytes((TOOLS / "ledger.py").read_bytes() + b"\n# stale\n")
    if prepush != "none":
        (t / "githooks").mkdir()
        shutil.copyfile(TOOLS / "githooks" / "pre-push", t / "githooks" / "pre-push")
        if prepush == "stale":
            with open(t / "githooks" / "pre-push", "ab") as f:
                f.write(b"\n# stale\n")
    if hooks_path:
        hv2.git(p, "config", "core.hooksPath", ".harness-tools/githooks")
    if workflow == "dispatch":
        _w(p / ".github" / "workflows" / "ci.yml", WF_DISPATCH)
    elif workflow:
        _w(p / ".github" / "workflows" / "ci.yml", workflow)
    _w(p / "PROJECT_PLAN.md", PLAN)
    (p / ".harness").mkdir()
    _w(p / ".harness" / "ledger.json", json.dumps(EMPTY_LEDGER))
    if ledger == "plan":
        ledger_sync.BACKUP_DIR = hv2.fresh("backup")
        res = ledger_sync.sync_project(p, False)
        assert res["status"] in ("changed", "created"), res
    return p


def _audit(root, home, *args, project=None):
    au.HOME = home
    au.AUDIT_TMP = hv2.fresh("auditscratch")
    ledger_sync.BACKUP_DIR = hv2.fresh("backup")
    argv = ["--root", str(root), "--no-github", *args]
    if project:
        argv += ["--project", project]
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = au.main(argv)
    return rc, out.getvalue(), err.getvalue()


def _cells(root, home, name, **kw):
    rc, out, _ = _audit(root, home, "--json", project=name, **kw)
    rows = {r["project"]: r["cells"] for r in json.loads(out)}
    return rc, rows[name]


def _snap(base):
    return {str(p.relative_to(base)): (p.stat().st_size, p.stat().st_mtime_ns) if p.is_file() else None
            for p in sorted(base.rglob("*"))}


# ---------- criterion 1 ----------
def test_current_project_cells_ok():
    root, home = hv2.fresh("root"), _claude_home()
    _mk_project(root, "good")
    rc, c = _cells(root, home, "good")
    for col in ("hooks_path", "tools_current", "prepush_current", "ci_local", "ledger_whole", "gates_live"):
        assert c[col]["status"] == "OK", (col, c[col])
    assert c["ci_github"]["status"] == "n.a."  # --no-github
    assert c["client_cfg"]["status"] == "n.a."
    assert rc == 0, (rc, c)


def test_stale_tools_fail_with_reason_naming_file():
    root, home = hv2.fresh("root"), _claude_home()
    _mk_project(root, "p", tools="stale")
    rc, c = _cells(root, home, "p")
    assert c["tools_current"]["status"] == "FAIL" and "ledger.py" in c["tools_current"]["detail"], c["tools_current"]
    assert c["prepush_current"]["status"] == "OK"
    assert rc == 1


def test_missing_tools_fail():
    root, home = hv2.fresh("root"), _claude_home()
    _mk_project(root, "p", tools="none")
    _, c = _cells(root, home, "p")
    assert c["tools_current"]["status"] == "FAIL" and "missing" in c["tools_current"]["detail"], c["tools_current"]


def test_missing_prepush_fails_prepush_and_hooks_path():
    root, home = hv2.fresh("root"), _claude_home()
    _mk_project(root, "p", prepush="none")
    rc, c = _cells(root, home, "p")
    assert c["prepush_current"]["status"] == "FAIL" and "missing" in c["prepush_current"]["detail"], c["prepush_current"]
    assert c["hooks_path"]["status"] == "FAIL", c["hooks_path"]
    assert rc == 1


def test_stale_prepush_fails():
    root, home = hv2.fresh("root"), _claude_home()
    _mk_project(root, "p", prepush="stale")
    _, c = _cells(root, home, "p")
    assert c["prepush_current"]["status"] == "FAIL" and "differs" in c["prepush_current"]["detail"], c["prepush_current"]
    assert c["tools_current"]["status"] == "OK"


def test_hooks_path_unset_and_wrong_target_fail():
    root, home = hv2.fresh("root"), _claude_home()
    _mk_project(root, "unset", hooks_path=False)
    p = _mk_project(root, "wrong")
    hv2.git(p, "config", "core.hooksPath", ".githooks")
    _, c1 = _cells(root, home, "unset")
    _, c2 = _cells(root, home, "wrong")
    assert c1["hooks_path"]["status"] == "FAIL" and "not set" in c1["hooks_path"]["detail"], c1["hooks_path"]
    assert c2["hooks_path"]["status"] == "FAIL" and ".githooks" in c2["hooks_path"]["detail"], c2["hooks_path"]


def test_auto_trigger_workflows_fail_each_event():
    root, home = hv2.fresh("root"), _claude_home()
    cases = {"push": "name: x\non: push\njobs: {}\n",
             "pull_request": "name: x\non:\n  pull_request:\n    branches: [main]\njobs: {}\n",
             "schedule": "name: x\non:\n  schedule:\n    - cron: '0 0 * * *'\njobs: {}\n",
             "push-mixed": "name: x\non: [workflow_dispatch, push]\njobs: {}\n"}
    for i, (ev, text) in enumerate(cases.items()):
        _mk_project(root, f"w{i}", workflow=text)
        _, c = _cells(root, home, f"w{i}")
        assert c["ci_local"]["status"] == "FAIL" and "ci.yml" in c["ci_local"]["detail"], (ev, c["ci_local"])
        assert ev.split("-")[0] in c["ci_local"]["detail"], (ev, c["ci_local"])


def test_no_workflows_is_na_not_fail():
    root, home = hv2.fresh("root"), _claude_home()
    _mk_project(root, "p", workflow=None)
    rc, c = _cells(root, home, "p")
    assert c["ci_local"]["status"] == "n.a.", c["ci_local"]
    assert rc == 0


def test_empty_ledger_with_plan_fails_and_synced_ledger_ok():
    root, home = hv2.fresh("root"), _claude_home()
    _mk_project(root, "empty", ledger="empty")
    _mk_project(root, "whole")
    _, ce = _cells(root, home, "empty")
    _, cw = _cells(root, home, "whole")
    d = ce["ledger_whole"]["detail"]
    assert ce["ledger_whole"]["status"] == "FAIL" and "3" in d and "0" in d, ce["ledger_whole"]
    assert cw["ledger_whole"]["status"] == "OK", cw["ledger_whole"]


def test_fully_broken_project_exit_1_and_good_one_exit_0():
    root, home = hv2.fresh("root"), _claude_home()
    _mk_project(root, "bad", tools="stale", prepush="none", workflow="name: x\non: push\njobs: {}\n", ledger="empty")
    rc, c = _cells(root, home, "bad")
    fails = {k for k, v in c.items() if v["status"] == "FAIL"}
    assert {"hooks_path", "tools_current", "prepush_current", "ci_local", "ledger_whole"} <= fails, fails
    assert rc == 1


# ---------- criterion 2: gates_live through the audit entry point ----------
def test_gates_live_ok_with_real_rules_gate():
    root, home = hv2.fresh("root"), _claude_home()
    _mk_project(root, "p")
    _, c = _cells(root, home, "p")
    g = c["gates_live"]
    assert g["status"] == "OK" and "8 denied" in g["detail"] and "git status allowed" in g["detail"], g


def test_gates_live_fails_listing_every_denied_command_when_gate_is_noop():
    root, home = hv2.fresh("root"), _claude_home(rules_gate=False)
    _mk_project(root, "p")
    rc, c = _cells(root, home, "p")
    g = c["gates_live"]
    assert g["status"] == "FAIL", g
    for cmd in ("npm test", "npx vitest", "npm run lint", "tsc --noEmit", "npm run verify", "pytest",
                "gh workflow run ci.yml", "git push"):
        assert cmd in g["detail"].split("not denied:")[1].split(";")[0], (cmd, g)
    assert "rules_gate.py not registered" in g["detail"]
    assert rc == 1


def test_gates_live_flags_wrongly_denied_git_status():
    root = hv2.fresh("root")
    deny_all = hv2.fresh("denyall") / "deny_all.py"
    _w(deny_all, "import sys\nsys.stdin.read()\nsys.stderr.write('no')\nsys.exit(2)\n")
    home = _claude_home(pre_cmds=[_hook_cmd("rules_gate.py"), f"{PYX} {str(deny_all).replace(chr(92), '/')}"])
    _mk_project(root, "p")
    _, c = _cells(root, home, "p")
    g = c["gates_live"]
    assert g["status"] == "FAIL" and "should be allowed but denied: git status" in g["detail"], g


def test_gates_live_each_command_individually_by_real_rules_gate():
    # Per-command proof: a config holding rules_gate only, with ONE command class removed from DENY_CMDS at a time must still be OK,
    # and a config with a hook that denies only that command class must not hide the others. Here: audit each deny command alone.
    root, home = hv2.fresh("root"), _claude_home()
    p = _mk_project(root, "p")
    au.HOME = home
    au.AUDIT_TMP = hv2.fresh("auditscratch")
    handlers = au.registered_handlers(au.load_json(home / ".claude" / "settings.json"), "PreToolUse", "Bash")
    tmp = hv2.fresh("gtmp")
    for cmd in ("npm test", "npx vitest", "npm run lint", "tsc --noEmit", "npm run verify", "pytest", "gh workflow run ci.yml", "git push"):
        by, _ = au.run_gate(handlers, cmd, p, tmp, "claude")
        assert by == "rules_gate.py", (cmd, by)
    by, _ = au.run_gate(handlers, "git status", p, tmp, "claude")
    assert by is None, by


def test_gates_live_git_push_skipped_when_approval_on_head():
    root, home = hv2.fresh("root"), _claude_home()
    p = _mk_project(root, "p")
    head = hv2.git(p, "rev-parse", "HEAD").out.strip()
    led = json.loads((p / ".harness" / "ledger.json").read_text(encoding="utf-8"))
    led["approval"] = {"head": head, "result": "APPROVE"}
    (p / ".harness" / "ledger.json").write_text(json.dumps(led), encoding="utf-8", newline="\n")
    _, c = _cells(root, home, "p")
    g = c["gates_live"]
    assert "git push skipped" in g["detail"], g


def test_gates_live_unreadable_or_empty_settings_fail():
    root = hv2.fresh("root")
    _mk_project(root, "p")
    home = hv2.fresh("home")  # no settings.json at all
    _, c = _cells(root, home, "p")
    assert c["gates_live"]["status"] == "FAIL" and "unreadable" in c["gates_live"]["detail"], c["gates_live"]
    _w(home / ".claude" / "settings.json", json.dumps({"hooks": {}}))
    _, c = _cells(root, home, "p")
    assert c["gates_live"]["status"] == "FAIL" and "no PreToolUse" in c["gates_live"]["detail"], c["gates_live"]


def test_gates_live_with_args_array_hook_format_like_real_claude_settings():
    # The real ~/.claude/settings.json and out/claude/settings.hooks.json register hooks as {"command": python.exe, "args": [...]}.
    root = hv2.fresh("root")
    home = hv2.fresh("home")
    h = {"type": "command", "command": hv2.PY, "timeout": 20,
         "args": ["-s", "-X", "utf8", str(HOOKS / "run_hook.py"), "rules_gate.py", "--harness-client=claude"]}
    _w(home / ".claude" / "settings.json", json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash|PowerShell", "hooks": [h]}]}}))
    _mk_project(root, "p")
    _, c = _cells(root, home, "p")
    assert c["gates_live"]["status"] == "OK", c["gates_live"]


def test_client_rows_claude_home_ok_and_fail():
    root = hv2.fresh("root")
    _, c = _cells(root, _claude_home(), "claude-home")
    assert c["gates_live"]["status"] == "OK", c["gates_live"]
    assert c["client_cfg"]["status"] == "OK", c["client_cfg"]
    assert c["hooks_path"]["status"] == "n.a."
    rc, c = _cells(root, _claude_home(rules_gate=False, full=False, rules_block=False), "claude-home")
    d = c["client_cfg"]["detail"]
    assert c["client_cfg"]["status"] == "FAIL" and "rules_gate.py not in PreToolUse" in d and "Operator development rules" in d, c["client_cfg"]
    assert "agent_visibility.py not in SubagentStart" in d, d
    assert c["gates_live"]["status"] == "FAIL" and rc == 1


def test_client_rows_codex_home_ok_and_untrusted_fail():
    root = hv2.fresh("root")
    _, c = _cells(root, _codex_home(), "codex-home")
    assert c["gates_live"]["status"] == "OK", c["gates_live"]
    assert c["client_cfg"]["status"] == "OK", c["client_cfg"]
    rc, c = _cells(root, _codex_home(trusted=False, rules_block=False), "codex-home")
    d = c["client_cfg"]["detail"]
    assert c["client_cfg"]["status"] == "FAIL" and "untrusted 2 of 2" in d and "AGENTS.md lacks" in d, d
    assert rc == 1


# ---------- criterion 3: read-only ----------
def test_audit_writes_nothing_into_project_or_fake_home():
    root, home = hv2.fresh("root"), _claude_home()
    _mk_project(root, "good")
    _mk_project(root, "bad", tools="stale", prepush="none", workflow="name: x\non: push\njobs: {}\n", ledger="empty")
    before_r, before_h = _snap(root), _snap(home)
    rc, out, _ = _audit(root, home)
    assert rc == 1 and "bad" in out
    after_r, after_h = _snap(root), _snap(home)
    assert before_r == after_r, {k for k in set(before_r) | set(after_r) if before_r.get(k) != after_r.get(k)}
    assert before_h == after_h


def test_audit_does_not_touch_real_claude_and_codex_files():
    watched = []
    for base, names in ((Path.home() / ".claude", ["settings.json", "CLAUDE.md"]),
                        (Path.home() / ".codex", ["hooks.json", "config.toml", "AGENTS.md"])):
        watched += [base / n for n in names if (base / n).exists()]
    hooks_dir = Path.home() / ".claude" / "hooks"
    if hooks_dir.is_dir():
        watched += [p for p in hooks_dir.glob("*.py")]
    snap = lambda: {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in watched}
    before = snap()
    root = hv2.fresh("root")  # empty root: only the two client rows, run against the REAL home
    au.HOME = Path.home()
    au.AUDIT_TMP = hv2.fresh("auditscratch")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        au.main(["--root", str(root), "--no-github"])
    assert "claude-home" in out.getvalue() and "codex-home" in out.getvalue()
    assert snap() == before


# ---------- criterion 4: json, --project, discovery ----------
def _table_names(text):
    names = []
    for ln in text.splitlines()[2:]:
        if not ln.startswith("|"):
            break
        names.append([c.strip() for c in ln.strip("|").split("|")][0])
    return names


def test_json_parses_and_matches_table_rows_and_cells():
    root, home = hv2.fresh("root"), _claude_home()
    _mk_project(root, "alpha")
    _mk_project(root, "beta", tools="stale")
    _, table, _ = _audit(root, home)
    _, js, _ = _audit(root, home, "--json")
    data = json.loads(js)
    names = [r["project"] for r in data]
    assert names == _table_names(table) == ["alpha", "beta", "claude-home", "codex-home"], (names, _table_names(table))
    for r in data:
        assert list(r["cells"]) == au.COLS, list(r["cells"])
        for v in r["cells"].values():
            assert v["status"] in ("OK", "FAIL", "n.a.") and isinstance(v["detail"], str)
    lines = {ln.split("|")[1].strip(): [c.strip() for c in ln.strip("|").split("|")[1:]] for ln in table.splitlines()[2:2 + len(data)]}
    for r in data:
        assert lines[r["project"]] == [r["cells"][c]["status"] for c in au.COLS], r["project"]
    assert table.splitlines()[0].count("|") == 10 and "gates_live" in table.splitlines()[0]


def test_project_limits_to_one_row_case_insensitive_and_unknown_is_rc2():
    root, home = hv2.fresh("root"), _claude_home()
    _mk_project(root, "alpha")
    _mk_project(root, "beta")
    rc, js, _ = _audit(root, home, "--json", project="ALPHA")
    assert [r["project"] for r in json.loads(js)] == ["alpha"] and rc == 0
    _, js, _ = _audit(root, home, "--json", project="codex-home")
    assert [r["project"] for r in json.loads(js)] == ["codex-home"]
    rc, out, err = _audit(root, home, project="nope")
    assert rc == 2 and "no row named nope" in err and out == "", (rc, out, err)


def test_discovery_skips_virtual_marketing_company_and_non_harness_dirs():
    root, home = hv2.fresh("root"), _claude_home()
    _mk_project(root, "alpha")
    _mk_project(root, "Virtual_marketing_company")
    (root / "plain_dir").mkdir()
    (root / "only_ledger" / ".harness").mkdir(parents=True)
    _w(root / "only_ledger" / ".harness" / "ledger.json", json.dumps(EMPTY_LEDGER))
    _w(root / "a_file.txt", "x")
    _, js, _ = _audit(root, home, "--json")
    names = [r["project"] for r in json.loads(js)]
    assert names == ["alpha", "only_ledger", "claude-home", "codex-home"], names
    rc, _, err = _audit(root, home, project="Virtual_marketing_company")
    assert rc == 2


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
