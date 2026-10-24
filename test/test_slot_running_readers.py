from __future__ import annotations

import ast
from pathlib import Path

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
    # Member Fresh start: the slot's task or the session's turn.
    "_turn_busy": frozenset({"running"}),
    # A failed create's newborn retraction: keeps a slot a turn has reserved.
    "_retract_failed_newborn": frozenset({"running"}),
}


def test_running_guarded_task_loads_null_check_task() -> None:
    """Every ``x.task`` read under an ``x.running`` guard also checks it is set."""
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
    assert {path for path, _line in guarded_task_loads} == {"slot_lifecycle.py"}


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
                # A crewmate's first greeting (Mate's, or a new crewmate's goal
                # question) must not land on a slot a send has already reserved,
                # so it reads the reservation, not the turn.
                ("mate_welcome.py", "maybe_start_first_greeting"),
                # A goal check-in skips its run rather than land on a slot a send
                # has already reserved; it asks again after reading the ledger.
                ("goal_reminder.py", "_idle_thread"),
                # A worker report or the upgrade notice waits for the crewmate's
                # thread to be free, reservation included.
                ("mate_thread_wakes.py", "_idle"),
                ("chat_folders.py", "api_chat_slot_mode"),
                ("chat_api/slot_guards.py", "_switch_target_busy"),
                # The create's retraction of a newborn it answers with a
                # failure, through _retract_failed_newborn: it reads the
                # RESERVATION, because a turn that started on the slot during
                # the create's awaits keeps it.
                ("chat_api/slot_create.py", "_retract_failed_newborn"),
                ("chat_api/slot_create.py", "api_chat_slot_create"),
                ("handlers/members_thread_turns.py", "_turn_busy"),
                ("handlers/members_thread_turns.py", "_turn_ends_within"),
                # The agent route's transaction; the in-turn /agent command shares it.
                ("chat_api/agent_switch.py", "switch_slot_agent"),
                ("chat_api/turn_stop.py", "api_chat_slot_interrupt"),
                ("chat_api/slot_model.py", "api_chat_slot_model"),
                ("chat_api/slot_note.py", "api_chat_slot_note"),
                # The project route arms a deferred reset when a start carrying
                # the old cwd is already in flight; slot.running (the
                # reservation) catches a cold-starting turn no provider has
                # registered yet.
                ("chat_api/session_relaunch.py", "api_chat_slot_project"),
                ("chat_api/slot_effort.py", "api_chat_slot_reasoning_effort"),
                # Reload refuses a reserved slot or sibling alias through
                # _switch_target_busy, as the session_reload verb does.
                ("chat_api/session_relaunch.py", "api_chat_slot_reload"),
                # A crewmate's worker reload waits for a reserved worker slot the
                # same way, through _switch_target_busy.
                ("crewmate_worker_reload.py", "_is_busy"),
                ("chat_api/slot_lifecycle.py", "api_chat_slot_reset_conversation"),
                ("chat_api/resume.py", "api_chat_slot_resume"),
                ("chat_api/session_relaunch.py", "api_chat_slot_workspace"),
                ("chat_api/slot_lifecycle.py", "api_chat_slots_cleanup"),
                ("chat_api/slot_model.py", "api_chat_slots_model"),
                ("chat_api/continue_turn.py", "continue_slot_turn"),
                ("chat_api/turn_stop.py", "stop_slot_turn"),
                # Stop settlement wakes retained priority only on an unreserved slot.
                ("chat_api/turn_stop.py", "_make_stop_resolver"),
                ("chat_rewind.py", "api_chat_slot_rewind"),
                # The memory-ready queue drain starts a turn only on a slot no
                # dispatch has reserved, the same guard the dispatch routes take.
                ("chat_turn/queue_drain.py", "_drain_parked_queues"),
                ("chat_turn/eager_spawn.py", "_eager_spawn"),
                ("chat_turn/eager_spawn.py", "_prefetch_ttl"),
                ("handlers/autonudge.py", "api_autonudge_fire"),
                ("handlers/members_thread_turns.py", "api_member_fresh_start"),
                ("handlers/members_thread.py", "api_member_thread"),
                ("handlers/members_roster.py", "api_members"),
                # The session-side delivery entry point: idle gets a row and a
                # turn, a reserved slot gets the queue or a steer.
                ("session_deliver.py", "deliver"),
                # A peer's steer needs a running turn to cut into.
                ("session_deliver.py", "_deliver_peer"),
                ("session_control_parts/session_create.py", "create_session"),
                # The roster verb. Reservation state, like `read_messages` beside
                # it and for the same reason: it reports whether a session is
                # working so a patrol knows whether to wait.
                ("session_control_parts/session_status.py", "created_session_status"),
                ("session_control_parts/session_status.py", "read_messages"),
                # The summary verb reports liveness beside the digest for the
                # same reason `read_messages` does.
                ("session_control_parts/session_status.py", "read_summary"),
                ("session_control_parts/session_messaging.py", "send_to_target"),
                # Pre-pick idle check via _switch_target_busy (idle-only tool contract).
                ("session_control_parts/model_switch.py", "set_model_target"),
                ("openai_compat.py", "api_completions"),
                # Idle-only teardown: the same pre-lock and in-lock probe via
                # _switch_target_busy, so a reserved slot between stages is busy.
                ("session_control_parts/turn_control.py", "reload_target"),
                # The idle sweep never picks or pops a slot holding a reservation.
                ("slot_retention.py", "_still_idle_check"),
                ("slot_retention.py", "select_idle_slot_keys"),
                ("state_parts/chat_slot.py", "_ChatSlot.enqueue_or_run_prompt"),
                # A return brief must not start or publish after a send has
                # reserved the slot, even before its model turn begins.
                ("warm_task_brief.py", "_run_warm_task_brief"),
                # A worker holding a reservation is in-flight work for the
                # thread that created it, even before its model turn begins.
                ("warm_task_brief.py", "_running_workers"),
                ("warm_task_brief.py", "_settled"),
                ("warm_task_brief.py", "_still_current"),
                ("warm_task_brief.py", "_wait_for_idle_turn"),
                ("ws.py", "_handle_slot_focused"),
            }
        },
        **{
            site: execution
            for site in {
                ("channel_slots.py", "_window_refresh_is_safe"),
                ("chat_slack.py", "drain_slack_backfill"),
                # The synthesis outage re-check fires only on an idle slot: a
                # running TURN, not a reservation, is what it must not overlap.
                ("chat_turn/queue_cycle.py", "_arm_synthesis_recheck"),
                # A guide or card tool call is admitted only from the turn this
                # slot is executing; a reservation has no tool call yet.
                ("handlers/guide.py", "_resolve_agent_caller"),
                # A responsive steer needs a turn executing now; a reserved
                # slot keeps the queue path.
                ("responsive_chat.py", "_steer_ready"),
                ("slot_projection.py", "SlotProjection.to_dict"),
                ("slot_registry.py", "SlotRegistry.running_session_keys"),
                ("state_parts/chat_slot.py", "_ChatSlot.running"),
            }
        },
        **{
            site: both
            for site in {
                ("chat_api/slot_detail.py", "api_chat_slot_detail"),
                ("chat_api/chat_send.py", "api_chat"),
                ("responsive_chat.py", "accept"),
                ("chat_regenerate.py", "_destructive_history_busy"),
                ("chat_regenerate.py", "api_chat_slot_edit_resend"),
                ("chat_regenerate.py", "api_chat_slot_regenerate"),
                ("chat_regenerate.py", "api_chat_slot_switch_variant"),
            }
        },
    }
    assert actual == expected
    assert publishers == {
        # A crewmate's first-open greeting: a gateway-authored turn, started the
        # way api_send_message starts one (spawn_guarded_turn, then slot.task).
        # The Daily Goal Reminder's hidden turn shares the same publisher.
        ("mate_welcome.py", "dispatch_hidden_turn"),
        ("chat_api/chat_send.py", "api_chat"),
        ("chat_regenerate.py", "api_chat_slot_edit_resend"),
        ("chat_regenerate.py", "api_chat_slot_regenerate"),
        ("chat_rewind.py", "api_chat_slot_rewind"),
        ("chat_turn/queue_cycle.py", "_launch_synthesis"),
        ("chat_turn/queue_drain.py", "_start_next_queued_turn"),
        ("handlers/taskrunner.py", "api_taskrunner_to_chat"),
        ("openai_compat.py", "api_completions"),
        ("session_deliver.py", "deliver"),
        ("state_parts/chat_slot.py", "_ChatSlot.enqueue_or_run_prompt"),
    }
