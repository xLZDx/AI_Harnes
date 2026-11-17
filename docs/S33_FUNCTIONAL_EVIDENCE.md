# S33 functional test evidence

Task: S33-T1. Base revision: `ba07e5585b9f93e84496016e038d914813493441`.

## Test artifact and coverage intent

`tests/test_s33_outgoing_ref.py` invokes the real `harness/hooks/rules_gate.py` subprocess with shell-hook JSON, uses native Git repositories and a local bare remote, and copies the real project `tools/ledger.py` into each isolated fixture so PM approval is checked by `find_pm_approve`. It does not execute push commands through Git; setup pushes only mutate temporary fixture repositories. It snapshots and compares ledger task states around the main-to-master authorization check.

Coverage intent:

- Checked-out detached commit A while outgoing `main` is approved, published commit B; remote `master` is B's ancestor.
- Matching unfinished sprint denied; unrelated unfinished sprint ignored; matching verified task allowed.
- Approval head, request ID, reply ID, project, latest REVISE, conflicting HEAD in reply and wrong reply project denied.
- Implicit refs, multiple refs, wildcard, deletion, force, mirror, follow-tags, no-verify, unknown options and dynamic `git -C $OTHER` fail closed.
- Configured follow-tags and distinct push URL denied; explicit real `git -C` repository path exercised.
- Approved source advance after approval denied.

## Execution evidence

Source hash observed at the start: `harness/hooks/rules_gate.py` SHA256 `D64AA8CF54671681FDEE79A21D0A8FA0FD001D36B1541C05CE4EF879C8605312`; this matched the assigned source hash. The final complete-file invocation below ran against the implementer's subsequent source SHA256 `D25C2BAFF446C2A8D8FE6A9983EB670225BB81ED9FD308A0B61ED83EF6F6990F`. Base `HEAD` was the revision above.

Exact command, invocation 1: `python tests/test_s33_outgoing_ref.py`.

Observed output showed errors in the test fixture helper because it called `unittest.TestCase.assertEqual` on a plain fixture object. That helper was corrected. This invocation is a test-authoring failure, not product evidence.

Exact command, invocation 2: `python tests/test_s33_outgoing_ref.py`.

The command exceeded the tool's 30-second wait, but its returned session was later recovered by the parent as PID 37636. That run used an intermediate test file with fixture authoring defects and is not result evidence.

Exact command, invocation 3: `python tests/test_s33_outgoing_ref.py` (stdout/stderr captured at `D:/tmp/hv2-s33-functional-final.log`). It completed with exit code 1: 7 tests ran in 175.284 seconds; 6 methods passed and `test_approval_and_durable_latest_reply_must_match_exact_outgoing_sha` failed because the test placed `project` in the mutable approval object rather than in the canonical ledger root. The test was corrected by removing this invalid fixture case; wrong canonical PM journal project is independently denied in that method.

Exact command, focused invocation 4: `python tests/test_s33_outgoing_ref.py OutgoingReferenceTests.test_approval_and_durable_latest_reply_must_match_exact_outgoing_sha` (stdout/stderr captured at `D:/tmp/hv2-s33-functional-focused.log`). It completed with exit code 0: one test ran in 62.874 seconds and reported `OK`.

After those runs, two additional push-option regressions and a true dry-run case were added on the implementer's request, and the implementation was reported as receiving a further narrow source fix. Those latest edits have **NOT_RUN** evidence. Runtime/staging evidence is **NOT_RUN**; tests use only local temporary Git data and do not establish installed-hook, real remote or production behavior. Do not treat earlier complete-file success as validation of these latest edits.

## Remaining test gaps

- Non-fast-forward with a truly divergent destination, absent destination, and retry/idempotency outcomes are not independently exercised by this test file.
- Missing or malformed ledger cases and missing PM journal are not covered here.
- `HEAD` source mapping and unsupported submodule config variants are not separately exercised.
- No installed Claude/Codex hook, real network, staging or production execution is tested.
