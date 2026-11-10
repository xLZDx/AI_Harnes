---
name: workspace-docgen
description: Reverse-engineer a repo into 3 grep-friendly reference docs (TECHSTACK / CODEMAP / DEPENDENCIES, optionally ARCHITECTURE / CONTEXT) under core/. Current-state only, never overwrites human content. Use to onboard a repo or give Codex/Copilot the same map. Cherry-picked from Grid Dynamics Rosetta init-workspace pipeline.
---

# Workspace DocGen

Role: workspace cartographer. Source code is truth; existing docs may be stale.
Turns a repository into a small, standardized, grep-friendly reference set that every
future session (Claude Code, Codex, Copilot) reads instead of re-discovering the codebase.

Cherry-picked and trimmed from Grid Dynamics **Rosetta** (Apache-2.0) `init-workspace-discovery`
+ `init-workspace-documentation`. The Rosetta-adoption plumbing (gain.json / rules-copy /
shells) is intentionally dropped — this is doc value only, no framework dependency.

## Targets (default dir: `<project>/core/` ; configurable)

- **TECHSTACK.md** — languages, frameworks, build tools, runtimes + key stack decisions.
- **CODEMAP.md** — 3-4 levels deep. Headers = repo-relative path + recursive file count +
  `<10-word description`; list immediate child filenames only.
- **DEPENDENCIES.md** — flat list of DIRECT deps (name, version), per package manager.
- (optional) **ARCHITECTURE.md** / **CONTEXT.md** — only if the repo lacks an equivalent.

## Generation (Windows-first)

1. Enumerate files honoring `.gitignore`:
   `git ls-files --cached --others --exclude-standard`
   (fallback: `Get-ChildItem` / `find` with cache/build/binary exclusions).
2. Build the CODEMAP tree with recursive per-directory counts (awk over the file list;
   no ASCII-art). Any helper script goes to a TEMP dir on D:, never C:. A `.ps1` helper
   MUST be UTF-8-with-BOM + ASCII-only (Windows PS-encoding rule).
3. Read manifests for DEPENDENCIES (pubspec.yaml, package.json, requirements.txt,
   pyproject.toml, go.mod, Cargo.toml, *.csproj — whatever the stack uses).
4. Mode: `install` = create all; `upgrade` = fill gaps only. NEVER overwrite a
   human-authored section — merge alongside. Report created / updated / skipped.

## Discipline

- CODEMAP / TECHSTACK / DEPENDENCIES = CURRENT STATE ONLY. No deltas, no changelog, no "why".
- Emit a CSV twin for any table-shaped doc (CSV-twin rule).
- Do NOT generate changelog / assumptions / todo / memory docs — those live in the
  memory system + the project CLAUDE.md "Current System State". No duplication.
- Grep-friendly headers; the shorter the better.
- One-time per repo; re-run in `upgrade` mode when the stack changes.

## Cost

Discovery is cheap (a handful of read-only shell commands + manifest reads). The deep
ARCHITECTURE / CONTEXT pass is heavier — run it only when needed.

## Install

To activate in Claude Code, copy the folder to `~/.claude/skills/workspace-docgen/`
(operator step — `~/.claude` is on C:, Write-denied to the assistant). For Codex / Copilot,
paste the body above as a project prompt / instruction file.
