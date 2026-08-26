"""Finalization contract of the shared channel turn pipeline.

``drive_turn`` owns the semaphore lifetime for every adopted channel, so a bug
in its ``finally`` is a bug in all of them at once. These tests pin the part
that is invisible on the happy path: what happens to ``release()`` when
finalization itself fails.
"""

from __future__ import annotations

import asyncio
from typing import Any

from kiro_crew.messaging import dispatch as D
from kiro_crew.messaging.dispatch import ChannelTurn, drive_turn
from kiro_crew.messaging.renderer import SilentRenderer


class _Sessions:
    """Minimal stand-in that counts the calls this contract is about."""

    def __init__(self, raise_on_acquire: bool = False):
        self.released = 0
        self.successes = 0
        self.failures = 0
        self._raise_on_acquire = raise_on_acquire

    async def get_or_create(self, key, agent=None, channel_id=None):
        if self._raise_on_acquire:
            raise RuntimeError("cold start failed")
        return object(), False, False

    async def set_channel(self, key, channel_id):
        pass

    def record_success(self, key):
        self.successes += 1

    async def record_failure(self, key):
        self.failures += 1

    def release(self, key):
        self.released += 1

    def get_provider(self, key):
        return object()


class _Renderer:
    """Renderer whose ``close`` can fail the way a real one can mid-flush."""

    def __init__(self, close_raises: bool = False):
        self.close_raises = close_raises
        self.closed = 0

    async def on_turn_start(self):
        pass

    async def close(self):
        self.closed += 1
        if self.close_raises:
            raise RuntimeError("renderer finalization failed")


class _CtxBuilder:
    def build_message(self, text, is_new, session_key, **kw):
        return text, None


class _Driver:
    def __init__(self, *a, **kw):
        pass

    async def run(self, message):
        return "the reply"


def _turn(renderer: Any) -> ChannelTurn:
    return ChannelTurn(
        channel_type="weixin",
        session_key="weixin:agentA:direct:userA",
        conversation_id="weixin:userA",
        agent="agentA",
        user_text="hi",
        renderer=renderer,
        approval_mode="auto",
    )


def _patch_pipeline(monkeypatch, *, permitted: bool = True):
    """Stub everything drive_turn touches except the finalization under test."""

    async def _permitted(_channel_type):
        return permitted

    async def _publish(_sessions, _key):
        pass

    async def _embed(fn, *args, **kw):
        return fn(*args, **kw)

    monkeypatch.setattr(D, "inbound_permitted", _permitted)
    monkeypatch.setattr(D, "publish_turn_identity", _publish)
    monkeypatch.setattr(D, "run_in_embed_pool", _embed)
    monkeypatch.setattr(D, "TurnDriver", _Driver)


def test_release_still_runs_when_renderer_close_fails(monkeypatch) -> None:
    """A failed renderer.close must NOT strand the session semaphore.

    The semaphore is keyed by SESSION, so leaking it does not merely lose this
    turn -- every later message for that conversation blocks forever and any
    queued turn never drains, until the gateway restarts.
    """
    _patch_pipeline(monkeypatch)
    sessions = _Sessions()
    renderer = _Renderer(close_raises=True)

    asyncio.run(drive_turn(_turn(renderer), sessions=sessions, ctx_builder=_CtxBuilder()))

    assert renderer.closed == 1, "close should still be attempted"
    assert sessions.released == 1, (
        "renderer.close raised and the session was never released -- the "
        "conversation is now permanently busy"
    )


def test_a_failing_close_does_not_escape_drive_turn(monkeypatch) -> None:
    """The failure is logged and swallowed, not raised at the caller.

    Adopters call drive_turn from a per-message task; letting finalization
    raise would surface as an unhandled task exception for a turn that already
    delivered its reply.
    """
    _patch_pipeline(monkeypatch)
    sessions = _Sessions()

    # asyncio.run re-raises anything drive_turn lets escape.
    asyncio.run(
        drive_turn(
            _turn(_Renderer(close_raises=True)),
            sessions=sessions,
            ctx_builder=_CtxBuilder(),
        )
    )

    assert sessions.successes == 1, "the turn itself succeeded"


def test_release_is_not_called_when_the_semaphore_was_never_acquired(monkeypatch) -> None:
    """The _acquired gate must survive the new guard.

    A cold-start failure raises before get_or_create returns, so nothing was
    ever held -- releasing here would hand back a permit that does not exist.
    """
    _patch_pipeline(monkeypatch)
    sessions = _Sessions(raise_on_acquire=True)
    renderer = _Renderer(close_raises=True)

    asyncio.run(drive_turn(_turn(renderer), sessions=sessions, ctx_builder=_CtxBuilder()))

    assert renderer.closed == 1, "finalization still runs on the failure path"
    assert sessions.released == 0, "nothing was acquired, so nothing may be released"
    assert sessions.failures == 0, "record_failure is also gated on _acquired"


def test_the_happy_path_releases_exactly_once(monkeypatch) -> None:
    """Guard rail: the new try/except must not double-release."""
    _patch_pipeline(monkeypatch)
    sessions = _Sessions()
    renderer = _Renderer()

    asyncio.run(drive_turn(_turn(renderer), sessions=sessions, ctx_builder=_CtxBuilder()))

    assert renderer.closed == 1
    assert sessions.released == 1
    assert sessions.successes == 1


def test_a_denied_turn_neither_renders_nor_releases(monkeypatch) -> None:
    """Governance backstop returns before any side effect."""
    _patch_pipeline(monkeypatch, permitted=False)
    sessions = _Sessions()
    renderer = _Renderer()

    asyncio.run(drive_turn(_turn(renderer), sessions=sessions, ctx_builder=_CtxBuilder()))

    assert renderer.closed == 0
    assert sessions.released == 0
    assert sessions.successes == 0


class _PauseSessions(_Sessions):
    """Interface parity with the real SessionManager for the pause lookup.

    Extended here rather than leaning on production's fail-open: that fallback
    exists for the bare ``MagicMock`` managers elsewhere in the suite, and a test
    about the gate must not be silently exercising the fallback instead.
    """

    def __init__(self, paused: bool = False):
        super().__init__()
        self.paused = paused
        self.pause_calls: list[tuple[str, bool]] = []

    def is_mirror_paused(self, key, *, origin=False):
        self.pause_calls.append((key, origin))
        return self.paused


class _CountingRenderer(_Renderer):
    """Records the turn-start the user would SEE as a typing indicator."""

    def __init__(self):
        super().__init__()
        self.started = 0

    async def on_turn_start(self):
        self.started += 1


def _capture_driver(box: list) -> type:
    class _Capturing(_Driver):
        def __init__(self, provider, renderer, **kw):
            super().__init__()
            box.append(renderer)

    return _Capturing


def _turn_with_key(renderer: Any, session_key: str) -> ChannelTurn:
    return ChannelTurn(
        channel_type="weixin",
        session_key=session_key,
        conversation_id="weixin:userA",
        agent="agentA",
        user_text="hi",
        renderer=renderer,
        approval_mode="auto",
    )


def test_a_disconnected_conversation_is_silenced(monkeypatch) -> None:
    """Disconnect stops the replies, which for a non-Slack channel happens HERE.

    Slack enforces a disconnect on its own streaming mirror. Every other channel
    answers through this pipeline, so before this gate a disconnected channel
    kept replying and the dashboard control changed nothing but its own label.

    The turn still runs and the semaphore is still released: the binding is
    retained by design, so the inbound message must still land in the session.
    """
    box: list[Any] = []
    _patch_pipeline(monkeypatch)
    monkeypatch.setattr(D, "TurnDriver", _capture_driver(box))
    sessions = _PauseSessions(paused=True)
    renderer = _CountingRenderer()

    asyncio.run(
        drive_turn(
            _turn_with_key(renderer, "weixin:agentA:direct:userA"),
            sessions=sessions,
            ctx_builder=_CtxBuilder(),
        )
    )

    assert isinstance(box[0], SilentRenderer), "the driver must stream into the silent one"
    assert renderer.started == 0, "a disconnected conversation must not even show typing"
    assert renderer.closed == 0, "the real renderer was never used, so it has nothing to close"
    assert sessions.successes == 1, "the turn still ran"
    assert sessions.released == 1, "and the session semaphore was still released"


def test_a_connected_conversation_keeps_its_real_renderer(monkeypatch) -> None:
    """The non-vacuity half: without it, deleting the gate would still pass above."""
    box: list[Any] = []
    _patch_pipeline(monkeypatch)
    monkeypatch.setattr(D, "TurnDriver", _capture_driver(box))
    sessions = _PauseSessions(paused=False)
    renderer = _CountingRenderer()

    asyncio.run(
        drive_turn(
            _turn_with_key(renderer, "weixin:agentA:direct:userA"),
            sessions=sessions,
            ctx_builder=_CtxBuilder(),
        )
    )

    assert box[0] is renderer
    assert renderer.started == 1
    assert renderer.closed == 1


def test_the_pause_is_read_for_the_role_the_turn_arrived_on(monkeypatch) -> None:
    """Two non-Slack deliveries mute independently, so the ROLE decides the flag.

    A channel-BORN session's key IS its conversation, so a turn arriving in that
    namespace is the origin. Anything else reaching this pipeline came over a
    mirror/resume binding. Reading the wrong flag would let one row's disconnect
    silence the other's conversation.
    """
    _patch_pipeline(monkeypatch)

    born = _PauseSessions(paused=False)
    asyncio.run(
        drive_turn(
            _turn_with_key(_CountingRenderer(), "weixin:agentA:direct:userA"),
            sessions=born,
            ctx_builder=_CtxBuilder(),
        )
    )
    assert born.pause_calls == [("weixin:agentA:direct:userA", True)], "born-in reads origin"

    mirrored = _PauseSessions(paused=False)
    asyncio.run(
        drive_turn(
            _turn_with_key(_CountingRenderer(), "dashboard:chat-1"),
            sessions=mirrored,
            ctx_builder=_CtxBuilder(),
        )
    )
    assert mirrored.pause_calls == [("dashboard:chat-1", False)], "a mirror reads the mirror flag"


def _capture_driver_kwargs(box: list) -> type:
    """A driver stand-in recording the kwargs the pipeline constructs it with."""

    class _Capturing(_Driver):
        def __init__(self, provider, renderer, **kw):
            super().__init__()
            box.append(kw)

    return _Capturing


# ---------------------------------------------------------------------------
# What the pipeline forwards to the driver, and what it binds per turn.
#
# Both of these were asymmetries rather than missing features: the field existed
# on the driver and the helper existed in ``link``, but the shared pipeline never
# passed them, so every channel riding ``drive_turn`` (webex, wecom, teams,
# weixin, imessage) silently lost a capability the forked channels had.
# ---------------------------------------------------------------------------


class _MirrorSessions(_Sessions):
    """Adds the origin/mirror surface ``drive_turn`` binds through."""

    def __init__(self, *, opt_out: bool = False, existing=None, raises: bool = False):
        super().__init__()
        self.origin_links: dict = {}
        self.mirror_links: dict = {} if existing is None else dict(existing)
        self._opt_out = opt_out
        self._raises = raises

    def set_origin_link(self, key, link):
        if self._raises:
            raise RuntimeError("session map unavailable")
        self.origin_links[key] = link

    def mirror_opt_out(self, key) -> bool:
        return self._opt_out

    def get_mirror_link(self, key):
        return self.mirror_links.get(key)

    def set_mirror_link(self, key, link, *, reason=""):
        self.mirror_links[key] = link


def _capture_turn_driver(box: dict) -> type:
    class _Capturing(_Driver):
        def __init__(self, provider, renderer, **kw):
            box.update(kw)
            super().__init__(provider, renderer, **kw)

    return _Capturing


def test_auto_approve_session_reaches_the_driver(monkeypatch) -> None:
    """A channel with no approve/deny buttons needs an out-of-band trust grant.

    Teams renders no widget, so under INTERACTIVE the ladder denies every tool and
    the agent can only talk. ``ChannelTurn.auto_approve_session`` is how such a
    channel grants trust; if the pipeline drops it, the grant silently does
    nothing and the channel looks like the feature does not exist.
    """
    box: list = []
    _patch_pipeline(monkeypatch)
    monkeypatch.setattr(D, "TurnDriver", _capture_driver_kwargs(box))
    turn = _turn(_CountingRenderer())
    turn.auto_approve_session = lambda: True

    asyncio.run(drive_turn(turn, sessions=_Sessions(), ctx_builder=_CtxBuilder()))

    assert box, "the driver was never constructed"
    predicate = box[0].get("auto_approve_session")
    assert predicate is not None and predicate() is True


def test_omitting_auto_approve_session_keeps_the_deny_default(monkeypatch) -> None:
    """The field is additive: a channel that does not set it is unaffected.

    Four other channels ride this pipeline, so a None default that leaked through
    as something truthy would hand them an auto-approve nobody granted.
    """
    box: list = []
    _patch_pipeline(monkeypatch)
    monkeypatch.setattr(D, "TurnDriver", _capture_driver_kwargs(box))

    asyncio.run(
        drive_turn(_turn(_CountingRenderer()), sessions=_Sessions(), ctx_builder=_CtxBuilder())
    )

    assert box[0].get("auto_approve_session") is None


class _RecordingCtxBuilder:
    """Captures the kwargs the pipeline hands ``build_message``.

    The signature is spelled out rather than swallowed into ``**kw`` for
    ``minimal_context``, so a pipeline that stops forwarding it fails here
    instead of quietly falling back to the builder's own default.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def build_message(self, text, is_new, session_key, *, minimal_context=False, **kw):
        self.calls.append({"minimal_context": minimal_context, **kw})
        return text, None


def _turn_minimal(renderer: Any, *, minimal_context: bool) -> ChannelTurn:
    return ChannelTurn(
        channel_type="weixin",
        session_key="weixin:agentA:direct:userA",
        conversation_id="weixin:userA",
        agent="agentA",
        user_text="hi",
        renderer=renderer,
        approval_mode="auto",
        minimal_context=minimal_context,
    )


def test_minimal_context_reaches_build_message(monkeypatch) -> None:
    """A non-operator's turn must be assembled WITHOUT the operator's context.

    The exposure is in the PROMPT: memory, lessons, skills and prior history are
    injected before any tool runs, so denying the sender's tools does not stop the
    operator's private notes from being quoted back to an admitted peer. The
    pipeline is the only place that calls ``build_message``, so a flag it drops is
    a flag no channel can set.
    """
    _patch_pipeline(monkeypatch)
    ctx = _RecordingCtxBuilder()

    asyncio.run(
        drive_turn(
            _turn_minimal(_Renderer(), minimal_context=True),
            sessions=_Sessions(),
            ctx_builder=ctx,
        )
    )

    assert ctx.calls, "build_message was never called"
    assert ctx.calls[0]["minimal_context"] is True, (
        "the pipeline dropped minimal_context, so the peer's turn was built with "
        "the operator's memory, lessons, skills and history"
    )


def test_the_default_turn_still_gets_full_context(monkeypatch) -> None:
    """The non-vacuity half: the default must stay byte-identical for adopters.

    Without this, hardcoding ``minimal_context=True`` in the pipeline would pass
    the test above while stripping every existing channel's context.
    """
    _patch_pipeline(monkeypatch)
    ctx = _RecordingCtxBuilder()

    asyncio.run(
        drive_turn(
            _turn(_Renderer()),  # constructed without naming the field at all
            sessions=_Sessions(),
            ctx_builder=ctx,
        )
    )

    assert ctx.calls[0]["minimal_context"] is False


class _GovernanceStub:
    """Records what the shared gate asked governance, and answers a fixed verdict."""

    def __init__(self, permitted: bool) -> None:
        self.permitted = permitted
        self.asked: list[str] = []

    async def __call__(self, channel_type: str) -> bool:
        self.asked.append(channel_type)
        return self.permitted


def _gate(monkeypatch, *, permitted: bool) -> _GovernanceStub:
    stub = _GovernanceStub(permitted)
    monkeypatch.setattr(D, "channel_inbound_permitted", stub)
    return stub


class TestPureCancelPredicate:
    """PURE is what makes the governance exemption safe to grant."""

    def test_every_channel_spelling_is_recognised(self) -> None:
        # 停止 is WeCom's, and it is the one that was missing: the ASCII spellings
        # are not reachable for a user whose whole surface is Chinese.
        for text in ("/stop", "/cancel", "!stop", "!cancel", "停止"):
            assert D.is_pure_cancel(text), text
            assert D.is_pure_cancel(f"  {text.upper()}  "), text

    def test_an_attachment_makes_it_impure(self) -> None:
        """The channel fetches media AFTER authorizing, so this is the leak edge."""
        assert D.is_pure_cancel("/stop", has_attachments=True) is False

    def test_anything_beyond_the_word_is_an_ordinary_message(self) -> None:
        for text in (
            "/stop please",
            "please /stop",
            "/stopwatch",
            "/restart",
            "!restart",
            "stop",
            "",
        ):
            assert D.is_pure_cancel(text) is False, text

    def test_the_shared_set_covers_the_channel_command_tables(self) -> None:
        """Drift tripwire: a channel alias the shared gate does not know is a hole.

        DISCOVERED rather than listed. The first version of this test imported
        Discord's and Telegram's tables by name, which made it blind in exactly
        the way the mirror it guards is blind: WeCom, Teams and WhatsApp each
        declare their own stop spellings, and WeCom's ``停止`` reached its
        ``/help`` card while the shared exemption did not know the word. A test
        that hand-lists the channels is the same mirror one level up, so this
        walks the packages instead and a new channel is covered by existing.

        The three shapes below are the ones in the tree. An unrecognised shape
        FAILS rather than being skipped: a channel whose table this cannot read
        is a channel whose drift it cannot see, and silence there is the whole
        defect being re-created.
        """
        import importlib
        import pkgutil

        import kiro_crew

        found: dict[str, set[str]] = {}
        unreadable: list[str] = []
        for mod in pkgutil.iter_modules(kiro_crew.__path__):
            # `messaging` is the shared layer that OWNS the union rather than a
            # channel that contributes to it, so it is not a mirror of anything.
            if not mod.ispkg or mod.name == "messaging":
                continue
            try:
                commands = importlib.import_module(f"kiro_crew.{mod.name}.commands")
            except ModuleNotFoundError:
                continue
            aliases: set[str] = set()
            # Shape 1: a private frozenset (discord, telegram, wecom).
            stop_set = getattr(commands, "_STOP_ALIASES", None)
            if stop_set is not None:
                aliases |= set(stop_set)
            # Shape 2: ``(canonical, aliases, description)`` rows (teams).
            for row in getattr(commands, "COMMAND_SPEC", ()) or ():
                if len(row) == 3 and row[0] == "stop" and isinstance(row[1], tuple):
                    aliases |= set(row[1])
            # Shape 3: dataclass rows carrying ``.name`` / ``.aliases`` (whatsapp).
            for row in getattr(commands, "COMMANDS", ()) or ():
                if getattr(row, "name", "") == "stop":
                    aliases |= set(getattr(row, "aliases", ()))
            if aliases:
                found[mod.name] = aliases
                continue
            # No stop spellings read. That is legitimate for a channel with no
            # cancel command at all, but suspicious if the module mentions one.
            source = getattr(commands, "__doc__", "") or ""
            if "/stop" in source or "/cancel" in source:
                unreadable.append(mod.name)

        assert not unreadable, (
            "channel command tables this tripwire could not parse, so their drift "
            f"is invisible to it: {unreadable}"
        )
        # The channels known to ship a cancel today. A channel dropping out of
        # this set means the discovery above silently stopped seeing it.
        assert {"discord", "telegram", "wecom", "teams", "whatsapp"} <= set(found), found

        union = set().union(*found.values())
        missing = union - D._CANCEL_ALIASES
        assert not missing, f"cancel spellings the shared exemption would gate: {missing}"
        # And the reverse: a spelling in the shared set that no channel accepts is
        # a governance exemption granted to a word nothing can act on.
        assert not D._CANCEL_ALIASES - union, D._CANCEL_ALIASES - union


class TestCancellationSurvivesAGovernanceDeny:
    """A denied channel must still be able to halt the session it started.

    ``max_buttons=0`` channels have no Reject button to press, so the typed cancel
    is the only cancel affordance there is: gating it strands a runaway turn with
    no way to stop it, which is the opposite of what a deny is for.
    """

    def test_a_pure_cancel_is_permitted_on_a_denied_channel(self, monkeypatch) -> None:
        _gate(monkeypatch, permitted=False)
        assert asyncio.run(D.inbound_permitted("whatsapp", text="/stop")) is True

    def test_an_ordinary_message_is_still_dropped(self, monkeypatch) -> None:
        """Non-vacuity: the deny must still deny everything that is not a cancel."""
        _gate(monkeypatch, permitted=False)
        assert asyncio.run(D.inbound_permitted("whatsapp", text="summarise my inbox")) is False

    def test_a_restart_is_not_a_cancellation(self, monkeypatch) -> None:
        _gate(monkeypatch, permitted=False)
        assert asyncio.run(D.inbound_permitted("whatsapp", text="/restart")) is False

    def test_an_attachment_bearing_cancel_is_gated(self, monkeypatch) -> None:
        """Otherwise the denied channel still pays for the download."""
        _gate(monkeypatch, permitted=False)
        assert (
            asyncio.run(D.inbound_permitted("whatsapp", text="/stop", has_attachments=True))
            is False
        )

    def test_the_argument_less_call_stays_strict(self, monkeypatch) -> None:
        """``drive_turn``'s backstop names no text, so nothing is exempt there."""
        _gate(monkeypatch, permitted=False)
        assert asyncio.run(D.inbound_permitted("whatsapp")) is False

    def test_a_permitted_channel_still_short_circuits(self, monkeypatch) -> None:
        stub = _gate(monkeypatch, permitted=True)
        assert asyncio.run(D.inbound_permitted("whatsapp", text="anything")) is True
        assert stub.asked == ["whatsapp"], "governance must be consulted first, once"


def test_the_origin_conversation_is_recorded_and_bound(monkeypatch) -> None:
    from kiro_crew.messaging.link import ChannelLink

    _patch_pipeline(monkeypatch)
    sessions = _MirrorSessions()
    turn = _turn(_Renderer())
    turn.origin_conversation = ChannelLink("weixin", channel_id="ROOM", thread_id=None)

    asyncio.run(drive_turn(turn, sessions=sessions, ctx_builder=_CtxBuilder()))

    assert sessions.origin_links[turn.session_key].channel_id == "ROOM"
    assert sessions.mirror_links[turn.session_key].channel_id == "ROOM"


def test_a_unified_key_records_no_origin_conversation(monkeypatch) -> None:
    """``dm_scope="unified"`` collapses every allowed user's DM into one bucket.

    So "the conversation this session is read in" has no single answer: recording
    one points the session's origin at whichever human spoke LAST, and a later
    notice (a cron result, a subagent completion) lands in that person's chat
    regardless of whose turn produced it. ``bind_origin_mirror`` already declines
    for exactly this reason, so the sibling ``set_origin_link`` must not be the
    hole that reopens it.
    """
    from kiro_crew.messaging.link import ChannelLink

    _patch_pipeline(monkeypatch)
    sessions = _MirrorSessions()
    turn = _turn(_Renderer())
    turn.session_key = "unified:agentA"
    turn.origin_conversation = ChannelLink("webex", channel_id="ROOM_A", thread_id=None)

    asyncio.run(drive_turn(turn, sessions=sessions, ctx_builder=_CtxBuilder()))

    assert sessions.origin_links == {}
    assert sessions.mirror_links == {}


def test_a_turn_that_omits_the_origin_conversation_binds_nothing(monkeypatch) -> None:
    _patch_pipeline(monkeypatch)
    sessions = _MirrorSessions()

    asyncio.run(drive_turn(_turn(_Renderer()), sessions=sessions, ctx_builder=_CtxBuilder()))

    assert sessions.origin_links == {}
    assert sessions.mirror_links == {}


def test_the_persisted_opt_out_is_honoured(monkeypatch) -> None:
    """An in-channel unlink has to survive the user's next message.

    The bind is re-asserted every turn, so without reading the opt-out "off"
    would last exactly until they typed again.
    """
    from kiro_crew.messaging.link import ChannelLink

    _patch_pipeline(monkeypatch)
    sessions = _MirrorSessions(opt_out=True)
    turn = _turn(_Renderer())
    turn.origin_conversation = ChannelLink("weixin", channel_id="ROOM", thread_id=None)

    asyncio.run(drive_turn(turn, sessions=sessions, ctx_builder=_CtxBuilder()))

    assert sessions.mirror_links == {}


def test_a_binding_aimed_elsewhere_is_not_repointed(monkeypatch) -> None:
    # The dashboard can aim a session's mirror at any surface; overwriting it
    # would silently redirect the user's replies into this conversation.
    from kiro_crew.messaging.link import ChannelLink

    _patch_pipeline(monkeypatch)
    elsewhere = ChannelLink("discord", channel_id="99", thread_id=None)
    sessions = _MirrorSessions(existing={"weixin:agentA:direct:userA": elsewhere})
    turn = _turn(_Renderer())
    turn.origin_conversation = ChannelLink("weixin", channel_id="ROOM", thread_id=None)

    asyncio.run(drive_turn(turn, sessions=sessions, ctx_builder=_CtxBuilder()))

    assert sessions.mirror_links["weixin:agentA:direct:userA"] is elsewhere


def test_a_bind_failure_does_not_drop_the_turn(monkeypatch) -> None:
    """This is the widest call site in the codebase — five channels route here.

    Losing the mirror costs a dashboard convenience; raising costs the user the
    answer they are waiting for.
    """
    from kiro_crew.messaging.link import ChannelLink

    _patch_pipeline(monkeypatch)
    sessions = _MirrorSessions(raises=True)
    turn = _turn(_Renderer())
    turn.origin_conversation = ChannelLink("weixin", channel_id="ROOM", thread_id=None)

    asyncio.run(drive_turn(turn, sessions=sessions, ctx_builder=_CtxBuilder()))

    assert sessions.successes == 1
    assert sessions.released == 1


class _KnownProviderSessions(_Sessions):
    """Returns an identifiable provider, so the hook's argument can be asserted."""

    def __init__(self) -> None:
        super().__init__()
        self.provider = object()

    async def get_or_create(self, key, agent=None, channel_id=None, **kw):
        return self.provider, False, False


def test_the_live_provider_is_handed_to_the_channel(monkeypatch) -> None:
    """A channel that uploads local files needs the provider's own cwd as the
    extraction root, and that is unknowable until ``get_or_create`` returns.

    Reading it from the session map BEFORE the turn yields ``None`` on the first
    message of every session generation, so the feature is silently off for
    exactly the turn that introduces it and mysteriously on afterwards.
    """
    seen: list = []
    _patch_pipeline(monkeypatch)
    sessions = _KnownProviderSessions()
    turn = _turn(_Renderer())
    turn.bind_provider = seen.append

    asyncio.run(drive_turn(turn, sessions=sessions, ctx_builder=_CtxBuilder()))

    assert seen == [sessions.provider]


def test_the_hook_runs_before_the_driver(monkeypatch) -> None:
    # Whatever it authorizes has to be in place for the turn it belongs to, not
    # the next one.
    order: list[str] = []
    _patch_pipeline(monkeypatch)

    class _OrderedDriver(_Driver):
        def __init__(self, *a, **kw) -> None:
            order.append("driver")
            super().__init__(*a, **kw)

    monkeypatch.setattr(D, "TurnDriver", _OrderedDriver)
    turn = _turn(_Renderer())
    turn.bind_provider = lambda _p: order.append("bind")

    asyncio.run(drive_turn(turn, sessions=_Sessions(), ctx_builder=_CtxBuilder()))

    assert order == ["bind", "driver"]


def test_a_failing_hook_degrades_the_feature_not_the_turn(monkeypatch) -> None:
    # Guarded like the origin bind: what it authorizes is an enhancement, so a
    # failure must not drop an answer the user is waiting for.
    _patch_pipeline(monkeypatch)
    sessions = _Sessions()
    turn = _turn(_Renderer())

    def _boom(_provider) -> None:
        raise RuntimeError("no cwd")

    turn.bind_provider = _boom

    asyncio.run(drive_turn(turn, sessions=sessions, ctx_builder=_CtxBuilder()))

    assert sessions.successes == 1
    assert sessions.released == 1


def test_a_turn_that_omits_the_hook_still_runs(monkeypatch) -> None:
    _patch_pipeline(monkeypatch)
    sessions = _Sessions()

    asyncio.run(drive_turn(_turn(_Renderer()), sessions=sessions, ctx_builder=_CtxBuilder()))

    assert sessions.successes == 1
