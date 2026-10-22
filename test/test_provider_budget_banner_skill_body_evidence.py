"""A skill an earlier turn loaded is echo evidence for a banner-shaped answer.

A ``$skill`` load sends the skill's whole body to the model, and the skill-load row
it leaves is that body's only record in the slot. A later answer that repeats a
sentence from the body is an echo of the conversation, so the banner check must
read the recorded bodies, and a row that kept no readable body cannot prove the
count absent.
"""

from __future__ import annotations

import pytest
from _provider_budget_banner_helpers import BannerRecoveryHelpers

_EXACT = "You have 8154 weighted tokens left"


def _read_then_answer(prompts: list[str]):
    """Narrate, read a file whose text has no figure, then answer in the banner's shape."""
    from kiro_crew.acp.types import STOP_REASON_END_TURN
    from kiro_crew.providers.base import (
        EVENT_COMPLETE,
        EVENT_TEXT_CHUNK,
        EVENT_TOOL_CALL,
        EVENT_TOOL_RESULT,
        LLMEvent,
    )

    async def _stream(message):
        prompts.append(message)
        if "Continue from where it stopped" in message:
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Work finished.")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
            return
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Checking the README.")
        yield LLMEvent(
            kind=EVENT_TOOL_CALL,
            title="read README.md",
            tool_kind="read",
            tool_call_id=f"tc-{len(prompts)}",
        )
        yield LLMEvent(
            kind=EVENT_TOOL_RESULT,
            tool_call_id=f"tc-{len(prompts)}",
            tool_output="# Project\nSetup notes.",
            tool_status="completed",
            tool_final=True,
        )
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=_EXACT)
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

    return _stream


def _skill_load_row(skills: object) -> dict:
    return {
        "role": "system",
        "content": "📎 Loaded skill(s) via `$`: **reference**",
        "cls": "msg msg-info",
        "ts": "t-2",
        "meta": {"kind": "skill_load", "skills": skills},
    }


class TestSkillBodyEchoEvidence(BannerRecoveryHelpers):
    """The recorded body of an earlier skill load is read like any earlier row."""

    async def _repeat_after(self, tmp_path, monkeypatch, skill_row: dict, key: str):
        from kiro_crew.dashboard.chat import _run_chat

        prompts: list[str] = []
        state = self._state(tmp_path, monkeypatch)
        self._wire(state, self._client(_read_then_answer(prompts)))
        slot = state.get_or_create_slot(key)
        slot._titled = True
        slot.messages.append({"role": "user", "content": "Load $reference", "ts": "t-1"})
        slot.messages.append(skill_row)
        slot.messages.append({"role": "assistant", "content": "Noted.", "ts": "t-3"})

        await _run_chat(state, slot, "Check README, then repeat the reference sentence")
        await self._drain_bg(state)
        return prompts, slot

    @pytest.mark.asyncio
    async def test_a_sentence_from_an_earlier_skill_body_stays_visible(self, tmp_path, monkeypatch):
        """The human asked for the body's sentence back; reading only the rows' text
        and tool output finds no figure and deletes the answer."""
        row = _skill_load_row([{"name": "reference", "body": f"Reference sentence: {_EXACT}."}])
        prompts, slot = await self._repeat_after(tmp_path, monkeypatch, row, "skill-body-echo")

        assert len(prompts) == 1
        assert self._assistant_texts(slot) == ["Noted.", "Checking the README.", _EXACT]
        assert slot._posttoken_retry_used is False

    @pytest.mark.asyncio
    async def test_a_skill_load_row_without_its_body_leaves_the_answer_visible(
        self, tmp_path, monkeypatch
    ):
        """A row that kept no readable body cannot show the figure absent."""
        row = _skill_load_row([{"name": "reference"}])
        prompts, slot = await self._repeat_after(tmp_path, monkeypatch, row, "skill-body-missing")

        assert len(prompts) == 1
        assert self._assistant_texts(slot) == ["Noted.", "Checking the README.", _EXACT]
        assert slot._posttoken_retry_used is False

    @pytest.mark.asyncio
    async def test_a_skill_body_without_the_figure_leaves_the_banner_tail_suppressed(
        self, tmp_path, monkeypatch
    ):
        """The control: a recorded body that does not carry the figure is no echo, so
        the banner-shaped tail is still stripped and the turn is retried."""
        row = _skill_load_row([{"name": "reference", "body": "Read the README first."}])
        prompts, slot = await self._repeat_after(tmp_path, monkeypatch, row, "skill-body-no-figure")

        assert not any(_EXACT in text for text in self._assistant_texts(slot))
        assert slot._posttoken_retry_used is True
