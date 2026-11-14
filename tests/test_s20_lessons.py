"""S20-T1 / S20-T2 tests: global lessons store core, CLI, parser and import (harness/hooks/_lessons.py, tools/lessons.py).
Contract comes from D:\\tmp\\s20_criteria.md and docs/S20_GLOBAL_LESSONS_DESIGN.md (revision section).
Every test points HARNESS_LESSONS_DIR, HARNESS_LESSONS_BACKUP and HARNESS_HOOK_STATE at fresh temp folders; the real store is never touched.
Expected values are written by hand from the fixtures below, never taken from a run of the code.
Run: C:\\Python314\\python.exe -I -s tests\\test_s20_lessons.py"""
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

_s = importlib.util.spec_from_file_location("hv2", Path(__file__).resolve().parent / "hv2.py")
h = importlib.util.module_from_spec(_s)
sys.modules["hv2"] = h
_s.loader.exec_module(h)

CLI = h.TOOLS / "lessons.py"
AWS_KEY = "AKIA" + "Q7XJ4N2MPLR8TV3W"  # built in two pieces so the source file holds no complete key


class Store:
    def __init__(self, name):
        root = h.fresh(name)
        self.root = root
        self.dir = root / "store"
        self.backup = root / "backup"
        self.state = root / "state"
        self.env = {"HARNESS_LESSONS_DIR": str(self.dir), "HARNESS_LESSONS_BACKUP": str(self.backup),
                    "HARNESS_HOOK_STATE": str(self.state)}

    def cli(self, *args):
        return h.run([h.PY, "-I", "-s", str(CLI), *args], self.root, self.env)

    def add(self, rule, kind="FAILURE", project="ProjA", actions="push", tags="", extra=()):
        a = ["add", "--kind", kind, "--project", project, "--rule", rule, "--actions", actions, *extra]
        if tags:
            a += ["--tags", tags]
        return self.cli(*a)

    def records(self):
        p = self.dir / "lessons.jsonl"
        if not p.exists():
            return []
        return [json.loads(x) for x in p.read_text(encoding="utf-8").split("\n") if x.strip()]

    def errors(self):
        p = self.dir / "errors.jsonl"
        return [x for x in p.read_text(encoding="utf-8").split("\n") if x.strip()] if p.exists() else []


def ids_of(res):
    return [ln.split(" | ")[0] for ln in res.out.splitlines() if ln.strip()]


# ------------------------------------------------------------------------------------------------ T1 store core and CLI

def test_basic_add_gives_first_id_and_valid_jsonl():
    st = Store("t1basic")
    r = st.add("Run the guard check before every push to the remote.")
    assert r.rc == 0, r.all
    assert json.loads(r.out)["status"] == "added" and json.loads(r.out)["id"] == "L0001", r.out
    recs = st.records()
    assert len(recs) == 1 and recs[0]["id"] == "L0001" and recs[0]["project"] == "ProjA" and recs[0]["kind"] == "FAILURE"
    assert (st.dir / "INDEX.md").exists() and (st.dir / "GLOBAL_LESSONS.md").exists()
    assert "L0001" in (st.dir / "INDEX.md").read_text(encoding="utf-8")


def test_no_free_text_facts_are_stored():
    st = Store("t1nofacts")
    st.add("Check the lock file before deleting it.", extra=("--repo", "ProjA", "--sha", "abc123"))
    rec = st.records()[0]
    assert "facts" not in rec and set(rec) >= {"id", "date", "project", "kind", "tags", "actions", "rule", "source"}, sorted(rec)
    assert rec["source"] == {"repo": "ProjA", "sha": "abc123"}, rec["source"]


def test_eight_concurrent_adds_of_four_rules_give_four_entries():
    st = Store("t1conc")
    rules = [f"Concurrency rule number {n} must be obeyed before any push." for n in range(4)]
    procs = []
    for i in range(8):
        cmd = [h.PY, "-I", "-s", str(CLI), "add", "--kind", "FAILURE", "--project", "ProjA", "--rule", rules[i % 4], "--actions", "push"]
        procs.append(subprocess.Popen(cmd, env=h.base_env(st.env), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                      cwd=str(st.root)))
    outs = [(p.communicate(), p.returncode) for p in procs]
    assert all(rc == 0 for _, rc in outs), outs
    raw = (st.dir / "lessons.jsonl").read_text(encoding="utf-8")
    assert raw.endswith("\n")
    recs = [json.loads(x) for x in raw.split("\n") if x.strip()]  # every line valid json
    assert len(recs) == 4, [r["rule"] for r in recs]
    assert len({r["id"] for r in recs}) == 4 and sorted(r["id"] for r in recs) == ["L0001", "L0002", "L0003", "L0004"]
    assert sorted(r["rule"] for r in recs) == sorted(rules)
    statuses = sorted(json.loads(o[0][0])["status"] for o in outs)
    assert statuses == ["added"] * 4 + ["duplicate"] * 4, statuses


def test_same_rule_from_another_project_is_merged_not_duplicated():
    st = Store("t1merge")
    st.add("Never edit a guard without its test.", project="ProjA")
    r = st.add("never edit a guard, without its TEST!", project="ProjB")  # same normalised text
    assert json.loads(r.out)["status"] == "merged", r.out
    recs = st.records()
    assert len(recs) == 1 and sorted(recs[0]["seen_in"]) == ["ProjA", "ProjB"], recs
    twin = st.add("A completely different rule about push.", project="ProjB")  # passing twin: a new rule is added
    assert json.loads(twin.out)["status"] == "added" and len(st.records()) == 2


def test_truncated_last_line_is_repaired_on_next_add_and_search_still_works():
    st = Store("t1trunc")
    st.add("Rule one about push safety.")
    st.add("Rule two about push safety.")
    p = st.dir / "lessons.jsonl"
    p.write_bytes(p.read_bytes() + b'{"id":"L0003","rule":"half written and cut')  # crash mid-append, no newline
    s = st.cli("search", "--action", "push")
    assert s.rc == 0 and len(ids_of(s)) == 2 and set(ids_of(s)) == {"L0001", "L0002"}, s.all
    r = st.add("Rule three about push safety.")
    assert r.rc == 0 and json.loads(r.out)["status"] == "added", r.all
    raw = p.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    recs = [json.loads(x) for x in raw.split("\n") if x.strip()]  # all lines valid again
    assert [x["rule"] for x in recs] == ["Rule one about push safety.", "Rule two about push safety.", "Rule three about push safety."]
    assert len({x["id"] for x in recs}) == 3


def test_truncated_last_line_logs_a_warning():
    st = Store("t1truncwarn")
    st.add("Rule one about push safety.")
    p = st.dir / "lessons.jsonl"
    p.write_bytes(p.read_bytes() + b'{"id":"L0002","rule":"cut')
    st.cli("search", "--action", "push")
    st.add("Rule two about push safety.")
    assert st.errors(), "criterion: a truncated last line logs a warning; errors.jsonl has no line"


def test_intact_store_logs_no_warning():  # twin of the warning test
    st = Store("t1intact")
    st.add("Rule one about push safety.")
    st.cli("search", "--action", "push")
    st.add("Rule two about push safety.")
    assert st.errors() == []


def test_stale_index_is_rebuilt_by_search():
    st = Store("t1stale")
    st.add("First push rule for the index.")
    # crash between append and rebuild: a valid line reaches the jsonl, INDEX.md is old
    rec = {"id": "L0002", "date": "2026-11-14", "project": "ProjA", "kind": "FAILURE", "tags": [], "actions": ["push"],
           "rule": "Appended behind the index back.", "source": {"repo": "", "sha": ""}, "key": "k2", "idem": "i2", "seen_in": ["ProjA"]}
    p = st.dir / "lessons.jsonl"
    p.write_bytes(p.read_bytes() + json.dumps(rec).encode("utf-8") + b"\n")
    assert "L0002" not in (st.dir / "INDEX.md").read_text(encoding="utf-8")
    s = st.cli("search", "--action", "push")
    assert s.rc == 0 and set(ids_of(s)) == {"L0001", "L0002"}, s.all
    assert "L0002" in (st.dir / "INDEX.md").read_text(encoding="utf-8")


def test_secret_input_is_refused_and_never_stored():
    st = Store("t1secret")
    r = st.add(f"Use the key {AWS_KEY} for the deploy step.")
    assert r.rc == 3 and json.loads(r.out)["status"] == "secret", r.all
    assert st.records() == []
    r2 = st.add("A clean rule about deploys.", tags=f"deploy,{AWS_KEY.lower()},x")  # lower-cased key no longer matches: not a secret
    assert json.loads(r2.out)["status"] == "added"
    r3 = st.add("Another clean rule.", extra=("--repo", AWS_KEY))
    assert r3.rc == 3 and json.loads(r3.out)["status"] == "secret"
    blob = "".join(p.read_text(encoding="utf-8", errors="replace") for p in st.dir.rglob("*") if p.is_file())
    assert AWS_KEY not in blob, "the key reached a store file"
    assert [x["rule"] for x in st.records()] == ["A clean rule about deploys."]  # passing twin kept, refused ones absent


def test_search_limit_ranking_and_kind_filter():
    st = Store("t1search")
    for n in range(5):
        st.add(f"Push rule {n} says verify the branch first.", actions="push")
    st.add("Commit only rule about trailers.", kind="SHORTCOMING", actions="commit")
    st.add("General fallback rule about habits.", actions="general")
    st.add("Success story about push that must not be recalled.", kind="SUCCESS", actions="push")
    r = st.cli("search", "--action", "push", "--limit", "3")
    got = ids_of(r)
    assert r.rc == 0 and len(got) == 3, r.all
    assert set(got) <= {"L0001", "L0002", "L0003", "L0004", "L0005"}, got  # push-tagged only, first
    assert "L0008" not in ids_of(st.cli("search", "--action", "push", "--limit", "50"))  # SUCCESS excluded
    full = ids_of(st.cli("search", "--action", "push", "--limit", "50"))
    assert full.index("L0006") > full.index("L0005") if "L0006" in full else True  # commit-only never ahead of push
    assert "L0007" not in full[:5], full  # general fallback ranks after the five push rules
    assert len(ids_of(st.cli("search", "--action", "push", "--limit", "0"))) == 0
    assert len(ids_of(st.cli("search", "--action", "commit"))) >= 1  # twin: another action finds its own entry


def test_search_prefers_more_seen_rule_among_equals():
    st = Store("t1seen")
    st.add("Rule seen once about push.", project="ProjA")
    st.add("Rule seen twice about push.", project="ProjA")
    st.add("Rule seen twice about push.", project="ProjB")
    assert ids_of(st.cli("search", "--action", "push"))[0] == "L0002"


def test_missing_store_gives_exit_zero_and_no_output():
    st = Store("t1missing")
    r = st.cli("search", "--action", "push")
    assert r.rc == 0 and r.out.strip() == "", r.all
    assert r.err.strip() == "" or "Traceback" not in r.err, r.err


def test_missing_store_logs_one_line():
    st = Store("t1missinglog")
    st.cli("search", "--action", "push")
    assert len(st.errors()) == 1, f"criterion: a missing store gives one log line, got {len(st.errors())}"


def test_corrupt_store_gives_exit_zero_and_no_output():
    st = Store("t1corrupt")
    st.dir.mkdir(parents=True)
    (st.dir / "lessons.jsonl").write_bytes(b"\xff\xfe not json at all\n{broken\n")
    (st.dir / "INDEX.md").write_text("garbage\n", encoding="utf-8")
    r = st.cli("search", "--action", "push")
    assert r.rc == 0 and r.out.strip() == "" and "Traceback" not in r.err, r.all
    # passing twin: a later good add still works and the corrupt last line is dropped
    ok = st.add("Good rule after corruption about push.")
    assert ok.rc == 0 and json.loads(ok.out.splitlines()[0])["status"] == "added"


def test_corrupt_store_logs_one_line():
    st = Store("t1corruptlog")
    st.dir.mkdir(parents=True)
    (st.dir / "lessons.jsonl").write_bytes(b"garbage line\n")
    st.cli("search", "--action", "push")
    assert len(st.errors()) == 1, f"criterion: a corrupt store gives one log line, got {len(st.errors())}"


def test_store_path_that_is_a_file_fails_open():
    st = Store("t1filepath")
    st.root.mkdir(parents=True, exist_ok=True)
    st.dir.write_text("i am a file, not a folder", encoding="utf-8")
    r = st.cli("search", "--action", "push")
    assert r.rc == 0 and r.out.strip() == "" and "Traceback" not in r.err, r.all
    a = st.add("Rule that cannot be stored.")
    assert a.rc == 1 and "Traceback" not in a.err, a.all


def test_show_prints_full_entry_and_ignores_unknown_ids():
    st = Store("t1show")
    st.add("Rule to show about push.", project="ProjZ")
    r = st.cli("show", "L0001", "L9999")
    assert r.rc == 0
    obj = json.loads(r.out)  # exactly one object printed
    assert obj["id"] == "L0001" and obj["project"] == "ProjZ" and obj["rule"] == "Rule to show about push."


def test_rebuild_makes_dated_backup_and_restore_recovers():
    st = Store("t1backup")
    st.add("Backed up rule one about push.")
    st.add("Backed up rule two about push.")
    assert st.cli("rebuild").rc == 0
    files = sorted(st.backup.glob("lessons-*.jsonl"))
    assert files, "no dated backup after rebuild"
    assert files[-1].read_bytes() == (st.dir / "lessons.jsonl").read_bytes()
    (st.dir / "lessons.jsonl").write_bytes(b"destroyed\n")
    r = st.cli("restore")
    assert r.rc == 0 and "restored from" in r.out, r.all
    assert [x["rule"] for x in st.records()] == ["Backed up rule one about push.", "Backed up rule two about push."]
    assert set(ids_of(st.cli("search", "--action", "push"))) == {"L0001", "L0002"}  # index rebuilt too


def test_restore_without_backup_changes_nothing():
    st = Store("t1norestore")
    st.add("Rule that stays about push.")
    before = (st.dir / "lessons.jsonl").read_bytes()
    assert list(st.backup.glob("lessons-*.jsonl")), "the add itself should have produced a backup"
    for f in st.backup.glob("*"):  # emulate a machine without the backup drive
        f.unlink()
    r = st.cli("restore")
    assert r.rc == 0 and "no backup" in r.out and (st.dir / "lessons.jsonl").read_bytes() == before


def test_backup_keeps_only_the_last_twenty():
    st = Store("t1keep")
    for n in range(23):
        assert json.loads(st.add(f"Distinct rule number {n} about push.").out)["status"] == "added"
    assert len(list(st.backup.glob("lessons-*.jsonl"))) == 20


# ------------------------------------------------------------------------------------------------ T2 parser and import

# Hand-counted fixture. Expected, written before any run:
#   entries with a rule: 6 -> FAILURE 3 (A, D, E), SHORTCOMING 1 (B), SUCCESS 2 (C, F); entries without a rule: 2.
FIXTURE = """# Lessons log

## 2026-01-01 Block with two entries
- Kind: FAILURE. Facts: something broke on a push.
- Root cause: no check.
- Rule: always run check A before every commit.
- Kind: SHORTCOMING. Facts: a smaller miss.
- Rule: verify B before the push.

## 2026-01-02 A success block
- Kind: SUCCESS. Facts: it went well.
- Why it worked: measured first.
- Rule: keep measuring C before changes.

## 2026-01-03 Failure without a rule
- Kind: FAILURE. Facts: nobody wrote what to do next time.

## 2026-01-04 Plain lines
Kind: FAILURE
Facts: plain lines without bullets.
Rule: plain rule D needs a test.

## 2026-01-05 One line entries
- FAILURE 2026-01-05 S1-T1: a one line failure. Next time: one line rule E for the hook.
- SUCCESS 2026-01-06 S1-T2: a one line success. Next time: one line rule F stays.
- SHORTCOMING 2026-01-07 S1-T3: a one line miss with nothing to do next.
"""
EXPECT_RULES = {
    "always run check A before every commit.": "FAILURE",
    "verify B before the push.": "SHORTCOMING",
    "keep measuring C before changes.": "SUCCESS",
    "plain rule D needs a test.": "FAILURE",
    "one line rule E for the hook.": "FAILURE",
    "one line rule F stays.": "SUCCESS",
}


def mk_project(parent, name, log=FIXTURE):
    p = Path(parent) / name
    (p / "core").mkdir(parents=True)
    (p / "core" / "LESSONS_LOG.md").write_text(log, encoding="utf-8", newline="\n")
    return p


def counts(line):
    return {k: int(v) for k, v in re.findall(r"([a-z-]+)=(-?\d+)", line)}


def line_of(res, name):
    for ln in res.out.splitlines():
        if ln.startswith(name + ":"):
            return ln
    raise AssertionError(f"no line for {name}: {res.out}")


def test_parser_on_fixture_counts_kinds_rules_and_skips():
    sys.path.insert(0, str(h.SRC / "harness" / "hooks"))
    import _lessons as L
    parsed = L.parse_log(FIXTURE)
    assert parsed["no_rule"] == 2 and parsed["neutral"] == 0 and parsed["total"] == 8, parsed
    got = {e["rule"]: e["kind"] for e in parsed["entries"]}
    assert got == EXPECT_RULES, got
    assert L.parse_log("")["entries"] == [] and L.parse_log("# only a title\n")["total"] == 0  # empty input


def test_import_fixture_gives_known_counts_and_reports_skips():
    st = Store("t2fixture")
    proj = mk_project(st.root, "ProjA")
    r = st.cli("import", "--repo", str(proj))
    assert r.rc == 0, r.all
    c = counts(line_of(r, "ProjA"))
    assert c["added"] == 6 and c["skipped-no-rule"] == 2 and c["merged"] == 0 and c["duplicate"] == 0, c
    recs = st.records()
    assert {x["rule"]: x["kind"] for x in recs} == EXPECT_RULES
    kinds = [x["kind"] for x in recs]
    assert (kinds.count("FAILURE"), kinds.count("SHORTCOMING"), kinds.count("SUCCESS")) == (3, 1, 2)
    assert {x["project"] for x in recs} == {"ProjA"}
    # SUCCESS entries are stored but never recalled
    found = ids_of(st.cli("search", "--action", "commit", "--limit", "20"))
    kinds_found = {x["kind"] for x in recs if x["id"] in found}
    assert "SUCCESS" not in kinds_found and kinds_found, found


def test_second_import_adds_zero():
    st = Store("t2idem")
    proj = mk_project(st.root, "ProjA")
    st.cli("import", "--repo", str(proj))
    before = (st.dir / "lessons.jsonl").read_bytes()
    r = st.cli("import", "--repo", str(proj))
    c = counts(line_of(r, "ProjA"))
    assert c["added"] == 0 and c["duplicate"] == 6, c
    assert (st.dir / "lessons.jsonl").read_bytes() == before


def test_reimport_after_cosmetic_rule_edit_adds_zero():
    st = Store("t2edit")
    proj = mk_project(st.root, "ProjA")
    st.cli("import", "--repo", str(proj))
    edited = FIXTURE.replace("always run check A before every commit.", "Always RUN check A, before every commit!")
    assert edited != FIXTURE
    (proj / "core" / "LESSONS_LOG.md").write_text(edited, encoding="utf-8", newline="\n")
    r = st.cli("import", "--repo", str(proj))
    assert counts(line_of(r, "ProjA"))["added"] == 0, r.out
    assert len(st.records()) == 6
    # twin: a genuinely new entry IS added, so the zero above is not a dead import
    (proj / "core" / "LESSONS_LOG.md").write_text(edited + "\n## 2026-01-09 New\n- Kind: FAILURE. Facts: x.\n- Rule: brand new rule G.\n",
                                                  encoding="utf-8", newline="\n")
    r = st.cli("import", "--repo", str(proj))
    assert counts(line_of(r, "ProjA"))["added"] == 1 and len(st.records()) == 7


def test_reimport_after_facts_only_edit_adds_zero():
    st = Store("t2facts")
    proj = mk_project(st.root, "ProjA")
    st.cli("import", "--repo", str(proj))
    (proj / "core" / "LESSONS_LOG.md").write_text(FIXTURE.replace("something broke on a push", "a broken push, reworded"),
                                                  encoding="utf-8", newline="\n")
    r = st.cli("import", "--repo", str(proj))
    assert counts(line_of(r, "ProjA"))["added"] == 0, r.out
    assert len(st.records()) == 6


def test_per_project_counts_and_merge_across_projects():
    st = Store("t2perproject")
    a = mk_project(st.root, "ProjA")
    b = mk_project(st.root, "ProjB", "## 2026-02-01 B only\n- Kind: FAILURE. Facts: b.\n- Rule: rule unique to B.\n"
                                      "- Kind: FAILURE. Facts: again.\n- Rule: Always run check A before every commit\n")
    r = st.cli("import", "--repo", str(a), "--repo", str(b))
    assert r.rc == 0, r.all
    ca, cb = counts(line_of(r, "ProjA")), counts(line_of(r, "ProjB"))
    assert ca["added"] == 6 and cb["added"] == 1 and cb["merged"] == 1, (ca, cb)
    total = counts(line_of(r, "TOTAL"))
    assert total["added"] == 7 and total["merged"] == 1 and total["skipped-no-rule"] == 2, total
    shared = [x for x in st.records() if x["rule"].startswith("always run check A")][0]
    assert sorted(shared["seen_in"]) == ["ProjA", "ProjB"]


def test_excluded_projects_add_zero_and_twin_project_adds():
    st = Store("t2excl")
    root = st.root / "projects"
    root.mkdir()
    for n in ("Virtual_marketing_company", "VMC_x", "vmc_lower"):
        mk_project(root, n)
    mk_project(root, "ProjA")
    for n in ("Virtual_marketing_company", "VMC_x", "vmc_lower"):
        r = st.cli("import", "--repo", str(root / n))
        assert r.rc == 0 and "excluded" in r.out and counts(r.out) == {}, (n, r.all)
    assert st.records() == []
    r = st.cli("import", "--all", str(root))  # --all walks every folder with a log: the excluded ones must still add 0
    assert r.rc == 0, r.all
    assert {x["project"] for x in st.records()} == {"ProjA"} and len(st.records()) == 6
    assert "excluded" in r.out or "ProjA" in r.out


def test_import_of_folder_without_log_reports_and_exits_zero():
    st = Store("t2nolog")
    empty = st.root / "ProjEmpty"
    empty.mkdir(parents=True)
    r = st.cli("import", "--repo", str(empty))
    assert r.rc == 0 and "no core/LESSONS_LOG.md" in r.out and st.records() == [], r.all


def test_import_refuses_a_secret_entry_and_counts_it():
    st = Store("t2secret")
    log = "## 2026-03-01 Leak\n- Kind: FAILURE. Facts: leaked.\n- Rule: rotate " + AWS_KEY + " now.\n" \
          "## 2026-03-02 Fine\n- Kind: FAILURE. Facts: ok.\n- Rule: keep keys in env vars.\n"
    proj = mk_project(st.root, "ProjA", log)
    r = st.cli("import", "--repo", str(proj))
    c = counts(line_of(r, "ProjA"))
    assert c["skipped-secret"] == 1 and c["added"] == 1, c
    assert AWS_KEY not in (st.dir / "lessons.jsonl").read_text(encoding="utf-8")


def test_import_real_harness_log_is_idempotent_and_matches_independent_count():
    log_path = Path(r"D:\tmp\scratch_worktrees\hv2-s20\core\LESSONS_LOG.md")
    text = log_path.read_text(encoding="utf-8")
    # independent simple count: one entry starts at a Kind marker or a one-line `- KIND date` marker
    start_re = re.compile(r"^\s*(?:[-*]\s+)?(?:\*\*)?Kind(?:\*\*)?\s*:\s*(?:\*\*)?(FAILURE|SHORTCOMING|SUCCESS)\b"
                          r"|^\s*[-*]\s+(FAILURE|SHORTCOMING|SUCCESS)\s+\d{4}-\d\d-\d\d\b", re.M)
    marks = list(start_re.finditer(text))
    segs = [text[m.start(): (marks[i + 1].start() if i + 1 < len(marks) else len(text))] for i, m in enumerate(marks)]
    segs = [re.split(r"^## ", s, maxsplit=1, flags=re.M)[0] for s in segs]  # a header ends the previous entry
    kind_of = lambda m: m.group(1) or m.group(2)  # noqa: E731
    with_rule = [(kind_of(m), s) for m, s in zip(marks, segs) if re.search(r"(?:Rule|Next time)\s*:", s)]
    ind_total, ind_rule = len(marks), len(with_rule)
    ind_fs = sum(1 for k, _ in with_rule if k in ("FAILURE", "SHORTCOMING"))
    st = Store("t2real")
    proj = st.root / "real"
    (proj / "core").mkdir(parents=True)
    (proj / "core" / "LESSONS_LOG.md").write_text(text, encoding="utf-8", newline="\n")
    r = st.cli("import", "--repo", str(proj))
    assert r.rc == 0, r.all
    c = counts(line_of(r, "real"))
    stored = c["added"] + c["merged"] + c["duplicate"]
    recs = st.records()
    tool_fs = sum(1 for x in recs if x["kind"] in ("FAILURE", "SHORTCOMING"))
    print(f"INFO real log: independent markers={ind_total} with-rule={ind_rule} (FAILURE+SHORTCOMING with rule={ind_fs}); "
          f"tool: counts={c} entries-with-rule={stored} stored-records={len(recs)} stored FAILURE+SHORTCOMING={tool_fs}")
    assert abs(stored - ind_rule) <= 3, (stored, ind_rule)
    assert abs(c["skipped-no-rule"] - (ind_total - ind_rule)) <= 3, (c, ind_total, ind_rule)
    assert 0 < tool_fs <= stored
    before = (st.dir / "lessons.jsonl").read_bytes()
    r2 = st.cli("import", "--repo", str(proj))
    c2 = counts(line_of(r2, "real"))
    assert c2["added"] == 0 and c2["merged"] == 0, c2
    assert (st.dir / "lessons.jsonl").read_bytes() == before
    # the log sits in the Harness_v2 worktree: it was only read
    assert log_path.read_text(encoding="utf-8") == text


# ------------------------------------------------------------------------------------------------ revision 7ae9854: new behaviour

def first_json(res):
    return json.loads(res.out.splitlines()[0])


def n_backups(st):
    return len(list(st.backup.glob("lessons-*.jsonl")))


def mk_rec(n):
    return json.dumps({"id": f"L{n:04d}", "rule": f"Rule {n} about push.", "kind": "FAILURE", "actions": ["push"], "tags": [],
                       "date": "2026-11-01", "project": "ProjA", "key": f"k{n}", "idem": f"i{n}", "seen_in": ["ProjA"],
                       "source": {"repo": "", "sha": ""}}) + "\n"


def test_search_limit_zero_returns_nothing():
    st = Store("t1limit0")
    st.add("Push rule one about checks.")
    assert ids_of(st.cli("search", "--action", "push", "--limit", "0")) == []
    assert len(ids_of(st.cli("search", "--action", "push", "--limit", "1"))) == 1  # twin


def test_backup_is_skipped_when_the_store_shrank_and_taken_when_it_grew():
    st = Store("t1bkshrink")
    for n in range(3):
        st.add(f"Growing rule {n} about push.")
    assert n_backups(st) == 3
    p = st.dir / "lessons.jsonl"
    first_line = p.read_bytes().split(b"\n")[0] + b"\n"
    p.write_bytes(first_line)  # the store lost two records
    r = st.add("Rule after the loss about push.")  # 2 records now, the newest backup holds 3
    assert first_json(r)["status"] == "added"
    assert n_backups(st) == 3 and "backup skipped" in r.out, r.out
    grown = Store("t1bkgrow")  # twin: more records than the newest backup
    grown.add("Rule one about push.")
    grown.add("Rule two about push.")
    assert n_backups(grown) == 2


def test_search_and_idle_rebuild_create_no_backup_but_a_changed_store_does():
    st = Store("t1bksearch")
    st.add("Rule about push.")
    before = n_backups(st)
    st.cli("search", "--action", "push")
    assert n_backups(st) == before
    st.cli("rebuild")  # identical content: no duplicate backup
    assert n_backups(st) == before
    st.add("Another rule about push.")  # twin: new content gets one
    assert n_backups(st) == before + 1


def test_restore_picks_newest_fully_valid_backup_and_lists_counts():
    st = Store("t1restorepick")
    st.backup.mkdir(parents=True)
    (st.backup / "lessons-20261101-100000-001.jsonl").write_text(mk_rec(1) + mk_rec(2), encoding="utf-8")
    (st.backup / "lessons-20261102-100000-001.jsonl").write_text(mk_rec(1) + "this line is damaged\n" + mk_rec(3), encoding="utf-8")
    lst = st.cli("restore", "--list")
    assert lst.rc == 0
    rows = [ln for ln in lst.out.splitlines() if ln.strip()]
    assert rows[0].startswith("lessons-20261102-100000-001.jsonl") and "records=2" in rows[0] and "unparsable=1" in rows[0], rows
    assert rows[1].startswith("lessons-20261101-100000-001.jsonl") and "records=2" in rows[1] and "unparsable=0" in rows[1], rows
    r = st.cli("restore")
    assert r.rc == 0 and "restored from" in r.out and "20261101-100000" in r.out, r.all  # the newer one is damaged: skipped
    assert [x["id"] for x in st.records()] == ["L0001", "L0002"]


def test_restore_picks_the_newest_when_valid_and_from_selects_a_named_file():
    st = Store("t1restorenewest")
    st.backup.mkdir(parents=True)
    (st.backup / "lessons-20261101-100000-001.jsonl").write_text(mk_rec(1), encoding="utf-8")
    (st.backup / "lessons-20261102-100000-001.jsonl").write_text(mk_rec(1) + mk_rec(2), encoding="utf-8")
    assert st.cli("restore").rc == 0
    assert [x["id"] for x in st.records()] == ["L0001", "L0002"]
    r = st.cli("restore", "--from", "lessons-20261101-100000-001.jsonl")
    assert r.rc == 0 and [x["id"] for x in st.records()] == ["L0001"], r.all
    bad = st.cli("restore", "--from", "lessons-no-such-file.jsonl")
    assert bad.rc == 1 and "restore failed" in bad.out
    assert [x["id"] for x in st.records()] == ["L0001"]  # a failed restore changes nothing


def test_default_backup_folder_is_inside_the_store_when_only_the_store_is_redirected():
    st = Store("t1bkdefault")
    env = {"HARNESS_LESSONS_DIR": str(st.dir), "HARNESS_HOOK_STATE": str(st.state)}
    r = h.run([h.PY, "-I", "-s", str(CLI), "add", "--kind", "FAILURE", "--project", "ProjA", "--rule", "Rule about push.",
               "--actions", "push"], st.root, env)
    assert r.rc == 0, r.all
    assert list((st.dir / "backup").glob("lessons-*.jsonl")), "default backup folder is <store>/backup"
    assert not st.backup.exists()
    st2 = Store("t1bkexplicit")  # twin: an explicit HARNESS_LESSONS_BACKUP wins
    st2.add("Rule about push.")
    assert n_backups(st2) == 1 and not (st2.dir / "backup").exists()


def test_non_latin_rules_keep_their_own_identity_and_equal_ones_merge():
    st = Store("t1unicode")
    a = "Всегда проверяй ветку перед отправкой."
    b = "Никогда не удаляй блокировку вручную."
    assert first_json(st.add(a, project="ProjA"))["status"] == "added"
    assert first_json(st.add(b, project="ProjA"))["status"] == "added", "two different Russian rules must not collapse into one"
    r = st.add("ВСЕГДА, проверяй ветку перед отправкой!", project="ProjB")
    assert first_json(r)["status"] == "merged"
    recs = st.records()
    assert len(recs) == 2 and sorted(recs[0]["seen_in"]) == ["ProjA", "ProjB"]


def test_url_credential_and_jwt_are_refused_and_plain_url_is_kept():
    st = Store("t1urlsecret")
    url = "postgres" + "://admin:" + "s3cretPass99" + "@db.internal/main"
    jwt = "eyJ" + "hbGciOiJIUzI1" + "." + "eyJ" + "zdWIiOiIxMjM0NTY3" + "." + "abcDEF123"
    assert first_json(st.add(f"Connect with {url} for the check."))["status"] == "secret"
    assert first_json(st.add(f"Send the header {jwt} with the call."))["status"] == "secret"
    assert first_json(st.add("Read the docs at https://docs.internal/main for the check."))["status"] == "added"  # twin
    blob = (st.dir / "lessons.jsonl").read_text(encoding="utf-8")
    assert "s3cretPass99" not in blob and "zdWIiOiIxMjM0NTY3" not in blob


def test_dropped_truncated_line_is_saved_under_corrupt_and_intact_store_has_none():
    st = Store("t1corruptsave")
    st.add("Rule one about push safety.")
    p = st.dir / "lessons.jsonl"
    p.write_bytes(p.read_bytes() + b'{"id":"L0002","rule":"half written marker text')
    st.add("Rule two about push safety.")
    saved = list((st.dir / "corrupt").glob("*.txt"))
    assert saved and b"half written marker text" in saved[0].read_bytes()
    ok = Store("t1corruptnone")
    ok.add("Rule one about push safety.")
    ok.add("Rule two about push safety.")
    assert not (ok.dir / "corrupt").exists()


def test_lock_timeout_queues_the_entry_with_exit_two_and_next_writer_drains_it():
    st = Store("t1queue")
    st.add("Existing rule about push.")
    lock = st.dir / "lessons.jsonl.lock"
    lock.write_text("99999", encoding="utf-8")  # a fresh lock held by someone else
    try:
        r = st.add("Queued rule about push.")
        assert r.rc == 2 and first_json(r)["status"] == "queued", r.all
        assert len(list((st.dir / "outbox").glob("*.json"))) == 1
        assert [x["rule"] for x in st.records()] == ["Existing rule about push."]
    finally:
        lock.unlink()
    r2 = st.add("Third rule about push.")  # twin: no lock, exit 0, and the outbox is drained
    assert r2.rc == 0
    assert sorted(x["rule"] for x in st.records()) == ["Existing rule about push.", "Queued rule about push.", "Third rule about push."]
    assert not list((st.dir / "outbox").glob("*.json"))


def test_damaged_line_in_the_middle_is_moved_to_corrupt_and_healed_by_the_next_write():
    st = Store("t1heal")
    st.add("First good rule about push.")
    p = st.dir / "lessons.jsonl"
    good = p.read_bytes()
    p.write_bytes(b"this line is damaged MIDMARK\n" + good)
    st.cli("search", "--action", "push")  # a read never heals
    assert b"MIDMARK" in p.read_bytes()
    r = st.add("Second good rule about push.")
    assert first_json(r)["status"] == "added" and "backup skipped" not in r.out, r.out
    data = p.read_bytes()
    assert b"MIDMARK" not in data and len([x for x in data.split(b"\n") if x.strip()]) == 2
    saved = list((st.dir / "corrupt").glob("*.txt"))
    assert saved and b"MIDMARK" in saved[0].read_bytes()
    assert n_backups(st) == 2  # the healed store is healthy again, so its backup is taken
    ok = Store("t1healtwin")  # twin: a healthy store has no corrupt folder
    ok.add("First good rule about push.")
    ok.add("Second good rule about push.")
    assert not (ok.dir / "corrupt").exists()


def test_rebuild_of_a_damaged_store_skips_the_backup_and_a_changed_healthy_store_gets_one():
    st = Store("t1rebuilddamaged")
    st.add("First good rule about push.")
    p = st.dir / "lessons.jsonl"
    p.write_bytes(p.read_bytes() + b"this line is damaged\n" + mk_rec(7).encode("utf-8"))
    r = st.cli("rebuild")
    assert r.rc == 0 and "backup skipped" in r.out, r.all
    assert n_backups(st) == 1
    ok = Store("t1rebuildhealthy")  # twin
    ok.add("First good rule about push.")
    q = ok.dir / "lessons.jsonl"
    q.write_bytes(q.read_bytes() + mk_rec(7).encode("utf-8"))
    r2 = ok.cli("rebuild")
    assert "backup skipped" not in r2.out and n_backups(ok) == 2, r2.all


def test_the_same_damaged_line_is_logged_once_across_repeated_reads():
    st = Store("t1logonce")
    st.dir.mkdir(parents=True)
    (st.dir / "lessons.jsonl").write_bytes(b"garbage line\n")
    st.cli("search", "--action", "push")
    first = len(st.errors())
    st.cli("search", "--action", "push")
    st.cli("search", "--action", "push")
    assert first >= 1 and len(st.errors()) == first, (first, len(st.errors()))


def test_backups_resume_at_once_after_a_restore_to_a_smaller_backup():
    st = Store("t1restoreresume")
    for n in range(3):
        st.add(f"Resume rule {n} about push.")
    assert n_backups(st) == 3
    rows = [ln for ln in st.cli("restore", "--list").out.splitlines() if ln.strip()]
    oldest = rows[-1].split(" | ")[0]
    assert st.cli("restore", "--from", oldest).rc == 0 and len(st.records()) == 1
    before = n_backups(st)
    r = st.add("Rule after the restore about push.")  # 2 records, the newest backup holds 3
    assert first_json(r)["status"] == "added" and "backup skipped" not in r.out, r.out
    assert n_backups(st) == before + 1
    shrunk = Store("t1noresume")  # twin: the same shrink without a restore is still refused
    for n in range(3):
        shrunk.add(f"Resume rule {n} about push.")
    sp = shrunk.dir / "lessons.jsonl"
    sp.write_bytes(sp.read_bytes().split(b"\n")[0] + b"\n")
    r2 = shrunk.add("Rule after the loss about push.")
    assert "backup skipped" in r2.out


def test_backup_folder_follows_a_store_moved_only_by_hook_state():
    st = Store("t1bkstate")
    env = {"HARNESS_HOOK_STATE": str(st.state)}  # no HARNESS_LESSONS_DIR, no HARNESS_LESSONS_BACKUP
    r = h.run([h.PY, "-I", "-s", str(CLI), "add", "--kind", "FAILURE", "--project", "ProjA", "--rule", "Rule about push.",
               "--actions", "push"], st.root, env)
    assert r.rc == 0, r.all
    assert list((st.state / "lessons" / "backup").glob("lessons-*.jsonl")), "backup must sit beside the moved store"
    assert (st.state / "lessons" / "lessons.jsonl").is_file()


def test_add_exit_codes_zero_ok_one_failed_three_refused():
    st = Store("t1exit")
    assert st.add("A good rule about push.").rc == 0
    assert st.add("A good rule about push.").rc == 0  # duplicate is still success
    assert st.add(AWS_KEY + " leaked in the rule").rc == 3
    assert st.add("Rule from an excluded project.", project="VMC_x").rc == 3
    assert st.add("   ").rc == 3  # blank rule: invalid
    broken = Store("t1exitfail")
    broken.root.mkdir(parents=True, exist_ok=True)
    broken.dir.write_text("a file where the store folder should be", encoding="utf-8")
    r = broken.add("A good rule about push.")
    assert r.rc == 1 and "Traceback" not in r.err, r.all
    assert [x["rule"] for x in st.records()] == ["A good rule about push."]


def test_commit_shas_are_kept_as_provenance_without_creating_a_second_record():
    st = Store("t1shas")
    st.add("Provenance rule about push.", extra=("--sha", "aaa111", "--date", "2026-11-01"))
    r = st.add("Provenance rule about push.", extra=("--sha", "bbb222", "--date", "2026-11-01"))
    assert first_json(r)["status"] == "duplicate"
    recs = st.records()
    assert len(recs) == 1 and recs[0]["shas"] == ["aaa111", "bbb222"], recs
    plain = Store("t1noshas")  # twin: no sha given, no provenance list
    plain.add("Provenance rule about push.")
    assert "shas" not in plain.records()[0]


BLOCK_AX = ("## 2026-05-01 Block\n- Kind: FAILURE. Facts: a.\n- Rule: rule A about push.\n"
            "- Kind: FAILURE. Facts: x.\n- Rule: rule X about commit.\n")
BLOCK_ANX = ("## 2026-05-01 Block\n- Kind: FAILURE. Facts: a.\n- Rule: rule A about push.\n"
             "- Kind: FAILURE. Facts: n.\n- Rule: rule N about hooks.\n"
             "- Kind: FAILURE. Facts: x.\n- Rule: rule X about commit.\n")


def test_inserting_an_entry_between_two_stored_ones_adds_it_and_overwrites_nothing():
    st = Store("t2insert")
    proj = mk_project(st.root, "ProjA", BLOCK_AX)
    c0 = counts(line_of(st.cli("import", "--repo", str(proj)), "ProjA"))
    assert c0["added"] == 2
    (proj / "core" / "LESSONS_LOG.md").write_text(BLOCK_ANX, encoding="utf-8", newline="\n")
    c = counts(line_of(st.cli("import", "--repo", str(proj)), "ProjA"))
    assert c["added"] == 1 and c["duplicate"] == 2 and c["updated"] == 0, c
    assert sorted(x["rule"] for x in st.records()) == ["rule A about push.", "rule N about hooks.", "rule X about commit."]
    keys = [x["key"] for x in st.records()]
    assert len(keys) == len(set(keys))
    again = counts(line_of(st.cli("import", "--repo", str(proj)), "ProjA"))  # twin: now fully stable
    assert again["added"] == 0 and again["updated"] == 0 and again["duplicate"] == 3, again


def test_reword_into_a_rule_another_record_already_holds_does_not_overwrite_or_duplicate():
    st = Store("t2rewordclash")
    log = "## 2026-05-01 One\n- Kind: FAILURE. Facts: a.\n- Rule: rule A about push.\n## 2026-05-02 Two\n- Kind: FAILURE. Facts: b.\n- Rule: rule B about commit.\n"
    proj = mk_project(st.root, "ProjA", log)
    st.cli("import", "--repo", str(proj))
    (proj / "core" / "LESSONS_LOG.md").write_text(log.replace("rule B about commit.", "Rule A about push!"), encoding="utf-8", newline="\n")
    c = counts(line_of(st.cli("import", "--repo", str(proj)), "ProjA"))
    print(f"INFO reword into an existing rule (same project): {c}")
    assert c["added"] == 0 and c["updated"] == 0 and c["merged"] + c["duplicate"] == 2, c
    recs = st.records()
    assert sorted(x["rule"] for x in recs) == ["rule A about push.", "rule B about commit."], recs  # nothing overwritten
    assert len({x["key"] for x in recs}) == 2


def test_reworded_rule_adds_one_new_lesson_keeps_the_earlier_and_never_reports_updated():
    st = Store("t2reword3")
    proj = mk_project(st.root, "ProjA")
    st.cli("import", "--repo", str(proj))
    assert len(st.records()) == 6
    again = counts(line_of(st.cli("import", "--repo", str(proj)), "ProjA"))  # twin: no change adds 0
    assert again["added"] == 0 and again["updated"] == 0 and again["duplicate"] == 6, again
    (proj / "core" / "LESSONS_LOG.md").write_text(FIXTURE.replace("verify B before the push.", "confirm B has passed before you push anything."),
                                                  encoding="utf-8", newline="\n")
    c = counts(line_of(st.cli("import", "--repo", str(proj)), "ProjA"))
    assert c["added"] == 1 and c["updated"] == 0 and c["duplicate"] == 5, c
    rules = [x["rule"] for x in st.records()]
    assert len(rules) == 7 and "verify B before the push." in rules and "confirm B has passed before you push anything." in rules, rules
    third = counts(line_of(st.cli("import", "--repo", str(proj)), "ProjA"))  # and it is stable afterwards
    assert third["added"] == 0 and third["updated"] == 0, third


def test_facts_only_edit_adds_nothing_and_updated_is_always_zero():
    st = Store("t2factsonly")
    proj = mk_project(st.root, "ProjA")
    st.cli("import", "--repo", str(proj))
    (proj / "core" / "LESSONS_LOG.md").write_text(FIXTURE.replace("something broke on a push", "facts reworded only"),
                                                  encoding="utf-8", newline="\n")
    c = counts(line_of(st.cli("import", "--repo", str(proj)), "ProjA"))
    assert c["added"] == 0 and c["updated"] == 0 and len(st.records()) == 6, c


def test_two_blocks_with_the_same_date_and_title_are_two_lessons():
    sys.path.insert(0, str(h.SRC / "harness" / "hooks"))
    import _lessons as L
    log = ("## 2026-11-14 Retro\n- Kind: FAILURE. Facts: a.\n- Rule: first retro rule about push.\n\n"
           "## 2026-11-14 Retro\n- Kind: FAILURE. Facts: b.\n- Rule: second retro rule about commit.\n")
    assert [e["block"] for e in L.parse_log(log)["entries"]] == [0, 1]
    st = Store("t2blocks")
    proj = mk_project(st.root, "ProjA", log)
    c = counts(line_of(st.cli("import", "--repo", str(proj)), "ProjA"))
    assert c["added"] == 2, c
    recs = st.records()
    assert len({x["idem"] for x in recs}) == 2, "two blocks with one date and title must have two identities"
    c2 = counts(line_of(st.cli("import", "--repo", str(proj)), "ProjA"))
    assert c2["added"] == 0 and c2["duplicate"] == 2, c2
    twin = log.replace("## 2026-11-14 Retro\n- Kind: FAILURE. Facts: b.", "## 2026-11-14 Other\n- Kind: FAILURE. Facts: b.")  # different titles: block 0 each
    assert [e["block"] for e in L.parse_log(twin)["entries"]] == [0, 0]


def test_inserting_a_same_kind_entry_before_the_first_one_keeps_everything_and_adds_it():
    st = Store("t2insertfirst")
    proj = mk_project(st.root, "ProjA", BLOCK_AX)
    st.cli("import", "--repo", str(proj))
    inserted = ("## 2026-05-01 Block\n- Kind: FAILURE. Facts: n.\n- Rule: rule N about hooks.\n"
                "- Kind: FAILURE. Facts: a.\n- Rule: rule A about push.\n- Kind: FAILURE. Facts: x.\n- Rule: rule X about commit.\n")
    (proj / "core" / "LESSONS_LOG.md").write_text(inserted, encoding="utf-8", newline="\n")
    c = counts(line_of(st.cli("import", "--repo", str(proj)), "ProjA"))
    assert c["added"] == 1 and c["duplicate"] == 2 and c["updated"] == 0, c
    assert sorted(x["rule"] for x in st.records()) == ["rule A about push.", "rule N about hooks.", "rule X about commit."]


def test_failed_quarantine_keeps_the_bad_line_in_the_store_and_logs_it():
    st = Store("t1quarfail")
    st.add("First good rule about push.")
    p = st.dir / "lessons.jsonl"
    p.write_bytes(b"this line is damaged KEEPME\n" + p.read_bytes())
    (st.dir / "corrupt").write_text("a file where the quarantine folder should be", encoding="utf-8")
    r = st.add("Second good rule about push.")
    assert first_json(r)["status"] == "added", r.all
    assert b"KEEPME" in p.read_bytes(), "bytes that could not be saved must not be dropped"
    assert any("quarantine failed" in e for e in st.errors()), st.errors()
    good = [ln for ln in p.read_bytes().splitlines() if ln.strip().startswith(b"{")]
    assert len(good) == 2, good
    ok = Store("t1quarok")  # twin: when the quarantine works the line moves out of the store
    ok.add("First good rule about push.")
    q = ok.dir / "lessons.jsonl"
    q.write_bytes(b"this line is damaged KEEPME\n" + q.read_bytes())
    ok.add("Second good rule about push.")
    assert b"KEEPME" not in q.read_bytes() and not any("quarantine failed" in e for e in ok.errors())


def test_restore_still_succeeds_when_the_baseline_marker_cannot_be_written():
    st = Store("t1baselinefail")
    for n in range(2):
        st.add(f"Baseline rule {n} about push.")
    (st.dir / "restore-baseline.json").mkdir()  # a folder where the marker file should go
    r = st.cli("restore")
    assert r.rc == 0 and "restored from" in r.out, r.all
    assert len(st.records()) == 2
    assert any("restore:baseline" in e for e in st.errors()), st.errors()
    ok = Store("t1baselineok")  # twin: no obstacle, no such error
    for n in range(2):
        ok.add(f"Baseline rule {n} about push.")
    assert ok.cli("restore").rc == 0 and not any("restore:baseline" in e for e in ok.errors())


def test_off_drive_fallback_is_logged_once_per_store_with_a_marker():
    st = Store("t1offdrive")
    env = {"HARNESS_LESSONS_DIR": str(st.dir), "HARNESS_HOOK_STATE": str(st.state)}  # moved store, no HARNESS_LESSONS_BACKUP
    for n in range(2):
        h.run([h.PY, "-I", "-s", str(CLI), "add", "--kind", "FAILURE", "--project", "ProjA", "--rule", f"Rule {n} about push.",
               "--actions", "push"], st.root, env)
    lines = [e for e in st.errors() if "off-drive backups are off" in e]
    assert len(lines) == 1 and (st.dir / "backup-offdrive-off.marker").is_file(), (lines, st.errors())
    st2 = Store("t1offdrivetwin")  # twin: an explicit backup folder logs nothing
    st2.add("Rule about push.")
    assert not (st2.dir / "backup-offdrive-off.marker").exists() and not any("off-drive" in e for e in st2.errors())


QUEUE_DRIVER = """import json, sys
sys.path.insert(0, sys.argv[1])
import _lessons as L
L.LOCK_WAIT_S = 0.2
base = {"kind": "FAILURE", "project": "ProjA", "date": "2026-05-01", "title": "Same title", "ordinal": 1, "block": 1,
        "actions": ["push"]}
rules = json.loads(sys.argv[2])
print(json.dumps(L.add_many([dict(base, rule=r) for r in rules])))
"""


def queue_via_driver(st, rules):
    drv = st.root / "queue_driver.py"
    drv.write_text(QUEUE_DRIVER, encoding="utf-8")
    hooks = str(h.SRC / "harness" / "hooks")
    return h.run([h.PY, "-I", "-s", str(drv), hooks, json.dumps(rules)], st.root, st.env)


def test_reworded_rule_with_same_idem_queued_beside_its_earlier_wording_keeps_both():
    st = Store("f1queue")
    st.add("Existing rule about hooks.")
    lock = st.dir / "lessons.jsonl.lock"
    lock.write_text("99999", encoding="utf-8")  # a fresh lock held by someone else
    try:
        r = queue_via_driver(st, ["Wording one about reviewing the push.", "Wording two about reviewing the push."])
        assert r.rc == 0 and [x["status"] for x in json.loads(r.out.splitlines()[-1])] == ["queued", "queued"], r.all
        assert len(list((st.dir / "outbox").glob("*.json"))) == 2, "a reworded rule must not overwrite its earlier wording"
    finally:
        lock.unlink()
    assert st.add("Third rule about commit.").rc == 0  # the next writer drains the outbox
    rules = sorted(x["rule"] for x in st.records())
    assert rules == ["Existing rule about hooks.", "Third rule about commit.", "Wording one about reviewing the push.",
                     "Wording two about reviewing the push."], rules
    assert not list((st.dir / "outbox").glob("*.json"))
    assert st.add("Third rule about commit.").rc == 0  # a second drain adds nothing
    again = queue_via_driver(st, ["Wording one about reviewing the push.", "Wording two about reviewing the push."])
    assert [x["status"] for x in json.loads(again.out.splitlines()[-1])] == ["duplicate", "duplicate"], again.all
    assert len(st.records()) == 4


def test_the_same_entry_queued_twice_leaves_one_outbox_file_and_one_record():  # twin of the reworded-rule test
    st = Store("f1queuetwin")
    st.add("Existing rule about hooks.")
    lock = st.dir / "lessons.jsonl.lock"
    lock.write_text("99999", encoding="utf-8")
    try:
        for _ in range(2):
            r = queue_via_driver(st, ["Wording one about reviewing the push."])
            assert [x["status"] for x in json.loads(r.out.splitlines()[-1])] == ["queued"], r.all
        assert len(list((st.dir / "outbox").glob("*.json"))) == 1
    finally:
        lock.unlink()
    assert st.add("Third rule about commit.").rc == 0
    assert sorted(x["rule"] for x in st.records()) == ["Existing rule about hooks.", "Third rule about commit.",
                                                      "Wording one about reviewing the push."]


def test_restore_refuses_when_the_current_store_cannot_be_preserved_and_leaves_it_unchanged():
    st = Store("f2keepfail")
    for n in range(2):
        st.add(f"Keep rule {n} about push.")
    assert n_backups(st) >= 1
    p = st.dir / "lessons.jsonl"
    before = p.read_bytes()
    (st.dir / "corrupt").write_text("a file where the keep folder should be", encoding="utf-8")
    r = st.cli("restore")
    assert r.rc == 1 and "restore failed" in r.out and "could not be preserved" in r.out, r.all
    assert p.read_bytes() == before
    assert any("restore:keep" in e for e in st.errors()), st.errors()


def test_restore_with_a_writable_corrupt_folder_succeeds_and_keeps_a_replaced_copy():  # twin
    st = Store("f2keepok")
    for n in range(2):
        st.add(f"Keep rule {n} about push.")
    before = (st.dir / "lessons.jsonl").read_bytes()
    r = st.cli("restore")
    assert r.rc == 0 and "restored from" in r.out, r.all
    copies = list((st.dir / "corrupt").glob("replaced-by-restore-*.jsonl"))
    assert len(copies) == 1 and copies[0].read_bytes() == before


if __name__ == "__main__":
    sys.exit(h.run_all(globals()))
