"""Tests for the S7 review-fix batch (secret_guard failure event + Grep, GENERIC_VALUE_RE, ask_last_gate cap, _turn, plan_first_gate
counter, agent_visibility.mutate, run_hook client and budget, hook_metrics refusal/counters, metrics.py report, plan_ledger strictness,
session_close_gate ledger streak). Expected outcomes come from the fix-batch acceptance criteria, not from the code.

Everything runs against temp folders: HARNESS_HOOK_STATE, HARNESS_METRICS_DIR, HARNESS_HOOK_CONFIG and USERPROFILE/HOME are set for
every subprocess; nothing under the real ~/.harness-hooks is read or written. Secret-looking values are built from pieces at run time.
Run: C:\\Python314\\python.exe -I tests/test_s7_fixbatch.py"""
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hv2  # noqa: E402

HOOKS = hv2.SRC / "harness" / "hooks"
TOOLS = hv2.TOOLS
PY = hv2.PY

# in-process imports must never touch the real machine config or state
_BOOT = hv2.fresh("s7fix_boot")
os.environ["HARNESS_HOOK_CONFIG"] = str(_BOOT / "none.json")
os.environ["HARNESS_HOOK_STATE"] = str(_BOOT / "state")
os.environ["HARNESS_METRICS_DIR"] = str(_BOOT / "metrics")
sys.path.insert(0, str(HOOKS))
sys.path.insert(0, str(TOOLS))
import hook_metrics as hm  # noqa: E402
import secret_policy as sp  # noqa: E402
import ask_last_gate as alg  # noqa: E402
import _turn  # noqa: E402
import plan_ledger  # noqa: E402

GENERIC = "Xk29Pq7L" + "mN4rTs8U" + "vW3yZa6B" + "1c5D"


# ---------------------------------------------------------------- shared helpers
def sandbox(name):
    root = hv2.fresh(name)
    sb = {"root": root, "state": root / "state", "metrics": root / "metrics", "home": root / "home"}
    sb["home"].mkdir()
    return sb


def envof(sb, extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARNESS_")}
    env.update({"HARNESS_HOOK_STATE": str(sb["state"]), "HARNESS_METRICS_DIR": str(sb["metrics"]),
                "HARNESS_HOOK_CONFIG": str(sb["root"] / "none.json"), "USERPROFILE": str(sb["home"]), "HOME": str(sb["home"]),
                "CLAUDE_HOOK_STATE_DIR": str(sb["root"] / "dstate"), "PYTHONIOENCODING": "utf-8"})
    env.update(extra or {})
    return env


def run_script(sb, script, payload, args=(), extra_env=None, hooks_dir=HOOKS, raw=None):
    cp = subprocess.run([PY, "-s", "-X", "utf8", str(Path(hooks_dir) / script), *args],
                        input=raw if raw is not None else json.dumps(payload), capture_output=True, text=True, encoding="utf-8",
                        env=envof(sb, extra_env), cwd=str(sb["root"]), timeout=120)
    return cp.returncode, cp.stdout.strip(), cp.stderr


def metric_rows(sb):
    f = sb["metrics"] / "events.jsonl"
    if not f.exists():
        return []
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]


def write_transcript(sb, human, calls=(), ts="2026-11-12T10:00:00Z"):
    """Claude-format JSONL. calls: (tool name, input, result text, is_error)."""
    rows = []
    if human is not None:
        rows.append({"type": "user", "timestamp": ts, "message": {"role": "user", "content": human}})
    for name, inp, result, is_err in calls:
        tid = "toolu_" + uuid.uuid4().hex[:10]
        rows.append({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}})
        rows.append({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": result,
                                                                               "is_error": is_err}]}})
    p = sb["root"] / (uuid.uuid4().hex[:8] + ".jsonl")
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8", newline="\n")
    return str(p)


class use_metrics_dir:
    """Point in-process hook_metrics at a folder."""

    def __init__(self, d):
        self.d = d

    def __enter__(self):
        self.old = os.environ.get("HARNESS_METRICS_DIR")
        os.environ["HARNESS_METRICS_DIR"] = str(self.d)

    def __exit__(self, *a):
        os.environ["HARNESS_METRICS_DIR"] = self.old


# ================================================================ secret_guard: PostToolUseFailure
def sg_post(sb, event, cmd, **kw):
    payload = {"hook_event_name": event, "tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": "s1", "cwd": str(sb["root"])}
    payload.update(kw)
    return run_script(sb, "secret_guard.py", payload)


def sg_records(sb):
    f = sb["state"] / "evidence" / "records.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []


def sg_pre(sb, tool, tool_input):
    payload = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input, "session_id": "s1", "cwd": str(sb["root"]),
               "transcript_path": str(sb["root"] / "missing.jsonl")}
    rc, out, err = run_script(sb, "secret_guard.py", payload)
    try:
        dec = json.loads(out)["hookSpecificOutput"].get("permissionDecision", "")
    except (ValueError, KeyError, TypeError):
        dec = ""
    return rc, dec, out, err


def test_failure_event_records_credential_invalid_for_gcloud_and_wrangler():
    for cmd, svc, proj in [("gcloud projects describe p --project p", "gcp", "p"), ("wrangler whoami", "cloudflare", "")]:
        sb = sandbox("s7sg_fail")
        rc, out, err = sg_post(sb, "PostToolUseFailure", cmd, error="Exit code 1\nERROR: (cli) 401 Unauthorized: invalid authentication credentials")
        assert rc == 0, (rc, err)
        rows = [r for r in sg_records(sb) if r.get("kind") == "auth"]
        assert len(rows) == 1, (cmd, rows)
        assert rows[0]["class"] == "credential-invalid" and rows[0]["service"] == svc and rows[0]["project"] == proj, rows
        assert rows[0]["credential_id"] == "default:" + svc, rows


def test_failure_event_evidence_unlocks_a_change_for_same_service_and_project_only():
    sb = sandbox("s7sg_unlock")
    sg_post(sb, "PostToolUseFailure", "gcloud projects describe p --project p", error="Exit code 1\n401 Unauthorized")
    create = "gcloud iam service-accounts keys create k.json --iam-account x --project p"
    assert sg_pre(sb, "Bash", {"command": create})[1] == ""
    assert sg_pre(sb, "Bash", {"command": create.replace("--project p", "--project q")})[1] == "deny"


def test_failure_event_with_unclassifiable_text_records_nothing():
    sb = sandbox("s7sg_unk")
    sg_post(sb, "PostToolUseFailure", "gcloud projects describe p --project p", error="Exit code 1\nsomething odd happened")
    assert [r for r in sg_records(sb) if r.get("kind") == "auth"] == []
    assert sg_pre(sb, "Bash", {"command": "gcloud iam service-accounts keys create k.json --iam-account x --project p"})[1] == "deny"


def test_post_tool_use_with_unknown_success_records_nothing():
    cmd = "gcloud projects describe p --project p"
    sb = sandbox("s7sg_none")
    # no exit code anywhere in the response: command_succeeded is None, so the 401 text must NOT become evidence
    sg_post(sb, "PostToolUse", cmd, tool_response="HTTP 401 Unauthorized")
    assert sg_records(sb) == []
    # control: the same text with a proven failing exit code is recorded
    sb2 = sandbox("s7sg_none2")
    sg_post(sb2, "PostToolUse", cmd, tool_response="Exit code: 1\nHTTP 401 Unauthorized")
    assert [r["class"] for r in sg_records(sb2) if r.get("kind") == "auth"] == ["credential-invalid"]


# ================================================================ secret_guard: Grep tool, registry, build
def test_grep_content_on_secret_file_is_denied():
    sb = sandbox("s7sg_grep")
    misses = []
    for ti in [{"pattern": "KEY", "path": "D:/proj/.env", "output_mode": "content"},
               {"pattern": "KEY", "path": "D:/proj/infra", "glob": "*.tfvars", "output_mode": "content"},
               {"pattern": "KEY", "glob": "**/.env.production", "output_mode": "content"}]:
        rc, dec, out, err = sg_pre(sb, "Grep", ti)
        if not (rc == 0 and dec == "deny" and "output_mode" in out):
            misses.append((ti, out, err))
    assert not misses, "Grep content on a secret file was not denied: %r" % (misses,)


def test_grep_count_files_with_matches_and_other_files_are_allowed():
    sb = sandbox("s7sg_grep2")
    for ti in [{"pattern": "KEY", "path": "D:/proj/.env", "output_mode": "count"},
               {"pattern": "KEY", "path": "D:/proj/.env", "output_mode": "files_with_matches"},
               {"pattern": "KEY", "path": "D:/proj/infra", "glob": "*.tfvars", "output_mode": "count"},
               {"pattern": "KEY", "path": "D:/proj/.env"},  # default mode is files_with_matches
               {"pattern": "KEY", "path": "D:/proj/src/app.py", "output_mode": "content"},
               {"pattern": "KEY", "path": "D:/proj", "glob": "*.py", "output_mode": "content"}]:
        rc, dec, out, err = sg_pre(sb, "Grep", ti)
        assert rc == 0 and dec == "" and out == "", (ti, out, err)


def test_registry_and_build_wire_grep_and_post_tool_use_failure():
    reg = json.loads((HOOKS / "registry.json").read_text(encoding="utf-8"))
    pre = [h for h in reg["hooks"] if h["script"] == "secret_guard.py" and h["event"] == "PreToolUse"]
    assert len(pre) == 1 and "Grep" in pre[0]["claude"].split("|"), pre
    assert "Grep" not in pre[0].get("codex", "").split("|")  # Codex has no Grep tool: its matcher is untouched
    fail = [h for h in reg["hooks"] if h["script"] == "secret_guard.py" and h["event"] == "PostToolUseFailure"]
    assert len(fail) == 1 and fail[0].get("claude") and "codex" not in fail[0], fail
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_under_test", TOOLS / "build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    assert "PostToolUseFailure" in build.HOOK_EVENTS["claude"]
    assert "PostToolUseFailure" not in build.HOOK_EVENTS["codex"]
    assert build.check_hooks() == [], build.check_hooks()
    claude = build.hook_config("claude", Path("C:/h/hooks"), "py")
    codex = build.hook_config("codex", Path("C:/h/hooks"), "py")
    assert "PostToolUseFailure" in claude and any("secret_guard.py" in json.dumps(g) for g in claude["PostToolUseFailure"]), list(claude)
    assert "PostToolUseFailure" not in codex, list(codex)
    # the built artefacts agree (they are committed output of tools/build.py)
    out_claude = json.loads((hv2.SRC / "out" / "claude" / "settings.hooks.json").read_text(encoding="utf-8"))["hooks"]
    out_codex = json.loads((hv2.SRC / "out" / "codex" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert "PostToolUseFailure" in out_claude and "PostToolUseFailure" not in out_codex


# ================================================================ secret_guard: inline interpreter, multi-line
def test_inline_python_printing_env_secret_still_denied_but_length_allowed():
    sb = sandbox("s7sg_inline")
    assert sg_pre(sb, "Bash", {"command": "python -c \"import os; print(os.environ['API_TOKEN'])\""})[1] == "deny"
    assert sg_pre(sb, "Bash", {"command": "python -c \"import os; print(len(os.environ['API_TOKEN']))\""})[1] == ""


def test_multiline_command_with_secret_read_on_second_line_is_denied():
    sb = sandbox("s7sg_ml")
    assert sg_pre(sb, "Bash", {"command": "cd x\ncat .env"})[1] == "deny"
    assert sg_pre(sb, "Bash", {"command": "cd x\nls -l"})[1] == ""
    assert sg_pre(sb, "Bash", {"command": "cd x\ngrep -c KEY .env"})[1] == ""  # name-only count on the second line stays allowed


# ================================================================ secret_policy: linear regex
def test_generic_value_regex_is_linear_on_a_long_adversarial_input():
    for text in ("a-" * 50000, "x_" * 50000 + "token", ("token-" * 20000)):
        t0 = time.monotonic()
        sp.find_secrets(text)
        dt = time.monotonic() - t0
        assert dt < 2.0, f"find_secrets took {dt:.2f}s on {len(text)} chars"
    # the optimisation keeps detecting a real labelled value
    hits = sp.find_secrets("config api_token=" + GENERIC)
    assert [h["type"] for h in hits] == ["labelled-value"] and hits[0]["known"] is False, hits
    assert sp.find_secrets("a-" * 50 + " nothing labelled here") == []


# ================================================================ ask_last_gate
def alg_ask(sb, question, tp, session="sess-A", extra_env=None):
    qs = [{"question": question, "options": [{"label": "Yes", "description": ""}, {"label": "No", "description": ""}]}]
    payload = {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion", "tool_input": {"questions": qs},
               "transcript_path": tp, "session_id": session, "cwd": str(sb["root"])}
    return run_script(sb, "ask_last_gate.py", payload, args=("--harness-client=claude",), extra_env=extra_env)


def is_deny(out):
    try:
        return json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    except (ValueError, KeyError, TypeError):
        return False


def test_ask_last_decision_question_is_not_gated_but_a_request_without_proof_is_denied():
    sb = sandbox("s7alg_a")
    tp = write_transcript(sb, "Deploy the stack")
    rc, out, err = alg_ask(sb, "Should I rotate the key?", tp)
    assert rc == 0 and out == "", (rc, out, err)
    rc, out, err = alg_ask(sb, "Please give me the API key", tp)
    assert rc == 0 and is_deny(out), (rc, out, err)
    assert "missing proof" in out and "do it yourself" in out.lower()


def test_ask_last_third_denial_in_one_turn_is_released_and_counted():
    sb = sandbox("s7alg_b")
    tp = write_transcript(sb, "Deploy the stack")
    q = "Please give me the API key"
    first = alg_ask(sb, q, tp)
    second = alg_ask(sb, q, tp)
    assert is_deny(first[1]) and is_deny(second[1]), (first, second)
    assert metric_rows(sb) == [], "denials are not releases"
    third = alg_ask(sb, q, tp)
    assert third[0] == 0 and third[1] == "", third
    rel = [r for r in metric_rows(sb) if r["decision"] == "release"]
    assert len(rel) == 1 and rel[0]["hook"] == "ask_last_gate.py" and rel[0]["reason"] == "ask-last-exception", metric_rows(sb)
    # the cap is per session and turn: another session is still denied
    other = alg_ask(sb, q, tp, session="sess-B")
    assert is_deny(other[1]), other
    # and a new turn (new human message marker) of the first session starts counting again
    tp2 = write_transcript(sb, "Deploy the stack again", ts="2026-11-12T11:00:00Z")
    assert is_deny(alg_ask(sb, q, tp2)[1])


def test_ask_last_failed_ignores_calls_refused_by_a_hook():
    refused = {"result": "PreToolUse:Bash hook blocking error: Bash denied by hook: Secret guard: it prints a secret", "is_error": True}
    assert alg.failed(refused) is False
    plain = {"result": "Error: terraform exited with status 3", "is_error": True}
    assert alg.failed(plain) is True
    classified = {"result": "ERROR: 401 Unauthorized", "is_error": False}
    assert alg.failed(classified) is True
    transient = {"result": "ERROR: request timed out", "is_error": True}
    assert alg.failed(transient) is False
    ok = {"result": "all good", "is_error": False}
    assert alg.failed(ok) is False


# ================================================================ _turn.read_turn
def test_read_turn_without_a_genuine_human_row_is_empty():
    sb = sandbox("s7turn")
    calls = [("Bash", {"command": "ls"}, "a b", False)]
    only_machine = write_transcript(sb, None, calls)
    t = _turn.read_turn(only_machine)
    assert t["calls"] == [] and t["human_text"] == "" and t["marker"] == "", t
    # automated user rows are not human rows either
    p = Path(only_machine)
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    rows.insert(0, {"type": "user", "timestamp": "t0", "message": {"role": "user", "content": "<system-reminder>auto</system-reminder>"}})
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    t = _turn.read_turn(only_machine)
    assert t["calls"] == [] and t["human_text"] == "", t
    assert _turn.read_turn(str(sb["root"] / "does-not-exist.jsonl"))["calls"] == []
    # control: the same calls after a real human message are returned
    with_human = write_transcript(sb, "do the thing", calls)
    t = _turn.read_turn(with_human)
    assert t["human_text"] == "do the thing" and [c["name"] for c in t["calls"]] == ["Bash"] and t["calls"][0]["result"] == "a b", t


# ================================================================ plan_first_gate
def pf_call(sb, tp, session="sess-P"):
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Write", "tool_input": {"file_path": "D:/proj/src/app.py", "content": "x = 1"},
               "session_id": session, "transcript_path": tp, "cwd": str(sb["root"])}
    return run_script(sb, "plan_first_gate.py", payload)


def test_plan_first_denies_a_change_without_a_plan():
    sb = sandbox("s7pf_a")
    rc, out, err = pf_call(sb, write_transcript(sb, "Implement the feature"))
    assert rc == 0 and is_deny(out), (rc, out, err)
    assert metric_rows(sb) == []


def test_plan_first_unwritable_counter_releases_instead_of_denying():
    sb = sandbox("s7pf_b")
    (sb["state"] / "plan_first" / "denies.json").mkdir(parents=True)  # a directory where the counter file should be
    rc, out, err = pf_call(sb, write_transcript(sb, "Implement the feature"))
    assert rc == 0 and out == "", (rc, out, err)
    rel = [r for r in metric_rows(sb) if r["decision"] == "release"]
    assert len(rel) == 1 and rel[0]["hook"] == "plan_first_gate.py" and rel[0]["reason"] == "release-counter-unavailable", metric_rows(sb)


def test_plan_first_empty_turn_is_released_with_a_metrics_row():
    sb = sandbox("s7pf_c")
    rc, out, err = pf_call(sb, write_transcript(sb, None))  # no genuine human row, no calls
    assert rc == 0 and out == "", (rc, out, err)
    rel = [r for r in metric_rows(sb) if r["decision"] == "release"]
    assert len(rel) == 1 and rel[0]["reason"] == "fail-open-empty-turn", metric_rows(sb)
    # a missing transcript is the same
    rc, out, err = pf_call(sb, str(sb["root"] / "missing.jsonl"))
    assert out == "" and len([r for r in metric_rows(sb) if r["decision"] == "release"]) == 2


# ================================================================ agent_visibility.mutate
MUTATE_CODE = ("import sys, time\n"
               "sys.path.insert(0, sys.argv[1])\n"
               "import agent_visibility as av\n"
               "t0 = float(sys.argv[2]); i = sys.argv[3]\n"
               "while time.time() < t0: pass\n"
               "print(av.mutate(lambda d: d['agents'].append({'type': 'agent' + i, 'start': 1.0})))\n")


def test_mutate_loses_no_update_with_eight_concurrent_processes():
    sb = sandbox("s7av_conc")
    t0 = time.time() + 4.0  # all eight start together once Python is up
    procs = [subprocess.Popen([PY, "-s", "-X", "utf8", "-c", MUTATE_CODE, str(HOOKS), str(t0), str(i)], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, env=envof(sb), cwd=str(sb["root"])) for i in range(8)]
    outs = [p.communicate(timeout=60) for p in procs]
    assert all(p.returncode == 0 for p in procs), outs
    assert [o[0].strip() for o in outs] == ["True"] * 8, outs
    d = json.loads((sb["state"] / "agents" / "active.json").read_text(encoding="utf-8"))
    assert sorted(a["type"] for a in d["agents"]) == sorted(f"agent{i}" for i in range(8)), d
    assert not (sb["state"] / "agents" / "active.json.lock").exists(), "the lock must be released"


def test_mutate_with_a_held_lock_returns_false_and_changes_nothing():
    sb = sandbox("s7av_lock")
    d = sb["state"] / "agents"
    d.mkdir(parents=True)
    (d / "active.json").write_text(json.dumps({"agents": [{"type": "keep", "start": 1.0}]}), encoding="utf-8")
    (d / "active.json.lock").write_text("99999", encoding="utf-8")  # fresh lock held by someone else
    t0 = time.time()
    cp = subprocess.run([PY, "-s", "-X", "utf8", "-c", MUTATE_CODE, str(HOOKS), str(t0), "X"], capture_output=True, text=True,
                        env=envof(sb), cwd=str(sb["root"]), timeout=60)
    assert cp.stdout.strip() == "False", cp
    assert [a["type"] for a in json.loads((d / "active.json").read_text(encoding="utf-8"))["agents"]] == ["keep"]
    assert (d / "active.json.lock").exists(), "a lock we did not take must stay"


# ================================================================ run_hook
STUB = "import sys\nsys.argv[:] = sys.argv[:1]  # strips --harness-client like a hook that parses its own argv\nsys.stdout.write('{}')\n"


def hooks_copy(sb, recorder=None):
    import shutil
    hd = sb["root"] / "hooks_copy"
    shutil.copytree(HOOKS, hd, ignore=shutil.ignore_patterns("__pycache__"))
    (hd / "argv_stub.py").write_text(STUB, encoding="utf-8")
    if recorder is not None:
        (hd / "hook_metrics.py").write_text(recorder, encoding="utf-8")
    return hd


def via_runner(sb, hd, script, payload, flag):
    return run_script(sb, "run_hook.py", payload, args=(script, *flag), hooks_dir=hd)


def test_run_hook_records_client_codex_even_when_the_hook_strips_argv():
    sb = sandbox("s7rh_client")
    hd = hooks_copy(sb)
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "echo hi"}, "session_id": "s1", "cwd": str(sb["root"])}
    rc, out, err = via_runner(sb, hd, "argv_stub.py", payload, ("--harness-client=codex",))
    assert rc == 0 and out == "{}", (rc, out, err)
    rows = metric_rows(sb)
    assert [r["client"] for r in rows] == ["codex"], rows
    rc, out, err = via_runner(sb, hd, "argv_stub.py", payload, ("--harness-client=claude",))
    rc2, out2, err2 = via_runner(sb, hd, "argv_stub.py", payload, ())
    assert [r["client"] for r in metric_rows(sb)] == ["codex", "claude", "claude"], metric_rows(sb)
    # the real hook that strips its flag in _agentio (agent_visibility), from the real hooks folder
    sb2 = sandbox("s7rh_client2")
    stop = {"hook_event_name": "SubagentStop", "agent_type": "code-reviewer", "session_id": "s1", "cwd": str(sb2["root"])}
    rc, out, err = via_runner(sb2, HOOKS, "agent_visibility.py", stop, ("--harness-client=codex",))
    assert rc == 0, err
    rows = [r for r in metric_rows(sb2) if r["hook"] == "agent_visibility.py"]
    assert rows and all(r["client"] == "codex" for r in rows), rows


def test_run_hook_bookkeeping_never_delays_the_result_much():
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "echo hi"}, "session_id": "s1", "cwd": "C:/w/p"}
    # baseline: the stub on its own
    sb0 = sandbox("s7rh_base")
    hd0 = hooks_copy(sb0)
    t0 = time.monotonic()
    run_script(sb0, "argv_stub.py", payload, hooks_dir=hd0)
    base = time.monotonic() - t0
    # (a) the metrics lock is held by someone else
    sb = sandbox("s7rh_lock")
    hd = hooks_copy(sb)
    sb["metrics"].mkdir()
    (sb["metrics"] / "events.jsonl.lock").write_text("99999", encoding="utf-8")
    t0 = time.monotonic()
    rc, out, err = via_runner(sb, hd, "argv_stub.py", payload, ("--harness-client=claude",))
    held = time.monotonic() - t0
    assert rc == 0 and out == "{}", (rc, out, err)
    assert held - base < 1.5 + 0.7, f"lock held: {held:.2f}s vs {base:.2f}s without the runner"  # 0.7 s slack for the runner's own imports
    assert metric_rows(sb) == [] and (sb["metrics"] / "dropped.count").read_text(encoding="utf-8") == "1"
    # (b) the recorder hangs for ever: the hard budget cuts it, the result is already correct
    sb3 = sandbox("s7rh_hang")
    hd3 = hooks_copy(sb3, recorder="import time\ndef record(*a, **k):\n    time.sleep(60)\n")
    t0 = time.monotonic()
    rc, out, err = via_runner(sb3, hd3, "argv_stub.py", payload, ("--harness-client=claude",))
    hung = time.monotonic() - t0
    assert rc == 0 and out == "{}", (rc, out, err)
    assert hung - base < 1.5 + 1.0 + 0.7, f"hung recorder: {hung:.2f}s vs {base:.2f}s"
    # (c) control: with a free lock the row is written
    sb4 = sandbox("s7rh_free")
    via_runner(sb4, hooks_copy(sb4), "argv_stub.py", payload, ("--harness-client=claude",))
    assert len(metric_rows(sb4)) == 1


# ================================================================ hook_metrics
def test_metrics_folder_inside_git_work_tree_is_refused_and_nothing_is_created():
    sb = sandbox("s7hm_git")
    repo = sb["root"] / "repo"
    (repo / ".git").mkdir(parents=True)
    with use_metrics_dir(repo / "deep" / "metrics"):
        assert hm.record("h.py", "PreToolUse", "deny") is False
        assert hm.counters() == {"dropped": 0, "errors": 0, "refused": 0}  # folder absent: no counter file can be created either
    assert sorted(p.name for p in repo.rglob("*")) == [".git"], list(repo.rglob("*"))
    # a folder that already exists inside the tree: the refusal is counted, but no event row or lock is created
    existing = repo / "metrics"
    existing.mkdir()
    with use_metrics_dir(existing):
        assert hm.record("h.py", "PreToolUse", "deny") is False
        assert hm.record("h.py", "PreToolUse", "deny") is False
        assert hm.counters() == {"dropped": 0, "errors": 0, "refused": 2}, hm.counters()
    assert sorted(p.name for p in existing.iterdir()) == ["refused.count"], list(existing.iterdir())
    # control: outside a work tree the same call writes a row
    ok = sb["root"] / "plain" / "metrics"
    with use_metrics_dir(ok):
        assert hm.record("h.py", "PreToolUse", "allow") is True
        assert hm.counters()["refused"] == 0


def test_counters_report_dropped_errors_and_refused_separately():
    sb = sandbox("s7hm_cnt")
    d = sb["metrics"]
    d.mkdir()
    (d / ".perm_done").write_text("1", encoding="utf-8")  # skip the icacls step: it must not add errors here
    # dropped: the lock is held
    (d / "events.jsonl.lock").write_text("99999", encoding="utf-8")
    with use_metrics_dir(d):
        assert hm.record("h.py", "PreToolUse", "allow") is False
        assert hm.counters() == {"dropped": 1, "errors": 0, "refused": 0}, hm.counters()
    (d / "events.jsonl.lock").unlink()
    # errors: the active file cannot be appended to (it is a directory)
    (d / "events.jsonl").mkdir()
    with use_metrics_dir(d):
        assert hm.record("h.py", "PreToolUse", "allow") is False
        assert hm.counters() == {"dropped": 1, "errors": 1, "refused": 0}, hm.counters()
    assert not (d / "events.jsonl.lock").exists(), "the lock must be released after the failure"


def metrics_cli(sb, *args):
    cp = subprocess.run([PY, "-I", str(TOOLS / "metrics.py"), *args, "--ledger", str(sb["root"] / "noledger.json")], capture_output=True,
                        text=True, encoding="utf-8", env=envof(sb), cwd=str(sb["root"]), timeout=120)
    return cp.returncode, cp.stdout, cp.stderr


def metric_row(ts, decision="allow", hook="h.py"):
    return {"ts": ts.isoformat(), "client": "claude", "session": "s", "hook": hook, "event": "PreToolUse", "decision": decision,
            "reason": "", "ms": 5, "project": "p", "agent": ""}


def test_metrics_report_warns_when_newest_row_is_older_than_24h():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    sb = sandbox("s7mc_old")
    sb["metrics"].mkdir()
    (sb["metrics"] / "events.jsonl").write_text(json.dumps(metric_row(now - timedelta(hours=30))) + "\n", encoding="utf-8")
    rc, out, err = metrics_cli(sb, "--days", "3")
    assert rc == 0 and "WARNING: the newest row is" in out, (out, err)
    sb2 = sandbox("s7mc_new")
    sb2["metrics"].mkdir()
    (sb2["metrics"] / "events.jsonl").write_text(json.dumps(metric_row(now - timedelta(hours=1))) + "\n", encoding="utf-8")
    rc, out, err = metrics_cli(sb2, "--days", "3")
    assert rc == 0 and "WARNING" not in out, (out, err)
    sb3 = sandbox("s7mc_none")
    rc, out, err = metrics_cli(sb3, "--days", "3")
    assert "WARNING: no metrics rows" in out


def test_metrics_report_counts_unknown_decisions_as_other_and_shows_counters():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc) - timedelta(minutes=5)
    sb = sandbox("s7mc_other")
    sb["metrics"].mkdir()
    rows = [metric_row(now, "allow"), metric_row(now, "weird-decision"), metric_row(now, "deny"), metric_row(now, "release")]
    (sb["metrics"] / "events.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    (sb["metrics"] / "refused.count").write_text("3", encoding="utf-8")
    (sb["metrics"] / "errors.count").write_text("2", encoding="utf-8")
    rc, out, err = metrics_cli(sb, "--json")
    assert rc == 0, err
    res = json.loads(out)
    h = res["hooks"]["h.py"]
    assert (h["fired"], h["allow"], h["deny"], h["release"], h["other"]) == (4, 1, 1, 1, 1), h
    assert res["counters"] == {"dropped": 0, "errors": 2, "refused": 3}, res["counters"]
    rc, text, err = metrics_cli(sb)
    assert "recording errors: 2" in text and "refused (folder inside git): 3" in text, text


# ================================================================ plan_ledger and report
def plan(rows, header="| Task | Title | Status | Commit |", sep="|---|---|---|---|"):
    return "## Sprint S1 - first\n\n" + header + "\n" + sep + "\n" + "\n".join(rows) + "\n"


def raises_value_error(fn, *needles):
    try:
        fn()
    except ValueError as e:
        assert all(n.lower() in str(e).lower() for n in needles), (needles, str(e))
        return True
    return False


def test_plan_ledger_unknown_status_missing_column_and_no_sprints_raise():
    assert raises_value_error(lambda: plan_ledger.parse(plan(["| T1 | a | WHATEVER | |"])), "unknown status")
    assert raises_value_error(lambda: plan_ledger.parse(plan(["| T1 | a |"], header="| Task | Title |", sep="|---|---|")), "status")
    assert raises_value_error(lambda: plan_ledger.parse("# Plan\n\nNothing planned yet.\n"), "sprint")
    # controls: a known status parses
    d = plan_ledger.parse(plan(["| T1 | a | NOT_STARTED | |"]))
    assert [t["state"] for t in d["sprints"][0]["tasks"]] == ["todo"]


def test_plan_ledger_accepted_task_has_no_invented_verify_or_review():
    d = plan_ledger.parse(plan(["| T1 | a | ACCEPTED | abc1234 |", "| T2 | b | IN_REVIEW | |"]))
    t1, t2 = d["sprints"][0]["tasks"]
    assert t1["state"] == "done" and t1["verify"] is None and t1["review"] is None, t1
    assert t1["commits"] == ["abc1234"]
    assert t2["state"] == "verified" and t2["verify"] is None and t2["review"] is None, t2


def test_plan_ledger_compound_accepted_statuses():
    d = plan_ledger.parse(plan(["| T1 | a | ACCEPTED BOUNDED TEST DELTA / PUSHED | |", "| T2 | b | ACCEPTED | |"]))
    assert [t["state"] for t in d["sprints"][0]["tasks"]] == ["verified", "done"]  # bounded delta is never `done`
    assert raises_value_error(lambda: plan_ledger.parse(plan(["| T1 | a | ACCEPTED_X | |"])), "unknown status")
    assert raises_value_error(lambda: plan_ledger.parse(plan(["| T1 | a | ACCEPTED SOMETHING | |"])), "unknown status")


def test_report_keeps_the_existing_report_when_the_plan_parses_to_zero_tasks():
    repo = hv2.mk_repo("s7rep")
    out = repo / "reports" / "rep.html"
    (repo / "PROJECT_PLAN.md").write_text(plan(["| T1 | a | ACCEPTED | |", "| T2 | b | NOT_STARTED | |"]), encoding="utf-8", newline="\n")
    r = hv2.tool(repo, "report.py", "--out", str(out))
    assert r.rc == 0 and out.is_file(), r.all
    before = out.read_text(encoding="utf-8")
    assert "harness-report v1" in before
    # the plan now yields no tasks (empty table): the old report must survive and the exit code must say so
    (repo / "PROJECT_PLAN.md").write_text(plan([]), encoding="utf-8", newline="\n")
    r = hv2.tool(repo, "report.py", "--out", str(out))
    assert r.rc == 1, r.all
    assert "0 tasks" in r.err and "left as it is" in r.err, r.err
    assert out.read_text(encoding="utf-8") == before
    # a plan with no sprint section behaves the same
    (repo / "PROJECT_PLAN.md").write_text("# nothing\n", encoding="utf-8", newline="\n")
    r = hv2.tool(repo, "report.py", "--out", str(out))
    assert r.rc == 1 and out.read_text(encoding="utf-8") == before, r.all


# ================================================================ session_close_gate ledger streak
def mk_task(tid, state="todo"):
    return {"id": tid, "title": "title " + tid, "criteria": [], "run": "", "risk": "local", "journey": False, "state": state,
            "commits": [], "verify": None, "review": None, "routing": None, "tokens": None}


class Proj:
    def __init__(self, name):
        self.sb = sandbox(name)
        self.proj = self.sb["root"] / "proj"
        (self.proj / ".harness").mkdir(parents=True)
        led = {"version": 1, "project": "proj", "approval": None, "events": [], "sprints": [
            {"id": "S9", "title": "s", "branch": "sprint/s9", "state": "open", "opened": "2026-11-12T00:00:00+02:00", "tasks": [mk_task("T1")]}]}
        (self.proj / ".harness" / "ledger.json").write_text(json.dumps(led), encoding="utf-8", newline="\n")

    def stop(self, session="sess1"):
        payload = {"hook_event_name": "Stop", "session_id": session, "cwd": str(self.proj),
                   "last_assistant_message": "I finished the part I could do and I am stopping here."}
        cp = subprocess.run([PY, "-I", str(HOOKS / "session_close_gate.py")], input=json.dumps(payload), capture_output=True, text=True,
                            encoding="utf-8", env=envof(self.sb, {"SESSION_CLOSE_GATE_MODE": "live"}), cwd=str(self.sb["root"]), timeout=60)
        blocked = any('"block"' in ln for ln in cp.stdout.splitlines())
        return blocked, cp

    @property
    def state_file(self):
        return self.sb["state"] / "session_close" / "session_close_state.json"

    def streaks(self):
        return json.loads(self.state_file.read_text(encoding="utf-8")).get("ledger") or {}

    def edit_streaks(self, fn):
        d = json.loads(self.state_file.read_text(encoding="utf-8"))
        for v in d["ledger"].values():
            fn(v)
        self.state_file.write_text(json.dumps(d), encoding="utf-8")


def test_ledger_streak_key_is_per_session_and_project_root():
    p = Proj("s7sc_key")
    assert p.stop("A")[0] and p.stop("A")[0]
    p.stop("B")
    st = p.streaks()
    root = str(p.proj.resolve())
    assert sorted(st) == sorted([f"A|{root}", f"B|{root}"]), list(st)
    assert st[f"A|{root}"]["n"] == 2 and st[f"B|{root}"]["n"] == 1, st


def test_ledger_streak_restarts_after_a_pause_longer_than_five_minutes():
    p = Proj("s7sc_gap")
    p.stop()
    p.stop()
    assert list(p.streaks().values())[0]["n"] == 2
    p.edit_streaks(lambda v: v.update(last=time.time() - 120))  # 2 minutes: same streak continues
    p.stop()
    assert list(p.streaks().values())[0]["n"] == 3
    p.edit_streaks(lambda v: v.update(last=time.time() - 360))  # 6 minutes, same signature: restarts at 1
    blocked, cp = p.stop()
    rec = list(p.streaks().values())[0]
    assert rec["n"] == 1 and blocked, (rec, cp.stdout)
    assert rec["first"] > time.time() - 60, "a new streak also resets the 30-minute clock"


def test_ledger_release_writes_a_metrics_row_with_decision_release():
    p = Proj("s7sc_rel")
    for _ in range(5):
        assert p.stop()[0] is True
    assert [r for r in metric_rows(p.sb) if r["decision"] == "release"] == [], "no release while blocks are still allowed"
    blocked, cp = p.stop()  # the 6th block without progress releases the session
    assert blocked is False, cp.stdout
    rel = [r for r in metric_rows(p.sb) if r["decision"] == "release"]
    assert len(rel) == 1 and rel[0]["hook"] == "session_close_gate.py" and rel[0]["event"] == "Stop", metric_rows(p.sb)
    assert rel[0]["reason"] == "ledger-no-progress-blocks" and rel[0]["project"] == "proj", rel
    # the time-based release is recorded too
    q = Proj("s7sc_rel2")
    q.stop()
    q.edit_streaks(lambda v: v.update(first=time.time() - 31 * 60, last=time.time()))
    blocked, cp = q.stop()
    assert blocked is False
    assert [r["reason"] for r in metric_rows(q.sb) if r["decision"] == "release"] == ["ledger-30-minutes"], metric_rows(q.sb)


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
