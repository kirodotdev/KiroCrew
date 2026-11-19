"""The Bot store stage is never a directory an archive can name or pre-fill.

An archive picks the name of its single top directory, which becomes the extraction
directory beside the stage. These tests import a real export rewritten under the top
directory ``.bot-stage`` (the stage prefix) with extra trees laid out where a stage
derived from that name would sit, and check only allowlisted files are installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from portability_bot_helpers import (
    Homes,
    apply_dashboard,
    archive_members,
    create_bot,
    export_archive,
    install_fixture_catalog,
    write_archive,
)

from kiro_crew import portability_bot_stores as stores
from kiro_crew.config.paths import data_home
from kiro_crew.portability import apply_import_zip


@pytest.fixture
def homes(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> Homes:
    install_fixture_catalog(tmp_path / "catalog", _floor_monkeypatch)
    return Homes(tmp_path / "homes", _floor_monkeypatch)


@pytest.mark.parametrize("mode", ["replace", "merge"])
def test_an_archive_named_like_the_stage_installs_nothing_the_allowlist_refuses(
    homes: Homes, tmp_path: Path, mode: str
) -> None:
    homes.use("a")
    atlas = create_bot("Atlas")
    board = apply_dashboard(atlas, '<p data-dashboard-field="goal">atlas</p>')
    members = archive_members(export_archive(tmp_path / "a.zip"))
    slug = atlas.slug
    planted = {
        f"{slug}/dashboard/planted.json": b'{"planted": true}',
        f"{slug}/dashboard/history/planted.json": b'{"planted": true}',
        f"{slug}/artifacts/{board.package}/planted.txt": b"planted",
        f"{slug}/briefing/welcome_owed.json": b"{}",
    }
    hostile = write_archive(tmp_path / "h.zip", {**members, **planted}, stores.STAGE_DIRNAME)
    homes.use("b").mkdir(parents=True, exist_ok=True)
    summary = apply_import_zip(hostile, mode=mode)
    [row] = [r for r in summary["bots"] if r["bot"] == "Atlas"]
    member = data_home() / "members" / slug
    assert (member / "dashboard" / "instance.json").is_file(), row
    assert not (member / "dashboard" / "planted.json").exists()
    assert not (member / "dashboard" / "history" / "planted.json").exists()
    assert not (member / "welcome_owed.json").exists()
    assert not (data_home() / "artifacts" / board.package / "planted.txt").exists()
