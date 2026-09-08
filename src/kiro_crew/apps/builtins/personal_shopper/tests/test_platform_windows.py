"""Windows support for the Personal Shopper app.

Two independent things are pinned here.

The manifest half: ``platform.os`` is the only gate between this app and a
native-Windows dashboard, because ``apps/routes.py`` enables an app by calling
``supports_platform(sys.platform)``. An ABSENT ``platform`` block does not mean
"runs anywhere" -- ``PlatformConfig`` defaults to ``["macos", "linux"]``, so
omitting it silently refuses to enable the app on Windows. The declaration is
honest rather than optimistic: this app spawns no subprocess and shells out
nowhere, persists through stdlib sqlite3 with no file locks, and builds every
path with ``pathlib`` from ``app_data_dir``, so there is no POSIX-only surface
to carve out and nothing here degrades on Windows.

The shutdown half: the module-level store singleton holds a WAL-mode sqlite
connection, and closing it is what makes the app resettable. On Windows an open
handle blocks deleting the files it points at, so an unclosed connection leaves
the app's data directory unremovable -- and the app therefore unresettable and
uninstallable -- once it has served a single request.
"""

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import web

from kiro_crew.apps.builtins.personal_shopper.backend import routes as routes_mod
from kiro_crew.apps.builtins.personal_shopper.backend.store import PreferenceStore
from kiro_crew.apps.manifest import PlatformConfig

_DECLARED_OS = ["macos", "linux", "windows"]


def _manifest() -> dict:
    """Read the app manifest the same way the app loader does."""
    path = Path(routes_mod.__file__).resolve().parents[1] / "app.json"
    return json.loads(path.read_text(encoding="utf-8"))


class TestManifestPlatformDeclaration(unittest.TestCase):
    def test_manifest_declares_every_platform_the_app_runs_on(self) -> None:
        """Pinned because both narrower answers misinform.

        Naming fewer platforms reads as "does not run there", and omitting the
        block altogether falls back to the implicit ``["macos", "linux"]``
        default, which silently drops Windows.
        """
        self.assertEqual(_manifest()["platform"]["os"], _DECLARED_OS)

    def test_declared_platforms_all_resolve_to_a_real_sys_platform(self) -> None:
        """Every declared name must map to a ``sys.platform`` value.

        An unmapped name is accepted into the list and then never matches, so a
        declaration can claim a platform the gate still rejects.
        """
        cfg = PlatformConfig(os=_manifest()["platform"]["os"])
        for sys_platform in ("darwin", "linux", "win32"):
            self.assertTrue(cfg.supports_platform(sys_platform), sys_platform)


class TestStoreHandlesAreReleasedOnShutdown(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        # Registered first so it runs last: the connection must be closed before
        # the directory holding its database is removed.
        self.addCleanup(self._rmtree)
        self._store = PreferenceStore(db_path=Path(self._tmp) / "preferences.db")
        # Closing an already-closed sqlite connection is a no-op, so this is safe
        # alongside the close the tests themselves assert.
        self.addCleanup(self._store.close)

    def _rmtree(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    async def test_register_routes_registers_the_shutdown_hook(self) -> None:
        """Without this the hook can be written and never wired up."""
        app = web.Application()
        routes_mod.register_routes(app)
        self.assertIn(routes_mod._close_store, app.on_cleanup)

    async def test_the_cleanup_closes_the_connection_and_clears_the_singleton(self) -> None:
        with mock.patch.object(routes_mod, "_store", self._store):
            await routes_mod._close_store(web.Application())
            self.assertIsNone(
                routes_mod._store,
                "a request arriving mid-shutdown must rebuild, not reuse a closed store",
            )
        with self.assertRaises(sqlite3.ProgrammingError):
            self._store.list_all()

    async def test_the_data_directory_can_be_removed_after_cleanup(self) -> None:
        """The reason the hook exists, stated as the outcome it buys.

        ``shutil.rmtree`` without ``ignore_errors`` raises ``PermissionError`` on
        Windows while any handle on the database or its WAL siblings is still
        open, so this asserts the handles are genuinely gone rather than merely
        that a ``close`` was called. On POSIX it passes either way -- the
        platform that can regress is the one the assertion is for.
        """
        self._store.add("a preference that forces the WAL siblings into existence")
        with mock.patch.object(routes_mod, "_store", self._store):
            await routes_mod._close_store(web.Application())
        shutil.rmtree(self._tmp)
        self.assertFalse(Path(self._tmp).exists())


if __name__ == "__main__":
    unittest.main()
