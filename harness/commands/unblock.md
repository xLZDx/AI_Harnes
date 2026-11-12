---
description: Get what you need yourself before asking the operator - search, use, create, store outside git, then ask.
argument-hint: [what is missing - key, access, tool, file, state]
---
Follow the "Self-service ladder" in the shared rules. The operator is the last resort.

1. Name what is missing (from the argument or from the failure in front of you).
2. Check that nothing prohibits the action: permission denies, the rules, the operator-only list
   (real prohibition with a quoted error, money, personal password or secret, CAPTCHA, creating an
   account, unrecoverable deletion, force-push, production data). If it is operator-only, stop and say why with the quote.
3. Search with real commands: environment variable names, `.env.example`, project and user config,
   the secret store, CLI status (`gh auth status`, `aws sts get-caller-identity`, and the like).
4. Use what you found. If it is missing and the tool allows it, issue a new key or token, or create
   the missing tool, stack or file with the smallest working content.
5. Store the result outside git (environment variable or a store outside the repository). Add one
   value-free line to `core/DECISION_LOG.md`: what was created and where.
6. Report in three parts: tried / result / stored where. Never print a secret value. Only if every step failed, ask the
   operator with AskUserQuestion, quoting the failed results.
