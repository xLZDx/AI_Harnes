---
name: gpt-pm-gate
description: One GPT-PM code and contract review per sprint through PM Bridge, repeated until APPROVE bound to the exact HEAD, then recorded in the ledger. Use at /ship of the sprint's final task only (every other task done), after tests are green and reviewer consensus is done, before any push or PR. Intermediate tasks never call it.
---

# gpt-pm-gate

## Apply when
Sprint code is committed, the test evidence is green (or `NOT_RUN` with reasons) and `agent-consensus` is finished. One GPT-PM review per sprint, over code and contracts only. Documents are never sent and never block the gate.

## Method
1. The tree is clean and committed. The approval binds to this exact HEAD (`git rev-parse HEAD`).
2. Build the packet: sprint tasks with acceptance criteria, the full diff against the sprint base, test commands and outputs, reviewer results with unresolved single-reviewer items as separate items, the HEAD sha. Never shorten it.
3. Round 1 asks for ONE complete adversarial sweep for BLOCKER/MAJOR: the whole mechanism, every integration and code path touching it, concurrency, failure paths, adversarial input; not only the changed lines. Say this explicitly in the request.
4. Send via PM Bridge, then wait for the reply. Do not poll or resend; one send is one durable job.
5. Not APPROVE: verify each finding against primary evidence (file, line, run). Fix the whole valid BLOCKER/MAJOR set in one batch and commit.
6. Later rounds check only those fixes and regressions caused by them. Unrelated improvements go to the backlog. Repeat until APPROVE.
7. APPROVE: record it with `python .harness-tools/ledger.py approve --evidence "<GPT-PM verdict reference>"`. Then push and open one PR per sprint.
8. Any new commit changes HEAD and invalidates the approval; code changes after APPROVE need a new round limited to the delta.

## PM Bridge tool notes
- `gpt_send(text, transport?, open_tab?, project?)` then `gpt_await_reply(transport?, timeout_s?, poll_ms?)`. Omit `transport` to use the default automatic one. `"clipboard"` only copies the text and sends nothing; do not choose it yourself.
- `gpt_ingest_reply(text)` logs a reply the operator pasted into the chat.
- Sessions: `pm_bridge_session_start()` holds one browser open across several sends; `gpt_session_peek(project?)` checks a reply later without blocking; always call `pm_bridge_session_end()` when the block is done, or other browser users queue and time out. Only one shared session at a time.
- `pm_bridge_status(limit?)` shows recent message history. Use it, or peek, before re-sending after a cancelled or slow await.
- A reply read via await or peek returns whatever the last message is; fact-check content instead of trusting freshness.
- Use the registered project conversation, not UI titles as identity. Never test in a live project chat.
- If the tools are missing, the MCP server was registered after the session started; tell the operator to restart.
- Ask for a devil's-advocate stance: look for reasons the change is wrong; if none is real, say so plainly.

## Rules
- A GPT objection is a finding to verify, not a prohibition. A verified safety or legal constraint is reported to the operator.
- Never hand-edit the ledger approval; never claim approval without a bare APPROVE reply for the current HEAD.
- Do not run reviews under a model outside the allowed list.

## Required output
Verdict, HEAD sha, request and reply ids, findings by severity with disposition, what is still unverified.

## Do not
Send documents, ask GPT-PM to review each task, send a shortened packet, resend while a job is pending, push before the ledger shows the approval.
