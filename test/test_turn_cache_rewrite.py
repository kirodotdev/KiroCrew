"""A turn that rebuilt the prompt cache says so in its footer stats.

``cache_rewritten`` compares the cache-write count with how much the context grew:
a warm cache writes only the new tokens, a cold one writes the prefix again. The
table uses the counts of a real captured session (the first row and the compaction
row) and realistic ones for the rest.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state
from dashboard_chat_helpers import _provider_mock

from kiro_crew.acp.types import TurnUsage
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import _attach_turn_stats, turn_stats_meta
from kiro_crew.dashboard.state import _ChatSlot, row_mid
from kiro_crew.providers.base import (
    EVENT_COMPACTION_STATUS,
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    LLMEvent,
)


def _rule():
    """The rule module, imported when a test runs so a missing module fails that test."""
    return importlib.import_module("kiro_crew.dashboard.cache_rewrite")


class _Provider:
    """A provider that names its backend session, as the ACP providers do."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id


class _Unnamed:
    """A provider whose session name cannot be read."""

    @property
    def session_id(self) -> str:
        raise RuntimeError("no session handle")


_SESSION_A = _Provider("session-a")
_SESSION_B = _Provider("session-b")


# (name, previous context, context now, cache write, flagged). The rule reads no
# cache-read count: a tool loop counts the same prefix once per model call.
_CASES = [
    ("first turn of a session", 0, 31_158, 13_376, False),
    ("small growing turn", 31_158, 31_250, 92, False),
    ("turn that reads a 20k-token file", 25_000, 45_000, 20_000, False),
    ("warm tool loop, four calls", 60_000, 80_000, 20_000, False),
    ("idle expiry, one call", 60_000, 61_500, 43_725, True),
    ("tool loop whose first call rewrote", 60_000, 80_000, 80_000, True),
    ("after a compaction", 31_250, 26_171, 8_386, False),
    ("no cache write reported (zeros, or reads only)", 60_000, 61_500, 0, False),
    ("backend reporting no context size", 60_000, 0, 43_725, False),
    ("prefix one token under the floor", 19_999, 19_999, 19_999, False),
    ("prefix at the floor, write at half", 20_000, 20_000, 10_000, True),
    ("prefix at the floor, write one under half", 20_000, 20_000, 9_999, False),
]


@pytest.mark.parametrize(
    ("prev", "ctx", "write", "expected"),
    [c[1:] for c in _CASES],
    ids=[c[0] for c in _CASES],
)
def test_the_rule_over_the_cases(prev, ctx, write, expected) -> None:
    assert _rule().cache_rewritten(prev, ctx, write) is expected


def test_the_first_turn_after_a_restart_is_not_flagged_then_the_next_can_be() -> None:
    slot = _ChatSlot("restart")
    assert slot._prev_turn_ctx_used == 0
    assert _rule().note_turn_cache(slot, _SESSION_A, 60_000, 42_225) is False
    assert slot._prev_turn_ctx_used == 60_000
    assert _rule().note_turn_cache(slot, _SESSION_A, 61_500, 43_725) is True
    assert slot._prev_turn_ctx_used == 61_500


def test_a_turn_with_no_context_reading_keeps_the_remembered_size() -> None:
    slot = _ChatSlot("no-reading")
    _rule().note_turn_cache(slot, _SESSION_A, 60_000, 42_225)
    assert _rule().note_turn_cache(slot, _SESSION_A, 0, 43_725) is False
    assert slot._prev_turn_ctx_used == 60_000


def test_a_compaction_turn_is_not_flagged_and_sets_the_new_base() -> None:
    slot = _ChatSlot("compact")
    _rule().note_turn_cache(slot, _SESSION_A, 60_000, 42_225)
    slot._compact_command_turn = True
    # The counts alone would flag it (the summary is written fresh).
    assert _rule().cache_rewritten(60_000, 30_000, 30_000) is True
    assert _rule().note_turn_cache(slot, _SESSION_A, 30_000, 30_000) is False
    assert slot._prev_turn_ctx_used == 30_000


def test_a_turn_in_a_new_backend_session_is_not_flagged() -> None:
    # A reset or replaced conversation runs in a new backend session whose cache
    # starts empty, so its first turn writes everything once.
    slot = _ChatSlot("reset")
    _rule().note_turn_cache(slot, _SESSION_A, 60_000, 42_225)
    # In the same session these counts would read as a rewrite.
    assert _rule().cache_rewritten(60_000, 40_000, 22_185) is True
    assert _rule().note_turn_cache(slot, _SESSION_B, 40_000, 22_185) is False
    assert slot._prev_turn_ctx_used == 40_000
    assert slot._prev_turn_ctx_session == "session-b"
    # The new session is the baseline from here on.
    assert _rule().note_turn_cache(slot, _SESSION_B, 41_500, 40_225) is True


def test_a_session_name_that_cannot_be_read_counts_as_no_name() -> None:
    slot = _ChatSlot("unnamed")
    assert _rule().note_turn_cache(slot, _Unnamed(), 60_000, 42_225) is False
    assert slot._prev_turn_ctx_session == ""
    # A provider with no session name compares as the same unnamed session.
    assert _rule().note_turn_cache(slot, object(), 61_500, 43_725) is True


def test_the_payload_carries_the_key_only_for_a_rewrite() -> None:
    assert turn_stats_meta(4200, 1.5, 0.0, "m", cache_rewrite=True) == {
        "elapsed_ms": 4200,
        "credits": 1.5,
        "model": "m",
        "cache": "rewrite",
    }
    plain = {"elapsed_ms": 4200, "credits": 1.5, "model": "m"}
    assert turn_stats_meta(4200, 1.5, 0.0, "m") == plain
    assert turn_stats_meta(4200, 1.5, 0.0, "m", cache_rewrite=False) == plain
    assert turn_stats_meta(0, 1.5, 0.0, "m", cache_rewrite=True) is None


def test_the_attached_footer_carries_the_key_only_for_a_rewrite() -> None:
    slot = _ChatSlot("attach")
    slot.append("assistant", "done.", "msg msg-a", broadcast=False)
    assert _attach_turn_stats(slot, 5000, 1.0, 0.0, cache_rewrite=True)
    assert slot.messages[-1]["meta"]["turn_stats"]["cache"] == "rewrite"
    slot = _ChatSlot("attach-plain")
    slot.append("assistant", "done.", "msg msg-a", broadcast=False)
    assert _attach_turn_stats(slot, 5000, 1.0, 0.0)
    assert "cache" not in slot.messages[-1]["meta"]["turn_stats"]


def _turn(ctx: int, read: int, write: int, stop_reason: str = "end_turn"):
    usage = TurnUsage(
        input_tokens=3,
        output_tokens=40,
        cache_read_tokens=read,
        cache_creation_tokens=write,
        duration_ms=5000,
    )
    return ctx, [
        LLMEvent(kind=EVENT_TEXT_CHUNK, text=f"reply at {ctx}"),
        LLMEvent(kind=EVENT_COMPLETE, stop_reason=stop_reason, usage=usage),
    ]


async def _run_turns(tmp_path, monkeypatch, turns, sessions=None):
    """Run each ``(ctx, events)`` as one real chat turn on one slot; return the slot.

    *sessions* names the backend session each turn runs in; by default all of
    them run in one.
    """
    from kiro_crew.dashboard.chat import _run_chat

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.context_builder = None
    state.consolidator = None
    state._hook_store = None
    state._yolo = False
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_runner.generate_session_summary", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner.read_turn_model", lambda c: "served-model")
    slot = state.get_or_create_slot("cache-rewrite")
    client = _provider_mock()
    client.context_usage_pct = MagicMock(return_value=10.0)
    client.context_window_tokens = MagicMock(return_value=200_000)
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    for index, (ctx, events) in enumerate(turns):
        client.context_used_tokens = MagicMock(return_value=ctx)
        client.session_id = sessions[index] if sessions else "session-1"

        async def _stream(msg, _events=events):
            for ev in _events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        await _run_chat(state, slot, "go on")
    return slot


def _footers(slot) -> list[dict]:
    return [
        m["meta"]["turn_stats"]
        for m in slot.messages
        if m.get("role") == "assistant" and "turn_stats" in (m.get("meta") or {})
    ]


@pytest.mark.asyncio
async def test_a_real_turn_after_an_idle_expiry_carries_the_key(tmp_path, monkeypatch) -> None:
    slot = await _run_turns(
        tmp_path,
        monkeypatch,
        [
            _turn(60_000, 17_775, 42_225),  # first turn of the chat
            _turn(61_500, 17_775, 43_725),  # idle expiry: the prefix is written again
            _turn(61_650, 61_500, 150),  # warm again
        ],
    )
    footers = _footers(slot)
    assert [f.get("cache") for f in footers] == [None, "rewrite", None]
    assert "cache" not in footers[0] and "cache" not in footers[2]


@pytest.mark.asyncio
async def test_a_synthesized_completion_is_never_flagged(tmp_path, monkeypatch) -> None:
    ctx, events = _turn(61_500, 17_775, 43_725)
    events[-1].synthetic_completion = True
    slot = await _run_turns(tmp_path, monkeypatch, [_turn(60_000, 17_775, 42_225), (ctx, events)])
    assert all("cache" not in f for f in _footers(slot))
    assert slot._prev_turn_ctx_used == 61_500


@pytest.mark.asyncio
async def test_a_turn_that_compacted_automatically_is_not_flagged(tmp_path, monkeypatch) -> None:
    # The session compacts at the start of the turn: the context halves and the
    # new prefix is written once, which the rule alone reads as a rewrite. The
    # next turn reads the compacted prefix warm.
    ctx, events = _turn(30_000, 0, 20_000)
    events.insert(0, LLMEvent(kind=EVENT_COMPACTION_STATUS, text="completed", title="summary"))
    slot = await _run_turns(
        tmp_path,
        monkeypatch,
        [_turn(60_000, 17_775, 42_225), (ctx, events), _turn(30_150, 30_000, 150)],
    )
    footers = _footers(slot)
    assert len(footers) == 3
    assert all("cache" not in f for f in footers)
    assert slot._prev_turn_ctx_used == 30_150


@pytest.mark.asyncio
async def test_a_real_turn_after_a_conversation_reset_is_not_flagged(tmp_path, monkeypatch) -> None:
    # The second turn reads a large file in a fresh native conversation (a reset
    # without replay): growth against the discarded conversation would be zero.
    slot = await _run_turns(
        tmp_path,
        monkeypatch,
        [_turn(60_000, 17_775, 42_225), _turn(40_000, 17_775, 22_185)],
        sessions=["session-1", "session-2"],
    )
    assert all("cache" not in f for f in _footers(slot))
    assert slot._prev_turn_ctx_session == "session-2"


def _cut_turn(slot: _ChatSlot, flagged: bool) -> SimpleNamespace:
    """A turn its completion billed and judged, then cut before the end-of-turn attach."""
    return SimpleNamespace(
        slot=slot,
        state=MagicMock(),
        client=object(),
        _ttft_visible=SimpleNamespace(ms=900),
        _turn_usage_persisted=True,
        _turn_stats_captured=True,
        _turn_boundary_set=True,
        _turn_footer_attached=False,
        _turn_credits=1.4,
        _turn_cost_usd=0.0,
        _turn_elapsed_ms=13_000,
        _turn_model="served-model",
        _turn_cache_rewrite=flagged,
        _turn_msg_boundary=0,
        _turn_start_mid=None,
    )


@pytest.mark.parametrize("flagged", [True, False])
def test_a_turn_cut_after_its_completion_keeps_the_flag_in_its_footer(flagged) -> None:
    # The turn ceiling's acknowledged stop, or a recovery arm, can cut a turn
    # after EVENT_COMPLETE billed and judged it; the seam then writes the footer
    # the completion would have written.
    slot = _ChatSlot(f"cut-{flagged}")
    reply = slot.append("assistant", "partial reply", "msg msg-a", broadcast=False)
    slot._turn_reply_mids.append(row_mid(reply))
    stamped, owed = chat_runner._stamp_abnormal_turn_footer(_cut_turn(slot, flagged))
    assert stamped is True and owed is None
    stats = slot.messages[-1]["meta"]["turn_stats"]
    assert stats["credits"] == 1.4
    assert stats.get("cache") == ("rewrite" if flagged else None)
