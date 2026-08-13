"""Tests for chat_runner._context_usage_payload.

Regression guard: the payload must include absolute used/window token counts
when the provider reports a window. The bug this catches shipped green because
nothing exercised the helper — it read last_prompt_stats off the AcpProvider
(where it does not exist) instead of via the provider's public accessors.

Second regression guard (#1645): when real token counts are unavailable the
payload must carry ``reset: True`` rather than a bare ``{slot, pct}`` frame.
A pct-only frame updates the frontend's percentage slice while stranding the
token-count slice, which surfaced as the "225K used / 0%" disagreement right
after ``/compact`` — the reset percentage sitting next to a stale
pre-compaction token count.
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
    # A frame carrying real counts is not a reset.
    assert "reset" not in payload


def test_payload_resets_when_window_unknown():
    # Before the first usage_update, window is 0 → no token counts to ship, so
    # the frame must reset the frontend's stored counts rather than update the
    # percentage alone and leave stale tokens behind.
    provider = _provider_with_stats(used=0, window=0, pct=0.0)
    payload = _context_usage_payload("dashboard:1", provider)
    assert payload == {"slot": "dashboard:1", "pct": 0.0, "reset": True}
    assert "used_tokens" not in payload
    assert "window_tokens" not in payload


def test_payload_resets_for_provider_without_token_accessors():
    # A provider lacking the token accessors (e.g. a bare stub) must not crash;
    # it yields pct plus a reset so the meter cannot strand stale counts.
    stub = MagicMock(spec=["context_usage_pct"])
    stub.context_usage_pct.return_value = 12.3
    payload = _context_usage_payload("dashboard:1", stub)
    assert payload == {"slot": "dashboard:1", "pct": 12.3, "reset": True}


def test_payload_resets_when_used_unmeasured():
    # Post-compaction state (#1645): reset_after_compaction keeps the window but
    # zeroes the counts. used == 0 means "not measured yet", not "empty
    # context" — shipping {used: 0, window: W} would assert a false "0 / W
    # tokens", and a bare pct-only frame would strand the pre-compaction token
    # count beside the freshly-reset 0%. The frame must carry `reset: True` so
    # the frontend drops its stored counts.
    provider = _provider_with_stats(used=0, window=200000, pct=0.0)
    payload = _context_usage_payload("dashboard:1", provider)
    assert payload == {"slot": "dashboard:1", "pct": 0.0, "reset": True}
    assert "used_tokens" not in payload
    assert "window_tokens" not in payload
