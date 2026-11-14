"""S20-T4 tests: harness/hooks/lessons_recall.py run as a subprocess with Claude Code and Codex PreToolUse payloads.
Contract (D:\\tmp\\s20_criteria.md, design revision section 7): a git push / commit / gh pr / agent launch / edit under hooks, guards
or workflows / PM Bridge send injects the matching FAILURE and SHORTCOMING rules (never SUCCESS, at most 5) as PreToolUse
additionalContext, each lesson once per session per action; a command that matches no action reads no index; HARNESS_LESSONS_RECALL=off,
a missing store and a corrupt store each give exit 0 and no injection; the no-action path costs at most 100 ms more than a bare
`python -s -c pass` (median of 5).
Every test points HARNESS_LESSONS_DIR, HARNESS_LESSONS_BACKUP and HARNESS_HOOK_STATE at temp folders.
Run: C:\\Python314\\python.exe -I -s tests\\test_s20_recall.py"""
import importlib.util
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

_s = importlib.util.spec_from_file_location("hv2", Path(__file__).resolve().parent / "hv2.py")
h = importlib.util.module_from_spec(_s)
sys.modules["hv2"] = h
_s.loader.exec_module(h)

HOOK = h.SRC / "harness" / "hooks" / "lessons_recall.py"
CLI = h.SRC / "tools" / "lessons.py"
sys.path.insert(0, str(h.SRC / "harness" / "hooks"))

PUSH_RULE = "Verify the branch is approved before you push."
COMMIT_RULE = "Add the Task trailer to every commit message."
SUCCESS_RULE = "Success story about push that must never be recalled."


class World:
    def __init__(self, name):
        self.root = h.fresh(name)
        self.store = self.root / "store"
        self.state = self.root / "state"
        self.env = {"HARNESS_LESSONS_DIR": str(self.store), "HARNESS_LESSONS_BACKUP": str(self.root / "backup"),
                    "HARNESS_HOOK_STATE": str(self.state)}

    def seed(self, items):
        """items: (rule, kind, actions list). Uses the library in this process, pointed at the temp store."""
        import _lessons as L
        old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        try:
            res = L.add_many([{"rule": r, "kind": k, "project": "ProjA", "actions": a, "tags": [], "date": "2026-11-01"}
                              for r, k, a in items])
            assert all(x["status"] == "added" for x in res), res
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def seed_default(self):
        self.seed([(PUSH_RULE, "FAILURE", ["push"]), (COMMIT_RULE, "SHORTCOMING", ["commit"]),
                   (SUCCESS_RULE, "SUCCESS", ["push"])])

    def run(self, payload, extra=None, raw=None):
        env = h.base_env({**self.env, **(extra or {})})
        inp = raw if raw is not None else json.dumps(payload)
        cp = subprocess.run([h.PY, "-I", "-s", str(HOOK)], input=inp, env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", cwd=str(self.root), timeout=60)
        return cp.returncode, cp.stdout, cp.stderr

    def context(self, payload, extra=None):
        """additionalContext string or None when nothing was injected; also checks the exit code and the JSON shape."""
        rc, out, err = self.run(payload, extra)
        assert rc == 0, (rc, out, err)
        if not out.strip():
            return None
        obj = json.loads(out)
        spec = obj["hookSpecificOutput"]
        assert spec["hookEventName"] == "PreToolUse", obj
        return spec["additionalContext"]


def bash(cmd, sid="s1", tool="Bash"):
    return {"hook_event_name": "PreToolUse", "session_id": sid, "tool_name": tool, "tool_input": {"command": cmd}}


def ids(ctx):
    import re
    return re.findall(r"\bL\d{4}\b", ctx or "")


def test_claude_git_push_injects_matching_failure_rule_only():
    w = World("t4push")
    w.seed_default()
    ctx = w.context(bash("git push origin main"))
    assert ctx is not None and ctx.startswith("Global lessons for push"), ctx
    assert PUSH_RULE in ctx and "L0001" in ctx
    assert SUCCESS_RULE not in ctx and COMMIT_RULE not in ctx, ctx


def test_second_push_in_same_session_injects_nothing_but_new_session_does():
    w = World("t4once")
    w.seed_default()
    assert w.context(bash("git push origin main", "sessA")) is not None
    assert w.context(bash("git push origin main", "sessA")) is None
    assert w.context(bash("git push --force-with-lease", "sessA")) is None
    assert w.context(bash("git push origin main", "sessB")) is not None  # twin: a new session sees it again
    ctx = w.context(bash('git commit -m "x"', "sessA"))  # same session, other action: its own lessons appear once
    assert ctx is not None and COMMIT_RULE in ctx
    assert w.context(bash('git commit -m "y"', "sessA")) is None
    assert (w.store / "sessions" / "sessA.json").is_file()


def test_commit_and_gh_pr_and_chained_commands_map_to_actions():
    w = World("t4actions")
    w.seed([(COMMIT_RULE, "FAILURE", ["commit"]), ("Open the PR only after approval.", "FAILURE", ["pr"]), (PUSH_RULE, "FAILURE", ["push"])])
    assert COMMIT_RULE in w.context(bash("git add -A && git commit -m x", "c1"))
    assert "Open the PR only after approval." in w.context(bash("gh pr create --fill", "c2"))
    both = w.context(bash("git commit -m x && git push", "c3"))
    assert COMMIT_RULE in both and PUSH_RULE in both, both
    assert w.context(bash("git status", "c4")) is None  # twin: other git verbs do not trigger


def test_codex_payloads_shell_exec_command_apply_patch_and_spawn_agent():
    w = World("t4codex")
    w.seed([(PUSH_RULE, "FAILURE", ["push"]), (COMMIT_RULE, "FAILURE", ["commit"]),
            ("Run the hook test after editing a hook.", "FAILURE", ["edit-hook"]),
            ("Check the guard fixtures when touching a guard.", "FAILURE", ["edit-guard"]),
            ("Name the agent role in the description.", "FAILURE", ["agent"])])
    shell = {"session_id": "cx", "tool_name": "shell", "tool_input": {"command": ["bash", "-lc", "git commit -m x"]}}
    assert COMMIT_RULE in w.context(shell)
    execc = {"session_id": "cx", "tool_name": "exec_command", "tool_input": {"cmd": "git push origin HEAD"}}
    assert PUSH_RULE in w.context(execc)
    patch = ("*** Begin Patch\n*** Update File: harness/hooks/lessons_recall.py\n@@\n-a\n+b\n*** End Patch\n")
    ctx = w.context({"session_id": "cx", "tool_name": "apply_patch", "tool_input": {"command": patch}})
    assert ctx is not None and "Run the hook test after editing a hook." in ctx, ctx
    patch2 = "*** Begin Patch\n*** Add File: tools/path_guard.py\n+x\n*** End Patch\n"
    ctx2 = w.context({"session_id": "cx", "tool_name": "apply_patch", "tool_input": {"input": patch2}})
    assert ctx2 is not None and "Check the guard fixtures when touching a guard." in ctx2, ctx2
    spawn = {"session_id": "cx", "tool_name": "spawn_agent", "tool_input": {"agent_type": "worker", "message": "do it"}}
    assert "Name the agent role in the description." in w.context(spawn)
    plain = "*** Begin Patch\n*** Update File: docs/readme.md\n@@\n-a\n+b\n*** End Patch\n"  # twin: unrelated patch
    assert w.context({"session_id": "cx2", "tool_name": "apply_patch", "tool_input": {"command": plain}}) is None


def test_claude_edit_agent_and_pm_bridge_payloads():
    w = World("t4claude")
    w.seed([("Run the hook test after editing a hook.", "FAILURE", ["edit-hook"]),
            ("Check the guard fixtures when touching a guard.", "FAILURE", ["edit-guard"]),
            ("Name the agent role in the description.", "FAILURE", ["agent"]),
            ("Send GPT-PM the diff file, not the repo.", "FAILURE", ["gpt-pm"])])

    def edit(path, sid, tool="Edit"):
        return {"session_id": sid, "tool_name": tool, "tool_input": {"file_path": path, "old_string": "a", "new_string": "b"}}
    assert "Run the hook test" in w.context(edit("D:\\Repo\\Harness_v2\\harness\\hooks\\x_gate.py", "e1"))
    assert "Check the guard fixtures" in w.context(edit("D:/Repo/P/.github/workflows/ci.yml", "e2", "Write"))
    assert "Check the guard fixtures" in w.context(edit("/repo/tools/ledger_guard.py", "e3", "MultiEdit"))
    assert w.context(edit("D:/Repo/P/src/app.py", "e4")) is None  # twin: ordinary file
    assert "Name the agent role" in w.context({"session_id": "a1", "tool_name": "Agent", "tool_input": {"subagent_type": "code-reviewer"}})
    assert "Name the agent role" in w.context({"session_id": "a2", "tool_name": "Task", "tool_input": {"subagent_type": "x"}})
    assert "Send GPT-PM" in w.context({"session_id": "p1", "tool_name": "mcp__pm-bridge__gpt_send", "tool_input": {"message": "hi"}})
    assert w.context({"session_id": "p2", "tool_name": "mcp__pm-bridge__gpt_status", "tool_input": {}}) is None
    assert w.context({"session_id": "p3", "tool_name": "Read", "tool_input": {"file_path": "x"}}) is None


def test_at_most_five_rules_are_injected():
    w = World("t4limit")
    w.seed([(f"Push rule number {n} says check things.", "FAILURE", ["push"]) for n in range(9)])
    ctx = w.context(bash("git push", "lim"))
    assert 1 <= len(set(ids(ctx))) <= 5, ctx
    assert len(ids(ctx)) == len(set(ids(ctx)))


def test_no_action_command_reads_no_index_and_injects_nothing():
    w = World("t4noaction")
    w.seed_default()
    index = w.store / "INDEX.md"
    errlog = w.store / "errors.jsonl"
    assert index.exists() and not errlog.exists()
    index.unlink()  # a hook that looked for the index would log "INDEX.md missing while lessons.jsonl exists"
    for cmd in ("ls -la", "git status", "git log --oneline -5", "python tool.py --push-mode", "echo hello"):
        rc, out, err = w.run(bash(cmd, "na"))
        assert rc == 0 and out.strip() == "" and "Traceback" not in err, (cmd, rc, out, err)
    assert not index.exists() and not errlog.exists(), "no-action path touched the index"
    assert not (w.store / "sessions").exists() or not list((w.store / "sessions").glob("*.json"))
    # passing twin: an action does read the index (its absence is then logged), so the check above can fail
    rc, out, err = w.run(bash("git push", "na"))
    assert rc == 0 and errlog.exists() and "INDEX.md missing" in errlog.read_text(encoding="utf-8"), (out, err)


def test_call_without_session_id_injects_every_time_and_writes_no_marker():
    w = World("t4nosession")
    w.seed_default()
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "git push"}}
    assert w.context(payload) is not None and w.context(payload) is not None
    sess = w.store / "sessions"
    assert not sess.exists() or not list(sess.glob("*.json"))


def test_kill_switch_gives_exit_zero_and_no_injection():
    w = World("t4kill")
    w.seed_default()
    for val in ("off", "OFF", "Off"):
        rc, out, err = w.run(bash("git push", "k" + val), {"HARNESS_LESSONS_RECALL": val})
        assert rc == 0 and out.strip() == "", (val, out)
    for val in ("on", "", "1"):  # twin: any other value leaves the hook active
        assert w.context(bash("git push", "kk" + val), {"HARNESS_LESSONS_RECALL": val}) is not None, val
    assert not (w.store / "sessions" / "kOFF.json").exists()  # a switched-off hook does not mark anything shown


def test_missing_store_gives_exit_zero_and_no_injection():
    w = World("t4missing")
    rc, out, err = w.run(bash("git push", "m1"))
    assert rc == 0 and out.strip() == "" and "Traceback" not in err, (rc, out, err)
    w.seed_default()  # twin: once the store exists the same call injects
    assert w.context(bash("git push", "m2")) is not None


def test_corrupt_store_gives_exit_zero_and_no_injection():
    w = World("t4corrupt")
    w.store.mkdir(parents=True)
    (w.store / "lessons.jsonl").write_bytes(b"\xff\xfe{{{ not json\nstill not json\n")
    (w.store / "INDEX.md").write_text("garbage garbage\n", encoding="utf-8")
    rc, out, err = w.run(bash("git push", "c1"))
    assert rc == 0 and out.strip() == "" and "Traceback" not in err, (rc, out, err)


def test_corrupt_index_with_valid_store_exits_zero_and_either_rebuilds_or_stays_silent():
    w = World("t4corruptidx")
    w.seed_default()
    (w.store / "INDEX.md").write_bytes(b"\x00\x01 garbage \xff\n")
    rc, out, err = w.run(bash("git push", "ci1"))
    assert rc == 0 and "Traceback" not in err, (rc, out, err)
    if out.strip():  # self-healing: the rule must come from the valid jsonl and be the right one
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert PUSH_RULE in ctx and SUCCESS_RULE not in ctx, ctx
    print(f"INFO corrupt INDEX.md with valid lessons.jsonl: {'rebuilt and injected' if out.strip() else 'silent'}")


def test_store_path_that_is_a_file_exits_zero():
    w = World("t4filestore")
    w.root.mkdir(parents=True, exist_ok=True)
    w.store.write_text("not a folder", encoding="utf-8")
    rc, out, err = w.run(bash("git push", "f1"))
    assert rc == 0 and out.strip() == "", (rc, out, err)


def test_bad_stdin_never_fails():
    w = World("t4stdin")
    w.seed_default()
    for raw in ("", "   ", "not json", "[1, 2]", "null", '{"tool_name": 5, "tool_input": "x"}', '{"tool_name": "Bash", "tool_input": null}',
                '{"tool_name": "Bash", "tool_input": {"command": 7}}'):
        rc, out, err = w.run(None, raw=raw)
        assert rc == 0 and out.strip() == "" and "Traceback" not in err, (raw, rc, out, err)
    assert w.context(bash("git push", "good")) is not None  # twin


def test_session_markers_older_than_seven_days_are_removed_and_fresh_ones_kept():
    w = World("t4markers")
    w.seed_default()
    sess = w.store / "sessions"
    sess.mkdir(parents=True)
    old, fresh = sess / "old-session.json", sess / "fresh-session.json"
    old.write_text("{}", encoding="utf-8")
    fresh.write_text("{}", encoding="utf-8")
    eight_days = time.time() - 8 * 86400
    os.utime(old, (eight_days, eight_days))
    assert w.context(bash("git push", "mk")) is not None
    assert not old.exists() and fresh.exists() and (sess / "mk.json").exists()


def test_corrupt_session_marker_is_replaced_and_still_injects():
    w = World("t4badmarker")
    w.seed_default()
    sess = w.store / "sessions"
    sess.mkdir(parents=True)
    (sess / "bm.json").write_text("{{{ broken", encoding="utf-8")
    assert w.context(bash("git push", "bm")) is not None
    assert w.context(bash("git push", "bm")) is None


def _median_seconds(cmd, runs=5, inp=None, env=None):
    times = []
    for _ in range(runs):
        t = time.perf_counter()
        subprocess.run(cmd, input=inp, env=env, capture_output=True, text=True, stdin=None if inp is not None else subprocess.DEVNULL)
        times.append(time.perf_counter() - t)
    return statistics.median(times)


def test_no_action_path_is_within_100_ms_of_a_bare_python_start_on_a_500_entry_store():
    w = World("t4timing")
    w.seed([(f"Distinct timing rule number {n} about {('push', 'commit', 'agent', 'test', 'install')[n % 5]} work.", "FAILURE",
             [("push", "commit", "agent", "test", "install")[n % 5]]) for n in range(500)])
    env = h.base_env(w.env)
    payload = json.dumps(bash("ls -la", "timing"))
    _median_seconds([h.PY, "-s", "-c", "pass"], 2, env=env)  # warm the file cache
    base = _median_seconds([h.PY, "-s", "-c", "pass"], 5, env=env)
    hook = _median_seconds([h.PY, "-I", "-s", str(HOOK)], 5, inp=payload, env=env)
    act_payload = json.dumps(bash("git push", "timing-act"))
    act = _median_seconds([h.PY, "-I", "-s", str(HOOK)], 5, inp=act_payload, env=env)
    print(f"INFO timing: bare python -s -c pass median={base * 1000:.0f} ms; hook no-action median={hook * 1000:.0f} ms "
          f"(delta {1000 * (hook - base):.0f} ms, limit 100); hook action path (git push, warm index, 500 entries) median={act * 1000:.0f} ms")
    assert hook - base <= 0.100, f"no-action path {1000 * (hook - base):.0f} ms above a bare python start (limit 100 ms)"


def test_registry_lists_the_hook_for_both_clients():
    reg = json.loads((h.SRC / "harness" / "hooks" / "registry.json").read_text(encoding="utf-8"))
    items = reg if isinstance(reg, list) else reg.get("hooks", reg)
    mine = [x for x in items if isinstance(x, dict) and x.get("script") == "lessons_recall.py"]
    assert len(mine) == 1, mine
    m = mine[0]
    assert m["event"] == "PreToolUse"
    for tool in ("Bash", "Agent", "Edit", "Write", "mcp__pm-bridge__gpt_send"):
        assert tool in m["claude"], (tool, m["claude"])
    for tool in ("Bash", "apply_patch", "spawn_agent"):
        assert tool in m["codex"].split("|"), (tool, m["codex"])


def test_recall_serves_a_stale_index_as_it_is_and_never_rebuilds_it():
    w = World("t4stale")
    w.seed_default()
    index = w.store / "INDEX.md"
    before = index.read_bytes()
    new = {"id": "L0099", "date": "2026-11-14", "project": "ProjA", "kind": "FAILURE", "tags": [], "actions": ["push"],
           "rule": "Appended behind the index back.", "source": {"repo": "", "sha": ""}, "key": "k99", "idem": "i99", "seen_in": ["ProjA"]}
    p = w.store / "lessons.jsonl"
    p.write_bytes(p.read_bytes() + json.dumps(new).encode("utf-8") + b"\n")
    ctx = w.context(bash("git push", "st1"))
    assert ctx is not None and PUSH_RULE in ctx and "L0099" not in ctx, ctx
    assert index.read_bytes() == before, "the recall path rebuilt the index"
    cli = h.run([h.PY, "-I", "-s", str(CLI), "search", "--action", "push"], w.root, w.env)  # twin: the CLI refreshes
    assert "L0099" in cli.out and "L0099" in index.read_text(encoding="utf-8")


def test_recall_output_is_ascii_json_that_round_trips_a_russian_rule():
    w = World("t4ascii")
    ru = "Всегда проверяй ветку перед отправкой."
    w.seed([(ru, "FAILURE", ["push"])])
    rc, out, err = w.run(bash("git push", "ru1"))
    assert rc == 0 and out.isascii(), out
    assert ru in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    w2 = World("t4asciitwin")
    w2.seed([(PUSH_RULE, "FAILURE", ["push"])])
    rc, out, err = w2.run(bash("git push", "en1"))
    assert out.isascii() and PUSH_RULE in out


if __name__ == "__main__":
    sys.exit(h.run_all(globals()))
