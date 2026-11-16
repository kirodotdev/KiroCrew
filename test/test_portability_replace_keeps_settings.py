"""A dashboard import in Replace keeps this install's settings documents unless picked.

``config.json``, ``config.local.json`` and ``hooks.json`` of the archive take effect on a
running gateway within seconds of being installed, so a Replace installs them over an
EXISTING file only when the request carries the explicit pick (``include_settings``). A
missing live file is still filled from the archive, and everything else Replace does is
unchanged.
"""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import portability
from kiro_crew.portability import apply_import_zip

MARKER = {"agent": {"provider": "archive-marker"}}
LIVE = {"agent": {"provider": "live-original"}}
SETTINGS = ("config.json", "config.local.json", "hooks.json")


def _archive(path: Path) -> Path:
    """A minimal archive that carries the three settings documents and one skill."""
    with zipfile.ZipFile(str(path), "w") as zf:
        zf.writestr("snap/MANIFEST.json", json.dumps({"version": 2}))
        zf.writestr("snap/config.json", json.dumps(MARKER))
        zf.writestr("snap/config.local.json", json.dumps({"agent": {"model": "archive-local"}}))
        zf.writestr("snap/hooks.json", json.dumps({"hooks": []}))
        zf.writestr("snap/skills/pick-me/SKILL.md", "archive skill")
    return path


def _live_home(tmp_path: Path, *, with_settings: bool) -> Path:
    home = tmp_path / "live_home"
    home.mkdir()
    if with_settings:
        (home / "config.json").write_text(json.dumps(LIVE), encoding="utf-8")
        (home / "config.local.json").write_text(json.dumps({"x": 1}), encoding="utf-8")
        (home / "hooks.json").write_text(json.dumps({"hooks": [{"id": "live"}]}), encoding="utf-8")
    return home


def _import(zip_path: Path, home: Path, **kwargs) -> dict:
    with patch("kiro_crew.portability.config_dir", return_value=home):
        with patch.dict(os.environ, {"KIROCREW_HOME": str(home)}):
            return apply_import_zip(zip_path, mode="replace", **kwargs)


def test_replace_without_the_pick_keeps_the_three_live_documents(tmp_path):
    home = _live_home(tmp_path, with_settings=True)
    before = {name: (home / name).read_bytes() for name in SETTINGS}

    summary = _import(_archive(tmp_path / "a.zip"), home)

    for name in SETTINGS:
        assert (home / name).read_bytes() == before[name], name
    assert (home / "skills" / "pick-me" / "SKILL.md").read_text(encoding="utf-8") == (
        "archive skill"
    ), "the rest of Replace is unchanged"
    assert sorted(summary["settings_kept"]) == sorted(SETTINGS), summary


def test_replace_with_the_pick_replaces_them_and_backs_up_the_old_ones(tmp_path):
    home = _live_home(tmp_path, with_settings=True)

    summary = _import(_archive(tmp_path / "a.zip"), home, include_settings=True)

    assert json.loads((home / "config.json").read_text(encoding="utf-8")) == MARKER
    assert "archive-local" in (home / "config.local.json").read_text(encoding="utf-8")
    assert not any(n in summary.get("settings_kept", []) for n in SETTINGS), summary
    backed_up = [p.name for p in home.rglob("config.json") if p != home / "config.json"]
    assert backed_up, "the old config.json must be saved before it is replaced"


def test_replace_installs_the_archives_documents_where_the_live_ones_are_absent(tmp_path):
    home = _live_home(tmp_path, with_settings=False)

    summary = _import(_archive(tmp_path / "a.zip"), home)

    assert json.loads((home / "config.json").read_text(encoding="utf-8")) == MARKER
    assert (home / "hooks.json").is_file()
    assert not any(n in summary.get("settings_kept", []) for n in SETTINGS), summary


def test_a_marker_config_imported_without_the_pick_is_not_seen_by_a_fresh_load(
    tmp_path, monkeypatch
):
    from kiro_crew.config import loader_document

    home = _live_home(tmp_path, with_settings=True)
    _import(_archive(tmp_path / "a.zip"), home)

    seen = loader_document.read_config_for_update(home / "config.json")
    assert seen["agent"]["provider"] == "live-original", seen


@pytest.mark.parametrize("pick", [False, True])
def test_the_summary_names_what_it_kept_only_when_not_picked(tmp_path, pick):
    home = _live_home(tmp_path, with_settings=True)
    summary = _import(_archive(tmp_path / "a.zip"), home, include_settings=pick)
    assert bool(summary.get("settings_kept")) is (not pick), summary


def test_the_portability_module_exposes_the_settings_pick_files():
    assert portability.SETTINGS_PICK_FILES == SETTINGS
