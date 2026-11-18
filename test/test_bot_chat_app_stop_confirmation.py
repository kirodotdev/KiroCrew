"""Rule 2 of the Bot-chat app stop: only a confirmed teardown counts, for every runtime.

Each test names the one change that turns it red.

* a start in flight (recorded as mounted, not registered) keeps the stop
  unconfirmed -- read ``spawn_in_flight`` after lifting the ending fence, or not at
  all, and the slug is reported stopped;
* the ending fence spans the discard -- lift it before the discard and the recorded
  order changes;
* a fence that cannot be raised is a failure -- swallow the error and the slug is
  reported stopped;
* a refused Bot start ends through its own shutdown -- hard-kill it like any other
  and the shutdown is never awaited;
* the AcpClient host is judged by its own tree verdict -- forward only the KAS flag
  and the outer provider reads confirmed;
* a reset with no process keeps the verdict, and records what survived -- reset it
  unconditionally and a confirmed tree reads unconfirmed;
* a dead runtime's evict confirms once the PIDs its kill left are gone -- read the
  stored verdict alone and the retry never confirms.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from test_bot_chat_app_tools import _fresh_record, world  # noqa: F401 - fixtures

from kiro_crew import bot_chat_app_tools as tools

APP = "team-app"
SLUG = "atlas"
KEY = "dashboard_member-atlas.memory-member-atlas-1"


class _Alloc:
    def __init__(self, order: list[str], in_flight: str | None) -> None:
        self.order = order
        self.in_flight = in_flight

    def begin_ending(self, key: str) -> None:
        self.order.append("begin")

    def end_ending(self, key: str) -> None:
        self.order.append("end")

    def spawn_in_flight(self, key: str) -> str | None:
        self.order.append("spawn")
        return self.in_flight


class _Sessions:
    def __init__(self, alloc: object) -> None:
        self.alloc = alloc

    async def discard_conversation(self, key: str, **_kw: object) -> bool:
        getattr(self.alloc, "order", []).append("discard")
        return True  # no registered session under the key

    def _allocation_boundary(self) -> object:
        if self.alloc is None:
            raise RuntimeError("no allocator")
        return self.alloc


def _mount() -> None:
    tools._MOUNTED[SLUG] = {f"{APP}:team": "fp"}
    tools._MOUNTED_SESSION_KEYS[SLUG] = KEY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "in_flight", [None, "refused at registration, its provider hard-killed there"]
)
async def test_a_start_in_flight_keeps_the_stop_unconfirmed(in_flight):
    _mount()
    order: list[str] = []
    state = SimpleNamespace(sessions=_Sessions(_Alloc(order, in_flight)))

    stopped, failed = await tools.stop_app_bot_chat_servers(state, APP)

    assert order == ["begin", "discard", "spawn", "end"]
    if in_flight:
        assert (stopped, failed) == ([], [SLUG])
        assert SLUG in tools._MOUNTED, "the mounted record must stay for the retry"
    else:
        assert (stopped, failed) == ([SLUG], [])


@pytest.mark.asyncio
async def test_a_pending_delivery_with_a_start_in_flight_is_not_confirmed():
    order: list[str] = []
    tools._PENDING_DELIVERY[KEY] = {APP}
    state = SimpleNamespace(sessions=_Sessions(_Alloc(order, "start in flight")))

    _stopped, failed = await tools.stop_app_bot_chat_servers(state, APP)

    assert failed == [KEY]
    assert KEY in tools._PENDING_DELIVERY


@pytest.mark.asyncio
async def test_an_ending_fence_that_cannot_be_raised_is_a_failure():
    _mount()
    state = SimpleNamespace(sessions=_Sessions(None))

    stopped, failed = await tools.stop_app_bot_chat_servers(state, APP)

    assert (stopped, failed) == ([], [SLUG])


@pytest.mark.asyncio
@pytest.mark.parametrize("mounted", [True, False])
async def test_a_refused_bot_start_ends_through_its_own_shutdown(mounted):
    from kiro_crew.session_allocation_parts import get_or_create

    if mounted:
        _mount()
    provider = SimpleNamespace(_client=SimpleNamespace(_session_key=KEY), shutdown=AsyncMock())
    owner = SimpleNamespace(_dispatch_hard_kill=lambda p: killed.append(p))
    killed: list[object] = []

    with patch.object(get_or_create, "is_shared_chat_runtime", lambda p: False):
        await get_or_create._end_started_provider(owner, provider)

    if mounted:
        provider.shutdown.assert_awaited_once()
        assert killed == []
    else:
        provider.shutdown.assert_not_awaited()
        assert killed == [provider]


@pytest.mark.asyncio
async def test_a_refused_bot_start_whose_shutdown_fails_is_kept_unconfirmed():
    from kiro_crew.session_allocation_parts import get_or_create

    _mount()
    provider = SimpleNamespace(
        _client=SimpleNamespace(_session_key=KEY),
        shutdown=AsyncMock(side_effect=RuntimeError("shutdown failed")),
    )
    killed: list[object] = []
    owner = SimpleNamespace(_dispatch_hard_kill=lambda p: killed.append(p))

    with patch.object(get_or_create, "is_shared_chat_runtime", lambda p: False):
        await get_or_create._end_started_provider(owner, provider)
    tools.cancel_pending_close_timers()

    assert killed == [provider]
    handles = tools._PENDING_CLOSE.get((SLUG, APP))
    assert handles, "the hard-killed start must stay an unconfirmed close"
    assert await handles[0][0].evict_session(handles[0][1]) is False


def test_the_registration_refusal_ends_a_bot_start_through_its_shutdown():
    from kiro_crew.session_allocation_parts import get_or_create

    source = inspect.getsource(get_or_create)
    assert (
        "if _bot_chat_mount_key(provider) is not None:\n"
        "                await _end_started_provider(owner, provider)"
    ) in source


@pytest.mark.asyncio
@pytest.mark.parametrize("tree_dead", [False, True])
async def test_the_acp_client_host_is_judged_by_its_own_tree_verdict(tree_dead):
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.acp.session_provider_shared import TreeSurvivors
    from kiro_crew.providers import acp_lifecycle

    client = AcpClient.__new__(AcpClient)
    client.shutdown = AsyncMock()  # type: ignore[method-assign]
    client._process_tree_confirmed_dead = tree_dead
    client._unreaped_pids = () if tree_dead else (4242,)
    client._session_key = KEY
    outer = SimpleNamespace(
        session_id="sid-1",
        memory_mode="persistent",
        is_pi_backend=False,
        is_kas_backend=False,
        _disposable_work_dir=False,
        _client=client,
    )
    recorded: list[tuple] = []
    with patch(
        "kiro_crew.bot_chat_app_tools.note_unconfirmed_close",
        side_effect=lambda key, rt, sid: recorded.append((key, rt, sid)),
    ):
        await acp_lifecycle.shutdown(outer)  # type: ignore[arg-type]

    if tree_dead:
        assert getattr(outer, "_kas_shutdown_unconfirmed", False) is False
        assert recorded == []
    else:
        assert outer._kas_shutdown_unconfirmed is True
        key, rt, sid = recorded[0]
        assert (key, sid) == (KEY, "sid-1")
        assert isinstance(rt, TreeSurvivors) and rt.pids == (4242,)


@pytest.mark.parametrize("verdict", [True, False])
def test_a_client_reset_with_no_process_keeps_the_tree_verdict(tmp_path, verdict):
    from kiro_crew.acp.client import AcpClient

    client = AcpClient(work_dir=str(tmp_path))
    client._process = None
    client._pid = None
    client._process_tree_confirmed_dead = verdict
    client._reset_state()
    assert client._process_tree_confirmed_dead is verdict


@pytest.mark.asyncio
@pytest.mark.parametrize("gone", [True, False])
async def test_a_dead_runtime_evict_confirms_once_the_survivors_are_gone(tmp_path, gone):
    from kiro_crew.acp.runtime import AcpRuntime

    rt = AcpRuntime(work_dir=str(tmp_path))
    rt._dead = True
    rt._process = None
    rt._process_tree_confirmed_dead = False
    rt._unreaped_pids = (4242,)
    with patch("kiro_crew.session_pid_tracking._pid_gone_or_unmanaged", lambda pid: gone):
        confirmed = await rt.evict_session("sid-1")
    assert confirmed is gone
    assert rt.process_tree_confirmed_dead is gone


@pytest.mark.asyncio
async def test_tree_survivors_confirm_only_with_every_pid_gone():
    from kiro_crew.acp.session_provider_shared import TreeSurvivors

    assert await TreeSurvivors(()).evict_session("s") is False
    with patch("kiro_crew.session_pid_tracking._pid_gone_or_unmanaged", lambda pid: pid != 2):
        assert await TreeSurvivors((1, 2)).evict_session("s") is False
        assert await TreeSurvivors((1, 3)).evict_session("s") is True


# ── An app with no installed record still has its Bot chats stopped ────────────


@pytest.mark.asyncio
async def test_tracked_bot_chats_stop_from_the_tracking_tables_alone(monkeypatch):
    """``stop_tracked_bot_chats`` stops an app found only in the Bot tracking tables,
    inside its stop fence, and returns the failures; with nothing tracked it does
    nothing. Make it return ``None`` unconditionally and the first case fails."""
    from kiro_crew.bot_chat_ending_fence import stop_tracked_bot_chats

    seen: list[int] = []

    async def _stop(state, app_name):
        seen.append(tools._FENCE_HOLD_COUNTS.get(app_name, 0))
        return [], [SLUG]

    monkeypatch.setattr(tools, "stop_app_bot_chat_servers", _stop)
    assert await stop_tracked_bot_chats(object(), APP) is None
    tools._PENDING_DELIVERY[KEY] = {APP}  # pending delivery counts as tracked
    assert await stop_tracked_bot_chats(object(), APP) == [SLUG]
    assert seen == [1]
    assert tools._FENCE_HOLD_COUNTS.get(APP, 0) == 0


@pytest.mark.asyncio
async def test_an_in_process_uninstall_of_a_missing_app_stops_its_tracked_bot_chats(monkeypatch):
    """No installed record, a Bot chat still mounted: the in-process uninstall
    stops it and answers 409 when the stop fails, instead of a bare 404."""
    from kiro_crew.apps import routes_uninstall

    _mount()

    async def _stop(state, app_name):
        return [], [SLUG]

    monkeypatch.setattr(tools, "stop_app_bot_chat_servers", _stop)
    with patch("kiro_crew.apps.routes.get_app", return_value=None):
        out = await routes_uninstall.uninstall_app_in_process({"state": object()}, APP, caller="t")
    assert out["status"] == 409
    assert out["code"] == "bot_chat_stop_incomplete"
    assert out["bot_chat_stop_failed"] == [SLUG]


def test_every_missing_record_branch_stops_tracked_bot_chats():
    """Disable, uninstall (route, in-process) and migrate-cleanup each consult the
    tracking tables when the app has no installed record. Drop the call from any of
    them and this fails."""
    from kiro_crew.apps import routes_enable, routes_uninstall

    for fn in (
        routes_enable.handle_disable_app,
        routes_uninstall.handle_uninstall_app,
        routes_uninstall.uninstall_app_in_process,
        routes_uninstall.handle_migrate_cleanup,
    ):
        assert "await stop_tracked_bot_chats(" in inspect.getsource(fn), fn.__name__
