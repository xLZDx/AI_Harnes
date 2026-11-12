"""Shared secret / credential policy (S7-T5, T4). Pure functions, stdlib only, no I/O except the evidence store.

Contracts (GPT-PM plan rounds 1-3):
  * Secret VALUES are never stored, logged or echoed. Credentials are referenced by an id without the value
    (env var NAME, key file basename, key id, or "default:<service>"); a leaked value is correlated only by a short sha256 prefix.
  * Auth evidence is bound to {credential_id, service, project, operation, class, at}; it unlocks a credential change only for the
    SAME credential id, service and project within EVIDENCE_TTL_S (R3-2).
  * Exposure has three states, none of which is implied by another: detected -> confirmed -> authorized (R3-3).
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

EVIDENCE_TTL_S = 3600
MAX_RECORDS = 200

SECRET_NAME_RE = re.compile(r"(?:API[_-]?)?(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE)", re.I)
SENSITIVE_FILE_RE = re.compile(
    r"(?:^|[\\/])(?:\.env(?:\.(?!example$|sample$|template$|dist$|tpl$)[\w.-]+)?|[\w.-]*\.tfvars|[\w.-]*\.pem|[\w.-]*\.p12|[\w.-]*\.pfx|"
    r"id_(?:rsa|ed25519|ecdsa)(?:\.(?!pub$)[\w.-]+)?|\.netrc|\.npmrc|\.pypirc|credentials\.json|\.aws[\\/]credentials|"
    r"[\w.-]*service[-_]?account[\w.-]*\.json|[\w.-]*(?:secret|private)[\w.-]*\.(?:json|ya?ml|txt|key))$",
    re.I)

# value patterns: known provider formats (confirmed-capable) and a generic labelled value (detected only)
KNOWN_VALUE_RES = (
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google-oauth", re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[0-9A-Za-z]{30,}\b|\bgithub_pat_[0-9A-Za-z_]{40,}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}\b")),
    ("openai-style-key", re.compile(r"\bsk-[0-9A-Za-z_-]{20,}\b")),
)
GENERIC_VALUE_RE = re.compile(r"(?i)(?<![\w-])([\w-]{0,40}(?:token|secret|password|passwd|api[_-]?key|access[_-]?key)[\w-]{0,40})\s*[=:]\s*['\"]?([A-Za-z0-9_\-/+=.]{24,})")
FIXTURE_HINT_RE = re.compile(r"(?i)fake|example|dummy|placeholder|test[-_/\\]|fixture|sample|your[-_]|xxxx|<[a-z_ -]+>")

# commands that change, create or destroy credentials
CRED_CHANGE_RES = (
    ("create", re.compile(r"gcloud\s+iam\s+service-accounts\s+keys\s+(?:create|upload)|gcloud\s+services\s+api-keys\s+create|"
                          r"aws\s+iam\s+create-access-key|az\s+ad\s+(?:sp|app)\s+credential\s+reset|"
                          r"api\.cloudflare\.com/client/v4/(?:user|accounts/[^/\s]+)/tokens(?!/verify)[^\n]*?(?:-X\s*POST|--request\s*POST)|"
                          r"(?:-X\s*POST|--request\s*POST)[^\n]*api\.cloudflare\.com/client/v4/(?:user|accounts/[^/\s]+)/tokens(?!/verify)", re.I)),
    ("roll", re.compile(r"/tokens/[^/\s]+/value[^\n]*(?:-X\s*PUT|--request\s*PUT)|(?:-X\s*PUT|--request\s*PUT)[^\n]*/tokens/[^/\s]+/value", re.I)),
    ("delete", re.compile(r"gcloud\s+iam\s+service-accounts\s+keys\s+(?:delete|disable)|gcloud\s+auth\s+revoke|gcloud\s+services\s+api-keys\s+delete|"
                          r"aws\s+iam\s+(?:delete-access-key|update-access-key)|gh\s+auth\s+logout|"
                          r"kubectl\s+delete\s+secret|"
                          r"(?:-X\s*DELETE|--request\s*DELETE)[^\n]*api\.cloudflare\.com/client/v4/(?:user|accounts/[^/\s]+)/tokens|"
                          r"api\.cloudflare\.com/client/v4/(?:user|accounts/[^/\s]+)/tokens[^\n]*(?:-X\s*DELETE|--request\s*DELETE)", re.I)),
    ("state", re.compile(r"terraform\s+state\s+(?:rm|mv|push)\b|terraform\s+destroy\b|tofu\s+destroy\b", re.I)),
)
MONEY_CMD_RE = re.compile(
    r"gcloud\s+(?:beta\s+)?(?:billing|commitments|compute\s+(?:instances|disks|addresses|reservations)\s+create|sql\s+instances\s+create|"
    r"container\s+clusters\s+create|spanner\s+instances\s+create)|aws\s+(?:ec2\s+run-instances|rds\s+create-db-instance|"
    r"savingsplans\s+create|ec2\s+purchase-\S+|support\s+create-case|organizations\s+create-account)|"
    r"az\s+(?:vm|aks|sql\s+server|mysql\s+server|postgres\s+server|reservations)\s+create|"
    r"stripe\s+(?:charges|payment_intents|payouts|transfers)\s+create", re.I)
# words in a QUESTION or request text that mean the operator decides about spending (never used on shell commands)
MONEY_TEXT_RE = re.compile(
    r"\b(?:purchase|buy|billing|invoice|subscription|subscribe|upgrade (?:the )?plan|paid plan|pay(?:ment)?s?|transfer funds|trade|trading|order)\b|"
    r"оплат|куп(?:и|ить|лю)|покуп|счёт|счет\b|подписк|тариф|перевод\s+средств|сделк|платн", re.I)

AUTH_CLASS_RES = (
    ("transient", re.compile(r"(?i)time[- ]?out|timed out|\b5\d\d\b(?! *(?:ms|files|lines))|connection (?:refused|reset|aborted)|temporar|rate.?limit|try again|"
                             r"could not resolve|network is unreachable|econn")),
    ("permission-insufficient", re.compile(r"(?i)\b403\b|permission[ _]denied|insufficient (?:permission|scope)|forbidden|not authorized to perform|"
                                           r"access denied|does not have .*permission|required permission")),
    ("config-error", re.compile(r"(?i)project .{0,40}(?:not found|does not exist|invalid)|invalid project|unknown (?:flag|option|command)|"
                                r"unrecognized arguments|missing (?:required|argument)|no such (?:file|project|zone|region)|could not load the default credentials|"
                                r"not logged in|no credentials|bad request|\b400\b|\b404\b|not found")),
    ("credential-invalid", re.compile(r"(?i)\b401\b|unauthorized|invalid[_ ](?:api[_ ])?(?:token|key|credential|grant)|expired|revoked|bad credentials|"
                                      r"authentication failed|invalid_grant|token (?:is )?(?:invalid|expired)|api key not valid")),
)
SERVICE_RES = (("gcp", re.compile(r"\bgcloud\b|googleapis\.com|\bgsutil\b|\bfirebase\b", re.I)),
               ("aws", re.compile(r"\baws\b|amazonaws\.com", re.I)),
               ("cloudflare", re.compile(r"cloudflare|\bwrangler\b", re.I)),
               ("github", re.compile(r"\bgh\b|api\.github\.com", re.I)),
               ("azure", re.compile(r"\baz\b\s|azure", re.I)),
               ("terraform", re.compile(r"\bterraform\b|\btofu\b", re.I)),
               ("kubernetes", re.compile(r"\bkubectl\b|\bhelm\b", re.I)))


# ---------------------------------------------------------------- parsing helpers

HEREDOC_RE = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n.*?\n\s*\1\b", re.S)
SEGMENT_SPLIT_RE = re.compile(r"\n|;|&&|\|\||\|")
CMD_HEAD_RE = re.compile(r"^\s*(?:sudo\s+|env\s+|time\s+|\w+=\S*\s+|cd\s+\S+\s*&&\s*)*(?:gcloud|gsutil|aws|az|gh|wrangler|kubectl|terraform|tofu|stripe|curl|http|Invoke-RestMethod|Invoke-WebRequest|irm|iwr)\b", re.I)


def command_segments(cmd: str) -> list[str]:
    """Segments that START with a cloud/CLI executable. Heredoc bodies and everything that is only an argument or a quoted
    message (git commit -m "...", grep "...", echo ...) are not segments, so mentioning a command is not running it."""
    text = HEREDOC_RE.sub("\n", cmd or "")
    return [seg for seg in SEGMENT_SPLIT_RE.split(text) if CMD_HEAD_RE.match(seg)]


def service_of(cmd: str) -> str:
    for name, rx in SERVICE_RES:
        if rx.search(cmd or ""):
            return name
    return ""


def project_of(cmd: str) -> str:
    m = re.search(r"--project[= ]\s*['\"]?([\w.:-]+)|--account[- ]id[= ]\s*['\"]?([\w-]+)|(?<![\w-])-p\s+([\w-]{4,})|accounts/([0-9a-f]{8,})", cmd or "")
    return next((g for g in (m.groups() if m else ()) if g), "")


def credential_id_of(cmd: str) -> str:
    """Reference to the credential a command uses or changes, never its value."""
    cmd = cmd or ""
    m = re.search(r"--key-file[= ]\s*['\"]?([^\s'\"]+)", cmd)
    if m:
        return "file:" + re.split(r"[\\/]", m.group(1))[-1]
    m = re.search(r"keys\s+(?:delete|disable)\s+['\"]?([0-9a-fA-F]{6,})", cmd)
    if m:
        return "keyid:" + m.group(1)[:6]
    m = re.search(r"--access-key-id[= ]\s*['\"]?(AKIA[0-9A-Z]{4})", cmd)
    if m:
        return "keyid:" + m.group(1)[:6]
    for rx in (r"\$\{?([A-Z][A-Z0-9_]*)\}?", r"%([A-Z][A-Z0-9_]*)%", r"\$env:([A-Za-z][A-Za-z0-9_]*)", r"\b([A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|CREDENTIALS?))="):
        for m in re.finditer(rx, cmd):
            if SECRET_NAME_RE.search(m.group(1)) or m.group(1) in ("GOOGLE_APPLICATION_CREDENTIALS",):
                return "env:" + m.group(1).upper()
    svc = service_of(cmd)
    return f"default:{svc}" if svc else ""


def classify_auth(text: str) -> str:
    """credential-invalid | permission-insufficient | config-error | transient | unknown (priority: transient first)."""
    t = text or ""
    for name, rx in AUTH_CLASS_RES:
        if rx.search(t):
            return name
    return "unknown"


def change_kind(cmd: str) -> str:
    """create | roll | delete | state, only for a command that really starts with the tool (see command_segments).
    A curl pipeline is judged on the whole command, because its URL and method sit in different pipeline parts."""
    whole = HEREDOC_RE.sub("\n", cmd or "")
    for seg in command_segments(cmd):
        for kind, rx in CRED_CHANGE_RES:
            if rx.search(seg):
                return kind
    if re.match(r"^\s*(?:\w+=\S*\s+)*(?:curl|http|Invoke-RestMethod|irm)\b", whole, re.I):
        for kind, rx in CRED_CHANGE_RES:
            if rx.search(whole):
                return kind
    return ""


def money_command(cmd: str) -> bool:
    return any(MONEY_CMD_RE.search(seg) for seg in command_segments(cmd))


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:8]


def find_secrets(text: str) -> list[dict]:
    """[{type, known, fp, label}] for secret VALUES in text (never returns the value itself)."""
    out, seen = [], set()
    for typ, rx in KNOWN_VALUE_RES:
        for m in rx.finditer(text or ""):
            v = m.group(0)
            if FIXTURE_HINT_RE.search(text[max(0, m.start() - 40):m.end() + 40]):
                continue
            fp = fingerprint(v)
            if fp not in seen:
                seen.add(fp)
                out.append({"type": typ, "known": True, "fp": fp, "label": typ})
    for m in GENERIC_VALUE_RE.finditer(text or ""):
        v = m.group(2)
        if FIXTURE_HINT_RE.search(text[max(0, m.start() - 40):m.end() + 40]) or len(set(v)) < 8:
            continue
        fp = fingerprint(v)
        if fp not in seen:
            seen.add(fp)
            out.append({"type": "labelled-value", "known": False, "fp": fp, "label": m.group(1).upper()[:40]})
    return out


# ---------------------------------------------------------------- evidence store

def _store(state_dir: Path) -> Path:
    return state_dir / "records.json"


def load_records(state_dir: Path) -> list[dict]:
    try:
        d = json.loads(_store(state_dir).read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def add_record(state_dir: Path, rec: dict) -> bool:
    from _agentio import atomic_write_text, file_lock
    f = _store(state_dir)
    with file_lock(f, wait=1.0) as got:
        if not got:
            return False
        rows = load_records(state_dir)
        rows.append({**rec, "at": time.time()})
        atomic_write_text(f, json.dumps(rows[-MAX_RECORDS:]))
        return True


def fresh(rec: dict, now: float | None = None) -> bool:
    return (now or time.time()) - float(rec.get("at", 0)) <= EVIDENCE_TTL_S


def auth_evidence_ok(rows: list[dict], cred: str, service: str, project: str, strict: bool = False) -> tuple[bool, str]:
    """(ok, mismatch note). Needs a fresh credential-invalid record for the SAME credential, service and project."""
    near = [r for r in rows if r.get("kind") == "auth" and fresh(r)]
    same = sorted([r for r in near if r.get("credential_id") == cred and r.get("service") == service
                   and ((r.get("project") or "") == project if strict else (not project or not r.get("project") or r.get("project") == project))],
                  key=lambda r: r.get("at", 0))  # strict (change commands): the project must be the same, an empty one matches only an empty one
    if same and same[-1].get("class") == "credential-invalid":  # the LATEST check decides: a later success clears it
        return True, ""
    if same:
        return False, "the latest check of this credential was classified " + same[-1].get("class", "unknown") + ", not credential-invalid"
    if near:
        return False, "no fresh auth check for credential %s / service %s / project %s (found: %s)" % (
            cred or "?", service or "?", project or "?", ", ".join(sorted({f"{r.get('credential_id')}@{r.get('service')}" for r in near})[:4]))
    return False, "no fresh auth check of this credential in this session"


def open_rotations(rows: list[dict], service: str, project: str) -> list[dict]:
    """Verified rotations (created -> verified) of this service and project from the last 24 h whose old credential was not revoked yet."""
    now = time.time()
    revoked = {r.get("rotation_id") for r in rows if r.get("kind") == "rotation" and r.get("step") == "old_revoked"}
    return [r for r in rows if r.get("kind") == "rotation" and r.get("step") == "verified" and r.get("service") == service
            and (r.get("project") or "") == project and r.get("rotation_id") not in revoked and now - float(r.get("at", 0)) < 86400]


def target_id_of(cmd: str) -> str:
    """The credential a delete/revoke command REMOVES (not the one that authenticates it): `keyid:<first 6 chars>` or ''.
    Authentication (--key-file, $TOKEN) says nothing about the target, so it is never used here."""
    cmd = cmd or ""
    m = re.search(r"keys\s+(?:delete|disable)\s+['\"]?([0-9a-zA-Z]{6,})", cmd) or         re.search(r"--access-key-id[= ]\s*['\"]?([A-Z0-9]{8,})", cmd) or         re.search(r"(?:delete|revoke)[-\w]*\s+(?:--[\w-]+[= ]\S+\s+)*['\"]?(?:key[-_]?id[= ]|keys/|tokens?/)?([0-9a-zA-Z]{8,})", cmd, re.I)
    return "keyid:" + m.group(1)[:6] if m else ""


def rotation_for_delete(rows: list[dict], target: str, service: str, project: str) -> dict | None:
    """The verified rotation whose OLD credential is exactly the credential this delete removes. `target` is target_id_of(cmd), never
    the authentication credential. The match needs a recorded `old_key_id` of the transaction (same service and project, <24 h, not
    old_revoked). No recorded id, no target, or a different id => None (credential-identity-unresolved)."""
    if not target:
        return None
    pair = [v for v in open_rotations(rows, service, project) if v.get("old_key_id") and v.get("old_key_id") == target]
    return pair[-1] if pair else None


def exposure_state(rows: list[dict], cred: str) -> str:
    """Highest exposure state for a credential id: authorized > confirmed > detected > ''."""
    order = {"detected": 1, "confirmed": 2, "authorized": 3}
    best = 0
    for r in rows:  # exposure records live 24 h (a detected-only record is dropped after that, R3-3)
        if r.get("kind") == "exposure" and r.get("credential_id") == cred and (time.time() - float(r.get("at", 0))) < 86400:
            best = max(best, order.get(r.get("state"), 0))
    return {0: "", 1: "detected", 2: "confirmed", 3: "authorized"}[best]
