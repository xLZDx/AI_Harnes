"""S20-T3 tests: tools/githooks/post-commit writes the lessons a commit added to the global store (real temp git repos).
Contract (D:\\tmp\\s20_criteria.md, design revision section): a commit that adds a FAILURE entry to core/LESSONS_LOG.md gives one
global entry with the project name and the real commit sha; a commit with no new entry, or an amend, adds nothing; a missing lessons
tool never fails the commit and writes one hook error line; works in a project other than Harness_v2 and from a worktree; excluded
projects (VMC_*) add nothing. The hook is installed either as .git/hooks/post-commit, through core.hooksPath, or by the real
installer (build.py --install-project).
Every test points HARNESS_LESSONS_DIR, HARNESS_LESSONS_BACKUP, HARNESS_HOOK_STATE and HARNESS_HOOK_CONFIG at temp folders.
Run: C:\\Python314\\python.exe -I -s tests\\test_s20_post_commit.py"""
import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path

_s = importlib.util.spec_from_file_location("hv2", Path(__file__).resolve().parent / "hv2.py")
h = importlib.util.module_from_spec(_s)
sys.modules["hv2"] = h
_s.loader.exec_module(h)

HOOK_SRC = h.SRC / "tools" / "githooks" / "post-commit"
MODULES = ("_lessons.py", "_agentio.py", "secret_policy.py")
LOG = "core/LESSONS_LOG.md"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def entry(rule, kind="FAILURE", title="Some mistake"):
    return f"## 2026-04-01 {title}\n- Kind: {kind}. Facts: it went wrong.\n- Rule: {rule}\n"


class World:
    """One temp store + env; repos are created on demand."""

    def __init__(self, name):
        self.root = h.fresh(name)
        self.store = self.root / "store"
        self.env = {"HARNESS_LESSONS_DIR": str(self.store), "HARNESS_LESSONS_BACKUP": str(self.root / "backup"),
                    "HARNESS_HOOK_STATE": str(self.root / "state"), "HARNESS_HOOK_CONFIG": str(self.root / "no-config.json"),
                    "HARNESS_PYTHON": h.PY, "HARNESS_LESSONS_TOOL": (h.SRC / "tools" / "lessons.py").as_posix()}

    def repo(self, name="ProjA", hook="git-hooks-dir", stub_ledger=True):
        p = self.root / name
        p.mkdir(parents=True)
        assert h.git(p, "init", "-q", "-b", "main").rc == 0
        for k, v in (("user.name", "t"), ("user.email", "t@t"), ("core.autocrlf", "false")):
            h.git(p, "config", k, v)
        if stub_ledger:  # a ledger tool that succeeds, so the only hook errors seen come from the lessons part
            (p / ".harness-tools").mkdir()
            (p / ".harness-tools" / "ledger.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        if hook == "git-hooks-dir":
            dst = p / ".git" / "hooks" / "post-commit"
            shutil.copyfile(HOOK_SRC, dst)
        elif hook == "hooks-path":
            d = self.root / (name + "-hooks")
            d.mkdir()
            shutil.copyfile(HOOK_SRC, d / "post-commit")
            h.git(p, "config", "core.hooksPath", d.as_posix())
        return p

    def commit(self, repo, rel, content, msg="change", env=None):
        e = dict(self.env)
        e.update(env or {})
        return h.commit(repo, rel, content, msg, env=e)

    def records(self):
        f = self.store / "lessons.jsonl"
        return [json.loads(x) for x in f.read_text(encoding="utf-8").split("\n") if x.strip()] if f.exists() else []

    @staticmethod
    def hook_errors(repo):
        f = repo / ".harness" / "hook-error.log"
        return [x for x in f.read_text(encoding="utf-8").split("\n") if x.strip()] if f.exists() else []


def seed_repo(w, repo):
    w.commit(repo, "README.md", "hello\n", "init")


def test_commit_adding_failure_gives_one_entry_with_project_and_real_sha():
    w = World("t3basic")
    repo = w.repo("ProjA")
    seed_repo(w, repo)
    assert w.records() == []
    sha = w.commit(repo, LOG, "# Lessons log\n\n" + entry("Run the gate before every push."), "add lesson")
    recs = w.records()
    assert len(recs) == 1, recs
    r = recs[0]
    assert r["project"] == "ProjA" and r["kind"] == "FAILURE" and r["rule"] == "Run the gate before every push.", r
    assert r["source"]["sha"] == sha and SHA_RE.match(sha), (r["source"], sha)
    assert w.hook_errors(repo) == []


def test_commit_without_new_entry_adds_nothing_and_second_entry_adds_exactly_one():
    w = World("t3nochange")
    repo = w.repo("ProjA")
    seed_repo(w, repo)
    w.commit(repo, LOG, "# Lessons log\n\n" + entry("First rule about pushes."), "lesson one")
    assert len(w.records()) == 1
    w.commit(repo, "src/app.py", "x = 1\n", "unrelated change")
    assert len(w.records()) == 1, "an unrelated commit must add nothing"
    w.commit(repo, LOG, "# Lessons log\n\n" + entry("First rule about pushes.") + entry("Second rule about commits.", title="Other"),
             "lesson two")
    recs = w.records()
    assert [x["rule"] for x in recs] == ["First rule about pushes.", "Second rule about commits."], recs  # old entry not re-added
    w.commit(repo, LOG, "# Lessons log\n\n" + entry("First rule about pushes.") + entry("Second rule about commits.", title="Other") + "\n",
             "whitespace only")
    assert len(w.records()) == 2


def test_amend_adds_nothing():
    w = World("t3amend")
    repo = w.repo("ProjA")
    seed_repo(w, repo)
    sha = w.commit(repo, LOG, "# Lessons log\n\n" + entry("Rule that must be stored once."), "lesson")
    assert len(w.records()) == 1
    e = dict(w.env)
    r = h.git(repo, "commit", "--amend", "-q", "-m", "lesson (reworded)", env=e)
    assert r.rc == 0, r.all
    assert h.head(repo) != sha, "amend did not change the sha"
    assert len(w.records()) == 1, w.records()
    # amend of a commit that has nothing to do with the log
    w.commit(repo, "a.txt", "a\n", "plain")
    assert h.git(repo, "commit", "--amend", "-q", "-m", "plain 2", env=e).rc == 0
    assert len(w.records()) == 1


def test_amend_that_adds_a_new_entry_adds_that_entry_only():  # twin: the amend path is not simply dead
    w = World("t3amendnew")
    repo = w.repo("ProjA")
    seed_repo(w, repo)
    w.commit(repo, LOG, "# Lessons log\n\n" + entry("Rule one stored first."), "lesson")
    (repo / LOG).write_text("# Lessons log\n\n" + entry("Rule one stored first.") + entry("Rule two added by amend.", title="Two"),
                            encoding="utf-8", newline="\n")
    h.git(repo, "add", LOG)
    assert h.git(repo, "commit", "--amend", "-q", "--no-edit", env=w.env).rc == 0
    assert sorted(x["rule"] for x in w.records()) == ["Rule one stored first.", "Rule two added by amend."]


def test_entry_without_rule_adds_nothing_and_does_not_fail():
    w = World("t3norule")
    repo = w.repo("ProjA")
    seed_repo(w, repo)
    w.commit(repo, LOG, "## 2026-04-02 No rule\n- Kind: FAILURE. Facts: forgot the rule line.\n", "no rule")
    assert w.records() == []
    errs = w.hook_errors(repo)  # design of the hook: a skipped entry leaves exactly one visible line
    assert len(errs) == 1 and "skipped-no-rule=1" in errs[0], errs
    w.commit(repo, LOG, "## 2026-04-02 No rule\n- Kind: FAILURE. Facts: forgot the rule line.\n" + entry("Now a rule exists."), "with rule")
    assert [x["rule"] for x in w.records()] == ["Now a rule exists."]
    assert len(w.hook_errors(repo)) == 1, "the old no-rule entry must not be reported again"


def test_missing_tool_never_fails_the_commit_and_writes_one_error_line():
    w = World("t3missing")
    repo = w.repo("ProjA")
    seed_repo(w, repo)
    env = {"HARNESS_LESSONS_TOOL": ""}  # no env var, no config file, no .harness-tools copy, no tools/lessons.py in the repo
    before = h.head(repo)
    sha = w.commit(repo, LOG, entry("Rule that cannot be stored now."), "lesson", env=env)
    assert sha != before and h.head(repo) == sha, "the commit must still land"
    assert w.records() == []
    errs = w.hook_errors(repo)
    assert len(errs) == 1 and "lessons tool not found" in errs[0], errs


def test_tool_path_to_a_file_that_does_not_exist_is_treated_as_missing():
    w = World("t3badpath")
    repo = w.repo("ProjA")
    seed_repo(w, repo)
    sha = w.commit(repo, LOG, entry("Rule behind a bad path."), "lesson",
                   env={"HARNESS_LESSONS_TOOL": (w.root / "nope" / "lessons.py").as_posix()})
    assert h.head(repo) == sha and w.records() == []
    errs = w.hook_errors(repo)
    assert len(errs) == 1 and "lessons tool not found" in errs[0], errs


def test_crashing_tool_never_fails_the_commit_and_writes_one_error_line():
    w = World("t3crash")
    repo = w.repo("ProjA")
    seed_repo(w, repo)
    bad = w.root / "bad_lessons.py"
    bad.write_text("import sys\nprint('boom on purpose')\nsys.exit(3)\n", encoding="utf-8")
    sha = w.commit(repo, LOG, entry("Rule for a crashing tool."), "lesson", env={"HARNESS_LESSONS_TOOL": bad.as_posix()})
    assert h.head(repo) == sha
    errs = w.hook_errors(repo)
    assert len(errs) == 1 and "lessons.py import exit 3" in errs[0] and "boom on purpose" in errs[0], errs
    # passing twin: the real tool leaves no error line
    repo2 = w.repo("ProjB")
    seed_repo(w, repo2)
    w.commit(repo2, LOG, entry("Rule for a working tool."), "lesson")
    assert w.hook_errors(repo2) == [] and len(w.records()) == 1


def test_tool_found_through_machine_config_key():
    w = World("t3config")
    repo = w.repo("ProjA")
    seed_repo(w, repo)
    cfg = w.root / "cfg.json"
    cfg.write_text(json.dumps({"HARNESS_LESSONS_TOOL": (h.SRC / "tools" / "lessons.py").as_posix()}), encoding="utf-8")
    w.commit(repo, LOG, entry("Rule found through the config key."), "lesson",
             env={"HARNESS_LESSONS_TOOL": "", "HARNESS_HOOK_CONFIG": str(cfg)})
    assert [x["rule"] for x in w.records()] == ["Rule found through the config key."]
    assert w.hook_errors(repo) == []


def test_tool_found_in_installer_copy_under_dot_harness_tools():
    w = World("t3copy")
    repo = w.repo("ProjA")
    seed_repo(w, repo)
    tools = repo / ".harness-tools"
    shutil.copyfile(h.SRC / "tools" / "lessons.py", tools / "lessons.py")
    for m in MODULES:
        shutil.copyfile(h.SRC / "harness" / "hooks" / m, tools / m)
    w.commit(repo, LOG, entry("Rule found through the installer copy."), "lesson", env={"HARNESS_LESSONS_TOOL": ""})
    assert [x["rule"] for x in w.records()] == ["Rule found through the installer copy."]
    assert w.hook_errors(repo) == []
    # the copy without its modules cannot work: the commit still lands and one error line says so
    for m in MODULES:
        (tools / m).unlink()
    sha = w.commit(repo, LOG, entry("Rule found through the installer copy.") + entry("Another one for the broken copy.", title="B"),
                   "lesson 2", env={"HARNESS_LESSONS_TOOL": ""})
    assert h.head(repo) == sha and len(w.hook_errors(repo)) == 1 and len(w.records()) == 1


def test_tool_found_in_tools_folder_of_the_project_itself():
    w = World("t3tools")
    repo = w.repo("ProjA")
    seed_repo(w, repo)
    (repo / "tools").mkdir()
    (repo / "harness" / "hooks").mkdir(parents=True)
    shutil.copyfile(h.SRC / "tools" / "lessons.py", repo / "tools" / "lessons.py")
    for m in MODULES:
        shutil.copyfile(h.SRC / "harness" / "hooks" / m, repo / "harness" / "hooks" / m)
    w.commit(repo, LOG, entry("Rule found in the project tools folder."), "lesson", env={"HARNESS_LESSONS_TOOL": ""})
    assert [x["rule"] for x in w.records()] == ["Rule found in the project tools folder."]


def test_hook_through_core_hookspath_works_too():
    w = World("t3hookspath")
    repo = w.repo("ProjA", hook="hooks-path")
    seed_repo(w, repo)
    sha = w.commit(repo, LOG, entry("Rule through hooks path."), "lesson")
    recs = w.records()
    assert len(recs) == 1 and recs[0]["source"]["sha"] == sha and recs[0]["project"] == "ProjA"


def test_worktree_commit_records_main_project_name_and_worktree_sha():
    w = World("t3worktree")
    repo = w.repo("ProjA")
    seed_repo(w, repo)
    wt = w.root / "wt-feature"
    r = h.git(repo, "worktree", "add", "-q", "-b", "feature", str(wt))
    assert r.rc == 0, r.all
    sha = w.commit(wt, LOG, entry("Rule from a worktree."), "lesson in worktree")
    recs = w.records()
    assert len(recs) == 1, recs
    assert recs[0]["project"] == "ProjA", "the project is the main checkout folder, not the worktree folder: " + recs[0]["project"]
    assert recs[0]["source"]["sha"] == sha
    assert w.hook_errors(repo) == []


def test_project_other_than_harness_v2_with_installed_tools_only():
    w = World("t3other")
    repo = w.repo("Some_Other_Project")
    seed_repo(w, repo)
    w.commit(repo, LOG, entry("Rule from another project."), "lesson")
    assert [x["project"] for x in w.records()] == ["Some_Other_Project"]


def test_excluded_project_adds_nothing_and_does_not_fail():
    for name in ("VMC_x", "Virtual_marketing_company"):
        w = World("t3excl")
        repo = w.repo(name)
        seed_repo(w, repo)
        sha = w.commit(repo, LOG, entry("Rule from an excluded project."), "lesson")
        assert h.head(repo) == sha and w.records() == [], (name, w.records())
    w = World("t3exclpass")  # passing twin: the same commit in a normal project is stored
    repo = w.repo("ProjA")
    seed_repo(w, repo)
    w.commit(repo, LOG, entry("Rule from an excluded project."), "lesson")
    assert len(w.records()) == 1


def test_excluded_project_writes_no_error_line():
    """An intended exclusion is not a fault: the hook must not log 'needs attention' for it."""
    w = World("t3exclquiet")
    repo = w.repo("VMC_x")
    seed_repo(w, repo)
    w.commit(repo, LOG, entry("Rule from an excluded project."), "lesson")
    assert w.hook_errors(repo) == [], w.hook_errors(repo)


def test_secret_in_new_entry_is_refused_but_commit_lands():
    w = World("t3secret")
    repo = w.repo("ProjA")
    seed_repo(w, repo)
    key = "AKIA" + "Q7XJ4N2MPLR8TV3W"
    sha = w.commit(repo, LOG, entry(f"Rotate {key} after use."), "lesson")
    assert h.head(repo) == sha and w.records() == []
    errs = w.hook_errors(repo)
    assert len(errs) == 1 and "skipped-secret=1" in errs[0] and key not in errs[0], errs
    store_text = "".join(p.read_text(encoding="utf-8", errors="replace") for p in w.store.rglob("*") if p.is_file()) if w.store.exists() else ""
    assert key not in store_text


def test_first_commit_of_a_repo_that_already_contains_the_log():
    w = World("t3root")
    repo = w.repo("ProjA")
    sha = w.commit(repo, LOG, entry("Rule in the very first commit."), "first")
    recs = w.records()
    assert len(recs) == 1 and recs[0]["source"]["sha"] == sha, "a root commit that adds a FAILURE entry must reach the store: " + str(recs)


def test_real_installer_hooks_write_the_lesson():
    w = World("t3installer")
    repo = w.repo("ProjA", hook="none", stub_ledger=False)
    seed_repo(w, repo)
    r = h.run([h.PY, "-I", "-s", str(h.SRC / "tools" / "build.py"), "--install-project", str(repo)], repo, w.env)
    assert r.rc == 0, r.all
    assert (repo / ".harness-tools" / "lessons.py").is_file() and (repo / ".harness-tools" / "_lessons.py").is_file()
    env = dict(w.env)
    env["HARNESS_LESSONS_TOOL"] = ""  # force the lookup through the installed copy
    sha = w.commit(repo, LOG, entry("Rule through the real installer."), "lesson\n\nTask: X1", env=env)
    recs = w.records()
    assert len(recs) == 1 and recs[0]["source"]["sha"] == sha and recs[0]["project"] == "ProjA", recs
    errs = [x for x in w.hook_errors(repo) if "lessons" in x]
    assert errs == [], errs


def test_changed_log_with_a_block_the_parser_cannot_read_leaves_one_attention_line():
    w = World("t3unparsed")
    repo = w.repo("ProjA")
    seed_repo(w, repo)
    sha = w.commit(repo, LOG, "## 2026-04-03 Odd block\nJust prose with no Kind marker and no rule.\n", "odd")
    assert h.head(repo) == sha and w.records() == []
    errs = w.hook_errors(repo)
    assert len(errs) == 1 and "unparsed=1" in errs[0], errs
    w2 = World("t3unparsedtwin")  # twin: a readable block leaves no line
    r2 = w2.repo("ProjA")
    seed_repo(w2, r2)
    w2.commit(r2, LOG, entry("Readable rule."), "ok")
    assert w2.hook_errors(r2) == [] and len(w2.records()) == 1


def test_reworded_rule_in_a_later_commit_adds_a_new_lesson_and_keeps_the_earlier():
    w = World("t3reword")
    repo = w.repo("ProjA")
    seed_repo(w, repo)
    w.commit(repo, LOG, entry("Check the gate before pushing."), "lesson")
    w.commit(repo, LOG, entry("Check the gate and the ledger before pushing."), "reword")
    recs = w.records()
    print(f"INFO reworded rule committed again: {len(recs)} record(s): {[r['rule'] for r in recs]}")
    assert sorted(r["rule"] for r in recs) == ["Check the gate and the ledger before pushing.", "Check the gate before pushing."], recs  # reword = new lesson, earlier kept
    assert w.hook_errors(repo) == []
    twin = World("t3rewordtwin")  # twin: an unchanged re-commit adds nothing
    r2 = twin.repo("ProjA")
    seed_repo(twin, r2)
    twin.commit(r2, LOG, entry("Check the gate before pushing."), "lesson")
    twin.commit(r2, "x.txt", "x\n", "other")
    assert len(twin.records()) == 1


def test_post_commit_import_and_bulk_import_of_the_same_entry_are_one_record():
    w = World("t3bulk")
    repo = w.repo("ProjA")
    seed_repo(w, repo)
    sha = w.commit(repo, LOG, entry("Rule seen by both writers."), "lesson")
    assert len(w.records()) == 1
    r = h.run([h.PY, "-I", "-s", str(h.SRC / "tools" / "lessons.py"), "import", "--repo", str(repo)], repo, w.env)
    assert r.rc == 0, r.all
    recs = w.records()
    assert len(recs) == 1 and sha in recs[0].get("shas", []), recs
    assert "added=0" in r.out and "duplicate=1" in r.out, r.out
    w2 = World("t3bulktwin")  # twin: a different entry in the log is added by the bulk import
    r2 = w2.repo("ProjA")
    seed_repo(w2, r2)
    w2.commit(r2, LOG, entry("Rule seen by both writers."), "lesson")
    (r2 / LOG).write_text(entry("Rule seen by both writers.") + entry("A second rule only the bulk import sees.", title="Other"),
                          encoding="utf-8", newline="\n")
    out = h.run([h.PY, "-I", "-s", str(h.SRC / "tools" / "lessons.py"), "import", "--repo", str(r2)], r2, w2.env)
    assert "added=1" in out.out and len(w2.records()) == 2, out.out


if __name__ == "__main__":
    sys.exit(h.run_all(globals()))
