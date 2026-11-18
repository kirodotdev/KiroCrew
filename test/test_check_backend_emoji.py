"""The backend-emoji gate must be real, wired into CI, and diff-scoped.

``scripts/check_backend_emoji.py`` enforces the "Backend messages" row of
``docs/system-specs/common/code-style.md``. Pins that CI runs it with its base
ref, that it judges string tokens and not comments, and that the doc names it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import gate_script_checks
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_backend_emoji.py"
FAST_GATE = ROOT / ".github" / "workflows" / "fast-gate.yml"
CODE_STYLE = ROOT / "docs" / "system-specs" / "common" / "code-style.md"

SPEC = importlib.util.spec_from_file_location("check_backend_emoji", SCRIPT)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def _gate_step() -> dict:
    workflow = yaml.safe_load(FAST_GATE.read_text(encoding="utf-8"))
    for step in workflow["jobs"]["source-gates"].get("steps") or []:
        if "scripts/check_backend_emoji.py" in str(step.get("run", "")):
            return step
    raise AssertionError("source-gates runs no check_backend_emoji.py step")


class TestCiWiring:
    def test_the_step_self_tests_then_enforces_with_the_base_ref(self) -> None:
        # Without the exported env the script only reports and passes every PR.
        step = _gate_step()
        run = str(step.get("run", ""))
        assert "--test" in run
        assert gate.BASE_ENV in (step.get("env") or {})
        assert f"export {gate.BASE_ENV}" in run

    def test_code_style_names_the_gate_and_the_marker(self) -> None:
        text = CODE_STYLE.read_text(encoding="utf-8")
        assert "scripts/check_backend_emoji.py" in text
        assert gate.BASE_ENV in text
        assert "# emoji-ok: <reason>" in text


class TestRules(gate_script_checks.SelfTestPasses):
    def test_a_string_glyph_is_flagged_and_a_comment_glyph_is_not(self) -> None:
        source = 'print("✅ done")  # ⚠ note\n'
        assert gate.scan_source(source) == [(1, "✅")]

    def test_only_added_lines_are_judged(self) -> None:
        source = 'A = "❌ old"\nB = ":no_entry: new"\n'
        assert gate.scan_source(source, {2}) == [(2, ":no_entry:")]

    def test_the_marker_needs_a_reason(self) -> None:
        assert gate.scan_source('x = "🔧"  # emoji-ok: tool-row reader\n') == []
        assert gate.scan_source('x = "🔧"  # emoji-ok:\n') == [(1, "🔧")]

    def test_an_escaped_glyph_is_flagged_outside_raw_strings(self) -> None:
        # "\u2705" prints the same emoji as the glyph itself.
        assert gate.scan_source('print("\\u2705 done")\n') == [(1, "\\u2705")]
        assert gate.scan_source('R = r"\\u2705"\n') == []

    def test_the_glyph_set_is_the_dashboard_gates(self) -> None:
        # One owner for what an emoji is: the backend gate reads the set from
        # scripts/check_emoji_icons.py instead of keeping a copy.
        icons = gate._load_script("check_emoji_icons")
        assert gate.GLYPH.pattern == icons.GLYPH.pattern
