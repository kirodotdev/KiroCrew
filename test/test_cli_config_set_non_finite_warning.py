"""``kirocrew config set`` warns when a value is ``NaN`` or infinite.

``nan``, ``inf`` and an overflowing ``1e999`` parse as floats, pass a number
key's type check and are written as the JSON tokens ``NaN`` and ``Infinity``.
The loader's number reader replaces a non-finite value with the key's default,
so the ``✅`` line names a value that never takes effect. The write still
succeeds, as it does today; the warning names the key and says so.

Every test writes under a temp data home; none touches ``~/.kiro/crew``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.cli_config import _config_cmd

KEY = "session.autocompact_pct"


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


_WARNING = "is not a finite number: it is saved, but a number setting loads it as its default"


@pytest.mark.parametrize("raw, token", [("nan", "NaN"), ("inf", "Infinity"), ("1e999", "Infinity")])
@pytest.mark.parametrize("local", [False, True])
def test_a_non_finite_number_is_saved_with_a_warning_naming_the_key(
    home, capsys, raw, token, local
):
    _set(home, KEY, raw, local=local)
    out, err = capsys.readouterr()
    stored = (home / ("config.local.json" if local else "config.json")).read_text(encoding="utf-8")
    assert f'"autocompact_pct": {token}' in stored
    assert out.startswith("✅ ")
    assert f"⚠️  {KEY} {_WARNING}, so it will not take effect." in err


def test_a_non_finite_number_inside_a_section_names_its_path(home, capsys):
    _set(home, "session", json.dumps({"autocompact_pct": 70}).replace("70", "NaN"))
    err = capsys.readouterr().err
    assert f"⚠️  {KEY} {_WARNING}" in err


def test_a_finite_number_warns_nothing(home, capsys):
    _set(home, KEY, "80")
    assert "finite" not in capsys.readouterr().err
