#!/usr/bin/env python3
"""Shared logic for the Rosetta hooks: read PM Bridge's plan state, classify a tool call, spool it.

Rosetta is the workflow protocol -- Plan -> GO -> Act -> Validate -> Document. PM Bridge owns the
record (`src/rosetta.js` in the PM Bridge checkout); these hooks own enforcement, because an MCP tool
cannot deny an Edit and a PreToolUse hook can. This module is the part both hooks share.

Three decisions here are load-bearing, all of them from GPT-PM's design review (2026-08-26):

1. **Fail-open, but never silently.** Every failure path in this module returns "allow" -- an
   unreadable state directory, an absent pm-bridge checkout, a parse error, anything. A governance
   layer that can brick every editor on the machine when its own state file is malformed is worse
   than no governance layer. But fail-open must create DEBT, not erase it: the event is still
   spooled, marked `governed: false` with the reason, so the Stop hook and the eventual closure
   can see that work happened outside the protocol. R0 stops there; the debt ledger that makes
   commit/push fail closed until those mutations are reconciled is a later stage.

2. **The shell classifier is an ALLOWLIST GRAMMAR, not a blacklist.** `Edit`/`Write`/`NotebookEdit`
   announce their own semantics; an arbitrary shell command does not. `python -c`, a script, a
   redirect, `sed -i`, a package manager, command substitution, a nested shell -- all mutate while
   looking nothing like a write. Maintaining a list of mutating commands is unwinnable, so this
   goes the other way: a command is read-only only if EVERY segment of it has a head this module
   recognises as read-only and the whole command is free of redirection, substitution and
   heredocs. Anything unrecognised counts as a mutation. False "mutation" costs one spool record;
   false "read-only" is a hole straight through the protocol.

3. **An intent record is provenance, not truth.** What this module writes at PreToolUse is what
   was about to be attempted. It cannot know the command failed halfway or touched a path nothing
   in its input mentioned, which is why there is a second phase at PostToolUse and why closure
   reconstructs the real changed-set from git rather than from these records.

Switch: Rosetta is OFF unless `rosetta_mode.json` (in _agentio.state_dir("rosetta"); override CLAUDE_ROSETTA_MODE_FILE) says {"enabled": true}; the
`/rosetta on|off` command writes it via `rosetta_mode.py`, and every hook call re-reads it, so a
toggle applies at once. CLAUDE_ROSETTA_GATE=off|on (process-level) overrides the file.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# --- Locating PM Bridge -------------------------------------------------------------------
# Overridable so a test never touches the live state this machine's concurrent sessions share.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _agentio as agentio  # noqa: E402

# Set PM_BRIDGE_STATE_DIR to the PM Bridge checkout's state folder; without it Rosetta finds no plan (fail-open).
DEFAULT_STATE_DIR = agentio.state_dir("pm_bridge_state")

# "apply_patch" is the Codex edit tool (patch text in tool_input.command).
ALWAYS_MUTATING_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit", "apply_patch"}
SHELL_TOOLS = {"Bash", "PowerShell"}


def state_dir() -> Path:
    override = os.environ.get("PM_BRIDGE_STATE_DIR")
    return Path(override) if override else DEFAULT_STATE_DIR


def rosetta_dir() -> Path:
    return state_dir() / "rosetta"


MODE_FILE = agentio.state_dir("rosetta") / "rosetta_mode.json"


def mode_file() -> Path:
    override = os.environ.get("CLAUDE_ROSETTA_MODE_FILE")
    return Path(override) if override else MODE_FILE


def mode_enabled() -> bool:
    """The `/rosetta on|off` switch. Absent or unreadable file = off (operator 2026-10-09)."""
    try:
        data = json.loads(mode_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("enabled") is True


def gate_off() -> bool:
    env = os.environ.get("CLAUDE_ROSETTA_GATE", "").lower()
    if env == "off":
        return True
    if env == "on":
        return False
    return not mode_enabled()


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _safe_id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", str(value))


def normalise_repo(repo) -> str | None:
    """Match `rosetta.js`'s own key normalisation, so a plan written by Node is found by Python.

    Lexical (`os.path.abspath`), NOT `Path.resolve()`. Node's `path.resolve` does not follow
    symlinks, and this used to, so a checkout reached through a junction or symlink produced a
    Node `repo_key` (the alias) that could never match Python's answer (the target) -- a valid
    plan would read as `plan_scoped_to_other_repo` and its work would be recorded as ungoverned.
    Found in review. Lexical also costs no filesystem call, which matters on a function called
    once per tool invocation in every session on this machine.
    """
    if not repo:
        return None
    return os.path.abspath(str(repo)).rstrip("\\/").lower()


_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def git_root(cwd) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


# --- Governance scope classification (P0 impossible-debt guard) --------------------------
#
# GPT-PM design review, 2026-08-30 (decision PMB-D-ROSETTA-NONVCS-01, roadmap item P0): the Stop
# hook was recording actionable debt for mutations `pm_rosetta_plan` structurally cannot create a
# plan for -- a write to D:\Repo\reports (outside every git repository) is real work, but the
# hook's own prescribed remediation ("run pm_rosetta_plan") fails with "Cannot prove the physical
# Git repository identity", every time, for every session. A hook must not issue an instruction
# that cannot be carried out. This is the P0 fix; first-class non-VCS/workspace plans (roadmap P1)
# are a separate, larger change to `rosetta.js` -- not made here.
#
# Three classes, not two. GPT-PM's REJECTED an earlier "target vs cwd, pick one" draft of this:
# a Bash/PowerShell command run from a non-git cwd can still mutate a file INSIDE a child git
# repo via a path argument ("D:\Repo`n cd D:\Repo && sed -i ... ERP\foo.py`n" is real and governable
# by ERP), so folding every non-git-cwd shell mutation into the same bucket as a genuinely
# unreachable target would silently drop real debt. Hence three classes:
#
#   REPO                       -- a real git root was resolved; ordinary plan-scoped governance
#                                  applies exactly as before this change.
#   UNGOVERNABLE_PATH          -- Edit/Write/NotebookEdit only, target's own directory has no git
#                                  root. The file being written IS the mutation; there is no
#                                  argument-parsing ambiguity the way there is for a shell command.
#                                  A warning, never actionable Rosetta debt.
#   GOVERNANCE_SCOPE_AMBIGUOUS -- Bash/PowerShell only, cwd has no git root. The command MAY still
#                                  touch a path inside a real repo (the ERP example above); this
#                                  hook does not attempt to parse path arguments out of an
#                                  arbitrary shell command (that is the same unreliability the
#                                  shell classifier's own module docstring already rejects for
#                                  read/write classification). Audit-visible, but not actionable
#                                  Rosetta debt either -- "cannot determine" is not "safe to
#                                  dismiss", and it is not "identical to a real repo's debt" either.
#
# R0 is audit mode: none of these three ever deny a tool call. The distinction only changes what
# the Stop hook is allowed to ask a session to do about it (see rosetta_due.py).
SCOPE_REPO = "repo"
SCOPE_UNGOVERNABLE_PATH = "ungovernable_path"
SCOPE_GOVERNANCE_AMBIGUOUS = "governance_scope_ambiguous"


def resolve_governance_scope(tool_name: str, target: str, cwd) -> tuple[str, str | None]:
    """(scope_class, scope_repo). scope_repo is the git root to authorize against, or None.

    Called only for confirmed mutations (classify_call already filtered reads out before this
    would run), so the extra `git rev-parse` subprocess this adds is bounded by mutation
    frequency, not total tool-call frequency -- the same tradeoff `repo_within_plan`'s docstring
    already accepts for a much hotter path.
    """
    if tool_name in ALWAYS_MUTATING_TOOLS:
        # target IS the actual mutated path for these tools -- reliable, unlike a shell command's
        # text. Resolve against its own directory, not cwd: a Write to an absolute path elsewhere
        # on disk is not scoped by whatever directory the tool call happened to run from.
        if not target:
            return SCOPE_UNGOVERNABLE_PATH, None
        directory = os.path.dirname(os.path.abspath(str(target)))
        root = git_root(directory)
        return (SCOPE_REPO, root) if root else (SCOPE_UNGOVERNABLE_PATH, None)
    if tool_name in SHELL_TOOLS:
        if not cwd:
            return SCOPE_GOVERNANCE_AMBIGUOUS, None
        root = git_root(cwd)
        return (SCOPE_REPO, root) if root else (SCOPE_GOVERNANCE_AMBIGUOUS, None)
    return SCOPE_REPO, None


# --- Plan state ---------------------------------------------------------------------------

def active_plan(session_id: str | None, repo: str | None) -> tuple[dict | None, str]:
    """The plan authorizing mutation for this session, plus a machine-readable reason.

    The authorization test is the stored pair `approval.approved_plan_hash == plan_hash`, not a
    hash recomputed here. `rosetta.js` recomputes it from the plan's canonical form on every read
    and is the authority; duplicating that canonicalisation in a second language would put the
    protocol's core check at the mercy of two JSON encoders agreeing byte-for-byte about escapes.
    What the pair does guarantee is the property that actually matters in practice: a plan whose
    content moved after review does not get a new approval for free, because there is no code path
    that edits a plan in place -- a changed plan is a NEW plan, `pending`, with no approval at all.
    A hand-forged plan file defeats this check, and is outside the threat model: Rosetta governs a
    cooperating agent, not an attacker with write access to the state directory.
    """
    if not session_id:
        return None, "no_session"
    pointer = _read_json(rosetta_dir() / "sessions" / f"{_safe_id(session_id)}.json")
    if not pointer or not pointer.get("plan_id"):
        return None, "no_plan"
    plan = _read_json(rosetta_dir() / "plans" / f"{_safe_id(pointer['plan_id'])}.json")
    if not plan:
        return None, "no_plan"
    if plan.get("status") != "in-progress":
        return plan, f"plan_status_{plan.get('status')}"
    approval = plan.get("approval")
    if not approval:
        return plan, "plan_not_approved"
    if approval.get("approved_plan_hash") != plan.get("plan_hash"):
        return plan, "plan_hash_mismatch"
    if not repo_within_plan(repo, plan.get("repo_key")):
        return plan, "plan_scoped_to_other_repo"
    return plan, "ok"


def repo_within_plan(repo, repo_key) -> bool:
    """Is `repo` the plan's repository, or a directory inside it?

    A prefix test rather than equality, deliberately: the hook is handed the tool call's `cwd`,
    which is routinely a subdirectory of the checkout the plan was written against. Resolving it
    to a git root instead would mean a `git rev-parse` subprocess on EVERY tool call in every
    session on this machine -- tens of milliseconds of tax on a hook that must be invisible.
    """
    wanted = normalise_repo(repo)
    if not wanted or not repo_key:
        return True
    return wanted == repo_key or wanted.startswith(repo_key.rstrip("\\/") + os.sep)


# --- Act spool ----------------------------------------------------------------------------

def _now_iso() -> str:
    """UTC ISO 8601, the same shape `rosetta.js` writes.

    Added after reading the first production spool: every hook-written record came out with no
    timestamp at all, because the JS writer stamps `ts` and this one did not -- and the Node test
    that asserted `typeof a.ts === "string"` was exercising the JS writer, so it stayed green while
    the records that actually matter were undated. An audit trail with no times is not one.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def spool_dir(session_id: str | None) -> Path:
    return rosetta_dir() / "spool" / _safe_id(session_id or "unknown-session")


def write_act_event(session_id: str | None, event: dict) -> bool:
    """Publish one act event as its own file, atomically. Never appends to a shared file.

    Act events come from many concurrent hook processes across the sessions this machine runs at
    once. On Windows an append from several processes is not the atomic operation POSIX O_APPEND
    would give, so a shared JSONL would eventually interleave two half-lines and lose both. One
    file per event has exactly one writer and cannot interleave. This is a compatibility spool:
    Gate C ingests it into the SQLite WAL ledger (ROADMAP.md:26-28) and deletes it.
    """
    directory = spool_dir(session_id)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{uuid.uuid4().hex}.json"
        tmp = directory / f".{target.name}.tmp"
        stamped = {"v": 1, "ts": _now_iso(), **event}
        tmp.write_text(json.dumps(stamped, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)
        return True
    except OSError as err:
        # The contract is "fail open, but never silently". If the record itself cannot be written
        # there is nowhere left to record the loss -- so say it on stderr, which Claude Code
        # surfaces, rather than returning a bool every caller was ignoring anyway. Still returns,
        # still exits 0: a governance recorder must not break the session it is recording.
        print(f"[rosetta] could not write act event for session {session_id}: {err}", file=sys.stderr)
        return False


def read_acts(session_id: str | None) -> tuple[list[dict], bool, str]:
    """(events, ok, reason). `ok` is False when the answer is UNKNOWN rather than empty.

    Same class of defect as `changedSet`'s in rosetta.js, fixed the same way. A spool directory
    that does not exist means the session genuinely never mutated anything; a directory that
    cannot be read, or a record that will not parse, means the answer is unknown. Returning an
    empty list for both made a lost record indistinguishable from a clean session -- and an empty
    list is exactly what lets a Stop pass without a word.
    """
    directory = spool_dir(session_id)
    try:
        names = sorted(p for p in directory.iterdir() if p.suffix == ".json")
    except FileNotFoundError:
        return [], True, "no_spool"
    except OSError as err:
        return [], False, f"unreadable_spool:{err.strerror or err}"
    out = []
    unreadable = 0
    for path in names:
        record = _read_json(path)
        if record:
            out.append(record)
        else:
            unreadable += 1
    if unreadable:
        return out, False, f"unreadable_records:{unreadable}"
    return out, True, "ok"


# --- Legacy impossible-debt migration (GPT-PM MAJOR-2, PMB-D-ROSETTA-NONVCS-01) -----------
#
# Acts recorded BEFORE this fix (no `governance_scope` field at all) that were marked
# `governed: False` may be exactly the class this whole change exists to stop nagging about --
# work whose prescribed reconciliation (`pm_rosetta_plan`) was structurally impossible at the time
# it was recorded. GPT-PM's explicit instruction: do not leave them as ordinary open debt (the
# Stop hook would keep asking for an impossible plan forever), do not silently relabel them
# `governed: true` (no GO ever existed for them -- that would falsify pre-approval semantics), and
# do not delete the record. Reclassify into a terminal, forensic-only state instead.
LEGACY_STATE = "LEGACY_UNRECONCILABLE_PROTOCOL_DEFECT"


def reclassified_keys(acts: list[dict]) -> set[str]:
    """intent_keys already migrated to LEGACY_STATE, from prior `reclassified` events.

    Idempotency source of truth: migrating the same acts twice must be a no-op, not a duplicate
    record, so every migration pass reads this first.
    """
    return {
        a.get("intent_key") for a in acts
        if a.get("phase") == "reclassified" and a.get("new_state") == LEGACY_STATE
        and a.get("intent_key")
    }


def legacy_ungoverned_acts(acts: list[dict]) -> list[dict]:
    """Ungoverned intents recorded before this fix existed -- no `governance_scope` field at all.

    Distinguishes "recorded under the old code, which had no way to know the difference" from
    "recorded under the new code and genuinely could not resolve a scope" (UNGOVERNABLE_PATH /
    GOVERNANCE_SCOPE_AMBIGUOUS) -- the latter are not legacy debt, they are the new classification
    already working as intended and need no migration at all.
    """
    already = reclassified_keys(acts)
    return [
        a for a in acts
        if a.get("phase") == "intent" and a.get("governed") is False
        and "governance_scope" not in a
        and a.get("intent_key") not in already
    ]


def migrate_legacy_debt(session_id: str) -> list[str]:
    """Reclassify this session's pre-fix ungoverned acts into LEGACY_STATE. Returns migrated keys.

    Idempotent: a key already reclassified (from a prior call, possibly a prior process) is
    skipped, never double-recorded -- `already_fired`-style markers are not needed here because
    `reclassified_keys` itself is the durable idempotency check, read fresh from the spool.
    """
    acts, _, _ = read_acts(session_id)
    to_migrate = legacy_ungoverned_acts(acts)
    migrated = []
    for act in to_migrate:
        key = act.get("intent_key")
        write_act_event(session_id, {
            "phase": "reclassified",
            "intent_key": key,
            "session_id": session_id,
            "new_state": LEGACY_STATE,
            "reason": (
                "Rosetta stop hook created debt for a scope for which pm_rosetta_plan "
                "structurally refused identity; prescribed reconciliation was impossible at "
                "creation time. Retrospectively accounted for; not pre-governed."
            ),
            "original_tool": act.get("tool"),
            "original_target": act.get("target"),
        })
        migrated.append(key)
    return migrated


# --- Shell classification -----------------------------------------------------------------
#
# Read-only heads only. Anything absent from this set makes the whole command a mutation.
# Deliberately excluded even though they are often used read-only: python/py/node/pwsh (a `-c`
# one-liner writes files as easily as it prints them), curl/wget (`-o`), tar/zip, xargs (runs
# whatever it is handed), ssh, docker, npm/pip (`install` mutates the machine).

READ_ONLY_HEADS = {
    # `cd` changes the shell's own working directory and nothing else. It is here because the
    # first production spool showed nearly every Bash call in this workspace opens with
    # `cd "D:/Repo/<project>" && <read-only command>`, and without `cd` allowlisted every one of
    # those was recorded as a mutation. Not a hole -- a second segment that mutates still fails
    # on its own merits -- but an audit trail in which almost everything is flagged carries no
    # signal at all, which is its own kind of failure.
    "cd",
    "awk", "basename", "cat", "cksum", "column", "comm", "cut", "date", "df", "diff", "dirname",
    "du", "echo", "egrep", "false", "fgrep", "file", "grep", "head", "hostname", "id", "jq",
    "less", "ls", "md5sum", "more", "nl", "od", "printenv", "printf", "pwd", "readlink", "realpath",
    "rev", "rg", "sha1sum", "sha256sum", "sleep", "sort", "stat", "tail", "tr", "tree", "true",
    "type", "uname", "uniq", "wc", "whoami", "which", "xxd", "env",
}

# git is read-only only for these subcommands. `checkout`, `apply`, `restore`, `stash`, `clean`,
# `reset`, `commit`, `push` and friends are absent on purpose.
GIT_READ_ONLY_SUBCOMMANDS = {
    "blame", "branch", "cat-file", "check-ignore", "config", "count-objects", "describe", "diff",
    "for-each-ref", "grep", "log", "ls-files", "ls-remote", "ls-tree", "merge-base", "name-rev",
    "remote", "rev-list", "rev-parse", "shortlog", "show", "show-ref", "status", "symbolic-ref",
    "tag", "whatchanged", "worktree",
}

# Command-level constructs that make any command a mutation regardless of its head.
_REDIRECT_RE = re.compile(r"(?<![0-9<>])>{1,2}(?![>])|(?<!\d)\d?>&|<<")
_SUBSTITUTION_RE = re.compile(r"\$\(|`")
_SEGMENT_SPLIT_RE = re.compile(r"\|\||&&|[;|\n&]")


# git's global options come BEFORE the subcommand, and these take a value. Without skipping the
# value, `git -C D:\Repo\pm-bridge status` reads as subcommand "D:\Repo\pm-bridge" -- unrecognised,
# therefore a mutation. That is the single most common shape of a read-only git call in this
# workspace, so getting it wrong would have marked most inspection as ungoverned work.
# `-c` and `--config-env` are handled earlier, as mutations, so they never reach this skip list --
# they are kept in it only so that a git command carrying one is still parsed coherently by any
# future caller of this helper rather than mis-reading the config value as the subcommand.
GIT_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}


def _git_subcommand(args: list[str]) -> tuple[str | None, list[str]]:
    index = 0
    while index < len(args):
        token = args[index]
        if token in GIT_GLOBAL_WITH_VALUE:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        break
    if index >= len(args):
        return None, []
    return args[index], args[index + 1:]


def _segment_is_read_only(segment: str) -> bool:
    segment = segment.strip()
    if not segment:
        return True
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        return False
    # Drop leading `VAR=value` assignments and `sudo`-style prefixes: an assignment prefix does
    # not change what the command does, but `sudo` deliberately does not appear in the allowlist.
    while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return True
    head = Path(tokens[0]).name.lower()
    if head.endswith(".exe"):
        head = head[:-4]
    args = tokens[1:]

    if head == "git":
        # Two escapes that make the SUBCOMMAND irrelevant, both found in review:
        #
        # `-c` / `--config-env` inject configuration for the duration of one call, and several
        # config keys are executable -- `core.pager`, `core.editor`, `alias.*`, `diff.external`.
        # `git -c core.pager=<command> log` runs that command. The subcommand is read-only and the
        # invocation is not.
        #
        # `--output=<path>` / `-o <path>` is accepted by log, diff, show and blame (it is a
        # git-diff-options flag), and writes their output to an arbitrary file. Reading with a
        # redirect built into the reader is still a write.
        if any(a in ("-c", "--config-env") or a.startswith("--config-env=") for a in args):
            return False
        if any(a == "-o" or a == "--output" or a.startswith("--output=") or
               (a.startswith("-o") and len(a) > 2) for a in args):
            return False
        subcommand, sub_args = _git_subcommand(args)
        if subcommand not in GIT_READ_ONLY_SUBCOMMANDS:
            return False
        # `git config --get x` reads; `git config x y` writes. Only the explicit read forms pass.
        if subcommand == "config" and not any(
            a in ("--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l")
            for a in sub_args
        ):
            return False
        # `git worktree list` reads; `git worktree add` creates a checkout (and possibly a branch).
        if subcommand == "worktree" and (not sub_args or sub_args[0] != "list"):
            return False
        # `git remote -v` reads; `git remote add|remove|set-url` mutates.
        if subcommand == "remote" and any(not a.startswith("-") for a in sub_args):
            return False
        # `git tag`/`git branch` with a name argument creates a ref; bare or listing forms read.
        if subcommand in ("tag", "branch"):
            if any(not a.startswith("-") for a in sub_args):
                return False
            if any(a in ("-d", "-D", "-m", "-M", "--delete", "--move") for a in sub_args):
                return False
        return True

    if head not in READ_ONLY_HEADS:
        return False

    # A handful of allowlisted heads have a mutating mode reached by a flag.
    if head == "sed" and not any(a == "-n" or a.startswith("-n") for a in args):
        return False
    if head == "sed" and any(a.startswith("-i") for a in args):
        return False
    if head == "awk" and any(a.startswith("-i") for a in args):
        return False
    if head == "find" and any(
        a in ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint") for a in args
    ):
        return False
    return True


# `sed -n` is the read-only form and is common enough to be worth allowing; it is checked in
# `_segment_is_read_only` rather than being in the bare allowlist, so a plain `sed` never passes.
READ_ONLY_HEADS.add("sed")
READ_ONLY_HEADS.add("find")


# Deliberately minimal control-flow support: a single, non-nested `for VAR in LIST; do BODY;
# done` is recognised and its BODY is classified the same way as any other command list -- every
# segment must pass `_segment_is_read_only` or the whole loop is a mutation, exactly as it already
# works for `&&`/`;`-chained commands. Nothing else (while, if, case, nested loops, subshells) is
# parsed; those fall through to the existing "unrecognised = mutation" rule -- this regex cannot
# correctly bracket-match a nested `do`/`done`, so a nested construct must never be silently
# mis-parsed as if it were flat, only conservatively refused. `$(...)`/backtick substitution and
# redirection anywhere in the command (list, body, or otherwise) are already rejected by
# `classify_shell`'s own checks before this ever runs, so a for-loop cannot smuggle either past
# them by hiding inside the loop body. Found necessary live (pm-bridge session, 2026-09-17): a
# pure-diagnostic `for f in ...; do echo "$f:"; file "$f"; done` was flagged as a mutation purely
# because the classifier had no grammar for `for` at all -- not because anything inside it was
# actually unsafe.
_FOR_LOOP_RE = re.compile(
    r"^for\s+[A-Za-z_][A-Za-z0-9_]*\s+in\s+(?P<list>[^;]*);\s*do\s+(?P<body>.*);\s*done\s*$",
    re.DOTALL,
)
_NESTED_CONTROL_RE = re.compile(r"(?<![\w-])(?:for|while|until|case|do|done|then|fi|esac)(?![\w-])")


def _for_loop_read_only(command: str) -> bool | None:
    """True/False if `command` is a single non-nested for-loop and every body segment classifies;
    None if it does not match this shape at all (caller falls through to the normal path)."""
    m = _FOR_LOOP_RE.match(command.strip())
    if not m:
        return None
    body = m.group("body")
    if _NESTED_CONTROL_RE.search(body) or _NESTED_CONTROL_RE.search(m.group("list")):
        return None
    for segment in _SEGMENT_SPLIT_RE.split(body):
        if not _segment_is_read_only(segment):
            return False
    return True


def classify_shell(command: str) -> tuple[bool, str]:
    """(is_read_only, reason). Unknown shapes are mutations -- see the module header."""
    if not command or not command.strip():
        return True, "empty"
    if _SUBSTITUTION_RE.search(command):
        return False, "command_substitution"
    if _REDIRECT_RE.search(command):
        return False, "redirection_or_heredoc"

    for_loop = _for_loop_read_only(command)
    if for_loop is True:
        return True, "read_only_for_loop"
    if for_loop is False:
        return False, "non_readonly_for_loop_body"

    for segment in _SEGMENT_SPLIT_RE.split(command):
        if not _segment_is_read_only(segment):
            return False, f"non_readonly_segment:{segment.strip()[:60]}"
    return True, "read_only_allowlist"


def classify_call(tool_name: str, tool_input: dict) -> tuple[bool, str, str]:
    """(is_mutation, reason, target) for one tool call."""
    if tool_name in ALWAYS_MUTATING_TOOLS:
        target = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if not target and tool_name == "apply_patch":
            paths = agentio.edited_paths({"tool_name": "apply_patch", "tool_input": tool_input})
            target = paths[0] if paths else ""
        return True, "file_write_tool", str(target)
    if tool_name in SHELL_TOOLS or tool_name in agentio.SHELL_TOOLS:
        command = str(tool_input.get("command") or tool_input.get("cmd") or "")
        read_only, reason = classify_shell(command)
        return (not read_only), reason, command[:500]
    return False, "unmatched_tool", ""
