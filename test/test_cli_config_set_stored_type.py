"""`config set` type-checks a declared key that is already stored.

The generic value parser turns `007` into the int `7` and `true` into a bool,
so the declared-type check must run on every write: otherwise a stored string
leaf such as `agent.bot_name` would save a non-string.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest


def _run(config_dir: Path, key: str, value: str) -> None:
    from kiro_crew.cli_config import _config_cmd

    args = argparse.Namespace(config_action="set", key=key, value=value, file=None, local=False)
    with (
        patch("kiro_crew.cli_config.config_path", return_value=config_dir / "config.json"),
        patch(
            "kiro_crew.cli_config.config_local_path",
            return_value=config_dir / "config.local.json",
        ),
        patch("kiro_crew.config.loader.config_dir", return_value=config_dir),
        patch("kiro_crew.cli_config.sel"),
    ):
        _config_cmd(args)


def _config(tmp_path: Path, data: dict) -> Path:
    config_dir = tmp_path / ".kirocrew"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps(data), encoding="utf-8")
    return config_dir


def _saved(config_dir: Path) -> dict:
    return json.loads((config_dir / "config.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("value", ["007", "true", "1.5"])
def test_stored_string_leaf_refuses_a_non_string(tmp_path: Path, capsys, value: str) -> None:
    config_dir = _config(tmp_path, {"agent": {"bot_name": "Kiro"}})

    with pytest.raises(SystemExit) as exc:
        _run(config_dir, "agent.bot_name", value)

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "agent.bot_name: expected string" in err
    assert "quote it as JSON" in err
    assert _saved(config_dir)["agent"]["bot_name"] == "Kiro"


def test_stored_string_leaf_takes_a_json_quoted_number(tmp_path: Path) -> None:
    config_dir = _config(tmp_path, {"agent": {"bot_name": "Kiro"}})

    _run(config_dir, "agent.bot_name", '"007"')

    assert _saved(config_dir)["agent"]["bot_name"] == "007"


def test_stored_integer_leaf_refuses_a_word(tmp_path: Path, capsys) -> None:
    config_dir = _config(tmp_path, {"dashboard": {"terminal": {"max_sessions": 20}}})

    with pytest.raises(SystemExit) as exc:
        _run(config_dir, "dashboard.terminal.max_sessions", "nope")

    assert exc.value.code == 1
    assert "expected integer, got str" in capsys.readouterr().err
    assert _saved(config_dir)["dashboard"]["terminal"]["max_sessions"] == 20


@pytest.mark.parametrize(("value", "expected"), [("auto", "auto"), ("8", 8)])
def test_stored_int_or_str_leaf_takes_both_types(tmp_path: Path, value: str, expected) -> None:
    """`agent.session_start_concurrency` is `int | str`: `auto` or a count."""
    config_dir = _config(tmp_path, {"agent": {"session_start_concurrency": 4}})

    _run(config_dir, "agent.session_start_concurrency", value)

    assert _saved(config_dir)["agent"]["session_start_concurrency"] == expected


def test_stored_int_or_str_leaf_refuses_a_boolean(tmp_path: Path, capsys) -> None:
    config_dir = _config(tmp_path, {"agent": {"session_start_concurrency": 4}})

    with pytest.raises(SystemExit) as exc:
        _run(config_dir, "agent.session_start_concurrency", "true")

    assert exc.value.code == 1
    assert "got boolean" in capsys.readouterr().err


def test_null_on_a_stored_leaf_still_writes(tmp_path: Path) -> None:
    """A `null` on a stored leaf is written, not type-checked."""
    config_dir = _config(tmp_path, {"agent": {"bot_name": "Kiro"}})

    _run(config_dir, "agent.bot_name", "null")

    assert _saved(config_dir)["agent"]["bot_name"] is None


def test_a_preserved_key_no_code_reads_is_still_unknown(tmp_path: Path, capsys) -> None:
    """``decisions.enabled`` is not a field; a hand-written one is only preserved.

    The load keeps such a key so a save does not erase it, which puts it in the
    loaded document. Setting it changes nothing, so it is refused the same way
    as on a config that never had it, and the file is left as it was.
    """
    config_dir = _config(tmp_path, {"decisions": {"enabled": True}})

    with pytest.raises(SystemExit) as exc:
        _run(config_dir, "decisions.enabled", "false")

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Unknown key: decisions.enabled" in err
    assert "does not read it" in err
    assert _saved(config_dir)["decisions"] == {"enabled": True}


def test_a_section_the_core_does_not_model_can_still_be_set(tmp_path: Path) -> None:
    """A whole section an edition or companion owns is not a preserved stray key."""
    config_dir = _config(tmp_path, {"companion_x": {"mode": "a"}})

    _run(config_dir, "companion_x.mode", '"b"')

    assert _saved(config_dir)["companion_x"]["mode"] == "b"


def test_a_named_record_with_a_preserved_key_still_takes_its_real_fields(tmp_path: Path) -> None:
    """A stray key on one ``workspaces`` record does not lock its declared fields."""
    config_dir = _config(tmp_path, {"workspaces": {"default": {"dir": "/a", "note": "x"}}})

    _run(config_dir, "workspaces.default.dir", '"/b"')

    assert _saved(config_dir)["workspaces"]["default"]["dir"] == "/b"


def test_a_preserved_key_on_a_named_record_is_unknown(tmp_path: Path, capsys) -> None:
    config_dir = _config(tmp_path, {"workspaces": {"default": {"dir": "/a", "note": "x"}}})

    with pytest.raises(SystemExit) as exc:
        _run(config_dir, "workspaces.default.note", '"y"')

    assert exc.value.code == 1
    assert "Unknown key: workspaces.default.note" in capsys.readouterr().err
    assert _saved(config_dir)["workspaces"]["default"]["note"] == "x"
