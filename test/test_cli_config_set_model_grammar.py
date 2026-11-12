"""`config set` refuses a knowledge extraction model id the Settings page refuses.

The installer copies `knowledge.extraction_model` into the knowledge agent's
spec as written, so the CLI holds it to the Settings spec's length bound and
pattern instead of storing it. The other model pins keep taking any id.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest


def _run(config_dir: Path, key: str, value: str, *, local: bool = False) -> None:
    from kiro_crew.cli_config import _config_cmd

    args = argparse.Namespace(config_action="set", key=key, value=value, file=None, local=local)
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


def _config(tmp_path: Path) -> Path:
    config_dir = tmp_path / ".kirocrew"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps({"knowledge": {"extraction_model": "auto"}}), encoding="utf-8"
    )
    return config_dir


def _saved(config_dir: Path) -> dict:
    return json.loads((config_dir / "config.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("knowledge.extraction_model", "provider//model;x"),
        ("knowledge.extraction_model", "my model"),
        ("knowledge.extraction_model", "m" * 65),
        ("knowledge", '{"extraction_model": "provider//model;x"}'),
    ],
)
def test_malformed_extraction_model_is_refused(
    tmp_path: Path, capsys, key: str, value: str, local: bool
) -> None:
    config_dir = _config(tmp_path)

    with pytest.raises(SystemExit) as exc:
        _run(config_dir, key, value, local=local)

    assert exc.value.code == 1
    assert f"{key}:" in capsys.readouterr().err
    assert _saved(config_dir)["knowledge"]["extraction_model"] == "auto"
    assert not (config_dir / "config.local.json").exists()


@pytest.mark.parametrize(
    ("key", "value", "stored"),
    [
        ("knowledge.extraction_model", "model-x-1.5", "model-x-1.5"),
        ("knowledge.extraction_model", "openrouter/model-x", "openrouter/model-x"),
        ("knowledge.extraction_model", "auto", "auto"),
        ("knowledge.extraction_model", "", ""),
        ("knowledge", '{"extraction_model": "model-x-1.5"}', "model-x-1.5"),
    ],
)
def test_well_formed_extraction_model_is_written(
    tmp_path: Path, key: str, value: str, stored: str
) -> None:
    config_dir = _config(tmp_path)

    _run(config_dir, key, value)

    assert _saved(config_dir)["knowledge"]["extraction_model"] == stored


def test_another_model_pin_takes_an_id_no_catalog_claims(tmp_path: Path) -> None:
    config_dir = _config(tmp_path)

    _run(config_dir, "agent.model", "us.vendor.model-x-v1:0")

    assert _saved(config_dir)["agent"]["model"] == "us.vendor.model-x-v1:0"
