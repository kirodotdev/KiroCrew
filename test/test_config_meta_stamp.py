"""``config.json`` always carries a ``meta`` stamp naming the build that wrote it.

The stamp is the first thing to check when a config looks like it came from an
older schema, so a write must never leave the file without one. The hazard is
specific to writers that rebuild the file from ``KiroCrewConfig.to_dict()``:
that mapping models the schema only, so any top-level key the dataclass does
not carry — ``meta`` among them — is absent from the output and the write drops
it. Writers that mutate the raw dict they read keep the block for free.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import __version__
from kiro_crew.config.loader import stamp_config_meta

_OLD_STAMP = {"lastTouchedVersion": "0.0.1", "lastTouchedAt": "2020-01-01T00:00:00+00:00"}

# Values a user would notice losing, spread over several sections so a write
# that rebuilds the file has something to preserve besides the stamp.
_REAL_SETTINGS = {
    "agent": {"approval_mode": "interactive", "max_subagents": 8},
    "dashboard": {"theme_mode": "dark", "language": "zh-CN"},
    "session": {"timeout_secs": 7200},
    "timezone": "Asia/Shanghai",
}


def _set_args(key: str, value: str, *, local: bool = False) -> argparse.Namespace:
    return argparse.Namespace(config_action="set", key=key, value=value, file=None, local=local)


def _run_config_cmd(args: argparse.Namespace, config_dir: Path) -> None:
    from kiro_crew.cli_config import _config_cmd

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


class TestStampConfigMeta:
    def test_stamps_current_version_and_a_timestamp(self):
        out = stamp_config_meta({"timezone": "UTC"})
        assert out["meta"]["lastTouchedVersion"] == __version__
        assert out["meta"]["lastTouchedAt"]

    def test_replaces_an_existing_stamp_rather_than_merging(self):
        """The block names the build writing now, so a stale one cannot survive."""
        out = stamp_config_meta({"meta": _OLD_STAMP, "timezone": "UTC"})
        assert out["meta"]["lastTouchedVersion"] == __version__
        assert out["meta"]["lastTouchedAt"] != _OLD_STAMP["lastTouchedAt"]

    def test_keeps_every_other_key(self):
        out = stamp_config_meta({"meta": _OLD_STAMP, **_REAL_SETTINGS})
        assert {k: v for k, v in out.items() if k != "meta"} == _REAL_SETTINGS

    def test_meta_is_written_first(self):
        """Leading position keeps the stamp readable in a hand-opened config."""
        assert next(iter(stamp_config_meta(_REAL_SETTINGS))) == "meta"

    def test_does_not_mutate_the_input(self):
        source = {"meta": _OLD_STAMP, "timezone": "UTC"}
        stamp_config_meta(source)
        assert source["meta"] == _OLD_STAMP


class TestCliConfigSetKeepsMeta:
    """``kirocrew config set <key> <value>`` rebuilds the file from the dataclass."""

    def test_set_refreshes_the_stamp_instead_of_dropping_it(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "crew"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(
            json.dumps({"meta": _OLD_STAMP, **_REAL_SETTINGS}), encoding="utf-8"
        )

        _run_config_cmd(_set_args("timezone", "Europe/Berlin"), config_dir)

        saved = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
        assert saved["meta"]["lastTouchedVersion"] == __version__
        assert saved["meta"]["lastTouchedAt"] != _OLD_STAMP["lastTouchedAt"]
        assert saved["timezone"] == "Europe/Berlin"

    def test_set_stamps_a_config_that_never_had_meta(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "crew"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(json.dumps(_REAL_SETTINGS), encoding="utf-8")

        _run_config_cmd(_set_args("timezone", "Europe/Berlin"), config_dir)

        saved = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
        assert saved["meta"]["lastTouchedVersion"] == __version__

    def test_set_from_file_stamps_the_written_config(self, tmp_path: Path) -> None:
        """``--file`` replaces the whole config, so it owns the new stamp too."""
        config_dir = tmp_path / "crew"
        config_dir.mkdir()
        source = tmp_path / "incoming.json"
        source.write_text(json.dumps({"meta": _OLD_STAMP, **_REAL_SETTINGS}), encoding="utf-8")

        args = argparse.Namespace(
            config_action="set", key=None, value=None, file=str(source), local=False
        )
        _run_config_cmd(args, config_dir)

        saved = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
        assert saved["meta"]["lastTouchedVersion"] == __version__
        assert saved["timezone"] == "Asia/Shanghai"

    def test_local_set_does_not_stamp_the_overlay(self, tmp_path: Path) -> None:
        """Only the base file carries the block; the overlay holds settings alone."""
        config_dir = tmp_path / "crew"
        config_dir.mkdir()

        _run_config_cmd(_set_args("timezone", "Europe/Berlin", local=True), config_dir)

        overlay = json.loads((config_dir / "config.local.json").read_text(encoding="utf-8"))
        assert "meta" not in overlay
        assert overlay["timezone"] == "Europe/Berlin"

    def test_set_from_file_refuses_a_non_object_payload(self, tmp_path: Path, capsys) -> None:
        """A JSON array parses but is not a config: refuse, leaving the file alone."""
        config_dir = tmp_path / "crew"
        config_dir.mkdir()
        existing = {"meta": _OLD_STAMP, **_REAL_SETTINGS}
        (config_dir / "config.json").write_text(json.dumps(existing), encoding="utf-8")
        source = tmp_path / "incoming.json"
        source.write_text("[1, 2, 3]", encoding="utf-8")

        args = argparse.Namespace(
            config_action="set", key=None, value=None, file=str(source), local=False
        )
        with pytest.raises(SystemExit) as exc:
            _run_config_cmd(args, config_dir)

        assert exc.value.code == 1
        assert "Not a config object" in capsys.readouterr().err
        assert json.loads((config_dir / "config.json").read_text(encoding="utf-8")) == existing


class TestConfigSaveKeepsMeta:
    def test_save_stamps_the_current_build(self, tmp_path: Path) -> None:
        from kiro_crew.config import KiroCrewConfig

        config_dir = tmp_path / "crew"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(
            json.dumps({"meta": _OLD_STAMP, **_REAL_SETTINGS}), encoding="utf-8"
        )

        with (
            patch(
                "kiro_crew.config.loader.config_path", return_value=config_dir / "config.json"
            ),
            patch(
                "kiro_crew.config.loader.config_local_path",
                return_value=config_dir / "config.local.json",
            ),
            patch("kiro_crew.config.loader.config_dir", return_value=config_dir),
        ):
            KiroCrewConfig.load().save()
            saved = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))

        assert saved["meta"]["lastTouchedVersion"] == __version__
        assert saved["agent"]["approval_mode"] == "interactive"
