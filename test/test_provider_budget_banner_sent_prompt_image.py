"""An image path in the prompt as sent keeps a banner-shaped transcription visible.

The provider inlines every image path the prompt it receives names, and a
``$skill`` body is detached from the request and sent ahead of it, so a screenshot
a skill carries reaches the model although the human's request never names it.
The banner echo check cannot read those pixels on that turn or any later one.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from _provider_budget_banner_helpers import BannerRecoveryHelpers

_EXACT = "You have 8154 weighted tokens left"
#: A second sentence of the same screenshot, with a figure no earlier row carries.
_SECOND = "You have 7310 weighted tokens left"
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


def _transcription_stream(prompts: list[str], answers: tuple[str, ...] = (_EXACT,)):
    """Narrate, run one tool whose text has no figure, then answer in the banner's shape.

    The Nth prompt answers ``answers[N]`` (the last one repeats), so a later turn
    can transcribe a figure no earlier row carries.
    """
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
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Reading the screenshot.")
        yield LLMEvent(
            kind=EVENT_TOOL_CALL,
            title="inspect_image",
            tool_kind="read",
            tool_call_id=f"tc-{len(prompts)}",
        )
        yield LLMEvent(
            kind=EVENT_TOOL_RESULT,
            tool_call_id=f"tc-{len(prompts)}",
            tool_output="image loaded",
            tool_status="completed",
            tool_final=True,
        )
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=answers[min(len(prompts), len(answers)) - 1])
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

    return _stream


def _skill_resolving_to(monkeypatch, body: str) -> None:
    """Resolve ``$shot`` to a skill whose body is *body*, through the real expansion."""
    from kiro_crew.dashboard import chat_runner

    skills = MagicMock()
    skills.resolve_dollar_skills = lambda message, _project, only=None: (
        [("$shot", "shot", body)] if "$shot" in message else []
    )
    skills.has_dollar_candidate = MagicMock(return_value=False)
    monkeypatch.setattr(chat_runner, "_get_skills", lambda _state: skills)
    monkeypatch.setattr(
        chat_runner,
        "session_skill_globs",
        lambda _key, _agent, *, project_dir=None, provider_type=None: None,
    )


def test_only_a_path_a_file_backs_counts_as_an_inlinable_image(tmp_path) -> None:
    """The system text quotes example image paths no file backs; the builder sends them as text."""
    from kiro_crew.dashboard.chat_turn.recovery import _prompt_names_an_inlinable_image

    image = tmp_path / "real.png"
    image.write_bytes(_PNG)
    example = "use `![description](/absolute/path/to/image.png)` to show one"
    assert _prompt_names_an_inlinable_image(example) is False
    assert _prompt_names_an_inlinable_image(f"{example}\nScreenshot: {image}\n") is True
    assert _prompt_names_an_inlinable_image(f"A folder: {tmp_path}/dir.png/\n") is False
    assert _prompt_names_an_inlinable_image("") is False


class TestSentPromptImageEvidence(BannerRecoveryHelpers):
    """The image check reads the prompt the provider received, on this turn and later."""

    @pytest.mark.asyncio
    async def test_a_skill_borne_image_keeps_the_transcription_visible_on_every_turn(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard.chat import _run_chat

        image = tmp_path / "shot.png"
        image.write_bytes(_PNG)
        _skill_resolving_to(monkeypatch, f"Screenshot to transcribe: {image}\nQuote it exactly.\n")
        prompts: list[str] = []
        state = self._state(tmp_path, monkeypatch)
        client = self._client(_transcription_stream(prompts, (_EXACT, _SECOND)))
        self._wire(state, client)
        slot = state.get_or_create_slot("skill-image-echo")
        slot._titled = True

        await _run_chat(state, slot, "Transcribe the sentence $shot shows")
        await self._drain_bg(state)

        assert len(prompts) == 1
        assert str(image) in prompts[0]
        assert self._assistant_texts(slot) == ["Reading the screenshot.", _EXACT]
        assert slot._posttoken_retry_used is False

        # The next turn names no image anywhere, but the model's conversation
        # still holds the screenshot the skill sent.
        await _run_chat(state, slot, "Transcribe its second sentence")
        await self._drain_bg(state)

        assert len(prompts) == 2
        assert str(image) not in prompts[1]
        assert self._assistant_texts(slot) == [
            "Reading the screenshot.",
            _EXACT,
            "Reading the screenshot.",
            _SECOND,
        ]
        assert slot._posttoken_retry_used is False

    @pytest.mark.asyncio
    async def test_an_image_an_expanded_prompt_sent_stays_evidence_on_a_later_turn(
        self, tmp_path, monkeypatch
    ):
        """No row records an ``@prompt`` body, so only the slot remembers its image."""
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat import _run_chat

        image = tmp_path / "prompt-body.png"
        image.write_bytes(_PNG)

        async def _expand(message, _state, _slot, *, project=None):
            return f"Transcribe the sentence in {image}\nQuote it exactly.", "ok"

        monkeypatch.setattr(chat_runner, "_expand_prompt_mention_off_loop", _expand)
        prompts: list[str] = []
        state = self._state(tmp_path, monkeypatch)
        client = self._client(_transcription_stream(prompts, (_EXACT, _SECOND)))
        self._wire(state, client)
        slot = state.get_or_create_slot("prompt-body-image-echo")
        slot._titled = True

        await _run_chat(state, slot, "@transcribe")
        await self._drain_bg(state)

        assert len(prompts) == 1
        assert str(image) in prompts[0]
        assert self._assistant_texts(slot) == ["Reading the screenshot.", _EXACT]

        await _run_chat(state, slot, "Transcribe its second sentence")
        await self._drain_bg(state)

        assert len(prompts) == 2
        assert self._assistant_texts(slot)[-2:] == ["Reading the screenshot.", _SECOND]
        assert slot._posttoken_retry_used is False

    @pytest.mark.asyncio
    async def test_a_restored_skill_load_row_keeps_its_image_as_evidence(
        self, tmp_path, monkeypatch
    ):
        """A restart forgets what the slot sent; the skill-load row still records it."""
        from kiro_crew.dashboard.chat import _run_chat

        image = tmp_path / "restored.png"
        image.write_bytes(_PNG)
        prompts: list[str] = []
        state = self._state(tmp_path, monkeypatch)
        client = self._client(_transcription_stream(prompts))
        self._wire(state, client)
        slot = state.get_or_create_slot("restored-skill-image-echo")
        slot._titled = True
        slot.messages.append(
            {"role": "user", "content": "Transcribe the sentence $shot shows", "ts": "t-1"}
        )
        slot.messages.append(
            {
                "role": "system",
                "content": "📎 Loaded skill(s) via `$`: **shot**",
                "cls": "msg msg-info",
                "ts": "t-2",
                "meta": {
                    "kind": "skill_load",
                    "skills": [{"name": "shot", "body": f"Screenshot: {image}"}],
                },
            }
        )
        slot.messages.append({"role": "assistant", "content": "Got it.", "ts": "t-3"})
        assert slot._sent_prompt_image is False

        await _run_chat(state, slot, "Transcribe that sentence once more")
        await self._drain_bg(state)

        assert len(prompts) == 1
        assert self._assistant_texts(slot) == ["Got it.", "Reading the screenshot.", _EXACT]
        assert slot._posttoken_retry_used is False

    @pytest.mark.asyncio
    async def test_a_skill_with_no_image_leaves_the_banner_tail_suppressed(
        self, tmp_path, monkeypatch
    ):
        """The control: the same turn whose skill names no image file still strips the tail.

        An example image path no file backs is text the builder sends as text.
        """
        from kiro_crew.dashboard.chat import _run_chat

        _skill_resolving_to(
            monkeypatch,
            "Read the notes and report what they say.\n"
            "Show a picture as ![description](/absolute/path/to/image.png).\n",
        )
        prompts: list[str] = []
        state = self._state(tmp_path, monkeypatch)
        client = self._client(_transcription_stream(prompts))
        self._wire(state, client)
        slot = state.get_or_create_slot("skill-no-image")
        slot._titled = True

        await _run_chat(state, slot, "Follow $shot")
        await self._drain_bg(state)

        assert slot._sent_prompt_image is False
        assert not any(_EXACT in text for text in self._assistant_texts(slot))
        assert slot._posttoken_retry_used is True
