"""S32-T1 independent probe: git restore through harness/hooks/shell_policy_gate.py as a subprocess (Claude Bash payload)."""
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
    d = hv2.fresh("s32_probe")
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


def test_named_path_restore_not_denied():
    p = call("git restore docs/plan.md")
    assert p.returncode == 0, p.stderr
    assert not is_denied(p), p.stdout


def test_dot_restore_denied():
    p = call("git restore .")
    assert p.returncode == 0, p.stderr
    assert is_denied(p), f"expected deny, got stdout={p.stdout!r}"


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
