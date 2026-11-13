"""S13 ledger_sync tests (criteria 1-5). Temp projects only under D:\\tmp\\harness_v2_tests; the backup dir is patched to a temp folder
in-process; real projects under D:\\Repo are touched by --dry-run only (and their ledger files are checked as unchanged).
Run: C:\\Python314\\python.exe -I tests/test_s13_ledger_sync.py"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools"))
import hv2  # noqa: E402
import ledger_sync  # noqa: E402


def _gen_task(tid, commit):
    return {"id": tid, "title": "genuine " + tid, "criteria": [], "run": "", "risk": "local", "journey": False, "state": "done",
            "commits": [commit], "verify": {"result": "pass", "task_commit": commit}, "review": None, "routing": None, "tokens": None}


def _copy_task(tid, state):
    return {"id": tid, "title": "copy " + tid, "criteria": [], "run": "", "risk": "local", "journey": False, "state": state,
            "commits": [], "verify": {"note": "canonical plan row imported as evidence text"}, "review": None, "routing": None, "tokens": None}


def _repo_with(name, plan_text=None, ledger_obj=None, files=None):
    repo = hv2.mk_repo(name)
    if plan_text is not None:
        (repo / "PROJECT_PLAN.md").write_text(plan_text, encoding="utf-8", newline="\n")
    if ledger_obj is not None:
        (repo / ".harness").mkdir()
        (repo / ".harness" / "ledger.json").write_text(json.dumps(ledger_obj, indent=2), encoding="utf-8", newline="\n")
    for rel, txt in (files or {}).items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(txt, encoding="utf-8", newline="\n")
    return repo


def _sync(repo, dry=False, plan=None):
    bk = hv2.fresh("backup")
    ledger_sync.BACKUP_DIR = bk  # never D:\Backup
    res = ledger_sync.sync_project(repo, dry, plan)
    return res, bk


def _states(repo, sid):
    sp = next(s for s in hv2.load_ledger(repo)["sprints"] if s["id"] == sid)
    out = {}
    for t in sp["tasks"]:
        out[t["state"]] = out.get(t["state"], 0) + 1
    return sp, out


ROAD = "---\nproject: demo\n---\n# Plan\n\n## Roadmap\n| Sprint | Title | Status | Goal |\n|---|---|---|---|\n"
S00_TABLE = ("## Sprint S00 - Zero\n\n| Task | Title | Status |\n|---|---|---|\n"
             "| ST-A#1 | a | ACCEPTED |\n| ST-A#2 | b | ACCEPTED |\n| ST-A#3 | c | PARTIAL |\n"
             "| ST-A#4 | d | SUPERSEDED |\n| ST-A#5 | e | NOT_STARTED |\n")


# ---------- criterion 1 ----------
def _crit1_project():
    plan = (ROAD + "| S00 | Zero | NOT_STARTED | expanded below |\n"
            "| S01 | One | NOT_STARTED | 3/4 tasks closed, task list not expanded |\n"
            "| S02 | Two | NOT_STARTED | 6 task placements, task list not expanded |\n"
            "| S03 | Three | NOT_STARTED | 10 planned tasks, 2 accepted in the report, task list not expanded |\n\n" + S00_TABLE)
    copies = [_copy_task(f"ST-A #{k}", "done") for k in range(1, 6)]
    old = {"version": 1, "project": "demo", "approval": {"head": "abc123", "result": "APPROVE"},
           "events": [{"at": "2026-11-01", "kind": "note", "text": "keep me"}],
           "sprints": [{"id": "S00", "title": "Zero", "branch": "", "state": "closed", "opened": None, "closed": None, "tasks": copies},
                       {"id": "S02", "title": "Two", "branch": "", "state": "open", "opened": None, "closed": None,
                        "tasks": [_gen_task("S02-G1", "a" * 40), _gen_task("S02-G2", "b" * 40)]}]}
    return _repo_with("crit1", plan, old), old


def test_crit1_states_counts_after_real_sync():
    repo, old = _crit1_project()
    res, bk = _sync(repo)
    assert res["status"] == "changed", res
    sp0, s0 = _states(repo, "S00")
    assert len(sp0["tasks"]) == 5 and s0 == {"done": 2, "implemented": 1, "superseded": 1, "todo": 1}, s0
    sp1, s1 = _states(repo, "S01")
    assert len(sp1["tasks"]) == 4 and s1 == {"done": 3, "todo": 1}, s1
    assert sp1["state"] != "closed", sp1["state"]
    sp2, s2 = _states(repo, "S02")
    ids2 = [t["id"] for t in sp2["tasks"]]
    assert len(ids2) == 6 and ids2[:2] == ["S02-G1", "S02-G2"], ids2
    ph = [t for t in sp2["tasks"] if t["title"].startswith("UNVERIFIED")]
    assert len(ph) == 4 and all(t["state"] == "todo" for t in ph), ph
    assert all(t["state"] == "done" for t in sp2["tasks"][:2])
    sp3, s3 = _states(repo, "S03")
    assert len(sp3["tasks"]) == 10 and s3 == {"done": 2, "todo": 8}, s3


def test_crit1_copies_replaced_no_duplicates_approval_events_kept_backup_written():
    repo, old = _crit1_project()
    res, bk = _sync(repo)
    led = hv2.load_ledger(repo)
    ids = [t["id"] for s in led["sprints"] for t in s["tasks"]]
    assert not any(re.search(r"\s", i) for i in ids), [i for i in ids if " " in i]
    norm = [re.sub(r"\s+", "", i) for i in ids]
    assert len(norm) == len(set(norm)), "duplicate ids"
    assert not any("canonical plan row" in json.dumps(t.get("verify")) for s in led["sprints"] for t in s["tasks"]), "copy survived"
    assert led["approval"] == old["approval"] and led["events"] == old["events"]
    files = list(bk.glob("ledger-sync-*.json"))
    assert len(files) == 1 and res.get("backup") == str(files[0]), (files, res.get("backup"))
    assert json.loads(files[0].read_text(encoding="utf-8")) == old, "backup is not the old ledger"


def test_crit1_second_run_unchanged_and_byte_identical():
    repo, _ = _crit1_project()
    _sync(repo)
    p = repo / ".harness" / "ledger.json"
    before = p.read_bytes()
    res2, bk2 = _sync(repo)
    assert res2["status"] == "unchanged", res2["status"]
    assert p.read_bytes() == before
    assert not list(bk2.glob("*.json")), "unchanged run must not write a backup"


def test_dry_run_writes_nothing():
    repo, _ = _crit1_project()
    p = repo / ".harness" / "ledger.json"
    before = p.read_bytes()
    res, bk = _sync(repo, dry=True)
    assert res["status"] == "would change (dry-run)", res["status"]
    assert p.read_bytes() == before and not list(bk.glob("*.json"))


# ---------- criterion 2 ----------
def test_crit2_roadmap_state_words():
    plan = (ROAD + "| S00 | Zero | NOT_STARTED | expanded below |\n"
            "| S01 | Four | NOT_STARTED | 4/4 tasks closed, task list not expanded |\n"
            "| S02 | Big | NOT_STARTED | 40 task placements, 30/40 closed, 40/40 have source code, task list not expanded |\n"
            "| S03 | Gates | NOT_STARTED | 5 task placements, gates 0/5 closed, task list not expanded |\n\n" + S00_TABLE)
    repo = _repo_with("crit2", plan)
    res, _ = _sync(repo)
    assert res["status"] == "created", res
    sp1, s1 = _states(repo, "S01")
    assert sp1["state"] == "closed" and s1 == {"done": 4}, (sp1["state"], s1)
    sp2, s2 = _states(repo, "S02")
    assert s2 == {"done": 30, "implemented": 10}, s2
    sp3, s3 = _states(repo, "S03")
    assert s3 == {"todo": 5} and sp3["state"] != "closed", (s3, sp3["state"])


# ---------- criterion 3 ----------
def test_crit3_task_ledger_md_expansion_with_placeholders():
    plan = (ROAD + "| S00 | Zero | NOT_STARTED | expanded below |\n"
            "| S01 | One | NOT_STARTED | 6 task placements, task list not expanded |\n\n"
            "## Sprint S00 - Zero\n\n| Task | Title | Status |\n|---|---|---|\n| ST-A#1 | a | ACCEPTED |\n")
    tl = ("# S01 task ledger\n\n| Task | Status | Title | Basis |\n|---|---|---|---|\n"
          "| ST-X-01.01 | ACCEPTED | first | evidence one |\n"
          "| ST-X-01.02 | ACCEPTED (BOUNDED) | second | evidence two |\n"
          "| ST-X-01.03 | PARTIAL | third | evidence three |\n"
          "| ST-X-01.04 | SUPERSEDED | fourth | evidence four |\n")
    repo = _repo_with("crit3", plan, files={"docs/sdlc/s01-task-ledger.md": tl})
    res, _ = _sync(repo)
    assert res["status"] == "created", res
    sp, _s = _states(repo, "S01")
    assert len(sp["tasks"]) == 6, [t["id"] for t in sp["tasks"]]
    real = sp["tasks"][:4]
    assert [t["id"] for t in real] == [f"ST-X-01.0{k}" for k in range(1, 5)], [t["id"] for t in real]
    assert [t["state"] for t in real] == ["done", "verified", "implemented", "superseded"], [t["state"] for t in real]
    ph = sp["tasks"][4:]
    assert all(t["state"] == "todo" and t["title"].startswith("UNVERIFIED") for t in ph), ph


# ---------- criterion 4 ----------
def test_crit4_program_plan_master_md_via_profile():
    master = ("# Master plan\n\n## Phase 1\n\n### Спринт M10 — Тестовый спринт\n\n"
              "| № | Статус | Задача | Источник | DoD |\n|---|---|---|---|---|\n"
              "| M10.36 | ЗАКРЫТО | первая задача | план | готово |\n"
              "| M10.37 | ГОТОВО | вторая задача | план | код есть |\n"
              "| M10.37b | ЗАКРЫТО | суффикс b | план | не считается |\n"
              "| M10.38 | ОТКРЫТО | третья задача | план | позже |\n")
    mp = hv2.fresh("master") / "master.ru.md"
    mp.write_text(master, encoding="utf-8", newline="\n")
    repo = _repo_with("crit4", None, files={".harness/profile.json": json.dumps({"program_plan": str(mp)})})
    res, _ = _sync(repo)
    assert res["status"] == "created" and res["plan"] == str(mp), res
    led = hv2.load_ledger(repo)
    got = {t["id"]: t["state"] for s in led["sprints"] for t in s["tasks"]}
    assert got == {"M10.36": "done", "M10.37": "implemented", "M10.38": "todo"}, got


# ---------- criterion 5 ----------
REAL = Path(r"D:\Repo")
_dry = {}


def _real_dry_run():
    if "r" not in _dry:
        before = {n: (REAL / n / ".harness" / "ledger.json") for n in ("ERP", "Fitness_App", "Personal_Decision_Command_Center")}
        snap = {n: (p.stat().st_mtime_ns, p.stat().st_size) for n, p in before.items() if p.is_file()}
        r = hv2.run([hv2.PY, "-I", str(hv2.TOOLS / "ledger_sync.py"), "--all", "--dry-run"], hv2.SRC)
        after = {n: (p.stat().st_mtime_ns, p.stat().st_size) for n, p in before.items() if p.is_file()}
        _dry["r"], _dry["same"] = r, snap == after
    return _dry["r"], _dry["same"]


def _row(out, name):
    for ln in out.splitlines():
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if ln.startswith("|") and cells and cells[0] == name:
            return cells
    return None


def test_crit5_real_projects_dry_run_rows():
    r, same = _real_dry_run()
    assert r.rc == 0, r.all[-1500:]
    assert same, "a real ledger file changed during --dry-run"
    # columns: project | plan tasks | old ledger | new ledger | accepted | ...
    erp = _row(r.out, "ERP")
    fit = _row(r.out, "Fitness_App")
    pdc = _row(r.out, "Personal_Decision_Command_Center")
    assert erp and fit and pdc, r.out[:3000]
    assert erp[1] == "443", erp
    assert (fit[1], fit[4]) == ("243", "229"), fit
    assert (pdc[1], pdc[4]) == ("776", "168"), pdc
    for row in (erp, fit, pdc):
        assert "dry-run" in row[-1] or row[-1] == "unchanged", row


def test_crit5_virtual_marketing_company_absent_and_refused():
    r, _ = _real_dry_run()
    assert _row(r.out, "Virtual_marketing_company") is None and "virtual_marketing" not in r.out.lower()
    r2 = hv2.run([hv2.PY, "-I", str(hv2.TOOLS / "ledger_sync.py"), "--project", r"D:\Repo\Virtual_marketing_company", "--dry-run"], hv2.SRC)
    assert r2.rc == 2 and "never touched" in r2.err, (r2.rc, r2.all)


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
