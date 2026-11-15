"""S29 acceptance tests: hook timeouts raised to >= 30 s (lessons_recall.py == 15 s).

Checks the registry, the built Claude/Codex hook files, and the entry count against HEAD.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import traceback
from pathlib import Path

ROOT = Path(r"D:\tmp\scratch_worktrees\hv2-s29")
REG = ROOT / "harness" / "hooks" / "registry.json"
PY = r"C:\Python314\python.exe"
SPECIAL = {"lessons_recall.py": 15}
_built = False


def registry():
    return json.loads(REG.read_text(encoding="utf-8"))["hooks"]


def head_registry():
    cp = subprocess.run(["git", "show", "HEAD:harness/hooks/registry.json"], cwd=str(ROOT), capture_output=True,
                        encoding="utf-8", timeout=60)
    assert cp.returncode == 0, cp.stderr[-300:]
    return json.loads(cp.stdout)["hooks"]


def expected(script):
    return SPECIAL.get(script, 30)


def build():
    global _built
    if not _built:
        cp = subprocess.run([PY, "-s", "tools/build.py"], cwd=str(ROOT), capture_output=True, encoding="utf-8",
                            errors="replace", timeout=300)
        assert cp.returncode == 0, (cp.returncode, cp.stdout[-300:], cp.stderr[-300:])
        _built = True


def built_limits(path):
    """{(event, script): timeout} from a built hook file."""
    data = json.loads(path.read_text(encoding="utf-8"))["hooks"]
    got = {}
    for event, groups in data.items():
        for g in groups:
            for h in g["hooks"]:
                if "args" in h:
                    script = h["args"][-2]
                else:
                    m = re.search(r"(\S+\.py) --harness-client=", h["command"])
                    assert m, h["command"]
                    script = m.group(1)
                key = (event, Path(script).name)
                assert key not in got, f"duplicate {key}"
                got[key] = h["timeout"]
    return got


def reg_limits(client):
    return {(h["event"], h["script"]): h["timeout"] for h in registry() if client in h}


def test_registry_floor_and_special():
    bad = []
    for h in registry():
        t = h["timeout"]
        if h["script"] in SPECIAL:
            if t != SPECIAL[h["script"]]:
                bad.append((h["script"], t))
        elif t < 30:
            bad.append((h["script"], t))
    assert not bad, bad
    assert any(h["script"] == "lessons_recall.py" for h in registry()), "lessons_recall.py missing"


def test_high_limits_unchanged_and_low_raised():
    old = {(h["event"], h["script"]): h["timeout"] for h in head_registry()}
    new = {(h["event"], h["script"]): h["timeout"] for h in registry()}
    high = {k: v for k, v in old.items() if v > 30}
    assert high, "no entries above 30 at HEAD?"
    for k, v in high.items():
        assert new.get(k) == v, f"{k}: HEAD {v}, now {new.get(k)}"
    for k, v in old.items():
        if v < 30:
            assert new[k] == expected(k[1]), f"{k}: HEAD {v}, now {new[k]}"


def _built_check(client, path):
    build()
    got = built_limits(path)
    want = reg_limits(client)
    diff = {k: (got.get(k), want.get(k)) for k in set(got) | set(want) if got.get(k) != want.get(k)}
    assert not diff, diff
    for (ev, s), t in got.items():
        assert t == SPECIAL[s] if s in SPECIAL else t >= 30, (ev, s, t)


def test_built_claude_matches_registry():
    _built_check("claude", ROOT / "out" / "claude" / "settings.hooks.json")


def test_built_codex_matches_registry():
    _built_check("codex", ROOT / "out" / "codex" / "hooks.json")


def test_registry_entry_count_same_as_head():
    cur = [(h["event"], h["script"]) for h in registry()]
    old = [(h["event"], h["script"]) for h in head_registry()]
    assert len(cur) == len(old), (len(cur), len(old))
    assert cur == old


def run_all():
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:
                fails += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"OK: {fails} failing")
    return fails


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
