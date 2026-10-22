"""Row metadata reaches the composing agent and matches real public fold rows."""

from types import SimpleNamespace

from kiro_crew import dashboard_types, mcp_panel
from kiro_crew.crew_log import projection_crewmate as crewmate
from kiro_crew.crew_log import projection_workstreams as workstreams


def test_mcp_preserves_row_keys_and_row_names_from_the_catalog():
    catalog = dashboard_types.describe({})
    text = mcp_panel._render_types(catalog)
    for fold in catalog["types"]:
        properties = fold["shape"].get("properties", {})
        for name, shape in properties.items():
            if "row_keys" not in shape:
                continue
            line = next(
                line
                for line in text.splitlines()
                if f"{name}: " in line and "row_keys: " + ", ".join(shape["row_keys"]) in line
            )
            assert "names_the_row: " + shape["names_the_row"] in line
    assert "items[].tasks: array" in text
    assert "items[].series: array" in text
    assert "items[].tasks[].events: array" in text
    assert "nested rows (script only)" in text
    assert "not cumulative" in text


def test_goal_and_waiting_metadata_match_folded_rows():
    metadata = dashboard_types._row_keys()
    goal = crewmate._goal_start()
    crewmate._goal_step(
        goal,
        SimpleNamespace(
            type=crewmate.GOAL_ENTRY_TYPE,
            time=1,
            data={
                "goals": [{"text": "Example"}],
                "milestones": [{"text": "Check", "target": "1", "current": "met"}],
            },
        ),
    )
    rendered = crewmate._goal_render(goal)
    for field in ("goals", "milestones"):
        assert set(metadata[f"goal.{field}"]["row_keys"]) == set(rendered[field][0])
    waiting = crewmate._waiting_start()
    crewmate._waiting_step(
        waiting,
        SimpleNamespace(
            type=crewmate.WAITING_ENTRY_TYPE,
            time=1,
            data={"items": [{"text": "Choose", "options": ["Go"]}]},
        ),
    )
    row = crewmate._waiting_render(waiting)["items"][0]
    assert set(metadata["waiting.items"]["row_keys"]) == set(row)


def test_workstream_metadata_matches_board_task_series_and_event_rows():
    state = workstreams._workstreams_start()
    board = workstreams._workstreams_new_board("board", "slot", "generation")
    task = workstreams._workstreams_new_item("item")
    board["items"]["item"] = task
    board["order"] = ["item"]
    board["series"] = {"2026-10-01T00:00Z": {"credits": 1.0, "accepted": 0}}
    state["boards"]["board"] = board
    state["order"] = ["board"]
    state["events"] = [
        {
            "board": "board",
            "item_id": "item",
            "at": "now",
            "kind": "worker",
            "status": "progress",
            "text": "Checking",
        }
    ]
    rendered = workstreams._workstreams_render(state)
    row = rendered["items"][0]
    metadata = dashboard_types._row_keys()
    shape = metadata["workstreams.items"]
    assert set(shape["row_keys"]) == set(row)
    for name in ("tasks", "series"):
        assert set(shape["nested_rows"][name]["row_keys"]) == set(row[name][0])
    event = shape["nested_rows"]["tasks"]["nested_rows"]["events"]
    assert set(event["row_keys"]) == set(row["tasks"][0]["events"][0])
    assert set(metadata["workstreams.series"]["row_keys"]) == set(rendered["series"][0])


def test_nested_row_hints_keep_their_parent_path_on_one_line():
    node = {
        "type": "array",
        "row_keys": ["goal", "tasks"],
        "names_the_row": "goal",
        "nested_rows": {
            "tasks": {
                "row_keys": ["title", "events"],
                "names_the_row": "title",
                "nested_rows": {"events": {"row_keys": ["at", "text"]}},
            },
        },
    }
    lines = mcp_panel._flatten(node, "items")
    assert len(lines) == 1
    assert "items: array" in lines[0]
    assert "nested rows (script only): {items[].tasks: array" in lines[0]
    assert "nested rows (script only): {items[].tasks[].events: array" in lines[0]
    assert "[row_keys: at, text]" in lines[0]
    assert "row.events:" not in lines[0]
