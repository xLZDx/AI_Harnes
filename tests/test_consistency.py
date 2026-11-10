"""Text consistency: GPT-PM runs only for the sprint's final task (ship.md, harness-flow, gpt-pm-gate)."""
import importlib.util
import re
import sys
from pathlib import Path

_s = importlib.util.spec_from_file_location("hv2", Path(__file__).resolve().parent / "hv2.py")
h = importlib.util.module_from_spec(_s)
sys.modules["hv2"] = h
_s.loader.exec_module(h)

FILES = ["harness/commands/ship.md", "harness/skills/harness-flow/SKILL.md", "harness/skills/gpt-pm-gate/SKILL.md"]


def _text(rel):
    return (h.SRC / rel).read_text(encoding="utf-8")


def test_each_file_says_gpt_pm_only_for_the_final_task():
    for rel in FILES:
        t = _text(rel)
        assert re.search(r"final task", t, re.I), f"{rel}: no 'final task' statement"
        assert re.search(r"every other task", t, re.I) and re.search(r"done", t, re.I), f"{rel}: final task must mean all other tasks done"
        assert "GPT-PM" in t, rel


def test_each_file_says_intermediate_tasks_stop_without_gpt_pm_or_push():
    for rel in FILES:
        t = _text(rel)
        assert re.search(r"intermediate", t, re.I), f"{rel}: no 'intermediate task' statement"
    for rel in ("harness/commands/ship.md", "harness/skills/harness-flow/SKILL.md"):
        t = _text(rel)
        assert re.search(r"Intermediate task[^\n]*stop[^\n]*no GPT-PM, no push", t), f"{rel}: must say stop, no GPT-PM, no push"
    gate = _text("harness/skills/gpt-pm-gate/SKILL.md")
    assert re.search(r"Intermediate tasks never call it", gate), "gpt-pm-gate must say intermediate tasks never call it"


def test_no_file_still_asks_for_gpt_pm_per_task():
    for rel in FILES:
        t = _text(rel)
        assert not re.search(r"(GPT-PM|gpt-pm-gate)[^\n]*(after|for) (each|every) task", t, re.I), rel
        assert not re.search(r"(each|every) task[^\n]*GPT-PM", t, re.I), rel


if __name__ == "__main__":
    sys.exit(h.run_all(globals()))
