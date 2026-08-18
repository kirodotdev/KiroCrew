"""Unit tests for the kanban task board data model and store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.kanban.backend.store import (
    EXECUTION_RESULTS,
    BoardUnreadableError,
    KanbanStore,
    attach_session_key,
    create_task,
    move_task,
    settle_execution,
    start_execution,
)

# ── Pure state transition tests ──


class TestUnreadableBoard:
    """An unreadable board must never be silently replaced with an empty one.

    Every mutation is read-then-write, so a board that failed to parse used to
    come back as "no tasks" and the next write destroyed every card on it. The
    read now refuses, which leaves the file intact and recoverable.
    """

    def test_a_corrupt_board_refuses_to_load(self, tmp_path):
        store = KanbanStore(tmp_path / "kanban")
        store.add_task(create_task(title="keep me"))
        store._board_path.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(BoardUnreadableError):
            store.load()

    def test_a_corrupt_board_is_not_overwritten_by_a_mutation(self, tmp_path):
        store = KanbanStore(tmp_path / "kanban")
        store.add_task(create_task(title="keep me"))
        good = store._board_path.read_text(encoding="utf-8")
        store._board_path.write_text("{truncated", encoding="utf-8")

        with pytest.raises(BoardUnreadableError):
            store.add_task(create_task(title="should not land"))

        # The damaged file is still exactly as found — not replaced by a board
        # containing only the new task.
        assert store._board_path.read_text(encoding="utf-8") == "{truncated"
        # And restoring it brings the original task back.
        store._board_path.write_text(good, encoding="utf-8")
        assert [t.title for t in store.load()] == ["keep me"]

    def test_a_non_object_board_refuses_to_load(self, tmp_path):
        store = KanbanStore(tmp_path / "kanban")
        store._board_path.parent.mkdir(parents=True, exist_ok=True)
        store._board_path.write_text("[]", encoding="utf-8")
        with pytest.raises(BoardUnreadableError):
            store.load()

    def test_a_missing_board_is_genuinely_empty(self, tmp_path):
        """Absent is not corrupt — a fresh board still reads as no tasks."""
        store = KanbanStore(tmp_path / "kanban")
        assert store.load() == []


class TestCreateTask:
    def test_creates_with_defaults(self) -> None:
        task = create_task(title="Hello")
        assert task.title == "Hello"
        assert task.status == "todo"
        assert task.id  # non-empty
        assert task.created_at > 0
        assert task.updated_at > 0
        assert task.executions == []
        assert task.tags == []
        assert task.priority == "medium"

    def test_creates_with_all_fields(self) -> None:
        task = create_task(
            title="Test",
            description="desc",
            prompt="do something",
            status="backlog",
            tags=["ops", "auto"],
            priority="high",
        )
        assert task.status == "backlog"
        assert task.description == "desc"
        assert task.prompt == "do something"
        assert task.tags == ["ops", "auto"]
        assert task.priority == "high"

    def test_invalid_status_defaults_to_todo(self) -> None:
        task = create_task(title="X", status="invalid")
        assert task.status == "todo"

    def test_invalid_priority_defaults_to_medium(self) -> None:
        task = create_task(title="X", priority="ultra")
        assert task.priority == "medium"


class TestMoveTask:
    def test_moves_to_valid_status(self) -> None:
        task = create_task(title="T")
        moved = move_task(task, "backlog")
        assert moved.status == "backlog"
        assert moved.id == task.id
        assert moved.updated_at >= task.updated_at

    def test_rejects_invalid_status(self) -> None:
        task = create_task(title="T")
        with pytest.raises(ValueError, match="Invalid status"):
            move_task(task, "bogus")


class TestExecution:
    def test_start_execution(self) -> None:
        task = create_task(title="Run me", prompt="do it")
        new_task, execution = start_execution(task)

        assert new_task.status == "running"
        assert len(new_task.executions) == 1
        assert new_task.executions[0].id == execution.id
        assert execution.started_at > 0
        assert execution.result is None

    def test_settle_execution_succeeded(self) -> None:
        task = create_task(title="T")
        running, execution = start_execution(task)
        settled = settle_execution(running, execution.id, "succeeded")

        assert settled.status == "done"
        assert settled.executions[0].result == "succeeded"
        assert settled.executions[0].ended_at is not None

    def test_settle_execution_failed(self) -> None:
        task = create_task(title="T")
        running, execution = start_execution(task)
        settled = settle_execution(running, execution.id, "failed", "something broke")

        assert settled.status == "failed"
        assert settled.executions[0].result == "failed"
        assert settled.executions[0].error == "something broke"

    def test_settle_execution_cancelled(self) -> None:
        task = create_task(title="T")
        running, execution = start_execution(task)
        settled = settle_execution(running, execution.id, "cancelled")
        assert settled.status == "todo"  # goes back to todo
        assert settled.executions[0].result == "cancelled"

    def test_attach_session_key(self) -> None:
        task = create_task(title="T")
        running, execution = start_execution(task)
        attached = attach_session_key(running, execution.id, "sess-abc")

        assert attached.executions[0].session_key == "sess-abc"


# ── Store tests ──


class TestKanbanStore:
    @pytest.fixture
    def store(self, tmp_path: Path) -> KanbanStore:
        return KanbanStore(root=tmp_path / "kanban")

    def test_load_empty(self, store: KanbanStore) -> None:
        tasks = store.load()
        assert tasks == []

    def test_a_write_leaves_no_partial_board_behind(self, store: KanbanStore) -> None:
        """The board is replaced by rename, so a reader never sees a half-written file.

        A truncating write that died mid-flush would leave invalid JSON, which
        ``_read`` reports as an empty board — and the next mutation would persist
        that emptiness over every task.
        """
        store.add_task(create_task(title="Survivor"))
        board = store._board_path
        assert json.loads(board.read_text(encoding="utf-8"))["tasks"], "board must be readable"
        # No temp artefact is left in the board's directory once the rename lands.
        strays = [
            p.name
            for p in board.parent.iterdir()
            if p.name not in {board.name, store._lock_path.name}
        ]
        assert strays == [], f"unexpected leftovers: {strays}"

    def test_add_and_load(self, store: KanbanStore) -> None:
        task = create_task(title="First task")
        store.add_task(task)

        tasks = store.load()
        assert len(tasks) == 1
        assert tasks[0].id == task.id
        assert tasks[0].title == "First task"

    def test_get_task(self, store: KanbanStore) -> None:
        task = create_task(title="Find me")
        store.add_task(task)

        found = store.get_task(task.id)
        assert found is not None
        assert found.title == "Find me"

        not_found = store.get_task("nonexistent")
        assert not_found is None

    def test_update_task(self, store: KanbanStore) -> None:
        task = create_task(title="Before")
        store.add_task(task)

        result = store.update_task(task.id, lambda t: move_task(t, "backlog"))
        assert result is not None
        assert result.status == "backlog"

        # Verify persisted
        loaded = store.get_task(task.id)
        assert loaded is not None
        assert loaded.status == "backlog"

    def test_delete_task(self, store: KanbanStore) -> None:
        task = create_task(title="Delete me")
        store.add_task(task)

        assert store.delete_task(task.id) is True
        assert store.get_task(task.id) is None
        assert store.delete_task(task.id) is False  # already gone

    def test_persistence_across_instances(self, tmp_path: Path) -> None:
        root = tmp_path / "kanban"
        store1 = KanbanStore(root=root)
        task = create_task(title="Persistent")
        store1.add_task(task)

        # New store instance reads the same file
        store2 = KanbanStore(root=root)
        tasks = store2.load()
        assert len(tasks) == 1
        assert tasks[0].title == "Persistent"

    def test_an_invalid_record_holds_the_whole_board_back(self, tmp_path: Path) -> None:
        """One unreadable record refuses the load; it is not dropped around.

        Reading the valid cards and skipping the rest looks resilient and is
        destructive: the load is the first half of every read-then-write
        mutation, so the next move would persist a board with the skipped
        records -- and their execution history -- gone for good.
        """
        root = tmp_path / "kanban"
        root.mkdir(parents=True)
        board_path = root / "board.json"

        board_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "tasks": [
                        {"id": "good", "title": "Valid task", "status": "todo"},
                        {"id": "", "title": ""},  # invalid: empty id and title
                        {"not_a_task": True},  # invalid: no id
                    ],
                }
            )
        )

        store = KanbanStore(root=root)
        with pytest.raises(BoardUnreadableError):
            store.load()
        # The file is untouched, so a human can repair the two broken records.
        assert len(json.loads(board_path.read_text())["tasks"]) == 3


class TestRefiningFlag:
    """The ``refining`` flag says "the background namer has not answered yet".

    It is read straight off a board file the user can edit, so a non-bool must
    not be able to pin a card in a perpetually-refining state.
    """

    def test_a_new_task_is_not_refining_by_default(self):
        assert create_task(title="Plain task").refining is False

    def test_the_flag_survives_a_move_and_an_execution(self):
        task = create_task(title="Named later", prompt="p", refining=True)
        assert move_task(task, "backlog").refining is True
        started, _ = start_execution(task)
        assert started.refining is True

    @pytest.mark.parametrize("raw_value", ["yes", 1, None, [], {}, "false"])
    def test_only_a_literal_true_reads_back_as_refining(self, tmp_path: Path, raw_value):
        root = tmp_path / "kanban"
        root.mkdir(parents=True)
        (root / "board.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "tasks": [
                        {"id": "t1", "title": "A task", "status": "todo", "refining": raw_value}
                    ],
                }
            )
        )
        tasks = KanbanStore(root=root).load()
        assert tasks[0].refining is False

    def test_a_board_file_written_before_the_flag_existed_still_loads(self, tmp_path: Path):
        root = tmp_path / "kanban"
        root.mkdir(parents=True)
        (root / "board.json").write_text(
            json.dumps({"version": 1, "tasks": [{"id": "t1", "title": "Old", "status": "todo"}]})
        )
        tasks = KanbanStore(root=root).load()
        assert tasks[0].refining is False


class TestSupersededExecutionDoesNotMoveStatus:
    """A finished run may only move the card it is still the current run for.

    The sequence that broke it: a card is running, a human settles it by hand,
    the card is started again, and only THEN does the first run's watcher finish.
    Settling wrote ``status`` unconditionally, so the stale outcome landed on top
    of the new run's ``running`` and the board showed a finished card for work
    that was still in flight. The execution row is still recorded either way — a
    run's own outcome is a fact about that run.
    """

    def test_a_superseded_watcher_leaves_the_new_runs_status_alone(self) -> None:
        first_running, first = start_execution(create_task(title="T"))
        # The human settles it, then starts it again: `second` is now the run the
        # card's status belongs to.
        settled_by_hand = settle_execution(first_running, first.id, "succeeded")
        second_running, second = start_execution(settled_by_hand)
        assert second_running.status == "running"

        # The ORIGINAL run's watcher finally reports, long after it was superseded.
        after = settle_execution(second_running, first.id, "failed", "late failure")

        assert after.status == "running", "a superseded run must not move the card"
        assert after.executions[0].result == "failed"
        assert after.executions[0].error == "late failure"
        assert after.executions[1].id == second.id
        assert after.executions[1].result is None

    def test_the_latest_unsettled_execution_still_moves_the_card(self) -> None:
        # The guard must not cost the normal case: the current run still settles.
        first_running, first = start_execution(create_task(title="T"))
        done = settle_execution(first_running, first.id, "succeeded")
        second_running, second = start_execution(done)

        after = settle_execution(second_running, second.id, "failed", "boom")

        assert after.status == "failed"
        assert after.executions[1].result == "failed"


class TestMalformedTaskCollection:
    """A board whose ``tasks`` is the wrong SHAPE is corrupt, not empty.

    ``for item in raw.get("tasks", [])`` raised ``TypeError`` on a JSON ``null``
    and ``AttributeError`` on a non-object entry, which surfaced as an HTTP 500.
    Treating either as an empty board would be worse than the crash: every
    mutation is read-then-write, so the next write would persist that emptiness
    over every card. Both now refuse, leaving the file intact.
    """

    def _board(self, tmp_path: Path, payload: object) -> KanbanStore:
        root = tmp_path / "kanban"
        root.mkdir(parents=True)
        (root / "board.json").write_text(json.dumps(payload))
        return KanbanStore(root=root)

    def test_a_null_tasks_collection_refuses_to_load(self, tmp_path: Path):
        store = self._board(tmp_path, {"version": 1, "tasks": None})
        with pytest.raises(BoardUnreadableError):
            store.load()

    def test_a_non_array_tasks_collection_refuses_to_load(self, tmp_path: Path):
        store = self._board(tmp_path, {"version": 1, "tasks": {"t1": {"title": "T"}}})
        with pytest.raises(BoardUnreadableError):
            store.load()

    def test_a_task_entry_that_is_not_an_object_refuses_to_load(self, tmp_path: Path):
        store = self._board(tmp_path, {"version": 1, "tasks": ["not-a-task"]})
        with pytest.raises(BoardUnreadableError):
            store.load()

    def test_the_refusal_leaves_the_file_on_disk(self, tmp_path: Path):
        store = self._board(tmp_path, {"version": 1, "tasks": None})
        with pytest.raises(BoardUnreadableError):
            store.load()
        # Recoverable by hand: the point of refusing rather than reading empty.
        assert (tmp_path / "kanban" / "board.json").exists()

    def test_a_task_missing_its_id_refuses_to_load(self, tmp_path: Path):
        """A readable record the parser cannot accept is corruption, not absence.

        Skipping it dropped one card -- and its whole execution history -- from
        the load, and the next unrelated move then wrote that omission to disk.
        """
        store = self._board(tmp_path, {"version": 1, "tasks": [{"title": "no id"}]})
        with pytest.raises(BoardUnreadableError):
            store.load()

    def test_a_task_missing_its_title_refuses_to_load(self, tmp_path: Path):
        store = self._board(tmp_path, {"version": 1, "tasks": [{"id": "t1"}]})
        with pytest.raises(BoardUnreadableError):
            store.load()

    def test_a_task_with_an_unparsable_field_refuses_to_load(self, tmp_path: Path):
        store = self._board(
            tmp_path,
            {"version": 1, "tasks": [{"id": "t1", "title": "T", "created_at": "not-a-number"}]},
        )
        with pytest.raises(BoardUnreadableError):
            store.load()

    def test_a_valid_sibling_is_not_written_over_the_broken_record(self, tmp_path: Path):
        """The whole board is held back, so a repair still sees every card."""
        store = self._board(
            tmp_path,
            {
                "version": 1,
                "tasks": [
                    {"id": "t1", "title": "keeper", "status": "todo"},
                    {"title": "broken"},
                ],
            },
        )
        with pytest.raises(BoardUnreadableError):
            store.load()
        on_disk = json.loads((tmp_path / "kanban" / "board.json").read_text())
        assert [t.get("title") for t in on_disk["tasks"]] == ["keeper", "broken"]

    def test_a_non_string_title_refuses_to_load(self, tmp_path: Path):
        """A wrong TYPE is corruption too, not just a missing value.

        The value is handed to the UI as-is, where the search filter calls
        ``.toLowerCase()`` on it and takes the whole board down.
        """
        store = self._board(tmp_path, {"version": 1, "tasks": [{"id": "t1", "title": 42}]})
        with pytest.raises(BoardUnreadableError):
            store.load()

    def test_a_non_string_id_refuses_to_load(self, tmp_path: Path):
        store = self._board(tmp_path, {"version": 1, "tasks": [{"id": {"x": 1}, "title": "T"}]})
        with pytest.raises(BoardUnreadableError):
            store.load()

    def test_tags_that_are_not_a_list_refuse_to_load(self, tmp_path: Path):
        store = self._board(
            tmp_path, {"version": 1, "tasks": [{"id": "t1", "title": "T", "tags": "urgent"}]}
        )
        with pytest.raises(BoardUnreadableError):
            store.load()

    def test_a_non_string_tag_refuses_to_load(self, tmp_path: Path):
        """`tags.some(tag => tag.toLowerCase())` in the UI has the same exposure."""
        store = self._board(
            tmp_path, {"version": 1, "tasks": [{"id": "t1", "title": "T", "tags": ["ok", 7]}]}
        )
        with pytest.raises(BoardUnreadableError):
            store.load()

    def test_an_execution_that_is_not_an_object_refuses_to_load(self, tmp_path: Path):
        """A malformed run is refused, not skipped -- skipping erases the run.

        The load is the first half of every mutation, so a discarded execution
        row is gone from the card's history after the next unrelated edit.
        """
        store = self._board(
            tmp_path,
            {"version": 1, "tasks": [{"id": "t1", "title": "T", "executions": ["nope"]}]},
        )
        with pytest.raises(BoardUnreadableError):
            store.load()

    def test_an_execution_with_no_usable_id_refuses_to_load(self, tmp_path: Path):
        store = self._board(
            tmp_path,
            {"version": 1, "tasks": [{"id": "t1", "title": "T", "executions": [{"result": "ok"}]}]},
        )
        with pytest.raises(BoardUnreadableError):
            store.load()

    def test_a_good_record_with_every_field_still_loads(self, tmp_path: Path):
        """The guard rejects wrong types; it must not reject valid boards."""
        store = self._board(
            tmp_path,
            {
                "version": 1,
                "tasks": [
                    {
                        "id": "t1",
                        "title": "T",
                        "description": "d",
                        "prompt": "p",
                        "status": "done",
                        "tags": ["a", "b"],
                        "priority": "high",
                        "executions": [{"id": "e1", "started_at": 1.0, "result": "succeeded"}],
                    }
                ],
            },
        )
        (task,) = store.load()
        assert (task.title, task.tags, task.priority) == ("T", ["a", "b"], "high")
        assert [ex.id for ex in task.executions] == ["e1"]

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("result", {"nested": "object"}),
            ("result", ["a", "list"]),
            ("result", "not-an-outcome"),
            ("error", {"nested": "object"}),
            ("error", 7),
            ("session_key", {"nested": "object"}),
            ("started_at", "not-a-number"),
            ("started_at", True),
            ("ended_at", "not-a-number"),
        ],
    )
    def test_a_malformed_execution_field_refuses_to_load(self, tmp_path, field, value):
        """Every execution field is checked, not just the id.

        These values are rendered straight by the task detail panel, so an
        object where a string belongs arrives at React as a child and takes the
        whole page down. An out-of-vocabulary `result` is corruption too: the
        settle map has no lane for it.
        """
        store = self._board(
            tmp_path,
            {
                "version": 1,
                "tasks": [{"id": "t1", "title": "T", "executions": [{"id": "e1", field: value}]}],
            },
        )
        with pytest.raises(BoardUnreadableError):
            store.load()

    @pytest.mark.parametrize("result", EXECUTION_RESULTS)
    def test_every_real_outcome_still_loads(self, tmp_path, result):
        """The result vocabulary the settle path writes must survive a reload."""
        store = self._board(
            tmp_path,
            {
                "version": 1,
                "tasks": [
                    {
                        "id": "t1",
                        "title": "T",
                        "executions": [{"id": "e1", "started_at": 1, "result": result}],
                    }
                ],
            },
        )
        (task,) = store.load()
        assert task.executions[0].result == result

    def test_an_unsettled_execution_still_loads(self, tmp_path: Path):
        """A running execution has no result yet -- None is not corruption."""
        store = self._board(
            tmp_path,
            {
                "version": 1,
                "tasks": [
                    {
                        "id": "t1",
                        "title": "T",
                        "status": "running",
                        "executions": [{"id": "e1", "started_at": 1, "session_key": "s1"}],
                    }
                ],
            },
        )
        (task,) = store.load()
        assert task.executions[0].result is None
        assert task.executions[0].ended_at is None
