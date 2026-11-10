#!/usr/bin/env python3
"""
Export an AI coding session to a self-contained HTML page (default) — main
conversation plus every sub-agent's full transcript, rendered chat-style.

Pipeline (see the sibling modules):
  <collector>.collect()        reads an IDE's log store -> IR (ir.py)
  generator_html.render(IR)    IR -> one .html file

The collector is selected per target/environment through a small registry
(see COLLECTORS below), so the same entrypoint serves any IDE whose logs a
collector can turn into the frozen Session IR. Claude Code is the collector
that ships working today; others plug into the registry unchanged. Each
collector owns its own session-store location (platform-neutral) and its own
id -> transcript resolution — none of that lives here or in the SKILL prose.

Usage:
  export_session.py <jsonl | session-id> [--collector NAME] [--output-dir DIR]
                    [--name NAME] [--format html|md|both]
                    [--include-subagents | --no-subagents] [--max-output N] [--detect]

  --collector NAME      which IDE collector to use (default: claude-code, or the
                        ADK_EXPORT_COLLECTOR env var). Selects the log store and
                        the parser; the IR and generators are collector-agnostic.
  --detect              print a tiny summary of how many sub-agents exist, then exit
                        (the skill uses this to decide whether to ask the user).
  --include-subagents   embed sub-agent transcripts (default).
  --no-subagents        export the main conversation only; Task calls stay one-liners.

The invoking agent should NOT read the session log itself — this script prints
only a small summary so the agent's context stays clean.
"""

import argparse
import os
import sys
from pathlib import Path

# Windows consoles default to a legacy codepage; the summary is ASCII today but
# reconfigure defensively so any future non-ASCII summary line never crashes.
try:  # pragma: no cover - platform-dependent, no-op where already utf-8
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

import cc_parse as P
import collector_claude_code
import collector_devin
import collector_vscode_family
import generator_html
import generator_md


# ── Collector registry ───────────────────────────────────────────────
# Maps a collector name to the module that turns that IDE's log store into the
# frozen Session IR. A collector module must expose: detect(path),
# collect(path, include_subagents, max_output), store_root(), and
# resolve_session(arg). New IDEs register here — the IR and both generators are
# reused unchanged.
COLLECTORS = {}


def register_collector(name, module):
    """Register (or override) a collector module under `name`."""
    COLLECTORS[name] = module


register_collector(collector_claude_code.SOURCE_IDE, collector_claude_code)

# VS Code GitHub Copilot chat store (feasible capture target). Windsurf is NOT
# registered: it keeps chats in an opaque Cascade .pb store and its VS Code fork
# writes no chatSessions JSON (see the capture table in references/architecture.md).
# The VSCodeFamilyCollector is parameterized by product dir so a fork that DOES
# adopt the chatSessions JSON shape can be registered here.
register_collector("vscode",
                   collector_vscode_family.VSCodeFamilyCollector("vscode", "Code"))

# Devin's local desktop app writes an Agent Client Protocol (ACP) NDJSON event
# stream per session under <app-support>/Devin/User/acp-events/. That is a real,
# retrievable on-disk transcript (the earlier spike predated it), so Devin is a
# feasible LOCAL capture target — flat transcript, no token usage (cost "—").
register_collector(collector_devin.SOURCE_IDE, collector_devin)

DEFAULT_COLLECTOR = collector_claude_code.SOURCE_IDE


def select_collector(name=None):
    """Pick the collector for this run. Precedence: explicit --collector arg,
    then the ADK_EXPORT_COLLECTOR env var, then the default (claude-code — the
    proven, demoable path)."""
    name = name or os.environ.get("ADK_EXPORT_COLLECTOR") or DEFAULT_COLLECTOR
    module = COLLECTORS.get(name)
    if module is None:
        print("ERROR: unknown collector '%s' — available: %s"
              % (name, ", ".join(sorted(COLLECTORS))), file=sys.stderr)
        sys.exit(2)
    return module


def resolve_session(arg, collector):
    """Resolve a `.jsonl` path or bare session id via the selected collector's
    store logic. Exits with a clear message when nothing matches."""
    path = collector.resolve_session(arg)
    if path is None:
        print("ERROR: no session found for '%s' — not a file, and no match under %s/*/"
              % (arg, collector.store_root()), file=sys.stderr)
        sys.exit(2)
    return path


def main():
    ap = argparse.ArgumentParser(description="Export a coding session to HTML (or markdown).")
    ap.add_argument("jsonl", help="Path to the session .jsonl file, OR a bare session id "
                                  "(resolved under the selected collector's store)")
    ap.add_argument("--collector", default=None,
                    help="IDE collector to use (default: %s, or $ADK_EXPORT_COLLECTOR)"
                         % DEFAULT_COLLECTOR)
    ap.add_argument("--output-dir", default=os.getcwd())
    ap.add_argument("--name", default=None, help="Explicit filename (auto-increments on conflict)")
    ap.add_argument("--title", default=None,
                    help="Override the document title (else custom/ai title, else first prompt)")
    ap.add_argument("--format", choices=["html", "md", "both"], default="html")
    ap.add_argument("--include-subagents", dest="subagents", action="store_true", default=True)
    ap.add_argument("--no-subagents", dest="subagents", action="store_false")
    ap.add_argument("--max-output", type=int, default=4000,
                    help="Max chars of each tool output to embed (default 4000)")
    ap.add_argument("--detect", action="store_true",
                    help="Report sub-agent count and exit (no file written)")
    args = ap.parse_args()

    collector = select_collector(args.collector)
    jsonl = resolve_session(args.jsonl, collector)

    if args.detect:
        d = collector.detect(jsonl)
        print("subagents=%d" % d["count"])
        print("title=%s" % d.get("title", ""))
        print("title_source=%s" % d.get("title_source", ""))
        for a in d["agents"]:
            print("agent\t%s\t%s" % (a["type"], a["desc"]))
        return

    if not os.path.isdir(args.output_dir):
        print("ERROR: Output directory not found: %s" % args.output_dir, file=sys.stderr)
        sys.exit(2)

    ir = collector.collect(jsonl, include_subagents=args.subagents,
                           max_output=args.max_output)
    if args.title:
        ir["title"], ir["title_source"] = args.title, "provided"

    targets = ["html", "md"] if args.format == "both" else [args.format]
    written = []
    for fmt in targets:
        ext = ".html" if fmt == "html" else ".md"
        out_path = P.resolve_output_path(args.output_dir, args.name, ir["title"], ext)
        content = generator_html.render(ir) if fmt == "html" else generator_md.render(ir)
        Path(out_path).write_text(content, encoding="utf-8")
        written.append((fmt, out_path))

    for fmt, path in written:
        print("path=%s" % path)
        print("format=%s" % fmt)
        print("bytes=%d" % os.path.getsize(path))
    print("title=%s" % ir["title"])
    print("source=%s" % ir["title_source"])
    print("rounds=%d" % sum(1 for t in ir["turns"] if t["role"] == "user"))
    print("subagents=%d" % ir["agent_count"])


if __name__ == "__main__":
    main()
