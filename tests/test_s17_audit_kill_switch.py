"""S17-T2 tests: tools/audit_projects.py reports active hook kill switches and Codex harness commands without the client flag.
Written from the S17-T2 acceptance criteria. os.environ is patched inside each test (restored afterwards) and winreg is a
fake module put into sys.modules, so the real environment and the real registry are never read or changed.
Run: C:\\Python314\\python.exe -I tests\\test_s17_audit_kill_switch.py"""
import contextlib
import importlib.util
import os
import sys
import tempfile
import shutil
import types
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parent.parent / "tools"
_s = importlib.util.spec_from_file_location("audit_projects_s17", TOOLS / "audit_projects.py")
ap = importlib.util.module_from_spec(_s)
_s.loader.exec_module(ap)

USER_SUB = r"Environment"
MACHINE_SUB = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
OPT_IN_VARS = ("CLAUDE_ROSETTA_GATE", "CLAUDE_SHELL_POLICY_AUDIT")  # opt-in modes: never kill switches
SESSION_MODE_VARS = ("SESSION_SUPERVISOR_MODE", "SESSION_CLOSE_GATE_MODE")  # kill switches for "off" only
MODE_VARS = OPT_IN_VARS + SESSION_MODE_VARS
ALL3 = {"off", "0", "false"}


def fake_winreg(user=(), machine=(), user_err=None, machine_err=None, user_enum_err=None, machine_enum_err=None):
    """A winreg stand-in. user/machine: lists of (name, value); *_err: exception raised by OpenKey for that scope;
    *_enum_err: exception EnumValue raises once the listed values are exhausted (instead of the normal error 259)."""
    m = types.ModuleType("winreg")
    m.HKEY_CURRENT_USER, m.HKEY_LOCAL_MACHINE = "HKCU", "HKLM"
    data = {("HKCU", USER_SUB): (list(user), user_err, user_enum_err),
            ("HKLM", MACHINE_SUB): (list(machine), machine_err, machine_enum_err)}

    class Key:
        def __init__(self, vals, enum_err):
            self.vals, self.enum_err = vals, enum_err

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def OpenKey(root, sub):
        vals, err, enum_err = data[(root, sub)]
        if err is not None:
            raise err
        return Key(vals, enum_err)

    def EnumValue(key, i):
        if i >= len(key.vals):
            raise key.enum_err if key.enum_err is not None else OSError(259, "no more data")
        n, v = key.vals[i]
        return n, v, 1

    m.OpenKey, m.EnumValue = OpenKey, EnumValue
    return m


@contextlib.contextmanager
def env(winreg_mod, **set_vars):
    """Process env with every kill-switch-like and mode variable removed, then set_vars applied; fake winreg installed."""
    drop = ap.kill_switch_names() | set(MODE_VARS)  # read once: the scan of the hook sources is slow per call
    clean = {k: v for k, v in os.environ.items() if k not in drop}
    clean.update(set_vars)
    with mock.patch.dict(os.environ, clean, clear=True), mock.patch.dict(sys.modules, {"winreg": winreg_mod}):
        yield


def items(detail):
    return {x.strip() for x in detail.split(";")[0].split(",")}


# ---------------------------------------------------------------- criterion 1
def test_names_found_in_real_hook_sources_session_modes_in_and_opt_in_modes_out():
    names = ap.kill_switch_names()
    for must in ("CLAUDE_GO_GATE", "HARNESS_SECRET_GUARD", "CODEX_CITATION_JUDGE", "CLAUDE_CITATION_JUDGE",
                 "CODEX_EVIDENCE_GATE", "CLAUDE_EVIDENCE_GATE") + SESSION_MODE_VARS:
        assert must in names, (must, sorted(names))
    for mode in OPT_IN_VARS:
        assert mode not in names, mode
    assert names == set(ap.kill_switch_values())


def test_honoured_values_per_name_from_real_hook_sources():
    vals = ap.kill_switch_values()
    assert vals["CLAUDE_GO_GATE"] == {"off"}, vals["CLAUDE_GO_GATE"]            # go_gate.py compares == "off"
    assert vals["HARNESS_SECRET_GUARD"] == ALL3, vals["HARNESS_SECRET_GUARD"]    # secret_guard.py: in {"off","0","false"}
    for n in ("CLAUDE_EVIDENCE_GATE", "CODEX_EVIDENCE_GATE"):                    # evidence_gate off('NAME') indirect
        assert vals[n] == ALL3, (n, vals[n])
    for n in SESSION_MODE_VARS:
        assert vals[n] == {"off"}, (n, vals[n])


def test_names_from_synthetic_hook_folder():
    d = Path(tempfile.mkdtemp(prefix="s17t2_"))
    try:
        hooks = d / "harness" / "hooks"
        hooks.mkdir(parents=True)
        (hooks / "a.py").write_text(
            'import os\n'
            'if os.environ.get("MY_SWITCH_ONE", "").lower() == "off":\n    pass\n'
            'if any(os.environ.get(n, "").lower() in {"off", "0", "false"} for n in ("MY_TUPLE_A", "MY_TUPLE_B")):\n    pass\n'
            'if os.environ.get("SESSION_SUPERVISOR_MODE", "").lower() == "off":\n    pass\n'
            'if os.environ.get("NOT_A_SWITCH_LIVE", "").lower() == "live":\n    pass\n'
            'x = os.environ.get("PLAIN_READ_ONLY")\n'
            'if evidence_gate.off("MY_INDIRECT"):\n    pass\n', encoding="utf-8")
        with mock.patch.object(ap, "HARNESS", d):
            got = ap.kill_switch_names()
        assert got == {"MY_SWITCH_ONE", "MY_TUPLE_A", "MY_TUPLE_B", "MY_INDIRECT", *SESSION_MODE_VARS}, got
        with mock.patch.object(ap, "HARNESS", d):
            vals = ap.kill_switch_values()
        assert vals == {"MY_SWITCH_ONE": {"off"}, "MY_TUPLE_A": ALL3, "MY_TUPLE_B": ALL3, "MY_INDIRECT": ALL3,
                        "SESSION_SUPERVISOR_MODE": {"off"}, "SESSION_CLOSE_GATE_MODE": {"off"}}, vals
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- criterion 2
def test_reports_only_name_value_scope_for_off_values_in_three_scopes():
    wr = fake_winreg(user=[("HARNESS_SECRET_GUARD", "0"), ("CLAUDE_QUESTION_POLICY", "on"), ("USER_SECRET_TOKEN", "off"),
                           ("Path", "C:\\secret\\bin")],
                     machine=[("CLAUDE_EVIDENCE_GATE", "False"), ("SESSION_SUPERVISOR_MODE", "off"), ("OTHER", "hunter2"),
                              ("CLAUDE_ROSETTA_GATE", "off")])
    with env(wr, CLAUDE_GO_GATE="off", CLAUDE_PLAN_FIRST_GATE="enabled", SOME_OTHER_VAR="hunter3", SESSION_CLOSE_GATE_MODE="off",
             CLAUDE_SHELL_POLICY_AUDIT="off"):
        status, detail = ap.chk_kill_switch()
    assert status == ap.FAIL, (status, detail)
    assert items(detail) == {"CLAUDE_GO_GATE=off (process)", "SESSION_CLOSE_GATE_MODE=off (process)",
                             "HARNESS_SECRET_GUARD=0 (user)", "CLAUDE_EVIDENCE_GATE=False (machine)",
                             "SESSION_SUPERVISOR_MODE=off (machine)"}, detail
    for leak in ("hunter", "secret", "enabled", "USER_SECRET_TOKEN", "ROSETTA", "SHELL_POLICY", "on ("):
        assert leak not in detail, (leak, detail)


def test_each_off_value_spelling_counts_for_a_switch_that_honours_all_three():
    for val in ("off", "0", "false", "OFF", " False "):
        with env(fake_winreg(), HARNESS_SECRET_GUARD=val):
            st, detail = ap.chk_kill_switch()
        assert st == ap.FAIL and f"HARNESS_SECRET_GUARD={val} (process)" in detail, (val, st, detail)
    for val in ("on", "1", "true", "", "live", "no"):
        with env(fake_winreg(), HARNESS_SECRET_GUARD=val):
            st, detail = ap.chk_kill_switch()
        assert st == ap.OK, (val, st, detail)


def test_switch_that_honours_only_off_ignores_zero_and_false():
    for val in ("off", "OFF", " off "):
        with env(fake_winreg(), CLAUDE_GO_GATE=val):
            st, detail = ap.chk_kill_switch()
        assert st == ap.FAIL and f"CLAUDE_GO_GATE={val} (process)" in detail, (val, st, detail)
    for val in ("0", "false", "False", "on", ""):
        with env(fake_winreg(), CLAUDE_GO_GATE=val):
            st, detail = ap.chk_kill_switch()
        assert st == ap.OK, (val, st, detail)
    for n in SESSION_MODE_VARS:  # the session modes honour only off as well
        with env(fake_winreg(), **{n: "0"}):
            assert ap.chk_kill_switch()[0] == ap.OK, n
        with env(fake_winreg(), **{n: "off"}):
            assert ap.chk_kill_switch()[0] == ap.FAIL, n


def test_go_gate_zero_not_reported_but_secret_guard_zero_is_in_process_and_registry():
    with env(fake_winreg(), CLAUDE_GO_GATE="0", HARNESS_SECRET_GUARD="0"):
        st, detail = ap.chk_kill_switch()
    assert st == ap.FAIL and items(detail) == {"HARNESS_SECRET_GUARD=0 (process)"}, (st, detail)
    with env(fake_winreg(user=[("CLAUDE_GO_GATE", "0")], machine=[("HARNESS_SECRET_GUARD", "0")])):
        st, detail = ap.chk_kill_switch()
    assert st == ap.FAIL and items(detail) == {"HARNESS_SECRET_GUARD=0 (machine)"}, (st, detail)
    with env(fake_winreg(user=[("CLAUDE_GO_GATE", "0")], machine=[("CLAUDE_GO_GATE", "false")])):
        assert ap.chk_kill_switch()[0] == ap.OK


def test_registry_names_compare_case_insensitively():
    wr = fake_winreg(user=[("claude_go_gate", "off")], machine=[("Harness_Secret_Guard", "0"), ("claude_go_gate", "0")])
    with env(wr):
        st, detail = ap.chk_kill_switch()
    assert st == ap.FAIL, (st, detail)
    assert items(detail) == {"claude_go_gate=off (user)", "Harness_Secret_Guard=0 (machine)"}, detail


# ---------------------------------------------------------------- criterion 3
def test_no_switch_set_is_ok():
    with env(fake_winreg(user=[("Path", "x")], machine=[("TEMP", "y")])):
        st, detail = ap.chk_kill_switch()
    assert st == ap.OK and "none off" in detail, (st, detail)


def test_missing_registry_key_is_not_an_error():
    with env(fake_winreg(user_err=FileNotFoundError(2, "no key"), machine_err=FileNotFoundError(2, "no key"))):
        st, detail = ap.chk_kill_switch()
    assert st == ap.OK, (st, detail)


# ---------------------------------------------------------------- criterion 4
def test_registry_read_error_without_switch_is_fail_with_reason():
    with env(fake_winreg(user_err=PermissionError(5, "access denied USERKEY"))):
        st, detail = ap.chk_kill_switch()
    assert st == ap.FAIL, (st, detail)
    assert "user registry unreadable" in detail and "access denied USERKEY" in detail, detail


def test_machine_registry_error_is_fail_with_reason():
    with env(fake_winreg(machine_err=OSError(1, "boom MACHINEKEY"))):
        st, detail = ap.chk_kill_switch()
    assert st == ap.FAIL and "machine registry unreadable" in detail and "boom MACHINEKEY" in detail, (st, detail)


def test_enumvalue_error_other_than_259_is_fail_not_end_of_list():
    for scope, kw in (("user", "user_enum_err"), ("machine", "machine_enum_err")):
        wr = fake_winreg(**{kw: OSError(5, "enum denied ENUMKEY")})
        with env(wr):
            st, detail = ap.chk_kill_switch()
        assert st == ap.FAIL and f"{scope} registry unreadable" in detail and "ENUMKEY" in detail, (scope, st, detail)
    # the error comes after a readable value: the value found before it is still reported next to the failure
    wr = fake_winreg(user=[("HARNESS_SECRET_GUARD", "off")], user_enum_err=OSError(1, "enum boom"))
    with env(wr):
        st, detail = ap.chk_kill_switch()
    assert st == ap.FAIL and "HARNESS_SECRET_GUARD=off (user)" in detail and "user registry unreadable" in detail, detail


def test_enumvalue_error_259_by_errno_or_winerror_ends_the_list_cleanly():
    with env(fake_winreg(user=[("Path", "x")], user_enum_err=OSError(259, "no more"))):
        assert ap.chk_kill_switch()[0] == ap.OK
    exc = OSError(0, "no more items", None, 259)  # winerror form (Windows only; elsewhere winerror is not set)
    if getattr(exc, "winerror", None) == 259:
        with env(fake_winreg(machine=[("Path", "x")], machine_enum_err=exc)):
            st, detail = ap.chk_kill_switch()
        assert st == ap.OK, (st, detail)
    else:
        print("SKIP winerror form: OSError has no winerror here")


class _PosixOs:
    """Proxy for audit_projects.os: reports a non-Windows host, everything else is the real os module."""
    name = "posix"

    def __getattr__(self, attr):
        return getattr(os, attr)


def test_non_windows_host_is_na_with_reason_and_process_switch_still_fails():
    with env(fake_winreg()), mock.patch.object(ap, "os", _PosixOs()):
        st, detail = ap.chk_kill_switch()
    assert st == ap.NA and "not Windows" in detail and "registry" in detail, (st, detail)
    with env(fake_winreg(user=[("HARNESS_SECRET_GUARD", "off")]), HARNESS_SECRET_GUARD="0"), \
            mock.patch.object(ap, "os", _PosixOs()):
        st, detail = ap.chk_kill_switch()
    assert st == ap.FAIL and "HARNESS_SECRET_GUARD=0 (process)" in detail and "(user)" not in detail, (st, detail)


def test_registry_read_error_with_found_switch_is_fail_carrying_both():
    with env(fake_winreg(machine_err=PermissionError(5, "access denied MACHINEKEY")), CLAUDE_GO_GATE="off"):
        st, detail = ap.chk_kill_switch()
    assert st == ap.FAIL, (st, detail)
    assert "CLAUDE_GO_GATE=off (process)" in detail and "machine registry unreadable" in detail \
        and "access denied MACHINEKEY" in detail, detail


def test_switch_found_in_readable_scope_survives_error_in_other_scope():
    wr = fake_winreg(user=[("HARNESS_SECRET_GUARD", "off")], machine_err=PermissionError(5, "denied"))
    with env(wr):
        st, detail = ap.chk_kill_switch()
    assert st == ap.FAIL and "HARNESS_SECRET_GUARD=off (user)" in detail and "machine registry unreadable" in detail, detail


# ---------------------------------------------------------------- criterion 5
def _cfg(*cmds, key="command", event="Stop"):
    return {"hooks": {event: [{"hooks": [{"type": "command", key: c} for c in cmds]}]}}


def test_codex_harness_command_without_flag_is_flagged():
    cfg = _cfg("py C:/h/hooks/run_hook.py C:/h/hooks/rules_gate.py")
    bad = ap.codex_commands_lacking_flag(cfg, "~/.codex/hooks.json")
    assert len(bad) == 1 and bad[0].startswith("~/.codex/hooks.json Stop command:"), bad


def test_codex_commandwindows_without_flag_is_flagged_and_with_flag_is_not():
    assert len(ap.codex_commands_lacking_flag(_cfg("py C:\\h\\run_hook.py x.py", key="commandWindows"), "w")) == 1
    ok = _cfg("py C:/h/hooks/run_hook.py x.py --harness-client=codex")
    assert ap.codex_commands_lacking_flag(ok, "w") == []
    wrong = _cfg("py C:/h/hooks/run_hook.py x.py --harness-client=claude")
    assert len(ap.codex_commands_lacking_flag(wrong, "w")) == 1


def test_foreign_codex_command_is_ignored():
    cfg = _cfg("python3 ~/.codex/hooks/completion_gate.py")
    assert ap.codex_commands_lacking_flag(cfg, "w") == []
    mixed = _cfg("python3 ~/.codex/hooks/completion_gate.py", "py C:/h/run_hook.py a.py")
    assert len(ap.codex_commands_lacking_flag(mixed, "w")) == 1


def test_malformed_codex_config_does_not_crash():
    for cfg in (None, [], {"hooks": []}, {"hooks": {"Stop": "x"}}, {"hooks": {"Stop": [None, {"hooks": ["s", 3]}]}}, {}):
        assert ap.codex_commands_lacking_flag(cfg, "w") == [], cfg


# ---------------------------------------------------------------- criterion 6
SENTINEL = ("SENT", "sentinel detail")


def _patched(names):
    return [mock.patch.object(ap, n, lambda *a, **k: SENTINEL) for n in names]


def test_kill_switch_column_is_declared():
    assert "kill_switch" in ap.COLS


def test_client_rows_have_kill_switch_cell_from_the_check():
    all_checks = ["chk_gates_live", "chk_claude_cfg", "chk_codex_cfg", "chk_kill_switch"]
    for client in ("claude-home", "codex-home"):
        with contextlib.ExitStack() as st:
            for p in _patched(all_checks):
                st.enter_context(p)
            row = ap.audit_client(client)
        assert row["project"] == client
        assert row["cells"]["kill_switch"] == SENTINEL, (client, row["cells"])
        assert set(row["cells"]) == set(ap.COLS), row["cells"]


def test_codex_home_row_asks_for_home_check_of_codex_cfg():
    calls = []

    def spy(*a, **k):
        calls.append((a, k))
        return SENTINEL

    with contextlib.ExitStack() as st:
        for p in _patched(["chk_gates_live", "chk_kill_switch"]):
            st.enter_context(p)
        st.enter_context(mock.patch.object(ap, "chk_codex_cfg", spy))
        ap.audit_client("codex-home")
    assert len(calls) == 1 and calls[0][0][-1] is True, calls


def test_client_row_survives_crashing_kill_switch_check():
    def boom():
        raise RuntimeError("kaput")

    with contextlib.ExitStack() as st:
        for p in _patched(["chk_gates_live", "chk_claude_cfg"]):
            st.enter_context(p)
        st.enter_context(mock.patch.object(ap, "chk_kill_switch", boom))
        row = ap.audit_client("claude-home")
    st_, detail = row["cells"]["kill_switch"]
    assert st_ == ap.FAIL and "kaput" in detail, row["cells"]["kill_switch"]


def test_project_rows_show_na_for_kill_switch():
    checks = ["chk_hooks_path", "chk_tools_current", "chk_prepush_current", "chk_ci_local", "chk_ci_github", "chk_ledger_whole",
              "chk_gates_live", "chk_codex_cfg", "chk_kill_switch"]
    with contextlib.ExitStack() as st:
        for p in _patched(checks):
            st.enter_context(p)
        row = ap.audit_project(Path("X:/nonexistent_project_s17"), False)
    assert row["cells"]["kill_switch"] == (ap.NA, ""), row["cells"]["kill_switch"]
    assert row["cells"]["hooks_path"] == SENTINEL  # control: the other checks did run through the same path


def main():
    fails = 0
    for name in [n for n, f in list(globals().items()) if n.startswith("test_") and callable(f)]:
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
