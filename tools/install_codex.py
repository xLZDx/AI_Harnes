"""Install the built Codex harness (out/codex) into a Codex home, with a full snapshot and a restore script.

  python tools/build.py --python <interpreter>                   # builds out/codex
  python tools/install_codex.py --home ~/.codex --snapshot <new folder>

What it does (nothing is deleted without a copy in the snapshot folder):
  AGENTS.md, config.toml, hooks.json, agents/, skills/, prompts/, hooks/  -> copied to <snapshot> first, with restore.ps1
  AGENTS.md, agents/*.toml, skills/<name>, prompts/*.md, hooks/*          -> overwritten from out/codex
  agents/*.toml that the harness does not ship                           -> moved to <snapshot>/moved-out/agents
  hooks.json                                                              -> merged: entries running a script the
      harness replaces are dropped, every other entry (for example a SessionEnd hook) is kept, harness entries added
It never edits config.toml and never writes a hook trust hash: Codex runs new or changed hooks only after the
operator approves them (`/hooks` in Codex). Before touching anything it checks: the snapshot folder does not exist,
out/codex is complete, hooks.json (old and new) has the expected shape and the merge works, and config.toml has
`hooks = true` under [features]. restore.ps1 does not restore config.toml (the install never changes it, and Codex
may have changed it since: trust state).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ITEMS = ("AGENTS.md", "config.toml", "hooks.json", "agents", "skills", "prompts", "hooks")
RESTORE_ITEMS = tuple(i for i in ITEMS if i != "config.toml")
SCRIPT_RE = re.compile(r"[\w.-]+\.py\b")


def _rm_readonly(func, path, _exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


_FAIL_AFTER = None  # test seam: raise OSError after the n-th write step (set from HARNESS_INSTALL_FAIL_AFTER in install())
_STEPS = 0


def _tick() -> None:
    """Count one write step (a copied file, then the written hooks.json.tmp); fail on the n-th if the seam is set."""
    global _STEPS
    _STEPS += 1
    if _FAIL_AFTER is not None and _STEPS == _FAIL_AFTER:
        raise OSError(f"simulated failure after write step {_STEPS} (HARNESS_INSTALL_FAIL_AFTER)")


def _copy_file(src: Path, dst: Path) -> None:
    if dst.exists():
        os.chmod(dst, stat.S_IWRITE)
    shutil.copy2(src, dst)
    _tick()


def _copy_tree(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for p in src.iterdir():
        if p.is_dir():
            if p.name == "__pycache__":
                continue
            _copy_tree(p, dst / p.name)
        else:
            _copy_file(p, dst / p.name)


def _scripts(h: dict) -> set[str]:
    return set(SCRIPT_RE.findall(str(h.get("command", "")) + " " + str(h.get("commandWindows", ""))))


def _check_shape(cfg: object, what: str) -> dict:
    if not isinstance(cfg, dict) or not isinstance(cfg.get("hooks", {}), dict):
        raise ValueError(f"{what}: top level and 'hooks' must be objects")
    for ev, groups in cfg.get("hooks", {}).items():
        if not isinstance(groups, list) or not all(isinstance(g, dict) and isinstance(g.get("hooks", []), list)
                                                   and all(isinstance(h, dict) for h in g.get("hooks", []))
                                                   for g in groups):
            raise ValueError(f"{what}: hooks.{ev} must be a list of groups with a list of hook objects")
    return cfg


def replaced_scripts(new: dict) -> set[str]:
    return set().union(*(_scripts(h) for v in new.get("hooks", {}).values() for g in v for h in g.get("hooks", [])), set())


def merge_hooks(old: dict, new: dict) -> dict:
    """Keep old entries that run a script the new file does not replace; append the new entries."""
    old, new = _check_shape(old, "existing hooks.json"), _check_shape(new, "built hooks.json")
    if "hooks" not in new:
        raise ValueError("built hooks.json has no 'hooks'")
    replaced = set().union(*(_scripts(h) for v in new["hooks"].values() for g in v for h in g.get("hooks", [])), set())
    out: dict[str, list] = {}
    for event, groups in (old.get("hooks") or {}).items():
        for g in groups:
            kept = [h for h in g.get("hooks", []) if not (_scripts(h) & replaced)]
            if kept:
                out.setdefault(event, []).append({**{k: v for k, v in g.items() if k != "hooks"}, "hooks": kept})
    for event, groups in new["hooks"].items():
        out.setdefault(event, []).extend(groups)
    return {**{k: v for k, v in old.items() if k != "hooks"}, **{k: v for k, v in new.items() if k != "hooks"},
            "hooks": out}


def _features_hooks_on(config: str) -> bool:
    m = re.search(r"^\[features\][ \t]*(?:#.*)?\r?\n((?:(?!\[).*\r?\n?)*)", config, re.M)
    return bool(m and re.search(r"^\s*hooks\s*=\s*true\b", m.group(1), re.M))


def _ps(s: object) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def restore_script(home: Path, snap: Path) -> str:
    lines = ["# Restores the Codex home items from this snapshot (the main settings file is left alone). Generated by tools/install_codex.py.",
             "# Items the install replaced are kept next to the restored ones as <item>.replaced-by-restore-<time>.",
             "$ErrorActionPreference = 'Stop'", f"$home_ = {_ps(home)}", f"$snap = {_ps(snap)}",
             "$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'"]
    for it in RESTORE_ITEMS:
        lines += [f"$item = {_ps(it)}",
                  "$cur = Join-Path $home_ $item; $old = Join-Path $snap $item",
                  "if (Test-Path -LiteralPath $cur) { Move-Item -LiteralPath $cur -Destination \"$cur.replaced-by-restore-$stamp\" }",
                  "if (Test-Path -LiteralPath $old) { Copy-Item -LiteralPath $old -Destination $cur -Recurse }"]
    lines.append("Write-Output 'restored'")
    return "\n".join(lines) + "\n"


def _complete(source: Path) -> list[str]:
    bad = []
    for name, ok in (("AGENTS.md", (source / "AGENTS.md").is_file()),
                     ("agents/*.toml", any((source / "agents").glob("*.toml"))),
                     ("skills/*/SKILL.md", any((source / "skills").glob("*/SKILL.md"))),
                     ("prompts/*.md", any((source / "prompts").glob("*.md"))),
                     ("hooks/*.py", any((source / "hooks").glob("*.py"))),
                     ("hooks.json", (source / "hooks.json").is_file())):
        if not ok:
            bad.append(name)
    return bad


def install(home: Path, snap: Path, source: Path) -> int:
    if not source.is_dir() or _complete(source):
        print("install stopped, build output incomplete (run tools/build.py first): "
              + (", ".join(_complete(source)) if source.is_dir() else f"{source} missing"))
        return 1
    if snap.exists():
        print(f"install stopped: snapshot folder {snap} already exists; use a new one "
              "(after a failed install the FIRST snapshot is the one to restore from)")
        return 1
    cfg_path = home / "config.toml"
    if not cfg_path.is_file() or not _features_hooks_on(cfg_path.read_text(encoding="utf-8-sig")):
        print(f"install stopped: {cfg_path} must contain `hooks = true` under [features] (not edited by this tool)")
        return 1
    try:  # pre-flight: everything that can fail on content fails before the first write
        new_cfg = json.loads((source / "hooks.json").read_text(encoding="utf-8-sig"))
        hp = home / "hooks.json"
        old_cfg = json.loads(hp.read_text(encoding="utf-8-sig")) if hp.is_file() else {"hooks": {}}
        merged = merge_hooks(old_cfg, new_cfg)
    except (ValueError, OSError) as e:
        print(f"install stopped before any change: {e}")
        return 1
    try:
        snap.mkdir(parents=True)
        (snap / "restore.ps1").write_text(restore_script(home, snap), encoding="utf-8-sig", newline="\n")
        for it in ITEMS:  # the snapshot is complete before the first write
            p = home / it
            if p.is_dir():
                shutil.copytree(p, snap / it)
            elif p.is_file():
                shutil.copy2(p, snap / it)
    except OSError as e:
        shutil.rmtree(snap, onexc=_rm_readonly, ignore_errors=True)
        print(f"install stopped, the snapshot could not be written ({e}); nothing was changed")
        return 1
    global _FAIL_AFTER, _STEPS
    _STEPS = 0
    env_n = os.environ.get("HARNESS_INSTALL_FAIL_AFTER", "").strip()
    if env_n.isdigit():
        _FAIL_AFTER = int(env_n)
    try:
        _copy_file(source / "AGENTS.md", home / "AGENTS.md")
        (home / "agents").mkdir(exist_ok=True)
        ship = {p.name for p in (source / "agents").glob("*.toml")}
        for p in sorted((home / "agents").glob("*.toml")):
            if p.name not in ship:
                (snap / "moved-out" / "agents").mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), str(snap / "moved-out" / "agents" / p.name))
        for p in (source / "agents").glob("*.toml"):
            _copy_file(p, home / "agents" / p.name)
        (home / "skills").mkdir(exist_ok=True)
        for d in (source / "skills").iterdir():
            if d.is_dir():
                if (home / "skills" / d.name).is_dir():
                    shutil.rmtree(home / "skills" / d.name, onexc=_rm_readonly)
                _copy_tree(d, home / "skills" / d.name)
        (home / "prompts").mkdir(exist_ok=True)
        for p in (source / "prompts").glob("*.md"):
            _copy_file(p, home / "prompts" / p.name)
        _copy_tree(source / "hooks", home / "hooks")
        tmp = home / "hooks.json.tmp"
        tmp.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        _tick()
        for attempt in range(8):  # Windows may refuse the replace for a moment (antivirus, indexer)
            try:
                os.replace(tmp, home / "hooks.json")
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.2)
    except BaseException as e:  # any failure after the first write (also Ctrl+C): say how to go back
        try:
            (home / "hooks.json.tmp").unlink()
        except OSError:
            pass
        print(f"install FAILED half-way ({type(e).__name__}: {e})\n  the Codex home is mixed; run {snap / 'restore.ps1'} "
              "to go back (config.toml was not touched)")
        return 1
    skills_dir = home / "skills"
    extra_skills = sorted(p.name for p in skills_dir.iterdir()
                          if p.is_dir() and not p.name.startswith(".") and not (source / "skills" / p.name).is_dir()) \
        if skills_dir.is_dir() else []
    n_hooks = sum(len(g["hooks"]) for v in merged["hooks"].values() for g in v)
    print(f"installed into {home}: {len(ship)} agents, {len(list((source / 'skills').iterdir()))} skills, "
          f"{len(list((source / 'prompts').glob('*.md')))} prompts, hooks.json with {n_hooks} hook entries.")
    print(f"snapshot and restore script: {snap}")
    old_hooks = (old_cfg.get("hooks") or {})
    dropped = [f"{ev}: {h.get('command') or h.get('commandWindows')}" for ev, gs in old_hooks.items() for g in gs
               for h in g.get("hooks", []) if _scripts(h) & replaced_scripts(new_cfg)]
    if dropped:
        print(f"old hook entries replaced by harness entries ({len(dropped)}; matched by script file name):")
        for d in dropped:
            print("  - " + d[:160])
    if extra_skills:
        print("skills in the Codex home that the harness does not ship (left alone): " + ", ".join(extra_skills))
    print("NEXT: open Codex and approve the new hooks with /hooks (this tool never writes trust hashes).")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", type=Path, default=Path.home() / ".codex")
    ap.add_argument("--snapshot", type=Path, required=True)
    ap.add_argument("--source", type=Path, default=ROOT / "out" / "codex")
    a = ap.parse_args(argv)
    return install(a.home.resolve(), a.snapshot.resolve(), a.source.resolve())


if __name__ == "__main__":
    sys.exit(main())
