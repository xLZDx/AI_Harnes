"""S15-T4 behavior tests: go_gate crash handling, audit isolation of broken configs, app-server path keys.

Run: C:\\Python314\\python.exe -I tests\\test_s15_t4_crash_and_paths.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import runpy
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))
sys.path.insert(0, str(HERE.parent / "harness" / "hooks"))
sys.path.insert(0, str(HERE))
import hv2  # noqa: E402
import audit_projects as au  # noqa: E402
import test_s15_codex_audit as base  # noqa: E402
import _agentio  # noqa: E402

GO_GATE = HERE.parent / "harness" / "hooks" / "go_gate.py"
GATE_ENV_KEYS = ("CLAUDE_GO_GATE",)


# ----------------------------------------------------------------------------------------------- A: go_gate crash
def _run_gate_in_process(client: str, state_root: Path):
    """Run go_gate.py as __main__ with shell_command raising; return (exit code, stdout)."""
    payload = {"tool_name": "Bash", "tool_input": {"command": "echo hi"}, "cwd": str(state_root),
               "turn_id": "t1", "transcript_path": str(state_root / ".codex" / "x.jsonl")}
    env = {k: v for k, v in os.environ.items() if k not in GATE_ENV_KEYS}
    env.update({"CLAUDE_GPT_REVIEW_STATE": str(state_root / "gpt"), "HARNESS_HOOK_STATE": str(state_root / "hs"),
                "CLAUDE_HOOK_STATE_DIR": str(state_root / "chs")})
    out = io.StringIO()
    code = None
    with patch.dict(os.environ, env, clear=True), patch.object(_agentio, "CLIENT", client), \
            patch.object(_agentio, "shell_command", side_effect=ZeroDivisionError("boom")), \
            patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
        try:
            runpy.run_path(str(GO_GATE), run_name="__main__")
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue()


def test_go_gate_crash_denies_in_codex_naming_the_exception_type():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        code, out = _run_gate_in_process("codex", Path(tmp))
    assert code == 0, code
    hso = json.loads(out)["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny", out
    assert "cannot verify Codex authorization context (gate crashed: ZeroDivisionError)" in hso["permissionDecisionReason"], out


def test_go_gate_crash_still_allows_in_claude():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        code, out = _run_gate_in_process("claude", Path(tmp))
    assert code == 0, code
    assert out.strip() == "", out  # allow() prints nothing


# ----------------------------------------------------------------------------------------------- B: audit isolation
def test_project_hooks_json_that_is_a_list_gives_one_failed_client_cfg_cell_and_all_other_rows():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        project = root / "project"
        base._write(project / ".harness" / "ledger.json", "{}")
        base._write(project / ".codex" / "hooks.json", "[]")
        home = base._fake_home(root)
        item = base._record(project, [base._hook("user", "trusted", "user guard", home / ".codex" / "hooks.json")])
        with patch.object(au, "HOME", home), patch.object(au, "CODEX_HOOKS_BY_CWD", {au._codex_key(project): item}), \
                patch.object(au, "CODEX_HOOKS_ERROR", None):
            status, detail = au.chk_codex_cfg(project)
            row = au.audit_project(project, github=False)
        assert status == au.FAIL and "invalid hooks structure" in detail, (status, detail)
        assert set(row["cells"]) == set(au.COLS), sorted(row["cells"])
        cfg_status, cfg_detail = row["cells"]["client_cfg"]
        assert cfg_status == au.FAIL and "invalid hooks structure" in cfg_detail and "Traceback" not in cfg_detail, cfg_detail
        assert "check crashed" not in cfg_detail, cfg_detail


def test_a_crash_in_a_client_check_is_one_failed_cell_not_an_aborted_audit():
    with patch.object(au, "chk_gates_live", return_value=(au.OK, "stub")), \
            patch.object(au, "chk_claude_cfg", side_effect=KeyError("k")), \
            patch.object(au, "chk_codex_cfg", side_effect=ValueError("v")):
        claude = au.audit_client("claude-home")
        codex = au.audit_client("codex-home")
    for row, exc in ((claude, "KeyError"), (codex, "ValueError")):
        assert set(row["cells"]) == set(au.COLS)
        status, detail = row["cells"]["client_cfg"]
        assert status == au.FAIL and "check crashed" in detail and exc in detail, (status, detail)
        assert row["cells"]["gates_live"] == (au.OK, "stub")
    # and a crash in gates_live does not take client_cfg down with it
    with patch.object(au, "chk_gates_live", side_effect=RuntimeError("g")), \
            patch.object(au, "chk_claude_cfg", return_value=(au.OK, "fine")):
        row = au.audit_client("claude-home")
    assert row["cells"]["gates_live"][0] == au.FAIL and "RuntimeError" in row["cells"]["gates_live"][1]
    assert row["cells"]["client_cfg"] == (au.OK, "fine")


# ----------------------------------------------------------------------------------------------- C: path keys
def test_codex_key_strips_extended_prefix_and_resolves_dots():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        real = Path(tmp).resolve() / "b"
        real.mkdir()
        plain = au._codex_key(real)
        assert au._codex_key("\\\\?\\" + str(real)) == plain
        assert au._codex_key(str(real.parent / "a" / ".." / "b")) == plain
        assert not plain.startswith("//?/") and "/../" not in plain
    assert au._codex_key("") == "" and au._codex_key(None) == ""


class _FakeProc:
    def __init__(self, lines):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("".join(line + "\n" for line in lines))
        self.returncode = None

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        self.returncode = self.returncode or 0
        return self.returncode


def test_app_server_results_with_extended_prefix_are_found_for_the_project_cwd():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        project = root / "project"
        hooks_file = project / ".codex" / "hooks.json"
        base._write(project / ".harness" / "ledger.json", "{}")
        base._write(hooks_file, json.dumps({"hooks": {"PreToolUse": [
            {"hooks": [{"command": "first_guard.py", "statusMessage": "project first hook"}]}]}}))
        home = base._fake_home(root)
        project_hook = base._hook("project", "trusted", "project first hook", hooks_file, "first_guard.py")
        project_hook["sourcePath"] = "\\\\?\\" + str(hooks_file.resolve())  # app-server style path
        user_hook = base._hook("user", "trusted", "user guard", home / ".codex" / "hooks.json")
        item = {"cwd": "\\\\?\\" + str(project.resolve()), "hooks": [user_hook, project_hook], "warnings": [], "errors": []}
        responses = [json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}),
                     json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"data": [item]}})]
        with patch.object(au, "_codex_executable", return_value="codex.exe"), \
                patch.object(au.subprocess, "Popen", return_value=_FakeProc(responses)):
            by_cwd, error = au.codex_hooks_list([project])
        assert error is None, error
        with patch.object(au, "HOME", home), patch.object(au, "CODEX_HOOKS_BY_CWD", by_cwd), \
                patch.object(au, "CODEX_HOOKS_ERROR", None):
            status, detail = au.chk_codex_cfg(project)
            assert status == au.OK, (status, detail)  # entry found AND prefixed sourcePath matched the project file
            # control: a sourcePath of another file must still be reported missing (the match is not vacuous)
            other = json.loads(json.dumps(by_cwd[au._codex_key(project)]))
            other["hooks"][1]["sourcePath"] = "\\\\?\\" + str((root / "other" / "hooks.json").resolve())
            with patch.object(au, "CODEX_HOOKS_BY_CWD", {au._codex_key(project): other}):
                status, detail = au.chk_codex_cfg(project)
        assert status == au.FAIL and "missing from hooks/list" in detail, (status, detail)


# ----------------------------------------------------------------------------------------------- D: main scope
def test_main_with_only_claude_home_never_calls_app_server_but_full_audit_does():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        empty_root = Path(tmp) / "projects"
        empty_root.mkdir()
        calls = []

        def fake_list(cwds):
            calls.append(list(cwds))
            return {}, None

        def fake_client(name):
            return {"project": name, "cells": {c: (au.NA, "") for c in au.COLS}}

        out = io.StringIO()
        with patch.object(au, "codex_hooks_list", side_effect=fake_list), patch.object(au, "audit_client", side_effect=fake_client), \
                contextlib.redirect_stdout(out):
            rc = au.main(["--root", str(empty_root), "--project", "claude-home", "--json"])
        assert rc == 0 and calls == [], (rc, calls)
        assert [r["project"] for r in json.loads(out.getvalue())] == ["claude-home"]

        out = io.StringIO()
        with patch.object(au, "codex_hooks_list", side_effect=fake_list), patch.object(au, "audit_client", side_effect=fake_client), \
                contextlib.redirect_stdout(out):
            au.main(["--root", str(empty_root), "--project", "codex-home", "--json"])
        assert len(calls) == 1 and calls[0] == [au.HARNESS], calls  # control: codex-home does query it


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
