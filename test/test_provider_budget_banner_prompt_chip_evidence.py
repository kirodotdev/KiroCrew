"""An earlier ``@prompt`` expansion leaves the banner echo evidence not whole.

An ``@prompt`` mention sends the prompt file's whole body to the model, but the
slot keeps only the "Loaded prompt" chip. A later reply that repeats or translates
a banner-like sentence from that body cannot be told from the provider's own
banner, so the evidence reads as not whole and the reply stays visible.
"""

from __future__ import annotations

import pytest
from _provider_budget_banner_helpers import BannerRecoveryHelpers
from test_provider_budget_banner_skill_body_evidence import _EXACT, _read_then_answer


def _prompt_chip_row(*, tagged: bool) -> dict:
    row = {
        "role": "system",
        "content": "📜 Loaded prompt **@translate** (412 chars)",
        "cls": "msg msg-info",
        "ts": "t-2",
    }
    if tagged:
        row["meta"] = {"kind": "prompt_load"}
    return row


class TestPromptChipEchoEvidence(BannerRecoveryHelpers):
    async def _repeat_after(self, tmp_path, monkeypatch, rows: list[dict], key: str):
        from kiro_crew.dashboard.chat import _run_chat

        prompts: list[str] = []
        state = self._state(tmp_path, monkeypatch)
        self._wire(state, self._client(_read_then_answer(prompts)))
        slot = state.get_or_create_slot(key)
        slot._titled = True
        slot.messages.append({"role": "user", "content": "@translate", "ts": "t-1"})
        slot.messages.extend(rows)
        slot.messages.append({"role": "assistant", "content": "Noted.", "ts": "t-3"})

        await _run_chat(state, slot, "Check README, then translate the prompt's sentence")
        await self._drain_bg(state)
        return prompts, slot

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tagged", [True, False], ids=["tagged", "legacy-chip"])
    async def test_a_reply_after_an_earlier_prompt_expansion_stays_visible(
        self, tmp_path, monkeypatch, tagged
    ):
        """The body the chip stands for may hold the sentence, so the reply is kept."""
        prompts, slot = await self._repeat_after(
            tmp_path, monkeypatch, [_prompt_chip_row(tagged=tagged)], f"prompt-chip-{tagged}"
        )

        assert len(prompts) == 1
        assert self._assistant_texts(slot) == ["Noted.", "Checking the README.", _EXACT]
        assert slot._posttoken_retry_used is False

    @pytest.mark.asyncio
    async def test_with_no_prompt_expansion_the_banner_tail_is_still_suppressed(
        self, tmp_path, monkeypatch
    ):
        """The control: the same turn with no earlier chip still strips the tail."""
        _prompts, slot = await self._repeat_after(tmp_path, monkeypatch, [], "no-prompt-chip")

        assert not any(_EXACT in text for text in self._assistant_texts(slot))
        assert slot._posttoken_retry_used is True


def test_the_chip_row_carries_the_prompt_load_kind() -> None:
    """The writer tags the chip, so the evidence check does not depend on its wording."""
    from unittest.mock import MagicMock

    from kiro_crew.dashboard.chat_turn.prompt_assembly import _surface_prompt_chip

    slot = MagicMock()
    _surface_prompt_chip(MagicMock(), slot, "📜 Loaded prompt **@x** (1 chars)")
    slot.append.assert_called_once_with(
        "system",
        "📜 Loaded prompt **@x** (1 chars)",
        "msg msg-info",
        meta={"kind": "prompt_load"},
    )
