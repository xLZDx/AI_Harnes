"""Tests for _agentio.command_succeeded / run_git / config_path_problems and session_close_gate
(PR event recording gated on command success, warn_config_once).
Plain script: PASS/FAIL lines, final 'OK: 0 failing' or 'FAILED: n failing'. All state and config live in temp dirs."""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "harness" / "hooks"
TMP = Path(tempfile.mkdtemp(prefix="hook_helpers_"))
# state and config must point at temp BEFORE the modules are imported (they read env at import time)
os.environ["HARNESS_HOOK_STATE"] = str(TMP / "hookstate")
os.environ["HARNESS_HOOK_CONFIG"] = str(TMP / "no_config.json")
os.environ["SESSION_CLOSE_PLAN_TOOL"] = str(TMP / "no_tool.py")
os.environ.pop("SESSION_CLOSE_GATE_MODE", None)
sys.path.insert(0, str(HOOKS))


def load(name):
    spec = importlib.util.spec_from_file_location(name, HOOKS / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ag = load("_agentio")
sys.modules["_agentio"] = ag
sg = load("session_close_gate")

FAILS = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL") + ": " + name + ("" if cond else "  " + str(detail)))
    if not cond:
        FAILS.append(name)


# ---- (1) command_succeeded ----
TRUE_CASES = {
    "claude stdout/stderr/interrupted": {"stdout": "x", "stderr": "", "interrupted": False},
    "metadata exit_code 0": {"metadata": {"exit_code": 0}, "output": "x"},
    "top-level exit_code 0": {"exit_code": 0},
    "string Exit code: 0": "Exit code: 0",
}
for n, resp in TRUE_CASES.items():
    check("succeeded True: " + n, ag.command_succeeded({"tool_response": resp}) is True,
          ag.command_succeeded({"tool_response": resp}))

FALSE_CASES = {
    "is_error True": {"is_error": True, "stdout": "x"},
    "interrupted True": {"stdout": "x", "stderr": "", "interrupted": True},
    "exit_code 1": {"exit_code": 1},
    "string Exit code: 1 boom": "Exit code: 1\nboom",
    "string Process exited with code 2": "Process exited with code 2",
    "content list exited with code 2": [{"type": "text", "text": "exited with code 2"}],
    "JSON string metadata exit_code 3": '{"metadata":{"exit_code":3}}',
    "success False": {"success": False},
    "is_error wins over exit_code 0": {"is_error": True, "exit_code": 0},
    "interrupted wins over exit_code 0": {"interrupted": True, "exit_code": 0},
    "success False wins over exit_code 0": {"success": False, "exit_code": 0},
}
for n, resp in FALSE_CASES.items():
    r = ag.command_succeeded({"tool_response": resp})
    check("succeeded False: " + n, r is False, r)

UNKNOWN = {"empty dict": {}, "None": None, "plain text hello": "hello", "int": 5}
for n, resp in UNKNOWN.items():
    r = ag.command_succeeded({"tool_response": resp})
    check("succeeded None: " + n, r is None, r)
check("succeeded None: payload without tool_response", ag.command_succeeded({}) is None)

# ---- (2) run_git ----
r = ag.run_git(["--version"])
check("run_git --version returncode 0", r.returncode == 0 and "git version" in r.stdout, (r.returncode, r.stderr))
try:
    r = ag.run_git(["status"], cwd=TMP / "does" / "not" / "exist")
    check("run_git nonexistent cwd -> 127 without exception", r.returncode == 127 and r.stdout == "", r.returncode)
except Exception as exc:  # noqa: BLE001
    check("run_git nonexistent cwd -> 127 without exception", False, repr(exc))

check("GIT_TIMEOUT_S is 20", ag.GIT_TIMEOUT_S == 20)
seen = []
real_run = subprocess.run


def fake_timeout(cmd, **kw):
    seen.append(kw.get("timeout"))
    raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))


subprocess.run = fake_timeout
try:
    r = ag.run_git(["status"])
finally:
    subprocess.run = real_run
check("run_git TimeoutExpired -> returncode 124", r.returncode == 124, r.returncode)
check("run_git default timeout passed is 20", seen == [20], seen)


def fake_ok(cmd, **kw):
    seen.append(kw.get("timeout"))
    return subprocess.CompletedProcess(cmd, 0, "", "")


seen.clear()
subprocess.run = fake_ok
try:
    ag.run_git(["status"], timeout=3)
finally:
    subprocess.run = real_run
check("run_git explicit timeout overrides default", seen == [3], seen)

# ---- (3) config_path_problems ----
def with_config(cfg):
    p = TMP / "cfg.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    os.environ["HARNESS_HOOK_CONFIG"] = str(p)
    return ag.config_path_problems()


existing = TMP / "exists"
existing.mkdir()
missing = TMP / "missing_thing"
pr = with_config({"SESSION_CLOSE_PLAN_TOOL": str(missing / "tool.py")})
check("problems: missing tool path -> one entry naming key", len(pr) == 1 and "SESSION_CLOSE_PLAN_TOOL" in pr[0], pr)
pr = with_config({"PM_BRIDGE_STATE_DIR": str(existing / "newdir")})
check("problems: *_DIR missing but parent exists -> none", pr == [], pr)
pr = with_config({"PM_BRIDGE_STATE_DIR": str(missing / "a" / "b")})
check("problems: *_DIR missing and parent missing -> reported", len(pr) == 1 and "PM_BRIDGE_STATE_DIR" in pr[0], pr)
pr = with_config({"SESSION_CLOSE_PLAN_TOOL": "<path to tool>", "OTHER_DIR": "", "THIRD_ROOT": "   "})
check("problems: placeholder and empty values -> none", pr == [], pr)
os.environ["HARNESS_HOOK_CONFIG"] = str(TMP / "absent_config.json")
check("problems: missing config file -> []", ag.config_path_problems() == [])
bad = TMP / "bad.json"
bad.write_text("{not json", encoding="utf-8")
os.environ["HARNESS_HOOK_CONFIG"] = str(bad)
check("problems: invalid JSON config -> []", ag.config_path_problems() == [])
roots = os.pathsep.join([str(existing), str(missing / "gone")])
pr = with_config({"CLAUDE_GPT_REVIEW_ROOTS": roots})
check("problems: ROOTS with two paths, one missing -> only that one",
      len(pr) == 1 and "gone" in pr[0] and str(existing) not in pr[0], pr)

# ---- (4) session_close_gate PR event ----
os.environ["HARNESS_HOOK_CONFIG"] = str(TMP / "no_config.json")
plan = TMP / "proj"
plan.mkdir()
(plan / "PROJECT_PLAN.md").write_text("# plan\n", encoding="utf-8")


def pr_state():
    if sg.STATE.exists():
        sg.STATE.unlink()


def post(resp, cmd="gh pr create --title t --body b", with_resp=True):
    data = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(plan)}
    if with_resp:
        data["tool_response"] = resp
    sg.on_post_tool_use(data)
    return sg.load_state().get("pr", {})


pr_state()
rec = post({"stdout": "https://x/pull/1", "stderr": "", "interrupted": False})
check("gate: successful gh pr create records a pr entry",
      len(rec) == 1 and "gh pr create" in list(rec.values())[0]["cmd"], rec)
pr_state()
check("gate: exit code 1 string records nothing", post("Exit code: 1\nfailed") == {})
pr_state()
check("gate: is_error records nothing", post({"is_error": True, "stdout": "x"}) == {})
pr_state()
check("gate: unknown response shape records nothing", post({"foo": "bar"}) == {})
pr_state()
check("gate: missing tool_response records nothing", post(None, with_resp=False) == {})
pr_state()
check("gate: successful non-PR command records nothing", post({"exit_code": 0}, cmd="git status") == {})
pr_state()
check("gate: successful 'gh pr merge' via exit_code 0 records entry", len(post({"exit_code": 0}, cmd="gh pr merge 3")) == 1)

# ---- (5) warn_config_once ----
def run_warn():
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        sg.warn_config_once()
    return buf.getvalue()


pr_state()
cfg = TMP / "warn_cfg.json"
cfg.write_text(json.dumps({"SESSION_CLOSE_PLAN_TOOL": str(missing / "t.py")}), encoding="utf-8")
os.environ["HARNESS_HOOK_CONFIG"] = str(cfg)
first = run_warn()
second = run_warn()
check("warn: first call prints exactly one stderr line naming key",
      len(first.splitlines()) == 1 and "SESSION_CLOSE_PLAN_TOOL" in first, first)
check("warn: same problem set again prints nothing", second == "", second)
cfg.write_text(json.dumps({"SESSION_CLOSE_PLAN_TOOL": str(missing / "t.py"), "FOO_PATH": str(missing / "f")}),
               encoding="utf-8")
third = run_warn()
check("warn: changed problem set prints again", len(third.splitlines()) == 1 and "FOO_PATH" in third, third)
cfg.write_text("{}", encoding="utf-8")
check("warn: problems resolved prints nothing", run_warn() == "")
cfg.write_text(json.dumps({"SESSION_CLOSE_PLAN_TOOL": str(missing / "t.py")}), encoding="utf-8")
check("warn: problem reappearing after being cleared prints again", len(run_warn().splitlines()) == 1)

orig = ag.config_path_problems


def boom():
    raise RuntimeError("boom")


ag.config_path_problems = boom
try:
    sg.warn_config_once()
    check("warn: never raises when the check itself fails", True)
except Exception as exc:  # noqa: BLE001
    check("warn: never raises when the check itself fails", False, repr(exc))
finally:
    ag.config_path_problems = orig

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} failing")
    sys.exit(1)
print("OK: 0 failing")
