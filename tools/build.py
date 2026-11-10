"""Build the harness for Claude Code and Codex from one source (harness/).

  python tools/build.py                      -> out/claude/ and out/codex/ (nothing installed)
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
    # Codex layout (mirrors ~/.codex): no subagent files, so the roster and role files ride along
    x = OUT / "codex"
    if x.exists():
        shutil.rmtree(x, onexc=_rm_readonly)
    x.mkdir(parents=True)
    agents_md = core + "\n## Agent roster (role files in `agents/`; load the file and act as that role)\n" + roster + "\n"
    (x / "AGENTS.md").write_text(agents_md, encoding="utf-8", newline="\n")
    copytree(SRC / "agents", x / "agents")
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
    a = ap.parse_args(argv)
    if a.check:
        errs = check(json.loads((SRC / "manifest.json").read_text(encoding="utf-8")))
        print("\n".join(errs) or "source ok")
        return 1 if errs else 0
    if a.install_project:
        return install_project(a.install_project.resolve(), a.force)
    return build()


if __name__ == "__main__":
    sys.exit(main())
