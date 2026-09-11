"""The one semantic core both surfaces read a cron target through.

``POST /api/crons`` and ``kirocrew cron adopt`` apply different POLICY to a target
session -- the route accepts only a live dashboard slot, adopt accepts any
namespace an operator can name -- but they must agree on what a target string
MEANS. These pin the shared meaning, and pin the drift that motivated sharing it.
"""

from __future__ import annotations

import pytest

from kiro_crew.cron_session_target import (
    DASHBOARD_PREFIX,
    check_dashboard_target,
    dashboard_slot_of,
    is_dashboard_target,
    namespace_target,
    unsupported_namespace_problem,
)
from kiro_crew.history import ConversationLog

_MAX = 200


class TestNamespacing:
    def test_a_bare_name_gains_the_dashboard_namespace(self):
        assert namespace_target("my-chat") == "dashboard:my-chat"

    def test_an_already_namespaced_key_passes_through(self):
        assert namespace_target("dashboard:my-chat") == "dashboard:my-chat"

    def test_a_foreign_namespace_is_preserved_not_rewritten(self):
        """Whether that namespace is ACCEPTABLE is the caller's policy.

        Rewriting it here would hide a channel key from the caller whose job is to
        decide about it.
        """
        assert namespace_target("slack:1785370133.085469") == "slack:1785370133.085469"

    def test_surrounding_whitespace_is_not_part_of_the_name(self):
        assert namespace_target("  my-chat  ") == "dashboard:my-chat"

    def test_an_empty_target_stays_empty(self):
        assert namespace_target("   ") == ""

    def test_slot_name_round_trips(self):
        assert dashboard_slot_of(namespace_target("my-chat")) == "my-chat"

    def test_only_dashboard_keys_are_delivery_targets(self):
        assert is_dashboard_target("dashboard:my-chat")
        assert not is_dashboard_target("slack:1785370133.085469")


class TestTheTranscriptAddressBothSurfacesDependOn:
    def test_a_dashboard_conversation_is_filed_under_the_namespaced_key(self, tmp_path):
        """The drift that motivated one shared core.

        A dashboard chat's transcript is keyed by ``dashboard:<slot>``, so a probe
        that asks with the BARE slot name reads a file that normally does not
        exist -- and reports "no recorded session" for a conversation sitting
        right next to it. Anything probing a target has to derive the key the same
        way, which is what :func:`namespace_target` is for.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("dashboard:my-chat", "user", "what shipped?")
        assert log.has_log(namespace_target("my-chat")) is True
        assert log.has_log("my-chat") is False


class TestDiagnosis:
    def test_a_clean_live_target_has_no_problem(self):
        assert (
            check_dashboard_target(
                "dashboard:my-chat",
                max_len=_MAX,
                slot_exists=True,
                linked_session_key="",
                display="my-chat",
            )
            is None
        )

    def test_the_cap_applies_to_the_namespaced_key(self):
        """The store persists and bounds the prefixed form.

        Budgeting only the bare input leaves the namespace's own characters
        unaccounted for, so a name just inside the cap overflows it downstream.
        """
        just_under = "x" * (_MAX - len(DASHBOARD_PREFIX))
        assert (
            check_dashboard_target(
                f"{DASHBOARD_PREFIX}{just_under}",
                max_len=_MAX,
                slot_exists=True,
                linked_session_key="",
                display="x",
            )
            is None
        )
        problem = check_dashboard_target(
            f"{DASHBOARD_PREFIX}{just_under}x",
            max_len=_MAX,
            slot_exists=True,
            linked_session_key="",
            display="x",
        )
        assert problem is not None
        assert problem.code == "invalid_session_key"

    @pytest.mark.parametrize("slot_exists", [False])
    def test_a_target_naming_no_slot_is_unknown(self, slot_exists):
        problem = check_dashboard_target(
            "dashboard:typo",
            max_len=_MAX,
            slot_exists=slot_exists,
            linked_session_key="",
            display="typo",
        )
        assert problem is not None
        assert problem.code == "unknown_session"
        assert "typo" in problem.message

    def test_an_empty_slot_name_is_unknown_even_when_a_slot_exists(self):
        problem = check_dashboard_target(
            DASHBOARD_PREFIX,
            max_len=_MAX,
            slot_exists=True,
            linked_session_key="",
            display="",
        )
        assert problem is not None
        assert problem.code == "unknown_session"

    def test_a_tab_showing_another_conversation_is_refused(self):
        """A channel-born tab keeps the channel's key; a cron tab carries cron:<id>.

        Delivering there would arrive in a chat with no way to list or cancel the
        job, because ownership matches the linked key and delivery matches this one.
        """
        problem = check_dashboard_target(
            "dashboard:my-chat",
            max_len=_MAX,
            slot_exists=True,
            linked_session_key="slack:1785370133.085469",
            display="my-chat",
        )
        assert problem is not None
        assert problem.code == "linked_session_not_targetable"

    def test_a_tab_linked_to_its_own_key_is_accepted(self):
        assert (
            check_dashboard_target(
                "dashboard:my-chat",
                max_len=_MAX,
                slot_exists=True,
                linked_session_key="dashboard:my-chat",
                display="my-chat",
            )
            is None
        )

    def test_the_unsupported_namespace_wording_is_shared(self):
        """Both surfaces reach this conclusion; only their policy differs."""
        problem = unsupported_namespace_problem()
        assert problem.code == "unsupported_session_namespace"
        assert "cron adopt" in problem.message
