from __future__ import annotations

import unittest

from kiro_byo.acp_compat import adapter_for_session_params
from kiro_byo.acp_conformance import PROFILES, conformance_report, validate_initialize
from kiro_byo.protocol import initialize_result


class AcpCompatConformanceTests(unittest.TestCase):
    def test_kiro_metadata_isolated_behind_host_adapter(self) -> None:
        generic = adapter_for_session_params({})
        self.assertEqual(generic.session_hints({}).host, "generic")
        kiro_params = {"_meta": {"kiro": {"modelId":"m", "effortLevel":"high", "thinkingMode":"on"}}}
        adapter = adapter_for_session_params(kiro_params)
        hints = adapter.session_hints(kiro_params)
        self.assertEqual(hints.host, "kirocrew")
        self.assertEqual(hints.requested_model, "m")
        self.assertEqual(hints.requested_effort, "high")
        self.assertEqual(hints.requested_thinking, "on")

    def test_initialize_is_stable_acp_v1_and_profiles_are_protocol_claims(self) -> None:
        self.assertEqual(validate_initialize(initialize_result()), ())
        report = conformance_report()
        self.assertTrue(report["initialize_valid"])
        ids = {row.profile_id for row in PROFILES}
        self.assertTrue({"generic", "kirocrew", "zed", "jetbrains", "qwen-code"}.issubset(ids))
        self.assertIn("session/prompt", report["standard_request_methods"])
        self.assertEqual(report["legacy_compat_request_methods"], ["session/set_model"])
        self.assertNotIn("session/set_model", report["standard_request_methods"])
        self.assertIn("_kiro/team/spawn", report["kiro_extension_methods"])


if __name__ == "__main__":
    unittest.main()
