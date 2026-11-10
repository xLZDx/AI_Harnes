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
    if busy:
        sys.exit(f"ledger: one task at a time; {', '.join(busy)} is in progress")
    if not t["commits"]:
        t["state"] = "in_progress"
    event(d, "start", task=a.id)
    print(f"{a.id} {t['state']}")


def c_commit_hook(a, d):
    sha = head()
    msg = git("log", "-1", "--format=%B")
    m = TASK_LINE.search(msg)
    if not m:
        return
    tid = m.group(1)
    if not any(t["id"] == tid for s in d["sprints"] for t in s["tasks"]):
        hook_error(d, f"commit {sha[:12]} names unknown task {tid}")
        return
    _, t = find_task(d, tid)
    if sha not in t["commits"]:
        t["commits"].append(sha)
    recompute(t)  # a new commit makes earlier verify/review stale
    event(d, "commit", task=tid, sha=sha)
    print(f"ledger: {tid} <- {sha[:7]} ({t['state']})")


def c_verify(a, d):
    _, t = find_task(d, a.id)
    if not t["commits"]:
        sys.exit(f"ledger: {a.id} has no commit yet; verify needs code (commit with 'Task: {a.id}')")
    t["verify"] = {"result": a.result, "evidence": a.evidence, "at": now(), "task_commit": t["commits"][-1]}
    recompute(t)
    event(d, "verify", task=a.id, result=a.result)
    print(f"{a.id} verify={a.result} state={t['state']}")


def c_review(a, d):
    _, t = find_task(d, a.id)
    if not t["commits"]:
        sys.exit(f"ledger: {a.id} has no commit yet")
    t["review"] = {"result": a.result, "findings": a.findings, "at": now(), "task_commit": t["commits"][-1]}
    recompute(t)
    event(d, "review", task=a.id, result=a.result)
    print(f"{a.id} review={a.result} state={t['state']}")


def c_tokens(a, d):
    _, t = find_task(d, a.id)
    t["tokens"] = {"code": a.code, "tests": a.tests, "docs": a.docs}
    print(f"{a.id} tokens recorded")


def c_approve(a, d):
    h = resolve(a.head) if a.head else head()
    d["approval"] = {"head": h, "by": "GPT-PM", "at": now(), "evidence": a.evidence}
    event(d, "approve", head=h, evidence=a.evidence)
    print(f"approval bound to {h[:12]}")


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
    ap = (d.get("approval") or {}).get("head")
    bad = []
    for sha in shas:
        if ap and (sha == ap or is_ancestor(sha, ap) or (is_ancestor(ap, sha) and doc_only_since(ap, sha))):
            continue
        bad.append(sha)
    if not bad:
        print(f"approval ok ({len(shas)} ref(s), approved {ap[:12]})")
        return 0
    print(f"push blocked: no GPT-PM APPROVE covers {', '.join(s[:12] for s in bad)} (approved: {(ap or 'none')[:12]}). "
          "Run the gpt-pm-gate skill, then `python .harness-tools/ledger.py approve --evidence ...`.", file=sys.stderr)
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


def c_status(a, d):
    errs = [e for e in d.get("events", []) if e["kind"] == "hook-error"]
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
    sub.add_parser("commit-hook")
    x = sub.add_parser("verify"); x.add_argument("id"); x.add_argument("--result", choices=["pass", "fail", "NOT_RUN"], required=True); x.add_argument("--evidence", required=True)
    x = sub.add_parser("review"); x.add_argument("id"); x.add_argument("--result", choices=["accepted", "changes"], required=True); x.add_argument("--findings", type=int, default=0)
    x = sub.add_parser("tokens"); x.add_argument("id"); x.add_argument("--code", type=int, default=0); x.add_argument("--tests", type=int, default=0); x.add_argument("--docs", type=int, default=0)
    x = sub.add_parser("approve"); x.add_argument("--head"); x.add_argument("--evidence", required=True, help="GPT-PM verdict reference: chat, reply id or quote")
    sub.add_parser("check-approval")
    x = sub.add_parser("sprint-close"); x.add_argument("id"); x.add_argument("--force", action="store_true")
    sub.add_parser("status")
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
