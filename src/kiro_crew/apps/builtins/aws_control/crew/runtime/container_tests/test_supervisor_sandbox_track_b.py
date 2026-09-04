"""Track B pin for the most dangerous truthiness site: the sandbox opt-in.

``_unsandboxed_exec_opted_in`` reads ``agent.sandbox_allow_unsandboxed_exec`` from
the data home. ``bool("false")`` is truthy, so a config that said the STRING
"false" -- meaning off -- would be read as CONSENT to run the model subprocess
UNSANDBOXED, the single worst direction to be wrong in. The fix requires a real
boolean: ``True`` opts in, ``False`` does not, and anything else is refused loudly
(fail-closed -- a refused boot never runs a turn, let alone an unsandboxed one).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from container.common import Settings
from container.common.config import ConfigError
from container.supervisor import __main__ as entry


def make_settings(tmp_path: Path) -> Settings:
    data_home = tmp_path / "data"
    data_home.mkdir(parents=True, exist_ok=True)
    return Settings(
        backend_port=8765,
        backend_run_dir=data_home / "run",
        front_port=8080,
        route_prefix="",
        control_secret=None,
        data_home=data_home,
        config_dir=data_home,
        crew_name="test-crew",
        backup_bucket=None,
        backup_prefix="",
        backup_interval_secs=30,
        allow_unsandboxed_exec=False,
    )


def _write_config(settings: Settings, value, *, name: str = "config.json") -> None:
    (settings.data_home / name).write_text(
        json.dumps({"agent": {"sandbox_allow_unsandboxed_exec": value}}), encoding="utf-8"
    )


def test_string_false_is_not_read_as_consent(tmp_path: Path) -> None:
    """The reproduction: the STRING "false" must not opt in to unsandboxed exec.

    It is refused rather than silently read as "no" so the operator learns their
    config does not say what they meant.
    """
    settings = make_settings(tmp_path)
    _write_config(settings, "false")
    with pytest.raises(ConfigError, match="non-boolean"):
        entry._unsandboxed_exec_opted_in(settings)


@pytest.mark.parametrize("bad", ["true", "1", "yes", 1, 0, "off"])
def test_any_non_boolean_is_refused(tmp_path: Path, bad) -> None:
    settings = make_settings(tmp_path)
    _write_config(settings, bad)
    with pytest.raises(ConfigError, match="non-boolean"):
        entry._unsandboxed_exec_opted_in(settings)


def test_real_booleans_are_honoured(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    _write_config(settings, True)
    assert entry._unsandboxed_exec_opted_in(settings) is True
    _write_config(settings, False)
    assert entry._unsandboxed_exec_opted_in(settings) is False


def test_absent_key_defaults_to_not_opted_in(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    (settings.data_home / "config.json").write_text(json.dumps({"agent": {}}), encoding="utf-8")
    assert entry._unsandboxed_exec_opted_in(settings) is False


def test_the_guard_refuses_to_boot_on_string_false(tmp_path: Path) -> None:
    """End to end: with no user namespace and a stringy opt-in, boot is refused
    rather than proceeding UNSANDBOXED."""
    settings = make_settings(tmp_path)
    _write_config(settings, "false")
    with pytest.raises(ConfigError, match="non-boolean"):
        entry.verify_sandbox(settings, probe=lambda: False)


def test_REVERT_string_false_would_opt_in() -> None:
    """The reddening: the reverted ``bool(...)`` coercion reads "false" as consent."""
    assert (
        bool("false") is True
    ), "the pre-fix coercion would enable UNSANDBOXED execution on the string 'false'"
