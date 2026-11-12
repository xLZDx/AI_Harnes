"""S8 tests: (T1) test isolation from the real ~/.harness-hooks, (T2) agent rows out of hook latency, one release row per streak.
Run: C:\\Python314\\python.exe -I tests/test_s8_metrics.py"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools"))
import hv2  # noqa: E402
import metrics  # noqa: E402
import test_ledger_stop_gate as lsg  # noqa: E402  (importing it must keep the sink env too)

SINK = Path(r"D:\tmp\harness_v2_tests\sink")


# ---------- T1: isolation ----------
def test_base_env_contains_sink_paths_and_extra_overrides_them():
    e = hv2.base_env()
    assert Path(e["HARNESS_METRICS_DIR"]) == SINK / "metrics", e["HARNESS_METRICS_DIR"]
    assert Path(e["HARNESS_HOOK_STATE"]) == SINK / "state", e["HARNESS_HOOK_STATE"]
    e2 = hv2.base_env({"HARNESS_METRICS_DIR": "X:\\own", "HARNESS_HOOK_STATE": "X:\\own2"})
    assert e2["HARNESS_METRICS_DIR"] == "X:\\own" and e2["HARNESS_HOOK_STATE"] == "X:\\own2"


def test_base_env_replaces_inherited_harness_vars(monkeypatch=None):
    old = os.environ.get("HARNESS_METRICS_DIR")
    os.environ["HARNESS_METRICS_DIR"] = "Z:\\elsewhere"
    try:
        assert Path(hv2.base_env()["HARNESS_METRICS_DIR"]) == SINK / "metrics"
    finally:
        os.environ["HARNESS_METRICS_DIR"] = old


def test_importing_hv2_sets_process_metrics_dir_to_sink():
    r = subprocess.run([sys.executable, "-I", "-c",
                        "import sys,os; sys.path.insert(0, r'%s'); import hv2; print(os.environ['HARNESS_METRICS_DIR'])" % HERE],
                       capture_output=True, text=True, env={k: v for k, v in os.environ.items() if not k.startswith("HARNESS_")})
    assert r.returncode == 0, r.stderr
    assert Path(r.stdout.strip()) == SINK / "metrics", r.stdout


FIXTURE_SESSIONS = {"s1", "A", "sess1"}


def _fixture_rows():
    f = Path.home() / ".harness-hooks" / "metrics" / "events.jsonl"
    n = 0
    if not f.is_file():
        return 0
    for ln in f.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if r.get("session") in FIXTURE_SESSIONS or r.get("project") == "proj" or str(r.get("project", "")).startswith("headchg-"):
            n += 1
    return n


def test_end_to_end_gate_tests_do_not_touch_real_metrics_folder():
    before = _fixture_rows()
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARNESS_")}
    for name in ("test_plan_first_gate.py", "test_ledger_stop_gate.py"):
        r = subprocess.run([sys.executable, "-I", str(HERE / name)], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=900)
        assert "FAILED" not in r.stdout, f"{name} failed itself:\n{r.stdout[-1500:]}"
    after = _fixture_rows()
    assert after == before, f"{after - before} fixture rows leaked into the real metrics log"


# ---------- T2: summarize ----------
def row(hook="h.py", decision="allow", reason="", ms=0, agent="", project="p"):
    return {"hook": hook, "decision": decision, "reason": reason, "ms": ms, "agent": agent, "project": project, "session": "s"}


def test_agent_stop_ms_is_excluded_from_latency_but_counted_fired():
    rows = [row(reason="agent-stop", ms=650000, agent="x"), row(ms=20), row(ms=20)]
    h = metrics.summarize(rows)["hooks"]["h.py"]
    assert h["fired"] == 3 and h["p95_ms"] == 20 and h["p50_ms"] == 20, h


def test_agent_start_is_excluded_from_latency_and_counted():
    rows = [row(reason="agent-start", ms=900000, agent="x"), row(ms=10)]
    r = metrics.summarize(rows)
    assert r["hooks"]["h.py"]["fired"] == 2 and r["hooks"]["h.py"]["p95_ms"] == 10
    assert r["agents"]["x"]["starts"] == 1


def test_agent_runs_table_seconds_is_ms_floor_div_1000():
    rows = [row(reason="agent-start", agent="rev"), row(reason="agent-stop", ms=650999, agent="rev"),
            row(reason="agent-stop", ms=1500, agent="rev")]
    a = metrics.summarize(rows)["agents"]["rev"]
    assert (a["starts"], a["stops"], a["seconds"]) == (1, 2, 650 + 1), a


def test_decisions_unchanged_by_agent_rows():
    rows = [row(decision="allow", reason="agent-stop", ms=5000, agent="a"), row(decision="deny", ms=7), row(decision="release", ms=9)]
    h = metrics.summarize(rows)["hooks"]["h.py"]
    assert (h["fired"], h["allow"], h["deny"], h["release"]) == (3, 1, 1, 1), h
    assert h["p95_ms"] == 9


def test_hook_with_only_agent_rows_has_zero_latency():
    h = metrics.summarize([row(reason="agent-stop", ms=70000, agent="a")])["hooks"]["h.py"]
    assert h["p50_ms"] == 0 and h["p95_ms"] == 0 and h["fired"] == 1


# ---------- T2: one release row per streak ----------
def mrows(c, d):
    f = d / "events.jsonl"
    rows = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()] if f.is_file() else []
    return [r for r in rows if r["decision"] == "release"]


def stop(c, d):
    return c.call(env_extra={"HARNESS_METRICS_DIR": str(d)})


def test_release_writes_exactly_one_row_for_repeated_stops_of_the_same_streak():
    c = lsg.Ctx("s8rel", [lsg.mk_task("T1", "todo")])
    d = c.root / "metrics"
    for i in range(5):
        p = stop(c, d)
        assert lsg.is_block(p), (i, p.stdout, p.stderr)
    assert mrows(c, d) == []
    assert not lsg.is_block(stop(c, d))  # 6th: released
    assert len(mrows(c, d)) == 1, mrows(c, d)
    for _ in range(3):
        p = stop(c, d)
        assert not lsg.is_block(p)
    rel = mrows(c, d)
    assert len(rel) == 1, rel
    assert rel[0]["hook"] == "session_close_gate.py" and rel[0]["reason"] == "ledger-no-progress-blocks", rel


def test_new_streak_released_again_writes_a_second_row():
    c = lsg.Ctx("s8rel2", [lsg.mk_task("T1", "todo")])
    d = c.root / "metrics"
    for _ in range(7):
        stop(c, d)
    assert len(mrows(c, d)) == 1
    c.set_tasks([lsg.mk_task("T1", "in_progress")])  # signature changes = a new streak
    p = stop(c, d)
    assert lsg.is_block(p), "progress must start a fresh blocking streak"
    assert len(mrows(c, d)) == 1
    for _ in range(4):
        stop(c, d)
    assert not lsg.is_block(stop(c, d))
    for _ in range(2):
        stop(c, d)
    assert len(mrows(c, d)) == 2, mrows(c, d)


def test_stop_with_progress_before_release_writes_no_row():
    c = lsg.Ctx("s8rel3", [lsg.mk_task("T1", "todo")])
    d = c.root / "metrics"
    for _ in range(4):
        assert lsg.is_block(stop(c, d))
    c.set_tasks([lsg.mk_task("T1", "in_progress")])
    for _ in range(4):  # n = 1..4 of the new streak, never past 5
        assert lsg.is_block(stop(c, d))
    assert mrows(c, d) == []


def test_release_never_writes_to_the_real_metrics_folder():
    # the gate subprocess in Ctx.call gets no HARNESS_METRICS_DIR unless given; hv2 import must not leave the real one active
    assert os.environ["HARNESS_METRICS_DIR"].startswith(str(SINK))


if __name__ == "__main__":
    raise SystemExit(hv2.run_all(globals()))
