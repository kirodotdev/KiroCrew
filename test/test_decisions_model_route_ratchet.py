"""A routed model does not outlive its turn: an unpinned tier goes back to the base.

``model.route`` switches the live session onto the answered tier's model. That is
harmless on a FULLY pinned map -- every turn is answered and every answer applied,
so the model the turn runs on is always the current answer's.

It is not harmless on a PARTIALLY pinned one, which is what every install is on
the way to (each tier ships as ``""``). A turn answered ``simple`` moves the
session to the cheap pin; the next turn answered ``complex`` is UNPINNED and
applies no tier model, so without a restore the session keeps the cheap model: the
owner's configured model is never reached again for the life of that session, and
that turn's own receipt names the cheap model as its "model without routing".

So an unpinned tier switches BACK to the model the session ran on before routing
first moved it, and the receipt's ``baseline_model`` names that same pre-route
model on every turn rather than the previous turn's tier.

The baseline is a SESSION fact, latched on the wire session's epoch host, because
several slot aliases can drive one session. The client double here is what makes
any of it observable: the real backend's ``served_model`` FOLLOWS a ``set_model``,
and a slot's own ``served_model`` is only that alias's cache of it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_decisions_strip_rides_message import _quiet_sel, _runner_state, _settle, _slot

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.config.sections import DECISION_PROVIDER_ENDPOINT_DEFAULT, DecisionsConfig
from kiro_crew.dashboard import chat_runner
from kiro_crew.decisions import gate as gate_mod
from kiro_crew.decisions import log as log_mod
from kiro_crew.decisions import outcomes
from kiro_crew.decisions.points import model_route as mr
from kiro_crew.decisions.types import Answer
from kiro_crew.llm_helpers import TURN_FALLBACK_ATTR, pick_epoch_host
from kiro_crew.providers.base import LLMEvent

#: Stands in for the owner's configured model (opus in the report).
BASE_MODEL = "model-base"
#: Stands in for the cheap model one tier is pinned to (haiku in the report).
CHEAP_MODEL = "model-cheap"
#: Stands in for a model the owner picks by hand, through any alias.
PICKED_MODEL = "model-picked"

ADVERTISED = [BASE_MODEL, CHEAP_MODEL, PICKED_MODEL]

#: The partially pinned map: one tier pinned cheap, the other two shipped unpinned.
PARTIAL_MAP = {"simple": CHEAP_MODEL, "medium": "", "complex": ""}


@pytest.fixture(autouse=True)
def clean_registry():
    outcomes.reset()
    yield
    outcomes.reset()


@pytest.fixture(autouse=True)
def consented(tmp_path, monkeypatch):
    path = tmp_path / "decisions_consent.json"
    path.write_text(
        json.dumps({"enabled": True, "endpoint": DECISION_PROVIDER_ENDPOINT_DEFAULT}),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.config.loader.decisions_consent_path", lambda: path)
    monkeypatch.setattr(log_mod, "log_dir", lambda: tmp_path / "decisions")
    monkeypatch.setattr(
        gate_mod,
        "_snapshot",
        lambda: SimpleNamespace(decisions=DecisionsConfig(model_route=dict(PARTIAL_MAP))),
    )


@pytest.fixture
def answers(monkeypatch):
    """An oracle answering whichever tier the test last set."""
    import kiro_crew.decisions.impl_jev as impl_mod

    state = {"tier": "simple", "during_await": None}

    class _Oracle:
        async def ask(self, _state, questions):
            hook = state["during_await"]
            if hook is not None:
                hook()
            return {q.id: Answer(id=q.id, value=state["tier"], p=0.9) for q in questions}

    monkeypatch.setattr(impl_mod, "JevOracle", lambda provider: _Oracle())

    def _set(tier: str, during_await=None) -> None:
        state["tier"] = tier
        state["during_await"] = during_await

    return _set


def _following_client(state, client) -> None:
    """Script a clean turn on a client whose served model FOLLOWS ``set_model``.

    The fixed-attribute double used by the apply suite cannot show this behaviour:
    it reports the same served model whatever was switched to, so a later turn
    reads the same baseline as the first no matter what the first turn did.
    """
    client.available_models = MagicMock(return_value=[{"modelId": name} for name in ADVERTISED])
    client.served_model = BASE_MODEL

    async def _set_model(name: str) -> None:
        client.served_model = name

    client.set_model = AsyncMock(side_effect=_set_model)

    async def _stream(*_args, **_kwargs):
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="an answer")
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

    client.stream = MagicMock(side_effect=lambda *a, **k: _stream())


def _routed(key):
    """A slot armed for routing. Slots sharing one client share one wire session."""
    slot = _slot(key)
    slot.jev_route = True
    return slot


def _strip_rows(slot):
    """Every ``model.route`` strip row on this slot's assistant messages, in order."""
    rows = []
    for message in slot.messages:
        if message.get("role") != "assistant":
            continue
        for row in (message.get("meta") or {}).get("decisions_strip") or []:
            if row.get("point") == mr.POINT:
                rows.append(row)
    return rows


def _switched(client):
    return [call.args[0] for call in client.set_model.await_args_list]


async def _turn(state, slot, message):
    with _quiet_sel():
        await chat_runner._run_chat(state, slot, message, _directive_user_origin=True)
    await _settle(slot)


@pytest.mark.asyncio
async def test_an_unpinned_second_turn_returns_to_the_model_the_first_turn_left(tmp_path, answers):
    """Two consecutive turns on one slot. Turn two is back on the base model."""
    state, client = _runner_state(tmp_path)
    _following_client(state, client)
    slot = _routed("chat-ratchet-1")

    # Turn one: answered `simple`, which IS pinned -> the session moves to cheap.
    answers("simple")
    await _turn(state, slot, "rename this variable")

    first = _strip_rows(slot)
    assert [row["tier"] for row in first] == ["simple"]
    assert first[0]["model_chosen"] == CHEAP_MODEL
    assert first[0]["baseline_model"] == BASE_MODEL, "turn one starts on the base model"
    assert client.served_model == CHEAP_MODEL

    # Turn two: answered `complex`, which is UNPINNED -> the tier applies nothing,
    # and the session goes back to the model it would have run on without routing.
    answers("complex")
    await _turn(state, slot, "redesign the scheduler")

    second = _strip_rows(slot)[len(first) :]
    assert [row["tier"] for row in second] == ["complex"]
    assert second[0]["model_chosen"] == "", "an unpinned tier applies no tier model"

    # The hardest turn of the conversation runs on the owner's own model, and its
    # receipt -- captioned "Model without routing" and "default:" on the strip --
    # names that model rather than the one a one-line rename was routed to.
    assert second[0]["baseline_model"] == BASE_MODEL
    assert client.served_model == BASE_MODEL
    # The restore IS a switch, so the row says whether it took and names the model
    # the turn ran on. A reader takes an absent `applied` as applied, so a row
    # silent about a restore would report a stuck turn as a healthy one.
    assert second[0]["applied"] is True
    assert second[0]["model_used"] == BASE_MODEL

    # Not a picker write: the stored pin is untouched, so nothing here depends on
    # `slot.model`.
    assert slot.model in ("", "auto")


@pytest.mark.asyncio
async def test_a_third_turn_routes_from_the_base_model_again(tmp_path, answers):
    """The baseline is the PRE-ROUTE model on every turn, not the last tier's.

    Turn three is answered `simple` again. Its receipt has to name the base model as
    what it would otherwise have run on: a baseline read off the live session would
    name the cheap model here on any session that had already been routed, which is
    what makes the receipt for a dear turn claim a cheap default.
    """
    state, client = _runner_state(tmp_path)
    _following_client(state, client)
    slot = _routed("chat-ratchet-2")

    answers("simple")
    await _turn(state, slot, "rename this variable")
    answers("complex")
    await _turn(state, slot, "redesign the scheduler")
    answers("simple")
    await _turn(state, slot, "rename another variable")

    rows = _strip_rows(slot)
    assert [row["tier"] for row in rows] == ["simple", "complex", "simple"]
    assert [row["baseline_model"] for row in rows] == [BASE_MODEL] * 3
    assert rows[2]["model_chosen"] == CHEAP_MODEL
    assert rows[2]["applied"] is True
    assert client.served_model == CHEAP_MODEL


@pytest.mark.asyncio
async def test_a_sibling_alias_restores_the_session_the_other_one_routed(tmp_path, answers):
    """TWO slots, ONE wire session. The baseline cannot be per-slot.

    Alias A routes the shared session onto the cheap pin. Alias B then gets an
    UNPINNED tier. Its own cache still says base, and a per-slot latch would have
    it compare two values of its own and switch nothing, leaving the session on A's
    pin with no alias able to put it back. The baseline is latched on the session,
    so B restores it.
    """
    state, client = _runner_state(tmp_path)
    _following_client(state, client)
    alias_a = _routed("chat-ratchet-3a")
    alias_b = _routed("chat-ratchet-3b")
    # B's cache as its own session start left it: the base model, which is also the
    # baseline. That equality is the whole defect -- comparing two values of its own,
    # B sees nothing to put back.
    alias_b.record_served_model(BASE_MODEL)

    answers("simple")
    await _turn(state, alias_a, "rename this variable")
    assert client.served_model == CHEAP_MODEL
    assert alias_b.served_model == BASE_MODEL, "B's own cache did not follow A"
    # A WARM session for B's turn, which is what production has: the runner syncs a
    # slot's served model only for a fresh or reloaded session, so nothing else
    # refreshes B's cache and the routing hook has to re-read it itself.
    state.sessions.get_or_create = AsyncMock(return_value=(client, False, False))

    answers("complex")
    await _turn(state, alias_b, "redesign the scheduler")

    assert client.served_model == BASE_MODEL, "B put the shared session back"
    assert _strip_rows(alias_b)[0]["baseline_model"] == BASE_MODEL
    assert _switched(client) == [CHEAP_MODEL, BASE_MODEL]


@pytest.mark.asyncio
async def test_a_session_whose_model_the_backend_never_named_keeps_its_model(tmp_path, answers):
    """No baseline id to go back to -> no switch, the same outcome a refusal has.

    `served_model` is `""` when the backend serves its own default without naming
    it. There is then no id a restore could ask for -- `set_model("")` is not a
    request any provider answers -- so the unpinned turn keeps the model it is on
    and the receipt says so.
    """
    state, client = _runner_state(tmp_path)
    _following_client(state, client)
    client.served_model = ""
    slot = _routed("chat-ratchet-4")

    answers("simple")
    await _turn(state, slot, "rename this variable")
    assert client.served_model == CHEAP_MODEL

    answers("complex")
    await _turn(state, slot, "redesign the scheduler")

    assert _switched(client) == [CHEAP_MODEL], "there is no baseline id to switch back to"
    assert client.served_model == CHEAP_MODEL
    rows = _strip_rows(slot)
    assert [row["baseline_model"] for row in rows] == ["", ""]


@pytest.mark.asyncio
async def test_the_sessions_first_explicit_pick_becomes_the_new_baseline(tmp_path, answers):
    """An explicit pick is a newer instruction than anything latched before it.

    The session's FIRST pick is the hard case: the epoch host carries no epoch at
    all when the baseline is latched, so the comparison is absent against 1. The
    record stores what it read and compares for EQUALITY, which makes that a move;
    an `isinstance` pair-test would read it as "not comparable", leave the latch
    looking fresh, and revert the session off the model the owner just chose -- on
    every unpinned turn after it too, since the latch is never re-taken. The read's
    `0` default is parity with the fallback restore probe, not a second mechanism.
    """
    state, client = _runner_state(tmp_path)
    _following_client(state, client)
    host = pick_epoch_host(client)
    assert not hasattr(host, "_explicit_pick_epoch") or not isinstance(
        getattr(host, "_explicit_pick_epoch"), int
    ), "the host must start with no integer epoch for this to be the first pick"
    slot = _routed("chat-ratchet-5")

    answers("simple")
    await _turn(state, slot, "rename this variable")
    assert client.served_model == CHEAP_MODEL

    # What the picker does, through any alias: the session moves and the SHARED
    # epoch is bumped from absent to 1.
    client.served_model = PICKED_MODEL
    host._explicit_pick_epoch = 1

    answers("complex")
    await _turn(state, slot, "redesign the scheduler")

    assert client.served_model == PICKED_MODEL, "the owner's own pick is left alone"
    assert _switched(client) == [CHEAP_MODEL], "no restore over a pick made by hand"
    assert _strip_rows(slot)[1]["baseline_model"] == PICKED_MODEL


@pytest.mark.asyncio
async def test_a_pick_landing_during_the_await_through_another_alias_is_kept(tmp_path, answers):
    """`decide` is a round trip, and a pick can land inside it through any alias.

    A pick through THIS slot updates this slot's cache, which the guard already
    sees. A pick through a sibling alias leaves the cache untouched and moves only
    the shared epoch, so the guard has to re-read that epoch or it applies the
    tier's model over a choice the owner made a moment ago.
    """
    state, client = _runner_state(tmp_path)
    _following_client(state, client)
    host = pick_epoch_host(client)
    host._explicit_pick_epoch = 0
    slot = _routed("chat-ratchet-6")

    def _pick_through_another_alias():
        client.served_model = PICKED_MODEL
        host._explicit_pick_epoch = 1

    answers("simple", during_await=_pick_through_another_alias)
    await _turn(state, slot, "rename this variable")

    assert _switched(client) == [], "the tier must not be applied over the pick"
    assert client.served_model == PICKED_MODEL


@pytest.mark.asyncio
async def test_a_pooled_runtime_claimed_for_a_new_session_latches_afresh(tmp_path, answers):
    """The record names the session it describes, so a reused host cannot leak it.

    A pooled runtime is one host object serving one session after another. The
    baseline carries the session id it was taken under, so the next session latches
    its own instead of restoring to a model the previous one served.
    """
    state, client = _runner_state(tmp_path)
    _following_client(state, client)
    host = pick_epoch_host(client)
    host._session_id = "sess-one"
    slot = _routed("chat-ratchet-7")

    answers("simple")
    await _turn(state, slot, "rename this variable")
    assert client.served_model == CHEAP_MODEL

    # The pool hands the same host to a new session, already serving its own model.
    host._session_id = "sess-two"
    client.served_model = PICKED_MODEL

    answers("complex")
    await _turn(state, slot, "redesign the scheduler")

    assert client.served_model == PICKED_MODEL, "the previous session's model is not restored"
    assert _strip_rows(slot)[1]["baseline_model"] == PICKED_MODEL


@pytest.mark.asyncio
async def test_a_restore_that_does_not_take_says_so_on_the_row(tmp_path, answers):
    """`set_model` is not required to raise when it declines.

    A backend that judges the model VALUE can exhaust its candidate ladder and
    return having stayed put. The row is durable and never rewritten, and its reader
    treats an ABSENT `applied` as applied, so an unpinned turn whose restore did not
    take has to say so -- otherwise the receipt reports a turn still on the cheap
    tier as one that went back to the owner's model.
    """
    state, client = _runner_state(tmp_path)
    _following_client(state, client)
    slot = _routed("chat-ratchet-8")

    answers("simple")
    await _turn(state, slot, "rename this variable")
    assert client.served_model == CHEAP_MODEL

    # Accepts the call, changes nothing: the session stays on the cheap tier.
    client.set_model = AsyncMock()

    answers("complex")
    await _turn(state, slot, "redesign the scheduler")

    row = _strip_rows(slot)[1]
    assert row["model_chosen"] == "", "the tier is still unpinned"
    assert row["applied"] is False
    assert row["model_used"] == CHEAP_MODEL, "the model the turn actually ran on"


@pytest.mark.asyncio
async def test_a_restore_that_raises_says_so_on_the_row(tmp_path, answers):
    """The same honesty when the provider refuses the restore outright.

    A raising restore is not the tier failing to apply -- no tier model was asked
    for -- so it is the ordinary outcome row rather than an `error` category. It
    still carries the verdict, because the turn is left on the previous tier.
    """
    state, client = _runner_state(tmp_path)
    _following_client(state, client)
    slot = _routed("chat-ratchet-9")

    answers("simple")
    await _turn(state, slot, "rename this variable")
    assert client.served_model == CHEAP_MODEL

    client.set_model = AsyncMock(side_effect=RuntimeError("the provider refused"))

    answers("complex")
    await _turn(state, slot, "redesign the scheduler")

    row = _strip_rows(slot)[1]
    assert row["tier"] == "complex"
    assert row["model_chosen"] == ""
    assert row["applied"] is False
    assert row["model_used"] == CHEAP_MODEL
    assert row.get("error") is None, "no tier model was asked for, so no error category"


@pytest.mark.asyncio
async def test_a_throttle_fallback_is_never_latched_as_the_baseline(tmp_path, answers):
    """The ladder's substitute is temporary, so it is not what the session runs.

    The model-fallback ladder moves a throttled session onto a substitute and the
    restore probe puts the primary back later, clearing the sticky state without
    bumping any pick epoch. A baseline latched from the live model during that
    window would name the substitute, survive the restore, and send every later
    unpinned turn back onto a model the ladder had already abandoned.
    """
    state, client = _runner_state(tmp_path)
    _following_client(state, client)
    slot = _routed("chat-ratchet-10")
    # What the ladder leaves on the slot: serving the substitute, primary remembered.
    client.served_model = "model-substitute"
    slot._active_fallback_model = "model-substitute"
    slot._fallback_primary_model = BASE_MODEL

    answers("simple")
    await _turn(state, slot, "rename this variable")

    host = pick_epoch_host(client)
    latched = getattr(host, chat_runner._JEV_ROUTE_BASELINE_ATTR)
    assert latched[1] == BASE_MODEL, "the primary is the baseline, not the substitute"
    assert _strip_rows(slot)[0]["baseline_model"] == BASE_MODEL


@pytest.mark.asyncio
async def test_no_restore_while_the_fallback_ladder_holds_the_session(tmp_path, answers):
    """An unpinned turn does not undo the ladder's own switch.

    The baseline is the primary the ladder walked away from, and that primary is the
    model that just failed. Putting it back is the restore probe's decision, taken
    when it has evidence the primary serves again -- not this turn's.
    """
    state, client = _runner_state(tmp_path)
    _following_client(state, client)
    slot = _routed("chat-ratchet-11")
    client.served_model = "model-substitute"
    slot._active_fallback_model = "model-substitute"
    slot._fallback_primary_model = BASE_MODEL

    # The window this test is about: the turn's own restore probe tries the primary
    # FIRST and the primary is still throttled, so the ladder keeps the substitute
    # and the routing hook below runs with the fallback still active.
    async def _refuse_the_primary(name: str) -> None:
        if name == BASE_MODEL:
            raise RuntimeError("the primary is still throttled")
        client.served_model = name

    client.set_model = AsyncMock(side_effect=_refuse_the_primary)

    answers("complex")
    await _turn(state, slot, "redesign the scheduler")

    # Exactly one attempt on the primary, and it is the probe's own. A restore from
    # the routing hook would be a second.
    assert _switched(client) == [BASE_MODEL], "the routing hook must attempt nothing"
    assert client.served_model == "model-substitute", "the ladder keeps its substitute"
    assert slot._active_fallback_model == "model-substitute"
    row = _strip_rows(slot)[0]
    assert row["model_chosen"] == ""
    assert row["baseline_model"] == BASE_MODEL
    assert "applied" not in row, "nothing was attempted, so there is no verdict"


@pytest.mark.asyncio
async def test_a_sibling_alias_reads_the_fallback_off_the_shared_marker(tmp_path, answers):
    """The sticky fields live on whichever alias took the swap; the marker is shared.

    Alias A's error ladder moves the session onto a substitute: A's slot carries the
    sticky fields and the provider carries the marker. Alias B has neither field, so
    a slot-only read tells B there is no fallback -- and B would latch the substitute
    as the model the session runs when nothing has moved it, then switch every later
    unpinned turn back onto a model the ladder had abandoned.
    """
    state, client = _runner_state(tmp_path)
    _following_client(state, client)
    alias_b = _routed("chat-ratchet-12")
    # What the ladder leaves behind: the marker on the shared provider, the sticky
    # fields on alias A's slot, which B cannot see.
    client.served_model = "model-substitute"
    setattr(client, TURN_FALLBACK_ATTR, (BASE_MODEL, "model-substitute"))
    assert not alias_b._active_fallback_model, "B's slot knows nothing about the swap"

    answers("simple")
    await _turn(state, alias_b, "rename this variable")

    host = pick_epoch_host(client)
    latched = getattr(host, chat_runner._JEV_ROUTE_BASELINE_ATTR)
    assert latched[1] == BASE_MODEL, "the primary is the baseline, not the substitute"
    assert _strip_rows(alias_b)[0]["baseline_model"] == BASE_MODEL


@pytest.mark.asyncio
async def test_no_restore_when_only_the_shared_marker_says_fallback(tmp_path, answers):
    """The suppression reads the same shared record the latch does.

    Same session state as above, and an UNPINNED tier this time. Read per slot, B
    would see no fallback, take the baseline as a restore target and switch the
    session onto the primary the ladder is deliberately away from.
    """
    state, client = _runner_state(tmp_path)
    _following_client(state, client)
    alias_b = _routed("chat-ratchet-13")
    client.served_model = "model-substitute"
    setattr(client, TURN_FALLBACK_ATTR, (BASE_MODEL, "model-substitute"))

    answers("complex")
    await _turn(state, alias_b, "redesign the scheduler")

    assert _switched(client) == [], "the ladder keeps the model it chose"
    assert client.served_model == "model-substitute"
    row = _strip_rows(alias_b)[0]
    assert row["model_chosen"] == ""
    assert row["baseline_model"] == BASE_MODEL
    assert "applied" not in row


@pytest.mark.asyncio
async def test_a_fallback_that_names_no_primary_latches_nothing(tmp_path, answers):
    """A record that cannot be named correctly is not written at all.

    The marker says a fallback serves the session and nothing names what it left.
    Latching the live model there would make the substitute the durable baseline,
    which is the defect the whole branch exists to avoid; the next turn latches once
    the ladder is done.
    """
    state, client = _runner_state(tmp_path)
    _following_client(state, client)
    slot = _routed("chat-ratchet-14")
    client.served_model = "model-substitute"
    setattr(client, TURN_FALLBACK_ATTR, ("", "model-substitute"))

    answers("complex")
    await _turn(state, slot, "redesign the scheduler")

    # The stub host answers every attribute, so the SHAPE is what says a record was
    # written: only a 3-tuple is one this reader would accept next turn.
    host = pick_epoch_host(client)
    assert not isinstance(
        getattr(host, chat_runner._JEV_ROUTE_BASELINE_ATTR, None), tuple
    ), "the substitute must not become the durable baseline"
    assert _switched(client) == []
    assert client.served_model == "model-substitute"
    # The receipt names what the turn ran on rather than inventing a default.
    assert _strip_rows(slot)[0]["baseline_model"] == "model-substitute"


@pytest.mark.asyncio
async def test_a_stranded_refusal_swap_is_not_latched_as_the_baseline(tmp_path, answers):
    """The refusal fallback keeps its own record, and it is not on the shared marker.

    A content-filter refusal swaps the session onto a configured substitute for one
    replayed message, and its restore runs at the start of the next genuine turn. A
    restore that could not land leaves the session on that candidate with only
    `_refusal_fallback_primary` naming what it replaced -- deliberately off
    `TURN_FALLBACK_ATTR`, whose start-of-turn probe would move the session back
    before the retry ran. Latched from the live model there, the candidate becomes
    the durable baseline and no pick epoch moves, so it survives the restore and
    every later unpinned turn switches back onto it.
    """
    state, client = _runner_state(tmp_path)
    _following_client(state, client)
    slot = _routed("chat-ratchet-15")
    client.served_model = "model-substitute"
    slot._refusal_fallback_primary = BASE_MODEL
    slot._refusal_fallback_candidate = "model-substitute"
    # The restore drops its record when a pick looks to have landed after the swap,
    # and the stub host answers every attribute -- so both epochs are pinned equal.
    pick_epoch_host(client)._explicit_pick_epoch = 0
    slot._refusal_client_pick_epoch = 0

    # The window: the turn's own refusal restore runs first and cannot land, so the
    # record is still there when the routing hook reads it.
    async def _refuse_the_primary(name: str) -> None:
        if name == BASE_MODEL:
            raise RuntimeError("the primary is still refusing")
        client.served_model = name

    client.set_model = AsyncMock(side_effect=_refuse_the_primary)

    answers("simple")
    await _turn(state, slot, "rename this variable")

    host = pick_epoch_host(client)
    latched = getattr(host, chat_runner._JEV_ROUTE_BASELINE_ATTR)
    assert latched[1] == BASE_MODEL, "the replaced model is the baseline"
    assert _strip_rows(slot)[0]["baseline_model"] == BASE_MODEL


@pytest.mark.asyncio
async def test_no_restore_while_a_refusal_swap_is_unrestored(tmp_path, answers):
    """An unpinned turn does not race the refusal restore for the same switch."""
    state, client = _runner_state(tmp_path)
    _following_client(state, client)
    slot = _routed("chat-ratchet-16")
    client.served_model = "model-substitute"
    slot._refusal_fallback_primary = BASE_MODEL
    slot._refusal_fallback_candidate = "model-substitute"
    # The restore drops its record when a pick looks to have landed after the swap,
    # and the stub host answers every attribute -- so both epochs are pinned equal.
    pick_epoch_host(client)._explicit_pick_epoch = 0
    slot._refusal_client_pick_epoch = 0

    # The window: the turn's own refusal restore runs first and cannot land, so the
    # record is still there when the routing hook reads it.
    async def _refuse_the_primary(name: str) -> None:
        if name == BASE_MODEL:
            raise RuntimeError("the primary is still refusing")
        client.served_model = name

    client.set_model = AsyncMock(side_effect=_refuse_the_primary)

    answers("complex")
    await _turn(state, slot, "redesign the scheduler")

    # One attempt on the primary, and it is the refusal restore's own.
    assert _switched(client) == [BASE_MODEL], "the refusal restore owns that switch"
    assert client.served_model == "model-substitute"
    row = _strip_rows(slot)[0]
    assert row["model_chosen"] == ""
    assert row["baseline_model"] == BASE_MODEL
