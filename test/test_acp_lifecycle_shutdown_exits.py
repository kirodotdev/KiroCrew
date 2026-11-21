"""Every exit of ``AcpProvider.shutdown`` records whether the close is confirmed.

The rule (``acp_lifecycle._settle_shutdown_confirmation``):

* returned, whole tree confirmed dead: confirmed;
* returned, a PID still alive (or none readable for a process it held): unconfirmed;
* returned, the client never launched a process and held none: confirmed;
* raised, whatever the verdict: unconfirmed, and the exception still propagates;
* a shared-runtime inner provider that raised: unconfirmed on its runtime.

Each test names the change that turns it red.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.session_provider_shared import TreeSurvivors
from kiro_crew.platform_compat_wintree import WindowsTreeDrainPending
from kiro_crew.providers import acp_lifecycle

KEY = "dashboard_member-atlas.memory-member-atlas-1"


def _client(*, spawned=True, process=None, tree_dead=False, unreaped=(), raises=None):
    client = AcpClient.__new__(AcpClient)
    client.shutdown = AsyncMock(side_effect=raises)  # type: ignore[method-assign]
    client._spawned = spawned
    client._process = process
    client._process_tree_confirmed_dead = tree_dead
    client._unreaped_pids = tuple(unreaped)
    client._session_key = KEY
    return client


def _outer(inner):
    return SimpleNamespace(
        session_id="sid-1",
        memory_mode="persistent",
        is_pi_backend=False,
        is_kas_backend=False,
        _disposable_work_dir=False,
        _client=inner,
    )


async def _shutdown(outer):
    recorded: list[tuple] = []
    with patch(
        "kiro_crew.bot_chat_app_tools.note_unconfirmed_close",
        side_effect=lambda key, rt, sid: recorded.append((key, rt, sid)),
    ):
        await acp_lifecycle.shutdown(outer)  # type: ignore[arg-type]
    return recorded


@pytest.mark.asyncio
async def test_a_client_that_never_started_a_process_is_a_confirmed_close():
    """Drop the never-started exit and the refused start is kept as a close no
    retry can ever settle (``TreeSurvivors(())``)."""
    outer = _outer(_client(spawned=False))
    assert await _shutdown(outer) == []
    assert getattr(outer, "_kas_shutdown_unconfirmed", False) is False


@pytest.mark.asyncio
async def test_a_held_process_whose_pid_cannot_be_read_is_not_read_as_never_started():
    """Judge "never started" on ``_spawned`` alone and a process the test holds
    without a PID reads as confirmed."""
    outer = _outer(_client(spawned=False, process=object()))
    recorded = await _shutdown(outer)
    assert outer._kas_shutdown_unconfirmed is True
    _key, rt, _sid = recorded[0]
    assert isinstance(rt, TreeSurvivors) and rt.pids == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("tree_dead", [False, True])
async def test_a_shutdown_that_raises_is_unconfirmed_and_still_raises(tree_dead):
    """Leave the verdict after the try (not in its finally) and the raise skips it:
    nothing is recorded, and a later stop reads the Bot as stopped. The client's
    own shutdown writes the surviving PIDs in its ``finally`` before the raise."""
    outer = _outer(
        _client(
            process=object(),
            unreaped=(777,),
            tree_dead=tree_dead,
            raises=WindowsTreeDrainPending(root_pid=777, pending=1),
        )
    )
    recorded: list[tuple] = []
    with patch(
        "kiro_crew.bot_chat_app_tools.note_unconfirmed_close",
        side_effect=lambda key, rt, sid: recorded.append((key, rt, sid)),
    ):
        with pytest.raises(WindowsTreeDrainPending):
            await acp_lifecycle.shutdown(outer)  # type: ignore[arg-type]
    assert outer._kas_shutdown_unconfirmed is True
    key, rt, sid = recorded[0]
    assert (key, sid) == (KEY, "sid-1")
    assert isinstance(rt, TreeSurvivors) and rt.pids == (777,)


@pytest.mark.asyncio
async def test_a_returned_shutdown_with_a_live_pid_is_unconfirmed():
    outer = _outer(_client(process=object(), unreaped=(4242,)))
    recorded = await _shutdown(outer)
    assert outer._kas_shutdown_unconfirmed is True
    assert recorded[0][1].pids == (4242,)


@pytest.mark.asyncio
async def test_a_returned_shutdown_with_the_tree_dead_is_confirmed():
    outer = _outer(_client(process=object(), tree_dead=True))
    assert await _shutdown(outer) == []
    assert getattr(outer, "_kas_shutdown_unconfirmed", False) is False


@pytest.mark.asyncio
async def test_a_shared_runtime_provider_that_raised_is_unconfirmed_on_its_runtime():
    """Forward only the inner provider's own flag and a raise before it set one
    reads as a confirmed close."""
    runtime = object()
    inner = SimpleNamespace(
        shutdown=AsyncMock(side_effect=RuntimeError("evict failed")),
        _runtime=runtime,
        _session_key=KEY,
    )
    outer = _outer(inner)
    recorded: list[tuple] = []
    with patch(
        "kiro_crew.bot_chat_app_tools.note_unconfirmed_close",
        side_effect=lambda key, rt, sid: recorded.append((key, rt, sid)),
    ):
        with pytest.raises(RuntimeError):
            await acp_lifecycle.shutdown(outer)  # type: ignore[arg-type]
    assert outer._kas_shutdown_unconfirmed is True
    assert recorded == [(KEY, runtime, "sid-1")]
