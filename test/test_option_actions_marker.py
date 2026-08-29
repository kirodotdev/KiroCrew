"""``[OPTION-ACTIONS:]`` — the grammar, the non-collision, and every strip site.

The new marker is acted on ONLY by the dashboard frontend. Every backend parser
keys on the literal ``[OPTIONS:``, so the new head is INERT for them — and
inert is not harmless: a parser that does not match a marker does not mangle it,
it passes it through VERBATIM as visible text. So the marker leaks raw into Slack,
into the sidebar preview, and — worst — is READ ALOUD by TTS, where the artefact
cannot be scrolled past or re-rendered.

Two properties are pinned here, and the second is the one the whole design rests
on:

1. Every surface STRIPS the marker, and non-dashboard surfaces strip-and-DROP it —
   no widget, no choice. A Slack or Discord button cannot close a dashboard tab,
   so a button built from an action entry would be a button that lies.
2. The two heads NEVER parse each other's markers, in BOTH directions. If that
   ever breaks, an action label becomes a channel button label (free text fed back
   into the session as if the user typed it), or a content choice is silently
   swallowed. Both failures are silent: the regex matches, the anchor is
   satisfied, and only the captured label is wrong.

Every strip-site test carries a paired REGRESSION assertion that an ordinary
``[OPTIONS:]`` marker still behaves exactly as before, because the cheapest way to
make a marker stop leaking is to break the marker that was already working.
"""

from __future__ import annotations

import pathlib
import re
import time

import pytest

from kiro_crew.constants import (
    _MARKER_CLOSE_CLASS,
    _MARKER_HEAD_ALT,
    _MARKER_PREFIX_SCAN_RE,
    _TEMPER,
    MARKER_CLOSERS,
    MARKER_PREFIXES,
    MARKER_STRIP_ANYWHERE_RE,
    OPTION_ACTIONS_RE_LINE,
    OPTION_ACTIONS_RE_TRAILER,
    OPTIONS_RE_LINE,
    OPTIONS_RE_TRAILER,
    _is_inside_unclosed_marker,
    _unclosed_marker_flags,
    excise_marker_spans,
    marker_prefix_is_case_insensitive,
    match_action_markers,
    split_trailing_protocol_suffix,
    strip_action_markers,
)
from kiro_crew.dashboard.state import (
    _KNOWN_OPTION_ACTIONS,
    _ChatSlot,
    _has_option_actions,
    _parse_options,
)
from kiro_crew.messaging.renderer import split_options_trailer
from kiro_crew.messaging.tables import _is_row_candidate, starts_with_marker_head
from kiro_crew.preview_text import strip_markdown_preview
from kiro_crew.slack.format import extract_options
from kiro_crew.slack.handler import (
    _filter_options_brackets,
    _strip_line_markers,
    settle_marker_hold,
)
from kiro_crew.voice_reply import strip_markdown
from kiro_crew.whatsapp.turn_renderer import _strip_options

#: The one authoritative spelling, mirroring `constants.MARKER_PREFIXES` and the
#: frontend's `app-sdk/protocol/optionMarker.ts`. Every test builds its fixture
#: from this rather than re-typing the head, so a spelling change cannot pass
#: here while breaking production.
ACTION_HEAD = "[OPTION-ACTIONS:"
ACTION_MARKER = "[OPTION-ACTIONS: close=Nothing else, close this tab]"
CONTENT_MARKER = "[OPTIONS: Alpha | Beta]"


# ── 1. Grammar ──────────────────────────────────────────────────────────────


class TestActionGrammar:
    def test_canonical_form_matches_both_variants(self):
        assert OPTION_ACTIONS_RE_LINE.search(ACTION_MARKER) is not None
        assert OPTION_ACTIONS_RE_TRAILER.search(ACTION_MARKER) is not None

    def test_label_is_free_text_after_the_first_equals(self):
        """The label is arbitrary prose — commas, ``=``, and brackets included.

        Pinned because the action is a strict enum precisely SO THAT the label can
        stay free text: if the label had to be constrained, an agent writing prose
        about this feature could emit a live close button.
        """
        match = OPTION_ACTIONS_RE_LINE.search("[OPTION-ACTIONS: close=Drop a[1] = done]")
        assert match is not None
        assert match.group(1).strip() == "close=Drop a[1] = done"

    def test_every_cjk_closer_is_accepted(self):
        for close in MARKER_CLOSERS:
            text = f"body prose\n\n[OPTION-ACTIONS: close=Shut it{close}"
            line = OPTION_ACTIONS_RE_LINE.search(text)
            assert line is not None, f"LINE rejected U+{ord(close):04X}"
            assert line.group(1).strip() == "close=Shut it"
            assert (
                OPTION_ACTIONS_RE_TRAILER.search(text) is not None
            ), f"TRAILER rejected U+{ord(close):04X}"

    def test_stray_markdown_link_close_is_absorbed(self):
        """``](OPTIONS)`` is a real model tic; it must not break the end anchor."""
        for suffix in ("(OPTIONS)", "(x)", "()"):
            text = f"[OPTION-ACTIONS: close=Shut it]{suffix}"
            match = OPTION_ACTIONS_RE_LINE.search(text)
            assert match is not None, suffix
            # The absorbed group stays OUTSIDE the label capture.
            assert match.group(1).strip() == "close=Shut it", suffix
            assert OPTION_ACTIONS_RE_TRAILER.search(text) is not None, suffix

    def test_multi_entry_body(self):
        match = OPTION_ACTIONS_RE_LINE.search("[OPTION-ACTIONS: close=Shut it | close=Or this]")
        assert match is not None
        assert [e.strip() for e in match.group(1).split("|")] == [
            "close=Shut it",
            "close=Or this",
        ]


# ── 2. Negative controls + the non-collision, in BOTH directions ────────────


class TestNegativeControls:
    def test_trailing_prose_after_the_close_does_not_match(self):
        """The end anchor is load-bearing: prose after ``]`` is not a marker.

        Without this, a quoted mention mid-answer could swallow the body between
        it and some later ``]``.
        """
        text = "[OPTION-ACTIONS: close=Shut it] and then I did something else"
        assert OPTION_ACTIONS_RE_LINE.search(text) is None
        assert OPTION_ACTIONS_RE_TRAILER.search(text) is None

    def test_spaced_note_after_the_close_does_not_match(self):
        # A gap before ``(`` means it is prose, not the tightly-attached tic.
        assert OPTION_ACTIONS_RE_LINE.search("[OPTION-ACTIONS: close=Shut it] (note)") is None

    def test_unfinished_marker_does_not_match(self):
        assert OPTION_ACTIONS_RE_LINE.search("[OPTION-ACTIONS: close=Shut i") is None
        assert OPTION_ACTIONS_RE_TRAILER.search("[OPTION-ACTIONS: close=Shut i") is None

    def test_content_marker_is_not_an_action_marker(self):
        """A bare ``[OPTIONS: a | b]`` must never parse as an action marker."""
        assert OPTION_ACTIONS_RE_LINE.search(CONTENT_MARKER) is None
        assert OPTION_ACTIONS_RE_TRAILER.search(CONTENT_MARKER) is None
        assert OPTION_ACTIONS_RE_LINE.search("[OPTIONS: close=Shut it]") is None

    def test_action_marker_is_not_a_content_marker(self):
        """The direction that keeps action labels OUT of channel button lists.

        ``[OPTIONS:`` requires the literal ``OPTIONS:`` immediately after ``[``,
        and ``[OPTION-`` cannot supply it. This is the load-bearing half: if it
        broke, ``close=Nothing else, close this tab`` would be rendered as a
        tappable Slack choice and echoed back into the session on click.
        """
        assert OPTIONS_RE_LINE.search(ACTION_MARKER) is None
        assert OPTIONS_RE_TRAILER.search(ACTION_MARKER) is None

    def test_heads_are_not_prefixes_of_each_other(self):
        """Why a ``startswith``/``rfind`` written against one head misses the other.

        The strings diverge at ``S`` vs ``-``. Every raw-scan site in the tree was
        originally written against the single ``"[OPTIONS"`` literal, so this is the
        exact reason each of them silently passed the new marker through.
        """
        assert not ACTION_HEAD.startswith("[OPTIONS")
        assert not "[OPTIONS:".startswith(ACTION_HEAD)
        assert set(MARKER_PREFIXES) == {"[OPTION-ACTIONS", "[OPTIONS"}

    def test_neither_trailer_swallows_the_other_marker(self):
        """MEASURED regression: the pre-tempering TRAILER captured across heads.

        Before the shared cross-tempered body, ``OPTIONS_RE_TRAILER`` on
        ``"[OPTIONS: a | b]\\n[OPTION-ACTIONS: close=x]"`` captured
        ``" a | b]\\n[OPTION-ACTIONS: close=x"`` — so the action marker's raw text
        became a Discord/Telegram BUTTON LABEL and the real second choice was lost.
        The mirror case did the same to the action pattern.
        """
        options_first = f"Body\n{CONTENT_MARKER}\n{ACTION_MARKER}"
        action_first = f"Body\n{ACTION_MARKER}\n{CONTENT_MARKER}"

        # Whichever pattern matches, its capture must stop at its OWN closer.
        for text in (options_first, action_first):
            content = OPTIONS_RE_TRAILER.search(text)
            if content:
                assert ACTION_HEAD not in content.group(1), text
            action = OPTION_ACTIONS_RE_TRAILER.search(text)
            if action:
                assert "[OPTIONS:" not in action.group(1), text

    def test_redos_shape_stays_linear(self):
        """A long run of heads must not blow up: the tempered body buys this."""
        evil = "[OPTION-ACTIONS:" * 4000 + "x"
        start = time.perf_counter()
        assert OPTION_ACTIONS_RE_LINE.search(evil) is None
        assert OPTION_ACTIONS_RE_TRAILER.search(evil) is None
        assert time.perf_counter() - start < 2.0


# ── 3. Every strip site (each paired with an OPTIONS regression) ────────────


BODY = "Here is the answer."


class TestNestedInsideUnclosedMarker:
    """An action marker nested in an UNCLOSED marker head is not a marker.

    The action pattern scans INDEPENDENTLY of the content one, so a nested span
    matches on its own even when the marker enclosing it never closed. On
    ``[OPTIONS: dropped closer [OPTION-ACTIONS: close=X]`` the content marker matches
    nothing -- its body is tempered against every head, so it stops at the ``[`` and
    finds no closer before it -- while the action marker matches in full.

    That mattered on two backend seams at once, and in opposite directions:

    * ``_has_option_actions`` counted it as choices, suppressing
      ``waiting_for_input`` for a row that renders NO chip -- a tab sitting in the
      waiting lane with nothing to press, the exact symptom that function's docstring
      already describes for an out-of-enum action.
    * the preview/voice strip removed it, deleting the broken syntax that is the
      user's only cue that a marker was intended.

    The two must agree, which is why the matcher and the stripper are a PAIR: a span
    the matcher refuses stays visible.
    """

    NESTED = "[OPTIONS: dropped closer [OPTION-ACTIONS: close=Nothing else]"

    def test_the_raw_pattern_DOES_match_it(self):
        """Pins WHY the helper has to exist rather than being a regex tweak.

        A pure-pattern refusal would need variable-length lookbehind, which Python's
        ``re`` does not have. So the raw pattern matching here is not a bug to fix in
        the grammar -- it is the reason the positional filter exists. If this ever
        stops matching, the filter is dead code and should be removed with it.
        """
        assert len(OPTION_ACTIONS_RE_LINE.findall(self.NESTED)) == 1

    def test_the_matcher_refuses_it(self):
        assert match_action_markers(self.NESTED) == []

    def test_it_does_not_read_as_choices(self):
        """The seam the finding named: no chip renders, so this is not choices."""
        assert _has_option_actions(self.NESTED) is False

    def test_the_stripper_leaves_it_visible(self):
        """Paired with the matcher: a refused span is not a marker, so it stays."""
        assert strip_action_markers(self.NESTED) == self.NESTED

    def test_POSITIVE_CONTROL_a_well_formed_marker_still_counts_and_strips(self):
        """Without this, both assertions above would pass on a helper that refuses
        EVERYTHING -- a filter that never admits a marker is not a fix."""
        good = "[OPTIONS: A | B] [OPTION-ACTIONS: close=Nothing else]"
        assert len(match_action_markers(good)) == 1
        assert _has_option_actions(good) is True
        assert "OPTION-ACTIONS" not in strip_action_markers(good)

    def test_NEGATIVE_CONTROL_a_closed_content_marker_does_not_shield_it(self):
        """The guard must key on an UNCLOSED head, not on any preceding head.

        Here the content marker closes properly, so the action marker beside it is a
        real marker and must still count. A guard that rejected any match with a head
        anywhere earlier on the line would break the ordinary two-marker line the
        feature is built around -- and would pass the four assertions above.
        """
        beside = "[OPTIONS: A | B] [OPTION-ACTIONS: close=Done]"
        assert len(match_action_markers(beside)) == 1
        assert _has_option_actions(beside) is True

    def test_an_unclosed_head_on_an_EARLIER_line_does_not_reach_across(self):
        """A marker is line-local, so the scan starts at the line, not the string."""
        two_lines = "[OPTIONS: broken\n[OPTION-ACTIONS: close=Done]"
        assert len(match_action_markers(two_lines)) == 1
        assert _has_option_actions(two_lines) is True

    def test_a_CJK_closer_also_closes_the_enclosing_head(self):
        """The closer scan is derived from ``MARKER_CLOSERS``, so the lookalikes count.

        Without this, the guard would silently treat a CJK-closed content marker as
        unclosed and drop a live action chip beside it.
        """
        cjk = "[OPTIONS: A\u3011 [OPTION-ACTIONS: close=Done]"
        assert len(match_action_markers(cjk)) == 1
        assert _has_option_actions(cjk) is True


class TestPreviewTextStrip:
    def test_action_marker_absent_from_preview(self):
        out = strip_markdown_preview(f"{BODY}\n{ACTION_MARKER}")
        assert ACTION_HEAD not in out
        assert "close=" not in out
        assert out.strip() == BODY

    def test_content_marker_regression(self):
        out = strip_markdown_preview(f"{BODY}\n{CONTENT_MARKER}")
        assert "[OPTIONS:" not in out
        assert out.strip() == BODY

    def test_both_markers_leave_only_the_body(self):
        out = strip_markdown_preview(f"{BODY}\n{CONTENT_MARKER}\n{ACTION_MARKER}")
        assert ACTION_HEAD not in out
        assert "[OPTIONS:" not in out
        assert out.strip() == BODY


class TestVoiceReplyStrip:
    def test_action_marker_is_never_spoken(self):
        out = strip_markdown(f"{BODY}\n{ACTION_MARKER}")
        assert ACTION_HEAD not in out
        assert "close=" not in out
        assert "Nothing else" not in out

    def test_content_marker_regression(self):
        out = strip_markdown(f"{BODY}\n{CONTENT_MARKER}")
        assert "[OPTIONS:" not in out
        assert "Alpha" not in out

    def test_bracket_inside_a_label_does_not_leave_a_tail(self):
        """The old hand-rolled ``.*?]`` stopped at the FIRST ``]``.

        A label containing a bracket left the remainder to be spoken aloud. The
        shared tempered body captures the inner bracket instead.
        """
        out = strip_markdown(f"{BODY}\n[OPTION-ACTIONS: close=Drop a[1] now]")
        assert "now]" not in out
        assert "a[1]" not in out

    def test_cjk_closer_is_not_spoken(self):
        out = strip_markdown(f"{BODY}\n[OPTION-ACTIONS: close=Shut it\u3011")
        assert ACTION_HEAD not in out
        assert "close=" not in out

    def test_a_later_bracket_on_the_LINE_does_not_swallow_the_prose_between(self):
        """The strip must end at the marker's OWN closer, not the line's last one.

        ``_MARKER_BODY_LINE``'s class is ``[^[\\n]`` — it excludes ``[`` and newline
        but NOT ``]`` — and the strip pattern carries no tail anchor, unlike the LINE
        forms which require end-of-line or an abutting sibling. So a greedy body ran
        past the marker's closer, the temper admitted the intervening ``[``, and
        backtracking gave back only the LAST closer on the line: every word between
        was deleted from the utterance.

        A citation is the ordinary shape of that, and it is prose the user DID mean to
        hear, so it must survive. Speech is the surface where this cannot be recovered
        — there is nothing to scroll back to.
        """
        assert strip_markdown("See [OPTIONS: A | B] for details [1]") == "See for details [1]"

    def test_the_same_holds_for_the_ACTION_head(self):
        """Both heads, because ``[OPTIONS`` is not a prefix of ``[OPTION-ACTIONS``.

        They diverge at ``S`` vs ``-``, so a fix verified on one head proves nothing
        about the other — that asymmetry is what made the original local regex speak
        the action marker aloud in full.
        """
        assert (
            strip_markdown("Action [OPTION-ACTIONS: close=Done] and a citation [2]")
            == "Action and a citation [2]"
        )

    def test_brackets_on_BOTH_sides_of_the_marker_survive(self):
        assert strip_markdown("Ref [9] then [OPTIONS: A] then [10]") == "Ref [9] then then [10]"

    def test_negative_control_prose_discussing_the_syntax_keeps_its_tail(self):
        """The control that separates "strip the marker" from "eat the line".

        A sentence merely DISCUSSING the marker is not a dispatchable marker, and the
        strip surface still removes the bracketed token — but everything after it is
        ordinary prose. If a fix over-consumes, this goes red even when the assertions
        above are satisfied by a pattern that simply stops earlier.
        """
        assert (
            strip_markdown("The [OPTIONS:] marker is documented [3]")
            == "The marker is documented [3]"
        )


class TestSlackExtractOptions:
    def test_action_marker_stripped_and_yields_no_choices(self):
        cleaned, choices = extract_options(f"{BODY}\n{ACTION_MARKER}")
        assert ACTION_HEAD not in cleaned
        assert "close=" not in cleaned
        assert choices == []  # strip-and-DROP: a Slack button cannot close a tab
        assert cleaned.strip() == BODY

    def test_content_marker_regression(self):
        cleaned, choices = extract_options(f"{BODY}\n{CONTENT_MARKER}")
        assert choices == ["Alpha", "Beta"]
        assert cleaned == BODY

    def test_no_marker_text_is_returned_byte_identical(self):
        """The conditional ``rstrip`` guard: the no-marker path must not change."""
        raw = "plain answer with trailing space   "
        cleaned, choices = extract_options(raw)
        assert cleaned == raw
        assert choices == []

    def test_both_markers_keep_content_choices_and_drop_the_action(self):
        cleaned, choices = extract_options(f"{BODY}\n{CONTENT_MARKER}\n{ACTION_MARKER}")
        assert choices == ["Alpha", "Beta"]
        assert ACTION_HEAD not in cleaned
        assert cleaned.strip() == BODY

    def test_action_marker_before_content_marker(self):
        cleaned, choices = extract_options(f"{BODY}\n{ACTION_MARKER}\n{CONTENT_MARKER}")
        assert choices == ["Alpha", "Beta"]
        assert ACTION_HEAD not in cleaned


class TestSlackStreamingFilter:
    @staticmethod
    def _stream(text: str) -> str:
        """Feed *text* one character at a time, as the live Slack path does."""
        hold, buf = "", ""
        for ch in text:
            hold, buf = _filter_options_brackets(ch, hold, buf)
        return buf

    def test_action_marker_never_reaches_the_live_bubble(self):
        out = self._stream(f"{BODY}\n{ACTION_MARKER}")
        assert ACTION_HEAD not in out
        assert "close=" not in out
        assert out.strip() == BODY

    def test_content_marker_regression(self):
        out = self._stream(f"{BODY}\n{CONTENT_MARKER}")
        assert "[OPTIONS:" not in out
        assert out.strip() == BODY

    def test_ordinary_brackets_are_still_released(self):
        """The filter must not become a general bracket eater."""
        out = self._stream("See [1] and a[0] and [not a marker].")
        assert out == "See [1] and a[0] and [not a marker]."

    def test_whole_string_in_one_chunk(self):
        hold, buf = _filter_options_brackets(f"{BODY}\n{ACTION_MARKER}", "", "")
        # The marker is on the LAST line with no newline after it, so it is still
        # HELD here rather than already resolved — that is the fix for a `]` inside
        # a label, which cannot be judged complete until the line ends. What the
        # contract requires is that it never becomes visible, so settle it the way
        # the end-of-stream flush does and assert on that.
        assert ACTION_HEAD not in buf
        assert settle_marker_hold(hold) == ""
        assert ACTION_HEAD not in buf + settle_marker_hold(hold)

    def test_prose_sharing_the_final_line_is_recovered_not_dropped(self):
        # The marker span is excised and the sentence that followed it survives, so
        # holding to end-of-stream does not eat text. Same contract as
        # `[OPTIONS: a | b] bye` has always had, now reached via the settle point.
        hold, buf = _filter_options_brackets(f"{CONTENT_MARKER} and then words", "", "")
        assert (buf + settle_marker_hold(hold)).strip() == "and then words"
        assert "[OPTIONS:" not in buf + settle_marker_hold(hold)

    def test_an_unterminated_marker_is_still_dropped(self):
        # Pre-existing behaviour, kept deliberately: a half-written protocol tag
        # must not be painted into the bubble.
        hold, _buf = _filter_options_brackets(f"{BODY}\n[OPTION-ACTIONS: close=Do", "", "")
        assert settle_marker_hold(hold) == ""


class TestMessagingRendererSplit:
    def test_action_marker_stripped_with_no_widget(self):
        body, choices = split_options_trailer(f"{BODY}\n{ACTION_MARKER}")
        assert ACTION_HEAD not in body
        assert choices == []
        assert body.strip() == BODY

    def test_content_marker_regression(self):
        body, choices = split_options_trailer(f"{BODY}\n{CONTENT_MARKER}")
        assert choices == ["Alpha", "Beta"]
        assert body == BODY

    def test_both_markers_keep_content_choices(self):
        body, choices = split_options_trailer(f"{BODY}\n{CONTENT_MARKER}\n{ACTION_MARKER}")
        assert choices == ["Alpha", "Beta"]
        assert ACTION_HEAD not in body
        assert body.strip() == BODY

    def test_action_BEFORE_content_is_still_stripped(self):
        """The ordering the ``\\Z``-anchored strip could not see.

        This is the regression that shipped raw protocol text into channel bodies.
        The strip was TRAILER-anchored, so it only matched an action marker ENDING
        the text: content-then-action was stripped, action-then-content was not —
        and the content branch returns ``text[:match.start()]``, which still
        contains that unstripped action marker. Every channel body (Discord,
        Telegram, Webex, WeCom, Teams) posted the marker verbatim.

        Asserted on the ORDER, not just the outcome, because the two orderings are
        different code paths through the same function.
        """
        body, choices = split_options_trailer(f"{BODY}\n{ACTION_MARKER}\n{CONTENT_MARKER}")
        assert ACTION_HEAD not in body, "action marker leaked into the channel body"
        assert choices == ["Alpha", "Beta"], "content choices must survive the strip"
        assert body.strip() == BODY

    def test_either_ordering_yields_the_same_body_and_choices(self):
        """Ordering symmetry, stated once so neither path can drift from the other."""
        first = split_options_trailer(f"{BODY}\n{ACTION_MARKER}\n{CONTENT_MARKER}")
        second = split_options_trailer(f"{BODY}\n{CONTENT_MARKER}\n{ACTION_MARKER}")
        assert first[1] == second[1]
        assert first[0].strip() == second[0].strip() == BODY

    def test_action_marker_mid_body_is_stripped(self):
        """Neither anchoring covers this; matching per line does.

        Consuming a trailing RUN of both marker types would fix both orderings
        above and still miss an action marker followed by ordinary prose, so the
        line-anchored form is the one that holds. Pinned so a future "optimisation"
        back to a trailing scan fails here rather than in a channel.
        """
        text = f"{BODY}\n{ACTION_MARKER}\nAnd one more sentence after it."
        body, choices = split_options_trailer(text)
        assert ACTION_HEAD not in body
        assert choices == []
        assert "And one more sentence after it." in body

    def test_hide_partial_hides_a_half_arrived_action_marker(self):
        """Streaming surfaces: a fragment must not render as raw protocol text."""
        body, choices = split_options_trailer(
            f"{BODY}\n[OPTION-ACTIONS: close=Sh", hide_partial=True
        )
        assert ACTION_HEAD not in body
        assert choices == []
        assert body == BODY

    def test_buffered_default_keeps_a_partial_fragment(self):
        """``hide_partial=False`` must still refuse to cut the assistant's prose."""
        text = "see the [OPTION-ACTIONS section"
        body, choices = split_options_trailer(text)
        assert body == text
        assert choices == []


class TestSplitTrailingProtocolSuffix:
    def test_complete_action_marker_moves_into_the_suffix(self):
        visible, suffix = split_trailing_protocol_suffix(f"{BODY}\n{ACTION_MARKER}")
        assert ACTION_HEAD not in visible
        assert ACTION_MARKER in suffix

    def test_unfinished_action_marker_is_detached(self):
        """The old ``rfind("[OPTIONS")`` could not see this head at all."""
        visible, suffix = split_trailing_protocol_suffix(f"{BODY}\n[OPTION-ACTIONS: close=Sh")
        assert suffix.startswith(ACTION_HEAD)
        assert ACTION_HEAD not in visible

    def test_a_run_of_both_markers_is_absorbed_whole(self):
        """Either marker left in the visible half is exposed to a length split."""
        for tail in (
            f"{CONTENT_MARKER}\n{ACTION_MARKER}",
            f"{ACTION_MARKER}\n{CONTENT_MARKER}",
        ):
            visible, suffix = split_trailing_protocol_suffix(f"{BODY}\n{tail}")
            assert ACTION_HEAD not in visible, tail
            assert "[OPTIONS:" not in visible, tail
            assert ACTION_HEAD in suffix and "[OPTIONS:" in suffix, tail

    def test_content_only_regression(self):
        visible, suffix = split_trailing_protocol_suffix(f"{BODY}\n{CONTENT_MARKER}")
        assert visible.strip() == BODY
        assert CONTENT_MARKER in suffix


# ── 4. has_options / waiting_for_input for an action-only row ───────────────


def _slot(*messages: dict) -> _ChatSlot:
    s = _ChatSlot("test-slot")
    for m in messages:
        s.messages.append(m)
    return s


class TestActionMarkerIsCaseInsensitive:
    """The frontend renders a live chip for a mixed-case head, so the backend must
    recognise one too.

    The dashboard's regex carries ``i``. A case-sensitive backend pattern therefore
    disagrees with the surface the user is looking at: ``has_options`` is wrong for
    that row, and every strip that keys on the pattern leaves the marker in place —
    so the raw text reaches Slack, the TTS voice and the sidebar preview.
    """

    @pytest.mark.parametrize(
        "head",
        ["[Option-Actions:", "[option-actions:", "[OPTION-actions:", "[oPtIoN-aCtIoNs:"],
    )
    def test_a_mixed_case_head_is_recognised(self, head):
        assert OPTION_ACTIONS_RE_LINE.search(f"{head} close=Done]") is not None

    @pytest.mark.parametrize("head", ["[Option-Actions:", "[option-actions:"])
    def test_a_mixed_case_marker_does_not_leak_into_a_stripped_surface(self, head):
        text = f"Body\n{head} close=Done]"
        assert strip_markdown_preview(text).strip() == "Body"
        assert "close=Done" not in strip_markdown(text)

    def test_the_trailer_form_agrees_with_the_line_form(self):
        # A divergence between one marker's two spellings is the same defect a
        # level down, so the flag is on both.
        assert OPTION_ACTIONS_RE_TRAILER.search("Body\n[Option-Actions: close=Done]") is not None

    def test_the_content_marker_is_deliberately_UNchanged(self):
        # Pre-existing divergence, independent of the action marker and explicitly
        # left for its own change. Pinned so a later edit is a decision, not a drift.
        assert OPTIONS_RE_LINE.search("[options: A | B]") is None


class TestStreamingSuppressionSurvivesABracketInTheLabel:
    """A ``]`` inside an action label must not end suppression early.

    Labels are model-authored free text and the body grammar admits ``]``, so
    ``[OPTION-ACTIONS: close=Done (see [1])]`` is ONE marker. Deciding at the first
    ``]`` ended the hold inside the label and streamed the remainder into the live
    Slack bubble, which is the leak this pins.
    """

    LABEL_WITH_BRACKET = "[OPTION-ACTIONS: close=Done (see [1])]"

    def _stream(self, text, chunk=1):
        """Feed the filter one character at a time, as the live path does."""
        hold, out = "", ""
        for i in range(0, len(text), chunk):
            hold, out = _filter_options_brackets(text[i : i + chunk], hold, out)
        return hold, out

    def test_no_part_of_the_marker_reaches_the_bubble(self):
        _hold, out = self._stream(f"Answer.\n{self.LABEL_WITH_BRACKET}\n")
        assert "close=Done" not in out
        assert ")]" not in out
        assert "[1]" not in out
        assert out.strip() == "Answer."

    def test_a_plain_action_marker_is_still_suppressed(self):
        _hold, out = self._stream("Answer.\n[OPTION-ACTIONS: close=Done]\n")
        assert out.strip() == "Answer."

    def test_a_content_marker_is_still_suppressed(self):
        _hold, out = self._stream("Answer.\n[OPTIONS: A | B]\n")
        assert out.strip() == "Answer."

    def test_ordinary_bracketed_prose_still_releases_promptly(self):
        # Held only to its own closer, NOT to end of line: holding every `[foo]`
        # until the newline would visibly stall the stream.
        hold, out = self._stream("see [note] here")
        assert "[note]" in out
        assert hold == ""

    def test_a_lowercase_marker_is_held_too(self):
        # The candidate test must match the regex's case-insensitivity, or a
        # lowercase marker streams out raw while the batch path strips it.
        _hold, out = self._stream("Answer.\n[option-actions: close=Done]\n")
        assert "close=Done" not in out


class TestSameLineMixedMarkers:
    """Two markers sharing ONE line: both must be recognised, neither may leak.

    The line tail used to require end-of-line, so on a shared line only the TRAILING
    marker could match. The leading one was left unmatched, which on the backend
    means its raw text is posted verbatim into a Slack body, spoken by TTS, and left
    in the sidebar preview — and when the survivor is a destructive ``close``, the
    affordance that reaches the user is the one that deletes the tab.
    """

    MIXED = f"Pick one. {CONTENT_MARKER} {ACTION_MARKER}"
    MIXED_ACTION_FIRST = f"Pick one. {ACTION_MARKER} {CONTENT_MARKER}"

    def test_both_markers_match_their_own_pattern(self):
        assert OPTIONS_RE_LINE.search(self.MIXED) is not None
        assert OPTION_ACTIONS_RE_LINE.search(self.MIXED) is not None

    def test_both_match_when_the_action_marker_comes_first(self):
        assert OPTIONS_RE_LINE.search(self.MIXED_ACTION_FIRST) is not None
        assert OPTION_ACTIONS_RE_LINE.search(self.MIXED_ACTION_FIRST) is not None

    def test_the_content_body_is_captured_not_swallowed(self):
        match = OPTIONS_RE_LINE.search(self.MIXED)
        assert match is not None
        assert match.group(1).strip() == "Alpha | Beta"

    def test_neither_marker_survives_the_preview_strip(self):
        out = strip_markdown_preview(self.MIXED)
        assert "[OPTIONS:" not in out
        assert ACTION_HEAD not in out

    def test_neither_marker_is_spoken(self):
        out = strip_markdown(self.MIXED)
        assert "[OPTIONS:" not in out
        assert ACTION_HEAD not in out

    def test_slack_gets_the_content_choices_and_no_raw_marker(self):
        cleaned, choices = extract_options(self.MIXED)
        assert choices == ["Alpha", "Beta"]
        assert "[OPTIONS:" not in cleaned
        assert ACTION_HEAD not in cleaned

    def test_same_kind_pair_leaves_no_raw_marker(self):
        """Both are recognised now, so neither leaks.

        WHICH marker supplies the choices is a SEPARATE, pre-existing divergence and is
        deliberately not changed here: ``extract_options`` uses ``.search()``, so this
        side takes the FIRST marker, while the frontend's ``matchAll`` takes the LAST.
        Measured: the backend already returned the first marker for a pair on SEPARATE
        lines, so terminating before a same-line sibling makes the same-line case
        consistent with this side's own existing rule rather than introducing a new one.
        The leak is what this fix is about, and the leak is gone.
        """
        out = strip_markdown_preview("Body [OPTIONS: A] [OPTIONS: B]")
        assert "[OPTIONS:" not in out
        _cleaned, choices = extract_options("Body [OPTIONS: A] [OPTIONS: B]")
        assert choices == ["A"]
        # Same answer for the multi-line layout, which is the point: one rule, not two.
        _c2, choices2 = extract_options("Body\n[OPTIONS: A]\n[OPTIONS: B]")
        assert choices2 == ["A"]

    def test_a_marker_followed_by_prose_is_still_not_a_marker(self):
        """The terminator admits a sibling MARKER, never arbitrary trailing text."""
        assert OPTIONS_RE_LINE.search("See [OPTIONS: A | B] for details") is None


class TestMixedCaseActionSiblingDoesNotCorruptTheContentMarker:
    """A case-SENSITIVE temper against a case-INSENSITIVELY matched head corrupts.

    ``OPTION_ACTIONS_RE_*`` carry ``re.IGNORECASE`` to match the frontend's ``i``
    flag, so ``[Option-Actions: close=B]`` IS a live marker: the dashboard renders a
    chip for it. But the temper and the line tail's sibling lookahead shared a
    case-SENSITIVE head alternation, so the content pattern did not recognise that
    same text as a head — its negative lookahead succeeded and the body consumed
    straight through the sibling.

    The result is not a missed match, it is DATA CORRUPTION carried to every client:
    the action marker arrives as a user-visible option LABEL. That label is fed back
    into the session verbatim when clicked, so the user's own next message becomes a
    protocol marker.
    """

    #: The exact corrupted capture, quoted from the measurement rather than
    #: paraphrased — this test may only pass because the temper recognises the head,
    #: never because the expected string drifted toward whatever the code emits.
    CORRUPTED = " A] [Option-Actions: close=B"
    MIXED = "[OPTIONS: A] [Option-Actions: close=B]"

    def test_the_content_body_is_not_the_corrupted_capture(self):
        match = OPTIONS_RE_LINE.search(self.MIXED)
        assert match is not None
        assert match.group(1) != self.CORRUPTED
        assert match.group(1).strip() == "A"

    def test_no_casing_of_the_action_head_leaks_into_a_content_label(self):
        """Every casing, not just the one the finding happened to quote."""
        for head in (
            "OPTION-ACTIONS:",
            "Option-Actions:",
            "option-actions:",
            "OpTiOn-AcTiOnS:",
        ):
            text = f"[OPTIONS: A] [{head} close=B]"
            match = OPTIONS_RE_LINE.search(text)
            assert match is not None, head
            assert match.group(1).strip() == "A", (head, match.group(1))

    def test_the_choices_delivered_to_a_client_are_clean(self):
        """Pins the layer the finding's chain ends at, not just the regex.

        ``extract_options`` is what hands choices to a channel client; the dashboard
        reaches the same capture group through ``_parse_options``. A regex-only
        assertion would pass while a caller re-derived the label some other way.
        """
        cleaned, choices = extract_options(self.MIXED)
        assert choices == ["A"]
        assert "Option-Actions" not in cleaned
        assert self.CORRUPTED not in cleaned
        assert _parse_options(self.MIXED) == ["A"]

    def test_the_mixed_case_action_marker_still_parses_from_the_shared_line(self):
        """Terminating before the sibling must not consume it.

        If the content pattern swallowed the sibling to stay 'clean', the action
        marker would vanish instead of being corrupted — a different bug with the
        same green test. So assert the action side still gets its own match.
        """
        match = OPTION_ACTIONS_RE_LINE.search(self.MIXED)
        assert match is not None
        assert match.group(1).strip() == "close=B"

    def test_negative_control_the_content_head_is_NOT_widened(self):
        """The control that makes the fix per-head rather than a blanket widening.

        Slapping ``re.IGNORECASE`` on ``OPTIONS_RE_*`` would also make the
        assertions above pass — while changing how every pre-existing
        ``[OPTIONS:]`` marker on every streamed channel message parses, which is
        explicitly out of scope. A mixed-case CONTENT sibling must therefore still
        behave exactly as it did before this fix: swallowed, because ``Options:`` is
        not a recognised head. This test FAILS if anyone reaches for the blanket
        flag, which is the only way the pair above can be trusted.
        """
        match = OPTIONS_RE_LINE.search("[OPTIONS: A] [Options: B]")
        assert match is not None
        assert match.group(1) == " A] [Options: B"


class TestOnlyOneBackendFileDefinesTheMarkerPatterns:
    """The backend mirror of ``website/src/test/chatProtocolBoundary.test.ts``.

    The frontend has asserted "the protocol module is the only non-test source that
    defines the markers" for a while. The BACKEND had no such rule, and that is
    exactly where the leak this PR fixes came from: ``voice_reply`` carried its own
    hand-rolled marker regex, so it never learned about the new head and SPOKE it
    aloud. Two grep ratchets in this same PR had also gone vacuous. Every one of
    those is the same defect — a scanner written against one literal, in a file that
    has no reason to know the grammar changed.

    A hand-rolled pattern is not caught by review reliably and not by a type checker
    at all, so it is caught here: a NEW file that spells a marker head in a regex
    fails this test and has to either use the shared patterns or argue its way onto
    the list below.

    Prose is deliberately NOT matched. An ESCAPED bracket is the signal — that means
    someone is DEFINING the pattern rather than discussing the syntax, and the
    grammar is explicitly tempered to allow the discussion.
    """

    #: Escaped-bracket OPTIONS-family head: a definition, not a mention.
    #:
    #: Scoped to this marker family on purpose. ``\[STEERING`` is defined locally in
    #: three renderers already — a pre-existing pattern for a different marker with
    #: its own history, and ratcheting it here would be an unrelated change wearing
    #: this one's justification. ``OPTIONS?`` covers ``\[OPTION``, ``\[OPTIONS`` and
    #: ``\[OPTION-ACTIONS`` alike, since the head is a prefix of the last.
    MARKER_DEFINITION = re.compile(r"\\\[OPTIONS?")

    #: ``#``-comments are stripped before scanning. A comment RECORDING that a local
    #: copy was removed is documentation of this very fix — ``voice_reply.py`` carries
    #: exactly that — and counting it as a definition would make the ratchet demand
    #: an exemption for the file it just cured.
    COMMENT = re.compile(r"#.*")

    #: Paths (relative to ``src/kiro_crew``) allowed to define a marker pattern.
    #: EXACT matches, each with the reason it is here — a bare set would grow
    #: silently, which is how the thing this test prevents gets back in.
    ALLOWED = {
        "constants.py": (
            "the grammar's only home: every head, temper, body and tail is defined "
            "here once and imported everywhere else"
        ),
        "context_management.py": (
            "PRE-EXISTING and out of this change's scope: two local patterns for the "
            "fixed `[OPTION: Go | Cancel]` compaction footer, which is a different "
            "marker family with fixed labels rather than the parsed protocol. Listed "
            "so the ratchet is honest about what is already here; it must not grow, "
            "and folding these into the shared grammar is a separate change."
        ),
    }

    def _definers(self) -> list[str]:
        root = pathlib.Path(__file__).resolve().parent.parent / "src" / "kiro_crew"
        assert root.is_dir(), f"backend source root not found at {root}"
        out = []
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if self.MARKER_DEFINITION.search(self.COMMENT.sub("", text)):
                out.append(path.relative_to(root).as_posix())
        return out

    def test_positive_control_the_scan_finds_the_grammar_owner(self):
        """Without this the whole test passes vacuously.

        A scan that walks the wrong directory, or a pattern that stopped matching,
        yields an EMPTY offender list — which reads exactly like success. So require
        the one file that certainly defines the patterns to be found.
        """
        assert "constants.py" in self._definers()

    def test_no_new_backend_file_defines_a_marker_pattern(self):
        unexpected = [p for p in self._definers() if p not in self.ALLOWED]
        assert unexpected == [], (
            "these files define a marker pattern themselves instead of importing the "
            f"shared ones from constants.py: {unexpected}. Import the shared pattern, "
            "or add the path to ALLOWED with the reason it must be an exception."
        )

    def test_the_allowlist_carries_no_stale_entry(self):
        """An exemption that no longer applies is a hole left open by accident.

        If a listed file stops defining a pattern — folded into the shared grammar,
        or deleted — the entry must go, or it silently re-permits a future
        definition in that same path.
        """
        definers = set(self._definers())
        stale = sorted(p for p in self.ALLOWED if p not in definers)
        assert stale == [], f"ALLOWED entries no longer needed: {stale}"


class TestPreExistingOptionsBehaviourIsUnchanged:
    """A CHARACTERIZATION pin: what an already-shipped ``[OPTIONS:]`` message does.

    This PR rewrites machinery the OLD marker shares — one tempered body, one head
    alternation, one line tail, one streaming settle. Every other test here asserts a
    PROPERTY, and a property test cannot answer the question review actually asked:
    does a message that shipped last week still strip and stream the same way? Inside
    a 50-file feature PR such a regression reads as unrelated and gets attributed
    somewhere else entirely.

    The expectations are therefore CAPTURED, not authored: the same corpus was run
    against the pre-PR implementation on a pristine worktree at this branch's
    merge-base, and the values were generated from that run. A future edit to the
    shared grammar that moves any of them fails here with the old value in the
    message.

    MEASURED, and not hypothetically: the first version of this change repointed
    ``voice_reply`` from its own permissive regex to the anchored LINE form, and
    ``"See [OPTIONS: A | B] for details"`` — spoken as ``"See for details"`` before —
    began being READ ALOUD in full, which is the worst surface for the artefact to
    reach. This corpus found it; review had not. That is why
    ``MARKER_STRIP_ANYWHERE_RE`` exists: a surface whose job is to REMOVE protocol
    noise needs a different pattern from one deciding whether to DISPATCH.
    """

    #: ``input -> (spoken, slack_cleaned, slack_choices)`` as produced by the PRE-PR
    #: implementation. GENERATED from the captured run, not transcribed: the first
    #: attempt was hand-copied from a summary that printed only ``spoken``, and two
    #: rows silently took that value in the ``slack_cleaned`` column — the same class
    #: of error this test exists to catch, one level up.
    UNCHANGED = [
        ("Pick one. [OPTIONS: Alpha | Beta]", "Pick one.", "Pick one.", ["Alpha", "Beta"]),
        ("Pick one.\n[OPTIONS: Alpha | Beta]", "Pick one.", "Pick one.", ["Alpha", "Beta"]),
        ("[OPTIONS: Only]", "", "", ["Only"]),
        # The singular `[OPTION:` head is not known to this side at all — a
        # pre-existing divergence from the frontend, declared and untouched here.
        ("[OPTION: Single]", "[OPTION: Single]", "[OPTION: Single]", []),
        ("Body\n[OPTIONS: A]\n[OPTIONS: B]", "Body", "Body", ["A"]),
        # THE ROW THE REGRESSION HIT. Prose after a marker on the same line is not a
        # dispatchable marker — deliberate, so a sentence discussing the syntax cannot
        # become a button — yet speech must still not read the brackets aloud. The two
        # columns legitimately DIFFER here, which is exactly what the hand-copied
        # version got wrong.
        (
            "See [OPTIONS: A | B] for details",
            "See for details",
            "See [OPTIONS: A | B] for details",
            [],
        ),
        (
            "An array literal [1, 2, 3] and a link [text](url).",
            "An array literal [1, 2, 3] and a link text.",
            "An array literal [1, 2, 3] and a link [text](url).",
            [],
        ),
        (
            "Trailing prose after.\n[OPTIONS: Yes (recommended) | No]",
            "Trailing prose after.",
            "Trailing prose after.",
            ["Yes (recommended)", "No"],
        ),
        ("[OPTIONS: Set x=1 | Keep, as is]", "", "", ["Set x=1", "Keep, as is"]),
        ("Unclosed [OPTIONS: A | B", "Unclosed [OPTIONS: A | B", "Unclosed [OPTIONS: A | B", []),
        (
            "Markdown tic [OPTIONS: A | B](OPTIONS)",
            "Markdown tic OPTIONS: A | B",
            "Markdown tic",
            ["A", "B"],
        ),
    ]

    @pytest.mark.parametrize(
        ("text", "spoken", "cleaned", "choices"),
        UNCHANGED,
        ids=[t[:38] for t, *_ in UNCHANGED],
    )
    def test_matches_the_pre_pr_implementation(self, text, spoken, cleaned, choices):
        assert strip_markdown(text) == spoken
        got_cleaned, got_choices = extract_options(text)
        assert got_cleaned == cleaned
        assert got_choices == choices

    def test_positive_control_the_corpus_exercises_real_markers(self):
        """Guards the table above against going vacuous.

        If ``extract_options`` stopped recognising anything at all, every ``choices``
        would be ``[]`` — and four rows legitimately expect ``[]``, so a wholesale
        failure could hide among them. Require the corpus to still yield real choices.
        """
        yielded = [c for _t, _s, _cl, c in self.UNCHANGED if c]
        assert len(yielded) >= 5, yielded

    def test_the_three_intended_changes_are_the_ONLY_ones(self):
        """The changes to pre-existing behaviour, asserted rather than left implicit.

        All three are improvements. Pinning them means the description and the code
        cannot drift apart silently, and a FOURTH change cannot appear unnoticed — it
        would have to break a row above or be added here deliberately.
        """
        # 1. Two markers on ONE line — the declared rider. Before, the leading marker
        #    leaked RAW into the Slack body and the TRAILING one supplied the choices.
        #    Now both are recognised, so neither leaks, and the FIRST supplies them,
        #    which is already this side's rule for the multi-LINE layout above.
        cleaned, choices = extract_options("Body [OPTIONS: A] [OPTIONS: B]")
        assert cleaned == "Body", "the leading marker must no longer leak raw"
        assert choices == ["A"]

        # 2. A bracket INSIDE a label no longer leaves a tail to be spoken. The old
        #    local regex was lazy and stopped at the first closer, so `| B]` survived
        #    into the utterance.
        assert strip_markdown("Nested brackets [OPTIONS: A [inner] | B]") == "Nested brackets"

        # 3. A CJK closer is recognised. Before, the marker was spoken in full,
        #    because the local regex accepted only an ASCII `]`.
        assert strip_markdown("CJK closer [OPTIONS: A | B\u3011") == "CJK closer"


class TestTheTwoLanguagesAgreeOnTheActionEnum:
    """The backend mirror and the frontend authority must name the SAME actions.

    ``_KNOWN_OPTION_ACTIONS`` exists only to mirror the frontend's ``KNOWN_ACTIONS``,
    and both comments say the frontend is the authority — but nothing FAILED when
    they drifted. The consequence is silent and asymmetric: a member added to the
    frontend alone makes the dashboard render a chip for a row the backend does not
    count as offering choices, so ``has_options`` (and the ``waiting_for_input``
    state derived from it) is wrong for that row.

    Read from the frontend SOURCE rather than restating its list here. Restating it
    would put a third copy in play and pin nothing.
    """

    FRONTEND = (
        pathlib.Path(__file__).resolve().parents[1]
        / "website"
        / "src"
        / "app-sdk"
        / "protocol"
        / "options.ts"
    )

    def _frontend_actions(self) -> set[str]:
        src = self.FRONTEND.read_text(encoding="utf-8")
        # Body is "anything but a closing bracket", NOT a lazy `.*?` under DOTALL:
        # measured while negative-controlling this test, a lazy body ran PAST the
        # list when the declaration did not end in exactly `])` and captured the
        # surrounding prose as if it were action names. An extractor that can run
        # away is one that can also capture a body that happens to look valid.
        match = re.search(r"KNOWN_ACTIONS\s*=\s*new Set<[^>]*>\(\[([^\]]*)\]", src)
        assert match is not None, f"could not find KNOWN_ACTIONS in {self.FRONTEND}"
        return set(re.findall(r"['\"]([^'\"]+)['\"]", match.group(1)))

    def test_the_frontend_source_is_readable_and_parses(self):
        """Positive control, so the comparison below cannot pass vacuously.

        Without this, a renamed constant or a reshaped declaration would yield an
        EMPTY set on the frontend side, and ``empty == empty`` would report
        agreement while pinning nothing at all.
        """
        assert self.FRONTEND.is_file(), f"{self.FRONTEND} is missing"
        parsed = self._frontend_actions()
        assert parsed, "parsed an EMPTY action set from the frontend — the regex is stale"
        assert "close" in parsed

    def test_the_backend_set_equals_the_frontend_set(self):
        assert set(_KNOWN_OPTION_ACTIONS) == self._frontend_actions()

    def test_the_backend_set_is_not_empty(self):
        # Guards the other direction of the same vacuity: an emptied backend set
        # would make every marker unknown and silently stop counting options.
        assert _KNOWN_OPTION_ACTIONS


class TestTableProtocolPrefixes:
    """A marker line must never be absorbed as a GFM table body row.

    An action body is pipe-separated (``close=Ship it | close=Hold``), which is
    exactly the shape a table one line above swallows as a row — so a marker
    directly after a table rendered the user's action as a card. The literal list
    here knew only ``"[OPTIONS"``, and that is NOT a prefix of ``"[OPTION-ACTIONS"``
    (they diverge at ``S`` vs ``-``), so the omission was silent.
    """

    def test_every_marker_head_is_a_protocol_prefix(self):
        for head in MARKER_PREFIXES:
            assert starts_with_marker_head(head)

    def test_an_action_marker_line_is_not_a_table_row(self):
        assert starts_with_marker_head("[OPTION-ACTIONS: close=Ship it | close=Hold]")

    def test_a_mixed_case_action_marker_line_is_not_a_table_row(self):
        """The head is matched case-INSENSITIVELY, as its own pattern is.

        A lowercase line carries the same pipes, so a case-sensitive
        ``startswith`` let it through and the table above absorbed the user's
        action as a body row.
        """
        assert starts_with_marker_head("[option-actions: close=Ship it | close=Hold]")
        assert starts_with_marker_head("[Option-Actions: close=Ship it]")

    def test_a_lowercase_content_marker_line_is_still_a_row_candidate(self):
        """The content head stays case-SENSITIVE — the divergence is deliberate.

        Pinned so a later widening of ``[OPTIONS:`` shows up here rather than
        silently changing how every channel parses a pre-existing marker.
        """
        assert not starts_with_marker_head("[options: Alpha | Bravo]")

    def test_a_content_marker_line_is_not_a_table_row(self):
        assert starts_with_marker_head(CONTENT_MARKER)

    def test_a_steering_line_is_not_a_table_row(self):
        # `[STEERING` is not a marker head, so it is the CALL SITE's literal now --
        # assert the behaviour that actually ships rather than the helper's old vararg.
        assert not _is_row_candidate("[STEERING: a | b]")

    def test_a_genuine_table_row_is_untouched(self):
        """The negative control — the guard must still let real rows through."""
        assert _is_row_candidate("| a genuine | table row |")
        assert not starts_with_marker_head("| a genuine | table row |")


class TestMixedCasePartialMarkerIsDetachedBeforeALengthSplit:
    """A half-arrived MIXED-CASE action marker must be detached, not chunked.

    The action patterns carry ``re.IGNORECASE``, so ``[option-actions: close=X]``
    IS a marker the parser strips. The two scans that answer "is the tail an
    UNFINISHED marker?" used a case-SENSITIVE ``rfind``, so they missed a
    mixed-case fragment: it stayed in the length-split path, a rotation cut it
    mid-marker, and the surface sealed the halves as raw protocol text — permanent
    on any channel that cannot edit a sent message.

    Each case below FAILS on a case-sensitive scan: the fragment is returned as
    visible body instead of being pulled into the suffix. A test that could not
    fail that way would prove nothing about a silent leak.
    """

    PARTIALS = (
        "[option-actions: close=Sh",
        "[Option-Actions: close=Sh",
        "[OPTION-actions: close=Sh",
    )

    def test_split_trailing_protocol_suffix_detaches_a_mixed_case_partial(self):
        for tail in self.PARTIALS:
            visible, suffix = split_trailing_protocol_suffix(f"{BODY}\n{tail}")
            assert suffix.strip() == tail, f"{tail!r} was left in the length-split path"
            assert tail not in visible, f"{tail!r} left the fragment in the visible body"

    def test_hide_partial_hides_a_mixed_case_half_arrived_action_marker(self):
        for tail in self.PARTIALS:
            body, choices = split_options_trailer(f"{BODY}\n{tail}", hide_partial=True)
            assert body == BODY, f"{tail!r} was rendered as raw text in the live frame"
            assert choices == []

    def test_a_completed_mixed_case_marker_is_stripped_not_left_raw(self):
        """The control: a CLOSED mixed-case marker is COMPLETE, so it is removed
        outright rather than held back as a fragment — and no body text goes with
        it. This is what makes the partial branch's job narrow."""
        body, choices = split_options_trailer(
            f"{BODY}\n[option-actions: close=Shut it]", hide_partial=True
        )
        assert body == BODY
        assert choices == []

    def test_prose_with_no_marker_head_is_untouched(self):
        """The negative control — the scan must not cut ordinary prose."""
        prose = f"{BODY}\nI mentioned options and actions but wrote no marker."
        assert split_trailing_protocol_suffix(prose) == (prose, "")
        assert split_options_trailer(prose, hide_partial=True) == (prose, [])

    def test_the_lowercase_content_head_is_still_case_sensitive(self):
        """``[options:`` stays case-sensitive, a pre-existing deliberate
        divergence. Pinned so widening it cannot happen silently."""
        text = f"{BODY}\n[options: Alpha | Brav"
        assert split_trailing_protocol_suffix(text) == (text, "")


class TestMarkerHeadOffsetAddressesTheOriginalString:
    """A head offset must index the string the caller SLICES.

    ``str.lower`` is not length-preserving: ``"İ"`` (U+0130) lowers to two
    codepoints. A scan that folded the haystack and returned that index, while the
    caller sliced the ORIGINAL, cut one position late per expanding character
    before the head — leaking a marker fragment into a message already sent.

    Each case below FAILS on a folded-haystack scan.
    """

    EXPANDING = "\u0130"  # LATIN CAPITAL LETTER I WITH DOT ABOVE

    def test_settle_marker_hold_cuts_at_the_right_place_after_expanding_text(self):
        """The SITE, not just the helper: this is the function that SLICES.

        An UNCLOSED head is the branch that returns ``residue[:head_at]``. With a
        folded-haystack scan the offset is one position late per expanding
        character before the head, so the cut lands inside the marker and leaves a
        bracket fragment visible in a Slack bubble already sent.
        """
        prose = f"{self.EXPANDING}{self.EXPANDING} still here "
        for fragment in (
            "[OPTION-ACTIONS: close=x",
            "[option-actions: close=x",
            "[OPTIONS: a | b",
        ):
            settled = settle_marker_hold(f"{prose}{fragment}")
            assert "[" not in settled, f"{fragment!r} left a bracket fragment: {settled!r}"
            assert settled == prose, f"{fragment!r} moved the cut: {settled!r}"

    def test_lower_really_is_length_expanding(self):
        """The positive control: without this the tests above prove nothing."""
        assert len(self.EXPANDING.lower()) == 2
        assert len(self.EXPANDING) == 1


class TestHasOptionsSplit:
    def test_parse_options_never_returns_an_action_label(self):
        assert _parse_options(ACTION_MARKER) == []
        assert _has_option_actions(ACTION_MARKER) is True

    def test_has_option_actions_is_false_for_a_content_marker(self):
        assert _has_option_actions(CONTENT_MARKER) is False
        assert _parse_options(CONTENT_MARKER) == ["Alpha", "Beta"]

    def test_action_only_row_reports_options_on_screen_but_no_payload(self):
        """The whole point of the split.

        ``has_options`` is the STATUS fact — a button IS on screen, so the row
        must not be reported as idle-awaiting-a-prompt. ``options`` is the
        PAYLOAD channel button builders consume, and an action label must never
        enter it: the ``close`` action is a local dashboard operation, so a
        channel button carrying that label could not work, and its free text
        would be echoed back into the session on click.
        """
        s = _slot({"role": "assistant", "content": f"{BODY}\n{ACTION_MARKER}", "ts": "t1"})
        d = s.to_dict()
        assert d["has_options"] is True
        assert d["options"] == []
        assert d["waiting_for_input"] is False

    def test_action_only_row_preview_carries_no_raw_marker(self):
        s = _slot({"role": "assistant", "content": f"{BODY}\n{ACTION_MARKER}", "ts": "t1"})
        d = s.to_dict()
        assert ACTION_HEAD not in d["prompt_preview"]
        assert "close=" not in d["prompt_preview"]
        assert ACTION_HEAD not in d["last_message"]

    @pytest.mark.parametrize(
        "marker",
        [
            "[OPTION-ACTIONS: ]",
            "[OPTION-ACTIONS: close=]",
            "[OPTION-ACTIONS: close=   ]",
            "[OPTION-ACTIONS: reboot=Restart now]",
            "[OPTION-ACTIONS: close]",
            "[OPTION-ACTIONS: =Nothing else]",
        ],
    )
    def test_a_marker_that_renders_no_chip_is_not_choices_on_screen(self, marker: str):
        """Grammar-valid is not the same as renderable, and the gap stranded a tab.

        The frontend drops an entry whose action is outside the enum, whose label
        is empty, or that carries no ``=``. Keying ``has_options`` on the regex
        alone reported these as choices and so suppressed ``waiting_for_input``:
        the session sat in the waiting lane with no button to press and nothing
        asking for a turn, which the user reads as a tab that looks answered and
        is not.
        """
        assert _has_option_actions(marker) is False

    def test_an_unrenderable_marker_leaves_the_row_waiting_for_input(self):
        """The consequence, asserted at the surface the user actually sees."""
        s = _slot(
            {"role": "assistant", "content": f"{BODY}\n[OPTION-ACTIONS: reboot=Go]", "ts": "t1"}
        )
        d = s.to_dict()
        assert d["has_options"] is False
        assert d["options"] == []
        assert d["waiting_for_input"] is True

    def test_the_LAST_marker_decides_mirroring_the_frontend(self):
        """A valid marker followed by a broken one renders nothing on screen.

        The frontend derives its chips from the last marker only, so an earlier
        valid one must not keep the row out of the waiting lane here either.
        """
        assert _has_option_actions(f"{ACTION_MARKER}\n[OPTION-ACTIONS: reboot=Go]") is False
        assert _has_option_actions(f"[OPTION-ACTIONS: reboot=Go]\n{ACTION_MARKER}") is True

    def test_a_label_may_contain_an_equals_sign(self):
        """Split on the FIRST ``=`` only — the rest belongs to the label."""
        assert _has_option_actions("[OPTION-ACTIONS: close=Set a=b and move on]") is True

    def test_one_valid_entry_among_invalid_ones_still_counts(self):
        """A chip WILL render, so the row genuinely has choices on screen."""
        assert _has_option_actions("[OPTION-ACTIONS: reboot=Go | close=Close it]") is True

    def test_both_markers_row(self):
        s = _slot(
            {
                "role": "assistant",
                "content": f"{BODY}\n{CONTENT_MARKER}\n{ACTION_MARKER}",
                "ts": "t1",
            }
        )
        d = s.to_dict()
        assert d["has_options"] is True
        assert d["options"] == ["Alpha", "Beta"]
        assert ACTION_HEAD not in d["prompt_preview"]
        assert "[OPTIONS:" not in d["prompt_preview"]

    def test_content_only_row_regression(self):
        s = _slot({"role": "assistant", "content": f"{BODY}\n{CONTENT_MARKER}", "ts": "t1"})
        d = s.to_dict()
        assert d["has_options"] is True
        assert d["options"] == ["Alpha", "Beta"]
        assert d["waiting_for_input"] is False

    def test_no_marker_row_still_waits_for_input(self):
        """Negative control: the suppression must be caused by the marker."""
        s = _slot({"role": "assistant", "content": BODY, "ts": "t1"})
        d = s.to_dict()
        assert d["has_options"] is False
        assert d["options"] == []
        assert d["waiting_for_input"] is True


def test_no_hand_rolled_options_literal_remains_in_voice_reply():
    """``voice_reply`` had a private copy of the grammar; it must stay gone.

    A local copy is how this surface came to know only one head, stop at the first
    ``]``, and accept no CJK closer — and TTS is the one surface where the leak is
    SPOKEN rather than merely displayed.
    """
    import kiro_crew.voice_reply as vr

    source = re.sub(r"#.*", "", (vr.__file__ and open(vr.__file__).read()) or "")
    assert r"\[OPTIONS:" not in source


class TestSomethingActuallyEmitsTheMarker:
    """A producer exists: the dashboard prompt TEACHES the marker.

    Every other test in this file pins how the marker is parsed, stripped and
    rendered once it exists. None of them can tell whether anything ever emits
    one — a pipeline with zero producers passes all of them and still cannot
    fire in production, because no agent is told the syntax and models keep
    emitting a plain ``[OPTIONS:]`` entry for "close this tab", which costs the
    very LLM turn this feature exists to avoid.

    The teaching is asserted through the SHIPPED PARSER rather than by matching a
    string, because the failure mode that matters is a prompt that teaches a
    layout the parser drops. ``OPTION_ACTIONS_RE_LINE`` requires the marker to
    end its line, so a taught example with prose after it would parse to nothing
    while every string-match assertion here still passed.
    """

    def test_the_dashboard_block_teaches_the_action_marker(self):
        from kiro_crew import context

        assert "[OPTION-ACTIONS:" in context._CRITICAL_RULES

    def test_positive_control_the_probe_can_see_taught_marker_syntax(self):
        """The assertion above must be able to FAIL for the intended reason.

        If the block were renamed, emptied, or restructured so the teaching text
        no longer reached this constant, the assertion above would fail — but so
        would an assertion about the marker that has been taught here all along.
        Pinning the OLD marker's teaching in the same constant proves the probe
        reads real prompt text, so a failure above means "the action teaching is
        gone", not "this test lost its grip on the prompt".
        """
        from kiro_crew import context

        assert "[OPTIONS:" in context._CRITICAL_RULES

    def test_the_taught_example_actually_parses(self):
        """The example in the prompt must survive the real extractor.

        Feeds every line of the teaching that contains the marker through
        ``OPTION_ACTIONS_RE_LINE`` and requires at least one to yield a KNOWN
        action with a non-empty label — the same two conditions the dispatcher
        applies. A prompt whose example is unparseable is a producer in name
        only.
        """
        from kiro_crew import context

        taught = [
            line
            for line in context._OPTION_ACTIONS_RULE_DASHBOARD.splitlines()
            if "[OPTION-ACTIONS:" in line
        ]
        assert taught, "the teaching names no marker at all"

        parsed: list[tuple[str, str]] = []
        for line in taught:
            for match in OPTION_ACTIONS_RE_LINE.finditer(line):
                for entry in match.group(1).split("|"):
                    action, _, label = entry.partition("=")
                    if action.strip().lower() in _KNOWN_OPTION_ACTIONS and label.strip():
                        parsed.append((action.strip().lower(), label.strip()))
        assert parsed, (
            "no line of the teaching yields a dispatchable action — the taught "
            f"syntax does not survive OPTION_ACTIONS_RE_LINE: {taught!r}"
        )

    def test_the_channel_block_does_NOT_teach_it(self):
        """The split is the point, not an accident of where the text landed.

        The chip renders only on the dashboard, and every channel renderer in
        this file's other tests strips the marker and drops it. Teaching it to a
        channel session would spend tokens on an instruction whose only possible
        outcome is a dropped marker, so the shared tail must NOT carry it.
        """
        from kiro_crew import context

        assert "[OPTION-ACTIONS:" not in context._CRITICAL_RULES_CHANNEL
        assert "[OPTION-ACTIONS:" not in context._CRITICAL_RULES_TAIL

    def test_the_runtime_selector_delivers_it_to_the_dashboard_only(self):
        """Pins the INJECTION PATH, not just the constants.

        Both assertions above would still hold if ``_critical_rules_for`` handed
        the channel block to a dashboard session, which would leave the feature
        unteachable through the only route that reaches a live turn.
        """
        from kiro_crew import context

        dashboard = context._critical_rules_for("dashboard_1234", None)
        channel = context._critical_rules_for("slack_C123", None)
        assert "[OPTION-ACTIONS:" in dashboard
        assert "[OPTION-ACTIONS:" not in channel
        # Positive control on the selector itself: prove these two calls really
        # resolved to DIFFERENT blocks, so the pair above cannot pass by both
        # returning the same string.
        assert dashboard != channel


# ── 15. The streaming hold and the batch parser agree on CASING ──────────────


class TestStreamingHoldMatchesTheParserPerHead:
    """The stream must hold exactly the heads the batch parser strips — no more.

    Holding MORE is not harmless caution. ``settle_marker_hold`` excises the held run
    from the LIVE bubble, and then ``stop_stream`` replaces that bubble with text
    derived from the case-SENSITIVE ``extract_options``. So a head the stream holds
    but the parser declines to strip disappears mid-turn and reappears as raw
    protocol text when the final message lands — a visible pop-in that did not happen
    before the streaming helpers existed.
    """

    @staticmethod
    def _rendered(text: str) -> str:
        """What the reader ends up seeing, live bubble then settle."""
        hold, buf = _filter_options_brackets(text, "", "")
        return (buf + settle_marker_hold(hold)).strip()

    @staticmethod
    def _parser_strips(text: str) -> bool:
        """Whether the authoritative LINE patterns remove anything."""
        stripped = OPTION_ACTIONS_RE_LINE.sub("", OPTIONS_RE_LINE.sub("", text))
        return stripped.strip() != text.strip()

    @pytest.mark.parametrize(
        "text",
        [
            "[options: a | b]",
            "[Options: a | b]",
            "[OPTIONS: a | b]",
            "[option-actions: close=Close]",
            "[Option-Actions: close=Close]",
            "[OPTION-ACTIONS: close=Close]",
        ],
    )
    def test_the_stream_holds_exactly_what_the_parser_strips(self, text):
        """The invariant itself, over both heads in three casings each.

        Asserted as an EQUIVALENCE against the parser rather than against a
        hand-written expected string, so the two can never drift apart: whichever
        way a casing rule changes, this fails unless both sides changed together.
        """
        assert (self._rendered(text) == "") is self._parser_strips(text)

    def test_a_lowercase_content_marker_is_released_not_held(self):
        """Finding 1, stated directly. Fails on the pre-fix helpers.

        Before the fix both helpers folded every head to lower, so this was held and
        excised, then re-inserted verbatim by the final message.
        """
        assert self._rendered("[options: a | b]") == "[options: a | b]"
        assert not self._parser_strips("[options: a | b]")

    @pytest.mark.parametrize(
        "text",
        [
            "[option-actions: close=Close]",
            "[Option-Actions: close=Close]",
            "[OPTION-ACTIONS: close=Close]",
        ],
    )
    def test_the_action_marker_still_holds_in_any_casing(self, text):
        """The other direction — the fix must not narrow the ACTION head.

        Without this the finding could be "fixed" by making both heads
        case-sensitive, which would leak a lowercase action marker into the bubble.
        """
        assert self._rendered(text) == ""

    def test_the_two_heads_really_do_differ_in_casing_rule(self):
        """Positive control on the premise, read from the shipped predicate.

        If both heads ever carried the same rule, every assertion above could pass
        for the wrong reason.
        """
        assert marker_prefix_is_case_insensitive("[OPTION-ACTIONS") is True
        assert marker_prefix_is_case_insensitive("[OPTIONS") is False


# ── 16. Every channel strip site covers BOTH heads ──────────────────────────


class TestEveryChannelStripSiteCoversBothHeads:
    """Finding 2: the WhatsApp renderer was the one site still single-head.

    An action marker is inert on a transport that cannot render a chip — but inert
    has to mean STRIPPED, not passed through, or the "inert" claim shows the user raw
    protocol text.
    """

    ACTION = "[OPTION-ACTIONS: close=Close]"

    @pytest.mark.parametrize(
        "text",
        [
            f"Body {ACTION}",
            f"Body [OPTIONS: a | b]\n{ACTION}",
            f"Body {ACTION}\n[OPTIONS: a | b]",
            f"Body {ACTION}\nand more prose",
        ],
    )
    def test_whatsapp_strips_the_action_marker(self, text):
        """Fails on the pre-fix renderer, which only ran ``OPTIONS_RE_TRAILER``."""
        assert "OPTION-ACTIONS" not in _strip_options(text)

    @pytest.mark.parametrize(
        "text",
        [
            f"Body {ACTION}",
            f"Body [OPTIONS: a | b]\n{ACTION}",
            f"Body {ACTION}\n[OPTIONS: a | b]",
            f"Body {ACTION}\nand more prose",
            f"Body {ACTION} and more prose",
        ],
    )
    def test_whatsapp_agrees_with_its_sibling_site(self, text):
        """Pins the SHAPE, not just the outcome.

        The last case is the deliberate exception both sides share: the LINE tail
        requires the marker to end its line, so a marker mid-sentence is prose. That
        is why this asserts agreement with the sibling rather than absence — an
        absence assertion would demand behaviour the established shape does not have,
        and "fixing" it here would make WhatsApp the odd site out again.
        """
        sibling, _choices = split_options_trailer(text)
        assert _strip_options(text) == sibling.strip()

    def test_the_content_marker_is_still_stripped(self):
        """Negative control: the fix must not cost the pre-existing strip."""
        assert _strip_options("Body [OPTIONS: a | b]") == "Body"

    def test_no_strip_site_is_left_single_head(self):
        """A ratchet, so a fourth renderer cannot reintroduce the same gap.

        Keyed on the file set rather than one path: any module consulting the
        content trailer must also consult the action pattern.

        ``strip_action_markers`` COUNTS as consulting it, and is the form a new site
        should reach for: it wraps the same pattern but refuses a span nested in an
        unclosed marker, so it cannot delete text the reader is meant to see. The raw
        names stay accepted because a matching-only site has nothing to guard.
        """
        consults_actions = (
            "OPTION_ACTIONS_RE_LINE",
            "OPTION_ACTIONS_RE_TRAILER",
            "strip_action_markers",
            "match_action_markers",
        )
        root = pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        offenders = []
        for path in root.rglob("*.py"):
            body = path.read_text(encoding="utf-8")
            if "OPTIONS_RE_TRAILER.sub" not in body:
                continue
            if not any(token in body for token in consults_actions):
                offenders.append(str(path.relative_to(root)))
        assert offenders == [], f"single-head strip site(s): {offenders}"

    def test_the_ratchet_can_actually_fail(self):
        """Positive control: prove the scan above is able to find an offender.

        Without this, a glob that silently matched nothing would report a clean
        sweep, which is exactly the false all-clear this finding was.
        """
        root = pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        consulting = [
            p
            for p in root.rglob("*.py")
            if "OPTIONS_RE_TRAILER.sub" in p.read_text(encoding="utf-8")
        ]
        assert consulting, "scan found no strip site at all — the query is broken"


class TestMarkerStripBodyIsLinearTime:
    """The strip body must not backtrack exponentially on malformed model output.

    ``_MARKER_BODY_STRIP`` once carried ONE bracketed-span branch whose closer was
    OPTIONAL. That let a run of plain characters after a ``[`` be divided between the
    span and the single-character branch in any proportion, and the choice multiplies
    per ``[`` — so a string of repeated ``[x`` fragments cost exponential time.

    This matters because of WHERE it runs: the sole caller is
    ``voice_reply.strip_markdown``, on the gateway's own event loop, over text a model
    produced. Malformed output is not a hypothetical there — it is the normal failure
    mode of the thing generating the input.
    """

    #: A marker head, then n repeated ``[x`` fragments, and NO closer anywhere. The
    #: missing closer is the whole point: it forces the overall match to FAIL, and only
    #: a failing match makes the engine enumerate every partition rather than stopping
    #: at the first one that works.
    @staticmethod
    def _pathological(n: int) -> str:
        return "[OPTIONS: " + "[x" * n + "!"

    #: The body as it stood BEFORE the fix. Reconstructed here rather than imported,
    #: because the point of the fix is that it no longer exists in the module.
    _SUPERSEDED_BODY = rf"(?:[^[\]\n]|{_TEMPER}[^[\]\n]*{_MARKER_CLOSE_CLASS}?)*"

    @classmethod
    def _build(cls, body: str) -> re.Pattern[str]:
        """Assemble the full strip pattern around a body, as ``constants`` does."""
        return re.compile(
            rf"\[(?:{_MARKER_HEAD_ALT}){body}{_MARKER_CLOSE_CLASS}" r"(?:\([^\s()]*\))?",
            re.IGNORECASE,
        )

    def test_pathological_marker_body_strips_in_bounded_time(self):
        text = self._pathological(5000)

        started = time.perf_counter()
        MARKER_STRIP_ANYWHERE_RE.sub("", text)

        assert time.perf_counter() - started < 1.0

    def test_the_superseded_body_blows_up_on_the_same_input(self):
        """NEGATIVE CONTROL for the bound above.

        Without this, the timing assertion would pass just as happily against a
        pattern that never had the defect, so it would prove nothing about having
        fixed one. n=22 rather than 5000 because the superseded form has to actually
        COMPLETE here for the comparison to be measurable.
        """
        text = self._pathological(22)

        started = time.perf_counter()
        self._build(self._SUPERSEDED_BODY).sub("", text)
        superseded_elapsed = time.perf_counter() - started

        started = time.perf_counter()
        for _ in range(100):
            MARKER_STRIP_ANYWHERE_RE.sub("", text)
        current_elapsed = (time.perf_counter() - started) / 100

        assert superseded_elapsed > 0.1, (
            "the superseded body completed too fast to be a control — either the "
            "reconstruction above no longer matches what was replaced, or this "
            f"machine is fast enough to need a larger n (got {superseded_elapsed:.4f}s)"
        )
        assert current_elapsed * 100 < superseded_elapsed

    def test_the_fix_did_not_change_which_text_is_stripped(self):
        """Equivalence with the superseded form, asserted rather than argued.

        A faster expression that strips DIFFERENT text is a regression, not a fix —
        and this file already records two measured defects in this exact area. The
        corpus deliberately includes the unbalanced-inner-bracket case that the old
        optional closer was there to serve, since that is what a careless rewrite
        (simply making the closer required) silently loses.
        """
        superseded = self._build(self._SUPERSEDED_BODY)
        corpus = [
            "See [OPTIONS: A | B] for details [1]",
            "See [OPTIONS: A | B] for details",
            "[OPTIONS: do [x] now]",
            "[OPTIONS: a [b] c] tail",
            "[OPTIONS: do [x now]",
            "[OPTIONS: a [b [c]",
            "[OPTIONS: a [b [c] d]",
            "[OPTIONS: plain]",
            "[OPTION-ACTIONS: close=A]",
            "[Option-Actions: close=A]",
            "[OPTIONS: A] [OPTIONS: B]",
            f"[OPTIONS: a{MARKER_CLOSERS[1]}",
            "[OPTIONS: a](b)",
            "[OPTIONS: a\nb]",
            "[OPTIONS: a",
            "[NOTAHEAD: a]",
        ]

        for text in corpus:
            assert MARKER_STRIP_ANYWHERE_RE.sub("", text) == superseded.sub("", text), text

        rewritten = sum(1 for t in corpus if MARKER_STRIP_ANYWHERE_RE.sub("", t) != t)
        assert rewritten >= 8, (
            "positive control: the corpus barely exercises the strip, so agreeing "
            f"with the superseded form proves little (only {rewritten} rewritten)"
        )


# ── 9. Every strip site keeps a nested malformed marker ──────────────────────


class TestEveryStripSiteKeepsANestedMalformedMarker:
    """The strip sites must ALL use the guarded stripper, not the raw pattern.

    ``strip_action_markers`` was added with the dashboard offer/strip pair, but the
    five channel-facing strip sites kept calling ``OPTION_ACTIONS_RE_LINE.sub`` and so
    still excised a span the matcher refuses. The consequence is not cosmetic: the
    malformed syntax is the reader's ONLY cue that a marker was intended, and these
    paths are the ones a human actually reads — a Slack post, a WhatsApp turn, the
    sidebar preview. Deleting it there loses information with no way to recover it.

    Parametrized over the shipped entry points rather than over the helper, because
    the defect was never in the helper: it was in who called it. A test of
    ``strip_action_markers`` alone passed throughout and proved nothing about these.
    """

    #: One fixture, shared with the class that pins the helper itself, so the two
    #: cannot drift into disagreeing about what "nested and malformed" means.
    NESTED = TestNestedInsideUnclosedMarker.NESTED

    #: Well-formed for the paired control: every site MUST still strip this.
    GENUINE = f"{CONTENT_MARKER} {ACTION_MARKER}"

    @staticmethod
    def _sites():
        """The shipped entry points, each reduced to text-in / text-out."""
        return {
            "messaging.renderer.split_options_trailer": lambda t: split_options_trailer(t)[0],
            "slack.format.extract_options": lambda t: extract_options(t)[0],
            "slack.handler._strip_line_markers": _strip_line_markers,
            "preview_text.strip_markdown_preview": strip_markdown_preview,
            "whatsapp.turn_renderer._strip_options": _strip_options,
        }

    def test_the_matcher_refuses_the_fixture(self):
        """Guard the premise: if this span were a real marker, stripping is correct."""
        assert match_action_markers(self.NESTED) == []
        assert OPTION_ACTIONS_RE_LINE.search(self.NESTED) is not None

    @pytest.mark.parametrize("name", list(_sites.__func__()))
    def test_the_nested_span_survives_every_site(self, name):
        out = self._sites()[name](self.NESTED)
        assert "OPTION-ACTIONS" in out.upper(), (
            f"{name} deleted a span the matcher refuses; the malformed text is the "
            f"reader's only cue a marker was meant. got {out!r}"
        )

    @pytest.mark.parametrize("name", list(_sites.__func__()))
    def test_POSITIVE_CONTROL_a_genuine_marker_is_still_stripped(self, name):
        """Without this, the assertions above would pass on a site that strips NOTHING."""
        out = self._sites()[name](self.GENUINE)
        assert "OPTION-ACTIONS" not in out.upper(), (
            f"{name} stopped stripping a well-formed marker, which would leak the "
            f"raw protocol text. got {out!r}"
        )


class TestSettleMarkerHoldIsLinear:
    """The streaming twin of the batch-path linearization, measured the same way.

    ``_unclosed_marker_flags`` already carries this defect's batch-path history: one
    long line with many markers cost O(n*k) and stalled the gateway event loop for
    1.6 seconds. The streaming hold reached the same shape by a different route --
    the excision re-derived the whole string per span -- and on the streaming side
    the input is worse, because a hold grows until a newline arrives and the model
    decides when that is.
    """

    #: A single streamed LINE carrying *n* complete markers with prose between them.
    @staticmethod
    def _held_line(n: int) -> str:
        return "[OPTIONS: a] y " * n

    #: The excision as it stood BEFORE the fix: one slice-and-concat per span, each of
    #: them O(n). Reconstructed here rather than imported, because the point of the fix
    #: is that this shape no longer exists in the module.
    @staticmethod
    def _superseded(residue: str) -> str:
        while True:
            match = _MARKER_PREFIX_SCAN_RE.search(residue)
            head_at = None if match is None else match.start()
            if head_at is None:
                return residue
            close_at = next(
                (i for i in range(head_at, len(residue)) if residue[i] in MARKER_CLOSERS),
                None,
            )
            if close_at is None:
                return residue[:head_at]
            residue = residue[:head_at] + residue[close_at + 1 :]

    def test_the_held_line_actually_reaches_the_excision(self):
        """POSITIVE CONTROL for both timing tests below.

        The markers in the fixture are COMPLETE, so the obvious reading is that
        ``_strip_line_markers`` removes them and nothing reaches the excision at all.
        It does not: the LINE patterns are anchored, so a marker sitting mid-line
        survives. Pinned here because if that ever changed, the timing tests would
        keep passing while measuring an empty string.
        """
        held = self._held_line(50)
        residue = _strip_line_markers(held)

        assert len(residue) == len(held), (
            "the line strip consumed this fixture, so the timing assertions below "
            f"would measure an empty string (held {len(held)}, residue {len(residue)})"
        )
        assert residue.count("[OPTIONS:") == 50

    def test_a_long_held_line_settles_in_bounded_time(self):
        held = self._held_line(16000)

        started = time.perf_counter()
        settle_marker_hold(held)

        assert time.perf_counter() - started < 1.0

    def test_the_superseded_excision_blows_up_on_the_same_input(self):
        """NEGATIVE CONTROL for the bound above.

        Without this, that timing assertion would pass just as happily against an
        implementation that never had the defect, so it would prove nothing about
        having fixed one. n is smaller here than in the bound test because the
        superseded form has to actually COMPLETE for the comparison to be measurable.
        """
        residue = _strip_line_markers(self._held_line(4000))

        started = time.perf_counter()
        self._superseded(residue)
        superseded_elapsed = time.perf_counter() - started

        started = time.perf_counter()
        for _ in range(10):
            excise_marker_spans(residue)
        current_elapsed = (time.perf_counter() - started) / 10

        assert superseded_elapsed > 0.05, (
            "the superseded excision completed too fast to be a control — either the "
            "reconstruction above no longer matches what was replaced, or this "
            f"machine needs a larger n (got {superseded_elapsed:.4f}s)"
        )
        assert current_elapsed * 5 < superseded_elapsed

    @pytest.mark.parametrize(
        "hold",
        [
            "",
            "plain text with no markers at all",
            "[OPTIONS: a | b] bye",
            "hello [OPTIONS: a",
            # The SEAM case: excising the inner marker joins `[OPTI` to `ONS: b]`, so a
            # head appears that neither side of the cut contained.
            "[OPTI[OPTIONS: a]ONS: b]",
            "[option-actions: close=x] tail",
            "[OPTION-ACTIONS: close=x]",
            "] [OPTIONS: a] after",
            "[OPTIONS: a\u3011 cjk closer",
            # `str.lower` is not length-preserving for U+0130, so an implementation that
            # folded the haystack would cut at the wrong offset here.
            "\u0130[OPTIONS: a] expanding codepoint first",
            "[OPTIONS: one] mid [OPTIONS: two] end",
            "[OPTIONS: unclosed [OPTION-ACTIONS: close=y]",
            "[OPTIONS",
            "[OPTION-ACTI",
            "text ][OPTIONS: x] more",
        ],
    )
    def test_the_fix_did_not_change_what_is_excised(self, hold: str):
        """Equivalence with the superseded form, asserted rather than argued.

        A faster excision that keeps DIFFERENT text is a regression, not a fix: this
        function decides what a live bubble shows, so keeping too much leaks raw
        protocol text and keeping too little eats the user's sentence.
        """
        assert excise_marker_spans(hold) == self._superseded(hold)


class TestNestedActionSuppressionMatchesTheFrontend:
    """A nested action the FRONTEND refuses must not raise ``waiting_for_input`` here.

    The two sides decide independently whether an action nested inside an unclosed head
    is a marker. The frontend's ``HEAD_RE`` (``optionMarker.ts``) is
    ``\\[(?:OPTION-ACTIONS:|OPTIONS?:)`` with the ``i`` flag — singular OR plural, either
    casing. The backend's scan was the marker-grammar alternation, which is plural-only
    and case-SENSITIVE for the content head, so it accepted actions the frontend refused
    and the turn then waited on a chip that never rendered.

    Widening the SUPPRESSION scan is not widening the marker grammar: this predicate is
    only ever asked about ACTION offsets, and the action head has no base occurrences, so
    no pre-existing ``[OPTIONS:]`` marker changes how it parses. That separation is what
    the sibling characterization corpus keeps honest.
    """

    #: Heads that must SUPPRESS a nested action, in the frontend's own spellings.
    SUPPRESSING = [
        "[OPTIONS:",
        "[options:",
        "[OPtIoNS:",
        "[OPTION:",
        "[option:",
        "[OPTION-ACTIONS:",
        "[option-actions:",
    ]

    @pytest.mark.parametrize("head", SUPPRESSING)
    def test_an_unclosed_head_suppresses_the_nested_action(self, head: str):
        text = f"{head} broken {ACTION_MARKER}"

        assert match_action_markers(text) == [], (
            f"{head!r} left unclosed did not suppress the nested action, so the backend "
            "would raise waiting_for_input for a chip the frontend refuses to render"
        )
        assert _has_option_actions(text) is False

    @pytest.mark.parametrize("head", SUPPRESSING)
    def test_a_suppressed_span_stays_visible(self, head: str):
        """PAIRED with the matcher: a span refused as a marker must not be stripped.

        Otherwise the one cue that a marker was intended is deleted from the text.
        """
        text = f"{head} broken {ACTION_MARKER}"
        assert strip_action_markers(text) == text

    def test_a_CLOSED_head_does_not_suppress_the_sibling(self):
        """The control that keeps the widening honest.

        Without it, a scan that suppressed on ANY head occurrence would pass every
        assertion above while silently killing every legitimate action marker that
        follows a normal, closed `[OPTIONS:]` block.
        """
        for head in ("[OPTIONS: A | B]", "[options: A | B]", "[OPTION: A]"):
            text = f"{head}\n{ACTION_MARKER}"
            assert (
                len(match_action_markers(text)) == 1
            ), f"a CLOSED {head!r} wrongly suppressed the action that follows it"
            assert _has_option_actions(text) is True

    def test_the_content_grammar_is_NOT_widened(self):
        """The separation this fix rests on, asserted rather than argued.

        Suppression is case-insensitive and knows the singular head; the CONTENT marker
        grammar stays case-sensitive and plural-only, so no shipped marker re-parses.
        """
        assert OPTIONS_RE_LINE.search("[OPTIONS: A | B]") is not None
        assert (
            OPTIONS_RE_LINE.search("[options: A | B]") is None
        ), "content head went case-insensitive"
        assert (
            OPTIONS_RE_LINE.search("[OPTION: A]") is None
        ), "singular head became a content marker"
        # ...and the ANYWHERE strip, which feeds speech and previews, is untouched too.
        assert (
            MARKER_STRIP_ANYWHERE_RE.search("[OPTION: Single]") is None
        ), "the singular head became strippable, which would change what TTS reads aloud"


class TestNestedSiblingActionIsCountedByDepth:
    """A sibling action after a BALANCED nested pair, inside an open head, is not a marker.

    The pairwise "last closer before last head" test could not see this: the first nested
    pair supplies BOTH the last head and the last closer, so it reads as closed and the
    SECOND action was accepted while the outer head was still open. The frontend twin
    (``optionMarkerNestingDepth.test.ts``) pins the identical set, because a chip the
    frontend offers and a backend that disagrees is how ``waiting_for_input`` is raised for
    an affordance that never rendered.
    """

    A = "[OPTION-ACTIONS: close=Alpha]"
    B = "[OPTION-ACTIONS: close=Bravo]"

    def test_a_sibling_after_a_balanced_nested_pair_is_refused(self):
        text = f"[OPTIONS: broken {self.A} {self.B}"
        assert match_action_markers(text) == [], (
            "the sibling action after a balanced nested pair was accepted while the outer "
            "[OPTIONS: head was still open -- a chip built out of broken syntax"
        )
        assert _has_option_actions(text) is False

    def test_the_malformed_run_stays_visible(self):
        text = f"[OPTIONS: broken {self.A} {self.B}"
        assert strip_action_markers(text) == text

    def test_a_third_sibling_is_refused_too(self):
        text = f"[OPTIONS: broken {self.A} {self.B} {self.A}"
        assert match_action_markers(text) == []

    def test_two_well_formed_siblings_are_still_ACCEPTED(self):
        """The control: depth must not suppress legitimate sequential markers."""
        text = f"{self.A} {self.B}"
        assert len(match_action_markers(text)) == 2
        assert strip_action_markers(text).strip() == ""

    def test_an_action_after_a_CLOSED_content_marker_is_still_accepted(self):
        assert len(match_action_markers(f"[OPTIONS: A | B] {self.A}")) == 1

    def test_a_stray_closer_does_not_cancel_an_open_head(self):
        """Depth is clamped at zero, or the leading ] offsets the real head."""
        assert match_action_markers(f"] [OPTIONS: broken {self.A}") == []

    def test_an_open_head_does_not_reach_past_its_newline(self):
        assert len(match_action_markers(f"[OPTIONS: broken\n{self.A}")) == 1


class TestTrailingSuffixSplitIsLinear:
    """A model-controlled run of trailing markers must not cost O(n*k).

    The shape this replaced re-sliced the whole prefix and re-ran a ``\\Z``-anchored search
    once per trailing marker. MEASURED on the old code: 76 ms at k=500 growing ~4x per
    doubling (4.9 s at k=4000), so the 16k-marker tail a model can emit clears the 25 s
    watchdog and exits the gateway mid-render. The replacement scans heads once and walks
    backward anchored at each head, which measures ~2x per doubling.
    """

    MARK = "[OPTION-ACTIONS: close=X]"

    def test_a_16k_marker_tail_stays_well_under_the_watchdog(self):
        text = "body text here. " + (self.MARK + "\n") * 16000
        start = time.perf_counter()
        visible, suffix = split_trailing_protocol_suffix(text)
        elapsed = time.perf_counter() - start

        # The whole run belongs to the suffix -- that is the behaviour being preserved.
        assert visible == "body text here. "
        assert suffix.count("[OPTION-ACTIONS:") == 16000
        # Generous vs the ~31 ms measured, and still ~600x under the old shape's ~19 s at
        # HALF this size, so a reintroduced quadratic walk cannot slip past.
        assert elapsed < 3.0, f"took {elapsed:.2f}s -- the quadratic suffix rescan is back"

    def test_growth_is_linear_not_quadratic(self):
        """Doubling the marker count must not quadruple the cost."""

        def cost(k: int) -> float:
            text = "body. " + (self.MARK + "\n") * k
            # MIN of several runs, not a single sample: at these absolute times one GC
            # pause swamps the signal, and a ratio built from single samples fails
            # spuriously (measured 20x on a workload that grows 1.96x).
            best = float("inf")
            for _ in range(5):
                start = time.perf_counter()
                split_trailing_protocol_suffix(text)
                best = min(best, time.perf_counter() - start)
            return best

        cost(500)  # warm the pattern cache so the first timing is not the outlier
        small, large = cost(2000), cost(4000)
        # Linear predicts ~2.0, quadratic ~4.0. Assert well below the quadratic floor
        # rather than near the linear ideal, so ordinary CI jitter cannot fail it.
        ratio = large / small if small > 0 else 0.0
        assert ratio < 3.0, f"cost ratio {ratio:.2f} on a 2x input -- superlinear"


class TestTrailingSuffixSplitPreservesTheRunSemantics:
    """The linear walk must peel exactly the markers the anchored search used to peel.

    These are the shapes the previous implementation's own comment cited as its reason for
    looping -- either head order, an unfinished tail, a nested head, a lookalike closer, and
    the deliberate case asymmetry between the two heads.
    """

    A = "[OPTION-ACTIONS: close=Shut it]"
    CONTENT = "[OPTIONS: Alpha | Bravo]"

    def test_a_mixed_run_stays_whole_in_either_order(self):
        for tail in (
            f"{self.CONTENT}\n{self.A}",
            f"{self.A}\n{self.CONTENT}",
            f"{self.CONTENT}\n{self.A}\n{self.CONTENT}",
        ):
            visible, suffix = split_trailing_protocol_suffix(f"body\n{tail}")
            assert visible == "body\n", tail
            assert suffix == tail, tail

    def test_an_unfinished_tail_is_still_detached(self):
        visible, suffix = split_trailing_protocol_suffix(
            f"body\n{self.CONTENT}\n[OPTION-ACTIONS: close=Sh"
        )
        assert visible == "body\n"
        assert suffix.startswith(self.CONTENT)

    def test_a_marker_followed_by_prose_is_not_a_trailer(self):
        text = f"body\n{self.CONTENT} trailing prose"
        assert split_trailing_protocol_suffix(text) == (text, "")

    def test_a_nested_head_peels_only_the_inner_complete_marker(self):
        text = "body\n[OPTIONS: see [OPTIONS: nested]"
        visible, suffix = split_trailing_protocol_suffix(text)
        assert suffix == "[OPTIONS: nested]"
        assert visible == "body\n[OPTIONS: see "

    def test_the_head_case_asymmetry_is_preserved(self):
        """Content head case-SENSITIVE, action head case-INsensitive -- deliberate."""
        lower_content = "body\n[options: lower]"
        assert split_trailing_protocol_suffix(lower_content) == (lower_content, "")
        visible, suffix = split_trailing_protocol_suffix("body\n[Option-Actions: close=X]")
        assert suffix == "[Option-Actions: close=X]"


class TestACitationBracketCannotCancelAnOpenHead:
    """A closer pops the bracket it OPENED, so `[1]` cannot close a marker head.

    Depth was a bare count, so any closer decremented it. A citation inside an open
    content head therefore cancelled that head, and the action marker nested after it
    read as top-level: ``[OPTIONS: see [1] for details [OPTION-ACTIONS: close=X]``
    rendered a live close chip built from syntax that matches no content marker at all,
    while the identical line without the citation suppressed it. Same defect class the
    depth form was introduced to fix, one nesting level down.
    """

    ACTION = "[OPTION-ACTIONS: close=X]"

    def test_a_citation_inside_an_open_head_keeps_the_action_suppressed(self):
        text = f"[OPTIONS: see [1] for details {self.ACTION}"
        assert _is_inside_unclosed_marker(text, text.index(self.ACTION)) is True

    def test_several_citations_still_keep_it_suppressed(self):
        text = f"[OPTIONS: see [1] and [2] here {self.ACTION}"
        assert _is_inside_unclosed_marker(text, text.index(self.ACTION)) is True

    def test_the_citation_agrees_with_the_same_line_without_one(self):
        """The pair is the point: the citation must not change the verdict."""
        with_cite = f"[OPTIONS: see [1] for details {self.ACTION}"
        without = f"[OPTIONS: see for details {self.ACTION}"
        assert _is_inside_unclosed_marker(with_cite, with_cite.index(self.ACTION)) == (
            _is_inside_unclosed_marker(without, without.index(self.ACTION))
        )

    def test_control_a_citation_with_no_open_head_suppresses_nothing(self):
        """Proves the cases above are caused by the open head, not by the citation."""
        text = f"see [1] for details {self.ACTION}"
        assert _is_inside_unclosed_marker(text, text.index(self.ACTION)) is False

    def test_control_a_closed_content_marker_still_admits_the_action(self):
        text = f"[OPTIONS: A | B] see [1] {self.ACTION}"
        assert _is_inside_unclosed_marker(text, text.index(self.ACTION)) is False

    def test_a_stray_closer_with_no_opener_is_still_a_no_op(self):
        """It pops an empty stack, so the head that FOLLOWS it stays open."""
        text = f"] [OPTIONS: broken {self.ACTION}"
        assert _is_inside_unclosed_marker(text, text.index(self.ACTION)) is True

    def test_the_plural_form_agrees_with_the_singular_one(self):
        """Two spellings of one predicate must not diverge on the new rule."""
        text = f"[OPTIONS: see [1] for details {self.ACTION} tail {self.ACTION}"
        starts = [m.start() for m in OPTION_ACTIONS_RE_TRAILER.finditer(text)]
        starts += [m.start() for m in OPTION_ACTIONS_RE_LINE.finditer(text)]
        starts = sorted(set(starts))
        assert starts, "no action marker found -- the fixture stopped exercising the scan"
        assert _unclosed_marker_flags(text, starts) == [
            _is_inside_unclosed_marker(text, s) for s in starts
        ]
