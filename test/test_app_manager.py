"""Tests for kiro_crew.apps.manager — App lifecycle management."""
from __future__ import annotations

import json
import os
import shutil

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    AppResult,
    InstalledApp,
    _read_installed,
    _validate_source_path,
    disable_app,
    enable_app,
    get_app,
    get_app_manifest,
    install_app,
    list_apps,
    uninstall_app,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_app_source(tmp_path, name="test-app", **manifest_overrides):
    """Create a minimal app source directory with a valid app.json."""
    src = tmp_path / "source" / name
    src.mkdir(parents=True)
    manifest = {
        "name": name,
        "version": "1.0.0",
        "displayName": "Test App",
        "description": "A test app for unit tests",
        "author": "tester",
        **manifest_overrides,
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2))
    return src


@pytest.fixture()
def app_home(tmp_path, monkeypatch):
    """Set KIROCREW_HOME to a temp directory for isolated testing."""
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    return home


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_valid_source(self, tmp_path):
        src = _make_app_source(tmp_path)
        assert _validate_source_path(src) == []

    def test_missing_manifest(self, tmp_path):
        src = tmp_path / "empty"
        src.mkdir()
        errors = _validate_source_path(src)
        assert any("missing" in e for e in errors)

    def test_invalid_json(self, tmp_path):
        src = tmp_path / "bad"
        src.mkdir()
        (src / APP_MANIFEST_FILENAME).write_text("{not valid json")
        errors = _validate_source_path(src)
        assert any("invalid" in e.lower() for e in errors)

    def test_manifest_validation_errors(self, tmp_path):
        src = _make_app_source(tmp_path, name="")
        errors = _validate_source_path(src)
        assert any("name" in e for e in errors)


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

class TestInstall:
    def test_install_from_directory(self, tmp_path, app_home):
        src = _make_app_source(tmp_path)
        result = install_app(src)
        assert result.ok
        assert result.name == "test-app"
        # Verify files copied
        installed_dir = app_home / "apps" / "test-app"
        assert installed_dir.is_dir()
        assert (installed_dir / APP_MANIFEST_FILENAME).is_file()
        # Verify installed.json
        meta = _read_installed("test-app")
        assert meta is not None
        assert meta.name == "test-app"
        assert meta.version == "1.0.0"
        assert meta.enabled is False  # installed but not enabled
        assert meta.installedAt != ""

    def test_install_creates_data_dir(self, tmp_path, app_home):
        src = _make_app_source(tmp_path)
        install_app(src)
        data = app_home / "apps" / "test-app" / "data"
        assert data.is_dir()

    def test_install_nonexistent_source(self, app_home):
        result = install_app("/nonexistent/path")
        assert not result.ok
        assert "not a directory" in result.error

    def test_install_invalid_manifest(self, tmp_path, app_home):
        src = tmp_path / "bad-app"
        src.mkdir()
        (src / APP_MANIFEST_FILENAME).write_text('{"name": ""}')
        result = install_app(src)
        assert not result.ok
        assert "name" in result.error

    def test_install_duplicate_rejected(self, tmp_path, app_home):
        src = _make_app_source(tmp_path)
        r1 = install_app(src)
        assert r1.ok
        r2 = install_app(src)
        assert not r2.ok
        assert "already installed" in r2.error

    def test_install_with_agents_and_skills(self, tmp_path, app_home):
        src = _make_app_source(
            tmp_path,
            agents=["agents/analyst.json"],
            skills=["skills/triage"],
        )
        # Create the referenced files
        (src / "agents").mkdir()
        (src / "agents" / "analyst.json").write_text('{"name": "analyst"}')
        (src / "skills" / "triage").mkdir(parents=True)
        (src / "skills" / "triage" / "SKILL.md").write_text("# Triage skill")

        result = install_app(src)
        assert result.ok
        # Verify files were copied
        installed = app_home / "apps" / "test-app"
        assert (installed / "agents" / "analyst.json").is_file()
        assert (installed / "skills" / "triage" / "SKILL.md").is_file()


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

class TestUninstall:
    def test_uninstall_preserves_data_by_default(self, tmp_path, app_home):
        src = _make_app_source(tmp_path)
        install_app(src)
        data_file = app_home / "apps" / "test-app" / "data" / "state.json"
        data_file.write_text('{"saved": true}')

        result = uninstall_app("test-app")

        assert result.ok
        assert data_file.read_text() == '{"saved": true}'
        assert not (app_home / "apps" / "test-app" / APP_MANIFEST_FILENAME).exists()

    def test_uninstall_purges_data_only_when_explicit(self, tmp_path, app_home):
        src = _make_app_source(tmp_path)
        install_app(src)
        data_file = app_home / "apps" / "test-app" / "data" / "state.json"
        data_file.write_text('{"saved": true}')

        result = uninstall_app("test-app", keep_data=False)

        assert result.ok
        assert not (app_home / "apps" / "test-app").exists()

    def test_uninstall_not_installed(self, app_home):
        result = uninstall_app("nonexistent")
        assert not result.ok
        assert "not installed" in result.error

    def test_uninstall_keep_data(self, tmp_path, app_home):
        src = _make_app_source(tmp_path)
        install_app(src)
        # Write some data
        data_dir = app_home / "apps" / "test-app" / "data"
        (data_dir / "cache.json").write_text('{"key": "value"}')

        result = uninstall_app("test-app", keep_data=True)
        assert result.ok
        # Data preserved
        assert (app_home / "apps" / "test-app" / "data" / "cache.json").is_file()
        # App files removed
        assert not (app_home / "apps" / "test-app" / APP_MANIFEST_FILENAME).exists()

    def test_install_preserves_existing_data(self, tmp_path, app_home):
        """Reinstall after default uninstall must preserve user data."""
        src = _make_app_source(tmp_path)
        install_app(src)
        # Write user data
        data_dir = app_home / "apps" / "test-app" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "priorities.md").write_text("- item1\n- item2\n")
        (data_dir / "state").mkdir(exist_ok=True)
        (data_dir / "state" / "oncall.json").write_text('{"oncall": true}')

        # Uninstall with keep_data
        result = uninstall_app("test-app", keep_data=True)
        assert result.ok
        assert (data_dir / "priorities.md").is_file()

        # Reinstall from same source (source has empty data/)
        src2 = _make_app_source(tmp_path / "src2")
        result = install_app(src2)
        assert result.ok

        # User data must survive
        assert (data_dir / "priorities.md").read_text(encoding="utf-8") == "- item1\n- item2\n"
        assert (data_dir / "state" / "oncall.json").read_text(encoding="utf-8") == '{"oncall": true}'

    def test_install_rollback_restores_data_on_copy_failure(self, tmp_path, app_home, monkeypatch):
        """If copytree fails after data/ was preserved, rollback must restore data/."""
        src = _make_app_source(tmp_path)
        install_app(src)
        # Write user data
        data_dir = app_home / "apps" / "test-app" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "config.yaml").write_text("oncall:\n  rotation: my-rotation\n")
        (data_dir / "state").mkdir(exist_ok=True)
        (data_dir / "state" / "oncall.json").write_text('{"oncall": true}')

        # Uninstall with keep_data
        uninstall_app("test-app", keep_data=True)
        assert (data_dir / "config.yaml").is_file()

        # Patch copytree to fail AFTER rmtree succeeds (simulates partial install failure)
        def failing_copytree(*args, **kwargs):
            raise OSError("Simulated disk full error")

        src2 = _make_app_source(tmp_path / "src2")
        monkeypatch.setattr("shutil.copytree", failing_copytree)
        result = install_app(src2)

        # Install must fail
        assert not result.ok
        assert "failed to copy app files" in result.error

        # Rollback must have restored data/
        assert data_dir.is_dir(), "data/ directory must be restored after rollback"
        assert (data_dir / "config.yaml").read_text(encoding="utf-8") == "oncall:\n  rotation: my-rotation\n"
        assert (data_dir / "state" / "oncall.json").read_text(encoding="utf-8") == '{"oncall": true}'

    def test_install_rejects_unsafe_app_name(self, tmp_path, app_home, monkeypatch):
        """Path-traversal name must be rejected with SEL audit event."""
        # Use a valid kebab-case name that passes manifest validation,
        # but monkeypatch _check_path_safety to simulate a traversal detection.
        src = _make_app_source(tmp_path, name="evil-app")
        sel_calls = []
        monkeypatch.setattr(
            "kiro_crew.apps.manager.sel",
            lambda: type("FakeSel", (), {
                "log_api_access": lambda self, **kw: sel_calls.append(kw)
            })(),
        )
        monkeypatch.setattr(
            "kiro_crew.apps.manager._check_path_safety",
            lambda name: False,
        )
        result = install_app(src)
        assert not result.ok
        assert "unsafe app name" in result.error
        # Verify SEL rejection event was emitted
        assert len(sel_calls) == 1
        assert sel_calls[0]["outcome"] == "rejected"
        assert sel_calls[0]["operation"] == "path_safety_check"
        # Verify nothing was written to disk
        assert not (app_home / "apps" / "evil-app" / APP_MANIFEST_FILENAME).exists()

    def test_install_reclaims_stale_tmp_when_data_absent(self, tmp_path, app_home):
        """Stale .data-tmp from a crashed uninstall must be reclaimed on reinstall."""
        src = _make_app_source(tmp_path)
        install_app(src)
        dest = app_home / "apps" / "test-app"
        data_dir = dest / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "myfile.md").write_text("precious data\n")

        # Simulate crashed uninstall: data moved to .data-tmp, app dir removed
        stale_tmp = dest.parent / ".test-app-data-tmp"
        shutil.move(str(data_dir), str(stale_tmp))
        shutil.rmtree(str(dest))
        assert stale_tmp.is_dir()
        assert not dest.exists()

        # Reinstall — must reclaim data from stale tmp
        src2 = _make_app_source(tmp_path / "src2")
        result = install_app(src2)
        assert result.ok
        assert (data_dir / "myfile.md").read_text(encoding="utf-8") == "precious data\n"
        assert not stale_tmp.exists()

    def test_install_stale_tmp_removed_when_current_data_exists(self, tmp_path, app_home):
        """If both stale .data-tmp and current data/ exist, current wins."""
        src = _make_app_source(tmp_path)
        install_app(src)
        dest = app_home / "apps" / "test-app"
        data_dir = dest / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "current.md").write_text("current data\n")

        # Uninstall with keep_data — data/ is preserved in dest
        uninstall_app("test-app", keep_data=True)
        assert (data_dir / "current.md").is_file()

        # Now simulate a leftover stale tmp (as if a PREVIOUS crashed install
        # left it behind after uninstall restored data/)
        stale_tmp = dest.parent / ".test-app-data-tmp"
        stale_tmp.mkdir(parents=True, exist_ok=True)
        (stale_tmp / "old.md").write_text("old stale data\n")

        # Reinstall — current data/ must win; stale tmp must be cleaned
        src2 = _make_app_source(tmp_path / "src2")
        result = install_app(src2)
        assert result.ok

        # Current data must survive; stale tmp must be gone
        assert (data_dir / "current.md").read_text(encoding="utf-8") == "current data\n"
        assert not (data_dir / "old.md").exists()
        assert not stale_tmp.exists()

    def test_install_emits_success_sel_event(self, tmp_path, app_home, monkeypatch):
        """Successful install must emit SEL audit event."""
        sel_calls = []
        monkeypatch.setattr(
            "kiro_crew.apps.manager.sel",
            lambda: type("FakeSel", (), {
                "log_api_access": lambda self, **kw: sel_calls.append(kw)
            })(),
        )
        src = _make_app_source(tmp_path)
        result = install_app(src)
        assert result.ok
        # Must have emitted a success event
        success_events = [c for c in sel_calls if c.get("outcome") == "success"]
        assert len(success_events) == 1
        assert success_events[0]["operation"] == "install"
        assert "test-app" in success_events[0]["resources"]


# ---------------------------------------------------------------------------
# App admission gate
# ---------------------------------------------------------------------------

class TestAppAdmission:
    def _write_policy(self, app_home, policy):
        (app_home / "app_admission.json").write_text(json.dumps(policy))

    def test_install_allowed_when_absent_policy(self, tmp_path, app_home):
        # No app_admission.json → open default → admit (preserves current behavior).
        src = _make_app_source(tmp_path)
        result = install_app(src)
        assert result.ok

    def test_install_denied_when_banned(self, tmp_path, app_home):
        self._write_policy(app_home, {"mode": "enforce", "banned": ["test-app"]})
        src = _make_app_source(tmp_path)
        result = install_app(src)
        assert not result.ok
        assert "blocked by admission policy" in result.error
        # Nothing landed on disk.
        assert not (app_home / "apps" / "test-app" / APP_MANIFEST_FILENAME).exists()

    def test_install_denied_when_banned_open_mode(self, tmp_path, app_home):
        # Kill-switch wins even in open mode.
        self._write_policy(app_home, {"mode": "open", "banned": ["test-app"]})
        src = _make_app_source(tmp_path)
        result = install_app(src)
        assert not result.ok
        assert "blocked by admission policy" in result.error

    def test_install_denied_when_not_approved(self, tmp_path, app_home):
        self._write_policy(app_home, {"mode": "enforce", "approved": ["other-app"]})
        src = _make_app_source(tmp_path)
        result = install_app(src)
        assert not result.ok
        assert "blocked by admission policy" in result.error

    def test_install_allowed_when_approved(self, tmp_path, app_home):
        self._write_policy(app_home, {"mode": "enforce", "approved": ["test-app"]})
        src = _make_app_source(tmp_path)
        result = install_app(src)
        assert result.ok

    def test_unreadable_policy_fails_closed(self, tmp_path, app_home):
        (app_home / "app_admission.json").write_text("{not valid json")
        src = _make_app_source(tmp_path)
        result = install_app(src)
        assert not result.ok
        assert "blocked by admission policy" in result.error

    def test_enable_denied_when_banned(self, tmp_path, app_home):
        # Install with an open policy, then ban and confirm enable is gated.
        src = _make_app_source(tmp_path)
        assert install_app(src).ok
        self._write_policy(app_home, {"mode": "enforce", "banned": ["test-app"]})
        result = enable_app("test-app")
        assert not result.ok
        assert "blocked by admission policy" in result.error

    def test_register_external_denied_when_banned(self, tmp_path, app_home):
        from kiro_crew.apps.manager import register_external_app

        self._write_policy(app_home, {"mode": "enforce", "banned": ["ext-app"]})
        result = register_external_app("ext-app", "1.0.0", "Ext App")
        assert not result.ok
        assert "blocked by admission policy" in result.error
        # The HTTP-reachable register path must not write enabled metadata.
        assert _read_installed("ext-app") is None

    def test_register_external_admits_signed_manifest(self, tmp_path, app_home):
        # register_external_app now passes its self-reported manifest to
        # admission, so a correctly-signed app self-registers under
        # require_signature (previously denied because no manifest was passed).
        import hashlib
        import hmac

        from kiro_crew.apps.manager import register_external_app
        from kiro_crew.apps.manifest import AppManifest

        secret = "s3cr3t"
        manifest_data = {
            "name": "ext-signed", "version": "1.0.0",
            "displayName": "Ext Signed", "description": "signed external app",
            "author": "tester", "signer": "acme",
        }
        m = AppManifest.from_dict(manifest_data)
        manifest_data["signature"] = hmac.new(
            secret.encode(), m.signing_payload(), hashlib.sha256
        ).hexdigest()
        self._write_policy(app_home, {
            "mode": "enforce", "require_signature": True,
            "approved": ["ext-signed"], "trust_keys": {"acme": secret},
        })
        result = register_external_app(
            "ext-signed", "1.0.0", "Ext Signed", manifest_data=manifest_data,
        )
        assert result.ok
        assert _read_installed("ext-signed") is not None

    def test_register_external_denies_unsigned_manifest(self, tmp_path, app_home):
        from kiro_crew.apps.manager import register_external_app

        self._write_policy(app_home, {
            "mode": "enforce", "require_signature": True,
            "approved": ["ext-unsigned"], "trust_keys": {"acme": "s3cr3t"},
        })
        result = register_external_app(
            "ext-unsigned", "1.0.0", "Ext Unsigned",
            manifest_data={"name": "ext-unsigned", "version": "1.0.0"},
        )
        assert not result.ok
        assert "blocked by admission policy" in result.error
        assert _read_installed("ext-unsigned") is None

    def test_signature_required_admits_valid_signature(self, tmp_path, app_home):
        import hashlib
        import hmac

        from kiro_crew.apps.manifest import AppManifest

        secret = "s3cr3t"
        m = AppManifest.from_dict({
            "name": "signed-app", "version": "1.0.0",
            "displayName": "Signed", "description": "signed app",
            "author": "tester", "signer": "acme",
        })
        sig = hmac.new(secret.encode(), m.signing_payload(), hashlib.sha256).hexdigest()
        self._write_policy(app_home, {
            "mode": "enforce", "require_signature": True,
            "approved": ["signed-app"], "trust_keys": {"acme": secret},
        })
        src = _make_app_source(
            tmp_path, name="signed-app", signer="acme", signature=sig,
        )
        result = install_app(src)
        assert result.ok

    def test_signature_required_denies_missing_signature(self, tmp_path, app_home):
        self._write_policy(app_home, {
            "mode": "enforce", "require_signature": True,
            "approved": ["test-app"], "trust_keys": {"acme": "s3cr3t"},
        })
        src = _make_app_source(tmp_path)  # no signer/signature
        result = install_app(src)
        assert not result.ok
        assert "blocked by admission policy" in result.error

    def test_enable_builtin_exempt_under_require_signature(self, tmp_path, app_home):
        # Builtins ship unsigned with defaultEnabled=False; a require_signature
        # policy must NOT strand them (they are trusted first-party code). The
        # admission gate governs third-party enable, not builtins.
        from kiro_crew.apps.manager import _write_installed
        src = _make_app_source(tmp_path, name="builtin-app")
        assert install_app(src).ok
        meta = _read_installed("builtin-app")
        assert meta is not None
        meta.origin = "builtin"
        _write_installed("builtin-app", meta)
        self._write_policy(app_home, {
            "mode": "enforce", "require_signature": True,
            "approved": [], "trust_keys": {},
        })
        result = enable_app("builtin-app")
        assert result.ok
        assert _read_installed("builtin-app").enabled is True

    def test_enable_third_party_still_denied_under_require_signature(self, tmp_path, app_home):
        # A non-builtin (unsigned) app is still denied under require_signature.
        src = _make_app_source(tmp_path)  # origin defaults to non-builtin
        assert install_app(src).ok
        self._write_policy(app_home, {
            "mode": "enforce", "require_signature": True,
            "approved": ["test-app"], "trust_keys": {"acme": "s3cr3t"},
        })
        result = enable_app("test-app")
        assert not result.ok
        assert "blocked by admission policy" in result.error

    def test_non_ascii_signature_is_clean_deny(self):
        # A non-ASCII signature (attacker-controlled) must NOT raise TypeError out
        # of hmac.compare_digest — it must be a clean deny (no unhandled 500 DoS).
        from kiro_crew.apps.admission import AppAdmissionPolicy, _signature_valid
        from kiro_crew.apps.manifest import AppManifest

        policy = AppAdmissionPolicy(
            mode="enforce", require_signature=True, trust_keys={"acme": "s3cr3t"}
        )
        m = AppManifest.from_dict({
            "name": "evil-app", "version": "1.0.0", "displayName": "Evil",
            "description": "d", "author": "tester", "signer": "acme",
            "signature": "é" * 64,  # non-ASCII, would crash bytes-less compare
        })
        assert _signature_valid(m, policy) is False


# ---------------------------------------------------------------------------
# Enable / Disable
# ---------------------------------------------------------------------------

class TestEnableDisable:
    def test_enable(self, tmp_path, app_home):
        src = _make_app_source(tmp_path)
        install_app(src)
        result = enable_app("test-app")
        assert result.ok
        meta = _read_installed("test-app")
        assert meta is not None
        assert meta.enabled is True

    def test_enable_already_enabled(self, tmp_path, app_home):
        src = _make_app_source(tmp_path)
        install_app(src)
        enable_app("test-app")
        result = enable_app("test-app")
        assert result.ok
        assert "already enabled" in result.message

    def test_enable_not_installed(self, app_home):
        result = enable_app("nonexistent")
        assert not result.ok

    def test_disable(self, tmp_path, app_home):
        src = _make_app_source(tmp_path)
        install_app(src)
        enable_app("test-app")
        result = disable_app("test-app")
        assert result.ok
        meta = _read_installed("test-app")
        assert meta is not None
        assert meta.enabled is False

    def test_disable_already_disabled(self, tmp_path, app_home):
        src = _make_app_source(tmp_path)
        install_app(src)
        result = disable_app("test-app")
        assert result.ok
        assert "already disabled" in result.message

    def test_disable_not_installed(self, app_home):
        result = disable_app("nonexistent")
        assert not result.ok


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

class TestListing:
    def test_list_empty(self, app_home):
        assert list_apps() == []

    def test_list_installed_apps(self, tmp_path, app_home):
        src1 = _make_app_source(tmp_path, name="app-one")
        src2 = _make_app_source(tmp_path, name="app-two")
        install_app(src1)
        install_app(src2)
        apps = list_apps()
        assert len(apps) == 2
        names = {a["name"] for a in apps}
        assert names == {"app-one", "app-two"}

    def test_get_app(self, tmp_path, app_home):
        src = _make_app_source(tmp_path)
        install_app(src)
        info = get_app("test-app")
        assert info is not None
        assert info["name"] == "test-app"
        assert "manifest" in info
        assert info["manifest"]["name"] == "test-app"

    def test_get_app_not_installed(self, app_home):
        assert get_app("nonexistent") is None

    def test_get_manifest(self, tmp_path, app_home):
        src = _make_app_source(tmp_path)
        install_app(src)
        m = get_app_manifest("test-app")
        assert m is not None
        assert m.name == "test-app"
        assert m.version == "1.0.0"

    def test_get_manifest_not_installed(self, app_home):
        assert get_app_manifest("nonexistent") is None


# ---------------------------------------------------------------------------
# InstalledApp dataclass
# ---------------------------------------------------------------------------

class TestInstalledApp:
    def test_round_trip(self):
        meta = InstalledApp(
            name="my-app", version="1.0.0", displayName="My App",
            enabled=True, installedAt="2026-04-10T00:00:00Z", source="/tmp/src",
            origin="registry", resources="gateway", lifecycle="gateway",
        )
        d = meta.to_dict()
        meta2 = InstalledApp.from_dict(d)
        assert meta2.name == meta.name
        assert meta2.version == meta.version
        assert meta2.enabled == meta.enabled
        assert meta2.origin == meta.origin
        assert meta2.resources == meta.resources
        assert meta2.lifecycle == meta.lifecycle
        assert meta2.schemaVersion == 2

    def test_from_empty_dict(self):
        meta = InstalledApp.from_dict({})
        assert meta.name == ""
        assert meta.enabled is True  # default
        assert meta.origin == "registry"
        assert meta.resources == "gateway"
        assert meta.lifecycle == "gateway"

    def test_builtin_fields(self):
        meta = InstalledApp.from_dict({
            "name": "channels", "origin": "builtin",
            "resources": "gateway", "lifecycle": "locked",
        })
        assert meta.origin == "builtin"
        assert meta.lifecycle == "locked"

    def test_external_fields(self):
        meta = InstalledApp.from_dict({
            "name": "mochi-pet", "origin": "external",
            "resources": "app", "lifecycle": "app",
        })
        assert meta.origin == "external"
        assert meta.resources == "app"
        assert meta.lifecycle == "app"

    def test_invalid_origin_falls_back(self):
        meta = InstalledApp.from_dict({"name": "bad", "origin": "typo"})
        assert meta.origin == "registry"  # default fallback

    def test_invalid_lifecycle_falls_back(self):
        meta = InstalledApp.from_dict({"name": "bad", "lifecycle": "gatway"})
        assert meta.lifecycle == "gateway"

    def test_invalid_resources_falls_back(self):
        meta = InstalledApp.from_dict({"name": "bad", "resources": "self"})
        assert meta.resources == "gateway"

    def test_validate_fields_valid(self):
        meta = InstalledApp(origin="builtin", resources="app", lifecycle="locked")
        assert meta.validate_fields() == []

    def test_validate_fields_invalid(self):
        meta = InstalledApp(origin="bad", resources="bad", lifecycle="bad")
        errors = meta.validate_fields()
        assert len(errors) == 3

    def test_schema_version_persisted(self):
        meta = InstalledApp(name="x")
        d = meta.to_dict()
        assert d["schemaVersion"] == 2

    # ── Migration from old "managed" field ──

    def test_migrate_managed_self(self):
        """Old managed='self' → external/app/app classification."""
        meta = InstalledApp.from_dict({"name": "old", "managed": "self"})
        assert meta.origin == "external"
        assert meta.resources == "app"
        assert meta.lifecycle == "app"
        assert meta.schemaVersion == 2

    def test_migrate_managed_builtin(self):
        """Old managed='builtin' → builtin/gateway/locked classification."""
        meta = InstalledApp.from_dict({"name": "old", "managed": "builtin"})
        assert meta.origin == "builtin"
        assert meta.resources == "gateway"
        assert meta.lifecycle == "locked"
        assert meta.schemaVersion == 2

    def test_migrate_managed_kirocrew(self):
        """Old managed='kirocrew' with no source → defaults to registry."""
        meta = InstalledApp.from_dict({"name": "old", "managed": "kirocrew"})
        assert meta.origin == "registry"
        assert meta.resources == "gateway"
        assert meta.lifecycle == "gateway"
        assert meta.schemaVersion == 2

    def test_migrate_managed_kirocrew_local_source(self):
        """Old managed='kirocrew' with filesystem source → origin='local'."""
        meta = InstalledApp.from_dict({
            "name": "old", "managed": "kirocrew",
            "source": "/Users/dev/my-tool",
        })
        assert meta.origin == "local"
        assert meta.resources == "gateway"
        assert meta.lifecycle == "gateway"

    def test_migrate_managed_kirocrew_registry_source(self):
        """Old managed='kirocrew' with registry: source → origin='registry'."""
        meta = InstalledApp.from_dict({
            "name": "old", "managed": "kirocrew",
            "source": "registry:my-app",
        })
        assert meta.origin == "registry"
        assert meta.resources == "gateway"
        assert meta.lifecycle == "gateway"

    def test_migrate_skipped_when_origin_present(self):
        """If origin is already in the dict, migration is skipped even with schemaVersion < 2."""
        meta = InstalledApp.from_dict({
            "name": "old", "managed": "self",
            "origin": "local", "schemaVersion": 1,
        })
        # origin was explicitly set — migration should NOT override it
        assert meta.origin == "local"
        assert meta.resources == "gateway"  # default, not migrated to "app"

    def test_uninstall_locked_rejected(self, tmp_path, app_home):
        """lifecycle=locked apps cannot be uninstalled."""
        from kiro_crew.apps.manager import register_builtin_apps
        register_builtin_apps()
        result = uninstall_app("agent-worlds")
        assert not result.ok
        assert "locked" in result.error


# ---------------------------------------------------------------------------
# InstalledApp property tests (Hypothesis)
# ---------------------------------------------------------------------------

_valid_origins = st.sampled_from(["builtin", "registry", "local", "external"])
_valid_resources = st.sampled_from(["gateway", "app"])
_valid_lifecycles = st.sampled_from(["gateway", "app", "locked"])


class TestInstalledAppProperties:
    # Feature: app-classification-redesign, Property 1: InstalledApp 序列化往返一致性
    @given(
        name=st.from_regex(r"[a-z][a-z0-9\-]{0,20}", fullmatch=True),
        version=st.from_regex(r"[0-9]+\.[0-9]+\.[0-9]+", fullmatch=True),
        enabled=st.booleans(),
        origin=_valid_origins,
        resources=_valid_resources,
        lifecycle=_valid_lifecycles,
    )
    @settings(max_examples=200)
    def test_round_trip_property(self, name, version, enabled, origin, resources, lifecycle):
        """**Validates: Requirements 1.4**"""
        meta = InstalledApp(
            name=name, version=version, displayName=f"App {name}",
            enabled=enabled, installedAt="2026-01-01T00:00:00Z",
            source="test", origin=origin, resources=resources, lifecycle=lifecycle,
        )
        d = meta.to_dict()
        restored = InstalledApp.from_dict(d)
        assert restored.name == meta.name
        assert restored.version == meta.version
        assert restored.enabled == meta.enabled
        assert restored.origin == meta.origin
        assert restored.resources == meta.resources
        assert restored.lifecycle == meta.lifecycle
        assert restored.schemaVersion == meta.schemaVersion

    # Feature: app-classification-redesign, Property 2: 无效字段值回退到默认值
    @given(
        bad_origin=st.text(min_size=1, max_size=10).filter(
            lambda s: s not in {"builtin", "registry", "local", "external"}
        ),
        bad_resources=st.text(min_size=1, max_size=10).filter(
            lambda s: s not in {"gateway", "app"}
        ),
        bad_lifecycle=st.text(min_size=1, max_size=10).filter(
            lambda s: s not in {"gateway", "app", "locked"}
        ),
    )
    @settings(max_examples=200)
    def test_invalid_fields_fallback_property(self, bad_origin, bad_resources, bad_lifecycle):
        """**Validates: Requirements 1.6**"""
        meta = InstalledApp.from_dict({
            "name": "test", "origin": bad_origin,
            "resources": bad_resources, "lifecycle": bad_lifecycle,
        })
        assert meta.origin == "registry"
        assert meta.resources == "gateway"
        assert meta.lifecycle == "gateway"


# ---------------------------------------------------------------------------
# AppResult
# ---------------------------------------------------------------------------

class TestAppResult:
    def test_success(self):
        r = AppResult(ok=True, name="x", message="done")
        d = r.to_dict()
        assert d["ok"] is True
        assert d["name"] == "x"
        assert "error" not in d

    def test_failure(self):
        r = AppResult(ok=False, name="x", error="bad")
        d = r.to_dict()
        assert d["ok"] is False
        assert d["error"] == "bad"


# --- item #5: cleanup_migrated_builtin matches by name, no migratedTo needed ---

class TestCleanupMigratedBuiltin:
    """cleanup_migrated_builtin must handle pre-existing installs without migratedTo."""

    def test_no_migrated_to_still_cleaned_up(self, tmp_path, monkeypatch):
        """Old deploy_web install with origin=builtin but NO migratedTo -> still removed."""
        from kiro_crew.apps import manager
        from kiro_crew.apps.manager import (
            INSTALLED_META_FILENAME,
            cleanup_migrated_builtin,
        )

        monkeypatch.setattr(manager, "app_dir", lambda name: tmp_path / name)

        # Create a fake deploy_web installed.json with origin=builtin, no migratedTo
        app_path = tmp_path / "deploy_web"
        app_path.mkdir()
        installed = {
            "name": "deploy_web",
            "version": "1.0.0",
            "origin": "builtin",
            "enabled": True,
        }
        (app_path / INSTALLED_META_FILENAME).write_text(json.dumps(installed))
        (app_path / "app.json").write_text(json.dumps({"name": "deploy_web"}))
        # Also create a data/ dir that must be PRESERVED
        (app_path / "data").mkdir()
        (app_path / "data" / "user-file.txt").write_text("keep me")

        result = cleanup_migrated_builtin("deploy_web")
        assert result.ok is True
        assert "cleaned up" in result.message

        # Metadata removed
        assert not (app_path / INSTALLED_META_FILENAME).exists()
        assert not (app_path / "app.json").exists()
        # Data preserved
        assert (app_path / "data" / "user-file.txt").exists()

    def test_idempotent_already_gone(self, tmp_path, monkeypatch):
        """If app was never installed, returns ok=True (idempotent)."""
        from kiro_crew.apps import manager
        from kiro_crew.apps.manager import cleanup_migrated_builtin

        monkeypatch.setattr(manager, "app_dir", lambda name: tmp_path / name)

        result = cleanup_migrated_builtin("deploy_web")
        assert result.ok is True
        assert "nothing to clean up" in result.message

    def test_standalone_origin_not_touched(self, tmp_path, monkeypatch):
        """If origin is not 'builtin', no cleanup (standalone owns the slot)."""
        from kiro_crew.apps import manager
        from kiro_crew.apps.manager import (
            INSTALLED_META_FILENAME,
            cleanup_migrated_builtin,
        )

        monkeypatch.setattr(manager, "app_dir", lambda name: tmp_path / name)

        app_path = tmp_path / "deploy_web"
        app_path.mkdir()
        installed = {
            "name": "deploy_web",
            "version": "2.0.0",
            "origin": "registry",
            "enabled": True,
        }
        (app_path / INSTALLED_META_FILENAME).write_text(json.dumps(installed))

        result = cleanup_migrated_builtin("deploy_web")
        assert result.ok is True
        assert "already migrated" in result.message
        # File was NOT deleted
        assert (app_path / INSTALLED_META_FILENAME).exists()


# ---------------------------------------------------------------------------
# _copy_app_tree — symlink / denylist / off-loop regression tests
# (app install used to run a raw follow-symlinks copytree on the event loop;
# a large `build` symlink target froze the loop until the watchdog killed
# the gateway)
# ---------------------------------------------------------------------------

class TestCopyAppTree:
    def test_symlink_escaping_source_root_omitted(self, tmp_path, app_home):
        """A symlink resolving outside the app source is omitted — never
        followed (no multi-GB walk) and never preserved (nothing in the
        installed tree can point at e.g. ~/.ssh)."""
        src = _make_app_source(tmp_path)
        big = tmp_path / "big-target"
        big.mkdir()
        for i in range(20):
            (big / f"file{i}.bin").write_text("x" * 1024)
        (src / "assets-link").symlink_to(big)

        result = install_app(src)
        assert result.ok, result.error

        from kiro_crew.apps.manager import app_dir

        dest = app_dir("test-app")
        assert not (dest / "assets-link").exists()
        assert not (dest / "assets-link").is_symlink()
        # Target contents were not copied anywhere in the installed tree.
        copied_files = [p for p in dest.rglob("*") if p.is_file() and not p.is_symlink()]
        assert not any("file0.bin" in str(p) for p in copied_files)

    def test_symlink_inside_source_root_preserved(self, tmp_path, app_home):
        """An in-tree symlink is preserved — and an ABSOLUTE in-tree link is
        rewritten to a relative link targeting the installed copy, so the
        installed app never depends on the original source directory."""
        import os
        import shutil as _shutil

        src = _make_app_source(tmp_path)
        (src / "shared").mkdir()
        (src / "shared" / "common.js").write_text("export {}")
        (src / "alias").symlink_to(src / "shared")  # absolute in-tree link

        result = install_app(src)
        assert result.ok, result.error

        from kiro_crew.apps.manager import app_dir

        dest = app_dir("test-app")
        link = dest / "alias"
        assert link.is_symlink()
        # Rewritten relative — must not embed an absolute path to the source.
        assert not os.path.isabs(os.readlink(link))
        # Resolves inside the installed tree and stays usable even after the
        # original source directory is gone.
        _shutil.rmtree(src)
        assert (link / "common.js").is_file()
        assert link.resolve().is_relative_to(dest.resolve())

    def test_denylist_dirs_dropped_runtime_payload_kept(self, tmp_path, app_home):
        src = _make_app_source(tmp_path)
        (src / "ui" / "node_modules").mkdir(parents=True)
        (src / "ui" / "node_modules" / "junk.js").write_text("junk")
        (src / "ui" / "dist").mkdir(parents=True)
        (src / "ui" / "dist" / "index.mjs").write_text("export {}")
        (src / ".git").mkdir()
        (src / ".git" / "config").write_text("[core]")
        (src / "__pycache__").mkdir()
        (src / "__pycache__" / "x.pyc").write_bytes(b"\x00")
        # A real `build/` dir is NOT denylisted: the manifest may reference
        # runtime paths anywhere under the app root, so it must survive.
        # (A `build` *symlink* is neutralized by symlinks=True instead.)
        (src / "build").mkdir()
        (src / "build" / "artifact.txt").write_text("built")

        result = install_app(src)
        assert result.ok, result.error

        from kiro_crew.apps.manager import app_dir

        dest = app_dir("test-app")
        assert not (dest / "ui" / "node_modules").exists()
        assert not (dest / ".git").exists()
        assert not (dest / "__pycache__").exists()
        assert (dest / "build" / "artifact.txt").is_file()
        assert (dest / "ui" / "dist" / "index.mjs").is_file()

    def test_lifecycle_lock_is_per_app(self):
        from kiro_crew.apps.manager import app_lifecycle_lock

        lock_a = app_lifecycle_lock("app-a")
        assert app_lifecycle_lock("app-a") is lock_a
        assert app_lifecycle_lock("app-b") is not lock_a

    @pytest.mark.asyncio
    async def test_install_off_loop_does_not_block_event_loop(self, tmp_path, app_home):
        """Heartbeat latency stays low while a many-file install runs off-loop."""
        import asyncio
        import time

        src = _make_app_source(tmp_path, name="fat-app")
        payload = src / "payload"
        payload.mkdir()
        for i in range(2000):
            (payload / f"f{i}.txt").write_text(str(i))

        gaps: list[float] = []

        async def heartbeat():
            prev = time.monotonic()
            while True:
                await asyncio.sleep(0.01)
                now = time.monotonic()
                gaps.append(now - prev)
                prev = now

        hb = asyncio.ensure_future(heartbeat())
        try:
            result = await asyncio.to_thread(install_app, src)
        finally:
            hb.cancel()
        assert result.ok, result.error
        # The watchdog threshold is 30s; anything close to that (or even 1s)
        # would indicate the copy ran on the loop.
        assert max(gaps) < 1.0

    def test_orphaned_partial_install_self_heals(self, tmp_path, app_home):
        """dest exists with junk but no installed metadata → fresh install wins."""
        from kiro_crew.apps.manager import app_dir

        orphan = app_dir("test-app")
        orphan.mkdir(parents=True)
        (orphan / "leftover.bin").write_text("partial copy from a crash")

        src = _make_app_source(tmp_path)
        result = install_app(src)
        assert result.ok, result.error
        assert not (orphan / "leftover.bin").exists()
        assert (orphan / APP_MANIFEST_FILENAME).is_file()

    def test_update_preserves_data_and_secret(self, tmp_path, app_home):
        from kiro_crew.apps.manager import app_dir, update_app

        src = _make_app_source(tmp_path)
        assert install_app(src).ok
        dest = app_dir("test-app")
        (dest / "data").mkdir(exist_ok=True)
        (dest / "data" / "state.json").write_text('{"k": 1}')
        secret = (dest / ".app_secret")
        secret.write_text("s3cret")

        v2 = _make_app_source(tmp_path / "v2", version="2.0.0")
        result = update_app(v2)
        assert result.ok, result.error
        assert (dest / "data" / "state.json").read_text(encoding="utf-8") == '{"k": 1}'
        assert secret.read_text(encoding="utf-8") == "s3cret"

    def test_directory_junction_omitted(self, tmp_path, app_home, monkeypatch):
        """Windows directory junctions (reparse points not reported by
        islink) are omitted from the copy. Simulated by monkeypatching
        os.path.isjunction since junctions don't exist on POSIX."""
        import os

        src = _make_app_source(tmp_path)
        (src / "junction-dir").mkdir()
        (src / "junction-dir" / "secret.txt").write_text("sensitive")

        def fake_isjunction(p):
            return os.path.basename(str(p)) == "junction-dir"

        monkeypatch.setattr(os.path, "isjunction", fake_isjunction, raising=False)

        result = install_app(src)
        assert result.ok, result.error

        from kiro_crew.apps.manager import app_dir

        dest = app_dir("test-app")
        assert not (dest / "junction-dir").exists()

    def test_update_rejects_mismatched_source_name(self, tmp_path, app_home):
        """expected_name guards against updating app A from app B's source."""
        from kiro_crew.apps.manager import update_app

        src = _make_app_source(tmp_path)
        assert install_app(src).ok
        other = _make_app_source(tmp_path / "other", name="other-app")

        result = update_app(other, expected_name="test-app")
        assert not result.ok
        assert "does not match" in (result.error or "")

    def test_shutil_error_rolls_back_cleanly(self, tmp_path, app_home, monkeypatch):
        """shutil.Error (copytree aggregate, not an OSError) is caught and
        reported as a failed AppResult instead of propagating."""
        src = _make_app_source(tmp_path)

        def failing_copytree(*args, **kwargs):
            raise shutil.Error([("a", "b", "boom")])

        monkeypatch.setattr(shutil, "copytree", failing_copytree)
        result = install_app(src)
        assert not result.ok
        assert "failed to copy app files" in (result.error or "")
        assert _read_installed("test-app") is None


class TestBootSkillReconcile:
    """Tests for reconcile_app_skills — startup creates missing skill symlinks."""

    def test_reconcile_creates_missing_skill_symlinks(self, tmp_path, monkeypatch):
        """An enabled app with manifest skills but missing symlinks gets them on reconcile."""
        from kiro_crew.apps import bridges, manager
        from kiro_crew.apps.bridges import reconcile_app_skills

        # Set up fake app dir with a skill directory
        apps_root = tmp_path / "apps"
        app_root = apps_root / "test-app"
        app_root.mkdir(parents=True)
        skill_dir = app_root / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# My Skill\n")

        # Set up fake skills dir (where symlinks go)
        skills_root = tmp_path / "skills"
        skills_root.mkdir(parents=True)

        # Write installed.json (enabled, gateway-managed)
        installed = {
            "name": "test-app",
            "version": "1.0.0",
            "displayName": "Test App",
            "enabled": True,
            "origin": "builtin",
            "resources": "gateway",
            "lifecycle": "locked",
            "schemaVersion": 2,
        }
        (app_root / "installed.json").write_text(json.dumps(installed))

        # Write app.json manifest with skills
        manifest_data = {
            "name": "test-app",
            "version": "1.0.0",
            "displayName": "Test App",
            "description": "A test app",
            "author": "test",
            "skills": ["skills/my-skill"],
        }
        (app_root / "app.json").write_text(json.dumps(manifest_data))

        # Monkeypatch paths
        monkeypatch.setattr(manager, "apps_dir", lambda: apps_root)
        monkeypatch.setattr(manager, "app_dir", lambda name: apps_root / name)
        monkeypatch.setattr(bridges, "_skills_dir", lambda: skills_root)
        monkeypatch.setattr(bridges, "app_dir", lambda name: apps_root / name)

        # Verify NO symlinks exist yet
        assert not (skills_root / "test-app").exists()
        assert not (skills_root / "my-skill").exists()

        # Run reconcile
        registered = reconcile_app_skills("test-app")

        # Verify symlinks were created
        assert len(registered) == 1
        assert "test-app/my-skill" in registered
        assert (skills_root / "test-app" / "my-skill").is_symlink()
        assert (skills_root / "my-skill").is_symlink()
        # Symlink target resolves to the actual skill dir
        assert (skills_root / "test-app" / "my-skill").resolve() == skill_dir.resolve()

    def test_reconcile_removes_stale_skill_symlinks(self, tmp_path, monkeypatch):
        """Skills removed from manifest get their stale symlinks cleaned up."""
        from kiro_crew.apps import bridges, manager
        from kiro_crew.apps.bridges import reconcile_app_skills

        # Set up fake app dir with one skill (kept)
        apps_root = tmp_path / "apps"
        app_root = apps_root / "test-app"
        app_root.mkdir(parents=True)
        kept_skill = app_root / "skills" / "kept-skill"
        kept_skill.mkdir(parents=True)
        (kept_skill / "SKILL.md").write_text("# Kept\n")

        # Set up skills dir with a STALE symlink (removed from manifest)
        skills_root = tmp_path / "skills"
        app_skills_dir = skills_root / "test-app"
        app_skills_dir.mkdir(parents=True)
        stale_target = tmp_path / "old-skill"
        stale_target.mkdir()
        os.symlink(str(stale_target), str(app_skills_dir / "old-skill"))
        os.symlink(str(stale_target), str(skills_root / "old-skill"))

        # Write metadata
        installed = {
            "name": "test-app", "version": "1.0.0", "displayName": "Test",
            "enabled": True, "origin": "builtin", "resources": "gateway",
            "lifecycle": "locked", "schemaVersion": 2,
        }
        (app_root / "installed.json").write_text(json.dumps(installed))
        manifest_data = {
            "name": "test-app", "version": "1.0.0", "displayName": "Test",
            "description": "t", "author": "t",
            "skills": ["skills/kept-skill"],  # old-skill NOT listed
        }
        (app_root / "app.json").write_text(json.dumps(manifest_data))

        monkeypatch.setattr(manager, "apps_dir", lambda: apps_root)
        monkeypatch.setattr(manager, "app_dir", lambda name: apps_root / name)
        monkeypatch.setattr(bridges, "_skills_dir", lambda: skills_root)
        monkeypatch.setattr(bridges, "app_dir", lambda name: apps_root / name)

        registered = reconcile_app_skills("test-app")

        # Kept skill is registered
        assert "test-app/kept-skill" in registered
        assert (skills_root / "test-app" / "kept-skill").is_symlink()
        # Stale skill symlinks removed
        assert not (app_skills_dir / "old-skill").exists()
        assert not (skills_root / "old-skill").exists()


# ---------------------------------------------------------------------------
# Builtin app-secret generation for mcpServers-only backends
#
# Platform defect: the gateway proxy (handle_app_api_proxy) resolves an app's
# backend three ways — the third being a fallback that derives a loopback base
# URL from a manifest's mcpServers entry (self-managed apps whose backend is a
# separate loopback process, e.g. the Crew Companion desktop app on :7778).
# register_builtin_apps() used to write a .app_secret ONLY when
# backend.entryPoint was present, so a builtin declaring only mcpServers
# resolved a backend fine but was refused a secret — and every proxied request
# then 502'd with "has no secret". The fix generates the secret whenever a
# backend is resolvable (entryPoint OR a loopback mcpServers URL), while an app
# with no backend of any kind still gets none.
# ---------------------------------------------------------------------------

class TestBuiltinSecretForMcpServers:
    def _register_only(self, monkeypatch, apps):
        """Run register_builtin_apps() with exactly `apps` as the builtin set."""
        from kiro_crew.apps import manager

        monkeypatch.setattr(manager, "_BUILTIN_APPS", [])
        monkeypatch.setattr(manager, "discover_builtin_apps", lambda *a, **k: apps)
        monkeypatch.setattr(manager, "_edition_builtin_apps", lambda: [])
        manager.register_builtin_apps()

    def test_declares_backend_helper(self):
        from kiro_crew.apps.manager import _app_declares_backend

        # entryPoint → backend
        assert _app_declares_backend({"backend": {"entryPoint": "pkg.server"}})
        # loopback mcpServers URL → backend (the defect case)
        assert _app_declares_backend(
            {"mcpServers": {"x": {"url": "http://127.0.0.1:7778/mcp"}}}
        )
        assert _app_declares_backend(
            {"mcpServers": {"x": {"url": "http://localhost:7778/mcp"}}}
        )
        # no backend of any kind → no secret
        assert not _app_declares_backend({})
        assert not _app_declares_backend({"mcpServers": {}})
        # non-loopback URL is not a reachable local backend
        assert not _app_declares_backend(
            {"mcpServers": {"x": {"url": "http://10.0.0.5:7778/mcp"}}}
        )
        # self-referential gateway port is refused by the proxy → no secret
        assert not _app_declares_backend(
            {"mcpServers": {"x": {"url": "http://127.0.0.1:5476/mcp"}}}
        )

    def test_mcpservers_only_builtin_gets_secret(self, tmp_path, app_home, monkeypatch):
        """A builtin declaring only mcpServers must receive a .app_secret.

        FAILS before the fix (condition was `backend.entryPoint` only), passes
        after (condition is `_app_declares_backend`).
        """
        from kiro_crew.apps.manager import app_dir

        mcp_only = {
            "name": "mcp-only-app",
            "version": "1.0.0",
            "displayName": "MCP Only",
            "description": "declares only an mcpServers loopback backend",
            "author": "tester",
            "defaultEnabled": False,
            "mcpServers": {"mcp-only-app": {"url": "http://127.0.0.1:7778/mcp"}},
        }
        self._register_only(monkeypatch, [mcp_only])
        assert (app_dir("mcp-only-app") / ".app_secret").is_file()

    def test_no_backend_builtin_gets_no_secret(self, tmp_path, app_home, monkeypatch):
        """A builtin with no backend of any kind must NOT get a secret."""
        from kiro_crew.apps.manager import app_dir

        no_backend = {
            "name": "no-backend-app",
            "version": "1.0.0",
            "displayName": "No Backend",
            "description": "declares no backend at all",
            "author": "tester",
            "defaultEnabled": False,
        }
        self._register_only(monkeypatch, [no_backend])
        assert not (app_dir("no-backend-app") / ".app_secret").is_file()


class TestBuiltinDoesNotClobberUserInstall:
    """A builtin must never take over a user-installed app of the same name.

    Apps live at ``apps/<name>/`` keyed on name alone, so a builtin that shares a
    name with an externally distributed app would, on every gateway restart:
    replace the user's manifest, set ``lifecycle="locked"`` (removing their
    ability to uninstall), and overwrite ``origin`` -- which destroys the only
    record that the install was ever user-owned. That last part is why this is
    pinned: after one restart, no corrective release could tell the two apart.
    """

    def _register_only(self, monkeypatch, apps):
        from kiro_crew.apps import manager

        monkeypatch.setattr(manager, "_BUILTIN_APPS", [])
        monkeypatch.setattr(manager, "discover_builtin_apps", lambda *a, **k: apps)
        monkeypatch.setattr(manager, "_edition_builtin_apps", lambda: [])
        manager.register_builtin_apps()

    BUILTIN = {
        "name": "collide-app",
        "version": "9.9.9",
        "displayName": "Collide (builtin)",
        "description": "a builtin that shares a name with a user install",
        "author": "kirocrew",
        "defaultEnabled": False,
    }

    def _seed_user_install(self, name="collide-app"):
        """Write metadata + a manifest the way install_app() would."""
        import json

        from kiro_crew.apps.manager import (
            APP_MANIFEST_FILENAME,
            InstalledApp,
            _now_iso,
            _write_installed,
            app_dir,
        )

        d = app_dir(name)
        d.mkdir(parents=True, exist_ok=True)
        (d / APP_MANIFEST_FILENAME).write_text(
            json.dumps({"name": name, "version": "0.1.0", "displayName": "Mine"}) + "\n"
        )
        _write_installed(name, InstalledApp(
            name=name,
            version="0.1.0",
            displayName="Collide (user install)",
            enabled=True,
            installedAt=_now_iso(),
            source="/Users/someone/src/collide-app",
            origin="registry",
            lifecycle="gateway",
        ))
        return d

    def test_user_manifest_is_not_overwritten(self, tmp_path, app_home, monkeypatch):
        """FAILS before the fix: the manifest was atomic_write'n unconditionally."""
        import json

        from kiro_crew.apps.manager import APP_MANIFEST_FILENAME

        d = self._seed_user_install()
        self._register_only(monkeypatch, [self.BUILTIN])

        kept = json.loads((d / APP_MANIFEST_FILENAME).read_text())
        assert kept["displayName"] == "Mine", "the user's manifest was replaced"
        assert kept["version"] == "0.1.0"

    def test_origin_and_lifecycle_survive(self, tmp_path, app_home, monkeypatch):
        """The unrecoverable part: origin must still say the install was the user's.

        FAILS before the fix (origin -> "builtin", lifecycle -> "locked").
        """
        from kiro_crew.apps.manager import _read_installed

        self._seed_user_install()
        self._register_only(monkeypatch, [self.BUILTIN])

        meta = _read_installed("collide-app")
        assert meta is not None
        assert meta.origin == "registry", "the user-owned origin record was destroyed"
        assert meta.lifecycle != "locked", "the user can no longer uninstall"
        assert meta.version == "0.1.0", "the builtin's version was forced on to it"

    def test_a_genuine_builtin_is_still_updated(self, tmp_path, app_home, monkeypatch):
        """The guard must not freeze real builtins: ours still take the update."""
        from kiro_crew.apps.manager import _read_installed

        # First registration creates it with source="builtin".
        self._register_only(monkeypatch, [self.BUILTIN])
        assert _read_installed("collide-app").source == "builtin"

        bumped = dict(self.BUILTIN, version="10.0.0", displayName="Collide v10")
        self._register_only(monkeypatch, [bumped])

        meta = _read_installed("collide-app")
        assert meta.version == "10.0.0"
        assert meta.displayName == "Collide v10"

    def test_helper_classifies_both_cases(self):
        from kiro_crew.apps.manager import InstalledApp, _builtin_owns_install

        ours = InstalledApp(
            name="x", version="1", displayName="X", enabled=False,
            installedAt="t", source="builtin",
        )
        theirs = InstalledApp(
            name="x", version="1", displayName="X", enabled=False,
            installedAt="t", source="/path/to/x", origin="registry",
        )
        assert _builtin_owns_install(ours)
        assert not _builtin_owns_install(theirs)


class TestMalformedMcpUrlIsSkippedNotFatal:
    """A malformed mcpServers URL must be SKIPPED, never raise.

    ``resolve_mcp_backend_url`` runs inside ``register_builtin_apps()`` at gateway
    startup, and a manifest is user-supplied data. ``urlparse`` accessors are lazy and
    raise ValueError on malformed input -- ``parsed.port`` does it for ":notaport" --
    so an escape from here propagates out of registration and the gateway fails to
    START. One bad manifest would take down every builtin, not just its own app.
    """

    BAD_URLS = [
        "http://127.0.0.1:notaport/mcp",   # port is not an integer
        "http://127.0.0.1:99999/mcp",      # port out of range
        "http://[::1:/mcp",                # unparsable authority
    ]

    def test_malformed_urls_return_none_and_do_not_raise(self):
        from kiro_crew.apps.manager import resolve_mcp_backend_url

        for url in self.BAD_URLS:
            # The assertion is that this LINE does not raise.
            assert resolve_mcp_backend_url({"x": {"url": url}}) is None, url

    def test_a_hostless_url_defaults_to_loopback_by_design(self):
        """`http://:7778/mcp` is not an error — it resolves to loopback deliberately.

        `host = parsed.hostname or "127.0.0.1"` treats a missing host as "this
        machine", which is the only safe default here: the SSRF guard still holds,
        because the fallback is loopback rather than anything the manifest supplied.
        Pinned so the malformed-input guard above is never "tightened" into rejecting
        it.
        """
        from kiro_crew.apps.manager import resolve_mcp_backend_url

        assert resolve_mcp_backend_url(
            {"x": {"url": "http://:7778/mcp"}}
        ) == "http://127.0.0.1:7778"

    def test_a_good_server_after_a_bad_one_still_resolves(self):
        """Skipping means continuing, not abandoning the whole manifest."""
        from kiro_crew.apps.manager import resolve_mcp_backend_url

        servers = {
            "broken": {"url": "http://127.0.0.1:notaport/mcp"},
            "good": {"url": "http://127.0.0.1:7778/mcp"},
        }
        assert resolve_mcp_backend_url(servers) == "http://127.0.0.1:7778"

    def test_registration_survives_a_malformed_manifest(self, tmp_path, app_home, monkeypatch):
        """The end-to-end shape: startup registration must not blow up.

        FAILS before the fix with ValueError out of register_builtin_apps().
        """
        from kiro_crew.apps import manager

        bad = {
            "name": "bad-url-app",
            "version": "1.0.0",
            "displayName": "Bad URL",
            "description": "declares an unparsable mcpServers port",
            "author": "tester",
            "defaultEnabled": False,
            "mcpServers": {"bad-url-app": {"url": "http://127.0.0.1:notaport/mcp"}},
        }
        monkeypatch.setattr(manager, "_BUILTIN_APPS", [])
        monkeypatch.setattr(manager, "discover_builtin_apps", lambda *a, **k: [bad])
        monkeypatch.setattr(manager, "_edition_builtin_apps", lambda: [])

        manager.register_builtin_apps()   # must not raise

        # It registers, it just gets no secret — there is no reachable backend.
        assert not (manager.app_dir("bad-url-app") / ".app_secret").is_file()

    def test_a_valid_loopback_url_is_unaffected(self):
        from kiro_crew.apps.manager import resolve_mcp_backend_url

        assert resolve_mcp_backend_url(
            {"crew-companion": {"url": "http://127.0.0.1:7778/mcp"}}
        ) == "http://127.0.0.1:7778"
