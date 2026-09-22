"""The auto default: an unpinned slot routes while the Jev preview is on.

A slot NAMING NO MODEL is one of the two ways into ``model.route``, beside the
owner's explicit ``Auto (Jev)`` pick (``slot.jev_route``), and the two are one
answer to one question: ``auto`` and ``""`` both mean "the owner pinned nothing
here", which is exactly what the point is for. It is what makes the preview a
feature rather than a per-session chore -- an owner who consented in Settings
routes every unpinned session without arming each one -- and it is the only way a
freshly dispatched worker slot routes at all, since its ``model`` is ``""`` and
nobody is sitting at its picker. A slot that DOES name a model is never routed: a
pin is the owner answering this point by hand.

What neither way in changes is the envelope. Routing can only reach a model the
owner listed in ``decisions.model_route``, and only while the keystone says
``enabled: true`` -- neither of which any agent or app can write. That is why the
preview being on is the whole authorization, and why this file asserts the four
corners of it: preview on + unpinned routes, preview on + pinned does not, preview
off routes nothing whatever the slot says, and a turn a conductor delivered
(``session_send``, which queues with ``user_origin=False``) routes like one typed
into the composer.

The suite drives the real runner hook against a recording client, like
``test_decisions_model_route_apply.py``, and asserts the id ``set_model`` received:
"the turn ran on the tier's model" is not observable any other way.
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
from kiro_crew.decisions.types import Answer
from kiro_crew.providers.base import LLMEvent

ADVERTISED = ["model-a", "model-b", "model-c"]

#: No tier map ships -- a hardcoded model id as a default is gated -- so every test
#: that expects a turn to move supplies one. Test files are outside the tree the
#: model-id gate reads.
TIER_MAP = {"simple": "model-a", "medium": "model-b", "complex": "model-c"}


@pytest.fixture(autouse=True)
def clean_registry():
    outcomes.reset()
    yield
    outcomes.reset()


@pytest.fixture
def keystone(tmp_path, monkeypatch):
    """Redirect the keystone and the log dir; returns a writer for the consent state.

    Deliberately NOT autouse-consented: half this file is about the preview being
    OFF, and a fixture that consented for every test would make the off cases
    depend on tearing consent down again.
    """
    path = tmp_path / "decisions_consent.json"
    monkeypatch.setattr("kiro_crew.config.loader.decisions_consent_path", lambda: path)
    monkeypatch.setattr(log_mod, "log_dir", lambda: tmp_path / "decisions")
    monkeypatch.setattr(
        gate_mod,
        "_snapshot",
        lambda: SimpleNamespace(decisions=DecisionsConfig(model_route=dict(TIER_MAP))),
    )

    def _write(enabled: bool) -> None:
        if not enabled:
            # An ABSENT keystone, not `enabled: false`: that is the state every
            # install ships in, and it is what the preview being off means.
            if path.exists():
                path.unlink()
            return
        path.write_text(
            json.dumps({"enabled": True, "endpoint": DECISION_PROVIDER_ENDPOINT_DEFAULT}),
            encoding="utf-8",
        )

    return _write


@pytest.fixture
def answers(monkeypatch):
    """Install an oracle answering a fixed tier; returns a setter."""
    import kiro_crew.decisions.impl_jev as impl_mod

    state = {"tier": "complex"}

    class _Oracle:
        async def ask(self, _state, questions):
            return {q.id: Answer(id=q.id, value=state["tier"], p=0.91) for q in questions}

    monkeypatch.setattr(impl_mod, "JevOracle", lambda provider: _Oracle())

    def _set(tier: str) -> None:
        state["tier"] = tier

    return _set


def _turn_client(state, client) -> None:
    """Script one clean turn, and make the client answer the two model reads."""
    client.available_models = MagicMock(return_value=[{"modelId": name} for name in ADVERTISED])
    client.set_model = AsyncMock()
    client.served_model = "model-b"

    async def _stream(*_args, **_kwargs):
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="an answer")
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

    client.stream = MagicMock(side_effect=lambda *a, **k: _stream())


async def _run(tmp_path, slot, **kwargs):
    """One turn through the real hook. ``user_origin`` defaults to the composer's."""
    kwargs.setdefault("_directive_user_origin", True)
    state, client = _runner_state(tmp_path)
    _turn_client(state, client)
    with _quiet_sel():
        await chat_runner._run_chat(state, slot, "please redesign the scheduler", **kwargs)
    await _settle(slot)
    return client


def _switched_to(client) -> list[str]:
    return [call.args[0] for call in client.set_model.await_args_list]


# ---------------------------------------------------------------------------
# The predicate, on its own
# ---------------------------------------------------------------------------


class TestWhichSlotsAreArmed:
    @pytest.mark.parametrize("model", ["", "auto", "  AUTO  ", "Auto"])
    def test_a_slot_naming_no_model_is_armed(self, model):
        """Both spellings, and neither is case- or whitespace-sensitive: the value
        reaches the slot from a client, and a near-miss that reads as a pin would
        silently turn the feature off for that session."""
        slot = _slot("chat-armed")
        slot.model = model

        assert chat_runner._jev_route_armed(slot) is True

    @pytest.mark.parametrize("model", ["model-a", "auto:jev", "claude-haiku-4-5"])
    def test_a_slot_naming_a_model_is_not_armed(self, model):
        """A pin is the owner answering this point by hand. ``auto:jev`` is here as a
        sentinel that must never reach the slot: it is resolved to ``auto`` at the
        top of the model handler, so a slot still holding it is a bug, and reading it
        as unpinned would hide that bug."""
        slot = _slot("chat-pinned")
        slot.model = model

        assert chat_runner._jev_route_armed(slot) is False

    def test_the_owners_explicit_pick_arms_a_slot_whatever_its_model_says(self):
        """The first way in still stands on its own, so the sentinel's own path does
        not depend on the model field agreeing with it."""
        slot = _slot("chat-explicit")
        slot.model = "model-a"
        slot.jev_route = True

        assert chat_runner._jev_route_armed(slot) is True


# ---------------------------------------------------------------------------
# The four corners, through the real hook
# ---------------------------------------------------------------------------


class TestThePreviewIsTheAuthorization:
    @pytest.mark.asyncio
    async def test_preview_on_and_an_unpinned_slot_routes(self, tmp_path, keystone, answers):
        """The headline: the owner turned the preview on and never touched the
        picker, and the turn still lands on the complex tier's model."""
        keystone(True)
        answers("complex")
        slot = _slot("chat-auto")
        assert slot.model == "", "the fixture's slot must be unpinned for this to mean anything"

        client = await _run(tmp_path, slot)

        assert _switched_to(client) == ["model-c"]

    @pytest.mark.asyncio
    async def test_preview_on_and_a_pinned_slot_routes_nothing(self, tmp_path, keystone, answers):
        """MUTATION of the case above: only the model field moves. A hook that routed
        every turn once the preview was on passes the test above and fails this one,
        having overridden a choice the owner made by hand."""
        keystone(True)
        answers("complex")
        slot = _slot("chat-pinned")
        slot.model = "model-a"

        client = await _run(tmp_path, slot)

        assert _switched_to(client) == []

    @pytest.mark.asyncio
    async def test_preview_off_routes_nothing_for_an_unpinned_slot(
        self, tmp_path, keystone, answers
    ):
        """The shipped state: no keystone, every slot unpinned, and nothing is sent.
        This is the case the in-memory arm deliberately cannot decide, so it is the
        one that proves the off-loop preview read is wired."""
        keystone(False)
        answers("complex")

        client = await _run(tmp_path, _slot("chat-auto-off"))

        assert _switched_to(client) == []

    @pytest.mark.asyncio
    async def test_preview_off_routes_nothing_even_for_the_explicit_pick(
        self, tmp_path, keystone, answers
    ):
        """A slot still carrying the flag from before consent was withdrawn is not a
        grant. ``decide`` re-reads the keystone, so the flag alone buys nothing."""
        keystone(False)
        answers("complex")
        slot = _slot("chat-explicit-off")
        slot.jev_route = True

        client = await _run(tmp_path, slot)

        assert _switched_to(client) == []


class TestADeliveredTurnRoutesToo:
    @pytest.mark.asyncio
    async def test_a_conductor_delivered_turn_routes(self, tmp_path, keystone, answers):
        """A dispatched worker's turns arrive through ``session_send``, which queues
        with ``user_origin=False`` and names no actor. That is the shape that made the
        feature unreachable for exactly the sessions with nobody at the picker, so it
        is the shape asserted here.

        It is inside the envelope for the same reason a composer turn is: the tier map
        and the keystone are the only things routing spends against, and a worker
        cannot write either."""
        keystone(True)
        answers("complex")

        client = await _run(tmp_path, _slot("chat-worker"), _directive_user_origin=False)

        assert _switched_to(client) == ["model-c"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("actor", ["cron", "subagent", "app", "crew", "gateway"])
    async def test_a_producer_that_names_itself_is_still_excluded(
        self, tmp_path, keystone, answers, actor
    ):
        """Relaxing provenance did not relax the ACTOR check. Each of these resolves
        its model through its own tier already, and an unpinned slot must not become a
        second, contradicting answer for the same turn."""
        keystone(True)
        answers("complex")

        client = await _run(tmp_path, _slot("chat-actor"), _turn_actor=actor)

        assert _switched_to(client) == []


class TestTheEnvelopeIsTheCeiling:
    @pytest.mark.asyncio
    async def test_an_unlisted_tier_cannot_reach_a_model(
        self, tmp_path, keystone, answers, monkeypatch
    ):
        """What bounds an auto-routed turn is the owner's map, not the arm.

        This is the assertion that replaces the provenance check: with the preview on
        and every slot armed by default, the only thing standing between a turn and a
        dearer model is that the owner LISTED it. An answered tier the map does not
        name switches nothing, so no widening of who may route can widen what they
        reach."""
        keystone(True)
        answers("complex")
        monkeypatch.setattr(
            gate_mod,
            "_snapshot",
            lambda: SimpleNamespace(decisions=DecisionsConfig(model_route={"simple": "model-a"})),
        )

        client = await _run(tmp_path, _slot("chat-unlisted"))

        assert _switched_to(client) == []

    @pytest.mark.asyncio
    async def test_the_auto_default_reaches_only_advertised_ids(self, tmp_path, keystone, answers):
        """A map naming a model this account cannot run keeps the session's own
        model, which is the same refusal the explicit arm gets."""
        keystone(True)
        answers("complex")
        state, client = _runner_state(tmp_path)
        _turn_client(state, client)
        client.available_models = MagicMock(return_value=[{"modelId": "model-a"}])
        with _quiet_sel():
            await chat_runner._run_chat(
                state, _slot("chat-withheld"), "please redesign the scheduler"
            )

        assert _switched_to(client) == []


class TestRestoredProvenanceDoesNotRoute:
    @pytest.mark.asyncio
    async def test_a_turn_drained_from_a_restored_entry_routes_nothing(
        self, tmp_path, keystone, answers
    ):
        """The one arm that spends on an actor of ``user`` must not take a turn whose
        author is whoever could write the session file.

        A queue entry restored from disk arrives with no provenance at all: the
        repository drops the directive flags AND the actor stamp, because the line it
        came off is an ordinary writable file in the crew home. But an ABSENT actor
        resolves to ``user`` in the drain, which is exactly the arm this point admits
        -- so dropping the stamp is only half a fail-closed rule. The drain therefore
        also says that the provenance is a previous process's, and the gate refuses on
        that rather than on the actor it cannot trust.

        The cost is the same one ``slot.jev_route`` already documents for a restart:
        the turn runs on the model the session is already on."""
        keystone(True)
        answers("complex")

        client = await _run(tmp_path, _slot("chat-restored"), _turn_provenance_restored=True)

        assert _switched_to(client) == []

    @pytest.mark.asyncio
    async def test_the_same_turn_routes_when_this_process_accepted_it(
        self, tmp_path, keystone, answers
    ):
        """MUTATION of the case above: only the provenance flag moves. Without this,
        a gate that refused every turn would pass the test above and take the feature
        with it."""
        keystone(True)
        answers("complex")

        client = await _run(tmp_path, _slot("chat-in-process"), _turn_provenance_restored=False)

        assert _switched_to(client) == ["model-c"]
