"""Only a Bot's own chat on a host that carries its app servers is tracked.

``carries_app_tools`` is the one test: the mount side (``session_mcp_projection``'s
*bot_app_tools_carried*, the KAS projection, the mirrors, the client array) and the
start record (``bot_chat_ending_fence``) both use it. Any other chat, a Kiro CLI
one or a Bot's chat on Kiro CLI included, runs as it does without the Bot's app
tools. Each case below breaks with one change:

* the start record: make ``_runtime_start_key`` / ``_client_start_key`` return the
  key without ``carries_app_tools``;
* a cancelled ``session/new`` is not shielded: the same change (``_kept_to_its_end``
  reads ``current_start_key``);
* a load neither waits for an abandoned close nor is refused by a pending one: drop
  ``bot_start`` from those two tests in ``load_session``;
* an abandoned load on KAS closes through ``terminate_session``: drop ``bot_start``
  from ``_close_abandoned_load``'s verb choice;
* a timed-out start's collector carries no chat key: set ``collector.session_key``
  from ``session_key`` again;
* the client array records nothing: drop ``carries_app_tools`` from
  ``_record_bot_chat_mounts``;
* the worker-kit projection mounts no app servers on Kiro CLI: pass
  ``backend in ACP_BACKENDS_MATE_WORKER_KIT`` to ``session_mcp_projection`` again.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew import bot_chat_ending_fence as fence
from kiro_crew.acp import runtime_session_create as create
from kiro_crew.acp import runtime_session_load as load
from kiro_crew.bot_chat_app_tools import carries_app_tools
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, await_until

KEY = "dashboard:member-atlas.memory-member-atlas-1"
BOT = "kirocrew-mate-conductor"


class _Reached(Exception):
    pass


@pytest.mark.parametrize(
    "agent,key,backend,carried",
    [
        (BOT, KEY, "kas", True),
        (BOT, KEY, "claude", True),
        (BOT, KEY, "kiro", False),  # a Bot's chat on Kiro CLI
        ("kiro_default", KEY, "kas", False),  # an ordinary chat
        (BOT, "", "kas", False),
        (BOT, None, "kas", False),
    ],
)
def test_carries_app_tools(agent, key, backend, carried):
    assert carries_app_tools(agent, key, backend) is carried


def _runtime(backend: str, agent: str) -> SimpleNamespace:
    return SimpleNamespace(acp_backend=backend, _agent=agent)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend,agent", [("kiro", BOT), ("kas", "kiro_default")])
async def test_an_ordinary_start_is_not_recorded(backend, agent):
    seen: list[tuple[str, bool]] = []

    @fence.runtime_session_start
    async def start(self, *, session_key: str = "", agent: str | None = None):
        seen.append((fence.current_start_key(), fence.start_in_flight(KEY)))

    await start(_runtime(backend, agent), session_key=KEY)
    await start(_runtime("kas", BOT), session_key=KEY, agent=agent)
    assert seen[0] == ("", False)
    assert seen[1] == ((KEY, True) if agent == BOT else ("", False))


@pytest.mark.asyncio
async def test_an_ordinary_client_start_is_not_recorded():
    seen: list[str] = []

    @fence.client_session_start
    async def init(self):
        seen.append(fence.current_start_key())

    await init(SimpleNamespace(_session_key=KEY, _agent=BOT, _acp_backend="kiro"))
    await init(SimpleNamespace(_session_key=KEY, _agent=BOT, _acp_backend="codex"))
    assert seen == ["", KEY]


@pytest.mark.asyncio
async def test_a_cancelled_ordinary_session_new_is_not_shielded():
    request: asyncio.Future = asyncio.get_running_loop().create_future()
    started = asyncio.Event()

    @fence.runtime_session_start
    async def start(self, *, session_key: str = ""):
        started.set()
        return await create._kept_to_its_end(self, request, None)

    caller = asyncio.ensure_future(start(_runtime("kiro", BOT), session_key=KEY))
    await await_until(started, what="the start reached session/new")
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await await_until(caller, what="the cancelled start returned")
    assert request.cancelled() and not create._ABANDONED_NEW_CLOSES


def _loading_runtime(backend: str, agent: str) -> SimpleNamespace:
    async def source_agent(_agent):
        raise _Reached

    return SimpleNamespace(
        _harness=SimpleNamespace(per_session_state=False),
        acp_backend=backend,
        _agent=agent,
        _initialized=True,
        _can_load_session=True,
        _session_queues={},
        _source_agent=source_agent,
    )


@pytest.mark.asyncio
async def test_an_ordinary_load_neither_waits_nor_is_refused(monkeypatch):
    rt = _loading_runtime("kiro", BOT)
    barrier: asyncio.Future = asyncio.get_running_loop().create_future()
    load._PENDING_ABANDONED_CLOSES[(id(rt), "resumed-1")] = barrier
    monkeypatch.setattr(load, "close_pending", lambda *_a: True)
    try:
        with pytest.raises(_Reached):  # reached the load itself, at once
            await asyncio.wait_for(
                load.load_session(rt, "", "resumed-1", session_key=KEY),
                timeout=FALLBACK_TIMEOUT_SECS,
            )
    finally:
        load._PENDING_ABANDONED_CLOSES.pop((id(rt), "resumed-1"), None)


@pytest.mark.parametrize("bot_start,verb", [(False, "terminate"), (True, "evict")])
def test_an_abandoned_ordinary_load_on_kas_closes_as_on_main(bot_start, verb):
    calls: list[str] = []

    async def _close(name, _sid):
        calls.append(name)

    rt = SimpleNamespace(
        acp_backend="kas",
        _session_queues={},
        evict_session=lambda sid: _close("evict", sid),
        terminate_session=lambda sid: _close("terminate", sid),
    )

    async def run():
        answered: asyncio.Future = asyncio.get_running_loop().create_future()
        answered.set_result({})
        task = load._close_abandoned_load(rt, "sid-1", answered, bot_start)
        await task

    asyncio.run(run())
    assert calls == [verb]


@pytest.mark.asyncio
async def test_a_timed_out_ordinary_start_hands_its_collector_no_chat_key():
    from kiro_crew.acp.session_handle import AcpRequestTimeout

    future: asyncio.Future = asyncio.get_running_loop().create_future()
    exc = AcpRequestTimeout("session/new timed out")
    exc.req_id, exc.adopted_future = 9, future
    permit = SimpleNamespace(
        hold_for_collector=lambda: False,
        release=lambda: None,
        priority=SimpleNamespace(value="background"),
        gate_counts=lambda: (0, 0),
        gate_state=lambda: "",
        _gate=SimpleNamespace(collector_holds=0, collector_hold_ceiling=0),
    )
    rt = SimpleNamespace(
        acp_backend="kiro",
        _agent=BOT,
        _start_collect_timeout=FALLBACK_TIMEOUT_SECS,
        _start_collectors={},
        _pending_init_notifications=[],
        _pending_requests={},
    )

    @fence.runtime_session_start
    async def start(self, *, session_key: str = ""):
        return create._collect_late_start(
            self,
            exc,
            permit,
            agent=None,
            crew_agent=None,
            kas_agents=None,
            mcp_servers=[],
            budget=1.0,
            stub_token="",
            denied_tools=frozenset(),
            mirrored_snapshot=None,
            ref_spec=None,
            active_agent=BOT,
            session_work_dir="/w",
            projected_sources={},
            payload_snapshot=None,
            late_adopter=None,
            session_key=session_key,
        )

    collector = await start(rt, session_key=KEY)
    try:
        assert collector.session_key == ""
    finally:
        future.cancel()
        await await_until(collector.settled, what="the collector settled")


def test_the_client_array_records_nothing_for_an_ordinary_chat(monkeypatch):
    from kiro_crew import bot_chat_app_tools as tools
    from kiro_crew.acp import client_mcp_array

    def _boom(*_a, **_k):
        raise AssertionError("an ordinary chat reached the Bot's record")

    monkeypatch.setattr(tools, "record_mounted", _boom)
    monkeypatch.setattr(tools, "check_and_filter_projection", _boom)
    client = SimpleNamespace(_agent=BOT, _session_key=KEY, _acp_backend="kiro")
    servers = [{"name": "probe:team"}]
    client_mcp_array._record_bot_chat_mounts(client, servers)
    assert servers == [{"name": "probe:team"}]


def test_the_worker_kit_projection_mounts_no_app_servers_on_kiro_cli(monkeypatch):
    from kiro_crew.acp import session_mcp, session_mcp_crewmate

    seen: list[bool] = []

    def _projection(agent, **kwargs):
        seen.append(kwargs["bot_app_tools_carried"])
        raise _Reached

    monkeypatch.setattr(session_mcp, "session_mcp_projection", _projection)
    for backend in ("kiro", "kas"):
        with pytest.raises(_Reached):
            session_mcp_crewmate.crewmate_worker_deny_rules(
                BOT, work_dir="/w", session_key=KEY, backend=backend
            )
    assert seen == [False, True]


def test_an_ordinary_chats_unconfirmed_teardown_reaches_no_bot_close(monkeypatch):
    """A close the host did not confirm is kept on the provider for any chat, but
    only a Bot chat with app servers on record gets a pending close to retry."""
    from kiro_crew import bot_chat_app_tools as tools
    from kiro_crew.acp.session_provider_shared import note_unconfirmed_teardown

    provider = SimpleNamespace(_session_key="dashboard:plain-chat")
    note_unconfirmed_teardown(provider, SimpleNamespace(acp_backend="kiro"), "sid-9")
    assert provider._kas_shutdown_unconfirmed is True
    assert not tools._PENDING_CLOSE and not tools._PENDING_CLOSE_APPS


def test_every_harness_mounts_through_the_one_test():
    """The KAS projection and the mirrors gate on ``carries_app_tools`` like the
    projection and the client array above, so no harness mounts where no start
    is recorded."""
    import inspect

    from kiro_crew.acp import runtime_session_setup
    from kiro_crew.acp.harness import kas

    kas_src = inspect.getsource(kas.KasHarness)
    assert kas_src.count("carries_app_tools(agent, session_key, ACP_BACKEND_KAS)") == 2
    mirror_src = inspect.getsource(runtime_session_setup._mirrored_session_mcp)
    assert "carries_app_tools(active_agent, session_key, self.acp_backend)" in mirror_src
