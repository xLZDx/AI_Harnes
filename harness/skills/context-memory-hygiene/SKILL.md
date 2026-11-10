---
name: context-memory-hygiene
description: Keep instruction and memory context compact, current and correctly scoped. Use when editing CLAUDE.md, AGENTS.md, memory, agent or skill prompts.
---

# context-memory-hygiene

## Apply when
Editing CLAUDE.md, AGENTS.md, memory files, agent or skill prompts.

## Method
1. Global files hold only universal rules; project facts stay project-local; one rule lives in one place (link, do not copy).
2. Keep durable decisions and canonical pointers, not status history; mark superseded text and move it to a dated history file.
3. Budgets: global CLAUDE.md <= 6k tokens, project CLAUDE.md + AGENTS.md <= 4k. Procedures go to skills, knowledge to references.
4. Verify with `agentctl.py --strict`.

## Required output
Before/after token counts, what moved where, lint result.

## Do not
Delete history; duplicate a rule across memory, CLAUDE.md and agents; store branch HEADs or pass counts in memory.

Typical failure modes: `references/failure-modes.md` (read only when the work hits one of them).
