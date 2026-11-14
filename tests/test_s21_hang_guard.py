"""S21 hang guard + heredoc semantics for harness/hooks/_commit_detect.py (stdlib only)."""
from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DEFAULT_ROOT = Path(r"D:\tmp\scratch_worktrees\hv2-s21")
ROOT = Path(os.environ.get("HV2_ROOT") or _HERE.parent)
if not (ROOT / "harness" / "hooks").is_dir():
    ROOT = _DEFAULT_ROOT
sys.path.insert(0, str(ROOT / "harness" / "hooks"))
import _commit_detect as cd  # noqa: E402

PUSH, COMMIT, STRIP = cd.is_push_command, cd.is_commit_command, cd._strip_heredocs

TIMING = {
    "heredoc_unterminated": "git commit\n" + "<<A\n" * 40000,
    "semicolon_git_x": ";git x " * 33000,
    "git_dash_c": "git " + "-c x " * 40000,
    "xargs": "xargs -n1 " * 22000,
    "sudo": "sudo -a " * 25000,
    "powershell": "powershell -a b " * 12000,
    "env": "env -i " * 30000,
    "git_git": "git " * 50000,
    "heredoc_terminated": "<<A\nx\nA\n" * 20000,
}


def _timing(name):
    s = TIMING[name]
    assert len(s) >= 150_000, len(s)
    for fn in (PUSH, COMMIT):
        t = time.perf_counter()
        r = fn(s)
        dt = time.perf_counter() - t
        assert isinstance(r, bool)
        assert dt < 10, f"{fn.__name__} on {name} took {dt:.1f}s"


def _mk_timing(name):
    return lambda: _timing(name)


for _n in TIMING:
    globals()[f"test_timing_{_n}"] = _mk_timing(_n)


def test_timing_twin_small_inputs_still_detect():
    assert PUSH("git push") and COMMIT("git commit -m x")
    assert not PUSH("git status") and not COMMIT("git status")
    assert PUSH("git commit\n" + "<<A\n" * 3 + "git push")


# ---- heredoc semantics
def test_prose_heredoc_not_push():
    assert not PUSH("cat > notes.md <<'EOF'\ngit push is bad\nEOF")


def test_prose_heredoc_twin_real_push_after():
    assert PUSH("cat > notes.md <<'EOF'\ngit push is bad\nEOF\ngit push origin main")


def test_heredoc_to_shell_is_push():
    assert PUSH("bash <<'EOF'\ngit push origin main\nEOF")


def test_cat_pipe_bash_is_push():
    assert PUSH("cat <<EOF | bash\ngit push origin main\nEOF")


def test_cat_without_pipe_twin_not_push():
    assert not PUSH("cat <<EOF\ngit push origin main\nEOF")


def test_unterminated_opener_no_crash_push_line_is_push():
    assert PUSH("cat <<EOF\ngit push") is True


def test_unterminated_opener_twin_without_push():
    assert PUSH("cat <<EOF\necho hi") is False


def test_unterminated_commit_line_is_commit():
    assert COMMIT("cat <<EOF\ngit commit -m x") is True


def test_terminator_indented_spaces_closes():
    assert not PUSH("cat > f <<EOF\ngit push\n   EOF")
    assert PUSH("cat > f <<EOF\ngit push\n   EOF\ngit push")


def test_terminator_indented_tab_closes():
    assert not PUSH("cat > f <<EOF\ngit push\n\tEOF")
    assert PUSH("cat > f <<EOF\ngit push\n\tEOF\ngit push")


def test_dash_form():
    assert not PUSH("cat > f <<-EOF\n\tgit push\n\tEOF")
    assert PUSH("bash <<-EOF\n\tgit push\n\tEOF")


def test_quoted_delimiters():
    assert not PUSH("cat > f <<'END'\ngit push\nEND")
    assert not PUSH('cat > f <<"END"\ngit push\nEND')
    assert PUSH("sh <<'END'\ngit push\nEND")
    assert PUSH('sh <<"END"\ngit push\nEND')


def test_two_heredocs_one_command():
    assert not PUSH("cat > a <<A\ngit push\nA\ncat > b <<B\ngit push\nB")
    assert PUSH("cat > a <<A\ngit push\nA\nbash <<B\ngit push\nB")
    assert PUSH("cat > a <<A\ngit push\nA\ncat > b <<B\nx\nB\ngit push")


def test_commit_message_shape_commit_not_push():
    c = "git commit -m \"$(cat <<'EOF'\nfix: do not git push here\nEOF\n)\""
    assert COMMIT(c) is True
    assert PUSH(c) is False


def test_commit_message_shape_twin_real_push_chained():
    c = "git commit -m \"$(cat <<'EOF'\nfix: x\nEOF\n)\" && git push"
    assert COMMIT(c) and PUSH(c)


def test_empty_body_not_closed_by_next_line():
    # `EOF` right after the opener is a body line, so the heredoc runs to the second EOF
    assert not PUSH("cat > f <<EOF\nEOF\ngit push\nEOF")
    # twin: one real body line, the first EOF closes it, then the push is a command
    assert PUSH("cat > f <<EOF\nx\nEOF\ngit push")


def test_strip_heredocs_blanks_prose_keeps_code():
    s = STRIP("cat > f <<EOF\ngit push\nEOF\necho done")
    assert "git push" not in s and "echo done" in s
    k = STRIP("bash <<EOF\ngit push\nEOF")
    assert "git push" in k


def test_strip_heredocs_no_heredoc_identity():
    assert STRIP("git status\necho hi") == "git status\necho hi"


def run_all(g):
    fails = 0
    for name in [n for n in g if n.startswith("test_")]:
        try:
            g[name]()
            print(f"PASS {name}")
        except BaseException:
            fails += 1
            print(f"FAIL {name}")
            traceback.print_exc(limit=4, file=sys.stdout)
    print(f"{'FAILED' if fails else 'OK'}: {fails} failing")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run_all(globals()))
