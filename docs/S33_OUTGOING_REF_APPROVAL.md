# S33: authorize the outgoing reference, not the unrelated checkout

Task: S33-T1. Operator authorization: 2026-11-17, correct the hook and complete PDCC main-to-master synchronization without closing unfinished tasks.

## Contract

The R2 shell guard must retain and resolve the source/destination in the actual push command. This narrow release supports one explicit branch refspec; multiple refs and implicit/default push selection are rejected rather than partially authorized. Compare real correlated GPT approval with the outgoing commit, not the unrelated working-copy HEAD. Use the sprint named by the outgoing source branch when present; its acceptance remains required. Do not select another branch's first unclosed sprint. For main/master synchronization without a matching sprint, permit only an exact, genuinely approved commit already published on main/master of the same configured remote, and only a normal fast-forward branch update. Unknown or ambiguous operations fail closed. Do not change task states to allow publication.

Revalidate approval against the durable PM reply using the existing verifier; mutable ledger fields alone are insufficient. Keep installed native pre-push checks and branch protection. Deny unresolved refs, multi-ref pushes with any unapproved source, force/deletion/wildcard/tag mappings and unsupported options with a clear reason. Current PR creation and test-command safeguards remain in force.

Review refinements: the published-primary exception requires exactly one existing destination ref, not initial branch creation. Command-scoped environment changes and compound shell expressions containing a push are unsupported and denied; issue a standalone push, using `git -C` for repository selection when needed. Environment context is retained through supported wrappers, shells and substitution recursion rather than silently discarded. This conservative boundary avoids guessing whether a preceding environment setter changes Git's effective routing.

## Verification and delivery

Implementation is confined to the push authorization path. After coding, functional-test-reviewer authors and executes a named plain-Python behavioral file using isolated temporary Git repositories and synthetic correlated PM journals. Cover checked-out A/pushing B, exact approved published main-to-master, unfinished matching versus unrelated sprint, bad/missing/revised approval, multiple refs, unknown options and repeat/crash/race-sensitive refusal. No real ref mutations in tests. Prohibited runners and CI remain prohibited.

Relevant independent code/Python review follows; fixes receive focused rechecks. The final committed delta goes to GPT-PM before installation/publication. Installation copies only the reviewed rules_gate.py to the configured Claude/Codex hook locations with recoverable old copies and hash readback. Then attempt the original normal PDCC master push and read back both remote tips. Do not disable a guard or manufacture acceptance if another safety condition refuses it.

## Preservation

Harness_v2 primary checkout has pre-existing dirty rules_gate.py and a deleted older test file. They remain untouched. The implementation uses a temporary worktree from published origin/main ba07e55. Product code and unfinished PDCC tasks are outside this change. Remove the temporary tree after its branch is merged and all tracked/unignored changes are preserved.
