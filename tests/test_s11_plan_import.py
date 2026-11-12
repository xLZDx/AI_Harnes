"""S11-T2 tests: roadmap placeholders in plan_ledger, merge_plan_and_ledger with placeholder sprints, plan_import_ledger_md importer.
Run: C:\\Python314\\python.exe -I tests/test_s11_plan_import.py"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools"))
import hv2  # noqa: E402
import plan_ledger  # noqa: E402
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


def test_merge_drops_placeholder_sprint_replaced_by_ledger_sprint():
    plan = {"sprints": [_real("S00", 46, "A"), _ph("S02", 37)]}
    led = {"sprints": [_real("S02", 2, "L")]}
    m = report.merge_plan_and_ledger(plan, led)
    counts = {s["id"]: len(s["tasks"]) for s in m["sprints"]}
    assert counts == {"S02": 2, "S00": 46}, counts
    assert sum(counts.values()) == 48
    assert m["sprints"][0]["id"] == "S02"  # ledger sprints first


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
    assert (int(m.group(1)), int(m.group(2))) == (2, 3 + 5 + 2), m.group(0)  # S00: 2 accepted of 3; S01 5 placeholders; ledger S02 2


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
    exp = {"ST-A-1": "ACCEPTED", "ST-A-2": "ACCEPTED", "ST-A-3": "PARTIAL", "ST-A-4": "SUPERSEDED", "ST-A-5": "SUPERSEDED",
           "ST-A-6": "BLOCKED_EXTERNAL", "ST-A-7": "BLOCKED_OPERATOR", "ST-A-8": "ACCEPTED"}
    for tid, st in exp.items():
        assert _status_of(t, tid) == st, (tid, _status_of(t, tid))
    assert _status_of(t, "ST-A-9") is None and "do something" not in t
    assert t.count("| ST-A-8 |") == 1 and "eighth last" in t and "eighth first" not in t
    assert "tasks written: 8" in r.out and "ACCEPTED=3" in r.out, r.out


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
    assert _run_imp(d).rc == 0
    t = p.read_text(encoding="utf-8")
    assert len(re.findall(r"^## Sprint S01\b", t, re.M)) == 1, t
    assert "Goal: keep me" in t and "OLD-1" not in t
    assert len(re.findall(r"^## Sprint S02\b", t, re.M)) == 1 and "| S02-1 |" in t
    # the result still parses as a plan with S01 = 8 tasks minus 2 superseded (left out by the plan reader)
    parsed = plan_ledger.parse(t)
    assert len(_sp(parsed, "S01")["tasks"]) == 6, [x["id"] for x in _sp(parsed, "S01")["tasks"]]


# ---------- (4) real-data smoke ----------
def test_real_erp_ledger_dry_run_48_tasks_39_accepted():
    led = Path(r"D:\Repo\ERP\docs\sdlc\s01-task-ledger.md")
    plan = Path(r"D:\Repo\ERP\PROJECT_PLAN.md")
    if not (led.is_file() and plan.is_file()):
        print("SKIP test_real_erp_ledger_dry_run_48_tasks_39_accepted: D:\\Repo\\ERP not present")
        return
    before = plan.read_bytes()
    r = hv2.run([hv2.PY, "-I", str(IMPORTER), "--ledger", str(led), "--plan", str(plan), "--sprint", "S01", "--dry-run"], hv2.fresh("s11-real"))
    print(r.out.strip())
    assert r.rc == 0, r.all
    assert "tasks written: 48" in r.out and "ACCEPTED=39" in r.out, r.out
    assert plan.read_bytes() == before


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
