#!/usr/bin/env python3
"""Conservative PreToolUse shell policy for review-only subagents.

The parent session may run in ``bypassPermissions`` and that mode cannot be reduced by a
subagent. Reviewers that need Bash therefore receive a narrow command allowlist here.

This is a command-shape guard, not a sandbox: test programs, database functions, provider
CLIs, and binaries can have side effects that are not visible in the command string. The
policy prevents obvious source/git/cloud/db mutation and routes opaque commands back to the
parent/implementer.
"""
from __future__ import annotations

import json
import re
import shlex
import sys

# Only review agents that actually have Bash/PowerShell need to be in this runtime policy.
REVIEWERS = {
    "performance-optimizer",
    "database-reviewer",
    "qa-correctness-reviewer",
    "sre-platform-reviewer",
    "financial-ml-reviewer",
    "training-inspector",
    "reliability-reviewer",
}

DB_REVIEWERS = {"database-reviewer", "performance-optimizer"}
INFRA_REVIEWERS = {"sre-platform-reviewer", "performance-optimizer", "reliability-reviewer"}
PROFILING_REVIEWERS = {"performance-optimizer"}

READ_ONLY_BASES = {
    "cat", "type", "head", "tail", "wc", "ls", "dir", "pwd", "cd", "echo", "printf",
    "rg", "grep", "egrep", "fgrep", "fd", "stat", "file", "du", "df", "where", "which",
    "where.exe", "sort", "uniq", "cut", "tr", "diff", "cmp", "more", "less", "column",
    "jq", "yq", "ps", "tasklist", "whoami", "hostname", "uname", "date", "printenv",
    "nl", "tree", "basename", "dirname", "realpath", "readlink", "md5sum", "sha1sum",
    "sha256sum", "cksum", "od", "strings", "base64", "comm", "join", "paste", "fold",
}

PS_READ_ONLY = {
    "get-item", "get-childitem", "get-content", "get-location", "get-process", "get-service",
    "get-command", "get-filehash", "get-member", "select-string", "select-object",
    "measure-object", "compare-object", "resolve-path", "test-path", "format-list",
    "format-table", "write-output", "write-host",
}

VERSION_SAFE_BASES = {
    "git", "python", "python3", "python.exe", "py", "py.exe", "node", "npm", "pnpm", "yarn",
    "pytest", "ruff", "mypy", "pyright", "pylint", "flake8", "flutter", "dart", "cargo",
    "rustc", "go", "dotnet", "docker", "kubectl", "terraform", "psql", "sqlite3", "java",
    "javac", "gcc", "clang", "make", "cmake", "gradle", "mvn", "gcloud", "bq", "jq", "rg",
}


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return []


def _base(tokens: list[str]) -> str:
    if not tokens:
        return ""
    return tokens[0].lower().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]


def _outside_quotes(command: str) -> str:
    """Replace quoted spans with spaces so only shell-active operators are inspected."""
    out: list[str] = []
    quote: str | None = None
    escaped = False
    for ch in command:
        if escaped:
            out.append(" ")
            escaped = False
            continue
        if ch == "\\" and quote != "'":
            out.append(" ")
            escaped = True
            continue
        if quote:
            out.append(" ")
            if ch == quote:
                quote = None
            continue
        if ch in {"'", '"'}:
            quote = ch
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def _split_unquoted_pipes(command: str, bare: str) -> list[str]:
    positions = [m.start() for m in re.finditer(r"(?<!\|)\|(?!\|)", bare)]
    if not positions:
        return [command]
    parts: list[str] = []
    start = 0
    for pos in positions:
        parts.append(command[start:pos].strip())
        start = pos + 1
    parts.append(command[start:].strip())
    return parts


def _has_shell_control(command: str) -> bool:
    bare = _outside_quotes(command)
    # A single '&' also backgrounds/invokes; do not allow it as a reviewer escape hatch.
    if re.search(r"&&|\|\||;|\n|\$\(|[<>]\(|`|(?<!&)\&(?!&)", bare):
        return True
    # Redirection is an intentional write/read-from-file channel. Reviewers do not need it.
    if ">" in bare or "<" in bare:
        return True
    return False


def _version_or_help(tokens: list[str]) -> bool:
    # Exactly one benign argument. Do not let --help launder --eval/-c/script execution.
    return (
        len(tokens) == 2
        and _base(tokens) in VERSION_SAFE_BASES
        and tokens[1].lower() in {"--version", "-version", "version", "--help", "-h", "help", "/?"}
    )


def _git_read_only(tokens: list[str]) -> bool:
    if len(tokens) < 2:
        return False
    low = [t.lower() for t in tokens]
    sub = low[1]
    # Several read commands can intentionally write output files.
    if any(t == "--output" or t.startswith("--output=") for t in low[2:]):
        return False
    if sub in {
        "status", "diff", "log", "show", "rev-parse", "ls-files", "ls-tree", "grep",
        "blame", "shortlog", "describe", "cat-file", "for-each-ref", "name-rev", "merge-base",
    }:
        return True
    if sub == "remote":
        return len(low) >= 3 and low[2] in {"-v", "show", "get-url"}
    if sub == "worktree":
        return len(low) >= 3 and low[2] == "list"
    if sub == "config":
        return len(low) >= 3 and low[2] in {"--get", "--get-all", "--get-regexp", "--list", "-l"}
    if sub == "branch":
        rest = low[2:]
        if not rest:
            return True
        value_flags = {"--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--sort", "--format"}
        simple_flags = {"-a", "--all", "-r", "--remotes", "-v", "-vv", "--list", "--show-current", "--color", "--no-color"}
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok in simple_flags:
                i += 1
                continue
            if tok in value_flags:
                if i + 1 >= len(rest):
                    return False
                i += 2
                continue
            if any(tok.startswith(f + "=") for f in value_flags):
                i += 1
                continue
            return False
        return True
    if sub == "tag":
        rest = low[2:]
        return not rest or all(t in {"-l", "--list", "-n", "--sort"} or t.startswith(("--sort=", "--format=")) for t in rest)
    return False


def _text_read_only(tokens: list[str]) -> bool:
    base = _base(tokens)
    if base == "find":
        bad = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}
        return not any(t.lower() in bad for t in tokens[1:])
    if base not in READ_ONLY_BASES:
        return False
    low = [t.lower() for t in tokens[1:]]
    if base == "sort" and any(t == "-o" or t.startswith("--output=") for t in low):
        return False
    if base == "diff" and any(t == "--output" or t.startswith("--output=") for t in low):
        return False
    if base == "yq" and any(t in {"-i", "--inplace", "--in-place"} for t in low):
        return False
    if base == "base64" and any(t == "-o" or t.startswith("--output=") for t in low):
        return False
    return True


def _test_or_static_check(tokens: list[str]) -> bool:
    if not tokens:
        return False
    base = _base(tokens)
    low = [t.lower() for t in tokens]
    mutating = {
        "--fix", "--fix-only", "--unsafe-fixes", "--write", "-w", "--in-place", "-i",
        "--update-goldens", "--update-snapshot", "-u", "--snapshot-update", "--install-types",
        "--apply", "--autofix", "--basetemp", "--junitxml", "--junit-xml", "--html",
    }
    if any(t in mutating or any(t.startswith(x + "=") for x in mutating if x.startswith("--")) for t in low[1:]):
        return False
    if any(t.startswith("--cov-report=") and not t.endswith(("term", "term-missing")) for t in low[1:]):
        return False
    if base == "ruff":
        if len(low) >= 2 and low[1] == "format":
            return "--check" in low or "--diff" in low
        return True
    if base in {"pytest", "mypy", "pyright", "pylint", "flake8"}:
        return True
    if base in {"python", "python3", "python.exe", "py", "py.exe"}:
        # `compileall` writes pyc files, so it is intentionally excluded.
        joined = " ".join(low[1:])
        return bool(re.search(r"(?:^|\s)-m\s+(?:pytest|unittest|mypy|ruff)\b", joined))
    if base in {"npm", "pnpm", "yarn"}:
        if len(low) >= 2 and low[1] == "test":
            return True
        return len(low) >= 3 and low[1] == "run" and bool(
            re.fullmatch(r"(?:test|lint|check|typecheck|type-check|verify)(?::[\w.-]+)?", low[2])
        )
    if base in {"flutter", "dart"} and len(low) >= 2:
        return low[1] in {"test", "analyze"}
    if base == "cargo" and len(low) >= 2:
        if low[1] == "fmt":
            return "--check" in low
        return low[1] in {"test", "check", "clippy"}
    if base == "go" and len(low) >= 2:
        return low[1] in {"test", "vet"}
    if base == "dotnet" and len(low) >= 2:
        return low[1] in {"test", "build"}
    return False


def _sql_has_mutation(sql: str) -> bool:
    return bool(re.search(
        r"\b(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|DROP|ALTER|CREATE|GRANT|REVOKE|COPY|VACUUM|CALL|DO|REINDEX|CLUSTER)\b",
        sql,
        re.I,
    ))


def _sql_read_only(tokens: list[str]) -> bool:
    base = _base(tokens)
    if base not in {"psql", "sqlite3"}:
        return False
    if base == "psql":
        if any(t in {"-f", "--file"} or t.startswith("--file=") for t in tokens[1:]):
            return False
        cmds: list[str] = []
        i = 1
        while i < len(tokens):
            t = tokens[i]
            if t in {"-c", "--command"}:
                if i + 1 >= len(tokens):
                    return False
                cmds.append(tokens[i + 1])
                i += 2
                continue
            if t.startswith("--command="):
                cmds.append(t.split("=", 1)[1])
            i += 1
        if len(cmds) != 1:
            return False
        sql = cmds[0].strip()
        if _sql_has_mutation(sql):
            return False
        if re.match(r"^EXPLAIN\b", sql, re.I):
            return not bool(re.search(r"\bANALYZE\b", sql, re.I))
        if re.match(r"^(SHOW\b|\\d(?:\w*)?\b)", sql, re.I):
            return True
        # SELECT/WITH can call volatile functions. Require an explicit read-only transaction;
        # this is stronger but still not a proof against external side effects in extensions.
        if re.match(r"^BEGIN\s+(?:TRANSACTION\s+)?READ\s+ONLY\s*;", sql, re.I):
            body = re.sub(r"^BEGIN\s+(?:TRANSACTION\s+)?READ\s+ONLY\s*;", "", sql, count=1, flags=re.I).strip()
            body = re.sub(r";\s*(?:ROLLBACK|COMMIT)\s*;?\s*$", "", body, flags=re.I).strip()
            return bool(re.match(r"^(SELECT|WITH)\b", body, re.I)) and not _sql_has_mutation(body)
        return False
    # sqlite3: one inline statement only; no dot-command script loading or mutation.
    if len(tokens) < 3:
        return False
    sql = tokens[-1].strip()
    return not _sql_has_mutation(sql) and bool(re.match(r"^(SELECT|WITH|EXPLAIN|PRAGMA\s+(?!.*=))\b", sql, re.I))


def _profiler_read_only(tokens: list[str]) -> bool:
    base = _base(tokens)
    low = [t.lower() for t in tokens]
    if base in {"py-spy", "hyperfine"}:
        return not any(t in {"-o", "--output"} or t.startswith("--output=") for t in low[1:])
    if base == "perf" and len(low) >= 2:
        return low[1] in {"stat", "report", "list"} and "-o" not in low and "--output" not in low
    if base in {"python", "python3", "python.exe", "py", "py.exe"}:
        joined = " ".join(low[1:])
        return bool(re.search(r"(?:^|\s)-m\s+(?:cprofile|profile|timeit|tracemalloc)\b", joined)) and "-o" not in low
    return False


def _infra_read_only(tokens: list[str]) -> bool:
    base = _base(tokens)
    low = [t.lower() for t in tokens]
    if base == "docker" and len(low) >= 2:
        return low[1] in {"ps", "logs", "inspect", "stats", "info", "version"} or low[1:3] == ["system", "df"]
    if base == "kubectl" and len(low) >= 2:
        return low[1] in {"get", "describe", "logs", "explain", "api-resources", "api-versions", "version", "cluster-info"}
    if base == "terraform" and len(low) >= 2:
        if any(t == "-out" or t.startswith("-out=") or t.startswith("-generate-config-out=") for t in low[2:]):
            return False
        return low[1] in {"plan", "show", "output", "validate", "version", "providers"} or low[1:3] == ["state", "list"]
    if base == "gcloud":
        words = [t for t in low[1:] if not t.startswith("-")]
        mutating = {"deploy", "delete", "create", "update", "set", "add", "remove", "replace",
                    "apply", "import", "restore", "enable", "disable", "grant", "revoke",
                    "submit", "call", "kill", "patch"}
        if any(w in mutating for w in words):
            return False
        return any(w in {"list", "describe", "read", "get-iam-policy"} for w in words)
    if base == "bq" and len(low) >= 2:
        if low[1] in {"show", "ls", "head"}:
            return True
        if low[1] == "query":
            sql = tokens[-1] if len(tokens) >= 3 else ""
            return bool(re.match(r"^(SELECT|WITH|EXPLAIN)\b", sql.strip(), re.I)) and not _sql_has_mutation(sql)
    return False


def allowed(command: str, tool_name: str, agent_type: str | None = None) -> bool:
    command = (command or "").strip()
    if not command:
        return True
    if _has_shell_control(command):
        return False
    bare = _outside_quotes(command)
    pipe_parts = _split_unquoted_pipes(command, bare)
    if len(pipe_parts) > 1:
        # Pipes are allowed only when every stage independently satisfies the same role policy.
        return all(allowed(part, tool_name, agent_type) for part in pipe_parts)

    tokens = _tokens(command)
    if not tokens:
        return False
    base = _base(tokens)
    role = agent_type or ""

    if _version_or_help(tokens):
        return True
    if base == "git":
        return _git_read_only(tokens)
    if base == "find" or base in READ_ONLY_BASES:
        return _text_read_only(tokens)
    if tool_name.lower() == "powershell" and base in PS_READ_ONLY:
        return True
    if _test_or_static_check(tokens):
        return True
    if role in DB_REVIEWERS and _sql_read_only(tokens):
        return True
    if role in PROFILING_REVIEWERS and _profiler_read_only(tokens):
        return True
    if role in INFRA_REVIEWERS and _infra_read_only(tokens):
        return True
    return False


def main() -> int:
    data = json.loads(sys.stdin.read() or "{}")
    agent_type = str(data.get("agent_type") or "")
    if agent_type not in REVIEWERS:
        return 0
    tool_name = str(data.get("tool_name") or "")
    if tool_name not in {"Bash", "PowerShell"}:
        return 0
    ti = data.get("tool_input") or {}
    command = str(ti.get("command") or ti.get("script") or "")
    if allowed(command, tool_name, agent_type):
        return 0
    reason = (
        f"Review-only agent `{agent_type}` may use {tool_name} only for role-relevant inspection, "
        "tests/static checks, and narrowly allowed read-only database/infra/profiling commands. "
        "This command is outside that policy; hand it to the parent/implementer if it is required."
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
    raise SystemExit(main())
