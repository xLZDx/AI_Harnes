# S7 plan: four hook gaps found by the operator on 2026-11-12

Status: PLAN FOR GPT-PM REVIEW. No code is changed yet. Hooks are the Python files in `harness/hooks/`,
built to Claude (`~/.claude/hooks`) and Codex (`~/.codex`) by `tools/build.py` and installed by the two installers.

## Operator's complaint (verbatim meaning)
1. Agents ask the operator to do things they could do themselves. Today, on all projects: asked the operator to
   produce a Terraform state that did not exist (the agent then made it), asked the operator to change gcloud
   settings (then found the keys itself), used valid Cloudflare keys wrongly and made the operator re-create them,
   then printed the keys and asked the operator to rotate them although the agent held working keys and could do it.
   Wanted: a safeguard. Go to the operator only with proof that nothing was found anywhere and nothing can be done.
2. The hook that accepts the recommended answer after 10 minutes does not work.
3. The check "no tasks left" does not work.
4. The work plan before starting is not visible.

## Facts found in this run (primary sources)
- Item 2. `harness/hooks/question_policy.py` DEFAULTS: `pending_mode: "probe"`, `idle_minutes: 5`, `pending_minutes: 5`.
  No `~/.harness-hooks/state/question_policy/config.json` exists, so the defaults apply. In `probe` mode the
  PermissionRequest branch only logs `pending-seen` and returns. The log `auto_answers.jsonl` holds 25 `pending-seen`
  (mode probe) and 0 answered by that path; the 8 `auto-accepted` rows all come from the PreToolUse branch, which
  answers only when the operator was already silent >= idle_minutes BEFORE the question was asked. A question shown to
  an operator who is present never gets answered. The `pending-seen` row is written about 6 s before the
  `permission_prompt` Notification, so the PermissionRequest hook does fire while the dialog is shown. Unverified:
  whether returning `behavior: allow` + `updatedInput.answers` from a PermissionRequest hook that has waited minutes
  really closes the dialog with those answers. `registry.json` gives that hook `timeout: 400` s, which is shorter than
  10 minutes (600 s) plus polling.
- Item 3. `session_close_gate.py` reads only `PROJECT_PLAN.md` found by walking up from the session `cwd`. Harness v2
  projects keep tasks in `.harness/ledger.json` (sprints[].tasks[].state). `session_close_gate.jsonl` shows verdict
  `no-plan` on every recent Stop. Many sessions also start in the workspace root `D:\Repo` (no ledger there), so a
  cwd-only lookup would still miss the project. `keep_going_gate.py` only checks the text of the last message for
  deferral words and the closing line; it never reads the ledger.
- Item 4. `plan_approval_gate.py` fires only on ExitPlanMode under bypassPermissions (settings `defaultMode` is
  `bypassPermissions`). `go_gate.py` requires a GO word, not a plan. No hook requires that a plan is shown before the
  first change.
- Item 1. No hook exists for it. `shell_policy_gate.py`/`dangerous_command_gate.py` block deletion-class commands only.
  `question_policy.py` checks the option format of questions, not whether the agent tried first.

## Tasks (sprint S7, one branch, one PR, one GPT-PM review at the end)

### S7-T1 Fix the 10-minute auto-answer (item 2), risk: concurrency/timing
- DEFAULTS become `pending_mode: "live"`, `idle_minutes: 10`, `pending_minutes: 10`. Destructive/irreversible
  questions (header word check, existing `DESTRUCTIVE_RE`) are still never auto-answered.
- `registry.json` PermissionRequest timeout 400 -> 720. A consistency test fails the build when
  `timeout < pending_minutes*60 + 60`.
- First a real experiment in a live session (`probe_sleep_seconds: 20`): ask one question, do not answer, record
  whether the dialog closes with the recommended answer. If it does not close, the fallback is decided with the
  operator before more work (the plan does not assume it works).
- Keep the re-check: nothing answered meanwhile (`answered_since`) before the hook answers; log every auto-answer
  and report it to the agent at the next prompt (existing `prompt()` branch).
- Tests: deadline/answer logic with a fake transcript and `pending_minutes` 0.05; answered-meanwhile path; destructive
  path; timeout consistency; config typo does not enable the handler.

### S7-T2 Ledger-based "tasks left" Stop gate (item 3), risk: money-free but session-trapping
- Extend `session_close_gate.py` (do not add a second gate): the project is resolved from (a) `cwd`, (b) the git top
  folders of file paths in this session's Edit/Write/shell tool calls (so a session that starts in `D:\Repo` is still
  tied to `D:\Repo\<project>`). For each project with `.harness/ledger.json` whose active sprint is not `closed`, tasks
  whose state is not `done` and not `blocked` are OPEN.
- Open tasks and the last message does not end with a question -> `{"decision":"block"}` listing task ids and states.
  Progress-based release as today: the counter resets when the open count or HEAD changes; after 5 blocks with no
  progress the hook releases with a visible message (never traps a session). A task may be marked blocked only with
  `ledger.py block <ID> --class OPERATOR|EXTERNAL|PROHIBITED --cause "<verified cause>"` (new ledger command, cause
  required, written to the ledger events).
- Several sessions in the same project: the gate counts only tasks, not who owns them, so it never blocks on a
  closed sprint; state file under a lock (existing `file_lock`).
- Tests: ledger fixtures (all done / one pending / one blocked / corrupt ledger = log and allow, not "closed");
  session started in the workspace root; the release after 5 no-progress blocks; Codex Stop payload.

### S7-T3 Plan-first gate (item 4), risk: loop/false positive
- New `plan_first_gate.py`, PreToolUse on Edit|Write|NotebookEdit|MultiEdit and on shell commands classified as
  mutating (reuse the existing `go_gate.bash_needs_go` and git-write classifier, plus `git commit`/`ledger.py`).
  If the current turn (transcript since the last human message) contains no plan call (`update_plan`, `TodoWrite`,
  `TaskCreate`, `ExitPlanMode`, Codex `update_plan`), deny ONCE per turn with: "show the plan first: call update_plan
  with the steps, then continue". Second attempt in the same turn passes (never a trap).
- Skipped: subagents, turns whose operator message is a question or a single short status request, edits of files
  under the scratchpad/tmp folders, kill switch `CLAUDE_PLAN_FIRST_GATE=off`.
- Stop backup: a turn that made changes but never called a plan tool gets one block asking for the plan to be recorded.
- Tests: first edit denied, second allowed; read-only turn untouched; subagent untouched; question turn untouched;
  plan call present -> allowed; Codex tool names.

### S7-T4 "Ask last" gate with proof (item 1), risk: false positives vs. missed incidents
- New `ask_last_gate.py` on AskUserQuestion (PreToolUse, Claude), on Stop for prose asks (both clients).
  An ask is an OPERATOR-ACTION REQUEST when the question/options ask the operator to provide, find, create, configure,
  run, rotate or approve access: keywords key/token/secret/credential/password/state/tfstate/config/access/login/
  rotate/create/set up and Russian equivalents (ключ, токен, секрет, доступ, ротаци, создай, настрой, сделай вручную).
- Such an ask is allowed only with PROOF: the question text must contain a `Tried:` list of at least 3 items; each
  item names a command or path and its result; the gate verifies that each named command/path occurs in the tool
  calls of the current session (not invented). The list must cover what is generally available: environment variable
  NAMES, project config/secret files by name only, CLI auth status (`gcloud auth list`, `terraform state list`, `gh auth
  status` and similar), repo docs, and creating the missing resource itself (for example `terraform init`/`import`).
  Otherwise deny with that checklist.
- Exempt (operator-only by rule, no proof needed but a one-line reason): real money, production data, force-push,
  unrecoverable deletion, creating accounts, passwords, CAPTCHA, anything the platform marks prohibited.
- Tests: ask without proof denied; ask with proof of fabricated commands denied; ask with verified proof allowed;
  exempt classes allowed; non-operator-action questions (design choices) untouched.

### S7-T5 Credential misuse and secret exposure guard (item 1), risk: security-sensitive, trust boundary
- PreToolUse on shell/Read: deny commands that print secret VALUES into the transcript: `cat|type|Get-Content` of
  `.env*`, `*.tfvars`, key files, service-account JSON; `printenv`/`env`/`set` without a NAME filter; `echo $...TOKEN`;
  `gcloud auth print-access-token`; `terraform output` of sensitive values without a name. Allowed: name-only checks
  (`[ -n "$VAR" ]`, length check, masked output) and using a secret inside a command without echoing it.
- PreToolUse: credential-destroying or re-creating commands (delete/create/roll/rotate of API keys, tokens,
  service-account keys; `terraform state rm`, `terraform destroy`) are denied unless the same session already ran an
  authentication check with the EXISTING credential that failed (proof the old one is invalid) or the operator's
  current message names the action. This stops "re-create valid keys".
- PostToolUse: scan tool output for secret patterns (private key blocks, `AKIA`, `ya29.`, `ghp_`, long token after
  `token|key|secret=`); on a hit, add context to the agent: the secret is now exposed, rotate it YOURSELF with the
  working credentials first (the proof requirement of T4 applies before asking the operator).
- Tests: each pattern positive/negative; name-only checks pass; no secret value is written to any log or test output
  (fixtures use obviously fake strings).

## Delivery
- Hook sources + `registry.json` entries for both clients, `tools/build.py` rebuild, `tools/install_claude.py` and
  `tools/install_codex.py`, `tools/probe_live_hooks.py` proof run, `~/.codex` re-approval by the operator via `/hooks`.
- Tests are written and run by the test agents from the acceptance criteria above (functional-test-reviewer / tdd-guide);
  no lint, no full suites. Expected values come from the ledger/transcript fixtures, not from the code under test.
- Rules text: `harness/rules/CORE.md` gets one line per new gate. `core/DECISION_LOG.md` and `core/LESSONS_LOG.md`
  per commit. Review once per sprint by route.py-chosen reviewers, then GPT-PM until APPROVE bound to HEAD.
- Kill switches (env, per hook) and fail-open on internal errors for T2-T5; T5 deny rules fail CLOSED only for the
  explicit secret-printing patterns.

## Questions for GPT-PM
1. Is the order right (T1 timer first because it needs a live experiment)? Anything missing for the four items?
2. T4: is "Tried: with commands verified against the session's tool calls" a sound, non-gameable proof, or should the
   gate instead require the investigative calls to have occurred in the turn before the ask?
3. T5: is blocking secret-printing commands plus a PostToolUse scan enough, and is the "destructive credential change
   needs a failed auth check or the operator's explicit request" rule safe against both over-blocking and bypass?
4. T3: one deny per turn + Stop backup - can this trap or be bypassed (for example by a trivial `update_plan` call)?
5. T2: is resolving the project from tool-call file paths acceptable, and is the 5-no-progress release safe?

---

# REVISION 2 (after GPT-PM round 1: REVISE, 2 BLOCKER / 7 MAJOR / 3 MINOR)

Order changed: T1 (safe lifecycle experiment) -> T5 -> T4 -> T3 -> T2 -> cross-hook integration test (inside T2's test run).
Each finding has a disposition. Where a finding conflicts with an explicit operator order, the order stays and the finding
becomes a narrowing condition.

## BLOCKER-01 auto-answer bypasses confirmation -- ACCEPTED IN PART
The 10-minute auto-accept of the recommended option is an explicit operator order (2026-11-01 and 2026-11-12) and stays. It applies
only to AskUserQuestion (a choice question), never to a platform permission dialog of another tool (the handler acts on tool_name
AskUserQuestion only; a negative test proves other tools are not answered).
Narrowing: the question is KEPT FOR THE OPERATOR (not auto-answered) when the header, the question text, or the label/description
of the RECOMMENDED option matches a risk class: delete/destroy/force/reset, money/trade/payment/production deploy,
credentials/keys/tokens/secrets/access grants, rotate, account creation, permission change. (The 2026-10-25 header-only scope is
widened to the recommended option's text; other options' text is not scanned, to avoid the old false positives.) Every auto-answer
is logged and reported to the agent at the next prompt (existing). Negative tests: escalation, secrets, irreversible, other tool.

## BLOCKER-02 rotate-yourself instruction after a leak -- ACCEPTED
The PostToolUse secret hit no longer says "rotate it yourself". It injects a CONTAINMENT checklist: (1) never repeat the value,
redact it in messages and files; (2) record the exposure in the session exposure record (name, place, time; no value); (3) find
owner and dependent services from repo/config, read-only; (4) rotation by the agent is allowed only when ALL hold: the credential
belongs to the project in scope, a working credential with rotate rights is verified, the order is create-new -> switch consumers
-> verify -> delete-old (never delete first), and the project profile does not list rotation as operator-only; (5) otherwise ask
the operator with the T4 evidence. Test credential or unknown owner -> step 5. No hook rotates anything; the hook only injects text.

## MAJOR-01 "Tried:" can be faked -- ACCEPTED
The agent-written list is replaced by gate-derived evidence. The gate reads this session's tool calls AND RESULTS from the
transcript (tool_use id, command/path, exit status/error text) and classifies read-only investigation by category: env-var NAMES,
config/secret files by name, CLI auth/status, repo docs, try-it-yourself action (create/init/import the missing resource, run the
command, call the API). An ask is allowed when, for the topic words of the ask, each category that applies to that topic (a
topic -> categories table kept as hook data) has at least one RELEVANT call (topic keyword in command/path/result) AND either one
try-it-yourself action failed with a non-transient, non-permission-mapping error, or every category returned "not found".
Counting calls is not used. The deny message lists what is missing. Evidence from earlier in the session counts when newer than
the last operator message that changed the topic and not older than 60 minutes.

## MAJOR-02 plan-first bypass -- ACCEPTED
No automatic second pass. The gate checks plan CONTENT: a plan call with >= 2 steps, each >= 12 characters, no duplicates, one step
naming a file/resource/outcome and one verification step (data keywords test/verify/check/run/проверк/тест). Denied until such a
plan exists in the turn; hard cap of 3 denies per turn, then release with a logged "plan-first-release" and a visible message
(recovery path, never a trap). Kill switch kept. The Stop backup asks once for a recorded plan only when the turn changed files and
no valid plan call exists. Emergency fixes: the kill switch and the cap are the documented recovery.

## MAJOR-03, 04, 05 Stop gate -- ACCEPTED
- Ownership: `ledger.py start` records the starting session id as optional `owner` on the task. The gate blocks for (a) tasks
  owned by THIS session that are not done/blocked and (b) pending tasks with no owner in a sprint this session has worked in.
  Tasks owned by another session are only logged.
- Project identity priority: (1) git root of the session cwd if it has a ledger; (2) ledger roots of ABSOLUTE paths in this
  session's Edit/Write/NotebookEdit calls (shell guesses are not used); (3) none -> log "no-project", allow. Several candidates:
  each one with owned open tasks counts; no random pick.
- Release: progress = a task changed state or the open count dropped (HEAD changes alone are NOT progress). Release after 5
  no-progress blocks OR 30 minutes since the first block of the streak, whichever is first, with a visible message and the
  reason in the log. Corrupt ledger -> log and allow.
- Marking a task blocked needs `ledger.py block <ID> --class OPERATOR|EXTERNAL|PROHIBITED --cause "<verified cause>"`.

## MAJOR-06 secret channel -- ACCEPTED with stated limits
One shared module `secret_policy.py`: sensitive path patterns, command patterns, secret value patterns. Applied in PreToolUse to
the Read tool and to shell commands INCLUDING inline interpreters (python -c, node -e, PowerShell Get-Content/[IO.File]) that name a
sensitive path or print an env var with a secret-looking name. PostToolUse is DETECTIVE only (it cannot undo what is already in the
transcript); text and docs say so. Recorded limit in DECISION_LOG: a secret printed by an arbitrary program through an
unrecognized channel cannot be prevented by hooks.

## MAJOR-07 failed auth != invalid credential -- ACCEPTED
Auth outcome is classified from result text/exit code: credential-invalid (401, invalid/expired/revoked token or key, bad
credentials), permission-insufficient (403, permission denied, scope), config-error (wrong project/region/endpoint, missing
argument), transient (timeout, 5xx, network), unknown. A credential-changing command is allowed only after credential-invalid for
the EXISTING credential, or via an exposure record (containment path), or when the operator's current message names the action.
Any other class -> deny with the class and the fix to try.

## MINOR -- ACCEPTED
- Precedence table, documented and tested: platform restriction -> secret safety (T5) -> operator authorization (go_gate) ->
  ask-last / plan-first -> lifecycle (Stop gates).
- Every decision log row carries session_id, turn marker (timestamp of the last human row), tool_use_id when present and project;
  no secret values in any log (tested with fake secrets).
- T1 experiment success criteria: the dialog closes; the applied answer equals the recommended label; the agent continues with it
  exactly once; no duplicate when the operator answers within 5 s before the deadline; a following question still shows normally.

## Added acceptance criteria (GPT-PM section 3)
1. Auto-answer never touches a non-AskUserQuestion tool and never answers a risk-class question.
2. Ask-last evidence comes from tool results, linked to the ask topic.
3. Plan-first cannot be passed by an empty or one-line plan nor by retry; it has a bounded release.
4. The Stop gate never blocks on tasks owned by another session; every Stop gate releases within 5 no-progress blocks or 30 minutes.
5. The secret guard never triggers rotation by itself; failed auth alone never unlocks a credential change.
6. One integration test replays a scripted turn through all new hooks in precedence order: no deadlock, no double deny.

---

# REVISION 3 (after GPT-PM round 2: REVISE, 3 MAJOR)

## R3-1 plan visibility (replaces the plan-first acceptance rule in MAJOR-02 above)
A valid plan needs a `plan-presented` event for the current turn, taken from the transcript, not from the agent's claim:
(a) the plan tool call has a tool RESULT with success text (the client confirms it rendered the list, for example "Plan shown");
or (b) the assistant text of this turn, written before the first mutation, contains the plan as a numbered/bulleted list of >= 2
steps (text is visible in the chat by definition). A plan tool call without a successful result, or content check failure, is
"recorded, not presented" and does not pass. Release after 3 denies is kept but is logged as `plan-first-exception` (never as a
pass) and shown in the next-turn reminder and in the program report counters. Test: plan call recorded but result missing/failed
-> the gate is not passed; plan only in text -> passed; 3 denies -> exception row, mutation allowed.

## R3-2 credential-invalid bound to one credential (replaces the MAJOR-07 rule)
An auth evidence record is {credential_id, service, account_or_project, operation, checked_at, outcome_class, cause_text_hash}.
credential_id is a stable reference WITHOUT the value: env var NAME, key file path, key id/fingerprint prefix (first 6 chars at
most), or token name. It is written to the evidence store only by the PostToolUse side, from the failing command's own arguments
and environment NAMES. A credential-changing command is parsed for its target (service, account/project, key id/name, operation);
it is allowed only when an evidence record with outcome `credential-invalid` exists for the SAME credential_id, SAME service and
account/project, taken within 60 minutes, and the operation is within the agent's verified rights (a read-only `can rotate?` check
such as listing keys succeeded). Mismatch in any field -> deny with a message naming the mismatch. Deleting or revoking the old
credential is a separate step: allowed only after (i) the new credential was created, (ii) each consumer found in the repo
config was switched and a read-only verification with the NEW credential succeeded; both are evidence records. Tests: 401 on
service A + change of service B key -> denied; same service different project -> denied; stale (>60 min) -> denied; old key
delete before new key verified -> denied.

## R3-3 exposure is not authorization (replaces the containment rule in BLOCKER-02 above)
Three separate states per credential_id, stored as evidence records:
- `exposure_detected`: pattern hit. Effect: containment text only (do not repeat, redact, find owner). Grants nothing.
- `exposure_confirmed`: a strict second check passed: a known-format match (private-key block, provider prefix with the provider
  length) outside test/fixture/doc paths, AND a read-only check that the secret belongs to the project in scope. A false
  positive stays `detected` and is dropped after 24 h.
- `rotation_authorized`: needs `exposure_confirmed` AND a permission source: (a) the operator's current-session message naming
  rotation of that credential, or (b) the project profile (`.harness/profile.json`, key `rotation_policy`) listing the
  credential_id as `agent_may_rotate`, with the reversible order create-new -> switch consumers -> verify -> delete-old.
  Default without (a)/(b): ask the operator, with the T4 evidence and the exposure record attached. The permission binds to that
  credential_id and scope only.
The credential-changing gate of R3-2 accepts a change as part of rotation only in state `rotation_authorized`; `detected` and
`confirmed` never unlock it. Negative test: false-positive detection + technical rotate rights -> change denied. Negative test:
confirmed exposure without permission -> change denied, operator ask allowed with evidence.

---

# ADDITION (operator 2026-11-12, mid-sprint): T6 agent visibility, T7 measuring the harness

Operator: "always show on screen the name of the agent that is running so it is visible what is happening; can the harness be
measured somehow?"

## S7-T6 Running agent name always visible, risk: local
- Claude: new SubagentStart hook (and the existing SubagentStop entry) print a one-line `systemMessage` shown in the chat:
  `[agent START] <agent_type> | model <model> | effort <effort> | task <first 80 chars of the prompt>` and
  `[agent STOP] <agent_type> | <seconds> s | <tokens if known>`. The agent model gate (`agent_model_gate.py`, PreToolUse Agent) also
  emits the same START line when it allows a spawn (covers clients where SubagentStart is missing), de-duplicated by tool_use_id.
- Codex: no systemMessage field; the same lines go to stderr from `agent_model_gate.py` (spawn_agent) and from the Stop hook for
  the finished agent, if the payload carries it. Anything unproven is recorded as such and not claimed.
- Status line: a small `statusLine` command reads the active-agents file (written by START, cleared by STOP, entries older than
  60 min dropped as stale) and shows `agents: code-reviewer, tdd-guide` plus the current sprint/task from the ledger. It is
  additive: an existing user statusLine is not overwritten; the installer only offers it and records the choice.
- Failure behavior: any error prints nothing and never blocks (these hooks only print).
- Tests: START/STOP lines for a fixture payload; stale entry dropped; two parallel agents listed; statusLine output with no
  ledger; installer does not overwrite an existing statusLine.

## S7-T7 Measuring how the harness works, risk: local
- One event log written by every hook through a shared helper `hook_metrics.py`: JSONL rows {ts, client, session_id, turn marker,
  hook, event, decision (allow/deny/block/release/exception), reason code, duration_ms, project, agent_type?}. No prompt text, no
  command text, no secret values (reason codes only; tested with fake secrets). Rotation at 5 MB, failure to write never blocks.
- Agent runs: START/STOP rows (T6) with duration and, when the transcript has them, input/output tokens and model.
- `tools/metrics.py` prints, for a period (default 7 days) and per project: hooks fired, denies/blocks per hook, exceptions and
  safety-valve releases, average hook latency (p50/p95), agent runs per agent type with total time and tokens, auto-answers
  of questions, plan-first exceptions, tasks done per sprint and ledger events (from `.harness/ledger.json`), commits with and
  without a LESSONS_LOG entry, and a "possible false positives" list = blocks followed within 2 minutes by a release or kill
  switch. Output: text table and `--json`. It reads only local files and prints no secret. The program status report format is
  NOT changed; metrics is a separate command.
- Honest limits stated in the output header: counts show activity and friction, not code quality; tokens are present only where
  the client writes them to the transcript.
- Tests: fixture log -> exact counts, percentile math against an independent computation, empty log, corrupt row skipped,
  rotation, no text fields leaked.

## REVISION 4 (after GPT-PM round 4: 3 MAJOR on T6/T7, all accepted; supersedes the conflicting lines above)
- R4-1 no prompt text anywhere. START line = `[agent START] <agent_type> | model <model> | effort <effort> | task <ledger task id or "active">`.
  The prompt, even truncated, is never printed or logged. Test: a prompt with a synthetic secret -> absent from UI text, state
  file and JSONL.
- R4-2 lifecycle states, by proof level: START_OBSERVED (hook saw the start), ACTIVE_CONFIRMED (a later event of that agent or its
  transcript activity within 10 minutes), STATUS_UNKNOWN (no event for 10+ minutes, still no STOP), FINISHED_CONFIRMED (STOP seen).
  Entries are NEVER deleted by age; an entry without STOP is shown as `<name>?` (unknown) after 10 minutes and is removed only on
  STOP or on reconciliation (the agent's transcript file shows a final message, or the parent session ended). The status line shows
  the state marker. stderr is not counted as visible to the user. For Codex a live UI experiment (spawn one agent, record what the
  operator actually sees) decides the claim; if nothing is shown, the documented result is "Codex: logged and in metrics, no
  on-screen indicator", without blocking the Claude implementation.
- R4-3 metrics storage: folder `~/.harness-hooks/metrics/` (outside every git repo; never inside a project; a test fails if the
  path resolves inside a git work tree), owner-only permissions where the OS supports it (icacls on Windows, 0600 elsewhere),
  retention 30 days (rows older are dropped at rotation; a project profile may set a shorter value, never longer), at most 5 rotated
  files of 5 MB, append and rotation guarded by the existing `_agentio.file_lock` with a short timeout (on timeout the row is
  dropped and a drop counter is incremented, nothing waits), one row per line so one corrupt line never hides the others,
  `metrics.py` skips corrupt lines and reports their count. Rows hold only: ts, client, session id, hook, event, decision, reason
  code, duration_ms, project folder name, agent_type. Telemetry failure never delays or changes any decision: every hook calls
  the helper last, inside try/except, with a 200 ms budget; T2/T3 recovery paths do not import it.

- R4-4 retention independent of rotation (replaces "dropped at rotation"): a purge pass removes rows older than the retention
  (30 days, or the shorter project value) from the active AND all rotated files, rewriting each file under the same
  `file_lock` (write temp file, atomic replace; a rotated file with no rows left is deleted). It runs (a) at the start of
  `metrics.py`, (b) on a metrics write when the marker file `purge.stamp` is older than 1 hour (so a few new events after a long
  pause purge the old data at once), (c) on demand with `metrics.py --purge`, and (d) from a Windows scheduled task
  `HarnessMetricsPurge` (daily, runs `metrics.py --purge`) that the installer creates only after the operator accepts it and
  reports in its summary; without (d), data older than the retention can stay on disk only while no Harness hook and no metrics
  command runs at all, and the docs say so. A purge error is logged to the metrics error counter and never blocks a hook.
  Tests: log under 5 MB whose last row is 31 days old -> emptied by the next write or `metrics.py` without rotation; mixed old
  and new rows keep only new; rotated file fully old is deleted; purge under lock does not lose a concurrently appended row;
  purge failure (read-only file) does not raise from a hook.

## ADDITION 2 (operator 2026-11-12, T4): "first do it yourself" and "everything not prohibited is allowed, except money"
Standing operator order, applied after the GPT-PM plan approval; it narrows T4 and adds a permission source to T5. It is part of
the code-review packet and is called out to GPT-PM there.
- T4 default is DO, not ASK: for any action that is not prohibited the agent performs it (finds and uses the keys it has,
  creates the missing state or resource, runs the CLI, changes the setting) and asks only with the gate-derived evidence of a
  real impossibility. Deny text says "do it yourself" first and lists what to try.
- Operator-only (the ask stays allowed with a one-line reason, the action is never taken by the agent): real money (purchases,
  payments, transfers, trading, paid-plan upgrades, any spend decision), plus the existing CLAUDE.md operator-only list
  (unrecoverable deletion, force-push, `git reset --hard`, `git clean -f`, deleting remote branches, production data) and the
  platform prohibitions (CAPTCHA, creating accounts, entering passwords for the operator). Money detection is data in the hook
  config (keywords: pay, purchase, buy, billing, invoice, subscription, upgrade plan, transfer, trade, order, оплат, куп, счёт,
  подписк, тариф, перевод, сделка) and matches the action or the command (for example cloud billing/commitment commands).
- T5 gets a permission source for `rotation_authorized` (c): this standing order. A confirmed exposure of a credential that
  belongs to the project in scope MAY be rotated by the agent with its working credentials in the safe order create-new ->
  switch consumers -> verify -> delete-old. Everything else in R3-2/R3-3 stays: no change without same-credential evidence or a
  confirmed exposure, false positives never unlock, nothing that costs money (for example a paid key tier) is created.
- Tests: an ask for a non-prohibited action without evidence is denied with "do it yourself"; an ask about a payment or a
  purchase is allowed with a reason; a command that creates a billable resource is denied for the agent; confirmed exposure +
  standing order -> rotation allowed in the safe order only.

Order: T6 and T7 come after T1 (T7's log is used by all other tasks' hooks, so the helper is written first, inside T6).
