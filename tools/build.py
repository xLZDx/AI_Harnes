"""Build the harness for Claude Code and Codex from one source (harness/).

  python tools/build.py                      -> out/claude/ and out/codex/ (nothing installed)
  python tools/build.py --codex-project D --backup B
                                             -> D/.codex/agents/*.toml from D/.claude/agents/*.md;
                                                replaced and stale TOML files go to B/<project>/
  python tools/build.py --install-project D  -> copy tools + routing into project D/.harness-tools,
                                                set git core.hooksPath, create .harness/ledger.json
Installing into ~/.claude or ~/.codex is a separate operator decision and is not done here.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "harness"
OUT = ROOT / "out"


def frontmatter(text: str) -> dict:
    m = re.match(r"---\s*\r?\n(.*?)\r?\n---", text.lstrip("﻿"), re.S)
    if not m:
        return {}
    d, key = {}, None
    for line in m.group(1).splitlines():
        if line.lstrip().startswith("- "):
            if key:
                d[key] = (d[key] + "," if d[key] else "") + line.strip()[2:]
        elif re.match(r"^[A-Za-z_][\w-]*:", line):
            key, val = line.split(":", 1)
            key = key.strip()
            d[key] = val.strip()
    return d


CODEX_MODEL = "gpt-6-luna"
CODEX_EFFORT = "medium"
NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
WIN_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def split_agent(text: str) -> tuple[dict, str] | None:
    """(frontmatter, body) of an agent file, or None when it has no frontmatter block.
    Raises ValueError on frontmatter this tool cannot read exactly (multi-line values)."""
    text = text.lstrip("﻿").replace("\r\n", "\n")
    m = re.match(r"---[ \t]*\n(.*?)\n---[ \t]*(\n|$)", text, re.S)
    if not m:
        return None
    key = None
    for ln in m.group(1).split("\n"):
        top = re.match(r"([A-Za-z_][\w-]*):", ln)
        if top:
            key = top.group(1)
        elif ln.strip() and not ln.lstrip().startswith("#"):
            # nested lines (e.g. a hooks: map) are fine under keys this tool does not read
            if key in ("name", "description") or (key == "skills" and not re.match(r"\s*- ", ln)):
                raise ValueError(f"{key} must be a one-line value (continuation line {ln!r})")
            if key is None:
                raise ValueError(f"unsupported frontmatter line {ln!r}")
    return frontmatter(text), text[m.end():].strip("\n")


def unquote(v: str) -> str:
    if v[:1] == '"':
        if len(v) < 2 or v[-1] != '"':
            raise ValueError(f"unterminated quoted value {v!r}")
        try:
            return json.loads(v)
        except ValueError:
            raise ValueError(f"cannot decode quoted value {v!r}") from None
    if v[:1] == "'":
        if len(v) < 2 or v[-1] != "'":
            raise ValueError(f"unterminated quoted value {v!r}")
        return v[1:-1].replace("''", "'")
    return v


def toml_str(s: str) -> str:
    out = []
    for ch in s:
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif o < 0x20 or o == 0x7F:
            out.append(f"\\u{o:04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def codex_agent(path: Path, label: str) -> tuple[str, str]:
    """(name, TOML text) for one Claude agent file; raises ValueError on anything it cannot convert exactly."""
    with open(path, encoding="utf-8", newline="") as fh:  # keep a lone CR as data; only CRLF becomes LF
        raw = fh.read()
    try:
        parts = split_agent(raw)
        if parts is None:
            raise ValueError("no frontmatter")
        fm, body = parts
        name = unquote(fm.get("name", "")) or path.name[: -len(".md")]
        desc = unquote(fm.get("description", ""))
    except ValueError as e:
        raise ValueError(f"{path}: {e}") from None
    if not NAME_RE.fullmatch(name) or name.strip(".") == "" or name.split(".")[0].lower() in WIN_RESERVED:
        raise ValueError(f"{path}: bad agent name {name!r} (allowed: A-Z a-z 0-9 _ . -, not a Windows device name)")
    if not desc or re.fullmatch(r"[>|][+-]?", desc):
        raise ValueError(f"{path}: description must be a one-line value (got {desc!r})")
    skills = [s.strip() for s in fm.get("skills", "").strip("[]").split(",") if s.strip()]
    pre = f"Generated from `{label}` by Harness v2 tools/build.py; edit the source, not this file.\n"
    if skills:
        pre += f"Skills: {', '.join(skills)}. Follow them when the task matches.\n"
    fields = [("name", name), ("model", CODEX_MODEL), ("model_reasoning_effort", CODEX_EFFORT),
              ("description", desc), ("developer_instructions", pre + "\n" + body + "\n")]
    text = "".join(f"{k} = {toml_str(v)}\n" for k, v in fields)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as e:
        raise ValueError(f"{path}: text cannot be written as UTF-8 ({e.reason})") from None
    return name, text


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def codex_project(target: Path, backup: Path) -> int:
    src, dst = target / ".claude" / "agents", target / ".codex" / "agents"
    if not src.is_dir():
        print(f"codex-project stopped: {src} does not exist")
        return 1
    made, errs = {}, []
    for p in sorted(src.glob("*.md")):
        try:
            name, text = codex_agent(p, f".claude/agents/{p.name}")
        except ValueError as e:
            errs.append(str(e))
            continue
        if name.casefold() in made:  # Windows file names ignore case
            errs.append(f"{p}: duplicate agent name {name!r}")
        made[name.casefold()] = (name, text)
    if not made and not errs:
        errs.append(f"{src}: no agent files")
    if errs:
        print("codex-project stopped, nothing written:\n  " + "\n  ".join(errs))
        return 1
    keep = backup / target.name
    old = sorted(dst.glob("*.toml")) if dst.is_dir() else []
    if old:
        if keep.exists() and not keep.is_dir():
            print(f"codex-project stopped: backup path {keep} is a file")
            return 1
        clash = [f.name for f in old if (keep / f.name).exists()]
        if clash:
            print(f"codex-project stopped: backup {keep} already holds {', '.join(clash)}; use a new --backup folder")
            return 1
    done = []
    try:
        if old:
            keep.mkdir(parents=True, exist_ok=True)
        for f in old:  # 1. copy every old file to the backup; nothing in the project changes yet
            shutil.copy2(f, keep / f.name)
            done.append(f"backed up {f.name}")
        dst.mkdir(parents=True, exist_ok=True)
        for cf, (name, text) in made.items():  # 2. write new files, each replaced atomically
            stale_case = [f for f in old if f.stem.casefold() == cf and f.stem != name]
            for f in stale_case:  # same agent, other letter case: drop the old spelling
                f.unlink()
            _write_atomic(dst / f"{name}.toml", text)
            done.append(f"wrote {name}.toml")
        stale = [f for f in old if f.stem.casefold() not in made]
        for f in stale:  # 3. stale files already have a backup copy; remove them from the project
            f.unlink()
            done.append(f"removed stale {f.name} (copy in backup)")
    except OSError as e:
        print(f"codex-project FAILED half-way: {e}\n  every original file is in {keep}; done before the failure:\n  "
              + "\n  ".join(done or ["nothing"]) + "\n  re-run with a new --backup folder after fixing the cause")
        return 1
    print(f"codex-project {target.name}: wrote {len(made)} agents, removed {len(stale)} stale, "
          f"backed up {len(old)} old files to {keep}")
    return codex_project_extras(target, keep)


def _unique(path: Path) -> Path:
    """path, or path.2, path.3 ... when it exists: a backup is never overwritten and nothing nests."""
    if not path.exists():
        return path
    i = 2
    while Path(f"{path}.{i}").exists():
        i += 1
    return Path(f"{path}.{i}")


def _same_tree(a: Path, b: Path) -> bool:
    fa = {f.relative_to(a).as_posix().lower(): f for f in a.rglob("*") if f.is_file()}
    fb = {f.relative_to(b).as_posix().lower(): f for f in b.rglob("*") if f.is_file()}
    return fa.keys() == fb.keys() and all(fa[k].read_bytes() == fb[k].read_bytes() for k in fa)


def sync_skills(target: Path, keep: Path) -> str:
    """D/.claude/skills/<n> -> D/.agents/skills/<n> (Codex reads project skills from .agents/skills).
    A different existing copy and a Codex-only skill go to keep/skills/<n> (a fresh name when it exists)."""
    src, dst = target / ".claude" / "skills", target / ".agents" / "skills"
    if not src.is_dir():
        return "no skills"
    names = sorted(p.name for p in src.iterdir() if p.is_dir() and (p / "SKILL.md").is_file())
    low = {n.lower() for n in names}
    stash = keep / "skills"
    changed = moved = 0
    for n in names:
        a, b = src / n, dst / n
        if b.is_dir():
            if _same_tree(a, b):
                continue
            stash.mkdir(parents=True, exist_ok=True)
            shutil.copytree(b, _unique(stash / n))
            shutil.rmtree(b, onexc=_rm_readonly)
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copytree(a, b)
        changed += 1
    if dst.is_dir():
        for p in sorted(dst.iterdir()):
            if p.is_dir() and not p.name.startswith(".") and p.name.lower() not in low:
                stash.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), str(_unique(stash / p.name)))
                moved += 1
    return f"{len(names)} skills, {changed} written, {moved} stale moved to {stash}"


# Claude tool name -> Codex tool name, for tool events only (PreToolUse / PostToolUse / PermissionRequest)
MATCHER_MAP = {"Agent": "spawn_agent", "Task": "spawn_agent", "Bash": "Bash", "PowerShell": "Bash",
               "Edit": "apply_patch", "Write": "apply_patch", "MultiEdit": "apply_patch", "NotebookEdit": "apply_patch"}
TOOL_EVENTS = {"PreToolUse", "PostToolUse", "PermissionRequest"}
PROJECT_VAR = re.compile(r"\$\{CLAUDE_PROJECT_DIR\}|\$CLAUDE_PROJECT_DIR\b|%CLAUDE_PROJECT_DIR%")
GENERATED_MARK = "generated by Harness v2"


def map_matcher(event: str, matcher: str, warn: list[str]) -> tuple[bool, str]:
    """(keep, codex matcher). Tool events: known tools are mapped, '*' means all tools; a group with a tool Codex
    lacks keeps the mapped tools (and a warning); a group with none mapped is dropped (and a warning).
    Other events keep their matcher as it is."""
    if event not in TOOL_EVENTS:
        return True, matcher
    m = matcher.strip()
    if m in ("", "*", ".*"):
        return True, ""
    toks = [t for t in re.split(r"[|()^$\s]+", m) if t]
    mapped = [MATCHER_MAP[t] for t in toks if t in MATCHER_MAP]
    lost = [t for t in toks if t not in MATCHER_MAP]
    if not mapped:
        warn.append(f"dropped {event} group with matcher {matcher!r}: Codex has none of these tools")
        return False, ""
    if lost:
        warn.append(f"{event} matcher {matcher!r} narrowed to {'|'.join(dict.fromkeys(mapped))}: Codex lacks {lost}")
    return True, "|".join(dict.fromkeys(mapped))


def project_codex_hooks(target: Path, codex_home: Path, warn: list[str] | None = None) -> dict | None:
    """Codex hooks for the project's own Claude hooks (.claude/settings.json), each run through
    codex_spawn_shim.py so the unchanged project script sees a Claude-shaped payload. None = nothing to write.
    Raises ValueError on a settings file this tool cannot convert exactly (nothing is written then)."""
    warn = warn if warn is not None else []
    sp = target / ".claude" / "settings.json"
    if not sp.is_file():
        return None
    try:
        hooks = json.loads(sp.read_text(encoding="utf-8-sig")).get("hooks") or {}
    except (ValueError, AttributeError) as e:
        raise ValueError(f"{sp}: cannot read hooks ({e})") from None
    if not isinstance(hooks, dict):
        raise ValueError(f"{sp}: 'hooks' is not an object")
    root = posix(target)
    shim = posix(codex_home / "hooks" / "codex_spawn_shim.py")
    out: dict[str, list] = {}
    for event, groups in hooks.items():
        if event not in HOOK_EVENTS["codex"]:
            warn.append(f"dropped {event} hooks: Codex has no such event")
            continue
        if not isinstance(groups, list):
            raise ValueError(f"{sp}: hooks.{event} is not a list")
        for g in groups:
            if not isinstance(g, dict):
                raise ValueError(f"{sp}: a hooks.{event} group is not an object")
            keep, m = map_matcher(event, str(g.get("matcher") or ""), warn)
            if not keep:
                continue
            entries = []
            for h in g.get("hooks") or []:
                if not isinstance(h, dict) or h.get("type") != "command" or not h.get("command"):
                    continue
                cmd0 = PROJECT_VAR.sub(root, str(h["command"]))
                args = [PROJECT_VAR.sub(root, str(a)) for a in h.get("args") or []]
                if not args:  # shell form `python "script" arg`: split it so the shim gets interpreter and script
                    import shlex
                    toks = [t.strip('"') for t in shlex.split(cmd0, posix=False)]
                    cmd0, args = toks[0], toks[1:]
                    if not args:
                        raise ValueError(f"{sp}: hook {h['command']!r} has no script argument")
                try:
                    t = int(float(h.get("timeout") or 30))
                except (TypeError, ValueError):
                    raise ValueError(f"{sp}: hook timeout {h.get('timeout')!r} is not a number") from None
                line = cmdline([posix(sys.executable), "-s", "-X", "utf8", shim, f"--timeout={t}", root, cmd0, *args])
                entries.append({"type": "command", "command": line, "commandWindows": line, "timeout": t + 10,
                                "statusMessage": h.get("statusMessage") or "project hook"})
            if entries:
                out.setdefault(event, []).append({**({"matcher": m} if m else {}), "hooks": entries})
    return out or None


def codex_project_extras(target: Path, keep: Path, codex_home: Path | None = None) -> int:
    codex_home = codex_home or Path.home() / ".codex"
    warn: list[str] = []
    hp = target / ".codex" / "hooks.json"
    try:
        cfg = project_codex_hooks(target, codex_home, warn)  # first: a bad settings file changes nothing
        msg = sync_skills(target, keep)
        if cfg:
            hp.parent.mkdir(parents=True, exist_ok=True)
            if hp.is_file():
                keep.mkdir(parents=True, exist_ok=True)
                before = keep / "hooks.json.before"
                if not before.exists():  # the first backup is the original; a re-run never overwrites it
                    shutil.copy2(hp, before)
            _write_atomic(hp, json.dumps({"description": f"Project hooks, {GENERATED_MARK} tools/build.py from "
                                          ".claude/settings.json", "hooks": cfg}, indent=1, ensure_ascii=False) + "\n")
            hmsg = f"hooks.json with {sum(len(g['hooks']) for v in cfg.values() for g in v)} hook(s)"
            if not (codex_home / "hooks" / "codex_spawn_shim.py").is_file():
                warn.append(f"{codex_home / 'hooks' / 'codex_spawn_shim.py'} is missing: run tools/install_codex.py "
                            "or every guarded call of this project fails")
        else:
            hmsg = "no project hooks"
            if hp.is_file() and GENERATED_MARK in hp.read_text(encoding="utf-8", errors="replace")[:300]:
                keep.mkdir(parents=True, exist_ok=True)  # a generated file for hooks the project no longer has
                shutil.move(str(hp), str(_unique(keep / "hooks.json.removed")))
                hmsg += " (stale generated hooks.json moved to the backup)"
    except (OSError, ValueError, AttributeError, TypeError) as e:
        print(f"codex-project {target.name}: agents done, skills/hooks step FAILED: {e}")
        return 1
    for w in warn:
        print(f"codex-project {target.name}: WARNING {w}")
    print(f"codex-project {target.name}: {msg}; {hmsg}")
    return 0


def _rm_readonly(func, path, _exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def check(manifest: dict) -> list[str]:
    errs = []
    skills = {s["name"] for s in manifest["skills"]}
    for a in manifest["agents"]:
        p = SRC / "agents" / f"{a['name']}.md"
        if not p.exists():
            errs.append(f"missing agent {p.name}")
            continue
        fm = frontmatter(p.read_text(encoding="utf-8"))
        if fm.get("model") != "sonnet" or fm.get("effort") != "medium":
            errs.append(f"{p.name}: model/effort must be sonnet/medium (got {fm.get('model')}/{fm.get('effort')})")
        for s in filter(None, (x.strip() for x in fm.get("skills", "").split(","))):
            if s not in skills:
                errs.append(f"{p.name}: unknown skill {s}")
        try:
            cname, _ = codex_agent(p, f"harness/agents/{p.name}")
            if cname != a["name"]:
                errs.append(f"{p.name}: frontmatter name {cname!r} differs from manifest name {a['name']!r}")
        except ValueError as e:
            errs.append(f"codex: {e}")
    for s in manifest["skills"]:
        if not (SRC / "skills" / s["name"] / "SKILL.md").exists():
            errs.append(f"missing skill {s['name']}")
    extra = {p.stem for p in (SRC / "agents").glob("*.md")} - {a["name"] for a in manifest["agents"]}
    errs += [f"agent not in manifest: {x}" for x in sorted(extra)]
    extra = {p.name for p in (SRC / "skills").iterdir() if p.is_dir()} - skills
    errs += [f"skill not in manifest: {x}" for x in sorted(extra)]
    return errs


HOOK_EVENTS = {
    "claude": {"PreToolUse", "PostToolUse", "PreCompact", "Stop", "SubagentStop", "UserPromptSubmit",
               "PermissionRequest", "Notification", "SessionStart", "SessionEnd"},
    "codex": {"PreToolUse", "PostToolUse", "PreCompact", "Stop", "SubagentStop", "UserPromptSubmit",
              "PermissionRequest", "SessionStart", "SessionEnd", "SubagentStart"},
}


def check_hooks() -> list[str]:
    errs = []
    reg = json.loads((SRC / "hooks" / "registry.json").read_text(encoding="utf-8"))
    for i, h in enumerate(reg["hooks"]):
        if not (SRC / "hooks" / h["script"]).exists():
            errs.append(f"hook {i}: missing script {h['script']}")
        for cl in ("claude", "codex"):
            if cl in h and h["event"] not in HOOK_EVENTS[cl]:
                errs.append(f"hook {i}: {cl} has no {h['event']} event")
        if "claude" not in h and "codex" not in h:
            errs.append(f"hook {i}: {h['script']} is installed for no client")
    names = {a["name"] for a in json.loads((SRC / "manifest.json").read_text(encoding="utf-8"))["agents"]}
    errs += [f"subagent_evidence_skip: unknown agent {n}" for n in reg.get("subagent_evidence_skip", []) if n not in names]
    return errs


def cmdline(parts: list[str]) -> str:
    """One command line for Codex (it may run it through PowerShell or cmd). Mirrors the operator's working hook:
    forward slashes, the program first and unquoted when its path has no space, every other part double-quoted when
    it has a space or a shell metacharacter, embedded double quotes escaped with a backslash."""
    out = []
    for i, p in enumerate(parts):
        p = str(p)
        if i == 0 and not re.search(r'[\s"&|;<>^()$`]', p):
            out.append(p)
        elif p == "" or re.search(r'[\s"&|;<>^()$`%!]', p):
            out.append('"' + p.replace('"', '\\"') + '"')
        else:
            out.append(p)
    return " ".join(out)


def posix(p) -> str:
    return str(p).replace("\\", "/")


def hook_config(client: str, hooks_dir: Path, python: str) -> dict:
    """Hooks section for one client, grouped by event and matcher in registry order."""
    reg = json.loads((SRC / "hooks" / "registry.json").read_text(encoding="utf-8"))
    out: dict[str, list] = {}
    for h in reg["hooks"]:
        if client not in h:
            continue
        script = str(hooks_dir / h["script"])
        # -X utf8: hook stdin/stdout are UTF-8 JSON whatever the Windows code page; --harness-client tells
        # _agentio which client runs the hook, so it never has to guess
        runner = str(hooks_dir / "run_hook.py")  # S7-T7: runs the hook in-process and records one metrics row
        args = ["-s", "-X", "utf8", runner, h["script"], f"--harness-client={client}"]
        if client == "claude":
            entry = {"type": "command", "command": python, "args": args,
                     "timeout": h["timeout"], "statusMessage": h["status"]}
        else:
            cmd = cmdline([posix(python), "-s", "-X", "utf8", posix(runner), h["script"], f"--harness-client={client}"])
            entry = {"type": "command", "command": cmd, "commandWindows": cmd,
                     "timeout": h["timeout"], "statusMessage": h["status"]}
        groups = out.setdefault(h["event"], [])
        matcher = h[client]
        if groups and groups[-1].get("matcher", "") == matcher:
            groups[-1]["hooks"].append(entry)
        else:
            groups.append({**({"matcher": matcher} if matcher else {}), "hooks": [entry]})
    return out


def evidence_skip() -> list[str]:
    """Agents whose reports the SubagentStop evidence hooks skip (registry "subagent_evidence_skip")."""
    reg = json.loads((SRC / "hooks" / "registry.json").read_text(encoding="utf-8"))
    return sorted(reg.get("subagent_evidence_skip", []))


def emit_hooks(manifest: dict, client_dir: Path, client: str, home_dir: Path, python: str) -> None:
    hooks_out = client_dir / "hooks"
    hooks_out.mkdir(parents=True, exist_ok=True)
    for f in sorted((SRC / "hooks").glob("*.py")):
        shutil.copy2(f, hooks_out / f.name)
    (hooks_out / "codex_policy.json").write_text(json.dumps({"model": CODEX_MODEL, "effort": CODEX_EFFORT}) + "\n",
                                                 encoding="utf-8", newline="\n")
    (hooks_out / "evidence_skip.json").write_text(json.dumps(evidence_skip(), indent=1) + "\n", encoding="utf-8",
                                                  newline="\n")
    cfg = hook_config(client, home_dir / "hooks", python)
    name = "settings.hooks.json" if client == "claude" else "hooks.json"
    body = {"hooks": cfg} if client == "claude" else {"description": "Harness v2 hooks, built by tools/build.py", "hooks": cfg}
    (client_dir / name).write_text(json.dumps(body, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def copytree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst, onexc=_rm_readonly)
    shutil.copytree(src, dst)


def build(python: str = "") -> int:
    python = python or sys.executable
    manifest = json.loads((SRC / "manifest.json").read_text(encoding="utf-8"))
    errs = check(manifest) + check_hooks()
    if errs:
        print("build stopped:\n  " + "\n  ".join(errs))
        return 1
    core = (SRC / "rules" / "CORE.md").read_text(encoding="utf-8")
    roster = "\n".join(f"- `{a['name']}` - {a['trigger']}" for a in manifest["agents"])
    # Claude Code layout (mirrors ~/.claude)
    c = OUT / "claude"
    if c.exists():
        shutil.rmtree(c, onexc=_rm_readonly)
    (c).mkdir(parents=True)
    (c / "CLAUDE.md").write_text(core, encoding="utf-8", newline="\n")
    copytree(SRC / "agents", c / "agents")
    copytree(SRC / "skills", c / "skills")
    copytree(SRC / "commands", c / "commands")
    # Codex layout (mirrors ~/.codex): agents are Codex TOML files built from the same sources
    x = OUT / "codex"
    if x.exists():
        shutil.rmtree(x, onexc=_rm_readonly)
    x.mkdir(parents=True)
    agents_md = core + "\n## Agent roster (Codex agents in `agents/*.toml`)\n" + roster + "\n"
    (x / "AGENTS.md").write_text(agents_md, encoding="utf-8", newline="\n")
    (x / "agents").mkdir()
    for a in manifest["agents"]:
        name, text = codex_agent(SRC / "agents" / f"{a['name']}.md", f"harness/agents/{a['name']}.md")
        (x / "agents" / f"{name}.toml").write_text(text, encoding="utf-8", newline="\n")
    copytree(SRC / "skills", x / "skills")
    copytree(SRC / "commands", x / "prompts")
    home = Path.home()
    emit_hooks(manifest, c, "claude", home / ".claude", python)
    emit_hooks(manifest, x, "codex", home / ".codex", python)
    n_a, n_s = len(manifest["agents"]), len(manifest["skills"])
    print(f"built out/claude and out/codex: {n_a} agents, {n_s} skills, {len(list((SRC / 'commands').glob('*.md')))} commands")
    return 0


TOOL_FILES = ("ledger.py", "route.py", "report.py", "worktree_gc.py", "plan_ledger.py")
REPORT_ASSETS = ("report.css", "filter.js", "copy.js")  # harness/report/* -> <project>/.harness-tools/report/
HOOKS = ("pre-commit", "post-commit", "pre-push", "post-merge")


def _git(target: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(target), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")


def install_project(target: Path, force: bool = False) -> int:
    r = _git(target, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if r.returncode != 0:
        print(f"{target} is not a git repository: {r.stderr.strip()}")
        return 1
    common = Path(r.stdout.strip())
    main = common.parent if common.name == ".git" else common  # tools live in the main checkout
    missing = [p for p in [*(ROOT / "tools" / f for f in TOOL_FILES), *(ROOT / "tools" / "githooks" / h for h in HOOKS),
                           SRC / "routing.json", SRC / "manifest.json",
                           *(SRC / "report" / a for a in REPORT_ASSETS)] if not p.exists()]
    if missing:
        print("install stopped, sources missing: " + ", ".join(map(str, missing)))
        return 1
    hooks_dir = (main / ".harness-tools" / "githooks").as_posix()
    cur = _git(main, "config", "--get", "core.hooksPath").stdout.strip()
    if cur and cur != hooks_dir and not force:
        print(f"install stopped: core.hooksPath is already {cur!r}; re-run with --force to replace it")
        return 1
    if not cur and any((common / "hooks" / h).exists() for h in HOOKS) and not force:
        print(f"install stopped: {common / 'hooks'} has its own hooks; re-run with --force to switch to the harness hooks")
        return 1
    tools = main / ".harness-tools"
    (tools / "harness").mkdir(parents=True, exist_ok=True)
    for f in TOOL_FILES:
        shutil.copy2(ROOT / "tools" / f, tools / f)
    (tools / "report").mkdir(parents=True, exist_ok=True)
    for a in REPORT_ASSETS:
        shutil.copy2(SRC / "report" / a, tools / "report" / a)
    shutil.copy2(SRC / "routing.json", tools / "harness" / "routing.json")
    shutil.copy2(SRC / "manifest.json", tools / "harness" / "manifest.json")
    copytree(ROOT / "tools" / "githooks", tools / "githooks")
    for h in HOOKS:
        os.chmod(tools / "githooks" / h, 0o755)
    ledger_file = main / ".harness" / "ledger.json"
    if not ledger_file.exists() and not (main / "PROJECT_PLAN.md").is_file():  # plan-based project: the plan is the source
        ledger_file.parent.mkdir(parents=True, exist_ok=True)
        ledger_file.write_text(json.dumps({"version": 1, "project": main.name, "sprints": [], "approval": None, "events": []},
                                          indent=1) + "\n", encoding="utf-8", newline="\n")
    # absolute path: works from every worktree even though .harness-tools is not committed
    r = _git(main, "config", "core.hooksPath", hooks_dir)
    if r.returncode != 0:
        print(f"install failed: git config core.hooksPath: {r.stderr.strip()}")
        return 1
    got = _git(main, "config", "--get", "core.hooksPath").stdout.strip()
    absent = [h for h in HOOKS if not (Path(got) / h).exists()]
    if got != hooks_dir or absent:
        print(f"install failed: hooksPath={got!r}, missing hooks: {absent}")
        return 1
    print(f"installed into {main}: .harness-tools/ ({len(TOOL_FILES)} tools), hooksPath={got}, ledger {ledger_file}")
    print("add `.harness-tools/` and `.harness/` to the project's .gitignore if they should stay untracked")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--install-project", type=Path)
    ap.add_argument("--force", action="store_true", help="with --install-project: replace an existing hooks setup")
    ap.add_argument("--check", action="store_true", help="only validate the source")
    ap.add_argument("--codex-project", type=Path, help="write D/.codex/agents/*.toml from D/.claude/agents/*.md")
    ap.add_argument("--backup", type=Path, help="with --codex-project: folder for replaced and stale TOML files")
    ap.add_argument("--python", default="", help="interpreter written into the hook configs (default: this one)")
    a = ap.parse_args(argv)
    if sum(map(bool, (a.codex_project, a.install_project, a.check))) > 1:
        ap.error("--codex-project, --install-project and --check cannot be combined")
    if a.backup and not a.codex_project:
        ap.error("--backup is only used with --codex-project")
    if a.force and not a.install_project:
        ap.error("--force is only used with --install-project")
    if a.codex_project:
        if not a.backup:
            ap.error("--codex-project needs --backup")
        return codex_project(a.codex_project.resolve(), a.backup.resolve())
    if a.check:
        errs = check(json.loads((SRC / "manifest.json").read_text(encoding="utf-8"))) + check_hooks()
        print("\n".join(errs) or "source ok")
        return 1 if errs else 0
    if a.install_project:
        return install_project(a.install_project.resolve(), a.force)
    return build(a.python)


if __name__ == "__main__":
    sys.exit(main())
