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


def copytree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst, onexc=_rm_readonly)
    shutil.copytree(src, dst)


def build() -> int:
    manifest = json.loads((SRC / "manifest.json").read_text(encoding="utf-8"))
    errs = check(manifest)
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
    n_a, n_s = len(manifest["agents"]), len(manifest["skills"])
    print(f"built out/claude and out/codex: {n_a} agents, {n_s} skills, {len(list((SRC / 'commands').glob('*.md')))} commands")
    return 0


TOOL_FILES = ("ledger.py", "route.py", "report.py", "worktree_gc.py")
HOOKS = ("post-commit", "pre-push", "post-merge")


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
                           SRC / "routing.json", SRC / "manifest.json"] if not p.exists()]
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
    shutil.copy2(SRC / "routing.json", tools / "harness" / "routing.json")
    shutil.copy2(SRC / "manifest.json", tools / "harness" / "manifest.json")
    copytree(ROOT / "tools" / "githooks", tools / "githooks")
    for h in HOOKS:
        os.chmod(tools / "githooks" / h, 0o755)
    ledger_file = main / ".harness" / "ledger.json"
    if not ledger_file.exists():
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
        errs = check(json.loads((SRC / "manifest.json").read_text(encoding="utf-8")))
        print("\n".join(errs) or "source ok")
        return 1 if errs else 0
    if a.install_project:
        return install_project(a.install_project.resolve(), a.force)
    return build()


if __name__ == "__main__":
    sys.exit(main())
