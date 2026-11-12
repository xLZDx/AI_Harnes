"""S13 review-round tests (commit 25b17be): ledger_sync evidence/shrink/backup/race, plan_import_ledger_md lock, audit_projects Claude
handlers + ledger_whole, install_claude CLAUDE.md. Temp dirs only under D:\\tmp\\harness_v2_tests; ledger_sync.BACKUP_DIR, audit HOME/AUDIT_TMP
and install --home/--snapshot always point to temp folders; no real ledger, plan or ~/.claude is touched.
Run: C:\\Python314\\python.exe -I tests/test_s13_review_tools.py"""
import contextlib
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools"))
import hv2  # noqa: E402
import ledger_sync  # noqa: E402
import plan_import_ledger_md as pim  # noqa: E402
import audit_projects as au  # noqa: E402

SRC = HERE.parent
ROAD = "---\nproject: demo\n---\n# Plan\n\n## Roadmap\n| Sprint | Title | Status | Goal |\n|---|---|---|---|\n"


def _w(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _task(tid, state="todo", **extra):
    t = {"id": tid, "title": "t " + tid, "criteria": [], "run": "", "risk": "local", "journey": False, "state": state,
         "commits": [], "verify": None, "review": None, "routing": None, "tokens": None}
    t.update(extra)
    return t


def _copy_task(tid, state):
    return _task(tid, state, verify={"note": "canonical plan row imported as evidence text"})


def _ledger(sprints):
    return {"version": 1, "project": "demo", "approval": None, "events": [], "sprints": sprints}


def _sprint(sid, tasks):
    return {"id": sid, "title": sid, "branch": "", "state": "open", "opened": None, "closed": None, "tasks": tasks}


def _project(name, plan, led):
    repo = hv2.mk_repo(name)
    _w(repo / "PROJECT_PLAN.md", plan)
    _w(repo / ".harness" / "ledger.json", json.dumps(led, indent=2))
    return repo


def _sync(repo, bk=None, dry=False, allow_shrink=False):
    bk = bk or hv2.fresh("backup")
    ledger_sync.BACKUP_DIR = bk  # never D:\Backup
    return ledger_sync.sync_project(repo, dry, None, allow_shrink), bk


def _lpath(repo):
    return repo / ".harness" / "ledger.json"


def _find(repo, tid):
    for s in hv2.load_ledger(repo)["sprints"]:
        for t in s["tasks"]:
            if t["id"] == tid:
                return t
    return None


PLAN5 = (ROAD + "| S00 | Zero | NOT_STARTED | expanded below |\n\n## Sprint S00 - Zero\n\n| Task | Title | Status |\n|---|---|---|\n"
         "| ST-A#1 | a | NOT_STARTED |\n| ST-A#2 | b | NOT_STARTED |\n| ST-A#3 | c | NOT_STARTED |\n"
         "| ST-A#4 | d | BLOCKED_OPERATOR |\n| ST-A#5 | e | NOT_STARTED |\n")


# ---------- ledger_sync: ledger-written evidence ----------
def _evidence_project():
    blocked = {"class": "EXTERNAL", "cause": "vendor down", "at": "2026-11-10T10:00:00"}
    tokens = {"input": 1200, "output": 340}
    routing = {"reviewers": [{"agent": "code-reviewer", "reason": "local risk"}], "chosen_at": "2026-11-10"}
    tasks = [_task("ST-A#1", "todo", blocked=blocked),
             _task("ST-A#2", "in_progress"),
             _task("ST-A#3", "todo", tokens=tokens, routing=routing),
             _task("ST-A#4", "todo", blocked={"class": "OPERATOR", "cause": "from plan", "at": None}),
             _task("ST-A#5", "todo")]
    return _project("evid", PLAN5, _ledger([_sprint("S00", tasks)])), blocked, tokens, routing


def test_evidence_blocked_inprogress_tokens_routing_survive_sync():
    repo, blocked, tokens, routing = _evidence_project()
    res, _ = _sync(repo)
    assert res["status"] == "changed", res["status"]
    assert _find(repo, "ST-A#1")["blocked"] == blocked
    assert _find(repo, "ST-A#2")["state"] == "in_progress"
    t3 = _find(repo, "ST-A#3")
    assert t3["tokens"] == tokens and t3["routing"] == routing, t3
    assert [t["id"] for s in hv2.load_ledger(repo)["sprints"] for t in s["tasks"]] == [f"ST-A#{k}" for k in range(1, 6)]


def test_evidence_second_sync_is_unchanged_and_byte_identical():
    repo, *_ = _evidence_project()
    _sync(repo)
    before = _lpath(repo).read_bytes()
    res2, bk2 = _sync(repo)
    assert res2["status"] == "unchanged", res2["status"]
    assert _lpath(repo).read_bytes() == before and not list(bk2.glob("*.json"))


def test_plan_reader_blocked_with_null_at_is_not_evidence():
    # the ledger copy says EXTERNAL/at:null on a task whose plan row is NOT_STARTED: no ledger.py `block` wrote it, so the plan wins
    plan = ROAD + "| S00 | Z | NOT_STARTED | below |\n\n## Sprint S00 - Z\n\n| Task | Title | Status |\n|---|---|---|\n| ST-A#1 | a | NOT_STARTED |\n"
    led = _ledger([_sprint("S00", [_task("ST-A#1", "todo", blocked={"class": "EXTERNAL", "cause": "stale copy", "at": None})])])
    repo = _project("nullat", plan, led)
    res, _ = _sync(repo)
    t = _find(repo, "ST-A#1")
    assert not t.get("blocked"), t
    assert not (res.get("stats") or {}).get("carried"), res.get("stats")


def test_plan_blocked_status_keeps_plan_blocked_with_null_at():
    repo, *_ = _evidence_project()
    _sync(repo)
    b = _find(repo, "ST-A#4").get("blocked")
    assert b and b.get("at") is None and "OPERATOR" in str(b.get("class")), b


# ---------- ledger_sync: shrink guard ----------
SHRINK_PLAN = (ROAD + "| S00 | Zero | NOT_STARTED | below |\n\n## Sprint S00 - Zero\n\n| Task | Title | Status |\n|---|---|---|\n"
               "| ST-A#1 | a | ACCEPTED |\n| ST-A#2 | b | ACCEPTED |\n")


def _shrink_project():
    led = _ledger([_sprint("S00", [_copy_task("ST-A#1", "done"), _copy_task("ST-A#2", "done")]),
                   _sprint("S01", [_copy_task(f"ST-B#{k}", "done") for k in (1, 2, 3)])])
    return _project("shrink", SHRINK_PLAN, led)


def test_shrink_refused_cli_exit_nonzero_bytes_unchanged_ids_printed():
    repo = _shrink_project()
    before = _lpath(repo).read_bytes()
    r = hv2.run([hv2.PY, "-I", str(hv2.TOOLS / "ledger_sync.py"), "--project", str(repo)], hv2.SRC)
    assert r.rc != 0, r.all
    assert "ERROR" in r.out and "smaller" in r.out, r.out
    for tid in ("ST-B#1", "ST-B#2", "ST-B#3"):
        assert tid in r.out, (tid, r.out)
    assert _lpath(repo).read_bytes() == before


def test_shrink_refused_in_process_no_backup_and_allow_shrink_writes():
    repo = _shrink_project()
    before = _lpath(repo).read_bytes()
    res, bk = _sync(repo)
    assert res["status"].startswith("ERROR") and "ST-B#1" in res["status"], res["status"]
    assert _lpath(repo).read_bytes() == before and not list(bk.glob("*.json"))
    res2, bk2 = _sync(repo, allow_shrink=True)
    assert res2["status"] == "changed", res2["status"]
    ids = [t["id"] for s in hv2.load_ledger(repo)["sprints"] for t in s["tasks"]]
    assert ids == ["ST-A#1", "ST-A#2"], ids
    bks = list(bk2.glob("ledger-sync-*.json"))
    assert len(bks) == 1 and bks[0].read_bytes() == before


def test_shrink_cli_allow_shrink_flag_is_accepted():
    # dry-run only (a real write would go to D:\Backup): the flag must turn the ERROR into "would change"
    repo = _shrink_project()
    before = _lpath(repo).read_bytes()
    r = hv2.run([hv2.PY, "-I", str(hv2.TOOLS / "ledger_sync.py"), "--project", str(repo), "--dry-run", "--allow-shrink"], hv2.SRC)
    assert r.rc == 0 and "would change" in r.out and "ERROR" not in r.out, r.all
    assert _lpath(repo).read_bytes() == before


# ---------- ledger_sync: backups ----------
def test_consecutive_syncs_write_distinct_readable_backups_of_the_old_ledger():
    plan = ROAD + "| S00 | Z | NOT_STARTED | below |\n\n## Sprint S00 - Z\n\n| Task | Title | Status |\n|---|---|---|\n| ST-A#1 | a | NOT_STARTED |\n"
    repo = _project("bk", plan, _ledger([]))
    bk = hv2.fresh("backup")
    olds, paths = [], []
    for k in range(2, 6):
        olds.append(_lpath(repo).read_bytes())
        res, _ = _sync(repo, bk)
        assert res["status"] == "changed", (k, res["status"])
        paths.append(res["backup"])
        plan += f"| ST-A#{k} | t{k} | NOT_STARTED |\n"
        _w(repo / "PROJECT_PLAN.md", plan)
    assert len(set(paths)) == 4, paths
    assert len(list(bk.glob("ledger-sync-*.json"))) == 4
    for p, old in zip(paths, olds):
        assert Path(p).read_bytes() == old, p
        assert isinstance(json.loads(Path(p).read_text(encoding="utf-8")), dict)


def test_backup_name_has_microseconds_and_pid():
    repo = _project("bkname", SHRINK_PLAN, _ledger([_sprint("S00", [_copy_task("ST-A#1", "done")])]))
    res, _ = _sync(repo)
    name = Path(res["backup"]).name
    import re
    assert re.fullmatch(r"ledger-sync-.+-\d{8}-\d{6}-\d{6}-\d+\.json", name), name
    assert name.endswith(f"-{os.getpid()}.json")


# ---------- ledger_sync: ledger created / changed between read and lock ----------
def test_write_path_refuses_when_ledger_appeared_after_read():
    repo = hv2.mk_repo("race1")
    lp = _lpath(repo)
    _w(lp, json.dumps({"version": 1, "project": "other", "sprints": [], "approval": None, "events": [{"kind": "x"}]}))
    before = lp.read_bytes()
    try:
        ledger_sync.write(lp, False, {}, _ledger([_sprint("S00", [_task("T1")])]), "race1")
        raise AssertionError("write() must refuse")
    except RuntimeError as e:
        assert "created while the sync ran" in str(e), e
    assert lp.read_bytes() == before


def test_sync_project_end_to_end_does_not_overwrite_ledger_created_during_sync():
    plan = ROAD + "| S00 | Z | NOT_STARTED | below |\n\n## Sprint S00 - Z\n\n| Task | Title | Status |\n|---|---|---|\n| ST-A#1 | a | NOT_STARTED |\n"
    repo = hv2.mk_repo("race2")
    _w(repo / "PROJECT_PLAN.md", plan)
    lp = _lpath(repo)
    assert not lp.exists()
    injected = json.dumps({"version": 1, "project": "demo", "approval": {"head": "h", "result": "APPROVE"}, "events": [], "sprints": []})
    real_build = ledger_sync.build

    def build_then_race(p, o):
        out = real_build(p, o)
        _w(lp, injected)  # another process creates the ledger after our read, before our lock
        return out
    ledger_sync.build = build_then_race
    try:
        res, bk = _sync(repo)
    finally:
        ledger_sync.build = real_build
    assert res["status"].startswith("ERROR write"), res["status"]
    assert lp.read_text(encoding="utf-8") == injected
    assert not list(bk.glob("*.json"))


def test_sync_project_end_to_end_does_not_overwrite_ledger_changed_during_sync():
    repo, *_ = _evidence_project()
    lp = _lpath(repo)
    real_build = ledger_sync.build
    injected = {}

    def build_then_race(p, o):
        out = real_build(p, o)
        d = json.loads(lp.read_text(encoding="utf-8"))
        d["events"].append({"kind": "note", "text": "written by someone else"})
        injected["text"] = json.dumps(d, indent=2)
        _w(lp, injected["text"])
        return out
    ledger_sync.build = build_then_race
    try:
        res, bk = _sync(repo)
    finally:
        ledger_sync.build = real_build
    assert res["status"].startswith("ERROR write") and "changed while the sync ran" in res["status"], res["status"]
    assert lp.read_text(encoding="utf-8") == injected["text"]
    assert not list(bk.glob("*.json"))
    assert "HARNESS_LEDGER" not in os.environ or os.environ["HARNESS_LEDGER"] != str(lp)


# ---------- plan_import_ledger_md: lock ----------
def _task_ledger_md(prefix):
    return ("# task ledger\n\n| Task | Status | Title | Basis |\n|---|---|---|---|\n"
            f"| ST-{prefix}-01.01 | ACCEPTED | first {prefix} | evidence |\n| ST-{prefix}-01.02 | PARTIAL | second {prefix} | evidence |\n")


def _import_setup(name):
    d = hv2.fresh(name)
    _w(d / "PROJECT_PLAN.md", "# Plan\n\n## Roadmap\n| Sprint | Title | Status | Goal |\n|---|---|---|---|\n| S01 | One | NOT_STARTED | x |\n| S02 | Two | NOT_STARTED | y |\n")
    _w(d / "l1.md", _task_ledger_md("ONE"))
    _w(d / "l2.md", _task_ledger_md("TWO"))
    return d


def _imp_cmd(d, led, sid, *more):
    return [hv2.PY, "-I", str(hv2.TOOLS / "plan_import_ledger_md.py"), "--ledger", str(d / led), "--plan", str(d / "PROJECT_PLAN.md"),
            "--sprint", sid, *more]


def test_two_concurrent_imports_with_different_sprints_keep_both_sections_and_leave_no_tmp():
    for rnd in range(6):
        d = _import_setup(f"imp{rnd}")
        env = hv2.base_env()
        procs = [subprocess.Popen(_imp_cmd(d, led, sid), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for led, sid in (("l1.md", "S01"), ("l2.md", "S02"))]
        outs = [p.communicate(timeout=90) for p in procs]
        assert [p.returncode for p in procs] == [0, 0], (rnd, outs)
        text = (d / "PROJECT_PLAN.md").read_text(encoding="utf-8")
        assert "## Sprint S01" in text and "## Sprint S02" in text, (rnd, text)
        assert "ST-ONE-01.01" in text and "ST-TWO-01.02" in text, (rnd, text)
        assert not list(d.glob("*.tmp")), list(d.iterdir())  # the OS-lock file stays (never deleted); no temp leftovers


def test_old_scheme_lock_file_older_than_120s_does_not_block_cli_and_is_kept():
    d = _import_setup("stale")
    lock = d / "PROJECT_PLAN.md.lock"
    lock.write_text(json.dumps({"token": "old", "pid": 99999, "start": ""}))
    old = time.time() - 200
    os.utime(lock, (old, old))
    r = hv2.run(_imp_cmd(d, "l1.md", "S01"), hv2.SRC)
    assert r.rc == 0, r.all
    assert "## Sprint S01" in (d / "PROJECT_PLAN.md").read_text(encoding="utf-8")
    assert lock.is_file(), "the lock file must never be deleted"


_HOLD = (
    "import sys, time\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import plan_import_ledger_md as pl\n"
    "with pl.plan_lock(Path(sys.argv[2])):\n"
    "    print('held', flush=True)\n"
    "    time.sleep(300)\n"
)


def _hold(plan):
    s = hv2.fresh("hold") / "h.py"
    s.write_text(_HOLD, encoding="utf-8")
    p = subprocess.Popen([hv2.PY, "-s", str(s), str(hv2.TOOLS), str(plan)], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


def test_live_holder_blocks_waiter_whatever_the_file_age_and_dead_holder_frees_it_at_once():
    d = _import_setup("bound")
    plan = d / "PROJECT_PLAN.md"
    lock = d / "PROJECT_PLAN.md.lock"
    holder = _hold(plan)
    try:
        for age in (100, 1000):
            t = time.time() - age
            os.utime(lock, (t, t))
            try:
                with pim.plan_lock(plan, timeout=0.4):
                    raise AssertionError("a lock of a live holder must be respected")
            except TimeoutError as e:
                assert str(lock) in str(e), str(e)
            assert lock.is_file(), "a waiter must not delete a live lock"
    finally:
        holder.kill()
        holder.wait(timeout=20)
    t0 = time.monotonic()
    with pim.plan_lock(plan, timeout=5):
        pass
    assert time.monotonic() - t0 < 3, "acquire after the holder died must be immediate"
    assert lock.is_file()


def test_dry_run_takes_no_lock_and_ignores_a_held_one():
    d = _import_setup("dry")
    plan = d / "PROJECT_PLAN.md"
    before = plan.read_bytes()
    lock = d / "PROJECT_PLAN.md.lock"
    r = hv2.run(_imp_cmd(d, "l1.md", "S01", "--dry-run"), hv2.SRC)
    assert r.rc == 0 and not lock.exists() and plan.read_bytes() == before, r.all  # no lock file is even created
    holder = _hold(plan)
    try:
        t0 = time.monotonic()
        r2 = hv2.run(_imp_cmd(d, "l1.md", "S01", "--dry-run"), hv2.SRC)
        assert r2.rc == 0 and time.monotonic() - t0 < 20, r2.all  # a lock-taking run would wait 30 s then fail
        assert plan.read_bytes() == before
    finally:
        holder.kill()
        holder.wait(timeout=20)


# ---------- audit_projects ----------
def _args_handler(script, event_extra=None):
    h = {"type": "command", "command": hv2.PY, "args": ["-s", "-X", "utf8", str(SRC / "harness" / "hooks" / "run_hook.py"), script,
                                                          "--harness-client=claude"], "timeout": 20}
    h.update(event_extra or {})
    return h


def _claude_args_home(rules_script="rules_gate.py", block=True):
    home = hv2.fresh("home")
    hooks = {"PreToolUse": [{"matcher": "Bash|PowerShell", "hooks": [_args_handler(rules_script)]},
                            {"matcher": "Agent|Task", "hooks": [_args_handler("agent_visibility.py")]}],
             "SubagentStart": [{"hooks": [_args_handler("agent_visibility.py")]}],
             "SubagentStop": [{"hooks": [_args_handler("agent_visibility.py")]}]}
    _w(home / ".claude" / "settings.json", json.dumps({"hooks": hooks}))
    _w(home / ".claude" / "CLAUDE.md", "# rules\n## Operator development rules (x)\n" if block else "# nothing\n")
    return home


def test_args_style_handler_recognised_by_registered_handlers_and_handler_text():
    h = _args_handler("rules_gate.py")
    cfg = {"hooks": {"PreToolUse": [{"matcher": "Bash|PowerShell", "hooks": [h]}]}}
    got = au.registered_handlers(cfg, "PreToolUse", "Bash")
    assert len(got) == 1 and got[0][1] == 20, got
    cmd = got[0][0]
    assert "run_hook.py" in cmd and "rules_gate.py" in cmd and "--harness-client=claude" in cmd, cmd
    parts = au.split_cmd(cmd)
    assert parts[0] == hv2.PY and "rules_gate.py" in parts, parts
    assert au.hook_name(cmd) == "rules_gate.py"
    assert "rules_gate.py" in au.handler_text(h) and "agent_visibility.py" not in au.handler_text(h)
    assert au.registered_handlers(cfg, "PreToolUse", "Read") == []  # matcher still honoured


def test_chk_claude_cfg_ok_with_args_style_and_fail_when_rules_gate_missing():
    au.HOME = _claude_args_home()
    st, detail = au.chk_claude_cfg()
    assert st == au.OK, (st, detail)
    au.HOME = _claude_args_home(rules_script="other_hook.py")
    st2, d2 = au.chk_claude_cfg()
    assert st2 == au.FAIL and "rules_gate.py not in PreToolUse" in d2, (st2, d2)
    au.HOME = _claude_args_home(block=False)
    st3, d3 = au.chk_claude_cfg()
    assert st3 == au.FAIL and "lacks" in d3, (st3, d3)


def test_chk_gates_live_ok_with_args_style_claude_handlers():
    home = _claude_args_home()
    au.AUDIT_TMP = hv2.fresh("auditscratch")
    import test_s13_audit as ta  # the project fixture the audit tests use (git repo with .harness, tools, pre-push)
    ledger_sync.BACKUP_DIR = hv2.fresh("backup")
    cwd = ta._mk_project(hv2.fresh("gateroot"), "p")
    st, detail = au.chk_gates_live(cwd, home / ".claude" / "settings.json", "claude", True)
    assert st == au.OK, (st, detail)


LW_PLAN = (ROAD + "| S00 | Zero | NOT_STARTED | below |\n\n## Sprint S00 - Zero\n\n| Task | Title | Status |\n|---|---|---|\n"
           "| ST-A#1 | a | ACCEPTED |\n| ST-A#2 | b | NOT_STARTED |\n| ST-A#3 | c | NOT_STARTED |\n")


def _lw_project(extra_genuine):
    repo = hv2.mk_repo("lw")
    _w(repo / "PROJECT_PLAN.md", LW_PLAN)
    _w(_lpath(repo), json.dumps(_ledger([])))
    res, _ = _sync(repo)
    assert res["status"] == "changed", res
    if extra_genuine:
        led = hv2.load_ledger(repo)
        sha = "a" * 40
        led["sprints"][0]["tasks"].append(_task("GEN-1", "done", commits=[sha], verify={"result": "pass", "task_commit": sha}))
        _w(_lpath(repo), json.dumps(led, indent=2))
    return repo


def test_ledger_whole_ok_when_equal():
    repo = _lw_project(False)
    st, detail = au.chk_ledger_whole(repo)
    assert st == au.OK and "plan 3 = ledger 3" in detail, (st, detail)


def test_ledger_whole_ok_when_unchanged_and_genuine_extra_tasks():
    repo = _lw_project(True)
    before = _lpath(repo).read_bytes()
    res, _ = _sync(repo, dry=True)
    assert res["status"] == "unchanged", res["status"]  # precondition: the sync keeps the genuine extra task
    st, detail = au.chk_ledger_whole(repo)
    assert st == au.OK and "plan 3 + 1 genuine" in detail, (st, detail)
    assert _lpath(repo).read_bytes() == before


def test_ledger_whole_fail_when_ledger_has_fewer_tasks_than_plan():
    repo = hv2.mk_repo("lwfew")
    _w(repo / "PROJECT_PLAN.md", LW_PLAN)
    _w(_lpath(repo), json.dumps(_ledger([_sprint("S00", [_task("ST-A#1", "done")])])))
    ledger_sync.BACKUP_DIR = hv2.fresh("backup")
    st, detail = au.chk_ledger_whole(repo)
    assert st == au.FAIL and "plan 3 tasks, ledger 1 tasks" in detail, (st, detail)


def test_ledger_whole_fail_when_extra_tasks_but_not_in_sync():
    repo = _lw_project(True)
    _w(repo / "PROJECT_PLAN.md", LW_PLAN.replace("| ST-A#2 | b | NOT_STARTED |", "| ST-A#2 | b | ACCEPTED |"))
    ledger_sync.BACKUP_DIR = hv2.fresh("backup")
    st, detail = au.chk_ledger_whole(repo)
    assert st == au.FAIL, (st, detail)


# ---------- install_claude ----------
def _built_source():
    src = SRC / "out" / "claude"
    if not (src / "CLAUDE.md").is_file() or not (src / "settings.hooks.json").is_file():
        r = hv2.run([hv2.PY, "-I", str(SRC / "tools" / "build.py"), "--python", hv2.PY], SRC)
        assert r.rc == 0, r.all
    return src


def test_install_replaces_claude_md_snapshots_old_and_restore_puts_it_back():
    src = _built_source()
    home = hv2.fresh("claudehome")
    old_md = "# OLD operator rules\nline two\n"
    _w(home / "CLAUDE.md", old_md)
    _w(home / "settings.json", json.dumps({"model": "keep-me", "hooks": {}}))
    snap = hv2.fresh("snapparent") / "snap"
    r = hv2.run([hv2.PY, "-I", str(SRC / "tools" / "install_claude.py"), "--home", str(home), "--snapshot", str(snap),
                 "--source", str(src)], SRC)
    assert r.rc == 0, r.all
    assert (home / "CLAUDE.md").read_bytes() == (src / "CLAUDE.md").read_bytes()
    assert (home / "CLAUDE.md").read_bytes() != old_md.encode()
    assert (snap / "CLAUDE.md").read_text(encoding="utf-8") == old_md
    assert json.loads((home / "settings.json").read_text(encoding="utf-8"))["model"] == "keep-me"
    rp = subprocess.run(["pwsh", "-NoProfile", "-File", str(snap / "restore.ps1")], capture_output=True, text=True, timeout=120)
    assert rp.returncode == 0, (rp.stdout, rp.stderr)
    assert (home / "CLAUDE.md").read_text(encoding="utf-8") == old_md
    restored = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert restored == {"model": "keep-me", "hooks": {}}, restored


def test_install_into_home_without_claude_md_creates_it_and_no_snapshot_copy():
    src = _built_source()
    home = hv2.fresh("claudehome2")
    _w(home / "settings.json", json.dumps({"hooks": {}}))
    snap = hv2.fresh("snapparent2") / "snap"
    r = hv2.run([hv2.PY, "-I", str(SRC / "tools" / "install_claude.py"), "--home", str(home), "--snapshot", str(snap),
                 "--source", str(src)], SRC)
    assert r.rc == 0, r.all
    assert (home / "CLAUDE.md").read_bytes() == (src / "CLAUDE.md").read_bytes()
    assert not (snap / "CLAUDE.md").exists()


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
