from __future__ import annotations

import unittest

from kiro_byo.overview_state import build_overview_snapshot


class OverviewStateTests(unittest.TestCase):
    def _snapshot(self, **overrides):
        args = dict(
            version="0.9.10",
            app_home=r"D:\\KiroCrewData\\apps\\kiro-byo\\data",
            credential_backend="windows-credential-manager",
            providers=[{"id": "demo", "models": [{"id": "m1"}, {"id": "m2"}]}],
            runtime_capabilities={"session_capabilities": ["load", "prompt", "cancel"]},
            native_tools=["read", "glob", "grep", "write", "shell", "edit"],
            integration={"supported": True, "installed": True, "crew_version": "0.9.0-insider.2"},
            diagnostics={"turns": 4, "success": 4, "failed": 0, "median_first_visible_ms": 350},
        )
        args.update(overrides)
        return build_overview_snapshot(**args)

    def test_ready_snapshot_contains_sidebar_facts(self):
        row = self._snapshot()
        self.assertEqual(row["health"]["state"], "ready")
        self.assertEqual(row["provider_count"], 1)
        self.assertEqual(row["model_count"], 2)
        self.assertEqual(row["session_capability_count"], 3)
        self.assertEqual(row["native_tool_count"], 6)
        self.assertEqual(row["diagnostics"]["turns"], 4)
        self.assertEqual(row["crew_version"], "0.9.0-insider.2")

    def test_integration_problem_takes_precedence_over_provider(self):
        row = self._snapshot(
            providers=[],
            integration={"supported": True, "installed": False, "crew_version": "0.9.0-insider.2"},
        )
        self.assertEqual(row["health"]["state"], "needs_integration")
        self.assertEqual(row["alerts"][0]["action"], "integration")

    def test_recent_failures_link_to_records_surface(self):
        row = self._snapshot(diagnostics={"turns": 5, "success": 4, "failed": 1})
        self.assertEqual(row["health"]["state"], "degraded")
        self.assertEqual(row["alerts"][0]["action"], "records")
        self.assertIn("记录", row["alerts"][0]["detail"])

    def test_unsupported_crew_is_error(self):
        row = self._snapshot(integration={"supported": False, "installed": False, "error": "no profile"})
        self.assertEqual(row["health"]["state"], "incompatible")
        self.assertEqual(row["health"]["tone"], "error")


if __name__ == "__main__":
    unittest.main()
