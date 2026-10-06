"""The channel turn pipeline, tested through its own interface.

:class:`~kiro_crew.messaging.dispatch.ChannelTurns` owns the sequence every
channel dispatcher runs around the ``TurnDriver``: the governance backstop, the
mute substitution, the hook short-circuit, the claim, the crew-log opener, the
identity publish, the build, the driver, the compaction replay, the accounting,
the ledger, the refusal ladder and the finalize order. A bug in any of them is a
bug in every channel at once, so each is pinned here by what a caller can
observe: what the session manager was asked, in order (one ledger), what the
provider was prompted with, what the renderer showed, what the ledger adapter was
handed, and the :class:`~kiro_crew.messaging.dispatch.TurnOutcome`.

The real ``TurnDriver`` runs behind every case, and the collaborators the
pipeline is HANDED are the fakes in ``channel_turn_fakes``. A few process-wide
collaborators the pipeline reaches as ``messaging.dispatch`` globals are replaced
too, each because the real one would leave the test or make it unobservable:

* ``channel_inbound_permitted`` (autouse): the governance backstop reads the
  operator's policy files; the fake records each ask, and the backstop tests
  flip it.
* ``publish_turn_identity`` (autouse): the real publisher writes the per-turn
  identity file the managed MCP servers read; the fake writes the call into the
  session ledger, which is how the identity-before-build order is observed.
* ``spool_refused_turn`` (autouse): the real spool persists refused messages
  under the data home for a restart; the fake records each spool.
* per test, ``session_store_for_turn`` (to refuse member memory),
  ``charge_turn_failure`` (to see which provider is charged),
  ``open_turn_crew_log`` (the real emitter is ``test_discord``'s e2e subject) and
  ``build_directive_consumer`` (to see whether one is built); the ceiling tests
  install a one-turn ``turn_ceiling`` ceiling, which conftest resets.

Behaviours a dispatcher still diverges on are parametrized over ``drift``: the
default and Discord's declared
:data:`~kiro_crew.messaging.dispatch.DISCORD_DRIFT`; ``TestTheDiscordLeg`` runs
Discord's cases on its real renderer.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest
from channel_turn_fakes import (
    FakeDiscordClient,
    FakeSessions,
    Recorder,
    RecordingCtxBuilder,
    RecordingDispatcher,
    RecordingRenderer,
    ScriptedProvider,
    answer,
    compaction_status,
    complete,
    directive,
    permission,
    steer_consumed,
    text,
    thinking,
    tool_call,
)

from kiro_crew.acp.types import (
    STOP_REASON_CANCELLED,
    STOP_REASON_COMPACTION_FAILED,
    STOP_REASON_STALE_RECOVER,
)
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE
from kiro_crew.discord.renderer import DiscordRenderer
from kiro_crew.discord.transport import DISCORD_CAPABILITIES
from kiro_crew.hooks import TOOL_DENY
from kiro_crew.memory_stores import UnknownMemoryStore
from kiro_crew.messaging import dispatch as D
from kiro_crew.messaging import turn_ceiling
from kiro_crew.messaging.dispatch import (
    DISCORD_DRIFT,
    TOOLLESS_TURN_AGENT,
    TOOLLESS_TURN_REFUSAL_NOTE,
    Approvals,
    Asker,
    Audience,
    ChannelTurn,
    ChannelTurns,
    Drift,
    MonitorWake,
    TurnRecord,
    Verdict,
    drive_turn,
    exchange_writer,
)
from kiro_crew.messaging.inbound_spool import InboundRoute
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.messaging.turn_ceiling import ConversationTurnCeiling
from kiro_crew.monitoring.models import MonitorDispatchResult
from kiro_crew.session_allocation import SessionClosingError
from kiro_crew.start_priority import StartPriority

KEY = "weixin:agentA:direct:userA"
CONV = "weixin:userA"
ROUTE = InboundRoute(conversation_id="ROOM", text="what the user typed", user_id="userA")

DRIFTS = [
    pytest.param(frozenset(), id="default"),
    pytest.param(DISCORD_DRIFT, id="discord-drift"),
]


# ── fixtures and builders ────────────────────────────────────────────────────


class _Governance:
    """What the channels policy answers, and every channel it was asked about."""

    def __init__(self) -> None:
        self.permitted = True
        self.asked: list[str] = []

    async def __call__(self, channel_type: str) -> bool:
        self.asked.append(channel_type)
        return self.permitted


# The three autouse stubs patch through the isolation floor's own MonkeyPatch
# (testing-conventions D11), so a test's own ``monkeypatch.undo()`` never strips them.
@pytest.fixture(autouse=True)
def governance(_floor_monkeypatch: pytest.MonkeyPatch) -> _Governance:
    gate = _Governance()
    _floor_monkeypatch.setattr(D, "channel_inbound_permitted", gate)
    return gate


@pytest.fixture(autouse=True)
def published(_floor_monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Identity publication, recorded into the session ledger it ran against."""
    keys: list[str] = []

    async def _publish(sessions: Any, session_key: str) -> None:
        keys.append(session_key)
        ledger = getattr(sessions, "ledger", None)
        if ledger is not None:
            ledger.append(("publish_turn_identity", session_key))

    _floor_monkeypatch.setattr(D, "publish_turn_identity", _publish)
    return keys


@pytest.fixture(autouse=True)
def spooled(_floor_monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """Every durable refusal the pipeline spooled, as ``(channel_type, route)``."""
    calls: list[tuple[str, Any]] = []

    async def _spool(*, channel_type: str, route: Any) -> bool:
        calls.append((channel_type, route))
        return True

    _floor_monkeypatch.setattr(D, "spool_refused_turn", _spool)
    return calls


def _turns(
    sessions: FakeSessions,
    *,
    ctx: RecordingCtxBuilder | None = None,
    recorder: Recorder | None = None,
    drift: frozenset[Drift] = frozenset(),
    approval_mode: str = "auto",
    approvals: Approvals | None = None,
    restricted: Any = None,
    conv_log: Any = None,
    dispatcher: Any = None,
    agent: str = "agentA",
    channel: str = "weixin",
) -> ChannelTurns:
    recorder = recorder if recorder is not None else Recorder()
    return ChannelTurns(
        channel,
        sessions=sessions,
        ctx_builder=ctx if ctx is not None else RecordingCtxBuilder(ledger=sessions.ledger),
        dispatcher=dispatcher if dispatcher is not None else RecordingDispatcher(approval_mode),
        agent=lambda: agent,
        record=recorder.record,
        notice=recorder.notice,
        surface=recorder.surface,
        restricted=restricted,
        conv_log=conv_log,
        approvals=approvals if approvals is not None else Approvals(grant=None),
        drift=drift,
    )


def _asker(session_key: str = KEY, **kw: Any) -> Asker:
    kw.setdefault("route", ROUTE)
    kw.setdefault("start_priority", StartPriority.FOREGROUND)
    kw.setdefault("audit_caller", "weixin:userA")
    kw.setdefault("reply_to", "ROOM")
    return Asker(session_key, CONV, **kw)


def _answer(turns: ChannelTurns, *, asker: Asker | None = None, message: str = "hi", **kw: Any):
    renderer = kw.pop("renderer", None)
    return asyncio.run(
        turns.answer(
            asker if asker is not None else _asker(),
            message,
            renderer if renderer is not None else RecordingRenderer(),
            **kw,
        )
    )


def _shown(renderer: RecordingRenderer) -> str:
    """What the conversation read: the stream redactor may split one reply in two."""
    return "".join(renderer.shown)


def _discord_renderer(client: FakeDiscordClient) -> DiscordRenderer:
    return DiscordRenderer(client, "c1", DISCORD_CAPABILITIES, session_key=KEY)


# ── the happy path and the finalize order ────────────────────────────────────


class TestAnAnsweredTurn:
    @pytest.mark.parametrize("drift", DRIFTS)
    def test_the_turn_runs_once_and_finalizes_in_order(self, drift) -> None:
        provider = ScriptedProvider(answer("the reply"))
        sessions = FakeSessions(provider)
        recorder = Recorder()
        renderer = RecordingRenderer()

        outcome = _answer(_turns(sessions, recorder=recorder, drift=drift), renderer=renderer)

        assert outcome.verdict is Verdict.ANSWERED
        assert outcome.reply_text == "the reply" and outcome.landed is True
        assert outcome.stop_reason == "end_turn" and outcome.monitor is None
        assert provider.prompts == ["hi"]
        assert _shown(renderer) == "the reply" and renderer.closed == 1
        # One claim, one gate pass, one success, one release -- in that order, with
        # the identity published before the build that the driver then runs.
        names = sessions.names()
        assert names.count("get_or_create") == 1 and names.count("release") == 1
        assert names.index("get_or_create") < names.index("publish_turn_identity")
        assert names.index("publish_turn_identity") < names.index("build_message")
        assert names.index("build_message") < names.index("begin_turn")
        assert names.index("begin_turn") < names.index("record_success")
        assert names.index("record_success") < names.index("release")
        assert sessions.held == 0
        assert [r.kind for r in recorder.records] == ["answered"]
        assert recorder.notices == [("ROOM", KEY, provider)]

    @pytest.mark.parametrize("drift", DRIFTS)
    def test_the_typing_indicator_starts_before_the_cold_start(self, drift) -> None:
        """``get_or_create`` can spend seconds spawning a runtime; the indicator's
        refresh task must exist before that, or the user sees dead air."""
        sessions = FakeSessions()
        renderer = RecordingRenderer(ledger=sessions.ledger)

        _answer(_turns(sessions, drift=drift), renderer=renderer)

        names = sessions.names()
        assert names.index("on_turn_start") < names.index("get_or_create")

    def test_the_claim_carries_the_askers_identity(self) -> None:
        sessions = FakeSessions()
        _answer(
            _turns(sessions),
            asker=_asker(start_priority=StartPriority.FOREGROUND, model="model-x"),
        )
        _name, key, kwargs = sessions.calls("get_or_create")[0]
        assert key == KEY
        assert kwargs["agent"] == "agentA" and kwargs["channel_id"] == CONV
        assert kwargs["start_priority"] is StartPriority.FOREGROUND
        assert kwargs["model"] == "model-x"
        assert kwargs["wait_if_busy"] is True

    def test_a_model_is_named_only_when_the_asker_picked_one(self) -> None:
        sessions = FakeSessions()
        _answer(_turns(sessions))
        assert "model" not in sessions.calls("get_or_create")[0][2]

    @pytest.mark.parametrize("drift", DRIFTS)
    def test_release_still_runs_when_renderer_close_fails(self, drift) -> None:
        """The semaphore is keyed by SESSION, so leaking it does not merely lose this
        turn -- every later message for that conversation blocks forever."""
        sessions = FakeSessions()
        renderer = RecordingRenderer(close_raises=True)

        outcome = _answer(_turns(sessions, drift=drift), renderer=renderer)

        assert renderer.closed == 1, "close should still be attempted"
        assert sessions.count("release") == 1
        assert outcome.verdict is Verdict.ANSWERED, "the failed close does not escape"

    @pytest.mark.parametrize("drift", DRIFTS)
    def test_nothing_is_released_when_nothing_was_acquired(self, drift) -> None:
        sessions = FakeSessions(acquire_raises=RuntimeError("cold start failed"))
        renderer = RecordingRenderer(close_raises=True)

        outcome = _answer(_turns(sessions, drift=drift), renderer=renderer)

        assert outcome.verdict is Verdict.FAILED
        assert renderer.closed == 1, "finalization still runs on the failure path"
        assert sessions.count("release") == 0
        assert sessions.count("record_failure") == 0, "an unacquired failure is not charged"

    @pytest.mark.parametrize("drift", DRIFTS)
    def test_a_raised_turn_is_charged_once_and_recorded_with_its_partial_reply(self, drift) -> None:
        boom = RuntimeError("provider fell over")
        provider = ScriptedProvider([text("half an answer "), boom])
        sessions = FakeSessions(provider)
        recorder = Recorder()

        outcome = _answer(_turns(sessions, recorder=recorder, drift=drift))

        assert outcome.verdict is Verdict.FAILED
        assert sessions.count("record_failure") == 1 and sessions.count("record_success") == 0
        assert sessions.count("release") == 1
        (record,) = recorder.records
        assert record.kind == "failed" and record.error is boom
        assert record.reply_text.startswith("half an answer")
        assert record.user_text == "hi" and record.is_new is False

    def test_the_answered_record_is_what_the_ledger_needs(self) -> None:
        """The prompt as prepared, the reply exactly as accumulated (whitespace is the
        adapter's to normalize), and whether THIS message opened the conversation."""
        provider = ScriptedProvider([text("  "), complete()])
        sessions = FakeSessions(provider, is_new=True)
        recorder = Recorder()

        async def _prepare(_provider: Any, prompt: str) -> str:
            return prompt + " [attached]"

        _answer(_turns(sessions, recorder=recorder), prepare=_prepare)

        (record,) = recorder.records
        assert record == TurnRecord(
            "answered",
            KEY,
            "agentA",
            "hi [attached]",
            "  ",
            True,
            notice=record.notice,
        )
        assert record.notice, "a turn with no assistant text carries the driver's verdict"
        assert recorder.surfaced == 1, "a new conversation is surfaced after its record"


# ── the governance backstop ──────────────────────────────────────────────────


class TestTheGovernanceBackstop:
    def test_a_denied_turn_neither_renders_nor_acquires(self, governance) -> None:
        governance.permitted = False
        sessions = FakeSessions()
        renderer = RecordingRenderer()

        outcome = _answer(_turns(sessions), renderer=renderer)

        assert outcome.verdict is Verdict.DENIED
        assert governance.asked == ["weixin"]
        assert sessions.ledger == []
        assert renderer.closed == 0 and renderer.started == 0

    def test_a_dispatcher_that_gates_at_its_own_entry_is_not_asked_twice(self, governance) -> None:
        governance.permitted = False
        sessions = FakeSessions()

        outcome = _answer(_turns(sessions, drift=frozenset({Drift.NO_GOVERNANCE_BACKSTOP})))

        assert governance.asked == [], "the backstop is the dispatcher's own entry gate"
        assert outcome.verdict is Verdict.ANSWERED


# ── the mute substitution ────────────────────────────────────────────────────


class TestADisconnectedConversation:
    @pytest.mark.parametrize("drift", DRIFTS)
    def test_is_silenced_while_the_turn_still_runs(self, drift) -> None:
        provider = ScriptedProvider(answer("the reply"))
        sessions = FakeSessions(provider)
        sessions.muted.add((KEY, True))
        renderer = RecordingRenderer()
        recorder = Recorder()

        outcome = _answer(_turns(sessions, recorder=recorder, drift=drift), renderer=renderer)

        assert provider.prompts == ["hi"], "the inbound message still lands in the session"
        assert renderer.started == 0, "no typing indicator in a disconnected conversation"
        assert renderer.shown == [] and renderer.events == []
        assert renderer.closed == 0, "the real renderer opened nothing, so nothing is closed"
        assert outcome.verdict is Verdict.ANSWERED and sessions.count("release") == 1
        assert [r.kind for r in recorder.records] == ["answered"]

    def test_a_connected_conversation_keeps_its_real_renderer(self) -> None:
        sessions = FakeSessions(ScriptedProvider(answer("the reply")))
        renderer = RecordingRenderer()

        _answer(_turns(sessions), renderer=renderer)

        assert renderer.started >= 1 and _shown(renderer) == "the reply" and renderer.closed == 1

    def test_the_pause_is_read_for_the_role_the_turn_arrived_on(self) -> None:
        """A channel-born key IS its conversation (the origin flag); any other key
        reached this pipeline over a mirror binding (the mirror flag)."""
        born = FakeSessions()
        born.muted.add((KEY, False))  # the MIRROR flag is set, the origin one is not
        renderer = RecordingRenderer()
        _answer(_turns(born), renderer=renderer)
        assert renderer.shown == ["ok"], "a born-in turn reads the origin flag"

        mirrored = FakeSessions()
        mirrored.muted.add(("dashboard:chat-1", False))
        renderer = RecordingRenderer()
        _answer(_turns(mirrored), asker=_asker("dashboard:chat-1"), renderer=renderer)
        assert renderer.shown == [], "a mirrored turn reads the mirror flag"

    @pytest.mark.parametrize("muted", [True, False])
    def test_a_renderer_factory_is_told_the_mute_once_and_built_once(self, muted) -> None:
        sessions = FakeSessions(ScriptedProvider(answer("the reply")))
        if muted:
            sessions.muted.add((KEY, True))
        built: list[bool] = []
        renderer = RecordingRenderer()

        async def _factory(is_muted: bool) -> RecordingRenderer:
            built.append(is_muted)
            return renderer

        _answer(_turns(sessions), renderer=_factory)

        assert built == [muted]
        assert _shown(renderer) == ("" if muted else "the reply")

    def test_a_factory_failure_before_any_claim_propagates_and_holds_nothing(self) -> None:
        sessions = FakeSessions()

        async def _broken(_muted: bool) -> RecordingRenderer:
            raise RuntimeError("renderer setup failed")

        with pytest.raises(RuntimeError, match="renderer setup failed"):
            _answer(_turns(sessions), renderer=_broken)
        assert sessions.ledger == []


# ── the hook short-circuit ───────────────────────────────────────────────────


class TestAHookReply:
    def test_answers_without_a_session_and_records_the_redacted_reply(self) -> None:
        sessions = FakeSessions()
        ctx = RecordingCtxBuilder(hook_reply="pong", ledger=sessions.ledger)
        recorder = Recorder()
        renderer = RecordingRenderer()

        outcome = _answer(_turns(sessions, ctx=ctx, recorder=recorder), renderer=renderer)

        assert outcome.verdict is Verdict.HOOK_REPLIED
        assert renderer.shown == ["pong"] and renderer.closed == 1
        assert sessions.count("get_or_create") == 0 and sessions.count("release") == 0
        assert [(r.kind, r.reply_text, r.is_new) for r in recorder.records] == [
            ("hook_reply", "pong", False)
        ]

    def test_its_record_waits_for_an_open_replay_gap(self) -> None:
        """The reply goes out at once, but the record is written only once an older
        message on the key has settled its replay: the transcript must show that turn
        first, as the reader saw it."""

        class _GappedSessions(FakeSessions):
            def __init__(self) -> None:
                super().__init__()
                self.arrived = asyncio.Event()
                self.release_gap = asyncio.Event()

            async def await_replay_gap(self, key: str) -> None:
                self.ledger.append(("await_replay_gap", key))
                self.arrived.set()
                await self.release_gap.wait()

        async def scenario() -> None:
            sessions = _GappedSessions()
            recorder = Recorder()
            renderer = RecordingRenderer()
            turns = _turns(
                sessions,
                ctx=RecordingCtxBuilder(hook_reply="pong", ledger=sessions.ledger),
                recorder=recorder,
            )
            task = asyncio.create_task(turns.answer(_asker(), "hi", renderer))
            await asyncio.wait_for(sessions.arrived.wait(), timeout=30)
            assert renderer.shown == ["pong"] and "on_done" in renderer.events
            assert recorder.records == [], "the record waits for the replay to settle"
            sessions.release_gap.set()
            outcome = await asyncio.wait_for(task, timeout=30)
            assert outcome.verdict is Verdict.HOOK_REPLIED
            assert [r.reply_text for r in recorder.records] == ["pong"]

        asyncio.run(scenario())

    def test_a_dispatcher_that_never_asks_the_hooks_runs_the_model(self) -> None:
        provider = ScriptedProvider(answer("the model"))
        sessions = FakeSessions(provider)
        ctx = RecordingCtxBuilder(hook_reply="pong", ledger=sessions.ledger)
        renderer = RecordingRenderer()

        outcome = _answer(_turns(sessions, ctx=ctx, drift=DISCORD_DRIFT), renderer=renderer)

        assert outcome.verdict is Verdict.ANSWERED
        assert provider.prompts == ["hi"] and _shown(renderer) == "the model"


# ── the turn ceiling ─────────────────────────────────────────────────────────


@pytest.fixture
def one_turn_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        turn_ceiling, "_SHARED", ConversationTurnCeiling(max_turns=1, window_secs=3600.0)
    )


class TestTheTurnCeiling:
    @pytest.mark.parametrize("drift", DRIFTS)
    @pytest.mark.parametrize("muted", [False, True])
    def test_a_refused_turn_is_shown_not_spooled_not_charged(
        self, one_turn_ceiling, spooled, drift, muted
    ) -> None:
        provider = ScriptedProvider(answer("first"), answer("second"))
        sessions = FakeSessions(provider)
        if muted:
            sessions.muted.add((KEY, True))
        turns = _turns(sessions, drift=drift)
        _answer(turns)
        renderer = RecordingRenderer()

        outcome = _answer(turns, renderer=renderer)

        assert outcome.verdict is Verdict.CEILING
        assert provider.prompts == ["hi"], "the refused turn never opened a prompt"
        # Through the mute substitute when the conversation is disconnected: the
        # latch does not clear on its own, so a concrete renderer would post into it
        # once per inbound message forever.
        assert (renderer.shown == []) is muted
        if not muted:
            assert renderer.shown == [turn_ceiling.REFUSAL_TEXT]
        assert spooled == []
        assert sessions.count("record_failure") == 0
        assert sessions.count("release") == 2


# ── the shutdown race ────────────────────────────────────────────────────────


class TestAShutdownBetweenTheClaimAndTheDispatch:
    @pytest.mark.parametrize("drift", DRIFTS)
    def test_never_opens_the_turn_and_spools_the_route(self, spooled, drift) -> None:
        provider = ScriptedProvider()
        sessions = FakeSessions(provider, closing=True)
        renderer = RecordingRenderer()

        outcome = _answer(_turns(sessions, drift=drift), renderer=renderer)

        assert outcome.verdict is Verdict.SHUTTING_DOWN
        assert provider.prompts == [], "the turn must not open behind close_all's snapshot"
        assert sessions.count("begin_turn") == 1
        assert renderer.closed == 1 and sessions.count("release") == 1
        assert sessions.count("record_failure") == 0 and sessions.count("record_success") == 0
        # ``route.text`` and only that: the prompt may carry private context.
        assert spooled == [("weixin", ROUTE)]

    def test_a_restricted_session_never_spools(self, spooled) -> None:
        sessions = FakeSessions(closing=True)
        asked: list[str] = []

        async def _restricted(session_key: str) -> bool:
            asked.append(session_key)
            return True

        _answer(_turns(sessions, restricted=_restricted))

        assert asked == [KEY] and spooled == []
        assert sessions.count("release") == 1


# ── the refusal ladder ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    # Neutral roots of each shape the redactor strips (a POSIX root, another root, a
    # drive letter); a fixture never names a home directory, even a made-up one.
    ["/srv/alice/memory.db", "/mnt/alice/memory.db", r"D:\data\alice\memory.db"],
)
@pytest.mark.parametrize("drift", DRIFTS)
def test_a_memory_refusal_hides_paths_and_credentials(monkeypatch, path, drift) -> None:
    secret = "ghp_" + "x" * 36

    async def _refuse(_ctx: Any, _key: str) -> str:
        raise UnknownMemoryStore(
            f"Member memory unavailable: cannot read {path}; token={secret}. "
            "Repair this member's memory. Global Memory V1 was not used."
        )

    monkeypatch.setattr(D, "session_store_for_turn", _refuse)
    sessions = FakeSessions()
    renderer = RecordingRenderer()

    outcome = _answer(_turns(sessions, drift=drift), renderer=renderer)

    assert outcome.verdict is Verdict.MEMORY_REFUSED
    (visible,) = renderer.shown
    assert "Repair this member's memory" in visible
    assert path not in visible and "alice" not in visible and secret not in visible
    assert len(visible) <= 1000 and "on_done" in renderer.events
    assert sessions.count("get_or_create") == 0 and sessions.count("release") == 0


class TestAToolLessGuestTurn:
    """A guest talks to a tool-less agent, never to the operator's.

    On the kiro backend a tool named in the agent spec's ``allowedTools`` runs
    without a permission request, so the driver's refusal never sees it: the turn
    is driven on :data:`TOOLLESS_TURN_AGENT`, whose spec mounts nothing, and it is
    refused outright when that cannot hold.
    """

    _KEY = f"weixin:{TOOLLESS_TURN_AGENT}:direct:peerB"

    def _guest(self, **kw: Any) -> Asker:
        return _asker(self._KEY, audience=Audience.GUEST, **kw)

    def test_the_session_is_acquired_tool_less_in_its_own_directory(
        self, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setenv("KIROCREW_WORKSPACE", str(tmp_path / "ws"))
        ctx = RecordingCtxBuilder()
        sessions = FakeSessions()

        outcome = _answer(_turns(sessions, ctx=ctx), asker=self._guest())

        assert outcome.verdict is Verdict.ANSWERED
        _name, key, kwargs = sessions.calls("get_or_create")[0]
        assert key == self._KEY and kwargs["agent"] == TOOLLESS_TURN_AGENT
        # The name resolves to the TEMPLATE, never a namesake crew, and the process is
        # a cold start in the session's own directory, never the project cwd.
        assert kwargs["crew_agent"] == ""
        cwd = Path(kwargs["cwd"])
        assert cwd.is_dir() and cwd.is_relative_to(tmp_path.resolve())
        assert "weixin_" in cwd.name and TOOLLESS_TURN_AGENT in cwd.name and "peerB" in cwd.name
        # A guest's prompt is built without the operator's private context.
        assert ctx.builds[0]["minimal_context"] is True
        # The directory is the session's own: another guest gets another one.
        other = FakeSessions()
        _answer(
            _turns(other),
            asker=_asker(self._KEY.replace("peerB", "peerC"), audience=Audience.GUEST),
        )
        assert Path(other.calls("get_or_create")[0][2]["cwd"]) != cwd

    def test_a_guest_never_gets_a_decider_or_the_grant(self) -> None:
        """INTERACTIVE with no decider and no grant: a permission that does reach the
        driver is denied, whatever the dispatcher's own approval mode."""
        provider = ScriptedProvider([permission("r1"), complete()])
        sessions = FakeSessions(provider)
        asked: list[Any] = []

        async def _decider(event: Any) -> bool:
            asked.append(event)
            return True

        _answer(
            _turns(sessions, approval_mode="auto", approvals=Approvals(grant=lambda *_: True)),
            asker=self._guest(),
            decider=_decider,
        )

        assert provider.rejected == ["r1"] and provider.approved == []
        assert asked == []

    def test_a_session_bound_to_a_tooled_agent_refuses_the_turn(self) -> None:
        provider = ScriptedProvider()
        sessions = FakeSessions(provider, bound_agent="agentA")
        renderer = RecordingRenderer()

        outcome = _answer(_turns(sessions), asker=self._guest(), renderer=renderer)

        assert outcome.verdict is Verdict.TOOLLESS_REFUSED
        assert provider.prompts == []
        assert renderer.shown == [TOOLLESS_TURN_REFUSAL_NOTE] and renderer.closed == 1
        assert sessions.count("record_failure") == 0 and sessions.count("release") == 1

    @pytest.mark.parametrize("backend", [ACP_BACKEND_CLAUDE, None])
    def test_a_backend_that_reads_no_agent_spec_refuses_the_turn(self, backend) -> None:
        provider = ScriptedProvider(backend=backend)
        sessions = FakeSessions(provider)
        renderer = RecordingRenderer()

        outcome = _answer(_turns(sessions), asker=self._guest(), renderer=renderer)

        assert outcome.verdict is Verdict.TOOLLESS_REFUSED and provider.prompts == []
        assert renderer.shown == [TOOLLESS_TURN_REFUSAL_NOTE]
        assert sessions.count("record_failure") == 0

    def test_an_unaddressed_refusal_posts_nothing(self) -> None:
        sessions = FakeSessions(ScriptedProvider(backend=ACP_BACKEND_CLAUDE))
        renderer = RecordingRenderer()

        outcome = _answer(_turns(sessions), asker=self._guest(addressed=False), renderer=renderer)

        assert outcome.verdict is Verdict.TOOLLESS_REFUSED
        assert renderer.shown == [] and renderer.closed == 1

    def test_an_owner_turn_keeps_its_agent_and_is_not_backend_gated(self) -> None:
        sessions = FakeSessions(ScriptedProvider(answer(), backend=ACP_BACKEND_CLAUDE))

        outcome = _answer(_turns(sessions))

        assert outcome.verdict is Verdict.ANSWERED
        kwargs = sessions.calls("get_or_create")[0][2]
        assert kwargs["agent"] == "agentA"
        assert "crew_agent" not in kwargs and "cwd" not in kwargs


# ── the audience and the approvals ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("audience", "minimal"),
    [(Audience.OWNER, False), (Audience.SHARED, True), (Audience.GUEST, True)],
)
def test_the_audience_decides_what_context_the_prompt_carries(audience, minimal, tmp_path) -> None:
    ctx = RecordingCtxBuilder()
    key = f"weixin:{TOOLLESS_TURN_AGENT}:direct:x" if audience is Audience.GUEST else KEY
    _answer(_turns(FakeSessions(), ctx=ctx), asker=_asker(key, audience=audience))
    assert ctx.builds[0]["minimal_context"] is minimal


class TestThePermissionLadder:
    def test_the_grant_approves_a_tool_no_decider_can_answer(self) -> None:
        provider = ScriptedProvider([permission("r1"), complete()])
        granted: list[tuple[str, Any]] = []

        def _grant(session_key: str, decider: Any) -> bool:
            granted.append((session_key, decider))
            return True

        _answer(
            _turns(
                FakeSessions(provider),
                approval_mode="interactive",
                approvals=Approvals(grant=_grant),
            )
        )

        assert provider.approved == ["r1"]
        assert granted == [(KEY, None)], "the grant is read per request, for this session"

    def test_without_a_grant_interactive_denies_by_default(self) -> None:
        provider = ScriptedProvider([permission("r1"), complete()])
        _answer(_turns(FakeSessions(provider), approval_mode="interactive"))
        assert provider.rejected == ["r1"] and provider.approved == []

    def test_the_dispatchers_approval_mode_is_read_per_turn(self) -> None:
        provider = ScriptedProvider([permission("r1"), complete()], [permission("r2"), complete()])
        dispatcher = RecordingDispatcher("interactive")
        turns = _turns(FakeSessions(provider), dispatcher=dispatcher)
        _answer(turns)
        dispatcher.approval_mode = "auto"
        _answer(turns)
        assert provider.rejected == ["r1"] and provider.approved == ["r2"]

    def test_the_decider_answers_an_addressed_owners_prompt(self) -> None:
        provider = ScriptedProvider([permission("r1"), complete()])

        async def _decider(_event: Any) -> bool:
            return True

        _answer(
            _turns(FakeSessions(provider), approval_mode="interactive"),
            decider=_decider,
        )
        assert provider.approved == ["r1"]

    def test_an_unaddressed_turn_has_no_one_to_ask(self) -> None:
        provider = ScriptedProvider([permission("r1"), complete()])

        async def _decider(_event: Any) -> bool:
            return True

        _answer(
            _turns(FakeSessions(provider), approval_mode="interactive"),
            asker=_asker(addressed=False),
            decider=_decider,
        )
        assert provider.rejected == ["r1"]

    @pytest.mark.parametrize("drift", DRIFTS)
    def test_the_window_sweep_runs_first_in_the_finally(self, drift) -> None:
        sessions = FakeSessions()
        renderer = RecordingRenderer()
        approvals = Approvals(
            grant=None, discard=lambda key: sessions.ledger.append(("discard", key))
        )

        _answer(_turns(sessions, approvals=approvals, drift=drift), renderer=renderer)

        names = sessions.names()
        assert names.index("discard") < names.index("release")
        assert names.index("record_success") < names.index("discard")

    @pytest.mark.parametrize(
        ("drift", "steered"),
        [
            pytest.param(frozenset(), "rule 7 blocks it", id="default"),
            pytest.param(
                DISCORD_DRIFT, "blocked by the PreToolUse security gate", id="discord-drift"
            ),
        ],
    )
    def test_a_hook_deny_tells_the_model_which_rule_blocked_it(self, drift, steered) -> None:
        provider = ScriptedProvider([permission("r1"), complete()])
        sessions = FakeSessions(provider)
        ctx = RecordingCtxBuilder(tool_verdict=TOOL_DENY, deny_reason="rule 7 blocks it")

        _answer(_turns(sessions, ctx=ctx, drift=drift))

        assert provider.rejected == ["r1"]
        assert len(provider.steered) == 1 and steered in provider.steered[0]


# ── the post-compaction re-injection flag ────────────────────────────────────


class TestTheReinjectionFlag:
    @pytest.mark.parametrize("drift", DRIFTS)
    def test_a_compacted_session_forwards_the_flag_once(self, drift) -> None:
        sessions = FakeSessions(reinjection=True)
        ctx = RecordingCtxBuilder(ledger=sessions.ledger)

        _answer(_turns(sessions, ctx=ctx, drift=drift))

        assert ctx.builds[0]["needs_reinjection"] is True
        assert sessions.count("consume_reinjection") == 1
        assert sessions.count("mark_reinjection") == 0, "a landed turn keeps it consumed"
        assert ctx.settles == [("commit", KEY)]

    @pytest.mark.parametrize("drift", DRIFTS)
    @pytest.mark.parametrize(
        "script",
        [
            pytest.param([RuntimeError("provider fell over")], id="raised"),
            pytest.param([complete(STOP_REASON_CANCELLED)], id="cancelled"),
            pytest.param([complete(STOP_REASON_STALE_RECOVER)], id="stale-recover"),
            pytest.param([text("partial")], id="no-completion"),
        ],
    )
    def test_a_consuming_turn_that_never_landed_puts_it_back(self, drift, script) -> None:
        sessions = FakeSessions(ScriptedProvider(script), reinjection=True)
        ctx = RecordingCtxBuilder(ledger=sessions.ledger)

        _answer(_turns(sessions, ctx=ctx, drift=drift))

        assert ctx.builds[0]["needs_reinjection"] is True
        assert sessions.reinjection_armed is True
        # Re-armed BEFORE the skill bodies roll back, then the renderer closes and the
        # permit is released.
        names = sessions.names()
        assert names.index("mark_reinjection") < names.index("rollback_skill_bodies")
        assert names.index("rollback_skill_bodies") < names.index("release")

    def test_a_failed_turn_that_consumed_nothing_arms_nothing(self) -> None:
        sessions = FakeSessions(ScriptedProvider([RuntimeError("boom")]))
        _answer(_turns(sessions))
        assert sessions.count("mark_reinjection") == 0

    def test_a_session_stand_in_without_the_flag_reads_false(self) -> None:
        class _Bare(FakeSessions):
            consume_needs_reinjection = None  # type: ignore[assignment]

        sessions = _Bare()
        ctx = RecordingCtxBuilder()
        outcome = _answer(_turns(sessions, ctx=ctx))
        assert ctx.builds[0]["needs_reinjection"] is False
        assert outcome.verdict is Verdict.ANSWERED

    @pytest.mark.parametrize("drift", DRIFTS)
    def test_a_backend_compaction_arms_the_flag_for_the_next_turn(self, drift) -> None:
        provider = ScriptedProvider(
            [text("the reply"), compaction_status("completed"), complete()], answer()
        )
        sessions = FakeSessions(provider)
        ctx = RecordingCtxBuilder(ledger=sessions.ledger)
        turns = _turns(sessions, ctx=ctx, drift=drift)

        _answer(turns)
        _answer(turns)

        assert [build["needs_reinjection"] for build in ctx.builds] == [False, True]
        # Armed as the compacting turn settles, before its skill bodies commit.
        names = sessions.names()
        assert names.index("mark_reinjection") < names.index("commit_skill_bodies")
        assert sessions.reinjection_armed is False, "the next turn consumed it"

    def test_a_backend_compaction_arms_the_flag_when_the_turn_then_fails(self) -> None:
        sessions = FakeSessions(
            ScriptedProvider([compaction_status("completed"), RuntimeError("provider fell over")])
        )
        _answer(_turns(sessions))
        assert sessions.reinjection_armed is True

    def test_a_failed_backend_compaction_arms_nothing(self) -> None:
        sessions = FakeSessions(ScriptedProvider([compaction_status("failed"), *answer()]))
        _answer(_turns(sessions))
        assert sessions.count("mark_reinjection") == 0

    def test_a_compaction_reported_before_a_replay_is_not_counted_after_it(self) -> None:
        """The reset replaced the session that compacted; the replay's own session
        starts cold, so arming it would inject the context twice."""
        first = ScriptedProvider(
            [compaction_status("completed"), *_abandoned()], transient=True, name="first"
        )
        second = ScriptedProvider(answer("the reply"), name="second")
        sessions = FakeSessions(first, second, is_new=[False, True])

        outcome = _answer(_turns(sessions))

        assert outcome.verdict is Verdict.ANSWERED and sessions.count("reset") == 1
        assert sessions.count("mark_reinjection") == 0


# ── the origin and own-mirror bind ───────────────────────────────────────────


_ROOM = ChannelLink("weixin", channel_id="ROOM", thread_id=None)


class TestTheOriginBind:
    @pytest.mark.parametrize(
        ("drift", "on_loop"),
        [
            pytest.param(frozenset(), False, id="default-offloaded"),
            pytest.param(DISCORD_DRIFT, True, id="discord-drift-on-the-loop"),
        ],
    )
    def test_the_conversation_is_recorded_and_bound(self, drift, on_loop) -> None:
        threads: list[int] = []

        class _Threaded(FakeSessions):
            def set_mirror_link(self, key: str, link: Any) -> None:
                threads.append(threading.get_ident())
                super().set_mirror_link(key, link)

        sessions = _Threaded()
        _answer(_turns(sessions, drift=drift), asker=_asker(origin=_ROOM))

        assert sessions.origin_links[KEY] is _ROOM and sessions.mirror_links[KEY] is _ROOM
        # asyncio.run drives the loop on THIS thread.
        assert (threads == [threading.get_ident()]) is on_loop

    def test_a_unified_key_records_nothing(self) -> None:
        sessions = FakeSessions()
        _answer(_turns(sessions), asker=_asker("unified:agentA", origin=_ROOM))
        assert sessions.origin_links == {} and sessions.mirror_links == {}

    def test_a_turn_without_an_origin_binds_nothing(self) -> None:
        sessions = FakeSessions()
        _answer(_turns(sessions))
        assert sessions.origin_links == {} and sessions.mirror_links == {}

    def test_the_persisted_opt_out_is_honoured(self) -> None:
        sessions = FakeSessions()
        sessions.opted_out.add(KEY)
        _answer(_turns(sessions), asker=_asker(origin=_ROOM))
        assert sessions.mirror_links == {}

    def test_a_binding_aimed_elsewhere_is_not_repointed(self) -> None:
        elsewhere = ChannelLink("discord", channel_id="99", thread_id=None)
        sessions = FakeSessions()
        sessions.mirror_links[KEY] = elsewhere
        _answer(_turns(sessions), asker=_asker(origin=_ROOM))
        assert sessions.mirror_links[KEY] is elsewhere

    @pytest.mark.parametrize("drift", DRIFTS)
    def test_a_bind_failure_does_not_drop_the_turn(self, drift) -> None:
        class _Broken(FakeSessions):
            def set_origin_link(self, key: str, link: Any) -> None:
                raise RuntimeError("session map unavailable")

        sessions = _Broken()
        outcome = _answer(_turns(sessions, drift=drift), asker=_asker(origin=_ROOM))
        assert outcome.verdict is Verdict.ANSWERED
        assert sessions.count("record_success") == 1 and sessions.count("release") == 1

    def test_a_resumed_session_owes_no_new_session_bookkeeping(self) -> None:
        sessions = FakeSessions(is_new=True)
        recorder = Recorder()
        _answer(_turns(sessions, recorder=recorder), asker=_asker(resumed=True))
        assert sessions.count("set_channel") == 0
        assert recorder.records[0].is_new is False and recorder.surfaced == 0

    def test_a_new_conversation_is_attributed_once(self) -> None:
        sessions = FakeSessions(is_new=True)
        _answer(_turns(sessions))
        assert sessions.calls("set_channel") == [("set_channel", KEY, CONV)]


def test_a_resumed_session_runs_under_its_persisted_agent(tmp_path) -> None:
    class _Log:
        def get_metadata(self, session_key: str) -> dict[str, Any]:
            return {"agent": "the-dashboard-agent"}

    sessions = FakeSessions()
    ctx = RecordingCtxBuilder()
    _answer(_turns(sessions, ctx=ctx, conv_log=_Log()), asker=_asker(resumed=True))
    assert sessions.calls("get_or_create")[0][2]["agent"] == "the-dashboard-agent"
    assert ctx.builds[0]["agent"] == "the-dashboard-agent"


# ── delivery-aware accounting and the unclosed-stream seal ───────────────────


class TestDeliveryAwareAccounting:
    @pytest.mark.parametrize("drift", DRIFTS)
    def test_a_reply_that_reached_nobody_is_a_failure(self, drift) -> None:
        sessions = FakeSessions(ScriptedProvider(answer("the reply")))
        renderer = RecordingRenderer(delivery_failed=True)
        recorder = Recorder()

        outcome = _answer(_turns(sessions, recorder=recorder, drift=drift), renderer=renderer)

        assert outcome.verdict is Verdict.UNDELIVERED and outcome.landed is True
        assert sessions.count("record_failure") == 1 and sessions.count("record_success") == 0
        assert [r.kind for r in recorder.records] == ["answered"], "still recorded as said"

    def test_an_undelivered_turn_that_landed_keeps_the_flag_consumed(self) -> None:
        """The provider took the re-injected context; a failed send is a delivery
        failure, not a lost prompt, so the flag is not put back."""
        sessions = FakeSessions(ScriptedProvider(answer("the reply")), reinjection=True)
        ctx = RecordingCtxBuilder(ledger=sessions.ledger)

        outcome = _answer(
            _turns(sessions, ctx=ctx, drift=DISCORD_DRIFT),
            renderer=RecordingRenderer(delivery_failed=True),
        )

        assert outcome.verdict is Verdict.UNDELIVERED
        assert ctx.builds[0]["needs_reinjection"] is True
        assert sessions.count("record_failure") == 1
        assert sessions.count("mark_reinjection") == 0
        assert ctx.settles == [("commit", KEY)]

    def test_only_a_literal_true_reports_an_undelivered_turn(self) -> None:
        sessions = FakeSessions(ScriptedProvider(answer("the reply")))
        outcome = _answer(_turns(sessions), renderer=RecordingRenderer(delivery_failed="yes"))
        assert outcome.verdict is Verdict.ANSWERED and sessions.count("record_success") == 1

    @pytest.mark.parametrize("fail_sends", [False, True])
    def test_the_real_discord_renderer_reports_its_own_delivery(self, fail_sends) -> None:
        client = FakeDiscordClient(fail_sends=fail_sends)
        sessions = FakeSessions(ScriptedProvider(answer("the reply")))

        outcome = _answer(_turns(sessions, drift=DISCORD_DRIFT), renderer=_discord_renderer(client))

        assert (outcome.verdict is Verdict.UNDELIVERED) is fail_sends
        assert "the reply" in client.shown()

    @pytest.mark.parametrize(
        ("drift", "sealed"),
        [
            pytest.param(frozenset(), False, id="default"),
            pytest.param(DISCORD_DRIFT, True, id="discord-drift"),
        ],
    )
    def test_an_unclosed_stream_is_sealed_with_the_drivers_verdict(self, drift, sealed) -> None:
        sessions = FakeSessions(ScriptedProvider([text("half")]))
        renderer = RecordingRenderer()

        outcome = _answer(_turns(sessions, drift=drift), renderer=renderer)

        assert outcome.landed is False and outcome.stop_reason is None
        assert (len(renderer.done) == 1) is sealed
        if sealed:
            assert renderer.done[0].stop_reason == "error"


# ── prepare ──────────────────────────────────────────────────────────────────


class TestPrepare:
    def test_runs_after_the_claim_and_feeds_the_build(self) -> None:
        provider = ScriptedProvider(answer())
        sessions = FakeSessions(provider)
        ctx = RecordingCtxBuilder(ledger=sessions.ledger)
        seen: list[Any] = []

        async def _prepare(p: Any, prompt: str) -> str:
            seen.append(p)
            sessions.ledger.append(("prepare", prompt))
            return prompt + "!"

        _answer(_turns(sessions, ctx=ctx), prepare=_prepare)

        assert seen == [provider]
        names = sessions.names()
        assert names.index("get_or_create") < names.index("prepare") < names.index("build_message")
        assert ctx.builds[0]["text"] == "hi!"

    def test_an_empty_prompt_ends_the_turn_with_nothing_recorded(self) -> None:
        provider = ScriptedProvider()
        sessions = FakeSessions(provider, is_new=True)
        recorder = Recorder()

        async def _nothing(_p: Any, _prompt: str) -> str:
            return ""

        outcome = _answer(_turns(sessions, recorder=recorder), prepare=_nothing)

        assert outcome.verdict is Verdict.EMPTY and provider.prompts == []
        assert recorder.records == [] and sessions.count("set_channel") == 0
        assert sessions.count("record_success") == 0 and sessions.count("release") == 1

    def test_a_failing_prepare_is_a_charged_failure(self) -> None:
        sessions = FakeSessions()

        async def _fails(_p: Any, _prompt: str) -> str:
            raise RuntimeError("attachment fetch failed")

        outcome = _answer(_turns(sessions), prepare=_fails)

        assert outcome.verdict is Verdict.FAILED
        assert sessions.count("record_failure") == 1 and sessions.count("release") == 1


# ── the transient compaction replay ──────────────────────────────────────────


def _abandoned() -> list[Any]:
    return [complete(STOP_REASON_COMPACTION_FAILED)]


class TestCompactionRecovery:
    def test_a_compaction_failed_terminal_resets_the_session(self) -> None:
        sessions = FakeSessions(ScriptedProvider(_abandoned()))
        _answer(_turns(sessions))
        assert sessions.count("reset") == 1 and sessions.count("release") == 1

    def test_an_ordinary_terminal_does_not_reset(self) -> None:
        sessions = FakeSessions()
        _answer(_turns(sessions))
        assert sessions.count("reset") == 0

    def test_a_dispatcher_without_recovery_never_resets_or_replays(self) -> None:
        provider = ScriptedProvider(_abandoned(), transient=True)
        sessions = FakeSessions(provider)
        renderer = RecordingRenderer()

        _answer(_turns(sessions, drift=DISCORD_DRIFT), renderer=renderer)

        assert provider.prompts == ["hi"] and sessions.count("reset") == 0
        assert [e.stop_reason for e in renderer.done] == [STOP_REASON_COMPACTION_FAILED]
        assert "open_replay_gap" not in sessions.names()
        assert "close_replay_gap" not in sessions.names()

    def test_a_transient_failure_replays_the_message_once(self) -> None:
        first = ScriptedProvider(_abandoned(), transient=True, name="first")
        second = ScriptedProvider(answer("the reply"), name="second")
        # The reacquire after a reset cold-starts a runtime and reports it new, the
        # way the real manager does for a conversation that has existed for hours.
        sessions = FakeSessions(first, second, is_new=[False, True])
        recorder = Recorder()
        renderer = RecordingRenderer()

        outcome = _answer(_turns(sessions, recorder=recorder), renderer=renderer)

        assert first.prompts == ["hi"] and second.prompts == ["hi"], "replayed verbatim"
        assert outcome.verdict is Verdict.ANSWERED
        assert [e.stop_reason for e in renderer.done] == ["end_turn"], "one completion shown"
        assert _shown(renderer) == "the reply"
        names = sessions.names()
        assert names.count("get_or_create") == 2 and names.count("release") == 1
        # One gap spans the replay, opened before the reset and closed only once the
        # whole turn settled and gave its permit back.
        assert names.index("open_replay_gap") < names.index("reset")
        assert names.index("release") < names.index("close_replay_gap")
        assert sessions.gap_open is False
        # Post-turn bookkeeping uses THIS message's newness, from the first acquire:
        # the replay's fresh runtime must not re-run the new-conversation work.
        assert [(r.reply_text, r.is_new) for r in recorder.records] == [("the reply", False)]
        assert recorder.surfaced == 0 and sessions.count("set_channel") == 1
        assert outcome.is_new is False

    def test_identity_is_published_before_every_attempts_build(self) -> None:
        sessions = FakeSessions(
            ScriptedProvider(_abandoned(), transient=True), ScriptedProvider(answer())
        )
        ctx = RecordingCtxBuilder(ledger=sessions.ledger)
        _answer(_turns(sessions, ctx=ctx))
        attempts = [n for n in sessions.names() if n in ("publish_turn_identity", "build_message")]
        assert attempts == ["publish_turn_identity", "build_message"] * 2

    def test_the_charge_names_the_attempt_that_failed(self, monkeypatch) -> None:
        """A death after a replay re-claim belongs to the successor's runtime, never
        to the one the reset already discarded."""
        charged: list[Any] = []

        async def _charge(sessions: Any, key: str, *, exc: Any, provider: Any, channel_type: str):
            charged.append(provider)

        monkeypatch.setattr(D, "charge_turn_failure", _charge)
        first = ScriptedProvider(_abandoned(), transient=True, name="first")
        second = ScriptedProvider([RuntimeError("runtime died")], name="second")

        outcome = _answer(_turns(FakeSessions(first, second)))

        assert outcome.verdict is Verdict.FAILED and charged == [second]

    def test_a_permanent_failure_keeps_the_give_up_behaviour(self) -> None:
        provider = ScriptedProvider(_abandoned(), transient=False)
        sessions = FakeSessions(provider)
        renderer = RecordingRenderer()

        _answer(_turns(sessions), renderer=renderer)

        assert provider.prompts == ["hi"] and sessions.count("get_or_create") == 1
        assert [e.stop_reason for e in renderer.done] == [STOP_REASON_COMPACTION_FAILED]
        assert "open_replay_gap" not in sessions.names()

    @pytest.mark.parametrize("transient", [None, "yes"])
    def test_only_a_literal_true_verdict_is_transient(self, transient) -> None:
        provider = ScriptedProvider(_abandoned(), transient=transient)
        _answer(_turns(FakeSessions(provider)))
        assert provider.prompts == ["hi"]

    @pytest.mark.parametrize(
        ("before", "replayed"),
        [
            pytest.param(text("part"), False, id="text-landed"),
            pytest.param(tool_call(), False, id="tool-call-landed"),
            pytest.param(steer_consumed(), False, id="steer-folded"),
            pytest.param(thinking("hmm"), True, id="only-reasoning"),
            pytest.param(compaction_status("failed"), True, id="only-the-compaction-notice"),
        ],
    )
    def test_a_turn_is_replayed_only_while_nothing_landed(self, before, replayed) -> None:
        """Verbatim replay is safe only before text, a tool call, a prompt or a folded
        steer reached the chat; reasoning renders, but replaying after it repeats
        nothing."""
        first = ScriptedProvider([before, complete(STOP_REASON_COMPACTION_FAILED)], transient=True)
        second = ScriptedProvider(answer())
        sessions = FakeSessions(first, second)

        _answer(_turns(sessions))

        assert (second.prompts == ["hi"]) is replayed
        assert sessions.count("reset") == 1

    def test_a_turn_that_showed_an_approval_prompt_is_never_replayed(self) -> None:
        first = ScriptedProvider(
            [permission("r1"), complete(STOP_REASON_COMPACTION_FAILED)], transient=True
        )
        second = ScriptedProvider(answer())

        async def _decider(_event: Any) -> bool:
            return True

        _answer(_turns(FakeSessions(first, second), approval_mode="interactive"), decider=_decider)

        assert first.approved == ["r1"] and second.prompts == []

    def test_the_replay_budget_is_bounded_per_turn(self) -> None:
        budget = D._COMPACTION_FAILED_RETRIES
        providers = [ScriptedProvider(_abandoned(), transient=True) for _ in range(budget + 3)]
        sessions = FakeSessions(*providers)
        renderer = RecordingRenderer()

        _answer(_turns(sessions), renderer=renderer)

        assert sessions.count("get_or_create") == budget + 1
        assert sessions.count("reset") == budget + 1
        assert [e.stop_reason for e in renderer.done] == [STOP_REASON_COMPACTION_FAILED]
        assert sessions.count("release") == 1 and renderer.closed == 1
        # One gap spans every retry: opened before the first reset, closed once, last.
        names = sessions.names()
        assert names.index("open_replay_gap") < names.index("reset")
        assert names[-1] == "close_replay_gap" and names.count("close_replay_gap") == 1

    def test_a_failed_reset_delivers_the_held_completion_instead(self) -> None:
        provider = ScriptedProvider(_abandoned(), transient=True)
        sessions = FakeSessions(provider, reset_raises=True)
        renderer = RecordingRenderer()

        _answer(_turns(sessions), renderer=renderer)

        assert provider.prompts == ["hi"] and sessions.count("get_or_create") == 1
        assert [e.stop_reason for e in renderer.done] == [STOP_REASON_COMPACTION_FAILED]

    @pytest.mark.parametrize("what", ["stop", "new"])
    def test_a_stop_or_new_inside_the_reset_gap_drops_the_replay(self, what) -> None:
        def _in_gap(sessions: FakeSessions) -> None:
            if not sessions.gap_open:
                return
            if what == "stop":
                sessions.stop_gen += 1
            else:
                sessions.max_gen += 1

        first = ScriptedProvider(_abandoned(), transient=True)
        second = ScriptedProvider(answer("must not run"))
        sessions = FakeSessions(first, second, on_reset=_in_gap)
        recorder = Recorder()
        renderer = RecordingRenderer()

        outcome = _answer(_turns(sessions, recorder=recorder), renderer=renderer)

        assert outcome.verdict is Verdict.ABANDONED
        assert second.prompts == [], "the stopped (or retired) message must not run again"
        assert [e.stop_reason for e in renderer.done] == [STOP_REASON_COMPACTION_FAILED]
        assert recorder.records == [] and sessions.count("record_success") == 0
        assert sessions.count("release") == 1 and sessions.gap_open is False


# ── the crew-log opener ──────────────────────────────────────────────────────


class TestTheCrewLogOpener:
    @pytest.fixture
    def opened(self, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []

        def _open(provider: Any, **kw: Any) -> None:
            calls.append({"provider": provider, **kw})

        monkeypatch.setattr(D, "open_turn_crew_log", _open)
        return calls

    def test_the_default_opens_no_crew_log(self, opened) -> None:
        _answer(_turns(FakeSessions()))
        assert opened == []

    def test_an_opener_runs_at_the_allocation_before_any_further_call(self, monkeypatch) -> None:
        provider = ScriptedProvider(answer())
        sessions = FakeSessions(provider)
        opened: list[dict[str, Any]] = []

        def _open(p: Any, **kw: Any) -> None:
            opened.append({"provider": p, **kw})
            sessions.ledger.append(("open_turn_crew_log", kw["session_key"]))

        monkeypatch.setattr(D, "open_turn_crew_log", _open)
        dispatcher = RecordingDispatcher("auto", dashboard_state=object())

        _answer(_turns(sessions, dispatcher=dispatcher, drift=frozenset({Drift.OPENS_CREW_LOG})))

        (call,) = opened
        assert call["provider"] is provider and call["session_key"] == KEY
        assert call["agent"] == "agentA" and call["resumed"] is False
        names = sessions.names()
        assert names.index("get_or_create") + 1 == names.index("open_turn_crew_log")

    @pytest.mark.parametrize(
        ("key", "opens"),
        [
            pytest.param("dashboard:chat-1", False, id="resumed-dashboard-session"),
            pytest.param("discord:agentA:direct:u1", True, id="resumed-channel-history"),
        ],
    )
    def test_a_resumed_dashboard_session_is_left_to_its_own_opener(self, opened, key, opens):
        _answer(
            _turns(FakeSessions(), drift=frozenset({Drift.OPENS_CREW_LOG})),
            asker=_asker(key, resumed=True),
        )
        assert bool(opened) is opens


# ── a monitor wake ───────────────────────────────────────────────────────────


class _Hook:
    """A monitor completion hook: authorizes, and records acceptance and completion."""

    def __init__(self, *, authorize: bool = True) -> None:
        self._authorize = authorize
        self.accepted = False
        self.completed: list[Any] = []

    async def authorize(self) -> bool:
        return self._authorize

    def mark_accepted(self) -> None:
        self.accepted = True

    async def complete(self, disposition: Any, usage: Any) -> None:
        self.completed.append(disposition)


class TestAMonitorWake:
    def _wake(self, hook: _Hook, *, current: bool = True) -> MonitorWake:
        return MonitorWake(hook, lambda: current)  # type: ignore[arg-type]

    def test_a_wake_claims_without_waiting_and_composes_no_ceiling(self, one_turn_ceiling):
        sessions = FakeSessions(ScriptedProvider(answer(), answer()))
        turns = _turns(sessions, drift=DISCORD_DRIFT)
        for _ in range(2):
            hook = _Hook()
            outcome = _answer(turns, asker=_asker(route=None), monitor=self._wake(hook))
            assert outcome.monitor is MonitorDispatchResult.DISPATCHED
        assert all(c[2]["wait_if_busy"] is False for c in sessions.calls("get_or_create"))

    def test_a_busy_conversation_answers_busy_before_anything_is_built(self) -> None:
        sessions = FakeSessions(busy=True)
        built: list[bool] = []

        async def _factory(muted: bool) -> RecordingRenderer:
            built.append(muted)
            return RecordingRenderer()

        outcome = _answer(
            _turns(sessions, drift=DISCORD_DRIFT),
            asker=_asker(route=None),
            renderer=_factory,
            monitor=self._wake(_Hook()),
        )

        assert outcome.verdict is Verdict.BUSY and outcome.monitor is MonitorDispatchResult.BUSY
        assert built == [] and sessions.count("release") == 0

    @pytest.mark.parametrize(
        ("raises", "verdict", "result"),
        [
            (SessionClosingError("closing"), Verdict.SHUTTING_DOWN, MonitorDispatchResult.BUSY),
            (RuntimeError("claim failed"), Verdict.FAILED, MonitorDispatchResult.UNAVAILABLE),
        ],
    )
    def test_a_failed_claim_holds_nothing(self, spooled, raises, verdict, result) -> None:
        sessions = FakeSessions(acquire_raises=raises)
        outcome = _answer(
            _turns(sessions, drift=DISCORD_DRIFT),
            asker=_asker(route=None),
            monitor=self._wake(_Hook()),
        )
        assert outcome.verdict is verdict and outcome.monitor is result
        assert sessions.count("release") == 0 and spooled == []

    def test_a_renderer_that_fails_after_the_claim_gives_the_claim_back(self) -> None:
        sessions = FakeSessions()

        async def _broken(_muted: bool) -> RecordingRenderer:
            raise RuntimeError("renderer setup failed")

        outcome = _answer(
            _turns(sessions, drift=DISCORD_DRIFT),
            asker=_asker(route=None),
            renderer=_broken,
            monitor=self._wake(_Hook()),
        )

        assert outcome.monitor is MonitorDispatchResult.BUSY
        assert sessions.count("release") == 1

    def test_a_refused_wake_counts_records_and_audits_nothing(self) -> None:
        provider = ScriptedProvider()
        sessions = FakeSessions(provider)
        recorder = Recorder()

        outcome = _answer(
            _turns(sessions, recorder=recorder, drift=DISCORD_DRIFT),
            asker=_asker(route=None),
            monitor=self._wake(_Hook(authorize=False)),
        )

        assert outcome.verdict is Verdict.STALE
        assert outcome.monitor is MonitorDispatchResult.UNAVAILABLE
        assert provider.prompts == [] and recorder.records == []
        assert sessions.count("record_success") == 0 and sessions.count("release") == 1

    def test_a_replaced_conversation_refuses_the_wake_at_its_gate(self) -> None:
        provider = ScriptedProvider()
        sessions = FakeSessions(provider)

        outcome = _answer(
            _turns(sessions, drift=DISCORD_DRIFT),
            asker=_asker(route=None),
            monitor=self._wake(_Hook(), current=False),
        )

        assert outcome.verdict is Verdict.STALE and provider.prompts == []
        assert sessions.count("begin_turn") == 0 and sessions.count("release") == 1

    @pytest.mark.parametrize(
        ("fails_in", "result"),
        [
            pytest.param("stream", MonitorDispatchResult.DISPATCHED, id="after-acceptance"),
            pytest.param("build", MonitorDispatchResult.BUSY, id="before-acceptance"),
        ],
    )
    def test_a_failure_after_the_claim_reports_whether_the_wake_ran(self, fails_in, result):
        """A wake that failed after its provider turn opened has spent its claim; one
        that failed before it was accepted leaves the claim retryable."""
        if fails_in == "stream":
            sessions = FakeSessions(ScriptedProvider([RuntimeError("provider fell over")]))
            ctx = RecordingCtxBuilder()
        else:
            sessions = FakeSessions()
            ctx = RecordingCtxBuilder(build_raises=RuntimeError("build failed"))

        outcome = _answer(
            _turns(sessions, ctx=ctx, drift=DISCORD_DRIFT),
            asker=_asker(route=None),
            monitor=self._wake(_Hook()),
        )

        assert outcome.verdict is Verdict.FAILED and outcome.monitor is result
        assert sessions.count("record_failure") == 1 and sessions.count("release") == 1

    def test_a_shutdown_at_the_wakes_gate_answers_busy_and_spools_nothing(self, spooled):
        sessions = FakeSessions(closing=True)
        outcome = _answer(
            _turns(sessions, drift=DISCORD_DRIFT),
            asker=_asker(route=None),
            monitor=self._wake(_Hook()),
        )
        assert outcome.monitor is MonitorDispatchResult.BUSY and spooled == []


# ── the session-directive consumer ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("drift", "consumes"),
    [
        pytest.param(DISCORD_DRIFT, True, id="discord-drift"),
        pytest.param(frozenset({Drift.NO_DIRECTIVES}), False, id="no-directives"),
    ],
)
def test_a_dispatcher_without_directives_builds_no_consumer(monkeypatch, drift, consumes) -> None:
    built: list[dict[str, Any]] = []
    real = D.build_directive_consumer

    def _build(**kw: Any) -> Any:
        built.append(kw)
        return real(**kw)

    monkeypatch.setattr(D, "build_directive_consumer", _build)
    dispatcher = RecordingDispatcher("auto")

    _answer(_turns(FakeSessions(), dispatcher=dispatcher, drift=drift))

    assert built == (
        [{"session_key": KEY, "sessions": built[0]["sessions"], "dispatcher": dispatcher}]
        if consumes
        else []
    )


# ── the record adapter every exchange-only channel uses ──────────────────────


def test_exchange_writer_writes_answers_off_loop_and_drops_failures() -> None:
    written: list[tuple[Any, ...]] = []
    threads: list[int] = []

    def _persist(*args: Any) -> None:
        threads.append(threading.get_ident())
        written.append(args)

    record = exchange_writer(_persist)

    async def scenario() -> None:
        await record(TurnRecord("answered", KEY, "agentA", "hi", "the reply", True))
        await record(TurnRecord("hook_reply", KEY, "agentA", "ping", "pong", False))
        await record(TurnRecord("failed", KEY, "agentA", "hi", "", False, error=RuntimeError()))

    asyncio.run(scenario())
    assert written == [
        (KEY, "hi", "the reply", True, "agentA"),
        (KEY, "ping", "pong", False, "agentA"),
    ]
    assert threading.get_ident() not in threads


# ── drive_turn: the ChannelTurn entry, field for field ───────────────────────


def _channel_turn(renderer: Any, **kw: Any) -> ChannelTurn:
    kw.setdefault("session_key", KEY)
    kw.setdefault("approval_mode", "auto")
    return ChannelTurn(
        channel_type="weixin",
        conversation_id=CONV,
        agent="agentA",
        user_text="hi",
        renderer=renderer,
        **kw,
    )


class TestDriveTurn:
    def test_persists_the_raw_reply_with_the_first_acquires_newness(self) -> None:
        persisted: list[tuple[str, str, bool]] = []
        sessions = FakeSessions(ScriptedProvider([text("  "), complete()]), is_new=True)
        turn = _channel_turn(
            RecordingRenderer(), persist=lambda u, r, n: persisted.append((u, r, n))
        )

        asyncio.run(drive_turn(turn, sessions=sessions, ctx_builder=RecordingCtxBuilder()))

        assert persisted == [("hi", "  ", True)]

    def test_hands_the_live_provider_on_before_the_driver_and_degrades_on_failure(self) -> None:
        provider = ScriptedProvider(answer())
        sessions = FakeSessions(provider)
        seen: list[Any] = []

        def _bind(p: Any) -> None:
            seen.append(p)
            sessions.ledger.append(("bind_provider", ""))
            raise RuntimeError("no cwd")

        turn = _channel_turn(RecordingRenderer(), bind_provider=_bind)
        asyncio.run(
            drive_turn(
                turn, sessions=sessions, ctx_builder=RecordingCtxBuilder(ledger=sessions.ledger)
            )
        )

        assert seen == [provider]
        names = sessions.names()
        assert names.index("bind_provider") < names.index("build_message")
        assert sessions.count("record_success") == 1

    def test_an_unprompted_refusal_ends_silently(self) -> None:
        renderer = RecordingRenderer()
        turn = _channel_turn(
            renderer,
            session_key=f"weixin:{TOOLLESS_TURN_AGENT}:direct:peerB",
            deny_all_tools=True,
            unprompted=True,
        )
        sessions = FakeSessions(ScriptedProvider(backend=ACP_BACKEND_CLAUDE))
        asyncio.run(drive_turn(turn, sessions=sessions, ctx_builder=RecordingCtxBuilder()))
        assert renderer.shown == [] and renderer.closed == 1

    def test_a_disconnected_conversation_is_silenced(self) -> None:
        provider = ScriptedProvider(answer())
        sessions = FakeSessions(provider)
        sessions.muted.add((KEY, True))  # a channel-born key reads the origin flag
        renderer = RecordingRenderer()
        asyncio.run(
            drive_turn(
                _channel_turn(renderer), sessions=sessions, ctx_builder=RecordingCtxBuilder()
            )
        )
        assert provider.prompts == ["hi"], "the message still lands in the session"
        assert renderer.shown == [] and renderer.started == 0 and renderer.closed == 0

    def test_the_origin_conversation_is_bound_off_the_loop(self) -> None:
        threads: list[int] = []

        class _Threaded(FakeSessions):
            def set_mirror_link(self, key: str, link: Any) -> None:
                threads.append(threading.get_ident())
                super().set_mirror_link(key, link)

        sessions = _Threaded()
        turn = _channel_turn(RecordingRenderer(), origin_conversation=_ROOM)
        asyncio.run(drive_turn(turn, sessions=sessions, ctx_builder=RecordingCtxBuilder()))
        assert sessions.origin_links[KEY] is _ROOM and sessions.mirror_links[KEY] is _ROOM
        assert threads and threading.get_ident() not in threads

    def test_a_restricted_shutdown_refusal_never_spools(self, spooled) -> None:
        turn = _channel_turn(RecordingRenderer(), inbound_route=ROUTE, inbound_restricted=True)
        asyncio.run(
            drive_turn(turn, sessions=FakeSessions(closing=True), ctx_builder=RecordingCtxBuilder())
        )
        assert spooled == []

    def test_an_unprompted_turn_is_still_recorded(self) -> None:
        """``unprompted`` only silences the tool-less refusal; whether the exchange is
        written is the channel's ``persist`` alone."""
        persisted: list[tuple[str, str, bool]] = []
        turn = _channel_turn(
            RecordingRenderer(),
            unprompted=True,
            persist=lambda u, r, n: persisted.append((u, r, n)),
        )
        asyncio.run(
            drive_turn(
                turn,
                sessions=FakeSessions(ScriptedProvider(answer("the reply"))),
                ctx_builder=RecordingCtxBuilder(),
            )
        )
        assert [(u, "".join(r), n) for u, r, n in persisted] == [("hi", "the reply", False)]

    def test_an_unprompted_hook_reply_is_still_recorded_after_the_gap(self) -> None:
        persisted: list[tuple[str, str, bool]] = []
        sessions = FakeSessions()
        turn = _channel_turn(
            RecordingRenderer(),
            unprompted=True,
            persist=lambda u, r, n: persisted.append((u, r, n)),
        )
        asyncio.run(
            drive_turn(turn, sessions=sessions, ctx_builder=RecordingCtxBuilder(hook_reply="pong"))
        )
        assert persisted == [("hi", "pong", False)]
        assert sessions.count("await_replay_gap") == 1

    @pytest.mark.parametrize(
        "priority", [None, StartPriority.BACKGROUND, StartPriority.FOREGROUND], ids=str
    )
    def test_the_turns_priority_reaches_the_claim(self, priority) -> None:
        """An automation or unprompted turn (the ``ChannelTurn`` default) cold-starts
        in the background; only a person's message starts at person priority."""
        sessions = FakeSessions()
        kw = {} if priority is None else {"start_priority": priority}
        turn = _channel_turn(RecordingRenderer(), **kw)
        asyncio.run(drive_turn(turn, sessions=sessions, ctx_builder=RecordingCtxBuilder()))
        expected = StartPriority.BACKGROUND if priority is None else priority
        assert sessions.calls("get_or_create")[0][2]["start_priority"] is expected

    def test_the_turns_own_fields_reach_the_driver(self) -> None:
        provider = ScriptedProvider([permission("r1"), complete()])
        consumed: list[Any] = []

        async def _directives(kind: str, args: dict[str, Any]) -> None:
            consumed.append(kind)

        ctx = RecordingCtxBuilder()
        turn = _channel_turn(
            RecordingRenderer(),
            approval_mode="interactive",
            auto_approve_session=lambda: True,
            minimal_context=True,
            directive_consumer=_directives,
            user_display_name="Ann",
        )
        asyncio.run(drive_turn(turn, sessions=FakeSessions(provider), ctx_builder=ctx))
        assert provider.approved == ["r1"]
        assert ctx.builds[0]["minimal_context"] is True
        assert ctx.builds[0]["user_display_name"] == "Ann"

    def test_the_turns_directive_consumer_receives_its_directives(self) -> None:
        """The per-turn consumer the channel built is the one the driver hands a
        session directive to, not a default one."""
        provider = ScriptedProvider([*directive("autonudge_stop"), complete()])
        consumed: list[Any] = []

        async def _directives(kind: str, args: dict[str, Any]) -> None:
            consumed.append(kind)

        turn = _channel_turn(RecordingRenderer(), directive_consumer=_directives)
        asyncio.run(
            drive_turn(turn, sessions=FakeSessions(provider), ctx_builder=RecordingCtxBuilder())
        )
        assert consumed == ["autonudge_stop"], "the turn's own consumer was never handed it"

    def test_without_a_grant_an_interactive_turn_denies_what_no_one_answers(self) -> None:
        """No ``auto_approve_session`` and no decider: a permission request is
        rejected, never approved (the grant is opt-in per channel)."""
        provider = ScriptedProvider([permission("r1"), complete()])
        turn = _channel_turn(RecordingRenderer(), approval_mode="interactive")
        asyncio.run(
            drive_turn(turn, sessions=FakeSessions(provider), ctx_builder=RecordingCtxBuilder())
        )
        assert provider.rejected == ["r1"] and provider.approved == []


# ── the Discord leg: Discord's drift on its real renderer ────────────────────


class _LoggingDiscordRenderer(DiscordRenderer):
    """The real renderer, writing its turn start to a session ledger and, when
    asked, failing its close the way a flush can."""

    def __init__(self, client: FakeDiscordClient, ledger: list[Any], *, close_raises: bool = False):
        super().__init__(client, "c1", DISCORD_CAPABILITIES, session_key=KEY)
        self._ledger = ledger
        self._close_raises = close_raises

    async def on_turn_start(self) -> None:
        self._ledger.append(("on_turn_start", ""))
        await super().on_turn_start()

    async def close(self) -> None:
        await super().close()
        if self._close_raises:
            raise RuntimeError("renderer finalization failed")


class TestTheDiscordLeg:
    """The turn cases Discord's own suite drove through ``handle_message``, run at
    the pipeline with ``DISCORD_DRIFT`` and the real ``DiscordRenderer`` over a fake
    REST client, so what is asserted is what the conversation could read."""

    @staticmethod
    def _run(sessions: FakeSessions, client: FakeDiscordClient, **kw: Any) -> Any:
        renderer = kw.pop("renderer", None) or _LoggingDiscordRenderer(client, sessions.ledger)
        return _answer(
            _turns(sessions, drift=DISCORD_DRIFT, channel="discord", **kw), renderer=renderer
        )

    def test_a_turn_streams_its_reply_and_releases_once(self) -> None:
        client = FakeDiscordClient()
        sessions = FakeSessions(ScriptedProvider(answer("the reply")))

        outcome = self._run(sessions, client)

        assert outcome.verdict is Verdict.ANSWERED and "the reply" in client.shown()
        assert sessions.count("release") == 1 and sessions.held == 0

    def test_the_typing_indicator_starts_before_the_cold_start(self) -> None:
        sessions = FakeSessions()

        self._run(sessions, FakeDiscordClient())

        names = sessions.names()
        assert names.index("on_turn_start") < names.index("get_or_create")

    def test_a_disconnected_conversation_posts_nothing(self) -> None:
        client = FakeDiscordClient()
        provider = ScriptedProvider(answer("the reply"))
        sessions = FakeSessions(provider)
        # Both flags: which one a turn reads depends on whether its key is native
        # to the channel (that rule has its own test above).
        sessions.muted.update({(KEY, True), (KEY, False)})

        outcome = self._run(sessions, client)

        assert provider.prompts == ["hi"], "the message still lands in the session"
        assert client.sent == [] and client.edits == []
        assert outcome.verdict is Verdict.ANSWERED and sessions.count("release") == 1

    def test_a_member_memory_refusal_is_posted_redacted(self, monkeypatch) -> None:
        secret = "ghp_" + "x" * 36

        async def _refuse(_ctx: Any, _key: str) -> str:
            raise UnknownMemoryStore(
                f"Member memory unavailable: cannot read /srv/alice/memory.db; token={secret}. "
                "Repair this member's memory. Global Memory V1 was not used."
            )

        monkeypatch.setattr(D, "session_store_for_turn", _refuse)
        client = FakeDiscordClient()
        sessions = FakeSessions()

        outcome = self._run(sessions, client)

        shown = client.shown()
        assert outcome.verdict is Verdict.MEMORY_REFUSED
        assert "Repair this member's memory" in shown
        assert "alice" not in shown and secret not in shown
        assert sessions.count("get_or_create") == 0 and sessions.count("release") == 0

    def test_a_shutdown_never_opens_the_turn_and_spools_its_route(self, spooled) -> None:
        provider = ScriptedProvider()
        sessions = FakeSessions(provider, closing=True)

        outcome = self._run(sessions, FakeDiscordClient())

        assert outcome.verdict is Verdict.SHUTTING_DOWN and provider.prompts == []
        assert spooled == [("discord", ROUTE)] and sessions.count("release") == 1

    def test_a_restricted_shutdown_is_not_spooled(self, spooled) -> None:
        async def _restricted(_key: str) -> bool:
            return True

        self._run(FakeSessions(closing=True), FakeDiscordClient(), restricted=_restricted)

        assert spooled == []

    def test_a_cold_start_failure_releases_and_charges_nothing(self) -> None:
        sessions = FakeSessions(acquire_raises=RuntimeError("cold start failed"))

        outcome = self._run(sessions, FakeDiscordClient())

        assert outcome.verdict is Verdict.FAILED
        assert sessions.count("release") == 0 and sessions.count("record_failure") == 0

    def test_the_session_is_released_when_the_close_raises(self) -> None:
        client = FakeDiscordClient()
        sessions = FakeSessions(ScriptedProvider(answer("the reply")))
        renderer = _LoggingDiscordRenderer(client, sessions.ledger, close_raises=True)

        outcome = self._run(sessions, client, renderer=renderer)

        assert outcome.verdict is Verdict.ANSWERED and sessions.count("release") == 1

    @pytest.mark.parametrize("fail_sends", [False, True])
    def test_a_compacted_session_forwards_the_flag_and_a_landed_turn_keeps_it(
        self, fail_sends
    ) -> None:
        """A send that failed after the provider took the context is a delivery
        failure, not a lost prompt: the flag stays consumed either way."""
        client = FakeDiscordClient(fail_sends=fail_sends)
        sessions = FakeSessions(ScriptedProvider(answer("the reply")), reinjection=True)
        ctx = RecordingCtxBuilder(ledger=sessions.ledger)

        outcome = self._run(sessions, client, ctx=ctx)

        assert (outcome.verdict is Verdict.UNDELIVERED) is fail_sends
        assert ctx.builds[0]["needs_reinjection"] is True
        assert sessions.count("mark_reinjection") == 0

    @pytest.mark.parametrize(
        "script",
        [
            pytest.param([RuntimeError("provider fell over")], id="raised"),
            pytest.param([complete(STOP_REASON_CANCELLED)], id="cancelled"),
        ],
    )
    def test_a_consuming_turn_that_never_landed_puts_the_flag_back(self, script) -> None:
        sessions = FakeSessions(ScriptedProvider(script), reinjection=True)
        ctx = RecordingCtxBuilder(ledger=sessions.ledger)

        self._run(sessions, FakeDiscordClient(), ctx=ctx)

        assert ctx.builds[0]["needs_reinjection"] is True
        assert sessions.count("mark_reinjection") == 1
