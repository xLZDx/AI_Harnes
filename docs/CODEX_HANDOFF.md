# Handoff to the Codex agent: Harness v2 on the Codex side

Written 2026-11-13 by the Claude Code session that built and installed Harness v2. Read this whole file before you act.
You are the Codex agent. Your job: check the Codex half of the harness from inside Codex (things Claude cannot observe),
fix what is broken on the Codex side, and report. The operator stopped all project sessions until the harness rules are
proven to work in every project, for both clients.

---

## 0. Rules you must follow (from ~/.codex/AGENTS.md and the operator; they win over this file only if newer)

- Talk to the operator in Russian. Everything committed to git is English.
- Any claim about code, runtime, tests or git needs a command and its verbatim output from your own run. Unrun = `NOT_RUN` + reason.
- Codex subagents only on `gpt-6-luna`, reasoning effort medium (the `agent_model_gate` hook enforces it).
- Never write Codex hook trust: no edits of `[hooks.state.*]` `trusted_hash` / `trusted_content_hash` in
  `~/.codex/config.toml`, no new `[projects.'...'] trust_level` entries, never `--dangerously-bypass-hook-trust`.
  Hook trust and folder trust are the operator's platform safety review. You may READ them.
- Prohibited by the operator (never run, never use as evidence): CI, full pytest/npm/vitest suites, lint, typecheck,
  verify scripts. A single named test file run with `C:\Python314\python.exe -I tests\test_x.py` is allowed, and only
  the test roles (functional-test-reviewer, tdd-guide, e2e-runner) write and run tests.
- Operator-only: force-push, `git reset --hard`, `git clean -f`, `git branch -D`, deleting remote branches, unrecoverable
  deletion, production data, real money, merging PRs. Never push the `dev-history` branch.
- Never touch `D:\Repo\Virtual_marketing_company` or any `D:\Repo\VMC_*` folder. Do not change the PM Bridge
  production checkout `D:\Repo\pm-bridge` without an explicit operator instruction.
- Secrets: never print, commit or repeat a value.
- Every commit stages an entry in `core/DECISION_LOG.md` and in `core/LESSONS_LOG.md` (FAILURE / SHORTCOMING / SUCCESS)
  of the repo it changes, in the same commit. Message: at most two sentences + `Task: <ID>` trailer.
- Do it yourself first; ask the operator only for operator-only items, with the quoted error. Never end a turn with a
  question about an allowed step. End each reply with `Выполнимых задач не осталось` (only if true) or
  `Блокеры: <OPERATOR|EXTERNAL|PROHIBITED> - <cause>`.

Machine facts:
- Python: always `C:\Python314\python.exe` (bare `python` may be another venv; `py -3` points to a missing 3.15). Use `-s`
  to skip slow user-site startup.
- Python writes CRLF by default here; pass `newline="\n"` when a repo pins LF.
- Shell commands with `$(...)` are denied by the go_gate hook. Do not pass code with backslash escapes through heredocs;
  write a script file instead.
- Norton re-signs outbound TLS; never use `verify=False`.
- Codex binary (v0.167.0):
  `D:\npm-global\node_modules\@openai\codex\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe`
- Several Claude/Codex sessions may share `D:\Repo` checkouts. Check `git status` first. Do code changes in a scratch
  worktree under `D:\tmp\scratch_worktrees\`, never on a branch another session has checked out.

---

## 1. How the harness reaches Codex (architecture)

Source repo: `D:\Repo\Harness_v2` (public: https://github.com/xLZDx/AI_Harnes.git).

- `harness/hooks/*.py`: one file per hook for both clients. `harness/hooks/_agentio.py` is the only reader of
  client-specific input:
  - shell tool is `Bash` in Codex (PowerShell under it on Windows);
  - edits are `apply_patch`, with the patch text in `tool_input.command` or `tool_input.input`;
  - a spawn is `spawn_agent` with `agent_type`;
  - the transcript is `response_item` JSONL.
  The client is known from the `--harness-client=codex` argument that build.py writes into every hook command. Without
  that argument, only evidence in the payload counts: a `/.codex/` transcript path or a `turn_id`.
- `harness/hooks/registry.json`: every hook with its event and matcher per client. `tools/build.py` writes
  `out/codex/hooks.json`, `out/codex/AGENTS.md`, `out/codex/agents/*.toml` (Luna medium), skills and prompts.
  It also writes `codex_policy.json`, the required model and effort for agent_model_gate.
- `tools/install_codex.py --home C:\Users\koros\.codex --snapshot <new folder>`:
  - takes a full snapshot with `restore.ps1` first;
  - overwrites `AGENTS.md`, `agents/`, `skills/`, `prompts/` and `hooks/`;
  - merges `hooks.json`: it drops entries that run a replaced script and keeps other entries, such as the operator's
    own SessionEnd hook;
  - never edits `config.toml` (it requires `[features] hooks = true`) and never writes trust.
- `tools/build.py --install-project D:/Repo/<p> --force` installs per project. For Codex it writes:
  - `<p>/.codex/agents/*.toml`;
  - `<p>/.agents/skills/<n>` (Codex reads project skills there);
  - `<p>/.codex/hooks.json`, generated from `<p>/.claude/settings.json`. Agent becomes spawn_agent, Bash/PowerShell
    becomes Bash, edit tools become apply_patch, and tools Codex lacks are dropped. Every project script runs through
    `~/.codex/hooks/codex_spawn_shim.py`, which has a timeout, fails closed with exit 2 and turns a project `ask` into
    a deny.
- Codex-only differences, by design:
  - agent_model_gate checks `spawn_agent`. An explicit model or effort that differs is denied, and so is an agent TOML
    that declares something else. A spawn where neither the call nor a TOML gives the values is also denied.
  - plan_approval_gate and the AskUserQuestion part of question_policy are Claude-only; Codex has no such tools.
  - Codex has no Notification event.
- `tools/audit_projects.py` checks every project and both client homes.

Live Codex user hooks today (`~/.codex/hooks.json`, 33 handlers):
- SessionEnd: the operator's own cleanup hook.
- PreToolUse: shell policy, operator rules gate, repeat work gate, decision log gate, gpt review gate, worktree
  policy, secret guard, GO gate, plan first gate, agent model policy.
- PostToolUse: secret scan, PR event.
- Stop: ask last check, plan first check, citation check, unverified claim check, session supervisor, keep-going
  check, project plan close gate, decision refs check, closing claim check, question policy, worktree cleanup check.
- PreCompact: decision log.
- SubagentStop: citation check, citation judge, agent stop line.
- SubagentStart: agent start line.
- UserPromptSubmit: decision log reminder, repeat work check, question policy report, worktree reminder.

---

## 2. History of the Codex work (what was done, including my mistakes)

1. 2026-11-10: `~/.codex` was converted to the harness core:
   - AGENTS.md;
   - 21 agents as TOML (gpt-6-luna, medium);
   - skills and prompts;
   - `[features] hooks = true`, `default_subagent_reasoning_effort` changed to medium.
   Snapshot: `D:\Backup\codex-pre-harness-v2-20261110-212855`.
2. S3, 2026-11-11: build.py started generating the Codex agents. Project `.codex/agents` were regenerated in 7
   projects; old files are kept in `D:\Backup\codex-project-agents-pre-harness-v2-20261111`. One hook source now
   serves both clients. Project skills and hooks were ported to Codex; backup in
   `D:\Backup\codex-project-extras-pre-harness-v2-20261111`.
3. S9: I said Codex 0.167 has no `/hooks` command. **Wrong.** I only checked `codex --help`. `/hooks` is a slash
   command inside the interactive CLI, and the app has Settings -> Hooks and a "Review hooks" button. Corrected in S11.
4. S9: I tried 13824 serialisations to reproduce Codex's trust hash and none matched. Decision: never compute or write
   hashes.
5. S13-R7: hooks.json was installed with rules_gate. Audit `codex-home gates_live OK`: every prohibited command was
   denied when the hook script was run directly.
6. S13: `closing_claim_gate` was added. It is a Stop hook that blocks the closing claim "Выполнимых задач не осталось"
   while open items or uncommitted changes remain.
7. S14 close, 2026-11-12 22:29: Claude, Codex and all 17 harness projects were installed.
   Snapshots: `D:\Backup\harness-snap-20261112222941-claude` and `...-codex`.
8. 2026-11-12: the operator saw the Codex "Review hooks" dialog, with *closing claim check* Modified, *question policy*
   Modified and *worktree cleanup check* New, and accepted it. My audit had said "untrusted 1 of 33". **Wrong
   method**: `chk_codex_cfg` counts a hook as trusted when any `trusted_hash` exists, so Modified hooks pass (backlog
   B21).
9. I found the authoritative source: `codex app-server` JSON-RPC `hooks/list`. Its results are in section 3.

---

## 3. Verified state now (from `codex app-server` `hooks/list`, run 2026-11-13)

```
AI_trading_assistance {'trusted': 33, 'untrusted': 2} {'user': 33, 'project': 2} warn [] err []
    ('untrusted', 'D:\\Repo\\AI_trading_assistance\\.codex\\hooks.json', ['pre_tool_use', '0', '0'], 'agent-spawn gate')
    ('untrusted', 'D:\\Repo\\AI_trading_assistance\\.codex\\hooks.json', ['user_prompt_submit', '0', '0'], 'reset agent-spawn budget')
Fitness_App {'trusted': 33} {'user': 33} warn [] err []
db-test-tool-analysis {'trusted': 33} {'user': 33} warn [] err []
(all other 15 folders: {'trusted': 33} {'user': 33} warn [] err [])
```

- All 33 user hooks are trusted in all 18 folders.
- In AI_trading_assistance, 2 project hooks are untrusted, so Codex does not run them there yet.
- Fitness_App and db-test-tool-analysis each have `.codex/hooks.json` (a spawn_agent gate through codex_spawn_shim),
  but Codex does not load them: `hooks/list` shows only the 33 user hooks in those folders (backlog B22).
- `~/.codex/config.toml` `[projects]` has explicit trust entries only for `c:\`, `d:\repo`, `d:\repo\aeve`,
  `d:\repo\personal_decision_command_center`, `d:\repo\erp` and `d:\repo\ai_trading_assistance`. Fitness_App and
  db-test-tool-analysis have none. This is the only visible difference from AI_trading_assistance, and it is
  **unproven** as the cause: a `-c projects...trust_level=...` override has no effect on app-server.
- config.toml keeps 3 trust-state entries for handlers that no longer exist (`stop:1:0`, `stop:1:1`,
  `subagent_stop:1:0`). They are harmless; leave them.
- `[hooks.state]` key format: `'<hooks.json path>:<snake_case event>:<group index>:<handler index>'`.

Probe script (read-only, no trust written). Save it as a file and run it with
`C:\Python314\python.exe -s probe.py "{\"cwds\": [\"D:/Repo/Fitness_App\"]}"`:

```python
import json, os, queue, subprocess, sys, threading
exe = r"D:\npm-global\node_modules\@openai\codex\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe"
p = subprocess.Popen([exe, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     text=True, encoding="utf-8")
q = queue.Queue()
threading.Thread(target=lambda: [q.put(l) for l in p.stdout], daemon=True).start()
def send(o): p.stdin.write(json.dumps(o) + "\n"); p.stdin.flush()
def wait(i):
    while True:
        try: o = json.loads(q.get(timeout=30))
        except ValueError: continue
        if o.get("id") == i: return o
send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"clientInfo": {"name": "harness-audit", "title": "harness audit", "version": "1.0"}}})
wait(1)
send({"jsonrpc": "2.0", "method": "initialized"})
send({"jsonrpc": "2.0", "id": 2, "method": "hooks/list", "params": json.loads(sys.argv[1])})
r = wait(2); p.kill()
for b in r["result"]["data"]:
    print(b["cwd"], [(h["source"], h["trustStatus"], h.get("statusMessage")) for h in b["hooks"] if h["source"] != "user"],
          "user:", sum(h["source"] == "user" for h in b["hooks"]), "warn", b["warnings"], "err", b["errors"])
```

Each hook entry has `key`, `source` (user/project), `sourcePath`, `currentHash`, `trustStatus`, `statusMessage` and
`enabled`.

---

## 4. Your tasks

Do them in this order. Read-only checks first.

### C1. Prove the hooks really fire in a live Codex session (only you can see this)

Claude proved the hooks by calling each script directly. Nobody has yet watched them fire inside a real Codex turn.
In a fresh Codex session in a scratch folder, or in `D:\Repo\Harness_v2` without committing, show for each item what
Codex actually did, with verbatim output:

1. **PreToolUse / rules_gate.** `git push` from a repo without a GPT-PM approval must be denied. Do not push; a dry
   form is enough if it is denied. A command with `$(...)` must be denied by go_gate.
2. **agent_model_gate.**
   - `spawn_agent` with a model other than gpt-6-luna, or an effort other than medium, must be denied.
   - A spawn with neither value and no TOML must be denied.
   - A spawn with gpt-6-luna medium must be allowed.
   Record the exact `spawn_agent` tool_input field names Codex sends; `_agentio.py` assumes `agent_type`, `model` and
   effort.
3. **SubagentStart / SubagentStop.** Do the "agent start line" and "agent stop line" (agent_visibility) appear, and
   where? CORE.md says that in Codex the line goes to stderr and that it is **not confirmed** to be visible to the
   operator. Confirm or refute it.
4. **PostToolUse.** Record the real payload shape of a shell tool response, with secret values redacted. It was never
   verified (DECISION_LOG S5 "Codex PostToolUse shape"); `secret scan` and `PR event` parse several shapes by guess.
5. **Stop.** Show the closing claim check blocking: end a turn with "Выполнимых задач не осталось" while you have an
   uncommitted tracked change in a scratch repo. Show question policy and keep-going firing.
6. **Transcript reading.** Hooks read the Codex transcript as `response_item` JSONL (`_agentio._codex_records`). Check
   that citation check and unverified claim check see your last assistant message. Their block messages quote it.
7. **UserPromptSubmit.** Show that the reminders arrive as additional context.

For every hook that does not fire or misreads the payload, open a defect with the payload sample and the file and line.

### C2. B22: why Codex does not load the project hooks of Fitness_App and db-test-tool-analysis

Find the real loading rule for project `.codex/hooks.json`. Possible causes:
- project folder trust;
- the project root marker (git root or `.codex` folder);
- a schema difference in the file;
- a feature flag.

Sources: the Codex docs, `codex.exe` strings, and app-server `hooks/list` warnings with different `cwds`, for example
a subfolder or the git root. Compare against AI_trading_assistance, which is loaded. All three files have the same
shape (see section 1).

Do not add trust entries yourself. If the answer is folder trust, tell the operator exactly which folders to trust and
how (CLI: open Codex in the folder and accept; app: the folder trust prompt). Then re-run the probe and show the
project hooks listed.

Also check that the interpreter the AI_trading_assistance hooks call exists:
`D:\Repo\AI_trading_assistance\venv312\Scripts\python.exe`. It existed on 2026-11-13. If it goes missing, the shim
fails closed and every spawn_agent there is denied.

### C3. AI_trading_assistance: 2 untrusted project hooks

The operator reviews them in Codex. Codex shows the dialog when it is opened in that folder. After that, re-run the
probe and record `trusted` for `pre_tool_use:0:0` and `user_prompt_submit:0:0`.

### C4. B21: make the audit ask Codex itself (Harness_v2 code change, sprint S15)

`tools/audit_projects.py` `chk_codex_cfg` (about line 412) re-derives trust from config.toml and counts any existing
`trusted_hash` as trusted. Replace that with the `hooks/list` probe:
- per project `cwd`, report user and project handlers whose `trustStatus != "trusted"`;
- report `warnings` and `errors`;
- report project hooks.json files that exist but are not listed (the B22 case);
- give a clear `FAIL` when app-server cannot start, never a silent OK.

Related: B19 is a false `gates_live` FAIL "not denied: git push" for claude-home and codex-home. It happens because
the probe runs in the Harness_v2 checkout, whose HEAD carries a real GPT-PM approval. Probe from a temp repo without
an approval instead.

This is code. It follows the harness flow:
1. `python tools/ledger.py` plan task for S15.
2. Implement in a scratch worktree from branch `s15-backlog`.
3. Test agent on a named test file.
4. Reviewers chosen by `tools/route.py`.
5. GPT-PM via PM Bridge until APPROVE bound to HEAD.
6. Push and PR only after the approval.

If you change any hook source, rebuild and reinstall both clients: `tools/build.py`, then
`tools/install_claude.py --snapshot <new>` and `tools/install_codex.py --snapshot <new>`, then
`build.py --install-project` for the 17 projects. Claude and Codex must stay on the same hook set.

### C5. Fix whatever C1 shows is broken on the Codex side

Typical places:
- `_agentio.py` (payload shapes);
- `registry.json` matchers for Codex;
- `codex_spawn_shim.py`;
- `build.py` (the generated Codex hooks.json and project hooks).

The fix goes through the same S15 flow. Every new or changed hook needs the operator's review in Codex again
(Modified/New); tell them, do not bypass it.

---

## 5. Where things are

| What | Path |
|---|---|
| Hook sources, registry | `D:\Repo\Harness_v2\harness\hooks\` |
| Build, installers, audit | `D:\Repo\Harness_v2\tools\build.py`, `install_codex.py`, `install_claude.py`, `audit_projects.py` |
| Backlog (B19-B22) | `D:\Repo\Harness_v2\docs\BACKLOG.md` |
| Decision history | `D:\Repo\Harness_v2\core\DECISION_LOG.md` (search "Codex") |
| Codex home | `C:\Users\koros\.codex\` (`AGENTS.md`, `hooks.json`, `hooks\`, `agents\`, `config.toml`) |
| Machine hook config | `C:\Users\koros\.harness-hooks\config.json` |
| Project Codex hooks | `D:\Repo\<project>\.codex\hooks.json` (only AI_trading_assistance, Fitness_App, db-test-tool-analysis have them) |
| Snapshots | `D:\Backup\harness-snap-20261112222941-codex` (has `restore.ps1`) |

Git state of Harness_v2:
- Branch `s15-backlog` is local and not pushed. It holds the backlog and log commits after S14.
- PRs #1-#4 are open; merging them is the operator's step.
- S14 was approved on 570f6ca and pushed.

---

## 6. What to report back to the operator (in Russian)

For C1-C5:
- what you checked;
- the verbatim output;
- what you fixed, with commit ids;
- what is still open.

Name each open item's class: OPERATOR (with which operator-only case applies), EXTERNAL or PROHIBITED.
