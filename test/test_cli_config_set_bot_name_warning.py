"""``kirocrew config set agent.bot_name`` warns when the loader will change the name.

``config set`` stores and echoes the raw value, but the loader runs ``bot_name``
through ``_sanitize_bot_name`` before any session reads it — dropping emoji,
symbols and braces, and capping the length. Without a warning the user learns
nothing until a later ``config get`` returns a different name, or, when every
character is dropped, until the default name appears instead. The warning names
the spelling the name will load as; the write itself still succeeds.

Non-ASCII fixtures are spelled as escapes so the file stays ASCII.

Every test writes under a temp data home; none touches ``~/.kiro/crew``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.cli_config import _config_cmd

BOT_NAME = "agent.bot_name"
_ROBOT = "\U0001f916"  # ROBOT FACE emoji (category So), dropped by the sanitizer
_SUPER2 = "\u00b2"  # SUPERSCRIPT TWO (No), a symbol drawn from a digit, dropped


def _set(home: Path, key: str, value: str, *, local: bool = False) -> None:
    args = argparse.Namespace(
        config_action="set",
        key=key,
        value=value,
        value_base64=None,
        file=None,
        local=local,
    )
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


class TestBotNameWarning:
    def test_dropped_characters_warn_with_the_loaded_spelling(self, home, capsys):
        _set(home, BOT_NAME, "Kiro " + _ROBOT)
        captured = capsys.readouterr()
        # The write succeeded and stored the typed value verbatim.
        assert _read(home / "config.json")["agent"]["bot_name"] == "Kiro " + _ROBOT
        assert "✅" in captured.out
        # The warning on stderr names the spelling the loader will produce.
        assert "⚠️" in captured.err
        assert '"Kiro"' in captured.err
        assert "will load as" in captured.err

    def test_symbol_dropped_warns(self, home, capsys):
        _set(home, BOT_NAME, "Ops" + _SUPER2)
        err = capsys.readouterr().err
        assert "⚠️" in err
        assert '"Ops"' in err

    def test_braces_stripped_warn(self, home, capsys):
        _set(home, BOT_NAME, "Kiro{x}")
        err = capsys.readouterr().err
        assert "⚠️" in err
        assert '"Kirox"' in err

    def test_name_emptied_warns_that_the_default_is_used(self, home, capsys):
        _set(home, BOT_NAME, _ROBOT + _ROBOT)
        captured = capsys.readouterr()
        assert _read(home / "config.json")["agent"]["bot_name"] == _ROBOT + _ROBOT
        assert "⚠️" in captured.err
        assert "empty" in captured.err
        assert "default name" in captured.err

    def test_unchanged_name_warns_nothing(self, home, capsys):
        _set(home, BOT_NAME, "Kiro Bot")
        captured = capsys.readouterr()
        assert _read(home / "config.json")["agent"]["bot_name"] == "Kiro Bot"
        assert "⚠️" not in captured.err

    def test_another_key_is_never_warned(self, home, capsys):
        # A different string key carrying a sanitizer-dropped character must not
        # warn: the sanitizer applies to bot_name alone.
        _set(home, "agent.model", "Kiro " + _ROBOT)
        assert "⚠️" not in capsys.readouterr().err

    def test_local_overlay_path_warns_too(self, home, capsys):
        _set(home, BOT_NAME, "Kiro " + _ROBOT, local=True)
        captured = capsys.readouterr()
        assert _read(home / "config.local.json")["agent"]["bot_name"] == "Kiro " + _ROBOT
        assert "saved to config.local.json" in captured.out
        assert "⚠️" in captured.err
        assert '"Kiro"' in captured.err

    def test_local_overlay_empty_name_warns_about_the_default(self, home, capsys):
        _set(home, BOT_NAME, _ROBOT, local=True)
        err = capsys.readouterr().err
        assert "⚠️" in err
        assert "empty" in err
        assert "default name" in err

    def test_numeric_value_warns_it_loads_empty(self, home, capsys):
        # The generic parser coerces "007" to the int 7; a prior string write
        # means no type check rejects it, so it is stored and the loader drops
        # it to "". The warning must still fire on the non-string value.
        _set(home, BOT_NAME, "Kiro")
        capsys.readouterr()
        _set(home, BOT_NAME, "007")
        captured = capsys.readouterr()
        assert _read(home / "config.json")["agent"]["bot_name"] == 7
        assert "⚠️" in captured.err
        assert "empty" in captured.err
        assert "default name" in captured.err

    def test_boolean_value_warns_it_loads_empty(self, home, capsys):
        _set(home, BOT_NAME, "Kiro")
        capsys.readouterr()
        _set(home, BOT_NAME, "true")
        captured = capsys.readouterr()
        assert _read(home / "config.json")["agent"]["bot_name"] is True
        assert "⚠️" in captured.err
        assert "empty" in captured.err

    def test_local_overlay_numeric_value_warns(self, home, capsys):
        # The overlay path takes a non-string even with no prior write.
        _set(home, BOT_NAME, "42", local=True)
        captured = capsys.readouterr()
        assert _read(home / "config.local.json")["agent"]["bot_name"] == 42
        assert "⚠️" in captured.err
        assert "empty" in captured.err
