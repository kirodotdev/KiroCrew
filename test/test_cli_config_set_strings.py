"""``kirocrew config set signal.account`` never stores a phone number as an integer.

The generic parser tries ``int()`` first, and Python's ``int("+447700900001")``
succeeds, so an unquoted E.164 number parses as the integer ``447700900001``
without its ``+`` (and ``0044...`` loses its leading zeros). The loader reads a
wrong-typed ``signal.account`` as absent, so storing that would silently empty
the setting. The declared-type check refuses it on every write instead, with a
hint to quote the value as JSON, and a quoted number is stored as typed.

Every test writes under a temp data home; none touches ``~/.kiro/crew``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.cli_config import _config_cmd


def _set(home: Path, key: str, value: str, *, local: bool = False) -> None:
    args = argparse.Namespace(config_action="set", key=key, value=value, file=None, local=local)
    with (
        patch("kiro_crew.cli_config.config_path", return_value=home / "config.json"),
        patch("kiro_crew.cli_config.config_local_path", return_value=home / "config.local.json"),
        patch("kiro_crew.config.loader.config_path", return_value=home / "config.json"),
        patch("kiro_crew.config.loader.config_dir", return_value=home),
        patch("kiro_crew.cli_config.sel"),
    ):
        _config_cmd(args)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    d = tmp_path / "crew"
    d.mkdir()
    return d


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("value", ["+447700900001", "447700900001", "0044770090"])
def test_an_unquoted_phone_number_is_refused_with_nothing_written(home, capsys, value, local):
    with pytest.raises(SystemExit) as exc:
        _set(home, "signal.account", value, local=local)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "signal.account: expected string" in err and "quote it as JSON" in err
    assert not (home / ("config.local.json" if local else "config.json")).exists()


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("value", ["+447700900001", "0044770090"])
def test_a_quoted_phone_number_is_stored_as_typed(home, value, local):
    _set(home, "signal.account", json.dumps(value), local=local)
    saved = _read(home / ("config.local.json" if local else "config.json"))
    assert saved["signal"]["account"] == value


def test_a_non_string_key_still_parses(home):
    _set(home, "signal.enabled", "true")
    _set(home, "signal.soft_threshold_pct", "70")
    signal = _read(home / "config.json")["signal"]
    assert signal["enabled"] is True and signal["soft_threshold_pct"] == 70


def test_the_round_trip_through_the_loader_keeps_the_plus(home):
    from kiro_crew.config.loader import KiroCrewConfig

    _set(home, "signal.account", '"+447700900001"')
    with (
        patch("kiro_crew.config.loader.config_path", return_value=home / "config.json"),
        patch("kiro_crew.config.loader.config_dir", return_value=home),
    ):
        assert KiroCrewConfig.load().signal.account == "+447700900001"
