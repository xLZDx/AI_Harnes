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
WRAPPERS = {"env", "time", "call", "nohup", "sudo", "command", "exec", "start-process", "poetry", "pipenv", "uv", "uvx",
            "pipx", "hatch", "rye", "conda", "npx", "pnpx", "bunx"}
PKG = {"npm", "pnpm", "yarn", "bun"}


# ---- parsing -----------------------------------------------------------------------------------------------------

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


def has_file(args: list[str]) -> bool:
    return any(not is_flag(a) and FILE_RE.search(a) for a in args)


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
        return []
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
        r = [a for a in rest]
        if b in ("poetry", "pipenv", "uv", "hatch", "rye", "conda", "pipx") and r and r[0] in ("run", "exec"):
            r = r[1:]
        while r and is_flag(r[0]):
            r = r[1:]
        return classify(r, cwd, depth + 1)
    if b in ("npx", "pnpx", "bunx"):
        r = list(rest)
        while r and is_flag(r[0]):
            if r[0] in ("-p", "--package") and len(r) > 1:
                r = r[1:]
            r = r[1:]
        return classify(r, cwd, depth + 1)

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
                gcwd = os.path.normpath(os.path.join(gcwd, rest[i + 1]))
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
        args, i = list(rest), 0
        while i < len(args) and is_flag(args[i]):
            i += 2 if args[i] in ("-C", "--prefix", "--cwd", "--filter", "-F", "-w", "--workspace") else 1
        if i >= len(args):
            return []
        sub, after = args[i].lower(), args[i + 1:]
        if sub in ("test", "t", "tst"):
            return [("named-test" if has_file(after) else "full-test", f"{b} test", cwd)]
        if sub in ("run", "run-script", "run-s", "run-p") and after:
            script = after[0]
            k = script_kind(script)
            if k == "test":
                return [("named-test" if has_file(after[1:]) else "full-test", f"{b} run {script}", cwd)]
            if k:
                return [(k, f"{b} run {script}", cwd)]
            return []
        if sub in ("exec", "dlx", "x"):
            return classify(after, cwd, depth + 1)
        k = script_kind(sub)
        if k == "test":
            return [("named-test" if has_file(after) else "full-test", f"{b} {sub}", cwd)]
        if k:
            return [(k, f"{b} {sub}", cwd)]
        return classify(args[i:], cwd, depth + 1) if b in ("pnpm", "yarn", "bun") else []

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
    if b in ("eslint", "eslint_d", "ruff", "flake8", "pylint", "biome"):
        return [("lint", b, cwd)]
    if b in ("mypy", "pyright", "basedpyright"):
        return [("typecheck", b, cwd)]
    if b == "tsc" and any(a.lower() in ("--noemit", "-noemit") for a in rest):
        return [("typecheck", "tsc --noEmit", cwd)]
    if VERIFY_FILE_RE.match(toks[0].replace("\\", "/").rsplit("/", 1)[-1]):
        return [("verify", "verify script " + toks[0].replace("\\", "/").rsplit("/", 1)[-1], cwd)]
    return []


def analyze(command: str, cwd: str, depth: int = 0) -> list[tuple]:
    found: list[tuple] = []
    cur = cwd
    for seg in split_segments(command):
        toks = tokenize(seg)
        if not toks:
            continue
        b = base(toks[0])
        if b in ("cd", "pushd", "set-location", "sl", "chdir") and len(toks) > 1:
            target = next((t for t in toks[1:] if not is_flag(t) and t.lower() != "-path"), "")
            if target:
                cur = os.path.normpath(os.path.join(cur, os.path.expanduser(target)))
            continue
        found.extend(classify(toks, cur, depth))
    return found


# ---- R2: ledger ----------------------------------------------------------------------------------------------------

def find_ledger(cwd: str):
    """(ledger_path, why) using the same discovery as the project pre-push hook; ledger_path None = no harness."""
    import _agentio
    top = _agentio.run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
    com = _agentio.run_git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=cwd)
    if top.returncode != 0 or com.returncode != 0:
        return None, "not a git repository"
    top_p, common_dir = Path(top.stdout.strip()), Path(com.stdout.strip())
    common = common_dir.parent if common_dir.name == ".git" else common_dir
    if (common / ".harness-tools" / "ledger.py").is_file():
        pass
    else:
        lp = top_p / "tools" / "ledger.py"
        try:
            ok = "single source of sprint and task state" in lp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            ok = False
        if not ok:
            return None, "no harness tools in the repository"
    led = Path(os.environ.get("HARNESS_LEDGER") or common / ".harness" / "ledger.json")
    return (led if led.is_file() else None), ("" if led.is_file() else "no ledger file")


def push_problems(cwd: str) -> list[str] | None:
    """What is missing for a push or PR; [] = allowed; None = no harness ledger (allowed, logged)."""
    import _agentio
    led, why = find_ledger(cwd)
    if led is None:
        return None
    try:
        data = json.loads(led.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise RuntimeError(f"ledger unreadable: {e}")
    problems: list[str] = []
    h = _agentio.run_git(["rev-parse", "--verify", "HEAD^{commit}"], cwd=cwd)
    head = h.stdout.strip() if h.returncode == 0 else ""
    ap = ((data.get("approval") or {}).get("head") or "")
    if not ap:
        problems.append("no GPT-PM approval is recorded in the ledger (run the gpt-pm-gate skill, then `ledger.py approve`)")
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
    cwd = str(data.get("cwd") or os.getcwd())
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
            probs = push_problems(fcwd)
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
