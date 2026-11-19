"""A streaming export builds the Bot roster and its manifest fields once.

:func:`portability_stream.plan_export` reads ``config.json`` and builds the roster
and the Bot manifest fields; the measurement pass and :func:`stream_export_zip`
use that one result instead of reading again. A direct caller of either helper
that passes no roster still gets one read of its own.
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path

import portability_helpers
import pytest

from kiro_crew import portability, portability_bot_export, portability_stream
from kiro_crew.config import paths as paths_mod

fake_kirocrew_home = portability_helpers.fake_kirocrew_home
patched_config_dir = portability_helpers.patched_config_dir


@pytest.fixture
def mates_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    mates = tmp_path / "kiro" / "mates" / "agents"
    mates.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "_mate_agents_dir_override", lambda: mates)
    return mates


def _seed(mc: Path) -> None:
    config = json.loads((mc / "config.json").read_text())
    config["agents"] = {"Atlas": {"member_id": "atlas", "kiro_agent": "Atlas"}}
    (mc / "config.json").write_text(json.dumps(config, indent=2))
    member = mc / "members" / "atlas"
    (member / "dashboard").mkdir(parents=True)
    (member / "dashboard" / "instance.json").write_text(json.dumps({"state": "ok"}))
    (member / "briefing.md").write_text("# Atlas\n")


class _Counting:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.rosters = 0
        self.fields = 0
        real_roster = portability_bot_export.load_roster
        real_fields = portability_bot_export.bot_manifest_fields

        def load_roster(mc_real: str) -> list[tuple[str, str]]:
            self.rosters += 1
            return real_roster(mc_real)

        def bot_manifest_fields(bots: list[tuple[str, str]]) -> dict:
            self.fields += 1
            return real_fields(bots)

        monkeypatch.setattr(portability_bot_export, "load_roster", load_roster)
        monkeypatch.setattr(portability_bot_export, "bot_manifest_fields", bot_manifest_fields)


@pytest.mark.parametrize("include_sessions", [True, False])
def test_a_streaming_export_reads_the_roster_once(
    patched_config_dir: Path,
    mates_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    include_sessions: bool,
) -> None:
    _seed(patched_config_dir)
    counts = _Counting(monkeypatch)

    chunks: list[bytes] = []
    plan = portability.plan_export(include_sessions=include_sessions)
    manifest = portability.stream_export_zip(chunks.append, plan)

    assert (counts.rosters, counts.fields) == (1, 1)
    with zipfile.ZipFile(io.BytesIO(b"".join(chunks))) as zf:
        names = {n.split("/", 1)[1] for n in zf.namelist()}
    assert {"members/atlas/briefing.md", "members/atlas/dashboard/instance.json"} <= names
    assert manifest["contents"]["bot_store_files"] == 2


def test_a_memory_only_plan_reads_no_roster(
    patched_config_dir: Path, mates_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(patched_config_dir)
    counts = _Counting(monkeypatch)

    plan = portability.plan_export(memory_only=True)
    portability.stream_export_zip(lambda _b: None, plan)

    assert (counts.rosters, counts.fields) == (0, 0)


def test_the_helpers_still_read_a_roster_when_given_none(
    patched_config_dir: Path, mates_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(patched_config_dir)
    counts = _Counting(monkeypatch)

    members, _bytes = portability_stream._measure_nonsession_footprint(
        os.path.realpath(patched_config_dir), memory_only=False, include_agent_templates=False
    )
    manifest = portability.create_export_zip()[1]

    assert counts.rosters == 2
    assert manifest["contents"]["bot_store_files"] == 2
    assert members >= 2
