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
        # A COLUMN-0 directive after leading blank lines still counts; an
        # indented one does not (see TestDenyByDefault.test_indented_*).
        assert parse_spec_approval_mode("\n\napproval: auto\n") == "auto"

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

    def test_indented_directive_is_not_top_level(self):
        # Indentation signals nesting (a mapping value), never a top-level
        # directive, so it must NOT enable auto (GPT review, PR #2129).
        assert parse_spec_approval_mode("  approval: auto\n") == ""
        assert parse_spec_approval_mode("\tapproval: auto\n") == ""

    def test_nested_yaml_key_ignored(self):
        # 'approval' nested under another mapping key is not a directive.
        assert parse_spec_approval_mode("---\nsteps:\n  approval: auto\n---\n") == ""
        assert parse_spec_approval_mode("steps:\n  approval: auto\n") == ""

    def test_code_fenced_example_ignored(self):
        # 'approval: auto' shown inside a fenced code block is an example, not
        # a directive, even at column 0 inside the fence.
        assert parse_spec_approval_mode("# How to\n```\napproval: auto\n```\n") == ""
        assert parse_spec_approval_mode("~~~\napproval: auto\n~~~\n") == ""

    def test_fence_not_closed_by_invalid_closer(self):
        # A closing fence must match the opener's char and be at least as long,
        # with no info string. A different char, a shorter run, or an info-string
        # 'closer' does NOT close the block, so the example stays fenced and the
        # directive is never read (GPT review, PR #2129, B8).
        # Opened with ```; a ~~~ line does not close it.
        assert parse_spec_approval_mode("```\n~~~\napproval: auto\n```\n") == ""
        # Opened with ````; a shorter ``` does not close it.
        assert parse_spec_approval_mode("````\n```\napproval: auto\n````\n") == ""
        # A fence line carrying an info string is an OPENER, never a closer.
        assert parse_spec_approval_mode("```\n```yaml\napproval: auto\n```\n") == ""
        # An opened-but-never-closed fence swallows the rest of the region.
        assert parse_spec_approval_mode("```\napproval: auto\n") == ""

    def test_fence_closes_on_valid_longer_closer(self):
        # A closer LONGER than the opener is valid (CommonMark), so a real
        # directive after it still counts — the fix must not over-block.
        assert parse_spec_approval_mode("```\nexample\n`````\napproval: auto\n") == "auto"

    def test_html_commented_directive_ignored(self):
        # 'approval: auto' inside an HTML comment is commented-out text, not a
        # directive. A multi-line comment block puts it at column 0 on its own
        # line where it would otherwise match (GPT review, PR #2129, B6).
        assert parse_spec_approval_mode("<!-- approval: auto -->\n# Task\n") == ""
        assert parse_spec_approval_mode("<!--\napproval: auto\n-->\n# Task\n") == ""
        # An unterminated comment swallows the rest of the scan region.
        assert parse_spec_approval_mode("<!--\napproval: auto\n") == ""
        # A real directive AFTER a closed comment still counts (no over-blocking).
        assert parse_spec_approval_mode("<!-- note -->\napproval: auto\n") == "auto"

    def test_html_element_wrapped_directive_ignored(self):
        # A directive inside ANY HTML element is example/rendered text, not a
        # top-level directive. Handled as a CLASS (any tag), not per-tag (GPT
        # review, PR #2129, B9). A real directive line never contains '<'/'>',
        # so stripping markup can only remove examples, never a genuine directive.
        assert parse_spec_approval_mode("<pre>\napproval: auto\n</pre>\n") == ""
        assert parse_spec_approval_mode("<code>approval: auto</code>\n") == ""
        assert parse_spec_approval_mode("<details>\napproval: auto\n</details>\n") == ""
        assert parse_spec_approval_mode("<div class='x'>\napproval: auto\n</div>\n") == ""
        # An unterminated opening tag swallows the rest of the region.
        assert parse_spec_approval_mode("<pre>\napproval: auto\n") == ""
        # A real directive AFTER a closed element still counts (no over-blocking).
        assert parse_spec_approval_mode("<pre>example</pre>\napproval: auto\n") == "auto"

    def test_mismatched_quotes_rejected(self):
        # Quotes must balance: a lone opening or closing quote is malformed and
        # must NOT enable auto (GPT review, PR #2129).
        assert parse_spec_approval_mode('approval: "auto\n') == ""
        assert parse_spec_approval_mode("approval: auto'\n") == ""
        assert parse_spec_approval_mode("approval: 'auto\"\n") == ""
        assert parse_spec_approval_mode('---\napproval: "auto\n---\n') == ""

    def test_balanced_quotes_still_accepted(self):
        # The tightening must not regress the legitimate quoted forms.
        assert parse_spec_approval_mode('approval: "auto"\n') == "auto"
        assert parse_spec_approval_mode("approval: 'auto'\n") == "auto"

    def test_unterminated_frontmatter_rejected(self):
        # A frontmatter fence with no closing '---' is malformed; deny-by-default
        # rather than scanning body text as frontmatter (GPT review, PR #2129).
        assert parse_spec_approval_mode("---\napproval: auto\n# Task, no close\n") == ""
        assert parse_spec_approval_mode("---\nname: X\napproval: auto\n") == ""


class TestExecutePlanParity:
    """The plan→/execute launch path derives the directive from the SAME bytes
    as /start, so ``approval: auto`` is honored identically on both.

    /start reads a bounded file prefix (``_read_spec_head``); /execute parses the
    run's in-memory ``spec_content`` via ``_approval_head`` (an already-loaded
    snapshot — no fresh disk read, no TOCTOU). For the same spec the two heads
    must parse to the same mode.
    """

    def test_in_memory_head_matches_file_head(self, tmp_path):
        from kiro_crew.dashboard.handlers.taskrunner import (
            _approval_head,
            _read_spec_head,
        )

        for body, expected in [
            ("---\napproval: auto\n---\n# Task: t\n", "auto"),
            ("approval: auto\n# Task\n", "auto"),
            ("# Task: t\n1. do\n2. do more\n", ""),
            ("```\napproval: auto\n```\n", ""),
            ("<!--\napproval: auto\n-->\n# Task\n", ""),
        ]:
            spec = tmp_path / "spec.md"
            spec.write_text(body, encoding="utf-8")
            # /execute feeds the in-memory head to the parser …
            mem_head = _approval_head(body)
            assert parse_spec_approval_mode(mem_head) == expected
            # … and it agrees with the /start file-read head on the same bytes.
            file_head = _read_spec_head(str(spec))
            assert parse_spec_approval_mode(file_head) == parse_spec_approval_mode(mem_head)

    def test_in_memory_truncation_cannot_forge_directive(self):
        # The in-memory head deriver (/execute) drops an incomplete final line at
        # the read boundary, exactly like the file path, so a poisoned line that
        # straddles the window cannot be read as a bare `approval: auto` directive.
        from kiro_crew.dashboard.handlers.taskrunner import (
            _SPEC_APPROVAL_READ_CHARS,
            _approval_head,
        )

        pad = "# filler\n" * ((_SPEC_APPROVAL_READ_CHARS // 9) + 1)
        spec_content = pad + "approval: auto is NOT actually enabled, just prose\n"
        head = _approval_head(spec_content)
        assert "approval: auto is NOT" not in head
        assert parse_spec_approval_mode(head) == ""


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
