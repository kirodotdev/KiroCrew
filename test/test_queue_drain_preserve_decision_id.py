"""A merged queue drain keeps every ``spec_decision_delivery_id`` it folds.

A spec decision answer that arrives while the slot is busy is queued, carrying
``meta.spec_decision_delivery_id`` (the id its outbox row matches on in
``decision_outbox._deliver_pending_decision``). When two answers are queued the
drain can fold them into one user row, and the row's meta is the union of the
entries' meta. ``spec_decision_delivery_id`` is a scalar, so a plain union keeps
only the last writer -- the earlier answer's outbox record then finds no row
carrying its id and reports the answer as un-relayed, so it is dispatched again.

These pins cover the merged-row shape (``spec_decision_delivery_ids`` names every
id the row stands for), the single-entry case (unchanged scalar shape), and the
matcher reading both the scalar and the list.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from chat_test_helpers import _make_state

_ID_A = "delivery-a"
_ID_B = "delivery-b"


async def _drain_once(state, slot) -> None:
    from kiro_crew.dashboard import chat_runner

    with (
        patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
        patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
    ):
        assert await chat_runner._start_next_queued_turn(state, slot) is True


def _user_rows(slot) -> list[dict]:
    return [m for m in slot.messages if m.get("role") == "user"]


def _queue_decision_answer(slot, text: str, delivery_id: str) -> None:
    """Mimic runtime._dispatch_turn's busy branch for a decision answer.

    It queues with ``meta`` carrying the decision delivery id next to the
    containment snapshot and marks the entry user-origin, which is what lets two
    such answers merge into one drained row.
    """
    slot.queue_append(
        text,
        meta={"spec_decision_delivery_id": delivery_id, "app": True},
        directive_user_origin=True,
    )


@pytest.mark.asyncio
async def test_merged_row_names_every_decision_delivery_id(tmp_path, monkeypatch):
    """Two answers folded into one row: the row names both ids, so each answer's
    outbox record can find a row carrying its id."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.subagents = None
    slot = state.get_or_create_slot("busy-chat")

    _queue_decision_answer(slot, "answer one", _ID_A)
    _queue_decision_answer(slot, "answer two", _ID_B)

    from kiro_crew.dashboard import chat_runner

    cfg = MagicMock()
    cfg.load.return_value.dashboard.merge_queued_messages = True
    monkeypatch.setattr(chat_runner, "KiroCrewConfig", cfg)
    await _drain_once(state, slot)

    rows = _user_rows(slot)
    assert rows
    meta = rows[-1].get("meta") or {}
    assert "2 queued messages merged" in rows[-1].get("content", "")
    assert meta.get("spec_decision_delivery_ids") == [_ID_A, _ID_B]
    # The scalar stays present (last writer) so a reader of just the scalar key
    # is never left without an id when any entry had one.
    assert meta.get("spec_decision_delivery_id") in (_ID_A, _ID_B)


@pytest.mark.asyncio
async def test_single_entry_keeps_the_scalar_shape(tmp_path, monkeypatch):
    """One queued answer: the row keeps the plain scalar, no list."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.subagents = None
    slot = state.get_or_create_slot("busy-chat")

    _queue_decision_answer(slot, "only answer", _ID_A)

    from kiro_crew.dashboard import chat_runner

    cfg = MagicMock()
    cfg.load.return_value.dashboard.merge_queued_messages = True
    monkeypatch.setattr(chat_runner, "KiroCrewConfig", cfg)
    await _drain_once(state, slot)

    rows = _user_rows(slot)
    assert rows
    meta = rows[-1].get("meta") or {}
    assert meta.get("spec_decision_delivery_id") == _ID_A
    assert "spec_decision_delivery_ids" not in meta


def test_matcher_reads_both_scalar_and_list():
    """The outbox already-relayed scan matches a delivery id against both the
    scalar key and the merged list, so a folded earlier answer is still seen."""
    from kiro_crew.apps.builtins.spec_builder.backend.orchestration.decision_outbox import (
        _row_carries_delivery_id,
    )

    # Scalar match (single-entry row, the common case).
    assert _row_carries_delivery_id({"meta": {"spec_decision_delivery_id": _ID_A}}, _ID_A)
    # List match: the earlier id on a merged row, which the scalar lost.
    assert _row_carries_delivery_id(
        {
            "meta": {
                "spec_decision_delivery_id": _ID_B,
                "spec_decision_delivery_ids": [_ID_A, _ID_B],
            }
        },
        _ID_A,
    )
    # No false positive.
    assert not _row_carries_delivery_id({"meta": {"spec_decision_delivery_id": _ID_B}}, _ID_A)
    assert not _row_carries_delivery_id({"meta": {}}, _ID_A)
    assert not _row_carries_delivery_id({}, _ID_A)
