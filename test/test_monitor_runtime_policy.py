"""One finite runtime policy across monitor entry points and persisted state."""

from types import SimpleNamespace

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.monitoring.models import MonitorBudgets, monitor_state_from_dict
from kiro_crew.validation import (
    MONITOR_START_SCHEMA,
    MONITOR_UPDATE_SCHEMA,
    MONITOR_WATCH_SCHEMA,
    ValidationError,
    validate_tool_args,
)

MONTH = 2_592_000


@pytest.fixture
def monthly_policy(monkeypatch):
    monkeypatch.setattr(
        KiroCrewConfig,
        "load",
        lambda: SimpleNamespace(monitoring=SimpleNamespace(max_runtime_secs=MONTH)),
    )


@pytest.mark.parametrize(
    "schema,args",
    [
        (MONITOR_START_SCHEMA, {"message": "Daily PR maintenance"}),
        (MONITOR_UPDATE_SCHEMA, {}),
        (
            MONITOR_WATCH_SCHEMA,
            {
                "kind": "github_pull_request",
                "target": "https://github.com/a/b/pull/1",
                "objective": "review_ready",
            },
        ),
    ],
)
def test_tools_share_operator_runtime_boundary(monthly_policy, schema, args):
    assert (
        validate_tool_args({**args, "max_runtime_secs": MONTH}, schema)["max_runtime_secs"] == MONTH
    )
    for bad in (0, -1, MONTH + 1, True, float("inf")):
        with pytest.raises(ValidationError):
            validate_tool_args({**args, "max_runtime_secs": bad}, schema)


def test_monitor_metadata_obeys_absolute_boundary(monthly_policy):
    raw = {
        "kind": "github_pull_request",
        "target": "https://github.com/a/b/pull/1",
        "objective": "review_ready",
        "created_ts": 100.0,
        "budgets": {"max_runtime_secs": MONTH},
    }
    assert monitor_state_from_dict(raw).budgets.max_runtime_secs == MONTH
    raw["budgets"]["max_runtime_secs"] = MONTH + 1
    with pytest.raises(ValueError):
        monitor_state_from_dict(raw)
    with pytest.raises(ValueError):
        MonitorBudgets(max_runtime_secs=MONTH + 1)


@pytest.mark.asyncio
async def test_daily_loop_restart_update_expiry_and_stop(tmp_path, monthly_policy):
    from kiro_crew.autonudge import AutoNudgeService, runtime_budget_exceeded

    service = AutoNudgeService(base_dir=tmp_path)
    try:
        loop = await service.add(
            slot_key="daily-pr",
            message="Maintain PR",
            idle_secs=86400,
            max_cycles=30,
            max_runtime_secs=MONTH,
            gate=False,
        )
        created = loop.created_ts
        deadline = loop.next_due_ts
        with pytest.raises(ValueError):
            await service.update(loop.id, max_runtime_secs=MONTH + 1)
        assert loop.max_runtime_secs == MONTH
    finally:
        service.stop()
    restored = AutoNudgeService(base_dir=tmp_path)
    restored._load()
    try:
        row = restored.get_by_slot("daily-pr")
        assert row.active and row.max_runtime_secs == MONTH
        assert row.idle_secs == 86400 and row.created_ts == created
        assert row.next_due_ts == deadline
        assert not runtime_budget_exceeded(row, created + MONTH - 1)
        assert runtime_budget_exceeded(row, created + MONTH)
        await restored.update(row.id, active=False, stopped_reason="user_stop")
    finally:
        restored.stop()
    stopped = AutoNudgeService(base_dir=tmp_path)
    stopped._load()
    try:
        row = stopped.get_by_slot("daily-pr")
        assert not row.active and row.stopped_reason == "user_stop"
    finally:
        stopped.stop()


def test_api_state_creation_shares_runtime_boundary(monthly_policy):
    from kiro_crew.dashboard.handlers.autonudge import _monitor_config

    body = {
        "kind": "github_pull_request",
        "target": "https://github.com/a/b/pull/1",
        "objective": "review_ready",
        "max_runtime_secs": MONTH,
    }
    assert _monitor_config(body, gitlab_hosts=frozenset()).budgets.max_runtime_secs == MONTH
    for bad in (MONTH + 1, 0, True, 1.5):
        with pytest.raises(ValueError):
            _monitor_config({**body, "max_runtime_secs": bad}, gitlab_hosts=frozenset())


@pytest.mark.asyncio
async def test_lowered_policy_leaves_a_stored_budget_running(tmp_path, monthly_policy, monkeypatch):
    """The ceiling binds budgets as they are WRITTEN. A loop armed under a
    monthly ceiling and loaded under a weekly one keeps running on its stored
    budget; a pause and a resume that carry no budget are not re-checked, and
    only a new budget over the ceiling is refused."""
    from kiro_crew.autonudge import AutoNudgeService

    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(
        slot_key="lowered", message="check", idle_secs=86400, max_runtime_secs=MONTH
    )
    service.stop()
    monkeypatch.setattr("kiro_crew.monitoring.limits.runtime_ceiling_secs", lambda: 604800)
    restored = AutoNudgeService(base_dir=tmp_path)
    restored._load()
    try:
        row = restored.get_by_slot("lowered")
        assert row.active and row.stopped_reason == "" and row.max_runtime_secs == MONTH
        await restored.update(loop.id, active=False)
        assert not row.active and row.stopped_reason == "manual"
        resumed = await restored.update(loop.id, active=True)
        assert resumed is row and row.active and row.max_runtime_secs == MONTH
        with pytest.raises(ValueError, match="604800"):
            await restored.update(loop.id, max_runtime_secs=MONTH)
        assert row.active and row.max_runtime_secs == MONTH
    finally:
        restored.stop()


@pytest.mark.asyncio
async def test_persisted_legacy_budget_above_the_absolute_maximum_is_left_as_stored(tmp_path):
    """A hand-edited legacy budget above the absolute maximum is not the load
    path's to correct: the row is read back as written, exactly as before the
    ceiling existed, and the value meets the bound only when it is next
    written."""
    import json

    from kiro_crew.autonudge import AutoNudgeService
    from kiro_crew.monitoring.limits import MAX_RUNTIME_CEILING_SECS

    assert MAX_RUNTIME_CEILING_SECS == MONTH
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(
        slot_key="over-max", message="check", idle_secs=86400, max_runtime_secs=600
    )
    service.stop()
    store = tmp_path / "autonudge.json"
    raw = json.loads(store.read_text(encoding="utf-8"))
    (row,) = [entry for entry in raw["loops"] if entry["id"] == loop.id]
    assert row["active"] is True
    row["max_runtime_secs"] = 5_000_000
    store.write_text(json.dumps(raw), encoding="utf-8")

    restored = AutoNudgeService(base_dir=tmp_path)
    restored._load()
    try:
        kept = restored.get_by_id(loop.id)
        assert kept is not None and kept.active
        assert kept.stopped_reason == ""
        assert kept.max_runtime_secs == 5_000_000
        assert not restored._store_dirty
        with pytest.raises(ValueError):
            await restored.update(loop.id, max_runtime_secs=5_000_000)
        assert kept.max_runtime_secs == 5_000_000
    finally:
        restored.stop()


@pytest.mark.asyncio
async def test_persisted_structured_budget_above_the_absolute_maximum_is_quarantined(tmp_path):
    """A structured budget above the absolute maximum fails the model's own
    shape check (``MonitorBudgets``), so it is a malformed record and takes the
    inert quarantine shape, never a policy hold."""
    import json

    from kiro_crew.autonudge import AutoNudgeService
    from kiro_crew.monitoring.models import MONITOR_STOP_INVALID_RECORD, MonitorOutcome

    service = AutoNudgeService(base_dir=tmp_path)
    try:
        loop = await service.add_monitor(
            slot_key="over-max",
            kind="github_pull_request",
            target="https://github.com/a/b/pull/1",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(max_runtime_secs=600),
        )
        store = service._path
    finally:
        service.stop()
    raw = json.loads(store.read_text(encoding="utf-8"))
    (row,) = [entry for entry in raw["loops"] if entry["id"] == loop.id]
    row["monitor"]["budgets"]["max_runtime_secs"] = MONTH + 1
    store.write_text(json.dumps(raw), encoding="utf-8")
    restored = AutoNudgeService(base_dir=tmp_path)
    restored._load()
    try:
        kept = restored.get_by_id(loop.id)
        assert kept is not None and not kept.active
        assert kept.monitor.outcome is MonitorOutcome.BLOCKED
        assert kept.monitor.stopped_reason == MONITOR_STOP_INVALID_RECORD
    finally:
        restored.stop()


def test_an_unknown_stop_reason_is_not_replaceable():
    """No load path records a runtime-policy stop, so no such reason is in the
    replaceable set; a row carrying one (a store written by another build)
    fails CLOSED like any reason this version does not know."""
    from kiro_crew.autonudge import (
        _REPLACEABLE_LOOP_STOP_REASONS,
        NudgeLoop,
        _stopped_row_is_replaceable,
    )

    assert "invalid_runtime_budget" not in _REPLACEABLE_LOOP_STOP_REASONS
    row = NudgeLoop(
        id="held",
        slot_key="chat-1",
        message="check",
        active=False,
        stopped_reason="invalid_runtime_budget",
    )
    assert not _stopped_row_is_replaceable(row)


@pytest.mark.asyncio
async def test_low_policy_keeps_over_policy_structured_record_active(tmp_path, monkeypatch):
    """A well-formed structured record above a lowered ceiling loads active,
    with its budget, target and typed state as stored, and is never parked in
    ``_unparsed_rows`` or rewritten."""
    from kiro_crew.autonudge import AutoNudgeService

    service = AutoNudgeService(base_dir=tmp_path)
    try:
        armed = await service.add_monitor(
            slot_key="lowered",
            kind="github_pull_request",
            target="https://github.com/a/b/pull/1",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(max_runtime_secs=600),
        )
    finally:
        service.stop()
    monkeypatch.setattr("kiro_crew.monitoring.limits.runtime_ceiling_secs", lambda: 60)
    restored = AutoNudgeService(base_dir=tmp_path)
    restored._load()
    row = restored.get_by_slot("lowered")
    assert row is not None and row.active
    assert row.stopped_reason == ""
    assert row.monitor.outcome is None
    assert row.monitor.budgets.max_runtime_secs == 600
    assert row.monitor.created_ts == armed.monitor.created_ts
    assert row.monitor.target == "https://github.com/a/b/pull/1"
    assert not restored._unparsed_rows
    restored.stop()


@pytest.mark.asyncio
async def test_malformed_structured_record_is_still_quarantined(tmp_path, monkeypatch):
    """A record the model cannot parse keeps the inert quarantine shape; the
    ceiling plays no part in that decision."""
    import json

    from kiro_crew.autonudge import AutoNudgeService
    from kiro_crew.monitoring.models import MONITOR_STOP_INVALID_RECORD, MonitorOutcome

    service = AutoNudgeService(base_dir=tmp_path)
    try:
        await service.add_monitor(
            slot_key="broken",
            kind="github_pull_request",
            target="https://github.com/a/b/pull/1",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(max_runtime_secs=600),
        )
        store = service._path
    finally:
        service.stop()
    raw = json.loads(store.read_text())
    rows = raw["loops"] if isinstance(raw, dict) and "loops" in raw else raw
    for row in rows:
        row["monitor"]["budgets"]["max_runtime_secs"] = "not-a-number"
    store.write_text(json.dumps(raw))
    restored = AutoNudgeService(base_dir=tmp_path)
    restored._load()
    row = restored.get_by_slot("broken")
    assert row is not None and not row.active
    assert row.monitor.outcome is MonitorOutcome.BLOCKED
    assert row.monitor.stopped_reason == MONITOR_STOP_INVALID_RECORD
    restored.stop()


@pytest.mark.asyncio
async def test_low_policy_preserves_legacy_gate_metadata(tmp_path, monkeypatch):
    from kiro_crew.autonudge import AutoNudgeService, infer_monitor

    monkeypatch.setattr("kiro_crew.monitoring.limits.runtime_ceiling_secs", lambda: 3600)
    message = "Watch https://github.com/a/b/pull/1"
    assert infer_monitor(message, 100) is not None
    service = AutoNudgeService(base_dir=tmp_path)
    try:
        await service.add(slot_key="short-gated", message=message, max_runtime_secs=600, gate=True)
    finally:
        service.stop()
    restored = AutoNudgeService(base_dir=tmp_path)
    restored._load()
    try:
        row = restored.get_by_slot("short-gated")
        assert row.active and row.gate and row.monitor.outcome is None
        assert row.max_runtime_secs == 600
    finally:
        restored.stop()


@pytest.mark.asyncio
async def test_resume_without_a_budget_is_not_rechecked_but_a_supplied_one_is(
    tmp_path, monthly_policy, monkeypatch
):
    """Over PATCH, a resume that omits ``max_runtime_secs`` lands on a paused
    loop whose stored budget exceeds the lowered ceiling; a PATCH that supplies
    that same budget is the write the ceiling binds and answers 400 with the
    refusing range."""
    import asyncio
    import json
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.autonudge import AutoNudgeService
    from kiro_crew.dashboard.handlers.autonudge import api_autonudge_update

    service = AutoNudgeService(base_dir=tmp_path)
    try:
        loop = await service.add(slot_key="lowered", message="check", max_runtime_secs=MONTH)
        await service.update(loop.id, active=False)
        monkeypatch.setattr("kiro_crew.monitoring.limits.runtime_ceiling_secs", lambda: 604800)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.autonudge._require_monitor_owner",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.autonudge._autonudge_get", lambda: service
        )
        monkeypatch.setattr("kiro_crew.autonudge_authz.sel", MagicMock())

        def _request(body):
            return SimpleNamespace(
                match_info={"loop_id": loop.id},
                remote="test",
                json=AsyncMock(return_value=body),
            )

        response = await asyncio.wait_for(api_autonudge_update(_request({"active": True})), 5)
        assert response.status == 200
        assert loop.active and loop.max_runtime_secs == MONTH

        response = await asyncio.wait_for(
            api_autonudge_update(_request({"max_runtime_secs": MONTH})), 5
        )
        assert response.status == 400
        assert "604800" in json.loads(response.text)["error"]
        assert loop.active and loop.max_runtime_secs == MONTH
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_structured_service_applies_policy_on_create_and_update(tmp_path, monthly_policy):
    from kiro_crew.autonudge import AutoNudgeService

    service = AutoNudgeService(base_dir=tmp_path)
    args = dict(
        slot_key="structured",
        kind="github_pull_request",
        target="https://github.com/a/b/pull/1",
        objective="review_ready",
        cadence_secs=86400,
    )
    try:
        with pytest.raises(ValueError):
            await service.add_monitor(**args, budgets=MonitorBudgets(max_runtime_secs=MONTH + 1))
        assert service.get_by_slot("structured") is None
        loop = await service.add_monitor(**args, budgets=MonitorBudgets(max_runtime_secs=MONTH))
        with pytest.raises(ValueError):
            await service.update_monitor(loop.id, budget_patch={"max_runtime_secs": MONTH + 1})
        assert loop.monitor.budgets.max_runtime_secs == MONTH
    finally:
        service.stop()


def test_stdio_descriptors_advertise_the_operator_ceiling(monthly_policy):
    from kiro_crew.mcp_tools.control import schemas

    tools = {tool["name"]: tool for tool in schemas()}
    for name in ("monitor_start", "monitor_watch", "monitor_update"):
        assert tools[name]["inputSchema"]["properties"]["max_runtime_secs"]["maximum"] == MONTH


@pytest.mark.asyncio
async def test_active_metadata_update_keeps_a_stored_budget_over_the_lowered_policy(
    tmp_path, monthly_policy, monkeypatch
):
    from kiro_crew.autonudge import AutoNudgeService

    service = AutoNudgeService(base_dir=tmp_path)
    try:
        loop = await service.add(slot_key="lowered", message="original", max_runtime_secs=MONTH)
        created = loop.created_ts
        monkeypatch.setattr("kiro_crew.monitoring.limits.runtime_ceiling_secs", lambda: 604800)
        updated = await service.update(loop.id, message="revised")
        assert updated is loop and loop.message == "revised" and loop.active
        assert loop.created_ts == created and loop.max_runtime_secs == MONTH
        with pytest.raises(ValueError, match="604800"):
            await service.update(loop.id, message="again", max_runtime_secs=MONTH)
        assert loop.message == "revised" and loop.max_runtime_secs == MONTH
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_monitor_slot_read_advertises_the_live_operator_ceiling(monkeypatch):
    """The popover validates its runtime input against the ceiling the
    create/update handlers enforce, not against the contract's absolute max, so
    the per-slot read it already performs carries the live value."""
    import asyncio
    import json
    from unittest.mock import AsyncMock

    from kiro_crew.dashboard.handlers.autonudge import api_monitor_slot_get

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.autonudge.runtime_ceiling_secs", lambda: MONTH
    )
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.autonudge._require_monitor_owner",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr("kiro_crew.dashboard.handlers.autonudge._autonudge_get", lambda: None)
    request = SimpleNamespace(match_info={"slot_key": "chat-1"}, remote="test")
    response = await asyncio.wait_for(api_monitor_slot_get(request), 5)
    assert response.status == 200
    payload = json.loads(response.text)
    assert payload["monitor"] is None
    assert payload["max_runtime_ceiling_secs"] == MONTH


@pytest.mark.asyncio
async def test_raising_policy_does_not_extend_existing_deadline(tmp_path, monkeypatch):
    from kiro_crew.autonudge import AutoNudgeService, runtime_budget_exceeded
    from kiro_crew.monitoring.limits import DEFAULT_RUNTIME_CEILING_SECS

    policy = SimpleNamespace(max_runtime_secs=DEFAULT_RUNTIME_CEILING_SECS)
    config = SimpleNamespace(monitoring=policy)
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: config)
    monkeypatch.setattr("kiro_crew.config.live.snapshot", lambda: None)

    service = AutoNudgeService(base_dir=tmp_path)
    try:
        loop = await service.add(
            slot_key="raised-policy",
            message="check",
            max_runtime_secs=DEFAULT_RUNTIME_CEILING_SECS,
        )
        original = (
            loop.max_runtime_secs,
            loop.created_ts,
            loop.created_ts + loop.max_runtime_secs,
        )
    finally:
        service.stop()

    policy.max_runtime_secs = MONTH
    restored = AutoNudgeService(base_dir=tmp_path)
    restored._load()
    try:
        row = restored.get_by_slot("raised-policy")
        assert row is not None
        deadline = row.created_ts + row.max_runtime_secs
        assert (row.max_runtime_secs, row.created_ts, deadline) == original
        assert not runtime_budget_exceeded(row, deadline - 1)
        assert runtime_budget_exceeded(row, deadline)
    finally:
        restored.stop()


def _row_snapshot(row):
    if row.monitor is not None:
        return (
            row.active,
            row.stopped_reason,
            row.next_due_ts,
            row.monitor.created_ts,
            row.monitor.budgets.max_runtime_secs,
            row.monitor.outcome,
        )
    return (row.active, row.stopped_reason, row.next_due_ts, row.created_ts, row.max_runtime_secs)


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [False, True])
async def test_ceiling_changes_across_loads_never_rewrite_a_row(
    tmp_path, monthly_policy, monkeypatch, structured
):
    """Arm under a month, load under a week, then under a month again: every
    load reads the row back identical to how it was armed, none marks the
    store dirty for it, and the deadline stays creation time plus budget."""
    from kiro_crew.autonudge import AutoNudgeService, runtime_budget_exceeded
    from kiro_crew.monitoring.decision import monitor_budget_reason

    service = AutoNudgeService(base_dir=tmp_path)
    try:
        if structured:
            armed = await service.add_monitor(
                slot_key="kept",
                kind="github_pull_request",
                target="https://github.com/a/b/pull/1",
                objective="review_ready",
                cadence_secs=60,
                budgets=MonitorBudgets(max_runtime_secs=MONTH),
            )
        else:
            armed = await service.add(
                slot_key="kept", message="check", idle_secs=86400, max_runtime_secs=MONTH
            )
        expected = _row_snapshot(armed)
    finally:
        service.stop()
    for ceiling in (604800, MONTH, 60):
        monkeypatch.setattr("kiro_crew.monitoring.limits.runtime_ceiling_secs", lambda c=ceiling: c)
        restored = AutoNudgeService(base_dir=tmp_path)
        restored._load()
        try:
            row = restored.get_by_slot("kept")
            assert _row_snapshot(row) == expected, ceiling
            assert not restored._store_dirty, ceiling
            if structured:
                anchor = row.monitor.created_ts
                assert monitor_budget_reason(row.monitor, now=anchor + MONTH - 1) == ""
                assert monitor_budget_reason(row.monitor, now=anchor + MONTH) == "runtime_budget"
            else:
                anchor = row.created_ts
                assert not runtime_budget_exceeded(row, anchor + MONTH - 1)
                assert runtime_budget_exceeded(row, anchor + MONTH)
        finally:
            restored.stop()


@pytest.mark.asyncio
async def test_manual_pause_is_not_resumed_by_a_raised_ceiling(tmp_path, monthly_policy):
    from kiro_crew.autonudge import AutoNudgeService

    service = AutoNudgeService(base_dir=tmp_path)
    try:
        loop = await service.add(
            slot_key="paused", message="check", idle_secs=86400, max_runtime_secs=MONTH
        )
        await service.update(loop.id, active=False)
    finally:
        service.stop()
    restored = _loaded_service(tmp_path)
    row = restored.get_by_slot("paused")
    assert not row.active and row.stopped_reason == "manual"


def _loaded_service(tmp_path):
    from kiro_crew.autonudge import AutoNudgeService

    restored = AutoNudgeService(base_dir=tmp_path)
    restored._load()
    restored.stop()
    return restored


@pytest.mark.asyncio
async def test_structured_update_revalidates_only_a_supplied_runtime_budget(
    tmp_path, monthly_policy, monkeypatch
):
    """A lower live ceiling governs new budgets, not unrelated patches."""
    from kiro_crew.autonudge import AutoNudgeService

    service = AutoNudgeService(base_dir=tmp_path)
    try:
        loop = await service.add_monitor(
            slot_key="monthly-pr",
            kind="github_pull_request",
            target="https://github.com/a/b/pull/1",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(max_runtime_secs=MONTH),
        )
        monkeypatch.setattr(
            KiroCrewConfig,
            "load",
            lambda: SimpleNamespace(monitoring=SimpleNamespace(max_runtime_secs=604_800)),
        )

        updated = await service.update_monitor(loop.id, cadence_secs=120)
        assert updated is loop
        assert updated.monitor is not None
        assert updated.monitor.cadence_secs == 120
        assert updated.monitor.budgets.max_runtime_secs == MONTH

        with pytest.raises(ValueError, match="604800"):
            await service.update_monitor(
                loop.id,
                budget_patch={"max_runtime_secs": MONTH},
            )
        assert loop.monitor.budgets.max_runtime_secs == MONTH
    finally:
        service.stop()
