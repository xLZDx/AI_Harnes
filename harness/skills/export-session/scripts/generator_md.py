#!/usr/bin/env python3
"""
Markdown generator — Session IR -> markdown. Secondary to the HTML generator
(the skill defaults to HTML); kept for pasting into plain-text targets.
Sub-agents render as collapsible <details> blocks (works on GitHub/most viewers).
"""

import generator_html as _GH  # reuse the single IDE-name source of truth (no cycle)
import ir as IR


def _blocks_md(blocks, depth):
    out = []
    for b in blocks:
        if b["t"] == "text":
            out.append(b["text"].strip())
        elif b["t"] == "tool":
            q = b.get("question")
            if q:
                out.append("> **Asked you:** %s\n> **Your answer:** %s" % (q["text"], q.get("answer", "")))
            else:
                line = "🔧 " + b["summary"]
                if b.get("output"):
                    body = b["output"]
                    if b.get("output_truncated"):
                        body += "\n… (truncated)"
                    line += "\n\n```\n" + body + "\n```"
                out.append(line)
        elif b["t"] == "agent":
            out.append(_agent_md(b["agent"], depth))
    return "\n\n".join(x for x in out if x)


def _agent_md(a, depth):
    htok = IR.fmt_tokens(IR.headline_tokens(a["tokens"]))
    cost = IR.fmt_cost(a.get("cost_usd"))
    chip = htok + " tok" + ((" · " + cost) if cost else "")
    title = "%s — %s · %s" % (a.get("type", "agent"), a.get("desc", ""), chip)
    inner = []
    for t in a.get("turns", []):
        role = "You" if t["role"] == "user" else "Claude"
        inner.append("**%s:** %s" % (role, _blocks_md(t["blocks"], depth + 1)))
    return "<details>\n<summary>🤖 %s</summary>\n\n%s\n\n</details>" % (title, "\n\n".join(inner))


def render(ir):
    # IDE-aware provenance line (was hardcoded to "Claude Code"); consistent with
    # the HTML masthead label. Empty/unknown source_ide -> a generic phrasing.
    _name = _GH._ide_name(ir.get("source_ide"))
    _from = ("Exported from " + _name) if _name else "Exported from a coding session"
    lines = ["# " + ir["title"], "",
             "_%s — title source: %s · %d sub-agents_"
             % (_from, ir.get("title_source", ""), ir.get("agent_count", 0)), ""]
    n = 0
    i = 0
    turns = ir["turns"]
    while i < len(turns):
        t = turns[i]
        if t["role"] == "user":
            n += 1
            lines += ["# Round %d" % n, "## Prompt",
                      _blocks_md(t["blocks"], 0), ""]
            if i + 1 < len(turns) and turns[i + 1]["role"] == "assistant":
                lines += ["## Response", _blocks_md(turns[i + 1]["blocks"], 0), "", "---", ""]
                i += 2
                continue
            lines += ["---", ""]
        else:
            lines += ["## Response", _blocks_md(t["blocks"], 0), "", "---", ""]
        i += 1
    return "\n".join(lines)
