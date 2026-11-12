"""S13 GPT-PM round 1 regression tests (commit a42e102): workflow_guard quoted triggers, rules_gate 'harness installed but ledger
missing', plan_import_ledger_md lock ownership. Written from core/DECISION_LOG.md ('S13 GPT-PM round 1'), not from the code.
Real entry points: workflow_guard.py via the pre-push stdin protocol, rules_gate.py via hook JSON, plan_lock with a real second
process. Temp repos under D:\\tmp\\harness_v2_tests. Every denied input has an allowed twin.
Run: C:\\Python314\\python.exe -I tests/test_s13_gpt_round1.py"""
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
import test_s13_enforcement as enf  # noqa: E402
import plan_import_ledger_md as pl  # noqa: E402
import workflow_guard as wg  # noqa: E402

PY = hv2.PY
TAIL = "jobs:\n  b:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"


def check(failures):
    assert not failures, "\n" + "\n".join(failures)


# ------------------------------------------------------------------ 1. workflow_guard
REFUSE = {
    "scalar-dq": 'on: "push"\n',
    "scalar-sq": "on: 'push'\n",
    "flow-dq-pair": 'on: ["push", "pull_request"]\n',
    "flow-sq": "on: ['push']\n",
    "map-dq-key": 'on: {"push": {branches: [main]}}\n',
    "block-dq-key": 'on:\n  "pull_request":\n    branches: [main]\n',
    "block-sq-key": "on:\n  'push':\n    branches: [main]\n",
    "quoted-on-key": '"on": ["push"]\n',
    "alias": "on: *x\n",
}
PASS = {
    "scalar-dispatch": 'on: "workflow_dispatch"\n',
    "flow-sq-dispatch": "on: ['workflow_dispatch']\n",
    "map-dq-dispatch": 'on: {"workflow_dispatch": {}}\n',
    "block-sq-dispatch": "on:\n  'workflow_dispatch':\n    inputs: {}\n",
}


def guard(name, on_text):
    repo = hv2.mk_repo("r1-wg-" + name)
    sha = hv2.commit(repo, ".github/workflows/ci.yml", "name: ci\n" + on_text + TAIL, "add workflow")
    line = f"refs/heads/main {sha} refs/heads/main {'0' * 40}\n"
    return hv2.run([PY, "-I", str(hv2.TOOLS / "workflow_guard.py")], repo, inp=line)


def test_workflow_guard_refuses_quoted_and_unparsable_triggers():
    fails = []
    for k, t in REFUSE.items():
        r = guard(k, t)
        if r.rc != 1:
            fails.append(f"{k}: accepted rc={r.rc} {t!r} err={r.err!r}")
        elif "push blocked" not in r.err or "ci.yml" not in r.err:
            fails.append(f"{k}: refusal message lacks 'push blocked'/file: {r.err!r}")
    check(fails)


def test_workflow_guard_passes_quoted_workflow_dispatch_only():
    fails = []
    for k, t in PASS.items():
        r = guard(k, t)
        if r.rc != 0:
            fails.append(f"{k}: wrongly refused rc={r.rc} {t!r} err={r.err!r}")
    check(fails)


def test_workflow_guard_triggers_unit():
    fails = []
    for k, t in REFUSE.items():
        got = wg.triggers("name: ci\n" + t + TAIL)
        if not (got & set(wg.BAD)):
            fails.append(f"{k}: triggers()={got} has no BAD member for {t!r}")
    for k, t in PASS.items():
        got = wg.triggers("name: ci\n" + t + TAIL)
        if got != {"workflow_dispatch"}:
            fails.append(f"{k}: triggers()={got}, expected {{'workflow_dispatch'}} for {t!r}")
    # the names themselves must be unquoted (not just flagged as unparsable)
    exp = {"scalar-dq": {"push"}, "flow-dq-pair": {"push", "pull_request"}, "flow-sq": {"push"},
           "map-dq-key": {"push"}, "block-dq-key": {"pull_request"}, "block-sq-key": {"push"}, "quoted-on-key": {"push"}}
    for k, want in exp.items():
        got = wg.triggers("name: ci\n" + REFUSE[k] + TAIL)
        # the flow-map form also yields nested keys/values (branches, main): over-inclusive is safe, so check containment
        ok = got >= want if k == "map-dq-key" else got == want
        if not ok:
            fails.append(f"{k}: triggers()={got}, expected {want}")
    if wg.triggers("name: ci\non: *x\n" + TAIL) != {wg.UNPARSABLE}:
        fails.append("alias is not reported as UNPARSABLE")
    check(fails)


# ------------------------------------------------------------------ 2. rules_gate: ledger missing
def cmds():
    return ["git push", "gh pr create --title t --body b"]


def gate_both(repo):
    out = []
    for c in cmds():
        d, reason, _ = enf.gate(c, "claude", "", repo)
        out.append((c, d, reason))
    return out


def expect_deny(repo, label, fails, need="ledger.json missing"):
    for c, d, reason in gate_both(repo):
        if not d:
            fails.append(f"{label}: NOT denied {c!r}")
        elif need not in reason:
            fails.append(f"{label}: {c!r} denied without {need!r}: {reason[:200]!r}")


def tools_only(name):
    repo = hv2.mk_repo(name)
    (repo / ".harness-tools").mkdir()
    (repo / ".harness-tools" / "ledger.py").write_text("# stub\n", encoding="utf-8")
    return repo


def test_rules_gate_allows_plain_git_repo_without_harness():
    repo = hv2.mk_repo("r1-rg-plain")
    fails = []
    for c, d, reason in gate_both(repo):
        if d:
            fails.append(f"wrongly denied {c!r}: {reason[:200]}")
    check(fails)


def test_rules_gate_denies_tools_without_ledger():
    fails = []
    expect_deny(tools_only("r1-rg-tools"), "tools-only", fails)
    check(fails)


def test_rules_gate_denies_hookspath_only():
    repo = hv2.mk_repo("r1-rg-hooks")
    assert hv2.git(repo, "config", "core.hooksPath", ".harness-tools/githooks").rc == 0
    fails = []
    expect_deny(repo, "hooksPath-only", fails)
    check(fails)


def test_rules_gate_denies_harness_dir_without_ledger():
    repo = hv2.mk_repo("r1-rg-dir")
    (repo / ".harness").mkdir()
    fails = []
    expect_deny(repo, ".harness-dir-only", fails)
    check(fails)


def test_rules_gate_denies_in_worktree_of_tools_only_repo():
    repo = tools_only("r1-rg-wt")
    wt = hv2.fresh("r1-rg-wt-tree") / "wt"
    r = hv2.git(repo, "worktree", "add", "-q", "-b", "wtb", str(wt))
    assert r.rc == 0, r.all
    assert not (wt / ".harness-tools").exists()  # untracked tools live only in the main checkout
    fails = []
    expect_deny(wt, "worktree", fails)
    check(fails)


def test_rules_gate_allows_tools_with_approved_ledger_on_head():
    repo = enf.make_gate_repo("r1-rg-twin", "head", [("verified", False), ("done", False)])
    fails = []
    for c, d, reason in gate_both(repo):
        if d:
            fails.append(f"wrongly denied {c!r}: {reason[:200]}")
    check(fails)


# ------------------------------------------------------------------ 3. plan_lock
HOLDER = (
    "import sys, time\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import plan_import_ledger_md as pl\n"
    "with pl.plan_lock(Path(sys.argv[2])):\n"
    "    print('held', flush=True)\n"
    "    time.sleep(300)\n"
)
SLEEPER = "import time\ntime.sleep(300)\n"


def start_holder(plan):
    script = hv2.fresh("r1-lock-script") / "holder.py"
    script.write_text(HOLDER, encoding="utf-8")
    p = subprocess.Popen([PY, "-s", str(script), str(hv2.TOOLS), str(plan)], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


def plan_path(name):
    d = hv2.fresh(name)
    plan = d / "PROJECT_PLAN.md"
    plan.write_text("# plan\n", encoding="utf-8")
    return plan, plan.with_name(plan.name + ".lock")


def backdate(lock, secs=1000):
    t = time.time() - secs
    os.utime(lock, (t, t))


def test_lock_live_owner_not_taken_over_by_age_then_taken_over_after_kill():
    plan, lock = plan_path("r1-lock-live")
    a = start_holder(plan)
    try:
        owner = json.loads(lock.read_text(encoding="utf-8"))
        assert owner["pid"] == a.pid, (owner, a.pid)
        backdate(lock)
        try:
            with pl.plan_lock(plan, timeout=1.0):
                raise AssertionError("B acquired a lock held by a live owner whose lock file is old")
        except TimeoutError as e:
            msg = str(e)
            assert str(lock) in msg, msg
            assert str(a.pid) in msg, msg
        assert lock.is_file() and json.loads(lock.read_text(encoding="utf-8"))["token"] == owner["token"]
        a.kill()
        a.wait(timeout=20)
        with pl.plan_lock(plan, timeout=5.0):
            cur = json.loads(lock.read_text(encoding="utf-8"))
            assert cur["token"] != owner["token"] and cur["pid"] == os.getpid(), cur
        assert not lock.exists(), "lock must be released after B"
    finally:
        a.kill()


def test_lock_release_does_not_delete_a_lock_written_by_someone_else():
    plan, lock = plan_path("r1-lock-token")
    other = {"token": "someone-else", "pid": os.getpid(), "start": pl._proc_start(os.getpid())}
    with pl.plan_lock(plan, timeout=2.0):
        lock.write_text(json.dumps(other), encoding="utf-8")
    assert lock.is_file(), "A's release deleted a lock it does not own"
    assert json.loads(lock.read_text(encoding="utf-8"))["token"] == "someone-else"
    # twin: an own lock is deleted
    plan2, lock2 = plan_path("r1-lock-token2")
    with pl.plan_lock(plan2, timeout=2.0):
        assert lock2.is_file()
    assert not lock2.exists()


def test_lock_with_live_pid_but_different_creation_time_is_taken_over():
    plan, lock = plan_path("r1-lock-reuse")
    sl = subprocess.Popen([PY, "-s", "-c", SLEEPER])
    try:
        real = pl._proc_start(sl.pid)
        assert real, "cannot read the process start time on this platform"
        # twin first: same live pid with the matching start time must stand
        lock.write_text(json.dumps({"token": "t1", "pid": sl.pid, "start": real}), encoding="utf-8")
        try:
            with pl.plan_lock(plan, timeout=1.0):
                raise AssertionError("took over a lock whose owner is alive with matching start time")
        except TimeoutError as e:
            assert str(sl.pid) in str(e) and str(lock) in str(e), str(e)
        # reused pid: stored creation time differs
        lock.write_text(json.dumps({"token": "t2", "pid": sl.pid, "start": str(int(real) + 12345)}), encoding="utf-8")
        with pl.plan_lock(plan, timeout=5.0):
            assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == os.getpid()
        assert sl.poll() is None, "the unrelated live process must not be touched"
    finally:
        sl.kill()


def test_lock_unparseable_is_never_taken_over():
    fails = []
    for k, body in {"garbage": "not json at all", "empty": "", "json-no-pid": '{"token": "x"}', "json-list": "[1,2]"}.items():
        plan, lock = plan_path("r1-lock-bad-" + k)
        lock.write_text(body, encoding="utf-8")
        backdate(lock, 100000)
        try:
            with pl.plan_lock(plan, timeout=1.0):
                fails.append(f"{k}: acquired over an unparseable lock")
        except TimeoutError as e:
            if str(lock) not in str(e):
                fails.append(f"{k}: message lacks the lock path: {e}")
        if not lock.is_file() or lock.read_text(encoding="utf-8") != body:
            fails.append(f"{k}: the unparseable lock was removed or changed")
    check(fails)


def test_lock_dead_pid_is_taken_over_twin():
    plan, lock = plan_path("r1-lock-dead")
    sl = subprocess.Popen([PY, "-s", "-c", "pass"])
    sl.wait(timeout=30)
    lock.write_text(json.dumps({"token": "dead", "pid": sl.pid, "start": ""}), encoding="utf-8")
    with pl.plan_lock(plan, timeout=5.0):
        assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == os.getpid()


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
