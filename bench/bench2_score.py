"""Score benchmark v2 reviewer runs against bench2_key.json (line range +-2 and keyword regex, one finding per defect)."""
import json, re, collections
from pathlib import Path
HERE = Path(__file__).parent
SEV = ["NIT", "MINOR", "MAJOR", "BLOCKER"]
TOL = 2
key = json.loads((HERE / "bench2_key.json").read_text(encoding="utf-8"))["reviewer"]
# widen one keyword set: "stays due"/"overdue" are the same idea as "still due"
key["v05"]["defects"][1]["keywords"] += r"|stays? due|overdue"
runs = json.loads((HERE / "bench2_results.json").read_text(encoding="utf-8"))


def hit(f, spec):
    lo, hi = spec["lines"]
    return lo - TOL <= f["line"] <= hi + TOL and re.search(spec["keywords"], f["claim"], re.I)


per = collections.defaultdict(lambda: collections.Counter())
cases = collections.defaultdict(list)
for r in runs:
    k = key[r["unit"]]
    a = per[r["agent"]]
    a["runs"] += 1; a["tokens"] += r["tokens"]; a["ms"] += r["ms"]
    used = set()
    for d in k["defects"]:
        a["defects"] += 1
        cand = [(i, f) for i, f in enumerate(r["findings"]) if i not in used and hit(f, d)]
        if not cand:
            a["missed"] += 1; cases[r["unit"] + ":" + d["id"]].append(r["agent"] + "=MISS"); continue
        i, f = max(cand, key=lambda x: SEV.index(x[1]["severity"]))
        used.add(i); a["found"] += 1
        ok = SEV.index(f["severity"]) >= SEV.index(d["min_severity"])
        a["sev_ok" if ok else "sev_under"] += 1
        cases[r["unit"] + ":" + d["id"]].append(r["agent"] + ("" if ok else "=LOW(" + f["severity"] + ")"))
    for i, f in enumerate(r["findings"]):
        if i in used or f["severity"] not in ("BLOCKER", "MAJOR"):
            continue
        if any(hit(f, t) for t in k["traps"]):
            a["trap_fp"] += 1
        else:
            a["extra_serious"] += 1

out = {}
print(f"{'agent':24}{'runs':>5}{'defects':>8}{'found':>6}{'sevOK':>6}{'low':>5}{'trapFP':>7}{'extraBM':>8}{'avgTok':>8}{'avgSec':>7}")
for ag, a in sorted(per.items(), key=lambda x: (-x[1]["sev_ok"] / x[1]["defects"], x[1]["tokens"] / x[1]["runs"])):
    row = {k: a[k] for k in ("runs", "defects", "found", "sev_ok", "sev_under", "missed", "trap_fp", "extra_serious")}
    row["avg_tokens"] = a["tokens"] // a["runs"]; row["avg_sec"] = round(a["ms"] / a["runs"] / 1000, 1)
    out[ag] = row
    print(f"{ag:24}{a['runs']:>5}{a['defects']:>8}{a['found']:>6}{a['sev_ok']:>6}{a['sev_under']:>5}{a['trap_fp']:>7}{a['extra_serious']:>8}{row['avg_tokens']:>8}{row['avg_sec']:>7}")
print("\nPER DEFECT:")
for c in sorted(cases):
    print(c, cases[c])
tot = sum(r["tokens"] for r in runs)
print(f"\nTOTAL runs={len(runs)} tokens={tot} avg={tot // len(runs)}")
(HERE / "bench2_scored.json").write_text(json.dumps({"per_agent": out, "per_defect": cases}, indent=1), encoding="utf-8")
