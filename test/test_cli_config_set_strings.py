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


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        ('"+447700900001"', "+447700900001"),
        ('"some text"', "some text"),
        ('"caf\\u00e9"', "café"),
        ('""', ""),
    ],
)
def test_a_json_string_literal_is_decoded_to_its_string(home, value, stored):
    # The generic parse always JSON-decoded a quoted value; a string key keeps
    # doing so, so `config set k '"text"'` stores `text`, not `"text"`.
    _set(home, "signal.account", value)
    assert _read(home / "config.json")["signal"]["account"] == stored


@pytest.mark.parametrize("value", ['"a" "b"', '"', '"unterminated', 'say "hi"', '"bad \\x"'])
def test_anything_that_is_not_one_json_string_literal_stays_verbatim(home, value):
    _set(home, "signal.account", value)
    assert _read(home / "config.json")["signal"]["account"] == value


@pytest.mark.parametrize("value", ["null", " null\n"])
def test_null_on_a_non_nullable_string_key_is_json_null_as_the_generic_parse_has_it(home, value):
    from kiro_crew.cli_config import _declared_entry, _parse_value

    entry = _declared_entry("signal.account")
    assert entry is not None and entry.type == "string" and not entry.nullable
    assert _parse_value(value) is None
    _set(home, "signal.account", value)
    assert _read(home / "config.json")["signal"]["account"] is None


@pytest.mark.parametrize("value", ["NULL", "Null", '"null"', "null null"])
def test_only_a_bare_null_is_json_null(home, value):
    _set(home, "signal.account", value)
    stored = _read(home / "config.json")["signal"]["account"]
    assert stored == ("null" if value == '"null"' else value)
