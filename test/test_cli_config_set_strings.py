"""``kirocrew config set`` on a string-typed key stores the text as typed.

The generic parser tries ``int()`` first, and Python's ``int("+447700900001")``
succeeds: ``config set signal.account +447700900001`` stored the JSON integer
``447700900001``, dropping the ``+`` that makes it an E.164 number. The
first-write type check could not catch it, because ``signal.account`` is already
in the document (as ``""``). A key declared ``string`` now keeps the raw text.

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


@pytest.mark.parametrize("value", ["+447700900001", "447700900001", "0044770090"])
def test_a_phone_number_is_stored_as_the_string_typed(home, value):
    _set(home, "signal.account", value)
    assert _read(home / "config.json")["signal"]["account"] == value


def test_the_local_overlay_keeps_the_string_too(home):
    _set(home, "signal.account", "+447700900001", local=True)
    assert _read(home / "config.local.json")["signal"]["account"] == "+447700900001"


def test_a_string_that_looks_like_json_stays_text(home):
    # A string key never takes a parsed JSON value either: `true` or `{}` typed
    # for one is the text the person typed.
    _set(home, "signal.account", "true")
    assert _read(home / "config.json")["signal"]["account"] == "true"


def test_a_non_string_key_still_parses(home):
    _set(home, "signal.enabled", "true")
    _set(home, "signal.soft_threshold_pct", "70")
    signal = _read(home / "config.json")["signal"]
    assert signal["enabled"] is True and signal["soft_threshold_pct"] == 70


def test_the_round_trip_through_the_loader_keeps_the_plus(home):
    from kiro_crew.config.loader import KiroCrewConfig

    _set(home, "signal.account", "+447700900001")
    with (
        patch("kiro_crew.config.loader.config_path", return_value=home / "config.json"),
        patch("kiro_crew.config.loader.config_dir", return_value=home),
    ):
        assert KiroCrewConfig.load().signal.account == "+447700900001"
