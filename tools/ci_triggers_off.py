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

Every git call has a hard time limit (GIT_TIMEOUT). On Windows each git process runs in a Job Object with
KILL_ON_JOB_CLOSE, so a timeout kills the whole tree including processes that git hooks started (fallback when the job
cannot be set up: System32 taskkill /T /F, said in the timeout message); elsewhere git starts in its own session and the
process group is killed. The whole run has a time budget (RUN_BUDGET_SECONDS, --budget): projects not reached get an ERROR
row. The budget is soft: it bounds when new projects start, and a git call after the deadline still gets BUDGET_FLOOR_SECONDS.
Ctrl-C prints the rows computed so far.

Usage: ci_triggers_off.py [--dry-run] [--project NAME] [--include-deploy] [--root D:\\Repo] [--dirty-ok] [--scratch DIR] [--recover]
                          [--budget SECONDS]
Exit code 1 when any row is ERROR (130 after Ctrl-C)."""
from __future__ import annotations

import argparse
import contextlib
import datetime
import glob
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
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


GIT_TIMEOUT = 120  # seconds for every git and `git merge-file` call; one constant
RUN_BUDGET_SECONDS = 1800.0  # overall time budget of one run (all projects); --budget overrides, 0 = no limit
TASKKILL = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")  # not via PATH or cwd


class GitTimeout(OSError):
    """A git subprocess did not finish within GIT_TIMEOUT. Never a TimeoutError: process() maps that to 'lock busy'.
    An OSError, so main() reports the repo as ERROR; it is never treated as success.
    kill_confirmed is True only when the process tree is known dead (job terminated or taskkill/killpg succeeded and the
    launcher was reaped); callers must not assume git stopped writing when it is False."""

    def __init__(self, msg: str = "", kill_confirmed: bool = False):
        super().__init__(msg)
        self.kill_confirmed = kill_confirmed


BUDGET_FLOOR_SECONDS = 10.0  # a git call is never given less than this by the run budget (still capped by GIT_TIMEOUT)
_RUN_IN_FLIGHT = False  # True while a _run git call may be alive; cleared on a normal exit or a confirmed kill
RUN_DEADLINE: float | None = None  # time.monotonic() value at which the run budget is spent; set by main()
JOB_FAILURES: list[str] = []  # why a Job Object could not be set up (first one is printed once by main())
_WIN_API = None


def _win_job_api():
    """ctypes bindings for the Windows Job Object calls, built once (Windows only)."""
    global _WIN_API
    if _WIN_API is None:
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOp", "WriteOp", "OtherOp", "ReadBytes", "WriteBytes", "OtherBytes")]

        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.restype, k.CreateJobObjectW.argtypes = wintypes.HANDLE, [wintypes.LPVOID, wintypes.LPCWSTR]
        k.SetInformationJobObject.restype = wintypes.BOOL
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        k.AssignProcessToJobObject.restype = wintypes.BOOL
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.TerminateJobObject.restype, k.TerminateJobObject.argtypes = wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT]
        k.OpenProcess.restype, k.OpenProcess.argtypes = wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.CloseHandle.restype, k.CloseHandle.argtypes = wintypes.BOOL, [wintypes.HANDLE]
        _WIN_API = types.SimpleNamespace(ctypes=ctypes, k=k, Extended=Extended)
    return _WIN_API


JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
PROCESS_SET_QUOTA_AND_TERMINATE = 0x0100 | 0x0001  # what AssignProcessToJobObject needs on the process handle


def _job_set_flags(w, job, flags: int) -> bool:
    info = w.Extended()
    info.BasicLimitInformation.LimitFlags = flags
    return bool(w.k.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, w.ctypes.byref(info),
                                            w.ctypes.sizeof(info)))


def _job_attach(pid: int):
    """Put process `pid` into a new Job Object that kills every member when the job closes. Returns (handle, "") or
    (None, why) when job creation or assignment failed (the caller then falls back to taskkill). The child is assigned just
    after it started; git needs many milliseconds before it can start a hook, so no hook escapes in practice."""
    job = None
    try:
        w = _win_job_api()
        job = w.k.CreateJobObjectW(None, None)
        if not job:
            return None, f"CreateJobObject failed (winerror {w.ctypes.get_last_error()})"
        if not _job_set_flags(w, job, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE):
            raise OSError(f"SetInformationJobObject failed (winerror {w.ctypes.get_last_error()})")
        ph = w.k.OpenProcess(PROCESS_SET_QUOTA_AND_TERMINATE, False, pid)
        if not ph:
            raise OSError(f"OpenProcess failed (winerror {w.ctypes.get_last_error()})")
        try:
            if not w.k.AssignProcessToJobObject(job, ph):
                raise OSError(f"AssignProcessToJobObject failed (winerror {w.ctypes.get_last_error()})")
        finally:
            w.k.CloseHandle(ph)
        return job, ""
    except Exception as e:  # noqa: BLE001 - any failure means "no job"; the caller falls back
        if job:
            try:
                _win_job_api().k.CloseHandle(job)
            except Exception:  # noqa: BLE001
                pass
        return None, f"{type(e).__name__}: {e}"


def _job_terminate(job) -> bool:
    try:
        return bool(_win_job_api().k.TerminateJobObject(job, 1))
    except Exception:  # noqa: BLE001
        return False


def _job_close(jb: dict, keep_survivors: bool) -> None:
    """Close the job handle; never raises. keep_survivors=True (git finished normally) first clears KILL_ON_JOB_CLOSE so
    a deliberate background process of git (for example the fsmonitor daemon) is not killed by the close; on a timeout
    the flag stays and the close itself is a second kill. If the flag cannot be cleared the handle is NOT closed: it is
    leaked until this tool exits, where Windows closes it with KILL_ON_JOB_CLOSE still set, so such a daemon then dies at
    tool exit (not at once). The reason is recorded in JOB_FAILURES and printed once.
    jb is the job box {"h": handle or None, "result": ...}: the handle is set to None here, so a second call (or a kill
    after the close) never touches a closed handle."""
    job, jb["h"] = jb.get("h"), None
    if job is None:
        return
    try:
        w = _win_job_api()
        if keep_survivors and not _job_set_flags(w, job, 0):
            if not JOB_FAILURES:
                JOB_FAILURES.append(f"could not clear KILL_ON_JOB_CLOSE (winerror {w.ctypes.get_last_error()}); "
                                    "background processes of git die when this tool exits")
            return  # leak the handle until this process exits
        w.k.CloseHandle(job)
    except Exception:  # noqa: BLE001
        pass


def _kill_tree(p, jb: dict, why: str) -> tuple[bool, str]:
    """Kill the process tree of p and reap p. Returns (confirmed dead, extra message text). Windows: System32 taskkill
    /T /F on the root first, then TerminateJobObject (kills processes started by git hooks too); when there is no usable
    job only taskkill counts, which can miss a hook process, and the message says so. POSIX: SIGKILL to the process
    group (git was started in its own session).
    Idempotent: the first result is kept in jb["result"] and returned by every later call without touching anything. A
    root that is already reaped (p.returncode is set) is never signalled again, because its pid may have been reused."""
    if jb.get("result") is not None:
        return jb["result"]
    dead, note = True, ""
    reaped = p.returncode is not None
    job = jb.get("h")
    if os.name != "nt":
        if not reaped:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # the group is already gone
            except OSError:
                dead = False
    else:
        # 1. taskkill the tree of the root pid FIRST, while the root is alive: cmd\git.exe is a launcher that can start
        #    mingw64/bin/git.exe before AssignProcessToJobObject ran, and that child is outside the job. Once the root is
        #    dead taskkill /T cannot find its children (exit 128). p is not reaped yet, so the pid cannot be reused.
        tk_dead = reaped
        if not reaped:
            try:
                t = subprocess.run([TASKKILL, "/T", "/F", "/PID", str(p.pid)], capture_output=True, timeout=20)
                tk_dead = t.returncode in (0, 128) or p.poll() is not None  # 128 = pid not found = already gone
            except (OSError, subprocess.SubprocessError):
                pass
        # 2. then terminate the job: kills everything that was assigned, including processes started by hooks
        done = job is not None and _job_terminate(job)
        if done:
            dead = True
        else:
            dead = tk_dead
            if job is not None:
                why = "TerminateJobObject failed"
            if not reaped:
                note = f"; no Job Object ({why}): relied on taskkill /T /F, which can miss a process started by a git hook"
    if not reaped:
        try:
            p.kill()
        except OSError:
            pass
        try:
            p.communicate(timeout=10)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            dead = False
    jb["result"] = (dead, note)
    return dead, note


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """Run a command with a hard time limit that also kills grandchildren (on Windows subprocess.run kills only the
    launcher; the real git keeps running and holds index.lock). Windows: the process runs in a Job Object and a timeout
    terminates the whole job; elsewhere it runs in its own session and the group is killed."""
    posix = os.name != "nt"
    if posix:
        kw["start_new_session"] = True
    limit, cut = GIT_TIMEOUT, False
    if RUN_DEADLINE is not None:  # the run budget caps every call, with a floor so a call is never cut to nothing
        eff = min(GIT_TIMEOUT, max(RUN_DEADLINE - time.monotonic(), BUDGET_FLOOR_SECONDS))
        if eff < GIT_TIMEOUT:
            limit, cut = eff, True
    global _RUN_IN_FLIGHT
    p = None
    jb: dict = {"h": None, "result": None}
    why = ""
    _RUN_IN_FLIGHT = True  # cleared on a normal exit and once the kill is confirmed (see _process' interrupt handler)
    try:
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
            jb["h"], why = (None, "") if posix else _job_attach(p.pid)
            if jb["h"] is None and why and not JOB_FAILURES:
                JOB_FAILURES.append(why)  # only the first reason of the run; main() prints it once
            out, err = p.communicate(timeout=limit)
        except subprocess.TimeoutExpired as e:
            try:
                dead, note = _kill_tree(p, jb, why)
            except Exception as e2:  # noqa: BLE001 - the timeout must still be reported
                dead, note = False, f"; kill failed ({type(e2).__name__}: {e2})"
            _job_close(jb, keep_survivors=False)
            if not dead:
                note = "; the process tree could not be confirmed dead, a git process (and index.lock) may remain" + note
            what = (f"timeout after {limit:g}s (the run budget is nearly spent; per-call limit {GIT_TIMEOUT}s)" if cut
                    else f"timeout after {GIT_TIMEOUT}s")
            _RUN_IN_FLIGHT = False  # the caller reads e.kill_confirmed
            raise GitTimeout(f"{what}: {' '.join(str(c) for c in cmd[:6])}{note}", kill_confirmed=dead) from e
    except GitTimeout:
        raise
    except BaseException as ex:  # also an interrupt while the Job Object is set up or the timeout is handled
        def tag(value: bool) -> None:
            try:
                ex.ci_kill_confirmed = value  # lets the caller's interrupt handler decide whether files may be restored
            except Exception:  # noqa: BLE001
                pass

        tag(False)  # FIRST: if a second Ctrl-C lands below, the exception that escapes must still not look confirmed
        dead = False  # unknown until the kill below says otherwise (fail closed)
        if p is not None:
            try:  # never raise over the original exception (for example a second Ctrl-C while killing)
                dead, _ = _kill_tree(p, jb, why)
            except BaseException:  # noqa: BLE001
                pass
        try:
            _job_close(jb, keep_survivors=False)
        except BaseException:  # noqa: BLE001
            pass
        tag(dead)
        if dead:
            _RUN_IN_FLIGHT = False
        raise
    _RUN_IN_FLIGHT = False
    _job_close(jb, keep_survivors=True)
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return _run(["git", "-C", str(repo), *args], text=True, encoding="utf-8", errors="replace")


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
    return _run(["git", "-C", str(repo), *args])


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


def merged_bytes(repo: Path, old: str, new: str, rel: str, cur: bytes | None, tmp: Path) -> bytes | None:
    """The bytes the working file should get: a 3-way merge of `cur` (the working bytes read once by the caller, None if
    the file is absent) with old->new, computed entirely in temp files. The working file is never touched here, so a
    conflict or a crash leaves it exactly as it was. None = keep the working file as it is."""
    is_log = rel.endswith("_LOG.md")
    base = raw_git(repo, "cat-file", "blob", f"{old}:{rel}")
    theirs = raw_git(repo, "cat-file", "blob", f"{new}:{rel}").stdout
    base_b = base.stdout if base.returncode == 0 else b""
    if cur is None:
        if base.returncode == 0:
            raise RuntimeError(f"{rel}: deleted in working copy but changed in the commit")
        return theirs  # new file only in the commit (e.g. the LIVE report), absent locally
    if base.returncode != 0 and not is_log:
        return None  # untracked local file that the commit also adds: keep the local one
    crlf = b"\r\n" in cur and b"\r\n" not in base_b  # working copy CRLF, blobs LF
    wt, bt, nt = tmp / "work", tmp / "base", tmp / "new"
    wt.write_bytes(cur.replace(b"\r\n", b"\n") if crlf else cur)
    bt.write_bytes(base_b)
    nt.write_bytes(theirs)
    m = _run(["git", "merge-file", *(["--union"] if is_log else []), str(wt), str(bt), str(nt)], text=True)
    if m.returncode != 0:
        raise RuntimeError(f"merge conflict in {rel} (rc={m.returncode}); the working file was not touched")
    out = wt.read_bytes()
    return out.replace(b"\n", b"\r\n") if crlf else out


def aside_path(p: Path) -> Path:
    """Where swap_in parks a file's current bytes while it is replaced (same folder, so the move is one rename)."""
    return p.with_name(f".{p.name}.cioff-aside")


def _place_noclobber(src: Path, dst: Path) -> bool:
    """Move src to dst only if dst does not exist, in one step; never overwrites. False if dst exists."""
    try:
        if os.name == "nt":
            os.rename(src, dst)  # Windows rename refuses an existing target
        else:
            os.link(src, dst)  # POSIX link refuses an existing target; rename would overwrite
            os.unlink(src)
        return True
    except FileExistsError:
        return False


def _park(p: Path, a: Path) -> str | None:
    """Move p to the parked name a without ever overwriting a. Returns None when parked, else why not (p untouched,
    or p was replaced by a newer file which then keeps its path). FileNotFoundError when p does not exist.
    Windows: one rename, which refuses an existing a. POSIX has no no-clobber rename in the standard library, so:
    link p to a (refuses an existing a), move p to an unguessable name, and drop that name only when it is the same
    file as a; if a newer file had replaced p in between, it goes back to p and a keeps the earlier bytes."""
    if os.name == "nt":
        return None if _place_noclobber(p, a) else f"{a.name} exists; it was not overwritten"
    try:
        os.link(p, a)
    except FileExistsError:
        return f"{a.name} exists; it was not overwritten"
    t2 = p.with_name(f".{p.name}.cioff-park-{secrets.token_hex(8)}")
    try:
        os.rename(p, t2)  # unguessable name: no other writer can have a file there
    except OSError:
        # p vanished or cannot be moved: drop the link we just made unless p is now another file (then a holds the
        # earlier bytes and the caller's message names it)
        try:
            same = os.path.samestat(os.stat(p), os.stat(a))
        except FileNotFoundError:
            same = True  # p is gone: a is only an extra name of bytes the other writer removed
        if same:
            os.unlink(a)
        raise
    s2, sa = os.stat(t2), os.stat(a)
    if (s2.st_ino, s2.st_dev) == (sa.st_ino, sa.st_dev):
        os.unlink(t2)  # only a second name of the parked file; its bytes stay at a
        return None
    if _place_noclobber(t2, p):
        return f"{p.name} was replaced during the run; the newer file kept its path, {a.name} holds the earlier bytes"
    return f"{p.name} was replaced twice during the run; kept as {t2.name} and {p.name}, earlier bytes in {a.name}"


def _drop_parked(a: Path, known: set, tries: int = 6) -> str:
    """Delete a parked copy only when no other process can write to it and it holds bytes in `known`.
    Returns "deleted" (or already gone), "changed" (other bytes: kept) or "kept" (not provably safe: kept).
    Windows: the copy is opened with share mode 0, which fails while any other handle to it is open and blocks every
    new open while ours is held; it is hashed and marked for deletion through that one handle, so no write can land
    between the check and the delete. POSIX cannot exclude a writer that already holds the file open, so the copy is
    always kept there and reported for a manual check."""
    if os.name != "nt":
        return "kept"
    import ctypes
    import msvcrt
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.SetFileInformationByHandle.restype = wintypes.BOOL
    k32.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    invalid = wintypes.HANDLE(-1).value
    generic_read, delete, open_existing, attr_normal, file_disposition_info = 0x80000000, 0x00010000, 3, 0x80, 4
    h = None
    for i in range(tries):
        h = k32.CreateFileW(str(a), generic_read | delete, 0, None, open_existing, attr_normal, None)
        if h not in (None, invalid):
            break
        if ctypes.get_last_error() in (2, 3):  # file or path not found: nothing left to delete
            return "deleted"
        h = None
        if i < tries - 1:
            time.sleep(0.05 * (2 ** i))  # sharing violation: another handle is open (or a scanner holds it briefly)
    if h is None:
        return "kept"
    try:
        fd = msvcrt.open_osfhandle(h, os.O_RDONLY)
    except OSError:
        k32.CloseHandle(h)  # never keep an exclusive handle that would block the user's writer
        return "kept"
    try:
        f = open(fd, "rb")
    except OSError:
        os.close(fd)
        return "kept"
    with f:  # closing f closes the handle
        if hashlib.sha256(f.read()).hexdigest() not in known:
            return "changed"
        flag = ctypes.c_ubyte(1)  # FILE_DISPOSITION_INFO.DeleteFile = TRUE: deleted when our handle closes
        if not k32.SetFileInformationByHandle(h, file_disposition_info, ctypes.byref(flag), 1):
            return "kept"
    return "deleted"


LEFTOVER_ASIDES: list[str] = []  # parked copies that were kept after a completed swap (reported in the row)
KEPT_NOTE = ("; NOTE kept parked copies of known old bytes (not provably safe to delete: another process had them "
             "open, or a POSIX system); check and delete by hand: ")


def _retire_kept(a: Path) -> Path:
    """Move a parked copy that has to be kept to a unique name, so it never blocks a later swap of the same file
    (which needs the parked name free). Never overwrites; returns where the copy is now (a if it could not move)."""
    k = a.with_name(f"{a.name}-kept-{secrets.token_hex(4)}")
    try:
        if _place_noclobber(a, k):
            return k
    except OSError:
        pass  # Windows: another process has it open without delete sharing; it stays at a
    return a


def swap_in(p: Path, data: bytes | None, expect: str | None) -> str | None:
    """Replace p with `data` (None = remove p) only if p holds the bytes with sha256 `expect` (None = p absent), with no
    window in which another writer's bytes can be overwritten:
      0. the new bytes are written to a temp file first, so p is absent only between two renames;
      1. the current file is moved aside in one rename; from that instant any other writer creates a NEW p;
      2. the moved bytes are hashed: not `expect` -> they are put back (never over a newer p) and the swap is refused;
      3. the new bytes are placed only where p does not exist (no-clobber); if someone created p meanwhile, theirs stays;
      4. the parked bytes are removed only through _drop_parked (no other open handle, still `expect`); a copy that
         is kept is reported in LEFTOVER_ASIDES and the swap still counts as done.
    Step 1 never overwrites an existing parked name (_park).
    Returns None on success, else an error; on error no other writer's bytes were lost (they are at p or at the aside)."""
    a = aside_path(p)
    t = p.with_name(f".{p.name}.cioff-{os.getpid()}.tmp")
    try:
        if data is not None:
            t.write_bytes(data)  # step 0
        if expect is None:
            if p.exists():
                return f"{p.name} exists but should not; left as it is"
            if data is None:
                return None
            if not _place_noclobber(t, p):
                return f"{p.name} was created by someone else during the run; theirs kept"
            return None
        if a.exists():
            return (f"{a.name} exists from an earlier swap; check that it holds old bytes of {p.name} (pre-run or "
                    f"merged), delete it by hand, then run again")
        try:
            perr = _park(p, a)  # step 1: never overwrites a
        except FileNotFoundError:
            return f"{p.name} disappeared during the run; nothing written"
        except OSError as e:
            return f"{p.name} could not be moved aside ({e}); nothing written"
        if perr:
            return perr + "; nothing written"
        if _sha(a) != expect:  # step 2: someone changed it before we moved it
            if _place_noclobber(a, p):
                return f"{p.name} changed during the run; it was not overwritten"
            return (f"{p.name} changed during the run and was recreated meanwhile; both kept "
                    f"({a.name} holds the earlier edit, {p.name} the newer one: merge them by hand, then delete {a.name})")
        if data is not None and not _place_noclobber(t, p):  # step 3
            return (f"{p.name} was recreated by someone else during the run; theirs kept, the pre-run bytes are in "
                    f"{a.name} (delete it after checking)")
        r = _drop_parked(a, {expect})  # step 4
        if r == "changed":
            return f"{a.name} was written to while parked; kept for a manual check (compare with {p.name})"
        if r == "kept":
            LEFTOVER_ASIDES.append(str(_retire_kept(a)))
        return None
    finally:
        try:
            t.unlink(missing_ok=True)  # placed temp files are gone already; this only removes an unused one
        except OSError:
            pass


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


def _sha(p: Path) -> str | None:
    """sha256 of a file's bytes, None when it does not exist."""
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def unexpected_edits(repo: Path, j: dict) -> list[str]:
    """Touched files whose current bytes are neither their pre-run bytes nor the bytes our merge wrote, i.e. someone
    edited them after the snapshot. A journal without hashes (older format) is treated as unsafe for every file."""
    pre, merged = j.get("pre_hash"), j.get("merged_hash") or {}
    if pre is None:
        return sorted(j["files"])
    out = []
    for f in j["files"]:
        p = repo / f
        a = aside_path(p)
        known = {pre.get(f), merged.get(f)}
        if p.exists() and a.exists():
            # a swap stopped between placing the new file and deleting the parked copy (or a copy could not be
            # deleted): a known state when both hold our bytes; anything else needs a person
            if _sha(p) not in known or _sha(a) not in known:
                out.append(f)
            continue
        now = _sha(p) if p.exists() else _sha(a)  # a crash inside swap_in leaves the bytes parked at the aside
        if now not in known:
            out.append(f)
    return out


def rollback(repo: Path, j: dict) -> list[str]:
    """Undo a (partial) dirty run from its journal. Returns errors; empty means the pre-run state is back.
    Nothing is changed (branch or files) when a touched file was edited after the snapshot: the journal and the byte
    backups are then kept for a manual merge, so newer work is never overwritten."""
    branch, old, new = j["branch"], j["old"], j["new"]
    ref = f"refs/heads/{branch}"
    if j["phase"] != "prepared":  # before the branch move nothing in the working tree was touched
        changed = unexpected_edits(repo, j)
        if changed:
            git(repo, "update-index", "-q", "--refresh")  # rc 1 just means modified files; makes `git status` accurate
            return [f"{f} changed after the interrupted run (neither its pre-run nor its merged bytes); nothing "
                    f"restored, journal and backups kept in {j['backup_dir']} for a manual merge" for f in changed]
    cur = git(repo, "rev-parse", "-q", "--verify", ref).stdout.strip()
    if cur == new and new != old:
        if git(repo, "update-ref", ref, old, new).returncode != 0:  # compare-and-swap
            return ["branch rollback FAILED"]
    elif cur != old:
        return [f"branch {branch} is at {cur[:7]}, expected {new[:7]} or {old[:7]}; left alone"]
    errs: list[str] = []
    if j["phase"] != "prepared":
        bdir = Path(j["backup_dir"])
        pre, merged = j.get("pre_hash") or {}, j.get("merged_hash") or {}
        for f, name in j["files"].items():
            p = repo / f
            a = aside_path(p)
            for t in p.parent.glob(f".{glob.escape(p.name)}.cioff-*.tmp"):  # temp files of a crashed swap (our new bytes only)
                try:
                    t.unlink()
                except OSError as e:
                    errs.append(f"{f}: could not delete temp file {t.name} ({e}); delete it by hand")
            for t in p.parent.glob(f".{glob.escape(p.name)}.cioff-park-*"):  # POSIX _park crashed after moving p to t
                try:
                    if a.exists() and os.path.samestat(os.stat(t), os.stat(a)):
                        t.unlink()  # only a second name of the parked copy
                    else:
                        errs.append(f"{f}: {t.name} may hold a newer edit made during the run; compare and merge by hand")
                except OSError as e:
                    errs.append(f"{f}: could not check {t.name} ({e})")
            try:
                if not p.exists() and a.exists() and not _place_noclobber(a, p):  # crash inside swap_in: unpark
                    errs.append(f"{f}: could not move {a.name} back; both kept")
                    continue
                if p.exists() and a.exists():
                    # parked copy of known bytes (pre-run bytes are also in the backup): dropped only when provably
                    # safe; otherwise it stays and blocks the restore, which would need the same parked name
                    r = _drop_parked(a, {h for h in (pre.get(f), merged.get(f)) if h})
                    k = _retire_kept(a) if r == "kept" else a
                    if k != a:
                        LEFTOVER_ASIDES.append(str(k))  # known bytes kept under a unique name; the restore can go on
                    elif r != "deleted":
                        errs.append(f"{f}: {a.name} kept ({r}); check it against the backup, delete it by hand")
                        continue
                now = _sha(p)
                if now == pre.get(f):
                    continue  # already the pre-run bytes
                # swap_in refuses (and keeps the other bytes) if p no longer holds our merged bytes at the instant it
                # is moved aside, so there is no check-then-write window here either
                err = swap_in(p, None if name is None else (bdir / name).read_bytes(), merged.get(f))
                if err:
                    errs.append(f"{f}: {err} (backups in {bdir})")
            except OSError as e:
                errs.append(f"restore of {f} failed: {e}")
    try:
        refresh_index(repo, j["ita"])
    except Exception as e:  # noqa: BLE001
        errs.append(f"index restore failed: {e}")
    return errs


def _step(notes: list[str], what: str, fn) -> None:
    """Run one cleanup step on its own: an OSError (including GitTimeout) or a returned error text becomes a note and never
    stops the later steps."""
    try:
        err = fn()
    except OSError as e:
        notes.append(f"{what}: {e}")
        return
    if err:
        notes.append(f"{what}: {err}")


def _git_err(r: subprocess.CompletedProcess) -> str | None:
    """None when the git call succeeded, else its exit code and the end of its stderr."""
    if r.returncode == 0:
        return None
    return f"git exit {r.returncode}: {(r.stderr or '').strip()[-150:]}".replace("\n", " ")


def _rmtree_err(path: Path) -> str | None:
    """Remove a directory tree; None on success, else why not (also when something is still there afterwards)."""
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        return f"{e}; delete it by hand"
    return f"{path} still exists; delete it by hand" if path.exists() else None


def _remove_worktree(repo: Path, wt, notes: list[str]) -> bool:
    """worktree remove, then a shutil.rmtree fallback for a leftover directory (reported if that fails too), then prune.
    Every return code is checked; 'remove' failing because the directory is already gone is not a failure, and a remove
    failure is dropped from the notes when the fallback then removed the directory. Returns True when something is
    left behind (the directory, or a registration that prune could not drop)."""
    wtp = Path(wt)
    left = False

    def remove() -> str | None:
        r = git(repo, "worktree", "remove", "--force", str(wtp))
        return _git_err(r) if r.returncode != 0 and wtp.exists() else None

    n0 = len(notes)
    _step(notes, "worktree remove", remove)
    remove_note = len(notes) > n0

    def leftover() -> str | None:
        nonlocal left, remove_note
        if not wtp.exists():
            return None
        err = _rmtree_err(wtp)
        if err:
            left = True
            return f"leftover directory {wtp} could not be removed ({err})"
        if remove_note:
            del notes[n0]  # the fallback succeeded: the earlier 'worktree remove' failure no longer matters
            remove_note = False
        return None

    _step(notes, "worktree dir", leftover)
    if wtp.exists():
        left = True  # leftover() itself failed with an OSError that _step turned into a note
    n1 = len(notes)
    _step(notes, "worktree prune", lambda: _git_err(git(repo, "worktree", "prune")))
    return left or len(notes) > n1


def _drop_private_ref(repo: Path, ref: str, notes: list[str]) -> bool:
    """Delete the private ref, checking every return code. Returns True when the ref may still exist."""
    n0 = len(notes)

    def drop() -> str | None:
        v = git(repo, "rev-parse", "-q", "--verify", ref)
        if v.returncode == 1:
            return None  # the ref does not exist (any other failure is a real error)
        if v.returncode != 0:
            return _git_err(v)
        return _git_err(git(repo, "update-ref", "-d", ref))  # private ref: no leftover branch is ever created

    _step(notes, "private ref " + ref, drop)
    return len(notes) > n0


def cleanup_run(repo: Path, j: dict, jp: Path | None) -> str:
    """Remove the temp worktree, the private ref, the backups and (last) the journal. Every step checks its result.
    Returns "" or text to append to the row: '; cleanup incomplete: ...' for leftovers and '; WARNING journal ...' when
    the journal could not be deleted (the next run then refuses until --recover removes it)."""
    notes: list[str] = []
    left = False
    if j.get("worktree"):
        left = _remove_worktree(repo, j["worktree"], notes) or left
    if j.get("ref"):
        left = _drop_private_ref(repo, j["ref"], notes) or left
    if not left:  # with a worktree or ref left behind the backups stay too, so a second --recover is as safe as the first
        for d in (j.get("backup_dir"), j.get("tmpdir")):
            if d and Path(d).exists():
                _step(notes, f"remove {d}", lambda d=d: _rmtree_err(Path(d)))
    out = ("; cleanup incomplete: " + "; ".join(notes)) if notes else ""
    if (left or notes) and jp is not None:  # any incomplete cleanup keeps the journal: --recover retries it
        # Callers reach this point only after the rollback succeeded (or when there was nothing to roll back). Rewrite the
        # journal as phase "done" (atomically) so a retry runs only this cleanup: a second rollback could refuse because
        # the operator kept working, and every later run would then SKIP 'interrupted run found' forever.
        if j.get("phase") != "done":
            j["phase"], j["rolled_back"] = "done", True
            try:
                write_journal(jp, j)
            except OSError as e:
                out += f"; journal phase could not be rewritten ({e})"
        return out + "; journal kept, run --recover"
    if jp is not None:  # last
        try:
            jp.unlink(missing_ok=True)
        except OSError as e:
            out += f"; WARNING journal {jp} could not be deleted ({e}); the next run refuses until --recover removes it"
    return out


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
        w = cleanup_run(repo, j, jp)
        if "WARNING journal" in w or "journal kept" in w:
            return f"ERROR run had completed at {j['new'][:7]}{w}"
        if j.get("rolled_back"):
            return f"RECOVERED earlier rollback done, leftovers cleaned; journal removed{w}"
        return f"RECOVERED run had completed at {j['new'][:7]}; journal removed{w}"
    LEFTOVER_ASIDES.clear()
    errs = rollback(repo, j)
    kept = (KEPT_NOTE + ", ".join(LEFTOVER_ASIDES)) if LEFTOVER_ASIDES else ""
    LEFTOVER_ASIDES.clear()
    if errs:
        return "ERROR recovery incomplete, journal kept: " + "; ".join(errs) + kept
    extra = drift(repo, j)
    w = cleanup_run(repo, j, jp)
    return (f"{'ERROR' if 'WARNING journal' in w or 'journal kept' in w else 'RECOVERED'} from phase {j['phase']}: branch {j['branch']} at {j['old'][:7]}, "
            f"{len(j['files'])} file(s) restored{extra}{kept}{w}")


def process_dirty(repo: Path, include_deploy: bool, scratch: Path) -> str:
    """Run _dirty_run, then always remove the temp worktree and the private ref, each step on its own so a git timeout
    in one never skips the others and never replaces the computed result (it is appended as 'cleanup incomplete')."""
    st: dict = {}
    res = "ERROR interrupted"
    try:
        res = _dirty_run(repo, include_deploy, scratch, st)
    except Exception as e:  # noqa: BLE001 - includes GitTimeout from the setup git calls
        res = f"ERROR {e}"
    finally:  # also on KeyboardInterrupt: the private ref and the temp worktree are always removed
        res = _dirty_cleanup(repo, st, res)
    return res


def _dirty_cleanup(repo: Path, st: dict, res: str) -> str:
    notes: list[str] = []
    left = False
    ref, wt = st.get("ref"), st.get("wt")
    if wt:
        left = _remove_worktree(repo, wt, notes) or left
    if ref:
        left = _drop_private_ref(repo, ref, notes) or left
    if st.get("tmp_note"):
        notes.append(st["tmp_note"])
    if notes:
        res += "; cleanup incomplete: " + "; ".join(notes)
    jp = st.get("jp")
    if left:
        # a worktree or ref is left behind: the row is an ERROR and a journal stays so that --recover can retry the
        # cleanup (a finished run has deleted its journal already: write a phase-done one; a rollback journal is kept as is)
        res = res if res.startswith("ERROR") else "ERROR " + res
        try:
            j = st.get("j")
            if j is not None and jp and not Path(jp).exists():
                j["phase"] = "done"
                write_journal(Path(jp), j)
        except (OSError, TypeError, ValueError) as e:
            res += f"; journal for the cleanup retry could not be written ({e})"
    try:
        if res.startswith("ERROR") and jp and Path(jp).exists() and "journal kept" not in res:
            res += "; journal kept, run --recover"
    except OSError:
        pass
    return res


def _dirty_run(repo: Path, include_deploy: bool, scratch: Path, st: dict) -> str:
    """Switch triggers off in a repo whose working tree holds uncommitted work, without touching that work.
    Caller holds the repo lock and has checked that there is no journal. st receives ref and wt for cleanup."""
    LEFTOVER_ASIDES.clear()  # never carry another repo's note into this run
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
    st["ref"], st["wt"], st["jp"] = ref, wt, jp  # recorded BEFORE the git calls: the caller cleans up even after a timeout
    if git(repo, "update-ref", ref, old).returncode != 0:
        return "ERROR cannot create private ref " + ref
    a = git(repo, "worktree", "add", "-q", "--detach", str(wt), old)
    if a.returncode == 0:  # commit on the private ref, never on a branch
        a = git(wt, "symbolic-ref", "HEAD", ref)
    j: dict = {"version": 1, "repo": str(repo), "branch": branch, "old": old, "new": old, "ref": ref,
               "worktree": str(wt), "backup_dir": str(scratch / f"cioff-backup-{run}"), "files": {},
               "ita": ita, "snap_stage": snap_stage, "snap_status": snap_status, "phase": "prepared"}
    st["j"] = j  # lets the caller write a cleanup-retry journal when a worktree or ref cannot be removed
    if a.returncode != 0:
        w = cleanup_run(repo, j, None)  # no journal exists yet, so this never says "journal kept"
        return "ERROR worktree add failed: " + a.stderr.strip()[-300:] + w
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
            j["pre_hash"], j["merged_hash"] = {}, {}
            outs: dict[str, bytes | None] = {}
            for n, f in enumerate(files):  # byte backups outside the repo, before anything is touched
                p = repo / f
                data = p.read_bytes() if p.is_file() else None  # read ONCE: backup, hash and merge use these bytes
                if data is not None:
                    (bdir / str(n)).write_bytes(data)
                    j["files"][f] = str(n)
                else:
                    j["files"][f] = None
                j["pre_hash"][f] = hashlib.sha256(data).hexdigest() if data is not None else None
                # every merge is computed now, in temp files: a conflict stops the run before anything is changed
                outs[f] = merged_bytes(repo, old, new, f, data, tmpdir)
            j["new"] = new
            write_journal(jp, j)
            journaled = True
            set_phase(jp, j, "prepared")
            u = git(repo, "update-ref", f"refs/heads/{branch}", new, old)  # fails if HEAD moved
            if u.returncode != 0:
                journaled = False
                w = cleanup_run(repo, j, jp)
                return "ERROR branch moved meanwhile, nothing changed: " + u.stderr.strip()[-200:] + w
            set_phase(jp, j, "branch-moved")
            for f in files:
                p = repo / f
                out = outs[f]
                if out is None:
                    if _sha(p) != j["pre_hash"][f]:
                        raise RuntimeError(f"{f} changed during the run; it was not overwritten")
                    j["merged_hash"][f] = j["pre_hash"][f]
                    continue
                j["merged_hash"][f] = hashlib.sha256(out).hexdigest()  # journaled BEFORE the write: both states are ours
                write_journal(jp, j)
                err = swap_in(p, out, j["pre_hash"][f])  # no check-then-write window: see swap_in
                if err:
                    raise RuntimeError(f"{f}: {err}")
            set_phase(jp, j, "files-merged")
            refresh_index(repo, ita)
            set_phase(jp, j, "index-refreshed")
            after, stage_after = status_map(repo), stage_map(repo)
            for p in set(snap_status) | set(after):
                if snap_status.get(p) != after.get(p) and not (p in files and snap_status.get(p) == "??")                         and not (p in files and p in eol_only and after.get(p) is None):
                    if (snap_status.get(p) is None and after.get(p) == "??" and ".cioff-aside" in p
                            and any(os.path.normcase(str(Path(x).resolve())) == os.path.normcase(str((repo / p).resolve()))
                                    for x in LEFTOVER_ASIDES)):
                        continue  # our own parked copy that could not be deleted: reported in the row, not a user change
                    raise RuntimeError(f"status changed for {p}: {snap_status.get(p)!r} -> {after.get(p)!r}")
            for p in set(snap_stage) | set(stage_after):
                if p not in files and snap_stage.get(p) != stage_after.get(p):
                    raise RuntimeError(f"index entry changed for {p}")
            set_phase(jp, j, "done")
            result = f"CHANGED {new[:7]} (dirty merge into {', '.join(files)})"
            journaled = False
            try:
                jp.unlink(missing_ok=True)  # only after the final status/stage check passed
            except OSError as e:  # the commit stays valid, but the next run would refuse: say so
                result += f"; WARNING journal {jp} could not be deleted ({e}); run --recover to remove it"
            berr = _rmtree_err(Path(j["backup_dir"]))
            if berr:  # the commit is fine: a note, not an ERROR
                result += f"; cleanup incomplete: backups left at {j['backup_dir']} ({berr})"
        except Exception as e:  # noqa: BLE001 - restore everything
            if not journaled:
                result = f"ERROR {e}"
                if not jp.exists():  # nothing to recover from: do not leave copies of user files in scratch
                    berr = _rmtree_err(Path(j["backup_dir"]))
                    if berr:
                        result += f"; cleanup incomplete: backups left at {j['backup_dir']} ({berr})"
            elif isinstance(e, GitTimeout) and not _kill_settled(repo, e):
                # a git that may still be running could write the index or refs while the rollback runs: leave the
                # journal for --recover (the rollback itself is idempotent and hash-guarded)
                result = (f"ERROR {e}; the timed-out git was not confirmed dead (or .git/index.lock remains), nothing "
                          "rolled back, journal kept (use --recover once no git process runs)")
            else:
                errs = rollback(repo, j)
                if errs:
                    result = f"ERROR {e}; rollback incomplete, journal kept (use --recover): " + "; ".join(errs)
                else:
                    try:
                        w = cleanup_run(repo, j, jp)
                    except OSError as e2:
                        w = f"; cleanup incomplete: {e2}"
                    result = f"ERROR {e}; working files restored, branch back at {old[:7]}{w}"
        if LEFTOVER_ASIDES:  # this repo's parked copies only (the list is cleared when the run starts)
            result += KEPT_NOTE + ", ".join(LEFTOVER_ASIDES)
            LEFTOVER_ASIDES.clear()
        return result
    finally:
        terr = _rmtree_err(tmpdir)  # worktree and ref are removed by process_dirty; the note is added there
        if terr:
            st["tmp_note"] = f"temp folder left at {tmpdir} ({terr})"


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


INDEX_LOCK_WAIT = 2.0  # seconds to wait for .git/index.lock to disappear after a kill


def _lock_gone_after_kill(repo: Path) -> bool:
    """True when <git dir>/index.lock is absent, waiting up to INDEX_LOCK_WAIT s. A git killed in the middle of its work
    leaves its lock behind, so a lock that stays means the kill cannot be treated as clean."""
    r = git(repo, "rev-parse", "--git-path", "index.lock")
    if r.returncode != 0:
        return False
    lp = Path(r.stdout.strip())
    if not lp.is_absolute():
        lp = repo / lp
    deadline = time.monotonic() + INDEX_LOCK_WAIT
    while lp.exists():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def _kill_settled(repo: Path, e: GitTimeout) -> bool:
    """True when the killed git is confirmed dead and its index.lock is gone (waits up to INDEX_LOCK_WAIT s)."""
    try:
        return bool(e.kill_confirmed) and _lock_gone_after_kill(repo)
    except OSError:
        return False


def _interrupted_cleanup(repo: Path, touched: list[str], head0: str, kill_confirmed: bool, restore,
                         written: bool = True) -> str:
    """After a git timeout or Ctrl-C in the clean path: unstage, say what state git may be in, and call restore() only
    when it is provably safe: the killed git is confirmed dead (and its index.lock is gone), HEAD is readable and equal
    to head0 (read before any file was written), and the index reset worked. `written` is True once the first file write
    was started (originals is filled BEFORE write_bytes, so a file may be truncated even when `touched` is still empty).
    Returns the text to append to the row; it says "files restored" only when restore() really ran."""
    if not written:
        return "; interrupted before any file was written, nothing to undo"
    note = ""
    safe = bool(kill_confirmed)
    if safe:
        try:
            if not _lock_gone_after_kill(repo):
                safe = False
                note += f"; index.lock still present {INDEX_LOCK_WAIT:g}s after the kill, a git process may still be running"
        except OSError as e2:
            safe = False
            note += f"; index.lock could not be checked ({e2})"
    else:
        note += "; the interrupted git was not confirmed killed, it may still write"
    if touched:  # `git reset --` without paths would reset the whole index: skipped when nothing could be staged yet
        try:
            rr = git(repo, "reset", "-q", "--", *touched)
            if rr.returncode != 0:
                safe = False
                note += "; index reset failed (" + rr.stderr.strip()[-80:].replace("\n", " ") + "), touched paths may stay staged"
        except OSError as e2:
            safe = False
            note += f"; index reset failed ({e2}), touched paths may stay staged"
    try:
        hr = git(repo, "rev-parse", "HEAD")
        head1 = hr.stdout.strip() if hr.returncode == 0 else ""
        if not head1:
            safe = False
            note += "; HEAD unreadable after the interruption, a commit may have landed"
        elif not head0:
            safe = False
            note += "; HEAD before the run unknown"
        elif head1 != head0:
            safe = False
            note += f"; HEAD moved to {head1[:12]}, the commit landed"
        else:
            note += "; HEAD unchanged, no commit landed"
    except OSError as e2:
        safe = False
        note += f"; HEAD could not be read ({e2}), a commit may have landed"
    try:
        if git_dir_has(repo, "index.lock"):
            note += "; a stale .git/index.lock exists, remove it once no git process runs"
    except OSError:
        pass
    if safe:
        try:
            restore()
            note += "; files restored"
        except OSError as e3:  # keep everything said so far and add the error
            note += f"; files NOT fully restored ({e3}); check git status"
    else:
        note += "; files kept as written (not restored, so the tree matches what may be committed); check git status"
    return note


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
        """Put every original back; a failure on one file does not stop the others. Raises OSError listing the failures."""
        errs = []
        for rel, old in originals.items():
            p = repo / rel
            try:
                if old is None:
                    if p.exists():
                        p.unlink()
                else:
                    p.write_bytes(old)
            except OSError as e:
                errs.append(f"{rel}: {e}")
        if errs:
            raise OSError("; ".join(errs))

    # HEAD before the run is read BEFORE any file is written: a timeout later can then tell whether a commit landed
    hr0 = git(repo, "rev-parse", "HEAD")
    head0 = hr0.stdout.strip() if hr0.returncode == 0 else ""

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
    except GitTimeout as e:  # add or commit hung: unstage, restore files, and say what state git may be in
        row["status"] = f"ERROR {e}{_interrupted_cleanup(repo, touched, head0, e.kill_confirmed, restore, bool(originals))}"
    except KeyboardInterrupt as ki:  # Ctrl-C: the same decision as for a timeout, then the interrupt goes on
        try:
            # _run sets ci_kill_confirmed on every interrupt that passes through it (unknown = False). No attribute means
            # the interrupt hit between git calls (for example inside write_bytes): no git of ours is running.
            kc = getattr(ki, "ci_kill_confirmed", None)
            if kc is None:  # no tag: confirmed only when no _run call is still marked alive
                kc = not _RUN_IN_FLIGHT
            ki.ci_note = _interrupted_cleanup(repo, touched, head0, kc, restore, bool(originals))
        except BaseException as e2:  # noqa: BLE001 - never raise over the original interrupt
            ki.ci_note = f"; interrupt cleanup failed ({type(e2).__name__}: {e2})"
        raise
    except Exception as e:  # noqa: BLE001 - report and restore
        extra = ""
        try:
            restore()
        except OSError as e3:  # never replace the original error
            extra = f"; files NOT fully restored ({e3}); check git status"
        row["status"] = f"ERROR {e}{extra}"
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


def print_job_note() -> None:
    """Say once per run why a Job Object could not be set up (timeouts then used the taskkill fallback)."""
    if JOB_FAILURES:
        print(f"NOTE: Job Object problem in this run: {JOB_FAILURES[0]}. (A failed set-up means timeouts used taskkill /T /F, "
              "which can miss a process started by a git hook.)")


def main() -> int:
    global RUN_DEADLINE
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
    ap.add_argument("--budget", type=float, default=RUN_BUDGET_SECONDS, metavar="SECONDS",
                    help=f"overall time budget of the run (default {RUN_BUDGET_SECONDS:g} s, 0 = no limit); projects not "
                         "started before it is spent get an ERROR row. Soft limit: git calls after the deadline still get "
                         f"{BUDGET_FLOOR_SECONDS:g} s each")
    a = ap.parse_args()
    root = Path(a.root)
    projects = []
    for d in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not d.is_dir() or d.name.lower() in SKIP_PROJECTS:
            continue
        if a.project and d.name.lower() != a.project.lower():
            continue
        if not (d / ".git").exists() or not (d / ".github" / "workflows").is_dir():
            continue
        projects.append(d)
    rows: list[dict] = []
    started = time.monotonic()
    RUN_DEADLINE = started + a.budget if a.budget > 0 else None  # caps every git call (see _run)
    JOB_FAILURES.clear()
    current: Path | None = None
    try:
        for d in projects:
            current = d
            if a.budget > 0 and time.monotonic() - started >= a.budget:  # never skipped silently
                rows.append({"repo": d.name, "branch": "", "files": "", "trig": "",
                             "status": f"ERROR run budget of {a.budget:g}s is spent; this project was not processed"})
                continue
            try:
                rows.append(process(d, a.include_deploy, a.dry_run, a.dirty_ok, Path(a.scratch), a.recover))
            except (OSError, PermissionError) as e:
                rows.append({"repo": d.name, "branch": "", "files": "", "trig": "", "status": f"ERROR {type(e).__name__}: {e}"})
            current = None
    except KeyboardInterrupt as ki:
        note = getattr(ki, "ci_note", "")
        if current is not None:
            try:  # the interrupted project may have left a journal; say how to roll it back
                left = journal_path(current).exists()
            except Exception:  # noqa: BLE001 - never raise from the interrupt handler
                left = False
            if left:
                tail = f"; journal kept, run --recover --project {current.name}"
            elif "files restored" in note or "nothing to undo" in note:
                tail = ""
            else:
                tail = f"; files may be modified or staged; check git status in {current}"
            rows.append({"repo": current.name, "branch": "", "files": "", "trig": "",
                         "status": f"ERROR interrupted (Ctrl-C){note}{tail}"})
        print_table(rows)
        print(f"Interrupted: {len(rows)} of {len(projects)} project(s) have a row above; the rest were not processed.")
        print_job_note()
        return 130
    finally:
        RUN_DEADLINE = None
    print_table(rows)
    print_job_note()
    return 1 if any(r["status"].startswith("ERROR") for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())

