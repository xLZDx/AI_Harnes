"""Global lessons store shared by every project and both clients (S20-T1, S20-T2).

Store folder: HARNESS_LESSONS_DIR, else _agentio.state_dir("lessons") (outside every git repository).
  lessons.jsonl      the ONLY source of truth, one JSON object per line, append-only under a file lock.
  INDEX.md           derived: header with the jsonl size, mtime and sha256, then one line per lesson
                     `L0042 | actions | tags | rule | K=KIND n=<seen> d=<date>` (a '|' inside a field becomes '/').
  GLOBAL_LESSONS.md  derived: human-readable, grouped by action, newest first.
  outbox/            entries that could not get the lock within 10 s; the next writer drains them.
                     A malformed outbox file moves to outbox/dead/ with a log line.
  corrupt/           bytes of every jsonl line that was dropped (never silent), one <timestamp>.txt per event.
  errors.jsonl       one line per swallowed error or notable event (every public function fails open).

Backups: a raw byte copy of lessons.jsonl, taken only after a write (add, add_many, restore, CLI rebuild), never on the
search path. Folder: HARNESS_LESSONS_BACKUP; else <store>/backup whenever the store is not the default
~/.harness-hooks/lessons (HARNESS_LESSONS_DIR or HARNESS_HOOK_STATE moved it); else D:\\Backup\\lessons
(a Windows default outside git). Names `lessons-<YYYYmmdd-HHMMSS>-<counter>.jsonl` sort by time. The last 20 are kept plus the
newest one of each day for 30 days. A backup is skipped (and logged) when the store has an unparsable line or fewer valid
records than the newest backup.

Reading: the search path (the recall hook) never takes the lock and never rebuilds; it serves the existing INDEX.md even if it
is stale. Only writers and the CLI rebuild. Any lock wait on a read path is at most READ_LOCK_WAIT_S.

Privacy: no free-text facts are stored, only rule, tags, actions, kind, date, project and source. Input that matches the
secret_policy value patterns (without its fixture exemption) or a URL credential or a JWT is refused. Stdlib only.

Public API (never raises): add, add_many, rebuild, rebuild_report, search, show, restore, restore_report, list_backups,
parse_log, derive_tags, derive_actions, last_backup.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import unicodedata
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
READ_LOCK_WAIT_S = 0.5
LOCK_STALE_S = 120.0
BACKUP_KEEP = 20
BACKUP_DAYS = 30
TMP_SWEEP_S = 600.0
GENERAL_CAP = 2
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


def _note_no_offdrive(sd: Path) -> None:
    """Once per store (marker file): off-drive backups are off because the store is moved and HARNESS_LESSONS_BACKUP is unset."""
    try:
        marker = sd / "backup-offdrive-off.marker"
        if marker.exists():
            return
        _log_event("backup", f"store {sd} is not the default store and HARNESS_LESSONS_BACKUP is unset: backups go to "
                             f"{sd / 'backup'}, off-drive backups are off")
        marker.write_bytes(b"1\n")
    except OSError:
        pass


def backup_dir() -> Path:
    override = os.environ.get("HARNESS_LESSONS_BACKUP")
    if override:
        return Path(override)
    sd = store_dir()
    try:  # a store moved by HARNESS_LESSONS_DIR or HARNESS_HOOK_STATE never reaches the real backup drive
        moved = sd.resolve() != (Path.home() / ".harness-hooks" / "lessons").resolve()
    except OSError:
        moved = True
    if moved:
        _note_no_offdrive(sd)
        return sd / "backup"
    if os.name == "nt":
        return Path("D:\\Backup\\lessons")
    return Path.home() / ".harness-hooks" / "lessons-backup"


def _log_event(where: str, message: str, **extra) -> None:
    """One line to errors.jsonl; never raises."""
    try:
        rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "where": where, "error": str(message)[:300]}
        rec.update(extra)
        with open(store_dir() / "errors.jsonl", "ab") as fh:
            fh.write((json.dumps(rec, ensure_ascii=True) + "\n").encode("ascii"))
    except Exception:  # noqa: BLE001 - the error log itself must never break a caller
        pass


def _log_error(where: str, exc) -> None:
    if isinstance(exc, BaseException):
        _log_event(where, f"{type(exc).__name__}: {exc}")
    else:
        _log_event(where, exc)


def _write_raw(path: Path, data: bytes, retries: int = 5) -> None:
    """Atomic write of raw bytes (temp file in the same folder, then os.replace, retried on Windows PermissionError)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(retries):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == retries - 1:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _read_text(path: Path) -> str:
    """UTF-8 text with a leading BOM removed; raises OSError / UnicodeDecodeError."""
    return path.read_bytes().decode("utf-8-sig")


def _sweep_tmp(dirs) -> None:
    """Remove leftover `*.tmp<pid>` files older than TMP_SWEEP_S (a crashed writer leaves them behind)."""
    cutoff = time.time() - TMP_SWEEP_S
    for d in dirs:
        try:
            for f in Path(d).iterdir():
                if re.search(r"\.tmp\d*$", f.name) and f.is_file() and f.stat().st_mtime < cutoff:
                    try:
                        f.unlink()
                    except OSError:
                        pass
        except OSError:
            pass


# ------------------------------------------------------------------ text helpers

def normalise(text: str) -> str:
    """Casefold and keep letters, digits and combining marks of any script; everything else becomes one space."""
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    spaced = "".join(ch if unicodedata.category(ch)[0] in "LNM" else " " for ch in folded)
    return re.sub(r"\s+", " ", spaced).strip()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def rule_key(rule: str) -> str:
    n = normalise(rule)
    return _sha(n if n else "raw:" + (rule or "").strip())


def entry_idem(project: str, date: str, title: str, kind: str, ordinal: int, block: int = 0) -> str:
    """Identity of a log entry: project, date, title, kind, ordinal among entries of that kind in the block, and the block's
    ordinal among the blocks of that file with the same date and title (two `## <date> Session retro` blocks never share an
    idem; block 0 hashes as before). Never the rule wording and never the commit sha; the sha is kept only as provenance."""
    parts = [project, date, title, kind, str(int(ordinal))]
    if int(block):
        parts.append(str(int(block)))
    return _sha("\n".join(parts))


URL_CRED_RE = re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s/:@]+:[^\s/@]+@", re.I)
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*")
_BUILTIN_SECRET_RES = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[0-9A-Za-z_-]{20,}\b"), re.compile(r"\bgh[pousr]_[0-9A-Za-z]{30,}\b"),
    re.compile(r"\bgithub_pat_[0-9A-Za-z_]{40,}\b"), re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}\b"),
    URL_CRED_RE, JWT_RE,
)


def secret_hits(*texts: str) -> list:
    """Fingerprints (first 4 chars + length, never the value) of every secret-looking value. The secret_policy patterns are
    used WITHOUT its fixture-hint exemption: a store entry is never allowed to hold a key, example or not."""
    blob = "\n".join(t for t in texts if t)
    if not blob:
        return []
    rxs = list(_BUILTIN_SECRET_RES)
    generic = None
    if _sp is not None:
        try:
            rxs += [rx for _, rx in _sp.KNOWN_VALUE_RES]
            generic = _sp.GENERIC_VALUE_RE
        except Exception as exc:  # noqa: BLE001
            _log_error("secret_hits", exc)
    found = []
    for rx in rxs:
        for m in rx.finditer(blob):
            found.append(m.group(0))
    if generic is not None:
        for m in generic.finditer(blob):
            if len(set(m.group(2))) >= 8:
                found.append(m.group(2))
    return list(dict.fromkeys(f"{v[:4]}...(len={len(v)})" for v in found))


def has_secret(*texts: str) -> bool:
    return bool(secret_hits(*texts))


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


def _clean_tag(t) -> str:
    """A tag or action is one token: '|' (index column separator), ',' (list separator) and blanks become '-'."""
    return re.sub(r"[|,\s]+", "-", str(t).strip().lower()).strip("-")


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
    """Parse a core/LESSONS_LOG.md. Returns {"entries": [{date, kind, title, rule, text, ordinal, block}], "no_rule": n,
    "no_rule_texts": [text], "neutral": n, "unparsed": n, "total": n}. One entry per Kind marker; an entry with no rule is
    counted in no_rule, a NEUTRAL one in neutral; a `## ` header without a date or a dated block with no recognisable Kind
    (and some content) is counted in unparsed. `ordinal` is the entry's position among entries of its kind inside its block; `block` is the block's position among
    the blocks of the file with the same date and title. Recognised shapes:
    `## <date> <title>` blocks with bullet or plain `Kind:` lines, a `(KIND)` suffix in the header, one-line
    `- KIND <date> <scope>: text`, and `- <date> <scope>: KIND - text`."""
    text = (text or "").lstrip("\ufeff")
    blocks: list = []
    cur_block = None
    unparsed = 0
    undated_content = False
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        hm = HEADER_RE.match(line)
        if hm:
            if undated_content:
                unparsed += 1
            undated_content = False
            cur_block = {"date": hm.group(1), "title": hm.group(2), "lines": []}
            blocks.append(cur_block)
        elif line.startswith("## "):
            if undated_content:
                unparsed += 1
            undated_content = False
            cur_block = {"undated": True, "lines": []}  # a header without a date: its lines belong to no entry
        elif cur_block is not None and cur_block.get("undated"):
            if line.strip():
                undated_content = True
        elif cur_block is not None:
            cur_block["lines"].append(line)
    if undated_content:
        unparsed += 1
    entries: list = []
    block_seen: dict = {}
    for b in blocks:
        bkey = (b["date"], b["title"])
        b["block"] = block_seen.get(bkey, 0)  # position among the blocks with the same date and title
        block_seen[bkey] = b["block"] + 1
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
            elif any(x.strip() for x in b["lines"]):
                unparsed += 1  # a dated block with content but no recognisable Kind
        per_kind: dict = {}
        for c in found:
            c["block"] = b["block"]
            c["ordinal"] = per_kind.get(c["kind"], 0)  # position among the entries of the same kind in this block
            per_kind[c["kind"]] = c["ordinal"] + 1
            _finish(c, entries)
    out, no_rule, no_rule_texts, neutral = [], 0, [], 0
    for e in entries:
        if e["kind"] == "NEUTRAL":
            neutral += 1
            continue
        rule = _rule_of(e["text"])
        if not rule:
            no_rule += 1
            no_rule_texts.append(e["text"])
            continue
        out.append({"date": e["date"], "kind": e["kind"], "title": e["title"], "rule": rule, "text": e["text"],
                    "ordinal": e["ordinal"], "block": e["block"]})
    return {"entries": out, "no_rule": no_rule, "no_rule_texts": no_rule_texts, "neutral": neutral,
            "unparsed": unparsed, "total": len(entries)}


# ------------------------------------------------------------------ jsonl access

def _jsonl() -> Path:
    return store_dir() / "lessons.jsonl"


def _parse_bytes(data: bytes) -> dict:
    """{recs: [(lineno, raw, obj_or_None)], ends_nl, bom}; a line that is not a JSON object with id and rule has obj None."""
    bom = data.startswith(b"\xef\xbb\xbf")
    if bom:
        data = data[3:]
    recs = []
    for n, raw in enumerate(data.split(b"\n"), 1):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw.decode("utf-8"))
            recs.append((n, raw, obj if isinstance(obj, dict) and "id" in obj and "rule" in obj else None))
        except (ValueError, UnicodeDecodeError):
            recs.append((n, raw, None))
    return {"recs": recs, "ends_nl": data.endswith(b"\n") or not data, "bom": bom}


def _scan(path: Path) -> dict:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return {"recs": [], "ends_nl": True, "bom": False, "exists": False}
    out = _parse_bytes(data)
    out["exists"] = True
    return out


def _read_records(path: Path):
    """[(raw_line_bytes, obj_or_None)] and whether the file ends with a newline (kept for compatibility)."""
    sc = _scan(path)
    return [(raw, obj) for _, raw, obj in sc["recs"]], sc["ends_nl"]


def load_lessons() -> list[dict]:
    return [o for _, _, o in _scan(_jsonl())["recs"] if o is not None]


def _line(obj: dict) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _report_bad(bad: list, action: str, where: str, save: bool) -> bool:
    """Log every unusable jsonl line (number and length) once per distinct content (corrupt/logged.txt keeps the hashes);
    when `save`, keep the bytes with their line numbers in corrupt/<ts>.txt. Returns True when nothing had to be saved or the
    bytes were written and fsynced; False when the quarantine copy failed (the caller must then keep the lines)."""
    if not bad:
        return True
    d = store_dir() / "corrupt"
    seen_f = d / "logged.txt"
    try:
        seen = set(seen_f.read_text(encoding="ascii", errors="replace").split())
    except OSError:
        seen = set()
    fresh = []
    for n, raw, _ in bad:
        h = hashlib.sha256(raw).hexdigest()[:24]
        if h not in seen:
            fresh.append((n, raw, h))
    if not fresh and not save:
        return True
    saved = True
    for n, raw, _h in fresh:
        _log_event(where, f"jsonl line {n} {action}", line=n, length=len(raw))
    try:
        d.mkdir(parents=True, exist_ok=True)
        if save:
            name = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.txt"
            with open(name, "ab") as fh:
                for n, raw, _ in bad:
                    fh.write(f"# line {n} length {len(raw)}\n".encode("ascii") + raw + b"\n")
                fh.flush()
                os.fsync(fh.fileno())
        if fresh:
            with open(seen_f, "ab") as fh:
                fh.write("".join(h + "\n" for _, _, h in fresh).encode("ascii"))
    except OSError as exc:
        _log_error(where + ":corrupt", exc)
        saved = not save
    return saved


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
    tags = [t for t in (_clean_tag(x) for x in (item.get("tags") or [])) if t]
    actions = [a for a in (_clean_tag(x) for x in (item.get("actions") or [])) if a]
    title = item.get("title")
    title = None if title is None else str(title)
    hits = secret_hits(rule, title or "", str(item.get("entry_text") or ""), " ".join(tags), " ".join(actions), project,
                       source["repo"])
    if hits:
        _log_event("add", "secret refused", fingerprints=hits)
        return None, "secret"
    date = str(item.get("date") or "")
    if not re.fullmatch(r"\d{4}-\d\d-\d\d", date):
        date = time.strftime("%Y-%m-%d")
    key = rule_key(rule)
    try:
        ordinal = int(item.get("ordinal") or 0)
        block = int(item.get("block") or 0)
    except (TypeError, ValueError):
        ordinal, block = 0, 0
    # a manual add has no log title: its identity then rests on the rule itself
    ident_title = title if title is not None else "rule:" + key
    entry = {"date": date, "project": project, "kind": kind, "tags": tags, "actions": actions or ["general"],
             "rule": rule, "source": source, "key": key,
             "idem": entry_idem(project, date, ident_title, kind, ordinal, block), "seen_in": [project]}
    if source["sha"]:
        entry["shas"] = [source["sha"]]
    return entry, None


def _queue(entry: dict) -> None:
    box = store_dir() / "outbox"
    box.mkdir(parents=True, exist_ok=True)
    # The name carries the rule key as well as the identity: a reworded rule shares the idem with its earlier wording, and
    # both must survive the queue (GPT-PM S20 F1). A retry of the same entry still lands on the same file.
    name = f"{entry['idem']}-{_sha(entry['key'])[:16]}.json"
    _agentio.atomic_write_text(box / name, json.dumps(entry, ensure_ascii=False))


def _apply(entries: list, results: list) -> bool:
    """Apply prepared entries to lessons.jsonl; the caller holds the lock. Appends `{status,id}` to results.
    Returns True when the file changed."""
    path = _jsonl()
    sc = _scan(path)
    recs = sc["recs"]
    rewrite = sc["bom"]
    bad = [r for r in recs if r[2] is None]
    keep_bad: dict = {}
    if bad:  # truncated or corrupt lines: keep their bytes in corrupt/, log once, rewrite the store without them
        if _report_bad(bad, "dropped", "apply", save=True):
            recs = [r for r in recs if r[2] is not None]
        else:  # never drop bytes that could not be saved: they stay in the rewritten store
            _log_event("apply", f"quarantine failed; {len(bad)} unparsable line(s) kept in the store")
            keep_bad = {id(r): r[1] for r in bad}
        rewrite = True
    elif not sc["ends_nl"]:
        rewrite = True
    objs = [o for _, _, o in recs if o is not None]
    by_idem: dict = {}
    by_key: dict = {}
    for o in objs:
        for i in [o.get("idem")] + list(o.get("idems") or []):
            if i:
                by_idem.setdefault(i, o)
        by_key.setdefault(o.get("key"), o)
    added: list = []

    def note_sha(rec: dict, e: dict) -> bool:
        sha = (e.get("source") or {}).get("sha")
        if not sha:
            return False
        shas = rec.setdefault("shas", [])
        if sha in shas:
            return False
        shas.append(sha)
        return True

    for e in entries:
        old = by_idem.get(e["idem"])
        # a stored record is never rewritten: a rule reworded at its source (same idem, other key) becomes a new lesson below
        if old is not None and old.get("key") == e["key"]:
            if note_sha(old, e):
                rewrite = True
            results.append({"status": "duplicate", "id": old.get("id")})
            continue
        old = by_key.get(e["key"])
        if old is None and by_idem.get(e["idem"]) is not None:
            _log_event("apply", f"reworded at source: new lesson {_next_id(objs)}, earlier {by_idem[e['idem']].get('id')} kept")
        if old is not None:
            changed = False
            idems = old.setdefault("idems", [])
            if e["idem"] not in idems and e["idem"] != old.get("idem"):
                idems.append(e["idem"])
                changed = True
            if not idems:
                del old["idems"]
            by_idem.setdefault(e["idem"], old)
            if "seen_in" not in old:
                old["seen_in"] = [old.get("project")]
                changed = True
            if note_sha(old, e):
                changed = True
            if e["project"] in old["seen_in"]:
                results.append({"status": "duplicate", "id": old.get("id")})
            else:
                old["seen_in"].append(e["project"])
                changed = True
                results.append({"status": "merged", "id": old.get("id")})
            if changed:
                rewrite = True
            continue
        rec = dict(e)
        rec["id"] = _next_id(objs)
        ordered = {"id": rec["id"], **{k: v for k, v in rec.items() if k != "id"}}
        objs.append(ordered)
        by_idem.setdefault(e["idem"], ordered)
        by_key[e["key"]] = ordered
        added.append(ordered)
        results.append({"status": "added", "id": ordered["id"]})
    new_lines = [_line(o) for o in added]
    if rewrite:
        lines = [keep_bad[id(r)] if id(r) in keep_bad else _line(r[2]) for r in recs]  # every record is written
        _write_raw(path, b"".join(ln + b"\n" for ln in lines + new_lines))
        return True
    if new_lines:
        with open(path, "ab") as fh:
            fh.write(b"".join(ln + b"\n" for ln in new_lines))
            fh.flush()
            os.fsync(fh.fileno())
        return True
    return False


def _to_dead(f: Path, why) -> None:
    try:
        dead = f.parent / "dead"
        dead.mkdir(parents=True, exist_ok=True)
        os.replace(f, dead / f.name)
        _log_event("outbox", f"malformed outbox file {f.name} moved to outbox/dead ({why})")
    except OSError as exc:
        _log_error("outbox:dead", exc)


def _drain_outbox(results: list) -> None:
    box = store_dir() / "outbox"
    try:
        files = sorted(box.glob("*.json"))
    except OSError:
        return
    pending, used = [], []
    for f in files:
        try:
            e = json.loads(f.read_bytes().decode("utf-8-sig"))
        except OSError as exc:
            _log_error("outbox", exc)
            continue
        except (ValueError, UnicodeDecodeError) as exc:
            _to_dead(f, exc)
            continue
        if isinstance(e, dict) and e.get("rule") and e.get("idem") and e.get("key"):
            pending.append(e)
            used.append(f)
        else:
            _to_dead(f, "missing rule, idem or key")
    if pending:
        _apply(pending, results)
    for f in used:
        try:
            f.unlink()
        except OSError:
            pass


def add_many(items: list) -> list:
    """Add several lessons under one lock and rebuild once. Each item is a dict with rule, kind, project and optional
    date, tags, actions, source {repo, sha}, title, ordinal and block (the log entry's identity), entry_text. Returns one
    {status, id} per item (status: added, merged, duplicate, queued, failed, secret, excluded, invalid) or []
    on an internal error. A failure to drain the outbox or to rebuild after a successful write is logged, not returned."""
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
        _LAST_BACKUP.update(status="none", detail="")
        sd = store_dir()
        with _agentio.file_lock(sd / "lessons.jsonl", wait=LOCK_WAIT_S, stale=LOCK_STALE_S) as got:
            if not got:
                _log_event("add_many", f"lock timeout after {LOCK_WAIT_S:g}s; {len(prepared)} entries queued in outbox")
                for e in prepared:
                    try:
                        _queue(e)
                        results.append({"status": "queued", "id": None})
                    except Exception as exc:  # noqa: BLE001
                        _log_error("queue", exc)
                        results.append({"status": "failed", "id": None})
                return results
            _apply(prepared, results)
            try:
                _drain_outbox([])
            except Exception as exc:  # noqa: BLE001 - the write above already succeeded
                _log_error("drain_outbox", exc)
            try:
                _rebuild_locked(backup=True)
            except Exception as exc:  # noqa: BLE001
                _log_error("rebuild_after_add", exc)
        return results
    except Exception as exc:  # noqa: BLE001 - fail open
        _log_error("add_many", exc)
        return []


def add(rule: str, kind: str, project: str, **kw):
    """Add one lesson; returns {status, id} or None on an internal error."""
    res = add_many([dict(kw, rule=rule, kind=kind, project=project)])
    return res[0] if res else None


# ------------------------------------------------------------------ backup

_LAST_BACKUP = {"status": "none", "detail": ""}
_BACKUP_NAME_RE = re.compile(r"^lessons-(\d{8})-(\d{6})-(.+)\.jsonl$")


def last_backup() -> dict:
    """{status: none|ok|skipped|failed, detail} of the last backup attempt in this process."""
    return dict(_LAST_BACKUP)


def _set_backup(status: str, detail: str = "") -> None:
    _LAST_BACKUP["status"], _LAST_BACKUP["detail"] = status, detail
    if status in ("skipped", "failed"):
        _log_event("backup", f"{status}: {detail}")


def _backup_files(bd: Path) -> list:
    try:
        return sorted(f for f in bd.glob("lessons-*.jsonl") if f.is_file())  # names sort by timestamp then counter
    except OSError:
        return []


def _valid_bad(data: bytes):
    recs = _parse_bytes(data)["recs"]
    bad = sum(1 for r in recs if r[2] is None)
    return len(recs) - bad, bad


def _prune_backups(bd: Path) -> None:
    files = _backup_files(bd)
    keep = set(files[-BACKUP_KEEP:])
    today = time.strftime("%Y%m%d")
    newest_of_day: dict = {}
    for f in files:
        m = _BACKUP_NAME_RE.match(f.name)
        if m:
            newest_of_day[m.group(1)] = f  # files are sorted, the last one of a day wins
    for day, f in newest_of_day.items():
        try:
            age = (time.mktime(time.strptime(today, "%Y%m%d")) - time.mktime(time.strptime(day, "%Y%m%d"))) / 86400
        except ValueError:
            continue
        if age <= BACKUP_DAYS:
            keep.add(f)
    for f in files:
        if f not in keep:
            try:
                f.unlink()
            except OSError:
                pass


BASELINE_FILE = "restore-baseline.json"


def _clear_baseline() -> None:
    try:
        (store_dir() / BASELINE_FILE).unlink()
    except OSError:
        pass


def _backup(path: Path) -> None:
    """Raw byte copy of the store; skipped (and logged) when the store is damaged or smaller than the newest backup."""
    try:
        if not path.exists():
            _set_backup("none", "no store file")
            return
        data = path.read_bytes()
        valid, bad = _valid_bad(data)
        if bad:
            _set_backup("skipped", f"store has {bad} unparsable line(s)")
            return
        if valid == 0:
            _set_backup("skipped", "store holds no records")
            return
        bd = backup_dir()
        bd.mkdir(parents=True, exist_ok=True)
        files = _backup_files(bd)
        if files:
            prev = files[-1].read_bytes()
            if prev == data:
                _clear_baseline()
                _set_backup("none", "identical to the newest backup")
                return
            pv, _ = _valid_bad(prev)
            try:  # after an explicit restore the restored store is the baseline, not the newest (larger) backup
                pv = int(json.loads((store_dir() / BASELINE_FILE).read_text(encoding="ascii"))["valid"])
            except (OSError, ValueError, KeyError, TypeError):
                pass
            if valid < pv:
                _set_backup("skipped", f"store has {valid} records, newest backup {files[-1].name} has {pv}")
                return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        counter = 1
        for f in files:
            m = _BACKUP_NAME_RE.match(f.name)
            if m and f"{m.group(1)}-{m.group(2)}" == stamp and m.group(3).isdigit():
                counter = max(counter, int(m.group(3)) + 1)
        name = bd / f"lessons-{stamp}-{counter:03d}.jsonl"
        _write_raw(name, data)
        _clear_baseline()
        _prune_backups(bd)
        _set_backup("ok", str(name))
    except Exception as exc:  # noqa: BLE001 - a missing backup drive must never block the store
        _set_backup("failed", f"{type(exc).__name__}: {exc}")


def list_backups() -> list:
    """[{path, name, valid, bad}] newest first."""
    out = []
    for f in reversed(_backup_files(backup_dir())):
        try:
            v, b = _valid_bad(f.read_bytes())
        except OSError:
            v, b = 0, -1
        out.append({"path": str(f), "name": f.name, "valid": v, "bad": b})
    return out


# ------------------------------------------------------------------ rebuild (index, global view)

def _file_state(path: Path):
    try:
        data = path.read_bytes()
        mt = path.stat().st_mtime_ns
    except OSError:
        return 0, 0, _sha("")
    return len(data), mt, hashlib.sha256(data).hexdigest()


def _index_line(o: dict) -> str:
    clean = lambda s: str(s).replace("|", "/").replace("\n", " ").replace("\r", " ")  # noqa: E731
    cl = lambda xs: ",".join(_clean_tag(x) for x in (xs or []))  # noqa: E731
    return (f"{o['id']} | {cl(o.get('actions'))} | {cl(o.get('tags'))} | {clean(o['rule'])} | "
            f"K={o.get('kind')} n={len(o.get('seen_in') or [])} d={o.get('date')}")


def _rebuild_locked(backup: bool = True) -> None:
    sd = store_dir()
    path = _jsonl()
    _sweep_tmp([sd, sd / "outbox", sd / "sessions", backup_dir()])
    objs = load_lessons()
    size, mtime, digest = _file_state(path)
    head = f"<!-- lessons.jsonl size={size} mtime_ns={mtime} sha256={digest} count={len(objs)} -->"
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
    if backup:
        _backup(path)


def rebuild_report(backup: bool = True, wait: float = LOCK_WAIT_S) -> dict:
    """Rebuild INDEX.md and GLOBAL_LESSONS.md, drain the outbox, take a backup (when `backup`).
    Returns {ok, backup: {status, detail}}."""
    rep = {"ok": False, "backup": {"status": "none", "detail": ""}}
    try:
        _LAST_BACKUP.update(status="none", detail="")
        with _agentio.file_lock(_jsonl(), wait=wait, stale=LOCK_STALE_S) as got:
            if not got:
                _log_error("rebuild", RuntimeError("lock timeout"))
                return rep
            _report_bad([r for r in _scan(_jsonl())["recs"] if r[2] is None], "skipped (kept in file)", "rebuild", save=False)
            _drain_outbox([])
            _rebuild_locked(backup=backup)
        rep["ok"] = True
    except Exception as exc:  # noqa: BLE001
        _log_error("rebuild", exc)
    rep["backup"] = last_backup()
    return rep


def rebuild(backup: bool = True, wait: float = LOCK_WAIT_S) -> bool:
    return rebuild_report(backup=backup, wait=wait)["ok"]


# ------------------------------------------------------------------ search and show

_HEAD_RE = re.compile(r"<!-- lessons\.jsonl size=(\d+)(?: mtime_ns=(\d+))? sha256=([0-9a-f]{64})")


def _index_current() -> bool:
    """Cheap staleness check (jsonl size and mtime against the INDEX header); no hashing, no lock."""
    try:
        with open(store_dir() / "INDEX.md", "rb") as fh:
            head = fh.readline().decode("utf-8-sig", "replace")
        m = _HEAD_RE.match(head)
        if not m or m.group(2) is None:
            return False
        try:
            st = _jsonl().stat()
            size, mtime = st.st_size, st.st_mtime_ns
        except FileNotFoundError:
            size, mtime = 0, 0
        return int(m.group(1)) == size and int(m.group(2)) == mtime
    except OSError:
        return False


def search(action: str, words="", limit: int = 5, refresh: bool = False) -> list:
    """FAILURE and SHORTCOMING lessons for an action, best first. Reads the existing INDEX.md: it never takes the lock and
    never rebuilds unless the caller (the CLI) passes refresh=True, which rebuilds a stale index waiting at most
    READ_LOCK_WAIT_S for the lock and takes no backup. Rank: action match, exact tag overlap with words, number of
    projects that saw it, recency; `general` lessons only with a tag overlap, at most GENERAL_CAP of them.
    Returns [{id, rule, actions, tags, kind, seen, date}] or [] on any error."""
    try:
        if refresh and not _index_current():
            rebuild(backup=False, wait=READ_LOCK_WAIT_S)
        idx = store_dir() / "INDEX.md"
        try:
            text = _read_text(idx)
        except FileNotFoundError:
            if _jsonl().exists():
                _log_event("search", "INDEX.md missing while lessons.jsonl exists; nothing served")
            return []
        except UnicodeDecodeError as exc:
            _log_error("search:index-decode", exc)
            return []
        action = (action or "").strip().lower()
        toks = {w for w in re.split(r"[^a-z0-9-]+", (words if isinstance(words, str) else " ".join(words)).lower()) if w}
        scored = []
        for ln in text.splitlines():
            parts = [p.strip() for p in ln.split(" | ")]
            if len(parts) != 5 or not re.fullmatch(r"L\d+", parts[0]):
                continue
            meta = dict(kv.split("=", 1) for kv in parts[4].split() if "=" in kv)
            if meta.get("K") not in SEARCH_KINDS:
                continue
            acts = [a for a in parts[1].split(",") if a]
            tags = [t for t in parts[2].split(",") if t]
            overlap = sum(1 for t in tags if t in toks)
            score = 10.0 * overlap
            general = False
            if action and action in acts:
                score += 100
            elif action and "general" in acts:
                if overlap == 0:
                    continue
                score += 10
                general = True
            if score <= 0:
                continue
            try:
                seen = int(meta.get("n", "1") or 1)
            except ValueError:
                seen = 1
            score += 3 * min(seen, 10)
            date = meta.get("d", "")
            score += int(re.sub(r"\D", "", date) or 0) / 1e9
            scored.append((score, parts[0], general, {"id": parts[0], "rule": parts[3], "actions": acts, "tags": tags,
                                                      "kind": meta.get("K"), "seen": seen, "date": date}))
        scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
        out, n_general = [], 0
        if int(limit) <= 0:
            return out
        for s in scored:
            if s[2]:
                if n_general >= GENERAL_CAP:
                    continue
                n_general += 1
            out.append(s[3])
            if len(out) >= max(0, int(limit)):
                break
        return out
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


# ------------------------------------------------------------------ restore

def restore_report(from_file=None) -> dict:
    """Replace lessons.jsonl with a backup and rebuild. Without `from_file` the newest backup whose lines all parse is used;
    `from_file` is a path or a name inside the backup folder. Returns {ok, path, reason}; reason is "no backup" when the
    folder holds none (nothing changed)."""
    rep = {"ok": False, "path": None, "reason": ""}
    try:
        if from_file:
            f = Path(from_file)
            if not f.is_file():
                f = backup_dir() / str(from_file)
            if not f.is_file():
                rep["reason"] = f"backup file not found: {from_file}"
                return rep
            cands = [f]
        else:
            cands = _backup_files(backup_dir())[::-1]
            if not cands:
                rep["reason"] = "no backup"
                return rep
        chosen, data = None, b""
        for f in cands:
            try:
                d = f.read_bytes()
            except OSError:
                continue
            v, b = _valid_bad(d)
            if b == 0 and v > 0:
                chosen, data = f, d
                break
            _log_event("restore", f"backup {f.name} skipped: {v} valid, {b} unparsable line(s)")
        if chosen is None:
            rep["reason"] = "no backup with all lines parsable"
            return rep
        if data.startswith(b"\xef\xbb\xbf"):
            data = data[3:]
        if not data.endswith(b"\n"):
            data += b"\n"
        with _agentio.file_lock(_jsonl(), wait=LOCK_WAIT_S, stale=LOCK_STALE_S) as got:
            if not got:
                rep["reason"] = "lock timeout"
                _log_error("restore", RuntimeError("lock timeout"))
                return rep
            if _jsonl().exists() and _jsonl().stat().st_size:  # keep what is being replaced
                try:
                    d = store_dir() / "corrupt"
                    d.mkdir(parents=True, exist_ok=True)
                    _write_raw(d / f"replaced-by-restore-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.jsonl",
                               _jsonl().read_bytes())
                except OSError as exc:
                    # never replace a store whose current bytes could not be kept (GPT-PM S20 F2)
                    _log_error("restore:keep", exc)
                    rep["reason"] = f"current store could not be preserved, nothing restored: {type(exc).__name__}: {exc}"
                    return rep
            _LAST_BACKUP.update(status="none", detail="")
            try:  # the marker goes first; a failed marker never turns a completed restore into a failure
                _write_raw(store_dir() / BASELINE_FILE,
                           json.dumps({"valid": _valid_bad(data)[0], "from": chosen.name}).encode("ascii", "replace"))
            except OSError as exc:
                _log_error("restore:baseline", exc)
            _write_raw(_jsonl(), data)
            _rebuild_locked(backup=True)
        rep["ok"], rep["path"] = True, str(chosen)
    except Exception as exc:  # noqa: BLE001
        _log_error("restore", exc)
        rep["reason"] = f"{type(exc).__name__}: {exc}"
    return rep


def restore(from_file=None):
    """Returns the backup path used, or None."""
    return restore_report(from_file)["path"]
