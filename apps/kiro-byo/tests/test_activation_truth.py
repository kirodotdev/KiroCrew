"""Regression tests for the 'Integration says installed, picker has no Types' failure.

Real incident (KiroCrew 0.9.0-insider.6, Kiro BYO 0.1.0):

1. Gateway PID 19348 started, then the App's Install/Update Integration rewrote
   ``kiro_crew/dashboard/handlers/agents.py`` on disk (``installed_by_pid=19348``).
2. The running Gateway kept the pre-install ``agents.py`` in memory.
3. The Kiro BYO ACP child (``python -m kiro_byo acp``) started from the same
   interpreter, its ``.pth`` hook wrote ``activation.json`` with ITS pid, and the
   App therefore reported the integration as active -- no restart banner.
4. ``/api/models`` kept serving the old in-memory code: no ``kiro-byo-type:*``.

The old tests only transformed source text; they never asked *which process*
recorded activation, nor ran Crew's real ``api_models`` against the patched tree.
"""
from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import patch

from kiro_byo.integration_manager import _activation_is_live
from kiro_byo.process_role import is_crew_gateway_process

PY = r"C:\KiroCrew\backend\python.exe"
GATEWAY_ARGV = [PY, "-s", "-P", "-m", "kiro_crew", "gateway", "--no-open", "--port", "5476"]


class ProcessRoleTests(unittest.TestCase):
    def test_gateway_is_recognised(self) -> None:
        self.assertTrue(is_crew_gateway_process(GATEWAY_ARGV))

    def test_children_of_the_same_interpreter_are_not_the_gateway(self) -> None:
        for argv in (
            [PY, "-s", "-P", "-m", "kiro_byo", "acp"],
            [PY, "-s", "-P", "-m", "kiro_crew", "mcp-core"],
            [PY, "-s", "-P", "-m", "kiro_crew", "mcp-cron"],
            [PY, "-s", "-P", "-m", "kiro_crew.mcp_gateway.gatewayd", "--socket", "x"],
            [PY, "-s", "-P", "probe.py", "gateway"],
            [PY, "-c", "print('gateway')"],
        ):
            with self.subTest(argv=argv):
                self.assertFalse(is_crew_gateway_process(argv))


class ActivationTruthTests(unittest.TestCase):
    BUILD = "build-1"

    def test_child_process_activation_does_not_make_the_installing_gateway_live(self) -> None:
        # Exactly the incident: gateway 19348 installed, ACP child 4242 wrote activation.
        activation = {"pid": 4242, "kiro_byo_build": self.BUILD}
        self.assertFalse(
            _activation_is_live(
                activation, 19348, self.BUILD, current_pid=19348, in_gateway=True
            )
        )

    def test_fresh_gateway_that_recorded_its_own_activation_is_live(self) -> None:
        activation = {"pid": 20000, "kiro_byo_build": self.BUILD}
        self.assertTrue(
            _activation_is_live(
                activation, 19348, self.BUILD, current_pid=20000, in_gateway=True
            )
        )

    def test_stale_activation_from_a_previous_gateway_is_not_live(self) -> None:
        activation = {"pid": 18000, "kiro_byo_build": self.BUILD}
        self.assertFalse(
            _activation_is_live(
                activation, 0, self.BUILD, current_pid=20000, in_gateway=True
            )
        )

    def test_other_build_is_never_live(self) -> None:
        activation = {"pid": 20000, "kiro_byo_build": "older"}
        self.assertFalse(
            _activation_is_live(
                activation, 19348, self.BUILD, current_pid=20000, in_gateway=True
            )
        )

    def test_bootstrap_hook_does_not_record_activation_outside_gateway(self) -> None:
        from kiro_byo import bootstrap_patch

        with patch.object(bootstrap_patch, "_is_crew_gateway_process", return_value=False), \
             patch.object(bootstrap_patch, "_atomic_write_text") as write:
            bootstrap_patch._record_activation()
        write.assert_not_called()


def _real_crew_agents():
    """Import the Crew dashboard handler the way the Gateway does, or None."""
    try:
        import kiro_crew.apps  # noqa: F401 - gateway import order avoids acp.client cycle
        from kiro_crew.dashboard.handlers import agents
        from kiro_crew.config.loader import KiroCrewConfig
    except Exception:
        return None, None
    if not hasattr(agents, "_kiro_byo_models"):
        return None, None
    return agents, KiroCrewConfig


@unittest.skipUnless(
    os.environ.get("KIRO_BYO_E2E_CREW") == "1",
    "set KIRO_BYO_E2E_CREW=1 and run with KiroCrew's bundled python after Install/Update",
)
class RealCrewApiModelsTests(unittest.TestCase):
    """End-to-end: Crew's own api_models + KiroCrewConfig backend resolution."""

    def _call(self, backend: str) -> list:
        agents, cfg_cls = _real_crew_agents()
        if agents is None:
            self.skipTest("patched kiro_crew not importable in this interpreter")
        real_load = cfg_cls.load

        def load(*args, **kwargs):
            cfg = real_load(*args, **kwargs)
            # In memory only; config.json is never written.
            cfg.agent.acp_backend = backend
            return cfg

        class _Req(dict):
            query: dict = {}
            app: dict = {}

        with patch.object(cfg_cls, "load", staticmethod(load)):
            response = asyncio.run(agents.api_models(_Req()))
        self.assertEqual(response.status, 200)
        return json.loads(response.body.decode("utf-8"))

    def test_kiro_byo_backend_serves_types_before_models_and_no_auto(self) -> None:
        names = [row["model_name"] for row in self._call("kiro-byo")]
        types = [n for n in names if n.startswith("kiro-byo-type:")]
        self.assertEqual(
            types,
            [
                "kiro-byo-type:tiga:sky",
                "kiro-byo-type:tiga:multi",
                "kiro-byo-type:tiga:power",
                "kiro-byo-type:tiga:glitter",
            ],
        )
        self.assertEqual(names[: len(types)], types)
        self.assertGreater(len(names), len(types), "at least one concrete model")
        self.assertNotIn("auto", names)
        self.assertNotIn("auto:jev", names)


if __name__ == "__main__":
    unittest.main()
