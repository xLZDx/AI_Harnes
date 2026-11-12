"""S15-T1 behavior tests for the Codex app-server hook audit.

Run: C:\\Python314\\python.exe -I tests\\test_s15_codex_audit.py
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))
sys.path.insert(0, str(HERE))
import hv2  # noqa: E402
import audit_projects as au  # noqa: E402


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _fake_home(root: Path) -> Path:
    home = root / "home"
    _write(home / ".codex" / "hooks.json", json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"command": "rules_gate.py"}]},
        {"matcher": "spawn_agent", "hooks": [{"command": "agent_visibility.py"}]},
    ]}}))
    _write(home / ".codex" / "AGENTS.md", "Operator development rules\n")
    _write(home / ".claude" / "settings.json", json.dumps({"hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"command": "rules_gate.py"}]},
            {"matcher": "Agent", "hooks": [{"command": "agent_visibility.py"}]},
        ],
        "SubagentStart": [{"hooks": [{"command": "agent_visibility.py"}]}],
        "SubagentStop": [{"hooks": [{"command": "agent_visibility.py"}]}],
    }}))
    _write(home / ".claude" / "CLAUDE.md", "Operator development rules\n")
    return home


def _record(cwd: Path, hooks=None, warnings=None, errors=None):
    return {"cwd": str(cwd.resolve()), "hooks": hooks or [], "warnings": warnings or [], "errors": errors or []}


def _hook(source: str, trust: str, status: str, source_path: Path):
    return {"source": source, "trustStatus": trust, "event": "pre_tool_use",
            "statusMessage": status, "sourcePath": str(source_path.resolve())}


def test_codex_audit_lists_every_untrusted_handler_and_missing_project_file_entries():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project = root / "project"
        hooks_file = project / ".codex" / "hooks.json"
        _write(project / ".harness" / "ledger.json", "{}")
        _write(hooks_file, json.dumps({"hooks": {"PreToolUse": [
            {"hooks": [{"command": "first"}, {"command": "second"}]}
        ]}}))
        home = _fake_home(root)
        user_hooks = [
            _hook("user", "modified", "modified user hook", home / ".codex" / "hooks.json"),
            _hook("user", "trusted", "trusted user hook", home / ".codex" / "hooks.json"),
        ]
        project_hooks = [_hook("project", "untrusted", "project spawn gate", hooks_file)]
        item = _record(project, user_hooks + project_hooks)

        with patch.object(au, "HOME", home), \
                patch.object(au, "CODEX_HOOKS_BY_CWD", {au.norm_path(project): item}), \
                patch.object(au, "CODEX_HOOKS_ERROR", None):
            status, detail = au.chk_codex_cfg(project)

        assert status == au.FAIL
        assert "modified user hook" in detail
        assert "trusted user hook" not in detail
        assert "project spawn gate" in detail
        assert "project hooks.json handlers missing from hooks/list: 1 of 2 listed" in detail


def test_codex_audit_reports_warnings_and_errors_from_app_server():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = _fake_home(root)
        project = root / "project"
        item = _record(project, warnings=["hook configuration warning"], errors=["hook load error"])
        with patch.object(au, "HOME", home), \
                patch.object(au, "CODEX_HOOKS_BY_CWD", {au.norm_path(project): item}), \
                patch.object(au, "CODEX_HOOKS_ERROR", None):
            status, detail = au.chk_codex_cfg(project)

        assert status == au.FAIL
        assert 'Codex app-server warnings: ["hook configuration warning"]' in detail
        assert 'Codex app-server errors: ["hook load error"]' in detail


def test_codex_audit_fails_clearly_when_app_server_is_unavailable_or_cwd_missing():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = _fake_home(root)
        project = root / "project"
        with patch.object(au, "HOME", home), \
                patch.object(au, "CODEX_HOOKS_BY_CWD", {}), \
                patch.object(au, "CODEX_HOOKS_ERROR", "Codex app-server hooks/list failed: timed out"):
            status, detail = au.chk_codex_cfg(project)
        assert status == au.FAIL and "timed out" in detail

        with patch.object(au, "HOME", home), \
                patch.object(au, "CODEX_HOOKS_BY_CWD", {}), \
                patch.object(au, "CODEX_HOOKS_ERROR", None):
            status, detail = au.chk_codex_cfg(project)
        assert status == au.FAIL and "returned no entry" in detail


def test_codex_app_server_start_failure_and_invalid_response_fail_closed():
    with patch.object(au, "_codex_executable", return_value="codex.exe"), \
            patch.object(au.subprocess, "Popen", side_effect=OSError("not installed")):
        result, error = au.codex_hooks_list([Path("C:/project")])
    assert result == {}
    assert error == "could not start Codex app-server: OSError: not installed"

    class FakeProcess:
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

    fake = FakeProcess([
        json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"data": None}}),
    ])
    with patch.object(au, "_codex_executable", return_value="codex.exe"), \
            patch.object(au.subprocess, "Popen", return_value=fake):
        result, error = au.codex_hooks_list([Path("C:/project")])
    assert result == {}
    assert error == "Codex app-server hooks/list response has no result.data list"

    malformed = FakeProcess(["[]"])
    with patch.object(au, "_codex_executable", return_value="codex.exe"), \
            patch.object(au.subprocess, "Popen", return_value=malformed):
        result, error = au.codex_hooks_list([Path("C:/project")])
    assert result == {}
    assert error is not None and "not an object" in error.lower()

    hanging = FakeProcess([])
    with patch.object(au, "_codex_executable", return_value="codex.exe"), \
            patch.object(au.subprocess, "Popen", return_value=hanging), \
            patch.object(au, "_codex_rpc_response", side_effect=[{}, TimeoutError("response timeout")]):
        result, error = au.codex_hooks_list([Path("C:/project")])
    assert result == {}
    assert error is not None and "timeout" in error.lower()


def test_audit_queries_all_project_cwds_and_keeps_client_cfg_report_cell():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "projects"
        root.mkdir()
        projects = [root / "one", root / "two"]
        for project in projects:
            _write(project / ".harness" / "ledger.json", "{}")
        home = _fake_home(Path(tmp))
        seen = []

        def fake_hooks_list(cwds):
            seen.extend(cwds)
            return ({au.norm_path(cwd): _record(cwd, [_hook(
                "user", "trusted", "user guard", home / ".codex" / "hooks.json")]) for cwd in cwds}, None)

        output = io.StringIO()
        with patch.object(au, "HOME", home), patch.object(au, "codex_hooks_list", side_effect=fake_hooks_list), \
                patch.object(au, "CODEX_HOOKS_ERROR", None), patch.object(au, "CODEX_HOOKS_BY_CWD", {}), \
                __import__("contextlib").redirect_stdout(output):
            au.main(["--root", str(root), "--no-github", "--json"])

        assert {p.resolve() for p in projects} <= set(seen)
        rows = {row["project"]: row["cells"] for row in json.loads(output.getvalue())}
        assert rows["one"]["client_cfg"]["status"] == "OK"
        assert rows["two"]["client_cfg"]["status"] == "OK"
        assert "client_cfg" in rows["codex-home"]


def test_claude_configuration_check_remains_unchanged():
    with tempfile.TemporaryDirectory() as tmp:
        home = _fake_home(Path(tmp))
        with patch.object(au, "HOME", home):
            status, detail = au.chk_claude_cfg()
        assert status == au.OK
        assert "agent_visibility" in detail


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
