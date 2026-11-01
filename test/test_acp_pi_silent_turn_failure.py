"""pi reports a FAILED turn exactly as it reports an empty one.

Measured on pi-acp 0.0.33 against pi 0.85.1 with ``~/.aws`` unreachable: the
Bedrock call fails, pi records ``stopReason: "error"`` with
``errorMessage: "Region is missing"`` in its OWN session file, writes nothing to
stderr, and ``session/prompt`` still answers ``{"stopReason": "end_turn"}`` with no
content. Crew therefore classifies ``provider_empty`` and spends the whole
empty-response ladder re-asking a question that fails identically.

Declared rather than derived on purpose: the fact cannot be read off usage (pi
forwards no ``usage_update``, so a GOOD pi turn is unbilled too) nor off a stop
reason (both outcomes are ``end_turn``). Membership only changes the give-up
card's WORDS; nothing here starts reading pi's session file.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from test_dashboard_chat_empty_response import TestEmptyResponseRetry as _EmptyRetrySuite

from kiro_crew.acp_backends import ACP_BACKEND_OPENCODE, ACP_BACKEND_PI


class TestTheSilentTurnFailureDeclaration:
    def test_pi_declares_it_and_every_other_known_backend_does_not(self) -> None:
        from kiro_crew.acp_backends import ACP_BACKENDS_KNOWN, ACP_BACKENDS_SILENT_TURN_FAILURE
        from kiro_crew.providers.acp import AcpProvider

        assert ACP_BACKENDS_SILENT_TURN_FAILURE == frozenset({ACP_BACKEND_PI})
        for backend in sorted(ACP_BACKENDS_KNOWN):
            provider = AcpProvider(acp_backend=backend)
            expected = backend if backend == ACP_BACKEND_PI else None
            assert provider.silent_turn_failure_backend == expected, backend

    def test_the_default_is_silence_so_an_undeclared_provider_is_unchanged(self) -> None:
        """H14: the property is declared on the ABC with a safe default."""
        from kiro_crew.providers.base import LLMProvider

        assert LLMProvider.silent_turn_failure_backend.fget(object()) is None

    def test_a_test_double_cannot_rewrite_a_transcript_card(self) -> None:
        """A non-``str`` backend answers ``None``, so a ``MagicMock`` client cannot
        make an unrelated test's card grow a warning."""
        from kiro_crew.providers.acp import AcpProvider

        provider = AcpProvider(acp_backend=ACP_BACKEND_PI)
        provider._client = MagicMock()
        assert provider.silent_turn_failure_backend is None

    def test_the_bare_shared_subagent_shape_answers_the_same(self) -> None:
        """The wrapper is not the only shape handed out; both must agree."""
        from kiro_crew.acp.session_provider import AcpSessionProvider

        provider = AcpSessionProvider.__new__(AcpSessionProvider)
        provider._runtime = MagicMock()
        provider._runtime.acp_backend = ACP_BACKEND_PI
        assert provider.silent_turn_failure_backend == ACP_BACKEND_PI
        provider._runtime.acp_backend = ACP_BACKEND_OPENCODE
        assert provider.silent_turn_failure_backend is None


class TestTheGiveUpCardNamesASilentFailureHarness:
    """The dashboard give-up card, on the single-card path (depth>0)."""

    _make_state_and_slot = _EmptyRetrySuite._make_state_and_slot
    _make_empty_stream = _EmptyRetrySuite._make_empty_stream
    _cancel_background_tasks = staticmethod(_EmptyRetrySuite._cancel_background_tasks)

    @pytest.mark.asyncio
    async def test_a_silent_failure_harness_is_named_on_the_give_up_card(
        self, tmp_path: Path
    ) -> None:
        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._make_empty_stream(client)
        state.sessions.get_provider = MagicMock(
            return_value=SimpleNamespace(silent_turn_failure_backend="pi")
        )

        await _run_chat(state, slot, "test message", _prompt_depth=1)
        await self._cancel_background_tasks(state)

        row = next(m for m in slot.messages if m.get("role") == "notice")
        card = row["content"]
        assert "returned nothing this turn" in card
        assert "the pi backend reports a FAILED turn" in card
        assert "credential error" in card
        # The dashboard renders the TOKEN's catalog copy, never ``content``, so
        # the note must ride a token of its own or the card still says "resend".
        from kiro_crew.dashboard import gateway_notice as gn

        assert row["meta"]["notice"] in {
            gn.GATEWAY_NOTICE_EMPTY_TURN_SILENT_FAILURE,
            gn.GATEWAY_NOTICE_EMPTY_TURN_AFTER_RECOVERY_SILENT_FAILURE,
        }
        assert row["meta"]["notice_args"] == {"harness": "pi"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider", [SimpleNamespace(silent_turn_failure_backend=None), None])
    async def test_a_harness_that_declares_nothing_keeps_the_old_card(
        self, tmp_path: Path, provider
    ) -> None:
        """An undeclared provider and a provider-less slot leave the card as it was."""
        from kiro_crew.messaging.empty_turn_copy import EMPTY_TURN_NOTICE

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._make_empty_stream(client)
        state.sessions.get_provider = MagicMock(return_value=provider)

        await _run_chat(state, slot, "test message", _prompt_depth=1)
        await self._cancel_background_tasks(state)

        from kiro_crew.dashboard import gateway_notice as gn

        row = next(m for m in slot.messages if m.get("role") == "notice")
        assert row["content"] == EMPTY_TURN_NOTICE
        assert row["meta"]["notice"] in {
            gn.GATEWAY_NOTICE_EMPTY_TURN,
            gn.GATEWAY_NOTICE_EMPTY_TURN_AFTER_RECOVERY,
        }
        assert "notice_args" not in row["meta"]

    @pytest.mark.asyncio
    async def test_a_productive_episode_keeps_the_after_work_card_alone(
        self, tmp_path: Path
    ) -> None:
        """The note rides only a NON-productive card, by the card's own test."""
        from kiro_crew.dashboard.chat_utils import _PROMISE_ONLY_CONTINUE_MSG

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._empty_response_retries = 2
        slot._empty_episode_productive = True
        self._make_empty_stream(client)
        state.sessions.get_provider = MagicMock(
            return_value=SimpleNamespace(silent_turn_failure_backend="pi")
        )

        await _run_chat(
            state,
            slot,
            _PROMISE_ONLY_CONTINUE_MSG,
            _synthetic_payload=True,
            _synthetic_recovery_turn=True,
        )
        await self._cancel_background_tasks(state)

        cards = [m["content"] for m in slot.messages if m.get("role") == "notice"]
        assert any("will not re-run" in c for c in cards)
        assert not any("FAILED turn" in c for c in cards)
