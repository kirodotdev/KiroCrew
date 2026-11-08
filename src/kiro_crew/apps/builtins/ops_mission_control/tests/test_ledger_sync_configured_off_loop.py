"""``configured()`` must not read the keystone policy file on the event loop.

``run_cycle`` awaits ``sync_safely(direction="pull")`` directly on the loop on every
dispatch heartbeat, and ``sync_safely``, ``pull`` and ``push`` each opened with an inline
``configured()``. That is one or two ``policy_store.get`` calls, and each one re-reads and
parses the keystone policy file (``policy_store._read``). Every git step in those functions
already runs off the loop. These record the thread each policy read runs on and require
that it is not the loop's.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import unittest
from typing import Any
from unittest import mock


class TestLedgerSyncConfiguredOffTheLoop(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync, policy_store

        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.read_threads: list[int] = []
        self.policy: dict[str, Any] = {}

        def _recording_get(key: str, default: Any = None) -> Any:
            self.read_threads.append(threading.get_ident())
            return self.policy.get(key, default)

        # Stop right after the gate: these tests are about the configuration check, and
        # nothing past it should run (no repo init, no git spawn).
        self.ensure_repo = mock.AsyncMock(return_value=(False, "stopped after the gate"))
        for patcher in (
            mock.patch.dict(os.environ, {"KIROCREW_HOME": self.tmp}),
            mock.patch.object(policy_store, "get", _recording_get),
            mock.patch.object(ledger_sync, "_ensure_repo", self.ensure_repo),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _configure(self) -> None:
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync

        self.policy = {
            ledger_sync._ENABLED_KEY: True,
            ledger_sync._REMOTE_KEY: "https://example.invalid/team/ledger.git",
        }

    def _assert_off_loop(self) -> None:
        self.assertTrue(self.read_threads, "the configuration check read no policy at all")
        self.assertNotIn(threading.get_ident(), self.read_threads)

    async def test_unconfigured_sync_safely_reads_the_policy_off_the_loop(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync

        result = await ledger_sync.sync_safely(direction="pull")

        self.assertEqual(result, "")
        self._assert_off_loop()
        self.ensure_repo.assert_not_awaited()

    async def test_configured_sync_safely_pull_reads_the_policy_off_the_loop(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync

        self._configure()
        result = await ledger_sync.sync_safely(direction="pull")

        # Both gates passed (sync_safely's and pull's), so the step reached the repo.
        self.assertEqual(result, "stopped after the gate")
        self._assert_off_loop()
        self.ensure_repo.assert_awaited_once()

    async def test_unconfigured_pull_and_push_read_the_policy_off_the_loop(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync

        self.assertEqual(await ledger_sync.pull(), (False, "ledger sync is not configured"))
        self.assertEqual(await ledger_sync.push(), (False, "ledger sync is not configured"))
        self._assert_off_loop()
        self.ensure_repo.assert_not_awaited()

    async def test_configured_push_reads_the_policy_off_the_loop(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync

        self._configure()

        self.assertEqual(await ledger_sync.push(), (False, "stopped after the gate"))
        self._assert_off_loop()
        self.ensure_repo.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
