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


# ---------- GPT-PM S8 round 1: F1 idempotent release row (rid), fault injection ----------
import ast  # noqa: E402
import hashlib  # noqa: E402
import threading  # noqa: E402

sys.path.insert(0, str(HERE.parent / "harness" / "hooks"))
import hook_metrics as hm  # noqa: E402


class use_metrics:
    """Point in-process hook_metrics at a fresh temp folder; restore the sink afterwards."""

    def __init__(self, name):
        self.d = hv2.fresh(name) / "metrics"

    def __enter__(self):
        self.old = os.environ.get("HARNESS_METRICS_DIR")
        os.environ["HARNESS_METRICS_DIR"] = str(self.d)
        return self.d

    def __exit__(self, *a):
        os.environ["HARNESS_METRICS_DIR"] = self.old


def all_rows(d):
    f = d / "events.jsonl"
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()] if f.is_file() else []


def test_row_shape_has_rid_as_eleventh_field_defaulting_to_empty():
    assert len(hm.FIELDS) == 11 and hm.FIELDS[-1] == "rid"
    with use_metrics("s8shape") as d:
        assert hm.record("h.py", "Stop", "allow") is True
        r = all_rows(d)[0]
    assert tuple(r) == hm.FIELDS and r["rid"] == "", r


def test_record_same_rid_twice_writes_one_row_and_returns_true_both_times():
    with use_metrics("s8rid1") as d:
        assert hm.record("h.py", "Stop", "release", rid="abc123") is True
        assert hm.record("h.py", "Stop", "release", rid="abc123") is True
        rows = all_rows(d)
    assert len(rows) == 1 and rows[0]["rid"] == "abc123", rows


def test_record_different_rid_writes_two_rows():
    with use_metrics("s8rid2") as d:
        assert hm.record("h.py", "Stop", "release", rid="a1") is True
        assert hm.record("h.py", "Stop", "release", rid="b2") is True
        assert [r["rid"] for r in all_rows(d)] == ["a1", "b2"]


def test_record_empty_rid_never_dedupes():
    with use_metrics("s8rid3") as d:
        for _ in range(3):
            assert hm.record("h.py", "Stop", "allow") is True
        assert len(all_rows(d)) == 3


def test_record_rid_is_cut_to_40_chars_and_still_dedupes():
    long_rid = "x" * 80
    with use_metrics("s8rid4") as d:
        assert hm.record("h.py", "Stop", "release", rid=long_rid) is True
        assert hm.record("h.py", "Stop", "release", rid=long_rid) is True
        rows = all_rows(d)
    assert len(rows) == 1 and len(rows[0]["rid"]) <= 40, rows


def _released_with_metrics_down(name):
    """Drive a streak to the release Stop while the metrics lock is held by someone else. Returns (ctx, metrics dir, lock)."""
    c = lsg.Ctx(name, [lsg.mk_task("T1", "todo")])
    d = c.root / "metrics"
    d.mkdir()
    lock = d / "events.jsonl.lock"
    for i in range(5):
        assert lsg.is_block(stop(c, d)), i
    lock.write_text("99999", encoding="utf-8")  # fresh foreign lock: record() drops the row and returns False
    p = stop(c, d)
    assert not lsg.is_block(p), (p.stdout, p.stderr)
    return c, d, lock


def test_release_with_metrics_failure_leaves_no_row_and_no_metric_ok_then_recovers_with_exactly_one_row():
    c, d, lock = _released_with_metrics_down("s8fault1")
    assert all_rows(d) == []
    st = c.streak()
    assert st.get("released") is True and not st.get("metric_ok"), st
    assert not lsg.is_block(stop(c, d)) and all_rows(d) == [] and not c.streak().get("metric_ok")  # still down
    lock.unlink()  # recovery
    assert not lsg.is_block(stop(c, d))
    rel = mrows(c, d)
    assert len(rel) == 1 and c.streak().get("metric_ok") is True, (rel, c.streak())
    for _ in range(2):
        assert not lsg.is_block(stop(c, d))
    assert len(mrows(c, d)) == 1, mrows(c, d)


def test_release_when_metrics_path_is_unusable_keeps_decision_and_retries_later():
    c = lsg.Ctx("s8fault2", [lsg.mk_task("T1", "todo")])
    d = c.root / "metrics"
    d.write_text("not a folder", encoding="utf-8")  # record() cannot create the folder: returns False, no lock involved
    for i in range(5):
        assert lsg.is_block(stop(c, d)), i
    p = stop(c, d)
    assert not lsg.is_block(p) and p.returncode == 0, (p.stdout, p.stderr)
    assert not c.streak().get("metric_ok") and c.streak().get("released") is True
    d.unlink()
    assert not lsg.is_block(stop(c, d))
    assert len(mrows(c, d)) == 1 and c.streak().get("metric_ok") is True


def test_crash_after_row_written_but_state_not_saved_does_not_duplicate_the_row():
    c, d, lock = _released_with_metrics_down("s8crash")
    key, st = list(json.loads(c.state_file().read_text(encoding="utf-8"))["ledger"].items())[0]
    assert not st.get("metric_ok")
    rid = hashlib.sha1(f"{key}|{st['first']}".encode("utf-8")).hexdigest()[:16]  # spec formula: sha1(key|streak first)[:16]
    lock.unlink()
    old = os.environ.get("HARNESS_METRICS_DIR")
    os.environ["HARNESS_METRICS_DIR"] = str(d)
    try:  # the process "crashed" right after the row landed: state still says metric_ok unset
        assert hm.record("session_close_gate.py", "Stop", "release", "ledger-no-progress-blocks", rid=rid) is True
    finally:
        os.environ["HARNESS_METRICS_DIR"] = old
    assert len(all_rows(d)) == 1 and all_rows(d)[0]["rid"] == rid
    assert not lsg.is_block(stop(c, d))
    assert len(all_rows(d)) == 1, all_rows(d)
    assert c.streak().get("metric_ok") is True, "a dedupe hit counts as recorded"
    assert not lsg.is_block(stop(c, d)) and len(all_rows(d)) == 1


def test_release_row_rid_follows_the_spec_formula():
    c = lsg.Ctx("s8ridf", [lsg.mk_task("T1", "todo")])
    d = c.root / "metrics"
    for _ in range(6):
        stop(c, d)
    key, st = list(json.loads(c.state_file().read_text(encoding="utf-8"))["ledger"].items())[0]
    want = hashlib.sha1(f"{key}|{st['first']}".encode("utf-8")).hexdigest()[:16]
    assert [r["rid"] for r in all_rows(d)] == [want]


def test_two_concurrent_stops_of_a_released_streak_write_exactly_one_row():
    for n in range(3):
        c, d, lock = _released_with_metrics_down("s8conc%d" % n)
        lock.unlink()
        gate = threading.Barrier(2)
        res = []

        def go():
            gate.wait()
            res.append(stop(c, d))

        ts = [threading.Thread(target=go) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert len(res) == 2 and all(not lsg.is_block(p) for p in res), [(p.stdout, p.stderr) for p in res]
        assert len(mrows(c, d)) == 1, (n, all_rows(d))


# ---------- F2: no corrupted (TAB) sink path literals in the four gate tests ----------
def _metrics_dir_exprs(src):
    """Yield (lineno, source of the value expression) for every assignment / setdefault of HARNESS_METRICS_DIR."""
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Subscript):
            k = node.targets[0].slice
            if isinstance(k, ast.Constant) and k.value == "HARNESS_METRICS_DIR":
                yield node.lineno, ast.get_source_segment(src, node.value)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "setdefault" and len(node.args) == 2:
            k = node.args[0]
            if isinstance(k, ast.Constant) and k.value == "HARNESS_METRICS_DIR":
                yield node.lineno, ast.get_source_segment(src, node.args[1])


def test_sink_paths_in_four_gate_tests_have_no_tab_and_are_absolute():
    import tempfile
    for name in ("test_ask_last_gate.py", "test_ledger_stop_gate.py", "test_plan_first_gate.py", "test_secret_guard.py"):
        src = (HERE / name).read_text(encoding="utf-8")
        lines = [(i + 1, ln) for i, ln in enumerate(src.splitlines()) if "HARNESS_METRICS_DIR" in ln]
        assert lines, name
        for i, ln in lines:
            assert "\t" not in ln, f"{name}:{i} has a TAB character: {ln!r}"
        exprs = list(_metrics_dir_exprs(src))
        assert exprs, f"{name}: no HARNESS_METRICS_DIR assignment found"
        for i, expr in exprs:
            assert "\t" not in expr, (name, i, expr)
            ns = {"str": str, "os": os, "Path": Path, "hv2": hv2, "tempfile": tempfile, "TMP": Path(tempfile.gettempdir()) / "ask_x"}
            val = str(eval(expr, {"__builtins__": __builtins__, **ns}))
            assert os.path.isabs(val) and "\t" not in val, (name, i, expr, val)
            assert val.count(":") <= 1, (name, i, val)


if __name__ == "__main__":
    raise SystemExit(hv2.run_all(globals()))
