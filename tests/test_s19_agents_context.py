"""S19-T5 tests (B38): harness/hooks/agents_context.py (UserPromptSubmit) and tools/report.py running_agents / sec_agents.
Written from the S19-T5 acceptance criteria, not from the code.
Hook (run as a real subprocess, HARNESS_HOOK_STATE = temp folder):
1. Output JSON: hookSpecificOutput.hookEventName == 'UserPromptSubmit', additionalContext == 'Running harness agents: <roles>'.
2. Finished entries and entries of another project are left out; an entry of the same session is listed.
3. No agents: empty stdout. Garbage stdin: exit 0 and empty stdout.
4. A '?' suffix after 10 minutes without an event; at most 10 names.
Report (in-process, environment set per test):
5. running_agents reads HARNESS_HOOK_STATE from the config file named by HARNESS_HOOK_CONFIG when the variable is unset.
6. From a linked git worktree it matches entries stored with the main checkout path.
7. sec_agents prints the 'not running' line when no agent runs.
Run: C:\\Python314\\python.exe -I -s tests/test_s19_agents_context.py"""
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

_s = importlib.util.spec_from_file_location("hv2", Path(__file__).resolve().parent / "hv2.py")
h = importlib.util.module_from_spec(_s)
sys.modules["hv2"] = h
_s.loader.exec_module(h)

HOOK = h.SRC / "harness" / "hooks" / "agents_context.py"
sys.path.insert(0, str(h.TOOLS))
_r = importlib.util.spec_from_file_location("report_under_test", h.TOOLS / "report.py")
report = importlib.util.module_from_spec(_r)
_r.loader.exec_module(report)

PREFIX = "Running harness agents: "


def _e(typ, start_ago=60, project="", sid="other", stop=None, activity_ago=None):
    e = {"type": typ, "model": "m", "effort": "e", "task": "S19-T5", "start": time.time() - start_ago, "project": project,
         "session": sid, "tool_use_id": "", "sources": ["pre"]}
    if stop:
        e["stop"] = time.time()
    if activity_ago is not None:
        e["activity"] = time.time() - activity_ago
    return e


def _seed(state, entries):
    d = state / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "active.json").write_text(json.dumps({"agents": entries}), encoding="utf-8")


def _hook(state, payload, raw=None):
    inp = raw if raw is not None else json.dumps(payload)
    return h.run([h.PY, "-I", "-s", "-X", "utf8", str(HOOK), "--harness-client=claude"], state,
                 {"HARNESS_HOOK_STATE": str(state), "HARNESS_HOOK_CONFIG": str(state / "no-config.json")}, inp)


def _payload(cwd, sid="me"):
    return {"hook_event_name": "UserPromptSubmit", "session_id": sid, "cwd": str(cwd), "prompt": "hello"}


def _ctx(r):
    assert r.rc == 0, r.all
    out = json.loads(r.out)
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit", out
    return out["hookSpecificOutput"]["additionalContext"]


def _proj(repo):
    return str(Path(repo).resolve())


# ---------------------------------------------------------------- hook: output shape and selection
def test_output_json_shape_and_line_for_a_project_agent():
    st = h.fresh("s19t5_shape")
    repo = h.mk_repo("s19t5_shape_repo")
    _seed(st, [_e("functional-test-reviewer", project=_proj(repo)), _e("code-reviewer", project=_proj(repo))])
    r = _hook(st, _payload(repo))
    assert r.rc == 0, r.all
    out = json.loads(r.out)
    assert set(out) == {"hookSpecificOutput"}, out
    assert out["hookSpecificOutput"] == {"hookEventName": "UserPromptSubmit",
                                         "additionalContext": PREFIX + "functional-test-reviewer, code-reviewer"}, out


def test_finished_entries_are_excluded():
    st = h.fresh("s19t5_fin")
    repo = h.mk_repo("s19t5_fin_repo")
    _seed(st, [_e("done-agent", project=_proj(repo), stop=True), _e("live-agent", project=_proj(repo))])
    assert _ctx(_hook(st, _payload(repo))) == PREFIX + "live-agent"


def test_only_finished_entries_means_empty_output():
    st = h.fresh("s19t5_onlyfin")
    repo = h.mk_repo("s19t5_onlyfin_repo")
    _seed(st, [_e("done-agent", project=_proj(repo), stop=True, sid="me")])
    r = _hook(st, _payload(repo))
    assert r.rc == 0 and r.out == "", r.all


def test_other_project_entries_are_excluded():
    st = h.fresh("s19t5_oth")
    mine, other = h.mk_repo("s19t5_oth_a"), h.mk_repo("s19t5_oth_b")
    _seed(st, [_e("other-agent", project=_proj(other)), _e("my-agent", project=_proj(mine))])
    assert _ctx(_hook(st, _payload(mine))) == PREFIX + "my-agent"


def test_only_other_project_means_empty_output():
    st = h.fresh("s19t5_oth2")
    mine, other = h.mk_repo("s19t5_oth2_a"), h.mk_repo("s19t5_oth2_b")
    _seed(st, [_e("other-agent", project=_proj(other))])
    r = _hook(st, _payload(mine))
    assert r.rc == 0 and r.out == "", r.all


def test_same_session_entry_is_included_even_without_a_project():
    st = h.fresh("s19t5_sess")
    plain = h.fresh("s19t5_sess_plain")  # not a repository
    _seed(st, [_e("sess-agent", project="", sid="me"), _e("foreign-agent", project="", sid="not-me")])
    assert _ctx(_hook(st, _payload(plain, sid="me"))) == PREFIX + "sess-agent"


def test_same_session_entry_of_another_project_is_included():
    st = h.fresh("s19t5_sess2")
    mine, other = h.mk_repo("s19t5_sess2_a"), h.mk_repo("s19t5_sess2_b")
    _seed(st, [_e("sess-agent", project=_proj(other), sid="me")])
    assert _ctx(_hook(st, _payload(mine, sid="me"))) == PREFIX + "sess-agent"


def test_finished_same_session_entry_is_still_excluded():
    st = h.fresh("s19t5_sess3")
    plain = h.fresh("s19t5_sess3_plain")
    _seed(st, [_e("sess-agent", sid="me", stop=True)])
    r = _hook(st, _payload(plain, sid="me"))
    assert r.rc == 0 and r.out == "", r.all


# ---------------------------------------------------------------- hook: empty and garbage
def test_no_agents_file_means_empty_stdout():
    st = h.fresh("s19t5_none")
    r = _hook(st, _payload(st))
    assert r.rc == 0 and r.out == "", r.all


def test_empty_agent_list_means_empty_stdout():
    st = h.fresh("s19t5_empty")
    _seed(st, [])
    r = _hook(st, _payload(st))
    assert r.rc == 0 and r.out == "", r.all


def test_garbage_stdin_exits_zero_with_empty_stdout():
    st = h.fresh("s19t5_garb")
    _seed(st, [_e("some-agent", project="", sid="me")])  # an agent is running, so only the bad input keeps the output empty
    for raw in ("this is not json {{{", "[1, 2, 3]", "\x00\xff garbage", ""):
        r = _hook(st, None, raw=raw)
        assert r.rc == 0 and r.out == "", (raw, r.all)


def test_corrupt_agents_file_exits_zero_with_empty_stdout():
    st = h.fresh("s19t5_badfile")
    (st / "agents").mkdir(parents=True)
    (st / "agents" / "active.json").write_text("{corrupt", encoding="utf-8")
    r = _hook(st, _payload(st, sid="me"))
    assert r.rc == 0 and r.out == "", r.all


# ---------------------------------------------------------------- hook: '?' suffix and the name limit
def test_question_mark_suffix_after_ten_minutes_without_events():
    st = h.fresh("s19t5_q")
    _seed(st, [_e("old-agent", start_ago=700, sid="me"),
               _e("alive-agent", start_ago=700, sid="me", activity_ago=100),
               _e("new-agent", start_ago=100, sid="me")])
    assert _ctx(_hook(st, _payload(st, sid="me"))) == PREFIX + "old-agent?, alive-agent, new-agent"


def test_agent_just_under_ten_minutes_has_no_question_mark():
    st = h.fresh("s19t5_q2")
    _seed(st, [_e("edge-agent", start_ago=570, sid="me")])
    assert _ctx(_hook(st, _payload(st, sid="me"))) == PREFIX + "edge-agent"


def test_at_most_ten_names_are_listed():
    st = h.fresh("s19t5_max")
    _seed(st, [_e(f"agent{i:02d}", sid="me") for i in range(12)])
    line = _ctx(_hook(st, _payload(st, sid="me")))
    assert line.startswith(PREFIX), line
    listed = line[len(PREFIX):].split(" (+")[0].split(", ")
    assert listed == [f"agent{i:02d}" for i in range(10)], line
    assert "agent10" not in line and "agent11" not in line, line
    assert "(+2 more)" in line, line  # documented in the hook: the cut-off count is shown


def test_exactly_ten_names_are_all_listed_without_a_more_marker():
    st = h.fresh("s19t5_ten")
    _seed(st, [_e(f"agent{i:02d}", sid="me") for i in range(10)])
    line = _ctx(_hook(st, _payload(st, sid="me")))
    assert line == PREFIX + ", ".join(f"agent{i:02d}" for i in range(10)), line


# ---------------------------------------------------------------- report.running_agents
class _Env:
    """Sets/unsets environment variables for one block and restores them."""

    def __init__(self, **kv):
        self.kv, self.old = kv, {}

    def __enter__(self):
        for k, v in self.kv.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *e):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_report_reads_the_state_folder_from_the_config_file_when_the_variable_is_unset():
    st = h.fresh("s19t5_cfg_state")
    repo = h.mk_repo("s19t5_cfg_repo")
    _seed(st, [_e("cfg-agent", project=_proj(repo))])
    cfg = h.fresh("s19t5_cfg") / "config.json"
    cfg.write_text(json.dumps({"HARNESS_HOOK_STATE": str(st)}), encoding="utf-8")
    with _Env(HARNESS_HOOK_STATE=None, HARNESS_HOOK_CONFIG=str(cfg)):
        got = report.running_agents(repo)
    assert [e["type"] for e in got] == ["cfg-agent"], got


def test_report_environment_variable_wins_over_the_config_file():
    st_env, st_cfg = h.fresh("s19t5_win_env"), h.fresh("s19t5_win_cfg")
    repo = h.mk_repo("s19t5_win_repo")
    _seed(st_env, [_e("env-agent", project=_proj(repo))])
    _seed(st_cfg, [_e("cfg-agent", project=_proj(repo))])
    cfg = h.fresh("s19t5_win") / "config.json"
    cfg.write_text(json.dumps({"HARNESS_HOOK_STATE": str(st_cfg)}), encoding="utf-8")
    with _Env(HARNESS_HOOK_STATE=str(st_env), HARNESS_HOOK_CONFIG=str(cfg)):
        got = report.running_agents(repo)
    assert [e["type"] for e in got] == ["env-agent"], got


def test_report_unreadable_config_gives_empty_list_not_an_error():
    st = h.fresh("s19t5_badcfg_state")
    repo = h.mk_repo("s19t5_badcfg_repo")
    _seed(st, [_e("x-agent", project=_proj(repo))])
    cfg = h.fresh("s19t5_badcfg") / "config.json"
    cfg.write_text("{not json", encoding="utf-8")
    with _Env(HARNESS_HOOK_STATE=None, HARNESS_HOOK_CONFIG=str(cfg), USERPROFILE=str(cfg.parent), HOME=str(cfg.parent)):
        assert report.running_agents(repo) == []  # home folder is a temp folder without agents/active.json


def test_report_excludes_finished_and_other_project_entries():
    st = h.fresh("s19t5_rep_sel")
    mine, other = h.mk_repo("s19t5_rep_a"), h.mk_repo("s19t5_rep_b")
    _seed(st, [_e("fin", project=_proj(mine), stop=True), _e("oth", project=_proj(other)), _e("mine", project=_proj(mine))])
    with _Env(HARNESS_HOOK_STATE=str(st)):
        assert [e["type"] for e in report.running_agents(mine)] == ["mine"]


def test_report_from_a_linked_worktree_matches_entries_stored_with_the_main_checkout():
    st = h.fresh("s19t5_wt_state")
    main = h.mk_repo("s19t5_wt_main")
    wt = h.fresh("s19t5_wt_dir") / "linked"
    r = h.git(main, "worktree", "add", "-q", str(wt), "-b", "wtbranch")
    assert r.rc == 0, r.all
    assert wt.joinpath(".git").is_file(), "fixture: a linked worktree has a .git file, not a folder"
    _seed(st, [_e("wt-agent", project=_proj(main))])
    with _Env(HARNESS_HOOK_STATE=str(st)):
        assert [e["type"] for e in report.running_agents(wt)] == ["wt-agent"]
        assert [e["type"] for e in report.running_agents(main)] == ["wt-agent"]  # twin: the main checkout itself


def test_report_worktree_does_not_match_an_unrelated_project():
    st = h.fresh("s19t5_wt2_state")
    main, other = h.mk_repo("s19t5_wt2_main"), h.mk_repo("s19t5_wt2_other")
    wt = h.fresh("s19t5_wt2_dir") / "linked"
    assert h.git(main, "worktree", "add", "-q", str(wt), "-b", "wtbranch2").rc == 0
    _seed(st, [_e("other-agent", project=_proj(other))])
    with _Env(HARNESS_HOOK_STATE=str(st)):
        assert report.running_agents(wt) == []


# ---------------------------------------------------------------- report.sec_agents
def test_sec_agents_shows_the_not_running_line_when_none_run():
    st = h.fresh("s19t5_sec_none")
    repo = h.mk_repo("s19t5_sec_repo")
    _seed(st, [_e("fin", project=_proj(repo), stop=True)])
    with _Env(HARNESS_HOOK_STATE=str(st)):
        html_out = report.sec_agents(repo)
    assert 'id="running-agents"' in html_out and "Запущенные агенты" in html_out, html_out
    assert "не запущены" in html_out, html_out
    assert "<table>" not in html_out and "fin" not in html_out, html_out


def test_sec_agents_without_a_state_file_shows_the_not_running_line():
    st = h.fresh("s19t5_sec_nofile")
    repo = h.mk_repo("s19t5_sec_nofile_repo")
    with _Env(HARNESS_HOOK_STATE=str(st)):
        assert "не запущены" in report.sec_agents(repo)


def test_sec_agents_lists_a_running_agent_and_no_not_running_line_twin():
    st = h.fresh("s19t5_sec_run")
    repo = h.mk_repo("s19t5_sec_run_repo")
    _seed(st, [_e("run-agent", project=_proj(repo), start_ago=100), _e("stale-agent", project=_proj(repo), start_ago=900)])
    with _Env(HARNESS_HOOK_STATE=str(st)):
        html_out = report.sec_agents(repo)
    assert "<code>run-agent</code>" in html_out and "<code>stale-agent</code>" in html_out, html_out
    assert "не запущены" not in html_out, html_out
    assert "статус неизвестен" in html_out and "старт замечен" in html_out, html_out


if __name__ == "__main__":
    sys.exit(h.run_all(globals()))
