"""Task ledger: the single source of sprint and task state (.harness/ledger.json).

Two planning levels only: sprint -> tasks. Stdlib only. Run `python .harness-tools/ledger.py -h`.
The ledger lives next to the main checkout (git common dir), so every worktree sees the same one.
Kill switch for the push check: HARNESS_SKIP_APPROVAL=1 (recorded as an event, shown in the report).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

STATES = ["todo", "in_progress", "implemented", "not_run", "verified", "done", "failed"]
RISKS = ["trivial", "local", "multi-module", "risky"]
TASK_LINE = re.compile(r"^Task:\s*(\S+)\s*$", re.M)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass


class GitError(RuntimeError):
    pass


def git(*args, cwd=None, check=True) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")} if cwd else None
    try:
        r = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd, env=env)
    except FileNotFoundError as e:
        raise GitError("git not found on PATH") from e
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def main_root() -> Path:
    """Top of the main checkout, also when called from a linked worktree."""
    try:
        common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir"))
    except GitError:
        return Path.cwd()
    return common.parent if common.name == ".git" else common


def ledger_path() -> Path:
    return Path(os.environ.get("HARNESS_LEDGER") or main_root() / ".harness" / "ledger.json")


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def head() -> str:
    h = git("rev-parse", "--verify", "HEAD^{commit}")
    if not re.fullmatch(r"[0-9a-f]{40,64}", h):
        raise GitError(f"unexpected HEAD value {h!r}")
    return h


def resolve(rev: str) -> str:
    return git("rev-parse", "--verify", f"{rev}^{{commit}}")


def load() -> dict:
    p = ledger_path()
    if not p.exists():
        return {"version": 1, "project": main_root().name, "sprints": [], "approval": None, "events": []}
    return json.loads(p.read_text(encoding="utf-8"))


def save(data: dict) -> None:
    p = ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".ledger-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(10):  # Windows: a reader may hold the file for a moment
            try:
                os.replace(tmp, p)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.1 * (attempt + 1))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextmanager
def locked():
    """Serialize load-modify-save across processes (hooks, agents, route.py)."""
    lock = ledger_path().with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 30
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except (FileExistsError, PermissionError):  # Windows: PermissionError while the holder deletes it
            if time.monotonic() > deadline:
                try:
                    stale = time.time() - lock.stat().st_mtime > 120  # lock from a killed process
                except FileNotFoundError:
                    continue
                if stale:
                    lock.unlink(missing_ok=True)
                    continue
                raise SystemExit(f"ledger: lock {lock} held for more than 30 s")
            time.sleep(0.05)
    try:
        d = load()
        yield d
        save(d)
    finally:
        for attempt in range(20):
            try:
                lock.unlink(missing_ok=True)
                break
            except PermissionError:
                time.sleep(0.05)


def event(data: dict, kind: str, **kw) -> None:
    data.setdefault("events", []).append({"at": now(), "kind": kind, **kw})


def hook_error(data: dict, msg: str) -> None:
    """A durable, visible record of a hook failure (shown in status and the report)."""
    event(data, "hook-error", message=msg)
    p = ledger_path().parent / "hook-error.log"
    with open(p, "a", encoding="utf-8") as f:
        f.write(f"{now()} {msg}\n")
    print(f"HARNESS HOOK ERROR: {msg}", file=sys.stderr)


def find_task(data: dict, tid: str) -> tuple[dict, dict]:
    for s in data["sprints"]:
        for t in s["tasks"]:
            if t["id"] == tid:
                return s, t
    sys.exit(f"ledger: task {tid} not found")


def active_sprint(data: dict) -> dict | None:
    return next((s for s in data["sprints"] if s["state"] != "closed"), None)


def counter(sprint: dict) -> tuple[int, int]:
    return sum(1 for t in sprint["tasks"] if t["state"] == "done"), len(sprint["tasks"])


def recompute(t: dict) -> None:
    """The task state follows from its evidence; it can go down as well as up."""
    if t["state"] in ("todo", "in_progress") and not t["commits"]:
        return
    v, r = t.get("verify") or {}, t.get("review") or {}
    fresh = bool(t["commits"]) and v.get("task_commit") == t["commits"][-1]
    if not t["commits"]:
        t["state"] = "in_progress" if t["state"] != "todo" else "todo"
    elif not v or not fresh:
        t["state"] = "implemented"
    elif v["result"] == "fail":
        t["state"] = "failed"
    elif v["result"] == "NOT_RUN":
        t["state"] = "not_run"
    elif (r.get("result") == "accepted" and r.get("task_commit") == t["commits"][-1]) or t.get("risk") == "trivial":
        t["state"] = "done"
    else:
        t["state"] = "verified"


# ---- commands -------------------------------------------------------------

def c_sprint_add(a, d):
    if any(s["id"] == a.id for s in d["sprints"]):
        sys.exit(f"ledger: sprint {a.id} exists")
    d["sprints"].append({"id": a.id, "title": a.title, "branch": a.branch or f"sprint/{a.id.lower()}",
                         "state": "open", "opened": now(), "tasks": []})
    event(d, "sprint-add", sprint=a.id)
    print(f"sprint {a.id} added")


def c_task_add(a, d):
    if a.sprint:
        s = next((s for s in d["sprints"] if s["id"] == a.sprint), None)
        if not s:
            sys.exit(f"ledger: sprint {a.sprint} not found")
    else:
        s = active_sprint(d)
    if not s:
        sys.exit("ledger: no open sprint; run sprint-add first")
    tid = a.id or f"{s['id']}-T{len(s['tasks']) + 1}"
    if any(t["id"] == tid for x in d["sprints"] for t in x["tasks"]):
        sys.exit(f"ledger: task {tid} exists")
    s["tasks"].append({"id": tid, "title": a.title, "criteria": a.criterion or [], "run": a.run or "", "risk": a.risk,
                       "journey": a.journey, "state": "todo", "commits": [], "verify": None,
                       "review": None, "routing": None, "tokens": None})
    event(d, "task-add", task=tid)
    print(f"task {tid} added to {s['id']}")


def c_next(a, d):
    s = active_sprint(d)
    if not s:
        print("no open sprint")
        return
    for t in s["tasks"]:
        if t["state"] != "done":
            print(json.dumps(t, indent=1, ensure_ascii=False))
            return
    print(f"all tasks of {s['id']} are done")


def c_start(a, d):
    _, t = find_task(d, a.id)
    if t["state"] not in ("todo", "failed", "in_progress", "not_run", "implemented"):
        sys.exit(f"ledger: {a.id} is {t['state']}; start works on todo, failed, not_run or implemented tasks")
    busy = [x["id"] for s in d["sprints"] for x in s["tasks"] if x["state"] == "in_progress" and x["id"] != a.id]
    # Operator 2026-11-11: "one task at a time" is off by default; opt in with HARNESS_ONE_TASK=1.
    if busy and os.environ.get("HARNESS_ONE_TASK") == "1":
        sys.exit(f"ledger: one task at a time; {', '.join(busy)} is in progress")
    if not t["commits"]:
        t["state"] = "in_progress"
    t.pop("blocked", None)  # starting a task lifts an earlier block
    event(d, "start", task=a.id)
    print(f"{a.id} {t['state']}")


OPERATOR_CASES = ("prohibition", "money", "personal-secret", "captcha", "account", "irreversible-delete", "force-push", "production-data")


# a closed pair of quotes with 6+ characters of text between them
QUOTE_PATTERNS = ("\"[^\"]{6,}\"", "'[^']{6,}'", "`[^`]{6,}`", "“[^”]{6,}”", "«[^»]{6,}»")


def c_block(a, d):
    """Mark a task as blocked for a verified reason. The tasks-left Stop gate ignores blocked tasks (S7-T2)."""
    _, t = find_task(d, a.id)
    if t["state"] == "done":
        sys.exit(f"ledger: {a.id} is already done")
    cause = (a.cause or "").strip()
    if len(cause) < 10:
        sys.exit("ledger: --cause must state the verified cause (at least 10 characters)")
    case = (getattr(a, "case", None) or "").strip()
    if a.klass == "OPERATOR":
        # S9-T6: the operator is the last resort. An OPERATOR block must name one operator-only case; a prohibition also needs the quoted error.
        if case not in OPERATOR_CASES:
            sys.exit("ledger: --class OPERATOR needs --case one of: " + ", ".join(OPERATOR_CASES)
                     + " (anything else the agent decides or does itself, see the self-service ladder)")
        if case == "prohibition" and not any(re.search(pat, cause) for pat in QUOTE_PATTERNS):
            sys.exit("ledger: --case prohibition needs the tool's error quoted in --cause (a closed pair of quotes with the text between)")
    elif case:
        sys.exit("ledger: --case is only for --class OPERATOR")
    t["blocked"] = {"class": a.klass, "cause": cause, "at": now(), **({"case": case} if case else {})}
    event(d, "block", task=a.id, klass=a.klass)
    print(f"{a.id} blocked ({a.klass})")


def c_commit_hook(a, d):
    sha = head()
    msg = git("log", "-1", "--format=%B")
    m = TASK_LINE.search(msg)
    if not m:
        return
    tid = m.group(1)
    if tid in HOUSEKEEPING_TASKS:  # harness maintenance commits (tools/ci_triggers_off.py) are not project tasks
        event(d, "housekeeping-commit", task=tid, sha=sha)
        return
    if not any(t["id"] == tid for s in d["sprints"] for t in s["tasks"]):
        hook_error(d, f"commit {sha[:12]} names unknown task {tid}")
        return
    _, t = find_task(d, tid)
    if sha not in t["commits"]:
        t["commits"].append(sha)
    recompute(t)  # a new commit makes earlier verify/review stale
    event(d, "commit", task=tid, sha=sha)
    print(f"ledger: {tid} <- {sha[:7]} ({t['state']})")


TEST_ROLES = {"functional-test-reviewer", "tdd-guide", "e2e-runner"}
NOT_REVIEWERS = {"implementation-engineer", "general-purpose", "claude", "explore", "plan", "planner",
                 "build-error-resolver", "code-simplifier", "refactor-cleaner", "doc-updater", "technical-writer"}


def _hook_state_base() -> Path:
    """Same place the hooks write: HARNESS_HOOK_STATE, else that key of the machine config the hooks load
    (~/.harness-hooks/config.json or HARNESS_HOOK_CONFIG), else ~/.harness-hooks."""
    v = os.environ.get("HARNESS_HOOK_STATE")
    if not v:
        cfg = Path(os.environ.get("HARNESS_HOOK_CONFIG") or Path.home() / ".harness-hooks" / "config.json")
        try:
            c = json.loads(cfg.read_text(encoding="utf-8-sig"))
            x = c.get("HARNESS_HOOK_STATE") if isinstance(c, dict) else None
            if isinstance(x, str) and x and "<" not in x:
                v = os.path.expandvars(os.path.expanduser(x))
        except (OSError, ValueError):
            pass
    return Path(v) if v else Path.home() / ".harness-hooks"


def _agents_state() -> list:
    base = _hook_state_base()
    try:
        d = json.loads((base / "agents" / "active.json").read_text(encoding="utf-8"))
        return d.get("agents", []) if isinstance(d, dict) else []
    except (OSError, ValueError):
        return []


def require_agent_run(role: str, task_commit: str, what: str) -> None:
    """A pass/accepted result needs a finished run of that agent role that started after the task's last commit, as
    recorded by the agent_visibility hook. 2026-11-12 incident: a runaway helper session wrote 'verify pass' and
    'review accepted' for every S14 task with no test or review agent ever run, then closed the sprint."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import route
        rules = route.load_rules(main_root())
        # a docs-only commit (logs, backlog) after the agent run does not make the run stale
        cur = task_commit
        while True:
            files = [f for f in git("diff-tree", "--no-commit-id", "--name-only", "-r", "-m", "--root", cur).splitlines() if f]
            if not files or any(not route.is_doc(rules, f) for f in files):
                break
            prev = [c for c in git("rev-list", "--parents", "-n", "1", cur).split()[1:2]]
            if not prev:
                break
            cur = prev[0]
        since = int(git("show", "-s", "--format=%ct", cur))
    except (GitError, ValueError, ImportError):
        sys.exit(f"ledger: cannot read the time of commit {task_commit[:12]}")
    runs = [e for e in _agents_state() if str(e.get("type", "")).lower() == role.lower()
            and e.get("stop") and float(e.get("start", 0)) >= since - 5]
    if not runs:
        sys.exit(f"ledger: no finished {role} agent run after commit {task_commit[:12]} is recorded; {what} needs one "
                 "(the agent_visibility hook records START/STOP). Run that agent, then record the result.")


def c_verify(a, d):
    _, t = find_task(d, a.id)
    if not t["commits"]:
        sys.exit(f"ledger: {a.id} has no commit yet; verify needs code (commit with 'Task: {a.id}')")
    if a.result == "pass":
        if (a.by or "").lower() not in TEST_ROLES:
            sys.exit(f"ledger: verify pass needs --by <test agent role>: one of {', '.join(sorted(TEST_ROLES))}")
        require_agent_run(a.by, t["commits"][-1], "a verify pass")
    t["verify"] = {"result": a.result, "evidence": a.evidence, "at": now(), "task_commit": t["commits"][-1],
                   "by": a.by or None}
    recompute(t)
    event(d, "verify", task=a.id, result=a.result)
    print(f"{a.id} verify={a.result} state={t['state']}")


def c_review(a, d):
    _, t = find_task(d, a.id)
    if not t["commits"]:
        sys.exit(f"ledger: {a.id} has no commit yet")
    if a.result == "accepted":
        by = [r.strip() for r in (a.by or "").split(",") if r.strip()]
        if not by or any(r.lower() in NOT_REVIEWERS or r.lower() in TEST_ROLES for r in by):
            sys.exit("ledger: review accepted needs --by <reviewer role[,role]> (the reviewers route.py chose; "
                     "an implementation, test or general agent is not a reviewer)")
        for r in by:
            require_agent_run(r, t["commits"][-1], "an accepted review")
    t["review"] = {"result": a.result, "findings": a.findings, "at": now(), "task_commit": t["commits"][-1],
                   "by": a.by or None}
    recompute(t)
    event(d, "review", task=a.id, result=a.result)
    print(f"{a.id} review={a.result} state={t['state']}")


def c_tokens(a, d):
    _, t = find_task(d, a.id)
    t["tokens"] = {"code": a.code, "tests": a.tests, "docs": a.docs}
    print(f"{a.id} tokens recorded")


REQ_ID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
APPROVE_LINE = re.compile(r"^[\s*#>_]*(?:VERDICT\s*:\s*)?APPROVE[D]?[\s*_.]*$", re.I)
NEGATIVE_LINE = re.compile(r"^[\s*#>_]*(?:VERDICT\s*:\s*)?(?:REVISE|REQUEST[_ ]CHANGES|MODIFY|REJECT|RESEND\b|NOT APPROVED)",
                           re.I)
HEAD_LINE = re.compile(r"^[\s*#>_]*HEAD\s*:\s*`?([0-9a-f]{7,64})", re.I | re.M)


def pm_bridge_messages() -> Path | None:
    """PM Bridge's message log: HARNESS_PM_BRIDGE_MESSAGES, else a `pm-bridge/state/messages.jsonl` beside the project
    or beside one of its parent folders (the workspace layout: <workspace>/<project>, <workspace>/pm-bridge)."""
    env = os.environ.get("HARNESS_PM_BRIDGE_MESSAGES")
    if env:
        return Path(env)
    for p in [main_root(), *main_root().parents]:
        c = p.parent / "pm-bridge" / "state" / "messages.jsonl"
        if c.is_file():
            return c
    return None


def find_pm_approve(request_id: str, h: str, project: str) -> tuple[str | None, str]:
    """(reply id, '') when PM Bridge holds a correlated GPT-PM reply to `request_id` whose verdict is APPROVE and whose
    HEAD (in the reply, else in the request we sent) is `h`; otherwise (None, reason)."""
    path = pm_bridge_messages()
    if not path:
        return None, "PM Bridge message log not found (set HARNESS_PM_BRIDGE_MESSAGES)"
    norm = lambda s: re.sub(r"[^a-z0-9]", "", str(s or "").lower())
    out_text, replies = "", []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if request_id not in line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("reviewRequestId") != request_id:
                    continue
                if r.get("project") and norm(r["project"]) != norm(project):
                    continue
                if r.get("direction") == "outbound":
                    out_text += str(r.get("text") or "")
                elif r.get("direction") == "inbound" and r.get("correlated"):
                    replies.append(r)
    except OSError as e:
        return None, f"cannot read {path}: {e}"
    if not replies:
        return None, f"no correlated GPT-PM reply to request {request_id} for project {project} in {path}"
    for r in reversed(replies):
        text = str(r.get("text") or "")
        lines = text.splitlines()
        if any(NEGATIVE_LINE.match(l) for l in lines) or not any(APPROVE_LINE.match(l) for l in lines):
            continue
        heads = HEAD_LINE.findall(text) or HEAD_LINE.findall(out_text)
        if not heads or not all(h.startswith(x.lower()) for x in heads):
            return None, f"the APPROVE reply to {request_id} is for HEAD {', '.join(heads) or '(none named)'}, not {h[:12]}"
        return str(r.get("replyId") or r.get("messageId")), ""
    return None, f"the GPT-PM reply to {request_id} is not a bare APPROVE (REVISE/RESEND/findings or no verdict line)"


def c_approve(a, d):
    h = resolve(a.head) if a.head else head()
    if not a.request_id or not REQ_ID_RE.fullmatch(a.request_id):
        sys.exit("ledger: approve needs --request-id <PM Bridge review request id>; the approval is checked against "
                 "GPT-PM's real reply, a typed text is not an approval")
    reply, why = find_pm_approve(a.request_id, h, d.get("project") or main_root().name)
    if not reply:
        sys.exit(f"ledger: approval refused: {why}")
    d["approval"] = {"head": h, "by": "GPT-PM", "at": now(), "evidence": a.evidence or "",
                     "request_id": a.request_id, "reply_id": reply}
    event(d, "approve", head=h, request_id=a.request_id, reply_id=reply)
    print(f"approval bound to {h[:12]} (GPT-PM reply {reply})")


def c_reopen(a, d):
    """Undo a wrong sprint close: the sprint is open again, every task's verify and review are cleared (to be recorded
    again with real agent runs) and an approval of a commit on this sprint is dropped. History stays in the events."""
    s = next((s for s in d["sprints"] if s["id"] == a.id), None)
    if not s:
        sys.exit(f"ledger: sprint {a.id} not found")
    s["state"], s["closed"] = "open", None
    for t in s["tasks"]:
        t["verify"], t["review"] = None, None
        recompute(t)
    dropped = bool(d.get("approval"))
    d["approval"] = None
    event(d, "sprint-reopen", sprint=a.id, reason=a.reason, approval_dropped=dropped)
    print(f"sprint {a.id} reopened; verify/review cleared for {len(s['tasks'])} task(s); approval dropped={dropped}")


def is_ancestor(a: str, b: str) -> bool:
    r = subprocess.run(["git", "merge-base", "--is-ancestor", a, b], capture_output=True)
    return r.returncode == 0


def doc_only_since(base: str, tip: str) -> bool:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import route
    rules = route.load_rules(main_root())
    commits = [c for c in git("rev-list", f"{base}..{tip}").splitlines() if c]
    if not commits:
        return False
    # Check every intervening commit, not the net diff: a code change later reverted still needs review.
    for c in commits:
        files = [f for f in git("diff-tree", "--no-commit-id", "--name-only", "-r", "-m", "--root", c).splitlines() if f]
        if any(not route.is_doc(rules, f) for f in files):
            return False
    return True


def c_check_approval(a, d):
    """pre-push: every pushed commit must be the approved one, an ancestor of it, or a docs-only descendant."""
    lines = [l.split() for l in (sys.stdin.read() if not sys.stdin.isatty() else "").splitlines() if l.strip()]
    shas = [l[1] for l in lines if len(l) == 4 and set(l[1]) != {"0"}] if lines else [head()]
    if lines and not shas:
        print("approval check: delete-only push, nothing to check")
        return 0
    if os.environ.get("HARNESS_SKIP_APPROVAL") == "1":
        with locked() as dd:
            event(dd, "approval-skipped", shas=shas)
        print("approval check skipped by HARNESS_SKIP_APPROVAL=1 (recorded in the ledger)", file=sys.stderr)
        return 0
    rec = d.get("approval") or {}
    # an approval without the PM Bridge request id was typed, not checked against a GPT-PM reply (2026-11-12 incident)
    ap = rec.get("head") if rec.get("request_id") else None
    bad = []
    for sha in shas:
        if ap and (sha == ap or is_ancestor(sha, ap) or (is_ancestor(ap, sha) and doc_only_since(ap, sha))):
            continue
        bad.append(sha)
    if not bad:
        print(f"approval ok ({len(shas)} ref(s), approved {ap[:12]})")
        return 0
    print(f"push blocked: no GPT-PM APPROVE covers {', '.join(s[:12] for s in bad)} (approved: {(ap or 'none')[:12]}). "
          "Run the gpt-pm-gate skill, then `python .harness-tools/ledger.py approve --request-id <PM Bridge request id>` (checked against the real reply).", file=sys.stderr)
    return 1


def c_sprint_close(a, d):
    s = next((s for s in d["sprints"] if s["id"] == a.id), None)
    if not s:
        sys.exit(f"ledger: sprint {a.id} not found")
    open_ = [t["id"] for t in s["tasks"] if t["state"] != "done"]
    if open_ and not a.force:
        sys.exit(f"ledger: not done: {', '.join(open_)} (use --force to close anyway)")
    s["state"], s["closed"] = "closed", now()
    event(d, "sprint-close", sprint=a.id, forced=bool(open_))
    print(f"sprint {a.id} closed")


UNKNOWN_TASK_RE = re.compile(r"names unknown task (\S+)")
HOUSEKEEPING_TASKS = {"CI-OFF"}  # Task: trailers of harness maintenance commits in project repos


def open_hook_errors(d) -> list:
    """Hook errors still worth a warning. History is never removed (operator rule 5): an error is only hidden when
    (a) it reported an unknown task that the ledger now has (the plan sync added it), or
    (b) an 'ack-errors' event recorded later acknowledges it with a reason."""
    ids = {t["id"] for s in d["sprints"] for t in s["tasks"]}
    events = d.get("events", [])
    last_ack = max((i for i, e in enumerate(events) if e.get("kind") == "hook-error-ack"), default=-1)
    out = []
    for i, e in enumerate(events):
        if e.get("kind") != "hook-error" or i < last_ack:
            continue
        m = UNKNOWN_TASK_RE.search(str(e.get("message", "")))
        if m and (m.group(1) in ids or m.group(1) in HOUSEKEEPING_TASKS):
            continue
        out.append(e)
    return out


def c_ack_errors(a, d):
    n = len(open_hook_errors(d))
    event(d, "hook-error-ack", reason=a.reason, count=n)
    print(f"ledger: {n} open hook error(s) acknowledged ({a.reason}); history kept")


def c_status(a, d):
    errs = open_hook_errors(d)
    if errs:
        print(f"WARNING: {len(errs)} hook error(s); last: {errs[-1]['message']}")
    if not d["sprints"]:
        print("ledger empty")
        return
    total_done = sum(counter(s)[0] for s in d["sprints"])
    total = sum(counter(s)[1] for s in d["sprints"])
    print(f"project {d['project']}: done {total_done} of {total}")
    for s in d["sprints"]:
        n, m = counter(s)
        print(f"  {s['id']} [{s['state']}] {s['title']}: done {n} of {m}")
        if s["state"] != "closed":
            for t in s["tasks"]:
                print(f"    {t['id']:10} {t['state']:12} {t['risk']:12} {t['title']}")


READ_ONLY = {"next", "status", "check-approval"}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ledger", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    x = sub.add_parser("sprint-add"); x.add_argument("--id", required=True); x.add_argument("--title", required=True); x.add_argument("--branch")
    x = sub.add_parser("task-add"); x.add_argument("--sprint"); x.add_argument("--id"); x.add_argument("--title", required=True)
    x.add_argument("--criterion", action="append", help="repeat for each acceptance criterion")
    x.add_argument("--run", help="how a tester runs the feature (command, screen, endpoint)")
    x.add_argument("--risk", choices=RISKS, default="local"); x.add_argument("--journey", action="store_true")
    sub.add_parser("next")
    x = sub.add_parser("start"); x.add_argument("id")
    x = sub.add_parser("block"); x.add_argument("id"); x.add_argument("--class", dest="klass", choices=["OPERATOR", "EXTERNAL", "PROHIBITED"], required=True)
    x.add_argument("--cause", required=True, help="the verified cause")
    x.add_argument("--case", help="OPERATOR only: " + "|".join(OPERATOR_CASES))
    sub.add_parser("commit-hook")
    x = sub.add_parser("verify"); x.add_argument("id"); x.add_argument("--result", choices=["pass", "fail", "NOT_RUN"], required=True); x.add_argument("--evidence", required=True); x.add_argument("--by", help="test agent role that ran the tests (required for pass)")
    x = sub.add_parser("review"); x.add_argument("id"); x.add_argument("--result", choices=["accepted", "changes"], required=True); x.add_argument("--findings", type=int, default=0); x.add_argument("--by", help="reviewer role(s) that ran, comma separated (required for accepted)")
    x = sub.add_parser("tokens"); x.add_argument("id"); x.add_argument("--code", type=int, default=0); x.add_argument("--tests", type=int, default=0); x.add_argument("--docs", type=int, default=0)
    x = sub.add_parser("approve"); x.add_argument("--head"); x.add_argument("--request-id", required=True, help="PM Bridge review request id whose GPT-PM reply is APPROVE for this HEAD"); x.add_argument("--evidence", default="", help="optional note")
    sub.add_parser("check-approval")
    x = sub.add_parser("sprint-close"); x.add_argument("id"); x.add_argument("--force", action="store_true")
    x = sub.add_parser("reopen"); x.add_argument("id"); x.add_argument("--reason", required=True)
    sub.add_parser("status")
    x = sub.add_parser("ack-errors"); x.add_argument("--reason", required=True, help="why the open hook errors are acknowledged")
    a = p.parse_args(argv)
    fn = globals()["c_" + a.cmd.replace("-", "_")]
    try:
        if a.cmd in READ_ONLY:
            return fn(a, load()) or 0
        with locked() as d:
            return fn(a, d) or 0
    except GitError as e:
        if a.cmd == "commit-hook":
            with locked() as d:
                hook_error(d, str(e))
            return 1
        print(f"ledger: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
