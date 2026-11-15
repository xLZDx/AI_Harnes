"""S27 tests: B71 (sprint chosen by work-copy branch in ledger.py and session_close_gate.py), reopen --keep-evidence,
B72 (dangerous_command_gate: remote-tracking main, symbolic-ref -d, option values are not branch names).
Written from the acceptance criteria. All state is temp (temp git repos, HARNESS_LEDGER temp file).
Run: C:\\Python314\\python.exe -I tests/test_s27_b71.py"""
import importlib.util
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hv2  # noqa: E402

TOOLS = hv2.SRC / "tools"
HOOKS = hv2.SRC / "harness" / "hooks"


def load(name, path):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def g(cwd, *a):
    r = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, (a, r.stderr)
    return r.stdout.strip()


def mk_repo(name):
    p = hv2.fresh(name)
    g(p, "init", "-q", "-b", "main")
    g(p, "config", "user.email", "t@t")
    g(p, "config", "user.name", "t")
    (p / "a.txt").write_text("x\n", encoding="utf-8", newline="\n")
    g(p, "add", ".")
    g(p, "commit", "-q", "-m", "init")
    return p


def run_ledger(cwd, ledger, *a):
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARNESS_")}
    env["HARNESS_LEDGER"] = str(ledger)
    r = subprocess.run([sys.executable, "-I", str(TOOLS / "ledger.py"), *a], cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", env=env)
    return r.returncode, r.stdout + r.stderr


def task(tid, **kw):
    t = {"id": tid, "title": "t " + tid, "criteria": [], "run": "", "risk": "local", "journey": False, "state": "todo",
         "commits": [], "verify": None, "review": None, "routing": None, "tokens": None}
    t.update(kw)
    return t


def sp(sid, branch, tasks=(), state="open"):
    return {"id": sid, "title": "s " + sid, "branch": branch, "state": state, "opened": "2026-11-12T00:00:00+02:00",
            "tasks": list(tasks)}


# ---------- 1. active_sprint ----------
def test_active_sprint_unit():
    led = load("ledger_mod", TOOLS / "ledger.py")
    d = {"sprints": [sp("M2", "sprint/m2"), sp("V", "fix/voice"), sp("C", "fix/c", state="closed")]}
    assert led.active_sprint(d)["id"] == "M2"
    assert led.active_sprint(d, None)["id"] == "M2"
    assert led.active_sprint(d, "fix/voice")["id"] == "V"
    assert led.active_sprint(d, "other")["id"] == "M2"
    assert led.active_sprint(d, "fix/c")["id"] == "M2", "closed sprint must not be returned for its branch"
    d["sprints"][0]["state"] = "closed"
    d["sprints"][1]["state"] = "closed"
    assert led.active_sprint(d) is None and led.active_sprint(d, "fix/voice") is None


def test_cli_next_and_task_add_by_branch():
    repo = mk_repo("b71cli")
    lf = repo / "ledger.json"
    for a in (("sprint-add", "--id", "M2", "--title", "foreign", "--branch", "sprint/m2"),
              ("task-add", "--sprint", "M2", "--id", "M2-T1", "--title", "foreign task"),
              ("sprint-add", "--id", "VOICE2", "--title", "mine", "--branch", "fix/voice"),
              ("task-add", "--sprint", "VOICE2", "--id", "V2-T1", "--title", "my task")):
        rc, out = run_ledger(repo, lf, *a)
        assert rc == 0, out
    rc, out = run_ledger(repo, lf, "next")
    assert "M2-T1" in out and "V2-T1" not in out, out
    rc, out = run_ledger(repo, lf, "task-add", "--id", "ON-MAIN", "--title", "x")
    assert rc == 0, out
    d = json.loads(lf.read_text(encoding="utf-8"))
    assert [t["id"] for t in d["sprints"][0]["tasks"]] == ["M2-T1", "ON-MAIN"]
    g(repo, "checkout", "-q", "-b", "fix/voice")
    rc, out = run_ledger(repo, lf, "next")
    assert "V2-T1" in out and "M2-T1" not in out, out
    rc, out = run_ledger(repo, lf, "task-add", "--id", "ON-VOICE", "--title", "y")
    assert rc == 0, out
    d = json.loads(lf.read_text(encoding="utf-8"))
    assert "ON-VOICE" in [t["id"] for t in d["sprints"][1]["tasks"]]
    assert "ON-VOICE" not in [t["id"] for t in d["sprints"][0]["tasks"]]
    rc, out = run_ledger(repo, lf, "task-add", "--sprint", "M2", "--id", "EXPL", "--title", "z")
    assert rc == 0, out
    d = json.loads(lf.read_text(encoding="utf-8"))
    assert "EXPL" in [t["id"] for t in d["sprints"][0]["tasks"]], "explicit --sprint must win over the branch"


# ---------- 2. reopen ----------
def closed_ledger():
    v = {"result": "pass", "task_commit": "abc", "agent_run": "r1", "at": "2026-11-12T00:00:00+02:00"}
    r = {"result": "accepted", "task_commit": "abc", "findings": 0}
    t = task("S-T1", state="done", commits=["abc"], verify=v, review=r)
    s = sp("S", "sprint/s", [t], state="closed")
    s["closed"] = "2026-11-13T00:00:00+02:00"
    return {"version": 1, "project": "p", "sprints": [s], "events": [],
            "approval": {"head": "abc", "result": "APPROVE", "at": "2026-11-13T00:00:00+02:00"}}


def reopen(extra):
    repo = mk_repo("b71reopen")
    lf = repo / "ledger.json"
    d = closed_ledger()
    lf.write_text(json.dumps(d, indent=1), encoding="utf-8", newline="\n")
    rc, out = run_ledger(repo, lf, "reopen", "S", "--reason", "x", *extra)
    assert rc == 0, out
    return d, json.loads(lf.read_text(encoding="utf-8")), out


def test_reopen_default_clears():
    before, after, out = reopen([])
    t = after["sprints"][0]["tasks"][0]
    assert t["verify"] is None and t["review"] is None
    assert after["approval"] is None
    assert after["sprints"][0]["state"] == "open"
    assert "CLEARED" in out and "--keep-evidence" in out, out
    ev = [e for e in after["events"] if "sprint-reopen" in json.dumps(e)]
    assert ev and ev[-1].get("evidence_kept") is False and ev[-1].get("approval_dropped") is True, ev


def test_reopen_keep_evidence():
    before, after, out = reopen(["--keep-evidence"])
    bt, at = before["sprints"][0]["tasks"][0], after["sprints"][0]["tasks"][0]
    assert json.dumps(at["verify"], sort_keys=True) == json.dumps(bt["verify"], sort_keys=True)
    assert json.dumps(at["review"], sort_keys=True) == json.dumps(bt["review"], sort_keys=True)
    assert after["approval"] == before["approval"] and after["approval"] is not None
    assert at["commits"] == ["abc"]
    assert after["sprints"][0]["state"] == "open" and after["sprints"][0]["closed"] is None
    ev = [e for e in after["events"] if "sprint-reopen" in json.dumps(e)]
    assert ev and ev[-1].get("evidence_kept") is True and ev[-1].get("approval_dropped") is False, ev
    assert "CLEARED" not in out


# ---------- 3. session_close_gate ----------
def test_pick_sprint_and_open_tasks():
    gate = load("session_close_gate", HOOKS / "session_close_gate.py")
    led = {"sprints": [sp("M2", "sprint/m2", [task("M2-T1", state="in_progress")]),
                       sp("V", "fix/voice", [task("V-T1", state="in_progress")]),
                       sp("W", "fix/w", state="closed")]}
    assert gate.pick_sprint(led, [])["id"] == "M2"
    assert gate.pick_sprint(led, ["fix/voice"])["id"] == "V"
    assert gate.pick_sprint(led, ["nothing", "fix/voice"])["id"] == "V"
    assert gate.pick_sprint(led, ["fix/w"])["id"] == "M2", "closed sprint branch is ignored"
    led["sprints"][0]["branch"] = "a"
    led["sprints"][1]["branch"] = "b"
    assert gate.pick_sprint(led, ["b", "a"])["id"] == "V", "first branch in the list wins"
    led["sprints"][0]["state"] = led["sprints"][1]["state"] = "closed"
    assert gate.pick_sprint(led, ["a"]) is None
    proj = hv2.fresh("b71gate") / "proj"
    (proj / ".harness").mkdir(parents=True)
    d = {"version": 1, "project": "p", "approval": None, "events": [],
         "sprints": [sp("M2", "sprint/m2", [task("M2-T1", state="in_progress")]),
                     sp("V", "fix/voice", [task("V-T1", state="in_progress")])]}
    (proj / ".harness" / "ledger.json").write_text(json.dumps(d), encoding="utf-8", newline="\n")
    assert gate.ledger_open_tasks(proj, set(), ["fix/voice"]) == [("V-T1", "in_progress")]
    assert gate.ledger_open_tasks(proj, set(), []) == [("M2-T1", "in_progress")]
    assert gate.ledger_open_tasks(proj, set()) == [("M2-T1", "in_progress")]
    assert gate.ledger_open_tasks(proj, set(), ["zzz"]) == [("M2-T1", "in_progress")]


def test_work_branches_uses_worktree_not_main_checkout():
    gate = load("session_close_gate2", HOOKS / "session_close_gate.py")
    repo = mk_repo("b71wt")
    g(repo, "checkout", "-q", "-b", "other-main-branch")
    wt = hv2.fresh("b71wtdir") / "wc"
    g(repo, "worktree", "add", "-q", "-b", "fix/voice", str(wt))
    assert gate.work_branches({"cwd": str(wt)}) == ["fix/voice"]
    assert gate.work_branches({"cwd": str(repo)}) == ["other-main-branch"]
    assert gate.work_branches({}) == []


# ---------- 4. dangerous_command_gate ----------
def test_classify_branch_and_symbolic_ref():
    dg = load("dangerous_command_gate", HOOKS / "dangerous_command_gate.py")
    refuse = ["git branch -dr origin/main", "git branch -d -r origin/master", "git symbolic-ref -d HEAD",
              "git symbolic-ref --delete refs/remotes/origin/HEAD", "git branch -D main", "git branch -d main",
              "git branch -D master", "git branch --delete main"]
    allow = ["git branch -d x --contains main", "git branch --contains main", "git branch -d feature/main-menu",
             "git branch -dr origin/feature-x", "git symbolic-ref HEAD", "git symbolic-ref --short HEAD", "git branch -d x"]
    for c in refuse:
        assert dg.classify(c), "must refuse: " + c
    for c in allow:
        assert not dg.classify(c), "must allow: %s (got %r)" % (c, dg.classify(c))


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    bad = 0
    for fn in TESTS:
        try:
            fn()
            print("PASS", fn.__name__)
        except BaseException:  # noqa: BLE001
            bad += 1
            print("FAIL", fn.__name__)
            traceback.print_exc()
    print(f"{len(TESTS) - bad} passed, {bad} failed")
    sys.exit(1 if bad else 0)
