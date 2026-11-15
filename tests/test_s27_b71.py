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


def test_work_branches_from_transcript_edit_path_and_git_c():
    gate = load("session_close_gate3", HOOKS / "session_close_gate.py")
    repo = mk_repo("b71tr")
    g(repo, "checkout", "-q", "-b", "other-main-branch")
    base = hv2.fresh("b71trdir")
    wt = base / "wc"
    g(repo, "worktree", "add", "-q", "-b", "fix/voice", str(wt))
    wt2 = base / "wc2"
    g(repo, "worktree", "add", "-q", "-b", "fix/second", str(wt2))
    target = wt / "a.txt"

    def row(typ, block):
        return {"type": typ, "message": {"role": typ, "content": [block]}}

    edit = row("assistant", {"type": "tool_use", "id": "t1", "name": "Edit",
                             "input": {"file_path": str(target), "old_string": "x", "new_string": "y"}})
    push = row("assistant", {"type": "tool_use", "id": "t2", "name": "Bash",
                             "input": {"command": "git -C %s push" % wt2}})
    # a Read of a file in a third worktree must NOT tie the session to its branch
    wt3 = base / "wc3"
    g(repo, "worktree", "add", "-q", "-b", "fix/third", str(wt3))
    read = row("assistant", {"type": "tool_use", "id": "t3", "name": "Read", "input": {"file_path": str(wt3 / "a.txt")}})

    def tp(name, rows):
        p = base / name
        p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8", newline="\n")
        return str(p)

    got = gate.work_branches({"cwd": str(repo), "transcript_path": tp("t1.jsonl", [edit])})
    assert got == ["fix/voice", "other-main-branch"], got  # Edit path's work copy first, then the cwd
    got = gate.work_branches({"cwd": str(repo), "transcript_path": tp("t2.jsonl", [edit, push, read])})
    assert got == ["fix/voice", "fix/second", "other-main-branch"], got  # Edit folders outrank git -C folders, cwd last, Read ignored
    got = gate.work_branches({"cwd": str(repo), "transcript_path": tp("t3.jsonl", [])})
    assert got == ["other-main-branch"], got
    got = gate.work_branches({"cwd": str(repo), "transcript_path": str(base / "missing.jsonl")})
    assert got == ["other-main-branch"], got
    # end to end: the worktree branch picks the sprint
    led = {"sprints": [sp("M2", "sprint/m2", [task("M2-T1", state="in_progress")]),
                       sp("V", "fix/voice", [task("V-T1", state="in_progress")])]}
    br = gate.work_branches({"cwd": str(repo), "transcript_path": tp("t4.jsonl", [edit, push])})
    assert gate.pick_sprint(led, br)["id"] == "V"


# ---------- 3b. report: sprint of the checked-out branch is the active one ----------
def test_report_marks_branch_sprint_active():
    sys.path.insert(0, str(TOOLS))
    report = load("report_mod", TOOLS / "report.py")
    repo = mk_repo("b71rep")
    sha = g(repo, "rev-parse", "HEAD")
    d = {"version": 1, "project": "p", "events": [], "approval": None,
         "sprints": [sp("M2", "sprint/m2", [task("M2-T1", state="in_progress", commits=[sha])]),
                     sp("VOICE2", "fix/voice", [task("V2-T1", state="in_progress", commits=[sha])])]}

    def rows(html):
        return {p.split('"', 1)[0]: p for p in html.split('id="sp-')[1:]}

    git = report.Git(repo)
    # on main nobody owns the branch: the first unclosed sprint is active
    a = report.sec_active(d, git)
    assert "Спринт M2</b>" in a and "Спринт VOICE2</b>" not in a, a[:400]
    r = rows(report.sec_sprints(d, git))
    assert "до сейчас" in r["M2"] and "до сейчас" not in r["VOICE2"]
    g(repo, "checkout", "-q", "-b", "fix/voice")
    git = report.Git(repo)
    a = report.sec_active(d, git)
    assert "Спринт VOICE2</b>" in a and "Спринт M2</b>" not in a and "V2-T1" in a and "M2-T1" not in a, a[:400]
    r = rows(report.sec_sprints(d, git))
    assert set(r) == {"M2", "VOICE2"}, list(r)
    assert "до сейчас" in r["VOICE2"] and "до сейчас" not in r["M2"]
    # a closed sprint on the current branch is not active
    d["sprints"][1]["state"] = "closed"
    a = report.sec_active(d, report.Git(repo))
    assert "Спринт M2</b>" in a, a[:400]


# ---------- 4. dangerous_command_gate ----------
def test_classify_branch_and_symbolic_ref():
    dg = load("dangerous_command_gate", HOOKS / "dangerous_command_gate.py")
    refuse = ["git branch -dr origin/main", "git branch -d -r origin/master", "git symbolic-ref -d HEAD",
              "git symbolic-ref --delete refs/remotes/origin/HEAD", "git branch -D main", "git branch -d main",
              "git branch -D master", "git branch --delete main"]
    refuse += ['git branch -d "main"', "git branch -d 'main'", "git branch -d origin/main -r",
               "git branch -d --remotes origin/main", "git branch --delete --remotes origin/main",
               'git branch -D "master"']
    allow = ["git branch -d x --contains main", "git branch --contains main", "git branch -d feature/main-menu",
             "git branch -dr origin/feature-x", "git symbolic-ref HEAD", "git symbolic-ref --short HEAD", "git branch -d x"]
    for c in refuse:
        assert dg.classify(c), "must refuse: " + c
    for c in allow:
        assert not dg.classify(c), "must allow: %s (got %r)" % (c, dg.classify(c))


def test_classify_branch_option_forms():
    dg = load("dangerous_command_gate2", HOOKS / "dangerous_command_gate.py")
    refuse = ["git branch -D --abbrev main", "git branch -d --column main", "git branch --del main",
              "git branch --dele main", "git branch -dr foo/bar/main", "git symbolic-ref -dq HEAD",
              "git symbolic-ref --del HEAD", "git symbolic-ref --delete HEAD",
              "git branch -d --remote origin/main", "git branch -d --rem origin/main",
              "git branch --delete --remotes origin/master"]
    allow = ["git branch --abbrev=7", "git branch -d x --contains main", "git branch -d feature/main-menu",
             "git symbolic-ref HEAD", "git branch --contains main", "git branch -d --remote origin/feature-x"]
    bad = [c for c in refuse if not dg.classify(c)]
    assert not bad, "must refuse: %s" % bad
    bad = ["%s (got %r)" % (c, dg.classify(c)) for c in allow if dg.classify(c)]
    assert not bad, "must allow: %s" % bad


def _edit_row(i, path):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "e%d" % i, "name": "Edit",
         "input": {"file_path": str(path), "old_string": "x", "new_string": "y"}}]}}


def _folder_of(p):
    return p if os.path.isdir(p) else os.path.dirname(p)


def test_work_branches_one_git_call_per_folder():
    gate = load("session_close_gate4", HOOKS / "session_close_gate.py")
    base = hv2.fresh("b71cnt")
    cwd = base / "cwd"
    folders = [base / ("f%d" % i) for i in range(3)]
    for d in [cwd, *folders]:
        d.mkdir()
    calls = []

    def fake(path):
        calls.append(path)
        return "br-" + os.path.basename(_folder_of(path))

    orig = gate._branch_of
    gate._branch_of = fake
    try:
        rows = [_edit_row(i, folders[i % 3] / ("file%d.txt" % i)) for i in range(50)]
        tp = base / "t.jsonl"
        tp.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8", newline="\n")
        got = gate.work_branches({"cwd": str(cwd), "transcript_path": str(tp)})
        assert len(calls) <= 4, len(calls)
        assert set(got) == {"br-cwd", "br-f0", "br-f1", "br-f2"}, got
        assert got[-1] == "br-cwd", got  # activity first, cwd after
        assert got[0] == "br-f1", got  # most recent edit (file49) is in f1

        calls.clear()
        many = [base / ("m%d" % i) for i in range(40)]
        for d in many:
            d.mkdir()
        rows = [_edit_row(i, many[i] / "a.txt") for i in range(40)]
        tp2 = base / "t2.jsonl"
        tp2.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8", newline="\n")
        got = gate.work_branches({"cwd": str(cwd), "transcript_path": str(tp2)})
        assert len(calls) <= 25, len(calls)  # 24 activity folders + the always-read cwd
        assert got[0] == "br-m39" and got[1] == "br-m38" and "br-cwd" in got and "br-m0" not in got, got
    finally:
        gate._branch_of = orig


def _tool_row(i, name, inp):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "x%d" % i, "name": name, "input": inp}]}}


def _write_tp(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8", newline="\n")
    return str(path)


def _first_row(cwd):
    return {"type": "user", "cwd": str(cwd), "message": {"role": "user", "content": [{"type": "text", "text": "go"}]}}


def test_session_started_in_main_checkout_works_in_worktree_picks_worktree_sprint():
    gate = load("session_close_gate5", HOOKS / "session_close_gate.py")
    repo = mk_repo("b71ord")
    g(repo, "checkout", "-q", "-b", "sprint/a")  # main checkout sits on the branch of unclosed sprint A
    base = hv2.fresh("b71orddir")
    wt = base / "wcb"
    g(repo, "worktree", "add", "-q", "-b", "sprint/b", str(wt))
    first = _first_row(repo)
    rows = [first, _tool_row(1, "Edit", {"file_path": str(wt / "a.txt"), "old_string": "x", "new_string": "y"})]
    tp = _write_tp(base / "t.jsonl", rows)
    br = gate.work_branches({"cwd": str(repo), "transcript_path": tp})
    assert br == ["sprint/b", "sprint/a"], br
    proj = base / "proj"
    (proj / ".harness").mkdir(parents=True)
    d = {"version": 1, "project": "p", "approval": None, "events": [],
         "sprints": [sp("A", "sprint/a", [task("A-T1", state="in_progress")]),
                     sp("B", "sprint/b", [task("B-T1", state="in_progress")])]}
    (proj / ".harness" / "ledger.json").write_text(json.dumps(d), encoding="utf-8", newline="\n")
    assert gate.ledger_open_tasks(proj, set(), br) == [("B-T1", "in_progress")]
    # without worktree activity the session belongs to the main checkout's sprint A
    tp0 = _write_tp(base / "t0.jsonl", [first])
    br0 = gate.work_branches({"cwd": str(repo), "transcript_path": tp0})
    assert br0 == ["sprint/a"], br0
    assert gate.ledger_open_tasks(proj, set(), br0) == [("A-T1", "in_progress")]


def test_payload_cwd_ranks_before_session_start_cwd():
    gate = load("session_close_gate6", HOOKS / "session_close_gate.py")
    repo = mk_repo("b71ord2")
    g(repo, "checkout", "-q", "-b", "sprint/a")
    base = hv2.fresh("b71ord2dir")
    wt = base / "wcb"
    g(repo, "worktree", "add", "-q", "-b", "sprint/b", str(wt))
    tp = _write_tp(base / "t.jsonl", [_first_row(wt)])
    got = gate.work_branches({"cwd": str(repo), "transcript_path": tp})
    assert got == ["sprint/a", "sprint/b"], got


def test_two_worktree_edits_later_one_ranks_first():
    gate = load("session_close_gate7", HOOKS / "session_close_gate.py")
    repo = mk_repo("b71ord3")
    g(repo, "checkout", "-q", "-b", "sprint/a")
    base = hv2.fresh("b71ord3dir")
    wb = base / "wcb"
    wc = base / "wcc"
    g(repo, "worktree", "add", "-q", "-b", "sprint/b", str(wb))
    g(repo, "worktree", "add", "-q", "-b", "sprint/c", str(wc))
    first = _first_row(repo)

    def ed(i, d):
        return _tool_row(i, "Edit", {"file_path": str(d / "a.txt"), "old_string": "x", "new_string": "y"})

    tp = _write_tp(base / "t1.jsonl", [first, ed(1, wb), ed(2, wc)])
    got = gate.work_branches({"cwd": str(repo), "transcript_path": tp})
    assert got == ["sprint/c", "sprint/b", "sprint/a"], got
    tp = _write_tp(base / "t2.jsonl", [first, ed(1, wc), ed(2, wb)])
    got = gate.work_branches({"cwd": str(repo), "transcript_path": tp})
    assert got == ["sprint/b", "sprint/c", "sprint/a"], got
    tp = _write_tp(base / "t3.jsonl", [first, ed(1, wb), ed(2, wc), ed(3, wb)])  # re-editing b makes it most recent again
    got = gate.work_branches({"cwd": str(repo), "transcript_path": tp})
    assert got == ["sprint/b", "sprint/c", "sprint/a"], got


def test_edit_in_b_then_git_c_read_of_a_keeps_session_on_b():
    gate = load("session_close_gate8", HOOKS / "session_close_gate.py")
    repo = mk_repo("b71ord4")  # payload cwd: main checkout on main
    base = hv2.fresh("b71ord4dir")
    wa = base / "wca"
    wb = base / "wcb"
    g(repo, "worktree", "add", "-q", "-b", "sprint/a", str(wa))
    g(repo, "worktree", "add", "-q", "-b", "sprint/b", str(wb))
    rows = [_first_row(repo),
            _tool_row(1, "Edit", {"file_path": str(wb / "a.txt"), "old_string": "x", "new_string": "y"}),
            _tool_row(2, "Bash", {"command": "git -C %s log" % wa})]  # later, but only a read of A
    tp = _write_tp(base / "t.jsonl", rows)
    br = gate.work_branches({"cwd": str(repo), "transcript_path": tp})
    assert br[0] == "sprint/b", br
    assert br == ["sprint/b", "sprint/a", "main"], br
    proj = base / "proj"
    (proj / ".harness").mkdir(parents=True)
    d = {"version": 1, "project": "p", "approval": None, "events": [],
         "sprints": [sp("A", "sprint/a", [task("A-T1", state="in_progress")]),  # unclosed and listed first
                     sp("B", "sprint/b", [task("B-T1", state="in_progress")])]}
    (proj / ".harness" / "ledger.json").write_text(json.dumps(d), encoding="utf-8", newline="\n")
    assert gate.ledger_open_tasks(proj, set(), br) == [("B-T1", "in_progress")]


def test_git_c_path_not_polluted_by_trailing_punctuation_or_quotes():
    gate = load("session_close_gate9", HOOKS / "session_close_gate.py")
    repo = mk_repo("b71punct")  # cwd on main; only the git -C folder can yield sprint/p
    base = hv2.fresh("b71punctdir")
    wt = base / "wcp"
    g(repo, "worktree", "add", "-q", "-b", "sprint/p", str(wt))
    spaced = base / "wc space"
    g(repo, "worktree", "add", "-q", "-b", "sprint/sp", str(spaced))
    forms = ["git -C %s;" % wt, "git -C %s&& git status" % wt, "(git -C %s)" % wt, "git -C %s; git log" % wt,
             "git -C %s | cat" % wt, 'git -C "%s" log' % wt, "git -C '%s' log" % wt]
    for i, cmd in enumerate(forms):
        tp = _write_tp(base / ("p%d.jsonl" % i), [_tool_row(1, "Bash", {"command": cmd})])
        br = gate.work_branches({"cwd": str(repo), "transcript_path": tp})
        assert br == ["sprint/p", "main"], (cmd, br)
    for i, cmd in enumerate(['git -C "%s" log' % spaced, "git -C '%s' status" % spaced, 'git -C "%s"; ls' % spaced]):
        tp = _write_tp(base / ("s%d.jsonl" % i), [_tool_row(1, "Bash", {"command": cmd})])
        br = gate.work_branches({"cwd": str(repo), "transcript_path": tp})
        assert br == ["sprint/sp", "main"], (cmd, br)


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
