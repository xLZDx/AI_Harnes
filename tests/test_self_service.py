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


# ---------- E: ledger block --class OPERATOR needs --case (S9-T6) ----------
LEDGER = ROOT / "tools" / "ledger.py"
ALL_CASES = ["prohibition", "money", "personal-secret", "captcha", "account", "irreversible-delete", "force-push", "production-data"]


@pytest.fixture()
def ledger_env(tmp_path):
    lp = tmp_path / "ledger.json"
    env = {**os.environ, "HARNESS_LEDGER": str(lp), "PYTHONIOENCODING": "utf-8"}

    def run(*args):
        return subprocess.run([PY, str(LEDGER), *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", cwd=str(tmp_path), env=env)

    assert run("sprint-add", "--id", "ST", "--title", "t").returncode == 0
    r = run("task-add", "--sprint", "ST", "--id", "ST-T1", "--title", "x")
    assert r.returncode == 0, r.stderr

    def blocked():
        d = json.loads(lp.read_text(encoding="utf-8"))
        for s in d["sprints"]:
            for t in s["tasks"]:
                if t["id"] == "ST-T1":
                    return t.get("blocked")
        raise AssertionError("task missing")

    return run, blocked


def test_e_operator_without_case_refused(ledger_env):
    run, blocked = ledger_env
    r = run("block", "ST-T1", "--class", "OPERATOR", "--cause", "needs the operator to decide")
    assert r.returncode != 0
    assert blocked() is None


@pytest.mark.parametrize("bad", ["foo", "Money", "", "decision", "prohibited"])
def test_e_operator_unknown_case_refused(ledger_env, bad):
    run, blocked = ledger_env
    r = run("block", "ST-T1", "--class", "OPERATOR", "--case", bad, "--cause", "needs the operator to decide")
    assert r.returncode != 0
    assert blocked() is None


def test_e_operator_money_accepted(ledger_env):
    run, blocked = ledger_env
    r = run("block", "ST-T1", "--class", "OPERATOR", "--case", "money", "--cause", "real trading order needs the operator")
    assert r.returncode == 0, r.stderr
    b = blocked()
    assert b and b["class"] == "OPERATOR" and b["case"] == "money"


@pytest.mark.parametrize("case", [c for c in ALL_CASES if c != "prohibition"])
def test_e_operator_each_nonprohibition_case_accepted(ledger_env, case):
    run, blocked = ledger_env
    r = run("block", "ST-T1", "--class", "OPERATOR", "--case", case, "--cause", "only the operator can do this")
    assert r.returncode == 0, r.stderr
    assert blocked()["class"] == "OPERATOR"


def test_e_prohibition_without_quoted_error_refused(ledger_env):
    run, blocked = ledger_env
    r = run("block", "ST-T1", "--class", "OPERATOR", "--case", "prohibition", "--cause", "the tool was prohibited by policy")
    assert r.returncode != 0
    assert blocked() is None


def test_e_prohibition_with_quoted_error_accepted(ledger_env):
    run, blocked = ledger_env
    r = run("block", "ST-T1", "--class", "OPERATOR", "--case", "prohibition",
            "--cause", 'tool said "permission denied by policy X"')
    assert r.returncode == 0, r.stderr
    b = blocked()
    assert b["class"] == "OPERATOR" and b["case"] == "prohibition"


@pytest.mark.parametrize("klass", ["EXTERNAL", "PROHIBITED"])
def test_e_other_classes_work_without_case(ledger_env, klass):
    run, blocked = ledger_env
    r = run("block", "ST-T1", "--class", klass, "--cause", "service returned HTTP 503 twice")
    assert r.returncode == 0, r.stderr
    b = blocked()
    assert b["class"] == klass and "case" not in b


# ---------- F: put leaves no temp file; device names; ledger case rules ----------
def _race(sdir, name, n=6):
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARNESS_")}
    env["HARNESS_SECRET_DIR"] = str(sdir)
    procs = [subprocess.Popen([PY, "-I", str(STORE), "put", name], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, env=env) for _ in range(n)]
    return [p.communicate("dummy-race-value-%d" % i, timeout=120)[0].strip() for i, p in enumerate(procs)]


def test_f_no_temp_file_left_after_put_and_second_batch_one_created(sdir):
    r = run_store(["put", "TMP_KEY"], DUMMY1, sdir)
    assert r.stdout.strip() == "created"
    assert sorted(os.listdir(sdir)) == ["TMP_KEY"]
    first = _race(sdir, "BATCH_ONE")
    second = _race(sdir, "BATCH_TWO")
    assert first.count("created") == 1 and first.count("exists") == 5, first
    assert second.count("created") == 1 and second.count("exists") == 5, second
    assert _race(sdir, "BATCH_ONE").count("created") == 0
    assert sorted(os.listdir(sdir)) == ["BATCH_ONE", "BATCH_TWO", "TMP_KEY"]
    assert not [n for n in os.listdir(sdir) if n.startswith(".put-")]


@pytest.mark.parametrize("bad", ["NUL", "nul", "CON", "COM1", "KEY\n", "KEY\r\n", " KEY"])
def test_f_device_and_newline_names_refused_create_nothing(sdir, bad):
    r = run_store(["put", bad], DUMMY1, sdir)
    assert r.returncode != 0
    assert "created" not in r.stdout
    assert DUMMY1 not in r.stdout + r.stderr
    assert not sdir.exists() or os.listdir(sdir) == []
    assert not (sdir.parent / "NUL.txt").exists()


def test_f_operator_prohibition_lone_apostrophe_refused(ledger_env):
    run, blocked = ledger_env
    r = run("block", "ST-T1", "--class", "OPERATOR", "--case", "prohibition", "--cause", "operator's approval needed here")
    assert r.returncode != 0
    assert blocked() is None


def test_f_external_with_case_refused(ledger_env):
    run, blocked = ledger_env
    r = run("block", "ST-T1", "--class", "EXTERNAL", "--case", "money", "--cause", "service returned HTTP 503 twice")
    assert r.returncode != 0
    assert blocked() is None


# ---------- G: secret_store without os.link, git errors, log, crash, non-ASCII ----------
def _load_store():
    import importlib.util
    spec = importlib.util.spec_from_file_location("secret_store_under_test", str(STORE))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_store_cwd(args, d, cwd, stdin=b""):
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARNESS_")}
    env["HARNESS_SECRET_DIR"] = str(d)
    return subprocess.run([PY, "-I", str(STORE)] + args, input=stdin, capture_output=True, env=env, cwd=str(cwd), timeout=60)


def test_g_put_without_os_link_still_publishes_and_never_overwrites(sdir, monkeypatch):
    mod = _load_store()
    monkeypatch.setenv("HARNESS_SECRET_DIR", str(sdir))

    def no_link(*a, **k):
        raise OSError("link not supported")
    monkeypatch.setattr(os, "link", no_link)
    assert mod.put("NOLINK_KEY", DUMMY1) == "created"
    assert mod.put("NOLINK_KEY", DUMMY2) == "exists"
    assert (sdir / "NOLINK_KEY").read_bytes() == DUMMY1.encode("utf-8")
    assert not [n for n in os.listdir(sdir) if n.startswith(".put-")]


def test_g_git_error_other_than_not_a_repo_refuses_and_writes_nothing(sdir, monkeypatch):
    mod = _load_store()
    monkeypatch.setenv("HARNESS_SECRET_DIR", str(sdir))

    def fake_run(*a, **k):
        return subprocess.CompletedProcess(a[0], 128, stdout="", stderr="fatal: detected dubious ownership")
    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(SystemExit) as ei:
        mod.put("GITERR_KEY", DUMMY1)
    assert ei.value.code not in (0, None)
    assert not sdir.exists() or os.listdir(sdir) == []


def test_g_log_unknown_name_fails_and_leaves_decision_log_untouched(sdir, tmp_path):
    (tmp_path / "core").mkdir()
    log = tmp_path / "core" / "DECISION_LOG.md"
    log.write_bytes(b"# Decisions\n")
    r = _run_store_cwd(["log", "NEVER_STORED"], sdir, tmp_path)
    assert r.returncode != 0
    assert log.read_bytes() == b"# Decisions\n"


def test_g_log_twice_on_stored_name_appends_exactly_one_line(sdir, tmp_path):
    (tmp_path / "core").mkdir()
    log = tmp_path / "core" / "DECISION_LOG.md"
    log.write_bytes(b"# Decisions\n")
    assert _run_store_cwd(["put", "LOGGED_KEY"], sdir, tmp_path, DUMMY1.encode()).stdout.strip() == b"created"
    assert _run_store_cwd(["log", "LOGGED_KEY"], sdir, tmp_path).returncode == 0
    assert _run_store_cwd(["log", "LOGGED_KEY"], sdir, tmp_path).returncode == 0
    lines = [l for l in log.read_text(encoding="utf-8").splitlines() if "LOGGED_KEY" in l]
    assert len(lines) == 1, lines
    assert DUMMY1 not in log.read_text(encoding="utf-8")


def test_g_crash_between_temp_write_and_publish_leaves_nothing(sdir, monkeypatch):
    mod = _load_store()
    monkeypatch.setenv("HARNESS_SECRET_DIR", str(sdir))
    with monkeypatch.context() as m:
        def boom(*a, **k):
            raise RuntimeError("simulated crash")
        m.setattr(os, "link", boom)
        m.setattr(os, "rename", boom)
        with pytest.raises(RuntimeError):
            mod.put("CRASH_KEY", DUMMY1)
    assert os.listdir(sdir) == []
    assert mod.put("CRASH_KEY", DUMMY2) == "created"
    assert (sdir / "CRASH_KEY").read_bytes() == DUMMY2.encode("utf-8")


def test_g_non_ascii_utf8_value_stored_byte_exact_and_not_printed(sdir, tmp_path):
    raw = "секрет-ключ-ÄÖ-日本語-\u20ac".encode("utf-8")
    r = _run_store_cwd(["put", "UNI_KEY"], sdir, tmp_path, raw + b"\n")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == b"created"
    assert (sdir / "UNI_KEY").read_bytes() == raw
    assert raw not in r.stdout + r.stderr
    assert "секрет".encode("utf-8") not in r.stdout + r.stderr


# ---------- H: no-hardlink race, git not startable, stale lock (GPT-PM F1-F3) ----------
WRAPPER_TMPL = '''import importlib.util, os, subprocess, sys
{patch}
spec = importlib.util.spec_from_file_location("secret_store_wrapped", {store!r})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
sys.exit(mod.main(sys.argv[1:]))
'''
PATCH_NO_LINK = '''def _nolink(*a, **k):
    raise OSError("hard links not supported")
os.link = _nolink'''
PATCH_NO_GIT = '''def _nogit(*a, **k):
    raise FileNotFoundError("git not found")
subprocess.run = _nogit'''


def _wrapper(patch):
    d = Path(tempfile.mkdtemp(prefix="h_wrapper_"))
    w = d / "wrapper.py"
    w.write_text(WRAPPER_TMPL.format(patch=patch, store=str(STORE)), encoding="utf-8")
    return w


def _wenv(sdir):
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARNESS_")}
    env["HARNESS_SECRET_DIR"] = str(sdir)
    return env


def _wrun(w, args, sdir, value=""):
    return subprocess.run([PY, "-I", str(w)] + args, input=value, capture_output=True, text=True, env=_wenv(sdir), timeout=60)


def test_h1_no_hardlink_eight_concurrent_puts_one_created_value_matches_winner():
    w = _wrapper(PATCH_NO_LINK)
    sdir = Path(tempfile.mkdtemp(prefix="h1_secrets_")) / "store"
    sdir.mkdir()
    values = ["dummy-h1-value-%d-XYZ" % i for i in range(8)]
    procs = [subprocess.Popen([PY, "-I", str(w), "put", "SAMEKEY"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, env=_wenv(sdir)) for _ in values]
    results = []
    for p, v in zip(procs, values):
        o, e = p.communicate(v, timeout=120)
        results.append((v, o.strip(), p.returncode, e))
    outs = [r[1] for r in results]
    assert all(r[2] == 0 for r in results), results
    assert outs.count("created") == 1, outs
    assert outs.count("exists") == 7, outs
    winner = [r[0] for r in results if r[1] == "created"][0]
    assert (sdir / "SAMEKEY").read_bytes() == winner.encode("utf-8")
    left = sorted(os.listdir(sdir))
    assert left == ["SAMEKEY"], left
    assert not [n for n in left if n.startswith(".put-") or n.endswith(".lock")]


def test_h2_git_not_startable_inside_git_marker_refused_creates_nothing():
    w = _wrapper(PATCH_NO_GIT)
    base = Path(tempfile.mkdtemp(prefix="h2_repo_"))
    (base / ".git").mkdir()
    sdir = base / "sub" / "store"
    r = _wrun(w, ["put", "GITLESS_KEY"], sdir, DUMMY1)
    assert r.returncode != 0
    assert "created" not in r.stdout
    assert not sdir.exists()
    assert not (base / "sub").exists()
    assert sorted(os.listdir(base)) == [".git"]
    assert DUMMY1 not in r.stdout + r.stderr


def test_h2_git_not_startable_no_marker_put_works():
    w = _wrapper(PATCH_NO_GIT)
    base = Path(tempfile.mkdtemp(prefix="h2_plain_"))
    assert not any((q / ".git").exists() for q in (base, *base.parents)), "temp dir lies inside a git work tree"
    sdir = base / "store"
    r = _wrun(w, ["put", "PLAIN_KEY"], sdir, DUMMY1)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "created"
    assert (sdir / "PLAIN_KEY").read_bytes() == DUMMY1.encode("utf-8")
    assert not [n for n in os.listdir(sdir) if n.startswith(".put-")]


def test_h2_empty_path_inside_git_marker_refused():
    base = Path(tempfile.mkdtemp(prefix="h2_path_"))
    (base / ".git").mkdir()
    sdir = base / "store"
    env = _wenv(sdir)
    env["PATH"] = ""
    r = subprocess.run([PY, "-I", str(STORE), "put", "PATHLESS_KEY"], input=DUMMY1, capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode != 0
    assert "created" not in r.stdout
    assert not sdir.exists()


def test_h3_stale_lock_does_not_block_put_without_hardlink():
    w = _wrapper(PATCH_NO_LINK)
    sdir = Path(tempfile.mkdtemp(prefix="h3_secrets_")) / "store"
    sdir.mkdir()
    lock = sdir / "STALE_KEY.lock"
    lock.write_bytes(b"")
    old = lock.stat().st_mtime - 40
    os.utime(lock, (old, old))
    r = _wrun(w, ["put", "STALE_KEY"], sdir, DUMMY1)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "created"
    assert (sdir / "STALE_KEY").read_bytes() == DUMMY1.encode("utf-8")
    assert sorted(os.listdir(sdir)) == ["STALE_KEY"]


def test_h3_fresh_lock_with_existing_target_returns_exists_and_keeps_value():
    w = _wrapper(PATCH_NO_LINK)
    sdir = Path(tempfile.mkdtemp(prefix="h3_fresh_")) / "store"
    sdir.mkdir()
    (sdir / "HELD_KEY").write_bytes(DUMMY1.encode("utf-8"))
    (sdir / "HELD_KEY.lock").write_bytes(b"")
    r = _wrun(w, ["put", "HELD_KEY"], sdir, DUMMY2)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "exists"
    assert (sdir / "HELD_KEY").read_bytes() == DUMMY1.encode("utf-8")
    assert not [n for n in os.listdir(sdir) if n.startswith(".put-")]


def test_h3_fresh_lock_reaches_publish_and_returns_exists_without_overwrite():
    # put() short-circuits on an existing target, so call the publish step directly to exercise the lock branch.
    mod = _load_store()
    sdir = Path(tempfile.mkdtemp(prefix="h3_pub_"))
    target = sdir / "PUB_KEY"
    target.write_bytes(DUMMY1.encode("utf-8"))
    (sdir / "PUB_KEY.lock").write_bytes(b"")
    tmp = sdir / ".put-manual"
    tmp.write_bytes(DUMMY2.encode("utf-8"))

    def no_link(*a, **k):
        raise OSError("link not supported")
    orig = os.link
    os.link = no_link
    try:
        assert mod._publish(str(tmp), target) == "exists"
    finally:
        os.link = orig
    assert target.read_bytes() == DUMMY1.encode("utf-8")
