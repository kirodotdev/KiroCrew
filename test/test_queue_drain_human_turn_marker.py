"""A human send that was queued while the slot was busy keeps its human-turn marker.

The Slack sessions list ranks by the last HUMAN turn, and the ranking gate
(``chat_persistence``) counts a row only when its ``meta`` carries
:data:`~kiro_crew.history.HUMAN_TURN_META_KEY` set to ``True``. The direct
``/api/chat`` path stamps that marker on the user row whenever the send is
human-authored (``chat_handlers``: ``if not request_app``). A message that
arrives while the slot is busy is QUEUED instead and later written by the drain
in ``chat_runner``, which -- without the stamp under test -- wrote an UNMARKED
user row. The gate would then skip a genuine human turn and ``last_user_at``
would stale, misranking the very list this feature introduces.

These pins cover the reachable path the review flagged: a human busy-slot drain
carries the marker; an app-origin drain does not (fail-closed); and the marker
never rides an ``inject`` row (cron/recovery/app), which is a different provenance.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.history import HUMAN_TURN_META_KEY

_TEXT = "typed while the agent was working"


async def _drain_once(state, slot) -> None:
    from kiro_crew.dashboard import chat_runner

    with (
        patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
        patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
    ):
        assert await chat_runner._start_next_queued_turn(state, slot) is True


def _user_rows(slot) -> list[dict]:
    return [m for m in slot.messages if m.get("role") == "user"]


def _inject_rows(slot) -> list[dict]:
    return [m for m in slot.messages if m.get("role") == "inject"]


class TestDrainedHumanTurnMarker:
    @pytest.mark.asyncio
    async def test_human_busy_slot_drain_carries_the_marker(self, tmp_path, monkeypatch):
        """The reachable finding: a human types while the slot is busy, the send
        is queued and later drained -- the drained user row must carry the
        human-turn marker so the last-human-turn ranking counts it."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.subagents = None
        slot = state.get_or_create_slot("busy-chat")

        from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

        # ``directive_user_origin=True`` is the ``not request_app`` provenance the
        # HTTP handler carries onto the entry for a genuine human send.
        queue_for_next_turn(state, slot, _TEXT, directive_user_origin=True)
        await _drain_once(state, slot)

        rows = _user_rows(slot)
        assert rows, "the drain must have written a user row for the queued send"
        meta = rows[-1].get("meta") or {}
        assert meta.get(HUMAN_TURN_META_KEY) is True, (
            "a queued human turn drains as an unmarked row without the stamp, and "
            "the last-human-turn ranking gate would then skip it"
        )

    @pytest.mark.asyncio
    async def test_app_origin_drain_is_not_marked(self, tmp_path, monkeypatch):
        """Fail-closed: an app-initiated send (``directive_user_origin`` false)
        that drains must NOT be marked, or app traffic would forge a human turn
        and displace human sessions in the ranking."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.subagents = None
        slot = state.get_or_create_slot("busy-chat")

        from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

        queue_for_next_turn(state, slot, _TEXT, directive_user_origin=False)
        await _drain_once(state, slot)

        rows = _user_rows(slot)
        assert rows
        meta = rows[-1].get("meta") or {}
        assert HUMAN_TURN_META_KEY not in meta, (
            "an app-origin drain must stay unmarked so it cannot advance the "
            "human-turn ranking stamp"
        )

    @pytest.mark.asyncio
    async def test_default_origin_drain_is_not_marked(self, tmp_path, monkeypatch):
        """An entry with no explicit origin defaults to non-human, so it stays
        unmarked -- the marker is added only on proven human provenance."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.subagents = None
        slot = state.get_or_create_slot("busy-chat")

        from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

        queue_for_next_turn(state, slot, _TEXT)
        await _drain_once(state, slot)

        rows = _user_rows(slot)
        assert rows
        assert HUMAN_TURN_META_KEY not in (rows[-1].get("meta") or {})
