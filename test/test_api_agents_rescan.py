"""Tests for POST /api/agents/rescan endpoint."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.agents import api_agents_rescan


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/api/agents/rescan", api_agents_rescan)
    return app


class TestApiAgentsRescan:
    @pytest.mark.asyncio
    async def test_invalid_body_falls_back_to_registry(self):
        """Malformed/missing body defaults to {} and falls back to registry reload."""
        with patch(
            "kiro_crew.dashboard.handlers.agents.load_registry",
            return_value={},
        ), patch(
            "kiro_crew.dashboard.handlers.agents.list_agents",
            return_value=[],
        ):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.post(
                    "/api/agents/rescan",
                    data="not json",
                    headers={"Content-Type": "application/json"},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["discovered"] == 0

    @pytest.mark.asyncio
    async def test_paths_not_array_returns_400(self):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/agents/rescan",
                json={"paths": "/a/single/string"},
            )
            assert resp.status == 400
            data = await resp.json()
            assert "error" in data

    @pytest.mark.asyncio
    async def test_sensitive_path_rejected(self, tmp_path: Path):
        with patch(
            "kiro_crew.dashboard.handlers.agents.is_sensitive_path",
            return_value=True,
        ):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.post(
                    "/api/agents/rescan",
                    json={"paths": [str(tmp_path)]},
                )
                # All provided paths rejected → 400 (reject, not fallback)
                assert resp.status == 400
                data = await resp.json()
                assert "error" in data

    @pytest.mark.asyncio
    async def test_scan_valid_path_returns_discovered(self, tmp_path: Path):
        # Create a project with a .kiro/agents/ directory
        agents_dir = tmp_path / "myproj" / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "dev.json").write_text(
            json.dumps({"name": "dev", "model": "auto"}), encoding="utf-8"
        )

        with patch(
            "kiro_crew.dashboard.handlers.agents.list_agents",
            return_value=[],
        ), patch(
            "kiro_crew.aim_agents._registry_path",
            return_value=tmp_path / "reg.json",
        ), patch(
            "kiro_crew.dashboard.handlers.agents.Path.home",
            return_value=tmp_path,
        ):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.post(
                    "/api/agents/rescan",
                    json={"paths": [str(tmp_path)]},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["discovered"] >= 1

    @pytest.mark.asyncio
    async def test_empty_body_falls_back_to_registry(self, tmp_path: Path):
        """No paths → reloads from existing registry."""
        with patch(
            "kiro_crew.dashboard.handlers.agents.load_registry",
            return_value={},
        ), patch(
            "kiro_crew.dashboard.handlers.agents.list_agents",
            return_value=[],
        ):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.post("/api/agents/rescan", json={})
                assert resp.status == 200
                data = await resp.json()
                assert data["discovered"] == 0
                assert "agents" in data

    @pytest.mark.asyncio
    async def test_non_string_elements_filtered(self, tmp_path: Path):
        """Integer / null / empty elements in paths array all invalid → 400."""
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/agents/rescan",
                json={"paths": [None, 42, ""]},
            )
            assert resp.status == 400
            data = await resp.json()
            assert "error" in data


class TestApiAgentsRescanPathValidation:
    """Path validation rules for POST /api/agents/rescan."""

    @pytest.mark.asyncio
    async def test_filesystem_root_rejected(self) -> None:
        """'/' must return 400 — not under HOME, would trigger a full filesystem walk."""
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/agents/rescan", json={"paths": ["/"]})
            assert resp.status == 400
            data = await resp.json()
            assert "error" in data

    @pytest.mark.asyncio
    async def test_path_not_under_home_rejected(self) -> None:
        """Paths outside HOME (e.g. /etc) must be rejected."""
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/agents/rescan", json={"paths": ["/etc"]})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_home_itself_is_accepted(self, tmp_path: Path) -> None:
        """Scanning HOME itself is valid — user should be able to scan ~/."""
        with patch(
            "kiro_crew.dashboard.handlers.agents.load_registry",
            return_value={},
        ), patch(
            "kiro_crew.dashboard.handlers.agents.list_agents",
            return_value=[],
        ), patch(
            "kiro_crew.dashboard.handlers.agents.scan_directory",
            return_value=[],
        ):
            async with TestClient(TestServer(_make_app())) as client:
                import os
                home = os.path.expanduser("~")
                resp = await client.post("/api/agents/rescan", json={"paths": [home]})
                assert resp.status == 200

    @pytest.mark.asyncio
    async def test_null_byte_path_returns_400(self) -> None:
        """Path with embedded null byte must return 400, not 500."""
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/agents/rescan",
                json={"paths": ["/tmp/\x00bad"]},
            )
            assert resp.status == 400
            assert "error" in await resp.json()

    @pytest.mark.asyncio
    async def test_newline_in_path_rejected(self) -> None:
        """Path with embedded newline must return 400."""
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/agents/rescan", json={"paths": ["/tmp/bad\npath"]})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_overlong_path_rejected(self) -> None:
        """Path exceeding 4096 chars must return 400."""
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/agents/rescan",
                json={"paths": ["/" + "a" * 4097]},
            )
            assert resp.status == 400
