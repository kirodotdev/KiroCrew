"""Tests for chat_runner._context_usage_payload.

Regression guard: the payload must include absolute used/window token counts
when the provider reports a window. The bug this catches shipped green because
nothing exercised the helper — it read last_prompt_stats off the AcpProvider
(where it does not exist) instead of via the provider's public accessors.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from kiro_crew.acp.types import AcpPromptStats
from kiro_crew.dashboard.chat_runner import _context_usage_payload
from kiro_crew.providers.acp import AcpProvider


def _provider_with_stats(used: int, window: int, pct: float) -> AcpProvider:
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider()
    provider._client = MagicMock()
    provider._client.last_prompt_stats = AcpPromptStats(
        context_pct=pct,
        context_used_tokens=used,
        context_window_tokens=window,
    )
    return provider


def test_payload_includes_tokens_when_window_known():
    # The marquee feature: a real AcpProvider must surface used/window tokens.
    provider = _provider_with_stats(used=88000, window=200000, pct=44.0)
    payload = _context_usage_payload("dashboard:1", provider)
    assert payload["slot"] == "dashboard:1"
    assert payload["pct"] == 44.0
    assert payload["used_tokens"] == 88000
    assert payload["window_tokens"] == 200000


def test_payload_omits_tokens_when_window_unknown():
    # Before the first usage_update, window is 0 → token fields omitted, pct only.
    provider = _provider_with_stats(used=0, window=0, pct=0.0)
    payload = _context_usage_payload("dashboard:1", provider)
    assert payload == {"slot": "dashboard:1", "pct": 0.0}
    assert "used_tokens" not in payload
    assert "window_tokens" not in payload


def test_payload_pct_only_for_provider_without_token_accessors():
    # A provider lacking the token accessors (e.g. a bare stub) must not crash;
    # it simply yields pct only.
    stub = MagicMock(spec=["context_usage_pct"])
    stub.context_usage_pct.return_value = 12.3
    payload = _context_usage_payload("dashboard:1", stub)
    assert payload == {"slot": "dashboard:1", "pct": 12.3}
