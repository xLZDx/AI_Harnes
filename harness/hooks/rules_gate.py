#!/usr/bin/env python3
"""Operator rules gate (recovery plan R1 and R2): PreToolUse on the shell tool of Claude Code (Bash, PowerShell) and
Codex (Bash). One mechanism for every project and both clients.

R1, operator rule 4 (tests only at sprint end, by test agents; no lint, typecheck, verify, CI):
  deny   full test suites (`npm test`, `npm run test`, `pnpm|yarn test`, bare `vitest`/`jest`, bare `pytest`,
         `python -m pytest`, bare `python -m unittest`) without a named test file;
         lint (npm run lint, eslint, ruff, flake8, pylint, biome), typecheck (tsc --noEmit, npm run typecheck, mypy,
         pyright), any `verify` script; `gh workflow run|enable`, `gh run rerun|watch`, `gh run view --log*`,
         `gh pr checks`.
  A test command that names explicit test files is allowed only for a test agent (functional-test-reviewer,
  tdd-guide, e2e-runner) or, in the main session, as a plain `python <test script>` run by path.
R2: `git push` and `gh pr create` are allowed only when the ledger approval is bound to the current HEAD and every task
  of the active sprint is verified or done (or explicitly blocked). A repo without a harness ledger is allowed (logged).

Fails open on any hook error (logged to <state>/rules_gate/gate.log); never prints the command text into the log.
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

TEST_AGENTS = {"functional-test-reviewer", "tdd-guide", "e2e-runner"}
RULE4 = "Operator rule 4 (tests run only at sprint end, written and run by test agents; no full suites, lint, typecheck, verify scripts or CI)"
ALTERNATIVE = ("Allowed instead: ask functional-test-reviewer, tdd-guide or e2e-runner to run the task's own named test "
               "files (for example `pytest tests/test_x.py`, `npx vitest run path/to/file.test.ts`); the main session "
               "may run a plain `python -I tests/test_x.py`.")
FILE_RE = re.compile(r"\.(py|[cm]?[jt]sx?|dart|go|rs|java|kt|cs|rb|php)(::|:\d+|$)", re.I)
VERIFY_FILE_RE = re.compile(r"^verify([_.-][\w.-]*)?\.(py|sh|ps1|js|mjs|cjs|bat|cmd)$", re.I)
PY_BASES = {"python", "python3", "py", "pypy", "pypy3"}
SHELLS = {"bash", "sh", "zsh", "dash", "pwsh", "powershell", "cmd"}
WRAPPERS = {"env", "time", "call", "nohup", "sudo", "doas", "command", "exec", "start-process", "poetry", "pipenv", "uv",
            "uvx", "pipx", "hatch", "rye", "conda", "npx", "pnpx", "bunx", "timeout", "nice", "ionice", "stdbuf", "xargs",
            "cross-env", "dotenv", "dotenvx", "setsid", "unbuffer", "winpty", "chronic"}
PKG = {"npm", "pnpm", "yarn", "bun"}
# flags that take the NEXT token as their value, per wrapper
WRAP_VFLAGS = {
    "env": {"-u", "--unset", "-C", "--chdir"},
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U", "--user", "--group", "--host", "--prompt", "--chdir"},
    "doas": {"-u", "-C"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "-P", "-u", "--class", "--classdata"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    "xargs": {"-n", "-I", "-L", "-P", "-d", "-E", "-s", "-a", "--max-args", "--max-procs", "--delimiter", "--arg-file"},
    "uv": {"--with", "--with-requirements", "--with-editable", "--python", "-p", "--project", "--directory", "--extra",
           "--group", "--env-file", "--index", "--default-index", "--from", "-w", "--package", "--only-group"},
    "uvx": {"--with", "--python", "-p", "--from", "--with-requirements", "--env-file", "--index"},
    "poetry": {"-C", "--directory", "-P", "--project"},
    "pipx": {"--python", "--spec", "--pip-args"},
    "dotenv": {"-e", "-f", "-p", "-c", "-o"},
    "dotenvx": {"-f", "--env-file", "-fk", "--env-keys-file"},
    "start-process": {"-filepath", "-workingdirectory", "-argumentlist"},
}
PKG_VFLAGS = {"-C", "--prefix", "--cwd", "--filter", "-F", "-w", "--workspace", "--include", "--exclude", "-p", "--package",
              "--loglevel", "--registry", "--userconfig", "--cache", "--reporter", "--cwd"}
# flags of test runners that take the next token as a value (a config / plugin / setup file is not a named test file)
TEST_VFLAGS = {"-c", "--config", "--configLoader", "-p", "--plugin", "--rootdir", "--root", "--confcutdir", "--setupFiles",
               "--setupFilesAfterEnv", "--setup-files", "--setupFile", "--globalSetup", "--project", "--projects", "-o",
               "--override-ini", "--ignore", "--ignore-glob", "--deselect", "-k", "-m", "--cov", "--cov-report",
               "--cov-config", "--basetemp", "--junitxml", "--log-file", "--log-level", "-n", "--numprocesses", "--workers",
               "-w", "--maxWorkers", "--testTimeout", "--environment", "--outputFile", "--output-file", "--testPathIgnorePatterns",
               "--testNamePattern", "-t", "--testPathPattern", "--shard", "--dir", "--browser", "--pool", "--mode",
               "--cache-dir", "--cacheDirectory", "--import", "--require", "-r", "--reporter", "--reporters", "--tsconfig",
               "--tb", "--durations", "--maxfail", "-W", "--rootDir", "--roots", "--env", "--transform", "--moduleNameMapper",
               "--coverage-dir", "--coverageDirectory", "--changed", "--dom", "--watch-ignore", "--logHeapUsage"}
LEAD_KW = {"then", "do", "else", "elif", "if", "while", "until", "!", "{", "(", "}", ")", "fi", "done", "esac", ";;"}
TEST_DIRS = {"tests", "__tests__"}
VAR_ASSIGN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
HEREDOC_RE = re.compile(r"(?<!<)<<(?!<)(-?)\s*(?:\"([^\"]+)\"|'([^']+)'|\\?([A-Za-z_][\w.-]*))")


class Cwd(str):
    """A working directory; `.given` is True when it came from a cd / git -C target in the command (not the session cwd)."""
    given = False


def mk_cwd(path: str, given: bool) -> Cwd:
    c = Cwd(path)
    c.given = given
    return c


# ---- parsing -----------------------------------------------------------------------------------------------------

def _in_quote(line: str, pos: int) -> bool:
    q = ""
    for ch in line[:pos]:
        if q:
            if ch == q:
                q = ""
        elif ch in "\"'":
            q = ch
    return bool(q)


def strip_heredocs(cmd: str) -> str:
    """Heredoc bodies are data: drop them through the terminator line. Keep the body (as ordinary lines, to be analysed)
    only when the heredoc feeds a shell or python interpreter."""
    if "<<" not in cmd:
        return cmd
    lines = cmd.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        out.append(line)
        ms = [m for m in HEREDOC_RE.finditer(line) if not _in_quote(line, m.start())]
        if not ms:
            continue
        pre = split_segments(line[:ms[0].start()])
        last = tokenize(pre[-1]) if pre else []
        interp = any(base(t) in SHELLS or base(t) in PY_BASES for t in last[:4])
        py_body = not any(base(t) in SHELLS for t in last[:4])  # a python body has no shell substitution
        for m in ms:
            tag = m.group(2) or m.group(3) or m.group(4)
            body: list[str] = []
            while i < len(lines):
                l = lines[i]
                i += 1
                if l.strip() == tag:
                    break
                body.append(l)
            if interp:
                # python source keeps its plain lines (a shell-like `npm test` line is still read), but `$(` and
                # backticks there are Python text, not shell substitution, so they are neutralised
                out.extend([b.replace("$(", "$ (").replace("`", "'") for b in body] if py_body else body)
    return "\n".join(out)


def clean_tokens(toks: list[str]) -> list[str]:
    """Drop grouping characters and shell keywords around a command: (cmd), { cmd; }, then cmd, do cmd, ! cmd."""
    t = list(toks)
    while t:
        s = t[0].lstrip("({")
        if s != t[0]:
            if s:
                t[0] = s
            else:
                t.pop(0)
            continue
        if t[0].lower() in LEAD_KW:
            t.pop(0)
            continue
        break
    while t:
        e = t[-1].rstrip(")}")
        if e == t[-1]:
            break
        if e:
            t[-1] = e
            break
        t.pop()
    return t


def split_segments(cmd: str) -> list[str]:
    """Split on unquoted ; & | and newlines (a '&' inside a redirection such as 2>&1 does not split)."""
    out, cur, q = [], [], ""
    for i, ch in enumerate(cmd):
        if q:
            cur.append(ch)
            if ch == q:
                q = ""
            continue
        if ch in "\"'":
            q = ch
            cur.append(ch)
        elif ch in ";|\n" or (ch == "&" and not (i > 0 and cmd[i - 1] == ">") and not (i + 1 < len(cmd) and cmd[i + 1] == ">")):
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [s.strip() for s in out if s.strip()]


def tokenize(seg: str) -> list[str]:
    toks = re.findall(r"""(?:"[^"]*"|'[^']*'|[^\s"'])+""", seg)
    return [re.sub(r"""["']""", "", t) if not re.search(r"\s", t) else t.strip("\"'") for t in toks]


def base(tok: str) -> str:
    b = tok.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return re.sub(r"\.(exe|cmd|bat|ps1)$", "", b)


def is_flag(t: str) -> bool:
    return t.startswith("-") and len(t) > 1


def looks_like_test(a: str) -> bool:
    """A path that names a test file: test_*.py, *_test.*, *.test.*, *.spec.*, or a file under tests/ or __tests__/."""
    if not FILE_RE.search(a):
        return False
    p = re.sub(r"(::.*|:\d+)$", "", a.replace("\\", "/"))
    parts = p.split("/")
    name = parts[-1].lower()
    if re.match(r"test_[^/]*\.\w+$", name) or re.search(r"_test\.\w+$", name) or re.search(r"\.(test|spec)\.\w+$", name):
        return True
    return any(d.lower() in TEST_DIRS for d in parts[:-1])


def has_file(args: list[str]) -> bool:
    """True when a test-looking path appears that is not the value of a flag (a config / plugin / setup file is no test)."""
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a == "--":
            continue
        if is_flag(a):
            if "=" not in a and a in TEST_VFLAGS:
                skip = True
            continue
        if looks_like_test(a):
            return True
    return False


def skip_flags(args: list[str], vflags: set) -> list[str]:
    r = list(args)
    while r:
        a = r[0]
        if a == "--":
            r = r[1:]
        elif is_flag(a):
            r = r[2:] if ("=" not in a and a in vflags) else r[1:]
        else:
            break
    return r


def strip_wrapper(b: str, rest: list[str]) -> list[str]:
    """The wrapped command of env / timeout / nice / sudo -u x / uv run --with p / cross-env VAR=1 / dotenv -- ..."""
    r = list(rest)
    if b in ("poetry", "pipenv", "uv", "hatch", "rye", "conda", "pipx"):
        if b == "uv" and r[:2] == ["tool", "run"]:
            r = r[2:]
        elif r and r[0] in ("run", "exec"):
            r = r[1:]
    vf = WRAP_VFLAGS.get(b, set())
    duration = b != "timeout"
    while r:
        a = r[0]
        if a == "--":
            r = r[1:]
        elif is_flag(a):
            if b == "command" and a.lower() in ("-v", "-V"):
                return []
            r = r[2:] if ("=" not in a and a in vf) else r[1:]
        elif VAR_ASSIGN_RE.fullmatch(a):
            r = r[1:]
        elif not duration and re.fullmatch(r"\d+(\.\d+)?[smhd]?", a):
            duration = True
            r = r[1:]
        else:
            break
    return r


# ---- directories (Git Bash, ~, $HOME) -----------------------------------------------------------------------------

def msys_path(s: str) -> str:
    s = s.strip().strip("\"'")
    m = re.match(r"^/(?:mnt/|cygdrive/)?([A-Za-z])(?:/(.*))?$", s)  # Git Bash /d/Repo -> D:/Repo
    return f"{m.group(1).upper()}:/{m.group(2) or ''}" if m else s


def resolve_dir(target: str, cur: str) -> Cwd:
    """The directory a cd / git -C target points to. A target that cannot be expanded keeps its raw text (it will not be a
    directory, so the push check treats it as an unresolved target)."""
    t = target.strip().strip("\"'")
    home = os.path.expanduser("~")
    t = re.sub(r"\$\{?(?:env:)?(?:HOME|USERPROFILE)\}?|%USERPROFILE%", lambda m: home.replace("\\", "/"), t, flags=re.I)
    t = re.sub(r"\$\{?(?:PWD)\}?", str(cur).replace("\\", "/"), t)
    if t == "~" or t.startswith(("~/", "~\\")):
        t = home + t[1:]
    if re.search(r"[$`%]", t) or t == "-":
        return mk_cwd(t, True)  # cannot be resolved
    t = msys_path(t)
    return mk_cwd(os.path.normpath(os.path.join(str(cur), t)), True)


def script_kind(name: str) -> str:
    n = name.lower()
    if re.fullmatch(r"verify([:_.-].*)?", n):
        return "verify"
    if re.fullmatch(r"(lint|eslint|lint[:_.-].*|.*[:_.-]lint)", n):
        return "lint"
    if re.fullmatch(r"(typecheck|type-check|types?|tsc|check-types|tsc[:_.-].*|typecheck[:_.-].*|type-check[:_.-].*)", n):
        return "typecheck"
    if re.fullmatch(r"(test|tests|test[:_.-].*|(unit|e2e|integration)[:_-]?test.*)", n):
        return "test"
    return ""


# ---- classification: each finding is (kind, detail, cwd) ----------------------------------------------------------

def classify(tokens: list[str], cwd: str, depth: int = 0) -> list[tuple]:
    """Findings for one command (already split). kinds: full-test, named-test, py-script-test, lint, typecheck, verify,
    ci, push, pr-create."""
    if depth > 3:
        return [("full-test", "command nested deeper than 3 levels (substitution or shell -c) cannot be analysed; split it into plain commands", cwd)]  # fail closed: too deep to read is not allowed
    toks = list(tokens)
    while toks and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", toks[0]):
        toks.pop(0)  # leading VAR=value
    while toks and toks[0] in ("&", "&&", "call", "."):
        toks.pop(0)
    if not toks:
        return []
    b = base(toks[0])
    rest = toks[1:]

    if b in SHELLS:  # bash -c "npm test", powershell -Command "..."
        for i, a in enumerate(rest):
            if a.lower() in ("-c", "-lc", "-command", "-c", "/c", "/k", "-cmd") and i + 1 < len(rest):
                return analyze(" ".join(rest[i + 1:]), cwd, depth + 1)
        return []
    if b in WRAPPERS and b not in ("npx", "pnpx", "bunx"):
        return classify(strip_wrapper(b, rest), cwd, depth + 1)
    if b in ("npx", "pnpx", "bunx"):
        return classify(skip_flags(rest, {"-p", "--package", "-c", "--call"}), cwd, depth + 1)

    if b == "gh":
        subs, skip = [], False
        for a in rest:
            if skip:
                skip = False
            elif a in ("-R", "--repo"):
                skip = True
            elif not is_flag(a):
                subs.append(a.lower())
        pair = tuple(subs[:2])
        if pair in (("workflow", "run"), ("workflow", "enable"), ("run", "rerun"), ("run", "watch"), ("pr", "checks")):
            return [("ci", "gh " + " ".join(pair), cwd)]
        if pair == ("run", "view") and any(a.lower().startswith("--log") for a in rest):
            return [("ci", "gh run view --log", cwd)]
        if pair == ("pr", "create"):
            return [("pr-create", "gh pr create", cwd)]
        return []

    if b == "git":
        sub, i, gcwd = "", 0, cwd
        while i < len(rest):
            a = rest[i]
            if a == "-C" and i + 1 < len(rest):
                gcwd = resolve_dir(rest[i + 1], gcwd)
                i += 2
            elif a in ("-c", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(rest):
                i += 2
            elif is_flag(a):
                i += 1
            else:
                sub = a.lower()
                i += 1
                break
        if sub == "push":
            args = rest[i:]
            if any(a in ("--dry-run", "-n") for a in args):
                return []
            return [("push", "git push", gcwd)]
        return []

    if b in PKG:
        args = skip_flags(rest, PKG_VFLAGS)
        if not args:
            return []
        sub, after = args[0].lower(), args[1:]
        if sub in ("test", "t", "tst"):
            return [("named-test" if has_file(after) else "full-test", f"{b} test", cwd)]
        if sub in ("run", "run-script", "run-s", "run-p"):
            after = skip_flags(after, PKG_VFLAGS)
            if not after:
                return []
            script = after[0]
            k = script_kind(script)
            if k == "test":
                return [("named-test" if has_file(after[1:]) else "full-test", f"{b} run {script}", cwd)]
            if k:
                return [(k, f"{b} run {script}", cwd)]
            return []
        if sub in ("exec", "dlx", "x"):
            return classify(skip_flags(after, PKG_VFLAGS), cwd, depth + 1)
        if sub == "workspace" and after:  # yarn workspace <name> <command...>
            return classify([b, *after[1:]], cwd, depth + 1)
        if sub == "workspaces":  # yarn workspaces foreach [flags] run test
            a2 = skip_flags(after, PKG_VFLAGS)
            if a2 and a2[0].lower() == "foreach":
                a2 = skip_flags(a2[1:], PKG_VFLAGS)
            return classify([b, *a2], cwd, depth + 1) if a2 else []
        k = script_kind(sub)
        if k == "test":
            return [("named-test" if has_file(after) else "full-test", f"{b} {sub}", cwd)]
        if k:
            return [(k, f"{b} {sub}", cwd)]
        return classify(args, cwd, depth + 1) if b in ("pnpm", "yarn", "bun") else []

    # python family: python -m mod ..., python script.py
    if b in PY_BASES:
        args = list(rest)
        mod, margs = "", []
        for j, a in enumerate(args):
            if a == "-m" and j + 1 < len(args):
                mod, margs = args[j + 1], args[j + 2:]
                break
        if mod:
            return classify([mod, *margs], cwd, depth + 1) if base(mod) in {
                "pytest", "unittest", "ruff", "flake8", "pylint", "mypy", "pyright", "black", "tsc"} else []
        script = next((a for a in args if not is_flag(a)), "")
        if script:
            if VERIFY_FILE_RE.match(script.replace("\\", "/").rsplit("/", 1)[-1]):
                return [("verify", "verify script " + script.replace("\\", "/").rsplit("/", 1)[-1], cwd)]
            p = script.replace("\\", "/").lower()
            if p.endswith(".py") and (re.search(r"(^|/)test_[^/]*\.py$|_test\.py$", p) or re.search(r"(^|/)tests?/", p)):
                return [("py-script-test", "python test script", cwd)]
        return []

    if b in ("pytest", "py.test"):
        return [("named-test" if has_file(rest) else "full-test", "pytest", cwd)]
    if b == "unittest":
        names = [a for a in rest if not is_flag(a) and a.lower() != "discover"]
        return [("named-test" if names and "discover" not in [a.lower() for a in rest] else "full-test", "unittest", cwd)]
    if b in ("vitest", "jest"):
        return [("named-test" if has_file(rest) else "full-test", b, cwd)]
    if b in ("flutter", "dart", "go", "cargo", "playwright", "dotnet", "mvn", "mvnw", "gradle", "gradlew"):
        r = classify_runner(b, rest, cwd)
        if r:
            return r
    if b in ("eslint", "eslint_d", "ruff", "flake8", "pylint", "biome"):
        return [("lint", b, cwd)]
    if b in ("mypy", "pyright", "basedpyright"):
        return [("typecheck", b, cwd)]
    if b == "tsc" and any(a.lower() in ("--noemit", "-noemit") for a in rest):
        return [("typecheck", "tsc --noEmit", cwd)]
    if VERIFY_FILE_RE.match(toks[0].replace("\\", "/").rsplit("/", 1)[-1]):
        return [("verify", "verify script " + toks[0].replace("\\", "/").rsplit("/", 1)[-1], cwd)]
    return []


def classify_runner(b: str, rest: list[str], cwd: str) -> list[tuple]:
    """Other test runners (flutter, dart, go, cargo, playwright, dotnet, mvn, gradle): full-test unless a test file is named."""
    args = [a for a in rest if not a.startswith("+")]  # cargo +nightly test
    plain = [a for a in args if not is_flag(a)]
    sub = plain[0].lower() if plain else ""
    ext_named = lambda ext: has_file(args) or any(not is_flag(a) and a.lower().endswith(ext) for a in args)  # noqa: E731
    if b in ("flutter", "dart") and sub == "test":
        return [("named-test" if ext_named(".dart") else "full-test", f"{b} test", cwd)]
    if b == "go" and sub == "test":
        named = any(not is_flag(a) and a.lower().endswith("_test.go") for a in args)
        return [("named-test" if named else "full-test", "go test", cwd)]
    if b == "cargo" and sub == "test":
        named = any(a == "--test" or a.startswith("--test=") for a in args)
        return [("named-test" if named else "full-test", "cargo test", cwd)]
    if b == "playwright" and sub == "test":
        return [("named-test" if has_file(args[args.index(plain[0]) + 1:]) else "full-test", "playwright test", cwd)]
    if b == "dotnet" and sub == "test":
        return [("full-test", "dotnet test", cwd)]
    if b in ("mvn", "mvnw") and "test" in [a.lower() for a in plain]:
        return [("full-test", f"{b} test", cwd)]
    if b in ("gradle", "gradlew") and any(a.lower() == "test" or a.lower().endswith(":test") for a in plain):
        return [("full-test", f"{b} test", cwd)]
    return []


def extract_subst(cmd: str) -> tuple[str, list[str]]:
    """Command substitutions $(...) and `...` (active outside single quotes, also inside double quotes): return the command
    with each replaced by a placeholder, and the inner command texts to analyse on their own."""
    out: list[str] = []
    inners: list[str] = []
    i, n, sq, dq = 0, len(cmd), False, False
    while i < n:
        ch = cmd[i]
        if ch == "\\" and not sq and i + 1 < n:
            out.append(cmd[i:i + 2])
            i += 2
            continue
        if ch == "'" and not dq:
            sq = not sq
        elif ch == '"' and not sq:
            dq = not dq
        elif not sq and ch == "$" and cmd.startswith("$(", i):
            depth, j = 1, i + 2
            while j < n and depth:
                depth += (cmd[j] == "(") - (cmd[j] == ")")
                j += 1
            inners.append(cmd[i + 2:j - 1 if depth == 0 else j])
            out.append("x")
            i = j
            continue
        elif not sq and ch == "`":
            j = i + 1
            while j < n and cmd[j] != "`":
                j += 2 if cmd[j] == "\\" else 1
            inners.append(cmd[i + 1:j])
            out.append("x")
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out), inners


def strip_case(toks: list[str]) -> list[str]:
    """`case $a in x) cmd` and later branches `y) cmd`: drop the case head / pattern so cmd is classified."""
    if toks and toks[0].lower() == "case":
        for k, t in enumerate(toks):
            if k >= 2 and t.endswith(")") and "in" in [x.lower() for x in toks[:k]]:
                return toks[k + 1:]
        return []
    if toks and len(toks) > 1 and re.fullmatch(r"[^()\s]*\)", toks[0]):
        return toks[1:]
    return toks


def analyze(command: str, cwd: str, depth: int = 0) -> list[tuple]:
    if depth > 3:
        return [("full-test", "command nested deeper than 3 levels (substitution or shell -c) cannot be analysed; split it into plain commands", cwd)]
    found: list[tuple] = []
    cur = cwd
    outer, inners = extract_subst(strip_heredocs(command))
    for inner in inners:
        found.extend(analyze(inner, cwd, depth + 1))
    for seg in split_segments(outer):
        toks = clean_tokens(tokenize(seg))
        toks = clean_tokens(strip_case(toks)) if toks else toks
        if not toks:
            continue
        b = base(toks[0])
        if b in ("cd", "pushd", "set-location", "sl", "chdir") and len(toks) > 1:
            vals = [t for t in toks[1:] if not is_flag(t)]
            if len(vals) > 1 and vals[0].lower() == "/d":  # cmd.exe `cd /d D:\x`
                vals = vals[1:]
            if vals:
                cur = resolve_dir(vals[0], cur)
            continue
        found.extend(classify(toks, cur, depth))
    return found


# ---- R2: ledger ----------------------------------------------------------------------------------------------------

MISSING_LEDGER = "harness installed but ledger missing"


def find_ledger(cwd: str):
    """(ledger_path, why) using the same discovery as the project pre-push hook; ledger_path None with why
    'no harness tools in the repository' = no harness (allowed); None with MISSING_LEDGER = installed, ledger absent (deny)."""
    import _agentio
    top = _agentio.run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
    com = _agentio.run_git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=cwd)
    if top.returncode != 0 or com.returncode != 0:
        return None, "not a git repository"
    top_p, common_dir = Path(top.stdout.strip()), Path(com.stdout.strip())
    common = common_dir.parent if common_dir.name == ".git" else common_dir
    installed = (common / ".harness-tools" / "ledger.py").is_file() or (common / ".harness").is_dir()
    if not installed:
        lp = top_p / "tools" / "ledger.py"
        try:
            installed = "single source of sprint and task state" in lp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            installed = False
    if not installed:
        hp = _agentio.run_git(["config", "--get", "core.hooksPath"], cwd=cwd)
        hooks = hp.stdout.strip().replace("\\", "/").lower() if hp.returncode == 0 else ""
        installed = ".harness-tools" in hooks
    if not installed:
        return None, "no harness tools in the repository"
    led = Path(os.environ.get("HARNESS_LEDGER") or common / ".harness" / "ledger.json")
    return (led if led.is_file() else None), ("" if led.is_file() else MISSING_LEDGER)


def _safe_find_ledger(cwd: str):
    if not os.path.isdir(str(cwd)):
        return None, "not a git repository"
    try:
        return find_ledger(str(cwd))
    except Exception:  # noqa: BLE001 - a git failure is "not a repository", decided by the caller
        return None, "not a git repository"


def push_problems(cwd: str, fallback: str = "") -> list[str] | None:
    """What is missing for a push or PR; [] = allowed; None = no harness ledger (allowed, logged).
    A target given in the command (cd / git -C) that is not a git repository falls back to the session cwd; if that is no
    harness repo either, the push is denied (an unresolved target is never "no harness")."""
    led, why = _safe_find_ledger(cwd)
    if led is None and why == "not a git repository" and getattr(cwd, "given", False):
        led, why2 = _safe_find_ledger(fallback) if fallback else (None, "")
        if led is None:
            return [f"the push target `{str(cwd)[:120]}` cannot be resolved to a git repository (use a plain absolute path "
                    "or run the command from the repository)"]
        cwd = fallback
        why = why2
    if led is None and why == MISSING_LEDGER:
        return ["harness installed but .harness/ledger.json missing; restore it (ledger_sync) before push"]
    if led is None:
        return None
    try:
        return _ledger_problems(str(cwd), led)
    except Exception:  # noqa: BLE001 - a broken ledger fails closed
        return ["ledger unreadable or malformed"]


def _ledger_problems(cwd: str, led: Path) -> list[str]:
    import _agentio
    try:
        data = json.loads(led.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return ["ledger unreadable or malformed"]
    if not isinstance(data, dict):
        return ["ledger unreadable or malformed"]
    approval = data.get("approval")
    sprints = data.get("sprints", [])
    if (approval is not None and not isinstance(approval, dict)) or not isinstance(sprints, list):
        return ["ledger unreadable or malformed"]
    problems: list[str] = []
    h = _agentio.run_git(["rev-parse", "--verify", "HEAD^{commit}"], cwd=cwd)
    head = h.stdout.strip() if h.returncode == 0 else ""
    ap = ((approval or {}).get("head") or "")
    if not isinstance(ap, str):
        return ["ledger unreadable or malformed"]
    if not ap:
        problems.append("no GPT-PM approval is recorded in the ledger (run the gpt-pm-gate skill, then `ledger.py approve`)")
    elif not (approval or {}).get("request_id"):
        problems.append("the recorded approval has no PM Bridge request id, so it was never checked against a GPT-PM "
                        "reply; record it again with `ledger.py approve --request-id <id>`")
    elif head and ap != head:
        problems.append(f"the GPT-PM approval is bound to {ap[:12]}, not to the current HEAD {head[:12]} "
                        "(a new commit removes the approval; run gpt-pm-gate again)")
    sprint = next((s for s in data.get("sprints", []) if s.get("state") != "closed"), None)
    if sprint:
        open_ = []
        for t in sprint.get("tasks", []):
            if t.get("state") in ("verified", "done") or t.get("blocked"):
                continue
            open_.append(f"{t.get('id')} ({t.get('state')})")
        if open_:
            problems.append(f"sprint {sprint.get('id')} has tasks that are neither verified/done nor explicitly blocked: "
                            + ", ".join(open_[:12]) + (" ..." if len(open_) > 12 else ""))
    return problems


# ---- decision ------------------------------------------------------------------------------------------------------

def log(rec: dict) -> None:
    try:
        import _agentio
        d = _agentio.state_dir("rules_gate")
        with (d / "gate.log").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **rec}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def deny(reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}, ensure_ascii=False))


def decide(data: dict) -> str | None:
    """Deny reason, or None to allow."""
    import _agentio
    command = _agentio.shell_command(data) or str(_agentio.tool_input(data).get("script") or "")
    if not command.strip():
        return None
    agent = str(data.get("agent_type") or "")
    cwd = mk_cwd(str(data.get("cwd") or os.getcwd()), False)
    findings = analyze(command, cwd)
    for kind, detail, fcwd in findings:
        if kind == "full-test":
            return (f"{RULE4}: `{detail}` would run a whole test suite. Name the specific test file instead. " + ALTERNATIVE)
        if kind in ("lint", "typecheck", "verify"):
            word = {"lint": "lint", "typecheck": "typecheck", "verify": "a verify script"}[kind]
            return f"{RULE4}: `{detail}` is {word}, which is prohibited. " + ALTERNATIVE
        if kind == "ci":
            return (f"{RULE4}: `{detail}` starts or reads CI, which is prohibited (CI is off; workflows are "
                    "workflow_dispatch only and nobody runs them). Do not trigger or read CI results; run the task's "
                    "named test files through a test agent instead.")
        if kind == "named-test" and agent not in TEST_AGENTS:
            who = f"the subagent `{agent}`" if agent else "the main session"
            return (f"{RULE4}: `{detail}` with named test files may be run only by functional-test-reviewer, tdd-guide or "
                    f"e2e-runner, not by {who}. " + ALTERNATIVE)
        if kind == "py-script-test" and agent and agent not in TEST_AGENTS:
            return (f"{RULE4}: running a test script is allowed only for the main session or a test agent, not for the "
                    f"subagent `{agent}`. " + ALTERNATIVE)
    for kind, detail, fcwd in findings:
        if kind in ("push", "pr-create"):
            probs = push_problems(fcwd, cwd)
            if probs is None:
                log({"verdict": "allow-no-ledger", "kind": kind, "cwd": fcwd})
                continue
            if probs:
                return (f"Operator rule (one PR per sprint, push and PR only after GPT-PM approval of this HEAD and every "
                        f"sprint task verified): `{detail}` is denied. Missing: " + "; ".join(probs) + ".")
    return None


def main() -> int:
    try:
        import _agentio
        data = _agentio.read_stdin()
        if not _agentio.is_shell(data):
            return 0
        reason = decide(data)
        if reason:
            log({"verdict": "deny", "agent": str(data.get("agent_type") or ""), "reason": reason[:160]})
            deny(reason)
    except Exception:
        log({"verdict": "hook-error", "traceback": traceback.format_exc()[-1500:]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
