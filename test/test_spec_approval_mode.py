"""Unit tests for spec-declared approval mode parsing.

`task_planner.parse_spec_approval_mode()` reads an optional `approval:` directive
from the top of a spec (YAML frontmatter or a bare top-of-file line). It reports
INTENT only — deny-by-default: only a literal `auto` requests unattended
execution; everything else yields "" (→ per-action prompting). Whether `auto` is
honored is decided later by the launch-provenance gate, NOT here.
"""

from __future__ import annotations

from kiro_crew.task_planner import SPEC_APPROVAL_MODES, parse_spec_approval_mode


class TestFrontmatter:
    def test_frontmatter_auto(self):
        spec = "---\napproval: auto\n---\n# Task: do the thing\n"
        assert parse_spec_approval_mode(spec) == "auto"

    def test_frontmatter_auto_with_other_keys(self):
        spec = "---\nname: My Plan\napproval: auto\ntimeout: 600\n---\nbody\n"
        assert parse_spec_approval_mode(spec) == "auto"

    def test_frontmatter_quoted_value(self):
        assert parse_spec_approval_mode('---\napproval: "auto"\n---\n') == "auto"
        assert parse_spec_approval_mode("---\napproval: 'auto'\n---\n") == "auto"

    def test_frontmatter_per_task_is_recognized_but_not_auto(self):
        # Recognized (so it doesn't fall through as unknown) but requests no trust.
        assert parse_spec_approval_mode("---\napproval: per-task\n---\n") == "per-task"

    def test_frontmatter_per_action(self):
        assert parse_spec_approval_mode("---\napproval: per-action\n---\n") == "per-action"


class TestBareDirective:
    def test_bare_top_line_auto(self):
        assert parse_spec_approval_mode("approval: auto\n# Task\n") == "auto"

    def test_bare_after_blank_lines(self):
        assert parse_spec_approval_mode("\n\n  approval: auto\n") == "auto"

    def test_bare_within_scan_window(self):
        spec = "# Task: something\n\nSome intro.\napproval: auto\n"
        assert parse_spec_approval_mode(spec) == "auto"

    def test_case_insensitive(self):
        assert parse_spec_approval_mode("approval: AUTO\n") == "auto"
        assert parse_spec_approval_mode("APPROVAL: Auto\n") == "auto"


class TestDenyByDefault:
    def test_empty(self):
        assert parse_spec_approval_mode("") == ""
        assert parse_spec_approval_mode("   \n\n") == ""

    def test_none_safe(self):
        assert parse_spec_approval_mode(None) == ""  # type: ignore[arg-type]

    def test_no_directive(self):
        assert parse_spec_approval_mode("# Task: build\n1. step one\n2. step two\n") == ""

    def test_unknown_value(self):
        assert parse_spec_approval_mode("approval: yolo\n") == ""
        assert parse_spec_approval_mode("---\napproval: trust-all\n---\n") == ""

    def test_prose_mention_does_not_match(self):
        # A single bare word is required — prose after the colon must not match.
        assert parse_spec_approval_mode("approval: pending review of the plan\n") == ""

    def test_directive_below_scan_window_ignored(self):
        # 'approval: auto' buried deep in the body must NOT flip the mode.
        body = "# Task\n" + "\n".join(f"line {i}" for i in range(40)) + "\napproval: auto\n"
        assert parse_spec_approval_mode(body) == ""

    def test_approval_inside_frontmatter_only_scans_fence(self):
        # An unterminated fence still only scans up to EOF within the fence; a
        # value after a closed fence is body text and is not read.
        spec = "---\nname: X\n---\n\napproval: auto\n"
        # The bare directive sits just after the fence, within the top window of
        # the post-frontmatter... but our parser scans the FENCE for frontmatter
        # specs. Here the fence has no approval key, so → "".
        assert parse_spec_approval_mode(spec) == ""

    def test_word_boundary_not_confused(self):
        # A key that merely starts with 'approval' must not match.
        assert parse_spec_approval_mode("approval_mode: auto\n") == ""
        assert parse_spec_approval_mode("preapproval: auto\n") == ""


class TestMisc:
    def test_bom_stripped(self):
        assert parse_spec_approval_mode("﻿approval: auto\n") == "auto"

    def test_trailing_comment_ignored(self):
        assert parse_spec_approval_mode("approval: auto  # trusted plan\n") == "auto"

    def test_all_modes_constant_covers_returns(self):
        # Every non-empty return value must be a declared mode.
        for spec, expected in [
            ("approval: auto\n", "auto"),
            ("approval: per-task\n", "per-task"),
            ("approval: per-action\n", "per-action"),
        ]:
            got = parse_spec_approval_mode(spec)
            assert got == expected
            assert got in SPEC_APPROVAL_MODES
