"""Acceptance tests for S9 self-service ladder (criteria A-D). Expected values come from the sprint plan criteria."""
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
HOOK = ROOT / "harness" / "hooks" / "ask_last_gate.py"
STORE = ROOT / "tools" / "secret_store.py"
DUMMY1 = "dummy-first-value-AAA111"
DUMMY2 = "dummy-second-value-BBB222"


def read(p):
    return Path(p).read_text(encoding="utf-8")


# ---------- A ----------
def test_a_core_has_ladder_and_operator_only_list():
    t = read(ROOT / "harness/rules/CORE.md")
    assert "Self-service ladder" in t
    low = t.lower()
    for word in ("money", "captcha", "creat", "production data"):
        assert word in low
    assert re.search(r"operator-only", low)
    assert "account" in low


@pytest.fixture(scope="module")
def built():
    r = subprocess.run([PY, "-I", str(ROOT / "tools" / "build.py")], capture_output=True, text=True, cwd=ROOT, timeout=300)
    assert r.returncode == 0, r.stdout[-500:] + r.stderr[-500:]
    return ROOT / "out"


def test_a_build_outputs_contain_ladder(built):
    assert "Self-service ladder" in read(built / "claude/CLAUDE.md")
    assert "Self-service ladder" in read(built / "codex/AGENTS.md")


# ---------- B ----------
def test_b_unblock_exists_and_built(built):
    assert (ROOT / "harness/commands/unblock.md").is_file()
    assert (built / "claude/commands/unblock.md").is_file()
    assert (built / "codex/prompts/unblock.md").is_file()


def test_b_unblock_text_contract():
    t = read(ROOT / "harness/commands/unblock.md").lower()
    assert "tried" in t and "result" in t and "stored where" in t
    # no placeholder that invites a secret value
    assert not re.search(r"(api[_-]?key|token|password|secret)\s*[=:]\s*\S{6,}", t)
    assert "never print a secret value" in t


# ---------- C ----------
TMP = Path(tempfile.mkdtemp(prefix="selfsvc_"))


def transcript(calls=()):
    rows = [{"type": "user", "message": {"role": "user", "content": "Deploy the stack"}, "timestamp": "2026-11-12T10:00:00Z"}]
    for cmd, result, err in calls:
        tid = "toolu_" + uuid.uuid4().hex[:10]
        rows.append({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": cmd}}]}})
        rows.append({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": result, "is_error": err}]}})
    p = TMP / (uuid.uuid4().hex[:8] + ".jsonl")
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8", newline="\n")
    return str(p)


def hook(question, tp):
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARNESS_")}
    env["HARNESS_HOOK_STATE"] = str(TMP / ("st_" + uuid.uuid4().hex[:6]))
    env["HARNESS_METRICS_DIR"] = str(TMP / "metrics")
    env["PYTHONIOENCODING"] = "utf-8"
    payload = {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion", "transcript_path": tp,
               "tool_input": {"questions": [{"question": question, "options": []}]}}
    p = subprocess.run([PY, "-I", "-s", "-X", "utf8", str(HOOK), "--harness-client=claude"], input=json.dumps(payload),
                       capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    out = p.stdout.strip()
    h = (json.loads(out).get("hookSpecificOutput") or {}) if out else {}
    return p.returncode, out, h.get("permissionDecision"), h.get("permissionDecisionReason", "")


@pytest.mark.parametrize("q", ["Please give me the API key for the deploy", "Please create the terraform state file"])
def test_c_ask_without_attempts_denied_with_unblock(q):
    rc, out, dec, why = hook(q, transcript())
    assert rc == 0 and dec == "deny", out
    assert "/unblock" in why


def test_c_ask_after_real_failed_attempt_allowed():
    calls = [
        ("printenv | cut -d= -f1 | grep -i -E 'github|gh_|token|key'", "", False),
        ("find . -maxdepth 3 -name '.env*' -o -name '*token*'", "find: no such file or directory: token not found", False),
        ("gh auth status", "You are not logged in to any GitHub hosts. Run gh auth login to authenticate.", True),
    ]
    rc, out, dec, why = hook("Please give me the GitHub API token", transcript(calls))
    assert rc == 0 and out == "", out


@pytest.mark.parametrize("q", [
    "Please pay the invoice for the cloud billing",
    "Please enter your password to authorize the login",
    "Please purchase the subscription and send the API key",
])
def test_c_money_and_password_never_denied(q):
    rc, out, dec, why = hook(q, transcript())
    assert rc == 0 and out == "", out


# ---------- D ----------
def run_store(args, stdin="", d=None, env_extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARNESS_")}
    env["HARNESS_SECRET_DIR"] = str(d)
    env.update(env_extra or {})
    return subprocess.run([PY, "-I", str(STORE)] + args, input=stdin, capture_output=True, text=True, env=env, timeout=60)


@pytest.fixture
def sdir():
    d = Path(tempfile.mkdtemp(prefix="secrets_")) / "store"
    return d


def test_d_put_created_then_exists_keeps_first(sdir):
    r1 = run_store(["put", "MY_KEY"], DUMMY1, sdir)
    assert r1.returncode == 0 and r1.stdout.strip() == "created"
    r2 = run_store(["put", "MY_KEY"], DUMMY2, sdir)
    assert r2.returncode == 0 and r2.stdout.strip() == "exists"
    assert (sdir / "MY_KEY").read_text(encoding="utf-8").strip() == DUMMY1
    for r in (r1, r2):
        assert DUMMY1 not in r.stdout + r.stderr and DUMMY2 not in r.stdout + r.stderr


def test_d_names_lists_name_not_value(sdir):
    run_store(["put", "LISTED_NAME"], DUMMY1, sdir)
    r = run_store(["names"], "", sdir)
    assert r.returncode == 0
    assert "LISTED_NAME" in r.stdout.split()
    assert DUMMY1 not in r.stdout + r.stderr


def test_d_dir_inside_git_refused():
    inside = ROOT / "tests" / "_secret_dir_should_not_exist"
    try:
        r = run_store(["put", "X_KEY"], DUMMY1, inside)
        assert r.returncode != 0
        assert not (inside / "X_KEY").exists()
        assert DUMMY1 not in r.stdout + r.stderr
    finally:
        if inside.exists():
            import shutil
            shutil.rmtree(inside, ignore_errors=True)


@pytest.mark.parametrize("bad", ["../x", "a/b", "..", "1abc", "a b", ""])
def test_d_bad_name_refused(sdir, bad):
    r = run_store(["put", bad], DUMMY1, sdir)
    assert r.returncode != 0
    assert "created" not in r.stdout
    parent_escape = sdir.parent / "x"
    assert not parent_escape.exists()


def test_d_concurrent_put_exactly_one_created(sdir):
    sdir.mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARNESS_")}
    env["HARNESS_SECRET_DIR"] = str(sdir)
    procs = []
    for i in range(8):
        p = subprocess.Popen([PY, "-I", str(STORE), "put", "RACE_KEY"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=env)
        procs.append((p, "dummy-race-value-%d" % i))
    outs = []
    for p, v in procs:
        o, e = p.communicate(v, timeout=120)
        outs.append(o.strip())
    assert outs.count("created") == 1, outs
    assert outs.count("exists") == 7, outs
