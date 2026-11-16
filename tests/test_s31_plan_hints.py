"""S31-T1: the plan-first deny names what is missing, offers a template, says a text plan is no bypass.
Run: C:\\Python314\\python.exe -I tests/test_s31_plan_hints.py"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hv2  # noqa: E402
from test_plan_first_gate import asst, check, denied, edit_denied, human, text  # noqa: E402


def reason_of(rows, name):
    e, p = edit_denied(rows, name)
    check(denied(p), f"{name}: expected deny, got {p.stdout!r} {p.stderr!r}")
    return json.loads(p.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


def test_deny_has_template_bypass_note_and_release_note():
    r = reason_of([human("Fix the parser bug")], "s31base")
    for needle in ("not a bypass", "after 3 denies in one turn the gate releases itself", "Plan:\n1. <the change", "2. Verify the change by running", "Denied 1 of 3"):
        check(needle in r, f"missing {needle!r} in {r!r}")
    check("Why your last list was rejected" not in r, "no list was written, so no reason expected")


def test_reason_for_one_step_list():
    r = reason_of([human("Fix the parser bug"), asst(text("Plan:\n1. Edit the parser module in src/app.py"))], "s31one")
    check("only 1 step" in r, r)


def test_reason_for_missing_verification_step():
    r = reason_of([human("Fix the parser bug"),
                   asst(text("1. Edit the parser module in src/app.py\n2. Update the changelog entry now"))], "s31nover")
    check("no verification step" in r, r)


def test_reason_for_short_step():
    r = reason_of([human("Fix the parser bug"), asst(text("1. Edit parser\n2. Run the unit tests for the parser"))], "s31short")
    check("shorter than 12" in r, r)


def test_reason_for_duplicate_steps():
    r = reason_of([human("Fix the parser bug"),
                   asst(text("1. Run the unit tests for the parser\n2. Run the unit tests for the parser"))], "s31dup")
    check("duplicate" in r, r)


def test_valid_text_plan_still_passes_and_decision_unchanged():
    rows = [human("Fix the parser bug"),
            asst(text("Plan:\n1. Edit the parser module in src/app.py\n2. Run the unit tests for the parser"))]
    e, p = edit_denied(rows, "s31ok")
    check(p.returncode == 0 and p.stdout.strip() == "", f"valid plan denied: {p.stdout!r}")


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
