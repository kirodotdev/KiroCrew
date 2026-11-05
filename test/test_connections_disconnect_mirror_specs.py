"""Only specs a Disconnect clears count as its own mirrors; every other one is a sharer.

The Disconnect census labels each agent spec it reads. A ``mirror:`` spec reflects
an entry the transaction removes or re-derives from the purged default agent, so it
never keeps the stored sign-in token alive; an ``agent:`` spec is a sharer and does.
"""

from __future__ import annotations

import pathlib

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew import mcp_discovery
from kiro_crew.agent_sdk.drivers import acp as acp_driver
from kiro_crew.config import paths as connections_paths
from kiro_crew.connections import ownership
from kiro_crew.dashboard.handlers import mcp as mcp_handlers

#: The two marks every worker derivation writes: its name and the ``kirocrew-work`` mount.
_OUR_WORKER = '{"name": "kirocrew-worker", "mcpServers": {"kirocrew-work": {}}}'


def _agents_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    """Point every census source at ``tmp_path`` and return the agents dir."""
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(mcp_discovery, "_MCP_SOURCES", ((tmp_path / "mcp.json", "kirocrew"),))
    monkeypatch.setattr(mcp_discovery, "_extra_scope_sources", list)
    monkeypatch.setattr(connections_paths, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(mcp_handlers, "_extra_mcp_scopes", list)
    return agents


def _view_sources(monkeypatch: pytest.MonkeyPatch, sources: dict[str, str]) -> None:
    """Record the source agent of each named skill view; any other has none."""
    monkeypatch.setattr(acp_driver, "skill_view_source_agent", sources.get)


def _labels() -> set[str]:
    return {label for label, _path in ownership.agent_spec_sources()}


def test_the_default_spec_its_worker_and_its_views_are_mirrors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """These are rewritten from the purged default agent, so they hold no grant.

    Labelled ``agent:`` they counted as a user sharer, so Disconnect kept the stored
    token and named the provider itself as the other user of it.
    """
    agents = _agents_dir(monkeypatch, tmp_path)
    (agents / "kirocrew.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    (agents / "kirocrew-worker.json").write_text(_OUR_WORKER, encoding="utf-8")
    (agents / "kirocrew-skill-view-0123abcd.json").write_text("{}", encoding="utf-8")
    _view_sources(monkeypatch, {"kirocrew-skill-view-0123abcd": "kirocrew"})

    labels = _labels()

    assert "mirror:kirocrew.json" in labels, labels
    assert "mirror:kirocrew-worker.json" in labels, labels
    assert "mirror:kirocrew-skill-view-0123abcd.json" in labels, labels


@pytest.mark.parametrize(
    "name",
    [
        "kirocrew-mine.json",
        "kirocrew-research.json",
        "kirocrew-mate-worker.json",
        "kirocrew-dashboard-author.json",
    ],
)
def test_a_spec_the_disconnect_does_not_rewrite_is_a_sharer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, name: str
) -> None:
    """Other Kiro Crew names included: their installers keep entries the purge never edits.

    A research or team-lead spec keeps an ``agent.json`` override, and a customized
    mate copy is preserved, so the entry outlives the Disconnect and still holds
    the grant.
    """
    agents = _agents_dir(monkeypatch, tmp_path)
    (agents / name).write_text('{"mcpServers": {"notion": {}}}', encoding="utf-8")

    assert f"agent:{name}" in _labels()


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("kiroCrew.json", '{"mcpServers": {"notion": {}}}'),  # brand-ok
        ("kiroCrew-worker.json", _OUR_WORKER),  # brand-ok
        ("kiroCrew-skill-view-0123abcd.json", '{"mcpServers": {"notion": {}}}'),  # brand-ok
    ],
)
def test_a_name_that_differs_only_by_case_is_a_sharer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, name: str, body: str
) -> None:
    """Kiro Crew writes only the lowercase names, and the purge and rebuild touch only those.

    On a case-sensitive filesystem a hand-written mixed-case file is a different
    spec the Disconnect leaves alone, so its entry still holds the grant.
    """
    agents = _agents_dir(monkeypatch, tmp_path)
    (agents / name).write_text(body, encoding="utf-8")
    _view_sources(monkeypatch, {pathlib.Path(name).stem: "kirocrew"})

    assert f"agent:{name}" in _labels()


def test_a_foreign_or_unreadable_worker_file_is_a_sharer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The worker installer refuses to overwrite a file it did not write, so it survives."""
    agents = _agents_dir(monkeypatch, tmp_path)
    worker = agents / "kirocrew-worker.json"
    worker.write_text('{"name": "my-own-agent", "mcpServers": {"notion": {}}}', encoding="utf-8")
    assert "agent:kirocrew-worker.json" in _labels()

    worker.write_text("{not json", encoding="utf-8")
    assert "agent:kirocrew-worker.json" in _labels()


def test_a_skill_view_of_another_agent_is_a_sharer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """A view of a custom or project agent keeps that agent's servers.

    Neither the purge nor the rebuild strips it, so its entry keeps the grant. A view
    with no recorded source is not provably ours either.
    """
    agents = _agents_dir(monkeypatch, tmp_path)
    for stem in ("kirocrew-skill-view-0000aaaa", "kirocrew-skill-view-1111bbbb"):
        (agents / f"{stem}.json").write_text('{"mcpServers": {"notion": {}}}', encoding="utf-8")
    _view_sources(monkeypatch, {"kirocrew-skill-view-0000aaaa": "dev"})

    labels = _labels()

    assert "agent:kirocrew-skill-view-0000aaaa.json" in labels, labels
    assert "agent:kirocrew-skill-view-1111bbbb.json" in labels, labels


def test_a_provenance_check_that_raises_leaves_a_sharer_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Damaged state behind the ownership check must not abort Disconnect."""
    agents = _agents_dir(monkeypatch, tmp_path)
    (agents / "kirocrew-worker.json").write_text(_OUR_WORKER, encoding="utf-8")

    def _broken(_spec):
        raise ValueError("agent state is malformed")

    monkeypatch.setattr(agent_mod, "_foreign_worker_spec_reason", _broken)

    assert "agent:kirocrew-worker.json" in _labels()
