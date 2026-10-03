"""Acceptance tests for never cutting a segment mid-construct.

The bug: a segment cut is a persistence boundary, and a tool call triggers one.
While background agents run, their tool calls interleave with the parent's token
stream, so the parent's reply is cut wherever a child happens to call a tool.
One reply then persists as several assistant rows split mid-word, and an options
marker whose closing bracket falls in the next row matches no render grammar --
the chips are lost and the tail reaches the user as literal prose.

Provenance cannot gate the cut. A child tool call reaches this path carrying no
``sub_session_id`` and no agent marker, byte-identical to the parent's own call,
which is why the fix asks WHERE the cut lands instead of who caused it.

The option line here is assembled from a fragment rather than written whole: a
complete ``[OPTIONS: ...]`` literal in a test file is a live UI control marker,
and the harness renders one it finds in transcript text.
"""

from __future__ import annotations

from kiro_crew.dashboard.chat_utils import open_construct_at_end

_OPEN = "[" + "OPTIONS:"


class TestOptionsLine:
    def test_unterminated_options_marker_defers_the_cut(self):
        text = "Board refreshed.\n\n" + _OPEN + " Let it finish | Kill it and relaun"
        assert open_construct_at_end(text) == "options-line"

    def test_closed_options_marker_is_safe_to_cut(self):
        text = "Board refreshed.\n\n" + _OPEN + " Let it finish | Kill it]"
        assert open_construct_at_end(text) == ""

    def test_a_realistic_mid_label_split_point_is_caught(self):
        """A prefix cut inside an option label, the shape a live wave produces."""
        text = (
            "an-out onward I'll use KiroCrew's own path.\n\n"
            + _OPEN
            + " Let it finish, then post the cards | Kill it and relaun"
        )
        assert open_construct_at_end(text) == "options-line"

    def test_a_later_marker_is_the_one_that_counts(self):
        """A closed earlier marker must not mask an open later one."""
        text = _OPEN + " a | b]\n\nmore text\n\n" + _OPEN + " c | d"
        assert open_construct_at_end(text) == "options-line"

    def test_a_bracket_before_the_marker_does_not_close_it(self):
        text = "see [the docs](x)\n\n" + _OPEN + " a | b"
        assert open_construct_at_end(text) == "options-line"


class TestCodeFence:
    def test_open_fence_defers_the_cut(self):
        assert open_construct_at_end("here:\n```python\nx = 1\n") == "code-fence"

    def test_closed_fence_is_safe_to_cut(self):
        assert open_construct_at_end("here:\n```python\nx = 1\n```\ndone.") == ""


class TestWidget:
    def test_unclosed_widget_defers_the_cut(self):
        assert open_construct_at_end('<mcwidget title="X">body') == "mcwidget"

    def test_closed_widget_is_safe_to_cut(self):
        assert open_construct_at_end('<mcwidget title="X">body</mcwidget>') == ""


class TestPlainText:
    def test_plain_prose_is_always_safe(self):
        assert open_construct_at_end("All 30 briefs are on disk.") == ""

    def test_empty_text_is_safe(self):
        assert open_construct_at_end("") == ""

    def test_mid_word_plain_prose_is_still_safe(self):
        """Deliberate: a mid-word cut in prose is cosmetic, not destructive.

        Widening the predicate to every word boundary would defer almost every
        cut and reorder the whole transcript, which costs more than it buys.
        Only constructs that render as a UNIT are protected.
        """
        assert open_construct_at_end("Rather than kill") == ""
