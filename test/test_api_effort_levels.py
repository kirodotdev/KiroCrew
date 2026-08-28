"""GET /api/effort-levels must not invent kiro levels for an adapter session."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from kiro_crew.acp.types import ACP_BACKEND_CODEX, ACP_BACKEND_KAS
from kiro_crew.dashboard.handlers import agents as agents_handler

KIRO_FALLBACK = ["low", "medium", "high", "xhigh", "max"]


def _slot_request(provider: object | None, slot: str = "chat-1") -> MagicMock:
    sessions = MagicMock()
    sessions.get_provider = MagicMock(return_value=provider)
    request = MagicMock()
    request.query = {"slot": slot}
    request.app = {"state": MagicMock(sessions=sessions)}
    return request


def _config_request() -> MagicMock:
    request = MagicMock()
    request.query = {}
    request.app = {}
    return request


class _Provider:
    def __init__(self, backend: str, levels: list[str]) -> None:
        self.backend = backend
        self._levels = levels

    def get_valid_effort_levels(self) -> list[str]:
        return list(self._levels)


@pytest.fixture
def kiro_union(monkeypatch: Any) -> list[str]:
    monkeypatch.setattr(agents_handler, "get_reasoning_effort_ordered", lambda: list(KIRO_FALLBACK))
    return KIRO_FALLBACK


class TestEffortLevelsFor:
    def test_an_adapter_slot_with_no_advertisement_is_empty(self, kiro_union: list[str]) -> None:
        """Codex effort is often baked into the model id.

        Substituting kiro's process-global union would show high/low/max
        notches the selected model never advertised.
        """
        provider = _Provider(ACP_BACKEND_CODEX, [])
        assert agents_handler._effort_levels_for(_slot_request(provider)) == []

    def test_an_adapter_slot_returns_exactly_what_it_advertised(
        self, kiro_union: list[str]
    ) -> None:
        provider = _Provider(ACP_BACKEND_CODEX, ["low", "high"])
        assert agents_handler._effort_levels_for(_slot_request(provider)) == ["low", "high"]

    def test_a_kiro_slot_with_empty_advertisement_keeps_the_global_fallback(
        self, kiro_union: list[str]
    ) -> None:
        """Do not change the kiro path (H1)."""
        provider = _Provider("", [])
        assert agents_handler._effort_levels_for(_slot_request(provider)) == kiro_union

    def test_kas_is_kiro_family_and_keeps_the_fallback(self, kiro_union: list[str]) -> None:
        provider = _Provider(ACP_BACKEND_KAS, [])
        assert agents_handler._effort_levels_for(_slot_request(provider)) == kiro_union

    def test_a_missing_slot_provider_does_not_invent_levels(self, kiro_union: list[str]) -> None:
        assert agents_handler._effort_levels_for(_slot_request(None)) == []

    def test_settings_on_an_adapter_do_not_inherit_the_kiro_union(
        self, kiro_union: list[str], monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(agents_handler, "_configured_acp_backend", lambda: ACP_BACKEND_CODEX)
        assert agents_handler._effort_levels_for(_config_request()) == []

    def test_settings_on_kiro_keep_the_global_list(
        self, kiro_union: list[str], monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(agents_handler, "_configured_acp_backend", lambda: "")
        assert agents_handler._effort_levels_for(_config_request()) == kiro_union


@pytest.mark.asyncio
async def test_handler_returns_the_resolved_list(kiro_union: list[str]) -> None:
    provider = _Provider(ACP_BACKEND_CODEX, [])
    resp = await agents_handler.api_effort_levels(_slot_request(provider))
    assert isinstance(resp, web.Response)
    assert resp.status == 200
    assert resp.body == b"[]"
