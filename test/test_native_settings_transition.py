"""Native settings consent/retirement races, using synthetic homes only."""

from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.dashboard import native_settings as transition


@pytest.fixture
def setup_transition(monkeypatch):
    monkeypatch.setattr(transition, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(transition, "_needed", lambda: True)
    monkeypatch.setattr(transition.windows_settings, "permitted", lambda: False)
    monkeypatch.setattr(
        transition.windows_settings, "snapshot", lambda: {"config.json": "generation-a"}
    )
    monkeypatch.setattr(transition.windows_settings, "claim_transition", lambda row: dict(row))
    state = SimpleNamespace(
        _background_tasks=set(),
        _gateway_restart_in_progress=False,
        _gateway_restart_task=None,
        _native_settings_work_counts=lambda: (1, 2),
    )
    request = SimpleNamespace(app={"state": state}, path="/api/mcp-gateway/enable")
    return request, state


def _body(response):
    return json.loads(response.body)


@pytest.mark.asyncio
async def test_consent_is_exact_and_endpoint_bound(setup_transition, monkeypatch):
    request, state = setup_transition
    operation = AsyncMock()
    initial = await transition.maybe_restart(request, {"enabled": True}, operation)
    assert initial.status == 409
    assert _body(initial)["work"] == {"turns": 1, "background": 2}
    token = _body(initial)["confirmation_token"]
    request.path = "/api/mcp-gateway/servers/stub"
    stale = await transition.maybe_restart(
        request,
        {
            "enabled": True,
            "restart_native_sessions": True,
            "native_settings_confirmation_token": token,
        },
        operation,
    )
    assert _body(stale)["code"] == "native_settings_confirmation_stale"
    operation.assert_not_called()
    assert not state._background_tasks


@pytest.mark.asyncio
async def test_two_confirmed_requests_cannot_claim_one_restart(setup_transition, monkeypatch):
    request, state = setup_transition
    pending = asyncio.Event()
    started = asyncio.Event()
    scheduled = []

    async def apply(*args):
        scheduled.append(args)
        started.set()
        await pending.wait()

    monkeypatch.setattr(transition, "_apply", apply)
    monkeypatch.setattr(transition, "block_windows_tree_starts", lambda owner: True)
    bodies = []
    for enabled in (True, False):
        response = await transition.maybe_restart(request, {"enabled": enabled}, AsyncMock())
        bodies.append(
            {
                "enabled": enabled,
                "restart_native_sessions": True,
                "native_settings_confirmation_token": _body(response)["confirmation_token"],
            }
        )
    # Both requests cross the first task check before either snapshot returns.
    original_thread = asyncio.to_thread
    entered = 0
    both = asyncio.Event()

    async def controlled_thread(fn, *args, **kwargs):
        nonlocal entered
        if fn is transition.windows_settings.snapshot:
            entered += 1
            if entered == 2:
                both.set()
            await both.wait()
        return await original_thread(fn, *args, **kwargs)

    monkeypatch.setattr(transition.asyncio, "to_thread", controlled_thread)
    try:
        results = await asyncio.gather(
            *(transition.maybe_restart(request, body, AsyncMock()) for body in bodies)
        )
        assert sorted(result.status for result in results) == [200, 409]
        await started.wait()
        assert len(scheduled) == 1
    finally:
        pending.set()
        await asyncio.gather(*state._background_tasks)


@pytest.mark.asyncio
async def test_retirement_advances_existing_pending_tree_after_fleet_stopped(monkeypatch):
    pending = [True, True, False]
    passes = []
    monkeypatch.setattr(transition, "windows_tree_admissions_pending", lambda: pending.pop(0))
    monkeypatch.setattr(
        transition, "retry_pending_windows_process_trees", lambda limit: passes.append(limit)
    )
    await transition.wait_retirement(timeout=2)
    assert passes == [1, 1]


@pytest.mark.asyncio
async def test_unconfirmed_retirement_never_runs_commit(monkeypatch):
    monkeypatch.setattr(transition, "windows_tree_admissions_pending", lambda: 1)
    monkeypatch.setattr(transition, "retry_pending_windows_process_trees", lambda limit: ())
    with pytest.raises(OSError, match="unconfirmed"):
        await transition.wait_retirement(timeout=0)


@pytest.mark.asyncio
async def test_stop_failure_keeps_documents_unsaved_and_admission_blocked(
    setup_transition, monkeypatch
):
    request, state = setup_transition
    from kiro_crew.dashboard.handlers import updates

    records = []
    monkeypatch.setattr(
        transition.windows_settings,
        "record_status",
        lambda row, **kwargs: records.append(dict(row)),
    )

    async def failed_stop(state, **kwargs):
        state._native_settings_stop_started = True
        raise OSError("synthetic stop failure")

    monkeypatch.setattr(updates, "_restart_gateway", failed_stop)
    operation = AsyncMock()
    await transition._apply(
        state, operation, {"config.json": "generation-a"}, "digest", object(), "id"
    )
    operation.assert_not_called()
    assert records[-1]["status"] == "activation_failed"
    assert records[-1]["saved"] is False
    assert records[-1]["starts_blocked"] is True
    assert records[-1]["failed_step"] == "stop"


@pytest.mark.asyncio
async def test_status_publication_failure_has_terminal_api_state(setup_transition, monkeypatch):
    request, state = setup_transition
    released = []
    monkeypatch.setattr(
        transition, "unblock_windows_tree_starts", lambda owner: released.append(owner)
    )

    def failed_write(row, **kwargs):
        raise PermissionError("synthetic status-write refusal")

    monkeypatch.setattr(transition.windows_settings, "record_status", failed_write)
    monkeypatch.setattr(transition.windows_settings, "claim_transition", failed_write)
    owner = object()
    operation = AsyncMock()
    await transition._apply(
        state, operation, {"config.json": "generation-a"}, "digest", owner, "id"
    )
    operation.assert_not_called()
    assert state._native_settings_failure["status"] == "activation_failed"
    assert state._native_settings_failure["transition_id"] == "id"
    assert released == [owner]


@pytest.mark.asyncio
async def test_resolver_refusal_preserves_durable_block_without_local_fence(
    setup_transition, monkeypatch
):
    from kiro_crew.dashboard.handlers import updates

    _, state = setup_transition
    records, released = [], []
    monkeypatch.setattr(
        transition.windows_settings,
        "claim_transition",
        lambda row: {**row, "prior_admission_blocked": True},
    )
    monkeypatch.setattr(
        transition.windows_settings,
        "record_status",
        lambda row, **kwargs: records.append(dict(row)),
    )
    monkeypatch.setattr(
        transition, "unblock_windows_tree_starts", lambda owner: released.append(owner)
    )
    monkeypatch.setattr(updates, "_restart_gateway", AsyncMock(return_value=False))
    operation = AsyncMock()
    await transition._apply(
        state, operation, {"config.json": "generation-a"}, "digest", object(), "id"
    )
    operation.assert_not_called()
    assert records[-1]["starts_blocked"] is True
    assert records[-1]["saved"] is False
    assert not released


@pytest.mark.asyncio
async def test_native_manager_retains_original_receipt_when_tree_drain_is_pending(
    monkeypatch, tmp_path
):
    from kiro_crew.mcp_gateway import manager

    proc = SimpleNamespace(returncode=1)
    mgr = manager.GatewayManager(manager.GatewaySpec(socket_path=str(tmp_path / "broker")))
    mgr._process = proc
    monkeypatch.setattr(manager.platform_compat, "IS_WINDOWS", True)
    drain = AsyncMock(side_effect=OSError("synthetic pending tree"))
    monkeypatch.setattr(manager.platform_compat, "terminate_windows_asyncio_tree", drain)
    with pytest.raises(OSError, match="pending"):
        await mgr._terminate_process(grace_secs=0)
    assert mgr._process is proc
    drain.side_effect = None
    drain.return_value = True
    await mgr._terminate_process(grace_secs=0)
    assert mgr._process is None
    assert drain.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refused,expected",
    [(frozenset(), "active"), (frozenset({"KIROCREW_CORE"}), "activation_failed")],
)
async def test_activation_ignores_ungranted_followers_but_refuses_declared_unapproved_launches(
    monkeypatch, refused, expected
):
    from kiro_crew import agent

    record = {"status": "saved", "starts_blocked": True}
    records = []
    monkeypatch.setattr(transition, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(transition.windows_settings, "status", lambda: dict(record))
    monkeypatch.setattr(transition.windows_settings, "permitted", lambda: True)
    monkeypatch.setattr(transition.windows_settings, "snapshot", lambda: {})
    monkeypatch.setattr(
        transition.windows_settings,
        "record_status",
        lambda row, **kwargs: records.append(dict(row)),
    )

    @contextlib.asynccontextmanager
    async def transaction():
        yield

    monkeypatch.setattr(transition.windows_settings, "async_write_transaction", transaction)
    rebuilds = []
    monkeypatch.setattr(agent, "rebuild_agent_config", lambda **kwargs: rebuilds.append(kwargs))
    cfg = SimpleNamespace(mcp_gateway=SimpleNamespace(enabled=False))
    manager = SimpleNamespace(is_running=True, ping=AsyncMock(return_value=True))
    await transition.finish_activation(
        cfg,
        manager,
        frozenset({"kirocrew-core", "kirocrew-cron", "kirocrew-debug"}),
        refused=refused,
    )
    assert records[-1]["status"] == expected
    assert records[-1]["starts_blocked"] is (expected != "active")
    if expected == "active":
        assert rebuilds == [{"refresh_forks": True}]
    else:
        assert not rebuilds


@pytest.mark.asyncio
async def test_failed_transition_is_not_hidden_by_healthy_old_routing(monkeypatch):
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.handlers import mcp

    cfg = SimpleNamespace(enabled=False, stub_servers=["kirocrew-core"])
    manager = SimpleNamespace(is_running=True, ping=AsyncMock(return_value=True))
    state = SimpleNamespace(
        _mcp_gateway_manager=manager,
        _mcp_stub_servers_started=["kirocrew-core"],
        _mcp_sharing_started=False,
    )
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: SimpleNamespace(mcp_gateway=cfg))
    monkeypatch.setattr(mcp, "refused_servers", lambda: {})
    monkeypatch.setattr(
        transition.windows_settings,
        "status",
        lambda: {
            "status": "activation_failed",
            "saved": False,
            "starts_blocked": False,
            "transition_id": "failed-before-stop",
        },
    )
    response = await mcp.api_mcp_gateway_status(SimpleNamespace(app={"state": state}))
    body = _body(response)
    assert body["status"] == "activation_failed"
    assert body["saved"] is False
    assert body["active"] is False
    assert body["live_routing_active"] is True
    assert body["transition_id"] == "failed-before-stop"
