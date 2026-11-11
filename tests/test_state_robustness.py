"""Tests for state-file robustness in harness/hooks: _agentio.atomic_write_text, keep_going_gate._counts,
repeat_work_gate.load_state/save_state, worktree_policy.ledger_rows and its UserPromptSubmit reminder.
Plain script: PASS/FAIL lines, final 'OK: 0 failing' or 'FAILED: n failing'. All state lives in temp dirs."""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "harness" / "hooks"
TMP = Path(tempfile.mkdtemp(prefix="state_robust_"))
# state folders must point at temp BEFORE the modules are imported (some read env at import time)
os.environ["HARNESS_HOOK_STATE"] = str(TMP / "hookstate")
os.environ["CLAUDE_REPEAT_WORK_STATE"] = str(TMP / "repeat")
os.environ["CLAUDE_WORKTREE_POLICY_STATE"] = str(TMP / "wt")
os.environ["HARNESS_HOOK_CONFIG"] = str(TMP / "no_config.json")
sys.path.insert(0, str(HOOKS))


def load(name):
    spec = importlib.util.spec_from_file_location(name, HOOKS / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


agentio = load("_agentio")
sys.modules["_agentio"] = agentio
kg = load("keep_going_gate")
rw = load("repeat_work_gate")
wp = load("worktree_policy")

FAILS = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL") + ": " + name + ("" if cond else "  " + str(detail)))
    if not cond:
        FAILS.append(name)


CORRUPT = {
    "truncated-object": b'{"a":',
    "bare-number": b"5",
    "empty-list": b"[]",
    "invalid-utf8": b"\xff\xfe\x80\x81{\xc3\x28",
}

# ---- (1) atomic write ----
d1 = TMP / "atomic1"
d1.mkdir()
target = d1 / "out.json"
payload = '{"k": "значение", "n": [1, 2, 3]}\n'
agentio.atomic_write_text(target, payload)
check("atomic: full content written", target.read_bytes() == payload.encode("utf-8"))
check("atomic: no leftover temp file", [p.name for p in d1.iterdir()] == ["out.json"], list(d1.iterdir()))
agentio.atomic_write_text(target, "second")
check("atomic: overwrite replaces content", target.read_text(encoding="utf-8") == "second")
nested = d1 / "sub" / "deep" / "f.txt"
agentio.atomic_write_text(nested, "x")
check("atomic: creates missing parent folders", nested.read_text() == "x")

# ---- (2) transient PermissionError twice then success ----
real_replace = os.replace
calls = {"n": 0}


def flaky(src, dst):
    calls["n"] += 1
    if calls["n"] <= 2:
        raise PermissionError(13, "locked")
    return real_replace(src, dst)


d2 = TMP / "atomic2"
d2.mkdir()
os.replace = flaky
try:
    agentio.atomic_write_text(d2 / "f.txt", "after-retry")
    err2 = None
except BaseException as e:  # noqa: BLE001
    err2 = e
finally:
    os.replace = real_replace
check("atomic: succeeds after 2 transient PermissionErrors", err2 is None, repr(err2))
check("atomic: retry content correct", (d2 / "f.txt").read_text() == "after-retry")
check("atomic: replace attempted exactly 3 times", calls["n"] == 3, calls["n"])
check("atomic: no temp left after retry success", [p.name for p in d2.iterdir()] == ["f.txt"])

# ---- (3) always fails ----
d3 = TMP / "atomic3"
d3.mkdir()
(d3 / "f.txt").write_text("original")
calls3 = {"n": 0}


def always(src, dst):
    calls3["n"] += 1
    raise PermissionError(13, "locked forever")


os.replace = always
try:
    agentio.atomic_write_text(d3 / "f.txt", "never-lands")
    err3 = None
except BaseException as e:  # noqa: BLE001
    err3 = e
finally:
    os.replace = real_replace
check("atomic: persistent failure raises OSError", isinstance(err3, OSError), repr(err3))
check("atomic: temp file removed after failure", [p.name for p in d3.iterdir()] == ["f.txt"], list(d3.iterdir()))
check("atomic: destination untouched after failure", (d3 / "f.txt").read_text() == "original")
check("atomic: retries bounded by retries=5", calls3["n"] == 5, calls3["n"])

# ---- (4) keep_going counts ----
counts_path = agentio.state_dir("citation_gate") / "keep_going_counts.json"
check("keep_going: state dir is under temp", str(counts_path).startswith(str(TMP)), counts_path)
sess = {"session_id": "s1"}
counts_path.unlink() if counts_path.exists() else None
r1, r2 = kg._counts(sess), kg._counts(sess)
check("keep_going: baseline counts increment 1,2", (r1, r2) == (1, 2), (r1, r2))
for label, raw in CORRUPT.items():
    counts_path.write_bytes(raw)
    try:
        v = kg._counts(sess)
        err = None
    except BaseException as e:  # noqa: BLE001
        v, err = None, e
    check(f"keep_going[{label}]: no crash, counts restart at 1", err is None and v == 1, (v, repr(err)))
    try:
        saved = json.loads(counts_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        saved = repr(e)
    check(f"keep_going[{label}]: file valid after, {{'s1': 1}}", saved == {"s1": 1}, saved)
    check(f"keep_going[{label}]: next call continues 2", kg._counts(sess) == 2)
leftover = [p.name for p in counts_path.parent.iterdir() if ".tmp" in p.name]
check("keep_going: no temp files left", leftover == [], leftover)

# ---- (5) repeat_work_gate state ----
check("repeat_work: state file under temp", str(rw.STATE_FILE).startswith(str(TMP)), rw.STATE_FILE)
rw.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
if rw.STATE_FILE.exists():
    rw.STATE_FILE.unlink()
check("repeat_work: missing file -> {}", rw.load_state() == {})
for label, raw in CORRUPT.items():
    rw.STATE_FILE.write_bytes(raw)
    try:
        st = rw.load_state()
        err = None
    except BaseException as e:  # noqa: BLE001
        st, err = None, e
    check(f"repeat_work[{label}]: load_state returns {{}}", err is None and st == {}, (st, repr(err)))
    try:
        tu = rw.tripped_and_unanswered({"session_id": "s1", "transcript_path": ""})
    except BaseException as e:  # noqa: BLE001
        tu = repr(e)
    check(f"repeat_work[{label}]: tripped_and_unanswered False", tu is False, tu)
    rw.save_state({"s1": {"warned": ["abc"]}})
    try:
        back = json.loads(rw.STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        back = repr(e)
    check(f"repeat_work[{label}]: save after corrupt leaves valid file", back == {"s1": {"warned": ["abc"]}}, back)
    check(f"repeat_work[{label}]: load_state reads saved value", rw.load_state() == back)
leftover = [p.name for p in rw.STATE_FILE.parent.iterdir() if ".tmp" in p.name]
check("repeat_work: no temp files left", leftover == [], leftover)

# ---- (6) worktree_policy.ledger_rows ----
wp.LEDGER = TMP / "wt" / "scratch.jsonl"
wp.LEDGER.parent.mkdir(parents=True, exist_ok=True)
good1, good2 = {"sid": "a", "path": "p1"}, {"sid": "b", "path": "p2"}


def write_ledger(lines):
    wp.LEDGER.write_text("\n".join(lines) + "\n", encoding="utf-8")


write_ledger([json.dumps(good1), '{"sid": "bad", "pa', json.dumps(good2)])
rows = wp.ledger_rows()
check("ledger: good,bad,good -> 2 rows in order", rows == [good1, good2], rows)
check("ledger: SKIPPED_LINES == 1", wp.SKIPPED_LINES == 1, wp.SKIPPED_LINES)

write_ledger([json.dumps(good1), json.dumps(good2)])
rows = wp.ledger_rows()
check("ledger: clean -> 2 rows, SKIPPED_LINES reset to 0", len(rows) == 2 and wp.SKIPPED_LINES == 0, wp.SKIPPED_LINES)

write_ledger([json.dumps(good1), "", "   ", json.dumps(good2), ""])
rows = wp.ledger_rows()
check("ledger: blank lines not counted as skipped", len(rows) == 2 and wp.SKIPPED_LINES == 0, wp.SKIPPED_LINES)

write_ledger([json.dumps(good1), "[1, 2]", json.dumps(good2)])
rows = wp.ledger_rows()
check("ledger: JSON array line skipped and counted", rows == [good1, good2] and wp.SKIPPED_LINES == 1,
      (rows, wp.SKIPPED_LINES))

write_ledger(["5", '"str"', "null", "{bad", json.dumps(good1)])
rows = wp.ledger_rows()
check("ledger: scalars/null/garbage each counted (4), good row kept", rows == [good1] and wp.SKIPPED_LINES == 4,
      (rows, wp.SKIPPED_LINES))

wp.LEDGER.write_bytes(json.dumps(good1).encode() + b"\n\xff\xfe\x80\n" + json.dumps(good2).encode() + b"\n")
rows = wp.ledger_rows()
check("ledger: invalid-UTF-8 line skipped, later lines read", rows == [good1, good2] and wp.SKIPPED_LINES == 1,
      (rows, wp.SKIPPED_LINES))

wp.LEDGER.unlink()
check("ledger: missing file -> [] and 0 skipped", wp.ledger_rows() == [] and wp.SKIPPED_LINES == 0)

# ---- (7) reminder sentence ----
scratch = TMP / "scratch_dir"
scratch.mkdir()
old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
entry = {"sid": "x", "path": str(scratch), "start_head": "", "ts": old_ts}


def run_prompt():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            wp.prompt({"prompt": "hi"})
        except SystemExit:
            pass
    return buf.getvalue()


write_ledger([json.dumps(entry)])
out = run_prompt()
ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"] if out.strip() else ""
check("reminder: clean ledger emits reminder about the stale worktree", "older than 24 h" in ctx, out)
check("reminder: clean ledger has no 'corrupt' sentence", "corrupt" not in ctx and "Note:" not in ctx, ctx)

write_ledger([json.dumps(entry), "{broken", "[1]"])
out = run_prompt()
ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"] if out.strip() else ""
check("reminder: 2 skipped lines -> exact sentence",
      ctx.endswith("Note: 2 corrupt ledger line(s) were skipped."), ctx)
check("reminder: main text still present with corruption", "older than 24 h" in ctx, ctx)

write_ledger(["{broken"])
out = run_prompt()
check("reminder: only corrupt lines and no stale worktree -> no output", out.strip() == "", out)

write_ledger([json.dumps(entry)])
out = run_prompt()
ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"] if out.strip() else ""
check("reminder: sentence disappears once ledger is clean again", "corrupt" not in ctx and ctx != "", ctx)

print("OK: 0 failing" if not FAILS else f"FAILED: {len(FAILS)} failing")
sys.exit(1 if FAILS else 0)
