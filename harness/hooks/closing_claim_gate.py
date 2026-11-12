#!/usr/bin/env python3
"""Stop hook: the closing line "Выполнимых задач не осталось" / "No doable tasks left" is a claim; check that it is true.

When the last assistant message contains the claim, collect contradictions:
  1. open-item phrases in the same message ("не трогаю", "NOT_RUN", "ещё не", "remaining", ...), one entry per line;
  2. open tasks in .harness/ledger.json of the repos the session worked in (state not verified/done, not blocked);
  3. uncommitted tracked changes (git status --porcelain -uno, .harness/ and reports/*_LIVE* ignored) in those repos.
Exceptions: a line that names an operator-only case (real prohibition, money, password, CAPTCHA, account, unrecoverable
deletion, force-push, production data, platform safety approval such as Codex hook trust); open ledger tasks that own
running background agents work on, only when the message names the task id; a message with "Блокеры:"/"Blockers:" and no claim.
Blocks with a Russian + English reason. Loop guard: the same signature blocks at most 2 times in a row.
Kill switch HARNESS_CLOSING_CLAIM_GATE=off. Fails open with a logged traceback; skips subagents.
One file for Claude Code and Codex (same Stop stdin, same {"decision": "block"} output).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _agentio  # noqa: E402  shared Claude Code / Codex adapter

CLAIM_RE = re.compile(r"Выполнимых\s+задач\s+не\s+осталось|No\s+doable\s+tasks\s+left", re.IGNORECASE)
OPEN_RE = re.compile(
    r"не\s+зелён|не\s+зелен|не\s+готов|не\s+сделан|не\s+проверен|ещё\s+не|еще\s+не|осталось|остаётся|остается|не\s+трогаю"
    r"|только\s+в\s+новой\s+сессии|следующая\s+сессия|нужно\s+(?:одно\s+)?действие\s+от\s+вас"
    r"|NOT_RUN|not\s+done|not\s+verified|\bremaining\b|left\s+open|next\s+session|\bTODO\b|backlog\s+item",
    re.IGNORECASE,
)
OPERATOR_ONLY_RE = re.compile(
    r"real\s+prohibition|prohibited\s+by|запрещ|реальн\w*\s+(?:деньг|запрет)|real\s+money|live\s+trading|деньги|"
    r"password|парол|personal\s+secret|секрет\s+оператора|captcha|капч|creating\s+an?\s+account|создани\w+\s+аккаунт|"
    r"unrecoverable|необратим|force[- ]push|production\s+data|боев\w+\s+данн|прод\w*\s+данн|"
    r"hook\s+trust|trust\s+(?:the\s+)?hook|доверие\s+к\s+хук|доверить\s+хук|platform\s+(?:safety\s+)?approval|"
    r"подтверждени\w+\s+платформ|safety\s+approval",
    re.IGNORECASE,
)
BLOCKERS_RE = re.compile(r"(?:Блокеры|Blockers)\s*:", re.IGNORECASE)
DONE_STATES = {"verified", "done", "blocked"}
MAX_SAME_BLOCKS = 2
MAX_LINE = 160
IGNORED_PATH_RE = re.compile(r"(?:^|/)\.harness/|(?:^|/)reports/[^/]*_LIVE", re.IGNORECASE)


def state_path() -> Path:
    return _agentio.state_dir("closing_claim_gate") / "closing_claim_decisions.jsonl"


def log(rec: dict) -> None:
    try:
        with state_path().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **rec}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _loop(data: dict, sig: str, reset: bool = False) -> int:
    """Consecutive blocks of the same signature in this session; 0 after a reset. Fails to 1 on error."""
    path = _agentio.state_dir("closing_claim_gate") / "closing_claim_loop.json"
    key = str(data.get("session_id") or "default")
    try:
        try:
            st = json.loads(path.read_text(encoding="utf-8"))
            st = st if isinstance(st, dict) else {}
        except (OSError, ValueError):
            st = {}
        if reset:
            st.pop(key, None)
            n = 0
        else:
            rec = st.get(key) or {}
            n = int(rec.get("n", 0)) + 1 if rec.get("sig") == sig else 1
            st[key] = {"sig": sig, "n": n}
        st = dict(list(st.items())[-40:])
        _agentio.atomic_write_text(path, json.dumps(st))
        return n
    except (OSError, ValueError, TypeError):
        return 1


def open_phrase_lines(message: str) -> list[str]:
    out = []
    for raw in message.splitlines():
        line = CLAIM_RE.sub("", raw).strip()
        if not line or not OPEN_RE.search(line):
            continue
        if OPERATOR_ONLY_RE.search(line):
            continue  # names an operator-only case: allowed
        text = line if len(line) <= MAX_LINE else line[: MAX_LINE - 3] + "..."
        out.append(text)
    return out


def worked_dirs(data: dict) -> list[str]:
    """Directories of the session's own work: start cwd, payload cwd, Edit/Write file dirs, `git commit` dirs."""
    import session_close_gate as scg
    dirs: list[str] = []
    tp = data.get("transcript_path") or ""
    start = scg._start_cwd(tp) if tp else ""
    for r in (_agentio.records(tp) if tp else []):
        if r.get("kind") != "tool_use":
            continue
        inp = r.get("input") if isinstance(r.get("input"), dict) else {}
        if r.get("name") in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
            for key in ("file_path", "notebook_path"):
                if inp.get(key) and Path(str(inp[key])).is_absolute():
                    dirs.append(str(Path(str(inp[key])).parent))
        cmd = str(inp.get("command") or inp.get("cmd") or "")
        if "commit" in cmd:
            for rx in scg.COMMIT_DIR_RES:
                for m in rx.finditer(cmd):
                    d = scg._msys_path(m.group(1))
                    if Path(d).is_absolute():
                        dirs.append(d)
    for d in (start, str(data.get("cwd") or "")):
        if d:
            dirs.append(d)
    return dirs


def git_root(d: str) -> Path | None:
    try:
        if not Path(d).is_dir():
            return None
        r = _agentio.run_git(["-C", d, "rev-parse", "--show-toplevel"], timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip()).resolve()
    except (OSError, ValueError):
        pass
    return None


def ledger_open(root: Path) -> list[tuple[str, str]] | None:
    """Tasks in non-closed sprints whose state is not verified/done/blocked; None = unreadable."""
    try:
        led = json.loads((root / ".harness" / "ledger.json").read_text(encoding="utf-8"))
        out = []
        for s in led["sprints"]:
            if s.get("state") == "closed":
                continue
            for t in s.get("tasks", []):
                st = str(t.get("state") or "").lower()
                if st in DONE_STATES or t.get("blocked"):
                    continue
                out.append((str(t["id"]), st or "?"))
        return out
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def dirty_files(root: Path) -> list[str]:
    r = _agentio.run_git(["-C", str(root), "status", "--porcelain", "-uno"], timeout=10)
    if r.returncode != 0:
        return []
    out = []
    for ln in r.stdout.splitlines():
        path = ln[3:].strip().strip('"').replace("\\", "/")
        if path and not IGNORED_PATH_RE.search(path):
            out.append(path)
    return out


def collect(data: dict, message: str) -> list[str]:
    import session_close_gate as scg
    items = [f"open item in message: «{ln}»" for ln in open_phrase_lines(message)]
    running = scg.pending_agents(data.get("transcript_path") or "")
    roots: dict[str, Path] = {}
    for d in worked_dirs(data):
        g = git_root(d)
        lr = scg._ledger_root_of(d) if Path(d).is_absolute() else None
        for r in (g, lr):
            if r is not None:
                roots[os.path.normcase(str(r))] = r
    for r in roots.values():
        if (r / ".harness" / "ledger.json").is_file():
            tasks = ledger_open(r)
            if tasks is None:
                log({"verdict": "ledger-unreadable", "root": str(r)})
            else:
                for tid, st in tasks:
                    if running and tid in message:
                        continue  # own running agent works on it and the message names the task id
                    items.append(f"open ledger task {tid} ({st}) in {r.name}")
        dirty = dirty_files(r)
        if dirty:
            items.append(f"uncommitted tracked changes in {r.name}: " + ", ".join(dirty[:5]) + (f" (+{len(dirty) - 5})" if len(dirty) > 5 else ""))
    return items


def build_reason(items: list[str]) -> str:
    lst = "; ".join(items)
    return (
        f'Ты написал "Выполнимых задач не осталось", но: {lst}. Для каждого пункта: сделай его сейчас, или закрой через '
        f'ledger.py block с проверенной причиной из списка operator-only, или замени концовку на "Блокеры: <класс> - <причина>". '
        f"Не повторяй утверждение без проверки.\n"
        f'You wrote "No doable tasks left", but: {lst}. For each item: do it now, or close it with ledger.py block using a verified '
        f'operator-only cause, or replace the closing line with "Blockers: <class> - <cause>". Do not repeat the claim without checking.'
    )


def main() -> int:
    if os.environ.get("HARNESS_CLOSING_CLAIM_GATE", "").lower() in {"off", "0", "false"}:
        return 0
    data = json.loads(sys.stdin.read() or "{}")
    if data.get("agent_type"):
        return 0
    message = data.get("last_assistant_message") or ""
    if not message and _agentio.client(data) == "codex":
        message = _agentio.last_assistant_text(data)
    if not message or not CLAIM_RE.search(message):
        return 0
    items = collect(data, message)
    if not items:
        _loop(data, "", reset=True)
        log({"verdict": "pass"})
        return 0
    sig = "|".join(sorted(items))
    if _loop(data, sig) > MAX_SAME_BLOCKS:
        log({"verdict": "loop-release", "items": items, "cap": MAX_SAME_BLOCKS})
        return 0
    log({"verdict": "block", "items": items})
    print(json.dumps({"decision": "block", "reason": build_reason(items)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        log({"verdict": "hook-error", "traceback": traceback.format_exc()[-1500:]})
        raise SystemExit(0)
