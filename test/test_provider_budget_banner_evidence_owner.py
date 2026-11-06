"""The banner's echo evidence has one owner in ``chat_utils_turn_text``.

Its two per-turn records are bounded by count and size, a text left out marks
the evidence incomplete, and the evidence reads the slot's rows only when asked.
A typed provider recovery that strips the banner keeps the answer after it.
"""

from __future__ import annotations

import pytest
from _provider_budget_banner_helpers import BannerRecoveryHelpers

from kiro_crew.dashboard.chat_utils import (
    TURN_EVIDENCE_MAX_CHARS,
    TURN_EVIDENCE_MAX_ITEMS,
    TurnEchoEvidence,
    classify_provider_budget_banner,
    extend_user_authority,
    retain_turn_evidence,
)


def _evidence(messages=(), before=(), **overrides):
    kwargs = dict(
        prompt_text="please continue",
        turn_inputs=["please continue"],
        attachments=(),
        sent_prompt_image=False,
        omitted_older_rows=False,
        tool_output_truncated=False,
        inputs_overflowed=False,
        carries_image_ref=lambda _text: False,
    )
    kwargs.update(overrides)
    return TurnEchoEvidence(list(messages), frozenset(before), **kwargs)


class TestTurnEvidenceBounds:
    def test_inputs_keep_every_text_within_the_bounds(self):
        kept: list[str] = ["prompt"]
        assert retain_turn_evidence(kept, ["", "steer one", "steer two"]) is True
        assert kept == ["prompt", "", "steer one", "steer two"]

    def test_inputs_stop_at_the_item_bound(self):
        kept = ["x"] * (TURN_EVIDENCE_MAX_ITEMS - 1)
        assert retain_turn_evidence(kept, ["last fits", "one too many", "later"]) is False
        assert len(kept) == TURN_EVIDENCE_MAX_ITEMS
        assert kept[-1] == "last fits"

    def test_inputs_stop_at_the_size_bound(self):
        kept = ["a" * (TURN_EVIDENCE_MAX_CHARS - 3)]
        assert retain_turn_evidence(kept, ["bcd", "e"]) is False
        assert kept == ["a" * (TURN_EVIDENCE_MAX_CHARS - 3), "bcd"]

    def test_authority_joins_each_text_on_its_own_line(self):
        assert extend_user_authority("first", ["", "second", "third"]) == (
            "first\nsecond\nthird",
            True,
        )

    def test_authority_stops_at_the_size_bound(self):
        start = "a" * (TURN_EVIDENCE_MAX_CHARS - 4)
        authority, fits = extend_user_authority(start, ["bcd", "efgh"])
        assert fits is False
        assert authority == start + "\nbcd"


class TestTurnEchoEvidence:
    def test_whole_by_default(self):
        assert _evidence().complete is True

    @pytest.mark.parametrize(
        "override",
        [
            {"inputs_overflowed": True},
            {"sent_prompt_image": True},
            {"omitted_older_rows": True},
            {"tool_output_truncated": True},
            {"carries_image_ref": lambda text: "shot.png" in text, "attachments": ("shot.png",)},
        ],
    )
    def test_each_unreadable_source_makes_it_incomplete(self, override):
        assert _evidence(**override).complete is False

    def test_earlier_rows_decide_wholeness(self):
        legacy_tool = {"role": "tool", "ts": "t1", "meta": {"output": "ok"}}
        whole_tool = {
            "role": "tool",
            "ts": "t1",
            "meta": {"output": "ok", "outputTruncated": False},
        }
        bodyless_skill = {"role": "inject", "ts": "t2", "meta": {"kind": "skill_load"}}
        assert _evidence([legacy_tool], {"t1"}).complete is False
        assert _evidence([whole_tool], {"t1"}).complete is True
        assert _evidence([bodyless_skill], {"t2"}).complete is False

    def test_rows_are_read_only_when_asked(self):
        calls: list[str] = []

        def _probe(text):
            calls.append(text)
            return False

        evidence = _evidence(carries_image_ref=_probe)
        assert calls == []
        assert evidence.complete is True
        assert calls

    def test_sources_split_this_turns_tools_from_the_transcript(self):
        rows = [
            {"role": "user", "ts": "t1", "content": "earlier question"},
            {
                "role": "inject",
                "ts": "t2",
                "meta": {"kind": "skill_load", "skills": [{"name": "s", "body": "skill body"}]},
            },
            {"role": "assistant", "ts": "t3", "content": "this turn's first segment"},
            {"role": "tool", "ts": "t4", "meta": {"output": "8154 left"}},
        ]
        evidence = _evidence(rows, {"t1", "t2"})
        assert list(evidence.tool_results()) == ["8154 left"]
        assert list(evidence.transcript_texts()) == [
            "earlier question",
            "skill body",
            "this turn's first segment",
        ]

    def test_overflowed_inputs_keep_a_banner_only_tail_visible(self):
        banner = "You have 8154 weighted tokens left"
        for overflowed, expected in ((False, ("", True)), (True, (banner, False))):
            evidence = _evidence(inputs_overflowed=overflowed)
            assert (
                classify_provider_budget_banner(
                    banner,
                    "",
                    is_provider_recovery=False,
                    is_transient_recovery=False,
                    prior_visible_output=True,
                    tool_results=evidence.tool_results(),
                    echo_evidence_complete=evidence.complete,
                    transcript_texts=evidence.transcript_texts(),
                    turn_inputs=[],
                )
                == expected
            )


class TestProviderRecoveryKeepsTheAnswer(BannerRecoveryHelpers):
    @pytest.mark.parametrize("separator", ["\n", ""])
    @pytest.mark.asyncio
    async def test_stripped_banner_leaves_the_answer_saved(self, tmp_path, monkeypatch, separator):
        """The banner is cut off the raw segment copy too, so the flush's hash
        check holds and the saved row is the answer, not the withheld notice."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_runner import _POSTTOKEN_RECOVER_MSG, _SEGMENT_UNCHECKED_TEXT
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        async def _stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="You have 8154 weighted tokens left")
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=f"{separator}Here is the rest.")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot(f"provider-recovery-answer-{len(separator)}")
        slot._titled = True
        slot._posttoken_retry_used = True

        await _run_chat(
            state,
            slot,
            _POSTTOKEN_RECOVER_MSG,
            _synthetic_payload=True,
            _current_message=self._provider_recovery_row(),
        )
        await self._drain_bg(state)

        assert self._assistant_texts(slot) == ["Here is the rest."]
        assert _SEGMENT_UNCHECKED_TEXT not in self._assistant_texts(slot)
        assert not slot._queue
