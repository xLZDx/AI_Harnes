"""S7-T6 tests: running-agent visibility (agent_visibility.py, agents_statusline.py, agent_model_gate.py announce,
install_claude.py statusLine). Hooks run as subprocesses the way the installed commands run them. Every test points
HARNESS_METRICS_DIR / HARNESS_HOOK_STATE (and the gate's own state folders) at fresh temp folders; fixtures use fake secrets only.
Run: C:\\Python314\\python.exe -I tests/test_agent_visibility.py"""
import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hv2  # noqa: E402

HOOKS = hv2.SRC / "harness" / "hooks"
PY = hv2.PY
SECRET = "FAKE-SECRET-123"


def sandbox(name):
    root = hv2.fresh(name)
    d = {"root": root, "metrics": root / "metrics", "state": root / "state", "home": root / "chome",
         "cwd": root / "proj"}
    d["home"].mkdir()
    d["cwd"].mkdir()
    (d["cwd"] / ".git").mkdir()  # stops the ledger search from walking above the fixture
    return d


def envof(sb):
    return {"HARNESS_METRICS_DIR": str(sb["metrics"]), "HARNESS_HOOK_STATE": str(sb["state"]),
            "CLAUDE_HOOK_STATE_DIR": str(sb["root"] / "dstate"), "CLAUDE_CONTROL_PLANE_HOME": str(sb["home"]),
            "CODEX_HOME": str(sb["root"] / "codexhome"), "CLAUDE_PROJECT_DIR": str(sb["cwd"]),
            "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet", "PYTHONIOENCODING": "utf-8"}


def hook(sb, script, payload, client="claude"):
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARNESS_")}
    env.update(envof(sb))
    cp = subprocess.run([PY, "-s", "-X", "utf8", str(HOOKS / script), f"--harness-client={client}"],
                        input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8", env=env,
                        cwd=str(sb["root"]), timeout=60)
    return cp.returncode, cp.stdout, cp.stderr


def spawn(sb, **ti):
    base = {"subagent_type": "code-reviewer", "model": "sonnet", "effort": "medium", "prompt": f"do it {SECRET}"}
    base.update(ti)
    return {"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_input": base, "cwd": str(sb["cwd"]),
            "session_id": "sess-1"}


def stop_payload(sb, agent="code-reviewer"):
    return {"hook_event_name": "SubagentStop", "agent_type": agent, "session_id": "sess-1", "cwd": str(sb["cwd"])}


def state_path(sb):
    return sb["state"] / "agents" / "active.json"


def agents_in_state(sb):
    return json.loads(state_path(sb).read_text(encoding="utf-8"))["agents"]


def seed(sb, entries):
    state_path(sb).parent.mkdir(parents=True, exist_ok=True)
    state_path(sb).write_text(json.dumps({"agents": entries}), encoding="utf-8")


def all_files_text(*dirs):
    out = []
    for d in dirs:
        if Path(d).exists():
            for f in Path(d).rglob("*"):
                if f.is_file():
                    out.append(f.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(out)


def write_ledger(sb, state):
    (sb["cwd"] / ".harness").mkdir(exist_ok=True)
    led = {"sprints": [{"id": "S9", "tasks": [{"id": "S9-T1", "state": "done"}, {"id": "S9-T2", "state": state}]}]}
    (sb["cwd"] / ".harness" / "ledger.json").write_text(json.dumps(led), encoding="utf-8")


def load_av():
    spec = importlib.util.spec_from_file_location("agent_visibility_under_test", HOOKS / "agent_visibility.py")
    sys.path.insert(0, str(HOOKS))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ---- criterion 1: lines and secrecy
def test_start_line_fields_and_no_secret_anywhere():
    sb = sandbox("av1")
    write_ledger(sb, "in_progress")
    rc, out, err = hook(sb, "agent_model_gate.py", spawn(sb))
    assert rc == 0, (rc, out, err)
    msg = json.loads(out)["systemMessage"]
    assert msg == "[agent START] code-reviewer | model sonnet | effort medium | task S9-T2", msg
    assert not re.search(r"\d+ s\b", msg), "duration must be on STOP only"
    assert SECRET not in out + err
    assert SECRET not in all_files_text(sb["state"], sb["metrics"], sb["root"] / "dstate")
    e = agents_in_state(sb)[0]
    assert (e["type"], e["model"], e["effort"], e["task"]) == ("code-reviewer", "sonnet", "medium", "S9-T2")
    assert "prompt" not in e and SECRET not in json.dumps(e)


def test_start_task_is_active_without_in_progress_task():
    sb = sandbox("av1b")
    write_ledger(sb, "pending")
    rc, out, _ = hook(sb, "agent_model_gate.py", spawn(sb))
    assert rc == 0 and json.loads(out)["systemMessage"].endswith("| task active"), out


def test_stop_line_has_type_and_duration_and_no_secret():
    sb = sandbox("av1c")
    seed(sb, [{"type": "code-reviewer", "model": "sonnet", "effort": "medium", "task": "S9-T2",
               "start": time.time() - 125, "session": "sess-1", "tool_use_id": ""}])
    p = stop_payload(sb)
    p["last_assistant_message"] = f"report {SECRET}"
    rc, out, err = hook(sb, "agent_visibility.py", p)
    assert rc == 0, err
    msg = json.loads(out)["systemMessage"]
    assert re.fullmatch(r"\[agent STOP\] code-reviewer \| 12[5-9] s", msg), msg
    assert agents_in_state(sb)[0].get("stop"), "entry must be finished"
    assert SECRET not in out + err + all_files_text(sb["state"], sb["metrics"])
    rows = [json.loads(x) for x in (sb["metrics"] / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["reason"] == "agent-stop" and rows[-1]["agent"] == "code-reviewer"
    assert 125000 <= rows[-1]["ms"] < 130000, rows[-1]


def test_stop_without_observed_start_says_so():
    sb = sandbox("av1d")
    rc, out, _ = hook(sb, "agent_visibility.py", stop_payload(sb, "ghost"))
    assert rc == 0 and json.loads(out)["systemMessage"] == "[agent STOP] ghost | 0 s | start was not observed", out


# ---- criterion 2: states
def test_state_of_each_level():
    av = load_av()
    now = 1_000_000.0
    assert av.state_of({"start": now - 5}, now) == "START_OBSERVED"
    assert av.state_of({"start": now - 599}, now) == "START_OBSERVED"
    assert av.state_of({"start": now - 601}, now) == "STATUS_UNKNOWN"
    assert av.state_of({"start": now - 7200}, now) == "STATUS_UNKNOWN"
    assert av.state_of({"start": now - 7200, "stop": now - 7000}, now) == "FINISHED_CONFIRMED"
    assert av.state_of({"start": now - 700, "activity": now - 30}, now) == "ACTIVE_CONFIRMED"
    assert av.state_of({"start": now - 700, "activity": now - 650}, now) == "STATUS_UNKNOWN"


def test_real_start_is_start_observed_and_old_entries_are_kept():
    sb = sandbox("av2")
    old = {"type": "tdd-guide", "model": "sonnet", "effort": "medium", "task": "active",
           "start": time.time() - 7200, "session": "", "tool_use_id": ""}
    seed(sb, [old])
    rc, _, err = hook(sb, "agent_model_gate.py", spawn(sb))
    assert rc == 0, err
    ents = agents_in_state(sb)
    assert len(ents) == 2 and ents[0]["type"] == "tdd-guide" and not ents[0].get("stop"), ents
    av = load_av()
    assert av.state_of(ents[0]) == "STATUS_UNKNOWN"
    assert av.state_of(ents[1]) == "START_OBSERVED"
    rc, _, _ = hook(sb, "agent_visibility.py", stop_payload(sb))
    ents = agents_in_state(sb)
    assert av.state_of(ents[1]) == "FINISHED_CONFIRMED" and not ents[0].get("stop")


def test_statusline_marks_unknown_and_hides_finished():
    sb = sandbox("av2b")
    now = time.time()
    seed(sb, [{"type": "tdd-guide", "start": now - 700},
              {"type": "code-reviewer", "start": now - 10},
              {"type": "done-agent", "start": now - 100, "stop": now - 50},
              {"type": "live-long", "start": now - 3000, "activity": now - 20}])
    rc, out, err = hook(sb, "agents_statusline.py", {})
    assert rc == 0, err
    assert out == "agents: tdd-guide?, code-reviewer, live-long | task active", out


def test_statusline_empty_and_corrupt_state():
    sb = sandbox("av2c")
    assert hook(sb, "agents_statusline.py", {})[1] == "agents: none | task active"
    state_path(sb).parent.mkdir(parents=True, exist_ok=True)
    state_path(sb).write_text("{broken", encoding="utf-8")
    rc, out, _ = hook(sb, "agents_statusline.py", {})
    assert rc == 0 and out == "agents: none | task active", out


# ---- criterion 3: client channels
def test_codex_start_goes_to_stderr_only():
    sb = sandbox("av3")
    p = {"hook_event_name": "PreToolUse", "tool_name": "spawn_agent", "cwd": str(sb["cwd"]), "session_id": "c1",
         "tool_input": {"agent_type": "code-reviewer", "model": "gpt-6-luna", "reasoning_effort": "medium",
                        "prompt": f"x {SECRET}"}}
    rc, out, err = hook(sb, "agent_model_gate.py", p, client="codex")
    assert rc == 0, err
    assert out == "", out
    assert "[agent START] code-reviewer | model gpt-6-luna | effort medium | task active" in err, err
    assert SECRET not in out + err + all_files_text(sb["state"], sb["metrics"])


def test_claude_start_is_json_system_message_not_stderr_line():
    sb = sandbox("av3b")
    rc, out, err = hook(sb, "agent_model_gate.py", spawn(sb))
    assert rc == 0
    assert list(json.loads(out)) == ["systemMessage"]
    assert "[agent START]" not in err


# ---- criterion 4: denied spawn
def test_denied_spawn_prints_no_start_and_writes_no_state():
    sb = sandbox("av4")
    rc, out, err = hook(sb, "agent_model_gate.py", spawn(sb, model="claude-opus-4-1"))
    assert rc == 0, err
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "[agent START]" not in out + err and "systemMessage" not in out
    assert not state_path(sb).exists() or agents_in_state(sb) == []
    log = sb["metrics"] / "events.jsonl"
    assert not log.exists() or "agent-start" not in log.read_text(encoding="utf-8")
    assert SECRET not in out + err


def test_codex_denied_spawn_prints_no_start():
    sb = sandbox("av4b")
    p = {"tool_name": "spawn_agent", "cwd": str(sb["cwd"]),
         "tool_input": {"agent_type": "code-reviewer", "model": "gpt-5-other", "reasoning_effort": "medium"}}
    rc, out, err = hook(sb, "agent_model_gate.py", p, client="codex")
    assert "[agent START]" not in out + err
    assert not state_path(sb).exists()


# ---- criterion 5: install_claude statusLine
def _install(settings):
    sb = sandbox("av5")
    src = sb["root"] / "src"
    (src / "hooks").mkdir(parents=True)
    cfg = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python", "args": ["x/run_hook.py", "a.py"]}]}]}}
    (src / "settings.hooks.json").write_text(json.dumps(cfg), encoding="utf-8")
    for n in ("a.py", "run_hook.py"):
        (src / "hooks" / n).write_text("pass", encoding="utf-8")
    (src / "hooks" / "agents_statusline.py").write_text((HOOKS / "agents_statusline.py").read_text(encoding="utf-8"),
                                                          encoding="utf-8")
    home = sb["root"] / "home"
    (home / "hooks").mkdir(parents=True)
    if settings is not None:
        (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("install_claude_t6", hv2.TOOLS / "install_claude.py")
    ic = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ic)
    with contextlib.redirect_stdout(io.StringIO()):
        rc = ic.install(home, sb["root"] / "snap", src)
    assert rc == 0
    return json.loads((home / "settings.json").read_text(encoding="utf-8")), home


def test_install_keeps_existing_statusline():
    mine = {"type": "command", "command": "my-own-status --fancy"}
    s, _ = _install({"statusLine": mine, "model": "x"})
    assert s["statusLine"] == mine, s
    assert s["model"] == "x"


def test_install_adds_statusline_when_missing():
    for start in (None, {"model": "x"}):
        s, home = _install(start)
        sl = s["statusLine"]
        assert sl["type"] == "command"
        assert (home / "hooks" / "agents_statusline.py").as_posix() in sl["command"], sl
        assert "-s -X utf8" in sl["command"]
        assert (home / "hooks" / "agents_statusline.py").is_file()


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
