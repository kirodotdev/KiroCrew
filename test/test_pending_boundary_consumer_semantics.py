from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard import channel_slots, chat_regenerate, session_health
from kiro_crew.dashboard.slot_registry import SlotRegistry
from kiro_crew.dashboard.state import StageBoundary, _ChatSlot, stage_boundary_for

# Busy-check helpers, and the slot state each one reads on its caller's behalf.
#
# A handler that delegates its refusal to one of these is still a CONSUMER of
# that state even though it does not name the attribute itself, so the
# enumeration below credits the caller with what the helper reads. Without that,
# extracting a guard into a helper silently drops every caller out of the table,
# and a NEW handler wired to the same helper adds no entry at all -- the drift
# this test exists to catch stops tripping it.
#
# Add a helper here when it becomes the only thing standing between a handler
# and a live turn.
_BUSY_HELPERS: dict[str, frozenset[str]] = {
    # Regenerate, variant switch and edit-resend: refuses on either state.
    "_destructive_history_busy": frozenset({"running", "turn_running"}),
    # Agent, model, reasoning-effort and workspace switches: refuses on the
    # RESERVATION, because ``slot.running`` is set at dispatch and so sees a
    # cold-starting first turn that no provider has registered yet.
    "_switch_target_busy": frozenset({"running"}),
}


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


def test_stage_boundary_for_tolerates_frozen_minimal_test_double(caplog) -> None:
    """A slots-only test double gets an observable ephemeral boundary."""

    class _MinimalSlot:
        __slots__ = ()

    slot = _MinimalSlot()
    caplog.set_level("WARNING", logger="kiro_crew.dashboard.state")

    boundary = stage_boundary_for(slot)

    assert isinstance(boundary, StageBoundary)
    assert not hasattr(slot, "stage_boundary")
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "_MinimalSlot" in warnings[0].getMessage()


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


def test_running_and_turn_running_slot_readers_are_enumerated() -> None:
    """Every slot reader declares whether it needs reservation or execution state."""
    dashboard = Path(__file__).parents[1] / "src" / "kiro_crew" / "dashboard"
    actual: dict[tuple[str, str], frozenset[str]] = {}
    publishers: set[tuple[str, str]] = set()
    for path in sorted(dashboard.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        definitions: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                definitions.append((node.name, node))
            elif isinstance(node, ast.ClassDef):
                definitions.extend(
                    (f"{node.name}.{child.name}", child)
                    for child in node.body
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                )
        for symbol, definition in definitions:
            aliases = {"slot", "removed"}
            if symbol.startswith("_ChatSlot."):
                aliases.add("self")
            site = (path.relative_to(dashboard).as_posix(), symbol)
            for assignment in ast.walk(definition):
                if isinstance(assignment, ast.Assign):
                    targets = assignment.targets
                    value = assignment.value
                elif isinstance(assignment, ast.AnnAssign):
                    targets = [assignment.target]
                    value = assignment.value
                else:
                    continue
                if isinstance(value, ast.Constant) and value.value is None:
                    continue
                if any(
                    isinstance(target, ast.Attribute)
                    and target.attr == "task"
                    and isinstance(target.value, ast.Name)
                    and target.value.id in aliases
                    for target in targets
                ):
                    publishers.add(site)
            predicates = {
                attr.attr
                for attr in ast.walk(definition)
                if isinstance(attr, ast.Attribute)
                and attr.attr in {"running", "turn_running"}
                and isinstance(attr.value, ast.Name)
                and attr.value.id in aliases
                and isinstance(attr.ctx, ast.Load)
            }
            for call in ast.walk(definition):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                    predicates.update(_BUSY_HELPERS.get(call.func.id, frozenset()))
            if predicates:
                actual[site] = frozenset(predicates)

    reservation = frozenset({"running"})
    execution = frozenset({"turn_running"})
    both = frozenset({"running", "turn_running"})
    expected = {
        **{
            site: reservation
            for site in {
                ("chat_folders.py", "api_chat_slot_mode"),
                ("chat_handlers.py", "_switch_target_busy"),
                ("chat_handlers.py", "api_chat_slot_agent"),
                # Per-chat backend switch: tears the session down, so it refuses on
                # the RESERVATION like its agent/model siblings (and delegates the
                # sibling-turn scan to _switch_target_busy, credited above).
                ("chat_handlers.py", "api_chat_slot_backend"),
                # Slot creation with a backend pick: when the sealed pin store
                # cannot be written, the newborn is retracted only while it is
                # still this request's own -- no turn RESERVED on it -- the same
                # check session_control.create_session makes on a failed birth
                # write (credited below).
                ("chat_handlers.py", "api_chat_slot_create"),
                ("chat_handlers.py", "api_chat_slot_continue"),
                ("chat_handlers.py", "api_chat_slot_detail"),
                ("chat_handlers.py", "api_chat_slot_interrupt"),
                ("chat_handlers.py", "api_chat_slot_model"),
                ("chat_handlers.py", "api_chat_slot_note"),
                ("chat_handlers.py", "api_chat_slot_reasoning_effort"),
                ("chat_handlers.py", "api_chat_slot_reset_conversation"),
                ("chat_handlers.py", "api_chat_slot_resume"),
                ("chat_handlers.py", "api_chat_slot_workspace"),
                ("chat_handlers.py", "api_chat_slots_cleanup"),
                ("chat_handlers.py", "api_chat_slots_model"),
                ("chat_handlers.py", "stop_slot_turn"),
                ("chat_rewind.py", "api_chat_slot_rewind"),
                ("chat_runner.py", "_eager_spawn"),
                ("chat_runner.py", "_prefetch_ttl"),
                ("handlers/autonudge.py", "api_autonudge_fire"),
                ("handlers/members.py", "api_member_thread"),
                ("handlers/members.py", "api_members"),
                ("handlers/messaging.py", "api_send_message"),
                ("session_control.py", "create_session"),
                ("session_control.py", "read_messages"),
                ("session_control.py", "send_to_target"),
                ("state.py", "_ChatSlot.enqueue_or_run_prompt"),
                ("ws.py", "_handle_slot_focused"),
            }
        },
        **{
            site: execution
            for site in {
                ("channel_slots.py", "_window_refresh_is_safe"),
                ("chat_orchestrator.py", "api_chat_plan_action"),
                ("chat_slack.py", "drain_slack_backfill"),
                ("slot_projection.py", "SlotProjection.to_dict"),
                ("slot_registry.py", "SlotRegistry.running_session_keys"),
                ("state.py", "_ChatSlot.running"),
            }
        },
        **{
            site: both
            for site in {
                ("chat_handlers.py", "api_chat"),
                ("chat_regenerate.py", "_destructive_history_busy"),
                ("chat_regenerate.py", "api_chat_slot_edit_resend"),
                ("chat_regenerate.py", "api_chat_slot_regenerate"),
                ("chat_regenerate.py", "api_chat_slot_switch_variant"),
                ("openai_compat.py", "api_completions"),
            }
        },
    }
    assert actual == expected
    assert publishers == {
        ("chat_handlers.py", "api_chat"),
        ("chat_orchestrator.py", "_stage_loop"),
        ("chat_orchestrator.py", "api_chat_plan_action"),
        ("chat_regenerate.py", "api_chat_slot_edit_resend"),
        ("chat_regenerate.py", "api_chat_slot_regenerate"),
        ("chat_rewind.py", "api_chat_slot_rewind"),
        ("chat_runner.py", "_finish_queue_cycle"),
        ("chat_runner.py", "_start_next_queued_turn"),
        ("handlers/messaging.py", "api_send_message"),
        ("handlers/taskrunner.py", "api_taskrunner_to_chat"),
        ("openai_compat.py", "api_completions"),
        ("state.py", "_ChatSlot.enqueue_or_run_prompt"),
    }
