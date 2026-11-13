"""S19-T4 tests (B40, B41): harness/hooks/session_close_gate.py finished-agents cache and notification parsing. Written from
the S19-T4 acceptance criteria, not from the code.
1. Two concurrent writers (real processes) never lose an id: the union of their disjoint ids is on disk.
2. Merge keeps the newer time per id and drops resumed (removed) ids.
3. Unchanged content means no write (file mtime stays).
4. Lock held by another holder: nothing is written and a lock-timeout LOG line appears.
5. A corrupt cache (bad JSON, a list, non-numeric values) logs 'corrupt' and counts as empty.
6. A <task-notification> block with no <summary>/<status> logs 'ignored-no-summary-or-status' and changes nothing.
LOG lines are JSON lines of session_close_gate.LOG (event, verdict, ...). The module computes its paths at import, so the state
folder is set through HARNESS_HOOK_STATE before the import and FINISHED / LOG are re-pointed per test to a fresh temp folder.
Run: C:\\Python314\\python.exe -I -s tests/test_s19_session_close.py"""
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_s = importlib.util.spec_from_file_location("hv2", Path(__file__).resolve().parent / "hv2.py")
h = importlib.util.module_from_spec(_s)
sys.modules["hv2"] = h
_s.loader.exec_module(h)

HOOKS = h.SRC / "harness" / "hooks"
_boot = h.fresh("s19t4_boot")
os.environ["HARNESS_HOOK_STATE"] = str(_boot)  # before the import: never the real ~/.harness-hooks
sys.path.insert(0, str(HOOKS))
import session_close_gate as g  # noqa: E402

assert str(g.FINISHED).startswith(str(_boot)), g.FINISHED


def _fresh_paths(name):
    d = h.fresh(name)
    g.FINISHED = d / "finished_agents.json"
    g.LOG = d / "session_close_gate.jsonl"
    return d


def _log():
    if not g.LOG.exists():
        return []
    return [json.loads(x) for x in g.LOG.read_text(encoding="utf-8").splitlines() if x.strip()]


def _verdicts(event="finished-cache"):
    return [r.get("verdict") for r in _log() if r.get("event") == event]


def _disk():
    return json.loads(g.FINISHED.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 1: concurrent writers
WRITER = ("import sys,time,json;sys.path.insert(0,sys.argv[1]);import session_close_gate as g;"
          "w=sys.argv[2];t0=float(sys.argv[3]);n=int(sys.argv[4])\n"
          "while time.time()<t0:time.sleep(0.001)\n"
          "print(json.dumps([g._save_finished({f'{w}-{i}':1000.0+i}) for i in range(n)]))")


def test_two_concurrent_writer_processes_never_lose_an_id():
    st = h.fresh("s19t4_conc")
    env = {"HARNESS_HOOK_STATE": str(st), "HARNESS_HOOK_CONFIG": str(st / "no-config.json")}
    n = 25
    t0 = time.time() + 4.0  # both children import first, then start writing at the same instant
    full_env = h.base_env(env)
    procs = [subprocess.Popen([h.PY, "-I", "-s", "-c", WRITER, str(HOOKS), w, str(t0), str(n)], env=full_env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for w in ("A", "B")]
    outs = [p.communicate(timeout=120) for p in procs]
    assert [p.returncode for p in procs] == [0, 0], outs
    results = {w: json.loads(o[0]) for w, o in zip(("A", "B"), outs)}
    fin = st / "session_close" / "finished_agents.json"
    disk = json.loads(fin.read_text(encoding="utf-8"))
    for w, res in results.items():
        for i, ok in enumerate(res):
            if ok:  # a save that answered True must be on disk: a lost update would drop it
                assert f"{w}-{i}" in disk, (w, i, sorted(disk))
    assert sum(sum(r) for r in results.values()) >= n, results  # not a vacuous pass: most writes did happen
    assert len(disk) == sum(sum(r) for r in results.values()), (len(disk), results)  # nothing extra, nothing lost


def test_two_concurrent_writers_in_threads_never_lose_an_id():
    import threading
    _fresh_paths("s19t4_thr")
    res = {}

    def work(w):
        res[w] = [g._save_finished({f"{w}-{i}": 1000.0 + i}) for i in range(15)]
    ts = [threading.Thread(target=work, args=(w,)) for w in ("A", "B")]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    disk = _disk()
    for w, r in res.items():
        for i, ok in enumerate(r):
            if ok:
                assert f"{w}-{i}" in disk, (w, i)
    assert len(disk) == sum(sum(r) for r in res.values()) and len(disk) >= 15, (len(disk), res)


# ---------------------------------------------------------------- 2: merge
def test_merge_keeps_the_newer_time_and_drops_resumed_ids():
    _fresh_paths("s19t4_merge")
    g.FINISHED.write_text(json.dumps({"a": 100.0, "b": 200.0, "r": 50.0}), encoding="utf-8")
    assert g._save_finished({"a": 300.0, "b": 150.0, "c": 400.0}, removed=("r",)) is True
    assert _disk() == {"a": 300.0, "b": 200.0, "c": 400.0}, _disk()


def test_removed_id_is_dropped_even_when_the_caller_still_holds_it():
    _fresh_paths("s19t4_resume")
    g.FINISHED.write_text(json.dumps({"r": 50.0, "k": 60.0}), encoding="utf-8")
    assert g._save_finished({"r": 50.0, "k": 60.0}, removed={"r"}) is True
    assert _disk() == {"k": 60.0}, _disk()


def test_save_into_a_missing_file_creates_it():
    _fresh_paths("s19t4_new")
    assert not g.FINISHED.exists()
    assert g._save_finished({"x": 5.0}) is True
    assert _disk() == {"x": 5.0}


def test_only_the_500_newest_ids_are_kept():
    _fresh_paths("s19t4_cap")
    assert g._save_finished({f"id{i}": float(i) for i in range(1, 506)}) is True
    d = _disk()
    assert len(d) == 500 and "id5" not in d and "id6" in d and "id505" in d, len(d)


# ---------------------------------------------------------------- 3: unchanged content, no write
def test_unchanged_content_does_not_write_the_file():
    _fresh_paths("s19t4_same")
    g.FINISHED.write_text(json.dumps({"a": 100.0, "b": 200.0}), encoding="utf-8")
    old = 1_000_000_000
    os.utime(g.FINISHED, (old, old))
    before = g.FINISHED.stat().st_mtime_ns
    assert g._save_finished({"a": 100.0}) is True  # a subset with the same time: nothing new
    assert g._save_finished({"a": 50.0, "b": 200.0}) is True  # older times lose against the disk: nothing new
    assert g.FINISHED.stat().st_mtime_ns == before, "the file was rewritten although nothing changed"
    assert _disk() == {"a": 100.0, "b": 200.0}


def test_changed_content_does_write_the_file_twin():
    _fresh_paths("s19t4_diff")
    g.FINISHED.write_text(json.dumps({"a": 100.0}), encoding="utf-8")
    old = 1_000_000_000
    os.utime(g.FINISHED, (old, old))
    before = g.FINISHED.stat().st_mtime_ns
    assert g._save_finished({"a": 100.0, "n": 1.0}) is True
    assert g.FINISHED.stat().st_mtime_ns != before and "n" in _disk()


# ---------------------------------------------------------------- 4: lock held by another holder
def test_lock_held_by_another_holder_writes_nothing_and_logs():
    _fresh_paths("s19t4_lock")
    g.FINISHED.write_text(json.dumps({"a": 100.0}), encoding="utf-8")
    lock = Path(str(g.FINISHED) + ".lock")
    lock.write_text("99999", encoding="utf-8")  # fresh: younger than the 10 s stale limit
    t0 = time.time()
    assert g._save_finished({"z": 999.0}) is False
    assert time.time() - t0 < 8, "the writer must give up after its short wait"
    assert _disk() == {"a": 100.0}, _disk()
    assert "lock-timeout" in _verdicts(), _log()
    assert lock.exists(), "a lock this writer did not take must not be removed"


def test_lock_held_and_no_file_yet_creates_no_file():
    _fresh_paths("s19t4_lock2")
    Path(str(g.FINISHED) + ".lock").write_text("99999", encoding="utf-8")
    assert g._save_finished({"z": 1.0}) is False
    assert not g.FINISHED.exists()


def test_free_lock_twin_writes():
    _fresh_paths("s19t4_free")
    assert g._save_finished({"z": 1.0}) is True and _disk() == {"z": 1.0}
    assert "lock-timeout" not in _verdicts()
    assert not Path(str(g.FINISHED) + ".lock").exists(), "the lock must be released"


# ---------------------------------------------------------------- 5: corrupt cache
def _corrupt_case(text, name):
    _fresh_paths(name)
    g.FINISHED.write_text(text, encoding="utf-8")
    got = g._load_finished()
    assert got == {}, (text, got)
    assert "corrupt" in _verdicts(), (text, _log())


def test_corrupt_cache_bad_json_logs_and_is_empty():
    _corrupt_case("{not json", "s19t4_c1")


def test_corrupt_cache_a_list_logs_and_is_empty():
    _corrupt_case(json.dumps(["a", "b"]), "s19t4_c2")


def test_corrupt_cache_non_numeric_value_logs_and_is_empty():
    _corrupt_case(json.dumps({"a": "yesterday"}), "s19t4_c3")


def test_corrupt_cache_null_value_logs_and_is_empty():
    _corrupt_case(json.dumps({"a": None}), "s19t4_c4")


def test_corrupt_cache_a_scalar_logs_and_is_empty():
    _corrupt_case("42", "s19t4_c5")


def test_valid_cache_loads_and_does_not_log_twin():
    _fresh_paths("s19t4_ok")
    g.FINISHED.write_text(json.dumps({"a": 1.5, "b": 2}), encoding="utf-8")
    assert g._load_finished() == {"a": 1.5, "b": 2.0}
    assert _log() == []


def test_missing_cache_is_empty_without_a_log_line():
    _fresh_paths("s19t4_miss")
    assert g._load_finished() == {} and _log() == []


def test_a_corrupt_cache_is_replaced_by_the_next_save():
    _fresh_paths("s19t4_heal")
    g.FINISHED.write_text("{broken", encoding="utf-8")
    assert g._save_finished({"x": 7.0}) is True
    assert _disk() == {"x": 7.0}


# ---------------------------------------------------------------- 6: notification blocks
def _transcript(d, rows):
    p = d / "t.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return str(p)


def _ts(ago=60):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - ago))  # transcript stamps are UTC


def _launch(aid):
    return {"timestamp": _ts(120), "message": {"content": [
        {"type": "tool_result", "content": f"Async agent launched successfully. agentId: {aid}"}]}}


def _notif(body, ago=30):
    return {"timestamp": _ts(ago), "message": {"role": "user", "content": body}}


def test_tagless_notification_block_logs_ignored_and_changes_nothing():
    d = _fresh_paths("s19t4_n1")
    tp = _transcript(d, [_launch("ag1"), _notif("<task-notification><task-id>ag1</task-id>done, no tags</task-notification>")])
    aged = g.pending_agents_aged(tp)
    logs = [r for r in _log() if r.get("event") == "task-notification"]
    assert [r.get("verdict") for r in logs] == ["ignored-no-summary-or-status"], _log()
    assert logs[0].get("id") == "ag1", logs
    assert [a for a, _ in aged] == ["ag1"], aged  # the malformed block did not finish the agent
    assert not g.FINISHED.exists() or "ag1" not in _disk()


def test_notification_with_summary_finishes_the_agent_twin():
    d = _fresh_paths("s19t4_n2")
    tp = _transcript(d, [_launch("ag1"),
                         _notif("<task-notification><task-id>ag1</task-id><summary>done</summary></task-notification>")])
    assert g.pending_agents_aged(tp) == []
    assert "ignored-no-summary-or-status" not in _verdicts("task-notification")
    assert "ag1" in _disk()


def test_notification_with_status_only_counts_as_a_tagged_block():
    d = _fresh_paths("s19t4_n3")
    tp = _transcript(d, [_launch("ag2"),
                         _notif("<task-notification><task-id>ag2</task-id><status>completed</status></task-notification>")])
    assert g.pending_agents_aged(tp) == []
    assert _verdicts("task-notification") == []


def test_tagless_block_without_task_id_is_logged_with_an_empty_id():
    d = _fresh_paths("s19t4_n4")
    tp = _transcript(d, [_notif("<task-notification>nothing useful</task-notification>")])
    g.pending_agents_aged(tp)
    logs = [r for r in _log() if r.get("event") == "task-notification"]
    assert len(logs) == 1 and logs[0]["verdict"] == "ignored-no-summary-or-status" and logs[0]["id"] == "", logs


def test_tagless_block_does_not_hide_a_following_tagged_block():
    d = _fresh_paths("s19t4_n5")
    body = ("<task-notification><task-id>bad</task-id>x</task-notification>"
            "<task-notification><task-id>ok1</task-id><summary>done</summary></task-notification>")
    tp = _transcript(d, [_launch("bad"), _launch("ok1"), _notif(body)])
    aged = g.pending_agents_aged(tp)
    assert [a for a, _ in aged] == ["bad"], aged
    assert _verdicts("task-notification") == ["ignored-no-summary-or-status"]


if __name__ == "__main__":
    sys.exit(h.run_all(globals()))
