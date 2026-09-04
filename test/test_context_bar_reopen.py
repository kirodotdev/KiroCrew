"""Context meter must be populated when a session is REOPENED, not only mid-turn.

``context_usage`` WS frames are turn-scoped, so before this the slot-detail
response carried no usage at all and reopening a session rendered an empty bar
until the user sent a message. Two tiers are covered here: a live pooled
provider (exact) and a persisted snapshot for a session whose ACP process is
gone (last-known, flagged stale).
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.acp.types import AcpPromptStats
from kiro_crew.dashboard.state import DashboardState
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


@pytest.fixture(autouse=True)
def _isolate_snapshot_file(tmp_path, monkeypatch):
    """Point the snapshot sidecar at tmp_path for every test in this module.

    Without this, ensure_context_snapshots_loaded() reads the developer's real
    ~/.kiro/crew/context_snapshots.json and a stray entry there would change
    what these tests observe.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)


async def _detail(state: DashboardState, slot_key: str) -> dict:
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.get(f"/api/chat/slots/{slot_key}")
        assert resp.status == 200
        return await resp.json()


# ── Tier 1: live pooled session ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_detail_carries_live_provider_usage(tmp_path):
    state = _make_state(tmp_path)
    state.get_or_create_slot("s1")
    state.sessions.get_provider = MagicMock(return_value=_provider(88000, 200000, 44.0))

    body = await _detail(state, "s1")

    assert body["context_pct"] == 44.0
    assert body["context_used_tokens"] == 88000
    assert body["context_window_tokens"] == 200000
    assert body["context_stale"] is False


@pytest.mark.asyncio
async def test_pct_alone_is_a_usable_reading(tmp_path):
    # The COMMON live case: kiro-cli reports contextUsagePercentage with no
    # usage_update, so the session knows it is 11.4% full and knows neither
    # token count. The bar only needs the pct; the frontend supplies a
    # model-derived window.
    state = _make_state(tmp_path)
    state.get_or_create_slot("s1")
    state.sessions.get_provider = MagicMock(return_value=_provider(0, 0, 11.4))

    body = await _detail(state, "s1")

    assert body["context_pct"] == 11.4
    assert body["context_stale"] is False
    assert "context_window_tokens" not in body
    assert "context_used_tokens" not in body


@pytest.mark.asyncio
async def test_detail_omits_usage_when_nothing_measured(tmp_path):
    # A resident session that has not had a turn reports 0% and no tokens —
    # indistinguishable from a fresh session, and it renders an empty bar
    # either way, so it is reported as "no reading" rather than a measurement.
    state = _make_state(tmp_path)
    state.get_or_create_slot("s1")
    state.sessions.get_provider = MagicMock(return_value=_provider(0, 0, 0.0))

    body = await _detail(state, "s1")

    assert "context_pct" not in body
    assert "context_window_tokens" not in body


# ── Tier 2: cold session, persisted snapshot ───────────────────────────────


@pytest.mark.asyncio
async def test_detail_falls_back_to_persisted_snapshot(tmp_path):
    # The idle-timeout / gateway-restart case: no provider in the pool, so the
    # snapshot recorded at the last turn's end is the only reading available.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    slot.model = "claude-opus-5"
    state.sessions.get_provider = MagicMock(return_value=None)
    state._context_snapshots = {
        "s1": {
            "pct": 31.5,
            "used_tokens": 63000,
            "window_tokens": 200000,
            "model": "claude-opus-5",
        }
    }

    body = await _detail(state, "s1")

    assert body["context_pct"] == 31.5
    assert body["context_window_tokens"] == 200000
    # Flagged stale: it predates the resume, and the next turn overwrites it.
    assert body["context_stale"] is True
    # The count is withheld, not merely ignored downstream: no process measured
    # it, so any consumer of this endpoint would otherwise render a
    # never-measured figure as measured. The bar shows a ~ approximation
    # derived from pct instead.
    assert "context_used_tokens" not in body


@pytest.mark.asyncio
async def test_snapshot_survives_a_gateway_restart(tmp_path):
    # End-to-end across process death: the writer's file is what a cold start
    # reads back. A state that has never touched the map must load it from disk
    # rather than starting empty.
    before = _make_state(tmp_path)
    slot = before.get_or_create_slot("s1")
    slot.model = "claude-opus-5"
    before.broadcast_ws = MagicMock()
    before.broadcast_context_usage(
        "s1", {"slot": "s1", "pct": 44.0, "used_tokens": 88000, "window_tokens": 200000}
    )
    _flush(before)  # the shutdown / periodic flush is what reaches disk

    after = _make_state(tmp_path)
    restored = after.get_or_create_slot("s1")
    restored.model = "claude-opus-5"
    after.sessions.get_provider = MagicMock(return_value=None)

    body = await _detail(after, "s1")

    assert body["context_pct"] == 44.0
    assert body["context_window_tokens"] == 200000
    assert body["context_stale"] is True


@pytest.mark.asyncio
async def test_snapshot_discarded_when_model_changed_while_cold(tmp_path):
    # The counts are denominated in the OLD model's window. Showing them
    # against the new one would misreport usage, so they are dropped and the
    # frontend falls back to the model-derived window at 0%.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    slot.model = "gpt-5.6-sol"
    state.sessions.get_provider = MagicMock(return_value=None)
    state._context_snapshots = {
        "s1": {
            "pct": 31.5,
            "used_tokens": 63000,
            "window_tokens": 1000000,
            "model": "claude-opus-5",
        }
    }

    body = await _detail(state, "s1")

    assert "context_pct" not in body
    assert "context_window_tokens" not in body


@pytest.mark.asyncio
async def test_detail_omits_usage_for_fresh_session(tmp_path):
    # No provider and no snapshot — a genuinely new session. The bar stays
    # empty, which is correct rather than a bug.
    state = _make_state(tmp_path)
    state.get_or_create_slot("s1")
    state.sessions.get_provider = MagicMock(return_value=None)

    body = await _detail(state, "s1")

    assert "context_pct" not in body
    assert "context_stale" not in body


@pytest.mark.asyncio
async def test_unusable_provider_reading_cannot_break_the_response(tmp_path):
    # A display field must never fail the request that carries the whole
    # transcript. A provider whose accessors return something non-numeric (or
    # raise) degrades to an empty bar, not a 500 that blanks the conversation.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hello", "msg msg-u")
    state.sessions.get_provider = MagicMock(return_value=MagicMock())

    body = await _detail(state, "s1")

    assert [m["content"] for m in body["messages"]] == ["hello"]
    assert "context_pct" not in body


@pytest.mark.asyncio
async def test_corrupt_snapshot_is_ignored(tmp_path):
    # The snapshot file is on disk and hand-editable; a garbage entry must read
    # as "no reading" rather than reaching json_response.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    slot.model = "claude-opus-5"
    state.sessions.get_provider = MagicMock(return_value=None)
    state._context_snapshots = {
        "s1": {"pct": "lots", "used_tokens": None, "window_tokens": "big", "model": "claude-opus-5"}
    }

    body = await _detail(state, "s1")

    assert "context_pct" not in body
    assert "context_window_tokens" not in body


# ── The write side: broadcast_context_usage is the single writer ───────────


def _snapshot(state: DashboardState, slot_key: str = "s1") -> object:
    return state._context_snapshots.get(slot_key)


def _flush(state: DashboardState) -> None:
    """Run the off-loop flush that the periodic executor pass performs."""
    state._persist_context_snapshots()


def test_broadcast_records_the_reading_and_still_broadcasts(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    slot.model = "claude-opus-5"
    state.broadcast_ws = MagicMock()

    state.broadcast_context_usage(
        "s1", {"slot": "s1", "pct": 44.0, "used_tokens": 88000, "window_tokens": 200000}
    )

    # The broadcast is not sacrificed to the persistence.
    state.broadcast_ws.assert_called_once()
    assert state.broadcast_ws.call_args[0][0] == "context_usage"
    assert _snapshot(state) == {
        "pct": 44.0,
        "used_tokens": 88000,
        "window_tokens": 200000,
        "model": "claude-opus-5",
    }


def test_reset_frame_records_the_post_compaction_truth(tmp_path):
    # Compaction genuinely drops usage toward zero, and its frame carries pct
    # with no tokens. Storing pct 0 is the new truth.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    slot.model = "claude-opus-5"
    state.broadcast_ws = MagicMock()
    state.broadcast_context_usage(
        "s1", {"slot": "s1", "pct": 44.0, "used_tokens": 88000, "window_tokens": 200000}
    )

    state.broadcast_context_usage("s1", {"slot": "s1", "pct": 0.0, "reset": True})

    assert _snapshot(state) == {"pct": 0.0, "model": "claude-opus-5"}


def test_pct_only_frame_is_recorded(tmp_path):
    # The common frame shape: a pct-only frame must still produce a snapshot,
    # or a reopened session has nothing to restore.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    slot.model = "claude-opus-5"
    state.broadcast_ws = MagicMock()

    state.broadcast_context_usage("s1", {"slot": "s1", "pct": 11.4})

    assert _snapshot(state) == {"pct": 11.4, "model": "claude-opus-5"}


def test_repeating_the_same_reading_leaves_nothing_to_flush(tmp_path, monkeypatch):
    # An unchanged reading must not mark the map dirty, so the periodic flush
    # has no write to do. Asserted on the writer itself rather than on file
    # mtime, which is a proxy a coarse filesystem clock can satisfy vacuously.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    slot.model = "claude-opus-5"
    state.broadcast_ws = MagicMock()
    state.broadcast_context_usage("s1", {"slot": "s1", "pct": 11.4})
    writes = MagicMock()
    monkeypatch.setattr("kiro_crew.dashboard.state.atomic_write", writes)
    _flush(state)
    assert writes.call_count == 1

    state.broadcast_context_usage("s1", {"slot": "s1", "pct": 11.4})
    _flush(state)

    assert writes.call_count == 1  # still one — the repeat was a no-op


def test_ephemeral_slot_leaves_no_snapshot(tmp_path):
    # Incognito/temporary tabs leave no memory behind by contract — the same
    # filter _persist_open_slots applies to its own snapshot.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    slot.model = "claude-opus-5"
    slot.memory_mode = "incognito"
    state.broadcast_ws = MagicMock()

    state.broadcast_context_usage(
        "s1", {"slot": "s1", "pct": 44.0, "used_tokens": 88000, "window_tokens": 200000}
    )

    state.broadcast_ws.assert_called_once()
    assert _snapshot(state) is None


def test_deleted_slots_are_evicted_from_the_file(tmp_path):
    # The map must stay bounded by the open-slot set: a deleted session cannot
    # leave its usage behind to be served to whatever reuses its key.
    state = _make_state(tmp_path)
    for name in ("s1", "s2"):
        state.get_or_create_slot(name).model = "claude-opus-5"
    state.broadcast_ws = MagicMock()
    for name in ("s1", "s2"):
        state.broadcast_context_usage(
            name, {"slot": name, "pct": 10.0, "used_tokens": 20000, "window_tokens": 200000}
        )
    assert _snapshot(state, "s1") is not None

    del state._slots["s1"]
    state.broadcast_context_usage(
        "s2", {"slot": "s2", "pct": 12.0, "used_tokens": 24000, "window_tokens": 200000}
    )
    _flush(state)

    on_disk = json.loads((tmp_path / "context_snapshots.json").read_text())
    assert set(on_disk) == {"s2"}


def test_failed_write_is_retried_on_the_next_flush(tmp_path, monkeypatch):
    # The flush loop runs forever; one transient disk failure must not drop
    # the reading's claim on persistence — the next flush must retry it, and
    # the failure must not escape (a raising flush callee kills the loop).
    state = _make_state(tmp_path)
    state.get_or_create_slot("s1").model = "claude-opus-5"
    state.broadcast_ws = MagicMock()
    state.broadcast_context_usage("s1", {"slot": "s1", "pct": 11.4})

    from kiro_crew.dashboard.state import atomic_write as real_atomic_write

    monkeypatch.setattr(
        "kiro_crew.dashboard.state.atomic_write",
        MagicMock(side_effect=OSError("disk full")),
    )
    _flush(state)  # must swallow, not raise

    monkeypatch.setattr("kiro_crew.dashboard.state.atomic_write", real_atomic_write)
    _flush(state)

    on_disk = json.loads((tmp_path / "context_snapshots.json").read_text())
    assert on_disk["s1"]["pct"] == 11.4


def test_flush_preserves_prior_process_readings_it_has_not_served_yet(tmp_path):
    # A reading recorded by an EARLIER gateway process sits only on disk until
    # something reopens that session. A flush triggered by a NEW reading must
    # merge the file before writing it, or it would overwrite the earlier
    # process's readings with a memory-only view.
    (tmp_path / "context_snapshots.json").write_text(
        json.dumps({"s2": {"pct": 33.0, "model": "claude-opus-5"}})
    )
    state = _make_state(tmp_path)
    for name in ("s1", "s2"):
        state.get_or_create_slot(name).model = "claude-opus-5"
    state.broadcast_ws = MagicMock()
    state.broadcast_context_usage("s1", {"slot": "s1", "pct": 11.4})

    _flush(state)

    on_disk = json.loads((tmp_path / "context_snapshots.json").read_text())
    assert on_disk["s2"] == {"pct": 33.0, "model": "claude-opus-5"}
    assert on_disk["s1"]["pct"] == 11.4


def test_overlapping_flushes_cannot_roll_the_file_back(tmp_path, monkeypatch):
    # Two flush paths exist (periodic executor pass, shutdown save). If they
    # overlap, the slower one must not land an OLDER serialization after a
    # newer one — the persisted meter would roll back with the dirty flag
    # already cleared, so nothing would correct it until a new reading.
    import threading

    from kiro_crew.dashboard.state import atomic_write as real_write

    state = _make_state(tmp_path)
    state.get_or_create_slot("s1").model = "claude-opus-5"
    state.broadcast_ws = MagicMock()
    state.broadcast_context_usage("s1", {"slot": "s1", "pct": 10.0})

    first_write_entered = threading.Event()
    release_first_write = threading.Event()
    writes = 0
    completed_payloads = []

    def stalled_first_write(path, payload, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 1:
            first_write_entered.set()
            assert release_first_write.wait(5)
        real_write(path, payload, **kwargs)
        completed_payloads.append(payload)

    monkeypatch.setattr("kiro_crew.dashboard.state.atomic_write", stalled_first_write)

    flush_a = threading.Thread(target=state._persist_context_snapshots)
    flush_a.start()  # serializes pct 10, then stalls inside the file write
    assert first_write_entered.wait(5)

    state.broadcast_context_usage("s1", {"slot": "s1", "pct": 20.0})
    flush_b = threading.Thread(target=state._persist_context_snapshots)
    flush_b.start()  # must not complete a pct-20 write that pct 10 then buries

    # Give flush B every chance to COMPLETE its write while A is stalled.
    # Serialized flushes keep B blocked so this times out; unserialized
    # flushes let B's pct-20 write land here — which A's stale pct-10 write
    # then buries once released.
    deadline = time.monotonic() + 1.0
    while not completed_payloads and time.monotonic() < deadline:
        time.sleep(0.01)

    release_first_write.set()
    flush_a.join(5)
    flush_b.join(5)

    on_disk = json.loads((tmp_path / "context_snapshots.json").read_text())
    assert on_disk["s1"]["pct"] == 20.0


def test_flush_during_startup_restore_cannot_evict_unrestored_tabs(tmp_path):
    # The startup restore yields to the event loop between tabs, so mid-restore
    # the slot map holds only the tabs restored so far. A flush landing in that
    # window must not prune the rest as "deleted" — their readings would be
    # permanently lost. Same guard _persist_open_slots carries.
    (tmp_path / "context_snapshots.json").write_text(
        json.dumps(
            {
                "s1": {"pct": 11.0, "model": "claude-opus-5"},
                "s2": {"pct": 22.0, "model": "claude-opus-5"},
            }
        )
    )
    state = _make_state(tmp_path)
    state.restoring_open_slots = True
    state.get_or_create_slot("s1").model = "claude-opus-5"  # s2 not restored yet
    state.broadcast_ws = MagicMock()
    state.broadcast_context_usage("s1", {"slot": "s1", "pct": 12.0})

    _flush(state)  # must be a no-op while the restore is in flight

    on_disk = json.loads((tmp_path / "context_snapshots.json").read_text())
    assert on_disk["s2"] == {"pct": 22.0, "model": "claude-opus-5"}

    # The skipped write is owed, not lost: the first flush after the restore
    # completes (s2 now present) writes the new reading without evicting s2.
    state.get_or_create_slot("s2").model = "claude-opus-5"
    state.restoring_open_slots = False
    _flush(state)

    on_disk = json.loads((tmp_path / "context_snapshots.json").read_text())
    assert on_disk["s1"]["pct"] == 12.0
    assert on_disk["s2"] == {"pct": 22.0, "model": "claude-opus-5"}


def test_frame_without_a_usable_pct_is_not_recorded(tmp_path):
    # A frame carrying no numeric pct has nothing to restore from, so it must
    # not create a snapshot file at all.
    state = _make_state(tmp_path)
    state.get_or_create_slot("s1")
    state.broadcast_ws = MagicMock()

    state.broadcast_context_usage("s1", {"slot": "s1"})
    _flush(state)

    state.broadcast_ws.assert_called_once()
    assert not (tmp_path / "context_snapshots.json").exists()
