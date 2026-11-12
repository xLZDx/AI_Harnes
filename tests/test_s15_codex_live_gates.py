"""S15-T2 behavior tests for Codex GO-gate failure handling.

Run: C:\\Python314\\python.exe -I tests\\test_s15_codex_live_gates.py
"""
from __future__ import annotations

import json
import importlib.util
import io
import os
import sys
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_hooks_dual as dual  # noqa: E402


def _codex_deny_reason(result):
    assert result.kind == "deny", result.dump()
    assert not dual.codex_out_problems(result), result.dump()
    payload = json.loads(result.out)
    return payload["hookSpecificOutput"]["permissionDecisionReason"]


def _mutation_payload(sb, *, ti=None, transcript_path=None):
    _, codex = dual.pl(sb, dual.PRE, tool="Edit", ti=ti or {"file_path": "src/a.py"},
                       xtool="Edit", xti=ti or {"file_path": "src/a.py"}, cwd=sb.root)
    codex["transcript_path"] = str(transcript_path) if transcript_path is not None else None
    return codex


def test_powershell_substitution_requires_go_except_the_accepted_whitelist():
    sb = dual.SB("s15_ps_substitution")
    dual.put_tx(sb, [("user", "please do this change")])

    safe = "Write-Output $($sb.ToString())"
    claude, codex = dual.pl(sb, dual.PRE, tool="PowerShell", ti=dual.bash(safe),
                            xtool="PowerShell", xti=dual.bash(safe), cwd=sb.root)
    dual.trio(sb, "go_gate.py", claude, codex, "allow")

    unsafe = "Write-Output $(Get-Date)"
    claude, codex = dual.pl(sb, dual.PRE, tool="PowerShell", ti=dual.bash(unsafe),
                            xtool="PowerShell", xti=dual.bash(unsafe), cwd=sb.root)
    result = dual.trio(sb, "go_gate.py", claude, codex, "deny")
    assert "command_substitution" in _codex_deny_reason(result["newx"]), result["newx"].dump()


def test_codex_mutation_without_go_or_transcript_fails_closed_while_claude_stays_fail_open():
    sb = dual.SB("s15_missing_context")
    dual.put_tx(sb, [("user", "change the file")])
    c, x = dual.pl(sb, dual.PRE, tool="Edit", ti={"file_path": "src/a.py"},
                   xtool="Edit", xti={"file_path": "src/a.py"}, cwd=sb.root)
    result = dual.trio(sb, "go_gate.py", c, x, "deny")
    assert "GO" in _codex_deny_reason(result["newx"])

    c, x = dual.pl(sb, dual.PRE, tool="Edit", ti={"file_path": "src/a.py"},
                   xtool="Edit", xti={"file_path": "src/a.py"}, cwd=sb.root,
                   xextra={"transcript_path": None})
    c["transcript_path"] = None
    result = dual.trio(sb, "go_gate.py", c, x, "allow", expect_x="deny")
    assert "cannot verify Codex authorization context" in _codex_deny_reason(result["newx"])


def test_codex_mutation_fails_closed_for_unreadable_and_malformed_transcripts():
    sb = dual.SB("s15_bad_transcript")
    unreadable = sb.root / "transcript-directory"
    unreadable.mkdir()
    malformed = sb.root / "malformed.jsonl"
    malformed.write_text("{not valid JSON\n", encoding="utf-8")
    missing = sb.root / "missing.jsonl"

    for name, path in (("missing", missing), ("unreadable", unreadable), ("malformed", malformed)):
        payload = _mutation_payload(sb, transcript_path=path)
        result = dual.run_hook(sb, "newx", "go_gate.py", payload)
        reason = _codex_deny_reason(result)
        assert "cannot verify Codex authorization context" in reason, (name, result.dump())


def test_codex_malformed_pretool_payload_fails_closed():
    sb = dual.SB("s15_malformed_input")
    for raw in ("[]", "{}", "{not JSON"):
        result = dual.run_hook(sb, "newx", "go_gate.py", None, raw=raw)
        reason = _codex_deny_reason(result)
        assert "GO gate" in reason or "authorization context" in reason, (raw, result.dump())


def test_codex_classifier_exception_fails_closed():
    sb = dual.SB("s15_classifier_exception")
    spec = importlib.util.spec_from_file_location("go_gate_s15", dual.NEW / "go_gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    payload = {"tool_name": "Edit", "tool_input": {"file_path": "src/a.py"},
               "cwd": str(sb.root), "session_id": "s15-session", "turn_id": "codex-turn",
               "transcript_path": str(sb.tp_codex), "hook_event_name": dual.PRE}
    output = io.StringIO()
    with patch.object(module, "classify_call", side_effect=RuntimeError("injected classifier failure")), \
            patch.object(module.agentio, "CLIENT", "codex"), \
            patch.dict(os.environ, {"CLAUDE_GO_GATE": ""}), \
            patch.object(module.sys, "stdin", io.StringIO(json.dumps(payload))), \
            patch.object(module.sys, "stdout", output):
        try:
            module.main()
        except SystemExit as exc:
            assert exc.code == 0
    reason = _codex_deny_reason(dual.Res("newx", type("Result", (), {"returncode": 0, "stdout": output.getvalue(), "stderr": ""})()))
    assert "could not classify tool input" in reason


if __name__ == "__main__":
    sys.exit(dual.h.run_all(globals()))
