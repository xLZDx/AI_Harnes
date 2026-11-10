#!/usr/bin/env python3
"""
Low-level parsing helpers for Claude Code session JSONL.

This module knows the Claude Code log *format* (the shape of each JSONL line).
It is the only place that should change if Claude Code's on-disk schema changes.
The collector (collector_claude_code.py) turns these primitives into the
IDE-agnostic IR; generators never import this module.
"""

import json
import os
import re
from typing import Optional

# ---------- Tool-call one-line summaries (no emoji; renderer adds icons) ----------

_TOOL_SUMMARY = {
    "Read":      lambda i: f"Read {i.get('file_path','?')}",
    "Write":     lambda i: f"Write {i.get('file_path','?')}",
    "Edit":      lambda i: f"Edit {i.get('file_path','?')}",
    "MultiEdit": lambda i: f"Edit {i.get('file_path','?')}",
    "Bash":      lambda i: (
        f"Bash {(i.get('command','') or '').splitlines()[0][:140]}"
        + (f"  · {i['description']}" if i.get('description') else '')
    ),
    "Grep":      lambda i: f"Grep {i.get('pattern','?')}",
    "Glob":      lambda i: f"Glob {i.get('pattern','?')}",
    "Skill":     lambda i: f"Skill {i.get('skill','?')}"
                            + (f"  · {str(i['args'])[:80]}" if i.get('args') else ''),
    "Task":      lambda i: f"Agent {i.get('subagent_type','?')} — {i.get('description','')}",
    "Agent":     lambda i: f"Agent {i.get('subagent_type','?')} — {i.get('description','')}",
    "WebFetch":  lambda i: f"WebFetch {i.get('url','?')}",
    "WebSearch": lambda i: f"WebSearch {i.get('query','?')}",
    "AskUserQuestion": lambda i: "Asked you a question",
}


def tool_summary(name: str, inp: dict) -> str:
    inp = inp or {}
    if name in _TOOL_SUMMARY:
        try:
            return _TOOL_SUMMARY[name](inp)
        except Exception:
            pass
    # Generic fallback for any tool not in the table above (chiefly MCP tools,
    # whose parameter names we cannot enumerate in advance) — surface the
    # tool's own first few arguments instead of a fixed key allowlist, so an
    # unrecognized call still shows real arguments rather than a bare name.
    pairs = []
    for k, v in inp.items():
        if v is None or v == "":
            continue
        pairs.append(f"{k}={str(v)[:60]}")
        if len(pairs) >= 4:
            break
    return name + (f": {', '.join(pairs)}" if pairs else "")


# ---------- User-entry classifier ----------

STRIP_PATTERNS = [
    re.compile(r"<system-reminder>.*?</system-reminder>", re.S),
    re.compile(r"<local-command-caveat>.*?</local-command-caveat>", re.S),
    re.compile(r"<local-command-stdout>.*?</local-command-stdout>", re.S),
    re.compile(r"<command-message>.*?</command-message>", re.S),
]


def strip_wrappers(s: str) -> str:
    for pat in STRIP_PATTERNS:
        s = pat.sub("", s)
    return s.strip()


def user_text(entry: dict) -> str:
    """The plain text of a user entry, whether its content is a bare string or a
    content-block list. One place, because both the classifier and the
    workflow-boundary detection read the same text."""
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return content if isinstance(content, str) else ""


def classify_user_entry(entry: dict):
    """Return (kind, payload). Kinds: TOOL_RESULT, SKILL_BODY, SYSTEM_REMINDER,
    LOCAL_CMD_META, LOCAL_CMD_STDOUT, INTERRUPT, SLASH_COMMAND,
    CONTINUATION_SUMMARY, REAL_PROMPT, EMPTY."""
    msg = entry.get("message", {})
    content = msg.get("content")
    text = user_text(entry)

    if isinstance(content, list):
        only_tool_results = (
            len(content) > 0 and
            all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
        )
    else:
        only_tool_results = False

    if only_tool_results:
        return ("TOOL_RESULT", None)

    raw = text
    if raw.startswith("Base directory for this skill"):
        return ("SKILL_BODY", None)
    if raw.startswith("<local-command-stdout>") and "<command-name>" not in raw:
        return ("LOCAL_CMD_STDOUT", None)
    if raw.startswith("<local-command-caveat>") and "<command-name>" not in raw:
        return ("LOCAL_CMD_META", None)
    if re.fullmatch(r"\s*<system-reminder>.*?</system-reminder>\s*", raw, re.S):
        return ("SYSTEM_REMINDER", None)
    if raw.strip() in (
        "[Request interrupted by user]",
        "[Request interrupted by user for tool use]",
    ):
        return ("INTERRUPT", None)

    m = re.search(r"<command-name>(/[^<]+)</command-name>", raw)
    if m:
        cmd = m.group(1).strip()
        args_m = re.search(r"<command-args>([^<]*)</command-args>", raw)
        args = args_m.group(1).strip() if args_m else ""
        prose = strip_wrappers(re.sub(r"<command-[a-z]+>.*?</command-[a-z]+>", "", raw, flags=re.S))
        prose = re.sub(r"<command-args>[^<]*</command-args>", "", prose).strip()
        full = f"{cmd} {args}".strip()
        if prose:
            return ("SLASH_COMMAND", f"{full}\n\n{prose}")
        return ("SLASH_COMMAND", full)

    cleaned = strip_wrappers(raw)
    if cleaned.startswith("This session is being continued from a previous conversation"):
        return ("CONTINUATION_SUMMARY", cleaned)
    if cleaned:
        return ("REAL_PROMPT", cleaned)
    return ("EMPTY", None)


# ---------- Workflow-invocation markers (format knowledge) ----------
#
# Two marker families announce a skill/workflow invocation in a Claude Code log,
# and mining every transcript under ~/.claude/projects/ (207 files) shows they
# are disjoint and complete: 129 slash-command invocations + 67 Skill-tool calls
# accounted for exactly all 196 skill activations, with no activation announced
# twice. The Skill-tool marker is read straight off the assistant block
# (name == "Skill", input.skill); the two helpers below cover the other family.
#
# The `<command-name>` shape also carries CLI built-ins that are NOT workflows
# (/model, /exit, /plugin, /compact). Rather than maintain a denylist that will
# drift as the CLI grows, the caller confirms the invocation against the log's
# own proof of activation: Claude Code writes a "Base directory for this skill"
# entry when — and only when — a real skill body loads. In the same 207-file
# sample that follow-up sits exactly ONE entry after the command (129/129), and
# no CLI built-in has one at all.

SKILL_BODY_PREFIX = "Base directory for this skill:"

_COMMAND_NAME_RE = re.compile(r"<command-name>([^<]*)</command-name>")


def slash_command_name(entry: dict):
    """The command a user entry invoked (`/adk:adk-implement-pbi` ->
    `adk:adk-implement-pbi`), or None. Says nothing about whether the command is
    a skill or a CLI built-in — confirm with skill_body_name()."""
    m = _COMMAND_NAME_RE.search(user_text(entry))
    if not m:
        return None
    name = m.group(1).strip().lstrip("/").strip()
    return name or None


# A finished background task reports back by INJECTING its result into the
# conversation as a user-role entry. It is machine-written text, and because
# those results are reports about the codebase they are dense with skill names:
# measured over 40 real transcripts, 234 of the 688 entries the classifier calls
# REAL_PROMPT are these, and they accounted for 100 of 106 name matches in one
# session before this exclusion existed.
#
# (generator_html.py matches the same opening tag for its own rendering. The
# duplication is deliberate: the generators must stay IDE-agnostic and so may
# not import this module — see the note at the top of this file.)
TASK_NOTIFICATION_TAG = "<task-notification>"


def is_task_notification(text: str) -> bool:
    return (text or "").lstrip().startswith(TASK_NOTIFICATION_TAG)


def authored_text(entry: dict) -> str:
    """The text a HUMAN actually typed in this user entry — "" for everything
    else. This is the ONLY text a mention marker may be detected in.

    The `user` role in a Claude Code transcript is a mailbox, not a person. It
    carries tool results, `<system-reminder>` blocks (which enumerate skill
    names constantly — the available-skills listing, memory files, hook output),
    local-command caveats and stdout, injected skill bodies, finished background
    tasks reporting their results, and the machine-written continuation summary
    of a resumed session. Every one of those would fire a name match, and none
    of them is a person asking for a workflow. So the classifier decides, and
    only its two human kinds count:

      REAL_PROMPT     the user's own message, already stripped of the wrapper
                      blocks by strip_wrappers() — unless it is a background
                      task's injected result (see is_task_notification)
      SLASH_COMMAND   only the PROSE the user typed after the command; the
                      command itself is the activation marker's business, not
                      the mention detector's

    CONTINUATION_SUMMARY is deliberately excluded: Claude writes it, not the
    user, and it summarizes work that already happened.

    One residual, stated rather than hidden: an injected agent/skill PROMPT
    ("You are a senior security engineer…") is indistinguishable from prose by
    shape alone — 1 occurrence in the same 40-transcript sample. The collector
    suppresses it positionally instead, because such a body always arrives
    immediately behind the activation that asked for it."""
    kind, payload = classify_user_entry(entry)
    if kind == "REAL_PROMPT":
        return "" if is_task_notification(payload) else (payload or "")
    if kind == "SLASH_COMMAND":
        # payload is "<command> <args>" plus, after a blank line, the user's own
        # prose (see classify_user_entry). Only that prose is authored text.
        parts = (payload or "").split("\n\n", 1)
        return parts[1] if len(parts) == 2 else ""
    return ""


def skill_body_name(entry: dict):
    """The skill whose body this user entry carries, taken from the directory
    name in "Base directory for this skill: <...>/skills/<name>", or None."""
    text = user_text(entry)
    if not text.startswith(SKILL_BODY_PREFIX):
        return None
    path = text[len(SKILL_BODY_PREFIX):].splitlines()[0].strip()
    if not path:
        return None
    return os.path.basename(path.replace("\\", "/").rstrip("/")) or None


# ---------- Assistant block extraction (structured) ----------

def extract_blocks(content):
    """Return ordered blocks from an assistant message content list:
    {'t':'text','text':str} or {'t':'tool','name':str,'input':dict,'id':str}.
    'thinking' blocks are skipped."""
    out = []
    if not isinstance(content, list):
        return out
    for b in content:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text":
            t = b.get("text", "")
            if t.strip():
                out.append({"t": "text", "text": t})
        elif bt == "tool_use":
            out.append({"t": "tool", "name": b.get("name", ""),
                        "input": b.get("input") or {}, "id": b.get("id")})
    return out


def result_text(block) -> str:
    """Extract plain text from a tool_result content block. Falls back to a
    JSON dump when the content list has no `type: text` block (e.g. an
    image reference), so a structured result still renders instead of
    silently coming back empty."""
    tr = block.get("content")
    if isinstance(tr, str):
        return tr
    if isinstance(tr, list):
        texts = [x.get("text", "") for x in tr
                 if isinstance(x, dict) and x.get("type") == "text"]
        if texts:
            return "\n".join(texts)
        if tr:
            return json.dumps(tr, default=str)
        return ""
    return ""


# ---------- IO + naming ----------

def load_entries(jsonl_path: str):
    """The transcript's records in file order, with REPLAYED records dropped.

    Returns (entries, custom_title, ai_title). Every consumer of a transcript
    goes through here, so the entry list it hands back is the one definition of
    "what this session did" — turn building, active-time deltas, completion
    signals and both halves of the token maths all read it.

    A session that moves between a git worktree and its main checkout has its
    already-written records RE-APPENDED with a rewritten `cwd` — same `uuid`,
    same `message.id`, same `timestamp`, same `tool_use` ids. That is a
    host-produced shape, not corruption, and it recurs for anyone who leaves a
    worktree mid-session. Counting those records twice inflates the session's
    own spend, and a replayed record carrying an `Agent` call re-injects that
    delegate's whole subtree a second time.

    Two rules, both load-bearing:

      * Keyed on `uuid` ONLY. A record without one (`queue-operation`,
        `last-prompt`, `custom-title`, `mode`, `atis-latch`) passes through
        untouched, and a repeated `message.id` is NOT a duplicate record —
        Claude Code splits one assistant message across several entries, one per
        content block, so deduping on that would delete real content.
      * Keep the FIRST copy. The replay lands out of chronological order (a
        13:59 record among 14:03 neighbours), so keeping the later copy would
        feed a backwards jump into the active-time deltas and break the parent
        chain. The only thing lost is the rewritten `cwd`, which no consumer
        reads.
    """
    custom_title = None
    ai_title = None
    entries = []
    seen_uuids = set()
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            uuid = d.get("uuid")
            if uuid is not None:
                if uuid in seen_uuids:
                    continue
                seen_uuids.add(uuid)
            t = d.get("type")
            if t == "custom-title":
                custom_title = d.get("customTitle") or custom_title
            elif t == "ai-title":
                ai_title = d.get("aiTitle") or ai_title
            entries.append(d)
    return entries, custom_title, ai_title


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower())
    s = re.sub(r"^-+|-+$", "", s)
    return s or "session"


def resolve_output_path(output_dir: str, requested_name: Optional[str],
                        title: str, ext: str) -> str:
    if requested_name:
        base = requested_name
        if not base.endswith(ext):
            base = os.path.splitext(base)[0] + ext
        stem = os.path.splitext(base)[0]
    else:
        stem = slugify(title)
    path = os.path.join(output_dir, f"{stem}{ext}")
    n = 2
    while os.path.exists(path):
        path = os.path.join(output_dir, f"{stem}-{n}{ext}")
        n += 1
    return path
