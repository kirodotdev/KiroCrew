"""The apply path: what the PROVIDER is handed when Jev answers a tier.

This is the load-bearing suite for ``model.route``. Everything else about the point
is observable from its own return value, but "the turn actually ran on the model the
tier names" is not: nothing downstream can tell "Jev said complex" from "the session
happened to be on that model already". So these tests drive the real runner hook
against a recording client and assert the id ``set_model`` received.

Two mutation checks sit beside the happy path and are what make it more than a
tautology. One breaks the tier-to-model MAP and asserts the provider is handed the
new id; the other changes the ANSWER and asserts the provider follows it. A hook
that ignored either -- switching to a hardcoded model, or switching on nothing --
passes a single happy-path assertion and fails both of these.

The rest is WHOSE turn gets routed. The point runs for a normal dashboard chat turn
of a slot whose owner picked ``Auto (Jev)``, and for nothing else: a cron delivery,
a sub-agent turn, an app injection and an autonudge wake each already resolve their
model through their own tier, and none of them has an owner watching the price.
"""

from __future__ import annotations

import json
from pathlib import Path
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
from kiro_crew.providers.base import LLMEvent

ADVERTISED = ["model-a", "model-b", "model-c"]

#: Nothing ships a tier map -- a hardcoded model id as a default is gated -- so
#: every test that expects a turn to move supplies one. Test files are outside the
#: tree the model-id gate reads.
TIER_MAP = {
    "simple": "model-a",
    "medium": "model-b",
    "complex": "model-c",
}


@pytest.fixture(autouse=True)
def clean_registry():
    outcomes.reset()
    yield
    outcomes.reset()


@pytest.fixture(autouse=True)
def consented(tmp_path, monkeypatch):
    """A real consented keystone, and a live snapshot that samples every session."""
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
        lambda: SimpleNamespace(decisions=DecisionsConfig(model_route=dict(TIER_MAP))),
    )


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


def _routed_slot(key: str = "chat-route-1"):
    slot = _slot(key)
    slot.jev_route = True
    return slot


def _turn_client(state, client) -> None:
    """Script one clean turn, and make the client answer the two model reads."""
    client.available_models = MagicMock(return_value=[{"modelId": name} for name in ADVERTISED])
    client.set_model = AsyncMock()
    client.served_model = "model-b"

    async def _stream(*_args, **_kwargs):
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="an answer")
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

    client.stream = MagicMock(side_effect=lambda *a, **k: _stream())


async def _run(tmp_path, slot, message="please redesign the scheduler", **kwargs):
    # ``_directive_user_origin`` defaults to what the dashboard's own send passes
    # (`not bool(request_app)` in `api_chat_send`), because "a normal chat turn" is
    # what most of these tests mean. An app-authored dispatch overrides it to False.
    kwargs.setdefault("_directive_user_origin", True)
    state, client = _runner_state(tmp_path)
    _turn_client(state, client)
    with _quiet_sel():
        await chat_runner._run_chat(state, slot, message, **kwargs)
    await _settle(slot)
    return client


def _rows(tmp_path):
    """Every decision row written under this test's log dir, oldest first."""
    rows = []
    for path in sorted((tmp_path / "decisions").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _switched_to(client) -> list[str]:
    return [call.args[0] for call in client.set_model.await_args_list]


# ---------------------------------------------------------------------------
# The apply path, and the two mutations that make it load-bearing
# ---------------------------------------------------------------------------


class TestTheProviderGetsTheMappedModel:
    @pytest.mark.asyncio
    async def test_a_complex_tier_puts_the_turn_on_the_complex_model(self, tmp_path, answers):
        answers("complex")
        client = await _run(tmp_path, _routed_slot())

        assert _switched_to(client) == ["model-c"]

    @pytest.mark.asyncio
    async def test_the_same_turn_follows_a_changed_map(self, tmp_path, answers, monkeypatch):
        """MUTATION 1 -- the map. A hook switching to a hardcoded model passes the
        test above and fails this one."""
        answers("complex")
        monkeypatch.setattr(
            gate_mod,
            "_snapshot",
            lambda: SimpleNamespace(decisions=DecisionsConfig(model_route={"complex": "model-a"})),
        )
        client = await _run(tmp_path, _routed_slot())

        assert _switched_to(client) == ["model-a"]

    @pytest.mark.asyncio
    async def test_the_same_map_follows_a_changed_answer(self, tmp_path, answers):
        """MUTATION 2 -- the tier. A hook switching on nothing (always the same
        row of the map) passes both tests above and fails this one."""
        answers("simple")
        client = await _run(tmp_path, _routed_slot())

        assert _switched_to(client) == ["model-a"]

    @pytest.mark.asyncio
    async def test_the_shipped_unpinned_map_switches_nothing_but_still_reports(
        self, tmp_path, answers, monkeypatch
    ):
        """The state every install starts in, driven through the real hook. No model
        id ships, so no `set_model` may be attempted -- and the receipt must still
        land, because that answer is what an owner pins from."""
        answers("complex")
        monkeypatch.setattr(
            gate_mod,
            "_snapshot",
            lambda: SimpleNamespace(
                decisions=DecisionsConfig(model_route={"simple": "", "medium": "", "complex": ""})
            ),
        )
        slot = _routed_slot()
        client = await _run(tmp_path, slot)

        assert _switched_to(client) == [], "an unpinned tier must not reach set_model"
        rows = [m for m in slot.messages if m.get("role") == "assistant"]
        strips = (rows[0].get("meta") or {}).get("decisions_strip") or []
        model_rows = [row for row in strips if row.get("point") == mr.POINT]
        assert len(model_rows) == 1
        assert model_rows[0]["tier"] == "complex"
        assert model_rows[0]["model_chosen"] == ""
        assert model_rows[0]["p"] == 0.91

    @pytest.mark.asyncio
    async def test_the_reply_carries_the_routing_receipt(self, tmp_path, answers):
        answers("complex")
        slot = _routed_slot()
        await _run(tmp_path, slot)

        rows = [m for m in slot.messages if m.get("role") == "assistant"]
        strips = (rows[0].get("meta") or {}).get("decisions_strip") or []
        model_rows = [row for row in strips if row.get("point") == mr.POINT]
        assert len(model_rows) == 1
        assert model_rows[0]["tier"] == "complex"
        assert model_rows[0]["model_chosen"] == "model-c"
        # The model the turn WOULD have used, read off the live session.
        assert model_rows[0]["baseline_model"] == "model-b"


# ---------------------------------------------------------------------------
# Every refusal keeps the model the session was already on
# ---------------------------------------------------------------------------


class TestRefusalsKeepTheCurrentModel:
    @pytest.mark.asyncio
    async def test_a_slot_the_owner_did_not_route_is_never_asked(self, tmp_path, answers):
        """The picker's `Auto (Jev)` entry is the whole arming surface: a manual
        model choice is never overridden."""
        answers("complex")
        client = await _run(tmp_path, _slot("chat-pinned"))

        assert _switched_to(client) == []

    @pytest.mark.asyncio
    async def test_an_unconsented_keystone_routes_nothing(self, tmp_path, answers, monkeypatch):
        answers("complex")
        monkeypatch.setattr(
            "kiro_crew.config.loader.decisions_consent_path", lambda: tmp_path / "absent.json"
        )
        client = await _run(tmp_path, _routed_slot())

        assert _switched_to(client) == []

    @pytest.mark.asyncio
    async def test_an_unsampled_session_routes_nothing(self, tmp_path, answers, monkeypatch):
        answers("complex")
        monkeypatch.setattr(
            gate_mod,
            "_snapshot",
            lambda: SimpleNamespace(
                decisions=DecisionsConfig(bucket=0, model_route=dict(TIER_MAP))
            ),
        )
        client = await _run(tmp_path, _routed_slot())

        assert _switched_to(client) == []

    @pytest.mark.asyncio
    async def test_an_id_the_account_cannot_run_routes_nothing(self, tmp_path, answers):
        answers("complex")
        state, client = _runner_state(tmp_path)
        _turn_client(state, client)
        # The account lost access to the complex tier's model.
        client.available_models = MagicMock(return_value=[{"modelId": "model-a"}])
        slot = _routed_slot()
        with _quiet_sel():
            await chat_runner._run_chat(state, slot, "please redesign the scheduler")
        await _settle(slot)

        assert _switched_to(client) == []
        rows = [m for m in slot.messages if m.get("role") == "assistant"]
        assert not ((rows[0].get("meta") or {}).get("decisions_strip") or [])

    @pytest.mark.asyncio
    async def test_a_failing_switch_keeps_the_turn_and_writes_no_receipt(self, tmp_path, answers):
        """The turn must survive a provider that refuses the switch: the seam may
        cost an observation and never a reply."""
        answers("complex")
        state, client = _runner_state(tmp_path)
        _turn_client(state, client)
        client.set_model = AsyncMock(side_effect=RuntimeError("model unavailable"))
        slot = _routed_slot()
        with _quiet_sel():
            await chat_runner._run_chat(state, slot, "please redesign the scheduler")
        await _settle(slot)

        rows = [m for m in slot.messages if m.get("role") == "assistant"]
        assert rows and "an answer" in rows[0]["content"]
        assert not ((rows[0].get("meta") or {}).get("decisions_strip") or [])

    @pytest.mark.asyncio
    async def test_a_provider_with_no_switch_seam_routes_nothing(self, tmp_path, answers):
        answers("complex")
        state, client = _runner_state(tmp_path)
        _turn_client(state, client)
        # A provider that cannot express a per-turn model at all.
        del client.set_model
        client._client = SimpleNamespace()
        slot = _routed_slot()
        with _quiet_sel():
            await chat_runner._run_chat(state, slot, "please redesign the scheduler")
        await _settle(slot)

        rows = [m for m in slot.messages if m.get("role") == "assistant"]
        assert rows and "an answer" in rows[0]["content"]


# ---------------------------------------------------------------------------
# Whose turn gets routed
# ---------------------------------------------------------------------------


class TestOnlyANormalChatTurn:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("actor", ["cron", "subagent", "app", "crew", "gateway"])
    async def test_a_turn_no_owner_is_watching_is_never_routed(self, tmp_path, answers, actor):
        """Each of these already resolves its model through its own tier
        (`agent.role_models`, a crew binding, a cron's own slot), and none has an
        owner watching what a dearer model costs."""
        answers("complex")
        client = await _run(tmp_path, _routed_slot(), _turn_actor=actor)

        assert _switched_to(client) == []

    @pytest.mark.asyncio
    async def test_an_app_authored_turn_routes_nothing_even_unnamed(self, tmp_path, answers):
        """The dispatch paths an app reaches -- rewind, regenerate, the
        OpenAI-compatible route -- pass `_directive_user_origin=not bool(request_app)`
        and name NO actor, and the actor resolver's fallback is `user`. So the actor
        check alone admits them and the owner is billed for a routed turn nobody typed.
        Provenance is what the guard asks for; the actor is left unnamed here on
        purpose, because that is the shape those paths actually dispatch."""
        answers("complex")
        client = await _run(tmp_path, _routed_slot(), _directive_user_origin=False)

        assert _switched_to(client) == []

    @pytest.mark.asyncio
    async def test_an_autonudge_wake_is_never_routed(self, tmp_path, answers):
        answers("complex")
        client = await _run(tmp_path, _routed_slot(), _directive_self_wake=True)

        assert _switched_to(client) == []

    @pytest.mark.asyncio
    async def test_a_synthetic_payload_is_never_routed(self, tmp_path, answers):
        """A runner-authored continuation is not a request whose difficulty is a
        question, and re-routing mid-answer would swap the model under a turn
        already in progress."""
        answers("complex")
        client = await _run(tmp_path, _routed_slot(), _synthetic_payload=True)

        assert _switched_to(client) == []

    @pytest.mark.asyncio
    async def test_a_kind_tagged_recovery_requeue_is_never_routed(self, tmp_path, answers):
        """A requeue of the USER'S OWN words carries no marker text to match on.

        The runner requeues a turn after a pre-output failure, and on a
        poisoned-conversation discard the requeued text is what the person typed --
        so ``_SYNTHETIC_RECOVERY_MSGS`` membership, which is a fixed-text check,
        recognizes nothing. The structural flag is the only evidence, which is why
        the two sibling guards in this module pair it with the text check. Without
        it the owner pays for the same turn's tier twice and the model can change
        under an answer already in progress.

        The message here is the ordinary one every other test in this file sends,
        on purpose: a requeue whose text looks exactly like a first send is the
        whole case, and a marker string would make the text check sufficient.
        """
        answers("complex")
        client = await _run(tmp_path, _routed_slot(), _synthetic_recovery_turn=True)

        assert _switched_to(client) == []


class TestWhatTextIsClassified:
    @pytest.mark.asyncio
    async def test_app_injected_context_is_not_part_of_the_routed_text(
        self, tmp_path, answers, monkeypatch
    ):
        """The question carries the TYPED message, never the drained context prefix.

        ``_run_chat`` prepends whatever ``drain_pending_context`` returns onto the
        variable holding the turn's text, so by the routing hook that variable is
        app-authored in part. Two separate things are wrong if the hook reads it: the
        tier is decided on bytes nobody typed, and silent background context an app
        injected leaves the machine on a send whose consent names the person's own
        message.

        The drain only runs on the context-builder path, so this test supplies a real
        builder rather than the file's default state -- without one the prefix is
        never prepended and the assertion would hold no matter which variable the
        hook read.

        The assertion is on the FIRST argument of ``routed_model``, because that is
        the whole egress: the point derives the excerpt it sends from it. Both
        directions are held -- the typed text is present and the injected marker is
        absent -- so a hook that sent the prefix alone fails as loudly as one that
        sent both.
        """
        from kiro_crew.context import ContextBuilder
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        answers("complex")
        seen: list[str] = []
        real = mr.routed_model

        async def _capture(message, **kwargs):
            seen.append(message)
            return await real(message, **kwargs)

        monkeypatch.setattr(mr, "routed_model", _capture)
        state, client = _runner_state(tmp_path)
        _turn_client(state, client)
        state.context_builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        client.mcp_session_report = MagicMock(return_value=None)
        client.client = MagicMock(pop_pending_oauth_requests=MagicMock(return_value=[]))
        slot = _routed_slot()
        slot._pending_context = [{"content": "INJECTED-BYTES", "source": "an app"}]
        with _quiet_sel():
            await chat_runner._run_chat(
                state, slot, "please redesign the scheduler", _directive_user_origin=True
            )
        await _settle(slot)

        assert seen == ["please redesign the scheduler"]
        assert "INJECTED-BYTES" not in seen[0]
        # The turn still routes on the typed text; the guard is about WHAT was sent.
        assert _switched_to(client) == ["model-c"]

    @pytest.mark.asyncio
    async def test_runner_authored_prepends_are_not_part_of_the_routed_text(
        self, tmp_path, answers, monkeypatch
    ):
        """Nor the preamble and failure text the RUNNER prepends before the drain.

        The cancelled-turn preamble is prior TRANSCRIPT, and prior transcript reaches
        this send through exactly one door: the consented ``history_budget_chars``
        ceiling, whose shipped value is 0. A snapshot taken after the preamble
        therefore hands Jev the previous turn's text on an install that consented to
        none of it -- which is why the routing text is snapshotted beside the mirror's
        rather than sharing it. Sub-agent failure text is the same shape: nobody typed
        it, and it would decide the tier.

        All three prepends are present in one turn, so the assertion names the one
        variable that is right rather than passing on whichever prepend happens to be
        empty.
        """
        import kiro_crew.context as context_mod
        from kiro_crew.context import ContextBuilder
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        answers("complex")
        seen: list[str] = []
        real = mr.routed_model

        async def _capture(message, **kwargs):
            seen.append(message)
            return await real(message, **kwargs)

        monkeypatch.setattr(mr, "routed_model", _capture)
        monkeypatch.setattr(
            context_mod, "build_cancelled_turn_preamble", lambda *_a, **_k: "PREAMBLE-BYTES"
        )
        state, client = _runner_state(tmp_path)
        _turn_client(state, client)
        state.context_builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        # The preamble branch reads the builder's own log, and the runner reaches it
        # only for a session the manager flags with ``prev_turn_cancelled``.
        state.context_builder.conversation_log = state.conversation_log
        state.sessions._sessions = {
            "chat-route-1": SimpleNamespace(prev_turn_cancelled=True),
        }
        client.mcp_session_report = MagicMock(return_value=None)
        client.client = MagicMock(pop_pending_oauth_requests=MagicMock(return_value=[]))
        slot = _routed_slot()
        slot._pending_subagent_failures = ["FAILURE-BYTES"]
        slot._pending_context = [{"content": "INJECTED-BYTES", "source": "an app"}]
        with _quiet_sel():
            await chat_runner._run_chat(
                state, slot, "please redesign the scheduler", _directive_user_origin=True
            )
        await _settle(slot)

        assert seen == ["please redesign the scheduler"]
        for authored in ("PREAMBLE-BYTES", "FAILURE-BYTES", "INJECTED-BYTES"):
            assert authored not in seen[0]
        assert _switched_to(client) == ["model-c"]


# ---------------------------------------------------------------------------
# The flag itself
# ---------------------------------------------------------------------------


class TestTheRoutingFlag:
    def test_a_new_slot_is_not_routed(self):
        assert _slot("chat-fresh").jev_route is False

    def test_the_flag_never_touches_agent_writable_transcript_metadata(self):
        """The flag records an OWNER's pick that spends money -- a routed turn can
        run on a dearer model -- and transcript metadata is editable by the agent's
        own file tools. Persisting it there would let a prompt-injected agent write
        `"jev_route": true`, restart the gateway, and be granted routing the owner
        never selected. So neither loader reads it and neither writer writes it.

        Asserted against the SOURCE of every site that handles this file, because
        the defect is an absence: a future edit that re-adds the key would restore
        the hole silently, and no behavioural test of a restored slot can see a key
        nobody wrote."""
        import kiro_crew.dashboard.channel_slots as channel_slots
        import kiro_crew.dashboard.chat_persistence as persistence

        for module in (persistence, channel_slots):
            source = Path(module.__file__).read_text(encoding="utf-8")
            offenders = [
                line.strip()
                for line in source.splitlines()
                if "jev_route" in line and not line.lstrip().startswith("#")
            ]
            assert offenders == [], (
                f"{Path(module.__file__).name} reads or writes jev_route through the "
                f"transcript metadata again: {offenders}"
            )

    def test_the_slot_payload_always_reports_it(self):
        """A positive value rather than an absent key, so a stale client cannot read
        a routed session as pinned. Read through the slot's OWN projection, which is
        what the dashboard receives."""
        assert _routed_slot().to_dict()["jev_route"] is True
        assert _slot("chat-b").to_dict()["jev_route"] is False

    def test_a_refused_pick_leaves_the_routing_flag_alone(self):
        """A pick the handler answers 409 for changed nothing, so it must not change
        what the NEXT turn runs on either. Asserted on the source order rather than
        by driving the handler: the flag is committed on each success path, and the
        defect was a write placed ABOVE the busy check where no rollback covers it."""
        import kiro_crew.dashboard.chat_handlers as handlers

        source = Path(handlers.__file__).read_text(encoding="utf-8")
        body = source[source.index("async def api_chat_slot_model(") :]
        body = body[: body.index("\nasync def ")]
        busy = body.index('"code": "turn_in_flight"')
        writes = [
            body.count("slot.jev_route = jev_route"),
            body[:busy].count("slot.jev_route = jev_route"),
        ]
        assert writes[0] == 2, "the flag should be committed on exactly the two success paths"
        assert writes[1] == 1, (
            "a jev_route write sits above the busy 409 on a path the rollback does not "
            "cover, so a refused pick would leak into the next turn"
        )
        # The one write above the 409 is the same-value shortcut, which RETURNS ok.
        shortcut = body.index("slot.jev_route = jev_route")
        assert (
            body.index('"ok": True', shortcut) < busy
        ), "the early flag write is no longer on a success path"
        # And the transaction's write is covered by the rollback.
        assert "slot.jev_route = prior_jev_route" in body

    def test_the_sentinel_is_not_a_provider_model_id(self):
        """`slot.model` reaches `session/set_model`, the session allocation and the
        composer chip. The sentinel therefore never lands in it."""
        from kiro_crew.dashboard.chat_handlers import JEV_ROUTE_MODEL, _is_jev_route_pick

        assert JEV_ROUTE_MODEL == "auto:jev"
        assert _is_jev_route_pick("auto:jev") is True
        assert _is_jev_route_pick(" auto:jev ") is True
        for other in ["auto", "", "auto:", "jev", "auto:jev:x", None, 7]:
            assert _is_jev_route_pick(other) is False


# ---------------------------------------------------------------------------
# Who may arm it, and whose instruction wins when two arrive at once
# ---------------------------------------------------------------------------


def _owner_app(state):
    """The slot-model route behind the identity middleware, so a test can pick a caller.

    ``X-Test-User`` selects the caller: the default reads as the local owner, any
    other subject reads as an authenticated non-owner whose ``app`` claim is empty --
    the shape an allow-listed messaging identity carries, which the cross-app guard
    admits.
    """
    from aiohttp import web
    from dashboard_owner_helpers import _identity

    from kiro_crew.dashboard.chat import api_chat_slot_model

    app = web.Application(middlewares=[_identity])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/model", api_chat_slot_model)
    return app


class TestArmingIsAnOwnerAction:
    @pytest.mark.asyncio
    async def test_a_non_owner_cannot_arm_routing_but_can_still_pick_a_model(self, tmp_path):
        """Routing spends the OWNER's credential on a model the caller never names, so
        the arm answers to the owner predicate. The second half is what keeps the gate
        honest: the same caller's PLAIN pick is untouched, so this is an authorization
        boundary around one field rather than a lock on the route."""
        from aiohttp.test_utils import TestClient, TestServer

        state, _client = _runner_state(tmp_path)
        slot = _slot("chat-arm-1")
        state._slots[slot.key] = slot

        async with TestClient(TestServer(_owner_app(state))) as http:
            armed = await http.post(
                f"/api/chat/slots/{slot.key}/model",
                json={"model": "auto:jev"},
                headers={"X-Test-User": "someone-else"},
            )
            assert armed.status == 403
            assert (await armed.json())["code"] in ("owner_only", "owner_session_stale")
            assert slot.jev_route is False

            plain = await http.post(
                f"/api/chat/slots/{slot.key}/model",
                json={"model": "model-a"},
                headers={"X-Test-User": "someone-else"},
            )
            assert plain.status != 403

    def test_a_fork_inherits_routing_only_for_the_owner(self):
        """Inheriting arms a SECOND routed session. The fork route is gated on app
        ownership, which the same non-owner passes for a slot the owner armed, so the
        copy carries the owner predicate itself. Asserted on the source because the
        defect is an ABSENT condition on one assignment."""
        import kiro_crew.dashboard.chat_fork as fork

        source = Path(fork.__file__).read_text(encoding="utf-8")
        assert "new_slot.jev_route = slot.jev_route and is_owner_dashboard_request(request)" in (
            source
        ), "the fork copies the routing flag without asking whether the forker is the owner"


class TestAManualPickDuringTheAwaitWins:
    @pytest.mark.asyncio
    async def test_a_pick_that_lands_during_the_await_is_not_overwritten(
        self, tmp_path, monkeypatch
    ):
        """``decide`` is a network round trip and is deliberately outside the locks, so
        the premise it was computed against can move while it is in flight. A pick made
        by hand is the newer instruction: the answer is dropped, not applied.

        Driven through the oracle, which is the only place inside the await window: it
        moves the slot the way a landed pick does. Both halves of the premise are
        checked here -- the model the baseline named, and the flag any manual pick
        clears -- because either alone would leave a live overwrite path."""
        import kiro_crew.decisions.impl_jev as impl_mod

        for moved in ("model", "flag"):
            state, client = _runner_state(tmp_path)
            _turn_client(state, client)
            slot = _routed_slot(f"chat-repick-{moved}")
            slot.served_model = "model-b"

            class _PickingOracle:
                async def ask(self, _state, questions):
                    if moved == "model":
                        slot.served_model = "model-a"
                    else:
                        slot.jev_route = False
                    return {q.id: Answer(id=q.id, value="complex", p=0.93) for q in questions}

            monkeypatch.setattr(impl_mod, "JevOracle", lambda provider: _PickingOracle())

            with _quiet_sel():
                await chat_runner._run_chat(state, slot, "please redesign the scheduler")
            await _settle(slot)

            assert "model-c" not in _switched_to(client), (
                f"the routed model was applied after the {moved} moved during the await, "
                f"overwriting a newer instruction"
            )


class TestASwitchThatDoesNotTake:
    @pytest.mark.asyncio
    async def test_the_row_says_not_applied_and_names_the_model_the_turn_ran_on(
        self, tmp_path, answers
    ):
        """`set_model` is not required to raise when it declines: a backend that judges
        the model VALUE exhausts its candidate ladder and returns, having stayed on the
        backend default (`acp/session_handle.py`). The outcome row is durable and never
        rewritten, so believing the call means the row claims a model the turn did not
        run on -- and that row is what an owner reads when deciding what to pin.

        The recording client here accepts the call and reports the same served model
        afterwards, which is exactly that shape."""
        answers("complex")
        state, client = _runner_state(tmp_path)
        _turn_client(state, client)
        # Accepts the switch, changes nothing: `served_model` stays where it started.
        client.set_model = AsyncMock()
        slot = _routed_slot()
        slot.served_model = "model-b"

        with _quiet_sel():
            await chat_runner._run_chat(
                state, slot, "please redesign the scheduler", _directive_user_origin=True
            )
        await _settle(slot)

        assert _switched_to(client) == ["model-c"], "the switch should still be attempted"
        row = _rows(tmp_path)[-1]
        assert row["model_chosen"] == "model-c"
        assert row["applied"] is False
        assert row["model_used"] == "model-b"
