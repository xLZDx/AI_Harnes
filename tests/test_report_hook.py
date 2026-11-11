"""Tests for the pre-commit report hook and report.py --stage (S6-T2). Temp git repos only; real repos and ~/.claude untouched.
Run: python -I tests/test_report_hook.py"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = "C:/Python314/python.exe"
REPORT = "reports/HookProj_PROGRAM_STATUS_LIVE.ru.html"
_TMPS = []
FAILS = []
ENV = dict(os.environ, HARNESS_PYTHON=PY)
ENV.pop("HARNESS_LEDGER", None)


def sh(args, cwd, check=True):
    r = subprocess.run(args, cwd=cwd, env=ENV, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise AssertionError(f"{args} -> {r.returncode}: {r.stdout} {r.stderr}")
    return r


def git(repo, *a, check=True):
    return sh(["git", *a], repo, check)


def ledger_dict():
    return {"version": 1, "project": "HookProj", "approval": None, "events": [], "sprints": [
        {"id": "S1", "title": "S", "state": "open", "tasks": [
            {"id": "X-T1", "title": "t1", "state": "todo", "criteria": ["c"], "commits": []}]}]}


def repo(with_ledger=True):
    d = Path(tempfile.mkdtemp(prefix="rph_"))
    _TMPS.append(d)
    git(d, "init", "-q")
    git(d, "config", "user.name", "T")
    git(d, "config", "user.email", "t@example.invalid")
    git(d, "config", "commit.gpgsign", "false")
    if with_ledger:
        (d / ".harness").mkdir()
        (d / ".harness" / "ledger.json").write_text(json.dumps(ledger_dict()), encoding="utf-8")
    return d


def installed():
    d = repo(with_ledger=False)
    sh([PY, "-I", str(ROOT / "tools" / "build.py"), "--install-project", str(d), "--force"], ROOT)
    (d / ".harness" / "ledger.json").write_text(json.dumps(ledger_dict()), encoding="utf-8")
    return d


def stage(d):
    return sh([PY, "-I", str(ROOT / "tools" / "report.py"), "--stage"], d, check=False)


def commit(d, name, text, msg="change\n\nTask: X-T1"):
    (d / name).write_text(text, encoding="utf-8")
    git(d, "add", name)
    return git(d, "commit", "-q", "-m", msg, check=False)


def files_of(d, rev="HEAD"):
    return git(d, "show", "--name-only", "--format=", rev).stdout.split()


def t_stage_writes_and_stages():
    d = repo()
    r = stage(d)
    assert r.returncode == 0, r.stderr
    assert (d / REPORT).is_file()
    assert REPORT in git(d, "diff", "--cached", "--name-only").stdout.split()


def t_real_commit_includes_report():
    d = installed()
    r = commit(d, "a.txt", "1")
    assert r.returncode == 0, r.stderr
    assert REPORT in files_of(d), files_of(d)
    assert "a.txt" in files_of(d)


def t_handmade_untouched():
    d = installed()
    p = d / REPORT
    p.parent.mkdir()
    p.write_text("<html>mine</html>", encoding="utf-8")
    r = commit(d, "a.txt", "1")
    assert r.returncode == 0, r.stderr
    assert p.read_text(encoding="utf-8") == "<html>mine</html>"
    assert REPORT not in files_of(d)


def t_failure_never_blocks():
    # reports is a plain file -> the report cannot be written
    d = installed()
    (d / "reports").write_text("not a dir", encoding="utf-8")
    assert stage(d).returncode == 1  # real failure is reported (exit 1) but never blocks the commit
    r = commit(d, "a.txt", "1")
    assert r.returncode == 0, r.stderr
    assert git(d, "rev-parse", "--verify", "HEAD", check=False).returncode == 0
    assert "report.py --stage exit 1" in hook_log(d)
    # corrupt ledger
    d2 = installed()
    (d2 / ".harness" / "ledger.json").write_text("{not json", encoding="utf-8")
    assert stage(d2).returncode == 1
    r = commit(d2, "a.txt", "1")
    assert r.returncode == 0, r.stderr
    assert git(d2, "rev-parse", "--verify", "HEAD", check=False).returncode == 0


def t_busy_state_skipped():
    d = repo()
    (d / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n")
    r = stage(d)
    assert r.returncode == 0
    assert "skipped" in r.stdout, r.stdout
    assert not (d / "reports").exists()
    assert git(d, "diff", "--cached", "--name-only").stdout.strip() == ""


def t_second_commit_updates_same_file():
    d = installed()
    assert commit(d, "a.txt", "1").returncode == 0
    assert commit(d, "a.txt", "2", "second\n\nTask: X-T1").returncode == 0
    assert REPORT in files_of(d)
    first = git(d, "show", f"HEAD~1:{REPORT}").stdout
    second = git(d, "show", f"HEAD:{REPORT}").stdout
    assert first != second
    tracked = [f for f in git(d, "ls-files").stdout.split() if f.startswith("reports/")]
    assert tracked == [REPORT], tracked


def t_post_commit_no_report():
    text = (ROOT / "tools" / "githooks" / "post-commit").read_text(encoding="utf-8")
    assert "report.py" not in text


def hook_log(d):
    p = d / ".harness" / "hook-error.log"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def t_corrupt_ledger_keeps_existing_report():
    d = repo()
    assert stage(d).returncode == 0
    git(d, "commit", "-q", "-m", "base", "--no-verify")
    p = d / REPORT
    before = p.read_bytes()
    (d / ".harness" / "ledger.json").write_text("{not json", encoding="utf-8")
    r = stage(d)
    assert r.returncode == 1, (r.returncode, r.stderr)
    assert "not readable" in r.stderr, r.stderr
    assert p.read_bytes() == before
    assert REPORT not in git(d, "diff", "--cached", "--name-only").stdout.split()


def t_corrupt_ledger_no_report_renders_empty_but_exit_1():
    d = repo()
    (d / ".harness" / "ledger.json").write_text("{not json", encoding="utf-8")
    r = stage(d)
    assert r.returncode == 1, (r.returncode, r.stderr)
    assert "not readable" in r.stderr
    assert any((d / "reports").glob("*.html"))


def t_hook_corrupt_ledger_commit_ok_old_report_kept():
    d = installed()
    assert commit(d, "a.txt", "1").returncode == 0
    old = git(d, "show", f"HEAD:{REPORT}").stdout
    (d / ".harness" / "ledger.json").write_text("{not json", encoding="utf-8")
    r = commit(d, "b.txt", "2", "second\n\nTask: X-T1")
    assert r.returncode == 0, r.stderr
    assert git(d, "show", f"HEAD:{REPORT}").stdout == old
    assert "report.py --stage exit 1" in hook_log(d), hook_log(d)


def t_handmade_exit_1_and_logged():
    d = repo()
    p = d / REPORT
    p.parent.mkdir()
    p.write_text("<html>mine</html>", encoding="utf-8")
    r = stage(d)
    assert r.returncode == 1 and "refusing to overwrite" in r.stderr, (r.returncode, r.stderr)
    assert p.read_text(encoding="utf-8") == "<html>mine</html>"
    d2 = installed()
    p2 = d2 / REPORT
    p2.parent.mkdir()
    p2.write_text("<html>mine</html>", encoding="utf-8")
    assert commit(d2, "a.txt", "1").returncode == 0
    assert p2.read_text(encoding="utf-8") == "<html>mine</html>"
    assert "report.py --stage exit 1" in hook_log(d2) and "refusing to overwrite" in hook_log(d2), hook_log(d2)


def t_git_add_failure_exit_1_and_logged():
    d = installed()
    (d / ".gitignore").write_text("reports/\n", encoding="utf-8")
    r = stage(d)
    assert r.returncode == 1 and "git add failed" in r.stderr, (r.returncode, r.stderr)
    d2 = installed()
    (d2 / ".gitignore").write_text("reports/\n", encoding="utf-8")
    assert commit(d2, ".gitignore", "reports/\n").returncode == 0
    assert "report.py --stage exit 1" in hook_log(d2), hook_log(d2)
    assert REPORT not in files_of(d2)


def t_busy_state_exit_0_no_log():
    d = installed()
    (d / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n")
    r = stage(d)
    assert r.returncode == 0 and "skipped" in r.stdout
    assert hook_log(d) == ""


def t_token_not_in_committed_report():
    d = installed()
    git(d, "remote", "add", "origin", "https://user:ghp_SYNTHETICTOKEN123@example.com/o/r.git?token=SECRET2#frag")
    assert commit(d, "a.txt", "1").returncode == 0
    page = git(d, "show", f"HEAD:{REPORT}").stdout
    assert "SYNTHETICTOKEN123" not in page and "SECRET2" not in page and "user:" not in page
    assert "example.com/o/r.git" in page


def t_scp_remote_query_not_in_committed_report():
    d = installed()
    git(d, "remote", "add", "origin", "git@example.com:org/repo.git?token=SYNTHETIC_SECRET_SCP")
    assert commit(d, "a.txt", "1").returncode == 0
    assert REPORT in files_of(d), files_of(d)
    page = git(d, "show", f"HEAD:{REPORT}").stdout
    assert "SYNTHETIC_SECRET_SCP" not in page
    assert "example.com:org/repo.git" in page


def main():
    for name, fn in list(globals().items()):
        if name.startswith("t_"):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                FAILS.append(name)
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    for p in _TMPS:
        shutil.rmtree(p, ignore_errors=True)
    print("OK: 0 failing" if not FAILS else f"FAILED: {len(FAILS)} failing")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
