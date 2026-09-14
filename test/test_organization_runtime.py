"""The scheduler consumes authoritative turn results and preserves retry state."""

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from member_memory_helpers import patch_private_memory_supported

from kiro_crew.config import KiroCrewConfig
from kiro_crew.monitoring.completion import MonitorCompletionHook
from kiro_crew.monitoring.models import MonitorActionDisposition
from kiro_crew.organization import DEFAULT_STAFFING, OWNER, OrganizationStore
from kiro_crew.organization_runtime import OrganizationRunner
from kiro_crew.session import SessionClosingError, SessionManager


@pytest.fixture
def real_organization_delivery(tmp_path, monkeypatch):
    return lambda: _real_organization_delivery(tmp_path, monkeypatch)


@asynccontextmanager
async def _real_organization_delivery(tmp_path, monkeypatch):
    """Real private-member chat and session lifecycle with an inert provider."""
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO
    from kiro_crew.agent_sdk.capabilities import capabilities_for
    from kiro_crew.context import ContextBuilder, release_cached_memory_store
    from kiro_crew.dashboard import chat_runner
    from kiro_crew.dashboard.chat_utils import effective_session_key
    from kiro_crew.dashboard.handlers.members import ensure_member_thread
    from kiro_crew.dashboard.state import SlotOrigin
    from kiro_crew.history import ConversationLog
    from kiro_crew.organization import organization_path
    from kiro_crew.organization_service import create_member
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
    from kiro_crew.session import _Session

    patch_private_memory_supported(monkeypatch)
    store = OrganizationStore(organization_path())
    member = await asyncio.to_thread(
        lambda: create_member(store, OWNER, role="conductor", manager_id=None, name="Conductor")
    )
    store.configure(
        OWNER,
        revision=store.snapshot()["settings"]["revision"],
        concurrency=1,
        enabled=True,
        staffing=DEFAULT_STAFFING,
    )
    store.message(OWNER, member, "A material update")
    queued_id = store.snapshot()["runs"][0]["id"]
    state = _make_state(tmp_path / "sessions")
    state.conversation_log = ConversationLog()
    state.broadcast_ws = Mock()
    state.push_slots_update = Mock()
    state.push_refresh = Mock()
    state.context_builder = ContextBuilder(conversation_log=state.conversation_log)
    state.context_builder.build_message = Mock(side_effect=lambda message, *_a, **_k: (message, []))
    state.consolidator = None
    state._hook_store = None
    state._yolo = False
    state.slack_client = None
    sessions = state.sessions = SessionManager(KiroCrewConfig.load())
    response = await ensure_member_thread(state, "conductor", origin=SlotOrigin.SYSTEM)
    assert response.status == 200, response.text
    slot = state._slots[json.loads(response.text)["slot_key"]]
    slot._titled = True
    key = effective_session_key(slot)
    streams = []

    async def stream(message):
        streams.append(message)
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="done")
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

    client = MagicMock(
        _private_memory=True,
        client=None,
        pid=None,
        capabilities=capabilities_for(ACP_BACKEND_KIRO),
    )
    client.stream = stream
    client.is_process_alive.return_value = True
    client.has_active_turn.return_value = False
    client.has_unfinished_turn.return_value = False
    client.context_usage_pct.return_value = 0.0
    client.context_used_tokens.return_value = 0
    client.context_window_tokens.return_value = 0
    client.runtime_info.return_value = (None, None)
    client.shutdown = AsyncMock()
    warm = _Session(provider=client, consecutive_failures=2)
    sessions._sessions[key] = warm
    failure = AsyncMock(wraps=sessions.record_failure)
    monkeypatch.setattr(sessions, "record_failure", failure)
    monkeypatch.setattr(chat_runner, "generate_session_summary", AsyncMock(return_value=None))
    runners = []

    def new_runner():
        runner = OrganizationRunner(state, store)
        runners.append(runner)
        return runner

    try:
        yield SimpleNamespace(
            store=store,
            state=state,
            sessions=sessions,
            slot=slot,
            key=key,
            client=client,
            warm=warm,
            streams=streams,
            failure=failure,
            queued_id=queued_id,
            new_runner=new_runner,
        )
    finally:
        try:
            for runner in runners:
                await asyncio.wait_for(runner.close(), timeout=5)
        finally:
            try:
                await asyncio.wait_for(sessions.close_all(), timeout=5)
            finally:
                await asyncio.to_thread(release_cached_memory_store, slot.memory_store)


@pytest.mark.asyncio
async def test_real_chat_allocation_refusal_survives_update_resume(
    real_organization_delivery, monkeypatch
):
    from kiro_crew.organization_runtime import wait_for_memory_preparation
    from kiro_crew.slack.gateway import GatewayOrchestrator

    async with real_organization_delivery() as delivery:
        store, state, sessions = delivery.store, delivery.state, delivery.sessions
        key, client, warm = delivery.key, delivery.client, delivery.warm
        streams, failure, queued_id = delivery.streams, delivery.failure, delivery.queued_id

        # Permit one scheduler poll at a time so the returned durable claim can be
        # inspected before its next delivery.
        polls = asyncio.Semaphore(1)

        async def memory_ready(task):
            await wait_for_memory_preparation(task)
            await asyncio.wait_for(polls.acquire(), timeout=5)

        monkeypatch.setattr(
            "kiro_crew.organization_runtime.wait_for_memory_preparation", memory_ready
        )
        pause_waiting, allocation_waiting = asyncio.Event(), asyncio.Event()
        pause = sessions.pause_turn_admission_for_update
        allocate = sessions.get_or_create

        async def observed_pause():
            pause_waiting.set()
            return await pause()

        async def observed_allocate(*args, **kwargs):
            allocation_waiting.set()
            return await allocate(*args, **kwargs)

        monkeypatch.setattr(sessions, "pause_turn_admission_for_update", observed_pause)
        monkeypatch.setattr(sessions, "get_or_create", observed_allocate)
        updater = object.__new__(GatewayOrchestrator)
        updater.sessions = sessions
        updater.dashboard_state = state
        updater._session_tasks = {}
        updater.subagent_mgr = None
        updater._running_script_ids = set()
        updater.task_runner = None
        updater._schedule_inbound_replay = Mock()
        updater._update_apply_deferred = False
        runner = delivery.new_runner()
        updating = None
        # Model another session's awaited telemetry write holding the registry.
        # Pause queues first, allocation second, then updater resume queues behind
        # allocation: the allocation refuses, but admission reopens before delivery ends.
        await sessions._lock.acquire()
        registry_held = True
        try:
            updating = asyncio.create_task(updater._prepare_auto_update_apply(mandatory=False))
            await asyncio.wait_for(pause_waiting.wait(), timeout=5)
            await runner.start()
            await asyncio.wait_for(allocation_waiting.wait(), timeout=5)
            sessions._lock.release()
            registry_held = False
            assert not await asyncio.wait_for(updating, timeout=5)
            await _wait_until(lambda: not runner._turns)
            assert not sessions.admission_closed
            assert (await asyncio.to_thread(store.snapshot))["runs"][0]["state"] == "queued"
            assert streams == []
            failure.assert_not_awaited()
            assert sessions.get_provider(key) is client
            assert warm.consecutive_failures == 2
            assert sessions.inbound_callback_count == 0
            polls.release()
            await _wait_until(lambda: bool(streams) and not runner._turns)
            runs = (await asyncio.to_thread(store.snapshot))["runs"]
            assert [(run["id"], run["state"]) for run in runs] == [(queued_id, "completed")]
            assert len(streams) == 1
            failure.assert_not_awaited()
        finally:
            if registry_held:
                sessions._lock.release()
            if updating is not None and not updating.done():
                updating.cancel()
                await asyncio.gather(updating, return_exceptions=True)
            await asyncio.wait_for(runner.close(), timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatched", [False, True], ids=["preparation", "accepted"])
async def test_shutdown_drains_real_chat_that_swallows_cancellation(
    real_organization_delivery, monkeypatch, dispatched
):
    from kiro_crew.dashboard import chat_runner

    async with real_organization_delivery() as delivery:
        entered, release = asyncio.Event(), asyncio.Event()
        cleaning, release_cleanup = asyncio.Event(), asyncio.Event()
        allocate = delivery.sessions.get_or_create
        stream = delivery.client.stream
        consume = chat_runner._consume_pending_reset
        child = None

        async def prepare(*args, **kwargs):
            if not dispatched and not entered.is_set():
                entered.set()
                await asyncio.wait_for(release.wait(), timeout=5)
            return await allocate(*args, **kwargs)

        async def provider_stream(message):
            if dispatched and not entered.is_set():
                delivery.streams.append(message)
                entered.set()
                await asyncio.wait_for(release.wait(), timeout=5)
            async for event in stream(message):
                yield event

        async def cleanup(*args, allow_discard=False, **kwargs):
            if allow_discard and asyncio.current_task() is child:
                cleaning.set()
                await asyncio.wait_for(release_cleanup.wait(), timeout=5)
            return await consume(*args, allow_discard=allow_discard, **kwargs)

        monkeypatch.setattr(delivery.sessions, "get_or_create", prepare)
        monkeypatch.setattr(delivery.client, "stream", provider_stream)
        monkeypatch.setattr(chat_runner, "_consume_pending_reset", cleanup)
        runner = delivery.new_runner()
        closing = None
        try:
            await runner.start()
            await asyncio.wait_for(entered.wait(), timeout=5)
            child = delivery.slot.task
            assert child is not None and not child.done()
            assert len(delivery.streams) == int(dispatched)
            assert delivery.sessions.inbound_callback_count == 1
            closing = asyncio.create_task(runner.close())
            await asyncio.wait_for(cleaning.wait(), timeout=5)
            # The real chat runner has caught cancellation but still owns cleanup.
            assert child.cancelling()
            assert not child.done()
            assert not closing.done()
            assert delivery.sessions.inbound_callback_count == 1
            assert (await asyncio.to_thread(delivery.store.snapshot))["runs"][0][
                "state"
            ] == "running"
            release_cleanup.set()
            await asyncio.wait_for(closing, timeout=5)
            assert child.done() and not child.cancelled()
            assert child.result() is None
            assert child not in delivery.state._background_tasks
            assert not runner._turns
            assert not delivery.sessions.is_busy(delivery.key)
            assert delivery.sessions.inbound_callback_count == 0
            result = (await asyncio.to_thread(delivery.store.snapshot))["runs"][0]
            assert result["state"] == ("interrupted" if dispatched else "queued")
            delivery.failure.assert_not_awaited()

            # Run a real scheduler poll after restart even in the no-replay case.
            claim = delivery.store.claim_run
            polled = asyncio.Event()
            loop = asyncio.get_running_loop()

            def observed_claim(**kwargs):
                run = claim(**kwargs)
                loop.call_soon_threadsafe(polled.set)
                return run

            monkeypatch.setattr(delivery.store, "claim_run", observed_claim)
            restarted = delivery.new_runner()
            await restarted.start()
            await asyncio.wait_for(polled.wait(), timeout=5)
            await _wait_until(lambda: not restarted._turns)
            runs = (await asyncio.to_thread(delivery.store.snapshot))["runs"]
            assert [(run["id"], run["state"]) for run in runs] == [
                (delivery.queued_id, "interrupted" if dispatched else "completed")
            ]
            assert len(delivery.streams) == 1
            assert await asyncio.to_thread(delivery.store.claim_run) is None
            assert delivery.sessions.inbound_callback_count == 0
        finally:
            release.set()
            release_cleanup.set()
            if closing is not None:
                await asyncio.wait_for(closing, timeout=5)


@pytest.fixture
def organization_delivery(tmp_path, monkeypatch):
    store = OrganizationStore(tmp_path / "organization.sqlite3")
    member = store.enroll(
        OWNER, name="Conductor", memory_store="private-root", role="conductor", manager_id=None
    )
    store.configure(
        OWNER,
        revision=store.snapshot()["settings"]["revision"],
        concurrency=1,
        enabled=True,
        staffing=DEFAULT_STAFFING,
    )
    store.message(OWNER, member, "A material update")
    slot = SimpleNamespace(
        key="member-root",
        agent="Conductor",
        memory_store="private-root",
        running=False,
        task=None,
        _lock=asyncio.Lock(),
        append=Mock(),
    )
    sessions = SessionManager(KiroCrewConfig())
    state = SimpleNamespace(
        _slots={slot.key: slot}, _background_tasks=set(), sessions=sessions, push_refresh=Mock()
    )
    opener = AsyncMock(return_value=web.json_response({"slot_key": slot.key}))
    monkeypatch.setattr("kiro_crew.organization_runtime.verify_member", lambda _: None)
    monkeypatch.setattr("kiro_crew.dashboard.handlers.members.ensure_member_thread", opener)
    turns = []

    async def chat(_state, _slot, _notice, *, monitor_completion, _synthetic_payload):
        if not await monitor_completion.authorize():
            return
        try:
            sessions.begin_turn(slot.key)
        except SessionClosingError:
            monitor_completion.mark_admission_refused()
            return
        monitor_completion.mark_accepted()
        turns.append(monitor_completion.monitor_id)
        await monitor_completion.complete(MonitorActionDisposition.SUCCESS)

    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", chat)
    return SimpleNamespace(
        store=store,
        state=state,
        slot=slot,
        sessions=sessions,
        opener=opener,
        turns=turns,
        runner=OrganizationRunner(state, store),
    )


async def _wait_until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout=5)


@pytest.mark.asyncio
async def test_update_pause_preserves_queued_wake_until_resume(organization_delivery, monkeypatch):
    delivery = organization_delivery
    from kiro_crew.organization_runtime import wait_for_memory_preparation

    ticks = 0

    async def memory_ready(task):
        nonlocal ticks
        await wait_for_memory_preparation(task)
        ticks += 1

    monkeypatch.setattr("kiro_crew.organization_runtime.wait_for_memory_preparation", memory_ready)
    claim = Mock(wraps=delivery.store.claim_run)
    monkeypatch.setattr(delivery.store, "claim_run", claim)
    queued = delivery.store.snapshot()["runs"]
    assert await delivery.sessions.pause_turn_admission_for_update()
    try:
        await delivery.runner.start()
        await _wait_until(lambda: ticks >= 2)
        claim.assert_not_called()
        assert (await asyncio.to_thread(delivery.store.snapshot))["runs"] == queued
        delivery.opener.assert_not_awaited()
        delivery.slot.append.assert_not_called()
        assert delivery.sessions.inbound_callback_count == 0

        await delivery.sessions.resume_turn_admission_after_update()
        await _wait_until(lambda: bool(delivery.turns) and not delivery.runner._turns)
        assert delivery.turns == [queued[0]["id"]]
        assert (await asyncio.to_thread(delivery.store.snapshot))["runs"][0]["state"] == "completed"
        assert delivery.sessions.inbound_callback_count == 0
    finally:
        await asyncio.wait_for(delivery.runner.close(), timeout=5)
        await delivery.sessions.resume_turn_admission_after_update()


@pytest.mark.asyncio
async def test_preparation_reservation_makes_actual_updater_defer(
    organization_delivery, monkeypatch
):
    from kiro_crew.slack.gateway import GatewayOrchestrator

    delivery = organization_delivery
    entered, release = asyncio.Event(), asyncio.Event()

    async def prepare(*_args, **_kwargs):
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=5)
        return web.json_response({"slot_key": delivery.slot.key})

    monkeypatch.setattr("kiro_crew.dashboard.handlers.members.ensure_member_thread", prepare)
    # Keep the real updater's admission decision and census, with no boot or transports.
    updater = object.__new__(GatewayOrchestrator)
    updater.sessions = delivery.sessions
    updater.dashboard_state = delivery.state
    updater._session_tasks = {}
    updater.subagent_mgr = None
    updater._running_script_ids = set()
    updater.task_runner = None
    updater._schedule_inbound_replay = Mock()
    updater._update_apply_deferred = False
    try:
        await delivery.runner.start()
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert delivery.slot.task is None
        assert updater._in_flight_work_counts() == (0, 1)
        assert not await asyncio.wait_for(
            updater._prepare_auto_update_apply(mandatory=False), timeout=5
        )
        assert updater._update_apply_deferred
        assert not delivery.sessions.admission_closed
        delivery.state.push_refresh.assert_called_once_with("update_available")
        release.set()
        await _wait_until(lambda: bool(delivery.turns) and not delivery.runner._turns)
        assert len(delivery.turns) == 1
        assert updater._in_flight_work_counts() == (0, 0)
    finally:
        release.set()
        await asyncio.wait_for(delivery.runner.close(), timeout=5)
        await delivery.sessions.resume_turn_admission_after_update()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["thread-open", "allocation", "authorization", "dispatched"])
async def test_update_closing_during_preparation_only_requeues_before_dispatch(
    organization_delivery, monkeypatch, stage
):
    delivery = organization_delivery
    from kiro_crew.dashboard.chat_runner import _run_chat

    attempted = asyncio.Event()

    async def chat(*args, monitor_completion, **kwargs):
        if attempted.is_set():
            return await _run_chat(*args, monitor_completion=monitor_completion, **kwargs)
        attempted.set()
        if stage == "dispatched":
            assert await monitor_completion.authorize()
            monitor_completion.mark_accepted()
            delivery.turns.append(monitor_completion.monitor_id)
        assert await delivery.sessions.pause_turn_admission_for_update()
        if stage == "allocation":
            # The real allocation gate refuses before a provider exists; the
            # dashboard catches this exception and returns without acceptance.
            with pytest.raises(SessionClosingError):
                await delivery.sessions.get_or_create(delivery.slot.key)
            monitor_completion.mark_admission_refused()
        elif stage == "authorization":
            assert not await monitor_completion.authorize()
            # Keep the refusal even if the updater reopens before chat unwinds.
            await delivery.sessions.resume_turn_admission_after_update()

    async def prepare(*_args, **_kwargs):
        if not attempted.is_set():
            assert await delivery.sessions.pause_turn_admission_for_update()
            attempted.set()
        return web.json_response({"slot_key": delivery.slot.key})

    if stage == "thread-open":
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.ensure_member_thread", prepare)
    else:
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", chat)
    try:
        await delivery.runner.start()
        await asyncio.wait_for(attempted.wait(), timeout=5)
        await _wait_until(lambda: not delivery.runner._turns)
        result = (await asyncio.to_thread(delivery.store.snapshot))["runs"][0]
        assert result["state"] == ("failed" if stage == "dispatched" else "queued")
        assert delivery.sessions.inbound_callback_count == 0
        if stage == "thread-open":
            delivery.slot.append.assert_not_called()
        if stage != "dispatched":
            await delivery.sessions.resume_turn_admission_after_update()
            await _wait_until(lambda: bool(delivery.turns) and not delivery.runner._turns)
            assert delivery.turns == [result["id"]]
            assert (await asyncio.to_thread(delivery.store.snapshot))["runs"][0][
                "state"
            ] == "completed"
        else:
            assert "No authoritative completion" in result["error"]
            await delivery.sessions.resume_turn_admission_after_update()
            assert await asyncio.to_thread(delivery.store.claim_run) is None
            assert delivery.turns == [result["id"]]
    finally:
        await asyncio.wait_for(delivery.runner.close(), timeout=5)
        await delivery.sessions.resume_turn_admission_after_update()


@pytest.mark.asyncio
async def test_cancel_before_admission_task_starts_releases_without_claiming(
    organization_delivery, monkeypatch
):
    delivery = organization_delivery
    create_task = asyncio.create_task
    cancelled = []
    claim = Mock(wraps=delivery.store.claim_run)
    monkeypatch.setattr(delivery.store, "claim_run", claim)

    def create(coroutine, *, name=None, **kwargs):
        task = create_task(coroutine, name=name, **kwargs)
        if name == "organization-admission":
            assert delivery.sessions.inbound_callback_count == 1
            task.cancel()
            cancelled.append(task)
        return task

    monkeypatch.setattr("kiro_crew.organization_runtime.asyncio.create_task", create)
    try:
        await delivery.runner.start()
        await _wait_until(lambda: bool(cancelled) and not delivery.runner._turns)
        assert cancelled[0].cancelled()
        assert delivery.sessions.inbound_callback_count == 0
        claim.assert_not_called()
        assert (await asyncio.to_thread(delivery.store.snapshot))["runs"][0]["state"] == "queued"
    finally:
        await asyncio.wait_for(delivery.runner.close(), timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage,expected", [("claim", "queued"), ("finish", "completed"), ("return", "queued")]
)
async def test_shutdown_drains_claim_and_durable_writes_before_releasing_reservation(
    organization_delivery, monkeypatch, stage, expected
):
    delivery = organization_delivery
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    operation = {"claim": "claim_run", "finish": "finish_run", "return": "return_run"}[stage]
    original = getattr(delivery.store, operation)
    reservations = []

    def blocked(*args, **kwargs):
        # In the claim case, shutdown races the interval AFTER SQLite commits.
        result = original(*args, **kwargs) if stage == "claim" else None
        reservations.append(delivery.sessions.inbound_callback_count)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "test did not release the storage worker"
        return result if stage == "claim" else original(*args, **kwargs)

    monkeypatch.setattr(delivery.store, operation, blocked)
    if stage == "return":
        delivery.opener.side_effect = lambda *_args, **_kwargs: web.json_response(
            {"code": "member_slot_conflict"}, status=409
        )
    closing = None
    try:
        await delivery.runner.start()
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert reservations == [1]
        assert delivery.sessions.inbound_callback_count == 1
        closing = asyncio.create_task(delivery.runner.close())
        await _wait_until(lambda: all(task.cancelling() for task in delivery.runner._turns))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(closing), timeout=0.05)
        assert not closing.done()
        assert delivery.sessions.inbound_callback_count == 1
    finally:
        release.set()
        await asyncio.wait_for(closing or delivery.runner.close(), timeout=5)
    assert delivery.sessions.inbound_callback_count == 0
    assert not delivery.runner._turns
    assert (await asyncio.to_thread(delivery.store.snapshot))["runs"][0]["state"] == expected
    assert len(delivery.turns) == (1 if stage == "finish" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatched", [False, True], ids=["preparation", "dispatched"])
async def test_shutdown_only_returns_a_claim_without_provider_dispatch(
    organization_delivery, monkeypatch, dispatched
):
    delivery = organization_delivery
    entered = asyncio.Event()

    async def blocked(*_args, monitor_completion=None, **_kwargs):
        if dispatched:
            assert await monitor_completion.authorize()
            monitor_completion.mark_accepted()
            delivery.turns.append(monitor_completion.monitor_id)
        entered.set()
        await asyncio.wait_for(asyncio.Event().wait(), timeout=5)

    target = (
        "kiro_crew.dashboard.chat_runner._run_chat"
        if dispatched
        else "kiro_crew.dashboard.handlers.members.ensure_member_thread"
    )
    monkeypatch.setattr(target, blocked)
    try:
        await delivery.runner.start()
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert delivery.sessions.inbound_callback_count == 1
    finally:
        await asyncio.wait_for(delivery.runner.close(), timeout=5)
    assert delivery.sessions.inbound_callback_count == 0
    assert (await asyncio.to_thread(delivery.store.snapshot))["runs"][0]["state"] == (
        "interrupted" if dispatched else "queued"
    )
    if dispatched:
        assert await asyncio.to_thread(delivery.store.claim_run) is None
    assert len(delivery.turns) == int(dispatched)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["empty", "claim-error", "preparation-error", "storage-error"])
async def test_admission_reservation_releases_on_empty_claim_and_errors(
    organization_delivery, monkeypatch, outcome
):
    delivery = organization_delivery
    attempted = asyncio.Event()
    original = delivery.store.claim_run
    loop = asyncio.get_running_loop()

    def claim(**kwargs):
        assert delivery.sessions.inbound_callback_count == 1
        loop.call_soon_threadsafe(attempted.set)
        if outcome == "empty":
            return None
        if outcome == "claim-error":
            raise OSError("Claim unavailable")
        return original(**kwargs)

    monkeypatch.setattr(delivery.store, "claim_run", claim)
    if outcome == "preparation-error":
        delivery.opener.side_effect = ValueError("Member unavailable")
    if outcome == "storage-error":
        monkeypatch.setattr(
            delivery.store, "finish_run", Mock(side_effect=OSError("Write unavailable"))
        )
    try:
        await delivery.runner.start()
        await asyncio.wait_for(attempted.wait(), timeout=5)
        await _wait_until(lambda: not delivery.runner._turns)
        assert delivery.sessions.inbound_callback_count == 0
        result = (await asyncio.to_thread(delivery.store.snapshot))["runs"][0]
        assert (
            result["state"]
            == {
                "empty": "queued",
                "claim-error": "queued",
                "preparation-error": "failed",
                "storage-error": "running",
            }[outcome]
        )
        if outcome == "preparation-error":
            assert result["error"] == "Member unavailable"
    finally:
        await asyncio.wait_for(delivery.runner.close(), timeout=5)
    assert delivery.sessions.inbound_callback_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "completion,fail_first_write",
    [(True, False), (False, False), (True, True)],
    ids=["success", "missing-completion", "swallowed-write-failure"],
)
async def test_delivery_uses_private_thread_and_requires_authoritative_completion(
    tmp_path, monkeypatch, completion, fail_first_write
):
    store = OrganizationStore(tmp_path / "organization.sqlite3")
    member = store.enroll(
        OWNER, name="Conductor", memory_store="private-root", role="conductor", manager_id=None
    )
    store.configure(
        OWNER,
        revision=store.snapshot()["settings"]["revision"],
        concurrency=1,
        enabled=True,
        staffing=DEFAULT_STAFFING,
    )
    store.assign(OWNER, member, title="Work", acceptance="Evidence")
    run = store.claim_run()
    messages = []
    slot = SimpleNamespace(
        agent="Conductor",
        memory_store="private-root",
        running=False,
        _lock=asyncio.Lock(),
        append=lambda *args, **kwargs: messages.append((args, kwargs)),
    )
    state = SimpleNamespace(
        _slots={"member-root": slot},
        _background_tasks=set(),
        sessions=SessionManager(KiroCrewConfig()),
    )
    monkeypatch.setattr("kiro_crew.organization_runtime.verify_member", lambda _: None)
    opener = AsyncMock(return_value=web.json_response({"slot_key": "member-root"}))
    monkeypatch.setattr("kiro_crew.dashboard.handlers.members.ensure_member_thread", opener)
    finish_run = store.finish_run
    writes = []
    swallowed = []

    def finish(run_id, error=""):
        writes.append(error)
        if fail_first_write and len(writes) == 1:
            raise OSError("The completion write failed.")
        finish_run(run_id, error)

    monkeypatch.setattr(store, "finish_run", finish)

    async def turn(_state, _slot, notice, *, monitor_completion, _synthetic_payload):
        assert _synthetic_payload
        assert isinstance(monitor_completion, MonitorCompletionHook)
        assert await monitor_completion.authorize()
        monitor_completion.mark_accepted()
        assert "org_inbox" in notice
        if completion:
            try:
                await monitor_completion.complete(MonitorActionDisposition.SUCCESS)
            except OSError:
                # The chat runner logs callback failures and finishes its turn.
                swallowed.append(True)

    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", turn)
    await asyncio.wait_for(OrganizationRunner(state, store)._deliver(run), timeout=5)
    result = store.snapshot()["runs"][0]
    assert result["state"] == ("completed" if completion and not fail_first_write else "failed")
    assert swallowed == ([True] if fail_first_write else [])
    if fail_first_write:
        assert len(writes) == 2
        assert writes[0] == ""
        assert "No authoritative completion" in writes[1]
        assert result["error"] == writes[1]
    assert messages[0][0][0] == "inject"
    # Provider completion cannot accept the business assignment.
    assert store.snapshot()["tasks"][0]["state"] == "queued"
    store.message(OWNER, member, "Another material update")
    next_run = store.claim_run()
    assert next_run is not None, "the completed delivery must release its running claim"
    assert next_run["member_id"] == member
    assert next_run["id"] != run["id"]


@pytest.mark.asyncio
async def test_busy_member_thread_returns_the_claim_without_losing_work(tmp_path, monkeypatch):
    store = OrganizationStore(tmp_path / "organization.sqlite3")
    member = store.enroll(
        OWNER, name="Conductor", memory_store="private-root", role="conductor", manager_id=None
    )
    store.configure(
        OWNER,
        revision=store.snapshot()["settings"]["revision"],
        concurrency=1,
        enabled=True,
        staffing=DEFAULT_STAFFING,
    )
    store.message(OWNER, member, "A material update")
    run = store.claim_run()
    monkeypatch.setattr("kiro_crew.organization_runtime.verify_member", lambda _: None)
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.members.ensure_member_thread",
        AsyncMock(return_value=web.json_response({"code": "member_slot_conflict"}, status=409)),
    )
    state = SimpleNamespace(sessions=SessionManager(KiroCrewConfig()))
    await OrganizationRunner(state, store)._deliver(run)
    assert store.snapshot()["runs"][0]["state"] == "queued"
    assert store.claim_run()["id"] == run["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", [False, True])
async def test_optional_recovery_starts_after_bind_and_cannot_break_dashboard(
    tmp_path, monkeypatch, corrupt
):
    from kiro_crew import organization_runtime
    from kiro_crew.dashboard import server

    path = tmp_path / "organization.sqlite3"
    if corrupt:
        path.write_bytes(b"not a SQLite database")
    else:
        await asyncio.to_thread(OrganizationStore(path).snapshot)
    monkeypatch.setattr(organization_runtime, "organization_path", lambda: path)
    recovered = []
    original = OrganizationStore.recover_runs

    def recover(store):
        recovered.append(True)
        return original(store)

    monkeypatch.setattr(OrganizationStore, "recover_runs", recover)
    app = web.Application()
    app["state"] = SimpleNamespace(
        _slots={}, _background_tasks=set(), sessions=SessionManager(KiroCrewConfig())
    )
    app.cleanup_ctx.append(server._organization_lifecycle)

    async def healthy(_request):
        return web.json_response({"ready": True})

    app.router.add_get("/health", healthy)
    async with TestClient(TestServer(app)) as client:
        assert recovered == [], "runner.setup must not touch organization storage"
        assert (await client.get("/health")).status == 200
        server._kick_organization(app)
        await asyncio.wait_for(app["organization_startup_task"], timeout=5)
        assert recovered == [True]
        assert ("organization_runner" in app) is not corrupt
        assert (await client.get("/health")).status == 200


@pytest.mark.asyncio
async def test_shutdown_drains_recovery_before_releasing_app_state(tmp_path, monkeypatch):
    from kiro_crew import organization_runtime
    from kiro_crew.dashboard import server

    path = tmp_path / "organization.sqlite3"
    await asyncio.to_thread(OrganizationStore(path).snapshot)
    monkeypatch.setattr(organization_runtime, "organization_path", lambda: path)
    entered = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    loop = asyncio.get_running_loop()
    original = OrganizationStore.recover_runs

    def recover(store):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "test did not release the recovery worker"
        try:
            return original(store)
        finally:
            finished.set()

    monkeypatch.setattr(OrganizationStore, "recover_runs", recover)
    app = web.Application()
    app["state"] = SimpleNamespace(
        _slots={}, _background_tasks=set(), sessions=SessionManager(KiroCrewConfig())
    )
    app.cleanup_ctx.append(server._organization_lifecycle)
    runner = web.AppRunner(app)
    await runner.setup()
    server._kick_organization(app)
    cleanup = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        cleanup = asyncio.create_task(runner.cleanup())
        await asyncio.sleep(0)  # let cleanup deliver cancellation to initialization
        assert not cleanup.done()
        assert not finished.is_set()
    finally:
        release.set()
        if cleanup is not None:
            await asyncio.wait_for(cleanup, timeout=5)
        else:
            await runner.cleanup()
    assert finished.is_set()
    assert app["organization_startup_task"].cancelled()
    assert "organization_runner" not in app
