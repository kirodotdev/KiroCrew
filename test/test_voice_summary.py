"""The spoken summary behind ``voice_reply.speak_mode = "summary"``.

The setting has two values and nothing else gets through: the loader reads a
stranger as ``full`` with one warning, the PUT refuses it. And a summary that
cannot be trusted is never spoken: every failure of the model step returns
``None`` and the caller speaks the full reply, so the user is never left in silence.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from kiro_crew import voice_summary
from kiro_crew.voice_summary import (
    DEFAULT_SPEAK_MODE,
    SPEAK_MODE_FULL,
    SPEAK_MODE_SUMMARY,
    SUMMARY_MIN_SOURCE_CHARS,
    speak_mode_from_config,
    summarize_for_speech,
    summary_is_usable,
    validated_speak_mode,
)

# A reply long enough to be worth summarizing, with a secret in it.
_REPLY = (
    "I ran the full backend suite and every test passed. The migration script now "
    "writes the new column before it drops the old one, so a rollback keeps the data. "
    "The access key for the staging box is AKIAIOSFODNN7EXAMPLE in case you need it. "
    "You still have to decide whether the nightly job should run at two or at four, "
    "and the frontend build is untouched by any of this."
)
_REPLY_SPOKEN = _REPLY


class TestTheSettingHasTwoValues:
    @pytest.mark.parametrize("value", ["full", "summary", " Summary ", "FULL"])
    def test_a_known_value_is_accepted_case_and_space_insensitively(self, value):
        assert validated_speak_mode(value) in {SPEAK_MODE_FULL, SPEAK_MODE_SUMMARY}

    @pytest.mark.parametrize(
        "value",
        ["", "brief", None, 1, True, ["summary"], {"mode": "summary"}],
        ids=["empty", "unknown-word", "none", "int", "bool", "list", "dict"],
    )
    def test_anything_else_is_rejected_without_raising(self, value):
        # A list or dict would raise TypeError from ``in frozenset`` without the
        # type check, which would turn a bad PUT body into a 500.
        assert validated_speak_mode(value) is None

    def test_an_absent_key_reads_as_full_with_no_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="kiro_crew.voice_summary"):
            assert speak_mode_from_config(None) == DEFAULT_SPEAK_MODE
        assert not caplog.records

    def test_a_stored_stranger_reads_as_full_with_one_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="kiro_crew.voice_summary"):
            assert speak_mode_from_config("brief") == SPEAK_MODE_FULL
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "brief" in warnings[0].getMessage()

    def test_a_stored_summary_reads_as_summary(self):
        assert speak_mode_from_config("summary") == SPEAK_MODE_SUMMARY


class TestWhatCountsAsASummary:
    def test_a_short_plain_answer_is_usable(self):
        assert summary_is_usable(
            _REPLY_SPOKEN, "I ran the tests and they passed. You pick the hour."
        )

    def test_an_empty_answer_is_not(self):
        assert not summary_is_usable(_REPLY_SPOKEN, "   \n")

    def test_an_answer_not_shorter_than_the_reply_is_not(self):
        # The model echoed the reply back; speaking the original is the honest answer.
        assert not summary_is_usable(_REPLY_SPOKEN, _REPLY_SPOKEN)
        assert not summary_is_usable(_REPLY_SPOKEN, _REPLY_SPOKEN[: int(len(_REPLY_SPOKEN) * 0.7)])

    @pytest.mark.parametrize(
        "answer",
        [
            "Here you go:\n```\nrm -rf build\n```",
            "## Summary\nAll done.",
            "- tests passed\n- pick the hour",
            "1. tests passed\n2. pick the hour",
            "| step | state |\n| --- | --- |",
            "See [the run](https://example.test/run/1).",
            "Done. [OPTIONS: Two | Four]",
        ],
        ids=["fence", "heading", "bullets", "numbered", "table", "link", "options"],
    )
    def test_an_answer_that_still_carries_markup_is_not(self, answer):
        assert not summary_is_usable(_REPLY_SPOKEN, answer)


@pytest.fixture
def oneliner(monkeypatch: pytest.MonkeyPatch):
    """Stub the background model turn and record what it was asked."""
    sent: list[dict] = []

    def set_reply(reply):
        async def _oneliner(sessions, prompt, **kwargs):
            sent.append({"prompt": prompt, **kwargs})
            if isinstance(reply, BaseException):
                raise reply
            return reply

        import kiro_crew.llm_helpers as helpers

        monkeypatch.setattr(helpers, "run_bg_oneliner", _oneliner)

    return sent, set_reply


class TestASummaryThatCannotBeTrustedIsNeverSpoken:
    @pytest.mark.asyncio
    async def test_the_prompt_carries_the_reply_and_the_call_is_tool_free(self, oneliner):
        sent, set_reply = oneliner
        set_reply("I ran the tests and they passed. You still pick the hour.")
        result = await summarize_for_speech(SimpleNamespace(), _REPLY, spoken=_REPLY_SPOKEN)
        assert result == "I ran the tests and they passed. You still pick the hour."
        assert len(sent) == 1
        assert "The migration script now" in sent[0]["prompt"]
        assert sent[0]["model"] == "auto"
        assert sent[0]["sel_source"] == "voice_summary"
        assert sent[0]["timeout"] == voice_summary.SUMMARY_TIMEOUT_SECS

    @pytest.mark.asyncio
    async def test_the_reply_is_redacted_before_it_leaves(self, oneliner):
        sent, set_reply = oneliner
        set_reply("Short and clean.")
        await summarize_for_speech(SimpleNamespace(), _REPLY, spoken=_REPLY_SPOKEN)
        assert "AKIAIOSFODNN7EXAMPLE" not in sent[0]["prompt"]

    @pytest.mark.asyncio
    async def test_the_answer_is_redacted_again_on_the_way_back(self, oneliner):
        _, set_reply = oneliner
        set_reply("Use AKIAIOSFODNN7EXAMPLE on staging.")
        result = await summarize_for_speech(SimpleNamespace(), _REPLY, spoken=_REPLY_SPOKEN)
        assert result is not None
        assert "AKIAIOSFODNN7EXAMPLE" not in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "failure",
        [RuntimeError("backend down"), asyncio.TimeoutError()],
        ids=["exception", "timeout"],
    )
    async def test_a_failed_model_turn_means_speak_the_full_reply(self, oneliner, failure):
        _, set_reply = oneliner
        set_reply(failure)
        assert await summarize_for_speech(SimpleNamespace(), _REPLY, spoken=_REPLY_SPOKEN) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "answer",
        ["", "   ", _REPLY, "```\nrm -rf build\n```", "- one\n- two"],
        ids=["empty", "blank", "echo", "fenced", "list"],
    )
    async def test_an_unusable_answer_means_speak_the_full_reply(self, oneliner, answer):
        _, set_reply = oneliner
        set_reply(answer)
        assert await summarize_for_speech(SimpleNamespace(), _REPLY, spoken=_REPLY_SPOKEN) is None

    @pytest.mark.asyncio
    async def test_a_long_reply_is_summarized_from_a_clipped_stripped_head(self, oneliner):
        sent, set_reply = oneliner
        set_reply("I ran the tests and they passed. You pick the hour.")
        head = "Here is what I did. " * 30
        code = "```\n" + "x = 1\n" * 6000 + "```\n"
        tail = "The last line says the tail."
        text = head + code + tail
        spoken = head + " (code block) " + tail
        result = await summarize_for_speech(SimpleNamespace(), text, spoken=spoken)
        assert result is not None
        assert len(sent[0]["prompt"]) < voice_summary.SUMMARY_MAX_INPUT_CHARS + 1000
        assert "x = 1" not in sent[0]["prompt"]
        assert "Here is what I did." in sent[0]["prompt"]

    @pytest.mark.asyncio
    async def test_a_very_long_reply_keeps_its_tail_where_the_outcome_is(self, oneliner):
        # The failure a summary must report sits at the END of a turn, so a clip
        # that kept only the head would summarize a failed turn as a success.
        sent, set_reply = oneliner
        set_reply("The deploy failed, so nothing shipped.")
        body = "I checked one more file and it looked fine. " * 700  # about 31,000 chars
        ending = "The deploy to the beta stage failed with an IAM error, so nothing shipped."
        text = body + ending
        result = await summarize_for_speech(SimpleNamespace(), text, spoken=text)
        assert result is not None
        prompt = sent[0]["prompt"]
        assert len(prompt) < voice_summary.SUMMARY_MAX_INPUT_CHARS + 1000
        assert ending in prompt
        assert prompt.index("I checked one more file") < prompt.index("left out")

    @pytest.mark.asyncio
    async def test_a_short_reply_is_spoken_as_it_is_without_a_model_call(self, oneliner):
        sent, set_reply = oneliner
        set_reply("Should not be asked.")
        short = "Done. Both files are in place."
        assert len(short) < SUMMARY_MIN_SOURCE_CHARS
        assert await summarize_for_speech(SimpleNamespace(), short, spoken=short) is None
        assert sent == []
