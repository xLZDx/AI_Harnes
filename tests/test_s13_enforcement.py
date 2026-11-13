"""S13 enforcement tests (recovery plan R1, R2, R4), written from the acceptance criteria.
Covers: rules_gate.py (operator rule 4 + push/PR approval gate) for the Claude `Bash` tool and the Codex `shell` tool,
tools/workflow_guard.py + tools/githooks/pre-push against a local bare remote, and agent_visibility.py (START deduplication,
STOP line, registration in the built settings.hooks.json).
Hooks run as subprocesses through harness/hooks/run_hook.py. All state and metrics live under D:\\tmp\\harness_v2_tests (hv2).
Run: C:\\Python314\\python.exe -I tests/test_s13_enforcement.py"""
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hv2  # noqa: E402

HOOKS = hv2.SRC / "harness" / "hooks"
RUNNER = HOOKS / "run_hook.py"
PY = hv2.PY


# ------------------------------------------------------------------ helpers
def hook(script, payload, client="claude"):
    r = hv2.run([PY, "-s", "-X", "utf8", str(RUNNER), script, f"--harness-client={client}"], hv2.TMP,
                inp=json.dumps(payload))
    return r


def shell_payload(command, client="claude", agent_type="", cwd=None):
    p = {"hook_event_name": "PreToolUse", "tool_name": "Bash" if client == "claude" else "shell",
         "tool_input": {"command": command}, "cwd": str(cwd or hv2.TMP), "session_id": "s13"}
    if agent_type:
        p["agent_type"] = agent_type
    return p


def gate(command, client="claude", agent_type="", cwd=None):
    """(denied, reason, raw result)."""
    r = hook("rules_gate.py", shell_payload(command, client, agent_type, cwd), client)
    assert r.rc == 0, (command, r.rc, r.all)
    if not r.out.strip():
        return False, "", r
    o = json.loads(r.out)["hookSpecificOutput"]
    return o.get("permissionDecision") == "deny", o.get("permissionDecisionReason", ""), r


def wrapped(cmd):
    return [cmd, f"cd x && {cmd}", f'bash -c "{cmd}"', f'powershell -Command "{cmd}"']


def check(failures):
    assert not failures, "\n" + "\n".join(failures)


DENIED_R1 = ["npm test", "npm run test", "pnpm test", "yarn test", "npx vitest", "vitest run", "pytest", "pytest -q",
             "python -m pytest", "npm run lint", "eslint .", "ruff check .", "tsc --noEmit", "npm run typecheck",
             "mypy src", "npm run verify", "gh workflow run ci.yml", "gh workflow enable 123", "gh run rerun 5",
             "gh run watch", "gh pr checks 12"]


# ------------------------------------------------------------------ R1: rules gate, shell commands
def _denied_matrix(client):
    fails = []
    for base in DENIED_R1:
        for cmd in wrapped(base):
            denied, reason, r = gate(cmd, client)
            if not denied:
                fails.append(f"[{client}] NOT denied: {cmd!r} out={r.out!r} err={r.err!r}")
            elif "rule 4" not in reason.lower():
                fails.append(f"[{client}] reason lacks 'rule 4': {cmd!r} -> {reason[:120]!r}")
    check(fails)


def test_r1_denied_commands_claude_bash():
    _denied_matrix("claude")


def test_r1_denied_commands_codex_shell():
    _denied_matrix("codex")


def test_r1_allowed_commands_both_clients():
    fails = []
    for client in ("claude", "codex"):
        for cmd in ['git commit -m "npm test fix"', "python -I tests/test_x.py", "git status"]:
            denied, reason, r = gate(cmd, client)
            if denied:
                fails.append(f"[{client}] wrongly denied: {cmd!r}: {reason[:160]}")
    check(fails)


def test_r1_named_test_files_depend_on_agent_type():
    fails = []
    for client in ("claude", "codex"):
        for cmd in ("pytest tests/test_a.py", "npx vitest run src/a.test.ts"):
            for agent in ("functional-test-reviewer", "tdd-guide", "e2e-runner"):
                denied, reason, _ = gate(cmd, client, agent)
                if denied:
                    fails.append(f"[{client}] test agent {agent} wrongly denied {cmd!r}: {reason[:120]}")
            for agent in ("code-reviewer", ""):
                denied, reason, _ = gate(cmd, client, agent)
                if not denied:
                    fails.append(f"[{client}] agent {agent or 'main'!r} NOT denied for {cmd!r}")
                elif "rule 4" not in reason.lower():
                    fails.append(f"[{client}] reason lacks 'rule 4' for {agent or 'main'!r}/{cmd!r}")
    check(fails)


def test_r1_full_suite_still_denied_for_test_agent():
    denied, reason, r = gate("pytest", "claude", "tdd-guide")
    assert denied, (r.out, r.err)
    assert "rule 4" in reason.lower()


def test_r1_non_shell_tool_is_ignored():
    p = shell_payload("npm test")
    p["tool_name"] = "Read"
    r = hook("rules_gate.py", p)
    assert r.rc == 0 and r.out.strip() == "", (r.out, r.err)


# ------------------------------------------------------------------ R2: push / PR approval gate
def make_gate_repo(name, approval, tasks):
    """approval: None | 'head' | 'stale'. tasks: list of (state, blocked?) in one open sprint."""
    repo = hv2.mk_repo(name)
    first = hv2.head(repo)
    hv2.commit(repo, "a.txt", "a\n", "second")
    cur = hv2.head(repo)
    assert first != cur
    (repo / ".harness-tools").mkdir()
    shutil.copy2(hv2.TOOLS / "ledger.py", repo / ".harness-tools" / "ledger.py")
    ts = []
    for i, (state, blocked) in enumerate(tasks, 1):
        t = {"id": f"S1-T{i}", "title": f"task {i}", "state": state}
        if blocked:
            t["blocked"] = {"class": "OPERATOR", "cause": "needs operator"}
        ts.append(t)
    ap = None
    if approval == "head":
        ap = {"head": cur, "by": "GPT-PM", "at": "2026-11-12T00:00:00+00:00", "evidence": "test"}
    elif approval == "stale":
        ap = {"head": first, "by": "GPT-PM", "at": "2026-11-12T00:00:00+00:00", "evidence": "test"}
    led = {"version": 1, "project": name, "sprints": [{"id": "S1", "title": "s", "state": "open", "tasks": ts}],
           "approval": ap, "events": []}
    (repo / ".harness").mkdir()
    (repo / ".harness" / "ledger.json").write_text(json.dumps(led), encoding="utf-8")
    return repo


def push_forms(repo):
    """(command, cwd) variants of git push and gh pr create."""
    rp = repo.as_posix()
    out = []
    for base in ("git push", "gh pr create --title t --body b"):
        out.append((base, repo))
        out.append((f"cd {rp} && {base}", hv2.TMP))
    out.append((f"git -C {rp} push", hv2.TMP))
    out.append((f"git -C {rp} push origin main", hv2.TMP))
    return out


def _expect(repo, want_denied, must_mention=None):
    fails = []
    for cmd, cwd in push_forms(repo):
        denied, reason, r = gate(cmd, "claude", cwd=cwd)
        if denied != want_denied:
            fails.append(f"want_denied={want_denied} got {denied}: {cmd!r} cwd={cwd} out={r.out!r} err={r.err!r}")
        elif denied and must_mention and must_mention.lower() not in reason.lower():
            fails.append(f"reason lacks {must_mention!r}: {cmd!r} -> {reason!r}")
    check(fails)


def test_r2_denied_without_approval():
    _expect(make_gate_repo("r2a", None, [("done", False)]), True, "approval")


def test_r2_denied_when_approval_bound_to_other_head():
    _expect(make_gate_repo("r2b", "stale", [("done", False)]), True, "HEAD")


def test_r2_denied_when_task_todo():
    _expect(make_gate_repo("r2c", "head", [("verified", False), ("todo", False)]), True, "S1-T2")


def test_r2_denied_when_task_implemented():
    _expect(make_gate_repo("r2d", "head", [("done", False), ("implemented", False)]), True, "S1-T2")


def test_r2_allowed_when_approved_and_all_verified_done_or_blocked():
    _expect(make_gate_repo("r2e", "head", [("verified", False), ("done", False), ("todo", True)]), False)


def test_r2_dry_run_allowed_even_without_approval():
    repo = make_gate_repo("r2f", None, [("todo", False)])
    rp = repo.as_posix()
    for cmd, cwd in [("git push --dry-run", repo), (f"git -C {rp} push --dry-run", hv2.TMP),
                     (f"cd {rp} && git push --dry-run origin main", hv2.TMP)]:
        denied, reason, r = gate(cmd, "claude", cwd=cwd)
        assert not denied, (cmd, reason)


def test_r2_repo_without_ledger_allowed():
    repo = hv2.mk_repo("r2g")
    rp = repo.as_posix()
    for cmd, cwd in [("git push", repo), (f"git -C {rp} push", hv2.TMP), (f"cd {rp} && git push", hv2.TMP),
                     ("gh pr create --title t", repo)]:
        denied, reason, r = gate(cmd, "claude", cwd=cwd)
        assert not denied, (cmd, reason)


def test_r2_codex_shell_push_gate():
    bad = make_gate_repo("r2h", "stale", [("done", False)])
    ok = make_gate_repo("r2i", "head", [("done", False)])
    for repo, want in ((bad, True), (ok, False)):
        rp = repo.as_posix()
        for cmd, cwd in [("git push", repo), (f"git -C {rp} push", hv2.TMP), (f"cd {rp} && git push", hv2.TMP)]:
            denied, reason, r = gate(cmd, "codex", cwd=cwd)
            assert denied == want, (want, cmd, r.out, r.err)


# ------------------------------------------------------------------ R1 git side: workflow_guard + pre-push hook
def push_with_workflow(name, workflow_text):
    repo = hv2.mk_repo(name)
    hv2.commit(repo, ".github/workflows/ci.yml", workflow_text, "add workflow")
    bare = hv2.fresh(name + "-remote")
    assert hv2.git(bare, "init", "-q", "--bare", "-b", "main").rc == 0
    assert hv2.git(repo, "remote", "add", "origin", str(bare)).rc == 0
    tools = repo / ".harness-tools"
    tools.mkdir()
    for f in ("ledger.py", "workflow_guard.py"):
        shutil.copy2(hv2.TOOLS / f, tools / f)
    hooks = repo / "hooks-copy"
    shutil.copytree(hv2.TOOLS / "githooks", hooks)
    h = hv2.head(repo)
    (repo / ".harness").mkdir()
    (repo / ".harness" / "ledger.json").write_text(json.dumps(
        {"version": 1, "project": name, "sprints": [], "events": [],
         "approval": {"head": h, "by": "GPT-PM", "at": "2026-11-12T00:00:00+00:00", "evidence": "test"}}), encoding="utf-8")
    assert hv2.git(repo, "config", "core.hooksPath", hooks.as_posix()).rc == 0
    r = hv2.git(repo, "push", "origin", "main", env={"HARNESS_PYTHON": PY})
    remote_has = hv2.git(bare, "rev-parse", "--verify", "-q", "refs/heads/main")
    return r, remote_has.rc == 0, h, hv2.git(bare, "rev-parse", "refs/heads/main").out.strip()


BAD_WORKFLOWS = {
    "push-list": "name: ci\non: [push]\njobs:\n  b:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n",
    "pull_request": "name: ci\non:\n  pull_request:\n    branches: [main]\njobs:\n  b:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n",
    "schedule": "name: ci\non:\n  schedule:\n    - cron: '0 3 * * *'\njobs:\n  b:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n",
}


def test_prepush_refuses_auto_trigger_workflows():
    fails = []
    for key, text in BAD_WORKFLOWS.items():
        r, pushed, head, remote_head = push_with_workflow("wf-" + key.replace("_", ""), text)
        msg = r.all
        if r.rc == 0:
            fails.append(f"{key}: push accepted, expected refusal: {msg}")
        if pushed:
            fails.append(f"{key}: remote has main although push should be refused")
        if ".github/workflows/ci.yml" not in msg:
            fails.append(f"{key}: message does not name the file: {msg}")
        if "workflow_dispatch" not in msg:
            fails.append(f"{key}: message does not name workflow_dispatch: {msg}")
    check(fails)


def test_prepush_accepts_workflow_dispatch_only():
    text = "name: ci\non: workflow_dispatch\njobs:\n  b:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
    r, pushed, head, remote_head = push_with_workflow("wf-ok", text)
    assert r.rc == 0, r.all
    assert pushed and remote_head == head, (remote_head, head, r.all)


# ------------------------------------------------------------------ R4: agent visibility
def test_r4_build_registers_agent_visibility():
    src = hv2.copy_source("r4build")
    r = hv2.run([PY, "-I", str(src / "tools" / "build.py")], src)
    assert r.rc == 0, r.all
    cfg = json.loads((src / "out" / "claude" / "settings.hooks.json").read_text(encoding="utf-8"))["hooks"]

    def has(event, matcher):
        for g in cfg.get(event, []):
            if g.get("matcher", "") != matcher:
                continue
            for h in g["hooks"]:
                if "agent_visibility.py" in (h.get("args") or []) or "agent_visibility.py" in h.get("command", ""):
                    return True
        return False

    assert has("SubagentStart", ""), json.dumps(cfg.get("SubagentStart"))
    assert has("PreToolUse", "Agent|Task"), json.dumps([g.get("matcher") for g in cfg.get("PreToolUse", [])])
    assert has("SubagentStop", ""), json.dumps(cfg.get("SubagentStop"))


def _line_count(outputs, needle):
    n = 0
    for out in outputs:
        if out.strip():
            m = json.loads(out).get("systemMessage", "")
            n += m.count(needle)
    return n


def test_r4_start_line_printed_once_for_pre_pre_and_subagent_start():
    cwd = hv2.fresh("r4cwd")
    (cwd / ".git").mkdir()
    pre = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_use_id": "t1", "session_id": "r4s",
           "cwd": str(cwd), "tool_input": {"subagent_type": "code-reviewer", "description": "Review x"}}
    start = {"hook_event_name": "SubagentStart", "agent_type": "code-reviewer", "session_id": "r4s", "cwd": str(cwd)}
    stop = {"hook_event_name": "SubagentStop", "agent_type": "code-reviewer", "session_id": "r4s", "cwd": str(cwd)}
    # isolated state so earlier tests cannot leave a matching unfinished entry
    state = hv2.fresh("r4state")
    import os
    saved = os.environ.get("HARNESS_HOOK_STATE")
    outs = []
    try:
        os.environ["HARNESS_HOOK_STATE"] = str(state)
        for p in (pre, pre, start):
            r = hv2.run([PY, "-s", "-X", "utf8", str(RUNNER), "agent_visibility.py", "--harness-client=claude"],
                        hv2.TMP, env={"HARNESS_HOOK_STATE": str(state)}, inp=json.dumps(p))
            assert r.rc == 0, r.all
            outs.append(r.out)
        needle = "[agent START] code-reviewer | role code-reviewer"
        assert _line_count(outs, needle) == 1, outs
        assert _line_count(outs, "[agent START]") == 1, outs
        r = hv2.run([PY, "-s", "-X", "utf8", str(RUNNER), "agent_visibility.py", "--harness-client=claude"],
                    hv2.TMP, env={"HARNESS_HOOK_STATE": str(state)}, inp=json.dumps(stop))
        assert r.rc == 0, r.all
        assert _line_count([r.out], "[agent STOP] code-reviewer") == 1, r.out
        assert "[agent START]" not in r.out
    finally:
        if saved is None:
            os.environ.pop("HARNESS_HOOK_STATE", None)
        else:
            os.environ["HARNESS_HOOK_STATE"] = saved


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
