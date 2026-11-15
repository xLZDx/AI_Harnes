"""S26 acceptance tests: dangerous_command_gate protects main/master from `git branch` deletion.

Drives the hook as a subprocess with a PreToolUse payload on stdin, for the Claude and the Codex client.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DEFAULT_ROOT = Path(r"D:\tmp\scratch_worktrees\hv2-s26")
ROOT = Path(os.environ.get("HV2_ROOT") or _HERE.parent)
if not (ROOT / "harness" / "hooks").is_dir():
    ROOT = _DEFAULT_ROOT
HOOK = ROOT / "harness" / "hooks" / "dangerous_command_gate.py"
MAIN_REASON = "deleting the main project branch (main/master) is never allowed"
CLIENTS = ("claude", "codex")


def decide(cmd, client="claude"):
    payload = {"session_id": "s26", "hook_event_name": "PreToolUse", "tool_name": "Bash",
               "tool_input": {"command": cmd}, "cwd": str(ROOT)}
    if client == "codex":
        payload["turn_id"] = "t1"
    cp = subprocess.run([sys.executable, "-s", str(HOOK), f"--harness-client={client}"], input=json.dumps(payload),
                        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(ROOT), timeout=60)
    assert cp.returncode == 0, (cp.returncode, cp.stderr[-300:])
    out = cp.stdout.strip()
    if not out:
        return "allow", ""
    h = json.loads(out)["hookSpecificOutput"]
    return h["permissionDecision"], h.get("permissionDecisionReason", "")


def deny_main(cmd):
    for c in CLIENTS:
        got, why = decide(cmd, c)
        assert got == "deny", f"[{c}] expected deny for {cmd!r}, got {got}"
        assert MAIN_REASON in why, f"[{c}] wrong reason for {cmd!r}: {why[:200]}"


def deny_with(cmd, reason):
    for c in CLIENTS:
        got, why = decide(cmd, c)
        assert got == "deny", f"[{c}] expected deny for {cmd!r}, got {got}"
        assert reason in why, f"[{c}] wrong reason for {cmd!r}: {why[:200]}"


UPD_REASON = "changing or deleting the main project branch (main/master) with update-ref is never allowed"
RECREATE_REASON = "force re-creating the main project branch (main/master) is never allowed"
OVERWRITE_REASON = "overwriting the main project branch (main/master) is never allowed"


def allow(cmd):
    for c in CLIENTS:
        got, why = decide(cmd, c)
        assert got == "allow", f"[{c}] expected allow for {cmd!r}, got {got}: {why[:200]}"


def test_deny_main_variants():
    for cmd in ["git branch -d main", "git branch -D main", "git branch --delete main", "git branch -d master",
                "git branch -d refs/heads/main", "git branch -d feature main", "git -C D:/x branch -d main",
                "git branch -d MAIN"]:
        deny_main(cmd)


def test_allow_non_main():
    for cmd in ["git branch -d s26-worktree-removal", "git branch -d main-feature", "git branch --list main",
                "git branch main2", "git worktree remove --force D:/tmp/scratch_worktrees/hv2-s99",
                "git worktree remove D:/tmp/scratch_worktrees/x", "git worktree prune"]:
        allow(cmd)


MOVE_REASON = "moving or force-resetting the main project branch (main/master) is never allowed"


def test_update_ref_delete_main_denied():
    for cmd in ["git update-ref -d refs/heads/main", "git update-ref --delete refs/heads/master",
                "git -C D:/x update-ref -d refs/heads/main", "git update-ref -d refs/heads/MAIN",
                "git update-ref --delete refs/heads/MASTER"]:
        deny_with(cmd, UPD_REASON)


def test_update_ref_with_message_flag_delete_main_denied():
    for cmd in ["git update-ref -m reason -d refs/heads/main", 'git update-ref -m "why" --delete refs/heads/master',
                "git -C D:/x update-ref -m x -d refs/heads/MAIN"]:
        deny_with(cmd, UPD_REASON)


def test_update_ref_with_message_flag_other_allowed():
    for cmd in ["git update-ref -m reason -d refs/heads/s99-sprint",
                "git update-ref -m reason refs/heads/s99-sprint abc1234"]:
        allow(cmd)


def test_update_ref_set_main_with_sha_denied():
    for cmd in ["git update-ref refs/heads/main abc1234", "git update-ref -m reason refs/heads/main abc1234",
                "git update-ref main abc1234", "git update-ref refs/heads/MASTER abc1234",
                "git update-ref refs/heads/main 0000000000000000000000000000000000000000",
                "git -C D:/x update-ref -m reason refs/heads/main abc1234"]:
        deny_with(cmd, UPD_REASON)


def test_update_ref_main_as_new_value_allowed():
    for cmd in ["git update-ref refs/heads/feat main", "git update-ref refs/heads/feat refs/heads/main",
                "git update-ref -m main refs/heads/feat abc1234",
                "git -C D:/x update-ref refs/heads/s99-sprint main"]:
        allow(cmd)


def test_force_recreate_main_denied():
    for cmd in ["git checkout -B main abc1234", "git switch -C main abc1234", "git switch --force-create main",
                "git checkout -B master abc1234", "git switch -C MAIN abc1234"]:
        deny_with(cmd, RECREATE_REASON)


def test_checkout_switch_other_allowed():
    for cmd in ["git checkout -b feat main", "git switch -c feat main", "git checkout main", "git switch main",
                "git checkout -B feat main"]:
        allow(cmd)


def test_overwrite_main_as_destination_denied():
    for cmd in ["git branch -M x main", "git branch -C x main", "git branch -M x master", "git branch -C x MAIN"]:
        deny_with(cmd, OVERWRITE_REASON)


def test_branch_move_copy_other_allowed():
    for cmd in ["git branch -M feat feat2", "git branch -C feat feat2", "git branch -c main main-copy"]:
        allow(cmd)


def test_rename_or_force_move_main_denied():
    for cmd in ["git branch -m main x", "git branch -M master x", "git branch -f main abc1234",
                "git branch --force main abc1234", "git -C D:/x branch -f MAIN abc1234"]:
        for c in CLIENTS:
            got, why = decide(cmd, c)
            assert got == "deny", f"[{c}] expected deny for {cmd!r}, got {got}"
            assert MOVE_REASON in why, f"[{c}] wrong reason for {cmd!r}: {why[:200]}"


def test_update_ref_and_move_other_allowed():
    for cmd in ["git update-ref -d refs/heads/s26-worktree-removal", "git update-ref refs/heads/s99-sprint abc1234",
                "git branch -f feat main", "git branch -m feat feat2", "git branch -d s26-worktree-removal"]:
        allow(cmd)


def test_forced_delete_of_other_branch_still_denied():
    for c in CLIENTS:
        got, why = decide("git branch -D s99", c)
        assert got == "deny", f"[{c}] expected deny, got {got}"
        assert "forced branch deletion" in why, why[:200]
        assert MAIN_REASON not in why


def test_text_rules():
    core = (ROOT / "harness" / "rules" / "CORE.md").read_text(encoding="utf-8")
    assert "never asks and is never blocked" in core
    assert "is never deleted" in core
    wp = (ROOT / "harness" / "hooks" / "worktree_policy.py").read_text(encoding="utf-8")
    start = wp.index("STOP_MSG")
    assert "without asking" in wp[start:start + 1500]


def run_all(g) -> int:
    fails = 0
    for name in [n for n in g if n.startswith("test_")]:
        try:
            g[name]()
            print(f"PASS {name}")
        except BaseException:
            fails += 1
            print(f"FAIL {name}")
            traceback.print_exc(limit=4, file=sys.stdout)
    print(f"{'FAILED' if fails else 'OK'}: {fails} failing")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run_all(globals()))
