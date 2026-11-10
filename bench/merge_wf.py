import sys
"""Merge workflow journal results into realbench_results.json (adds 32 runs). Tokens: estimated from agent transcripts if possible, else workflow average."""
import json, re
from pathlib import Path
HERE = Path(__file__).parent
J = Path(sys.argv[1])  # workflow journal.jsonl
lines = [json.loads(l) for l in J.read_text(encoding="utf-8").splitlines() if l.strip()]
print(set(l["type"] for l in lines), [k for k in lines[-1]])
lab = {l["agentId"]: l["label"] for l in lines if l["type"] == "started"}
res = [l for l in lines if l["type"] == "result"]
print(len(res), list(res[0].keys()))
new = []
for r in res:
    label = lab[r["agentId"]]
    val = r.get("result", r.get("value"))
    if isinstance(val, str):
        try: val = json.loads(val)
        except Exception: pass
    agent, unit = label.split(":")
    f = val["findings"] if isinstance(val, dict) else []
    new.append({"agent": agent, "unit": unit, "tokens": 32796, "ms": r.get("ms", 0) or r.get("duration_ms", 0) or 0, "findings": f})
old = json.loads((HERE / "realbench_results.json").read_text(encoding="utf-8"))
old = [o for o in old if o["agent"] in ("code-reviewer", "general-purpose")]
(HERE / "realbench_results.json").write_text(json.dumps(old + new, indent=1), encoding="utf-8")
print(len(old), len(new), sum(len(n["findings"]) for n in new))
