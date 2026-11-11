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
- `/plan` -> `/next` -> `/verify` -> `/ship` -> `/close`. One task at a time.
- Documents are written before the project starts and updated once at sprint end.
- Target budget: code 40 %, tests 40 %, documents 20 %.

## Agents
- In Claude Code all agents run on Sonnet (latest) with effort medium. Opus only when the
  operator explicitly authorizes that run, effort high. In Codex all agents run on the latest
  Luna model (`gpt-6-luna`) with reasoning effort medium, nothing else; the `agent_model_gate`
  hook enforces both. Never use an agent without a role (`general-purpose`) as a reviewer or tester.
- Agents are chosen per task by `.harness-tools/route.py` from the changed files: 0-4 reviewers by
  risk. Choice and reason are written to the ledger. Never launch all agents at once.
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
  formula. Task tests run once per task; the full own set once at sprint end.
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
