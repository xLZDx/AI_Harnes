#!/usr/bin/env python3
r"""PreToolUse hook -- mechanically enforces CLAUDE.md Sec4/Sec20: no mutating tool call proceeds
without an UNAMBIGUOUS GO -- the operator's own literal GO/GO (Cyrillic), a genuine, correlated
GPT-PM VERDICT: APPROVE receipt (Sec20's own transport, Sec15's review.js), or (added 2026-09-17,
same day, on explicit operator GO -- "2 ГО") a GPT-PM-approved, in-progress Rosetta plan for this
session and repo (Sec19's `pm_rosetta_go`). All three sources are valid; what this hook exists to
reject is an INTERPRETED signal masquerading as any of them -- an ambiguous operator remark, a
paraphrased or uncorrelated GPT-PM reply, a stale/fail-open receipt, an unapproved or closed plan.

History, so the back-and-forth is on the record rather than lost: earlier the same day this hook
was first built to accept ONLY the operator's own GO, reading Sec20's repeal as the operator wanting
GPT-PM's authority removed entirely. That was wrong -- corrected by the operator directly: "я не это
просил, я просил чтобы вообще без ГО ничего не делалось, но ГО от GPT-PM валидное и должно
исполнятся" (the ask was that NOTHING proceeds without a valid GO; a genuine GO from GPT-PM IS one
of the valid kinds). Sec20 is reaffirmed in CLAUDE.md; this hook now checks both sources.

Trigger, both from auto-memory: `feedback-never-start-anything-without-go.md` (2026-09-01,
operator: "запомни сотый раз без ГО не начинать ничего" -- a session started work off "хочу срочно
вернуться к редизайну") and a live incident the same week where a session read "что дальше по
плану?" (a status question) as permission to continue implementing. Existing hooks only close a
narrow slice of this: `plan_approval_gate.py` covers just the ExitPlanMode/bypassPermissions
chokepoint, `gpt_review_gate.py` covers only `git commit`/`git push`. This hook covers the general
case -- every Edit/Write/NotebookEdit/MultiEdit (always a mutation, via `_rosetta_common
.classify_call`), and, for Bash/PowerShell, a separate BLOCKLIST (`bash_needs_go`, added
2026-09-17): operator instruction, verbatim, after live false-positive reports from concurrent
sessions ("надо разрешить все кроме делита или чегото подобного... рид онли ГО не требует
вообще"), was to flip Bash/PowerShell from "unrecognised = needs GO" to "recognised-dangerous =
needs GO", scoped down to just: delete-class commands (`rm`/`Remove-Item`/`del`/`git rm`),
command substitution, and write-redirection/heredoc (either can hide a delete or any other
mutation behind an unrecognised shape). An earlier draft of this list also kept in-place edit
(`sed -i`), package install, and permission changes (`chmod`/`icacls`) requiring GO -- the operator
explicitly rejected that too, same message: "это тоже разрешить - Правка на месте (sed -i),
установка пакетов ..., смена прав (chmod/icacls)". Ordinary git (`commit`, `push`, `merge`,
`rebase`, `reset`, `tag`, branch creation) and everything else no longer need a GO from this hook
-- confirmed safe because `dangerous_command_gate.py` already blocks the actually-destructive
variants of those same verbs (`reset --hard`, `clean -f`, `branch -D`, remote-ref-delete, broad
`checkout --`/`restore`) unconditionally, regardless of any GO, and `gpt_review_gate.py` still
separately gates `git commit`/`git push` on a review receipt. This is a DIFFERENT, narrower
classifier than `_rosetta_common.classify_shell` -- that one stays an allowlist, unchanged, because
Rosetta's own audit trail for other repos depends on its conservatism; this hook's job (does this
specific command need a GO) is not the same job as Rosetta's (record every mutation accurately), so
the two are deliberately allowed to diverge.

Operator-GO oracle -- informed by a GPT-PM technical-review round, 2026-09-17 (advisory only, not
an authorization; CLAUDE.md Sec16). `plan_approval_gate.py`'s own regex, bare `\bGO\b|\bGO\b`
(Cyrillic), is too loose to reuse machine-wide: it matches a MENTION of the rule -- "без ГО ничего
не начинай", "почему ты сделал это без GO?" -- as if it were an invocation of it. This hook instead
requires GO/ГО to be the FIRST token of the message (`GO`, `ГО`, `GO: AUTHORIZED`, `ГО продолжай`),
OR the LAST token of a short message not immediately preceded by a negation word (`без`/`не`) --
added after the first version blocked the operator's own genuine "Да все верно, делай именно так
ГО" (GO trailing a short confirming sentence, exactly how the operator actually writes it). GO/ГО
appearing only in the MIDDLE of a longer message, or inside a quote/explanation of the rule, still
does not qualify either way. See `message_opens_with_go`'s own docstring for the exact shapes.

Automated-status-text exclusion ("Tool loaded.") -- added 2026-09-25 (relayed report: a genuine
operator GO was immediately followed, in the transcript, by a harness-generated "Tool loaded."
status row -- same `type: "user"`/attachment shape as genuine text -- which then became the "most
recent genuine message" and silently invalidated the real GO). `_is_automated_status_text` excludes
this exact narrow pattern from every text-extraction path in `latest_genuine_operator_text` --
same fix shape as the pre-existing `HOOK_FEEDBACK_PREFIX` exclusion in `_operator_text`, a
different automated source. Deliberately exact/narrow, not a heuristic -- a genuine message that
merely starts with those words ("Tool loaded. Continue...") is not excluded.

Decision-log APPEND-ONLY carve-out -- added 2026-09-25 (operator report: this system's own
per-project convention of a continuously updated `core/DECISION_LOG.md`, reminded on nearly every
turn, was hitting this hook's "Edit/Write always needs GO" rule on every single entry). Narrowed via
`AskUserQuestion` to append-only, not a blanket file exemption: an Edit/Write/MultiEdit targeting a
`DECISION_LOG.md` file (any repo, matched by basename) is exempt ONLY when the new content has the
existing content as an exact prefix -- nothing on disk can be removed or altered, only grown. See
`_decision_log_append_exempt` and its docstring for the full mechanism; this is the first carve-out
in this hook that touches Edit/Write's own "always needs GO" rule at all -- every OTHER Edit/Write
target, and any non-append shape of edit to this same file, is completely unaffected.

Operator-GO oracle, AskUserQuestion extension -- added 2026-09-24 (operator report: a session
offered "ГО на исправление этих N находок" as a selectable AskUserQuestion option, the operator
picked it, and this hook denied anyway). An AskUserQuestion answer is logged as a `type: "user"`
row whose `message.content` is `tool_result`-only, which `_operator_text` correctly excludes for
every OTHER tool (a tool result is automation, not the operator) -- but the answer itself genuinely
is the operator's own choice, just delivered structurally. `_ask_user_question_go_answer` reads the
row's own `toolUseResult.answers` (question -> answer text) and applies the exact same
`message_opens_with_go` bar to each answer's OWN text -- not the question, not the whole
multi-answer blob. Same strictness, wider channel, not a looser one.

Operator-GO oracle, BRANCH GO 1/2 extension -- added 2026-09-24 (operator report: a session gave a
genuine `BRANCH GO 2: AUTHORIZED FOR manifest-proposal/g3` message satisfying CLAUDE.md Sec14's own
branch-creation double consent, the branch was created, but the follow-up Write into that branch/
worktree was still denied here -- Sec14's ritual phrase was not recognised as a GO by this hook's
OWN, separate, general mutation-GO check). `message_opens_with_go` now also accepts a message that
OPENS with Sec14's exact phrasing (`BRANCH GO 1: AUTHORIZED` / `BRANCH GO 2: AUTHORIZED FOR <name>`)
-- at least as deliberate and unambiguous a signal as a bare GO/ГО, since Sec14 itself already
refuses generic phrasing for these two approvals. Turn-scoped like any other operator-GO (not a
freshness window like the AskUserQuestion extension above -- not asked for here, and Sec14 approvals
are typically acted on immediately). Deliberately does not cross-check the named branch against the
call's actual target -- CLAUDE.md Sec4 already records that as an accepted gap ("§14 enforced by
prose only"); this only teaches recognition of the phrase.

Bash/PowerShell blocklist, inert `$(cat <<'DELIM' ... DELIM)` carve-out -- added 2026-09-25
(operator report: this system's OWN mandated multi-line commit-message convention,
`git commit -m "$(cat <<'EOF' ... EOF)"`, denied `command_substitution` on every such commit even
though `git commit` is already fully exempt from this hook -- the substitution check runs before
the per-segment git allowlist and never inspected what the `$(...)` actually contained). Provably
safe per Bash's own heredoc semantics: a QUOTED delimiter disables all expansion inside the body
regardless of content, and `cat` has no side effects -- so this exact shape is equivalent to a
static string literal. `_strip_inert_cat_heredoc_spans` runs first, on the RAW command, finding the
real terminator by actual heredoc rules (not reusing the Bash-quote state machine, which has no
heredoc concept and would desync on a stray apostrophe in the message body) -- only stripped when
`cat` is bare (no flags), the delimiter is quoted, and the closing `)` immediately follows the
terminator line. Applies to both Bash and PowerShell calls (a no-op for PowerShell, which has no
heredoc syntax).

Bash/PowerShell blocklist, PowerShell `$(...)` narrow carve-out -- added 2026-09-25 (relayed
report, verified against the real transcript rather than trusted on the relaying session's own
guess -- CLAUDE.md Sec23: the guess, a here-string/heredoc match, was wrong; the real, confirmed
cause was `command_substitution` on `$($sb.ToString())` inside a read-only window-title
diagnostic). Operator was told this was, by design, the SAME deliberate policy already applied to
Bash's own `$(...)` (2026-09-17) and CAN hide a real mutation in PowerShell too
(`$(Remove-Item ...)`) -- asked to weaken it anyway via `AskUserQuestion`, explicit choice over
"change nothing". `bash_needs_go(..., is_powershell=...)` now lifts `command_substitution` for a
PowerShell call ONLY when EVERY `$(...)` span in the command is the narrow whitelisted shape
(`_powershell_subexpr_all_safe`/`_is_safe_ps_subexpr`) -- a simple `$var(.Property)*` chain ending
in one of a small, explicit method/property whitelist (`ToString`, `Length`, etc.), zero arguments,
no nesting, no backtick anywhere in the command. Bash calls always pass `is_powershell=False` and
are completely unaffected -- verified live, the identical command text still denies when run as a
Bash tool call. See `_SAFE_PS_SUBEXPR_RE`'s own comment block for the full shape and the stated
residual risk (a lexical check, not a semantic one -- cannot know an override actually has no side
effects).

Operator-GO oracle, mid-turn QUEUED-message extension -- added 2026-09-25 (relayed report: a
session's own genuine "ГО", typed while Claude Code was still mid-turn, was denied anyway; the
session guessed "known class of problem with this hook" -- verified true, not accepted on say-so,
per CLAUDE.md Sec23). A mid-turn message is logged as `type: "attachment"` (never `"user"`), the
real text at `attachment.prompt[i].text` -- structurally invisible to a loop that only ever checked
`type == "user"` rows. `_mid_turn_attachment_text` now reads it, guarded to
`attachment.type == "queued_command"` and `attachment.origin.kind == "human"` so an automated/
system-queued attachment (if that shape exists) is not mistaken for the operator. Turn-scoped like
any other operator-GO (a later mid-turn or ordinary message, GO or not, still overwrites it per the
existing "single most recent genuine message" rule) -- no separate freshness window, unlike the
AskUserQuestion extension above.

GPT-PM-GO oracle -- mirrors `gpt_review_gate.py`'s own receipt store (`receipts.jsonl`, written by
`D:\Repo\pm-bridge\src\cli\review.js`) and repo-scoping, so the two gates never disagree about what
"a genuine APPROVE" means. A mutating call is also allowed when the most recent receipt for the
call's own git repository is fresh (Sec15's RECEIPT_MAX_AGE_HOURS window), `ok`, and its `verdict`
field matches `VERDICT:\s*APPROVE`. Same known imprecision Sec15 already documents for this receipt
store applies here too: a receipt proves SOME review ran recently for the REPO, not that it covered
this specific call -- not fixed by this hook, inherited as-is.

Rosetta-plan-GO oracle -- added same day, later, on explicit operator instruction ("2 ГО") after a
session following the Rosetta protocol (Plan -> `pm_rosetta_go` -> Act) still got denied here with
no way to satisfy this hook short of a redundant second GO on top of GPT-PM's already-approved
plan. `CLAUDE.md` Sec19 says Rosetta "records and denies nothing" today (R0) and defers real
enforcement to a future Gate B, specifically because the plan-approval's own identity binding
("which GPT-PM conversation actually approved this") "still degrades to a folder-name convention" --
a known, accepted weakness, not a new one introduced here. This hook now overrides that R0 default
for ITSELF only (Rosetta's own hooks -- `rosetta_audit.py`/`rosetta_due.py` -- are untouched and
still audit-only): a mutating call is also allowed when `_rosetta_common.active_plan` finds THIS
session holds a GPT-PM-approved, in-progress plan scoped to the call's repo -- the identical check
`rosetta_audit.py` already performs for its own recording, so the two can never disagree about what
counts as an approved plan. Covers every mutating call for as long as that plan stays
`in-progress`, exactly like Sec4's own "one GO covers the whole bounded gate" (a Rosetta plan IS a
gate); stops the moment the plan closes or is replaced by an unapproved edit.

Scope of authority -- turn-scoped for the operator-GO path, not gate-persistent, from the same
review round. A qualifying operator GO authorizes every mutating call in the current, uninterrupted
run of tool calls (this hook looks at the SINGLE most recent genuine operator message, so a
multi-tool-call agentic loop within one turn is checked against the same qualifying message
throughout -- no repeat prompt for the same bounded gate, matching Sec4's own "do not ask for a
second confirmation" rule). A NEW operator message -- even "ок", "продолжай", "fix that test" --
does not carry the old GO forward; if that new message does not itself qualify AND no fresh GPT-PM
APPROVE receipt covers the repo, mutation is blocked again until one or the other arrives. This is
deliberately what closes the "что дальше по плану?" incident: that message does not qualify, so it
does not authorize anything by itself, regardless of what any earlier message in the conversation
authorized.

Fail-open on infrastructure failure (unreadable transcript, malformed JSON, any internal
exception) -- consistent with every other hook in this set ("a safety hook that hangs a session on
every action is worse than no net", per plan_approval_gate.py and _rosetta_common.py). This is a
deliberate divergence from the GPT-PM round's "fail-closed when authority cannot be verified"
suggestion: making this hook the one fail-closed exception in the fleet would mean a bug in its OWN
transcript-reading logic bricks Edit/Write/Bash machine-wide with no visible cause -- a worse
failure than the one this hook exists to prevent, and inconsistent with how the operator has
already chosen to run every other hook here. Fail-open applies ONLY to genuine infra failure; a
transcript that reads fine but shows no qualifying GO is not a failure state -- it is an ordinary,
intended deny.

Kill switch: CLAUDE_GO_GATE=off (process-level, set before the session starts -- same convention as
every other hook in this directory).

2026-09-27: VERDICT_APPROVE_RE was silently dead code since its introduction. It required the
literal substring "VERDICT:" inside the receipt's `verdict` field, but review.js's own
parseVerdict() (pm-bridge/src/reviewVerdict.js) always writes just the bare severity token --
"APPROVE"/"MINOR"/"MAJOR"/"BLOCKER"/"UNRECOGNIZED"/null -- never a sentence containing the word
"VERDICT". Confirmed by enumerating every value ever written to
D:\tmp\claude_gpt_review_gate\receipts.jsonl on this machine: "VERDICT:" appears in zero of them.
So the entire Sec20 GPT-PM-APPROVE GO path in gpt_pm_approve_covers() below could never fire for a
real receipt, on any project, ever -- a session could get receipt_written:true, ok=true,
correlated=true, verdict='APPROVE' and still be denied "not a qualifying APPROVE". This surfaced
live when exactly that happened twice in a row in an autonomous PM-Bridge session with no operator
present to type a literal GO and a separately broken Rosetta plan-binding path, leaving Edit/Write
completely blocked with no working authorization route at all. Fixed to match the bare token the
field actually holds (`re.compile(r"^\s*APPROVE\s*$", re.IGNORECASE)`), verified against every
historical value in the real receipts file plus a live end-to-end run of the actual hook binary
(fresh synthetic APPROVE receipt for a throwaway git repo -> allow; same call with no matching
receipt, or a real git repo with zero receipts -> deny, unchanged). Full regression (73 unit + 15
e2e + 9 AskUserQuestion) green. See `feedback-go-gate-mechanical-enforcement` (auto memory) for
detail. This was purely a bug fix restoring an already-documented Sec20 path to working order --
it does not by itself touch the separate, much larger question (raised the same day) of whether
Edit/Write should require a GO at all outside a live operator chat request.

2026-09-30: new narrow Bash-only `$(...)` carve-out (`_bash_subexpr_all_safe`/
`_is_safe_bash_subexpr`/`_SAFE_BASH_SUBEXPR_RE`) for the exact shape
`$(git diff --cached --name-only | grep <short-flags> '<single-quoted-pattern>')`, after a live
report: a routine pre-commit prettier-lint one-liner (`npx prettier --check $(git diff --cached
--name-only | grep -E '\.(mjs|json|ya?ml)$')`) was denied under `command_substitution` even though
`git diff`/`grep` here are provably read-only (see the carve-out's own comment block, just above
`_SAFE_BASH_SUBEXPR_RE`, for the full safety argument and its explicit non-generalization). First
implementation was silently broken: it checked each `$(...)` span's content against
`substitution_view`, the SAME quote-blanked view `_quote_aware_views` builds to detect a real
redirect/substitution vs. one that is just prose inside a quoted string -- but that blanking also
deletes single-quoted content INSIDE a real `$(...)` span, so every span came back with its
required trailing `'...'` pattern already stripped and the safety regex could never match a real
command. Unit tests against hand-typed strings passed anyway, because they fed the regex directly
and never exercised `bash_needs_go`'s real code path -- caught only by an end-to-end run against the
real hook binary, per CLAUDE.md Sec5 ("syntax/build success alone is not proof"). Fixed by adding a
separate, quote-content-PRESERVING span extractor (`_extract_dollar_paren_spans_raw` +
`_find_balanced_paren_close`) that walks the RAW command with the same live/dead `$(` quoting rules
`_quote_aware_views` already uses (dead inside single quotes, live inside double quotes and at top
level), but keeps each span's own inner quoting -- including a literal `(` inside the grep pattern's
single quotes, which must NOT perturb paren-balance counting. `bash_needs_go` now calls
`_bash_subexpr_all_safe(command)` with the raw command, not `substitution_view`; the PowerShell
carve-out is untouched (still reads `substitution_view`, which was never wrong for that narrower,
no-embedded-quotes shape). Verified: the exact reported command now allows end to end through the
real hook binary; 9 adversarial unit cases (double-quoted pattern, trailing `;`, a second unsafe
span alongside a safe one, missing pattern, non---cached `git diff`, a LITERAL `$(...)`-looking
string inside the single-quoted pattern that must still count as safe since it is inert text, and a
backtick anywhere failing closed) all pass; full regression (15 e2e + 9 AskUserQuestion + 5 PM-mode)
green -- the 73-case unit suite (`test_go_gate3.py`) referenced by the 2026-09-27 entry above was
not found on disk for this run (scratchpad fixture cleanup between sessions) and could not be
re-run; this is stated rather than silently assumed passing. See
`feedback-go-gate-mechanical-enforcement` (auto memory) for the exact regex/scanner and adversarial
test list.

2026-10-02: a plain typed GO ("ГО"/"GO") was turn-scoped only -- `message_opens_with_go` was
checked against the SINGLE MOST RECENT genuine message, so a genuine explicit GO silently
evaporated the moment the operator sent ANY later message that was not itself a GO, even an
unrelated follow-up remark with no intent to rescind anything. Confirmed live (ERP_MVP1_WORKTREE):
operator sent "ГО", the session began work, then sent an impatient "и че ты встал?" with no GO in
it, and the very next mutating call denied with "the latest operator message ... is not a literal
GO" -- correct per the old rule, but not what the operator wants, and not what CLAUDE.md v2 Sec3
("do not ask twice for the same bounded work") actually promises. This is NOT the historical
"что дальше по плану?" failure mode (an ambiguous remark being misread AS a GO) -- the strict
`message_opens_with_go` matching is completely unchanged; only how long a genuinely-matched GO
stays valid changed. Fixed the same way the AskUserQuestion-GO channel was fixed on 2026-09-24:
added `fresh_plain_text_go`, a full-transcript scan (mirroring `fresh_ask_user_question_go`) that
finds the most recent qualifying plain-text/mid-turn-attachment/AskUserQuestion-answer GO and
accepts it if it is within `PLAIN_GO_MAX_AGE_HOURS` (6h, same value as the other three
freshness-windowed sources), independent of whatever the single most recent message says. Wired
into `main()` as a new check between the existing turn-scoped check and the GPT-PM-APPROVE path.
Verified end to end through the real hook binary: the exact incident shape (short GO, then an
unrelated non-GO follow-up, then an Edit) now allows; a stale (>6h) GO followed by a non-GO message
still denies; a transcript with no qualifying GO anywhere still denies; and critically, a long
pasted message (such as this very fix's own triggering report) that merely ENDS in "ГО" still does
NOT qualify -- `message_opens_with_go`'s own `_TRAILING_GO_MAX_LEN` cap is untouched by this fix,
so a large log that happens to end in the GO token is still correctly refused, exactly as it was
moments before this fix was written (the operator's own first attempt to authorize this fix hit
that exact refusal, live, and had to resend a short standalone "ГО"). 10-case core regression
(delete/redirect/substitution/edit/push/sed/chmod/pip) green. The REASON_TEMPLATE denial message
was extended with a `plain_go_reason` line and a short explanation of the length cap, so a future
denial under this exact shape is self-explanatory instead of looking like the hook ignoring a real
GO. See `feedback-go-gate-mechanical-enforcement` (auto memory) for full detail.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _agentio as agentio  # noqa: E402
from _report_common import _non_human_origin, _operator_text, _tail_rows, pm_mode_active  # noqa: E402
from _rosetta_common import (  # noqa: E402
    active_plan,
    classify_call,
    _git_subcommand,
    _SUBSTITUTION_RE,
)

GO_TOKEN_RE = re.compile(r"^(?:GO|ГО)$", re.IGNORECASE)
_PUNCT_STRIP = ".,!?:;\"'()«»…"
_NEGATION_WORDS = {"без", "не"}
_TRAILING_GO_MAX_LEN = 500  # a short confirmation, not a long pasted block ending to end in GO

# CLAUDE.md Sec14's own two-approval ritual phrases -- added 2026-09-24 (operator report: a genuine
# "BRANCH GO 2: AUTHORIZED FOR manifest-proposal/g3" message, which already satisfies Sec14's
# branch-creation double consent, did NOT satisfy this hook's own separate GO requirement for the
# follow-up Write into the now-created branch/worktree). These are, if anything, a MORE deliberate
# and unambiguous signal than a bare GO/ГО -- Sec14 itself already refuses generic phrasing for
# them ("Generic phrases such as GO, ГО... do NOT count as BRANCH GO 1") -- so recognising the exact
# ritual phrase here closes a gap rather than loosening anything. Matched only at the START of the
# message (mirrors the first-token GO rule, not the trailing one -- these phrases are long and a
# trailing-match rule would risk matching a MENTION of the ritual inside a longer pasted message).
# Deliberately does NOT cross-check the named branch against the call's actual target repo/branch --
# CLAUDE.md Sec4 already records that gap as accepted ("§14 enforced by prose only... no OTHER
# mechanical backstop"); this only teaches the hook to recognise the phrase, not to verify it.
_BRANCH_GO_RE = re.compile(
    r"^BRANCH\s+GO\s+1\s*:\s*AUTHORIZED\b|^BRANCH\s+GO\s+2\s*:\s*AUTHORIZED\s+FOR\s+\S+",
    re.IGNORECASE,
)

# Same receipt store and freshness window as gpt_review_gate.py (CLAUDE.md Sec15) -- deliberately
# not re-derived, so the two gates can never disagree about what "a genuine APPROVE" means.
RECEIPTS_FILE = Path(os.environ.get("CLAUDE_GPT_REVIEW_STATE") or agentio.state_dir("gpt_review_gate")) / "receipts.jsonl"
RECEIPT_MAX_AGE_HOURS = 6
# FIXED 2026-09-27 (confirmed live bug, not a relayed session's guess): this used to be
# re.compile(r"VERDICT:\s*APPROVE", re.IGNORECASE) -- written as if the receipt's `verdict` field
# held the raw "VERDICT: APPROVE" reply line. It never has. review.js's own parseVerdict()
# (pm-bridge/src/reviewVerdict.js) strips the line down to a bare severity token before it ever
# reaches the receipt -- verdict: parsedVerdict.severity is one of
# APPROVE/MINOR/MAJOR/BLOCKER/UNRECOGNIZED/null (review.js:601), never a sentence containing the
# word "VERDICT". Enumerating every value ever written to
# D:\tmp\claude_gpt_review_gate\receipts.jsonl on this machine confirms it: the substring
# "VERDICT:" appears in zero of them, ever. So VERDICT_APPROVE_RE.search(verdict) could never match
# a real receipt -- the entire Sec20 GPT-PM-APPROVE GO path in gpt_pm_approve_covers() below has
# been dead code since it was introduced, on every project, the whole time. Confirmed live: a
# session got receipt_written:true, ok=true, correlated=true, verdict='APPROVE' twice in a row and
# was still denied with "not a qualifying APPROVE" -- this is why. Fixed to match the bare token
# the field actually holds, not the sentence it never held.
VERDICT_APPROVE_RE = re.compile(r"^\s*APPROVE\s*$", re.IGNORECASE)

# An AskUserQuestion-GO's own freshness window -- operator instruction, 2026-09-24 ("я хочу чтобы
# хук срабатывал на любой ответ", narrowed via a clarifying AskUserQuestion to: "ГО из
# AskUserQuestion не аннулируется обычным сообщением после него"). Independent constant from
# RECEIPT_MAX_AGE_HOURS above (same value, different concept -- a transcript scan, not a receipt
# file) so the two can be tuned separately later without implying they must stay coupled.
ASK_USER_QUESTION_GO_MAX_AGE_HOURS = 6

# A plain typed GO's own freshness window -- operator incident, 2026-10-02 (ERP_MVP1_WORKTREE): the
# operator typed a genuine "ГО", the session began work, then the operator sent an impatient
# follow-up ("и че ты встал?") that was not itself a GO. Under the strict turn-scoped rule
# (`message_opens_with_go(last_text)` only ever looks at the SINGLE MOST RECENT genuine message),
# that follow-up silently invalidated the already-granted GO, so the very next mutating call denied
# with "GO source 1: the latest operator message ... is not a literal GO", even though nothing the
# operator said rescinded anything. This is NOT the "что дальше по плану?" failure mode this hook
# was built to catch (an AMBIGUOUS remark being misread AS a GO) -- `message_opens_with_go` still
# requires the exact same strict, deliberate token it always did. It is the same gap already fixed
# once for the AskUserQuestion channel (`fresh_ask_user_question_go` above, 2026-09-24): a genuinely
# explicit GO evaporating on the very next unrelated message is friction, not safety, and CLAUDE.md
# v2 Sec3 says so directly -- "A GO covers the named gate/scope; do not ask twice for the same
# bounded work." A plain typed "ГО"/"GO" is at least as deliberate as a selected AskUserQuestion
# answer, so it gets the identical treatment: a bounded freshness window instead of evaporating on
# the next message. Same value as the AskUserQuestion window, same accepted tradeoff already live
# for GPT-PM receipts and Rosetta plans -- a GO can now be reused for up to this long instead of
# needing to be retyped after every intervening remark, at the cost of the same kind of imprecise
# scoping those other three sources already carry (bounded by TIME, not by a parsed understanding
# of which exact gate the GO named).
PLAIN_GO_MAX_AGE_HOURS = 6
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# PowerShell-only narrow carve-out for `$(...)` -- operator instruction, 2026-09-25, after a
# VERIFIED false positive (not the relayed session's own guess, which was wrong: it blamed a
# here-string/heredoc match; the real, confirmed cause -- read directly from the transcript --
# was `command_substitution` firing on `$($sb.ToString())` inside a read-only window-title
# diagnostic). `$(...)` stays gated in BOTH shells by deliberate policy (CLAUDE.md §4,
# 2026-09-17): a PowerShell subexpression is a full expression evaluator that CAN hide a mutation
# exactly like Bash's own `$(...)` -- `$(Remove-Item ...)` is exactly as dangerous as
# `$(rm -rf ...)`. Recommended AGAINST adding an allowlist here for that reason; the operator
# asked for one anyway (`AskUserQuestion`, explicit choice over "change nothing"), so this is
# deliberately as narrow as the request itself ("ToString()/property-getter... without complex
# scoped method calls with side effects") -- every one of these is required, not just typical:
#   - the ENTIRE `$(...)` content matches `_SAFE_PS_SUBEXPR_RE` -- a simple `$var`, zero or more
#     bare `.Property` hops (letters/digits/underscore only -- no parens, no args, no nested `$(`,
#     no `;`/`|`/backtick anywhere), ending in exactly one WHITELISTED zero-argument member;
#   - the final member is in `_SAFE_PS_SUBEXPR_METHODS` (called with literal empty `()`) or
#     `_SAFE_PS_SUBEXPR_PROPERTIES` (no parens at all);
#   - EVERY `$(...)` span in the command must independently qualify -- one unsafe span anywhere
#     still requires GO for the whole command;
#   - a backtick anywhere, or an unbalanced `$(`, makes the whole command unparseable by this
#     carve-out's own scanner and it fails closed (still requires GO) -- see
#     `_extract_dollar_paren_spans`.
# Residual, accepted risk, stated plainly rather than implied: this is a lexical shape check, not a
# semantic one -- it cannot know that a type's own `.ToString()`/`.Length`/etc. override is
# actually side-effect-free (a malicious or merely unusual type could make any of them mutate).
# Scoped to the specific low-risk pattern the operator asked for and nothing broader; PowerShell's
# `$(...)` remains gated for everything else, and Bash's `$(...)` is completely untouched by this.
_SAFE_PS_SUBEXPR_METHODS = {"ToString", "ToUpper", "ToLower", "Trim", "GetType", "GetHashCode"}
_SAFE_PS_SUBEXPR_PROPERTIES = {"Length", "Count"}
_SAFE_PS_SUBEXPR_RE = re.compile(
    r"^\$[A-Za-z_][A-Za-z0-9_]*"            # a simple variable, e.g. $sb
    r"(?:\.[A-Za-z_][A-Za-z0-9_]*)*"         # zero or more simple .Property hops (no calls, no args)
    r"\.([A-Za-z_][A-Za-z0-9_]*)(\(\))?$"    # the final member -- checked against the whitelists below
)


def _is_safe_ps_subexpr(inner: str) -> bool:
    """True only for the narrow, explicitly whitelisted shape described above."""
    inner = inner.strip()
    match = _SAFE_PS_SUBEXPR_RE.match(inner)
    if not match:
        return False
    member, call = match.group(1), match.group(2)
    if call:
        return member in _SAFE_PS_SUBEXPR_METHODS
    return member in _SAFE_PS_SUBEXPR_PROPERTIES


def _extract_dollar_paren_spans(text: str) -> list[str] | None:
    """Every top-level `$(...)` span's CONTENT in `text`, or None if unparseable by this scanner
    (a backtick anywhere, or an unbalanced `$(` with no matching `)`) -- callers must treat None as
    "cannot verify safety", i.e. fail closed, never as "no spans found"."""
    if "`" in text:
        return None
    spans: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "$" and i + 1 < n and text[i + 1] == "(":
            depth = 1
            j = i + 2
            while j < n and depth > 0:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                j += 1
            if depth != 0:
                return None
            spans.append(text[i + 2 : j - 1])
            i = j
            continue
        i += 1
    return spans


def _powershell_subexpr_all_safe(substitution_view: str) -> bool:
    """True only when EVERY `$(...)` span in `substitution_view` independently qualifies as the
    narrow safe shape -- used to lift the `command_substitution` requirement for PowerShell calls
    only. Bash calls never reach this function."""
    spans = _extract_dollar_paren_spans(substitution_view)
    if spans is None:
        return False
    if not spans:
        return False  # _SUBSTITUTION_RE matched something -- a bare backtick with no `$(` at all
    return all(_is_safe_ps_subexpr(span) for span in spans)


# Bash-only narrow carve-out for `$(git diff --cached --name-only | grep <flags> '<pattern>')` --
# operator report, 2026-09-30: a routine pre-commit lint step --
#   git add <files> && git diff --cached --stat | tail -3 \
#     && npx prettier --check $(git diff --cached --name-only | grep -E '\.(mjs|json|ya?ml)$')
# -- was denied under `command_substitution` for building the prettier file list this way. The
# session's own workaround (list the files by hand instead of `$(...)`) is a real option every
# time this fires, but the substitution itself is provably read-only, so a narrow carve-out closes
# it the same way the inert-heredoc one did for the git-commit-message convention.
#
# Provably safe, not just "looks safe": `git diff` has no destructive form under any flag
# combination -- it only ever reads and prints a diff, never writes to the repo or the working
# tree. `grep` is a pure text filter with no flag that executes a command or writes a file (no
# `--exec`, no in-place edit -- that is `sed -i`, a completely different program already gated by
# CLAUDE.md's own git-scope decisions elsewhere, not this one). So a `$(...)` whose ENTIRE content
# is `git diff --cached --name-only`, one pipe, then `grep` with only short `-X` flag groups and a
# SINGLE single-quoted pattern -- nothing else, matched end-to-end by `_SAFE_BASH_SUBEXPR_RE` -- can
# never hide a mutation, a nested substitution, or a redirect: any `;`, `&&`, `||`, backtick,
# nested `$(`, or `>`/`<` breaks the full match and the span is treated as unsafe (fails closed).
# Single-quoted only, deliberately -- a double-quoted grep pattern still allows `$(...)`/`$var`
# expansion inside it in real Bash, which would reopen exactly the hole this carve-out exists to
# avoid; an unquoted or double-quoted pattern argument does not match and still requires GO.
#
# Scoped to exactly this one pipeline shape, not a general "read-only command allowlist inside
# $(...)" -- that broader idea was deliberately not built; extend `_SAFE_BASH_SUBEXPR_RE` narrowly
# again if a different provably-inert shape shows up, rather than widening this into a generic
# safe-subcommand grammar.
_SAFE_BASH_SUBEXPR_RE = re.compile(
    r"^git\s+diff\s+--cached\s+--name-only"   # exactly this read-only git invocation
    r"\s*\|\s*grep"                            # piped into grep, nothing else in between
    r"(?:\s+-[A-Za-z]+)*"                      # zero or more short flag groups (-E, -i, -vE, ...)
    r"\s+'[^']*'\s*$"                          # exactly one single-quoted pattern, then end of span
)


def _is_safe_bash_subexpr(inner: str) -> bool:
    """True only for the narrow, explicitly whitelisted `git diff --cached --name-only | grep ...`
    shape described above."""
    return bool(_SAFE_BASH_SUBEXPR_RE.match(inner.strip()))


def _find_balanced_paren_close(text: str, j: int) -> int | None:
    """From `j` (just past an opening `$(`'s `(`), the index just past the matching `)`, tracking
    depth AND quote state so a `)` (or `(`) inside a quoted argument INSIDE the span -- e.g. the
    literal `(` in a grep pattern like `'\\.(mjs|json)$'` -- does not perturb the balance. Returns
    None (unparseable, fail closed) on an unterminated quote or unbalanced parens."""
    depth = 1
    quote: str | None = None
    escaped = False
    n = len(text)
    while j < n and depth > 0:
        ch = text[j]
        if escaped:
            escaped = False
            j += 1
            continue
        if quote:
            if ch == "\\" and quote == '"':
                escaped = True
            elif ch == quote:
                quote = None
            j += 1
            continue
        if ch == "\\":
            escaped = True
            j += 1
            continue
        if ch in "\"'":
            quote = ch
            j += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        j += 1
    if depth != 0 or quote is not None:
        return None
    return j


def _extract_dollar_paren_spans_raw(command: str) -> list[str] | None:
    """Like `_extract_dollar_paren_spans`, but walks the RAW `command` instead of a quote-blanked
    view, so each returned span's content keeps its own inner quoting intact (needed to verify a
    literal single-quoted grep pattern inside `$(...)` -- `_quote_aware_views`'s `substitution_view`
    strips single-quoted content everywhere, including INSIDE a `$(...)` span, which silently broke
    `_bash_subexpr_all_safe` the first time it was built: every span came back with its trailing
    `'...'` pattern already deleted, so the safety regex could never match a real command -- caught
    by an end-to-end test against the real hook binary after unit tests against hand-typed strings
    passed, per CLAUDE.md §5 ("syntax/build success alone is not proof").

    `$(` liveness follows real Bash quoting exactly like `_quote_aware_views` does: dead (not a
    substitution start) inside single quotes, LIVE inside double quotes and at top level. A
    backtick anywhere in the raw command fails closed (None), matching
    `_extract_dollar_paren_spans`'s contract."""
    if "`" in command:
        return None
    spans: list[str] = []
    quote: str | None = None
    escaped = False
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if quote == "'":
            if ch == "'":
                quote = None
            i += 1
            continue
        if quote == '"':
            if ch == "\\":
                escaped = True
                i += 1
                continue
            if ch == '"':
                quote = None
                i += 1
                continue
            if ch == "$" and i + 1 < n and command[i + 1] == "(":
                end = _find_balanced_paren_close(command, i + 2)
                if end is None:
                    return None
                spans.append(command[i + 2 : end - 1])
                i = end
                continue
            i += 1
            continue
        # quote is None -- top-level, unquoted context
        if ch == "\\":
            escaped = True
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            i += 1
            continue
        if ch == "$" and i + 1 < n and command[i + 1] == "(":
            end = _find_balanced_paren_close(command, i + 2)
            if end is None:
                return None
            spans.append(command[i + 2 : end - 1])
            i = end
            continue
        i += 1
    return spans


def _bash_subexpr_all_safe(command: str) -> bool:
    """True only when EVERY `$(...)` span in the RAW `command` independently qualifies as the
    narrow safe shape -- used to lift the `command_substitution` requirement for a plain Bash call
    (PowerShell calls use `_powershell_subexpr_all_safe` instead, never this function). Takes the
    raw command, not `substitution_view` -- see `_extract_dollar_paren_spans_raw` for why."""
    spans = _extract_dollar_paren_spans_raw(command)
    if spans is None:
        return False
    if not spans:
        return False  # _SUBSTITUTION_RE matched something -- a bare backtick with no `$(` at all
    return all(_is_safe_bash_subexpr(span) for span in spans)


# Bash-only carve-out for `$(cat <<'DELIM' ... DELIM)` -- operator report, 2026-09-25: a properly
# formatted, multi-line `git commit` (this system's OWN mandated convention -- see the top-level
# "Committing changes with git" instructions: `git commit -m "$(cat <<'EOF' ... EOF)"`) was denied
# under `command_substitution` on EVERY such commit, even though `git commit` itself is already
# fully exempt from this hook (CLAUDE.md §4, 2026-09-17) -- the substitution check runs BEFORE
# the per-segment git-specific allowlist and does not know or care what the `$(...)` is used for.
# Not a one-off: this fires on every multi-line commit message written the recommended way, which
# is most of them.
#
# Provably safe, not just "looks safe": per Bash's own documented heredoc semantics, quoting the
# delimiter (`<<'EOF'`/`<<"EOF"`, unlike a bare `<<EOF`) disables ALL expansion inside the body --
# no `$(...)`, backtick, or `$var` substitution happens there, regardless of what characters the
# body contains. `cat` reading that body has no side effects of its own. So
# `$(cat <<'DELIM' ... DELIM)` is provably equivalent to a static string literal, independent of
# context -- not tied to `git commit` specifically, and not weakened by anything the body contains
# (an apostrophe, a `$(`, an unbalanced paren -- e.g. a commit message that cites "(§15)").
#
# Deliberately heredoc-aware, NOT reusing `_quote_aware_views`'s Bash-quote state machine: that
# machine toggles quote state on any bare `'`/`"` character with no concept of heredocs, so a
# heredoc body containing a stray apostrophe (extremely common in natural-language commit
# messages) would desynchronize its quote tracking for everything AFTER it. `_strip_inert_
# cat_heredoc_spans` runs FIRST, directly on the raw command, finding the terminator by REAL
# Bash heredoc rules (a `DELIM`-only line, column 0 unless `<<-`, tab-only indent when it is) --
# the body's own characters, however messy, never reach the quote-aware logic at all once
# stripped. Only recognized as inert when the closing `)` immediately follows the terminator line
# with nothing else inside the substitution (no extra command, no pipe) -- anything else about the
# span is left untouched, so it still requires GO. `cat` must appear bare (no flags/args before
# `<<`) -- `cat -n <<'EOF'` is intentionally rejected and still needs GO, narrower than necessary
# rather than broader.
_CAT_HEREDOC_OPEN_RE = re.compile(
    r"\$\(\s*cat[ \t]+<<(-?)[ \t]*(['\"])([A-Za-z_][A-Za-z0-9_]*)\2[ \t]*\r?\n"
)


def _strip_inert_cat_heredoc_spans(command: str) -> str:
    """`command` with every provably-inert `$(cat <<'DELIM' ... DELIM)` span replaced by a single
    space, everything else preserved verbatim. See the block comment above for why this must run
    on the RAW command, before any quote-aware view is built."""
    out = []
    i = 0
    n = len(command)
    while i < n:
        m = _CAT_HEREDOC_OPEN_RE.match(command, i)
        if not m:
            out.append(command[i])
            i += 1
            continue
        dash, delim = m.group(1), m.group(3)
        indent = r"[\t]*" if dash else ""
        body_start = m.end()
        term_re = re.compile(r"\n" + indent + re.escape(delim) + r"[ \t]*\r?\n")
        term_match = term_re.search(command, body_start)
        if term_match is None:
            out.append(command[i])
            i += 1
            continue
        after_term = term_match.end()
        close_match = re.match(r"[ \t]*\)", command[after_term:])
        if close_match is None:
            out.append(command[i])
            i += 1
            continue
        span_end = after_term + close_match.end()
        out.append(" ")
        i = span_end
    return "".join(out)


# --- Bash/PowerShell: blocklist, not the shared allowlist -- operator instruction, 2026-09-17 ---
#
# "надо разрешить все кроме делита или чегото подобного... рид онли ГО не требует вообще", then,
# once asked to confirm the exact boundary: "это не трогать и разрешить - commit, push, merge,
# rebase, reset, tag, создание веток" / "это тоже разрешить - Правка на месте (sed -i), установка
# пакетов (pip/npm/go install и т.п.), смена прав (chmod/icacls)".
#
# `_rosetta_common.classify_shell` stays untouched -- it is shared with `rosetta_audit.py`, whose
# accuracy for OTHER repos' audit trail depends on staying a conservative allowlist ("unrecognised
# = mutation"). This hook's own job is different: not "record every mutation accurately" but "does
# THIS command need a GO" -- so for Bash/PowerShell specifically it uses a narrower, separate
# blocklist instead of reusing that classifier's read-only determination. Edit/Write/NotebookEdit/
# MultiEdit are untouched -- still always require GO via `classify_call`.
#
# Verified before this was written, not assumed: `dangerous_command_gate.py` (via
# `shell_policy_gate.py`) already blocks, unconditionally, regardless of any GO -- `git reset
# --hard`, `git clean -f`, `git branch -D`, `git push --delete`/remote-ref-delete, a path-less or
# root/home/wildcard `git checkout --`/`git restore`, a broad/forced `rm`, `chmod -R 777`/`icacls
# ... everyone`, `npm publish`/`pip uninstall -y`, disk/host-teardown commands. That is why
# commit/push/merge/rebase/reset/tag/branch-creation can drop out of THIS gate's GO requirement
# without losing the actually-dangerous variants of those same verbs -- a different, independent
# hook still catches those regardless of what this one decides. `git commit`/`git push` also keep
# their own dedicated review gate (`gpt_review_gate.py`, CLAUDE.md §15) untouched by this change.
#
# Known, accepted gap: CLAUDE.md §14 (branch-creation double consent, BRANCH GO 1/2) had no other
# mechanical backstop besides this hook's own (weaker, generic-GO) check -- removing branch
# creation from this blocklist leaves §14 with no mechanical enforcement at all, pure prose only.
# Confirmed with the operator before shipping this change, not assumed.
#
# In-place edit (`sed -i`/`awk -i`), package install/uninstall, and permission changes
# (`chmod`/`chown`/`icacls`/`takeown`) were in an earlier draft of this blocklist; the operator
# explicitly rejected keeping them, same message: "это тоже разрешить - Правка на месте (sed -i),
# установка пакетов (pip/npm/go install и т.п.), смена прав (chmod/icacls)". Only deletion-class
# commands (plus substitution/redirection, which can hide one) still need a GO from this hook.
_DELETE_HEADS = {"rm", "rmdir", "del", "erase", "remove-item", "ri"}

# `N>&M` (e.g. `2>&1`) duplicates one already-open file descriptor onto another -- it never
# targets a real file and is one of the single most common shell idioms there is (merging stderr
# into stdout before a pipe, `2>&1 | tail`). The shared `_rosetta_common._REDIRECT_RE` flags it
# anyway (harmless there -- an extra allowlist "mutation" record just costs an audit line), but
# under THIS hook's blocklist model it is a genuine false positive -- confirmed live, a concurrent
# session's routine `pytest ... 2>&1 | tail -20` was denied by it. `>&file`/`&>file` (no digit
# immediately before `>&`, and the target is not just another fd number) DOES write a real file
# and must still require GO -- this regex keeps that case, it only carves out fd-to-fd
# duplication specifically.
_GO_REDIRECT_RE = re.compile(
    r"(?<![0-9<>])>{1,2}(?![>&])"  # `>` / `>>` targeting a file (not a `>&...` fd-dup form)
    r"|(?<!\d)>&(?!\s*[-\d])"       # `>&TARGET`, only when TARGET isn't just an fd number/close
    r"|(?<!\d)&>(?!\s*[-\d])"       # `&>TARGET`, same bash shorthand, same exception
    r"|<<"                          # heredoc
)

_SEPARATOR_CHARS = ";|&\n"


def _quote_aware_views(command: str) -> tuple[str, str]:
    """Two filtered views of `command`, used to test for a REAL redirect/substitution rather than
    one that merely appears as prose text inside a quoted argument (a commit message, for
    instance) -- confirmed live: a commit message containing the plain-English arrow "->" was
    denied as `redirection_or_heredoc` because the check ran on the whole raw command string with
    no quote awareness at all.

    Shell quoting is not uniform, so one stripped view is not enough:
      - single quotes suppress EVERYTHING inside them -- no redirect, no substitution, no
        expansion of any kind;
      - double quotes suppress redirect/pipe/separator metacharacters, but NOT `$(...)`/backtick
        command substitution, which stays LIVE inside double quotes and really does execute.

    `for_redirect` drops content inside EITHER quote type (redirect chars are inert in both).
    `for_substitution` drops content inside SINGLE quotes only (a double-quoted `$(...)` must
    still be caught)."""
    redirect_buf: list[str] = []
    sub_buf: list[str] = []
    quote: str | None = None
    escaped = False
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if quote:
            if ch == "\\" and quote == '"':
                escaped = True
            elif ch == quote:
                quote = None
            elif quote == '"':
                sub_buf.append(ch)
            i += 1
            continue
        if ch == "\\":
            escaped = True
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            i += 1
            continue
        redirect_buf.append(ch)
        sub_buf.append(ch)
        i += 1
    return "".join(redirect_buf), "".join(sub_buf)


def _split_segments_quote_aware(command: str) -> list[str]:
    """Split on top-level `;`, `|`, `||`, `&&`, `&`, newline -- same separator set as
    `_rosetta_common._SEGMENT_SPLIT_RE` -- but only OUTSIDE single/double quotes.

    `_SEGMENT_SPLIT_RE` is a plain regex split with no quote awareness, which was harmless for
    the allowlist model this module shares with `_rosetta_common.classify_shell` (python/node
    were never on that allowlist anyway, so a broken split of `python -c "import uuid; ..."`
    still correctly landed on "mutation", just for the wrong-ish reason). This hook's blocklist
    model treats an unparsable fragment as "needs GO", so the same broken split turns a
    perfectly ordinary one-liner into a false positive -- confirmed live: `python -c "import
    uuid; print(uuid.uuid4())"` fractured on the semicolon inside the quotes, leaving
    `'import uuid` unterminated and unparsable by `shlex`. Walking the string once and tracking
    quote state avoids that without touching the shared classifier or its own callers."""
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    escaped = False
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if escaped:
            buf.append(ch)
            escaped = False
            i += 1
            continue
        if quote:
            buf.append(ch)
            if ch == "\\" and quote == '"':
                escaped = True
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch == "\\":
            buf.append(ch)
            escaped = True
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if command.startswith("&&", i) or command.startswith("||", i):
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch in _SEPARATOR_CHARS:
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return segments


def _segment_needs_go(segment: str) -> tuple[bool, str]:
    """(needs_go, reason) for one `;`/`&&`/`|`-split segment, blocklist model (see above)."""
    segment = segment.strip()
    if not segment:
        return False, "empty"
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        # Quoting this hook cannot parse could hide anything -- conservative.
        return True, "unparsable_segment"
    while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return False, "empty"
    head = Path(tokens[0]).name.lower()
    if head.endswith(".exe"):
        head = head[:-4]
    args = tokens[1:]

    if head == "git":
        sub, _sub_args = _git_subcommand(args)
        sub = (sub or "").lower()
        if sub == "rm":
            return True, "git_rm"
        return False, f"git_{sub or 'bare'}"

    if head in _DELETE_HEADS:
        return True, f"delete_command:{head}"

    return False, f"not_on_blocklist:{head}"


# Captures the TARGET following a real write-redirect operator (same three alternatives as
# `_GO_REDIRECT_RE`, minus heredoc, which has no single "target path" the way these do). Always
# run against `_mask_quotes(command)`, never the raw string -- see that function's docstring for
# why a position-preserving mask, not `_quote_aware_views`' shorter stripped view, is needed here.
_REDIRECT_TARGET_RE = re.compile(
    r"(?:(?<![0-9<>])>{1,2}(?![>&])"   # `>` / `>>`
    r"|(?<!\d)>&(?!\s*[-\d])"           # `>&TARGET`
    r"|(?<!\d)&>(?!\s*[-\d])"           # `&>TARGET`
    r")\s*(\S+)"
)
_SCRATCHPAD_SEGMENT_RE = re.compile(r"[\\/]scratchpad[\\/]", re.IGNORECASE)


def _mask_quotes(command: str) -> str:
    """Same LENGTH as `command`, every quoted character (including the quote delimiters
    themselves) replaced with `x` -- so `_REDIRECT_TARGET_RE`, run against this instead of the
    raw string, can never match a redirect operator that only appears as prose text inside a
    quoted argument (confirmed live: a commit message reading `"a -> b"` contains a literal `>`,
    which a non-quote-aware target extractor picked up as a spurious extra "target", causing the
    real, separate, actually-scratchpad-bound redirect elsewhere in the SAME command to be wrongly
    rejected).

    Length-preserving is the point, unlike `_quote_aware_views` (which drops quoted spans
    entirely, fine for a yes/no redirect check but not for this function's caller, which needs to
    slice the ORIGINAL command at a match's exact position to recover the real, possibly-quoted
    target text)."""
    out = list(command)
    quote: str | None = None
    escaped = False
    for i, ch in enumerate(command):
        if escaped:
            out[i] = "x"
            escaped = False
            continue
        if quote:
            out[i] = "x"
            if ch == "\\" and quote == '"':
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch in "\"'":
            quote = ch
            out[i] = "x"
            continue
    return "".join(out)


def _nearest_existing_dir(path: Path) -> Path:
    """Walk up from `path` to the nearest ancestor that actually exists on disk. `git -C <dir>`
    errors on a directory that does not exist yet -- the overwhelmingly common case for a
    write-redirect target, since the whole point is usually to CREATE a new file (or its parent
    directory). Once created, a path cannot cross a repository boundary its nearest existing
    ancestor is not already on the correct side of, so checking that ancestor is equivalent."""
    p = path
    while not p.exists():
        parent = p.parent
        if parent == p:
            return p
        p = parent
    return p


def _redirect_targets_all_in_scratchpad(command: str, redirect_view: str, cwd) -> bool:
    """True only when EVERY write-redirect target in `command` resolves into a session's own
    scratchpad directory AND outside every git repository. Operator instruction, 2026-09-18: a
    live Fitness_App session was denied piping diagnostic output into its own throwaway session
    scratchpad -- explicitly documented, machine-wide, as free-to-use, isolated-from-the-project
    space (every session's own environment block: "session-specific, isolated from the project,
    and can generally be used without permission prompts"). Gating a write there exactly like a
    write to real project files never matched that documented exemption.

    Heredoc (`<<`) is deliberately NOT covered -- it redirects into a command's STDIN, not to a
    named file, so there is no simple target path to check; any command containing `<<` anywhere
    still always needs GO.

    Each target is resolved (`Path.resolve()`, following `..`) before the scratchpad-segment
    check, specifically to close a path-traversal hole a purely lexical check would leave open --
    `scratchpad/../../../real_project_file.py` contains the literal substring `scratchpad` but
    must NOT qualify, since it resolves somewhere else entirely. The resolved path's nearest
    EXISTING ancestor (`_nearest_existing_dir`) must also not be inside any git repository, so a
    project that happens to keep its own committed `scratchpad/` directory is never accidentally
    exempted -- found necessary live: `git -C <not-yet-created-dir>` simply errors, which would
    otherwise silently read as "not in a repo" for the single most common case (a brand new
    target path that does not exist until this very write creates it)."""
    if "<<" in redirect_view:
        return False
    masked = _mask_quotes(command)
    targets: list[str] = []
    for m in _REDIRECT_TARGET_RE.finditer(masked):
        start, end = m.span(1)
        raw_target = command[start:end]
        if len(raw_target) >= 2 and raw_target[0] == raw_target[-1] and raw_target[0] in "\"'":
            raw_target = raw_target[1:-1]
        if not raw_target:
            return False
        targets.append(raw_target)
    if not targets:
        return False
    for target in targets:
        raw_path = Path(target)
        if not raw_path.is_absolute():
            raw_path = Path(cwd or os.getcwd()) / raw_path
        try:
            resolved = raw_path.resolve()
        except OSError:
            return False
        if not _SCRATCHPAD_SEGMENT_RE.search(str(resolved)):
            return False
        if _git_toplevel(str(_nearest_existing_dir(resolved.parent))):
            return False
    return True


def bash_needs_go(command: str, cwd=None, *, is_powershell: bool = False) -> tuple[bool, str]:
    """(needs_go, reason) for a whole Bash/PowerShell command -- blocklist, not allowlist.

    Command substitution and write-redirection always need GO regardless of head: both can hide
    an arbitrary mutation, including a delete, behind a shape this function never inspects. Both
    checks run against quote-aware views (`_quote_aware_views`), not the raw string -- a `>` or
    `$(` typed as ordinary prose inside a quoted argument must not trigger either one. A
    write-redirect whose target(s) are entirely inside the session's own scratchpad, and outside
    every git repository, is exempt (`_redirect_targets_all_in_scratchpad`) -- everything else
    about the redirect check is unchanged.

    `is_powershell` (2026-09-25) narrows the `command_substitution` check specifically for
    PowerShell: when every `$(...)` span in the command is the narrow whitelisted shape
    (`_powershell_subexpr_all_safe`), this reason does not fire. A Bash call runs the equivalent,
    separate Bash-only check instead (`_bash_subexpr_all_safe`, 2026-09-30): every `$(...)` span
    must be exactly `git diff --cached --name-only | grep <short-flags> '<pattern>'` -- see that
    function's own docstring, and `_SAFE_PS_SUBEXPR_RE`/`_SAFE_BASH_SUBEXPR_RE` for the exact shape
    and accepted residual risk of each carve-out.

    Before any of that, `_strip_inert_cat_heredoc_spans` (2026-09-25) removes every provably-inert
    `$(cat <<'DELIM' ... DELIM)` span from `command` first -- this system's own mandated
    multi-line-commit-message shape (see that function's own comment block for why it is safe and
    why it must run before quote-aware parsing, not after). Everything below this point operates
    on that stripped copy, never the original `command`, so redirect-target extraction and
    segment splitting see consistent positions throughout."""
    if not command or not command.strip():
        return False, "empty"
    command = _strip_inert_cat_heredoc_spans(command)
    redirect_view, substitution_view = _quote_aware_views(command)
    if _SUBSTITUTION_RE.search(substitution_view):
        if is_powershell:
            safe = _powershell_subexpr_all_safe(substitution_view)
        else:
            safe = _bash_subexpr_all_safe(command)
        if not safe:
            return True, "command_substitution"
    if _GO_REDIRECT_RE.search(redirect_view):
        if not _redirect_targets_all_in_scratchpad(command, redirect_view, cwd):
            return True, "redirection_or_heredoc"
    for segment in _split_segments_quote_aware(command):
        needs, reason = _segment_needs_go(segment)
        if needs:
            return True, reason
    return False, "not_dangerous"


def allow():
    sys.exit(0)


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }, ensure_ascii=False))
    sys.exit(0)


def context_unavailable(data: dict, reason: str) -> None:
    """Codex mutations cannot proceed when the operator authorization context is unreadable."""
    try:
        is_codex = agentio.client(data if isinstance(data, dict) else {}) == "codex"
    except Exception:
        is_codex = getattr(agentio, "CLIENT", "") == "codex"
    if is_codex:
        deny("GO gate: cannot verify Codex authorization context (" + reason + "); failing closed.")
    allow()


def _clean_token(token: str) -> str:
    return token.strip(_PUNCT_STRIP)


def message_opens_with_go(text: str | None) -> bool:
    """True if `text` is a genuine GO/ГО invocation, not a mention of the rule.

    Three shapes qualify:
      - GO/ГО as the FIRST token ("GO", "ГО", "GO: AUTHORIZED", "ГО продолжай") -- any length.
      - GO/ГО as the LAST token of a SHORT message ("делай именно так ГО", "Да все верно, ГО") --
        capped at _TRAILING_GO_MAX_LEN so a long pasted block that happens to end in "GO" does not
        qualify, and refused when the token immediately before it is a negation ("без", "не"), so
        "без ГО ничего не начинай" / "почему ты сделал это без GO?" still do not qualify.
      - The message OPENS with CLAUDE.md Sec14's own exact ritual phrase, `BRANCH GO 1: AUTHORIZED`
        or `BRANCH GO 2: AUTHORIZED FOR <name>` (added 2026-09-24) -- a genuine Sec14 approval is at
        least as deliberate as a bare GO, and previously satisfied Sec14's own branch-creation
        consent while leaving this hook's separate, general mutation-GO requirement unmet.
    GO/ГО appearing only in the MIDDLE of a message never qualifies either way.
    """
    if not text:
        return False
    text = text.strip()
    if not text:
        return False

    if _BRANCH_GO_RE.match(text):
        return True

    tokens = text.split()
    if not tokens:
        return False

    first = _clean_token(tokens[0])
    if GO_TOKEN_RE.match(first):
        return True

    if len(text) <= _TRAILING_GO_MAX_LEN:
        last = _clean_token(tokens[-1])
        if GO_TOKEN_RE.match(last):
            prev = _clean_token(tokens[-2]).lower() if len(tokens) >= 2 else ""
            if prev not in _NEGATION_WORDS:
                return True

    return False


def _ask_user_question_go_answer(row: dict) -> str | None:
    """If `row` is the tool_result row for an `AskUserQuestion` call, and at least one of the
    operator's own answers independently qualifies as a genuine GO/ГО invocation under
    `message_opens_with_go`'s own strict rules, return that answer's raw text -- otherwise None.

    An AskUserQuestion answer is logged as a `type: "user"` row whose `message.content` is a
    `tool_result`-only list -- correctly excluded by `_operator_text` (most tool results are
    automation, not the operator). But the answer genuinely IS the operator's own typed/selected
    choice, just delivered structurally instead of as free chat text. The harness records it,
    untouched, in the row's own top-level `toolUseResult.answers` (question -> answer text) --
    read that directly rather than the pre-formatted "The user answered: ..." prose block, which
    is fragile to parse and not needed here.

    Confirmed live, 2026-09-24 (operator report): a session offered "ГО на исправление этих N
    находок" as a selectable AskUserQuestion option; the operator picked it, and this hook denied
    the next mutation anyway, because that answer never reached `_operator_text` at all -- this
    function closes exactly that gap.

    Deliberately as strict as the plain-text path, not looser: an answer qualifies only when the
    answer's OWN text -- not the question, not another answer in the same call, not the whole
    multi-answer blob -- independently opens or ends with a bare GO/ГО token, the exact same rule
    `message_opens_with_go` already applies to a typed chat message. An answer that merely
    discusses or mentions GO in passing still does not qualify, same as it never did for a typed
    message."""
    if row.get("type") != "user":
        return None
    tool_use_result = row.get("toolUseResult")
    if not isinstance(tool_use_result, dict):
        return None
    answers = tool_use_result.get("answers")
    if not isinstance(answers, dict):
        return None
    for value in answers.values():
        if isinstance(value, str) and message_opens_with_go(value):
            return value
    return None


def _parse_transcript_timestamp(raw) -> datetime | None:
    """Best-effort parse of a transcript row's own `timestamp` field (observed live format:
    "2026-09-24T06:34:30.372Z"). Returns None on anything unparseable -- every caller must treat
    that as "cannot verify freshness", never as "fresh"."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def fresh_ask_user_question_go(transcript_path: str) -> tuple[str | None, str | None]:
    """(answer_text, question_text) for the MOST RECENT qualifying AskUserQuestion-GO row in this
    session's own transcript that is still within ASK_USER_QUESTION_GO_MAX_AGE_HOURS, or
    (None, None).

    Deliberately independent of `latest_genuine_operator_text`'s "single most recent genuine
    message" rule. Operator instruction, 2026-09-24, after a live incident: a session received a
    genuine "ГО ... (Recommended)" AskUserQuestion answer, did not act on it immediately, and a
    later ordinary operator remark (not itself a GO) silently invalidated it under that
    turn-scoped rule -- correct per that rule's own design (§4's "a NEW operator message does not
    carry the old GO forward"), but not what the operator wants for THIS specific channel. Asked
    directly via a clarifying AskUserQuestion whether "any answer" should count (it must not -- see
    `_ask_user_question_go_answer`'s own strictness) or specifically that an AskUserQuestion-GO
    should outlive an unrelated later message: the operator picked the latter.

    An AskUserQuestion answer is an unambiguous, explicitly SELECTED decision, not an interpreted
    free-text signal the way "что дальше по плану?" was -- so instead of the strict turn-scoped
    rule, it gets a bounded FRESHNESS WINDOW, the same model this hook already uses for a GPT-PM
    APPROVE receipt (`gpt_pm_approve_covers` above), applied here to a structural decision in the
    transcript instead of a receipt file. Full scan (not tail-bounded), for the same reason
    `latest_genuine_operator_text` falls back to one: a long session can push the qualifying row
    past the bounded tail window well within a freshness window that should still cover it."""
    now = datetime.now(timezone.utc)
    result: tuple[str | None, str | None] = (None, None)
    for row in _tail_rows(transcript_path, full=True):
        if row.get("type") != "user":
            continue
        answer = _ask_user_question_go_answer(row)
        if answer is None:
            continue
        ts = _parse_transcript_timestamp(row.get("timestamp"))
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_hours = (now - ts).total_seconds() / 3600.0
        if age_hours > ASK_USER_QUESTION_GO_MAX_AGE_HOURS:
            continue
        tool_use_result = row.get("toolUseResult") or {}
        answers = tool_use_result.get("answers") or {}
        question = next((q for q, a in answers.items() if a == answer), None)
        result = (answer, question)  # keep overwriting -- rows are oldest-first, want the latest
    return result


def _mid_turn_attachment_text(row: dict) -> str | None:
    """Genuine operator text from a mid-turn QUEUED message row, or None.

    Confirmed live, 2026-09-24/25 (relayed report, then verified directly against the real
    transcript per CLAUDE.md §23 -- the other session's own hypothesis was correct, but was
    verified rather than trusted on its say-so): the operator can type while Claude Code is
    mid-turn, and the harness logs that as a row shaped completely differently from an ordinary
    user turn -- `type: "attachment"` (never `"user"`), the real text sitting at
    `attachment.prompt[i].text`, not `message.content`. `latest_genuine_operator_text`'s loop
    previously only ever inspected `type == "user"` rows, so a genuine "ГО" sent this way was
    silently invisible to this hook end to end, indistinguishable from no message at all.

    Guarded to the genuine human-typed shape only: `attachment.type == "queued_command"` and
    `attachment.origin.kind == "human"` (excludes any automated/system-queued attachment, if that
    shape exists) -- text is the concatenation of every `type: "text"` block in `attachment.prompt`,
    mirroring `_operator_text`'s own text-block handling for an ordinary user row.
    """
    attachment = row.get("attachment")
    if not isinstance(attachment, dict):
        return None
    if attachment.get("type") != "queued_command":
        return None
    origin = attachment.get("origin")
    if not isinstance(origin, dict) or origin.get("kind") != "human":
        return None
    prompt = attachment.get("prompt")
    if not isinstance(prompt, list):
        return None
    text = "".join(
        block.get("text", "")
        for block in prompt
        if isinstance(block, dict) and block.get("type") == "text"
    )
    text = text.strip()
    return text or None


# A short, harness-generated status confirmation ("Tool loaded.") logged in the same row shape as
# genuine operator text -- added 2026-09-25 after a relayed report: this string became the "most
# recent genuine operator message" immediately after a real operator GO, silently invalidating it
# under the existing (otherwise correct) turn-scoped rule. Same failure shape as the
# HOOK_FEEDBACK_PREFIX exclusion `_operator_text` already applies to a Stop-hook's own feedback --
# a different automated source, same fix pattern: exact/narrow match, not a broad heuristic, so a
# genuine short operator message ("Tool loaded. Continue" or similar) is never at risk of being
# misread as automation.
_AUTOMATED_STATUS_TEXT_RE = re.compile(r"^tools?\s+loaded\.?$", re.IGNORECASE)


def _is_automated_status_text(text: str) -> bool:
    return bool(_AUTOMATED_STATUS_TEXT_RE.match(text.strip()))


def fresh_plain_text_go(transcript_path: str) -> tuple[str | None, float | None]:
    """(go_text, age_hours) for the MOST RECENT genuine message -- an ordinary user turn, a
    mid-turn queued attachment, or an AskUserQuestion answer -- that independently qualifies as a
    GO under `message_opens_with_go`'s own strict rules, provided it is still within
    `PLAIN_GO_MAX_AGE_HOURS`. (None, None) if nothing qualifying is fresh enough.

    Deliberately independent of `latest_genuine_operator_text`'s "single most recent genuine
    message" rule, mirroring `fresh_ask_user_question_go` above for the exact same reason (see
    `PLAIN_GO_MAX_AGE_HOURS`'s own comment for the triggering incident): an explicit, deliberate GO
    should not evaporate just because a later, unrelated message was not itself a GO. Uses the
    SAME row-filtering as `latest_genuine_operator_text` (automation/non-human exclusions) -- only
    the "turn-scoped vs freshness-windowed" policy differs, never what counts as a genuine message.
    Full scan (not tail-bounded), for the same reason `fresh_ask_user_question_go` uses one: a long
    session can push the qualifying row past the bounded tail window well within a freshness
    window that should still cover it."""
    now = datetime.now(timezone.utc)
    result: tuple[str | None, float | None] = (None, None)
    for row in _tail_rows(transcript_path, full=True):
        row_type = row.get("type")
        if row_type == "attachment":
            if row.get("isCompactSummary") or row.get("isMeta") or row.get("isSidechain"):
                continue
            text = _mid_turn_attachment_text(row)
        elif row_type == "user":
            if row.get("isCompactSummary") or row.get("isMeta") or row.get("isSidechain"):
                continue
            text = _operator_text((row.get("message") or {}).get("content"))
            if text is not None and _non_human_origin(row):
                continue
            if text is None:
                text = _ask_user_question_go_answer(row)
        else:
            continue
        if text is None or _is_automated_status_text(text):
            continue
        if not message_opens_with_go(text):
            continue
        ts = _parse_transcript_timestamp(row.get("timestamp"))
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_hours = (now - ts).total_seconds() / 3600.0
        if age_hours > PLAIN_GO_MAX_AGE_HOURS:
            continue
        result = (text, age_hours)  # rows are oldest-first -- keep overwriting for the latest
    return result


def latest_genuine_operator_text(transcript_path: str, *, full: bool) -> str | None:
    last_text = None
    for row in _tail_rows(transcript_path, full=full):
        row_type = row.get("type")
        if row_type == "attachment":
            if row.get("isCompactSummary") or row.get("isMeta") or row.get("isSidechain"):
                continue
            text = _mid_turn_attachment_text(row)
            if text is None or _is_automated_status_text(text):
                continue
            last_text = text
            continue
        if row_type != "user":
            continue
        if row.get("isCompactSummary") or row.get("isMeta") or row.get("isSidechain"):
            continue
        text = _operator_text((row.get("message") or {}).get("content"))
        if text is not None and _non_human_origin(row):
            continue
        if text is None:
            text = _ask_user_question_go_answer(row)
        if text is None or _is_automated_status_text(text):
            continue
        last_text = text
    return last_text


def _is_under(path, root) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (OSError, ValueError):
        return str(Path(path).resolve()) == str(Path(root).resolve())


def _git_toplevel(cwd) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _call_target_dir(tool_input: dict, fallback_cwd) -> str | None:
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    if target:
        try:
            return str(Path(target).parent)
        except (OSError, ValueError):
            return fallback_cwd
    return fallback_cwd


def gpt_pm_approve_covers(tool_input: dict, cwd) -> tuple[bool, str]:
    """True if a fresh, correlated GPT-PM VERDICT: APPROVE receipt covers this call's repo --
    the §20 authorization path. Mirrors gpt_review_gate.py's own receipt store (`RECEIPTS_FILE`)
    and repo-scoping so the two gates never disagree about what "a genuine APPROVE" means. Same
    known imprecision §15 already documents applies here: a receipt proves SOME review ran
    recently for the REPO, not that it covered this specific call."""
    if not RECEIPTS_FILE.is_file():
        return False, "no GPT review receipts file"

    target_dir = _call_target_dir(tool_input, cwd)
    repo_root = _git_toplevel(target_dir) if target_dir else None
    if not repo_root:
        return False, "call target is not inside a git repository"

    try:
        with RECEIPTS_FILE.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return False, "receipts file unreadable"

    now = datetime.now(timezone.utc)
    for line in reversed(lines[-500:]):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        entry_cwd = entry.get("cwd")
        ts_raw = entry.get("timestamp")
        if not entry_cwd or not ts_raw:
            continue
        if not _is_under(entry_cwd, repo_root):
            continue
        # Only the MOST RECENT receipt for this repo counts -- an old APPROVE must not be found by
        # scanning past a newer, non-qualifying receipt (mirrors gpt_review_gate.py's own
        # latest_receipt_for_repo, which stops at the first repo match too).
        try:
            ts = datetime.fromisoformat(ts_raw)
        except ValueError:
            return False, f"latest receipt for {repo_root} has an unparseable timestamp"
        age_hours = (now - ts).total_seconds() / 3600.0
        if age_hours > RECEIPT_MAX_AGE_HOURS:
            return False, f"latest receipt for {repo_root} is {age_hours:.1f}h old (stale)"
        verdict = str(entry.get("verdict") or "")
        if entry.get("ok") and entry.get("correlated", True) and VERDICT_APPROVE_RE.search(verdict):
            return True, f"GPT-PM APPROVE receipt for {repo_root} ({age_hours:.1f}h old)"
        return False, (f"latest receipt for {repo_root} is not a qualifying APPROVE "
                        f"(ok={entry.get('ok')}, correlated={entry.get('correlated')}, "
                        f"verdict={verdict!r})")
    return False, f"no receipt found for {repo_root} within {RECEIPT_MAX_AGE_HOURS}h"


def rosetta_plan_go_covers(tool_input: dict, cwd, session_id: str | None) -> tuple[bool, str]:
    """True if THIS session holds a genuine GPT-PM-approved, in-progress Rosetta plan (CLAUDE.md
    §19) scoped to this call's repo -- the THIRD authorization path, added 2026-09-17 on explicit
    operator GO ("2 ГО"), after a session following the Rosetta protocol (`pm_rosetta_go`) still
    got denied here with no way to satisfy this hook short of asking the operator for a redundant
    second GO on top of GPT-PM's already-approved plan.

    This deliberately overrides §19's own stated R0 default for THIS hook specifically: §19 says
    Rosetta "records and denies nothing" today and defers real enforcement to Gate B, because "an
    enforced GO must know *which* GPT-PM conversation authorized the work, and that binding still
    degrades to a folder-name convention" -- a known, named, NOT-yet-fixed weakness, not a new one
    introduced here. The operator was told this directly and chose to accept it now rather than
    wait for Gate B. Reuses `_rosetta_common.active_plan` -- the exact same check `rosetta_audit.py`
    already performs for its own audit recording -- so this hook and Rosetta's own future
    enforcement can never disagree about what counts as an approved plan.

    A plan's approval covers every mutating call for as long as that plan stays `in-progress`
    (mirrors CLAUDE.md §4's own "one GO covers the whole bounded gate" semantics -- a Rosetta plan
    IS a gate). It stops covering anything the moment the plan closes (`pm_rosetta_close`) or a
    materially different plan replaces it (`active_plan` itself already enforces the
    `approved_plan_hash == plan_hash` pairing, so an edited-but-unapproved plan never qualifies)."""
    if not session_id:
        return False, "no session_id in hook payload"
    target_dir = _call_target_dir(tool_input, cwd)
    repo_root = _git_toplevel(target_dir) if target_dir else None
    if not repo_root:
        return False, "call target is not inside a git repository"
    plan, state = active_plan(session_id, repo_root)
    if state != "ok":
        return False, f"no valid approved Rosetta plan for this repo ({state})"
    return True, f"Rosetta plan {plan.get('plan_id', '?')!s} approved, in-progress, scoped to {repo_root}"


REASON_TEMPLATE = (
    "БЕЗ ВАЛИДНОГО GO: ни последнее подлинное сообщение оператора, ни свежий (<= {plain_go_hours}ч) "
    "отдельно стоящий GO/ГО где-либо в сессии, ни свежая квитанция GPT-PM APPROVE не покрывает этот "
    "репозиторий, поэтому мутирующий вызов ({tool}) заблокирован -- CLAUDE.md §4/§20, механически "
    "обеспечено хуком go_gate.py (2026-09-17).\n\n"
    "Это попадает в список действий, требующих GO: {reason} (target: {target}).\n"
    "Свежий отдельный GO/ГО: {plain_go_reason}.\n"
    "GPT-PM APPROVE путь: {gpt_reason}.\n"
    "Rosetta pm_rosetta_go путь: {rosetta_reason}.\n"
    "Свежий ГО через AskUserQuestion: {askq_reason}.\n\n"
    "Если GO был дан недавно (в пределах {plain_go_hours}ч) явным отдельным словом GO/ГО -- это "
    "должно было пройти через путь выше; проверь, не было ли сообщение с GO слишком длинным (ГО "
    "засчитывается только как первый токен сообщения, либо как последний токен КОРОТКОГО "
    "сообщения -- большой вставленный текст, заканчивающийся на GO, не считается, чтобы случайный "
    "хвост лога не сработал как разрешение). Не переформулируй команду, чтобы обойти это -- сообщи, "
    "что собирался сделать, и пришли GO отдельным, коротким сообщением.\n\n"
    "Kill switch при ложном срабатывании: CLAUDE_GO_GATE=off (до старта процесса)."
)


# Decision-log APPEND-ONLY carve-out -- operator instruction, 2026-09-25 ("почини и исключи
# дисижен лог из хука"), narrowed via AskUserQuestion (append-only, not a blanket file exemption --
# the operator picked the narrower option over "exclude the whole file"). CLAUDE.md's own
# per-project convention ("Continuous Decision/Evidence/Refusal Log Per Project") asks for a
# `core/DECISION_LOG.md` entry after nearly every non-trivial decision -- a UserPromptSubmit hook
# reminds of this on every turn -- so requiring a fresh GO for every single entry is pure friction
# with no matching risk: this is a journal, not executable code or a governance-hash file.
#
# NOT a blanket exemption: Edit/Write/NotebookEdit/MultiEdit still always need GO for every OTHER
# target, and even for THIS file, only a byte-preserving APPEND is exempt -- deleting or rewriting
# any existing entry (falsifying the audit trail this file exists to be) still requires GO exactly
# like before. The safety property is deliberately simple and easy to verify: the new content must
# have the old content as an exact PREFIX, so nothing already on disk can be removed or altered,
# only grown -- true independent of WHERE in the file an Edit's `old_string` happens to match, and
# true for a brand-new file (nothing to preserve yet).
_DECISION_LOG_BASENAME = "decision_log.md"


def _is_decision_log_path(file_path) -> bool:
    """True when `file_path`'s basename is exactly `DECISION_LOG.md` (case-insensitive) -- a
    distinctive enough name that no unrelated file is expected to collide with it, in any repo."""
    if not isinstance(file_path, str) or not file_path:
        return False
    try:
        return Path(file_path).name.lower() == _DECISION_LOG_BASENAME
    except (OSError, ValueError):
        return False


def _is_append_only_edit(tool_input: dict) -> bool:
    """True only when `new_string` has `old_string` as an exact prefix -- every byte already
    matched by `old_string` survives unchanged in `new_string`, so this Edit can only ADD content,
    never remove or alter any existing byte, regardless of where in the file `old_string` matched."""
    old = tool_input.get("old_string")
    new = tool_input.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str):
        return False
    return new.startswith(old) and len(new) > len(old)


def _is_append_only_multiedit(tool_input: dict) -> bool:
    """True only when EVERY edit in a MultiEdit call independently satisfies
    `_is_append_only_edit` -- one non-conforming edit in the batch still requires GO for the whole
    call."""
    edits = tool_input.get("edits")
    if not isinstance(edits, list) or not edits:
        return False
    return all(isinstance(edit, dict) and _is_append_only_edit(edit) for edit in edits)


def _is_append_only_write(tool_input: dict, cwd) -> bool:
    """True only when the new file content has the file's CURRENT on-disk content (or "" if the
    file does not exist yet) as an exact prefix -- same non-destructive guarantee as
    `_is_append_only_edit`, applied to a full-file Write instead of a matched substring. Fails
    closed (False) on any read/decode error -- "cannot verify nothing was removed" is never treated
    as "nothing was removed"."""
    file_path = tool_input.get("file_path")
    new_content = tool_input.get("content")
    if not isinstance(file_path, str) or not isinstance(new_content, str):
        return False
    try:
        path = Path(file_path)
        if not path.is_absolute():
            path = Path(cwd) / path
        current = path.read_text(encoding="utf-8") if path.is_file() else ""
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    return new_content.startswith(current)


def _decision_log_append_exempt(tool_name: str, tool_input: dict, cwd) -> bool:
    """True only when this call is an APPEND-ONLY Edit/Write/MultiEdit targeting a
    `DECISION_LOG.md` file -- the general go_gate GO requirement is lifted for exactly this shape,
    nothing broader. Any other tool, any other target, or any non-append shape of edit to this same
    file still requires GO exactly as before."""
    if not _is_decision_log_path(tool_input.get("file_path")):
        return False
    if tool_name == "Edit":
        return _is_append_only_edit(tool_input)
    if tool_name == "MultiEdit":
        return _is_append_only_multiedit(tool_input)
    if tool_name == "Write":
        return _is_append_only_write(tool_input, cwd)
    return False


# PM-Bridge-active exemption for Edit/Write/NotebookEdit/MultiEdit -- operator instruction,
# 2026-09-27, after a GPT-PM-APPROVE regex bug (fixed above, same day) left an autonomous
# PM-Bridge session with Edit/Write completely blocked and no working authorization route at all
# (no operator present to type a literal GO, Rosetta plan-binding independently broken, and the
# APPROVE-receipt path dead). Operator's own words: "он не должен блокировать едит или запись,
# единственное что он должен блокировать так это когда я лично пишу что-то в чат и прошу
# действия... все остальное блокировать не надо, тем более в пм бридж режиме." Narrowed via
# AskUserQuestion to a concrete, checkable signal rather than the broadest reading (dropping GO for
# Edit/Write everywhere, in every session): active PM-Bridge orchestrator mode
# (`_report_common.pm_mode_active()`, the SAME liveness check `report_gate.py`/`report_due.py`
# already use for CLAUDE.md §18) is the sole exemption condition. When it is on, Edit/Write/
# NotebookEdit/MultiEdit skip the GO requirement entirely; a live interactive session with PM-Bridge
# mode off is completely unaffected -- it still needs one of the four GO sources exactly as before.
# Deliberately does NOT touch Bash/PowerShell's own separate blocklist (`bash_needs_go`) -- the
# operator's instruction named "едит или запись" specifically, not shell commands, and that
# classifier already has its own narrower, independently-reasoned exemptions.
#
# Known, accepted risk, stated plainly rather than glossed over: `pm_mode_active()` is a MACHINE-
# WIDE check (one shared `state/orchestrator.json` + a port probe), not scoped to the session
# actually invoking this hook. While `/pm-bridge-mode on` is active for ANY session on this
# machine, EVERY session's Edit/Write mutation is exempt from go_gate.py's GO requirement, not just
# the autonomous one the operator meant. This is the same accepted machine-wide scope §18 already
# relies on for the report-cadence checks -- extending it to the core Edit/Write authorization gate
# is a materially bigger consequence, and the operator was told this in these terms before choosing
# this option over "do nothing yet."
_PM_MODE_EXEMPT_TOOLS = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})


def _patch_repo(path: str, cwd: str) -> str:
    """git top-level of a patched file (its folder when outside git), lower-cased for comparison."""
    p = Path(path) if Path(path).is_absolute() else Path(cwd) / path
    d = p.parent
    while not d.exists() and d != d.parent:
        d = d.parent
    try:
        r = subprocess.run(["git", "-C", str(d), "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                           timeout=5)
        top = r.stdout.strip() if r.returncode == 0 else str(d)
    except (OSError, subprocess.SubprocessError):
        top = str(d)
    return top.replace("\\", "/").rstrip("/").lower()


def main():
    if os.environ.get("CLAUDE_GO_GATE", "").lower() == "off":
        allow()

    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        context_unavailable({}, "malformed PreToolUse input")
        return
    if not isinstance(data, dict):
        context_unavailable({}, "PreToolUse payload is not an object")
        return

    if agentio.client(data) == "codex":
        if not data.get("tool_name") or not isinstance(data.get("tool_input"), dict):
            context_unavailable(data, "PreToolUse tool_name or tool_input is missing or invalid")
            return

    tool_name = data.get("tool_name") or ""
    tool_input = data.get("tool_input") or {}
    cwd = data.get("cwd") or os.getcwd()

    # Codex -> Claude vocabulary (Claude payloads pass through unchanged): shell tools become Bash /
    # PowerShell with a plain `command`; apply_patch becomes an Edit on the first patched file (no
    # old_string, so it is never treated as an append-only DECISION_LOG edit).
    if agentio.is_shell(data) and tool_name not in ("Bash", "PowerShell"):
        tool_name = "PowerShell" if agentio.shell_kind(data) == "powershell" else "Bash"
    if agentio.is_shell(data):
        tool_input = {**tool_input, "command": agentio.shell_command(data)}
    elif tool_name == "apply_patch":
        patched = agentio.edited_paths(data)
        # Coverage (GPT-PM receipt, plan GO) is judged per repository from one path, so a patch that spans
        # several repositories is denied: one Claude Edit call is one file, one Codex patch must be one repo.
        if len({_patch_repo(p, cwd) for p in patched}) > 1:
            deny("GO gate: this apply_patch edits files in more than one repository. Split it into one patch "
                 "per repository so each one is checked against its own approval: " + ", ".join(patched[:6]))
            return
        tool_name = "Edit"
        tool_input = {"file_path": patched[0] if patched else ""}

    try:
        if tool_name in ("Bash", "PowerShell"):
            command = str(tool_input.get("command") or "")
            is_mutation, reason = bash_needs_go(
                command, cwd, is_powershell=(tool_name == "PowerShell")
            )
            target = command[:500]
        else:
            is_mutation, reason, target = classify_call(tool_name, tool_input)
            if is_mutation and _decision_log_append_exempt(tool_name, tool_input, cwd):
                is_mutation, reason = False, "decision_log_append"
            elif is_mutation and tool_name in _PM_MODE_EXEMPT_TOOLS and pm_mode_active():
                is_mutation, reason = False, "pm_mode_active"
    except Exception as exc:
        context_unavailable(data, f"could not classify tool input: {type(exc).__name__}")
        return

    if not is_mutation:
        allow()
        return

    transcript_path = data.get("transcript_path")
    if not transcript_path:
        context_unavailable(data, "transcript_path is missing")
        return

    try:
        last_text = latest_genuine_operator_text(transcript_path, full=False)
        if last_text is None:
            # Bounded tail found no genuine user row at all -- rare (very long single turn with
            # heavy tool output pushing the last real message past the tail window); fall back to
            # a full scan rather than silently treat "not found in the tail" as "never said".
            last_text = latest_genuine_operator_text(transcript_path, full=True)
    except Exception as exc:
        context_unavailable(data, f"transcript could not be read: {type(exc).__name__}")
        return

    if last_text is None:
        # `_tail_rows` "fails silent (yields nothing) on any missing file, unreadable path, or
        # parse error" (_report_common.py). Claude keeps its established fail-open behavior. In Codex
        # a readable transcript without operator text is normal for a spawned subagent (its only user
        # row is the injected AGENTS.md context), so there is no GO here, but the other authorizations
        # (GPT-PM receipt, plan GO) must still be checked; the final deny below applies if none covers.
        if agentio.client(data) != "codex":
            allow()
            return
        last_text = ""

    if message_opens_with_go(last_text):
        allow()
        return

    try:
        plain_go_text, plain_go_age = fresh_plain_text_go(transcript_path)
    except Exception:
        plain_go_text, plain_go_age = None, None

    if plain_go_text is not None:
        allow()
        return

    plain_go_reason = f"нет отдельного GO/ГО за последние {PLAIN_GO_MAX_AGE_HOURS}ч где-либо в сессии"

    try:
        gpt_ok, gpt_reason = gpt_pm_approve_covers(tool_input, cwd)
    except Exception:
        gpt_ok, gpt_reason = False, "receipt check failed internally"

    if gpt_ok:
        allow()
        return

    try:
        rosetta_ok, rosetta_reason = rosetta_plan_go_covers(tool_input, cwd, data.get("session_id"))
    except Exception:
        rosetta_ok, rosetta_reason = False, "rosetta plan check failed internally"

    if rosetta_ok:
        allow()
        return

    try:
        askq_answer, askq_question = fresh_ask_user_question_go(transcript_path)
    except Exception:
        askq_answer, askq_question = None, None

    if askq_answer is not None:
        allow()
        return

    askq_reason = (
        f"нет квалифицирующего ответа AskUserQuestion за последние {ASK_USER_QUESTION_GO_MAX_AGE_HOURS}h"
    )

    deny(REASON_TEMPLATE.format(
        tool=tool_name, reason=reason, target=target or "?",
        plain_go_hours=PLAIN_GO_MAX_AGE_HOURS, plain_go_reason=plain_go_reason,
        gpt_reason=gpt_reason, rosetta_reason=rosetta_reason, askq_reason=askq_reason,
    ))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # A safety hook must never be the thing that breaks a session.
        allow()
