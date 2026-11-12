#!/usr/bin/env python3
"""Semantic classifier for high-confidence destructive shell commands.

This module is a component of ``shell_policy_gate.py``. The active combined gate has no
environment kill switch. Static ``permissions.deny`` rules remain defense in depth; this
classifier exists because spelling variants and wrappers make static command prefixes brittle.

Decision logs go under ``CLAUDE_HOOK_STATE_DIR`` when set, otherwise under the configured
Claude directory (``CLAUDE_CONFIG_DIR`` when present, else ``~/.claude``).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
import traceback
from pathlib import Path

# A lone `&` also separates commands, but `&>`, `>&`, `2>&1` and `&&` are not separators.
SEPARATORS = re.compile(r"&&|\|\||;|\||\n|(?<![<>&])&(?![>&])")
# Subshell/group openers, negation and compound-command keywords that precede a command.
LEADERS = re.compile(r"^(?:[({]+\s*|!\s+|(?:then|do|else|elif|if|while|until)\b\s*)", re.I)
PREFIXES = re.compile(
    r"^(?:"
    r"sudo(?:\s+(?:(?-i:-[ughpCDrtUT])\s+\S+|"
    r"--(?:user|group|host|prompt|chdir|role|type|other-user|command-timeout|close-from)\s+\S+|"
    r"-\w+|--[\w-]+(?:=\S+)?|--(?=\s)))*|"
    r"doas(?:\s+(?:(?-i:-[uC])\s+\S+|-\w+|--(?=\s)))*|"
    r"env(?:\s+(?:-\S+|[A-Za-z_][A-Za-z0-9_]*=\S+))*|"
    r"nohup|time|"
    r"nice(?:\s+(?:-n\s*-?\d+|-?\d+))?|"
    r"timeout(?:\s+(?:(?-i:-[sk]|--signal|--kill-after)\s+\S+|--?\S+(?:=\S+)?))*\s+\S+|"
    r"setsid(?:\s+-\S+)*|"
    r"stdbuf(?:\s+-[ioe]\S+)*|"
    r"ionice(?:\s+(?:-[cnpt]\s*\S+|--[\w-]+(?:=\S+)?))*|"
    r"command|builtin|exec|xargs(?:\s+-\S+)*"
    r")\s+",
    re.I,
)
SHELL_C = re.compile(
    r"^(?:[\w./\\-]*\b(?:ba|z|k|d)?sh|pwsh|powershell(?:\.exe)?|cmd(?:\.exe)?)\s+"
    r"(?:-\S+\s+)*(?:-c|-Command|/c)\s+(.*)$",
    re.I,
)
DOWNLOAD_PIPE = re.compile(
    r"(?is)(?<!\S)(?:curl|wget|iwr|invoke-webrequest|fetch)\b[^\n|]*\|\s*"
    r"(?:sudo\s+)?(?:[\w./\\-]*(?:ba|z|k|d)?sh|pwsh|powershell(?:\.exe)?|perl|ruby|node)\b"
)

EVAL_WRAPPER = re.compile(r"^eval\s+(.*)$", re.I | re.S)

DOWNLOAD_SUBSTITUTION_EXEC = re.compile(
    r"(?is)(?:\b(?:eval|source)\b\s+|(?<!\S)\.\s+|\b(?:ba|z|k|d)?sh\b[^\n]*?-c\s+|"
    r"\b(?:python(?:[0-9.]*)?|node|perl|ruby)\b[^\n]*?(?:-c|-e)\s+)"
    r"[\"']?\$\(\s*(?:curl|wget|fetch|iwr|invoke-webrequest)\b"
)

DOWNLOAD_PY_STDIN = re.compile(
    r"(?is)(?<!\S)(?:curl|wget|iwr|invoke-webrequest|fetch)\b[^\n|]*\|\s*"
    r"python(?:[0-9.]*)?(?:\s+-)?\s*(?:$|[;&])"
)


def state_dir() -> Path:
    override = os.environ.get("CLAUDE_HOOK_STATE_DIR")
    if override:
        base = Path(override)
    else:
        cfg = os.environ.get("CLAUDE_CONFIG_DIR")
        base = Path(cfg).expanduser() if cfg else Path.home() / ".claude"
        base = base / "tmp"
    target = base / "claude_dangerous_cmd_gate"
    try:
        target.mkdir(parents=True, exist_ok=True)
        return target
    except OSError:
        fallback = Path.home() / ".claude_dangerous_cmd_gate"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def log(record: dict) -> None:
    try:
        d = state_dir()
        d.mkdir(parents=True, exist_ok=True)
        with (d / "decisions.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _command_substitutions(text: str) -> list[str]:
    """Extract literal shell command substitutions without executing or expanding them.

    This intentionally handles the common `$()` and backtick forms. Dynamic indirection
    through variables remains outside the scope of this deterministic hook.
    """
    found: list[str] = []
    i = 0
    while i < len(text):
        if text.startswith("$(", i) or text.startswith("<(", i) or text.startswith(">(", i):
            start = i + 2
            depth = 1
            j = start
            quote: str | None = None
            escaped = False
            while j < len(text):
                ch = text[j]
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif quote:
                    if ch == quote:
                        quote = None
                elif ch in "\"'":
                    quote = ch
                elif text.startswith("$(", j) or text.startswith("<(", j) or text.startswith(">(", j):
                    depth += 1
                    j += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        found.append(text[start:j])
                        i = j
                        break
                j += 1
        elif text[i] == "`":
            j = i + 1
            escaped = False
            while j < len(text):
                ch = text[j]
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == "`":
                    found.append(text[i + 1:j])
                    i = j
                    break
                j += 1
        i += 1
    return found


def _words(seg: str) -> list[tuple[str, int]]:
    """Whitespace-split words with their start offsets; a quoted span ends at the first matching quote."""
    out: list[tuple[str, int]] = []
    i, n = 0, len(seg)
    while i < n:
        if seg[i].isspace():
            i += 1
            continue
        start = i
        while i < n and not seg[i].isspace():
            if seg[i] in "'\"":
                j = seg.find(seg[i], i + 1)
                i = n if j < 0 else j + 1
            else:
                i += 1
        out.append((seg[start:i], start))
    return out


_EXEC_VALUE_FLAGS = {
    "docker": {"-e", "--env", "-u", "--user", "-w", "--workdir", "--env-file", "--detach-keys"},
    "kubectl": {"-n", "--namespace", "-c", "--container", "-f", "--filename", "--context", "--kubeconfig", "--pod-running-timeout"},
    "ssh": set("-" + c for c in "bcDEeFIiJLlmOopQRSWw"),
    "wsl": {"-d", "--distribution", "-u", "--user", "--cd", "--distribution-id"},
    "nsenter": {"-t", "--target", "-S", "--setuid", "-G", "--setgid", "-w", "--wd"},
    "chroot": {"--userspec", "--groups"},
}


def _rest(seg: str, words: list[tuple[str, int]], idx: int) -> str | None:
    """Text of the command that starts at word idx; a single quoted word is unquoted."""
    if idx >= len(words):
        return None
    rest = seg[words[idx][1]:].strip()
    if idx == len(words) - 1:
        rest = _unquote(rest)
    return rest or None


def _skip_flags(words: list[tuple[str, int]], idx: int, kind: str) -> int:
    values = _EXEC_VALUE_FLAGS.get(kind, set())
    while idx < len(words) and words[idx][0].startswith("-") and words[idx][0] != "--":
        w = words[idx][0]
        idx += 2 if (w in values) else 1
    return idx


def _exec_inner(seg: str) -> str | None:
    """The command an exec-style wrapper runs (docker/podman/kubectl exec, ssh, wsl, su -c, runuser, nsenter, chroot), or None."""
    words = _words(seg)
    if not words:
        return None
    base = words[0][0].lower().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if base.endswith(".exe"):
        base = base[:-4]
    if base in {"docker", "podman", "kubectl"}:
        kind = "kubectl" if base == "kubectl" else "docker"
        idx = _skip_flags(words, 1, kind)
        if idx < len(words) and words[idx][0].lower() in {"container", "compose"} and base != "kubectl":
            idx += 1
            idx = _skip_flags(words, idx, kind)
        if idx >= len(words) or words[idx][0].lower() != "exec":
            return None
        idx = _skip_flags(words, idx + 1, kind)
        if kind == "kubectl":
            for k in range(idx, len(words)):
                if words[k][0] == "--":
                    return _rest(seg, words, k + 1)
            return None
        return _rest(seg, words, idx + 1)  # words[idx] is the container
    if base == "ssh":
        idx = _skip_flags(words, 1, "ssh")
        return _rest(seg, words, idx + 1)  # words[idx] is the host
    if base == "wsl":
        idx = _skip_flags(words, 1, "wsl")
        if idx < len(words) and words[idx][0] == "--":
            idx += 1
        return _rest(seg, words, idx)
    if base in {"su", "runuser"}:
        for k in range(1, len(words) - 1):
            if words[k][0] in {"-c", "--command"} or (words[k][0].startswith("-") and not words[k][0].startswith("--") and words[k][0].endswith("c") and len(words[k][0]) > 2):
                return _rest(seg, words, k + 1)
        for k in range(1, len(words)):
            if words[k][0] == "--":
                return _rest(seg, words, k + 1)
        return None
    if base == "nsenter":
        for k in range(1, len(words)):
            if words[k][0] == "--":
                return _rest(seg, words, k + 1)
        return _rest(seg, words, _skip_flags(words, 1, "nsenter"))
    if base == "chroot":
        idx = _skip_flags(words, 1, "chroot")
        return _rest(seg, words, idx + 1)  # words[idx] is the new root
    return None


def segments(command: str, depth: int = 0) -> list[str]:
    """Return independently classifiable commands, unwrapping common shell wrappers."""
    out: list[str] = []
    if depth < 3:
        for inner in _command_substitutions(command or ""):
            out.extend(segments(inner, depth + 1))

    for raw in SEPARATORS.split(command or ""):
        seg = raw.strip()
        while seg:
            stripped = PREFIXES.sub("", seg, count=1)
            stripped = LEADERS.sub("", stripped.strip(), count=1)
            if stripped == seg:
                break
            seg = stripped.strip()
        while seg.endswith((")", "}")) and seg.count(")") + seg.count("}") > seg.count("(") + seg.count("{"):
            seg = seg[:-1].rstrip()
        if not seg:
            continue
        if depth < 3:
            wrapped = _exec_inner(seg)
            if wrapped:
                inner = segments(wrapped, depth + 1)
                if inner:
                    out.extend(inner)
                    continue
        m = SHELL_C.match(seg)
        if m and depth < 3:
            inner = segments(_unquote(m.group(1)), depth + 1)
            if inner:
                out.extend(inner)
                continue
        m = EVAL_WRAPPER.match(seg)
        if m and depth < 3:
            inner = segments(_unquote(m.group(1)), depth + 1)
            if inner:
                out.extend(inner)
                continue
        out.append(seg)
    return out


def tokens(seg: str) -> list[str]:
    try:
        return shlex.split(seg, posix=True)
    except ValueError:
        return seg.split()


GIT_VALUE_OPTIONS = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix", "--config-env"}


def skip_git_global_options(toks: list[str]) -> list[str]:
    """Drop git global options (`-C <dir>`, `-c k=v`, `--git-dir=...`) so toks[1] is the subcommand."""
    i = 1
    while i < len(toks) and toks[i].startswith("-"):
        i += 2 if toks[i] in GIT_VALUE_OPTIONS else 1
    return toks[:1] + toks[i:]


def has_long(seg: str, name: str) -> bool:
    return bool(name and re.search(r"(?<!\S)--" + re.escape(name) + r"(?:=\S+)?(?!\w)", seg))


def has_short(seg: str, short: str) -> bool:
    if not short:
        return False
    for token in tokens(seg):
        if re.fullmatch(r"-[A-Za-z]+", token) and short in token[1:]:
            return True
    return False


def has_flag(seg: str, short: str, long_name: str) -> bool:
    return has_long(seg, long_name) or has_short(seg, short)


def classify(seg: str) -> str | None:
    low = seg.lower().strip()
    toks = tokens(seg)
    if not toks:
        return None
    base = toks[0].lower().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]

    # Filesystem / disk destruction.
    if base in {"rm", "rmdir"}:
        recursive = has_flag(seg, "r", "recursive") or has_short(seg, "R")
        force = has_flag(seg, "f", "force")
        targets = [t for t in toks[1:] if not t.startswith("-")]
        broad = any(
            re.fullmatch(
                r"/|~|~/|\.|\./|\*|/\*|\$HOME|%USERPROFILE%|[A-Za-z]:[\\/]?|"
                r"/(?:etc|usr|var|bin|lib|opt|home|boot|System)/?",
                t,
                re.I,
            )
            for t in targets
        )
        if recursive and force:
            return "recursive force delete"
        if broad:
            return "delete targeting root/home/drive root/bare wildcard"
    if base in {"shred", "mkfs", "fdisk", "parted"} or base.startswith("mkfs."):
        return f"disk/partition destructive utility `{base}`"
    if base == "dd" and re.search(r"(?:^|\s)of=", low):
        return "`dd` with an output target"
    if base in {"format", "diskpart"}:
        return f"disk utility `{base}`"

    # Git history / worktree destruction.
    #
    # Narrowed 2026-08-21, operator instruction: "все действия с гитом разрешены кроме
    # удаления (удаление -- только по отдельному подтверждению)" -- all git actions are
    # allowed except deletion (deletion needs separate confirmation), given after this exact
    # gate blocked a legitimate 5-file merge-conflict resolution in a shared ERP worktree
    # with no way to grant a scoped exception. Force push and history rewrite
    # (filter-branch/filter-repo) no longer block here.
    #
    # `reset --hard` and `clean -f` put back 2026-08-21, same day, operator instruction --
    # narrower than "not a deletion" would suggest: `clean -f` does delete untracked files, and
    # `reset --hard` silently discards uncommitted work the same way, so the operator drew the
    # allowed/blocked line one step tighter than the literal "except deletion" wording above.
    #
    # Both actual git ref/branch deletions -- a local forced branch delete and a remote ref
    # delete via push -- stay blocked as before.
    if base == "git":
        toks = skip_git_global_options(toks)
        sub = toks[1].lower() if len(toks) > 1 else ""
        if sub == "push":
            if has_long(seg, "delete") or any(re.match(r"^:[^\s]+$", t) for t in toks[2:]):
                return "remote ref deletion via push"
        if sub == "reset" and has_long(seg, "hard"):
            return "`git reset --hard`"
        if sub == "clean" and has_flag(seg, "f", "force"):
            return "`git clean -f`"
        if sub == "branch" and has_short(seg, "D"):
            return "forced branch deletion"
        if sub == "checkout" and "--" in toks[2:]:
            dash_idx = toks.index("--", 2)
            co_paths = toks[dash_idx + 1:]
            # `git checkout -- <named file(s)>` (or `git checkout <ref> -- <named file(s)>`,
            # the standard merge/stash-conflict resolution move) is targeted and routine --
            # the same "named target is fine, broad target is not" distinction already applied
            # to `rm` above. Only a path-less or root/home/wildcard target is still high-risk
            # enough to block outright.
            co_broad = not co_paths or any(
                re.fullmatch(
                    r"/|~|~/|\.|\./|\*|/\*|\$HOME|%USERPROFILE%|[A-Za-z]:[\\/]?|"
                    r"/(?:etc|usr|var|bin|lib|opt|home|boot|System)/?",
                    p,
                    re.I,
                )
                for p in co_paths
            )
            if co_broad:
                return "`git checkout --` with no path or a root/home/wildcard target discards worktree changes broadly"
        if sub == "restore":
            staged = has_long(seg, "staged")
            worktree = has_long(seg, "worktree")
            # `git restore path`, `--source ... path`, and `--staged --worktree` all
            # write the worktree. Pure index restoration (`--staged`, optionally with
            # --source) is deliberately allowed.
            if (not staged) or worktree:
                return "`git restore` writes/discards worktree changes"

    # Package removal / publication and dynamic execution.
    if base in {"pip", "pip3", "python", "python3"}:
        # Support both `pip uninstall` and `python -m pip uninstall`.
        joined = " ".join(t.lower() for t in toks)
        if re.search(r"(?:^|\s)pip\s+uninstall\b", joined) and (
            has_short(seg, "y") or has_long(seg, "yes")
        ):
            return "non-interactive package uninstall"
    if base in {"iex", "invoke-expression"} or re.search(r"^\S*\biex\s*\(", low):
        return "PowerShell Invoke-Expression"
    if (base == "npm" and len(toks) > 1 and toks[1].lower() == "publish") or (
        base == "twine" and len(toks) > 1 and toks[1].lower() == "upload"
    ) or (base == "cargo" and len(toks) > 1 and toks[1].lower() == "publish"):
        return "public package publication"

    # Permissions / infrastructure / host teardown.
    if base in {"chmod", "chown", "icacls", "takeown"}:
        if has_short(seg, "R") and re.search(r"\b777\b|\beveryone\b|/grant\s+everyone", low):
            return "recursive broad permission grant"
    if base == "docker" and re.search(r"\bsystem\s+prune\b|\bvolume\s+(?:rm|prune)\b", low):
        return "Docker data/volume destruction"
    if base == "kubectl" and len(toks) > 1 and toks[1].lower() == "delete":
        return "kubectl delete"
    if base == "terraform" and len(toks) > 1 and (
        toks[1].lower() == "destroy" or (toks[1].lower() == "apply" and has_long(seg, "destroy"))
    ):
        return "Terraform infrastructure destruction"
    if base in {"shutdown", "reboot", "halt", "poweroff", "stop-computer", "restart-computer"}:
        return f"host power state change `{base}`"

    # PowerShell equivalents.
    if base in {"remove-item", "ri", "del", "erase"}:
        if re.search(r"-recurse", low) and re.search(r"-force", low):
            return "PowerShell recursive force delete"
    if base in {"format-volume", "clear-disk", "remove-partition", "initialize-disk"}:
        return f"PowerShell disk operation `{base}`"
    return None


def find_hits(command: str) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    if DOWNLOAD_PIPE.search(command) or DOWNLOAD_PY_STDIN.search(command) or DOWNLOAD_SUBSTITUTION_EXEC.search(command):
        hits.append({"segment": command[:300], "reason": "download executed directly by an interpreter/shell"})
    for seg in segments(command):
        reason = classify(seg)
        if reason:
            hits.append({"segment": seg[:300], "reason": reason})
    return hits


_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][\w.-]*)\1")
_WRAPPER_WORDS = {"sh", "bash", "zsh", "ksh", "dash", "pwsh", "powershell", "powershell.exe", "cmd", "cmd.exe", "eval", "source", ".", "xargs",
                  "wsl", "wsl.exe", "ssh", "su", "runuser", "docker", "podman", "kubectl", "nsenter", "chroot"}
# A quoted span right after a -c / -e / -Command style flag is code for another shell: keep it raw.
_CODE_FLAG_RE = re.compile(r"(?:^|\s)(?:-[A-Za-z]*[ce]|--command|-Command|-EncodedCommand|/[ckCK])\s+$")


def _is_wrapper(raw: str) -> bool:
    """True when the segment runs its quoted argument as shell code (bash -c "...", eval "...", bash <<EOF)."""
    seg = raw.strip()
    while seg:
        nxt = LEADERS.sub("", PREFIXES.sub("", seg, count=1).strip(), count=1).strip()
        if nxt == seg:
            break
        seg = nxt
    if SHELL_C.match(seg) or EVAL_WRAPPER.match(seg):
        return True
    first = seg.split(None, 1)[0].lower().rsplit("/", 1)[-1].rsplit("\\", 1)[-1] if seg.split() else ""
    return first in _WRAPPER_WORDS


def sanitize(cmd: str) -> str:
    """Quote-aware view of the command: separators inside a quoted span become spaces so log text is not split into
    fake commands. A span ends at the first matching quote (no backslash escape, so "C:\\proj\\" closes). Double-quoted
    spans holding $( ) or a backtick, spans after a -c/-e/-Command flag, wrapper segments and their heredoc bodies stay
    raw; other heredoc bodies are dropped. Same rules as shell_policy_gate._sanitize. Any error returns the raw command."""
    try:
        segs: list[list] = []  # [raw, san, body, separator]
        raw: list[str] = []
        san: list[str] = []
        body: list[str] = []
        pending: list[str] = []
        i, n = 0, len(cmd)

        def flush(sep: str) -> None:
            segs.append(["".join(raw), "".join(san), "".join(body), sep])
            raw.clear(); san.clear(); body.clear()

        while i < n:
            ch = cmd[i]
            if ch == "\\" and i + 1 < n:
                raw.append(cmd[i:i + 2]); san.append(cmd[i:i + 2]); i += 2
                continue
            if ch in "'\"":
                j = cmd.find(ch, i + 1)
                if j < 0:
                    j = n
                span = cmd[i:j + 1] if j < n else cmd[i:]
                i = j + 1 if j < n else n
                keep = (ch == '"' and ("$(" in span or "`" in span)) or bool(_CODE_FLAG_RE.search("".join(raw)))
                raw.append(span); san.append(span if keep else re.sub(r"[;|&\r\n]", " ", span))
                continue
            if cmd.startswith("<<", i) and not cmd.startswith("<<<", i):
                m = _HEREDOC_RE.match(cmd, i)
                if m:
                    pending.append(m.group(2))
                    raw.append(m.group(0)); san.append(m.group(0)); i = m.end()
                    continue
            if ch == "\n" and pending:
                i += 1
                for delim in pending:
                    while i < n:
                        end = cmd.find("\n", i)
                        line = cmd[i:] if end < 0 else cmd[i:end]
                        i = n if end < 0 else end + 1
                        if line.strip() == delim:
                            break
                        body.append(line + "\n")
                pending.clear()
                flush("\n")
                continue
            m = SEPARATORS.match(cmd, i)
            if m and m.group():
                flush(m.group()); i = m.end()
                continue
            raw.append(ch); san.append(ch); i += 1
        flush("")
        out = []
        for r, s, b, sep in segs:
            out.append((r + ("\n" + b if b else "") if _is_wrapper(r) else s) + sep)
        return "".join(out)
    except Exception:  # noqa: BLE001  never weaken the gate by failing: fall back to the raw command
        return cmd


def main() -> int:
    data = json.loads(sys.stdin.read() or "{}")
    tool_input = data.get("tool_input") or {}
    command = tool_input.get("command") or tool_input.get("script") or ""
    if not isinstance(command, str) or not command.strip():
        return 0

    hits = find_hits(sanitize(command))
    if not hits:
        return 0

    log({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tool": data.get("tool_name"),
        "cwd": data.get("cwd"),
        "command": command[:600],
        "hits": hits,
        "verdict": "deny",
    })
    detail = "; ".join(f"{h['reason']} -> `{h['segment']}`" for h in hits)
    reason = (
        "Blocked by dangerous_command_gate: " + detail + ". "
        "Use a non-destructive alternative, or obtain explicit operator authorization for the exact destructive action."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        log({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "verdict": "hook-error-block",
            "traceback": traceback.format_exc(),
        })
        print("dangerous_command_gate failed internally; blocking this shell call.", file=sys.stderr)
        raise SystemExit(2)
