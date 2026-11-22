"""An agent can create, find and list a ``canvas`` artifact through its own tools.

``artifact_save`` and ``artifact_list`` are an agent's only paths to the store, and
each advertises a closed ``kind`` enum. A kind the store accepts but the enum omits is
refused ``kind: invalid format`` before the store sees the call, so the canvas kind has
to be named in both enums, the schema's kind pattern and ``ALLOWED_KINDS`` alike.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew.artifact_store.rules import ALLOWED_KINDS, USER_SELECTABLE_KINDS
from kiro_crew.artifacts import ArtifactStore, ArtifactValidationError
from kiro_crew.validation import (
    _ARTIFACT_KIND_RE,
    ARTIFACT_LIST_SCHEMA,
    ARTIFACT_SAVE_SCHEMA,
    validate_tool_args,
)

BOARD = json.dumps(
    {
        "kirocanvas": 1,
        "nodes": [
            {
                "id": "a",
                "type": "text",
                "x": 0,
                "y": 0,
                "width": 200,
                "height": 120,
                "text": "Plan",
            },
            {
                "id": "b",
                "type": "text",
                "x": 320,
                "y": 0,
                "width": 200,
                "height": 120,
                "text": "Ship",
            },
        ],
        "edges": [{"id": "e", "fromNode": "a", "toNode": "b"}],
    }
)


def _tool_kind_enum(tool: str) -> list[str]:
    from kiro_crew.mcp_tools.artifacts import schemas

    for descriptor in schemas():
        if descriptor["name"] == tool:
            enum = descriptor["inputSchema"]["properties"]["kind"]["enum"]
            assert isinstance(enum, list) and enum
            return enum
    raise AssertionError(f"no {tool} descriptor in the artifacts tool schemas")


def _save_through_the_tool(
    store: ArtifactStore, args: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Run the real ``artifact_save`` handler with *store* behind the gateway POST."""
    from kiro_crew import mcp_core
    from kiro_crew.mcp_tools.artifacts import artifact_save

    posted: dict[str, Any] = {}

    def _fake_post(path: str, body: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        assert path == "/api/artifacts", path
        posted.update(body or {})
        try:
            art = store.create(
                name=posted["name"], kind=posted.get("kind"), content=posted["content"]
            )
        except ArtifactValidationError as exc:
            return {"error": str(exc)}
        return {"slug": art.slug, "version": art.version, "name": art.name, "kind": art.kind}

    with (
        patch.object(mcp_core, "_post", side_effect=_fake_post),
        patch.object(mcp_core, "_resolve_session_key", return_value=""),
    ):
        return artifact_save("artifact_save", args), posted


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(root=tmp_path / "artifacts")


def test_every_list_between_the_agent_and_the_store_names_canvas() -> None:
    assert "canvas" in ALLOWED_KINDS
    assert "canvas" not in USER_SELECTABLE_KINDS
    assert _ARTIFACT_KIND_RE.fullmatch("canvas") is not None
    assert "canvas" in _tool_kind_enum("artifact_save")
    assert "canvas" in _tool_kind_enum("artifact_list")


def test_the_save_and_list_schemas_take_the_kind() -> None:
    saved = validate_tool_args(
        {"name": "Board", "content": BOARD, "kind": "canvas"}, ARTIFACT_SAVE_SCHEMA
    )
    assert saved["kind"] == "canvas"
    listed = validate_tool_args({"kind": "canvas"}, ARTIFACT_LIST_SCHEMA)
    assert listed["kind"] == "canvas"


def test_a_board_saved_through_the_tool_is_stored_as_a_canvas(store: ArtifactStore) -> None:
    out, posted = _save_through_the_tool(
        store, {"name": "Board", "content": BOARD, "kind": "canvas"}
    )
    # The handler forwards the kind, so inference never re-types the JSON body as
    # plain ``json``, which the canvas editor would not open.
    assert posted["kind"] == "canvas"
    assert out.startswith("Saved artifact: slug=")
    stored = store.list(kind="canvas")
    assert len(stored) == 1
    assert json.loads(store.get(stored[0].slug).content)["nodes"][0]["text"] == "Plan"
