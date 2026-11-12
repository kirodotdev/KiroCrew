"""A desktop channel file's ``releaseNotes`` is the published version's CHANGELOG.md section."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
import yaml

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "feed_release_notes.py"

_SECTION = '### Fixed\n- version: 9.9.9 is "quoted" \\ — and: colons'
_CHANGELOG = f"""# Changelog

## [0.4.0] — 2026-08-25

{_SECTION}

## [0.3.0] — 2026-08-17

- older
"""


@pytest.fixture(scope="module")
def notes() -> ModuleType:
    spec = importlib.util.spec_from_file_location("feed_release_notes", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("version", ["0.4.0", "0.4.0-insider.14", "0.4.0-rc.2"])
def test_a_release_and_its_prereleases_get_its_section(notes: ModuleType, version: str) -> None:
    assert notes.release_notes(_CHANGELOG, version) == _SECTION


@pytest.mark.parametrize("version", ["0.5.0-nightly.20261105t010203", "0.5.0", ""])
def test_a_version_with_no_section_gets_empty_notes(notes: ModuleType, version: str) -> None:
    assert notes.release_notes(_CHANGELOG, version) == ""


def test_printed_notes_are_one_yaml_value_in_the_channel_file(
    notes: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "CHANGELOG.md").write_text(_CHANGELOG, encoding="utf-8")
    monkeypatch.setattr(notes, "ROOT", tmp_path)
    assert notes.main(["feed_release_notes.py", "0.4.0-insider.14"]) == 0
    printed = capsys.readouterr().out
    assert printed.isascii() and printed.count("\n") == 1
    feed = yaml.safe_load(f"version: 0.4.0-insider.14\nreleaseNotes: {printed}")
    assert feed == {"version": "0.4.0-insider.14", "releaseNotes": _SECTION}
