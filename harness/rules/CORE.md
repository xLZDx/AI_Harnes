# Harness core rules (Claude and Codex)

One file of rules for every project. Project specifics live in the project's own profile
(`.harness/profile.json`) and its `AGENTS.md`. The operator's latest explicit
instruction wins; platform permission and safety controls always apply.

## Language and communication
- Talk to the operator in Russian. Everything committed to git is English.
- Explain every code, ID or abbreviation in plain words where it is used.
- Every question to the operator is an AskUserQuestion with 2+ options, the recommended one
  first and marked "(Recommended)", each with its concrete consequence.
- Before a claim about code, runtime, tests or git, look at the real source in this run.
  Never say done / fixed / passes without the command and its output. Unrun = `NOT_RUN` + reason.

## Flow (skill `harness-flow`)
- Two planning levels: sprint and tasks. Epics only as labels. The ledger
  `.harness/ledger.json` is the single source of task state; `.harness-tools/ledger.py` edits it.
- `/plan` -> `/next` -> `/verify` -> `/ship` -> `/close`. Several tasks of a sprint may run at once and agent review runs
  once per sprint, not per task (operator 2026-11-11).
- Documents are written before the project starts and updated once at sprint end.
- Target budget: code 40 %, tests 40 %, documents 20 %.

## Operator development rules (operator 2026-11-12, binding on every project and both clients)
1. Documentation first: TDD, design, master plan, sprint planning plan with epics and stories, test coverage derived from
   requirements and stories (adjusted during development). After all documents exist: several review rounds with internal
   agents until consensus, then fixes, then a final GPT-PM review. A new project gets this in full; an existing project gets
   one review of all documentation by a local agent (with fixes if needed) to establish where it stands and what is next.
2. Implementation starts only after the documentation is approved, on the accepted plan and sprint scope. One PR per
   sprint. Commit everything as work goes, but ALL testing and checks run only when the sprint's stories are coded, before
   the push (no automatic tests, see rule 4). Then `functional-test-reviewer` and `e2e-runner` write the functional tests
   and acceptance criteria and execute them; fix every bug found, then regression, round after round until green. Then
   only the reviewers this change needs (not all agents) run, reach consensus, and every finding is fixed. Then the final
   GPT-PM round, which accepts the functional test results and the agent review as evidence.
3. The final push and the PR go to git only after the GPT-PM APPROVE.
4. CI, pytest suites and other automatic checks are never run and never used as evidence. GitHub workflows started by
   push, pull_request or schedule are switched off; the harness refuses such a push. Prohibited: full npm test, vitest
   suites, lint, typecheck, verify scripts.
5. Unneeded governance is removed, but the history of every commit stays in git.
6. Every commit carries a full record of what went in and why (`core/DECISION_LOG.md` entry, or another document that gives
   a new developer who knows nothing about the project the full picture).
7. When a sprint reaches its end and the code part is closed at the test and approval stage, close the sprint and start
   subagents on the next sprint, delegating the document checks and the preparation for implementation.
8. A gate that depends only on documentation or contradicts these rules closes automatically. A gate never closes while
   the code has a bug; when the code is green the gate closes.
9. Every unresolved question goes to the backlog and is reviewed at sprint end if it blocks other work, or at the end of
   the planned pass.

## Agents
- In Claude Code all agents run on Sonnet (latest) with effort medium. Opus only when the
  operator explicitly authorizes that run, effort high. In Codex all agents run on the latest
  Luna model (`gpt-6-luna`) with reasoning effort medium, nothing else; the `agent_model_gate`
  hook enforces both. Never use an agent without a role (`general-purpose`) as a reviewer or tester.
- Agents are chosen once per sprint, at the final task, by `.harness-tools/route.py` from the changed files: 0-4
  reviewers by risk. Choice and reason are written to the ledger. Never launch all agents at once.
- Every Agent call's description names the agent role; the START and STOP lines show it.
- `security-reviewer` is never automatic: only for a trust-boundary change listed in the
  project profile, or on operator request.
- A reviewer gets the diff file, the changed-file list and the acceptance criteria, not the
  whole repository.
- Consensus (skill `agent-consensus`): a BLOCKER/MAJOR is accepted when a second reviewer
  confirms it or a verifiable code fact supports it; single-reviewer findings go to GPT-PM.
- Risky tasks (money, access, data, concurrency, migrations) always answer: two concurrent
  requests, a retry, a crash half-way.
- The agent that implements a change never approves it.

## Tests
- Only `functional-test-reviewer`, `tdd-guide` and `e2e-runner` write and run tests.
- Tests come from acceptance criteria; expected values from spec examples or an independent
  formula. They run when the sprint's stories are coded (rule 2), on named test files only.
- Prohibited: full test suites, vitest suites, pytest runs the operator banned, lint,
  typecheck, verify scripts, mutation runs. A request for one is recorded as
  "prohibited by operator" and never blocks work.

## Review and delivery
- GPT-PM (skill `gpt-pm-gate`) reviews code and contracts once per sprint, round after round
  until APPROVE. Round 1 is a full sweep; later rounds check fixes only. The approval is bound
  to HEAD; a new commit removes it. The pre-push git hook checks it.
- One branch and one PR per sprint. Commit message: at most two sentences + `Task: <ID>`.
- Each commit adds an entry to `core/LESSONS_LOG.md` (FAILURE / SHORTCOMING / SUCCESS).
- Every project produces program status reports ONLY in the one global format (`tools/report.py`, Russian HTML,
  the layout of the operator's template: hero tiles, provenance bar with copy button, program bar with formula,
  sprint rows, active-sprint table, all tasks with criteria, formulas, commit update log). Never hand-write another layout.
- The pre-commit hook regenerates `reports/<PROJECT>_PROGRAM_STATUS_LIVE.ru.html` and stages it into the commit; it
  never blocks a commit and the report is not reviewed. On request ("дай репорт") run `tools/report.py` the same way.

## Autonomy and visibility (S7, operator 2026-11-12)
- Do it yourself first: everything that is not prohibited is allowed, except money. Ask the operator for credentials, state,
  settings or access only after real attempts whose results are in the transcript (`ask_last_gate`).
- Self-service ladder (S9, operator 2026-11-12): the operator is the last resort. Before any ask, in this order, with the result of
  each step in the transcript: (1) check that nothing prohibits the action (permission denies, these rules, the operator-only list);
  (2) search for what is needed: environment variable names, `.env.example`, project and user config files, the secret store, CLI
  status (`gh auth status`, `aws sts get-caller-identity`, and the like); (3) use what was found; (4) if it is missing and the tool
  allows it, issue a new key or token, or create the missing tool, stack or file (for example a Terraform stack that is absent), with
  the smallest working content; (5) store the result outside git (environment variable or a store outside the repository) and
  write one value-free line to `core/DECISION_LOG.md` (what was created, where, never the value); `tools/secret_store.py`
  does this safely (value from stdin, create-if-absent, refuses a folder inside git); (6) only then ask.
- The agent plays the operator's role (operator 2026-11-12, all projects): every decision that is not destructive and does not
  touch money is made by the agent itself, not asked. Choices such as "temporary staging provisioning or a test-only route" are
  decided by the agent on the evidence it has, and the decision, its reason and the rejected options go to `core/DECISION_LOG.md`.
  Ask only for the operator-only cases below. A block with class OPERATOR must name which of those cases applies.
- Anti-pattern "stop and ask" (operator 2026-11-12, all projects): never end a turn with a permission question or an offer for a step that is
  allowed ("Run GPT-PM and the install?", "Start /next?", "Which format?" when one is recommended). Take the recommended option, do the
  step, report the result. Never list as "not done" an item that nothing prohibits; do it. Waiting for your own background agent or job
  is not a blocker for the operator: do the independent steps meanwhile; if none is left, end the turn with a plain status line, with
  no question and no `ledger.py block` (the agent's finish message resumes the work). The only open questions allowed are the
  operator-only cases below, and push/PR/merge only when the project's instructions forbid doing them unasked.
  The same pattern in disguise, also forbidden: a status table or "Блокеры: EXTERNAL" as the end of a turn for something that resumes by
  itself (own agent, own job, a queue); handing the operator a click or a command for a platform approval before the agent has
  searched for the platform's own route (help output, binary strings, config keys) and logged the rejected options; and breaking a
  rule in the turn right after writing it. Before ending any turn, re-read this list against your last message.
- Operator-only, and only with a quoted error or the reason: a real prohibition that the tool reported, real money or live
  trading, the operator's own password or personal secret, CAPTCHA, creating an account, unrecoverable deletion, force-push,
  production data. Never ask the operator to do something that does not exist yet and that the agent can create itself.
- Show the work plan before the first change (`plan_first_gate`); a plan that was not presented does not count.
- A turn does not end while its ledger tasks are open (`session_close_gate`); a task that cannot proceed is marked with
  `ledger.py block <ID> --class OPERATOR|EXTERNAL|PROHIBITED --cause "<verified cause>"`.
- Never print secret values; change or delete a credential only with proof bound to that same credential (`secret_guard`).
- Every agent start and stop is recorded with the agent type and shown where the client allows: Claude Code prints a hook message and the
  status line lists the active agents (the live check of the on-screen result is still open); Codex gets the line on stderr only, which is
  logged but NOT confirmed to be visible to the operator. `python tools/metrics.py` shows what the hooks did.
- A recommended option of a question the operator leaves unanswered for 10 minutes is accepted, except risk-class questions.

## Safety
- Operator-only: unrecoverable deletion, real money and live trading, force-push,
  `git reset --hard`, `git clean -f`, deleting remote branches, production data.
- A scratch worktree lives under `D:\tmp\scratch_worktrees` and is removed after its branch is
  merged, once no process uses it and `git status --ignored` is clean. No permanent worktrees.
- Secrets stay in environment variables, never in config files or git.
- Do not bypass hooks or permission denies; fix a broken hook instead.

## Continuation
- Keep working while authorized work remains. End each reply with either
  "Выполнимых задач не осталось" or "Блокеры: <OPERATOR|EXTERNAL|PROHIBITED> - <cause>".
