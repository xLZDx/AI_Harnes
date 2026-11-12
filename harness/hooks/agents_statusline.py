#!/usr/bin/env python3
"""Status line for Claude Code (S7-T6): `agents: code-reviewer, tdd-guide? | sprint task S7-T1`.

A name followed by `?` is STATUS_UNKNOWN (no STOP seen for 10+ minutes; it may still be running). Reads only the active-agents
file written by agent_visibility.py and the ledger. Prints nothing useful on any error and always exits 0. Installed only when
the operator has no statusLine of their own (install_claude.py never overwrites one).
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    try:
        import agent_visibility as av
        try:
            data = json.loads(sys.stdin.read() or "{}")
        except Exception:
            data = {}
        now = time.time()
        names = []
        for e in av.load()["agents"]:
            st = av.state_of(e, now)
            if st == "FINISHED_CONFIRMED":
                continue
            names.append(e["type"] + ("?" if st == "STATUS_UNKNOWN" else ""))
        cwd = str((data.get("workspace") or {}).get("current_dir") or data.get("cwd") or "")
        task = av.task_id(cwd) if cwd else "active"
        sys.stdout.write("agents: " + (", ".join(names) if names else "none") + f" | task {task}")
    except Exception:
        sys.stdout.write("agents: ?")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
