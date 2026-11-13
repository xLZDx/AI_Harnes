r"""S19 workflow_guard hardening: fail-closed behaviour of tools/workflow_guard.py and audit_projects.chk_ci_local.
Run: C:\Python314\python.exe -I -s tests\test_s19_workflow_guard.py (from the repo root)."""
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parent.parent / "tools"
GUARD = TOOLS / "workflow_guard.py"
sys.path.insert(0, str(TOOLS))
import workflow_guard  # noqa: E402
import audit_projects  # noqa: E402

ZERO = "0" * 40
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e.x", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e.x")


def git(cwd, *a):
    r = subprocess.run(["git", *a], cwd=cwd, env=ENV, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.repo = Path(self._td.name)
        git(self.repo, "init", "-q")

    def push(self, name, content, raw=False):
        """Commit one workflow file and run the guard as git's pre-push hook would; returns (exit, stderr)."""
        wd = self.repo / ".github" / "workflows"
        wd.mkdir(parents=True, exist_ok=True)
        data = content if raw else content.encode("utf-8")
        (wd / name).write_bytes(data)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "w")
        sha = git(self.repo, "rev-parse", "HEAD")
        r = subprocess.run([sys.executable, "-I", "-s", str(GUARD)], cwd=self.repo, env=ENV, capture_output=True,
                           input=f"refs/heads/x {sha} refs/heads/x {ZERO}\n".encode(), timeout=120)
        return r.returncode, r.stderr.decode("utf-8", "replace")


class GuardPush(Base):
    def test_non_ascii_name_push_blocked(self):
        code, err = self.push("сборка.yml", "name: x\non: push\njobs: {}\n")
        self.assertEqual(code, 1, err)
        self.assertIn("push", err)

    def test_non_ascii_name_dispatch_passes(self):
        code, err = self.push("сборка.yml", "name: x\non: workflow_dispatch\njobs: {}\n")
        self.assertEqual(code, 0, err)

    def test_bom_push_blocked(self):
        code, _ = self.push("a.yml", b"\xef\xbb\xbfname: x\non: push\n", raw=True)
        self.assertEqual(code, 1)

    def test_bom_dispatch_passes(self):
        code, err = self.push("a.yml", b"\xef\xbb\xbfname: x\non: workflow_dispatch\n", raw=True)
        self.assertEqual(code, 0, err)

    def test_uniform_indent_pull_request_blocked(self):
        code, _ = self.push("a.yml", "  name: x\n  on: pull_request\n  jobs: {}\n")
        self.assertEqual(code, 1)

    def test_uniform_indent_dispatch_passes(self):
        code, err = self.push("a.yml", "  name: x\n  on: workflow_dispatch\n  jobs: {}\n")
        self.assertEqual(code, 0, err)

    def test_no_on_key_blocked_unparsable(self):
        code, err = self.push("a.yml", "name: x\njobs:\n  b:\n    runs-on: x\n")
        self.assertEqual(code, 1)
        self.assertIn("unparsable", err)

    def test_dispatch_passes(self):
        code, err = self.push("a.yml", "name: x\non: workflow_dispatch\njobs: {}\n")
        self.assertEqual(code, 0, err)

    def test_trigger_forms_blocked(self):
        forms = {
            "inline": "on: push\n",
            "flow_list": "on: [push, workflow_dispatch]\n",
            "block_list": "on:\n  - workflow_dispatch\n  - push\n",
            "mapping": "on:\n  workflow_dispatch:\n  push:\n    branches: [main]\n",
            "flow_map": "on: {push: {branches: [main]}}\n",
            "schedule": "on:\n  schedule:\n    - cron: '0 0 * * *'\n",
        }
        for k, body in forms.items():
            with self.subTest(form=k):
                self.setUp()
                code, err = self.push(f"{k}.yml", "name: x\n" + body + "jobs: {}\n")
                self.assertEqual(code, 1, err)

    def test_trigger_forms_dispatch_only_pass(self):
        forms = ["on: workflow_dispatch\n", "on: [workflow_dispatch]\n", "on:\n  - workflow_dispatch\n",
                 "on:\n  workflow_dispatch:\n    inputs:\n      a:\n        description: d\n", "on: {workflow_dispatch: {}}\n"]
        for i, body in enumerate(forms):
            with self.subTest(i=i):
                self.setUp()
                code, err = self.push(f"f{i}.yml", "name: x\n" + body + "jobs: {}\n")
                self.assertEqual(code, 0, err)


class GuardTimeout(unittest.TestCase):
    def run_main(self, fake_git):
        err = io.StringIO()
        with mock.patch.object(workflow_guard, "git", fake_git), \
                mock.patch.object(sys, "stdin", io.StringIO(f"r {'a' * 40} r {ZERO}\n")), mock.patch.object(sys, "stderr", err):
            return workflow_guard.main(), err.getvalue()

    def test_timeout_blocks(self):
        def boom(*a):
            raise subprocess.TimeoutExpired(["git", *a], 60)
        code, err = self.run_main(boom)
        self.assertEqual(code, 1)
        self.assertIn("timed out", err)

    def test_timeout_via_subprocess_run(self):
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired(["git", "ls-tree"], 60)):
            code, err = self.run_main(workflow_guard.git)
        self.assertEqual(code, 1)
        self.assertIn("timed out", err)

    def test_twin_no_timeout_passes(self):
        def ok(*a):
            return subprocess.CompletedProcess(["git", *a], 0, stdout="", stderr="")
        code, err = self.run_main(ok)
        self.assertEqual(code, 0, err)
        self.assertNotIn("timed out", err)


class AuditUnreadable(unittest.TestCase):
    def proj(self, td, body):
        d = Path(td)
        (d / ".github" / "workflows").mkdir(parents=True)
        (d / ".github" / "workflows" / "a.yml").write_text(body, encoding="utf-8")
        return d

    def test_unreadable_is_fail(self):
        with tempfile.TemporaryDirectory() as td:
            d = self.proj(td, "on: workflow_dispatch\n")
            with mock.patch.object(Path, "read_text", side_effect=OSError("denied")):
                st, msg = audit_projects.chk_ci_local(d)
        self.assertEqual(st, audit_projects.FAIL)
        self.assertIn("unreadable", msg)

    def test_twin_readable_dispatch_ok(self):
        with tempfile.TemporaryDirectory() as td:
            st, msg = audit_projects.chk_ci_local(self.proj(td, "on: workflow_dispatch\n"))
        self.assertEqual(st, audit_projects.OK, msg)

    def test_no_on_key_is_fail(self):
        with tempfile.TemporaryDirectory() as td:
            st, _ = audit_projects.chk_ci_local(self.proj(td, "name: x\n"))
        self.assertEqual(st, audit_projects.FAIL)


if __name__ == "__main__":
    unittest.main()
