"""Tests for the agent TODO list surfaced above the chat composer.

The payload shapes here are the ones a live ``kiro-cli acp`` session puts on
the wire, and the wire splits a todo_list call over TWO frames: the
``tool_call`` frame carries the identity (``_meta.kiro.toolName``) and no
result, and the ``tool_call_update`` frame carries the result with NO ``_meta``
at all. The captured evidence for that split is
``test/fixtures/acp_frames/kiro/session.jsonl`` (2.21.4 on the wire, same shape
2.22.1 sends). ``rawOutput`` echoes the whole list on every command.

An earlier version of this file put ``_meta`` on the RESULT frame, which no
kiro-cli sends; that fixture is why the parse could break on every live call
with every test here green.
"""

from typing import Any

from kiro_crew.acp._dispatch import parse_session_update, parse_todo_snapshot
from kiro_crew.acp.types import EVENT_TODO_UPDATE, TODO_TASKS_MAX, TODO_TEXT_MAX
from kiro_crew.dashboard.state import _ChatSlot

_CALL_ID = "toolu_bdrk_01JEZ"


def _call(description: str = "Config workflow") -> dict[str, Any]:
    """The real ``tool_call`` frame: identity on ``_meta``, no result yet."""
    return {
        "sessionUpdate": "tool_call",
        "toolCallId": _CALL_ID,
        "title": f"Creating task list: {description}",
        "kind": "other",
        "rawInput": {"command": "create", "tasks": []},
        "_meta": {"kiro": {"toolName": "todo_list"}},
    }


def _update(tasks: list[dict[str, Any]], description: str = "Config workflow") -> dict[str, Any]:
    """The real ``tool_call_update`` result frame: it carries NO ``_meta``.

    So asking THIS frame what tool it is answers nothing: the name is only
    recoverable from what the preceding ``tool_call`` frame cached per call id.
    """
    return {
        "sessionUpdate": "tool_call_update",
        "toolCallId": _CALL_ID,
        "kind": "other",
        "status": "completed",
        "title": f"Creating task list: {description}",
        "rawOutput": {
            "items": [{"Json": {"tasks": tasks, "description": description, "context": []}}]
        },
    }


def _name_cache() -> dict[str, str]:
    """What the ``tool_call`` frame leaves behind for the result frame to read."""
    return {_CALL_ID: "todo_list"}


def _caches(cache_scope: str = "") -> dict[str, Any]:
    """One dispatcher's per-session caches, as a runtime owns them."""
    return {
        "tool_input_cache": {},
        "shell_cache": {},
        "raw_params_cache": {},
        "mcp_server_name_cache": {},
        "tool_name_cache": {},
        "cache_scope": cache_scope,
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
            ),
            _name_cache(),
        )
        assert snap == {
            "description": "Config workflow",
            "tasks": [
                {"id": "1", "text": "read config", "completed": True},
                {"id": "2", "text": "parse it", "completed": False},
            ],
        }

    def test_identified_by_meta_not_title(self) -> None:
        """The title is LLM prose; only a trusted name identifies the tool.

        Regression guard: a heuristic on the title would match any message that
        happens to say "task list", and would MISS a real todo result whose
        title the model phrased differently.

        A frame that DOES assert an identity is believed over the cache: the
        cached name only answers for a frame that asserts nothing.
        """
        upd = _update([{"id": "1", "task_description": "x", "completed": False}])
        upd["_meta"] = {"kiro": {"toolName": "fs_read"}}
        assert parse_todo_snapshot(upd, _name_cache()) is None

    def test_title_alone_does_not_match(self) -> None:
        """No ``_meta`` on the frame AND no cached name for it: still no match."""
        upd = _update([{"id": "1", "task_description": "x", "completed": False}])
        assert parse_todo_snapshot(upd) is None
        assert parse_todo_snapshot(upd, {}) is None

    def test_another_calls_cached_name_does_not_match(self) -> None:
        """The cached name is read for THIS call id, never for a neighbour's."""
        upd = _update([{"id": "1", "task_description": "x", "completed": False}])
        assert parse_todo_snapshot(upd, {"toolu_other": "todo_list"}) is None

    def test_cached_name_is_read_under_the_scoped_key(self) -> None:
        """Cache entries are origin-bound, so the scope has to be spelled the same.

        A reader that dropped the scope would MISS every entry a scoped writer
        left, and a reader that ignored it would read another session's.
        """
        scoped = {"sess-1|" + _CALL_ID: "todo_list"}
        upd = _update([{"id": "1", "task_description": "x", "completed": False}])
        assert parse_todo_snapshot(upd, scoped, cache_scope="sess-1") is not None
        assert parse_todo_snapshot(upd, scoped, cache_scope="sess-2") is None

    def test_empty_list_is_a_snapshot_not_a_non_match(self) -> None:
        """The agent clearing its list is meaningful — distinct from never using it."""
        snap = parse_todo_snapshot(_update([], description=""), _name_cache())
        assert snap == {"description": "", "tasks": []}

    def test_missing_raw_output_is_none(self) -> None:
        upd = _update([{"id": "1", "task_description": "x", "completed": False}])
        del upd["rawOutput"]
        assert parse_todo_snapshot(upd, _name_cache()) is None

    def test_bare_dict_raw_output_tolerated(self) -> None:
        """The {items:[{Json:…}]} wrapper is not ours; accept a bare dict too."""
        upd = _update([])
        upd["rawOutput"] = {"tasks": [{"id": "9", "task_description": "z", "completed": True}]}
        snap = parse_todo_snapshot(upd, _name_cache())
        assert snap is not None
        assert snap["tasks"] == [{"id": "9", "text": "z", "completed": True}]

    def test_non_bool_completed_is_coerced(self) -> None:
        snap = parse_todo_snapshot(
            _update([{"id": "1", "task_description": "x", "completed": "yes"}]), _name_cache()
        )
        assert snap is not None
        assert snap["tasks"][0]["completed"] is True

    def test_missing_id_falls_back_to_position(self) -> None:
        snap = parse_todo_snapshot(
            _update([{"task_description": "a"}, {"task_description": "b"}]), _name_cache()
        )
        assert snap is not None
        assert [t["id"] for t in snap["tasks"]] == ["1", "2"]

    def test_non_dict_task_entries_skipped(self) -> None:
        snap = parse_todo_snapshot(
            _update(["garbage", {"id": "2", "task_description": "ok", "completed": False}]),  # type: ignore[list-item]
            _name_cache(),
        )
        assert snap is not None
        assert [t["text"] for t in snap["tasks"]] == ["ok"]

    def test_task_count_is_capped(self) -> None:
        many = [
            {"id": str(i), "task_description": f"t{i}", "completed": False}
            for i in range(TODO_TASKS_MAX + 50)
        ]
        snap = parse_todo_snapshot(_update(many), _name_cache())
        assert snap is not None
        assert len(snap["tasks"]) == TODO_TASKS_MAX

    def test_task_text_is_capped(self) -> None:
        snap = parse_todo_snapshot(
            _update(
                [{"id": "1", "task_description": "x" * (TODO_TEXT_MAX + 500), "completed": False}]
            ),
            _name_cache(),
        )
        assert snap is not None
        assert len(snap["tasks"][0]["text"]) == TODO_TEXT_MAX

    def test_credentials_in_task_text_are_redacted(self) -> None:
        """Task text is agent-authored free text that reaches the browser."""
        secret = "AKIAIOSFODNN7EXAMPLE"
        snap = parse_todo_snapshot(
            _update([{"id": "1", "task_description": f"use key {secret}", "completed": False}]),
            _name_cache(),
        )
        assert snap is not None
        assert secret not in snap["tasks"][0]["text"]

    def test_non_dict_update_is_none(self) -> None:
        assert parse_todo_snapshot("nope") is None  # type: ignore[arg-type]


class TestTwoFrameDispatch:
    """The wire order a live kiro-cli sends: identity first, result second.

    Every test above hands the cached name to the parser directly. These feed
    the two real frames through the dispatcher instead, so the cache is filled
    the way the runtime fills it, which is the only thing that proves a live
    todo_list call still reaches the task panel and the crew log.
    """

    def test_call_then_result_emits_todo_update(self) -> None:
        caches = _caches()
        parse_session_update(_call(), **caches)
        events = parse_session_update(
            _update([{"id": "1", "task_description": "read config", "completed": False}]),
            **caches,
        )
        todo_events = [e for e in events if e.kind == EVENT_TODO_UPDATE]
        assert len(todo_events) == 1
        assert todo_events[0].todo is not None
        assert todo_events[0].todo["tasks"][0]["text"] == "read config"

    def test_call_then_result_emits_todo_update_under_a_scope(self) -> None:
        """A runtime hosting many sessions scopes its caches; the parse follows."""
        caches = _caches(cache_scope="sess-1")
        parse_session_update(_call(), **caches)
        events = parse_session_update(_update([{"task_description": "a"}]), **caches)
        assert [e.kind for e in events].count(EVENT_TODO_UPDATE) == 1

    def test_result_without_its_call_frame_emits_nothing(self) -> None:
        """A result whose call frame this dispatcher never saw stays unidentified."""
        caches = _caches()
        events = parse_session_update(_update([{"task_description": "a"}]), **caches)
        assert not [e for e in events if e.kind == EVENT_TODO_UPDATE]


class TestTodoEventEmission:
    """parse_session_update: the todo event is ADDITIVE, not a swallow."""

    def test_emits_todo_update_event(self) -> None:
        caches = _caches()
        parse_session_update(_call(), **caches)
        events = parse_session_update(
            _update([{"id": "1", "task_description": "a", "completed": False}]),
            **caches,
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
        caches = _caches()
        parse_session_update(_call(), **caches)
        events = parse_session_update(
            _update([{"id": "1", "task_description": "a", "completed": False}]),
            **caches,
        )
        kinds = [e.kind for e in events]
        assert "tool_result" in kinds
        assert "tool_call_update" in kinds

    def test_non_todo_update_emits_no_todo_event(self) -> None:
        caches = _caches()
        call = _call()
        call["_meta"] = {"kiro": {"toolName": "execute_bash"}}
        parse_session_update(call, **caches)
        events = parse_session_update(_update([]), **caches)
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
        slot._todo = {
            "description": "",
            "tasks": ["garbage", {"id": "1", "text": "a", "completed": True}],
        }
        payload = slot.todo_payload()
        assert payload is not None
        assert payload["total"] == 1
        assert payload["completed"] == 1
