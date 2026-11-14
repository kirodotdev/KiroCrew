"""The Command Bar left the app catalogue for a core setting, and nobody's choice moved.

``command-bar`` was a built-in app whose enabled state was the only switch for the
launcher. It is now ``dashboard.command_bar_enabled`` (default on), the app is a
migrated built-in, and the boot cleanup carries a record's opt-out into that setting
before removing the record (``apps/builtin_fold.py``). These tests pin that a user who
had the launcher keeps it, a user who turned it off keeps the palette, a record still
owed its default-on promotion reads as on, and a failed write keeps the record.
"""

from __future__ import annotations

import json

import pytest
from app_manager_helpers import app_home_fixture  # noqa: F401 - fixture
from test_config_patch import _make_app_with_state, _patch, tmp_config  # noqa: F401

from kiro_crew.config import KiroCrewConfig

NAME = "command-bar"
KEY = "command_bar_enabled"


def _seed_record(home, **fields) -> None:
    app = home / "apps" / NAME
    (app / "data").mkdir(parents=True)
    (app / "data" / "keep.txt").write_text("kept", encoding="utf-8")
    record = {"name": NAME, "version": "0.1.0", "origin": "builtin", "source": "builtin"}
    record.update(fields)
    (app / "installed.json").write_text(json.dumps(record), encoding="utf-8")
    (app / "app.json").write_text(json.dumps({"name": NAME}), encoding="utf-8")


def _dashboard(home) -> dict:
    data = json.loads((home / "config.json").read_text(encoding="utf-8"))
    return data.get("dashboard", {})


def _cleanup():
    from kiro_crew.apps.manager import cleanup_migrated_builtin

    return cleanup_migrated_builtin(NAME)


def test_the_setting_defaults_on(tmp_config) -> None:  # noqa: F811
    assert KiroCrewConfig().dashboard.command_bar_enabled is True
    assert KiroCrewConfig.load().dashboard.command_bar_enabled is True


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [False, True])
async def test_settings_writes_a_boolean(tmp_config, value) -> None:  # noqa: F811
    app, _ = _make_app_with_state()
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await _patch(client, f"dashboard.{KEY}", value)
        assert resp.status == 200
        refused = await _patch(client, f"dashboard.{KEY}", "false")
        assert refused.status == 400
    assert KiroCrewConfig.load().dashboard.command_bar_enabled is value


def test_the_app_is_a_migrated_builtin_that_no_longer_ships() -> None:
    from kiro_crew.apps.builtins import _FOLDED_BUILTIN_SWITCHES, _MIGRATED_BUILTINS
    from kiro_crew.apps.manager import (
        _DEFAULT_ON_BACKFILL,
        _DEFAULT_ON_BUILTINS,
        shipped_builtin_names,
    )

    assert NAME in _MIGRATED_BUILTINS
    assert NAME not in shipped_builtin_names()
    assert NAME not in _DEFAULT_ON_BUILTINS | _DEFAULT_ON_BACKFILL
    # Every folded switch names a real config flag that defaults on, as the app
    # did, because the carry writes only an opt-out.
    for name, path in _FOLDED_BUILTIN_SWITCHES.items():
        assert name in _MIGRATED_BUILTINS
        value: object = KiroCrewConfig()
        for part in path.split("."):
            value = getattr(value, part)
        assert value is True, path


def test_an_enabled_record_is_removed_and_writes_nothing(app_home) -> None:  # noqa: F811
    _seed_record(app_home, enabled=True, defaultOnBackfilled=True)

    result = _cleanup()

    assert result.ok, result.error
    assert not (app_home / "apps" / NAME / "installed.json").exists()
    assert (app_home / "apps" / NAME / "data" / "keep.txt").read_text() == "kept"
    assert KEY not in _dashboard(app_home)
    assert KiroCrewConfig.load().dashboard.command_bar_enabled is True


def test_a_disabled_record_keeps_the_palette(app_home) -> None:  # noqa: F811
    _seed_record(app_home, enabled=False, defaultOnBackfilled=True)

    result = _cleanup()

    assert result.ok, result.error
    assert not (app_home / "apps" / NAME / "installed.json").exists()
    assert _dashboard(app_home)[KEY] is False
    assert KiroCrewConfig.load().dashboard.command_bar_enabled is False


def test_a_record_owed_its_promotion_reads_as_on(app_home) -> None:  # noqa: F811
    """The old build's backfill turned such a record on at its next start."""
    _seed_record(app_home, enabled=False)

    assert _cleanup().ok

    assert KEY not in _dashboard(app_home)


def test_the_owners_disable_outranks_an_owed_promotion(app_home) -> None:  # noqa: F811
    """`kirocrew app disable command-bar` before the promotion ran kept the palette.

    The disable sits in the protected store, which every record read applied over the
    backfill's ``enabled: true``, so the carry writes the opt-out.
    """
    from kiro_crew.apps import protected_app_state

    _seed_record(app_home, enabled=False)
    protected_app_state.set_owner_disabled(NAME, True)

    assert _cleanup().ok

    assert _dashboard(app_home)[KEY] is False


def test_a_promotion_the_policy_withheld_stays_off(app_home, monkeypatch) -> None:  # noqa: F811
    import kiro_crew.apps.manager as manager

    # Through the mirrored facade, so the patch reaches the owner that reads it.
    monkeypatch.setattr(manager, "_app_activation_denied", lambda name, **_: "denied")
    _seed_record(app_home, enabled=False)

    assert _cleanup().ok

    assert _dashboard(app_home)[KEY] is False


def test_a_setting_already_chosen_is_kept(app_home) -> None:  # noqa: F811
    data = json.loads((app_home / "config.json").read_text(encoding="utf-8"))
    data["dashboard"] = {KEY: True}
    (app_home / "config.json").write_text(json.dumps(data), encoding="utf-8")
    _seed_record(app_home, enabled=False, defaultOnBackfilled=True)

    assert _cleanup().ok

    assert _dashboard(app_home)[KEY] is True


def test_a_failed_write_keeps_the_record_for_the_next_start(
    app_home, monkeypatch
) -> None:  # noqa: F811
    import kiro_crew.config.loader as loader

    def _fail(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(loader, "update_config_locked", _fail)
    _seed_record(app_home, enabled=False, defaultOnBackfilled=True)

    result = _cleanup()

    assert not result.ok
    assert (app_home / "apps" / NAME / "installed.json").is_file()


def test_a_user_install_under_the_name_is_left_alone(app_home) -> None:  # noqa: F811
    _seed_record(app_home, enabled=False, origin="local", source="/tmp/x")

    assert _cleanup().ok

    assert (app_home / "apps" / NAME / "installed.json").is_file()
    assert KEY not in _dashboard(app_home)


def test_the_retired_builtin_uninstall_carries_the_opt_out_too(app_home) -> None:  # noqa: F811
    from kiro_crew.apps.manager import uninstall_app

    _seed_record(app_home, enabled=False, defaultOnBackfilled=True)

    result = uninstall_app(NAME, retired_builtin=True)

    assert result.ok, result.error
    assert _dashboard(app_home)[KEY] is False
