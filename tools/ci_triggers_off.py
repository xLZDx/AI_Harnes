#!/usr/bin/env python3
"""Switch GitHub workflows that start on push, pull_request, pull_request_target or schedule to manual dispatch
(operator rule 4: no CI on push, pull request or schedule), in every project under a root folder.

For each project (a folder with .git and .github/workflows) the top-level `on:` value of every offending workflow is
rewritten to `on:\\n  workflow_dispatch:`; the original trigger block is kept as YAML comments right below. Everything else
stays byte-identical, line endings included. The result is re-parsed with tools/workflow_guard.py (the pre-push guard).
Then core/DECISION_LOG.md and core/LESSONS_LOG.md get an entry and exactly those paths are committed with git's normal
hooks (never --no-verify). Nothing is pushed. Stdlib only.

--dirty-ok handles a repo whose workflow/log files have uncommitted edits: the commit is made in a temp worktree, the
branch is moved with update-ref, the change is 3-way merged into the working copy (index entries intent-to-add kept),
and everything is rolled back on any conflict or unexpected status change. Refused when anything is staged.
The temp commit lives on a private ref refs/harness/ci-off/<run>, never on a branch.

Safety of the --dirty-ok path (and of every non-dry run):
- Concurrency: an OS lock on byte 0 of <git common dir>/ci_triggers_off.lock (msvcrt.locking / fcntl.flock, file never
  deleted). A second run waits up to 30 s, then reports SKIP 'another ci_triggers_off run holds the lock'. The update-ref
  compare-and-swap (new, expected old) stays as the backstop.
- Crash half-way: before the branch moves, <common>/ci_triggers_off.journal.json records old/new sha, branch, a byte-backup
  folder under --scratch (outside the repo), touched paths, the stage and status snapshot and the phase (prepared,
  branch-moved, files-merged, index-refreshed, done). It is deleted only after the final status/stage check passed. A run that
  finds a journal refuses to start (SKIP 'interrupted run found; use --recover'). --recover rolls back: branch back to the old
  sha (compare-and-swap), backed-up bytes restored, index refreshed and intent-to-add entries re-added, temp worktree and
  private ref removed, journal deleted; reports RECOVERED. A journal in phase done is just removed.
- TEST-ONLY fault injection: env HARNESS_CI_OFF_FAIL_AFTER=<phase> makes the process exit(70) at once (os._exit, no
  cleanup) right after that phase is journaled. Off by default; never set it in real use.

Usage: ci_triggers_off.py [--dry-run] [--project NAME] [--include-deploy] [--root D:\\Repo] [--dirty-ok] [--scratch DIR] [--recover]
Exit code 1 when any row is ERROR."""
from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workflow_guard as wg  # noqa: E402

SKIP_PROJECTS = {"virtual_marketing_company"}
DEPLOY_RE = re.compile(r"deploy|migrate|release", re.I)
NAME_RE = re.compile(r"""^name\s*:\s*(.*)$""")
COMMIT_SUBJECT = ("Switch auto-triggered workflows to manual dispatch "
                  "(operator rule 4: no CI on push, pull request or schedule).")
COMMIT_TRAILER = "Task: CI-OFF"
TODAY = datetime.date.today().isoformat()


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- text rewrite

def split_workflow(raw: bytes) -> tuple[str, bool]:
    bom = raw.startswith(b"\xef\xbb\xbf")
    return raw[3:].decode("utf-8") if bom else raw.decode("utf-8"), bom


def join_workflow(text: str, bom: bool) -> bytes:
    return (b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8")


def find_on_span(lines: list[str]) -> tuple[int, int] | None:
    """(first, last) line index of the top-level `on:` entry, mirroring workflow_guard.triggers()."""
    plain = [l.rstrip("\r") for l in lines]
    for i, line in enumerate(plain):
        m = wg.ON_RE.match(line)
        if not m:
            continue
        rest = re.sub(r"\s+#.*$", "", m.group(1)).strip()
        if rest[:1] in ("[", "{"):
            acc, k = rest, i
            while acc.count("[") + acc.count("{") > acc.count("]") + acc.count("}"):
                k += 1
                if k >= len(plain):
                    return None
                acc += " " + re.sub(r"(^|\s)#.*$", "", plain[k]).strip()
            return i, k
        if rest:
            return i, i
        last = i
        for j in range(i + 1, len(plain)):
            s = plain[j]
            if not s.strip() or s.lstrip().startswith("#"):
                continue
            if len(s) - len(s.lstrip()) > 0:
                last = j
            else:
                break
        return i, last
    return None


def rewrite(text: str) -> str | None:
    lines = text.split("\n")
    span = find_on_span(lines)
    if span is None:
        return None
    a, b = span
    cr = "\r" if lines[a].endswith("\r") else ""
    new = ["on:" + cr, "  workflow_dispatch:" + cr,
           f"# was (disabled by harness ci_triggers_off {TODAY}, operator rule 4):" + cr]
    for l in lines[a:b + 1]:
        body = l.rstrip("\r")
        new.append(("# " + body if body.strip() else "#") + cr)
    return "\n".join(lines[:a] + new + lines[b + 1:])


def workflow_name(text: str) -> str:
    for l in text.splitlines():
        m = NAME_RE.match(l)
        if m:
            return m.group(1).strip().strip("\"'")
    return ""


# ---------------------------------------------------------------- git state

def git_dir_has(repo: Path, name: str) -> bool:
    r = git(repo, "rev-parse", "--git-path", name)
    if r.returncode != 0:
        return False
    p = Path(r.stdout.strip())
    if not p.is_absolute():
        p = repo / p
    return p.exists()


def unsafe_state(repo: Path) -> str | None:
    if git(repo, "symbolic-ref", "-q", "HEAD").returncode != 0:
        return "detached HEAD"
    for n in ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
        if git_dir_has(repo, n):
            return f"merge/rebase in progress ({n})"
    return None


def dirty_paths(repo: Path, rels: list[str]) -> list[str]:
    r = git(repo, "status", "--porcelain", "--untracked-files=all", "--", *rels)
    return [l[3:] for l in r.stdout.splitlines() if l.strip()]


def log_path(repo: Path, name: str) -> str:
    for rel in (f"core/{name}", name):
        if (repo / rel).is_file():
            return rel
    return f"core/{name}"


# ---------------------------------------------------------------- per repo

def raw_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Bytes-safe git call (blob contents)."""
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True)


def status_map(repo: Path) -> dict[str, str]:
    out = git(repo, "status", "--porcelain=v1", "-uall", "-z").stdout
    return {e[3:]: e[:2] for e in out.split("\0") if len(e) > 3}


def stage_map(repo: Path) -> dict[str, str]:
    out = git(repo, "ls-files", "--stage", "-z").stdout
    return {e.split("\t", 1)[1]: e.split("\t", 1)[0] for e in out.split("\0") if "\t" in e}


def refresh_index(repo: Path, ita: list[str]) -> None:
    """Index := HEAD (working tree untouched), then re-apply intent-to-add entries that git reset drops."""
    r = git(repo, "reset", "-q")
    if r.returncode != 0:
        raise RuntimeError("git reset failed: " + r.stderr.strip()[-200:])
    for p in ita:
        if (repo / p).is_file() and git(repo, "cat-file", "-e", f"HEAD:{p}").returncode != 0:
            r = git(repo, "add", "-N", "--", p)
            if r.returncode != 0:
                raise RuntimeError(f"git add -N {p} failed: " + r.stderr.strip()[-200:])


def merge_into_working(repo: Path, old: str, new: str, rel: str, tmp: Path, backups: dict) -> None:
    p = repo / rel
    is_log = rel.endswith("_LOG.md")
    base = raw_git(repo, "cat-file", "blob", f"{old}:{rel}")
    theirs = raw_git(repo, "cat-file", "blob", f"{new}:{rel}").stdout
    base_b = base.stdout if base.returncode == 0 else b""
    if not p.exists():
        if base.returncode == 0:
            raise RuntimeError(f"{rel}: deleted in working copy but changed in the commit")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(theirs)  # new file only in the commit (e.g. the LIVE report), absent locally
        return
    if base.returncode != 0 and not is_log:
        return  # untracked local file that the commit also adds: keep the local one
    cur = p.read_bytes()
    crlf = b"\r\n" in cur and b"\r\n" not in base_b  # working copy CRLF, blobs LF
    if crlf:
        p.write_bytes(cur.replace(b"\r\n", b"\n"))
    bt, nt = tmp / "base", tmp / "new"
    bt.write_bytes(base_b)
    nt.write_bytes(theirs)
    m = subprocess.run(["git", "merge-file", *(["--union"] if is_log else []), str(p), str(bt), str(nt)],
                       capture_output=True, text=True)
    if m.returncode != 0:
        raise RuntimeError(f"merge conflict in {rel} (rc={m.returncode})")
    if crlf:
        p.write_bytes(p.read_bytes().replace(b"\n", b"\r\n"))


LOCK_WAIT_SECONDS = 30.0
LOCK_BUSY = "another ci_triggers_off run holds the lock"
INTERRUPTED = "interrupted run found; use --recover"
PHASES = ("prepared", "branch-moved", "files-merged", "index-refreshed", "done")


def common_dir(repo: Path) -> Path:
    r = git(repo, "rev-parse", "--git-common-dir")
    p = Path(r.stdout.strip())
    return (p if p.is_absolute() else repo / p).resolve()


def _try_lock(fd: int) -> bool:
    """One non-blocking exclusive OS lock attempt on byte 0 of fd. The kernel drops the lock when the holder dies."""
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, 0)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, 0)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass  # closing the descriptor releases the lock anyway


@contextlib.contextmanager
def repo_lock(common: Path, timeout: float = LOCK_WAIT_SECONDS):
    """Exclusive OS lock on byte 0 of <common>/ci_triggers_off.lock (same pattern as plan_lock in plan_import_ledger_md.py).
    The file is never deleted; the kernel releases the lock when the holder exits or dies. TimeoutError after `timeout` s."""
    lock = common / "ci_triggers_off.lock"
    deadline = time.monotonic() + timeout
    fd = os.open(lock, os.O_RDWR | os.O_CREAT)
    try:
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\n")  # byte 0 must exist
        except OSError:
            pass
        while not _try_lock(fd):
            if time.monotonic() > deadline:
                raise TimeoutError(LOCK_BUSY)
            time.sleep(0.05)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def journal_path(repo: Path) -> Path:
    return common_dir(repo) / "ci_triggers_off.journal.json"


def write_journal(jp: Path, j: dict) -> None:
    tmp = jp.with_name(jp.name + ".tmp")
    tmp.write_text(json.dumps(j, indent=1), encoding="utf-8", newline="\n")
    for i in range(20):  # os.replace is briefly refused on Windows
        try:
            os.replace(tmp, jp)
            return
        except OSError:
            if i == 19:
                raise
            time.sleep(0.05)


def set_phase(jp: Path, j: dict, phase: str) -> None:
    j["phase"] = phase
    write_journal(jp, j)
    if os.environ.get("HARNESS_CI_OFF_FAIL_AFTER") == phase:  # test-only fault injection: simulate a crash
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(70)


def rollback(repo: Path, j: dict) -> list[str]:
    """Undo a (partial) dirty run from its journal. Returns errors; empty means the pre-run state is back."""
    branch, old, new = j["branch"], j["old"], j["new"]
    ref = f"refs/heads/{branch}"
    cur = git(repo, "rev-parse", "-q", "--verify", ref).stdout.strip()
    if cur == new and new != old:
        if git(repo, "update-ref", ref, old, new).returncode != 0:  # compare-and-swap
            return ["branch rollback FAILED"]
    elif cur != old:
        return [f"branch {branch} is at {cur[:7]}, expected {new[:7]} or {old[:7]}; left alone"]
    errs: list[str] = []
    if j["phase"] != "prepared":  # before the branch move nothing in the working tree was touched
        bdir = Path(j["backup_dir"])
        for f, name in j["files"].items():
            p = repo / f
            try:
                if name is None:
                    if p.exists():
                        p.unlink()
                else:
                    p.write_bytes((bdir / name).read_bytes())
            except OSError as e:
                errs.append(f"restore of {f} failed: {e}")
    try:
        refresh_index(repo, j["ita"])
    except Exception as e:  # noqa: BLE001
        errs.append(f"index restore failed: {e}")
    return errs


def cleanup_run(repo: Path, j: dict, jp: Path | None) -> None:
    """Remove the temp worktree, the private ref, the backups and (last) the journal."""
    wt = j.get("worktree")
    if wt:
        git(repo, "worktree", "remove", "--force", wt)
        git(repo, "worktree", "prune")
        shutil.rmtree(wt, ignore_errors=True)
    if j.get("ref") and git(repo, "rev-parse", "-q", "--verify", j["ref"]).returncode == 0:
        git(repo, "update-ref", "-d", j["ref"])  # private ref, not a branch
    for d in (j.get("backup_dir"), j.get("tmpdir")):
        if d:
            shutil.rmtree(d, ignore_errors=True)
    if jp is not None:
        jp.unlink(missing_ok=True)


def drift(repo: Path, j: dict) -> str:
    notes = []
    if status_map(repo) != j["snap_status"]:
        notes.append("status differs from the pre-run snapshot")
    if stage_map(repo) != j["snap_stage"]:
        notes.append("stage differs from the pre-run snapshot")
    return ("; " + "; ".join(notes)) if notes else ""


def recover(repo: Path) -> str:
    jp = journal_path(repo)
    if not jp.is_file():
        return "SKIP no interrupted run (no journal)"
    try:
        j = json.loads(jp.read_text(encoding="utf-8"))
        j["phase"], j["files"], j["old"], j["new"], j["branch"]  # noqa: B018 - shape check
    except (OSError, ValueError, KeyError) as e:
        return f"ERROR journal {jp} unreadable: {e}"
    if j["phase"] == "done":
        cleanup_run(repo, j, jp)
        return f"RECOVERED run had completed at {j['new'][:7]}; journal removed"
    errs = rollback(repo, j)
    if errs:
        return "ERROR recovery incomplete, journal kept: " + "; ".join(errs)
    extra = drift(repo, j)
    cleanup_run(repo, j, jp)
    return f"RECOVERED from phase {j['phase']}: branch {j['branch']} at {j['old'][:7]}, {len(j['files'])} file(s) restored{extra}"


def process_dirty(repo: Path, include_deploy: bool, scratch: Path) -> str:
    """Switch triggers off in a repo whose working tree holds uncommitted work, without touching that work.
    Caller holds the repo lock and has checked that there is no journal."""
    if git(repo, "diff", "--cached", "--quiet").returncode != 0:
        return "SKIP staged changes present (--dirty-ok needs an empty index)"
    why = unsafe_state(repo)
    if why:
        return f"SKIP {why}"
    branch = git(repo, "symbolic-ref", "-q", "--short", "HEAD").stdout.strip()
    old = git(repo, "rev-parse", "HEAD").stdout.strip()
    # refresh stat info first: with core.autocrlf a file that differs only in line endings shows ' M' until the
    # index stat cache is refreshed, which made the before/after comparison fail; this never changes index content
    raw_git(repo, "update-index", "-q", "--refresh")
    snap_status, snap_stage = status_map(repo), stage_map(repo)
    ita = [p for p, c in snap_status.items() if c == " A"]
    # ' M' that is only a line-ending difference (no content change once CR at end of line is ignored): when the tool
    # rewrites such a file, git sees it clean afterwards, which is not lost work
    eol_only = {p for p, c in snap_status.items()
                if c == " M" and git(repo, "diff", "--quiet", "--ignore-cr-at-eol", "--", p).returncode == 0}
    scratch.mkdir(parents=True, exist_ok=True)
    tag = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    run = f"{repo.name}-{tag}"
    ref, wt = f"refs/harness/ci-off/{run}", scratch / f"cioff-{run}"
    jp = journal_path(repo)
    if git(repo, "update-ref", ref, old).returncode != 0:
        return "ERROR cannot create private ref " + ref
    a = git(repo, "worktree", "add", "-q", "--detach", str(wt), old)
    if a.returncode == 0:  # commit on the private ref, never on a branch
        a = git(wt, "symbolic-ref", "HEAD", ref)
    j: dict = {"version": 1, "repo": str(repo), "branch": branch, "old": old, "new": old, "ref": ref,
               "worktree": str(wt), "backup_dir": str(scratch / f"cioff-backup-{run}"), "files": {},
               "ita": ita, "snap_stage": snap_stage, "snap_status": snap_status, "phase": "prepared"}
    if a.returncode != 0:
        cleanup_run(repo, j, None)
        return "ERROR worktree add failed: " + a.stderr.strip()[-300:]
    tmpdir = Path(tempfile.mkdtemp(dir=scratch, prefix="cioff-merge-"))
    j["tmpdir"] = str(tmpdir)
    result = ""
    journaled = False
    try:
        try:
            inner = _process(wt, include_deploy, False)
            if not inner["status"].startswith("CHANGED"):
                return inner["status"] if inner["status"] == "CLEAN" or inner["status"].startswith(("ERROR", "SKIP")) \
                    else "ERROR " + inner["status"]
            new = git(wt, "rev-parse", "HEAD").stdout.strip()
            if new == old:
                return "ERROR tool made no commit in the temp worktree"
            files = [f for f in git(repo, "diff", "--name-only", "-z", old, new).stdout.split("\0") if f]
            bdir = Path(j["backup_dir"])
            bdir.mkdir(parents=True, exist_ok=True)
            for n, f in enumerate(files):  # byte backups outside the repo, before anything is touched
                p = repo / f
                if p.is_file():
                    (bdir / str(n)).write_bytes(p.read_bytes())
                    j["files"][f] = str(n)
                else:
                    j["files"][f] = None
            j["new"] = new
            write_journal(jp, j)
            journaled = True
            set_phase(jp, j, "prepared")
            u = git(repo, "update-ref", f"refs/heads/{branch}", new, old)  # fails if HEAD moved
            if u.returncode != 0:
                journaled = False
                cleanup_run(repo, j, jp)
                return "ERROR branch moved meanwhile, nothing changed: " + u.stderr.strip()[-200:]
            set_phase(jp, j, "branch-moved")
            for f in files:
                merge_into_working(repo, old, new, f, tmpdir, {})
            set_phase(jp, j, "files-merged")
            refresh_index(repo, ita)
            set_phase(jp, j, "index-refreshed")
            after, stage_after = status_map(repo), stage_map(repo)
            for p in set(snap_status) | set(after):
                if snap_status.get(p) != after.get(p) and not (p in files and snap_status.get(p) == "??")                         and not (p in files and p in eol_only and after.get(p) is None):
                    raise RuntimeError(f"status changed for {p}: {snap_status.get(p)!r} -> {after.get(p)!r}")
            for p in set(snap_stage) | set(stage_after):
                if p not in files and snap_stage.get(p) != stage_after.get(p):
                    raise RuntimeError(f"index entry changed for {p}")
            set_phase(jp, j, "done")
            result = f"CHANGED {new[:7]} (dirty merge into {', '.join(files)})"
            journaled = False
            try:
                jp.unlink(missing_ok=True)  # only after the final status/stage check passed
            except (OSError, PermissionError):
                pass  # unlink failure does not invalidate the successful commit
            shutil.rmtree(j["backup_dir"], ignore_errors=True)
        except Exception as e:  # noqa: BLE001 - restore everything
            if not journaled:
                result = f"ERROR {e}"
            else:
                errs = rollback(repo, j)
                if errs:
                    result = f"ERROR {e}; rollback incomplete, journal kept (use --recover): " + "; ".join(errs)
                else:
                    cleanup_run(repo, j, jp)
                    result = f"ERROR {e}; working files restored, branch back at {old[:7]}"
        return result
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        git(repo, "worktree", "remove", "--force", str(wt))
        git(repo, "worktree", "prune")
        if git(repo, "rev-parse", "-q", "--verify", ref).returncode == 0:
            git(repo, "update-ref", "-d", ref)  # private ref: no leftover branch is ever created


def process(repo: Path, include_deploy: bool, dry: bool, dirty_ok: bool = False,
            scratch: Path = Path(r"D:\tmp\scratch_worktrees"), recover_run: bool = False) -> dict:
    """Locked entry point: one run per repo at a time (OS lock in the git common dir), journal check, optional --recover."""
    if dry:
        return _process(repo, include_deploy, dry, dirty_ok, scratch)
    row = {"repo": repo.name, "branch": "", "files": "", "trig": "", "status": ""}
    b = git(repo, "symbolic-ref", "-q", "--short", "HEAD")
    row["branch"] = b.stdout.strip() if b.returncode == 0 else "(detached)"
    try:
        with repo_lock(common_dir(repo)):
            if recover_run:
                row["status"] = recover(repo)
            elif journal_path(repo).exists():
                row["status"] = f"SKIP {INTERRUPTED}"
            else:
                return _process(repo, include_deploy, dry, dirty_ok, scratch)
    except TimeoutError:
        row["status"] = f"SKIP {LOCK_BUSY}"
    return row


def _process(repo: Path, include_deploy: bool, dry: bool, dirty_ok: bool = False,
             scratch: Path = Path(r"D:\tmp\scratch_worktrees")) -> dict:
    row = {"repo": repo.name, "branch": "", "files": "", "trig": "", "status": ""}
    b = git(repo, "symbolic-ref", "-q", "--short", "HEAD")
    row["branch"] = b.stdout.strip() if b.returncode == 0 else "(detached)"

    wf_dir = repo / ".github" / "workflows"
    targets: list[tuple[str, str, bytes, bool, set[str]]] = []  # rel, new text, raw, bom, hit
    kept: list[str] = []
    for p in sorted(wf_dir.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in (".yml", ".yaml"):
            continue
        rel = p.relative_to(repo).as_posix()
        try:
            raw = p.read_bytes()
            text, bom = split_workflow(raw)
        except UnicodeDecodeError:
            row["status"] = f"ERROR {rel}: not valid UTF-8"
            return row
        hit = wg.triggers(text) & set(wg.BAD)
        if not hit:
            continue
        if not include_deploy and (DEPLOY_RE.search(p.name) or DEPLOY_RE.search(workflow_name(text))):
            kept.append(p.name)
            continue
        targets.append((rel, text, raw, bom, hit))

    kept_note = f" (kept: {', '.join(kept)})" if kept else ""
    if not targets:
        row["files"] = kept_note.strip()
        row["status"] = "CLEAN"
        return row
    row["files"] = ", ".join(Path(t[0]).name for t in targets) + kept_note
    row["trig"] = "; ".join(f"{Path(t[0]).name}: {'+'.join(sorted(t[4]))}" for t in targets)

    # safety
    why = unsafe_state(repo)
    dlog, llog = log_path(repo, "DECISION_LOG.md"), log_path(repo, "LESSONS_LOG.md")
    use_dirty = False
    if why is None:
        dirty = dirty_paths(repo, [t[0] for t in targets] + [dlog, llog])
        if dirty and not dirty_ok:
            why = "uncommitted changes in " + ", ".join(dirty)
        elif dirty:
            use_dirty = True
    if why is None:
        for rel in (dlog, llog):
            if git(repo, "check-ignore", "-q", rel).returncode == 0:
                why = f"{rel} is ignored by git"
    if why:
        row["status"] = f"SKIP {why}"
        return row

    # rewrite in memory and verify
    new_files: list[tuple[str, bytes, bytes]] = []
    for rel, text, raw, bom, hit in targets:
        new = rewrite(text)
        if new is None:
            row["status"] = f"ERROR {rel}: cannot locate on: block"
            return row
        left = wg.triggers(new) & set(wg.BAD)
        if left or wg.triggers(new) != {"workflow_dispatch"}:
            row["status"] = f"ERROR {rel}: triggers after rewrite {sorted(wg.triggers(new))}"
            return row
        new_files.append((rel, raw, join_workflow(new, bom)))
    if dry:
        row["status"] = "WOULD CHANGE" + (" (dirty merge)" if use_dirty else "")
        return row
    if use_dirty:
        row["status"] = process_dirty(repo, include_deploy, scratch)
        return row

    originals: dict[str, bytes | None] = {}
    touched: list[str] = []

    def restore() -> None:
        for rel, old in originals.items():
            p = repo / rel
            if old is None:
                if p.exists():
                    p.unlink()
            else:
                p.write_bytes(old)

    try:
        for rel, raw, newb in new_files:
            originals[rel] = raw
            (repo / rel).write_bytes(newb)
            touched.append(rel)
        details = "; ".join(f"{Path(t[0]).name} (was: {', '.join(sorted(t[4]))})" for t in targets)
        names = ", ".join(t[0] for t in targets)
        entries = {
            dlog: (f"## {TODAY} CI-OFF: workflows switched to manual dispatch\n\n"
                   f"Operator rule 4 (no CI on push, pull request or schedule). Changed `on:` to `workflow_dispatch` in "
                   f"{names}. Original triggers: {details}. The original trigger blocks are kept as comments in each file.\n"),
            llog: f"- SUCCESS {TODAY} CI-OFF: switched {len(targets)} workflow(s) to manual dispatch ({names}); "
                  f"workflow_guard re-parse shows no auto trigger.\n",
        }
        for rel, entry in entries.items():
            p = repo / rel
            old = p.read_bytes() if p.is_file() else None
            originals[rel] = old
            p.parent.mkdir(parents=True, exist_ok=True)
            crlf = old is not None and b"\r\n" in old
            body = entry.replace("\n", "\r\n") if crlf else entry
            base = old or b""
            if base and not base.endswith(b"\n"):
                base += b"\r\n" if crlf else b"\n"
            if base and rel.endswith("DECISION_LOG.md"):
                base += b"\r\n" if crlf else b"\n"
            p.write_bytes(base + body.encode("utf-8"))
            touched.append(rel)
        r = git(repo, "add", "--", *touched)
        if r.returncode != 0:
            raise RuntimeError("git add failed: " + r.stderr.strip()[-300:])
        c = git(repo, "commit", "-m", COMMIT_SUBJECT, "-m", COMMIT_TRAILER, "--", *touched)
        if c.returncode != 0:
            git(repo, "reset", "-q", "--", *touched)
            raise RuntimeError("git commit failed: " + (c.stdout + c.stderr).strip()[-300:].replace("\n", " | "))
        sha = git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
        row["status"] = f"CHANGED {sha}"
    except Exception as e:  # noqa: BLE001 - report and restore
        restore()
        row["status"] = f"ERROR {e}"
    return row


def print_table(rows: list[dict]) -> None:
    head = ["repo", "branch", "files changed", "triggers removed", "status"]
    data = [[r["repo"], r["branch"], r["files"], r["trig"], r["status"]] for r in rows]
    widths = [max(len(head[i]), *(len(d[i]) for d in data)) if data else len(head[i]) for i in range(5)]
    fmt = " | ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*head))
    print("-+-".join("-" * w for w in widths))
    for d in data:
        print(fmt.format(*d))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--project")
    ap.add_argument("--include-deploy", action="store_true")
    ap.add_argument("--root", default=r"D:\Repo")
    ap.add_argument("--dirty-ok", action="store_true",
                    help="repo has uncommitted work: commit in a temp worktree and 3-way merge into the working copy")
    ap.add_argument("--scratch", default=r"D:\tmp\scratch_worktrees", help="folder for the temp worktree (--dirty-ok)")
    ap.add_argument("--recover", action="store_true",
                    help="roll back an interrupted --dirty-ok run from its journal (branch, files, index)")
    a = ap.parse_args()
    root = Path(a.root)
    rows = []
    for d in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not d.is_dir() or d.name.lower() in SKIP_PROJECTS:
            continue
        if a.project and d.name.lower() != a.project.lower():
            continue
        if not (d / ".git").exists() or not (d / ".github" / "workflows").is_dir():
            continue
        try:
            rows.append(process(d, a.include_deploy, a.dry_run, a.dirty_ok, Path(a.scratch), a.recover))
        except (OSError, PermissionError) as e:
            rows.append({"repo": d.name, "branch": "", "files": "", "trig": "", "status": f"ERROR {type(e).__name__}: {e}"})
    print_table(rows)
    return 1 if any(r["status"].startswith("ERROR") for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())

