"""The boot drain removes the projection metadata an old build left in the agents directory.

Builds before ``v0.10.0`` wrote the view ledger and the ownership sidecars to
``~/.kiro/agents/.kirocrew-skill-projection-metadata/``. Nothing reads them
there, and kiro-cli loads every ``.json`` under the agents directory,
dot-prefixed subdirectories included, as an agent profile, so a file left
behind is a subagent in every session. The drain removes the files
whose name and content prove Crew wrote them, and the directory once it is
empty; anything else in it stays.
"""

from __future__ import annotations

import json
import os

import pytest

from kiro_crew import platform_compat

LEGACY_DIR = ".kirocrew-skill-projection-metadata"
VIEW = "kirocrew-skill-view-" + "a" * 24
LEDGER = "view-sources.json"
SIDECAR = {"x-kirocrew-managed": "skill-view", "x-kirocrew-agent": "docs-author"}


@pytest.fixture
def agents(tmp_path, monkeypatch):
    """An empty agents directory and data home the real boot drain runs against."""
    from kiro_crew.acp import skill_projection as projection

    directory = tmp_path / "kiro" / "agents"
    directory.mkdir(parents=True)
    monkeypatch.setattr(projection, "data_home", lambda: tmp_path / "crew", raising=False)
    monkeypatch.setattr(projection, "kiro_agents_dir", lambda: directory)
    monkeypatch.setattr(projection, "_DRAIN_BATCH_PAUSE_SECS", 0)
    return directory


def _write_legacy(agents, files):
    legacy = agents / LEGACY_DIR
    legacy.mkdir()
    for name, body in files.items():
        (legacy / name).write_text(json.dumps(body), encoding="utf-8")
    return legacy


def test_drain_removes_the_legacy_ledger_sidecars_and_directory(agents):
    from kiro_crew.acp import skill_projection as projection

    legacy = _write_legacy(agents, {LEDGER: {VIEW: "docs-author"}, f"{VIEW}.json": SIDECAR})

    projection.drain_stale_aliases()

    assert not legacy.exists()


@pytest.mark.parametrize(
    ("name", "body"),
    [
        pytest.param(LEDGER, {VIEW: "docs-author", "my-agent": "x"}, id="ledger-with-foreign-key"),
        pytest.param(LEDGER, [VIEW], id="ledger-not-an-object"),
        pytest.param(f"{VIEW}.json", {"name": VIEW}, id="sidecar-without-marker"),
        pytest.param("my-agent.json", SIDECAR, id="name-crew-never-wrote"),
    ],
)
def test_drain_keeps_a_file_crew_did_not_write(agents, name, body):
    from kiro_crew.acp import skill_projection as projection

    legacy = _write_legacy(agents, {f"{VIEW}.json": SIDECAR})
    (legacy / name).write_text(json.dumps(body), encoding="utf-8")

    projection.drain_stale_aliases()

    assert (legacy / name).is_file()
    assert sorted(entry.name for entry in legacy.iterdir()) == [name]


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="symlinks need privileges on Windows")
def test_drain_does_not_follow_a_linked_legacy_directory(agents, tmp_path):
    from kiro_crew.acp import skill_projection as projection

    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / LEDGER).write_text(json.dumps({VIEW: "docs-author"}), encoding="utf-8")
    os.symlink(target, agents / LEGACY_DIR)

    projection.drain_stale_aliases()

    assert (target / LEDGER).is_file()
