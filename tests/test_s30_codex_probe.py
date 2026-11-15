"""S30-T1 probe: Codex-style apply_patch with a numbered text plan and no plan tool is allowed; without the plan it is denied.
Run: C:\\Python314\\python.exe -I tests/test_s30_codex_probe.py"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hv2  # noqa: E402
from test_plan_first_gate import asst, check, denied, edit_denied, human, text  # noqa: E402

PATCH = {"command": "*** Begin Patch\n*** Update File: D:\\Repo\\proj\\src\\app.py\n@@\n-a\n+b\n*** End Patch"}
PLAN = "Plan:\n1. Patch the parser module in src/app.py\n2. Run the unit tests for the parser"


def test_codex_apply_patch_with_text_plan_is_allowed():
    e, p = edit_denied([human("Fix the parser bug"), asst(text(PLAN))], "s30codexok", tool="apply_patch", inp=PATCH)
    check(p.returncode == 0 and p.stdout.strip() == "", f"denied: {p.stdout!r}")


def test_codex_apply_patch_without_plan_is_denied():
    e, p = edit_denied([human("Fix the parser bug")], "s30codexno", tool="apply_patch", inp=PATCH)
    check(denied(p), f"expected deny: {p.stdout!r}")


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
