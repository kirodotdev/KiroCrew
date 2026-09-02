"""Coordinator-first restart recovery and legacy import contracts."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from conftest import requires_symlinks
from kiro_crew import platform_compat
from kiro_crew.run_coordinator import (
    CommandOperation,
    CoordinatorDecision,
    MemoryRunCoordinator,
    ObservedState,
    OwnerLease,
    RunOutcome,
    SubmitRun,
)
from kiro_crew.run_coordinator import legacy as legacy_module
from kiro_crew.run_coordinator.delivery import OutboxDeliveryAdapter
from kiro_crew.run_coordinator.legacy import LegacyRunImporter
from kiro_crew.run_coordinator.recovery import RunRecovery

requires_pinned_tree_walk = pytest.mark.skipif(
    not legacy_module.supports_pinned_tree_walk(),
    reason="legacy import requires descriptor-pinned tree traversal",
)


@pytest.mark.skipif(not hasattr(os, "O_NONBLOCK"), reason="O_NONBLOCK unavailable")
def test_legacy_file_opens_are_nonblocking() -> None:
    assert legacy_module._FILE_OPEN_FLAGS & os.O_NONBLOCK


@pytest.mark.asyncio
async def test_legacy_import_skips_when_pinned_tree_walk_is_unavailable(
    tmp_path,
    monkeypatch,
) -> None:
    _legacy_state(tmp_path, "unsupported1")
    coordinator = MemoryRunCoordinator()
    monkeypatch.setattr(legacy_module, "supports_pinned_tree_walk", lambda: False)

    report = await LegacyRunImporter(coordinator, root=tmp_path).import_all()

    assert report == legacy_module.LegacyImportReport()
    assert await coordinator.get_run("unsupported1") is None


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _legacy_state(root: Path, run_id: str, **fields: object) -> Path:
    folder = root / run_id
    _write_json(
        folder / "state.json",
        {
            "id": run_id,
            "task": "recover this work",
            "agent": "kirocrew",
            "parent_session": "dashboard:default",
            "started": 10.0,
            "updated_at": 20.0,
            "status": "running",
            "pid": None,
            **fields,
        },
    )
    return folder


async def _record_protected_process(
    coordinator: MemoryRunCoordinator,
    clock: list[float],
    run_id: str,
    process_id: int,
    process_start_id: str,
    *,
    process_owned: bool = True,
) -> None:
    submitted = await coordinator.submit(
        SubmitRun(
            run_id=run_id,
            command_id=f"command-{run_id}",
            idempotency_key=f"key-{run_id}",
            payload_hash=f"hash-{run_id}",
            parent_session="dashboard:default",
            agent="kirocrew",
            task="recover this work",
            conversation_key="",
            operation=CommandOperation.SPAWN,
        )
    )
    assert submitted.value is not None
    claim = await coordinator.claim_command(
        f"command-{run_id}",
        OwnerLease("executor", clock[0] + 10.0),
    )
    assert claim is not None
    starting = await coordinator.mark_starting(
        claim.command,
        claim.fence,
        claim.run.version,
    )
    assert starting.value is not None
    running = await coordinator.mark_running(run_id, claim.fence, starting.value.version)
    assert running.value is not None
    recorded = await coordinator.record_process(
        run_id,
        claim.fence,
        running.value.version,
        process_id,
        process_start_id,
        process_owned,
    )
    assert recorded.value is not None
    clock[0] += 11.0


@pytest.mark.asyncio
@requires_pinned_tree_walk
async def test_legacy_import_is_idempotent_and_leaves_files_byte_identical(tmp_path):
    clock = [100.0]
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0], id_factory=lambda: "event-1")
    _legacy_state(tmp_path, "running1", keep=True)
    delivered = _legacy_state(tmp_path, "done1")
    (delivered / "result.txt").write_text("finished", encoding="utf-8")
    _write_json(
        delivered / "tombstone.json",
        {
            "id": "done1",
            "cause": "delivered",
            "recovery_action": "delivered",
            "died": 30.0,
            "result_available": True,
            "result_path": str(delivered / "result.txt"),
        },
    )
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    importer = LegacyRunImporter(coordinator, root=tmp_path, clock=lambda: clock[0])
    first = await importer.import_all()
    second = await importer.import_all()

    assert first.imported == 2
    assert second.imported == 0
    assert second.existing == 2
    assert await coordinator.get_run("running1") is not None
    terminal = await coordinator.get_run("done1")
    assert terminal is not None
    assert terminal.observed_state is ObservedState.TERMINAL
    assert terminal.outcome is RunOutcome.COMPLETED
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


@pytest.mark.asyncio
@requires_pinned_tree_walk
async def test_legacy_import_submits_one_coordinator_batch_per_scan(tmp_path):
    coordinator = MemoryRunCoordinator()
    coordinator.import_legacy_batch = AsyncMock(  # type: ignore[method-assign]
        wraps=coordinator.import_legacy_batch
    )
    for index in range(20):
        _legacy_state(tmp_path, f"run-{index}")

    report = await LegacyRunImporter(coordinator, root=tmp_path).import_all()

    assert report.imported == 20
    coordinator.import_legacy_batch.assert_awaited_once()
    requests = coordinator.import_legacy_batch.await_args.args[0]
    assert len(requests) == 20


@pytest.mark.asyncio
@requires_pinned_tree_walk
async def test_legacy_import_skips_locally_active_run_ids(tmp_path):
    coordinator = MemoryRunCoordinator()
    _legacy_state(tmp_path, "active")
    _legacy_state(tmp_path, "orphan")

    report = await LegacyRunImporter(coordinator, root=tmp_path).import_all(
        exclude_run_ids=frozenset({"active"})
    )

    assert report.imported == 1
    assert await coordinator.get_run("active") is None
    assert await coordinator.get_run("orphan") is not None


@pytest.mark.asyncio
@requires_pinned_tree_walk
async def test_legacy_process_metadata_never_authorizes_termination(tmp_path):
    clock = [100.0]
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0], id_factory=lambda: "event-1")
    _legacy_state(
        tmp_path,
        "failed-live",
        pid=4321,
        pid_start_id="same",
        process_owned=True,
    )
    importer = LegacyRunImporter(coordinator, root=tmp_path, clock=lambda: clock[0])
    imported = await importer.import_all()
    run = await coordinator.get_run("failed-live")

    assert imported.imported == 1
    assert run is not None
    assert run.observed_state is ObservedState.RUNNING
    assert run.outcome is None
    assert run.process_id == 0
    assert run.process_owned is False

    terminated: list[int] = []
    recovery = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(
            coordinator,
            AsyncMock(return_value=True),
            owner_id="delivery",
            clock=lambda: clock[0],
        ),
        owner_id="recovery",
        clock=lambda: clock[0],
        terminate_process=terminated.append,
        process_identity=lambda _pid: "same",
        process_alive=lambda _pid: True,
    )

    report = await recovery.reconcile()
    recovered = await coordinator.get_run("failed-live")

    assert report.terminated == 0
    assert report.interrupted == 1
    assert terminated == []
    assert recovered is not None
    assert recovered.outcome is RunOutcome.INTERRUPTED


@pytest.mark.asyncio
@requires_pinned_tree_walk
async def test_legacy_import_skips_corrupt_and_traversal_like_folders(tmp_path, caplog):
    bad = tmp_path / "corrupt"
    bad.mkdir()
    (bad / "state.json").write_text("{broken", encoding="utf-8")
    mismatched = _legacy_state(tmp_path, "folder-id")
    state = json.loads((mismatched / "state.json").read_text(encoding="utf-8"))
    state["id"] = "../escape"
    _write_json(mismatched / "state.json", state)
    coordinator = MemoryRunCoordinator()

    report = await LegacyRunImporter(coordinator, root=tmp_path).import_all()

    assert report.corrupt == 2
    assert report.imported == 0
    assert "legacy subagent import skipped" in caplog.text


@pytest.mark.asyncio
@requires_pinned_tree_walk
async def test_legacy_import_rejects_run_id_that_redaction_would_change(tmp_path, caplog):
    run_id = "AKIA" + "IOSFODNN7EXAMPLE"
    _legacy_state(tmp_path, run_id)
    coordinator = MemoryRunCoordinator()

    report = await LegacyRunImporter(coordinator, root=tmp_path).import_all()

    assert report.corrupt == 1
    assert report.imported == 0
    assert await coordinator.get_run(run_id) is None
    assert run_id not in caplog.text


@pytest.mark.asyncio
@requires_pinned_tree_walk
async def test_legacy_import_redacts_and_bounds_routing_metadata(tmp_path):
    credential = "AKIAIOSFODNN7EXAMPLE"
    _legacy_state(
        tmp_path,
        "bounded-fields",
        agent="a" * 300 + credential,
        conversation_key="c" * 600 + credential,
    )
    coordinator = MemoryRunCoordinator()

    report = await LegacyRunImporter(coordinator, root=tmp_path).import_all()

    assert report.imported == 1
    run = await coordinator.get_run("bounded-fields")
    assert run is not None
    assert len(run.agent) == 200
    assert len(run.conversation_key) == 500
    assert credential not in run.agent
    assert credential not in run.conversation_key


@pytest.mark.asyncio
@requires_pinned_tree_walk
async def test_legacy_import_skips_deep_json_without_blocking_siblings(tmp_path):
    deep = tmp_path / "deep-json"
    deep.mkdir()
    depth = 2000
    (deep / "state.json").write_text(
        "[" * depth + "0" + "]" * depth,
        encoding="utf-8",
    )
    _legacy_state(tmp_path, "good-json")
    coordinator = MemoryRunCoordinator()

    report = await LegacyRunImporter(coordinator, root=tmp_path).import_all()

    assert report.corrupt == 1
    assert report.imported == 1
    assert await coordinator.get_run("good-json") is not None


@pytest.mark.asyncio
@requires_symlinks
@requires_pinned_tree_walk
async def test_legacy_import_bounds_and_pins_json_reads(tmp_path):
    import_root = tmp_path / "legacy"
    import_root.mkdir()
    oversized = import_root / "oversized"
    oversized.mkdir()
    (oversized / "state.json").write_bytes(b" " * (1024 * 1024 + 1))

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside = outside_dir / "state.json"
    _write_json(outside, {"id": "linked"})
    linked = import_root / "linked"
    linked.mkdir()
    (linked / "state.json").symlink_to(outside)

    _legacy_state(import_root, "good-bounded")
    coordinator = MemoryRunCoordinator()

    report = await LegacyRunImporter(coordinator, root=import_root).import_all()

    assert report.corrupt == 2
    assert report.imported == 1
    assert await coordinator.get_run("good-bounded") is not None


@pytest.mark.asyncio
@requires_symlinks
@requires_pinned_tree_walk
async def test_legacy_import_accepts_a_symlinked_trusted_data_home(tmp_path, monkeypatch):
    target_home = tmp_path / "target-home"
    legacy_root = target_home / "subagents"
    legacy_root.mkdir(parents=True)
    _legacy_state(legacy_root, "linked-home-run")
    configured_home = tmp_path / "configured-home"
    configured_home.symlink_to(target_home, target_is_directory=True)
    monkeypatch.setattr(legacy_module, "data_home", lambda: configured_home)
    coordinator = MemoryRunCoordinator()

    report = await LegacyRunImporter(coordinator).import_all()

    assert report.imported == 1
    assert await coordinator.get_run("linked-home-run") is not None


@pytest.mark.asyncio
@requires_symlinks
@requires_pinned_tree_walk
async def test_legacy_import_rejects_linked_folder_before_directory_probe(tmp_path, monkeypatch):
    import_root = tmp_path / "legacy"
    import_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = _legacy_state(import_root, "linked")
    parked = import_root / "parked"
    _legacy_state(import_root, "good-local")
    coordinator = MemoryRunCoordinator()
    real_is_directory = LegacyRunImporter._is_directory

    def _swap_after_directory_classification(base_fd, name):
        result = real_is_directory(base_fd, name)
        if name == "linked" and result:
            linked.rename(parked)
            linked.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(
        LegacyRunImporter,
        "_is_directory",
        staticmethod(_swap_after_directory_classification),
    )

    report = await LegacyRunImporter(coordinator, root=import_root).import_all()

    assert report.imported == 1
    assert await coordinator.get_run("good-local") is not None


@pytest.mark.asyncio
@requires_pinned_tree_walk
async def test_legacy_import_closes_pinned_root_off_loop(tmp_path, monkeypatch):
    _legacy_state(tmp_path, "good-local")
    coordinator = MemoryRunCoordinator()
    real_to_thread = asyncio.to_thread
    close_off_loop = False

    async def _track_to_thread(function, /, *args, **kwargs):
        nonlocal close_off_loop
        if function is os.close:
            close_off_loop = True
        return await real_to_thread(function, *args, **kwargs)

    monkeypatch.setattr(legacy_module.asyncio, "to_thread", _track_to_thread)

    report = await LegacyRunImporter(coordinator, root=tmp_path).import_all()

    assert report.imported == 1
    assert close_off_loop is True


@pytest.mark.asyncio
@requires_pinned_tree_walk
async def test_legacy_import_skips_non_finite_timestamp_without_blocking_siblings(tmp_path):
    _legacy_state(tmp_path, "bad-time", started=float("nan"))
    _legacy_state(tmp_path, "good-time")
    coordinator = MemoryRunCoordinator()

    report = await LegacyRunImporter(coordinator, root=tmp_path).import_all()

    assert report.corrupt == 1
    assert report.imported == 1
    assert await coordinator.get_run("bad-time") is None
    assert await coordinator.get_run("good-time") is not None


@pytest.mark.asyncio
@requires_pinned_tree_walk
async def test_legacy_import_skips_oversized_timestamp_without_blocking_siblings(tmp_path):
    bad = _legacy_state(tmp_path, "bad-time", started=10**400)
    _legacy_state(tmp_path, "good-time")
    before = (bad / "state.json").read_bytes()
    coordinator = MemoryRunCoordinator()

    report = await LegacyRunImporter(coordinator, root=tmp_path).import_all()

    assert report.corrupt == 1
    assert report.imported == 1
    assert await coordinator.get_run("bad-time") is None
    assert await coordinator.get_run("good-time") is not None
    assert (bad / "state.json").read_bytes() == before


@pytest.mark.asyncio
async def test_recovery_takeover_fences_old_owner_without_replaying_command(tmp_path):
    clock = [10.0]
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0], id_factory=lambda: "event-1")
    submitted = await coordinator.submit(
        SubmitRun(
            run_id="run1",
            command_id="command1",
            idempotency_key="key1",
            payload_hash="hash1",
            parent_session="dashboard:default",
            agent="kirocrew",
            task="work",
            conversation_key="",
            operation=CommandOperation.SPAWN,
        )
    )
    assert submitted.value is not None
    old_claim = await coordinator.claim_command("command1", OwnerLease("old", 20.0))
    assert old_claim is not None
    clock[0] = 21.0
    delivered = AsyncMock(return_value=True)
    recovery = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(
            coordinator,
            delivered,
            owner_id="delivery",
            clock=lambda: clock[0],
        ),
        owner_id="recovery",
        clock=lambda: clock[0],
    )

    report = await recovery.reconcile()

    assert report.interrupted == 1
    run = await coordinator.get_run("run1")
    assert run is not None
    assert run.outcome is RunOutcome.INTERRUPTED
    assert run.lease_epoch == old_claim.fence.lease_epoch + 1
    stale = await coordinator.complete(
        recovery.completion_for(run, outcome=RunOutcome.COMPLETED),
        old_claim.fence,
        old_claim.run.version,
    )
    assert stale.decision is CoordinatorDecision.REJECTED
    stale_exact = await coordinator.complete(
        recovery.completion_for(run),
        old_claim.fence,
        old_claim.run.version,
    )
    assert stale_exact.decision is CoordinatorDecision.REJECTED
    assert delivered.await_count == 1
    receipt = await coordinator.get_command_by_key("key1")
    assert receipt is not None
    assert receipt.command.attempt == 1


@pytest.mark.asyncio
async def test_recovery_terminates_only_verified_live_child(tmp_path):
    clock = [100.0]
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0], id_factory=lambda: "event-1")
    await _record_protected_process(coordinator, clock, "live1", 4321, "same")
    terminated: list[int] = []
    recovery = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(
            coordinator,
            AsyncMock(return_value=True),
            owner_id="delivery",
            clock=lambda: clock[0],
        ),
        owner_id="recovery",
        clock=lambda: clock[0],
        terminate_process=terminated.append,
        process_identity=lambda _pid: "same",
        process_alive=lambda _pid: True,
    )

    report = await recovery.reconcile()

    assert report.terminated == 1
    assert terminated == [4321]


@pytest.mark.asyncio
async def test_recovery_cleans_terminal_child_without_replacing_outcome(tmp_path):
    clock = [100.0]
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0], id_factory=lambda: "event-1")
    await _record_protected_process(coordinator, clock, "live1", 4321, "same")
    claim = await coordinator.claim_recovery(OwnerLease("terminal", clock[0] + 10), 1)
    assert len(claim) == 1
    completed = await coordinator.complete(
        RunRecovery(
            coordinator,
            OutboxDeliveryAdapter(coordinator, AsyncMock(return_value=True)),
            clock=lambda: clock[0],
        ).completion_for(claim[0].run, outcome=RunOutcome.COMPLETED),
        claim[0].fence,
        claim[0].run.version,
    )
    assert completed.value is not None
    clock[0] += 11.0
    delivered = AsyncMock(return_value=True)
    terminated: list[int] = []
    recovery = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(
            coordinator,
            delivered,
            owner_id="delivery",
            clock=lambda: clock[0],
        ),
        owner_id="recovery",
        clock=lambda: clock[0],
        terminate_process=terminated.append,
        process_identity=lambda _pid: "same",
        process_alive=lambda _pid: True,
    )

    report = await recovery.reconcile()
    run = await coordinator.get_run("live1")

    assert report.interrupted == 0
    assert report.terminated == 1
    assert report.delivered == 1
    assert terminated == [4321]
    delivered.assert_awaited_once()
    assert run is not None
    assert run.observed_state is ObservedState.TERMINAL
    assert run.outcome is RunOutcome.COMPLETED
    assert run.process_id == 0
    assert run.process_start_id == ""
    assert run.process_owned is False


@pytest.mark.asyncio
async def test_recovery_defers_terminal_delivery_when_child_cleanup_fails(tmp_path):
    clock = [100.0]
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0], id_factory=lambda: "event-1")
    await _record_protected_process(coordinator, clock, "live1", 4321, "same")
    claim = await coordinator.claim_recovery(OwnerLease("terminal", clock[0] + 10), 1)
    assert len(claim) == 1
    completed = await coordinator.complete(
        RunRecovery(
            coordinator,
            OutboxDeliveryAdapter(coordinator, AsyncMock(return_value=True)),
            clock=lambda: clock[0],
        ).completion_for(claim[0].run, outcome=RunOutcome.COMPLETED),
        claim[0].fence,
        claim[0].run.version,
    )
    assert completed.value is not None
    clock[0] += 11.0
    delivered = AsyncMock(return_value=True)

    def refuse_termination(_pid: int) -> None:
        raise PermissionError("access denied")

    recovery = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(
            coordinator,
            delivered,
            owner_id="delivery",
            clock=lambda: clock[0],
        ),
        owner_id="recovery",
        clock=lambda: clock[0],
        terminate_process=refuse_termination,
        process_identity=lambda _pid: "same",
        process_alive=lambda _pid: True,
    )

    report = await recovery.reconcile()
    run = await coordinator.get_run("live1")

    assert report.interrupted == 0
    assert report.terminated == 0
    assert report.delivered == 0
    delivered.assert_not_awaited()
    assert run is not None
    assert run.outcome is RunOutcome.COMPLETED
    assert run.process_owned is True

    clock[0] += 60.0
    second_report = await recovery.reconcile()

    assert second_report.delivered == 0
    delivered.assert_not_awaited()
    leased_run = await coordinator.get_run("live1")
    assert leased_run is not None
    assert leased_run.process_owned is True


@pytest.mark.asyncio
async def test_recovery_drains_unrelated_completion_when_terminal_child_cleanup_fails(tmp_path):
    clock = [100.0]
    event_ids = iter(("protected-event", "unrelated-event"))
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0], id_factory=event_ids.__next__)
    await _record_protected_process(coordinator, clock, "protected1", 4321, "same")
    protected_claim = await coordinator.claim_recovery(OwnerLease("terminal", clock[0] + 10), 1)
    assert len(protected_claim) == 1
    completion_factory = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(coordinator, AsyncMock(return_value=True)),
        clock=lambda: clock[0],
    )
    protected = await coordinator.complete(
        completion_factory.completion_for(
            protected_claim[0].run,
            outcome=RunOutcome.COMPLETED,
        ),
        protected_claim[0].fence,
        protected_claim[0].run.version,
    )
    assert protected.value is not None
    clock[0] += 11.0

    submitted = await coordinator.submit(
        SubmitRun(
            run_id="unrelated1",
            command_id="command-unrelated1",
            idempotency_key="key-unrelated1",
            payload_hash="hash-unrelated1",
            parent_session="dashboard:default",
            agent="kirocrew",
            task="finish independently",
            conversation_key="",
            operation=CommandOperation.SPAWN,
        )
    )
    assert submitted.value is not None
    unrelated_claim = await coordinator.claim_command(
        "command-unrelated1",
        OwnerLease("executor", clock[0] + 10),
    )
    assert unrelated_claim is not None
    starting = await coordinator.mark_starting(
        unrelated_claim.command,
        unrelated_claim.fence,
        unrelated_claim.run.version,
    )
    assert starting.value is not None
    running = await coordinator.mark_running(
        "unrelated1",
        unrelated_claim.fence,
        starting.value.version,
    )
    assert running.value is not None
    unrelated = await coordinator.complete(
        completion_factory.completion_for(running.value, outcome=RunOutcome.COMPLETED),
        unrelated_claim.fence,
        running.value.version,
    )
    assert unrelated.value is not None
    delivered = AsyncMock(return_value=True)

    def refuse_termination(_pid: int) -> None:
        raise PermissionError("access denied")

    recovery = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(
            coordinator,
            delivered,
            owner_id="delivery",
            clock=lambda: clock[0],
        ),
        owner_id="recovery",
        clock=lambda: clock[0],
        terminate_process=refuse_termination,
        process_identity=lambda _pid: "same",
        process_alive=lambda _pid: True,
    )

    report = await recovery.reconcile()

    assert report.delivered == 1
    delivered.assert_awaited_once()
    assert delivered.await_args.args[0].run_id == "unrelated1"
    protected_run = await coordinator.get_run("protected1")
    assert protected_run is not None
    assert protected_run.process_owned is True


@pytest.mark.asyncio
async def test_recovery_refuses_termination_when_sel_audit_fails(tmp_path, monkeypatch):
    clock = [100.0]
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0], id_factory=lambda: "event-1")
    await _record_protected_process(coordinator, clock, "live1", 4321, "same")
    terminated: list[int] = []
    audit = MagicMock()
    audit.log_tool_invocation.side_effect = OSError("audit unavailable")
    monkeypatch.setattr("kiro_crew.run_coordinator.recovery.sel", lambda: audit)
    recovery = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(
            coordinator,
            AsyncMock(return_value=True),
            owner_id="delivery",
            clock=lambda: clock[0],
        ),
        owner_id="recovery",
        clock=lambda: clock[0],
        terminate_process=terminated.append,
        process_identity=lambda _pid: "same",
        process_alive=lambda _pid: True,
    )

    report = await recovery.reconcile()
    run = await coordinator.get_run("live1")

    assert report.terminated == 0
    assert terminated == []
    assert run is not None
    assert run.outcome is None


@pytest.mark.asyncio
async def test_recovery_default_termination_pins_the_recorded_process_identity(
    tmp_path,
    monkeypatch,
):
    clock = [100.0]
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0], id_factory=lambda: "event-1")
    await _record_protected_process(coordinator, clock, "live1", 4321, "same")
    calls: list[tuple[int, str, int]] = []

    def pinned_kill(pid: int, start_id: str, sig: int) -> bool:
        calls.append((pid, start_id, sig))
        return True

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_compat, "kill_process_tree_pinned", pinned_kill)
    audit = MagicMock()
    audit_threads: list[int] = []
    audit.log_tool_invocation.side_effect = lambda **_kwargs: audit_threads.append(
        threading.get_ident()
    )
    event_loop_thread = threading.get_ident()
    monkeypatch.setattr("kiro_crew.run_coordinator.recovery.sel", lambda: audit)
    recovery = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(
            coordinator,
            AsyncMock(return_value=True),
            owner_id="delivery",
            clock=lambda: clock[0],
        ),
        owner_id="recovery",
        clock=lambda: clock[0],
        process_identity=lambda _pid: "same",
        process_alive=lambda _pid: True,
    )

    report = await recovery.reconcile()

    assert report.terminated == 1
    assert calls == [(4321, "same", platform_compat.SIGKILL)]
    assert audit_threads
    assert audit_threads[0] != event_loop_thread
    audit.log_tool_invocation.assert_called_once_with(
        session_key="subagent:live1",
        source="subagent",
        tool_name="orphan_reconcile_kill",
        outcome="kill_authorized",
        metadata={"subagent_id": "live1", "pid": 4321},
        critical=True,
    )


@pytest.mark.asyncio
async def test_recovery_uses_default_pinned_termination_on_posix(
    tmp_path,
    monkeypatch,
):
    clock = [100.0]
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0], id_factory=lambda: "event-1")
    await _record_protected_process(coordinator, clock, "live1", 4321, "same")
    pinned_kill = MagicMock(return_value=True)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "kill_process_tree_pinned", pinned_kill)
    recovery = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(
            coordinator,
            AsyncMock(return_value=True),
            owner_id="delivery",
            clock=lambda: clock[0],
        ),
        owner_id="recovery",
        clock=lambda: clock[0],
        process_identity=lambda _pid: "same",
        process_alive=lambda _pid: True,
    )

    report = await recovery.reconcile()
    run = await coordinator.get_run("live1")

    assert report.interrupted == 1
    assert report.terminated == 1
    pinned_kill.assert_called_once_with(4321, "same", platform_compat.SIGKILL)
    assert run is not None
    assert run.observed_state is ObservedState.TERMINAL


@pytest.mark.asyncio
async def test_recovery_never_terminates_a_shared_runtime(tmp_path):
    clock = [100.0]
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0], id_factory=lambda: "event-1")
    await _record_protected_process(
        coordinator,
        clock,
        "shared1",
        4321,
        "",
        process_owned=False,
    )
    terminated: list[int] = []
    recovery = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(
            coordinator,
            AsyncMock(return_value=True),
            owner_id="delivery",
            clock=lambda: clock[0],
        ),
        owner_id="recovery",
        clock=lambda: clock[0],
        terminate_process=terminated.append,
        process_identity=lambda _pid: "same",
        process_alive=lambda _pid: True,
    )

    report = await recovery.reconcile()

    assert report.terminated == 0
    assert report.interrupted == 1
    assert terminated == []


@pytest.mark.asyncio
async def test_recovery_does_not_terminalize_a_child_that_failed_to_terminate(tmp_path):
    clock = [100.0]
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0], id_factory=lambda: "event-1")
    await _record_protected_process(coordinator, clock, "live1", 4321, "same")

    def refuse_termination(_pid: int) -> None:
        raise PermissionError("access denied")

    recovery = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(
            coordinator,
            AsyncMock(return_value=True),
            owner_id="delivery",
            clock=lambda: clock[0],
        ),
        owner_id="recovery",
        clock=lambda: clock[0],
        terminate_process=refuse_termination,
        process_identity=lambda _pid: "same",
        process_alive=lambda _pid: True,
    )

    report = await recovery.reconcile()
    run = await coordinator.get_run("live1")

    assert report.interrupted == 0
    assert report.terminated == 0
    assert run is not None
    assert run.observed_state is not ObservedState.TERMINAL


@pytest.mark.asyncio
async def test_recovery_does_not_terminalize_when_pinned_kill_loses_identity(tmp_path):
    clock = [100.0]
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0], id_factory=lambda: "event-1")
    await _record_protected_process(coordinator, clock, "live1", 4321, "same")
    recovery = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(
            coordinator,
            AsyncMock(return_value=True),
            owner_id="delivery",
            clock=lambda: clock[0],
        ),
        owner_id="recovery",
        clock=lambda: clock[0],
        terminate_process=lambda _pid: False,
        process_identity=lambda _pid: "same",
        process_alive=lambda _pid: True,
    )

    report = await recovery.reconcile()
    run = await coordinator.get_run("live1")

    assert report.interrupted == 0
    assert report.terminated == 0
    assert run is not None
    assert run.observed_state is not ObservedState.TERMINAL


@pytest.mark.asyncio
async def test_recovery_preserves_live_child_with_unreadable_identity(tmp_path):
    clock = [100.0]
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0], id_factory=lambda: "event-1")
    await _record_protected_process(coordinator, clock, "live1", 4321, "old")
    recovery = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(
            coordinator,
            AsyncMock(return_value=True),
            owner_id="delivery",
            clock=lambda: clock[0],
        ),
        owner_id="recovery",
        clock=lambda: clock[0],
        process_identity=lambda _pid: None,
        process_alive=lambda _pid: True,
    )

    report = await recovery.reconcile()
    run = await coordinator.get_run("live1")

    assert report.interrupted == 0
    assert report.terminated == 0
    assert run is not None
    assert run.observed_state is not ObservedState.TERMINAL


@pytest.mark.asyncio
async def test_recovery_terminalizes_original_child_after_pid_reuse(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_compat, "IS_LINUX", True)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    clock = [100.0]
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0], id_factory=lambda: "event-1")
    await _record_protected_process(coordinator, clock, "live1", 4321, "v1:linux:old:boot")
    terminated: list[int] = []
    recovery = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(
            coordinator,
            AsyncMock(return_value=True),
            owner_id="delivery",
            clock=lambda: clock[0],
        ),
        owner_id="recovery",
        clock=lambda: clock[0],
        terminate_process=terminated.append,
        process_identity=lambda _pid: "v1:linux:reused:boot",
        process_alive=lambda _pid: True,
    )

    report = await recovery.reconcile()
    run = await coordinator.get_run("live1")

    assert report.interrupted == 1
    assert report.terminated == 0
    assert terminated == []
    assert run is not None
    assert run.observed_state is ObservedState.TERMINAL
    assert run.outcome is RunOutcome.INTERRUPTED


@pytest.mark.asyncio
async def test_recovery_preserves_live_child_when_posix_identity_format_drifts(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    clock = [100.0]
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0], id_factory=lambda: "event-1")
    await _record_protected_process(coordinator, clock, "live1", 4321, "old-format")
    recovery = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(
            coordinator,
            AsyncMock(return_value=True),
            owner_id="delivery",
            clock=lambda: clock[0],
        ),
        owner_id="recovery",
        clock=lambda: clock[0],
        process_identity=lambda _pid: "new-format",
        process_alive=lambda _pid: True,
    )

    report = await recovery.reconcile()
    run = await coordinator.get_run("live1")

    assert report.interrupted == 0
    assert report.terminated == 0
    assert run is not None
    assert run.observed_state is not ObservedState.TERMINAL


@pytest.mark.asyncio
async def test_recovery_terminalizes_recycled_pid_with_durable_posix_identity(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    clock = [100.0]
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0], id_factory=lambda: "event-1")
    await _record_protected_process(
        coordinator,
        clock,
        "live1",
        4321,
        "v1:darwin:1724500000.000042",
    )
    recovery = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(
            coordinator,
            AsyncMock(return_value=True),
            owner_id="delivery",
            clock=lambda: clock[0],
        ),
        owner_id="recovery",
        clock=lambda: clock[0],
        process_identity=lambda _pid: "v1:darwin:1724500001.000042",
        process_alive=lambda _pid: True,
    )

    report = await recovery.reconcile()
    run = await coordinator.get_run("live1")

    assert report.interrupted == 1
    assert report.terminated == 0
    assert run is not None
    assert run.observed_state is ObservedState.TERMINAL


@pytest.mark.asyncio
@requires_pinned_tree_walk
async def test_legacy_destination_cannot_create_pending_delivery(tmp_path):
    coordinator = MemoryRunCoordinator(id_factory=lambda: "legacy-event")
    folder = _legacy_state(tmp_path, "pending1")
    (folder / "result.txt").write_text("partial", encoding="utf-8")
    _write_json(
        folder / "tombstone.json",
        {
            "id": "pending1",
            "cause": "gateway_restart",
            "recovery_action": "notification_pending",
            "died": 40.0,
            "outcome": "interrupted",
        },
    )
    delivered = AsyncMock(return_value=True)
    recovery = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(coordinator, delivered, owner_id="delivery"),
        owner_id="recovery",
    )

    report = await recovery.reconcile(importer=LegacyRunImporter(coordinator, root=tmp_path))

    assert report.imported == 1
    assert report.interrupted == 0
    assert report.delivered == 0
    delivered.assert_not_awaited()
    imported = await coordinator.get_run("pending1")
    assert imported is not None
    assert imported.parent_session == ""


@pytest.mark.asyncio
async def test_real_sleeper_is_terminated_after_restart_takeover(tmp_path):
    if not platform_compat.IS_WINDOWS:
        pytest.skip("default identity-pinned termination is available only on Windows")
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=tmp_path,
        start_new_session=platform_compat.IS_POSIX,
        creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        start_id = platform_compat.process_identity_token(process.pid)
        if start_id is None:
            pytest.skip("process identity is unavailable on this host")
        clock = [100.0]
        coordinator = MemoryRunCoordinator(clock=lambda: clock[0])
        await _record_protected_process(
            coordinator,
            clock,
            "sleeper1",
            process.pid,
            start_id,
        )
        recovery = RunRecovery(
            coordinator,
            OutboxDeliveryAdapter(
                coordinator,
                AsyncMock(return_value=True),
                owner_id="delivery",
            ),
            owner_id="recovery",
            clock=lambda: clock[0],
        )

        report = await recovery.reconcile()
        process.wait(timeout=5)

        assert report.terminated == 1
        run = await coordinator.get_run("sleeper1")
        assert run is not None
        assert run.outcome is RunOutcome.INTERRUPTED
    finally:
        if process.poll() is None:
            try:
                platform_compat.kill_process_tree(process.pid, platform_compat.SIGKILL)
            except (OSError, ProcessLookupError, ValueError):
                pass
            process.wait(timeout=5)


@pytest.mark.asyncio
@requires_pinned_tree_walk
async def test_manager_startup_runs_legacy_reaping_before_coordinator_recovery(
    tmp_path,
    monkeypatch,
):
    from kiro_crew.subagent import SubagentManager

    _legacy_state(tmp_path, "startup1")
    (tmp_path / "startup1" / "result.txt").write_text("partial result", encoding="utf-8")
    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", tmp_path)
    coordinator = MemoryRunCoordinator(id_factory=lambda: "startup-event")
    announced = AsyncMock()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        on_done=announced,
        coordinator=coordinator,
    )
    importer = LegacyRunImporter(coordinator, root=tmp_path)
    manager._legacy_run_importer = importer
    manager._run_recovery = RunRecovery(
        coordinator,
        manager._outbox_delivery,
        owner_id="startup-recovery",
    )
    fallback = AsyncMock()
    monkeypatch.setattr(manager, "_reconcile_orphans", fallback)

    await manager._reconcile_startup()

    fallback.assert_awaited_once()
    announced.assert_awaited_once()
    info = announced.await_args.args[0]
    assert info.id == "startup1"
    assert info.error == "interrupted by gateway restart"
    assert info.outcome == "interrupted"
    assert info.result_path == str(tmp_path / "startup1" / "result.txt")
    run = await coordinator.get_run("startup1")
    assert run is not None
    assert run.outcome is RunOutcome.INTERRUPTED


@pytest.mark.asyncio
async def test_manager_startup_fails_closed_when_coordinator_recovery_errors(
    monkeypatch,
) -> None:
    from kiro_crew.subagent import SubagentManager

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=MemoryRunCoordinator(),
    )
    reconcile = AsyncMock(side_effect=OSError("coordinator unavailable"))
    monkeypatch.setattr(manager._run_recovery, "reconcile", reconcile)
    legacy_fallback = AsyncMock()
    monkeypatch.setattr(manager, "_reconcile_orphans", legacy_fallback)

    await manager._reconcile_startup()

    reconcile.assert_awaited_once()
    legacy_fallback.assert_awaited_once()


@pytest.mark.asyncio
async def test_manager_recovery_excludes_only_active_and_queued_runs() -> None:
    from kiro_crew.subagent import SubagentInfo, SubagentManager

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=MemoryRunCoordinator(),
    )
    active = asyncio.create_task(asyncio.Event().wait())
    manager._agents["active"] = SubagentInfo(id="active", task="active")
    manager._agents["retained"] = SubagentInfo(id="retained", task="done", done=True)
    manager._tasks["active"] = active
    manager._scheduler.enqueue({"_preassigned_id": "queued"})
    manager._run_recovery.reconcile = AsyncMock()  # type: ignore[method-assign]
    manager._reconcile_orphans = AsyncMock()  # type: ignore[method-assign]

    try:
        await manager._reconcile_startup()
    finally:
        active.cancel()
        await asyncio.gather(active, return_exceptions=True)

    assert manager._run_recovery.reconcile.await_args.kwargs["exclude_run_ids"] == frozenset(
        {"active", "queued"}
    )


@pytest.mark.asyncio
async def test_recovery_passes_active_run_exclusions_to_legacy_importer(tmp_path) -> None:
    coordinator = MemoryRunCoordinator()
    importer = LegacyRunImporter(coordinator, root=tmp_path)
    importer.import_all = AsyncMock(  # type: ignore[method-assign]
        return_value=legacy_module.LegacyImportReport()
    )
    recovery = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(coordinator, AsyncMock(return_value=True), owner_id="delivery"),
    )
    excluded = frozenset({"active", "queued"})

    await recovery.reconcile(importer=importer, exclude_run_ids=excluded)

    importer.import_all.assert_awaited_once_with(exclude_run_ids=excluded)


@pytest.mark.asyncio
async def test_periodic_recovery_retries_legacy_import(monkeypatch) -> None:
    from kiro_crew.subagent import SubagentManager

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=MemoryRunCoordinator(),
    )
    manager._conv_registry_rebuilt = True
    manager._run_recovery.reconcile = AsyncMock()  # type: ignore[method-assign]
    monkeypatch.setattr("kiro_crew.subagent.compact_cost_log", MagicMock())
    monkeypatch.setattr(manager, "_sweep_stuck_waves", MagicMock())
    monkeypatch.setattr(manager, "_sweep_digest_holds", MagicMock())
    monkeypatch.setattr(manager, "_sweep_conversations", MagicMock())
    monkeypatch.setattr(
        asyncio,
        "sleep",
        AsyncMock(side_effect=[None, asyncio.CancelledError]),
    )
    monkeypatch.setattr(
        asyncio.get_running_loop(),
        "run_in_executor",
        AsyncMock(return_value=None),
    )

    with pytest.raises(asyncio.CancelledError):
        await manager._reaper_loop()

    manager._run_recovery.reconcile.assert_awaited_once_with(
        importer=manager._legacy_run_importer,
        exclude_run_ids=frozenset(),
    )
