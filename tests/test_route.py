"""Criterion 3: deterministic reviewer router (contract at 6a6de90)."""
import importlib.util
import json
import sys
from pathlib import Path

_s = importlib.util.spec_from_file_location("hv2", Path(__file__).resolve().parent / "hv2.py")
h = importlib.util.module_from_spec(_s)
sys.modules["hv2"] = h
_s.loader.exec_module(h)

SPECIALISTS = {"security-reviewer", "database-reviewer", "reliability-reviewer"}


def _ok(repo, files, body="+x = 1", *args):
    r, res = h.route(repo, files, body, *args)
    assert r.rc == 0 and res is not None, r.all
    return res


def _profile(repo, data):
    (repo / ".harness").mkdir(exist_ok=True)
    (repo / ".harness" / "profile.json").write_text(json.dumps(data), encoding="utf-8")


def test_docs_only_is_trivial_with_zero_reviewers():
    r = h.mk_repo("rt_docs")
    res = _ok(r, ["README.md", "docs/guide.txt", "core/DECISION_LOG.md"])
    assert res["risk"] == "trivial" and res["reviewers"] == [], res


def test_docs_only_with_risky_looking_words_stays_trivial():
    r = h.mk_repo("rt_docs2")
    res = _ok(r, ["docs/auth.md"], "+we use a transaction and a lock here")
    assert res["risk"] == "trivial" and res["reviewers"] == [], res


def test_one_small_code_file_is_local_code_reviewer_only():
    r = h.mk_repo("rt_local")
    res = _ok(r, ["src/app.py"])
    assert res["risk"] == "local" and h.agents(res) == ["code-reviewer"], res


def test_more_than_three_code_files_is_multi_module_with_language_reviewer():
    r = h.mk_repo("rt_multi4")
    res = _ok(r, [f"src/m{i}.py" for i in range(4)])
    assert res["risk"] == "multi-module", res
    assert h.agents(res) == ["code-reviewer", "silent-failure-hunter", "python-reviewer"], res
    assert _ok(r, [f"src/m{i}.py" for i in range(3)])["risk"] == "local", "3 files in one dir stays local"


def test_two_top_level_dirs_is_multi_module_dart_gets_flutter_reviewer():
    r = h.mk_repo("rt_multi2")
    res = _ok(r, ["lib/a.dart", "test/b.dart"])
    assert res["risk"] == "multi-module", res
    assert h.agents(res) == ["code-reviewer", "silent-failure-hunter", "flutter-reviewer"], res
    py = _ok(r, ["svc/a.py", "web/b.txt2"])
    assert py["risk"] == "multi-module" and "python-reviewer" in h.agents(py), py


def test_multi_module_without_known_language_has_two_core_reviewers():
    r = h.mk_repo("rt_multi_nolang")
    res = _ok(r, ["a/x.js", "b/y.js"])
    assert res["risk"] == "multi-module" and h.agents(res) == ["code-reviewer", "silent-failure-hunter"], res


def test_root_level_files_count_as_one_area():
    r = h.mk_repo("rt_root")
    res = _ok(r, ["a.py", "b.py", "c.py"])
    assert res["risk"] == "local", res
    assert _ok(r, ["a.py", "src/b.py"])["risk"] == "multi-module", "root + one dir are two areas"


def test_risky_migration_path_exactly_one_specialist_and_max_four():
    r = h.mk_repo("rt_risky_mig")
    res = _ok(r, ["db/migrations/001_add.sql", "db/migrations/002_x.sql", "svc/run.py"])
    assert res["risk"] == "risky", res
    ag = h.agents(res)
    assert len(ag) <= 4 and len([a for a in ag if a in SPECIALISTS]) == 1, ag
    assert "database-reviewer" in ag and "security-reviewer" not in ag, ag


def test_risky_diff_keywords_lock_retry_transaction():
    for kw in ("lock", "retry", "transaction"):
        r = h.mk_repo(f"rt_kw_{kw}")
        res = _ok(r, ["svc/run.py"], f"+with {kw}:\n+    pass")
        assert res["risk"] == "risky", (kw, res)
        assert h.agents(res)[:2] == ["code-reviewer", "silent-failure-hunter"], res
        assert len(h.agents(res)) <= 4


def test_keywords_match_case_insensitively():
    r = h.mk_repo("rt_case")
    for body in ("+RETRY = 3", "+With LOCK:", "+x = Transaction()"):
        assert _ok(r, ["svc/run.py"], body)["risk"] == "risky", body


def test_risky_paths_are_anchored():
    r = h.mk_repo("rt_anchor")
    for ok_file in ("src/author.py", "src/border.py", "src/leader.py"):
        assert _ok(r, [ok_file])["risk"] == "local", ok_file
    for risky in ("auth/login.py", "src/auth/login.py", "src/auth_utils.py", "src/orders/x.py", "app/payments.py"):
        assert _ok(r, [risky])["risk"] == "risky", risky


def test_risky_many_triggers_never_more_than_four_and_one_specialist():
    r = h.mk_repo("rt_cap")
    body = "+retry lock transaction CREATE TABLE password jwt timeout"
    res = _ok(r, ["db/migrations/a.sql", "auth/login.py", "worker/jobs/q.py", "x/y.py"], body)
    ag = h.agents(res)
    assert res["risk"] == "risky" and len(ag) <= 4, ag
    assert len([a for a in ag if a in SPECIALISTS]) == 1, ag


def test_never_more_than_four_without_request():
    r = h.mk_repo("rt_max4")
    for files, body in ((["a/auth.py", "b/c.dart", "d/e.py", "f/g.py", "h/i.py"], "+retry"),
                        ([f"p{i}/m.py" for i in range(8)], "+x = 1")):
        assert len(h.agents(_ok(r, files, body))) <= 4


def test_security_reviewer_not_automatic_globally():
    r = h.mk_repo("rt_sec")
    for files, body in ((["src/auth/login.py"], "+password = 1\n+jwt = make()"), (["db/migrations/001.sql"], "+CREATE TABLE t (id int);"),
                        (["src/app.py"], "+jwt = 1"), (["svc/upload/x.py"], "+retry")):
        res = _ok(r, files, body)
        assert "security-reviewer" not in h.agents(res), (files, res)
    assert _ok(r, ["src/auth/login.py"])["risk"] == "risky"


def test_security_reviewer_via_profile_trigger_or_request():
    r = h.mk_repo("rt_sec_profile")
    _profile(r, {"security_triggers": {"paths": ["(^|/)upload"]}})
    res = _ok(r, ["svc/upload/x.py"], "+retry")
    assert res["risk"] == "risky" and "security-reviewer" in h.agents(res), res
    assert len([a for a in h.agents(res) if a in SPECIALISTS]) == 1, "one specialist per task"
    other = _ok(r, ["svc/other/x.py"], "+retry")
    assert "security-reviewer" not in h.agents(other), other
    plain = h.mk_repo("rt_sec_req")
    req = _ok(plain, ["src/app.py"], "+x = 1", "--request", "security-reviewer")
    assert h.agents(req) == ["code-reviewer", "security-reviewer"], req


PD = {"security_triggers": {"paths": ["(^|/)src/personal_data/"], "diff_keywords": ["\\bssn\\b"]}}


def test_security_trigger_adds_security_reviewer_on_a_local_change():
    r = h.mk_repo("rt_sec_local")
    _profile(r, PD)
    res = _ok(r, ["src/personal_data/x.py"])
    assert res["risk"] == "local" and h.agents(res) == ["code-reviewer", "security-reviewer"], res
    other = _ok(r, ["src/other/x.py"])
    assert other["risk"] == "local" and h.agents(other) == ["code-reviewer"], other


def test_security_trigger_by_diff_keyword_only():
    r = h.mk_repo("rt_sec_kw")
    _profile(r, PD)
    res = _ok(r, ["src/app.py"], "+ssn = load()")
    assert res["risk"] == "local" and h.agents(res) == ["code-reviewer", "security-reviewer"], res
    assert h.agents(_ok(r, ["src/app.py"], "+classnames = 1")) == ["code-reviewer"], "word-bounded keyword"


def test_security_trigger_on_docs_only_change_gets_code_and_security():
    r = h.mk_repo("rt_sec_docs")
    _profile(r, PD)
    res = _ok(r, ["src/personal_data/README.md"])
    assert res["risk"] == "trivial" and h.agents(res) == ["code-reviewer", "security-reviewer"], res
    assert _ok(r, ["docs/other.md"])["reviewers"] == []


def test_security_replaces_last_specialist_on_risky_with_four_picks():
    r = h.mk_repo("rt_sec_risky4")
    _profile(r, {"security_triggers": {"paths": ["(^|/)svc/personal/"]}})
    files = ["db/migrations/001.sql", "svc/personal/x.py"]
    res = _ok(r, files)
    assert res["risk"] == "risky"
    assert h.agents(res) == ["code-reviewer", "silent-failure-hunter", "python-reviewer", "security-reviewer"], res
    plain = h.mk_repo("rt_sec_risky4_none")
    assert h.agents(_ok(plain, files)) == ["code-reviewer", "silent-failure-hunter", "python-reviewer", "database-reviewer"]


def test_nothing_adds_security_reviewer_without_security_triggers():
    r = h.mk_repo("rt_sec_absent")
    for files, body in ((["src/personal_data/x.py"], "+ssn = 1"), (["src/personal_data/README.md"], "+x"),
                        (["db/migrations/001.sql", "svc/personal/x.py"], "+x")):
        assert "security-reviewer" not in h.agents(_ok(r, files, body)), files


def test_task_without_diff_writes_diff_artifact_and_reports_path():
    import os
    r = h.mk_repo("rt_artifact")
    h.setup_tasks(r, 1)
    h.commit(r, "db/migrations/001.sql", "CREATE TABLE t (id int);\n", "one", "Task: S1-T1")
    h.ledger(r, "commit-hook")
    h.commit(r, "svc/run.py", "x = 1\n", "two", "Task: S1-T1")
    h.ledger(r, "commit-hook")
    h.commit(r, "unrelated.py", "u = 1\n", "not in task")
    shas = h.task(r, "S1-T1")["commits"]
    assert len(shas) == 2
    res = h.tool(r, "route.py", "--task", "S1-T1")
    assert res.rc == 0, res.all
    out = json.loads(res.out)
    p = Path(out["diff_path"])
    assert p.is_absolute() and p.is_file() and os.path.samefile(p, r / ".harness" / "diff" / "S1-T1.diff"), out["diff_path"]
    text = p.read_text(encoding="utf-8")
    shows = [h.git(r, "show", "--format=", "--no-color", s).out.strip() for s in shas]
    pos = 0
    for s in shows:
        i = text.find(s, pos)
        assert i >= 0, "task diff must contain each commit's `git show --format= --no-color` output in order"
        pos = i + len(s)
    assert text.strip() == "\n".join(shows) or text.strip().replace("\n\n", "\n") == "\n".join(shows).replace("\n\n", "\n")
    assert "unrelated.py" not in text
    assert h.task(r, "S1-T1")["routing"]["diff_path"] == out["diff_path"]


def test_single_commit_task_diff_equals_git_show_exactly():
    r = h.mk_repo("rt_artifact1")
    h.setup_tasks(r, 1)
    sha = h.commit(r, "src/a.py", "x = 1\n", "one", "Task: S1-T1")
    h.ledger(r, "commit-hook")
    h.tool(r, "route.py", "--task", "S1-T1")
    text = (r / ".harness" / "diff" / "S1-T1.diff").read_text(encoding="utf-8")
    assert text.strip() == h.git(r, "show", "--format=", "--no-color", sha).out.strip()


def test_diff_flag_reports_absolute_path_of_given_file():
    import os
    r = h.mk_repo("rt_artifact_diff")
    res, out = h.route(r, ["src/a.py"])
    assert res.rc == 0
    assert Path(out["diff_path"]).is_absolute() and os.path.samefile(out["diff_path"], r / "t.diff"), out["diff_path"]
    assert not (r / ".harness" / "diff").exists(), "--diff must not write an artifact"
    sub = r / "sub"
    sub.mkdir()
    (sub / "rel.diff").write_text(h.diff_text(["src/a.py"]), encoding="utf-8")
    rel = h.tool(sub, "route.py", "--diff", "rel.diff")
    assert rel.rc == 0 and os.path.samefile(json.loads(rel.out)["diff_path"], sub / "rel.diff") and Path(json.loads(rel.out)["diff_path"]).is_absolute()


def test_explicit_request_is_never_cut_by_the_cap():
    r = h.mk_repo("rt_req_full")
    res = _ok(r, ["db/migrations/001.sql", "svc/run.py"], "+x = 1", "--request", "security-reviewer")
    ag = h.agents(res)
    assert ag == ["code-reviewer", "silent-failure-hunter", "python-reviewer", "database-reviewer", "security-reviewer"], ag
    again = _ok(r, ["db/migrations/001.sql", "svc/run.py"], "+x = 1", "--request", "database-reviewer")
    assert h.agents(again).count("database-reviewer") == 1 and len(h.agents(again)) == 4


def test_unknown_requested_agent_is_refused():
    r = h.mk_repo("rt_req_unknown")
    res, _ = h.route(r, ["src/app.py"], "+x = 1", "--request", "no-such-agent")
    assert res.rc != 0 and "no-such-agent" in res.all, res.all


def test_task_flag_stores_routing_in_ledger():
    r = h.mk_repo("rt_task")
    h.setup_tasks(r)
    res = _ok(r, ["db/migrations/001.sql"], "+x", "--task", "S1-T2")
    t = h.task(r, "S1-T2")
    assert t["routing"] is not None and t["routing"]["risk"] == res["risk"] == "risky", t["routing"]
    assert [x["agent"] for x in t["routing"]["reviewers"]] == h.agents(res)
    assert h.task(r, "S1-T1")["routing"] is None
    assert h.tool(r, "route.py", "--diff", str(r / "t.diff"), "--task", "S9-T9").rc != 0


def test_declared_risk_can_only_raise_the_automatic_risk():
    r = h.mk_repo("rt_declared")
    h.ledger(r, "sprint-add", "--id", "S1", "--title", "t")
    h.ledger(r, "task-add", "--title", "t", "--risk", "multi-module")
    res = _ok(r, ["src/a.py"], "+x = 1", "--task", "S1-T1")
    assert res["risk"] == "multi-module" and "silent-failure-hunter" in h.agents(res), res
    low = _ok(r, ["db/migrations/001.sql"], "+x", "--risk", "trivial")
    assert low["risk"] == "risky", "declared trivial must not lower a risky diff"
    low2 = _ok(r, ["svc/run.py"], "+with lock:", "--risk", "local")
    assert low2["risk"] == "risky"


def test_task_without_diff_builds_diff_from_task_commits():
    r = h.mk_repo("rt_taskdiff")
    h.setup_tasks(r)
    h.commit(r, "db/migrations/001.sql", "CREATE TABLE t (id int);\n", "migration", "Task: S1-T1")
    h.ledger(r, "commit-hook")
    h.commit(r, "unrelated/other.py", "x = 1\n", "not part of the task")
    res = h.tool(r, "route.py", "--task", "S1-T1")
    assert res.rc == 0, res.all
    out = json.loads(res.out)
    assert out["risk"] == "risky" and out["files"] == ["db/migrations/001.sql"], out
    assert "database-reviewer" in h.agents(out)
    assert h.task(r, "S1-T1")["routing"]["risk"] == "risky"
    nocommit = h.tool(r, "route.py", "--task", "S1-T2")
    assert nocommit.rc != 0, "a task without commits has nothing to route"


def test_empty_diff_and_diff_without_file_headers_exit_2():
    r = h.mk_repo("rt_baddiff")
    d = r / "e.diff"
    d.write_text("", encoding="utf-8")
    assert h.tool(r, "route.py", "--diff", str(d)).rc == 2
    d.write_text("hello world, this is not a diff\n+added line\n", encoding="utf-8")
    res = h.tool(r, "route.py", "--diff", str(d))
    assert res.rc == 2, res.all


def test_utf16_bom_diff_is_read_correctly():
    r = h.mk_repo("rt_utf16")
    d = r / "u.diff"
    d.write_bytes(h.diff_text(["db/migrations/001.sql"], "+-- privet").encode("utf-16"))
    assert d.read_bytes()[:2] in (b"\xff\xfe", b"\xfe\xff")
    res = h.tool(r, "route.py", "--diff", str(d))
    assert res.rc == 0, res.all
    out = json.loads(res.out)
    assert out["files"] == ["db/migrations/001.sql"] and out["risk"] == "risky", out


def test_paths_with_spaces_are_parsed():
    r = h.mk_repo("rt_spaces")
    res = _ok(r, ["src/my module/my file.py"])
    assert res["files"] == ["src/my module/my file.py"] and res["risk"] == "local", res


def test_profile_domain_specialist_chosen_before_global_on_risky():
    r = h.mk_repo("rt_profile")
    _profile(r, {"domain_specialists": [{"agent": "ledger-domain-expert", "paths": ["^db/"]}]})
    res = _ok(r, ["db/migrations/001.sql"])  # also matches the global database-reviewer
    ag = h.agents(res)
    assert res["risk"] == "risky" and "ledger-domain-expert" in ag and "database-reviewer" not in ag, ag
    other = h.mk_repo("rt_profile_none")
    ag2 = h.agents(_ok(other, ["db/migrations/001.sql"]))
    assert "database-reviewer" in ag2 and "ledger-domain-expert" not in ag2


def test_history_runs_on_repo_with_n_commits():
    r = h.mk_repo("rt_hist", with_commit=False)
    for i in range(5):
        h.commit(r, f"src/f{i}.py", f"x = {i}\n", f"c{i}")
    out = h.tool(r, "route.py", "--history", "5")
    assert out.rc == 0, out.all
    lines = [l for l in out.out.splitlines() if l.strip()]
    assert len(lines) == 6 and lines[-1].startswith("totals:"), lines
    assert json.loads(lines[-1][len("totals:"):])["code-reviewer"] == 5
    big = h.tool(r, "route.py", "--history", "50")
    assert big.rc == 0 and len([l for l in big.out.splitlines() if l.strip()]) == 6, big.all


def test_history_on_single_commit_repo():
    r = h.mk_repo("rt_hist1", with_commit=False)
    h.commit(r, "src/a.py", "x = 1\n", "only")
    out = h.tool(r, "route.py", "--history", "1")
    assert out.rc == 0 and "local" in out.out, out.all


def test_no_diff_and_no_history_is_usage_error():
    r = h.mk_repo("rt_usage")
    assert h.tool(r, "route.py").rc != 0


# ---- S2-T1: keyword triggers see only changed lines of non-doc files; project-defined agents ----
def _hunk(old, new, body, extra=""):
    return f"diff --git a/{old} b/{new}\n{extra}--- a/{old}\n+++ b/{new}\n@@ -1 +1 @@\n{body}\n"


def _raw(repo, text, *args):
    d = repo / "raw.diff"
    d.write_text(text, encoding="utf-8", newline="\n")
    r = h.tool(repo, "route.py", "--diff", str(d), *args)
    assert r.rc == 0, r.all
    return json.loads(r.out)


def test_docs_only_diff_with_security_keyword_routes_to_nobody():
    r = h.mk_repo("rt_t1_docsec")
    _profile(r, PD)
    res = _raw(r, _hunk("core/notes.md", "core/notes.md", "+the ssn field"))
    assert res["risk"] == "trivial" and res["reviewers"] == [], res
    code = _raw(r, _hunk("src/app.py", "src/app.py", "+ssn = 1"))
    assert h.agents(code) == ["code-reviewer", "security-reviewer"], "same keyword in code still fires"


def test_docs_only_diff_with_risky_keyword_is_trivial():
    r = h.mk_repo("rt_t1_docrisky")
    res = _raw(r, _hunk("core/notes.md", "core/notes.md", "-old\n+with lock and retry"))
    assert res["risk"] == "trivial" and res["reviewers"] == [], res


def test_mixed_diff_keyword_only_in_md_hunk_does_not_fire():
    r = h.mk_repo("rt_t1_mixed_md")
    text = _hunk("docs/g.md", "docs/g.md", "+use a lock") + _hunk("src/app.py", "src/app.py", "+x = 1")
    res = _raw(r, text)
    assert res["risk"] == "local" and h.agents(res) == ["code-reviewer"], res


def test_mixed_diff_keyword_in_py_hunk_fires():
    r = h.mk_repo("rt_t1_mixed_py")
    text = _hunk("docs/g.md", "docs/g.md", "+nothing") + _hunk("src/app.py", "src/app.py", "+with lock:")
    assert _raw(r, text)["risk"] == "risky"
    text2 = _hunk("src/app.py", "src/app.py", "+x = 1") + _hunk("docs/g.md", "docs/g.md", "+use a lock")
    assert _raw(r, text2)["risk"] == "local", "doc hunk after code hunk must still be excluded"


def test_specialist_and_security_diff_keywords_ignore_md_hunks():
    r = h.mk_repo("rt_t1_spec")
    _profile(r, {"domain_specialists": [{"agent": "zebra-expert", "diff_keywords": ["\\bzebra\\b"]}],
                 "security_triggers": {"diff_keywords": ["\\bssn\\b"]}})
    base = _hunk("auth/login.py", "auth/login.py", "+x = 1")
    md = _hunk("docs/z.md", "docs/z.md", "+zebra and ssn")
    res = _raw(r, base + md)
    assert res["risk"] == "risky" and "zebra-expert" not in h.agents(res) and "security-reviewer" not in h.agents(res), res
    zpy = _raw(r, base + _hunk("src/z.py", "src/z.py", "+zebra = 1"))
    assert "zebra-expert" in h.agents(zpy), zpy
    spy = _raw(r, base + _hunk("src/z.py", "src/z.py", "+ssn = 1"))
    assert "security-reviewer" in h.agents(spy), spy


def test_deleted_doc_file_lines_do_not_fire_but_deleted_code_lines_do():
    r = h.mk_repo("rt_t1_deleted")
    gone = "deleted file mode 100644\n"
    doc = f"diff --git a/docs/old.md b/docs/old.md\n{gone}--- a/docs/old.md\n+++ /dev/null\n@@ -1 +0,0 @@\n-we use a lock\n"
    code = f"diff --git a/src/old.py b/src/old.py\n{gone}--- a/src/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-with lock:\n"
    res = _raw(r, doc)
    assert res["risk"] == "trivial" and res["reviewers"] == [], res
    assert _raw(r, doc + _hunk("src/a.py", "src/a.py", "+x = 1"))["risk"] == "local"
    assert _raw(r, code)["risk"] == "risky"


def test_renamed_file_hunk_is_doc_only_if_both_paths_are_docs():
    r = h.mk_repo("rt_t1_rename")
    to_doc = _hunk("src/w.py", "docs/w.md", "+use a lock", "similarity index 50%\nrename from src/w.py\nrename to docs/w.md\n")
    to_doc += _hunk("src/a.py", "src/a.py", "+x = 1")
    assert _raw(r, to_doc)["risk"] == "risky", "py renamed into docs: doc only if BOTH paths are docs, hunk lines stay in the body"
    both_docs = _hunk("docs/old.md", "docs/new.md", "+use a lock", "rename from docs/old.md\nrename to docs/new.md\n")
    both_docs += _hunk("src/a.py", "src/a.py", "+x = 1")
    assert _raw(r, both_docs)["risk"] == "local", "md renamed to md: keyword is documentation"
    to_code = _hunk("docs/old.md", "src/new.py", "+with lock:", "similarity index 50%\nrename from docs/old.md\nrename to src/new.py\n")
    assert _raw(r, to_code)["risk"] == "risky", "md renamed to py: keyword is code"


def test_harness_sources_and_agent_instruction_files_stay_code():
    r = h.mk_repo("rt_t1_exclude")
    _profile(r, PD)
    for f in ("harness/agents/x.md", "harness/skills/s/SKILL.md", "harness/commands/c.md", "harness/rules/r.md",
              "CLAUDE.md", "AGENTS.md", "sub/AGENTS.md"):
        res = _raw(r, _hunk(f, f, "+must use a lock"))
        assert res["risk"] == "risky", (f, res)
        sec = _raw(r, _hunk(f, f, "+ssn"))
        assert "security-reviewer" in h.agents(sec), (f, sec)
    assert _raw(r, _hunk("harness/other/x.md", "harness/other/x.md", "+a lock"))["risk"] == "trivial"


def test_generated_reports_are_documentation_but_other_html_is_code():
    r = h.mk_repo("rt_t1_html")
    for f in ("reports/sprint1.html", ".harness/report.html"):
        res = _raw(r, _hunk(f, f, "+<p>with lock</p>"))
        assert res["risk"] == "trivial" and res["reviewers"] == [], (f, res)
    both = _raw(r, _hunk("reports/a.html", "reports/a.html", "+lock") + _hunk(".harness/report.html", ".harness/report.html", "+retry"))
    assert both["risk"] == "trivial", both
    web = _raw(r, _hunk("web/index.html", "web/index.html", "+<script>lock</script>"))
    assert web["risk"] == "risky", web
    assert _raw(r, _hunk("web/index.html", "web/index.html", "+<p>x</p>"))["risk"] == "local"


def test_report_html_patterns_are_anchored():
    r = h.mk_repo("rt_t1_anchor_html")
    for f in ("app/templates/reports/x.html", "src/report/x.html", "reports/sub/x.html", "src/.harness/report.html"):
        res = _raw(r, _hunk(f, f, "+<p>lock</p>"))
        assert res["risk"] == "risky", (f, res)
    for f in ("reports/x.html", ".harness/report.html"):
        res = _raw(r, _hunk(f, f, "+<p>lock</p>"))
        assert res["risk"] == "trivial" and res["reviewers"] == [], (f, res)


def test_header_with_trailing_tab_is_parsed_as_plain_name_and_is_doc():
    r = h.mk_repo("rt_t1_tab")
    text = "diff --git a/my notes.md b/my notes.md\n--- a/my notes.md\t\n+++ b/my notes.md\t\n@@ -1 +1 @@\n+use a lock\n"
    res = _raw(r, text)
    assert res["files"] == ["my notes.md"], res
    assert res["risk"] == "trivial" and res["reviewers"] == [], res
    code = "diff --git a/my app.py b/my app.py\n--- a/my app.py\t\n+++ b/my app.py\t\n@@ -1 +1 @@\n+use a lock\n"
    cres = _raw(r, code)
    assert cres["files"] == ["my app.py"] and cres["risk"] == "risky", cres


def test_is_doc_with_empty_rules_is_false():
    import importlib
    sys.path.insert(0, str(h.TOOLS))
    route_mod = importlib.import_module("route")
    assert route_mod.is_doc({}, "a.md") is False


def test_security_trigger_paths_still_fire_at_every_risk_level():
    r = h.mk_repo("rt_t1_secpaths")
    _profile(r, PD)
    for files, risk in ((["README.md", "src/personal_data/n.md"], "trivial"), (["src/personal_data/x.py"], "local"),
                        (["src/personal_data/x.py", "lib/y.py"], "multi-module"), (["src/personal_data/x.py", "db/migrations/1.sql"], "risky")):
        res = _ok(r, files)
        assert res["risk"] == risk and "security-reviewer" in h.agents(res), (files, res)


def test_parse_diff_without_rules_keeps_every_hunk():
    import importlib
    sys.path.insert(0, str(h.TOOLS))
    route_mod = importlib.import_module("route")
    text = _hunk("docs/g.md", "docs/g.md", "+docword") + _hunk("src/a.py", "src/a.py", "+codeword")
    files, body = route_mod.parse_diff(text)
    assert files == ["docs/g.md", "src/a.py"] and "docword" in body and "codeword" in body, (files, body)
    files2, body2 = route_mod.parse_diff(text, route_mod.load_rules(h.fresh("rt_t1_rules")))
    assert "docword" not in body2 and "codeword" in body2, body2


def _agent_file(repo, fname, text):
    d = repo / ".claude" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / fname).write_text(text, encoding="utf-8", newline="\n")


def test_request_accepts_agent_defined_only_in_project_claude_agents():
    r = h.mk_repo("rt_t1_projagent")
    _agent_file(r, "billing-helper.md", "---\nname: acme-billing-expert\ndescription: d\n---\nbody\n")
    res = _ok(r, ["src/app.py"], "+x = 1", "--request", "acme-billing-expert")
    assert h.agents(res) == ["code-reviewer", "acme-billing-expert"], res
    assert res["reviewers"][-1]["reason"] == "operator request", res


def test_request_accepts_agent_file_named_x_agent_md_with_frontmatter_name():
    r = h.mk_repo("rt_t1_dotagent")
    _agent_file(r, "x.agent.md", "---\nname: dotted-agent-name\n---\nbody\n")
    res = _ok(r, ["src/app.py"], "+x = 1", "--request", "dotted-agent-name")
    assert "dotted-agent-name" in h.agents(res), res


def test_project_agent_name_comes_from_frontmatter_block_only():
    r = h.mk_repo("rt_t1_fm")
    _agent_file(r, "q.md", '---\nname: "quoted-agent"\n---\nbody\n')
    _agent_file(r, "nofm.md", "no frontmatter here\nname: body-name\n")
    _agent_file(r, "late.md", "---\ndescription: d\n---\nname: late-body-name\n")
    _agent_file(r, "x.agent.md", "---\ndescription: no name here\n---\nbody\n")
    (r / ".claude" / "agents" / "bom.md").write_bytes(b"\xef\xbb\xbf---\nname: bom-agent\n---\nbody\n")
    for ok in ("quoted-agent", "bom-agent", "x.agent", "nofm", "late"):
        res = _ok(r, ["src/app.py"], "+x = 1", "--request", ok)
        assert ok in h.agents(res), (ok, res)
    for bad in ("body-name", "late-body-name", "q", "bom", "x"):
        res, _ = h.route(r, ["src/app.py"], "+x = 1", "--request", bad)
        assert res.rc != 0 and "unknown agent" in res.all, (bad, res.all)


def test_request_still_rejects_name_found_nowhere_even_with_project_agents():
    r = h.mk_repo("rt_t1_stillunknown")
    _agent_file(r, "a.md", "---\nname: real-one\n---\n")
    res, _ = h.route(r, ["src/app.py"], "+x = 1", "--request", "ghost-agent")
    assert res.rc != 0 and "unknown agent" in res.all and "ghost-agent" in res.all, res.all
    other = h.mk_repo("rt_t1_noagentdir")
    res2, _ = h.route(other, ["src/app.py"], "+x = 1", "--request", "real-one")
    assert res2.rc != 0 and "unknown agent" in res2.all, "agent from another project is not known here"


if __name__ == "__main__":
    sys.exit(h.run_all(globals()))
