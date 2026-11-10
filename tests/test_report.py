"""Criterion 4: incremental HTML report."""
import importlib.util
import re
import sys
from pathlib import Path

_s = importlib.util.spec_from_file_location("hv2", Path(__file__).resolve().parent / "hv2.py")
h = importlib.util.module_from_spec(_s)
sys.modules["hv2"] = h
_s.loader.exec_module(h)

IDS = ["progress", "kpis", "tasks", "next"]


def _changed(res):
    m = re.search(r"\(changed: (.*)\)", res.out)
    assert m, res.all
    return m.group(1)


def _sections(html):
    return re.findall(r'<section id="(\w+)" data-hash="\w+"([^>]*)>', html)


def test_first_run_creates_report_with_four_sections():
    r = h.mk_repo("rp_first")
    h.setup_tasks(r)
    res = h.tool(r, "report.py")
    assert res.rc == 0, res.all
    f = r / ".harness" / "report.html"
    assert f.exists()
    html = f.read_text(encoding="utf-8")
    assert [s[0] for s in _sections(html)] == IDS
    assert "S1-T1" in html and "S1-T2" in html
    assert _changed(res) == "progress, kpis, tasks, next"


def test_report_in_empty_ledger_still_renders():
    r = h.mk_repo("rp_empty")
    res = h.tool(r, "report.py")
    assert res.rc == 0, res.all
    assert [s[0] for s in _sections((r / ".harness" / "report.html").read_text(encoding="utf-8"))] == IDS


def test_second_run_without_change_reports_none_and_leaves_unmarked():
    r = h.mk_repo("rp_second")
    h.setup_tasks(r)
    h.tool(r, "report.py")
    res = h.tool(r, "report.py")
    assert _changed(res) == "none", res.all
    html = (r / ".harness" / "report.html").read_text(encoding="utf-8")
    assert 'class="upd"' not in html and 'class="tag"' not in html
    assert "изменено секций: 0" in html


def test_only_affected_sections_changed_and_tagged_then_cleared():
    r = h.mk_repo("rp_delta")
    h.setup_tasks(r)
    h.tool(r, "report.py")
    h.tool(r, "report.py")
    assert h.ledger(r, "start", "S1-T1").rc == 0
    res = h.tool(r, "report.py")
    changed = [c.strip() for c in _changed(res).split(",")]
    assert "tasks" in changed and "kpis" not in changed, changed
    assert set(changed) <= set(IDS) and changed, changed
    html = (r / ".harness" / "report.html").read_text(encoding="utf-8")
    tagged = [sid for sid, attrs in _sections(html) if 'class="upd"' in attrs]
    assert sorted(tagged) == sorted(changed), (tagged, changed)
    assert html.count('class="tag"') == len(changed)
    assert "в работе" in html
    # the next unchanged run clears all marks again
    res3 = h.tool(r, "report.py")
    assert _changed(res3) == "none"
    html3 = (r / ".harness" / "report.html").read_text(encoding="utf-8")
    assert 'class="upd"' not in html3 and 'class="tag"' not in html3


def test_unchanged_sections_are_byte_identical_across_runs():
    r = h.mk_repo("rp_bytes")
    h.setup_tasks(r)
    h.tool(r, "report.py")
    a = (r / ".harness" / "report.html").read_text(encoding="utf-8")
    h.ledger(r, "start", "S1-T1")
    h.tool(r, "report.py")
    b = (r / ".harness" / "report.html").read_text(encoding="utf-8")
    pa = re.search(r'<section id="kpis".*?</section>', a, re.S).group(0)
    pb = re.search(r'<section id="kpis".*?</section>', b, re.S).group(0)
    assert pa == pb
    ta = re.search(r'<section id="tasks".*?</section>', a, re.S).group(0)
    tb = re.search(r'<section id="tasks".*?</section>', b, re.S).group(0)
    assert ta != tb


def test_report_html_escapes_task_title():
    r = h.mk_repo("rp_escape")
    h.ledger(r, "sprint-add", "--id", "S1", "--title", "t")
    h.ledger(r, "task-add", "--title", "<script>alert(1)</script>")
    h.tool(r, "report.py")
    html = (r / ".harness" / "report.html").read_text(encoding="utf-8")
    assert "<script>alert(1)" not in html and "&lt;script&gt;" in html


def test_report_out_flag_and_harness_ledger_env():
    r = h.mk_repo("rp_out")
    alt = h.fresh("rp_out_alt")
    env = {"HARNESS_LEDGER": str(alt / "l" / "ledger.json")}
    h.ledger(r, "sprint-add", "--id", "S1", "--title", "t", env=env)
    res = h.tool(r, "report.py", env=env)
    assert res.rc == 0 and (alt / "l" / "report.html").exists()
    out = alt / "custom.html"
    assert h.tool(r, "report.py", "--out", str(out), env=env).rc == 0 and out.exists()


def _html(r):
    return (r / ".harness" / "report.html").read_text(encoding="utf-8")


def test_not_run_state_is_shown_as_tests_not_run():
    r = h.mk_repo("rp_notrun")
    h.setup_tasks(r, 1)
    h.commit(r, "a.py", "x = 1\n", "work", "Task: S1-T1")
    h.ledger(r, "commit-hook")
    h.ledger(r, "verify", "S1-T1", "--result", "NOT_RUN", "--evidence", "no device")
    assert h.tool(r, "report.py").rc == 0
    html = _html(r)
    assert re.search(r'class="st [\w-]+">тесты не запускались</span>', html), "state cell must say tests were not run"
    assert not re.search(r'class="st [\w-]+">тесты красные', html), "not_run must not look like a red test run"


def test_header_shows_hook_errors_and_approval_skips_as_alerts():
    r = h.mk_repo("rp_alerts")
    h.setup_tasks(r, 1)
    h.tool(r, "report.py")
    assert "Ошибки хуков" not in _html(r) and "была отключена" not in _html(r)
    h.commit(r, "a.py", "x = 1\n", "x", "Task: S9-T9")
    h.ledger(r, "commit-hook")
    h.ledger(r, "check-approval", env={"HARNESS_SKIP_APPROVAL": "1"})
    assert h.tool(r, "report.py").rc == 0
    html = _html(r)
    assert "Ошибки хуков: 1" in html and "S9-T9" in html, "hook error alert missing"
    assert "была отключена 1 раз" in html, "approval skip alert missing"
    assert html.index("Ошибки хуков") < html.index('<section id="progress"'), "alerts belong to the header"


def test_report_write_is_atomic_no_temp_leftover():
    r = h.mk_repo("rp_atomic")
    h.setup_tasks(r, 1)
    for _ in range(3):
        assert h.tool(r, "report.py").rc == 0
    names = sorted(p.name for p in (r / ".harness").iterdir())
    assert "report.html" in names and not [n for n in names if n.endswith(".tmp")], names


if __name__ == "__main__":
    sys.exit(h.run_all(globals()))
