"""S30 tests: the push gate (rules_gate._ledger_problems) checks the sprint whose `branch` equals the checked-out
branch, and falls back to the first unclosed sprint when no sprint names the branch or HEAD is detached.
Written from the acceptance criteria. All state is temp (temp git repo, temp ledger file).
Run: C:\\Python314\\python.exe -I tests/test_s30_push_gate_branch.py"""
import importlib.util
import json
import subprocess
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hv2  # noqa: E402

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


def task(tid, state):
    return {"id": tid, "title": "t " + tid, "criteria": [], "run": "", "risk": "local", "journey": False, "state": state,
            "commits": [], "verify": None, "review": None, "routing": None, "tokens": None}


def sprint(sid, branch, tasks, state="open"):
    return {"id": sid, "title": "s " + sid, "branch": branch, "state": state, "opened": "2026-11-12T00:00:00+02:00",
            "tasks": tasks}


def ledger_problems(repo, sprints):
    led = repo / "ledger.json"
    led.write_text(json.dumps({"sprints": sprints, "approval": None, "events": []}), encoding="utf-8", newline="\n")
    rg = load("rules_gate_mod", HOOKS / "rules_gate.py")
    return rg._ledger_problems(str(repo), led)


def sprint_text(problems):
    return " | ".join(p for p in problems if "neither verified/done nor explicitly blocked" in p)


def test_branch_sprint_decides_not_first_unclosed():
    repo = mk_repo("s30a")
    g(repo, "checkout", "-q", "-b", "feat/mine")
    sprints = [sprint("M2", "sprint/m2", [task("M2.03", "implemented")]),
               sprint("MINE", "feat/mine", [task("MINE.1", "verified")])]
    assert sprint_text(ledger_problems(repo, sprints)) == ""


def test_branch_sprint_with_open_task_blocks():
    repo = mk_repo("s30b")
    g(repo, "checkout", "-q", "-b", "feat/mine")
    sprints = [sprint("M2", "sprint/m2", [task("M2.03", "verified")]),
               sprint("MINE", "feat/mine", [task("MINE.1", "implemented")])]
    t = sprint_text(ledger_problems(repo, sprints))
    assert "sprint MINE" in t and "MINE.1" in t and "M2" not in t.replace("MINE", "")


def test_unknown_branch_falls_back_to_first_unclosed():
    repo = mk_repo("s30c")
    g(repo, "checkout", "-q", "-b", "feat/other")
    sprints = [sprint("M2", "sprint/m2", [task("M2.03", "implemented")]),
               sprint("MINE", "feat/mine", [task("MINE.1", "verified")])]
    t = sprint_text(ledger_problems(repo, sprints))
    assert "sprint M2" in t and "M2.03" in t


def test_detached_head_falls_back_to_first_unclosed():
    repo = mk_repo("s30d")
    g(repo, "checkout", "-q", "--detach", "HEAD")
    sprints = [sprint("M2", "sprint/m2", [task("M2.03", "implemented")]),
               sprint("MINE", "feat/mine", [task("MINE.1", "verified")])]
    assert "sprint M2" in sprint_text(ledger_problems(repo, sprints))


def test_closed_branch_sprint_is_ignored():
    repo = mk_repo("s30e")
    g(repo, "checkout", "-q", "-b", "feat/mine")
    sprints = [sprint("M2", "sprint/m2", [task("M2.03", "implemented")]),
               sprint("MINE", "feat/mine", [task("MINE.1", "verified")], state="closed")]
    assert "sprint M2" in sprint_text(ledger_problems(repo, sprints))


def main():
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception:  # noqa: BLE001
                fails += 1
                print("FAIL", name)
                traceback.print_exc()
    print("failed:", fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
