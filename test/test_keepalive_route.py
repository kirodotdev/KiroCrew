"""A Kiro Crew call is timed only on a session handle ``/api/session-keepalive`` reaches.

The route looks ``X-Session-Key`` up in the registry the gateway's ``DashboardState``
holds and hands a Kiro Crew tool's heartbeat to the provider it finds there, which
is the only way such a call proves it is alive (``kiro_crew.keepalive_route``). These
tests pin each link: the registry the route reads marks every provider it publishes
and a registry no route reads marks none; the mark and the heartbeat both reach the
handle through the provider that registry holds (an ``AcpProvider`` over an
``AcpSessionProvider``); and ``kirocrew chat``, whose provider no such registry
holds, does not time its Kiro Crew calls, so a ``spawn_sub_agents`` whose children
are still working is not cut there, while the same provider published by the
gateway's registry keeps the timer.

The clock is the tick harness's (``acp_tool_silence_helpers``): every instant is exact.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from acp_tool_silence_helpers import (
    _BOUND,
    _TICK,
    _TURN,
    _at,
    _Clock,
    _tool_call,
    _Turn,
    _update,
    install_tick_clock,
)
from aiohttp import web

from kiro_crew import cli_chat, keepalive_route
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import STOP_REASON_END_TURN, JsonRpcMessage
from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard.handlers.sessions import api_session_keepalive
from kiro_crew.dashboard.handlers.sessions_parts import live_turn
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.session import SessionManager

#: ``_chat`` builds a ``SkillsLoader``, whose index and catalog worker outlive the test.
pytestmark = pytest.mark.usefixtures("close_skills_loaders")

_GATEWAY_KEY = "dashboard:main"
_SPAWN = _tool_call(
    "tc-ssa",
    kind="other",
    kiro={"toolName": "spawn_sub_agents", "mcpServerName": "kirocrew-core"},
    raw={"agents": [{"task": "build"}]},
)
#: The children finish here, long past the sub-agent bound (16 ticks).
_CHILDREN_DONE = 100


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    return install_tick_clock(monkeypatch)


def _end_turn(request_id: int = 1) -> JsonRpcMessage:
    return JsonRpcMessage(id=request_id, result={"stopReason": "end_turn"})


def _children_done() -> JsonRpcMessage:
    return _update(
        {"sessionUpdate": "tool_call_update", "toolCallId": "tc-ssa", "status": "completed"}
    )


def _text(text: str) -> JsonRpcMessage:
    return _update(
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}
    )


# ── The registry the route reads marks what it publishes ──


def _registry_provider(session_key: str | None = None, **kwargs: Any) -> MagicMock:
    """A kiro-backed provider double for a real ``SessionManager``; no process runs."""
    provider = MagicMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.memory_mode = kwargs.get("memory_mode", "persistent")
    provider.is_process_alive = lambda: True
    provider.has_active_turn = lambda: False
    provider.runtime_abort_target = lambda: None
    provider.uses_kiro_identity_store = True
    return provider


@pytest.mark.asyncio
async def test_the_registry_the_keepalive_route_reads_marks_every_provider_it_publishes(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The gateway hands its ``SessionManager`` to ``DashboardState``, whose ``sessions``
    the route reads: a provider published before that is marked then, one published
    after it as it is published. A registry no ``DashboardState`` holds (``kirocrew
    run`` and ``kirocrew eval`` build their own) marks nothing."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    gateway = SessionManager(KiroCrewConfig(), provider_factory=_registry_provider)
    own = SessionManager(KiroCrewConfig(), provider_factory=_registry_provider)
    try:
        early, _new, _resumed = await gateway.get_or_create("cron:early")
        assert not early.mark_keepalive_routed.called

        DashboardState(sessions=gateway, crons=MagicMock(), lessons=MagicMock(), start_time=0.0)

        assert early.mark_keepalive_routed.called
        later, _new, _resumed = await gateway.get_or_create(_GATEWAY_KEY)
        assert later.mark_keepalive_routed.called
        unreached, _new, _resumed = await own.get_or_create("taskrunner:main")
        assert not unreached.mark_keepalive_routed.called
    finally:
        await gateway.close_all()
        await own.close_all()


# ── The mark and the heartbeat reach the handle through the provider the route finds ──


class _Registry:
    """The gateway's registry as the route reads it, holding *provider* under *key*."""

    def __init__(self, key: str, provider: object) -> None:
        self._key = key
        self._provider = provider

    def session_keys(self) -> frozenset[str]:
        return frozenset({self._key})

    def get_provider(self, key: str) -> object:
        return self._provider if key == self._key else None


def _acp_provider_over(handle: Any, session_key: str) -> AcpProvider:
    """A kiro session's provider once started: the ``AcpProvider`` a registry holds,
    its live client the ``AcpSessionProvider`` over *handle*."""
    provider = AcpProvider.__new__(AcpProvider)
    provider._keepalive_routed = False
    provider._client = AcpSessionProvider(handle, handle._runtime, session_key=session_key)
    return provider


@pytest.mark.parametrize("surface", ["gateway", "cli-chat"])
@pytest.mark.asyncio
async def test_a_heartbeat_reaches_the_timer_only_through_the_registry_the_route_reads(
    clock, surface
) -> None:
    """A ``spawn_sub_agents`` whose children work for 100 ticks, past its bound. Its
    tool sends a keepalive every 8 ticks, and the route delivers it to the provider
    the gateway's registry holds under the key it names.

    * gateway: the registry holds this session's ``AcpProvider``; the registry's mark
      and every beat reach the handle through it, and the call is not cut.
    * cli-chat: the session is ``kirocrew chat``'s, which that registry does not
      hold, so neither the mark nor any beat arrives, and the call is not judged."""
    key = _GATEWAY_KEY if surface == "gateway" else cli_chat._CLI_SESSION_KEY
    delivered: list[int] = []

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_SPAWN)]
        if tick % 8 == 0 and tick < _CHILDREN_DONE:
            found = gateway.get_provider(key)
            if found is not None:
                body = {"tool_call_id": "tc-ssa", "phase": "poll"}
                live_turn._note_tool_heartbeat(SimpleNamespace(), key, found, body)
                delivered.append(tick)
        if tick == _CHILDREN_DONE:
            return [_children_done()]
        if tick == _CHILDREN_DONE + 1:
            return [_end_turn()]
        return []

    turn = _Turn(clock, script, keepalive_routed=None)
    provider = _acp_provider_over(turn.handle, key)
    gateway = _Registry(_GATEWAY_KEY, provider if surface == "gateway" else None)
    keepalive_route.serve_keepalive_route(gateway)

    assert turn.handle.keepalive_routed is (surface == "gateway")
    events = await turn.prompt()

    assert events[-1].stop_reason == STOP_REASON_END_TURN
    assert turn.cancelled_at is None
    assert bool(delivered) is (surface == "gateway")
    assert turn.handle._tool_tracker.states() == []


@pytest.mark.asyncio
async def test_the_route_hands_a_beat_to_the_handle_and_answers_404_for_kirocrew_chat() -> None:
    """The real route over the gateway's registry: the session it holds gets the
    beat through its ``AcpProvider``; ``kirocrew chat``'s key finds nothing."""
    handle = MagicMock()
    handle._runtime = MagicMock()
    handle.note_tool_heartbeat = MagicMock(return_value=1)
    provider = _acp_provider_over(handle, _GATEWAY_KEY)
    state = MagicMock()
    state.sessions = _Registry(_GATEWAY_KEY, provider)

    statuses = []
    for key in (_GATEWAY_KEY, cli_chat._CLI_SESSION_KEY):
        req = MagicMock(spec=web.Request)
        req.headers = {"X-Session-Key": key}
        req.app = {"state": state}
        req.can_read_body = True
        req.json = AsyncMock(return_value={"tool_call_id": "tc-ssa", "phase": "poll"})
        statuses.append((await api_session_keepalive(req)).status)

    assert statuses == [200, 404]
    handle.note_tool_heartbeat.assert_called_once_with("tc-ssa", activity_at=None)


# ── kirocrew chat, through its own provider path ──


@pytest.mark.parametrize("hosted", ["kirocrew-chat", "published-by-the-gateway"])
@pytest.mark.asyncio
async def test_kirocrew_chat_does_not_cut_a_spawn_whose_children_still_work(
    clock, monkeypatch: pytest.MonkeyPatch, capsys, hosted
) -> None:
    """``kirocrew chat -m`` through ``_chat``, ``_send_and_print`` and the timed-out
    continuation, with the provider the real factory builds for it. The model
    calls ``spawn_sub_agents``; the children work for 100 ticks and nothing
    reaches the CLI chat's process meanwhile. Its start is stood in for: the live
    client becomes the ``AcpSessionProvider`` over the tick turn's handle, marked
    only when the provider was, as the runtime start does.

    * kirocrew-chat: nothing marks the provider, so the call is not judged by its
      silence; the turn ends as the children do and the model's answer prints.
    * published-by-the-gateway: the same provider, as the gateway's registry
      would publish it, keeps the timer: the call is cut one bound after its
      dispatch and handed back to the model, whose answer to the timeout prints."""
    turn2_from: list[int] = []

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if turn2_from:
            at = tick - turn2_from[0]
            return [_text("retrying the build")] if at == 1 else [_end_turn(2)] if at == 2 else []
        if tick == 1:
            return [_update(_SPAWN)]
        if tick == _CHILDREN_DONE:
            return [_children_done()]
        if tick == _CHILDREN_DONE + 1:
            return [_text("the build is done")]
        if tick == _CHILDREN_DONE + 2:
            return [_end_turn()]
        return []

    turn = _Turn(clock, script, keepalive_routed=None)
    sent: list[str] = []

    async def stream(message: str, *_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        if sent:
            turn2_from.append(turn.queue.tick + 1)
        sent.append(message)
        turn.handle._turn_done.set()
        async for event in turn.handle.prompt(message, timeout=_TURN):  # flake-ok: tick clock
            yield event

    built: list[Any] = []
    real_factory = cli_chat.build_provider_factory

    def factory(cfg: KiroCrewConfig) -> Any:
        make = real_factory(cfg)

        def build(*args: Any, **kwargs: Any) -> Any:
            provider = make(*args, **kwargs)
            built.append(provider)
            if hosted == "published-by-the-gateway":
                keepalive_route.serve_keepalive_route(_Registry(args[0], provider))

            async def start() -> None:
                inner = AcpSessionProvider(turn.handle, turn.handle._runtime, session_key=args[0])
                if provider._keepalive_routed:
                    inner.mark_keepalive_routed()
                provider._client = inner

            provider.start = start
            provider.shutdown = AsyncMock()
            provider.stream = stream
            return provider

        return build

    monkeypatch.setattr(cli_chat.KiroCrewConfig, "load", classmethod(lambda cls: KiroCrewConfig()))
    monkeypatch.setattr(cli_chat, "build_provider_factory", factory)

    await cli_chat._chat("build it", None)

    out = capsys.readouterr().out
    assert [type(provider) for provider in built] == [AcpProvider]
    if hosted == "kirocrew-chat":
        assert built[0]._keepalive_routed is False
        assert turn.handle.keepalive_routed is False
        assert turn.cancelled_at is None
        assert len(sent) == 1
        assert "the build is done" in out
    else:
        assert turn.handle.keepalive_routed is True
        assert turn.cancelled_at == _at(1) + _BOUND + _TICK
        assert len(sent) == 2
        assert "retrying the build" in out
        assert "the build is done" not in out
