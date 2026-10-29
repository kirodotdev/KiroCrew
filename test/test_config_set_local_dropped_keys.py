"""``config set --local`` names the overlay keys a whole-object write removes.

The replace itself is kept: it is the only CLI way to remove a key the overlay
pins. What a person must see is which pinned keys it took away, because a dropped
``slack.allowed_users`` silently falls back to ``config.json`` and changes who can
reach the gateway.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import cli_config


def _set_local(tmp_path: Path, overlay: dict, key: str, value: str) -> dict:
    config_dir = tmp_path / ".kirocrew"
    config_dir.mkdir()
    (config_dir / "config.json").write_text("{}", encoding="utf-8")
    local_file = config_dir / "config.local.json"
    local_file.write_text(json.dumps(overlay), encoding="utf-8")
    args = argparse.Namespace(config_action="set", key=key, value=value, file=None, local=True)
    with (
        patch("kiro_crew.config.loader.config_dir", return_value=config_dir),
        patch("kiro_crew.cli_config.config_local_path", return_value=local_file),
        patch("kiro_crew.cli_config.sel"),
    ):
        cli_config._config_cmd(args)
    return json.loads(local_file.read_text(encoding="utf-8"))


def test_a_whole_object_write_names_the_pinned_keys_it_drops(tmp_path, capsys) -> None:
    data = _set_local(
        tmp_path, {"slack": {"allowed_users": ["U1"]}}, "slack", '{"open_channels": ["C1"]}'
    )
    assert data["slack"] == {"open_channels": ["C1"]}
    err = capsys.readouterr().err
    assert "slack.allowed_users" in err
    assert "kirocrew config set --local slack.<name> <value>" in err


def test_a_dotted_leaf_write_drops_nothing_and_says_nothing(tmp_path, capsys) -> None:
    data = _set_local(
        tmp_path, {"slack": {"allowed_users": ["U1"]}}, "slack.dm_activation", "mention"
    )
    assert data["slack"]["allowed_users"] == ["U1"]
    assert capsys.readouterr().err == ""


def test_an_object_that_keeps_every_key_says_nothing(tmp_path, capsys) -> None:
    _set_local(
        tmp_path,
        {"slack": {"allowed_users": ["U1"]}},
        "slack",
        '{"allowed_users": ["U2"], "open_channels": ["C1"]}',
    )
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("old", "new", "dropped"),
    [
        ({"a": 1, "b": 2}, {"a": 3}, ["s.b"]),
        ({"a": {"x": 1, "y": 2}}, {"a": {"x": 1}}, ["s.a.y"]),
        ({"a": 1}, "scalar", ["s.a"]),
        ({"a": 1}, {"a": 1, "b": 2}, []),
        ("scalar", {"a": 1}, []),
        (object(), {"a": 1}, []),  # the key was absent: _dict_get's sentinel
    ],
)
def test_dropped_keys_are_named_by_their_full_dotted_path(old, new, dropped) -> None:
    assert cli_config._overlay_keys_dropped(old, new, "s") == dropped
