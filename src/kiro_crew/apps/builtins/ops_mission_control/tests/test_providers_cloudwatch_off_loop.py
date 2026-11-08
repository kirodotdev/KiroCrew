"""The CloudWatch adapters check readiness off the event loop.

``CloudWatchSignalSource.poll`` runs on every heartbeat and
``CloudWatchEvidenceSource.gather`` on every investigation. Each asked
``configured()`` -- a ``config.json`` read through ``provider_enabled`` -- inline in
the coroutine before its ``asyncio.to_thread`` hop, so a slow disk stalled the loop
every session on the gateway shares. These record the thread each config read runs
on and require it is not the loop's.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import unittest
from typing import Any
from unittest import mock

from kiro_crew.apps.builtins.ops_mission_control.backend import providers
from kiro_crew.apps.builtins.ops_mission_control.backend.models import Signal
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import cloudwatch
from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import EvidenceBudget


class TestCloudWatchReadsConfigOffTheLoop(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.read_threads: list[int] = []
        self.sync_calls: list[str] = []
        real_read = providers.read_config

        def _recording_read() -> dict[str, Any]:
            self.read_threads.append(threading.get_ident())
            return real_read()

        def _fake_poll_sync(_self: Any) -> list[Signal]:
            self.sync_calls.append("poll")
            return []

        def _fake_gather_sync(_self: Any, _signal: Signal, _budget: EvidenceBudget) -> list:
            self.sync_calls.append("gather")
            return []

        for patcher in (
            mock.patch.dict(os.environ, {"KIROCREW_HOME": self.tmp}),
            mock.patch.object(providers, "read_config", _recording_read),
            mock.patch.object(cloudwatch.CloudWatchSignalSource, "_poll_sync", _fake_poll_sync),
            mock.patch.object(
                cloudwatch.CloudWatchEvidenceSource, "_gather_sync", _fake_gather_sync
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _configure(self, slots: dict[str, Any]) -> None:
        providers.set_top_level("providers", slots)
        # The setup write ran on this thread; only the adapter's own reads count.
        self.read_threads.clear()

    def _assert_off_loop(self) -> None:
        self.assertTrue(self.read_threads, "the adapter read no config at all")
        self.assertNotIn(threading.get_ident(), self.read_threads)

    @staticmethod
    def _signal(source: str = cloudwatch.PROVIDER_ID) -> Signal:
        return Signal.create(
            source=source,
            native_id="alarm/api-5xx",
            title="api 5xx",
            severity="warning",
            state="firing",
            labels={"alarm_name": "api-5xx"},
        )

    async def test_poll_checks_readiness_off_the_loop(self):
        self._configure({cloudwatch.PROVIDER_ID: {"enabled": True}})

        self.assertEqual(await cloudwatch.CloudWatchSignalSource().poll(), [])

        self._assert_off_loop()
        self.assertEqual(self.sync_calls, ["poll"])

    async def test_disabled_poll_checks_off_the_loop_and_polls_nothing(self):
        self._configure({cloudwatch.PROVIDER_ID: {"enabled": False}})

        self.assertEqual(await cloudwatch.CloudWatchSignalSource().poll(), [])

        self._assert_off_loop()
        self.assertEqual(self.sync_calls, [])

    async def test_gather_checks_readiness_off_the_loop(self):
        self._configure({cloudwatch.EVIDENCE_PROVIDER_ID: {"enabled": True}})

        out = await cloudwatch.CloudWatchEvidenceSource().gather(self._signal(), EvidenceBudget())

        self.assertEqual(out, [])
        self._assert_off_loop()
        self.assertEqual(self.sync_calls, ["gather"])

    async def test_disabled_gather_checks_both_namespaces_off_the_loop(self):
        # Neither namespace enabled: configured() reads config twice, and both reads
        # must be off the loop.
        self._configure({})

        out = await cloudwatch.CloudWatchEvidenceSource().gather(self._signal(), EvidenceBudget())

        self.assertEqual(out, [])
        self._assert_off_loop()
        self.assertEqual(len(self.read_threads), 2)
        self.assertEqual(self.sync_calls, [])

    async def test_gather_skips_a_foreign_signal_without_gathering(self):
        self._configure({cloudwatch.PROVIDER_ID: {"enabled": True}})

        out = await cloudwatch.CloudWatchEvidenceSource().gather(
            self._signal(source="datadog"), EvidenceBudget()
        )

        self.assertEqual(out, [])
        self.assertNotIn(threading.get_ident(), self.read_threads)
        self.assertEqual(self.sync_calls, [])


if __name__ == "__main__":
    unittest.main()
