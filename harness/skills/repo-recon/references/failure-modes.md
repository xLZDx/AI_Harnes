# repo-recon: failure modes

Load on demand. Each line is a defect class seen in practice.

- Bare `python`/`pytest` resolves to a sibling project's venv - use the project-pinned interpreter.
- Truncated command output read as absence - re-run with a narrower query.
- A status document claims 'done' while git/CI says otherwise - git wins.
- A worktree has its own `.claude` copy - it is not a separate project.
- Generated or vendored directories dominate search results - exclude them before concluding.
