"""Channels folded into core: the ``channels`` app record is retired at boot.

The record held one thing the feature kept per install, whether it was on. Unlike the
other folded switches it defaulted OFF (the app was hidden and ``defaultEnabled:
false``), so the boot cleanup (``cleanup_migrated_builtin``) carries an ON record into
``dashboard.channels_enabled`` before it retires the record, writes nothing for an off
one, keeps the record beside the app's ``data/`` as ``<file>.retired``, and is a no-op
on every later start. ``kirocrew app enable channels`` and ``app disable channels``
keep working by writing the setting.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest
from test_config_patch import _make_app_with_state, _patch, tmp_config  # noqa: F401

from kiro_crew.apps import protected_app_state
from kiro_crew.apps.builtin_fold import set_folded_switch
from kiro_crew.apps.builtins import (
    _FOLDED_BUILTIN_SWITCHES,
    _FOLDED_OFF_BY_DEFAULT,
    _MIGRATED_BUILTINS,
)
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    INSTALLED_META_FILENAME,
    RETIRED_RECORD_SUFFIX,
    app_dir,
    cleanup_migrated_builtin,
    detect_orphaned_builtins,
    get_app,
    list_apps,
    shipped_builtin_names,
    uninstall_app,
)
from kiro_crew.config.loader import KiroCrewConfig, config_path

NAME = "channels"
SWITCH = "dashboard.channels_enabled"


def _seed_record(*, enabled: bool, origin: str = "builtin") -> Path:
    """The record a gateway that still shipped Channels as an app left behind."""
    root = app_dir(NAME)
    (root / "data").mkdir(parents=True)
    (root / "data" / "keep.txt").write_text("keep me", encoding="utf-8")
    record = {
        "name": NAME,
        "version": "1.0.0",
        "displayName": "Channels",
        "enabled": enabled,
        "installedAt": "2026-01-01T00:00:00Z",
        "source": "builtin" if origin == "builtin" else str(root.parent / "my-channels"),
        "origin": origin,
        "resources": "gateway",
        "lifecycle": "locked" if origin == "builtin" else "unlocked",
    }
    (root / INSTALLED_META_FILENAME).write_text(json.dumps(record), encoding="utf-8")
    (root / APP_MANIFEST_FILENAME).write_text(
        json.dumps({"name": NAME, "version": "1.0.0"}), encoding="utf-8"
    )
    return root


def _config() -> dict[str, Any]:
    path = config_path()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _write_config(data: dict[str, Any]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_channels_is_a_folded_builtin_that_defaults_off() -> None:
    assert NAME in _MIGRATED_BUILTINS
    assert _FOLDED_BUILTIN_SWITCHES[NAME] == SWITCH
    assert NAME in _FOLDED_OFF_BY_DEFAULT
    assert NAME not in shipped_builtin_names()
    assert KiroCrewConfig().dashboard.channels_enabled is False


def test_an_enabled_record_turns_the_setting_on_and_stays_as_a_backup() -> None:
    root = _seed_record(enabled=True)
    record_text = (root / INSTALLED_META_FILENAME).read_text(encoding="utf-8")

    result = cleanup_migrated_builtin(NAME)

    assert result.ok, result.error
    assert _config()["dashboard"] == {"channels_enabled": True}
    assert KiroCrewConfig.load().dashboard.channels_enabled is True
    assert not (root / INSTALLED_META_FILENAME).exists()
    assert not (root / APP_MANIFEST_FILENAME).exists()
    backup = root / (INSTALLED_META_FILENAME + RETIRED_RECORD_SUFFIX)
    assert backup.read_text(encoding="utf-8") == record_text
    assert (root / (APP_MANIFEST_FILENAME + RETIRED_RECORD_SUFFIX)).is_file()
    assert (root / "data" / "keep.txt").read_text(encoding="utf-8") == "keep me"
    # Not an app anywhere: not listed, not an orphan the nav sends to Migrate.
    assert get_app(NAME) is None
    assert NAME not in {a["name"] for a in list_apps()}
    assert NAME not in detect_orphaned_builtins(force_refresh=True)


def test_a_disabled_record_writes_no_setting() -> None:
    root = _seed_record(enabled=False)

    assert cleanup_migrated_builtin(NAME).ok

    assert "dashboard" not in _config()
    assert KiroCrewConfig.load().dashboard.channels_enabled is False
    assert (root / (INSTALLED_META_FILENAME + RETIRED_RECORD_SUFFIX)).is_file()


def test_the_owners_protected_disable_keeps_it_off() -> None:
    # installed.json says on, but the owner's disable outside the app tree stands.
    _seed_record(enabled=True)
    protected_app_state.set_owner_disabled(NAME, True)

    assert cleanup_migrated_builtin(NAME).ok

    assert "dashboard" not in _config()
    assert KiroCrewConfig.load().dashboard.channels_enabled is False


def test_a_second_start_changes_nothing() -> None:
    root = _seed_record(enabled=True)
    assert cleanup_migrated_builtin(NAME).ok
    config_before = config_path().read_bytes()
    backup = root / (INSTALLED_META_FILENAME + RETIRED_RECORD_SUFFIX)
    backup_before = backup.read_bytes()

    again = cleanup_migrated_builtin(NAME)

    assert again.ok and "nothing to clean up" in again.message
    assert config_path().read_bytes() == config_before
    assert backup.read_bytes() == backup_before


def test_a_setting_already_in_the_file_is_kept() -> None:
    _seed_record(enabled=True)
    _write_config({"dashboard": {"channels_enabled": False, "show_unread_dot": False}})

    assert cleanup_migrated_builtin(NAME).ok

    assert _config()["dashboard"] == {"channels_enabled": False, "show_unread_dot": False}


def test_a_failed_setting_write_keeps_the_record_for_the_next_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _seed_record(enabled=True)

    def fail(*_args: Any, **_kwargs: Any) -> dict:
        raise OSError("disk full")

    monkeypatch.setattr("kiro_crew.config.loader.update_config_locked", fail)

    result = cleanup_migrated_builtin(NAME)

    assert not result.ok and result.error_code == "io_error"
    assert (root / INSTALLED_META_FILENAME).is_file()
    assert get_app(NAME) is not None


def test_removing_the_retired_record_from_migrate_carries_the_switch() -> None:
    _seed_record(enabled=True)

    result = uninstall_app(NAME, retired_builtin=True)

    assert result.ok, result.error
    assert KiroCrewConfig.load().dashboard.channels_enabled is True


def test_a_default_on_switch_still_carries_only_an_opt_out() -> None:
    # The default-off row must not turn the other folded switches into opt-ins.
    from kiro_crew.apps.builtin_fold import carry_folded_switch

    carry_folded_switch("projects", True)
    assert "taskrunner" not in _config()
    carry_folded_switch("projects", False)
    assert _config()["taskrunner"] == {"enabled": False}


@pytest.mark.parametrize("on", [True, False])
def test_app_enable_and_disable_write_the_setting(on: bool) -> None:
    _write_config({"dashboard": {"channels_enabled": not on}})

    assert set_folded_switch(NAME, on) == SWITCH

    assert KiroCrewConfig.load().dashboard.channels_enabled is on


def test_app_enable_writes_the_setting_while_the_old_record_still_stands() -> None:
    # No gateway has started since the upgrade, so the record is not retired yet. The
    # written key is the user's choice, and the later carry keeps it.
    _seed_record(enabled=False)

    assert set_folded_switch(NAME, True) == SWITCH
    assert cleanup_migrated_builtin(NAME).ok

    assert KiroCrewConfig.load().dashboard.channels_enabled is True


def test_an_app_a_user_installed_under_the_old_name_is_its_own_app() -> None:
    _seed_record(enabled=False, origin="local")

    assert set_folded_switch(NAME, True) is None

    assert "dashboard" not in _config()


def test_a_name_that_never_folded_is_left_to_the_ordinary_path() -> None:
    assert set_folded_switch("file-explorer", True) is None
    assert not config_path().exists()


@pytest.mark.parametrize("action,on", [("enable", True), ("disable", False)])
def test_the_cli_answers_for_the_folded_app_without_a_gateway(
    action: str, on: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from kiro_crew import cli_commands_app

    def no_gateway(*_args: Any, **_kwargs: Any) -> bool:
        raise AssertionError("a folded app's switch is not an app the gateway knows")

    monkeypatch.setattr(cli_commands_app, "_run_app_action_through_gateway", no_gateway)

    cli_commands_app._handle_app(argparse.Namespace(app_action=action, name=NAME))

    assert KiroCrewConfig.load().dashboard.channels_enabled is on
    assert SWITCH in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [False, True])
async def test_settings_writes_a_boolean(tmp_config, value) -> None:  # noqa: F811
    app, _ = _make_app_with_state()
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await _patch(client, SWITCH, value)
        assert resp.status == 200
        refused = await _patch(client, SWITCH, "true")
        assert refused.status == 400
    assert KiroCrewConfig.load().dashboard.channels_enabled is value
