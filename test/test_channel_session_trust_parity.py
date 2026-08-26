"""One session-trust grant, five channels.

Webex, WeCom, iLink (weixin) and iMessage all render no approve/deny widget, so
each runs the shared pipeline with ``decider=None``. Under the default
INTERACTIVE approval mode that is deny-by-default with nothing able to say
otherwise: every tool call is rejected and the agent can only talk. Each
therefore also passes ``ChannelTurn.auto_approve_session``, reading the SAME
process-global ``safety_override`` grant the dashboard toggle and Slack's
``/kirocrew yolo`` drive -- so an operator who arms it anywhere gets tool use on
these channels too, and it still expires.

Teams is enrolled too even though it DOES have a widget (:data:`_WIDGET_CHANNELS`).
It passes the SAME predicate as the four -- it keeps no grant of its own, so its
card button and its ``/yolo`` both arm this one shared grant -- and its card only
changes what happens with NO grant: a prompt is posted and awaited instead of an
immediate refusal. Enrolling it is the point: nothing else drives a real permission
request through Teams' ``handle_message``, so both ``decider=`` and
``auto_approve_session=`` could be deleted from its ``ChannelTurn`` with every
Teams test still green.

One parameterized suite rather than five near-copies, because the property is
identical in all five and a per-channel copy is how one of them silently loses
the wiring. Every case drives that channel's real ``handle_message`` through the
real ``drive_turn`` and the real ``TurnDriver``, so what is pinned is the whole
path rather than the presence of a keyword:

* with no grant the tool is still rejected -- immediately on the four buttonless
  channels, and after the card's deadline passes on Teams -- and a widget is
  offered ONLY where a press can resolve one,
* with the grant armed the same tool is approved with no widget and no decider,
* the predicate is re-read per tool rather than snapshotted when the turn was
  built, and
* a hard PreToolUse deny still wins, because the security gate runs AHEAD of
  this rung.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST, AcpEvent
from kiro_crew.hooks import TOOL_DENY
from kiro_crew.messaging.renderer import Renderer, TransportCapabilities
from kiro_crew.safety_override import safety_override

# ── Channel-neutral doubles ───────────────────────────────────────────────────


class _Renderer(Renderer):
    """Stands in for whichever renderer the channel under test constructs.

    Substituted for the renderer CLASS in the dispatcher's own module namespace,
    so ``__init__`` has to swallow whatever positional and keyword arguments that
    channel passes it. The subject here is the approval ladder, not any
    transport surface, and this keeps one set of doubles serving all four.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(TransportCapabilities())
        self.prompts: list[Any] = []

    async def on_text_chunk(self, text: str) -> None:
        return None

    async def on_thinking(self, text: str) -> None:
        return None

    async def on_tool_call(
        self, tool_call_id: str, title: str, tool_kind: str = "", tool_purpose: str = ""
    ) -> None:
        return None

    async def on_prompt_choice(
        self,
        options: list[dict[str, Any]],
        request_id: str | int,
        tool_title: str = "",
        tool_purpose: str = "",
        tool_input: str = "",
    ) -> None:
        # Recorded because a widget offered on a decider-less channel is a dead
        # control: the ladder denies whatever the user presses. The two tool
        # fields ride the PROMPT_CHOICE event so a renderer can name the tool the
        # request actually asks about; this fake ignores them but must accept them.
        self.prompts.append(request_id)

    async def on_compaction(self, context_usage_pct: float) -> None:
        return None

    async def on_done(self, stop_reason: str = "") -> None:
        return None

    @property
    def has_pending_choices(self) -> bool:
        """Teams reads this to decide whether the renderer outlives its turn.

        False here: this suite drives the APPROVAL rung, which resolves inside the
        turn, never an ``[OPTIONS:]`` chip that is tapped afterwards.
        """
        return False


class _Provider:
    """Scripted provider: one permission request, then completion."""

    supports_steer = False

    def __init__(self) -> None:
        self.approved: list[Any] = []
        self.rejected: list[Any] = []

    async def stream(self, message: str) -> Any:
        yield AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id="rq1", options=[{"id": "approve"}])
        yield AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

    async def approve_tool(self, request_id: Any, *, always: bool = False) -> None:
        self.approved.append(request_id)

    async def reject_tool(self, request_id: Any) -> None:
        self.rejected.append(request_id)


class _Sessions:
    """The slice of ``SessionManager`` the shared pipeline touches."""

    def __init__(self, provider: _Provider) -> None:
        self._p = provider
        self.released: list[str] = []

    async def get_or_create(self, key: str, *, agent: Any = None, channel_id: Any = None) -> Any:
        # is_new=False deliberately: it keeps the turn off the dashboard-surfacing
        # and set_channel paths, neither of which this suite is about.
        return self._p, False, False

    async def set_channel(self, key: str, channel_id: str) -> None:
        return None

    def record_success(self, key: str) -> None:
        return None

    async def record_failure(self, key: str) -> None:
        return None

    def release(self, key: str) -> None:
        self.released.append(key)

    def get_provider(self, key: str) -> Any:
        return self._p

    async def aflush(self) -> None:
        # The resume release flushes the session map before it reports success; a
        # double without this correctly surfaces as a release FAILURE.
        return None

    def clear_mirror_links_at(self, link, *, reason: str = "") -> list:
        return []

    def find_mirror_sessions(self, link, *, inbound_only: bool = False) -> list:
        # No resumed dashboard session in these tests, so routing is a no-op. Present
        # because Teams routes EVERY message through the resume resolver.
        return []

    def is_busy(self, key: str) -> bool:
        return False

    def has_session(self, key: str) -> bool:
        return True

    def check_context_usage(self, key: str, provider: Any) -> float:
        # Below every channel's soft threshold, so no post-turn notice fires and
        # no out-of-band client call is needed.
        return 0.0

    def max_generation(self, bucket: str) -> int:
        return -1

    def get_pid(self, key: str) -> None:
        return None

    def is_mirror_paused(self, key: str, *, origin: bool = False) -> bool:
        return False

    # Teams binds the origin dashboard mirror on a conversation's first turn, which
    # reads and writes this slice. The other four never touch it.
    def mirror_opt_out(self, key: str) -> bool:
        return False

    def get_mirror_link(self, key: str) -> Any:
        return None

    def set_mirror_link(self, key: str, link: Any, *, reason: str = "") -> None:
        return None

    def batched_save(self) -> Any:
        return contextlib.nullcontext()

    def dequeue(self, key: str) -> None:
        # Teams drains the mid-turn queue at the tail of every turn; nothing was
        # queued here, so the drain finds an empty queue and returns.
        return None


class _Hooks:
    """PreToolUse gate that passes everything through."""

    auto_approve_subagent_spawn = False

    def on_tool_call(self, title: str, **kw: Any) -> Any:
        return SimpleNamespace(action="")


class _DenyHooks(_Hooks):
    """PreToolUse gate returning the un-overridable hard deny."""

    def on_tool_call(self, title: str, **kw: Any) -> Any:
        return SimpleNamespace(action=TOOL_DENY)


class _Ctx:
    def __init__(self, hooks: Any = None) -> None:
        self.hooks = hooks or _Hooks()

    def build_message(
        self,
        text: str,
        is_new: bool,
        key: str,
        *,
        channel_id: str,
        agent: str,
        resumed: bool,
        runtime_source: str,
        **kw: Any,
    ) -> tuple[str, None]:
        # `**kw` so a new field on the shared seam (minimal_context, and whatever
        # follows it) does not break this fake. The pipeline forwards every
        # ChannelTurn field unconditionally, which is what makes "absent" and
        # "False" distinguishable there; spelling each one here would make this
        # file fail again on the next addition.
        return (text, None)


class _Client:
    """Every attribute resolves to an async no-op.

    The renderer is replaced, so the only client calls left on this path would be
    out-of-band notices, and the configured thresholds never fire.
    """

    def __getattr__(self, name: str) -> Any:
        async def _noop(*args: Any, **kwargs: Any) -> None:
            return None

        return _noop


class _CtxStore:
    """iLink's context-token store: consulted synchronously by ``_say``."""

    def get(self, account_id: str, user_id: str) -> str:
        return ""


def _cfg(channel: str) -> SimpleNamespace:
    """Config carrying only what these dispatchers read off ``self.cfg``."""
    cfg = SimpleNamespace(
        agent=SimpleNamespace(default_agent="", approval_mode="interactive"),
        messaging=SimpleNamespace(
            dm_scope="per-channel-peer",
            idle_reset_minutes=0,
            daily_reset_hour=-1,
            queue_mode="steer",
        ),
    )
    setattr(
        cfg,
        channel,
        SimpleNamespace(
            hard_threshold_pct=95.0,
            soft_threshold_pct=80.0,
            # Webex threads its replies, so its dispatcher reads this on every send.
            # Harmless for the channels that do not: an attribute nobody looks at.
            reply_in_thread=True,
            # Read by the Webex card-press path when authorizing a sender.
            allowed_emails=["kyle@example.com"],
        ),
    )
    return cfg


# ── Per-channel construction (the only channel-specific code here) ────────────
#
# Each builder returns the dispatcher's DEFINING module (the namespace whose
# renderer name has to be patched -- a package re-export would be a silent
# no-op), that renderer attribute's name, the dispatcher, and one inbound
# message carrying plain text so the command intercepts are not taken.


def _build_webex(sessions: Any, ctx: Any) -> tuple[Any, str, Any, Any]:
    from kiro_crew.webex import transport_dispatch as mod
    from kiro_crew.webex.client import WebexInbound

    d = mod.WebexDispatcher(
        sessions=sessions,
        ctx_builder=ctx,
        cfg=_cfg("webex"),
        agent=None,
        conv_log=None,
        approval_mode="interactive",
    )
    d.client = _Client()
    inbound = WebexInbound(
        person_email="kyle@example.com", room_id="ROOM", text="hi", room_type="direct"
    )
    return mod, "WebexRenderer", d, inbound


def _build_wecom(sessions: Any, ctx: Any) -> tuple[Any, str, Any, Any]:
    from kiro_crew.wecom import transport_dispatch as mod
    from kiro_crew.wecom.client import WeComInbound

    d = mod.WeComDispatcher(
        sessions=sessions,
        ctx_builder=ctx,
        cfg=_cfg("wecom"),
        owner_id="",
        agent=None,
        conv_log=None,
        approval_mode="interactive",
    )
    d.client = _Client()
    inbound = WeComInbound(
        userid="u1", text="hi", response_url="https://example.invalid/reply", req_id="RQ"
    )
    return mod, "WeComRenderer", d, inbound


def _build_weixin(sessions: Any, ctx: Any) -> tuple[Any, str, Any, Any]:
    from kiro_crew.messaging.transport import InboundMessage
    from kiro_crew.weixin import transport_dispatch as mod

    d = mod.WeixinDispatcher(
        sessions=sessions,
        ctx_builder=ctx,
        cfg=_cfg("weixin"),
        account_id="acct",
        ctx_store=_CtxStore(),
        typing_cache=None,
        agent=None,
        conv_log=None,
        approval_mode="interactive",
    )
    d.client = _Client()
    inbound = InboundMessage(
        channel_type="weixin", user_id="u1", conversation_id="weixin:u1", text="hi"
    )
    return mod, "WeixinRenderer", d, inbound


def _build_imessage(sessions: Any, ctx: Any) -> tuple[Any, str, Any, Any]:
    from kiro_crew.imessage import transport_dispatch as mod
    from kiro_crew.imessage.client import IMessageInbound

    d = mod.IMessageDispatcher(
        sessions=sessions,
        ctx_builder=ctx,
        cfg=_cfg("imessage"),
        agent=None,
        conv_log=None,
        approval_mode="interactive",
    )
    d.client = _Client()
    inbound = IMessageInbound(handle="+15550100", text="hi", chat_guid="iMessage;-;+15550100")
    return mod, "IMessageRenderer", d, inbound


def _build_teams(sessions: Any, ctx: Any) -> tuple[Any, str, Any, Any]:
    from kiro_crew.teams import transport_dispatch as mod
    from kiro_crew.teams.client import TeamsInbound

    d = mod.TeamsDispatcher(
        sessions=sessions,
        ctx_builder=ctx,
        cfg=_cfg("teams"),
        agent=None,
        conv_log=None,
        approval_mode="interactive",
    )
    d.client = _Client()
    inbound = TeamsInbound(
        conversation_id="CONV",
        conversation_type="personal",
        service_url="https://smba.trafficmanager.net/",
        text="hi",
        user_email="kyle@example.com",
        activity_id="act-1",
    )
    return mod, "TeamsRenderer", d, inbound


_CHANNELS: dict[str, Any] = {
    "imessage": _build_imessage,
    "teams": _build_teams,
    "webex": _build_webex,
    "wecom": _build_wecom,
    "weixin": _build_weixin,
}

#: Channels that pass a real ``decider``, so a prompt IS clickable there. Their
#: no-grant case ends in a refusal too -- the decider denies by default -- but only
#: once its deadline passes, so these tests shorten it rather than waiting.
_WIDGET_CHANNELS = frozenset({"teams", "webex"})

#: Where each widget channel's click deadline lives, so the deny-by-default path
#: can resolve immediately instead of waiting one out. Read per call inside the
#: decider, so patching the module attribute is enough.
_APPROVAL_DEADLINE_SYMBOL = {
    "teams": "kiro_crew.teams.approvals.APPROVAL_TIMEOUT_SECS",
    "webex": "kiro_crew.messaging.approval.APPROVAL_TIMEOUT_S",
}


def _run_turn(
    channel: str, monkeypatch: pytest.MonkeyPatch, *, hooks: Any = None
) -> tuple[_Provider, _Renderer]:
    """Drive one real turn for *channel*; return its provider and renderer."""
    if channel in _WIDGET_CHANNELS:
        # Collapse the click deadline so the deny-by-default path resolves now.
        monkeypatch.setattr(_APPROVAL_DEADLINE_SYMBOL[channel], 0.01)
    provider = _Provider()
    mod, renderer_attr, dispatcher, inbound = _CHANNELS[channel](_Sessions(provider), _Ctx(hooks))
    rendered: list[_Renderer] = []

    def _make_renderer(*args: Any, **kwargs: Any) -> _Renderer:
        r = _Renderer()
        rendered.append(r)
        return r

    monkeypatch.setattr(mod, renderer_attr, _make_renderer)
    asyncio.run(dispatcher.handle_message(inbound))
    assert rendered, f"{channel} never constructed a renderer, so no turn ran"
    return provider, rendered[0]


# ── The shared property, one case per channel ─────────────────────────────────


@pytest.mark.parametrize("channel", sorted(_CHANNELS))
def test_no_grant_still_denies_every_tool(channel: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deny-by-default is what enrolling these channels must NOT have changed.

    The grant starts inactive (the singleton is reset around every test), so the
    predicate answers False and the INTERACTIVE ladder has no decider to fall
    through to: the tool is rejected. No widget is offered either, because on a
    channel with no buttons that would be a dead control.
    """
    assert safety_override().is_active() is False, "precondition: no grant is armed"
    provider, renderer = _run_turn(channel, monkeypatch)
    assert provider.rejected == ["rq1"]
    assert provider.approved == []
    if channel in _WIDGET_CHANNELS:
        assert renderer.prompts == ["rq1"], "a clickable channel must offer the choice"
    else:
        assert renderer.prompts == []


@pytest.mark.parametrize("channel", sorted(_CHANNELS))
def test_an_armed_grant_reaches_the_driver_and_approves(
    channel: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wiring, end to end.

    ``ChannelTurn.auto_approve_session`` has to carry the predicate all the way
    into ``TurnDriver`` for the grant to mean anything, so the same tool the case
    above rejected is approved here -- with no widget and no decider wait. A
    channel that omits the keyword fails exactly this half, which is the state
    all four were in.
    """
    safety_override().activate("test")
    assert safety_override().is_active() is True, "precondition: the grant is armed"
    provider, renderer = _run_turn(channel, monkeypatch)
    assert provider.approved == ["rq1"]
    assert provider.rejected == []
    # Even where a card exists, an armed grant must short-circuit ABOVE it: posting
    # a prompt the ladder has already decided is a control that does nothing.
    assert renderer.prompts == []


@pytest.mark.parametrize("channel", sorted(_CHANNELS))
def test_the_grant_is_read_per_tool_not_snapshotted(
    channel: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A snapshot would make arming the grant take effect only on the NEXT
    message, and letting it lapse take effect never. Mutating the grant after the
    turn has already been built is what proves the closure re-reads it.
    """
    from kiro_crew.messaging import dispatch as D

    captured: list[Any] = []

    class _Recorder:
        def __init__(self, provider: Any, renderer: Any, **kw: Any) -> None:
            captured.append(kw.get("auto_approve_session"))

        async def run(self, message: str) -> str:
            return "ok"

    monkeypatch.setattr(D, "TurnDriver", _Recorder)
    _run_turn(channel, monkeypatch)

    assert captured, "the pipeline never constructed a driver"
    predicate = captured[0]
    assert predicate is not None, f"{channel} passed no auto_approve_session"
    assert predicate() is False
    safety_override().activate("test")
    assert predicate() is True, "the predicate must re-read the grant, not cache it"


@pytest.mark.parametrize("channel", sorted(_CHANNELS))
def test_a_pretooluse_deny_still_wins_over_an_armed_grant(
    channel: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The grant is a rung BELOW the security gate, never a way past it.

    ``TurnDriver`` runs the ``tool_gate`` -- the sensitive-path keystone, the
    governance ceiling and the deny-list, built off ``ctx_builder.hooks`` -- and
    honours its hard deny BEFORE it consults ``auto_approve_session``. That
    ordering is what makes enrolling a buttonless channel safe, so it is pinned
    per channel here rather than left to the driver's own suite.
    """
    safety_override().activate("test")
    provider, _renderer = _run_turn(channel, monkeypatch, hooks=_DenyHooks())
    assert provider.rejected == ["rq1"]
    assert provider.approved == []
