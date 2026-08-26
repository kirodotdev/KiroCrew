"""Tests for the agent TODO list surfaced above the chat composer.

The payload shapes here were captured from a live ``kiro-cli acp`` session
(v2.14.0), not invented: the tool arrives as an ordinary ``tool_call_update``
identified ONLY by ``_meta.kiro.toolName``, and its ``rawOutput`` echoes the
whole list on every command.
"""

from typing import Any

from kiro_crew.acp._dispatch import parse_session_update, parse_todo_snapshot
from kiro_crew.acp.types import EVENT_TODO_UPDATE, TODO_TASKS_MAX, TODO_TEXT_MAX
from kiro_crew.dashboard.state import _ChatSlot


def _update(tasks: list[dict[str, Any]], description: str = "Config workflow") -> dict[str, Any]:
    """A real-shaped todo_list tool_call_update."""
    return {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "toolu_bdrk_01JEZ",
        "kind": "other",
        "status": "completed",
        "title": f"Creating task list: {description}",
        "_meta": {"kiro": {"toolName": "todo_list"}},
        "rawOutput": {
            "items": [{"Json": {"tasks": tasks, "description": description, "context": []}}]
        },
    }


class TestParseTodoSnapshot:
    """parse_todo_snapshot: identification and normalisation."""

    def test_parses_real_captured_payload(self) -> None:
        snap = parse_todo_snapshot(
            _update(
                [
                    {"id": "1", "task_description": "read config", "completed": True},
                    {"id": "2", "task_description": "parse it", "completed": False},
                ]
            )
        )
        assert snap == {
            "description": "Config workflow",
            "tasks": [
                {"id": "1", "text": "read config", "completed": True},
                {"id": "2", "text": "parse it", "completed": False},
            ],
        }

    def test_identified_by_meta_not_title(self) -> None:
        """The title is LLM prose; only _meta.kiro.toolName identifies the tool.

        Regression guard: a heuristic on the title would match any message that
        happens to say "task list", and would MISS a real todo result whose
        title the model phrased differently.
        """
        upd = _update([{"id": "1", "task_description": "x", "completed": False}])
        upd["_meta"] = {"kiro": {"toolName": "fs_read"}}
        assert parse_todo_snapshot(upd) is None

    def test_title_alone_does_not_match(self) -> None:
        upd = _update([{"id": "1", "task_description": "x", "completed": False}])
        del upd["_meta"]
        assert parse_todo_snapshot(upd) is None

    def test_empty_list_is_a_snapshot_not_a_non_match(self) -> None:
        """The agent clearing its list is meaningful — distinct from never using it."""
        snap = parse_todo_snapshot(_update([], description=""))
        assert snap == {"description": "", "tasks": []}

    def test_missing_raw_output_is_none(self) -> None:
        upd = _update([{"id": "1", "task_description": "x", "completed": False}])
        del upd["rawOutput"]
        assert parse_todo_snapshot(upd) is None

    def test_bare_dict_raw_output_tolerated(self) -> None:
        """The {items:[{Json:…}]} wrapper is not ours; accept a bare dict too."""
        upd = _update([])
        upd["rawOutput"] = {"tasks": [{"id": "9", "task_description": "z", "completed": True}]}
        snap = parse_todo_snapshot(upd)
        assert snap is not None
        assert snap["tasks"] == [{"id": "9", "text": "z", "completed": True}]

    def test_non_bool_completed_is_coerced(self) -> None:
        snap = parse_todo_snapshot(_update([{"id": "1", "task_description": "x", "completed": "yes"}]))
        assert snap is not None
        assert snap["tasks"][0]["completed"] is True

    def test_missing_id_falls_back_to_position(self) -> None:
        snap = parse_todo_snapshot(_update([{"task_description": "a"}, {"task_description": "b"}]))
        assert snap is not None
        assert [t["id"] for t in snap["tasks"]] == ["1", "2"]

    def test_non_dict_task_entries_skipped(self) -> None:
        snap = parse_todo_snapshot(
            _update(["garbage", {"id": "2", "task_description": "ok", "completed": False}])  # type: ignore[list-item]
        )
        assert snap is not None
        assert [t["text"] for t in snap["tasks"]] == ["ok"]

    def test_task_count_is_capped(self) -> None:
        many = [
            {"id": str(i), "task_description": f"t{i}", "completed": False}
            for i in range(TODO_TASKS_MAX + 50)
        ]
        snap = parse_todo_snapshot(_update(many))
        assert snap is not None
        assert len(snap["tasks"]) == TODO_TASKS_MAX

    def test_task_text_is_capped(self) -> None:
        snap = parse_todo_snapshot(
            _update([{"id": "1", "task_description": "x" * (TODO_TEXT_MAX + 500), "completed": False}])
        )
        assert snap is not None
        assert len(snap["tasks"][0]["text"]) == TODO_TEXT_MAX

    def test_credentials_in_task_text_are_redacted(self) -> None:
        """Task text is agent-authored free text that reaches the browser."""
        secret = "AKIAIOSFODNN7EXAMPLE"
        snap = parse_todo_snapshot(
            _update([{"id": "1", "task_description": f"use key {secret}", "completed": False}])
        )
        assert snap is not None
        assert secret not in snap["tasks"][0]["text"]

    def test_non_dict_update_is_none(self) -> None:
        assert parse_todo_snapshot("nope") is None  # type: ignore[arg-type]


class TestTodoEventEmission:
    """parse_session_update: the todo event is ADDITIVE, not a swallow."""

    def test_emits_todo_update_event(self) -> None:
        events = parse_session_update(
            _update([{"id": "1", "task_description": "a", "completed": False}]),
            tool_input_cache={},
        )
        todo_events = [e for e in events if e.kind == EVENT_TODO_UPDATE]
        assert len(todo_events) == 1
        assert todo_events[0].todo is not None
        assert todo_events[0].todo["tasks"][0]["text"] == "a"

    def test_tool_call_events_still_flow(self) -> None:
        """The todo tool call must still render in the transcript like any other.

        Revert guard: swallowing the update to "handle" the todo would silently
        drop a tool call from the conversation.
        """
        events = parse_session_update(
            _update([{"id": "1", "task_description": "a", "completed": False}]),
            tool_input_cache={},
        )
        kinds = [e.kind for e in events]
        assert "tool_result" in kinds
        assert "tool_call_update" in kinds

    def test_non_todo_update_emits_no_todo_event(self) -> None:
        upd = _update([])
        upd["_meta"] = {"kiro": {"toolName": "execute_bash"}}
        events = parse_session_update(upd, tool_input_cache={})
        assert not [e for e in events if e.kind == EVENT_TODO_UPDATE]


class TestSlotTodoStore:
    """_ChatSlot todo storage, change detection, and derived counts."""

    def _slot(self) -> _ChatSlot:
        slot = _ChatSlot.__new__(_ChatSlot)
        slot._todo = None
        return slot

    def test_absent_by_default(self) -> None:
        assert self._slot().todo_payload() is None

    def test_set_todo_reports_change(self) -> None:
        slot = self._slot()
        snap = {"description": "W", "tasks": [{"id": "1", "text": "a", "completed": False}]}
        assert slot.set_todo(snap) is True

    def test_identical_snapshot_reports_no_change(self) -> None:
        """Gates the WS broadcast — a turn re-echoes the same list repeatedly.

        Revert guard: returning True unconditionally fans a redundant broadcast
        to every connected socket on every todo tool result.
        """
        slot = self._slot()
        snap = {"description": "W", "tasks": [{"id": "1", "text": "a", "completed": False}]}
        assert slot.set_todo(snap) is True
        assert slot.set_todo(dict(snap)) is False

    def test_derived_counts(self) -> None:
        slot = self._slot()
        slot.set_todo(
            {
                "description": "W",
                "tasks": [
                    {"id": "1", "text": "a", "completed": True},
                    {"id": "2", "text": "b", "completed": False},
                    {"id": "3", "text": "c", "completed": False},
                ],
            }
        )
        payload = slot.todo_payload()
        assert payload is not None
        assert payload["completed"] == 1
        assert payload["total"] == 3

    def test_current_is_first_incomplete_task(self) -> None:
        """kiro-cli has no in-progress state, so "current" is this derivation."""
        slot = self._slot()
        slot.set_todo(
            {
                "description": "",
                "tasks": [
                    {"id": "1", "text": "done one", "completed": True},
                    {"id": "2", "text": "next one", "completed": False},
                    {"id": "3", "text": "later one", "completed": False},
                ],
            }
        )
        payload = slot.todo_payload()
        assert payload is not None
        assert payload["current"] == "next one"

    def test_current_empty_when_all_complete(self) -> None:
        slot = self._slot()
        slot.set_todo({"description": "", "tasks": [{"id": "1", "text": "a", "completed": True}]})
        payload = slot.todo_payload()
        assert payload is not None
        assert payload["current"] == ""
        assert payload["completed"] == payload["total"] == 1

    def test_cleared_list_is_distinct_from_absent(self) -> None:
        slot = self._slot()
        slot.set_todo({"description": "", "tasks": []})
        payload = slot.todo_payload()
        assert payload is not None
        assert payload["total"] == 0

    def test_reset_to_none(self) -> None:
        slot = self._slot()
        slot.set_todo({"description": "", "tasks": [{"id": "1", "text": "a", "completed": False}]})
        assert slot.set_todo(None) is True
        assert slot.todo_payload() is None

    def test_malformed_task_entries_do_not_crash_counts(self) -> None:
        slot = self._slot()
        slot._todo = {"description": "", "tasks": ["garbage", {"id": "1", "text": "a", "completed": True}]}
        payload = slot.todo_payload()
        assert payload is not None
        assert payload["total"] == 1
        assert payload["completed"] == 1
