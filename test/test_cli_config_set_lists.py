"""``kirocrew config set`` on a list-typed key: a list, or a refusal that stores nothing.

The generic parser answers a word it cannot parse with the STRING itself. Under a
list key the loader replaces that string with the field's default (and logs one
line), so the whole field falls back to its default: Windows PowerShell 5.1 strips
the inner quotes of ``'["a","b"]'``, ``[a,b]`` was stored as text, and the apps a
person had already trusted stopped being trusted.

Every test writes under a temp data home; none touches ``~/.kiro/crew``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.cli_config import _config_cmd, _is_list_key, _parse_list_value

TRUSTED = "agent.apps_trusted"


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


def _refused(home: Path, key: str, value: str, *, local: bool = False, capsys) -> str:
    with pytest.raises(SystemExit) as exc:
        _set(home, key, value, local=local)
    assert exc.value.code == 1
    return capsys.readouterr().err


def test_json_array_is_stored_as_a_list(home, capsys):
    _set(home, TRUSTED, '["a","b"]')
    assert _read(home / "config.json")["agent"]["apps_trusted"] == ["a", "b"]
    assert "✅" in capsys.readouterr().out


def test_empty_json_array_clears_the_list(home):
    _set(home, TRUSTED, '["a"]')
    _set(home, TRUSTED, "[]")
    assert _read(home / "config.json")["agent"]["apps_trusted"] == []


def test_powershell_flattened_array_is_refused_and_writes_nothing(home, capsys):
    _set(home, TRUSTED, '["keep-me"]')
    before = (home / "config.json").read_text(encoding="utf-8")
    err = _refused(home, TRUSTED, "[a,b]", capsys=capsys)
    # The stored trust list is exactly what it was: this is the incident.
    assert (home / "config.json").read_text(encoding="utf-8") == before
    assert "agent.apps_trusted" in err
    # The retry lines are built from the caller's own items.
    assert '\'["a","b"]\'' in err  # PowerShell 7.3+
    assert '\'[\\"a\\",\\"b\\"]\'' in err  # Windows PowerShell 5.1


def test_a_lone_word_is_refused_rather_than_stored_as_a_string(home, capsys):
    err = _refused(home, TRUSTED, "somebody", capsys=capsys)
    assert "expected a JSON array" in err
    assert not (home / "config.json").exists()


def test_a_json_scalar_or_object_is_refused(home, capsys):
    for bad in ("true", "12", '"x"', '{"a": 1}'):
        err = _refused(home, TRUSTED, bad, capsys=capsys)
        assert "expected a JSON array" in err
    assert not (home / "config.json").exists()


@pytest.mark.parametrize(
    "value", ["a,b", "a, b ,c", "a,,b", "a,", ",a", ",", "a,'b'", "[a,b", "a,{b}"]
)
def test_anything_but_a_json_array_is_refused(home, capsys, value):
    # No comma-list convenience: a list field's item type is not known here, and a
    # list of the wrong item type is dropped by the loader just like a string.
    _refused(home, TRUSTED, value, capsys=capsys)
    assert not (home / "config.json").exists()


def test_an_integer_list_key_keeps_its_integers(home):
    _set(home, "telegram.allowed_user_ids", "[123,456]")
    assert _read(home / "config.json")["telegram"]["allowed_user_ids"] == [123, 456]


def test_a_comma_list_is_refused_for_an_integer_list_key_too(home, capsys):
    _refused(home, "telegram.allowed_user_ids", "123,456", capsys=capsys)
    assert not (home / "config.json").exists()


def test_empty_value_is_refused(home, capsys):
    _refused(home, TRUSTED, "", capsys=capsys)
    assert not (home / "config.json").exists()


def test_local_path_applies_the_same_rule(home, capsys):
    _set(home, TRUSTED, '["a","b"]', local=True)
    assert _read(home / "config.local.json")["agent"]["apps_trusted"] == ["a", "b"]
    before = (home / "config.local.json").read_text(encoding="utf-8")
    err = _refused(home, TRUSTED, "[a,b]", local=True, capsys=capsys)
    assert "expected a JSON array" in err
    assert (home / "config.local.json").read_text(encoding="utf-8") == before
    assert not (home / "config.json").exists()


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("agent.streaming", "false", False),
        ("session.autocompact_pct", "82", 82),
        ("dashboard.url", "http://localhost:5476", "http://localhost:5476"),
        ("agent.log_level", "debug", "DEBUG"),
    ],
)
def test_scalar_keys_are_unchanged(home, key, value, expected):
    _set(home, key, value)
    node = _read(home / "config.json")
    for part in key.split("."):
        node = node[part]
    assert node == expected


def test_scalar_key_still_takes_bracketed_text(home):
    # A string key is not subject to the list rule, whatever the value looks like.
    _set(home, "dashboard.url", "[a,b]")
    assert _read(home / "config.json")["dashboard"]["url"] == "[a,b]"


def test_list_key_detection():
    assert _is_list_key(TRUSTED)
    assert _is_list_key("telegram.accounts.main.allowed_user_ids")  # wildcard path
    assert not _is_list_key("agent.yolo")
    assert not _is_list_key("dashboard.url")


def test_a_stored_list_under_an_undeclared_key_is_still_a_list(home):
    (home / "config.json").write_text(json.dumps({"custom": {"things": ["x"]}}), encoding="utf-8")
    with (
        patch("kiro_crew.config.loader.config_path", return_value=home / "config.json"),
        patch("kiro_crew.config.loader.config_dir", return_value=home),
        patch("kiro_crew.cli_config.KiroCrewConfig.load") as load,
    ):
        load.return_value.to_dict.return_value = {"custom": {"things": ["x"]}}
        assert _is_list_key("custom.things")
        assert not _is_list_key("custom.other")


def test_parse_list_value_returns_the_json_list_or_refuses():
    assert _parse_list_value(TRUSTED, '["a", "b"]') == ["a", "b"]
    with pytest.raises(ValueError, match="expected a JSON array"):
        _parse_list_value(TRUSTED, "[a,b]")
