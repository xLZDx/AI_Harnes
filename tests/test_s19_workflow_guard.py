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


class GuardMultipleOn(Base):
    """Criterion: more than one top-level `on:` key in a workflow is UNPARSABLE (push blocked)."""

    def test_dispatch_then_push_blocked(self):
        code, err = self.push("a.yml", "name: x\non: workflow_dispatch\non: push\njobs: {}\n")
        self.assertEqual(code, 1, err)

    def test_push_then_dispatch_blocked(self):
        code, err = self.push("a.yml", "name: x\non: push\non: workflow_dispatch\njobs: {}\n")
        self.assertEqual(code, 1, err)

    def test_two_documents_dispatch_then_push_blocked(self):
        body = "name: x\non: workflow_dispatch\njobs: {}\n---\nname: y\non: push\njobs: {}\n"
        code, err = self.push("a.yml", body)
        self.assertEqual(code, 1, err)

    def test_twin_nested_on_key_in_step_with_passes(self):
        body = ("name: x\non: workflow_dispatch\njobs:\n  b:\n    runs-on: x\n    steps:\n"
                "      - uses: some/action@v1\n        with:\n          on: push\n")
        code, err = self.push("a.yml", body)
        self.assertEqual(code, 0, err)

    def test_duplicate_dispatch_keys_blocked_fail_closed(self):
        # Follows the criterion "more than one top-level on: is UNPARSABLE" even when both are workflow_dispatch.
        code, err = self.push("a.yml", "name: x\non: workflow_dispatch\non: workflow_dispatch\njobs: {}\n")
        self.assertEqual(code, 1, err)


class GuardKeyForms(Base):
    """Criteria f-i: only plain or simply quoted top-level keys are accepted; keys case-insensitive; documents."""
    DECOY = "name: x\non: workflow_dispatch\n"

    def blocked(self, body, name="a.yml"):
        code, err = self.push(name, body)
        self.assertEqual(code, 1, err)

    def passes(self, body, name="a.yml"):
        code, err = self.push(name, body)
        self.assertEqual(code, 0, err)

    def test_exotic_top_level_keys_with_decoy_blocked(self):
        forms = {
            "escaped": '"on": [push]\n',
            "tagged": "!!str on: push\n",
            "anchored": "&k on: push\n",
            "explicit": "? on\n: push\n",
        }
        for k, extra in forms.items():
            with self.subTest(form=k):
                self.setUp()
                self.blocked(self.DECOY + extra + "jobs: {}\n", f"{k}.yml")

    def test_second_document_other_indent_blocked(self):
        self.blocked("on: workflow_dispatch\n---\n  on: push\n")

    def test_document_marker_inline_on_blocked(self):
        self.blocked("on: workflow_dispatch\n--- on: push\n")

    def test_leading_bare_document_marker_passes(self):
        self.passes("---\nname: x\non: workflow_dispatch\njobs: {}\n")

    def test_key_case_insensitive_capital_on_dispatch_passes(self):
        self.passes("name: x\nOn: workflow_dispatch\njobs: {}\n")

    def test_key_case_insensitive_upper_on_push_blocked(self):
        self.blocked("name: x\nON: push\njobs: {}\n")

    def test_event_name_case_insensitive_blocked(self):
        self.blocked("name: x\non: PUSH\njobs: {}\n")

    def test_twin_normal_workflow_with_nested_on_passes(self):
        body = ("name: x\non: workflow_dispatch\npermissions:\n  contents: read\njobs:\n  b:\n    runs-on: x\n"
                "    steps:\n      - uses: a/b@v1\n        with:\n          on: push\n")
        self.passes(body)

    def test_twin_quoted_on_key_dispatch_passes(self):
        self.passes('name: x\n"on": workflow_dispatch\njobs: {}\n')


class GuardFlowForms(Base):
    """Criteria j-m: flow collections with odd quoting/comments, unclosed flows, nested block lists, twins."""
    JOBS = "jobs:\n  b:\n    runs-on: x\n    steps:\n      - run: echo ]\n"

    def blocked(self, body, name="a.yml"):
        code, err = self.push(name, body)
        self.assertEqual(code, 1, err)

    def passes(self, body, name="a.yml"):
        code, err = self.push(name, body)
        self.assertEqual(code, 0, err)

    def test_flow_list_with_hash_item_blocked(self):
        for i, item in enumerate(["' #'", "'a #b'"]):
            with self.subTest(item=item):
                self.setUp()
                self.blocked(f"name: x\non: [workflow_dispatch, {item}, push]\n" + self.JOBS, f"l{i}.yml")

    def test_flow_list_a_hash_b_double_quoted_blocked(self):
        self.blocked('name: x\non: [workflow_dispatch, "a #b", push]\n' + self.JOBS)

    def test_flow_mapping_with_hash_key_blocked(self):
        self.blocked("name: x\non: {workflow_dispatch: , ' #': , push: }\n" + self.JOBS)

    def test_unclosed_flow_list_closed_at_column_zero_blocked(self):
        self.blocked("name: x\non: [workflow_dispatch,\njobs: ]\n")

    def test_nested_block_list_blocked(self):
        self.blocked("name: x\non:\n  - - push\njobs: {}\n")

    def test_flow_mapping_with_quote_in_continuation_blocked(self):
        self.blocked('name: x\non: {workflow_dispatch: {inputs: {a: {description: "}}}}"\n  }}}, push: {}}\njobs: {}\n')

    def test_quoted_flow_list_item_blocked(self):
        self.blocked('name: x\non: ["push"]\njobs: {}\n')

    def test_twin_unquoted_nested_flow_mapping_passes(self):
        self.passes("name: x\non: {workflow_dispatch: {inputs: {a: {description: x}}}}\njobs: {}\n")

    def test_twin_flow_list_with_trailing_comment_passes(self):
        self.passes("name: x\non: [workflow_dispatch]  # manual only\njobs: {}\n")

    def test_twin_multiline_flow_list_passes(self):
        # repository_dispatch is not push/pull_request/schedule, so it is accepted like workflow_dispatch.
        self.passes("name: x\non: [workflow_dispatch,\n  repository_dispatch]\njobs: {}\n")


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
