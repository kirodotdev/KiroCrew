"""Registering builtin and external apps: migrated-builtin cleanup, the boot-time skill
reconcile, app secrets for builtins that declare only mcpServers, a builtin never
overwriting a user install, registration surviving a malformed MCP URL, and
``register_external_app`` refusing a builtin-owned record while keeping
server-owned registry provenance. ``test_app_manager.py`` heads the suite and lists its parts.
"""

from __future__ import annotations

import json

import pytest
from app_manager_helpers import app_home_fixture  # noqa: F401 - fixture

from kiro_crew import platform_compat
from kiro_crew.apps.manager import (
    InstalledApp,
    _read_installed,
    _write_installed,
    register_external_app,
)

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

        assert not (app_path / INSTALLED_META_FILENAME).exists()
        assert not (app_path / "app.json").exists()
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
        assert (app_path / INSTALLED_META_FILENAME).exists()


def _ship_test_builtin(monkeypatch, root, manifest_data):
    """Give a synthetic builtin immutable package provenance for bridge tests."""
    from kiro_crew.apps import execution

    shipped = root / "shipped-builtins"
    shipped_app = shipped / manifest_data["name"]
    shipped_app.mkdir(parents=True)
    (shipped_app / "app.json").write_text(json.dumps(manifest_data), encoding="utf-8")
    monkeypatch.setattr(execution, "_BUILTINS_DIR", shipped)
    return shipped_app


class TestBootSkillReconcile:
    """Tests for reconcile_app_skills — startup creates missing skill symlinks."""

    def test_reconcile_creates_missing_skill_symlinks(self, tmp_path, monkeypatch):
        """An enabled app with manifest skills but missing symlinks gets them on reconcile."""
        from kiro_crew.apps import bridges, manager
        from kiro_crew.apps.bridges import reconcile_app_skills

        apps_root = tmp_path / "apps"
        app_root = apps_root / "test-app"
        app_root.mkdir(parents=True)

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

        manifest_data = {
            "name": "test-app",
            "version": "1.0.0",
            "displayName": "Test App",
            "description": "A test app",
            "author": "test",
            "skills": ["skills/my-skill"],
        }
        (app_root / "app.json").write_text(json.dumps(manifest_data))
        shipped_app = _ship_test_builtin(monkeypatch, tmp_path, manifest_data)
        skill_dir = shipped_app / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# My Skill\n")

        # Monkeypatch installed-state and registration paths.
        monkeypatch.setattr(manager, "apps_dir", lambda: apps_root)
        monkeypatch.setattr(manager, "app_dir", lambda name: apps_root / name)
        monkeypatch.setattr(bridges, "_skills_dir", lambda: skills_root)
        monkeypatch.setattr(bridges, "app_dir", lambda name: apps_root / name)

        assert not (skills_root / "test-app").exists()
        assert not (skills_root / "my-skill").exists()

        registered = reconcile_app_skills("test-app")

        assert len(registered) == 1
        assert "test-app/my-skill" in registered
        # symlink on POSIX, directory junction on non-admin Windows.
        assert platform_compat.is_link_or_junction(skills_root / "test-app" / "my-skill")
        assert platform_compat.is_link_or_junction(skills_root / "my-skill")
        # Registration must target the immutable shipped skill, not its install.
        assert (skills_root / "test-app" / "my-skill").resolve() == skill_dir.resolve()

    def test_reconcile_removes_stale_skill_symlinks(self, tmp_path, monkeypatch):
        """Skills removed from manifest get their stale symlinks cleaned up."""
        from kiro_crew.apps import bridges, manager
        from kiro_crew.apps.bridges import reconcile_app_skills

        apps_root = tmp_path / "apps"
        app_root = apps_root / "test-app"
        app_root.mkdir(parents=True)

        # Set up skills dir with a STALE symlink (removed from manifest)
        skills_root = tmp_path / "skills"
        app_skills_dir = skills_root / "test-app"
        app_skills_dir.mkdir(parents=True)
        stale_target = tmp_path / "old-skill"
        stale_target.mkdir()
        # symlink on POSIX, junction on non-admin Windows (a bare os.symlink
        # would raise WinError 1314 in the fixture setup).
        platform_compat.symlink_or_junction(str(stale_target), str(app_skills_dir / "old-skill"))
        platform_compat.symlink_or_junction(str(stale_target), str(skills_root / "old-skill"))

        # Write installed state and ship the authoritative builtin resources.
        installed = {
            "name": "test-app",
            "version": "1.0.0",
            "displayName": "Test",
            "enabled": True,
            "origin": "builtin",
            "resources": "gateway",
            "lifecycle": "locked",
            "schemaVersion": 2,
        }
        (app_root / "installed.json").write_text(json.dumps(installed))
        manifest_data = {
            "name": "test-app",
            "version": "1.0.0",
            "displayName": "Test",
            "description": "t",
            "author": "t",
            "skills": ["skills/kept-skill"],  # old-skill NOT listed
        }
        (app_root / "app.json").write_text(json.dumps(manifest_data))
        shipped_app = _ship_test_builtin(monkeypatch, tmp_path, manifest_data)
        kept_skill = shipped_app / "skills" / "kept-skill"
        kept_skill.mkdir(parents=True)
        (kept_skill / "SKILL.md").write_text("# Kept\n")

        monkeypatch.setattr(manager, "apps_dir", lambda: apps_root)
        monkeypatch.setattr(manager, "app_dir", lambda name: apps_root / name)
        monkeypatch.setattr(bridges, "_skills_dir", lambda: skills_root)
        monkeypatch.setattr(bridges, "app_dir", lambda name: apps_root / name)

        registered = reconcile_app_skills("test-app")

        # Kept skill is registered from immutable provenance.
        assert "test-app/kept-skill" in registered
        # symlink on POSIX, directory junction on non-admin Windows.
        assert platform_compat.is_link_or_junction(skills_root / "test-app" / "kept-skill")
        assert (skills_root / "test-app" / "kept-skill").resolve() == kept_skill.resolve()
        assert not (app_skills_dir / "old-skill").exists()
        assert not (skills_root / "old-skill").exists()


# ---------------------------------------------------------------------------
# Builtin app-secret generation for mcpServers-only backends
#
# Platform defect: the gateway proxy (handle_app_api_proxy) resolves an app's
# backend three ways — the third being a fallback that derives a loopback base
# URL from a manifest's mcpServers entry (self-managed apps whose backend is a
# separate loopback process, e.g. the Crew Companion desktop app on :7778).
# register_builtin_apps() must not write a .app_secret ONLY when
# backend.entryPoint was present, so a builtin declaring only mcpServers
# resolved a backend fine but was refused a secret — and every proxied request
# then 502'd with "has no secret". The fix generates the secret whenever a
# backend is resolvable (entryPoint OR a loopback mcpServers URL), while an app
# with no backend of any kind still gets none.
# ---------------------------------------------------------------------------


class TestBuiltinSecretForMcpServers:
    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- class-scoped delenv of one variable, moved verbatim; no test in this suite calls monkeypatch.undo()
    def _clean_port_env(self, monkeypatch):
        monkeypatch.delenv("KIROCREW_PORT", raising=False)

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
        assert _app_declares_backend({"mcpServers": {"x": {"url": "http://127.0.0.1:7778/mcp"}}})
        assert _app_declares_backend({"mcpServers": {"x": {"url": "http://localhost:7778/mcp"}}})
        # no backend of any kind → no secret
        assert not _app_declares_backend({})
        assert not _app_declares_backend({"mcpServers": {}})
        # non-loopback URL is not a reachable local backend
        assert not _app_declares_backend({"mcpServers": {"x": {"url": "http://10.0.0.5:7778/mcp"}}})
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
        _write_installed(
            name,
            InstalledApp(
                name=name,
                version="0.1.0",
                displayName="Collide (user install)",
                enabled=True,
                installedAt=_now_iso(),
                source="/Users/someone/src/collide-app",
                origin="registry",
                lifecycle="gateway",
            ),
        )
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
            name="x",
            version="1",
            displayName="X",
            enabled=False,
            installedAt="t",
            source="builtin",
        )
        theirs = InstalledApp(
            name="x",
            version="1",
            displayName="X",
            enabled=False,
            installedAt="t",
            source="/path/to/x",
            origin="registry",
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
        "http://127.0.0.1:notaport/mcp",  # port is not an integer
        "http://127.0.0.1:99999/mcp",  # port out of range
        "http://[::1:/mcp",  # unparsable authority
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

        assert (
            resolve_mcp_backend_url({"x": {"url": "http://:7778/mcp"}}) == "http://127.0.0.1:7778"
        )

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

        manager.register_builtin_apps()  # must not raise

        # It registers, it just gets no secret — there is no reachable backend.
        assert not (manager.app_dir("bad-url-app") / ".app_secret").is_file()

    def test_a_valid_loopback_url_is_unaffected(self):
        from kiro_crew.apps.manager import resolve_mcp_backend_url

        assert (
            resolve_mcp_backend_url({"crew-companion": {"url": "http://127.0.0.1:7778/mcp"}})
            == "http://127.0.0.1:7778"
        )


class TestRegisterExternalDoesNotTakeOverBuiltin:
    """Self-registration must not overwrite a builtin-owned installed record.

    Otherwise a POST /api/apps/register could downgrade a shipped builtin's
    provenance to external and hand its execution/auto-approve exemption to a
    third-party app — while leaving the boot-warmed first-party sets stale.
    """

    def test_register_external_refuses_builtin_owned_record(self, app_home):
        # A builtin-owned record exists (as register_builtin_apps would write).
        _write_installed(
            "meetings",
            InstalledApp(
                name="meetings",
                version="1.0.0",
                displayName="Meetings",
                source="builtin",
                origin="builtin",
                lifecycle="locked",
            ),
        )

        result = register_external_app(
            "meetings",
            version="9.9.9",
            display_name="Evil Meetings",
            source="/tmp/evil",
            origin="external",
            resources="app",
            lifecycle="app",
        )

        assert result.ok is False
        assert "builtin" in result.error.lower()
        # Record is untouched — provenance stays builtin, so the warmed
        # first-party set remains valid.
        after = _read_installed("meetings")
        assert after is not None
        assert after.origin == "builtin"
        assert after.source == "builtin"
        assert after.lifecycle == "locked"
        assert after.version == "1.0.0"


class TestRegisterExternalPreservesServerProvenance:
    """An app metadata refresh cannot rewrite its server-owned install identity."""

    _REPOSITORY = "https://clone.example.test/owner/self-app.git"
    _REGISTRY = "registry-A"
    _COMMIT = "a" * 40
    _SIGNER = "release-key"

    @classmethod
    def _seed_registry_app(cls) -> None:
        from kiro_crew.apps.manager import set_app_provenance

        result = register_external_app(
            "self-app",
            "1.0.0",
            "Self App",
            source="registry:self-app",
            origin="registry",
            resources="app",
            lifecycle="app",
            source_repository=cls._REPOSITORY,
        )
        assert result.ok, result.error
        assert set_app_provenance(
            "self-app",
            source="registry:self-app",
            url=cls._REPOSITORY,
            registry=cls._REGISTRY,
            commit=cls._COMMIT,
            signer=cls._SIGNER,
        )

    @classmethod
    def _assert_provenance(cls) -> None:
        meta = _read_installed("self-app")
        assert meta is not None
        assert meta.source == "registry:self-app"
        assert meta.sourceUrl == cls._REPOSITORY
        assert meta.sourceRegistry == cls._REGISTRY
        assert meta.sourceCommit == cls._COMMIT
        assert meta.sourceSigner == cls._SIGNER
        assert meta.origin == "registry"

    def test_app_controlled_registry_markers_are_not_durable_provenance(self, app_home):
        """Only sourceUrl, never app-authored classification text, is authority."""
        spoofed = register_external_app(
            "self-app",
            "1.0.0",
            "Spoofed App",
            source="registry:self-app",
            origin="registry",
        )
        assert spoofed.ok, spoofed.error

        refreshed = register_external_app(
            "self-app",
            "2.0.0",
            "Local App",
            source="C:/local/current",
            origin="external",
        )

        assert refreshed.ok, refreshed.error
        meta = _read_installed("self-app")
        assert meta is not None
        assert meta.source == "C:/local/current"
        assert meta.sourceUrl == ""
        assert meta.origin == "external"

    def test_nonempty_bound_repository_can_transition_a_local_registration(self, app_home):
        from kiro_crew.config.loader import _invalidate_config_cache

        local = register_external_app(
            "self-app",
            "1.0.0",
            "Local App",
            source="C:/local/source",
            origin="external",
        )
        assert local.ok, local.error
        (app_home / "config.json").write_text(
            json.dumps(
                {
                    "agent": {
                        "apps_allow_third_party": False,
                        "apps_trusted": ["self-app"],
                        "apps_trusted_repositories": {"self-app": self._REPOSITORY},
                    }
                }
            ),
            encoding="utf-8",
        )
        _invalidate_config_cache()

        transitioned = register_external_app(
            "self-app",
            "2.0.0",
            "Registry App",
            source="registry:self-app",
            origin="registry",
            source_repository=self._REPOSITORY,
        )

        assert transitioned.ok, transitioned.error
        meta = _read_installed("self-app")
        assert meta is not None
        assert meta.source == "registry:self-app"
        assert meta.sourceUrl == self._REPOSITORY
        assert meta.origin == "registry"

    def test_allow_all_refresh_preserves_pin_and_pinned_resolver(self, app_home, monkeypatch):
        from kiro_crew.apps import registry

        self._seed_registry_app()
        refreshed = register_external_app(
            "self-app",
            "2.0.0",
            "Self App v2",
            source="C:/caller-controlled/source",
            origin="external",
        )

        assert refreshed.ok, refreshed.error
        self._assert_provenance()
        meta = _read_installed("self-app")
        assert meta is not None
        assert meta.version == "2.0.0"
        assert meta.displayName == "Self App v2"

        attacker = {
            "name": "self-app",
            "gitUrl": "https://attacker.example.test/owner/self-app.git",
            "_registry": "registry-B",
        }
        pinned = {
            "name": "self-app",
            "gitUrl": self._REPOSITORY,
            "_registry": self._REGISTRY,
        }
        monkeypatch.setattr(registry, "_registry_app_candidates", lambda name: [attacker, pinned])

        def _bare_name_lookup(name):
            raise AssertionError(f"bare-name lookup attempted for {name}")

        monkeypatch.setattr(registry, "get_registry_app", _bare_name_lookup)
        assert registry._resolve_install_entry("self-app") == (pinned, "")

    def test_repository_bound_refresh_uses_existing_pin_and_rejects_rebind(self, app_home):
        from kiro_crew.config.loader import _invalidate_config_cache

        self._seed_registry_app()
        (app_home / "config.json").write_text(
            json.dumps(
                {
                    "agent": {
                        "apps_allow_third_party": False,
                        "apps_trusted": ["self-app"],
                        "apps_trusted_repositories": {"self-app": self._REPOSITORY},
                    }
                }
            ),
            encoding="utf-8",
        )
        _invalidate_config_cache()

        refreshed = register_external_app(
            "self-app",
            "2.0.0",
            "Self App v2",
            source="https://caller.example.test/spoof.git",
            origin="external",
        )
        assert refreshed.ok, refreshed.error
        self._assert_provenance()

        rebound = register_external_app(
            "self-app",
            "9.9.9",
            "Rebound App",
            source="registry:self-app",
            origin="registry",
            source_repository="https://attacker.example.test/owner/self-app.git",
        )
        assert not rebound.ok
        assert rebound.error_code == "app_trust_repository_mismatch"
        self._assert_provenance()
        meta = _read_installed("self-app")
        assert meta is not None
        assert meta.version == "2.0.0"

    def test_local_external_registration_remains_idempotent(self, app_home):
        first = register_external_app(
            "self-app",
            "1.0.0",
            "Self App",
            source="C:/local/first",
            origin="external",
        )
        second = register_external_app(
            "self-app",
            "2.0.0",
            "Self App v2",
            source="C:/local/second",
            origin="external",
        )

        assert first.ok, first.error
        assert second.ok, second.error
        meta = _read_installed("self-app")
        assert meta is not None
        assert meta.version == "2.0.0"
        assert meta.displayName == "Self App v2"
        assert meta.source == "C:/local/second"
        assert meta.sourceUrl == ""
        assert meta.origin == "external"
