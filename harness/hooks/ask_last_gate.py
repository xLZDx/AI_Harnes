#!/usr/bin/env python3
"""Ask-last gate (S7-T4, operator 2026-11-12): do it yourself first; everything not prohibited is allowed, except money.

An ask that wants the operator to provide, find, create, configure or run something about credentials, access, state or
resources is allowed only after the agent has really tried. Proof is taken from the TOOL CALLS AND RESULTS of the current turn
(not from text the agent wrote, GPT-PM plan round 1, MAJOR-01):
  * every category that applies to the topic has at least one call that mentions the topic;
      credentials/access topic: env-var names, config/secret files, CLI auth status;
      state/resource topic:      config files, CLI status, a try-it-yourself action;
  * and either a try-it-yourself call failed with a non-transient error, or every covered result says "not found / not set".
Exempt (the ask stays allowed, the agent must not act): money, unrecoverable deletion, force-push, production data, creating an
account, the operator's own password, CAPTCHA.
Events: PreToolUse AskUserQuestion (Claude) and Stop (prose asks, both clients; blocks once per message).
Fails open on any error. Kill switch: HARNESS_ASK_LAST=off.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _agentio  # noqa: E402
import secret_policy as sp  # noqa: E402
from _turn import read_turn  # noqa: E402

ACTION_RE = re.compile(
    r"\b(?:provide|give|send|paste|share|create|generate|set\s*up|configure|run|rotate|log\s*in|authori[sz]e|enable|upload|grant|add|install|"
    r"export|reissue|change|update)\b|"
    r"(?:дай(?:те)?|предостав\w+|пришл\w+|вставь\w*|созда\w+|сдела\w+|настро\w+|ротиру\w+|залогин\w*|авториз\w+|включ\w+|загруз\w+|выда\w+|"
    r"добав\w+|установ\w+|экспорт\w+|пересозда\w+|смени\w*|измени\w*|поменя\w+|запусти\w*)", re.I)
CRED_TOPIC_RE = re.compile(r"\b(?:key|keys|token|tokens|secret|secrets|credential|credentials|password|api|access|login|auth|iam|service account)\b|"
                           r"ключ|токен|секрет|доступ|парол|учётн|учетн|авториз|логин", re.I)
STATE_TOPIC_RE = re.compile(r"\b(?:state|tfstate|terraform|bucket|backend|resource|config|configuration|settings|gcloud|aws|cloudflare|wrangler)\b|"
                            r"стейт|конфиг|настройк|ресурс|бакет", re.I)
EXEMPT_RE = re.compile(r"force[- ]?push|reset --hard|clean -f|production data|prod(?:uction)? (?:db|database|data)|unrecoverable|permanent(?:ly)? delet|"
                       r"captcha|create (?:an? )?account|your (?:own )?password|enter (?:the |your )?password|"
                       r"прод\w* данн|боев\w+ баз|безвозвратн|навсегда удал|капч|созда\w+ аккаунт|ваш парол|введи\w* парол", re.I)
ENV_RE = re.compile(r"printenv|\benv\b|\$env:|Get-ChildItem\s+env:|\bgci\s+env:|os\.environ|\[\s*-[nz]\s|echo\s+\$\{?#?\w+|\bset\b\s*\|", re.I)
FILES_RE = re.compile(r"\b(?:ls|dir|find|Get-ChildItem|gci|grep|egrep|rg|cat|type|head|tail|Get-Content|Select-String|test\s+-[fe])\b", re.I)
STATUS_RE = re.compile(r"gcloud\s+(?:auth\s+list|config\s+list|projects\s+list|auth\s+application-default)|gh\s+auth\s+status|aws\s+sts\s+get-caller-identity|"
                       r"az\s+account\s+show|wrangler\s+whoami|firebase\s+(?:login:list|projects:list)|terraform\s+(?:state\s+list|show|version|validate|workspace)|"
                       r"kubectl\s+config|vault\s+status|docker\s+(?:info|context)", re.I)
TRY_RE = re.compile(r"(?:^|[;&|(]\s*|&&\s*)(?:cd\s+\S+\s*&&\s*)?(?:terraform|tofu|gcloud|aws|az|gh|wrangler|kubectl|firebase|curl|npm|docker|helm|gsutil)\b", re.I)
NOTFOUND_RE = re.compile(r"(?i)no credentialed|no active account|no accounts|no such file|not found|не найден|cannot find|does not exist|is not set|not set|empty|no matches|^\s*$|нет такого|unset|not logged in|no credentials")
SHELL_NAMES = ("Bash", "PowerShell", "shell", "shell_command")
SEARCH_NAMES = ("Grep", "Glob", "Read", "LS")


def call_text(c: dict) -> str:
    i = c.get("input") or {}
    return " ".join(str(i.get(k) or "") for k in ("command", "cmd", "pattern", "path", "file_path", "glob")) + " " + str(c.get("result") or "")[:4000]


def categories(calls: list[dict], topic_words: set[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {"env_names": [], "config_files": [], "cli_status": [], "try_action": []}
    for c in calls:
        if c.get("is_error") is None and not c.get("result"):
            continue  # a call without a result proves nothing
        txt = call_text(c).lower()
        if topic_words and not any(w in txt for w in topic_words):
            continue
        cmd = str((c.get("input") or {}).get("command") or (c.get("input") or {}).get("cmd") or "")
        if c["name"] in SHELL_NAMES:
            if ENV_RE.search(cmd):
                out["env_names"].append(c)
            if STATUS_RE.search(cmd):
                out["cli_status"].append(c)
            if FILES_RE.search(cmd):
                out["config_files"].append(c)
            if TRY_RE.search(cmd) and not STATUS_RE.search(cmd):
                out["try_action"].append(c)
        elif c["name"] in SEARCH_NAMES:
            out["config_files"].append(c)
    return out


def failed(c: dict) -> bool:
    return c.get("is_error") is True or sp.classify_auth(str(c.get("result") or "")) not in ("unknown",) or \
        bool(re.search(r"(?i)\berror\b|failed|exception|denied|no such|not found", str(c.get("result") or "")[:3000]))


def evidence_problem(ask_text: str, turn: dict) -> str:
    """'' when the ask may go to the operator, else the missing-proof description."""
    if EXEMPT_RE.search(ask_text) or sp.MONEY_TEXT_RE.search(ask_text):
        return ""
    if not ACTION_RE.search(ask_text):
        return ""
    cred, state = bool(CRED_TOPIC_RE.search(ask_text)), bool(STATE_TOPIC_RE.search(ask_text))
    if not (cred or state):
        return ""
    words = {w.lower() for w in re.findall(r"[A-Za-zА-Яа-яЁё_]{4,}", ask_text)} & {
        "key", "keys", "token", "tokens", "secret", "password", "credential", "credentials", "state", "tfstate", "terraform", "gcloud", "cloudflare",
        "aws", "azure", "github", "wrangler", "bucket", "config", "access", "login", "auth", "ключ", "токен", "секрет", "доступ", "стейт", "конфиг", "парол"}
    cats = categories(turn["calls"], {w for w in words if w.isascii()} | {w for w in words if not w.isascii()})
    need = ["env_names", "config_files", "cli_status"] if cred else ["config_files", "cli_status", "try_action"]
    missing = [n for n in need if not cats[n]]
    hint = {"env_names": "list environment variable NAMES (printenv | cut -d= -f1, or [ -n \"$NAME\" ])",
            "config_files": "search project config/secret files by name (ls, find, grep -l) and the repo docs",
            "cli_status": "check the CLI login/status (gcloud auth list, gh auth status, terraform state list, wrangler whoami ...)",
            "try_action": "TRY the action yourself (terraform init/import, run the command, call the API) and read the error"}
    if missing:
        return "missing proof: " + "; ".join(hint[m] for m in missing)
    tries = [c for c in cats["try_action"] if failed(c)]
    real_fail = [c for c in tries if sp.classify_auth(str(c.get("result") or "")) != "transient"]
    covered = [c for n in need for c in cats[n]]
    if real_fail:
        return ""
    if covered and all(NOTFOUND_RE.search(str(c.get("result") or "")) for c in covered):
        return ""
    if cred and not tries:
        # a credential ask also passes when every status/env/file probe returned something unusable
        if all(failed(c) or NOTFOUND_RE.search(str(c.get("result") or "")) for c in cats["cli_status"] + cats["env_names"]):
            return ""
    return "the investigation found something usable or failed only transiently: use what you found, or retry the failed step, before asking"


MSG = ("ASK LAST (operator 2026-11-12): do it yourself first; everything that is not prohibited is allowed, except money. This ask wants the "
       "operator to do something you may be able to do. %s. Then act with what you found (use the keys you have, create the missing state or "
       "resource, change the setting). Come back to the operator only with the results of these attempts.")


def ask_text_of(data: dict) -> str:
    parts = []
    for q in (_agentio.tool_input(data).get("questions") or []):
        if isinstance(q, dict):
            parts += [str(q.get("question", ""))]
            for o in q.get("options") or []:
                if isinstance(o, dict):
                    parts += [str(o.get("label", "")), str(o.get("description", ""))]
    return "\n".join(parts)


def on_pre(data: dict) -> None:
    if not _agentio.is_question(data):
        return
    turn = read_turn(data.get("transcript_path") or "")
    if not turn["calls"] and not turn["human_text"]:
        return
    why = evidence_problem(ask_text_of(data), turn)
    if why:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": MSG % why}}, ensure_ascii=False))


def on_stop(data: dict) -> None:
    if data.get("stop_hook_active"):
        return
    msg = data.get("last_assistant_message") or _agentio.last_assistant_text(data)
    tail = (msg or "")[-900:]
    if not tail.strip():
        return
    last = tail.strip().splitlines()[-1].strip()
    if not (last.rstrip("»)\"'` ").endswith("?") or re.search(r"(?i)could you|can you|please (?:provide|create|send|run)|пожалуйста|не могли бы|нужно, чтобы вы|дайте|пришлите", tail)):
        return
    turn = read_turn(data.get("transcript_path") or "")
    if any(c["name"] in _agentio.QUESTION_TOOLS for c in turn["calls"]):
        return  # went through the question tool: the PreToolUse gate judged it
    why = evidence_problem(tail, turn)
    if not why:
        return
    f = _agentio.state_dir("ask_last") / "seen.json"
    key = hashlib.sha1(tail.encode("utf-8", "replace")).hexdigest()[:16]
    try:
        seen = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seen = []
    if key in seen:
        return
    try:
        _agentio.atomic_write_text(f, json.dumps((seen + [key])[-50:]))
    except OSError:
        pass
    print(json.dumps({"decision": "block", "reason": MSG % why}, ensure_ascii=False))


def main() -> int:
    if os.environ.get("HARNESS_ASK_LAST", "").lower() in {"off", "0", "false"}:
        return 0
    data = _agentio.read_stdin()
    if data.get("agent_type"):
        return 0
    ev = data.get("hook_event_name") or ("PreToolUse" if "tool_input" in data else "Stop")
    try:
        if ev == "PreToolUse":
            on_pre(data)
        elif ev == "Stop":
            on_stop(data)
    except SystemExit:
        raise
    except Exception:
        pass  # fail open
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
