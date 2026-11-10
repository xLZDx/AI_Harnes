"""Score the domain-agent workflow: synthetic domain cases (dombench) and real bugs (realbench, compared with earlier generic runs)."""
import json, re, collections, sys
from pathlib import Path
HERE = Path(__file__).parent
J = Path(sys.argv[1])
SEV = ["NIT", "MINOR", "MAJOR", "BLOCKER"]
lines = [json.loads(l) for l in J.read_text(encoding="utf-8").splitlines() if l.strip()]
lab = {l["agentId"]: l["label"] for l in lines if l["type"] == "started"}
runs = []
for r in (l for l in lines if l["type"] == "result"):
    label = lab[r["agentId"]]
    *ag, unit = label.split(":")
    v = r["result"]
    if isinstance(v, str):
        v = json.loads(v)
    runs.append({"agent": ":".join(ag), "unit": unit, "findings": v.get("findings", [])})
(HERE / "dombench_results.json").write_text(json.dumps(runs, indent=1), encoding="utf-8")
keys = {**json.loads((HERE / "dombench_key.json").read_text(encoding="utf-8")),
        **json.loads((HERE / "realbench_key.json").read_text(encoding="utf-8"))}
real = json.loads((HERE / "realbench_results.json").read_text(encoding="utf-8"))


def hit(f, d, tol):
    return (Path(str(f.get("file", ""))).name == d["file"] and d["lines"][0] - tol <= int(f.get("line", -999)) <= d["lines"][1] + tol
            and re.search(d["keywords"], f.get("claim", ""), re.I))


def score(rs, prefix):
    per = collections.defaultdict(collections.Counter)
    cases = collections.defaultdict(list)
    for r in rs:
        if not r["unit"].startswith(prefix):
            continue
        tol = 3 if prefix == "d" else 15
        a = per[r["agent"]]; a["runs"] += 1
        for d in keys[r["unit"]]["defects"]:
            a["defects"] += 1
            c = [f for f in r["findings"] if hit(f, d, tol)]
            if not c:
                cases[f"{r['unit']}:{d['id']}"].append(r["agent"] + "=MISS"); continue
            best = max(c, key=lambda f: SEV.index(str(f["severity"]).upper()))["severity"].upper()
            a["found"] += 1
            ok = SEV.index(best) >= SEV.index(d.get("min_severity", "MAJOR"))
            a["sev_ok"] += ok
            cases[f"{r['unit']}:{d['id']}"].append(f"{r['agent']}={best}")
        a["bm_total"] += sum(1 for f in r["findings"] if str(f["severity"]).upper() in ("BLOCKER", "MAJOR"))
    return per, cases


for title, prefix, rs in (("SYNTHETIC DOMAIN CASES", "d", runs), ("REAL BUGS: domain runs", "r", runs), ("REAL BUGS: earlier generic runs", "r", real)):
    per, cases = score(rs, prefix)
    print(f"\n== {title}\n{'agent':38}{'runs':>5}{'defects':>8}{'found':>6}{'sevOK':>6}{'allBM':>6}")
    for ag, a in sorted(per.items(), key=lambda x: -x[1]["found"] / max(x[1]["defects"], 1)):
        print(f"{ag:38}{a['runs']:>5}{a['defects']:>8}{a['found']:>6}{a['sev_ok']:>6}{a['bm_total']:>6}")
    for c in sorted(cases):
        print(" ", c, cases[c])
