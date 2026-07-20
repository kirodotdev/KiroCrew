"""Property-based tests for builtin app optional enable feature.

Tests correctness properties from the design document:
- Property 1: First-time registration respects defaultEnabled
- Property 2: Re-registration preserves user state
- Property 3: All builtin apps have lifecycle=locked
- Property 8: Invalid definitions are skipped without affecting others
"""
from __future__ import annotations

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

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def app_home(tmp_path, monkeypatch):
    """Set KIROCREW_HOME to a temp directory for isolated testing."""
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    return home


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

# Generate valid kebab-case app names
app_name_st = st.from_regex(r"[a-z][a-z0-9]{1,8}(-[a-z][a-z0-9]{1,5}){0,2}", fullmatch=True)

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


# ---------------------------------------------------------------------------
# Property 1: First-time registration respects defaultEnabled
# ---------------------------------------------------------------------------


class TestProperty1FirstTimeRegistration:
    """Validates: Requirements 1.1, 1.2, 1.3"""

    @given(app_def=valid_builtin_def_st)
    @settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_first_registration_uses_default_enabled(self, app_def, tmp_path, monkeypatch):
        """For any builtin app registered for the first time, enabled SHALL equal
        defaultEnabled if specified, or True if not specified."""
        import tempfile

        home = tempfile.mkdtemp()
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
    def test_reregistration_preserves_enabled(self, app_def, user_enabled, tmp_path, monkeypatch):
        """For any builtin app with existing installed.json, re-registration
        SHALL NOT change the enabled field."""
        import tempfile

        home = tempfile.mkdtemp()
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
    def test_builtin_apps_always_locked(self, app_def, tmp_path, monkeypatch):
        """For any builtin app after registration, lifecycle SHALL equal 'locked'."""
        import tempfile

        home = tempfile.mkdtemp()
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
        keep it small and update this test in the same change.
        """
        import kiro_crew.apps.manager as mgr

        # Core surfaces intentionally enabled on a fresh install. A default-on
        # builtin still honors the ``apps`` governance allowlist at registration
        # (see test_default_enabled_builtin_respects_governance_deny).
        intentionally_default_on = {"projects"}  # Task Runner

        for app_data in mgr._BUILTIN_APPS:
            name = app_data["name"]
            if name in intentionally_default_on:
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
        so they follow the same opt-in policy. A manifest that omits defaultEnabled
        (or sets it true) would surface the app on a fresh install, so require an
        explicit False on every discovered manifest.
        """
        from kiro_crew.apps.discovery import discover_builtin_apps

        for app_data in discover_builtin_apps():
            assert app_data.get("defaultEnabled") is False, (
                f"{app_data['name']} (file-based builtin) must ship with "
                f"defaultEnabled=False (got {app_data.get('defaultEnabled')!r})"
            )

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

    def test_enable_then_read(self, app_home, monkeypatch):
        """Enabling a disabled builtin app persists enabled=True."""
        import kiro_crew.apps.manager as mgr
        from kiro_crew.apps.manager import enable_app

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
    def test_toggle_roundtrip(self, initial_enabled, tmp_path, monkeypatch):
        """For any initial state, toggling preserves the new state."""
        import tempfile

        home = tempfile.mkdtemp()
        monkeypatch.setenv("KIROCREW_HOME", home)

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
