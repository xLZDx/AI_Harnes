"""S28 acceptance tests: agents_context drop/`?` ages and agent_visibility._archive_stale (stale.jsonl)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

HOOKS = Path(r"D:\tmp\scratch_worktrees\hv2-s28\harness\hooks")
CTX = HOOKS / "agents_context.py"
H, M = 3600, 60


def env(state):
    e = {k: v for k, v in os.environ.items() if not k.startswith("HARNESS_")}
    e["HARNESS_HOOK_STATE"] = str(state)
    e["HARNESS_HOOK_CONFIG"] = str(Path(state) / "no_config.json")
    return e


def write_active(state, entries):
    d = Path(state) / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "active.json").write_text(json.dumps({"agents": entries}), encoding="utf-8")


def read_active(state):
    return json.loads((Path(state) / "agents" / "active.json").read_text(encoding="utf-8"))["agents"]


def ent(typ, age, session="s1", project="", stop=None, activity=None):
    e = {"type": typ, "model": "m", "effort": "e", "task": "t", "start": time.time() - age,
         "project": project, "session": session, "tool_use_id": "", "sources": ["pre"]}
    if stop:
        e["stop"] = time.time() - age + 5
    if activity is not None:
        e["activity"] = time.time() - activity
    return e


def line(state, cwd, session="s1"):
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": session, "cwd": str(cwd)}
    cp = subprocess.run([sys.executable, "-I", "-s", str(CTX)], input=json.dumps(payload), capture_output=True,
                        text=True, encoding="utf-8", env=env(state), timeout=60)
    assert cp.returncode == 0, cp.stderr[-300:]
    if not cp.stdout.strip():
        return ""
    return json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"]


def start(state, cwd, typ="newtype"):
    code = ("import sys; sys.path.insert(0, r'%s'); import agent_visibility as av; "
            "av.announce_start({'tool_input': {'subagent_type': '%s'}, 'cwd': r'%s', 'session_id': 's1', "
            "'tool_use_id': 'tu-new'}, 'claude')" % (HOOKS, typ, cwd))
    cp = subprocess.run([sys.executable, "-I", "-s", "-c", code], capture_output=True, text=True,
                        encoding="utf-8", env=env(state), timeout=60)
    assert cp.returncode == 0, cp.stderr[-300:]
    return cp.stdout


def tmp():
    return tempfile.mkdtemp(prefix="s28_")


def test_line_ages():
    st, cwd = tmp(), tmp()
    write_active(st, [ent("a30s", 30), ent("a20m", 20 * M), ent("a2h", 2 * H), ent("a30h", 30 * H),
                      ent("stopped", 30, stop=True)])
    got = line(st, cwd)
    assert got == "Running harness agents: a30s, a20m?", got


def test_activity_keeps_old_start_listed():
    st, cwd = tmp(), tmp()
    write_active(st, [ent("busy", 2 * H, activity=30)])
    assert line(st, cwd) == "Running harness agents: busy"


def test_boundary_just_under_and_over_one_hour():
    st, cwd = tmp(), tmp()
    write_active(st, [ent("under", 59 * M), ent("over", 61 * M)])
    assert line(st, cwd) == "Running harness agents: under?"


def test_only_stale_gives_no_output():
    st, cwd = tmp(), tmp()
    write_active(st, [ent("a2h", 2 * H), ent("stopped", 10, stop=True)])
    assert line(st, cwd) == ""


def test_project_match_other_session():
    st, cwd = tmp(), tmp()
    r = subprocess.run(["git", "init", "-q", cwd], capture_output=True)
    assert r.returncode == 0
    proj = str(Path(cwd).resolve())
    write_active(st, [ent("mine", 20, session="other", project=proj),
                      ent("foreign", 20, session="x", project="D:\\nope"),
                      ent("oldmine", 2 * H, session="other", project=proj)])
    assert line(st, cwd, session="s1") == "Running harness agents: mine"


def stale_lines(st):
    f = Path(st) / "agents" / "stale.jsonl"
    if not f.is_file():
        return []
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_archive_30h_unfinished_and_new_start():
    st, cwd = tmp(), tmp()
    write_active(st, [ent("dead30h", 30 * H)])
    start(st, cwd)
    types = [e["type"] for e in read_active(st)]
    assert types == ["newtype"], types
    sl = stale_lines(st)
    assert len(sl) == 1 and sl[0]["type"] == "dead30h", sl
    assert isinstance(sl[0]["archived"], (int, float)) and abs(sl[0]["archived"] - time.time()) < 120, sl[0]


def test_finished_30h_stays_and_readable():
    st, cwd = tmp(), tmp()
    write_active(st, [ent("done30h", 30 * H, stop=True)])
    start(st, cwd)
    a = read_active(st)
    assert [e["type"] for e in a] == ["done30h", "newtype"], a
    assert a[0].get("stop"), a[0]
    assert stale_lines(st) == []


def test_2h_unfinished_stays():
    st, cwd = tmp(), tmp()
    write_active(st, [ent("a2h", 2 * H)])
    start(st, cwd)
    assert [e["type"] for e in read_active(st)] == ["a2h", "newtype"]
    assert stale_lines(st) == []


def test_boundary_23h_stays_25h_archived():
    st, cwd = tmp(), tmp()
    write_active(st, [ent("a23h", 23 * H), ent("a25h", 25 * H)])
    start(st, cwd)
    assert [e["type"] for e in read_active(st)] == ["a23h", "newtype"]
    assert [e["type"] for e in stale_lines(st)] == ["a25h"]


def test_activity_refreshes_age():
    st, cwd = tmp(), tmp()
    write_active(st, [ent("alive", 30 * H, activity=10 * M)])
    start(st, cwd)
    assert [e["type"] for e in read_active(st)] == ["alive", "newtype"]
    assert stale_lines(st) == []


def test_archive_appends_not_overwrites():
    st, cwd = tmp(), tmp()
    write_active(st, [ent("d1", 30 * H)])
    start(st, cwd, "n1")
    write_active(st, read_active(st) + [ent("d2", 40 * H)])
    start(st, cwd, "n2")
    assert [e["type"] for e in stale_lines(st)] == ["d1", "d2"]


def test_unwritable_archive_keeps_everything():
    st, cwd = tmp(), tmp()
    write_active(st, [ent("dead30h", 30 * H), ent("done", 30 * H, stop=True)])
    (Path(st) / "agents" / "stale.jsonl").mkdir()  # sibling path is a directory: open() fails
    start(st, cwd)
    assert [e["type"] for e in read_active(st)] == ["dead30h", "done", "newtype"], read_active(st)
    assert (Path(st) / "agents" / "stale.jsonl").is_dir()


def run_all():
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception:
                fails += 1
                print("FAIL", name)
                traceback.print_exc()
    print(f"OK: {fails} failing" if not fails else f"FAILED: {fails} failing")
    return fails


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
