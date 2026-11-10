#!/usr/bin/env python3
"""
Devin (local desktop app) collector — reads the Agent Client Protocol (ACP)
event stream the local Devin app writes, and emits the IDE-agnostic Session IR
(see ir.py).

Store layout (one NDJSON file per session, the filename is the session UUID):
  <app-support>/Devin/User/acp-events/<session-uuid>.ndjson

Each line is one JSON object:
  { "providerId": <str>, "notification": { ... } }

`notification` always carries a `sessionUpdate` discriminator. The kinds this
collector maps (ACP), with everything else treated as presentation/metadata
noise and skipped:

  user_message_chunk    -> user-turn text      (content = {text, type})
  agent_message_chunk   -> assistant-turn text  (content = {text, type})
  agent_thought_chunk   -> DROPPED — this is the agent's private thinking; like
                           the other collectors we keep it out of the message
                           body so the transcript reads as the conversation.
  tool_call             -> a tool block in the current assistant turn, keyed by
                           `toolCallId` (title -> summary; name from
                           _meta.inferenceToolName when present, else the ACP
                           `kind`; a `failed` status is folded into the summary).
  tool_call_update      -> folded into the same tool block by `toolCallId`
                           (streamed status/detail updates; does not start a new
                           turn or block). A completed update that carries a
                           `content` array is the tool's OUTPUT — its text is
                           captured into the tool block's `output` (capped at
                           max_output, with output_truncated set when the raw
                           output exceeds the cap), matching the Claude
                           collector's _enrich_tool. A status-only update (no
                           content) never clears an output already captured.
  session_info_update   -> `title` is the app's own session name (preferred title)
  usage_update          -> per-inference token counters under `_meta` (vendor keys
                           end in inputTokens / outputTokens / cachedReadTokens /
                           cachedWriteTokens); SUMMED into the session IR token
                           totals (see the "Token usage" note below).
  everything else       -> skipped (config_option_update, current_mode_update,
                           available_commands_update, ...)

Chunk reassembly: consecutive *_message_chunk events of the same role are
concatenated into ONE transcript turn (Devin streams messages in many small
chunks); a run of assistant chunks + tool calls stays one assistant turn until
the next user message.

Degradation (documented, matches the VS Code collector's "flat + no cost" story):
  * Timestamps: the ACP stream carries NO timestamp field (nothing time-like in
    `_meta` or on any record), so IR times are left None — never fabricated.
  * Token usage: `usage_update` events ARE summed into the session IR token
    totals. Verified semantics (from real sessions): each event is PER-INFERENCE
    (output is non-monotonic across events) and its `_meta` carries counters whose
    vendor keys end in inputTokens / outputTokens / cachedReadTokens /
    cachedWriteTokens (e.g. cognition.ai/inputTokens). Prompt caching dominates,
    so per event inputTokens ≈ cachedReadTokens + cachedWriteTokens and fresh
    full-price input is ~0. Each event maps as: output += outputTokens;
    cache_read += cachedReadTokens; cache_creation += cachedWriteTokens; and
    fresh input += max(0, inputTokens - cachedReadTokens - cachedWriteTokens).
    Missing / None counters count as 0. No model id is attached to the transcript,
    so cost is NOT computed (pricing._cost(None, …) returns None) — the
    breakdown / sub-agent cost columns render "—" (the summary card floors an
    uncomputable cost to "<$0.01", a frozen-generator behavior this collector
    does not control).
  * Structure: flat transcript, no sub-agent tree (agent_count = 0).

This collector is READ-ONLY (no network, no subprocess). It exposes the same
module API as the other collectors: SOURCE_IDE, store_root(), resolve_session(),
detect(), collect().
"""

import glob
import json
import os
import re
import sys

import ir as IR
import skill_registry as SR
import usage_attribution as UA

SOURCE_IDE = "devin"

_EXT = ".ndjson"

# sessionUpdate discriminators we act on.
_SU_USER = "user_message_chunk"
_SU_AGENT = "agent_message_chunk"
_SU_THOUGHT = "agent_thought_chunk"
_SU_TOOL_CALL = "tool_call"
_SU_TOOL_UPDATE = "tool_call_update"
_SU_SESSION_INFO = "session_info_update"
_SU_USAGE = "usage_update"
_SU_COMMANDS = "available_commands_update"

# ── Workflow-invocation markers ──────────────────────────────────────────
# This machine's ACP store held no session to mine, and a marker shape is not
# something to invent, so both markers below come from a source that IS
# authoritative: the two invocation forms the kit's own Devin deploy target
# renders (core/templates/tokens.yaml, DELEGATE -> devin):
#
#   "Run `/adk:{skill} {args}`"                     -> a slash command in a
#                                                      user message, the
#                                                      ACP-native command form
#   "or read `.../skills/{skill}/SKILL.md`"         -> a tool_call that reads a
#                                                      skill body inline
#
# The slash form is cross-checked against `available_commands_update` — the ACP
# event by which the app advertises its command list — whenever the stream
# carries one, so an ordinary message that merely starts with a path never opens
# a run. With no advertisement the kit's own skill registry (core/agents.yaml,
# via skill_registry.py) decides; with neither, shape alone. See _is_command.
_SLASH_RE = re.compile(r"^/([A-Za-z0-9][A-Za-z0-9_.:-]*)")
_SKILL_PATH_RE = re.compile(r"skills/([A-Za-z0-9][A-Za-z0-9_.-]*)/SKILL\.md")

# _meta key carrying the underlying tool name for a tool_call / tool_call_update.
_META_TOOL_NAME = "cognition.ai/inferenceToolName"

# usage_update `_meta` suffixes for the four per-inference token counters. The
# vendor keys are namespaced (e.g. "cognition.ai/inputTokens"), so we match on
# the suffix rather than the full key, to stay robust to the namespace prefix.
# None of these four is a suffix of another, so match order is immaterial.
_USAGE_SUFFIXES = (
    ("input", "inputTokens"),
    ("output", "outputTokens"),
    ("cache_read", "cachedReadTokens"),
    ("cache_creation", "cachedWriteTokens"),
)


def _sum_usage(tokens, meta):
    """Add ONE usage_update's per-inference counters (located by suffix in `_meta`)
    into the running IR token totals, applying the verified cache-dominant split:
    fresh input = max(0, inputTokens - cachedReadTokens - cachedWriteTokens).
    Missing / non-numeric counters count as 0. Populates the same IR token fields
    (input / output / cache_read / cache_creation) the Claude collector fills via
    IR.add_tokens, so the frozen generators light up unchanged. See the module
    "Token usage" note.

    Returns this one event's contribution as its own IR Tokens dict, so the
    workflow-attribution pass can charge it to whichever run was open at this
    position (the running total alone cannot say where spend happened)."""
    delta = IR.empty_tokens()
    if not isinstance(meta, dict):
        return delta
    vals = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    for k, v in meta.items():
        if not isinstance(k, str):
            continue
        for field, suffix in _USAGE_SUFFIXES:
            if k.endswith(suffix):
                vals[field] = v if isinstance(v, (int, float)) else 0
                break
    delta["output"] = vals["output"]
    delta["cache_read"] = vals["cache_read"]
    delta["cache_creation"] = vals["cache_creation"]
    # Fresh (full-price) input only — prompt caching means this is typically ~0;
    # clamp so a per-event undercount (in < cr+cw) never pushes the total negative.
    delta["input"] = max(0, vals["input"] - vals["cache_read"] - vals["cache_creation"])
    IR.add_tokens_from(tokens, delta)
    return delta


def _app_support_base():
    """Platform-appropriate application-support base dir (mirrors the VS Code
    collector).

    macOS   -> ~/Library/Application Support
    Windows -> %APPDATA%   (roaming)
    Linux   -> $XDG_CONFIG_HOME or ~/.config
    """
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support")
    if os.name == "nt":
        return os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    return os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")


def store_root():
    """Absolute path to Devin's ACP event store (platform-neutral)."""
    return os.path.join(_app_support_base(), "Devin", "User", "acp-events")


def resolve_session(arg):
    """Accept an `.ndjson` path OR a bare session id (the UUID filename). For an
    id, look under the ACP store; most-recent wins. Returns the resolved path, or
    None when nothing matches."""
    if os.path.isfile(arg):
        return arg
    sid = arg[:-len(_EXT)] if arg.endswith(_EXT) else arg
    hits = glob.glob(os.path.join(store_root(), sid + _EXT))
    if not hits:
        return None
    hits.sort(key=os.path.getmtime, reverse=True)
    if len(hits) > 1:
        print("note: %d matches for id %s; using most recent" % (len(hits), sid),
              file=sys.stderr)
    return hits[0]


def _iter_notifications(path):
    """Yield each line's `notification` dict, skipping blank/malformed lines."""
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            notif = rec.get("notification")
            if isinstance(notif, dict):
                yield notif


def _chunk_text(content):
    """Extract displayable text from a message-chunk `content` block
    ({text, type} for text; a small marker for non-text like images)."""
    if not isinstance(content, dict):
        return ""
    ctype = content.get("type")
    if ctype == "text":
        return content.get("text") or ""
    if ctype == "image":
        return "[image]"
    # any other content type: fall back to its text field if present, else nothing
    return content.get("text") or ""


def _tool_output_text(content):
    """Extract the result text from a tool_call_update `content` array.

    Each item is an ACP ToolCallContent block of shape
    `{content: <content-block>, type: "content"}` — the inner `content` is a
    message-style content block ({type:"text", text:...} or an image), so we run
    the same `_chunk_text` extraction on it. Defensive: if an item is itself a
    bare content block (no nested `content`), extract from the item directly.
    Multiple items are joined with newlines; an image renders as "[image]"
    (consistent with message-chunk handling)."""
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        inner = item.get("content")
        block = inner if isinstance(inner, dict) else item
        txt = _chunk_text(block)
        if txt:
            parts.append(txt)
    return "\n".join(parts)


def _raw_snippet(raw):
    """A short one-line snippet of a string rawInput (for a summary when no
    title is given)."""
    if not (isinstance(raw, str) and raw.strip()):
        return None
    snippet = re.sub(r"\s+", " ", raw.strip())
    return snippet[:80] + "…" if len(snippet) > 80 else snippet


def _apply_tool_event(blk, notif, max_output):
    """Fold a tool_call / tool_call_update into an existing tool block. Updates
    stream in over time and each may carry only a subset of fields, so we only
    OVERWRITE a field when this event actually supplies it — a later update
    without a title must never clobber the title set by the tool_call, and a
    status-only update (no content) must never clear an output already captured."""
    meta = notif.get("_meta") or {}
    # Prefer the underlying tool name; fall back to the ACP `kind`
    # (search/read/execute/edit) — inferenceToolName is not present in every
    # session, but `kind` usually is, so tool blocks stay self-describing.
    name = meta.get(_META_TOOL_NAME) if isinstance(meta, dict) else None
    if not (isinstance(name, str) and name):
        name = notif.get("kind")
    if isinstance(name, str) and name:
        blk["name"] = name
    # Summary base: a real title wins; else a rawInput snippet, but only if we
    # don't already have a (title-derived) base — never downgrade to name here.
    title = notif.get("title")
    if isinstance(title, str) and title.strip():
        blk["summary_base"] = title.strip()
    elif blk.get("summary_base") is None:
        snippet = _raw_snippet(notif.get("rawInput"))
        if snippet:
            blk["summary_base"] = "%s %s" % (blk["name"], snippet)
    status = notif.get("status")
    if isinstance(status, str) and status:
        blk["status"] = status
    # Fold a failed status into the visible summary (the generators render
    # `summary`, not `status`); completed / in_progress add nothing to show.
    base = blk.get("summary_base") or blk["name"]
    blk["summary"] = base + " — failed" if blk.get("status") == "failed" else base
    # Capture the tool's OUTPUT when this update carries a content array (parity
    # with the Claude collector's _enrich_tool: cap at max_output and flag
    # output_truncated when the raw output is longer). offloaded_path is a
    # Claude-only mechanism — left None here. A status-only update carries no
    # `content`, so this branch is skipped and any captured output is preserved.
    out = _tool_output_text(notif.get("content"))
    if out:
        if len(out) > max_output:
            blk["output"] = out[:max_output]
            blk["output_truncated"] = True
        else:
            blk["output"] = out
            blk["output_truncated"] = False


def _new_tool_block(tcid):
    return {"t": "tool", "name": "tool", "id": tcid, "summary": "tool",
            "summary_base": None, "status": None,
            "command": None, "output": None, "output_truncated": False,
            "offloaded_path": None, "question": None}


def _finalize_block(blk):
    """Strip the internal bookkeeping keys so the block matches the IR tool shape
    the frozen generators consume."""
    blk.pop("summary_base", None)
    blk.pop("status", None)
    return blk


class _Builder:
    """Streams ACP notifications into flat IR turns, reassembling chunked
    messages and folding tool_call_update into its tool_call by toolCallId."""

    def __init__(self, max_output=4000):
        self.turns = []
        self.cur_role = None
        self.cur_blocks = []
        self.text_buf = []
        self.tools = {}          # toolCallId -> tool block (shared by reference)
        self.session_title = None
        self.tokens = IR.empty_tokens()   # summed from usage_update events
        self.max_output = max_output
        # Workflow-attribution bookkeeping (see the marker note above).
        self.events = []         # ordered (boundary | usage) event stream
        self.commands = set()    # names advertised by available_commands_update
        self.user_buf = []       # current user message, still being streamed
        self.checked_tools = set()   # toolCallIds already examined for a marker

    # ── Workflow-boundary adapter (feeds usage_attribution) ──────────────
    def _is_command(self, name):
        """Whether `/name` really invoked a skill, by the best evidence
        available — in this order:

          1. the app's own advertised command list, when the stream carried one;
          2. the kit's skill registry (core/agents.yaml), which turns what would
             otherwise be a guess into a check against declared skills;
          3. shape alone, when neither is available, so a stream recorded
             without an advertisement still segments rather than going blank."""
        if self.commands:
            return name in self.commands or UA.normalize(name) in self.commands
        known = SR.names()
        if known:
            return UA.normalize(name) in known
        return True

    def _flush_user_boundary(self):
        """A user message is streamed in chunks, so its text can only be tested
        once the message is complete. Called at every transition out of the user
        role, which is exactly when the message has ended."""
        if not self.user_buf:
            return
        text = "".join(self.user_buf).strip()
        self.user_buf = []
        m = _SLASH_RE.match(text)
        if m and self._is_command(m.group(1)):
            name = m.group(1)
            self.events.append(
                UA.boundary(name, "slash-command",
                            kind="kit" if SR.knows(name) else "other"))

    def _skill_path_boundary(self, notif, tcid):
        """A tool call that reads `skills/<name>/SKILL.md` is the kit's own
        inline-delegation form on this target. Examined once per toolCallId, so
        the streamed updates that follow a call never re-open its run."""
        if tcid in self.checked_tools:
            return
        self.checked_tools.add(tcid)
        for field in ("title", "rawInput"):
            value = notif.get(field)
            if not isinstance(value, str):
                continue
            m = _SKILL_PATH_RE.search(value)
            if m:
                name = m.group(1)
                self.events.append(
                    UA.boundary(name, "skill-body-read",
                                kind="kit" if SR.knows(name) else "other"))
                return

    def _flush_text(self):
        if self.text_buf:
            joined = "".join(self.text_buf).strip()
            if joined:
                self.cur_blocks.append({"t": "text", "text": joined})
            self.text_buf = []

    def _close_turn(self):
        self._flush_text()
        if self.cur_role and self.cur_blocks:
            self.turns.append({"role": self.cur_role, "ts": None,
                               "blocks": self.cur_blocks, "is_summary": False})
        self.cur_blocks = []

    def _ensure_role(self, role):
        if self.cur_role != role:
            self._close_turn()
            self.cur_role = role

    def _tool(self, tcid):
        """Existing tool block for `tcid`, creating (and attaching to the current
        assistant turn) on first sight — handles updates that precede their call."""
        blk = self.tools.get(tcid)
        if blk is None:
            blk = _new_tool_block(tcid)
            self.tools[tcid] = blk
            self._ensure_role("assistant")
            self._flush_text()
            self.cur_blocks.append(blk)
        return blk

    def feed(self, notif):
        su = notif.get("sessionUpdate")
        if su == _SU_USER:
            self._ensure_role("user")
            txt = _chunk_text(notif.get("content"))
            if txt:
                self.text_buf.append(txt)
                self.user_buf.append(txt)
        elif su == _SU_AGENT:
            self._flush_user_boundary()
            self._ensure_role("assistant")
            self.text_buf.append(_chunk_text(notif.get("content")))
        elif su == _SU_THOUGHT:
            pass  # private thinking — intentionally dropped from the body
        elif su in (_SU_TOOL_CALL, _SU_TOOL_UPDATE):
            tcid = notif.get("toolCallId")
            if tcid is None:
                return
            self._flush_user_boundary()
            self._skill_path_boundary(notif, tcid)
            _apply_tool_event(self._tool(tcid), notif, self.max_output)
        elif su == _SU_SESSION_INFO:
            title = notif.get("title")
            if isinstance(title, str) and title.strip():
                self.session_title = title.strip()
        elif su == _SU_COMMANDS:
            for cmd in (notif.get("availableCommands")
                        or notif.get("available_commands") or []):
                name = cmd.get("name") if isinstance(cmd, dict) else cmd
                if isinstance(name, str) and name.strip():
                    self.commands.add(name.strip().lstrip("/"))
        elif su == _SU_USAGE:
            # Per-inference token counters -> summed into the session IR totals,
            # and charged as one usage event to whichever run is open here. No
            # model id exists in this stream, so cost stays None (never guessed).
            self._flush_user_boundary()
            self.events.append(UA.usage(_sum_usage(self.tokens, notif.get("_meta"))))
        # all other sessionUpdate kinds are metadata/noise -> skip

    def finish(self):
        self._flush_user_boundary()
        self._close_turn()
        for t in self.turns:
            for b in t["blocks"]:
                if b.get("t") == "tool":
                    _finalize_block(b)
        return self.turns


def _first_user_text(turns):
    """The reassembled text of the first user turn (chunks already concatenated),
    or None."""
    for t in turns:
        if t["role"] == "user":
            for b in t["blocks"]:
                if b.get("t") == "text" and b.get("text", "").strip():
                    return b["text"]
            return None
    return None


def _resolve_title(builder, turns, fallback_id):
    """Best available title: the app's own session name, else a slug of the first
    (reassembled) user message, else an id-based fallback (mirrors the tiered
    approach of the other collectors)."""
    if builder.session_title:
        title = builder.session_title
        return (title[:64] + ("…" if len(title) > 64 else "")), "session-title"
    first_user = _first_user_text(turns)
    if first_user:
        first = re.sub(r"\s+", " ", first_user.splitlines()[0]).strip()
        if first:
            return (first[:64] + ("…" if len(first) > 64 else "")), "first-prompt"
    return (fallback_id or "session") + "-session", "session-id fallback"


def _session_id(path):
    base = os.path.basename(path)
    return base[:-len(_EXT)] if base.endswith(_EXT) else base


def _build(path, max_output=4000):
    builder = _Builder(max_output)
    for notif in _iter_notifications(path):
        builder.feed(notif)
    turns = builder.finish()
    return builder, turns


def detect(path):
    """No sub-agents in this store; report the best resolvable title + its source.
    (Streams the file to find the app's title / first user message — the calling
    agent still never sees the transcript, only this summary.)"""
    builder, turns = _build(path)
    title, source = _resolve_title(builder, turns, _session_id(path))
    return {"count": 0, "agents": [], "title": title, "title_source": source}


def collect(path, include_subagents=True, max_output=4000):
    # include_subagents is accepted for API parity — this store has no sub-agent
    # tree (agent_count is always 0). max_output IS honored: a completed
    # tool_call_update's `content` is captured as the tool block's `output`,
    # capped at max_output (see _apply_tool_event / _tool_output_text).
    builder, turns = _build(path, max_output)
    sid = _session_id(path)
    title, source = _resolve_title(builder, turns, sid)
    return {
        "title": title,
        "title_source": source,
        "source_ide": SOURCE_IDE,
        "session_id": sid,
        "model": None,          # no reliable per-transcript model id in the ACP stream
                                # -> cost is not computed (pricing._cost(None, …) == None)
        "started": None,        # no timestamps in the ACP stream (not fabricated)
        "ended": None,
        "turns": turns,
        "agent_count": 0,
        "tokens": builder.tokens,   # summed from usage_update events (may be empty)
        # session_cost is None: with no model id there is nothing to price, so
        # the attribution reconciles on tokens and reports cost as unknown.
        "workflow_usage": UA.attribute(builder.events,
                                       session_tokens=builder.tokens),
    }
