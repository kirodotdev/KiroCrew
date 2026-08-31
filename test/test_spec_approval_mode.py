"""Unit tests for the spec-declared approval mode.

`task_planner.spec_declares_auto()` reports whether a spec's leading YAML
frontmatter DECLARES `approval: auto`. It grants nothing: the grant comes from
the launching human's request body (`auto_approve`), so these tests are about
what is REPORTED, never about what is honored. The gating tests live in
`test_auto_approve.py`.

Deny-by-default throughout: anything other than an unambiguous top-level
`approval: auto` reports False.
"""

from __future__ import annotations

from kiro_crew.task_planner import spec_declares_auto


class TestDeclared:
    def test_frontmatter_auto(self):
        assert spec_declares_auto("---\napproval: auto\n---\n# Task: do the thing\n") is True

    def test_auto_alongside_other_keys(self):
        spec = "---\nname: Widget refactor\napproval: auto\nowner: me\n---\n# Task: x\n"
        assert spec_declares_auto(spec) is True

    def test_quoted_value(self):
        assert spec_declares_auto('---\napproval: "auto"\n---\n') is True
        assert spec_declares_auto("---\napproval: 'auto'\n---\n") is True

    def test_value_is_case_insensitive_but_the_key_is_not(self):
        assert spec_declares_auto("---\napproval: AUTO\n---\n") is True
        assert spec_declares_auto("---\nApproval: auto\n---\n") is False

    def test_leading_blank_lines_tolerated(self):
        # A spec is hand-authored; a stray blank first line must not silently
        # read as "declared nothing" (frontmatter.TASK_SPEC: leading_ws_fence).
        assert spec_declares_auto("\n\n---\napproval: auto\n---\n") is True


class TestDenyByDefault:
    def test_empty_and_none_safe(self):
        assert spec_declares_auto("") is False
        assert spec_declares_auto(None) is False  # type: ignore[arg-type]

    def test_no_frontmatter(self):
        assert spec_declares_auto("# Task: do the thing\n1. step\n") is False

    def test_other_modes_are_not_auto(self):
        # `per-task`/`per-action` are simply not `auto`. Nothing enumerates them
        # any more: only the literal `auto` is meaningful, so there is no mode
        # table left for a typo to land next to.
        assert spec_declares_auto("---\napproval: per-task\n---\n") is False
        assert spec_declares_auto("---\napproval: per-action\n---\n") is False

    def test_unknown_value(self):
        assert spec_declares_auto("---\napproval: yolo\n---\n") is False

    def test_prose_value_never_matches(self):
        assert spec_declares_auto("---\napproval: pending review of the design\n---\n") is False

    def test_substring_is_not_the_value(self):
        assert spec_declares_auto("---\napproval: automatic\n---\n") is False
        assert spec_declares_auto("---\napproval_mode: auto\n---\n") is False

    def test_indented_key_is_not_top_level(self):
        # An indented occurrence belongs to an enclosing mapping or block
        # scalar, not to the document's top level.
        assert spec_declares_auto("---\n  approval: auto\n---\n") is False
        assert spec_declares_auto("---\nsteps:\n  approval: auto\n---\n") is False
        assert spec_declares_auto("---\napproval:\n  auto\n---\n") is False

    def test_block_scalar_cannot_fold_into_a_declaration(self):
        # TASK_SPEC does not resolve block scalars, so the indicator character
        # is the value and the indented continuation stays prose.
        assert spec_declares_auto("---\napproval: |\n  auto\n---\n") is False
        assert spec_declares_auto("---\napproval: >\n  auto\n---\n") is False

    def test_unterminated_frontmatter_is_not_frontmatter(self):
        assert spec_declares_auto("---\napproval: auto\n# Task: x\n") is False

    def test_body_directive_is_outside_the_region(self):
        # Everything after the closing fence is body text, at any depth or
        # distance — including a fenced example.
        assert spec_declares_auto("---\nname: x\n---\napproval: auto\n") is False
        assert spec_declares_auto("---\nname: x\n---\n```\napproval: auto\n```\n") is False

    def test_bare_top_of_file_directive_is_no_longer_honored(self):
        # Deliberate narrowing: a declaration must live in frontmatter. The bare
        # form required scanning arbitrary document text, which is what forced
        # the code-fence and HTML-markup defenses this refactor deleted.
        assert spec_declares_auto("approval: auto\n# Task: x\n") is False
        assert spec_declares_auto("# Task: x\napproval: auto\n") is False

    def test_bom_prefixed_spec_fails_closed(self):
        # A BOM is not whitespace, so this is not frontmatter and the
        # declaration is not reported. Failing closed costs a UI hint, never a
        # grant — the human's checkbox is unaffected either way.
        assert spec_declares_auto("﻿---\napproval: auto\n---\n") is False


class TestMarkupCannotSynthesizeADeclaration:
    """The whole value must BE the word, so no deletion of characters can
    manufacture one out of two harmless fragments."""

    def test_element_span_between_key_and_value(self):
        # GPT 5.6's original finding against a splicing parser: removing the
        # element span from this line leaves `approval:  auto`, which a splicing
        # reader honors even though the author wrote `per-task`. Nothing is
        # removed here, so the value is the whole markup-bearing string.
        assert spec_declares_auto("---\napproval:<span>per-task</span> auto\n---\n") is False

    def test_html_comment_on_the_line(self):
        assert spec_declares_auto("---\napproval: <!-- auto -->\n---\n") is False
        assert spec_declares_auto("---\napproval: auto <!-- really -->\n---\n") is False

    def test_trailing_yaml_comment_is_not_a_declaration_of_auto(self):
        # Not a YAML parser: a trailing `#` comment is part of the value, so the
        # line does not read as a bare `auto`. Fails closed, not open.
        assert spec_declares_auto("---\napproval: auto # trusted plan\n---\n") is False


class TestConflictingDeclarationsFailClosed:
    """Two `approval:` keys report NOT DECLARED.

    `parse_frontmatter` returns a dict, so without this a duplicate would be
    resolved silently by position — and whichever declaration a human reads
    first need not be the one the parser honored (GPT 5.6, PR #2129). The guard
    is the `reject_duplicate_keys` axis on `frontmatter.TASK_SPEC`.
    """

    def test_auto_then_per_action(self):
        assert spec_declares_auto("---\napproval: auto\napproval: per-action\n---\n") is False

    def test_per_action_then_auto(self):
        # Both orders, because a first-wins rule and a last-wins rule disagree
        # about exactly this input and neither is safe.
        assert spec_declares_auto("---\napproval: per-action\napproval: auto\n---\n") is False

    def test_auto_twice(self):
        # Even a non-contradictory repeat is refused: the rule is over the
        # occurrence count, not over whether the values happen to agree.
        assert spec_declares_auto("---\napproval: auto\napproval: auto\n---\n") is False

    def test_second_declaration_separated_by_other_keys(self):
        spec = "---\napproval: auto\nname: x\nowner: me\napproval: yolo\n---\n"
        assert spec_declares_auto(spec) is False

    def test_an_indented_second_occurrence_is_not_a_duplicate(self):
        # Indented lines are prose under this dialect, so they never reached the
        # field dict and cannot suppress a real declaration.
        assert spec_declares_auto("---\napproval: auto\n  approval: per-action\n---\n") is True

    def test_a_body_occurrence_is_not_a_duplicate(self):
        assert spec_declares_auto("---\napproval: auto\n---\napproval: per-action\n") is True
