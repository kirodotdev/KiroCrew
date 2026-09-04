"""Context-group flags across the MCP -> HTTP -> spawn boundary.

The unit tests in ``test_subagent_context_group_plumbing.py`` start at
``SubagentManager.spawn``, which skips the whole chain the model actually calls
through: the ``spawn_run`` / ``spawn_sub_agents`` tool handlers build an HTTP
body, ``POST /api/spawn`` validates it, and only then does ``spawn`` see the
flags. Each hop is a place a group can be silently dropped or silently kept, so
each hop is asserted here rather than trusted.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.validation import SPAWN_RUN_SCHEMA, ValidationError, validate_tool_args


def _posted(args: dict[str, Any], tool: str = "spawn_run") -> list[dict]:
    """Run a spawn tool call and return the bodies it POSTed to /api/spawn."""
    from kiro_crew import mcp_core

    bodies: list[dict] = []

    def _fake_post(path: str, body: dict) -> dict:
        if path == "/api/spawn":
            bodies.append(body)
        return {"id": "a1"}

    with (
        patch.object(mcp_core, "_post", side_effect=_fake_post),
        patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:chat-1"),
        patch.object(mcp_core, "sel", MagicMock()),
    ):
        mcp_core._call_tool_inner(tool, args)
    return bodies


class TestSpawnRunToolForwarding:
    """The omit-when-true wire contract: only a withheld group is sent."""

    def test_withheld_group_is_sent_as_false(self):
        bodies = _posted({"task": "x", "include_memory": False})
        assert len(bodies) == 1
        assert bodies[0]["include_memory"] is False

    def test_kept_groups_are_omitted_from_the_body(self):
        """Absent means true at the receiver, so true is not worth a wire field."""
        bodies = _posted({"task": "x", "include_memory": False})
        assert "include_lessons" not in bodies[0]
        assert "include_project" not in bodies[0]

    def test_nothing_sent_when_no_flag_is_passed(self):
        bodies = _posted({"task": "x"})
        assert "include_memory" not in bodies[0]
        assert "include_lessons" not in bodies[0]
        assert "include_project" not in bodies[0]

    def test_explicit_true_is_not_forwarded_as_false(self):
        bodies = _posted({"task": "x", "include_memory": True})
        assert bodies[0].get("include_memory", True) is True

    def test_flags_apply_to_every_member_of_a_batch(self):
        bodies = _posted({"tasks": ["t1", "t2", "t3"], "include_lessons": False})
        assert len(bodies) == 3
        assert all(b["include_lessons"] is False for b in bodies)


class TestSpawnSubAgentsToolForwarding:
    def test_flags_are_batch_wide(self):
        bodies = _posted(
            {
                "agents": [{"prompt": "a"}, {"prompt": "b"}],
                "include_project": False,
            },
            tool="spawn_sub_agents",
        )
        assert len(bodies) == 2
        assert all(b["include_project"] is False for b in bodies)

    def test_absent_flags_are_not_sent_as_false(self):
        bodies = _posted(
            {"agents": [{"prompt": "a"}]},
            tool="spawn_sub_agents",
        )
        assert "include_memory" not in bodies[0]


class TestSchemaCoercion:
    """A group must never be withheld by a value the caller did not mean."""

    def test_explicit_null_resolves_to_kept_not_withheld(self):
        """JSON null means "unset"; it must not read as a withheld group."""
        cleaned = validate_tool_args({"task": "x", "include_memory": None}, SPAWN_RUN_SCHEMA)
        assert cleaned["include_memory"] is True

    def test_absent_field_cleans_to_true(self):
        cleaned = validate_tool_args({"task": "x"}, SPAWN_RUN_SCHEMA)
        assert cleaned["include_memory"] is True
        assert cleaned["include_lessons"] is True
        assert cleaned["include_project"] is True

    def test_explicit_false_survives(self):
        cleaned = validate_tool_args({"task": "x", "include_memory": False}, SPAWN_RUN_SCHEMA)
        assert cleaned["include_memory"] is False

    @pytest.mark.parametrize("bad", ["false", "true", "", 0, 1, 2.5, [], {}])
    def test_non_bool_is_rejected_rather_than_coerced(self, bad):
        """A truthy/falsey string must not silently decide a group's fate."""
        with pytest.raises(ValidationError):
            validate_tool_args({"task": "x", "include_memory": bad}, SPAWN_RUN_SCHEMA)


class TestApiSpawnHandler:
    """POST /api/spawn is the only door into spawn() — it must not lose a flag."""

    def _request(self, body: dict) -> tuple[Any, MagicMock]:
        mgr = MagicMock()
        mgr.spawn.return_value = SimpleNamespace(id="a1", done=False, error="")
        mgr.max_concurrent = 4
        state = SimpleNamespace(subagents=mgr)
        request = MagicMock()
        request.app = {"state": state}

        async def _json() -> dict:
            return body

        request.json = _json
        return request, mgr

    @pytest.mark.asyncio
    async def test_withheld_group_reaches_spawn(self):
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        request, mgr = self._request({"task": "x", "include_memory": False})
        await api_spawn(request)
        assert mgr.spawn.call_args.kwargs["include_memory"] is False
        assert mgr.spawn.call_args.kwargs["include_lessons"] is True

    @pytest.mark.asyncio
    async def test_absent_flags_reach_spawn_as_true(self):
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        request, mgr = self._request({"task": "x"})
        await api_spawn(request)
        kwargs = mgr.spawn.call_args.kwargs
        assert (kwargs["include_memory"], kwargs["include_lessons"]) == (True, True)
        assert kwargs["include_project"] is True

    @pytest.mark.asyncio
    async def test_null_does_not_withhold(self):
        """The regression this guards: bool(None) would have read as withheld."""
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        request, mgr = self._request({"task": "x", "include_memory": None})
        await api_spawn(request)
        assert mgr.spawn.call_args.kwargs["include_memory"] is True
