"""Criterion 5: build and --check (contract at 6a6de90). worktree_gc lives in test_gc.py."""
import importlib.util
import sys
from pathlib import Path

_s = importlib.util.spec_from_file_location("hv2", Path(__file__).resolve().parent / "hv2.py")
h = importlib.util.module_from_spec(_s)
sys.modules["hv2"] = h
_s.loader.exec_module(h)


def _build(src, *args):
    return h.run([h.PY, "-I", str(src / "tools" / "build.py"), *args], src)


def test_check_prints_source_ok_on_real_repo():
    res = _build(h.SRC, "--check")
    assert res.rc == 0 and res.out.strip() == "source ok", res.all


def test_build_on_copy_produces_claude_and_codex_layouts():
    src = h.copy_source("bd_copy")
    assert not (src / "out").exists()
    res = _build(src)
    assert res.rc == 0, res.all
    assert "21 agents, 24 skills, 5 commands" in res.out, res.out
    c, x = src / "out" / "claude", src / "out" / "codex"
    assert (c / "CLAUDE.md").is_file() and (c / "CLAUDE.md").stat().st_size > 0
    assert len(list((c / "agents").glob("*.md"))) == 21
    assert len([p for p in (c / "skills").iterdir() if (p / "SKILL.md").is_file()]) == 24
    assert len(list((c / "commands").glob("*.md"))) == 5
    assert (x / "AGENTS.md").is_file()
    assert len(list((x / "prompts").glob("*.md"))) == 5
    agents_md = (x / "AGENTS.md").read_text(encoding="utf-8")
    assert "Agent roster" in agents_md
    for p in (c / "agents").glob("*.md"):
        assert f"`{p.stem}`" in agents_md, f"{p.stem} missing from codex roster"
    core = (src / "harness" / "rules" / "CORE.md").read_text(encoding="utf-8")
    assert (c / "CLAUDE.md").read_text(encoding="utf-8") == core
    (c / "agents" / "stale.md").write_text("x", encoding="utf-8")  # rebuild replaces stale files
    assert _build(src).rc == 0 and not (c / "agents" / "stale.md").exists()


def test_real_out_matches_counts():
    c = h.SRC / "out" / "claude"
    assert c.exists(), "out/claude missing in real repo"
    assert len(list((c / "agents").glob("*.md"))) == 21
    assert len(list((h.SRC / "out" / "codex" / "prompts").glob("*.md"))) == 5


def _bad(name, old, new, agent="code-reviewer"):
    src = h.copy_source(name)
    f = src / "harness" / "agents" / f"{agent}.md"
    t = f.read_text(encoding="utf-8")
    assert old in t
    f.write_text(t.replace(old, new, 1), encoding="utf-8", newline="\n")
    return src


def test_check_fails_when_agent_effort_is_high():
    src = _bad("bd_high", "effort: medium", "effort: high")
    res = _build(src, "--check")
    assert res.rc != 0 and "code-reviewer.md" in res.out and "source ok" not in res.out, res.all
    built = _build(src)
    assert built.rc != 0 and not (src / "out" / "claude").exists(), "build must stop on invalid source"


def test_check_fails_when_agent_model_not_sonnet():
    src = _bad("bd_model", "model: sonnet", "model: opus")
    res = _build(src, "--check")
    assert res.rc != 0 and "code-reviewer.md" in res.out, res.all


def test_check_fails_on_unknown_skill_and_unlisted_agent():
    src = _bad("bd_skill", "- harness-flow", "- no-such-skill")
    res = _build(src, "--check")
    assert res.rc != 0 and "no-such-skill" in res.out, res.all
    src2 = h.copy_source("bd_extra")
    (src2 / "harness" / "agents" / "ghost.md").write_text("---\nname: ghost\nmodel: sonnet\neffort: medium\n---\n", encoding="utf-8")
    res2 = _build(src2, "--check")
    assert res2.rc != 0 and "ghost" in res2.out, res2.all


if __name__ == "__main__":
    sys.exit(h.run_all(globals()))
