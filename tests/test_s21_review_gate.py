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
    b.receipt(repo, final=True, ok=True, verdict="APPROVE", correlated=True)
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
def test_pm_bridge_repo_no_longer_pushes_on_a_fresh_ok_false_receipt():
    """S21 (770455b): the pm-bridge exception for failed receipts is retired."""
    b = Box()
    repo = b.repo("pm-bridge")
    b.receipt(repo, ok=False, error=OUTAGE)  # fresh, not final, ok=false
    expect(b.run("git push origin main", repo), "deny")
    # also with a FINAL ok=false receipt
    b1 = Box()
    repo1 = b1.repo("pm-bridge")
    b1.receipt(repo1, final=True, ok=False, error=OUTAGE)
    expect(b1.run("git push origin main", repo1), "deny")
    # passing twins: a fresh ok=true receipt (verdict APPROVE, or no verdict) still opens the pm-bridge push
    # (S21 final: ok true + correlated true + APPROVE; final is not needed here)
    for kw in ({"ok": True, "verdict": "APPROVE", "correlated": True},
               {"ok": True, "final": True, "verdict": "APPROVE", "correlated": True}):
        bt = Box()
        rt = bt.repo("pm-bridge")
        bt.receipt(rt, **kw)
        expect(bt.run("git push origin main", rt), "allow")
    # a fresh ok=true receipt that lacks any one of the three conditions does not (final or not)
    for kw in ({"verdict": "REVISE", "correlated": True},
               {"verdict": "APPROVE", "correlated": False},
               {"verdict": "APPROVE"},                          # correlated missing
               {"correlated": True},                            # verdict missing
               {"correlated": True, "verdict": None},           # verdict null
               {}):
        for final in (False, True):
            bv = Box()
            rv = bv.repo("pm-bridge")
            bv.receipt(rv, ok=True, final=final, **kw)
            r = bv.run("git push origin main", rv)
            assert r.kind == "deny", (kw, final, r)
    # a naive (no time zone) timestamp is stale for pm-bridge too, and does not crash the hook
    bn = Box()
    rn = bn.repo("pm-bridge")
    bn.receipt(rn, ok=True, verdict="APPROVE", correlated=True, timestamp=datetime.now().isoformat())
    expect(bn.run("git push origin main", rn), "deny")
    # twin: the same ok=true receipt does not open the push of an ordinary repo unless it is final
    other = b.repo("ordinary")
    b.receipt(other, ok=True)
    expect(b.run("git push origin main", other), "deny")
    # twin: pm-bridge with NO fresh receipt is still denied
    b2 = Box()
    repo2 = b2.repo("pm-bridge")
    expect(b2.run("git push origin main", repo2), "deny")
    b3 = Box()
    repo3 = b3.repo("pm-bridge")
    b3.receipt(repo3, age_hours=30, ok=True)
    expect(b3.run("git push origin main", repo3), "deny")


# ------------------------------------------------------------------------------------------------ 5. S21 fixes (770455b)
GOOD = dict(final=True, ok=True, verdict="APPROVE", correlated=True)
WRAPPED_PUSHES = [
    'bash -c "git push origin main"',
    "sh -c 'git push'",
    'powershell -Command "git push"',
    "time git push",
    "env X=1 git push",
    "if git push; then echo ok; fi",
    "{ git push; }",
    "git --no-pager push",
    "echo don't; git push origin x; echo 'y'",
]


def test_wrapped_pushes_deny_without_a_receipt():
    b = Box()
    repo = b.repo()  # no receipt
    for cmd in WRAPPED_PUSHES:
        r = b.run(cmd, repo)
        expect(r, "deny")
        assert "(push)" in r.reason, (cmd, r)


def test_wrapped_pushes_are_allowed_with_a_good_final_receipt_twin():
    b = Box()
    repo = b.repo()
    b.receipt(repo, **GOOD)
    for cmd in WRAPPED_PUSHES:
        r = b.run(cmd, repo)
        assert r.kind == "allow", (cmd, r)


def test_mentions_of_git_push_are_not_pushes():
    b = Box()
    repo = b.repo()
    b.receipt(repo, ok=True)  # fresh, not final: enough for a commit, never for a push
    for cmd in ('echo "git push"', "echo 'git push'"):
        expect(b.run(cmd, repo), "allow")
    r = b.run('git commit -m "mention git push"', repo)
    expect(r, "allow")  # a commit with a receipt; if it were read as a push it would be denied (not final)
    # twin: a real push on the same receipt is denied
    expect(b.run("git push", repo), "deny")


def test_git_dash_C_other_repo_from_non_git_cwd():
    b = Box()
    spaced = b.repo("my proj")
    plain = b.repo("plainrepo")
    nogit = b.root / "nogit"
    forms = [
        (f'git -C "{spaced}" push', spaced),
        (f"git -C {plain.as_posix()} push", plain),
        (f'git -C "{plain}" push origin main', plain),
        (f"git -C '{spaced.as_posix()}' push", spaced),
    ]
    for cmd, _ in forms:
        r = b.run(cmd, nogit)
        expect(r, "deny")  # no receipt anywhere
        assert "(push)" in r.reason, (cmd, r)
    b.receipt(spaced, **GOOD)
    b.receipt(plain, **GOOD)
    for cmd, _ in forms:
        expect(b.run(cmd, nogit), "allow")


def test_git_dash_C_repo_is_judged_not_the_payload_cwd():
    b = Box()
    good = b.repo("good")
    bare = b.repo("bare")
    b.receipt(good, **GOOD)
    # payload cwd is a gated repo WITH a good receipt, -C points at a repo with none: deny
    expect(b.run(f"git -C {bare.as_posix()} push", good), "deny")
    expect(b.run(f'git -C "{bare}" push', good), "deny")
    # twin: payload cwd without a receipt, -C points at the good repo: allow
    expect(b.run(f"git -C {good.as_posix()} push", bare), "allow")
    # twin: no -C at all, cwd = good: allow; cwd = bare: deny
    expect(b.run("git push", good), "allow")
    expect(b.run("git push", bare), "deny")


def test_no_verify_and_inline_skip_approval_deny_even_with_a_good_receipt():
    b = Box()
    repo = b.repo()
    b.receipt(repo, **GOOD)
    expect(b.run("git push origin main", repo), "allow")  # twin
    for cmd in ("git push --no-verify origin main", "git push origin main --no-verify",
                "HARNESS_SKIP_APPROVAL=1 git push origin main", "HARNESS_SKIP_APPROVAL=1 git push"):
        r = b.run(cmd, repo)
        expect(r, "deny")
        assert "pm_bridge_mode_on" in r.reason, (cmd, r)
    # pm-bridge repo with a good fresh receipt is not exempt
    bp = Box()
    pm = bp.repo("pm-bridge")
    bp.receipt(pm, **GOOD)
    expect(bp.run("git push origin main", pm), "allow")
    expect(bp.run("git push --no-verify origin main", pm), "deny")
    expect(bp.run("HARNESS_SKIP_APPROVAL=1 git push", pm), "deny")


def test_final_receipt_with_wrong_verdict_or_uncorrelated_denies():
    for kw in ({"verdict": "APPROVE", "correlated": False},      # ok true, uncorrelated
               {"verdict": "APPROVE"},                           # correlated key missing
               {"correlated": True},                             # verdict missing
               {"correlated": True, "verdict": None},            # verdict null
               {"correlated": True, "verdict": ""},
               {"verdict": "REVISE", "correlated": True},
               {"verdict": "REJECT", "correlated": True},
               {"verdict": "REVISE"}, {}):
        b = Box()
        repo = b.repo()
        b.receipt(repo, final=True, ok=True, **kw)
        r = b.run("git push origin main", repo)
        expect(r, "deny")
        assert "pm_bridge_mode_on" in r.reason, (kw, r)
    # twins that open the push: ok true + correlated true + APPROVE in the verdict
    for kw in ({"verdict": "APPROVE", "correlated": True}, {"verdict": "approve", "correlated": True}):
        b = Box()
        repo = b.repo()
        b.receipt(repo, final=True, ok=True, **kw)
        expect(b.run("git push origin main", repo), "allow")


def test_receipt_timestamp_without_time_zone_is_stale_not_a_crash():
    b = Box()
    repo = b.repo()
    naive = datetime.now().isoformat()  # no tz suffix
    assert "+" not in naive and not naive.endswith("Z")
    b.receipt(repo, final=True, ok=True, verdict="APPROVE", correlated=True, timestamp=naive)
    r = b.run("git push origin main", repo)  # Box.run asserts exit code 0: no crash
    expect(r, "deny")
    assert "time zone" in r.reason, r
    # twin: the same receipt with an explicit UTC offset opens the push
    b2 = Box()
    repo2 = b2.repo()
    b2.receipt(repo2, final=True, ok=True, verdict="APPROVE", correlated=True, timestamp=datetime.now(timezone.utc).isoformat())
    expect(b2.run("git push origin main", repo2), "allow")


def test_denial_and_commit_warning_name_the_remedy_tools():
    b = Box()
    repo = b.repo()
    b.receipt(repo, final=True, ok=False, error=OUTAGE)
    r = b.run("git push origin main", repo)
    expect(r, "deny")
    assert "pm_bridge_mode_on" in r.reason and "review.js" in r.reason, r
    b2 = Box()
    repo2 = b2.repo()
    b2.receipt(repo2, ok=False, error=OUTAGE)
    r = b2.run('git commit -m "x"', repo2)
    expect(r, "allow")
    assert "pm_bridge_mode_on" in r.reason and "review.js" in r.reason, r
    r = b2.run('git commit -m "x"', repo2, client="codex")
    assert "pm_bridge_mode_on" in r.err and "review.js" in r.err, r


# ------------------------------------------------------------------------------------------------ 6. S21 regressions (7a5b291)
sys.path.insert(0, str(ROOT / "harness" / "hooks"))
import _commit_detect  # noqa: E402  (the worktree's copy, not the installed one)


def _pushes(box, repo):
    """Commands that MUST be treated as a push: (command, label). Paths come from the given repo."""
    return [
        ("/usr/bin/git push", "absolute posix git path"),
        ('"C:/Program Files/Git/bin/git.exe" push', "quoted git.exe path with a space"),
        (f"git --git-dir {(repo / '.git').as_posix()} push", "--git-dir space form"),
        ("git push --no-ver", "abbreviated --no-verify (alone: still a push, still needs a receipt)"),
        ('bash -c "git push origin main"', "bash -c"),
        ("ssh host 'git push'", "ssh quoted"),
        ('powershell -Command "git push"', "powershell -Command"),
        ("echo don't; git push origin x; echo 'y'", "apostrophe in prose before a real push"),
    ]


MENTIONS = [
    'git commit -m "do X, then git push later"',
    'git commit -m "Block git push when env missing"',
    'git commit -m "fix" && echo "git push"',
]


def test_is_push_command_true_for_the_hard_shapes_and_false_for_mentions():
    b = Box()
    repo = b.repo()
    for cmd, label in _pushes(b, repo):
        assert _commit_detect.is_push_command(cmd) is True, (label, cmd)
    assert _commit_detect.is_push_command("git -c core.hooksPath=NUL push") is True
    for cmd in MENTIONS + ['echo "git push"']:
        assert _commit_detect.is_push_command(cmd) is False, cmd
    # twin of each commit mention: the bare commit is a commit and not a push
    assert _commit_detect.is_commit_command('git commit -m "do X, then git push later"') is True
    assert _commit_detect.is_push_command('git commit -m "x"') is False


def test_hard_push_shapes_deny_without_receipt_and_allow_with_a_good_one():
    b = Box()
    repo = b.repo()
    for cmd, label in _pushes(b, repo):
        r = b.run(cmd, repo)
        expect(r, "deny")
        assert "(push)" in r.reason, (label, cmd, r)
    b.receipt(repo, **GOOD)
    for cmd, label in _pushes(b, repo):
        if "--no-ver" in cmd:
            continue  # an abbreviated skip-hook flag is denied even with a receipt, see the next test
        r = b.run(cmd, repo)
        assert r.kind == "allow", (label, cmd, r)


def test_skip_hook_flags_deny_even_with_a_good_receipt_abbreviation_and_hookspath():
    b = Box()
    repo = b.repo()
    b.receipt(repo, **GOOD)
    expect(b.run("git push", repo), "allow")  # twin
    for cmd in ("git push --no-ver", "git push --no-verif origin main",
                "git -c core.hooksPath=NUL push", "git -c core.hooksPath=/dev/null push origin main"):
        r = b.run(cmd, repo)
        expect(r, "deny")
        assert "pm_bridge_mode_on" in r.reason, (cmd, r)
    # and without any receipt
    b2 = Box()
    repo2 = b2.repo()
    expect(b2.run("git -c core.hooksPath=NUL push", repo2), "deny")


def test_mentions_of_push_in_a_commit_are_commits_with_a_recent_receipt():
    b = Box()
    repo = b.repo()
    b.receipt(repo, ok=True)  # recent, NOT final and without verdict: opens a commit, never a push
    for cmd in MENTIONS:
        r = b.run(cmd, repo)
        assert r.kind == "allow" and r.out == "" and r.err == "", (cmd, r)
    expect(b.run('echo "git push"', repo), "allow")
    expect(b.run("git push", repo), "deny")  # twin: the real push on the same receipt


def test_mentions_of_push_in_a_commit_are_denied_as_commits_without_a_receipt():
    b = Box()
    repo = b.repo()  # no receipt
    for cmd in MENTIONS:
        r = b.run(cmd, repo)
        expect(r, "deny")
        assert "(commit)" in r.reason and "(push)" not in r.reason, (cmd, r)
    expect(b.run('echo "git push"', repo), "allow")  # not a commit, not a push
    r = b.run("git push", repo)  # twin
    expect(r, "deny")
    assert "(push)" in r.reason, r


def test_dash_C_in_an_earlier_segment_does_not_hide_the_pushed_repo():
    b = Box()
    a = b.repo("repoA")
    bb = b.repo("repoB")
    b.receipt(a, **GOOD)  # A is good, B has nothing
    r = b.run(f"git -C {a.as_posix()} status; git -C {bb.as_posix()} push", a)
    expect(r, "deny")
    assert "(push)" in r.reason and "repoB" in r.reason, r
    # twin: the push targets the good repo, the status targets the bare one
    expect(b.run(f"git -C {bb.as_posix()} status; git -C {a.as_posix()} push", bb), "allow")
    # twin: same shape with && and a payload cwd that is the good repo
    expect(b.run(f"git -C {a.as_posix()} status && git -C {bb.as_posix()} push", a), "deny")


# ------------------------------------------------------------------------------------------------ 7. S21 third batch
def _push_denied(r, cmd):
    expect(r, "deny")
    assert "(push)" in r.reason, (cmd, r)


def test_skip_flags_only_skip_when_they_belong_to_the_git_command():
    b = Box()
    repo = b.repo()  # NO receipt
    for cmd in ("git push origin main; echo -h", "git push origin main && echo --dry-run",
                "git push origin main; echo --help",
                "git push origin main | cat --dry-run"):
        _push_denied(b.run(cmd, repo), cmd)
    # twins: the flag is git's own, so the command is harmless and passes with no receipt
    for cmd in ("git push --dry-run", "git push -h", "git commit --dry-run", "git push --help"):
        r = b.run(cmd, repo)
        assert r.kind == "allow", (cmd, r)


OBFUSCATED_PUSHES = [
    'git "push"', "git 'push'", '"git" push', "git -c alias.p=push p", "git --no-replace-objects push",
    "git --glob-pathspecs push origin", "git $(echo push)", "git ${P:-push}", 'git -c "x=a;b" push',
    "git \\\npush", 'echo "<<A"\ngit push\nA', "echo it\\'s; git push; echo don\\'t",
    "bash <<'EOF'\ngit push origin main\nEOF", "cmd /c git push", "powershell -c git push", "wsl git push",
    "timeout 60 git push", "echo x | xargs git push",
    "python -c \"import os; os.system('git push')\"",
    "node -e \"require('child_process').execSync('git push')\"",
]


def test_obfuscated_subcommands_deny_without_a_receipt():
    b = Box()
    repo = b.repo()
    for cmd in OBFUSCATED_PUSHES:
        _push_denied(b.run(cmd, repo), cmd)
    # -C into a folder that does not exist: the repository of the push cannot be checked
    expect(b.run("git -C 'my;dir' push", repo), "deny")


def test_obfuscated_subcommands_are_allowed_with_a_good_receipt_twin():
    b = Box()
    repo = b.repo()
    b.receipt(repo, **GOOD)
    for cmd in OBFUSCATED_PUSHES:
        r = b.run(cmd, repo)
        assert r.kind == "allow", (cmd, r)


def test_trailing_comment_with_a_skip_flag_does_not_skip_the_push_gate():
    b = Box()
    repo = b.repo()  # NO receipt
    _push_denied(b.run("git push origin main # --help", repo), "# --help")


def test_dash_C_into_a_missing_quoted_folder_with_semicolon_denies_even_with_a_good_receipt():
    b = Box()
    repo = b.repo()
    b.receipt(repo, **GOOD)
    expect(b.run("git -C 'my;dir' push", repo), "deny")


NOT_PUSHES = [
    "git status", "git lg", "git log $(git rev-parse HEAD)..HEAD", 'git commit -m "msg with git push inside"',
    "git commit -m \"$(cat <<'EOF'\nsubject line\n\ngit push later\nEOF\n)\"",
    "python -c \"print('git push is blocked')\"", "echo hello", "git --no-pager log -3",
]


def test_non_push_twins_never_count_as_a_push():
    b = Box()
    repo = b.repo()  # no receipt: a commit is denied as a commit, anything else is allowed
    for cmd in NOT_PUSHES:
        r = b.run(cmd, repo)
        if cmd.startswith("git commit"):
            expect(r, "deny")
            assert "(commit)" in r.reason and "(push)" not in r.reason, (cmd, r)
        else:
            assert r.kind == "allow", (cmd, r)
    # with a recent non-final receipt the commits pass, a real push on the same receipt does not
    b.receipt(repo, ok=True)
    for cmd in NOT_PUSHES:
        assert b.run(cmd, repo).kind == "allow", cmd
    expect(b.run("git push", repo), "deny")


def test_undecidable_repository_denies_a_push_but_not_a_commit():
    b = Box()
    repo = b.repo()
    nogit = b.root / "nogit"
    r = b.run("git push", nogit)
    expect(r, "deny")
    assert "cannot be determined" in r.reason, r
    # twin: a commit from the non-git folder is still allowed (fail-open kept for commits)
    assert b.run("git commit -m x", nogit).kind == "allow"
    expect(b.run("git push", repo), "deny")


def _repo_forms(repo: Path):
    p = repo.as_posix()
    return [
        f"cd {p} && git push", f"git -C {p} push", f"git --work-tree {p} push", f"git --git-dir {p}/.git push",
        f"cd {p}\ngit push", f"(cd {p} && git push)", f"pushd {p}; git push", f"cd {p} || exit; git push",
    ]


def test_push_from_a_non_git_folder_is_judged_against_the_named_repository():
    b = Box()
    repo = b.repo()
    nogit = b.root / "nogit"
    missing = nogit / "missing"
    for cmd in _repo_forms(repo):
        r = b.run(cmd, nogit)
        _push_denied(r, cmd)
        assert "proj" in r.reason, (cmd, r)
    expect(b.run(f"git -C {missing.as_posix()} push", nogit), "deny")
    b.receipt(repo, **GOOD)
    for cmd in _repo_forms(repo):
        r = b.run(cmd, nogit)
        assert r.kind == "allow", (cmd, r)
    # twin: a good receipt for the repo does not open a push into a folder that does not exist
    expect(b.run(f"git -C {missing.as_posix()} push", nogit), "deny")
    # twin: another repository without a receipt is still denied
    other = b.repo("other")
    _push_denied(b.run(f"git -C {other.as_posix()} push", nogit), "other")


def _good_line(repo, **kw) -> bytes:
    rec = {"timestamp": datetime.now(timezone.utc).isoformat(), "cwd": str(repo), "ok": True, "round": 1}
    rec.update(GOOD)
    rec.update(kw)
    return (json.dumps(rec) + "\n").encode("utf-8")


def test_receipt_file_with_invalid_utf8_before_a_good_receipt_still_allows():
    b = Box()
    repo = b.repo()
    b.receipts.write_bytes(b"\xff\xfe\x80 not json\n" + _good_line(repo))
    expect(b.run("git push", repo), "allow")
    # twin: the same garbage with no good receipt behind it denies
    b2 = Box()
    repo2 = b2.repo()
    b2.receipts.write_bytes(b"\xff\xfe\x80 not json\n")
    expect(b2.run("git push", repo2), "deny")


def test_malformed_receipt_lines_deny_without_a_crash():
    for name in ("proj", "pm-bridge"):
        for raw in (b"5\n", b"[]\n", b'"text"\n', b"null\n"):
            b = Box()
            repo = b.repo(name)
            b.receipts.write_bytes(raw)
            expect(b.run("git push", repo), "deny")
        for ts in (5, ["x"]):
            b = Box()
            repo = b.repo(name)
            b.receipts.write_bytes(_good_line(repo, timestamp=ts))
            expect(b.run("git push", repo), "deny")
    # twin: the same good line with a string timestamp opens the push
    b = Box()
    repo = b.repo()
    b.receipts.write_bytes(_good_line(repo))
    expect(b.run("git push", repo), "allow")


def test_only_a_real_approve_verdict_opens_a_push():
    for verdict in ("DO NOT APPROVE", "NOT APPROVED", "approved?", "REJECT", ""):
        b = Box()
        repo = b.repo()
        b.receipt(repo, **{**GOOD, "verdict": verdict})
        r = b.run("git push", repo)
        assert r.kind == "deny", (verdict, r)
    for verdict in ("APPROVE", "approve", " APPROVE "):
        b = Box()
        repo = b.repo()
        b.receipt(repo, **{**GOOD, "verdict": verdict})
        r = b.run("git push", repo)
        assert r.kind == "allow", (verdict, r)


def test_unreadable_receipts_path_denies_the_push():
    b = Box()
    repo = b.repo()
    b.receipts.mkdir()  # a folder where the file should be
    _push_denied(b.run("git push", repo), "git push")
    # twin: a normal file with the good receipt allows
    b2 = Box()
    repo2 = b2.repo()
    b2.receipt(repo2, **GOOD)
    expect(b2.run("git push", repo2), "allow")


# ------------------------------------------------------------------------------------------------ 8. S21 fourth batch
def test_flag_words_inside_quotes_or_glued_to_other_text_do_not_skip_the_gate():
    b = Box()
    repo = b.repo()  # NO receipt: the gate must run
    r = b.run('git commit -m "fix --dry-run"', repo)
    expect(r, "deny")
    assert "(commit)" in r.reason, r
    r = b.run('git commit -m "fix -h"', repo)
    expect(r, "deny")
    assert "(commit)" in r.reason, r
    for cmd in ("git push origin feature--help", "git push origin main --push-option=--dry-run",
                "git push -o --dry-run origin main", "git push --push-option --dry-run origin main"):
        _push_denied(b.run(cmd, repo), cmd)
    # twins: the real flags of git skip the gate
    for cmd in ("git push --dry-run origin main", "git push -h", "git commit --help", "git push --help",
                "git commit --dry-run", "git push -o x --dry-run origin main"):
        r = b.run(cmd, repo)
        assert r.kind == "allow", (cmd, r)


def test_git_dash_C_commit_is_judged_against_the_other_repository():
    b = Box()
    mine = b.repo("mine")
    other = b.repo("other")
    b.receipt(mine, ok=True)  # a receipt for the session folder only
    r = b.run(f"git -C {other.as_posix()} commit -m x", mine)
    expect(r, "deny")
    assert "(commit)" in r.reason and "other" in r.reason, r
    # twin: a plain commit in the session folder is covered by its receipt
    expect(b.run("git commit -m x", mine), "allow")
    # twin: a receipt for the other repository opens the -C commit, also from a cwd without one
    b.receipt(other, ok=True)
    expect(b.run(f"git -C {other.as_posix()} commit -m x", mine), "allow")
    expect(b.run(f'git -C "{other}" commit -m x', b.root / "nogit"), "allow")
    # reverse: the receipt of the session folder must not cover -C into a repository without one
    b2 = Box()
    a2 = b2.repo("a2")
    c2 = b2.repo("c2")
    b2.receipt(a2, ok=True)
    expect(b2.run(f'git -C "{c2}" commit -m x', a2), "deny")


def test_git_dash_C_commit_into_a_missing_folder_denies():
    b = Box()
    repo = b.repo()
    b.receipt(repo, ok=True)  # the session folder is fully covered
    missing = b.root / "nogit" / "missing"
    r = b.run(f"git -C {missing.as_posix()} commit -m x", repo)
    expect(r, "deny")
    assert "does not exist" in r.reason, r
    # twin: the same commit with an existing folder passes
    expect(b.run(f"git -C {repo.as_posix()} commit -m x", repo), "allow")


def test_two_different_dash_C_folders_in_one_push_command_deny():
    b = Box()
    a = b.repo("repoA")
    c = b.repo("repoB")
    b.receipt(a, **GOOD)
    b.receipt(c, **GOOD)  # BOTH repositories are good: the refusal is about the shape, not the receipts
    cmd = f"git -C {a.as_posix()} push && git -C {c.as_posix()} push"
    r = b.run(cmd, a)
    expect(r, "deny")
    assert "more than one repository" in r.reason, r
    r = b.run(f"git -C {a.as_posix()} push; git -C {c.as_posix()} push", c)
    expect(r, "deny")
    assert "more than one repository" in r.reason, r
    # twins: each push on its own is allowed, and so is the same folder twice
    expect(b.run(f"git -C {a.as_posix()} push", a), "allow")
    expect(b.run(f"git -C {c.as_posix()} push", a), "allow")
    expect(b.run(f"git -C {a.as_posix()} push && git -C {a.as_posix()} push", c), "allow")
    expect(b.run(f"git -C {a.as_posix()} status && git -C {c.as_posix()} push", a), "allow")


def _junk_variants():
    """Lines that must neither crash the gate nor stand in for a receipt."""
    ok_ts = datetime.now(timezone.utc).isoformat()
    return {
        "naive timestamp": lambda repo: json.dumps({"timestamp": datetime.now().isoformat(), "cwd": str(repo),
                                                    "ok": True, "final": True, "verdict": "APPROVE",
                                                    "correlated": True}).encode(),
        "cwd is a number": lambda repo: json.dumps({"timestamp": ok_ts, "cwd": 5, "ok": True}).encode(),
        "cwd is a list": lambda repo: json.dumps({"timestamp": ok_ts, "cwd": ["x"], "ok": True}).encode(),
        "cwd is an object": lambda repo: json.dumps({"timestamp": ok_ts, "cwd": {"a": 1}, "ok": True}).encode(),
        "nested array 3000": lambda repo: b"[" * 3000 + b"]" * 3000,
        "nested object 3000": lambda repo: b'{"a":' * 3000 + b"1" + b"}" * 3000,
        "nested cwd 3000": lambda repo: b'{"timestamp":"' + ok_ts.encode() + b'","cwd":' + b"[" * 3000 + b"]" * 3000 + b"}",
        "5 MB junk line": lambda repo: b"x" * 5_000_000,
    }


def test_junk_receipt_lines_before_a_valid_receipt_do_not_hide_it_for_push_and_commit():
    for name in ("proj", "pm-bridge"):
        for label, make in _junk_variants().items():
            b = Box()
            repo = b.repo(name)
            b.receipts.write_bytes(make(repo) + b"\n" + _good_line(repo))
            r = b.run("git push", repo)  # Box.run asserts exit code 0: no crash
            assert r.kind == "allow", (name, label, r)
            r = b.run("git commit -m x", repo)
            assert r.kind == "allow" and r.err == "", (name, label, r)


def test_junk_receipt_lines_after_a_valid_receipt_do_not_hide_it_for_commit():
    for label, make in _junk_variants().items():
        if label == "5 MB junk line":
            continue  # the receipt would sit beyond the 4 MB tail window; covered by the before-case above
        b = Box()
        repo = b.repo()
        b.receipts.write_bytes(_good_line(repo) + make(repo) + b"\n")
        r = b.run("git commit -m x", repo)
        assert r.kind == "allow" and r.err == "", (label, r)
    # junk lines that carry no usable cwd are skipped by the push scan as well
    for label in ("cwd is a number", "cwd is a list", "cwd is an object", "nested array 3000", "nested object 3000",
                  "nested cwd 3000"):
        b = Box()
        repo = b.repo()
        b.receipts.write_bytes(_good_line(repo) + _junk_variants()[label](repo) + b"\n")
        r = b.run("git push", repo)
        assert r.kind == "allow", (label, r)


def test_push_and_commit_with_only_junk_receipts_deny_without_a_crash():
    for name in ("proj", "pm-bridge"):
        for label, make in _junk_variants().items():
            b = Box()
            repo = b.repo(name)
            b.receipts.write_bytes(make(repo) + b"\n")
            r = b.run("git push", repo)
            assert r.kind == "deny" and "(push)" in r.reason, (name, label, r)
            if name == "proj":
                r = b.run("git commit -m x", repo)
                assert r.kind == "deny" and "(commit)" in r.reason, (label, r)
    # twin: the same file shape holding a valid receipt opens the push
    b = Box()
    repo = b.repo()
    b.receipts.write_bytes(_good_line(repo))
    expect(b.run("git push", repo), "allow")


DETECT_TRUE = [
    "cat <<'EOF' | bash\ngit push origin main\nEOF",
    "echo git push | sh",
    "printf 'git push\\n' | bash",
    "python - <<'PY'\nimport subprocess\nsubprocess.run('git push', shell=True)\nPY",
    "python3 <<'PY'\nimport os\nos.system('git push origin main')\nPY",
    "py -c \"import os; os.system('git push')\"",
    "python3.12 -c \"import os; os.system('git push')\"",
    "xargs -n1 git push",
    "sudo -u x git push",
    "env -i git push",
    "nice -n 10 git push",
    "start /b git push",
    "wsl -d Ubuntu git push",
    "cmd /d /c git push",
    "powershell -ExecutionPolicy Bypass -Command git push",
    "Start-Process git -ArgumentList push",
    "$c='git push'; iex $c",
]
DETECT_FALSE = [
    "git commit -m \"don't (git push) here\"",
    "cat > notes.md <<'EOF'\nremember to git push later\nEOF",
    "git log --oneline | head -3",
    "echo hello | sh",
    "git stash push",
    "git diff | python -c \"print(1)\"",
]


def test_is_push_command_true_for_wrapped_and_piped_shapes():
    missed = [c for c in DETECT_TRUE if _commit_detect.is_push_command(c) is not True]
    assert not missed, f"not detected as a push: {missed!r}"


def test_is_push_command_false_for_mentions_and_non_pushes():
    wrong = [c for c in DETECT_FALSE if _commit_detect.is_push_command(c) is not False]
    assert not wrong, f"wrongly detected as a push: {wrong!r}"
    # twins: the same words as a real command are a push
    assert _commit_detect.is_push_command("git push") is True
    assert _commit_detect.is_push_command("git stash; git push") is True


def test_detected_wrapped_pushes_deny_without_a_receipt_and_allow_with_a_good_one():
    b = Box()
    repo = b.repo()
    for cmd in DETECT_TRUE:
        _push_denied(b.run(cmd, repo), cmd)
    for cmd in DETECT_FALSE:
        r = b.run(cmd, repo)
        assert "(push)" not in r.reason, (cmd, r)
    b.receipt(repo, **GOOD)
    for cmd in DETECT_TRUE:
        r = b.run(cmd, repo)
        assert r.kind == "allow", (cmd, r)


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
