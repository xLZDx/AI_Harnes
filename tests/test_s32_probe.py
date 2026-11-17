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


def call(command, client=None):
    d = hv2.fresh("s32_probe")
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARNESS_")}
    env["HARNESS_HOOK_STATE"] = str(d / "state")
    env["HARNESS_METRICS_DIR"] = str(hv2.SINK / "metrics")
    env["PYTHONIOENCODING"] = "utf-8"
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
    return subprocess.run([sys.executable, "-I", str(HOOK)] + (["--harness-client=" + client] if client else []), input=json.dumps(payload), capture_output=True,
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


def test_env_prefix_does_not_hide_a_broad_restore_or_reset():
    for client in (None, "codex"):
        for cmd in ("FOO=1 git restore .", "FOO=1 git reset --hard", 'A=1 B="x y" git restore .', "FOO= git restore ."):
            p = call(cmd, client)
            assert p.returncode == 0, p.stderr
            assert is_denied(p), f"{client}: expected deny for {cmd!r}, got stdout={p.stdout!r}"


def test_env_prefix_named_restore_still_allowed():
    for client in (None, "codex"):
        p = call("FOO=1 git restore docs/plan.md", client)
        assert p.returncode == 0, p.stderr
        assert not is_denied(p), f"{client}: {p.stdout!r}"


def test_assignment_values_with_spaces_quotes_and_substitutions_do_not_hide_a_command():
    """Reviewer rounds 6-7: a regex stopped at the first space; the scanner reads whole shell words (escapes, quotes, $'..', ${..}, $(..))."""
    cmds = [
        "FOO=a\\ b git reset --hard",
        "FOO=a\"b c\" git reset --hard",
        "FOO=\"a b\"c git reset --hard",
        "FOO=$'a b' git reset --hard",
        "FOO=$'a\\' b' git reset --hard",
        "FOO=${X:-a b} git reset --hard",
        "env FOO=\"a b\" git reset --hard",
        "env -u X git reset --hard",
        "env -u A -u B git reset --hard",
        "env -C /tmp git reset --hard",
        "env --unset=X git reset --hard",
        "FOO=1 BAR=2 git restore .",
        "FOO+=x git reset --hard",
        "a[0]=1 git reset --hard",
        "FOO=$(echo a) git reset --hard",
        "FOO=\"$(echo a)\" git reset --hard",
        "FOO=\"unclosed git reset --hard",
        "FOO=$(unclosed git reset --hard",
    ]
    for client in (None, "codex"):
        for cmd in cmds:
            p = call(cmd, client)
            assert p.returncode == 0, p.stderr
            assert is_denied(p), f"{client}: expected deny for {cmd!r}, got stdout={p.stdout!r}"


def test_assignment_prefix_on_harmless_commands_is_allowed():
    cmds = ['FOO="a b" git restore docs/plan.md', "FOO=a\\ b git restore docs/plan.md", "FOO=1 echo hi", "x=1",
            "SHA=$(git rev-parse HEAD)", 'd="$(pwd)"', "X=$(date)", "V=${HOME:-x} echo hi", "env -u X echo hi", "FOO=$'a b' echo hi"]
    for cmd in cmds:
        p = call(cmd)
        assert p.returncode == 0, p.stderr
        assert not is_denied(p), f"{cmd!r}: {p.stdout!r}"


def test_env_split_string_and_quoted_wrapper_options_do_not_hide_a_command():
    """GPT-PM round 2 (both MAJOR): `env -S "git restore ."` and `env -u "A B" git restore .` / `sudo -u "a b" ...`."""
    cmds = [
        'env -S "git restore ."',
        "env -S 'git reset --hard'",
        "env -S git restore .",
        'env -iS "git restore ."',
        'env --split-string="git restore ."',
        'env -u "A B" git restore .',
        'env -C "/my dir" git reset --hard',
        'sudo -u "a b" git reset --hard',
        "sudo -u a\\ b git restore .",
    ]
    for client in (None, "codex"):
        for cmd in cmds:
            p = call(cmd, client)
            assert p.returncode == 0, p.stderr
            assert is_denied(p), f"{client}: expected deny for {cmd!r}, got stdout={p.stdout!r}"


def test_normal_wrapper_invocations_stay_allowed():
    for cmd in ("env FOO=1 git restore docs/plan.md", "env -u X echo hi", "sudo -u root git restore docs/plan.md", "env FOO=1 make",
                "sudo ls", 'echo "a; b"', "time git status", "env make -S"):
        p = call(cmd)
        assert p.returncode == 0, p.stderr
        assert not is_denied(p), f"{cmd!r}: {p.stdout!r}"


def test_wrapper_option_values_never_hide_the_command_word():
    """Reviewer round 10: the prefix regex cannot know every wrapper option; the gate also judges every later-word suffix."""
    cmds = [
        "env -iu A git reset --hard", "env -iC /x git restore .", "env -a foo git reset --hard", "sudo -nu root git reset --hard",
        "sudo -u#1000 git reset --hard", "time -p git reset --hard", "exec -a n git reset --hard", "command -p git reset --hard",
        "xargs -n 1 git reset --hard", "nice -n +5 git reset --hard", "stdbuf -o L git reset --hard", "sudo -u $(id -un) git reset --hard",
        # reviewer round 11: a leftover option value followed by a second wrapper or a shell -c
        "sudo -nu root bash -c 'git reset --hard'", 'sudo -u "a b" bash -c "git reset --hard"', "sudo -nu root nohup git reset --hard",
        "env -iu A bash -c 'git restore .'", "sudo -nu root env -u X git reset --hard",
    ]
    for client in (None, "codex"):
        for cmd in cmds:
            p = call(cmd, client)
            assert p.returncode == 0, p.stderr
            assert is_denied(p), f"{client}: expected deny for {cmd!r}, got stdout={p.stdout!r}"


def test_harmless_wrapped_commands_with_quotes_stay_allowed():
    for cmd in ('sudo bash -c "cd /x; ls"', 'sudo echo "a b"', 'timeout 5 sh -c "a && b"', 'nohup sh -c "make; make test" &',
                "nice -n 10 make", "xargs -n1 echo", "command -v git", "timeout 5 sleep 1"):
        p = call(cmd)
        assert p.returncode == 0, p.stderr
        assert not is_denied(p), f"{cmd!r}: {p.stdout!r}"


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
