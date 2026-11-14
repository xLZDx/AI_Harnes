# S20 design: one global lessons store for every project

Operator request 2026-11-14: one shared, indexed lessons file for all projects. It lists every mistake the agent made
that must not be repeated in any project. Every project writes to it, and agents read only the entries the index
finds for the action at hand, never the whole file.

## Goals

1. One store on this machine, shared by every project and by both clients (Claude Code and Codex).
2. Writing is automatic: a FAILURE or SHORTCOMING entry added to any project's `core/LESSONS_LOG.md` reaches the
   global store at commit time, without relying on the agent to remember.
3. Reading is cheap: an index of short lines (id, tags, one-line rule) is searched; only matching entries are injected
   into the agent's context, at most N per event.
4. Reading happens at defined actions, not on every prompt: before a commit, a push, a PR, a GPT-PM send, an agent
   launch, an edit of a guard or hook, and on a prompt whose words match index tags.
5. Fail open: a broken or missing store never blocks work; errors go to the hook error log.

## Storage (decision)

- Folder: `<HARNESS_HOOK_STATE>/lessons/` (today `~/.harness-hooks/state/lessons/`), outside every git repository, like
  the other shared hook state. Rejected: a file inside Harness_v2 (other projects' sessions would leave uncommitted
  changes in the Harness_v2 checkout); one file per project (no global view).
- `lessons.jsonl`: one JSON object per lesson, append-only under the existing `_agentio.file_lock`:
  `id` (L0001...), `date`, `project`, `kind` (FAILURE | SHORTCOMING | SUCCESS), `tags` (lower-case words),
  `actions` (commit, push, pr, gpt-pm, agent, test, edit-guard, edit-hook, install, delete, shell ...),
  `rule` (one sentence: what to do next time), `facts` (short text), `source` (repo path and commit sha).
- `INDEX.md`: generated, one line per lesson: `L0042 | commit,push | ci, workflow, guard | <rule>`. Rebuilt atomically
  after each append.
- `GLOBAL_LESSONS.md`: generated, human-readable, grouped by action, newest first. The operator reads this one.
- Backup: `install_claude.py` / `install_codex.py` snapshots already copy the hook state; a copy of `lessons.jsonl`
  is also committed to `Harness_v2/docs/GLOBAL_LESSONS.snapshot.jsonl` at each Harness_v2 sprint close (history kept).

## Tool `tools/lessons.py`

- `add --kind --project --tags --actions --rule --facts --source`: dedup by normalised rule text, assign the next id,
  append under lock, rebuild INDEX.md and GLOBAL_LESSONS.md atomically.
- `search --action <a> [--words "..."] [--limit 5]`: reads INDEX.md only, scores by action match plus tag and word
  overlap, prints matching rules with ids.
- `show <id>...`: prints full entries.
- `import --repo <path>`: parses an existing `core/LESSONS_LOG.md` (lines `- FAILURE|SHORTCOMING|SUCCESS <date> <scope>:
  <text> Next time: <rule>`), derives tags from the scope and words, skips duplicates. Used once for every project in
  D:\Repo (except Virtual_marketing_company and VMC_*), then by the commit hook.
- English only (git rule); the operator view adds a Russian header.

## Writing hook (automatic)

- The pre-commit path that already regenerates the status report also runs `lessons.py import --staged`: it reads the
  lines added to `core/LESSONS_LOG.md` in the staged diff and adds the FAILURE and SHORTCOMING ones (SUCCESS optional,
  tagged `success`). Never blocks the commit; errors are logged.

## Reading hooks

- `lessons_recall.py`, PreToolUse on Bash (`git commit`, `git push`, `gh pr`), on Agent launches, on Edit/Write of
  files under `harness/hooks/`, `tools/*guard*`, `.github/workflows/`, and on the PM Bridge send tool: maps the call to
  an action, runs the search, and adds at most 5 rules as additionalContext ("Global lessons for <action>: L0042 ...").
  Each lesson is shown at most once per session per action, so it does not repeat on every call.
- UserPromptSubmit: words of the prompt (Latin and Russian stems) against index tags; at most 3 rules.
- Budget: index read only, no git call, under 200 ms; kill switch `HARNESS_LESSONS_RECALL=off`.
- Same file for Claude Code and Codex (shared `_agentio` adapter), registered in `harness/hooks/registry.json`.

## Rules text

CORE.md gains one rule: "Before an action listed in the lessons index, read the injected global lessons and follow
them; a new FAILURE or SHORTCOMING goes to core/LESSONS_LOG.md and reaches the global store at commit."

## Acceptance criteria (draft)

1. `lessons.py add` twice with the same rule from two processes at once gives one entry and an intact JSONL file.
2. A commit that adds a FAILURE line to any project's `core/LESSONS_LOG.md` adds one global entry with that project
   name and the commit's source path; a commit with no new line adds nothing.
3. `search --action push` returns the push-tagged rules first and never more than `--limit`.
4. A PreToolUse `git push` in any installed project injects the matching rules once per session; a second push in the
   same session injects nothing new.
5. A missing or corrupt store gives no injection, exit 0, and one error log line.
6. The initial import of all projects runs once, reports counts per project, and is idempotent (second run adds 0).
7. `GLOBAL_LESSONS.md` and `INDEX.md` are rebuilt atomically; a crash half-way leaves the previous versions.

## Plan

S20, after S19 is approved and closed: T1 store and `lessons.py` with import; T2 commit-time writer; T3 recall hooks
and registry; T4 initial import across projects and install. Reviewers by route.py; tests by functional-test-reviewer.
