"""Code Review Sage never falls back to Kiro CLI in an LTS build (RFC D6).

In ``oss`` a review runs on Kiro CLI when the configured backend is served neither by
the shared review runtime nor by a per-task client. In an LTS build that
automatic fallback is gone: Kiro CLI runs a review only when it is picked
(``agent.acp_backend`` ``"kiro"``) and the remote ``agent_backend`` policy allows
it. With no backend configured an LTS build runs on its governed default,
KAS, and a KAS that the gateway's boot proved cannot start is refused before any
process.
"""

import unittest
import unittest.mock

from sage_lib import review_pool as rp

from kiro_crew import lts_boot
from kiro_crew.agent_sdk import backends as acp_backends

KIRO = acp_backends.ACP_BACKEND_KIRO
KAS = acp_backends.ACP_BACKEND_KAS
CLAUDE = acp_backends.ACP_BACKEND_CLAUDE


class _Base(unittest.TestCase):
    def setUp(self):
        rp._WARNED_FALLBACK_BACKENDS.clear()
        self.addCleanup(rp._WARNED_FALLBACK_BACKENDS.clear)
        patcher = unittest.mock.patch.object(lts_boot, "_kas_refusal", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _lts(self):
        patcher = unittest.mock.patch("kiro_crew.edition.EDITION", "lts")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _selectable(self, *backends):
        for name in ("_baseline", "_selectable"):
            patcher = unittest.mock.patch.object(acp_backends, name, set(backends))
            patcher.start()
            self.addCleanup(patcher.stop)

    def _configure(self, value):
        stored = rp._ABSENT_AGENT_KEY if value is None else value
        patcher = unittest.mock.patch.object(
            rp, "_stored_agent_value", lambda key: stored if key == "acp_backend" else None
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _no_client(self):
        # Every registered backend is served one way or the other, so "served
        # neither way" is a backend no per-task client can run.
        patcher = unittest.mock.patch.object(
            rp,
            "capabilities_for",
            lambda backend: type("Caps", (), {"acp_client_spawnable": False})(),
        )
        patcher.start()
        self.addCleanup(patcher.stop)


class TestLts(_Base):
    def test_an_unserved_backend_is_refused_even_when_kiro_is_allowed(self):
        self._lts()
        self._selectable(KIRO, KAS, CLAUDE)
        self._configure("claude")
        self._no_client()
        with self.assertRaises(rp.ReviewHarnessDenied) as exc:
            rp.review_backend()
        message = str(exc.exception)
        self.assertTrue(message.startswith("review_harness_unavailable:"))
        self.assertIn("does not fall back to the Kiro CLI harness", message)
        self.assertIn("kiro-cli update", message)
        self.assertIn("--agent-engine v3", message)
        self.assertIn("Nothing runs on another harness", message)

    def test_a_client_backend_the_policy_allows_runs_on_it(self):
        self._lts()
        self._selectable(KAS, CLAUDE)
        self._configure("claude")
        self.assertEqual(rp.review_backend(), CLAUDE)

    def test_an_explicit_kiro_pick_the_policy_allows_runs(self):
        self._lts()
        self._selectable(KIRO, KAS)
        self._configure("kiro")
        self.assertEqual(rp.review_backend(), KIRO)

    def test_an_explicit_kiro_pick_the_policy_denies_runs_on_kas(self):
        # The denied pick resolves to the KAS floor through the one gate.
        self._lts()
        self._selectable(KAS)
        self._configure("kiro")
        self.assertEqual(rp.review_backend(), KAS)

    def test_no_backend_runs_on_the_governed_default(self):
        self._lts()
        self._selectable(KIRO, KAS)
        self._configure(None)
        self.assertEqual(rp.review_backend(), KAS)

    def test_a_kas_boot_proved_unstartable_is_refused(self):
        self._lts()
        self._selectable(KAS)
        self._configure(None)
        with unittest.mock.patch.object(lts_boot, "_kas_refusal", ("KAS cannot start: x", "do y")):
            with self.assertRaises(rp.ReviewHarnessDenied) as exc:
                rp.review_backend()
        self.assertIn("KAS cannot start: x", str(exc.exception))

    def test_preflight_names_the_refusal_and_spawns_nothing(self):
        self._lts()
        self._selectable(KAS, CLAUDE)
        self._configure("claude")
        self._no_client()
        with (
            unittest.mock.patch.object(rp, "AcpRuntime", object()),
            unittest.mock.patch.object(rp, "resolve_kiro_cli", lambda: "/usr/local/bin/kiro-cli"),
        ):
            message = rp.runtime_preflight()
        self.assertTrue(message.startswith("review_harness_unavailable:"))


class TestOssUnchanged(_Base):
    def test_no_backend_stays_on_kiro(self):
        self._selectable(KAS)
        self._configure(None)
        self.assertEqual(rp.review_backend(), KIRO)

    def test_an_unserved_backend_falls_back_to_kiro(self):
        self._selectable(KAS, CLAUDE)
        self._configure("claude")
        self._no_client()
        self.assertEqual(rp.review_backend(), KIRO)

    def test_a_recorded_kas_refusal_is_ignored(self):
        self._selectable(KAS)
        self._configure("kas")
        with unittest.mock.patch.object(lts_boot, "_kas_refusal", ("KAS cannot start: x", "do y")):
            self.assertEqual(rp.review_backend(), KAS)


if __name__ == "__main__":
    unittest.main()
