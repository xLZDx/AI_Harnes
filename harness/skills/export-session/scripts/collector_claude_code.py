#!/usr/bin/env python3
"""
Claude Code collector — reads the ~/.claude/projects/<slug>/ log store and
emits the IDE-agnostic Session IR (see ir.py).

Layout this collector understands:
  <session-id>.jsonl                       main transcript
  <session-id>/subagents/agent-<id>.jsonl  sub-agent transcript (same schema)
  <session-id>/subagents/agent-<id>.meta.json  {agentType, description, toolUseId}
  <session-id>/tool-results/<id>.txt        offloaded large tool outputs

To support another IDE, write a sibling collector that emits the same IR; the
generators need no changes.
"""

import glob
import os
import json
import re
import sys
from datetime import datetime

import cc_parse as P
import ir as IR
import run_completion as RC
import skill_registry as SR
import usage_attribution as UA
from pricing import _cost  # shared, collector-agnostic pricing (PRICING/_family too)

# ── Session-store location (per-target; owned by the collector, not the
# entrypoint or the SKILL prose) ───────────────────────────────────────
# The Claude Code CLI writes transcripts under <home>/.claude/projects/.
# `os.path.expanduser("~")` is platform-neutral: it honors $HOME on POSIX
# and %USERPROFILE% (or %HOMEDRIVE%%HOMEPATH%) on Windows, so the same code
# resolves the store on Mac/Linux and Windows without branching.
SOURCE_IDE = "claude-code"


def store_root():
    """Absolute path to this IDE's session store (platform-neutral)."""
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def _cwd_project_dir(base):
    """The store dir for the current working directory, or None. Claude Code
    names each project dir after the cwd with every non-alphanumeric character
    flattened to '-' (/Users/username/Code/App -> -Users-username-Code-App)."""
    cand = os.path.join(base, re.sub(r"[^A-Za-z0-9]", "-", os.getcwd()))
    return cand if os.path.isdir(cand) else None


def resolve_session(arg):
    """Accept a `.jsonl` path, a bare session id, OR the literal alias
    `current` / `latest`, and return the transcript path.

    The alias resolves to the most-recently-written transcript — preferring
    the project dir derived from the cwd, falling back to the whole store —
    because a live session's transcript is the one being appended to right
    now. For an id, search every project dir under the store (so the caller
    need not know which project the session belongs to); most-recent wins if
    the id somehow appears under two projects.

    Returns the resolved path, or None when nothing matches (the entrypoint
    turns that into a user-facing error)."""
    if os.path.isfile(arg):
        return arg
    sid = arg[:-len(".jsonl")] if arg.endswith(".jsonl") else arg
    base = store_root()
    if sid.strip().lower() in ("current", "latest"):
        proj = _cwd_project_dir(base)
        for pattern in ([os.path.join(proj, "*.jsonl")] if proj else []) + \
                       [os.path.join(base, "*", "*.jsonl")]:
            hits = glob.glob(pattern)
            if hits:
                hits.sort(key=os.path.getmtime, reverse=True)
                # Two sessions active in the same folder are indistinguishable
                # by mtime — say so, so the caller can correct with an id.
                rivals = [h for h in hits[1:]
                          if os.path.getmtime(hits[0]) - os.path.getmtime(h) < 120]
                if rivals:
                    print("note: %d recently-active transcripts here; using %s "
                          "(newest). Pass the session id explicitly if this is "
                          "the wrong one." % (1 + len(rivals),
                          os.path.basename(hits[0])), file=sys.stderr)
                return hits[0]
        return None
    hits = glob.glob(os.path.join(base, "*", sid + ".jsonl"))
    if not hits:
        return None
    hits.sort(key=os.path.getmtime, reverse=True)
    if len(hits) > 1:
        print("note: %d matches for id %s; using most recent under %s"
              % (len(hits), sid, os.path.dirname(hits[0])), file=sys.stderr)
    return hits[0]

# Pricing table + cost math (PRICING / _family / _cost) live in pricing.py so the
# frozen, IDE-agnostic generator can share them without importing this collector.


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _load_jsonl(path):
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _scan_results(entries):
    """tool_use_id -> result text (covers tool outputs and AskUserQuestion answers)."""
    res = {}
    for d in entries:
        if d.get("type") != "user":
            continue
        c = (d.get("message", {}) or {}).get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    tid = b.get("tool_use_id")
                    if tid:
                        res[tid] = P.result_text(b)
    return res


def _enrich_tool(b, results, max_output):
    name = b["name"]
    inp = b["input"] or {}
    bid = b.get("id")
    out = results.get(bid)
    blk = {"t": "tool", "name": name, "id": bid, "summary": P.tool_summary(name, inp),
           "command": inp.get("command") if name == "Bash" else None,
           "input": None, "input_truncated": False,
           "output": None, "output_truncated": False, "offloaded_path": None,
           "question": None}
    if inp:
        input_json = json.dumps(inp, indent=2, default=str)
        if len(input_json) > max_output:
            blk["input"] = input_json[:max_output]
            blk["input_truncated"] = True
        else:
            blk["input"] = input_json
    if name == "AskUserQuestion":
        qs = inp.get("questions", []) or []
        blk["question"] = {
            "text": "; ".join(q.get("question", "") for q in qs),
            "options": "; ".join(
                ", ".join(o.get("label", "") for o in q.get("options", [])) for q in qs),
            "answer": (out or "").strip(),
        }
    elif out:
        m = re.search(r"saved to:\s*(\S+)", out)
        if "persisted-output" in out and m:
            blk["offloaded_path"] = m.group(1)
            blk["output"] = out.strip()[:max_output]
        elif len(out) > max_output:
            blk["output"] = out[:max_output]
            blk["output_truncated"] = True
        else:
            blk["output"] = out
    # Approximate "lines written" for output metrics: full content for writes,
    # added lines for edits (gross — not net of deletions/overwrites).
    if name == "Write":
        c = inp.get("content") or inp.get("file_text") or ""
        blk["lines"] = c.count("\n") + (1 if c.strip() else 0)
    elif name == "Edit":
        ns = inp.get("new_string") or ""
        blk["lines"] = ns.count("\n") + (1 if ns.strip() else 0)
    elif name == "MultiEdit":
        blk["lines"] = sum((ed.get("new_string") or "").count("\n")
                           + (1 if (ed.get("new_string") or "").strip() else 0)
                           for ed in (inp.get("edits") or []))
    elif name == "NotebookEdit":
        s = inp.get("new_source") or ""
        blk["lines"] = s.count("\n") + (1 if s.strip() else 0)
    elif name == "Bash":
        # Approximate, and only for heredocs that write — see
        # _shell_written_lines for why this cannot be exact from a transcript.
        shell = _shell_written_lines(inp)
        if shell:
            blk["lines"] = shell
    return blk


def _build_turns(entries, results, max_output):
    turns = []
    cur = None  # current assistant turn
    for d in entries:
        t = d.get("type")
        ts = d.get("timestamp")
        if t == "user":
            kind, payload = P.classify_user_entry(d)
            if kind in ("SLASH_COMMAND", "REAL_PROMPT", "CONTINUATION_SUMMARY"):
                text = ("> " + payload) if kind == "SLASH_COMMAND" else payload
                turns.append({"role": "user", "ts": ts,
                              "blocks": [{"t": "text", "text": text}],
                              "is_summary": kind == "CONTINUATION_SUMMARY"})
                cur = None
            # all other kinds (tool_result, reminders, skill bodies, ...) are
            # noise that must NOT split a continuous assistant turn.
        elif t == "assistant":
            blocks = P.extract_blocks((d.get("message", {}) or {}).get("content"))
            if not blocks:
                continue
            enriched = [_enrich_tool(b, results, max_output) if b["t"] == "tool" else b
                        for b in blocks]
            if cur is None:
                cur = {"role": "assistant", "ts": ts, "blocks": [], "is_summary": False}
                turns.append(cur)
            cur["blocks"].extend(enriched)
    return turns


def _usage_model_span(entries):
    tokens = IR.empty_tokens()
    model = None
    first = last = None
    for d in entries:
        ts = _parse_iso(d.get("timestamp"))
        if ts:
            first = ts if first is None or ts < first else first
            last = ts if last is None or ts > last else last
        if d.get("type") == "assistant":
            msg = d.get("message", {}) or {}
            model = model or msg.get("model")
            u = msg.get("usage") or {}
            if u:
                IR.add_tokens(tokens, u)
    return tokens, model, first, last


def _attach_agents(turns, ctx):
    for turn in turns:
        if turn["role"] != "assistant":
            continue
        new_blocks = []
        for blk in turn["blocks"]:
            # Any tool call whose id matches a sub-agent's meta.toolUseId is the
            # call that spawned it — the spawner is named "Agent" or "Task"
            # depending on how it was launched, so match on the id, not the name.
            if (blk.get("t") == "tool" and blk.get("id") in ctx["by_tooluse"]):
                agent = _build_agent(ctx["by_tooluse"][blk["id"]], ctx)
                if agent:
                    new_blocks.append({"t": "agent", "agent": agent})
                    continue
            new_blocks.append(blk)
        turn["blocks"] = new_blocks


def _build_agent(stem, ctx):
    if stem in ctx["visited"]:
        return None
    ctx["visited"].add(stem)
    entries = _load_jsonl(ctx["paths"][stem])
    # Recorded in ctx, not on the agent: the agent dict IS the payload, and a
    # detection detail has no business shipping to consumers.
    ctx.setdefault("signals", {})[stem] = _raw_completion_signals(entries)
    ctx.setdefault("lines", {})[stem] = _raw_written_lines(entries)
    results = _scan_results(entries)
    turns = _build_turns(entries, results, ctx["max_output"])
    tokens, model, first, last = _usage_model_span(entries)
    _attach_agents(turns, ctx)  # recurse into nested sub-agents
    meta = ctx["metas"].get(stem, {})
    return {
        "id": stem,
        "role": _resolve_role(meta, entries),
        "type": meta.get("agentType", "agent"),
        "desc": meta.get("description", ""),
        "ts": first.isoformat() if first else None,
        "duration_s": (last - first).total_seconds() if first and last else None,
        "model": model,
        "tokens": tokens,
        "cost_usd": _cost(model, tokens),
        "turns": turns,
    }


def _index_subagents(subdir):
    metas, paths, by_tooluse = {}, {}, {}
    for meta_path in glob.glob(os.path.join(subdir, "*.meta.json")):
        stem = os.path.basename(meta_path)[:-len(".meta.json")]
        jsonl = os.path.join(subdir, stem + ".jsonl")
        if not os.path.isfile(jsonl):
            continue
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            meta = {}
        metas[stem] = meta
        paths[stem] = jsonl
        tid = meta.get("toolUseId")
        if tid:
            by_tooluse[tid] = stem
    # sub-agent transcripts that exist without a meta file (rare) — keep as orphans
    for jsonl in glob.glob(os.path.join(subdir, "*.jsonl")):
        stem = os.path.basename(jsonl)[:-len(".jsonl")]
        if stem not in paths:
            metas[stem] = {}
            paths[stem] = jsonl
    return metas, paths, by_tooluse


# ── Workflow-boundary adapter (feeds usage_attribution) ─────────────────
# This collector's half of the per-workflow attribution: it walks the parsed
# entry stream and emits the ordered (boundary | usage | elapsed) events the
# shared segmenter folds into runs. The algorithm itself lives in
# usage_attribution.py and is identical for every host.
#
# THREE marker families open a run here:
#
#   1. ACTIVATION — a `<command-name>` slash command whose skill body loads
#      (or whose name the kit declares), and
#   2. ACTIVATION — an assistant `Skill` tool call.
#      Both verified against every real transcript on disk; see cc_parse's
#      marker note for the counts.
#   3. MENTION — an L1 WORKFLOW named as a standalone token in text the USER
#      TYPED. No slash, no activation proof: naming the workflow is asking for
#      it. Two limits, both registry-driven (skill_registry.workflows()):
#        * only agents.yaml entries with menu_group: workflows qualify. An L2
#          role is delegated to from INSIDE a workflow, so its spend already
#          belongs to that run — mentioning one in conversation is talk about
#          the work, not the work. (Activation still opens a run for ANY skill:
#          a user really can invoke a role directly.)
#        * a non-kit skill is not in the registry at all, so it can never be
#          mentioned into a run — it stays activation-only.
#      Detection runs strictly over cc_parse.authored_text(), which is what
#      keeps the user role's tool results, system reminders (they list skill
#      names constantly), command caveats and injected skill bodies from
#      inventing runs.
#
# A mention immediately confirmed by an activation of the SAME skill is ONE run
# (see _coalesce_mentions) — the activation proves the mention rather than
# starting a second run. Activation-to-activation never coalesces: two real
# invocations are two runs, per the original spec.

# How far after a slash command the skill body may appear before we stop
# believing the command activated a skill. Measured at exactly 1 across 129
# real invocations; 4 leaves headroom without ever reaching the next command.
SKILL_BODY_WINDOW = 4


def _skill_activated(entries, i, name):
    """Whether the slash command at entries[i] invoked a skill rather than a CLI
    built-in (/model, /exit, /plugin — same shape, not workflows).

    Two independent ways to say yes, and either is enough:
      * the log's own activation proof — the "Base directory for this skill"
        entry Claude Code writes when a body loads (see cc_parse's marker note);
      * the kit's own skill registry, which catches an invocation whose proof is
        missing because the transcript was truncated mid-activation or the run
        was interrupted.

    The proof comes first because it also covers skills the kit does not
    declare — in real sessions those include the costliest workflows of all."""
    short = name.rsplit(":", 1)[-1]
    for j in range(i + 1, min(i + 1 + SKILL_BODY_WINDOW, len(entries))):
        if P.skill_body_name(entries[j]) == short:
            return True
    return SR.knows(name)


# Boundary sources. MENTION is the only one a user's prose can produce; the two
# activation sources are the only ones that can absorb it.
MENTION = "skill-mention"
MENTION_ACTIVATED = "skill-mention-activated"
ACTIVATION_SOURCES = ("slash-command", "skill-tool")


def _kind(name):
    """"kit" when the kit declares this skill, else "other" — what the surfaces
    label a row with, and the same registry the mention marker is built from."""
    return "kit" if SR.knows(name) else "other"


def _coalesce_mentions(events):
    """Collapse `mention of X` immediately followed by `activation of X` into one
    run.

    The surviving boundary is the MENTION, so the run starts where the user asked
    rather than where the machinery caught up — the thinking between the two was
    part of the run. Only an unconfirmed mention absorbs an activation:
    activation-to-activation stays two runs, and a mention already confirmed does
    not swallow a second, genuine invocation. "Immediately" means no other
    boundary in between; usage and elapsed events may sit between them freely."""
    out = []
    for event in events:
        if (event["t"] == "boundary"
                and event.get("source") in ACTIVATION_SOURCES):
            previous = next((e for e in reversed(out) if e["t"] == "boundary"), None)
            if (previous is not None
                    and previous.get("source") == MENTION
                    and previous["workflow"] == event["workflow"]):
                previous["source"] = MENTION_ACTIVATED
                continue
        out.append(event)
    return out


# Always loaded by every delegate, so it never identifies the role it was given.
_ALWAYS_LOADED = "adk-context"



# A kit delegation's prompt OPENS with the DELEGATE token's own rendered form:
#   Read <CLAUDE_PLUGIN_ROOT>/skills/<skill>/SKILL.md and follow it. <args>
# Anchoring on that opening — rather than searching the prompt for a skill name
# — is what makes this a structural read instead of a text match. The plugin
# path segment (/plugins/.../adk/<version>/skills/) proves the file is the
# INSTALLED kit skill; the marketplace name is deliberately not matched, since
# it is org-specific and the kit forbids such literals.
_DELEGATION_OPENING = re.compile(
    r"^\s*Read\s+\S*/plugins/\S*/adk/[^/\s]+/skills/(adk-[A-Za-z0-9_-]+)"
    r"/SKILL\.md\s+and follow it", re.I)


def _first_prompt(entries):
    """Text of the delegate's own first user message — the prompt it was
    handed. Only its OPENING is ever examined; see _DELEGATION_OPENING."""
    for e in entries or ():
        if e.get("type") != "user":
            continue
        c = (e.get("message") or {}).get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return " ".join(b.get("text", "") for b in c
                            if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _resolve_role(meta, entries):
    """The kit ROLE a delegate was given, or None.

    Three registry-driven paths, in priority order — no hand-maintained mapping
    anywhere, so a skill added to agents.yaml resolves with no code change:

      (a) `agentType` from the sub-agent's meta, when it names a declared skill.
          The FUTURE-PROOF path: the day plugin-scoped subagents become usable,
          delegations start carrying the role here and this begins working with
          no change at all.
      (b) The delegate's own prompt, matched ONLY at its opening against the
          DELEGATE token's rendered form (Read <plugin>/skills/<skill>/SKILL.md
          and follow it). Structural, not a search.
      (c) No match: not a kit role. It is simply a delegated agent.

    Two earlier attempts were removed for the same fault: they confused the
    SUBJECT of a delegation with its ROLE. Searching the prompt for any
    SKILL.md path, and matching a skill name anywhere in the meta description,
    both labelled agents whose instructions merely CONCERNED a skill — "author
    the cue sheet for adk-design-ux" resolved to that skill. Measured on a real
    session, that produced five confident labels, all wrong. A wrong role reads
    as fact and is worse than an absent one.

    Anchoring on the delegation's opening fixes it because the opening is
    generated by the DELEGATE token itself, not written by whoever composed the
    prompt. On the same session it resolves 5 of 32 agents — exactly the five
    real adk-product-analyst delegations (decomposer, two writers, reviewer,
    publisher) — and declines the other 27, which are ad-hoc agents that were
    never delegations at all. No false positives, and none of the 27 is a
    delegation being missed.

    FOLLOW-UP, deliberately not done here: rendering the role into the DELEGATE
    token would make (b) exact by construction. It touches every workflow on all
    five deploy targets, and that PR is waiting to merge."""
    declared = SR.names()
    if not declared:
        return None
    agent_type = (meta or {}).get("agentType") or ""
    if UA.normalize(agent_type) in declared:
        return UA.normalize(agent_type)
    m = _DELEGATION_OPENING.match(_first_prompt(entries))
    if m and m.group(1) in declared and m.group(1) != _ALWAYS_LOADED:
        return m.group(1)
    return None



def _agent_subtree_usage(agent, lines_by_stem=None):
    """One usage event per agent for an agent and every agent nested inside it,
    carrying the label/model/turns the Usage tab shows on its detail rows. A
    delegate's spend belongs to whoever delegated, so the whole subtree is
    injected at the position of the spawning tool call — all of it, on that one
    run, never split and never handed to a workflow that opens later while the
    delegate is still working."""
    own = (lines_by_stem or {}).get(agent["id"], ())
    by_ext = {}
    for count, ext in own:
        if ext:
            by_ext[ext] = by_ext.get(ext, 0) + count
    out = [UA.usage(agent["tokens"], agent.get("model"), agent.get("cost_usd"),
                    label=agent.get("type") or "agent", desc=agent.get("desc") or "",
                    turns=len(agent.get("turns") or ()), agent=True,
                    role=agent.get("role"),
                    # This agent's OWN lines — not its children's, which ride on
                    # their own events, so every row shows what it wrote.
                    lines=sum(c for c, _ in own), lines_by_ext=by_ext,
                    # The agent's own span, for its detail row only. It is NOT
                    # added to the run's active time: siblings overlap, and the
                    # main thread already counted the elapsed wait as one long
                    # tool call.
                    duration_s=agent.get("duration_s"),
                    agent_id=agent.get("id"))]
    for turn in agent.get("turns", []):
        for blk in turn.get("blocks", []):
            if blk.get("t") == "agent":
                out.extend(_agent_subtree_usage(blk["agent"], lines_by_stem))
    return out


# Defined in the IR so every surface that renders lines agrees on it — see the
# note there for why shell lines get one bucket instead of a per-extension split.
SHELL_EXT = IR.SHELL_EXT

_HEREDOC_OPEN = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")

# An interpreter heredoc that actually writes. Deliberately narrow: `>` alone
# matches comparisons and arrows in ordinary code.
_SHELL_WRITES = re.compile(r"\.write_text\s*\(|\bopen\s*\([^)]*['\"][wa]b?['\"]")

# The heredoc IS the file: `cat > f <<'EOF'`, `tee f <<'EOF'`.
_REDIRECTS_TO_FILE = re.compile(r"(?<![0-9<>])>>?\s*['\"]?[\w./${}~-]+|\btee\b")


def _heredoc_blocks(command):
    """(opening_line, body_lines) for every heredoc in a shell command.

    The opening line matters as much as the body: `cat > f <<'EOF'` names its
    target there, while a `python - <<'PY'` writes from inside the body.
    """
    out = []
    lines = command.splitlines()
    i = 0
    while i < len(lines):
        match = _HEREDOC_OPEN.search(lines[i])
        if not match:
            i += 1
            continue
        delim, opening, body = match.group(2), lines[i], []
        i += 1
        while i < len(lines) and lines[i].strip() != delim:
            body.append(lines[i])
            i += 1
        out.append((opening, body))
        i += 1
    return out


def _shell_written_lines(tool_input):
    """Approximate lines authored by a Bash command that writes a file.

    APPROXIMATE, and cannot be otherwise. The transcript stores a Bash call's
    command text and nothing else — no target path, no diff — and the content
    written is frequently COMPUTED from the file's current contents
    (`src.replace(old, new)`), which never appears in the log at all. So this
    is a presence-and-scale signal, not a measurement.

    Only heredoc bodies count. Restricting to them is what keeps a command that
    merely MENTIONS writing -- an inline `python -c` whose regex happens to
    contain `write_text` -- from being counted as an edit; measured against a
    real session, that restriction was the difference between a clean 18 and 21
    with three false positives.

    Scaffolding inside an interpreter heredoc (imports, the anchor text being
    matched, asserts, prints) is counted along with the content, which biases
    this HIGH -- roughly 1.4x against git on the session this was calibrated on.
    That direction is deliberate and matches the metric's existing meaning: it
    reports editing activity, not artifact size, exactly as an Edit contributing
    its whole new_string every time already does.
    """
    command = (tool_input or {}).get("command") or ""
    if not command:
        return 0
    total = 0
    for opening, body in _heredoc_blocks(command):
        text = "\n".join(body)
        # Body writes (interpreter heredoc), or the heredoc IS the file.
        if _SHELL_WRITES.search(text) or _REDIRECTS_TO_FILE.search(opening):
            total += sum(1 for line in body if line.strip())
    return total


def _written_lines(tool_name, tool_input):
    """(lines, extension) for one write-family tool call, or None.

    GROSS lines added, matching the per-block `lines` the IR already reports:
    an Edit contributes its new_string every time, so a file edited twenty
    times counts twenty times. That makes this a measure of editing ACTIVITY,
    not of artifact size, and it is labelled that way wherever it surfaces.

    Bash counts too, under SHELL_EXT. Without it a session that edits through
    the shell -- which is how a good deal of work is actually done -- reports
    zero lines written while having authored hundreds.
    """
    if not isinstance(tool_input, dict):
        return None
    if tool_name == "Bash":
        shell = _shell_written_lines(tool_input)
        return (shell, SHELL_EXT) if shell else None
    if tool_name == "Write":
        body = tool_input.get("content") or tool_input.get("file_text") or ""
    elif tool_name == "Edit":
        body = tool_input.get("new_string") or ""
    elif tool_name == "NotebookEdit":
        body = tool_input.get("new_source") or ""
    elif tool_name == "MultiEdit":
        body = "\n".join((e.get("new_string") or "")
                          for e in (tool_input.get("edits") or []))
    else:
        return None
    lines = body.count("\n") + (1 if body.strip() else 0)
    if not lines:
        return None
    paths = RC.written_paths(tool_name, tool_input)
    ext = os.path.splitext(paths[0])[1].lower() if paths else ""
    return (lines, ext or None)


def _raw_written_lines(entries):
    """Every (lines, ext) these RAW entries wrote — the delegate-side twin of
    the main-thread scan, so a sub-agent's files count for the run that spawned
    it exactly as its tokens and its terminal artifacts already do."""
    out = []
    for d in entries:
        if d.get("type") != "assistant":
            continue
        for b in (d.get("message", {}) or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                hit = _written_lines(b.get("name"), b.get("input"))
                if hit:
                    out.append(hit)
    return out


def _raw_completion_signals(entries):
    """Every completion signal these RAW transcript entries emit, as
    (kind, value) pairs — see run_completion.signals.

    Raw, not the IR: an IR tool block keeps its input as JSON capped at
    `max_output` for display, and a Write's input carries the whole file body —
    so it is essentially always truncated. Detection read from there would miss
    nearly every write. The untruncated entries are the only sound source."""
    kinds = RC.active_kinds()
    if not kinds:
        return []                   # nothing declares an ending — collect nothing
    out = []
    for d in entries:
        if d.get("type") != "assistant":
            continue
        for b in (d.get("message", {}) or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                out.extend(RC.signals(b.get("name"), b.get("input"), kinds))
    return out


def _agent_subtree_stems(agent):
    """This agent's id and every id nested inside it."""
    out = [agent["id"]]
    for turn in agent.get("turns", []):
        for blk in turn.get("blocks", []):
            if blk.get("t") == "agent":
                out.extend(_agent_subtree_stems(blk["agent"]))
    return out


def _agent_usage_positions(turns, by_tooluse, agent_signals_by_stem=None,
                           agent_lines_by_stem=None):
    """Split sub-agent spend into (by spawning tool_use id, unattributable,
    files written by spawning tool_use id).

    A sub-agent linked to the tool call that spawned it can be placed exactly:
    its usage is injected where that call sits, so it lands on the run that was
    open. An UNLINKED sub-agent (no meta.toolUseId — rare, and the collector
    already renders those in a trailing section) has no position at all, so it
    cannot honestly be charged to any run: it goes to unattributed."""
    stem_to_tid = {stem: tid for tid, stem in by_tooluse.items()}
    positioned, unplaced, signals = {}, [], {}
    for turn in turns:
        for blk in turn.get("blocks", []):
            if blk.get("t") != "agent":
                continue
            agent = blk["agent"]
            spend = _agent_subtree_usage(agent, agent_lines_by_stem)
            tid = stem_to_tid.get(agent["id"])
            if tid is None:
                unplaced.extend(spend)
            else:
                positioned.setdefault(tid, []).extend(spend)
                # The files this delegate and its own delegates wrote, at the
                # same position their spend was charged — that is where a
                # terminal artifact one of them produced has to close the run.
                for stem in _agent_subtree_stems(agent):
                    signals.setdefault(tid, []).extend(
                        (agent_signals_by_stem or {}).get(stem, ()))
    return positioned, unplaced, signals


# Entry kinds that START a response chain — i.e. something arrived that the
# assistant then answered. The first two are the human typing; the third is the
# injected summary that opens a RESUMED session, which also triggers a response
# chain and so must start one here: without it, the days between the old session
# ending and the new one beginning would be counted as working time.
_CHAIN_STARTERS = ("REAL_PROMPT", "SLASH_COMMAND", "CONTINUATION_SUMMARY")


def _activation_payload_indices(entries):
    """Entry indices that hold an activation's OWN injected payload, not a new
    request from anyone.

    When a skill is invoked, its body is injected as a user-role entry right
    behind the call — measured across every real transcript on disk, a `Skill`
    call's body lands 2 to 5 entries later. A plugin skill's body is
    self-identifying ("Base directory for this skill"), but a personal or
    built-in skill's is not: it just opens with prose like "You are a senior
    security engineer…", which no shape rule can separate from a person typing.
    So it is excluded POSITIONALLY instead — nothing between a tool call and its
    result can be a human typing.

    Cost of being wrong: if a user did interrupt in that window, one mention is
    missed. Cost of not doing it: an injected prompt full of skill names opens
    runs nobody asked for."""
    suppressed = set()
    for i, d in enumerate(entries):
        if d.get("type") != "assistant":
            continue
        for b in (d.get("message", {}) or {}).get("content") or []:
            if (isinstance(b, dict) and b.get("type") == "tool_use"
                    and b.get("name") == "Skill"):
                suppressed.update(range(i + 1, i + 2 + SKILL_BODY_WINDOW))
    return suppressed


def _active_deltas(entries):
    """{entry index -> active seconds between that entry and the next}.

    ACTIVE means the work was in flight. A response chain runs from the message
    that triggered it to the last entry before the next such message, and every
    gap INSIDE that chain is time the machine was busy — including a long tool
    call, whose start and end are both logged, and therefore including the
    elapsed time of a delegated agent, since delegation appears on this thread as
    one long tool call. That is why Active needs no cross-transcript union maths.

    The gap from a chain's last entry to the NEXT triggering message is a human
    reading, thinking or going to lunch. It belongs to neither the workflow that
    just finished nor the one about to start, so it is simply not emitted.

    Summing these deltas IS the union of the active intervals: consecutive gaps
    inside a chain tile the interval exactly, and they cannot overlap. A negative
    delta (clock skew, out-of-order writes) is dropped rather than subtracted."""
    stamps = [_parse_iso(d.get("timestamp")) for d in entries]
    starts = [d.get("type") == "user"
              and P.classify_user_entry(d)[0] in _CHAIN_STARTERS
              for d in entries]
    # A turn may own an interval only if it BILLED for one. Same rule as row
    # creation — "no tokens, no row" — applied to ownership so the two cannot
    # diverge: otherwise a `<synthetic>` notice (all-zero usage, and in one real
    # session the entry that ends a 6h stall) would claim hours of active time
    # and mint a main row for a model that never ran.
    models = [_billing_model(d) for d in entries]
    deltas = {}
    for i in range(len(entries) - 1):
        if starts[i + 1]:
            continue                      # human wait — belongs to nobody
        if stamps[i] is None or stamps[i + 1] is None:
            continue                      # untimed host: no active time claimed
        seconds = (stamps[i + 1] - stamps[i]).total_seconds()
        if seconds > 0:
            deltas[i] = (seconds, _interval_model(models, i))
    return deltas


def _billing_model(entry):
    """This entry's model, but only when it actually consumed tokens."""
    if entry.get("type") != "assistant":
        return None
    msg = entry.get("message", {}) or {}
    tokens = IR.empty_tokens()
    IR.add_tokens(tokens, msg.get("usage") or {})
    return msg.get("model") if any(tokens.values()) else None


def _interval_model(models, i):
    """The assistant turn that OWNS the interval between entries i and i+1.

    The work in that gap produced entry i+1, so when i+1 is an assistant entry
    it is that turn's model. When i+1 is a tool result, the turn in flight is
    the most recent assistant entry at or before i. Falling back to the nearest
    assistant entry either way means an interval is never left unattributed —
    which is what makes the main rows sum to the segment's active time exactly.

    A model is fixed for the whole of a turn and switches only between turns, so
    an interval cannot span two models and the granularity needs no tie-break.
    Were one ever needed, the rule is the FIRST assistant entry's model."""
    if models[i + 1]:
        return models[i + 1]
    for j in range(i, -1, -1):
        if models[j]:
            return models[j]
    for j in range(i + 1, len(models)):
        if models[j]:
            return models[j]
    return None


def _completion_events(entries, agent_signals):
    """{entry index -> (complete event, ...)} for every end-state signal this
    session emitted — a terminal artifact written, a terminal command run, or a
    terminal tool invoked.

    A `complete` event names the workflow whose end state the signal marks, and
    the segmenter closes the open run only if it IS that workflow — another
    skill's terminal signal appearing mid-run means nothing. So this scanner can
    stay stateless: it reports what happened where, and the segmenter decides
    whether it ended anything.

    Both threads are scanned. A main-thread signal is placed at its own entry; a
    DELEGATE's is placed at the entry that spawned it, which is the only
    position it has on this thread and the same place its spend was charged.
    Delegates count for all three kinds, not just writes: a workflow whose last
    act is a devops sub-agent opening the pull request ends there just as surely
    as one whose last act is a file."""
    kinds = RC.active_kinds()
    if not kinds:
        return {}                       # no cue sheets reachable — no early close
    # Indexed per kind so a signal is tested only against sheets that declare
    # that kind, rather than against every watched workflow.
    by_kind = {kind: RC.workflows_by_kind(kind) for kind in kinds}
    out = {}

    def note(index, kind, value):
        for workflow in by_kind.get(kind, ()):
            if RC.closes_signal(workflow, kind, value):
                out.setdefault(index, []).append(UA.complete(workflow))

    for index, d in enumerate(entries):
        if d.get("type") != "assistant":
            continue
        for b in (d.get("message", {}) or {}).get("content") or []:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            for kind, value in RC.signals(b.get("name"), b.get("input"), kinds):
                note(index, kind, value)
            for kind, value in agent_signals.get(b.get("id"), ()):
                note(index, kind, value)
    return out


def _workflow_events(entries, model, agent_usage, unplaced_usage,
                     agent_signals=None):
    """The ordered event stream for this session — see the marker note above for
    the three boundary families and the coalescing rule.

    Sub-agent delegation is deliberately NOT a boundary. The kit renders its
    Claude delegations as Agent(subagent_type="general-purpose", …) because
    plugin-scoped subagents cannot reach MCP (claude-code#13605), so the agent
    name carries no workflow identity in this host — 350 of 357 real spawns are
    the generic type. The delegate's tokens are injected at the spawn position
    instead, which charges the workflow run that delegated for what it spent.

    A run also gets an END. Whenever a file is written whose path matches the
    open workflow's own `completes_run` globs, a `complete` event is emitted so
    the run closes there — see run_completion. Writes by a DELEGATE count: the
    terminal artifact is often the sub-agent's output (`created-pbis.json` is
    the Publisher's), so watching only this thread would never see a run
    finish."""
    deltas = _active_deltas(entries)
    injected = _activation_payload_indices(entries)
    events = []
    # `agent_usage` is a DICT keyed by spawning tool_use id, and the session
    # totals sum its values — so each delegate's subtree counts once there. This
    # loop walks raw entries instead, so a tool_use id appearing at two
    # positions would inject that subtree twice and the two sums would disagree.
    # Emitting each id at most once is what makes this stream agree with the
    # dict's own semantics, whatever a host does with its ids.
    spawns_emitted = set()
    completions = _completion_events(entries, agent_signals or {})
    for i, d in enumerate(entries):
        t = d.get("type")
        if t == "user":
            # Mentions first, then the command: a mention of the skill the
            # command invokes is the same intent, and putting it first lets
            # _coalesce_mentions fold the pair into one run that starts where
            # the user asked.
            authored = "" if i in injected else P.authored_text(d)
            for mentioned in SR.mentions(authored):
                events.append(UA.boundary(mentioned, MENTION, kind="kit"))
            name = P.slash_command_name(d)
            if name and _skill_activated(entries, i, name):
                events.append(UA.boundary(name, "slash-command", kind=_kind(name)))
            # cc_parse already classifies a stopped turn; it just never reached
            # attribution before. Emitted AFTER any boundary on this entry so
            # it marks the run that was actually interrupted.
            if P.classify_user_entry(d)[0] == "INTERRUPT":
                events.append(UA.interrupt())
        elif t == "assistant":
            msg = d.get("message", {}) or {}
            tokens = IR.empty_tokens()
            IR.add_tokens(tokens, msg.get("usage") or {})
            # Resolved for the whole entry: the usage event AND anything this
            # entry wrote belong to the same model.
            entry_model = msg.get("model") or model
            if any(tokens.values()):
                # Priced at THIS ENTRY'S model, not the session's. A user
                # switches model mid-conversation — this session ran three on
                # the main thread — and pricing them all at the first one seen
                # overstated its cost by 18.7%. The session headline is derived
                # from these per-entry costs now: when the parts are right and
                # the whole disagrees, the whole is what was wrong.
                events.append(UA.usage(tokens, entry_model,
                                       _cost(entry_model, tokens)))
                # An entry with no tokens (a `<synthetic>` notice, an aborted
                # turn) is skipped above rather than special-cased by name: it
                # contributes nothing, so it must not produce a row or tag a
                # model that never billed.
            for b in msg.get("content") or []:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                # The usage above is emitted BEFORE this boundary on purpose:
                # those tokens were spent deciding to invoke the skill.
                if b.get("name") == "Skill":
                    skill = (b.get("input") or {}).get("skill")
                    if skill:
                        events.append(
                            UA.boundary(skill, "skill-tool", kind=_kind(skill)))
                spend = agent_usage.get(b.get("id"), ())
                if spend and b.get("id") not in spawns_emitted:
                    spawns_emitted.add(b.get("id"))
                    events.extend(spend)
                # What this call wrote, and what any delegate it spawned wrote,
                # both land at this position — the same placement rule the
                # delegate's tokens and terminal artifacts already follow.
                hit = _written_lines(b.get("name"), b.get("input"))
                if hit:
                    events.append(UA.produced(hit[0], hit[1], entry_model))
        # A terminal artifact written at this entry closes the run it belongs
        # to. Emitted after the entry's own spend, so the write itself is inside
        # the run that produced it.
        events.extend(completions.get(i, ()))
        # Active time is emitted LAST for this entry, so a boundary opened here
        # owns the work that follows it — the same reasoning that puts an
        # entry's token usage before the boundary it contains.
        if i in deltas:
            seconds, owner = deltas[i]
            events.append(UA.elapsed(seconds, owner))
    if unplaced_usage:
        events.append(UA.boundary(None, "unlinked-subagent"))
        events.extend(unplaced_usage)
    return _coalesce_mentions(events)


def _session_totals(main_tokens, events, agent_usage, unplaced_usage):
    """The session totals the attribution must reconcile against.

    TOKENS are cross-checked INDEPENDENTLY — the main transcript's own span plus
    every sub-agent's — so the reconciliation is a real second opinion rather
    than the same sum computed twice.

    COST is the sum of the per-entry costs, because that is the only correct way
    to price a session that switched models: there is no single rate to apply to
    an aggregate. This is the corrected behaviour; pricing aggregate main tokens
    at one model was the defect."""
    tokens = dict(main_tokens)
    for spend in list(agent_usage.values()) + [unplaced_usage]:
        for event in spend:
            IR.add_tokens_from(tokens, event["tokens"])
    priced = [e["cost"] for e in events
              if e["t"] == "usage" and e["cost"] is not None]
    return tokens, (sum(priced) if priced else None)


def _main_cost(events):
    """The main transcript's cost: the sum of the per-entry costs, at each
    entry's own model.

    Main-thread events only — a delegate's spend carries `agent=True` — so this
    is the session headline's share of the money and is independent of how the
    attribution pass divides it up. None when nothing could be priced."""
    priced = [e["cost"] for e in events
              if e["t"] == "usage" and not e.get("agent") and e["cost"] is not None]
    return sum(priced) if priced else None


def _attribution(events, session_tokens, session_cost):
    """(workflow_usage, error) — the per-workflow breakdown, or None and why.

    The reconciliation guard stays hard: a breakdown that does not add up to the
    session is never returned, never rounded off, never partially shown. What
    this narrows is the BLAST RADIUS. A session's totals, cost, model, span and
    main/agent split are derived without this pass and are still true when it
    fails, so blanking the whole screen threw away trustworthy figures along
    with the untrustworthy one.

    So the breakdown is WITHHELD rather than degraded, and the reason travels
    with the gap. Every surface must say the attribution is unavailable and why
    — a partial breakdown presented as complete is exactly what the guard
    exists to prevent, and this must not become a way around it."""
    try:
        return UA.attribute(events, session_tokens=session_tokens,
                            session_cost=session_cost), None
    except UA.ReconciliationError as e:
        return None, str(e)


def _resolve_title(entries, custom_title, ai_title, out_base):
    """Best available session title: an explicit custom title, else Claude's ai-title,
    else a short slug of the first real user prompt, else the project-name fallback.
    (The desktop app's own display name is not in the logs, so first-prompt is our best
    on-disk approximation of it.)"""
    if custom_title:
        return custom_title, "custom-title"
    if ai_title:
        return ai_title, "ai-title"
    for d in entries:
        if d.get("type") == "user":
            kind, payload = P.classify_user_entry(d)
            if kind in ("REAL_PROMPT", "SLASH_COMMAND") and payload:
                first = re.sub(r"\s+", " ", payload.splitlines()[0]).strip()
                if first:
                    return (first[:64] + ("…" if len(first) > 64 else "")), "first-prompt"
    return out_base + "-session", "project-name fallback"


def detect(main_jsonl):
    """Pre-scan: sub-agent count/list + the best resolvable title and its source, so the
    caller can decide whether to pass an explicit --title. No transcript parsing."""
    subdir = os.path.join(main_jsonl[:-len(".jsonl")], "subagents")
    agents = []
    if os.path.isdir(subdir):
        for meta_path in sorted(glob.glob(os.path.join(subdir, "*.meta.json"))):
            stem = os.path.basename(meta_path)[:-len(".meta.json")]
            if not os.path.isfile(os.path.join(subdir, stem + ".jsonl")):
                continue
            try:
                with open(meta_path, encoding="utf-8") as f:
                    m = json.load(f)
            except Exception:
                m = {}
            agents.append({"id": stem, "type": m.get("agentType", "agent"),
                           "desc": m.get("description", "")})
    entries, custom_title, ai_title = P.load_entries(main_jsonl)
    out_base = os.path.basename(os.path.dirname(os.path.abspath(main_jsonl)))
    title, source = _resolve_title(entries, custom_title, ai_title, out_base)
    return {"count": len(agents), "agents": agents, "title": title, "title_source": source}


def collect(main_jsonl, include_subagents=True, max_output=4000):
    entries, custom_title, ai_title = P.load_entries(main_jsonl)
    out_base = os.path.basename(os.path.dirname(os.path.abspath(main_jsonl)))
    title, source = _resolve_title(entries, custom_title, ai_title, out_base)

    results = _scan_results(entries)
    turns = _build_turns(entries, results, max_output)
    tokens, model, first, last = _usage_model_span(entries)

    agent_count = 0
    by_tooluse = {}
    ctx_signals = {}    # stem -> completion signals that agent emitted (raw)
    ctx_lines = {}      # stem -> (lines, ext) that agent wrote (raw)
    subdir = os.path.join(main_jsonl[:-len(".jsonl")], "subagents")
    if include_subagents and os.path.isdir(subdir):
        metas, paths, by_tooluse = _index_subagents(subdir)
        ctx = {"metas": metas, "paths": paths, "by_tooluse": by_tooluse,
               "visited": set(), "max_output": max_output,
               "signals": ctx_signals, "lines": ctx_lines}
        _attach_agents(turns, ctx)
        # any sub-agent not linked to a Task call goes into a trailing section
        orphans = [s for s in paths if s not in ctx["visited"]]
        if orphans:
            blocks = []
            for stem in sorted(orphans):
                a = _build_agent(stem, ctx)
                if a:
                    blocks.append({"t": "agent", "agent": a})
            if blocks:
                turns.append({"role": "assistant", "ts": None, "is_summary": False,
                              "blocks": [{"t": "text", "text": "_Unlinked sub-agents_"}] + blocks})
        agent_count = len(ctx["visited"])

    # Per-workflow-run attribution over the same entry stream. Runs last so the
    # sub-agent tree is already attached and every delegate's spend can be
    # placed at the call that spawned it.
    agent_usage, unplaced_usage, agent_signals = _agent_usage_positions(
        turns, by_tooluse, ctx_signals, ctx_lines)
    events = _workflow_events(entries, model, agent_usage, unplaced_usage,
                              agent_signals)
    session_tokens, session_cost = _session_totals(tokens, events, agent_usage,
                                                   unplaced_usage)
    workflow_usage, workflow_usage_error = _attribution(
        events, session_tokens, session_cost)

    return {
        "title": title,
        "title_source": source,
        "source_ide": "claude-code",
        "session_id": os.path.basename(main_jsonl)[:-len(".jsonl")]
        if main_jsonl.endswith(".jsonl") else os.path.basename(main_jsonl),
        "model": model,
        "started": first.isoformat() if first else None,
        "ended": last.isoformat() if last else None,
        "turns": turns,
        "agent_count": agent_count,
        "tokens": tokens,
        "workflow_usage": workflow_usage,
        # Why the breakdown is missing, when it is. None on a healthy session.
        "workflow_usage_error": workflow_usage_error,
        # The main transcript's cost, priced PER ENTRY at the model that entry
        # used. Carried in its own right rather than left to be re-derived from
        # the attribution's detail rows, because it is the one figure the
        # session headline needs from this pass and it stays correct when the
        # per-workflow split does not: a delegate injected twice is an
        # `agent=True` event and never touches this sum.
        "main_cost_usd": _main_cost(events),
    }
