#!/usr/bin/env python3
"""Stop hook: project session supervisor -- nudge a session that stopped without a legitimate reason.

Scope: only sessions whose cwd is inside a git work tree. Optional narrowing by environment:
  SESSION_SUPERVISOR_ROOTS  os.pathsep-separated parent folders; when set, the repo must sit under one of them.
  SESSION_SUPERVISOR_SKIP   comma-separated repo folder names (or prefixes) that are never supervised.
  SESSION_SUPERVISOR_HINTS  path of a JSON file {"<repo folder name prefix>": "<source-of-truth hint>"};
                            default <state dir>/projects.json. Missing file = the generic hint.
Folders whose name contains "stale" and sessions outside any git repo are ignored (project isolation).
Each project (repo folder name) is supervised with its own state and hint.
Works unchanged in Claude Code and Codex (same Stop stdin and {"decision": "block"} output; the last
assistant message comes from the payload or from either transcript format via _agentio).

Decision per Stop event:
  * stop_hook_active set          -> allow (never loop on our own block).
  * last message asks the operator, waits for GO/credentials/device, hits a safe-stop condition from
    governance/AUTONOMY_BOUNDARY.md, or reports completion -> allow (logged with the reason).
  * otherwise (the session simply stopped) -> block with "continue" feedback, at most MAX_NUDGES
    times in a row without progress (git HEAD + porcelain status unchanged); then escalate: allow
    and log ESCALATE so the operator sees it.

Modes (env SESSION_SUPERVISOR_MODE): live (default), dry-run (only logs what it would do), off.
The supervisor never grants GO and never takes operator-only actions: it only asks the session to
continue its own authorized work. Fails open on any error. Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _agentio  # noqa: E402  shared Claude Code / Codex adapter

MAX_NUDGES = 3
SUPERVISOR_DIR = Path(os.environ.get("SESSION_SUPERVISOR_DIR") or _agentio.state_dir("supervisor"))

OTHER_HINT = "CLAUDE.md / AGENTS.md проекта и его план/DoD/спринт-документы"

# Reasons a stop is legitimate. RU + EN, matched on the tail of the last assistant message.
ASK_OPERATOR = re.compile(
    r"(\?\s*$|жду (ваш|ответ|go|го\b)|нужен (ваш|ваше|go|го\b)|нужно (ваше )?(разрешение|решение|подтвержд)|"
    r"напишите|скажите|подтвердите|выберите|awaiting|waiting for (the )?(operator|user|go)|"
    r"need your|please confirm|operator[- ]only|требуется (ваш|go)|"
    r"могу (сделать|проверить|выполнить|продолжить|запустить|начать)|если (хотите|скажете|нужно|откроете|разрешите)|"
    r"пришлите|дайте (знать|go|го)|го|GO|ваше решение|на ваш выбор|"
    r"let me know|would you like|if you (want|like|say)|i can (do|run|check|continue))",
    re.IGNORECASE | re.MULTILINE,
)
SAFE_STOP = re.compile(
    r"(safe[- ]stop|необратим|irreversible|production side effect|без (проверенного )?бэкап|without (a )?verified backup|"
    r"нет (внешних )?(credential|учётн|доступ|устройств)|missing (external )?(credential|device|provider)|"
    r"exhaust\w* .*budget|бюджет .*исчерпан|OA-0\d\d|нужен интерактивный|interactive (user )?input)",
    re.IGNORECASE,
)
COMPLETE = re.compile(
    r"(все (задачи|гейты|workstream\w*) (закрыт|выполнен)|задача (закрыта|выполнена)|work(stream)? (is )?(closed|complete)|"
    r"all (gates|tasks|workstreams) (are )?(closed|complete|done)|APPROVE=GO|\bNO-GO\b|CONDITIONAL GO|"
    # the closing line the operator's own keep-going Stop skill requires: a deliberate, classified stop
    r"выполнимых задач не осталось|блокеры:\s*(operator|external|prohibited|нет))",
    re.IGNORECASE,
)

# Waiting on an external event (CI, background job, provider). Not a stop the Stop hook can fix -- the
# event may finish while nobody is listening -- so it is allowed there and handled by the watchdog.
WAITING = re.compile(
    r"(жду результат|жд[уё]м|ожида(ю|ние|нием)|фонов\w+ (ожидан|задач|процесс)|вернёт управление|верн[её]т управление|"
    r"сообщу,? когда|сообщу о завершении|waiting for (ci|the (build|run|job|checks?))|will (notify|report back)|"
    r"background (wait|task|job) (is )?(running|started)|"
    r"(проверю|сверю|посмотрю|гляну) (лог|результат|статус|прогон|ci|тест)|через (несколько |пару )?минут|"
    r"когда (прогон|тесты|ci|сборка|джоба|job) (завершит|закончит|пройд)|"
    r"(прогон|тесты|ci|сборка)\W+(запущен|идёт|идет|крутится)|идёт (скачивание|установка|сборка|прогон)|запущен\w* (в фоне|заново)|"
    r"(tests?|ci|build|run) (is |are )?(running|in progress)|check (back|the log) (in|later))",
    re.IGNORECASE,
)

WAIT_NUDGE = (
    "Supervisor: ты ждал внешнего события (CI/фоновая задача/провайдер), но тишина затянулась. "
    "Не жди уведомления: проверь ФАКТИЧЕСКОЕ состояние первоисточника (gh run list / gh pr checks для точного коммита, "
    "лог, статус задачи), как в реальной проверке, а не по памяти или пустому логу. "
    "Если событие уже произошло - действуй по результату (мерж/следующий шаг по плану и разрешениям). "
    "Если ещё идёт - назови, что именно и когда проверил, и поставь реальный механизм ожидания (Monitor с until-циклом), "
    "а не просто остановку. Не имитируй GO для операторских действий."
)

NUDGE = (
    "Supervisor: ты остановился, но причина не похожа на допустимую остановку. "
    "Прочитай {hint} (источники правды проекта). "
    "Если остановка вызвана safe-stop причиной (нужен интерактивный ввод, необратимый production-эффект, "
    "деструктивная миграция без бэкапа, нет внешних credential/устройства/провайдера, исчерпан бюджет) или вопросом "
    "к оператору - назови её явно в ответе. Иначе продолжай свою текущую задачу до критериев приёмки "
    "(спринт/gate/DoD проекта), не останавливайся на чекпойнте. Не запрашивай и не имитируй GO для операторских действий."
)


def mode() -> str:
    value = (os.environ.get("SESSION_SUPERVISOR_MODE") or "live").strip().lower()
    return value if value in {"dry-run", "live", "off"} else "live"


def _matches(name: str, prefix: str) -> bool:
    return name == prefix or (name.startswith(prefix) and not name[len(prefix)].isalnum())


def _norm(path: str) -> str:
    return str(path or "").replace("\\", "/").rstrip("/").lower()


def _repo_top(cwd: str) -> str:
    """git work-tree root of cwd, '' when cwd is not inside one."""
    if not cwd:
        return ""
    try:
        out = _agentio.run_git(["-C", cwd, "rev-parse", "--show-toplevel"])
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def project_of(cwd: str) -> str | None:
    """Repo folder name (lower case) when the session is supervised, else None."""
    top = _repo_top(cwd)
    if not top:
        return None
    name = _norm(top).rsplit("/", 1)[-1]
    if not name or "stale" in name:
        return None
    skip = [s.strip().lower() for s in (os.environ.get("SESSION_SUPERVISOR_SKIP") or "").split(",") if s.strip()]
    if any(_matches(name, x) for x in skip):
        return None
    roots = [_norm(r) for r in (os.environ.get("SESSION_SUPERVISOR_ROOTS") or "").split(os.pathsep) if r.strip()]
    if roots and not any(_norm(top).startswith(r + "/") for r in roots):
        return None
    return name


def hint_for(project: str) -> str:
    path = Path(os.environ.get("SESSION_SUPERVISOR_HINTS") or (SUPERVISOR_DIR / "projects.json"))
    try:
        hints = json.loads(path.read_text(encoding="utf-8"))
        for prefix, hint in (hints.items() if isinstance(hints, dict) else ()):
            if _matches(project, str(prefix).lower()) and isinstance(hint, str) and hint.strip():
                return hint
    except (OSError, ValueError):
        pass
    return OTHER_HINT


def applicable(cwd: str) -> bool:
    return project_of(cwd) is not None


def last_assistant_text(transcript_path: str) -> str:
    """Text of the last assistant message that has text; '' when unreadable."""
    try:
        with Path(transcript_path).open("rb") as fh:  # tail only: transcripts reach hundreds of MB
            fh.seek(0, 2)
            start = max(0, fh.tell() - 1_500_000)
            fh.seek(start)
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
            if start:
                lines = lines[1:]  # first line is cut mid-record
    except OSError:
        return ""
    for raw in reversed(lines[-400:]):
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if entry.get("type") != "assistant":
            continue
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, str):
            if content.strip():
                return content
            continue
        texts = [b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
        joined = "\n".join(t for t in texts if t.strip())
        if joined.strip():
            return joined
    return ""


def classify(text: str) -> str:
    """ASK_OPERATOR | SAFE_STOP | COMPLETE | IDLE. Looks at the tail only: the end is the stop reason."""
    tail = text[-1200:] if text else ""
    if not tail.strip():
        return "IDLE"
    if ASK_OPERATOR.search(tail):
        return "ASK_OPERATOR"
    if SAFE_STOP.search(tail):
        return "SAFE_STOP"
    if WAITING.search(tail):
        return "WAITING"
    if COMPLETE.search(tail) or tail.rstrip().endswith("```"):
        return "COMPLETE"  # a closing code fence = a handoff/prompt block delivered as the result
    return "IDLE"


def git_progress_key(cwd: str) -> str:
    def run(*args: str) -> str:
        try:
            out = _agentio.run_git(["-C", cwd, *args])
            return out.stdout if out.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    blob = run("rev-parse", "HEAD") + "\n" + run("status", "--porcelain")
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:16]


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def log_event(event: dict) -> None:
    try:
        SUPERVISOR_DIR.mkdir(parents=True, exist_ok=True)
        event = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **event}
        with (SUPERVISOR_DIR / "supervisor.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass


def decide(payload: dict, run_mode: str, progress_key=git_progress_key) -> dict | None:
    """Return a hook response dict to block, or None to allow. Logs every applicable event."""
    cwd = payload.get("cwd") or ""
    session_id = payload.get("session_id") or "unknown"
    project = project_of(cwd)
    if run_mode == "off" or project is None:
        return None
    base = {"project": project, "session_id": session_id, "cwd": cwd, "mode": run_mode}
    if payload.get("stop_hook_active"):
        log_event({**base, "action": "allow", "reason": "stop_hook_active"})
        return None

    if _agentio.client(payload) == "codex":
        text = _agentio.last_assistant_text(payload)  # payload message first, Codex transcript as fallback
    else:
        text = last_assistant_text(payload.get("transcript_path") or "")
    category = classify(text)
    state_path = SUPERVISOR_DIR / "state" / f"{session_id}.json"
    state = _load_state(state_path)

    if category != "IDLE":
        _save_state(state_path, {"nudges": 0, "key": ""})
        log_event({**base, "action": "allow", "reason": category})
        return None

    key = progress_key(cwd)
    nudges = state.get("nudges", 0) + 1 if state.get("key") == key else 1
    if nudges > MAX_NUDGES:
        _save_state(state_path, {"nudges": nudges, "key": key})
        log_event({**base, "action": "ESCALATE", "reason": f"no progress after {MAX_NUDGES} nudges"})
        return None

    _save_state(state_path, {"nudges": nudges, "key": key})
    log_event({**base, "action": "would_block" if run_mode == "dry-run" else "block", "nudge": nudges})
    if run_mode == "dry-run":
        return None
    return {"decision": "block", "reason": NUDGE.format(hint=hint_for(project))}


def main() -> int:
    try:
        payload = _agentio.read_stdin()
        response = decide(payload, mode())
        if response:
            sys.stdout.write(json.dumps(response, ensure_ascii=False))
    except Exception as exc:  # fail open: a supervisor bug must never wedge a session
        log_event({"action": "error", "error": repr(exc)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
