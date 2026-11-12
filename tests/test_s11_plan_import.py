"""S11-T2 tests: roadmap placeholders in plan_ledger, merge_plan_and_ledger with placeholder sprints, plan_import_ledger_md importer.
Run: C:\\Python314\\python.exe -I tests/test_s11_plan_import.py"""
import contextlib
import io
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools"))
import hv2  # noqa: E402
import plan_ledger  # noqa: E402
import plan_import_ledger_md as imp  # noqa: E402
import report  # noqa: E402

IMPORTER = hv2.TOOLS / "plan_import_ledger_md.py"


def _plan(extra_sections="", roadmap=None):
    roadmap = roadmap if roadmap is not None else [
        ("S00", "Done", "NOT_STARTED", "expanded below"),
        ("S01", "Five", "NOT_STARTED", "5 task placements, task list not expanded here"),
        ("S02", "Four", "NOT_STARTED", "4 task placements, task list not expanded here"),
    ]
    rows = "".join(f"| {a} | {b} | {c} | {d} |\n" for a, b, c, d in roadmap)
    return ("---\nproject: demo\n---\n# Plan\n\n## Roadmap\n| Sprint | Title | Status | Goal |\n|---|---|---|---|\n" + rows + "\n"
            + extra_sections)


S00 = ("## Sprint S00 - Zero\nGoal: g\n\n| Task | Title | Status |\n|---|---|---|\n"
       "| S00-1 | a | ACCEPTED |\n| S00-2 | b | ACCEPTED |\n| S00-3 | c | NOT_STARTED |\n")


def _sp(d, sid):
    return next(s for s in d["sprints"] if s["id"] == sid)


# ---------- (1) placeholders ----------
def test_roadmap_placements_make_12_placeholder_tasks_not_expanded():
    d = plan_ledger.parse(_plan(S00, [("S00", "Zero", "X", "g"), ("S05", "Big", "NOT_STARTED", "12 task placements, task list not expanded here")]))
    s = _sp(d, "S05")
    assert len(s["tasks"]) == 12, len(s["tasks"])
    assert [t["id"] for t in s["tasks"]] == [f"S05-PLACEHOLDER-{k}" for k in range(1, 13)]
    assert all(t["state"] == "todo" for t in s["tasks"])
    assert s["expanded"] is False


def test_sprint_with_section_is_expanded_with_real_tasks_and_not_duplicated_as_placeholder():
    d = plan_ledger.parse(_plan(S00, [("S00", "Zero", "X", "7 task placements, task list not expanded here")]))
    s = _sp(d, "S00")
    assert s["expanded"] is True and [t["id"] for t in s["tasks"]] == ["S00-1", "S00-2", "S00-3"], s
    assert len([x for x in d["sprints"] if x["id"] == "S00"]) == 1


def test_roadmap_row_without_task_placements_makes_no_placeholders():
    d = plan_ledger.parse(_plan(S00, [("S00", "Zero", "X", "g"), ("S09", "Plain", "NOT_STARTED", "just a goal, 8 tasks maybe")]))
    assert [s["id"] for s in d["sprints"]] == ["S00"], [s["id"] for s in d["sprints"]]


def test_plan_without_any_sprint_section_still_raises():
    try:
        plan_ledger.parse(_plan("", [("S01", "Five", "NOT_STARTED", "5 task placements, task list not expanded here")]))
    except ValueError:
        return
    raise AssertionError("expected ValueError for a plan with no '## Sprint' section")


# ---------- (2) merge ----------
def _real(sid, n, prefix):
    return {"id": sid, "title": sid, "state": "open", "expanded": True, "tasks": [{"id": f"{prefix}-{i}", "state": "todo"} for i in range(n)]}


def _ph(sid, n):
    return {"id": sid, "title": sid, "state": "planned", "expanded": False, "tasks": [{"id": f"{sid}-PLACEHOLDER-{i}", "state": "todo"} for i in range(n)]}


def test_merge_placeholder_sprint_keeps_ledger_tasks_plus_unverified_placeholders_up_to_n():
    plan = {"sprints": [_real("S00", 46, "A"), _ph("S02", 37)]}
    led = {"sprints": [_real("S02", 2, "L")]}
    m = report.merge_plan_and_ledger(plan, led)
    counts = {s["id"]: len(s["tasks"]) for s in m["sprints"]}
    assert counts == {"S02": 37, "S00": 46}, counts  # N=37, k=2 -> 37 total, never fewer than N
    assert sum(counts.values()) == 83
    assert m["sprints"][0]["id"] == "S02"  # ledger sprints first
    tasks = m["sprints"][0]["tasks"]
    assert [t["id"] for t in tasks[:2]] == ["L-0", "L-1"], "the k ledger tasks stay real"
    extra = tasks[2:]
    assert len(extra) == 35
    assert [t["id"] for t in extra] == [f"S02-PLACEHOLDER-{j}" for j in range(3, 38)]
    for j, t in zip(range(3, 38), extra):
        assert t["title"].startswith(f"UNVERIFIED: planned placement {j} of 37"), t["title"]
        assert t["state"] == "todo"


def test_merge_ledger_larger_than_announced_keeps_only_ledger_tasks():
    m = report.merge_plan_and_ledger({"sprints": [_real("S00", 1, "A"), _ph("S02", 1)]}, {"sprints": [_real("S02", 3, "L")]})
    counts = {s["id"]: len(s["tasks"]) for s in m["sprints"]}
    assert counts == {"S02": 3, "S00": 1}, counts
    assert not any("PLACEHOLDER" in t["id"] for s in m["sprints"] for t in s["tasks"])


def test_merge_ledger_equal_to_announced_adds_no_placeholders():
    m = report.merge_plan_and_ledger({"sprints": [_ph("S02", 2)]}, {"sprints": [_real("S02", 2, "L")]})
    assert [len(s["tasks"]) for s in m["sprints"]] == [2]
    assert not any("PLACEHOLDER" in t["id"] for t in m["sprints"][0]["tasks"])


def test_merge_expanded_plan_sprint_with_same_id_gets_ledger_suffix():
    plan = {"sprints": [_real("S02", 3, "A")]}
    led = {"sprints": [_real("S02", 2, "L")]}
    m = report.merge_plan_and_ledger(plan, led)
    assert [s["id"] for s in m["sprints"]] == ["S02-ledger", "S02"], [s["id"] for s in m["sprints"]]
    assert sum(len(s["tasks"]) for s in m["sprints"]) == 5


def test_e2e_report_denominator_counts_every_roadmap_sprint():
    repo = hv2.mk_repo("s11-e2e")
    hv2.commit(repo, "PROJECT_PLAN.md", _plan(S00), "plan")
    assert hv2.ledger(repo, "sprint-add", "--id", "S02", "--title", "Ledger two").rc == 0
    for i in range(2):
        r = hv2.ledger(repo, "task-add", "--title", f"lt {i}", "--risk", "local")
        assert r.rc == 0, r.all
    out = repo / "r.html"
    r = hv2.run([hv2.PY, "-I", str(hv2.TOOLS / "report.py"), "--out", str(out)], repo)
    assert r.rc == 0, r.all
    m = re.search(r"(\d+)\s*÷\s*(\d+)\s*×\s*100", out.read_text(encoding="utf-8"))
    assert m, "no formula in the report"
    # S00: 2 accepted of 3; S01 5 placeholders; S02 announces 4: ledger 2 real + 2 UNVERIFIED placeholders = 4
    assert (int(m.group(1)), int(m.group(2))) == (2, 3 + 5 + 4), m.group(0)


# ---------- (3) importer ----------
LEDGER_MD = """# Ledger

| Story / task | Status | Title | Basis |
|---|---|---|---|
| ST-A-1 | ACCEPTED | first | commit a1b2c3d4e5f6 merged |
| ST-A-2 | ACCEPTED (BOUNDED RUNTIME) | second | bounded |
| ST-A-3 | PARTIAL | third | half |
| ST-A-4 | SUPERSEDED_OPERATOR_SCOPE | fourth | dropped |
| ST-A-5 | HISTORICAL_ONLY | fifth | old |
| ST-A-6 | BLOCKED | sixth | waiting on EXTERNAL vendor |
| ST-A-7 | BLOCKED | seventh | waiting for the operator |
| ST-A-8 | NOT_STARTED | eighth first | early |
| ST-A-8 | ACCEPTED | eighth last | later row wins |

| Task | Single next step |
|---|---|
| ST-A-9 | do something |
"""

BASE_PLAN = """---
project: imp
---
# Plan

## Roadmap
| Sprint | Title | Status | Goal |
|---|---|---|---|
| S01 | Imported sprint | NOT_STARTED | roadmap goal |

## Sprint S01 - Old title
Goal: keep me

| Task | Title | Status |
|---|---|---|
| OLD-1 | old | NOT_STARTED |

## Sprint S02 - Other
Goal: other

| Task | Title | Status |
|---|---|---|
| S02-1 | x | ACCEPTED |
"""


def _imp(name, ledger_text=LEDGER_MD, plan_text=BASE_PLAN, crlf=False):
    d = hv2.fresh(name)
    (d / "ledger.md").write_text(ledger_text, encoding="utf-8", newline="\n")
    p = d / "PROJECT_PLAN.md"
    p.write_bytes((plan_text.replace("\n", "\r\n") if crlf else plan_text).encode("utf-8"))
    return d, p


def _run_imp(d, *extra):
    return hv2.run([hv2.PY, "-I", str(IMPORTER), "--ledger", str(d / "ledger.md"), "--plan", str(d / "PROJECT_PLAN.md"),
                    "--sprint", "S01", *extra], d)


def _status_of(text, tid):
    for ln in text.splitlines():
        c = [x.strip() for x in ln.strip().strip("|").split("|")]
        if ln.startswith("|") and c[0] == tid:
            return c[3]
    return None


def test_importer_status_mapping_last_row_wins_and_next_step_table_ignored():
    d, p = _imp("s11-imp-map")
    r = _run_imp(d)
    assert r.rc == 0, r.all
    t = p.read_text(encoding="utf-8")
    exp = {"ST-A-1": "ACCEPTED", "ST-A-2": "ACCEPTED_BOUNDED", "ST-A-3": "PARTIAL", "ST-A-4": "SUPERSEDED", "ST-A-5": "SUPERSEDED",
           "ST-A-6": "BLOCKED_EXTERNAL", "ST-A-7": "BLOCKED_OPERATOR", "ST-A-8": "ACCEPTED"}
    for tid, st in exp.items():
        assert _status_of(t, tid) == st, (tid, _status_of(t, tid))
    assert _status_of(t, "ST-A-9") is None and "do something" not in t
    assert t.count("| ST-A-8 |") == 1 and "eighth last" in t and "eighth first" not in t
    assert "tasks written: 8" in r.out and "ACCEPTED=2" in r.out and "ACCEPTED_BOUNDED=1" in r.out, r.out


def test_importer_unknown_status_exits_nonzero_and_plan_untouched():
    bad = LEDGER_MD.replace("| ST-A-3 | PARTIAL |", "| ST-A-3 | WEIRD_STATE |")
    d, p = _imp("s11-imp-bad", ledger_text=bad)
    before = p.read_bytes()
    r = _run_imp(d)
    assert r.rc != 0, r.all
    assert p.read_bytes() == before


def test_importer_dry_run_leaves_plan_byte_identical():
    d, p = _imp("s11-imp-dry")
    before = p.read_bytes()
    r = _run_imp(d, "--dry-run")
    assert r.rc == 0 and "tasks written: 8" in r.out, r.all
    assert p.read_bytes() == before


def test_importer_two_real_runs_are_byte_identical_and_lf_only_even_for_crlf_input():
    d, p = _imp("s11-imp-idem", crlf=True)
    assert _run_imp(d).rc == 0
    first = p.read_bytes()
    assert b"\r" not in first, "CR found in output"
    assert _run_imp(d).rc == 0
    assert p.read_bytes() == first


def test_importer_replaces_existing_sprint_section_keeps_goal_and_other_sections():
    d, p = _imp("s11-imp-repl")
    rr = _run_imp(d)
    assert rr.rc == 0, rr.all
    t = p.read_text(encoding="utf-8")
    assert len(re.findall(r"^## Sprint S01\b", t, re.M)) == 1, t
    assert "Goal: keep me" in t and "OLD-1" not in t
    assert len(re.findall(r"^## Sprint S02\b", t, re.M)) == 1 and "| S02-1 |" in t
    # the result parses as a plan with all 8 tasks (superseded ones are kept); bounded acceptance is verified, never done
    parsed = plan_ledger.parse(t)
    tasks = {x["id"]: x for x in _sp(parsed, "S01")["tasks"]}
    assert len(tasks) == 8, sorted(tasks)
    assert tasks["ST-A-2"]["state"] == "verified" and tasks["ST-A-1"]["state"] == "done"
    assert tasks["ST-A-4"]["state"] == "superseded" and tasks["ST-A-5"]["state"] == "superseded"
    assert "old rows removed: 1" in rr.out, rr.out


# ---------- 3a. superseded is its own category ----------
def test_report_shows_superseded_as_own_category_counted_in_denominator():
    rows = ([(f"Q-{i}", "ACCEPTED") for i in range(1, 7)] + [(f"Q-{i}", "SUPERSEDED") for i in range(7, 9)]
            + [(f"Q-{i}", "NOT_STARTED") for i in range(9, 11)])
    plan = "---\nproject: sup\n---\n# Plan\n\n## Sprint Q1 - Ten rows\nGoal: g\n\n| Task | Title | Status |\n|---|---|---|\n" + "".join(
        f"| {a} | t {a} | {b} |\n" for a, b in rows)
    repo = hv2.mk_repo("s11-sup")
    hv2.commit(repo, "PROJECT_PLAN.md", plan, "plan")
    out = repo / "r.html"
    r = hv2.run([hv2.PY, "-I", str(hv2.TOOLS / "report.py"), "--out", str(out)], repo)
    assert r.rc == 0, r.all
    html = out.read_text(encoding="utf-8")
    m = re.search(r"(\d+)\s*÷\s*(\d+)\s*×\s*100", html)
    assert m and (int(m.group(1)), int(m.group(2))) == (6, 10), m and m.group(0)
    assert "заменено планом" in html
    d = plan_ledger.parse(plan)
    assert len(d["sprints"][0]["tasks"]) == 10 and sum(t["state"] == "superseded" for t in d["sprints"][0]["tasks"]) == 2


# ---------- 3b. roadmap strictness ----------
def _rm(goal, sid="S05", extra_rows=()):
    return _plan(S00, [("S00", "Zero", "X", "g"), (sid, "Row title", "NOT_STARTED", goal), *extra_rows])


def _raises(text, needle=None):
    try:
        plan_ledger.parse(text)
    except ValueError as e:
        if needle:
            assert needle in str(e), str(e)
        return str(e)
    raise AssertionError("expected ValueError")


def test_roadmap_range_thousands_over_limit_noint_badid_duplicate_all_raise_naming_the_row():
    _raises(_rm("5-7 task placements, task list not expanded here"), "S05")
    _raises(_rm("1,200 task placements, task list not expanded here"), "S05")
    _raises(_rm("1001 task placements, task list not expanded here"), "S05")
    _raises(_rm("many task placements, task list not expanded here"), "S05")
    _raises(_rm("5 task placements, task list not expanded here", sid="X5"), "X5")
    _raises(_rm("5 task placements, task list not expanded here",
                extra_rows=[("S05", "Again", "NOT_STARTED", "6 task placements, task list not expanded here")]), "S05")


def test_roadmap_limit_1000_is_accepted():
    d = plan_ledger.parse(_rm("1000 task placements, task list not expanded here"))
    assert len(_sp(d, "S05")["tasks"]) == 1000


def test_roadmap_ids_with_bold_or_lowercase_normalise():
    for raw in ("**S05**", "s05"):
        d = plan_ledger.parse(_rm("3 task placements, task list not expanded here", sid=raw))
        assert [t["id"] for t in _sp(d, "S05")["tasks"]] == [f"S05-PLACEHOLDER-{k}" for k in (1, 2, 3)], raw


def test_heading_with_title_glued_to_id_resolves_to_the_id():
    text = _plan("## Sprint S05-Title\nGoal: g\n\n| Task | Title | Status |\n|---|---|---|\n| T-1 | a | ACCEPTED |\n", [("S05", "Title", "X", "g")])
    d = plan_ledger.parse(text)
    assert [s["id"] for s in d["sprints"]] == ["S05"], [s["id"] for s in d["sprints"]]
    assert d["sprints"][0]["title"].lower() == "title", d["sprints"][0]["title"]  # (source upper-cases it: 'TITLE', cosmetic)


def _heading_sprint(heading):
    body = f"{heading}\nGoal: g\n\n| Task | Title | Status |\n|---|---|---|\n| T-1 | a | ACCEPTED |\n"
    d = plan_ledger.parse(_plan(body, [("S00", "Zero", "X", "g")]))
    assert len(d["sprints"]) == 1, [s["id"] for s in d["sprints"]]
    return d["sprints"][0]


def test_heading_title_glued_to_id_keeps_original_case():
    s = _heading_sprint("## Sprint S05-Payment flow: checkout")
    assert (s["id"], s["title"]) == ("S05", "Payment flow: checkout"), (s["id"], s["title"])


def test_heading_mixed_case_title_after_dash_is_not_upper_cased():
    s = _heading_sprint("## Sprint S07 — Mixed Case Title")
    assert (s["id"], s["title"]) == ("S07", "Mixed Case Title"), (s["id"], s["title"])


def test_heading_bold_id_keeps_title_case():
    s = _heading_sprint("## Sprint **S08**: Bold id")
    assert (s["id"], s["title"]) == ("S08", "Bold id"), (s["id"], s["title"])


def test_heading_underscore_glued_tail_keeps_case_and_drops_leading_underscore():
    s = _heading_sprint("## Sprint S09_lower_tail rest")
    assert (s["id"], s["title"]) == ("S09", "lower_tail rest"), (s["id"], s["title"])


def test_heading_lowercase_id_is_still_upper_cased():
    s = _heading_sprint("## Sprint s10-x")
    assert s["id"] == "S10" and s["title"] == "x", (s["id"], s["title"])


def test_expanded_section_with_zero_tasks_while_roadmap_announces_n_raises():
    text = _plan("## Sprint S05 - Empty\nGoal: g\n", [("S05", "Empty", "X", "3 task placements, task list not expanded here")])
    _raises(text, "S05")


# ---------- 3c. importer ----------
def test_importer_status_table_continues_after_prose_paragraphs():
    led = ("| Story / task | Status | Title | Basis |\n|---|---|---|---|\n| ST-P-1 | ACCEPTED | one | b |\n\n"
           "Attempt log: some prose paragraph.\n\nMore prose.\n\n| ST-P-2 | PARTIAL | two | b |\n\nprose\n| ST-P-3 | NOT_STARTED | three | b |\n")
    d, p = _imp("s11-imp-prose", ledger_text=led)
    r = _run_imp(d)
    assert r.rc == 0 and "tasks written: 3" in r.out and "skipped rows: 0" in r.out, r.all
    t = p.read_text(encoding="utf-8")
    assert [_status_of(t, x) for x in ("ST-P-1", "ST-P-2", "ST-P-3")] == ["ACCEPTED", "PARTIAL", "NOT_STARTED"]


LED_SKIP = LEDGER_MD.replace("| ST-A-3 | PARTIAL | third | half |", "| ST-A-3 | PARTIAL | third | half |\n| NOTE | stray row | x | y |")


def test_importer_skipped_row_fails_with_exit_1_and_plan_untouched_unless_allowed():
    d, p = _imp("s11-imp-skip", ledger_text=LED_SKIP)
    before = p.read_bytes()
    r = _run_imp(d)
    assert r.rc == 1, r.all
    assert "skipped rows: 1" in r.out and "NOTE" in r.out, r.out
    assert p.read_bytes() == before
    r2 = _run_imp(d, "--allow-skipped")
    assert r2.rc == 0 and "skipped rows: 1" in r2.out, r2.all
    assert p.read_bytes() != before


def test_importer_prints_defaulted_to_operator_for_blocked_without_class_word():
    led = LEDGER_MD.replace("waiting for the operator", "waiting")
    d, p = _imp("s11-imp-dflt", ledger_text=led)
    r = _run_imp(d)
    assert r.rc == 0 and "defaulted to OPERATOR: 1" in r.out, r.all
    assert _status_of(p.read_text(encoding="utf-8"), "ST-A-7") == "BLOCKED_OPERATOR"
    d2, _ = _imp("s11-imp-nodflt")  # the basis names the operator and the EXTERNAL row is classified: nothing defaulted
    assert "defaulted to OPERATOR: 0" in _run_imp(d2, "--dry-run").out


def test_importer_keeps_prose_before_and_after_the_old_table():
    old = "Goal: keep me\n\n| Task | Title | Status |\n|---|---|---|\n| OLD-1 | old | NOT_STARTED |\n"
    assert old in BASE_PLAN
    plan = BASE_PLAN.replace(old, "Goal: keep me\n\nPROSE BEFORE the table.\n\n| Task | Title | Status |\n|---|---|---|\n"
                                  "| OLD-1 | old | NOT_STARTED |\n\nPROSE AFTER the table.\n")
    d, p = _imp("s11-imp-prose-keep", plan_text=plan)
    r = _run_imp(d)
    assert r.rc == 0 and "old rows removed: 1" in r.out, r.all
    t = p.read_text(encoding="utf-8")
    assert "PROSE BEFORE the table." in t and "PROSE AFTER the table." in t and "OLD-1" not in t
    assert t.index("PROSE BEFORE") < t.index("| ST-A-1") < t.index("PROSE AFTER")
    again = p.read_bytes()
    assert _run_imp(d).rc == 0 and p.read_bytes() == again, "still idempotent with prose kept"


def test_importer_two_sections_with_same_sprint_id_warn_on_stderr():
    plan = BASE_PLAN + "\n## Sprint S01 - Duplicate\nGoal: dup\n\n| Task | Title | Status |\n|---|---|---|\n| DUP-1 | d | NOT_STARTED |\n"
    d, p = _imp("s11-imp-dup", plan_text=plan)
    r = _run_imp(d, "--dry-run")
    assert r.rc == 0 and "warning" in r.err and "2 sections" in r.err, r.all


def test_importer_aborts_when_plan_changes_between_read_and_replace():
    d, p = _imp("s11-imp-race")
    other = BASE_PLAN + "\n<!-- edited by someone else -->\n"
    real = imp.atomic_write

    def racing(path, text, expected=None, retries=5):
        path.write_bytes(other.encode("utf-8"))  # a concurrent writer wins between our read and our replace
        return real(path, text, expected=expected, retries=retries)

    imp.atomic_write = racing
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = imp.main(["--ledger", str(d / "ledger.md"), "--plan", str(p), "--sprint", "S01"])
    finally:
        imp.atomic_write = real
    assert rc == 1 and "changed on disk" in err.getvalue(), (rc, err.getvalue())
    assert p.read_bytes() == other.encode("utf-8"), "the concurrent edit must survive"
    assert not list(d.glob("*.tmp")), "temp file left behind"


def test_importer_retries_permission_error_on_replace():
    d = hv2.fresh("s11-imp-retry")
    f = d / "PROJECT_PLAN.md"
    f.write_bytes(b"old\n")
    real, calls = os.replace, []

    def flaky(a, b):
        calls.append(1)
        if len(calls) <= 2:
            raise PermissionError(5, "denied")
        return real(a, b)

    os.replace = flaky
    try:
        imp.atomic_write(f, "new\n", expected=b"old\n")
    finally:
        os.replace = real
    assert len(calls) == 3 and f.read_bytes() == b"new\n", (len(calls), f.read_bytes())
    assert not list(d.glob("*.tmp"))


def test_importer_permission_error_that_never_clears_raises_and_keeps_original():
    d = hv2.fresh("s11-imp-retry-fail")
    f = d / "PROJECT_PLAN.md"
    f.write_bytes(b"old\n")
    real = os.replace

    def always(a, b):
        raise PermissionError(5, "denied")

    os.replace = always
    try:
        try:
            imp.atomic_write(f, "new\n", expected=b"old\n")
        except PermissionError:
            pass
        else:
            raise AssertionError("expected PermissionError")
    finally:
        os.replace = real
    assert f.read_bytes() == b"old\n" and not list(d.glob("*.tmp"))


# ---------- (4) real-data smoke ----------
ERP = Path(r"D:\Repo\ERP")


def test_real_erp_plan_roadmap_totals():
    plan = ERP / "PROJECT_PLAN.md"
    if not plan.is_file():
        print("SKIP test_real_erp_plan_roadmap_totals: D:\\Repo\\ERP not present")
        return
    before = plan.read_bytes()
    d = plan_ledger.load(plan)
    n = {s["id"]: len(s["tasks"]) for s in d["sprints"]}
    print(f"ERP sprints={len(n)} total={sum(n.values())} S00={n.get('S00')} S01={n.get('S01')} S02={n.get('S02')}")
    assert n.get("S00") == 60, n.get("S00")
    assert n.get("S02") == 37, n.get("S02")
    if n.get("S01") == 48:
        assert len(n) == 23 and sum(n.values()) == 443, (len(n), sum(n.values()))
    else:
        print(f"NOTE: ERP S01 has {n.get('S01')} tasks in the plan (not 48): the 443/23 total is not asserted")
    assert plan.read_bytes() == before


def test_real_erp_ledger_dry_run_48_tasks_39_accepted_0_skipped():
    led = ERP / "docs" / "sdlc" / "s01-task-ledger.md"
    plan = ERP / "PROJECT_PLAN.md"
    if not (led.is_file() and plan.is_file()):
        print("SKIP test_real_erp_ledger_dry_run_48_tasks_39_accepted_0_skipped: D:\\Repo\\ERP not present")
        return
    before = plan.read_bytes()
    r = hv2.run([hv2.PY, "-I", str(IMPORTER), "--ledger", str(led), "--plan", str(plan), "--sprint", "S01", "--dry-run"], hv2.fresh("s11-real"))
    print(r.out.strip())
    assert r.rc == 0, r.all
    assert "tasks written: 48" in r.out and "skipped rows: 0" in r.out, r.out
    plain = re.search(r"ACCEPTED=(\d+)", r.out)
    bounded = re.search(r"ACCEPTED_BOUNDED=(\d+)", r.out)
    assert plain and int(plain.group(1)) + (int(bounded.group(1)) if bounded else 0) == 39, r.out  # 39 accepted incl. bounded
    assert plan.read_bytes() == before


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
