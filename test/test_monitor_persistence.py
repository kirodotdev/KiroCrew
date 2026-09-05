"""Persistence compatibility for structured AutoNudge monitor state."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.autonudge import AutoNudgeService, NudgeLoop
from kiro_crew.monitoring.models import (
    DEFAULT_MONITOR_CADENCE_SECS,
    MonitorBudgets,
    MonitorDecision,
    MonitorObservationStatus,
    MonitorOutcome,
    MonitorState,
    ProviderErrorKind,
    monitor_frontend_contract,
    monitor_state_from_dict,
    monitor_state_public_dict,
    monitor_state_to_dict,
)


def test_frontend_monitor_contract_matches_backend_vocabulary() -> None:
    path = Path(__file__).parents[1] / "website/src/monitoring/contract.json"

    assert json.loads(path.read_text(encoding="utf-8")) == monitor_frontend_contract()


def test_frontend_monitor_contract_has_one_version_authority() -> None:
    assert "schemaVersion" not in monitor_frontend_contract()


def test_legacy_loop_round_trip_does_not_acquire_monitor_state(tmp_path) -> None:
    """Reading and rewriting a legacy registry must not migrate its record."""
    store = {
        "version": 1,
        "loops": [
            {
                "id": "legacy01",
                "slot_key": "chat-1-123",
                "message": "keep going",
                "idle_secs": 300,
            }
        ],
    }
    (tmp_path / "autonudge.json").write_text(json.dumps(store), encoding="utf-8")
    service = AutoNudgeService(base_dir=tmp_path)

    service._load()

    restored = service._loops["legacy01"]
    assert restored.monitor is None
    serialized = service._serialize_state()["loops"][0]
    assert "monitor" not in serialized


def test_pre_gate_prompt_loop_drops_inert_probe_state(tmp_path) -> None:
    """A pre-gate prompt record must not become a structured controller."""
    monitor = MonitorState(
        kind="github_pull_request",
        target="owner/repo#123",
        objective="review_ready",
        created_ts=1_000.0,
    )
    store = {
        "version": 1,
        "loops": [
            {
                "id": "prompt01",
                "slot_key": "chat-1-123",
                "message": "keep checking",
                "idle_secs": 300,
                "active": True,
                "monitor": monitor_state_to_dict(monitor),
            }
        ],
    }
    (tmp_path / "autonudge.json").write_text(json.dumps(store), encoding="utf-8")
    service = AutoNudgeService(base_dir=tmp_path)

    service._load()

    restored = service._loops["prompt01"]
    assert restored.monitor is None
    assert restored.active
    assert "monitor" not in service._serialize_state()["loops"][0]


def test_explicit_null_monitor_is_malformed_and_cannot_rearm(tmp_path) -> None:
    """Only an absent monitor field denotes a legacy loop."""
    store = {
        "version": 1,
        "loops": [
            {
                "id": "null01",
                "slot_key": "chat-1-123",
                "message": "unsafe instructions",
                "idle_secs": 300,
                "active": True,
                "monitor": None,
            }
        ],
    }
    (tmp_path / "autonudge.json").write_text(json.dumps(store), encoding="utf-8")
    service = AutoNudgeService(base_dir=tmp_path)

    service._load()

    assert "null01" not in service._loops


def test_structured_monitor_cadence_defaults_without_changing_legacy_loop() -> None:
    """Structured cadence lives in typed monitor state, not the legacy timer default."""
    monitor = MonitorState(
        kind="github_pull_request",
        target="owner/repo#123",
        objective="review_ready",
        created_ts=1_000.0,
    )

    assert monitor.cadence_secs == DEFAULT_MONITOR_CADENCE_SECS == 300
    assert monitor_state_from_dict(monitor_state_to_dict(monitor)).cadence_secs == 300
    assert NudgeLoop(id="legacy02", slot_key="chat-1-123", message="keep going").idle_secs == 60


def test_structured_monitor_cadence_must_be_a_positive_integer() -> None:
    """Structured monitors cannot inherit legacy unlimited or malformed cadence values."""
    for cadence_secs in (0, -1, True, "300"):
        try:
            MonitorState(
                kind="github_pull_request",
                target="owner/repo#123",
                objective="review_ready",
                created_ts=1_000.0,
                cadence_secs=cadence_secs,
            )
        except ValueError:
            continue
        raise AssertionError(f"cadence_secs={cadence_secs!r} was accepted")


def test_wake_count_defaults_to_zero_and_rejects_negative_values() -> None:
    """Older records load as unused while malformed negative accounting fails closed."""
    payload = {
        "kind": "github_pull_request",
        "target": "owner/repo#123",
        "objective": "review_ready",
        "created_ts": 1_000.0,
    }

    assert monitor_state_from_dict(payload).wake_count == 0
    with pytest.raises(ValueError, match="wake_count"):
        MonitorState(**payload, wake_count=-1)


@pytest.mark.parametrize(
    ("payload", "field_name"),
    [
        ({"last_observation": {"ratio": float("inf")}}, "last_observation"),
        ({"future_field": {"ratio": float("nan")}}, "extra_fields"),
        ({"version": 2, "future_field": {"ratio": float("-inf")}}, "_raw_payload"),
    ],
)
def test_monitor_nested_state_rejects_non_strict_json(
    payload: dict[str, object], field_name: str
) -> None:
    """Nested non-finite values cannot poison the registry or public JSON."""
    base = {
        "kind": "github_pull_request",
        "target": "owner/repo#123",
        "objective": "review_ready",
        "created_ts": 1_000.0,
    }

    with pytest.raises(ValueError, match=field_name):
        monitor_state_from_dict({**base, **payload})


def test_latest_classification_defaults_for_older_records_and_rejects_unknown_status() -> None:
    """Older state stays readable, while an unknown classification cannot become public truth."""
    payload = {
        "kind": "github_pull_request",
        "target": "owner/repo#123",
        "objective": "review_ready",
        "created_ts": 1_000.0,
    }

    restored = monitor_state_from_dict(payload)

    assert restored.last_observation_status is None
    assert restored.last_observation_reason_code == ""
    with pytest.raises(ValueError, match="MonitorObservationStatus"):
        monitor_state_from_dict({**payload, "last_observation_status": "unexpected"})


def test_monitor_state_survives_store_round_trip(tmp_path) -> None:
    """Restart recovery retains the fingerprint, usage, budgets, and outcome."""
    monitor = MonitorState(
        kind="github_pull_request",
        target="owner/repo#123",
        objective="review_ready",
        created_ts=1_000.0,
        budgets=MonitorBudgets(
            max_runtime_secs=7_200,
            max_agent_turns=4,
            max_tokens=80_000,
            max_provider_errors=2,
        ),
        cadence_secs=120,
        last_observation={"head_revision": "abc123", "checks": "failing"},
        last_observation_status=MonitorObservationStatus.ACTIONABLE,
        last_observation_reason_code="checks_failed",
        last_fingerprint="failure-a",
        last_observed_at=1_200.0,
        last_wake_fingerprint="failure-a",
        wake_count=3,
        agent_turns=2,
        input_tokens=12_000,
        output_tokens=3_000,
        consecutive_provider_errors=1,
        probe_count=4,
        provider_error_count=2,
        last_probe_at=1_240.0,
        last_decision=MonitorDecision.RETRY_PROVIDER,
        last_provider_error=ProviderErrorKind.RATE_LIMITED,
        next_probe_at=1_500.0,
        outcome=MonitorOutcome.BUDGET,
        stopped_reason="token_budget",
        stopped_at=1_250.0,
    )
    service = AutoNudgeService(base_dir=tmp_path)
    service._loops["monitor1"] = NudgeLoop(
        id="monitor1",
        slot_key="chat-1-123",
        message="inspect the changed pull request",
        monitor=monitor,
    )
    service._save()

    restored_service = AutoNudgeService(base_dir=tmp_path)
    restored_service._load()
    restored_loop = restored_service._loops["monitor1"]
    restored = restored_loop.monitor

    assert restored is not None
    assert not restored_loop.active
    assert restored.kind == "github_pull_request"
    assert restored.last_observation == {"head_revision": "abc123", "checks": "failing"}
    assert restored.last_observation_status is MonitorObservationStatus.ACTIONABLE
    assert restored.last_observation_reason_code == "checks_failed"
    assert restored.last_fingerprint == "failure-a"
    assert restored.last_wake_fingerprint == "failure-a"
    assert restored.wake_count == 3
    assert restored.budgets == MonitorBudgets(
        max_runtime_secs=7_200,
        max_agent_turns=4,
        max_tokens=80_000,
        max_provider_errors=2,
    )
    assert restored.cadence_secs == 120
    assert restored.agent_turns == 2
    assert restored.total_tokens == 15_000
    assert restored.probe_count == 4
    assert restored.provider_error_count == 2
    assert restored.last_probe_at == 1_240.0
    assert restored.last_decision is MonitorDecision.RETRY_PROVIDER
    assert restored.last_provider_error is ProviderErrorKind.RATE_LIMITED
    assert restored.outcome is MonitorOutcome.BUDGET
    assert restored.stopped_reason == "token_budget"


@pytest.mark.asyncio
async def test_failed_legacy_loop_update_restores_live_state(tmp_path, monkeypatch) -> None:
    """A failed registry write must leave the live legacy record unchanged."""
    loop = NudgeLoop(
        id="monitor-update-failure",
        slot_key="chat-1-123",
        message="inspect the pull request",
        next_due_ts=1_500.0,
    )
    service = AutoNudgeService(base_dir=tmp_path)
    service._loops[loop.id] = loop

    def fail_write(_payload) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(service, "_write_state", fail_write)

    with pytest.raises(OSError, match="disk full"):
        await service.update(loop.id, active=False)

    assert loop.active
    assert loop.next_due_ts == 1_500.0


def test_public_projection_exposes_only_safe_latest_classification_fields() -> None:
    """Inspection needs typed status without exposing persistence-only provider payloads."""
    canonical_checks: dict[str, object] = {
        "failed": [],
        "passed": ["CI / test", "lint"],
        "pending": [],
        "unknown": [],
    }
    canonical: dict[str, object] = {
        "blocking_review": "none",
        "checks": canonical_checks,
        "checks_complete": True,
        "draft": False,
        "head_revision": "0123456789abcdef0123456789abcdef01234567",
        "kind": "github_pull_request",
        "mergeability": "mergeable",
        "review_decision": "approved",
        "review_threads_complete": True,
        "state": "open",
        "target": "github.com/owner/repo#123",
        "unresolved_review_threads": 0,
    }
    state = MonitorState(
        kind="github_pull_request",
        target="owner/repo#123",
        objective="review_ready",
        created_ts=1_000.0,
        last_observation={
            **canonical,
            "checks": {
                **canonical_checks,
                "provider_diagnostics": ["must-not-escape"],
            },
            "raw_provider_payload": {"secret": "must-not-escape"},
        },
        last_observation_status=MonitorObservationStatus.PENDING,
        last_observation_reason_code="checks_pending",
        extra_fields={
            "last_observation_summary": "Deprecated summary must not escape.",
            "raw_provider_payload": {"secret": "must-not-escape"},
        },
    )

    public = monitor_state_public_dict(state)

    assert public["last_observation_status"] is MonitorObservationStatus.PENDING
    assert public["last_observation_reason_code"] == "checks_pending"
    assert "last_observation_summary" not in public
    assert public["last_observation"] == canonical
    assert "raw_provider_payload" not in public
    assert "must-not-escape" not in repr(public)
    public_observation = public["last_observation"]
    assert isinstance(public_observation, dict)
    public_checks = public_observation["checks"]
    assert isinstance(public_checks, dict)
    passed = public_checks["passed"]
    assert isinstance(passed, list)
    passed.append("mutated public copy")
    assert "mutated public copy" not in repr(state.last_observation)


def test_monitor_public_projection_includes_lifecycle_timestamps_and_wake_reason() -> None:
    state = MonitorState(
        kind="github_pull_request",
        target="owner/repo#123",
        objective="review_ready",
        created_ts=1_000.0,
        last_wake_reason_code="checks_failed",
        user_stop_reason="Superseded by a newer review.",
    )

    public = monitor_state_public_dict(state)

    assert public["created_ts"] == 1_000.0
    assert public["last_wake_reason_code"] == "checks_failed"
    assert public["user_stop_reason"] == "Superseded by a newer review."


@pytest.mark.parametrize(
    "kind",
    [
        "github_pull_request",
        "gitlab_merge_request",
        "azure_devops_pull_request",
        "bitbucket_pull_request",
    ],
)
def test_public_projection_accepts_the_exact_shared_schema_for_every_provider(
    kind: str,
) -> None:
    """A supported provider keeps safe facts without widening the public payload."""
    observation = {
        "blocking_review": "none",
        "checks": {"failed": [], "passed": ["CI"], "pending": [], "unknown": []},
        "checks_complete": True,
        "draft": False,
        "head_revision": "abc123",
        "kind": kind,
        "mergeability": "mergeable",
        "review_decision": "approved",
        "review_threads_complete": True,
        "state": "open",
        "target": "provider.example/repository#17",
        "unresolved_review_threads": 0,
    }
    state = MonitorState(
        kind=kind,
        target="https://provider.example/repository/17",
        objective="review_ready",
        created_ts=1_000.0,
        last_observation={**observation, "raw_provider_payload": {"secret": "no"}},
    )

    public = monitor_state_public_dict(state)

    assert public["last_observation"] == observation
    assert "raw_provider_payload" not in repr(public)


def test_public_projection_drops_an_observation_for_a_different_provider() -> None:
    state = MonitorState(
        kind="github_pull_request",
        target="https://github.com/acme/widgets/pull/17",
        objective="review_ready",
        created_ts=1_000.0,
        last_observation={
            "blocking_review": "none",
            "checks": {"failed": [], "passed": [], "pending": [], "unknown": []},
            "draft": False,
            "head_revision": "abc123",
            "kind": "gitlab_merge_request",
            "mergeability": "mergeable",
            "review_decision": "approved",
            "review_threads_complete": True,
            "state": "open",
            "target": "gitlab.com/acme/widgets!17",
            "unresolved_review_threads": 0,
        },
    )

    assert monitor_state_public_dict(state)["last_observation"] == {}


def test_public_projection_rejects_unbounded_persisted_check_names() -> None:
    state = MonitorState(
        kind="github_pull_request",
        target="https://github.com/acme/widgets/pull/17",
        objective="review_ready",
        created_ts=1_000.0,
        last_observation={
            "blocking_review": "none",
            "checks": {
                "failed": [],
                "passed": ["x" * 201],
                "pending": [],
                "unknown": [],
            },
            "draft": False,
            "head_revision": "abc123",
            "kind": "github_pull_request",
            "mergeability": "mergeable",
            "review_decision": "approved",
            "review_threads_complete": True,
            "state": "open",
            "target": "github.com/acme/widgets#17",
            "unresolved_review_threads": 0,
        },
    )

    assert monitor_state_public_dict(state)["last_observation"] == {}


@pytest.mark.parametrize(
    "field_name",
    ["blocking_review", "mergeability", "review_decision", "state"],
)
def test_public_projection_rejects_unhashable_persisted_enum_fields(
    field_name: str,
) -> None:
    observation: dict[str, object] = {
        "blocking_review": "none",
        "checks": {"failed": [], "passed": [], "pending": [], "unknown": []},
        "draft": False,
        "head_revision": "abc123",
        "kind": "github_pull_request",
        "mergeability": "mergeable",
        "review_decision": "approved",
        "review_threads_complete": True,
        "state": "open",
        "target": "github.com/acme/widgets#17",
        "unresolved_review_threads": 0,
    }
    observation[field_name] = ["malformed"]
    state = MonitorState(
        kind="github_pull_request",
        target="https://github.com/acme/widgets/pull/17",
        objective="review_ready",
        created_ts=1_000.0,
        last_observation=observation,
    )

    assert monitor_state_public_dict(state)["last_observation"] == {}


def test_unknown_monitor_version_is_inspectable_inert_and_persisted_inactive(
    tmp_path,
) -> None:
    """A downgrade preserves opaque state without presenting unsupported work as active."""
    future_monitor = {
        "version": 99,
        "kind": "github_pull_request",
        "target": "owner/repo#123",
        "objective": "review_ready",
        "created_ts": 1_000.0,
        "cadence_secs": 123,
        "last_fingerprint": "future-fingerprint",
        "budgets": {
            "max_runtime_secs": 7_777,
            "max_agent_turns": 9,
            "max_tokens": 88_888,
            "max_provider_errors": 4,
            "future_budget": "keep",
        },
        "outcome": "future_paused",
        "future_policy": {"wake_every_time": True},
    }
    store = {
        "version": 1,
        "loops": [
            {
                "id": "future01",
                "slot_key": "chat-1-123",
                "message": "future instructions",
                "idle_secs": 300,
                "active": True,
                "monitor": future_monitor,
            }
        ],
    }
    (tmp_path / "autonudge.json").write_text(json.dumps(store), encoding="utf-8")
    service = AutoNudgeService(base_dir=tmp_path)

    service._load()

    restored = service._loops["future01"]
    assert not restored.active
    assert restored.monitor is not None
    assert restored.monitor.version == 99
    assert restored.monitor.target == "owner/repo#123"
    assert restored.monitor.outcome is MonitorOutcome.BLOCKED
    assert restored.monitor.stopped_reason == "unsupported_monitor_version"
    serialized_loop = service._serialize_state()["loops"][0]
    assert serialized_loop["active"] is False
    serialized_monitor = serialized_loop["monitor"]
    assert serialized_monitor == future_monitor


@pytest.mark.asyncio
async def test_unknown_monitor_version_is_never_armed(tmp_path) -> None:
    service = AutoNudgeService(base_dir=tmp_path)
    loop = NudgeLoop(
        id="future03",
        slot_key="chat-1-123",
        message="future instructions",
        idle_secs=300,
        active=True,
        next_due_ts=time.time() + 60,
        monitor=MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=time.time(),
            version=99,
        ),
    )

    with patch.object(service, "_arm_timer") as arm_timer:
        service._arm_from_deadline(loop)

    arm_timer.assert_not_called()


def test_future_monitor_without_current_identity_is_inert_and_survives_rewrite(tmp_path) -> None:
    """A future schema stays opaque but cannot appear active to this gateway."""
    future_monitor = {
        "version": 99,
        "identity_v2": {"resource": "opaque", "intent": "future"},
        "future_policy": {"wake_every_time": True},
    }
    store = {
        "version": 1,
        "loops": [
            {
                "id": "future02",
                "slot_key": "chat-1-123",
                "message": "future instructions",
                "idle_secs": 300,
                "active": True,
                "monitor": future_monitor,
            }
        ],
    }
    path = tmp_path / "autonudge.json"
    path.write_text(json.dumps(store), encoding="utf-8")
    service = AutoNudgeService(base_dir=tmp_path)

    service._load()

    restored = service._loops["future02"]
    assert not restored.active
    assert restored.monitor is not None
    assert restored.monitor.version == 99
    assert restored.monitor.outcome is MonitorOutcome.BLOCKED
    service._save()
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["loops"][0]["active"] is False
    assert persisted["loops"][0]["monitor"] == future_monitor


@pytest.mark.asyncio
async def test_generic_update_cannot_reactivate_future_monitor(tmp_path) -> None:
    future_monitor = {
        "version": 99,
        "identity_v2": {"resource": "opaque", "intent": "future"},
    }
    store = {
        "version": 1,
        "loops": [
            {
                "id": "future03",
                "slot_key": "chat-1-123",
                "message": "future instructions",
                "idle_secs": 300,
                "active": True,
                "monitor": future_monitor,
            }
        ],
    }
    path = tmp_path / "autonudge.json"
    path.write_text(json.dumps(store), encoding="utf-8")
    service = AutoNudgeService(base_dir=tmp_path)
    service._load()
    await service.start()

    updated = await service.update("future03", active=True)

    assert updated is not None
    assert updated.active is False
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["loops"][0]["active"] is False
    assert persisted["loops"][0]["monitor"] == future_monitor
    service.stop()


def test_malformed_monitor_cannot_rearm_after_restart(tmp_path) -> None:
    """Invalid current state stays inspectable and cannot reach decision arithmetic."""
    malformed_monitor = {
        "version": 1,
        "kind": "github_pull_request",
        "target": "owner/repo#123",
        "objective": "review_ready",
        "created_ts": 1_000.0,
        "agent_turns": "many",
    }
    store = {
        "version": 1,
        "loops": [
            {
                "id": "broken01",
                "slot_key": "chat-1-123",
                "message": "unsafe instructions",
                "idle_secs": 300,
                "active": True,
                "monitor": malformed_monitor,
            }
        ],
    }
    (tmp_path / "autonudge.json").write_text(json.dumps(store), encoding="utf-8")
    service = AutoNudgeService(base_dir=tmp_path)

    service._load()

    restored = service._loops["broken01"]
    assert not restored.active
    assert restored.monitor is not None
    assert restored.monitor.outcome is MonitorOutcome.BLOCKED
    assert restored.monitor.stopped_reason == "invalid_monitor_record"
    serialized_monitor = service._serialize_state()["loops"][0]["monitor"]
    assert serialized_monitor["outcome"] == "blocked"
    assert serialized_monitor["stopped_reason"] == "invalid_monitor_record"
    assert serialized_monitor["agent_turns"] == 0

    service._save()
    persisted = json.loads((tmp_path / "autonudge.json").read_text(encoding="utf-8"))
    persisted_monitor = persisted["loops"][0]["monitor"]
    assert persisted_monitor["outcome"] == "blocked"
    assert persisted_monitor["stopped_reason"] == "invalid_monitor_record"

    reloaded = AutoNudgeService(base_dir=tmp_path)
    reloaded._load()
    assert not reloaded._store_dirty


def test_oversized_monitor_timestamp_is_quarantined_without_data_loss(tmp_path) -> None:
    """An integer too large for float conversion remains inspectable and inert."""
    malformed_monitor = {
        "version": 1,
        "kind": "github_pull_request",
        "target": "owner/repo#123",
        "objective": "review_ready",
        "created_ts": 10**400,
    }
    store = {
        "version": 1,
        "loops": [
            {
                "id": "broken-large-timestamp",
                "slot_key": "chat-1-123",
                "message": "unsafe instructions",
                "idle_secs": 300,
                "active": True,
                "monitor": malformed_monitor,
            }
        ],
    }
    path = tmp_path / "autonudge.json"
    path.write_text(json.dumps(store), encoding="utf-8")
    service = AutoNudgeService(base_dir=tmp_path)

    service._load()

    restored = service._loops["broken-large-timestamp"]
    assert not restored.active
    assert restored.monitor is not None
    assert restored.monitor.created_ts == 0.0
    assert restored.monitor.outcome is MonitorOutcome.BLOCKED
    assert restored.monitor.stopped_reason == "invalid_monitor_record"
    serialized_monitor = service._serialize_state()["loops"][0]["monitor"]
    assert serialized_monitor["outcome"] == "blocked"
    assert serialized_monitor["stopped_reason"] == "invalid_monitor_record"


def test_malformed_current_outcome_cannot_rearm_after_restart(tmp_path) -> None:
    """A non-enum terminal value is malformed, not evidence the loop is active."""
    store = {
        "version": 1,
        "loops": [
            {
                "id": "broken02",
                "slot_key": "chat-1-123",
                "message": "unsafe instructions",
                "idle_secs": 300,
                "active": True,
                "monitor": {
                    "version": 1,
                    "kind": "github_pull_request",
                    "target": "owner/repo#123",
                    "objective": "review_ready",
                    "created_ts": 1_000.0,
                    "outcome": "",
                },
            }
        ],
    }
    (tmp_path / "autonudge.json").write_text(json.dumps(store), encoding="utf-8")
    service = AutoNudgeService(base_dir=tmp_path)

    service._load()

    restored = service._loops["broken02"]
    assert not restored.active
    assert restored.monitor is not None
    assert restored.monitor.outcome is MonitorOutcome.BLOCKED
    assert restored.monitor.stopped_reason == "invalid_monitor_record"
