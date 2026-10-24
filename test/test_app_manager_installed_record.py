"""The installed-app record: InstalledApp and AppResult serialisation, field fallbacks
and legacy-field migration, credential stripping at the write boundary, the
property tests, and ``app_enabled_state`` telling an unreadable record from an
absent one. ``test_app_manager.py`` heads the suite and lists its parts.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from app_manager_helpers import app_home_fixture  # noqa: F401 - fixture
from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew.apps.manager import (
    AppResult,
    InstalledApp,
    _read_installed,
    _write_installed,
    app_enabled_state,
    get_app,
    is_app_enabled,
    list_apps,
    uninstall_app,
)

# ---------------------------------------------------------------------------
# InstalledApp dataclass
# ---------------------------------------------------------------------------


class TestInstalledApp:
    def test_round_trip(self):
        meta = InstalledApp(
            name="my-app",
            version="1.0.0",
            displayName="My App",
            enabled=True,
            installedAt="2026-04-10T00:00:00Z",
            source="/tmp/src",
            origin="registry",
            resources="gateway",
            lifecycle="gateway",
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
        meta = InstalledApp.from_dict(
            {
                "name": "channels",
                "origin": "builtin",
                "resources": "gateway",
                "lifecycle": "locked",
            }
        )
        assert meta.origin == "builtin"
        assert meta.lifecycle == "locked"

    def test_external_fields(self):
        meta = InstalledApp.from_dict(
            {
                "name": "some-external-app",
                "origin": "external",
                "resources": "app",
                "lifecycle": "app",
            }
        )
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

    @pytest.mark.parametrize(
        "coordinate",
        [
            "/tmp/pkg:a@host:path",
            "./pkg:a@host:path",
            "../pkg:a@host:path",
            r"C:\work\pkg:a@host:path",
            "C:/work/pkg:a@host:path",
            "registry:my-app",
            "deploy@host.example:Owner/Repo.git",
            "deploy:local-segment@host.example:Owner/Repo.git",
        ],
    )
    def test_write_boundary_preserves_non_uri_source_metadata(self, app_home, coordinate: str):
        _write_installed(
            "metadata-app",
            InstalledApp(
                name="metadata-app",
                source=coordinate,
                sourceRegistry=coordinate,
            ),
        )

        stored = _read_installed("metadata-app")
        assert stored is not None
        assert stored.source == coordinate
        assert stored.sourceRegistry == coordinate

    @pytest.mark.parametrize(
        "scheme,leading_whitespace",
        [("https", ""), ("ftp", ""), ("s3", "  "), ("x", "")],
    )
    def test_write_boundary_strips_explicit_uri_credentials(
        self, app_home, scheme: str, leading_whitespace: str
    ):
        raw = (
            f"{leading_whitespace}{scheme}://user:secret@example.test/Owner/Repo"
            "?token=secret#private"
        )
        safe = f"{scheme}://example.test/Owner/Repo"
        _write_installed(
            "metadata-app",
            InstalledApp(
                name="metadata-app",
                source=raw,
                sourceUrl=raw,
                sourceRegistry=raw,
            ),
        )

        stored = _read_installed("metadata-app")
        assert stored is not None
        assert stored.source == safe
        assert stored.sourceUrl == safe
        assert stored.sourceRegistry == safe

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
        meta = InstalledApp.from_dict(
            {
                "name": "old",
                "managed": "kirocrew",
                "source": "/Users/dev/my-tool",
            }
        )
        assert meta.origin == "local"
        assert meta.resources == "gateway"
        assert meta.lifecycle == "gateway"

    def test_migrate_managed_kirocrew_registry_source(self):
        """Old managed='kirocrew' with registry: source → origin='registry'."""
        meta = InstalledApp.from_dict(
            {
                "name": "old",
                "managed": "kirocrew",
                "source": "registry:my-app",
            }
        )
        assert meta.origin == "registry"
        assert meta.resources == "gateway"
        assert meta.lifecycle == "gateway"

    def test_migrate_skipped_when_origin_present(self):
        """If origin is already in the dict, migration is skipped even with schemaVersion < 2."""
        meta = InstalledApp.from_dict(
            {
                "name": "old",
                "managed": "self",
                "origin": "local",
                "schemaVersion": 1,
            }
        )
        # origin was explicitly set — migration should NOT override it
        assert meta.origin == "local"
        assert meta.resources == "gateway"  # default, not migrated to "app"

    @pytest.mark.parametrize("origin", ["builtin", "local"])
    def test_retired_uninstall_refuses_locked_non_qualifying_app(self, app_home, origin):
        from kiro_crew.apps.manager import app_dir

        name = "agent-worlds"
        _write_installed(name, InstalledApp(name=name, origin=origin, lifecycle="locked"))
        path = app_dir(name) / "installed.json"
        before = path.read_bytes()
        result = uninstall_app(name, retired_builtin=True)
        assert not result.ok and result.error_code == "not_orphaned"
        assert path.read_bytes() == before

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
    # Feature: app-classification-redesign, Property 1: InstalledApp serialisation round-trips
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
            name=name,
            version=version,
            displayName=f"App {name}",
            enabled=enabled,
            installedAt="2026-01-01T00:00:00Z",
            source="test",
            origin=origin,
            resources=resources,
            lifecycle=lifecycle,
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

    # Feature: app-classification-redesign, Property 2: invalid field values fall back to defaults
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
        meta = InstalledApp.from_dict(
            {
                "name": "test",
                "origin": bad_origin,
                "resources": bad_resources,
                "lifecycle": bad_lifecycle,
            }
        )
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


class TestEnabledStateTellsUnreadableFromNotInstalled:
    """``app_enabled_state`` exists to keep those apart, and ``is_file()`` cannot.

    ``Path.is_file()`` answers a silent False for five path shapes that are not
    absence -- a dangling symlink, a directory or fifo in the file's place, a symlink
    loop, and a non-directory parent component -- so leading with it made each of them
    indistinguishable from a deliberate uninstall. A genuine ``stat`` fault such as
    EACCES was already correct, because ``is_file`` re-raises it.

    The cost is asymmetric now that ``hook_reconcile`` consumes this state unattended
    on a 15s tick: a wrong ``False`` fires an automatic teardown of a live app's
    routes and modules, while a ``None`` defers to the next tick.
    """

    def test_a_dangling_symlink_is_unknown(self, app_home):
        app_root = app_home / "apps" / "shape-probe"
        app_root.mkdir(parents=True)
        (app_root / "installed.json").symlink_to(app_root / "gone.json")

        assert app_enabled_state("shape-probe") is None

    def test_a_directory_in_its_place_is_unknown(self, app_home):
        (app_home / "apps" / "shape-probe" / "installed.json").mkdir(parents=True)

        assert app_enabled_state("shape-probe") is None

    @pytest.mark.skipif(
        not hasattr(os, "mkfifo"),
        reason="a FIFO is a POSIX-only path shape; os.mkfifo does not exist on Windows",
    )
    def test_a_fifo_in_its_place_is_unknown(self, app_home):
        app_root = app_home / "apps" / "shape-probe"
        app_root.mkdir(parents=True)
        os.mkfifo(app_root / "installed.json")

        assert app_enabled_state("shape-probe") is None

    def test_a_symlink_loop_is_unknown(self, app_home):
        app_root = app_home / "apps" / "shape-probe"
        app_root.mkdir(parents=True)
        (app_root / "installed.json").symlink_to(app_root / "other.json")
        (app_root / "other.json").symlink_to(app_root / "installed.json")

        assert app_enabled_state("shape-probe") is None

    def test_a_non_directory_parent_is_unknown(self, app_home):
        """Something occupies the app directory's own path."""
        (app_home / "apps").mkdir(parents=True, exist_ok=True)
        (app_home / "apps" / "shape-probe").write_text("not a directory", encoding="utf-8")

        assert app_enabled_state("shape-probe") is None

    def test_a_non_directory_parent_is_unknown_under_the_windows_error_class(
        self, app_home, monkeypatch
    ):
        """The verdict must come from the path's SHAPE, not the exception class.

        One condition, two classes: POSIX raises NotADirectoryError (ENOTDIR) while
        Windows maps ERROR_PATH_NOT_FOUND to ENOENT and raises FileNotFoundError --
        the same class a genuinely missing file raises. Keying absence on that class
        told the truth on Linux and not on Windows, where a wrong-shape parent still
        read as a deliberate uninstall and the hook reconciler would tear a LIVE app
        down for it.

        The Windows mapping is injected so this runs on every platform; the natural
        path is covered by the test above on whichever platform produces it.
        """
        (app_home / "apps").mkdir(parents=True, exist_ok=True)
        (app_home / "apps" / "shape-probe").write_text("not a directory", encoding="utf-8")

        real_stat = Path.stat

        def as_windows(self, *args, **kwargs):
            try:
                return real_stat(self, *args, **kwargs)
            except NotADirectoryError as exc:
                raise FileNotFoundError(2, "No such file or directory", str(self)) from exc

        monkeypatch.setattr(Path, "stat", as_windows)

        assert app_enabled_state("shape-probe") is None

    def test_genuine_absence_stays_false_under_the_windows_error_class(self, app_home, monkeypatch):
        """The control for the above: the shape check must not swallow real absence.

        Uninstall depends on this False, so a fix for the wrong-shape case that also
        reported unknown for a missing file would break the caller it exists to serve.
        """
        (app_home / "apps" / "shape-probe").mkdir(parents=True)

        real_stat = Path.stat

        def as_windows(self, *args, **kwargs):
            try:
                return real_stat(self, *args, **kwargs)
            except NotADirectoryError as exc:
                raise FileNotFoundError(2, "No such file or directory", str(self)) from exc

        monkeypatch.setattr(Path, "stat", as_windows)

        assert app_enabled_state("shape-probe") is False

    def test_a_dangling_windows_junction_ancestor_is_unknown(self, app_home, monkeypatch):
        """A dangling junction occupies the path while looking absent to every predicate.

        The same mistake as the error-class one, one predicate over: ``is_symlink`` is
        False for a Windows directory junction, so a junction whose target is gone
        presents as ``is_dir=False, exists=False, is_symlink=False`` -- indistinguishable
        from nothing at all, which is why this walk stepped over it and reported genuine
        absence. ``app_enabled_state`` then answers False and the hook reconciler tears a
        LIVE app down.

        Fed as a SHAPE rather than a real junction: os.mkfifo has a POSIX equivalent to
        skip for, but a junction has none at all, so requiring one would mean this case
        is only ever exercised on the platform it breaks. The three ordinary predicates
        are already False for a path that does not exist, which IS the dangling
        junction's shape, so only the junction probe has to be stood in for.
        """
        (app_home / "apps").mkdir(parents=True, exist_ok=True)
        junction = app_home / "apps" / "shape-probe"
        assert not junction.exists() and not junction.is_symlink() and not junction.is_dir()

        monkeypatch.setattr(
            "kiro_crew.apps.manager.is_link_or_junction",
            lambda path: Path(path) == junction,
        )

        assert app_enabled_state("shape-probe") is None

    def test_a_dangling_junction_at_the_metadata_path_is_unknown(self, app_home, monkeypatch):
        """The same shape ON the metadata path, which the ancestor walk cannot reach.

        ``Path.parents`` excludes the path itself, so `_absence_is_genuine` inspects
        every ancestor and never `installed.json`. The self-check beside it asked only
        `is_symlink`, which a junction answers False, so a junction occupying the
        metadata path read as a deliberate uninstall while every ancestor was a healthy
        directory.
        """
        (app_home / "apps" / "shape-probe").mkdir(parents=True)
        meta = app_home / "apps" / "shape-probe" / "installed.json"
        assert not meta.exists() and not meta.is_symlink() and not meta.is_dir()

        monkeypatch.setattr(
            "kiro_crew.apps.manager.is_link_or_junction",
            lambda path: Path(path) == meta,
        )

        assert app_enabled_state("shape-probe") is None

    def test_an_unreadable_directory_was_already_correct(self, app_home):
        """The boundary: is_file() RE-RAISES EACCES, so this case never regressed.

        Kept so the distinction is pinned -- the bug was path SHAPES reading as
        absence, not permission faults.
        """
        app_root = app_home / "apps" / "shape-probe"
        app_root.mkdir(parents=True)
        (app_root / "installed.json").write_text('{"enabled": true}', encoding="utf-8")
        os.chmod(app_root, 0o000)
        try:
            if os.access(app_root / "installed.json", os.R_OK):
                pytest.skip("this user bypasses directory permissions")
            assert app_enabled_state("shape-probe") is None
        finally:
            os.chmod(app_root, stat.S_IRWXU)

    def test_nothing_there_is_still_a_definite_false(self, app_home):
        """The one case that DOES mean not installed, which uninstall relies on."""
        (app_home / "apps" / "shape-probe").mkdir(parents=True)

        assert app_enabled_state("shape-probe") is False

    def test_a_readable_record_still_reports_its_flag(self, app_home):
        app_root = app_home / "apps" / "shape-probe"
        app_root.mkdir(parents=True)
        (app_root / "installed.json").write_text(
            '{"name": "shape-probe", "enabled": false}', encoding="utf-8"
        )

        assert app_enabled_state("shape-probe") is False

        (app_root / "installed.json").write_text(
            '{"name": "shape-probe", "enabled": true}', encoding="utf-8"
        )

        assert app_enabled_state("shape-probe") is True


class TestEnabledReadsStrictly:
    """Only a JSON boolean ``true`` (or a missing key) reads as enabled.

    ``bool(data.get("enabled", True))`` made any non-empty string enabled, so
    ``"enabled": "false"`` turned an app ON for every reader. Each reader family
    is checked, since they all share ``InstalledApp.from_dict``.
    """

    @pytest.mark.parametrize("value", ["false", "true", "0", "no", 1, 0, None, [], {}])
    def test_from_dict_reads_a_non_boolean_as_disabled(self, value):
        assert InstalledApp.from_dict({"name": "x", "enabled": value}).enabled is False

    @pytest.mark.parametrize("value", [True, False])
    def test_from_dict_keeps_a_real_boolean(self, value):
        assert InstalledApp.from_dict({"name": "x", "enabled": value}).enabled is value

    def test_a_missing_key_still_reads_as_enabled(self):
        assert InstalledApp.from_dict({"name": "x"}).enabled is True

    def _write_record(self, app_home, enabled):
        app_root = app_home / "apps" / "strict-probe"
        app_root.mkdir(parents=True)
        (app_root / "installed.json").write_text(
            json.dumps({"name": "strict-probe", "enabled": enabled}), encoding="utf-8"
        )

    def test_every_reader_reports_a_string_false_as_disabled(self, app_home):
        self._write_record(app_home, "false")

        assert app_enabled_state("strict-probe") is False
        assert is_app_enabled("strict-probe") is False
        assert get_app("strict-probe")["enabled"] is False
        rows = [r for r in list_apps() if r.get("name") == "strict-probe"]
        assert [r["enabled"] for r in rows] == [False]

    def test_every_reader_reports_a_real_true_as_enabled(self, app_home):
        self._write_record(app_home, True)

        assert app_enabled_state("strict-probe") is True
        assert is_app_enabled("strict-probe") is True
        assert get_app("strict-probe")["enabled"] is True
        rows = [r for r in list_apps() if r.get("name") == "strict-probe"]
        assert [r["enabled"] for r in rows] == [True]
