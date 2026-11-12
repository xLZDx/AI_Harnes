#!/usr/bin/env python3
"""Secret exposure and credential misuse guard (S7-T5). One script, two events.

PreToolUse (shell + Read):
  1. Money: CLI commands that spend money (billing, purchases, creating billable instances) are denied for the agent: the operator decides.
  2. Printing secret VALUES into the transcript is denied: reading .env/.tfvars/key files, dumping the environment, echoing a
     secret-named variable, printing access tokens, inline interpreters doing the same. Name-only checks pass (length, -n, -c,
     key names).
  3. Credential-changing commands (create/roll/delete of keys and tokens, terraform destroy / state rm) are denied unless one of:
     the operator's current message names the action; a fresh `credential-invalid` auth record exists for the SAME credential,
     service and project (R3-2); or the credential is under a confirmed exposure and the standing operator order allows rotation
     (R3-3). Deleting or revoking the old credential also needs a verified replacement (created, then a successful call).
PostToolUse (shell): records auth evidence {credential id, service, project, operation, class} from failing/succeeding service
  calls; scans the output for secret VALUES (detective only: it cannot undo what is already in the transcript) and starts the
  containment checklist. No value is ever written anywhere: only type, short sha256 prefix and a credential reference.

Standing operator order 2026-11-12 ("everything not prohibited is allowed except money") is the permission source for rotating a
CONFIRMED exposure; disable with HARNESS_ROTATION_STANDING_ORDER=0. Kill switch: HARNESS_SECRET_GUARD=off. Fails open on internal
errors, except that the explicit patterns above deny.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _agentio  # noqa: E402
import secret_policy as sp  # noqa: E402
from _turn import read_turn  # noqa: E402

READER_RE = re.compile(r"(?:^|[;&|(`]\s*|&&\s*)(?:cat|type|more|less|head|tail|bat|gc|Get-Content|Select-String|sls|grep|egrep|rg|awk|sed|"
                       r"cut|sort|strings|xxd|base64|jq|python[\d.]*\s+-m\s+json\.tool)\b", re.I)
GREP_COUNT_RE = re.compile(r"\b(?:grep|egrep|rg|Select-String|sls)\b[^|]*\s-[a-zA-Z]*[clqL]\b|--count|--quiet|--files-with-matches|--files-without-match", re.I)
MASK_RE = re.compile(r"\|\s*(?:wc|measure|Measure-Object)\b|cut\s+-d\s*['\"]?=['\"]?\s+-f\s*1\b|"
                     r"sed\s+(?:-[Ee]\s+)?['\"]s[/|#].*=\s*\.\*[/|#].*\*{3}|awk\s+-F\s*['\"]?=['\"]?\s+['\"]\{\s*print\s+\$1\s*\}|"
                     r"Select-Object\s+-ExpandProperty\s+Name|\.Name\b|\[\s*-[nz]\s|test\s+-[nz]\s|\$\{#\w+\}|\$\{\w+:\+", re.I)
ENV_DUMP_RE = re.compile(r"(?:^|[;&|(]\s*|&&\s*)(?:printenv|env|set|export\s+-p|declare\s+-x|(?:Get-ChildItem|gci|ls|dir)\s+env:)\s*(?:$|[|;&>])", re.I)
PRINTENV_NAME_RE = re.compile(r"(?:^|[;&|(]\s*|&&\s*)printenv\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)
ECHO_SECRET_RE = re.compile(
    r"(?:^|[;&|(]\s*|&&\s*)(?:echo|printf|Write-Host|Write-Output|print|puts)\b[^\n;&|]*"
    r"(?:\$\{?[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Za-z0-9_]*\}?|%[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Za-z0-9_]*%|"
    r"\$env:\w*(?:KEY|TOKEN|SECRET|PASSWORD)\w*)", re.I)
TOKEN_PRINT_RE = re.compile(r"gcloud\s+auth\s+(?:application-default\s+)?print-(?:access|identity)-token|aws\s+configure\s+get\s+aws_secret_access_key|"
                            r"gh\s+auth\s+token|az\s+account\s+get-access-token|vault\s+read\b|op\s+read\b", re.I)
CAPTURED_RE = re.compile(r"(?:=\s*|\$\(\s*|`\s*|Set-Variable\b[^\n]*)\s*$|\|\s*(?:curl|xargs|clip|docker\s+login|gh\s+auth\s+login|wrangler|jq\s+-r\s+['\"]?\.\w+)", re.I)
INLINE_RE = re.compile(r"(?:python[\d.]*|py|node|ruby|perl|php)\s+(?:-[a-zA-Z]+\s+)*-[cer]\b|(?:powershell|pwsh)\S*\s+(?:-\w+\s+)*-(?:Command|c)\b", re.I)
INLINE_PRINT_RE = re.compile(r"print|echo|write|console\.log|puts|Write-", re.I)
INLINE_SECRET_RE = re.compile(r"(?:environ|getenv|process\.env|\$env:|ENV)\W{0,6}[A-Za-z_]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.I)
INLINE_SAFE_RE = re.compile(r"\blen\s*\(|\bbool\s*\(|\.length\b|is not None|\bin os\.environ|\.startswith|\[:\s*0\s*\]", re.I)
OPERATOR_NAMED_RE = re.compile(r"rotat|ротац|пересозда|recreat|revoke|отозв|delete (?:the )?key|удали(?:ть)? ключ|создай (?:новый )?ключ|"
                               r"terraform destroy|state rm|destroy", re.I)
CONTAINMENT = ("SECRET EXPOSED (type %s, id %s, fingerprint %s; the value is not repeated here). Contain it: (1) never repeat the value; redact it "
               "in messages, files and commits; (2) find the owner and the dependent services read-only from repo/config; (3) rotation is a "
               "separate step that the guard allows only for a CONFIRMED exposure of a credential in this project, in the safe order create-new "
               "-> switch consumers -> verify -> delete-old, with the credentials you already hold (standing operator order 2026-11-12); (4) a "
               "false positive or unknown owner: do nothing destructive and say so; (5) ask the operator only with the evidence of what you tried.")


def deny(reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}, ensure_ascii=False))


def state() -> Path:
    return _agentio.state_dir("evidence")


def log(rec: dict) -> None:
    try:
        with (state() / "secret_guard.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **rec}, ensure_ascii=False) + "\n")
    except OSError:
        pass


QUOTED_RE = re.compile(r"\"([^\"]*)\"|'([^']*)'")
SEG_SPLIT_RE = re.compile(r"\n|;|&&|\|\|")


def _masked(seg: str) -> bool:
    return bool(MASK_RE.search(seg) or GREP_COUNT_RE.search(seg))


def _operand_tokens(seg: str) -> list[str]:
    """Words of a segment that can be file operands. A quoted string that looks like a regex or a sentence (a search pattern, a
    commit message) is not an operand."""
    def unquote(m):
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        return " " + inner + " " if not re.search(r"[\\^$|()\s*+?\[\]{}]", inner) else " "
    return re.findall(r"[^\s'\"|;&<>()]+", QUOTED_RE.sub(unquote, seg))


def printing_problem(cmd: str) -> str:
    """Reason when the command would print secret values, else ''. Each command segment is judged on its own: a mask in
    another segment (`cat .env; ls -l`) does not excuse it."""
    if INLINE_RE.search(cmd) and INLINE_PRINT_RE.search(cmd) and not INLINE_SAFE_RE.search(cmd):
        # an inline script holds `;` inside its code, so it is judged as a whole, before the split into segments
        if INLINE_SECRET_RE.search(cmd) or any(sp.SENSITIVE_FILE_RE.search(t.replace("\\", "/")) for t in _operand_tokens(cmd)):
            return "an inline script prints a secret; print only presence or length"
    for seg in SEG_SPLIT_RE.split(cmd):
        if not seg.strip():
            continue
        why = _segment_problem(seg)
        if why:
            return why
    for mt in TOKEN_PRINT_RE.finditer(cmd):
        before, after = cmd[:mt.start()], cmd[mt.end():]
        if not (CAPTURED_RE.search(before[-40:] + "") or re.match(r"\s*(?:\)|`)?\s*\|\s*(?:curl|xargs|clip|docker|gh|wrangler|jq)", after)):
            return "it prints an access token into the transcript; capture it in a variable or pipe it straight into the tool that needs it"
    return ""


def _segment_problem(seg: str) -> str:
    sens = [t for t in _operand_tokens(seg) if sp.SENSITIVE_FILE_RE.search(t.replace("\\", "/"))]
    masked = _masked(seg)
    if sens and READER_RE.search(seg) and not masked:
        return f"it prints the content of a secret file ({os.path.basename(sens[0])}); check names only, for example grep -c, grep -l or cut -d= -f1"
    if ENV_DUMP_RE.search(seg) and not masked:
        return "it dumps the whole environment; use a filtered name list (cut -d= -f1) or test one variable with [ -n \"$NAME\" ]"
    m = PRINTENV_NAME_RE.search(seg)
    if m and sp.SECRET_NAME_RE.search(m.group(1)) and not masked:
        return f"it prints the value of {m.group(1)}; test it with [ -n \"${m.group(1)}\" ] or print its length"
    mo = re.search(r"terraform\s+output\b([^\n;&|]*)", seg, re.I)
    if mo and not [a for a in mo.group(1).split() if not a.startswith("-")] and not masked:
        return "terraform output without an output name prints every value, sensitive ones included; ask for one named output"
    if ECHO_SECRET_RE.search(seg) and not masked:
        return "it echoes a secret variable; test it with [ -n \"$NAME\" ] or print its length instead"
    if INLINE_RE.search(seg) and INLINE_PRINT_RE.search(seg) and not INLINE_SAFE_RE.search(seg):
        if sens or INLINE_SECRET_RE.search(seg):
            return "an inline script prints a secret; print only presence or length"
    return ""


NEGATION_RE = re.compile(r"(?i)(?:\bnot\b|\bn't\b|\bnever\b|\bno\b|\bwithout\b|\bне\b|\bнельзя\b|\bбез\b|\bни в коем\b)\W+(?:\w+\W+){0,3}$")


def operator_named(text: str) -> bool:
    """The operator's message names the action in an affirmative sentence: a match counts when it is not negated just before it and
    the message is not a question."""
    if not text or text.strip().endswith("?"):
        return False
    for m in OPERATOR_NAMED_RE.finditer(text):
        if not NEGATION_RE.search(text[max(0, m.start() - 40):m.start()]):
            return True
    return False


def change_problem(cmd: str, kind: str, data: dict) -> str:
    """Reason when a credential-changing command is not allowed yet, else ''."""
    turn = read_turn(data.get("transcript_path") or "")
    if operator_named(turn["human_text"] or ""):
        return ""  # the operator's current message names the action
    if kind == "state":
        return ("terraform destroy / state rm change infrastructure or its record; the operator's message must name this action. "
                "If the state file is missing, recreate it with terraform init / import instead")
    rows = sp.load_records(state())
    cred, svc, proj = sp.credential_id_of(cmd), sp.service_of(cmd), sp.project_of(cmd)
    ok, note = sp.auth_evidence_ok(rows, cred, svc, proj, strict=True)
    exposure = sp.exposure_state(rows, cred) if cred and not cred.startswith("default:") else ""
    standing = os.environ.get("HARNESS_ROTATION_STANDING_ORDER", "1") != "0"
    allowed_by_exposure = exposure in ("confirmed", "authorized") and standing
    if not (ok or allowed_by_exposure):
        extra = "" if exposure != "detected" else "; a detected (unconfirmed) leak never unlocks a change"
        return (f"credential change ({kind}) for {cred or 'an unknown credential'} / {svc or 'unknown service'} / project {proj or '?'} has no proof: "
                f"{note}{extra}. First test the EXISTING key with a read-only call and fix the cause (project, scope, endpoint); "
                "re-create a key only after it is proven invalid for this exact credential")
    if kind == "delete":
        if sp.rotation_for_delete(rows, sp.target_id_of(cmd), svc, proj) is None:
            return ("deleting or revoking the old credential needs a verified replacement of THIS rotation (same service and project): create the "
                    "new one, switch the consumers, run a successful read-only call with the NEW one, then delete the old. The key this command removes "
                    "is not proven to be the OLD credential of that rotation (credential-identity-unresolved; how a command authenticates says nothing "
                    "about its target): the operator names this deletion, or the rotation record carries the old key id")
    return ""


def on_pre(data: dict) -> None:
    name = _agentio.tool_name(data)
    if name == "Read":
        path = str(_agentio.tool_input(data).get("file_path") or "")
        if path and sp.SENSITIVE_FILE_RE.search(path.replace("\\", "/")):
            log({"event": "deny-read", "file": os.path.basename(path)})
            deny(f"Secret guard: reading {os.path.basename(path)} would put secret values into the transcript. Check key NAMES with grep -c / "
                 "cut -d= -f1, or use the value inside a command without printing it.")
        return
    if name == "Grep":
        ti = _agentio.tool_input(data)
        target = str(ti.get("path") or "") + " " + str(ti.get("glob") or "")
        if str(ti.get("output_mode") or "") == "content" and any(sp.SENSITIVE_FILE_RE.search(t.replace("\\", "/").replace("*", fill).replace("?", fill)) for t in target.split() for fill in ("", "x")):
            log({"event": "deny-grep"})
            deny("Secret guard: Grep with output_mode=content on a secret file would print its values; use output_mode=count or files_with_matches.")
        return
    if not _agentio.is_shell(data):
        return
    cmd = _agentio.shell_command(data) or ""
    if not cmd.strip():
        return
    if sp.money_command(cmd):
        log({"event": "deny-money"})
        deny("Secret guard (money): this command spends or commits money (billing, purchase or a billable resource). Only the operator decides "
             "about money; ask with the cost and the reason.")
        return
    why = printing_problem(cmd)
    if why:
        log({"event": "deny-print", "why": why[:60]})
        deny(f"Secret guard: {why}.")
        return
    kind = sp.change_kind(cmd)
    if kind:
        why = change_problem(cmd, kind, data)
        if why:
            log({"event": "deny-change", "kind": kind, "cred": sp.credential_id_of(cmd), "service": sp.service_of(cmd)})
            deny(f"Secret guard: {why}.")


def response_text(data: dict) -> str:
    r = data.get("tool_response")
    if r is None:
        r = data.get("tool_output")
    if r is None:
        r = data.get("error")  # PostToolUseFailure carries the failure text here (first line "Exit code N")
    if isinstance(r, str):
        return r[:200000]
    try:
        return json.dumps(r, ensure_ascii=False)[:200000]
    except (TypeError, ValueError):
        return str(r)[:200000]


def _add(d: Path, rec: dict) -> None:
    if not sp.add_record(d, rec):
        log({"event": "evidence-not-written", "kind": rec.get("kind")})  # lock timeout: the proof is lost, say so


def on_post(data: dict, failure: bool = False) -> None:
    """PostToolUse (success) and PostToolUseFailure (a command that exited non-zero never reaches PostToolUse)."""
    if not _agentio.is_shell(data):
        return
    cmd = _agentio.shell_command(data) or ""
    text = response_text(data)
    svc = sp.service_of(cmd)
    cred, proj = sp.credential_id_of(cmd), sp.project_of(cmd)
    ok = False if failure else _agentio.command_succeeded(data)  # None = unknown: records nothing (never counted as a failure)
    kind = sp.change_kind(cmd)
    d = state()
    if svc and cred:
        if ok is False:
            cls = sp.classify_auth(text)
            if cls != "unknown":
                _add(d, {"kind": "auth", "credential_id": cred, "service": svc, "project": proj, "operation": kind or "call", "class": cls})
        elif ok is True:
            _add(d, {"kind": "auth", "credential_id": cred, "service": svc, "project": proj, "operation": kind or "call", "class": "ok"})
            rows = sp.load_records(d)
            if kind in ("create", "roll"):
                _add(d, {"kind": "rotation", "credential_id": cred, "old_credential_id": cred, "service": svc, "project": proj,
                         "step": "created", "rotation_id": f"{svc}:{int(time.time() * 1000)}"})
            elif kind == "delete":
                rot = sp.rotation_for_delete(rows, sp.target_id_of(cmd), svc, proj)
                if rot:
                    _add(d, {"kind": "rotation", "service": svc, "project": proj, "step": "old_revoked", "rotation_id": rot.get("rotation_id")})
            elif not kind and not cred.startswith("default:"):
                # verified = a successful call with an EXPLICIT credential other than the one that was replaced, for the same project
                verified_ids = {r.get("rotation_id") for r in rows if r.get("kind") == "rotation" and r.get("step") == "verified"}
                created = [r for r in rows if r.get("kind") == "rotation" and r.get("service") == svc and r.get("step") == "created"
                           and (r.get("project") or "") == proj and r.get("old_credential_id") != cred and r.get("rotation_id") not in verified_ids]
                if len(created) == 1:  # two unverified rotations of one service/project: which one this call proves is unknown, so record nothing
                    c = created[0]
                    _add(d, {"kind": "rotation", "credential_id": cred, "old_credential_id": c.get("old_credential_id"), "service": svc,
                             "project": proj, "step": "verified", "rotation_id": c.get("rotation_id")})
    hits = sp.find_secrets(text)
    if not hits:
        return
    notes = []
    for h in hits[:3]:
        ref = "value:" + h["fp"]
        if cred.startswith("env:"):  # link the leak to a credential only when the live value of that variable has the same fingerprint
            live = os.environ.get(cred[4:])
            if live and sp.fingerprint(live) == h["fp"]:
                ref = cred
        _add(d, {"kind": "exposure", "credential_id": ref, "state": "confirmed" if h["known"] else "detected",
                 "type": h["type"], "fp": h["fp"], "service": svc})
        log({"event": "exposure", "type": h["type"], "fp": h["fp"], "state": "confirmed" if h["known"] else "detected"})
        notes.append(CONTAINMENT % (h["type"], ref, h["fp"]))
    msg = " | ".join(notes[:1]) + (f" (+{len(notes) - 1} more)" if len(notes) > 1 else "")
    if _agentio.client(data) == "codex":
        sys.stderr.write("secret_guard: " + msg + "\n")
    else:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}}, ensure_ascii=False))


def main() -> int:
    if os.environ.get("HARNESS_SECRET_GUARD", "").lower() in {"off", "0", "false"}:
        return 0
    data = _agentio.read_stdin()
    ev = data.get("hook_event_name") or ("PostToolUse" if "tool_response" in data else "PreToolUse")
    try:
        if ev == "PreToolUse":
            on_pre(data)
        elif ev == "PostToolUse":
            on_post(data)
        elif ev == "PostToolUseFailure":
            on_post(data, failure=True)
    except SystemExit:
        raise
    except Exception as exc:  # fail open on internal errors
        log({"event": "internal-error", "error": repr(exc)[:120]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
