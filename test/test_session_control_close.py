"""Tests for ``close_target``, the session-control close that archives a tab.

Covers closing a peer and the caller itself, including a fenced session whose
``_created_by`` names another, the channel-link refusal that still holds when a
caller closes itself, and a ``SlotCloseError`` surfacing as a
``SessionControlError`` with the same code and status, audited as denied.

Also pins the SEL prewarm before authorization, no ``await`` between
``authorize_target`` and ``close_slot``, and the pre-pop re-check: a target that
gains a channel mirror is refused and kept, a key re-minted onto another session
aborts with ``target_replaced``, and in ``chat_handlers.close_slot`` an abort
leaves the slot unpopped and the check runs synchronously after nudge retirement.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _busy,
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _peer_target,
    _slot,
)

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key

# ── Close (archive) ──────────────────────────────────────────────────────────


def test_close_archives_a_peer_and_removes_the_slot(tmp_path):
    """The happy path end to end: a peer in the caller's workspace is closed via
    the real ``close_slot`` extraction, so the slot leaves ``_slots`` and the
    per-tab session is torn down — exactly what the tab ✕ does."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)

    result = asyncio.run(sc.close_target(state, caller_session_key=_key(caller), target=target.key))

    assert result == {"ok": True, "target": target.key}
    assert target.key not in state._slots
    # The per-tab kiro-cli session is torn down through the shared close path.
    state.sessions.remove.assert_awaited()


def test_close_archives_the_caller_itself(tmp_path):
    """A session that has finished its one job can close its own tab: close waives
    the self-target refusal, on its admission gate AND its pre-pop re-check, and
    runs the same archival a peer close does. Its own running turn is the turn
    asking for the close, and it is cancelled through the shared teardown."""
    state = _make_state(tmp_path)
    caller = _busy(_slot(state, "chat-1"))
    running_turn = caller.task

    result = asyncio.run(sc.close_target(state, caller_session_key=_key(caller), target=caller.key))

    assert result == {"ok": True, "target": caller.key}
    assert caller.key not in state._slots
    running_turn.cancel.assert_called()
    state.sessions.remove.assert_awaited()


def test_close_lets_a_session_another_session_created_close_itself(tmp_path):
    """The reported case: a session opened by another one (``_created_by`` names
    its creator) and fenced to what it created can still close ITSELF, because
    addressing itself reaches no peer -- the same waiver release relies on."""
    state = _make_state(tmp_path)
    parent = _slot(state, "chat-1")
    child = _slot(state, "chat-2")
    child._created_by = parent.key

    result = asyncio.run(
        sc.close_target(state, caller_session_key=_key(child), target=child.key, caller_fenced=True)
    )

    assert result == {"ok": True, "target": child.key}
    assert child.key not in state._slots
    assert parent.key in state._slots


def test_close_still_refuses_a_channel_linked_caller_closing_itself(tmp_path):
    """The self waiver is the self-target refusal only: a caller that may not use
    session control at all is still refused, and its slot survives."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller.linked_session_key = "slack:1786300000.000200"

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.close_target(state, caller_session_key=_key(caller), target=caller.key))
    # The caller IS the target, so the target-side link refusal answers first.
    assert exc.value.code == "linked_session_target"
    assert caller.key in state._slots


def test_close_maps_a_close_failure_to_its_code(tmp_path, monkeypatch):
    """A ``SlotCloseError`` from the shared path surfaces as a
    ``SessionControlError`` carrying the SAME code and status — so a caller can
    tell "history could not be saved" from a generic failure — and the failed
    close is audited as denied."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)

    from kiro_crew.dashboard import chat_handlers

    async def _boom(_state, _slot, _name, *, pre_pop_check=None):
        raise chat_handlers.SlotCloseError("failed to notify the app", code="app_close_hook_failed")

    audited: list[tuple[str, str]] = []
    real_audit = sc._audit

    def _audit(*, caller_session_key, operation, slot_key, outcome, detail=None):
        audited.append((operation, outcome))
        return real_audit(
            caller_session_key=caller_session_key,
            operation=operation,
            slot_key=slot_key,
            outcome=outcome,
            detail=detail,
        )

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.close_slot", _boom)
    monkeypatch.setattr(sc, "_audit", _audit)

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.close_target(state, caller_session_key=_key(caller), target=target.key))
    assert exc.value.code == "app_close_hook_failed"
    assert exc.value.status == 500
    # The target was NOT removed — the failing close rolled back, and the trail
    # records the attempt as denied.
    assert target.key in state._slots
    assert ("close", "denied") in audited


def test_close_authorizes_then_acts_with_nothing_in_between(tmp_path, monkeypatch):
    """Same adjacency contract stop keeps: the SEL prewarm precedes authorization,
    and no ``await`` separates the gate from the act it authorizes.

    Mutation guard: moving the prewarm below the gate, or slipping an await
    between ``authorize_target`` and ``close_slot``, reorders this list."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    order: list[str] = []

    def _sel():
        order.append("sel")
        return MagicMock()

    real_authorize = sc.authorize_target

    def _authorize(*a, **kw):
        order.append("authorize")
        return real_authorize(*a, **kw)

    async def _fake_close(_state, _slot, _name, *, pre_pop_check=None):
        order.append("close")

    monkeypatch.setattr(sc, "sel", _sel)
    monkeypatch.setattr(sc, "authorize_target", _authorize)
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.close_slot", _fake_close)

    asyncio.run(sc.close_target(state, caller_session_key=_key(caller), target=target.key))

    assert order[:1] == ["sel"], f"the prewarm must precede authorization; got {order}"
    assert (
        order.index("close") - order.index("authorize") == 1
    ), f"something ran between the gate and the act it authorizes: {order}"


def test_close_reauthorizes_at_the_point_of_no_return(tmp_path, monkeypatch):
    """A target that gains a channel mirror DURING close_slot's awaits is refused
    at the pre-pop re-check, so the now-channel-backed session is not archived.

    This is the GPT-flagged race: `authorize_target` runs before `close_slot`,
    whose nudge-retirement + app-hook awaits are a window in which the target can
    gain a mirror link — the same class `create_session` re-gates for.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)

    async def _fake_close(_state, _slot, _name, *, pre_pop_check=None):
        # Simulate the race: the target gains an outbound channel mirror while
        # close_slot is mid-await, then the point-of-no-return check runs.
        state.sessions.set_mirror_link(slot_history_key(target), "C123", "T1")
        assert pre_pop_check is not None, "close_target must arm a pre-pop re-check"
        pre_pop_check()  # re-runs authorize_target -> raises for the new mirror
        raise AssertionError("close must not reach the pop after a stale-auth abort")

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.close_slot", _fake_close)

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.close_target(state, caller_session_key=_key(caller), target=target.key))
    # The re-check's mirrored_target refusal round-trips with its own 403, not a
    # generic close failure, and the session survives.
    assert exc.value.code == "mirrored_target"
    assert exc.value.status == 403
    assert target.key in state._slots


def test_close_slot_pre_pop_abort_rolls_back_and_does_not_pop(tmp_path):
    """A `pre_pop_check` that raises `SlotCloseError` aborts the close at the point
    of no return: the slot stays in `_slots` and the per-tab session is never torn
    down. The human ✕ path (pre_pop_check=None) is unaffected."""
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")

    def _abort():
        raise chat_handlers.SlotCloseError(
            "target became unreachable", code="mirrored_target", status=403
        )

    with pytest.raises(chat_handlers.SlotCloseError) as exc:
        asyncio.run(chat_handlers.close_slot(state, slot, slot.key, pre_pop_check=_abort))

    assert exc.value.code == "mirrored_target"
    assert slot.key in state._slots  # not popped
    assert slot.is_closing is False  # failed close must not fence future monitor admission
    state.sessions.remove.assert_not_awaited()  # teardown never ran


def test_close_aborts_if_the_key_was_reminted_during_the_close(tmp_path, monkeypatch):
    """A concurrent close+reopen re-mints the same key onto a DIFFERENT session
    while close_slot awaits. The pre-pop re-check compares slot identity (not mere
    presence) and aborts with `target_replaced`, so close_slot never pops the
    replacement — the GPT-flagged data-corruption race."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    original_key = target.key

    async def _fake_close(_state, _slot, _name, *, pre_pop_check=None):
        # Simulate the re-mint: drop the original and put a fresh slot object
        # under the SAME key, then run the point-of-no-return check.
        state._slots.pop(original_key, None)
        replacement = state.get_or_create_slot(original_key)
        assert replacement is not target
        assert pre_pop_check is not None
        pre_pop_check()  # authorize_target resolves the replacement -> identity mismatch
        raise AssertionError("close must not pop after a re-mint abort")

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.close_slot", _fake_close)

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.close_target(state, caller_session_key=_key(caller), target=original_key))
    assert exc.value.code == "target_replaced"
    assert exc.value.status == 409
    # The replacement survives.
    assert original_key in state._slots


def test_close_slot_runs_the_pre_pop_check_synchronously_after_retirement(tmp_path, monkeypatch):
    """`pre_pop_check` runs SYNCHRONOUSLY after the (awaited) nudge retirement and
    immediately before the pop, so there is no suspension between the last
    retirement, the re-check, and the removal — a concurrent `monitor_start`
    cannot arm a loop in a window that would leave a timer to rehydrate the
    archived tab, and a mirror/link cannot land between the re-authorization and
    the archival.

    Asserted by the call order (retire is the last AWAIT; the sync check follows,
    then the pop) and by the callback being a plain synchronous function."""
    import inspect

    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    order: list[str] = []

    async def _retire(_name):
        order.append("retire")
        return None

    def _check():  # synchronous by contract — no await before the pop
        order.append("check")

    assert not inspect.iscoroutinefunction(_check)
    monkeypatch.setattr(chat_handlers, "_retire_slot_nudge_loop", _retire)

    asyncio.run(chat_handlers.close_slot(state, slot, slot.key, pre_pop_check=_check))

    # Retirement (the last await) then the synchronous check, then the pop. No
    # second retirement is needed because the check itself suspends nothing.
    assert order == ["retire", "check"], order
    assert slot.key not in state._slots  # closed
