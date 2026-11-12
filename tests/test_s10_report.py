"""S10/S11 tests: (S11-T1) Codex installer NEXT line names /hooks, (S10-T2) report merges PROJECT_PLAN.md with the ledger.
Run: C:\\Python314\\python.exe -I tests/test_s10_report.py"""
import copy
import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools"))
import hv2  # noqa: E402
import report  # noqa: E402

ROOT = HERE.parent


# ---------- S11-T1 (supersedes S10-T1): Codex DOES have /hooks ----------
INSTALLER = ROOT / "tools" / "install_codex.py"
_RUN = {}


def _installer_next():
    """NEXT text from the real installer run into a temp home/snapshot; falls back to the source text if it cannot run."""
    if "v" not in _RUN:
        base = hv2.fresh("s11-codex")
        home, snap = base / "home", base / "snapshot"
        home.mkdir()
        (home / "config.toml").write_text("[features]\nhooks = true\n", encoding="utf-8", newline="\n")  # installer precondition
        r = hv2.run([hv2.PY, "-I", str(INSTALLER), "--home", str(home), "--snapshot", str(snap)], base)
        nxt = [ln for ln in r.out.splitlines() if ln.startswith("NEXT:")]
        _RUN["v"] = (r, nxt[0] if r.rc == 0 and nxt else None, home)
    return _RUN["v"]


def test_s11_t1_next_line_names_hooks_command_app_settings_and_new_session():
    r, nxt, _ = _installer_next()
    assert nxt, f"installer did not print a NEXT line (rc={r.rc}): {r.all[-600:]}"
    for needle in ("/hooks", "Settings -> Hooks", "Review hooks", "NEW Codex session", "never writes trust hashes"):
        assert needle in nxt, (needle, nxt)


def test_s11_t1_source_next_text_has_the_same_facts():
    text = INSTALLER.read_text(encoding="utf-8")
    for needle in ("/hooks", "Settings -> Hooks", "Review hooks", "NEW Codex session", "never writes trust hashes"):
        assert needle in text, needle


def test_s11_t1_no_text_claims_codex_has_no_hooks_command():
    bad = []
    pat = re.compile(r"(has|have|with)\s+no\s+[`'\"]?/hooks|no\s+[`'\"]?/hooks\s+command|without\s+[`'\"]?/hooks|lacks?\s+[`'\"]?/hooks|doesn.t\s+(have|support)\s+[`'\"]?/hooks", re.I)
    for d in ("tools", "harness"):
        base = ROOT / d
        if not base.is_dir():
            continue
        for f in base.rglob("*"):
            if not f.is_file() or "__pycache__" in f.parts:
                continue
            try:
                txt = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for i, ln in enumerate(txt.splitlines(), 1):
                if pat.search(ln):
                    bad.append(f"{f.relative_to(ROOT)}:{i}: {ln.strip()[:120]}")
    assert not bad, "; ".join(bad)
    r, nxt, _ = _installer_next()
    assert not nxt or not pat.search(nxt), nxt


def test_s11_t1_installer_never_writes_trusted_hash():
    src = INSTALLER.read_text(encoding="utf-8")
    assert not re.search(r"trusted_hash\s*[=:]", src), "installer source assigns trusted_hash"
    assert not re.search(r"""["']trusted_hash["']""", src), "installer source references a trusted_hash key"
    r, nxt, home = _installer_next()
    assert nxt, r.all[-600:]
    cfg = home / "config.toml"
    if cfg.exists():
        assert "trusted_hash" not in cfg.read_text(encoding="utf-8"), "installed config.toml carries trusted_hash"


# ---------- T2 unit: merge_plan_and_ledger ----------
def _sprint(sid, ids, state="todo"):
    return {"id": sid, "title": sid, "state": "open", "tasks": [{"id": i, "title": i, "state": state} for i in ids]}


def _total(d):
    return sum(len(s["tasks"]) for s in d["sprints"])


def test_t2a_disjoint_ids_two_sprints_48_tasks():
    plan = {"sprints": [_sprint("P1", [f"P-{i}" for i in range(46)])]}
    led = {"sprints": [_sprint("L1", ["L-1", "L-2"])]}
    m = report.merge_plan_and_ledger(plan, led)
    assert len(m["sprints"]) == 2 and _total(m) == 48, (len(m["sprints"]), _total(m))


def test_t2b_same_id_counted_once_ledger_state_wins_and_empty_plan_sprint_dropped():
    plan = {"sprints": [_sprint("P1", ["X-1"], state="todo"), _sprint("P2", ["X-2", "X-3"])]}
    led = {"sprints": [_sprint("L1", ["X-1"], state="done")]}
    m = report.merge_plan_and_ledger(plan, led)
    ids = [t["id"] for s in m["sprints"] for t in s["tasks"]]
    assert sorted(ids) == ["X-1", "X-2", "X-3"], ids
    x1 = [t for s in m["sprints"] for t in s["tasks"] if t["id"] == "X-1"]
    assert len(x1) == 1 and x1[0]["state"] == "done", x1
    assert "P1" not in [s["id"] for s in m["sprints"]], m["sprints"]


def test_t2c_ledger_without_tasks_keeps_plan_sprints_and_takes_ledger_approval_events():
    plan = {"sprints": [_sprint("P1", ["A-1", "A-2"])], "approval": {"who": "plan"}, "events": [{"e": "plan"}]}
    snap = copy.deepcopy(plan["sprints"])
    appr, evs = {"head": "abc"}, [{"e": "led"}]
    for sprints in (None, [], [_sprint("L1", [])]):
        led = {"approval": appr, "events": evs}
        if sprints is not None:
            led["sprints"] = sprints
        m = report.merge_plan_and_ledger(plan, led)
        assert m["sprints"] == snap, (led, m["sprints"])
        assert m["approval"] == appr and m["events"] == evs, m
    # falsy ledger values fall back to the plan's
    m = report.merge_plan_and_ledger(plan, {})
    assert m["sprints"] == snap and m["approval"] == {"who": "plan"} and m["events"] == [{"e": "plan"}], m


def test_t2_merged_source_names_plan_and_ledger():
    led = {"sprints": [_sprint("L1", ["L-1"])]}
    m = report.merge_plan_and_ledger({"sprints": [_sprint("P1", ["P-1"])]}, led)
    assert m["source"] == "PROJECT_PLAN.md + .harness/ledger.json", m.get("source")
    m = report.merge_plan_and_ledger({"sprints": [_sprint("P1", ["P-1"])], "source": "X.md"}, led)
    assert m["source"] == "X.md + .harness/ledger.json", m.get("source")


def test_t2d_sprint_id_collision_gets_ledger_suffix():
    plan = {"sprints": [_sprint("S1", ["A-1"])]}
    led = {"sprints": [_sprint("S1", ["B-1"])]}
    m = report.merge_plan_and_ledger(plan, led)
    sids = [s["id"] for s in m["sprints"]]
    assert sids == ["S1-ledger", "S1"], sids  # ledger sprints first; the colliding ledger id gets the suffix
    assert m["sprints"][0]["tasks"][0]["id"] == "B-1" and m["sprints"][1]["tasks"][0]["id"] == "A-1"
    assert _total(m) == 2


def test_t2_ledger_open_sprint_is_the_active_one_after_merge():
    import ledger as ledger_mod
    plan = {"sprints": [_sprint("P2", ["P-1", "P-2"])]}  # open, not started
    led = {"sprints": [_sprint("L1", ["L-1"])]}  # open
    m = report.merge_plan_and_ledger(plan, led)
    act = ledger_mod.active_sprint(m)
    assert act is not None and act["id"] == "L1", act


# ---------- T2 end to end ----------
PLAN = """---
project: demo
---
# Plan

## Sprint P1 - plan sprint
| Task | Title | Status | Acceptance |
|---|---|---|---|
| PX-1 | first | ACCEPTED | works |
| PX-2 | second | NOT_STARTED | works |
| PX-3 | third | IN_PROGRESS | works |
"""


def _repo(name, plan=True, ledger_tasks=2):
    repo = hv2.mk_repo(name)
    if plan:
        hv2.commit(repo, "PROJECT_PLAN.md", PLAN, "plan")
    if ledger_tasks:
        hv2.setup_tasks(repo, ledger_tasks)
    return repo


def _report(repo, out, *extra):
    return hv2.run([hv2.PY, "-I", str(hv2.TOOLS / "report.py"), "--out", str(out), *extra], repo)


def _formula(html):
    m = re.search(r"(\d+)\s*÷\s*(\d+)\s*×\s*100", html)
    assert m, "no 'N ÷ M × 100' formula in the report"
    return int(m.group(1)), int(m.group(2))


def test_t2_e2e_denominator_is_plan_plus_ledger_tasks():
    repo = _repo("s10-e2e")
    out = repo / "r.html"
    r = _report(repo, out)
    assert r.rc == 0, r.all
    html = out.read_text(encoding="utf-8")
    n, m = _formula(html)
    prov = re.search(r"\(([^()]*?), все спринты", html)
    assert prov and "PROJECT_PLAN.md" in prov.group(1) and ".harness/ledger.json" in prov.group(1), prov and prov.group(1)
    sec = re.search(r'id="active-sprint">(.*?)</section>', html, re.S)
    assert sec, "no active-sprint section"
    assert "S1" in sec.group(1) and "Sprint one" in sec.group(1), sec.group(1)[:300]  # the ledger's open sprint, not plan sprint P1
    assert "P1" not in sec.group(1), sec.group(1)[:300]
    assert m == 3 + 2, (n, m)  # 3 plan tasks + 2 ledger tasks, not 2
    assert n == 1, (n, m)      # only PX-1 is ACCEPTED, ledger tasks are todo


def test_t2_e2e_ledger_only_project_uses_its_own_task_count():
    repo = _repo("s10-ledger-only", plan=False, ledger_tasks=2)
    out = repo / "r.html"
    r = _report(repo, out)
    assert r.rc == 0, r.all
    n, m = _formula(out.read_text(encoding="utf-8"))
    assert (n, m) == (0, 2), (n, m)


def test_t2_e2e_zero_task_plan_keeps_existing_report_byte_identical_exit_1():
    repo = _repo("s10-badplan", plan=False, ledger_tasks=2)
    out = repo / "r.html"
    assert _report(repo, out).rc == 0
    before = out.read_bytes()
    # a plan with a sprint heading but no task rows parses to 0 tasks
    hv2.commit(repo, "PROJECT_PLAN.md", "# Plan\n\n## Sprint P1 - empty\n", "empty plan")
    r = _report(repo, out)
    assert r.rc == 1, (r.rc, r.all)
    assert out.read_bytes() == before, "existing report was modified"
    assert hashlib.sha256(out.read_bytes()).hexdigest() == hashlib.sha256(before).hexdigest()


def test_t2_e2e_unparsable_plan_keeps_existing_report_byte_identical_exit_1():
    repo = _repo("s10-nosprint", plan=False, ledger_tasks=2)
    out = repo / "r.html"
    assert _report(repo, out).rc == 0
    before = out.read_bytes()
    hv2.commit(repo, "PROJECT_PLAN.md", "no sprints here\n", "garbage plan")
    r = _report(repo, out)
    assert r.rc == 1, (r.rc, r.all)
    assert out.read_bytes() == before


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
