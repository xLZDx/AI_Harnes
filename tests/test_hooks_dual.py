"""S3-T2 acceptance criteria 1-5: one hook file serves Claude Code and Codex CLI.

Method (black box, from the acceptance criteria, not from the diff):
  * criterion 1: the same Claude-shaped payload goes to the ORIGINAL hook (~/.claude/hooks, run from a temp copy whose
    hard-coded D:\\tmp state paths and D:\\Repo gpt root are redirected into the sandbox so no real state is touched)
    and to the ported hook in harness/hooks; decision kind and exit code must be equal, and equal to the decision the
    hook's documented contract demands (so two hooks that fail the same way cannot pass);
  * criterion 2: the same situation as a Codex payload (Bash / apply_patch / spawn_agent, Codex response_item
    transcript) must give the same decision;
  * criterion 3: every Codex stdout is checked for permissionDecision "ask" and for keys Codex rejects;
  * criterion 4: source scan for personal names/paths, machine config applied with setdefault semantics;
  * criterion 5: plan_approval_gate is Claude-only, nothing weaker than the original.
Every run uses a temp HOME and temp state folders; the kill-switch and client env vars of the caller are dropped.
"""
import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_s = importlib.util.spec_from_file_location("hv2", Path(__file__).resolve().parent / "hv2.py")
h = importlib.util.module_from_spec(_s)
sys.modules["hv2"] = h
_s.loader.exec_module(h)

NEW = h.SRC / "harness" / "hooks"
OLD = Path.home() / ".claude" / "hooks"
OUT = h.SRC / "out"
SIDES = ("old", "new", "newx")  # original (Claude payload), port (Claude payload), port (Codex payload)
DROP_ENV = re.compile(r"^(CLAUDE|CODEX|HARNESS|SESSION_|PM_BRIDGE|ANTHROPIC)")
PRE = "PreToolUse"


# ----------------------------------------------------------------------------------------------- sandbox
def _path_without_claude() -> str:
    keep = []
    for p in os.environ.get("PATH", "").split(os.pathsep):
        try:
            if any((Path(p) / n).exists() for n in ("claude", "claude.cmd", "claude.exe", "claude.ps1")):
                continue
        except OSError:
            pass
        keep.append(p)
    return os.pathsep.join(keep)


class SB:
    """One isolated world: HOME, state folders, repos, transcripts, a patched copy of the original hooks."""

    def __init__(self, name):
        self.root = h.fresh("hd_" + name)
        for d in ("home", "repos", "scratch", "cp", "pm", "nogit"):
            (self.root / d).mkdir()
        self.home, self.repos, self.scratch = self.root / "home", self.root / "repos", self.root / "scratch"
        self.cp, self.pm, self.nogit = self.root / "cp", self.root / "pm", self.root / "nogit"
        self.cfg = self.root / "cfg.json"
        self.cfg.write_text("{}", encoding="utf-8")
        self.tp_claude = self.root / ".claude" / "projects" / "p" / "t.jsonl"
        self.tp_codex = self.root / ".codex" / "sessions" / "rollout.jsonl"
        for p in (self.tp_claude, self.tp_codex):
            p.parent.mkdir(parents=True)
        self._old = None

    def st(self, side) -> Path:
        d = self.root / f"state_{side}"
        d.mkdir(exist_ok=True)
        return d

    def old_dir(self) -> Path:
        if self._old:
            return self._old
        dst = self.root / "orig_hooks"
        dst.mkdir()
        redirect = str(self.st("old"))
        pat = re.compile(r"""r?(["'])D:[\\/]+tmp((?:[\\/]+[^"'\\/\n]+)*)\1""")
        for f in OLD.glob("*.py"):
            text = f.read_text(encoding="utf-8")
            text = pat.sub(lambda m: 'r"' + redirect + m.group(2) + '"', text)
            if f.name == "gpt_review_gate.py":
                assert 'ENABLED_REPO_ROOTS = [r"D:\\Repo"]' in text, "original gpt gate changed shape"
                text = text.replace('ENABLED_REPO_ROOTS = [r"D:\\Repo"]', 'ENABLED_REPO_ROOTS = [r"%s"]' % self.repos)
            (dst / f.name).write_text(text, encoding="utf-8", newline="\n")
        self._old = dst
        return dst

    def gpt_receipts(self, side) -> Path:
        if side == "old":
            return self.st("old") / "claude_gpt_review_gate" / "receipts.jsonl"
        return self.st(side) / "gpt" / "receipts.jsonl"

    def old_state(self, *parts) -> Path:
        return self.st("old").joinpath(*parts)

    def new_state(self, side, *parts) -> Path:
        return self.st(side).joinpath("hs", *parts)

    def env(self, side, extra=None) -> dict:
        e = {k: v for k, v in os.environ.items() if not DROP_ENV.match(k)}
        st = self.st(side)
        e.update({
            "PYTHONIOENCODING": "utf-8", "GIT_TERMINAL_PROMPT": "0", "PATH": _path_without_claude(),
            "USERPROFILE": str(self.home), "HOME": str(self.home),
            "HARNESS_HOOK_STATE": str(st / "hs"), "HARNESS_HOOK_CONFIG": str(self.cfg),
            "CLAUDE_HOOK_STATE_DIR": str(st / "chs"), "PM_BRIDGE_STATE_DIR": str(self.pm),
            "CLAUDE_ROSETTA_MODE_FILE": str(self.root / "no_rosetta.json"),
            "CLAUDE_GPT_REVIEW_STATE": str(st / "gpt"), "CLAUDE_GPT_REVIEW_ROOTS": str(self.repos),
            "CLAUDE_WORKTREE_POLICY_STATE": str(st / "wt"), "CLAUDE_WORKTREE_SCRATCH_ROOT": str(self.scratch),
            "CLAUDE_REPEAT_WORK_STATE": str(st / "rw"), "CLAUDE_QUESTION_POLICY_STATE": str(st / "qp"),
            "CLAUDE_CONTROL_PLANE_HOME": str(self.cp), "SESSION_SUPERVISOR_DIR": str(st / "sup"),
        })
        for k, v in (extra or {}).items():
            if v is None:
                e.pop(k, None)
            else:
                e[k] = v
        return e

    def repo(self, name, parent=None) -> Path:
        p = (parent or self.repos) / name
        p.mkdir(parents=True)
        assert h.git(p, "init", "-q", "-b", "main").rc == 0
        h.git(p, "config", "user.name", "t")
        h.git(p, "config", "user.email", "t@t")
        h.git(p, "config", "core.autocrlf", "false")
        h.commit(p, "README.md", "hello\n", "init")
        return p


class Res:
    def __init__(self, side, cp):
        self.side, self.rc, self.out, self.err = side, cp.returncode, cp.stdout or "", cp.stderr or ""
        self.kind = kind_of(self)

    def dump(self):
        return f"[{self.side}] rc={self.rc} kind={self.kind}\n  stdout={self.out.strip()[:500]!r}\n  stderr={self.err.strip()[-500:]!r}"


def kind_of(r) -> str:
    out = r.out.strip()
    if r.rc == 2:
        return "exit2"
    if r.rc != 0:
        return f"rc{r.rc}"
    if not out:
        return "allow"
    try:
        j = json.loads(out)
    except ValueError:
        return "garbage"
    hso = j.get("hookSpecificOutput") or {}
    if j.get("decision") == "block":
        return "block"
    if hso.get("permissionDecision"):
        return hso["permissionDecision"]
    if "additionalContext" in hso:
        return "context"
    if "systemMessage" in j:
        return "message"
    return "other"


def run_hook(sb, side, hook, payload, extra_env=None, raw=None, cwd=None) -> Res:
    d = sb.old_dir() if side == "old" else NEW
    inp = raw if raw is not None else json.dumps(payload, ensure_ascii=False)
    flag = {"new": ["--harness-client=claude"], "newx": ["--harness-client=codex"]}.get(side, [])  # as build.py wires it
    cp = subprocess.run([h.PY, "-s", str(d / hook), *flag], input=inp, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", env=sb.env(side, extra_env), cwd=str(cwd or sb.root), timeout=90)
    return Res(side, cp)


def codex_out_problems(r) -> list:
    """What Codex would reject: exit codes other than 0/2, non-JSON or unsupported stdout fields, permissionDecision ask."""
    probs = []
    if r.rc not in (0, 2):
        probs.append(f"exit code {r.rc}")
    if r.rc == 2 and not r.err.strip():
        probs.append("exit 2 without a stderr reason")
    out = r.out.strip()
    if not out:
        return probs
    try:
        j = json.loads(out)
    except ValueError:
        return probs + [f"stdout is not JSON: {out[:100]!r}"]
    if not isinstance(j, dict):
        return probs + ["stdout JSON is not an object"]
    extra = set(j) - {"decision", "reason", "hookSpecificOutput"}
    if extra:
        probs.append(f"unsupported top-level keys {sorted(extra)}")
    if "decision" in j and j["decision"] != "block":
        probs.append(f"decision {j['decision']!r} is not 'block'")
    hso = j.get("hookSpecificOutput")
    if hso is not None:
        if not isinstance(hso, dict):
            probs.append("hookSpecificOutput is not an object")
        else:
            bad = set(hso) - {"hookEventName", "permissionDecision", "permissionDecisionReason", "additionalContext"}
            if bad:
                probs.append(f"unsupported hookSpecificOutput keys {sorted(bad)}")
            if "permissionDecision" in hso and hso["permissionDecision"] != "deny":
                probs.append(f"permissionDecision {hso['permissionDecision']!r} (Codex supports only deny)")
    return probs


# ----------------------------------------------------------------------------------------------- payloads
def _ts(i):
    return (datetime.now(timezone.utc) + timedelta(seconds=i)).isoformat().replace("+00:00", "Z")


CODEX_TOOL_NAME = {"AskUserQuestion": "request_user_input"}


def put_tx(sb, items):
    """items: ("user", text) ("assistant", text) ("use", tool, input) ("result", text) -> Claude and Codex transcripts."""
    cl, cx = [], []
    for i, it in enumerate(items):
        ts, k = _ts(i), it[0]
        if k == "user":
            cl.append({"type": "user", "timestamp": ts, "message": {"role": "user", "content": it[1]}})
            cx.append({"type": "response_item", "timestamp": ts, "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": it[1]}]}})
        elif k == "assistant":
            cl.append({"type": "assistant", "timestamp": ts, "message": {
                "role": "assistant", "content": [{"type": "text", "text": it[1]}]}})
            cx.append({"type": "response_item", "timestamp": ts, "payload": {
                "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": it[1]}]}})
        elif k == "use":
            cl.append({"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": f"tu{i}", "name": it[1], "input": it[2]}]}})
            cx.append({"type": "response_item", "timestamp": ts, "payload": {
                "type": "function_call", "name": CODEX_TOOL_NAME.get(it[1], it[1]),
                "arguments": json.dumps(it[2]), "call_id": f"c{i}"}})
        elif k == "result":
            cl.append({"type": "user", "timestamp": ts, "toolUseResult": it[1], "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"tu{i}", "content": it[1]}]}})
            cx.append({"type": "response_item", "timestamp": ts, "payload": {
                "type": "function_call_output", "call_id": f"c{i}", "output": it[1]}})
        else:
            raise AssertionError(k)
    sb.tp_claude.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in cl), encoding="utf-8", newline="\n")
    sb.tp_codex.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in cx), encoding="utf-8", newline="\n")


def pl(sb, event, *, tool=None, ti=None, xtool=None, xti=None, msg=None, xnull=False, prompt=None, cwd=None,
       sid="sess0001", agent_type=None, active=None, extra=None, xextra=None):
    """(claude payload, codex payload) of one situation."""
    c = {"session_id": sid, "transcript_path": str(sb.tp_claude), "cwd": str(cwd or sb.root), "hook_event_name": event}
    x = {**c, "transcript_path": str(sb.tp_codex), "turn_id": "turn-1", "model": "gpt-5.5"}
    if tool:
        c.update(tool_name=tool, tool_input=ti)
        x.update(tool_name=xtool or tool, tool_input=xti if xti is not None else ti)
    if msg is not None:
        c["last_assistant_message"] = msg
        x["last_assistant_message"] = None if xnull else msg
    if prompt is not None:
        c["prompt"] = x["prompt"] = prompt
    if agent_type:
        c["agent_type"] = x["agent_type"] = agent_type
    if active:
        c["stop_hook_active"] = x["stop_hook_active"] = True
    c.update(extra or {})
    x.update(xextra if xextra is not None else (extra or {}))
    return c, x


def trio(sb, hook, c, x, expect, expect_x=None, env=None, env_x=None):
    """Run original(Claude), port(Claude), port(Codex); all must equal the contract's decision."""
    res = {"old": run_hook(sb, "old", hook, c, env), "new": run_hook(sb, "new", hook, c, env)}
    if x is not None:
        res["newx"] = run_hook(sb, "newx", hook, x, env if env_x is None else env_x)
    dump = "\n".join(r.dump() for r in res.values())
    assert res["old"].kind == expect, f"{hook}: the ORIGINAL hook gave {res['old'].kind}, contract says {expect}\n{dump}"
    assert res["new"].kind == res["old"].kind, f"{hook}: Claude parity broken\n{dump}"
    if x is not None:
        want = expect_x or expect
        assert res["newx"].kind == want, f"{hook}: Codex payload gave {res['newx'].kind}, expected {want}\n{dump}"
        probs = codex_out_problems(res["newx"])
        assert not probs, f"{hook}: Codex output problems {probs}\n{dump}"
    return res


def bash(cmd):
    return {"command": cmd}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def write_receipt(sb, repo, **kw):
    rec = {"timestamp": now_iso(), "cwd": str(repo), "ok": True, "round": 1, **kw}
    for side in SIDES:
        p = sb.gpt_receipts(side)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")


# ----------------------------------------------------------------------------------------------- shell_policy_gate
def test_shell_policy_gate_claude_parity_and_codex_match():
    sb = SB("shell")
    for cmd, expect in (("git reset --hard HEAD~1", "deny"), ("git clean -fd", "deny"), ("git status", "allow")):
        c, x = pl(sb, PRE, tool="Bash", ti=bash(cmd))
        trio(sb, "shell_policy_gate.py", c, x, expect)
    c, _ = pl(sb, PRE, tool="PowerShell", ti=bash("git reset --hard HEAD~1"))
    trio(sb, "shell_policy_gate.py", c, None, "deny")
    c, x = pl(sb, PRE, tool="Read", ti={"file_path": "a.py"}, xtool="apply_patch", xti={"command": "*** Begin Patch\n*** End Patch\n"})
    trio(sb, "shell_policy_gate.py", c, x, "allow")


def test_shell_policy_gate_malformed_input_fails_closed_like_original():
    sb = SB("shell_bad")
    o, n = run_hook(sb, "old", "shell_policy_gate.py", None, raw="{not json"), run_hook(sb, "new", "shell_policy_gate.py", None, raw="{not json")
    assert o.rc == 2 and n.rc == 2, o.dump() + "\n" + n.dump()


# ----------------------------------------------------------------------------------------------- go_gate
PATCH = "*** Begin Patch\n*** Update File: src/a.py\n@@\n-a = 1\n+a = 2\n*** End Patch\n"


def test_go_gate_edit_needs_go_claude_parity_and_codex_apply_patch_match():
    sb = SB("go")
    put_tx(sb, [("user", "please change the value in src/a.py to 2")])
    c, x = pl(sb, PRE, tool="Edit", ti={"file_path": "src/a.py", "old_string": "1", "new_string": "2"},
              xtool="apply_patch", xti={"command": PATCH})
    r = trio(sb, "go_gate.py", c, x, "deny")
    assert "GO" in r["newx"].out or "ГО" in r["newx"].out, r["newx"].dump()
    put_tx(sb, [("user", "GO")])
    trio(sb, "go_gate.py", c, x, "allow")
    put_tx(sb, [("user", "GO"), ("assistant", "starting"), ("user", "Да все верно, делай именно так ГО")])
    trio(sb, "go_gate.py", c, x, "allow")


def test_go_gate_shell_delete_needs_go_read_only_does_not():
    sb = SB("go_sh")
    put_tx(sb, [("user", "clean the build output please")])
    for cmd, expect in (("rm -rf build", "deny"), ("git status", "allow"), ("ls -la", "allow")):
        c, x = pl(sb, PRE, tool="Bash", ti=bash(cmd))
        trio(sb, "go_gate.py", c, x, expect)
    put_tx(sb, [("user", "GO")])
    c, x = pl(sb, PRE, tool="Bash", ti=bash("rm -rf build"))
    trio(sb, "go_gate.py", c, x, "allow")


def test_go_gate_decision_log_append_is_exempt_only_for_claude_shape_and_nothing_weaker():
    sb = SB("go_dl")
    put_tx(sb, [("user", "log it")])
    log = sb.root / "core" / "DECISION_LOG.md"
    log.parent.mkdir()
    log.write_text("# log\n", encoding="utf-8")
    c, _ = pl(sb, PRE, tool="Write", ti={"file_path": str(log), "content": "# log\nentry\n"})
    trio(sb, "go_gate.py", c, None, "allow")  # append-only carve-out, same in both
    c, _ = pl(sb, PRE, tool="Write", ti={"file_path": str(log), "content": "rewritten\n"})
    trio(sb, "go_gate.py", c, None, "deny")  # non-append edit of the same file still needs GO


def test_go_gate_kill_switch_still_disables():
    sb = SB("go_ks")
    put_tx(sb, [("user", "change it")])
    c, x = pl(sb, PRE, tool="Edit", ti={"file_path": "src/a.py"}, xtool="apply_patch", xti={"command": PATCH})
    trio(sb, "go_gate.py", c, x, "allow", env={"CLAUDE_GO_GATE": "off"})


# ----------------------------------------------------------------------------------------------- decision_log_gate + reminder
def _dl_repo(sb, name):
    r = sb.repo(name)
    h.commit(r, "core/DECISION_LOG.md", "# log\n", "add log")
    (r / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert h.git(r, "add", "a.py").rc == 0
    return r


def test_decision_log_gate_commit_without_log_blocked_with_log_allowed_and_marker_written():
    sb = SB("dlg")
    r = _dl_repo(sb, "r1")
    c, x = pl(sb, PRE, tool="Bash", ti=bash('git commit -m "feat: x"'), cwd=r, sid="dlsess01")
    res = trio(sb, "decision_log_gate.py", c, x, "deny")
    assert "DECISION_LOG" in res["newx"].out, res["newx"].dump()
    c2, x2 = pl(sb, PRE, tool="Bash", ti=bash('echo "git commit -m x"'), cwd=r)
    trio(sb, "decision_log_gate.py", c2, x2, "allow")  # only mentioned in a string
    c3, x3 = pl(sb, PRE, tool="Bash", ti=bash("git status"), cwd=r)
    trio(sb, "decision_log_gate.py", c3, x3, "allow")
    for side in ("new", "newx"):
        marker = sb.new_state(side, "decision_log_gate", "session_repo", "dlsess01.json")
        assert marker.is_file(), f"{side}: session marker missing at {marker}"
        assert Path(json.loads(marker.read_text(encoding="utf-8"))["repo_root"]).resolve() == r.resolve()
    assert h.git(r, "add", "core/DECISION_LOG.md").rc == 0
    (r / "core" / "DECISION_LOG.md").write_text("# log\nentry\n", encoding="utf-8")
    assert h.git(r, "add", "core/DECISION_LOG.md").rc == 0
    trio(sb, "decision_log_gate.py", c, x, "allow")


def test_decision_log_reminder_repo_with_log_gives_context_and_session_fallback():
    sb = SB("dlr")
    r = _dl_repo(sb, "r2")
    c, x = pl(sb, "UserPromptSubmit", prompt="continue with the task", cwd=r, sid="dlsess02")
    res = trio(sb, "decision_log_reminder.py", c, x, "context")
    j = json.loads(res["newx"].out)
    assert j["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "DECISION_LOG.md" in j["hookSpecificOutput"]["additionalContext"]
    plain = sb.repo("r2plain")
    c, x = pl(sb, "UserPromptSubmit", prompt="continue with the task", cwd=plain, sid="dlsess03")
    trio(sb, "decision_log_reminder.py", c, x, "allow")  # repo without core/DECISION_LOG.md
    trio(sb, "decision_log_reminder.py", *pl(sb, "UserPromptSubmit", prompt="x", cwd=r, sid="dlsess02"), "allow",
         env={"CLAUDE_DECISION_LOG_REMINDER": "off"})
    # session-repo fallback: a commit seen by the gate in this session, then a prompt from a non-git folder
    c, x = pl(sb, PRE, tool="Bash", ti=bash('git commit -m "feat: x"'), cwd=r, sid="dlsess04")
    trio(sb, "decision_log_gate.py", c, x, "deny")
    c, x = pl(sb, "UserPromptSubmit", prompt="continue with the task", cwd=sb.nogit, sid="dlsess04")
    trio(sb, "decision_log_reminder.py", c, x, "context")
    c, x = pl(sb, "UserPromptSubmit", prompt="continue with the task", cwd=sb.nogit, sid="otherses")
    trio(sb, "decision_log_reminder.py", c, x, "allow")  # another session has no marker


# ----------------------------------------------------------------------------------------------- worktree_policy
def test_worktree_policy_pre_prompt_and_stop_flow():
    sb = SB("wt")
    r = sb.repo("wtrepo")
    sid = "wtsess01"
    c, x = pl(sb, PRE, tool="Bash", ti=bash("git worktree add -b other ../elsewhere"), cwd=r, sid=sid)
    trio(sb, "worktree_policy.py", c, x, "deny")
    wt = sb.scratch / "ok1"
    c, x = pl(sb, PRE, tool="Bash", ti=bash(f"git worktree add -b feat {wt.as_posix()}"), cwd=r, sid=sid)
    trio(sb, "worktree_policy.py", c, x, "allow")  # inside the scratch root: registered, not blocked
    for side in SIDES:
        led = sb.st(side) / "wt" / "scratch.jsonl"
        assert led.is_file() and "ok1" in led.read_text(encoding="utf-8").replace("\\", "/"), f"{side}: ledger row missing"
    c, x = pl(sb, PRE, tool="Bash", ti=bash("ssh host git worktree add /x/y"), cwd=r, sid=sid)
    trio(sb, "worktree_policy.py", c, x, "allow")  # remote hosts are not covered
    c, x = pl(sb, PRE, tool="Bash", ti=bash("git status"), cwd=r, sid=sid)
    trio(sb, "worktree_policy.py", c, x, "allow")
    assert h.git(r, "worktree", "add", "-q", "-b", "feat", str(wt)).rc == 0
    h.commit(wt, "f.txt", "f\n", "feature work")
    assert h.git(r, "merge", "-q", "--ff-only", "feat").rc == 0
    c, x = pl(sb, "UserPromptSubmit", prompt="next", cwd=r, sid=sid)
    res = trio(sb, "worktree_policy.py", c, x, "context")
    assert "ok1" in res["newx"].out
    c, x = pl(sb, "Stop", msg="all done", cwd=r, sid=sid)
    res = trio(sb, "worktree_policy.py", c, x, "block")
    assert "ok1" in res["newx"].out
    trio(sb, "worktree_policy.py", c, x, "allow")  # the same stop is blocked only once (retry)
    c, x = pl(sb, "Stop", msg="all done", cwd=r, sid="wtsess99")
    trio(sb, "worktree_policy.py", c, x, "allow")  # another session never owned the worktree


# ----------------------------------------------------------------------------------------------- gpt_review_gate
def test_gpt_review_gate_commit_and_push_receipts_and_codex_warning_goes_to_stderr():
    sb = SB("gpt")
    r = sb.repo("g1")
    c, x = pl(sb, PRE, tool="Bash", ti=bash('git commit -m "x"'), cwd=r)
    trio(sb, "gpt_review_gate.py", c, x, "deny")
    c_h, x_h = pl(sb, PRE, tool="Bash", ti=bash("git commit --dry-run"), cwd=r)
    trio(sb, "gpt_review_gate.py", c_h, x_h, "allow")
    cs, xs = pl(sb, PRE, tool="Bash", ti=bash("git status"), cwd=r)
    trio(sb, "gpt_review_gate.py", cs, xs, "allow")
    write_receipt(sb, r, ok=False, error="login expired")
    res = trio(sb, "gpt_review_gate.py", c, x, "allow")  # attempted review counts, with a warning
    for side in ("old", "new"):
        j = json.loads(res[side].out)["hookSpecificOutput"]
        assert j["permissionDecision"] == "allow" and "ok=false" in j["permissionDecisionReason"], res[side].dump()
    assert res["newx"].out.strip() == "" and "ok=false" in res["newx"].err, res["newx"].dump()  # criterion 3
    cp, xp = pl(sb, PRE, tool="Bash", ti=bash("git push origin main"), cwd=r)
    trio(sb, "gpt_review_gate.py", cp, xp, "deny")  # latest receipt is not final
    write_receipt(sb, r, final=True, ok=True)
    trio(sb, "gpt_review_gate.py", cp, xp, "allow")
    trio(sb, "gpt_review_gate.py", c, x, "allow")


def test_gpt_review_gate_stale_receipt_and_scope_and_kill_switch():
    sb = SB("gpt2")
    r = sb.repo("g2")
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    write_receipt(sb, r, timestamp=old_ts, final=True)
    c, x = pl(sb, PRE, tool="Bash", ti=bash('git commit -m "x"'), cwd=r)
    trio(sb, "gpt_review_gate.py", c, x, "deny")  # 30 h old: stale for commit (6 h)
    cp, xp = pl(sb, PRE, tool="Bash", ti=bash("git push"), cwd=r)
    trio(sb, "gpt_review_gate.py", cp, xp, "deny")  # and for push (24 h)
    trio(sb, "gpt_review_gate.py", c, x, "allow", env={"CLAUDE_GPT_REVIEW_GATE": "off"})
    outside = sb.repo("g3", parent=sb.nogit)  # not under the configured roots
    c, x = pl(sb, PRE, tool="Bash", ti=bash('git commit -m "x"'), cwd=outside)
    trio(sb, "gpt_review_gate.py", c, x, "allow")
    res = run_hook(sb, "new", "gpt_review_gate.py", c, {"CLAUDE_GPT_REVIEW_ROOTS": None})
    assert res.kind == "deny", "no roots configured must cover every repo\n" + res.dump()


def test_gpt_review_gate_transport_repo_pushes_on_fresh_receipt():
    sb = SB("gpt3")
    r = sb.repo("pm-bridge")  # the old gate special-cased a path under D:\Repo; the port matches the folder name
    cp, xp = pl(sb, PRE, tool="Bash", ti=bash("git push origin main"), cwd=r)
    assert run_hook(sb, "new", "gpt_review_gate.py", cp).kind == "deny"
    assert run_hook(sb, "newx", "gpt_review_gate.py", xp).kind == "deny"
    write_receipt(sb, r, ok=True)  # fresh, not final
    for side, payload in (("new", cp), ("newx", xp)):
        res = run_hook(sb, side, "gpt_review_gate.py", payload)
        assert res.kind == "allow", res.dump()
    r2 = sb.repo("other-repo")  # a non-transport repo with the same fresh non-final receipt must still be denied
    write_receipt(sb, r2, ok=True)
    cp2, _ = pl(sb, PRE, tool="Bash", ti=bash("git push origin main"), cwd=r2)
    assert run_hook(sb, "new", "gpt_review_gate.py", cp2).kind == "deny"


# ----------------------------------------------------------------------------------------------- repeat_work_gate
def _history(sb, name, n):
    r = sb.repo(name)
    subjects = ["alpha rewrite", "bravo tweak", "charlie fix", "delta patch", "echo adjust", "foxtrot revise",
                "golf refine", "hotel rework"]
    for i in range(n):
        (r / "a.py").write_text(f"a = {i}\n", encoding="utf-8")
        (r / "b.py").write_text(f"b = {i}\n", encoding="utf-8")
        assert h.git(r, "add", "a.py", "b.py").rc == 0
        assert h.git(r, "commit", "-q", "-m", subjects[i]).rc == 0
    (r / "a.py").write_text("a = 99\n", encoding="utf-8")
    (r / "b.py").write_text("b = 99\n", encoding="utf-8")
    assert h.git(r, "add", "a.py", "b.py").rc == 0
    return r


def test_repeat_work_gate_warns_once_then_allows_and_ignores_other_commands():
    sb = SB("rw")
    r = _history(sb, "h3", 3)
    put_tx(sb, [("user", "go on")])
    c, x = pl(sb, PRE, tool="Bash", ti=bash('git commit -m "zzzzzz yyyyyy"'), cwd=r, sid="rwsess01")
    res = trio(sb, "repeat_work_gate.py", c, x, "deny")
    assert "3 earlier commits" in res["newx"].out, res["newx"].dump()
    trio(sb, "repeat_work_gate.py", c, x, "allow")  # warned once; same files, retry passes
    cs, xs = pl(sb, PRE, tool="Bash", ti=bash("git status"), cwd=r, sid="rwsess01")
    trio(sb, "repeat_work_gate.py", cs, xs, "allow")
    r2 = _history(sb, "h2", 2)  # below WARN_AT
    c, x = pl(sb, PRE, tool="Bash", ti=bash('git commit -m "zzzzzz yyyyyy"'), cwd=r2, sid="rwsess02")
    trio(sb, "repeat_work_gate.py", c, x, "allow")


def test_repeat_work_gate_escalation_waits_for_a_new_operator_message():
    sb = SB("rw8")
    r = _history(sb, "h8", 8)
    put_tx(sb, [("user", "start the job")])
    c, x = pl(sb, PRE, tool="Bash", ti=bash('git commit -m "zzzzzz yyyyyy"'), cwd=r, sid="rwsess08")
    res = trio(sb, "repeat_work_gate.py", c, x, "deny")
    assert "plain message" in res["newx"].out and "AskUserQuestion" not in res["newx"].out  # Codex wording
    assert "AskUserQuestion" in res["new"].out
    trio(sb, "repeat_work_gate.py", c, x, "deny")  # still no new operator message: still stopped
    put_tx(sb, [("user", "start the job"), ("assistant", "asked"), ("user", "yes, try once more")])
    trio(sb, "repeat_work_gate.py", c, x, "allow")  # operator wrote: allowed and re-armed
    put_tx(sb, [("user", "start the job"), ("assistant", "asked"), ("user", "yes, try once more")])
    trio(sb, "repeat_work_gate.py", c, x, "deny")  # tripped again (n still >= 8, humans == none tripped)


def test_repeat_work_gate_survives_corrupt_state_file():
    sb = SB("rwc")
    r = _history(sb, "h3c", 3)
    for side in SIDES:
        d = sb.st(side) / "rw"
        d.mkdir(exist_ok=True)
        (d / "state.json").write_text('{"sess": {"warned": [', encoding="utf-8")  # crash half-way through a write
    c, x = pl(sb, PRE, tool="Bash", ti=bash('git commit -m "zzzzzz yyyyyy"'), cwd=r, sid="rwsess03")
    trio(sb, "repeat_work_gate.py", c, x, "deny")
    for side in SIDES:
        json.loads((sb.st(side) / "rw" / "state.json").read_text(encoding="utf-8"))  # healed to valid JSON


# ----------------------------------------------------------------------------------------------- Stop hooks
def test_question_policy_stop_blocks_prose_question_once_unless_question_tool_used():
    sb = SB("qp")
    msg = "I finished the analysis of both caches.\nWhich cache should I use for the session store?"
    put_tx(sb, [("user", "analyze the caches"), ("assistant", msg)])
    c, x = pl(sb, "Stop", msg=msg)
    res = trio(sb, "question_policy.py", c, x, "block")
    assert json.loads(res["new"].out)["reason"] != json.loads(res["newx"].out)["reason"]  # Codex wording differs
    trio(sb, "question_policy.py", c, x, "allow")  # same message is blocked only once (retry)
    msg2 = "Done with the analysis.\nShall I proceed with the migration now?"
    put_tx(sb, [("user", "analyze"), ("use", "AskUserQuestion", {"questions": []}), ("result", "ok"), ("assistant", msg2)])
    c, x = pl(sb, "Stop", msg=msg2)
    trio(sb, "question_policy.py", c, x, "allow")  # the question tool was used this turn
    c, x = pl(sb, "Stop", msg="Everything is finished.")
    trio(sb, "question_policy.py", c, x, "allow")  # no question
    c, x = pl(sb, "Stop", msg="Which one should I use?", agent_type="code-reviewer")
    trio(sb, "question_policy.py", c, x, "allow")  # subagents never blocked


def test_question_policy_codex_reply_with_recommended_option_is_compliant():
    sb = SB("qp2")
    msg = "Two options:\n1. Redis (Recommended) - fastest, needs a server\n2. In-memory - no server\nWhich one do you want?"
    put_tx(sb, [("user", "pick a cache"), ("assistant", msg)])
    c, x = pl(sb, "Stop", msg=msg)
    trio(sb, "question_policy.py", c, x, "block", expect_x="allow")


def test_evidence_gate_citations_claude_parity_and_codex_transcript_fallback():
    sb = SB("ev")
    (sb.root / "src").mkdir()
    (sb.root / "src" / "real.py").write_text("a\nb\nc\n", encoding="utf-8")
    cases = (("The bug is in src/nowhere/missing.py:10 and breaks the build.", "block"),
             ("The bug is in src/real.py:99 and breaks the build.", "block"),
             ("The bug is in src/real.py:2 and breaks the build.", "allow"),
             ("No citations at all, only an opinion about the design.", "allow"))
    for msg, expect in cases:
        put_tx(sb, [("user", "review"), ("assistant", msg)])
        c, x = pl(sb, "SubagentStop", msg=msg, xnull=True, agent_type="code-reviewer")
        trio(sb, "evidence_gate.py", c, x, expect)  # Codex: last_assistant_message null, read from the transcript
    put_tx(sb, [("user", "review"), ("assistant", cases[0][0])])
    c, x = pl(sb, "SubagentStop", msg=cases[0][0], agent_type="code-reviewer", active=True)
    trio(sb, "evidence_gate.py", c, x, "allow")  # loop guard
    c, x = pl(sb, "SubagentStop", msg=cases[0][0], agent_type="code-reviewer")
    trio(sb, "evidence_gate.py", c, x, "allow", env={"CLAUDE_CITATION_GATE": "off"})


def test_evidence_gate_skips_subagents_listed_in_evidence_skip_json():
    sb = SB("evw")
    import shutil
    d = sb.root / "hooks_copy"
    shutil.copytree(NEW, d, ignore=shutil.ignore_patterns("__pycache__"))
    (d / "evidence_skip.json").write_text('["implementation-engineer"]', encoding="utf-8")
    bad = "The bug is in src/nowhere/missing.py:10 and breaks the build."
    for agent, expect in (("implementation-engineer", "allow"), ("code-reviewer", "block")):
        c, _ = pl(sb, "SubagentStop", msg=bad, agent_type=agent)
        cp = subprocess.run([h.PY, "-s", str(d / "evidence_gate.py")], input=json.dumps(c), capture_output=True, text=True,
                            encoding="utf-8", env=sb.env("new"), cwd=str(sb.root), timeout=60)
        assert kind_of(Res("new", cp)) == expect, (agent, cp.stdout, cp.stderr)


EVID_ITEMS = [("user", "check the build"), ("assistant", "running"), ("use", "Bash", {"command": "cat v.txt"}),
              ("result", "build exit code 0 all green")]


def test_unverified_claim_gate_requires_quoted_evidence_from_this_turn():
    sb = SB("uc")
    good = "Everything works now and the build is verified.\nEvidence: `build exit code 0 all green`"
    fake = "Everything works now and the build is verified.\nEvidence: `build exit code 7 all red`"
    noline = "Everything works now and the build is verified."
    for msg, expect in ((good, "allow"), (fake, "block"), (noline, "block")):
        put_tx(sb, EVID_ITEMS + [("assistant", msg)])
        c, x = pl(sb, "Stop", msg=msg)
        res = trio(sb, "unverified_claim_gate.py", c, x, expect)
        if expect == "block":
            assert "UNPROVEN ANSWER" in res["newx"].out
    put_tx(sb, [("user", "is it done?"), ("assistant", good)])
    c, x = pl(sb, "Stop", msg=good)
    res = trio(sb, "unverified_claim_gate.py", c, x, "block")  # a claim in a turn with no tool call at all
    assert "no tool call" in res["newx"].out
    put_tx(sb, EVID_ITEMS + [("assistant", "ok")])
    c, x = pl(sb, "Stop", msg="ok")
    trio(sb, "unverified_claim_gate.py", c, x, "allow")  # short, no claim word
    put_tx(sb, EVID_ITEMS + [("assistant", noline)])
    c, x = pl(sb, "Stop", msg=noline, active=True)
    trio(sb, "unverified_claim_gate.py", c, x, "allow")  # stop_hook_active
    c, x = pl(sb, "Stop", msg=noline)
    trio(sb, "unverified_claim_gate.py", c, x, "allow", env={"CLAUDE_UNVERIFIED_CLAIM_GATE": "off"})


def test_unverified_claim_gate_codex_null_message_read_from_transcript():
    sb = SB("uc2")
    noline = "Everything works now and the build is verified."
    put_tx(sb, EVID_ITEMS + [("assistant", noline)])
    c, x = pl(sb, "Stop", msg=noline, xnull=True)
    trio(sb, "unverified_claim_gate.py", c, x, "block")


def test_decision_explain_gate_unexplained_refs_blocked():
    sb = SB("de")
    pad = " ok" * 90
    bad = "We should move the work to the other branch (D-2)." + pad
    good = "We should move the work to the other branch (D-2: where to merge - phase2 is safer)." + pad
    for msg, expect in ((bad, "block"), (good, "allow"), ("No references here at all." + pad, "allow"),
                        ("short (D-2)", "allow")):
        put_tx(sb, [("user", "plan"), ("assistant", msg)])
        c, x = pl(sb, "Stop", msg=msg, xnull=True)
        trio(sb, "decision_explain_gate.py", c, x, expect)
    c, x = pl(sb, "Stop", msg=bad, agent_type="code-reviewer")
    trio(sb, "decision_explain_gate.py", c, x, "allow")


DEFER = "Next steps: I recommend that we continue with the remaining work later, after the review of this change. "


def test_keep_going_gate_blocks_deferral_until_closing_line():
    sb = SB("kg")
    cases = ((DEFER * 2, "block"),
             (DEFER * 2 + "\nВыполнимых задач не осталось", "allow"),
             (DEFER * 2 + "\nБлокеры: OPERATOR - needs your decision on the key", "allow"),
             (DEFER * 2 + " All done: committed and pushed to origin.", "block"),  # the original blocks it too
             ("remaining: nothing much", "allow"),
             ("Plain status update. " * 25, "block"))  # 400+ chars without a closing line
    for i, (msg, expect) in enumerate(cases):
        put_tx(sb, [("user", "work"), ("assistant", msg)])
        c, x = pl(sb, "Stop", msg=msg, xnull=True, sid=f"kgsess{i:02d}")
        trio(sb, "keep_going_gate.py", c, x, expect)
    c, x = pl(sb, "Stop", msg=DEFER * 2, agent_type="code-reviewer")
    trio(sb, "keep_going_gate.py", c, x, "allow")
    c, x = pl(sb, "Stop", msg=DEFER * 2)
    trio(sb, "keep_going_gate.py", c, x, "allow", env={"CLAUDE_KEEP_GOING_GATE": "off"})


def test_keep_going_gate_safety_valve_after_eight_consecutive_blocks_and_corrupt_counter():
    sb = SB("kg2")
    msg = DEFER * 2
    put_tx(sb, [("user", "work"), ("assistant", msg)])
    c, x = pl(sb, "Stop", msg=msg, sid="kgvalve1")
    seq = {s: [] for s in SIDES}
    for _ in range(10):
        for s, payload in (("old", c), ("new", c), ("newx", x)):
            seq[s].append(run_hook(sb, s, "keep_going_gate.py", payload).kind)
    want = ["block"] * 8 + ["allow"] * 2
    assert seq["old"] == want, seq
    assert seq["new"] == want and seq["newx"] == want, seq
    # a counter file torn by a crash must not crash or release the gate
    sb2 = SB("kg3")
    put_tx(sb2, [("user", "work"), ("assistant", msg)])
    for s in SIDES:
        d = sb2.old_state("claude_citation_gate") if s == "old" else sb2.new_state(s, "citation_gate")
        d.mkdir(parents=True, exist_ok=True)
        (d / "keep_going_counts.json").write_text('{"kgsess": 3, "ot', encoding="utf-8")
    c, x = pl(sb2, "Stop", msg=msg, sid="kgtorn01")
    trio(sb2, "keep_going_gate.py", c, x, "block")


def test_keep_going_gate_parallel_sessions_all_decide_and_counter_file_stays_valid_json():
    sb = SB("kgpar")
    msg = DEFER * 2
    put_tx(sb, [("user", "work"), ("assistant", msg)])
    procs = []
    for i in range(6):
        c, _ = pl(sb, "Stop", msg=msg, sid=f"par{i:05d}")
        procs.append(subprocess.Popen([h.PY, "-s", str(NEW / "keep_going_gate.py")], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=sb.env("new"), cwd=str(sb.root)))
        procs[-1].stdin.write(json.dumps(c).encode("utf-8"))
        procs[-1].stdin.close()
    outs = [(p.stdout.read().decode("utf-8", "replace"), p.wait()) for p in procs]
    assert all(rc == 0 and '"decision": "block"' in o for o, rc in outs), outs
    counts = json.loads(sb.new_state("new", "citation_gate", "keep_going_counts.json").read_text(encoding="utf-8"))
    print(f"INFO parallel keep_going: counter keys surviving = {len(counts)}/6 (lost updates are not asserted)")


# ----------------------------------------------------------------------------------------------- citation_judge
def test_citation_judge_without_judge_command_passes():
    sb = SB("cj")
    (sb.root / "src").mkdir()
    (sb.root / "src" / "real.py").write_text("a\nb\nc\n", encoding="utf-8")
    msg = "The function in src/real.py:2 returns the second value as claimed."
    put_tx(sb, [("user", "review"), ("assistant", msg)])
    c, x = pl(sb, "SubagentStop", msg=msg, agent_type="code-reviewer", xextra={"turn_id": "t9"})
    for payload, side in ((c, "new"), (x, "newx")):
        res = run_hook(sb, side, "citation_judge.py", payload, {"HARNESS_JUDGE_CMD": None})
        assert res.kind == "allow" and "Traceback" not in res.err, res.dump()
        assert not codex_out_problems(res)
        log = sb.new_state(side, "citation_judge", "decisions.jsonl")
        assert log.is_file() and json.loads(log.read_text(encoding="utf-8").splitlines()[-1])["verdict"] == "pass", side
    res = run_hook(sb, "new", "citation_judge.py", c, {"HARNESS_JUDGE_CMD": str(sb.root / "no_such_judge.exe")})
    assert res.kind == "allow" and res.rc == 0, res.dump()  # a broken judge command fails open
    res = run_hook(sb, "new", "citation_judge.py", pl(sb, "Stop", msg=msg)[0])
    assert res.kind == "allow", res.dump()  # only SubagentStop is judged


# ----------------------------------------------------------------------------------------------- agent_model_gate
def _agent(model=None, name="helper"):
    ti = {"subagent_type": name, "prompt": "do it", "description": "d"}
    if model:
        ti["model"] = model
    return ti


def test_agent_model_gate_claude_parity():
    sb = SB("am")
    proj = sb.root / "proj"
    (proj / ".claude" / "agents").mkdir(parents=True)
    (proj / ".claude" / "agents" / "opusy.md").write_text("---\nname: opusy\nmodel: opus\n---\nbody\n", encoding="utf-8")
    (proj / ".claude" / "agents" / "old.md").write_text("---\nname: old\nmodel: sonnet\ndeprecated: true\n---\nb\n", encoding="utf-8")
    (proj / ".claude" / "agents" / "good.md").write_text("---\nname: good\nmodel: sonnet\neffort: medium\n---\nb\n", encoding="utf-8")
    (proj / ".claude" / "agents" / "badeff.md").write_text("---\nname: badeff\nmodel: sonnet\neffort: extreme\n---\nb\n", encoding="utf-8")
    cases = [(_agent("opus"), {}, "deny"), (_agent("haiku"), {}, "deny"), (_agent("claude-sonnet-5-4"), {}, "deny"),
             (_agent("sonnet"), {}, "allow"), (_agent("claude-sonnet-5-5"), {}, "allow"),
             (_agent("claude-sonnet-5-5-20261001"), {}, "allow"),
             (_agent(None, "unknown-agent"), {}, "deny"),  # inherits an unknown model: fail closed
             (_agent(None, "unknown-agent"), {"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"}, "allow"),
             (_agent(None, "opusy"), {}, "deny"), (_agent(None, "old"), {}, "deny"),
             (_agent(None, "good"), {}, "allow"), (_agent(None, "badeff"), {}, "deny")]
    for ti, env, expect in cases:
        c, _ = pl(sb, PRE, tool="Agent", ti=ti, cwd=proj)
        trio(sb, "agent_model_gate.py", c, None, expect, env=env)
    c, _ = pl(sb, PRE, tool="Bash", ti=bash("git status"), cwd=proj)
    trio(sb, "agent_model_gate.py", c, None, "allow")
    o = run_hook(sb, "old", "agent_model_gate.py", None, raw="{broken")
    n = run_hook(sb, "new", "agent_model_gate.py", None, raw="{broken")
    assert o.rc == 2 and n.rc == 2, o.dump() + n.dump()


def test_agent_model_gate_codex_spawn_agent_with_opus_model_is_denied():
    """Operator rule 2026-11-11: Codex subagents run only on the latest Luna model (gpt-6-luna) at medium effort.
    Claude keeps denying the Opus subagent; Codex denies any other model or effort and passes the allowed pair."""
    sb = SB("am2")
    c, x = pl(sb, PRE, tool="Agent", ti=_agent("opus"), xtool="spawn_agent",
              xti={"agent_type": "helper", "message": "do it", "model": "opus"})
    trio(sb, "agent_model_gate.py", c, x, "deny")
    for model, effort, want in (("gpt-6-luna", "medium", "allow"), ("gpt-5.6-luna", "medium", "deny"),
                                ("gpt-6-luna", "high", "deny"), ("gpt-6-sol", "medium", "deny"),
                                ("", "", "exit_or_deny")):  # nothing establishes model/effort: fail closed
        ti = {"agent_type": "helper", "message": "do it"}
        if model:
            ti["model"] = model
        if effort:
            ti["reasoning_effort"] = effort
        _, x2 = pl(sb, PRE, tool="Agent", ti=_agent("sonnet"), xtool="spawn_agent", xti=ti)
        r = run_hook(sb, "newx", "agent_model_gate.py", x2, {"CODEX_HOME": str(sb.root / "empty_codex_home")})
        ok = r.kind in ("deny", "exit2") if want == "exit_or_deny" else r.kind == want
        assert ok, f"model={model!r} effort={effort!r}: got {r.kind}, expected {want}\n{r.dump()}"
    reg = json.loads((NEW / "registry.json").read_text(encoding="utf-8"))
    assert any(e["script"] == "agent_model_gate.py" and e.get("codex") == "spawn_agent" for e in reg["hooks"])


def _codex_spawn(sb, xti, toml=None, name="helper"):
    """Run the Codex spawn_agent payload with an empty CODEX_HOME; optional agent TOML under cwd/.codex/agents."""
    proj = sb.root / "cproj"
    proj.mkdir(exist_ok=True)
    home = sb.root / "empty_codex_home"
    home.mkdir(exist_ok=True)
    if toml is not None:
        (proj / ".codex" / "agents").mkdir(parents=True, exist_ok=True)
        (proj / ".codex" / "agents" / f"{name}.toml").write_text(toml, encoding="utf-8", newline="\n")
    _, x = pl(sb, PRE, tool="Agent", ti=_agent("sonnet"), xtool="spawn_agent", xti=xti, cwd=proj)
    return run_hook(sb, "newx", "agent_model_gate.py", x, {"CODEX_HOME": str(home)})


def test_agent_model_gate_codex_fails_closed_when_model_or_effort_cannot_be_established():
    blocked = ("deny", "exit2")
    base = {"agent_type": "helper", "message": "do it"}
    # omitted model and effort, no TOML anywhere
    r = _codex_spawn(SB("amc1"), dict(base))
    assert r.kind in blocked, r.dump()
    # unknown agent type, nothing declared
    r = _codex_spawn(SB("amc2"), {"agent_type": "no-such-agent", "message": "m"})
    assert r.kind in blocked, r.dump()
    # explicit policy pair is allowed
    r = _codex_spawn(SB("amc3"), {**base, "model": "gpt-6-luna", "reasoning_effort": "medium"})
    assert r.kind == "allow", r.dump()
    # TOML with luna/medium, no explicit values: allowed
    ok_toml = 'name = "helper"\nmodel = "gpt-6-luna"\nmodel_reasoning_effort = "medium"\n'
    r = _codex_spawn(SB("amc4"), dict(base), toml=ok_toml)
    assert r.kind == "allow", r.dump()
    # TOML lacking model (effort only), no explicit model: cannot establish model
    r = _codex_spawn(SB("amc5"), dict(base), toml='name = "helper"\nmodel_reasoning_effort = "medium"\n')
    assert r.kind in blocked, r.dump()
    # TOML lacking effort, explicit model only: effort open
    r = _codex_spawn(SB("amc5b"), {**base, "model": "gpt-6-luna"}, toml='name = "helper"\nmodel = "gpt-6-luna"\n')
    assert r.kind in blocked, r.dump()
    # model given, effort missing, no TOML
    r = _codex_spawn(SB("amc6"), {**base, "model": "gpt-6-luna"})
    assert r.kind in blocked, r.dump()
    # effort given, model missing, no TOML
    r = _codex_spawn(SB("amc6b"), {**base, "reasoning_effort": "medium"})
    assert r.kind in blocked, r.dump()
    # wrong model with correct effort
    r = _codex_spawn(SB("amc8"), {**base, "model": "gpt-5.5", "reasoning_effort": "medium"})
    assert r.kind in blocked, r.dump()
    # TOML declaring another model is denied even when the call is silent
    r = _codex_spawn(SB("amc9"), dict(base), toml='model = "gpt-5.5"\nmodel_reasoning_effort = "medium"\n')
    assert r.kind in blocked, r.dump()


def test_agent_model_gate_codex_non_object_tool_input_is_blocked():
    for i, bad in enumerate(("just a string", ["agent_type", "helper"], 5)):
        r = _codex_spawn(SB(f"amn{i}"), bad)
        assert r.kind in ("deny", "exit2"), f"tool_input={bad!r}\n{r.dump()}"
    # a Codex payload with tool_input absent entirely must not pass either
    sb = SB("amn9")
    _, x = pl(sb, PRE, tool="Agent", ti=_agent("sonnet"), xtool="spawn_agent", xti={})
    del x["tool_input"]
    r = run_hook(sb, "newx", "agent_model_gate.py", x, {"CODEX_HOME": str(sb.root / "empty_codex_home")})
    assert r.kind in ("deny", "exit2"), r.dump()


def test_agent_model_gate_claude_payload_is_not_taken_for_codex_when_codex_home_is_set():
    """A Claude payload must stay a Claude payload even when CODEX_HOME exists in the environment
    (this machine runs both clients); otherwise the Opus gate silently turns into a no-op."""
    sb = SB("am3")
    other = sb.root / "claude_cfg" / "projects" / "t.jsonl"  # CLAUDE_CONFIG_DIR elsewhere: no '/.claude/' in the path
    other.parent.mkdir(parents=True)
    other.write_text("", encoding="utf-8")
    for tp in ("", str(other)):
        c, _ = pl(sb, PRE, tool="Agent", ti=_agent("opus"))
        c["transcript_path"] = tp
        res = run_hook(sb, "new", "agent_model_gate.py", c, {"CODEX_HOME": str(sb.root / "codex_home")})
        assert res.kind == "deny", f"transcript_path={tp!r}: Opus subagent allowed when CODEX_HOME is set\n" + res.dump()


# ----------------------------------------------------------------------------------------------- plan_approval_gate (Claude only)
def test_plan_approval_gate_is_claude_only_and_not_weaker():
    sb = SB("pa")
    put_tx(sb, [("user", "make a plan")])
    c, x = pl(sb, PRE, tool="ExitPlanMode", ti={"plan": "p"}, extra={"permission_mode": "bypassPermissions"})
    # a Codex-shaped payload that still says ExitPlanMode cannot occur (Codex has no such tool and the hook is not
    # registered there); the gate decides by tool_name, so it denies exactly like Claude: never weaker
    trio(sb, "plan_approval_gate.py", c, x, "deny")
    put_tx(sb, [("user", "GO")])
    trio(sb, "plan_approval_gate.py", c, x, "allow")
    put_tx(sb, [("user", "make a plan")])
    c, x = pl(sb, PRE, tool="ExitPlanMode", ti={"plan": "p"}, extra={"permission_mode": "default"})
    trio(sb, "plan_approval_gate.py", c, x, "allow")
    reg = json.loads((NEW / "registry.json").read_text(encoding="utf-8"))
    pa = [e for e in reg["hooks"] if e["script"] == "plan_approval_gate.py"]
    assert pa and all("codex" not in e for e in pa) and any(e.get("claude") == "ExitPlanMode" for e in pa), pa


# ----------------------------------------------------------------------------------------------- criterion 3 sweep
def test_codex_output_sweep_over_every_codex_registry_entry():
    sb = SB("sweep")
    r = _dl_repo(sb, "swrepo")
    (sb.root / "src").mkdir()
    (sb.root / "src" / "real.py").write_text("a\nb\nc\n", encoding="utf-8")
    long_msg = ("We should merge it (D-1) and I recommend the next steps later. Which database should I use? "
                "It works and src/nowhere/gone.py:5 is the cause. " * 4)
    put_tx(sb, [("user", "go"), ("use", "Bash", {"command": "ls"}), ("result", "x"), ("assistant", long_msg)])
    reg = json.loads((NEW / "registry.json").read_text(encoding="utf-8"))
    problems, ran = [], 0
    for e in reg["hooks"]:
        if "codex" not in e:
            continue
        ev = e["event"]
        base = {"session_id": "sw000001", "transcript_path": str(sb.tp_codex), "cwd": str(r), "hook_event_name": ev,
                "turn_id": "turn-9", "model": "gpt-5.5"}
        variants = []
        if ev == "PreToolUse":
            variants = [{"tool_name": "Bash", "tool_input": bash('git commit -m "x"')},
                        {"tool_name": "Bash", "tool_input": bash("git reset --hard")},
                        {"tool_name": "Bash", "tool_input": bash("git worktree add ../nowhere")},
                        {"tool_name": "apply_patch", "tool_input": {"command": PATCH}},
                        {"tool_name": "spawn_agent", "tool_input": {"agent_type": "worker", "message": "x", "model": "opus"}}]
        elif ev in ("Stop", "SubagentStop"):
            variants = [{"last_assistant_message": long_msg}, {"last_assistant_message": None},
                        {"last_assistant_message": long_msg, "agent_type": "code-reviewer"}]
        elif ev == "UserPromptSubmit":
            variants = [{"prompt": "continue with the whole task please, step by step"}]
        elif ev == "PostToolUse":
            variants = [{"tool_name": "Bash", "tool_input": bash("gh pr create --title x"), "tool_response": {"output": "ok"}}]
        else:
            variants = [{}]
        for v in variants:
            res = run_hook(sb, "newx", e["script"], {**base, **v}, {"CLAUDE_GPT_REVIEW_ROOTS": None})
            ran += 1
            probs = codex_out_problems(res)
            if probs:
                problems.append(f"{ev}/{e['script']}/{v.get('tool_name') or list(v)[:1]}: {probs}\n{res.dump()}")
    assert ran >= 20, ran
    assert not problems, "\n".join(problems)


def test_gpt_gate_allow_warning_never_emits_permission_allow_for_codex_and_no_ask_anywhere():
    sb = SB("noask")
    r = sb.repo("g9")
    write_receipt(sb, r, ok=False, error="x")
    _, x = pl(sb, PRE, tool="Bash", ti=bash('git commit -m "x"'), cwd=r)
    res = run_hook(sb, "newx", "gpt_review_gate.py", x)
    assert '"ask"' not in res.out and "permissionDecision" not in res.out, res.dump()
    for f in sorted(NEW.glob("*.py")):  # no hook source can even build an ask decision
        assert not re.search(r"""permissionDecision["']\s*:\s*["']ask""", f.read_text(encoding="utf-8")), f.name


def test_precompact_in_codex_mode_writes_stderr_not_system_message():
    sb = SB("pc")
    r = _dl_repo(sb, "pcrepo")
    c, x = pl(sb, "PreCompact", cwd=r)
    res = trio(sb, "decision_log_precompact.py", c, x, "message", expect_x="allow")  # Claude: systemMessage; Codex: silent stdout
    assert "PreCompact" in res["newx"].err and "systemMessage" not in res["newx"].out, res["newx"].dump()


# ----------------------------------------------------------------------------------------------- criterion 4
CODE_PATTERNS = (re.compile(r"koros", re.I), re.compile(r"C:[\\/]+Users", re.I))


def test_no_personal_name_or_user_path_in_any_hook_source():
    hits = []
    for f in sorted(NEW.glob("*")):
        if f.suffix not in (".py", ".json"):
            continue
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for p in CODE_PATTERNS:
                if p.search(line):
                    hits.append(f"{f.name}:{n}: {line.strip()[:100]}")
    assert not hits, "\n".join(hits)


def test_no_hard_coded_drive_path_in_hook_string_literals():
    hits = []
    for f in sorted(NEW.glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        doc_nodes = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)) and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant):
                    doc_nodes.add(id(first.value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in doc_nodes:
                if re.search(r"(?<![A-Za-z])[A-Za-z]:[\\/]", node.value) and not re.fullmatch(r"[\s\S]{0,3}", node.value):
                    hits.append(f"{f.name}:{node.lineno}: {node.value[:80]!r}")
    # drive letters inside regex/format helpers (e.g. "C:" scheme tests) are listed, not hidden
    assert not hits, "\n".join(hits)


def _py(code, env):
    cp = subprocess.run([h.PY, "-s", "-c", code], capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    assert cp.returncode == 0, cp.stderr
    return json.loads(cp.stdout)


def test_load_machine_config_sets_defaults_and_never_overrides_a_real_env_var():
    sb = SB("cfg")
    cfg = sb.root / "machine.json"
    cfg.write_bytes(b"\xef\xbb\xbf" + json.dumps({
        "_comment": "ignored", "HV2_FROM_CFG": "cfg-value", "HV2_REAL_WINS": "cfg-loses", "lower_case": "x",
        "HV2_NOT_A_STRING": 5, "HV2_TILDE": "~/sub", "HV2_VAR": "%HV2_BASE%/z"}).encode("utf-8"))
    env = sb.env("new", {"HARNESS_HOOK_CONFIG": str(cfg), "HV2_REAL_WINS": "env-wins", "HV2_BASE": "base"})
    code = ("import sys,os,json; sys.path.insert(0, %r); import _agentio; "
            "print(json.dumps({k: os.environ.get(k) for k in ['HV2_FROM_CFG','HV2_REAL_WINS','lower_case','HV2_NOT_A_STRING','HV2_TILDE','HV2_VAR','_comment']}))") % str(NEW)
    got = _py(code, env)
    assert got["HV2_FROM_CFG"] == "cfg-value"
    assert got["HV2_REAL_WINS"] == "env-wins"
    assert got["lower_case"] is None and got["HV2_NOT_A_STRING"] is None and got["_comment"] is None
    assert got["HV2_TILDE"] == str(Path(sb.home) / "sub") or got["HV2_TILDE"].replace("\\", "/") == str(sb.home).replace("\\", "/") + "/sub"
    assert got["HV2_VAR"] == "base/z" or got["HV2_VAR"] == "%HV2_BASE%/z" and os.name != "nt", got
    for bad in (b"{broken", b"[1,2]", b""):  # unreadable or wrong-shape config never breaks a hook
        cfg.write_bytes(bad)
        got = _py(code, env)
        assert got["HV2_REAL_WINS"] == "env-wins"
    cfg.unlink()
    assert _py(code, env)["HV2_REAL_WINS"] == "env-wins"


def test_machine_config_values_equal_the_old_hard_coded_ones():
    real = Path.home() / ".harness-hooks" / "config.json"
    if not real.is_file():
        print("SKIP machine config not installed on this machine")
        return
    cfg = json.loads(real.read_text(encoding="utf-8-sig"))
    old_gpt = (OLD / "gpt_review_gate.py").read_text(encoding="utf-8")
    old_wt = (OLD / "worktree_policy.py").read_text(encoding="utf-8")

    def norm(p):
        return os.path.normcase(os.path.normpath(os.path.expandvars(os.path.expanduser(str(p)))))

    state = re.search(r'STATE_DIR = Path\(r"([^"]+)"\)', old_gpt).group(1)
    root = re.search(r'ENABLED_REPO_ROOTS = \[r"([^"]+)"\]', old_gpt).group(1)
    scratch = re.search(r'"CLAUDE_WORKTREE_SCRATCH_ROOT", r"([^"]+)"', old_wt).group(1)
    assert norm(cfg["CLAUDE_GPT_REVIEW_STATE"]) == norm(state)
    assert norm(cfg["CLAUDE_GPT_REVIEW_ROOTS"]) == norm(root)
    assert norm(cfg["CLAUDE_WORKTREE_SCRATCH_ROOT"]) == norm(scratch)
    pm = re.search(r'Path\(r"([^"]+pm-bridge\\state)"\)', (OLD / "_report_common.py").read_text(encoding="utf-8")).group(1)
    assert norm(cfg["PM_BRIDGE_STATE_DIR"]) == norm(pm)
    for k in ("CLAUDE_GPT_REVIEW_CLI", "SESSION_CLOSE_PLAN_TOOL", "SESSION_SUPERVISOR_ROOTS"):
        assert cfg.get(k), k


def test_state_dir_follows_harness_hook_state_override():
    sb = SB("sd")
    code = ("import sys,json; sys.path.insert(0, %r); import _agentio; print(json.dumps(str(_agentio.state_dir('xyz'))))") % str(NEW)
    got = _py(code, sb.env("new"))
    assert Path(got) == sb.st("new") / "hs" / "xyz" and Path(got).is_dir(), got
    got = _py(code, sb.env("new", {"HARNESS_HOOK_STATE": None}))
    assert Path(got) == sb.home / ".harness-hooks" / "xyz", got


# ----------------------------------------------------------------------------------------------- build output
def _expected_from_registry(client):
    reg = json.loads((NEW / "registry.json").read_text(encoding="utf-8"))
    return sorted((e["event"], e[client], e["script"], e["timeout"], e["status"]) for e in reg["hooks"] if client in e)


def _flatten(cfg, client):
    got = []
    for event, groups in cfg["hooks"].items():
        for g in groups:
            for hk in g["hooks"]:
                if client == "claude":
                    script = [Path(a).name for a in hk["args"] if a.endswith(".py")][-1]
                    assert f"--harness-client={client}" in hk["args"], hk
                else:
                    script = re.findall(r"[\w]+\.py\b", hk["command"])[-1]
                    assert f"--harness-client={client}" in hk["command"], hk
                got.append((event, g.get("matcher", ""), script, hk["timeout"], hk["statusMessage"]))
    return sorted(got)


def test_built_hook_files_contain_every_registry_entry_with_its_matcher():
    for client, path in (("claude", OUT / "claude" / "settings.hooks.json"), ("codex", OUT / "codex" / "hooks.json")):
        cfg = json.loads(path.read_text(encoding="utf-8"))
        assert _flatten(cfg, client) == _expected_from_registry(client), f"{path} differs from registry.json"
    cx = json.loads((OUT / "codex" / "hooks.json").read_text(encoding="utf-8"))
    text = json.dumps(cx)
    for forbidden in ("Notification", "ExitPlanMode", "AskUserQuestion", "PermissionRequest", "plan_approval_gate"):
        assert forbidden not in text, f"codex hooks.json must not mention {forbidden}"
    assert any(g.get("matcher") == "Bash|apply_patch" for g in cx["hooks"]["PreToolUse"])
    assert any(g.get("matcher") == "spawn_agent" for g in cx["hooks"]["PreToolUse"])
    cl = json.loads((OUT / "claude" / "settings.hooks.json").read_text(encoding="utf-8"))
    cl_text = json.dumps(cl)
    for needed in ("Notification", "ExitPlanMode", "AskUserQuestion", "plan_approval_gate"):
        assert needed in cl_text, f"claude settings must keep {needed}"
    assert any(g.get("matcher") == "Agent" for g in cl["hooks"]["PreToolUse"])


def test_build_hook_config_function_matches_registry_for_both_clients():
    spec = importlib.util.spec_from_file_location("hv2_build", h.TOOLS / "build.py")
    b = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(b)
    assert b.check_hooks() == []
    for client in ("claude", "codex"):
        cfg = {"hooks": b.hook_config(client, Path("X:/hooks"), "py.exe")}
        assert _flatten(cfg, client) == _expected_from_registry(client), client
    assert "Notification" not in b.hook_config("codex", Path("X:/hooks"), "py.exe")


def test_built_hook_scripts_exist_and_equal_sources():
    for client in ("claude", "codex"):
        d = OUT / client / "hooks"
        for f in sorted(NEW.glob("*.py")):
            assert (d / f.name).is_file(), f"{client}: {f.name} not built"
            assert (d / f.name).read_bytes() == f.read_bytes(), f"{client}/{f.name} is stale versus harness/hooks"
        assert isinstance(json.loads((d / "evidence_skip.json").read_text(encoding="utf-8")), list)


# ----------------------------------------------------------------------------------------------- spawn shim
SHIM = NEW / "codex_spawn_shim.py"


def test_codex_spawn_shim_rewrites_spawn_agent_and_passes_exit_code_and_streams_through():
    sb = SB("shim")
    root = sb.root / "projroot"
    root.mkdir()
    hook = sb.root / "tiny_hook.py"
    hook.write_text(
        "import sys, json, os\n"
        "d = json.loads(sys.stdin.read())\n"
        "sys.stdout.write(json.dumps({'tool': d.get('tool_name'), 'ti': d.get('tool_input'), 'sid': d.get('session_id'),\n"
        "  'proj': os.environ.get('CLAUDE_PROJECT_DIR'), 'cwd': os.getcwd(), 'args': sys.argv[1:]}))\n"
        "sys.stderr.write('tiny-stderr-marker')\n"
        "sys.exit(2)\n", encoding="utf-8", newline="\n")
    spawn = {"session_id": "s1", "tool_name": "spawn_agent",
             "tool_input": {"agent_type": "reviewer", "message": "check it", "model": "m1", "extra": [1]}}
    cp = subprocess.run([h.PY, "-s", str(SHIM), str(root), h.PY, str(hook), "--flag"], input=json.dumps(spawn),
                        capture_output=True, text=True, encoding="utf-8", env=sb.env("new"), timeout=60)
    assert cp.returncode == 2, (cp.returncode, cp.stderr)
    seen = json.loads(cp.stdout)
    assert seen["tool"] == "Agent" and seen["sid"] == "s1"
    assert seen["ti"] == {"model": "m1", "extra": [1], "subagent_type": "reviewer", "prompt": "check it"}, seen["ti"]
    assert os.path.normcase(seen["proj"]) == os.path.normcase(str(root))
    assert os.path.normcase(os.path.realpath(seen["cwd"])) == os.path.normcase(os.path.realpath(root))
    assert seen["args"] == ["--flag"]
    assert "tiny-stderr-marker" in cp.stderr
    other = {"session_id": "s2", "tool_name": "Bash", "tool_input": {"command": "ls", "agent_type": "keep"}}
    cp = subprocess.run([h.PY, "-s", str(SHIM), str(root), h.PY, str(hook)], input=json.dumps(other),
                        capture_output=True, text=True, encoding="utf-8", env=sb.env("new"), timeout=60)
    seen = json.loads(cp.stdout)
    assert seen["tool"] == "Bash" and seen["ti"] == other["tool_input"], seen  # other tools pass through unchanged


def test_codex_spawn_shim_exit_zero_stdout_passthrough_and_usage_error_and_bad_json():
    sb = SB("shim2")
    root = sb.root / "pr"
    root.mkdir()
    echo = sb.root / "echo_hook.py"
    echo.write_text("import sys\nsys.stdout.write(sys.stdin.read())\n", encoding="utf-8", newline="\n")
    raw = "this is not json"
    cp = subprocess.run([h.PY, "-s", str(SHIM), str(root), h.PY, str(echo)], input=raw, capture_output=True, text=True,
                        encoding="utf-8", env=sb.env("new"), timeout=60)
    assert cp.returncode == 0 and cp.stdout == raw, (cp.returncode, cp.stdout, cp.stderr)  # not JSON: forwarded untouched
    cp = subprocess.run([h.PY, "-s", str(SHIM), str(root)], input="{}", capture_output=True, text=True, encoding="utf-8",
                        env=sb.env("new"), timeout=60)
    # a broken install fails closed (exit 2 = block), see the S3-T2 review (silent-failure finding)
    assert cp.returncode == 2 and "usage" in cp.stderr.lower(), (cp.returncode, cp.stderr)
    deny = sb.root / "deny_hook.py"
    deny.write_text("import sys, json\nsys.stdin.read()\n"
                    "print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',"
                    " 'permissionDecisionReason': 'nope'}}))\n", encoding="utf-8", newline="\n")
    spawn = {"tool_name": "spawn_agent", "tool_input": {"agent_type": "a", "message": "m"}}
    cp = subprocess.run([h.PY, "-s", str(SHIM), str(root), h.PY, str(deny)], input=json.dumps(spawn), capture_output=True,
                        text=True, encoding="utf-8", env=sb.env("new"), timeout=60)
    assert cp.returncode == 0 and json.loads(cp.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny", cp.stdout


if __name__ == "__main__":
    sys.exit(h.run_all(globals()))
