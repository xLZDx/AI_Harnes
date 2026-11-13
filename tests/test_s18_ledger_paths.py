"""S18-T3 tests (B31): tools/ledger.py safe_non_code_path / AGENT_CONFIG_RE.
Root-level plugin folders agents/, commands/, skills/, rules/, prompts/ count as code (not safe); docs/agents/x.md,
docs/BACKLOG.md and core/DECISION_LOG.md stay safe; xclaude/a.md is not treated as .claude.
Run: C:\\Python314\\python.exe -I tests/test_s18_ledger_paths.py"""
import importlib.util
import sys
from pathlib import Path

_s = importlib.util.spec_from_file_location("hv2", Path(__file__).resolve().parent / "hv2.py")
h = importlib.util.module_from_spec(_s)
sys.modules["hv2"] = h
_s.loader.exec_module(h)

sys.path.insert(0, str(h.TOOLS))
import ledger  # noqa: E402


def test_root_level_plugin_files_count_as_code():
    for f in ("agents/x.md", "commands/y.md", "skills/z/SKILL.md", "rules/r.md", "prompts/p.md"):
        assert ledger.safe_non_code_path(f) is False, f
        assert ledger.AGENT_CONFIG_RE.search(f), f


def test_backslash_spelling_is_also_code():
    assert ledger.safe_non_code_path("agents\\x.md") is False
    assert ledger.safe_non_code_path("skills\\z\\SKILL.md") is False


def test_docs_and_logs_stay_safe():
    for f in ("docs/BACKLOG.md", "core/DECISION_LOG.md", "docs/guide.md"):
        assert ledger.safe_non_code_path(f) is True, f


def test_nested_folder_with_a_plugin_name_counts_as_code_at_any_depth():
    # S18 review (commit 61e4eb8): plugin layouts count at any depth, so a docs/ folder named like a plugin folder is code too
    for f in ("docs/skills/z.md", "docs/rules/r.md", "docs/prompts/p.md", "docs/commands/y.md", "docs/agents/x.md"):
        assert ledger.AGENT_CONFIG_RE.search(f), f
        assert ledger.safe_non_code_path(f) is False, f
    for f in ("docs/skillset/z.md", "docs/my-agents/x.md"):  # twin: a longer folder name is not a plugin folder
        assert not ledger.AGENT_CONFIG_RE.search(f), f
        assert ledger.safe_non_code_path(f) is True, f
    for f in ("AGENTS.md", "docs/CLAUDE.md", "x/SKILL.md"):
        assert ledger.safe_non_code_path(f) is False, f


def test_xclaude_is_not_dot_claude():
    assert not ledger.AGENT_CONFIG_RE.search("xclaude/a.md")
    assert ledger.AGENT_CONFIG_RE.search(".claude/a.md")  # twin: the real folder still matches
    assert ledger.AGENT_CONFIG_RE.search("sub/.claude/a.md")
    assert ledger.safe_non_code_path("xclaude/a.md") is True
    assert ledger.safe_non_code_path(".claude/a.md") is False


def test_previously_covered_locations_still_code():
    for f in ("harness/agents/a.md", ".codex/x.md", ".agents/s.md", "harness/skills/s/SKILL.md"):
        assert ledger.safe_non_code_path(f) is False, f


if __name__ == "__main__":
    sys.exit(h.run_all(globals()))
