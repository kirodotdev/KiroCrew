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
async def test_second_failure_on_the_fallback_model_does_not_swap_again(tmp_path, monkeypatch):
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
