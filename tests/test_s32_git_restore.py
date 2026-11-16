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
]
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
