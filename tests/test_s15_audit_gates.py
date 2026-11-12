"""S15-T3 tests for audit_projects gate probe isolation and cleanup.

Run: C:\\Python314\\python.exe -I tests\\test_s15_audit_gates.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_s13_audit as prior  # noqa: E402

au = prior.au
hv2 = prior.hv2


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _combined_home() -> Path:
    root = hv2.fresh("s15_audit_gate_home")
    claude = prior._claude_home()
    codex = prior._codex_home()
    shutil.copytree(claude / ".claude", root / ".claude")
    shutil.copytree(codex / ".codex", root / ".codex", dirs_exist_ok=True)
    return root


def _approved_repo(parent: Path) -> Path:
    repo = parent / "approved-harness"
    repo.mkdir(parents=True)
    assert hv2.git(repo, "init", "-q", "-b", "main").rc == 0
    hv2.git(repo, "config", "user.name", "test")
    hv2.git(repo, "config", "user.email", "test@example.invalid")
    _write(repo / "README.md", "temporary approval context\n")
    assert hv2.git(repo, "add", "README.md").rc == 0
    assert hv2.git(repo, "commit", "-q", "-m", "init").rc == 0
    head = hv2.git(repo, "rev-parse", "HEAD").out.strip()
    _write(repo / ".harness" / "ledger.json", json.dumps({
        "version": 1,
        "project": "approved-harness-fixture",
        "approval": {"head": head, "result": "APPROVE"},
        "sprints": [{"id": "S00", "state": "open", "tasks": [{"id": "S00-T1", "state": "done"}]}],
    }))
    assert au.approval_on_head(repo)
    assert hv2.git(repo, "remote").out.strip() == ""
    return repo


def _audit_scope(home: Path, harness: Path, audit_tmp: Path):
    hooks, error = prior._fake_codex_hooks([harness], home)
    assert error is None
    return (
        patch.object(au, "HOME", home),
        patch.object(au, "HARNESS", harness),
        patch.object(au, "AUDIT_TMP", audit_tmp),
        patch.object(au, "DENY_CMDS", ["git push"]),
        patch.object(au, "ALLOW_CMDS", ["git status"]),
        patch.object(au, "CODEX_HOOKS_BY_CWD", hooks),
        patch.object(au, "CODEX_HOOKS_ERROR", None),
    )


def _enter_all(patches):
    from contextlib import ExitStack
    stack = ExitStack()
    for item in patches:
        stack.enter_context(item)
    return stack


def test_client_rows_probe_push_from_no_approval_no_remote_repo_and_remove_it():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = _combined_home()
        harness = _approved_repo(root)
        audit_tmp = root / "audit-temp"
        audit_tmp.mkdir()
        observed = []
        lock = threading.Lock()
        real_run_gate = au.run_gate

        def record_probe(handlers, command, cwd, state_tmp, client):
            if command == "git push":
                ledger = json.loads((cwd / ".harness" / "ledger.json").read_text(encoding="utf-8"))
                remotes = hv2.git(cwd, "remote")
                with lock:
                    observed.append((cwd, client, ledger.get("approval"), remotes.out.strip(), remotes.rc))
            return real_run_gate(handlers, command, cwd, state_tmp, client)

        with _enter_all(_audit_scope(home, harness, audit_tmp)), patch.object(au, "run_gate", side_effect=record_probe):
            claude = au.audit_client("claude-home")
            codex = au.audit_client("codex-home")

        assert claude["cells"]["gates_live"][0] == au.OK, claude["cells"]["gates_live"]
        assert codex["cells"]["gates_live"][0] == au.OK, codex["cells"]["gates_live"]
        assert len(observed) == 2, observed
        assert {entry[1] for entry in observed} == {"claude", "codex"}, observed
        for probe, _client, approval, remotes, remote_rc in observed:
            assert probe != harness and probe != harness.resolve(), (probe, harness)
            assert approval is None, (probe, approval)
            assert remote_rc == 0 and remotes == "", (probe, remotes, remote_rc)
            assert not probe.exists(), f"disposable probe repo was left behind: {probe}"


def test_project_row_skips_push_only_for_approval_matching_project_head():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = _combined_home()
        harness = _approved_repo(root)
        audit_tmp = root / "audit-temp"
        audit_tmp.mkdir()
        project = prior._mk_project(root, "project")
        head = hv2.git(project, "rev-parse", "HEAD").out.strip()
        ledger_path = project / ".harness" / "ledger.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        ledger["approval"] = {"head": head, "result": "APPROVE"}
        _write(ledger_path, json.dumps(ledger))
        seen = []
        real_run_gate = au.run_gate

        def record_project_gate(handlers, command, cwd, state_tmp, client):
            seen.append(command)
            return real_run_gate(handlers, command, cwd, state_tmp, client)

        with _enter_all(_audit_scope(home, harness, audit_tmp)), patch.object(au, "run_gate", side_effect=record_project_gate):
            matched = au.audit_project(project, github=False)

        gate_status, gate_detail = matched["cells"]["gates_live"]
        assert gate_status == au.OK, (gate_status, gate_detail)
        assert "git push skipped" in gate_detail, gate_detail
        assert "git push" not in seen, seen

        ledger["approval"] = {"head": "stale-head", "result": "APPROVE"}
        _write(ledger_path, json.dumps(ledger))
        seen.clear()
        with _enter_all(_audit_scope(home, harness, audit_tmp)), patch.object(au, "run_gate", side_effect=record_project_gate):
            stale = au.audit_project(project, github=False)

        gate_status, gate_detail = stale["cells"]["gates_live"]
        assert gate_status == au.OK, (gate_status, gate_detail)
        assert "git push skipped" not in gate_detail, gate_detail
        assert "git push" in seen, seen


def test_client_probe_repo_is_removed_when_probe_fails_or_raises():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = _combined_home()
        harness = _approved_repo(root)
        audit_tmp = root / "audit-temp"
        audit_tmp.mkdir()
        handlers = au.registered_handlers(au.load_json(home / ".claude" / "settings.json"), "PreToolUse", "Bash")
        probes = []

        def no_denial(_handlers, command, cwd, _tmp, _client):
            if command == "git push":
                probes.append(cwd)
            return None, []

        with _enter_all(_audit_scope(home, harness, audit_tmp)), patch.object(au, "run_gate", side_effect=no_denial):
            status, detail = au.chk_gates_live(harness, home / ".claude" / "settings.json", "claude", False)
        assert status == au.FAIL and "not denied: git push" in detail, (status, detail)
        assert len(probes) == 1 and not probes[0].exists(), probes

        probes.clear()

        def raise_during_probe(_handlers, _command, cwd, _tmp, _client):
            probes.append(cwd)
            raise RuntimeError("simulated hook probe failure")

        with _enter_all(_audit_scope(home, harness, audit_tmp)), patch.object(au, "run_gate", side_effect=raise_during_probe):
            try:
                au.chk_gates_live(harness, home / ".claude" / "settings.json", "claude", False)
            except RuntimeError as exc:
                assert str(exc) == "simulated hook probe failure"
            else:
                raise AssertionError("probe exception was swallowed")
        assert probes and all(not probe.exists() for probe in probes), probes


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
