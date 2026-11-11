"""Tests for tools/report.py render() (S6-T1). Expected values come from the task spec, not from the code output.
Only temp dirs are used; real repos and ~/.claude are never touched. Run: python -I tests/test_report_format.py"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("report", ROOT / "tools" / "report.py")
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)

_TMPS = []
FAILS = []


def tmp():
    p = Path(tempfile.mkdtemp(prefix="rpf_"))
    _TMPS.append(p)
    return p


def ledger(titles=None):
    def task(i, state, title=None):
        return {"id": f"X-T{i}", "title": title or f"Zadacha{i}", "state": state, "criteria": [f"crit{i}a", f"crit{i}b"], "commits": []}
    return {"version": 1, "project": "FakeProj", "approval": None, "events": [], "sprints": [
        {"id": "S1", "title": "First", "state": "closed", "tasks": [task(1, "done"), task(2, "done")]},
        {"id": "S2", "title": "Second", "state": "open", "tasks": [task(3, "done"), task(4, "in_progress"), task(5, "todo")]},
    ]}


def section(page, sid):
    a = page.index(f'id="{sid}"')
    m = re.search(r'<section class="panel[^"]*" id="', page[a + 5:])
    return page[a:a + 5 + m.start()] if m else page[a:]


def t_structure():
    p = report.render(ledger(), tmp())
    assert re.match(r"<!doctype html>\r?\n<!-- harness-report v1 -->", p, re.I), p[:80]
    assert '<html lang="ru"' in p
    assert '<header class="hero" id="program-progress">' in p
    ids = ["program-visual", "sprint-distribution", "active-sprint", "stories", "freshness", "updates"]
    pos = [p.find(f'id="{i}"') for i in ids]
    assert all(x > 0 for x in pos), pos
    assert pos == sorted(pos), pos
    assert p.find('id="program-progress"') < pos[0]
    assert 'id="rc-copy"' in p and "data-idle=" in p and "data-done=" in p and "data-fail=" in p


def t_numbers():
    p = report.render(ledger(), tmp())  # 3 done of 5
    assert "принято 3/5" in p
    assert "3 ÷ 5 × 100 = 60,0%" in p
    assert "принято 3 из 5" in p


def t_no_commits_nr():
    p = report.render(ledger(), tmp())
    sp = section(p, "sprint-distribution")
    dur = re.findall(r'<span class="sprint-duration"[^>]*>([^<]*)</span>', sp)
    rat = re.findall(r'<span class="sprint-ratio"[^>]*>([^<]*)</span>', sp)
    assert dur == ["NR", "NR"], dur
    assert rat == ["NR / NR / NR / NR", "NR / NR / NR / NR"], rat


def t_escape():
    d = ledger()
    d["sprints"][1]["tasks"][0]["title"] = "<script>alert(1)</script>"
    d["sprints"][1]["tasks"][1]["criteria"] = ["<script>alert(1)</script>"]
    d["sprints"][0]["title"] = "<script>alert(1)</script>"
    p = report.render(d, tmp())
    assert "<script>alert(1)" not in p
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in p


def t_all_tasks_listed():
    d = ledger()
    p = report.render(d, tmp())
    st = section(p, "stories")
    for i in range(1, 6):
        assert f"<code>X-T{i}</code>" in st, i
        assert f"Zadacha{i}" in st
    assert re.findall(r'<tr data-search="[^"]*" data-state="([a-z_]+)"', st) == ["done", "done", "done", "in_progress", "todo"]
    act = section(p, "active-sprint")
    assert re.findall(r'<tr data-state="([a-z_]+)">', act) == ["done", "in_progress", "todo"]
    for i in (3, 4, 5):
        assert f"<code>X-T{i}</code>" in act
    for i in (1, 2):
        assert f"<code>X-T{i}</code>" not in act


def t_empty_ledger():
    p = report.render({"version": 1, "project": "Empty", "sprints": [], "approval": None, "events": []}, tmp())
    assert "harness-report v1" in p and 'id="stories"' in p
    assert "0/0" in p  # no division error, zero counts shown


def t_no_template_content():
    d = tmp()
    tpl = d / "tpl_file.html"
    tpl.write_text("<html>PRIVATEWORD secret layout</html>", encoding="utf-8")
    cfg = d / "cfg.json"
    cfg.write_text(json.dumps({"REPORT_TEMPLATE_FILE": str(tpl)}), encoding="utf-8")
    old = os.environ.get("HARNESS_HOOK_CONFIG")
    os.environ["HARNESS_HOOK_CONFIG"] = str(cfg)
    try:
        p = report.render(ledger(), tmp())
    finally:
        if old is None:
            del os.environ["HARNESS_HOOK_CONFIG"]
        else:
            os.environ["HARNESS_HOOK_CONFIG"] = old
    assert "PRIVATEWORD" not in p and "secret layout" not in p


def t_inlined_assets():
    p = report.render(ledger(), tmp())
    assert ".rc-bar" in p and "rc-copy" in p
    assert "<style>" in p and "<link " not in p and "<script src" not in p


def run_cli(args, cwd, ledger_file):
    env = dict(os.environ, HARNESS_LEDGER=str(ledger_file))
    return subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "report.py"), *args], cwd=cwd, env=env,
                          capture_output=True, text=True, encoding="utf-8")


def t_out_and_marker_window():
    d = tmp()
    lf = d / "l.json"
    lf.write_text(json.dumps(ledger()), encoding="utf-8")
    out = d / "o" / "r.html"
    r = run_cli(["--out", str(out)], d, lf)
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>\n<!-- harness-report v1 -->")
    # marker only after the first 400 bytes -> hand-made, never overwritten
    late = d / "late.html"
    body = "<html>" + "x" * 450 + "<!-- harness-report v1 -->"
    late.write_text(body, encoding="utf-8")
    run_cli(["--out", str(late)], d, lf)
    assert late.read_text(encoding="utf-8") == body
    # marker inside the first 400 bytes -> ours, replaced
    ours = d / "ours.html"
    ours.write_text("<!-- harness-report v1 -->old", encoding="utf-8")
    run_cli(["--out", str(ours)], d, lf)
    assert ours.read_text(encoding="utf-8") != "<!-- harness-report v1 -->old"


def t_busy_git_state():
    d = tmp()
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    assert report.busy_git_state(d) == ""
    (d / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n")
    assert report.busy_git_state(d) == "MERGE_HEAD"
    (d / ".git" / "MERGE_HEAD").unlink()
    (d / ".git" / "rebase-merge").mkdir()
    assert report.busy_git_state(d) == "rebase-merge"


def main():
    for name, fn in list(globals().items()):
        if name.startswith("t_"):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                FAILS.append(name)
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    for p in _TMPS:
        shutil.rmtree(p, ignore_errors=True)
    print("OK: 0 failing" if not FAILS else f"FAILED: {len(FAILS)} failing")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
