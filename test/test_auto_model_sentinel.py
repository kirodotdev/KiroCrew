"""Tests for the `auto` model sentinel reaching kiro-cli as a real model id.

`auto` is not "no model chosen": kiro-cli serves it as an advertised model id
(its own ``default_model``) that routes per task at a 1.0 credit multiplier.
Treating it as a sentinel and skipping ``session/set_model`` left every cold
session on whatever ``--agent`` resolved to — the agent spec's pinned model —
while the picker still reported ``auto``.

These tests pin the gate that decides when the sentinel is sent: ask the backend
what it advertised, rather than hardcoding either behaviour. The claude backend
never advertises ``auto`` and must keep the historical skip.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import DEFAULT_MODEL
from kiro_crew.acp.types import advertises_model
from kiro_crew.dashboard.chat_handlers import _wire_model_id

_KIRO_MODELS = [
    {"modelId": "auto", "name": "auto", "description": "Models chosen by task"},
    {"modelId": "claude-opus-4.8", "name": "claude-opus-4.8", "description": ""},
    {"modelId": "gpt-5.6-sol", "name": "gpt-5.6-sol", "description": ""},
]


class TestAdvertisesModel:
    """The single gate shared by the handshake, the runtime cold start, the
    warm-worker re-apply, and the live dashboard switch."""

    def test_true_for_advertised_id(self):
        assert advertises_model(_KIRO_MODELS, "auto") is True
        assert advertises_model(_KIRO_MODELS, "claude-opus-4.8") is True

    def test_false_for_unadvertised_id(self):
        assert advertises_model(_KIRO_MODELS, "claude-fable-5") is False

    def test_false_for_empty_list(self):
        """A backend that reported no models degrades to the historical skip."""
        assert advertises_model([], "auto") is False

    def test_false_for_empty_model_id(self):
        assert advertises_model(_KIRO_MODELS, "") is False

    def test_ignores_malformed_entries(self):
        """A non-dict row must not raise — the list comes off the wire."""
        assert advertises_model(["auto", None, {"modelId": "auto"}], "auto") is True  # type: ignore[list-item]
        assert advertises_model(["auto", None], "auto") is False  # type: ignore[list-item]

    def test_does_not_match_on_name_or_description(self):
        """Only ``modelId`` is a selectable id; ``name`` is display copy."""
        rows = [{"modelId": "claude-opus-4.8", "name": "auto", "description": "auto"}]
        assert advertises_model(rows, "auto") is False


class TestWireModelIdAuto:
    """`_wire_model_id` translates slot.model into the id THIS backend accepts."""

    @staticmethod
    def _provider(models, *, is_claude=False):
        provider = MagicMock()
        provider.is_claude_backend = is_claude
        provider.available_models = MagicMock(return_value=models)
        return provider

    @pytest.mark.parametrize("stored", ["", "auto"])
    def test_auto_wired_when_kiro_advertises_it(self, stored):
        """Both spellings of "provider default" resolve to the real `auto` id."""
        assert _wire_model_id(self._provider(_KIRO_MODELS), stored) == "auto"

    @pytest.mark.parametrize("stored", ["", "auto"])
    def test_empty_when_backend_does_not_advertise_auto(self, stored):
        """No advertised `auto` means the caller must fall back to a reset."""
        assert _wire_model_id(self._provider([]), stored) == ""

    def test_concrete_model_passes_through(self):
        assert _wire_model_id(self._provider(_KIRO_MODELS), "gpt-5.6-sol") == "gpt-5.6-sol"

    def test_canonical_key_translated_to_kiro_id(self):
        assert _wire_model_id(self._provider(_KIRO_MODELS), "opus-4.8-1m") == "claude-opus-4.8"

    def test_claude_backend_never_wires_auto(self):
        """The claude backend has no id meaning "let the server choose", so a
        return to default still needs a session reset."""
        provider = self._provider(_KIRO_MODELS, is_claude=True)
        assert _wire_model_id(provider, "auto") == ""
        assert _wire_model_id(provider, "") == ""


class TestSessionProviderAutoReapply:
    """`AcpSessionProvider.new_conversation` re-applies the model to the fresh
    session. A worker that was on `auto` must be put back on `auto`, not left to
    inherit the agent spec's pinned model."""

    @staticmethod
    def _handles(advertised):
        old = MagicMock()
        old.model = DEFAULT_MODEL
        new_handle = MagicMock()
        new_handle.available_models = advertised
        new_handle.set_model = AsyncMock()
        new_handle.destroy = AsyncMock()
        return old, new_handle

    @pytest.mark.asyncio
    async def test_reapplies_auto_when_advertised(self):
        from kiro_crew.acp import session_provider as sp

        old, new_handle = self._handles(_KIRO_MODELS)
        provider = object.__new__(sp.AcpSessionProvider)
        provider._handle = old
        runtime = MagicMock()
        runtime._agent = "kirocrew"
        runtime.create_session = AsyncMock(return_value=new_handle)
        provider._runtime = runtime
        old.destroy = AsyncMock()

        await provider.new_conversation()

        new_handle.set_model.assert_awaited_once_with(DEFAULT_MODEL)
        assert provider._handle is new_handle

    @pytest.mark.asyncio
    async def test_skips_auto_when_not_advertised(self):
        from kiro_crew.acp import session_provider as sp

        old, new_handle = self._handles([])
        provider = object.__new__(sp.AcpSessionProvider)
        provider._handle = old
        runtime = MagicMock()
        runtime._agent = "kirocrew"
        runtime.create_session = AsyncMock(return_value=new_handle)
        provider._runtime = runtime
        old.destroy = AsyncMock()

        await provider.new_conversation()

        new_handle.set_model.assert_not_awaited()
        assert provider._handle is new_handle
