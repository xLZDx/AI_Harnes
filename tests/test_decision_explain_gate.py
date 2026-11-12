"""Tests for harness/hooks/decision_explain_gate.py (Stop hook). Hook is run as a subprocess;
is_decision_ref is also called directly. Expected outcomes come from the task description."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "harness" / "hooks" / "decision_explain_gate.py"
PY = "C:/Python314/python.exe"
# Long filler with short lowercase words and no brackets, so it can never look like an explanation or a code.
FILLER = "the quick brown fox jumps over the lazy dogs again and again near a river bank " * 4
HASH40 = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"

_spec = importlib.util.spec_from_file_location("decision_explain_gate", HOOK)
_mod = importlib.util.module_from_spec(_spec)
sys.path.insert(0, str(HOOK.parent))
_spec.loader.exec_module(_mod)

FAILS = []


def run_hook(message, extra_env=None):
    env = dict(os.environ)
    env.pop("CLAUDE_DECISION_EXPLAIN_GATE", None)
    env.update(extra_env or {})
    payload = json.dumps({"last_assistant_message": message})
    p = subprocess.run([PY, "-s", "-X", "utf8", str(HOOK), "--harness-client=claude"],
                       input=payload, capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    return p.returncode, p.stdout.strip()


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL") + ": " + name + ("" if cond else "  " + detail))
    if not cond:
        FAILS.append(name)


def expect_silent(name, message, extra_env=None):
    rc, out = run_hook(message, extra_env)
    check(name, rc == 0 and out == "", "rc=%s out=%r" % (rc, out))


def expect_block(name, message):
    rc, out = run_hook(message)
    try:
        d = json.loads(out)
    except ValueError:
        d = {}
    check(name, rc == 0 and d.get("decision") == "block" and "DECISION REFS UNEXPLAINED" in d.get("reason", ""),
          "rc=%s out=%r" % (rc, out[:120]))


assert len(FILLER) >= 200

# is_decision_ref direct
check("is_decision_ref 7-hex hash is False", _mod.is_decision_ref("(dc47d8a)") is False)
check("is_decision_ref 40-hex hash is False", _mod.is_decision_ref("(" + HASH40 + ")") is False)
check("is_decision_ref 6-hex is False (not D-n/Dn/DEC-n, not in a DECISION_LOG)", _mod.is_decision_ref("(abc123)") is False)
check("is_decision_ref 41-hex is False", _mod.is_decision_ref("(" + HASH40 + "0)") is False)
check("is_decision_ref (D-1) is True", _mod.is_decision_ref("(D-1)") is True)
check("is_decision_ref (S4) is False (sprint id)", _mod.is_decision_ref("(S4)") is False)

# hook behaviour
expect_silent("short hash dc47d8a ignored", FILLER + "see commit (dc47d8a) for now")
expect_silent("short hash e9c683d ignored", FILLER + "see commit (e9c683d) for now")
expect_silent("40-hex hash ignored", FILLER + "see commit (" + HASH40 + ") for now")
expect_silent("6-hex (abc123) is not a decision ref", FILLER + "see the item (abc123) for now")
expect_block("(D-1) unexplained blocks", FILLER + "we go with this (D-1) for now")
expect_silent("(S4) sprint id does not block", FILLER + "we go with this (S4) for now")
expect_silent("(D-1: ...) explained passes",
              FILLER + "we go with this (D-1: where to merge - recommend phase2) for now")
expect_block("hash plus unexplained (D-2) blocks", FILLER + "commit (dc47d8a) and also (D-2) for now")
expect_silent("message under 200 chars passes", "short (D-1) message")
expect_silent("kill switch off passes", FILLER + "we go with this (D-1) for now",
              {"CLAUDE_DECISION_EXPLAIN_GATE": "off"})

if FAILS:
    print("FAILED: %d failing" % len(FAILS))
    sys.exit(1)
print("OK: 0 failing")
