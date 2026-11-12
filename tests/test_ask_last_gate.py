"""Tests for harness/hooks/ask_last_gate.py (S7-T4: PreToolUse AskUserQuestion + Stop). The hook runs as a subprocess against
JSONL transcript fixtures; HARNESS_HOOK_STATE points at a fresh temp folder. Expected outcomes come from the S7 plan
(S7-T4, MAJOR-01, ADDITION 2) and the 2026-11-12 incident, not from the code."""
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "harness" / "hooks" / "ask_last_gate.py"
PY = "C:/Python314/python.exe"
TMP = Path(tempfile.mkdtemp(prefix="ask_last_test_"))
FAILS = []

ASK_INCIDENT = "Please create the terraform state file or give me the gcloud key"


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL") + ": " + name + ("" if cond else "  " + detail))
    if not cond:
        FAILS.append(name)


def transcript(calls=(), human="Deploy the stack"):
    """calls: list of (name, input, result, is_error). Returns the JSONL path."""
    rows = [{"type": "user", "message": {"role": "user", "content": human}, "timestamp": "2026-11-12T10:00:00Z"}]
    for name, inp, result, is_err in calls:
        tid = "toolu_" + uuid.uuid4().hex[:10]
        rows.append({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": name, "input": inp}]}})
        rows.append({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": result, "is_error": is_err}]}})
    p = TMP / (uuid.uuid4().hex[:8] + ".jsonl")
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8", newline="\n")
    return str(p)


def sh(cmd, result, err=False):
    return ("Bash", {"command": cmd}, result, err)


def run_hook(payload, extra_env=None, state=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARNESS_")}
    env["HARNESS_HOOK_STATE"] = str(state or (TMP / ("state_" + uuid.uuid4().hex[:6])))
    env.setdefault("HARNESS_METRICS_DIR", str(TMP / "metrics_sink"))  # S8-T1: never the real metrics folder
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra_env or {})
    p = subprocess.run([PY, "-I", "-s", "-X", "utf8", str(HOOK), "--harness-client=claude"], input=json.dumps(payload),
                       capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    return p.returncode, p.stdout.strip(), p.stderr


def ask_payload(question, tp, options=(), **extra):
    qs = [{"question": question, "options": [{"label": o, "description": ""} for o in options]}]
    d = {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion", "tool_input": {"questions": qs}, "transcript_path": tp}
    d.update(extra)
    return d


def decision(out):
    try:
        return json.loads(out)
    except ValueError:
        return {}


def denied(out):
    h = decision(out).get("hookSpecificOutput") or {}
    return h.get("permissionDecision") == "deny", h.get("permissionDecisionReason", "")


def expect_deny(name, question, tp, must=(), options=(), extra_env=None, **extra):
    rc, out, err = run_hook(ask_payload(question, tp, options, **extra), extra_env)
    d, why = denied(out)
    check(name, rc == 0 and d and all(m.lower() in why.lower() for m in must), "rc=%s out=%r err=%r" % (rc, out[:300], err[-200:]))


def expect_allow(name, question, tp, options=(), extra_env=None, **extra):
    rc, out, err = run_hook(ask_payload(question, tp, options, **extra), extra_env)
    check(name, rc == 0 and out == "", "rc=%s out=%r err=%r" % (rc, out[:300], err[-200:]))


# Investigation calls that all mention the topic and return errors / not found.
GOOD_CALLS = [
    sh("printenv | cut -d= -f1 | grep -i -E 'gcloud|terraform|key|token'", ""),
    sh("find . -maxdepth 3 -name '*terraform*' -o -name '*gcloud*key*'", "find: no such file or directory: terraform state not found"),
    sh("gcloud auth list", "ERROR: (gcloud.auth.list) You do not have credentials. not logged in. no credentials"),
    sh("terraform state list", "Error: No state file was found! state not found", True),
]

# 1. empty turn -> deny, "do it yourself", missing proof listed
tp_empty = transcript()
expect_deny("1 incident ask with empty turn denied with do-it-yourself and missing proof", ASK_INCIDENT, tp_empty,
            must=("do it yourself", "missing proof"))

# 2. real investigation with results -> allowed
expect_allow("2 incident ask after real investigation with not-found results allowed", ASK_INCIDENT, transcript(GOOD_CALLS))

# 2b. realistic gcloud text "No credentialed accounts." must also count as not found
real_calls = [
    sh("printenv | cut -d= -f1 | grep -i -E 'gcloud|terraform|key'", ""),
    sh("ls -la *.tfstate terraform/ config/ 2>&1 | grep -i terraform", "ls: cannot access 'terraform/': No such file or directory"),
    sh("gcloud auth list", "No credentialed accounts.\n\nTo login, run:\n  $ gcloud auth login `ACCOUNT`"),
]
expect_allow("2b realistic gcloud 'No credentialed accounts.' counts as not found", ASK_INCIDENT, transcript(real_calls))

# 2c. state ask (no credential word): config files + cli status + failed non-transient try action -> allowed
state_calls = [
    sh("ls terraform/ backend.tf config", "ls: cannot access 'terraform/': No such file or directory"),
    sh("terraform state list", "Error: No state file was found!", True),
    sh("terraform init", "Error: Failed to get existing workspaces: bucket does not exist", True),
]
expect_allow("2c state ask allowed after failing non-transient try action",
             "Please create the terraform state bucket", transcript(state_calls))

# 3. fabricated / irrelevant calls
irrelevant = [sh("pwd", "/home/u/proj"), sh("ls /tmp/photos", "a.jpg b.jpg"), sh("git status", "On branch main\nnothing to commit")]
expect_deny("3 irrelevant calls (pwd, ls unrelated, git status) do not satisfy", ASK_INCIDENT, transcript(irrelevant),
            must=("do it yourself",))
# calls without results (never executed) must not count
no_result = [("Bash", {"command": c}, "", None) for c in ("printenv | grep -i gcloud", "find . -name '*terraform*'", "gcloud auth list")]
tp_nr = transcript([])
rows = []
for name, inp, _, _ in no_result:
    rows.append({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "x" + inp["command"][:5], "name": name, "input": inp}]}})
Path(tp_nr).write_text(Path(tp_nr).read_text(encoding="utf-8") + "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8", newline="\n")
expect_deny("3b investigation calls with no results (never returned) do not satisfy", ASK_INCIDENT, tp_nr, must=("do it yourself",))

# 4. usable result, no failing try action -> use what you found
usable = [
    sh("printenv | cut -d= -f1 | grep -i gcloud", "GOOGLE_APPLICATION_CREDENTIALS_GCLOUD"),
    sh("ls ~/.config/gcloud/ keys terraform", "application_default_credentials.json\nconfigurations"),
    sh("gcloud auth list", "ACTIVE  ACCOUNT\n*       sa-deploy@proj.iam.gserviceaccount.com"),
]
expect_deny("4 usable result without failing try action denied with use-what-you-found", ASK_INCIDENT, transcript(usable),
            must=("use what you found",))

# 5. only transient errors from the try action
transient = [
    sh("ls *state* bucket", "ls: cannot access 'bucket': No such file or directory"),
    sh("terraform state list", "Error: request timed out, 503 Service Unavailable", True),
    sh("terraform init", "Error: Failed to query backend: 503 Service Unavailable, timeout", True),
]
expect_deny("5 only transient errors from try action denied", "Please create the terraform state bucket", transcript(transient),
            must=("retry",))

# 5b. a file listing that merely names a CLI (ls terraform/) is a config-file probe, not a failed try-it action
tp5b = transcript([
    sh("ls terraform/ backend.tf", "ls: cannot access 'terraform/': No such file or directory"),
    sh("terraform state list", "Error: request timed out, 503 Service Unavailable", True),
    sh("terraform init", "Error: Failed to query backend: 503 Service Unavailable, timeout", True),
])
expect_deny("5b ls naming terraform is not a failed try action; transient-only still denied",
            "Please create the terraform state bucket", tp5b, must=("retry",))

# 6. exempt asks allowed with an empty turn
for label, q in [
    ("payment EN", "Please make the payment for the cloud billing invoice"),
    ("purchase EN", "Please purchase the domain and give me the access token"),
    ("upgrade plan EN", "Please upgrade plan to unlock the API key quota"),
    ("upgrade plan EN with action verb", "Please upgrade the plan and send me the new API key"),
    ("billing EN with action verb", "Please enable billing and add the payment access token"),
    ("pay EN with action verb", "Please pay the invoice and share the credentials"),
    ("payment RU", "Пожалуйста, оплатите счёт и дайте доступ к облаку"),
    ("purchase RU", "Купите подписку и пришлите ключ"),
    ("force-push", "Please run a force-push to rewrite the config history"),
    ("production data", "Please delete production data and change the config"),
    ("create account", "Please create an account in the cloud console and send the key"),
    ("password", "Please enter your password to authorize the login"),
    ("captcha", "Please solve the CAPTCHA to continue the login"),
]:
    expect_allow("6 exempt ask allowed on empty turn: " + label, q, tp_empty)

# 7. design/choice question
expect_allow("7 design choice question allowed", "Which layout do you prefer?", tp_empty, options=("Grid", "List"))
expect_allow("7b design question with options allowed", "Which color scheme should the dashboard use?", tp_empty,
             options=("Dark", "Light"))

# 8. subagent payload untouched; kill switch
expect_allow("8 subagent payload untouched", ASK_INCIDENT, tp_empty, agent_type="general-purpose")
expect_allow("8b HARNESS_ASK_LAST=off disables", ASK_INCIDENT, tp_empty, extra_env={"HARNESS_ASK_LAST": "off"})
expect_deny("8c control: same payload without the switch is denied", ASK_INCIDENT, tp_empty, must=("do it yourself",))
expect_allow("8d fails open on unreadable transcript path", ASK_INCIDENT, str(TMP / "missing.jsonl"))

# 9. Stop hook
PROSE = "I looked at the setup.\n\nCould you send me the API key?"


def stop_payload(msg, tp, **extra):
    d = {"hook_event_name": "Stop", "last_assistant_message": msg, "transcript_path": tp}
    d.update(extra)
    return d


st = TMP / "stop_state"
rc, out, err = run_hook(stop_payload(PROSE, tp_empty), state=st)
check("9 prose ask without evidence blocked", rc == 0 and decision(out).get("decision") == "block", "rc=%s out=%r err=%r" % (rc, out[:200], err[-200:]))
check("9 block reason says do it yourself", "do it yourself" in decision(out).get("reason", "").lower(), out[:200])
rc, out, err = run_hook(stop_payload(PROSE, tp_empty), state=st)
check("9 same message again not blocked", rc == 0 and out == "", "rc=%s out=%r" % (rc, out[:200]))
rc, out, err = run_hook(stop_payload("Done.\n\nCould you send me the API token please?", tp_empty), state=st)
check("9 different message with same state blocked again (once per message)", decision(out).get("decision") == "block", out[:200])
rc, out, err = run_hook(stop_payload(PROSE, tp_empty, stop_hook_active=True))
check("9 stop_hook_active not blocked", rc == 0 and out == "", out[:200])

tp_q = transcript([("AskUserQuestion", {"questions": [{"question": "Which API key?", "options": []}]}, "answered", False)])
rc, out, err = run_hook(stop_payload(PROSE, tp_q))
check("9 turn that used AskUserQuestion not blocked by Stop", rc == 0 and out == "", out[:200])

rc, out, err = run_hook(stop_payload(PROSE, transcript(GOOD_CALLS)))
check("9 with evidence not blocked", rc == 0 and out == "", out[:200])

rc, out, err = run_hook(stop_payload("All done, tests pass.", tp_empty))
check("9 plain statement without ask not blocked", rc == 0 and out == "", out[:200])
rc, out, err = run_hook(stop_payload(PROSE, tp_empty), extra_env={"HARNESS_ASK_LAST": "off"})
check("9 kill switch disables Stop", rc == 0 and out == "", out[:200])
rc, out, err = run_hook(stop_payload("Could you pay the invoice?", tp_empty))
check("9 exempt prose ask (money) not blocked", rc == 0 and out == "", out[:200])

if FAILS:
    print("FAILED: %d failing" % len(FAILS))
    sys.exit(1)
print("OK: 0 failing")
