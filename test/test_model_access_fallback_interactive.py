"""Interactive-path reactive fallback when the account is not entitled to the
configured model.

A new conversation starts on the configured model (commonly the ``auto``
sentinel). When the account is not entitled to it, the prompt-time error is an
ENTITLEMENT rejection, classified terminal -- the two throttle-gated fallback
branches in the chat runner do not fire, so the first reply just fails. The runner
runs the same reactive swap the unattended surfaces run
(``stream_and_collect`` Case 2.5 / ``run_bg_oneliner``): retry ONCE on the first
advertised model the account can run, which is never the failed id and never the
``auto`` sentinel.

These tests pin, through the real ``_run_chat`` ladder:
  - an unentitled named model triggers exactly one ``set_model`` to a non-auto
    advertised id and re-queues the turn on the same session;
  - an account advertising NOTHING accessible surfaces the terminal entitlement
    error with no swap and no re-queue;
  - an unrelated provider error (no rejected model) stays terminal -- the trigger
    is not a catch-all.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.acp.client import AcpError
from kiro_crew.dashboard.chat import _run_chat
from kiro_crew.dashboard.chat_utils import MODEL_UNENTITLED_KIND, SYNTHETIC_RECOVERY_KIND


def _make_state_for_run_chat(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.context_builder = None
    state.consolidator = None
    state._hook_store = None
    state._yolo = False
    return state


def _client_raising(exc: BaseException) -> AsyncMock:
    """A mock ACP client whose FIRST stream raises *exc* before any token, then
    streams a normal completion on the re-queued turn (so a successful swap is
    observable as the replay completing rather than as transient queue state,
    which ``_run_chat`` drains within the same call)."""
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

    client = AsyncMock()
    client.context_usage_pct = MagicMock(return_value=10.0)
    client.set_model = AsyncMock()
    client.served_model = ""
    # No wrapped ACP child, so the session-init OAuth drain is a no-op instead
    # of awaiting an auto-created AsyncMock coroutine.
    client.client = None
    calls = {"n": 0}

    async def _stream(msg):
        calls["n"] += 1
        if calls["n"] == 1:
            raise exc
        for ev in (
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="hello from the fallback model"),
            LLMEvent(kind=EVENT_COMPLETE),
        ):
            yield ev

    client.stream = _stream
    client.stream_command = _stream
    return client


def _client_raising_always(exc: BaseException) -> AsyncMock:
    """A mock ACP client whose stream always raises *exc* (for the paths that
    must NOT swap or re-queue -- the turn ends terminally on the first pass)."""
    client = AsyncMock()
    client.context_usage_pct = MagicMock(return_value=10.0)
    client.set_model = AsyncMock()
    client.served_model = ""
    client.client = None

    async def _stream(msg):
        raise exc
        yield  # pragma: no cover -- makes _stream an async generator

    client.stream = _stream
    client.stream_command = _stream
    return client


def _rejection(model: str, advertised: list[str]) -> AcpError:
    """A prompt-time entitlement rejection exactly as ``_raise_acp_error`` tags it:
    a named model absent from the advertised list, ``transient`` False."""
    exc = AcpError(f"Your account does not have access to model '{model}'.", transient=False)
    exc.rejected_model = model
    exc.advertised = list(advertised)
    return exc


@pytest.mark.asyncio
async def test_unentitled_model_swaps_to_first_advertised_and_requeues(tmp_path, monkeypatch):
    """auto is refused for entitlement while the account advertises real models:
    the runner swaps to the first accessible non-auto id and re-queues once."""
    state = _make_state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    # auto rejected; account is served two concrete models.
    client = _client_raising(_rejection("auto", ["claude-opus-5", "claude-sonnet-5"]))
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    # Capture every queue_insert so the re-queue is observable regardless of the
    # tail-drain that consumes it within the same _run_chat call. _ChatSlot is
    # __slots__-based (its bound method is read-only), so wrap the repository.
    _inserts: list[dict] = []
    _repo = slot._queue_repository
    _real_insert = _repo.queue_insert

    def _record_insert(owner, index, content, kind="", *args, **kw):
        _inserts.append({"content": content, "kind": kind})
        return _real_insert(owner, index, content, kind, *args, **kw)

    monkeypatch.setattr(_repo, "queue_insert", _record_insert)

    await _run_chat(state, slot, "first message")

    # Exactly one swap, to the first advertised model (never auto, never the
    # failed id).
    client.set_model.assert_awaited_once_with("claude-opus-5")
    assert slot._model_access_fallback_used is True

    # A visible, persisted notice names both the refused and the substitute id.
    notices = [m for m in slot.messages if m.get("role") == "notice"]
    assert any("auto" in m["content"] and "claude-opus-5" in m["content"] for m in notices), notices

    # The turn was re-queued once on the same session as a synthetic recovery,
    # not surfaced as a terminal entitlement error.
    recovery_inserts = [
        i
        for i in _inserts
        if i["kind"] == SYNTHETIC_RECOVERY_KIND and i["content"] == "first message"
    ]
    assert len(recovery_inserts) == 1, _inserts
    errors = [m for m in slot.messages if m.get("role") == "error"]
    assert not any(
        (m.get("meta") or {}).get("kind") == MODEL_UNENTITLED_KIND for m in errors
    ), errors


@pytest.mark.asyncio
async def test_no_accessible_model_surfaces_terminal_error_without_swap(tmp_path, monkeypatch):
    """An account advertising nothing but the refused id has no accessible
    candidate: fail fast and legibly with the terminal entitlement error, no
    swap, no re-queue."""
    state = _make_state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    # The configured model is genuinely unentitled (absent from the advertised
    # set), and the only thing advertised is the "auto" sentinel -- which
    # first_advertised_fallback skips -- so there is no accessible candidate.
    client = _client_raising_always(_rejection("claude-opus-5", ["auto"]))
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    await _run_chat(state, slot, "first message")

    client.set_model.assert_not_awaited()
    assert slot._model_access_fallback_used is False
    # Terminal entitlement error surfaced (tagged so the frontend offers the
    # picker); nothing re-queued.
    errors = [m for m in slot.messages if m.get("role") == "error"]
    assert any((m.get("meta") or {}).get("kind") == MODEL_UNENTITLED_KIND for m in errors), errors
    assert not [q for q in slot._queue if q.get("kind") == SYNTHETIC_RECOVERY_KIND]


@pytest.mark.asyncio
async def test_oversized_advertised_candidate_is_refused_not_retained(tmp_path, monkeypatch):
    """A provider may advertise an arbitrarily long id; it must not be adopted.

    ``first_advertised_fallback`` checks only that the entry is a non-empty
    string, so the replacement id is provider-controlled and unbounded. It is
    retained in the transcript notice, in ``slot._active_fallback_model`` and in
    the session log, so an id past ``_MAX_MODEL_ID_LEN`` makes the guard go false
    and the already-bounded terminal error surface instead of a swap.
    """
    from kiro_crew.dashboard.chat_runner import _MAX_MODEL_ID_LEN

    state = _make_state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    oversized = "m" * (_MAX_MODEL_ID_LEN + 1)
    client = _client_raising_always(_rejection("claude-opus-5", [oversized]))
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    await _run_chat(state, slot, "first message")

    client.set_model.assert_not_awaited()
    assert slot._model_access_fallback_used is False
    assert getattr(slot, "_active_fallback_model", "") != oversized
    # Nothing retained the unbounded id: not the transcript, not the queue.
    assert not any(oversized in str(m.get("text") or "") for m in slot.messages), slot.messages
    errors = [m for m in slot.messages if m.get("role") == "error"]
    assert any((m.get("meta") or {}).get("kind") == MODEL_UNENTITLED_KIND for m in errors), errors
    assert not [q for q in slot._queue if q.get("kind") == SYNTHETIC_RECOVERY_KIND]


@pytest.mark.asyncio
async def test_oversized_rejected_id_is_refused_not_retained(tmp_path, monkeypatch):
    """The REJECTED id is provider-supplied too, and the notice retains it.

    Same bound, applied to ``exc.rejected_model``: an oversized rejected id must
    not reach the persisted notice even though a perfectly good candidate is
    advertised alongside it.
    """
    from kiro_crew.dashboard.chat_runner import _MAX_MODEL_ID_LEN

    state = _make_state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    oversized = "r" * (_MAX_MODEL_ID_LEN + 1)
    client = _client_raising_always(_rejection(oversized, ["claude-sonnet-4"]))
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    await _run_chat(state, slot, "first message")

    client.set_model.assert_not_awaited()
    assert slot._model_access_fallback_used is False
    assert not any(oversized in str(m.get("text") or "") for m in slot.messages), slot.messages
    assert not [q for q in slot._queue if q.get("kind") == SYNTHETIC_RECOVERY_KIND]


@pytest.mark.asyncio
async def test_unrelated_provider_error_stays_terminal_no_swap(tmp_path, monkeypatch):
    """An error that names NO rejected model is not an access denial: the trigger
    must not widen into a catch-all that masks a real fault as a model switch."""
    state = _make_state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    # A terminal error carrying NO rejected_model tag (e.g. a validation fault).
    # It even carries a usable advertised list, so the ONLY thing that keeps this
    # from swapping is the rejected-model requirement itself -- widen the trigger
    # to fire without a named rejection and this test reds.
    exc = AcpError("ValidationException: malformed request", transient=False)
    exc.advertised = ["claude-opus-5", "claude-sonnet-5"]
    client = _client_raising_always(exc)
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    await _run_chat(state, slot, "first message")

    client.set_model.assert_not_awaited()
    assert slot._model_access_fallback_used is False
    assert not [q for q in slot._queue if q.get("kind") == SYNTHETIC_RECOVERY_KIND]


@pytest.mark.asyncio
async def test_swap_recovery_replay_preserves_the_one_shot_flag(tmp_path, monkeypatch):
    """The swap re-queues the user's ORIGINAL message, which the turn-start reset
    cannot tell from a fresh user turn. The _model_access_recovery_pending latch
    the swap sets makes the reset preserve _model_access_fallback_used for that
    one replay, so a still-unentitled candidate cannot trigger a second swap.

    Without the latch the flag would reset to False on the replay and the one-shot
    guarantee would be delivered only by model_is_unusable, contradicting the
    branch's own bounded-by-one-attempt invariant."""
    state = _make_state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    # A clean client: the point is the reset path, not another rejection.
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

    client = AsyncMock()
    client.context_usage_pct = MagicMock(return_value=10.0)
    client.set_model = AsyncMock()
    client.served_model = ""
    client.client = None

    async def _stream(msg):
        for ev in (LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok"), LLMEvent(kind=EVENT_COMPLETE)):
            yield ev

    client.stream = _stream
    client.stream_command = _stream
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    # Simulate the state a swap leaves behind before its replay turn runs.
    slot._model_access_fallback_used = True
    slot._model_access_recovery_pending = True

    await _run_chat(state, slot, "first message")

    # The replay preserved the one-shot flag and consumed the latch.
    assert slot._model_access_fallback_used is True
    assert slot._model_access_recovery_pending is False


@pytest.mark.asyncio
async def test_stop_during_set_model_abandons_the_replay(tmp_path, monkeypatch):
    """set_model is a provider RPC that yields the event loop, so a Stop can land
    between the elif's entry guard and the re-queue. The re-queue re-checks the
    live-stop signals every sibling requeue site checks; a stopped turn must not
    replay ahead of the user's intent. The swap itself already happened and is
    harmless -- only the replay is abandoned."""
    state = _make_state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")

    # A stop resolves during the set_model await: bump _stop_generation there, the
    # same signal the sibling guards read.
    client = _client_raising(_rejection("auto", ["claude-opus-5", "claude-sonnet-5"]))

    async def _set_model_then_stop(_model):
        slot._stop_generation = getattr(slot, "_stop_generation", 0) + 1

    client.set_model = AsyncMock(side_effect=_set_model_then_stop)
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    _inserts: list[dict] = []
    _repo = slot._queue_repository
    _real_insert = _repo.queue_insert

    def _record_insert(owner, index, content, kind="", *args, **kw):
        _inserts.append({"content": content, "kind": kind})
        return _real_insert(owner, index, content, kind, *args, **kw)

    monkeypatch.setattr(_repo, "queue_insert", _record_insert)

    await _run_chat(state, slot, "first message")

    # The swap ran, but the stopped turn was NOT re-queued.
    client.set_model.assert_awaited_once_with("claude-opus-5")
    assert not [
        i
        for i in _inserts
        if i["kind"] == SYNTHETIC_RECOVERY_KIND and i["content"] == "first message"
    ], _inserts
    assert slot._model_access_recovery_pending is False


@pytest.mark.asyncio
async def test_set_model_failure_surfaces_terminal_card_not_a_silent_escape(tmp_path, monkeypatch):
    """The swap's own set_model is a provider RPC that can fail. When it does, the
    turn must surface the original entitlement error through the terminal card
    path and end, NOT re-raise -- a bare re-raise escapes _run_chat to a log-only
    callback and dead-ends the turn with no card, the exact silent failure this
    PR fixes."""
    state = _make_state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    client = _client_raising_always(_rejection("auto", ["claude-opus-5", "claude-sonnet-5"]))
    # The swap seam exists but the RPC to move the model fails.
    client.set_model = AsyncMock(side_effect=RuntimeError("provider set_model boom"))
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    # Must not raise out of _run_chat.
    await _run_chat(state, slot, "first message")

    client.set_model.assert_awaited_once_with("claude-opus-5")
    # The user sees the terminal entitlement error card (tagged so the frontend
    # offers the picker), not a silent dead turn.
    errors = [m for m in slot.messages if m.get("role") == "error"]
    assert any((m.get("meta") or {}).get("kind") == MODEL_UNENTITLED_KIND for m in errors), errors
    # Nothing re-queued: a failed swap is terminal, not a retry chain.
    assert not [q for q in slot._queue if q.get("kind") == SYNTHETIC_RECOVERY_KIND]
    # No misleading "running on X instead" notice when the swap did not take.
    notices = [m for m in slot.messages if m.get("role") == "notice"]
    assert not any("running on" in m.get("content", "") for m in notices), notices


@pytest.mark.asyncio
async def test_soft_stop_after_enqueue_drops_the_recovery_at_dequeue(tmp_path, monkeypatch):
    """A soft Stop (first press) does NOT clear the queue, and the drain's
    continuation purge covers only the two auto-continue constants -- not a
    message replay. So a swap recovery that is already queued when a Stop lands
    during post-turn cleanup must be dropped at DEQUEUE, comparing the live stop
    counter to the value snapshotted at enqueue, or the cancelled prompt would
    replay from the queue head."""
    from kiro_crew.dashboard.chat_runner import _start_next_queued_turn
    from kiro_crew.dashboard.chat_utils import payload_for_replay

    state = _make_state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    # A swap has already run: the flag is used, the recovery is queued, and the
    # enqueue stop-gen was snapshotted.
    slot._model_access_fallback_used = True
    slot._model_access_recovery_pending = True
    slot._model_access_recovery_stop_gen = 0
    slot.queue_insert(
        0,
        "first message",
        kind=SYNTHETIC_RECOVERY_KIND,
        payload=payload_for_replay(False),
    )
    # A soft Stop lands during the post-turn cleanup await: the counter advances
    # but the queue is NOT cleared.
    slot._stop_generation = 1

    dispatched = await _start_next_queued_turn(state, slot)

    # The stopped replay was dropped, not dispatched, and the one-shot refunded.
    assert dispatched is False
    assert not [q for q in slot._queue if q.get("kind") == SYNTHETIC_RECOVERY_KIND]
    assert slot._model_access_recovery_pending is False
    assert slot._model_access_fallback_used is False


@pytest.mark.asyncio
async def test_linked_channel_stop_drops_the_recovery_at_dequeue(tmp_path, monkeypatch):
    """A Stop issued on a linked channel surface (Slack/Discord) advances only
    the SESSION-scoped stop counter -- the slot's own _stop_generation stays put.
    The dequeue drain must compare the session-scoped counter too (snapshotted at
    enqueue), or an ordinary linked-channel Stop with nothing queued would leave
    the cancelled prompt in the queue head and replay it, repeating any
    destructive tool actions. This is the linked-surface twin of the soft-stop
    drain above."""
    from unittest.mock import MagicMock

    from kiro_crew.dashboard.chat_runner import _start_next_queued_turn
    from kiro_crew.dashboard.chat_utils import payload_for_replay

    state = _make_state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    # A swap has run: flag used, recovery queued, and BOTH stop-gen snapshots
    # taken at enqueue (slot=0, session=0).
    slot._model_access_fallback_used = True
    slot._model_access_recovery_pending = True
    slot._model_access_recovery_stop_gen = 0
    slot._model_access_recovery_session_stop_gen = 0
    slot.queue_insert(
        0,
        "first message",
        kind=SYNTHETIC_RECOVERY_KIND,
        payload=payload_for_replay(False),
    )
    # A linked-channel Stop lands: the SLOT counter is untouched (still 0), only
    # the session-scoped counter advances. Without the session-scoped comparison
    # this Stop is invisible to the drain.
    slot._stop_generation = 0
    state.sessions.stop_generation = MagicMock(return_value=1)

    dispatched = await _start_next_queued_turn(state, slot)

    # The linked-channel Stop was seen via the session-scoped counter: replay
    # dropped, one-shot refunded.
    assert dispatched is False
    assert not [q for q in slot._queue if q.get("kind") == SYNTHETIC_RECOVERY_KIND]
    assert slot._model_access_recovery_pending is False
    assert slot._model_access_fallback_used is False
    """The one-shot fence, proven at its WEAKEST arm: after a swap, the recovery
    replay runs and fails again with an entitlement rejection that DOES satisfy
    model_is_unusable (the rejected id is absent from the advertised set). The
    discriminator arm would let the elif fire -- so the ONLY thing that stops a
    second swap here is the preserved _model_access_fallback_used flag. It holds:
    the latch made the turn-start reset preserve the flag, `not _model_access_
    fallback_used` is False, the elif is skipped, and the turn ends on the
    terminal entitlement card with no second swap and no second recovery.

    Driven directly as the recovery turn: the drain dispatches the replay as its
    own turn (a background task the tail-drain does not await), so the fence is
    pinned by putting the slot in the exact state that turn starts in -- the flag
    set and the recovery-pending latch armed, as the swap left them."""
    state = _make_state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")

    # The recovery turn begins with the swap already applied: the one-shot flag is
    # set and the latch armed (so the turn-start reset preserves the flag rather
    # than refunding it -- exactly the state the swap enqueued).
    slot._model_access_fallback_used = True
    slot._model_access_recovery_pending = True
    slot.served_model = "claude-opus-5"
    # The replay fails with a rejection whose id is ABSENT from the advertised set
    # (model_is_unusable is True) AND a real candidate exists. Every gate EXCEPT
    # the one-shot flag now points at "swap again" -- so if the flag did not hold
    # across the recovery turn, this reds. It is the flag arm, isolated.
    client = _client_raising_always(_rejection("auto", ["claude-opus-5", "claude-sonnet-5"]))
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    _inserts: list[dict] = []
    _repo = slot._queue_repository
    _real_insert = _repo.queue_insert

    def _record_insert(owner, index, content, kind="", *args, **kw):
        _inserts.append({"content": content, "kind": kind})
        return _real_insert(owner, index, content, kind, *args, **kw)

    monkeypatch.setattr(_repo, "queue_insert", _record_insert)

    # Drive the recovery turn itself: the user's original message, replayed.
    await _run_chat(state, slot, "first message")

    # No SECOND swap: the fence held on the flag arm alone. set_model is never
    # called on this turn.
    client.set_model.assert_not_awaited()
    # The flag stayed set across the recovery turn (the latch preserved it, then
    # consumed the latch), so a still-unentitled candidate cannot re-open the swap.
    assert slot._model_access_fallback_used is True
    assert slot._model_access_recovery_pending is False
    # The replay's own failure surfaced the terminal entitlement card (this
    # rejection IS model_is_unusable, so it is tagged so the frontend offers the
    # picker), not a silent dead turn and not a second swap.
    errors = [m for m in slot.messages if m.get("role") == "error"]
    assert any((m.get("meta") or {}).get("kind") == MODEL_UNENTITLED_KIND for m in errors), errors
    # No new recovery enqueued: the second failure is terminal, not a retry chain.
    assert not [i for i in _inserts if i["kind"] == SYNTHETIC_RECOVERY_KIND], _inserts
    # No second "running on X instead" notice: no swap took place.
    notices = [m for m in slot.messages if m.get("role") == "notice"]
    assert not any("running on" in m.get("content", "") for m in notices), notices


@pytest.mark.asyncio
async def test_user_followup_drops_the_recovery_regardless_of_queue_position(tmp_path, monkeypatch):
    """Cell 2, pinned as position-independence rather than head-ordering. A user
    follow-up queued while a swap recovery is pending aborts the recovery and
    refunds the one-shot -- and it does so no matter WHERE the recovery sits,
    because the dequeue drop scans the whole queue by is_synthetic_recovery_item
    and _has_user_queued_followup scans the whole queue for user speech; neither
    reads an index. Here the user follow-up is enqueued AHEAD of the recovery (the
    opposite of the index-0 placement the swap uses), and the drop still fires. So
    the disposition needs no queue-ordering invariant: if a future change appends
    the replay instead of prepending it, this stays correct and this test stays
    green."""
    from kiro_crew.dashboard.chat_runner import _start_next_queued_turn
    from kiro_crew.dashboard.chat_utils import payload_for_replay

    state = _make_state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    slot._model_access_fallback_used = True
    slot._model_access_recovery_pending = True
    slot._model_access_recovery_stop_gen = getattr(slot, "_stop_generation", 0)
    # A user follow-up sits at the HEAD (plain entry, no kind => user speech), and
    # the recovery replay sits BEHIND it -- the reverse of the swap's own index-0
    # insert. No Stop is pressed; the follow-up alone is the intervention.
    slot.queue_append("please answer this instead")
    slot.queue_insert(
        1,
        "first message",
        kind=SYNTHETIC_RECOVERY_KIND,
        payload=payload_for_replay(False),
    )

    await _start_next_queued_turn(state, slot)

    # The recovery was dropped and the one-shot refunded, even though it sat
    # BEHIND the user follow-up rather than at the head -- the drop scanned the
    # whole queue, not index 0. That is the position-independence the disposition
    # rests on; the follow-up's own dispatch is not part of this claim.
    assert not [q for q in slot._queue if q.get("kind") == SYNTHETIC_RECOVERY_KIND]
    assert slot._model_access_recovery_pending is False
    assert slot._model_access_fallback_used is False


@pytest.mark.asyncio
async def test_swap_registers_active_fallback_and_restores_without_pinning_slot_model(
    tmp_path, monkeypatch
):
    """State-contract axis, full round trip: denial -> swap -> restore, with
    slot.model asserted UNCHANGED throughout.

    The swap must register the SAME sticky record the throttle walk writes
    (_active_fallback_model / _fallback_primary_model / _fallback_slot_model /
    _fallback_pick_gen), not merely flip the one-shot flag. Two things rest on
    that record and neither is covered by the recovery-turn control-flow cells:
      - the spawn backfill (guarded by `not slot.model and not
        slot._active_fallback_model`) must NOT pin the served substitute into an
        unpinned auto slot -- setting _active_fallback_model holds that guard;
      - _probe_fallback_restore_for_slot fires only while _active_fallback_model
        is set, so the record is what lets a later turn set_model back to the
        primary and heal slot.model once the account is entitled again.

    Writing the field is not the property that matters -- the ROUND TRIP is: this
    drives the real restore probe and asserts it clears the sticky state AND
    leaves slot.model exactly as it started (empty, an auto slot never gains a
    pin)."""
    from kiro_crew.dashboard.chat_runner import _probe_fallback_restore_for_slot

    state = _make_state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    # An auto slot: no explicit pin. slot.model must stay this way end to end.
    assert (slot.model or "") == ""
    _model_before = slot.model
    client = _client_raising(_rejection("auto", ["claude-opus-5", "claude-sonnet-5"]))
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    await _run_chat(state, slot, "first message")

    # The swap fired and registered the FULL sticky record (the state contract),
    # not just the one-shot flag.
    client.set_model.assert_awaited_once_with("claude-opus-5")
    assert slot._model_access_fallback_used is True
    assert slot._active_fallback_model == "claude-opus-5"
    assert slot._fallback_primary_model == "auto"
    # The slot was unpinned, and it STAYED unpinned across the swap -- the sticky
    # record carries the substitution, slot.model is not touched.
    assert slot._fallback_slot_model == ""
    assert (slot.model or "") == (_model_before or "")

    # Round trip: the account is now entitled to the primary again. The restore
    # probe (fires only because _active_fallback_model is set) sets_model back to
    # the primary, heals slot.model, and clears the sticky record. served_model is
    # the substitute (provider_active_model reads it) so the probe is not treated
    # as a stale external change.
    slot.served_model = "claude-opus-5"
    client.set_model.reset_mock()
    await _probe_fallback_restore_for_slot(slot, client)

    # Primary restored, sticky record cleared, and slot.model STILL unchanged --
    # a transient entitlement denial left no durable trace on the user's slot.
    client.set_model.assert_awaited_once_with("auto")
    assert slot._active_fallback_model == ""
    assert slot._fallback_primary_model == ""
    assert (slot.model or "") == (_model_before or "")


@pytest.mark.asyncio
async def test_restore_probe_honors_a_cross_alias_pick_via_the_shared_epoch(tmp_path, monkeypatch):
    """Two slots can drive one wire session and one client (a channel-born slot
    and its dashboard twin). An explicit model pick made through the OTHER alias
    bumps only the shared client's pick epoch, not this slot's local
    _model_pick_gen. If the restore probe compared the slot-local generation
    alone, it would not see the alias's pick and would restore the rejected
    primary, silently overwriting the user's explicit choice. The probe must
    treat the record as stale when the shared client epoch has moved since the
    fallback activated -- the guard the sibling refusal path carries via
    _refusal_client_pick_epoch.

    Mutation guard: drop the shared-epoch term from the restore probe's staleness
    check and this test reddens, because set_model is awarded back to the primary
    over the alias's pick.
    """
    from kiro_crew.dashboard.chat_runner import _probe_fallback_restore_for_slot

    state = _make_state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    client = _client_raising(_rejection("auto", ["claude-opus-5", "claude-sonnet-5"]))
    # pick_epoch_host(client) resolves to the client itself (client.client is
    # None); pin the epoch to a real int so the activation snapshot is 0, not an
    # auto-created mock.
    client._explicit_pick_epoch = 0
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    await _run_chat(state, slot, "first message")

    # The fallback is active and the shared epoch was snapshotted at activation.
    assert slot._active_fallback_model == "claude-opus-5"
    assert slot._fallback_primary_model == "auto"
    assert slot._fallback_client_pick_epoch == 0

    # An alias sharing this wire session makes an explicit pick: the shared
    # client epoch moves, but THIS slot's _model_pick_gen does NOT.
    _slot_gen_before = slot._model_pick_gen
    client._explicit_pick_epoch = 1
    assert slot._model_pick_gen == _slot_gen_before

    # The account is entitled to the primary again; the restore probe runs. It
    # must see the alias's pick through the shared epoch and DROP the record
    # rather than set_model back to the primary.
    slot.served_model = "claude-opus-5"
    client.set_model.reset_mock()
    await _probe_fallback_restore_for_slot(slot, client)

    # The alias's explicit pick stands: no restore to the primary, record cleared.
    client.set_model.assert_not_awaited()
    assert slot._active_fallback_model == ""
    assert slot._fallback_primary_model == ""


@pytest.mark.asyncio
async def test_restore_probe_holds_the_session_lock_across_the_rpc(tmp_path, monkeypatch):
    """The restore probe reads its staleness signal (pick generation / shared
    epoch) once BEFORE the set_model await, so a cross-alias pick landing inside
    that await would be applied first and then silently overwritten when the
    restore's set_model completes last. Holding the session-scoped switch lock
    across the whole check-and-restore serializes the two switches -- the same
    guard the swap and the refusal restore carry.

    Mutation guard: drop the slot_switch_session_lock acquisition around the
    restore probe and this test reddens, because the session lock is not held
    while the restore RPC (the exact window a twin's set_model interleaves) runs.
    """
    from unittest.mock import AsyncMock

    from kiro_crew.dashboard.chat_runner import _probe_fallback_restore_for_slot
    from kiro_crew.dashboard.chat_utils import effective_session_key
    from kiro_crew.llm_helpers import slot_switch_session_lock

    state = _make_state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    slot._active_fallback_model = "claude-opus-5"
    slot._fallback_primary_model = "auto"
    slot.served_model = "claude-opus-5"

    client = _client_raising(_rejection("auto", ["claude-opus-5"]))
    client._explicit_pick_epoch = 0
    session_key = effective_session_key(slot)
    held: dict[str, bool] = {"during_restore": False}

    async def _set_model_checks_lock(_model):
        held["during_restore"] = slot_switch_session_lock(session_key).locked()

    client.set_model = AsyncMock(side_effect=_set_model_checks_lock)

    await _probe_fallback_restore_for_slot(slot, client)

    client.set_model.assert_awaited_once_with("auto")
    assert held["during_restore"] is True, "session switch lock was not held during the restore RPC"
    # And it is released afterward -- the lock is not leaked past the restore.
    assert slot_switch_session_lock(session_key).locked() is False


@pytest.mark.asyncio
async def test_swap_does_not_pin_the_substitute_into_an_unpinned_slot(tmp_path, monkeypatch):
    """The persistence-narrowing property, pinned at the backfill guard itself.

    GPT's finding: without _active_fallback_model set, the spawn backfill
    (`if not slot.model and not slot._active_fallback_model: slot.model =
    _backfill_canonical_model(...)`) writes the served substitute into the
    unpinned auto slot on the replay turn -- a PERSISTENT pin surviving a reload,
    born of a transient denial. This drives that exact backfill branch after a
    swap and asserts it leaves slot.model empty because the fallback-active guard
    now holds.

    Mutation target: delete the `slot._active_fallback_model = _access_fb_candidate`
    line in the swap and this reds -- the backfill sees an unguarded unpinned slot
    and pins the substitute."""
    from kiro_crew.dashboard import chat_runner

    state = _make_state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    client = _client_raising(_rejection("auto", ["claude-opus-5", "claude-sonnet-5"]))
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    await _run_chat(state, slot, "first message")

    # The swap registered the fallback-active guard, so the backfill precondition
    # `not slot.model and not slot._active_fallback_model` is already False.
    assert slot._active_fallback_model == "claude-opus-5"
    assert (slot.model or "") == ""

    # Now run the backfill branch's exact guard as the spawn path evaluates it. It
    # must NOT pin: the substitute stays out of slot.model, so a reload finds an
    # unpinned auto slot, not a stuck fallback.
    backfilled = None
    if not slot.model and not slot._active_fallback_model:
        backfilled = chat_runner._backfill_canonical_model(client, "acp")
        slot.model = backfilled or slot.model
    assert backfilled is None, "backfill fired despite an active fallback -- persistent pin"
    assert (slot.model or "") == "", slot.model


@pytest.mark.asyncio
async def test_swap_holds_the_pick_lock_across_the_rpc_and_state_writes(tmp_path, monkeypatch):
    """State-contract axis, LOCK-SERIALISATION sub-axis.

    The reactive swap writes the pick/fallback fields under
    ``slot._model_pick_lock`` -- the lock every OTHER writer holds (explicit
    pick + bulk pick in chat_handlers, the throttle swap and the restore probe).
    A forced bulk pick (skip_running=false) is a documented concurrent API call:
    it can land in the ``await set_model(...)`` window, and without the lock it
    would observe the slot mid-swap -- the RPC has moved the live session but the
    sticky record (_active_fallback_model / _fallback_primary_model / ...) is not
    yet written -- then commit its own model and tear the session down, leaving
    our unlocked writes to record a stale fallback and diverge slot.model from the
    live session.

    This is an INTERLEAVING test, not an atomicity assertion: a competing task
    tries to acquire ``slot._model_pick_lock`` WHILE the swap is suspended inside
    the set_model RPC. It asserts the competitor cannot enter the lock until the
    swap has finished its state writes and released -- i.e. it observes the fully
    written record, never the half-written one.

    Mutation target: remove the ``async with _pick_lock:`` wrap in the swap and
    this reds -- the competitor enters during the RPC await, before
    _active_fallback_model is written, so it observes the half-written slot.
    """
    import asyncio

    state = _make_state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")

    # Gates that let the test pin the exact interleaving: the swap signals it has
    # ENTERED the set_model RPC, then waits for the test's go-ahead before the RPC
    # returns; meanwhile a competing task tries to take the pick lock.
    rpc_entered = asyncio.Event()
    let_rpc_return = asyncio.Event()
    # What the competitor observed the moment it got INTO the lock.
    observed: dict[str, object] = {}

    client = _client_raising(_rejection("auto", ["claude-opus-5", "claude-sonnet-5"]))

    async def _blocking_set_model(_model):
        # The swap is now inside the RPC. If it holds _model_pick_lock (correct),
        # the competitor below cannot enter until the whole swap completes; if it
        # does not (mutated), the competitor enters right here, mid-swap.
        rpc_entered.set()
        await let_rpc_return.wait()

    client.set_model = AsyncMock(side_effect=_blocking_set_model)
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    async def _competing_pick():
        # Wait until the swap is inside the RPC, then race for the pick lock the
        # way a concurrent bulk pick would.
        await rpc_entered.wait()
        # Give the swap task a few loop turns to prove it is NOT progressing past
        # the RPC (it is gated on let_rpc_return), so any lock entry here would be
        # a genuine mid-swap interleave, not a post-swap acquisition.
        for _ in range(5):
            await asyncio.sleep(0)
        async with slot._model_pick_lock:
            # Snapshot what the competitor sees at the instant it holds the lock.
            observed["active_fallback"] = slot._active_fallback_model
            observed["fallback_used"] = slot._model_access_fallback_used
            observed["primary"] = slot._fallback_primary_model

    swap_task = asyncio.create_task(_run_chat(state, slot, "first message"))
    pick_task = asyncio.create_task(_competing_pick())

    # Let both tasks run up to their gates.
    await rpc_entered.wait()
    for _ in range(10):
        await asyncio.sleep(0)

    # While the swap is suspended in the RPC, the competitor must NOT yet hold the
    # lock: nothing was observed. If the lock wrap is missing, the competitor has
    # already entered and observed the half-written slot.
    assert observed == {}, (
        "a competing pick-lock acquirer entered mid-swap -- the swap is not "
        f"holding _model_pick_lock across the RPC (observed={observed!r})"
    )

    # Release the RPC; the swap finishes its state writes and releases the lock,
    # then the competitor gets in.
    let_rpc_return.set()
    await asyncio.wait_for(asyncio.gather(swap_task, pick_task), timeout=5)

    # The competitor observed the FULLY written record, never the half-written
    # one: the sticky fields are all present together.
    assert observed["active_fallback"] == "claude-opus-5"
    assert observed["fallback_used"] is True
    assert observed["primary"] == "auto"


@pytest.mark.asyncio
async def test_reactive_swap_holds_the_per_session_switch_lock(tmp_path, monkeypatch):
    """The reactive fallback swap must serialize across session ALIASES, not just
    the per-slot pick lock.

    A channel-born slot and its dashboard twin drive one wire session through
    disjoint slot objects, so ``slot._model_pick_lock`` is disjoint across them
    and cannot order this swap against a concurrent forced bulk pick on the twin.
    Every other writer of the model/fallback state -- the switch handlers and the
    restore probe -- takes ``slot_switch_session_lock(session_key)`` before the
    pick lock; this reactive path had been the lone writer skipping it, letting
    persisted fallback state and ``slot.model`` diverge from the live session.

    Mutation guard: drop the ``slot_switch_session_lock`` acquisition around the
    swap and this test reddens, because the session lock is not held while the
    swap RPC (the exact window a twin's ``set_model`` interleaves) runs.
    """
    from kiro_crew.dashboard.chat_utils import effective_session_key
    from kiro_crew.llm_helpers import slot_switch_session_lock

    state = _make_state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    client = _client_raising(_rejection("auto", ["claude-opus-5", "claude-sonnet-5"]))

    session_key = effective_session_key(slot)
    held: dict[str, bool] = {"during_swap": False}

    async def _set_model_checks_lock(_model):
        # The per-session switch lock MUST be held while the swap RPC runs, so a
        # concurrent alias pick cannot interleave with it.
        held["during_swap"] = slot_switch_session_lock(session_key).locked()

    client.set_model = AsyncMock(side_effect=_set_model_checks_lock)
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    await _run_chat(state, slot, "first message")

    client.set_model.assert_awaited_once_with("claude-opus-5")
    assert held["during_swap"] is True, "session switch lock was not held during the swap RPC"
    # And it is released afterward -- the lock is not leaked past the swap.
    assert slot_switch_session_lock(session_key).locked() is False


@pytest.mark.asyncio
async def test_recovery_replay_dropped_when_the_slot_rebinds_mid_episode(tmp_path, monkeypatch):
    """A cron result binding an unbound slot during the awaited set_model rebinds
    the session mid-episode. The queued recovery replay is the user's ORIGINAL
    prompt, bound to the OLD session; dispatching it onto the newly bound session
    would run that prompt cross-session. The drain must drop it when the live
    binding differs from the one recorded at enqueue -- the guard the sibling
    refusal replay already carries.

    Mutation guard: drop ``_ma_rebound`` from the drain's condition (or the
    ``_model_access_recovery_session_key`` capture) and this test reddens, because
    the rebound replay is dispatched instead of dropped.
    """
    from unittest.mock import MagicMock

    from kiro_crew.dashboard.chat_runner import _start_next_queued_turn
    from kiro_crew.dashboard.chat_utils import effective_session_key, payload_for_replay

    state = _make_state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    slot._model_access_fallback_used = True
    slot._model_access_recovery_pending = True
    slot._model_access_recovery_stop_gen = 0
    slot._model_access_recovery_session_stop_gen = 0
    # Enqueue recorded a binding; the slot has since rebound to a DIFFERENT
    # session key (the mid-episode cron bind). No stop was issued -- the ONLY
    # signal that must drop this replay is the binding mismatch.
    slot._model_access_recovery_session_key = effective_session_key(slot) + "-OLD-BOUND"
    slot._stop_generation = 0
    state.sessions.stop_generation = MagicMock(return_value=0)
    slot.queue_insert(
        0,
        "first message",
        kind=SYNTHETIC_RECOVERY_KIND,
        payload=payload_for_replay(False),
    )

    dispatched = await _start_next_queued_turn(state, slot)

    # The rebind was seen: replay dropped, latch cleared, one-shot refunded.
    assert dispatched is False
    assert not [q for q in slot._queue if q.get("kind") == SYNTHETIC_RECOVERY_KIND]
    assert slot._model_access_recovery_pending is False
    assert slot._model_access_recovery_session_key == ""


@pytest.mark.asyncio
async def test_recovery_latch_cleared_when_the_admission_sweep_removes_the_entry(
    tmp_path, monkeypatch
):
    """The admission sweep (``_drop_stale_admissions``) drops a containment-changed
    recovery entry from the queue WITHOUT touching slot state -- not a stop, a
    rebind, or user input, so none of the drain's trigger-based drops fire. Without
    an entry-gone guard the ``_model_access_recovery_pending`` latch survives the
    sweep, and the user's next genuine turn is misclassified as a replay at the
    consume seam and discarded. The drain must clear the latch when the recorded
    replay entry is absent from the queue -- the guard the sibling refusal replay
    carries at ``_replay_entry is None``.

    Mutation guard: delete the entry-gone branch at the top of the
    ``_model_access_recovery_pending`` block and this test reddens, because the
    latch is left True after the sweep emptied the queue.
    """
    from unittest.mock import MagicMock

    from kiro_crew.dashboard.chat_runner import _start_next_queued_turn
    from kiro_crew.dashboard.chat_utils import effective_session_key, payload_for_replay

    state = _make_state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    slot._model_access_fallback_used = True
    slot._model_access_recovery_pending = True
    slot._model_access_recovery_stop_gen = 0
    slot._model_access_recovery_session_stop_gen = 0
    # No stop, no rebind, no user input: the ONLY reason the latch may clear is
    # the recorded replay entry being gone from the queue.
    slot._model_access_recovery_session_key = effective_session_key(slot)
    slot._stop_generation = 0
    state.sessions.stop_generation = MagicMock(return_value=0)

    qid = slot.queue_insert(
        0,
        "first message",
        kind=SYNTHETIC_RECOVERY_KIND,
        payload=payload_for_replay(False),
    )
    # queue_insert's return may be None on some slot variants; read the id off
    # the queued entry itself so the recorded qid is always the real one.
    if not qid:
        qid = slot._queue[0]["id"]
    slot._model_access_recovery_queue_id = qid
    # The admission sweep removed the entry (containment change) but left the
    # latch set -- reproduce that state directly.
    slot.queue_remove_by_id(qid)
    assert not [q for q in slot._queue if q.get("kind") == SYNTHETIC_RECOVERY_KIND]

    dispatched = await _start_next_queued_turn(state, slot)

    # The entry-gone guard cleared the latch and refunded the one-shot, so a
    # genuine next turn is not misclassified as a replay.
    assert dispatched is False
    assert slot._model_access_recovery_pending is False
    assert slot._model_access_recovery_queue_id == ""
    assert slot._model_access_recovery_session_key == ""
    assert slot._model_access_fallback_used is False


@pytest.mark.asyncio
async def test_recovery_replay_forwards_attachment_metadata(tmp_path, monkeypatch):
    """The model-access recovery replay is the SAME turn again, so it must carry
    the original turn's attachment metadata -- a folder attachment has to replay
    as a folder, not be retyped as a file (the sibling refusal replay forwards it
    the same way).

    Mutation guard: drop the ``extra_meta`` argument from the recovery
    ``_queue_recovery`` call and this test reddens, because the requeued entry's
    meta then lacks the ``dirs``/``files`` lists.
    """
    from kiro_crew.dashboard.chat_utils import SYNTHETIC_RECOVERY_KIND

    state = _make_state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    client = _client_raising(_rejection("auto", ["claude-opus-5", "claude-sonnet-5"]))
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    _metas: list[dict] = []
    _repo = slot._queue_repository
    _real_insert = _repo.queue_insert

    def _record_insert(owner, index, content, kind="", payload="", meta=None, *args, **kw):
        if kind == SYNTHETIC_RECOVERY_KIND:
            _metas.append(meta or {})
        return _real_insert(owner, index, content, kind, payload, meta, *args, **kw)

    monkeypatch.setattr(_repo, "queue_insert", _record_insert)

    # A folder attachment on the original turn, keyed by its meta list.
    await _run_chat(
        state,
        slot,
        "first message",
        _attachments=["/proj/docs"],
        _attachment_meta={"dirs": ["/proj/docs"]},
    )

    client.set_model.assert_awaited_once_with("claude-opus-5")
    assert _metas, "no synthetic-recovery entry was queued"
    # The folder replays AS a folder: dirs preserved, not rebucketed under files.
    assert _metas[0].get("dirs") == ["/proj/docs"], _metas[0]


@pytest.mark.asyncio
async def test_silent_noop_set_model_surfaces_error_not_a_false_fallback(tmp_path, monkeypatch):
    """A non-raising ``set_model`` can be a silent no-op that leaves the original
    still-unentitled model active. Recording fallback state and a "running on X"
    notice then would be a false claim, and the replay would rerun the rejected
    model. When the model is unchanged and is not the requested candidate, the
    swap is treated as a failure and the entitlement error is surfaced instead.

    Mutation guard: remove the before/after witness (record fallback state on any
    non-raising return) and this test reddens, because the false fallback state
    and notice are published for a no-op swap.
    """
    from kiro_crew.dashboard.chat_utils import MODEL_UNENTITLED_KIND

    state = _make_state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    client = _client_raising_always(_rejection("auto", ["claude-opus-5", "claude-sonnet-5"]))
    # The provider reports it is STILL on the rejected model both before and
    # after set_model: a silent no-op. set_model returns without raising and
    # without moving served_model.
    client.served_model = "auto"

    async def _noop_set_model(_model):
        # Deliberately does NOT change served_model -- the silent no-op.
        return None

    client.set_model = AsyncMock(side_effect=_noop_set_model)
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    await _run_chat(state, slot, "first message")

    # No false fallback state, no "running on X instead" notice: the swap was a
    # no-op, so the entitlement error is surfaced instead.
    assert slot._model_access_fallback_used is False
    assert not slot._active_fallback_model
    errors = [m for m in slot.messages if m.get("role") == "error"]
    assert any((m.get("meta") or {}).get("kind") == MODEL_UNENTITLED_KIND for m in errors), errors
    notices = [m for m in slot.messages if m.get("role") == "notice"]
    assert not any("instead" in (m.get("content") or "") for m in notices), notices


@pytest.mark.asyncio
async def test_recovery_replay_aborts_at_consume_when_stopped_after_dequeue(tmp_path, monkeypatch):
    """A Stop landing in the spawn-to-consume window (after the drain dequeued the
    replay, before it runs) is harmless at the provider level -- no turn is
    active yet -- so it must be caught at the consume seam, or the cancelled
    prompt executes. The model-access consume guard rechecks the same stop
    signals its sibling refusal guard does.

    Mutation guard: drop the stop recheck from the consume guard (gate on rebind
    alone) and this test reddens, because the stopped replay streams instead of
    aborting.
    """
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

    state = _make_state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")

    _streamed = {"n": 0}

    client = AsyncMock()
    client.context_usage_pct = MagicMock(return_value=10.0)
    client.set_model = AsyncMock()
    client.served_model = ""
    client.client = None

    async def _stream(msg):
        _streamed["n"] += 1
        for ev in (LLMEvent(kind=EVENT_TEXT_CHUNK, text="ran"), LLMEvent(kind=EVENT_COMPLETE)):
            yield ev

    client.stream = _stream
    client.stream_command = _stream
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    # The state a swap leaves behind before its replay turn runs, with the
    # stop-gen snapshots taken AT ENQUEUE.
    slot._model_access_fallback_used = True
    slot._model_access_recovery_pending = True
    slot._model_access_recovery_stop_gen = 0
    slot._model_access_recovery_session_stop_gen = 0
    # A Stop landed after the drain dequeued the replay: the slot stop counter has
    # advanced past the snapshot. No rebind, no user follow-up -- the ONLY signal
    # that must abort this replay is the advanced stop counter.
    slot._stop_generation = 1

    await _run_chat(state, slot, "first message")

    # The consume guard saw the Stop and aborted: the cancelled prompt never
    # streamed, and the latch/record were cleared.
    assert _streamed["n"] == 0, "the stopped replay executed instead of aborting"
    assert slot._model_access_recovery_pending is False
