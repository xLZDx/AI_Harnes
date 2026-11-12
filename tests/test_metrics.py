"""S7-T7 tests: run_hook.py (transparent runner + one metrics row), hook_metrics.py (storage, retention, lock) and
tools/metrics.py (report). Every test uses fresh temp folders for HARNESS_METRICS_DIR / HARNESS_HOOK_STATE; the CLI runs with a
temp USERPROFILE so nothing under the real ~/.harness-hooks is read or written. Fixtures use fake secrets only.
Run: C:\\Python314\\python.exe -I tests/test_metrics.py"""
import json
import math
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hv2  # noqa: E402

HOOKS = hv2.SRC / "harness" / "hooks"
METRICS_CLI = hv2.TOOLS / "metrics.py"
PY = hv2.PY
SECRET = "FAKE-SECRET-123"
FIELDS = ("ts", "client", "session", "hook", "event", "decision", "reason", "ms", "project", "agent", "rid")

sys.path.insert(0, str(HOOKS))
import hook_metrics as hm  # noqa: E402


def sandbox(name):
    root = hv2.fresh(name)
    d = {"root": root, "metrics": root / "metrics", "state": root / "state", "home": root / "userhome"}
    d["home"].mkdir()
    return d


def envof(sb):
    return {"HARNESS_METRICS_DIR": str(sb["metrics"]), "HARNESS_HOOK_STATE": str(sb["state"]),
            "HARNESS_HOOK_CONFIG": str(sb["root"] / "none.json"), "CLAUDE_HOOK_STATE_DIR": str(sb["root"] / "dstate"),
            "CLAUDE_CONTROL_PLANE_HOME": str(sb["root"] / "chome"), "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet",
            "USERPROFILE": str(sb["home"]), "HOME": str(sb["home"]), "PYTHONIOENCODING": "utf-8"}


def proc(sb, argv, stdin=b"", cwd=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARNESS_")}
    env.update(envof(sb))
    cp = subprocess.run(argv, input=stdin, capture_output=True, env=env, cwd=str(cwd or sb["root"]), timeout=120)
    return cp.returncode, cp.stdout, cp.stderr


def direct(sb, hooks_dir, script, stdin):
    return proc(sb, [PY, "-s", "-X", "utf8", str(Path(hooks_dir) / script), "--harness-client=claude"], stdin)


def via_runner(sb, hooks_dir, script, stdin):
    return proc(sb, [PY, "-s", "-X", "utf8", str(Path(hooks_dir) / "run_hook.py"), script, "--harness-client=claude"], stdin)


def rows_of(sb):
    f = sb["metrics"] / "events.jsonl"
    if not f.exists():
        return []
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]


def iso(dt):
    return dt.isoformat()


def row(ts, hook="h", decision="allow", ms=0, session="s", reason="", agent="", project="p", event="PreToolUse"):
    return {"ts": iso(ts), "client": "claude", "session": session, "hook": hook, "event": event, "decision": decision,
            "reason": reason, "ms": ms, "project": project, "agent": agent}


def write_rows(path, rows, extra_lines=()):
    lines = [json.dumps(r) for r in rows] + list(extra_lines)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


class use_dir:
    """Point in-process hook_metrics at a sandbox."""
    def __init__(self, sb):
        self.sb = sb

    def __enter__(self):
        self.old = {k: os.environ.get(k) for k in ("HARNESS_METRICS_DIR", "HARNESS_HOOK_CONFIG")}
        os.environ["HARNESS_METRICS_DIR"] = str(self.sb["metrics"])
        os.environ["HARNESS_HOOK_CONFIG"] = str(self.sb["root"] / "none.json")

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------- criterion 6: transparent runner
def payload(cmd, extra=None):
    d = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": "sess-9",
         "cwd": "C:/work/myproj"}
    d.update(extra or {})
    return json.dumps(d).encode("utf-8")


def compare(sb, hooks_dir, script, stdin):
    d = direct(sb, hooks_dir, script, stdin)
    r = via_runner(sb, hooks_dir, script, stdin)
    return d, r


def test_runner_deny_is_identical_and_row_says_deny():
    sb = sandbox("m6a")
    stdin = payload(f"git reset --hard # {SECRET}")
    d, r = compare(sb, HOOKS, "shell_policy_gate.py", stdin)
    assert b"permissionDecision" in d[1] and b"deny" in d[1], d  # the fixture really is a deny
    assert d == r, (d, r)
    rows = rows_of(sb)
    assert len(rows) == 1, rows
    assert rows[0]["decision"] == "deny" and rows[0]["hook"] == "shell_policy_gate.py"
    assert rows[0]["session"] == "sess-9" and rows[0]["project"] == "myproj" and rows[0]["client"] == "claude"


def test_runner_allow_is_identical_and_row_says_allow():
    sb = sandbox("m6b")
    d, r = compare(sb, HOOKS, "shell_policy_gate.py", payload("echo hello"))
    assert d[0] == 0 and d[1].strip() == b"", d
    assert d == r, (d, r)
    rows = rows_of(sb)
    assert len(rows) == 1 and rows[0]["decision"] == "allow", rows


def test_runner_exit2_is_identical_and_row_says_deny():
    sb = sandbox("m6c")
    d, r = compare(sb, HOOKS, "agent_model_gate.py", b"{ this is not json")
    assert d[0] == 2 and b"malformed hook JSON" in d[2], d
    assert d == r, (d, r)
    rows = rows_of(sb)
    assert len(rows) == 1 and rows[0]["decision"] == "deny" and rows[0]["reason"] == "exit2", rows


def test_runner_crashing_hook_keeps_stdout_and_exit_code_and_row_says_error():
    sb = sandbox("m6d")
    hooks = sb["root"] / "hooks_copy"
    shutil.copytree(HOOKS, hooks, ignore=shutil.ignore_patterns("__pycache__"))
    (hooks / "boom.py").write_text('import sys\nsys.stdout.write("partial-out")\nraise RuntimeError("boom-xyz")\n',
                                   encoding="utf-8")
    d, r = compare(sb, hooks, "boom.py", b"{}")
    assert d[0] == 1 and b"RuntimeError: boom-xyz" in d[2], d
    assert (r[0], r[1]) == (d[0], d[1]) and r[1] == b"partial-out", (d, r)
    assert b"RuntimeError: boom-xyz" in r[2], r  # traceback reaches stderr through the runner
    rows = rows_of(sb)
    assert len(rows) == 1 and rows[0]["decision"] == "error", rows


def test_runner_unknown_script_fails_without_row():
    sb = sandbox("m6e")
    rc, out, err = via_runner(sb, HOOKS, "no_such_hook.py", b"{}")
    assert rc == 1 and b"no such hook" in err and rows_of(sb) == []


# ---------------------------------------------------------------- criterion 7: report
def build_fixture(sb):
    t0 = NOW - timedelta(hours=2)
    rows = []
    for i, ms in enumerate([10, 20, 30, 40, 50, 60, 70, 80, 90, 100]):  # 10 values: nearest rank p50=5th, p95=10th
        dec = "deny" if i in (0, 1) else "block" if i == 2 else "allow"
        rows.append(row(t0 + timedelta(minutes=i), hook="h1.py", decision=dec, ms=ms, session=f"u{i}"))
    for i, ms in enumerate([9, 1, 5]):  # sorted 1,5,9: p50 = 2nd = 5, p95 = ceil(2.85)=3rd = 9
        rows.append(row(t0 + timedelta(minutes=20 + i), hook="h2.py", ms=ms, session="v"))
    for sec in (0, 50, 100):  # 3 blocks within 120 s -> repeat block
        rows.append(row(t0 + timedelta(hours=1, seconds=sec), hook="h3.py", decision="deny", session="AAAAAAAAXXXX"))
    for sec in (0, 70, 140):  # no 3 inside any 120 s window -> not listed
        rows.append(row(t0 + timedelta(hours=1, minutes=30, seconds=sec), hook="h4.py", decision="deny", session="BBBB"))
    return rows


def cli(sb, *args):
    rc, out, err = proc(sb, [PY, "-I", str(METRICS_CLI), *args, "--ledger", str(sb["root"] / "noledger.json")])
    return rc, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def test_metrics_exact_counts_percentiles_and_repeat_blocks():
    sb = sandbox("m7a")
    write_rows(sb["metrics"] / "events.jsonl", build_fixture(sb), extra_lines=["{garbage", json.dumps({"ts": "not-a-date"})])
    rc, out, err = cli(sb, "--json", "--days", "1")
    assert rc == 0, err
    res = json.loads(out)
    assert res["rows"] == 19 and res["corrupt"] == 2, (res["rows"], res["corrupt"])
    h1, h2 = res["hooks"]["h1.py"], res["hooks"]["h2.py"]
    assert (h1["fired"], h1["allow"], h1["deny"], h1["block"], h1["error"]) == (10, 7, 2, 1, 0), h1
    assert (h1["p50_ms"], h1["p95_ms"]) == (50, 100), h1
    assert (h2["fired"], h2["p50_ms"], h2["p95_ms"]) == (3, 5, 9), h2
    assert res["repeat_blocks"] == [{"hook": "h3.py", "session": "AAAAAAAA", "blocks_within_120s": 3}], res["repeat_blocks"]
    # the text report carries the same facts
    rc, text, _ = cli(sb, "--days", "1")
    assert rc == 0 and "corrupt lines skipped: 2" in text and "h3.py session AAAAAAAA x3" in text, text


def test_metrics_period_and_project_filters():
    sb = sandbox("m7b")
    rows = [row(NOW - timedelta(days=3), hook="old.py", project="a"), row(NOW - timedelta(hours=1), hook="new.py", project="a"),
            row(NOW - timedelta(hours=1), hook="other.py", project="b")]
    write_rows(sb["metrics"] / "events.jsonl", rows)
    res = json.loads(cli(sb, "--json", "--days", "1")[1])
    assert set(res["hooks"]) == {"new.py", "other.py"}
    res = json.loads(cli(sb, "--json", "--days", "7", "--project", "a")[1])
    assert set(res["hooks"]) == {"old.py", "new.py"}


def test_metrics_empty_and_missing_log():
    sb = sandbox("m7c")
    rc, out, err = cli(sb, "--json")  # no folder at all
    assert rc == 0, err
    res = json.loads(out)
    assert res["rows"] == 0 and res["corrupt"] == 0 and res["hooks"] == {} and res["repeat_blocks"] == []
    (sb["metrics"]).mkdir()
    (sb["metrics"] / "events.jsonl").write_text("", encoding="utf-8")
    rc, out, _ = cli(sb, "--json")
    assert rc == 0 and json.loads(out)["rows"] == 0
    rc, text, _ = cli(sb)
    assert rc == 0 and "0 hook runs" in text


def test_metrics_only_corrupt_lines_is_not_fatal():
    sb = sandbox("m7d")
    sb["metrics"].mkdir()
    (sb["metrics"] / "events.jsonl").write_text("nope\n{\n[1,2]\n", encoding="utf-8")
    rc, out, err = cli(sb, "--json")
    res = json.loads(out)
    assert rc == 0 and res["rows"] == 0 and res["corrupt"] == 3, (rc, err, res)


def test_metrics_agent_runs():
    sb = sandbox("m7e")
    t = NOW - timedelta(minutes=5)
    write_rows(sb["metrics"] / "events.jsonl", [
        row(t, hook="agent_model_gate.py", reason="agent-start", agent="code-reviewer"),
        row(t, hook="agent_visibility.py", reason="agent-stop", agent="code-reviewer", ms=125000),
    ])
    res = json.loads(cli(sb, "--json")[1])
    assert res["agents"] == {"code-reviewer": {"starts": 1, "stops": 1, "seconds": 125}}, res["agents"]


# ---------------------------------------------------------------- criterion 8: retention
def test_purge_via_cli_empties_a_small_log_of_old_rows():
    sb = sandbox("m8a")
    f = sb["metrics"] / "events.jsonl"
    write_rows(f, [row(NOW - timedelta(days=31), session=str(i)) for i in range(5)])
    assert f.stat().st_size < 5 * 1024 * 1024
    rc, out, err = cli(sb, "--purge")
    assert rc == 0 and "purge done" in out, err
    assert not f.exists() or f.read_text(encoding="utf-8") == ""


def test_record_purges_old_rows_when_stamp_is_older_than_an_hour():
    """record() never purges inline: with a stale stamp it claims the stamp and starts a detached `--purge` process."""
    sb = sandbox("m8b")
    f = sb["metrics"] / "events.jsonl"
    write_rows(f, [row(NOW - timedelta(days=31), hook="ancient.py")])
    stamp = sb["metrics"] / "purge.stamp"
    stamp.write_text(str(time.time() - 4000), encoding="utf-8")
    with use_dir(sb):
        t0 = time.monotonic()
        assert hm.record("fresh.py", "PreToolUse", "allow") is True
        assert time.monotonic() - t0 < 3, "record() must not wait for the purge"
    assert float(stamp.read_text(encoding="utf-8")) > time.time() - 120, "the stamp must be claimed (refreshed) by record()"
    assert "fresh.py" in [r["hook"] for r in rows_of(sb)], "the new row is appended first"
    deadline = time.time() + 10  # let the detached process finish so it does not touch the sandbox after the test
    while time.time() < deadline and "ancient.py" in [r["hook"] for r in rows_of(sb)]:
        time.sleep(0.2)


def test_purge_function_removes_old_rows_and_keeps_fresh_ones():
    sb = sandbox("m8b2")
    f = sb["metrics"] / "events.jsonl"
    write_rows(f, [row(NOW - timedelta(days=31), hook="ancient.py"), row(NOW - timedelta(days=1), hook="fresh.py")])
    with use_dir(sb):
        assert hm.purge() >= 1
    assert [r["hook"] for r in rows_of(sb)] == ["fresh.py"]
    assert (sb["metrics"] / "purge.stamp").exists()


def test_record_does_not_purge_when_stamp_is_recent():
    sb = sandbox("m8c")
    f = sb["metrics"] / "events.jsonl"
    write_rows(f, [row(NOW - timedelta(days=31), hook="ancient.py")])
    (sb["metrics"] / "purge.stamp").write_text(str(time.time() - 60), encoding="utf-8")
    with use_dir(sb):
        assert hm.record("fresh.py", "PreToolUse", "allow") is True
    assert [r["hook"] for r in rows_of(sb)] == ["ancient.py", "fresh.py"]


def test_purge_keeps_only_recent_rows_in_a_mixed_log_and_keeps_corrupt_line():
    sb = sandbox("m8d")
    f = sb["metrics"] / "events.jsonl"
    write_rows(f, [row(NOW - timedelta(days=40), hook="a"), row(NOW - timedelta(days=29), hook="b"),
                   row(NOW - timedelta(days=31), hook="c"), row(NOW - timedelta(hours=1), hook="d")],
               extra_lines=["{corrupt-line-stays"])
    assert cli(sb, "--purge")[0] == 0
    lines = f.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3 and lines[2] == "{corrupt-line-stays", lines
    assert [json.loads(x)["hook"] for x in lines[:2]] == ["b", "d"]


def test_purge_deletes_fully_old_rotated_file_and_trims_partial_one():
    sb = sandbox("m8e")
    m = sb["metrics"]
    write_rows(m / "events.jsonl", [row(NOW, hook="live")])
    write_rows(m / "events.1.jsonl", [row(NOW - timedelta(days=45), hook="x"), row(NOW - timedelta(days=35), hook="y")])
    write_rows(m / "events.2.jsonl", [row(NOW - timedelta(days=45), hook="x"), row(NOW - timedelta(days=2), hook="keepme")])
    assert cli(sb, "--purge")[0] == 0
    assert not (m / "events.1.jsonl").exists()
    assert [json.loads(x)["hook"] for x in (m / "events.2.jsonl").read_text(encoding="utf-8").splitlines()] == ["keepme"]
    assert (m / "events.jsonl").exists()


def _big_active(path, tag):
    base = json.dumps(row(NOW, hook=tag, reason="x" * 40)) + "\n"
    n = (5 * 1024 * 1024) // len(base) + 10
    Path(path).write_text(base * n, encoding="utf-8", newline="\n")
    return base


def test_rotation_at_5mb_keeps_at_most_five_rotated_files():
    sb = sandbox("m8f")
    m = sb["metrics"]
    m.mkdir()
    base = _big_active(m / "events.jsonl", "active-before")
    for i in range(1, 6):
        write_rows(m / f"events.{i}.jsonl", [row(NOW, hook=f"rot{i}")])
    with use_dir(sb):
        assert hm.record("after.py", "PreToolUse", "allow") is True
    rotated = sorted(p.name for p in m.glob("events.*.jsonl"))
    assert rotated == [f"events.{i}.jsonl" for i in range(1, 6)], rotated
    assert (m / "events.1.jsonl").stat().st_size >= 5 * 1024 * 1024
    assert (m / "events.1.jsonl").read_text(encoding="utf-8").startswith(base)
    for i in range(2, 6):  # shifted by one; the oldest (rot5) is gone
        assert json.loads((m / f"events.{i}.jsonl").read_text(encoding="utf-8"))["hook"] == f"rot{i - 1}"
    assert [r["hook"] for r in rows_of(sb)] == ["after.py"]


def test_no_rotation_below_5mb():
    sb = sandbox("m8g")
    m = sb["metrics"]
    write_rows(m / "events.jsonl", [row(NOW, hook="first")])
    with use_dir(sb):
        hm.record("second.py", "PreToolUse", "allow")
    assert not (m / "events.1.jsonl").exists()
    assert [r["hook"] for r in rows_of(sb)] == ["first", "second.py"]


# ---------------------------------------------------------------- criterion 9: git refusal and privacy
def test_metrics_folder_inside_git_work_tree_is_refused():
    sb = sandbox("m9a")
    repo = sb["root"] / "repo"
    (repo / ".git").mkdir(parents=True)
    sb["metrics"] = repo / "deep" / "metrics"
    with use_dir(sb):
        assert hm.record("h.py", "PreToolUse", "deny") is False
    assert not (repo / "deep").exists(), "nothing may be created inside the work tree"
    d, r = compare(sb, HOOKS, "shell_policy_gate.py", payload("echo hi"))
    assert d == r
    assert not (repo / "deep").exists()


def test_rows_hold_no_prompt_command_or_secret():
    sb = sandbox("m9b")
    cmd = f"curl -H 'Authorization: {SECRET}' http://example.invalid/leak-marker"
    stdin = payload(cmd, {"prompt": f"my prompt {SECRET}", "agent_type": "code-reviewer",
                          "tool_response": {"stdout": SECRET}})
    via_runner(sb, HOOKS, "shell_policy_gate.py", stdin)
    via_runner(sb, HOOKS, "agent_model_gate.py", json.dumps({
        "hook_event_name": "PreToolUse", "tool_name": "Agent", "cwd": str(sb["root"]),
        "tool_input": {"subagent_type": "code-reviewer", "model": "sonnet", "prompt": SECRET}}).encode())
    raw = (sb["metrics"] / "events.jsonl").read_text(encoding="utf-8")
    assert raw.strip(), "rows expected"
    assert SECRET not in raw and "leak-marker" not in raw and "curl" not in raw and "my prompt" not in raw, raw
    for r in rows_of(sb):
        assert tuple(r) == FIELDS, tuple(r)
    # the secret is not in any file of the metrics folder or hook-state folder either
    for p in list(sb["metrics"].rglob("*")) + list(sb["state"].rglob("*")):
        if p.is_file():
            assert SECRET not in p.read_text(encoding="utf-8", errors="replace"), p


def test_record_truncates_and_basenames_untrusted_fields():
    sb = sandbox("m9c")
    with use_dir(sb):
        hm.record("h.py", "Stop", "allow", reason="r" * 500, session="s" * 500, project="C:\\Users\\someone\\secret-proj\\")
    r = rows_of(sb)[0]
    assert len(r["reason"]) <= 60 and len(r["session"]) <= 64 and r["project"] == "secret-proj", r


# ---------------------------------------------------------------- criterion 10: lock
def test_record_when_lock_is_held_drops_and_counts_without_raising():
    sb = sandbox("m10")
    m = sb["metrics"]
    m.mkdir()
    lock = m / "events.jsonl.lock"
    lock.write_text("99999", encoding="utf-8")  # fresh lock held by "someone else"
    with use_dir(sb):
        t0 = time.monotonic()
        assert hm.record("h.py", "PreToolUse", "deny") is False
        assert time.monotonic() - t0 < 5, "must not block for long"
        assert (m / "dropped.count").read_text(encoding="utf-8") == "1"
        assert hm.record("h.py", "PreToolUse", "deny") is False
        assert (m / "dropped.count").read_text(encoding="utf-8") == "2"
    assert not (m / "events.jsonl").exists(), "no row may be written without the lock"
    assert lock.exists(), "a lock we did not take must not be removed"
    # the dropped count is reported by the CLI
    rc, out, err = cli(sb, "--json")
    assert rc == 0 and json.loads(out)["dropped"] >= 2, (out, err)  # the CLI purge itself also fails to lock and counts one more
    # once the lock is gone, writing works again
    lock.unlink()
    with use_dir(sb):
        assert hm.record("h.py", "PreToolUse", "allow") is True
    assert len(rows_of(sb)) == 1 and not lock.exists()


def test_record_takes_over_a_stale_lock():
    sb = sandbox("m10b")
    m = sb["metrics"]
    m.mkdir()
    lock = m / "events.jsonl.lock"
    lock.write_text("1", encoding="utf-8")
    old = time.time() - 300
    os.utime(lock, (old, old))
    with use_dir(sb):
        assert hm.record("h.py", "PreToolUse", "allow") is True
    assert len(rows_of(sb)) == 1


def test_runner_result_unchanged_when_lock_is_held():
    sb = sandbox("m10c")
    sb["metrics"].mkdir()
    (sb["metrics"] / "events.jsonl.lock").write_text("1", encoding="utf-8")
    stdin = payload("git reset --hard")
    d = direct(sb, HOOKS, "shell_policy_gate.py", stdin)
    r = via_runner(sb, HOOKS, "shell_policy_gate.py", stdin)
    assert d == r and rows_of(sb) == []


def test_nearest_rank_formula_matches_independent_definition():
    sys.path.insert(0, str(hv2.TOOLS))
    import importlib.util
    spec = importlib.util.spec_from_file_location("metrics_cli", METRICS_CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    vals = [15, 20, 35, 40, 50]  # textbook nearest-rank example: P30=20, P40=20, P50=35, P100=50
    assert [mod.pct(vals, p) for p in (30, 40, 50, 100)] == [20, 20, 35, 50]
    assert mod.pct([], 95) == 0 and mod.pct([7], 95) == 7


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
