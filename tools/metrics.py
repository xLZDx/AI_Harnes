#!/usr/bin/env python3
"""Harness metrics (S7-T7): what the hooks and agents did, from the local metrics log. Reads local files only.

  python tools/metrics.py [--days N] [--project NAME] [--json] [--purge] [--ledger PATH]

Prints, for the period: hooks fired; allow / deny / block / error counts and p50 / p95 latency per hook (nearest-rank
percentiles); safety-valve releases and exceptions; agent runs per type with total seconds; question auto-answers; tasks done
per sprint from the ledger; "repeat blocks" = 3 or more blocks of one hook in one session within 120 s (a possible false
positive or a loop). It shows activity and friction, NOT code quality. Tokens are not logged by the hooks, so none are shown.
The log never holds prompts, commands or secrets (hook_metrics.py). --purge applies the 30-day retention now.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


def load_helper():
    here = Path(__file__).resolve().parent
    for cand in (here.parent / "harness" / "hooks", Path.home() / ".claude" / "hooks", Path.home() / ".codex" / "hooks", here):
        if (cand / "hook_metrics.py").is_file():
            sys.path.insert(0, str(cand))
            import hook_metrics
            return hook_metrics
    raise SystemExit("hook_metrics.py not found (build and install the harness first)")


def pct(values: list[int], p: float) -> int:
    """Nearest-rank percentile; 0 for an empty list."""
    if not values:
        return 0
    v = sorted(values)
    return v[max(0, math.ceil(p / 100 * len(v)) - 1)]


def repeat_blocks(rows: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for r in rows:
        if r.get("decision") in ("deny", "block"):
            try:
                groups[(r["hook"], r["session"])].append(datetime.fromisoformat(r["ts"]))
            except Exception:
                pass
    out = []
    for (hook, sess), ts in groups.items():
        ts.sort()
        j = 0
        best = 0
        for i in range(len(ts)):
            while ts[i] - ts[j] > timedelta(seconds=120):
                j += 1
            best = max(best, i - j + 1)
        if best >= 3:
            out.append({"hook": hook, "session": sess[:8], "blocks_within_120s": best})
    return sorted(out, key=lambda x: -x["blocks_within_120s"])


def summarize(rows: list[dict], project: str = "") -> dict:
    if project:
        rows = [r for r in rows if r.get("project") == project]
    hooks = defaultdict(lambda: {"fired": 0, "allow": 0, "deny": 0, "block": 0, "error": 0, "release": 0, "exception": 0, "ms": []})
    agents = defaultdict(lambda: {"starts": 0, "stops": 0, "seconds": 0})
    for r in rows:
        h = hooks[r.get("hook", "?")]
        h["fired"] += 1
        d = r.get("decision", "allow")
        h[d if d in h else "allow"] += 1
        try:
            h["ms"].append(int(r.get("ms", 0)))
        except Exception:
            pass
        if r.get("reason") == "agent-start":
            agents[r.get("agent") or "?"]["starts"] += 1
        elif r.get("reason") == "agent-stop":
            a = agents[r.get("agent") or "?"]
            a["stops"] += 1
            a["seconds"] += int(r.get("ms", 0)) // 1000
    table = {}
    for name, h in sorted(hooks.items()):
        table[name] = {**{k: v for k, v in h.items() if k != "ms"}, "p50_ms": pct(h["ms"], 50), "p95_ms": pct(h["ms"], 95)}
    return {"rows": len(rows), "hooks": table, "agents": dict(agents), "repeat_blocks": repeat_blocks(rows)}


def auto_answers(since: datetime) -> int:
    f = Path.home() / ".harness-hooks" / "state" / "question_policy" / "auto_answers.jsonl"
    n = 0
    try:
        for ln in f.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(ln)
                if e.get("action") == "auto-accepted" and datetime.fromisoformat(e["ts"]) >= since:
                    n += 1
            except Exception:
                pass
    except OSError:
        pass
    return n


def ledger_progress(path: Path) -> dict:
    try:
        led = json.loads(path.read_text(encoding="utf-8"))
        return {sp["id"]: {"done": sum(1 for t in sp.get("tasks", []) if t.get("state") == "done"),
                           "tasks": len(sp.get("tasks", [])), "state": sp.get("state")} for sp in led.get("sprints", [])}
    except Exception:
        return {}


def render(res: dict) -> str:
    out = [f"Harness metrics, last {res['days']} day(s): {res['rows']} hook runs "
           f"(corrupt lines skipped: {res['corrupt']}, rows dropped by lock timeout: {res['dropped']})",
           "Shows activity and friction, not code quality. Tokens are not logged.", "",
           f"{'hook':32} {'fired':>6} {'allow':>6} {'deny':>5} {'block':>5} {'error':>5} {'p50ms':>6} {'p95ms':>6}"]
    for name, h in res["hooks"].items():
        out.append(f"{name:32} {h['fired']:>6} {h['allow']:>6} {h['deny']:>5} {h['block']:>5} {h['error']:>5} {h['p50_ms']:>6} {h['p95_ms']:>6}")
    out += ["", "Agent runs: " + (", ".join(f"{k} x{v['starts']} ({v['seconds']} s)" for k, v in sorted(res["agents"].items())) or "none"),
            f"Question auto-answers: {res['auto_answers']}",
            "Safety-valve releases / exceptions: " + str(sum(h["release"] + h["exception"] for h in res["hooks"].values()))]
    if res["repeat_blocks"]:
        out.append("Possible false positives or loops (3+ blocks within 120 s): " +
                   "; ".join(f"{x['hook']} session {x['session']} x{x['blocks_within_120s']}" for x in res["repeat_blocks"]))
    if res["ledger"]:
        out.append("Sprints: " + ", ".join(f"{k} {v['done']}/{v['tasks']} ({v['state']})" for k, v in res["ledger"].items()))
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--project", default="")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--purge", action="store_true")
    ap.add_argument("--ledger", default="")
    a = ap.parse_args(argv)
    hm = load_helper()
    hm.purge()  # retention runs first, whatever the options
    if a.purge:
        print("purge done")
        return 0
    since = datetime.now(timezone.utc) - timedelta(days=a.days)
    rows, bad, dropped = hm.read_rows(since)
    res = summarize(rows, a.project)
    res.update({"days": a.days, "corrupt": bad, "dropped": dropped, "auto_answers": auto_answers(since),
                "ledger": ledger_progress(Path(a.ledger) if a.ledger else Path(".harness") / "ledger.json")})
    print(json.dumps(res, indent=1, ensure_ascii=False) if a.json else render(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
