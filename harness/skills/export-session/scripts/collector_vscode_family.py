#!/usr/bin/env python3
"""
VS Code-family collector — reads the GitHub Copilot Chat session store used by
VS Code and its forks, and emits the IDE-agnostic Session IR (see ir.py).

Store layout (per workspace), a JSON file per chat session:
  <app-support>/<Product>/User/workspaceStorage/<hash>/chatSessions/<id>.json
  <app-support>/<Product>/User/globalStorage/emptyWindowChatSessions/<id>.json

Session JSON shape (Copilot chat, version 3):
  { "sessionId", "creationDate", "lastMessageDate", "responderUsername",
    "requests": [ {
        "message": { "text": "<user prompt>" },
        "timestamp": <ms epoch>, "modelId": "<model>",
        "response": [ <part>, ... ],        # ordered assistant output parts
        ...
    }, ... ] }

Response parts we map:
  - a kind-less MarkdownString part `{ "value": "<assistant text>" }`  -> text
  - `{ "kind": "toolInvocationSerialized", "toolId"/"invocationMessage" }` -> tool
  - everything else (thinking, prepareToolInvocation, inlineReference, edit
    groups, mcp-starting, ...) is presentation noise and is skipped.

The store carries NO token usage and NO sub-agent structure, so the IR degrades
gracefully: token totals are zero and cost is unknown (the generators already
render "—" for unknown cost), and the transcript is flat (agent_count = 0).

This collector is PARAMETERIZED by (source_ide, product_dir) so the same code
serves any VS Code fork that writes the same chatSessions JSON. Register an
instance per fork in export_session.py. Forks whose transcript is NOT in this
JSON shape (e.g. Windsurf's opaque Cascade .pb store, Devin's cloud/IndexedDB
store) are out of scope for this collector — see the SKILL.md capture table.
"""

import glob
import json
import os
import re
import sys
from datetime import datetime, timezone

import ir as IR
import skill_registry as SR
import usage_attribution as UA

# Response-part kinds that carry a tool invocation (mapped to an IR tool block).
_TOOL_KIND = "toolInvocationSerialized"

# ── Workflow-invocation markers ──────────────────────────────────────────
# Verified against 269 real Copilot chat sessions (2,182 requests) in this
# store. Boundaries live in `request.message.parts`:
#
#   {"kind": "prompt",     "slashPromptCommand": {"command": "<name>"}}
#       A prompt-file invocation — how an Agentic Studio skill is invoked here.
#   {"kind": "agent",      "agent": {"id": ..., "name": ...}}
#       The host's own record of delegation-by-agent-name. All 100 real ones in
#       the sample are BUILT-IN Copilot participants (`@agent Continue`), so
#       those are filtered out by publisher prefix and any other agent — an
#       Agentic Studio `.agent.md` — opens a run.
#   {"kind": "subcommand", "command": {"name": "fix"}}
#       Built-in participant commands (/fix, /explain, /new). Host built-ins,
#       not workflows — excluded, the same line the Claude collector draws
#       against /model and /exit.
#
# Honest negative result: `runSubagent` tool invocations DO appear (12 real),
# but the serialized part keeps only a free-text `invocationMessage`
# ("Search for duplicate CTEs") — the agent name is not recorded anywhere in the
# part, so a delegation here cannot name a workflow run.
#
# Neither marker consults the kit's skill registry (skill_registry.py): a prompt
# file IS a unit of work whatever it is named, so a new skill is picked up with
# no change either way, and checking the name would only drop the prompt files
# and agents a user wrote themselves.
_PROMPT_PART = "prompt"
_AGENT_PART = "agent"

# Built-in chat participants are named by their extension's publisher id; a
# user- or plugin-authored agent is not published by GitHub Copilot.
_BUILTIN_AGENT_PREFIX = "github.copilot."


def _kind(name):
    """"kit" when the kit declares this skill, else "other". Labelling only —
    unlike Claude Code, nothing here is GATED on the registry."""
    return "kit" if SR.knows(name) else "other"


def _app_support_base():
    """Platform-appropriate application-support base dir.

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


def _parse_ms(ms):
    if ms is None:
        return None
    try:
        # TZ-aware UTC so IR timestamps carry an offset (+00:00), matching
        # collector_claude_code (whose ISO stamps are UTC); a naive local-time
        # datetime here would render inconsistent times across collectors.
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except Exception:
        return None


def _iso(dt):
    return dt.isoformat() if dt else None


class VSCodeFamilyCollector:
    """A collector bound to one VS Code-family product's chat store."""

    def __init__(self, source_ide, product_dir):
        self.SOURCE_IDE = source_ide
        self.product_dir = product_dir  # "Code", "Windsurf", "Devin", ...

    # ── Store location (owned by the collector, platform-neutral) ────────
    def store_root(self):
        """The product's `User/` dir — parent of both session stores."""
        return os.path.join(_app_support_base(), self.product_dir, "User")

    def _search_globs(self, sid):
        root = self.store_root()
        return [
            os.path.join(root, "workspaceStorage", "*", "chatSessions", sid + ".json"),
            os.path.join(root, "globalStorage", "emptyWindowChatSessions", sid + ".json"),
        ]

    def resolve_session(self, arg):
        """Accept a `.json` path OR a bare session id. For an id, search every
        workspace's chatSessions/ plus the empty-window store; most-recent wins.
        Returns the resolved path, or None when nothing matches."""
        if os.path.isfile(arg):
            return arg
        sid = arg[:-len(".json")] if arg.endswith(".json") else arg
        hits = []
        for pat in self._search_globs(sid):
            hits.extend(glob.glob(pat))
        if not hits:
            return None
        hits.sort(key=os.path.getmtime, reverse=True)
        if len(hits) > 1:
            print("note: %d matches for id %s; using most recent" % (len(hits), sid),
                  file=sys.stderr)
        return hits[0]

    # ── Parsing ──────────────────────────────────────────────────────────
    def _load(self, path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _first_prompt(self, doc):
        for req in doc.get("requests") or []:
            text = ((req.get("message") or {}).get("text") or "").strip()
            if text:
                first = re.sub(r"\s+", " ", text.splitlines()[0]).strip()
                if first:
                    return first
        return None

    def _resolve_title(self, doc, fallback_id):
        first = self._first_prompt(doc)
        if first:
            return (first[:64] + ("…" if len(first) > 64 else "")), "first-prompt"
        return (fallback_id or "session") + "-session", "session-id fallback"

    def _tool_block(self, part):
        """Map a toolInvocationSerialized part to the IR tool-block shape."""
        name = part.get("toolId") or part.get("toolName") or "tool"
        summary = part.get("invocationMessage")
        if not isinstance(summary, str) or not summary:
            past = part.get("pastTenseMessage")
            if isinstance(past, dict):
                summary = past.get("value")
        summary = summary if isinstance(summary, str) and summary else name
        return {"t": "tool", "name": name, "id": part.get("toolCallId"),
                "summary": summary, "command": None, "output": None,
                "output_truncated": False, "offloaded_path": None, "question": None}

    def _assistant_blocks(self, response):
        """Ordered blocks from a request's `response` list: markdown text parts
        become text blocks, tool invocations become tool blocks; everything else
        (thinking, edit groups, references, progress) is skipped."""
        blocks = []
        text_buf = []

        def flush():
            if text_buf:
                joined = "".join(text_buf).strip()
                if joined:
                    blocks.append({"t": "text", "text": joined})
                text_buf.clear()

        for part in response or []:
            if not isinstance(part, dict):
                continue
            kind = part.get("kind")
            if kind is None and isinstance(part.get("value"), str):
                text_buf.append(part["value"])
            elif kind == _TOOL_KIND:
                flush()
                blocks.append(self._tool_block(part))
            # all other kinds are presentation noise -> skip
        flush()
        return blocks

    def _turns_and_span(self, doc):
        turns = []
        model = None
        first = last = None
        for req in doc.get("requests") or []:
            ts = _parse_ms(req.get("timestamp"))
            if ts:
                first = ts if first is None or ts < first else first
                last = ts if last is None or ts > last else last
            model = model or req.get("modelId")
            user_text = ((req.get("message") or {}).get("text") or "").strip()
            if user_text:
                turns.append({"role": "user", "ts": _iso(ts),
                              "blocks": [{"t": "text", "text": user_text}],
                              "is_summary": False})
            blocks = self._assistant_blocks(req.get("response"))
            if blocks:
                turns.append({"role": "assistant", "ts": _iso(ts),
                              "blocks": blocks, "is_summary": False})
        return turns, model, first, last

    # ── Workflow-boundary adapter (feeds usage_attribution) ──────────────
    def _request_boundaries(self, req):
        """Boundary events this request opens, in the order its message parts
        appear (see the marker note at the top of this module)."""
        events = []
        for part in ((req.get("message") or {}).get("parts") or []):
            if not isinstance(part, dict):
                continue
            kind = part.get("kind")
            if kind == _PROMPT_PART:
                name = ((part.get("slashPromptCommand") or {}).get("command") or "").strip()
                if name:
                    events.append(UA.boundary(name, "prompt-file", kind=_kind(name)))
            elif kind == _AGENT_PART:
                agent = part.get("agent") or {}
                aid = str(agent.get("id") or "")
                if aid and not aid.startswith(_BUILTIN_AGENT_PREFIX):
                    name = agent.get("name") or aid
                    events.append(UA.boundary(name, "agent-mention", kind=_kind(name)))
        return events

    def _workflow_events(self, doc):
        """The ordered event stream for this session.

        This store persists NO token usage (see the module note), so each
        request contributes a zero-token usage event: the segmentation is real
        and the run counts are real, while the token and cost columns stay
        honestly empty rather than invented. If a future store version carries
        usage, only this method changes."""
        events = []
        for req in doc.get("requests") or []:
            events.extend(self._request_boundaries(req))
            events.append(UA.usage(IR.empty_tokens(), req.get("modelId")))
        return events

    # ── Public API (mirrors collector_claude_code) ───────────────────────
    def detect(self, path):
        """No sub-agents in this store; report the best title + its source."""
        doc = self._load(path)
        sid = doc.get("sessionId") or os.path.basename(path)[:-len(".json")]
        title, source = self._resolve_title(doc, sid)
        return {"count": 0, "agents": [], "title": title, "title_source": source}

    def collect(self, path, include_subagents=True, max_output=4000):
        doc = self._load(path)
        sid = doc.get("sessionId") or os.path.basename(path)[:-len(".json")]
        title, source = self._resolve_title(doc, sid)
        turns, model, first, last = self._turns_and_span(doc)
        # include_subagents / max_output are accepted for API parity but do
        # nothing here: this store has no sub-agents, and the chatSessions
        # `toolInvocationSerialized` parts persist NO tool result/output field
        # (their keys are invocationMessage / toolSpecificData / isComplete /
        # pastTenseMessage — no result or output), so `output` stays None by
        # store limitation, not by choice. Nothing to cap with max_output.
        return {
            "title": title,
            "title_source": source,
            "source_ide": self.SOURCE_IDE,
            "session_id": sid,
            "model": model,
            "started": _iso(first),
            "ended": _iso(last),
            "turns": turns,
            "agent_count": 0,
            "tokens": IR.empty_tokens(),
            "workflow_usage": UA.attribute(self._workflow_events(doc),
                                           session_tokens=IR.empty_tokens()),
        }
