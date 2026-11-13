"""S19-T3 tests: tools/audit_projects.py kills the app-server tree with the System32 taskkill.exe by absolute path, treats
taskkill exit 128 as success only when the process really exited, picks the vendor codex.exe by the exact Rust target-triple
directory (and keeps the fallback note in a FAIL row), and counts a hooks/list hook whose sourcePath is the config.toml next
to hooks.json (a foreign sourcePath still does not count).
Written from the S19-T3 acceptance criteria. os is patched to look like Windows where needed, subprocess.run and Popen are
faked, HOME is a temp folder; Codex is never started.
Run: C:\\Python314\\python.exe -I tests\\test_s19_audit.py"""
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_s18_audit as t18  # noqa: E402  (FakeProc, _NtOs, fake_home, layouts; also loads audit_projects)

ap = t18.ap
FakeProc, _NtOs, tmpdir, machine, _which = t18.FakeProc, t18._NtOs, t18.tmpdir, t18.machine, t18._which
fake_home, _hooks_json, _all_listed = t18.fake_home, t18._hooks_json, t18._all_listed


# ---------------------------------------------------------------- taskkill by absolute System32 path
def _recording_run(proc, calls, rc=0, dies=False):
    def run(cmd, **kw):
        calls.append(cmd)
        if dies:
            proc.alive = False
        return subprocess.CompletedProcess(cmd, rc, b"", b"")
    return run


def test_kill_tree_runs_system32_taskkill_exe_under_systemroot_not_the_bare_word():
    proc, calls = FakeProc(pid=777, dies_on_taskkill=True), []
    with mock.patch.dict(os.environ, {"SystemRoot": r"C:\FakeWinRoot"}), mock.patch.object(ap, "os", _NtOs()), \
            mock.patch.object(ap.subprocess, "run", _recording_run(proc, calls, dies=True)):
        warns = ap._kill_process_tree(proc)
    assert len(calls) == 1, calls
    cmd = calls[0]
    assert cmd[0] != "taskkill", "the bare word is looked up through PATH and the cwd"
    assert cmd[0].lower() == r"c:\fakewinroot\system32\taskkill.exe", cmd
    assert cmd[1:] == ["/T", "/F", "/PID", "777"], cmd
    assert warns == [], warns


def test_kill_tree_uses_the_default_windows_folder_when_systemroot_is_unset():
    proc, calls = FakeProc(pid=5, dies_on_taskkill=True), []
    saved = os.environ.pop("SystemRoot", None)
    try:
        with mock.patch.object(ap, "os", _NtOs()), mock.patch.object(ap.subprocess, "run", _recording_run(proc, calls, dies=True)):
            ap._kill_process_tree(proc)
    finally:
        if saved is not None:
            os.environ["SystemRoot"] = saved
    assert len(calls) == 1 and calls[0][0].lower() == r"c:\windows\system32\taskkill.exe", calls


def test_kill_tree_on_the_real_host_uses_an_existing_taskkill_exe_path():
    if os.name != "nt":
        print("SKIP real taskkill path: not Windows")
        return
    proc, calls = FakeProc(pid=1, dies_on_taskkill=True), []
    with mock.patch.object(ap.subprocess, "run", _recording_run(proc, calls, dies=True)):
        ap._kill_process_tree(proc)
    assert calls and os.path.isabs(calls[0][0]) and os.path.isfile(calls[0][0]), calls
    assert calls[0][0].lower().endswith(os.path.join("system32", "taskkill.exe")), calls[0][0]


# ---------------------------------------------------------------- taskkill exit 128
def test_exit_128_for_a_process_that_already_exited_gives_no_warning():
    proc, calls = FakeProc(pid=31), []
    with mock.patch.object(ap, "os", _NtOs()), mock.patch.object(ap.subprocess, "run", _recording_run(proc, calls, rc=128, dies=True)):
        warns = ap._kill_process_tree(proc)  # the process exited on its own between the poll and taskkill: 128 = not found
    assert len(calls) == 1, calls
    assert warns == [], warns


def test_exit_128_while_the_process_is_still_alive_still_warns():
    proc, calls = FakeProc(pid=32), []
    with mock.patch.object(ap, "os", _NtOs()), mock.patch.object(ap.subprocess, "run", _recording_run(proc, calls, rc=128)):
        warns = ap._kill_process_tree(proc)
    assert any("taskkill for app-server pid 32 exited 128" in w for w in warns), warns


def test_other_nonzero_exit_codes_warn_even_when_the_process_exited():
    for rc in (1, 5, 129):
        proc, calls = FakeProc(pid=33), []
        with mock.patch.object(ap, "os", _NtOs()), mock.patch.object(ap.subprocess, "run", _recording_run(proc, calls, rc=rc, dies=True)):
            warns = ap._kill_process_tree(proc)
        assert any(f"taskkill for app-server pid 33 exited {rc}" in w for w in warns), (rc, warns)


def test_exit_zero_gives_no_warning():
    proc, calls = FakeProc(pid=34), []
    with mock.patch.object(ap, "os", _NtOs()), mock.patch.object(ap.subprocess, "run", _recording_run(proc, calls, rc=0, dies=True)):
        assert ap._kill_process_tree(proc) == []


# ---------------------------------------------------------------- vendor codex.exe by the exact target-triple directory
X64_TRIPLE = "x86_64-pc-windows-msvc"
ARM_TRIPLE = "aarch64-pc-windows-msvc"


def _vendor(shim, pkg, triple):
    exe = shim.parent / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai" / pkg / "vendor" / triple / "bin" / "codex.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    return exe


def _pick(d_shim, mach):
    with mock.patch.object(ap.shutil, "which", _which({"codex": str(d_shim)})), mock.patch.object(ap, "os", _NtOs()), \
            machine(mach), mock.patch.object(ap, "CODEX_EXE_NOTE", None):
        got = ap._codex_executable()
        return got, ap.CODEX_EXE_NOTE


def test_amd64_picks_the_x86_64_triple_and_arm64_picks_the_aarch64_triple():
    for mach, triple in (("AMD64", X64_TRIPLE), ("amd64", X64_TRIPLE), ("x86_64", X64_TRIPLE),
                         ("ARM64", ARM_TRIPLE), ("arm64", ARM_TRIPLE), ("aarch64", ARM_TRIPLE)):
        with tmpdir() as d:
            shim = d / "npm" / "codex.cmd"
            shim.parent.mkdir(parents=True)
            shim.write_text("@echo shim\n")
            # the sorted-first package is the arm one, so a wrong pick for AMD64 is visible; and the reverse order is
            # covered by ARM64 where the x64 package sorts last
            arm = _vendor(shim, "codex-win32-arm64", ARM_TRIPLE)
            x64 = _vendor(shim, "codex-win32-x64", X64_TRIPLE)
            got, note = _pick(shim, mach)
            want = x64 if triple == X64_TRIPLE else arm
            assert os.path.samefile(got, want), (mach, got)
            assert Path(got).parent.parent.name == triple, (mach, got)
            assert note is None, (mach, note)


def test_a_directory_named_only_x64_is_not_matched_and_the_fallback_note_is_set():
    with tmpdir() as d:
        shim = d / "npm" / "codex.cmd"
        shim.parent.mkdir(parents=True)
        shim.write_text("@echo shim\n")
        arm = _vendor(shim, "codex-win32-arm64", ARM_TRIPLE)  # sorts first
        lookalike = _vendor(shim, "codex-win32-x64", "x64")   # the old substring heuristic would have taken this
        got, note = _pick(shim, "AMD64")
        assert not os.path.samefile(got, lookalike), "a directory named only 'x64' must not be matched"
        assert os.path.samefile(got, arm), got  # fallback = first sorted entry
        assert note and "no vendor codex.exe matches machine AMD64" in note and "used first entry" in note, note


def test_a_directory_that_only_contains_the_triple_as_a_prefix_is_not_matched():
    with tmpdir() as d:
        shim = d / "npm" / "codex.cmd"
        shim.parent.mkdir(parents=True)
        shim.write_text("@echo shim\n")
        near = _vendor(shim, "codex-win32-x64", X64_TRIPLE + "-old")
        got, note = _pick(shim, "AMD64")
        assert os.path.samefile(got, near), "it is the only candidate, so it is the first-entry fallback"
        assert note and "no vendor codex.exe matches machine AMD64" in note, note


def test_the_arm_triple_does_not_satisfy_an_amd64_machine_and_vice_versa():
    with tmpdir() as d:
        shim = d / "npm" / "codex.cmd"
        shim.parent.mkdir(parents=True)
        shim.write_text("@echo shim\n")
        arm = _vendor(shim, "codex-win32-arm64", ARM_TRIPLE)
        got, note = _pick(shim, "AMD64")
        assert os.path.samefile(got, arm) and note and "AMD64" in note, (got, note)
        got, note = _pick(shim, "ARM64")
        assert os.path.samefile(got, arm) and note is None, (got, note)


# ---------------------------------------------------------------- the fallback note stays in a FAIL row
def _unmatched_note():
    """Drive the real fallback and return the note it sets (the machine is AMD64, only an x64-named directory exists)."""
    with tmpdir() as d:
        shim = d / "npm" / "codex.cmd"
        shim.parent.mkdir(parents=True)
        shim.write_text("@echo shim\n")
        _vendor(shim, "codex-win32-x64", "x64")
        got, note = _pick(shim, "AMD64")
    assert note and "used first entry" in note, note
    return note


def test_the_codex_exe_note_is_in_an_ok_row_detail():
    note = _unmatched_note()
    with fake_home(_hooks_json(), lambda hp: _all_listed(hp)) as proj, mock.patch.object(ap, "CODEX_EXE_NOTE", note):
        st, detail = ap.chk_codex_cfg(proj, home=True)
    assert st == ap.OK and note in detail, (st, detail)


def test_the_codex_exe_note_stays_in_a_fail_row_detail():
    note = _unmatched_note()
    with fake_home(_hooks_json(), lambda hp: _all_listed(hp, skip=("go_gate.py",))) as proj, \
            mock.patch.object(ap, "CODEX_EXE_NOTE", note):
        st, detail = ap.chk_codex_cfg(proj, home=True)
    assert st == ap.FAIL and "not loaded by hooks/list: PreToolUse/Bash: go_gate.py" in detail, (st, detail)
    assert note in detail, f"the executable note was dropped from the FAIL row: {detail!r}"


def test_a_fail_row_without_any_note_has_no_stray_note_text():
    with fake_home(_hooks_json(), lambda hp: _all_listed(hp, skip=("go_gate.py",))) as proj, \
            mock.patch.object(ap, "CODEX_EXE_NOTE", None):
        st, detail = ap.chk_codex_cfg(proj, home=True)
    assert st == ap.FAIL and "no vendor codex.exe" not in detail and "None" not in detail, detail


# ---------------------------------------------------------------- sourcePath: hooks.json or the config.toml beside it
def test_a_hook_listed_with_the_config_toml_next_to_hooks_json_counts_as_loaded():
    with fake_home(_hooks_json(), lambda hp: [dict(h, sourcePath=str(hp.parent / "config.toml")) for h in _all_listed(hp)]) as proj:
        st, detail = ap.chk_codex_cfg(proj, home=True)
    assert st == ap.OK, (st, detail)


def test_hooks_split_between_hooks_json_and_config_toml_all_count():
    def listed(hp):
        out = _all_listed(hp)
        out[0] = dict(out[0], sourcePath=str(hp.parent / "config.toml"))  # one inline table, the rest from hooks.json
        return out
    with fake_home(_hooks_json(), listed) as proj:
        st, detail = ap.chk_codex_cfg(proj, home=True)
    assert st == ap.OK, (st, detail)


def test_a_foreign_source_path_still_does_not_count():
    foreign = {
        "other file next to hooks.json": lambda hp: hp.parent / "other.toml",
        "config.toml one level up": lambda hp: hp.parent.parent / "config.toml",
        "hooks.json with a suffix": lambda hp: Path(str(hp) + ".other"),
        "empty": lambda hp: "",
    }
    for label, make in foreign.items():
        with fake_home(_hooks_json(), lambda hp, make=make: [dict(h, sourcePath=str(make(hp))) if h["eventName"] == "stop" else h
                                                              for h in _all_listed(hp)]) as proj:
            st, detail = ap.chk_codex_cfg(proj, home=True)
        assert st == ap.FAIL and "not loaded by hooks/list" in detail and "rules_gate.py" in detail, (label, st, detail)
        assert "go_gate.py" not in detail.split("not loaded by hooks/list")[1].split(";")[0], (label, detail)


def test_the_disabled_flag_is_still_judged_on_a_hook_listed_from_config_toml():
    with fake_home(_hooks_json(), lambda hp: [dict(h, sourcePath=str(hp.parent / "config.toml")) for h in
                                              _all_listed(hp, disabled=("rules_gate.py",))]) as proj:
        st, detail = ap.chk_codex_cfg(proj, home=True)
    assert st == ap.FAIL and "disabled in hooks/list: stop/*: rules_gate.py" in detail, (st, detail)


def test_a_project_hook_listed_with_the_config_toml_beside_the_project_hooks_json_counts():
    cmd = "python3 check_project.py"
    pcfg = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": cmd}]}]}}

    def listed(hp):
        proj_codex = hp.parent.parent.parent / "proj" / ".codex"
        return _all_listed(hp) + [{"source": "project", "trustStatus": "trusted", "sourcePath": str(proj_codex / "config.toml"),
                                   "eventName": "stop", "matcher": None, "handlerType": "command", "command": cmd}]
    with fake_home(_hooks_json(), listed) as proj:
        (proj / ".codex").mkdir()
        (proj / ".codex" / "hooks.json").write_text(json.dumps(pcfg), encoding="utf-8")
        st, detail = ap.chk_codex_cfg(proj, home=True)
    assert st == ap.OK, (st, detail)


def test_a_project_hook_listed_from_a_foreign_path_is_missing():
    cmd = "python3 check_project.py"
    pcfg = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": cmd}]}]}}

    def listed(hp):
        proj_codex = hp.parent.parent.parent / "proj" / ".codex"
        return _all_listed(hp) + [{"source": "project", "trustStatus": "trusted", "sourcePath": str(proj_codex / "other.toml"),
                                   "eventName": "stop", "matcher": None, "handlerType": "command", "command": cmd}]
    with fake_home(_hooks_json(), listed) as proj:
        (proj / ".codex").mkdir()
        (proj / ".codex" / "hooks.json").write_text(json.dumps(pcfg), encoding="utf-8")
        st, detail = ap.chk_codex_cfg(proj, home=True)
    assert st == ap.FAIL and "project hooks.json handlers missing from hooks/list: stop/*: check_project.py" in detail, (st, detail)


def main():
    fails = 0
    for name in [n for n, f in list(globals().items()) if n.startswith("test_") and callable(f) and f.__module__ == __name__]:
        try:
            globals()[name]()
            print(f"PASS {name}")
        except BaseException as e:
            fails += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    print("OK: 0 failing" if not fails else f"FAILED: {fails} failing")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
