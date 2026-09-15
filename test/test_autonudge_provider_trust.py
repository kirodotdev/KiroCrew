from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import autonudge_authz
from kiro_crew import autonudge_provider_trust as trust
from kiro_crew.autonudge import AutoNudgeService
from kiro_crew.monitoring.models import (
    MonitorBudgets,
    MonitorCreationSurface,
    MonitorOutcome,
    MonitorState,
)


@pytest.fixture(autouse=True)
def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(trust, "data_home", lambda: tmp_path)


def test_pending_grant_cannot_authorize_until_activated(tmp_path: Path) -> None:
    trust.prepare_monitor_owner_credentials(
        "monitor1",
        "chat-1",
        "bitbucket_pull_request",
        "bitbucket.org/acme/widgets#10",
    )

    assert trust.monitor_owner_credentials_path().parent == tmp_path / ".vault"
    assert not trust.is_monitor_owner_credentials_recorded(
        "monitor1",
        "chat-1",
        "bitbucket_pull_request",
        "bitbucket.org/acme/widgets#10",
    )

    trust.activate_monitor_owner_credentials("monitor1")

    assert trust.is_monitor_owner_credentials_recorded(
        "monitor1",
        "chat-1",
        "bitbucket_pull_request",
        "bitbucket.org/acme/widgets#10",
    )


def test_grant_binds_every_probe_identity_field_and_is_revocable() -> None:
    trust.record_monitor_owner_credentials(
        "monitor1",
        "chat-1",
        "azure_devops_pull_request",
        "dev.azure.com/acme/widgets/_git/service#12",
    )

    for candidate in (
        (
            "monitor2",
            "chat-1",
            "azure_devops_pull_request",
            "dev.azure.com/acme/widgets/_git/service#12",
        ),
        (
            "monitor1",
            "chat-2",
            "azure_devops_pull_request",
            "dev.azure.com/acme/widgets/_git/service#12",
        ),
        (
            "monitor1",
            "chat-1",
            "bitbucket_pull_request",
            "dev.azure.com/acme/widgets/_git/service#12",
        ),
        (
            "monitor1",
            "chat-1",
            "azure_devops_pull_request",
            "dev.azure.com/acme/widgets/_git/other#12",
        ),
    ):
        assert not trust.is_monitor_owner_credentials_recorded(*candidate)

    trust.forget_monitor_owner_credentials("monitor1")

    assert not trust.is_monitor_owner_credentials_recorded(
        "monitor1",
        "chat-1",
        "azure_devops_pull_request",
        "dev.azure.com/acme/widgets/_git/service#12",
    )


def test_failed_revocation_denies_immediately_and_retries_on_the_next_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor_id = "monitor-with-transient-revoke-failure"
    identity = (
        monitor_id,
        "chat-1",
        "azure_devops_pull_request",
        "dev.azure.com/acme/widgets/_git/service#12",
    )
    trust.record_monitor_owner_credentials(*identity)
    revocations_path = trust.monitor_owner_credentials_revocations_path()
    real_atomic_write = trust.atomic_write
    attempts = 0

    def fail_twice(path: Path, contents: str, *args: Any, **kwargs: Any) -> None:
        nonlocal attempts
        if path == revocations_path and monitor_id in json.loads(contents)["monitor_ids"]:
            attempts += 1
            if attempts <= 2:
                raise OSError("transient write failure")
        real_atomic_write(path, contents, *args, **kwargs)

    monkeypatch.setattr(trust, "atomic_write", fail_twice)

    with pytest.raises(OSError, match="transient write failure"):
        trust.forget_monitor_owner_credentials(monitor_id)

    assert not trust.is_monitor_owner_credentials_recorded(*identity)
    assert attempts == 2
    assert not trust.is_monitor_owner_credentials_recorded(*identity)
    assert attempts == 3
    assert (
        monitor_id
        not in json.loads(trust.monitor_owner_credentials_path().read_text(encoding="utf-8"))[
            "monitors"
        ]
    )


def test_failed_grant_cleanup_stays_revoked_after_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor_id = "monitor-with-failed-grant-cleanup"
    identity = (
        monitor_id,
        "chat-1",
        "bitbucket_pull_request",
        "bitbucket.org/acme/widgets#10",
    )
    trust.record_monitor_owner_credentials(*identity)
    grant_path = trust.monitor_owner_credentials_path()
    real_atomic_write = trust.atomic_write

    def fail_grant_cleanup(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == grant_path:
            raise OSError("grant cleanup unavailable")
        real_atomic_write(path, *args, **kwargs)

    monkeypatch.setattr(trust, "atomic_write", fail_grant_cleanup)

    trust.forget_monitor_owner_credentials(monitor_id)

    with trust._PENDING_REVOCATIONS_LOCK:
        trust._PENDING_REVOCATIONS.clear()

    assert not trust.is_monitor_owner_credentials_recorded(*identity)


def test_failed_tombstone_write_refuses_to_finalize_revocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor_id = "monitor-with-failed-tombstone-write"
    identity = (
        monitor_id,
        "chat-1",
        "bitbucket_pull_request",
        "bitbucket.org/acme/widgets#10",
    )
    trust.record_monitor_owner_credentials(*identity)
    revocations_path = trust.monitor_owner_credentials_revocations_path()
    real_atomic_write = trust.atomic_write

    def fail_tombstone(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == revocations_path:
            raise OSError("tombstone unavailable")
        real_atomic_write(path, *args, **kwargs)

    monkeypatch.setattr(trust, "atomic_write", fail_tombstone)

    with pytest.raises(OSError, match="tombstone unavailable"):
        trust.forget_monitor_owner_credentials(monitor_id)


@pytest.mark.asyncio
async def test_remove_keeps_monitor_when_durable_provider_revocation_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    svc = AutoNudgeService(tmp_path / "store")
    loop = await svc.add_monitor(
        slot_key="chat-1",
        kind="bitbucket_pull_request",
        target="bitbucket.org/acme/widgets#10",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(max_runtime_secs=600),
        creation_surface=MonitorCreationSurface.DASHBOARD,
    )
    assert loop.monitor is not None
    trust.record_monitor_owner_credentials(
        loop.id,
        loop.slot_key,
        loop.monitor.kind,
        loop.monitor.target,
    )

    def fail_revocation(_monitor_id: str) -> None:
        raise OSError("durable revocation unavailable")

    monkeypatch.setattr(trust, "forget_monitor_owner_credentials", fail_revocation)

    with pytest.raises(OSError, match="durable revocation unavailable"):
        await svc.remove(loop.id)

    assert svc.get_by_id(loop.id) is loop
    timer = svc._timers.get(loop.id)
    assert loop.active is True and timer is not None and not timer.done()
    reloaded = AutoNudgeService(tmp_path / "store")
    reloaded._load()
    assert reloaded.get_by_id(loop.id) is not None
    svc.stop()


@pytest.mark.asyncio
async def test_failed_remove_persistence_restores_provider_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    svc = AutoNudgeService(tmp_path / "store")
    loop = await svc.add_monitor(
        slot_key="chat-1",
        kind="azure_devops_pull_request",
        target="dev.azure.com/acme/widgets/_git/service#12",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(max_runtime_secs=600),
        creation_surface=MonitorCreationSurface.DASHBOARD,
    )
    assert loop.monitor is not None
    trust.record_monitor_owner_credentials(
        loop.id,
        loop.slot_key,
        loop.monitor.kind,
        loop.monitor.target,
    )

    def fail_snapshot(_payload: dict[str, Any]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(svc, "_write_state", fail_snapshot)

    with pytest.raises(OSError, match="disk full"):
        await svc.remove(loop.id)

    assert svc.get_by_id(loop.id) is loop
    assert trust.is_monitor_owner_credentials_recorded(
        loop.id,
        loop.slot_key,
        loop.monitor.kind,
        loop.monitor.target,
    )
    svc.stop()


@pytest.mark.asyncio
async def test_failed_legacy_replacement_restores_prior_monitor_grant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    svc = AutoNudgeService(tmp_path / "store")
    prior = await svc.add_monitor(
        slot_key="chat-1",
        kind="bitbucket_pull_request",
        target="bitbucket.org/acme/widgets#10",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(max_runtime_secs=600),
        creation_surface=MonitorCreationSurface.DASHBOARD,
    )
    assert prior.monitor is not None
    trust.record_monitor_owner_credentials(
        prior.id,
        prior.slot_key,
        prior.monitor.kind,
        prior.monitor.target,
    )

    def fail_snapshot(_payload: dict[str, Any]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(svc, "_write_state", fail_snapshot)

    with pytest.raises(OSError, match="disk full"):
        await svc.add(
            slot_key=prior.slot_key,
            message="legacy replacement",
            idle_secs=60,
        )

    assert svc.get_by_slot(prior.slot_key) is prior
    assert trust.is_monitor_owner_credentials_recorded(
        prior.id,
        prior.slot_key,
        prior.monitor.kind,
        prior.monitor.target,
    )
    svc.stop()


def test_failed_revocation_read_cannot_clear_the_pending_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor_id = "monitor-with-transient-read-failure"
    identity = (
        monitor_id,
        "chat-1",
        "bitbucket_pull_request",
        "bitbucket.org/acme/widgets#10",
    )
    trust.record_monitor_owner_credentials(*identity)
    record_path = trust.monitor_owner_credentials_path()
    real_read_text = Path.read_text
    attempts = 0

    def fail_twice(path: Path, *args: Any, **kwargs: Any) -> str:
        nonlocal attempts
        if path == record_path:
            attempts += 1
            if attempts <= 2:
                raise OSError("transient read failure")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_twice)

    with pytest.raises(OSError, match="transient read failure"):
        trust.forget_monitor_owner_credentials(monitor_id)

    assert not trust.is_monitor_owner_credentials_recorded(*identity)
    assert attempts == 2
    assert not trust.is_monitor_owner_credentials_recorded(*identity)
    assert attempts == 3
    assert monitor_id not in json.loads(real_read_text(record_path, encoding="utf-8"))["monitors"]


def test_active_grant_can_move_only_through_gateway_record_write() -> None:
    trust.record_monitor_owner_credentials(
        "monitor1", "chat-1", "bitbucket_pull_request", "bitbucket.org/acme/old#1"
    )
    trust.record_monitor_owner_credentials(
        "monitor1", "chat-1", "bitbucket_pull_request", "bitbucket.org/acme/new#2"
    )

    assert not trust.is_monitor_owner_credentials_recorded(
        "monitor1", "chat-1", "bitbucket_pull_request", "bitbucket.org/acme/old#1"
    )
    assert trust.is_monitor_owner_credentials_recorded(
        "monitor1", "chat-1", "bitbucket_pull_request", "bitbucket.org/acme/new#2"
    )


def test_reader_fails_closed_for_malformed_record() -> None:
    path = trust.monitor_owner_credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"monitors": {"monitor1": "invalid"}}), encoding="utf-8")

    assert not trust.is_monitor_owner_credentials_recorded(
        "monitor1", "chat-1", "bitbucket_pull_request", "bitbucket.org/acme/widgets#10"
    )


@pytest.mark.parametrize(
    "contents",
    [
        "{not-json",
        json.dumps({"version": 1, "monitors": {"existing": "invalid"}}),
    ],
)
@pytest.mark.parametrize(
    "mutation",
    [
        trust.prepare_monitor_owner_credentials,
        trust.record_monitor_owner_credentials,
    ],
)
def test_grant_mutations_refuse_to_replace_an_unreadable_record(
    contents: str,
    mutation: Any,
) -> None:
    path = trust.monitor_owner_credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(OSError, match="provenance record is invalid"):
        mutation(
            "monitor2",
            "chat-2",
            "bitbucket_pull_request",
            "bitbucket.org/acme/widgets#11",
        )

    assert path.read_text(encoding="utf-8") == contents


def test_grant_mutation_propagates_transient_read_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = trust.monitor_owner_credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "monitors": {}}), encoding="utf-8")
    real_read_text = Path.read_text

    def fail_record_read(candidate: Path, *args: Any, **kwargs: Any) -> str:
        if candidate == path:
            raise OSError("transient read failure")
        return real_read_text(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_record_read)

    with pytest.raises(OSError, match="transient read failure"):
        trust.record_monitor_owner_credentials(
            "monitor2",
            "chat-2",
            "bitbucket_pull_request",
            "bitbucket.org/acme/widgets#11",
        )


@pytest.mark.asyncio
async def test_dashboard_stamp_alone_cannot_mint_an_owner_credential_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Service:
        def get_by_id(self, _loop_id: str) -> None:
            return None

        def commit_monitor_replacement(self, _loop_id: str) -> None:
            return None

        async def rollback_monitor_replacement(self, _loop_id: str) -> bool:
            return True

        async def add_monitor(self, **kwargs: Any) -> Any:
            state = MonitorState(
                kind=kwargs["kind"],
                target=kwargs["target"],
                objective=kwargs["objective"],
                created_ts=1_000.0,
                budgets=kwargs["budgets"],
                cadence_secs=kwargs["cadence_secs"],
                creation_surface=kwargs["creation_surface"],
            )
            return SimpleNamespace(
                id=kwargs.get("loop_id") or "untrusted1",
                slot_key=kwargs["slot_key"],
                monitor=state,
            )

    monkeypatch.setattr(
        autonudge_authz,
        "sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **_kwargs: None),
    )
    monitor = MonitorState(
        kind="bitbucket_pull_request",
        target="bitbucket.org/acme/widgets#10",
        objective="review_ready",
        created_ts=1_000.0,
        budgets=MonitorBudgets(max_runtime_secs=600),
        cadence_secs=60,
    )
    state = SimpleNamespace(
        _slots={"chat-1": SimpleNamespace(mode="chat", memory_mode="persistent")},
        sessions=None,
        channel_transports={},
    )

    untrusted, error, status = await autonudge_authz.authorize_and_add_nudge(
        svc=Service(),
        state=state,
        slot_key="chat-1",
        message="watch",
        monitor=monitor,
        source="test",
        creation_surface=MonitorCreationSurface.DASHBOARD,
    )

    assert error is None and status == 200 and untrusted is not None
    assert not trust.is_monitor_owner_credentials_recorded(
        untrusted.id, untrusted.slot_key, monitor.kind, monitor.target
    )

    trusted, error, status = await autonudge_authz.authorize_and_add_nudge(
        svc=Service(),
        state=state,
        slot_key="chat-1",
        message="watch",
        monitor=monitor,
        source="dashboard",
        creation_surface=MonitorCreationSurface.DASHBOARD,
        grant_owner_provider_credentials=True,
    )

    assert error is None and status == 200 and trusted is not None
    assert trust.is_monitor_owner_credentials_recorded(
        trusted.id, trusted.slot_key, monitor.kind, monitor.target
    )


@pytest.mark.asyncio
async def test_dashboard_restart_cannot_mint_grant_from_writable_monitor_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    svc = AutoNudgeService(tmp_path / "store")
    prior = await svc.add_monitor(
        slot_key="chat-1",
        kind="bitbucket_pull_request",
        target="bitbucket.org/acme/widgets#10",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(max_runtime_secs=600),
        creation_surface=MonitorCreationSurface.CHANNEL,
    )
    assert prior.monitor is not None
    prior.active = False
    prior.monitor.outcome = MonitorOutcome.USER_STOP
    prior.monitor.creation_surface = MonitorCreationSurface.DASHBOARD
    prior.monitor.target = "bitbucket.org/attacker/widgets#99"
    await svc._write_monitor_snapshot_locked()
    monkeypatch.setattr(
        autonudge_authz,
        "sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **_kwargs: None),
    )
    state = SimpleNamespace(
        _slots={"chat-1": SimpleNamespace(mode="chat", memory_mode="persistent")},
        sessions=None,
        channel_transports={},
    )

    restarted, error, status = await autonudge_authz.authorize_and_add_nudge(
        svc=svc,
        state=state,
        slot_key=prior.slot_key,
        message="watch",
        monitor=prior.monitor,
        source="dashboard",
        expected_existing_monitor_id=prior.id,
        expected_existing_config_generation=prior.monitor.config_generation,
        creation_surface=prior.monitor.creation_surface,
        grant_owner_provider_credentials=True,
    )

    assert error is None and status == 200 and restarted is not None
    assert restarted.monitor is not None
    assert not trust.is_monitor_owner_credentials_recorded(
        restarted.id,
        restarted.slot_key,
        restarted.monitor.kind,
        restarted.monitor.target,
    )
    svc.stop()


@pytest.mark.asyncio
async def test_failed_restart_activation_restores_the_terminal_monitor_and_grant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    svc = AutoNudgeService(tmp_path / "store")
    prior = await svc.add_monitor(
        slot_key="chat-1",
        kind="bitbucket_pull_request",
        target="bitbucket.org/acme/widgets#10",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(max_runtime_secs=600),
    )
    assert prior.monitor is not None
    prior.active = False
    prior.monitor.outcome = MonitorOutcome.USER_STOP
    await svc._write_monitor_snapshot_locked()
    trust.record_monitor_owner_credentials(
        prior.id,
        prior.slot_key,
        prior.monitor.kind,
        prior.monitor.target,
    )

    monkeypatch.setattr(
        autonudge_authz,
        "sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **_kwargs: None),
    )

    def fail_activation(_monitor_id: str) -> None:
        raise OSError("transient vault failure")

    monkeypatch.setattr(trust, "activate_monitor_owner_credentials", fail_activation)
    state = SimpleNamespace(
        _slots={"chat-1": SimpleNamespace(mode="chat", memory_mode="persistent")},
        sessions=None,
        channel_transports={},
    )

    restarted, error, status = await autonudge_authz.authorize_and_add_nudge(
        svc=svc,
        state=state,
        slot_key=prior.slot_key,
        message="watch",
        monitor=prior.monitor,
        source="dashboard",
        expected_existing_monitor_id=prior.id,
        expected_existing_config_generation=prior.monitor.config_generation,
        creation_surface=MonitorCreationSurface.DASHBOARD,
        grant_owner_provider_credentials=True,
    )

    assert restarted is None and status == 503
    assert error == "monitor credential authorization unavailable — prior monitor restored"
    restored = svc.get_by_slot(prior.slot_key)
    assert restored is not None and restored.id == prior.id
    assert restored.active is False
    assert restored.monitor is not None
    assert restored.monitor.outcome is MonitorOutcome.USER_STOP
    assert trust.is_monitor_owner_credentials_recorded(
        prior.id,
        prior.slot_key,
        prior.monitor.kind,
        prior.monitor.target,
    )
    reloaded = AutoNudgeService(tmp_path / "store")
    reloaded._load()
    persisted = reloaded.get_by_slot(prior.slot_key)
    assert persisted is not None and persisted.id == prior.id
    assert persisted.monitor is not None
    assert persisted.monitor.outcome is MonitorOutcome.USER_STOP


@pytest.mark.asyncio
async def test_restart_revokes_the_prior_grant_before_replacement_persistence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    svc = AutoNudgeService(tmp_path / "store")
    prior = await svc.add_monitor(
        slot_key="chat-1",
        kind="bitbucket_pull_request",
        target="bitbucket.org/acme/widgets#10",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(max_runtime_secs=600),
        creation_surface=MonitorCreationSurface.DASHBOARD,
    )
    assert prior.monitor is not None
    prior.active = False
    prior.monitor.outcome = MonitorOutcome.USER_STOP
    await svc._write_monitor_snapshot_locked()
    trust.record_monitor_owner_credentials(
        prior.id,
        prior.slot_key,
        prior.monitor.kind,
        prior.monitor.target,
    )
    write_snapshot = svc._write_monitor_snapshot_locked

    async def require_prior_revocation(payload: dict[str, Any] | None = None) -> None:
        assert not trust.is_monitor_owner_credentials_recorded(
            prior.id,
            prior.slot_key,
            prior.monitor.kind,
            prior.monitor.target,
        )
        await write_snapshot(payload)

    monkeypatch.setattr(svc, "_write_monitor_snapshot_locked", require_prior_revocation)

    replacement = await svc.add_monitor(
        slot_key=prior.slot_key,
        kind=prior.monitor.kind,
        target=prior.monitor.target,
        objective=prior.monitor.objective,
        cadence_secs=prior.monitor.cadence_secs,
        budgets=prior.monitor.budgets,
        expected_existing_monitor_id=prior.id,
        expected_existing_config_generation=prior.monitor.config_generation,
        loop_id="replacement",
        defer_replaced_trust_revocation=True,
        creation_surface=MonitorCreationSurface.DASHBOARD,
    )

    assert replacement.id == "replacement"
    svc.stop()


@pytest.mark.asyncio
async def test_failed_restart_persistence_restores_the_retained_monitor_grant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    svc = AutoNudgeService(tmp_path / "store")
    prior = await svc.add_monitor(
        slot_key="chat-1",
        kind="bitbucket_pull_request",
        target="bitbucket.org/acme/widgets#10",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(max_runtime_secs=600),
        creation_surface=MonitorCreationSurface.DASHBOARD,
    )
    assert prior.monitor is not None
    prior.active = False
    prior.monitor.outcome = MonitorOutcome.USER_STOP
    await svc._write_monitor_snapshot_locked()
    trust.record_monitor_owner_credentials(
        prior.id,
        prior.slot_key,
        prior.monitor.kind,
        prior.monitor.target,
    )

    def fail_snapshot(_payload: dict[str, Any]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(svc, "_write_state", fail_snapshot)

    with pytest.raises(OSError, match="disk full"):
        await svc.add_monitor(
            slot_key=prior.slot_key,
            kind=prior.monitor.kind,
            target=prior.monitor.target,
            objective=prior.monitor.objective,
            cadence_secs=prior.monitor.cadence_secs,
            budgets=prior.monitor.budgets,
            expected_existing_monitor_id=prior.id,
            expected_existing_config_generation=prior.monitor.config_generation,
            loop_id="replacement",
            defer_replaced_trust_revocation=True,
            creation_surface=MonitorCreationSurface.DASHBOARD,
        )

    assert svc.get_by_slot(prior.slot_key) is prior
    assert trust.is_monitor_owner_credentials_recorded(
        prior.id,
        prior.slot_key,
        prior.monitor.kind,
        prior.monitor.target,
    )
    svc.stop()


@pytest.mark.asyncio
async def test_failed_restart_activation_preserves_a_concurrent_committed_patch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    svc = AutoNudgeService(tmp_path / "store")
    prior = await svc.add_monitor(
        slot_key="chat-1",
        kind="bitbucket_pull_request",
        target="bitbucket.org/acme/widgets#10",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(max_runtime_secs=600),
    )
    assert prior.monitor is not None
    prior.active = False
    prior.monitor.outcome = MonitorOutcome.USER_STOP
    await svc._write_monitor_snapshot_locked()
    trust.record_monitor_owner_credentials(
        prior.id,
        prior.slot_key,
        prior.monitor.kind,
        prior.monitor.target,
    )
    monkeypatch.setattr(
        autonudge_authz,
        "sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **_kwargs: None),
    )
    activation_entered = threading.Event()
    continue_activation = threading.Event()

    def fail_activation(_monitor_id: str) -> None:
        activation_entered.set()
        assert continue_activation.wait(timeout=1)
        raise OSError("transient vault failure")

    monkeypatch.setattr(trust, "activate_monitor_owner_credentials", fail_activation)
    state = SimpleNamespace(
        _slots={"chat-1": SimpleNamespace(mode="chat", memory_mode="persistent")},
        sessions=None,
        channel_transports={},
    )
    restart = asyncio.create_task(
        autonudge_authz.authorize_and_add_nudge(
            svc=svc,
            state=state,
            slot_key=prior.slot_key,
            message="watch",
            monitor=prior.monitor,
            source="dashboard",
            expected_existing_monitor_id=prior.id,
            expected_existing_config_generation=prior.monitor.config_generation,
            creation_surface=MonitorCreationSurface.DASHBOARD,
            grant_owner_provider_credentials=True,
        )
    )
    assert await asyncio.to_thread(activation_entered.wait, 1)
    replacement = svc.get_by_slot(prior.slot_key)
    assert replacement is not None and replacement.id != prior.id
    concurrent = await svc.update_monitor(
        replacement.id,
        wake_instructions="Keep the committed edit.",
    )
    assert concurrent is replacement
    continue_activation.set()

    restarted, error, status = await asyncio.wait_for(restart, timeout=1)

    assert restarted is None and status == 409
    assert error == "monitor changed while credential authorization failed"
    current = svc.get_by_slot(prior.slot_key)
    assert current is replacement and current.monitor is not None
    assert current.monitor.wake_instructions == "Keep the committed edit."
    assert not trust.is_monitor_owner_credentials_recorded(
        prior.id,
        prior.slot_key,
        prior.monitor.kind,
        prior.monitor.target,
    )
    reloaded = AutoNudgeService(tmp_path / "store")
    reloaded._load()
    persisted = reloaded.get_by_slot(prior.slot_key)
    assert persisted is not None and persisted.monitor is not None
    assert persisted.id == replacement.id
    assert persisted.monitor.wake_instructions == "Keep the committed edit."
    svc.stop()


@pytest.mark.asyncio
async def test_dashboard_update_cannot_mint_grant_for_channel_monitor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    svc = AutoNudgeService(tmp_path / "store")
    loop = await svc.add_monitor(
        slot_key="chat-1",
        kind="bitbucket_pull_request",
        target="bitbucket.org/acme/widgets#10",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(max_runtime_secs=600),
        creation_surface=MonitorCreationSurface.CHANNEL,
    )
    assert loop.monitor is not None
    monkeypatch.setattr(
        autonudge_authz,
        "sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **_kwargs: None),
    )
    state = SimpleNamespace(
        _slots={"chat-1": SimpleNamespace(mode="chat", memory_mode="persistent")},
        sessions=None,
        channel_transports={},
    )

    updated, error, status = await autonudge_authz.authorize_and_update_monitor(
        svc=svc,
        state=state,
        loop_id=loop.id,
        session_key=loop.slot_key,
        patch={"target": "bitbucket.org/acme/widgets#11"},
        source="dashboard",
        grant_owner_provider_credentials=True,
    )

    assert updated is loop and error is None and status == 200
    assert updated.monitor is not None
    assert not trust.is_monitor_owner_credentials_recorded(
        loop.id,
        loop.slot_key,
        updated.monitor.kind,
        updated.monitor.target,
    )
    svc.stop()


@pytest.mark.asyncio
async def test_failed_update_grant_restores_the_prior_monitor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    svc = AutoNudgeService(tmp_path / "store")
    loop = await svc.add_monitor(
        slot_key="chat-1",
        kind="bitbucket_pull_request",
        target="bitbucket.org/acme/widgets#10",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(max_runtime_secs=600),
        creation_surface=MonitorCreationSurface.DASHBOARD,
    )
    assert loop.monitor is not None
    trust.record_monitor_owner_credentials(
        loop.id,
        loop.slot_key,
        loop.monitor.kind,
        loop.monitor.target,
    )
    monkeypatch.setattr(
        autonudge_authz,
        "sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **_kwargs: None),
    )

    def fail_record(*_args: Any) -> None:
        raise OSError("transient vault failure")

    monkeypatch.setattr(trust, "record_monitor_owner_credentials", fail_record)
    state = SimpleNamespace(
        _slots={"chat-1": SimpleNamespace(mode="chat", memory_mode="persistent")},
        sessions=None,
        channel_transports={},
    )

    updated, error, status = await autonudge_authz.authorize_and_update_monitor(
        svc=svc,
        state=state,
        loop_id=loop.id,
        session_key=loop.slot_key,
        patch={"target": "bitbucket.org/acme/widgets#11"},
        source="dashboard",
        grant_owner_provider_credentials=True,
    )

    assert updated is None and status == 503
    assert error == "monitor credential authorization unavailable — prior monitor restored"
    restored = svc.get_by_id(loop.id)
    assert restored is loop
    assert restored.monitor is not None
    assert restored.monitor.target == "bitbucket.org/acme/widgets#10"
    assert restored.monitor.config_generation == 1
    reloaded = AutoNudgeService(tmp_path / "store")
    reloaded._load()
    persisted = reloaded.get_by_id(loop.id)
    assert persisted is not None and persisted.monitor is not None
    assert persisted.monitor.target == "bitbucket.org/acme/widgets#10"
    svc.stop()


@pytest.mark.asyncio
async def test_failed_update_grant_preserves_a_concurrent_committed_patch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    svc = AutoNudgeService(tmp_path / "store")
    loop = await svc.add_monitor(
        slot_key="chat-1",
        kind="bitbucket_pull_request",
        target="bitbucket.org/acme/widgets#10",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(max_runtime_secs=600),
        creation_surface=MonitorCreationSurface.DASHBOARD,
    )
    assert loop.monitor is not None
    trust.record_monitor_owner_credentials(
        loop.id,
        loop.slot_key,
        loop.monitor.kind,
        loop.monitor.target,
    )
    monkeypatch.setattr(
        autonudge_authz,
        "sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **_kwargs: None),
    )

    def fail_record(*_args: Any) -> None:
        raise OSError("transient vault failure")

    monkeypatch.setattr(trust, "record_monitor_owner_credentials", fail_record)
    state = SimpleNamespace(
        _slots={"chat-1": SimpleNamespace(mode="chat", memory_mode="persistent")},
        sessions=None,
        channel_transports={},
    )
    update_entered = asyncio.Event()
    continue_update = asyncio.Event()
    update_monitor = svc.update_monitor

    async def delay_target_update(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("target") == "bitbucket.org/acme/widgets#11":
            update_entered.set()
            await continue_update.wait()
        return await update_monitor(*args, **kwargs)

    monkeypatch.setattr(svc, "update_monitor", delay_target_update)
    failing_update = asyncio.create_task(
        autonudge_authz.authorize_and_update_monitor(
            svc=svc,
            state=state,
            loop_id=loop.id,
            session_key=loop.slot_key,
            patch={"target": "bitbucket.org/acme/widgets#11"},
            source="dashboard",
            grant_owner_provider_credentials=True,
        )
    )
    await asyncio.wait_for(update_entered.wait(), timeout=1)
    concurrent = await update_monitor(loop.id, wake_instructions="Keep the committed edit.")
    assert concurrent is loop
    continue_update.set()

    updated, error, status = await asyncio.wait_for(failing_update, timeout=1)

    assert updated is None and status == 503
    assert error == "monitor credential authorization unavailable — prior monitor restored"
    restored = svc.get_by_id(loop.id)
    assert restored is loop and restored.monitor is not None
    assert restored.monitor.target == "bitbucket.org/acme/widgets#10"
    assert restored.monitor.wake_instructions == "Keep the committed edit."
    reloaded = AutoNudgeService(tmp_path / "store")
    reloaded._load()
    persisted = reloaded.get_by_id(loop.id)
    assert persisted is not None and persisted.monitor is not None
    assert persisted.monitor.target == "bitbucket.org/acme/widgets#10"
    assert persisted.monitor.wake_instructions == "Keep the committed edit."
    svc.stop()


@pytest.mark.asyncio
async def test_failed_update_revocation_restores_the_prior_monitor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    svc = AutoNudgeService(tmp_path / "store")
    loop = await svc.add_monitor(
        slot_key="chat-1",
        kind="bitbucket_pull_request",
        target="bitbucket.org/acme/widgets#10",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(max_runtime_secs=600),
        creation_surface=MonitorCreationSurface.DASHBOARD,
    )
    assert loop.monitor is not None
    trust.record_monitor_owner_credentials(
        loop.id,
        loop.slot_key,
        loop.monitor.kind,
        loop.monitor.target,
    )
    monkeypatch.setattr(
        autonudge_authz,
        "sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **_kwargs: None),
    )
    revocations_path = trust.monitor_owner_credentials_revocations_path()
    real_atomic_write = trust.atomic_write

    def fail_tombstone(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == revocations_path:
            raise OSError("tombstone unavailable")
        real_atomic_write(path, *args, **kwargs)

    monkeypatch.setattr(trust, "atomic_write", fail_tombstone)
    state = SimpleNamespace(
        _slots={"chat-1": SimpleNamespace(mode="chat", memory_mode="persistent")},
        sessions=None,
        channel_transports={},
    )

    updated, error, status = await autonudge_authz.authorize_and_update_monitor(
        svc=svc,
        state=state,
        loop_id=loop.id,
        session_key=loop.slot_key,
        patch={"target": "bitbucket.org/acme/widgets#11"},
        source="mcp",
    )

    assert updated is None and status == 503
    assert error == "monitor credential revocation unavailable — prior monitor restored"
    restored = svc.get_by_id(loop.id)
    assert restored is loop
    assert restored.monitor is not None
    assert restored.monitor.target == "bitbucket.org/acme/widgets#10"
    assert restored.monitor.config_generation == 1
    reloaded = AutoNudgeService(tmp_path / "store")
    reloaded._load()
    persisted = reloaded.get_by_id(loop.id)
    assert persisted is not None and persisted.monitor is not None
    assert persisted.monitor.target == "bitbucket.org/acme/widgets#10"
    with trust._PENDING_REVOCATIONS_LOCK:
        trust._PENDING_REVOCATIONS.clear()
    assert trust.is_monitor_owner_credentials_recorded(
        loop.id,
        loop.slot_key,
        loop.monitor.kind,
        "bitbucket.org/acme/widgets#10",
    )
    assert not trust.is_monitor_owner_credentials_recorded(
        loop.id,
        loop.slot_key,
        loop.monitor.kind,
        "bitbucket.org/acme/widgets#11",
    )
    svc.stop()
