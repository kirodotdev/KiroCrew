"""Keys whose loader silently coerces an unknown value declare that set as an enum.

The write gate (``cli_config._declared_enum_value``) fires only for a key whose
schema declares ``enum``. Without one, ``kirocrew config set
heartbeat.default_deliver telegram`` would store a value the loader reads back as
``slack``. Each test pins one direction of the agreement:
the gate refuses what the loader would drop, and the loader keeps every value the
gate admits.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.schema import SCHEMA_REGISTRY

# (dotted key, enum the loader keeps, a value it would coerce away, attribute path)
_KEYS = [
    ("heartbeat.default_deliver", ["slack", "dashboard"], "telegram", ("heartbeat",)),
    ("messaging.dm_scope", ["per-channel-peer", "unified"], "global", ("messaging",)),
    ("messaging.queue_mode", ["steer", "queue"], "drop", ("messaging",)),
    ("agent.task_store_journal_mode", ["auto", "wal", "delete"], "memory", ("agent",)),
]


@pytest.mark.parametrize(("key", "enum", "_bad", "_attr"), _KEYS)
def test_the_schema_declares_the_set_the_loader_keeps(key, enum, _bad, _attr) -> None:
    entries = {entry.path: entry for entry in SCHEMA_REGISTRY}
    assert list(entries[key].enum_values or []) == enum


@pytest.mark.parametrize(("key", "_enum", "bad", "_attr"), _KEYS)
def test_the_write_gate_refuses_what_the_loader_would_drop(key, _enum, bad, _attr) -> None:
    from kiro_crew.cli_config import _declared_enum_value

    with pytest.raises(ValueError, match="selectable values"):
        _declared_enum_value(key, bad)


def _load(tmp_path: Path, key: str, value: str) -> KiroCrewConfig:
    section, leaf = key.split(".", 1)
    config_dir = tmp_path / ".kirocrew"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps({section: {leaf: value}}))
    with patch("kiro_crew.config.loader.config_dir", return_value=config_dir):
        return KiroCrewConfig.load()


@pytest.mark.parametrize(
    ("key", "value", "attr"),
    [(key, value, attr) for key, enum, _bad, attr in _KEYS for value in enum],
)
def test_the_loader_keeps_every_enum_value(tmp_path: Path, key, value, attr) -> None:
    section = getattr(_load(tmp_path, key, value), attr[0])
    assert getattr(section, key.split(".", 1)[1]) == value


@pytest.mark.parametrize(
    ("key", "stored", "expected", "attr"),
    [
        ("heartbeat.default_deliver", "Dashboard", "dashboard", ("heartbeat",)),
        ("heartbeat.default_deliver", " dashboard ", "dashboard", ("heartbeat",)),
        ("agent.task_store_journal_mode", "WAL", "wal", ("agent",)),
    ],
)
def test_a_stored_spelling_the_loader_folds_still_loads(
    tmp_path: Path, key, stored, expected, attr
) -> None:
    """Load-time schema validation pops an out-of-enum value before the loader's own
    case folding runs, so these spellings are folded first and keep their meaning."""
    section = getattr(_load(tmp_path, key, stored), attr[0])
    assert getattr(section, key.split(".", 1)[1]) == expected


def test_config_set_refuses_the_reported_value_and_writes_nothing(tmp_path: Path, capsys) -> None:
    """The issue's own command: refused with the selectable values, file untouched."""
    from kiro_crew.cli_config import _config_cmd

    config_dir = tmp_path / ".kirocrew"
    config_dir.mkdir()
    before = json.dumps({"heartbeat": {"default_deliver": "slack"}})
    (config_dir / "config.json").write_text(before)
    args = argparse.Namespace(
        config_action="set",
        key="heartbeat.default_deliver",
        value="telegram",
        file=None,
        local=False,
    )
    with (
        patch("kiro_crew.cli_config.config_path", return_value=config_dir / "config.json"),
        patch(
            "kiro_crew.cli_config.config_local_path",
            return_value=config_dir / "config.local.json",
        ),
        patch("kiro_crew.config.loader.config_dir", return_value=config_dir),
        patch("kiro_crew.cli_config.sel"),
        pytest.raises(SystemExit) as exc,
    ):
        _config_cmd(args)

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "telegram" in err and "slack" in err and "dashboard" in err
    assert (config_dir / "config.json").read_text() == before
