import json, subprocess, sys
from pathlib import Path

s = json.loads((Path.home() / ".claude" / "settings.json").read_text(encoding="utf-8-sig"))
n = sum(len(g["hooks"]) for v in s["hooks"].values() for g in v)
print("live hook entries:", n)
payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": "D:/Repo/Harness_v2",
                      "session_id": "probe", "tool_input": {"command": "rm -rf /d/tmp/probe_nothing"}})
blocked = []
for g in s["hooks"]["PreToolUse"]:
    if "Bash" not in (g.get("matcher") or ""):
        continue
    for h in g["hooks"]:
        r = subprocess.run([h["command"], *h.get("args", [])], input=payload.encode(), capture_output=True, timeout=60)
        out = r.stdout.decode("utf-8", "replace")
        if r.returncode == 2 or '"deny"' in out or '"block"' in out:
            blocked.append(Path(h["args"][3]).name)
print("hooks that block 'rm -rf':", blocked)
