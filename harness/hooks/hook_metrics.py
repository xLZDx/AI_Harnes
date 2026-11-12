#!/usr/bin/env python3
"""Shared metrics helper (S7-T6/T7). Every hook may call `record(...)` LAST, inside its own try/except.

Contract (GPT-PM plan rounds 4-6):
  * Rows hold only: ts, client, session, hook, event, decision, reason (a code), ms, project (folder NAME), agent.
    Never a prompt, a command, a path with user text or a secret value.
  * Storage: ~/.harness-hooks/metrics (override HARNESS_METRICS_DIR), outside every git work tree, owner-only where the OS
    allows. A path inside a git work tree is refused (nothing is written).
  * Append, rotation and purge run under one cross-process lock with a short wait; on timeout the row is dropped and a
    drop counter file is incremented. Nothing here may delay or change a hook decision: record() never raises.
  * Retention: rows older than RETENTION_DAYS are removed from the active and the rotated files by purge(), which runs when
    the stamp file is older than an hour (checked on each write), from metrics.py and on demand. At most KEEP rotated files.
  * One row per line: a corrupt line never hides the others (readers skip it and count it).
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

RETENTION_DAYS = 30
ROTATE_BYTES = 5 * 1024 * 1024
KEEP = 5
PURGE_EVERY_S = 3600
FIELDS = ("ts", "client", "session", "hook", "event", "decision", "reason", "ms", "project", "agent")


def metrics_dir() -> Path:
    return Path(os.environ.get("HARNESS_METRICS_DIR") or (Path.home() / ".harness-hooks" / "metrics"))


def inside_git_work_tree(path: Path) -> bool:
    p = path.resolve()
    for d in [p, *p.parents]:
        if (d / ".git").exists():
            return True
    return False


def _lock(path: Path):
    try:
        from _agentio import file_lock
    except Exception:  # pragma: no cover
        return None
    return file_lock(path, wait=0.3, stale=20.0)


def _restrict(d: Path) -> None:
    """Owner-only access, once per folder. Best effort; failure is not an error."""
    mark = d / ".perm_done"
    if mark.exists():
        return
    try:
        mark.write_text("1", encoding="utf-8")  # first: a failing or slow icacls must not be retried on every hook call
    except Exception:
        pass
    try:
        if os.name == "nt":
            import subprocess
            user = os.environ.get("USERNAME") or ""
            if user:
                subprocess.run(["icacls", str(d), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F"],
                               capture_output=True, timeout=2)
        else:
            os.chmod(d, 0o700)
    except Exception:
        _bump(d, "errors")


def _bump(d: Path, name: str) -> None:
    """Separate counters: dropped = row lost on lock timeout, errors = rotation/purge/permission failure, refused = metrics folder
    inside a git work tree. metrics.py shows all three."""
    try:
        f = d / f"{name}.count"
        n = int(f.read_text(encoding="utf-8")) if f.exists() else 0
        f.write_text(str(n + 1), encoding="utf-8")
    except Exception:
        pass


def _bump_drop(d: Path) -> None:
    _bump(d, "dropped")


def counters() -> dict:
    d = metrics_dir()
    out = {}
    for name in ("dropped", "errors", "refused"):
        try:
            out[name] = int((d / f"{name}.count").read_text(encoding="utf-8"))
        except Exception:
            out[name] = 0
    return out


def _row_time(line: str):
    try:
        return datetime.fromisoformat(json.loads(line)["ts"])
    except Exception:
        return None


def _rewrite_without_old(path: Path, cutoff: datetime) -> None:
    keep = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            t = _row_time(line)
            if t is None or t >= cutoff:  # a corrupt line is kept (readers count it), age unknown
                keep.append(line if line.endswith("\n") else line + "\n")
    if not keep:
        path.unlink()
        return
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text("".join(keep), encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def purge(now: datetime | None = None, locked: bool = False) -> int:
    """Remove rows older than RETENTION_DAYS from every metrics file. Returns the number of files rewritten or removed."""
    d = metrics_dir()
    if not d.is_dir():
        return 0
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=RETENTION_DAYS)
    work = 0

    def run():
        nonlocal work
        for f in sorted(d.glob("events*.jsonl")):
            try:
                before = f.stat().st_size
                _rewrite_without_old(f, cutoff)
                if not f.exists() or f.stat().st_size != before:
                    work += 1
            except Exception:
                _bump(d, "errors")
        try:
            (d / "purge.stamp").write_text(str(time.time()), encoding="utf-8")
        except Exception:
            pass

    if locked:
        run()
        return work
    lk = _lock(d / "events.jsonl")
    if lk is None:
        return 0
    with lk as got:
        if got:
            run()
        else:
            _bump_drop(d)
    return work


def _purge_due(d: Path) -> bool:
    try:
        return time.time() - float((d / "purge.stamp").read_text(encoding="utf-8")) > PURGE_EVERY_S
    except Exception:
        return True


def _rotate(d: Path, active: Path) -> None:
    for i in range(KEEP, 0, -1):
        src = d / f"events.{i}.jsonl"
        if i == KEEP:
            if src.exists():
                src.unlink()
        elif src.exists():
            os.replace(src, d / f"events.{i + 1}.jsonl")
    os.replace(active, d / "events.1.jsonl")


def record(hook: str, event: str, decision: str, reason: str = "", ms: int = 0, session: str = "",
           client: str = "", project: str = "", agent: str = "") -> bool:
    """Append one row. Never raises, never blocks longer than the lock wait. Returns True when written."""
    try:
        d = metrics_dir()
        if inside_git_work_tree(d):
            if d.is_dir():  # count the refusal only where the folder already exists: nothing is created inside a work tree
                _bump(d, "refused")
            return False
        d.mkdir(parents=True, exist_ok=True)
        _restrict(d)
        row = {"ts": datetime.now(timezone.utc).isoformat(), "client": client, "session": str(session)[:64], "hook": hook,
               "event": event, "decision": decision, "reason": str(reason)[:60], "ms": int(ms),
               "project": os.path.basename(str(project).rstrip("\\/")) if project else "", "agent": agent}
        line = json.dumps({k: row[k] for k in FIELDS}, ensure_ascii=False) + "\n"
        active = d / "events.jsonl"
        lk = _lock(active)
        if lk is None:
            return False
        with lk as got:
            if not got:
                _bump_drop(d)
                return False
            if active.exists() and active.stat().st_size >= ROTATE_BYTES:
                try:
                    _rotate(d, active)
                except Exception:
                    _bump(d, "errors")  # rotation failed: keep appending to the active file, say so
            with open(active, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)
        if _purge_due(d):
            _spawn_purge(d)  # never on the hook path: a detached process does the rewrite
        return True
    except Exception:
        try:
            _bump(metrics_dir(), "errors")
        except Exception:
            pass
        return False


def _spawn_purge(d: Path) -> None:
    try:
        (d / "purge.stamp").write_text(str(time.time()), encoding="utf-8")  # claim the slot first: no start storm
        import subprocess
        flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([sys.executable, "-s", "-X", "utf8", str(Path(__file__).resolve()), "--purge"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags, close_fds=True)
    except Exception:
        _bump(d, "errors")


if __name__ == "__main__":
    if "--purge" in sys.argv:
        purge()


def read_rows(since: datetime | None = None):
    """Yield (rows, corrupt_count, dropped) from every metrics file; corrupt lines are skipped and counted."""
    d = metrics_dir()
    rows, bad = [], 0
    for f in sorted(d.glob("events*.jsonl")) if d.is_dir() else []:
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        r = json.loads(line)
                        t = datetime.fromisoformat(r["ts"])
                    except Exception:
                        bad += 1
                        continue
                    if since is None or t >= since:
                        rows.append(r)
        except OSError:
            bad += 1
    try:
        dropped = int((d / "dropped.count").read_text(encoding="utf-8"))
    except Exception:
        dropped = 0
    return rows, bad, dropped
