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
from kiro_crew import autonudge_selfarm as owner_trust
from kiro_crew.autonudge import (
    INVALID_BOUNDS_REASON,
    AutoNudgeService,
    MonitorUpdateConflict,
)
from kiro_crew.monitoring.models import (
    MonitorBudgets,
    MonitorCreationSurface,
    MonitorOutcome,
    MonitorState,
)

# Bound a broken handshake below pytest's 120s worker-kill timeout, not the
# throughput of the real fsync/replace and credential writes between barriers.
_CONCURRENCY_WATCHDOG_SECS = 30


@pytest.fixture(autouse=True)
def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(trust, "data_home", lambda: tmp_path)
    monkeypatch.setattr(owner_trust, "data_home", lambda: tmp_path)
    monkeypatch.setattr(owner_trust, "_seal_secret", lambda: b"provider-trust-test-key")


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

    trust.activate_monitor_owner_credentials(
        "monitor1",
        "chat-1",
        "bitbucket_pull_request",
        "bitbucket.org/acme/widgets#10",
    )

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
    assert trust.is_monitor_owner_credentials_recorded(
        loop.id,
        loop.slot_key,
        loop.monitor.kind,
        loop.monitor.target,
    )
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


def test_recovery_clears_prior_tombstone_before_dropping_pending_replacement() -> None:
    prior_identity = (
        "prior",
        "chat-1",
        "bitbucket_pull_request",
        "bitbucket.org/acme/widgets#10",
    )
    trust.record_monitor_owner_credentials(*prior_identity)
    trust.prepare_monitor_owner_credentials(
        "replacement",
        prior_identity[1],
        prior_identity[2],
        prior_identity[3],
        replaces_monitor_id=prior_identity[0],
    )
    trust._write_revocations({prior_identity[0]})

    trust.recover_monitor_owner_credentials({prior_identity[0]: prior_identity[1:]})

    assert trust.is_monitor_owner_credentials_recorded(*prior_identity)
    assert not trust.is_monitor_owner_credentials_recorded("replacement", *prior_identity[1:])
    assert set(trust._read_record(strict=True)) == {prior_identity[0]}
    assert trust._read_revocations_strict() == set()


def test_recovery_keeps_pending_replacement_when_tombstone_clear_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prior_identity = (
        "prior",
        "chat-1",
        "bitbucket_pull_request",
        "bitbucket.org/acme/widgets#10",
    )
    trust.record_monitor_owner_credentials(*prior_identity)
    trust.prepare_monitor_owner_credentials(
        "replacement",
        prior_identity[1],
        prior_identity[2],
        prior_identity[3],
        replaces_monitor_id=prior_identity[0],
    )
    trust._write_revocations({prior_identity[0]})
    entries = trust._read_record(strict=True)
    del entries[prior_identity[0]]
    trust._write_record(entries)
    write_revocations = trust._write_revocations

    def fail_tombstone_clear(monitor_ids: set[str]) -> None:
        if prior_identity[0] not in monitor_ids:
            raise OSError("tombstone clear unavailable")
        write_revocations(monitor_ids)

    monkeypatch.setattr(trust, "_write_revocations", fail_tombstone_clear)

    with pytest.raises(OSError, match="tombstone clear unavailable"):
        trust.recover_monitor_owner_credentials({prior_identity[0]: prior_identity[1:]})

    recovered = trust._read_record(strict=True)
    assert set(recovered) == {prior_identity[0], "replacement"}
    assert recovered["replacement"]["active"] is False
    assert trust._read_revocations_strict() == {prior_identity[0]}
    assert not trust.is_monitor_owner_credentials_recorded(*prior_identity)
    assert not trust.is_monitor_owner_credentials_recorded("replacement", *prior_identity[1:])

    monkeypatch.setattr(trust, "_write_revocations", write_revocations)
    trust.recover_monitor_owner_credentials({prior_identity[0]: prior_identity[1:]})
    assert trust.is_monitor_owner_credentials_recorded(*prior_identity)
    assert set(trust._read_record(strict=True)) == {prior_identity[0]}


def test_recovery_retry_after_pending_cleanup_failure_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prior_identity = (
        "prior",
        "chat-1",
        "bitbucket_pull_request",
        "bitbucket.org/acme/widgets#10",
    )
    trust.record_monitor_owner_credentials(*prior_identity)
    trust.prepare_monitor_owner_credentials(
        "replacement",
        prior_identity[1],
        prior_identity[2],
        prior_identity[3],
        replaces_monitor_id=prior_identity[0],
    )
    trust._write_revocations({prior_identity[0]})
    entries = trust._read_record(strict=True)
    del entries[prior_identity[0]]
    trust._write_record(entries)
    write_record = trust._write_record

    def fail_pending_cleanup(candidate: dict[str, dict[str, Any]]) -> None:
        if prior_identity[0] in candidate and "replacement" not in candidate:
            raise OSError("pending cleanup unavailable")
        write_record(candidate)

    monkeypatch.setattr(trust, "_write_record", fail_pending_cleanup)

    with pytest.raises(OSError, match="pending cleanup unavailable"):
        trust.recover_monitor_owner_credentials({prior_identity[0]: prior_identity[1:]})

    recovered = trust._read_record(strict=True)
    assert set(recovered) == {prior_identity[0], "replacement"}
    assert recovered["replacement"]["active"] is False
    assert trust._read_revocations_strict() == set()
    assert trust.is_monitor_owner_credentials_recorded(*prior_identity)
    assert not trust.is_monitor_owner_credentials_recorded("replacement", *prior_identity[1:])

    monkeypatch.setattr(trust, "_write_record", write_record)
    trust.recover_monitor_owner_credentials({prior_identity[0]: prior_identity[1:]})
    assert trust.is_monitor_owner_credentials_recorded(*prior_identity)
    assert set(trust._read_record(strict=True)) == {prior_identity[0]}


def test_recovery_finishes_prior_revocation_before_activating_replacement() -> None:
    prior_identity = (
        "prior",
        "chat-1",
        "bitbucket_pull_request",
        "bitbucket.org/acme/widgets#10",
    )
    trust.record_monitor_owner_credentials(*prior_identity)
    trust.prepare_monitor_owner_credentials(
        "replacement",
        prior_identity[1],
        prior_identity[2],
        prior_identity[3],
        replaces_monitor_id=prior_identity[0],
    )
    trust._write_revocations({prior_identity[0]})

    trust.recover_monitor_owner_credentials({"replacement": prior_identity[1:]})

    assert not trust.is_monitor_owner_credentials_recorded(*prior_identity)
    assert trust.is_monitor_owner_credentials_recorded("replacement", *prior_identity[1:])
    assert set(trust._read_record(strict=True)) == {"replacement"}
    assert trust._read_revocations_strict() == set()


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

        async def commit_monitor_replacement(self, _loop_id: str) -> bool:
            return False

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

    def fail_activation(*_identity: str) -> None:
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
async def test_request_cancellation_during_replacement_settlement_finishes_transaction(
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
        prior.id, prior.slot_key, prior.monitor.kind, prior.monitor.target
    )
    monkeypatch.setattr(
        autonudge_authz,
        "sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **_kwargs: None),
    )
    write_state = svc._write_state
    entered = asyncio.Event()
    release = threading.Event()
    event_loop = asyncio.get_running_loop()

    def commit_then_park(payload: dict[str, Any]) -> None:
        write_state(payload)
        ids = {row.get("id") for row in payload["loops"] if isinstance(row, dict)}
        if prior.id not in ids:
            event_loop.call_soon_threadsafe(entered.set)
            assert release.wait(timeout=_CONCURRENCY_WATCHDOG_SECS)

    monkeypatch.setattr(svc, "_write_state", commit_then_park)
    state = SimpleNamespace(
        _slots={"chat-1": SimpleNamespace(mode="chat", memory_mode="persistent")},
        sessions=None,
        channel_transports={},
    )
    request = asyncio.create_task(
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
    try:
        await asyncio.wait_for(entered.wait(), timeout=_CONCURRENCY_WATCHDOG_SECS)
        request.cancel()
        await asyncio.sleep(0)
        assert not request.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        replacement = svc.get_by_slot(prior.slot_key)
        assert replacement is not None and replacement.id != prior.id
        assert replacement.monitor is not None
        assert not svc._deferred_monitor_replacements
        assert trust.is_monitor_owner_credentials_recorded(
            replacement.id,
            replacement.slot_key,
            replacement.monitor.kind,
            replacement.monitor.target,
        )
    finally:
        release.set()
        svc.stop()


@pytest.mark.asyncio
async def test_request_cancellation_during_activation_joins_and_finalizes(
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
        prior.id, prior.slot_key, prior.monitor.kind, prior.monitor.target
    )
    monkeypatch.setattr(
        autonudge_authz,
        "sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **_kwargs: None),
    )
    activate = trust.activate_monitor_owner_credentials
    entered = asyncio.Event()
    release = threading.Event()
    event_loop = asyncio.get_running_loop()

    def activate_after_barrier(*identity: str) -> None:
        event_loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=_CONCURRENCY_WATCHDOG_SECS)
        activate(*identity)

    monkeypatch.setattr(trust, "activate_monitor_owner_credentials", activate_after_barrier)
    state = SimpleNamespace(
        _slots={"chat-1": SimpleNamespace(mode="chat", memory_mode="persistent")},
        sessions=None,
        channel_transports={},
    )
    request = asyncio.create_task(
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
    try:
        await asyncio.wait_for(entered.wait(), timeout=_CONCURRENCY_WATCHDOG_SECS)
        replacement = svc.get_by_slot(prior.slot_key)
        assert replacement is not None and replacement.monitor is not None
        request.cancel()
        await asyncio.sleep(0)
        assert not request.done()
        assert replacement.id in svc._deferred_monitor_replacements
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert not svc._deferred_monitor_replacements
        assert trust.is_monitor_owner_credentials_recorded(
            replacement.id,
            replacement.slot_key,
            replacement.monitor.kind,
            replacement.monitor.target,
        )
    finally:
        release.set()
        svc.stop()


@pytest.mark.asyncio
async def test_update_is_refused_while_replacement_activation_is_pending(
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
        prior.id, prior.slot_key, prior.monitor.kind, prior.monitor.target
    )
    monkeypatch.setattr(
        autonudge_authz,
        "sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **_kwargs: None),
    )
    activate = trust.activate_monitor_owner_credentials
    entered = asyncio.Event()
    release = threading.Event()
    event_loop = asyncio.get_running_loop()

    def activate_after_barrier(*identity: str) -> None:
        event_loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=_CONCURRENCY_WATCHDOG_SECS)
        activate(*identity)

    monkeypatch.setattr(trust, "activate_monitor_owner_credentials", activate_after_barrier)
    state = SimpleNamespace(
        _slots={"chat-1": SimpleNamespace(mode="chat", memory_mode="persistent")},
        sessions=None,
        channel_transports={},
    )
    request = asyncio.create_task(
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
    try:
        await asyncio.wait_for(entered.wait(), timeout=_CONCURRENCY_WATCHDOG_SECS)
        replacement = svc.get_by_slot(prior.slot_key)
        assert replacement is not None and replacement.monitor is not None
        frozen = svc._serialize_loop(replacement)

        with pytest.raises(MonitorUpdateConflict, match="still being finalized"):
            await svc.update_monitor(
                replacement.id,
                wake_instructions="must not land",
            )
        with pytest.raises(MonitorUpdateConflict, match="still being finalized"):
            await svc.remove(replacement.id)
        assert svc._serialize_loop(replacement) == frozen

        release.set()
        committed, error, status = await request
        assert error is None and status == 200 and committed is replacement
        assert not svc._deferred_monitor_replacements
        assert not trust.is_monitor_owner_credentials_recorded(
            prior.id,
            prior.slot_key,
            prior.monitor.kind,
            prior.monitor.target,
        )
        assert trust.is_monitor_owner_credentials_recorded(
            replacement.id,
            replacement.slot_key,
            replacement.monitor.kind,
            replacement.monitor.target,
        )
        reloaded = AutoNudgeService(tmp_path / "store")
        reloaded._load()
        persisted = reloaded.get_by_id(replacement.id)
        assert persisted is not None
        assert reloaded._serialize_loop(persisted) == frozen
    finally:
        release.set()
        svc.stop()


@pytest.mark.asyncio
async def test_restart_recovers_committed_replacement_fences(tmp_path: Path) -> None:
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
        prior.id, prior.slot_key, prior.monitor.kind, prior.monitor.target
    )
    trust.prepare_monitor_owner_credentials(
        "replacement",
        prior.slot_key,
        prior.monitor.kind,
        prior.monitor.target,
        replaces_monitor_id=prior.id,
    )
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
    assert replacement.monitor is not None
    assert replacement.id in svc._deferred_monitor_replacements
    assert not trust.is_monitor_owner_credentials_recorded(
        replacement.id,
        replacement.slot_key,
        replacement.monitor.kind,
        replacement.monitor.target,
    )
    svc.stop()

    reloaded = AutoNudgeService(tmp_path / "store")
    reloaded._load()
    persisted = reloaded.get_by_slot(prior.slot_key)
    assert persisted is not None and persisted.monitor is not None
    assert persisted.id == replacement.id
    assert trust.is_monitor_owner_credentials_recorded(
        persisted.id,
        persisted.slot_key,
        persisted.monitor.kind,
        persisted.monitor.target,
    )


@pytest.mark.asyncio
async def test_restart_keeps_owner_revoked_for_mutated_valid_same_id_row(
    tmp_path: Path,
) -> None:
    svc = AutoNudgeService(tmp_path / "store")
    loop = await svc.add(slot_key="member-scout", message="before", idle_secs=60)
    owner_trust.record_owner_arm(loop.id, loop.slot_key, txn="exact-prior")
    revocation = owner_trust.begin_owner_arm_revocation(
        loop.id,
        loop.slot_key,
        svc._durable_loop_row(loop),
    )
    assert revocation is not None
    stored = json.loads(svc._path.read_text(encoding="utf-8"))
    row = next(item for item in stored["loops"] if item["id"] == loop.id)
    row["message"] = "changed"
    svc._path.write_text(json.dumps(stored), encoding="utf-8")
    svc.stop()

    reloaded = AutoNudgeService(tmp_path / "store")
    reloaded._load()

    accepted = reloaded.get_by_id(loop.id)
    assert accepted is not None and accepted.message == "changed"
    assert not owner_trust.is_recorded_owner_arm(loop.id, loop.slot_key)


@pytest.mark.asyncio
async def test_restart_keeps_owner_revoked_for_quarantined_same_id_row(
    tmp_path: Path,
) -> None:
    svc = AutoNudgeService(tmp_path / "store")
    loop = await svc.add(slot_key="member-scout", message="before", idle_secs=60)
    owner_trust.record_owner_arm(loop.id, loop.slot_key, txn="exact-prior")
    revocation = owner_trust.begin_owner_arm_revocation(
        loop.id,
        loop.slot_key,
        svc._durable_loop_row(loop),
    )
    assert revocation is not None
    stored = json.loads(svc._path.read_text(encoding="utf-8"))
    row = next(item for item in stored["loops"] if item["id"] == loop.id)
    row["slot_key"] = "member-scout\u0000"
    svc._path.write_text(json.dumps(stored), encoding="utf-8")
    svc.stop()

    reloaded = AutoNudgeService(tmp_path / "store")
    reloaded._load()

    assert reloaded.get_by_id(loop.id) is None
    assert any(item.get("id") == loop.id for item in reloaded._store.quarantined)
    assert not owner_trust.is_recorded_owner_arm(loop.id, loop.slot_key)


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

    async def require_prior_revocation(payload: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        assert not trust.is_monitor_owner_credentials_recorded(
            prior.id,
            prior.slot_key,
            prior.monitor.kind,
            prior.monitor.target,
        )
        return await write_snapshot(payload, **kwargs)

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
async def test_failed_restart_activation_keeps_replacement_frozen_and_restores_prior(
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
        prior.id, prior.slot_key, prior.monitor.kind, prior.monitor.target
    )
    monkeypatch.setattr(
        autonudge_authz,
        "sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **_kwargs: None),
    )
    activation_entered = asyncio.Event()
    continue_activation = threading.Event()
    event_loop = asyncio.get_running_loop()
    write_record = trust._write_record

    def fail_activation_write(entries: dict[str, dict[str, Any]]) -> None:
        if any(entry["active"] for key, entry in entries.items() if key != prior.id):
            event_loop.call_soon_threadsafe(activation_entered.set)
            assert continue_activation.wait(timeout=_CONCURRENCY_WATCHDOG_SECS)
            raise OSError("transient vault failure")
        write_record(entries)

    monkeypatch.setattr(trust, "_write_record", fail_activation_write)
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
    try:
        await asyncio.wait_for(activation_entered.wait(), timeout=_CONCURRENCY_WATCHDOG_SECS)
        replacement = svc.get_by_slot(prior.slot_key)
        assert replacement is not None and replacement.id != prior.id
        frozen = svc._serialize_loop(replacement)
        with pytest.raises(MonitorUpdateConflict, match="still being finalized"):
            await svc.update_monitor(replacement.id, wake_instructions="must not land")
        assert svc._serialize_loop(replacement) == frozen
        continue_activation.set()
        restarted, error, status = await asyncio.wait_for(
            restart, timeout=_CONCURRENCY_WATCHDOG_SECS
        )
    finally:
        continue_activation.set()
        try:
            await asyncio.wait_for(
                asyncio.gather(restart, return_exceptions=True),
                timeout=_CONCURRENCY_WATCHDOG_SECS,
            )
        finally:
            svc.stop()

    assert restarted is None and status == 503
    assert error == "monitor credential authorization unavailable — prior monitor restored"
    current = svc.get_by_slot(prior.slot_key)
    assert current is not None
    assert svc._serialize_loop(current) == svc._serialize_loop(prior)
    assert trust.is_monitor_owner_credentials_recorded(
        prior.id, prior.slot_key, prior.monitor.kind, prior.monitor.target
    )
    assert not trust.is_monitor_owner_credentials_recorded(
        replacement.id,
        replacement.slot_key,
        replacement.monitor.kind,
        replacement.monitor.target,
    )


@pytest.mark.asyncio
async def test_invalid_loop_bounds_stop_before_trust_recovery(tmp_path: Path) -> None:
    svc = AutoNudgeService(tmp_path / "store")
    loop = await svc.add_monitor(
        slot_key="chat-1",
        kind="bitbucket_pull_request",
        target="bitbucket.org/acme/widgets#10",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(max_runtime_secs=600),
    )
    raw = json.loads(svc._path.read_text(encoding="utf-8"))
    row = next(item for item in raw["loops"] if item["id"] == loop.id)
    row["created_ts"] = float("nan")
    row["last_fire_ts"] = float("inf")
    row["max_cycles"] = float("nan")
    row["cycle_count"] = float("inf")
    row["max_runtime_secs"] = 0.5
    svc._path.write_text(json.dumps(raw), encoding="utf-8")
    svc.stop()

    reloaded = AutoNudgeService(tmp_path / "store")
    reloaded._load()

    repaired = reloaded.get_by_id(loop.id)
    assert repaired is not None
    assert repaired.created_ts == 0.0
    assert repaired.last_fire_ts == 0.0
    assert repaired.max_cycles == 0
    assert repaired.cycle_count == 0
    assert repaired.max_runtime_secs == 0
    assert repaired.active is False
    assert repaired.stopped_reason == INVALID_BOUNDS_REASON
    assert repaired.next_due_ts == 0.0
    assert repaired.monitor is not None
    assert repaired.monitor.next_probe_at == 0.0
    assert reloaded._store_dirty is True
    reloaded.stop()


def _write_legacy_row(store_dir: Path, **row: Any) -> Path:
    """One plain (non-monitor) loop row, as ``_load`` reads it back from disk."""
    path = store_dir / "autonudge.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    values: dict[str, Any] = {
        "id": "b0d50001",
        "slot_key": "chat-1",
        "message": "go",
        "idle_secs": 60,
        "active": True,
        "next_due_ts": 4_102_444_800.0,
    }
    values.update(row)
    path.write_text(json.dumps({"version": 1, "loops": [values]}), encoding="utf-8")
    return path


def test_repaired_created_ts_against_a_finite_runtime_cap_stops_the_loop(tmp_path: Path) -> None:
    """``created_ts`` is what ``max_runtime_secs`` is measured from: repaired to
    zero, ``runtime_budget_exceeded`` can never trip, so the cap reads as intact
    while the loop runs unlimited. That is an invalid bound, not a cosmetic fix."""
    _write_legacy_row(tmp_path / "store", created_ts=float("nan"), max_runtime_secs=600)

    svc = AutoNudgeService(tmp_path / "store")
    svc._load()
    try:
        loop = svc.get_by_id("b0d50001")
        assert loop is not None
        assert loop.created_ts == 0.0
        assert loop.max_runtime_secs == 600  # the cap itself was fine and is kept
        assert loop.active is False
        assert loop.stopped_reason == INVALID_BOUNDS_REASON
        assert loop.next_due_ts == 0.0
        assert svc._store_dirty is True
    finally:
        svc.stop()


def test_repaired_cycle_count_against_a_finite_cycle_cap_stops_the_loop(tmp_path: Path) -> None:
    """A spent-turn count that cannot be read back is a cap that restarts from
    zero: ``max_cycles`` stays intact on the row while every turn already taken
    against it is forgotten."""
    _write_legacy_row(tmp_path / "store", cycle_count="many", max_cycles=5)

    svc = AutoNudgeService(tmp_path / "store")
    svc._load()
    try:
        loop = svc.get_by_id("b0d50001")
        assert loop is not None
        assert loop.cycle_count == 0
        assert loop.max_cycles == 5
        assert loop.active is False
        assert loop.stopped_reason == INVALID_BOUNDS_REASON
    finally:
        svc.stop()


def test_repaired_anchor_against_an_unlimited_cap_only_repairs(tmp_path: Path) -> None:
    """With no finite cap to measure against, a repaired anchor changes nothing
    about the bound: the value is normalised and persisted, the loop stays armed."""
    _write_legacy_row(
        tmp_path / "store",
        created_ts=float("inf"),
        cycle_count=-3,
        max_cycles=0,
        max_runtime_secs=0,
    )

    svc = AutoNudgeService(tmp_path / "store")
    svc._load()
    try:
        loop = svc.get_by_id("b0d50001")
        assert loop is not None
        assert loop.created_ts == 0.0
        assert loop.cycle_count == 0
        assert loop.active is True
        assert loop.stopped_reason == ""
        assert svc._store_dirty is True
    finally:
        svc.stop()


def test_an_inactive_row_with_an_invalid_bound_is_stamped_too(tmp_path: Path) -> None:
    """The stamp is not gated on ``active``. A paused row whose cap was repaired
    to zero would otherwise keep its earlier reason and, on resume, run
    unlimited with nothing on the row saying why. The reason it gets is
    replaceable, so a deliberate re-arm with fresh bounds still displaces it."""
    from kiro_crew.autonudge_service.model import _stopped_row_is_replaceable

    _write_legacy_row(
        tmp_path / "store",
        active=False,
        stopped_reason="manual",
        stopped_detail="paused before the trip",
        next_due_ts=0.0,
        max_cycles=float("nan"),
    )

    svc = AutoNudgeService(tmp_path / "store")
    svc._load()
    try:
        loop = svc.get_by_id("b0d50001")
        assert loop is not None
        assert loop.active is False
        assert loop.max_cycles == 0
        assert loop.stopped_reason == INVALID_BOUNDS_REASON
        assert loop.stopped_detail == ""  # the earlier stop's words do not describe this one
        assert _stopped_row_is_replaceable(loop) is True
        assert svc._store_dirty is True
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_a_generic_activation_cannot_revive_an_invalid_bounds_row(tmp_path: Path) -> None:
    """``update(active=True)`` with no caps in the patch would clear the reason
    and run the row on its repaired zeros. The row stays inactive, keeps its
    reason, and holds no deadline."""
    _write_legacy_row(
        tmp_path / "store",
        active=True,
        max_cycles=float("nan"),
        max_runtime_secs=float("nan"),
    )

    svc = AutoNudgeService(tmp_path / "store")
    svc._load()
    try:
        loop = svc.get_by_id("b0d50001")
        assert loop is not None
        assert loop.active is False and loop.stopped_reason == INVALID_BOUNDS_REASON

        result = await svc.update("b0d50001", active=True)

        assert result is loop
        assert loop.active is False
        assert loop.stopped_reason == INVALID_BOUNDS_REASON
        assert loop.next_due_ts == 0.0
        assert loop.max_cycles == 0 and loop.max_runtime_secs == 0
        persisted = json.loads(svc._path.read_text(encoding="utf-8"))["loops"][0]
        assert persisted["active"] is False
        assert persisted["stopped_reason"] == INVALID_BOUNDS_REASON
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_an_activation_with_one_finite_cap_still_cannot_revive_it(tmp_path: Path) -> None:
    """The goal popover's save shape: a cap in the patch, the runtime budget
    left to the row. The row does not say which bound was repaired, so a
    supplied finite cap may still be measured against a repaired anchor (a
    zero ``created_ts`` never trips the budget). The cap edit lands on the
    paused row as any cap edit does; the revival does not."""
    _write_legacy_row(tmp_path / "store", created_ts=float("nan"), max_runtime_secs=600)

    svc = AutoNudgeService(tmp_path / "store")
    svc._load()
    try:
        loop = svc.get_by_id("b0d50001")
        assert loop is not None
        assert loop.stopped_reason == INVALID_BOUNDS_REASON

        await svc.update("b0d50001", active=True, max_cycles=10)

        assert loop.active is False
        assert loop.stopped_reason == INVALID_BOUNDS_REASON
        assert loop.max_cycles == 10
        assert loop.max_runtime_secs == 600
        assert loop.next_due_ts == 0.0

        await svc.update("b0d50001", active=True, max_cycles=10, max_runtime_secs=900)

        assert loop.active is False
        assert loop.stopped_reason == INVALID_BOUNDS_REASON
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_lifting_both_caps_explicitly_revives_an_invalid_bounds_row(tmp_path: Path) -> None:
    """The one activation that reads no stored bound: both caps set to zero in
    the patch. This is the owner's Perpetual takeover shape, and its cycle
    accounting and instruction survive as they do for any resumed row."""
    _write_legacy_row(
        tmp_path / "store",
        max_cycles=float("nan"),
        cycle_count=7,
        max_runtime_secs=600,
    )

    svc = AutoNudgeService(tmp_path / "store")
    svc._load()
    try:
        loop = svc.get_by_id("b0d50001")
        assert loop is not None
        assert loop.active is False and loop.stopped_reason == INVALID_BOUNDS_REASON

        result = await svc.update("b0d50001", active=True, max_cycles=0, max_runtime_secs=0)

        assert result is loop
        assert loop.active is True
        assert loop.stopped_reason == ""
        assert loop.max_cycles == 0 and loop.max_runtime_secs == 0
        assert loop.cycle_count == 7
        assert loop.message == "go"
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_a_directive_re_arm_displaces_an_invalid_bounds_row_with_fresh_bounds(
    tmp_path: Path,
) -> None:
    """The bounded remedy: ``add(replace_stopped=True)`` builds a new row with
    its own caps and anchors, and the stamped row is gone rather than revived."""
    _write_legacy_row(tmp_path / "store", max_cycles=float("nan"), cycle_count=7)

    svc = AutoNudgeService(tmp_path / "store")
    svc._load()
    try:
        stamped = svc.get_by_id("b0d50001")
        assert stamped is not None and stamped.stopped_reason == INVALID_BOUNDS_REASON

        fresh = await svc.add(
            slot_key="chat-1",
            message="again",
            idle_secs=60,
            max_cycles=12,
            max_runtime_secs=3600,
            replace_existing=False,
            replace_stopped=True,
        )

        assert fresh.id != stamped.id
        assert svc.get_by_id("b0d50001") is None
        assert [lp.id for lp in svc.list_all()] == [fresh.id]
        assert fresh.active is True
        assert fresh.max_cycles == 12 and fresh.max_runtime_secs == 3600
        assert fresh.cycle_count == 0 and fresh.created_ts > 0
    finally:
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

    write_record = trust._write_record

    def fail_target_write(entries: dict[str, dict[str, Any]]) -> None:
        if entries[loop.id]["target"] == "bitbucket.org/acme/widgets#11":
            raise OSError("transient vault failure")
        write_record(entries)

    monkeypatch.setattr(trust, "_write_record", fail_target_write)
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
            await asyncio.wait_for(continue_update.wait(), timeout=_CONCURRENCY_WATCHDOG_SECS)
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
    try:
        await asyncio.wait_for(update_entered.wait(), timeout=_CONCURRENCY_WATCHDOG_SECS)
        concurrent = await update_monitor(loop.id, wake_instructions="Keep the committed edit.")
        assert concurrent is loop
        continue_update.set()

        updated, error, status = await asyncio.wait_for(
            failing_update, timeout=_CONCURRENCY_WATCHDOG_SECS
        )
    finally:
        continue_update.set()
        try:
            await asyncio.wait_for(
                asyncio.gather(failing_update, return_exceptions=True),
                timeout=_CONCURRENCY_WATCHDOG_SECS,
            )
        finally:
            svc.stop()

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
    assert trust.is_monitor_owner_credentials_recorded(
        loop.id, loop.slot_key, persisted.monitor.kind, "bitbucket.org/acme/widgets#10"
    )
    assert not trust.is_monitor_owner_credentials_recorded(
        loop.id, loop.slot_key, persisted.monitor.kind, "bitbucket.org/acme/widgets#11"
    )


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
