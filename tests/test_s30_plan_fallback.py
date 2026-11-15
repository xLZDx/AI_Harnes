"""S30-T1: the plan-first gate tells every model that a numbered text plan is accepted when update_plan is missing.
Run: C:\\Python314\\python.exe -I tests/test_s30_plan_fallback.py"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hv2  # noqa: E402
from test_plan_first_gate import (Env, asst, blocked, check, denied, edit_denied, human, pre, stop, text,  # noqa: E402
                                  write_transcript, _changed_rows)

CORE = HERE.parent / "harness" / "rules" / "CORE.md"
TEXT_PLAN = "Plan:\n1. Edit the parser module in src/app.py\n2. Run the unit tests for the parser"


def test_deny_message_names_the_text_plan_fallback():
    e, p = edit_denied([human("Fix the parser bug")], "s30deny")
    check(denied(p), p.stdout)
    reason = json.loads(p.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    check("update_plan is not available" in reason and "is accepted" in reason and "do not report a blocker" in reason, reason)


def test_stop_message_names_the_text_plan_fallback():
    e = Env("s30stop")
    p = e.call(stop(write_transcript(e.dir, _changed_rows())))
    check(blocked(p), p.stdout)
    reason = json.loads(p.stdout)["reason"]
    check("no plan tool" in reason and "never a blocker" in reason, reason)


def test_text_plan_written_after_a_deny_counts_on_retry():
    from test_plan_first_gate import use, result
    edit = {"file_path": r"D:\Repo\proj\src\app.py", "old_string": "a", "new_string": "b"}
    rows = [human("Fix the parser bug"), asst(use("e1", "Edit", edit)),
            result("e1", "PLAN FIRST (operator 2026-11-12): the operator must see the work plan", True),
            asst(text(TEXT_PLAN)), asst(use("e2", "Edit", edit))]
    e, p = edit_denied(rows, "s30retry")
    check(p.returncode == 0 and p.stdout.strip() == "", f"retry after a deny with a text plan was denied: {p.stdout!r}")


def test_allowed_change_whose_output_mentions_plan_first_is_still_a_change():
    from test_plan_first_gate import use, result
    edit = {"file_path": r"D:\Repo\proj\src\app.py", "old_string": "a", "new_string": "b"}
    for out in ("edited; the file now says PLAN FIRST in a comment", "diff: + DENY_MSG = PLAN FIRST (operator 2026-11-12): x"):
        rows = [human("Fix the parser bug"), asst(use("e1", "Edit", edit)), result("e1", out, False),
                asst(text(TEXT_PLAN)), asst(use("e2", "Edit", edit))]
        e, p = edit_denied(rows, "s30abuse")
        check(denied(p), f"a real change whose output mentions PLAN FIRST hid itself: {out!r}")


def test_text_plan_written_after_a_real_change_still_does_not_count():
    from test_plan_first_gate import use, result
    edit = {"file_path": r"D:\Repo\proj\src\app.py", "old_string": "a", "new_string": "b"}
    rows = [human("Fix the parser bug"), asst(use("e1", "Edit", edit)), result("e1", "edited", False),
            asst(text(TEXT_PLAN)), asst(use("e2", "Edit", edit))]
    e, p = edit_denied(rows, "s30late")
    check(denied(p), "a plan written after a real change must not count")


def test_text_plan_without_any_plan_tool_call_passes():
    rows = [human("Fix the parser bug"), asst(text(TEXT_PLAN))]
    e, p = edit_denied(rows, "s30text")
    check(p.returncode == 0 and p.stdout.strip() == "", f"text plan denied: {p.stdout!r}")


def test_core_rule_says_missing_plan_tool_is_not_a_blocker():
    t = CORE.read_text(encoding="utf-8")
    check("A model without" in t and "never a blocker" in t, "CORE.md lacks the missing-plan-tool rule")


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
