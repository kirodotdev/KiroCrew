"""Cron-created sessions must open with a real context-meter reading, not 0%.

The cron executor resets its agent session the moment a run finishes, so the
slot-detail open path can never read the provider live — and the snapshot
fallback was only ever written by dashboard-driven turns. Opening a
``cron-{id}`` slot therefore rendered a 0% bar over a full transcript.

Covered here: the capture helper (``context_meter_reading``), the injection
wiring that routes the reading through ``broadcast_context_usage`` (the
meter's single writer), and the end-to-end read-back — inject, then open the
slot with no resident provider and get the stale reading the run recorded.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.acp.types import AcpPromptStats
from kiro_crew.dashboard.cron_inject import (
    context_meter_reading,
    inject_cron_result_to_dashboard,
)
from kiro_crew.providers.acp import AcpProvider


def _provider(used: int, window: int, pct: float) -> AcpProvider:
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider()
    provider._client = MagicMock()
    provider._client.last_prompt_stats = AcpPromptStats(
        context_pct=pct,
        context_used_tokens=used,
        context_window_tokens=window,
    )
    return provider


def _make_job(job_id="abc123", name="test-cron"):
    job = MagicMock()
    job.id = job_id
    job.name = name
    job.agent_id = ""
    return job


@pytest.fixture(autouse=True)
def _isolate_snapshot_file(tmp_path, monkeypatch):
    """Point the snapshot sidecar at tmp_path — same isolation as
    test_context_bar_reopen, so a stray entry in the developer's real
    ~/.kiro/crew/context_snapshots.json cannot change what we observe."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)


# ── context_meter_reading: capture from a live provider ────────────────────


def test_reading_carries_pct_and_counts():
    reading = context_meter_reading(_provider(88000, 200000, 44.0))
    assert reading == {"pct": 44.0, "used_tokens": 88000, "window_tokens": 200000}


def test_reading_pct_alone_when_counts_unmeasured():
    # The common kiro-cli case: contextUsagePercentage with no usage_update.
    reading = context_meter_reading(_provider(0, 0, 11.4))
    assert reading == {"pct": 11.4}


def test_reading_omits_counts_when_used_unmeasured():
    # used == 0 with a known window is the post-compaction state — shipping
    # {used: 0, window: W} would assert a false "0 / W tokens".
    reading = context_meter_reading(_provider(0, 200000, 12.0))
    assert reading == {"pct": 12.0}


def test_no_reading_when_nothing_measured():
    assert context_meter_reading(_provider(0, 0, 0.0)) is None


def test_no_reading_for_non_finite_pct():
    assert context_meter_reading(_provider(0, 0, float("nan"))) is None
    assert context_meter_reading(_provider(0, 0, float("inf"))) is None


def test_no_reading_without_accessors():
    assert context_meter_reading(object()) is None


def test_no_reading_when_accessor_raises():
    client = MagicMock()
    client.context_usage_pct.side_effect = RuntimeError("gone")
    assert context_meter_reading(client) is None


# ── inject wiring: route through the single writer ─────────────────────────


def test_inject_records_reading_via_single_writer():
    state = MagicMock()
    slot = MagicMock()
    slot.key = "cron-abc123"
    slot.linked_session_key = "cron:abc123"
    slot.messages = []
    state.get_or_create_slot.return_value = slot
    state.conversation_log = None

    inject_cron_result_to_dashboard(
        state, _make_job(), "result",
        context_reading={"pct": 61.2, "used_tokens": 122400, "window_tokens": 200000},
    )

    state.broadcast_context_usage.assert_called_once_with(
        "cron-abc123",
        {"slot": "cron-abc123", "pct": 61.2,
         "used_tokens": 122400, "window_tokens": 200000},
    )


def test_inject_pct_only_reading_signals_reset():
    # A bare {slot, pct} frame would leave stale token counts beside a fresh
    # percentage in the frontend's independent slices — reset moves them together.
    state = MagicMock()
    slot = MagicMock()
    slot.key = "cron-abc123"
    slot.linked_session_key = "cron:abc123"
    slot.messages = []
    state.get_or_create_slot.return_value = slot
    state.conversation_log = None

    inject_cron_result_to_dashboard(
        state, _make_job(), "result", context_reading={"pct": 33.0}
    )

    state.broadcast_context_usage.assert_called_once_with(
        "cron-abc123", {"slot": "cron-abc123", "pct": 33.0, "reset": True}
    )


def test_inject_without_reading_records_nothing():
    # The to-chat replay path (no client in hand) must not clobber whatever
    # snapshot an earlier run stored.
    state = MagicMock()
    slot = MagicMock()
    slot.key = "cron-abc123"
    slot.linked_session_key = "cron:abc123"
    slot.messages = []
    state.get_or_create_slot.return_value = slot
    state.conversation_log = None

    inject_cron_result_to_dashboard(state, _make_job(), "result")

    state.broadcast_context_usage.assert_not_called()


# ── end to end: the reported bug ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cron_slot_opens_with_stale_reading_not_zero(tmp_path):
    """Inject with a reading, then open the slot with no resident provider:
    the detail response must carry the run's percentage flagged stale —
    previously it carried nothing and the bar rendered 0%."""
    state = _make_state(tmp_path)
    state.sessions.get_provider = MagicMock(return_value=None)

    inject_cron_result_to_dashboard(
        state, _make_job(), "cron result",
        context_reading={"pct": 57.3, "used_tokens": 114600, "window_tokens": 200000},
    )

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.get("/api/chat/slots/cron-abc123")
        assert resp.status == 200
        body = await resp.json()

    assert body["context_pct"] == 57.3
    assert body["context_stale"] is True
    assert body["context_window_tokens"] == 200000
    # A stale reading omits `used` — no process measured it for THIS session
    # incarnation; the tooltip derives a ~ approximation from pct instead.
    assert "context_used_tokens" not in body


@pytest.mark.asyncio
async def test_cron_slot_reading_survives_model_check(tmp_path):
    """The snapshot records the slot's model at injection time, so the
    read-side model comparison passes for an untouched cron slot."""
    state = _make_state(tmp_path)
    state.sessions.get_provider = MagicMock(return_value=None)

    inject_cron_result_to_dashboard(
        state, _make_job(job_id="xyz789"), "cron result",
        context_reading={"pct": 12.5},
    )
    slot = state.get_or_create_slot(name="cron-xyz789")
    snapshot = state._context_snapshots["cron-xyz789"]
    assert snapshot["pct"] == 12.5
    assert snapshot["model"] == slot.model
