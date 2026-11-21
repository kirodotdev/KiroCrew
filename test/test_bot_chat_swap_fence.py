"""A Bot-chat app stop sees every new native session of the chat as a start in flight.

Every way a chat gets a new native session goes through one record,
``bot_chat_ending_fence.recorded_session_start``, which wraps the process owners'
only session-creating methods: ``AcpRuntime.create_session`` and
``AcpRuntime.load_session`` (cold start, in-place swap, Edit & Resend, the rewind,
an MCP re-injection's evict-then-load) and ``AcpClient._initialize_session``. A
cold start is also held by ``SessionAllocationService.spawn_in_flight``.

Each test names the change that turns it red. The recorded start is held on an
event, so the stop lands inside it without a sleep.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from session_helpers import (  # noqa: F401 -- the fixture is requested by name
    _mock_provider_factory,
    cfg_fixture,
)

from kiro_crew.bot_chat_ending_fence import (
    BotChatStopping,
    discard_with_ending_fence,
    runtime_session_start,
)
from kiro_crew.session import SessionManager

KEY = "dashboard:bot-chat-swap"
SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"


async def _bounded(awaitable):
    """Every handshake and join: a ceiling at the wait-discipline floor, which only
    names a hang, so a broken handshake fails the test instead of the worker."""
    return await asyncio.wait_for(awaitable, timeout=30)


async def _end(task: asyncio.Task) -> None:
    """Cancel *task* if a failed assertion left it waiting, and reap it."""
    if not task.done():
        task.cancel()
    try:
        await _bounded(task)
    except (asyncio.CancelledError, Exception):
        pass


class _Runtime:
    """A process owner whose one session-creating method carries the real record."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.started = 0

    @runtime_session_start
    async def create_session(self, *, session_key: str = ""):
        self.started += 1
        self.entered.set()
        await _bounded(self.release.wait())  # session/new, held
        return SimpleNamespace(session_id="fresh-sid")

    load_session = create_session  # the MCP re-injection's evict-then-load


async def _setup(cfg):
    mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
    provider, _, _ = await _bounded(mgr.get_or_create(KEY))
    mgr.release(mgr._fold_key(KEY))
    provider.swaps_conversation_in_place = True
    provider.session_id = "old-sid"
    runtime = _Runtime()
    folded = mgr._fold_key(KEY)

    async def _new_conversation() -> None:
        handle = await runtime.create_session(session_key=folded)
        provider.session_id = handle.session_id

    provider.new_conversation = AsyncMock(side_effect=_new_conversation)
    return mgr, provider, runtime


@pytest.mark.asyncio
async def test_a_stop_inside_a_swap_is_unconfirmed_and_the_replacement_is_kept(cfg):
    """Drop the start check from ``discard_with_ending_fence`` and the stop reads
    confirmed; drop the outrun branch from ``recorded_session_start`` and the
    replacement carrying the app's servers is never registered for its close."""
    mgr, _provider, runtime = await _setup(cfg)
    recorded: list[tuple] = []
    with patch(
        "kiro_crew.bot_chat_app_tools.note_unconfirmed_close",
        side_effect=lambda key, rt, sid: recorded.append((key, rt, sid)),
    ):
        swap = asyncio.create_task(mgr.swap_conversation(KEY))
        try:
            await _bounded(runtime.entered.wait())
            _confirmed, in_flight = await _bounded(discard_with_ending_fence(mgr, KEY, []))
            runtime.release.set()
            await _bounded(swap)
        finally:
            runtime.release.set()
            await _end(swap)

    assert in_flight is not None and "new native session" in in_flight
    assert (mgr._fold_key(KEY), runtime, "fresh-sid") in recorded


@pytest.mark.asyncio
async def test_an_mcp_reinjection_load_under_a_stop_is_unconfirmed_and_kept(cfg):
    """The re-injection evicts the session and loads it again through
    ``load_session``. Leave that load out of the record and the stop, finding the
    session evicted, reports success while the load brings the app's server back."""
    mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
    await _bounded(mgr.get_or_create(KEY))
    mgr.release(mgr._fold_key(KEY))
    runtime = _Runtime()
    recorded: list[tuple] = []
    with patch(
        "kiro_crew.bot_chat_app_tools.note_unconfirmed_close",
        side_effect=lambda key, rt, sid: recorded.append((key, rt, sid)),
    ):
        load = asyncio.create_task(runtime.load_session(session_key=mgr._fold_key(KEY)))
        try:
            await _bounded(runtime.entered.wait())
            confirmed, in_flight = await _bounded(discard_with_ending_fence(mgr, KEY, []))
            runtime.release.set()
            await _bounded(load)
        finally:
            runtime.release.set()
            await _end(load)
    assert in_flight is not None and "new native session" in in_flight
    assert recorded == [(mgr._fold_key(KEY), runtime, "fresh-sid")]


@pytest.mark.asyncio
async def test_a_start_is_refused_while_a_stop_runs(cfg):
    """Let the record admit a start under a running stop and the fresh session
    starts on the spec the stop is withdrawing; the swap falls back instead."""
    mgr, _provider, runtime = await _setup(cfg)
    runtime.release.set()
    discarding = asyncio.Event()
    finish = asyncio.Event()
    real_discard = mgr.discard_conversation

    async def _held_discard(key, **kw):
        discarding.set()
        await _bounded(finish.wait())
        return await _bounded(real_discard(key, **kw))

    mgr.discard_conversation = _held_discard  # type: ignore[method-assign]
    stop = asyncio.create_task(discard_with_ending_fence(mgr, KEY, []))
    try:
        await _bounded(discarding.wait())
        assert await _bounded(mgr.swap_conversation(KEY)) == "unavailable"
        with pytest.raises(BotChatStopping):
            await _bounded(runtime.load_session(session_key=mgr._fold_key(KEY)))
        finish.set()
        await _bounded(stop)
    finally:
        finish.set()
        await _end(stop)
    assert runtime.started == 0


@pytest.mark.asyncio
async def test_a_swap_with_no_stop_beside_it_swaps(cfg):
    mgr, provider, runtime = await _setup(cfg)
    runtime.release.set()
    assert await _bounded(mgr.swap_conversation(KEY)) == "swapped"
    assert provider.session_id == "fresh-sid"


@pytest.mark.asyncio
async def test_a_stop_while_a_start_projects_its_replacement_still_finds_the_chat(cfg):
    """The replacement's projection runs under the stop's fence, so its record
    withholds the app. Select only by that record and the stop finds no Bot with
    the app, reports success and discards nothing, while the session being
    replaced still runs the app's server."""
    from kiro_crew import bot_chat_app_tools as tools

    mgr, _provider, runtime = await _setup(cfg)
    folded = mgr._fold_key(KEY)
    slug, app = "atlas", "team-app"
    tools._PROJECTION_SLUGS[folded] = slug
    tools._OFFERED[slug] = ({f"{app}:srv": "srv"}, {})
    tools._MOUNTED[slug] = {f"{app}:srv": "srv"}
    tools._MOUNTED_SESSION_KEYS[slug] = folded
    swap = asyncio.create_task(mgr.swap_conversation(KEY))
    try:
        await _bounded(runtime.entered.wait())  # the replacement's session/new is in flight
        async with tools.app_stop_fence(app):
            tools.record_mounted(folded, [f"{app}:srv"])  # the replacement's projection
            assert tools.app_has_active_bot_sessions(app)
            _stopped, failed = await _bounded(
                tools.stop_app_bot_chat_servers(SimpleNamespace(sessions=mgr), app)
            )
        assert slug in failed
        runtime.release.set()
        await _bounded(swap)
    finally:
        runtime.release.set()
        await _end(swap)
        tools.cancel_pending_close_timers()


def _session_starting_methods() -> dict[str, list[str]]:
    """``{module: [function]}`` for every method named like a native-session start."""
    found: dict[str, list[str]] = {}
    for path in (SRC / "acp").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.AsyncFunctionDef) and node.name in {
                "create_session",
                "load_session",
                "_initialize_session",
            }:
                found.setdefault(path.relative_to(SRC).as_posix(), []).append(node.name)
    return found


def test_every_native_session_start_goes_through_the_one_record():
    """A new way to create or load a native session would start one the stop
    cannot see: it must be one of these, each carrying the record."""
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.acp.runtime import AcpRuntime

    assert _session_starting_methods() == {
        "acp/runtime_session_create.py": ["create_session"],
        "acp/runtime_session_load.py": ["load_session"],
        "acp/client_start.py": ["_initialize_session"],
    }
    for method in (
        AcpRuntime.create_session,
        AcpRuntime.load_session,
        AcpClient._initialize_session,
    ):
        assert getattr(method, "__bot_chat_start_recorded__", False) is True, method
