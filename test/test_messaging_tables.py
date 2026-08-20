"""Tests for ``kiro_crew.messaging.tables`` -- the pure outbound table renderer.

Everything here is a pure-function test: no channel, no client, no event loop.
The renderer-integration half (which channel converts, and that the canonical
turn text does not) lives in ``test_channel_table_rendering.py``.
"""

from __future__ import annotations

import pytest

from kiro_crew.messaging.tables import (
    GRID_MAX_DISPLAY_COLUMNS,
    TABLE_POLICIES,
    TABLE_POLICY_AUTO,
    TABLE_POLICY_CARDS,
    TABLE_POLICY_GRID,
    TABLE_POLICY_NATIVE,
    TABLE_POLICY_OFF,
    display_width,
    normalize_table_policy,
    render_tables,
    resolve_table_policy,
)

#: The three-column example the card rendering is specified against.
_PROVIDER_TABLE = (
    "| Provider | Auth | Status |\n" "| --- | --- | --- |\n" "| GitHub | OAuth app | Gated |"
)

#: Same shape plus a long fourth column, so ``auto`` must choose cards.
_WIDE_TABLE = (
    "| Provider | Auth | Status | Notes |\n"
    "| --- | --- | --- | --- |\n"
    "| GitHub | OAuth app | Gated | needs an installed app first |"
)

#: ``_PROVIDER_TABLE`` is 29 display columns wide, so ``auto`` grids it.
_PROVIDER_GRID = (
    "```\n"
    "Provider | Auth      | Status\n"
    "---------+-----------+-------\n"
    "GitHub   | OAuth app | Gated\n"
    "```"
)
_PROVIDER_CARDS = "**GitHub**\n- Auth: OAuth app\n- Status: Gated"


def _auto(text: str, *, final: bool = True) -> str:
    return render_tables(text, policy=TABLE_POLICY_AUTO, final=final)


def _two_column_table(width_a: int, width_b: int) -> str:
    """A 2-column table whose rendered grid is ``width_a + width_b + 3`` wide.

    Both the header and the single body row are exactly the column width, so
    no padding is added and the grid's widest line is exactly that sum -- which
    is what makes an exact boundary assertion meaningful rather than
    approximate.
    """
    return (
        f"| {'A' * width_a} | {'B' * width_b} |\n"
        "| --- | --- |\n"
        f"| {'a' * width_a} | {'b' * width_b} |"
    )


class TestDisplayWidth:
    def test_ascii_matches_len(self) -> None:
        assert display_width("Provider") == len("Provider") == 8

    def test_combining_marks_occupy_no_column(self) -> None:
        # "e" + COMBINING ACUTE renders as one glyph in one column, but len() is
        # 2 -- padding by len() would push the rest of the row one space right.
        assert display_width("e\u0301") == 1
        assert display_width("cafe\u0301") == display_width("cafe") == 4

    def test_wide_east_asian_characters_occupy_two_columns(self) -> None:
        assert display_width("\u4f60\u597d") == 4  # two W ideographs
        assert display_width("\uff21") == 2  # fullwidth A (F)
        assert display_width("\u30ab\u30bf") == 4  # katakana (W)

    def test_zero_width_characters_are_free(self) -> None:
        assert display_width("a\u200bb") == 2
        assert display_width("\ufeff") == 0

    def test_halfwidth_and_narrow_forms_stay_one_column(self) -> None:
        assert display_width("\uff71") == 1  # halfwidth katakana (H)
        assert display_width("\u00e9") == 1  # precomposed e-acute (N)


class TestPolicyContract:
    def test_the_five_policies_are_the_contract(self) -> None:
        assert TABLE_POLICIES == {
            TABLE_POLICY_OFF,
            TABLE_POLICY_CARDS,
            TABLE_POLICY_GRID,
            TABLE_POLICY_NATIVE,
            TABLE_POLICY_AUTO,
        }

    @pytest.mark.parametrize("policy", sorted(TABLE_POLICIES))
    def test_known_policies_normalize_to_themselves(self, policy: str) -> None:
        assert normalize_table_policy(policy) == policy

    def test_normalization_is_case_and_whitespace_tolerant(self) -> None:
        assert normalize_table_policy("  CARDS ") == TABLE_POLICY_CARDS

    @pytest.mark.parametrize("bogus", ["", "  ", "table", "vertical", "None"])
    def test_an_unknown_policy_falls_back_to_auto_not_off(self, bogus: str) -> None:
        # Falling back to ``off`` would ship raw pipes to a channel that cannot
        # render them, so the safe fallback is the adaptive policy.
        assert normalize_table_policy(bogus) == TABLE_POLICY_AUTO

    def test_native_on_a_native_target_passes_through(self) -> None:
        assert resolve_table_policy(TABLE_POLICY_NATIVE, native_tables=True) == TABLE_POLICY_OFF

    def test_native_on_an_unsupported_target_becomes_cards(self) -> None:
        assert resolve_table_policy(TABLE_POLICY_NATIVE, native_tables=False) == TABLE_POLICY_CARDS

    def test_auto_on_a_native_target_passes_through(self) -> None:
        assert resolve_table_policy(TABLE_POLICY_AUTO, native_tables=True) == TABLE_POLICY_OFF

    def test_explicit_renderings_are_honoured_on_any_target(self) -> None:
        for policy in (TABLE_POLICY_CARDS, TABLE_POLICY_GRID):
            assert resolve_table_policy(policy, native_tables=True) == policy
            assert resolve_table_policy(policy, native_tables=False) == policy

    def test_off_is_never_widened(self) -> None:
        assert resolve_table_policy(TABLE_POLICY_OFF, native_tables=False) == TABLE_POLICY_OFF


class TestUnsupportedNativeIsCoercedSafely:
    def test_requesting_native_where_it_is_unsupported_emits_cards_not_pipes(self) -> None:
        out = render_tables(_PROVIDER_TABLE, policy=TABLE_POLICY_NATIVE, native_tables=False)
        assert "|" not in out, "an unsupported native target must never get raw pipes"
        assert out == _PROVIDER_CARDS

    def test_declaring_native_where_it_is_supported_changes_nothing(self) -> None:
        out = render_tables(_PROVIDER_TABLE, policy=TABLE_POLICY_NATIVE, native_tables=True)
        assert out == _PROVIDER_TABLE


class TestCards:
    def test_one_card_per_row_first_column_is_the_heading(self) -> None:
        out = render_tables(_PROVIDER_TABLE, policy=TABLE_POLICY_CARDS)
        assert out == _PROVIDER_CARDS

    def test_cards_are_blank_line_separated(self) -> None:
        table = (
            "| Provider | Status |\n" "| --- | --- |\n" "| GitHub | Gated |\n" "| GitLab | Open |"
        )
        assert render_tables(table, policy=TABLE_POLICY_CARDS) == (
            "**GitHub**\n- Status: Gated\n\n**GitLab**\n- Status: Open"
        )

    def test_cards_fit_a_forty_column_mobile_viewport(self) -> None:
        out = render_tables(_PROVIDER_TABLE, policy=TABLE_POLICY_CARDS)
        widths = [display_width(line) for line in out.split("\n")]
        assert max(widths) <= 40, f"card lines exceed a ~40-column phone: {widths}"

    def test_wide_table_cards_still_fit_forty_columns(self) -> None:
        out = _auto(_WIDE_TABLE)
        assert out.startswith("**GitHub**")
        assert max(display_width(line) for line in out.split("\n")) <= 40

    def test_empty_cells_are_omitted_rather_than_rendered_as_bare_labels(self) -> None:
        table = "| Provider | Auth | Status |\n| --- | --- | --- |\n| GitHub |  | Gated |"
        assert render_tables(table, policy=TABLE_POLICY_CARDS) == ("**GitHub**\n- Status: Gated")

    def test_an_empty_first_cell_does_not_invent_a_heading(self) -> None:
        table = (
            "| Category | Item | Notes |\n"
            "| --- | --- | --- |\n"
            "|  | Widget | grouped under the previous category |"
        )
        assert render_tables(table, policy=TABLE_POLICY_CARDS) == (
            "- Item: Widget\n- Notes: grouped under the previous category"
        )

    def test_a_single_column_table_becomes_bare_headings(self) -> None:
        table = "| Provider |\n| --- |\n| GitHub |\n| GitLab |"
        assert render_tables(table, policy=TABLE_POLICY_CARDS) == "**GitHub**\n\n**GitLab**"

    def test_cell_markdown_is_preserved_verbatim(self) -> None:
        table = (
            "| Provider | Docs |\n" "| --- | --- |\n" "| GitHub | [guide](https://example.test) |"
        )
        assert render_tables(table, policy=TABLE_POLICY_CARDS) == (
            "**GitHub**\n- Docs: [guide](https://example.test)"
        )


class TestLosslessRendering:
    def test_header_validation_uses_gfm_pipe_boundaries(self) -> None:
        malformed = f"| `{'a' * 22}|{'b' * 22}` | Status |\n" "| --- | --- |\n" "| row | ok |"
        assert _auto(malformed) == malformed

    def test_disagreeing_gfm_and_render_header_widths_stay_unconverted(self) -> None:
        table = "| `a|b` | Meaning |\n" "| --- | --- | --- |\n" "| left | right | alternation |"
        assert _auto(table) == table

    def test_an_escaped_header_pipe_remains_one_gfm_cell(self) -> None:
        table = "| `a \\| b` | Status |\n" "| --- | --- |\n" "| row | ok |"
        out = _auto(table)
        assert out.startswith("```\n")
        assert "`a | b` | Status" in out

    def test_an_all_empty_wide_body_falls_back_to_a_grid(self) -> None:
        first, second = "A" * 20, "B" * 20
        table = f"| {first} | {second} |\n" "| --- | --- |\n" "|  |  |"
        out = _auto(table)
        assert out.startswith("```\n")
        assert first in out and second in out
        assert out.endswith("\n```")

    def test_a_mixed_empty_row_is_preserved_as_a_card_placeholder(self) -> None:
        first, second = "A" * 20, "B" * 20
        table = f"| {first} | {second} |\n" "| --- | --- |\n" "| populated | value |\n" "|  |  |"
        out = _auto(table)
        assert out == f"**populated**\n- {second}: value\n\n—"


class TestGrid:
    def test_columns_are_padded_to_a_common_width_and_fenced(self) -> None:
        table = "| Provider | Status |\n| --- | --- |\n| GitHub | Gated |\n| GH | Open |"
        assert render_tables(table, policy=TABLE_POLICY_GRID) == (
            "```\n"
            "Provider | Status\n"
            "---------+-------\n"
            "GitHub   | Gated\n"
            "GH       | Open\n"
            "```"
        )

    def test_wide_characters_are_padded_by_display_width(self) -> None:
        table = "| Name | V |\n| --- | --- |\n| \u4f60\u597d | 1 |\n| abcd | 2 |"
        lines = render_tables(table, policy=TABLE_POLICY_GRID).split("\n")
        # "你好" is 4 columns and "abcd" is 4, so both data rows must place the
        # separator in the same column. Padding by len() would put 你好 two
        # columns short.
        assert lines[3].index("|") == lines[4].index("|") - 2
        assert display_width(lines[3]) == display_width(lines[4])

    def test_a_header_only_table_still_renders_its_header(self) -> None:
        # No body row means there is no card to make, and dropping the run
        # would lose the only content it carries.
        out = _auto("| Provider | Status |\n| --- | --- |")
        assert out == "```\nProvider | Status\n---------+-------\n```"

    def test_cell_backticks_cannot_close_the_generated_grid_fence(self) -> None:
        table = "| Value |\n| --- |\n| ``` |"
        out = render_tables(table, policy=TABLE_POLICY_GRID)
        lines = out.split("\n")
        assert lines == ["````", "Value", "-----", "```", "````"]
        assert render_tables(out, policy=TABLE_POLICY_GRID) == out


class TestAutoThreshold:
    def test_the_threshold_is_forty_two_display_columns(self) -> None:
        assert GRID_MAX_DISPLAY_COLUMNS == 42

    def test_exactly_forty_two_columns_renders_as_a_grid(self) -> None:
        out = _auto(_two_column_table(20, 19))
        assert out.startswith("```\n"), "a 42-column table must stay a grid"
        widest = max(display_width(line) for line in out.split("\n"))
        assert widest == 42

    def test_forty_three_columns_flips_to_cards(self) -> None:
        out = _auto(_two_column_table(20, 20))
        assert not out.startswith("```")
        assert out.startswith("**" + "a" * 20 + "**")

    def test_the_boundary_is_measured_in_display_columns_not_characters(self) -> None:
        # 11 ideographs = 22 display columns; with a 19-column second column
        # the grid is 44 wide and must go to cards even though ``len`` sees 11.
        head, cell = "\u4f60" * 11, "\u597d" * 11
        table = f"| {head} | {'B' * 19} |\n| --- | --- |\n| {cell} | {'b' * 19} |"
        assert not _auto(table).startswith("```")


class TestNonTablesAreUntouched:
    def test_prose_passes_through(self) -> None:
        text = "Two options: a or b. Pipe | in a sentence.\n\nAnother paragraph."
        assert _auto(text) == text

    def test_text_without_a_pipe_is_returned_identically(self) -> None:
        assert _auto("no tables here") == "no tables here"

    def test_a_separator_whose_cell_count_does_not_match_is_left_alone(self) -> None:
        # GFM renders this as a paragraph, so converting it would change output
        # the channel already got right.
        malformed = "| a | b | c |\n| --- | --- |\n| 1 | 2 | 3 |"
        assert _auto(malformed) == malformed

    def test_an_empty_separator_cell_is_not_a_separator(self) -> None:
        malformed = "| a | b |\n| --- |  |\n| 1 | 2 |"
        assert _auto(malformed) == malformed

    def test_a_malformed_delimiter_cell_is_not_a_separator(self) -> None:
        malformed = "Command | Details\n-:- | ---\nrm | deletes files"
        assert _auto(malformed) == malformed

    def test_a_list_item_is_not_a_table_separator(self) -> None:
        text = "a | b\n- --- | ---"
        assert _auto(text) == text

    def test_a_pipe_line_with_no_separator_below_is_left_alone(self) -> None:
        text = "| a | b |\n| 1 | 2 |"
        assert _auto(text) == text

    def test_a_horizontal_rule_under_a_pipe_sentence_is_not_a_table(self) -> None:
        text = "cost | benefit\n------\nbody"
        assert _auto(text) == text

    def test_a_four_space_indented_table_is_code_not_a_table(self) -> None:
        text = "    | a | b |\n    | --- | --- |\n    | 1 | 2 |"
        assert _auto(text) == text

    @pytest.mark.parametrize("indent", ["\t", " \t", "  \t"])
    def test_a_tab_indented_table_is_code_not_a_table(self, indent: str) -> None:
        text = f"{indent}| a | b |\n" f"{indent}| --- | --- |\n" f"{indent}| 1 | 2 |"
        assert _auto(text) == text

    def test_an_options_trailer_is_not_swallowed_as_a_body_row(self) -> None:
        # The trailer sits directly under the table with no blank line, and it
        # holds pipes -- absorbing it would render the user's choices as a card
        # and destroy the widget the renderer parses out of it.
        text = _PROVIDER_TABLE + "\n[OPTIONS: retry | cancel]"
        out = _auto(text)
        assert out.endswith("\n[OPTIONS: retry | cancel]")

    def test_a_steering_marker_is_not_swallowed_as_a_body_row(self) -> None:
        text = _PROVIDER_TABLE + "\n[STEERING steer-ab12: folded | in]"
        assert _auto(text).endswith("\n[STEERING steer-ab12: folded | in]")


class TestFencedCodeIsNeverConverted:
    def test_a_backtick_fenced_table_is_byte_identical(self) -> None:
        text = "Example:\n\n```\n" + _PROVIDER_TABLE + "\n```\n\nDone."
        assert _auto(text) == text

    def test_a_tilde_fenced_table_is_byte_identical(self) -> None:
        text = "Example:\n\n~~~\n" + _PROVIDER_TABLE + "\n~~~\n\nDone."
        assert _auto(text) == text

    def test_an_info_string_fence_is_still_a_fence(self) -> None:
        text = "```markdown\n" + _PROVIDER_TABLE + "\n```"
        assert _auto(text) == text

    def test_a_shorter_run_inside_a_longer_fence_does_not_close_it(self) -> None:
        # Fence content is opaque: the inner ``` belongs to the sample.
        text = "````markdown\n```\n" + _PROVIDER_TABLE + "\n```\n````"
        assert _auto(text) == text

    def test_a_tilde_run_does_not_close_a_backtick_fence(self) -> None:
        text = "```\n~~~\n" + _PROVIDER_TABLE + "\n~~~\n```"
        assert _auto(text) == text

    def test_a_backtick_run_with_a_backtick_info_string_is_not_a_fence(self) -> None:
        # ``` `x` ``` is prose about code, not a fence, so the table after it
        # converts rather than being treated as fence content.
        out = _auto("```js `x`\n" + _PROVIDER_TABLE)
        assert out == "```js `x`\n" + _PROVIDER_GRID

    def test_a_table_before_a_fence_still_converts(self) -> None:
        text = _PROVIDER_TABLE + "\n\n```\n| not | converted |\n```"
        assert _auto(text) == _PROVIDER_GRID + "\n\n```\n| not | converted |\n```"

    @pytest.mark.parametrize(
        ("opener", "indent", "closer"),
        [
            ("- ```markdown", "  ", "```"),
            ("+ ~~~markdown", "  ", "~~~"),
            ("* ```", "  ", "```"),
            ("1. ```markdown", "   ", "```"),
            ("1) ~~~", "   ", "~~~"),
            ("10. ```", "    ", "```"),
        ],
    )
    def test_a_list_contained_fence_is_opaque_and_then_closes(
        self, opener: str, indent: str, closer: str
    ) -> None:
        sample = "\n".join(indent + line for line in _PROVIDER_TABLE.splitlines())
        fenced = f"{opener}\n{sample}\n{indent}{closer}"
        text = fenced + "\n\n" + _PROVIDER_TABLE
        assert _auto(text) == fenced + "\n\n" + _PROVIDER_GRID


class TestRawHtmlBlocksAreNeverConverted:
    def test_a_pre_block_is_byte_identical(self) -> None:
        text = "<pre>\n" + _PROVIDER_TABLE + "\n</pre>"
        assert _auto(text) == text

    @pytest.mark.parametrize(
        ("opener", "closer"),
        [
            ('<SCRIPT type="text/plain">', "</sCrIpT>"),
            ("<pre>", "</pre>"),
            ("<style>", "</style>"),
            ("<textarea>", "</textarea>"),
            ("<!--", "-->"),
            ("<?render", "?>"),
            ("<![CDATA[", "]]>"),
            ("<!DOCTYPE", ">"),
        ],
    )
    def test_explicitly_closed_html_blocks_are_opaque_then_release(
        self, opener: str, closer: str
    ) -> None:
        block = opener + "\n" + _PROVIDER_TABLE + "\n" + closer
        text = block + "\n" + _PROVIDER_TABLE
        assert _auto(text) == block + "\n" + _PROVIDER_GRID

    @pytest.mark.parametrize(
        "block",
        [
            '<div class="sample">\n' + _PROVIDER_TABLE + "\n</div>",
            "</section>\n" + _PROVIDER_TABLE,
            "<x-example data-value='a'>\n" + _PROVIDER_TABLE + "\n</x-example>",
        ],
    )
    def test_blank_terminated_html_blocks_are_opaque_then_release(self, block: str) -> None:
        text = block + "\n\n" + _PROVIDER_TABLE
        assert _auto(text) == block + "\n\n" + _PROVIDER_GRID

    def test_a_list_contained_pre_block_is_opaque_then_releases(self) -> None:
        sample = "\n".join("  " + line for line in _PROVIDER_TABLE.splitlines())
        block = "- <pre>\n" + sample + "\n  </pre>"
        text = block + "\n\n" + _PROVIDER_TABLE
        assert _auto(text) == block + "\n\n" + _PROVIDER_GRID

    def test_a_pipe_bearing_html_opener_ends_the_preceding_table(self) -> None:
        block = "<pre data-note='a|b'>\n" + _PROVIDER_TABLE + "\n</pre>"
        text = _PROVIDER_TABLE + "\n" + block
        assert _auto(text) == _PROVIDER_GRID + "\n" + block

    def test_a_raw_tag_name_prefix_is_not_an_html_block(self) -> None:
        text = "<prelude> prose\n" + _PROVIDER_TABLE
        assert _auto(text) == "<prelude> prose\n" + _PROVIDER_GRID


class TestEscapedPipesAndInlineCode:
    def test_an_escaped_pipe_is_cell_content_and_is_unescaped(self) -> None:
        table = "| Expr | Meaning |\n| --- | --- |\n| a \\| b | either |"
        assert render_tables(table, policy=TABLE_POLICY_CARDS) == ("**a | b**\n- Meaning: either")

    def test_inline_code_holding_an_escaped_pipe_survives(self) -> None:
        table = "| Expr | Meaning |\n| --- | --- |\n| `a \\| b` | either |"
        assert render_tables(table, policy=TABLE_POLICY_CARDS) == ("**`a | b`**\n- Meaning: either")

    def test_an_even_backslash_run_leaves_the_next_pipe_a_real_boundary(self) -> None:
        # "\\\\" is a literal backslash, so the pipe after it separates cells.
        # Reading it as an escape would merge two cells and under-count the row.
        table = "| a | b |\n| --- | --- |\n| x\\\\ | y |"
        assert render_tables(table, policy=TABLE_POLICY_CARDS) == "**x\\\\**\n- b: y"

    def test_an_unescaped_pipe_inside_inline_code_is_content_not_a_boundary(self) -> None:
        # GFM would split here and leave the backticks unpaired. Splitting
        # would DELETE the pipe from the rendered card, and a rendering may not
        # lose a character the author wrote -- so a code span is opaque.
        table = "| Expr | Meaning |\n| --- | --- |\n| `a|b` | either |"
        assert render_tables(table, policy=TABLE_POLICY_CARDS) == ("**`a|b`**\n- Meaning: either")

    def test_an_unpaired_backtick_does_not_swallow_the_rest_of_the_row(self) -> None:
        # One stray backtick opening a span to end-of-line would merge every
        # later cell into it.
        table = "| Expr | Meaning |\n| --- | --- |\n| `a | either |"
        assert render_tables(table, policy=TABLE_POLICY_CARDS) == "**`a**\n- Meaning: either"


class TestWhitespaceOutsideRunsIsPreserved:
    def test_indentation_and_blank_lines_around_a_run_survive(self) -> None:
        text = "  leading spaces kept\n\n\n" + _PROVIDER_TABLE + "\n\n\n   trailing block\n"
        out = _auto(text)
        assert out.startswith("  leading spaces kept\n\n\n")
        assert out.endswith("\n\n\n   trailing block\n")

    def test_a_trailing_newline_is_not_eaten(self) -> None:
        assert _auto(_PROVIDER_TABLE + "\n") == _PROVIDER_GRID + "\n"
        assert _auto(_WIDE_TABLE + "\n").endswith("- Notes: needs an installed app first\n")

    def test_trailing_spaces_on_prose_lines_are_kept(self) -> None:
        text = "prose with trailing space   \n\n" + _PROVIDER_TABLE
        assert _auto(text).startswith("prose with trailing space   \n\n")


class TestStreamingContract:
    def test_a_trailing_run_is_left_raw_while_more_rows_may_arrive(self) -> None:
        assert _auto(_PROVIDER_TABLE, final=False) == _PROVIDER_TABLE

    def test_a_trailing_run_followed_only_by_blanks_is_still_deferred(self) -> None:
        text = _PROVIDER_TABLE + "\n\n"
        assert _auto(text, final=False) == text

    def test_a_run_terminated_by_real_content_converts_mid_stream(self) -> None:
        text = _PROVIDER_TABLE + "\n\nand then some prose"
        assert _auto(text, final=False) == _PROVIDER_GRID + "\n\nand then some prose"

    def test_the_final_pass_converts_what_streaming_deferred(self) -> None:
        assert _auto(_PROVIDER_TABLE, final=False) == _PROVIDER_TABLE
        assert _auto(_PROVIDER_TABLE) == _PROVIDER_GRID


class TestContainerAndStreamingBoundaries:
    def test_an_unterminated_outer_pipe_less_row_keeps_the_table_pending(self) -> None:
        text = "Name | State\n--- | ---\nRow "
        assert _auto(text, final=False) == text

    @pytest.mark.parametrize(
        "block",
        ["> Note | caveat", "- Note | caveat", "1. Note | caveat", "# Note | caveat"],
    )
    def test_a_markdown_block_starter_ends_the_table_run(self, block: str) -> None:
        text = _PROVIDER_TABLE + "\n" + block
        assert _auto(text) == _PROVIDER_GRID + "\n" + block

    def test_a_three_space_container_indent_is_kept_on_rendered_lines(self) -> None:
        plain = "| a | b |\n| --- | --- |\n| 1 | 2 |"
        indented = "\n".join("   " + line for line in plain.splitlines())
        expected = "\n".join("   " + line for line in _auto(plain).splitlines())
        rendered = _auto(indented)
        assert rendered == expected
        assert _auto(rendered) == rendered


class TestIdempotence:
    @pytest.mark.parametrize("policy", [TABLE_POLICY_AUTO, TABLE_POLICY_CARDS, TABLE_POLICY_GRID])
    @pytest.mark.parametrize("table", [_PROVIDER_TABLE, _WIDE_TABLE, _two_column_table(20, 19)])
    def test_converting_twice_equals_converting_once(self, policy: str, table: str) -> None:
        once = render_tables(table, policy=policy)
        assert render_tables(once, policy=policy) == once

    def test_a_rendered_grid_is_not_re_entered_as_a_table(self) -> None:
        grid = render_tables(_PROVIDER_TABLE, policy=TABLE_POLICY_GRID)
        assert render_tables(grid, policy=TABLE_POLICY_CARDS) == grid


class TestMultipleRuns:
    def test_every_run_in_one_message_is_converted(self) -> None:
        text = _WIDE_TABLE + "\n\nmiddle prose\n\n" + _WIDE_TABLE
        out = _auto(text)
        assert out.count("**GitHub**") == 2
        assert "middle prose" in out
        assert "|" not in out

    def test_outer_pipes_are_optional_on_both_rows(self) -> None:
        table = "Provider | Status\n--- | ---\nGitHub | Gated"
        assert render_tables(table, policy=TABLE_POLICY_CARDS) == "**GitHub**\n- Status: Gated"

    def test_alignment_colons_are_accepted(self) -> None:
        table = "| Provider | Status |\n|:---|---:|\n| GitHub | Gated |"
        assert render_tables(table, policy=TABLE_POLICY_CARDS) == "**GitHub**\n- Status: Gated"

    def test_a_row_with_missing_cells_is_padded_not_dropped(self) -> None:
        table = "| Provider | Auth | Status |\n| --- | --- | --- |\n| GitHub | OAuth app |"
        assert render_tables(table, policy=TABLE_POLICY_CARDS) == ("**GitHub**\n- Auth: OAuth app")
