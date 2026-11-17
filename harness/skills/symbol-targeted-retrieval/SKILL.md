---
name: symbol-targeted-retrieval
description: Reduce irrelevant code context by using symbol-level retrieval and strict result scoping. Use for a large or unfamiliar codebase when a targeted function/class lookup is needed, especially if a configured Serena MCP server is available.
---

# Symbol-targeted retrieval

This is an optional *retrieval tactic*, not a replacement for `repo-recon`, `code-explorer`, or `implementation-workflow`.

## Apply when
- The task needs a few functions, classes or call sites within a large codebase.
- Repeated whole-file reads or giant search outputs would otherwise reach an expensive model.
- There is a clear functional question and an identified search area.

## Method
1. Get the repository identity, branch, HEAD, dirty files, and relevant project instructions as `repo-recon` requires.
2. Write one narrow retrieval question: target symbol/behavior, known module, expected consumer.
3. Use a configured Serena symbol search and reference lookup **only if already available and trusted for this project**. Otherwise use `rg -n`, a local language server, or `code-explorer`; do not auto-install or start a new MCP server.
4. Fetch the smallest symbol bodies and their immediate callers needed to answer the question. Keep one screen of results at a time; narrow overly broad searches before reading full files.
5. Expand to surrounding modules only when a verified dependency or test requires it. Stop once the functional question is answered.
6. Preserve exact paths and line numbers, distinguish observed source from inference, and run normal Harness verification for any subsequent edit.

## Output
- What was found: symbols/paths and evidence.
- Context scope: files and symbols read; unresolved boundaries.
- Measurement, when compared: same-model/effort input tokens, output tokens, cache reads, latency, and correctness before/after; or `NOT_MEASURED`.

## Do not
- Treat shorter search output as proof of saved money or maintained quality.
- Send private code to an unapproved cloud MCP provider.
- Edit files through Serena without ordinary project authorization and Harness gates.
- Replace the project's reviewer, test, or approval workflow.
