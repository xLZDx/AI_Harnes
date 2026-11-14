"""S21-T1 acceptance tests: go_gate no longer asks for GO on throwaway paths (temp folders outside git).

Final name in the worktree: tests/test_s21_go_gate.py (this copy has a neutral name because the NEW hook
treats any path part containing "gate" as sensitive, see report).

A. bash_needs_go: harmless deletes (empty dir, plain file in a temp folder outside git, missing path) need no GO;
   every unsafe shape (non-empty dir, git repo, "..", wildcard/variable, unknown flag, no target, link) still does.
B. sensitive folder names (receipts, approval, gate state, .claude, .codex, .harness*) never count as throwaway;
   redirect to a temp file outside git is free, inside git or via heredoc it still needs GO.
C. the hook itself (subprocess, Claude PreToolUse payload, transcript with NO operator GO, PM mode OFF):
   Write/Edit to a temp file outside git -> allow; inside git, with "..", or in a sensitive folder -> deny;
   Codex apply_patch: every path must be throwaway.
"""
from __future__ import annotations

import atexit
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DEFAULT_ROOT = Path(r"D:\tmp\scratch_worktrees\hv2-s21")
ROOT = Path(os.environ.get("HV2_ROOT") or _HERE.parent)
if not (ROOT / "harness" / "hooks").is_dir():
    ROOT = _DEFAULT_ROOT
HOOKS = ROOT / "harness" / "hooks"
GO_GATE = HOOKS / "go_gate.py"
sys.path.insert(0, str(HOOKS))
_spec = importlib.util.spec_from_file_location("go_gate_under_test", GO_GATE)
gg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gg)

TMPROOT = Path(tempfile.mkdtemp(prefix="s21t_")).resolve()
_JUNCTIONS: list[Path] = []


def _cleanup():
    for j in _JUNCTIONS:
        try:
            os.rmdir(j)  # removes the link only, never the target
        except OSError:
            pass
    shutil.rmtree(TMPROOT, ignore_errors=True)


atexit.register(_cleanup)
_counter = [0]


def fresh(name="d") -> Path:
    _counter[0] += 1
    p = TMPROOT / f"{name}{_counter[0]}"
    p.mkdir(parents=True)
    return p


def git_repo() -> Path:
    p = fresh("repo")
    cp = subprocess.run(["git", "init", "-q", str(p)], capture_output=True, text=True)
    assert cp.returncode == 0, cp.stderr
    return p


def needs(cmd, cwd=None, ps=False):
    return gg.bash_needs_go(cmd, str(cwd or TMPROOT), is_powershell=ps)


def assert_free(cmd, cwd=None, ps=False):
    r = needs(cmd, cwd, ps)
    assert r[0] is False, f"expected NO GO, got {r} for {cmd!r}"


def assert_go(cmd, cwd=None, ps=False):
    r = needs(cmd, cwd, ps)
    assert r[0] is True, f"expected GO required, got {r} for {cmd!r}"


# ------------------------------------------------------------------------------------------------ A: deletes
def test_a_empty_dir_removal_is_free_and_nonempty_dir_is_not():
    empty = fresh("empty")
    assert_free(f"rmdir {empty.as_posix()}")
    empty2 = fresh("empty")
    assert_free(f"Remove-Item {empty2.as_posix()}", ps=True)
    full = fresh("full")
    (full / "f.txt").write_text("x")
    assert_go(f"rmdir {full.as_posix()}")
    assert_go(f"Remove-Item {full.as_posix()}", ps=True)
    assert_go(f"rm -rf {full.as_posix()}")


def test_a_plain_file_in_temp_outside_git_is_free_but_same_file_in_git_repo_is_not():
    d = fresh()
    f = d / "plain.txt"
    f.write_text("x")
    assert_free(f"rm {f.as_posix()}")
    assert_free(f"rm -f {f.as_posix()}")
    # S21 (770455b): a RELATIVE target is unsafe (an earlier `cd` in the same command changes its meaning): it asks
    assert_go("rm plain.txt", cwd=d)
    assert_go("rm ./plain.txt", cwd=d)
    assert_go("rm -f plain.txt", cwd=d)
    repo = git_repo()
    g = repo / "plain.txt"
    g.write_text("x")
    assert_go(f"rm {g.as_posix()}")
    assert_go(f"rm -f {g.as_posix()}")


def test_a_dotdot_wildcard_and_variable_targets_need_go():
    d = fresh()
    (d / "sub").mkdir()
    f = d / "plain.txt"
    f.write_text("x")
    assert_free(f"rm {f.as_posix()}")  # twin
    assert_go(f"rm {(d / 'sub').as_posix()}/../plain.txt")
    assert_go(f"rm {d.as_posix()}/*.txt")
    assert_go(f"rm {d.as_posix()}/pl?in.txt")
    assert_go(f"rm {d.as_posix()}/[p]lain.txt")
    assert_go("rm $HOME/plain.txt")
    assert_go(f"rm {d.as_posix()}/$NAME")
    assert_go("rm %TEMP%/plain.txt")
    assert_go("rm ~/plain.txt")
    assert_go("rm ../plain.txt", cwd=d / "sub")


def test_a_unknown_flag_or_missing_target_needs_go():
    d = fresh()
    f = d / "plain.txt"
    f.write_text("x")
    assert_free(f"rm -f {f.as_posix()}")  # twin
    assert_go(f"rm --no-preserve-root {f.as_posix()}")
    assert_go(f"rm -Z {f.as_posix()}")
    assert_go("rm")
    assert_go("rm -f")
    assert_go("rm -rf")
    assert_go("Remove-Item -Force", ps=True)


def test_a_missing_path_needs_no_go_but_missing_path_with_wildcard_does():
    d = fresh()
    gone = d / "gone.txt"
    assert not gone.exists()
    assert_free(f"rm -rf {gone.as_posix()}")
    assert_free(f"rm {gone.as_posix()}")
    assert_go(f"rm {d.as_posix()}/gone*")


def test_a_windows_backslash_and_quotes_and_powershell_flags():
    e1 = fresh("emp")
    assert_free(f'rmdir "{e1}"')  # native backslashes, double quotes
    e2 = fresh("emp")
    assert_free(f"rmdir '{e2}'")
    e3 = fresh("emp")
    assert_free(f"Remove-Item -LiteralPath '{e3}' -Force -ErrorAction SilentlyContinue", ps=True)
    e4 = fresh("emp")
    assert_free(f'Remove-Item -Path "{e4}" -Recurse -Force', ps=True)  # empty dir: nothing to lose
    full = fresh("full")
    (full / "k.txt").write_text("x")
    assert_go(f"Remove-Item -LiteralPath '{full}' -Force -ErrorAction SilentlyContinue", ps=True)
    assert_go(f'rmdir "{full}"')
    f = fresh() / "plain.txt"
    f.write_text("x")
    assert_free(f'rm "{f}"')


def test_a_symlink_or_junction_needs_go():
    d = fresh()
    target = fresh("target")  # EMPTY target: a naive "empty dir" rule would call the link harmless
    link = d / "lnk"
    made = False
    try:
        os.symlink(target, link, target_is_directory=True)
        made = True
    except (OSError, NotImplementedError):
        pass
    if made:
        _JUNCTIONS.append(link)
        assert_go(f"rmdir {link.as_posix()}")
        assert_go(f"rm {link.as_posix()}")
        os.rmdir(link)
    j = d / "junc"
    cp = subprocess.run(["cmd", "/c", "mklink", "/J", str(j), str(target)], capture_output=True, text=True)
    if cp.returncode == 0 and j.exists():
        _JUNCTIONS.append(j)
        assert_go(f"rmdir {j.as_posix()}")
        assert_go(f"Remove-Item {j.as_posix()}", ps=True)
        os.rmdir(j)
    elif not made:
        print("SKIP link cases: neither symlink nor junction could be created")
    assert target.exists()  # the link tests must not have touched the target
    # control: the same empty dir without a link is free
    assert_free(f"rmdir {target.as_posix()}")


# ------------------------------------------------------------------------------------------------ B: sensitive names
SENSITIVE = ["claude_gpt_review_gate", "receipts", "approval", ".claude", ".codex", ".harness-x"]


def test_b_sensitive_folder_names_still_need_go_for_rm_and_redirect():
    ok_dir = fresh("okdir")
    ok_file = ok_dir / "f.txt"
    ok_file.write_text("x")
    assert_free(f"rm {ok_file.as_posix()}")  # twins
    assert_free(f"echo x > {ok_file.as_posix()}")
    for name in SENSITIVE:
        d = fresh() / name
        d.mkdir()
        f = d / "f.txt"
        f.write_text("x")
        assert_go(f"rm {f.as_posix()}")
        assert_go(f"echo x > {f.as_posix()}")
        assert_go(f"echo x >> {f.as_posix()}")
        assert_go(f"Remove-Item {f.as_posix()}", ps=True)
    # a name that merely contains a sensitive word still counts (pattern is a substring match)
    d = fresh() / "my-receipts-backup"
    d.mkdir()
    assert_go(f"echo x > {(d / 'f.txt').as_posix()}")


def test_b_redirect_inside_temp_free_inside_git_and_heredoc_need_go():
    d = fresh()
    assert_free(f"echo x > {(d / 'ok.txt').as_posix()}")
    assert_free(f'echo x > "{d / "ok2.txt"}"')  # quoted, backslashes
    assert_free(f"echo x >> {(d / 'ok.txt').as_posix()}")
    repo = git_repo()
    assert_go(f"echo x > {(repo / 'ok.txt').as_posix()}")
    assert_go(f"echo x >> {(repo / 'ok.txt').as_posix()}")
    assert_go("cat <<EOF\nhello\nEOF")
    assert_go(f"cat > {(d / 'ok.txt').as_posix()} <<EOF\nhello\nEOF")  # heredoc even when the file is throwaway
    # a ".." redirect is resolved first: it counts by where it LANDS (inside the git repo -> GO)
    assert_go(f"echo x > {d.as_posix()}/../{repo.name}/via_dotdot.txt")
    # T1: any ".." form asks for GO, even when it resolves back into the same folder
    assert_go(f"echo x > {d.as_posix()}/../{d.name}/via_dotdot.txt")


def test_b_redirect_target_with_unexpanded_variable_or_tilde_needs_go():
    """The shell expands $HOME / ~ / %VAR% to a real folder; the hook must not read them as a literal temp-relative
    name (the delete path and the Write path already refuse these characters)."""
    d = fresh()
    assert_free(f"echo x > {(d / 'plain.txt').as_posix()}")  # twin
    for target in ("$HOME/a.txt", "~/a.txt", "%USERPROFILE%/a.txt", "${HOME}/a.txt"):
        assert_go(f"echo x > {target}", cwd=d)


# ------------------------------------------------------------------------------------------------ C: hook main
NO_GO_TEXT = "please look at the logs and tell me what you see"


def _sandbox_env(box: Path) -> dict:
    keep = {k: v for k, v in os.environ.items()
            if not k.startswith(("CLAUDE", "CODEX", "HARNESS", "PM_BRIDGE", "SESSION_", "ANTHROPIC"))}
    (box / "pm").mkdir(exist_ok=True)  # EMPTY pm state: no orchestrator.json => PM mode off
    (box / "home").mkdir(exist_ok=True)
    keep.update({
        "PYTHONIOENCODING": "utf-8", "GIT_TERMINAL_PROMPT": "0",
        "USERPROFILE": str(box / "home"), "HOME": str(box / "home"),
        "PM_BRIDGE_STATE_DIR": str(box / "pm"),
        "CLAUDE_GPT_REVIEW_STATE": str(box / "gpt"),
        "HARNESS_HOOK_STATE": str(box / "hs"), "CLAUDE_HOOK_STATE_DIR": str(box / "chs"),
        "CLAUDE_ROSETTA_MODE_FILE": str(box / "no_rosetta.json"),
        "HARNESS_METRICS_DIR": str(box / "metrics"),
    })
    return keep


def _transcript(box: Path, client: str, text: str) -> Path:
    p = box / ("tx_" + client + ".jsonl")
    if client == "claude":
        row = {"type": "user", "timestamp": "2026-11-14T10:00:00Z", "message": {"role": "user", "content": text}}
    else:
        row = {"type": "response_item", "timestamp": "2026-11-14T10:00:00Z", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}
    p.write_text(json.dumps(row) + "\n", encoding="utf-8", newline="\n")
    return p


def run_hook(tool, tool_input, client="claude", text=NO_GO_TEXT, cwd=None, extra_env=None):
    box = fresh("box")
    payload = {"session_id": "s21", "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input,
               "cwd": str(cwd or box), "transcript_path": str(_transcript(box, client, text))}
    if client == "codex":
        payload["turn_id"] = "t1"
    cp = subprocess.run([sys.executable, "-s", str(GO_GATE), f"--harness-client={client}"], input=json.dumps(payload),
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                        env={**_sandbox_env(box), **(extra_env or {})}, cwd=str(box), timeout=90)
    assert cp.returncode == 0, (cp.returncode, cp.stderr[-400:])
    out = cp.stdout.strip()
    if not out:
        return "allow", ""
    hso = json.loads(out)["hookSpecificOutput"]
    return hso["permissionDecision"], hso.get("permissionDecisionReason", "")


def assert_decision(want, *a, **kw):
    got, reason = run_hook(*a, **kw)
    assert got == want, f"expected {want}, got {got} for {a[:2]} :: {reason[:200]}"


def test_c_write_to_temp_file_outside_git_is_allowed():
    d = fresh()
    assert_decision("allow", "Write", {"file_path": str(d / "new.txt"), "content": "x"})
    assert_decision("allow", "Write", {"file_path": str(d / "deep" / "er" / "new.txt"), "content": "x"})
    existing = d / "e.txt"
    existing.write_text("a")
    assert_decision("allow", "Edit", {"file_path": str(existing), "old_string": "a", "new_string": "b"})
    assert_decision("allow", "Write", {"file_path": "rel.txt", "content": "x"}, cwd=d)


def test_c_write_inside_git_dotdot_and_sensitive_folders_is_denied():
    repo = git_repo()
    assert_decision("deny", "Write", {"file_path": str(repo / "a.txt"), "content": "x"})
    assert_decision("deny", "Edit", {"file_path": str(repo / "a.txt"), "old_string": "a", "new_string": "b"})
    assert_decision("deny", "Write", {"file_path": str(repo / "deep" / "er" / "a.txt"), "content": "x"})
    d = fresh()
    (d / "sub").mkdir()
    assert_decision("allow", "Write", {"file_path": str(d / "sub" / "a.txt"), "content": "x"})  # twin
    assert_decision("deny", "Write", {"file_path": str(d / "sub" / ".." / "a.txt"), "content": "x"})
    for name in SENSITIVE:
        f = fresh() / name / "f.txt"
        assert_decision("deny", "Write", {"file_path": str(f), "content": "x"})
    # control: a genuine operator GO in the same sandbox lets the repo write through, so the denials above
    # come from the gate's GO logic and not from a broken sandbox
    assert_decision("allow", "Write", {"file_path": str(repo / "a.txt"), "content": "x"}, text="GO")


def test_c_codex_apply_patch_requires_every_path_to_be_throwaway():
    def patch(*paths):
        body = "".join(f"*** Add File: {p}\n+x\n" for p in paths)
        return {"command": f"*** Begin Patch\n{body}*** End Patch\n"}

    d = fresh()
    outside = Path(os.environ.get("SystemDrive", "C:") + "\\") / "s21_missing_dir" / "a.txt"
    repo = git_repo()
    # same folder, both throwaway -> allow; hook route
    assert_decision("allow", "apply_patch", patch(d / "a.txt", d / "b.txt"), client="codex")
    assert_decision("allow", "apply_patch", patch(d / "a.txt"), client="codex")
    # one path outside temp -> deny (hook route)
    assert_decision("deny", "apply_patch", patch(outside), client="codex")
    assert_decision("deny", "apply_patch", patch(d / "a.txt", outside), client="codex")
    assert_decision("deny", "apply_patch", patch(repo / "a.txt"), client="codex")
    assert_decision("deny", "apply_patch", patch(d / ".." / "a.txt"), client="codex")
    # decision function itself, mixed paths in separate folders (the hook's one-repo rule hides this on its own)
    d2 = fresh()
    assert gg._paths_all_throwaway([str(d / "a.txt"), str(d2 / "b.txt")], str(TMPROOT)) is True
    assert gg._paths_all_throwaway([str(d / "a.txt"), str(outside)], str(TMPROOT)) is False
    assert gg._paths_all_throwaway([str(d / "a.txt"), str(repo / "x.txt")], str(TMPROOT)) is False
    assert gg._paths_all_throwaway([], str(TMPROOT)) is False
    assert gg._paths_all_throwaway([""], str(TMPROOT)) is False


def test_c_bash_hook_route_matches_function_for_free_rmdir():
    e = fresh("emp")
    assert_decision("allow", "Bash", {"command": f"rmdir {e.as_posix()}"})
    full = fresh("full")
    (full / "f").write_text("x")
    assert_decision("deny", "Bash", {"command": f"rmdir {full.as_posix()}"})


# ------------------------------------------------------------------------------------------------ D: S21 fixes (770455b)
def plain_file(name="plain.txt") -> Path:
    f = fresh() / name
    f.write_text("x")
    return f


def test_d_cd_then_relative_rm_asks_but_absolute_target_is_free():
    d = fresh()
    f = d / "plain.txt"
    f.write_text("x")
    assert_go(f"cd {d.as_posix()} && rm plain.txt")
    assert_go(f"cd {d.as_posix()} && rm -f plain.txt")
    assert_go(f'cd "{d}" && rm plain.txt')
    assert_free(f"cd {d.as_posix()} && rm {f.as_posix()}")  # twin: absolute


def test_d_git_bash_absolute_forms_ask():
    assert_go("rm -rf /d/Repo/x")
    assert_go("rm /d/tmp/x.txt")
    assert_go("rm -rf /c/Users/someone/x")
    assert_go("rm /c/Users/someone/x.txt")
    assert_go("rm -rf /d/Repo/x", ps=False)
    f = plain_file()
    assert_free(f"rm {f.as_posix()}")  # twin: real drive form of an existing plain throwaway file


def test_d_lists_braces_and_comma_targets_ask():
    a, b = plain_file("a.py"), plain_file("b.py")
    assert_free(f"rm {a.as_posix()}")  # twins
    assert_free(f"rm {b.as_posix()}")
    assert_go(f"Remove-Item {a.as_posix()},{b.as_posix()} -Recurse -Force", ps=True)
    assert_go(f"Remove-Item {a.as_posix()},{b.as_posix()} -Recurse -Force")
    assert_go(f'Remove-Item "{a}","{b}" -Recurse -Force', ps=True)
    assert_go(f"rm {a.parent.as_posix()}/{{a,b}}.py")
    assert_go(f"del a.txt,b.txt")
    assert_go("del a.txt,b.txt", cwd=a.parent)
    assert_go(f"del {a.as_posix()},{b.as_posix()}")


def test_d_powershell_subexpression_target_asks():
    assert_go("Remove-Item (Get-ChildItem)", ps=True)
    assert_go("Remove-Item (Get-ChildItem -Path .)", ps=True)
    assert_go("Remove-Item (Get-ChildItem)")
    f = plain_file()
    assert_free(f"Remove-Item {f.as_posix()}", ps=True)  # twin


def test_d_backtick_target_asks():
    d = fresh()
    f = d / "plain.txt"
    f.write_text("x")
    assert_go(f"rm {d.as_posix()}/`echo plain`.txt")
    assert_go(f"rm `echo {f.as_posix()}`")
    assert_go(f"Remove-Item {d.as_posix()}/pl`ain.txt", ps=True)
    assert_free(f"rm {f.as_posix()}")  # twin


def test_d_unquoted_backslash_target_outside_powershell_asks():
    f = plain_file()
    native = str(f)
    assert "\\" in native
    assert_go(f"rm {native}")  # the shell eats the backslashes: it would remove another path
    assert_go(f"rm -f {native}")
    assert_free(f'rm "{native}"')  # twin: quoted
    assert_free(f"rm '{native}'")
    assert_free(f"rm {native}", ps=True)  # twin: PowerShell keeps backslashes
    assert_free(f"Remove-Item {native}", ps=True)
    assert_free(f"rm {f.as_posix()}")  # twin: forward slashes
    # still judged by the file: a plain file in a git repo asks in every form
    g = git_repo() / "p.txt"
    g.write_text("x")
    assert_go(f'rm "{g}"')
    assert_go(f"rm {g}", ps=True)


def test_d_rm_slash_f_is_a_root_path_but_cmd_del_slash_q_is_a_switch():
    f = plain_file()
    assert_go(f"rm /f {f.as_posix()}")
    assert_go(f"rm /q {f.as_posix()}")
    assert_go(f"rm /f {f.as_posix()}", ps=True)
    assert_free(f"del /q {f.as_posix()}")
    f2 = plain_file()
    assert_free(f'del /q "{f2}"')
    f3 = plain_file()
    assert_free(f"del /f /q {f3.as_posix()}")
    assert_free(f"rm -f {plain_file().as_posix()}")  # twin: a real flag


def test_d_dot_git_hooks_and_config_of_a_real_clone_ask():
    repo = git_repo()
    hook = repo / ".git" / "hooks" / "pre-push"
    cfg = repo / ".git" / "config"
    assert cfg.exists()
    assert_go(f"echo x > {hook.as_posix()}")
    assert_go(f"echo x > {cfg.as_posix()}")
    assert_go(f"echo x >> {cfg.as_posix()}")
    assert_go(f"rm {cfg.as_posix()}")
    assert_go(f"Remove-Item {cfg.as_posix()}", ps=True)
    assert_decision("deny", "Write", {"file_path": str(hook), "content": "#!/bin/sh\nexit 0\n"})
    assert_decision("deny", "Write", {"file_path": str(cfg), "content": "[core]\n"})
    assert_decision("deny", "Edit", {"file_path": str(cfg), "old_string": "a", "new_string": "b"})
    # twin: a plain temp folder outside git is free
    d = fresh()
    assert_free(f"echo x > {(d / 'pre-push').as_posix()}")
    assert_decision("allow", "Write", {"file_path": str(d / "pre-push"), "content": "x"})


def test_d_scratchpad_folder_under_fake_home_claude_asks_but_real_temp_scratchpad_is_free():
    fake = Path(os.environ.get("SystemDrive", "C:") + "\\") / "s21_fakehome_xyz" / ".claude" / "skills" / "scratchpad"
    skill = fake / "SKILL.md"
    assert gg._paths_all_throwaway([str(skill)], str(TMPROOT)) is False
    assert_decision("deny", "Write", {"file_path": str(skill), "content": "x"})
    assert_decision("deny", "Edit", {"file_path": str(skill), "old_string": "a", "new_string": "b"})
    assert_go(f"echo x > {skill.as_posix()}")
    # the same folder names under a temp root: .claude is sensitive, so still asks
    inside = fresh() / "home" / ".claude" / "skills" / "scratchpad" / "SKILL.md"
    assert_decision("deny", "Write", {"file_path": str(inside), "content": "x"})
    # twin: a folder named scratchpad directly under a plain temp folder is free (no .claude)
    sp = fresh() / "scratchpad" / "notes.md"
    assert_decision("allow", "Write", {"file_path": str(sp), "content": "x"})
    assert gg._paths_all_throwaway([str(sp)], str(TMPROOT)) is True


def test_d_rosetta_pm_bridge_state_and_decisions_folders_ask():
    for name in ("rosetta", "pm_bridge_state", "decisions", "Rosetta_Mode", "pm_bridge", "decision_log"):
        d = fresh() / name
        d.mkdir()
        f = d / "f.txt"
        f.write_text("x")
        assert_go(f"rm {f.as_posix()}")
        assert_go(f"echo x > {f.as_posix()}")
        assert_go(f"Remove-Item {f.as_posix()}", ps=True)
        assert_decision("deny", "Write", {"file_path": str(f), "content": "x"})
        e = d / "emptysub"
        e.mkdir()
        assert_go(f"rmdir {e.as_posix()}")
    d = fresh() / "notes"  # twin
    d.mkdir()
    f = d / "f.txt"
    f.write_text("x")
    assert_free(f"rm {f.as_posix()}")
    assert_free(f"echo x > {f.as_posix()}")
    assert_decision("allow", "Write", {"file_path": str(f), "content": "x"})


def test_d_missing_target_is_free_only_as_the_whole_command():
    d = fresh()
    gone = d / "gone.txt"
    assert not gone.exists()
    assert_free(f"rm {gone.as_posix()}")
    assert_free(f"rm -f {gone.as_posix()}")
    assert_go(f"echo hi; rm {gone.as_posix()}")
    assert_go(f"echo hi && rm {gone.as_posix()}")
    assert_go(f"rm {gone.as_posix()}; echo hi")
    assert_go(f"cd {d.as_posix()} && rm {gone.as_posix()}")
    # twin: an existing plain file stays free inside a longer command
    f = d / "there.txt"
    f.write_text("x")
    assert_free(f"echo hi; rm {f.as_posix()}")


def test_d_empty_dir_inside_dot_claude_or_dot_git_asks_but_plain_temp_empty_dir_is_free():
    plain = fresh("emp")
    assert_free(f"rmdir {plain.as_posix()}")  # twin
    for name in (".claude", ".git", ".codex", ".harness-x"):
        parent = fresh() / name
        parent.mkdir()
        e = parent / "emptysub"
        e.mkdir()
        assert_go(f"rmdir {e.as_posix()}")
        assert_go(f"rm -r {e.as_posix()}")
        assert_go(f"Remove-Item {e.as_posix()}", ps=True)
    # the sensitive folder itself, when empty
    solo = fresh() / ".claude"
    solo.mkdir()
    assert_go(f"rmdir {solo.as_posix()}")


def test_d_unc_paths_ask():
    assert_go(r"rm \\server\share\x.txt")
    assert_go(r'rm "\\server\share\x.txt"')
    assert_go("rm //server/share/x.txt")
    assert_go(r"Remove-Item \\server\share\x.txt", ps=True)
    assert_go(r"rmdir \\server\share\emptydir")
    assert gg._paths_all_throwaway([r"\\server\share\x.txt"], str(TMPROOT)) is False
    assert gg._paths_all_throwaway(["//server/share/x.txt"], str(TMPROOT)) is False
    assert gg._is_throwaway_path(Path(r"\\server\share\x.txt")) is False


def test_d_file_named_gate_in_plain_temp_folder_is_still_free():
    d = fresh()
    f = d / "test_x_gate.py"
    assert_decision("allow", "Write", {"file_path": str(f), "content": "x"})
    assert_free(f"echo x > {f.as_posix()}")
    f.write_text("x")
    assert_free(f"rm {f.as_posix()}")
    assert gg._paths_all_throwaway([str(f)], str(TMPROOT)) is True
    # twin: a FOLDER named gate asks
    g = fresh() / "my_gate" / "a.py"
    assert_decision("deny", "Write", {"file_path": str(g), "content": "x"})
    assert_go(f"echo x > {g.as_posix()}")


# ------------------------------------------------------------------------------------------------ E: S21 regressions (7a5b291)
def test_e_rd_is_a_delete_head_nonempty_asks_empty_plain_is_free():
    full = fresh("full")
    (full / "keep.txt").write_text("x")
    assert_go(f"rd /s /q {full.as_posix()}")
    assert_go(f"rd /s /q {full.as_posix()}", ps=True)
    assert_go(f"rd {full.as_posix()}")  # non-empty: still asks without switches
    empty = fresh("empty")
    assert_free(f"rd {empty.as_posix()}")  # twin: abs, empty, plain temp
    assert_free(f"rmdir {empty.as_posix()}")  # same shape with the older head
    # rd on a sensitive empty dir asks too
    sens = fresh() / ".claude"
    sens.mkdir()
    assert_go(f"rd {sens.as_posix()}")
    assert full.exists() and (full / "keep.txt").exists()


def test_e_redirect_with_unquoted_backslash_target_asks_outside_powershell():
    f = fresh() / "throw.txt"
    native = str(f)
    assert "\\" in native
    assert_go(f"echo x > {native}")  # bash reads D:\tmp\x as D:tmpx
    assert_go(f"echo x >> {native}")
    assert_free(f'echo x > "{native}"')  # twin: quoted
    assert_free(f"echo x > '{native}'")
    assert_free(f"echo x > {native}", ps=True)  # twin: PowerShell keeps backslashes
    assert_free(f"echo x > {f.as_posix()}")  # twin: forward slashes
    # an unquoted backslash target inside a git repo asks in every form (judged by the path too)
    g = git_repo() / "p.txt"
    assert_go(f'echo x > "{g}"')
    assert_go(f"echo x > {g}", ps=True)


def _bare_repo() -> Path:
    p = fresh("bareparent") / "remote.git"
    cp = subprocess.run(["git", "init", "-q", "--bare", str(p)], capture_output=True, text=True)
    assert cp.returncode == 0, cp.stderr
    assert (p / "HEAD").exists() and (p / "objects").is_dir() and (p / "refs").is_dir() and not (p / ".git").exists()
    return p


def test_e_bare_repository_refs_and_hooks_ask_but_a_plain_folder_does_not():
    bare = _bare_repo()
    for target in (bare / "refs" / "heads" / "main", bare / "hooks" / "x", bare / "config",
                   bare / "refs" / "heads" / "sub" / "deeper" / "new"):
        assert_go(f'echo x > "{target}"')
        assert_go(f"echo x > {target.as_posix()}")
        assert_go(f"echo x >> {target.as_posix()}")
        assert_decision("deny", "Write", {"file_path": str(target), "content": "x"})
    sample = next((bare / "hooks").glob("*.sample"), None)
    if sample is not None:
        assert_go(f"rm {sample.as_posix()}")
    # twin: the same relative shape in a plain folder (no HEAD+objects+refs) is free
    plain = fresh("notbare") / "remote.git"
    (plain / "refs" / "heads").mkdir(parents=True)
    (plain / "hooks").mkdir()
    assert_free(f"echo x > {(plain / 'refs' / 'heads' / 'main').as_posix()}")
    assert_free(f"echo x > {(plain / 'hooks' / 'x').as_posix()}")
    assert_decision("allow", "Write", {"file_path": str(plain / "hooks" / "x"), "content": "x"})
    # twin: HEAD alone (a folder that merely has a file named HEAD) does not make a bare repo
    only_head = fresh("onlyhead")
    (only_head / "HEAD").write_text("x")
    assert_free(f"echo x > {(only_head / 'f.txt').as_posix()}")


def _try_make_trailing(parent: Path, name: str) -> Path | None:
    """Create a folder whose name ends in a dot or space, using the extended-length prefix. None if refused."""
    p = parent / name
    try:
        os.mkdir("\\\\?\\" + str(p))
    except OSError:
        return None
    return p if os.path.isdir("\\\\?\\" + str(p)) else None


def test_e_sensitive_names_with_trailing_dot_or_space_ask():
    # pure normalisation first: independent of what the file system lets us create
    assert gg._norm_part(".claude.") == ".claude"
    assert gg._norm_part(".git ") == ".git"
    assert gg._norm_part(".") == "." and gg._norm_part("..") == ".."
    for name in (".claude.", ".git ", ".codex..", ".harness-x. ", "receipts."):
        assert gg._has_sensitive_part(Path("C:\\") / "tmpx" / name / "f.txt"), name
    assert not gg._has_sensitive_part(Path("C:\\") / "tmpx" / "claudefoo." / "f.txt")  # twin
    # the same through the path strings the Write/Edit route and the redirect route use
    created = []
    for name in (".claude.", ".git "):
        parent = fresh()
        made = _try_make_trailing(parent, name)
        if made is not None:
            created.append(made)
        p = parent / name / "f.txt"  # built by Python, passed as a string
        assert gg._paths_all_throwaway([str(p)], str(TMPROOT)) is False, (name, str(p))
        assert_decision("deny", "Write", {"file_path": str(p), "content": "x"})
        assert_go(f'echo x > "{p}"')
    if not created:
        print("NOTE trailing-dot/space folders could not be created on disk; string-built paths and _norm_part were tested")
    ok = fresh() / "claudefoo." / "f.txt"  # twin: not a sensitive name
    assert gg._paths_all_throwaway([str(ok)], str(TMPROOT)) is True
    for c in created:  # remove what we made, extended prefix
        try:
            os.rmdir("\\\\?\\" + str(c))
        except OSError:
            pass


def test_e_empty_dir_under_a_junction_into_a_sensitive_folder_asks():
    sens = fresh() / ".claude"
    sens.mkdir()
    emp = sens / "emp"
    emp.mkdir()
    link = fresh() / "junc"
    cp = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(sens)], capture_output=True, text=True)
    if cp.returncode != 0 or not link.exists():
        print("SKIP junction case: mklink /J could not be created")
        return
    _JUNCTIONS.append(link)
    try:
        via = link / "emp"
        assert via.is_dir() and not any(via.iterdir())
        assert_go(f"rmdir {via.as_posix()}")
        assert_go(f"rd {via.as_posix()}")
        assert_go(f"rm -r {via.as_posix()}")
        assert_go(f"Remove-Item {via.as_posix()}", ps=True)
        assert emp.exists()
    finally:
        os.rmdir(link)  # the link only
    assert emp.exists()
    # twin: the same shape through a junction into a plain temp folder
    plain_t = fresh("plaint")
    pe = plain_t / "emp"
    pe.mkdir()
    link2 = fresh() / "junc2"
    cp = subprocess.run(["cmd", "/c", "mklink", "/J", str(link2), str(plain_t)], capture_output=True, text=True)
    if cp.returncode == 0 and link2.exists():
        _JUNCTIONS.append(link2)
        try:
            assert_free(f"rmdir {(link2 / 'emp').as_posix()}")
        finally:
            os.rmdir(link2)


def test_e_path_longer_than_260_chars_as_only_rm_segment_asks():
    base = fresh("longbase")
    p = base
    while len(str(p)) <= 270:
        p = p / ("a" * 60)
    p = p / "f.txt"
    assert len(str(p)) > 260
    if os.path.lexists(p):
        print("SKIP long path: it exists")
        return
    real_206 = False
    try:
        os.lstat(p)
    except FileNotFoundError as exc:
        real_206 = getattr(exc, "winerror", None) == 206
    except OSError:
        pass
    if real_206:
        assert_go(f"rm {p.as_posix()}")
        assert_go(f'rm "{p}"')
    else:
        print("NOTE long paths are supported here (lstat gives winerror 3, not 206); the 206 branch is driven by a patched os.lstat")
    real_lstat = os.lstat

    def fake(winerror):
        def _l(path, *a, **k):
            if str(path) == str(p):
                e = FileNotFoundError(2, "fake")
                e.winerror = winerror
                raise e
            return real_lstat(path, *a, **k)
        return _l
    os.lstat = fake(206)  # lstat reports "path too long" for the only rm target
    try:
        assert_go(f"rm {p.as_posix()}")
        assert_go(f'rm "{p}"')
        os.lstat = fake(3)  # twin: a genuine "not found" on the same single target stays free
        assert_free(f"rm {p.as_posix()}")
    finally:
        os.lstat = real_lstat
    # twin: a short missing path as the only segment is free
    assert_free(f"rm {(base / 'short-gone.txt').as_posix()}")


# ------------------------------------------------------------------------------------------------ F: S21 third batch
def _outside_os_temp_root(prefix="s21x_") -> Path:
    """A real folder that is NOT under the OS temp folder and not in a git repository (D:\\tmp on this machine)."""
    base = Path(r"D:\tmp")
    if not base.is_dir():
        base = Path(r"C:\Users\Public")
    d = Path(tempfile.mkdtemp(prefix=prefix, dir=str(base))).resolve()
    atexit.register(lambda: shutil.rmtree(d, ignore_errors=True))
    return d


def test_f_harness_temp_roots_valid_absolute_folder_is_honoured_and_bad_entries_are_ignored():
    root = _outside_os_temp_root()
    target = {"file_path": str(root / "sub" / "new.txt"), "content": "x"}
    # without the variable the folder is not a throwaway folder
    assert_decision("deny", "Write", target)
    # twin: with the folder named as a root, a write under it is free
    assert_decision("allow", "Write", target, extra_env={"HARNESS_TEMP_ROOTS": str(root)})
    # bad entries do not widen the throwaway area: drive root, relative and empty
    drive = root.drive + "\\"
    for bad in (drive, "tmp", "", "   "):
        assert_decision("deny", "Write", target, extra_env={"HARNESS_TEMP_ROOTS": bad})
    # a bad entry beside a good one: the good one still works, the bad one adds nothing
    other = _outside_os_temp_root("s21y_")
    t2 = {"file_path": str(other / "n.txt"), "content": "x"}
    assert_decision("deny", "Write", t2, extra_env={"HARNESS_TEMP_ROOTS": drive + os.pathsep + str(root)})
    assert_decision("allow", "Write", target, extra_env={"HARNESS_TEMP_ROOTS": drive + os.pathsep + str(root)})
    # twin: the OS temp folder stays free whatever the variable says
    d = fresh()
    assert_decision("allow", "Write", {"file_path": str(d / "ok.txt"), "content": "x"},
                    extra_env={"HARNESS_TEMP_ROOTS": drive})


def test_f_receipts_and_approvals_file_names_in_a_plain_temp_folder_ask_but_notes_are_free():
    d = fresh()
    for name in ("receipts.jsonl", "approvals.json", "receipt.jsonl", "approval.json"):
        assert_decision("deny", "Write", {"file_path": str(d / name), "content": "x"})
        assert_go(f"echo x > {(d / name).as_posix()}")
    # twin: an ordinary file in the very same folder is free
    assert_decision("allow", "Write", {"file_path": str(d / "notes.txt"), "content": "x"})
    assert_free(f"echo x > {(d / 'notes.txt').as_posix()}")
    assert gg._paths_all_throwaway([str(d / "receipts.jsonl")], str(TMPROOT)) is False
    assert gg._paths_all_throwaway([str(d / "notes.txt")], str(TMPROOT)) is True


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
