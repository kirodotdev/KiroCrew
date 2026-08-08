"""Tests for the ``ops_mission_control_api`` MCP tool's authorization story.

The tool is the ONLY credentialed path from an agent session to the app's HTTP
surface (the ``issue_radar_record_investigation`` precedent: the MCP server
process holds the internal secret; the agent never sees a credential). Three
planes must stay mutually consistent, and each has a failure mode this module
pins:

- the **(method, path) allowlist** in ``validation.py`` — widening it is an
  authorization change and must look like one in review;
- the **schema** rejecting off-surface calls before any HTTP happens;
- the **gateway's mixed-internal path set** admitting exactly the allowlisted
  surface for internal-secret callers, and never the app's configuration,
  webhook-ingest, or human-decision routes.
"""

import unittest

from kiro_crew.validation import (
    OPS_MISSION_CONTROL_ALLOWED_CALLS,
    OPS_MISSION_CONTROL_API_SCHEMA,
    ValidationError,
    validate_tool_args,
)


class TestAllowlist(unittest.TestCase):
    def test_the_agent_surface_is_exactly_what_the_sops_use(self):
        """Every path an SOP instructs, nothing more.

        A new entry here must come WITH an SOP that needs it — this test is the
        review speed bump for widening the agent surface.
        """
        self.assertEqual(
            OPS_MISSION_CONTROL_ALLOWED_CALLS,
            frozenset(
                {
                    ("GET", "/state"),
                    ("GET", "/signals"),
                    ("GET", "/incidents"),
                    ("GET", "/handover"),
                    ("GET", "/rotation"),
                    ("GET", "/ledger"),
                    ("GET", "/ledger/contradictions"),
                    ("POST", "/dispatch"),
                    ("POST", "/incident/transition"),
                    ("POST", "/incident/claim"),
                    ("POST", "/incident/action"),
                    ("POST", "/rotation/arm"),
                    ("POST", "/ledger"),
                    ("POST", "/ledger/hygiene"),
                }
            ),
        )

    def test_excluded_routes_stay_excluded(self):
        """The routes an agent must never reach through the tool.

        ``/incident/proposal/decide`` is the human approval gate for provider
        actions; the provider config/secret routes hold credentials; ``/webhook``
        is external ingest with its own signature auth; bare ``/incident`` is
        excluded because the gateway's path matcher is exact-or-prefix and
        admitting it would prefix-admit the proposal routes.
        """
        allowed_paths = {p for _, p in OPS_MISSION_CONTROL_ALLOWED_CALLS}
        for excluded in (
            "/incident",
            "/incident/propose",
            "/incident/proposal/decide",
            "/proposals",
            "/providers",
            "/settings",
            "/webhook",
        ):
            self.assertNotIn(excluded, allowed_paths)


class TestSchema(unittest.TestCase):
    def _validate(self, **kwargs):
        return validate_tool_args(kwargs, OPS_MISSION_CONTROL_API_SCHEMA)

    def test_a_valid_get_passes(self):
        cleaned = self._validate(method="GET", path="/state")
        self.assertEqual(cleaned["method"], "GET")
        self.assertEqual(cleaned["path"], "/state")

    def test_a_valid_post_with_body_passes(self):
        cleaned = self._validate(
            method="POST",
            path="/incident/transition",
            body_json='{"id": "INV-7", "status": "resolved"}',
        )
        self.assertEqual(cleaned["path"], "/incident/transition")

    def test_method_path_pair_is_checked_not_just_membership(self):
        """Both halves are individually legal; the PAIR is not.

        ``/state`` is a real path and ``POST`` is a real method — a validator
        that checked the two enums independently would pass this.
        """
        with self.assertRaises(ValidationError):
            self._validate(method="POST", path="/state")
        with self.assertRaises(ValidationError):
            self._validate(method="GET", path="/incident/claim")

    def test_off_surface_paths_are_rejected(self):
        for path in ("/incident/proposal/decide", "/settings", "/webhook", "/incident"):
            with self.assertRaises(ValidationError):
                self._validate(method="POST", path=path)

    def test_full_urls_are_rejected(self):
        """Paths are app-base-relative; a full URL means the caller is confused."""
        with self.assertRaises(ValidationError):
            self._validate(method="GET", path="/api/apps/ops-mission-control/state")

    def test_query_cannot_smuggle_a_path(self):
        """No '/', '?' or '#': a query can never rewrite the path it rides on."""
        for bad in ("../incident/propose", "a=1?b=2", "x=1#frag", "a=/etc/passwd"):
            with self.assertRaises(ValidationError):
                self._validate(method="GET", path="/incidents", query=bad)

    def test_query_is_get_only(self):
        with self.assertRaises(ValidationError):
            self._validate(method="POST", path="/ledger", query="a=1")

    def test_body_is_post_only(self):
        with self.assertRaises(ValidationError):
            self._validate(method="GET", path="/state", body_json='{"a": 1}')


class TestGatewayAdmission(unittest.TestCase):
    """The mixed-internal path set and the allowlist must describe one surface."""

    def _mixed(self):
        from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS

        return _MIXED_INTERNAL_API_PATHS

    @staticmethod
    def _admitted(path: str, mixed) -> bool:
        # Mirrors token_auth.middleware's matcher: exact or prefix.
        return any(path == p or path.startswith(p + "/") for p in mixed)

    def test_every_allowlisted_call_is_admitted(self):
        """A pair the schema allows but the gateway 403s is a broken tool."""
        mixed = self._mixed()
        for _method, path in OPS_MISSION_CONTROL_ALLOWED_CALLS:
            full = "/api/apps/ops-mission-control" + path
            self.assertTrue(
                self._admitted(full, mixed),
                f"{full} is allowlisted for the tool but not admitted by the gateway",
            )

    def test_sensitive_routes_are_not_admitted(self):
        """The routes that must stay dashboard-only (cookie/token auth).

        This is the property PR #1066 named when it chose full paths over the
        app prefix: prefix-matching would admit provider secret writes and the
        human proposal-decision route to anything holding the internal secret.
        """
        mixed = self._mixed()
        for path in (
            "/api/apps/ops-mission-control",
            "/api/apps/ops-mission-control/incident",
            "/api/apps/ops-mission-control/incident/propose",
            "/api/apps/ops-mission-control/incident/proposal/decide",
            "/api/apps/ops-mission-control/proposals",
            "/api/apps/ops-mission-control/providers",
            "/api/apps/ops-mission-control/providers/aws/secret",
            "/api/apps/ops-mission-control/providers/aws/config",
            "/api/apps/ops-mission-control/settings",
            "/api/apps/ops-mission-control/webhook",
            "/api/apps/ops-mission-control/config",
        ):
            self.assertFalse(
                self._admitted(path, mixed),
                f"{path} must not be reachable with the internal secret",
            )


if __name__ == "__main__":
    unittest.main()
