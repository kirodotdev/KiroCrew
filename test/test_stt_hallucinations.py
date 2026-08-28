"""The Whisper hallucination filter: what it removes, and what it must never remove.

The filter is the one step in the transcription pipeline that can DELETE words the
speaker actually said, and a silent deletion is indistinguishable from the model
never having heard them. So the cases below are weighted toward proving that real
speech survives, not just that artefacts go.

Pure text logic, no audio and no model, which is why it can be covered
exhaustively and cheaply.
"""

from __future__ import annotations

import logging

import pytest

from kiro_crew.stt.hallucinations import (
    _collapse_repeated_phrases,
    _is_boilerplate_line,
    filter_hallucinations,
)


class TestBoilerplateDetection:
    @pytest.mark.parametrize(
        "line",
        [
            "Subtitles by",
            "subtitles by amara.org.",
            "  Subtitled by!  ",
            "Captioned by.",
            "Subtitles by Amara.org",
        ],
    )
    def test_detects_boilerplate(self, line):
        assert _is_boilerplate_line(line) is True

    @pytest.mark.parametrize(
        "line",
        [
            "",
            "   ",
            "Let's ship the recording change on Friday.",
            "The copyright review is blocked on legal.",
            "I said goodbye to the old design.",
            # A sentence CONTAINING a boilerplate phrase is not boilerplate: these
            # are real speech on the normal path, and deleting them is silent
            # content loss (the blocking finding this rule exists to prevent).
            "Thanks for joining today's standup, let's start with Priya.",
            "I really do thank you for watching over the rollout last week.",
            "The transcript is available in the shared drive for everyone.",
            "See you next time we meet in Boston, bring the roadmap.",
            # Even ONE extra word must spare the sentence — "Thanks for joining,
            # everyone." is a normal meeting opener, not an artefact.
            "Thanks for joining, everyone.",
            "Thanks for watching this, team.",
            # Ordinary-speech phrases were REMOVED from the list entirely: a
            # dictated farewell or rights notice is plausible real speech even
            # as a complete utterance, so it must never be filtered.
            "Goodbye.",
            "goodbye",
            "Copyright",
            "All rights reserved.",
            "Thanks for listening.",
            "Thanks for joining.",
            "See you next time!",
            "The transcript is available.",
            # Sign-offs and subscribe CTAs were REMOVED from the list: each is a
            # sentence someone recording a demo or dictating a video script says
            # out loud, and a whole-transcript match discarded the recording.
            "Thank you for watching.",
            "Thanks for watching!",
            "Please subscribe.",
            "Like and subscribe.",
            "Please like and subscribe.",
            "Don't forget to subscribe.",
            "Hit the bell.",
            "Click the subscribe button.",
            "See you in the next video.",
        ],
    )
    def test_keeps_real_speech(self, line):
        assert _is_boilerplate_line(line) is False

    def test_all_phrases_require_a_whole_line_match(self):
        # Substring or word-count-proximity matching deletes real sentences that
        # merely mention (or lightly extend) a phrase — the cases above.
        assert _is_boilerplate_line("Transcribed by") is True
        assert _is_boilerplate_line("We kept transcribed by in the caption doc") is False

    def test_every_listed_phrase_is_a_caption_artefact(self):
        # LIST DISCIPLINE, tightened: an entry must be attribution text a caption
        # track carries ABOUT ITSELF, not merely "video-flavoured". The looser
        # caption-domain rule is what admitted "thank you for watching" and
        # "hit the bell" — sentences a human genuinely records, which the
        # whole-transcript path then deleted. Required direction:
        import re as _re

        attribution_markers = _re.compile(r"subtitle|caption|transcri|translat|amara|mooji")
        from kiro_crew.stt.hallucinations import _WHISPER_BOILERPLATE as phrases

        for phrase in phrases:
            assert attribution_markers.search(phrase), (
                f"'{phrase}' is not caption self-attribution — it may be real"
                " dictated speech, so it must not be on the filter list"
            )

    def test_no_listed_phrase_is_a_spoken_sign_off(self):
        # Forbidden direction, and the half that actually holds the line: the
        # required-marker test above passes for "subscribe to my subtitles too",
        # so the vocabulary of speech a presenter utters is banned outright. This
        # fails if a future edit re-adds any entry of the deleted class.
        import re as _re

        speech_markers = _re.compile(r"watch|subscribe|bell|video|thank|see you|like and")
        from kiro_crew.stt.hallucinations import _WHISPER_BOILERPLATE as phrases

        for phrase in phrases:
            assert not speech_markers.search(phrase), (
                f"'{phrase}' reads as something a speaker says on a recording;"
                " filtering it can delete the only words a transcript had"
            )

    def test_known_artefact_variants_are_listed_as_full_phrases(self):
        # "Subtitles by Amara.org" is the canonical artefact shape; it matches by
        # being IN the phrase list, not by loosening the match rule.
        assert _is_boilerplate_line("Subtitles by Amara.org") is True
        assert _is_boilerplate_line("Subtitles by the Amara.org community") is True


class TestCollapseRepeatedPhrases:
    def test_collapses_a_long_run_to_one(self):
        text = " ".join(["Thank you."] * 12)
        assert _collapse_repeated_phrases(text) == "Thank you."

    def test_leaves_a_short_run_alone(self):
        # Real emphasis reaches well past two — "No. No. No." is ordinary
        # insistence, and even five repeats is plausible counted speech. Only
        # dozens-long runs are the Whisper artefact.
        for n in range(2, 6):
            text = " ".join(["No."] * n)
            assert _collapse_repeated_phrases(text) == text, n

    def test_only_consecutive_runs_collapse(self):
        # The same sentence recurring later in a meeting is ordinary speech.
        text = "Okay. Next item. Okay."
        assert _collapse_repeated_phrases(text) == "Okay. Next item. Okay."

    def test_preserves_surrounding_speech(self):
        run = " ".join(["Uh huh."] * 8)
        text = f"We start now. {run} Then we ship."
        assert _collapse_repeated_phrases(text) == "We start now. Uh huh. Then we ship."

    def test_single_sentence_is_untouched(self):
        assert _collapse_repeated_phrases("Just one sentence") == "Just one sentence"


class TestFilterHallucinations:
    def test_empty_input_is_returned_as_is(self):
        assert filter_hallucinations("") == ""

    def test_real_speech_survives_intact(self):
        text = "We agreed to ship on Friday. Priya owns the rollout."
        assert filter_hallucinations(text) == text

    def test_a_fully_hallucinated_transcript_becomes_empty(self):
        # Which the caller turns into None. An empty string is the honest answer for
        # a recording of silence.
        assert filter_hallucinations("Subtitles by Amara.org. Transcribed by.") == ""

    def test_strips_boilerplate_but_keeps_the_meeting(self):
        text = "Priya owns the rollout. Subtitles by Amara.org. We ship Friday."
        assert filter_hallucinations(text) == "Priya owns the rollout. We ship Friday."

    def test_a_dictated_sign_off_survives_whole(self):
        """The GPT 5.6 blocking finding, pinned.

        Each of these is a complete sentence a human records — a demo outro, a
        dictated video script — and each was previously deleted by an exact
        whole-sentence match. When it was the entire transcript the filter
        returned "", which ``transcribe_audio`` turns into ``None``: the only
        words the recording held, gone, with a log line as the sole trace.
        """
        for text in (
            "Thank you for watching.",
            "Thanks for watching!",
            "Please subscribe.",
            "Don't forget to subscribe.",
            "Hit the bell.",
            "See you in the next video.",
        ):
            assert filter_hallucinations(text) == text, text

    def test_handles_both_artefacts_together(self):
        run = " ".join(["Okay."] * 10)
        text = f"{run} Ship it. Transcribed by."
        assert filter_hallucinations(text) == "Okay. Ship it."


class TestFilterHallucinationsVisibility:
    """The filter is the one step that can delete words the speaker said.

    A silent deletion is indistinguishable from the model never having heard the
    words, so every removal has to leave a trace an operator can find after the
    fact. These tests pin what the trace says, not merely that one exists.
    """

    def test_dropped_boilerplate_is_named_in_the_log(self, caplog):
        with caplog.at_level(logging.INFO, logger="kiro_crew.stt.hallucinations"):
            filter_hallucinations("Priya owns the rollout. Subtitles by Amara.org.")
        assert "dropped 1 boilerplate line(s)" in caplog.text
        assert "Subtitles by Amara.org." in caplog.text

    def test_collapsed_repetitions_are_counted_but_not_quoted(self, caplog):
        # A repeated sentence is ordinary speech; its text belongs in the
        # transcript, not in the log, so only the count is recorded.
        with caplog.at_level(logging.INFO, logger="kiro_crew.stt.hallucinations"):
            filter_hallucinations(" ".join(["Ship the thing."] * 8))
        assert "collapsed 7 repeated sentence(s)" in caplog.text
        assert "Ship the thing" not in caplog.text

    def test_discarding_the_whole_transcript_warns(self, caplog):
        # The caller turns "" into None and the recording is gone with no other
        # trace, so this case is a warning rather than an info line.
        with caplog.at_level(logging.INFO, logger="kiro_crew.stt.hallucinations"):
            assert filter_hallucinations("Subtitles by Amara.org. Transcribed by.") == ""
        assert "discarded the entire transcript" in caplog.text
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    def test_an_untouched_transcript_logs_nothing(self, caplog):
        # Every recording passes through here. A line per transcription would bury
        # the removals this logging exists to surface.
        with caplog.at_level(logging.INFO, logger="kiro_crew.stt.hallucinations"):
            filter_hallucinations("We agreed to ship on Friday.")
        assert caplog.records == []


# ---------------------------------------------------------------------------
# Install path
# ---------------------------------------------------------------------------
