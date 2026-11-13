"""Twin of test_G: the LEFTOVER_ASIDES exemption must stay narrow.
A user-created untracked file named like a parked copy, NOT listed in LEFTOVER_ASIDES, is a real status change."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools"))
import hv2  # noqa: E402
import ci_triggers_off as ci  # noqa: E402
from test_s14_ci_dirty import dirty_repo, snapshot, _w, rd, WF, JOURNAL, FILES3, no_leftovers, _fail_unlink_of_asides  # noqa: E402

USER_ASIDE_NAME = ".x.cioff-aside"
USER_BYTES = b"user note, not ours\n"


def _run_with_user_file_created_before_final_check(repo, scratch, undeletable):
    o_refresh = ci.refresh_index
    st = {"n": 0}

    def hook(r, *a, **k):  # fires once, after files are merged and before the final status comparison
        if not st["n"]:
            st["n"] = 1
            _w(repo / USER_ASIDE_NAME, USER_BYTES)
        return o_refresh(r, *a, **k)
    ci.refresh_index = hook
    orig = _fail_unlink_of_asides() if undeletable else None
    try:
        out = ci.process_dirty(repo, False, scratch)
    finally:
        ci.refresh_index = o_refresh
        if orig:
            ci._drop_parked = orig
    assert st["n"] == 1
    return out


def test_user_file_named_like_parked_copy_is_a_status_change_error():
    root, scratch, repo = dirty_repo("twin1")
    before = snapshot(repo)
    out = _run_with_user_file_created_before_final_check(repo, scratch, undeletable=False)
    assert out.startswith("ERROR"), out
    assert f"status changed for {USER_ASIDE_NAME}" in out, out
    assert rd(repo / USER_ASIDE_NAME) == USER_BYTES, "user's file must survive rollback"
    assert "branch back at" in out, out
    for rel in FILES3:
        assert rd(repo / rel) == before["files"][rel], rel  # rolled back to the pre-run bytes
    assert not (repo / ".git" / JOURNAL).exists()


def test_exemption_does_not_leak_to_user_file_when_real_parked_copies_are_listed():
    root, scratch, repo = dirty_repo("twin2")
    before = snapshot(repo)
    out = _run_with_user_file_created_before_final_check(repo, scratch, undeletable=True)
    assert out.startswith("ERROR"), out  # not CHANGED: the user's file is not in LEFTOVER_ASIDES
    assert f"status changed for {USER_ASIDE_NAME}" in out, out
    assert rd(repo / USER_ASIDE_NAME) == USER_BYTES
    assert ci.LEFTOVER_ASIDES == [], ci.LEFTOVER_ASIDES
    # kept parked copies are retired to unique -kept- names, so the rollback completes; the NOTE names them
    assert "working files restored" in out and "NOTE kept parked copies" in out and "-kept-" in out, out
    for rel in FILES3:
        assert rd(repo / rel) == before["files"][rel], rel
    assert not (repo / ".git" / JOURNAL).exists()


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
