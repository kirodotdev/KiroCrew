"""A start that fails after its runtime launched keeps that runtime as the close.

``session/new`` times out after the app servers came up; the cleanup kill leaves a
child alive (or raises on a Windows tree still draining). The start raises before
the placeholder ``_client`` is replaced, so the provider's shutdown sees a client
that never launched anything. Each test names the change that turns it red:

* the failed start keeps the runtime as this provider's unconfirmed close -- drop
  the ``note_unconfirmed_teardown`` call in ``acp_runtime_start``'s cleanup;
* the shutdown after it keeps that close and its runtime handle, adding none that
  can never settle (``TreeSurvivors(())``) for the placeholder.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_acp_lifecycle_shutdown_exits import _client, _outer, _shutdown
from test_acp_provider import _build_provider

from kiro_crew.acp.session_provider_shared import RuntimeTreeGone, is_local_close_token
from kiro_crew.platform_compat_wintree import WindowsTreeDrainPending
from kiro_crew.testing.ids import UNALLOCATABLE_PID


def _kiro_provider():
    provider = _build_provider(backend="")
    provider._client._work_dir = "/tmp/ws"
    provider._client._agent = "kirocrew"
    provider._client._sandbox_mode = "auto"
    provider._client._extra_env = {}
    provider._client._mcp_gateway_overlay = None
    provider._client._mcp_gateway_socket = None
    provider._client._model = "auto"
    provider._client._resume_session_id = ""
    provider._client._session_key = "dashboard:member-atlas.memory-member-atlas-1"
    return provider


def _runtime(kill):
    rt = MagicMock()
    rt.pid = UNALLOCATABLE_PID
    rt.spawn = AsyncMock()
    rt.kill = kill
    rt.process_tree_confirmed_dead = False
    for latch in ("saw_not_logged_in", "saw_sandbox_init_failure", "saw_delegate_launch_failure"):
        setattr(rt, latch, MagicMock(return_value=False))
    rt.kas_extract_broken_message = MagicMock(return_value="")
    rt.settle_stderr = AsyncMock()
    rt.create_session = AsyncMock(side_effect=TimeoutError("session/new timed out"))
    return rt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kill",
    [
        AsyncMock(),
        AsyncMock(side_effect=WindowsTreeDrainPending(root_pid=UNALLOCATABLE_PID, pending=1)),
    ],
    ids=["survivor", "tree-draining"],
)
async def test_a_failed_start_keeps_its_runtime_as_the_close(kill):
    provider = _kiro_provider()
    rt = _runtime(kill)
    recorded: list[tuple] = []
    with (
        patch("kiro_crew.providers.acp.AcpRuntime", return_value=rt),
        patch("kiro_crew.providers.acp.AcpSessionProvider"),
        patch(
            "kiro_crew.bot_chat_app_tools.note_unconfirmed_close",
            side_effect=lambda key, handle, sid: recorded.append((key, handle)),
        ),
    ):
        with pytest.raises(TimeoutError):
            await provider._start_kiro_runtime()
    assert provider._kas_shutdown_unconfirmed is True
    # No native id came back: the close is kept under a local token and judged by
    # the runtime's tree (``close_handle``), never by a close sent under the token.
    kept = provider._kas_unconfirmed_runtime
    assert isinstance(kept, RuntimeTreeGone) and kept.runtime is rt
    assert is_local_close_token(provider._kas_unconfirmed_session_id)
    assert recorded == [("dashboard:member-atlas.memory-member-atlas-1", kept)]


@pytest.mark.asyncio
async def test_a_failed_start_whose_tree_is_confirmed_dead_keeps_nothing():
    provider = _kiro_provider()
    rt = _runtime(AsyncMock())
    rt.process_tree_confirmed_dead = True
    with (
        patch("kiro_crew.providers.acp.AcpRuntime", return_value=rt),
        patch("kiro_crew.providers.acp.AcpSessionProvider"),
    ):
        with pytest.raises(TimeoutError):
            await provider._start_kiro_runtime()
    assert getattr(provider, "_kas_shutdown_unconfirmed", False) is False


@pytest.mark.asyncio
async def test_the_shutdown_after_a_failed_start_stays_unconfirmed():
    rt = object()
    outer = _outer(_client(spawned=False))  # the placeholder the start never replaced
    outer._kas_shutdown_unconfirmed = True  # what the failed start recorded
    outer._kas_unconfirmed_runtime = rt
    assert await _shutdown(outer) == []
    assert outer._kas_shutdown_unconfirmed is True and outer._kas_unconfirmed_runtime is rt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "outrun", "kept_one"),
    [("kas", False, False), ("codex", False, False), ("kas", True, False), ("codex", True, True)],
)
async def test_a_failed_runtime_start_is_kept_only_beside_a_stop_on_codex(
    backend, outrun, kept_one
):
    """A KAS resume whose ``session/load`` raised falls back to ``create_session`` on the
    same live runtime: kept, its handle would block the Bot's tools for the life of
    the process. So a failed start is kept only when a stop ran beside it, and with
    no native id only on a host whose close leaves the servers running (codex).
    Keep it on every failure (``keep_failed``) and the ``kas``/no-stop rows go red."""
    from kiro_crew import bot_chat_ending_fence as fence

    key = "dashboard:member-atlas.x"

    @fence.runtime_session_start
    async def load_session(self, **_kw):
        if outrun:
            fence._START_OUTRUN.add(key)  # a stop ran beside this start
        raise TimeoutError("session/load refused")

    owner = SimpleNamespace(acp_backend=backend, _agent="kirocrew-mate-conductor")
    kept: list[tuple] = []
    with patch(
        "kiro_crew.bot_chat_app_tools.note_unconfirmed_close",
        side_effect=lambda k, rt, sid: kept.append((k, rt, sid)),
    ):
        with pytest.raises(TimeoutError):
            await load_session(owner, session_key=key)
    if kept_one:  # codex: kept under its tree, never under a close answer
        ((k, handle, sid),) = kept
        assert (k, sid) == (key, None) and handle.runtime is owner
    else:
        assert kept == []


def test_a_failed_resume_leaves_the_bots_tools_mountable():
    """The fallback ``create_session`` that follows a failed ``session/load`` finds
    no pending close for the Bot's app, so its projection keeps the app's tools and
    a later disable has nothing to report as not stopped."""
    from kiro_crew import bot_chat_app_tools as tools
    from kiro_crew import bot_chat_ending_fence as fence

    key = "dashboard:member-atlas.x"
    tools._SESSION_APP_SNAPSHOT_PENDING[key] = ("atlas", frozenset({"team-app"}))

    @fence.runtime_session_start
    async def load_session(self, **_kw):
        raise TimeoutError("require_replay refused")

    with pytest.raises(TimeoutError):
        asyncio.run(
            load_session(
                SimpleNamespace(acp_backend="kas", _agent="kirocrew-mate-conductor"),
                session_key=key,
            )
        )
    assert ("atlas", "team-app") not in tools._PENDING_CLOSE_APPS
    assert tools._SESSION_APP_SNAPSHOT_PENDING[key] == ("atlas", frozenset({"team-app"}))


def test_a_stop_during_a_start_leaves_its_pending_record():
    """A stop that confirms while the chat's start is still running leaves the
    start's pending record, so the start's own failure can keep it. Pop it
    regardless and the failed start finds nothing to keep."""
    from kiro_crew import bot_chat_app_tools as tools
    from kiro_crew import bot_chat_ending_fence as fence

    key = "dashboard:member-atlas.x"
    tools._SESSION_APP_SNAPSHOT_PENDING[key] = ("atlas", frozenset({"team-app"}))
    fence._STARTING[key] = 1
    try:
        tools._keep_left_sessions("atlas", key)
        assert key in tools._SESSION_APP_SNAPSHOT_PENDING
    finally:
        fence._STARTING.pop(key, None)
    tools._keep_left_sessions("atlas", key)
    assert key not in tools._SESSION_APP_SNAPSHOT_PENDING
