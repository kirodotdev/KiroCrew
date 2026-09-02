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

import threading
from unittest.mock import MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.acp.types import AcpPromptStats
from kiro_crew.dashboard.cron_inject import (
    context_meter_reading,
    inject_cron_result_to_dashboard,
    prefetch_cron_history,
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


def _make_job(job_id="abc123", name="test-cron", message="do the thing"):
    # ``message`` and ``last_result_ts`` are real values, not Mock attributes:
    # the injector now writes the run's prompt as a paired ``user`` row and
    # stamps both rows from the result timestamp, so a MagicMock here would
    # reach the redactors as a non-string.
    job = MagicMock()
    job.id = job_id
    job.name = name
    job.message = message
    job.last_result_ts = 0.0
    job.agent_id = ""
    job.timezone = "UTC"
    return job


def _inject(state, job, result_text, **kw):
    """The injection, with the transcript read its async callers now prefetch.

    ``history`` is a required parameter in production so that no async caller can
    leave the whole-transcript parse on the event loop (issue #7408). These tests
    drive the function synchronously, where a blocking read is the caller's own
    cost, so the read that used to live inside the injection lives here instead.
    """
    kw.setdefault(
        "history",
        state.conversation_log.read_messages(f"cron:{job.id}") if state.conversation_log else [],
    )
    inject_cron_result_to_dashboard(state, job, result_text, **kw)


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

    _inject(
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

    _inject(
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

    _inject(state, _make_job(), "result")

    state.broadcast_context_usage.assert_not_called()


# ── end to end: the reported bug ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cron_slot_opens_with_stale_reading_not_zero(tmp_path):
    """Inject with a reading, then open the slot with no resident provider:
    the detail response must carry the run's percentage flagged stale —
    previously it carried nothing and the bar rendered 0%."""
    state = _make_state(tmp_path)
    state.sessions.get_provider = MagicMock(return_value=None)

    _inject(
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

    _inject(
        state, _make_job(job_id="xyz789"), "cron result",
        context_reading={"pct": 12.5},
    )
    slot = state.get_or_create_slot(name="cron-xyz789")
    snapshot = state._context_snapshots["cron-xyz789"]
    assert snapshot["pct"] == 12.5
    assert snapshot["model"] == slot.model


# -- prefetch_cron_history: the injection's transcript read, off the loop ----


def _log_recording(rows: list[dict], seen: list[int]) -> MagicMock:
    """A conversation log whose ``read_messages`` records its calling thread."""

    def _read(key: str) -> list[dict]:
        seen.append(threading.get_ident())
        return rows

    log = MagicMock()
    log.read_messages = MagicMock(side_effect=_read)
    return log


@pytest.mark.asyncio
async def test_prefetch_reads_off_the_loop_when_the_slot_is_unlinked():
    """An unlinked slot means the injection WILL read, so the read is hoisted.

    Issue #7408: the sync injection reads ``cron:{id}`` itself in that case, and
    on an async caller that parse ran on the event loop. Thread identity is the
    assertion, not the presence of an ``await``.
    """
    seen: list[int] = []
    rows = [{"role": "assistant", "content": "earlier run"}]
    state = MagicMock()
    state.conversation_log = _log_recording(rows, seen)
    slot = MagicMock()
    slot.linked_session_key = ""
    state.get_slot = MagicMock(return_value=slot)

    assert await prefetch_cron_history(state, "abc123") == rows
    assert seen, "the transcript was never read"
    assert threading.get_ident() not in seen, (
        "the cron transcript was parsed on the event-loop thread"
    )
    state.get_slot.assert_called_once_with("cron-abc123")


@pytest.mark.asyncio
async def test_prefetch_reads_when_the_slot_does_not_exist_yet():
    """No slot yet: the injection creates one, links it, and consumes history."""
    seen: list[int] = []
    state = MagicMock()
    state.conversation_log = _log_recording([], seen)
    state.get_slot = MagicMock(return_value=None)

    assert await prefetch_cron_history(state, "abc123") == []
    assert threading.get_ident() not in seen


@pytest.mark.asyncio
async def test_prefetch_skips_the_read_for_an_already_linked_slot():
    """A linked slot never reaches the injection's read, so nothing is read.

    Skipping matters: these callers fire on every suppressed and every silent
    run, and an unconditional prefetch would add a whole-transcript parse whose
    result is discarded.
    """
    seen: list[int] = []
    state = MagicMock()
    state.conversation_log = _log_recording([], seen)
    slot = MagicMock()
    slot.linked_session_key = "cron:abc123"
    state.get_slot = MagicMock(return_value=slot)

    assert await prefetch_cron_history(state, "abc123") is None
    assert seen == []
    state.conversation_log.read_messages.assert_not_called()


@pytest.mark.asyncio
async def test_prefetch_returns_none_without_a_conversation_log():
    """No log configured is the injection's own ``[]`` case -- nothing to read."""
    state = MagicMock()
    state.conversation_log = None

    assert await prefetch_cron_history(state, "abc123") is None
