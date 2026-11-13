"""Harness v2 program status report tool: the Russian HTML PROGRAM STATUS page built from the ledger and git. No model calls.

The page is rendered from scratch on every run (nothing is merged with an older file).
Default output: <git top-level>/reports/<PROJECT>_PROGRAM_STATUS_LIVE.ru.html.
A file whose first 400 bytes lack the marker comment is treated as hand-made and never overwritten.
Usage: python tools/report.py [--out PATH] [--stage]   (--stage: pre-commit mode, adds the report to the commit, never fails)
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger  # noqa: E402

MARKER = "<!-- harness-report v1 -->"
E = lambda x: html.escape(str(x), quote=True)  # noqa: E731
NR = "NR"  # not recorded / not measurable

# ledger state -> (css key for bars and swatches, pill class, Russian label, group)
STATE_VIEW = {
    "done": ("accepted", "state-accepted", "принято", "done"),
    "verified": ("registry-review", "state-registry-review", "на ревью", "review"),
    "in_progress": ("registry-progress", "state-registry-progress", "в работе", "wip"),
    "implemented": ("registry-progress", "state-registry-progress", "код готов, ждёт тестов", "wip"),
    "not_run": ("registry-progress", "state-registry-progress", "тесты не запускались", "wip"),
    "failed": ("registry-blocked", "state-registry-blocked", "тесты красные", "red"),
    "todo": ("registry-open", "state-registry-open", "не начато", "todo"),
    "superseded": ("registry-superseded", "state-registry-superseded", "заменено планом", "superseded"),
}
GROUPS = [("done", "принято"), ("review", "на ревью"), ("wip", "в работе + код готов"), ("red", "тесты красные"), ("todo", "не начато"),
          ("superseded", "заменено планом")]
GROUP_CSS = {"done": ("accepted", "state-accepted"), "review": ("registry-review", "state-registry-review"),
             "wip": ("registry-progress", "state-registry-progress"), "red": ("registry-blocked", "state-blocked"),
             "todo": ("registry-open", "state-registry-open"), "superseded": ("registry-superseded", "state-registry-superseded")}
REMAINING = {"todo": "Код не написан.", "in_progress": "Закончить код и записать коммит с Task:.",
             "implemented": "Тесты не запускались на последнем коммите задачи.", "not_run": "Тесты не запущены (см. основание).",
             "verified": "Ревью не принято для последнего коммита.", "failed": "Исправить код и повторить тесты.", "done": "—",
             "superseded": "— (заменено планом)"}
NEXT_CMD = {"todo": "/next", "in_progress": "/next", "implemented": "/verify", "not_run": "/verify", "verified": "/ship",
            "failed": "исправить, затем /verify", "done": "—", "superseded": "—"}
RISK_RU = {"trivial": "тривиально", "local": "локально", "multi-module": "несколько модулей", "risky": "опасно"}
BUCKETS = ("code", "docs", "tests", "other")


def view(state: str) -> tuple:
    return STATE_VIEW.get(state, ("registry-open", "state-registry-open", str(state), "todo"))


# ---------------------------------------------------------------- git access

def safe_url(url: str) -> str:
    """A remote URL without credentials: userinfo (user:token@), query and fragment are dropped, so a token embedded
    in the remote never reaches a report that the pre-commit hook puts into git history."""
    u = re.split(r"[?#]", (url or "").strip(), 1)[0]  # query and fragment first, for every URL form
    if "://" not in u:
        return re.sub(r"^[^@/\s]+:[^@/\s]*@", "", u) if re.match(r"^[^@/\s]+:[^@/\s]*@", u) else u
    scheme, rest = u.split("://", 1)
    rest = re.split(r"[?#]", rest, 1)[0]
    host_path = rest.split("/", 1)
    host = host_path[0].rsplit("@", 1)[-1]
    return scheme + "://" + host + ("/" + host_path[1] if len(host_path) > 1 else "")


BUDGET_S = 60.0
DEADLINE = float("inf")  # set to now + BUDGET_S when main() starts


class Git:
    """Read-only git access in one folder; every failure becomes an empty answer, never an exception."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._commits: dict[str, tuple | None] = {}
        self._pushed: dict[str, bool] = {}

    def run(self, *args) -> tuple[int, str]:
        if time.monotonic() > DEADLINE:  # one time budget for the whole report: later git calls answer 'unknown'
            return 124, ""
        env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
        try:
            r = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                               cwd=self.root, env=env, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return 127, ""
        return r.returncode, r.stdout.strip()

    def out(self, *args) -> str:
        rc, o = self.run(*args)
        return o if rc == 0 else ""

    def snapshot(self) -> dict:
        s = {"head": self.out("rev-parse", "HEAD"), "branch": self.out("rev-parse", "--abbrev-ref", "HEAD"),
             "upstream": self.out("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"),
             "ahead": None, "behind": None, "remote_url": ""}
        s["short"] = s["head"][:7]
        s["remote"] = s["upstream"].split("/", 1)[0] if s["upstream"] else "origin"
        s["remote_url"] = safe_url(self.out("remote", "get-url", s["remote"]) or self.out("config", "--get", f"remote.{s['remote']}.url"))
        if s["upstream"]:
            lr = self.out("rev-list", "--left-right", "--count", "HEAD...@{upstream}").split()
            if len(lr) == 2 and all(x.isdigit() for x in lr):
                s["ahead"], s["behind"] = int(lr[0]), int(lr[1])
        return s

    def is_pushed(self, sha: str, upstream: str) -> bool:
        """True only when git confirms the commit is an ancestor of the upstream tip."""
        if sha not in self._pushed:
            self._pushed[sha] = bool(upstream) and self.run("merge-base", "--is-ancestor", sha, "@{upstream}")[0] == 0
        return self._pushed[sha]

    def commit(self, sha: str) -> tuple | None:
        """(committer timestamp, {bucket: added+deleted lines}) or None when the commit is unknown here."""
        if sha not in self._commits:
            rc, o = self.run("show", "--numstat", "--format=%ct", sha)
            info = None
            lines = o.splitlines()
            if rc == 0 and lines and lines[0].isdigit():
                counts = dict.fromkeys(BUCKETS, 0)
                for ln in lines[1:]:
                    parts = ln.split("\t", 2)
                    if len(parts) == 3:
                        n = sum(int(x) for x in parts[:2] if x.isdigit())
                        counts[bucket_of(parts[2])] += n
                info = (int(lines[0]), counts)
            self._commits[sha] = info
        return self._commits[sha]

    def log(self, n: int = 30) -> list[dict]:
        o = self.out("log", f"-{n}", "--numstat",
                     "--format=%x1e%h%x1f%ct%x1f%s%x1f%(trailers:key=Task,valueonly,separator=%x2c)%x1f")
        res = []
        for rec in o.split("\x1e"):
            f = rec.split("\x1f", 4)
            if len(f) == 5 and f[1].isdigit():
                res.append({"sha": f[0], "ts": int(f[1]), "subject": f[2], "task": f[3].strip().replace("\n", ", "),
                            "files": sum(1 for ln in f[4].splitlines() if ln.count("\t") >= 2)})
        return res


def bucket_of(path: str) -> str:
    p = "/" + path.replace("\\", "/").lower().lstrip("/")
    name = p.rsplit("/", 1)[-1]
    if "/tests/" in p or "test_" in name or "_test" in name or ".spec." in name:
        return "tests"
    if name.endswith((".md", ".rst", ".txt")) or p.startswith(("/docs/", "/core/", "/reports/")):
        return "docs"
    if name.endswith((".json", ".yaml", ".yml", ".lock", ".toml", ".ini", ".cfg")) or p.startswith(("/.github/", "/.harness/")):
        return "other"
    return "code"


# ---------------------------------------------------------------- small helpers

def pct(n: int, m: int) -> float:
    return n / m * 100 if m else 0.0


def fmt_pct(v: float) -> str:
    return f"{v:.1f}".replace(".", ",") + "%"


def fmt_ts(ts: int | None) -> str:
    return dt.datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M") if ts else NR


def fmt_dur(sec: float) -> str:
    sec = max(0, int(sec))
    if sec < 3600:
        return f"{max(1, sec // 60)} мин"
    if sec < 86400:
        return f"{sec // 3600} ч {sec % 3600 // 60} мин"
    return f"{sec // 86400} д {sec % 86400 // 3600} ч"


def tzinfo_text() -> str:
    n = dt.datetime.now().astimezone()
    return f"{n.strftime('%Y-%m-%d %H:%M')} (UTC{n.strftime('%z')[:3]}:{n.strftime('%z')[3:]})"


def safe_name(project: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(project)) or "project"


def all_tasks(d: dict) -> list[tuple[dict, dict]]:
    return [(s, t) for s in d.get("sprints", []) for t in s.get("tasks", [])]


def group_counts(tasks: list[dict]) -> dict:
    c = dict.fromkeys((g for g, _ in GROUPS), 0)
    for t in tasks:
        c[view(t.get("state", "todo"))[3]] += 1
    return c


def seg_bar(parts: list[tuple[str, int, str]], total: int, cls: str = "bar") -> str:
    """parts: (css key, count, tooltip). Segments with a zero count are skipped."""
    segs = "".join(f'<span class="{k}" style="width:{pct(n, total):.4f}%" title="{E(tip)}"></span>' for k, n, tip in parts if n)
    return f'<div class="{cls}">{segs}</div>'


def commits_info(g: Git, t: dict) -> tuple[list[str], list[int], dict]:
    """Recorded commits of a task, their timestamps and summed bucket line counts (known commits only)."""
    shas = [c for c in t.get("commits") or [] if isinstance(c, str)]
    stamps, counts = [], dict.fromkeys(BUCKETS, 0)
    for c in shas:
        info = g.commit(c)
        if info:
            stamps.append(info[0])
            for k in BUCKETS:
                counts[k] += info[1][k]
    return shas, stamps, counts


def shares_text(counts: dict | None) -> tuple[str, str]:
    """('a / b / c / d %' or 'NR / NR / NR / NR', raw-count tooltip)."""
    total = sum(counts.values()) if counts else 0
    if not total:
        return " / ".join([NR] * 4), "нет точно связанных коммитов или изменённых строк"
    shown = " / ".join(f"{round(counts[k] / total * 100)}" for k in BUCKETS) + " %"
    tip = "; ".join(f"{k}: {counts[k]} строк" for k in BUCKETS) + f"; всего {total} (добавлено + удалено)"
    return shown, tip


# ---------------------------------------------------------------- facts

def gather(d: dict, g: Git) -> dict:
    snap = g.snapshot()
    pairs = all_tasks(d)
    total = len(pairs)
    done = [t for _, t in pairs if t.get("state") == "done"]
    checked = bool(snap["upstream"])
    shipped = [t for t in done if t.get("commits") and all(g.is_pushed(c, snap["upstream"]) for c in t["commits"])] if checked else []
    ap = d.get("approval") or {}
    if snap["head"] and ap.get("head") == snap["head"]:
        gpt = "APPROVE для текущего коммита"
    elif ap.get("head"):
        gpt = "APPROVE устарел (был новый коммит)"
    else:
        gpt = "ещё не было"
    if not checked:
        remote = "отправка на remote не проверена"
    elif snap["ahead"] is None:
        remote = "расхождение с upstream не определено"
    elif snap["ahead"] == 0 and snap["behind"] == 0:
        remote = "синхронно с upstream"
    else:
        remote = f"впереди на {snap['ahead']}, позади на {snap['behind']}"
    return {"source": d.get("source") or "", "snap": snap, "total": total, "done": len(done), "shipped": len(shipped) if checked else None,
            "checked": checked, "gpt": gpt, "remote": remote, "groups": group_counts([t for _, t in pairs])}


# ---------------------------------------------------------------- sections

def sec_hero(d: dict, f: dict, root: Path) -> str:
    s = f["snap"]
    if f["checked"]:
        lead = f'<b>{fmt_pct(pct(f["shipped"], f["total"]))}</b> проекта закрыто и отправлено ({f["shipped"]} из {f["total"]} задач: принято и все коммиты есть на remote)'
    else:
        lead = f'<b>{NR}</b> проекта закрыто и отправлено: отправка на remote не проверена'
    sprints = d.get("sprints", [])
    closed = sum(1 for x in sprints if x.get("state") == "closed")
    gr = f["groups"]
    tiles = [(f'{f["done"]}/{f["total"]}', "принято"), (gr["review"], "на ревью"), (gr["wip"] + gr["red"], "в работе"),
             (gr["todo"], "не начато"), *([(gr["superseded"], "заменено планом")] if gr["superseded"] else []),
             (f"{closed}/{len(sprints)}", "спринтов закрыто"), (f["gpt"], "GPT-PM"), (f["remote"], "remote")]
    up = f'upstream {s["upstream"]}' if s["upstream"] else "upstream не настроен"
    return (f'<header class="hero" id="program-progress"><h1>Визуальный прогресс всей программы</h1>'
            f'<p class="headline">{lead} · <b>принято {f["done"]}/{f["total"]} задач</b></p>'
            f'<p class="muted">Снимок: {E(tzinfo_text())} · проект {E(d.get("project", NR))} · папка {E(root)} · '
            f'ветка {E(s["branch"] or NR)} · HEAD {E(s["short"] or NR)} · {E(up)}</p>'
            '<div class="metrics">' + "".join(f'<div class="metric"><b>{E(a)}</b><span>{E(b)}</span></div>' for a, b in tiles)
            + "</div></header>")


def sec_provenance(d: dict, f: dict, root: Path) -> str:
    s = f["snap"]
    ab = "" if s["ahead"] is None else f" · впереди {s['ahead']}, позади {s['behind']} относительно {s['upstream']}"
    git_txt = f'{s["remote_url"] or "remote не настроен"} · {s["branch"] or NR} @ {s["head"] or NR}{ab}'
    return ('<!-- report-conform:begin --><div class="rc-bar"><dl>'
            f'<dt>Проект</dt><dd>{E(d.get("project", NR))}</dd><dt>Папка</dt><dd>{E(root)}</dd><dt>Git</dt><dd>{E(git_txt)}</dd></dl>'
            '<button id="rc-copy" class="rc-copy" type="button" data-idle="Копировать страницу" data-done="Скопировано" '
            'data-fail="Выделите вручную">Копировать страницу</button></div><!-- report-conform:end -->')


def sec_program(f: dict) -> str:
    n, m = f["done"], f["total"]
    p = pct(n, m)
    gr = f["groups"]
    sup = gr["superseded"]
    rows = []
    for key, label in GROUPS:
        css, pill = GROUP_CSS[key]
        rows.append(f'<div class="gate-chart-row"><span class="gate-chart-name">{E(label)}</span><strong>{gr[key]}</strong>'
                    f'{seg_bar([(css, gr[key], f"{label}: {gr[key]} из {m}")], m)}'
                    f'<span class="pill {pill}">{fmt_pct(pct(gr[key], m))}</span></div>')
    return ('<section class="panel program-visual" id="program-visual"><h2>Прогресс программы</h2>'
            '<nav class="chart-jump" aria-label="Навигация по графикам"><a href="#program-visual">Вся программа</a>'
            '<a href="#gate-charts">По состояниям</a><a href="#sprint-distribution">По спринтам</a><a href="#active-sprint">Активный спринт</a></nav>'
            '<p class="chart-note">Принято = задача в состоянии done: тесты зелёные на последнем коммите и ревью принято.</p>'
            f'<div class="program-total"><b>{fmt_pct(p)}</b><span>принято {n} из {m} задач</span></div>'
            + seg_bar([("accepted", n, f"принято: {n}"), ("registry-superseded", sup, f"заменено планом: {sup}"),
                       ("remaining", m - n - sup, f"осталось: {m - n - sup}")], m, "bar overall-progress")
            + f'<p class="progress-caption"><code>{n} ÷ {m} × 100 = {fmt_pct(p)}</code> · знаменатель {m} — все места задач в плане '
            + f'({E(f.get("source") or ".harness/ledger.json")}, все спринты; из них {sup} заменено планом: они входят в знаменатель и показаны отдельно).</p><h3 id="gate-charts">Задачи по состояниям</h3>'
            '<p class="gate-chart-note">Один цвет означает одно состояние во всех графиках страницы.</p>'
            '<div class="gate-chart-list">' + "".join(rows) + "</div></section>")


def sprint_row(g: Git, s: dict, active_id: str | None) -> str:
    tasks = s.get("tasks", [])
    m = len(tasks)
    gr = group_counts(tasks)
    stamps, counts = [], dict.fromkeys(BUCKETS, 0)
    for t in tasks:
        _, st, cn = commits_info(g, t)
        stamps += st
        for k in BUCKETS:
            counts[k] += cn[k]
    parts = [(GROUP_CSS[k][0], gr[k], f"{lab}: {gr[k]}/{m}") for k, lab in GROUPS]
    aria = f'{s["id"]}: ' + "; ".join(f"{lab} {gr[k]}/{m}" for k, lab in GROUPS if gr[k])
    if not stamps:
        dur, dur_tip = NR, "нет коммитов, записанных на задачах спринта"
    elif s["id"] == active_id:
        dur, dur_tip = fmt_dur(time.time() - min(stamps)), f"от первого коммита {fmt_ts(min(stamps))} до сейчас"
    else:
        dur, dur_tip = fmt_dur(max(stamps) - min(stamps)), f"от {fmt_ts(min(stamps))} до {fmt_ts(max(stamps))}"
    shown, tip = shares_text(counts)
    summary = f'{gr["done"]}/{m} принято · {gr["review"]} на ревью · {gr["wip"] + gr["red"]} в работе' + (f' · {gr["superseded"]} заменено' if gr["superseded"] else "")
    return (f'<div class="sprint-chart-row" id="sp-{E(s["id"])}"><span class="sprint-chart-name"><b>Спринт {E(s["id"])} — {m} задач</b> '
            f'{E(s.get("title", ""))}</span>'
            f'<div class="sprint-chart-bar" role="img" aria-label="{E(aria)}">'
            + "".join(f'<span class="{k}" style="width:{pct(n, m):.4f}%" title="{E(tp)}"></span>' for k, n, tp in parts if n)
            + f'</div><strong>{E(summary)}</strong><span class="sprint-duration" title="{E(dur_tip)}">{E(dur)}</span>'
            f'<span class="sprint-ratio" title="{E(tip)}">{E(shown)}</span></div>')


def sec_sprints(d: dict, g: Git) -> str:
    act = ledger.active_sprint(d) if d.get("sprints") else None
    legend = "".join(f'<span><i class="swatch {GROUP_CSS[k][0]}"></i> {E(lab)}</span>' for k, lab in GROUPS)
    rows = "".join(sprint_row(g, s, act["id"] if act else None) for s in d.get("sprints", [])) or '<p class="muted">Спринтов в реестре нет.</p>'
    return ('<section class="panel" id="sprint-distribution"><h2 id="sprint-charts">Распределение по спринтам</h2>'
            f'<div class="sprintlegend registry-legend">{legend}</div>'
            '<p class="muted">Длительность: от времени первого до времени последнего коммита, записанного на задачах спринта '
            f'(активный спринт: до текущего момента; часовой пояс UTC{E(dt.datetime.now().astimezone().strftime('%z')[:3] + ':' + dt.datetime.now().astimezone().strftime('%z')[3:])}). '
            'Доли code / docs / tests / other считаются по строкам (добавлено + удалено, git numstat) этих коммитов.</p>'
            '<div class="sprint-chart-list"><div class="sprint-chart-columns"><span>Спринт</span><span>Статус задач</span>'
            '<span>Итог</span><span>Длительность</span><span>code / docs / tests / other</span></div>' + rows + "</div></section>")


def evidence_html(t: dict) -> str:
    v, r, rt = t.get("verify") or {}, t.get("review") or {}, t.get("routing") or {}
    out = []
    out.append(f'<p>Тесты: <b>{E(v.get("result", "NOT_RUN"))}</b>' + (f' — {E(v.get("evidence"))}' if v.get("evidence") else "") + "</p>")
    out.append(f'<p>Ревью: <b>{E(r.get("result", NR))}</b>' + (f', замечаний: {E(r.get("findings", NR))}' if r else "") + "</p>")
    revs = rt.get("reviewers") or []
    if revs:
        out.append("<p>Ревьюеры: " + "; ".join(f'{E(x.get("agent", NR))} ({E(x.get("reason", NR))})' for x in revs if isinstance(x, dict)) + "</p>")
    if rt:
        out.append(f'<p>Риск: {E(RISK_RU.get(rt.get("risk"), rt.get("risk", NR)))} — {E(rt.get("risk_reason", NR))}</p>')
    if t.get("run"):
        out.append(f'<p>Запуск: <code>{E(t["run"])}</code></p>')
    return "".join(out)


def risk_text(t: dict) -> str:
    rt = t.get("routing") or {}
    risk = rt.get("risk") or t.get("risk")
    return f'{RISK_RU.get(risk, risk)}' if risk and risk not in ("trivial", "local") else "—"


def sec_active(d: dict, g: Git) -> str:
    s = ledger.active_sprint(d) if d.get("sprints") else None
    s = s or (d["sprints"][-1] if d.get("sprints") else None)
    head = '<section class="panel" id="active-sprint"><h2>Активный спринт</h2>'
    if not s:
        return head + '<p class="muted">В реестре нет спринтов.</p></section>'
    tasks = s.get("tasks", [])
    n = sum(1 for t in tasks if t.get("state") == "done")
    sup = sum(1 for t in tasks if t.get("state") == "superseded")
    rows = []
    for t in tasks:
        st = t.get("state", "todo")
        css, pill, label, _ = view(st)
        shas, stamps, counts = commits_info(g, t)
        window = f'{fmt_ts(min(stamps))} → {fmt_ts(max(stamps))}' if stamps else NR
        cm = " ".join(f"<code>{E(c[:7])}</code>" for c in shas) or "—"
        shown, tip = shares_text(counts if stamps else None)
        rows.append(
            f'<tr data-state="{E(st)}"><td><code>{E(t.get("id", NR))}</code><br><span class="muted">позиция реестра</span><br>{E(t.get("title", ""))}</td>'
            "<td><ol>" + "".join(f"<li>{E(c)}</li>" for c in t.get("criteria") or []) + "</ol></td>"
            f'<td><span class="pill {pill}">{E(label)}</span></td>'
            f"<td><details><summary>Основание</summary>{evidence_html(t)}</details></td>"
            f"<td>{E(REMAINING.get(st, NR))}</td><td>{E(risk_text(t))}</td><td>{E(NEXT_CMD.get(st, NR))}</td>"
            f"<td>{cm}<br><span class=\"muted\">{E(window)}</span></td><td title=\"{E(tip)}\">{E(shown)}</td></tr>")
    cols = ["Позиция / задача", "Критерий приёмки", "Фактический статус", "Код / test / Git evidence", "Остаток", "Зависимость",
            "Владелец / следующий шаг", "Exact task-ID commits / окно", "Доли code/docs/tests/other"]
    return (head + f'<p class="active-summary"><b>Спринт {E(s["id"])}</b> — {E(s.get("title", ""))}: принято {n} из {len(tasks)}' + (f', заменено планом {sup}' if sup else "") + ', '
            f'состояние спринта: {E(s.get("state", NR))}.</p><div class="wrap"><table class="active-sprint-table"><thead><tr>'
            + "".join(f"<th>{c}</th>" for c in cols) + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div></section>")


def running_agents(root: Path) -> list[dict] | None:
    """Unfinished entries of agents/active.json (written by agent_visibility.py) that belong to this project. Read-only copy
    of the small reader: same state folder rule as _agentio (HARNESS_HOOK_STATE, else the machine config, else
    ~/.harness-hooks), never creates it. A missing file is []; any other failure returns None and sets AGENTS_ERROR."""
    global AGENTS_ERROR
    AGENTS_ERROR = ""
    try:
        state = os.environ.get("HARNESS_HOOK_STATE")
        if not state:  # the hooks read it from the machine config (_agentio.load_machine_config), so do the same
            cfg = Path(os.environ.get("HARNESS_HOOK_CONFIG") or Path.home() / ".harness-hooks" / "config.json")
            try:
                v = json.loads(cfg.read_text(encoding="utf-8-sig")).get("HARNESS_HOOK_STATE")
                if isinstance(v, str) and v.strip() and not v.lstrip().startswith("<"):
                    state = os.path.expandvars(os.path.expanduser(v))
            except (OSError, ValueError, AttributeError):
                state = None
        base = Path(state) if state else Path.home() / ".harness-hooks"
        try:
            agents = json.loads((base / "agents" / "active.json").read_text(encoding="utf-8")).get("agents", [])
        except FileNotFoundError:
            return []
        # entries store the MAIN checkout (agent_visibility), so a report built in a linked worktree compares with that
        top = Path(root)
        r = subprocess.run(["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            common = Path(r.stdout.strip())
            top = common.parent if common.name == ".git" else common
        key = os.path.normcase(os.path.normpath(str(top.resolve())))
        return [e for e in agents if isinstance(e, dict) and not e.get("stop")
                and os.path.normcase(os.path.normpath(str(e.get("project") or "-"))) == key]
    except Exception as ex:  # noqa: BLE001
        AGENTS_ERROR = f"{type(ex).__name__}: {ex}"
        return None


AGENTS_ERROR = ""


def sec_agents(root: Path) -> str:
    head = '<section class="panel" id="running-agents"><h2>Запущенные агенты</h2>'
    entries = running_agents(root)
    if entries is None:
        return head + f'<p class="muted">состояние не прочитано: {E(AGENTS_ERROR)}</p></section>'
    rows = []
    now = time.time()
    for e in entries:
        try:  # one bad entry (a string start/activity, a missing field) is skipped, never crashes the report
            start = float(e.get("start") or 0)
            last = max(start, float(e.get("activity") or 0))
            stale = now - last >= 600  # same limit as agent_visibility.UNKNOWN_AFTER_S
            state = "статус неизвестен (нет событий 10+ минут)" if stale else ("подтверждён активным" if e.get("activity") else "старт замечен")
            when = f"{fmt_ts(int(start))} ({fmt_dur(now - start)})" if start else NR
            rows.append(f'<tr><td><code>{E(e.get("type", NR))}</code></td><td>{E(e.get("task", NR))}</td>'
                        f'<td>{E(e.get("model", NR))} / {E(e.get("effort", NR))}</td><td>{E(state)}</td><td>{E(when)}</td></tr>')
        except Exception:  # noqa: BLE001
            continue
    if not rows:
        return head + '<p class="muted">Сейчас агенты этого проекта не запущены.</p></section>'
    cols = ["Роль (тип агента)", "Задача", "Модель / усилие", "Состояние", "Старт (длительность)"]
    return (head + '<div class="wrap"><table><thead><tr>' + "".join(f"<th>{c}</th>" for c in cols) + "</tr></thead><tbody>"
            + "".join(rows) + '</tbody></table></div><p class="muted">Источник: agents/active.json в папке состояния хуков; '
            'только записи без STOP, привязанные к этому проекту.</p></section>')


def sec_stories(d: dict) -> str:
    pairs = all_tasks(d)
    present: dict[str, int] = {}
    rows = []
    for s, t in pairs:
        st = t.get("state", "todo")
        present[st] = present.get(st, 0) + 1
        _, pill, label, _g = view(st)
        crit = t.get("criteria") or []
        v = t.get("verify") or {}
        blob = " ".join([str(t.get("id", "")), str(t.get("title", "")), label, st, str(v.get("evidence", "")), *map(str, crit),
                         *[str(c)[:7] for c in t.get("commits") or []]]).lower()
        rows.append(
            f'<tr data-search="{E(blob)}" data-state="{E(st)}"><td><code>{E(t.get("id", NR))}</code><br>{E(t.get("title", ""))}<br>'
            f'<span class="muted">спринт {E(s["id"])}</span></td><td><span class="pill {pill}">{E(label)}</span></td>'
            f'<td>{E(str(v.get("evidence", "—"))[:200])}</td><td>{E(REMAINING.get(st, NR))}</td><td>{E(risk_text(t))}</td>'
            f"<td>{E(NEXT_CMD.get(st, NR))}</td>"
            f'<td><details><summary>Критерии DoD ({len(crit)})</summary><ol>' + "".join(f"<li>{E(c)}</li>" for c in crit)
            + "</ol></details></td></tr>")
    opts = '<option value="">Все состояния</option>' + "".join(
        f'<option value="{E(k)}">{E(view(k)[2])} ({n})</option>' for k, n in sorted(present.items(), key=lambda kv: ledger.STATES.index(kv[0]) if kv[0] in ledger.STATES else 99))
    legend = "".join(f'<span><i class="swatch {GROUP_CSS[k][0]}"></i> {E(lab)}</span>' for k, lab in GROUPS)
    cols = ["Задача", "Состояние", "Evidence", "Остаток", "Зависимость", "Следующий шаг", "Критерии"]
    return ('<section class="panel" id="stories"><h2>Все истории и критерии</h2>'
            f'<p class="muted">Все задачи всех спринтов: {len(pairs)}. Поиск работает по ID, названию, состоянию, критериям и evidence.</p>'
            f'<div class="sprintlegend evidence-legend">{legend}</div>'
            '<div class="toolbar"><input id="q" placeholder="Найти ID, название или evidence" type="search">'
            f'<select id="st">{opts}</select><button id="open" type="button">Раскрыть все</button>'
            '<button id="close" type="button">Свернуть все</button></div>'
            '<p class="muted hide" id="empty">Ничего не найдено.</p><div class="wrap"><table><thead><tr>'
            + "".join(f"<th>{c}</th>" for c in cols) + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div></section>")


def sec_freshness(d: dict, f: dict, root: Path) -> str:
    tasks = [t for _, t in all_tasks(d)]
    impl = sum(1 for t in tasks if t.get("commits"))
    rev = sum(1 for t in tasks if (t.get("review") or {}).get("result") == "accepted")
    run = sum(1 for t in tasks if (t.get("verify") or {}).get("result") == "pass")
    not_run = len(tasks) - run
    lp = ledger.ledger_path()
    items = [
        f"Источники: реестр {E(lp)}; git в папке {E(root)} (log, rev-list, merge-base, show --numstat).",
        f"Дата отсчёта: {E(tzinfo_text())}; HEAD {E(f['snap']['head'] or NR)}.",
        f"Формула прогресса: принято ÷ все задачи реестра = {f['done']} ÷ {f['total']}. Знаменатель — все места задач во всех спринтах (включая заменённые планом, SUPERSEDED: они показаны отдельной категорией «заменено планом»).",
        "Формула «закрыто и отправлено»: задача принята и каждый её коммит — предок @{upstream} (git merge-base --is-ancestor). "
        "Без upstream или без git значение не оценивается.",
        "Доли code/docs/tests/other: добавленные + удалённые строки коммитов, записанных на задачах (git numstat); без таких коммитов — NR.",
        "Длительность: время коммитера первого и последнего коммитов спринта; у активного спринта конец — текущий момент.",
        "Не измерялось: работа в runtime, CI, выпуск (release), содержимое тестов; отчёт не запускает тесты и не вызывает модели.",
    ]
    ev = [("IMPLEMENTED", impl, "у задачи есть записанный коммит"), ("REVIEWED", rev, "ревью принято (review accepted)"),
          ("RUN", run, "тесты запускались и прошли (verify pass)"), ("NOT_RUN", not_run, "тесты не запускались или не прошли")]
    return ('<section class="panel" id="freshness"><h2>Свежесть, формулы и ограничения</h2><ul>'
            + "".join(f"<li>{x}</li>" for x in items) + '</ul><h3>Уровни доказательств (отдельно, не суммируются)</h3><ul>'
            + "".join(f"<li><b>{k}</b>: {n} из {len(tasks)} — {E(tx)}</li>" for k, n, tx in ev) + "</ul></section>")


def alerts_of(d: dict, g: Git, snap: dict) -> list[str]:
    evs = d.get("events", [])
    alerts = []
    errs = [e.get("message", "") for e in ledger.open_hook_errors(d)]  # resolved and acknowledged errors stay in history only
    try:
        log = ledger.ledger_path().parent / "hook-error.log"
        if log.exists():  # shell-level failures never reach the ledger
            # every message the ledger already has (open, resolved or acknowledged) is decided there, not re-raised here
            known = set(errs) | {e.get("message", "") for e in evs if e.get("kind") == "hook-error"}
            acks = [str(e.get("at", "")) for e in evs if e.get("kind") == "hook-error-ack"]
            last_ack = max(acks) if acks else ""
            for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
                stamp, _, rest = line.partition(" ")
                msg = rest.strip() if rest else line.strip()
                if last_ack and stamp and stamp <= last_ack:
                    continue  # log-only line acknowledged by ack-errors
                if msg and msg not in known:
                    errs.append(msg)
                    known.add(msg)
    except OSError:
        pass
    if errs:
        alerts.append(f"Ошибки хуков: {len(errs)}, последняя: {errs[-1]}")
    skips = [e for e in evs if e.get("kind") == "approval-skipped"]
    if skips:
        alerts.append(f"Проверка GPT-PM перед push была отключена {len(skips)} раз(а), последний {skips[-1].get('at', NR)}")
    if snap["head"] and not any(e.get("sha") == snap["head"] for e in evs) and "Task:" in g.out("log", "-1", "--format=%B"):
        alerts.append(f"Коммит {snap['short']} помечен задачей, но реестр его не записал")
    return alerts


def sec_updates(d: dict, g: Git, f: dict) -> str:
    alerts = "".join(f'<p class="note">{E(a)}</p>' for a in alerts_of(d, g, f["snap"]))
    rows = "".join(f'<tr><td><code>{E(c["sha"])}</code></td><td>{E(fmt_ts(c["ts"]))}</td><td>{E(c["subject"])}</td>'
                   f'<td>{E(c["task"] or "—")}</td><td>{c["files"]}</td></tr>' for c in g.log(30))
    if not rows:
        rows = '<tr><td colspan="5">git-журнал недоступен (NR)</td></tr>'
    return ('<section class="panel" id="updates"><h2>Журнал обновлений по коммитам</h2>' + alerts
            + '<p class="muted">Последние 30 коммитов, новые сверху.</p><div class="wrap"><table><thead><tr><th>Коммит</th><th>Время</th>'
            "<th>Тема</th><th>Task</th><th>Файлов</th></tr></thead><tbody>" + rows + "</tbody></table></div></section>")


# ---------------------------------------------------------------- assets and page

def asset_dir() -> Path:
    here = Path(__file__).resolve().parent / "report"
    return here if here.is_dir() else Path(__file__).resolve().parent.parent / "harness" / "report"


def read_asset(name: str) -> str:
    try:
        return (asset_dir() / name).read_text(encoding="utf-8")
    except OSError:
        return ""  # page still renders, just unstyled


def render(d: dict, root) -> str:
    """The whole page as text. Pure: reads git and assets, writes nothing."""
    root = Path(root)
    g = Git(root)
    f = gather(d, g)
    title = f'{d.get("project", NR)} — программа и все спринты'
    body = (sec_hero(d, f, root) + sec_provenance(d, f, root) + sec_program(f) + sec_sprints(d, g) + sec_active(d, g) + sec_agents(root)
            + sec_stories(d) + sec_freshness(d, f, root) + sec_updates(d, g, f))
    return ("<!doctype html>\n" + MARKER + '\n<html lang="ru"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{E(title)}</title><style>{read_asset('report.css').replace('</style', '<\\/style')}</style></head>"
            f'<body><main>{body}</main>'
            f"<script>{read_asset('filter.js').replace('</script', '<\\/script')}</script>"
            f"<script>{read_asset('copy.js').replace('</script', '<\\/script')}</script></body></html>\n")


def atomic_write_text(path: Path, text: str, retries: int = 5) -> None:
    """Temp file next to the target, then os.replace (retried briefly on Windows PermissionError)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        for attempt in range(retries):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == retries - 1:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def is_ours(path: Path) -> bool:
    """True when the file is absent, or carries the report marker in its first 400 bytes."""
    if not path.exists():
        return True
    try:
        with open(path, "rb") as fh:
            return MARKER.encode() in fh.read(400)
    except OSError:
        return False


def default_out(d: dict, root: Path) -> Path:
    return root / "reports" / f"{safe_name(d.get('project', 'project'))}_PROGRAM_STATUS_LIVE.ru.html"


def busy_git_state(root: Path) -> str:
    """Name of a rebase / merge / cherry-pick / revert in progress ('' when none): the hook leaves such commits alone."""
    gd = Git(root).out("rev-parse", "--git-dir")
    if not gd:
        return ""
    g = Path(gd) if Path(gd).is_absolute() else root / gd
    for name in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
        if (g / name).exists():
            return name
    return ""


def main(argv=None) -> int:
    """Exit 0 = report written (or skipped by design); 1 = a real failure, reason on stderr. The pre-commit hook
    ignores the code for the commit itself (it always lets the commit through) but logs the reason."""
    global DEADLINE
    DEADLINE = time.monotonic() + BUDGET_S
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out")
    ap.add_argument("--stage", action="store_true",
                    help="pre-commit mode: after writing, `git add` the report into the commit being made; "
                         "skipped during rebase/merge/cherry-pick")
    a = ap.parse_args(argv)
    try:
        return run(a)
    except Exception as e:  # noqa: BLE001
        print(f"report: unexpected error {type(e).__name__}: {e}", file=sys.stderr)
        return 1


def merge_plan_and_ledger(plan: dict, led: dict) -> dict:
    """Whole program = every plan task plus every ledger sprint. A task id in both is counted once and the ledger state wins
    (it is the newer, evidence-bound record). Without ledger sprints the plan is returned as it is."""
    led_sprints = [sp for sp in led.get("sprints", []) if sp.get("tasks")]
    if not led_sprints:  # no ledger tasks to add, but the ledger's approval and events still belong to the report
        return {**plan, "approval": led.get("approval") or plan.get("approval"), "events": led.get("events") or plan.get("events", [])}
    led_ids = {t.get("id") for sp in led_sprints for t in sp["tasks"]}
    led_sprint_ids = {sp.get("id") for sp in led_sprints}
    merged = []
    announced: dict = {}  # sprint id -> placements announced by a not-expanded roadmap row
    for sp in plan.get("sprints", []):
        if sp.get("expanded") is False and sp.get("id") in led_sprint_ids:
            # a not-expanded roadmap row keeps its announced size: the ledger tasks stay real, the rest become UNVERIFIED placeholders
            announced[sp.get("id")] = len(sp.get("tasks", []))
            continue
        kept = [t for t in sp.get("tasks", []) if t.get("id") not in led_ids]
        if kept:
            merged.append({**sp, "tasks": kept})
    taken = {sp.get("id") for sp in merged}
    ledger_part = []
    for sp in led_sprints:
        n_plan = announced.get(sp.get("id"), 0)
        k = len(sp["tasks"])
        if n_plan > k:  # k > N keeps the k ledger tasks only
            used = {t.get("id") for t in sp["tasks"]}
            extra = []
            for j in range(k + 1, n_plan + 1):
                tid = f"{sp['id']}-PLACEHOLDER-{j}"
                while tid in used or tid in led_ids:
                    tid += "x"
                used.add(tid)
                extra.append({"id": tid, "title": f"UNVERIFIED: planned placement {j} of {n_plan} not recorded in the ledger",
                              "criteria": [], "run": "", "risk": "local", "journey": False, "state": "todo", "commits": [],
                              "verify": None, "review": None, "routing": None, "tokens": None})
            sp = {**sp, "tasks": list(sp["tasks"]) + extra}
        ledger_part.append({**sp, "id": sp["id"] if sp.get("id") not in taken else f"{sp['id']}-ledger"})
        taken.add(ledger_part[-1]["id"])
    merged = ledger_part + merged  # ledger sprints first: the first sprint that is not closed is the active one, and that is the one being worked
    return {**led, "sprints": merged, "source": f"{plan.get('source') or 'PROJECT_PLAN.md'} + .harness/ledger.json"}


def run(a) -> int:
    top = Git(Path.cwd()).out("rev-parse", "--show-toplevel")
    root = Path(top) if top else Path.cwd()
    if a.stage:
        busy = busy_git_state(root)
        if busy:
            print(f"report: skipped ({busy} in progress)")
            return 0
    ledger_error = ""
    try:
        plan_file = root / "PROJECT_PLAN.md"
        d = ledger.load()
        if plan_file.is_file():  # plan-based project: the plan is the whole program; a ledger (if any) adds its sprints on top
            import plan_ledger
            pd = plan_ledger.load(plan_file)
            if not any(sp.get("tasks") for sp in pd.get("sprints", [])):
                raise ValueError("PROJECT_PLAN.md parses to 0 tasks")  # a plan that yields nothing is a read error, never an empty report
            d = merge_plan_and_ledger(pd, d)
    except (OSError, ValueError, ImportError) as e:
        ledger_error = str(e)
        d = {"version": 1, "project": Path.cwd().name, "sprints": [], "approval": None, "events": []}
    out = Path(a.out) if a.out else default_out(d, root)
    if not is_ours(out):
        print(f"report: refusing to overwrite a hand-made file {out}", file=sys.stderr)
        return 1
    if ledger_error and out.exists():  # never replace a real report by an empty page
        print(f"report: ledger not readable ({ledger_error}); the existing report {out} was left as it is", file=sys.stderr)
        return 1
    if ledger_error:
        print(f"report: ledger not readable ({ledger_error}); rendering an empty ledger", file=sys.stderr)
    try:
        atomic_write_text(out, render(d, root))
    except OSError as e:
        print(f"report: cannot write {out}: {e}", file=sys.stderr)
        return 1
    print(f"report: {out}")
    if a.stage:
        try:
            r = subprocess.run(["git", "-C", str(root), "add", "--", str(out)], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=30)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"report: git add failed: {e}", file=sys.stderr)
            return 1
        if r.returncode != 0:
            print(f"report: git add failed: {r.stderr.strip()}", file=sys.stderr)
            return 1
        print("report: staged into the commit")
    return 1 if ledger_error else 0


if __name__ == "__main__":
    sys.exit(main())
