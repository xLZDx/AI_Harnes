"""S13 review-round regression tests (commit 25b17be): rules_gate, shell_policy_gate, workflow_guard, pre-push and the
session_close_gate agent-age cap. Written from the findings list in core/DECISION_LOG.md ('2026-11-12 S13 review round'), not from the
code. Hooks run as subprocesses with JSON on stdin through harness/hooks/run_hook.py; git repos are real, under D:\\tmp\\harness_v2_tests.
Every denied input has an allowed twin so a gate that denies everything fails too.
Run: C:\\Python314\\python.exe -I tests/test_s13_review_gates.py"""
import json
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hv2  # noqa: E402
import test_s13_enforcement as enf  # noqa: E402  (hook(), gate(), make_gate_repo())
import test_s13_r5_false_positives as r5  # noqa: E402  (transcript row builders, ledger fixture)

PY = hv2.PY
HOOKS = hv2.SRC / "harness" / "hooks"
TEST_AGENT = "tdd-guide"


def check(failures):
    assert not failures, "\n" + "\n".join(failures)


def gitbash(p):
    """Git Bash spelling of a Windows path: D:\\a\\b -> /d/a/b."""
    s = Path(p).as_posix()
    return "/" + s[0].lower() + s[2:] if re.match(r"^[A-Za-z]:/", s) else s


def denied_rule4(cmd, agent=TEST_AGENT, cwd=None):
    d, reason, r = enf.gate(cmd, "claude", agent, cwd)
    return d, reason, r


# ------------------------------------------------------------------ rules_gate: rule 4 deny
RULE4_DENY = [
    "(npm test)", "{ pytest; }", "if x; then npm test; fi", "for f in a; do pytest; done", "! npm test",
    "timeout 600 pytest", "nice npm test", "xargs pytest", "cross-env CI=1 vitest", "env -u X npm test",
    "uv run --with pkg pytest", "npm run --silent test", "npm run -s lint", "pnpm run --if-present test",
    "yarn workspace foo test", "npm exec -- vitest", "bash <<EOF\nnpm test\nEOF",
]
# a config / plugin file after a flag is not a named test file (checked as a test agent, which may run named files)
CONFIG_NOT_A_TEST = [
    "npx vitest run --config vitest.config.ts", "jest --config jest.config.js", "pytest -p plugin.py",
]


def test_rules_gate_denies_wrapped_grouped_and_flagged_full_suites():
    fails = []
    for cmd in RULE4_DENY:
        d, reason, r = denied_rule4(cmd)
        if not d:
            fails.append(f"NOT denied: {cmd!r} out={r.out!r} err={r.err!r}")
        elif "rule 4" not in reason.lower():
            fails.append(f"reason lacks 'rule 4': {cmd!r} -> {reason[:120]!r}")
    check(fails)


def test_rules_gate_config_file_flag_is_not_a_named_test_file_even_for_test_agent():
    fails = []
    for cmd in CONFIG_NOT_A_TEST:
        for agent in ("functional-test-reviewer", "tdd-guide", "e2e-runner"):
            d, reason, r = denied_rule4(cmd, agent)
            if not d:
                fails.append(f"[{agent}] NOT denied: {cmd!r} out={r.out!r}")
            elif "whole test suite" not in reason:
                fails.append(f"[{agent}] denied for the wrong reason: {cmd!r} -> {reason[:140]!r}")
    check(fails)


def test_rules_gate_denies_in_codex_shell_too():
    fails = []
    for cmd in ("(npm test)", "timeout 600 pytest", "npm run --silent test"):
        d, reason, r = enf.gate(cmd, "codex", TEST_AGENT)
        if not d:
            fails.append(f"codex NOT denied: {cmd!r} out={r.out!r}")
    check(fails)


# ------------------------------------------------------------------ rules_gate: rule 4 allow
def test_rules_gate_allows_plain_and_data_commands():
    repo = enf.make_gate_repo("rv-heredoc", "stale", [("todo", False)])  # a push mis-read from the heredoc body would be denied here
    heredoc = "cat > docs/R.md <<'EOF'\nnpm test\ngit push\nEOF"
    fails = []
    for cmd in (heredoc, "git status", "command -v pytest"):
        d, reason, r = enf.gate(cmd, "claude", "", repo)
        if d:
            fails.append(f"wrongly denied: {cmd!r}: {reason[:200]}")
    check(fails)


def test_rules_gate_allows_named_test_files_for_test_agents_and_denies_others():
    fails = []
    for cmd in ("pytest tests/test_a.py", "npx vitest run src/a.test.ts", "timeout 600 pytest tests/test_a.py",
                "jest --config jest.config.js src/a.test.ts", "pytest -p plugin.py tests/test_a.py"):
        for agent in ("functional-test-reviewer", "tdd-guide"):
            d, reason, r = enf.gate(cmd, "claude", agent)
            if d:
                fails.append(f"[{agent}] wrongly denied {cmd!r}: {reason[:160]}")
        d, reason, r = enf.gate(cmd, "claude", "")
        if not d:
            fails.append(f"main session NOT denied for named test {cmd!r}")
    check(fails)


# ------------------------------------------------------------------ rules_gate: push / PR approval
def test_rules_gate_denies_grouped_and_git_bash_pushes_to_unapproved_repo():
    repo = enf.make_gate_repo("rv-push-bad", "stale", [("done", False)])
    gb = gitbash(repo)
    assert gb.startswith("/") and ":" not in gb, gb
    cases = [("(git push)", repo), (f"cd {gb} && git push", hv2.TMP), (f"git -C {gb} push", hv2.TMP),
             (f"git -C {gb} push origin main", hv2.TMP), (f"cd {gb} && git push origin main", hv2.TMP),
             ("cd /nonexistent/zz && git push", hv2.TMP)]
    fails = []
    for cmd, cwd in cases:
        d, reason, r = enf.gate(cmd, "claude", "", cwd)
        if not d:
            fails.append(f"NOT denied: {cmd!r} cwd={cwd} out={r.out!r} err={r.err!r}")
        elif gb in cmd and "HEAD" not in reason:  # the Git Bash path must be resolved to the real repo, not just fail somewhere
            fails.append(f"denied without reading the repo's ledger: {cmd!r} -> {reason[:200]!r}")
    check(fails)


def test_rules_gate_allows_git_bash_push_and_grouped_push_to_approved_repo():
    repo = enf.make_gate_repo("rv-push-ok", "head", [("verified", False), ("done", False)])
    gb = gitbash(repo)
    cases = [("git push", repo), ("(git push)", repo), (f"cd {gb} && git push", hv2.TMP), (f"git -C {gb} push", hv2.TMP),
             ("gh pr create --title t --body b", repo)]
    fails = []
    for cmd, cwd in cases:
        d, reason, r = enf.gate(cmd, "claude", "", cwd)
        if d:
            fails.append(f"wrongly denied: {cmd!r} cwd={cwd}: {reason[:200]}")
    check(fails)


def test_rules_gate_dry_run_and_non_harness_git_bash_target_still_allowed():
    plain = hv2.mk_repo("rv-plain")  # no ledger, no harness tools: allowed
    bad = enf.make_gate_repo("rv-dry", "stale", [("todo", False)])
    fails = []
    for cmd, cwd in ((f"git -C {gitbash(plain)} push", hv2.TMP), (f"cd {gitbash(bad)} && git push --dry-run", hv2.TMP)):
        d, reason, r = enf.gate(cmd, "claude", "", cwd)
        if d:
            fails.append(f"wrongly denied: {cmd!r}: {reason[:200]}")
    check(fails)


def _malformed(name, text):
    repo = enf.make_gate_repo(name, "head", [("done", False)])
    (repo / ".harness" / "ledger.json").write_text(text, encoding="utf-8")
    return repo


def test_rules_gate_pr_create_denied_with_malformed_ledger():
    head_hint = "abc"
    good = enf.make_gate_repo("rv-led-good", "head", [("done", False)])
    d, reason, r = enf.gate("gh pr create --title t", "claude", "", good)
    assert not d, ("control: valid ledger must allow", reason)
    variants = {
        "invalid-json": "{not json",
        "list-top-level": "[]",
        "approval-string": json.dumps({"version": 1, "project": "p", "sprints": [], "events": [], "approval": head_hint}),
    }
    fails = []
    for key, text in variants.items():
        repo = _malformed("rv-led-" + key, text)
        for cmd in ("gh pr create --title t --body b", "git push"):
            d, reason, r = enf.gate(cmd, "claude", "", repo)
            if not d:
                fails.append(f"[{key}] NOT denied: {cmd!r} out={r.out!r} err={r.err!r}")
    check(fails)


# ------------------------------------------------------------------ shell_policy_gate
def policy(cmd):
    state = hv2.fresh("rv-policy")
    payload = {"hook_event_name": "PreToolUse", "session_id": "rvp", "cwd": str(state), "tool_name": "Bash",
               "tool_input": {"command": cmd}}
    r = hv2.run([PY, "-I", "-s", "-X", "utf8", str(HOOKS / "run_hook.py"), "shell_policy_gate.py", "--harness-client=claude"],
                state, {"HARNESS_HOOK_STATE": str(state), "CLAUDE_HOOK_STATE_DIR": str(state / "claude_state")}, json.dumps(payload))
    assert r.rc == 0, (cmd, r.rc, r.all)
    if not r.out.strip():
        return False, r
    o = json.loads(r.out).get("hookSpecificOutput") or {}
    return o.get("permissionDecision") == "deny", r


POLICY_DENY = [
    r'cd "C:\proj\" && git clean -fdx && echo "done"',
    'wsl bash -c "cd x; rm -rf ~/y"',
    'ssh host "cd /x; rm -rf y"',
    'docker exec c sh -c "a; rm -rf /data"',
]
POLICY_ALLOW = [
    'git commit -m "format the disk report; done"',
    'echo "rm -rf x; ok"',
    r'cd "C:\proj\" && git status && echo "done"',
    'ssh host "cd /x; ls"',
]


def test_shell_policy_gate_denies_destructive_commands_hidden_by_quotes_and_wrappers():
    fails = []
    for cmd in POLICY_DENY:
        d, r = policy(cmd)
        if not d:
            fails.append(f"NOT denied: {cmd!r} out={r.out!r} err={r.err!r}")
    check(fails)


def test_shell_policy_gate_allows_destructive_words_inside_data():
    fails = []
    for cmd in POLICY_ALLOW:
        d, r = policy(cmd)
        if d:
            fails.append(f"wrongly denied: {cmd!r} out={r.out!r}")
    check(fails)


# ------------------------------------------------------------------ workflow_guard
WF_TAIL = "jobs:\n  b:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"


def guard_repo(name, workflow_text):
    repo = hv2.mk_repo(name)
    sha = hv2.commit(repo, ".github/workflows/ci.yml", workflow_text, "add workflow")
    return repo, sha


def run_guard(repo, sha):
    line = f"refs/heads/main {sha} refs/heads/main {'0' * 40}\n"
    return hv2.run([PY, "-I", str(hv2.TOOLS / "workflow_guard.py")], repo, inp=line)


def test_workflow_guard_refuses_multiline_flow_on_with_push_or_pull_request():
    fails = []
    for key, text in {
        "multi-push-pr": "name: ci\non: [\n  push,\n  pull_request\n]\n" + WF_TAIL,
        "multi-map": "name: ci\non: {\n  push: {},\n  workflow_dispatch: {}\n}\n" + WF_TAIL,
    }.items():
        repo, sha = guard_repo("rv-wg-" + key, text)
        r = run_guard(repo, sha)
        if r.rc == 0:
            fails.append(f"{key}: accepted, expected refusal: {r.all}")
        elif "push" not in r.err or ".github/workflows/ci.yml" not in r.err:
            fails.append(f"{key}: message lacks trigger/file: {r.err!r}")
    check(fails)


def test_workflow_guard_refuses_unbalanced_bracket_as_unparsable():
    repo, sha = guard_repo("rv-wg-unbal", "name: ci\non: [push, workflow_dispatch\n" + WF_TAIL)
    r = run_guard(repo, sha)
    assert r.rc == 1, r.all
    assert "unparsable" in r.err.lower(), r.err


def test_workflow_guard_allows_multiline_workflow_dispatch_only():
    fails = []
    for key, text in {
        "list-indented-close": "name: ci\non: [\n  workflow_dispatch\n  ]\n" + WF_TAIL,
        "spaced": "name: ci\non: [ workflow_dispatch ]\n" + WF_TAIL,
    }.items():
        repo, sha = guard_repo("rv-wg-ok-" + key, text)
        r = run_guard(repo, sha)
        if r.rc != 0:
            fails.append(f"{key}: wrongly refused rc={r.rc}: {r.all}")
    check(fails)


def test_workflow_guard_refuses_flow_list_closed_at_column_zero():
    # the stricter guard treats a flow list whose `]` sits at column 0 as unparsable (fail closed)
    repo, sha = guard_repo("rv-wg-col0", "name: ci\non: [\n  workflow_dispatch\n]\n" + WF_TAIL)
    r = run_guard(repo, sha)
    assert r.rc == 1, r.all
    assert "unparsable" in r.err.lower(), r.err


def test_workflow_guard_refuses_unknown_sha_with_message():
    repo = hv2.mk_repo("rv-wg-unknown")
    r = run_guard(repo, "deadbeef" * 5)
    assert r.rc == 1, r.all
    assert "push blocked" in r.err and "cannot check" in r.err, r.err
    assert "deadbeefdead" in r.err, r.err  # names the sha it could not read


# ------------------------------------------------------------------ pre-push hook
def prepush_push(name, with_guard):
    repo = hv2.mk_repo(name)
    bare = hv2.fresh(name + "-remote")
    assert hv2.git(bare, "init", "-q", "--bare", "-b", "main").rc == 0
    assert hv2.git(repo, "remote", "add", "origin", str(bare)).rc == 0
    tools = repo / ".harness-tools"
    tools.mkdir()
    shutil.copy2(hv2.TOOLS / "ledger.py", tools / "ledger.py")
    if with_guard:
        shutil.copy2(hv2.TOOLS / "workflow_guard.py", tools / "workflow_guard.py")
    hooks = repo / "hooks-copy"
    shutil.copytree(hv2.TOOLS / "githooks", hooks)
    (repo / ".harness").mkdir()
    (repo / ".harness" / "ledger.json").write_text(json.dumps(
        {"version": 1, "project": name, "sprints": [], "events": [],
         "approval": {"head": hv2.head(repo), "by": "GPT-PM", "at": "2026-11-12T00:00:00+00:00", "evidence": "test", "request_id": "11111111-2222-4333-8444-555555555555", "reply_id": "r1"}}), encoding="utf-8")
    assert hv2.git(repo, "config", "core.hooksPath", hooks.as_posix()).rc == 0
    r = hv2.git(repo, "push", "origin", "main", env={"HARNESS_PYTHON": PY})
    remote = hv2.git(bare, "rev-parse", "--verify", "-q", "refs/heads/main")
    return r, remote.rc == 0


def test_prepush_refuses_when_workflow_guard_is_missing():
    r, pushed = prepush_push("rv-pp-noguard", with_guard=False)
    assert r.rc != 0, r.all
    assert "build.py --install-project" in r.all, r.all
    assert "workflow_guard.py" in r.all, r.all
    assert not pushed, "remote must not receive main"


def test_prepush_accepts_when_workflow_guard_is_present():
    r, pushed = prepush_push("rv-pp-guard", with_guard=True)
    assert r.rc == 0, r.all
    assert pushed, "remote must receive main"


# ------------------------------------------------------------------ session_close_gate: age cap on pending agents
def utc(seconds_ago):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def stop_with_launch(name, launch_ts):
    root = hv2.fresh(name)
    proj = root / "proj"
    proj.mkdir()
    r5.write_ledger(proj, [r5.mk_task("S9-T1", "in_progress")])
    res = r5.tool_result("a1", r5.LAUNCH)
    if launch_ts is None:
        res.pop("timestamp", None)
    else:
        res["timestamp"] = launch_ts
    rows = [r5.human("start the review", cwd=proj),
            r5.tool_use("Bash", {"command": "python .harness-tools/ledger.py start S9-T1"}, "l1"), r5.tool_result("l1", "started S9-T1"),
            r5.tool_use("Agent", {"description": "review", "prompt": "p", "run_in_background": True}, "a1"), res]
    tp = r5.write_rows(root, rows, "t.jsonl")
    payload = {"hook_event_name": "Stop", "cwd": str(proj), "session_id": "age-" + name,
               "last_assistant_message": "Agent started, waiting for its report.", "transcript_path": str(tp)}
    return r5.hook("session_close_gate.py", payload, root / "state")


def test_stop_gate_does_not_block_while_a_fresh_own_agent_runs():
    r = stop_with_launch("rv-sc-fresh", utc(60))
    assert r.rc == 0 and not r5.blocked(r), (r.out, r.err)


def test_stop_gate_blocks_when_the_launch_is_45_minutes_old():
    r = stop_with_launch("rv-sc-old", utc(45 * 60))
    assert r.rc == 0 and r5.blocked(r), (r.out, r.err)
    assert "S9-T1" in r.out, r.out


def test_stop_gate_blocks_when_the_launch_has_no_timestamp():
    r = stop_with_launch("rv-sc-nots", None)
    assert r.rc == 0 and r5.blocked(r), (r.out, r.err)
    assert "S9-T1" in r.out, r.out


def test_stop_gate_blocks_when_the_launch_just_inside_the_cap_is_notified():
    # control: the same fresh launch followed by its task-notification must block (agent finished, task still open)
    root = hv2.fresh("rv-sc-notified")
    proj = root / "proj"
    proj.mkdir()
    r5.write_ledger(proj, [r5.mk_task("S9-T1", "in_progress")])
    res = r5.tool_result("a1", r5.LAUNCH)
    res["timestamp"] = utc(60)
    note = {"type": "user", "timestamp": utc(5), "message": {"role": "user", "content":
            "<task-notification>\n<task-id>abc123</task-id>\n<status>completed</status>\n</task-notification>"}}
    rows = [r5.human("go", cwd=proj), r5.tool_use("Bash", {"command": "python .harness-tools/ledger.py start S9-T1"}, "l1"),
            r5.tool_result("l1", "ok"), r5.tool_use("Agent", {"description": "d", "prompt": "p"}, "a1"), res, note]
    tp = r5.write_rows(root, rows, "t.jsonl")
    payload = {"hook_event_name": "Stop", "cwd": str(proj), "session_id": "notified",
               "last_assistant_message": "Agent started, waiting for its report.", "transcript_path": str(tp)}
    r = r5.hook("session_close_gate.py", payload, root / "state")
    assert r5.blocked(r) and "S9-T1" in r.out, (r.out, r.err)


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
