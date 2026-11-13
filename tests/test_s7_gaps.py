"""Sprint S7 gap tests: plan-based report (T8), build wiring, plan_first_gate TaskCreate, secret_guard terraform output,
agent_visibility.touch, session_close_gate PR-owed message, and the integration replay (acceptance criterion 6).

Run: C:\\Python314\\python.exe -I tests/test_s7_gaps.py
Everything runs in temp folders (D:\\tmp\\harness_v2_tests); hook state, metrics, HOME and the hook config are redirected there,
the real ~/.harness-hooks and ~/.claude are never touched. tools/build.py is only imported (hook_config / install_project on a temp repo).
Expected values come from the module docstrings / task statement and from the fixture rows below, not from the code's output.
"""
import collections
import contextlib
import importlib.util
import io
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hv2  # noqa: E402

HOOKS = hv2.SRC / "harness" / "hooks"
PY = hv2.PY


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


plan_ledger = _load("plan_ledger_under_test", hv2.TOOLS / "plan_ledger.py")
build = _load("build_under_test", hv2.TOOLS / "build.py")

# ====================================================================================================== fixture: PROJECT_PLAN.md
# (task id, status cell as written in the plan, commit cell, acceptance cell)
S1 = [("PX-1", "ACCEPTED", "`a1b2c3d`, e4f5a6b7c8d9", "first"),
      ("PX-2", "ACCEPTED", "n/a", "x \\| y"),
      ("PX-3", "IN_REVIEW", "", "c"),
      ("PX-4", "IN_PROGRESS", "", "c"),
      ("PX-5", "PARTIAL", "", "c"),
      ("PX-6", "NOT_STARTED", "", "c"),
      ("PX-7", "BLOCKED_OPERATOR", "", "c"),
      ("PX-8", "BLOCKED_EXTERNAL", "", "c"),
      ("PX-9", "PARKED", "", "c"),
      ("PX-10", "SUPERSEDED", "", "c"),
      ("PX-11", "**ACCEPTED**", "", "c"),
      ("PX-12", "", "", "c")]  # empty status cell = NOT_STARTED
S2 = [("PY-1", "ACCEPTED", "", "c"), ("PY-2", "SUPERSEDED", "", "c"), ("PY-3", "ACCEPTED", "", "c")]
S3 = [("PZ-1", "ACCEPTED", "", "c"), ("PZ-2", "NOT_STARTED", "", "c")]
S4 = [("PW-1", "SUPERSEDED", "", "c")]
SPRINTS = [("S1", "First sprint", S1), ("S2", "Second sprint", S2), ("S3", "Third sprint", S3), ("S4", "Fourth sprint", S4)]
EXPECT_STATE = {"ACCEPTED": "done", "IN_REVIEW": "verified", "IN_PROGRESS": "in_progress", "PARTIAL": "implemented",
                "NOT_STARTED": "todo", "BLOCKED_OPERATOR": "todo", "BLOCKED_EXTERNAL": "todo", "PARKED": "todo", "SUPERSEDED": "superseded"}


def norm(cell):
    return re.sub(r"[^A-Z_]", "", cell.upper()) or "NOT_STARTED"


def plan_text(project="DemoProj"):
    out = ["---", f"project: {project}", "as_of: 2026-11-12", "active_sprint: S1", "operator_approved: true  # comment", "---",
           f"# {project} - plan", "## Roadmap", "| Sprint | Title | Status | Goal |", "|---|---|---|---|",
           "| S1 | First | ACCEPTED | ROADMAP-ROW-NOT-A-TASK |", ""]
    for sid, title, rows in SPRINTS:
        out += [f"## Sprint {sid} - {title}", "Goal: something", "",
                "| Task | Title | Acceptance | Status | Evidence | Owner | Depends | Commit |",
                "|------|-------|------------|--------|----------|-------|---------|--------|"]
        for tid, st, commit, acc in rows:
            out.append(f"| {tid} | title {tid} | {acc} | {st} | ev {tid} | owner {tid} | | {commit} |")
        out.append("")
    return "\n".join(out) + "\n"


# SUPERSEDED rows are KEPT as tasks of state "superseded": a sprint size is its placement count
KEPT = [(sid, r) for sid, _, rows in SPRINTS for r in rows]
TOTAL = len(KEPT)
ACCEPTED = sum(1 for _, r in KEPT if norm(r[1]) == "ACCEPTED")


# ====================================================================================================== 1. plan_ledger
def test_t8_plan_ledger_parse_mapping_counts_and_sprint_state():
    d = plan_ledger.parse(plan_text())
    assert d["project"] == "DemoProj" and d["source"] == "PROJECT_PLAN.md", (d["project"], d.get("source"))
    assert d["sprints"] and [s["id"] for s in d["sprints"]] == ["S1", "S2", "S3", "S4"]
    tasks = {t["id"]: t for s in d["sprints"] for t in s["tasks"]}
    assert all(tasks[i]["state"] == "superseded" for i in ("PX-10", "PY-2", "PW-1")), "SUPERSEDED rows are kept with state superseded"
    assert not any("ROADMAP" in t["title"] + "".join(t["criteria"]) for t in tasks.values()), "roadmap rows are not tasks"
    assert len(tasks) == TOTAL, (len(tasks), TOTAL)
    for _, r in KEPT:
        t = tasks[r[0]]
        assert t["state"] == EXPECT_STATE[norm(r[1])], (r[0], r[1], t["state"])
        blocked = norm(r[1]).startswith("BLOCKED") or norm(r[1]) == "PARKED"
        assert ("blocked" in t) == blocked, (r[0], t.get("blocked"))
        if blocked:
            assert t["blocked"]["class"] == norm(r[1]), t["blocked"]
    # counts per status equal the number of rows with that status in the fixture
    got = collections.Counter(t["state"] for t in tasks.values())
    want = collections.Counter(EXPECT_STATE[norm(r[1])] for _, r in KEPT)
    assert got == want, (got, want)
    assert got["done"] == ACCEPTED == 6, got  # PX-1, PX-2, PX-11, PY-1, PY-3, PZ-1
    # commits: hex ids (7-40 chars) only
    assert tasks["PX-1"]["commits"] == ["a1b2c3d", "e4f5a6b7c8d9"], tasks["PX-1"]["commits"]
    assert tasks["PX-2"]["commits"] == [] and tasks["PX-3"]["commits"] == []
    assert tasks["PX-2"]["criteria"] == ["x | y"], tasks["PX-2"]["criteria"]  # escaped pipe inside a cell
    sp = {s["id"]: s for s in d["sprints"]}
    assert sp["S1"]["state"] == "open" and sp["S3"]["state"] == "open"
    assert sp["S2"]["state"] == "closed", "all kept tasks done (SUPERSEDED ignored) -> closed"
    assert sp["S4"]["state"] == "open" and [t["state"] for t in sp["S4"]["tasks"]] == ["superseded"], "a sprint with only superseded tasks is not 'closed'"


def test_t8_plan_ledger_sprint_closed_only_when_every_kept_task_is_done():
    text = plan_text().replace("| PY-3 | title PY-3 | c | ACCEPTED |", "| PY-3 | title PY-3 | c | IN_REVIEW |")
    d = plan_ledger.parse(text)
    assert {s["id"]: s["state"] for s in d["sprints"]}["S2"] == "open"


def test_t8_plan_ledger_load_reads_file_and_defaults_project_to_folder_name():
    root = hv2.fresh("pl_load")
    f = root / "PROJECT_PLAN.md"
    f.write_text("\ufeff" + plan_text().replace("project: DemoProj\n", ""), encoding="utf-8", newline="\n")  # BOM, no project key
    d = plan_ledger.load(f)
    assert d["project"] == root.name, d["project"]
    assert len([t for s in d["sprints"] for t in s["tasks"]]) == TOTAL
    f.write_text(plan_text("Named"), encoding="utf-8", newline="\n")
    assert plan_ledger.load(f)["project"] == "Named"
    # read-only: loading must not create a ledger or change the plan
    assert not (root / ".harness").exists()
    assert f.read_text(encoding="utf-8") == plan_text("Named")


# ====================================================================================================== 2. report.py on a plan
def _plan_repo(name, project="DemoProj"):
    r = hv2.mk_repo(name)
    (r / "PROJECT_PLAN.md").write_text(plan_text(project), encoding="utf-8", newline="\n")
    return r


def _report_file(r, project="DemoProj"):
    return r / "reports" / f"{project}_PROGRAM_STATUS_LIVE.ru.html"


def _check_plan_report(r):
    res = hv2.tool(r, "report.py")
    assert res.rc == 0, res.all
    f = _report_file(r)
    assert f.is_file(), (res.all, list((r / "reports").glob("*")) if (r / "reports").exists() else "no reports dir")
    html = f.read_text(encoding="utf-8")
    assert "<!-- harness-report v1 -->" in html[:400]
    assert f"принято {ACCEPTED} из {TOTAL} задач" in html, "accepted/total of the fixture"
    assert f"принято {ACCEPTED}/{TOTAL} задач" in html
    assert "PROJECT_PLAN.md" in html, "the plan must be named as the source"
    assert "PX-4" in html or "PZ-2" in html, "plan tasks are rendered"
    assert "заменено" in html, "superseded tasks are shown as their own category"
    return html


def test_t8_report_from_plan_without_ledger_file():
    r = _plan_repo("rp_plan_noledger")
    assert not (r / ".harness" / "ledger.json").exists()
    _check_plan_report(r)
    assert not (r / ".harness" / "ledger.json").exists(), "report.py must not create a ledger"


def test_t8_report_from_plan_with_empty_ledger():
    r = _plan_repo("rp_plan_emptyledger")
    (r / ".harness").mkdir()
    (r / ".harness" / "ledger.json").write_text(json.dumps({"version": 1, "project": "x", "sprints": [], "approval": None, "events": []}),
                                                encoding="utf-8", newline="\n")
    _check_plan_report(r)


def test_t8_report_with_real_ledger_ignores_the_plan():
    r = _plan_repo("rp_plan_vs_ledger")
    hv2.setup_tasks(r, 2)  # real ledger: S1-T1, S1-T2
    res = hv2.tool(r, "report.py")
    assert res.rc == 0, res.all
    html = (r / "reports" / f"{r.name}_PROGRAM_STATUS_LIVE.ru.html")
    if not html.is_file():
        found = list((r / "reports").glob("*.html")) if (r / "reports").exists() else []
        html = found[0] if found else (r / ".harness" / "report.html")
    text = html.read_text(encoding="utf-8")
    assert "S1-T1" in text and "S1-T2" in text, "ledger tasks must be shown"
    # S10-T2 contract (supersedes "ledger ignores the plan"): whole program = plan tasks + ledger tasks, ledger sprint collides -> -ledger
    assert f"принято {ACCEPTED} из {TOTAL + 2} задач" in text, "denominator = plan tasks + 2 ledger tasks"
    assert "S1-ledger" in text


def test_t8_report_never_overwrites_a_hand_made_file():
    r = _plan_repo("rp_plan_handmade")
    _check_plan_report(r)
    f = _report_file(r)
    mine = "<html><body>hand made, no marker</body></html>"
    f.write_text(mine, encoding="utf-8", newline="\n")
    res = hv2.tool(r, "report.py")
    assert res.rc == 1 and "refusing to overwrite" in res.err, res.all
    assert f.read_text(encoding="utf-8") == mine
    # a file that carries the marker is ours and is refreshed
    f.write_text("<!doctype html>\n<!-- harness-report v1 -->\n<p>old</p>", encoding="utf-8", newline="\n")
    assert hv2.tool(r, "report.py").rc == 0
    assert f"принято {ACCEPTED} из {TOTAL} задач" in f.read_text(encoding="utf-8")


# ====================================================================================================== 3. build.install_project
def _install(target):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = build.install_project(target)
    return rc, buf.getvalue()


def test_t8_install_project_with_plan_copies_plan_ledger_and_creates_no_ledger():
    r = _plan_repo("ins_plan")
    rc, out = _install(r)
    assert rc == 0, out
    copied = r / ".harness-tools" / "plan_ledger.py"
    assert copied.is_file() and copied.read_bytes() == (hv2.TOOLS / "plan_ledger.py").read_bytes()
    assert (r / ".harness-tools" / "report.py").is_file()
    assert not (r / ".harness" / "ledger.json").exists(), "a plan-based project must not get an empty ledger"
    rc, out = _install(r)  # repeat is harmless and still creates none
    assert rc == 0, out
    assert not (r / ".harness" / "ledger.json").exists()
    # the installed copy really works: report from the plan using the INSTALLED tools
    res = hv2.run([PY, "-I", str(r / ".harness-tools" / "report.py")], r)
    assert res.rc == 0, res.all
    assert f"принято {ACCEPTED} из {TOTAL} задач" in _report_file(r).read_text(encoding="utf-8")


def test_t8_install_project_without_plan_creates_the_ledger_as_before():
    r = hv2.mk_repo("ins_noplan")
    rc, out = _install(r)
    assert rc == 0, out
    f = r / ".harness" / "ledger.json"
    assert f.is_file(), out
    d = json.loads(f.read_text(encoding="utf-8"))
    assert d == {"version": 1, "project": r.name, "sprints": [], "approval": None, "events": []}, d
    assert (r / ".harness-tools" / "plan_ledger.py").is_file()
    # an existing ledger is kept as it is
    d["sprints"] = [{"id": "S1"}]
    f.write_text(json.dumps(d), encoding="utf-8", newline="\n")
    assert _install(r)[0] == 0
    assert json.loads(f.read_text(encoding="utf-8"))["sprints"] == [{"id": "S1"}]


# ====================================================================================================== 4. hook_config
def _entries(cfg):
    """[(event, matcher, entry)]"""
    out = []
    for ev, groups in cfg.items():
        for g in groups:
            for e in g["hooks"]:
                out.append((ev, g.get("matcher", ""), e))
    return out


def _tokens(client, entry):
    if client == "claude":
        return [entry["command"], *entry["args"]]
    assert entry["command"] == entry["commandWindows"]
    return shlex.split(entry["command"])


def test_hook_config_wires_every_registry_script_through_run_hook_for_both_clients():
    reg = json.loads((hv2.SRC / "harness" / "hooks" / "registry.json").read_text(encoding="utf-8"))["hooks"]
    hdir = Path("C:/Hook Dir/hooks")  # a space: quoting must survive
    py = "C:/Python 314/python.exe"
    for client in ("claude", "codex"):
        cfg = build.hook_config(client, hdir, py)
        ents = _entries(cfg)
        want = [h for h in reg if client in h]
        assert len(ents) == len(want) > 0, (client, len(ents), len(want))
        by_event = collections.OrderedDict()
        for h in want:
            by_event.setdefault(h["event"], []).append(h)
        flat = [(ev, h) for ev, hs in by_event.items() for h in hs]  # config groups by event, registry order inside an event
        assert [ev for ev, _, _ in ents] == [ev for ev, _ in flat], client
        for (ev, matcher, e), (_, h) in zip(ents, flat):
            assert ev == h["event"] and matcher == h[client], (client, h["script"], ev, matcher)
            t = _tokens(client, e)
            assert t[0].replace("\\", "/") == py, t
            assert t[1:4] == ["-s", "-X", "utf8"], t
            assert Path(t[4]).as_posix() == "C:/Hook Dir/hooks/run_hook.py", t
            assert t[5] == h["script"], t  # the script NAME follows run_hook.py
            assert t[6] == f"--harness-client={client}" and len(t) == 7, t
            assert e["timeout"] == h["timeout"] and e["statusMessage"] == h["status"]
        assert {t[5] for t in (_tokens(client, e) for _, _, e in ents)} == {h["script"] for h in want}
        stops = [e for ev, _, e in ents if ev == "SubagentStop" and _tokens(client, e)[5] == "agent_visibility.py"]
        assert len(stops) == 1, f"{client}: SubagentStop agent_visibility entry"


def test_hook_config_agent_visibility_registered_for_both_clients_in_registry():
    reg = json.loads((hv2.SRC / "harness" / "hooks" / "registry.json").read_text(encoding="utf-8"))["hooks"]
    av = [h for h in reg if h["script"] == "agent_visibility.py" and h["event"] == "SubagentStop"]
    assert len(av) == 1 and "claude" in av[0] and "codex" in av[0], av


# ====================================================================================================== hook subprocess helper
class Sandbox:
    def __init__(self, name):
        self.root = hv2.fresh(name)
        self.state = self.root / "state"
        self.metrics = self.root / "metrics"
        self.home = self.root / "home"
        self.home.mkdir()
        self.cfg = self.root / "no_config.json"

    def env(self, extra=None):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("HARNESS_") and not k.startswith("SESSION_CLOSE") and k not in ("CLAUDE_PLAN_FIRST_GATE",)}
        env.update({"HARNESS_HOOK_STATE": str(self.state), "HARNESS_METRICS_DIR": str(self.metrics), "HARNESS_HOOK_CONFIG": str(self.cfg),
                    "USERPROFILE": str(self.home), "HOME": str(self.home), "PYTHONIOENCODING": "utf-8",
                    "SESSION_CLOSE_GATE_MODE": "live", "SESSION_CLOSE_PLAN_TOOL": str(self.root / "no_tool.py"),
                    "CLAUDE_HOOK_STATE_DIR": str(self.root / "dstate")})
        env.update(extra or {})
        return env

    def hook(self, script, payload, client="claude", extra_env=None, cwd=None, direct=True):
        argv = [PY, "-s", "-X", "utf8"]
        argv += [str(HOOKS / script)] if direct else [str(HOOKS / "run_hook.py"), script]
        argv += [f"--harness-client={client}"]
        cp = subprocess.run(argv, input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8", errors="replace",
                            env=self.env(extra_env), cwd=str(cwd or self.root), timeout=90)
        return cp.returncode, cp.stdout.strip(), cp.stderr


def pre_denied(out):
    try:
        return json.loads(out).get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
    except ValueError:
        return False


def stop_blocked(out):
    try:
        return json.loads(out).get("decision") == "block"
    except ValueError:
        return False


# ---------- Claude transcript builders ----------
def t_human(text, ts="2026-11-12T10:00:00Z"):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}}


def t_asst(*blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def t_text(t):
    return {"type": "text", "text": t}


def t_use(tid, name, inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def t_res(tid, content="ok", is_error=False):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": content, "is_error": is_error}]}}


def write_tp(dirpath, rows, name="t.jsonl"):
    p = Path(dirpath) / name
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8", newline="\n")
    return str(p)


# ====================================================================================================== 5. plan_first_gate TaskCreate
EDIT = {"file_path": r"D:\Repo\proj\src\app.py", "old_string": "a", "new_string": "b"}
SUBJ_EDIT, SUBJ_TEST = "Edit the parser module in src/app.py", "Run the unit tests for the parser"


def _taskcreate_rows(subjects, errors=()):
    rows = [t_human("Fix the parser please")]
    for i, s in enumerate(subjects):
        tid = f"tc{i}"
        rows.append(t_asst(t_use(tid, "TaskCreate", {"subject": s, "description": "d " + s})))
        rows.append(t_res(tid, "Task #%d created successfully: %s" % (i + 1, s), is_error=(i in errors)))
    return rows


_PF_N = [0]


def _pf(sb, rows, event, sid=None):
    _PF_N[0] += 1
    sid = sid or f"s{_PF_N[0]}"  # a fresh session per call: the deny budget (3 per turn) must not leak between cases
    tp = write_tp(sb.root, rows)
    if event == "Stop":
        payload = {"hook_event_name": "Stop", "session_id": sid, "transcript_path": tp}
    else:
        payload = {"hook_event_name": "PreToolUse", "session_id": sid, "transcript_path": tp, "tool_name": "Edit", "tool_input": EDIT}
    return sb.hook("plan_first_gate.py", payload)


def test_plan_first_several_task_create_calls_count_as_a_presented_plan():
    sb = Sandbox("pf_tc_ok")
    rows = _taskcreate_rows([SUBJ_EDIT, SUBJ_TEST])
    rc, out, err = _pf(sb, rows, "PreToolUse")
    assert rc == 0 and out == "", (rc, out, err)  # change allowed
    rows3 = _taskcreate_rows(["Read the failing parser tests", SUBJ_EDIT, SUBJ_TEST])
    rc, out, _ = _pf(sb, rows3, "PreToolUse")
    assert rc == 0 and out == "", out
    # Stop: the turn changed something (Edit call in the transcript) and the plan is presented -> no block
    rows += [t_asst(t_use("e1", "Edit", EDIT)), t_res("e1", "edited")]
    rc, out, err = _pf(sb, rows, "Stop")
    assert rc == 0 and out == "", (rc, out, err)


def test_plan_first_single_task_create_is_not_a_plan():
    sb = Sandbox("pf_tc_one")
    rows = _taskcreate_rows([SUBJ_TEST])
    rc, out, _ = _pf(sb, rows, "PreToolUse")
    assert rc == 0 and pre_denied(out), out
    rows += [t_asst(t_use("e1", "Edit", EDIT)), t_res("e1", "edited")]
    rc, out, _ = _pf(sb, rows, "Stop")
    assert stop_blocked(out), out


def test_plan_first_task_create_failures_and_weak_steps_do_not_count():
    sb = Sandbox("pf_tc_bad")
    # one of two calls failed -> one valid step only
    rc, out, _ = _pf(sb, _taskcreate_rows([SUBJ_EDIT, SUBJ_TEST], errors=(1,)), "PreToolUse")
    assert pre_denied(out), ("failed TaskCreate counted", out)
    # two steps but no verification step
    rc, out, _ = _pf(sb, _taskcreate_rows([SUBJ_EDIT, "Rename the helper functions there"]), "PreToolUse")
    assert pre_denied(out), ("no verification step", out)
    # duplicates
    rc, out, _ = _pf(sb, _taskcreate_rows([SUBJ_TEST, SUBJ_TEST.upper()]), "PreToolUse")
    assert pre_denied(out), ("duplicate steps", out)
    # too short steps
    rc, out, _ = _pf(sb, _taskcreate_rows(["Edit it", "Run tests"]), "PreToolUse")
    assert pre_denied(out), ("short steps", out)
    # no plan at all
    rc, out, _ = _pf(sb, [t_human("Fix the parser please")], "PreToolUse")
    assert pre_denied(out), out


# ====================================================================================================== 6. secret_guard terraform output
def _sg(sb, cmd, tool="Bash"):
    tp = write_tp(sb.root, [t_human("look at the infra")])
    payload = {"hook_event_name": "PreToolUse", "session_id": "s1", "transcript_path": tp, "tool_name": tool,
               "tool_input": {"command": cmd}}
    return sb.hook("secret_guard.py", payload)


def test_secret_guard_terraform_output_without_a_name_is_denied():
    sb = Sandbox("sg_tf")
    for cmd in ("terraform output", "terraform output -json", "terraform output -no-color", "terraform   output",
                "cd infra && terraform output -json", "echo start; terraform output"):
        rc, out, err = _sg(sb, cmd)
        assert rc == 0 and pre_denied(out), (cmd, out, err)
        assert "terraform output" in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"], out
    rc, out, _ = _sg(sb, "terraform output -json", tool="PowerShell")
    assert pre_denied(out), out


def test_secret_guard_terraform_output_with_a_name_or_a_mask_is_allowed():
    sb = Sandbox("sg_tf_ok")
    for cmd in ("terraform output db_host", "terraform output -json db_host", "terraform output -raw db_host",
                "terraform output | cut -d= -f1", "terraform output -json | wc -l", "terraform output | grep -c .",
                "terraform plan", "terraform output-not-a-command-x db"):
        rc, out, err = _sg(sb, cmd)
        assert rc == 0 and out == "", (cmd, out, err)


# ====================================================================================================== 7. agent_visibility.touch
def _av(sb):
    """Import agent_visibility with HARNESS_HOOK_STATE pointing at this sandbox (restored afterwards)."""
    old = os.environ.get("HARNESS_HOOK_STATE")
    os.environ["HARNESS_HOOK_STATE"] = str(sb.state)
    sys.path.insert(0, str(HOOKS))
    try:
        for m in ("agent_visibility", "_agentio"):
            sys.modules.pop(m, None)
        mod = importlib.import_module("agent_visibility")
    finally:
        sys.path.remove(str(HOOKS))
        if old is None:
            os.environ.pop("HARNESS_HOOK_STATE", None)
        else:
            os.environ["HARNESS_HOOK_STATE"] = old
    return mod


def _active_file(sb):
    return sb.state / "agents" / "active.json"


def _seed(sb, agents):
    f = _active_file(sb)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"agents": agents}), encoding="utf-8", newline="\n")


def _agents(sb):
    return json.loads(_active_file(sb).read_text(encoding="utf-8"))["agents"]


def _entry(typ, start_ago=100, **kw):
    e = {"type": typ, "model": "sonnet", "effort": "medium", "task": "active", "start": time.time() - start_ago, "session": "s1"}
    e.update(kw)
    return e


def test_touch_marks_an_unfinished_agent_active_confirmed():
    sb = Sandbox("av_touch")
    av = _av(sb)
    _seed(sb, [_entry("code-reviewer")])
    assert av.state_of(_agents(sb)[0]) == "START_OBSERVED"
    t0 = time.time()
    with _env(sb):
        av.touch({"agent_type": "code-reviewer", "hook_event_name": "PreToolUse"})
    e = _agents(sb)[0]
    assert e["activity"] >= t0 and "stop" not in e, e
    assert av.state_of(e) == "ACTIVE_CONFIRMED"


@contextlib.contextmanager
def _env(sb):
    old = os.environ.get("HARNESS_HOOK_STATE")
    os.environ["HARNESS_HOOK_STATE"] = str(sb.state)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("HARNESS_HOOK_STATE", None)
        else:
            os.environ["HARNESS_HOOK_STATE"] = old


def test_touch_throttle_ignored_payloads_and_finished_entries():
    sb = Sandbox("av_throttle")
    av = _av(sb)
    _seed(sb, [_entry("code-reviewer")])
    with _env(sb):
        av.touch({"agent_type": "code-reviewer"})
        first = _active_file(sb).read_bytes()
        a1 = _agents(sb)[0]["activity"]
        time.sleep(0.05)
        av.touch({"agent_type": "code-reviewer"})  # within 20 s: nothing changes
        assert _active_file(sb).read_bytes() == first and _agents(sb)[0]["activity"] == a1
        # older than the throttle window -> updated again
        ents = _agents(sb)
        ents[0]["activity"] = time.time() - 30
        _seed(sb, ents)
        old_a = _agents(sb)[0]["activity"]
        av.touch({"agent_type": "code-reviewer"})
        assert _agents(sb)[0]["activity"] > old_a + 20
        # no agent_type / blank / other type / non-dict-ish: nothing changes
        before = _active_file(sb).read_bytes()
        for p in ({}, {"agent_type": ""}, {"agent_type": "   "}, {"agent_type": "someone-else"}, {"agent_type": None}):
            av.touch(p)
        assert _active_file(sb).read_bytes() == before
        # a finished entry is never touched
        _seed(sb, [_entry("a", stop=time.time() - 5)])
        before = _active_file(sb).read_bytes()
        av.touch({"agent_type": "a"})
        assert _active_file(sb).read_bytes() == before
        assert av.state_of(_agents(sb)[0]) == "FINISHED_CONFIRMED"
        # oldest unfinished of the type gets the mark, not a finished or a newer one
        _seed(sb, [_entry("b", 300, stop=time.time() - 200), _entry("b", 200), _entry("b", 100)])
        av.touch({"agent_type": "b"})
        es = _agents(sb)
        assert "activity" not in es[0] and "activity" in es[1] and "activity" not in es[2], es


def test_touch_recovers_a_status_unknown_agent_and_a_missing_state_file_is_harmless():
    sb = Sandbox("av_unknown")
    av = _av(sb)
    _seed(sb, [_entry("code-reviewer", start_ago=av.UNKNOWN_AFTER_S + 100)])
    assert av.state_of(_agents(sb)[0]) == "STATUS_UNKNOWN"
    with _env(sb):
        av.touch({"agent_type": "code-reviewer"})
    assert av.state_of(_agents(sb)[0]) == "ACTIVE_CONFIRMED"
    sb2 = Sandbox("av_nofile")
    av2 = _av(sb2)
    with _env(sb2):
        av2.touch({"agent_type": "code-reviewer"})  # must not raise nor invent entries
    assert not _active_file(sb2).exists() or _agents(sb2) == []


def _run_hook_py(sb, script, payload, client="claude"):
    return sb.hook(script, payload, client=client, direct=False)


def test_run_hook_calls_touch_for_payloads_carrying_agent_type():
    sb = Sandbox("av_runhook")
    _seed(sb, [_entry("code-reviewer")])
    base = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "git status"}, "session_id": "s1",
            "cwd": str(sb.root)}
    # no agent_type: the main agent's own call proves nothing about the subagent
    rc, out, err = _run_hook_py(sb, "shell_policy_gate.py", base)
    assert rc == 0 and out == "", (rc, out, err)
    assert "activity" not in _agents(sb)[0]
    # with agent_type: the entry becomes ACTIVE_CONFIRMED
    t0 = time.time()
    rc, out, err = _run_hook_py(sb, "shell_policy_gate.py", {**base, "agent_type": "code-reviewer"})
    assert rc == 0 and out == "", (rc, out, err)
    es = _agents(sb)
    assert es[0]["activity"] >= t0, es
    sys.path.insert(0, str(HOOKS))
    try:
        sys.modules.pop("agent_visibility", None)
        av = importlib.import_module("agent_visibility")
    finally:
        sys.path.remove(str(HOOKS))
    assert av.state_of(es[0]) == "ACTIVE_CONFIRMED"
    # throttle also holds through the runner
    a1 = es[0]["activity"]
    _run_hook_py(sb, "shell_policy_gate.py", {**base, "agent_type": "code-reviewer"})
    assert _agents(sb)[0]["activity"] == a1
    # a different agent type does not mark this entry
    _seed(sb, [_entry("code-reviewer")])
    _run_hook_py(sb, "shell_policy_gate.py", {**base, "agent_type": "other-agent"})
    assert "activity" not in _agents(sb)[0]


def test_run_hook_does_not_touch_for_the_agent_visibility_stop_itself():
    sb = Sandbox("av_runhook_stop")
    _seed(sb, [_entry("code-reviewer")])
    payload = {"hook_event_name": "SubagentStop", "agent_type": "code-reviewer", "session_id": "s1", "cwd": str(sb.root)}
    rc, out, err = _run_hook_py(sb, "agent_visibility.py", payload)
    assert rc == 0, err
    e = _agents(sb)[0]
    assert e.get("stop") and "activity" not in e, e  # finished, not 'touched' as active
    assert json.loads(out)["systemMessage"].startswith("[agent STOP] code-reviewer"), out


# ====================================================================================================== 8. session_close_gate PR-owed message
STUB_TOOL = '''"""Minimal stand-in for project_plan.py (the real tool lives outside this repo): same load/check API."""
import re
from pathlib import Path


def load(project):
    text = (Path(project) / "PROJECT_PLAN.md").read_text(encoding="utf-8-sig")
    meta = {}
    m = re.match(r"---\\n(.*?)\\n---", text, re.S)
    for ln in (m.group(1).splitlines() if m else []):
        if ":" in ln:
            k, v = ln.split(":", 1)
            meta[k.strip()] = v.strip()
    return {"meta": meta, "roadmap": [], "sprints": {}, "errors": []}


def check(project):
    return 0, "no open tasks"
'''


def _close_project(sb, name):
    proj = hv2.mk_repo(name)
    (proj / "PROJECT_PLAN.md").write_text("---\nproject: P\nactive_sprint: S1\noperator_approved: true\n---\n# P\n", encoding="utf-8", newline="\n")
    tool = sb.root / "project_plan_stub.py"
    tool.write_text(STUB_TOOL, encoding="utf-8", newline="\n")
    return proj, {"SESSION_CLOSE_PLAN_TOOL": str(tool)}


def _pr_event(sb, proj, env, ok=True):
    resp = {"stdout": "https://github.com/o/r/pull/7", "stderr": "", "interrupted": False} if ok else \
        {"stdout": "", "stderr": "failed", "interrupted": False, "is_error": True}
    payload = {"hook_event_name": "PostToolUse", "session_id": "s1", "cwd": str(proj), "tool_name": "Bash",
               "tool_input": {"command": "gh pr create --title t --body b"}, "tool_response": resp}
    return sb.hook("session_close_gate.py", payload, extra_env=env)


def _close_stop(sb, proj, env, msg="Everything is done."):
    payload = {"hook_event_name": "Stop", "session_id": "s1", "cwd": str(proj), "last_assistant_message": msg}
    return sb.hook("session_close_gate.py", payload, extra_env=env)


def test_session_close_pr_owed_message_points_to_report_py_not_project_plan_render():
    sb = Sandbox("sc_pr")
    proj, env = _close_project(sb, "sc_pr_proj")
    rc, out, err = _pr_event(sb, proj, env)
    assert rc == 0, err
    rc, out, err = _close_stop(sb, proj, env)
    assert rc == 0 and stop_blocked(out), (out, err)
    reason = json.loads(out)["reason"]
    assert "PROGRAM REPORT OWED" in reason
    assert "report.py" in reason, reason
    assert "_PROGRAM_STATUS_LIVE.ru.html" in reason, reason
    # project_plan.py render may be named only as the thing NOT to use
    for m in re.finditer(r"project_plan\.py render", reason):
        assert reason[:m.start()].rstrip().endswith("Do not use"), ("instructs to run project_plan.py render", reason)
    # blocks once per PR: the same Stop again is not blocked by the report debt
    rc, out2, _ = _close_stop(sb, proj, env)
    assert "PROGRAM REPORT OWED" not in out2, out2


def test_session_close_pr_owed_cleared_by_a_fresh_program_report_and_not_set_by_a_failed_pr():
    sb = Sandbox("sc_pr2")
    proj, env = _close_project(sb, "sc_pr2_proj")
    _pr_event(sb, proj, env)
    rep = proj / "reports" / "P_PROGRAM_STATUS_LIVE.ru.html"
    rep.parent.mkdir()
    rep.write_text("<!-- harness-report v1 -->", encoding="utf-8")
    os.utime(rep, (time.time() + 5, time.time() + 5))
    rc, out, err = _close_stop(sb, proj, env)
    assert "PROGRAM REPORT OWED" not in out, out
    sb2 = Sandbox("sc_pr3")
    proj2, env2 = _close_project(sb2, "sc_pr3_proj")
    _pr_event(sb2, proj2, env2, ok=False)  # failed `gh pr create` records nothing
    rc, out, err = _close_stop(sb2, proj2, env2)
    assert "PROGRAM REPORT OWED" not in out, out


# ====================================================================================================== 9. integration replay
ORDER = ["secret_guard.py", "plan_first_gate.py", "ask_last_gate.py", "session_close_gate.py"]  # secret -> plan/ask -> lifecycle
PRE_HOOKS = ["secret_guard.py", "plan_first_gate.py", "ask_last_gate.py"]
STOP_HOOKS = ["plan_first_gate.py", "ask_last_gate.py", "session_close_gate.py"]
FINAL_ASK = "I checked the environment, the config files and gcloud auth: nothing is set. Please provide the gcloud access key?"
FAKE_EDIT_DIR = "D:/Repo/demo_project/"  # not under tmp/: plan_first_gate exempts scratch paths
FINAL_DONE = "Done: the config is written and the tests pass."


def write_ledger(proj, tasks):
    f = proj / ".harness" / "ledger.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    tl = [{"id": tid, "title": "t", "criteria": [], "run": "", "risk": "local", "journey": False, "state": st, "commits": [],
           "verify": None, "review": None, "routing": None, "tokens": None} for tid, st in tasks]
    f.write_text(json.dumps({"version": 1, "project": proj.name, "approval": None, "events": [], "sprints": [
        {"id": "S9", "title": "s", "branch": "b", "state": "open", "opened": "2026-11-12T00:00:00+02:00", "tasks": tl}]}),
        encoding="utf-8", newline="\n")


class Replay:
    """One scripted turn. Steps: ('text', t) or ('call', name, input, result). Pre-hooks run for every call with the transcript as it
    is BEFORE that call; the call is then appended (a denied call is recorded as an error result, like the real client does)."""

    def __init__(self, name, open_tasks=False, human="Set up the deploy config for the demo project"):
        self.sb = Sandbox(name)
        self.proj = hv2.fresh(name + "_proj")
        write_ledger(self.proj, [("S9-T1", "todo" if open_tasks else "done")])
        self.rows = [t_human(human)]
        self.n = 0
        self.log = []   # per call: (label, set of hooks that denied)
        self.crashes = []

    def run(self, script, payload):
        rc, out, err = self.sb.hook(script, payload, direct=False)
        if rc != 0 or "Traceback" in err:
            self.crashes.append((script, rc, err[-300:]))
        return out

    def tp(self):
        return write_tp(self.sb.root, self.rows)

    def call(self, name, inp, result="ok"):
        denied = set()
        outs = {}
        pl = {"hook_event_name": "PreToolUse", "session_id": "s1", "transcript_path": self.tp(), "cwd": str(self.proj),
              "tool_name": name, "tool_input": inp}
        for s in PRE_HOOKS:
            outs[s] = self.run(s, pl)
            if pre_denied(outs[s]):
                denied.add(s)
        self.log.append((f"{name}:{json.dumps(inp)[:60]}", denied))
        self.n += 1
        tid = f"tu{self.n}"
        self.rows.append(t_asst(t_use(tid, name, inp)))
        self.rows.append(t_res(tid, "denied by hook" if denied else result, is_error=bool(denied)))
        return denied

    def say(self, text):
        self.rows.append(t_asst(t_text(text)))

    def stop(self, msg, passes=1):
        """Run the Stop hooks `passes` times in precedence order; return the list (per pass) of the hooks that blocked."""
        res = []
        self.rows.append(t_asst(t_text(msg)))
        for _ in range(passes):
            blocked = []
            for s in STOP_HOOKS:
                out = self.run(s, {"hook_event_name": "Stop", "session_id": "s1", "transcript_path": self.tp(), "cwd": str(self.proj),
                                   "last_assistant_message": msg})
                if stop_blocked(out):
                    blocked.append(s)
            res.append(blocked)
        return res

    def internal_errors(self):
        f = self.sb.state / "evidence" / "secret_guard.jsonl"
        return [ln for ln in f.read_text(encoding="utf-8").splitlines() if "internal-error" in ln] if f.is_file() else []


def present_plan(rp):
    rp.call("TaskCreate", {"subject": SUBJ_EDIT, "description": "d"}, "Task #1 created successfully")
    rp.call("TaskCreate", {"subject": SUBJ_TEST, "description": "d"}, "Task #2 created successfully")


def investigate(rp):
    rp.call("Bash", {"command": "printenv | cut -d= -f1 | grep -i gcloud"}, "no matches")
    rp.call("Bash", {"command": "grep -rl gcloud config/"}, "no matches")
    rp.call("Bash", {"command": "gcloud auth list"}, "No credentialed accounts.")


def work(rp):
    rp.call("Edit", {"file_path": FAKE_EDIT_DIR + "deploy.cfg", "old_string": "a", "new_string": "b"}, "edited")
    rp.call("Bash", {"command": "python -I tests/test_deploy.py"}, "OK: 0 failing")


def test_replay_good_turn_passes_all_four_hooks():
    rp = Replay("rp_good")
    present_plan(rp)
    investigate(rp)
    work(rp)
    assert all(not d for _, d in rp.log), [x for x in rp.log if x[1]]
    got = rp.stop(FINAL_ASK)
    assert got == [[]], ("plan + investigation + work: no Stop hook may block", got, rp.rows[-1])
    assert not rp.crashes and not rp.internal_errors(), (rp.crashes, rp.internal_errors())


def test_replay_good_turn_with_plain_final_message_passes():
    rp = Replay("rp_good2")
    present_plan(rp)
    work(rp)
    assert all(not d for _, d in rp.log)
    assert rp.stop(FINAL_DONE) == [[]]
    assert not rp.crashes


def test_replay_secret_read_is_stopped_by_secret_guard_only():
    rp = Replay("rp_secret")
    present_plan(rp)
    d = rp.call("Bash", {"command": "cat deploy/.env"}, "")
    assert d == {"secret_guard.py"}, d
    d = rp.call("Read", {"file_path": str(rp.proj / ".env")}, "")
    assert d == {"secret_guard.py"}, d
    assert not rp.crashes and not rp.internal_errors()


def test_replay_change_without_a_plan_is_stopped_by_plan_first_gate_only():
    rp = Replay("rp_noplan")
    d = rp.call("Edit", {"file_path": FAKE_EDIT_DIR + "deploy.cfg", "old_string": "a", "new_string": "b"}, "edited")
    assert d == {"plan_first_gate.py"}, d
    # the same turn at Stop: only plan_first_gate blocks (precedence: it comes before the lifecycle gate; ledger is all done)
    assert rp.stop(FINAL_DONE) == [["plan_first_gate.py"]]
    assert not rp.crashes


def test_replay_ask_without_investigation_is_stopped_by_ask_last_gate_only():
    rp = Replay("rp_ask")
    present_plan(rp)
    work(rp)
    assert rp.stop(FINAL_ASK) == [["ask_last_gate.py"]]
    # the same ask through the question tool is denied by the same gate, and by nobody else
    rp2 = Replay("rp_ask2")
    present_plan(rp2)
    d = rp2.call("AskUserQuestion", {"questions": [{"question": "Please provide the gcloud access key", "options": [
        {"label": "Provide the key", "description": "I paste it"}, {"label": "Skip", "description": "no"}]}]}, "")
    assert d == {"ask_last_gate.py"}, d
    assert not rp.crashes and not rp2.crashes


def test_replay_open_ledger_task_is_stopped_by_session_close_gate_only():
    rp = Replay("rp_open", open_tasks=True)
    present_plan(rp)
    work(rp)
    assert rp.stop(FINAL_DONE) == [["session_close_gate.py"]]
    assert not rp.crashes


def test_replay_precedence_plan_first_then_lifecycle_and_no_deadlock():
    """No plan AND an open ledger task: pass 1 -> plan gate first; pass 2 (plan gate already spoke once) -> the lifecycle gate."""
    rp = Replay("rp_both", open_tasks=True)
    rp.call("Edit", {"file_path": FAKE_EDIT_DIR + "deploy.cfg", "old_string": "a", "new_string": "b"}, "edited")
    passes = rp.stop(FINAL_DONE, passes=2)
    assert passes[0][0] == "plan_first_gate.py", passes
    assert "plan_first_gate.py" not in passes[1] and passes[1] == ["session_close_gate.py"], passes
    assert not rp.crashes


def test_replay_hooks_do_not_deny_twice_for_the_same_call():
    """Every call of a normal scripted turn is denied by at most one hook, and one hook denies a repeated identical call identically."""
    rp = Replay("rp_once")
    calls = [("Bash", {"command": "git status"}), ("Read", {"file_path": str(rp.proj / "README.md")}),
             ("Bash", {"command": "printenv | cut -d= -f1"}), ("Edit", {"file_path": FAKE_EDIT_DIR + "a.py", "old_string": "a", "new_string": "b"})]
    for name, inp in calls:
        rp.call(name, inp)
    for label, denied in rp.log:
        assert len(denied) <= 1, (label, denied)
    # secret read repeated: still exactly the same single hook each time (no escalation to a second hook)
    present_plan(rp)
    for _ in range(3):
        assert rp.call("Bash", {"command": "cat .env"}) == {"secret_guard.py"}
    assert not rp.crashes


def test_replay_overlap_secret_echo_into_file_without_plan_is_observed():
    """INFORMATIONAL: `echo $API_TOKEN > out.txt` with no presented plan is a secret AND a mutation. Both hooks may deny the one call.
    The test only requires that secret_guard is among the deniers (precedence) and that nothing crashes; the overlap is printed."""
    rp = Replay("rp_overlap")
    d = rp.call("Bash", {"command": "echo $API_TOKEN > out.txt"}, "")
    assert "secret_guard.py" in d, d
    if len(d) > 1:
        print("NOTE overlap: one call denied by", sorted(d))
    assert not rp.crashes


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
