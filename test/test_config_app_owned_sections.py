"""Top-level config.json sections a builtin app owns (``validation._APP_OWNED_TOP_KEYS``).

``dev_fleet.repo_path`` is read straight from ``config.json`` by the Dev Fleet app
(``apps/builtins/dev_fleet/repository.py``), and the dashboard's "no checkout
found" banner tells the operator to write exactly that key. The core has no
modelled field for it, so before this set existed every launch logged
``Config: unrecognized top-level keys: dev_fleet`` -- scolding the operator for
following the product's own instructions.

Three properties hold the fix together:

* validation does not report an app-owned section as unrecognized, and still
  reports a genuinely unknown key beside it;
* the section round-trips through ``load()`` -> ``to_dict()`` like any other
  unmodelled section (it is NOT a reserved key, which save() drops);
* every member of the set names a key some app actually reads -- pinned here for
  ``dev_fleet`` against the reader itself, so the set cannot drift into a list of
  silenced typos.

The set is private to ``validation.py``: the unrecognized-key warning is its only
consumer, so it is an exclusion at that site rather than an exported key class.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading

import pytest

from kiro_crew.apps import manager as app_manager
from kiro_crew.config import loader as L
from kiro_crew.config import validation
from kiro_crew.config.loader import _KNOWN_CONFIG_SECTIONS, CONFIG_RESERVED_TOP_KEYS, KiroCrewConfig

_UNRECOGNIZED = "unrecognized top-level keys"


def _unrecognized_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if _UNRECOGNIZED in r.getMessage()]


@pytest.fixture(autouse=True)
def _reset_extension_key_registry():
    for source in ("apps", "plugins"):
        validation.replace_extension_config_keys(source, ())
    yield
    for source in ("apps", "plugins"):
        validation.replace_extension_config_keys(source, ())


def _write_app(root, *, installed: bool) -> None:
    app_root = root / "secretary"
    app_root.mkdir(parents=True)
    (app_root / "app.json").write_text(
        json.dumps(
            {
                "name": "secretary",
                "version": "1.0.0",
                "displayName": "Secretary",
                "description": "Test app",
                "config_keys": ["secretary"],
            }
        )
    )
    if installed:
        (app_root / "installed.json").write_text(
            json.dumps({"name": "secretary", "version": "1.0.0", "enabled": True})
        )


class TestAppOwnedSectionsAreNotUnrecognized:
    @pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_installed_app_manifest_key_is_not_reported(
        self, tmp_path, monkeypatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        apps_root = tmp_path / "apps"
        _write_app(apps_root, installed=True)
        monkeypatch.setattr(app_manager, "apps_dir", lambda: apps_root)
        app_manager.refresh_app_config_keys()
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            validation.validate_config_data({"secretary": {"channel": "slack"}})
        assert _unrecognized_warnings(caplog) == []

    @pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_gateway_load_before_refresh_warns_once_then_loads_cleanly(
        self, tmp_path, monkeypatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The gateway skips the CLI prime; the dashboard executor refreshes later.
        apps_root = tmp_path / "apps"
        _write_app(apps_root, installed=True)
        monkeypatch.setattr(app_manager, "apps_dir", lambda: apps_root)
        data = {"secretary": {"channel": "slack"}}
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            validation.validate_config_data(dict(data))
            assert len(_unrecognized_warnings(caplog)) == 1
            app_manager.refresh_app_config_keys()
            caplog.clear()
            for _ in range(2):
                validation.validate_config_data(dict(data))
        assert _unrecognized_warnings(caplog) == []

    @pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_uninstalled_app_manifest_key_is_reported(
        self, tmp_path, monkeypatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        apps_root = tmp_path / "apps"
        _write_app(apps_root, installed=False)
        monkeypatch.setattr(app_manager, "apps_dir", lambda: apps_root)
        app_manager.refresh_app_config_keys()
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            validation.validate_config_data({"secretary": {"channel": "slack"}})
        warnings = _unrecognized_warnings(caplog)
        assert warnings and "secretary" in warnings[0]

    @pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_repeated_validation_does_not_rescan_installed_apps(
        self, tmp_path, monkeypatch
    ) -> None:
        apps_root = tmp_path / "apps"
        _write_app(apps_root, installed=True)
        monkeypatch.setattr(app_manager, "apps_dir", lambda: apps_root)
        reads: list[str] = []
        real_manifest = app_manager.get_app_manifest

        def _counting_manifest(name: str):
            reads.append(name)
            return real_manifest(name)

        monkeypatch.setattr(app_manager, "get_app_manifest", _counting_manifest)
        app_manager.refresh_app_config_keys()
        for _ in range(3):
            validation.validate_config_data({"secretary": {"channel": "slack"}})
        assert reads == ["secretary"]

    def test_explicit_refresh_tracks_install_update_and_uninstall(
        self, tmp_path, monkeypatch
    ) -> None:
        apps_root = tmp_path / "apps"
        _write_app(apps_root, installed=False)
        monkeypatch.setattr(app_manager, "apps_dir", lambda: apps_root)
        assert app_manager.refresh_app_config_keys() == frozenset()

        installed = apps_root / "secretary" / "installed.json"
        installed.write_text(json.dumps({"name": "secretary", "version": "1.0.0"}))
        assert validation.extension_config_keys("apps") == frozenset()
        assert app_manager.refresh_app_config_keys() == frozenset({"secretary"})

        manifest = apps_root / "secretary" / "app.json"
        data = json.loads(manifest.read_text())
        data["config_keys"] = ["secretary_v2"]
        manifest.write_text(json.dumps(data))
        assert validation.extension_config_keys("apps") == frozenset({"secretary"})
        assert app_manager.refresh_app_config_keys() == frozenset({"secretary_v2"})

        installed.unlink()
        assert app_manager.refresh_app_config_keys() == frozenset()

    def test_update_and_uninstall_refresh_registry(self, tmp_path, monkeypatch) -> None:
        apps_root = tmp_path / "apps"
        source_root = tmp_path / "source"
        _write_app(apps_root, installed=True)
        _write_app(source_root, installed=False)
        source = source_root / "secretary"
        source_manifest = source / "app.json"
        source_data = json.loads(source_manifest.read_text())
        source_data.update(version="2.0.0", config_keys=["secretary_v2"])
        source_manifest.write_text(json.dumps(source_data))
        monkeypatch.setattr(app_manager, "apps_dir", lambda: apps_root)
        monkeypatch.setattr(app_manager, "_drop_trust_grant", lambda _name: None)

        assert app_manager.refresh_app_config_keys() == frozenset({"secretary"})
        updated = app_manager.update_app(source)
        assert updated.ok
        assert validation.extension_config_keys("apps") == frozenset({"secretary_v2"})

        uninstalled = app_manager.uninstall_app("secretary", keep_data=False)
        assert uninstalled.ok
        assert validation.extension_config_keys("apps") == frozenset()

    def test_malformed_installed_record_does_not_freeze_valid_apps(
        self, tmp_path, monkeypatch
    ) -> None:
        apps_root = tmp_path / "apps"
        _write_app(apps_root, installed=True)
        broken = apps_root / "broken"
        broken.mkdir()
        (broken / "app.json").write_text(
            json.dumps({"name": "broken", "config_keys": ["broken-key"]})
        )
        (broken / "installed.json").write_text(
            json.dumps({"name": "broken", "schemaVersion": "not-an-int"})
        )
        monkeypatch.setattr(app_manager, "apps_dir", lambda: apps_root)

        assert app_manager.refresh_app_config_keys() == frozenset({"secretary"})

    def test_concurrent_refresh_cannot_publish_older_snapshot(self, monkeypatch) -> None:
        first_scanning = threading.Event()
        newer_waiting = threading.Event()
        release_first = threading.Event()
        calls_lock = threading.Lock()
        calls = 0
        failures: list[BaseException] = []

        class _ObservedLock:
            def __init__(self) -> None:
                self._lock = threading.Lock()

            def __enter__(self):
                if threading.current_thread().name == "newer-refresh":
                    newer_waiting.set()
                self._lock.acquire()
                return self

            def __exit__(self, *_args):
                self._lock.release()
                return False

        def _scan(_root):
            nonlocal calls
            with calls_lock:
                call = calls
                calls += 1
            if call == 0:
                first_scanning.set()
                assert release_first.wait(5), "first refresh was never released"
                return frozenset({"older"})
            return frozenset({"newer"})

        def _refresh():
            try:
                app_manager.refresh_app_config_keys()
            except BaseException as exc:
                failures.append(exc)

        monkeypatch.setattr(app_manager, "_scan_installed_app_config_keys", _scan)
        monkeypatch.setattr(app_manager, "_config_keys_refresh_lock", _ObservedLock())
        monkeypatch.setattr(app_manager, "apps_dir", lambda: None)

        older = threading.Thread(target=_refresh, name="older-refresh")
        newer = threading.Thread(target=_refresh, name="newer-refresh")
        older.start()
        try:
            assert first_scanning.wait(5), "first refresh did not enter its scan"
            newer.start()
            assert newer_waiting.wait(5), "newer refresh did not reach the serialization lock"
        finally:
            release_first.set()
        older.join(5)
        newer.join(5)

        assert not older.is_alive() and not newer.is_alive()
        assert failures == []
        assert validation.extension_config_keys("apps") == frozenset({"newer"})
        assert validation._installed_extension_config_keys() == {"newer"}

    @pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_startup_prime_suppresses_first_load_warning_for_app_and_plugin(
        self, tmp_path, monkeypatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        apps_root = tmp_path / "apps"
        _write_app(apps_root, installed=True)
        monkeypatch.setattr(app_manager, "apps_dir", lambda: apps_root)
        from kiro_crew.platform import discovery
        from kiro_crew.platform.admission import PluginManifest

        monkeypatch.setattr(
            discovery,
            "refresh_installed_plugin_config_keys",
            lambda: discovery.refresh_plugin_config_keys(
                [PluginManifest(name="companion", config_keys=["companion-owned"])]
            ),
        )
        validation.prime_extension_config_keys()

        cfgp = tmp_path / "config.json"
        cfgp.write_text(
            json.dumps(
                {
                    "secretary": {"channel": "slack"},
                    "companion-owned": {"enabled": True},
                }
            )
        )
        monkeypatch.setattr(L, "config_path", lambda: cfgp)
        monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")
        validation._CONFIG_CACHE.clear()

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            cfg = KiroCrewConfig.load()
        assert cfg._extra_sections["secretary"] == {"channel": "slack"}
        assert cfg._extra_sections["companion-owned"] == {"enabled": True}
        assert _unrecognized_warnings(caplog) == []

    @pytest.mark.asyncio
    @pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
    async def test_event_loop_validation_uses_snapshot_without_registry_io(
        self, monkeypatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        validation.replace_extension_config_keys("apps", ["secretary"])

        def _io_forbidden(*_args, **_kwargs):
            raise AssertionError("registry I/O ran on the event loop")

        monkeypatch.setattr(app_manager, "refresh_app_config_keys", _io_forbidden)
        monkeypatch.setattr(app_manager, "_scan_installed_app_config_keys", _io_forbidden)
        await asyncio.sleep(0)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            validation.validate_config_data({"secretary": {"channel": "slack"}})
        assert _unrecognized_warnings(caplog) == []

    def test_registry_failure_does_not_raise(self, monkeypatch) -> None:
        class _BrokenLock:
            def __enter__(self):
                raise RuntimeError("registry unavailable")

            def __exit__(self, *_args):
                return False

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(validation, "_extension_config_keys_lock", _BrokenLock())
            assert validation._installed_extension_config_keys() == set()
            data = {"agent": {"provider": "acp"}}
            assert validation.validate_config_data(data) is data

    @pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_validation_never_calls_discovery_or_filesystem_paths(
        self, monkeypatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from kiro_crew.platform import admission as admission_mod
        from kiro_crew.platform import discovery as discovery_mod

        def _must_not_run(*_args, **_kwargs):
            raise AssertionError("validation attempted discovery I/O")

        validation.replace_extension_config_keys("apps", frozenset({"secretary"}))
        validation.replace_extension_config_keys("plugins", frozenset({"aim_update"}))
        monkeypatch.setattr(app_manager, "_scan_installed_app_config_keys", _must_not_run)
        monkeypatch.setattr(app_manager.os, "scandir", _must_not_run)
        monkeypatch.setattr(discovery_mod, "plugin_entry_points", _must_not_run)
        monkeypatch.setattr(discovery_mod.importlib.metadata, "entry_points", _must_not_run)
        monkeypatch.setattr(admission_mod, "_read_plugin_manifest", _must_not_run)

        validation._CONFIG_CACHE.clear()
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            validation.validate_config_data(
                {"secretary": {"channel": "slack"}, "aim_update": {"enabled": True}}
            )
        assert _unrecognized_warnings(caplog) == []

    @pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_dev_fleet_section_is_not_reported(self, caplog: pytest.LogCaptureFixture) -> None:
        data = {"agent": {"provider": "acp"}, "dev_fleet": {"repo_path": "/opt/kc"}}
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            validation.validate_config_data(data)
        assert _unrecognized_warnings(caplog) == []

    @pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_genuinely_unknown_key_still_warns_beside_an_app_owned_one(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Excluding the app-owned set must not widen into silencing real typos.
        data = {"dev_fleet": {"repo_path": "/opt/kc"}, "totally_unknown_key": 1}
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            validation.validate_config_data(data)
        warnings = _unrecognized_warnings(caplog)
        assert warnings and "totally_unknown_key" in warnings[0]
        assert "dev_fleet" not in warnings[0]

    @pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
    @pytest.mark.parametrize("malformed", ["/opt/kc", ["/opt/kc"], 7, None])
    def test_a_non_object_dev_fleet_value_still_warns(
        self, malformed: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The exclusion covers the shape the app reads, not the key name.

        ``_load_dev_fleet_cfg`` keeps only a dict and silently falls back to
        discovery on anything else, so a scalar written by hand must keep the one
        warning that says it is being ignored.
        """
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            validation.validate_config_data({"agent": {"provider": "acp"}, "dev_fleet": malformed})
        warnings = _unrecognized_warnings(caplog)
        assert warnings and "dev_fleet" in warnings[0]

    @pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_full_load_of_a_dev_fleet_config_logs_no_unrecognized_warning(
        self, tmp_path, monkeypatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The operator-visible path: load() of a real file, not the validator alone."""
        cfgp = tmp_path / "config.json"
        cfgp.write_text(
            json.dumps({"agent": {"provider": "acp"}, "dev_fleet": {"repo_path": "/opt/kc"}})
        )
        monkeypatch.setattr(L, "config_path", lambda: cfgp)
        monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")

        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            KiroCrewConfig.load()
        assert _unrecognized_warnings(caplog) == []


class TestAppOwnedSectionsRoundTrip:
    def test_dev_fleet_section_survives_load_and_to_dict(self, tmp_path, monkeypatch) -> None:
        """Unlike a RESERVED key, an app-owned section must not be dropped on save."""
        cfgp = tmp_path / "config.json"
        cfgp.write_text(
            json.dumps({"agent": {"provider": "acp"}, "dev_fleet": {"repo_path": "/opt/kc"}})
        )
        monkeypatch.setattr(L, "config_path", lambda: cfgp)
        monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")

        cfg = KiroCrewConfig.load()
        assert cfg._extra_sections.get("dev_fleet") == {"repo_path": "/opt/kc"}
        assert cfg.to_dict().get("dev_fleet") == {"repo_path": "/opt/kc"}

    def test_set_is_disjoint_from_known_and_reserved(self) -> None:
        """A key in two sets would be classified two ways by the same loader."""
        assert not (validation._APP_OWNED_TOP_KEYS & set(_KNOWN_CONFIG_SECTIONS))
        assert not (validation._APP_OWNED_TOP_KEYS & CONFIG_RESERVED_TOP_KEYS)


class TestEveryAppOwnedKeyHasAReader:
    """The set is a claim that an app reads the key; pin the claim to the reader."""

    def test_members_are_exactly_the_documented_readers(self) -> None:
        # Extend this tuple together with validation._APP_OWNED_TOP_KEYS and add a
        # reader test below; a member with no reader test is a silenced typo.
        assert validation._APP_OWNED_TOP_KEYS == frozenset({"dev_fleet"})

    def test_dev_fleet_reads_repo_path_from_the_config_file(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.apps.builtins.dev_fleet import repository as repository_mod

        (tmp_path / "config.json").write_text(json.dumps({"dev_fleet": {"repo_path": "/opt/kc"}}))
        monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
        monkeypatch.delenv("KIROCREW_DEVFLEET_REPO", raising=False)

        assert repository_mod._configured_main_repo() == "/opt/kc"
