"""S33 behavioral checks through the real shell-hook subprocess and Git refs."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "harness" / "hooks" / "rules_gate.py"
LEDGER_SOURCE = ROOT / "tools" / "ledger.py"
RID = "11111111-2222-4333-8444-555555555555"


def git(*args, cwd, check=True):
    p = subprocess.run(["git", *map(str, args)], cwd=cwd, text=True, capture_output=True)
    if check and p.returncode:
        raise AssertionError(f"git {' '.join(map(str, args))}: {p.stderr}")
    return p


class Fixture:
    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.remote = self.tmp / "remote.git"
        self.repo = self.tmp / "repo"
        git("init", "--bare", "--initial-branch=main", self.remote, cwd=self.tmp)
        git("init", "--initial-branch=main", self.repo, cwd=self.tmp)
        git("config", "user.email", "test@example.invalid", cwd=self.repo)
        git("config", "user.name", "S33 test", cwd=self.repo)
        self.commit("base")
        git("remote", "add", "origin", self.remote, cwd=self.repo)
        git("push", "origin", "main", cwd=self.repo)
        git("branch", "master", cwd=self.repo)
        git("push", "origin", "main:master", cwd=self.repo)
        self.make_harness()

    def commit(self, message):
        (self.repo / "file.txt").write_text(message, encoding="utf-8")
        git("add", "file.txt", cwd=self.repo)
        git("commit", "-m", message, cwd=self.repo)
        return git("rev-parse", "HEAD", cwd=self.repo).stdout.strip()

    def make_harness(self):
        (self.repo / ".harness-tools").mkdir()
        shutil.copy2(LEDGER_SOURCE, self.repo / ".harness-tools" / "ledger.py")
        (self.repo / ".harness").mkdir()
        (self.tmp / "messages.jsonl").write_text("", encoding="utf-8")
        self.ledger_path = self.repo / ".harness" / "ledger.json"

    def journal(self, sha, verdict="APPROVE", reply_id="reply-1", project="s33-fixture", extra=""):
        records = [
            {"direction": "outbound", "reviewRequestId": RID, "project": project,
             "text": f"Review request HEAD {sha}"},
            {"direction": "inbound", "reviewRequestId": RID, "project": project,
             "correlated": True, "replyId": reply_id, "text": f"{verdict}\nHEAD: {sha}{extra}"},
        ]
        (self.tmp / "messages.jsonl").write_text("".join(json.dumps(x) + "\n" for x in records), encoding="utf-8")

    def ledger(self, sha, sprints=None, **approval_changes):
        approval = {"head": sha, "request_id": RID, "reply_id": "reply-1"}
        approval.update(approval_changes)
        data = {"project": "s33-fixture", "approval": approval, "sprints": sprints or []}
        self.ledger_path.write_text(json.dumps(data), encoding="utf-8")
        return data

    def hook(self, command, cwd=None):
        payload = {"tool_name": "Bash", "cwd": str(cwd or self.repo),
                   "tool_input": {"command": command}, "agent_type": "functional-test-reviewer"}
        env = os.environ.copy()
        env["HARNESS_PM_BRIDGE_MESSAGES"] = str(self.tmp / "messages.jsonl")
        env.pop("HARNESS_SKIP_APPROVAL", None)
        p = subprocess.run([os.environ.get("PYTHON", "python"), str(GATE)], input=json.dumps(payload),
                           text=True, capture_output=True, cwd=ROOT, env=env)
        self.assert_output = p
        if p.returncode != 0:
            raise AssertionError((p.returncode, p.stderr))
        if not p.stdout.strip():
            return None
        return json.loads(p.stdout)

    def expect(self, command, allowed):
        result = self.hook(command)
        denied = result is not None and result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
        if (not denied) != allowed:
            raise AssertionError((command, result, f"expected allowed={allowed}"))
        return result


class OutgoingReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="s33-outgoing-")
        self.fx = Fixture(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def approve(self, sha, sprints=None, **approval_changes):
        self.fx.journal(sha)
        return self.fx.ledger(sha, sprints, **approval_changes)

    def test_checked_out_a_can_publish_exact_approved_main_tip_to_master(self):
        f = self.fx
        git("checkout", "main", cwd=f.repo)
        base = git("rev-parse", "main", cwd=f.repo).stdout.strip()
        outgoing = f.commit("published main B")
        git("push", "origin", "main", cwd=f.repo)
        git("branch", "-f", "master", outgoing, cwd=f.repo)
        git("checkout", "main", cwd=f.repo)
        git("reset", "--hard", base, cwd=f.repo)
        # Unrelated open task state must not be edited to authorize synchronization.
        unrelated = [{"id": "OTHER", "branch": "feature/other", "state": "active",
                      "tasks": [{"id": "T02", "state": "todo"}]}]
        f.journal(outgoing)
        outgoing_ledger = f.ledger(outgoing, unrelated)
        task_snapshot = json.dumps(outgoing_ledger["sprints"], sort_keys=True)
        # Remote master is ancestor of the already-published approved main commit.
        f.expect("git push origin master:refs/heads/master", True)
        self.assertEqual(task_snapshot, json.dumps(json.loads(f.ledger_path.read_text())["sprints"], sort_keys=True))
        self.assertEqual(git("ls-remote", "origin", "refs/heads/master", cwd=f.repo).stdout.split()[0], base)

    def test_unfinished_matching_sprint_denies_but_unrelated_unfinished_sprint_does_not_select(self):
        f = self.fx
        sha = git("rev-parse", "main", cwd=f.repo).stdout.strip()
        f.journal(sha)
        f.ledger(sha, [{"id": "S33", "branch": "main", "state": "active", "tasks": [{"id": "T1", "state": "implemented"}]},
                       {"id": "OTHER", "branch": "other", "state": "active", "tasks": [{"id": "T2", "state": "todo"}]}])
        f.expect("git push origin main:refs/heads/main", False)
        f.ledger(sha, [{"id": "OTHER", "branch": "other", "state": "active", "tasks": [{"id": "T2", "state": "todo"}]}])
        f.expect("git push origin main:refs/heads/main", True)
        f.ledger(sha, [{"id": "S33", "branch": "main", "state": "active", "tasks": [{"id": "T1", "state": "verified"}]}])
        f.expect("git push origin main:refs/heads/main", True)

    def test_approval_and_durable_latest_reply_must_match_exact_outgoing_sha(self):
        f = self.fx
        sha = git("rev-parse", "main", cwd=f.repo).stdout.strip()
        self.approve(sha)
        f.expect("git push origin main:refs/heads/main", True)
        for changes in ({"head": "0" * 40}, {"request_id": "bad"}, {"reply_id": "forged"}):
            self.approve(sha, **changes)
            f.expect("git push origin main:refs/heads/main", False)
        self.fx.ledger(sha)
        f.journal(sha, verdict="APPROVE\nREVISE")
        f.expect("git push origin main:refs/heads/main", False)
        f.journal(sha, verdict="APPROVE", extra="\nHEAD: " + "f" * 40)
        f.expect("git push origin main:refs/heads/main", False)
        f.journal(sha, verdict="APPROVE", project="wrong")
        f.expect("git push origin main:refs/heads/main", False)

    def test_unapproved_tip_or_non_fast_forward_master_sync_is_denied(self):
        f = self.fx
        first = git("rev-parse", "main", cwd=f.repo).stdout.strip()
        second = f.commit("advance")
        git("push", "origin", "main", cwd=f.repo)
        f.journal(first)
        f.ledger(first)
        f.expect("git push origin main:refs/heads/master", False)
        self.approve(second)
        # Existing remote master equals first and is ancestor: allow.
        f.expect("git push origin main:refs/heads/master", True)
        # A source commit newly advanced beyond approval must fail even when HEAD remains unrelated.
        f.commit("after approval")
        f.expect("git push origin main:refs/heads/master", False)

    def test_ambiguous_and_side_effecting_push_shapes_fail_closed(self):
        f = self.fx
        sha = git("rev-parse", "main", cwd=f.repo).stdout.strip()
        self.approve(sha)
        denied = ["git push", "git push origin main:refs/heads/main other:refs/heads/other",
                  "git push origin main:refs/heads/*", "git push origin :refs/heads/main",
                  "git push origin +main:refs/heads/main", "git push --force origin main:refs/heads/main",
                  "git push --delete origin main", "git push --mirror origin main:refs/heads/main",
                  "git push --follow-tags origin main:refs/heads/main", "git push --all origin",
                  "git push --no-verify origin main:refs/heads/main", "git push --unknown origin main:refs/heads/main",
                  "git push origin -o --dry-run main:refs/heads/main",
                  "git push origin --push-option -n main:refs/heads/main",
                  "git -C $OTHER push origin main:refs/heads/main",
                  f'git -C "{f.repo / "missing"}" push origin main:refs/heads/main']
        for command in denied:
            with self.subTest(command=command):
                f.expect(command, False)

    def test_configured_extra_write_paths_and_git_dash_c_target(self):
        f = self.fx
        sha = git("rev-parse", "main", cwd=f.repo).stdout.strip()
        self.approve(sha)
        git("config", "push.followTags", "true", cwd=f.repo)
        f.expect("git push origin main:refs/heads/main", False)
        git("config", "--unset", "push.followTags", cwd=f.repo)
        git("config", "remote.origin.pushurl", self.temp.name + "/elsewhere.git", cwd=f.repo)
        f.expect("git push origin main:refs/heads/main", False)
        git("config", "--unset-all", "remote.origin.pushurl", cwd=f.repo)
        f.expect(f'git -C "{f.repo}" push origin main:refs/heads/main', True)

    def test_r1_ci_and_full_suite_protections_remain_active(self):
        f = self.fx
        for command in ("python -m pytest", "npm test", "gh workflow run checks.yml"):
            with self.subTest(command=command):
                result = f.hook(command)
                self.assertIsNotNone(result, command)
                self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny", result)

    def test_actual_dry_run_flag_is_not_confused_with_push_option_value(self):
        # A genuine dry-run is not a ref write and needs no publication approval.
        self.fx.expect("git push --dry-run origin main:refs/heads/main", True)

    def test_command_environment_config_overrides_and_environment_mutators_deny(self):
        f = self.fx
        sha = git("rev-parse", "main", cwd=f.repo).stdout.strip()
        self.approve(sha)
        plain = "git push origin main:refs/heads/main"
        f.expect(plain, True)
        value = str(self.temp.name).replace("\\", "/") + "/redirect.git"
        injection = (f"GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.pushurl "
                     f"GIT_CONFIG_VALUE_0={value} {plain}")
        wrapped = (f"env GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.pushurl "
                   f"GIT_CONFIG_VALUE_0={value} {plain}")
        for command in (injection, wrapped, f"env -u GIT_CONFIG_COUNT {plain}",
                        f"HOME={self.temp.name} {plain}", f"PATH={self.temp.name} {plain}",
                        f"env -u HOME {plain}", f"env -u PATH {plain}",
                        "$env:GIT_CONFIG_COUNT=1; git push origin main:refs/heads/main",
                        "set GIT_CONFIG_COUNT=1 && git push origin main:refs/heads/main",
                        "env GIT_CONFIG_COUNT=1 bash -c '$(git push origin main:refs/heads/main)'"):
            with self.subTest(command=command):
                f.expect(command, False)

    def test_missing_master_destination_denies_without_changing_ledger(self):
        f = self.fx
        sha = git("rev-parse", "main", cwd=f.repo).stdout.strip()
        self.approve(sha, [{"id": "OTHER", "branch": "feature/other", "state": "active",
                            "tasks": [{"id": "T02", "state": "todo"}]}])
        before = json.loads(f.ledger_path.read_text(encoding="utf-8"))
        # Removing a ref is confined to this temporary bare fixture; the hook itself is read-only.
        git("--git-dir", f.remote, "update-ref", "-d", "refs/heads/master")
        self.assertEqual(git("ls-remote", "origin", "refs/heads/master", cwd=f.repo).returncode, 0)
        self.assertEqual(git("ls-remote", "origin", "refs/heads/master", cwd=f.repo).stdout, "")
        f.expect("git push origin main:refs/heads/master", False)
        after = json.loads(f.ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(before["sprints"], after["sprints"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
