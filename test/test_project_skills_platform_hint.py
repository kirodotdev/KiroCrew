"""The Skills page learns when project skills cannot load on this platform.

``GET /api/skills/-/trust`` carries ``platform_unsupported``: a chat has a
project open, project skills are switched on, and the platform lacks the
no-follow walk the trust gate needs (Windows). The page shows a muted hint for
it. The flag is computed from the platform and the config only, so it must not
depend on the project path existing: these tests open a project that is not on
disk at all.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from kiro_crew import skill_trust

pytestmark = pytest.mark.usefixtures("isolated_home")


@pytest.fixture(name="isolated_home")
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    skill_trust.reset_cache_for_tests()
    yield
    skill_trust.reset_cache_for_tests()


async def _read_trust(monkeypatch, *, slots, supported, enabled):
    from kiro_crew.dashboard.handlers import prompts

    monkeypatch.setattr(prompts, "is_owner_dashboard_request", lambda _request: True, raising=False)
    monkeypatch.setattr(prompts, "_sel", lambda: SimpleNamespace(log_api_access=Mock()))
    monkeypatch.setattr(prompts, "project_skill_traversal_supported", lambda: supported)
    monkeypatch.setattr(prompts, "project_skills_enabled", lambda: enabled)
    # The Skills page asks with no chat key, as the dashboard's global page does.
    request = SimpleNamespace(app={"state": SimpleNamespace(_slots=slots)}, headers={})
    response = await prompts.api_skills_trust(request)
    assert response.status == 200
    return json.loads(response.body)


def _slot(project):
    return SimpleNamespace(project=project)


_MISSING_PROJECT = "Z:/not-on-disk/team-repo"


@pytest.mark.asyncio
async def test_unsupported_platform_with_an_open_project_sets_the_flag(monkeypatch):
    body = await _read_trust(
        monkeypatch, slots={"chat-1": _slot(_MISSING_PROJECT)}, supported=False, enabled=True
    )
    assert body["platform_unsupported"] is True
    # The page's own chat has no project, so nothing was canonicalized.
    assert body["project"] == ""
    assert body["project_key"] == ""


@pytest.mark.asyncio
async def test_two_different_open_projects_still_count_as_open(monkeypatch):
    slots = {"chat-1": _slot(_MISSING_PROJECT), "chat-2": _slot("Y:/other")}
    body = await _read_trust(monkeypatch, slots=slots, supported=False, enabled=True)
    assert body["platform_unsupported"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("slots", "supported", "enabled"),
    [
        ({}, False, True),
        ({"chat-1": _slot(None)}, False, True),
        ({"chat-1": _slot(_MISSING_PROJECT)}, True, True),
        ({"chat-1": _slot(_MISSING_PROJECT)}, False, False),
    ],
    ids=["no-slots", "slot-without-project", "supported-platform", "feature-off"],
)
async def test_flag_stays_off_otherwise(monkeypatch, slots, supported, enabled):
    body = await _read_trust(monkeypatch, slots=slots, supported=supported, enabled=enabled)
    assert body["platform_unsupported"] is False


def test_grant_and_revoke_snapshots_default_the_flag_off():
    from kiro_crew.dashboard.handlers.prompts import _trust_snapshot

    assert _trust_snapshot(None)["platform_unsupported"] is False
