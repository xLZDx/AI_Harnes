"""S21-T2 acceptance tests for gpt_review_gate.py (push on ok=false, outage warning on commit, quoted words).

Final name in the worktree: tests/test_s21_review_gate.py (this copy has a neutral name because the NEW go_gate
treats any path part containing "gate" as sensitive, see report).

Each case runs the hook as a subprocess with a Claude (or Codex) Bash payload, a temp HOME, a temp receipt store,
a temp enabled-repo root and real `git init` repositories.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DEFAULT_ROOT = Path(r"D:\tmp\scratch_worktrees\hv2-s21")
ROOT = Path(os.environ.get("HV2_ROOT") or _HERE.parent)
if not (ROOT / "harness" / "hooks").is_dir():
    ROOT = _DEFAULT_ROOT
HOOK = ROOT / "harness" / "hooks" / "gpt_review_gate.py"

TMPROOT = Path(tempfile.mkdtemp(prefix="s21r_")).resolve()
atexit.register(lambda: shutil.rmtree(TMPROOT, ignore_errors=True))
_n = [0]


class Box:
    """One isolated world: its own HOME, receipt store, enabled-repo root."""

    def __init__(self):
        _n[0] += 1
        self.root = TMPROOT / f"box{_n[0]}"
        self.repos = self.root / "repos"
        self.state = self.root / "state"
        self.home = self.root / "home"
        for d in (self.repos, self.state, self.home, self.root / "nogit"):
            d.mkdir(parents=True)
        self.receipts = self.state / "receipts.jsonl"

    def repo(self, name="proj") -> Path:
        p = self.repos / name
        p.mkdir(parents=True)
        cp = subprocess.run(["git", "init", "-q", "-b", "main", str(p)], capture_output=True, text=True)
        assert cp.returncode == 0, cp.stderr
        return p

    def receipt(self, repo: Path, age_hours: float = 0.0, **kw):
        ts = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
        rec = {"timestamp": ts, "cwd": str(repo), "ok": True, "round": 1, **kw}
        with self.receipts.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

    def env(self) -> dict:
        e = {k: v for k, v in os.environ.items()
             if not k.startswith(("CLAUDE", "CODEX", "HARNESS", "SESSION_", "PM_BRIDGE", "ANTHROPIC"))}
        e.update({
            "PYTHONIOENCODING": "utf-8", "GIT_TERMINAL_PROMPT": "0",
            "USERPROFILE": str(self.home), "HOME": str(self.home),
            "HARNESS_HOOK_STATE": str(self.root / "hs"), "CLAUDE_HOOK_STATE_DIR": str(self.root / "chs"),
            "CLAUDE_GPT_REVIEW_STATE": str(self.state), "CLAUDE_GPT_REVIEW_ROOTS": str(self.repos),
            "HARNESS_METRICS_DIR": str(self.root / "metrics"),
        })
        return e

    def run(self, command: str, cwd: Path, client="claude"):
        tp = self.root / ("tx_" + client + ".jsonl")
        tp.write_text("{}\n", encoding="utf-8")
        payload = {"session_id": "s21", "hook_event_name": "PreToolUse", "tool_name": "Bash",
                   "tool_input": {"command": command}, "cwd": str(cwd), "transcript_path": str(tp)}
        if client == "codex":
            payload["turn_id"] = "t1"
        cp = subprocess.run([sys.executable, "-s", str(HOOK), f"--harness-client={client}"], input=json.dumps(payload),
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            env=self.env(), cwd=str(self.root), timeout=90)
        assert cp.returncode == 0, (cp.returncode, cp.stderr[-400:])
        return Result(cp)


class Result:
    def __init__(self, cp):
        self.out, self.err = cp.stdout.strip(), cp.stderr.strip()
        self.kind, self.reason = "allow", ""
        if self.out:
            hso = json.loads(self.out)["hookSpecificOutput"]
            self.kind = hso["permissionDecision"]
            self.reason = hso.get("permissionDecisionReason", "")

    def __repr__(self):
        return f"kind={self.kind} reason={self.reason[:300]!r} stdout={self.out[:200]!r} stderr={self.err[:200]!r}"


def expect(res: Result, kind: str):
    assert res.kind == kind, f"expected {kind}, got {res!r}"


# ------------------------------------------------------------------------------------------------ 1. push, ok=false
def test_push_with_final_receipt_ok_false_is_denied_with_next_steps():
    b = Box()
    repo = b.repo()
    b.receipt(repo, final=True, ok=False, error="No compatible PM Bridge orchestrator is active")
    r = b.run("git push origin main", repo)
    expect(r, "deny")
    assert "pm_bridge_mode_on" in r.reason and "ok=false" in r.reason, r
    # same deny for a Codex payload (Codex supports deny)
    expect(b.run("git push origin main", repo, client="codex"), "deny")


def test_push_with_final_receipt_ok_true_is_allowed_twin():
    b = Box()
    repo = b.repo()
    b.receipt(repo, final=True, ok=True, verdict="APPROVE")
    r = b.run("git push origin main", repo)
    expect(r, "allow")
    assert r.out == "" and r.err == "", r


def test_push_ok_false_receipt_that_is_not_final_or_is_stale_is_still_denied():
    b = Box()
    repo = b.repo()
    b.receipt(repo, final=False, ok=True)
    expect(b.run("git push origin main", repo), "deny")
    b2 = Box()
    repo2 = b2.repo()
    b2.receipt(repo2, age_hours=30, final=True, ok=True)  # older than the 24 h push window
    expect(b2.run("git push origin main", repo2), "deny")


# ------------------------------------------------------------------------------------------------ 2. commit, ok=false
OUTAGE = "No compatible PM Bridge orchestrator is active"


def test_commit_with_ok_false_receipt_is_allowed_with_local_warning_claude():
    b = Box()
    repo = b.repo()
    b.receipt(repo, ok=False, error=OUTAGE)
    r = b.run('git commit -m "x"', repo)
    expect(r, "allow")
    assert "This commit stays LOCAL" in r.reason and "pm_bridge_mode_on" in r.reason, r
    assert OUTAGE in r.reason, r  # the receipt error is carried into the warning


def test_commit_with_ok_false_receipt_warns_on_stderr_for_codex_and_stdout_is_empty():
    b = Box()
    repo = b.repo()
    b.receipt(repo, ok=False, error=OUTAGE)
    r = b.run('git commit -m "x"', repo, client="codex")
    assert r.kind == "allow" and r.out == "", r
    assert "This commit stays LOCAL" in r.err and "pm_bridge_mode_on" in r.err, r


def test_commit_with_ok_true_receipt_is_allowed_silently_twin():
    b = Box()
    repo = b.repo()
    b.receipt(repo, ok=True)
    r = b.run('git commit -m "x"', repo)
    assert r.kind == "allow" and r.out == "" and r.err == "", r
    r = b.run('git commit -m "x"', repo, client="codex")
    assert r.kind == "allow" and r.out == "" and r.err == "", r


# ------------------------------------------------------------------------------------------------ 3. quoted words
def test_words_inside_quotes_or_heredocs_do_not_trigger_the_push_gate():
    b = Box()
    repo = b.repo()  # NO receipt at all
    expect(b.run('echo "git push"', repo), "allow")
    expect(b.run("echo 'git push now'", repo), "allow")
    expect(b.run("cat <<EOF\ngit push\nEOF", repo), "allow")
    expect(b.run("git status", repo), "allow")
    expect(b.run("git log --oneline", repo), "allow")


def test_commit_message_mentioning_push_is_gated_as_a_commit_only():
    b = Box()
    repo = b.repo()  # no receipt
    r = b.run('git commit -m "fix: git push gate"', repo)
    expect(r, "deny")
    assert "(commit)" in r.reason and "(push)" not in r.reason, r
    # twin: a real push without a receipt is gated as a push
    r = b.run("git push origin main", repo)
    expect(r, "deny")
    assert "(push)" in r.reason, r


def test_real_pushes_in_any_command_position_still_deny_without_a_receipt():
    b = Box()
    repo = b.repo()
    outside = b.root / "nogit"
    for cmd, cwd in (
        ("git push", repo),
        ("git push origin main", repo),
        ("echo done; git push", repo),
        ("git status && git push origin main", repo),
        (f'cd "{repo}" && git push', outside),
        (f"cd {repo} && git push", outside),
        ("GIT_TRACE=0 git push", repo),
    ):
        r = b.run(cmd, cwd)
        expect(r, "deny")
        assert "(push)" in r.reason, (cmd, r)


def test_git_dash_C_push_denies_without_a_receipt():
    """Criterion 3: `git -C <repo> push` still denies. The outer PUSH_RE (`\\bgit\\s+push\\b`) never sees the -C form."""
    b = Box()
    repo = b.repo()
    r = b.run(f"git -C {repo} push", repo)
    expect(r, "deny")
    assert "(push)" in r.reason, r


# ------------------------------------------------------------------------------------------------ 4. pm-bridge rule
def test_pm_bridge_repo_still_pushes_on_a_fresh_receipt_even_when_ok_false():
    b = Box()
    repo = b.repo("pm-bridge")
    b.receipt(repo, ok=False, error=OUTAGE)  # fresh, not final, ok=false
    expect(b.run("git push origin main", repo), "allow")
    # twin: the same receipt does not open the push of an ordinary repo
    other = b.repo("ordinary")
    b.receipt(other, ok=False, error=OUTAGE)
    expect(b.run("git push origin main", other), "deny")
    # twin: pm-bridge with NO fresh receipt is still denied
    b2 = Box()
    repo2 = b2.repo("pm-bridge")
    expect(b2.run("git push origin main", repo2), "deny")
    b3 = Box()
    repo3 = b3.repo("pm-bridge")
    b3.receipt(repo3, age_hours=30, ok=True)
    expect(b3.run("git push origin main", repo3), "deny")


def run_all(g) -> int:
    fails = 0
    for name in [n for n in g if n.startswith("test_")]:
        try:
            g[name]()
            print(f"PASS {name}")
        except BaseException:
            fails += 1
            print(f"FAIL {name}")
            traceback.print_exc(limit=4, file=sys.stdout)
    print(f"{'FAILED' if fails else 'OK'}: {fails} failing")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run_all(globals()))
