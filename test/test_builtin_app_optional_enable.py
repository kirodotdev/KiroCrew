"""Property-based tests for builtin app optional enable feature.

Tests correctness properties from the design document:
- Property 1: First-time registration respects defaultEnabled
- Property 2: Re-registration preserves user state
- Property 3: All builtin apps have lifecycle=locked
- Property 8: Invalid definitions are skipped without affecting others
"""
from __future__ import annotations

import re

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew.apps.manager import (
    InstalledApp,
    _read_installed,
    _validate_builtin_app,
    _write_installed,
    app_dir,
    list_apps,
    register_builtin_apps,
    uninstall_app,
)
from kiro_crew.apps.manifest import app_name_error
from kiro_crew.constants import WINDOWS_DEVICE_STEMS

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _ship_builtin(monkeypatch, root, name, **manifest_extra):
    """Give a test builtin real shipped provenance.

    Execution admission derives builtin status from the immutable shipped
    package tree (``execution._BUILTINS_DIR``), never from installed metadata.
    Tests that fabricate builtins must therefore ship them: create the package
    directory with an authoritative ``app.json`` and point the provenance root
    at it, mirroring how genuine builtins qualify.
    """
    import json as _json
    from pathlib import Path

    import kiro_crew.apps.execution as execution

    shipped = Path(root) / "shipped-builtins"
    app_root = shipped / name
    app_root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": name,
        "version": "1.0.0",
        "displayName": name,
        "description": "Test shipped builtin",
        "author": "kirocrew",
        **manifest_extra,
    }
    (app_root / "app.json").write_text(
        _json.dumps(manifest), encoding="utf-8"
    )
    monkeypatch.setattr(execution, "_BUILTINS_DIR", shipped)
    return app_root


@pytest.fixture()
def app_home(tmp_path, monkeypatch):
    """Set KIROCREW_HOME to a temp directory for isolated testing."""
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    return home


@pytest.mark.asyncio
async def test_shipped_builtin_cron_registers_with_default_off(
    app_home, tmp_path, monkeypatch
):
    import json

    import kiro_crew.apps.execution as execution
    import kiro_crew.apps.manager as mgr
    from kiro_crew.apps.bridges import (
        register_app,
        register_app_crons_with_service,
    )
    from kiro_crew.cron import CronService

    shipped_root = _ship_builtin(
        monkeypatch,
        tmp_path,
        "builtin-cron-app",
        defaultEnabled=True,
        crons=[{
            "name": "refresh",
            "every": 3600,
            "message": "refresh builtin state",
        }],
    )
    manifest = json.loads((shipped_root / "app.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(mgr, "_BUILTIN_APPS", [manifest])
    monkeypatch.setattr(mgr, "_orphaned_builtins_cache", None)
    monkeypatch.setattr(execution, "third_party_execution_allowed", lambda: False)

    register_builtin_apps()
    registration = register_app("builtin-cron-app")
    assert registration.crons == ["builtin-cron-app/refresh"]
    assert registration.errors == []

    service = CronService(base_dir=app_home / "crons")
    registered = await register_app_crons_with_service(
        "builtin-cron-app",
        service,
    )

    assert registered == ["builtin-cron-app/refresh"]
    assert [job.name for job in service.list_jobs()] == [
        "builtin-cron-app/refresh"
    ]


@pytest.fixture(autouse=True)
def _no_disk_discovery(monkeypatch):
    """Suppress real on-disk builtin discovery so tests only see what they
    explicitly inject via ``monkeypatch.setattr(mgr, "_BUILTIN_APPS", ...)``.

    Without this, ``register_builtin_apps()`` also picks up real builtins
    discovered from ``src/kiro_crew/apps/builtins/*/app.json`` (e.g.
    ``code-reviewer``), which would inflate counts in tests that rely on
    ``_BUILTIN_APPS`` being the sole source.
    """
    import kiro_crew.apps.manager as mgr
    monkeypatch.setattr(mgr, "discover_builtin_apps", lambda: [])


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Generate app names the admission contract actually accepts. The kebab-case
# grammar alone still reaches Windows reserved device stems (``aux``, ``con``,
# ``nul``, ``com1``…), which ``app_name_error`` refuses on every platform, so
# ``register_builtin_apps`` would skip the definition and ``_read_installed``
# would return None — a domain error in the strategy, not a bug in the store.
# The exclusion is delegated to ``app_name_error`` rather than restated, so the
# sampling domain cannot drift away from what production admits.
_APP_NAME_GRAMMAR = r"[a-z][a-z0-9]{1,8}(-[a-z][a-z0-9]{1,5}){0,2}"
app_name_st = st.from_regex(_APP_NAME_GRAMMAR, fullmatch=True).filter(
    lambda name: app_name_error(name) is None
)

# Generate valid builtin app definitions
valid_builtin_def_st = st.fixed_dictionaries({
    "name": app_name_st,
    "version": st.just("1.0.0"),
    "displayName": st.text(min_size=1, max_size=30).filter(lambda s: s.strip()),
    "description": st.text(min_size=1, max_size=50).filter(lambda s: s.strip()),
    "author": st.text(min_size=1, max_size=20).filter(lambda s: s.strip()),
}, optional={
    "defaultEnabled": st.booleans(),
    "tags": st.lists(st.text(min_size=1, max_size=10), max_size=3),
})


def test_strategy_cannot_sample_an_inadmissible_app_name() -> None:
    """Deterministic guard for the sampling domain.

    ``aux`` is kebab-case and inside the grammar's length range, so the regex
    alone still reaches it — and registration refuses it on every platform, so
    a strategy that samples it asserts on an impossible state (Hypothesis lands
    on it intermittently, surfacing as a Windows-shard flake). Production must
    keep refusing the name first; the strategy filter then keeps it out of the
    domain by delegating to the same contract.
    """
    assert re.fullmatch(
        _APP_NAME_GRAMMAR, "aux"
    ), "grammar no longer reaches the name under test"
    assert app_name_error("aux") is not None, "production must refuse it first"
    assert app_name_error("aux-tools") is None, "ordinary names stay in the domain"


# ---------------------------------------------------------------------------
# Property 1: First-time registration respects defaultEnabled
# ---------------------------------------------------------------------------


class TestProperty1FirstTimeRegistration:
    """Validates: Requirements 1.1, 1.2, 1.3"""

    @given(app_def=valid_builtin_def_st)
    @settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_first_registration_uses_default_enabled(self, app_def, tmp_path, monkeypatch, request):
        """For any builtin app registered for the first time, enabled SHALL equal
        defaultEnabled if specified, or True if not specified."""
        import shutil
        import tempfile

        home = tempfile.mkdtemp()
        request.addfinalizer(lambda h=home: shutil.rmtree(h, ignore_errors=True))
        monkeypatch.setenv("KIROCREW_HOME", home)

        import kiro_crew.apps.manager as mgr
        monkeypatch.setattr(mgr, "_BUILTIN_APPS", [app_def])
        monkeypatch.setattr(mgr, "_orphaned_builtins_cache", None)

        register_builtin_apps()

        meta = _read_installed(app_def["name"])
        assert meta is not None

        expected_enabled = app_def.get("defaultEnabled", True)
        assert meta.enabled == expected_enabled

    def test_explicit_default_enabled_false(self, app_home, monkeypatch):
        """A builtin app with defaultEnabled: false registers as disabled."""
        import kiro_crew.apps.manager as mgr
        monkeypatch.setattr(mgr, "_BUILTIN_APPS", [{
            "name": "test-opt-in",
            "version": "1.0.0",
            "displayName": "Test Opt-In",
            "description": "An opt-in feature",
            "author": "kirocrew",
            "defaultEnabled": False,
        }])

        register_builtin_apps()
        meta = _read_installed("test-opt-in")
        assert meta is not None
        assert meta.enabled is False

    def test_missing_default_enabled_defaults_to_true(self, app_home, monkeypatch):
        """A builtin app without defaultEnabled registers as enabled (backward compat)."""
        import kiro_crew.apps.manager as mgr
        monkeypatch.setattr(mgr, "_BUILTIN_APPS", [{
            "name": "test-legacy",
            "version": "1.0.0",
            "displayName": "Test Legacy",
            "description": "A legacy-style builtin",
            "author": "kirocrew",
        }])

        register_builtin_apps()
        meta = _read_installed("test-legacy")
        assert meta is not None
        assert meta.enabled is True


# ---------------------------------------------------------------------------
# Property 2: Re-registration preserves user state
# ---------------------------------------------------------------------------


class TestProperty2ReRegistrationPreservesState:
    """Validates: Requirements 1.4, 2.2, 6.2, 6.3"""

    @given(
        app_def=valid_builtin_def_st,
        user_enabled=st.booleans(),
    )
    @settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_reregistration_preserves_enabled(self, app_def, user_enabled, tmp_path, monkeypatch, request):
        """For any builtin app with existing installed.json, re-registration
        SHALL NOT change the enabled field."""
        import shutil
        import tempfile

        home = tempfile.mkdtemp()
        request.addfinalizer(lambda h=home: shutil.rmtree(h, ignore_errors=True))
        monkeypatch.setenv("KIROCREW_HOME", home)

        import kiro_crew.apps.manager as mgr
        monkeypatch.setattr(mgr, "_BUILTIN_APPS", [app_def])
        monkeypatch.setattr(mgr, "_orphaned_builtins_cache", None)

        name = app_def["name"]

        # Pre-create installed.json with user's chosen enabled state
        dest = app_dir(name)
        dest.mkdir(parents=True, exist_ok=True)
        existing_meta = InstalledApp(
            name=name,
            version="0.9.0",
            displayName="Old Name",
            enabled=user_enabled,
            installedAt="2024-01-01T00:00:00Z",
            source="builtin",
            origin="builtin",
            resources="gateway",
            lifecycle="locked",
        )
        _write_installed(name, existing_meta)

        # Re-register
        register_builtin_apps()

        meta = _read_installed(name)
        assert meta is not None
        # enabled MUST be preserved
        assert meta.enabled == user_enabled
        # version and displayName should be updated
        assert meta.version == app_def["version"]
        assert meta.displayName == app_def["displayName"]


# ---------------------------------------------------------------------------
# Property 3: All builtin apps have lifecycle=locked
# ---------------------------------------------------------------------------


class TestProperty3LifecycleLocked:
    """Validates: Requirements 2.3, 5.4"""

    @given(app_def=valid_builtin_def_st)
    @settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_builtin_apps_always_locked(self, app_def, tmp_path, monkeypatch, request):
        """For any builtin app after registration, lifecycle SHALL equal 'locked'."""
        import shutil
        import tempfile

        home = tempfile.mkdtemp()
        request.addfinalizer(lambda h=home: shutil.rmtree(h, ignore_errors=True))
        monkeypatch.setenv("KIROCREW_HOME", home)

        import kiro_crew.apps.manager as mgr
        monkeypatch.setattr(mgr, "_BUILTIN_APPS", [app_def])
        monkeypatch.setattr(mgr, "_orphaned_builtins_cache", None)

        register_builtin_apps()

        meta = _read_installed(app_def["name"])
        assert meta is not None
        assert meta.lifecycle == "locked"

    def test_uninstall_locked_rejected(self, app_home, monkeypatch):
        """Calling uninstall_app() on a builtin app SHALL return an error."""
        import kiro_crew.apps.manager as mgr
        monkeypatch.setattr(mgr, "_BUILTIN_APPS", [{
            "name": "locked-app",
            "version": "1.0.0",
            "displayName": "Locked App",
            "description": "Cannot uninstall",
            "author": "kirocrew",
        }])

        register_builtin_apps()
        result = uninstall_app("locked-app")
        assert not result.ok
        assert "locked" in result.error.lower()


# ---------------------------------------------------------------------------
# Property 8: Invalid definitions are skipped without affecting others
# ---------------------------------------------------------------------------


class TestProperty8InvalidDefinitionsSkipped:
    """Validates: Requirements 8.4"""

    def test_invalid_skipped_valid_registered(self, app_home, monkeypatch):
        """A mix of valid and invalid definitions: valid ones register, invalid skip."""
        import kiro_crew.apps.manager as mgr

        apps_list = [
            # Invalid: missing description
            {
                "name": "bad-app",
                "version": "1.0.0",
                "displayName": "Bad App",
                "description": "",
                "author": "kirocrew",
            },
            # Valid
            {
                "name": "good-app",
                "version": "1.0.0",
                "displayName": "Good App",
                "description": "A valid app",
                "author": "kirocrew",
            },
            # Invalid: non-boolean defaultEnabled
            {
                "name": "bad-default",
                "version": "1.0.0",
                "displayName": "Bad Default",
                "description": "Has bad defaultEnabled",
                "author": "kirocrew",
                "defaultEnabled": "yes",
            },
        ]
        monkeypatch.setattr(mgr, "_BUILTIN_APPS", apps_list)

        count = register_builtin_apps()

        # Only the valid app should be registered
        assert count == 1
        assert _read_installed("good-app") is not None
        assert _read_installed("bad-app") is None
        assert _read_installed("bad-default") is None

    def test_unsafe_name_skipped(self, app_home, monkeypatch):
        """An app with path-traversal in name is skipped."""
        import kiro_crew.apps.manager as mgr
        monkeypatch.setattr(mgr, "_BUILTIN_APPS", [{
            "name": "../evil",
            "version": "1.0.0",
            "displayName": "Evil",
            "description": "Path traversal attempt",
            "author": "attacker",
        }])

        count = register_builtin_apps()
        assert count == 0


# ---------------------------------------------------------------------------
# Validation function unit tests
# ---------------------------------------------------------------------------


class TestValidateBuiltinApp:
    """Unit tests for _validate_builtin_app()."""

    def test_valid_minimal(self):
        app = {
            "name": "my-app",
            "version": "1.0.0",
            "displayName": "My App",
            "description": "A test app",
            "author": "tester",
        }
        assert _validate_builtin_app(app) == []

    def test_valid_with_default_enabled(self):
        app = {
            "name": "my-app",
            "version": "1.0.0",
            "displayName": "My App",
            "description": "A test app",
            "author": "tester",
            "defaultEnabled": False,
        }
        assert _validate_builtin_app(app) == []

    def test_missing_name(self):
        app = {
            "version": "1.0.0",
            "displayName": "My App",
            "description": "A test app",
            "author": "tester",
        }
        errors = _validate_builtin_app(app)
        assert any("name" in e for e in errors)

    def test_missing_multiple_fields(self):
        app = {"name": "x"}
        errors = _validate_builtin_app(app)
        assert len(errors) >= 4  # version, displayName, description, author

    def test_non_boolean_default_enabled(self):
        app = {
            "name": "my-app",
            "version": "1.0.0",
            "displayName": "My App",
            "description": "A test app",
            "author": "tester",
            "defaultEnabled": "true",
        }
        errors = _validate_builtin_app(app)
        assert any("defaultEnabled" in e for e in errors)

    def test_unsafe_name(self):
        app = {
            "name": "../../etc",
            "version": "1.0.0",
            "displayName": "Evil",
            "description": "Bad",
            "author": "x",
        }
        errors = _validate_builtin_app(app)
        assert any("unsafe" in e for e in errors)

    @pytest.mark.parametrize("name", sorted(WINDOWS_DEVICE_STEMS))
    def test_windows_device_stem_rejected_on_all_platforms(self, name):
        """Builtins are registered from a dict, never through ``AppManifest``,
        so the reserved-stem refusal has to hold at this door too — otherwise a
        name the manifest path refuses would be admitted here and the store's
        write would silently produce no meta on Windows."""
        app = {
            "name": name,
            "version": "1.0.0",
            "displayName": "Device",
            "description": "Reserved stem",
            "author": "x",
        }
        errors = _validate_builtin_app(app)
        assert any("not portable" in e for e in errors), (name, errors)

    @pytest.mark.parametrize("name", ["AUX", "Con", "aux.txt", "nul."])
    def test_device_stem_variants_refused_by_the_grammar(self, name):
        """Windows reserves the stem case-insensitively and with any extension
        (``AUX``, ``aux.txt``, ``nul.``). These variants never reach the stem
        comparison because the kebab-case grammar refuses uppercase and dots
        first — so the assertion pins the rule that actually fires, not the
        stem check the names merely resemble."""
        app = {
            "name": name,
            "version": "1.0.0",
            "displayName": "Device",
            "description": "Reserved stem variant",
            "author": "x",
        }
        errors = _validate_builtin_app(app)
        assert any("kebab-case" in e for e in errors), (name, errors)

    def test_reserved_name_produces_no_meta_and_no_crash(
        self, app_home, monkeypatch, caplog
    ):
        """End-to-end at the registration boundary: a reserved name is skipped
        loudly (validation error + warning log) before any write, so no partial
        state lands on disk and other apps are unaffected."""
        import logging

        import kiro_crew.apps.manager as mgr

        monkeypatch.setattr(mgr, "_BUILTIN_APPS", [
            {
                "name": "aux",
                "version": "1.0.0",
                "displayName": "Aux",
                "description": "Reserved",
                "author": "x",
            },
            {
                "name": "good-app",
                "version": "1.0.0",
                "displayName": "Good App",
                "description": "A valid sibling",
                "author": "x",
            },
        ])
        monkeypatch.setattr(mgr, "_orphaned_builtins_cache", None)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.manager"):
            count = register_builtin_apps()

        assert "aux" in caplog.text, "the skip must be operator-visible"
        assert count == 1, "the valid sibling still registers"
        assert _read_installed("good-app") is not None
        assert _read_installed("aux") is None
        assert not app_dir("aux").exists()

    def test_existing_builtins_all_valid(self):
        """The shipped builtin apps must all pass validation."""
        import kiro_crew.apps.manager as mgr
        for app_data in mgr._BUILTIN_APPS:
            errors = _validate_builtin_app(app_data)
            assert errors == [], f"{app_data['name']} failed validation: {errors}"

    def test_all_builtins_default_disabled(self):
        """Builtin apps default to disabled (opt-in via App Store), except for a
        small, explicit allowlist of core surfaces we intentionally ship enabled.

        Builtin apps are hidden on a fresh install so the sidebar stays minimal;
        users enable the ones they want from the Browse tab. Entries not on the
        intentional default-on allowlist must set ``defaultEnabled: False``
        explicitly rather than relying on the field's backward-compat default of
        True. Adding an app to the allowlist is a deliberate product decision —
        it is declared once in ``manager._DEFAULT_ON_BUILTINS`` and read from
        there by both this test and the file-based-manifest test below.
        """
        import kiro_crew.apps.manager as mgr

        for app_data in mgr._BUILTIN_APPS:
            name = app_data["name"]
            if name in mgr._DEFAULT_ON_BUILTINS:
                assert app_data.get("defaultEnabled") is True, (
                    f"{name} is on the intentional default-on allowlist but does "
                    f"not set defaultEnabled=True (got {app_data.get('defaultEnabled')!r})"
                )
            else:
                assert app_data.get("defaultEnabled") is False, (
                    f"{name} must ship with defaultEnabled=False "
                    f"(got {app_data.get('defaultEnabled')!r})"
                )

    def test_all_file_based_builtins_default_disabled(self):
        """File-based builtin apps (apps/builtins/*/app.json) also default to disabled.

        These manifests are merged with _BUILTIN_APPS by register_builtin_apps(),
        so they follow the same opt-in policy AND the same default-on exemption:
        the allowlist is read from ``manager._DEFAULT_ON_BUILTINS`` rather than
        restated here, so moving an app between the two registration paths cannot
        silently change its fresh-install visibility. A manifest that omits
        defaultEnabled (or sets it true without being on the allowlist) would
        surface the app on a fresh install, so require an explicit False on every
        other discovered manifest.
        """
        import kiro_crew.apps.manager as mgr
        from kiro_crew.apps.discovery import discover_builtin_apps

        for app_data in discover_builtin_apps():
            name = app_data["name"]
            if name in mgr._DEFAULT_ON_BUILTINS:
                assert app_data.get("defaultEnabled") is True, (
                    f"{name} (file-based builtin) is on the intentional default-on "
                    f"allowlist but does not set defaultEnabled=True "
                    f"(got {app_data.get('defaultEnabled')!r})"
                )
            else:
                assert app_data.get("defaultEnabled") is False, (
                    f"{name} (file-based builtin) must ship with "
                    f"defaultEnabled=False (got {app_data.get('defaultEnabled')!r})"
                )

    def test_default_on_allowlist_names_a_real_builtin(self):
        """Every name on the default-on allowlist must resolve to a shipped builtin.

        A typo or a rename would otherwise leave a dead entry that silently
        exempts nothing, and the app it was meant to cover would fail the
        default-disabled assertion above with a confusing message.
        """
        import kiro_crew.apps.manager as mgr
        from kiro_crew.apps.discovery import discover_builtin_apps

        shipped = {a["name"] for a in mgr._BUILTIN_APPS}
        shipped |= {a["name"] for a in discover_builtin_apps()}
        unknown = set(mgr._DEFAULT_ON_BUILTINS) - shipped
        assert not unknown, f"default-on allowlist names unknown builtin(s): {sorted(unknown)}"

    def test_default_enabled_builtin_respects_governance_deny(self, app_home, monkeypatch):
        """A default-enabled builtin still honors the ``apps`` allowlist.

        register_builtin_apps() persists a default-enabled app on first
        registration without routing through enable_app(), so it must re-apply
        the same ``_app_activation_denied`` gate — otherwise a host deny-by-default
        policy would be bypassed for default-on apps. When governance denies the
        app, it must register DISABLED.
        """
        import kiro_crew.apps.manager as mgr

        monkeypatch.setattr(mgr, "_app_activation_denied", lambda name: "denied by policy")
        monkeypatch.setattr(mgr, "_BUILTIN_APPS", [{
            "name": "gov-denied-default-on",
            "version": "1.0.0",
            "displayName": "Governed",
            "description": "Default-on app that governance denies",
            "author": "kirocrew",
            "defaultEnabled": True,
        }])
        monkeypatch.setattr(mgr, "_orphaned_builtins_cache", None)

        register_builtin_apps()

        meta = _read_installed("gov-denied-default-on")
        assert meta is not None
        assert meta.enabled is False, "governance-denied default-on builtin must register disabled"

    def test_default_enabled_builtin_enabled_when_governance_permits(self, app_home, monkeypatch):
        """When governance permits (the common case), a default-enabled builtin
        registers enabled — the gate is a no-op absent a deny policy."""
        import kiro_crew.apps.manager as mgr

        monkeypatch.setattr(mgr, "_app_activation_denied", lambda name: None)
        monkeypatch.setattr(mgr, "_BUILTIN_APPS", [{
            "name": "gov-ok-default-on",
            "version": "1.0.0",
            "displayName": "Permitted",
            "description": "Default-on app that governance permits",
            "author": "kirocrew",
            "defaultEnabled": True,
        }])
        monkeypatch.setattr(mgr, "_orphaned_builtins_cache", None)

        register_builtin_apps()

        meta = _read_installed("gov-ok-default-on")
        assert meta is not None
        assert meta.enabled is True


# ---------------------------------------------------------------------------
# Property 6: Enable/disable round-trip persists state
# ---------------------------------------------------------------------------


class TestProperty6EnableDisableRoundTrip:
    """Validates: Requirements 4.1, 5.1, 6.1"""

    def test_enable_then_read(self, app_home, monkeypatch, tmp_path):
        """Enabling a disabled builtin app persists enabled=True."""
        import kiro_crew.apps.manager as mgr
        from kiro_crew.apps.manager import enable_app

        _ship_builtin(monkeypatch, tmp_path, "roundtrip-app")
        monkeypatch.setattr(mgr, "_BUILTIN_APPS", [{
            "name": "roundtrip-app",
            "version": "1.0.0",
            "displayName": "Roundtrip",
            "description": "Test round-trip",
            "author": "kirocrew",
            "defaultEnabled": False,
        }])

        register_builtin_apps()

        # Initially disabled
        meta = _read_installed("roundtrip-app")
        assert meta is not None
        assert meta.enabled is False

        # Enable
        result = enable_app("roundtrip-app")
        assert result.ok

        # Read back — must be True
        meta = _read_installed("roundtrip-app")
        assert meta is not None
        assert meta.enabled is True

    def test_disable_then_read(self, app_home, monkeypatch):
        """Disabling an enabled builtin app persists enabled=False."""
        import kiro_crew.apps.manager as mgr
        from kiro_crew.apps.manager import disable_app

        monkeypatch.setattr(mgr, "_BUILTIN_APPS", [{
            "name": "roundtrip-app2",
            "version": "1.0.0",
            "displayName": "Roundtrip 2",
            "description": "Test round-trip disable",
            "author": "kirocrew",
            "defaultEnabled": True,
        }])

        register_builtin_apps()

        # Initially enabled
        meta = _read_installed("roundtrip-app2")
        assert meta is not None
        assert meta.enabled is True

        # Disable
        result = disable_app("roundtrip-app2")
        assert result.ok

        # Read back — must be False
        meta = _read_installed("roundtrip-app2")
        assert meta is not None
        assert meta.enabled is False

    @given(initial_enabled=st.booleans())
    @settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_toggle_roundtrip(self, initial_enabled, tmp_path, monkeypatch, request):
        """For any initial state, toggling preserves the new state."""
        import shutil
        import tempfile

        home = tempfile.mkdtemp()
        request.addfinalizer(lambda h=home: shutil.rmtree(h, ignore_errors=True))
        monkeypatch.setenv("KIROCREW_HOME", home)
        _ship_builtin(monkeypatch, tmp_path, "toggle-app")

        import kiro_crew.apps.manager as mgr
        from kiro_crew.apps.manager import disable_app, enable_app

        monkeypatch.setattr(mgr, "_BUILTIN_APPS", [{
            "name": "toggle-app",
            "version": "1.0.0",
            "displayName": "Toggle",
            "description": "Toggle test",
            "author": "kirocrew",
            "defaultEnabled": initial_enabled,
        }])
        monkeypatch.setattr(mgr, "_orphaned_builtins_cache", None)

        register_builtin_apps()

        # Toggle to opposite
        if initial_enabled:
            disable_app("toggle-app")
            meta = _read_installed("toggle-app")
            assert meta is not None
            assert meta.enabled is False
        else:
            enable_app("toggle-app")
            meta = _read_installed("toggle-app")
            assert meta is not None
            assert meta.enabled is True


# ---------------------------------------------------------------------------
# Property 7: API returns complete manifest for all builtins
# ---------------------------------------------------------------------------


class TestProperty7APIReturnsCompleteManifest:
    """Validates: Requirements 7.1, 7.3"""

    def test_list_apps_includes_all_fields(self, app_home, monkeypatch):
        """list_apps() returns origin, enabled, lifecycle, and full manifest."""
        import kiro_crew.apps.manager as mgr

        monkeypatch.setattr(mgr, "_BUILTIN_APPS", [
            {
                "name": "full-manifest-app",
                "version": "2.0.0",
                "displayName": "Full Manifest",
                "description": "Has all fields",
                "author": "kirocrew",
                "tags": ["test", "full"],
                "defaultEnabled": False,
                "ui": {
                    "pages": [
                        {"route": "/full", "label": "Full", "icon": "Star"}
                    ],
                },
            },
        ])

        register_builtin_apps()

        apps = list_apps()
        assert len(apps) == 1

        app = apps[0]
        # Classification fields
        assert app["origin"] == "builtin"
        assert app["enabled"] is False
        assert app["lifecycle"] == "locked"

        # Manifest data
        manifest = app["manifest"]
        assert manifest["description"] == "Has all fields"
        assert manifest["tags"] == ["test", "full"]
        assert manifest["displayName"] == "Full Manifest"
        assert len(manifest["ui"]["pages"]) == 1
        assert manifest["ui"]["pages"][0]["route"] == "/full"

    def test_disabled_builtin_has_complete_manifest(self, app_home, monkeypatch):
        """A disabled builtin app still returns full manifest in list_apps()."""
        import kiro_crew.apps.manager as mgr

        monkeypatch.setattr(mgr, "_BUILTIN_APPS", [{
            "name": "disabled-full",
            "version": "1.0.0",
            "displayName": "Disabled Full",
            "description": "Disabled but complete",
            "author": "kirocrew",
            "tags": ["hidden"],
            "defaultEnabled": False,
            "permissions": {"api": ["/api/test"], "events": ["test_event"]},
            "ui": {"pages": [{"route": "/hidden", "label": "Hidden", "icon": "EyeOff"}]},
        }])

        register_builtin_apps()

        apps = list_apps()
        assert len(apps) == 1
        app = apps[0]

        # Must have all manifest fields even when disabled
        assert app["enabled"] is False
        manifest = app["manifest"]
        assert manifest["description"] == "Disabled but complete"
        assert manifest["tags"] == ["hidden"]
        assert manifest["permissions"]["api"] == ["/api/test"]
        assert manifest["ui"]["pages"][0]["label"] == "Hidden"


# ---------------------------------------------------------------------------
# Default-on backfill: reaching installs that registered before the exemption
# ---------------------------------------------------------------------------


def _register_builtin_disabled(monkeypatch, tmp_path, name, *, backfilled: bool):
    """Register *name* as a builtin and leave its record DISABLED.

    Reproduces the state this backfill exists for: an install that registered the
    app while it was still default-off. Written through the real registration
    path first so ``source``/``origin`` carry genuine builtin provenance, then
    flipped off — a hand-built record would not exercise
    ``_builtin_owns_install``.
    """
    import kiro_crew.apps.manager as mgr

    _ship_builtin(monkeypatch, tmp_path, name)
    monkeypatch.setattr(mgr, "_BUILTIN_APPS", [{
        "name": name,
        "version": "1.0.0",
        "displayName": name,
        "description": "Registered before the default-on exemption existed",
        "author": "kirocrew",
        "defaultEnabled": False,
    }])
    monkeypatch.setattr(mgr, "_orphaned_builtins_cache", None)
    monkeypatch.setattr(mgr, "_DEFAULT_ON_BUILTINS", frozenset({name}))
    # Registered while the promotion did NOT yet exist, which is the whole premise:
    # release N ships the app default-off, release N+1 adds it to the promotion set.
    # Setting the set before registering would make the record born flagged (see
    # test_a_fresh_registration_is_born_flagged) and there would be nothing owed.
    monkeypatch.setattr(mgr, "_DEFAULT_ON_BACKFILL", frozenset())
    register_builtin_apps()
    meta = _read_installed(name)
    assert meta is not None and meta.enabled is False
    assert meta.defaultOnBackfilled is False
    if backfilled:
        monkeypatch.setattr(mgr, "_DEFAULT_ON_BACKFILL", frozenset({name}))
    return meta


class TestDefaultOnBackfill:
    """``backfill_default_on_builtins()`` — the one-shot that reaches existing installs."""

    def test_flips_a_disabled_promoted_builtin(self, app_home, monkeypatch, tmp_path):
        """The case the backfill exists for: promotion owed, record still disabled."""
        from kiro_crew.apps.manager import backfill_default_on_builtins

        _register_builtin_disabled(monkeypatch, tmp_path, "late-default-on", backfilled=True)

        assert backfill_default_on_builtins() == ["late-default-on"]
        meta = _read_installed("late-default-on")
        assert meta is not None
        assert meta.enabled is True

    def test_does_not_read_the_fresh_install_allowlist(self, app_home, monkeypatch, tmp_path):
        """A default-on builtin NOT owed a promotion is left alone.

        ``_DEFAULT_ON_BUILTINS`` answers what a fresh install enables;
        ``_DEFAULT_ON_BACKFILL`` answers which promotion has not reached older
        installs. Reading the first for the second question re-enables apps that
        users deliberately turned off — the concrete case is ``projects``, which
        has been default-on far longer than the allowlist has existed, so every
        disabled record for it is an opt-out.
        """
        from kiro_crew.apps.manager import backfill_default_on_builtins

        _register_builtin_disabled(monkeypatch, tmp_path, "long-default-on", backfilled=False)

        assert backfill_default_on_builtins() == []
        meta = _read_installed("long-default-on")
        assert meta is not None
        assert meta.enabled is False

    def test_projects_is_not_backfilled(self):
        """Ratchet on the real sets, not a fixture.

        ``projects`` (Task Runner) shipped default-on long before the exemption
        allowlist existed, so it has been enabled and visible in the sidebar on
        every existing install; a disabled record is a user who found it and
        turned it off. Adding it here would silently reverse that.
        """
        import kiro_crew.apps.manager as mgr

        assert "projects" not in mgr._DEFAULT_ON_BACKFILL

    def test_backfill_targets_are_all_default_on(self):
        """A backfilled app must also be one a FRESH install enables.

        Otherwise the backfill would enable something the shipped policy says
        should be off, which is a promotion nobody declared.
        """
        import kiro_crew.apps.manager as mgr

        stray = set(mgr._DEFAULT_ON_BACKFILL) - set(mgr._DEFAULT_ON_BUILTINS)
        assert not stray, f"backfilled but not default-on: {sorted(stray)}"

    def test_does_not_touch_a_user_owned_entry(self, app_home, monkeypatch, tmp_path):
        """A user install under the same name keeps its own state.

        Same boundary ``register_builtin_apps()`` keeps via
        ``_builtin_owns_install``: the backfill must not enable something the
        user installed and disabled themselves.
        """
        import kiro_crew.apps.manager as mgr
        from kiro_crew.apps.manager import backfill_default_on_builtins

        monkeypatch.setattr(mgr, "_DEFAULT_ON_BACKFILL", frozenset({"user-owned"}))
        _write_installed("user-owned", InstalledApp(
            name="user-owned",
            version="1.0.0",
            displayName="User owned",
            enabled=False,
            source="/home/someone/apps/user-owned",
            origin="local",
        ))

        assert backfill_default_on_builtins() == []
        meta = _read_installed("user-owned")
        assert meta is not None
        assert meta.enabled is False

    def test_reports_nothing_when_already_enabled(self, app_home, monkeypatch, tmp_path):
        """An already-enabled app is not reported as flipped.

        The return value is what the caller logs, so a no-op start must not
        claim it enabled something.
        """
        import kiro_crew.apps.manager as mgr
        from kiro_crew.apps.manager import backfill_default_on_builtins

        _ship_builtin(monkeypatch, tmp_path, "already-on")
        monkeypatch.setattr(mgr, "_BUILTIN_APPS", [{
            "name": "already-on",
            "version": "1.0.0",
            "displayName": "Already on",
            "description": "Default-on and already enabled",
            "author": "kirocrew",
            "defaultEnabled": True,
        }])
        monkeypatch.setattr(mgr, "_orphaned_builtins_cache", None)
        monkeypatch.setattr(mgr, "_DEFAULT_ON_BUILTINS", frozenset({"already-on"}))
        monkeypatch.setattr(mgr, "_DEFAULT_ON_BACKFILL", frozenset({"already-on"}))
        register_builtin_apps()
        assert _read_installed("already-on").enabled is True  # precondition

        assert backfill_default_on_builtins() == []
        assert _read_installed("already-on").enabled is True

    def test_honors_governance_deny(self, app_home, monkeypatch, tmp_path):
        """A deny-by-default host policy is not bypassed by this path.

        ``register_builtin_apps()`` re-applies ``_app_activation_denied`` to a
        default-on builtin; arriving through the backfill instead must not be a
        way around it.
        """
        import kiro_crew.apps.manager as mgr
        from kiro_crew.apps.manager import backfill_default_on_builtins

        _register_builtin_disabled(monkeypatch, tmp_path, "gov-denied", backfilled=True)
        monkeypatch.setattr(mgr, "_app_activation_denied", lambda name: "denied by policy")

        assert backfill_default_on_builtins() == []
        meta = _read_installed("gov-denied")
        assert meta is not None
        assert meta.enabled is False

    def test_ignores_an_app_this_install_never_registered(self, app_home, monkeypatch):
        """An allowlist name with no record on disk is skipped, not created.

        An older wheel may not ship the app at all; the backfill reads existing
        state and must never conjure an install.
        """
        import kiro_crew.apps.manager as mgr
        from kiro_crew.apps.manager import backfill_default_on_builtins

        monkeypatch.setattr(mgr, "_DEFAULT_ON_BACKFILL", frozenset({"never-shipped"}))

        assert backfill_default_on_builtins() == []
        assert _read_installed("never-shipped") is None

    def test_runs_once_per_app_then_respects_a_disable(self, app_home, monkeypatch, tmp_path):
        """The load-bearing property: a second run must not override a disable.

        Disabling the app is the only way to get a replaced host surface back, so
        a backfill that re-ran would make the promotion impossible to opt out of.
        """
        from kiro_crew.apps.manager import backfill_default_on_builtins

        _register_builtin_disabled(monkeypatch, tmp_path, "once-only", backfilled=True)

        assert backfill_default_on_builtins() == ["once-only"]
        # The user turns it back off.
        meta = _read_installed("once-only")
        meta.enabled = False
        _write_installed("once-only", meta)

        assert backfill_default_on_builtins() == []
        assert _read_installed("once-only").enabled is False

    def test_audits_the_activation(self, app_home, monkeypatch, tmp_path):
        """Activating an app with no user request behind it is recorded in SEL.

        The dashboard and CLI enable paths are reachable only by someone asking;
        this one runs at startup, so without an event an operator cannot tell when
        the app became active or why.
        """
        import kiro_crew.sel as sel_mod
        from kiro_crew.apps.manager import backfill_default_on_builtins

        _register_builtin_disabled(monkeypatch, tmp_path, "audited-app", backfilled=True)
        events: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                events.append(kw)

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())

        assert backfill_default_on_builtins() == ["audited-app"]

        assert len(events) == 1, "activation was not audited"
        assert events[0]["operation"] == "app_default_on_backfill"
        assert events[0]["outcome"] == "allowed"
        assert "audited-app" in events[0]["resources"]

    def test_a_failing_audit_sink_does_not_lose_the_promotion(
        self, app_home, monkeypatch, tmp_path
    ):
        """Losing the audit line must not refuse the activation.

        Same trade the trust-grant withdrawal above makes: the record is emitted
        after the fact and never allowed to fail the operation.
        """
        import kiro_crew.sel as sel_mod
        from kiro_crew.apps.manager import backfill_default_on_builtins

        _register_builtin_disabled(monkeypatch, tmp_path, "audit-down", backfilled=True)

        def _boom():
            raise RuntimeError("sel sink unavailable")

        monkeypatch.setattr(sel_mod, "sel", _boom)

        assert backfill_default_on_builtins() == ["audit-down"]
        assert _read_installed("audit-down").enabled is True

    def test_a_failed_write_delivers_nothing_and_is_retried(
        self, app_home, monkeypatch, tmp_path
    ):
        """A failed record write leaves NOTHING half-done, and the next start retries.

        This is what one document buys. With a separate marker file there is no
        correct ordering: marker-last loses the record of an enable that happened
        (re-applied forever, reversing the user's disable), and marker-first can
        outlive a failed flip (skipped forever, never delivered). Here the flag and
        `enabled` land or fail together, so a failure is simply not-yet-done.

        Pins the persisted outcome, not the order of the in-memory append: the
        write raising propagates out of the call, so no caller ever sees a return
        value to be misled by.
        """
        import kiro_crew.apps.manager as mgr
        from kiro_crew.apps.manager import backfill_default_on_builtins

        _register_builtin_disabled(monkeypatch, tmp_path, "write-fails", backfilled=True)

        boom = {"on": True}
        real_write = mgr._write_installed

        def _maybe_fail(name, meta):
            if boom["on"] and name == "write-fails":
                raise OSError("no space left on device")
            return real_write(name, meta)

        monkeypatch.setattr(mgr, "_write_installed", _maybe_fail)

        with pytest.raises(OSError):
            backfill_default_on_builtins()

        # Nothing persisted: still disabled, still unflagged.
        meta = _read_installed("write-fails")
        assert meta is not None
        assert meta.enabled is False
        assert meta.defaultOnBackfilled is False

        boom["on"] = False
        assert backfill_default_on_builtins() == ["write-fails"]
        assert _read_installed("write-fails").enabled is True

    def test_records_the_promotion_when_it_flips_nothing(self, app_home, monkeypatch, tmp_path):
        """An already-enabled app is FLAGGED without being reported as flipped.

        The flag is what stops a later start from reading the user's subsequent
        disable as a promotion still owed; the return value stays empty because
        nothing was activated.
        """
        import kiro_crew.apps.manager as mgr
        from kiro_crew.apps.manager import backfill_default_on_builtins

        _ship_builtin(monkeypatch, tmp_path, "on-and-unflagged")
        monkeypatch.setattr(mgr, "_BUILTIN_APPS", [{
            "name": "on-and-unflagged",
            "version": "1.0.0",
            "displayName": "On and unflagged",
            "description": "Enabled by the user before the promotion shipped",
            "author": "kirocrew",
            "defaultEnabled": True,
        }])
        monkeypatch.setattr(mgr, "_orphaned_builtins_cache", None)
        monkeypatch.setattr(mgr, "_DEFAULT_ON_BUILTINS", frozenset({"on-and-unflagged"}))
        monkeypatch.setattr(mgr, "_DEFAULT_ON_BACKFILL", frozenset())
        register_builtin_apps()
        meta = _read_installed("on-and-unflagged")
        assert meta.enabled is True and meta.defaultOnBackfilled is False  # precondition

        monkeypatch.setattr(mgr, "_DEFAULT_ON_BACKFILL", frozenset({"on-and-unflagged"}))
        assert backfill_default_on_builtins() == []
        assert _read_installed("on-and-unflagged").defaultOnBackfilled is True

    def test_a_fresh_registration_is_born_flagged(self, app_home, monkeypatch, tmp_path):
        """A record created under the promoted default owes nothing.

        Without this, "install, disable the app in that same session, restart"
        re-enables it: the backfill would find a disabled record it had never
        flagged and read the user's own choice as a promotion still owed. There is
        no ordering fix for that, because on a fresh install the record may not
        exist yet when first-run setup runs.
        """
        import kiro_crew.apps.manager as mgr
        from kiro_crew.apps.manager import backfill_default_on_builtins, disable_app

        _ship_builtin(monkeypatch, tmp_path, "born-flagged")
        monkeypatch.setattr(mgr, "_BUILTIN_APPS", [{
            "name": "born-flagged",
            "version": "1.0.0",
            "displayName": "Born flagged",
            "description": "Registered on a fresh install under the promoted default",
            "author": "kirocrew",
            "defaultEnabled": True,
        }])
        monkeypatch.setattr(mgr, "_orphaned_builtins_cache", None)
        monkeypatch.setattr(mgr, "_DEFAULT_ON_BUILTINS", frozenset({"born-flagged"}))
        monkeypatch.setattr(mgr, "_DEFAULT_ON_BACKFILL", frozenset({"born-flagged"}))

        register_builtin_apps()
        assert _read_installed("born-flagged").defaultOnBackfilled is True

        disable_app("born-flagged")
        assert backfill_default_on_builtins() == []
        assert _read_installed("born-flagged").enabled is False

    def test_a_governance_denied_registration_is_still_owed_the_promotion(
        self, app_home, monkeypatch, tmp_path
    ):
        """A fresh install that governance DENIED did not receive the promotion.

        Registration gates the app off, so flagging the record would strand it:
        relaxing the policy later could never deliver the launcher, because the
        record would claim it already had. Matches the rule the backfill applies
        to the same situation.
        """
        import kiro_crew.apps.manager as mgr
        from kiro_crew.apps.manager import backfill_default_on_builtins

        _ship_builtin(monkeypatch, tmp_path, "denied-at-birth")
        monkeypatch.setattr(mgr, "_BUILTIN_APPS", [{
            "name": "denied-at-birth",
            "version": "1.0.0",
            "displayName": "Denied at birth",
            "description": "Promoted builtin that governance denied on first registration",
            "author": "kirocrew",
            "defaultEnabled": True,
        }])
        monkeypatch.setattr(mgr, "_orphaned_builtins_cache", None)
        monkeypatch.setattr(mgr, "_DEFAULT_ON_BUILTINS", frozenset({"denied-at-birth"}))
        monkeypatch.setattr(mgr, "_DEFAULT_ON_BACKFILL", frozenset({"denied-at-birth"}))
        monkeypatch.setattr(mgr, "_app_activation_denied", lambda name: "denied by policy")

        register_builtin_apps()
        meta = _read_installed("denied-at-birth")
        assert meta is not None
        assert meta.enabled is False
        assert meta.defaultOnBackfilled is False, "denied app was recorded as already promoted"

        # Policy relaxed: the promotion is still owed, so it is delivered.
        monkeypatch.setattr(mgr, "_app_activation_denied", lambda name: None)
        assert backfill_default_on_builtins() == ["denied-at-birth"]
        assert _read_installed("denied-at-birth").enabled is True

    def test_a_later_promotion_is_not_swallowed_by_an_earlier_one(
        self, app_home, monkeypatch, tmp_path
    ):
        """A promotion added in a LATER release still reaches the install.

        Simulates the real sequence: release N delivers one promotion, release N+1
        adds a second name to the set. Any design that records "the backfill has
        run" once for the whole install -- rather than once per app -- would make
        the second run a no-op and the app added later would never arrive.
        """
        import kiro_crew.apps.manager as mgr
        from kiro_crew.apps.manager import backfill_default_on_builtins

        # Release N: one promotion, delivered.
        _register_builtin_disabled(monkeypatch, tmp_path, "shipped-earlier", backfilled=True)
        assert backfill_default_on_builtins() == ["shipped-earlier"]

        # Release N+1: a second name joins the set. Its record is still disabled
        # on this install, exactly like the first one was.
        _register_builtin_disabled(monkeypatch, tmp_path, "added-later", backfilled=True)
        monkeypatch.setattr(
            mgr, "_DEFAULT_ON_BACKFILL", frozenset({"shipped-earlier", "added-later"})
        )

        assert backfill_default_on_builtins() == ["added-later"]
