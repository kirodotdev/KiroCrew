"""Every unconfirmed Bot-chat close handle confirms on evidence, and only on evidence.

A Claude Bot chat (an ``AcpClient`` that owns its process) is stopped while its
``_initialize_session`` runs; the stop keeps the start's handle. The handle must
confirm once the client's shutdown confirmed its tree dead, so a re-enabled app's
tools come back, and stay while it has not. Each test names the change that turns
it red:

* the client start's handle watches the client's tree -- register
  ``_UnobservedKill()`` in ``_keep_client_session`` again;
* a handle with no owner on record confirms on a confirmed close of its session --
  drop ``note_closed_session`` from ``teardown_confirmed``;
* an owner that never launched a process confirms at once -- drop the
  ``_spawned`` clause from ``RuntimeTreeGone``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from test_bot_chat_app_tools import APP, KEY, _fresh_record  # noqa: F401

from kiro_crew import bot_chat_app_tools as tools
from kiro_crew import bot_chat_ending_fence as fence
from kiro_crew.acp.session_provider_shared import RuntimeTreeGone, teardown_confirmed

PAIR = ("atlas", APP)


def _client(*, tree_dead: bool):
    return SimpleNamespace(
        _session_id=None,
        process_tree_confirmed_dead=tree_dead,
        _unreaped_pids=(),
        is_alive=lambda: False,
    )


def _outrun_start(client):
    tools._SESSION_APP_SNAPSHOT_PENDING[KEY] = ("atlas", frozenset({APP}))
    fence._keep_client_session(client, KEY, None)


def test_a_client_whose_tree_is_confirmed_dead_settles_and_unblocks_the_app():
    client = _client(tree_dead=False)
    _outrun_start(client)
    ((handle, _token),) = tools._PENDING_CLOSE[PAIR]
    assert isinstance(handle, RuntimeTreeGone) and handle.runtime is client
    client.process_tree_confirmed_dead = True  # the provider's shutdown confirmed it
    asyncio.run(tools._drain_pending_close())
    assert PAIR not in tools._PENDING_CLOSE
    assert PAIR not in tools._PENDING_CLOSE_APPS  # the delivery gate mounts it again


def test_a_client_not_confirmed_dead_keeps_the_close_pending():
    _outrun_start(_client(tree_dead=False))
    asyncio.run(tools._drain_pending_close())
    assert PAIR in tools._PENDING_CLOSE and PAIR in tools._PENDING_CLOSE_APPS


def test_a_handle_with_no_owner_confirms_once_its_session_close_is_confirmed():
    handle = fence._UnobservedKill()
    assert asyncio.run(handle.evict_session("sess-9")) is False
    runtime = SimpleNamespace(_unconfirmed_teardowns=set())
    assert teardown_confirmed(runtime, "sess-9", killed=False) is True
    assert asyncio.run(handle.evict_session("sess-9")) is True


def test_an_owner_that_never_launched_a_process_confirms():
    owner = SimpleNamespace(_spawned=False, is_alive=lambda: False, _unreaped_pids=())
    assert asyncio.run(RuntimeTreeGone(owner).evict_session("t")) is True
    owner._spawned = True
    assert asyncio.run(RuntimeTreeGone(owner).evict_session("t")) is False


class _CodexRuntime:
    """A live codex runtime whose host acknowledges any ``session/close``."""

    acp_backend = "codex"
    process_tree_confirmed_dead = False
    _unreaped_pids: tuple = ()

    def __init__(self) -> None:
        self.closed: list[str] = []

    def is_alive(self) -> bool:
        return True

    async def evict_session(self, session_id: str) -> bool:
        self.closed.append(session_id)
        return True


def test_a_close_answer_does_not_clear_the_tree_handle_beside_it():
    """One sid held by a host-close handle and a tree handle: the close answer
    settles only its own entry. Key the drain by sid again and both go."""
    rt = _CodexRuntime()
    tools._SESSION_APP_SNAPSHOT_PENDING[KEY] = ("atlas", frozenset({APP}))
    tools.note_unconfirmed_close(KEY, rt, "sess-c")
    tools._SESSION_APP_SNAPSHOT_PENDING[KEY] = ("atlas", frozenset({APP}))
    tools.note_unconfirmed_close(KEY, RuntimeTreeGone(rt), "sess-c")
    asyncio.run(tools._drain_pending_close())
    ((handle, sid),) = tools._PENDING_CLOSE[PAIR]
    assert isinstance(handle, RuntimeTreeGone) and sid == "sess-c"
    assert PAIR in tools._PENDING_CLOSE_APPS


def test_a_late_codex_start_is_kept_under_its_tree():
    """A codex start the stop outran is kept under ``RuntimeTreeGone``, never under
    the runtime whose close answer would settle it. Keep the raw runtime and the
    close answer confirms a session whose servers still run."""
    rt = _CodexRuntime()
    tools._SESSION_APP_SNAPSHOT_PENDING[KEY] = ("atlas", frozenset({APP}))
    fence._keep_runtime_session(rt, KEY, SimpleNamespace(session_id="sess-late"))
    ((handle, sid),) = tools._PENDING_CLOSE[PAIR]
    assert isinstance(handle, RuntimeTreeGone) and handle.runtime is rt
    asyncio.run(tools._drain_pending_close())
    assert rt.closed == [] and PAIR in tools._PENDING_CLOSE


def test_a_late_native_id_keeps_a_token_held_tree_handle():
    """A codex start kept under a local token gets its native id late: the tree
    handle moves to the real id, and a host-close handle beside it cannot take over.
    The owner runs this one chat's session, so the late id is that start's own."""
    rt = _CodexRuntime()
    rt._session_key = KEY
    tools._SESSION_APP_SNAPSHOT_PENDING[KEY] = ("atlas", frozenset({APP}))
    tools.note_unconfirmed_close(KEY, RuntimeTreeGone(rt), None)
    tools.note_unconfirmed_close(KEY, rt, "sess-real")
    held = tools._PENDING_CLOSE[PAIR]
    assert any(isinstance(h, RuntimeTreeGone) and s == "sess-real" for h, s in held)
    asyncio.run(tools._drain_pending_close())
    assert any(isinstance(h, RuntimeTreeGone) for h, _s in tools._PENDING_CLOSE[PAIR])


class _Receipt:
    """A Windows tree cleanup a kill left pending; the sweep sets ``retired``."""

    retired = False


def test_a_drain_pending_kill_settles_once_its_windows_receipt_retires():
    """A Windows drain raised ``WindowsTreeDrainPending`` before the reset recorded
    any survivor, so the handle holds no PID. It confirms once the sweep retires the
    cleanup receipt, and the Bot's app is mountable again. Drop the receipt check
    from ``TreeSurvivors`` and it never confirms."""
    from kiro_crew.acp.session_provider_shared import TreeSurvivors

    receipt = _Receipt()
    tools._SESSION_APP_SNAPSHOT_PENDING[KEY] = ("atlas", frozenset({APP}))
    tools.note_unconfirmed_close(KEY, TreeSurvivors((), receipt), "sess-w")
    asyncio.run(tools._drain_pending_close())
    assert PAIR in tools._PENDING_CLOSE and PAIR in tools._PENDING_CLOSE_APPS  # not yet
    receipt.retired = True
    asyncio.run(tools._drain_pending_close())
    assert PAIR not in tools._PENDING_CLOSE and PAIR not in tools._PENDING_CLOSE_APPS


def test_a_runtime_tree_handle_and_the_runtime_check_read_the_receipt():
    """``RuntimeTreeGone`` and ``_tree_confirmed_dead_now`` confirm a runtime whose
    pending Windows cleanup was retired, and not before."""
    from kiro_crew.acp.runtime_session_registry import _tree_confirmed_dead_now

    receipt = _Receipt()
    rt = SimpleNamespace(
        process_tree_confirmed_dead=False,
        _process_tree_confirmed_dead=False,
        _unreaped_pids=(),
        _windows_cleanup_receipt=receipt,
        is_alive=lambda: False,
    )
    assert asyncio.run(RuntimeTreeGone(rt).evict_session("s")) is False
    assert asyncio.run(_tree_confirmed_dead_now(rt)) is False
    receipt.retired = True
    assert asyncio.run(RuntimeTreeGone(rt).evict_session("s")) is True
    assert asyncio.run(_tree_confirmed_dead_now(rt)) is True
