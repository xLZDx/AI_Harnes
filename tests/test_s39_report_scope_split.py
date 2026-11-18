"""S39-T1: optional 'progress by product scope' section of tools/report.py (opt-in via .harness/scope-split.json)."""
import importlib.util
import json
import re
import sys
from pathlib import Path

_s = importlib.util.spec_from_file_location("hv2", Path(__file__).resolve().parent / "hv2.py")
h = importlib.util.module_from_spec(_s)
sys.modules["hv2"] = h
_s.loader.exec_module(h)

LABELS = ["Личный объём", "Общая основа", "Организационный объём", "Не размечено"]
ROW = re.compile(r'<span class="gate-chart-name"[^>]*>([^<]+)</span><strong>(\d+) из (\d+)</strong>')


def _html(r):
    fs = sorted((r / "reports").glob("*_PROGRAM_STATUS_LIVE.ru.html"))
    assert len(fs) == 1, fs
    return fs[0].read_text(encoding="utf-8")


def _section(html):
    i = html.index('id="scope-split"')
    j = html.find("</section>", i)
    return html[i:j]


def _rows(html):
    return {m.group(1): (int(m.group(2)), int(m.group(3))) for m in ROW.finditer(_section(html))}


def _repo(name, n=6):
    r = h.mk_repo(name)
    h.setup_tasks(r, n)
    return r


def _config(r, rel="docs/classes.json", tasks=None, classification=None):
    tasks = tasks if tasks is not None else [
        {"id": "S1-T1", "cls": "PERSONAL"}, {"id": "S1-T2", "cls": "PERSONAL"},
        {"id": "S1-T3", "cls": "FOUNDATION"}, {"id": "S1-T4", "cls": "ORGANIZATION"},
        {"id": "S1-T5", "cls": "UNCLEAR"}]  # S1-T6 deliberately absent
    f = r / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"generated_at": "2026-11-18", "tasks": tasks}), encoding="utf-8")
    (r / ".harness").mkdir(exist_ok=True)
    (r / ".harness" / "scope-split.json").write_text(
        json.dumps({"classification": classification or rel, "basis": "test basis"}), encoding="utf-8")


def _mark_done(r, tid):
    p = r / ".harness" / "ledger.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    hit = [t for s in d["sprints"] for t in s["tasks"] if t["id"] == tid]
    assert len(hit) == 1, tid
    hit[0]["state"] = "done"
    p.write_text(json.dumps(d, indent=2), encoding="utf-8")


def test_no_config_means_no_scope_section():
    r = _repo("s39_noconf")
    res = h.tool(r, "report.py")
    assert res.rc == 0, res.all
    assert 'id="scope-split"' not in _html(r)


def test_config_renders_four_rows_that_sum_to_ledger_task_count():
    r = _repo("s39_rows")
    _config(r)
    res = h.tool(r, "report.py")
    assert res.rc == 0, res.all
    html = _html(r)
    assert html.count('id="scope-split"') == 1
    rows = _rows(html)
    assert list(rows) == LABELS, rows
    assert [rows[k][1] for k in LABELS] == [2, 1, 1, 2], rows  # PERSONAL, FOUNDATION, ORGANIZATION, UNCLEAR+absent
    assert sum(m for _, m in rows.values()) == len(h.load_ledger(r)["sprints"][0]["tasks"]) == 6
    assert all(done == 0 for done, _ in rows.values()), rows


def test_done_count_of_a_row_follows_ledger_state():
    r = _repo("s39_done")
    _config(r)
    _mark_done(r, "S1-T1")
    assert h.tool(r, "report.py").rc == 0
    rows = _rows(_html(r))
    assert rows["Личный объём"] == (1, 2), rows
    assert rows["Общая основа"] == (0, 1) and rows["Организационный объём"] == (0, 1), rows
    _mark_done(r, "S1-T3")  # re-render picks up the new state
    assert h.tool(r, "report.py").rc == 0
    rows = _rows(_html(r))
    assert rows["Личный объём"] == (1, 2) and rows["Общая основа"] == (1, 1), rows


def test_task_missing_from_classification_counts_as_unmarked():
    r = _repo("s39_missing", n=3)
    _config(r, tasks=[{"id": "S1-T1", "cls": "PERSONAL"}])
    _mark_done(r, "S1-T3")  # the absent task is done: it must show in the unmarked row
    assert h.tool(r, "report.py").rc == 0
    rows = _rows(_html(r))
    assert rows["Личный объём"] == (0, 1), rows
    assert rows["Не размечено"] == (1, 2), rows


def test_classification_path_outside_repo_hides_section_but_report_renders():
    r = _repo("s39_outside")
    (r.parent / "outside.json").write_text(json.dumps(
        {"generated_at": "x", "tasks": [{"id": "S1-T1", "cls": "PERSONAL"}]}), encoding="utf-8")
    _config(r, rel="docs/unused.json", classification="../outside.json")
    res = h.tool(r, "report.py")
    assert res.rc == 0, res.all
    html = _html(r)
    assert 'id="scope-split"' not in html
    assert "<html" in html and 'id="updates"' in html, "the rest of the report must still render"


def test_invalid_json_config_or_classification_hides_section_exit_zero():
    r = _repo("s39_badjson", n=2)
    _config(r)
    (r / "docs" / "classes.json").write_text("{not json", encoding="utf-8")
    res = h.tool(r, "report.py")
    assert res.rc == 0, res.all
    assert 'id="scope-split"' not in _html(r)
    (r / ".harness" / "scope-split.json").write_text("][", encoding="utf-8")
    res = h.tool(r, "report.py")
    assert res.rc == 0, res.all
    assert 'id="scope-split"' not in _html(r)


def test_bom_lowercase_class_and_bad_entry_do_not_hide_the_section():
    r = _repo("s39_tolerant", n=3)
    _config(r, tasks=[{"id": "S1-T1", "cls": " personal "}, {"cls": "FOUNDATION"}, "junk", {"id": "S1-T2", "cls": "FOUNDATION"}])
    f = r / "docs" / "classes.json"
    f.write_bytes(b"\xef\xbb\xbf" + f.read_bytes())  # BOM as written by Windows PowerShell 5.1
    c = r / ".harness" / "scope-split.json"
    c.write_bytes(b"\xef\xbb\xbf" + c.read_bytes())
    res = h.tool(r, "report.py")
    assert res.rc == 0, res.all
    html = _html(r)
    rows = _rows(html)
    assert rows["Личный объём"] == (0, 1) and rows["Общая основа"] == (0, 1) and rows["Не размечено"] == (0, 1), rows
    assert "1 задача)" in html and "2 задачи)" not in html, "plural form of one task"


def test_unreadable_config_reports_on_stderr_and_renders_without_section():
    r = _repo("s39_stderr", n=2)
    _config(r)
    (r / "docs" / "classes.json").write_text("{not json", encoding="utf-8")
    res = h.tool(r, "report.py")
    assert res.rc == 0, res.all
    assert "scope-split section skipped" in res.all, res.all
    assert 'id="scope-split"' not in _html(r)


if __name__ == "__main__":
    sys.exit(h.run_all(globals()))
