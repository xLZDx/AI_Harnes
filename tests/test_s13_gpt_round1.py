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
    # criterion (df901e8): any quote character in a flow-style on: value is UNPARSABLE, even for workflow_dispatch
    "flow-sq-dispatch": "on: ['workflow_dispatch']\n",
    "map-dq-dispatch": 'on: {"workflow_dispatch": {}}\n',
}
PASS = {
    "scalar-dispatch": 'on: "workflow_dispatch"\n',
    "flow-plain-dispatch": "on: [workflow_dispatch]\n",
    "map-plain-dispatch": "on: {workflow_dispatch: {}}\n",
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
    exp = {"scalar-dq": {"push"}, "block-dq-key": {"pull_request"}, "block-sq-key": {"push"}}
    for k, want in exp.items():
        got = wg.triggers("name: ci\n" + REFUSE[k] + TAIL)
        if got != want:
            fails.append(f"{k}: triggers()={got}, expected {want}")
    # flow-style values (and a quoted on key) holding any quote are UNPARSABLE as a whole (criterion df901e8)
    for k in ("flow-dq-pair", "flow-sq", "map-dq-key", "quoted-on-key", "flow-sq-dispatch", "map-dq-dispatch"):
        got = wg.triggers("name: ci\n" + REFUSE[k] + TAIL)
        if got != {wg.UNPARSABLE}:
            fails.append(f"{k}: triggers()={got}, expected only UNPARSABLE")
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


# OS-lock contract (core/DECISION_LOG.md 'S13 GPT-PM round 2'): the kernel frees the lock when the holder exits; no takeover; the
# lock file is never deleted; <plan>.lock.owner is diagnostic only.
WAITER = (  # takes the lock, prints 'held', then waits for argv[3] (an event file) to appear, releases, prints 'released'
    "import sys, time\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import plan_import_ledger_md as pl\n"
    "ev = Path(sys.argv[3])\n"
    "with pl.plan_lock(Path(sys.argv[2])):\n"
    "    print('held', flush=True)\n"
    "    end = time.time() + 120\n"
    "    while not ev.exists() and time.time() < end:\n"
    "        time.sleep(0.05)\n"
    "print('released', flush=True)\n"
)
PROBE = (  # one acquire attempt with timeout argv[3]: rc 0 'acquired', rc 3 'timeout' (message on stderr)
    "import sys\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import plan_import_ledger_md as pl\n"
    "try:\n"
    "    with pl.plan_lock(Path(sys.argv[2]), timeout=float(sys.argv[3])):\n"
    "        print('acquired', flush=True)\n"
    "except TimeoutError as e:\n"
    "    print(str(e), file=sys.stderr, flush=True)\n"
    "    sys.exit(3)\n"
)
RACER = (  # argv: tools plan logfile n; appends 'enter <pid> <ns>' / 'leave <pid> <ns>' lines while holding the lock
    "import os, sys, time\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import plan_import_ledger_md as pl\n"
    "log = sys.argv[3]\n"
    "def put(kind):\n"
    "    with open(log, 'a', encoding='utf-8') as f:\n"
    "        f.write(f'{kind} {os.getpid()} {time.time_ns()}\\n')\n"
    "for _ in range(int(sys.argv[4])):\n"
    "    with pl.plan_lock(Path(sys.argv[2]), timeout=60):\n"
    "        put('enter')\n"
    "        time.sleep(0.01)\n"
    "        put('leave')\n"
)


def script(name, body):
    s = hv2.fresh(name) / "s.py"
    s.write_text(body, encoding="utf-8")
    return s


def start_waiter(plan, event):
    p = subprocess.Popen([PY, "-s", str(script("r2-waiter", WAITER)), str(hv2.TOOLS), str(plan), str(event)],
                         stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


def start_probe(plan, timeout):
    return subprocess.Popen([PY, "-s", str(script("r2-probe", PROBE)), str(hv2.TOOLS), str(plan), str(timeout)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def kill(p):
    p.kill()
    p.wait(timeout=20)


def test_lock_live_holder_times_out_second_process_with_lock_path_then_free_after_kill():
    plan, lock = plan_path("r2-lock-live")
    a = start_holder(plan)
    try:
        pr = start_probe(plan, 0.5)
        out, err = pr.communicate(timeout=30)
        assert pr.returncode == 3 and "acquired" not in out, (pr.returncode, out, err)
        assert str(lock) in err, err
        assert a.poll() is None
        # in-process contender too (age of the file is irrelevant)
        backdate(lock)
        try:
            with pl.plan_lock(plan, timeout=0.5):
                raise AssertionError("acquired a lock held by a live process")
        except TimeoutError as e:
            assert str(lock) in str(e), str(e)
        assert lock.is_file()
        kill(a)
        t0 = time.monotonic()
        pr2 = start_probe(plan, 20)
        out2, err2 = pr2.communicate(timeout=30)
        assert pr2.returncode == 0 and out2.strip() == "acquired", (out2, err2)
        assert time.monotonic() - t0 < 15
    finally:
        a.kill()


def test_lock_released_by_kernel_at_once_after_holder_killed_in_process_acquirer():
    plan, lock = plan_path("r2-lock-kill")
    a = start_holder(plan)
    kill(a)
    t0 = time.monotonic()
    with pl.plan_lock(plan, timeout=5.0):
        pass
    assert time.monotonic() - t0 < 3.0, "acquire after the holder died was not immediate"


def test_lock_release_does_not_delete_the_lock_file_and_it_can_be_reacquired():
    plan, lock = plan_path("r2-lock-keep")
    with pl.plan_lock(plan, timeout=2.0):
        assert lock.is_file()
    assert lock.is_file(), "release deleted the lock file"
    with pl.plan_lock(plan, timeout=2.0):
        pass
    assert lock.is_file()


def test_lock_leftover_json_lock_file_of_old_scheme_does_not_block():
    fails = []
    live = subprocess.Popen([PY, "-s", "-c", SLEEPER])
    try:
        bodies = {
            "dead-pid": json.dumps({"token": "dead", "pid": 2 ** 22 + 7, "start": ""}),
            "live-pid-matching-start": json.dumps({"token": "t1", "pid": live.pid, "start": pl._proc_start(live.pid)}),
            "garbage": "not json at all",
            "empty": "",
            "json-list": "[1,2]",
        }
        for k, body in bodies.items():
            plan, lock = plan_path("r2-lock-old-" + k)
            lock.write_text(body, encoding="utf-8")
            backdate(lock, 100000 if k == "garbage" else 5)
            try:
                with pl.plan_lock(plan, timeout=3.0):
                    pass
            except TimeoutError as e:
                fails.append(f"{k}: leftover lock file blocked: {e}")
            if not lock.is_file():
                fails.append(f"{k}: lock file was deleted")
        assert live.poll() is None, "the unrelated live process must not be touched"
    finally:
        live.kill()
    check(fails)


def test_gpt_scenario_a_killed_b_holds_c_must_not_acquire_until_b_releases():
    plan, lock = plan_path("r2-gpt")
    event = lock.parent / "release.evt"
    a = start_holder(plan)
    kill(a)
    b = start_waiter(plan, event)  # B acquires at once after A died
    c = None
    try:
        c = start_probe(plan, 60)  # C waits with a long timeout
        time.sleep(1.5)
        assert c.poll() is None, "C acquired (or died) while B holds the lock"
        assert b.poll() is None
        third = start_probe(plan, 0.5)  # independent probe: B must still hold
        out3, err3 = third.communicate(timeout=30)
        assert third.returncode == 3 and str(lock) in err3, (third.returncode, out3, err3)
        assert c.poll() is None
        event.write_text("go", encoding="utf-8")  # B releases
        outc, errc = c.communicate(timeout=30)
        assert c.returncode == 0 and outc.strip() == "acquired", (c.returncode, outc, errc)
        assert b.wait(timeout=30) == 0
    finally:
        for p in (a, b, c):
            if p is not None and p.poll() is None:
                p.kill()


def test_lock_twenty_rounds_of_two_racers_show_zero_overlap():
    racer = script("r2-racer", RACER)
    fails = []
    for rnd in range(20):
        plan, lock = plan_path(f"r2-race{rnd}")
        log = lock.parent / "race.log"
        procs = [subprocess.Popen([PY, "-s", str(racer), str(hv2.TOOLS), str(plan), str(log), "5"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        outs = [p.communicate(timeout=120) for p in procs]
        if [p.returncode for p in procs] != [0, 0]:
            fails.append(f"round {rnd}: racer failed {outs}")
            continue
        recs = [l.split() for l in log.read_text(encoding="utf-8").splitlines()]
        if len(recs) != 20:
            fails.append(f"round {rnd}: expected 20 records, got {len(recs)}")
            continue
        # file order: strictly enter,leave,enter,leave with the same pid inside a pair
        for i in range(0, len(recs), 2):
            (k1, p1, _), (k2, p2, _) = recs[i], recs[i + 1]
            if (k1, k2) != ("enter", "leave") or p1 != p2:
                fails.append(f"round {rnd}: interleaved critical sections at record {i}: {recs[i:i + 2]}")
                break
        # timestamps: intervals sorted by start must not overlap
        iv = sorted((int(recs[i][2]), int(recs[i + 1][2])) for i in range(0, len(recs), 2))
        for (s1, e1), (s2, e2) in zip(iv, iv[1:]):
            if s2 < e1:
                fails.append(f"round {rnd}: overlap {iv}")
                break
    check(fails)


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
