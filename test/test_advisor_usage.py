"""Advisor usage attribution: reviewer spend is visible and never the parent's.

Contract under test (see docs/system-specs/modules/advisor.md):

- An advisor reviewer usage row is keyed by the reviewer session's own stable
  synthetic key (``advisor:<parent session key>``) and tagged ``surface="advisor"`` -- the two
  existing token-record fields attribute the spend; the row is never keyed by
  the parent session and never appears as a user turn.
- ``advisor_usage_kwargs`` composes exactly those fields from a
  ``ReviewerSession``; the token-record schema is untouched.
"""

from __future__ import annotations

from kiro_crew.advisor.runtime import ReviewerSession
from kiro_crew.advisor.usage import advisor_usage_kwargs


class TestAdvisorUsageKwargs:
    def test_kwargs_compose_the_reviewer_identity_only(self):
        session = ReviewerSession("dashboard:a")
        kwargs = advisor_usage_kwargs(session)
        assert kwargs == {"slot_key": session.session_id, "surface": "advisor"}
        assert kwargs["slot_key"].startswith("advisor:")
        assert kwargs["slot_key"] != "dashboard:a"

    def test_reviewer_key_is_stable_per_session(self):
        session = ReviewerSession("dashboard:a")
        assert (
            advisor_usage_kwargs(session)["slot_key"] == advisor_usage_kwargs(session)["slot_key"]
        )

    def test_reviewer_key_survives_a_restart(self):
        """Derived from the parent key, not a process-local counter: spend for
        one conversation stays traceable across gateway restarts."""
        assert (
            advisor_usage_kwargs(ReviewerSession("dashboard:a"))["slot_key"]
            == advisor_usage_kwargs(ReviewerSession("dashboard:a"))["slot_key"]
        )

    def test_distinct_parents_get_distinct_reviewer_keys(self):
        assert (
            advisor_usage_kwargs(ReviewerSession("dashboard:a"))["slot_key"]
            != advisor_usage_kwargs(ReviewerSession("dashboard:b"))["slot_key"]
        )
