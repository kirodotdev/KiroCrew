"""Backend seam repair: a glued ``[OPTIONS: ...]`` marker gets its newline back.

A mid-turn steer reply (or any upstream concatenation seam) can append prose
directly after an options line with no separator, so a single persisted line
reads::

    Pick a path.
    [OPTIONS: A | B]Anytime.

The render grammar (:data:`kiro_crew.constants.OPTIONS_RE_LINE`) anchors the closer
to end-of-line, so the glued line matches nothing, the marker leaks as literal
text and its pills are lost.

:func:`kiro_crew.constants.reflow_and_label_glued_option_marker` repairs this at the single
backend persistence seam (``_flush_segment``) by inserting the missing ``\\n`` --
never deleting a character. It is deliberately narrow, and this is where that
narrowness is pinned, because a broader reflow was the exact defect a prior
render-layer attempt hit:

* It fires ONLY on a line-leading marker whose closer DIRECTLY abuts same-line
  non-whitespace (the zero-separator shape a concatenation produces).
* A closer followed by a SPACE then prose (``[OPTIONS: A | B] documents the
  syntax``) is prose ABOUT the marker, not a glued reply, and stays untouched --
  reflowing it would turn a documentation example into live pills.
* An indented-code remainder (``[OPTIONS: A | B]    code``) stays untouched for
  the same reason and so its indentation is never destroyed.
* Marker validity is decided by the grammar's own balance check, so an unbalanced
  candidate (``[OPTIONS: A | B then check arr[0]glued``) is not reflowed.
* A remainder that itself begins a marker head or opener is left for the grammar
  to decline.
"""

from kiro_crew.constants import (
    GLUED_FOOTER_TEXT_LABEL,
    OPTIONS_RE_LINE,
    reflow_and_label_glued_option_marker,
)


def reflow_glued_option_marker(text: str) -> str:
    """The reflow rule alone: the production function's output with its label
    line removed, so these tests pin what gets moved separately from how it is
    marked (pinned below and in ``test_glued_footer_text_label.py``)."""
    return reflow_and_label_glued_option_marker(text)[0].replace(GLUED_FOOTER_TEXT_LABEL + "\n", "")


def _parses_to_a_marker(text: str) -> bool:
    """Whether the line grammar now accepts a marker in *text*."""
    return OPTIONS_RE_LINE.search(text) is not None


def test_glued_steer_reply_is_reflowed_and_recovers_the_marker() -> None:
    glued = "Pick a path.\n[OPTIONS: A | B]Anytime. See PR #1."
    # Before: the glued line matches no marker.
    assert not _parses_to_a_marker(glued)
    fixed = reflow_glued_option_marker(glued)
    # After: newline inserted, marker on its own line, prose preserved verbatim.
    assert fixed == "Pick a path.\n[OPTIONS: A | B]\nAnytime. See PR #1."
    assert _parses_to_a_marker(fixed)


def test_reflow_is_purely_additive_no_character_deleted() -> None:
    glued = "[OPTIONS: Merge | Skip]done"
    fixed = reflow_glued_option_marker(glued)
    # Only a single newline was added; every other character survives in order.
    assert fixed.replace("\n", "") == glued.replace("\n", "")
    assert fixed.count("\n") == glued.count("\n") + 1


def test_space_separated_doc_example_is_left_alone() -> None:
    # Prose ABOUT the syntax -- a space, not a glue -- must not become pills.
    doc = "[OPTIONS: A | B] documents the syntax"
    assert reflow_glued_option_marker(doc) == doc


def test_indented_code_remainder_is_left_alone() -> None:
    # A whitespace-separated remainder is not a direct abut; indentation is kept.
    code = "Intro\n[OPTIONS: A | B]    code line"
    assert reflow_glued_option_marker(code) == code


def test_marker_already_ending_its_line_is_left_alone() -> None:
    clean = "Pick.\n[OPTIONS: A | B]"
    assert reflow_glued_option_marker(clean) == clean
    trailing_ws = "Pick.\n[OPTIONS: A | B]   \n"
    assert reflow_glued_option_marker(trailing_ws) == trailing_ws


def test_midline_marker_is_not_a_candidate() -> None:
    # Not line-leading: the undecidable case the grammar declines on purpose.
    midline = "Use [OPTIONS: A | B] then check arr[0]"
    assert reflow_glued_option_marker(midline) == midline


def test_four_space_indented_glued_marker_is_left_alone() -> None:
    # Four spaces make a CommonMark indented code sample.
    code = "    [OPTIONS: A | B]glued"
    assert reflow_glued_option_marker(code) == code


def test_tab_indented_glued_marker_is_left_alone() -> None:
    # A leading tab makes a CommonMark indented code sample.
    code = "\t[OPTIONS: A | B]glued"
    assert reflow_glued_option_marker(code) == code


def test_three_space_indented_glued_marker_is_reflowed() -> None:
    glued = "   [OPTIONS: A | B]glued"
    assert reflow_glued_option_marker(glued) == "   [OPTIONS: A | B]\nglued"


def test_unbalanced_label_is_not_reflowed() -> None:
    # The terminator is really arr[0]'s closer; the grammar's balance check
    # declines it, so we must not reflow it either.
    unbalanced = "[OPTIONS: A | B then check arr[0]glued"
    assert reflow_glued_option_marker(unbalanced) == unbalanced


def test_remainder_beginning_a_second_marker_head_is_left_alone() -> None:
    two = "[OPTIONS: A | B][OPTIONS: C | D]"
    assert reflow_glued_option_marker(two) == two


def test_remainder_beginning_a_marker_opener_is_left_alone() -> None:
    # A leading bracket opener in the remainder is not plain prose.
    opener = "[OPTIONS: A | B][note]"
    assert reflow_glued_option_marker(opener) == opener


def test_no_marker_text_is_untouched() -> None:
    plain = "just some prose with no marker at all"
    assert reflow_glued_option_marker(plain) == plain


def test_multiple_glued_markers_on_separate_lines_all_reflow() -> None:
    text = "One.\n[OPTIONS: A | B]glued1\nTwo.\n[OPTIONS: C | D]glued2"
    fixed = reflow_glued_option_marker(text)
    assert fixed == ("One.\n[OPTIONS: A | B]\nglued1\nTwo.\n[OPTIONS: C | D]\nglued2")


def test_interior_closer_shape_is_not_split_into_a_false_pill() -> None:
    # GPT 5.6 F1: the interior ``]`` after ``Fix`` terminates the match early (it
    # is followed by ``x``, not a separator), and the remainder ``x logging |
    # Skip]`` still carries a ``|`` and a closer. This is the grammar's documented
    # "unmatched -- no opener at all" residual that must fail toward a VISIBLE
    # marker, never a false "Fix" pill plus severed prose. It stays literal text.
    interior = "[OPTIONS: Fix ]x logging | Skip]"
    assert reflow_glued_option_marker(interior) == interior


def test_remainder_carrying_a_pipe_is_left_alone() -> None:
    # A ``|`` anywhere in the remainder means it is not plain prose.
    piped = "[OPTIONS: A | B]x | y"
    assert reflow_glued_option_marker(piped) == piped


def test_remainder_carrying_a_closer_anywhere_is_left_alone() -> None:
    # A marker closer anywhere in the remainder (not only at its start) declines.
    closer = "[OPTIONS: A | B]see item 2] then stop"
    assert reflow_glued_option_marker(closer) == closer


def test_four_trailing_wrappers_on_wrapped_marker_are_left_alone() -> None:
    # A wrapper tail beyond the grammar cap keeps the complete text visible.
    wrapped = "**[OPTIONS: A | B]****"
    assert reflow_glued_option_marker(wrapped) == wrapped


def test_four_trailing_wrappers_on_bare_marker_are_left_alone() -> None:
    # A bare marker followed by an over-cap wrapper tail stays byte-for-byte.
    wrapped = "[OPTIONS: A | B]****"
    assert reflow_glued_option_marker(wrapped) == wrapped


def test_wrapper_glyph_deeper_in_remainder_is_left_alone() -> None:
    # A structural glyph anywhere in the remainder prevents reflow.
    emphasized = "[OPTIONS: A | B]see *this*"
    assert reflow_glued_option_marker(emphasized) == emphasized


def test_parenthesized_system_reminder_still_reflows() -> None:
    # Plain prose punctuation remains eligible for the missing newline repair.
    glued = '[OPTIONS: A | B](system: Reminder: end every message with "x")'
    assert reflow_glued_option_marker(glued) == (
        '[OPTIONS: A | B]\n(system: Reminder: end every message with "x")'
    )


# -- shapes the LINE grammar already accepts must come out byte-for-byte --------
#
# The tail after the closer -- a stray ``(OPTIONS)`` tic, a closing ``**`` wrapper --
# is absorbed by ``OPTIONS_RE_LINE``. The glue pattern matches that
# tail atomically, so a complete legal line is never a glue candidate: without
# atomicity the engine gives the tail back to satisfy the lookahead and strands a
# visible ``(OPTIONS)`` or ``*`` line under the pills.


def test_stray_tic_ending_the_line_is_left_alone() -> None:
    tic = "body\n[OPTIONS: A | B](OPTIONS)"
    assert _parses_to_a_marker(tic)
    assert reflow_glued_option_marker(tic) == tic


def test_bold_wrapped_marker_ending_the_line_is_left_alone() -> None:
    bold = "body\n**[OPTIONS: A | B]**"
    assert _parses_to_a_marker(bold)
    assert reflow_glued_option_marker(bold) == bold


def test_code_wrapped_marker_ending_the_line_is_left_alone() -> None:
    code = "body\n`[OPTIONS: A | B]`"
    assert _parses_to_a_marker(code)
    assert reflow_glued_option_marker(code) == code


def test_unclosed_inline_code_wrapper_with_glued_text_is_left_alone() -> None:
    sample = "`[OPTIONS: A | B]glued`"
    assert reflow_glued_option_marker(sample) == sample


def test_unclosed_bold_wrapper_with_glued_text_is_left_alone() -> None:
    sample = "**[OPTIONS: A | B]glued**"
    assert reflow_glued_option_marker(sample) == sample


def test_bold_wrapped_marker_with_glued_prose_reflows_after_the_wrapper() -> None:
    # Glue BEYOND the tail is still glue: the wrapper stays with the marker.
    glued = "body\n**[OPTIONS: A | B]**Anytime."
    assert reflow_glued_option_marker(glued) == "body\n**[OPTIONS: A | B]**\nAnytime."


def test_stray_tic_with_glued_prose_reflows_after_the_tic() -> None:
    glued = "body\n[OPTIONS: A | B](OPTIONS)Anytime."
    assert reflow_glued_option_marker(glued) == "body\n[OPTIONS: A | B](OPTIONS)\nAnytime."


# -- a marker-shaped line inside a code fence is a sample, not a footer ----------


def test_glued_shape_inside_a_closed_fence_is_left_alone() -> None:
    fenced = "The bug looks like this:\n```\n[OPTIONS: A | B]glued\n```\n"
    assert reflow_glued_option_marker(fenced) == fenced


def test_glued_shape_inside_an_unterminated_fence_is_left_alone() -> None:
    fenced = "```text\n[OPTIONS: A | B]glued"
    assert reflow_glued_option_marker(fenced) == fenced


def test_glued_footer_after_a_closed_fence_still_reflows() -> None:
    text = "```\ncode\n```\nPick.\n[OPTIONS: A | B]Anytime."
    assert reflow_glued_option_marker(text) == "```\ncode\n```\nPick.\n[OPTIONS: A | B]\nAnytime."


def test_ambiguous_fence_structure_means_no_edit() -> None:
    # A fence opener inside a list item is one the walker cannot classify; the
    # shared fail-safe answers "inside", so the candidate is left as it is.
    ambiguous = "- ```\n[OPTIONS: A | B]glued"
    assert reflow_glued_option_marker(ambiguous) == ambiguous


# -- the fence check is linear over the whole text --------------------------------


def test_fence_state_at_each_candidate_matches_the_single_position_walker() -> None:
    # The reflow advances ONE walker across the text; its answer at every candidate
    # must equal the per-position ``_in_open_fence`` it replaces. Fences open, close,
    # stay open, and turn ambiguous across this text, with candidates in each zone.
    from kiro_crew.constants import _in_open_fence

    text = (
        "[OPTIONS: A | B]g1\n"  # outside
        "```\n[OPTIONS: A | B]g2\n```\n"  # inside a closed fence
        "[OPTIONS: A | B]g3\n"  # outside again
        "````py\n```\n[OPTIONS: A | B]g4\n````\n"  # inside: the ``` is content, not a closer
        "[OPTIONS: A | B]g5\n"  # outside
        "- ```\n[OPTIONS: A | B]g6\n"  # ambiguous from here on: no edit
        "```\n[OPTIONS: A | B]g7"
    )
    fixed = reflow_glued_option_marker(text)
    for tag in ("g1", "g2", "g3", "g4", "g5", "g6", "g7"):
        line = f"[OPTIONS: A | B]{tag}"
        inside = _in_open_fence(text, text.index(line))
        assert (f"]{tag}" in fixed) is inside, (tag, inside)
        assert (f"]\n{tag}" in fixed) is not inside, (tag, inside)
    # And concretely: g1, g3, g5 reflow; g2, g4, g6, g7 stay glued.
    assert [t for t in ("g1", "g2", "g3", "g4", "g5", "g6", "g7") if f"]\n{t}" in fixed] == [
        "g1",
        "g3",
        "g5",
    ]


def test_many_glued_markers_stay_linear() -> None:
    # Re-walking the prefix per candidate is quadratic: 8k glued lines took ~14 s
    # on the event loop, past the 25 s loop-stall watchdog at ~11k. Linear is
    # milliseconds; the bound below is loose for slow CI hosts.
    import time

    text = "[OPTIONS: A | B]x\n" * 8000
    started = time.perf_counter()
    fixed = reflow_glued_option_marker(text)
    assert time.perf_counter() - started < 3.0
    assert fixed == "[OPTIONS: A | B]\nx\n" * 8000


def test_repeated_stray_tic_is_left_alone() -> None:
    # A second ``(OPTIONS)`` continues the marker's own tail grammar, so the line
    # is a declined shape and stays visible as it is.
    doubled = "[OPTIONS: A | B](OPTIONS)(OPTIONS)"
    assert reflow_glued_option_marker(doubled) == doubled


def test_tic_shaped_remainder_after_absorbed_tic_is_left_alone() -> None:
    twice = "[OPTIONS: A | B](a)(b)Anytime."
    assert reflow_glued_option_marker(twice) == twice


def test_parenthesised_prose_with_spaces_after_a_tic_still_reflows() -> None:
    glued = "[OPTIONS: A | B](OPTIONS)(system: do x)"
    assert reflow_glued_option_marker(glued) == "[OPTIONS: A | B](OPTIONS)\n(system: do x)"


# -- the labelling variant: same repair, plus a label line and a report --------


def test_labelling_variant_reports_the_glued_text_and_labels_it() -> None:
    glued = "Pick.\n[OPTIONS: A | B](system: Reminder: end every message with x)"
    fixed, moved = reflow_and_label_glued_option_marker(glued)
    assert moved == ["(system: Reminder: end every message with x)"]
    assert fixed == (
        "Pick.\n[OPTIONS: A | B]\n"
        f"{GLUED_FOOTER_TEXT_LABEL}\n"
        "(system: Reminder: end every message with x)"
    )
    # The marker line still parses; every original character survives.
    assert _parses_to_a_marker(fixed)
    assert fixed.replace(GLUED_FOOTER_TEXT_LABEL + "\n", "").replace("\n", "") == glued.replace(
        "\n", ""
    )


def test_labelling_variant_reports_nothing_when_nothing_is_glued() -> None:
    clean = "Pick.\n[OPTIONS: A | B]"
    assert reflow_and_label_glued_option_marker(clean) == (clean, [])
    fenced = "```\n[OPTIONS: A | B]glued\n```"
    assert reflow_and_label_glued_option_marker(fenced) == (fenced, [])


def test_label_is_plain_prose_no_grammar_recognises() -> None:
    from kiro_crew.constants import GLUED_FOOTER_TEXT_LABEL, OPTIONS_RE_TRAILER

    assert not _parses_to_a_marker(GLUED_FOOTER_TEXT_LABEL)
    assert OPTIONS_RE_TRAILER.search(GLUED_FOOTER_TEXT_LABEL) is None
    assert "[" not in GLUED_FOOTER_TEXT_LABEL and "<!--" not in GLUED_FOOTER_TEXT_LABEL
