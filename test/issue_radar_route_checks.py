"""Checks the Issue Radar route test files share, written once.

Each check lives here as a mixin. A test class in ``test_issue_radar_routes_*.py`` takes
the mixin as a base, so the node id stays ``<file>::<Class>::<test>``, the class keeps
its own setup, and pytest resolves the test's fixtures from that file.
"""

from __future__ import annotations

from kiro_crew.apps.builtins.issue_radar.backend import routes


class AParentheticalCitationIsRemovedAsOneUnit:
    def test_a_parenthetical_citation_is_removed_as_one_unit(self):
        # Matching the refs individually left the opening fragment behind.
        self.assertEqual(routes._short_rationale("crashes (see #12, #34)"), "crashes")


class AbsentAndEmptyDefaultToIssue:
    def test_absent_and_empty_default_to_issue(self):
        self.assertEqual(routes._item_kind(None), "issue")
        self.assertEqual(routes._item_kind(""), "issue")


class AMalformedOrNonObjectPayloadIs400:
    async def test_a_malformed_or_non_object_payload_is_400(self):
        self.assertEqual((await self._call(None)).status, 400)
        self.assertEqual((await self._call([1])).status, 400)
