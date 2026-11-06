"""A ``config.json`` or ``config.local.json`` that is not UTF-8 is read as unreadable.

``read_config_text`` decodes as UTF-8, and a byte it cannot decode raises
``UnicodeDecodeError``: a ``ValueError``, not an ``OSError`` and not a
``json.JSONDecodeError``. Each reader here already treats an unreadable or
malformed file as absent or degraded; these tests pin that a non-UTF-8 file takes
that same path instead of escaping as a traceback.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from kiro_crew.config.loader import KiroCrewConfig, config_local_path, config_path

#: Valid JSON shape around one byte UTF-8 cannot decode.
_NOT_UTF8 = b'{"x_user_note": "caf\xe9"}'


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from kiro_crew import cli_config

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    assert config_path().parent == tmp_path
    # `config set` reads these through its own binding; pin them to the loader's
    # functions so the test does not depend on how an earlier test left them.
    monkeypatch.setattr(cli_config, "config_path", config_path)
    monkeypatch.setattr(cli_config, "config_local_path", config_local_path)
    return tmp_path


def test_doctor_reports_a_non_utf8_config_as_unreadable(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from kiro_crew.config.superseded_defaults import render_doctor_section

    config_path().write_bytes(_NOT_UTF8)
    issues: list[str] = []
    render_doctor_section(issues)
    assert "stored defaults unreadable" in issues
    assert "could not read" in capsys.readouterr().out


def test_config_set_ignores_a_non_utf8_overlay(home: Path) -> None:
    from kiro_crew.cli_config import _run_config_cmd

    config_path().write_text(json.dumps({"agent": {}}), encoding="utf-8")
    config_local_path().write_bytes(_NOT_UTF8)
    _run_config_cmd(
        argparse.Namespace(
            config_action="set", key="dashboard.theme_mode", value="light", file=None, local=False
        )
    )
    saved = json.loads(config_path().read_text(encoding="utf-8"))
    assert saved["dashboard"]["theme_mode"] == "light"


def test_save_ignores_a_non_utf8_overlay(home: Path) -> None:
    config_path().write_text(json.dumps({"agent": {}}), encoding="utf-8")
    cfg = KiroCrewConfig.load()
    config_local_path().write_bytes(_NOT_UTF8)
    cfg.save()
    assert isinstance(json.loads(config_path().read_text(encoding="utf-8")), dict)


def test_raw_config_reads_a_non_utf8_config_as_empty(home: Path) -> None:
    from kiro_crew.config.loader_document import _raw_config

    config_path().write_bytes(_NOT_UTF8)
    assert _raw_config() == {}


def test_computer_use_names_a_non_utf8_config_corrupt(home: Path) -> None:
    from kiro_crew.dashboard.handlers.computer_use import StateCorruptError, _read_config_strict

    config_path().write_bytes(_NOT_UTF8)
    with pytest.raises(StateCorruptError):
        _read_config_strict()


def test_mcp_gateway_reads_a_non_utf8_overlay_as_absent(home: Path) -> None:
    from kiro_crew.dashboard.handlers.mcp_gateway_routes import _local_overlay_section

    config_local_path().write_bytes(_NOT_UTF8)
    assert _local_overlay_section() == {}


def test_channel_presets_fall_back_to_defaults_on_a_non_utf8_config(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.dashboard import handlers_channel

    monkeypatch.setattr(handlers_channel, "_presets_cache", None)
    config_path().write_bytes(_NOT_UTF8)
    assert handlers_channel._load_presets() == handlers_channel._DEFAULT_PRESETS
