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
    async def test_a_slot_that_names_a_model_is_never_asked(self, tmp_path, answers):
        """A model picked by hand is never overridden. It is the OWNER answering the
        very question this point asks, so it wins over the tier map -- which is what
        keeps the auto default from being a way to ignore a pin."""
        answers("complex")
        pinned = _slot("chat-pinned")
        pinned.model = "model-a"
        client = await _run(tmp_path, pinned)

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
    async def test_a_turn_with_no_human_provenance_is_routed_inside_the_owner_envelope(
        self, tmp_path, answers
    ):
        """`_directive_user_origin` is NOT a condition, and that is the point.

        A turn delivered into a slot by something other than its own composer --
        a conductor's `session_send`, a rewind, the OpenAI-compatible route -- reaches
        here with `user_origin=False` and an unnamed actor. It routes, because what
        routing spends against is not the provenance of the TEXT but an envelope only
        the owner can write: the keystone the preview reads, and the
        `decisions.model_route` map that names every model a turn may land on. Both
        live outside anything an agent or an app can edit, so admitting the turn
        cannot reach a model the owner did not list.

        The producers that DECLARE themselves are still excluded by the actor check
        beside this one (the parametrized test above), because each of those already
        resolves its model through its own tier."""
        answers("complex")
        client = await _run(tmp_path, _routed_slot(), _directive_user_origin=False)

        assert _switched_to(client) == ["model-c"]

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
        # Two halves since the route was split: the wrapper answers the owner
        # predicate from the request, and the shared core applies that answer.
        assert (
            "jev_route_allowed=is_owner_dashboard_request(request)" in source
        ), "the fork route no longer asks whether the forker is the owner"
        assert "new_slot.jev_route = slot.jev_route and jev_route_allowed" in (
            source
        ), "the fork copies the routing flag without applying the owner predicate"


class TestAManualPickDuringTheAwaitWins:
    @pytest.mark.asyncio
    async def test_a_pick_that_lands_during_the_await_is_not_overwritten(
        self, tmp_path, monkeypatch
    ):
        """``decide`` is a network round trip and is deliberately outside the locks, so
        the premise it was computed against can move while it is in flight. A model
        picked by hand is the newer instruction: the answer is dropped, not applied.

        Driven through the oracle, which is the only place inside the await window: it
        moves the slot the way a landed pick does. Both halves of the premise are
        checked here -- the served model the baseline named, and the slot still being
        armed -- because either alone would leave a live overwrite path. The pin case
        moves `model` and NOT `served_model`, so it exercises the arm half only.

        A CLEARED `jev_route` is deliberately not one of the cases. With the preview
        on, a slot that still names no model is armed either way: picking plain `Auto`
        mid-await clears the flag and means "let Jev pick", so applying the answer is
        the instruction rather than an overwrite of it. Only a NAMED MODEL is a
        different instruction, which is what the `pin` case moves."""
        import kiro_crew.decisions.impl_jev as impl_mod

        for moved in ("served_model", "pin"):
            state, client = _runner_state(tmp_path)
            _turn_client(state, client)
            slot = _routed_slot(f"chat-repick-{moved}")
            slot.served_model = "model-b"

            class _PickingOracle:
                async def ask(self, _state, questions):
                    if moved == "served_model":
                        slot.served_model = "model-a"
                    else:
                        slot.model = "model-a"
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


# ---------------------------------------------------------------------------
# The two window rules, driven through the real hook
# ---------------------------------------------------------------------------

#: What the registry KNOWS about the three test ids. The ``simple`` pin is the
#: small one, which is the shape the rules exist for: the cheap tier is also the
#: one whose model has the least room to work in.
WINDOWS = {"model-a": 200_000, "model-b": 1_000_000, "model-c": 1_000_000}


@pytest.fixture
def windows(monkeypatch):
    """Pin the registry's window answers, as a MUTABLE ``{id: window}`` map.

    An id a test removes is one the registry has not met, which is the state both
    rules have to refuse nothing on.
    """
    from kiro_crew import model_registry

    live = dict(WINDOWS)
    monkeypatch.setattr(model_registry, "has_known_window", lambda name: name in live)
    monkeypatch.setattr(model_registry, "model_window", lambda name, **_kw: live.get(name))
    return live


@pytest.fixture
def answered(monkeypatch):
    """An oracle answering a chosen tier at a chosen probability."""
    import kiro_crew.decisions.impl_jev as impl_mod

    held = {"tier": "simple", "p": 0.41}

    class _Oracle:
        async def ask(self, _state, questions):
            return {q.id: Answer(id=q.id, value=held["tier"], p=held["p"]) for q in questions}

    monkeypatch.setattr(impl_mod, "JevOracle", lambda provider: _Oracle())

    def _set(tier: str, p: float) -> None:
        held.update(tier=tier, p=p)

    return _set


def _wired(
    tmp_path,
    *,
    serves: str,
    used: int = 0,
    limit: float = 70.0,
    served_window: int = 0,
    usage_unknown: bool = False,
):
    """``(state, client)`` for a session on *serves* with a context reading.

    The served model FOLLOWS ``set_model`` here, which the fixed-attribute double
    above cannot show: a refusal that lands somewhere is only observable as the
    model the next turn starts on.
    """
    state, client = _runner_state(tmp_path)
    client.available_models = MagicMock(return_value=[{"modelId": n} for n in ADVERTISED])
    client.served_model = serves
    client.context_used_tokens = MagicMock(return_value=used)
    # 0 is "this provider reports no window", which is what falls back to the
    # registry. A test with a live reading passes its own.
    client.context_window_tokens = MagicMock(return_value=served_window)
    # What the provider VOUCHES for: False means a 0 reading is a real empty
    # history rather than one nothing has measured.
    client.context_usage_unknown = MagicMock(return_value=usage_unknown)
    state.sessions.effective_autocompact_pct = MagicMock(return_value=limit)

    async def _set_model(name: str) -> None:
        client.served_model = name

    client.set_model = AsyncMock(side_effect=_set_model)

    async def _stream(*_args, **_kwargs):
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="an answer")
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

    client.stream = MagicMock(side_effect=lambda *a, **k: _stream())
    return state, client


async def _turn(state, slot, message: str = "please rename this variable") -> None:
    with _quiet_sel():
        await chat_runner._run_chat(state, slot, message, _directive_user_origin=True)
    await _settle(slot)


def _crew_log_calls(monkeypatch) -> list[str]:
    """Record which turn-level crew-log entries a turn writes, in order.

    The real writers still run: what is under test is WHICH of them a refusing gate
    reaches, and the log is append-only, so the order is the contract.
    """
    seen: list[str] = []

    def _spy(name: str) -> None:
        real = getattr(chat_runner.crew_log_emit, name)

        def _wrapped(*args, **kwargs):
            seen.append(name)
            return real(*args, **kwargs)

        monkeypatch.setattr(chat_runner.crew_log_emit, name, _wrapped)

    for name in ("on_message_received", "on_turn_refused", "on_turn_started"):
        _spy(name)
    return seen


def _refusals(tmp_path) -> list[dict]:
    return [row for row in _rows(tmp_path) if row.get("error") == mr.ERROR_WINDOW_REFUSED]


class TestARefusalStaysPut:
    @pytest.mark.asyncio
    async def test_a_refused_tier_asks_the_provider_for_nothing(self, tmp_path, answered, windows):
        """The turn keeps the window it has, which these rules already accepted. So
        there is no switch to make, nothing to land on and nothing to undo -- and the
        one observable form of that is a provider never asked."""
        answered("simple", 0.41)
        slot = _routed_slot("chat-stay-put")
        state, client = _wired(tmp_path, serves="model-b")
        await _turn(state, slot)

        assert _switched_to(client) == [], "set_model is never called"
        assert client.served_model == "model-b"
        assert client.stream.call_count == 1, "the turn still runs"
        row = _refusals(tmp_path)[0]
        assert row["tier"] == "simple"
        assert "applied" not in row and "model_used" not in row

    @pytest.mark.asyncio
    async def test_a_smaller_window_is_refused_with_no_model_to_go_back_to(
        self, tmp_path, answered, windows
    ):
        """A shrink is only applied because it can be TAKEN BACK. On a slot the backend
        never named a model for there is no id to ask for, so the shrink is refused on
        the grounds an unknown window is refused on -- even at full confidence with a
        history that would fit."""
        answered("simple", 0.99)
        slot = _routed_slot("chat-no-way-back")
        state, client = _wired(tmp_path, serves="", used=0, served_window=1_000_000)
        await _turn(state, slot)

        assert _switched_to(client) == [], "nothing to go back to, so nothing is tried"
        assert client.stream.call_count == 1, "the turn still runs"
        assert _refusals(tmp_path)[0]["tier"] == "simple"

    @pytest.mark.asyncio
    async def test_an_upgrade_needs_no_way_back(self, tmp_path, answered, windows):
        """MUTATION -- the rollback rule's scope. A window at least as large cannot
        compact anything, so it is applied on a slot with no model named just as it is
        anywhere else."""
        answered("complex", 0.41)
        slot = _routed_slot("chat-upgrade-no-baseline")
        state, client = _wired(tmp_path, serves="", used=0, served_window=200_000)
        await _turn(state, slot)

        assert _switched_to(client) == ["model-c"], "the bigger window is applied"
        assert _refusals(tmp_path) == []


class TestALowAnswerDoesNotShrinkTheWindow:
    @pytest.mark.asyncio
    async def test_a_low_probability_downgrade_is_refused(self, tmp_path, answered, windows):
        """A probability under the floor says the tier is not settled, not that the
        turn is hard, so the shrink it asked for is not taken and the session keeps the
        room it has."""
        answered("simple", 0.41)
        slot = _routed_slot("chat-window-1")
        state, client = _wired(tmp_path, serves="model-c")
        await _turn(state, slot)

        assert _switched_to(client) == []
        row = _refusals(tmp_path)[0]
        assert row["tier"] == "simple"
        assert row["p"] == 0.41
        assert "model_used" not in row and "applied" not in row

    @pytest.mark.asyncio
    async def test_the_same_downgrade_applies_when_the_answer_carries_it(
        self, tmp_path, answered, windows
    ):
        """MUTATION -- the probability. A hook refusing every downgrade passes the
        test above and fails this one: at the floor the tier IS applied."""
        answered("simple", 0.86)
        slot = _routed_slot("chat-window-2")
        state, client = _wired(tmp_path, serves="model-c")
        await _turn(state, slot)

        assert _switched_to(client) == ["model-a"]
        assert _refusals(tmp_path) == []

    @pytest.mark.asyncio
    async def test_the_same_window_is_not_a_downgrade(self, tmp_path, answered, windows):
        """Room that does not shrink changes nothing about what fits, so the
        probability is not asked to carry the move."""
        answered("complex", 0.41)
        slot = _routed_slot("chat-window-3")
        state, client = _wired(tmp_path, serves="model-b")
        await _turn(state, slot, "redesign the scheduler")

        assert _switched_to(client) == ["model-c"]
        assert _refusals(tmp_path) == []

    @pytest.mark.asyncio
    async def test_an_unknown_window_refuses_nothing(self, tmp_path, answered, windows):
        """Refusing on an unknown window would make routing inert on exactly the
        models nothing is known about."""
        answered("simple", 0.41)
        del windows["model-a"]
        slot = _routed_slot("chat-window-4")
        state, client = _wired(tmp_path, serves="model-c")
        await _turn(state, slot)

        assert _switched_to(client) == ["model-a"]
        assert _refusals(tmp_path) == []


class TestAHistoryThatWouldNotFit:
    @pytest.mark.asyncio
    async def test_a_confident_downgrade_is_refused_when_the_history_would_not_fit(
        self, tmp_path, answered, windows
    ):
        """The meter is a percentage of the SERVED window: 150k tokens sit at 15% of
        the large one and at 75% of the small one, above this session's compaction
        threshold. That turn would end by handing the backend a history it replaces
        with a summary, and no later switch back recovers it."""
        answered("simple", 0.95)
        slot = _routed_slot("chat-fit-1")
        state, client = _wired(tmp_path, serves="model-c", used=150_000, limit=70.0)
        await _turn(state, slot)

        assert _switched_to(client) == []
        assert _refusals(tmp_path)[0]["p"] == 0.95

    @pytest.mark.asyncio
    async def test_a_history_that_fits_is_applied(self, tmp_path, answered, windows):
        """MUTATION -- the reading. A hook refusing every confident downgrade passes
        the test above and fails this one."""
        answered("simple", 0.95)
        slot = _routed_slot("chat-fit-2")
        state, client = _wired(tmp_path, serves="model-c", used=10_000, limit=70.0)
        await _turn(state, slot)

        assert _switched_to(client) == ["model-a"]
        assert _refusals(tmp_path) == []

    @pytest.mark.asyncio
    async def test_a_large_message_over_a_small_history_is_refused(
        self, tmp_path, answered, windows
    ):
        """The meter answers for the history the session ALREADY holds. 100k held is
        50% of the 200k target and clears the threshold on its own; the 200k-character
        message this turn sends is another ~50k tokens, and the turn would end at 75%
        -- the compaction these rules exist to prevent. A fit test taken on the meter
        alone passes exactly that turn."""
        answered("simple", 0.95)
        slot = _routed_slot("chat-fit-prompt")
        state, client = _wired(tmp_path, serves="model-c", used=100_000, limit=70.0)
        await _turn(state, slot, "x" * 200_000)

        assert _switched_to(client) == []
        assert _refusals(tmp_path)[0]["p"] == 0.95


class TestTheFitReadingSizesTheAssembledPrompt:
    def test_the_rule_sizes_the_prompt_it_is_given(self, windows):
        """100k held is 50% of the 200k target and clears on its own. A short prompt
        keeps it there; the assembled one that a long session actually sends pushes the
        END of the turn over the threshold, which is what the rule is for."""
        client = MagicMock()
        client.context_window_tokens = MagicMock(return_value=1_000_000)
        client.context_used_tokens = MagicMock(return_value=100_000)
        client.context_usage_unknown = MagicMock(return_value=False)
        state = MagicMock()
        state.sessions.effective_autocompact_pct = MagicMock(return_value=70.0)
        sized = dict(
            current_window=1_000_000,
            target="model-a",
            p=0.95,
            # A model to go back to, so these cases are decided by the rule under
            # test rather than by the rollback rule.
            rollback_target="model-b",
        )

        assert (
            chat_runner._jev_downgrade_refused(
                state, client, "chat-1", mr, prompt="please rename this", **sized
            )
            is False
        )
        assert (
            chat_runner._jev_downgrade_refused(
                state, client, "chat-1", mr, prompt="x" * 200_000, **sized
            )
            is True
        )

    def test_both_threshold_readers_in_a_turn_adopt_a_published_change(self, monkeypatch):
        """The end-of-turn ladder adopts a newly published threshold before it reads,
        and the fit rule reads the same threshold BEFORE the turn. A read that skipped
        the sync would measure one turn against two different numbers, and the rule
        would clear a shrink the ladder then compacts on. Asked of the ANSWER each
        reader gives, so a reader that stops syncing fails here."""
        from kiro_crew import session as session_mod
        from kiro_crew.session import SessionManager

        monkeypatch.setattr(session_mod, "published_autocompact_pct", lambda: 55.0)

        def _owner():
            """A manager holding a STALE threshold, and a delegate that reports it."""
            held = SimpleNamespace(
                _adopted_autocompact_pct=70.0,
                _cfg=SimpleNamespace(session=SimpleNamespace(autocompact_pct=70.0)),
            )
            held._compaction = SimpleNamespace(
                effective_autocompact_pct=lambda _key: held._cfg.session.autocompact_pct,
                _compaction_gate_decision=lambda _k, _p, _pct: held._cfg.session.autocompact_pct,
            )
            # The real sync, so what is under test is the reader's call to it.
            held._sync_autocompact_pct = lambda: SessionManager._sync_autocompact_pct(held)
            return held

        assert SessionManager.effective_autocompact_pct(_owner(), "chat-1") == 55.0
        assert (
            SessionManager._compaction_gate_decision(_owner(), "chat-1", MagicMock(), 99.0) == 55.0
        )

    def test_an_unmeasurable_reading_refuses_and_a_vouched_for_zero_does_not(self, windows):
        """The guard PERMITS only on a reading that confirms the target holds this turn.

        A meter the provider itself calls unknown is the post-compaction and resumed
        state -- the history is real and unread -- so nothing confirms it and the turn
        keeps its window. A 0 the provider vouches for is a KNOWN small history, so a
        fresh session still downgrades: the two readings are identical on the wire and
        only this flag tells them apart."""
        state = MagicMock()
        state.sessions.effective_autocompact_pct = MagicMock(return_value=70.0)
        sized = dict(
            current_window=1_000_000,
            target="model-a",
            p=0.95,
            # A model to go back to, so these cases are decided by the rule under
            # test rather than by the rollback rule.
            rollback_target="model-b",
        )

        def _client(unknown: bool):
            client = MagicMock()
            client.context_window_tokens = MagicMock(return_value=1_000_000)
            client.context_used_tokens = MagicMock(return_value=0)
            client.context_usage_unknown = MagicMock(return_value=unknown)
            return client

        assert (
            chat_runner._jev_downgrade_refused(
                state, _client(True), "chat-1", mr, prompt="please rename this", **sized
            )
            is True
        )
        assert (
            chat_runner._jev_downgrade_refused(
                state, _client(False), "chat-1", mr, prompt="please rename this", **sized
            )
            is False
        )

    def test_a_reading_that_cannot_be_reached_refuses(self, windows):
        """A provider with no meter confirms nothing either, and a rule that permits
        only on a confirmation has to treat that the same way."""
        client = MagicMock()
        client.context_window_tokens = MagicMock(return_value=1_000_000)
        client.context_usage_unknown = MagicMock(side_effect=AttributeError("no meter"))
        state = MagicMock()
        state.sessions.effective_autocompact_pct = MagicMock(return_value=70.0)

        assert (
            chat_runner._jev_downgrade_refused(
                state,
                client,
                "chat-1",
                mr,
                current_window=1_000_000,
                target="model-a",
                p=0.95,
                prompt="please rename this",
                rollback_target="model-b",
            )
            is True
        )

    def test_a_prompt_that_alone_will_not_fit_is_refused_on_a_fresh_session(self, windows):
        """An unmeasured meter says nothing about the HISTORY and nothing about the
        prompt, which is in hand either way. Exempting the unmeasured case would exempt
        exactly the prompt that fills the small window on its own."""
        client = MagicMock()
        client.context_window_tokens = MagicMock(return_value=1_000_000)
        client.context_used_tokens = MagicMock(return_value=0)
        client.context_usage_unknown = MagicMock(return_value=False)
        state = MagicMock()
        state.sessions.effective_autocompact_pct = MagicMock(return_value=70.0)
        sized = dict(
            current_window=1_000_000,
            target="model-a",
            p=0.95,
            # A model to go back to, so these cases are decided by the rule under
            # test rather than by the rollback rule.
            rollback_target="model-b",
        )

        assert (
            chat_runner._jev_downgrade_refused(
                state, client, "chat-1", mr, prompt="please rename this", **sized
            )
            is False
        )
        assert (
            chat_runner._jev_downgrade_refused(
                state, client, "chat-1", mr, prompt="x" * 600_000, **sized
            )
            is True
        )

    def test_a_token_dense_prompt_of_the_same_length_is_refused(self, windows):
        """Same character COUNT, different token count. The rule sizes tokens, so a CJK
        prompt that fills the small window is refused where Latin text of that length
        sits well inside it -- the estimate the rule is handed has to say so, or the
        shrink is cleared on ordinary input."""
        client = MagicMock()
        client.context_window_tokens = MagicMock(return_value=1_000_000)
        client.context_used_tokens = MagicMock(return_value=0)
        client.context_usage_unknown = MagicMock(return_value=False)
        state = MagicMock()
        state.sessions.effective_autocompact_pct = MagicMock(return_value=70.0)
        # ``model-a`` is 200k and the threshold is 70%, so the bar is 140k tokens.
        sized = dict(
            current_window=1_000_000,
            target="model-a",
            p=0.95,
            # A model to go back to, so these cases are decided by the rule under
            # test rather than by the rollback rule.
            rollback_target="model-b",
        )

        assert (
            chat_runner._jev_downgrade_refused(
                state, client, "chat-1", mr, prompt="x" * 150_000, **sized
            )
            is False
        ), "37.5k tokens of Latin text fits"
        assert (
            chat_runner._jev_downgrade_refused(
                state, client, "chat-1", mr, prompt="\u4ea4" * 150_000, **sized
            )
            is True
        ), "150k tokens of CJK does not"

    def test_the_hook_is_handed_the_prompt_the_turn_sends(self):
        """Two texts reach this hook and only one of them is what the turn sends. The
        person's words are what the tier is classified from; ``full_message`` carries
        the request prefix, the replayed history and the hook context. A call site
        handing the typed text to both reads as correct and undercounts every injected
        prefix, so the pair is asserted here rather than left to two variable names."""
        import inspect

        packed = "".join(inspect.getsource(chat_runner._run_chat).split())
        assert "state,slot,client,_jev_route_text,session_key,prompt=full_message" in packed


class TestTheWindowTheSwitchActuallyLandedOn:
    @pytest.mark.asyncio
    async def test_a_substituted_served_model_is_put_back(self, tmp_path, answered, windows):
        """The rules size the id that was ASKED for. A backend answering a tier-policy
        substitution leaves the turn on a different model, and its id can still read as
        the requested one -- only the meter, rebased to what serves, says otherwise. So
        the turn is put back on the window it had rather than streaming into one the
        rules never authorised.

        `model-b` passes the pre-switch rules (1M, same as the session) and serves 200k
        once switched, with 150k held: 75% of the small window, over this session's
        threshold."""
        served_windows = {"model-c": 1_000_000, "model-b": 200_000}
        answered("medium", 0.95)
        slot = _routed_slot("chat-served-substitution")
        state, client = _wired(tmp_path, serves="model-c", used=150_000, limit=70.0)
        client.context_window_tokens = MagicMock(
            side_effect=lambda: served_windows.get(client.served_model, 1_000_000)
        )
        await _turn(state, slot)

        assert _switched_to(client) == ["model-b", "model-c"], "the room goes back"
        assert client.served_model == "model-c"
        row = _refusals(tmp_path)[0]
        assert row["tier"] == "medium"
        assert "applied" not in row and "model_used" not in row

    @pytest.mark.asyncio
    async def test_a_served_window_that_holds_is_left_alone(self, tmp_path, answered, windows):
        """MUTATION -- the re-check. A hook that put every switch back passes the test
        above and fails this one: when the served window is the one that was asked for,
        the turn stays on it and the receipt is written."""
        answered("medium", 0.95)
        slot = _routed_slot("chat-served-holds")
        state, client = _wired(
            tmp_path, serves="model-c", used=150_000, limit=70.0, served_window=1_000_000
        )
        await _turn(state, slot)

        assert _switched_to(client) == ["model-b"]
        assert _refusals(tmp_path) == []


class TestAnUnmeasurableReadingKeepsTheWindow:
    @pytest.mark.asyncio
    async def test_a_downgrade_is_refused_while_the_meter_is_unknown(
        self, tmp_path, answered, windows
    ):
        """A resumed session's history is real and unread, so a confident tier is still
        refused and the session keeps its window rather than shrinking on a reading
        nobody has."""
        answered("simple", 0.95)
        slot = _routed_slot("chat-usage-unknown")
        state, client = _wired(tmp_path, serves="model-c", used=0, usage_unknown=True)
        await _turn(state, slot)

        assert _switched_to(client) == []
        assert _refusals(tmp_path)[0]["p"] == 0.95


class TestARefusalNothingActedOn:
    @pytest.mark.asyncio
    async def test_a_pick_landing_during_the_await_leaves_no_refusal_row(
        self, tmp_path, windows, monkeypatch
    ):
        """The locked re-pick guard drops the whole answer, and the rows are durable
        and never rewritten -- so a refusal recorded before that guard describes a
        decision nothing acted on and nobody can clear. Driven through the oracle,
        which is the only place inside the await window."""
        import kiro_crew.decisions.impl_jev as impl_mod

        slot = _routed_slot("chat-repick-refused")
        state, client = _wired(tmp_path, serves="model-c")

        class _PickingOracle:
            async def ask(self, _state, questions):
                slot.served_model = "model-a"
                return {q.id: Answer(id=q.id, value="simple", p=0.41) for q in questions}

        monkeypatch.setattr(impl_mod, "JevOracle", lambda provider: _PickingOracle())
        await _turn(state, slot)

        assert _switched_to(client) == []
        assert _refusals(tmp_path) == []

    @pytest.mark.asyncio
    async def test_an_empty_landing_writes_no_row_when_a_pick_landed(
        self, tmp_path, answered, windows, monkeypatch
    ):
        """The exit with NOWHERE to land returns before the locks, so the in-lock guard
        never runs for it. Without its own reading of the premise that exit persists a
        routed answer a pick has already superseded, and the row is durable."""
        monkeypatch.setattr(
            gate_mod,
            "_snapshot",
            lambda: SimpleNamespace(
                decisions=DecisionsConfig(
                    model_route={"simple": "model-a", "medium": "", "complex": "model-c"}
                )
            ),
        )
        import kiro_crew.decisions.impl_jev as impl_mod

        slot = _routed_slot("chat-empty-landing-repick")
        state, client = _wired(tmp_path, serves="model-c")

        class _PickingOracle:
            async def ask(self, _state, questions):
                slot.served_model = "model-b"
                return {q.id: Answer(id=q.id, value="simple", p=0.41) for q in questions}

        monkeypatch.setattr(impl_mod, "JevOracle", lambda provider: _PickingOracle())
        await _turn(state, slot)

        assert _switched_to(client) == []
        assert _refusals(tmp_path) == []


class TestARefusalUnderTheFallbackLadder:
    @pytest.mark.asyncio
    async def test_a_refused_pin_never_attempts_the_failed_primary(
        self, tmp_path, answered, windows, monkeypatch
    ):
        """While the ladder holds the session the baseline NAMES the primary it walked
        away from, and the restore target is zeroed for exactly that reason. The
        landing reads that zeroed value, so a refused pin with no usable medium keeps
        the substitute rather than asking for the model that just failed.

        The primary is still refused here, which is what keeps the fallback active
        across the turn: the probe's own attempt is the ONE call that belongs to the
        primary, and a second would be the routing hook's."""
        monkeypatch.setattr(
            gate_mod,
            "_snapshot",
            lambda: SimpleNamespace(
                decisions=DecisionsConfig(
                    model_route={"simple": "model-a", "medium": "", "complex": "model-c"}
                )
            ),
        )
        answered("simple", 0.41)
        slot = _routed_slot("chat-fallback-refused")
        slot._active_fallback_model = "model-c"
        slot._fallback_primary_model = "model-b"
        state, client = _wired(tmp_path, serves="model-c")

        async def _refuse_the_primary(name: str) -> None:
            if name == "model-b":
                raise RuntimeError("the primary is still throttled")
            client.served_model = name

        client.set_model = AsyncMock(side_effect=_refuse_the_primary)
        await _turn(state, slot)

        assert _switched_to(client) == ["model-b"], "the routing hook must attempt nothing"
        assert client.served_model == "model-c", "the ladder keeps its substitute"
        assert len(_refusals(tmp_path)) == 1


class TestTheWindowTheSessionIsServedOn:
    @pytest.mark.asyncio
    async def test_a_registry_entry_that_lags_the_served_window_still_refuses(
        self, tmp_path, answered, windows
    ):
        """The meter's percentage is denominated in the SERVED window. With the
        registry naming 200k for the current model and the provider reporting 1M, the
        registry alone reads the move to a 200k target as no shrink at all -- and the
        150k transcript would then be compacted at the end of that turn."""
        answered("simple", 0.95)
        windows["model-c"] = 200_000
        slot = _routed_slot("chat-served-window")
        state, client = _wired(
            tmp_path, serves="model-c", used=150_000, limit=70.0, served_window=1_000_000
        )
        await _turn(state, slot)

        assert _switched_to(client) == []
        assert _refusals(tmp_path)[0]["p"] == 0.95
