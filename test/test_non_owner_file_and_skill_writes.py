"""The owner-only boundary on the two generic routes that can rewrite what the agent obeys.

``slack/allowlist.py::send_dashboard_link`` mints a dashboard token for any
Slack-allowlisted user with ``app=""``, so the token-auth middleware sets
``request["user"]`` to that channel user and ``request["app"]`` to ``""``: an
authenticated dashboard session whose subject is not the owner.

Steering mutations are owner-only because steering is injected into every agent
turn, and skill mutations are owner-only because installed skill content enters
the agent's context -- the steering create/update/delete routes and every skill
mutation routed through ``prompts._deny_non_owner_skill_operation`` refuse that
subject. Two sibling routes reach the same content and hold the same boundary:

* ``POST /api/file-write`` -- the markdown panel's save. It rewrites any existing
  file off the read+write sensitive floor, which includes a steering document, a
  ``SKILL.md`` and the MCP config the owner's sessions launch.
* ``POST /api/skills/-/discover/install`` -- installs a registry skill into the
  owner's catalog, the write ``POST /api/skills`` refuses the same caller. It
  lives in ``discover.py``, outside the module whose helper gates the rest.

Each refusal must land before the body is read, so a denied caller reaches no
path probe and no provider.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.dashboard.handlers import api_file_write, discover

pytestmark = pytest.mark.asyncio

NON_OWNER = "U0NONOWNER"


@pytest.fixture(autouse=True)
def _quiet_sel():
    with (
        patch("kiro_crew.sel.sel", return_value=MagicMock()),
        patch.object(discover, "_sel", return_value=MagicMock()),
    ):
        yield


# ── POST /api/file-write ──


def _file_write_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/api/file-write", api_file_write)
    return as_owner(app)


@pytest.fixture
def steering_doc(tmp_path: Path) -> Path:
    """An existing steering document -- a file the steering routes refuse to let a non-owner write."""
    doc = tmp_path / ".kiro" / "steering" / "rules.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("owner's rules", encoding="utf-8")
    return doc


async def test_file_write_refuses_a_non_owner(steering_doc: Path) -> None:
    from kiro_crew.dashboard.handlers import files

    with patch.object(files, "_probe_request_path", wraps=files._probe_request_path) as probe:
        async with TestClient(TestServer(_file_write_app())) as client:
            resp = await client.post(
                "/api/file-write",
                json={"path": str(steering_doc), "content": "planted"},
                headers={"X-Test-User": NON_OWNER},
            )
            body = await resp.json()

    assert resp.status == 403
    assert body.get("code") == "owner_only"
    assert steering_doc.read_text(encoding="utf-8") == "owner's rules"
    # Refused ahead of the path probe, so a non-owner cannot learn from a 404
    # whether a path exists either.
    probe.assert_not_called()


async def test_file_write_still_saves_for_the_owner(steering_doc: Path) -> None:
    """Positive control: the owner passes the gate and the write lands."""
    async with TestClient(TestServer(_file_write_app())) as client:
        resp = await client.post(
            "/api/file-write",
            json={"path": str(steering_doc), "content": "owner's edit"},
        )

    assert resp.status == 200
    assert steering_doc.read_text(encoding="utf-8") == "owner's edit"


# ── POST /api/skills/-/discover/install ──


def _install_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/api/skills/-/discover/install", discover.api_skills_discover_install)
    return as_owner(app)


async def test_skill_install_refuses_a_non_owner() -> None:
    registry = MagicMock()
    # Unavailable, so an ungated handler answers a plain 404 after consulting the
    # registry instead of going on to fetch -- the lookup alone shows it got past
    # the point where the refusal belongs.
    registry.get.return_value = None
    with patch.object(discover, "_get_registry", return_value=registry) as get_registry:
        async with TestClient(TestServer(_install_app())) as client:
            resp = await client.post(
                "/api/skills/-/discover/install",
                json={"provider": "skillsh", "skill_id": "planted-skill"},
                headers={"X-Test-User": NON_OWNER},
            )
            body = await resp.json()

    assert resp.status == 403
    assert body.get("code") == "dashboard_owner_required"
    get_registry.assert_not_called()


async def test_skill_install_still_reaches_the_provider_for_the_owner() -> None:
    """Positive control: the owner passes the gate and reaches the provider lookup."""
    registry = MagicMock()
    registry.get.return_value = None  # "not available": proves the lookup ran
    with patch.object(discover, "_get_registry", return_value=registry):
        async with TestClient(TestServer(_install_app())) as client:
            resp = await client.post(
                "/api/skills/-/discover/install",
                json={"provider": "skillsh", "skill_id": "some-skill"},
            )

    assert resp.status == 404
    registry.get.assert_called_once_with("skillsh")
