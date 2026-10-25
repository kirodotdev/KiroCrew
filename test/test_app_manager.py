"""Tests for kiro_crew.apps.manager — App lifecycle management.

This file holds the lifecycle doors: name admission, source validation, install, uninstall
and data preservation, the admission-policy gate, enable/disable and listing. The rest of the
suite sits beside it: ``test_app_manager_installed_record.py`` (the installed.json record),
``test_app_manager_copy_tree.py`` (the install and update copy transaction),
``test_app_manager_installed_tree.py`` (the installed-tree preview) and
``test_app_manager_registration.py`` (builtin and external registration). Shared fixtures
and builders are in ``app_manager_helpers.py``.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from app_manager_helpers import app_home_fixture  # noqa: F401 - fixture
from app_manager_helpers import _DATA_IS_A_FILE, _DATA_IS_A_LINK, _make_app_source

from conftest import requires_symlinks
from kiro_crew import platform_compat
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
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
# App-name admission contract
# ---------------------------------------------------------------------------


class TestUnportableAppName:
    """``nul`` must be refused by EVERY door, not just the manifest one.

    These are behavior-level rather than one test per gate: the defect was not
    that a single check was wrong, it was that three doors carried three
    different name checks and a name refused at one was admitted at another.
    Asserting the outcome at each entry point is what actually pins the shared
    contract — a future fourth door that grows its own check fails here.
    """

    def test_install_refuses_it_before_anything_lands_on_disk(self, tmp_path, app_home):
        """Windows cannot create apps/nul/, so install must refuse rather than
        half-create an app that can never start.

        The source directory is deliberately NOT named ``nul``: a POSIX host can
        author that tree and hand it to a Windows host, which is the case the
        contract exists for, and the destination name comes from the manifest
        anyway. Naming the source dir ``nul`` would also make the test itself
        unrunnable on Windows.
        """
        src = tmp_path / "source" / "nul-src"
        src.mkdir(parents=True)
        (src / APP_MANIFEST_FILENAME).write_text(
            json.dumps(
                {
                    "name": "nul",
                    "version": "1.0.0",
                    "displayName": "Null App",
                    "description": "A test app for unit tests",
                    "author": "tester",
                }
            )
        )
        result = install_app(src)
        assert not result.ok
        assert "not portable" in result.error, result.error
        assert not (app_home / "apps" / "nul").exists()

    def test_register_external_refuses_it_before_materialization(self, app_home):
        from kiro_crew.apps.manager import register_external_app

        result = register_external_app("nul", "1.0.0", "Null App")
        assert not result.ok
        assert "not portable" in result.error, result.error
        assert _read_installed("nul") is None
        assert not (app_home / "apps" / "nul").exists()

    def test_builtin_registration_refuses_it(self):
        from kiro_crew.apps.manager import _validate_builtin_app

        errors = _validate_builtin_app(
            {
                "name": "nul",
                "version": "1.0.0",
                "displayName": "Null App",
                "description": "d",
                "author": "tester",
            }
        )
        assert any("not portable" in e for e in errors), errors

    def test_a_normal_app_still_installs(self, tmp_path, app_home):
        """Preservation: the contract refuses one name, not names in general."""
        result = install_app(_make_app_source(tmp_path, name="null-app"))
        assert result.ok, result.error
        assert (app_home / "apps" / "null-app").is_dir()


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

    def test_installed_app_may_not_declare_ui_overlays(self, tmp_path):
        """An overlay must name a component compiled into the dashboard bundle.

        There is no per-overlay ``entryPoint`` the way ``ui.pages`` has one, so an
        installed app cannot supply the component its declaration points at. Accepting
        the manifest here would install an app whose overlay can only fail later as a
        browser console warning -- the one channel an app author never reads. The
        refusal belongs at install, which is the channel they do read.
        """
        src = _make_app_source(tmp_path)
        raw = json.loads((src / APP_MANIFEST_FILENAME).read_text())
        raw["ui"] = {"overlays": [{"id": "command-bar", "replaces": "quick-search"}]}
        (src / APP_MANIFEST_FILENAME).write_text(json.dumps(raw))
        errors = _validate_source_path(src)
        assert any("ui.overlays is not available to installed apps" in e for e in errors)

    def test_installed_app_without_overlays_is_unaffected(self, tmp_path):
        # Guards the guard: the refusal must key on a declared overlay, not on the
        # presence of a ui block.
        src = _make_app_source(tmp_path)
        raw = json.loads((src / APP_MANIFEST_FILENAME).read_text())
        raw["ui"] = {"pages": [{"route": "/apps/x", "label": "X"}]}
        (src / APP_MANIFEST_FILENAME).write_text(json.dumps(raw))
        assert _validate_source_path(src) == []


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


class TestInstall:
    def test_install_from_directory(self, tmp_path, app_home):
        src = _make_app_source(tmp_path)
        result = install_app(src)
        assert result.ok
        assert result.name == "test-app"
        installed_dir = app_home / "apps" / "test-app"
        assert installed_dir.is_dir()
        assert (installed_dir / APP_MANIFEST_FILENAME).is_file()
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
        (src / "agents").mkdir()
        (src / "agents" / "analyst.json").write_text('{"name": "analyst"}')
        (src / "skills" / "triage").mkdir(parents=True)
        (src / "skills" / "triage" / "SKILL.md").write_text("# Triage skill")

        result = install_app(src)
        assert result.ok
        installed = app_home / "apps" / "test-app"
        assert (installed / "agents" / "analyst.json").is_file()
        assert (installed / "skills" / "triage" / "SKILL.md").is_file()

    @requires_symlinks
    def test_a_shipped_app_secret_link_is_unlinked_before_the_secret_is_written(
        self, tmp_path, app_home
    ):
        """`.app_secret` is the gateway's. The copy keeps an in-tree link as a
        link, and `write_app_secret` opens the path it is given -- so an app
        shipping `.app_secret -> ui/leak.js` would have the freshly generated
        secret written THROUGH the link into a file the unauthenticated UI route
        serves. The install removes whatever the source shipped under that name,
        unfollowed, before writing its own regular file there."""
        src = _make_app_source(tmp_path)
        (src / "ui").mkdir()
        (src / "ui" / "leak.js").write_text("export const leak = 1;\n", encoding="utf-8")
        (src / ".app_secret").symlink_to(Path("ui") / "leak.js")

        result = install_app(src)

        assert result.ok, result.error
        installed = app_home / "apps" / "test-app"
        assert (installed / "ui" / "leak.js").read_text(encoding="utf-8") == (
            "export const leak = 1;\n"
        )  # the link's target was never written to
        secret = installed / ".app_secret"
        assert not os.path.islink(secret) and secret.is_file()
        assert secret.read_text(encoding="utf-8").strip()  # the gateway's own value
        assert (src / "ui" / "leak.js").read_text(encoding="utf-8") == "export const leak = 1;\n"

    def test_a_shipped_app_secret_file_is_replaced_by_the_gateway_s_own(self, tmp_path, app_home):
        src = _make_app_source(tmp_path)
        (src / ".app_secret").write_text("the-author-s-choice\n", encoding="utf-8")

        result = install_app(src)

        assert result.ok, result.error
        secret = app_home / "apps" / "test-app" / ".app_secret"
        assert secret.is_file()
        assert secret.read_text(encoding="utf-8") != "the-author-s-choice\n"

    @requires_symlinks
    def test_a_shipped_data_link_is_unlinked_before_preserved_data_is_put_back(
        self, tmp_path, app_home
    ):
        """A default uninstall leaves `data/` behind; the next install puts it back
        over whatever the source shipped under that name. A shipped `data -> ui`
        link is unlinked -- never traversed, never left for the move to fail on --
        exactly as the preview copy the desktop gate judges drops it."""
        assert install_app(_make_app_source(tmp_path)).ok
        installed = app_home / "apps" / "test-app"
        (installed / "data" / "state.json").write_text('{"kept": true}', encoding="utf-8")
        assert uninstall_app("test-app").ok  # keeps data/
        assert (installed / "data" / "state.json").is_file()

        src = _make_app_source(tmp_path / "again")
        (src / "ui").mkdir()
        (src / "ui" / "index.js").write_text("", encoding="utf-8")
        (src / "data").symlink_to(Path("ui"))

        result = install_app(src)

        assert result.ok, result.error
        assert not os.path.islink(installed / "data")
        assert (installed / "data" / "state.json").read_text(encoding="utf-8") == '{"kept": true}'
        assert (installed / "ui" / "index.js").is_file()  # the link's target, untouched

    def test_a_root_data_file_is_refused_before_the_installed_record_is_written(
        self, tmp_path, app_home
    ):
        """`app_data_dir()` is `mkdir(exist_ok=True)` at `<app dir>/data`, which a
        shipped FILE of that name makes raise -- and it ran AFTER installed.json was
        written, so the app was half-installed: a record, no data directory, no
        secret. The copied tree is now asked the data directory's own question
        before the record exists, and the whole copy goes with the refusal."""
        src = _make_app_source(tmp_path)
        (src / "data").write_text("a file where the gateway's data directory goes\n", encoding="utf-8")

        result = install_app(src)

        assert not result.ok
        assert result.error == _DATA_IS_A_FILE, result.error
        assert _read_installed("test-app") is None
        assert not (app_home / "apps" / "test-app").exists()  # no partial copy either

        # A preserved `data/` (left by a default uninstall) is put back OVER the
        # shipped file, as over any other shape: what stands there is then a
        # directory, and the same source installs.
        assert install_app(_make_app_source(tmp_path / "first")).ok
        installed = app_home / "apps" / "test-app"
        (installed / "data" / "state.json").write_text('{"kept": true}', encoding="utf-8")
        assert uninstall_app("test-app").ok
        result = install_app(src)
        assert result.ok, result.error
        assert (installed / "data" / "state.json").read_text(encoding="utf-8") == '{"kept": true}'

    @requires_symlinks
    def test_a_link_planted_at_the_temp_name_refuses_the_install_before_anything_moves(
        self, tmp_path, app_home
    ):
        """`.<name>-data-tmp` is where the install parks a leftover `data/` while it
        copies. A LINK planted there is not the gateway's stale copy: moving the
        directory onto it would deposit `data/` inside the link's target, and the
        restore would rename the link -- not the data -- back. Refused before any
        move: the leftover directory stays where it is, the link's target receives
        nothing, the link itself is untouched, no record is written."""
        assert install_app(_make_app_source(tmp_path / "first")).ok
        installed = app_home / "apps" / "test-app"
        (installed / "data" / "state.json").write_text('{"kept": true}', encoding="utf-8")
        assert uninstall_app("test-app").ok  # default uninstall keeps data/ behind
        assert (installed / "data" / "state.json").is_file()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        planted = app_home / "apps" / ".test-app-data-tmp"
        planted.symlink_to(elsewhere, target_is_directory=True)

        result = install_app(_make_app_source(tmp_path))

        assert not result.ok
        assert result.error == (
            f"{planted} is a link; Kiro Crew keeps the app's data directory at that name "
            "while it replaces the app files and cannot use it -- remove it first"
        ), result.error
        assert _read_installed("test-app") is None
        assert (installed / "data" / "state.json").read_text(encoding="utf-8") == '{"kept": true}'
        assert list(elsewhere.iterdir()) == []  # nothing was moved through the link
        assert os.path.islink(planted)

        # Remove the planted link and the same source installs, its data restored.
        planted.unlink()
        result = install_app(_make_app_source(tmp_path / "again"))
        assert result.ok, result.error
        assert (installed / "data" / "state.json").read_text(encoding="utf-8") == '{"kept": true}'

    @requires_symlinks
    def test_a_root_data_link_is_refused_whatever_it_resolves_to(self, tmp_path, app_home):
        """A link at `data` is refused before the record on every host, dangling or
        not: `mkdir(exist_ok=True)` fails on a dangling one, and a link RESOLVING to
        an in-tree directory would pass it -- then the next update would move the
        LINK aside, where its relative target does not resolve, put nothing back,
        and delete the directory it named with the retired tree. Refused here, the
        shape never reaches an update; the directory the link named is untouched."""
        src = _make_app_source(tmp_path)
        (src / "data").symlink_to(Path("nowhere"))
        result = install_app(src)
        assert not result.ok
        assert result.error == _DATA_IS_A_LINK, result.error
        assert _read_installed("test-app") is None
        assert not (app_home / "apps" / "test-app").exists()

        linked = _make_app_source(tmp_path / "linked")
        (linked / "state").mkdir()
        (linked / "state" / "kept.json").write_text('{"kept": true}', encoding="utf-8")
        (linked / "data").symlink_to(Path("state"), target_is_directory=True)
        result = install_app(linked)
        assert not result.ok
        assert result.error == _DATA_IS_A_LINK, result.error
        assert _read_installed("test-app") is None
        assert not (app_home / "apps" / "test-app").exists()  # the copy went with the refusal
        # The directory the link named is the source's own and is untouched.
        assert (linked / "state" / "kept.json").read_text(encoding="utf-8") == '{"kept": true}'
        assert os.path.islink(linked / "data")

    @requires_symlinks
    def test_a_pre_existing_data_link_at_a_recordless_app_dir_is_refused_before_the_transaction(
        self, tmp_path, app_home
    ):
        """The THIRD ask of the same predicate. An app directory already standing at
        the destination with no `installed.json` (a prior default uninstall's
        leftover, or an orphaned partial copy) whose `data` is a link to a directory
        elsewhere: the move-aside skips what is not an owned directory, so the orphan
        cleanup would `rmtree` the app directory and unlink the link, the post-copy
        ask would see only the source's `data`, and the install would return
        `ok=True` with a fresh empty `data/` -- the pre-existing entry gone with no
        refusal, no log line and no error, where `update_app` and `uninstall_app`
        refuse the identical shape before mutating. Refused here, before anything
        moves: the link still stands and still resolves, the directory it names is
        intact, no record and no temp copy appear."""
        from kiro_crew.apps.manager import app_dir

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "sentinel.json").write_text('{"kept": true}', encoding="utf-8")
        dest = app_dir("test-app")
        dest.mkdir(parents=True)
        (dest / "data").symlink_to(elsewhere, target_is_directory=True)
        assert _read_installed("test-app") is None

        result = install_app(_make_app_source(tmp_path))

        assert not result.ok
        assert result.error == _DATA_IS_A_LINK, result.error
        assert os.path.islink(dest / "data") and (dest / "data").resolve() == elsewhere.resolve()
        assert (elsewhere / "sentinel.json").read_text(encoding="utf-8") == '{"kept": true}'
        assert _read_installed("test-app") is None
        assert not (dest / APP_MANIFEST_FILENAME).exists()  # nothing was copied
        assert not (dest.parent / ".test-app-data-tmp").exists()

    def test_a_pre_existing_data_file_at_a_recordless_app_dir_is_refused_before_the_transaction(
        self, tmp_path, app_home
    ):
        """Same ask, the regular-file shape: `mkdir(exist_ok=True)` cannot stand on a
        file and the move-aside skips it, so without the refusal the orphan cleanup
        would delete it and the install would succeed over a fresh empty `data/`.
        Refused before anything moves; the file and its content are intact."""
        from kiro_crew.apps.manager import app_dir

        dest = app_dir("test-app")
        dest.mkdir(parents=True)
        (dest / "data").write_text("a regular file so named\n", encoding="utf-8")

        result = install_app(_make_app_source(tmp_path))

        assert not result.ok
        assert result.error == _DATA_IS_A_FILE, result.error
        assert (dest / "data").read_text(encoding="utf-8") == "a regular file so named\n"
        assert _read_installed("test-app") is None
        assert not (dest / APP_MANIFEST_FILENAME).exists()
        assert not (dest.parent / ".test-app-data-tmp").exists()

    def test_a_pre_existing_data_junction_at_a_recordless_app_dir_is_refused_before_the_transaction(
        self, tmp_path, app_home, monkeypatch
    ):
        """The Windows shape of the same ask, fed through the module's junction seam
        (no POSIX junction exists): a real directory at `data` that
        `is_link_or_junction` reports as a junction is refused with the link
        sentence, and the directory and its content are untouched."""
        from kiro_crew.apps.manager import app_dir

        dest = app_dir("test-app")
        (dest / "data").mkdir(parents=True)
        (dest / "data" / "state.json").write_text('{"k": 1}', encoding="utf-8")
        junction = dest / "data"
        monkeypatch.setattr(
            "kiro_crew.apps.manager.is_link_or_junction", lambda path: Path(path) == junction
        )

        result = install_app(_make_app_source(tmp_path))

        assert not result.ok
        assert result.error == _DATA_IS_A_LINK, result.error
        assert (dest / "data" / "state.json").read_text(encoding="utf-8") == '{"k": 1}'
        assert _read_installed("test-app") is None
        assert not (dest / APP_MANIFEST_FILENAME).exists()
        assert not (dest.parent / ".test-app-data-tmp").exists()


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------


class TestUninstall:
    @pytest.mark.parametrize("existing", [False, True])
    def test_uninstall_uses_exclusive_dependency_lock_creation(
        self, tmp_path, app_home, monkeypatch, existing
    ):
        install_app(_make_app_source(tmp_path))
        data = app_home / "apps" / "test-app" / "data"
        marker = data / "user.txt"
        marker.write_text("keep me", encoding="utf-8")
        lock = data / ".kirocrew-deps.lock"
        if existing:
            lock.write_text("existing lock", encoding="utf-8")
        calls = []
        real_open = os.open

        def record_open(path, flags, mode=0o777, *, dir_fd=None):
            if str(path).endswith(".kirocrew-deps.lock"):
                calls.append((flags, dir_fd))
            return real_open(path, flags, mode, dir_fd=dir_fd)

        if real_open in os.supports_dir_fd:
            monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {record_open})
        monkeypatch.setattr(os, "open", record_open)
        result = uninstall_app("test-app", keep_data=True)
        assert result.ok, result.error
        assert len(calls) == (2 if existing else 1)
        assert calls[0][0] & os.O_EXCL
        assert calls[0][0] & os.O_CREAT
        for flags, _fd in calls:
            assert flags & os.O_RDWR
            assert not flags & os.O_TRUNC
            if hasattr(os, "O_NOFOLLOW"):
                assert flags & os.O_NOFOLLOW
        if existing:
            assert not calls[1][0] & (os.O_CREAT | os.O_EXCL)
            assert calls[1][1] == calls[0][1]
        assert marker.read_text(encoding="utf-8") == "keep me"
        assert lock.is_file()

    def test_uninstall_refuses_a_dependency_lock_that_vanishes_before_reopen(
        self, tmp_path, app_home, monkeypatch
    ):
        install_app(_make_app_source(tmp_path))
        root = app_home / "apps" / "test-app"
        marker = root / "data" / "user.txt"
        marker.write_text("keep me", encoding="utf-8")
        calls = []
        real_open = os.open

        def race_open(path, flags, mode=0o777, *, dir_fd=None):
            if str(path).endswith(".kirocrew-deps.lock"):
                calls.append(flags)
                if len(calls) == 1:
                    raise FileExistsError("a contender created the lock")
            return real_open(path, flags, mode, dir_fd=dir_fd)

        if real_open in os.supports_dir_fd:
            monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {race_open})
        monkeypatch.setattr(os, "open", race_open)
        result = uninstall_app("test-app", keep_data=True)
        assert not result.ok
        assert len(calls) == 2
        assert not calls[1] & (os.O_CREAT | os.O_EXCL)
        assert (root / APP_MANIFEST_FILENAME).is_file()
        assert marker.read_text(encoding="utf-8") == "keep me"
        assert not (root / "data" / ".kirocrew-deps.lock").exists()

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

    def test_uninstall_removes_the_agent_lock_and_gateway_overlay(
        self, tmp_path, app_home, monkeypatch
    ):
        # Deregistration unlinks ``<app>--<agent>.json``; uninstall also removes
        # the empty ``<app>--<agent>.lock`` beside it and the MCP gateway's
        # overlay copy ``mcp-gateway/agents/<app>--<agent>.json``.
        from kiro_crew.apps import bridges

        agents = tmp_path / "kiro-agents"
        agents.mkdir()
        monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", agents)
        overlay = app_home / "mcp-gateway" / "agents"
        overlay.mkdir(parents=True)

        mine_lock = agents / "test-app--bg.lock"
        mine_lock.touch()
        mine_overlay = overlay / "test-app--bg.json"
        mine_overlay.write_text("{}", encoding="utf-8")
        # Another app's files, and a user agent's, must be left alone.
        other_lock = agents / "test-app-two--bg.lock"
        other_lock.touch()
        other_overlay = overlay / "test-app-two--bg.json"
        other_overlay.write_text("{}", encoding="utf-8")
        user_overlay = overlay / "kirocrew.json"
        user_overlay.write_text("{}", encoding="utf-8")

        install_app(_make_app_source(tmp_path))
        result = uninstall_app("test-app")

        assert result.ok
        assert not mine_lock.exists()
        assert not mine_overlay.exists()
        assert other_lock.exists()
        assert other_overlay.exists()
        assert user_overlay.exists()

    def test_uninstall_keeps_agent_residue_whose_spec_or_content_survives(
        self, tmp_path, app_home, monkeypatch
    ):
        # A spec that is still on disk (deregistration did not run or failed)
        # keeps its lock and overlay; a non-empty ``.lock`` is not the launcher's
        # empty sentinel and is never deleted.
        from kiro_crew.apps import bridges

        agents = tmp_path / "kiro-agents"
        agents.mkdir()
        monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", agents)
        overlay = app_home / "mcp-gateway" / "agents"
        overlay.mkdir(parents=True)

        (agents / "test-app--live.json").write_text("{}", encoding="utf-8")
        live_lock = agents / "test-app--live.lock"
        live_lock.touch()
        live_overlay = overlay / "test-app--live.json"
        live_overlay.write_text("{}", encoding="utf-8")
        full_lock = agents / "test-app--gone.lock"
        full_lock.write_text("not empty", encoding="utf-8")

        install_app(_make_app_source(tmp_path))
        assert uninstall_app("test-app").ok

        assert live_lock.exists()
        assert live_overlay.exists()
        assert full_lock.exists()

    def test_uninstall_leaves_a_held_agent_lock(self, tmp_path, app_home, monkeypatch):
        from kiro_crew.apps import bridges

        agents = tmp_path / "kiro-agents"
        agents.mkdir()
        monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", agents)
        held = agents / "test-app--bg.lock"
        held.touch()
        monkeypatch.setattr(platform_compat, "try_acquire_lock", lambda fd, exclusive: False)

        install_app(_make_app_source(tmp_path))
        assert uninstall_app("test-app").ok

        assert held.exists()

    def test_uninstall_not_installed(self, app_home):
        result = uninstall_app("nonexistent")
        assert not result.ok
        assert "not installed" in result.error

    def test_uninstall_keep_data(self, tmp_path, app_home):
        src = _make_app_source(tmp_path)
        install_app(src)
        data_dir = app_home / "apps" / "test-app" / "data"
        (data_dir / "cache.json").write_text('{"key": "value"}')

        result = uninstall_app("test-app", keep_data=True)
        assert result.ok
        assert (app_home / "apps" / "test-app" / "data" / "cache.json").is_file()
        assert not (app_home / "apps" / "test-app" / APP_MANIFEST_FILENAME).exists()

    def test_uninstall_keeps_data_when_the_app_dir_is_reached_through_a_link(
        self, tmp_path, app_home, monkeypatch
    ):
        """Uninstall pins the data directory, and that walk refuses any link it meets.

        Reached through a linked ancestor - a home that is itself a symlink -
        the pin refuses before the quarantine starts and the uninstall fails
        with the app still installed. The caller has to hand the walk a path
        whose ancestors are already canonical, while leaving the app's own name
        and ``data`` literal so a link at either is still refused.
        """
        import os as _os

        from kiro_crew.apps import manager as _mgr

        if not _mgr.platform_compat.IS_WINDOWS:
            linked_home = tmp_path / "home-link"
            linked_home.symlink_to(app_home)
            monkeypatch.setattr(
                _mgr, "app_dir", lambda name: linked_home / "apps" / name
            )

        src = _make_app_source(tmp_path)
        install_app(src)
        data_dir = _mgr.app_dir("test-app") / "data"
        (data_dir / "cache.json").write_text('{"key": "value"}', encoding="utf-8")

        result = uninstall_app("test-app", keep_data=True)

        assert result.ok, result.error
        assert (data_dir / "cache.json").is_file()
        assert _os.path.lexists(app_home / "apps" / "test-app" / "data" / "cache.json")

    def test_uninstall_purges_generated_deps_from_preserved_data(self, tmp_path, app_home):
        """data/ preservation exists for USER data. The gateway-generated
        dependency trees must not survive an uninstall: a compromised app
        could plant code there (sitecustomize.py) and a reinstall under the
        same name would prepend it to PYTHONPATH - revoked code executing in
        a fresh install."""
        src = _make_app_source(tmp_path)
        install_app(src)
        data_dir = app_home / "apps" / "test-app" / "data"
        (data_dir / "cache.json").write_text('{"key": "value"}')
        for gen in (".kirocrew-deps", ".kirocrew-deps-staging", ".kirocrew-deps-prior"):
            (data_dir / gen).mkdir(parents=True)
            (data_dir / gen / "sitecustomize.py").write_text("planted = True\n")

        result = uninstall_app("test-app", keep_data=True)
        assert result.ok
        preserved = app_home / "apps" / "test-app" / "data"
        assert (preserved / "cache.json").is_file()  # user data kept
        for gen in (".kirocrew-deps", ".kirocrew-deps-staging", ".kirocrew-deps-prior"):
            assert not (preserved / gen).exists(), gen

    def test_uninstall_refuses_a_linked_data_directory(self, tmp_path, app_home):
        """A linked data dir would make the purge (and the whole preserve
        dance) operate on the link's TARGET - an app pointing data at
        another app's tree would have this uninstall move and delete a
        foreign deps tree. The gateway creates data/ as a real directory, so
        a link is never legitimate: refuse, leaving the app installed and
        the target untouched."""
        import os as _os

        if not hasattr(_os, "symlink"):
            pytest.skip("no symlink support")
        src = _make_app_source(tmp_path)
        install_app(src)
        app_root = app_home / "apps" / "test-app"
        victim = tmp_path / "victim-data"
        victim.mkdir()
        (victim / ".kirocrew-deps").mkdir()
        (victim / ".kirocrew-deps" / "keepme.py").write_text("x = 1\n")
        data = app_root / "data"
        import shutil as _shutil

        _shutil.rmtree(data)
        try:
            _os.symlink(victim, data)
        except OSError:
            pytest.skip("symlink not permitted")

        result = uninstall_app("test-app", keep_data=True)
        assert not result.ok
        # the victim's tree is untouched and the app is still installed
        assert (victim / ".kirocrew-deps" / "keepme.py").is_file()
        assert (app_root / APP_MANIFEST_FILENAME).exists()

    def test_suffixed_staging_leftovers_are_purged_at_uninstall(self, tmp_path, app_home):
        """Staging dirs carry unique per-transaction suffixes; an interrupted
        install's leftover must not survive uninstall under a name the exact
        filter never matches."""
        src = _make_app_source(tmp_path)
        install_app(src)
        app_root = app_home / "apps" / "test-app"
        leftover = app_root / "data" / ".kirocrew-deps-staging-1234-deadbeef"
        leftover.mkdir()
        (leftover / "pkg.py").write_text("x = 1\n")
        result = uninstall_app("test-app", keep_data=True)
        assert result.ok, result
        preserved = app_home / "apps" / "test-app" / "data"
        assert not list(preserved.glob(".kirocrew-deps-staging*"))

    def test_failed_purge_restores_preserved_data_to_its_home(
        self, tmp_path, app_home, monkeypatch
    ):
        """A raise after data/ was moved to its temp name must move it BACK:
        the app is still installed, and its user data must not be orphaned
        under a hidden dot-name."""
        import kiro_crew.apps.manager as mgr

        src = _make_app_source(tmp_path)
        install_app(src)
        app_root = app_home / "apps" / "test-app"
        marker = app_root / "data" / "user-file.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("keep me")

        real_rmtree = mgr.shutil.rmtree

        def failing_rmtree(path, *args, **kwargs):
            if str(path) == str(app_root):
                raise OSError("simulated: app dir resists deletion")
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(mgr.shutil, "rmtree", failing_rmtree)
        result = uninstall_app("test-app", keep_data=True)
        assert not result.ok
        assert marker.exists(), "preserved data must be restored to data/"
        assert not (app_home / "apps" / ".test-app-data-tmp").exists()

    def test_app_owned_names_sharing_the_deps_prefix_survive_uninstall(
        self, tmp_path, app_home
    ):
        """The sweep deletes only the gateway's own generated names: an
        app-owned entry that merely shares the .kirocrew-deps prefix (a
        user's backup dir) is preserved data, not a purge target."""
        src = _make_app_source(tmp_path)
        install_app(src)
        app_root = app_home / "apps" / "test-app"
        backup = app_root / "data" / ".kirocrew-deps-backup"
        backup.mkdir(parents=True, exist_ok=True)
        (backup / "precious.txt").write_text("keep me")
        # A staging-prefix-sharing app name must equally survive: the
        # quarantine's strict matcher only claims the generated
        # -<pid>-<8hex> shape.
        assets = app_root / "data" / ".kirocrew-deps-staging-assets"
        assets.mkdir(parents=True, exist_ok=True)
        (assets / "art.bin").write_text("app asset")
        result = uninstall_app("test-app", keep_data=True)
        assert result.ok, result.error
        preserved = app_root / "data" / ".kirocrew-deps-backup" / "precious.txt"
        assert preserved.exists(), "app-owned prefix-sharing data must survive"
        assert (
            app_root / "data" / ".kirocrew-deps-staging-assets" / "art.bin"
        ).exists(), "app-owned staging-prefix data must survive"

    def test_a_file_shaped_deps_artifact_is_purged_and_does_not_poison(
        self, tmp_path, app_home
    ):
        """rmtree refuses non-directories, so a FILE written at a deps-tree
        name survives every uninstall and poisons the next quarantine
        rename. Shape-aware removal purges it - and a second
        install/uninstall round over the same name stays clean."""
        src = _make_app_source(tmp_path)
        install_app(src)
        app_root = app_home / "apps" / "test-app"
        (app_root / "data" / ".kirocrew-deps").write_text("not a directory\n")
        result = uninstall_app("test-app", keep_data=True)
        assert result.ok, result
        preserved = app_home / "apps" / "test-app" / "data"
        assert not (preserved / ".kirocrew-deps").exists()
        # the poison scenario: same name, directory shape, next round
        install_app(src)
        deps = app_home / "apps" / "test-app" / "data" / ".kirocrew-deps"
        deps.mkdir()
        (deps / "pkg.py").write_text("x = 1\n")
        result2 = uninstall_app("test-app", keep_data=True)
        assert result2.ok, result2
        assert not (app_home / "apps" / "test-app" / "data" / ".kirocrew-deps").exists()

    def test_uninstall_purge_unlinks_a_planted_deps_symlink(self, tmp_path, app_home):
        """rmtree refuses a symlink, so a malicious app could plant one at
        the deps name and its target would ride through the purge; the purge
        must unlink the LINK (never following it) so the reinstall starts
        clean while the link's target elsewhere is untouched."""
        import os as _os

        if not hasattr(_os, "symlink"):
            pytest.skip("no symlink support")
        src = _make_app_source(tmp_path)
        install_app(src)
        data_dir = app_home / "apps" / "test-app" / "data"
        target = tmp_path / "elsewhere"
        target.mkdir()
        (target / "sitecustomize.py").write_text("planted = True\n")
        try:
            _os.symlink(target, data_dir / ".kirocrew-deps")
        except OSError:
            pytest.skip("symlink not permitted")

        result = uninstall_app("test-app", keep_data=True)
        assert result.ok
        preserved = app_home / "apps" / "test-app" / "data"
        assert not (preserved / ".kirocrew-deps").exists()
        assert not (preserved / ".kirocrew-deps").is_symlink()
        # the purge removed the LINK, not the linked target's content
        assert (target / "sitecustomize.py").is_file()

    def test_install_preserves_existing_data(self, tmp_path, app_home):
        """Reinstall after default uninstall must preserve user data."""
        src = _make_app_source(tmp_path)
        install_app(src)
        data_dir = app_home / "apps" / "test-app" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "priorities.md").write_text("- item1\n- item2\n")
        (data_dir / "state").mkdir(exist_ok=True)
        (data_dir / "state" / "oncall.json").write_text('{"oncall": true}')

        result = uninstall_app("test-app", keep_data=True)
        assert result.ok
        assert (data_dir / "priorities.md").is_file()

        # Reinstall from same source (source has empty data/)
        src2 = _make_app_source(tmp_path / "src2")
        result = install_app(src2)
        assert result.ok

        assert (data_dir / "priorities.md").read_text(encoding="utf-8") == "- item1\n- item2\n"
        assert (data_dir / "state" / "oncall.json").read_text(
            encoding="utf-8"
        ) == '{"oncall": true}'

    def test_install_rollback_restores_data_on_copy_failure(self, tmp_path, app_home, monkeypatch):
        """If copytree fails after data/ was preserved, rollback must restore data/."""
        src = _make_app_source(tmp_path)
        install_app(src)
        data_dir = app_home / "apps" / "test-app" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "config.yaml").write_text("oncall:\n  rotation: my-rotation\n")
        (data_dir / "state").mkdir(exist_ok=True)
        (data_dir / "state" / "oncall.json").write_text('{"oncall": true}')

        uninstall_app("test-app", keep_data=True)
        assert (data_dir / "config.yaml").is_file()

        # Patch copytree to fail AFTER rmtree succeeds (simulates partial install failure)
        def failing_copytree(*args, **kwargs):
            raise OSError("Simulated disk full error")

        src2 = _make_app_source(tmp_path / "src2")
        monkeypatch.setattr("shutil.copytree", failing_copytree)
        result = install_app(src2)

        assert not result.ok
        assert "failed to copy app files" in result.error

        assert data_dir.is_dir(), "data/ directory must be restored after rollback"
        assert (data_dir / "config.yaml").read_text(
            encoding="utf-8"
        ) == "oncall:\n  rotation: my-rotation\n"
        assert (data_dir / "state" / "oncall.json").read_text(
            encoding="utf-8"
        ) == '{"oncall": true}'

    def test_install_rejects_unsafe_app_name(self, tmp_path, app_home, monkeypatch):
        """Path-traversal name must be rejected with SEL audit event."""
        # Use a valid kebab-case name that passes manifest validation,
        # but monkeypatch _check_path_safety to simulate a traversal detection.
        src = _make_app_source(tmp_path, name="evil-app")
        sel_calls = []
        monkeypatch.setattr(
            "kiro_crew.apps.manager.sel",
            lambda: type(
                "FakeSel", (), {"log_api_access": lambda self, **kw: sel_calls.append(kw)}
            )(),
        )
        monkeypatch.setattr(
            "kiro_crew.apps.manager._check_path_safety",
            lambda name: False,
        )
        result = install_app(src)
        assert not result.ok
        assert "unsafe app name" in result.error
        assert len(sel_calls) == 1
        assert sel_calls[0]["outcome"] == "rejected"
        assert sel_calls[0]["operation"] == "path_safety_check"
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
            lambda: type(
                "FakeSel", (), {"log_api_access": lambda self, **kw: sel_calls.append(kw)}
            )(),
        )
        src = _make_app_source(tmp_path)
        result = install_app(src)
        assert result.ok
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
        # require_signature (denied when no manifest is passed).
        import hashlib
        import hmac

        from kiro_crew.apps.manager import register_external_app
        from kiro_crew.apps.manifest import AppManifest

        secret = "s3cr3t"
        manifest_data = {
            "name": "ext-signed",
            "version": "1.0.0",
            "displayName": "Ext Signed",
            "description": "signed external app",
            "author": "tester",
            "signer": "acme",
        }
        m = AppManifest.from_dict(manifest_data)
        manifest_data["signature"] = hmac.new(
            secret.encode(), m.signing_payload(), hashlib.sha256
        ).hexdigest()
        self._write_policy(
            app_home,
            {
                "mode": "enforce",
                "require_signature": True,
                "approved": ["ext-signed"],
                "trust_keys": {"acme": secret},
            },
        )
        result = register_external_app(
            "ext-signed",
            "1.0.0",
            "Ext Signed",
            manifest_data=manifest_data,
        )
        assert result.ok
        assert _read_installed("ext-signed") is not None

    def test_register_external_denies_unsigned_manifest(self, tmp_path, app_home):
        from kiro_crew.apps.manager import register_external_app

        self._write_policy(
            app_home,
            {
                "mode": "enforce",
                "require_signature": True,
                "approved": ["ext-unsigned"],
                "trust_keys": {"acme": "s3cr3t"},
            },
        )
        result = register_external_app(
            "ext-unsigned",
            "1.0.0",
            "Ext Unsigned",
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
        m = AppManifest.from_dict(
            {
                "name": "signed-app",
                "version": "1.0.0",
                "displayName": "Signed",
                "description": "signed app",
                "author": "tester",
                "signer": "acme",
            }
        )
        sig = hmac.new(secret.encode(), m.signing_payload(), hashlib.sha256).hexdigest()
        self._write_policy(
            app_home,
            {
                "mode": "enforce",
                "require_signature": True,
                "approved": ["signed-app"],
                "trust_keys": {"acme": secret},
            },
        )
        src = _make_app_source(
            tmp_path,
            name="signed-app",
            signer="acme",
            signature=sig,
        )
        result = install_app(src)
        assert result.ok

    def test_signature_required_denies_missing_signature(self, tmp_path, app_home):
        self._write_policy(
            app_home,
            {
                "mode": "enforce",
                "require_signature": True,
                "approved": ["test-app"],
                "trust_keys": {"acme": "s3cr3t"},
            },
        )
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
        self._write_policy(
            app_home,
            {
                "mode": "enforce",
                "require_signature": True,
                "approved": [],
                "trust_keys": {},
            },
        )
        result = enable_app("builtin-app")
        assert result.ok
        enabled_meta = _read_installed("builtin-app")
        assert enabled_meta is not None
        assert enabled_meta.enabled is True

    def test_enable_third_party_still_denied_under_require_signature(self, tmp_path, app_home):
        # A non-builtin (unsigned) app is still denied under require_signature.
        src = _make_app_source(tmp_path)  # origin defaults to non-builtin
        assert install_app(src).ok
        self._write_policy(
            app_home,
            {
                "mode": "enforce",
                "require_signature": True,
                "approved": ["test-app"],
                "trust_keys": {"acme": "s3cr3t"},
            },
        )
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
        m = AppManifest.from_dict(
            {
                "name": "evil-app",
                "version": "1.0.0",
                "displayName": "Evil",
                "description": "d",
                "author": "tester",
                "signer": "acme",
                "signature": "é" * 64,  # non-ASCII, would crash bytes-less compare
            }
        )
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
