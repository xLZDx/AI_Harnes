"""S18-T2 tests: tools/audit_projects.py reports the effective kill-switch value per name, finds the vendor codex.exe behind an
npm shim, kills the whole app-server process tree, and flags harness hooks that Codex does not load or has disabled.
Written from the S18-T2 acceptance criteria. os.environ is patched inside each test and winreg is a fake module put into
sys.modules, so the real environment and the real registry are never read or changed. Codex is never started (Popen is faked)
except one real child-process tree that the test itself spawns and kills. HOME is a temp folder.
Run: C:\\Python314\\python.exe -I tests\\test_s18_audit.py"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import shutil
import time
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_s17_audit_kill_switch as k17  # noqa: E402  (fake_winreg, env, items, _PosixOs; also loads audit_projects)

ap = k17.ap
fake_winreg, env, items = k17.fake_winreg, k17.env, k17.items
GO = "CLAUDE_GO_GATE"          # honours only "off"
SG = "HARNESS_SECRET_GUARD"    # honours off / 0 / false


def run_ks(user=(), machine=(), user_err=None, machine_err=None, **proc_env):
    with env(fake_winreg(user=user, machine=machine, user_err=user_err, machine_err=machine_err), **proc_env):
        return ap.chk_kill_switch()


# ---------------------------------------------------------------- criterion 1: effective value
def test_machine_off_but_user_on_is_ok():
    st, detail = run_ks(user=[(GO, "on")], machine=[(GO, "off")])
    assert st == ap.OK, (st, detail)


def test_machine_on_but_user_off_is_fail_naming_user():
    st, detail = run_ks(user=[(GO, "off")], machine=[(GO, "on")])
    assert st == ap.FAIL, (st, detail)
    assert f"{GO}=off (user" in detail, detail
    assert "(machine" not in detail.split(";")[0], detail  # the effective scope is user, not machine


def test_user_and_machine_both_off_names_user_only_once():
    st, detail = run_ks(user=[(SG, "off")], machine=[(SG, "off")])
    assert st == ap.FAIL and items(detail) == {f"{SG}=off (user)"}, (st, detail)


def test_machine_off_alone_is_still_reported_as_machine():
    st, detail = run_ks(machine=[(SG, "false")])
    assert st == ap.FAIL and items(detail) == {f"{SG}=false (machine)"}, (st, detail)


def test_effective_value_is_per_name_not_per_scope():
    # GO: user overrides to on (fine). SG: only machine, off (fail). The two names must not influence each other.
    st, detail = run_ks(user=[(GO, "on")], machine=[(GO, "off"), (SG, "0")])
    assert st == ap.FAIL and items(detail) == {f"{SG}=0 (machine)"}, (st, detail)


def test_user_value_the_hook_does_not_honour_as_off_overrides_machine_off():
    # go_gate honours only "off": user "0" is NOT off, and it is the effective value, so machine=off is shadowed.
    st, detail = run_ks(user=[(GO, "0")], machine=[(GO, "off")])
    assert st == ap.OK, (st, detail)


def test_process_off_differing_from_effective_is_reported_as_this_process():
    st, detail = run_ks(user=[(GO, "on")], **{GO: "off"})
    assert st == ap.FAIL and items(detail) == {f"{GO}=off (this process)"}, (st, detail)


def test_process_off_with_no_registry_value_is_this_process():
    st, detail = run_ks(**{SG: "0"})
    assert st == ap.FAIL and items(detail) == {f"{SG}=0 (this process)"}, (st, detail)


def test_process_off_equal_to_effective_off_is_not_reported_twice():
    st, detail = run_ks(user=[(GO, "off")], **{GO: "off"})
    assert st == ap.FAIL and items(detail) == {f"{GO}=off (user)"}, (st, detail)
    assert "this process" not in detail, detail


def test_process_value_that_differs_but_is_not_off_is_silent():
    st, detail = run_ks(user=[(GO, "off")], **{GO: "on"})
    assert st == ap.FAIL and items(detail) == {f"{GO}=off (user)"}, (st, detail)  # only the registry off is reported
    st, detail = run_ks(machine=[(GO, "on")], **{GO: "live"})
    assert st == ap.OK, (st, detail)
    st, detail = run_ks(user=[(GO, "on")], **{GO: "0"})  # "0" is not off for a hook that honours only "off"
    assert st == ap.OK, (st, detail)


def test_process_off_while_effective_on_is_reported_even_if_machine_is_off():
    st, detail = run_ks(user=[(GO, "on")], machine=[(GO, "off")], **{GO: "off"})
    assert st == ap.FAIL and items(detail) == {f"{GO}=off (this process)"}, (st, detail)


# ---------------------------------------------------------------- criterion 1: unreadable scope, non-Windows
def test_unreadable_machine_scope_is_fail_even_when_user_scope_says_on():
    st, detail = run_ks(user=[(GO, "on")], machine_err=PermissionError(5, "denied MACHINEKEY"))
    assert st == ap.FAIL and "machine registry unreadable" in detail and "MACHINEKEY" in detail, (st, detail)


def test_unreadable_user_scope_is_fail_even_when_machine_scope_is_clean():
    st, detail = run_ks(machine=[(GO, "on")], user_err=OSError(1, "boom USERKEY"))
    assert st == ap.FAIL and "user registry unreadable" in detail and "USERKEY" in detail, (st, detail)


def test_non_windows_host_is_na_but_a_process_off_is_still_fail():
    with env(fake_winreg(user=[(GO, "on")], machine=[(GO, "off")])), mock.patch.object(ap, "os", k17._PosixOs()):
        st, detail = ap.chk_kill_switch()
    assert st == ap.NA and "not Windows" in detail, (st, detail)  # registry is never read, so machine=off is not seen
    with env(fake_winreg(), **{GO: "off"}), mock.patch.object(ap, "os", k17._PosixOs()):
        st, detail = ap.chk_kill_switch()
    assert st == ap.FAIL and items(detail) == {f"{GO}=off (this process)"}, (st, detail)


# ---------------------------------------------------------------- criterion 2: vendor codex.exe behind the npm shim
class _NtOs:
    """Proxy for audit_projects.os that reports a Windows host (so the Windows-only code runs on any host)."""
    name = "nt"

    def __getattr__(self, attr):
        return getattr(os, attr)


def machine(name):
    return mock.patch.object(ap.platform, "machine", lambda: name)


def _which(table):
    return lambda name, *a, **k: table.get(name)


@contextlib.contextmanager
def tmpdir():
    d = Path(tempfile.mkdtemp(prefix="s18t2_"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


NPM_VENDOR = "node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe"


def _npm_layout(d, vendor_rel=None):
    shim = d / "npm" / "codex.cmd"
    shim.parent.mkdir(parents=True)
    shim.write_text("@echo shim\n")
    if vendor_rel:
        exe = shim.parent / "node_modules" / "@openai" / "codex" / vendor_rel
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"MZ")
        return shim, exe
    return shim, None


def test_codex_executable_prefers_vendor_exe_next_to_npm_cmd_shim():
    with tmpdir() as d:
        shim, exe = _npm_layout(d, NPM_VENDOR)
        calls = []

        def which(name, *a, **k):  # the real lookup order: codex.exe misses, codex finds the shim
            calls.append(name)
            return str(shim) if name == "codex" else None
        with mock.patch.object(ap.shutil, "which", which), mock.patch.object(ap, "os", _NtOs()), machine("AMD64"), \
                mock.patch.object(ap, "CODEX_EXE_NOTE", None):
            got = ap._codex_executable()
            assert ap.CODEX_EXE_NOTE is None, "a matching vendor binary needs no note"
        assert calls[:2] == ["codex.exe", "codex"], calls
        assert os.path.samefile(got, exe), (got, exe)
        assert got != str(shim)


def test_codex_executable_falls_back_to_flat_vendor_folder_and_notes_an_unmatched_machine():
    with tmpdir() as d:
        shim, exe = _npm_layout(d, "vendor/x86_64-pc-windows-msvc/bin/codex.exe")
        with mock.patch.object(ap.shutil, "which", _which({"codex": str(shim)})), mock.patch.object(ap, "os", _NtOs()), \
                machine("AMD64"), mock.patch.object(ap, "CODEX_EXE_NOTE", None):
            got = ap._codex_executable()
            note = ap.CODEX_EXE_NOTE
        assert os.path.samefile(got, exe), (got, exe)
        assert note and "no vendor codex.exe matches machine AMD64" in note and "used first entry" in note, note


def _two_vendors(d):
    shim, x64 = _npm_layout(d, "node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe")
    arm = shim.parent / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai" / "codex-win32-arm64" / "vendor" / \
        "aarch64-pc-windows-msvc" / "bin" / "codex.exe"
    arm.parent.mkdir(parents=True)
    arm.write_bytes(b"MZ")
    return shim, x64, arm


def test_codex_executable_picks_the_vendor_binary_of_this_machine_architecture():
    for mach, want in (("AMD64", "x64"), ("x86_64", "x64"), ("ARM64", "arm64"), ("aarch64", "arm64")):
        with tmpdir() as d:
            shim, x64, arm = _two_vendors(d)
            with mock.patch.object(ap.shutil, "which", _which({"codex": str(shim)})), mock.patch.object(ap, "os", _NtOs()), \
                    machine(mach), mock.patch.object(ap, "CODEX_EXE_NOTE", None):
                got = ap._codex_executable()
                assert ap.CODEX_EXE_NOTE is None, (mach, ap.CODEX_EXE_NOTE)
            assert os.path.samefile(got, x64 if want == "x64" else arm), (mach, got)


def test_codex_executable_unknown_architecture_uses_first_entry_with_a_note():
    with tmpdir() as d:
        shim, x64, arm = _two_vendors(d)
        with mock.patch.object(ap.shutil, "which", _which({"codex": str(shim)})), mock.patch.object(ap, "os", _NtOs()), \
                machine("riscv64"), mock.patch.object(ap, "CODEX_EXE_NOTE", None):
            got = ap._codex_executable()
            note = ap.CODEX_EXE_NOTE
        assert Path(got).name == "codex.exe" and note and "riscv64" in note, (got, note)


def test_codex_executable_returns_the_shim_when_no_vendor_binary_exists():
    with tmpdir() as d:
        shim, _ = _npm_layout(d)
        with mock.patch.object(ap.shutil, "which", _which({"codex": str(shim)})), mock.patch.object(ap, "os", _NtOs()), \
                machine("AMD64"):
            assert ap._codex_executable() == str(shim)


def test_codex_executable_returns_a_real_exe_found_on_path_untouched():
    with tmpdir() as d:
        exe = d / "bin" / "codex.exe"
        exe.parent.mkdir()
        exe.write_bytes(b"MZ")
        _npm_layout(d, NPM_VENDOR)  # a vendor tree exists too, but a direct .exe wins
        with mock.patch.object(ap.shutil, "which", _which({"codex.exe": str(exe)})), mock.patch.object(ap, "os", _NtOs()):
            assert ap._codex_executable() == str(exe)


def test_codex_executable_none_when_not_installed_and_shim_kept_off_windows():
    with mock.patch.object(ap.shutil, "which", _which({})):
        assert ap._codex_executable() is None
    with tmpdir() as d:
        shim, _ = _npm_layout(d, NPM_VENDOR)
        with mock.patch.object(ap.shutil, "which", _which({"codex": str(shim)})), mock.patch.object(ap, "os", k17._PosixOs()):
            assert ap._codex_executable() == str(shim)  # the vendor lookup is Windows only


# ---------------------------------------------------------------- criterion 3: kill the process tree
class FakeProc:
    def __init__(self, pid=4242, alive=True, dies_on_taskkill=False):
        self.pid, self.alive, self.dies_on_taskkill = pid, alive, dies_on_taskkill
        self.killed = 0
        self.stdin = mock.MagicMock()
        self.stdout = io.StringIO("")

    def poll(self):
        return None if self.alive else 0

    def kill(self):
        self.killed += 1
        self.alive = False

    def wait(self, timeout=None):
        return 0


def _taskkill_runner(proc, calls, raises=None):
    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        if raises is not None:
            raise raises
        if proc.dies_on_taskkill:
            proc.alive = False
        return subprocess.CompletedProcess(cmd, 0, b"", b"")
    return fake_run


def test_kill_tree_on_windows_runs_taskkill_tree_force_by_pid():
    proc, calls = FakeProc(pid=777, dies_on_taskkill=True), []
    with mock.patch.object(ap, "os", _NtOs()), mock.patch.object(ap.subprocess, "run", _taskkill_runner(proc, calls)):
        warns = ap._kill_process_tree(proc)
    assert [c[0] for c in calls] == [["taskkill", "/T", "/F", "/PID", "777"]], calls
    assert proc.killed == 0, "taskkill already ended the process: no second kill is needed"
    assert warns == [], warns


def test_kill_tree_falls_back_to_kill_when_taskkill_leaves_the_process_alive():
    proc, calls = FakeProc(), []
    with mock.patch.object(ap, "os", _NtOs()), mock.patch.object(ap.subprocess, "run", _taskkill_runner(proc, calls)):
        warns = ap._kill_process_tree(proc)
    assert len(calls) == 1 and proc.killed == 1, (calls, proc.killed)
    assert warns == [], warns


def test_kill_tree_falls_back_to_kill_when_taskkill_is_missing_or_times_out():
    for exc in (FileNotFoundError(2, "no taskkill"), subprocess.TimeoutExpired("taskkill", 15)):
        proc, calls = FakeProc(), []
        with mock.patch.object(ap, "os", _NtOs()), \
                mock.patch.object(ap.subprocess, "run", _taskkill_runner(proc, calls, raises=exc)):
            warns = ap._kill_process_tree(proc)  # must not raise
        assert proc.killed == 1, (type(exc).__name__, proc.killed)
        assert len(warns) == 1 and "taskkill for app-server pid 4242 failed" in warns[0] and type(exc).__name__ in warns[0], warns


def test_kill_tree_reports_a_failing_taskkill_exit_code_a_failing_kill_and_a_survivor_without_raising():
    def bad_rc(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 128, b"", b"")
    with mock.patch.object(ap, "os", _NtOs()), mock.patch.object(ap.subprocess, "run", bad_rc):
        warns = ap._kill_process_tree(FakeProc(pid=5))
    assert any("taskkill for app-server pid 5 exited 128" in w for w in warns), warns
    stubborn = FakeProc(pid=9)

    def cant_kill():
        raise PermissionError(5, "access denied")
    stubborn.kill = cant_kill
    with mock.patch.object(ap, "os", k17._PosixOs()):
        warns = ap._kill_process_tree(stubborn)
    assert any("kill of app-server pid 9 failed" in w and "PermissionError" in w for w in warns), warns
    assert any("app-server pid 9 still running" in w for w in warns), warns


def test_kill_tree_reports_a_wait_failure_and_survives_a_broken_poll():
    proc = FakeProc(pid=11)

    def bad_wait(timeout=None):
        raise subprocess.TimeoutExpired("x", 5)
    proc.wait = bad_wait
    with mock.patch.object(ap, "os", k17._PosixOs()):
        warns = ap._kill_process_tree(proc)
    assert any("wait for app-server pid 11 failed" in w for w in warns), warns
    broken = FakeProc(pid=12)

    def bad_poll():
        raise RuntimeError("poll exploded")
    broken.poll = bad_poll
    warns = ap._kill_process_tree(broken)  # never raises
    assert any("cleanup of app-server failed" in w and "poll exploded" in w for w in warns), warns


def test_kill_tree_does_nothing_to_a_process_that_already_exited():
    proc, calls = FakeProc(alive=False), []
    with mock.patch.object(ap, "os", _NtOs()), mock.patch.object(ap.subprocess, "run", _taskkill_runner(proc, calls)):
        warns = ap._kill_process_tree(proc)
    assert calls == [] and proc.killed == 0 and warns == [], (calls, proc.killed, warns)


def test_kill_tree_off_windows_skips_taskkill_and_kills():
    proc, calls = FakeProc(), []
    with mock.patch.object(ap, "os", k17._PosixOs()), mock.patch.object(ap.subprocess, "run", _taskkill_runner(proc, calls)):
        warns = ap._kill_process_tree(proc)
    assert calls == [] and proc.killed == 1 and warns == [], (calls, proc.killed, warns)


def _pid_alive_windows(pid):
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                         timeout=20).stdout
    return f'"{pid}"' in out


def test_kill_tree_really_ends_a_grandchild_on_windows():
    if os.name != "nt":
        print("SKIP real tree kill: not Windows")
        return
    code = ("import subprocess,sys,time;"
            "g=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);"
            "print(g.pid,flush=True);time.sleep(120)")
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    grand = None
    try:
        grand = int(child.stdout.readline().strip())
        assert _pid_alive_windows(grand), "setup: the grandchild must be running"
        assert ap._kill_process_tree(child) == []
        child.wait(timeout=10)
        for _ in range(50):
            if not _pid_alive_windows(grand):
                break
            time.sleep(0.1)
        assert not _pid_alive_windows(grand), f"grandchild {grand} survived the tree kill"
    finally:
        if child.poll() is None:
            child.kill()
        if grand and _pid_alive_windows(grand):
            subprocess.run(["taskkill", "/F", "/PID", str(grand)], capture_output=True, timeout=15)


@contextlib.contextmanager
def hooks_list_env(proc, rpc):
    """codex_hooks_list with a fake executable, a fake Popen and a scripted rpc reader; _kill_process_tree is spied."""
    killed = []
    with mock.patch.object(ap, "_codex_executable", lambda: "fake-codex"), \
            mock.patch.object(ap.subprocess, "Popen", lambda *a, **k: proc), \
            mock.patch.object(ap, "_codex_rpc_response", rpc), \
            mock.patch.object(ap, "_kill_process_tree", lambda p: killed.append(p) or []):
        yield killed


def test_codex_hooks_list_kills_the_tree_after_success():
    with tmpdir() as d:
        proc = FakeProc()
        answers = {1: {"result": {}}, 2: {"result": {"data": [{"cwd": str(d), "hooks": []}]}}}
        with hooks_list_env(proc, lambda p, q, rid, t: answers[rid]) as killed:
            got, err = ap.codex_hooks_list([d])
        assert err is None and ap._codex_key(d) in got, (got, err)
        assert killed == [proc], killed


def test_codex_hooks_list_kills_the_tree_on_a_timeout():
    proc = FakeProc()

    def rpc(p, q, rid, t):
        raise TimeoutError("no answer")
    with hooks_list_env(proc, rpc) as killed:
        got, err = ap.codex_hooks_list([Path(".")])
    assert got == {} and err and "failed" in err and "no answer" in err, (got, err)
    assert killed == [proc], killed


def test_codex_hooks_list_kills_the_tree_on_a_caught_error():
    proc = FakeProc()

    def rpc(p, q, rid, t):
        raise ValueError("bad json")
    with hooks_list_env(proc, rpc) as killed:
        got, err = ap.codex_hooks_list([Path(".")])
    assert got == {} and "bad json" in err, (got, err)
    assert killed == [proc], killed


def test_codex_hooks_list_never_raises_an_unexpected_exception_becomes_an_error_and_the_tree_is_killed():
    proc = FakeProc()

    def rpc(p, q, rid, t):
        raise KeyError("surprise")
    with hooks_list_env(proc, rpc) as killed:
        got, err = ap.codex_hooks_list([Path(".")])
    assert got == {} and err and "crashed" in err and "KeyError" in err and "surprise" in err, (got, err)
    assert killed == [proc], killed


def test_codex_hooks_list_appends_cleanup_warnings_to_the_error_so_the_cell_fails():
    proc, killed = FakeProc(), []
    answers = {1: {"result": {}}, 2: {"result": {"data": []}}}
    with mock.patch.object(ap, "_codex_executable", lambda: "fake-codex"), \
            mock.patch.object(ap.subprocess, "Popen", lambda *a, **k: proc), \
            mock.patch.object(ap, "_codex_rpc_response", lambda p, q, rid, t: answers[rid]), \
            mock.patch.object(ap, "_kill_process_tree", lambda p: ["app-server pid 4242 still running"]):
        got, err = ap.codex_hooks_list([Path(".")])
    assert got == {} and err == "app-server cleanup: app-server pid 4242 still running", (got, err)


# ---------------------------------------------------------------- criterion 4: harness hooks loaded and enabled
RUN_HOOK = "py C:/h/hooks/run_hook.py C:/h/hooks/{name} --harness-client=codex"
HARNESS_HOOKS = [("Stop", None, "rules_gate.py"), ("PreToolUse", "Bash", "go_gate.py"),
                 ("PostToolUse", None, "agent_visibility.py")]


def _hooks_json(extra=None):
    hooks = {}
    for event, matcher, name in HARNESS_HOOKS:
        group = {"hooks": [{"type": "command", "command": RUN_HOOK.format(name=name)}]}
        if matcher:
            group["matcher"] = matcher
        hooks.setdefault(event, []).append(group)
    if extra:
        hooks.update(extra)
    return {"hooks": hooks}


def _all_listed(hp, skip=(), disabled=(), enabled_field=True):
    out = []
    for event, matcher, name in HARNESS_HOOKS:
        if name in skip:
            continue
        h = {"source": "user", "trustStatus": "trusted", "sourcePath": str(hp), "eventName": ap.codex_hook_event(event),
             "matcher": matcher, "handlerType": "command", "command": RUN_HOOK.format(name=name)}
        if enabled_field:
            h["enabled"] = name not in disabled
        out.append(h)
    return out


@contextlib.contextmanager
def fake_home(cfg, listed_fn):
    """A temp HOME with .codex/hooks.json and AGENTS.md; the hooks/list result is what a patched codex_hooks_list returns."""
    with tmpdir() as d:
        home, proj = d / "home", d / "proj"
        (home / ".codex").mkdir(parents=True)
        proj.mkdir()
        hp = home / ".codex" / "hooks.json"
        hp.write_text(json.dumps(cfg), encoding="utf-8")
        (home / ".codex" / "AGENTS.md").write_text(ap.RULES_BLOCK + "\n", encoding="utf-8")
        listing = {"cwd": str(proj), "hooks": listed_fn(hp), "warnings": [], "errors": []}
        with mock.patch.object(ap, "codex_hooks_list", lambda cwds: ({ap._codex_key(proj): listing}, None)):
            by_cwd, err = ap.codex_hooks_list([proj])
        with mock.patch.object(ap, "HOME", home), mock.patch.object(ap, "CODEX_HOOKS_BY_CWD", by_cwd), \
                mock.patch.object(ap, "CODEX_HOOKS_ERROR", err):
            yield proj


def test_home_check_passes_when_every_harness_hook_is_loaded_and_enabled():
    with fake_home(_hooks_json(), lambda hp: _all_listed(hp)) as proj:
        st, detail = ap.chk_codex_cfg(proj, home=True)
    assert st == ap.OK, (st, detail)


def test_home_check_flags_a_harness_hook_that_hooks_list_does_not_report():
    with fake_home(_hooks_json(), lambda hp: _all_listed(hp, skip=("go_gate.py",))) as proj:
        st, detail = ap.chk_codex_cfg(proj, home=True)
    assert st == ap.FAIL, (st, detail)
    assert "not loaded by hooks/list: PreToolUse/Bash: go_gate.py" in detail, detail
    assert detail.count("not loaded by hooks/list") == 1, detail  # only the missing one is named


def test_home_check_flags_a_harness_hook_reported_with_enabled_false():
    with fake_home(_hooks_json(), lambda hp: _all_listed(hp, disabled=("rules_gate.py",))) as proj:
        st, detail = ap.chk_codex_cfg(proj, home=True)
    assert st == ap.FAIL, (st, detail)
    assert "disabled in hooks/list: stop/*: rules_gate.py" in detail, detail
    assert "not loaded" not in detail, detail  # it is loaded, just disabled


def test_home_check_treats_a_missing_enabled_field_as_enabled():
    with fake_home(_hooks_json(), lambda hp: _all_listed(hp, enabled_field=False)) as proj:
        st, detail = ap.chk_codex_cfg(proj, home=True)
    assert st == ap.OK, (st, detail)


def test_home_check_counts_duplicates_a_twice_configured_hook_loaded_once_is_flagged():
    cfg = _hooks_json()
    cfg["hooks"]["Stop"].append(cfg["hooks"]["Stop"][0])  # the same handler configured twice
    with fake_home(cfg, lambda hp: _all_listed(hp)) as proj:
        st, detail = ap.chk_codex_cfg(proj, home=True)
    assert st == ap.FAIL and "not loaded by hooks/list" in detail and "rules_gate.py" in detail, (st, detail)


def test_home_check_ignores_foreign_hooks_and_does_not_count_other_source_paths():
    cfg = _hooks_json({"SessionStart": [{"hooks": [{"type": "command", "command": "python3 ~/.codex/hooks/completion_gate.py"}]}]})
    with fake_home(cfg, lambda hp: _all_listed(hp)) as proj:  # the foreign hook is not listed, and that is fine
        st, detail = ap.chk_codex_cfg(proj, home=True)
    assert st == ap.OK, (st, detail)
    # a harness hook listed from another sourcePath does not count as the user hook being loaded
    with fake_home(_hooks_json(), lambda hp: [dict(h, sourcePath=str(hp) + ".other") if h["eventName"] == "stop" else h
                                              for h in _all_listed(hp)]) as proj:
        st, detail = ap.chk_codex_cfg(proj, home=True)
    assert st == ap.FAIL and "not loaded by hooks/list" in detail and "rules_gate.py" in detail, (st, detail)


def test_the_new_hook_checks_run_only_for_the_home_row():
    with fake_home(_hooks_json(), lambda hp: _all_listed(hp, skip=("go_gate.py",), disabled=("rules_gate.py",))) as proj:
        st, detail = ap.chk_codex_cfg(proj, home=False)
    assert "not loaded by hooks/list" not in detail and "disabled in hooks/list" not in detail, (st, detail)


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
