"""Shared counting logic for the report hooks.

`report_due.py` (Stop) and `report_gate.py` (PreToolUse on `git commit`/`git push`) must agree on
what "unreported work" means, or a session gets blocked by one and cleared by the other. The
definition lives here once.

Thresholds were set from measured behaviour on 2026-08-19 across six repositories in D:\\Repo --
longest run of consecutive commits with no commit touching reports/: ERP 10, formcoach 5,
Virtual_marketing_company 4, Fitness_App 4, AI_trading_assistance 3, Ferma 1. Three is where
ordinary work stops and a silent gate starts; a lone commit never trips anything, because that is
the small-fix case.

`unanswered_handover()` (added 2026-08-19, same day) covers a different gap: the skill says to
STOP after handing a report over, but nothing enforced that. A Fitness_App session handed over a
stop-the-line report -- dream-team review found the tested APK wasn't even built from the branch
under test, MVP gate not closed -- got no operator reply (only an automatic context-compaction
boundary), and kept going: wrote a second report, committed it, and pushed. The operator caught
this from a VS Code "open external website" popup on the first report's link and asked why the
session hadn't simply stopped. It reads the session's own transcript for the most recent complete
handover (a real, non-placeholder `reports/...ru.html` path plus an artifact URL in the same
assistant message) and whether a genuine operator message -- not a compaction boundary, not a
sidechain/subagent message -- has arrived since. No reply since the last handover means the next
`git commit` or `git push` is denied outright, independent of anything it carries.

`program_mode_declared()` (added 2026-08-19, same day, third revision) is the fix for a design
tension the two rules above created together: "write a report per closed gate" plus "stop after
every report" adds up to "stop after every sub-gate" -- exactly wrong for a long run the operator
has already pre-authorized end to end and does not want to babysit. The operator's own words:
"нужно дождаться окончания прогона а потом писать финальный репорт и не останавливаться после
каждого сабгейта" -- wait for the run to finish, then write the final report, don't stop after
every sub-gate. The fix is not an env-var opt-out (that was proposed and explicitly rejected here:
env vars must be set before a session starts, so they cannot cover "the operator says so mid-run"),
it is a marker phrase in a GENUINE operator message -- the same trust boundary `unanswered_handover`
already uses to unblock a handover, extended to also suspend both report-cadence checks for the
rest of the transcript. A model cannot forge a genuine `user`-typed transcript row, so this cannot
be self-authorized any more than the existing "operator replied" unblock can.

Bug found and fixed 2026-08-21: `program_mode_declared()` used the shared `_tail_rows()` helper
with its default bounded read (`TRANSCRIPT_TAIL_BYTES`, 3 MB) -- fine for `unanswered_handover()`,
which genuinely only cares about recent history, wrong here, where the docstring's own promise is
"anywhere in the transcript". A program-mode declaration is typically said once near the start of a
long run; once that session's transcript grows past 3 MB (routine for a multi-gate program), the
declaring message falls out of the tail window and the check silently starts returning False again
`pm_mode_active()` / `program_mode_active()` (added 2026-08-26) give program mode a second, standing
trigger: PM Bridge orchestrator mode being ON. `program_mode_declared` only fires in the session the
operator typed the marker phrase into, so every OTHER project kept stopping at its report handover
during a pre-authorized run. Operator: "надо убедиться что такое не будет повторяться ни на одном
проекте если включен пм режим, сессия может закончиться только тогда когда задача полностью
выполнена." See the block above those functions for why liveness is probed rather than trusted.

-- reported live as "program mode does not survive context compaction" from an ERP-style session,
though the actual mechanism is the byte-bounded file read, not the model's own context compaction
(the two often coincide because both are driven by transcript size, which is why it read as one
problem). Fixed by scanning the full file via `_tail_rows(path, full=True)` the first time, then
caching the result in a one-line per-session marker file (`_program_mode_marker_path`) so every
later call in the same session is an O(1) file-existence check instead of a repeated full scan.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _agentio as agentio  # noqa: E402  (Claude and Codex transcript/state helpers)

MIN_COMMITS = 3
STALE_COMMITS = 2
STALE_AGE_SECONDS = 3 * 3600
LOOKBACK = "36.hours"
FRESH_REPORT_SECONDS = 900
TRANSCRIPT_TAIL_BYTES = 3_000_000

RU_REPORT_PATH_RE = re.compile(
    r'reports[\\/][^\s()\[\]"\']+(?:\.ru\.html|_ru\.html)', re.IGNORECASE
)
ARTIFACT_URL_RE = re.compile(
    r"https://claude\.ai/(?:code/artifact/[0-9a-f-]{36}|artifact/[0-9A-Za-z_-]{16,40})"
)
# Two real URL shapes have been observed from the Artifact tool: the legacy
# .../code/artifact/<uuid> form (36-char hex-dash id) and the current short-link form,
# .../artifact/<id> with no "code/" segment and a ~22-char mixed-case alphanumeric id
# (e.g. https://claude.ai/artifact/FddYzuHu6FGLzzyovLTFzg, confirmed live 2026-09-22).
# The tool switched formats without this regex being updated, so a genuine, correctly
# published artifact link was failing this check -- not a missing handover.
# A path mention is only real evidence of a handover if it names an actual file. The same regex
# also matches placeholder/example paths -- "reports/<NAME>.ru.html" from SKILL_HINT itself,
# "reports/...ru.html" written as prose describing this very pattern -- and both appear constantly
# when a reply explains or quotes a report-hook's own reason text. Filtering them out here is what
# keeps every check built on this pattern from re-triggering on its own explanation.
PLACEHOLDER_MARKERS = ("<", ">", "..", "ИМЯ", "NAME", "XXX")

_SKILL_HINT_STEPS = (
    "  1. Вызови скилл html-report и следуй ему.\n"
    "  2. Напиши пару reports/<ИМЯ>.ru.html и reports/<ИМЯ>.html.\n"
    "  3. Прогони tools/report_conform.py из набора harness: python <harness>/tools/report_conform.py <repo>/reports\n"
    "  4. Закоммить ОБА файла.\n"
    "  5. Показать оператору в сессии только русскую версию: ссылку на артефакт и локальный путь,\n"
    "     плюс короткое изложение сути прямо в ответе. НЕ предлагать открыть файл, не запускать\n"
    "     браузер и не поднимать никаких окон.\n"
)

_STEP6_STOP = (
    "  6. ОСТАНОВИТЬСЯ и ждать оператора. Не переходить к следующему гейту после отчёта."
)
_STEP6_CONTINUE = (
    "  6. ПРОДОЛЖАТЬ. ПМ-режим включён: отчёт — точка фиксации, а не точка остановки.\n"
    "     Переходи к следующему гейту, не дожидаясь ответа оператора."
)


def skill_hint(pm_mode: bool = False) -> str:
    """The six-step report procedure. Step 6 inverts under PM mode -- see `pm_mode_active`."""
    return _SKILL_HINT_STEPS + (_STEP6_CONTINUE if pm_mode else _STEP6_STOP)


# Kept as a module constant because both hooks imported it as one before `skill_hint()` existed,
# and because the stop-variant remains the default whenever PM mode is off.
SKILL_HINT = skill_hint(False)


_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def git(args: list[str], root: Path | str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(root)] + args, capture_output=True, text=True, timeout=10,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def unreported(root: Path) -> list[tuple[str, str, int]]:
    """Commits made after the most recent commit that touched reports/, newest first."""
    log = git(["log", f"--since={LOOKBACK}", "--format=%H%x1f%h%x1f%ct%x1f%s"], root)
    if not log:
        return []

    last_report = git(["log", "-1", f"--since={LOOKBACK}", "--format=%H", "--", "reports"], root)

    run = []
    for line in log.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue
        full, short, when, subject = parts
        if last_report and full == last_report:
            break
        try:
            run.append((short, subject, int(when)))
        except ValueError:
            continue
    return run


def over_threshold(run: list[tuple[str, str, int]]) -> bool:
    if not run:
        return False
    if len(run) >= MIN_COMMITS:
        return True
    oldest_age = time.time() - min(when for _, _, when in run)
    return len(run) >= STALE_COMMITS and oldest_age >= STALE_AGE_SECONDS


def report_being_written(root: Path) -> bool:
    """True while report files are fresh on disk -- the session is mid-write, so stay quiet."""
    reports = Path(root) / "reports"
    if not reports.is_dir():
        return False
    now = time.time()
    try:
        return any(
            now - path.stat().st_mtime < FRESH_REPORT_SECONDS for path in reports.glob("*.html")
        )
    except OSError:
        return False


def listing(run: list[tuple[str, str, int]], limit: int = 8) -> str:
    lines = [f"  - {short}  {subject[:96]}" for short, subject, _ in run[:limit]]
    if len(run) > limit:
        lines.append(f"  - ... и ещё {len(run) - limit}")
    return "\n".join(lines)


def _complete_handover(text: str) -> tuple[str, str] | None:
    """(path, artifact_url) if this text is a full handover, else None."""
    url_hit = ARTIFACT_URL_RE.search(text)
    if not url_hit:
        return None
    for hit in RU_REPORT_PATH_RE.finditer(text):
        path = hit.group(0)
        if not any(marker in path for marker in PLACEHOLDER_MARKERS):
            return path, url_hit.group(0)
    return None


HOOK_FEEDBACK_PREFIX = "Stop hook feedback:"


def _operator_text(content) -> str | None:
    """Genuine typed operator text from a transcript row's `message.content`, or None.

    A real user turn is logged in EITHER shape depending on how it was submitted: a bare string,
    or a list of content blocks with a `type: "text"` entry (confirmed 2026-08-19 -- a `ГО:
    ПРОДОЛЖАЙ...` authorization sent 3.5 minutes after a report handover was missed entirely
    because it used the list shape, and this function only checked `isinstance(content, str)`;
    the check then denied three separate sessions' next commit/push despite a real, on-time
    operator reply already sitting in their own transcripts). A `tool_result`-only list -- what
    follows literally every tool call -- must NOT count, so only `text`-typed blocks are read.

    A hook's own hookSpecificOutput.reason (e.g. a Stop-hook citation-integrity complaint) is
    logged as a same-shaped `type: "user"` row -- "Stop hook feedback: ..." -- and is automation,
    not the operator, so it is excluded by prefix.
    """
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    else:
        return None
    text = text.strip()
    if not text or text.startswith(HOOK_FEEDBACK_PREFIX):
        return None
    return text


def _non_human_origin(row: dict) -> bool:
    """True when a `type: "user"` row names an origin other than the operator.

    Measured 2026-09-27 across every local transcript: text rows carry `origin.kind` "human" (the
    operator, 1660 rows), "task-notification" (a background task finishing, 2895 rows) or "peer" (a
    message from ANOTHER Claude session). Only "human" is the operator. Read as operator text, the
    other two made a live GO stop counting the moment a background task reported in, would let a
    peer session's "GO ..." authorize this one, and would mark a report handover as answered or
    declare program mode without the operator saying anything. A row with no `origin` at all (older
    transcripts) keeps its previous treatment.
    """
    origin = row.get("origin")
    if not isinstance(origin, dict):
        return False
    kind = origin.get("kind")
    return kind is not None and kind != "human"


def _tail_rows(transcript_path: str | Path | None, *, full: bool = False):
    """Parsed JSON rows from a transcript, oldest first.

    Bounded to the tail (TRANSCRIPT_TAIL_BYTES) by default -- shared by every check that reads a
    session's own transcript and, for most of them (e.g. `unanswered_handover`), recent history is
    genuinely all that matters. Pass `full=True` to read the whole file instead, for a check whose
    correctness depends on "anywhere in the transcript", not just "recently" (see
    `program_mode_declared`).

    Fails silent (yields nothing) on any missing file, unreadable path, or parse error -- callers
    must treat "found nothing" as "allow", never as "found evidence of a problem".
    """
    if not transcript_path:
        return
    path = Path(transcript_path)
    try:
        if not path.is_file():
            return
        size = path.stat().st_size
        with path.open("rb") as fh:
            if not full and size > TRANSCRIPT_TAIL_BYTES:
                fh.seek(size - TRANSCRIPT_TAIL_BYTES)
                fh.readline()  # drop the partial line the seek landed inside
            raw = fh.read().decode("utf-8", "replace")
    except OSError:
        return

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("type") == "response_item":
            yield from _codex_row_as_claude(row)  # Codex transcript row -> Claude-shaped row(s)
        else:
            yield row


def _codex_row_as_claude(row: dict):
    """Rewrite one Codex `response_item` row into the Claude row shape the callers read
    ({"type": "user"|"assistant", "message": {"content": ...}, "timestamp"}), so every check built on
    `_tail_rows` (GO oracle, handover, program mode) works on Codex transcripts. Injected context is
    dropped by `_agentio._codex_records`; tool results become tool_result blocks (never operator text)."""
    ts = row.get("timestamp")
    try:
        recs = list(agentio._codex_records(row))
    except Exception:
        return
    for rec in recs:
        kind = rec["kind"]
        if kind == "user":
            out = {"type": "user", "message": {"role": "user", "content": rec.get("text", "")}}
        elif kind == "assistant":
            out = {"type": "assistant",
                   "message": {"role": "assistant", "content": [{"type": "text", "text": rec.get("text", "")}]}}
        elif kind == "tool_use":
            out = {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": rec.get("name", ""), "input": rec.get("input") or {}}]}}
        elif kind == "tool_result":
            out = {"type": "user", "toolUseResult": True, "message": {"role": "user", "content": [
                {"type": "tool_result", "content": rec.get("text", "")}]}}
        else:
            continue
        if ts:
            out["timestamp"] = ts
        yield out


def unanswered_handover(transcript_path: str | Path | None) -> str | None:
    """The report path of the most recent handover, if no genuine operator message followed it.

    Fails open (returns None) on any missing file, parse error, or unexpected shape: this check is
    aggressive enough -- it can deny a push outright -- that uncertainty must default to allowing,
    not to blocking.

    Handovers are deduplicated by artifact URL, not just counted. A session that gets blocked
    tends to explain itself -- "I handed over <link>, still waiting" -- and that explanation
    re-quotes the same link+path pair. Without dedup, every such explanation looks exactly like a
    fresh handover and re-arms the block, which is what actually happened in production 2026-08-19:
    an ERP session gave a real, on-time operator reply, got unblocked for one instant, then its own
    next message (explaining the *previous* block) re-quoted the same URL and re-locked itself --
    four denials in a row, all from one already-answered report. A URL seen once as answered stays
    answered for the rest of this transcript; only a URL that has never been resolved can still
    deny.
    """
    last_handover_path: str | None = None
    last_handover_url: str | None = None
    resolved_urls: set[str] = set()
    answered = True

    for row in _tail_rows(transcript_path):
        kind = row.get("type")

        if kind == "user":
            if row.get("isCompactSummary") or row.get("isMeta") or row.get("isSidechain"):
                continue
            if _non_human_origin(row):
                continue
            if _operator_text((row.get("message") or {}).get("content")):
                answered = True
                if last_handover_url:
                    resolved_urls.add(last_handover_url)
            continue

        if kind != "assistant" or row.get("isSidechain"):
            continue
        for block in (row.get("message") or {}).get("content") or []:
            if not (isinstance(block, dict) and block.get("type") == "text"):
                continue
            hit = _complete_handover(block.get("text") or "")
            if not hit:
                continue
            path_hit, url_hit = hit
            if url_hit in resolved_urls:
                continue  # a re-quote of an already-answered report, not a new handover
            last_handover_path, last_handover_url = path_hit, url_hit
            answered = False

    return None if answered else last_handover_path


# The operator's own established vocabulary (ГО/GO to authorize a gate, ПУШ/PUSH to authorize a
# push) extended with one more explicit marker for "this authorization covers the whole enumerated
# program, not just the next gate -- stop nagging me for a report at every sub-gate." Deliberately
# a small, fixed set of phrases rather than an attempt to infer intent from free prose: dictated
# Russian garbles words (see the global CLAUDE.md dictation note), and a marker that must be
# recognized reliably is worth more than one that tries to be clever.
PROGRAM_MODE_RE = re.compile(
    r"ОТЧЁТ\s*[:—-]\s*ТОЛЬКО\s+В\s+КОНЦЕ"
    r"|REPORT\s*[:—-]\s*(?:ONLY\s+AT\s+THE\s+END|END\s+OF\s+RUN\s+ONLY)"
    r"|не\s+останавливаться?\s+после\s+кажд\w+\s+(?:саб-?)?гейт\w*"
    r"|do\s+not\s+stop\s+after\s+each\s+sub-?gate"
    r"|АВТОНОМНАЯ\s+ПРОГРАММА\s+РАЗРЕШЕНА"
    r"|AUTONOMOUS\s+PROGRAM\s+AUTHORIZED",
    re.IGNORECASE,
)


def _program_mode_state_dir() -> Path | None:
    """Where the one-time 'program mode was declared' marker lives, keyed by session id.

    Lives under _agentio.state_dir("program_mode") (CLAUDE_HOOK_STATE_DIR override honoured), kept
    local to this module since program_mode_declared() is the only caller. Returns None if no
    directory is writable -- callers must fall back to scanning the transcript every time rather
    than failing outright.
    """
    override = os.environ.get("CLAUDE_HOOK_STATE_DIR")
    if override:
        try:
            target = Path(override) / "claude_program_mode"
            target.mkdir(parents=True, exist_ok=True)
            return target
        except OSError:
            pass
    target = agentio.state_dir("program_mode")
    return target if target.is_dir() else None


def _program_mode_marker_path(transcript_path: str | Path) -> Path | None:
    state_dir = _program_mode_state_dir()
    if state_dir is None:
        return None
    return state_dir / f"{Path(transcript_path).stem}.marker"


def program_mode_declared(transcript_path: str | Path | None) -> bool:
    """Has a genuine operator message anywhere in the transcript declared program mode?

    Once declared it holds for the rest of THIS transcript (this session) -- it is not re-checked
    per message, matching how a single GO already covers everything inside its own gate under the
    global authorization contract. A new session starts with a fresh transcript, so this never
    carries over to a session the operator did not address it to.

    Program mode suspends the report-cadence checks (unreported-work threshold, unanswered
    handover) -- not the report FORMAT rules, which still apply to whatever report eventually gets
    written, and not the operator's own destructive/safety/push protections, which this mechanism
    has no ability to touch at all.

    Checks the one-line marker file first (O(1)) before paying for a full-file scan -- see the
    2026-08-21 fix note at the top of this module for why the scan must cover the WHOLE transcript,
    not just its tail, and why that cost needs to be paid at most once per session.
    """
    if not transcript_path:
        return False

    marker = _program_mode_marker_path(transcript_path)
    if marker is not None:
        try:
            if marker.is_file():
                return True
        except OSError:
            pass

    for row in _tail_rows(transcript_path, full=True):
        if row.get("type") != "user":
            continue
        if row.get("isCompactSummary") or row.get("isMeta") or row.get("isSidechain"):
            continue
        if _non_human_origin(row):
            continue
        text = _operator_text((row.get("message") or {}).get("content"))
        if text and PROGRAM_MODE_RE.search(text):
            if marker is not None:
                try:
                    marker.write_text("1", encoding="utf-8")
                except OSError:
                    pass
            return True
    return False


# --- PM mode as a program-mode trigger (2026-08-26) --------------------------------------------
#
# `program_mode_declared` requires the operator to type a marker phrase in the session that needs
# it. That covers "the operator says so mid-run" but not the standing case: PM Bridge orchestrator
# mode is a GLOBAL toggle (`/pm-bridge-mode on`), deliberately not per-session, and turning it on
# already means "a pre-authorized run is under way across projects." Sessions kept stopping at the
# report handover anyway, in every project, because nothing connected the two.
#
# Operator, 2026-08-26: "надо убедиться что такое не будет повторяться ни на одном проекте если
# включен пм режим, сессия может закончиться только тогда когда задача полностью выполнена."
#
# The trigger is the orchestrator's own state file, which `pm_bridge_mode_on()` writes and
# `pm_bridge_mode_off()` removes -- the same record `orchestratorClient.js` uses, so there is no
# second source of truth to drift. Liveness is confirmed by connecting to the port it advertises,
# NOT by trusting the file: `orchestratorClient.js` already documents that a crashed orchestrator
# leaves the file behind, and a stale file must never silently disable a safety check for days.
# Direction of failure is deliberate and opposite to the checks it relaxes: anything uncertain --
# no file, unparseable, no port, connection refused -- reads as PM mode OFF, so the worst case is
# the status quo (a session stops and asks) rather than an unattended run past a stop-the-line
# report, which is the incident `unanswered_handover` exists for.
PM_MODE_PROBE_TIMEOUT_S = 0.4
PM_MODE_MAX_AGE_SECONDS = 24 * 3600


def _pm_mode_state_path() -> Path:
    """pm-bridge's `state/orchestrator.json`, wherever this machine keeps it.

    Honours `PM_BRIDGE_STATE_DIR` exactly as `src/state.js` does, so a relocated state directory
    (or a test fixture) is followed rather than silently missed. The fallback is absolute on
    purpose: these hooks run with the CWD of whatever project is being worked on, and the whole
    point of this check is that it answers the same way in every one of them.
    """
    override = os.environ.get("PM_BRIDGE_STATE_DIR")
    # No override = PM mode OFF (no state folder holds an orchestrator record), the safe direction.
    base = Path(override) if override else agentio.state_dir("pm_bridge_state")
    return base / "orchestrator.json"


def pm_mode_active() -> bool:
    """Is PM Bridge orchestrator mode on right now, with a process actually answering?"""
    path = _pm_mode_state_path()
    try:
        if not path.is_file():
            return False
        if time.time() - path.stat().st_mtime > PM_MODE_MAX_AGE_SECONDS:
            return False
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False

    port = record.get("port")
    if not isinstance(port, int) or not (0 < port < 65536):
        return False

    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), PM_MODE_PROBE_TIMEOUT_S):
            return True
    except OSError:
        return False


def program_mode_active(transcript_path: str | Path | None) -> bool:
    """Program mode from either source: this session's declaration, or a live PM mode.

    Both suspend the same two checks -- the unreported-work threshold and the unanswered handover.
    Neither touches the report FORMAT rules (an artifact link is still mandatory in a handover),
    and neither has any ability to reach the destructive/push/safety protections.
    """
    return program_mode_declared(transcript_path) or pm_mode_active()
