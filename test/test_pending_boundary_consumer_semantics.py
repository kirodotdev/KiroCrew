from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard import channel_slots, chat_regenerate, session_health
from kiro_crew.dashboard.slot_registry import SlotRegistry
from kiro_crew.dashboard.state import StageBoundary, _ChatSlot, stage_boundary_for


def test_stage_boundary_for_reraises_real_slot_assignment_failure(monkeypatch) -> None:
    """A production slot cannot hide a missing writable boundary field."""
    import kiro_crew.dashboard.state as state_module

    slot = _ChatSlot("miswired-boundary")
    slot.stage_boundary = None  # type: ignore[assignment]
    real_setattr = setattr

    def _reject_boundary(target, name, value) -> None:
        if target is slot and name == "stage_boundary":
            raise AttributeError("miswired stage boundary")
        real_setattr(target, name, value)

    monkeypatch.setattr(state_module, "setattr", _reject_boundary, raising=False)
    with pytest.raises(AttributeError, match="miswired stage boundary"):
        stage_boundary_for(slot)


def test_stage_boundary_for_tolerates_frozen_minimal_test_double() -> None:
    """A slots-only test double can still receive an ephemeral boundary."""

    class _MinimalSlot:
        __slots__ = ()

    slot = _MinimalSlot()

    boundary = stage_boundary_for(slot)

    assert isinstance(boundary, StageBoundary)
    assert not hasattr(slot, "stage_boundary")


def _paused_slot(name: str) -> _ChatSlot:
    slot = _ChatSlot(name)
    slot.stage_boundary.arm(1, consumed=True)
    assert slot.running is True
    assert slot.turn_running is False
    return slot


def test_slot_projection_distinguishes_paused_boundary_from_running_turn() -> None:
    slot = _paused_slot("projection-paused")
    slot.append("assistant", "Authentication paused this plan.")

    payload = slot.to_dict()

    assert payload["running"] is False
    assert payload["waiting_for_input"] is True


def test_session_health_ignores_a_paused_boundary_without_a_turn() -> None:
    slot = _paused_slot("health-paused")

    snapshot = session_health.snapshot_slot(slot, mono_now=1.0)

    assert snapshot.running is False
    monitor = session_health.SessionHealthMonitor(include_log_scan=False)
    assert monitor.classify_slot(snapshot, mono_now=1.0) is None


def test_channel_window_refresh_allows_a_paused_boundary() -> None:
    slot = _paused_slot("channel-paused")
    slot.linked_session_key = "slack:1712345678.901"
    slot._dirty = False

    assert channel_slots._window_refresh_is_safe(slot) is True


def test_slot_registry_excludes_a_paused_boundary_from_running_sessions() -> None:
    slot = _paused_slot("registry-paused")
    slot.linked_session_key = "slack:1712345678.902"
    owner = SimpleNamespace(_slots={slot.key: slot})

    running = SlotRegistry.running_session_keys(owner, lambda item: item.linked_session_key)

    assert running == frozenset()


def test_pending_boundary_refuses_destructive_history_edits() -> None:
    """Regenerate, variant switch, and edit-resend preserve reservations."""
    slot = _paused_slot("destructive-history-paused")

    response = chat_regenerate._destructive_history_busy(slot)

    assert response is not None
    assert response.status == 409
    assert json.loads(response.body) == {"error": "slot is busy", "code": "slot_busy"}
    for handler in (
        chat_regenerate.api_chat_slot_regenerate,
        chat_regenerate.api_chat_slot_switch_variant,
        chat_regenerate.api_chat_slot_edit_resend,
    ):
        assert "_destructive_history_busy(slot)" in inspect.getsource(handler)


def test_running_guarded_task_loads_null_check_task_for_pending_boundaries() -> None:
    slot = _paused_slot("taskless-boundary")
    assert slot.task is None

    dashboard = Path(__file__).parents[1] / "src" / "kiro_crew" / "dashboard"
    guarded_task_loads: list[tuple[str, int]] = []
    for path in dashboard.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            running_aliases = {
                attr.value.id
                for attr in ast.walk(node.test)
                if isinstance(attr, ast.Attribute)
                and attr.attr == "running"
                and isinstance(attr.value, ast.Name)
            }
            body_tree = ast.Module(body=node.body, type_ignores=[])
            for alias in running_aliases:
                task_loads = [
                    attr
                    for attr in ast.walk(body_tree)
                    if isinstance(attr, ast.Attribute)
                    and attr.attr == "task"
                    and isinstance(attr.value, ast.Name)
                    and attr.value.id == alias
                    and isinstance(attr.ctx, ast.Load)
                ]
                if not task_loads:
                    continue
                guarded_task_loads.append((path.name, node.lineno))
                assert f"{alias}.task is not None" in ast.unparse(node.test)

    assert len(guarded_task_loads) == 2
    assert {path for path, _line in guarded_task_loads} == {"chat_handlers.py"}
