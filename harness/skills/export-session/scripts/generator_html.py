#!/usr/bin/env python3
"""
HTML generator — Session IR (see ir.py) -> one self-contained, light,
chat-style HTML document.

Fully decoupled from any IDE: it only reads the IR. No external fonts, CDNs,
or assets (so it opens offline and pastes into email cleanly); native <details>
for collapse; a little vanilla JS for the filter box and expand/collapse-all.
"""

import re
from datetime import datetime

import ir as IR
import pricing  # shared, collector-agnostic pricing table + cost math

# Embedded brand assets (Source Sans Pro woff2 + the Agentic Studio marks).
# Optional: if the module is missing, the export still renders with system fonts
# and no mark. Two marks, because the export uses two size bands: the LOCKUP at
# 128px in the at-rest masthead (it carries the product name as vector paths, so
# the masthead names the product without the Raymond James wordmark), and the
# GLYPH at 36px in the condensed pinned bar, where the lockup's in-disc name
# would render at a 2.3px cap height. See tools/brand-kit/README.md.
try:
    import brand_assets as _BRAND
    FONT_CSS = _BRAND.FONT_CSS
    PRODUCT_MARK_SVG = _BRAND.PRODUCT_MARK_SVG
    PRODUCT_GLYPH_SVG = _BRAND.PRODUCT_GLYPH_SVG
except Exception:
    FONT_CSS = ""
    PRODUCT_MARK_SVG = ""
    PRODUCT_GLYPH_SVG = ""

PRODUCT_NAME = "Agentic Studio"

ROLE_COLORS = [
    ("analyst",  "#eeedfe", "#3c3489", "AN"),
    ("product",  "#eeedfe", "#3c3489", "PA"),
    ("design",   "#eeedfe", "#3c3489", "UX"),
    ("backend",  "#e1f5ee", "#085041", "BE"),
    ("frontend", "#e6f1fb", "#0c447c", "FE"),
    ("database", "#faeeda", "#633806", "DB"),
    ("test",     "#eaf3de", "#27500a", "QA"),
    ("review",   "#fbeaf0", "#72243e", "RV"),
    ("devops",   "#faece7", "#712b13", "OP"),
    ("triage",   "#fcebeb", "#791f1f", "TR"),
    ("explore",  "#f1efe8", "#5f5e5a", "EX"),
]
_DEFAULT_COLOR = ("#f1efe8", "#5f5e5a")


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _inline(s):
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", s)
    s = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"<em>\1</em>", s)
    return s


def md_to_html(text):
    codes = []

    def grab(m):
        codes.append(m.group(1))
        return "\x00CODE%d\x00" % (len(codes) - 1)

    text = re.sub(r"```[^\n]*\n(.*?)```", grab, text, flags=re.S)
    text = esc(text)
    out, para, items, ltype = [], [], [], None

    def flush_para():
        if para:
            out.append("<p>" + "<br>".join(_inline(x) for x in para) + "</p>")
            para.clear()

    def flush_list():
        nonlocal items, ltype
        if items:
            out.append("<%s>%s</%s>" % (ltype, "".join("<li>%s</li>" % _inline(i) for i in items), ltype))
            items, ltype = [], None

    def cells(line):
        line = line.strip()
        if line.startswith("|"):
            line = line[1:]
        if line.endswith("|"):
            line = line[:-1]
        return [c.strip() for c in line.split("|")]

    lines = text.split("\n")
    i, n = 0, len(lines)
    while i < n:
        line = lines[i].rstrip()
        ph = line.strip()
        if re.fullmatch(r"\x00CODE\d+\x00", ph):
            flush_para(); flush_list()
            out.append("<pre><code>%s</code></pre>" % esc(codes[int(ph[5:-1])]))
            i += 1; continue
        if not line.strip():
            flush_para(); flush_list(); i += 1; continue
        # GFM table: a header row followed by a |---|---| separator row
        if "|" in line and i + 1 < n and re.fullmatch(
                r"\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*", lines[i + 1]):
            flush_para(); flush_list()
            head = cells(line)
            rows = []
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(cells(lines[i])); i += 1
            thead = "".join("<th>%s</th>" % _inline(c) for c in head)
            tbody = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % _inline(c) for c in r) for r in rows)
            out.append('<div class="tw"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>'
                       % (thead, tbody))
            continue
        h = re.match(r"(#{1,6})\s+(.*)", line)
        if h:
            flush_para(); flush_list()
            lvl = min(len(h.group(1)) + 2, 6)
            out.append("<h%d>%s</h%d>" % (lvl, _inline(h.group(2)), lvl))
            i += 1; continue
        ul = re.match(r"[-*]\s+(.*)", line)
        ol = re.match(r"\d+\.\s+(.*)", line)
        if ul:
            flush_para()
            if ltype != "ul":
                flush_list(); ltype = "ul"
            items.append(ul.group(1)); i += 1; continue
        if ol:
            flush_para()
            if ltype != "ol":
                flush_list(); ltype = "ol"
            items.append(ol.group(1)); i += 1; continue
        flush_list(); para.append(line); i += 1
    flush_para(); flush_list()
    return "".join(out)


def _role(agent_type):
    t = (agent_type or "").lower()
    for kw, bg, fg, ini in ROLE_COLORS:
        if kw in t:
            return bg, fg, ini
    seg = re.sub(r"[^a-z]", "", t.split("-")[-1])[:2].upper() or "AG"
    return _DEFAULT_COLOR[0], _DEFAULT_COLOR[1], seg


def _fmt_time(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%H:%M")
    except Exception:
        return ""


def _epoch(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _ran_in_parallel(agents):
    """True only if at least two agents' [start, end] run windows actually
    overlap — so we never claim 'parallel' just because cards are adjacent."""
    spans = []
    for a in agents:
        s = _epoch(a.get("ts"))
        if s is None:
            continue
        spans.append((s, s + (a.get("duration_s") or 0)))
    spans.sort()
    for i in range(1, len(spans)):
        if spans[i][0] < spans[i - 1][1]:
            return True
    return False


def _tool(blk):
    q = blk.get("question")
    if q:
        h = ['<div class="q"><div class="qh">You were asked</div>',
             '<div class="qt">%s</div>' % esc(q["text"])]
        if q.get("options"):
            h.append('<div class="qo">Options: %s</div>' % esc(q["options"]))
        if q.get("answer"):
            h.append('<div class="qa">Your answer: %s</div>' % esc(q["answer"]))
        h.append("</div>")
        return "".join(h)
    summary = esc(blk["summary"])
    inp = blk.get("input")
    out = blk.get("output")
    if inp is None and out is None and not blk.get("offloaded_path"):
        return '<div class="tool"><span class="tk">&#187;</span> %s</div>' % summary
    sections = []
    if inp is not None:
        body = esc(inp)
        if blk.get("input_truncated"):
            body += "\n… (input truncated)"
        sections.append('<div class="iolabel">input</div><pre>%s</pre>' % body)
    if out is not None or blk.get("offloaded_path"):
        body = esc(out or "")
        if blk.get("output_truncated"):
            body += "\n… (output truncated)"
        note = ""
        if blk.get("offloaded_path"):
            note = '<div class="onote">full output saved to %s</div>' % esc(blk["offloaded_path"])
        sections.append('<div class="iolabel">output</div><pre>%s</pre>%s' % (body, note))
    return ('<details class="out"><summary><span class="tk">&#187;</span> %s</summary>'
            '%s</details>') % (summary, "".join(sections))


def _agent(a):
    bg, fg, ini = _role(a.get("type"))
    head = a.get("type", "agent")
    desc = a.get("desc", "")
    htok = IR.headline_tokens(a["tokens"])
    chip = IR.fmt_tokens(htok) + " tok"
    cost = IR.fmt_cost(a.get("cost_usd"))
    if cost:
        chip += " · " + cost
    t = _fmt_time(a.get("ts"))
    dur = IR.fmt_duration(a.get("duration_s"))
    when = " · ".join(x for x in (t, dur) if x)
    body = _turns(a.get("turns", []))
    return (
        '<details class="agent" id="%s">'
        '<summary class="ah">'
        '<span class="ico" style="background:%s;color:%s;">%s</span>'
        '<span class="amain"><span class="atag">Sub-agent</span>'
        '<span class="atype" style="color:%s;">%s</span>'
        '<span class="adesc">%s</span></span>'
        '<span class="tok">%s</span><span class="when">%s</span>'
        '<span class="chev">&#9662;</span>'
        '</summary><div class="ab">%s</div></details>'
    ) % (esc(a.get("_ref") or ""), bg, fg, esc(ini), fg, esc(head),
         (" · " + esc(desc)) if desc else "", esc(chip), esc(when), body)


def _assistant_blocks(blocks):
    parts, text_buf, agent_run = [], [], []

    def flush_text():
        if text_buf:
            parts.append('<div class="bub">%s</div>' % md_to_html("\n\n".join(text_buf)))
            text_buf.clear()

    def flush_agents():
        if agent_run:
            if len(agent_run) > 1:
                inner = "".join(_agent(a) for a in agent_run)
                label = ("%d agents in parallel" if _ran_in_parallel(agent_run)
                         else "%d sub-agents") % len(agent_run)
                parts.append('<div class="par"><span class="plbl">%s</span>%s</div>'
                             % (label, inner))
            else:
                parts.append(_agent(agent_run[0]))
            agent_run.clear()

    for blk in blocks:
        if blk["t"] == "text":
            flush_agents(); text_buf.append(blk["text"])
        elif blk["t"] == "tool":
            flush_agents(); flush_text(); parts.append(_tool(blk))
        elif blk["t"] == "agent":
            flush_text(); agent_run.append(blk["agent"])
    flush_text(); flush_agents()
    return "".join(parts)


def _tasknote(text, ts):
    """A background-task notification is injected as a user-role message, but it is a
    SYSTEM event, not something the user typed — render it as an understated notice."""
    summ = re.search(r"<summary>(.*?)</summary>", text, re.S)
    title = summ.group(1).strip() if summ else "Background task"
    res = re.search(r"<result>(.*?)</result>", text, re.S)
    if res:
        body = res.group(1).strip()
    else:  # strip the metadata envelope, keep whatever prose remains
        body = re.sub(r"</?task-notification>|<(task-id|tool-use-id|output-file|status|note|summary)>.*?</\1>",
                      "", text, flags=re.S).strip()
    when = (" · " + ts) if ts else ""
    body_html = ('<div class="bub snb">%s</div>' % md_to_html(body)) if body else ""
    return ('<div class="turn sysnote"><div class="sn">'
            '<div class="snh"><span class="sntag">Background task</span>%s'
            '<span class="snwhen">%s</span></div>%s</div></div>') % (
        esc(title), esc(when), body_html)


def _turns(turns):
    out = []
    for t in turns:
        ts = _fmt_time(t.get("ts"))
        if t["role"] == "user":
            txt = "\n\n".join(b["text"] for b in t["blocks"] if b["t"] == "text")
            if not t.get("is_summary") and txt.lstrip().startswith("<task-notification>"):
                out.append(_tasknote(txt, ts))
                continue
            cls = "turn user summary" if t.get("is_summary") else "turn user"
            label = (ts + " · resumed summary") if t.get("is_summary") else ("you" + (" · " + ts if ts else ""))
            out.append('<div class="%s"><div class="col"><div class="stamp">%s</div>'
                       '<div class="bub">%s</div></div></div>' % (cls, esc(label), md_to_html(txt)))
        else:
            label = "Claude" + (" · " + ts if ts else "")
            out.append('<div class="turn claude"><div class="col"><div class="stamp">%s</div>%s</div></div>'
                       % (esc(label), _assistant_blocks(t["blocks"])))
    return "".join(out)


_CSS = """
:root{
 --navy:#002949;--navy2:#1a3c5e;--steel:#3d7ca9;--neptune:#8cc2ce;
 --slate:#708090;--slate-d:#434d56;--slate-m:#c6ccd3;--slate-l:#f1f1f1;
 --ink:#002949;--body:#2b333c;--rule:#e4e7ec;
 --font:'Source Sans Pro','Segoe UI',Helvetica,Arial,sans-serif;
 --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
 /* Height of the condensed pinned bar. Declared once: .cond sizes itself from
    it, cancels itself out of flow with the negative of it, and the filter bar
    pins below it at exactly it. --cond-top parks .cond's at-rest slot clear of
    the card's 15px corner curve so it can stay square-cornered; see .cond. */
 --cond-h:44px;--cond-top:16px;
 /* Block widths, as a share of the card's text column. The requester set these
    four numbers directly, choosing DENSITY over a geometric separation
    guarantee: left + right is 120%, so a long assistant turn and a long user
    turn can overlap horizontally. What tells the two speakers apart is the
    alignment plus the 20-point width difference, not a guaranteed channel.
      left   assistant turns, flush left
      right  user turns, flush right
      tool   tool invocations and their output
      common shared, non-speaker blocks: the asked-question format, background
             task notices, and sub-agent containers */
 --w-left:70%;--w-right:50%;--w-tool:70%;--w-common:100%;
 /* Prose measure — a cap on the TEXT, not on any block. Blocks keep their
    --w-* widths above; this only stops a line of prose running the whole way
    across one. 82ch is ~90 rendered characters in Source Sans Pro (1ch is
    7.21px at 14.5px, an average character 6.53px), the top of the comfortable
    45-90 band. It is in `ch` rather than px so it stays ~90 characters in the
    smaller type used by the question block too.
    CODE IS DELIBERATELY EXCLUDED. Fenced blocks are emitted as top-level <pre>
    siblings of <p>, never inside one, so this cannot reach them; pre keeps
    overflow:auto and scrolls instead of wrapping. Tables are siblings too. */
 --measure:82ch;
}
*{box-sizing:border-box}
/* LIGHT THEME IS MANDATED. This document has no dark palette and must not be
   given one by the browser: color-scheme:light stops a dark OS darkening
   scrollbars and form controls, the one route by which dark leaks into an
   otherwise light page. Both surfaces state their background explicitly (the
   desk is grey, the document on it is white — that IS the letterhead design).
   tests/_check_structural.py FAILS the build if a prefers-color-scheme:dark
   block ever appears in this template or in the viewer CSS. */
:root{color-scheme:light}
html{background:#eef1f4}
body{margin:0;background:#eef1f4;color:var(--body);font-family:var(--font);line-height:1.55;-webkit-font-smoothing:antialiased}
/* No max-width: the card takes the full available page width, less a modest
   gutter (the 18px padding). Block widths are governed per element type by the
   --w-* custom properties above, not by capping the page. */
.wrap{margin:0 auto;padding:26px 18px 56px}
/* .doc must NOT establish a scroll container (no overflow hidden/auto/scroll/clip).
   .bar below is position:sticky, and its scrollport is the nearest scrolling ancestor.
   Give .doc an overflow that clips and .doc becomes that scrollport -- but .doc is
   auto-height and never scrolls, so the bar silently stops pinning and just rides up
   with the page. The masthead therefore rounds its own top corners rather than relying
   on the parent to clip them (15px = .doc's 16px radius less its 1px border, which is
   exactly the padding-box curve the old overflow:hidden clipped to). */
.doc{background:#fff;border:1px solid var(--rule);border-radius:16px}
/* The masthead is LETTERHEAD, not UI chrome: brand presence on a document that
   leaves the organisation. That is why it carries the 128px lockup — the form
   the approved family reserves for the 512-96px brand-presence band — rather
   than the glyph a chrome slot would take. The Raymond James wordmark is gone
   (D-USER-06); attribution survives in the document footer. The mark sits on
   the RIGHT deliberately: that is the light end of the 115deg gradient
   (#1a3c5e), where the mark's #002949 keyline still has contrast. Moved left it
   would sit on #002949 and the keyline would disappear.
   .mast is positioned so it paints over the condensed bar parked behind it. */
.mast{position:relative;z-index:6;background:linear-gradient(115deg,#002949 0%,#0a3358 55%,#1a3c5e 100%);color:#fff;padding:22px 26px 14px;border-radius:15px 15px 0 0}
.mast .mrow{display:flex;align-items:center;gap:28px}
.mast .mtext{flex:1;min-width:0}
.mast .mark{flex:none;line-height:0}
.mast .mark svg{width:128px;height:128px;display:block}
/* Narrow viewports stack instead of shrinking the mark: the lockup's product
   name is set inside the disc, so below ~96px it stops being words. */
@media(max-width:560px){.mast .mrow{flex-direction:column-reverse;align-items:flex-start;gap:14px}.mast .mtext{flex:none;width:100%}}
.mast .eyebrow{font-size:10px;letter-spacing:.22em;text-transform:uppercase;color:var(--neptune);font-weight:700}
.mast h1{font-size:23px;font-weight:700;margin:5px 0 4px;color:#fff;letter-spacing:-.01em}
.mast .meta{font-size:12px;color:#aebfd0}
.mast .sid{font-size:11px;color:#8ea4bb;margin-top:6px;font-family:var(--mono);letter-spacing:.01em}
.pad{padding:20px 26px 28px}
.mcards{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:12px}
.mcard{background:#f7f9fb;border:1px solid var(--rule);border-radius:10px;padding:11px 12px}
.mcard .lbl{font-size:9px;letter-spacing:.12em;text-transform:uppercase;color:var(--steel);font-weight:700}
.mcard .val{font-size:20px;font-weight:700;color:var(--navy);line-height:1.1;margin-top:3px}
.mcard .sub{font-size:9.5px;color:var(--slate);margin-top:1px}
@media(max-width:720px){.mcards{grid-template-columns:repeat(3,1fr)}}
.mstrip{display:flex;flex-wrap:wrap;gap:6px;margin:2px 0 16px}
.chip{font-size:11px;color:var(--slate-d);background:var(--slate-l);border-radius:7px;padding:3px 9px}
.chip b{color:var(--navy);font-weight:700}
.osec{margin:8px 0 16px}
.oh{font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:var(--steel);font-weight:700;margin:2px 0 8px}
.osec .mcards{grid-template-columns:repeat(4,1fr)}
@media(max-width:720px){.osec .mcards{grid-template-columns:repeat(2,1fr)}}
.bd{margin:4px 0 18px}
.bd .h{font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:var(--steel);font-weight:700;margin-bottom:7px}
.bd table{width:100%;border-collapse:collapse;font-size:12.5px}
.bd th{background:var(--navy);color:#fff;text-align:left;padding:6px 9px;font-size:8.5px;letter-spacing:.06em;text-transform:uppercase;font-weight:700}
.bd th.n,.bd td.n{text-align:right}
.bd td{padding:6px 9px;border-bottom:1px solid var(--rule);vertical-align:top}
.bd td.name{font-weight:700;color:var(--ink)}
.bd tr.mainrow td{background:#eef4fb}
.bd tr.mainrow td.name{color:var(--navy)}
.bd tr.mainrow td.name::after{content:' · this transcript';font-weight:400;color:var(--slate);font-size:10.5px}
.bd .more{font-size:11px;color:var(--slate);margin-top:5px}
/* ---- usage dashboard (approved format; same design as Backstage's Usage tab) ---- */
/* Approved statistic format: figure in steel blue, label in navy, caption in
   slate. All four equal — no hero; total cost is already in the session row. */
/* Four equal columns across the full width, each left-aligned in its column —
   the strips and table below run edge to edge. Layout only; treatment unchanged. */
.usg-metrics{display:grid;grid-template-columns:repeat(5,1fr);gap:18px;margin:6px 0 22px}
.usg-m{min-width:0}
.usg-m .v{font-size:26px;font-weight:700;color:var(--steel);line-height:1.05;font-variant-numeric:tabular-nums}
.usg-m .k{font-size:9.5px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--navy);margin-top:7px}
.usg-m .c{font-size:10px;color:var(--slate);margin-top:2px}
.usg-bar{display:flex;gap:2px;width:100%;height:26px;margin:0 0 20px;overflow:hidden}
/* min-width keeps a sliver hoverable rather than vanishing — the title is all
   it has. */
.usg-bar .seg{border-radius:3px;overflow:hidden;display:flex;align-items:center;min-width:3px}
/* Colour is set per segment from its tint, verified against WCAG 4.5:1. */
.usg-bar .seg .lbl{font-size:10.5px;white-space:nowrap;padding:0 8px}
.usg{margin:4px 0 18px}
table.usg-t{width:100%;border-collapse:collapse;font-size:12.5px}
/* Every horizontal rule belongs to its ROW, never to the cells — under
   border-collapse a tr border paints one continuous line, so no combination of
   cell heights can step it. The flex sits on an inner wrapper so every cell
   stays a real table-cell box (a display:flex td takes its height from its own
   content, which stepped the session rule by 3px). */
table.usg-t th{text-align:left;font-size:9px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--slate);padding:0 10px 6px;white-space:nowrap}
table.usg-t thead tr{border-bottom:1px solid var(--rule)}
table.usg-t th.n,table.usg-t td.n{text-align:right;font-variant-numeric:tabular-nums}
table.usg-t td{padding:9px 10px;vertical-align:middle}
table.usg-t tbody tr{border-bottom:1px solid var(--rule)}
table.usg-t tr.usg-session{border-bottom:2px solid var(--navy)}
table.usg-t tr.usg-session td{font-weight:700;color:var(--navy)}
table.usg-t tr.usg-session td.work{font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--slate)}
table.usg-t td.work > .cell{display:flex;align-items:center;gap:9px}
table.usg-t td.work.det > .cell{padding-left:30px}
table.usg-t td.work.det{box-shadow:inset 2px 0 0 var(--steel)}
table.usg-t .ico{flex:none;line-height:0}
table.usg-t .txt{display:flex;flex-direction:column;min-width:0;flex:1 1 auto}
table.usg-t .nm{font-size:13.5px;font-weight:600;color:var(--navy)}
/* Wraps, never clips: it carries the model name, the fact that makes the row
   recomputable. The row grows a line rather than hiding a character. */
table.usg-t .meta{font-size:10.5px;color:var(--slate);white-space:normal;overflow-wrap:anywhere}
table.usg-t tr.usg-det{background:#fbfcfe}
table.usg-t tr.usg-det .nm{font-size:12px}
table.usg-t tr.usg-det .meta{font-size:10px}
table.usg-t tr.usg-det.more .meta{font-style:italic}
table.usg-t a.jump{color:var(--navy);text-decoration:none}
table.usg-t a.jump:hover{color:var(--steel);text-decoration:underline}
/* The breakdown is a native disclosure: reachable by click in any browser, with
   no script, and never reachable ONLY by hovering. */
details.lx-d{display:inline}
details.lx-d>summary{cursor:pointer;color:var(--steel);list-style:none;text-decoration:underline;text-decoration-style:dotted;text-underline-offset:3px}
details.lx-d>summary::-webkit-details-marker{display:none}
.lx-wrap{display:flex;flex-wrap:wrap;gap:4px 16px;justify-content:flex-end;margin-top:5px}
.lx{display:flex;align-items:center;gap:6px;font-size:9.5px;color:var(--slate-d)}
.lx-e{min-width:38px;color:var(--slate);text-align:left}
.lx-bar{width:60px;height:5px;background:#e3eaf0;border-radius:3px;overflow:hidden}
.lx-bar>span{display:block;height:100%;background:var(--steel)}
.lx-n{font-variant-numeric:tabular-nums;min-width:40px;text-align:right}
.usg-note{font-size:10.5px;color:var(--slate);margin-top:8px;line-height:1.6}
.bar{position:sticky;top:0;background:#fff;display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:8px 0;margin:4px 0;border-bottom:1px solid var(--rule);z-index:5}
/* Condensed pinned header + filter bar pin as one block.
   The full masthead is NOT pinned: at 128px it costs 28.1% of an 800px viewport
   with the bar stacked under it, and grows another 35.6px per wrapped title
   line. The condensed bar costs a flat 12.1% at any title length, which is the
   trade the requester chose.
   The reveal is CSS-only, no scroll listener and no stuck-detection: .cond is
   placed immediately BEFORE .mast and cancels itself out of flow with a
   negative bottom margin, so it occupies no layout space and .mast — which
   follows at a higher z-index — covers it completely at rest. As the page
   scrolls .mast rides away and uncovers the already-pinned .cond, a wipe that
   falls out of paint order alone.
   The --cond-top offset is why .cond can keep SQUARE corners. Its at-rest slot
   starts just below the card's 15px corner curve, so it never paints into the
   corners the masthead leaves transparent; sticky then pulls it to top:0, where
   square is what you want. Rounding it instead — the obvious move, since it is
   standing in for the masthead — puts a 15px white notch in each top corner of
   the pinned bar, because once the card's own rounded top has scrolled away it
   is .doc's white sitting behind the curve, not the page grey.
   Stickiness is gated on .pin, which the script adds, so where scripting does
   not run the export degrades to a masthead that scrolls away and a bar that
   pins at 0. Anchor targets take their scroll-margin from --pin-h (.cond +
   .bar, both MEASURED — the filter bar wraps on a narrow viewport) so a
   breakdown jump link cannot land underneath the pinned block. */
.cond{position:relative;z-index:5;height:var(--cond-h);margin-top:var(--cond-top);margin-bottom:calc(-1 * (var(--cond-h) + var(--cond-top)));display:flex;align-items:center;gap:11px;padding:0 26px;color:#fff;background:linear-gradient(115deg,#002949 0%,#0a3358 55%,#1a3c5e 100%)}
/* BR-01: no room for the glyph's floor size plus a name inside the disc, so the
   product name is live text beside the glyph, never a shrunken lockup. The two
   are one lockup-in-layout: mark, then the name at the eyebrow's tracking. */
.cond .cmark{flex:none;line-height:0}
.cond .cmark svg{width:36px;height:36px;display:block}
.cond .cname{flex:none;font-size:11px;font-weight:700;letter-spacing:.15em;text-transform:uppercase;white-space:nowrap}
.cond .ct{font-size:13px;font-weight:700;letter-spacing:-.01em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;padding-left:13px;border-left:1px solid rgba(255,255,255,.24)}
.cond .cmeta{margin-left:auto;padding-left:14px;font-size:10.5px;color:#aebfd0;white-space:nowrap}
@media(max-width:720px){.cond .cname,.cond .cmeta{display:none}.cond .ct{padding-left:0;border-left:0}}
.doc.pin .cond{position:sticky;top:0}
.doc.pin .bar{top:var(--cond-h)}
.doc.pin details.agent{scroll-margin-top:calc(var(--pin-h) + 12px)}
.f{flex:1;min-width:170px;height:36px;padding:0 12px;font-size:14px;border:1px solid var(--slate-m);border-radius:9px;background:#fbfcfd;color:var(--body);font-family:var(--font)}
.btn{font-size:13px;padding:7px 12px;border-radius:9px;border:1px solid var(--slate-m);background:#fff;color:var(--navy);cursor:pointer;font-family:var(--font)}
.btn:hover{background:var(--slate-l)}
.sum{font-size:12.5px;color:var(--slate);margin-left:auto}
.turn{display:flex;margin-top:16px}
.turn.user{justify-content:flex-end}
.turn .col{max-width:84%}
/* Left is the assistant (.turn.claude, flush left by the flex default); right is
   the user (.turn.user, justify-content:flex-end). Verified against the rendered
   document, not inferred from the class names. */
.turn.user .col{max-width:var(--w-right);text-align:right}
.turn.claude .col{max-width:var(--w-common)}
.turn.claude .bub{max-width:var(--w-left)}
/* On a phone there is no room for two bands, and the caps would leave a user
   turn 150px wide running at 20 characters a line. Below the breakpoint both
   speakers take the full column; the desktop numbers above are untouched. */
@media(max-width:720px){.turn.claude .bub,.turn.user .col{max-width:var(--w-common)}}
.stamp{font-size:11px;color:var(--slate);margin:0 4px 3px}
.bub{display:inline-block;text-align:left;padding:10px 14px;border-radius:14px;font-size:14.5px}
.turn.user .bub{background:#e6f1fb;color:var(--navy);border-bottom-right-radius:5px}
.turn.claude .bub{background:var(--slate-l);color:var(--body);border-bottom-left-radius:5px}
.turn.summary .bub{background:#eaeef3;color:var(--slate-d);font-style:italic}
/* PROSE ONLY. p and li inside a bubble (which includes the background-task
   notice body, .bub.snb), plus the asked-question block's own text. Everything
   else is left alone on purpose: pre and code scroll, tables scroll in .tw,
   tool invocation lines and output summaries are tool usage not prose, and the
   summary header, breakdown table, metric cards, masthead and condensed bar
   are furniture. Adding pre or .tw here would make code wrap. */
.bub p,.bub li,.q .qt,.q .qo,.q .qa{max-width:var(--measure)}
.bub p{margin:0 0 8px}.bub p:last-child{margin:0}
.bub h3,.bub h4,.bub h5{font-size:15px;font-weight:700;margin:10px 0 4px;color:var(--ink)}
.bub code{font-family:var(--mono);font-size:12.5px;background:#e7ebef;padding:1px 5px;border-radius:5px}
.bub pre{background:#011c2d;color:#e6edf3;padding:10px 12px;border-radius:9px;overflow:auto;font-size:12.5px}
.bub pre code{background:none;padding:0;color:inherit}
.bub a{color:var(--steel)}
.tw{overflow-x:auto;margin:8px 0}
.bub table{border-collapse:collapse;font-size:13.5px}
.bub th,.bub td{border:1px solid var(--slate-m);padding:5px 9px;text-align:left;vertical-align:top}
.bub th{background:var(--slate-l);font-weight:700}
/* Tool usage — the invocation line and the collapsible output — is capped at
   --w-tool. It sits in .col, not in .bub, so it needs its own cap; without one
   it inherits the container and runs the full card width. The <pre> inside
   keeps overflow:auto, so long output still scrolls within the capped block
   rather than widening it. */
.tool{font-family:var(--mono);font-size:12px;color:var(--slate);margin-top:5px;max-width:var(--w-tool)}
.tool .tk,.out .tk{color:var(--slate-m)}
details.out{margin-top:5px;max-width:var(--w-tool)}
details.out>summary{font-family:var(--mono);font-size:12px;color:var(--slate-d);cursor:pointer;list-style:none}
details.out>summary::-webkit-details-marker{display:none}
details.out pre{background:var(--slate-l);border:1px solid var(--rule);border-radius:8px;padding:8px 10px;margin:5px 0 0;overflow:auto;font-size:12px;color:var(--slate-d);max-height:340px}
.onote{font-size:11px;color:var(--slate);margin-top:3px}
.iolabel{font-size:10px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--slate-m);margin-top:8px}
.iolabel:first-of-type{margin-top:5px}
/* Common blocks — not attributable to one speaker — stay full width:
   sub-agent containers (details.agent, .par), the background-task notice (.sn)
   and the asked-question format (.q). Inside a sub-agent the nested turns and
   tool blocks take --w-left / --w-right / --w-tool again, of that container. */
/* 64px is the no-pinning fallback — .doc.pin overrides it above with measured --pin-h. */
details.agent{max-width:var(--w-common);border:1px solid #dfe4ea;border-left:4px solid var(--navy);border-radius:10px;background:#fbfcfe;margin:11px 0;overflow:hidden;scroll-margin-top:64px}
.bd a.jump{color:var(--navy);text-decoration:none}
.bd a.jump:hover{color:var(--steel);text-decoration:underline}
.bd tr:hover td{background:#f4f8fc}
details.agent>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:10px;padding:11px 13px;background:#eef1f5}
details.agent>summary::-webkit-details-marker{display:none}
details.agent>summary:hover{background:#e4e9f0}
.atag{display:block;font-size:8.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--navy);font-weight:700;margin-bottom:1px}
.ico{width:26px;height:26px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;flex:none}
.amain{flex:1;min-width:0}
.atype{font-size:12.5px;font-weight:700;color:var(--navy)}
.adesc{font-size:12.5px;color:var(--slate-d)}
.tok{font-size:11.5px;color:var(--slate-d);background:var(--slate-l);padding:2px 8px;border-radius:7px;white-space:nowrap}
.when{font-size:11.5px;color:var(--slate);white-space:nowrap}
.chev{color:var(--slate-m);font-size:12px;transition:transform .15s}
details.agent[open]>summary .chev{transform:rotate(180deg)}
.ab{padding:2px 14px 14px;margin:6px 12px 12px 36px;border-left:2px solid #dbe0e7}
.par{max-width:var(--w-common);border:1px solid #dfe4ea;border-left:4px solid var(--navy);border-radius:10px;background:#f4f6f9;padding:9px 12px 12px;margin:11px 0}
.par details.agent{margin:8px 0;background:#fff;border-color:var(--rule);border-left:3px solid var(--navy)}
.par details.agent>summary{background:#fafbfd}
.par details.agent>summary:hover{background:#f1f4f8}
.plbl{display:inline-block;font-size:10px;letter-spacing:.1em;text-transform:uppercase;font-weight:700;color:#fff;background:var(--navy);padding:3px 10px;border-radius:7px;margin:0 0 6px}
/* System / background-task notice — understated, not a chat bubble */
.turn.sysnote{display:block}
.sn{max-width:var(--w-common);border:1px solid #dfe3e8;border-left:3px solid var(--slate);border-radius:9px;background:#f2f4f6;padding:9px 13px;margin-top:14px}
.snh{font-size:12px;color:var(--slate-d);font-weight:600}
.sntag{display:inline-block;font-size:8.5px;letter-spacing:.13em;text-transform:uppercase;color:#fff;background:var(--slate);padding:2px 7px;border-radius:6px;margin-right:8px;vertical-align:middle;font-weight:700}
.snwhen{color:var(--slate);font-weight:400;font-size:10.5px;margin-left:8px}
.snb{display:block;background:transparent;padding:0;margin-top:8px;font-size:13.5px;border-radius:0}
.q{max-width:var(--w-common);background:#f4f7fb;border:1px solid #dbe6f1;border-radius:9px;padding:8px 11px;margin-top:6px}
.qh{font-size:10.5px;font-weight:700;color:var(--steel);letter-spacing:.03em;text-transform:uppercase}
.qt{font-size:13.5px;margin-top:2px}.qo{font-size:12px;color:var(--slate);margin-top:2px}.qa{font-size:13px;color:#1f6a4a;margin-top:3px}
.hidden{display:none!important}
.foot{font-size:11px;color:var(--slate);text-align:center;margin-top:18px}
.foot b{color:var(--slate-d)}
"""

_JS = """
(function(){
var root=document.getElementById('doc');
var agents=[].slice.call(root.querySelectorAll('details.agent'));
var turns=[].slice.call(root.querySelectorAll('.turn'));
var flt=document.getElementById('flt');
// Enable the pinned block (condensed header + filter bar) and publish its real
// height. --cond-h is a declared constant in the stylesheet; --pin-h is not,
// because the filter bar wraps to a second line on a narrow viewport. Anchor
// scroll-margin reads --pin-h, so a stale value drops a jump-link target
// underneath the pinned block -- measure it, never hardcode it.
var cond=root.querySelector('.cond'),bar=root.querySelector('.bar');
if(cond&&bar){
  var sizePins=function(){
    root.style.setProperty('--pin-h',(cond.getBoundingClientRect().height
      +bar.getBoundingClientRect().height)+'px');
  };
  root.classList.add('pin');
  sizePins();
  if(window.ResizeObserver){var ro=new ResizeObserver(sizePins);ro.observe(cond);ro.observe(bar);}
  else{window.addEventListener('resize',sizePins);}
}
document.getElementById('ex').onclick=function(){agents.forEach(function(a){a.open=true;});};
document.getElementById('col').onclick=function(){agents.forEach(function(a){a.open=false;});};
flt.addEventListener('input',function(){
  var q=flt.value.trim().toLowerCase();
  turns.forEach(function(t){t.classList.toggle('hidden',!!q&&t.textContent.toLowerCase().indexOf(q)<0);});
  if(q)agents.forEach(function(a){if(a.textContent.toLowerCase().indexOf(q)>=0)a.open=true;});
});
// Deep-link: clicking a breakdown row opens that sub-agent (and any parent) and scrolls to it.
function reveal(){
  var h=location.hash; if(!h||h.length<2)return;
  var el; try{el=document.querySelector(h);}catch(e){return;}
  if(!el)return;
  var p=el; while(p){if(p.tagName==='DETAILS')p.open=true; p=p.parentElement;}
  el.scrollIntoView({block:'start'});
}
window.addEventListener('hashchange',reveal);
[].forEach.call(document.querySelectorAll('a.jump'),function(a){
  a.addEventListener('click',function(){setTimeout(reveal,0);});
});
if(location.hash)reveal();
})();
"""


def _session_cost(ir):
    """This transcript's own cost, priced PER ENTRY at the model that entry used.

    Pricing the aggregate at one model is wrong whenever the session switched
    models, which sessions routinely do — on one real 22MB transcript that
    overstated the main thread by 18.9%. The attribution pass already prices
    every entry at its own model, so the honest total is the sum of its
    main-thread detail rows. An IR from an older collector has no attribution;
    it falls back to the single-model estimate rather than reporting nothing."""
    wu = ir.get("workflow_usage")
    if wu:
        costs = [d["cost_usd"] for seg in wu.get("segments") or []
                 for d in seg.get("detail") or []
                 if d.get("kind") != "agent" and d.get("cost_usd") is not None]
        if costs:
            return sum(costs)
        # No priced main-thread rows at all (an unknown model everywhere) — say
        # unknown rather than falling back to a number built the wrong way.
        if wu.get("segments"):
            return None
    return pricing._cost(ir.get("model"), ir["tokens"])


def _duration_s(ir):
    s, e = _epoch(ir.get("started")), _epoch(ir.get("ended"))
    if s is not None and e is not None and e >= s:
        return e - s
    return None


def _flatten_agents(turns, acc=None):
    """All sub-agents anywhere in the tree (top-level + nested), for the breakdown."""
    if acc is None:
        acc = []
    for t in turns:
        for b in t.get("blocks", []):
            if b.get("t") == "agent":
                a = b["agent"]
                acc.append(a)
                _flatten_agents(a.get("turns", []), acc)
    return acc


def _card(lbl, val, sub=""):
    s = ('<div class="sub">%s</div>' % esc(sub)) if sub else ""
    return '<div class="mcard"><div class="lbl">%s</div><div class="val">%s</div>%s</div>' % (
        esc(lbl), esc(val), s)


def _grand(ir):
    """Totals across the main transcript + EVERY sub-agent. No double-count: each
    transcript's usage is its own (a parent's tokens never include its children's).
    Returns (grand_tokens, grand_cost, agents_sorted, main_cost).

    Cost is summed over COMPUTABLE components only: a component (the main session,
    or a sub-agent) contributes None when its model id is unknown, so its list-price
    cost cannot be computed. If NOTHING is computable, grand_cost is None
    (uncomputable) — the Est. cost card then renders "—" instead of flooring a
    summed 0.0 to a misleading "<$0.01". A genuinely computed small cost (0 < x <
    0.01) is a real float and still renders "<$0.01". main_cost is likewise the RAW
    per-session cost (None when uncomputable) so the breakdown "Main session" row
    renders "—" like the sub-agent rows already do."""
    gt = dict(ir["tokens"])
    agents = _flatten_agents(ir["turns"])
    for a in agents:
        for k in gt:
            gt[k] += a["tokens"].get(k, 0)
    main_cost = _session_cost(ir)   # None when the model id is unknown
    computable = [c for c in [main_cost] + [a.get("cost_usd") for a in agents]
                  if c is not None]
    gc = sum(computable) if computable else None
    agents.sort(key=lambda a: IR.headline_tokens(a["tokens"]), reverse=True)
    return gt, gc, agents, main_cost


# ── The usage dashboard (approved format, PO sign-off 2026-08-22) ────────
# The SAME design Backstage's Usage tab renders from the same
# `ir["workflow_usage"]` payload: four flat metrics, a single-hue spend
# composition bar, and one table grouped by the work the money went into.
# Chronological row order; ad-hoc chat sits where it really happened.

# Single hue in tints, dark = biggest. NOT a categorical palette: the brand four
# were run through the dataviz validator as a categorical set and FAILED
# (lightness band, chroma floor — they read as grays and cannot carry identity
# by colour). Identity comes from the labels; the hue carries magnitude only.
# Ranked purely by SIZE - no per-identity colour, so ad-hoc chat is tinted like
# anything else of its size. Four fading steps then flat: rank 5 and beyond share
# the lightest tint. Label ink is COMPUTED from the tint and verified to WCAG
# 4.5:1 (white 7.04 / 4.51 on the two darkest; navy 5.44 / 7.88 / 10.09 on the
# rest) - an unreadable label is worse than no label.
BAR_TINTS = ("#235e80", "#3d7ca9", "#6da3c4", "#9dc2d8", "#c2d8e6")
BAR_INK = ("#ffffff", "#ffffff", "#002949", "#002949", "#002949")
# The document's bar is ~700px of content width; labels are 10.5px with 8px of
# padding each side. Deciding from the nominal width keeps it deterministic.
BAR_NOMINAL_PX = 700
BAR_CHAR_PX = 5.9
BAR_LABEL_PAD = 16


def _bar_step(rank):
    i = min(rank, len(BAR_TINTS) - 1)
    return BAR_TINTS[i], BAR_INK[i]


def _bar_label(name, cost, pct):
    """Full "name - $x" when it fits, just "$x" when the name does not, nothing
    when even the number does not. The title carries it at every level."""
    room = (pct / 100.0) * BAR_NOMINAL_PX - BAR_LABEL_PAD
    full = "%s · %s" % (name, cost)
    if room >= len(full) * BAR_CHAR_PX:
        return full
    if room >= len(cost) * BAR_CHAR_PX:
        return cost
    return ""

# Hand-drawn in the RJ icon library's stroke language: single-weight stroke, no
# fill, round caps. The silhouettes are deliberately different shapes so they
# stay apart when the linework does not.
ICON_KIT = ('<svg role="img" aria-label="Kit workflow" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#002949" ><title>Kit workflow</title>'
            'stroke-width="1.4" stroke-linecap="round">'
            '<circle cx="12" cy="5.2" r="2.4"/>'
            '<path d="M12 7.6v3.2M6 10.8h12M6 10.8v2.6M18 10.8v2.6"/>'
            '<circle cx="6" cy="16.2" r="2.4"/><circle cx="18" cy="16.2" r="2.4"/></svg>')
# Notch on the TOP edge, tab on the RIGHT — perpendicular, not opposing. With
# both features on the same axis the notch eats the middle of the body and 24px
# reads as a bracket with two lumps rather than a puzzle piece (verified in a
# browser at 24px). Body is 14x14 so the knobs stay small relative to it.
ICON_OTHER = ('<svg role="img" aria-label="Other skill" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#002949" ><title>Other skill</title>'
              'stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">'
              '<path d="M5 19v-14h4.6a2 2 0 0 0 4.8 0H19v5.6a2 2 0 0 1 0 4.8V19z"/>'
              '</svg>')
ICON_MAIN = ('<svg role="img" aria-label="Main conversation" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#3d7ca9" ><title>Main conversation</title>'
             'stroke-width="1.5" stroke-linecap="round">'
             '<rect x="3.5" y="4" width="12" height="8.5" rx="2.2"/>'
             '<rect x="8.5" y="9" width="12" height="8.5" rx="2.2" fill="#fbfcfe"/>'
             '<path d="M12.5 17.5v3l3.2-3"/>'
             '<circle cx="12.2" cy="13.2" r=".75" fill="#3d7ca9" stroke="none"/>'
             '<circle cx="14.5" cy="13.2" r=".75" fill="#3d7ca9" stroke="none"/>'
             '<circle cx="16.8" cy="13.2" r=".75" fill="#3d7ca9" stroke="none"/></svg>')
ICON_AGENT = ('<svg role="img" aria-label="Delegated agent" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#3d7ca9" ><title>Delegated agent</title>'
              'stroke-width="1.5" stroke-linecap="round">'
              '<rect x="7" y="7" width="10" height="10" rx="1.7"/>'
              '<rect x="10.2" y="10.2" width="3.6" height="3.6" rx=".7"/>'
              '<path d="M4 10h3M4 14h3M17 10h3M17 14h3M10 4v3M14 4v3M10 17v3M14 17v3"/></svg>')
ICON_ADHOC = ('<svg role="img" aria-label="Outside any workflow" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#708090" ><title>Outside any workflow</title>'
              'stroke-width="1.4" stroke-linecap="round">'
              '<circle cx="12" cy="12" r="7.2" stroke-dasharray="2.6 2.6"/></svg>')

_GROUP_ICON = {"kit": ICON_KIT, "other": ICON_OTHER, "adhoc": ICON_ADHOC}


def _plural(n, one, many):
    return one if n == 1 else many


def _usage_groups(wu):
    """One row per INVOCATION, plus one per stretch of ad-hoc chat, in the order
    they happened.

    Deliberately NOT a per-skill rollup: two runs of the same workflow days
    apart are two rows, each with its own cost, duration and delegates. Rolling
    them together produced a single monolith spanning the session and destroyed
    the chronology the table is ordered by."""
    return [{"key": seg.get("order"),
             "name": "Ad-hoc chat" if seg.get("kind") == "adhoc" else seg["workflow"],
             "kind": seg.get("kind") or "other", "row": seg}
            for seg in wu.get("segments") or []]


def _metrics(ir, wu, grand_cost):
    """Top summary: what the session consisted of.

    Counts are RUNS, not distinct names — the table lists one row per
    invocation, so a metric counting distinct workflow names would contradict
    the thing directly beneath it. Total cost is deliberately absent: the
    masthead and the session row both already state it.

    DEGRADED CASE: an IR from an older collector carries no attribution, so
    there are no run counts to state. It falls back to the one figure that is
    still knowable — the session's own cost — because a header with nothing in
    it is worse than a header with one true number."""
    if not wu:
        return ('<div class="usg-metrics"><div class="usg-m"><div class="v">%s</div>'
                '<div class="k">Total cost</div><div class="c">public list rates'
                '</div></div></div>' % esc(IR.fmt_cost(grand_cost) or "—"))
    runs = wu.get("runs") or []
    kit_runs = sum(1 for r in runs if r.get("kind") == "kit")
    other_runs = len(runs) - kit_runs
    kit_agents = other_agents = 0
    for seg in wu.get("segments") or []:
        for d in seg.get("detail") or []:
            if d.get("kind") != "agent":
                continue
            if d.get("role"):
                kit_agents += 1
            else:
                other_agents += 1
    lines = (wu.get("totals") or {}).get("lines") or 0
    # Shell-authored lines are approximate — see collector's _shell_written_lines.
    # Named separately so an inexact share is not read as part of an exact count.
    shell_lines = ((wu.get("totals") or {}).get("lines_by_ext")
                   or {}).get(IR.SHELL_EXT) or 0
    lines_note = ("gross · %s approx. via shell" % IR.fmt_tokens(shell_lines)
                  if shell_lines else "gross, edits included")
    tiles = [
        (kit_runs, _plural(kit_runs, "Kit workflow run", "Kit workflow runs"),
         "Agentic Studio"),
        (other_runs, _plural(other_runs, "Other skill run", "Other skill runs"),
         "outside the kit"),
        (kit_agents, _plural(kit_agents, "Kit agent", "Kit agents"),
         "delegated to a kit role"),
        (other_agents, _plural(other_agents, "Other agent", "Other agents"),
         "no kit role resolved"),
        (IR.fmt_tokens(lines), _plural(lines, "Line written", "Lines written"),
         lines_note),
    ]
    cells = ['<div class="usg-m"><div class="v">%s</div><div class="k">%s</div>'
             '<div class="c">%s</div></div>' % (esc(str(value)), esc(label), esc(caption))
             for value, label, caption in tiles]
    return '<div class="usg-metrics">%s</div>' % "".join(cells)


def _usage_bar(wu):
    """Full-width 100% strip, biggest first. EVERY segment carries a title —
    the slivers are precisely the ones a reader needs the hover for. Segments
    that cost nothing are dropped: a 0%-wide strip is not a thing you can point
    at."""
    if not wu:
        return ""
    total = (wu.get("totals") or {}).get("cost_usd")
    if not total or total <= 0:
        return ""
    priced = [g for g in _usage_groups(wu) if (g["row"].get("cost_usd") or 0) > 0]
    priced.sort(key=lambda g: -g["row"]["cost_usd"])
    if not priced:
        return ""
    segs = []
    for i, g in enumerate(priced):
        pct = g["row"]["cost_usd"] / total * 100.0
        tint, ink = _bar_step(i)
        cost = IR.fmt_cost(g["row"]["cost_usd"]) or "—"
        label = _bar_label(g["name"], cost, pct)
        inner = ('<span class="lbl" style="color:%s">%s</span>' % (ink, esc(label))
                 ) if label else ""
        segs.append(
            '<span class="seg" style="width:%.2f%%;background:%s" title="%s">%s</span>'
            % (pct, tint, esc("%s — %s" % (g["name"], cost)), inner))
    return '<div class="usg-bar">%s</div>' % "".join(segs)


def _segment_models(row):
    """The distinct models used on this segment's main thread, first seen first.

    Every segment has the same two properties — its models and its delegates —
    and the row anatomy follows from them, identically for kit runs, non-kit runs
    and ad-hoc spans. An ad-hoc span can span models and spawn delegates just as
    a workflow can, so it is not a special leaf and gets no separate code path."""
    seen = []
    for d in row.get("detail") or []:
        if d.get("kind") != "agent" and d.get("model") and d["model"] not in seen:
            seen.append(d["model"])
    return seen


DOT = "\u00b7"


def _lines_cell(row):
    """The Lines figure, with its per-extension breakdown behind a native
    disclosure.

    A static document has no click handler to give, so `<details>` is the honest
    equivalent of the viewer's toggle: the breakdown is reachable, and never
    reachable ONLY by hovering."""
    lines = row.get("lines") or 0
    exts = sorted((row.get("lines_by_ext") or {}).items(), key=lambda kv: -kv[1])
    figure = IR.fmt_tokens(lines)
    if not lines or not exts:
        return figure
    top = exts[0][1] or 1
    bars = "".join(
        '<span class="lx"><span class="lx-e">%s</span>'
        '<span class="lx-bar"><span style="width:%.1f%%"></span></span>'
        '<span class="lx-n">%s</span></span>' % (esc(ext), n / top * 100.0, f"{n:,}")
        for ext, n in exts)
    return ('<details class="lx-d"><summary>%s</summary>'
            '<span class="lx-wrap">%s</span></details>') % (figure, bars)


def _num_cells(row):
    """All four priced quantities, then the cost — so a reader can recompute the
    figure rather than take it on trust. Cache WRITE precedes cache READ:
    creation happens before reuse, and it keeps the columns expensive-to-cheap."""
    tk = row.get("tokens") or {}
    return ('<td class="n">%s</td><td class="n">%s</td><td class="n">%s</td>'
            '<td class="n">%s</td><td class="n">%s</td>'
            '<td class="n lines">%s</td>') % (
        IR.fmt_tokens(tk.get("input", 0)), IR.fmt_tokens(tk.get("output", 0)),
        IR.fmt_tokens(tk.get("cache_creation", 0)),
        IR.fmt_tokens(tk.get("cache_read", 0)),
        # esc: fmt_cost can return "<$0.01", which must not go into markup raw.
        esc(IR.fmt_cost(row.get("cost_usd")) or "—"),
        _lines_cell(row))


def _work_cell(icon, name, meta, indent=False, ref=None):
    label = ('<a class="jump" href="#%s">%s</a>' % (esc(ref), esc(name))) if ref else esc(name)
    return ('<td class="work%s"><span class="cell">%s<span class="txt">'
            '<span class="nm">%s</span><span class="meta">%s</span></span>'
            '</span></td>') % (
        " det" if indent else "",
        ('<span class="ico">%s</span>' % icon) if icon else "",
        label, esc(meta))


def _usage_table(ir, wu, refs):
    """One table: the session anchor row, then every group in chronological
    order with its delegates folded underneath. In print and in a browser
    without JS the folds are simply open — a static document should not hide
    numbers behind an interaction it cannot offer."""
    if not wu:
        return ""
    rows = ['<tr class="usg-session"><td class="work">'
            '<span class="cell">This session</span></td>'
            '<td class="n">%s</td>%s</tr>' % (
                esc(IR.fmt_duration((wu["totals"] or {}).get("active_s")) or "—"),
                _num_cells(wu["totals"]))]
    for g in _usage_groups(wu):
        row = g["row"]
        agents = row.get("agents") or 0
        models = _segment_models(row)
        # Same anatomy for every kind — see _segment_models. THE MODEL IS NEVER
        # INVISIBLE: on the summary line when there is nothing to expand, on
        # each detail line otherwise.
        expandable = bool(agents) or len(models) > 1
        # The icon carries the kit/other/ad-hoc distinction and names itself on
        # hover and to a screen reader, so the words are gone from the line.
        # Model first, named while few enough to read and counted past that.
        bits = []
        if models and len(models) <= 2:
            bits.append(", ".join(models))
        elif models:
            bits.append("%d models" % len(models))
        if agents:
            bits.append("%d %s" % (agents, _plural(agents, "agent", "agents")))
        # Visible text, not a marker to decode: an interrupted run's figures are
        # partial and a reader comparing rows has to see that.
        if row.get("interrupted"):
            bits.append("interrupted")
        meta = (" %s " % DOT).join(bits)
        rows.append('<tr class="usg-row">%s<td class="n">%s</td>%s</tr>' % (
            _work_cell(_GROUP_ICON.get(g["kind"], ICON_OTHER), g["name"], meta),
            esc(IR.fmt_duration(row.get("active_s")) or "—"), _num_cells(row)))
        # Every agent, no cap — hiding rows behind a "+ N more" count in a cost
        # table is friction with nothing to gain.
        detail = row.get("detail") or []
        mains = [d for d in detail if d.get("kind") != "agent"]
        delegates = [d for d in detail if d.get("kind") == "agent"]
        for d in mains + delegates:
            is_main = d.get("kind") != "agent"
            name = ("Main conversation" if is_main
                    else (d.get("desc") or d.get("label") or "delegated agent"))
            # Just the model — the only discriminator between two
            # "Main conversation" rows, and never omitted for that reason.
            # "this run's share" explained the data model rather than labelling
            # the data, and the row already sits inside a run's detail.
            # Role FIRST, then the model — identity, then rate card.
            sub = (" %s " % DOT).join(
                x for x in [d.get("role"), d.get("model")] if x)
            rows.append('<tr class="usg-det">%s<td class="n">%s</td>%s</tr>' % (
                _work_cell(ICON_MAIN if is_main else ICON_AGENT, str(name)[:90], sub,
                           indent=True,
                           ref=None if is_main else refs.get(d.get("agent_id"))),
                esc(IR.fmt_duration(d.get("duration_s")) or "—"),
                _num_cells(d)))
    return (
        '<div class="usg"><table class="usg-t"><thead><tr><th>Work</th>'
        '<th class="n">Active / Agent&nbsp;time*</th><th class="n">In</th>'
        '<th class="n">Out</th><th class="n">Cache write</th>'
        '<th class="n">Cache read</th><th class="n">Cost</th>'
        '<th class="n">Lines</th>'
        '</tr></thead><tbody>%s</tbody></table>'
        '<p class="usg-note">Spend is charged to the workflow that was running; a delegate '
        'counts to the workflow that spawned it. Public list rates. Lines are gross '
        '&mdash; an edit counts its new text every time &mdash; so they measure editing '
        'activity, not artifact size; open a figure for the breakdown by file type. An '
        '<b>interrupted</b> run was stopped part-way, so its figures are partial.<br>'
        '* Main-conversation times are shares of the run\'s active time and DO add up to '
        'it. Agent times are independent spans that run in parallel and do not.</p>%s</div>') % (
            "".join(rows), _pricing_note(ir))


def _cost_note(ir, grand_cost):
    """Footnote directly under the Est. cost card, shown ONLY when the cost is
    uncomputable (grand_cost is None — no model id anywhere, so no per-token list
    price applies and the card shows "—"). Devin bills per ACU rather than per
    token, so it gets a specific note; any other uncomputable case gets the generic
    list-price-unknown note. When the cost IS computed, nothing is emitted here (the
    breakdown's _pricing_note explains the list-price rates)."""
    if grand_cost is not None:
        return ""
    if ir.get("source_ide") == "devin":
        msg = "Devin is priced per ACU (Agent Compute Units), not per token."
    else:
        msg = "Est. cost shown only when the model's list price is known."
    return '<div class="more">%s</div>' % esc(msg)


def _strip(ir):
    """Identity chips, not metrics. Session DURATION is deliberately gone: the
    only duration worth reporting is per-workflow ACTIVE time, which excludes
    the hours a session spends waiting for a human. The sub-agent count left too
    — it is one of the four top metrics now."""
    chips = []
    if ir.get("model"):
        chips.append('<span class="chip">Model <b>%s</b></span>' % esc(ir["model"]))
    chips.append('<span class="chip"><b>%d</b> rounds</span>'
                 % sum(1 for t in ir["turns"] if t["role"] == "user"))
    return '<div class="mstrip">%s</div>' % "".join(chips)


def _bd_row(name, model, tk, cost, dur, cls="", ref=None):
    name_html = ('<a class="jump" href="#%s">%s</a>' % (esc(ref), esc(name))) if ref else esc(name)
    return ('<tr%s><td class="name">%s</td><td>%s</td>'
            '<td class="n">%s</td><td class="n">%s</td><td class="n">%s</td><td class="n">%s</td>'
            '<td class="n">%s</td><td class="n">%s</td></tr>') % (
        (' class="%s"' % cls) if cls else "",
        name_html, esc(model or "—"),
        IR.fmt_tokens(tk.get("input", 0)),
        IR.fmt_tokens(tk.get("output", 0)),
        IR.fmt_tokens(tk.get("cache_read", 0)),
        IR.fmt_tokens(tk.get("cache_creation", 0)),
        IR.fmt_cost(cost) or "—",
        IR.fmt_duration(dur) or "—")


def _pricing_note(ir):
    """Small grey footnote: the exact list-price rates used, so the math is checkable."""
    fams, seen = [], set()
    for m in [ir.get("model")] + [a.get("model") for a in _flatten_agents(ir["turns"])]:
        f = pricing._family(m)
        if f and f not in seen:
            seen.add(f)
            fams.append(f)
    if not fams:
        return ('<div class="more">Est. cost is list-price (model pricing unknown); '
                'thinking is billed as output.</div>')
    rates = "; ".join(
        "%s — input $%.2f · output $%.2f · cache-write $%.2f · cache-read $%.2f"
        % (f.title(), pricing.PRICING[f]["in"], pricing.PRICING[f]["out"],
           pricing.PRICING[f]["cw"], pricing.PRICING[f]["cr"])
        for f in fams)
    return ('<div class="more">Est. cost = (input·in + output·out + cache-write·cw + cache-read·cr) ÷ 1M, '
            'at list price — %s. Thinking is billed as output (not logged separately).</div>') % esc(rates)


def _agent_refs(ir):
    """{agent id -> DOM ref} so a usage detail row can deep-link to that agent's
    card in the transcript below. The old standalone breakdown table carried
    these links; the detail rows inherit them rather than losing the feature."""
    return {a["id"]: a["_ref"] for a in _flatten_agents(ir["turns"])
            if a.get("id") and a.get("_ref")}


def _assign_refs(ir):
    """Give every sub-agent a stable DOM id (document order) so the breakdown table
    can deep-link to its card in the transcript."""
    n = [0]

    def walk(turns):
        for t in turns:
            for b in t.get("blocks", []):
                if b.get("t") == "agent":
                    n[0] += 1
                    b["agent"]["_ref"] = "sa-%d" % n[0]
                    walk(b["agent"].get("turns", []))
    walk(ir["turns"])


def _walk_tools(turns):
    """Yield every tool block across the main transcript and all (nested) sub-agents."""
    for t in turns:
        for b in t.get("blocks", []):
            if b.get("t") == "tool":
                yield b
            elif b.get("t") == "agent":
                for x in _walk_tools(b["agent"].get("turns", [])):
                    yield x


def _output_metrics(ir):
    """What the session PRODUCED — countable artifacts, aggregated over main + sub-agents.
    Lines are gross (see caveat). Side-effects are inferred from tool names."""
    lines = files_w = files_e = bash = prs = wi = commits = pages = 0
    for b in _walk_tools(ir["turns"]):
        n = b.get("name") or ""
        lines += b.get("lines", 0) or 0
        if n == "Write":
            files_w += 1
        elif n in ("Edit", "MultiEdit", "NotebookEdit"):
            files_e += 1
        elif n == "Bash":
            bash += 1
        if "create_pull_request" in n:
            prs += 1
        elif "create_work_item" in n:
            wi += 1
        elif "create_commit" in n:
            commits += 1
        elif "create_wiki_page" in n or "confluence_create_page" in n:
            pages += 1
    if not any([lines, files_w, files_e, bash, prs, wi, commits, pages]):
        return ""
    cards = [
        _card("Lines written", "{:,}".format(lines), "code + docs · gross"),
        _card("Files created", "{:,}".format(files_w), "Write"),
        _card("Files edited", "{:,}".format(files_e), "Edit / MultiEdit"),
        _card("Bash commands", "{:,}".format(bash), "run"),
    ]
    chips = []
    for label, v in [("PRs opened", prs), ("Work items created", wi),
                     ("Commits", commits), ("Wiki/Confluence pages", pages)]:
        if v:
            chips.append('<span class="chip"><b>%d</b> %s</span>' % (v, esc(label)))
    chip_html = ('<div class="mstrip" style="margin-top:8px">%s</div>' % "".join(chips)) if chips else ""
    return ('<div class="osec"><div class="oh">Output — what this session produced</div>'
            '<div class="mcards">%s</div>%s</div>') % ("".join(cards), chip_html)


# Source-of-truth for the human-readable IDE name (shared with generator_md so the
# label is defined once). Unknown-but-present source_ide -> shown verbatim; empty
# / None -> "" (callers substitute a generic label).
_IDE_NAMES = {"claude-code": "Claude Code", "vscode": "VS Code", "devin": "Devin"}


def _ide_name(source_ide):
    return _IDE_NAMES.get(source_ide) or (source_ide or "")


def _ide_label(source_ide):
    """Masthead session label derived from ir['source_ide'] (bug fix: this was
    hardcoded to 'Claude Code session' for every export). Known -> 'X session';
    other non-empty value -> '<value> session'; empty/None -> 'Coding session'."""
    name = _ide_name(source_ide)
    return (name + " session") if name else "Coding session"


def render(ir):
    _assign_refs(ir)
    date = ""
    try:
        date = datetime.fromisoformat(ir.get("started").replace("Z", "+00:00")).strftime("%d %b %Y")
    except Exception:
        pass
    span = " ".join(x for x in (date, ("%s–%s" % (
        _fmt_time(ir.get("started")), _fmt_time(ir.get("ended")))).strip("–")) if x)
    grand_tk, grand_cost, agents, main_cost = _grand(ir)

    mark = ('<div class="mark">%s</div>' % PRODUCT_MARK_SVG) if PRODUCT_MARK_SVG else ""
    mast_meta = " · ".join(x for x in [_ide_label(ir.get("source_ide")), ir.get("model") or "", span] if x)
    sid = ir.get("session_id") or ""
    sid_line = ('<div class="sid">Session %s</div>' % esc(sid)) if sid else ""
    masthead = (
        '<div class="mast"><div class="mrow"><div class="mtext">'
        '<div class="eyebrow">Session Export</div>'
        '<h1>%s</h1><div class="meta">%s</div>%s</div>%s</div></div>'
    ) % (esc(ir["title"]), esc(mast_meta), sid_line, mark)

    counts = "%d rounds · %d sub-agents" % (
        sum(1 for t in ir["turns"] if t["role"] == "user"), ir.get("agent_count", 0))

    # Condensed header: parked behind the masthead at rest, revealed and pinned
    # once the masthead scrolls away. Emitted from here rather than cloned by
    # the script so the reveal needs no JS at all.
    cglyph = ('<span class="cmark">%s</span>' % PRODUCT_GLYPH_SVG) if PRODUCT_GLYPH_SVG else ""
    cond = ('<div class="cond">%s<span class="cname">%s</span>'
            '<span class="ct">%s</span><span class="cmeta">%s</span></div>'
            ) % (cglyph, esc(PRODUCT_NAME), esc(ir["title"]), esc(counts))

    bar = ('<div class="bar"><input class="f" id="flt" placeholder="Filter turns and agents…">'
           '<button class="btn" id="ex">Expand all</button>'
           '<button class="btn" id="col">Collapse all</button>'
           '<span class="sum">%s</span></div>') % esc(counts)

    wu = ir.get("workflow_usage")
    pad = "".join([_metrics(ir, wu, grand_cost), _cost_note(ir, grand_cost),
                   _usage_bar(wu),
                   _strip(ir), _output_metrics(ir),
                   _usage_table(ir, wu, _agent_refs(ir)), bar, _turns(ir["turns"])])
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>%s</title><style>%s%s</style></head><body><div class=\"wrap\">"
        "<div class=\"doc\" id=\"doc\">%s%s<div class=\"pad\">%s</div></div>"
        "<div class=\"foot\"><b>Raymond James · Confidential</b> — internal use · "
        "Generated by export-session</div>"
        "</div><script>%s</script></body></html>"
    ) % (esc(ir["title"]), FONT_CSS, _CSS, cond, masthead, pad, _JS)
