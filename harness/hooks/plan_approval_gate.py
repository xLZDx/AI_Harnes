#!/usr/bin/env python3
"""PreToolUse hook on ExitPlanMode -- closes the silent-auto-approve gap under bypassPermissions.

Found 2026-08-22 (pm-bridge, Gate 8 -> Gate 9): the global settings.json has
`permissions.defaultMode = "bypassPermissions"`. In that mode the ExitPlanMode approval dialog is
never shown to the operator -- the harness auto-approves it -- but the tool call still returns the
same text it would after a real click: "User has approved your plan. You can now start coding." A
session cannot distinguish a genuine approval from a silent auto-approve by that text alone, and one
session did not: it treated the auto-approved return as equivalent to the operator's own literal
GO/GO under CLAUDE.md Sec4, wrote Gate 8 (commit a831af9), and started Gate 9 before anyone had
actually said yes. No PreToolUse hook covered ExitPlanMode at all until this one -- the whole
registered fleet (shell_policy_gate, decision_log_gate, codex_review_gate, gpt_review_gate,
report_gate) matches only Bash|PowerShell.

What this hook does: when permission_mode is bypassPermissions (the one mode where the dialog is
provably not shown), ExitPlanMode is denied unless a genuine operator-typed message containing a
literal GO or GO (Cyrillic) has arrived since the last time this same check consumed one. "Genuine"
reuses _report_common's own definition -- a real user-typed row, not a tool_result, not hook
feedback, not a sidechain/subagent message, not a compaction boundary. "Since the last consumed one"
is a one-way marker (by transcript timestamp, per-session) so a single old GO for gate N cannot
silently reauthorize an unrelated gate N+1 opened later in the same transcript -- mirroring the
Sec4 text itself: "A literal GO / GO authorizes only the currently proposed implementation gate."

In every OTHER permission mode (default/plan/acceptEdits/anything else, including a missing or
unrecognized field), the dialog is real and a click on it is real consent -- this hook allows
unconditionally and does not touch normal interactive plan-mode UX at all.

Deliberately scoped to ExitPlanMode only, not to git commit/push or Edit/Write in general -- this
fixes the specific mechanism that failed (a UI-consent channel silently defeated by a permission
mode), not a general "was there ever a GO" audit, which is a much larger, separate question the
operator did not ask for here.

Fail-open on any missing field, unreadable transcript, or internal error -- a safety net that can
itself hang a session on every plan is worse than no net. This hook can only ever make plan-exit
*stricter* than default Claude Code behavior; it can never grant a permission the harness itself
would not have granted.

Kill switch: CLAUDE_PLAN_APPROVAL_GATE=off (session/process-level, set before this Claude Code
process starts -- same convention as every other kill switch in this hook set).

Claude Code only: Codex has no ExitPlanMode tool, so this hook never fires there (matcher ExitPlanMode)
and, if registered by mistake, allows everything because the payload has no permission_mode=bypassPermissions
ExitPlanMode call. The marker folder now comes from _agentio.state_dir (HARNESS_HOOK_STATE override) instead
of a hard-coded drive path; CLAUDE_HOOK_STATE_DIR still wins when set.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _report_common import _non_human_origin, _operator_text, _tail_rows  # noqa: E402
import _agentio as agentio  # noqa: E402

GO_RE = re.compile(r"\bGO\b|\bГО\b", re.IGNORECASE)


def state_dir() -> Path:
    override = os.environ.get("CLAUDE_HOOK_STATE_DIR")
    if override:
        try:
            target = Path(override) / "claude_plan_approval"
            target.mkdir(parents=True, exist_ok=True)
            return target
        except OSError:
            pass
    return agentio.state_dir("plan_approval")


def marker_path(transcript_path: str) -> Path | None:
    try:
        return state_dir() / f"{Path(transcript_path).stem}.json"
    except OSError:
        return None


def load_marker(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8")).get("timestamp")
    except (OSError, ValueError):
        pass
    return None


def save_marker(path: Path | None, timestamp: str) -> None:
    if path is None or not timestamp:
        return
    try:
        path.write_text(json.dumps({"timestamp": timestamp}), encoding="utf-8")
    except OSError:
        pass


def latest_genuine_go(transcript_path: str, after: str | None) -> str | None:
    """Timestamp of the most recent genuine operator GO/GO row strictly after `after`, else None."""
    best: str | None = None
    for row in _tail_rows(transcript_path, full=True):
        if row.get("type") != "user":
            continue
        if row.get("isCompactSummary") or row.get("isMeta") or row.get("isSidechain"):
            continue
        if _non_human_origin(row):
            continue
        ts = row.get("timestamp") or ""
        if after and ts <= after:
            continue
        text = _operator_text((row.get("message") or {}).get("content"))
        if text and GO_RE.search(text):
            if best is None or ts > best:
                best = ts
    return best


REASON = (
    "PLAN-MODE АВТО-ОДОБРЕНИЕ БЕЗ ПОДТВЕРЖДЕНИЯ: сессия работает в permission_mode=bypassPermissions, "
    "поэтому диалог одобрения плана физически не показывается оператору -- ExitPlanMode вернул бы "
    "\"approved\" без того, чтобы кто-либо это видел (см. инцидент pm-bridge Gate 8->Gate 9, "
    "2026-08-22: план был закоммичен, а реального согласия на переход plan->build не было).\n\n"
    "Не полагайся на возврат этого инструмента как на GO. Вместо этого:\n"
    "  1. Покажи план текстом здесь, в чате (если ещё не показан).\n"
    "  2. Дождись, пока оператор ответит буквально GO или ГО.\n"
    "  3. После этого либо повтори ExitPlanMode, либо просто продолжай реализацию -- сам текстовый "
    "GO/ГО уже достаточен как авторизация (CLAUDE.md §4).\n\n"
    "Kill switch, если это ложное срабатывание: CLAUDE_PLAN_APPROVAL_GATE=off "
    "(process-level, до старта сессии)."
)


def allow():
    sys.exit(0)


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }, ensure_ascii=False))
    sys.exit(0)


def main():
    if os.environ.get("CLAUDE_PLAN_APPROVAL_GATE", "").lower() == "off":
        allow()

    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        allow()
        return

    if data.get("tool_name", "ExitPlanMode") != "ExitPlanMode":  # Codex has no ExitPlanMode; never registered there
        allow()
        return

    if data.get("permission_mode") != "bypassPermissions":
        allow()
        return

    transcript_path = data.get("transcript_path")
    if not transcript_path:
        allow()
        return

    mpath = marker_path(transcript_path)
    after = load_marker(mpath)

    hit = latest_genuine_go(transcript_path, after)
    if hit is None:
        deny(REASON)
        return

    save_marker(mpath, hit)
    allow()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # A safety hook must never be the thing that breaks a session.
        allow()
