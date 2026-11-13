"""S16-T2 tests: the project recorded for an agent run (harness/hooks/agent_visibility.py).
Written from the S16-T2 acceptance criteria, not from the code. The hook runs as a real subprocess with a JSON payload on
stdin; HARNESS_HOOK_STATE points to a temp folder, so the real ~/.harness-hooks is never touched.
Run: C:\\Python314\\python.exe -I tests/test_s16_agent_project.py"""
import importlib.util
import json
import os
import sys
from pathlib import Path

_s = importlib.util.spec_from_file_location("hv2", Path(__file__).resolve().parent / "hv2.py")
h = importlib.util.module_from_spec(_s)
sys.modules["hv2"] = h
_s.loader.exec_module(h)

HOOK = h.SRC / "harness" / "hooks" / "agent_visibility.py"


def _norm(p):
    return os.path.normcase(str(Path(p).resolve()))


def _state(name):
    return h.fresh(name)


def _fire(state, payload):
    r = h.run([h.PY, "-I", "-s", "-X", "utf8", str(HOOK), "--harness-client=claude"], state, {"HARNESS_HOOK_STATE": str(state)},
              json.dumps(payload))
    assert r.rc == 0, r.all
    return r


def _agents(state):
    f = state / "agents" / "active.json"
    return json.loads(f.read_text(encoding="utf-8"))["agents"] if f.exists() else []


def _pre(state, cwd, prompt=None, sid="s1", tuid="t1", agent="python-reviewer", key="prompt"):
    ti = {"subagent_type": agent}
    if prompt is not None:
        ti[key] = prompt
    return _fire(state, {"hook_event_name": "PreToolUse", "session_id": sid, "tool_use_id": tuid, "cwd": str(cwd),
                         "tool_name": "Agent", "tool_input": ti})


def _start(state, cwd, sid="s1", agent="python-reviewer"):
    return _fire(state, {"hook_event_name": "SubagentStart", "session_id": sid, "cwd": str(cwd), "agent_type": agent})


def _plain_dir(name):
    d = h.fresh(name)
    assert not h.git(d, "rev-parse", "--git-dir").rc == 0, f"temp folder {d} is inside a git repo; test premise broken"
    return d


# ---------------------------------------------------------------- criterion 3
def test_non_repo_cwd_takes_project_from_repo_path_in_prompt():
    repo = h.mk_repo("s16a_repo")
    cwd, st = _plain_dir("s16a_cwd"), _state("s16a_st")
    _pre(st, cwd, f"Review the change in {repo} and report.")
    a = _agents(st)
    assert len(a) == 1 and _norm(a[0]["project"]) == _norm(repo), a


def test_path_to_file_inside_repo_and_message_key_work():
    repo = h.mk_repo("s16a_file")
    cwd, st = _plain_dir("s16a_file_cwd"), _state("s16a_file_st")
    _pre(st, cwd, f"read {repo / 'README.md'} now", key="message")
    a = _agents(st)
    assert _norm(a[0]["project"]) == _norm(repo), a


def test_path_to_not_yet_existing_file_in_repo_resolves_to_repo():
    repo = h.mk_repo("s16a_missing")
    cwd, st = _plain_dir("s16a_missing_cwd"), _state("s16a_missing_st")
    _pre(st, cwd, f"create {repo / 'sub' / 'new_file.py'} please")
    assert _norm(_agents(st)[0]["project"]) == _norm(repo)


def test_worktree_path_maps_to_main_checkout():
    repo = h.mk_repo("s16a_main")
    wt = h.fresh("s16a_wt_parent") / "wt"
    r = h.git(repo, "worktree", "add", "-q", "-b", "feat", str(wt))
    assert r.rc == 0, r.all
    cwd, st = _plain_dir("s16a_wt_cwd"), _state("s16a_wt_st")
    _pre(st, cwd, f"work only in {wt}")
    p = _agents(st)[0]["project"]
    assert _norm(p) == _norm(repo) and _norm(p) != _norm(wt), p


def test_no_path_or_non_repo_path_gives_empty_project():
    cwd, other = _plain_dir("s16a_none_cwd"), _plain_dir("s16a_none_other")
    st = _state("s16a_none_st")
    _pre(st, cwd, "no paths here at all", tuid="t1")
    _pre(st, cwd, f"look at {other} only", tuid="t2", agent="architect")
    _pre(st, cwd, None, tuid="t3", agent="e2e-runner")
    a = _agents(st)
    assert len(a) == 3 and [x["project"] for x in a] == ["", "", ""], a


def test_leading_non_repo_path_is_skipped_and_repo_path_binds():
    repo_a = h.mk_repo("s16a_first_a")
    other = _plain_dir("s16a_first_plain")
    cwd, st = _plain_dir("s16a_first_cwd"), _state("s16a_first_st")
    _pre(st, cwd, f"compare {other} then {repo_a}")
    assert _norm(_agents(st)[0]["project"]) == _norm(repo_a)


def test_repo_cwd_wins_over_path_in_prompt():
    repo_cwd, repo_text = h.mk_repo("s16a_cwdwins"), h.mk_repo("s16a_textrepo")
    st = _state("s16a_cwdwins_st")
    _pre(st, repo_cwd, f"please review {repo_text}")
    assert _norm(_agents(st)[0]["project"]) == _norm(repo_cwd)


def test_forward_slash_and_trailing_punctuation_path():
    repo = h.mk_repo("s16a_slash")
    cwd, st = _plain_dir("s16a_slash_cwd"), _state("s16a_slash_st")
    _pre(st, cwd, f"Work in {str(repo).replace(chr(92), '/')}.")
    assert _norm(_agents(st)[0]["project"]) == _norm(repo)


def test_prompt_text_is_not_stored_in_state():
    repo = h.mk_repo("s16a_privacy")
    cwd, st = _plain_dir("s16a_privacy_cwd"), _state("s16a_privacy_st")
    _pre(st, cwd, f"SECRETMARKER-123 review {repo}")
    raw = (st / "agents" / "active.json").read_text(encoding="utf-8")
    assert "SECRETMARKER" not in raw


# ---------------------------------------------------------------- criterion 4
def test_subagent_start_fills_empty_project_of_claimed_pretooluse_entry():
    repo = h.mk_repo("s16a_claim")
    cwd, st = _plain_dir("s16a_claim_cwd"), _state("s16a_claim_st")
    _pre(st, cwd, "no path in this prompt")
    assert _agents(st)[0]["project"] == ""
    _start(st, repo)
    a = _agents(st)
    assert len(a) == 1, f"SubagentStart did not claim the entry: {a}"
    assert _norm(a[0]["project"]) == _norm(repo), a
    assert sorted(a[0]["sources"]) == ["pre", "start"], a


def test_subagent_start_does_not_overwrite_existing_project():
    repo_pre, repo_start = h.mk_repo("s16a_keep_pre"), h.mk_repo("s16a_keep_start")
    st = _state("s16a_keep_st")
    _pre(st, repo_pre, "x")
    _start(st, repo_start)
    a = _agents(st)
    assert len(a) == 1 and _norm(a[0]["project"]) == _norm(repo_pre), a


def test_subagent_start_with_non_repo_cwd_leaves_project_empty_and_does_not_crash():
    cwd, st = _plain_dir("s16a_empty_cwd"), _state("s16a_empty_st")
    _pre(st, cwd, "no path")
    _start(st, cwd)
    a = _agents(st)
    assert len(a) == 1 and a[0]["project"] == "", a


def test_start_of_other_agent_type_does_not_fill_entry():
    repo = h.mk_repo("s16a_othertype")
    cwd, st = _plain_dir("s16a_othertype_cwd"), _state("s16a_othertype_st")
    _pre(st, cwd, "no path", agent="python-reviewer")
    _start(st, repo, agent="architect")
    a = {x["type"]: x["project"] for x in _agents(st)}
    assert a["python-reviewer"] == "" and _norm(a["architect"]) == _norm(repo), a


# ---------------------------------------------------------------- criterion 3 (hardening)
def test_url_and_unc_tails_are_ignored_and_later_repo_path_still_binds():
    repo = h.mk_repo("s16a_urlunc")
    cwd, st = _plain_dir("s16a_urlunc_cwd"), _state("s16a_urlunc_st")
    fwd = str(repo).replace(chr(92), "/")
    _pre(st, cwd, f"see https://example.invalid/x/y and \\\\nohost.invalid\\share\\x and //nohost2.invalid/s/x then {fwd}")
    assert _norm(_agents(st)[0]["project"]) == _norm(repo)


def test_only_url_or_unc_gives_empty_project():
    cwd, st = _plain_dir("s16a_onlyurl_cwd"), _state("s16a_onlyurl_st")
    _pre(st, cwd, "https://example.invalid/a/b and \\\\nohost.invalid\\share\\x and //nohost2.invalid/s/x")
    assert _agents(st)[0]["project"] == ""


def test_bare_posix_path_is_ignored_on_windows():
    if os.name != "nt":
        return
    cwd, st = _plain_dir("s16a_posix_cwd"), _state("s16a_posix_st")
    # /c/... style path of an existing repo must not bind on Windows
    repo = h.mk_repo("s16a_posix_repo")
    posix = "/" + str(repo)[0].lower() + str(repo)[2:].replace(chr(92), "/")
    _pre(st, cwd, f"work in {posix}")
    assert _agents(st)[0]["project"] == ""


def test_two_different_repos_in_text_give_empty_project():
    a, b = h.mk_repo("s16a_two_a"), h.mk_repo("s16a_two_b")
    cwd, st = _plain_dir("s16a_two_cwd"), _state("s16a_two_st")
    _pre(st, cwd, f"compare {a} with {b}")
    assert _agents(st)[0]["project"] == ""


def test_two_paths_in_same_repo_and_its_worktree_still_bind():
    repo = h.mk_repo("s16a_same")
    wt = h.fresh("s16a_same_wtp") / "wt"
    assert h.git(repo, "worktree", "add", "-q", "-b", "f2", str(wt)).rc == 0
    cwd, st = _plain_dir("s16a_same_cwd"), _state("s16a_same_st")
    _pre(st, cwd, f"diff {repo / 'README.md'} against {wt / 'README.md'}")
    assert _norm(_agents(st)[0]["project"]) == _norm(repo)


# ---------------------------------------------------------------- criterion 4 (retry)
def test_retried_pretooluse_with_same_tool_use_id_fills_empty_project():
    repo = h.mk_repo("s16a_retry")
    cwd, st = _plain_dir("s16a_retry_cwd"), _state("s16a_retry_st")
    _pre(st, cwd, "no path yet", tuid="same")
    assert _agents(st)[0]["project"] == ""
    _pre(st, cwd, f"now in {repo}", tuid="same")
    a = _agents(st)
    assert len(a) == 1 and _norm(a[0]["project"]) == _norm(repo), a


def test_retried_pretooluse_does_not_overwrite_existing_project():
    r1, r2 = h.mk_repo("s16a_retry_k1"), h.mk_repo("s16a_retry_k2")
    cwd, st = _plain_dir("s16a_retry_k_cwd"), _state("s16a_retry_k_st")
    _pre(st, cwd, f"in {r1}", tuid="same")
    _pre(st, cwd, f"in {r2}", tuid="same")
    a = _agents(st)
    assert len(a) == 1 and _norm(a[0]["project"]) == _norm(r1), a


# ---------------------------------------------------------------- criterion 3 (directory cap)
def _subdirs(repo, n):
    out = []
    for i in range(n):
        (repo / f"sub{i}").mkdir()
        out.append(repo / f"sub{i}")
    return out


def test_directory_cap_with_more_paths_remaining_gives_empty_project():
    # S18-T1: paths past the 5-directory cap are still scanned. A NEW repository root after the cap gives '';
    # a 6th directory of the same repository keeps the binding.
    repo = h.mk_repo("s16a_cap6")
    other = h.mk_repo("s16a_cap6_other")
    cwd, st = _plain_dir("s16a_cap6_cwd"), _state("s16a_cap6_st")
    dirs = _subdirs(repo, 6)
    _pre(st, cwd, "dirs " + " ".join(str(d) for d in dirs) + f" and {other / 'x.py'}", tuid="t1")
    a = {x["type"]: x["project"] for x in _agents(st)}
    assert a["python-reviewer"] == "", a
    # same-repo half runs in-process with a long deadline: six git calls overrun the production 3 s deadline on a slow machine
    sys.path.insert(0, str(h.SRC / "harness" / "hooks"))
    import agent_visibility as av
    av.BIND_DEADLINE_S = 120.0
    av.BIND_GIT_TIMEOUT_S = 60
    text = "dirs " + " ".join(str(d) for d in dirs)
    assert _norm(av.project_from_text({"prompt": text})) == _norm(repo), text


def test_exactly_five_directories_in_one_repo_still_bind():
    repo = h.mk_repo("s16a_cap5")
    cwd, st = _plain_dir("s16a_cap5_cwd"), _state("s16a_cap5_st")
    dirs = _subdirs(repo, 5)
    _pre(st, cwd, "dirs " + " ".join(str(d) for d in dirs))
    assert _norm(_agents(st)[0]["project"]) == _norm(repo)


# ---------------------------------------------------------------- criterion 4 (git timeout / cannot start), in-process
def _hook_module():
    sys.path.insert(0, str(HOOK.parent))
    spec = importlib.util.spec_from_file_location("agent_visibility_under_test", HOOK)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    import _agentio
    return m, _agentio


def test_git_timeout_or_cannot_start_gives_empty_project_even_after_a_repo_was_found():
    import subprocess
    m, aio = _hook_module()
    repo = h.mk_repo("s16a_to_repo")
    other = _plain_dir("s16a_to_plain")
    (other / "d").mkdir()
    real = aio.run_git
    text = {"prompt": f"first {repo} then {other / 'd'}"}
    try:
        assert _norm(m.project_from_text(text)) == _norm(repo)  # control: unpatched git binds the repo
        for rc in (124, 127):
            def fake(args, cwd=None, timeout=None, _rc=rc):
                if str(cwd).lower() == str(other / "d").lower():
                    return subprocess.CompletedProcess(["git"], _rc, "", "x")
                return real(args, cwd=cwd, timeout=timeout)
            aio.run_git = fake
            assert m.project_from_text(text) == "", rc
        aio.run_git = lambda *a, **k: subprocess.CompletedProcess(["git"], 124, "", "git timed out")
        assert m.project_from_text({"prompt": f"only {repo}"}) == ""
    finally:
        aio.run_git = real


if __name__ == "__main__":
    sys.exit(h.run_all(globals()))
