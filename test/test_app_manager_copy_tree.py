"""The install and update copy transaction (``_copy_app_tree``): symlink, denylist and
off-loop copying, repository-bound trust grants and provenance, data and secret
preservation with rollback on update, and session-approval reconsent. ``test_app_manager.py`` heads the suite and lists its parts.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest
from app_manager_helpers import app_home_fixture  # noqa: F401 - fixture
from app_manager_helpers import _DATA_IS_A_FILE, _DATA_IS_A_LINK, _make_app_source

from conftest import requires_symlinks
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    _read_installed,
    enable_app,
    get_app,
    get_app_manifest,
    install_app,
    register_external_app,
    registry_source_repository,
    update_app,
)

# ---------------------------------------------------------------------------
# _copy_app_tree — symlink / denylist / off-loop regression tests
# (app install must not run a raw follow-symlinks copytree on the event loop;
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
        # The gateway's own pip --target provisioning output: machine- and
        # platform-specific, re-provisioned at the destination on first spawn.
        # Copying it would put a foreign wheel tree FIRST on the child's
        # PYTHONPATH, shadowing the correctly provisioned copy. The transient
        # staging/prior swap directories are denylisted for the same reason.
        (src / ".kirocrew-deps").mkdir()
        (src / ".kirocrew-deps" / "requests").mkdir()
        (src / ".kirocrew-deps" / "requests" / "__init__.py").write_text("x = 1")
        (src / ".kirocrew-deps-staging").mkdir()
        (src / ".kirocrew-deps-staging" / "partial.py").write_text("x = 1")
        (src / ".kirocrew-deps-prior").mkdir()
        (src / ".kirocrew-deps-prior" / "old.py").write_text("x = 1")
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
        assert not (dest / ".kirocrew-deps").exists()
        assert not (dest / ".kirocrew-deps-staging").exists()
        assert not (dest / ".kirocrew-deps-prior").exists()
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
                # flake-ok: the heartbeat's sampling interval, pre-existing shape, moved verbatim; tracked for a de-flake pass
                await asyncio.sleep(0.01)
                now = time.monotonic()
                gaps.append(now - prev)
                prev = now

        # flake-ok: heartbeat task cancelled in the finally and not awaited, pre-existing shape, moved verbatim; tracked for a de-flake pass
        hb = asyncio.ensure_future(heartbeat())
        try:
            result = await asyncio.to_thread(install_app, src)
        finally:
            hb.cancel()
        assert result.ok, result.error
        # The watchdog threshold is 30s; anything close to that (or even 1s)
        # would indicate the copy ran on the loop.
        # flake-ok: real elapsed-time ceiling on loop latency, pre-existing shape, moved verbatim; tracked for a de-flake pass
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

    def test_local_install_cannot_claim_a_repository_bound_grant(self, tmp_path, app_home):
        from kiro_crew.config.loader import _invalidate_config_cache

        reviewed = "https://clone.example.test/Owner/reviewed-app"
        (app_home / "config.json").write_text(
            json.dumps(
                {
                    "agent": {
                        "apps_allow_third_party": False,
                        "apps_trusted": ["test-app"],
                        "apps_trusted_repositories": {"test-app": reviewed},
                    }
                }
            ),
            encoding="utf-8",
        )
        _invalidate_config_cache()

        result = install_app(_make_app_source(tmp_path))

        assert not result.ok
        assert result.error_code == "app_trust_repository_mismatch"
        assert get_app("test-app") is None

    def test_external_registration_cannot_claim_a_repository_bound_grant(self, app_home):
        from kiro_crew.config.loader import _invalidate_config_cache

        reviewed = "https://clone.example.test/Owner/reviewed-app"
        (app_home / "config.json").write_text(
            json.dumps(
                {
                    "agent": {
                        "apps_allow_third_party": False,
                        "apps_trusted": ["test-app"],
                        "apps_trusted_repositories": {"test-app": reviewed},
                    }
                }
            ),
            encoding="utf-8",
        )
        _invalidate_config_cache()

        result = register_external_app("test-app", "1.0.0", "Rebound App")

        assert not result.ok
        assert result.error_code == "app_trust_repository_mismatch"
        assert get_app("test-app") is None

    def test_legacy_name_grant_cannot_install_repository_code(self, tmp_path, app_home):
        from kiro_crew.config.loader import _invalidate_config_cache

        (app_home / "config.json").write_text(
            json.dumps({"agent": {"apps_trusted": ["test-app"]}}),
            encoding="utf-8",
        )
        _invalidate_config_cache()

        result = install_app(
            _make_app_source(tmp_path),
            source_repository="https://User:Secret@example.test/owner/repo",
        )

        assert not result.ok
        assert result.error_code == "app_trust_repository_mismatch"
        assert "Secret" not in result.error
        assert get_app("test-app") is None

    def test_legacy_name_grant_cannot_claim_fresh_local_install(self, tmp_path, app_home):
        from kiro_crew.config.loader import _invalidate_config_cache

        (app_home / "config.json").write_text(
            json.dumps({"agent": {"apps_trusted": ["test-app"]}}),
            encoding="utf-8",
        )
        _invalidate_config_cache()

        result = install_app(_make_app_source(tmp_path))

        assert not result.ok
        assert result.error_code == "app_trust_repository_mismatch"
        assert get_app("test-app") is None

    def test_registry_context_preserves_callable_contract_and_bound_provenance(
        self, tmp_path, app_home
    ):
        """The registry's one-argument manager call still gets its safe source."""
        from kiro_crew.config.loader import _invalidate_config_cache

        reviewed = "ssh://deploy@example.test/owner/repo"
        (app_home / "config.json").write_text(
            json.dumps(
                {
                    "agent": {
                        "apps_trusted": ["test-app"],
                        "apps_trusted_repositories": {"test-app": reviewed},
                    }
                }
            ),
            encoding="utf-8",
        )
        _invalidate_config_cache()

        with registry_source_repository(reviewed):
            result = install_app(_make_app_source(tmp_path))

        # Success proves the repository-bound grant saw the scoped coordinate;
        # without it the one-argument call is classified as a local takeover and
        # denied. The same coordinate is provisional durable provenance before
        # later registry bookkeeping enriches the record.
        assert result.ok
        assert get_app("test-app").get("sourceUrl", "") == reviewed

    def test_install_bookkeeping_failure_keeps_provisional_repository_provenance(
        self, tmp_path, app_home, monkeypatch
    ):
        """A failure after installed.json cannot turn repository code local."""
        from kiro_crew.apps.execution import app_execution_denied
        from kiro_crew.config.loader import _invalidate_config_cache

        reviewed = "https://example.test/owner/reviewed"
        (app_home / "config.json").write_text(
            json.dumps(
                {
                    "agent": {
                        "apps_trusted": ["test-app"],
                        "apps_trusted_repositories": {"test-app": reviewed},
                    }
                }
            ),
            encoding="utf-8",
        )
        _invalidate_config_cache()

        def _bookkeeping_failure(*_args, **_kwargs):
            raise RuntimeError("secret bookkeeping failed")

        monkeypatch.setattr(
            "kiro_crew.dashboard.token_auth.write_app_secret",
            _bookkeeping_failure,
        )

        with pytest.raises(RuntimeError, match="bookkeeping failed"):
            install_app(
                _make_app_source(tmp_path),
                source_repository=reviewed,
            )

        installed = get_app("test-app")
        assert installed is not None
        assert installed["sourceUrl"] == reviewed
        assert app_execution_denied("test-app", action="module_load") is None

    def test_provenance_enrichment_failure_keeps_provisional_repository(
        self, tmp_path, app_home, monkeypatch
    ):
        from kiro_crew.apps import manager

        reviewed = "https://example.test/owner/reviewed"
        with registry_source_repository(reviewed):
            result = install_app(_make_app_source(tmp_path))
        assert result.ok, result.error

        def _provenance_write_failure(*_args, **_kwargs):
            raise OSError("provenance write failed")

        monkeypatch.setattr(manager, "_write_installed", _provenance_write_failure)
        with pytest.raises(OSError, match="provenance write failed"):
            manager.set_app_provenance(
                "test-app",
                source="registry:test-app",
                url=reviewed,
                registry="core",
                commit="a" * 40,
                signer="release-key",
            )

        persisted = manager._read_installed("test-app")
        assert persisted is not None
        assert persisted.sourceUrl == reviewed

    def test_registry_context_rechecks_binding_at_replacement_boundary(self, tmp_path, app_home):
        """A source changed after registry preflight cannot reach the copy step."""
        from kiro_crew.config.loader import _invalidate_config_cache

        reviewed = "ssh://deploy@example.test/owner/reviewed"
        rebound = "ssh://deploy@example.test/owner/rebound"
        (app_home / "config.json").write_text(
            json.dumps(
                {
                    "agent": {
                        "apps_trusted": ["test-app"],
                        "apps_trusted_repositories": {"test-app": reviewed},
                    }
                }
            ),
            encoding="utf-8",
        )
        _invalidate_config_cache()

        with registry_source_repository(rebound):
            result = install_app(_make_app_source(tmp_path))

        assert not result.ok
        assert result.error_code == "app_trust_repository_mismatch"
        assert reviewed not in result.error
        assert rebound not in result.error
        assert get_app("test-app") is None

    @pytest.mark.asyncio
    async def test_registry_context_is_task_local_across_to_thread(self):
        """Concurrent installs cannot exchange their repository coordinates."""
        from kiro_crew.apps.manager import _effective_source_repository

        async def _resolve(repository: str) -> str:
            with registry_source_repository(repository):
                # Interleave both task contexts before copying them to workers.
                # flake-ok: one yield that interleaves the two task contexts, pre-existing shape, moved verbatim; tracked for a de-flake pass
                await asyncio.sleep(0)
                return await asyncio.to_thread(_effective_source_repository, "")

        first, second = await asyncio.gather(
            _resolve("https://example.test/owner/first"),
            _resolve("https://example.test/owner/second"),
        )

        assert first == "https://example.test/owner/first"
        assert second == "https://example.test/owner/second"

    def test_legacy_name_grant_cannot_update_to_repository_code(self, tmp_path, app_home):
        from kiro_crew.apps.manager import update_app
        from kiro_crew.config.loader import _invalidate_config_cache

        assert install_app(_make_app_source(tmp_path)).ok
        (app_home / "config.json").write_text(
            json.dumps({"agent": {"apps_trusted": ["test-app"]}}),
            encoding="utf-8",
        )
        _invalidate_config_cache()

        result = update_app(
            _make_app_source(tmp_path / "v2", version="2.0.0"),
            source_repository="https://example.test/owner/repo",
        )

        assert not result.ok
        assert result.error_code == "app_trust_repository_mismatch"
        assert get_app("test-app")["version"] == "1.0.0"

    def test_legacy_name_grant_cannot_register_repository_code(self, app_home):
        from kiro_crew.config.loader import _invalidate_config_cache

        (app_home / "config.json").write_text(
            json.dumps({"agent": {"apps_trusted": ["test-app"]}}),
            encoding="utf-8",
        )
        _invalidate_config_cache()

        result = register_external_app(
            "test-app",
            "1.0.0",
            "Legacy Rebind",
            source_repository="https://example.test/owner/repo",
        )

        assert not result.ok
        assert result.error_code == "app_trust_repository_mismatch"
        assert get_app("test-app") is None

    def test_installed_legacy_local_grant_can_update_local_code(self, tmp_path, app_home):
        from kiro_crew.apps.manager import update_app
        from kiro_crew.config.loader import _invalidate_config_cache

        assert install_app(_make_app_source(tmp_path)).ok
        (app_home / "config.json").write_text(
            json.dumps({"agent": {"apps_trusted": ["test-app"]}}),
            encoding="utf-8",
        )
        _invalidate_config_cache()

        result = update_app(_make_app_source(tmp_path / "v2", version="2.0.0"))

        assert result.ok, result.error
        assert get_app("test-app")["version"] == "2.0.0"

    def test_update_preserves_data_and_secret(self, tmp_path, app_home):
        from kiro_crew.apps.manager import app_dir, update_app

        src = _make_app_source(tmp_path)
        assert install_app(src).ok
        dest = app_dir("test-app")
        (dest / "data").mkdir(exist_ok=True)
        (dest / "data" / "state.json").write_text('{"k": 1}')
        secret = dest / ".app_secret"
        secret.write_text("s3cret")

        v2 = _make_app_source(tmp_path / "v2", version="2.0.0")
        result = update_app(v2)
        assert result.ok, result.error
        assert (dest / "data" / "state.json").read_text(encoding="utf-8") == '{"k": 1}'
        assert secret.read_text(encoding="utf-8") == "s3cret"

    @requires_symlinks
    def test_an_installed_data_link_refuses_the_update_before_the_transaction(
        self, tmp_path, app_home
    ):
        """An installed `data` that is a LINK (an install from before the link
        refusal) cannot be preserved: the move would relocate the link beside the
        app directory, where its relative target does not resolve, put nothing
        back, and delete the directory it named with the retired tree on the
        update's success path. The update refuses before anything moves: the link,
        the directory it names, the rest of the tree and the record are untouched,
        no temp copy and no retired tree appear, and the sentence is the one
        predicate every entry point asks (`gateway_data_dir_obstruction`)."""
        from kiro_crew.apps.manager import app_dir, update_app

        src = _make_app_source(tmp_path)
        assert install_app(src).ok
        dest = app_dir("test-app")
        (dest / "data").rmdir()
        (dest / "state").mkdir()
        (dest / "state" / "state.json").write_text('{"k": 1}', encoding="utf-8")
        (dest / "data").symlink_to(Path("state"), target_is_directory=True)
        before = _read_installed("test-app")

        v2 = _make_app_source(tmp_path / "v2", version="2.0.0")
        result = update_app(v2)

        assert not result.ok
        assert result.error == _DATA_IS_A_LINK, result.error
        assert os.path.islink(dest / "data")
        assert (dest / "state" / "state.json").read_text(encoding="utf-8") == '{"k": 1}'
        assert (dest / "data" / "state.json").read_text(
            encoding="utf-8"
        ) == '{"k": 1}'  # still reachable
        assert _read_installed("test-app") == before  # version 1.0.0, untouched
        assert not (dest.parent / ".test-app-data-tmp").exists()
        assert [
            p.name for p in dest.parent.iterdir() if p.name.startswith(".test-app-update-old-")
        ] == []
        # The source of the refused update is not the app's business: untouched too.
        assert (v2 / APP_MANIFEST_FILENAME).is_file()

    def test_an_installed_data_junction_refuses_the_update_before_the_transaction(
        self, tmp_path, app_home, monkeypatch
    ):
        """The Windows shape of the same loss: a directory JUNCTION at `<app>/data`
        is not a symlink -- `os.path.islink` and `Path.is_symlink` both say False --
        so a link test built on either calls it a directory, moves it aside as one,
        retires the tree it names with the old app files and cannot put the
        dangling junction back: the stored data is deleted on the update's own
        success path. Fed as a SHAPE through the module's junction seam, the way the
        dangling-junction tests do (a junction has no POSIX equivalent to create):
        `data` is a real directory here that `is_link_or_junction` reports as a
        junction, and the predicate, the preservation forecast and the update
        preflight must all answer as they do for a symlink -- refuse before anything
        moves, with the data, the record and the tree untouched."""
        from kiro_crew.apps.manager import (
            _owned_data_dir,
            app_dir,
            preserved_data_awaits,
            update_app,
        )

        assert install_app(_make_app_source(tmp_path)).ok
        dest = app_dir("test-app")
        (dest / "data" / "state.json").write_text('{"k": 1}', encoding="utf-8")
        before = _read_installed("test-app")
        junction = dest / "data"
        monkeypatch.setattr(
            "kiro_crew.apps.manager.is_link_or_junction", lambda path: Path(path) == junction
        )
        assert _owned_data_dir(junction) is False  # not a directory the gateway may move
        assert (
            preserved_data_awaits("test-app") is False
        )  # so the preview preserves nothing over it

        result = update_app(_make_app_source(tmp_path / "v2", version="2.0.0"))

        assert not result.ok
        assert result.error == _DATA_IS_A_LINK, result.error
        assert (dest / "data" / "state.json").read_text(encoding="utf-8") == '{"k": 1}'
        assert _read_installed("test-app") == before  # version 1.0.0, untouched
        assert not (dest.parent / ".test-app-data-tmp").exists()
        assert [
            p.name for p in dest.parent.iterdir() if p.name.startswith(".test-app-update-old-")
        ] == []

    @requires_symlinks
    def test_an_installed_data_link_to_an_outside_directory_refuses_the_update_before_the_transaction(
        self, tmp_path, app_home
    ):
        """The OUTSIDE shape of the link: `data` names a directory elsewhere (state
        relocated to another volume). The predicate refuses a link whatever it
        resolves to, so the update refuses before anything moves: the link still
        stands and still resolves, the sentinel behind it is intact, the tree and
        the record are untouched, no temp copy and no retired tree appear."""
        from kiro_crew.apps.manager import app_dir, update_app

        assert install_app(_make_app_source(tmp_path)).ok
        dest = app_dir("test-app")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "sentinel.json").write_text('{"kept": true}', encoding="utf-8")
        (dest / "data").rmdir()
        (dest / "data").symlink_to(elsewhere, target_is_directory=True)
        before = _read_installed("test-app")

        result = update_app(_make_app_source(tmp_path / "v2", version="2.0.0"))

        assert not result.ok
        assert result.error == _DATA_IS_A_LINK, result.error
        assert os.path.islink(dest / "data") and (dest / "data").resolve() == elsewhere.resolve()
        assert (elsewhere / "sentinel.json").read_text(encoding="utf-8") == '{"kept": true}'
        assert _read_installed("test-app") == before  # version 1.0.0, untouched
        assert not (dest.parent / ".test-app-data-tmp").exists()
        assert [
            p.name for p in dest.parent.iterdir() if p.name.startswith(".test-app-update-old-")
        ] == []

    def test_an_installed_data_file_refuses_the_update_before_the_transaction(
        self, tmp_path, app_home
    ):
        """The FILE shape of the same loss: an installed `data` that is a regular
        file (an install from before the refusal, or the app's own runtime
        replacing its directory) is not a directory the gateway can move aside. A
        link-only preflight lets the update through: the move skips the file, the
        old tree is retired with the file in it, the post-copy question is asked of
        the NEW tree only, the retired tree is deleted on the success path and an
        empty `data/` is created in the file's place -- the content gone, with
        `ok=True`. The preflight asks the one predicate every entry point asks
        (`gateway_data_dir_obstruction`), so the update refuses before anything
        moves: the file and its content, the rest of the tree and the record are
        untouched, no temp copy and no retired tree appear, and no directory
        replaces the file."""
        from kiro_crew.apps.manager import app_dir, update_app

        assert install_app(_make_app_source(tmp_path)).ok
        dest = app_dir("test-app")
        (dest / "data").rmdir()
        (dest / "data").write_text("the app's own bytes", encoding="utf-8")
        before = _read_installed("test-app")
        tree_before = sorted(str(p.relative_to(dest)) for p in dest.rglob("*"))
        manifest_before = (dest / APP_MANIFEST_FILENAME).read_text(encoding="utf-8")

        v2 = _make_app_source(tmp_path / "v2", version="2.0.0")
        result = update_app(v2)

        assert not result.ok
        assert result.error == _DATA_IS_A_FILE, result.error
        assert (dest / "data").is_file()
        assert (dest / "data").read_text(encoding="utf-8") == "the app's own bytes"
        assert sorted(str(p.relative_to(dest)) for p in dest.rglob("*")) == tree_before
        assert (dest / APP_MANIFEST_FILENAME).read_text(encoding="utf-8") == manifest_before
        assert _read_installed("test-app") == before  # version 1.0.0, untouched
        assert not (dest.parent / ".test-app-data-tmp").exists()
        assert [
            p.name for p in dest.parent.iterdir() if p.name.startswith(".test-app-update-old-")
        ] == []
        # The source of the refused update is not the app's business: untouched too.
        assert (v2 / APP_MANIFEST_FILENAME).is_file()

    @requires_symlinks
    def test_a_link_planted_at_the_temp_name_refuses_the_update_before_the_transaction(
        self, tmp_path, app_home
    ):
        """Same shape on the update: `.<name>-data-tmp` holding a planted link is
        refused before the transaction opens, so the app's real `data/` is never
        moved onto it (which would deposit it inside the link's target and later
        rename the link back over `data`). Data, record, tree and the link's target
        are untouched; nothing is retired."""
        from kiro_crew.apps.manager import app_dir, update_app

        assert install_app(_make_app_source(tmp_path)).ok
        dest = app_dir("test-app")
        (dest / "data" / "state.json").write_text('{"k": 1}', encoding="utf-8")
        before = _read_installed("test-app")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        planted = dest.parent / ".test-app-data-tmp"
        planted.symlink_to(elsewhere, target_is_directory=True)

        result = update_app(_make_app_source(tmp_path / "v2", version="2.0.0"))

        assert not result.ok
        assert result.error.startswith(f"{planted} is a link; "), result.error
        assert (dest / "data" / "state.json").read_text(encoding="utf-8") == '{"k": 1}'
        assert list(elsewhere.iterdir()) == []
        assert os.path.islink(planted)
        assert _read_installed("test-app") == before
        assert [
            p.name for p in dest.parent.iterdir() if p.name.startswith(".test-app-update-old-")
        ] == []

    @requires_symlinks
    @pytest.mark.parametrize("secret_installed", [True, False])
    def test_update_never_keeps_a_shipped_app_secret_link(
        self, tmp_path, app_home, secret_installed
    ):
        """The copied `.app_secret` is removed, unfollowed, on EVERY update: with a
        preserved secret it is replaced by that regular file; with none (an install
        that predates per-app secrets) nothing the source shipped stands there
        either, so no later write can be steered through an app-chosen link."""
        from kiro_crew.apps.manager import app_dir, update_app

        assert install_app(_make_app_source(tmp_path)).ok
        dest = app_dir("test-app")
        secret = dest / ".app_secret"
        if secret_installed:
            secret.write_text("s3cret", encoding="utf-8")
        else:
            secret.unlink()

        v2 = _make_app_source(tmp_path / "v2", version="2.0.0")
        (v2 / "ui").mkdir()
        (v2 / "ui" / "leak.js").write_text("export const leak = 1;\n", encoding="utf-8")
        (v2 / ".app_secret").symlink_to(Path("ui") / "leak.js")

        result = update_app(v2)

        assert result.ok, result.error
        assert not os.path.islink(secret)
        if secret_installed:
            assert secret.read_text(encoding="utf-8") == "s3cret"
        else:
            assert not os.path.lexists(secret)
        assert (dest / "ui" / "leak.js").read_text(encoding="utf-8") == "export const leak = 1;\n"

    def test_an_update_shipping_a_root_data_file_where_none_is_preserved_rolls_back(
        self, tmp_path, app_home
    ):
        """With the installed `data/` gone (nothing to put back), a source's root
        `data` FILE reached the app directory and `app_data_dir()` raised AFTER the
        new record and tree were durable and the old tree discarded. Refused inside
        the transaction instead: the old tree and record come back whole."""
        from kiro_crew.apps.manager import app_dir, update_app

        src = _make_app_source(tmp_path)
        (src / "marker-v1.txt").write_text("", encoding="utf-8")
        assert install_app(src).ok
        dest = app_dir("test-app")
        shutil.rmtree(dest / "data")

        v2 = _make_app_source(tmp_path / "v2", version="2.0.0")
        (v2 / "data").write_text("a file\n", encoding="utf-8")
        result = update_app(v2)

        assert not result.ok
        assert result.error == f"failed to update app files: {_DATA_IS_A_FILE}", result.error
        meta = _read_installed("test-app")
        assert meta is not None and meta.version == "1.0.0"
        assert (dest / "marker-v1.txt").is_file()  # the old tree, restored
        assert not os.path.lexists(dest / "data")  # the shipped file did not stay
        assert [p.name for p in dest.parent.iterdir()] == ["test-app"]  # no retired tree left

    @pytest.mark.parametrize("rollback_metadata_fails", [False, True])
    def test_metadata_failure_restores_data_secret_and_retired_tree(
        self, tmp_path, app_home, monkeypatch, rollback_metadata_fails
    ):
        from kiro_crew.apps import manager as manager_mod
        from kiro_crew.apps.manager import app_dir, update_app

        assert install_app(_make_app_source(tmp_path)).ok
        dest = app_dir("test-app")
        data = dest / "data"
        data.mkdir(exist_ok=True)
        (data / "state.json").write_text('{"kept": true}', encoding="utf-8")
        secret = dest / ".app_secret"
        secret.write_text("kept-secret", encoding="utf-8")
        (dest / "old-only.txt").write_text("old tree", encoding="utf-8")

        v2 = _make_app_source(tmp_path / "v2", version="2.0.0")
        (v2 / "data").write_text("replacement file", encoding="utf-8")
        (v2 / ".app_secret").mkdir()
        (v2 / ".app_secret" / "replacement.txt").write_text(
            "replacement directory", encoding="utf-8"
        )
        (v2 / "new-only.txt").write_text("new tree", encoding="utf-8")

        real_write = manager_mod._write_installed
        writes = 0

        def _fail_metadata_write(name, meta):
            nonlocal writes
            writes += 1
            if writes == 1 or rollback_metadata_fails:
                raise OSError("metadata write failed")
            real_write(name, meta)

        monkeypatch.setattr(manager_mod, "_write_installed", _fail_metadata_write)
        result = update_app(v2)

        assert not result.ok
        assert data.is_dir()
        assert (data / "state.json").read_text(encoding="utf-8") == '{"kept": true}'
        assert secret.is_file()
        assert secret.read_text(encoding="utf-8") == "kept-secret"
        assert (dest / "old-only.txt").read_text(encoding="utf-8") == "old tree"
        assert not (dest / "new-only.txt").exists()
        assert get_app_manifest("test-app").version == "1.0.0"
        restored_meta = _read_installed("test-app")
        assert restored_meta is not None
        assert restored_meta.version == "1.0.0"

    def test_update_that_adds_session_approval_disables_until_reconsent(
        self, tmp_path, app_home, monkeypatch
    ):
        # Consent is captured at install/enable while the route guard reads the
        # live manifest, so a version that ADDS the grant must not inherit the
        # user's earlier "enabled" -- otherwise an update silently widens what
        # the app may do to their sessions.
        from kiro_crew.apps import manager as manager_mod
        from kiro_crew.apps.manager import update_app
        from kiro_crew.apps.permissions import app_can_manage_session_approvals

        assert install_app(_make_app_source(tmp_path)).ok
        assert enable_app("test-app").ok
        assert get_app("test-app")["enabled"] is True

        v2 = _make_app_source(
            tmp_path / "v2",
            version="2.0.0",
            permissions={"sessionApproval": True},
        )
        real_copy = manager_mod._copy_app_tree
        observed_grants = []

        def _copy_with_permission_probe(source, dest):
            real_copy(source, dest)
            observed_grants.append(app_can_manage_session_approvals("test-app"))

        monkeypatch.setattr(manager_mod, "_copy_app_tree", _copy_with_permission_probe)
        result = update_app(v2)
        assert result.ok, result.error
        assert observed_grants == [False]
        assert "session approval" in result.message
        # The UI branches on the structured notice, not on the prose.
        assert result.notice == "session_approval_reconsent"
        assert result.to_dict()["notice"] == "session_approval_reconsent"
        assert "code" not in result.to_dict()
        assert get_app("test-app")["enabled"] is False
        assert get_app("test-app")["version"] == "2.0.0"
        assert get_app("test-app")["sessionApprovalConsentPending"] is True

    def test_failed_widening_update_restores_original_tree_and_metadata(
        self, tmp_path, app_home, monkeypatch
    ):
        assert install_app(_make_app_source(tmp_path)).ok
        assert enable_app("test-app").ok
        original = _read_installed("test-app")
        assert original is not None

        v2 = _make_app_source(
            tmp_path / "v2",
            version="2.0.0",
            permissions={"sessionApproval": True},
        )
        real_copytree = shutil.copytree

        def _copy_then_fail(*args, **kwargs):
            real_copytree(*args, **kwargs)
            raise OSError("simulated copy failure")

        monkeypatch.setattr(shutil, "copytree", _copy_then_fail)
        result = update_app(v2)

        assert not result.ok
        assert "failed to update app files" in (result.error or "")
        assert _read_installed("test-app") == original
        assert get_app_manifest("test-app").version == "1.0.0"
        assert get_app("test-app")["enabled"] is True
        assert get_app("test-app")["sessionApprovalConsentPending"] is False

    def test_fresh_install_with_session_approval_requires_consent(self, tmp_path, app_home):
        result = install_app(_make_app_source(tmp_path, permissions={"sessionApproval": True}))

        assert result.ok, result.error
        assert result.notice == "session_approval_reconsent"
        assert get_app("test-app")["enabled"] is False
        assert get_app("test-app")["sessionApprovalConsentPending"] is True
        blocked = enable_app("test-app")
        assert not blocked.ok
        assert blocked.error_code == "session_approval_consent_required"
        assert enable_app("test-app", session_approval_consent=True).ok
        assert get_app("test-app")["sessionApprovalConsentPending"] is False

    def test_update_keeping_session_approval_stays_enabled(self, tmp_path, app_home):
        # The grant was already declared when the user enabled the app, so a
        # refresh that keeps it is not a new request.
        from kiro_crew.apps.manager import update_app

        assert install_app(_make_app_source(tmp_path, permissions={"sessionApproval": True})).ok
        assert enable_app("test-app", session_approval_consent=True).ok
        v2 = _make_app_source(
            tmp_path / "v2",
            version="2.0.0",
            permissions={"sessionApproval": True},
        )
        result = update_app(v2)
        assert result.ok, result.error
        assert result.notice == ""
        assert get_app("test-app")["enabled"] is True

    def test_self_registration_that_adds_session_approval_is_disabled(self, app_home):
        # Self-managed apps re-register on every launch and author their own
        # manifest, so a manifest that newly asks for session control must not
        # inherit the always-enabled default -- that would be a self-grant.
        assert register_external_app("ext-keypad", "1.0.0", "Keypad").ok
        assert get_app("ext-keypad")["enabled"] is True

        result = register_external_app(
            "ext-keypad",
            "1.1.0",
            "Keypad",
            manifest_data={
                "name": "ext-keypad",
                "version": "1.1.0",
                "permissions": {"sessionApproval": True},
            },
        )
        assert result.ok, result.error
        assert result.notice == "session_approval_reconsent"
        assert get_app("ext-keypad")["enabled"] is False

    def test_first_self_registration_with_session_approval_starts_disabled(self, app_home):
        result = register_external_app(
            "ext-keypad",
            "1.0.0",
            "Keypad",
            manifest_data={
                "name": "ext-keypad",
                "version": "1.0.0",
                "permissions": {"sessionApproval": True},
            },
        )
        assert result.ok, result.error
        assert result.notice == "session_approval_reconsent"
        assert get_app("ext-keypad")["enabled"] is False
        assert get_app("ext-keypad")["sessionApprovalConsentPending"] is True
        # Only a disclosure surface may clear pending consent.
        blocked = enable_app("ext-keypad")
        assert not blocked.ok
        assert blocked.error_code == "session_approval_consent_required"
        assert get_app("ext-keypad")["sessionApprovalConsentPending"] is True
        assert enable_app("ext-keypad", session_approval_consent=True).ok
        assert get_app("ext-keypad")["enabled"] is True
        assert get_app("ext-keypad")["sessionApprovalConsentPending"] is False

    def test_self_registration_keeping_session_approval_stays_enabled(self, app_home):
        manifest = {
            "name": "ext-keypad",
            "version": "1.0.0",
            "permissions": {"sessionApproval": True},
        }
        assert register_external_app("ext-keypad", "1.0.0", "Keypad", manifest_data=manifest).ok
        assert enable_app("ext-keypad", session_approval_consent=True).ok
        result = register_external_app(
            "ext-keypad", "1.0.1", "Keypad", manifest_data={**manifest, "version": "1.0.1"}
        )
        assert result.ok, result.error
        assert result.notice == ""
        assert get_app("ext-keypad")["enabled"] is True

    def test_self_registration_removing_session_approval_clears_pending(self, app_home):
        manifest = {
            "name": "ext-keypad",
            "version": "1.0.0",
            "permissions": {"sessionApproval": True},
        }
        assert register_external_app("ext-keypad", "1.0.0", "Keypad", manifest_data=manifest).ok
        assert get_app("ext-keypad")["sessionApprovalConsentPending"] is True

        assert register_external_app("ext-keypad", "1.0.0", "Keypad").ok
        assert get_app("ext-keypad")["sessionApprovalConsentPending"] is True

        result = register_external_app(
            "ext-keypad",
            "1.0.1",
            "Keypad",
            manifest_data={"name": "ext-keypad", "version": "1.0.1"},
        )

        assert result.ok, result.error
        assert get_app("ext-keypad")["enabled"] is False
        assert get_app("ext-keypad")["sessionApprovalConsentPending"] is False

    def test_failed_self_registration_widening_restores_metadata(self, app_home, monkeypatch):
        from kiro_crew.apps import manager as manager_mod

        assert register_external_app("ext-keypad", "1.0.0", "Keypad").ok
        original = _read_installed("ext-keypad")
        assert original is not None
        real_atomic_write = manager_mod.atomic_write
        manifest_writes = 0

        def _fail_manifest_once(path, data):
            nonlocal manifest_writes
            if Path(path).name == APP_MANIFEST_FILENAME:
                manifest_writes += 1
                if manifest_writes == 1:
                    raise OSError("manifest write failed")
            real_atomic_write(path, data)

        monkeypatch.setattr(manager_mod, "atomic_write", _fail_manifest_once)
        result = register_external_app(
            "ext-keypad",
            "1.1.0",
            "Keypad",
            manifest_data={
                "name": "ext-keypad",
                "version": "1.1.0",
                "permissions": {"sessionApproval": True},
            },
        )

        assert not result.ok
        assert _read_installed("ext-keypad") == original
        assert get_app_manifest("ext-keypad") is None

    def test_failed_self_registration_removal_preserves_pending_consent(
        self, app_home, monkeypatch
    ):
        from kiro_crew.apps import manager as manager_mod

        manifest = {
            "name": "ext-keypad",
            "version": "1.0.0",
            "permissions": {"sessionApproval": True},
        }
        assert register_external_app("ext-keypad", "1.0.0", "Keypad", manifest_data=manifest).ok
        original = _read_installed("ext-keypad")
        assert original is not None
        real_write = manager_mod._write_installed
        metadata_writes = 0

        def _fail_metadata_once(name, meta):
            nonlocal metadata_writes
            metadata_writes += 1
            if metadata_writes == 1:
                raise OSError("metadata write failed")
            real_write(name, meta)

        monkeypatch.setattr(manager_mod, "_write_installed", _fail_metadata_once)
        result = register_external_app(
            "ext-keypad",
            "1.1.0",
            "Keypad",
            manifest_data={"name": "ext-keypad", "version": "1.1.0"},
        )

        assert not result.ok
        assert _read_installed("ext-keypad") == original
        restored = get_app_manifest("ext-keypad")
        assert restored is not None
        assert restored.permissions.sessionApproval is True

    def test_update_of_disabled_app_adding_session_approval_requires_consent(
        self, tmp_path, app_home
    ):
        # A disabled app can be enabled later, so a new grant still needs consent.
        from kiro_crew.apps.manager import update_app

        assert install_app(_make_app_source(tmp_path)).ok
        assert get_app("test-app")["enabled"] is False
        v2 = _make_app_source(
            tmp_path / "v2",
            version="2.0.0",
            permissions={"sessionApproval": True},
        )
        result = update_app(v2)
        assert result.ok, result.error
        assert get_app("test-app")["enabled"] is False
        assert result.notice == "session_approval_reconsent"
        assert get_app("test-app")["sessionApprovalConsentPending"] is True

    def test_update_removing_session_approval_clears_pending(self, tmp_path, app_home):
        assert install_app(_make_app_source(tmp_path)).ok
        widened = _make_app_source(
            tmp_path / "v2",
            version="2.0.0",
            permissions={"sessionApproval": True},
        )
        assert update_app(widened).ok
        assert get_app("test-app")["sessionApprovalConsentPending"] is True

        narrowed = _make_app_source(tmp_path / "v3", version="3.0.0")
        result = update_app(narrowed)

        assert result.ok, result.error
        assert get_app("test-app")["enabled"] is False
        assert get_app("test-app")["sessionApprovalConsentPending"] is False

    def test_local_update_clears_prior_registry_provenance(self, tmp_path, app_home):
        from kiro_crew.apps.manager import (
            _read_installed,
            set_app_provenance,
            update_app,
        )

        src = _make_app_source(tmp_path)
        assert install_app(src).ok
        assert set_app_provenance(
            "test-app",
            source="registry:test-app",
            url="https://clone.example.test/Owner/reviewed-app",
            registry="corp",
            commit="a" * 40,
            signer="release-key",
        )

        local_v2 = _make_app_source(tmp_path / "local-v2", version="2.0.0")
        result = update_app(local_v2)
        assert result.ok, result.error

        meta = _read_installed("test-app")
        assert meta is not None
        assert meta.source == str(local_v2.resolve())
        assert meta.sourceUrl == ""
        assert meta.sourceRegistry == ""
        assert meta.sourceCommit == ""
        assert meta.sourceSigner == ""

    def test_directory_junction_omitted(self, tmp_path, app_home, monkeypatch):
        """Windows directory junctions (reparse points not reported by
        islink) are omitted from the copy. Simulated by monkeypatching
        os.path.isjunction since junctions don't exist on POSIX."""
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
