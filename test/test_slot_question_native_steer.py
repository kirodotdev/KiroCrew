"""``/api/chat/slots/{slot}/question/{steer,dismiss}``: one slot's NATIVE card.

The app-facing way to answer the native mid-turn ``AskUserQuestion`` card a slot
raised (the app-sdk ``ChatEmbed`` uses it). Pinned here:

* the owner app of a slot, holding ``permissions.sessionApproval``, steers a
  native card's answer into its OWN running turn, with APP provenance
  (``user_origin=False`` -> no human-turn marker; ``appLabel`` on the row), and
  the card's status is retired after;
* when no turn is running the steer reports ``unavailable`` rather than
  dropping the answer, so the embed can queue an ordinary send instead;
* an app never steers or dismisses a card on a slot it does not own -- another
  app's slot or a user session, even with the grant -- one uniform 404;
* without the grant even the owner app is refused;
* only a NATIVE (stateless) card is steerable/dismissible; a blocking ask, an
  unknown card_id and an already-retired card all read as the same 404;
* the dashboard owner's ``/api/ask-question/*`` path is untouched.

These tests drive the REAL ``steer_into_running_turn`` through a fake ACP client
rather than patching it: patching ``chat_delivery.steer_into_running_turn`` as a
module attribute, repeated across a file, corrupts an unrelated steer test's RPC
when the two files land in one pytest process (a shared-shard flake). A fake
client that the real path injects into has no such cross-file effect.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app
from slot_ownership_checkpoint_helpers import (  # noqa: F401 -- fixtures requested by name
    _GRANT,
    _NOT_FOUND,
    APP,
    _as,
    sel_spy_fixture,
    state_fixture,
)

from kiro_crew.dashboard.handlers.slot_question import (
    _release_native_steer,
    _reserve_native_steer,
    api_chat_slot_question_dismiss,
    api_chat_slot_question_steer,
)
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.history import HUMAN_TURN_META_KEY

_QUESTIONS = [{"question": "Which branch?", "options": [{"label": "main"}, {"label": "dev"}]}]


def _client(state, caller: str | None) -> TestClient:
    app = _make_app(state)
    app.router.add_post("/api/chat/slots/{slot}/question/steer", api_chat_slot_question_steer)
    app.router.add_post("/api/chat/slots/{slot}/question/dismiss", api_chat_slot_question_dismiss)
    app.middlewares.insert(0, _as(caller))
    return TestClient(TestServer(app))


def _seed(state) -> None:
    state.get_or_create_slot("mine", app=APP)
    state.get_or_create_slot("theirs", app="other-app")
    state.get_or_create_slot("u1", origin=SlotOrigin.USER)


def _native_card(state, slot_key: str, card_id: str) -> None:
    """Raise a pending native (stateless) card on *slot_key*.

    Written the way ``QuestionCoordinator.mark_pending`` writes it, so
    ``pending_question_cards`` and ``clear_question_pending`` read it the same.
    """
    state._slots[slot_key]._question_pending[card_id] = {
        "ts": 1.0,
        "blocking": False,
        "questions": _QUESTIONS,
        "native": True,
    }


def _blocking_card(state, slot_key: str, card_id: str) -> None:
    """Raise a pending BLOCKING ask record on *slot_key* (not a native card)."""
    state._slots[slot_key]._question_pending[card_id] = {
        "ts": 1.0,
        "blocking": True,
        "questions": _QUESTIONS,
    }


def _running(state, slot_key: str, flag: bool = True) -> None:
    """Make the slot read as having a turn in flight (admission reserved).

    ``slot.running`` is also True via ``_turn_admission_reserved``; used where the
    test only needs the handler's running-gate to pass, not a real steer.
    """
    state._slots[slot_key]._turn_admission_reserved = flag


def _fake_client(*, steered: bool = True, on_steer=None) -> MagicMock:
    """A steer-capable fake ACP client the real steer path injects into.

    ``on_steer`` runs inside the awaited ``steer`` call, the only vantage point
    where "while the RPC is suspended" is observable: a test uses a sync one to
    bump ``_stop_generation`` (forcing a requeue), or an async one to hold the RPC
    open until a second request has run (the concurrency test).
    """
    import inspect

    client = MagicMock()
    client.supports_steer = True
    # Explicit so the real steer takes the direct path: a bare MagicMock answers
    # truthy for these, which would send it through the retract clear-wait and the
    # loss-recovery bail, neither of which this fake implements.
    client.supports_steer_retract = False
    client.steer_needs_loss_recovery = False

    async def _steer(_msg):
        if on_steer is not None:
            res = on_steer()
            if inspect.isawaitable(res):
                await res
        return steered

    client.steer = AsyncMock(side_effect=_steer)
    return client


def _steer_capable(state, slot_key: str, *, client: MagicMock | None = None) -> MagicMock:
    """Give the slot a live, steer-capable turn so a real steer runs.

    ``slot.running`` is ``task is not None and not task.done()``; the published
    ``_acp_client`` is what ``steer_into_running_turn`` injects into. Returns the
    client so a test can assert it was awaited.
    """
    slot = state._slots[slot_key]
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    client = client or _fake_client()
    slot._acp_client = client
    return client


@pytest.mark.asyncio
async def test_owner_app_steers_a_native_answer_into_its_running_turn(state, sel_spy) -> None:
    """The real steer delivers the answer and the row reads as the APP's."""
    _seed(state)
    _native_card(state, "mine", "c1")
    client = _steer_capable(state, "mine")
    with patch(_GRANT, return_value=True):
        async with _client(state, APP) as c:
            resp = await c.post(
                "/api/chat/slots/mine/question/steer",
                json={"card_id": "c1", "message": "Which branch?: dev"},
            )
            assert resp.status == 200, await resp.text()
            assert await resp.json() == {"ok": True, "steered": True, "queued": False}
    slot = state._slots["mine"]
    # The answer was handed to THIS slot's live turn.
    client.steer.assert_awaited_once_with("Which branch?: dev")
    # The persisted row is the APP's: appLabel stamped, and NO human-turn marker
    # (user_origin=False), so an app answer never advances the human-turn stamp.
    steer_row = next(m for m in slot.messages if m.get("meta", {}).get("steer"))
    assert steer_row["meta"].get("appLabel") == APP
    assert HUMAN_TURN_META_KEY not in steer_row["meta"]
    # The card's needs_input status is retired after a delivered steer.
    assert "c1" not in slot._question_pending


@pytest.mark.asyncio
async def test_a_requeued_steer_is_delivered_and_retires_the_card(state, sel_spy) -> None:
    """A turn teardown during the RPC requeues the steer: still delivered, card retired."""
    _seed(state)
    _native_card(state, "mine", "c1")
    slot = state._slots["mine"]

    # Simulate the turn's teardown bumping the stop generation while the steer RPC
    # is suspended, with the steer still registered: the real path returns REQUEUED.
    def _teardown():
        slot._stop_generation = int(getattr(slot, "_stop_generation", 0) or 0) + 1

    _steer_capable(state, "mine", client=_fake_client(steered=True, on_steer=_teardown))
    with patch(_GRANT, return_value=True):
        async with _client(state, APP) as c:
            resp = await c.post(
                "/api/chat/slots/mine/question/steer",
                json={"card_id": "c1", "message": "dev"},
            )
            assert resp.status == 200
            assert await resp.json() == {"ok": True, "steered": True, "queued": True}
    assert "c1" not in slot._question_pending


@pytest.mark.asyncio
async def test_no_running_turn_reports_unavailable_and_keeps_the_card(state, sel_spy) -> None:
    """No turn in flight: the handler reports unavailable before any steer, card kept."""
    _seed(state)
    _native_card(state, "mine", "c1")  # no running turn
    with patch(_GRANT, return_value=True):
        async with _client(state, APP) as c:
            resp = await c.post(
                "/api/chat/slots/mine/question/steer",
                json={"card_id": "c1", "message": "dev"},
            )
            assert resp.status == 200
            assert await resp.json() == {"ok": True, "steered": False, "unavailable": True}
    # The embed falls back to a queued send; the card is NOT retired or dropped.
    assert "c1" in state._slots["mine"]._question_pending


@pytest.mark.asyncio
async def test_steer_unavailable_from_the_rpc_keeps_the_card(state, sel_spy) -> None:
    """Running but no live steer-capable client: the real steer returns unavailable."""
    _seed(state)
    _native_card(state, "mine", "c1")
    # Running gate passes, but no client is published, so the RPC path is unavailable.
    _running(state, "mine")
    state._slots["mine"].task = None
    state._slots["mine"]._acp_client = None
    with patch(_GRANT, return_value=True):
        async with _client(state, APP) as c:
            resp = await c.post(
                "/api/chat/slots/mine/question/steer",
                json={"card_id": "c1", "message": "dev"},
            )
            assert resp.status == 200
            assert await resp.json() == {"ok": True, "steered": False, "unavailable": True}
    assert "c1" in state._slots["mine"]._question_pending


@pytest.mark.parametrize("slot", ["theirs", "u1", "absent"])
@pytest.mark.asyncio
async def test_an_app_cannot_steer_or_dismiss_a_slot_it_does_not_own(state, sel_spy, slot) -> None:
    """A 404 on both routes proves the steer was never reached for a foreign slot."""
    _seed(state)
    if slot != "absent":
        _native_card(state, slot, "c1")
        _steer_capable(state, slot)
    with patch(_GRANT, return_value=True):
        async with _client(state, APP) as c:
            resp = await c.post(
                f"/api/chat/slots/{slot}/question/steer",
                json={"card_id": "c1", "message": "dev"},
            )
            assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
            resp = await c.post(f"/api/chat/slots/{slot}/question/dismiss", json={"card_id": "c1"})
            assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
    if slot != "absent":
        # Refused before the steer: the foreign client was never asked, card intact.
        state._slots[slot]._acp_client.steer.assert_not_awaited()
        assert "c1" in state._slots[slot]._question_pending


@pytest.mark.asyncio
async def test_without_the_grant_the_owner_app_is_refused(state, sel_spy) -> None:
    _seed(state)
    _native_card(state, "mine", "c1")
    client = _steer_capable(state, "mine")
    with patch(_GRANT, return_value=False):
        async with _client(state, APP) as c:
            resp = await c.post(
                "/api/chat/slots/mine/question/steer",
                json={"card_id": "c1", "message": "dev"},
            )
            assert resp.status == 403
            assert (await resp.json())["code"] == "session_approval_not_granted"
            resp = await c.post("/api/chat/slots/mine/question/dismiss", json={"card_id": "c1"})
            assert resp.status == 403
    client.steer.assert_not_awaited()
    assert "c1" in state._slots["mine"]._question_pending


@pytest.mark.asyncio
async def test_a_grant_removed_during_the_body_read_refuses_the_steer(state, sel_spy) -> None:
    _seed(state)
    _native_card(state, "mine", "c1")
    client = _steer_capable(state, "mine")
    grant_reads = {"n": 0}

    def _grant(_app):
        grant_reads["n"] += 1
        return grant_reads["n"] == 1  # granted on the first read, removed on the re-read

    with patch(_GRANT, side_effect=_grant):
        async with _client(state, APP) as c:
            resp = await c.post(
                "/api/chat/slots/mine/question/steer",
                json={"card_id": "c1", "message": "dev"},
            )
            assert resp.status == 403
    client.steer.assert_not_awaited()
    assert "c1" in state._slots["mine"]._question_pending


@pytest.mark.asyncio
async def test_a_blocking_ask_is_not_steerable_through_this_route(state, sel_spy) -> None:
    """A blocking ask owns /answer; naming its id here reads as no native card."""
    _seed(state)
    _blocking_card(state, "mine", "b1")  # native=False
    client = _steer_capable(state, "mine")
    with patch(_GRANT, return_value=True):
        async with _client(state, APP) as c:
            resp = await c.post(
                "/api/chat/slots/mine/question/steer",
                json={"card_id": "b1", "message": "dev"},
            )
            assert resp.status == 404
            assert (await resp.json())["code"] == "question_card_not_found"
    client.steer.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unknown_card_id_is_not_found(state, sel_spy) -> None:
    _seed(state)
    _native_card(state, "mine", "c1")
    client = _steer_capable(state, "mine")
    with patch(_GRANT, return_value=True):
        async with _client(state, APP) as c:
            resp = await c.post(
                "/api/chat/slots/mine/question/steer",
                json={"card_id": "nope", "message": "dev"},
            )
            assert resp.status == 404
            assert (await resp.json())["code"] == "question_card_not_found"
    client.steer.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_app_dismisses_a_native_card(state, sel_spy) -> None:
    _seed(state)
    _native_card(state, "mine", "c1")
    with patch(_GRANT, return_value=True):
        async with _client(state, APP) as c:
            resp = await c.post("/api/chat/slots/mine/question/dismiss", json={"card_id": "c1"})
            assert resp.status == 200
            assert await resp.json() == {"ok": True}
    assert "c1" not in state._slots["mine"]._question_pending


@pytest.mark.asyncio
async def test_dismiss_is_refused_while_a_steer_for_the_card_is_in_flight(state, sel_spy) -> None:
    """A dismiss racing an in-flight steer of the same card is refused 409.

    The steer's answer is already on its way into the running turn, and the card
    is retired only after it lands. A dismiss that retired it meanwhile would
    leave the two routes disagreeing about the card's state. It gets the same
    409 steer_in_flight the steer route uses, and the card stays pending.
    """
    _seed(state)
    _native_card(state, "mine", "c1")
    slot = state._slots["mine"]
    # Claim the card as an in-flight steer would, synchronously.
    assert _reserve_native_steer(slot, "c1") is True
    with patch(_GRANT, return_value=True):
        async with _client(state, APP) as c:
            resp = await c.post("/api/chat/slots/mine/question/dismiss", json={"card_id": "c1"})
            assert resp.status == 409
            assert (await resp.json())["code"] == "steer_in_flight"
    # The card is untouched -- the steer, not the dismiss, will retire it.
    assert "c1" in slot._question_pending
    # Once the steer settles and releases, a dismiss is accepted again.
    _release_native_steer(slot, "c1")
    with patch(_GRANT, return_value=True):
        async with _client(state, APP) as c:
            resp = await c.post("/api/chat/slots/mine/question/dismiss", json={"card_id": "c1"})
            assert resp.status == 200
    assert "c1" not in slot._question_pending
    _seed(state)
    with patch(_GRANT, return_value=True):
        async with _client(state, APP) as c:
            resp = await c.post("/api/chat/slots/mine/question/dismiss", json={"card_id": "nope"})
            assert resp.status == 404
            assert (await resp.json())["code"] == "question_card_not_found"


@pytest.mark.asyncio
async def test_missing_fields_are_bad_requests(state, sel_spy) -> None:
    _seed(state)
    _native_card(state, "mine", "c1")
    client = _steer_capable(state, "mine")
    with patch(_GRANT, return_value=True):
        async with _client(state, APP) as c:
            resp = await c.post("/api/chat/slots/mine/question/steer", json={"message": "dev"})
            assert (resp.status, (await resp.json())["code"]) == (400, "missing_card_id")
            resp = await c.post("/api/chat/slots/mine/question/steer", json={"card_id": "c1"})
            assert (resp.status, (await resp.json())["code"]) == (400, "missing_message")
            resp = await c.post(
                "/api/chat/slots/mine/question/steer",
                json={"card_id": "c1", "message": "   "},
            )
            assert (resp.status, (await resp.json())["code"]) == (400, "missing_message")
    client.steer.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_second_steer_for_the_same_card_is_not_found(state, sel_spy) -> None:
    """Delivery retires the card, so a repeat steer finds no native card: 404.

    One steer per card -- not an open steer channel into the app's running turn.
    """
    _seed(state)
    _native_card(state, "mine", "c1")
    _steer_capable(state, "mine")
    with patch(_GRANT, return_value=True):
        async with _client(state, APP) as c:
            first = await c.post(
                "/api/chat/slots/mine/question/steer",
                json={"card_id": "c1", "message": "dev"},
            )
            assert first.status == 200
            assert (await first.json())["steered"] is True
            second = await c.post(
                "/api/chat/slots/mine/question/steer",
                json={"card_id": "c1", "message": "dev again"},
            )
            assert second.status == 404
            assert (await second.json())["code"] == "question_card_not_found"


@pytest.mark.asyncio
async def test_a_blocking_ask_is_not_dismissable_through_this_route(state, sel_spy) -> None:
    """Dismiss names only a native card; a blocking ask reads as no native card."""
    _seed(state)
    _blocking_card(state, "mine", "b1")
    with patch(_GRANT, return_value=True):
        async with _client(state, APP) as c:
            resp = await c.post("/api/chat/slots/mine/question/dismiss", json={"card_id": "b1"})
            assert resp.status == 404
            assert (await resp.json())["code"] == "question_card_not_found"
    # The blocking record is left for its own /answer lifecycle.
    assert "b1" in state._slots["mine"]._question_pending


@pytest.mark.asyncio
async def test_the_dashboard_owner_steer_row_is_not_app_attributed(state, sel_spy) -> None:
    """The owner (no app identity) gets no appLabel: the row is the owner's own."""
    _seed(state)
    state.get_or_create_slot("owned", origin=SlotOrigin.USER)
    _native_card(state, "owned", "c9")
    _steer_capable(state, "owned")
    async with _client(state, "") as c:  # "" => dashboard owner, no app
        resp = await c.post(
            "/api/chat/slots/owned/question/steer",
            json={"card_id": "c9", "message": "owner answer"},
        )
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["steered"] is True
    slot = state._slots["owned"]
    steer_row = next(m for m in slot.messages if m.get("meta", {}).get("steer"))
    assert "appLabel" not in steer_row["meta"]


@pytest.mark.asyncio
async def test_two_concurrent_steers_deliver_the_card_exactly_once(state, sel_spy) -> None:
    """F1: a steer already in flight for a card_id -> a second is refused 409.

    ``steer_into_running_turn`` deduplicates only by message TEXT, so two owner-app
    requests with DIFFERENT answers for the same card would otherwise both reach
    the running turn. The handler reserves the card synchronously (on the card's
    own record) before the delivery await; a concurrent second request sees the
    reservation and is refused. Deterministic: the first steer's reservation is
    pinned (a real in-flight steer holds exactly this), then a second request is
    issued and must be refused BEFORE touching the turn; releasing the pin lets a
    steer deliver, and it lands exactly once.
    """
    _seed(state)
    _native_card(state, "mine", "c1")
    client = _steer_capable(state, "mine")
    slot = state._slots["mine"]
    # A first steer is in flight: it holds the card's reservation across its await.
    assert _reserve_native_steer(slot, "c1") is True
    with patch(_GRANT, return_value=True):
        async with _client(state, APP) as c:
            # A concurrent second request is refused without reaching the turn.
            second = await c.post(
                "/api/chat/slots/mine/question/steer",
                json={"card_id": "c1", "message": "main"},
            )
            assert second.status == 409
            assert (await second.json())["code"] == "steer_in_flight"
            client.steer.assert_not_awaited()
            # The first steer completes and releases its hold (delivered: it retires
            # the card, so the reservation goes with it). Model that here, then a
            # fresh steer for a NEW card delivers exactly once.
            _release_native_steer(slot, "c1")
            resp = await c.post(
                "/api/chat/slots/mine/question/steer",
                json={"card_id": "c1", "message": "dev"},
            )
            assert resp.status == 200
            assert (await resp.json())["steered"] is True
    # Across both requests exactly one answer reached the turn, and the card is gone.
    client.steer.assert_awaited_once_with("dev")
    assert "c1" not in slot._question_pending


@pytest.mark.asyncio
async def test_a_requeued_app_steer_drains_as_an_app_turn_not_a_user_turn(state, sel_spy) -> None:
    """F4: the requeued entry's carried meta makes the drain read actor 'app', not 'user'.

    A turn teardown during the RPC requeues the app steer. The requeue
    (`_requeue_unconsumed_steers`) rebuilds the entry by unioning the steer's
    carried `_steer_attachment_meta` onto it; the drain's `_actor_for_queue_items`
    then reads the turn actor from that meta. Without the app actor stamp it would
    read `user` and admit the answer to the user-only routing gate. Asserted on the
    carried meta the requeue consumes.
    """
    # Imported THROUGH chat_runner: chat_turn's `_bind`/`_facade_compose` rebinds
    # the owner functions' globals (logger, crew_log_emit) when chat_runner loads,
    # which is what the real chat path and the existing requeue tests do. A direct
    # `from ...steer_queue import _actor_for_queue_items` skips that bind and the
    # function then NameErrors on `crew_log_emit` -- a harness artifact, not a
    # runtime bug.
    import kiro_crew.dashboard.chat_runner  # noqa: F401  -- triggers the bind
    from kiro_crew.dashboard.chat_turn.steer_queue import _actor_for_queue_items

    _seed(state)
    _native_card(state, "mine", "c1")
    slot = state._slots["mine"]

    # Bump the stop generation inside the RPC with the steer still registered: the
    # real path returns REQUEUED and leaves the message pending for the teardown.
    def _teardown():
        slot._stop_generation = int(getattr(slot, "_stop_generation", 0) or 0) + 1

    _steer_capable(state, "mine", client=_fake_client(steered=True, on_steer=_teardown))
    with patch(_GRANT, return_value=True):
        async with _client(state, APP) as c:
            resp = await c.post(
                "/api/chat/slots/mine/question/steer",
                json={"card_id": "c1", "message": "dev"},
            )
            assert resp.status == 200
            assert (await resp.json())["queued"] is True
    # The message stayed pending for the teardown, and its carried meta -- the dict
    # the requeue unions onto the queue entry -- stamps the app actor. Build the
    # entry the way the requeue does and read the actor the drain would.
    assert "dev" in slot._pending_steers
    carried = slot._steer_attachment_meta["dev"]
    entry = {"content": "dev", "meta": dict(carried)}
    assert _actor_for_queue_items([entry]) == "app"


@pytest.mark.asyncio
async def test_an_invalid_app_label_still_requeues_as_an_app_turn(state, sel_spy) -> None:
    """The ACTOR is keyed on the authenticated caller, not the validated label.

    An app whose identity is over the label bound (or carries control chars) has
    its ``appLabel`` dropped, but the answer must STILL requeue as an app turn --
    `turnActor='app'` -- or it would drain as `user` and hit the user-only gate.
    """
    import kiro_crew.dashboard.chat_runner  # noqa: F401 -- triggers the bind
    from kiro_crew.dashboard.chat_turn.steer_queue import _actor_for_queue_items

    long_app = "x" * 200  # over MAX_SOURCE_LABEL_LEN (64): the label is dropped
    state.get_or_create_slot("mine", app=long_app)
    _native_card(state, "mine", "c1")
    slot = state._slots["mine"]

    def _teardown():
        slot._stop_generation = int(getattr(slot, "_stop_generation", 0) or 0) + 1

    _steer_capable(state, "mine", client=_fake_client(steered=True, on_steer=_teardown))
    with patch(_GRANT, return_value=True):
        async with _client(state, long_app) as c:
            resp = await c.post(
                "/api/chat/slots/mine/question/steer",
                json={"card_id": "c1", "message": "dev"},
            )
            assert resp.status == 200
            assert (await resp.json())["queued"] is True
    carried = slot._steer_attachment_meta["dev"]
    # No display label survived the bound, but the routing actor is still 'app'.
    assert "appLabel" not in carried
    assert _actor_for_queue_items([{"content": "dev", "meta": dict(carried)}]) == "app"


@pytest.mark.asyncio
async def test_the_steer_reservation_never_leaks_into_a_pending_card(state, sel_spy) -> None:
    """The in-flight claim lives on the slot's own set, never on the card record.

    So a listed pending card (GET .../question, the question_card broadcast)
    carries no steer-claim key even while a steer for that card is in flight.
    """
    from kiro_crew.dashboard.handlers.ask_question import pending_question_cards

    _seed(state)
    _native_card(state, "mine", "c1")
    slot = state._slots["mine"]
    # Claim the card as a real in-flight steer would, synchronously.
    assert _reserve_native_steer(slot, "c1") is True
    # The claim is on the slot set, not the record.
    assert "c1" in slot._native_steer_in_flight
    assert "_steering" not in slot._question_pending["c1"]
    # The listed card is unchanged and carries only its public fields.
    cards = pending_question_cards(state, "mine")
    card = next(c for c in cards if c.get("card_id") == "c1")
    assert set(card) <= {"card_id", "slot", "questions", "native", "ts"}
    assert "_steering" not in card
    _release_native_steer(slot, "c1")
    assert "c1" not in slot._native_steer_in_flight


@pytest.mark.asyncio
async def test_a_requeued_app_answer_drains_with_commands_off(state, sel_spy) -> None:
    """A native-card answer that requeues must NOT run as a slash command.

    The answer is the app user's own text -- a chosen option or free text that may
    literally be "/clear". The requeue carries COMMANDS_OFF so the drain treats it
    as plain text, exactly as a channel steer's requeue does. Without it the
    drained entry would execute "/clear" as a dashboard command.
    """
    from kiro_crew.dashboard.chat_delivery import COMMANDS_OFF_META_KEY

    _seed(state)
    _native_card(state, "mine", "c1")
    slot = state._slots["mine"]

    def _teardown():
        slot._stop_generation = int(getattr(slot, "_stop_generation", 0) or 0) + 1

    _steer_capable(state, "mine", client=_fake_client(steered=True, on_steer=_teardown))
    with patch(_GRANT, return_value=True):
        async with _client(state, APP) as c:
            resp = await c.post(
                "/api/chat/slots/mine/question/steer",
                json={"card_id": "c1", "message": "/clear"},
            )
            assert resp.status == 200
            assert (await resp.json())["queued"] is True
    assert "/clear" in slot._pending_steers
    carried = slot._steer_attachment_meta["/clear"]
    # Carried on the requeue meta the drain reads from each consumed entry.
    assert carried.get(COMMANDS_OFF_META_KEY) is True
    entry = {"content": "/clear", "meta": dict(carried)}
    # queue_drain reads commands-off from any consumed entry's meta (L957).
    assert (entry["meta"] or {}).get(COMMANDS_OFF_META_KEY) is True


@pytest.mark.asyncio
async def test_a_composer_steer_requeue_meta_has_no_commands_off(state, sel_spy) -> None:
    """Commands-off is keyed on the app actor, so a composer steer is unchanged.

    Driving ``steer_into_running_turn`` with ``app_actor=False`` (what a composer
    steer passes) must leave COMMANDS_OFF off the requeue meta -- the app change
    must not silently disarm slash commands for a human's own composer steer.
    """
    from kiro_crew.dashboard.chat_delivery import (
        COMMANDS_OFF_META_KEY,
        steer_into_running_turn,
    )

    _seed(state)
    slot = state._slots["mine"]

    def _teardown():
        slot._stop_generation = int(getattr(slot, "_stop_generation", 0) or 0) + 1

    _steer_capable(state, "mine", client=_fake_client(steered=True, on_steer=_teardown))
    # A composer steer: user_origin True, app_actor False -- the human's own text.
    await steer_into_running_turn(state, slot, "/clear", user_origin=True, app_actor=False)
    # It requeued (the teardown bumped the stop generation inside the RPC).
    carried = slot._steer_attachment_meta.get("/clear", {})
    assert COMMANDS_OFF_META_KEY not in carried
