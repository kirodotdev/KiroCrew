"""The ``restart_required`` hint the config PUT and PATCH handlers answer with.

The hint is computed from the schema's ``restart=True`` marks alone, checked first on the
function behind it and then over a real request to each handler.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from config_live_helpers import (  # noqa: F401 -- fixtures are used by name
    cfg_file_fixture,
)

from kiro_crew.config import live
from kiro_crew.config.loader import KiroCrewConfig

# ── the restart hint the dashboard answers with ───────────────────────────


class TestRestartHintIsSchemaDriven:
    """The PUT/PATCH handlers keep no list of boot-only keys; the schema is it.

    ``_changed_paths_need_restart`` is the whole computation behind the PUT's
    ``restart_required`` field, and the PUT's own vocabulary is checked against it
    so a newly added setting cannot quietly start claiming a restart it does not
    need (or hide one it does).
    """

    #: Every key ``PUT /api/config`` can apply, as ``agent.<key>``.
    PUT_KEYS = (
        "subagent_max_turns",
        "subagent_auto_max",
        "max_subagents",
    )

    def test_every_put_settable_field_is_hot(self) -> None:
        from kiro_crew.dashboard.handlers.core import _changed_paths_need_restart

        for key in self.PUT_KEYS:
            assert not _changed_paths_need_restart([f"agent.{key}"]), key
        assert not _changed_paths_need_restart([f"agent.{k}" for k in self.PUT_KEYS])

    def test_a_restart_true_path_flips_the_hint(self) -> None:
        from kiro_crew.dashboard.handlers.core import _changed_paths_need_restart

        for path in (
            "agent.jail",
            "agent.dangerously_skip_permissions",
            "dashboard.url",
            "dashboard.restore_sessions",
            "tunnel.enabled",
        ):
            assert _changed_paths_need_restart([path]), path
        # One restart-only path among live ones is enough to require a restart.
        assert _changed_paths_need_restart(["agent.max_subagents", "tunnel.enabled"])

    def test_an_unchanged_field_never_claims_a_restart(self) -> None:
        """The dashboard sends every setting on each save, so the handler feeds
        only the paths whose value actually MOVED -- an untouched restart=True
        field must not make a live edit report restart_required."""
        from kiro_crew.dashboard.handlers.core import _changed_paths_need_restart

        applied = ["max_subagents", "jail"]
        agent = {"max_subagents": 8, "jail": True}
        before = {"max_subagents": 4, "jail": True}  # jail resent unchanged
        changed = [f"agent.{k}" for k in applied if agent.get(k) != before.get(k)]
        assert changed == ["agent.max_subagents"]
        assert not _changed_paths_need_restart(changed)

    def test_the_hint_and_the_schema_cannot_drift(self) -> None:
        """Whatever the schema marks, the handler agrees with -- no second list."""
        from kiro_crew.config.schema import SCHEMA_REGISTRY, requires_restart
        from kiro_crew.dashboard.handlers.core import _changed_paths_need_restart

        marked = {e.path for e in SCHEMA_REGISTRY if e.requires_restart}
        assert marked, "no restart=True fields in the schema at all"
        for path in marked:
            assert requires_restart(path)
            assert _changed_paths_need_restart([path]), path

        def _has_marked_ancestor(path: str) -> bool:
            parts = path.split(".")
            return any(".".join(parts[:i]) in marked for i in range(1, len(parts)))

        for entry in SCHEMA_REGISTRY:
            if entry.requires_restart or _has_marked_ancestor(entry.path):
                continue
            assert not _changed_paths_need_restart([entry.path]), entry.path

    def test_a_child_of_a_marked_field_inherits_the_mark(self) -> None:
        """``requires_restart`` resolves through ancestors, so a marked container
        covers keys the schema never enumerates (a stub-server name, a jail
        sub-key) -- otherwise a nested edit would report itself hot."""
        from kiro_crew.config.schema import requires_restart

        assert requires_restart("mcp_gateway.stub_servers")
        assert requires_restart("mcp_gateway.stub_servers.some-server")
        assert requires_restart("agent.jail.anything")
        assert not requires_restart("mcp_gateway")  # the parent itself is hot


# ── the hint over a real request ──────────────────────────────────────────


class TestRestartRequiredOverHttp:
    """The same schema hint, but answered by the endpoint the dashboard calls.

    ``_changed_paths_need_restart`` is unit-tested above; this drives the two
    doors in front of it. That matters because the value the USER sees is the
    JSON field: the four keys ``PUT`` can apply are hot, the whole point of the
    watcher is that they never report a restart, and only a real request proves
    the response says so.

    ``PATCH`` is the other door and deliberately carries no ``restart_required``
    at all: every path in its allow-list is hot, so a hint there would be a
    field that is always ``False``.
    """

    @staticmethod
    def _app():
        from aiohttp import web
        from dashboard_owner_helpers import as_owner

        from kiro_crew.dashboard import handlers

        app = web.Application()
        app.router.add_put("/api/config/kirocrew", handlers.api_kirocrew_config)
        app.router.add_patch("/api/config/kirocrew", handlers.api_kirocrew_config_patch)
        # Both doors are owner-gated, so the restart hint is only reached as owner.
        return as_owner(app)

    @pytest.fixture
    def client_ctx(self, cfg_file: Path):
        from unittest.mock import MagicMock

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.sel", return_value=MagicMock()):
            yield lambda: TestClient(TestServer(self._app()))

    @pytest.mark.asyncio
    async def test_put_a_hot_field_reports_no_restart(self, client_ctx) -> None:
        """The regression pin: a live field must not ask the user to restart."""
        async with client_ctx() as c:
            resp = await c.put("/api/config/kirocrew", json={"agent": {"max_subagents": 8}})
            assert resp.status == 200
            body = await resp.json()
        assert body["restart_required"] is False

    @pytest.mark.asyncio
    async def test_put_several_hot_fields_at_once_reports_no_restart(
        self, client_ctx, cfg_file: Path
    ) -> None:
        async with client_ctx() as c:
            resp = await c.put(
                "/api/config/kirocrew",
                json={"agent": {"max_subagents": 8, "subagent_max_turns": 50}},
            )
            assert resp.status == 200
            body = await resp.json()
        assert body["restart_required"] is False
        saved = json.loads(cfg_file.read_text(encoding="utf-8"))["agent"]
        assert (saved["max_subagents"], saved["subagent_max_turns"]) == (8, 50)

    @pytest.mark.asyncio
    async def test_put_a_field_resent_unchanged_reports_no_restart(self, client_ctx) -> None:
        """The dashboard resends every setting on each save and enables Save when
        ANY one is dirty, so "was applied" is wider than "was changed" -- the hint
        must follow the narrower one. Marking the path ``restart=True`` is what
        makes the difference observable: the first PUT moves the value and reports
        a restart, the identical second one moves nothing and must not."""
        with patch(
            "kiro_crew.config.schema.requires_restart",
            side_effect=lambda p: p == "agent.max_subagents",
        ):
            async with client_ctx() as c:
                first = await c.put("/api/config/kirocrew", json={"agent": {"max_subagents": 8}})
                assert first.status == 200
                assert (await first.json())["restart_required"] is True
                again = await c.put("/api/config/kirocrew", json={"agent": {"max_subagents": 8}})
                assert again.status == 200
                body = await again.json()
        assert body["restart_required"] is False

    @pytest.mark.asyncio
    async def test_put_reports_a_restart_when_the_schema_marks_the_moved_path(
        self, client_ctx
    ) -> None:
        """The False above is the SCHEMA's answer, not a constant: mark the very
        same path ``restart=True`` and the same request flips to True."""
        with patch(
            "kiro_crew.config.schema.requires_restart",
            side_effect=lambda p: p == "agent.max_subagents",
        ):
            async with client_ctx() as c:
                resp = await c.put("/api/config/kirocrew", json={"agent": {"max_subagents": 8}})
                assert resp.status == 200
                body = await resp.json()
        assert body["restart_required"] is True

    @pytest.mark.asyncio
    async def test_patch_a_hot_field_carries_no_restart_hint(self, client_ctx) -> None:
        async with client_ctx() as c:
            resp = await c.patch(
                "/api/config/kirocrew", json={"path": "agent.model", "value": "model-patched"}
            )
            assert resp.status == 200
            body = await resp.json()
        assert "restart_required" not in body
        assert body["agent"]["model"] == "model-patched"

    @pytest.mark.asyncio
    async def test_patch_hot_applies_through_a_started_watcher(self, client_ctx) -> None:
        """PATCH ends in ``_hot_apply_after_write``, so the new value is in force
        before the response -- an applier has already run by the time it returns."""
        w = live.watch()
        await w.start(initial=KiroCrewConfig.load())
        seen: list[str] = []
        w.subscribe("agent.model", callback=lambda ch: seen.append(ch.new.agent.model), name="m")
        try:
            async with client_ctx() as c:
                resp = await c.patch(
                    "/api/config/kirocrew", json={"path": "agent.model", "value": "model-hotpatch"}
                )
                assert resp.status == 200
            assert seen == ["model-hotpatch"]
        finally:
            await w.stop()
