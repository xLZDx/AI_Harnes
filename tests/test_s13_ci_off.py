"""S13 CI-OFF tests: tools/ci_triggers_off.py (operator rule 4) and audit_projects.chk_ci_github/github_slugs (commit 65ce57f).
Temp repos only under D:\\tmp\\harness_v2_tests; the tool always runs as a subprocess with --root pointing at a temp folder;
gh is never called (audit_projects.run is stubbed for gh, passed through for git).
Run: C:\\Python314\\python.exe -I tests/test_s13_ci_off.py"""
import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools"))
import hv2  # noqa: E402
import audit_projects as au  # noqa: E402
import workflow_guard as wg  # noqa: E402

TOOL = HERE.parent / "tools" / "ci_triggers_off.py"
JOBS = "jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
WF_BLOCK = "name: ci\non:\n  push:\n    branches: [main]\n  pull_request:\n" + JOBS
WF_FLOW = "name: ci\non: [push, pull_request]\n" + JOBS
WF_FLOW_MULTI = "name: ci\non: [push,\n  pull_request]\n" + JOBS
WF_SCHED = "name: nightly\n'on':\n  schedule:\n    - cron: '0 0 * * *'\n" + JOBS
WF_DEPLOY = "name: deploy\non:\n  push:\n    branches: [main]\n" + JOBS
WF = ".github/workflows/ci.yml"


def _w(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)


def mk_root():
    return hv2.fresh("ciroot")


def mk_project(root, name, workflows, crlf=False):
    repo = root / name
    repo.mkdir()
    assert hv2.git(repo, "init", "-q", "-b", "main").rc == 0
    for k, v in (("user.name", "t"), ("user.email", "t@t"), ("core.autocrlf", "false")):
        hv2.git(repo, "config", k, v)
    nohooks = hv2.fresh("nohooks")  # isolate from any global hooksPath
    hv2.git(repo, "config", "core.hooksPath", str(nohooks).replace("\\", "/"))
    _w(repo / "core/DECISION_LOG.md", "# Decisions\n")
    _w(repo / "core/LESSONS_LOG.md", "# Lessons\n")
    for rel, text in workflows.items():
        _w(repo / rel, text.replace("\n", "\r\n") if crlf else text)
    assert hv2.git(repo, "add", "-A").rc == 0
    r = hv2.git(repo, "commit", "-q", "-m", "init")
    assert r.rc == 0, r.all
    return repo


def tool(root, *args):
    return hv2.run([hv2.PY, "-I", str(TOOL), "--root", str(root), *args], root)


def rd(p):
    return Path(p).read_bytes()


def commits(repo):
    return int(hv2.git(repo, "rev-list", "--count", "HEAD").out.strip())


def tip_file(repo, rel):
    return hv2.git(repo, "show", f"HEAD:{rel}").out


# ------------------------------------------------------------------ A. ci_triggers_off

def test_block_form_push_pull_request_is_switched_and_committed():
    root = mk_root()
    repo = mk_project(root, "p1", {WF: WF_BLOCK})
    before = commits(repo)
    res = tool(root)
    assert res.rc == 0, res.all
    assert "CHANGED" in res.out, res.out
    text = rd(repo / WF).decode("utf-8")
    assert "on:\n  workflow_dispatch:\n" in text, text
    assert "#   push:" in text and "#   pull_request:" in text, text
    assert text.endswith(JOBS), text  # rest untouched
    assert text.startswith("name: ci\n")
    assert wg.triggers(tip_file(repo, WF)) == {"workflow_dispatch"}
    # the real pre-push guard sees no auto trigger in the tip
    tip = hv2.head(repo)
    g = hv2.run([hv2.PY, "-I", str(HERE.parent / "tools" / "workflow_guard.py")], repo, inp=f"refs/heads/main {tip} refs/heads/main {'0' * 40}\n")
    assert g.rc == 0, g.all
    assert commits(repo) == before + 1
    names = sorted(hv2.git(repo, "show", "--name-only", "--format=", "HEAD").out.split())
    assert names == sorted([WF, "core/DECISION_LOG.md", "core/LESSONS_LOG.md"]), names
    msg = hv2.git(repo, "log", "-1", "--format=%B").out.rstrip()
    assert msg.endswith("Task: CI-OFF"), msg
    assert hv2.git(repo, "status", "--porcelain").out.strip() == ""
    assert "CI-OFF" in rd(repo / "core/DECISION_LOG.md").decode()
    assert "CI-OFF" in rd(repo / "core/LESSONS_LOG.md").decode()


def test_flow_list_multiline_flow_and_schedule_only_are_changed():
    for label, wf, old in (("flow", WF_FLOW, "push"), ("multi", WF_FLOW_MULTI, "pull_request"), ("sched", WF_SCHED, "schedule")):
        root = mk_root()
        repo = mk_project(root, "p_" + label, {WF: wf})
        res = tool(root)
        assert res.rc == 0 and "CHANGED" in res.out, (label, res.all)
        tip = tip_file(repo, WF)
        assert wg.triggers(tip) == {"workflow_dispatch"}, (label, tip)
        assert "# " in tip and old in tip, (label, tip)  # old trigger kept as a comment
        assert tip.endswith(JOBS), (label, tip)
        assert "name:" in tip.split("\n")[0]


def test_deploy_workflow_untouched_without_flag_changed_with_flag():
    root = mk_root()
    repo = mk_project(root, "p_dep", {".github/workflows/deploy.yml": WF_DEPLOY})
    raw, n = rd(repo / ".github/workflows/deploy.yml"), commits(repo)
    res = tool(root)
    assert res.rc == 0 and "CLEAN" in res.out and "CHANGED" not in res.out, res.all
    assert rd(repo / ".github/workflows/deploy.yml") == raw and commits(repo) == n
    res = tool(root, "--include-deploy")
    assert "CHANGED" in res.out, res.all
    assert wg.triggers(tip_file(repo, ".github/workflows/deploy.yml")) == {"workflow_dispatch"}
    assert commits(repo) == n + 1


def test_dirty_decision_log_skips_without_commit_or_file_change():
    root = mk_root()
    repo = mk_project(root, "p_dirty", {WF: WF_BLOCK})
    _w(repo / "core/DECISION_LOG.md", "# Decisions\nuncommitted edit\n")
    snap = {r: rd(repo / r) for r in (WF, "core/DECISION_LOG.md", "core/LESSONS_LOG.md")}
    n = commits(repo)
    res = tool(root)
    assert "SKIP" in res.out and "CHANGED" not in res.out, res.all
    assert commits(repo) == n
    assert {r: rd(repo / r) for r in snap} == snap


def test_detached_head_skips():
    root = mk_root()
    repo = mk_project(root, "p_det", {WF: WF_BLOCK})
    assert hv2.git(repo, "checkout", "-q", "--detach").rc == 0
    raw, n = rd(repo / WF), commits(repo)
    res = tool(root)
    assert "SKIP" in res.out and "detached" in res.out and "CHANGED" not in res.out, res.all
    assert rd(repo / WF) == raw and commits(repo) == n


def test_crlf_workflow_keeps_crlf():
    root = mk_root()
    repo = mk_project(root, "p_crlf", {WF: WF_BLOCK}, crlf=True)
    res = tool(root)
    assert "CHANGED" in res.out, res.all
    raw = rd(repo / WF)
    assert b"workflow_dispatch" in raw
    assert raw.replace(b"\r\n", b"").find(b"\n") == -1, raw  # no bare LF anywhere
    assert b"on:\r\n  workflow_dispatch:\r\n" in raw, raw
    assert wg.triggers(raw.decode()) == {"workflow_dispatch"}


def test_dry_run_changes_nothing():
    root = mk_root()
    repo = mk_project(root, "p_dry", {WF: WF_BLOCK})
    raw, n, head = rd(repo / WF), commits(repo), hv2.head(repo)
    res = tool(root, "--dry-run")
    assert res.rc == 0 and "WOULD CHANGE" in res.out, res.all
    assert rd(repo / WF) == raw and commits(repo) == n and hv2.head(repo) == head
    assert hv2.git(repo, "status", "--porcelain").out.strip() == ""


def test_second_run_is_clean_and_idempotent():
    root = mk_root()
    repo = mk_project(root, "p_twice", {WF: WF_BLOCK})
    assert "CHANGED" in tool(root).out
    head, raw = hv2.head(repo), rd(repo / WF)
    res = tool(root)
    assert res.rc == 0 and "CLEAN" in res.out and "CHANGED" not in res.out, res.all
    assert hv2.head(repo) == head and rd(repo / WF) == raw


def test_virtual_marketing_company_never_touched():
    root = mk_root()
    repo = mk_project(root, "Virtual_marketing_company", {WF: WF_BLOCK})
    raw, head = rd(repo / WF), hv2.head(repo)
    res = tool(root)
    assert res.rc == 0, res.all
    assert "Virtual_marketing_company" not in res.out
    res = tool(root, "--project", "Virtual_marketing_company", "--include-deploy")
    assert "Virtual_marketing_company" not in res.out
    assert rd(repo / WF) == raw and hv2.head(repo) == head


def test_failing_commit_hook_restores_files_and_unstages():
    root = mk_root()
    repo = mk_project(root, "p_hook", {WF: WF_BLOCK})
    hooks = hv2.fresh("failhooks")
    _w(hooks / "pre-commit", "#!/bin/sh\necho blocked >&2\nexit 1\n")
    hv2.git(repo, "config", "core.hooksPath", str(hooks).replace("\\", "/"))
    snap = {r: rd(repo / r) for r in (WF, "core/DECISION_LOG.md", "core/LESSONS_LOG.md")}
    n = commits(repo)
    res = tool(root)
    assert res.rc == 1, res.all
    assert "ERROR" in res.out and "CHANGED" not in res.out, res.out
    assert {r: rd(repo / r) for r in snap} == snap
    assert commits(repo) == n
    assert hv2.git(repo, "status", "--porcelain").out.strip() == "", hv2.git(repo, "status", "--porcelain").out


# ------------------------------------------------------------------ B. audit_projects ci_github

_REAL_RUN = au.run


def _b64(text):
    return base64.b64encode(text.encode()).decode()


def _cp(rc=0, out="", err=""):
    return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def _audit_repo(remotes):
    repo = hv2.mk_repo("auditgh")
    for name, url in remotes.items():
        assert hv2.git(repo, "remote", "add", name, url).rc == 0
    return repo


def _stub(list_by_slug, api_text=None):
    """gh stub; git and everything else go to the real run. Records gh calls."""
    calls = []

    def fake(cmd, cwd=None, timeout=20, env=None, stdin=None):
        if cmd and cmd[0] == "gh":
            calls.append(list(cmd))
            if cmd[1] == "workflow":
                slug = cmd[cmd.index("--repo") + 1]
                return list_by_slug[slug]
            if cmd[1] == "api":
                return _cp(0, _b64(api_text) + "\n")
            raise AssertionError(cmd)
        return _REAL_RUN(cmd, cwd=cwd, timeout=timeout, env=env, stdin=stdin)
    return fake, calls


def _check(repo, list_by_slug, api_text=None):
    fake, calls = _stub(list_by_slug, api_text)
    au.run = fake
    try:
        return au.chk_ci_github(repo, True), calls
    finally:
        au.run = _REAL_RUN


NOT_FOUND = _cp(1, "", "HTTP 404: Not Found")
ONE_WF = _cp(0, json.dumps([{"name": "ci", "state": "active", "path": ".github/workflows/ci.yml"}]))
REMOTES = {"origin": "https://github.com/a/missing.git", "other": "https://github.com/a/real.git"}


def test_github_slugs_lists_all_remotes_origin_first():
    repo = _audit_repo({"other": "https://github.com/a/real.git", "origin": "git@github.com:a/missing.git", "gl": "https://gitlab.com/x/y.git"})
    assert au.github_slugs(repo) == ["a/missing", "a/real"]


def test_ci_github_auto_trigger_on_second_remote_fails_naming_it():
    repo = _audit_repo(REMOTES)
    (st, msg), calls = _check(repo, {"a/missing": NOT_FOUND, "a/real": ONE_WF}, "name: ci\non: push\n" + JOBS)
    assert st == "FAIL", (st, msg)
    assert "a/real" in msg and "ci.yml" in msg and "push" in msg, msg
    assert any("repos/a/real/contents/.github/workflows/ci.yml" in " ".join(c) for c in calls)


def test_ci_github_dispatch_only_content_is_ok():
    repo = _audit_repo(REMOTES)
    (st, msg), _ = _check(repo, {"a/missing": NOT_FOUND, "a/real": ONE_WF}, "name: ci\non:\n  workflow_dispatch:\n" + JOBS)
    assert st == "OK", (st, msg)
    assert "a/real" in msg


def test_ci_github_only_404_remote_is_not_applicable():
    repo = _audit_repo({"origin": "https://github.com/a/missing.git"})
    (st, msg), _ = _check(repo, {"a/missing": NOT_FOUND})
    assert st == "n.a.", (st, msg)


def test_ci_github_non_404_gh_error_fails():
    repo = _audit_repo({"origin": "https://github.com/a/missing.git"})
    (st, msg), _ = _check(repo, {"a/missing": _cp(1, "", "HTTP 500: Server Error")})
    assert st == "FAIL", (st, msg)
    assert "a/missing" in msg


def test_ci_github_non_404_error_on_second_remote_still_fails():
    repo = _audit_repo(REMOTES)
    (st, msg), _ = _check(repo, {"a/missing": NOT_FOUND, "a/real": _cp(1, "", "HTTP 403: rate limit")})
    assert st == "FAIL" and "a/real" in msg, (st, msg)


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
