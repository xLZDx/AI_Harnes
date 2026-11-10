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


if __name__ == "__main__":
    sys.exit(h.run_all(globals()))
