"""S32-T1: the command gate allows `git restore <named paths>` and still blocks a broad restore.
Run: C:\\Python314\\python.exe -I tests/test_s32_git_restore.py"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "harness" / "hooks"))
import hv2  # noqa: E402
import dangerous_command_gate as g  # noqa: E402

ALLOWED = [
    "git restore docs/plan.md",
    "git restore -- docs/plan.md",
    "git restore --source=main docs/plan.md docs/other.md",
    "git restore --source main -- docs/plan.md",
    "git restore -s HEAD~1 docs/plan.md",
    "git restore --worktree docs/plan.md",
    "git restore --staged docs/plan.md",
    "git restore --staged",
    "git -C D:/Repo/proj restore docs/plan.md",
    "git -c k=v restore docs/plan.md",
    "git restore -sHEAD docs/plan.md",
    "git restore -W docs/plan.md",
    "git restore --staged -S docs/plan.md",
    "git restore docs",
    "git restore docs/sub/file.md",
    "git.exe restore docs/plan.md",
    "git restore -sRELEASE docs/plan.md",
    "git restore -s RELEASE docs/plan.md",
    "git restore -SW docs/plan.md",
    "git restore -p docs/plan.md",
]


def test_s_value_does_not_set_staged():
    # `-sRELEASE` is a ref value: it must NOT make the restore look staged-only (round 2 reviewer MAJOR)
    assert g.classify("git restore -sRELEASE .")
BLOCKED = [
    "git restore",
    "git restore .",
    "git restore ./",
    "git restore *",
    "git restore /",
    "git restore ~",
    "git restore C:/",
    "git restore --source=main",
    "git restore --worktree",
    "git restore -- .",
    "git restore --pathspec-from-file=list.txt",
    "git restore ':(glob)**/*.md'",
    "git restore docs/plan.md .",
    # round 1 reviewer bypasses
    "git restore ./*", "git restore ./.", "git restore .//", "git restore -- .\\\\", "git restore **", "git restore ..",
    "git restore docs/..", "git restore ./docs/../.", "git restore D:/Repo/proj", "git restore D:/Repo/proj/.", "git restore $PWD",
    "git restore '*.py'", "git restore 'src/*'", "git restore '**/*'", "git restore ../other.md",
    "git restore --staged -W .", "git restore --staged --work .", "git restore --staged -W -- .",
    "git restore --pathspec-from list.txt", "git restore --pathspec-from-fi list.txt", "git restore --sou main docs/plan.md",
    "git restore --staged --worktree",
    # round 2 reviewer bypasses
    "git restore -sRELEASE .", "git restore -sSTAGE -- .", "git restore -sMASTER ':/'",
    "git restore (pwd)", "git restore (gl)", "git restore (Get-Location)", "git restore @('.')", "git restore (,'.')",
    "git.exe restore .", "git.exe restore -- .", '"C:\\\\Program Files\\\\Git\\\\cmd\\\\git.exe" restore -- .',
    "git restore -x docs/plan.md",
]


def test_named_restore_is_allowed():
    for c in ALLOWED:
        r = g.classify(c)
        assert not r, f"{c!r} should be allowed, got {r!r}"


def test_broad_restore_stays_blocked():
    for c in BLOCKED:
        r = g.classify(c)
        assert r, f"{c!r} should be blocked"


def test_other_discards_unchanged():
    for c in ("git reset --hard", "git clean -fd", "git checkout --", "git checkout -- ."):
        assert g.classify(c), f"{c!r} must stay blocked"


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
