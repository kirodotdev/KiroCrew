"""Behavioral contract shared by durable run coordinator implementations."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

import kiro_crew.config.paths as paths_mod
import kiro_crew.run_coordinator.sqlite as sqlite_mod
import kiro_crew.run_coordinator_anchor as anchor_mod
from kiro_crew.run_coordinator import (
    LEGACY_SHADOW_SOURCE_VERSION,
    CommandClaim,
    CommandFence,
    CommandOperation,
    CommandStatus,
    CoordinatorDecision,
    CoordinatorReason,
    DeliveryFence,
    DeliveryState,
    LegacyRunImport,
    MemoryRunCoordinator,
    ObservedState,
    OwnerLease,
    RunCompletion,
    RunCoordinator,
    RunOutcome,
    RunRecord,
    SQLiteRunCoordinator,
    SubmitControl,
    SubmitRun,
    TerminalRun,
)


class FakeClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(params=("memory", "sqlite"))
def coordinator(
    request: pytest.FixtureRequest,
    clock: FakeClock,
    tmp_path: Path,
) -> RunCoordinator:
    ids = iter(("event-1", "event-2", "event-3"))
    kwargs = {"clock": clock, "id_factory": lambda: next(ids)}
    if request.param == "sqlite":
        return SQLiteRunCoordinator(tmp_path / "coordinator.db", **kwargs)
    return MemoryRunCoordinator(**kwargs)


def _request(
    *,
    run_id: str = "run-1",
    command_id: str = "command-1",
    idempotency_key: str = "key-1",
    payload_json: str = '{"task":"compare the candidates","version":1}',
    payload_hash: str = "hash-1",
    accepted: bool = True,
    operation: CommandOperation = CommandOperation.SPAWN,
) -> SubmitRun:
    return SubmitRun(
        run_id=run_id,
        command_id=command_id,
        idempotency_key=idempotency_key,
        payload_json=payload_json,
        payload_hash=payload_hash,
        parent_session="dashboard:parent",
        agent="researcher",
        task="compare the candidates",
        conversation_key="",
        operation=operation,
        accepted=accepted,
        rejection_reason="governance denied" if not accepted else "",
    )


def test_anchor_is_locked_down_before_payload_and_removed_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = tmp_path / "anchor"
    anchored = tmp_path / "run-coordinator"
    observed_payloads: list[bytes] = []

    def fail_lockdown(path: Path) -> None:
        observed_payloads.append(path.read_bytes())
        raise OSError("lockdown failed")

    monkeypatch.setattr(anchor_mod, "restrict_to_owner", fail_lockdown)

    with pytest.raises(OSError, match="lockdown failed"):
        anchor_mod._create_anchor(record, anchored)

    assert observed_payloads == [b""]
    assert not record.exists()


@pytest.mark.asyncio
async def test_default_sqlite_path_cannot_be_retargeted_after_first_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacing a supported data-home link must not split durable state."""
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(real_home, target_is_directory=True)
    monkeypatch.setattr(
        sqlite_mod,
        "canonical_run_coordinator_dir",
        lambda: linked_home / "run-coordinator",
    )
    coordinator = SQLiteRunCoordinator()

    created = await coordinator.submit(_request())
    assert created.value is not None

    linked_home.rename(tmp_path / "old-linked-home")
    linked_home.mkdir()

    assert await coordinator.get_run("run-1") == created.value.run
    assert (real_home / "run-coordinator" / "coordinator.db").exists()
    assert not (linked_home / "run-coordinator" / "coordinator.db").exists()


@pytest.mark.asyncio
async def test_default_sqlite_path_rejects_retarget_across_gateway_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A durable anchor must fail closed when its selected link moves."""
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    replacement_home = tmp_path / "replacement-home"
    replacement_home.mkdir()
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(real_home, target_is_directory=True)
    anchor_home = tmp_path / "operator-home"
    anchor_home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(linked_home))
    monkeypatch.setattr(paths_mod, "_config_dir_memo", None)
    monkeypatch.setattr(paths_mod, "_resolved_home", None)
    monkeypatch.setattr(anchor_mod, "_anchor_home", lambda: anchor_home)
    anchor_mod._clear_run_coordinator_anchor_cache()

    first = anchor_mod.canonical_run_coordinator_dir()
    assert first == real_home / "run-coordinator"

    linked_home.unlink()
    linked_home.symlink_to(replacement_home, target_is_directory=True)
    paths_mod._config_dir_memo = None
    anchor_mod._clear_run_coordinator_anchor_cache()

    with pytest.raises(OSError, match="does not match the configured data home"):
        anchor_mod.canonical_run_coordinator_dir()
    assert anchor_mod.run_coordinator_anchor_matches_current_home() is False
    coordinator = SQLiteRunCoordinator()
    with pytest.raises(OSError, match="does not match the configured data home"):
        await coordinator.submit(_request())

    assert not (first / "coordinator.db").exists()
    assert not (replacement_home / "run-coordinator" / "coordinator.db").exists()


def test_unconfined_anchor_check_ignores_memoized_data_home_after_retarget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live symlink retarget must outrank the config path memo."""
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    replacement_home = tmp_path / "replacement-home"
    replacement_home.mkdir()
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(real_home, target_is_directory=True)
    anchor_home = tmp_path / "operator-home"
    anchor_home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(linked_home))
    monkeypatch.setattr(paths_mod, "_config_dir_memo", None)
    monkeypatch.setattr(paths_mod, "_resolved_home", None)
    monkeypatch.setattr(anchor_mod, "_anchor_home", lambda: anchor_home)
    anchor_mod._clear_run_coordinator_anchor_cache()

    assert paths_mod.data_home() == real_home
    assert anchor_mod.canonical_run_coordinator_dir() == real_home / "run-coordinator"

    linked_home.unlink()
    linked_home.symlink_to(replacement_home, target_is_directory=True)

    assert anchor_mod.run_coordinator_anchor_matches_current_home() is False


def test_unconfined_anchor_check_uses_default_home_for_rejected_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected override must retain the configured default-home fallback."""
    default_home = tmp_path / "default-home"
    anchor_home = tmp_path / "operator-home"
    anchor_home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", os.path.abspath(os.sep))
    monkeypatch.setattr(paths_mod, "_default_home", lambda: default_home)
    monkeypatch.setattr(paths_mod, "_config_dir_memo", None)
    monkeypatch.setattr(paths_mod, "_resolved_home", None)
    monkeypatch.setattr(anchor_mod, "_anchor_home", lambda: anchor_home)
    anchor_mod._clear_run_coordinator_anchor_cache()

    assert anchor_mod.canonical_run_coordinator_dir() == default_home / "run-coordinator"
    assert anchor_mod.run_coordinator_anchor_matches_current_home() is True


def test_existing_anchor_rejects_linked_home_replaced_by_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(real_home, target_is_directory=True)
    anchor_home = tmp_path / "operator-home"
    anchor_home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(linked_home))
    monkeypatch.setattr(paths_mod, "_config_dir_memo", None)
    monkeypatch.setattr(paths_mod, "_resolved_home", None)
    monkeypatch.setattr(anchor_mod, "_anchor_home", lambda: anchor_home)
    anchor_mod._clear_run_coordinator_anchor_cache()

    first = anchor_mod.canonical_run_coordinator_dir()
    assert first == real_home / "run-coordinator"

    linked_home.unlink()
    linked_home.mkdir()
    paths_mod._config_dir_memo = None
    anchor_mod._clear_run_coordinator_anchor_cache()

    with pytest.raises(OSError, match="does not match the configured data home"):
        anchor_mod.canonical_run_coordinator_dir()


def test_default_real_home_needs_no_external_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordinary default path stays inside Kiro's protected data home."""
    data_home = tmp_path / ".kiro" / "crew"
    data_home.mkdir(parents=True)
    anchor_home = tmp_path / "operator-home"
    anchor_home.mkdir()
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.setattr(anchor_mod, "data_home", lambda: data_home)
    monkeypatch.setattr(anchor_mod, "_anchor_home", lambda: anchor_home)
    anchor_mod._clear_run_coordinator_anchor_cache()

    assert anchor_mod.canonical_run_coordinator_dir() == data_home / "run-coordinator"
    assert not anchor_mod.run_coordinator_anchor_dir().exists()


def test_prepare_anchor_directory_creates_and_tightens_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = tmp_path / ".kirocrew.run-coordinator"
    calls: list[tuple[str, Path]] = []

    monkeypatch.setattr(anchor_mod, "run_coordinator_anchor_dir", lambda: anchor)

    def make(path: Path) -> None:
        calls.append(("make", path))
        path.mkdir()

    monkeypatch.setattr(anchor_mod, "make_owner_only_dir", make)
    monkeypatch.setattr(
        anchor_mod,
        "restrict_dir_to_owner",
        lambda path: calls.append(("restrict", path)),
    )

    assert anchor_mod.prepare_run_coordinator_anchor_dir() == anchor
    assert calls == [("make", anchor), ("restrict", anchor)]


async def _claimed_running(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> tuple[CommandClaim, RunRecord]:
    receipt = await coordinator.submit(_request())
    assert receipt.value is not None
    claims = await coordinator.claim_commands(
        OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value + 30),
        limit=1,
    )
    assert len(claims) == 1
    claim = claims[0]
    starting = await coordinator.mark_starting(
        claim.command,
        claim.fence,
        expected_version=claim.run.version,
    )
    assert starting.value is not None
    running = await coordinator.mark_running(
        claim.command.run_id,
        claim.fence,
        expected_version=starting.value.version,
    )
    assert running.value is not None
    return claim, running.value


@pytest.mark.asyncio
async def test_submit_is_idempotent_and_detects_payload_conflicts(
    coordinator: RunCoordinator,
) -> None:
    created = await coordinator.submit(_request())
    replay = await coordinator.submit(_request())
    conflict = await coordinator.submit(_request(payload_hash="different"))

    assert created.decision is CoordinatorDecision.APPLIED
    assert created.reason is CoordinatorReason.CREATED
    assert created.value is not None
    assert created.value.created is True
    assert created.value.run.run_id == "run-1"
    assert created.value.command.status is CommandStatus.PENDING
    assert created.value.command.payload_json == _request().payload_json

    assert replay.decision is CoordinatorDecision.UNCHANGED
    assert replay.reason is CoordinatorReason.IDEMPOTENT_REPLAY
    assert replay.value is not None
    assert replay.value.created is False
    assert replay.value.run == created.value.run
    assert replay.value.command == created.value.command

    assert conflict.decision is CoordinatorDecision.REJECTED
    assert conflict.reason is CoordinatorReason.IDEMPOTENCY_CONFLICT
    assert conflict.value is None
    assert await coordinator.get_run("run-1") == created.value.run


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "run-other"),
        ("command_id", "command-other"),
        ("operation", CommandOperation.CONTINUE),
    ],
)
async def test_submit_replay_requires_the_full_execution_identity(
    coordinator: RunCoordinator,
    field: str,
    value: object,
) -> None:
    await coordinator.submit(_request())

    replay = await coordinator.submit(replace(_request(), **{field: value}))

    assert replay.decision is CoordinatorDecision.REJECTED
    assert replay.reason is CoordinatorReason.IDEMPOTENCY_CONFLICT


@pytest.mark.asyncio
async def test_rejected_submission_is_queryable_but_never_claimed(
    coordinator: RunCoordinator,
) -> None:
    result = await coordinator.submit(_request(accepted=False))

    assert result.decision is CoordinatorDecision.REJECTED
    assert result.reason is CoordinatorReason.ADMISSION_REJECTED
    assert result.value is not None
    assert result.value.run.observed_state is ObservedState.TERMINAL
    assert result.value.run.outcome is RunOutcome.FAILED
    assert result.value.command.status is CommandStatus.REJECTED
    assert (
        await coordinator.claim_commands(
            OwnerLease(owner_id="gateway-1", lease_expires_at=130.0), limit=1
        )
        == []
    )


@pytest.mark.asyncio
async def test_submit_rejects_duplicate_run_or_command_identity(
    coordinator: RunCoordinator,
) -> None:
    created = await coordinator.submit(_request())
    duplicate_run = await coordinator.submit(
        _request(command_id="command-2", idempotency_key="key-2")
    )
    duplicate_command = await coordinator.submit(_request(run_id="run-2", idempotency_key="key-3"))

    assert duplicate_run.decision is CoordinatorDecision.REJECTED
    assert duplicate_run.reason is CoordinatorReason.IDENTITY_CONFLICT
    assert duplicate_command.decision is CoordinatorDecision.REJECTED
    assert duplicate_command.reason is CoordinatorReason.IDENTITY_CONFLICT
    assert created.value is not None
    assert await coordinator.get_run("run-1") == created.value.run
    assert await coordinator.get_run("run-2") is None


@pytest.mark.asyncio
async def test_submit_rejects_control_commands_until_target_semantics_exist(
    coordinator: RunCoordinator,
) -> None:
    result = await coordinator.submit(_request(operation=CommandOperation.STEER))

    assert result.decision is CoordinatorDecision.REJECTED
    assert result.reason is CoordinatorReason.UNSUPPORTED_OPERATION
    assert result.value is None
    assert await coordinator.get_run("run-1") is None


@pytest.mark.asyncio
async def test_control_submission_is_idempotent_and_queryable_without_mutating_run(
    coordinator: RunCoordinator,
) -> None:
    created_run = await coordinator.submit(_request())
    assert created_run.value is not None
    before = created_run.value.run
    request = SubmitControl(
        command_id="control-1",
        idempotency_key="control-key-1",
        run_id="run-1",
        operation=CommandOperation.STEER,
        payload_hash="control-hash-1",
        payload_json='{"message":"focus"}',
    )

    created = await coordinator.submit_control(request)
    replay = await coordinator.submit_control(request)
    conflict = await coordinator.submit_control(replace(request, payload_hash="different"))
    queried = await coordinator.get_command_by_key("control-key-1")

    assert created.decision is CoordinatorDecision.APPLIED
    assert created.value is not None
    assert created.value.created is True
    assert created.value.command.status is CommandStatus.PENDING
    assert replay.decision is CoordinatorDecision.UNCHANGED
    assert replay.value is not None
    assert replay.value.created is False
    assert replay.value.command == created.value.command
    assert conflict.reason is CoordinatorReason.IDEMPOTENCY_CONFLICT
    assert queried == replay.value
    assert await coordinator.get_run("run-1") == before


@pytest.mark.asyncio
async def test_rejected_control_is_sticky_never_claimed_and_does_not_terminal_run(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    created_run = await coordinator.submit(_request())
    assert created_run.value is not None
    before = created_run.value.run
    request = SubmitControl(
        command_id="control-1",
        idempotency_key="control-key-1",
        run_id="run-1",
        operation=CommandOperation.CANCEL,
        payload_hash="control-hash-1",
        accepted=False,
        rejection_reason="not authorized",
    )

    rejected = await coordinator.submit_control(request)
    replay = await coordinator.submit_control(request)

    assert rejected.reason is CoordinatorReason.ADMISSION_REJECTED
    assert rejected.value is not None
    assert rejected.value.command.status is CommandStatus.REJECTED
    assert replay.value is not None
    assert replay.value.command.status is CommandStatus.REJECTED
    assert await coordinator.get_run("run-1") == before
    assert (
        await coordinator.claim_controls(OwnerLease("controller", clock.value + 10), limit=1) == []
    )


@pytest.mark.asyncio
async def test_control_for_legacy_target_does_not_require_coordinator_run(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    submitted = await coordinator.submit_control(
        SubmitControl(
            command_id="control-legacy",
            idempotency_key="control-key-legacy",
            run_id="legacy-run",
            operation=CommandOperation.STEER,
            payload_hash="control-hash-legacy",
        )
    )
    claim = (await coordinator.claim_controls(OwnerLease("controller", clock.value + 10), limit=1))[
        0
    ]

    assert submitted.value is not None
    assert submitted.value.run is None
    assert claim.run is None
    assert claim.fence is None


@pytest.mark.asyncio
async def test_control_claim_epoch_is_independent_from_execution_lease(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    await coordinator.submit(_request())
    execution = (
        await coordinator.claim_commands(OwnerLease("executor", clock.value + 100), limit=1)
    )[0]
    await coordinator.submit_control(
        SubmitControl(
            command_id="control-1",
            idempotency_key="control-key-1",
            run_id="run-1",
            operation=CommandOperation.CANCEL,
            payload_hash="control-hash-1",
        )
    )
    run_before = await coordinator.get_run("run-1")

    first = (
        await coordinator.claim_controls(OwnerLease("controller-1", clock.value + 5), limit=1)
    )[0]
    assert first.fence is None
    assert first.command_fence == CommandFence("control-1", "controller-1", 1)
    assert await coordinator.get_run("run-1") == run_before

    clock.value += 6
    second = (
        await coordinator.claim_controls(OwnerLease("controller-2", clock.value + 5), limit=1)
    )[0]
    stale = await coordinator.finish_control(first.command_fence, CommandStatus.APPLIED)
    applied = await coordinator.finish_control(
        second.command_fence,
        CommandStatus.APPLIED,
        "",
        '{"cancelled":true}',
    )

    assert execution.fence is not None
    assert second.command_fence.claim_epoch == first.command_fence.claim_epoch + 1
    assert stale.reason is CoordinatorReason.STALE_FENCE
    assert applied.decision is CoordinatorDecision.APPLIED
    assert applied.value is not None
    assert applied.value.result_json == '{"cancelled":true}'
    assert await coordinator.get_run("run-1") == run_before


@pytest.mark.asyncio
async def test_control_claim_can_finish_after_deadline_until_it_is_superseded(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    await coordinator.submit_control(
        SubmitControl(
            command_id="slow-control",
            idempotency_key="slow-control-key",
            run_id="legacy-run",
            operation=CommandOperation.CANCEL,
            payload_hash="slow-control-hash",
        )
    )
    claim = (
        await coordinator.claim_controls(
            OwnerLease("controller", clock.value + 5),
            limit=1,
        )
    )[0]
    clock.value += 6

    finished = await coordinator.finish_control(
        claim.command_fence,
        CommandStatus.APPLIED,
        result_json="true",
    )

    assert finished.decision is CoordinatorDecision.APPLIED
    assert finished.value is not None
    assert finished.value.result_json == "true"


@pytest.mark.asyncio
async def test_control_claim_can_target_one_command_and_finish_rejection(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    await coordinator.submit(_request())
    for index, operation in enumerate((CommandOperation.STEER, CommandOperation.RELEASE), start=1):
        await coordinator.submit_control(
            SubmitControl(
                command_id=f"control-{index}",
                idempotency_key=f"control-key-{index}",
                run_id="run-1",
                operation=operation,
                payload_hash=f"control-hash-{index}",
            )
        )

    claims = await coordinator.claim_controls(
        OwnerLease("controller", clock.value + 10),
        limit=2,
        command_id="control-2",
    )
    assert [claim.command.command_id for claim in claims] == ["control-2"]
    rejected = await coordinator.finish_control(
        claims[0].command_fence,
        CommandStatus.REJECTED,
        "conversation busy",
        '{"ok":false}',
    )
    queried = await coordinator.get_command_by_key("control-key-2")

    assert rejected.decision is CoordinatorDecision.APPLIED
    assert rejected.value is not None
    assert rejected.value.status is CommandStatus.REJECTED
    assert rejected.value.rejection_reason == "conversation busy"
    assert queried is not None
    assert queried.command == rejected.value
    remaining = await coordinator.claim_controls(
        OwnerLease("controller", clock.value + 10), limit=2
    )
    assert [claim.command.command_id for claim in remaining] == ["control-1"]


@pytest.mark.asyncio
async def test_exact_execution_command_claim_and_finish_are_idempotent(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    await coordinator.submit(_request())
    before = await coordinator.get_run("run-1")

    claim = await coordinator.claim_command("command-1", OwnerLease("admission", clock.value + 10))
    duplicate_claim = await coordinator.claim_command(
        "command-1", OwnerLease("other", clock.value + 10)
    )
    assert claim is not None
    assert claim.fence is not None
    assert claim.run is not None
    assert claim.run.owner_id == "admission"
    assert claim.fence.lease_epoch == claim.run.lease_epoch
    assert duplicate_claim is None
    leased = await coordinator.get_run("run-1")
    assert leased is not None
    assert before is not None
    assert leased.version == before.version
    assert leased.lease_epoch == before.lease_epoch + 1

    applied = await coordinator.finish_command(
        claim.command_fence,
        CommandStatus.APPLIED,
        "",
        '{"id":"run-1","queued":false}',
    )
    replay = await coordinator.finish_command(
        claim.command_fence,
        CommandStatus.APPLIED,
        "",
        '{"id":"run-1","queued":false}',
    )
    conflicting_replay = await coordinator.finish_command(
        claim.command_fence,
        CommandStatus.REJECTED,
        "different outcome",
        '{"error":"different outcome"}',
    )
    queried = await coordinator.get_command_by_key("key-1")

    assert applied.decision is CoordinatorDecision.APPLIED
    assert replay.decision is CoordinatorDecision.UNCHANGED
    assert conflicting_replay.decision is CoordinatorDecision.REJECTED
    assert conflicting_replay.reason is CoordinatorReason.OUTCOME_CONFLICT
    assert queried is not None
    assert queried.command.status is CommandStatus.APPLIED
    assert queried.command.result_json == '{"id":"run-1","queued":false}'
    assert await coordinator.get_run("run-1") == leased


@pytest.mark.asyncio
async def test_execution_rejection_retains_fence_for_terminal_outbox_commit(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    await coordinator.submit(_request())
    claim = await coordinator.claim_command("command-1", OwnerLease("admission", clock.value + 10))
    assert claim is not None

    rejected = await coordinator.finish_command(
        claim.command_fence,
        CommandStatus.REJECTED,
        "approval denied",
        '{"error":"approval denied","counted":true}',
    )
    run = await coordinator.get_run("run-1")

    assert rejected.decision is CoordinatorDecision.APPLIED
    assert rejected.value is not None
    assert rejected.value.status is CommandStatus.REJECTED
    assert run is not None
    assert run.observed_state is ObservedState.ACCEPTED
    assert run.outcome is None
    assert run.error == ""
    assert await coordinator.claim_commands(OwnerLease("executor", clock.value + 10), limit=1) == []


@pytest.mark.asyncio
async def test_command_claim_returns_fence_and_advances_legal_transitions(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    await coordinator.submit(_request())
    claims = await coordinator.claim_commands(
        OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value + 30),
        limit=1,
    )

    assert len(claims) == 1
    claim = claims[0]
    assert claim.command.status is CommandStatus.CLAIMED
    assert claim.run.owner_id == "gateway-1"
    assert claim.run.lease_epoch == 1
    assert claim.fence.run_id == "run-1"
    assert claim.fence.owner_id == "gateway-1"
    assert claim.fence.lease_epoch == 1

    starting = await coordinator.mark_starting(
        claim.command, claim.fence, expected_version=claim.run.version
    )
    assert starting.decision is CoordinatorDecision.APPLIED
    assert starting.value is not None
    assert starting.value.observed_state is ObservedState.STARTING
    receipt = await coordinator.get_command_by_key("key-1")
    assert receipt is not None
    assert receipt.command.status is CommandStatus.CLAIMED

    running = await coordinator.mark_running(
        "run-1", claim.fence, expected_version=starting.value.version
    )
    assert running.decision is CoordinatorDecision.APPLIED
    assert running.value is not None
    assert running.value.observed_state is ObservedState.RUNNING


@pytest.mark.asyncio
async def test_process_identity_is_fenced_versioned_and_idempotent(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    claim, running = await _claimed_running(coordinator, clock)

    recorded = await coordinator.record_process(
        "run-1",
        claim.fence,
        running.version,
        4321,
        "start-1",
        True,
    )
    assert recorded.decision is CoordinatorDecision.APPLIED
    assert recorded.value is not None
    assert recorded.value.process_id == 4321
    assert recorded.value.process_start_id == "start-1"
    assert recorded.value.process_owned is True
    assert recorded.value.version == running.version + 1

    replay = await coordinator.record_process(
        "run-1",
        claim.fence,
        recorded.value.version,
        4321,
        "start-1",
        True,
    )
    stale = await coordinator.record_process(
        "run-1",
        type(claim.fence)(run_id="run-1", owner_id="other", lease_epoch=1),
        recorded.value.version,
        9999,
        "forged",
        True,
    )
    missing_identity = await coordinator.record_process(
        "run-1",
        claim.fence,
        recorded.value.version,
        9999,
        "",
        True,
    )

    assert replay.decision is CoordinatorDecision.UNCHANGED
    assert stale.reason is CoordinatorReason.STALE_FENCE
    assert missing_identity.reason is CoordinatorReason.INVALID_TRANSITION
    assert await coordinator.get_run("run-1") == recorded.value


@pytest.mark.asyncio
async def test_process_identity_resynchronizes_one_committed_write(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    claim, running = await _claimed_running(coordinator, clock)

    recorded = await coordinator.record_process(
        "run-1",
        claim.fence,
        running.version,
        4321,
        "start-1",
        True,
    )
    assert recorded.value is not None

    replay = await coordinator.record_process(
        "run-1",
        claim.fence,
        running.version,
        4321,
        "start-1",
        True,
    )
    replacement = await coordinator.record_process(
        "run-1",
        claim.fence,
        running.version,
        8765,
        "start-2",
        True,
    )
    too_stale = await coordinator.record_process(
        "run-1",
        claim.fence,
        running.version,
        9999,
        "start-3",
        True,
    )

    assert replay.decision is CoordinatorDecision.UNCHANGED
    assert replay.value == recorded.value
    assert replacement.decision is CoordinatorDecision.REJECTED
    assert replacement.reason is CoordinatorReason.VERSION_CONFLICT
    assert too_stale.decision is CoordinatorDecision.REJECTED
    assert too_stale.reason is CoordinatorReason.VERSION_CONFLICT
    assert await coordinator.get_run("run-1") == recorded.value


@pytest.mark.asyncio
async def test_version_and_execution_fences_reject_without_mutation(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    claim, running = await _claimed_running(coordinator, clock)
    before = await coordinator.get_run("run-1")

    version_conflict = await coordinator.mark_running(
        "run-1", claim.fence, expected_version=running.version - 2
    )
    stale = await coordinator.mark_running(
        "run-1",
        type(claim.fence)(run_id="run-1", owner_id="other", lease_epoch=1),
        expected_version=running.version,
    )

    assert version_conflict.decision is CoordinatorDecision.REJECTED
    assert version_conflict.reason is CoordinatorReason.VERSION_CONFLICT
    assert stale.decision is CoordinatorDecision.REJECTED
    assert stale.reason is CoordinatorReason.STALE_FENCE
    assert await coordinator.get_run("run-1") == before


@pytest.mark.asyncio
async def test_lifecycle_transition_replays_after_commit_response_loss(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    receipt = await coordinator.submit(_request())
    assert receipt.value is not None
    claims = await coordinator.claim_commands(
        OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value + 30),
        limit=1,
    )
    assert len(claims) == 1
    claim = claims[0]

    starting = await coordinator.mark_starting(
        claim.command,
        claim.fence,
        expected_version=claim.run.version,
    )
    replay_starting = await coordinator.mark_starting(
        claim.command,
        claim.fence,
        expected_version=claim.run.version,
    )
    assert starting.value is not None
    assert replay_starting.decision is CoordinatorDecision.UNCHANGED
    assert replay_starting.value == starting.value

    running = await coordinator.mark_running(
        claim.run.run_id,
        claim.fence,
        expected_version=starting.value.version,
    )
    replay_running = await coordinator.mark_running(
        claim.run.run_id,
        claim.fence,
        expected_version=starting.value.version,
    )
    assert running.value is not None
    assert replay_running.decision is CoordinatorDecision.UNCHANGED
    assert replay_running.value == running.value


@pytest.mark.asyncio
async def test_execution_fence_cannot_cross_run_boundaries(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    await coordinator.submit(
        _request(run_id="run-a", command_id="command-a", idempotency_key="key-a")
    )
    await coordinator.submit(
        _request(run_id="run-b", command_id="command-b", idempotency_key="key-b")
    )
    claims = await coordinator.claim_commands(
        OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value + 30),
        limit=2,
    )
    claim_a, claim_b = claims
    before = await coordinator.get_run("run-b")
    assert before is not None

    starting = await coordinator.mark_starting(
        claim_b.command, claim_a.fence, expected_version=before.version
    )
    running = await coordinator.mark_running(
        "run-b", claim_a.fence, expected_version=before.version
    )
    completed = await coordinator.complete(
        RunCompletion(
            run_id="run-b",
            outcome=RunOutcome.FAILED,
            result_path="",
            error="wrong fence",
            event_type="subagent_completion",
            destination="dashboard:parent",
            payload_json='{"summary":"wrong fence"}',
            terminal_at=clock.value,
        ),
        claim_a.fence,
        expected_version=before.version,
    )
    renewed = await coordinator.renew("run-b", claim_a.fence, clock.value + 60)

    assert starting.reason is CoordinatorReason.STALE_FENCE
    assert running.reason is CoordinatorReason.STALE_FENCE
    assert completed.reason is CoordinatorReason.STALE_FENCE
    assert renewed is False
    assert await coordinator.get_run("run-b") == before


@pytest.mark.asyncio
async def test_expired_command_claim_is_taken_over_and_fences_old_owner(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    await coordinator.submit(_request())
    first = (
        await coordinator.claim_commands(
            OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value + 5),
            limit=1,
        )
    )[0]

    clock.value += 6
    second = (
        await coordinator.claim_commands(
            OwnerLease(owner_id="gateway-2", lease_expires_at=clock.value + 5),
            limit=1,
        )
    )[0]

    stale = await coordinator.mark_starting(
        first.command, first.fence, expected_version=second.run.version
    )
    current = await coordinator.mark_starting(
        second.command, second.fence, expected_version=second.run.version
    )

    assert second.fence.lease_epoch == first.fence.lease_epoch + 1
    assert stale.reason is CoordinatorReason.STALE_FENCE
    assert current.decision is CoordinatorDecision.APPLIED


@pytest.mark.asyncio
async def test_completion_is_first_writer_wins_and_creates_one_event(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    claim, running = await _claimed_running(coordinator, clock)
    completion = RunCompletion(
        run_id="run-1",
        outcome=RunOutcome.COMPLETED,
        result_path="/tmp/result.txt",
        error="",
        event_type="subagent_completion",
        destination="dashboard:parent",
        payload_json='{"summary":"done"}',
        terminal_at=clock.value,
    )

    completed = await coordinator.complete(
        completion, claim.fence, expected_version=running.version
    )
    replay = await coordinator.complete(
        completion,
        claim.fence,
        expected_version=running.version,
    )
    conflict = await coordinator.complete(
        replace(completion, outcome=RunOutcome.FAILED, error="late"),
        claim.fence,
        expected_version=running.version,
    )

    assert completed.decision is CoordinatorDecision.APPLIED
    assert completed.value is not None
    assert completed.value.event_id == "event-1"
    assert completed.value.status is DeliveryState.PENDING
    assert replay.decision is CoordinatorDecision.UNCHANGED
    assert replay.value == completed.value
    assert conflict.decision is CoordinatorDecision.REJECTED
    assert conflict.reason is CoordinatorReason.OUTCOME_CONFLICT


@pytest.mark.asyncio
async def test_completion_replay_requires_the_original_expected_version(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    claim, running = await _claimed_running(coordinator, clock)
    completion = RunCompletion(
        run_id="run-1",
        outcome=RunOutcome.COMPLETED,
        result_path="/tmp/result.txt",
        error="",
        event_type="subagent_completion",
        destination="dashboard:parent",
        payload_json='{"summary":"done"}',
        terminal_at=clock.value,
    )
    completed = await coordinator.complete(completion, claim.fence, running.version)
    assert completed.value is not None

    stale = await coordinator.complete(completion, claim.fence, expected_version=-1)

    assert stale.decision is CoordinatorDecision.REJECTED
    assert stale.reason is CoordinatorReason.VERSION_CONFLICT


@pytest.mark.asyncio
async def test_completion_is_atomic_when_event_identity_generation_fails(
    clock: FakeClock,
) -> None:
    def fail_event_identity() -> str:
        raise RuntimeError("identity source unavailable")

    coordinator = MemoryRunCoordinator(clock=clock, id_factory=fail_event_identity)
    claim, running = await _claimed_running(coordinator, clock)
    completion = RunCompletion(
        run_id="run-1",
        outcome=RunOutcome.COMPLETED,
        result_path="/tmp/result.txt",
        error="",
        event_type="subagent_completion",
        destination="dashboard:parent",
        payload_json='{"summary":"done"}',
        terminal_at=clock.value,
    )

    with pytest.raises(RuntimeError, match="identity source unavailable"):
        await coordinator.complete(completion, claim.fence, running.version)

    assert await coordinator.get_run("run-1") == running


@pytest.mark.asyncio
async def test_outbox_claim_epoch_fences_release_and_delivery(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    claim, running = await _claimed_running(coordinator, clock)
    completed = await coordinator.complete(
        RunCompletion(
            run_id="run-1",
            outcome=RunOutcome.COMPLETED,
            result_path="/tmp/result.txt",
            error="",
            event_type="subagent_completion",
            destination="dashboard:parent",
            payload_json='{"summary":"done"}',
            terminal_at=clock.value,
        ),
        claim.fence,
        expected_version=running.version,
    )
    assert completed.value is not None

    first = (
        await coordinator.claim_outbox(
            OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value + 5),
            limit=1,
        )
    )[0]
    first_fence = DeliveryFence(
        event_id=first.event_id,
        owner_id=first.claim_owner,
        claim_epoch=first.claim_epoch,
    )
    clock.value += 6
    second = (
        await coordinator.claim_outbox(
            OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value + 5),
            limit=1,
        )
    )[0]
    second_fence = DeliveryFence(
        event_id=second.event_id,
        owner_id=second.claim_owner,
        claim_epoch=second.claim_epoch,
    )

    stale_release = await coordinator.release_outbox(first_fence, available_at=clock.value + 10)
    stale_delivery = await coordinator.mark_delivered(first_fence)
    delivered = await coordinator.mark_delivered(second_fence)
    delivered_again = await coordinator.mark_delivered(second_fence)

    assert second.claim_epoch == first.claim_epoch + 1
    assert stale_release.reason is CoordinatorReason.STALE_FENCE
    assert stale_delivery.reason is CoordinatorReason.STALE_FENCE
    assert delivered.decision is CoordinatorDecision.APPLIED
    assert delivered.value is not None
    assert delivered.value.status is DeliveryState.DELIVERED
    assert delivered_again.decision is CoordinatorDecision.UNCHANGED
    assert (
        await coordinator.claim_outbox(
            OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value + 5),
            limit=1,
        )
        == []
    )


@pytest.mark.asyncio
async def test_release_outbox_rejects_non_finite_retry_deadline(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    claim, running = await _claimed_running(coordinator, clock)
    completed = await coordinator.complete(
        RunCompletion(
            run_id="run-1",
            outcome=RunOutcome.COMPLETED,
            result_path="/tmp/result.txt",
            error="",
            event_type="subagent_completion",
            destination="dashboard:parent",
            payload_json='{"summary":"done"}',
            terminal_at=clock.value,
        ),
        claim.fence,
        running.version,
    )
    assert completed.value is not None
    event = (
        await coordinator.claim_outbox(
            OwnerLease(owner_id="delivery", lease_expires_at=clock.value + 30),
            limit=1,
        )
    )[0]
    fence = DeliveryFence(event.event_id, event.claim_owner, event.claim_epoch)

    rejected = await coordinator.release_outbox(fence, available_at=float("nan"))

    assert rejected.decision is CoordinatorDecision.REJECTED
    assert rejected.reason is CoordinatorReason.INVALID_TRANSITION
    delivered = await coordinator.mark_delivered(fence)
    assert delivered.decision is CoordinatorDecision.APPLIED


@pytest.mark.asyncio
async def test_outbox_claim_waits_for_terminal_process_cleanup(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    claim, running = await _claimed_running(coordinator, clock)
    recorded = await coordinator.record_process(
        running.run_id,
        claim.fence,
        running.version,
        process_id=4321,
        process_start_id="same",
        process_owned=True,
    )
    assert recorded.value is not None
    completed = await coordinator.complete(
        RunCompletion(
            run_id=running.run_id,
            outcome=RunOutcome.COMPLETED,
            result_path="/tmp/result.txt",
            error="",
            event_type="subagent_completion",
            destination="dashboard:parent",
            payload_json='{"summary":"done"}',
            terminal_at=clock.value,
        ),
        claim.fence,
        expected_version=recorded.value.version,
    )
    assert completed.value is not None

    blocked = await coordinator.claim_outbox(
        OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value + 5),
        limit=1,
    )

    assert blocked == []
    clock.value += 31
    recovery_claims = await coordinator.claim_recovery(
        OwnerLease(owner_id="recovery", lease_expires_at=clock.value + 30),
        limit=1,
    )
    assert len(recovery_claims) == 1
    recovery_claim = recovery_claims[0]
    cleared = await coordinator.clear_recovered_process(
        recovery_claim.run.run_id,
        recovery_claim.fence,
        recovery_claim.run.version,
    )
    assert cleared.value is not None

    released = await coordinator.claim_outbox(
        OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value + 5),
        limit=1,
    )

    assert len(released) == 1


@pytest.mark.asyncio
async def test_claim_limits_and_order_are_deterministic(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    for index in range(3):
        await coordinator.submit(
            _request(
                run_id=f"run-{index}",
                command_id=f"command-{index}",
                idempotency_key=f"key-{index}",
                payload_hash=f"hash-{index}",
            )
        )

    assert (
        await coordinator.claim_commands(
            OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value + 5), limit=0
        )
        == []
    )
    claims = await coordinator.claim_commands(
        OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value + 5), limit=2
    )
    assert [item.command.command_id for item in claims] == ["command-0", "command-1"]


@pytest.mark.asyncio
async def test_claim_commands_ignores_non_future_owner_lease(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    await coordinator.submit(_request())

    expired = await coordinator.claim_commands(
        OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value), limit=1
    )
    current = await coordinator.claim_commands(
        OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value + 5), limit=1
    )

    assert expired == []
    assert len(current) == 1
    assert current[0].command.attempt == 1
    assert current[0].fence.lease_epoch == 1


@pytest.mark.asyncio
async def test_claim_outbox_ignores_non_future_owner_lease(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    claim, running = await _claimed_running(coordinator, clock)
    await coordinator.complete(
        RunCompletion(
            run_id="run-1",
            outcome=RunOutcome.COMPLETED,
            result_path="/tmp/result.txt",
            error="",
            event_type="subagent_completion",
            destination="dashboard:parent",
            payload_json='{"summary":"done"}',
            terminal_at=clock.value,
        ),
        claim.fence,
        expected_version=running.version,
    )

    expired = await coordinator.claim_outbox(
        OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value), limit=1
    )
    current = await coordinator.claim_outbox(
        OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value + 5), limit=1
    )

    assert expired == []
    assert len(current) == 1
    assert current[0].attempts == 1
    assert current[0].claim_epoch == 1


@pytest.mark.asyncio
async def test_renew_requires_current_unexpired_fence(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    await coordinator.submit(_request())
    claim = (
        await coordinator.claim_commands(
            OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value + 5), limit=1
        )
    )[0]

    assert await coordinator.renew("run-1", claim.fence, until=clock.value + 20) is True
    clock.value += 21
    assert await coordinator.renew("run-1", claim.fence, until=clock.value + 20) is False


@pytest.mark.asyncio
async def test_renew_never_shortens_a_live_lease(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    await coordinator.submit(_request())
    claim = (
        await coordinator.claim_commands(
            OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value + 60), limit=1
        )
    )[0]

    assert await coordinator.renew("run-1", claim.fence, until=clock.value + 10) is True

    run = await coordinator.get_run("run-1")
    assert run is not None
    assert run.lease_expires_at == clock.value + 60


@pytest.mark.asyncio
async def test_terminal_run_lease_cannot_be_renewed(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    claim, running = await _claimed_running(coordinator, clock)
    completed = await coordinator.complete(
        RunCompletion(
            run_id="run-1",
            outcome=RunOutcome.COMPLETED,
            result_path="",
            error="",
            event_type="subagent_completion",
            destination="dashboard:parent",
            payload_json="{}",
            terminal_at=clock.value,
        ),
        claim.fence,
        running.version,
    )
    assert completed.value is not None

    assert await coordinator.renew("run-1", claim.fence, clock.value + 30) is False


@pytest.mark.asyncio
async def test_transition_uses_one_clock_sample_for_fence_and_timestamp() -> None:
    ticks: list[float] = []

    def stepping_clock() -> float:
        return ticks.pop(0) if ticks else 100.0

    coordinator = MemoryRunCoordinator(clock=stepping_clock)
    await coordinator.submit(_request())
    claim = (
        await coordinator.claim_commands(
            OwnerLease(owner_id="gateway-1", lease_expires_at=105.0), limit=1
        )
    )[0]
    ticks[:] = [104.0, 106.0]

    starting = await coordinator.mark_starting(
        claim.command,
        claim.fence,
        claim.run.version,
    )

    assert starting.decision is CoordinatorDecision.APPLIED
    assert starting.value is not None
    assert starting.value.updated_at == 104.0
    assert ticks == [106.0]


@pytest.mark.asyncio
async def test_claim_commands_skips_orphaned_command_without_partial_failure(
    clock: FakeClock,
) -> None:
    memory = MemoryRunCoordinator(clock=clock)
    await memory.submit(_request())
    memory._runs.pop("run-1")

    claims = await memory.claim_commands(OwnerLease("gateway-1", clock.value + 10), limit=1)

    assert claims == []


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [False, True])
async def test_execution_claim_does_not_steal_a_live_or_terminal_run(
    clock: FakeClock,
    terminal: bool,
) -> None:
    memory = MemoryRunCoordinator(clock=clock)
    await memory.submit(_request())
    first = (await memory.claim_commands(OwnerLease("gateway-1", clock.value + 30), limit=1))[0]
    if terminal:
        starting = await memory.mark_starting(first.command, first.fence, first.run.version)
        assert starting.value is not None
        running = await memory.mark_running("run-1", first.fence, starting.value.version)
        assert running.value is not None
        completed = await memory.complete(
            RunCompletion(
                run_id="run-1",
                outcome=RunOutcome.COMPLETED,
                result_path="",
                error="",
                event_type="subagent_completion",
                destination="dashboard:parent",
                payload_json="{}",
                terminal_at=clock.value,
            ),
            first.fence,
            running.value.version,
        )
        assert completed.value is not None
    memory._commands["command-1"] = replace(
        memory._commands["command-1"], status=CommandStatus.PENDING
    )
    before = await memory.get_run("run-1")

    reclaimed = await memory.claim_command("command-1", OwnerLease("gateway-2", clock.value + 30))

    assert reclaimed is None
    assert await memory.get_run("run-1") == before


@pytest.mark.asyncio
async def test_expired_execution_fence_can_complete_before_takeover(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    await coordinator.submit(_request())
    claim = (
        await coordinator.claim_commands(
            OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value + 5), limit=1
        )
    )[0]
    assert claim.fence is not None
    assert claim.run is not None
    starting = await coordinator.mark_starting(
        claim.command,
        claim.fence,
        claim.run.version,
    )
    assert starting.value is not None
    running = await coordinator.mark_running(
        claim.run.run_id,
        claim.fence,
        starting.value.version,
    )
    assert running.value is not None
    clock.value += 6

    completed = await coordinator.complete(
        RunCompletion(
            run_id=claim.run.run_id,
            outcome=RunOutcome.COMPLETED,
            result_path="/result.txt",
            error="",
            event_type="subagent_completion",
            destination="dashboard:parent",
            payload_json="{}",
            terminal_at=clock.value,
        ),
        claim.fence,
        running.value.version,
    )

    assert completed.decision is CoordinatorDecision.APPLIED
    assert completed.value is not None


@pytest.mark.asyncio
async def test_terminal_record_atomically_creates_replayable_outbox_without_command(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    request = TerminalRun(
        run_id="synthetic-terminal",
        parent_session="dashboard:parent",
        agent="kirocrew",
        task="record a rejected batch member",
        conversation_key="",
        outcome=RunOutcome.FAILED,
        result_path="",
        error="spawn submission lost",
        created_at=clock.value,
        terminal_at=clock.value,
        event_type="subagent_completion",
        destination="dashboard:parent",
        payload_json='{"id":"synthetic-terminal"}',
    )

    created = await coordinator.record_terminal(request)
    replay = await coordinator.record_terminal(request)

    assert created.decision is CoordinatorDecision.APPLIED
    assert created.value is not None
    assert created.value.run.observed_state is ObservedState.TERMINAL
    assert created.value.run.outcome is RunOutcome.FAILED
    assert created.value.event.status is DeliveryState.PENDING
    assert replay.decision is CoordinatorDecision.UNCHANGED
    assert replay.value is not None
    assert replay.value.event.event_id == created.value.event.event_id
    assert (
        await coordinator.claim_commands(
            OwnerLease("executor", clock.value + 10),
            limit=1,
        )
        == []
    )
    claimed = await coordinator.claim_outbox(
        OwnerLease("delivery", clock.value + 10),
        limit=1,
        event_id=created.value.event.event_id,
    )
    assert [event.event_id for event in claimed] == [created.value.event.event_id]


@pytest.mark.asyncio
async def test_terminal_record_closes_rejected_execution_and_creates_outbox(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    submitted = await coordinator.submit(_request(run_id="rejected-before-registration"))
    assert submitted.value is not None
    claim = (
        await coordinator.claim_commands(
            OwnerLease("executor", clock.value + 10),
            limit=1,
        )
    )[0]
    rejected = await coordinator.finish_command(
        claim.command_fence,
        CommandStatus.REJECTED,
        rejection_reason="PlatformCompositionError",
        result_json='{"error":"platform policy unavailable"}',
    )
    assert rejected.decision is CoordinatorDecision.APPLIED

    request = TerminalRun(
        run_id="rejected-before-registration",
        parent_session="",
        agent="",
        task="compare the candidates",
        conversation_key="",
        outcome=RunOutcome.FAILED,
        result_path="",
        error="platform policy unavailable",
        created_at=clock.value + 1,
        terminal_at=clock.value + 2,
        event_type="subagent_completion",
        destination="",
        payload_json='{"id":"rejected-before-registration"}',
    )
    recorded = await coordinator.record_terminal(request)
    replay = await coordinator.record_terminal(request)

    assert recorded.decision is CoordinatorDecision.APPLIED
    assert recorded.value is not None
    assert recorded.value.run.observed_state is ObservedState.TERMINAL
    assert recorded.value.run.outcome is RunOutcome.FAILED
    assert recorded.value.run.parent_session == "dashboard:parent"
    assert recorded.value.run.agent == "researcher"
    assert recorded.value.run.created_at == submitted.value.run.created_at
    assert recorded.value.event.destination == "dashboard:parent"
    assert recorded.value.event.status is DeliveryState.PENDING
    assert replay.decision is CoordinatorDecision.UNCHANGED
    assert replay.value is not None
    assert replay.value.event.event_id == recorded.value.event.event_id


@pytest.mark.asyncio
async def test_terminal_record_cannot_close_pending_execution(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    await coordinator.submit(_request(run_id="pending-execution"))

    recorded = await coordinator.record_terminal(
        TerminalRun(
            run_id="pending-execution",
            parent_session="dashboard:parent",
            agent="researcher",
            task="compare the candidates",
            conversation_key="",
            outcome=RunOutcome.FAILED,
            result_path="",
            error="not actually settled",
            created_at=clock.value,
            terminal_at=clock.value,
            event_type="subagent_completion",
            destination="dashboard:parent",
            payload_json='{"id":"pending-execution"}',
        )
    )

    assert recorded.decision is CoordinatorDecision.REJECTED
    assert recorded.reason is CoordinatorReason.OUTCOME_CONFLICT


@pytest.mark.asyncio
async def test_terminal_record_rejects_conflicting_replay(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    request = TerminalRun(
        run_id="synthetic-terminal-conflict",
        parent_session="dashboard:parent",
        agent="kirocrew",
        task="record a rejected batch member",
        conversation_key="",
        outcome=RunOutcome.FAILED,
        result_path="",
        error="spawn submission lost",
        created_at=clock.value,
        terminal_at=clock.value,
        event_type="subagent_completion",
        destination="dashboard:parent",
        payload_json='{"id":"synthetic-terminal-conflict"}',
    )
    created = await coordinator.record_terminal(request)

    conflict = await coordinator.record_terminal(
        replace(request, payload_json='{"id":"different"}')
    )

    assert created.decision is CoordinatorDecision.APPLIED
    assert conflict.decision is CoordinatorDecision.REJECTED
    assert conflict.reason is CoordinatorReason.OUTCOME_CONFLICT


@pytest.mark.asyncio
async def test_legacy_import_and_recovery_claim_are_idempotent_and_fenced(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    request = LegacyRunImport(
        run_id="legacy-1",
        parent_session="dashboard:parent",
        agent="kirocrew",
        task="old work",
        conversation_key="",
        observed_state=ObservedState.RUNNING,
        outcome=None,
        result_path="/tmp/result.txt",
        error="",
        created_at=10.0,
        updated_at=20.0,
        terminal_at=None,
        source_version="legacy-state-v1",
    )

    created = await coordinator.import_legacy(request)
    replay = await coordinator.import_legacy(request)
    claims = await coordinator.claim_recovery(
        OwnerLease("recovery-1", clock.value + 10),
        1,
    )

    assert created.decision is CoordinatorDecision.APPLIED
    assert replay.decision is CoordinatorDecision.UNCHANGED
    assert len(claims) == 1
    assert claims[0].run.source_version == "legacy-state-v1"
    assert claims[0].fence.lease_epoch == 1
    assert (
        await coordinator.claim_recovery(
            OwnerLease("recovery-2", clock.value + 20),
            1,
        )
        == []
    )


@pytest.mark.asyncio
async def test_recovery_claims_terminal_run_with_owned_process_until_cleanup(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    claim, running = await _claimed_running(coordinator, clock)
    recorded = await coordinator.record_process(
        "run-1",
        claim.fence,
        running.version,
        4321,
        "start-1",
        True,
    )
    assert recorded.value is not None
    completed = await coordinator.complete(
        RunCompletion(
            run_id="run-1",
            outcome=RunOutcome.COMPLETED,
            result_path="/tmp/result.txt",
            error="",
            event_type="subagent_completion",
            destination="dashboard:parent",
            payload_json='{"id":"run-1"}',
            terminal_at=clock.value,
        ),
        claim.fence,
        recorded.value.version,
    )
    assert completed.value is not None
    terminal_version = completed.value.run_version
    clock.value += 31

    claims = await coordinator.claim_recovery(
        OwnerLease("recovery", clock.value + 10),
        1,
    )

    assert len(claims) == 1
    assert claims[0].run.observed_state is ObservedState.TERMINAL
    assert claims[0].run.outcome is RunOutcome.COMPLETED
    assert claims[0].run.version == terminal_version
    assert claims[0].run.process_owned is True

    cleared = await coordinator.clear_recovered_process(
        "run-1",
        claims[0].fence,
        claims[0].run.version,
    )
    replay = await coordinator.clear_recovered_process(
        "run-1",
        claims[0].fence,
        claims[0].run.version,
    )

    assert cleared.decision is CoordinatorDecision.APPLIED
    assert cleared.value is not None
    assert cleared.value.observed_state is ObservedState.TERMINAL
    assert cleared.value.outcome is RunOutcome.COMPLETED
    assert cleared.value.process_id == 0
    assert cleared.value.process_start_id == ""
    assert cleared.value.process_owned is False
    assert replay.decision is CoordinatorDecision.UNCHANGED


@pytest.mark.asyncio
async def test_recovery_does_not_claim_a_pending_execution_submission(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    submitted = await coordinator.submit(_request())
    assert submitted.value is not None

    claims = await coordinator.claim_recovery(
        OwnerLease("recovery-1", clock.value + 10),
        1,
    )

    assert claims == []
    command = await coordinator.claim_command(
        "command-1",
        OwnerLease("gateway-1", clock.value + 10),
    )
    assert command is not None


@pytest.mark.asyncio
async def test_recovery_defers_a_fresh_pending_legacy_shadow_submission(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    submitted = await coordinator.submit(
        replace(_request(), source_version=LEGACY_SHADOW_SOURCE_VERSION)
    )
    assert submitted.value is not None

    fresh_claims = await coordinator.claim_recovery(
        OwnerLease("recovery-1", clock.value + 10),
        1,
    )
    clock.value += 61.0
    stale_claims = await coordinator.claim_recovery(
        OwnerLease("recovery-2", clock.value + 10),
        1,
    )

    assert fresh_claims == []
    assert [claim.run.run_id for claim in stale_claims] == ["run-1"]


@pytest.mark.asyncio
async def test_legacy_terminal_import_preserves_delivery_state(
    coordinator: RunCoordinator,
) -> None:
    imported = await coordinator.import_legacy(
        LegacyRunImport(
            run_id="legacy-terminal",
            parent_session="dashboard:parent",
            agent="kirocrew",
            task="old work",
            conversation_key="",
            observed_state=ObservedState.TERMINAL,
            outcome=RunOutcome.INTERRUPTED,
            result_path="/tmp/result.txt",
            error="gateway restart",
            created_at=10.0,
            updated_at=20.0,
            terminal_at=20.0,
            source_version="legacy-state-v1",
            event_type="subagent_completion",
            destination="dashboard:parent",
            payload_json='{"id":"legacy-terminal"}',
            delivery_state=DeliveryState.PENDING,
        )
    )

    assert imported.value is not None
    assert imported.value.event is not None
    assert imported.value.event.status is DeliveryState.PENDING


@pytest.mark.asyncio
async def test_legacy_import_retains_result_evidence_without_terminalizing_existing_run(
    coordinator: RunCoordinator,
) -> None:
    submitted = await coordinator.submit(_request(run_id="shadow-run"))
    assert submitted.value is not None

    imported = await coordinator.import_legacy(
        LegacyRunImport(
            run_id="shadow-run",
            parent_session="dashboard:parent",
            agent="researcher",
            task="compare the candidates",
            conversation_key="",
            observed_state=ObservedState.TERMINAL,
            outcome=RunOutcome.INTERRUPTED,
            result_path="/tmp/shadow-run/result.txt",
            error="agent-written tombstone",
            created_at=10.0,
            updated_at=20.0,
            terminal_at=20.0,
            source_version="legacy-state-v1",
            event_type="subagent_completion",
            destination="dashboard:parent",
            payload_json='{"id":"shadow-run"}',
            delivery_state=DeliveryState.PENDING,
        )
    )

    assert imported.decision is CoordinatorDecision.APPLIED
    assert imported.value is not None
    assert imported.value.run.observed_state is ObservedState.ACCEPTED
    assert imported.value.run.outcome is None
    assert imported.value.run.error == ""
    assert imported.value.run.result_path == "/tmp/shadow-run/result.txt"
    assert imported.value.event is None
