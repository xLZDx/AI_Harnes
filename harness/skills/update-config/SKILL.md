---
name: update-config
description: Narrow audited edit to permissions.deny entries only, after explicit action-specific operator consent for the exact rule change.
---

# update-config — front door

Use this skill only when the operator has explicitly authorized, in the current conversation, a specific change to one or more named permissions.deny entries.

Allowed scope:
- the global Claude settings file or an explicitly scoped project settings file;
- permissions.deny entries only;
- the exact named entry or an explicitly requested narrower replacement.

Do not use this skill for allow lists, hooks, model policy, unrelated settings keys, or unrelated deny rules.

Procedure:
1. Read the current settings file and locate the exact target.
2. If the request could map to several entries, identify the ambiguity instead of guessing.
3. Record the exact before and after state.
4. Make the smallest edit; prefer a narrower rule when it fully meets the operator's request.
5. Append an audit entry to ~/.claude\logs\update-config-audit.log with timestamp, project, file, exact change and stated reason.
6. Re-read the file and confirm no unrelated settings changed.

This skill does not create authorization; it applies authorization that is already explicit.

Read references/full-2026-10-02.md only for historical examples or legacy rationale.
