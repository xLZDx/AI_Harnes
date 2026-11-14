"""Global lessons store shared by every project and both clients (S20-T1, S20-T2).

Store folder: HARNESS_LESSONS_DIR, else _agentio.state_dir("lessons") (outside every git repository).
  lessons.jsonl      the ONLY source of truth, one JSON object per line, append-only under a file lock.
  INDEX.md           derived: header with the jsonl size and sha256, then one line per lesson
                     `L0042 | actions | tags | rule | K=KIND n=<seen> d=<date>` (a '|' inside a rule becomes '/').
  GLOBAL_LESSONS.md  derived: human-readable, grouped by action, newest first.
  outbox/            entries that could not get the lock within 10 s; the next writer drains them.
  errors.jsonl       one line per swallowed error (every public function fails open).
A dated copy of lessons.jsonl goes to HARNESS_LESSONS_BACKUP (default D:\\Backup\\lessons) on every rebuild; last 20 kept.

Privacy: no free-text facts are stored, only rule, tags, actions, kind, date, project and source. Input that matches the
secret_policy patterns is refused. Stdlib only, no subprocess.

Public API (never raises): add, add_many, rebuild, search, show, restore, parse_log, derive_tags, derive_actions.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    import _agentio
except ImportError:  # imported by the CLI before the hooks folder is on sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _agentio

try:
    import secret_policy as _sp
except ImportError:  # pragma: no cover - the module sits next to this file in every install
    _sp = None

KINDS = ("FAILURE", "SHORTCOMING", "SUCCESS")
SEARCH_KINDS = ("FAILURE", "SHORTCOMING")
LOCK_WAIT_S = 10.0
LOCK_STALE_S = 120.0
BACKUP_KEEP = 20
MAX_RULE = 400
EXCLUDED_PROJECT_RE = re.compile(r"^(?:Virtual_marketing_company|VMC_.*)$", re.I)

# ------------------------------------------------------------------ paths and errors


def store_dir() -> Path:
    override = os.environ.get("HARNESS_LESSONS_DIR")
    if override:
        d = Path(override)
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return d
    return _agentio.state_dir("lessons")


def backup_dir() -> Path:
    override = os.environ.get("HARNESS_LESSONS_BACKUP")
    if override:
        return Path(override)
    if os.name == "nt":
        return Path("D:\\Backup\\lessons")
    return Path.home() / ".harness-hooks" / "lessons-backup"


def _log_error(where: str, exc) -> None:
    """One line to errors.jsonl; never raises."""
    try:
        rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "where": where, "error": f"{type(exc).__name__}: {exc}"[:300]}
        with open(store_dir() / "errors.jsonl", "ab") as fh:
            fh.write((json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
    except Exception:  # noqa: BLE001 - the error log itself must never break a caller
        pass


# ------------------------------------------------------------------ text helpers

def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (text or "").lower())).strip()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def rule_key(rule: str) -> str:
    return _sha(normalise(rule))


def entry_idem(project: str, sha: str, entry_text: str) -> str:
    return _sha(f"{project}\n{sha}\n{entry_text}")


def has_secret(*texts: str) -> bool:
    """True when any text matches the secret_guard value patterns. If the policy module is missing a minimal
    built-in set is used, so a broken install still refuses obvious keys."""
    blob = "\n".join(t for t in texts if t)
    if not blob:
        return False
    if _sp is not None:
        try:
            if _sp.find_secrets(blob):
                return True
        except Exception as exc:  # noqa: BLE001
            _log_error("has_secret", exc)
    return bool(re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bAKIA[0-9A-Z]{16}\b|\bsk-[0-9A-Za-z_-]{20,}\b|"
                          r"\bgh[pousr]_[0-9A-Za-z]{30,}\b|\bgithub_pat_[0-9A-Za-z_]{40,}\b|\bxox[abprs]-[0-9A-Za-z-]{10,}\b", blob))


# ------------------------------------------------------------------ tags and actions

TAG_RES = (
    ("commit", r"\bcommit"), ("push", r"\bpush"), ("pr", r"\bpr\b|pull request"), ("gpt-pm", r"gpt-?pm|pm bridge"),
    ("agent", r"\bagents?\b|subagent|reviewer|tester"), ("test", r"\btests?\b|tester|pytest|fixture|assert"),
    ("review", r"review"), ("guard", r"\bguards?\b|\bgates?\b"), ("hook", r"\bhooks?\b"),
    ("ci", r"\bci\b|github actions"), ("workflow", r"workflow"), ("ledger", r"ledger"), ("install", r"install"),
    ("delete", r"\bdelet|\brm\b|destroy|\bremov"), ("shell", r"\bshell\b|heredoc|\bbash\b|powershell|\bsed\b|\bawk\b"),
    ("git", r"\bgit\b|\bbranch|worktree|\bmerge|rebase"), ("windows", r"windows|ntfs|backslash|winerror|\bwsl\b"),
    ("encoding", r"encoding|utf-?\d|unicode|crlf|\bbom\b|control character"),
    ("secret", r"secret|\btokens?\b|credential|password|api key"), ("report", r"\breports?\b"),
    ("concurrency", r"concurren|\brace\b|\block\b|parallel"), ("regex", r"regex|pattern"),
    ("parser", r"parser|parsing"), ("fail-open", r"fail[- ]?open|fails? open|fail[- ]?closed"),
)
_TAG_RES = tuple((t, re.compile(p, re.I)) for t, p in TAG_RES)
ACTION_OF_TAG = (
    ("commit", ("commit", "ledger", "report", "git")), ("push", ("push", "ci", "workflow")), ("pr", ("pr",)),
    ("gpt-pm", ("gpt-pm",)), ("agent", ("agent", "review")), ("test", ("test",)),
    ("edit-guard", ("guard", "workflow", "fail-open")), ("edit-hook", ("hook",)), ("install", ("install",)),
    ("delete", ("delete",)), ("shell", ("shell", "windows", "regex")),
)


def derive_tags(*texts: str) -> list[str]:
    blob = " ".join(t for t in texts if t)
    return [t for t, rx in _TAG_RES if rx.search(blob)]


def derive_actions(tags) -> list[str]:
    ts = set(tags)
    out = [a for a, needs in ACTION_OF_TAG if ts.intersection(needs)]
    return out or ["general"]


# ------------------------------------------------------------------ parser of core/LESSONS_LOG.md

_KIND_ALT = r"(FAILURE|SHORTCOMING|SUCCESS|NEUTRAL)"
HEADER_RE = re.compile(r"^##\s+(\d{4}-\d\d-\d\d)\b\s*(?:--|-|\u2014|:)?\s*(.*?)\s*$")
HEADER_KIND_RE = re.compile(r"\(" + _KIND_ALT + r"\)\s*$")
START_KIND_RE = re.compile(r"^\s*(?:[-*]\s+)?(?:\*\*)?Kind(?:\*\*)?\s*:\s*(?:\*\*)?" + _KIND_ALT + r"\b(.*)$")
START_ONE_RE = re.compile(r"^\s*[-*]\s+" + _KIND_ALT + r"\b([^:\n]{0,100}):\s*(.*)$")
START_DATED_RE = re.compile(r"^\s*[-*]\s+(\d{4}-\d\d-\d\d)\s+([^:\n]{0,160}):\s*" + _KIND_ALT + r"\b(.*)$")
RULE_RE = re.compile(r"(?<![\w-])(?:Rule next time|Rule kept|Rule applied|Rule|Next time)\s*:\s*")
LABEL_RE = re.compile(r"(?<![\w-])(?:Kind|Facts|Root cause|Caught by|Missed by|Layers?|Layer that (?:missed it|caught it)|"
                      r"Why it worked|Evidence|Fix)\s*:")
DATE_RE = re.compile(r"\d{4}-\d\d-\d\d")


def _finish(cur: dict, out: list) -> None:
    text = " ".join(x for x in (s.strip() for s in cur["lines"]) if x)
    text = re.sub(r"^[-*]\s+", "", text)
    cur["text"] = re.sub(r"\s+(?:[-*]\s+)(?=(?:Why it worked|Rule|Root cause|Caught by|Facts|Layer)\b)", " ", text)
    out.append(cur)


def _rule_of(text: str) -> str:
    m = RULE_RE.search(text)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = LABEL_RE.search(rest)
    if nxt:
        rest = rest[:nxt.start()]
    rest = re.sub(r"\s+", " ", rest).strip().strip("*").strip()
    return rest[:MAX_RULE].rstrip()


def parse_log(text: str) -> dict:
    """Parse a core/LESSONS_LOG.md. Returns {"entries": [{date, kind, title, rule, text}], "no_rule": n, "neutral": n,
    "total": n}. One entry per Kind marker; an entry with no rule is counted in no_rule, a NEUTRAL one in neutral.
    Recognised shapes: `## <date> <title>` blocks with bullet or plain `Kind:` lines, a `(KIND)` suffix in the header,
    one-line `- KIND <date> <scope>: text`, and `- <date> <scope>: KIND - text`."""
    blocks: list = []
    cur_block = None
    for raw in (text or "").splitlines():
        line = raw.rstrip("\r")
        hm = HEADER_RE.match(line)
        if hm:
            title = hm.group(2)
            cur_block = {"date": hm.group(1), "title": title, "lines": []}
            blocks.append(cur_block)
        elif line.startswith("## "):
            cur_block = None  # a header without a date: its lines belong to no entry
        elif cur_block is not None:
            cur_block["lines"].append(line)
    entries: list = []
    for b in blocks:
        found: list = []
        cur = None
        for line in b["lines"]:
            m = START_KIND_RE.match(line)
            if m:
                cur = {"kind": m.group(1), "date": b["date"], "title": b["title"], "lines": [m.group(2)]}
            else:
                m = START_DATED_RE.match(line)
                if m:
                    cur = {"kind": m.group(3), "date": m.group(1), "title": m.group(2) or b["title"], "lines": [m.group(4)]}
                else:
                    m = START_ONE_RE.match(line)
                    if m:
                        dm = DATE_RE.search(m.group(2))
                        cur = {"kind": m.group(1), "date": dm.group(0) if dm else b["date"],
                               "title": (m.group(2).strip() or b["title"]), "lines": [m.group(3)]}
                    else:
                        if cur is not None:
                            cur["lines"].append(line)
                        continue
            found.append(cur)
        if not found:
            hk = HEADER_KIND_RE.search(b["title"])
            if hk and any(x.strip() for x in b["lines"]):
                found.append({"kind": hk.group(1), "date": b["date"], "title": b["title"], "lines": list(b["lines"])})
        for c in found:
            _finish(c, entries)
    out, no_rule, neutral = [], 0, 0
    for e in entries:
        if e["kind"] == "NEUTRAL":
            neutral += 1
            continue
        rule = _rule_of(e["text"])
        if not rule:
            no_rule += 1
            continue
        out.append({"date": e["date"], "kind": e["kind"], "title": e["title"], "rule": rule, "text": e["text"]})
    return {"entries": out, "no_rule": no_rule, "neutral": neutral, "total": len(entries)}


# ------------------------------------------------------------------ jsonl access

def _jsonl() -> Path:
    return store_dir() / "lessons.jsonl"


def _read_records(path: Path):
    """[(raw_line_bytes, obj_or_None)]; a line that is not a JSON object has obj None."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return [], True
    recs = []
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw.decode("utf-8"))
            recs.append((raw, obj if isinstance(obj, dict) and "id" in obj and "rule" in obj else None))
        except (ValueError, UnicodeDecodeError):
            recs.append((raw, None))
    return recs, data.endswith(b"\n") or not data


def load_lessons() -> list[dict]:
    recs, _ = _read_records(_jsonl())
    return [o for _, o in recs if o is not None]


def _line(obj: dict) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _atomic_bytes(path: Path, lines: list) -> None:
    text = "".join(ln.decode("utf-8", "replace") + "\n" for ln in lines)
    _agentio.atomic_write_text(path, text)


def _next_id(objs) -> str:
    top = 0
    for o in objs:
        m = re.fullmatch(r"L(\d+)", str(o.get("id", "")))
        if m:
            top = max(top, int(m.group(1)))
    return f"L{top + 1:04d}"


# ------------------------------------------------------------------ add

def _prepare(item: dict):
    """Validate and normalise one add() request; returns (entry, status). status is None when usable."""
    rule = re.sub(r"\s+", " ", str(item.get("rule") or "")).strip()[:MAX_RULE]
    kind = str(item.get("kind") or "").upper()
    project = str(item.get("project") or "").strip()
    if not rule or kind not in KINDS or not project:
        return None, "invalid"
    if EXCLUDED_PROJECT_RE.match(project):
        return None, "excluded"
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    source = {"repo": str(source.get("repo") or ""), "sha": str(source.get("sha") or "")}
    tags = [str(t).lower() for t in (item.get("tags") or [])]
    actions = [str(a).lower() for a in (item.get("actions") or [])]
    entry_text = str(item.get("entry_text") or rule)
    if has_secret(rule, entry_text, " ".join(tags), " ".join(actions), project, source["repo"]):
        return None, "secret"
    date = str(item.get("date") or "")
    if not re.fullmatch(r"\d{4}-\d\d-\d\d", date):
        date = time.strftime("%Y-%m-%d")
    entry = {"date": date, "project": project, "kind": kind, "tags": tags, "actions": actions or ["general"],
             "rule": rule, "source": source, "key": rule_key(rule),
             "idem": entry_idem(project, source["sha"], entry_text), "seen_in": [project]}
    return entry, None


def _queue(entry: dict) -> None:
    box = store_dir() / "outbox"
    box.mkdir(parents=True, exist_ok=True)
    _agentio.atomic_write_text(box / (entry["idem"] + ".json"), json.dumps(entry, ensure_ascii=False))


def _apply(entries: list, results: list) -> bool:
    """Apply prepared entries to lessons.jsonl; the caller holds the lock. Appends `{status,id}` to results.
    Returns True when the file changed."""
    path = _jsonl()
    recs, ends_nl = _read_records(path)
    rewrite = False
    if recs and recs[-1][1] is None:  # truncated or corrupt last line: drop it
        recs.pop()
        rewrite = True
    elif not ends_nl:
        rewrite = True
    objs = [o for _, o in recs if o is not None]
    by_idem = {o.get("idem"): o for o in objs}
    by_key = {o.get("key"): o for o in objs}
    added: list = []
    for e in entries:
        if e["idem"] in by_idem:
            results.append({"status": "duplicate", "id": by_idem[e["idem"]].get("id")})
            continue
        old = by_key.get(e["key"])
        if old is not None:
            seen = old.setdefault("seen_in", [old.get("project")])
            by_idem[e["idem"]] = old
            if e["project"] in seen:
                results.append({"status": "duplicate", "id": old.get("id")})
            else:
                seen.append(e["project"])
                rewrite = True
                results.append({"status": "merged", "id": old.get("id")})
            continue
        rec = dict(e)
        rec["id"] = _next_id(objs)
        ordered = {"id": rec["id"], **{k: v for k, v in rec.items() if k != "id"}}
        objs.append(ordered)
        by_idem[e["idem"]] = ordered
        by_key[e["key"]] = ordered
        added.append(ordered)
        results.append({"status": "added", "id": ordered["id"]})
    new_lines = [_line(o) for o in added]
    if rewrite:
        lines = []
        for raw, o in recs:
            lines.append(_line(by_key.get(o.get("key"), o)) if o is not None else raw)
        _atomic_bytes(path, lines + new_lines)
        return True
    if new_lines:
        with open(path, "ab") as fh:
            fh.write(b"".join(ln + b"\n" for ln in new_lines))
        return True
    return False


def _drain_outbox(results: list) -> None:
    box = store_dir() / "outbox"
    try:
        files = sorted(box.glob("*.json"))
    except OSError:
        return
    pending, used = [], []
    for f in files:
        try:
            e = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(e, dict) and e.get("rule") and e.get("idem") and e.get("key"):
                pending.append(e)
            used.append(f)
        except (OSError, ValueError) as exc:
            _log_error("outbox", exc)
    if pending:
        _apply(pending, results)
    for f in used:
        try:
            f.unlink()
        except OSError:
            pass


def add_many(items: list) -> list:
    """Add several lessons under one lock and rebuild once. Each item is a dict with rule, kind, project and optional
    date, tags, actions, source {repo, sha}, entry_text. Returns one {status, id} per item (status: added, merged,
    duplicate, queued, secret, excluded, invalid) or [] on an internal error."""
    try:
        prepared, results = [], []
        for it in items:
            e, bad = _prepare(it)
            if bad:
                results.append({"status": bad, "id": None})
            else:
                prepared.append(e)
        if not prepared:
            return results
        sd = store_dir()
        with _agentio.file_lock(sd / "lessons.jsonl", wait=LOCK_WAIT_S, stale=LOCK_STALE_S) as got:
            if not got:
                for e in prepared:
                    _queue(e)
                    results.append({"status": "queued", "id": None})
                return results
            _apply(prepared, results)
            _drain_outbox([])
            _rebuild_locked()
        return results
    except Exception as exc:  # noqa: BLE001 - fail open
        _log_error("add_many", exc)
        return []


def add(rule: str, kind: str, project: str, **kw):
    """Add one lesson; returns {status, id} or None on an internal error."""
    res = add_many([dict(kw, rule=rule, kind=kind, project=project)])
    return res[0] if res else None


# ------------------------------------------------------------------ rebuild (index, global view, backup)

def _file_state(path: Path):
    try:
        data = path.read_bytes()
    except OSError:
        return 0, _sha("")
    return len(data), hashlib.sha256(data).hexdigest()


def _index_line(o: dict) -> str:
    clean = lambda s: str(s).replace("|", "/").replace("\n", " ")  # noqa: E731
    return (f"{o['id']} | {','.join(o.get('actions') or [])} | {','.join(o.get('tags') or [])} | {clean(o['rule'])} | "
            f"K={o.get('kind')} n={len(o.get('seen_in') or [])} d={o.get('date')}")


def _rebuild_locked() -> None:
    sd = store_dir()
    path = _jsonl()
    objs = load_lessons()
    size, digest = _file_state(path)
    head = f"<!-- lessons.jsonl size={size} sha256={digest} count={len(objs)} -->"
    lines = [head, "# Lessons index (generated from lessons.jsonl; do not edit)"]
    lines += [_index_line(o) for o in objs]
    _agentio.atomic_write_text(sd / "INDEX.md", "\n".join(lines) + "\n")
    # global view grouped by action, newest first
    groups: dict = {}
    for o in objs:
        for a in (o.get("actions") or ["general"]):
            groups.setdefault(a, []).append(o)
    out = [head, "# Global lessons", "", "Глобальные уроки всех проектов (файл создаётся автоматически, не править).", ""]
    for a in sorted(groups):
        out.append(f"## {a}")
        for o in sorted(groups[a], key=lambda x: (str(x.get("date")), str(x.get("id"))), reverse=True):
            out.append(f"- {o['id']} [{o.get('kind')}] {o.get('date')} {o.get('project')} (seen in {len(o.get('seen_in') or [])}): "
                       f"{o['rule']}")
        out.append("")
    _agentio.atomic_write_text(sd / "GLOBAL_LESSONS.md", "\n".join(out))
    _backup(path, digest)


def _backup(path: Path, digest: str) -> None:
    try:
        if not path.exists():
            return
        bd = backup_dir()
        bd.mkdir(parents=True, exist_ok=True)
        files = sorted(bd.glob("lessons-*.jsonl"))
        if files and _file_state(files[-1])[1] == digest:
            return
        name = bd / f"lessons-{time.strftime('%Y%m%d-%H%M%S')}-{digest[:8]}.jsonl"
        _agentio.atomic_write_text(name, path.read_bytes().decode("utf-8", "replace"))
        files = sorted(bd.glob("lessons-*.jsonl"))
        for old in files[:-BACKUP_KEEP]:
            try:
                old.unlink()
            except OSError:
                pass
    except Exception as exc:  # noqa: BLE001 - a missing backup drive must never block the store
        _log_error("backup", exc)


def rebuild() -> bool:
    """Rebuild INDEX.md and GLOBAL_LESSONS.md from lessons.jsonl, drain the outbox, take a backup. True on success."""
    try:
        with _agentio.file_lock(_jsonl(), wait=LOCK_WAIT_S, stale=LOCK_STALE_S) as got:
            if not got:
                _log_error("rebuild", RuntimeError("lock timeout"))
                return False
            _drain_outbox([])
            _rebuild_locked()
        return True
    except Exception as exc:  # noqa: BLE001
        _log_error("rebuild", exc)
        return False


# ------------------------------------------------------------------ search and show

_HEAD_RE = re.compile(r"<!-- lessons\.jsonl size=(\d+) sha256=([0-9a-f]{64})")


def _index_current() -> bool:
    try:
        head = (store_dir() / "INDEX.md").read_text(encoding="utf-8").split("\n", 1)[0]
        m = _HEAD_RE.match(head)
        if not m:
            return False
        size, digest = _file_state(_jsonl())
        return int(m.group(1)) == size and m.group(2) == digest
    except OSError:
        return False


def search(action: str, words="", limit: int = 5) -> list:
    """FAILURE and SHORTCOMING lessons for an action, best first. Reads INDEX.md (rebuilt first when its header does
    not match lessons.jsonl). Rank: action match, tag overlap with words, number of projects that saw it, recency.
    Returns [{id, rule, actions, tags, kind, seen, date}] or [] on any error."""
    try:
        if not _index_current():
            if not rebuild() and not (store_dir() / "INDEX.md").exists():
                return []
        action = (action or "").strip().lower()
        toks = [w for w in re.split(r"[^a-z0-9-]+", (words if isinstance(words, str) else " ".join(words)).lower()) if w]
        scored = []
        for ln in (store_dir() / "INDEX.md").read_text(encoding="utf-8").splitlines():
            parts = [p.strip() for p in ln.split(" | ")]
            if len(parts) != 5 or not re.fullmatch(r"L\d+", parts[0]):
                continue
            meta = dict(kv.split("=", 1) for kv in parts[4].split() if "=" in kv)
            if meta.get("K") not in SEARCH_KINDS:
                continue
            acts = [a for a in parts[1].split(",") if a]
            tags = [t for t in parts[2].split(",") if t]
            score = 0.0
            if action and action in acts:
                score += 100
            elif "general" in acts and action:
                score += 10
            overlap = sum(1 for t in tags if any(w == t or w.startswith(t) for w in toks))
            score += 10 * overlap
            if score <= 0:
                continue
            seen = int(meta.get("n", "1") or 1)
            score += 3 * min(seen, 10)
            date = meta.get("d", "")
            score += int(re.sub(r"\D", "", date) or 0) / 1e9
            scored.append((score, parts[0], {"id": parts[0], "rule": parts[3], "actions": acts, "tags": tags,
                                             "kind": meta.get("K"), "seen": seen, "date": date}))
        scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
        return [s[2] for s in scored[:max(0, int(limit))]]
    except Exception as exc:  # noqa: BLE001
        _log_error("search", exc)
        return []


def show(ids) -> list:
    """Full records for the given ids (e.g. ["L0001"]); [] on error."""
    try:
        want = {str(i).upper() for i in ids}
        return [o for o in load_lessons() if str(o.get("id")).upper() in want]
    except Exception as exc:  # noqa: BLE001
        _log_error("show", exc)
        return []


def restore():
    """Replace lessons.jsonl with the newest backup and rebuild. Returns the backup path used, or None."""
    try:
        files = sorted(backup_dir().glob("lessons-*.jsonl"))
        if not files:
            return None
        data = files[-1].read_bytes().decode("utf-8", "replace")
        with _agentio.file_lock(_jsonl(), wait=LOCK_WAIT_S, stale=LOCK_STALE_S) as got:
            if not got:
                return None
            _agentio.atomic_write_text(_jsonl(), data)
            _rebuild_locked()
        return str(files[-1])
    except Exception as exc:  # noqa: BLE001
        _log_error("restore", exc)
        return None
