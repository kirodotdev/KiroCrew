"""Task Runner folded into core: the ``projects`` app record is retired at boot.

The record held one thing the feature kept per install, whether it was on. The boot
cleanup (``cleanup_migrated_builtin``) carries an off record into
``taskrunner.enabled`` before it retires the record, keeps the record beside the
app's ``data/`` as ``<file>.retired``, and is a no-op on every later start.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps import manager, protected_app_state
from kiro_crew.apps.builtins import _FOLDED_BUILTIN_SWITCHES, _MIGRATED_BUILTINS
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
from kiro_crew.taskrunner_switch import TASK_RUNNER_OFF_MESSAGE, task_runner_off

NAME = "projects"


def _seed_record(*, enabled: bool) -> Path:
    """The record a gateway that still shipped Task Runner as an app left behind."""
    root = app_dir(NAME)
    (root / "data").mkdir(parents=True)
    (root / "data" / "keep.txt").write_text("keep me", encoding="utf-8")
    record = {
        "name": NAME,
        "version": "1.0.0",
        "displayName": "Task Runner",
        "enabled": enabled,
        "installedAt": "2026-01-01T00:00:00Z",
        "source": "builtin",
        "origin": "builtin",
        "resources": "gateway",
        "lifecycle": "locked",
    }
    (root / INSTALLED_META_FILENAME).write_text(json.dumps(record), encoding="utf-8")
    (root / APP_MANIFEST_FILENAME).write_text(
        json.dumps({"name": NAME, "version": "1.0.0"}), encoding="utf-8"
    )
    return root


def _config() -> dict[str, Any]:
    path = config_path()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def test_task_runner_is_a_folded_builtin_not_a_shipped_app() -> None:
    assert NAME in _MIGRATED_BUILTINS
    assert _FOLDED_BUILTIN_SWITCHES[NAME] == "taskrunner.enabled"
    assert NAME not in shipped_builtin_names()
    assert NAME not in manager._DEFAULT_ON_BUILTINS


def test_a_disabled_record_turns_the_setting_off_and_stays_as_a_backup() -> None:
    root = _seed_record(enabled=False)
    record_text = (root / INSTALLED_META_FILENAME).read_text(encoding="utf-8")

    result = cleanup_migrated_builtin(NAME)

    assert result.ok, result.error
    assert _config()["taskrunner"] == {"enabled": False}
    assert KiroCrewConfig.load().taskrunner.enabled is False
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


def test_an_enabled_record_writes_no_setting() -> None:
    root = _seed_record(enabled=True)

    assert cleanup_migrated_builtin(NAME).ok

    assert "taskrunner" not in _config()
    assert KiroCrewConfig.load().taskrunner.enabled is True
    assert (root / (INSTALLED_META_FILENAME + RETIRED_RECORD_SUFFIX)).is_file()


def test_the_owners_protected_disable_is_carried_too() -> None:
    # installed.json says on, but the owner's disable outside the app tree stands.
    _seed_record(enabled=True)
    protected_app_state.set_owner_disabled(NAME, True)

    assert cleanup_migrated_builtin(NAME).ok

    assert KiroCrewConfig.load().taskrunner.enabled is False


def test_a_second_start_changes_nothing() -> None:
    root = _seed_record(enabled=False)
    assert cleanup_migrated_builtin(NAME).ok
    config_before = config_path().read_bytes()
    backup = root / (INSTALLED_META_FILENAME + RETIRED_RECORD_SUFFIX)
    backup_before = backup.read_bytes()

    again = cleanup_migrated_builtin(NAME)

    assert again.ok and "nothing to clean up" in again.message
    assert config_path().read_bytes() == config_before
    assert backup.read_bytes() == backup_before


def test_a_setting_already_in_the_file_is_kept() -> None:
    # The shared carry writes only a missing key: one in the file is Settings' own.
    _seed_record(enabled=False)
    path = config_path()
    path.write_text(
        json.dumps({"taskrunner": {"enabled": True, "max_parallel_steps": 2}}), encoding="utf-8"
    )

    assert cleanup_migrated_builtin(NAME).ok

    assert _config()["taskrunner"] == {"enabled": True, "max_parallel_steps": 2}


def test_a_failed_setting_write_keeps_the_record_for_the_next_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _seed_record(enabled=False)

    def fail(*_args: Any, **_kwargs: Any) -> dict:
        raise OSError("disk full")

    monkeypatch.setattr("kiro_crew.config.loader.update_config_locked", fail)

    result = cleanup_migrated_builtin(NAME)

    assert not result.ok and result.error_code == "io_error"
    assert (root / INSTALLED_META_FILENAME).is_file()
    assert get_app(NAME) is not None
    assert task_runner_off() == TASK_RUNNER_OFF_MESSAGE


@pytest.mark.parametrize("owner_disabled", [False, True])
def test_an_off_record_the_cleanup_never_retired_refuses_a_start(owner_disabled: bool) -> None:
    # A gateway started without the dashboard never runs the boot cleanup, so the
    # setting still reads its default and only the record holds the opt-out: an
    # off record, or an on record under the owner's protected disable.
    _seed_record(enabled=owner_disabled)
    if owner_disabled:
        protected_app_state.set_owner_disabled(NAME, True)

    assert KiroCrewConfig.load().taskrunner.enabled is True
    assert task_runner_off() == TASK_RUNNER_OFF_MESSAGE


def test_a_disabled_app_a_user_installed_under_the_old_name_is_not_the_switch() -> None:
    root = _seed_record(enabled=False)
    record = json.loads((root / INSTALLED_META_FILENAME).read_text(encoding="utf-8"))
    record.update(origin="local", source="/tmp/my-projects-app", lifecycle="unlocked")
    (root / INSTALLED_META_FILENAME).write_text(json.dumps(record), encoding="utf-8")

    assert task_runner_off() is None


def test_a_setting_written_while_the_record_still_stands_wins() -> None:
    # `kirocrew config set taskrunner.enabled true` on a gateway that never ran the
    # cleanup: the written key is the choice, as the carry treats it too.
    _seed_record(enabled=False)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"taskrunner": {"enabled": True}}), encoding="utf-8")

    assert task_runner_off() is None


def test_only_the_endorsed_document_counts_as_the_setting_being_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A hand edit the gateway has not endorsed yet is not what the load serves, so it
    # must not count as the written choice either.
    _seed_record(enabled=False)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"taskrunner": {"enabled": False}}), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.read_endorsed_config_text", lambda _p: "{}")
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cls()))

    assert task_runner_off() == TASK_RUNNER_OFF_MESSAGE


def test_an_on_record_the_cleanup_never_retired_lets_a_start_through() -> None:
    _seed_record(enabled=True)

    assert task_runner_off() is None


def test_a_retired_off_record_leaves_the_setting_in_charge() -> None:
    _seed_record(enabled=False)
    assert cleanup_migrated_builtin(NAME).ok
    path = config_path()
    path.write_text(json.dumps({"taskrunner": {"enabled": True}}), encoding="utf-8")

    assert task_runner_off() is None


def test_removing_the_retired_record_from_migrate_carries_the_switch() -> None:
    _seed_record(enabled=False)

    result = uninstall_app(NAME, retired_builtin=True)

    assert result.ok, result.error
    assert KiroCrewConfig.load().taskrunner.enabled is False
