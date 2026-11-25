"""A legacy template crew that runs on a user-level agent file of its name imports whole.

A crew row the agent sync wrote (``kiro_agent`` is its name, no ``member_id``) runs on
the person's own agent file in the Kiro home's ``agents/`` folder: a user-level file
of that name wins (``mate_worker._mate_file_on_disk``). An import back into the same
install, or into a fresh data home on the same Kiro home, changes nothing about that:
the crewmate part is ``not_needed`` with reason ``user_agent_named``, nothing is
written under ``mates/agents/`` and the import is not refused. A Bot accepted only
because the data home was fresh also gets the notice ``runs_on_user_agent``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from portability_bot_helpers import Homes, create_bot, export_archive, install_fixture_catalog

from kiro_crew import mate_config
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, _invalidate_config_cache
from kiro_crew.portability import apply_import_zip

CREW = "reviewer"
SPEC = {"name": CREW, "prompt": "You review code.", "tools": ["fs_read"]}


@pytest.fixture
def homes(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> Homes:
    install_fixture_catalog(tmp_path / "catalog", _floor_monkeypatch)
    return Homes(tmp_path / "homes", _floor_monkeypatch)


def _legacy_crew(homes: Homes) -> Path:
    """The crew row the agent sync writes, on a user-level spec of its name; that spec."""
    spec = homes.kiro(homes.current) / "agents" / f"{CREW}.json"
    spec.write_text(json.dumps(SPEC), encoding="utf-8")
    cfg = KiroCrewConfig.load()
    cfg.agents[CREW] = KiroCrewAgentConfig(kiro_agent=CREW)
    cfg.save()
    _invalidate_config_cache()
    return spec


def _row(summary: dict[str, Any]) -> dict[str, Any]:
    [row] = [r for r in summary.get("bots", []) if r["bot"] == CREW]
    return row


def _assert_whole(summary: dict[str, Any], spec: Path, spec_bytes: bytes) -> dict[str, Any]:
    row = _row(summary)
    assert row["crewmate_file"] == "not_needed", row
    assert row["part_reasons"]["crewmate_file"] == "user_agent_named", row
    assert "bots" not in summary.get("refused_merges", []), summary
    assert not [line for line in summary["items"] if "not whole" in line and CREW in line]
    assert not mate_config.mate_config_path(CREW).exists()
    assert spec.read_bytes() == spec_bytes
    return row


@pytest.mark.parametrize("mode", ["replace", "merge"])
def test_a_self_restore_of_a_legacy_template_crew_is_whole(
    homes: Homes, tmp_path: Path, mode: str
) -> None:
    homes.use("a")
    create_bot("Atlas")
    spec = _legacy_crew(homes)
    spec_bytes = spec.read_bytes()
    archive = export_archive(tmp_path / "a.zip")

    summary = apply_import_zip(archive, mode=mode)

    row = _assert_whole(summary, spec, spec_bytes)
    assert row["notices"] == [], row
    assert not [line for line in summary["items"] if line.startswith(f"bot {CREW} ")]


@pytest.mark.parametrize("mode", ["replace", "merge"])
def test_a_legacy_template_crew_restored_into_a_fresh_data_home_is_whole(
    homes: Homes, tmp_path: Path, mode: str
) -> None:
    homes.use("a")
    # No crewmate file: the one the source wrote would name the source's data home in
    # the owner record, which the fresh data home rightly reads as another's.
    create_bot("Atlas", crewmate_file=False)
    spec = _legacy_crew(homes)
    spec_bytes = spec.read_bytes()
    archive = export_archive(tmp_path / "a.zip")
    # A fresh data home on the same Kiro home: the user-level file is still there.
    homes.mp.setenv("KIROCREW_HOME", str(tmp_path / "fresh-crew"))
    _invalidate_config_cache()

    summary = apply_import_zip(archive, mode=mode)

    row = _assert_whole(summary, spec, spec_bytes)
    assert row["notices"] == ["runs_on_user_agent"], row
    lines = [line for line in summary["items"] if line.startswith(f"bot {CREW} ")]
    assert lines == [f"bot {CREW} (notice: runs_on_user_agent)"], summary["items"]
