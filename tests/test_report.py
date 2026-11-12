"""Criterion 4: the program status report (one format, tools/report.py; contract details in test_report_format.py)."""
import importlib.util
import re
import sys
from pathlib import Path

_s = importlib.util.spec_from_file_location("hv2", Path(__file__).resolve().parent / "hv2.py")
h = importlib.util.module_from_spec(_s)
sys.modules["hv2"] = h
_s.loader.exec_module(h)

MARKER = "<!-- harness-report v1 -->"


def _files(r):
    return sorted((r / "reports").glob("*_PROGRAM_STATUS_LIVE.ru.html"))


def _html(r):
    fs = _files(r)
    assert len(fs) == 1, fs
    return fs[0].read_text(encoding="utf-8")


def test_first_run_creates_report_in_reports_dir_with_marker():
    r = h.mk_repo("rp_first")
    h.setup_tasks(r)
    res = h.tool(r, "report.py")
    assert res.rc == 0, res.all
    assert not (r / ".harness" / "report.html").exists(), "old location must not be used"
    html = _html(r)
    assert MARKER in html[:400]
    assert "S1-T1" in html and "S1-T2" in html


def test_report_in_empty_ledger_still_renders():
    r = h.mk_repo("rp_empty")
    res = h.tool(r, "report.py")
    assert res.rc == 0, res.all
    assert MARKER in _html(r)[:400]


def test_second_run_overwrites_same_single_file():
    r = h.mk_repo("rp_second")
    h.setup_tasks(r)
    assert h.tool(r, "report.py").rc == 0
    assert h.tool(r, "report.py").rc == 0
    assert len(_files(r)) == 1


def test_report_is_rendered_from_scratch_after_ledger_change():
    r = h.mk_repo("rp_delta")
    h.setup_tasks(r)
    h.tool(r, "report.py")
    before = _html(r)
    assert h.ledger(r, "start", "S1-T1").rc == 0
    assert h.tool(r, "report.py").rc == 0
    after = _html(r)
    assert after != before and "в работе" in after


def test_hand_made_file_is_never_overwritten():
    r = h.mk_repo("rp_hand")
    h.setup_tasks(r, 1)
    h.tool(r, "report.py")
    f = _files(r)[0]
    f.write_text("<html>hand made</html>", encoding="utf-8")
    res = h.tool(r, "report.py")
    assert res.rc == 1, res.all
    assert f.read_text(encoding="utf-8") == "<html>hand made</html>"


def test_report_html_escapes_task_title():
    r = h.mk_repo("rp_escape")
    h.ledger(r, "sprint-add", "--id", "S1", "--title", "t")
    h.ledger(r, "task-add", "--title", "<script>alert(1)</script>")
    h.tool(r, "report.py")
    html = _html(r)
    assert "<script>alert(1)" not in html and "&lt;script&gt;" in html


def test_report_out_flag_and_harness_ledger_env():
    r = h.mk_repo("rp_out")
    alt = h.fresh("rp_out_alt")
    env = {"HARNESS_LEDGER": str(alt / "l" / "ledger.json")}
    h.ledger(r, "sprint-add", "--id", "S1", "--title", "t", env=env)
    res = h.tool(r, "report.py", env=env)
    assert res.rc == 0 and len(_files(r)) == 1, res.all
    assert not (alt / "l" / "report.html").exists()
    out = alt / "custom.html"
    assert h.tool(r, "report.py", "--out", str(out), env=env).rc == 0 and out.exists()


def test_not_run_state_is_shown_as_tests_not_run():
    r = h.mk_repo("rp_notrun")
    h.setup_tasks(r, 1)
    h.commit(r, "a.py", "x = 1\n", "work", "Task: S1-T1")
    h.ledger(r, "commit-hook")
    h.ledger(r, "verify", "S1-T1", "--result", "NOT_RUN", "--evidence", "no device")
    assert h.tool(r, "report.py").rc == 0
    html = _html(r)
    assert re.search(r'class="pill [\w-]+">тесты не запускались</span>', html), "state cell must say tests were not run"
    assert not re.search(r'class="pill [\w-]+">тесты красные', html), "not_run must not look like a red test run"


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
    assert html.index("Ошибки хуков") > html.index('id="updates"'), "alerts belong to the updates panel"


def test_shell_level_hook_errors_from_log_without_ledger_event_are_shown():
    r = h.mk_repo("rp_shelllog")
    h.setup_tasks(r, 1)
    assert not [e for e in h.load_ledger(r)["events"] if e["kind"] == "hook-error"]
    h.tool(r, "report.py")
    assert "Ошибки хуков" not in _html(r)
    log = r / ".harness" / "hook-error.log"
    log.write_text("2026-11-10T10:00:00 post-commit: no working Python (set HARNESS_PYTHON)\n", encoding="utf-8")
    assert h.tool(r, "report.py").rc == 0
    html = _html(r)
    assert "Ошибки хуков: 1" in html and "no working Python" in html, "shell-level error missing from header"
    assert html.index("Ошибки хуков") > html.index('id="updates"'), "must be in the updates panel"
    with open(log, "a", encoding="utf-8") as f:
        f.write("2026-11-10T10:05:00 post-commit: report.py failed\n")
        f.write("2026-11-10T10:06:00 post-commit: report.py failed\n")  # same message twice counts once
    h.tool(r, "report.py")
    html2 = _html(r)
    assert "Ошибки хуков: 2" in html2 and "report.py failed" in html2, html2[:1500]


def test_report_write_is_atomic_no_temp_leftover():
    r = h.mk_repo("rp_atomic")
    h.setup_tasks(r, 1)
    for _ in range(3):
        assert h.tool(r, "report.py").rc == 0
    names = sorted(p.name for p in (r / "reports").iterdir())
    assert len(names) == 1 and names[0].endswith("_PROGRAM_STATUS_LIVE.ru.html"), names
    assert not [n for n in names if ".tmp" in n], names


if __name__ == "__main__":
    sys.exit(h.run_all(globals()))
