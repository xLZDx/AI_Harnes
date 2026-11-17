# S33 functional test evidence

## Final bounded wrapper regression

Source commit `f6df8fe2c2bdf2db39fcc14b08671b97b1b17dfc`, unchanged hook SHA256 `6549A36E4CC743F86DA3994D4760BF36A0A606DDE7661B70CA714D63324E4419`. The actual functional-test-reviewer ran `python tests/test_s33_outgoing_ref.py OutgoingReferenceTests.test_command_environment_config_overrides_and_environment_mutators_deny OutgoingReferenceTests.test_checked_out_a_can_publish_exact_approved_main_tip_to_master OutgoingReferenceTests.test_r1_ci_and_full_suite_protections_remain_active OutgoingReferenceTests.test_actual_dry_run_flag_is_not_confused_with_push_option_value`. Exit 1: three methods passed; the environment method stopped at fixture setup because TemporaryDirectory was used as a Path. Log: `D:/tmp/hv2-s33-last-focused.log`, SHA256 `18093D207F594F83CD55B68B100D42F00805D2F9C787C8A6A855FE59194FC727`.

The same tester corrected only `self.temp / "harmless.env"` to `f.tmp / "harmless.env"`, then executed `python tests/test_s33_outgoing_ref.py OutgoingReferenceTests.test_command_environment_config_overrides_and_environment_mutators_deny`. Exit 0, `Ran 1 test in 44.643s`, `OK`; log `D:/tmp/hv2-s33-wrapper-focused.log`, SHA256 `EEF7AAE461FA1264F037B15EFBEB2D262B8020E54E706E1FB40105EC977061DC`. Corrected test SHA256 `BA46D7B7BC9EED50B0D624970D0C88E9F8245E12E0706459B97C6CB469A880B5`. Product source did not change between these runs. Thus the four affected methods were exercised successfully across the two runs; this is NOT a claim that one complete-file invocation on this source returned zero. Earlier full and focused runs below remain historical evidence with their exact revisions and limits.

The tester's shell append of this final result was rejected by its GO gate. The parent independently read the durable output and records these measured facts here, without editing role journals or fabricating ledger acceptance. Actual installed-hook/remote behavior is still NOT_RUN until the separately authorized installation and native push are observed. No CI or prohibited runner was executed.

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
- Direct and `env`-wrapped `GIT_CONFIG_*` overrides plus `HOME`, `PATH`, and `env -u` mutations are expected to deny while a plain approved push remains allowed.
- An approved, published main tip targeting a missing master ref is expected to deny without changing ledger task state.

## Execution evidence

Source hash observed at the start: `harness/hooks/rules_gate.py` SHA256 `D64AA8CF54671681FDEE79A21D0A8FA0FD001D36B1541C05CE4EF879C8605312`; this matched the assigned source hash. The final complete-file invocation below ran against the implementer's subsequent source SHA256 `D25C2BAFF446C2A8D8FE6A9983EB670225BB81ED9FD308A0B61ED83EF6F6990F`. Base `HEAD` was the revision above.

Exact command, invocation 1: `python tests/test_s33_outgoing_ref.py`.

Observed output showed errors in the test fixture helper because it called `unittest.TestCase.assertEqual` on a plain fixture object. That helper was corrected. This invocation is a test-authoring failure, not product evidence.

Exact command, invocation 2: `python tests/test_s33_outgoing_ref.py`.

The command exceeded the tool's 30-second wait, but its returned session was later recovered by the parent as PID 37636. That run used an intermediate test file with fixture authoring defects and is not result evidence.

Exact command, invocation 3: `python tests/test_s33_outgoing_ref.py` (stdout/stderr captured at `D:/tmp/hv2-s33-functional-final.log`). It completed with exit code 1: 7 tests ran in 175.284 seconds; 6 methods passed and `test_approval_and_durable_latest_reply_must_match_exact_outgoing_sha` failed because the test placed `project` in the mutable approval object rather than in the canonical ledger root. The test was corrected by removing this invalid fixture case; wrong canonical PM journal project is independently denied in that method.

Exact command, focused invocation 4: `python tests/test_s33_outgoing_ref.py OutgoingReferenceTests.test_approval_and_durable_latest_reply_must_match_exact_outgoing_sha` (stdout/stderr captured at `D:/tmp/hv2-s33-functional-focused.log`). It completed with exit code 0: one test ran in 62.874 seconds and reported `OK`.

After those runs, push-option regressions, a true dry-run case, command-scoped environment override cases, and missing-destination behavior were added as follow-up review coverage. The latest source fixes for configuration overrides and absent destinations have not yet been committed, and those tests have **NOT_RUN** evidence. Per parent instruction, do not execute until a new source commit is confirmed. Runtime/staging evidence is **NOT_RUN**; tests use only local temporary Git data and do not establish installed-hook, real remote or production behavior. Do not treat earlier complete-file success as validation of these latest edits.

## Remaining test gaps

- Non-fast-forward with a truly divergent destination, absent destination, and retry/idempotency outcomes are not independently exercised by this test file.
- Missing or malformed ledger cases and missing PM journal are not covered here.
- `HEAD` source mapping and unsupported submodule config variants are not separately exercised.
- Latest environment-override and absent-destination cases are authored but remain unexecuted pending the new source commit.
- No installed Claude/Codex hook, real network, staging or production execution is tested.

## Post-commit execution evidence (2026-11-17)

Source commit: `5193381e41b63a398a05507ad4d71e6c3dc3fcb8`. Before the run, `harness/hooks/rules_gate.py` SHA256 was `13ED23A92CB37E2CD473F9FA567279733723FB7046B2B09A3733A0F56C668CB7`; `tests/test_s33_outgoing_ref.py` SHA256 was `7A73ECF97E63AD9AACD9F7D640F5DDCF63FB7C89CB7D1CD10646234B51DF225A`.

Exact command: `python tests/test_s33_outgoing_ref.py` (stdout and stderr redirected to `D:/tmp/hv2-s33-postcommit-functional.log`). The command completed with exit code 0. Output: `Ran 8 tests in 278.242s` and `OK`. Log SHA256: `872F7E1780350490BFFFD74150569F5F838DB462D9F85E55869E9E8F425F7E3C`.

This execution covers the eight methods reported by `unittest`, including the true dry-run flag distinction. It exercises local temporary Git repositories/remotes and synthetic PM approval records; it does not prove installed-hook behavior, real network/remote behavior, staging or production behavior. CI, broader suites, lint, typecheck, build, and production checks were not run.

## Review-fix regression evidence (2026-11-17)

Source commit: `f23de0c32b6377df9519c3b8707d24e23c710cfe`. At execution start, `harness/hooks/rules_gate.py` SHA256 was `5BEF0A3D038FBBB82F5A88A6277872531F28A0D31A2E812043BCC4176A27CBDE`; the committed `tests/test_s33_outgoing_ref.py` SHA256 was `7B89359755AB8BD15682DD112A3A608E69289EEDF16622AF80DE8AEE1F8C17A4`.

Exact full-file command: `python tests/test_s33_outgoing_ref.py` (stdout/stderr redirected to `D:/tmp/hv2-s33-reviewfix-functional.log`). It exited 1 after running 10 tests: nine passed and `test_missing_master_destination_denies_without_changing_ledger` errored before exercising its assertions. The fixture called its `git()` helper without required keyword-only `cwd`; this is a test-fixture error (`TypeError`), not a product assertion failure. Full-run log SHA256: `298DE78FF9EA853FA2C39FAE893017F9359500A65010F0CF43D7BA2628BDDA79`.

The fixture call was corrected to pass `cwd=f.repo`, without changing its expected behavior or product source. Exact focused command: `python tests/test_s33_outgoing_ref.py OutgoingReferenceTests.test_missing_master_destination_denies_without_changing_ledger` (stdout/stderr redirected to `D:/tmp/hv2-s33-missing-target-focused.log`). It exited 0: one test ran in 18.811s and reported `OK`. Focused log SHA256: `8911387CF4691EA65A574D05719720BF368FF3853AABCF6D932EDEA57AF053A7`. The corrected test file SHA256 is `FF514416C121259BB2424B0EEDDC605A04E66B2AD563CB924E3FF22B79B6218E`.

Thus nine methods passed in the full run and the sole fixture-blocked method passed in its focused rerun; the complete ten-method file was not rerun after the fixture correction. This is local Git fixture evidence only, not installed-hook, live remote, staging, production or CI evidence. No broader suites, lint, typecheck, build or verification scripts were run.

## Final focused source-correction evidence (2026-11-17)

Source commit: `f6df8fe2c2bdf2db39fcc14b08671b97b1b17dfc`. Before execution, `harness/hooks/rules_gate.py` SHA256 was `6549A36E4CC743F86DA3994D4760BF36A0A606DDE7661B70CA714D63324E4419`; `tests/test_s33_outgoing_ref.py` SHA256 was `958FD8F6121890709CDA7A42B64D60CF973EE0F51E389B0900C42AE46BADA9A8`.

Exact command: `python tests/test_s33_outgoing_ref.py OutgoingReferenceTests.test_command_environment_config_overrides_and_environment_mutators_deny OutgoingReferenceTests.test_checked_out_a_can_publish_exact_approved_main_tip_to_master OutgoingReferenceTests.test_r1_ci_and_full_suite_protections_remain_active OutgoingReferenceTests.test_actual_dry_run_flag_is_not_confused_with_push_option_value` (stdout/stderr redirected to `D:/tmp/hv2-s33-last-focused.log`). It exited 1 after four methods: three passed, while `test_command_environment_config_overrides_and_environment_mutators_deny` errored in test setup before its behavior assertions. The fixture uses `self.temp / "harmless.env"` where `self.temp` is a `TemporaryDirectory`, producing `TypeError: unsupported operand type(s) for /: 'TemporaryDirectory' and 'str'`. This is test-fixture failure, not a product assertion failure. Log SHA256: `18093D207F594F83CD55B68B100D42F00805D2F9C787C8A6A855FE59194FC727`.

No source or test edits were made in this execution. The earlier ten-method run and focused missing-destination correction remain as recorded above; this focused run does not establish a green full file or green environment-mutator coverage. Evidence remains limited to local Git fixtures, with no installed-hook, live remote, staging, production or CI proof.
