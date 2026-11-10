#!/usr/bin/env python3
"""PreToolUse hook -- two independent checks, both via Bash/PowerShell. Operator GO,
2026-08-21 (Gate 5 of the PM Bridge project), forked
from codex_review_gate.py with the operator's explicit instruction to REPLACE the
(currently suspended) Codex mandatory-review gate with this one, same granularity and
same mechanics, only the reviewer and transport differ:

  1. `git commit` is blocked unless src/cli/review.js (PM Bridge CLI) has been
     invoked (ANY receipt, any round/final status) for this repo recently -- proves a
     GPT review was at least ATTEMPTED for this gate.
  2. `git push` is blocked unless the MOST RECENT receipt for this repo has final=True --
     proves the gate's GPT consensus loop actually CONCLUDED, not just attempted once.
     See ~/.claude/CLAUDE.md "GPT Consensus Review (via PM Bridge)" -> "No push before
     the final round".

Why this exists: same failure mode codex_review_gate.py and decision_log_gate.py were
both built to close -- a text-only MANDATORY rule to send every gate's diff to GPT before
committing gets silently skipped without a mechanical gate. See codex_review_gate.py's own
header for the fuller history of that failure shape; this hook is a structural fork, not a
new design.

What the commit check verifies is ATTEMPT, not SUCCESS. review.js writes a receipt line to
RECEIPTS_FILE on every run, including its own fail-open error paths (login expired,
ChatGPT markup changed, conversation not found, timeout). A receipt with ok=false still
satisfies the commit gate -- the reviewer being unavailable must never block the operator's
actual work, only an unattempted review does.

What the push check verifies is CONCLUSION: the LATEST receipt for the repo must carry
final=True (set via review.js's --final flag on the last round of that gate's loop).

No quota-style short-TTL receipt class exists here (unlike codex_review_gate.py's
usage_limit_exhausted handling) -- ChatGPT's web UI has no equivalent machine-readable
quota signal to detect. This is a known, honestly-documented open gap (same posture as
the Codex quota gap already accepted in ~/.claude/CLAUDE.md), not solved in this version.

No trivial-commit carve-out, deliberately, mirroring decision_log_gate.py and
codex_review_gate.py -- a gameable skip mechanism would defeat the point.

Scope, honestly (identical caveats to codex_review_gate.py):
- Catches git commit/push invoked through Claude Code's Bash/PowerShell tools only.
- A compound command containing both `git commit` and `git push` is held to the STRICTER
  push requirement (final receipt) for the whole command.
- Fails open on any internal error -- logged, never silent, never crashes the session.

Kill switch: CLAUDE_GPT_REVIEW_GATE=off -- SESSION/PROCESS-level env var, same rule as
CLAUDE_CODEX_REVIEW_GATE=off: must be set before this Claude Code process starts; inline
in the gated command has no effect. Covers both checks.

SCOPED TO pm-bridge ONLY as of 2026-08-21 (operator instruction, same day this gate was
built): the transport is still maturing -- new-conversation-per-send routing gaps, a live
"too many requests" incident, and a self-triggering detector bug were all found and fixed
during Gate 6's own build. The operator asked to keep the mandatory gate exercising itself
on pm-bridge (so it keeps getting hardened) while NOT blocking every other repo on this
machine on a mechanism still being debugged. See ENABLED_REPO_ROOTS below -- a repo whose
root is not under one of those paths skips BOTH checks entirely (allow, logged as
"allow-scoped-off"), same as if the whole gate were off for that repo. Widen or remove this
allowlist once the operator judges the mechanism reliable enough for other projects; ~/.claude/
CLAUDE.md section 15 carries the dated note.

Decision log: <state folder>/decisions.jsonl
Receipts read from: <state folder>/receipts.jsonl  (written by the PM Bridge review.js CLI)
State folder: CLAUDE_GPT_REVIEW_STATE, else _agentio.state_dir("gpt_review_gate"). Point review.js at the same folder.

Claude Code and Codex: the shell command is read through _agentio. The "allow with warning" output
(permissionDecision "allow") is not supported by Codex, so there the warning goes to stderr instead.
Config (environment, no hard-coded paths): CLAUDE_GPT_REVIEW_ROOTS (os.pathsep-separated repo roots the gate
covers; unset = every repo), CLAUDE_GPT_REVIEW_TRANSPORT_REPO (folder name of the repo that IS the review
transport and pushes on a fresh receipt; default "pm-bridge"), CLAUDE_GPT_REVIEW_CLI (review.js path shown in
messages).
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _agentio as agentio  # noqa: E402

STATE_DIR = Path(os.environ.get("CLAUDE_GPT_REVIEW_STATE") or agentio.state_dir("gpt_review_gate"))
REVIEW_CLI = os.environ.get("CLAUDE_GPT_REVIEW_CLI") or "<pm-bridge>/src/cli/review.js"
TRANSPORT_REPO = os.environ.get("CLAUDE_GPT_REVIEW_TRANSPORT_REPO") or "pm-bridge"
DECISIONS_FILE = STATE_DIR / "decisions.jsonl"
RECEIPTS_FILE = STATE_DIR / "receipts.jsonl"
RECEIPT_MAX_AGE_HOURS = 6
PUSH_RECEIPT_MAX_AGE_HOURS = 24

# Temporary scope-down, operator instruction 2026-08-21 (see module docstring). Originally
# pm-bridge-only (every other repo unblocked, pm-bridge itself still gated so the mechanism
# kept exercising and hardening itself). Widened the SAME day to empty: pm-bridge's own
# push kept blocking on a real, unresolved detection bug in review.js's reply-wait logic
# (a fresh navigation repeatedly reported a stale turn count that didn't reflect a
# confirmed-real GPT reply -- see D:\Repo\pm-bridge\core\DECISION_LOG.md, "Gate 6
# aftermath, continued" and "... push-GO despite the unresolved bug"), and the operator
# explicitly asked to lift the gate for pm-bridge too rather than keep chasing more live
# rounds against that bug. Empty list = every repo allows through unconditionally, i.e.
# this whole hook is currently a no-op everywhere. Repopulate (with pm-bridge, or any
# repo) once the mechanism is trusted enough to re-cover it.
#
# Re-enabled for Fitness_App, 2026-08-22 (operator instruction, mid-P1.G4-gate session):
# the same underlying pm-bridge/playwright transport had just proven itself reliable in
# this exact session -- pm_set_gate's auto-notification to GPT-PM for P1.G4's close went
# through cleanly and got a real, on-topic reply. Scoped to Fitness_App only, not widened
# to every repo on the machine -- same conservative, one-repo-at-a-time posture as the
# original pm-bridge-only scoping.
#
# Widened to EVERY repo under D:\Repo, 2026-08-22, same day, later (direct operator
# instruction: "Мандаторный GPT-review гейт (§15) сейчас глобально включи"). `D:\Repo`
# itself as the sole root means `is_under()` matches any repo underneath it, not just
# Fitness_App/pm-bridge. Honest caveat, not resolved by this change: the specific
# unresolved bug that caused the SAME-day empty-list widening (review.js's reply-wait
# logic intermittently reporting a stale turn count after a fresh navigation -- see
# D:\Repo\pm-bridge\core\DECISION_LOG.md, "Gate 6 aftermath, continued") has not been
# independently re-verified fixed. A separate, different bug (duplicate ChatGPT tabs from
# an in-process race in Gate 8's session fast path) was fixed the same day
# (D:\Repo\pm-bridge commit 56f4317) -- that is NOT the same defect as the stale-turn-count
# one; do not treat one fix as evidence the other is resolved. This is a fail-open gate
# (an unattempted/failed review never blocks a commit, only an absent one does; push still
# requires final=True), so the practical risk of widening now is degraded review coverage
# on other repos' pushes, not a hard block -- but it means "mandatory GPT review" may in
# practice mean "attempted, possibly against a stale reply" more often outside Fitness_App/
# pm-bridge until that bug is separately confirmed fixed.
# Now configurable: CLAUDE_GPT_REVIEW_ROOTS (os.pathsep-separated). Unset = every repo is covered.
ENABLED_REPO_ROOTS = [r for r in os.environ.get("CLAUDE_GPT_REVIEW_ROOTS", "").split(os.pathsep) if r.strip()]
ALL_REPOS_ENABLED = not ENABLED_REPO_ROOTS

# Fitness_App push-gate excluded, 2026-08-26 (operator instruction, live recurrence of the exact
# bug documented above): commit 1decbc5e17690b0f3269d51fba2d32d9c9aad21a was reviewed to genuine
# consensus -- GPT-PM said VERDICT: APPROVE / PUSH: AUTHORIZED for this exact SHA four times in a
# row (once in chat, three times via review.js --final, rounds 9-11), with specific, accurate,
# mutually consistent content each time -- but all three review.js rounds came back
# correlated:false, so --final was mechanically withheld and push stayed blocked despite genuine
# operator authorization (chosen explicitly over retrying again, after 3 straight failures). This
# is the stale-turn-count detector bug from the comment above, not a content/consensus problem.
# Excluding this one repo (commit gate stays active for it; only the PUSH final-receipt check is
# skipped) rather than re-emptying ENABLED_REPO_ROOTS for every repo again. Revert by removing
# this block once review.js's correlation detector is independently confirmed fixed.
#
# pm-bridge push-gate excluded, 2026-08-26 (operator instruction: "нужно сделать так чтобы пм
# бридж имел право пушить"). Different reason from Fitness_App's, and a permanent one rather than
# a workaround: pm-bridge IS the review transport. Requiring a pm-bridge-produced final receipt
# before pm-bridge itself may be pushed is circular -- any defect in the transport blocks shipping
# the fix for that defect, and the more broken it is the more firmly it is locked. Gate A hit this
# directly: 22 review rounds were spent on the very machinery the gate depends on, and a session
# ended up asking the operator to choose between setting a kill switch, waiting indefinitely, or
# marking --final by hand on a review that had not concluded. A gate that can only be satisfied by
# faking its own evidence is not providing the guarantee it appears to.
#
# The COMMIT gate stays active for pm-bridge deliberately, so the repo keeps exercising its own
# review path on every commit -- only the push-side final-receipt requirement is skipped. This is
# not "pm-bridge is exempt from review"; it is "pm-bridge cannot be its own release authority".
# Reviews still run, receipts are still written, and their content still has to be read.
#
# NOTE: pm-bridge is deliberately NOT in this list. Its exemption is CONDITIONAL and implemented
# separately in main() -- GPT-PM's review of the unconditional first version returned a BLOCKER
# and was right. See the branch above the EXCLUDED_REPO_ROOTS check in main().
#
# Fitness_App push-gate exclusion REMOVED, 2026-08-30 (direct operator instruction: "надо включить
# обратно" -- re-enable it, in response to a report that surfaced this exclusion). The 2026-08-26
# rationale above (review.js correlation-detector bug withholding `final:true` despite a genuine
# GPT-PM APPROVE) is left in place as history, not deleted -- if that bug recurs, re-add
# r"D:\Repo\Fitness_App" here rather than rediscovering the same diagnosis from scratch. Until then,
# Fitness_App push follows the same final-receipt requirement as every other non-excluded repo
# under ENABLED_REPO_ROOTS.
EXCLUDED_REPO_ROOTS = []

COMMIT_RE = re.compile(r"\bgit\s+commit\b", re.IGNORECASE)
PUSH_RE = re.compile(r"\bgit\s+push\b", re.IGNORECASE)
SKIP_RE = re.compile(r"--help\b|(?<!\S)-h\b|--dry-run\b", re.IGNORECASE)
CD_RE = re.compile(r'^\s*cd\s+(?P<path>"[^"]+"|\'[^\']+\'|\S+)\s*$')

# See codex_review_gate.py for the measured incident this guards against (Git-Bash
# `/d/Repo/x` style paths silently resolving to the wrong drive-joined path on Windows).
_GITBASH_DRIVE_RE = re.compile(r"^/([A-Za-z])(?=/|$)")


def normalise_shell_path(path):
    m = _GITBASH_DRIVE_RE.match(path)
    if not m:
        return path
    rest = path[2:].replace("/", "\\")
    return f"{m.group(1).upper()}:{rest or chr(92)}"


def resolve_effective_cwd(command, base_cwd):
    cwd = base_cwd
    for part in re.split(r"&&|;", command):
        m = CD_RE.match(part.strip())
        if not m:
            continue
        path = normalise_shell_path(m.group("path").strip("\"'"))
        if os.path.isabs(path) or re.match(r"^[A-Za-z]:", path) or path.startswith(("/", "\\")):
            cwd = path
        else:
            cwd = str(Path(cwd) / path)
    return cwd


def log_decision(entry):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        entry["ts"] = datetime.now(timezone.utc).isoformat()
        with open(DECISIONS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def allow():
    sys.exit(0)


def deny(reason, extra=None):
    log_decision({"decision": "deny", "reason": reason, **(extra or {})})
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def git(args, cwd=None, timeout=10):
    return subprocess.run(
        ["git"] + (["-C", cwd] if cwd else []) + args,
        capture_output=True, text=True, timeout=timeout,
        creationflags=_NO_WINDOW,
    )


def is_under(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return str(Path(path).resolve()) == str(Path(root).resolve())


def is_transport_repo(repo_root):
    return Path(repo_root).name.lower() == TRANSPORT_REPO.lower()


def warn_allow(payload, warn_reason):
    """Allow-with-warning: Claude shows permissionDecisionReason; Codex does not support "allow" output."""
    if agentio.client(payload) == "codex":
        sys.stderr.write(warn_reason + "\n")
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": warn_reason,
        }
    }))


def recent_receipt(repo_root):
    """Most recent fresh-enough receipt for this repo, or None. Returns the full entry (not just
    a bool) so the caller can tell an actual pass from a fail-open attempt -- see the commit-path
    warning added 2026-08-21, mirroring the one the push path already had."""
    if not RECEIPTS_FILE.is_file():
        return None
    now = datetime.now(timezone.utc)
    try:
        with open(RECEIPTS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None
    for line in reversed(lines[-500:]):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts_raw = entry.get("timestamp")
        cwd = entry.get("cwd")
        if not ts_raw or not cwd:
            continue
        try:
            ts = datetime.fromisoformat(ts_raw)
        except ValueError:
            continue
        age_hours = (now - ts).total_seconds() / 3600.0
        if age_hours > RECEIPT_MAX_AGE_HOURS:
            continue
        if is_under(cwd, repo_root):
            return entry
    return None


def latest_receipt_for_repo(repo_root):
    if not RECEIPTS_FILE.is_file():
        return None
    try:
        with open(RECEIPTS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None
    for line in reversed(lines[-500:]):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        cwd = entry.get("cwd")
        if not cwd:
            continue
        if is_under(cwd, repo_root):
            return entry
    return None


def final_receipt_fresh(repo_root):
    entry = latest_receipt_for_repo(repo_root)
    if not entry:
        return False, "no review.js receipt found at all", None
    if not entry.get("final"):
        return False, f"latest receipt is round {entry.get('round', '?')} (not marked --final) -- the GPT consensus loop hasn't concluded yet", entry
    ts_raw = entry.get("timestamp")
    if not ts_raw:
        return False, "latest final receipt has no timestamp", entry
    try:
        ts = datetime.fromisoformat(ts_raw)
    except ValueError:
        return False, "latest final receipt has an unparseable timestamp", entry
    age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
    if age_hours > PUSH_RECEIPT_MAX_AGE_HOURS:
        return False, f"latest final receipt is {age_hours:.1f}h old (> {PUSH_RECEIPT_MAX_AGE_HOURS}h) -- treat as stale, re-run the review", entry
    return True, None, entry


def main():
    if os.environ.get("CLAUDE_GPT_REVIEW_GATE", "").lower() == "off":
        allow()

    try:
        payload = json.loads(sys.stdin.read())
    except Exception as e:
        log_decision({"decision": "fail-open", "reason": f"bad stdin: {e}"})
        allow()

    command = agentio.shell_command(payload)
    if SKIP_RE.search(command):
        allow()

    is_push = bool(PUSH_RE.search(command))
    is_commit = bool(COMMIT_RE.search(command))
    if is_commit:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from _commit_detect import is_commit_command
            is_commit = is_commit_command(command)  # not a commit when only mentioned in a string/heredoc
        except Exception:
            pass
    if not is_push and not is_commit:
        allow()

    raw_cwd = payload.get("cwd") or os.getcwd()
    cwd = resolve_effective_cwd(command, raw_cwd)

    try:
        toplevel = git(["rev-parse", "--show-toplevel"], cwd=cwd)
        if toplevel.returncode != 0:
            log_decision({"decision": "fail-open", "reason": "not a git repo or git error",
                          "cwd": cwd, "stderr": toplevel.stderr.strip()[:300]})
            allow()
        repo_root = toplevel.stdout.strip()
    except Exception as e:
        log_decision({"decision": "fail-open", "reason": f"git rev-parse failed: {e}", "cwd": cwd})
        allow()

    if not ALL_REPOS_ENABLED and not any(is_under(repo_root, root) for root in ENABLED_REPO_ROOTS):
        log_decision({"decision": "allow-scoped-off",
                      "reason": "repo not in ENABLED_REPO_ROOTS -- gate temporarily scoped to pm-bridge only",
                      "repo": repo_root, "command": command[:200]})
        allow()

    # pm-bridge pushes on a RECEIPT rather than on a `final` receipt.
    #
    # Operator rule, 2026-08-26, stated after seeing a narrower first attempt: "цель не убрать или
    # сократить ревью а просто дать пм бридж право пушать когда есть квитанция." So review is not
    # reduced anywhere -- the commit gate is untouched, every commit still requires a review
    # attempt, and reviews still run before a push. What is dropped for this ONE repo is the
    # requirement that the consensus loop have CONCLUDED, because pm-bridge is the transport that
    # produces those receipts: a defect in it otherwise blocks shipping its own fix, and the more
    # broken it is the more firmly it is locked. Gate A ended with a session offering the operator
    # a choice between a kill switch, waiting indefinitely, and marking `--final` by hand.
    #
    # Residual risk, accepted knowingly (GPT-PM raised it as a BLOCKER against an even looser
    # first version, and the operator then set this rule): a receipt records that a review was
    # ATTEMPTED, not that it approved. A failed attempt still writes one, so pm-bridge can push
    # behind a review that never produced a verdict. That is the same "attempt, not success"
    # philosophy the commit gate has always used, now applied to this repo's push as well -- and
    # it is why the receipt's CONTENT still has to be read before pushing, by a human or by the
    # session that ran it.
    if is_push and is_transport_repo(repo_root):
        entry = latest_receipt_for_repo(repo_root)
        fresh = False
        if entry and entry.get("timestamp"):
            try:
                age_h = (datetime.now(timezone.utc)
                         - datetime.fromisoformat(entry["timestamp"])).total_seconds() / 3600.0
                fresh = age_h <= PUSH_RECEIPT_MAX_AGE_HOURS
            except ValueError:
                fresh = False
        if fresh:
            log_decision({"decision": "allow-push-on-receipt",
                          "reason": "pm-bridge is the review transport; it pushes on a fresh "
                                    "receipt rather than a final one (see the note in main()). "
                                    "Commit gate unchanged; the receipt's content still has to "
                                    "be read.",
                          "repo": repo_root,
                          "receipt": {k: entry.get(k) for k in
                                      ("round", "ok", "review_status", "final", "verdict", "correlated")},
                          "command": command[:200]})
            allow()
        # No receipt at all, or a stale one: fall through and deny like any other repo. "Push on a
        # receipt" is not "push on nothing" -- a review still has to have been run.

    if is_push and any(is_under(repo_root, root) for root in EXCLUDED_REPO_ROOTS):
        # Each excluded repo is excluded for its OWN reason; a single blanket sentence here
        # attributed Fitness_App's transient-bug rationale to every future exclusion, which would
        # have misread pm-bridge's permanent circularity exclusion as a temporary workaround.
        why = ("pm-bridge is the review transport itself -- gating its push on its own receipt is "
               "circular" if is_transport_repo(repo_root)
               else "review.js correlation-detector bug")
        log_decision({"decision": "allow-push-excluded",
                      "reason": f"repo in EXCLUDED_REPO_ROOTS -- push final-receipt check skipped "
                                f"({why}, see comment above EXCLUDED_REPO_ROOTS); "
                                f"commit-time check still applies",
                      "repo": repo_root, "command": command[:200]})
        allow()

    if is_push:
        try:
            passes, why, entry = final_receipt_fresh(repo_root)
        except Exception as e:
            log_decision({"decision": "fail-open", "reason": f"receipt scan failed: {e}", "repo": repo_root})
            allow()

        if passes:
            receipt_ok = entry.get("ok") if entry else None
            receipt_error = entry.get("error") if entry else None
            if receipt_ok is False:
                warn_reason = (
                    f"GPT REVIEW gate (push): ALLOWING per fail-open policy, but the latest final "
                    f"receipt for {repo_root} has ok=false (error={receipt_error!r}) -- the last GPT "
                    f"round did NOT actually succeed; it was marked --final anyway. State this plainly "
                    f"in the GPT-review: line of the stop-and-report -- do not report it as a clean "
                    f"review."
                )
                log_decision({"decision": "allow-with-warning", "reason": warn_reason,
                              "repo": repo_root, "command": command[:200],
                              "receipt_ok": receipt_ok, "receipt_error": receipt_error})
                warn_allow(payload, warn_reason)
                sys.exit(0)

            log_decision({"decision": "allow", "reason": "final review.js receipt found (ok)",
                          "repo": repo_root, "command": command[:200]})
            allow()

        reason = (
            f"GPT REVIEW gate (push): {why} -- repo {repo_root}. Per ~/.claude/CLAUDE.md 'GPT "
            f"Consensus Review (via PM Bridge)': push waits for this gate's GPT consensus loop to "
            f"CONCLUDE. Continue the review loop, then invoke `node {REVIEW_CLI} "
            f"--cwd {repo_root} --uncommitted --project <name> --final` on its last round, "
            f"then retry this push. CLAUDE_GPT_REVIEW_GATE=off is a session/process-level kill switch, "
            f"NOT a per-command one: it must be set in the environment BEFORE this Claude Code session/"
            f"process starts. Setting it inline in this Bash/PowerShell command has no effect."
        )
        deny(reason, {"repo": repo_root, "command": command[:200], "why": why})

    try:
        entry = recent_receipt(repo_root)
    except Exception as e:
        log_decision({"decision": "fail-open", "reason": f"receipt scan failed: {e}", "repo": repo_root})
        allow()

    if entry:
        if entry.get("ok") is False:
            warn_reason = (
                f"GPT REVIEW gate (commit): ALLOWING per fail-open policy, but the matching "
                f"review.js receipt for {repo_root} has ok=false (error={entry.get('error')!r}) -- "
                f"the review was attempted, not completed. State this plainly in the GPT-review: "
                f"line of the stop-and-report -- do not report it as a clean review or as CONSENSUS "
                f"PASS."
            )
            log_decision({"decision": "allow-with-warning", "reason": warn_reason,
                          "repo": repo_root, "command": command[:200],
                          "receipt_ok": entry.get("ok"), "receipt_error": entry.get("error")})
            warn_allow(payload, warn_reason)
            sys.exit(0)

        log_decision({"decision": "allow", "reason": "recent review.js receipt found (ok)",
                      "repo": repo_root, "command": command[:200]})
        allow()

    reason = (
        f"GPT REVIEW gate (commit): no review.js receipt found for {repo_root} in the last "
        f"{RECEIPT_MAX_AGE_HOURS}h. Per ~/.claude/CLAUDE.md 'GPT Consensus Review (via PM Bridge)' "
        f"(MANDATORY, 2026-08-21, replaces the suspended Codex gate): send this gate's diff to GPT "
        f"before committing -- run `node {REVIEW_CLI} --cwd {repo_root} "
        f"--uncommitted --project <name>`, then retry this commit. A fail-open error (login expired, "
        f"ChatGPT markup changed, conversation not found) still writes a receipt and satisfies this "
        f"gate -- only a call that was never attempted blocks it. CLAUDE_GPT_REVIEW_GATE=off is a "
        f"session/process-level kill switch, NOT a per-command one: it must be set in the environment "
        f"BEFORE this Claude Code session/process starts. Setting it inline in this Bash/PowerShell "
        f"command has no effect."
    )
    deny(reason, {"repo": repo_root, "command": command[:200]})


if __name__ == "__main__":
    main()
