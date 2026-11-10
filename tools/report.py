"""HTML sprint report built from the ledger and git. No model calls.

Each section is regenerated from data and compared with the previous report: unchanged sections
are kept byte-for-byte, changed ones are replaced and tagged with the commit that changed them.
Output: .harness/report.html (operator-facing text is Russian by operator rule).
Usage: python tools/report.py [--out PATH]
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger  # noqa: E402

STATE_RU = {"todo": ("не начато", "s-todo"), "in_progress": ("в работе", "s-wip"), "implemented": ("код готов, ждёт тестов", "s-wip"),
            "not_run": ("тесты не запускались", "s-block"), "verified": ("на ревью", "s-rev"), "done": ("готово", "s-done"),
            "failed": ("тесты красные", "s-block")}
RISK_RU = {"trivial": "тривиально", "local": "локально", "multi-module": "несколько модулей", "risky": "опасно"}
E = html.escape

CSS = """:root{--bg:#f3f5f4;--surface:#fff;--ink:#18201d;--muted:#5d6a65;--line:#d6ddda;--done:#1f6f5c;--rev:#9a6a00;--wip:#2d5fa8;--block:#a23a32;--new:#6b4bb0}
@media (prefers-color-scheme:dark){:root{--bg:#121715;--surface:#1a211e;--ink:#e4ebe8;--muted:#97a6a0;--line:#2c3632;--done:#6fd0b2;--rev:#e5b64a;--wip:#86aef0;--block:#f08b80;--new:#b9a0f0;color-scheme:dark}}
body{background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;margin:0}
main{max-width:1100px;margin:0 auto;padding:24px max(16px,env(safe-area-inset-left,0px)) 48px}
section{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-top:14px}
h1{font-size:24px;margin:0}h2{font-size:17px;margin:0 0 10px}.muted{color:var(--muted);font-size:13px}
.row{display:grid;grid-template-columns:minmax(140px,240px) 1fr 70px;gap:12px;align-items:center;margin:6px 0}
.bar{height:12px;border-radius:6px;background:var(--line);overflow:hidden;display:flex}.bar i{height:100%;display:block}
.v{text-align:right;font-variant-numeric:tabular-nums;font-family:ui-monospace,Consolas,monospace;font-size:13px}
.legend{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12px;color:var(--muted);margin-top:8px}.legend b{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px}
.tbl{overflow-x:auto}table{border-collapse:collapse;width:100%;min-width:720px;font-size:14px}
th{text-align:left;font-size:12px;color:var(--muted);font-weight:500;border-bottom:1px solid var(--line);padding:8px}
td{border-bottom:1px solid var(--line);padding:8px;vertical-align:top}tr:last-child td{border-bottom:0}
.st{white-space:nowrap}.st::before{content:"";display:inline-block;width:9px;height:9px;border-radius:50%;background:currentColor;margin-right:6px}
.s-done{color:var(--done)}.s-rev{color:var(--rev)}.s-wip{color:var(--wip)}.s-todo{color:var(--muted)}.s-block{color:var(--block)}
.upd{outline:2px dashed var(--new);outline-offset:3px}.tag{font-size:11px;color:var(--new);margin-left:8px;font-family:ui-monospace,monospace}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}.kpi{border:1px solid var(--line);border-radius:8px;padding:10px}
.kpi .n{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums}code{font-family:ui-monospace,Consolas,monospace;font-size:13px}"""


def git(*args) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.strip()


def bar(parts: list[tuple[int, str]], total: int) -> str:
    if not total:
        return '<div class="bar"></div>'
    return '<div class="bar">' + "".join(f'<i style="width:{n / total * 100:.1f}%;background:var(--{c})"></i>' for n, c in parts if n) + "</div>"


def sprint_parts(s):
    c = {k: sum(1 for t in s["tasks"] if t["state"] == k) for k in ledger.STATES}
    return [(c["done"], "done"), (c["verified"], "rev"), (c["in_progress"] + c["implemented"], "wip"), (c["failed"], "block")]


def sec_progress(d):
    done = sum(ledger.counter(s)[0] for s in d["sprints"])
    total = sum(ledger.counter(s)[1] for s in d["sprints"])
    pct = round(done / total * 100) if total else 0
    rows = [f'<div class="row"><div><b>Весь проект</b> <span class="muted">({sum(s["state"] == "closed" for s in d["sprints"])} из {len(d["sprints"])} спринтов закрыто)</span></div>{bar([(done, "done")], total)}<div class="v">{pct} %</div></div>']
    for s in d["sprints"]:
        n, m = ledger.counter(s)
        rows.append(f'<div class="row"><div>{E(s["id"])} {E(s["title"])}</div>{bar(sprint_parts(s), m)}<div class="v">{n} / {m}</div></div>')
    legend = ('<div class="legend"><span><b style="background:var(--done)"></b>готово: тесты зелёные и ревью принято</span>'
              '<span><b style="background:var(--rev)"></b>на ревью</span><span><b style="background:var(--wip)"></b>в работе</span>'
              '<span><b style="background:var(--block)"></b>тесты красные</span><span><b style="background:var(--line)"></b>не начато</span></div>'
              '<p class="muted">Формула: готово = задачи с коммитом, зелёными тестами тестировщика и принятым ревью ÷ все задачи.</p>')
    return "<h2>Прогресс</h2>" + "".join(rows) + legend


def sec_kpis(d):
    s = ledger.active_sprint(d)
    tasks = s["tasks"] if s else []
    tok = [t["tokens"] for t in tasks if t.get("tokens")]
    code, tests, docs = (sum(x[k] for x in tok) for k in ("code", "tests", "docs"))
    total = code + tests + docs
    split = f"{round(code / total * 100)} / {round(tests / total * 100)} / {round(docs / total * 100)} %" if total else "нет данных"
    revs = sum(len((t.get("routing") or {}).get("reviewers", [])) for t in tasks)
    ap = d.get("approval") or {}
    h = git("rev-parse", "HEAD")
    gpt = "APPROVE для текущего коммита" if ap.get("head") == h else ("APPROVE устарел (был новый коммит)" if ap.get("head") else "ещё не было")
    k = [("Токены активного спринта", f"{total / 1e6:.2f} млн" if total else "нет данных"), ("Код / тесты / документы (цель 40/40/20)", split),
         ("Запусков ревьюеров", str(revs)), ("GPT-PM", gpt)]
    return "<h2>Показатели</h2><div class=\"kpis\">" + "".join(f'<div class="kpi"><div class="muted">{E(a)}</div><div class="n">{E(b)}</div></div>' for a, b in k) + "</div>"


def sec_tasks(d):
    s = ledger.active_sprint(d)
    if not s:
        return "<h2>Задачи</h2><p class=\"muted\">Открытого спринта нет.</p>"
    rows = []
    for t in s["tasks"]:
        st, cls = STATE_RU.get(t["state"], (t["state"], "s-todo"))
        v = t.get("verify") or {}
        vtxt = {"pass": "зелёные", "fail": "красные", "NOT_RUN": "НЕ ЗАПУСКАЛОСЬ"}.get(v.get("result"), "—")
        if v.get("evidence"):
            vtxt += f'<div class="muted">{E(v["evidence"][:120])}</div>'
        rv = (t.get("routing") or {}).get("reviewers", [])
        rtxt = ", ".join(E(x["agent"]) for x in rv) or "—"
        if t.get("routing"):
            rtxt += f'<div class="muted">{E(RISK_RU.get(t["routing"]["risk"], ""))}: {E(t["routing"]["risk_reason"][:90])}</div>'
        commits = " ".join(f"<code>{E(c[:7])}</code>" for c in t["commits"]) or "—"
        crit = "".join(f"<li>{E(c)}</li>" for c in t["criteria"])
        rows.append(f'<tr><td><code>{E(t["id"])}</code></td><td>{E(t["title"])}<ul class="muted" style="margin:4px 0 0;padding-left:16px">{crit}</ul></td>'
                    f'<td><span class="st {cls}">{st}</span></td><td>{vtxt}</td><td>{rtxt}</td><td>{commits}</td></tr>')
    return (f"<h2>Задачи спринта {E(s['id'])} «{E(s['title'])}»</h2><div class=\"tbl\"><table><thead><tr><th>Задача</th><th>Что это и критерии</th>"
            "<th>Состояние</th><th>Тесты</th><th>Ревью (кто и почему)</th><th>Коммиты</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")


def sec_next(d):
    s = ledger.active_sprint(d)
    steps = []
    if s:
        pending = [t for t in s["tasks"] if t["state"] != "done"]
        for t in pending[:3]:
            nxt = {"todo": "/next — код", "in_progress": "/next — закончить код", "implemented": "/verify — тесты",
                   "not_run": "/verify — запустить тесты", "verified": "/ship — ревью", "failed": "исправить и /verify"}[t["state"]]
            steps.append((f"{t['id']} {t['title']}", nxt, "Claude"))
        if not pending:
            steps.append(("Весь спринт", "GPT-PM до APPROVE, push, PR, затем /close", "GPT-PM, затем Claude"))
    rows = "".join(f"<tr><td>{E(a)}</td><td>{E(b)}</td><td>{E(c)}</td></tr>" for a, b, c in steps) or '<tr><td colspan="3">—</td></tr>'
    return "<h2>Следующие шаги</h2><div class=\"tbl\"><table><thead><tr><th>Что</th><th>Шаг</th><th>Кто</th></tr></thead><tbody>" + rows + "</tbody></table></div>"


SECTIONS = [("progress", sec_progress), ("kpis", sec_kpis), ("tasks", sec_tasks), ("next", sec_next)]
SEC_RE = re.compile(r'<section id="(\w+)" data-hash="(\w+)"[^>]*>(.*?)</section>', re.S)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    d = ledger.load()
    out = Path(a.out) if a.out else ledger.ledger_path().parent / "report.html"
    old = {m.group(1): (m.group(2), m.group(0)) for m in SEC_RE.finditer(out.read_text(encoding="utf-8", errors="replace"))} if out.exists() else {}
    sha = git("rev-parse", "--short", "HEAD") or "нет коммитов"
    parts, changed = [], []
    for sid, fn in SECTIONS:
        body = fn(d)
        h = hashlib.sha1(body.encode()).hexdigest()[:12]
        if sid in old and old[sid][0] == h:
            parts.append(re.sub(r' class="upd"|<span class="tag">.*?</span>', "", old[sid][1]))
        else:
            changed.append(sid)
            tagged = body.replace("</h2>", f'<span class="tag">обновлено коммитом {E(sha)}</span></h2>', 1) if old else body
            parts.append(f'<section id="{sid}" data-hash="{h}"{" class=\"upd\"" if old else ""}>{tagged}</section>')
    evs = d.get("events", [])
    last = evs[-1] if evs else {}
    alerts = []
    errs = [e for e in evs if e["kind"] == "hook-error"]
    if errs:
        alerts.append(f"Ошибки хуков: {len(errs)}, последняя: {errs[-1].get('message', '')}")
    skips = [e for e in evs if e["kind"] == "approval-skipped"]
    if skips:
        alerts.append(f"Проверка GPT-PM перед push была отключена {len(skips)} раз(а), последний {skips[-1]['at']}")
    full = git("rev-parse", "HEAD")
    if full and not any(e.get("sha") == full for e in evs) and "Task:" in git("log", "-1", "--format=%B"):
        alerts.append(f"Коммит {sha} помечен задачей, но реестр его не записал")
    alert_html = "".join(f'<p class="s-block" style="font-weight:600">{E(x)}</p>' for x in alerts)
    head = (f"<header><h1>{E(d['project'])} · отчёт по задачам</h1><p class=\"muted\">коммит {E(sha)} · "
            f"последнее событие реестра: {E(last.get('kind', 'нет'))} {E(last.get('at', ''))} · "
            f"изменено секций: {len(changed)} ({', '.join(changed) or 'нет'})</p>{alert_html}</header>")
    doc = (f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
           f"<title>{E(d['project'])} отчёт</title><style>{CSS}</style></head><body><main>{head}{''.join(parts)}</main></body></html>")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(doc, encoding="utf-8", newline="\n")
    os.replace(tmp, out)
    print(f"report: {out} (changed: {', '.join(changed) or 'none'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
