"""Shared helpers for the harness v2 functional tests (plain Python, no pytest)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import traceback
import uuid
from pathlib import Path

PY = sys.executable
SRC = Path(__file__).resolve().parent.parent
TOOLS = SRC / "tools"
TMP = Path(r"D:\tmp\harness_v2_tests")


class R:
    def __init__(self, cp):
        self.rc, self.out, self.err = cp.returncode, cp.stdout or "", cp.stderr or ""

    @property
    def all(self):
        return self.out + self.err


# Tests must never write into the operator's real metrics/state folders (S8-T1: test rows had polluted ~/.harness-hooks/metrics).
SINK = TMP / "sink"
os.environ["HARNESS_METRICS_DIR"] = str(SINK / "metrics")  # in-process hook calls
os.environ.setdefault("HARNESS_HOOK_STATE", str(SINK / "state"))


def base_env(extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARNESS_")}
    env["HARNESS_METRICS_DIR"] = str(SINK / "metrics")  # default for hook subprocesses; a test's own `extra` still wins
    env["HARNESS_HOOK_STATE"] = str(SINK / "state")
    env["PYTHONIOENCODING"] = "utf-8"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.update(extra or {})
    return env


def run(cmd, cwd, env=None, inp=None):
    kw = {"input": inp} if inp is not None else {"stdin": subprocess.DEVNULL}
    return R(subprocess.run(cmd, cwd=str(cwd), env=base_env(env), capture_output=True, text=True,
                            encoding="utf-8", errors="replace", **kw))


def git(repo, *args, env=None, inp=None):
    return run(["git", *args], repo, env, inp)


def fresh(name):
    p = TMP / f"{name}-{uuid.uuid4().hex[:8]}"
    p.mkdir(parents=True)
    return p


def mk_repo(name, with_commit=True):
    p = fresh(name)
    assert git(p, "init", "-q", "-b", "main").rc == 0
    git(p, "config", "user.name", "t")
    git(p, "config", "user.email", "t@t")
    git(p, "config", "core.autocrlf", "false")
    if with_commit:
        commit(p, "README.md", "hello\n", "init")
    return p


def commit(repo, rel, content, msg, *more_paras, env=None):
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content, encoding="utf-8", newline="\n")
    assert git(repo, "add", rel).rc == 0
    args = ["commit", "-q", "-m", msg]
    for m in more_paras:
        args += ["-m", m]
    r = git(repo, *args, env=env)
    assert r.rc == 0, r.all
    return git(repo, "rev-parse", "HEAD").out.strip()


def tool(repo, script, *args, env=None, inp=None):
    return run([PY, "-I", str(TOOLS / script), *args], repo, env, inp)


def ledger(repo, *args, env=None, inp=None):
    return tool(repo, "ledger.py", *args, env=env, inp=inp)


def head(repo):
    return git(repo, "rev-parse", "HEAD").out.strip()


def load_ledger(repo):
    return json.loads((repo / ".harness" / "ledger.json").read_text(encoding="utf-8"))


def task(repo, tid):
    for s in load_ledger(repo)["sprints"]:
        for t in s["tasks"]:
            if t["id"] == tid:
                return t
    raise KeyError(tid)


def setup_tasks(repo, n=2, risk="local"):
    assert ledger(repo, "sprint-add", "--id", "S1", "--title", "Sprint one").rc == 0
    for i in range(n):
        r = ledger(repo, "task-add", "--title", f"task {i + 1}", "--risk", risk)
        assert r.rc == 0, r.all


def diff_text(files, body="+x = 1"):
    out = []
    for f in files:
        out.append(f"diff --git a/{f} b/{f}\n--- a/{f}\n+++ b/{f}\n@@ -0,0 +1 @@\n{body}\n")
    return "".join(out)


def route(repo, files, body="+x = 1", *args, env=None):
    d = repo / "t.diff"
    d.write_text(diff_text(files, body), encoding="utf-8", newline="\n")
    r = tool(repo, "route.py", "--diff", str(d), *args, env=env)
    return r, (json.loads(r.out) if r.rc == 0 and r.out.strip().startswith("{") else None)


def agents(res):
    return [x["agent"] for x in res["reviewers"]]


def run_all(g):
    fails = 0
    for name in [n for n in g if n.startswith("test_")]:
        try:
            g[name]()
            print(f"PASS {name}")
        except BaseException:
            fails += 1
            print(f"FAIL {name}")
            traceback.print_exc(limit=4, file=sys.stdout)
    print(f"{'FAILED' if fails else 'OK'}: {fails} failing")
    return 1 if fails else 0


def copy_source(name):
    dst = fresh(name) / "src"
    shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(".git", "out", "tests", "__pycache__", ".harness"))
    return dst
