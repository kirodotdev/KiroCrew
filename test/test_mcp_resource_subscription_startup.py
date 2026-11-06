"""Resource subscription routes while the service is still loading its store.

Until ``ResourceSubscriptionService.start`` publishes its startup snapshot, the
service holds an empty record set. Pinned here: the operator route and the two
session routes that read records (list and remove) answer a retryable refusal in
that window instead of an answer built from the empty set, and change nothing.
A retirement that runs with no service and races a start leaves that start
holding nothing of the retired session.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from test_mcp_resource_subscription_tools import (  # noqa: F401 -- fixtures ride along
    OTHER,
    SESSION,
    URI,
    _call,
    _clean_service,
    _record,
    recognized,
    switch,
)

from kiro_crew import cli_resource_subs
from kiro_crew.config.integration_sections import McpResourceSubscriptionsConfig
from kiro_crew.dashboard.handlers import resource_subscriptions as routes
from kiro_crew.mcp_gateway import resource_subscriptions as rs
from kiro_crew.mcp_gateway.resource_subscription_store import (
    HolderRecord,
    ResourceSubscriptionStore,
)
from kiro_crew.mcp_gateway.resource_subscriptions import ResourceSubscriptionService
from kiro_crew.testing.wait import async_wait_until, default_timeout


@pytest.mark.asyncio
async def test_operator_remove_while_service_is_starting_is_retryable_and_never_edits_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    record = _record(OTHER, (URI,), ("rsub_starting00000",))
    store = ResourceSubscriptionStore(tmp_path / "subs.json")
    store.put(record)
    original_load = store.load
    load_started = threading.Event()
    load_can_finish = threading.Event()

    def held_load() -> list[HolderRecord]:
        snapshot = original_load()
        load_started.set()
        if not load_can_finish.wait(default_timeout()):
            raise AssertionError("service start was never released")
        return snapshot

    monkeypatch.setattr(store, "load", held_load)
    settings = McpResourceSubscriptionsConfig(enabled=False)
    service = ResourceSubscriptionService(
        store=store,
        socket_path=lambda: None,
        sink=rs.RecordingSink(),
        settings=lambda: settings,
    )
    rs.set_active_service(service)
    start = asyncio.create_task(service.start())
    try:
        await async_wait_until(load_started.is_set)
        starting = await _call(
            routes.api_resource_subscriptions_operator,
            "POST",
            "/api/resource-subscriptions/operator",
            sk="",
            body={"subscription_id": "rsub_starting00000"},
        )
        assert starting == {
            "ok": False,
            "refused": "starting",
            "reason": "the gateway subscription service is starting; try again",
            "running": True,
            "_status": 200,
        }
        monkeypatch.setattr(cli_resource_subs, "_store", lambda: store)
        monkeypatch.setattr(cli_resource_subs, "_ask_gateway", lambda _body: starting)
        assert cli_resource_subs._remove("rsub_starting00000") == 1
        assert original_load()[0].subscriptions[URI].id == "rsub_starting00000"
        assert "starting; try again" in capsys.readouterr().err
    finally:
        load_can_finish.set()
        await start

    removed = await _call(
        routes.api_resource_subscriptions_operator,
        "POST",
        "/api/resource-subscriptions/operator",
        sk="",
        body={"subscription_id": "rsub_starting00000"},
    )
    assert removed["ok"] is True and removed["running"] is True
    assert original_load() == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch", "recognized")
async def test_session_list_and_remove_while_service_is_starting_are_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ResourceSubscriptionStore(tmp_path / "subs.json")
    store.put(_record(SESSION, (URI,), ("rsub_starting00000",)))
    original_load = store.load
    load_started = threading.Event()
    load_can_finish = threading.Event()

    def held_load() -> list[HolderRecord]:
        snapshot = original_load()
        load_started.set()
        if not load_can_finish.wait(default_timeout()):
            raise AssertionError("service start was never released")
        return snapshot

    monkeypatch.setattr(store, "load", held_load)
    settings = McpResourceSubscriptionsConfig(enabled=False)
    service = ResourceSubscriptionService(
        store=store,
        socket_path=lambda: None,
        sink=rs.RecordingSink(),
        settings=lambda: settings,
    )
    rs.set_active_service(service)
    start = asyncio.create_task(service.start())
    retry_later = {
        "ok": False,
        "refused": "gateway_unavailable",
        "reason": "the gateway subscription service is starting; try again",
        "_status": 200,
    }
    try:
        await async_wait_until(load_started.is_set)
        listed = await _call(
            routes.api_resource_subscriptions_list, "GET", "/api/resource-subscriptions"
        )
        removing = await _call(
            routes.api_resource_unsubscribe,
            "POST",
            "/api/resource-subscriptions/remove",
            body={"subscription_id": "rsub_starting00000"},
        )
        assert listed == retry_later
        assert removing == retry_later
    finally:
        load_can_finish.set()
        await start

    listed = await _call(
        routes.api_resource_subscriptions_list, "GET", "/api/resource-subscriptions"
    )
    assert [row["id"] for row in listed["subscriptions"]] == ["rsub_starting00000"]
    removed = await _call(
        routes.api_resource_unsubscribe,
        "POST",
        "/api/resource-subscriptions/remove",
        body={"subscription_id": "rsub_starting00000"},
    )
    assert removed == {"ok": True, "removed": "rsub_starting00000", "_status": 200}
    assert original_load() == []


@pytest.mark.asyncio
async def test_an_offline_retire_that_races_a_start_leaves_the_started_service_without_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ResourceSubscriptionStore(tmp_path / "subs.json")
    store.put(_record(SESSION, (URI,), ("rsub_retiring00000",)))
    original_remove = store.remove
    removal_started = threading.Event()
    service_has_loaded = threading.Event()

    def remove_after_the_start_loaded(session_key: str) -> int:
        removal_started.set()
        if not service_has_loaded.wait(default_timeout()):
            raise AssertionError("the racing service never finished loading")
        return original_remove(session_key)

    monkeypatch.setattr(store, "remove", remove_after_the_start_loaded)
    monkeypatch.setattr(rs, "default_store", lambda: store)
    settings = McpResourceSubscriptionsConfig(enabled=False)
    retiring = asyncio.create_task(rs.retire_session(SESSION))
    await async_wait_until(removal_started.is_set)

    service = ResourceSubscriptionService(
        store=store,
        socket_path=lambda: None,
        sink=rs.RecordingSink(),
        settings=lambda: settings,
    )
    rs.set_active_service(service)
    await service.start()
    assert [r.session_key for r in service.records(SESSION)] == [SESSION]
    service_has_loaded.set()

    assert await retiring == 1
    assert service.records(SESSION) == []
    assert store.load() == []
    await service.close()


def _pooled_record(session_key: str) -> HolderRecord:
    return HolderRecord(
        session_key=session_key,
        channel_id="",
        server="fake",
        register={
            "server_name": "fake",
            "poolable": True,
            "command_args_hash": "args",
            "effective_env_hash": "env",
            "work_dir": "/work/shared",
            "binary_version": "v1",
            "os_uid": 1000,
        },
        initialize={"method": "initialize", "params": {}},
        uris=[URI],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [True, False], ids=["sharing_on", "sharing_off"])
async def test_restored_holders_register_by_the_current_sharing_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shared: bool
) -> None:
    store = ResourceSubscriptionStore(tmp_path / "subs.json")
    for session_key in ("dashboard:pooled-a", "dashboard:pooled-b"):
        store.put(_pooled_record(session_key))
    settings = McpResourceSubscriptionsConfig(enabled=True, max_pinned_backends=1)
    service = ResourceSubscriptionService(
        store=store,
        socket_path=lambda: None,
        sink=rs.RecordingSink(),
        settings=lambda: settings,
        backends_shared=lambda: shared,
    )
    started: list[HolderRecord] = []

    def record_start(record: HolderRecord, **_kwargs: object) -> None:
        started.append(record)

    monkeypatch.setattr(service, "_start_holder", record_start)

    await service.start()

    registered_poolable = [record.register.get("poolable") is True for record in started]
    paused = [r.session_key for r in service.records() if r.paused_reason]
    if shared:
        # Both share one pooled backend, so one pin admits both.
        assert registered_poolable == [True, True]
        assert paused == []
    else:
        # Each holder is its own private backend, so the cap of one admits one.
        assert registered_poolable == [False]
        assert paused == ["dashboard:pooled-b"]


class _AcceptingHolder:
    def __init__(self) -> None:
        self.uris: list[str] = []
        self.paused_reason = ""
        self.paused_code = ""

    async def wait_ready(self, _timeout: float) -> None:
        return None

    async def subscribe(self, uri: str) -> dict:
        self.uris.append(uri)
        return {"result": {}}

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [True, False], ids=["sharing_on", "sharing_off"])
async def test_a_new_holder_registers_by_the_current_sharing_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shared: bool
) -> None:
    store = ResourceSubscriptionStore(tmp_path / "subs.json")
    settings = McpResourceSubscriptionsConfig(enabled=True)
    service = ResourceSubscriptionService(
        store=store,
        socket_path=lambda: None,
        sink=rs.RecordingSink(),
        settings=lambda: settings,
        backends_shared=lambda: shared,
    )
    await service.start()
    started: list[HolderRecord] = []

    async def describe(session_key: str, _server: str) -> HolderRecord:
        record = _pooled_record(session_key)
        record.uris = []
        return record

    def start(record: HolderRecord, **_kwargs: object) -> _AcceptingHolder:
        started.append(record)
        holder = _AcceptingHolder()
        service._holders[record.key] = holder  # type: ignore[assignment]
        return holder

    monkeypatch.setattr(service, "_describe", describe)
    monkeypatch.setattr(service, "_start_holder", start)

    await service.subscribe("dashboard:pooled-new", "fake", URI)

    assert [record.register.get("poolable") is True for record in started] == [shared]
    (stored,) = store.load()
    assert (stored.register.get("poolable") is True) is shared


@pytest.mark.asyncio
async def test_an_unwritable_pause_at_start_keeps_the_service_and_the_other_holders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ResourceSubscriptionStore(tmp_path / "subs.json")
    for session_key in ("dashboard:pooled-a", "dashboard:pooled-b"):
        store.put(_pooled_record(session_key))

    def refuse_put(_record: HolderRecord, *, deadline: float | None = None) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(store, "put", refuse_put)
    settings = McpResourceSubscriptionsConfig(enabled=True, max_pinned_backends=1)
    service = ResourceSubscriptionService(
        store=store,
        socket_path=lambda: None,
        sink=rs.RecordingSink(),
        settings=lambda: settings,
        backends_shared=lambda: False,
    )
    started: list[str] = []

    def record_start(record: HolderRecord, **_kwargs: object) -> None:
        started.append(record.session_key)

    monkeypatch.setattr(service, "_start_holder", record_start)

    await service.start()

    assert service.started
    assert started == ["dashboard:pooled-a"]
    # Only a stored pause is published: memory still matches the store.
    assert [r.session_key for r in service.records() if r.paused_reason] == []
    assert [r.paused_reason for r in store.load()] == ["", ""]
