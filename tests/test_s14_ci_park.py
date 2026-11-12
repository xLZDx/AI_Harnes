"""S14 round-4 tests: swap_in step 1 never overwrites a parked name (_park); step 4 deletes a parked copy only through
_drop_parked (Windows: share mode 0 handle). Acceptance criteria AC1-AC3 of GPT-PM round 3.
Temp folders only under D:\\tmp\\harness_v2_tests.
Run: C:\\Python314\\python.exe -I tests/test_s14_ci_park.py"""
import contextlib
import hashlib
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools"))
import hv2  # noqa: E402
import ci_triggers_off as ci  # noqa: E402
from test_s14_ci_dirty import dirty_repo, snapshot, rd, _w, WF, FILES3  # noqa: E402

OLD = b"old bytes of the file\n"
NEW = b"new bytes written by the tool\n"
FOREIGN = b"somebody else's parked file\n"
LATE = b"LATE-WRITE-FROM-HELD-HANDLE\n"


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def _mk():
    d = hv2.fresh("park")
    p = d / "f.txt"
    p.write_bytes(OLD)
    return d, p


@contextlib.contextmanager
def writer_handle(path):
    """Writable handle with share mode READ|WRITE|DELETE (7), the way a still-running editor/session would hold a file."""
    import ctypes
    import msvcrt
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    h = k32.CreateFileW(str(path), 0x80000000 | 0x40000000, 7, None, 3, 0x80, None)
    assert h not in (None, wintypes.HANDLE(-1).value), ctypes.get_last_error()
    f = open(msvcrt.open_osfhandle(h, os.O_RDWR | os.O_BINARY), "r+b", buffering=0)
    try:
        yield f
    finally:
        f.close()


# ---------------------------------------------------------------- AC1
def test_AC1_file_created_at_parked_name_just_before_parking_is_never_overwritten():
    d, p = _mk()
    a = ci.aside_path(p)
    o_place, fired = ci._place_noclobber, []

    def hook(src, dst):
        if Path(src) == p and Path(dst) == a and not fired:
            fired.append(1)
            a.write_bytes(FOREIGN)  # appears after swap_in's own "aside exists" pre-check, right before the park
        return o_place(src, dst)
    ci._place_noclobber = hook
    try:
        err = ci.swap_in(p, NEW, _sha(OLD))
    finally:
        ci._place_noclobber = o_place
    assert fired == [1]
    assert err and "not overwritten" in err, err
    assert a.read_bytes() == FOREIGN, "foreign parked file was overwritten"
    assert p.read_bytes() == OLD, "p must be untouched"
    assert ci.LEFTOVER_ASIDES == [] or str(a) not in ci.LEFTOVER_ASIDES


def test_AC1_park_helper_direct_refuses_existing_name():
    d, p = _mk()
    a = ci.aside_path(p)
    a.write_bytes(FOREIGN)
    msg = ci._park(p, a)
    assert msg and "not overwritten" in msg, msg
    assert a.read_bytes() == FOREIGN and p.read_bytes() == OLD
    a.unlink()
    assert ci._park(p, a) is None
    assert a.read_bytes() == OLD and not p.exists()


# ---------------------------------------------------------------- AC2
def test_AC2_writer_with_open_handle_keeps_parked_copy_and_its_late_bytes():
    if os.name != "nt":
        return
    d, p = _mk()
    a = ci.aside_path(p)
    ci.LEFTOVER_ASIDES.clear()
    with writer_handle(p) as w:
        err = ci.swap_in(p, NEW, _sha(OLD))
        assert err is None, err
        assert p.read_bytes() == NEW
        assert len(ci.LEFTOVER_ASIDES) == 1, ci.LEFTOVER_ASIDES
        k = Path(ci.LEFTOVER_ASIDES[0])
        assert k.is_file() and k.name.startswith(a.name + "-kept-"), k
        assert not a.exists(), "fixed parked name must be free again"
        assert k.read_bytes() == OLD
        w.seek(0, 2)
        w.write(LATE)  # the writer writes after the swap, into the parked file
    assert k.read_bytes() == OLD + LATE, "late bytes of the writer must be preserved"
    ci.LEFTOVER_ASIDES.clear()


def test_AC5_second_run_after_a_kept_copy_is_not_blocked_by_earlier_swap_check():
    if os.name != "nt":
        return
    d, p = _mk()
    a = ci.aside_path(p)
    ci.LEFTOVER_ASIDES.clear()
    with writer_handle(p) as w:
        assert ci.swap_in(p, NEW, _sha(OLD)) is None
        k = Path(ci.LEFTOVER_ASIDES[0])
        w.seek(0, 2)
        w.write(LATE)
    ci.LEFTOVER_ASIDES.clear()
    err = ci.swap_in(p, b"third version\n", _sha(NEW))
    assert err is None, err
    assert "exists from an earlier swap" not in (err or "")
    assert p.read_bytes() == b"third version\n"
    assert not a.exists()
    assert k.read_bytes() == OLD + LATE, "first kept copy untouched by the second run"


def test_AC2_drop_parked_direct_kept_while_open_deleted_when_free():
    if os.name != "nt":
        return
    d, p = _mk()
    with writer_handle(p):
        assert ci._drop_parked(p, {_sha(OLD)}, tries=2) == "kept"
        assert p.read_bytes() == OLD
    assert ci._drop_parked(p, {_sha(OLD)}) == "deleted"
    assert not p.exists()
    assert ci._drop_parked(p, {_sha(OLD)}) == "deleted"  # already gone counts as deleted


def test_AC2_process_dirty_end_to_end_reports_kept_copy_and_completes():
    if os.name != "nt":
        return
    root, scratch, repo = dirty_repo("ac2e2e")
    before = snapshot(repo)
    target = repo / WF
    a = ci.aside_path(target)
    with writer_handle(target) as w:
        out = ci.process_dirty(repo, False, scratch)
        assert out.startswith("CHANGED"), out
        assert "NOTE kept parked copies" in out, out
        kept = sorted(a.parent.glob(a.name + "-kept-*"))
        assert len(kept) == 1 and str(kept[0]) in out and not a.exists(), (kept, out)
        w.seek(0, 2)
        w.write(LATE)
    assert rd(kept[0]) == before["files"][WF] + LATE
    # a second run on the same repo is not blocked by "exists from an earlier swap" (AC5, end to end)
    _w(target, rd(target) + b"      - run: echo more\n")
    again = ci.process_dirty(repo, False, scratch)
    assert "exists from an earlier swap" not in again, again
    for rel in FILES3:
        if rel != WF:
            assert not ci.aside_path(repo / rel).exists(), rel  # unheld files are deleted as before
    assert ci.LEFTOVER_ASIDES == []


# ---------------------------------------------------------------- AC3
def test_AC3_parked_copy_changed_before_drop_is_kept_and_reported():
    d, p = _mk()
    a = ci.aside_path(p)
    o_drop, fired = ci._drop_parked, []

    def hook(aa, known, *args, **k):
        fired.append(1)
        with open(aa, "ab") as f:  # bytes change between the step-2 hash and the drop
            f.write(LATE)
        return o_drop(aa, known, *args, **k)
    ci._drop_parked = hook
    try:
        err = ci.swap_in(p, NEW, _sha(OLD))
    finally:
        ci._drop_parked = o_drop
    assert fired == [1]
    assert err and "written to while parked" in err, err
    assert a.read_bytes() == OLD + LATE, "changed copy must be kept intact"
    assert p.read_bytes() == NEW


def test_AC3_drop_parked_direct_unknown_bytes_returns_changed_and_keeps_file():
    if os.name != "nt":
        return
    d, p = _mk()
    r = ci._drop_parked(p, {_sha(b"something else entirely")})
    assert r == "changed", r
    assert p.read_bytes() == OLD


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
