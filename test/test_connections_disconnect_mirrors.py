"""Which agent specs the Disconnect census reads as Kiro Crew's own mirrors.

A mirror's entry named like the provider is not a holder of the grant once the purge
runs, so the label decides whether Disconnect can remove the stored token.
"""

from __future__ import annotations

import pathlib

import pytest

from kiro_crew.agent_sdk.drivers import acp as acp_driver
from kiro_crew.config import paths as config_paths
from kiro_crew.connections import ownership
from kiro_crew.dashboard.handlers import mcp as mcp_handlers

_VIEW_SOURCES = {
    "kirocrew-skill-view-0a1b": "kirocrew",
    "kirocrew-skill-view-2c3d": "kirocrew-worker",
    "kirocrew-skill-view-4e5f": "kirocrew-conductor",
}


def test_only_specs_reflecting_the_purged_scopes_are_mirrors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The worker and a view of either default spec are mirrors; nothing else is.

    ``kirocrew-worker.json`` is re-derived from the purged ``kirocrew.json`` and a
    skill view copies the spec it projects, so read as user specs they kept the token
    as "also configured by" the provider's own name. A conductor carries approved
    additions the purge never touches, and a view with no recorded source may stand
    for a spec the census cannot read: both still keep the grant.
    """
    monkeypatch.setattr(config_paths, "kiro_agents_dir", lambda: tmp_path)
    monkeypatch.setattr(mcp_handlers, "_extra_mcp_scopes", list)
    monkeypatch.setattr(acp_driver, "skill_view_source_agent", _VIEW_SOURCES.get)
    expected = {
        "kirocrew.json": "mirror",
        "kirocrew-worker.json": "mirror",
        "kirocrew-skill-view-0a1b.json": "mirror",
        "kirocrew-skill-view-2c3d.json": "mirror",
        "kirocrew-conductor.json": "agent",
        "kirocrew-skill-view-4e5f.json": "agent",
        "kirocrew-skill-view-ffff.json": "agent",
        "custom.json": "agent",
    }
    for name in expected:
        (tmp_path / name).write_text('{"mcpServers": {}}', encoding="utf-8")

    labels = {path.name: label.split(":")[0] for label, path in ownership.agent_spec_sources()}

    assert labels == expected
