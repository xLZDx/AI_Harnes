"""S32-T1 probe 2: path traversal through docs/.. in git restore, via shell_policy_gate.py subprocess."""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hv2  # noqa: E402

HOOK = HERE.parent / "harness" / "hooks" / "shell_policy_gate.py"


def call(command):
    d = hv2.fresh("s32_probe2")
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARNESS_")}
    env["HARNESS_HOOK_STATE"] = str(d / "state")
    env["HARNESS_METRICS_DIR"] = str(hv2.SINK / "metrics")
    env["PYTHONIOENCODING"] = "utf-8"
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
    return subprocess.run([sys.executable, "-I", str(HOOK)], input=json.dumps(payload), capture_output=True,
                          text=True, encoding="utf-8", env=env, timeout=60)


def is_denied(p):
    try:
        return json.loads(p.stdout).get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
    except ValueError:
        return False


def test_dotdot_traversal_restore_denied():
    p = call("git restore docs/../README.md")
    assert p.returncode == 0, p.stderr
    assert is_denied(p), f"expected deny, got stdout={p.stdout!r}"


def test_plain_named_restore_allowed():
    p = call("git restore docs/plan.md")
    assert p.returncode == 0, p.stderr
    assert not is_denied(p), p.stdout
