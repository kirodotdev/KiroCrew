"""A start that never handed its session to a caller keeps it until evidence settles it.

A Bot chat's ``session/new`` times out on a shared KAS runtime and its collector
gets the native id later; a start refused after ``session/new`` answered tears its
session down before any handle exists; a load its caller left closes the session
once the load settles. None of these reaches a provider, so a stop finds no
registered session. Each test names the change that turns it red:

* the collector counts as the chat's start until it settles -- drop ``hold_start``
  in ``StartCollector.start``: the stop drops the app snapshot and reads clean;
* a late id whose close is not confirmed goes on the chat's record -- drop the
  ``_keep_unsettled`` call in ``StartCollector._run``;
* an answer that never came keeps a tree handle -- drop the ``abandoned`` /
  ``runtime_dead`` arm in ``StartCollector._keep_unsettled``;
* a teardown inside a start keeps what it could not confirm -- drop
  ``note_start_teardown`` in ``terminate_session`` or ``evict_session``;
* an abandoned load or ``session/new`` holds the start, and its session stays on the
  record, until its own outcome settles it -- drop ``abandoned_start`` in
  ``_abandon_load`` or ``_kept_to_its_end``, or the Bot-start arm of either.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew import bot_chat_app_tools as tools
from kiro_crew import bot_chat_ending_fence as fence
from kiro_crew.acp import runtime_session_create as create
from kiro_crew.acp import runtime_session_load as load
from kiro_crew.acp import runtime_session_registry as registry
from kiro_crew.acp.runtime import AcpRuntimeDead
from kiro_crew.acp.runtime_start import StartCollector
from kiro_crew.acp.session_provider_shared import RuntimeTreeGone, is_local_close_token
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, await_until

KEY = "dashboard:member-atlas.memory-member-atlas-1"
SNAP = ("atlas", frozenset({"probe"}))
BOT_AGENT = "kirocrew-mate-conductor"  # a Bot's own chat: its starts are recorded


class _Runtime:
    """A shared runtime whose teardown the host never answers (KAS, another lease)."""

    acp_backend = "kas"
    _agent = BOT_AGENT

    def __init__(self, *, confirms: bool = False) -> None:
        self._dead = False
        self._process = None
        self.process_tree_confirmed_dead = False
        self._pending_requests: dict = {}
        self._start_collectors: dict = {}
        self._unconfirmed_teardowns: set[str] = set()
        self._unclaimed_new_sessions: set[str] = set()
        self._session_harness_states: dict = {}
        self._session_queues: dict = {}
        self.confirms = confirms
        self.torn_down: list[str] = []

    def is_alive(self) -> bool:
        return True

    def unregister_session(self, session_id: str) -> None:
        self._session_queues.pop(session_id, None)

    async def _teardown_late_session(self, session_id: str) -> None:
        self.torn_down.append(session_id)
        if not self.confirms:
            self._unconfirmed_teardowns.add(session_id)


def _collector(rt: _Runtime) -> tuple[StartCollector, asyncio.Future]:
    answer: asyncio.Future = asyncio.get_running_loop().create_future()
    collector = StartCollector(rt, 7, answer, permit=None, timeout=FALLBACK_TIMEOUT_SECS)
    collector.session_key = KEY
    rt._start_collectors[7] = collector
    return collector.start(), answer


@pytest.mark.asyncio
async def test_a_late_id_whose_close_is_unconfirmed_goes_to_the_stop_that_follows():
    rt = _Runtime()
    tools._SESSION_APP_SNAPSHOT_PENDING[KEY] = SNAP
    collector, answer = _collector(rt)
    # A stop while the collector waits: not confirmed, and the snapshot stays.
    assert fence.start_in_flight(KEY)
    assert tools._keep_left_sessions("atlas", KEY) is False
    assert tools._SESSION_APP_SNAPSHOT_PENDING[KEY] == SNAP
    answer.set_result({"sessionId": "late-1"})
    await await_until(collector.settled, what="the start collector settled")
    assert rt.torn_down == ["late-1"] and not fence.start_in_flight(KEY)
    assert tools._SESSION_APP_SNAPSHOT["late-1"] == SNAP
    # The next stop hands it to the close retry instead of reading "nothing left".
    assert tools._keep_left_sessions("atlas", KEY) is True
    assert (rt, "late-1") in tools._PENDING_CLOSE[("atlas", "probe")]


@pytest.mark.asyncio
async def test_a_late_id_a_stop_ran_beside_goes_to_the_close_retry_at_once():
    rt = _Runtime()
    tools._SESSION_APP_SNAPSHOT_PENDING[KEY] = SNAP
    collector, answer = _collector(rt)
    fence._START_OUTRUN.add(KEY)  # what a stop beside the start records
    answer.set_result({"sessionId": "late-2"})
    await await_until(collector.settled, what="the start collector settled")
    assert (rt, "late-2") in tools._PENDING_CLOSE[("atlas", "probe")]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["kas", "codex"])
async def test_a_late_id_the_host_closed_keeps_nothing_but_on_codex(backend):
    # Codex's close leaves the session's servers running: only the tree settles it.
    rt = _Runtime(confirms=True)
    rt.acp_backend = backend
    tools._SESSION_APP_SNAPSHOT_PENDING[KEY] = SNAP
    collector, answer = _collector(rt)
    answer.set_result({"sessionId": "late-3"})
    await await_until(collector.settled, what="the start collector settled")
    assert not tools._PENDING_CLOSE
    if backend == "kas":
        assert "late-3" not in tools._SESSION_APP_SNAPSHOT
    else:
        kept = tools._SESSION_RUNTIMES["late-3"]
        assert isinstance(kept, RuntimeTreeGone) and kept.runtime is rt


@pytest.mark.asyncio
async def test_an_answer_that_never_came_is_kept_under_the_runtime_tree():
    rt = _Runtime()
    tools._SESSION_APP_SNAPSHOT_PENDING[KEY] = SNAP
    collector, answer = _collector(rt)
    answer.set_exception(AcpRuntimeDead("transport closed"))
    await await_until(collector.settled, what="the start collector settled")
    (token,) = [t for t in tools._SESSION_APP_SNAPSHOT if is_local_close_token(t)]
    kept = tools._SESSION_RUNTIMES[token]
    assert isinstance(kept, RuntimeTreeGone) and kept.runtime is rt


@pytest.mark.asyncio
@pytest.mark.parametrize("teardown", ["terminate_session", "evict_session"])
async def test_a_teardown_inside_a_start_keeps_what_it_could_not_confirm(teardown):
    rt = _Runtime()
    tools._SESSION_APP_SNAPSHOT_PENDING[KEY] = SNAP
    rt._unconfirmed_teardowns.add("refused-1")  # the host did not answer the close

    @fence.runtime_session_start
    async def refused_start(self, *, session_key: str) -> None:
        await getattr(registry, teardown)(self, "refused-1")
        raise RuntimeError("memory acknowledgement refused")

    with pytest.raises(RuntimeError):
        await refused_start(rt, session_key=KEY)
    assert tools._SESSION_APP_SNAPSHOT["refused-1"] == SNAP
    assert tools._SESSION_RUNTIMES["refused-1"] is rt


@pytest.mark.asyncio
async def test_a_teardown_outside_a_start_keeps_nothing():
    rt = _Runtime()
    tools._SESSION_APP_SNAPSHOT_PENDING[KEY] = SNAP
    rt._unconfirmed_teardowns.add("owned-1")
    await registry.terminate_session(rt, "owned-1")
    assert "owned-1" not in tools._SESSION_APP_SNAPSHOT


def _host_refusal(message: str):
    """An error the host itself answered with: the reader keeps its frame."""
    from kiro_crew.acp.session_handle import AcpRuntimeError

    exc = AcpRuntimeError(message)
    exc.rpc_error = {"code": -32602, "message": message}
    return exc


def _in_start(fn):
    """Run *fn* as code inside a Bot chat start of KEY that took SNAP."""
    key, snap = fence._START_KEY.get(), fence._START_SNAP.get()
    fence._START_KEY.set(KEY)
    fence._START_SNAP.set((KEY, SNAP))
    try:
        return fn()
    finally:
        fence._START_KEY.set(key)
        fence._START_SNAP.set(snap)


class _Closer(_Runtime):
    """A shared runtime whose close of an abandoned session the test releases."""

    def __init__(self, *, confirms: bool, backend: str = "kas") -> None:
        super().__init__(confirms=confirms)
        self.acp_backend = backend
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.closed: list[tuple[str, str]] = []

    async def _close(self, verb: str, session_id: str) -> bool:
        self.closed.append((verb, session_id))
        self.entered.set()
        await await_until(self.gate, what="the test released the close")
        if not self.confirms:
            self._unconfirmed_teardowns.add(session_id)
        return self.confirms

    async def evict_session(self, session_id: str) -> bool:
        return await self._close("evict", session_id)

    async def terminate_session(self, session_id: str) -> None:
        await self._close("terminate", session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("confirms", [False, True])
async def test_an_abandoned_kas_load_is_evicted_and_kept_until_that_is_confirmed(confirms):
    # Release the start when the load settles, not when its close is confirmed,
    # and a stop in between reads a clean chat; close with terminate on KAS and the
    # stored conversation goes with it.
    from kiro_crew.acp.session_handle import AcpRequestTimeout

    rt = _Closer(confirms=confirms)
    pending: asyncio.Future = asyncio.get_running_loop().create_future()
    _in_start(lambda: load._abandon_load(rt, "resumed-1", pending))
    assert fence.start_in_flight(KEY) and tools.app_has_active_bot_sessions("probe")
    barrier = load._PENDING_ABANDONED_CLOSES[(id(rt), "resumed-1")]
    pending.set_exception(AcpRequestTimeout("session/load timed out"))
    try:
        await await_until(rt.entered, what="the late session's close started")
        assert fence.start_in_flight(KEY)
    finally:
        rt.gate.set()
    await await_until(barrier, what="the abandoned load settled")
    assert rt.closed == [("evict", "resumed-1")] and not fence.start_in_flight(KEY)
    assert ("resumed-1" in tools._SESSION_APP_SNAPSHOT) is not confirms


@pytest.mark.asyncio
async def test_an_abandoned_load_the_host_refused_keeps_nothing():
    rt = _Closer(confirms=False)
    pending: asyncio.Future = asyncio.get_running_loop().create_future()
    _in_start(lambda: load._abandon_load(rt, "resumed-2", pending))
    barrier = load._PENDING_ABANDONED_CLOSES[(id(rt), "resumed-2")]
    pending.set_exception(_host_refusal("session not found"))
    await await_until(barrier, what="the abandoned load settled")
    assert rt.closed == [] and not tools.app_has_active_bot_sessions("probe")


@pytest.mark.asyncio
@pytest.mark.parametrize("confirms", [False, True])
async def test_a_cancelled_bot_session_new_is_kept_to_its_end(confirms):
    # Pass a cancelled Bot start straight through, as a host with no per-session
    # state did, and its late session runs with no record and no close.
    rt = _Closer(confirms=confirms)
    request: asyncio.Future = asyncio.get_running_loop().create_future()
    started = asyncio.Event()

    async def call():
        started.set()
        return await create._kept_to_its_end(rt, request, None)

    caller = _in_start(lambda: asyncio.ensure_future(call()))
    try:
        await await_until(started, what="the start reached session/new")
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await await_until(caller, what="the cancelled start returned")
        assert not request.cancelled() and fence.start_in_flight(KEY)
        assert tools.app_has_active_bot_sessions("probe")
        request.set_result({"sessionId": "late-new"})
        await await_until(rt.entered, what="the late session's close started")
        assert fence.start_in_flight(KEY)
        (task,) = create._ABANDONED_NEW_CLOSES
    finally:
        rt.gate.set()
    # The task's own done callbacks (the settle among them) run before this await
    # resumes: they were added first.
    await await_until(task, what="the late session's close")
    assert rt.closed == [("terminate", "late-new")] and not fence.start_in_flight(KEY)
    assert ("late-new" in tools._SESSION_APP_SNAPSHOT) is not confirms


@pytest.mark.asyncio
async def test_a_load_waits_for_an_abandoned_close_of_its_id():
    # Load past it and the abandoned load's close ends the session this one restores.
    rt = _loading_runtime()
    barrier: asyncio.Future = asyncio.get_running_loop().create_future()
    load._PENDING_ABANDONED_CLOSES[(id(rt), "resumed-3")] = barrier
    loading = asyncio.ensure_future(load.load_session(rt, "", "resumed-3", session_key=KEY))
    done, _ = await asyncio.wait({loading}, timeout=0)
    assert not done
    barrier.set_result(None)
    with pytest.raises(_Reached):
        await await_until(loading, what="the load after the abandoned close")


def test_a_failed_shared_start_keeps_an_eviction_the_host_did_not_confirm():
    # Ignore ``abandon``'s eviction answer and the session's app servers outlive a
    # stop that reports it done.
    from kiro_crew.providers import acp_runtime_start as start

    rt = _Runtime()
    rt._unconfirmed_teardowns.add("left-1")
    kept: list = []
    provider = SimpleNamespace(_owning_session_key=lambda: KEY)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(start, "note_unconfirmed_teardown", lambda *a, **kw: kept.append((a, kw)))
        start._keep_abandoned_close(provider, rt, "left-1")
        start._keep_abandoned_close(provider, _Runtime(), "left-2")
    assert kept == [((provider, rt, "left-1"), {"session_key": KEY})]


def test_a_failed_shared_codex_start_keeps_its_tree_after_a_confirmed_close():
    # Codex's close leaves the session's app servers running: drop the tree arm
    # and an acknowledged close keeps nothing while those processes live on.
    from kiro_crew.providers import acp_runtime_start as start

    rt = _Runtime()
    rt.acp_backend = "codex"
    kept: list = []
    provider = SimpleNamespace(_owning_session_key=lambda: KEY)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(start, "note_unconfirmed_teardown", lambda *a, **kw: kept.append(a))
        start._keep_abandoned_close(provider, rt, "codex-1")
        rt.process_tree_confirmed_dead = True
        start._keep_abandoned_close(provider, rt, "codex-2")
    ((_p, handle, sid),) = kept
    assert sid == "codex-1" and isinstance(handle, RuntimeTreeGone) and handle.runtime is rt


class _Reached(Exception):
    """The load got past its pending-close check."""


def _loading_runtime() -> SimpleNamespace:
    async def source_agent(_agent):
        raise _Reached

    return SimpleNamespace(
        _harness=SimpleNamespace(per_session_state=False),
        acp_backend="claude",
        _agent=BOT_AGENT,
        _initialized=True,
        _can_load_session=True,
        _session_queues={},
        _source_agent=source_agent,
    )


@pytest.mark.asyncio
async def test_a_load_of_an_id_whose_close_is_pending_starts_fresh_instead():
    # Load it and the pending retry's evict_session ends the conversation the load
    # restored. Drop the ``close_pending`` check in ``load_session`` and it loads.
    from kiro_crew.acp.session_handle_runtime_contract import AcpResumeNotRestored

    rt = _loading_runtime()
    tools._PENDING_CLOSE[("atlas", "probe")] = [(rt, "kept-1")]
    with pytest.raises(AcpResumeNotRestored):
        await load.load_session(rt, "", "kept-1", session_key=KEY)
    assert "kept-1" not in rt._session_queues
    # Once the retry settled the close, the id loads again.
    tools._PENDING_CLOSE.clear()
    with pytest.raises(_Reached):
        await load.load_session(rt, "", "kept-1", session_key=KEY)


# Two sessions on one shared runtime, interleaved: what is kept for one is never
# replaced, confirmed or dropped by what happens to the other.

KEY_B = "dashboard:member-bea.memory-member-bea-1"


def test_another_chats_discard_on_the_shared_runtime_keeps_this_starts_token():
    # Merge by owner alone and the other chat's real id takes this token's place.
    from kiro_crew.agent_sdk.close_handles import close_handle, merge_close_handle

    rt = _Runtime()
    held = [close_handle(rt, None, KEY)]
    merge_close_handle(held, rt, "bea-1", KEY_B)
    assert any(is_local_close_token(s) for _h, s in held) and (rt, "bea-1") in held


def test_a_clients_token_from_its_last_process_is_not_taken_over_by_the_next():
    # Same chat, same client, a new process: the id belongs to another start.
    from kiro_crew.agent_sdk.close_handles import close_handle, merge_close_handle

    client = SimpleNamespace(_session_key=KEY, _process=object())
    held = [close_handle(client, None, KEY)]
    client._process = object()
    merge_close_handle(held, client, "next-1", KEY)
    assert any(is_local_close_token(s) for _h, s in held)
    # The process that kept the token reports its own id: that one takes over.
    client = SimpleNamespace(_session_key=KEY, _process=object())
    held = [close_handle(client, None, KEY)]
    merge_close_handle(held, client, "own-1", KEY)
    assert held == [(client, "own-1")]


@pytest.mark.asyncio
async def test_a_pending_close_refuses_the_load_of_its_id_on_its_own_runtime_only():
    from kiro_crew.acp.session_handle_runtime_contract import AcpResumeNotRestored

    mine, other = _loading_runtime(), _loading_runtime()
    tools._PENDING_CLOSE[("atlas", "probe")] = [(RuntimeTreeGone(mine), "same-1")]
    with pytest.raises(AcpResumeNotRestored):
        await load.load_session(mine, "", "same-1", session_key=KEY)
    with pytest.raises(_Reached):
        await load.load_session(other, "", "same-1", session_key=KEY_B)


@pytest.mark.asyncio
async def test_another_runtimes_pending_close_of_the_same_id_does_not_hide_this_teardown():
    mine, other = _Runtime(), _Runtime()
    tools._SESSION_APP_SNAPSHOT_PENDING[KEY] = SNAP
    tools._PENDING_CLOSE[("atlas", "probe")] = [(other, "same-2")]
    mine._unconfirmed_teardowns.add("same-2")

    @fence.runtime_session_start
    async def refused_start(self, *, session_key: str) -> None:
        await registry.terminate_session(self, "same-2")
        raise RuntimeError("refused")

    with pytest.raises(RuntimeError):
        await refused_start(mine, session_key=KEY)
    assert tools._SESSION_RUNTIMES["same-2"] is mine


def test_a_confirmed_close_on_another_runtime_keeps_this_record():
    from kiro_crew.acp.session_provider_shared import teardown_confirmed

    mine, other = _Runtime(), _Runtime()
    tools._SESSION_APP_SNAPSHOT["same-3"] = SNAP
    tools._SESSION_RUNTIMES["same-3"] = mine
    assert teardown_confirmed(other, "same-3", killed=False) is True
    assert tools._SESSION_APP_SNAPSHOT["same-3"] == SNAP
    assert teardown_confirmed(mine, "same-3", killed=False) is True
    assert "same-3" not in tools._SESSION_APP_SNAPSHOT


# Each start keeps the app snapshot it took itself: a later start of the same chat
# records and binds its own, never the one a start still waiting on its answer uses.


async def _timed_out_start(rt: _Runtime) -> tuple[StartCollector, asyncio.Future]:
    held: list = []

    @fence.runtime_session_start
    async def start(self, *, session_key: str) -> None:
        fence.capture_start_snapshot(session_key)
        held.append(_collector(self))
        raise TimeoutError("session/new outlived its budget")

    with pytest.raises(TimeoutError):
        await start(rt, session_key=KEY)
    return held[0]


@pytest.mark.asyncio
async def test_a_replacement_start_does_not_take_the_waiting_starts_snapshot():
    # Bind the late session to the chat's pending entry and the replacement's
    # snapshot (no app) is all it finds: drop ``capture_start_snapshot`` and this reds.
    rt = _Runtime()
    tools._SESSION_APP_SNAPSHOT_PENDING[KEY] = SNAP
    collector, answer = await _timed_out_start(rt)
    tools._SESSION_APP_SNAPSHOT_PENDING[KEY] = ("atlas", frozenset())  # the replacement
    tools.bind_snapshot_to_native_sid(KEY, "replacement-1", rt)
    # While the collector waits, a stop still sees the app: drop
    # ``hold_start_record`` and the app reads as not running here.
    assert tools.app_has_active_bot_sessions("probe")
    answer.set_result({"sessionId": "late-9"})
    await await_until(collector.settled, what="the start collector settled")
    assert tools._SESSION_APP_SNAPSHOT["late-9"] == SNAP
    assert tools._keep_left_sessions("atlas", KEY) is True
    assert (rt, "late-9") in tools._PENDING_CLOSE[("atlas", "probe")]


@pytest.mark.asyncio
@pytest.mark.parametrize("confirms", [False, True])
async def test_a_stop_while_the_start_waits_settles_on_that_starts_answer(confirms):
    # The stop hands the start's token on; the late id takes its place, or the
    # host's confirmed close drops it. Settle by owner alone and either another
    # session's answer moves it, or nothing does and it waits on the tree forever.
    rt = _Runtime(confirms=confirms)
    tools._SESSION_APP_SNAPSHOT_PENDING[KEY] = SNAP
    collector, answer = await _timed_out_start(rt)
    assert tools._keep_left_sessions("atlas", KEY) is True
    (token,) = [s for _h, s in tools._PENDING_CLOSE[("atlas", "probe")]]
    assert is_local_close_token(token)
    answer.set_result({"sessionId": "late-10"})
    await await_until(collector.settled, what="the start collector settled")
    held = tools._PENDING_CLOSE[("atlas", "probe")]
    assert held == ([] if confirms else [(rt, "late-10")])


def test_a_folded_codex_alias_is_withheld_under_the_offered_name():
    # after_app_change reads the offered name ("probe:my server"); key it by the
    # delivered one ("probe:my_server") and the chat reloads until the close settles.
    from kiro_crew.providers.mirrors.codex import codex_name

    tools._PROJECTION_GENS[KEY] = {"probe:my server": 0}
    tools._PROJECTION_SLUGS[KEY] = "atlas"
    tools._PENDING_CLOSE_APPS.add(("atlas", "probe"))
    tools.check_and_filter_projection(
        KEY, {"probe:my_server": {"command": "probe"}}, name_transform=codex_name
    )
    assert set(tools._PENDING_CLOSE_WITHHELD["atlas"]) == {"probe:my server"}


def test_a_killed_runtime_confirms_only_its_own_pending_close_of_an_id():
    # Confirm by id alone and the old runtime's close of the same id goes too,
    # while that runtime's app server may still run.
    old, replacement = _Runtime(), _Runtime()
    tools._PENDING_CLOSE[("atlas", "probe")] = [
        (RuntimeTreeGone(old), "kept-9"),
        (replacement, "kept-9"),
    ]
    tools._PENDING_CLOSE_APPS.add(("atlas", "probe"))
    tools.confirm_last_lease_killed("kept-9", replacement)
    held = tools._PENDING_CLOSE[("atlas", "probe")]
    assert len(held) == 1 and held[0][1] == "kept-9" and held[0][0].runtime is old


def test_another_owners_close_of_an_id_leaves_this_owners_record():
    # Take the record by id alone and a close on the replacement runtime hands the
    # old runtime's session to a retry that asks the wrong process.
    old, replacement = _Runtime(), _Runtime()
    tools._SESSION_APP_SNAPSHOT["kept-10"] = SNAP
    tools._SESSION_RUNTIMES["kept-10"] = old
    tools._SESSION_APP_SNAPSHOT_PENDING[KEY] = SNAP
    tools.note_unconfirmed_close(KEY, replacement, "kept-10")
    assert tools._SESSION_RUNTIMES["kept-10"] is old
    assert tools._SESSION_APP_SNAPSHOT["kept-10"] == SNAP
