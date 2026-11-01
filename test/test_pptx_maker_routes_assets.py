"""Icon asset routes: the provisioned manifest, the download worker, and its status.

Split from ``test_pptx_maker_routes.py`` by topic; the live-routes fixture both
modules use is in ``pptx_maker_routes_helpers.py``.
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pptx_maker_routes_helpers import _await_detached, _enabled, _RoutesFixture

from kiro_crew.apps.builtins.pptx_maker.backend import engine, routes


class TestIconProvisionedManifest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _pack(self, source: str) -> None:
        (self.tmp / source).mkdir(parents=True, exist_ok=True)
        (self.tmp / source / "manifest.json").write_text("{}", encoding="utf-8")

    def test_no_manifests_means_nothing_is_provisioned(self) -> None:
        self.assertEqual(routes._icon_provisioned(self.tmp), {})

    def test_only_sources_with_committed_manifests_are_ready(self) -> None:
        self._pack("aws")
        self.assertEqual(routes._icon_provisioned(self.tmp), {"aws": True})

    def test_all_engine_sources_are_detected(self) -> None:
        for source in engine.ICON_SOURCES:
            self._pack(source)
        self.assertEqual(set(routes._icon_provisioned(self.tmp)), set(engine.ICON_SOURCES))


class TestProvisionAssetsWorker(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config_dir = self.tmp / "sdpm"
        self.config_dir.mkdir(parents=True)
        routes._assets_state = engine.ProvisionState()
        self.addCleanup(setattr, routes, "_assets_state", engine.ProvisionState())

    def _run(
        self,
        *,
        force: bool = False,
        result: engine.EngineResult | None = None,
        create_manifest: bool = True,
    ) -> list[str]:
        calls: list[str] = []
        outcome = result or engine.EngineResult(returncode=0, stdout="{}")

        def _install(source: str, target: Path) -> engine.EngineResult:
            calls.append(source)
            if outcome.returncode == 0 and create_manifest:
                pack = target / source
                pack.mkdir(parents=True, exist_ok=True)
                (pack / "manifest.json").write_text("{}", encoding="utf-8")
            return outcome

        with (
            mock.patch.object(routes.engine, "user_config_dir", return_value=self.config_dir),
            mock.patch.object(routes.engine, "install_asset_source", side_effect=_install),
        ):
            routes._provision_assets(force)
        return calls

    def test_a_not_ready_engine_is_an_error_state_not_a_crash(self) -> None:
        with mock.patch.object(routes.engine, "user_config_dir", return_value=None):
            routes._provision_assets(False)
        self.assertEqual(routes._assets_state.state, "error")
        self.assertIn("not ready", routes._assets_state.log)

    def test_a_successful_run_installs_every_manifest(self) -> None:
        self.assertEqual(self._run(), list(engine.ICON_SOURCES))
        self.assertEqual(routes._assets_state.state, "done")
        for source in engine.ICON_SOURCES:
            self.assertTrue((self.config_dir / "assets" / source / "manifest.json").is_file())

    def test_a_failed_download_is_recorded_per_source(self) -> None:
        self._run(result=engine.EngineResult(returncode=1, stderr="404"))
        self.assertEqual(routes._assets_state.state, "error")
        for source in engine.ICON_SOURCES:
            self.assertEqual(routes._assets_state.per_source[source], "error")

    def test_a_zero_exit_without_a_manifest_is_still_a_failure(self) -> None:
        self._run(create_manifest=False)
        self.assertEqual(routes._assets_state.state, "error")
        self.assertTrue(
            all(routes._assets_state.per_source[s] == "error" for s in engine.ICON_SOURCES)
        )

    def test_an_already_provisioned_pack_is_skipped(self) -> None:
        self._run()
        self.assertEqual(self._run(), [])
        self.assertEqual(routes._assets_state.state, "done")

    def test_force_reinstalls_every_pack(self) -> None:
        self._run()
        self.assertEqual(self._run(force=True), list(engine.ICON_SOURCES))

    def test_a_failed_forced_refresh_keeps_the_last_good_pack(self) -> None:
        """The refresh downloads into staging, so a failure must not have
        deleted the pack the user already had."""
        self._run()
        self._run(force=True, result=engine.EngineResult(returncode=1, stderr="offline"))
        self.assertEqual(routes._assets_state.state, "error")
        for source in engine.ICON_SOURCES:
            self.assertTrue((self.config_dir / "assets" / source / "manifest.json").is_file())
        self.assertFalse((self.config_dir / "assets.staging").exists())

    def test_a_successful_forced_refresh_replaces_the_pack(self) -> None:
        self._run()
        stale = self.config_dir / "assets" / engine.ICON_SOURCES[0] / "stale.txt"
        stale.write_text("old", encoding="utf-8")
        self._run(force=True)
        self.assertEqual(routes._assets_state.state, "done")
        self.assertFalse(stale.exists())
        self.assertFalse((self.config_dir / "assets.staging").exists())
        for source in engine.ICON_SOURCES:
            self.assertFalse((self.config_dir / "assets" / f"{source}.old").exists())

    def test_an_uncreatable_assets_dir_is_an_error_state(self) -> None:
        real_mkdir = Path.mkdir
        target = self.config_dir / "assets"

        def _deny(self_path, *args, **kwargs):
            if self_path == target:
                raise OSError("read-only")
            return real_mkdir(self_path, *args, **kwargs)

        with (
            mock.patch.object(routes.engine, "user_config_dir", return_value=self.config_dir),
            mock.patch.object(Path, "mkdir", _deny),
        ):
            routes._provision_assets(False)
        self.assertEqual(routes._assets_state.state, "error")
        self.assertIn("cannot create", routes._assets_state.log)


class TestAssetsLogTail(unittest.TestCase):
    def test_the_log_is_bounded_so_a_long_run_cannot_grow_without_limit(self) -> None:
        """The UI polls this state; an unbounded list would grow for the whole
        download and be re-joined on every append."""
        lines: list[str] = []
        for i in range(900):
            routes._record_assets_log(lines, f"line {i}")
        self.assertEqual(len(lines), 400)
        self.assertEqual(lines[-1], "line 899")


class TestAssetsStatusRoute(_RoutesFixture):
    async def test_status_reports_every_source_and_the_engine_tag(self) -> None:
        with (
            _enabled(True),
            mock.patch.object(routes.engine, "user_config_dir", return_value=None),
            mock.patch.object(routes.engine, "engine_tag", return_value="v0.3.8"),
        ):
            resp = await self.client.get(self.url("/assets"))
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["sources"], list(engine.ICON_SOURCES))
        self.assertEqual(body["tag"], "v0.3.8")
        # A not-ready engine must report every pack as unprovisioned, not ready.
        self.assertFalse(body["ready"])
        self.assertFalse(any(body["provisioned"].values()))

    async def test_provision_refuses_to_start_twice(self) -> None:
        """One job at a time: a second POST while running must not spawn a
        parallel download over the same target directory."""
        with _enabled(True):
            routes._assets_state.state = "running"
            try:
                with mock.patch.object(routes, "_provision_assets") as worker:
                    resp = await self.client.post(self.url("/assets/provision"))
                self.assertEqual(resp.status, 202)
                self.assertEqual((await resp.json())["state"], "running")
                worker.assert_not_called()
            finally:
                routes._assets_state.state = "idle"

    async def test_provision_forwards_the_force_flag(self) -> None:
        """``?force=true`` is what re-downloads an already-provisioned pack; if it
        were dropped the endpoint would silently no-op for those users."""
        seen: list[bool] = []
        with (
            _enabled(True),
            mock.patch.object(routes, "_provision_assets", side_effect=seen.append),
        ):
            resp = await self.client.post(self.url("/assets/provision?force=true"))
            self.assertEqual(resp.status, 202)
            await _await_detached(lambda: bool(seen))
        routes._assets_state.state = "idle"
        self.assertEqual(seen, [True])

    async def test_provision_defaults_to_not_forcing(self) -> None:
        seen: list[bool] = []
        with (
            _enabled(True),
            mock.patch.object(routes, "_provision_assets", side_effect=seen.append),
        ):
            resp = await self.client.post(self.url("/assets/provision"))
            self.assertEqual(resp.status, 202)
            await _await_detached(lambda: bool(seen))
        routes._assets_state.state = "idle"
        self.assertEqual(seen, [False])
