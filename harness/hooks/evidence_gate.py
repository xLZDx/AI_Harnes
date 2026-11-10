#!/usr/bin/env python3
"""SubagentStop gate that rejects only demonstrably invalid local file/line citations.

It does not require citations for opinions and does not judge whether a cited line proves
a claim. It is intentionally scoped in settings to reviewer-type agents. The
`stop_hook_active` flag is the loop guard; no persistent marker files are needed.
One file for Claude Code and Codex: both send the same Stop/SubagentStop stdin and accept the same
{"decision": "block", "reason": ...} output. Decisions are logged under _agentio.state_dir("citation_gate").
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

MAX_CITATIONS = 60
SKIP_DIRS = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', 'dist', 'build', '.pytest_cache'}
URL_RE = re.compile(r'https?://\S+')
PLACEHOLDER_RE = re.compile(r'\.\.\.|…')
MD_LINK_RE = re.compile(r'\[[^\]\n]*\]\(\s*([^)#\n]+?)\s*(?:#L(\d+)(?:-L?(\d+))?)?\s*\)')
ABS_PATH_RE = re.compile(r'[A-Za-z]:[\\/][^\r\n:*?"<>|`]*\.[A-Za-z0-9]{1,8}+(?![\\/])(?::(\d+)(?:\s*-\s*(\d+))?)?')
PLAIN_CITE_RE = re.compile(r'(?<![\w/\\.#-])((?:[\w.+-]+[\\/])*[\w.+-]+\.[A-Za-z0-9]{1,8}):(\d+)(?:\s*-\s*(\d+))?(?![\w.])')
ENDS_IN_EXT_RE = re.compile(r'\.[A-Za-z0-9]{1,8}$')
IPV4_RE = re.compile(r'^\d{1,3}(?:\.\d{1,3}){3}$')
MAX_LABEL_REPORTS = 12
LABEL_LINK_RE = re.compile(r'\[([^\]\n]+)\]\(\s*([^)\s]+?)\s*\)')
LINE_ANCHOR_RE = re.compile(r'#L\d+(?:-L?\d+)?$')
SCHEME_RE = re.compile(r'[A-Za-z][A-Za-z0-9+.-]+:')  # two or more, so a C: drive is not a scheme


def state_dir() -> Path:
    override = os.environ.get('CLAUDE_HOOK_STATE_DIR')
    if override:
        return Path(override) / 'citation_gate'
    return _agentio.state_dir('citation_gate')


def log(rec: dict) -> None:
    try:
        d = state_dir(); d.mkdir(parents=True, exist_ok=True)
        with (d / 'decisions.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except OSError:
        pass


def is_noise(path: str) -> bool:
    if IPV4_RE.match(path):
        return True
    ext = path.rsplit('.', 1)[-1]
    return not re.search(r'[A-Za-z]', ext)


def extract(text: str) -> list[dict]:
    found: dict[tuple[str, int | None], dict] = {}
    work = URL_RE.sub(lambda m: ' ' * len(m.group(0)), text)

    def take(m: re.Match, path: str, start: str | None) -> None:
        nonlocal work
        if path and not is_noise(path):
            line = int(start) if start else None
            found.setdefault((path, line), {
                'path': path,
                'line': line,
                'raw': m.group(0).strip(),
                'placeholder': bool(PLACEHOLDER_RE.search(path)),
            })
        work = work[:m.start()] + ' ' * (m.end() - m.start()) + work[m.end():]

    for m in list(MD_LINK_RE.finditer(work)):
        path = m.group(1).strip(); take(m, path if '.' in Path(path).name else '', m.group(2))
    for m in list(ABS_PATH_RE.finditer(work)):
        path = re.sub(r':\d+(?:\s*-\s*\d+)?$', '', m.group(0)).strip(); take(m, path, m.group(1))
    for m in list(PLAIN_CITE_RE.finditer(work)):
        take(m, m.group(1), m.group(2))
    return list(found.values())[:MAX_CITATIONS]


def resolve_exact(raw: str, cwd: Path, check=Path.is_file) -> Path | None:
    p = Path(raw)
    if p.is_absolute():
        return p if check(p) else None
    direct = cwd / p
    if check(direct):
        return direct
    if os.sep not in raw:
        return None
    try:
        for child in cwd.iterdir():
            if not child.is_dir() or child.name in SKIP_DIRS or child.name.startswith('.'):
                continue
            cand = child / p
            if check(cand):
                return cand
    except OSError:
        pass
    return None


def resolve(path_str: str, cwd: Path) -> tuple[Path | None, bool]:
    raw = path_str.replace('\\', os.sep).replace('/', os.sep).strip()
    hit = resolve_exact(raw, cwd)
    if hit:
        return hit, True
    toks = raw.split(' ')
    for i in range(len(toks) - 1, 0, -1):
        cand = ' '.join(toks[:i])
        if not ENDS_IN_EXT_RE.search(cand):
            continue
        hit = resolve_exact(cand, cwd)
        if hit:
            return hit, False
    return None, True


def norm(text: str) -> str:
    return text.replace('\\', '/').lower()


def repo_relative(resolved: Path) -> str | None:
    """Path relative to the nearest enclosing git repository."""
    for parent in resolved.parents:
        try:
            if (parent / '.git').exists():
                return norm(str(resolved.relative_to(parent)))
        except OSError:
            return None
    return None


def label_target(resolved: Path) -> str | None:
    """Shortest path text a visible label must contain to actually name this file.

    Repo-relative is the minimum the rule asks for; a file sitting at a repo root
    would make that a bare filename, so fall back to directory + filename there.
    """
    rel = repo_relative(resolved)
    if rel and '/' in rel:
        return rel
    parts = norm(str(resolved)).split('/')
    return '/'.join(parts[-2:]) if len(parts) >= 2 else None


def bad_path_labels(text: str, cwd: Path) -> list[str]:
    """Markdown links to real local files whose visible label hides the path.

    The operator's rule: the reader gets a working link whose visible text is the
    full path. The href stays whatever makes it clickable; only the label is judged.
    """
    out: list[str] = []
    for m in LABEL_LINK_RE.finditer(text):
        label, href = m.group(1), m.group(2)
        # Defensive, and knowingly redundant: a URL also fails to resolve as a local
        # file below, so removing this changes no tested behaviour. Kept for intent.
        if SCHEME_RE.match(href) or href.startswith('#'):
            continue
        bare = LINE_ANCHOR_RE.sub('', href)
        if '.' not in Path(bare).name:  # directory link, not a file citation
            continue
        try:
            resolved, _ = resolve(bare, cwd)
        except OSError:
            continue
        if resolved is None:  # unresolvable hrefs are the other check's business
            continue
        try:
            want = label_target(resolved.resolve())
        except OSError:
            continue
        if want and want not in norm(label):
            out.append(f'[{label}]({href}) — visible label must be the full path, at least {want}')
        if len(out) >= MAX_LABEL_REPORTS:
            break
    return out


def line_count(path: Path) -> int | None:
    try:
        with path.open('rb') as f:
            return sum(1 for _ in f)
    except OSError:
        return None


def invalid_citations(citations: list[dict], cwd: Path) -> list[str]:
    hard: list[str] = []
    for c in citations:
        if c['placeholder']:
            continue
        path, line = c['path'], c['line']
        resolved, whole = resolve(path, cwd)
        if resolved is None and line is None:
            raw = path.replace('\\', os.sep).replace('/', os.sep).strip()
            if resolve_exact(raw, cwd, check=Path.exists) is not None:
                continue
        if resolved is None:
            norm = path.replace('\\', os.sep).replace('/', os.sep)
            if os.sep not in norm:  # bare filename is ambiguous, not demonstrably false
                continue
            hard.append(f"{c['raw']} — path does not resolve from {cwd}")
            continue
        if line is None or not whole:
            continue
        total = line_count(resolved)
        if total is not None and line > total:
            hard.append(f"{c['raw']} — {resolved} has only {total} lines")
    return hard


def off(var: str) -> bool:
    return os.environ.get(var, '').lower() in {'off', '0', 'false'}


def main() -> int:
    if off('CLAUDE_CITATION_GATE'):
        return 0
    data = json.loads(sys.stdin.read() or '{}')
    if data.get('stop_hook_active'):
        return 0
    if data.get('hook_event_name') == 'SubagentStop' and _agentio.is_writer(data):
        return 0  # reports of file-editing agents are not reviewer evidence (the old matcher excluded them)
    msg = data.get('last_assistant_message') or ''
    if not msg and _agentio.client(data) == 'codex':  # Codex may send null here; read the transcript instead
        msg = _agentio.last_assistant_text(data)
    cwd = Path(data.get('cwd') or os.getcwd())
    cites = extract(msg)
    hard = invalid_citations(cites, cwd)
    labels = [] if off('CLAUDE_PATH_LABEL_GATE') else bad_path_labels(msg, cwd)
    rec = {
        'ts': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'event': data.get('hook_event_name'),
        'agent_type': data.get('agent_type'),
        'citations': len(cites),
        'invalid': hard,
        'short_labels': labels,
        'verdict': 'block' if (hard or labels) else 'pass',
    }
    log(rec)
    if not hard and not labels:
        return 0
    parts = []
    if hard:
        parts.append('CITATION INTEGRITY: correct or remove these local citations before returning:\n- '
                     + '\n- '.join(hard))
    if labels:
        parts.append('FILE PATH LABELS: a file link must show the full path as its visible text '
                     '(the href stays whatever makes it clickable here):\n- ' + '\n- '.join(labels))
    reason = '\n\n'.join(parts)
    print(json.dumps({'decision': 'block', 'reason': reason}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        log({'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'verdict': 'hook-error', 'traceback': traceback.format_exc()})
        raise SystemExit(0)
