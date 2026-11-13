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


# ------------------------------------------------------------------------------------------ S15-T4
def _codex_rows(sb, texts):
    """Write a Codex transcript of user message rows (response_item/message/user) from raw texts."""
    rows = [{"type": "response_item", "timestamp": f"2026-11-13T10:00:0{i}Z", "payload": {
        "type": "message", "role": "user", "content": [{"type": "input_text", "text": t}]}}
        for i, t in enumerate(texts)]
    sb.tp_codex.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                           encoding="utf-8", newline="\n")


HOOK_TEXT = '<hook_prompt hook_run_id="stop:1:x">Continue: finish the open task.</hook_prompt>'
AGENTS_TEXT = "# AGENTS.md instructions for D:\\Repo\n\n<INSTRUCTIONS>rules</INSTRUCTIONS>"
GO_TEXT = "GO делай"
NO_VALID_GO = "БЕЗ ВАЛИДНОГО GO"


def _bash_payload(sb, cmd="rm -f x"):
    _, codex = dual.pl(sb, dual.PRE, tool="Bash", ti=dual.bash(cmd), cwd=sb.root)
    return codex


def test_s15t4_codex_hook_prompt_row_is_not_the_operator_message_so_earlier_go_counts():
    sb = dual.SB("s15t4_hook_prompt")
    _codex_rows(sb, [GO_TEXT, HOOK_TEXT])
    result = dual.run_hook(sb, "newx", "go_gate.py", _bash_payload(sb))
    assert result.kind == "allow", result.dump()


def test_s15t4_hook_prompt_alone_is_not_an_operator_go():
    sb = dual.SB("s15t4_hook_prompt_alone")
    _codex_rows(sb, [HOOK_TEXT])
    reason = _codex_deny_reason(dual.run_hook(sb, "newx", "go_gate.py", _bash_payload(sb)))
    assert reason.startswith(NO_VALID_GO), reason[:200]


def test_s15t4_codex_subagent_transcript_without_operator_text_reaches_normal_deny():
    sb = dual.SB("s15t4_subagent_deny")
    _codex_rows(sb, [AGENTS_TEXT])
    reason = _codex_deny_reason(dual.run_hook(sb, "newx", "go_gate.py", _bash_payload(sb)))
    assert reason.startswith(NO_VALID_GO), reason[:200]
    assert "cannot verify" not in reason


def test_s15t4_codex_subagent_transcript_is_allowed_by_a_gpt_pm_approve_receipt():
    sb = dual.SB("s15t4_subagent_receipt")
    repo = sb.repo("r15")
    _codex_rows(sb, [AGENTS_TEXT])
    _, codex = dual.pl(sb, dual.PRE, tool="Edit", ti={"file_path": str(repo / "a.py")}, cwd=repo)
    before = dual.run_hook(sb, "newx", "go_gate.py", codex, cwd=repo)
    assert before.kind == "deny", before.dump()
    dual.write_receipt(sb, repo, verdict="APPROVE")
    after = dual.run_hook(sb, "newx", "go_gate.py", codex, cwd=repo)
    assert after.kind == "allow", after.dump()


def test_s15t4_claude_with_the_same_transcript_stays_fail_open():
    sb = dual.SB("s15t4_claude_open")
    _codex_rows(sb, [AGENTS_TEXT])
    claude, _ = dual.pl(sb, dual.PRE, tool="Bash", ti=dual.bash("rm -f x"), cwd=sb.root)
    claude["transcript_path"] = str(sb.tp_codex)
    result = dual.run_hook(sb, "new", "go_gate.py", claude)
    assert result.kind == "allow", result.dump()


def test_s15t4_codex_missing_path_and_directory_transcript_still_cannot_verify():
    sb = dual.SB("s15t4_cannot_verify")
    codex = _bash_payload(sb)
    codex["transcript_path"] = None
    assert "cannot verify Codex authorization context" in _codex_deny_reason(
        dual.run_hook(sb, "newx", "go_gate.py", codex))
    folder = sb.root / "transcript-dir"
    folder.mkdir()
    codex["transcript_path"] = str(folder)
    reason = _codex_deny_reason(dual.run_hook(sb, "newx", "go_gate.py", codex))
    assert "cannot verify Codex authorization context" in reason, reason[:200]


def test_s15t4_codex_transcript_with_only_invalid_json_line_cannot_verify():
    sb = dual.SB("s15t4_invalid_json_only")
    sb.tp_codex.write_text("{not valid JSON\n", encoding="utf-8", newline="\n")
    reason = _codex_deny_reason(dual.run_hook(sb, "newx", "go_gate.py", _bash_payload(sb)))
    assert "cannot verify Codex authorization context" in reason, reason[:200]


if __name__ == "__main__":
    sys.exit(dual.h.run_all(globals()))
