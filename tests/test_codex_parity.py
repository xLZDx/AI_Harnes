"""S3-T3: Codex parity: project skills sync, project hooks.json (build.py --codex-project) and tools/install_codex.py.
Expected values come from the task brief (acceptance criteria 1-3), not from the code.
Backup layout follows the existing agents convention: <backup>/<project folder name>/..."""
import hashlib
import json
import shutil
import sys
import importlib.util
from pathlib import Path

_s = importlib.util.spec_from_file_location("hv2", Path(__file__).resolve().parent / "hv2.py")
h = importlib.util.module_from_spec(_s)
sys.modules["hv2"] = h
_s.loader.exec_module(h)

PYX = "C:\\Python314\\python.exe"
GOOD_AGENT = "---\nname: alpha\ndescription: Does alpha\n---\nBody A\n"
_BUILT = []


def _build_global():
    if not _BUILT:
        r = h.run([PYX, "-s", str(h.SRC / "tools" / "build.py"), "--python", PYX], h.SRC)
        assert r.rc == 0, r.all
        _BUILT.append(h.SRC / "out" / "codex")
    return _BUILT[0]


def _proj(name="proj"):
    root = h.fresh("cp")
    p = root / name
    (p / ".claude" / "agents").mkdir(parents=True)
    (p / ".claude" / "agents" / "alpha.md").write_bytes(GOOD_AGENT.encode("utf-8"))
    return p, root / "bk"


def _cp(p, b):
    return h.run([PYX, "-s", str(h.SRC / "tools" / "build.py"), "--codex-project", str(p), "--backup", str(b)], h.SRC)


def _tree(*roots):
    snap = {}
    for r in roots:
        if r.exists():
            for f in sorted(r.rglob("*")):
                snap[str(f)] = f.read_bytes() if f.is_file() else None
    return snap


def _w(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _skill(p, name, files):
    for rel, text in files.items():
        _w(p / ".claude" / "skills" / name / rel, text)


def _settings(p, hooks):
    _w(p / ".claude" / "settings.json", json.dumps({"hooks": hooks}))


def _hooks_json(p):
    return json.loads((p / ".codex" / "hooks.json").read_text(encoding="utf-8"))


# ======================= criterion 1: skills =======================
def test_skills_with_skill_md_are_copied_with_same_files_others_ignored():
    p, b = _proj()
    _skill(p, "sk-a", {"SKILL.md": "# A\n", "refs/x.txt": "xx\n", "refs/deep/y.bin": "yy"})
    (p / ".claude" / "skills" / "no-skill-md").mkdir(parents=True)
    _w(p / ".claude" / "skills" / "no-skill-md" / "readme.txt", "no SKILL.md here\n")
    _w(p / ".claude" / "skills" / "loose-file.md", "a file, not a dir\n")
    res = _cp(p, b)
    assert res.rc == 0, res.all
    d = p / ".agents" / "skills"
    assert sorted(x.name for x in d.iterdir()) == ["sk-a"], [x.name for x in d.iterdir()]
    for rel, text in {"SKILL.md": "# A\n", "refs/x.txt": "xx\n", "refs/deep/y.bin": "yy"}.items():
        assert (d / "sk-a" / rel).read_bytes() == text.encode(), rel
    assert not (b / "proj" / "skills").exists(), "nothing replaced, so no skills backup"


def test_existing_different_skill_is_backed_up_then_replaced():
    p, b = _proj()
    _skill(p, "sk-a", {"SKILL.md": "# new\n", "extra.txt": "new extra\n"})
    _w(p / ".agents" / "skills" / "sk-a" / "SKILL.md", "# old\n")
    _w(p / ".agents" / "skills" / "sk-a" / "old-only.txt", "old only\n")
    res = _cp(p, b)
    assert res.rc == 0, res.all
    bk = b / "proj" / "skills" / "sk-a"
    assert (bk / "SKILL.md").read_bytes() == b"# old\n"
    assert (bk / "old-only.txt").read_bytes() == b"old only\n"
    cur = p / ".agents" / "skills" / "sk-a"
    assert (cur / "SKILL.md").read_bytes() == b"# new\n"
    assert (cur / "extra.txt").read_bytes() == b"new extra\n"
    assert not (cur / "old-only.txt").exists()


def test_skill_only_in_agents_is_moved_to_backup():
    p, b = _proj()
    _skill(p, "sk-a", {"SKILL.md": "# A\n"})
    _w(p / ".agents" / "skills" / "only-codex" / "SKILL.md", "# codex only\n")
    _w(p / ".agents" / "skills" / "only-codex" / "sub" / "f.txt", "f\n")
    res = _cp(p, b)
    assert res.rc == 0, res.all
    assert not (p / ".agents" / "skills" / "only-codex").exists()
    bk = b / "proj" / "skills" / "only-codex"
    assert (bk / "SKILL.md").read_bytes() == b"# codex only\n"
    assert (bk / "sub" / "f.txt").read_bytes() == b"f\n"
    assert (p / ".agents" / "skills" / "sk-a" / "SKILL.md").is_file()


def test_identical_skill_is_left_alone_and_rerun_is_stable():
    p, b = _proj()
    _skill(p, "sk-a", {"SKILL.md": "# A\n", "r/x.txt": "x\n"})
    assert _cp(p, b).rc == 0
    f = p / ".agents" / "skills" / "sk-a" / "SKILL.md"
    m1 = f.stat().st_mtime_ns
    before = _tree(p / ".agents")
    res = _cp(p, h.fresh("cp") / "bk2")
    assert res.rc == 0, res.all
    assert f.stat().st_mtime_ns == m1, "identical skill must not be rewritten"
    assert _tree(p / ".agents") == before


def test_identical_skill_makes_no_skills_backup():
    p, b = _proj()
    _skill(p, "sk-a", {"SKILL.md": "# A\n"})
    _w(p / ".agents" / "skills" / "sk-a" / "SKILL.md", "# A\n")
    res = _cp(p, b)
    assert res.rc == 0, res.all
    assert not (b / "proj" / "skills").exists()


# ======================= criterion 2: project hooks =======================
def _hk(args=None, timeout=None, cmd="C:/proj venv/py.exe", status=None):
    e = {"type": "command", "command": cmd}
    if args is not None:
        e["args"] = args
    if timeout is not None:
        e["timeout"] = timeout
    if status:
        e["statusMessage"] = status
    return e


def test_project_without_hooks_gets_no_hooks_json():
    p, b = _proj()
    res = _cp(p, b)  # no settings.json at all
    assert res.rc == 0, res.all
    assert not (p / ".codex" / "hooks.json").exists()
    _settings(p, {})
    assert _cp(p, h.fresh("cp") / "bk").rc == 0
    assert not (p / ".codex" / "hooks.json").exists()
    _w(p / ".claude" / "settings.json", json.dumps({"permissions": {}}))
    assert _cp(p, h.fresh("cp") / "bk").rc == 0
    assert not (p / ".codex" / "hooks.json").exists()


def test_matcher_mapping_dedupe_order_and_dropped_groups():
    p, b = _proj()
    one = [_hk(["${CLAUDE_PROJECT_DIR}/s.py"])]
    _settings(p, {
        "PreToolUse": [
            {"matcher": "Agent|Task", "hooks": one},
            {"matcher": "Bash|PowerShell", "hooks": one},
            {"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": one},
            {"matcher": "Write|Edit|Bash|Edit", "hooks": one},
            {"matcher": "Task|Bash|Agent", "hooks": one},
            {"matcher": "AskUserQuestion|ExitPlanMode", "hooks": one},
            {"matcher": "AskUserQuestion|Bash", "hooks": one},
            {"hooks": one},
        ],
        "Notification": [{"hooks": one}],
        "Stop": [{"hooks": one}],
    })
    res = _cp(p, b)
    assert res.rc == 0, res.all
    cfg = _hooks_json(p)["hooks"]
    assert "Notification" not in cfg
    pre = cfg["PreToolUse"]
    matchers = [g.get("matcher") for g in pre]
    assert matchers == ["spawn_agent", "Bash", "apply_patch", "apply_patch|Bash", "spawn_agent|Bash", "Bash", None], matchers
    assert "matcher" not in pre[-1]
    assert "Stop" in cfg and len(cfg["Stop"]) == 1


def test_command_shape_project_root_substitution_spaces_and_timeouts():
    p, b = _proj("my proj")
    _settings(p, {"PreToolUse": [{"matcher": "Bash", "hooks": [
        _hk(["${CLAUDE_PROJECT_DIR}/.claude/hooks/g.py", "--flag", "two words", "${CLAUDE_PROJECT_DIR}/x y"], timeout=20,
            status="mine"),
        _hk(["plain.py"]),
    ]}]})
    res = _cp(p, b)
    assert res.rc == 0, res.all
    root = str(p).replace("\\", "/")
    ents = _hooks_json(p)["hooks"]["PreToolUse"][0]["hooks"]
    assert len(ents) == 2
    e0, e1 = ents
    assert e0["timeout"] == 30 and e1["timeout"] == 40, (e0["timeout"], e1["timeout"])
    assert e0["statusMessage"] == "mine"
    assert e0["type"] == "command"
    c = e0["command"]
    assert "${CLAUDE_PROJECT_DIR}" not in c
    assert "codex_spawn_shim.py" in c
    # order after the shim: project root, project interpreter, script, args; spaces are quoted
    tail = c[c.index("codex_spawn_shim.py"):]
    # the program and the shim path have no space (unquoted, like the working Codex hook); the project timeout is
    # passed to the shim; parts with a space are double-quoted
    expect = (f'codex_spawn_shim.py --timeout=20 "{root}" "C:/proj venv/py.exe" "{root}/.claude/hooks/g.py" --flag '
              f'"two words" "{root}/x y"')
    assert tail == expect, tail
    assert e0["commandWindows"] == c or "codex_spawn_shim.py" in e0["commandWindows"]
    assert e1["command"].endswith(' "C:/proj venv/py.exe" plain.py'), e1["command"]


def test_existing_hooks_json_copied_to_backup_before_overwrite():
    p, b = _proj()
    _settings(p, {"Stop": [{"hooks": [_hk(["s.py"])]}]})
    _w(p / ".codex" / "hooks.json", '{"old": "marker"}\n')
    res = _cp(p, b)
    assert res.rc == 0, res.all
    assert (b / "proj" / "hooks.json.before").read_bytes() == b'{"old": "marker"}\n'
    new = _hooks_json(p)
    assert "old" not in new and "Stop" in new["hooks"]


def test_no_hooks_leaves_existing_hooks_json_untouched():
    p, b = _proj()
    _w(p / ".codex" / "hooks.json", '{"keep": 1}\n')
    res = _cp(p, b)
    assert res.rc == 0, res.all
    assert (p / ".codex" / "hooks.json").read_bytes() == b'{"keep": 1}\n'


# ======================= criterion 3: installer =======================
OLD_COMPLETION = ('"C:/Python314/python.exe" "C:/old/hooks/completion_gate.py"')
OLD_EVIDENCE = ('"C:/Python314/python.exe" "C:/old/hooks/evidence_gate.py" --old-marker')


def _home(config="[features]\nhooks = true\n", hooks_json=True):
    root = h.fresh("ih")
    home = root / "codex_home"
    home.mkdir()
    _w(home / "config.toml", config)
    _w(home / "AGENTS.md", "old agents md\n")
    _w(home / "agents" / "stale-agent.toml", "stale\n")
    _w(home / "agents" / "keepme.txt", "not toml\n")
    _w(home / "skills" / "my-own-skill" / "SKILL.md", "mine\n")
    _w(home / "prompts" / "old.md", "old prompt\n")
    _w(home / "hooks" / "old_hook.py", "print(1)\n")
    if hooks_json:
        _w(home / "hooks.json", json.dumps({"custom": {"k": 1}, "hooks": {
            "SessionEnd": [{"hooks": [{"type": "command", "command": OLD_COMPLETION, "timeout": 5}]}],
            "Stop": [{"hooks": [{"type": "command", "command": OLD_EVIDENCE, "timeout": 5}]}],
        }}))
    return root, home


def _inst(home, snap, source=None):
    args = [PYX, "-s", str(h.SRC / "tools" / "install_codex.py"), "--home", str(home), "--snapshot", str(snap)]
    if source is not None:
        args += ["--source", str(source)]
    return h.run(args, h.SRC)


def _src_files(d):
    return {f.relative_to(d).as_posix(): f.read_bytes() for f in d.rglob("*") if f.is_file()}


def test_install_happy_path_overwrites_snapshots_and_never_touches_config():
    src = _build_global()
    root, home = _home()
    snap = root / "snap"
    cfg_before = (home / "config.toml").read_bytes()
    before = {k: v for k, v in _tree(home).items()}
    res = _inst(home, snap, src)
    assert res.rc == 0, res.all
    assert (home / "config.toml").read_bytes() == cfg_before, "config.toml must stay byte-identical"
    # overwritten from the source
    assert (home / "AGENTS.md").read_bytes() == (src / "AGENTS.md").read_bytes()
    for t in (src / "agents").glob("*.toml"):
        assert (home / "agents" / t.name).read_bytes() == t.read_bytes()
    for p in (src / "prompts").glob("*.md"):
        assert (home / "prompts" / p.name).read_bytes() == p.read_bytes()
    for f in (src / "hooks").iterdir():
        if f.is_file():
            assert (home / "hooks" / f.name).read_bytes() == f.read_bytes()
    for d in (src / "skills").iterdir():
        if d.is_dir():
            assert _src_files(home / "skills" / d.name) == _src_files(d), d.name
    # unrelated items stay
    assert (home / "skills" / "my-own-skill" / "SKILL.md").read_bytes() == b"mine\n"
    assert (home / "agents" / "keepme.txt").read_bytes() == b"not toml\n"
    assert (home / "prompts" / "old.md").read_bytes() == b"old prompt\n"
    assert (home / "hooks" / "old_hook.py").read_bytes() == b"print(1)\n"
    # stale agent moved out
    assert not (home / "agents" / "stale-agent.toml").exists()
    assert (snap / "moved-out" / "agents" / "stale-agent.toml").read_bytes() == b"stale\n"
    # snapshot holds the pre-install state and restore.ps1
    assert (snap / "restore.ps1").is_file() and (snap / "restore.ps1").stat().st_size > 0
    for rel in ("AGENTS.md", "config.toml", "hooks.json", "agents/stale-agent.toml", "skills/my-own-skill/SKILL.md",
                "prompts/old.md", "hooks/old_hook.py"):
        assert (snap / rel).is_file(), rel
        assert (snap / rel).read_bytes() == before[str(home / rel.replace("/", "\\"))], rel
    assert (snap / "AGENTS.md").read_bytes() == b"old agents md\n"
    assert (snap / "config.toml").read_bytes() == cfg_before
    assert str(snap) in res.all or snap.name in res.all


def test_install_merges_hooks_json_keeps_sessionend_replaces_stop_evidence_gate():
    src = _build_global()
    new = json.loads((src / "hooks.json").read_text(encoding="utf-8"))
    assert "completion_gate.py" not in json.dumps(new), "precondition: harness does not ship completion_gate"
    new_stop_ev = [g for g in new["hooks"]["Stop"] if "evidence_gate.py" in json.dumps(g)]
    assert new_stop_ev, "precondition: harness ships a Stop evidence_gate entry"
    root, home = _home()
    res = _inst(home, root / "snap", src)
    assert res.rc == 0, res.all
    m = json.loads((home / "hooks.json").read_text(encoding="utf-8"))
    assert m["custom"] == {"k": 1}, "unrelated top-level key kept"
    assert m["hooks"]["SessionEnd"][0]["hooks"][0]["command"] == OLD_COMPLETION
    assert "SessionEnd" in m["hooks"]
    text = json.dumps(m)
    assert "--old-marker" not in text and "C:/old/hooks/evidence_gate.py" not in text, "old evidence_gate entry replaced"
    stop_ev = [g for g in m["hooks"]["Stop"] if "evidence_gate.py" in json.dumps(g)]
    assert len(stop_ev) == len(new_stop_ev)
    # every new event/group is present, appended after the old ones
    for ev, groups in new["hooks"].items():
        for g in groups:
            assert g in m["hooks"][ev], (ev, g)
    assert [g for g in m["hooks"]["SessionEnd"]][0]["hooks"][0]["timeout"] == 5
    assert not (home / "hooks.json.tmp").exists()
    assert "trust" not in text.lower()


def test_install_without_existing_hooks_json_writes_new_one():
    src = _build_global()
    root, home = _home(hooks_json=False)
    res = _inst(home, root / "snap", src)
    assert res.rc == 0, res.all
    assert json.loads((home / "hooks.json").read_text(encoding="utf-8"))["hooks"] == \
        json.loads((src / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert not (root / "snap" / "hooks.json").exists()


def test_install_second_run_with_same_snapshot_is_refused_and_changes_nothing():
    src = _build_global()
    root, home = _home()
    snap = root / "snap"
    assert _inst(home, snap, src).rc == 0
    after1 = _tree(home, snap)
    res = _inst(home, snap, src)
    assert res.rc == 1, res.all
    assert _tree(home, snap) == after1


def test_install_stops_when_snapshot_exists_touching_nothing():
    src = _build_global()
    root, home = _home()
    snap = root / "snap"
    snap.mkdir()
    before = _tree(home, snap)
    res = _inst(home, snap, src)
    assert res.rc == 1, res.all
    assert _tree(home, snap) == before


def test_install_stops_when_hooks_true_missing_touching_nothing():
    src = _build_global()
    for cfg in ("[features]\nother = true\n", "[features]\nhooks = false\n", "model = 'x'\n",
                "[other]\nhooks = true\n[features]\nx = 1\n", "# hooks = true\n[features]\n"):
        root, home = _home(config=cfg)
        snap = root / "snap"
        before = _tree(home)
        res = _inst(home, snap, src)
        assert res.rc == 1, (cfg, res.all)
        assert _tree(home) == before, cfg
        assert not snap.exists(), cfg
        assert "hooks = true" in res.all, res.all


def test_install_stops_when_config_toml_missing():
    src = _build_global()
    root, home = _home()
    (home / "config.toml").unlink()
    before = _tree(home)
    res = _inst(home, root / "snap", src)
    assert res.rc == 1, res.all
    assert _tree(home) == before and not (root / "snap").exists()


def test_install_accepts_hooks_true_with_other_keys_and_crlf():
    src = _build_global()
    root, home = _home(config="model = 'x'\r\n[features]\r\nfoo = 1\r\nhooks = true\r\n[tui]\r\nx = 1\r\n")
    cfg_before = (home / "config.toml").read_bytes()
    res = _inst(home, root / "snap", src)
    assert res.rc == 0, res.all
    assert (home / "config.toml").read_bytes() == cfg_before


def test_install_stops_when_build_output_incomplete():
    src = _build_global()
    for missing in ("AGENTS.md", "agents", "skills", "prompts", "hooks", "hooks.json"):
        broken = h.fresh("ib") / "codex"
        shutil.copytree(src, broken)
        t = broken / missing
        shutil.rmtree(t) if t.is_dir() else t.unlink()
        root, home = _home()
        before = _tree(home)
        res = _inst(home, root / "snap", broken)
        assert res.rc == 1, (missing, res.all)
        assert _tree(home) == before, missing
        assert not (root / "snap").exists(), missing


def test_install_source_folder_missing_stops():
    root, home = _home()
    before = _tree(home)
    res = _inst(home, root / "snap", root / "does-not-exist")
    assert res.rc == 1, res.all
    assert _tree(home) == before and not (root / "snap").exists()


def test_install_failure_half_way_prints_restore_path_and_exits_1():
    src = _build_global()
    root, home = _home()
    shutil.rmtree(home / "prompts")
    _w(home / "prompts", "a file where a folder is needed\n")  # mkdir of prompts fails after AGENTS.md was overwritten
    snap = root / "snap"
    cfg_before = (home / "config.toml").read_bytes()
    res = _inst(home, snap, src)
    assert res.rc == 1, res.all
    assert str(snap / "restore.ps1") in res.all, res.all
    assert (snap / "restore.ps1").is_file()
    assert (snap / "AGENTS.md").read_bytes() == b"old agents md\n", "snapshot taken before any change"
    assert (home / "config.toml").read_bytes() == cfg_before


def test_restore_script_covers_every_snapshotted_item():
    src = _build_global()
    root, home = _home()
    snap = root / "snap"
    assert _inst(home, snap, src).rc == 0
    rs = (snap / "restore.ps1").read_text(encoding="utf-8")
    for it in ("AGENTS.md", "hooks.json", "agents", "skills", "prompts", "hooks"):
        assert it in rs, it
    assert "config.toml" not in rs  # by design the install never edits it and the restore never rolls it back
    assert str(home) in rs and str(snap) in rs


def test_install_never_writes_trust_hash_files():
    src = _build_global()
    root, home = _home()
    before_names = {f.relative_to(home).as_posix() for f in home.rglob("*")}
    assert _inst(home, root / "snap", src).rc == 0
    added = {f.relative_to(home).as_posix() for f in home.rglob("*")} - before_names
    for a in added:
        low = a.lower()
        assert "trust" not in low and "hash" not in low and not low.endswith(".sha256"), a
    assert (home / "config.toml").read_bytes() == b"[features]\nhooks = true\n"


# ======================= unit-level: matcher, hook projection, extras, installer internals, shim =======================
import contextlib
import io


def _load(name, path):
    s = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(s)
    sys.modules[name] = m
    s.loader.exec_module(m)
    return m


B = _load("hv2_build", h.SRC / "tools" / "build.py")
IC = _load("hv2_install_codex", h.SRC / "tools" / "install_codex.py")
SHIM = h.SRC / "harness" / "hooks" / "codex_spawn_shim.py"
SHIMMOD = _load("hv2_shim", SHIM)


def _quiet(fn, *a):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rv = fn(*a)
    return rv, buf.getvalue()


def test_map_matcher_star_and_empty_keep_all_tools():
    for m in ("*", ""):
        w = []
        assert B.map_matcher("PreToolUse", m, w) == (True, ""), m
        assert w == []


def test_map_matcher_known_tools_map_to_codex_names():
    for src, want in (("(Edit|Write)", "apply_patch"), ("^Bash$", "Bash"), ("Edit | Write", "apply_patch")):
        w = []
        assert B.map_matcher("PreToolUse", src, w) == (True, want), src
        assert w == [], src


def test_map_matcher_unknown_tool_mixed_warns_and_unmapped_only_is_dropped():
    w = []
    assert B.map_matcher("PreToolUse", "Edit|mcp__x", w) == (True, "apply_patch")
    assert len(w) == 1 and "mcp__x" in w[0]
    w = []
    keep, _ = B.map_matcher("PreToolUse", "AskUserQuestion", w)
    assert keep is False and len(w) == 1 and "AskUserQuestion" in w[0]


def test_map_matcher_non_tool_events_keep_matcher_unchanged():
    for ev, m in (("SessionStart", "startup|resume"), ("Stop", "Edit|Write"), ("UserPromptSubmit", "AskUserQuestion")):
        w = []
        assert B.map_matcher(ev, m, w) == (True, m), ev
        assert w == []


def _ph(settings_hooks):
    p = h.fresh("ph") / "pj"
    _settings(p, settings_hooks)
    root = str(p).replace("\\", "/")
    return p, root, B.project_codex_hooks(p, h.fresh("chome"))


def test_project_hooks_all_three_variable_forms_replaced_in_command_and_args():
    p = h.fresh("ph") / "pj"
    _settings(p, {"Stop": [{"hooks": [
        {"type": "command", "command": "%CLAUDE_PROJECT_DIR%/bin/py.exe",
         "args": ["${CLAUDE_PROJECT_DIR}/a.py", "$CLAUDE_PROJECT_DIR/b.py", "%CLAUDE_PROJECT_DIR%/c.py"]}]}]})
    root = str(p).replace("\\", "/")
    cfg = B.project_codex_hooks(p, h.fresh("chome"))
    line = cfg["Stop"][0]["hooks"][0]["command"]
    assert "CLAUDE_PROJECT_DIR" not in line, line
    assert line.endswith(f"--timeout=30 {root} {root}/bin/py.exe {root}/a.py {root}/b.py {root}/c.py"), line


def test_project_hooks_shell_form_command_is_split_into_interpreter_and_script():
    p = h.fresh("ph") / "pj"
    _settings(p, {"Stop": [{"hooks": [{"type": "command",
                                       "command": 'C:/py/python.exe "${CLAUDE_PROJECT_DIR}/s.py" --a'}]}]})
    root = str(p).replace("\\", "/")
    line = B.project_codex_hooks(p, h.fresh("chome"))["Stop"][0]["hooks"][0]["command"]
    assert line.endswith(f"{root} C:/py/python.exe {root}/s.py --a"), line


def test_project_hooks_timeout_seven_gives_flag_seven_and_codex_timeout_seventeen():
    p = h.fresh("ph") / "pj"
    _settings(p, {"Stop": [{"hooks": [{"type": "command", "command": "py.exe", "args": ["s.py"], "timeout": 7}]}]})
    e = B.project_codex_hooks(p, h.fresh("chome"))["Stop"][0]["hooks"][0]
    assert "--timeout=7 " in e["command"] and e["timeout"] == 17, e


def test_project_hooks_non_numeric_timeout_raises_value_error():
    p = h.fresh("ph") / "pj"
    _settings(p, {"Stop": [{"hooks": [{"type": "command", "command": "py.exe", "args": ["s.py"], "timeout": "abc"}]}]})
    try:
        B.project_codex_hooks(p, h.fresh("chome"))
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_extras_rerun_with_same_backup_keeps_original_hooks_json_before():
    p = h.fresh("ex") / "pj"
    keep = h.fresh("ex") / "keep"
    _settings(p, {"Stop": [{"hooks": [{"type": "command", "command": "py.exe", "args": ["s.py"]}]}]})
    _w(p / ".codex" / "hooks.json", '{"original": 1}\n')
    ch = h.fresh("chome")
    assert _quiet(B.codex_project_extras, p, keep, ch)[0] == 0
    assert (keep / "hooks.json.before").read_bytes() == b'{"original": 1}\n'
    assert "original" not in (p / ".codex" / "hooks.json").read_text(encoding="utf-8")
    assert _quiet(B.codex_project_extras, p, keep, ch)[0] == 0
    assert (keep / "hooks.json.before").read_bytes() == b'{"original": 1}\n', "re-run must not overwrite the first backup"


def test_extras_invalid_settings_json_returns_1_and_changes_nothing():
    p = h.fresh("ex") / "pj"
    keep = h.fresh("ex") / "keep"
    _w(p / ".claude" / "settings.json", "{not json")
    _skill(p, "sk-a", {"SKILL.md": "# A\n"})
    _w(p / ".codex" / "hooks.json", '{"original": 1}\n')
    before = _tree(p, keep)
    rv, _ = _quiet(B.codex_project_extras, p, keep, h.fresh("chome"))
    assert rv == 1
    assert _tree(p, keep) == before
    assert not (p / ".agents").exists() and not keep.exists()


def _mini_source():
    s = h.fresh("ms") / "codex"
    _w(s / "AGENTS.md", "new agents\n")
    _w(s / "agents" / "a.toml", "name = 'a'\n")
    _w(s / "skills" / "s" / "SKILL.md", "# s\n")
    _w(s / "prompts" / "p.md", "p\n")
    _w(s / "hooks" / "x.py", "print(1)\n")
    _w(s / "hooks.json", json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "py x.py"}]}]}}))
    return s


def test_merge_hooks_bad_shapes_raise_value_error():
    good = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "py x.py"}]}]}}
    bad_olds = [[], {"hooks": []}, {"hooks": {"Stop": "x"}}, {"hooks": {"Stop": ["x"]}},
                {"hooks": {"Stop": [{"hooks": "x"}]}}, {"hooks": {"Stop": [{"hooks": ["x"]}]}}]
    for old in bad_olds:
        try:
            IC.merge_hooks(old, good)
        except ValueError:
            continue
        raise AssertionError(f"old shape accepted: {old!r}")
    for new in ({"description": "no hooks key"}, {"hooks": {"Stop": "x"}}, {"hooks": []}):
        try:
            IC.merge_hooks({"hooks": {}}, new)
        except ValueError:
            continue
        raise AssertionError(f"new shape accepted: {new!r}")


def test_install_bad_existing_hooks_json_fails_before_any_write():
    src = _mini_source()
    root, home = _home(hooks_json=False)
    _w(home / "hooks.json", json.dumps({"hooks": {"Stop": "not a list"}}))
    snap = root / "snap"
    before = _tree(home)
    rv, out = _quiet(IC.install, home, snap, src)
    assert rv == 1, out
    assert _tree(home) == before
    assert not snap.exists()


def test_install_existing_snapshot_folder_refused_in_process():
    src = _mini_source()
    root, home = _home()
    snap = root / "snap"
    snap.mkdir()
    before = _tree(home, snap)
    rv, out = _quiet(IC.install, home, snap, src)
    assert rv == 1, out
    assert _tree(home, snap) == before


def test_restore_script_doubles_apostrophe_and_does_not_restore_config_toml():
    rs = IC.restore_script(Path(r"C:\o'brien\home"), Path(r"C:\snap'x"))
    assert "$home_ = 'C:\\o''brien\\home'" in rs, rs
    assert "$snap = 'C:\\snap''x'" in rs, rs
    assert "$item = 'config.toml'" not in rs
    for it in ("AGENTS.md", "hooks.json", "agents", "skills", "prompts", "hooks"):
        assert f"$item = '{it}'" in rs, it
    # the brief says the script text does not mention config.toml at all
    assert "config.toml" not in rs, "restore script text mentions config.toml"


def test_install_writes_restore_script_with_utf8_bom():
    src = _mini_source()
    root, home = _home()
    snap = root / "snap"
    rv, out = _quiet(IC.install, home, snap, src)
    assert rv == 0, out
    assert (snap / "restore.ps1").read_bytes()[:3] == b"\xef\xbb\xbf"


def test_shim_apply_patch_becomes_edit_with_first_file_and_all_files():
    patch = "*** Begin Patch\n*** Update File: a.py\n@@\n-x\n+y\n*** Add File: b.py\n+z\n*** End Patch\n"
    out = SHIMMOD.to_claude({"tool_name": "apply_patch", "tool_input": {"command": patch}})
    assert out["tool_name"] == "Edit"
    assert out["tool_input"]["file_path"] == "a.py"
    assert out["tool_input"]["files"] == ["a.py", "b.py"]
    assert out["tool_input"]["patch"] == patch


def test_shim_with_fewer_than_three_args_exits_2():
    for args in ([], ["root"], ["root", "interp"], ["--timeout=5", "root", "interp"]):
        r = h.run([PYX, "-I", str(SHIM), *args], h.SRC)
        assert r.rc == 2, (args, r.rc, r.all)
        assert "usage" in r.err


if __name__ == "__main__":
    sys.exit(h.run_all(globals()))
