"""The wake judge's calibration feedback loop.

Four things are under test and they are separable on purpose: the deterministic
labelling rule, the three states a verdict row can be in, the retroactive pass that
credits a run of suppressed verdicts to the delivery that followed them, and the
elapsed-time context the state carries.

The three states matter most, because two of them look alike and are not. A verdict
that WITHHELD the turn is eligible for a `missed` label. A verdict whose turn was
CONFIRMED delivered is eligible for `owner_acted`. A verdict that decided to wake but
whose fire was refused, cancelled or lost is neither, and labelling it would hand one
turn's actions to a verdict that never produced it.

Every case here drives pure functions with literals. The service wiring is exercised
through ``AutoNudgeService`` only where the behaviour IS the wiring -- a loop record
that survives a round trip.

The judge tick's ``answered`` flag, its decision-log rows, the streak bound the point
shares with the engine and the join-key bound the judge shares with the log live in
``test_wake_judge_feedback_decision_log.py``; the ``AutoNudgeService`` wiring beyond
that round trip -- the bounds its loaders put on a stored baseline and verdict history,
the turn-completion hook labelling the delivery it ends, the verdict recorded on the
tick and the probe floor's own fire -- in ``test_wake_judge_feedback_service.py``; the
shared verdict and row builders in ``wake_judge_feedback_helpers.py``.
"""

from __future__ import annotations

import json

import pytest
from wake_judge_feedback_helpers import _delivered, _suppressed, _verdict

from kiro_crew import autonudge_judge as judge
from kiro_crew import irq
from kiro_crew.autonudge import _MAX_QUIET_STREAK, AutoNudgeService
from kiro_crew.decisions.points import nudge_wake as point


def _forfeited(at: float, row_id: str = "") -> dict:
    """A stored row for a delivery whose turn this process never saw."""
    row = _delivered(at, row_id)
    row["forfeited"] = True
    return row


class TestOwnerActed:
    """The one function that decides whether a woken turn did anything."""

    def test_a_tool_call_is_action(self):
        assert judge.owner_acted(1, "") is True

    def test_many_tool_calls_are_action(self):
        assert judge.owner_acted(12, "ok") is True

    def test_no_tools_and_a_short_reply_is_the_quiet_shape(self):
        assert judge.owner_acted(0, "Nothing new since the last tick.") is False

    def test_no_tools_and_an_empty_reply_is_the_quiet_shape(self):
        assert judge.owner_acted(0, "") is False

    def test_whitespace_only_reply_is_the_quiet_shape(self):
        assert judge.owner_acted(0, "   \n\t  ") is False

    def test_a_long_reply_with_no_tools_is_action(self):
        assert judge.owner_acted(0, "x" * (judge.QUIET_REPLY_MAX_CHARS + 1)) is True

    def test_a_reply_exactly_at_the_bound_is_the_quiet_shape(self):
        # The bound is inclusive: at the limit the reply is still a quiet cycle.
        assert judge.owner_acted(0, "x" * judge.QUIET_REPLY_MAX_CHARS) is False

    def test_leading_whitespace_does_not_push_a_reply_over_the_bound(self):
        padded = " " * 50 + "x" * judge.QUIET_REPLY_MAX_CHARS + " " * 50
        assert judge.owner_acted(0, padded) is False

    @pytest.mark.parametrize(
        "reply",
        [
            "see https://github.com/o/r/pull/1",
            "http://localhost:7777/schedule",
        ],
    )
    def test_a_link_with_no_tools_is_action(self, reply):
        assert judge.owner_acted(0, reply) is True

    def test_an_unknown_tool_count_reads_as_action(self):
        # The count is the strong half of the rule, so without it the honest answer
        # is the one that does not teach the judge to suppress.
        assert judge.owner_acted(None, "") is True

    @pytest.mark.parametrize("bad", [True, False, "3", 1.5, object()])
    def test_a_non_count_reads_as_action(self, bad):
        assert judge.owner_acted(bad, "") is True

    def test_a_non_string_reply_does_not_raise(self):
        assert judge.owner_acted(0, None) is False
        assert judge.owner_acted(0, 17) is False

    def test_the_reading_takes_its_boolean_from_owner_acted(self, monkeypatch):
        calls: list = []

        def _owner_acted(*args, **kwargs):
            calls.append((args, kwargs))
            return False

        monkeypatch.setattr(judge, "owner_acted", _owner_acted)
        reply = "  a reply that the shared rule alone classifies  "
        reading = judge.owner_action_reading(1, reply, reply_flushed=True)

        # Fourth value: whether the dispatches were NAMED. Nothing was passed here, so
        # the label came from the count alone and a threshold read must exclude it.
        assert reading == (False, 1, len(reply.strip()), False)
        assert calls == [
            ((1, reply), {"reply_flushed": True, "tool_identities": None}),
        ]


class TestMutatingToolReading:
    """Which dispatches count as having changed something, and which merely looked.

    The label this feeds was constant-true across 84 of 84 labelled verdicts, because a
    COUNT cannot tell a read from a write: a turn whose only call was one read plus a
    124-character status reply scored acted. These pin the discrimination the count
    could not express.
    """

    @staticmethod
    def _builtin(name: str) -> tuple[str, str, str, bool]:
        """One dispatch of a trusted BUILT-IN, in the runner's own tuple shape."""
        return (f"tc-{name}", "", name, True)

    @staticmethod
    def _mcp(name: str, server: str = "kirocrew-core") -> tuple[str, str, str, bool]:
        return (f"tc-{name}", server, name, True)

    @pytest.mark.parametrize("name", ["fs_read", "read_file", "grep", "grep_search", "glob"])
    def test_a_read_only_builtin_changed_nothing(self, name):
        assert judge.tool_changed_something(name, "", identity_trusted=True) is False

    @pytest.mark.parametrize("name", ["fs_write", "str_replace", "delete_file", "execute_bash"])
    def test_a_writing_builtin_changed_something(self, name):
        assert judge.tool_changed_something(name, "", identity_trusted=True) is True

    def test_an_unknown_builtin_name_changed_something(self):
        """The allowlist's direction: unheard-of means it counts.

        Soundness is what the set needs. A read-only tool it has not heard of labels
        that turn acted, which is the error this label already errs toward; the reverse
        would teach the judge to suppress.
        """
        assert judge.tool_changed_something("some_new_tool", "", identity_trusted=True) is True

    def test_an_untrusted_builtin_identity_changed_something(self):
        """A name with no provenance behind it is prose, not identity.

        The same three-fact discipline ``hooks._is_host_read_only_builtin`` applies: an
        unverified pair says nothing about who named the tool.
        """
        assert judge.tool_changed_something("fs_read", "", identity_trusted=False) is True

    def test_a_server_named_fs_read_is_not_read_off_the_builtin_set(self):
        """An MCP server controls its own tool names, so it cannot borrow one."""
        assert judge.tool_changed_something("fs_read", "some-server", identity_trusted=True) is True

    @pytest.mark.parametrize(
        "name",
        [
            "memory_recall",
            "monitor_inspect",
            "spawn_status",
            "local_knowledge_search",
            "skill_search",
            "list_sessions",
            "knowledge_list_sources",
            "send_message",
            "learn_add",
        ],
    )
    @pytest.mark.parametrize(
        "server", ["kirocrew-core", "kirocrew-work", "some-vendor-mcp", "memory"]
    )
    def test_every_mcp_call_counts_as_mutating(self, name, server):
        """There is no read-only MCP tier, and this is the pin that keeps it that way.

        Read-shaped and write-shaped names, first-party servers and third-party ones: all
        mutating. Three review rounds found three tools whose names read and whose
        implementations lazily BUILT what they served, on ordinary cold-cache paths, and
        the set that named them was 25 unchecked assertions that a tool cannot write. No
        authority exists to check such a claim for MCP, unlike the built-in tier which
        derives from the approval gate's own scope table, so the tier was removed rather
        than corrected a fourth time.
        """
        assert judge.tool_changed_something(name, server, identity_trusted=True) is True

    def test_the_module_exposes_no_mcp_read_only_surface(self):
        """Re-introducing a tier must REDDEN, not pass quietly.

        A future reader who re-adds an allowlist here has to delete this test to do it,
        which is the point: the removal is a decision with evidence behind it, not an
        omission to be tidied up.
        """
        assert not hasattr(judge, "READ_ONLY_MCP_TOOLS")
        assert not hasattr(judge, "FIRST_PARTY_MCP_SERVERS")

    def test_a_first_party_server_gets_no_exemption(self):
        """The case that would have passed before, and must not now.

        ``memory_recall`` on ``kirocrew-core`` is a genuine read, and it counts as
        mutating anyway. That is the accepted cost: one flat row in the calibration curve
        for such a turn, against the alternative of trusting 25 unverifiable claims.
        """
        assert (
            judge.tool_changed_something("memory_recall", "kirocrew-core", identity_trusted=True)
            is True
        )

    def test_an_empty_name_changed_something(self):
        assert judge.tool_changed_something("", "", identity_trusted=True) is True

    def test_no_name_in_the_read_only_set_holds_a_write_scope(self):
        """The assertion that catches a write-capable tool entering the allowlist.

        A hand-spelled version of this set admitted ``code``, which
        ``governance.BUILTIN_TOOL_SCOPES`` maps to ``filesystem.write`` and
        ``commands``; a turn whose only dispatch edited the tree would then have been
        labelled a quiet cycle, which is the direction :func:`owner_acted` documents as
        the harmful one. The permissions-table join below could not catch it, because
        ``code`` has no row in ``KAS_TOOL_IDS_BY_KIRO_TOOL``.

        Names with no scope row are not covered here -- the KAS ids have none -- so this
        sits alongside that join rather than replacing it.
        """
        from kiro_crew.platform.governance import BUILTIN_TOOL_SCOPES

        write_scopes = {"filesystem.write", "commands", "tools"}
        covered = 0
        for name in judge.READ_ONLY_BUILTIN_TOOLS:
            scopes = BUILTIN_TOOL_SCOPES.get(name)
            if scopes is None:
                continue
            covered += 1
            assert not (set(scopes) & write_scopes), (name, scopes)
        # A control: the sweep above is vacuous if no name has a row at all.
        assert covered >= 5, covered
        # And the tool that got through before is named, so this cannot pass by the
        # scope table losing its row.
        assert "code" in BUILTIN_TOOL_SCOPES
        assert "code" not in judge.READ_ONLY_BUILTIN_TOOLS

    def test_the_builtin_half_is_the_approval_gate_s_own_set(self):
        """Not a copy of it: a copy is what admitted a write-capable tool."""
        from kiro_crew.hooks import _HOST_READ_ONLY_BUILTIN_TOOLS

        assert _HOST_READ_ONLY_BUILTIN_TOOLS
        assert _HOST_READ_ONLY_BUILTIN_TOOLS <= judge.READ_ONLY_BUILTIN_TOOLS

    def test_the_read_only_builtin_set_agrees_with_the_permissions_table(self):
        """The judge's literal set is held in step with the table that owns the mapping.

        ``acp.kas_permissions`` is the one kiro-cli <-> KAS tool-name table, but the
        agent-SDK boundary keeps application code off the ACP layer, so the judge cannot
        derive its set from it. This assertion is the join instead: a test may read both
        sides, so a renamed or added tool id breaks here rather than silently leaving a
        stale spelling in the judge that classifies a live read as unknown.
        """
        from kiro_crew.acp import kas_permissions

        read_only_kiro_names = ("fs_read", "grep", "glob", "web_fetch", "web_search")
        for name in read_only_kiro_names:
            assert name in judge.READ_ONLY_BUILTIN_TOOLS, name
            for tool_id in kas_permissions.KAS_TOOL_IDS_BY_KIRO_TOOL[name]:
                assert tool_id in judge.READ_ONLY_BUILTIN_TOOLS, tool_id
        # Every alias of a read-only name is itself read-only, and no alias of a
        # writing one leaked in.
        for alias, target in kas_permissions.KIRO_TOOL_ALIASES.items():
            if target in read_only_kiro_names:
                assert alias in judge.READ_ONLY_BUILTIN_TOOLS, alias
            else:
                assert alias not in judge.READ_ONLY_BUILTIN_TOOLS, alias
        # And the writing side is absent in both vocabularies.
        for name, ids in kas_permissions.KAS_TOOL_IDS_BY_KIRO_TOOL.items():
            if name in read_only_kiro_names:
                continue
            assert name not in judge.READ_ONLY_BUILTIN_TOOLS, name
            for tool_id in ids:
                assert tool_id not in judge.READ_ONLY_BUILTIN_TOOLS, tool_id

    def test_the_count_is_of_mutating_dispatches_only(self):
        """Two of these four count, and which two is the whole contract.

        The built-in reads do not count; ``fs_write`` does. ``memory_recall`` counts too
        even though it genuinely reads, because it arrives with a server name and there is
        no read-only MCP tier -- see ``test_every_mcp_call_counts_as_mutating``.
        """
        rows = (
            self._builtin("read_file"),
            self._builtin("grep"),
            self._builtin("fs_write"),
            self._mcp("memory_recall"),
        )
        assert judge.mutating_tool_count(rows) == 2

    def test_no_sequence_at_all_is_unknowable(self):
        assert judge.mutating_tool_count(None) is None

    def test_an_empty_sequence_is_a_real_zero(self):
        """Empty is the runner saying the turn dispatched nothing, not a missing read."""
        assert judge.mutating_tool_count(()) == 0

    @pytest.mark.parametrize("bad", ["fs_read", b"fs_read", {"a": 1}])
    def test_a_non_sequence_reading_is_unknowable(self, bad):
        assert judge.mutating_tool_count(bad) is None

    def test_an_unreadable_row_counts_as_mutating(self):
        assert judge.mutating_tool_count(("not-a-row",)) == 1
        assert judge.mutating_tool_count((("only-an-id",),)) == 1


class TestOwnerActedWithNames:
    """``owner_acted`` once the dispatches are named -- the defect and its fix."""

    def test_one_read_and_a_short_reply_is_the_quiet_shape(self):
        """The measured defect, at its measured shape.

        One read-only call plus a 124-character status reply. This labelled True on the
        count alone, which is how 84 of 84 rows came back True.
        """
        reply = "x" * 124
        assert judge.owner_acted(1, reply) is True
        assert (
            judge.owner_acted(
                1,
                reply,
                tool_identities=(("tc-1", "", "read_file", True),),
            )
            is False
        )

    def test_one_write_is_action(self):
        assert (
            judge.owner_acted(
                1,
                "done",
                tool_identities=(("tc-1", "", "fs_write", True),),
            )
            is True
        )

    def test_a_read_only_turn_with_a_long_reply_is_still_action(self):
        """The reply and link fallbacks are untouched by the names."""
        assert (
            judge.owner_acted(
                1,
                "x" * (judge.QUIET_REPLY_MAX_CHARS + 1),
                tool_identities=(("tc-1", "", "read_file", True),),
            )
            is True
        )
        assert (
            judge.owner_acted(
                1,
                "see https://example.invalid/x",
                tool_identities=(("tc-1", "", "read_file", True),),
            )
            is True
        )

    def test_a_flushed_reply_is_still_action(self):
        assert (
            judge.owner_acted(
                1,
                "short",
                reply_flushed=True,
                tool_identities=(("tc-1", "", "read_file", True),),
            )
            is True
        )

    def test_unnamed_dispatches_keep_the_old_count_behaviour(self):
        assert judge.owner_acted(1, "short") is True
        assert judge.owner_acted(0, "short") is False
        assert judge.owner_acted(None, "short") is True

    def test_the_named_reading_replaces_the_count_rather_than_joining_it(self):
        """Taking whichever is larger would put the defect straight back.

        Every read-only turn carries a positive count, so an OR over the two readings
        is the old rule wearing a new name.
        """
        assert (
            judge.owner_acted(
                9,
                "short",
                tool_identities=tuple(("tc", "", "read_file", True) for _ in range(9)),
            )
            is False
        )

    def test_the_reading_reports_that_the_names_were_known(self):
        acted, tool_calls, reply_chars, names_known = judge.owner_action_reading(
            1,
            "short",
            tool_identities=(("tc-1", "", "read_file", True),),
        )
        assert (acted, tool_calls, reply_chars, names_known) == (False, 1, 5, True)

    def test_the_reading_reports_that_the_names_were_not_known(self):
        acted, _tool_calls, _reply_chars, names_known = judge.owner_action_reading(1, "short")
        assert (acted, names_known) == (True, False)

    def test_rows_with_no_tool_name_are_not_named(self):
        """The shape a backend with no identity channel produces.

        ``claude-agent-acp`` and ``opencode`` send ``kind="other"`` with no identity, so
        ``_dispatch.classify_tool_call`` yields an empty ``tool_name`` and
        ``tool_identity_trusted`` is False, while the runner still appends one row per
        dispatch. Reading "a sequence was supplied" as "the names were known" would mark
        those turns name-derived and write count-only rows into an append-only
        calibration log under the flag that exists to exclude them.
        """
        unnamed = (("tc-1", "", "", False),)
        assert judge.tool_identities_are_named(unnamed) is False
        _acted, _calls, _chars, names_known = judge.owner_action_reading(
            1, "short", tool_identities=unnamed
        )
        assert names_known is False
        # Still counted as having changed something: an unreadable dispatch is not
        # evidence the turn was idle. The two questions fail in opposite directions.
        assert judge.mutating_tool_count(unnamed) == 1

    def test_one_unnamed_row_among_named_ones_makes_the_turn_unnamed(self):
        """ALL, not any -- the label is one boolean over the whole turn."""
        mixed = (
            ("tc-1", "", "read_file", True),
            ("tc-2", "", "", False),
            ("tc-3", "", "fs_write", True),
        )
        assert judge.tool_identities_are_named(mixed) is False
        assert judge.tool_identities_are_named(
            (("tc-1", "", "read_file", True), ("tc-3", "", "fs_write", True))
        )

    @pytest.mark.parametrize("bad", [None, "fs_read", b"fs_read", {"a": 1}, ("not-a-row",)])
    def test_an_absent_or_unreadable_reading_is_not_named(self, bad):
        assert judge.tool_identities_are_named(bad) is False

    def test_an_empty_dispatch_list_is_vacuously_named(self):
        """Nothing was dispatched, so the count and the named reading agree on zero."""
        assert judge.tool_identities_are_named(()) is True
        _acted, _calls, _chars, names_known = judge.owner_action_reading(
            0, "short", tool_identities=()
        )
        assert names_known is True

    def test_a_whitespace_only_tool_name_is_not_a_name(self):
        assert judge.tool_identities_are_named((("tc-1", "", "   ", True),)) is False


class TestVerdictEntry:
    """What the tick records, and the one thing it deliberately does not."""

    def test_it_keeps_the_previous_record_shape(self):
        row = judge.verdict_entry(_verdict(irq.Outcome.QUIET), 3, suppressed=True, answered=True)
        # ``verdict_record``'s own three fields are still present and still mean the
        # same thing, so a reader of the older field is not broken by the new one.
        assert row["outcome"] == "quiet"
        assert row["evidence_items"] == 3
        assert isinstance(row["at"], float)

    def test_a_wake_verdict_is_neither_suppressed_nor_delivered(self):
        # The third state, and the whole point of it: the tick decided to wake, and
        # nothing has gone out yet.
        row = judge.verdict_entry(_verdict(irq.Outcome.WAKE), 1, suppressed=False, answered=True)
        assert "suppressed" not in row
        assert "delivered" not in row

    def test_a_quiet_verdict_records_that_it_withheld_the_turn(self):
        row = judge.verdict_entry(_verdict(irq.Outcome.QUIET), 1, suppressed=True, answered=True)
        assert row["suppressed"] is True
        assert "delivered" not in row

    def test_it_records_whether_the_judge_was_asked(self):
        asked = judge.verdict_entry(_verdict(irq.Outcome.QUIET), 2, suppressed=True, answered=True)
        unasked = judge.verdict_entry(
            _verdict(irq.Outcome.QUIET), 0, suppressed=True, answered=False
        )
        assert asked["answered"] is True
        assert unasked["answered"] is False

    def test_it_carries_the_join_key_when_given_one(self):
        row = judge.verdict_entry(
            _verdict(irq.Outcome.WAKE), 0, suppressed=False, answered=True, verdict_id="ab12"
        )
        assert row["id"] == "ab12"

    def test_it_omits_the_join_key_when_given_none(self):
        row = judge.verdict_entry(_verdict(irq.Outcome.WAKE), 0, suppressed=False, answered=True)
        assert "id" not in row

    def test_an_oversized_join_key_is_clipped(self):
        row = judge.verdict_entry(
            _verdict(irq.Outcome.WAKE),
            0,
            suppressed=False,
            answered=True,
            verdict_id="z" * 500,
        )
        assert len(row["id"]) == judge.MAX_VERDICT_ID_CHARS

    def test_a_verdict_with_no_outcome_folds_to_unknown(self):
        row = judge.verdict_entry(object(), 0, suppressed=False, answered=False)
        assert row["outcome"] == "unknown"

    def test_new_verdict_ids_do_not_repeat(self):
        assert len({judge.new_verdict_id() for _ in range(50)}) == 50


class TestStoredWindow:
    """The stored window is sized by the streak floor, not by the request window."""

    def test_it_holds_a_full_streak_and_the_delivery_that_labels_it(self):
        # A full streak is the floor's worth of suppressions followed by the floor
        # delivery. A store smaller than that evicts the earliest suppressions before
        # their label arrives, and those decision rows never get a `missed`.
        assert judge.MAX_STORED_VERDICTS == _MAX_QUIET_STREAK + 1

    def test_it_is_wider_than_the_window_the_judge_reads(self):
        assert judge.MAX_STORED_VERDICTS > point.MAX_RECENT_VERDICTS

    def test_a_full_default_streak_keeps_every_suppression(self):
        history: list = []
        for index in range(_MAX_QUIET_STREAK - 1):
            history = judge.append_verdict(history, _suppressed(float(index), f"q{index}"))
        history = judge.append_verdict(history, _delivered(99.0, "floor", outcome="quiet"))
        labelled, changed = judge.label_latest_delivery(history, acted=True)
        assert len(labelled) == _MAX_QUIET_STREAK
        assert sum(1 for row in labelled if row.get("missed") is True) == _MAX_QUIET_STREAK - 1
        assert len(changed) == _MAX_QUIET_STREAK

    def test_it_keeps_the_newest_rows_and_drops_the_oldest(self):
        history: list = []
        for index in range(judge.MAX_STORED_VERDICTS + 4):
            history = judge.append_verdict(history, _suppressed(float(index)))
        assert len(history) == judge.MAX_STORED_VERDICTS
        assert [row["at"] for row in history] == [
            float(index) for index in range(4, judge.MAX_STORED_VERDICTS + 4)
        ]

    def test_it_tolerates_a_ruined_history(self):
        assert judge.append_verdict([None, 7, "x"], {"outcome": "wake"}) == [{"outcome": "wake"}]

    def test_it_does_not_mutate_the_list_it_was_given(self):
        original: list = [{"outcome": "quiet"}]
        judge.append_verdict(original, {"outcome": "wake"})
        assert original == [{"outcome": "quiet"}]


class TestConfirmDelivery:
    """Only the fire path may say a turn went out."""

    def test_it_stamps_the_undecided_row(self):
        history = [
            _suppressed(1.0, "q1"),
            judge.verdict_entry(
                _verdict(irq.Outcome.WAKE), 2, suppressed=False, answered=True, verdict_id="d1"
            ),
        ]
        stamped, changed = judge.confirm_delivery(history)
        assert changed is True
        assert stamped[-1]["delivered"] is True
        assert stamped[0].get("delivered") is None

    def test_a_suppressed_newest_row_is_not_a_delivery(self):
        stamped, changed = judge.confirm_delivery([_suppressed(1.0, "q1")])
        assert changed is False
        assert "delivered" not in stamped[0]

    def test_an_already_stamped_row_is_not_stamped_twice(self):
        stamped, changed = judge.confirm_delivery([_delivered(1.0, "d1")])
        assert changed is False

    def test_a_forfeited_row_is_a_permanent_boundary(self):
        stamped, changed = judge.confirm_delivery([_forfeited(1.0, "d1")])
        assert changed is False
        assert stamped[0]["delivered"] is True
        assert stamped[0]["forfeited"] is True

    def test_a_re_owed_delivery_stamps_its_newer_row(self):
        pending = judge.verdict_entry(
            _verdict(irq.Outcome.WAKE), 1, suppressed=False, answered=True, verdict_id="d2"
        )
        stamped, changed = judge.confirm_delivery([_forfeited(1.0, "d1"), pending])
        assert changed is True
        assert stamped[0]["forfeited"] is True
        assert stamped[1]["delivered"] is True

        labelled, labels = judge.label_latest_delivery(stamped, acted=True)
        assert "owner_acted" not in labelled[0]
        assert labelled[1]["owner_acted"] is True
        assert [row["id"] for row in labels] == ["d2"]

    def test_it_does_not_reach_past_a_decided_row_to_an_older_wake(self):
        # An older wake whose fire was lost must not be credited to this delivery.
        history = [
            judge.verdict_entry(
                _verdict(irq.Outcome.WAKE), 1, suppressed=False, answered=True, verdict_id="lost"
            ),
            _suppressed(2.0, "q1"),
        ]
        stamped, changed = judge.confirm_delivery(history)
        assert changed is False
        assert "delivered" not in stamped[0]

    def test_an_empty_history_stamps_nothing(self):
        assert judge.confirm_delivery([]) == ([], False)
        assert judge.confirm_delivery(None) == ([], False)

    def test_it_does_not_mutate_the_history_it_was_given(self):
        original = [
            judge.verdict_entry(
                _verdict(irq.Outcome.WAKE), 1, suppressed=False, answered=True, verdict_id="d1"
            )
        ]
        judge.confirm_delivery(original)
        assert "delivered" not in original[0]


class TestMarkFired:
    """A suppression the loop could not persist is withdrawn, not converted."""

    def test_it_withdraws_the_newest_suppression(self):
        rows = judge.mark_fired([_suppressed(1.0, "q1")])
        assert "suppressed" not in rows[0]
        # Undecided, not delivered: the fire path still has to confirm it.
        assert "delivered" not in rows[0]

    def test_a_withdrawn_row_is_then_stampable(self):
        rows = judge.mark_fired([_suppressed(1.0, "q1")])
        stamped, changed = judge.confirm_delivery(rows)
        assert changed is True
        assert stamped[0]["delivered"] is True

    def test_an_empty_history_does_not_raise(self):
        assert judge.mark_fired([]) == []
        assert judge.mark_fired(None) == []


class TestLabelLatestDelivery:
    """The retroactive pass. A delivery's label is also the verdict on the quiets."""

    def _history(self) -> list:
        # An earlier labelled delivery, three suppressed quiets, then the delivery
        # being judged.
        earlier = _delivered(1.0, "d0")
        earlier["owner_acted"] = True
        return [
            earlier,
            _suppressed(2.0, "q1"),
            _suppressed(3.0, "q2"),
            _suppressed(4.0, "q3"),
            _delivered(5.0, "d1"),
        ]

    def test_an_acting_turn_marks_every_quiet_behind_it_missed(self):
        history, changed = judge.label_latest_delivery(self._history(), acted=True)
        assert history[-1]["owner_acted"] is True
        assert [row["missed"] for row in history[1:4]] == [True, True, True]
        assert {row["id"] for row in changed} == {"d1", "q1", "q2", "q3"}

    def test_an_idle_turn_clears_every_quiet_behind_it(self):
        history, changed = judge.label_latest_delivery(self._history(), acted=False)
        assert history[-1]["owner_acted"] is False
        assert [row["missed"] for row in history[1:4]] == [False, False, False]
        assert len(changed) == 4

    def test_the_pass_stops_at_the_previous_delivery(self):
        history, _ = judge.label_latest_delivery(self._history(), acted=True)
        # The earlier delivery keeps its own label and never acquires a ``missed``.
        assert history[0]["owner_acted"] is True
        assert "missed" not in history[0]

    def test_a_second_turn_complete_relabels_nothing(self):
        once, first = judge.label_latest_delivery(self._history(), acted=True)
        twice, second = judge.label_latest_delivery(once, acted=False)
        assert second == []
        assert twice == once
        assert first != []

    def test_an_unstamped_wake_takes_no_label(self):
        # THE finding: a verdict that decided to wake but whose fire was refused,
        # cancelled or lost is not a delivery, so an unrelated turn completing must
        # not label it.
        history = [
            judge.verdict_entry(
                _verdict(irq.Outcome.WAKE), 2, suppressed=False, answered=True, verdict_id="lost"
            )
        ]
        labelled, changed = judge.label_latest_delivery(history, acted=True)
        assert changed == []
        assert "owner_acted" not in labelled[0]

    def test_an_unstamped_wake_takes_no_missed_either(self):
        # It withheld nothing, so the next delivery's label is not its verdict.
        history = [
            judge.verdict_entry(
                _verdict(irq.Outcome.WAKE), 2, suppressed=False, answered=True, verdict_id="lost"
            ),
            _delivered(3.0, "d1"),
        ]
        labelled, changed = judge.label_latest_delivery(history, acted=True)
        assert "missed" not in labelled[0]
        assert [row["id"] for row in changed] == ["d1"]

    def test_a_verdict_the_judge_never_answered_takes_no_missed(self):
        # A tick that read nothing new returns a quiet without asking the judge. It
        # sat on no evidence, so scoring it as a suppression the judge got wrong
        # would bias the very curve the thresholds are read from.
        history = [
            _suppressed(1.0, "unasked", answered=False),
            _suppressed(2.0, "asked"),
            _delivered(3.0, "d1"),
        ]
        labelled, changed = judge.label_latest_delivery(history, acted=True)
        assert "missed" not in labelled[0]
        assert labelled[1]["missed"] is True
        assert {row["id"] for row in changed} == {"d1", "asked"}

    def test_a_forfeited_delivery_is_never_labelled(self):
        # Its turn belongs to a process that is gone, so no ``owner_acted`` for it can
        # be read truthfully.
        labelled, changed = judge.label_latest_delivery([_forfeited(1.0, "lost")], acted=True)
        assert changed == []
        assert "owner_acted" not in labelled[0]

    def test_a_forfeited_delivery_ends_the_cycle_behind_it(self):
        # A lost delivery is a cycle boundary. The quiets it suppressed belong to ITS
        # cycle, so the label of the delivery after it is not their verdict.
        history = [
            _suppressed(1.0, "q1"),
            _forfeited(2.0, "lost"),
            _delivered(3.0, "d1"),
        ]
        labelled, changed = judge.label_latest_delivery(history, acted=True)
        assert labelled[-1]["owner_acted"] is True
        assert "missed" not in labelled[0]
        assert "owner_acted" not in labelled[1]
        assert [row["id"] for row in changed] == ["d1"]

    def test_a_forfeited_boundary_refuses_every_later_turn(self):
        history = [_suppressed(1.0, "q1"), _suppressed(2.0, "q2"), _forfeited(3.0, "lost")]

        for acted in (True, False, True, False):
            history, stamped = judge.confirm_delivery(history)
            assert stamped is False
            history, changed = judge.label_latest_delivery(history, acted=acted)
            assert changed == []

        assert "owner_acted" not in history[-1]
        assert all("missed" not in row for row in history[:-1])

    def test_a_wake_whose_fire_was_lost_ends_the_cycle_behind_it(self):
        # An unstamped wake is a delivery that was owed and never went out. The quiets
        # before it were suppressed inside ITS cycle, so the label of the delivery that
        # follows it is not their verdict.
        lost = judge.verdict_entry(
            _verdict(irq.Outcome.WAKE), 2, suppressed=False, answered=True, verdict_id="lost"
        )
        history = [_suppressed(1.0, "q1"), lost, _delivered(3.0, "d1")]
        labelled, changed = judge.label_latest_delivery(history, acted=True)
        assert labelled[-1]["owner_acted"] is True
        assert "missed" not in labelled[0]
        assert [row["id"] for row in changed] == ["d1"]

    def test_an_unanswered_delivery_takes_no_owner_acted(self):
        # A tick that returned a verdict without asking the judge spends the turn, but
        # the judge earned nothing by it: labelling it would feed the hit rate a
        # verdict that was never given.
        row = _delivered(2.0, "unasked")
        row["answered"] = False
        labelled, changed = judge.label_latest_delivery([row], acted=True)
        assert changed == []
        assert "owner_acted" not in labelled[0]

    def test_an_unanswered_delivery_also_ends_the_cycle_behind_it(self):
        unasked = _delivered(3.0, "unasked")
        unasked["answered"] = False
        labelled, changed = judge.label_latest_delivery(
            [_suppressed(1.0, "q1"), _delivered(2.0, "d0"), unasked], acted=True
        )
        assert changed == []
        assert "missed" not in labelled[0]
        assert "owner_acted" not in labelled[1]

    def test_a_delivery_with_no_quiets_behind_it_labels_only_itself(self):
        earlier = _delivered(1.0, "a")
        earlier["owner_acted"] = False
        history, changed = judge.label_latest_delivery([earlier, _delivered(2.0, "b")], acted=True)
        assert len(changed) == 1
        assert changed[0]["id"] == "b"
        assert history[0]["owner_acted"] is False

    def test_a_history_with_no_delivery_changes_nothing(self):
        rows = [_suppressed(1.0, "q1")]
        history, changed = judge.label_latest_delivery(rows, acted=True)
        assert changed == []
        assert history == rows

    def test_an_empty_history_changes_nothing(self):
        assert judge.label_latest_delivery([], acted=True) == ([], [])
        assert judge.label_latest_delivery(None, acted=False) == ([], [])

    def test_it_does_not_mutate_the_history_it_was_given(self):
        original = self._history()
        judge.label_latest_delivery(original, acted=True)
        assert "owner_acted" not in original[-1]
        assert "missed" not in original[1]

    def test_a_floor_delivery_is_labelled_like_any_other_delivery(self):
        # A quiet verdict that hit the streak floor SPENDS the turn, so it is judged
        # by what that turn did rather than counted as a suppression.
        history, changed = judge.label_latest_delivery(
            [_delivered(1.0, "floor", outcome="quiet")], acted=True
        )
        assert history[0]["owner_acted"] is True
        assert "missed" not in history[0]
        assert len(changed) == 1


class TestSinceLastWake:
    """The elapsed-time reading."""

    def test_it_measures_from_the_last_delivery(self):
        assert judge.since_last_wake_s(100.0, now_ts=460.0) == 360.0

    def test_a_loop_that_never_delivered_has_no_elapsed_time(self):
        # ``None`` and not 0.0: zero would read as a delivery this instant.
        assert judge.since_last_wake_s(0.0) is None
        assert judge.since_last_wake_s(None) is None

    @pytest.mark.parametrize("bad", [True, False, "60", float("nan"), float("inf"), -5.0])
    def test_an_unusable_stamp_has_no_elapsed_time(self, bad):
        assert judge.since_last_wake_s(bad) is None

    def test_a_clock_that_went_backwards_reads_as_now(self):
        assert judge.since_last_wake_s(500.0, now_ts=100.0) == 0.0


class TestRecentForState:
    """Stored absolute times become ages; nothing else is touched."""

    def test_it_turns_stored_times_into_ages(self):
        rows = judge.recent_for_state([{"outcome": "quiet", "at": 40.0}], now_ts=100.0)
        assert rows[0]["age_s"] == 60.0

    def test_a_row_with_no_time_ages_to_zero(self):
        rows = judge.recent_for_state([{"outcome": "wake"}], now_ts=100.0)
        assert rows[0]["age_s"] == 0.0

    def test_it_skips_rows_that_are_not_mappings(self):
        assert judge.recent_for_state([None, "x", {"outcome": "wake"}], now_ts=1.0) == [
            {"outcome": "wake", "age_s": 0.0}
        ]

    def test_it_does_not_mutate_the_stored_rows(self):
        stored = [{"outcome": "quiet", "at": 40.0}]
        judge.recent_for_state(stored, now_ts=100.0)
        assert "age_s" not in stored[0]


class TestRecentVerdictScreening:
    """What the point will and will not send."""

    def test_it_carries_the_outcome_the_age_the_count_and_one_label(self):
        item = point.recent_verdict_item(
            {
                "outcome": "quiet",
                "age_s": 30.0,
                "evidence_items": 4,
                "missed": True,
                "id": "q1",
            }
        )
        assert item == {"outcome": "quiet", "age_s": 30.0, "evidence_items": 4, "missed": True}

    def test_it_keeps_the_field_last_verdict_carried_for_meaning(self):
        # ``evidence_items`` is what lets a judge tell "still nothing" from "the same
        # thing again", so the history that supersedes ``last_verdict`` must carry it.
        item = point.recent_verdict_item({"outcome": "quiet", "age_s": 1.0, "evidence_items": 7})
        assert item["evidence_items"] == 7

    def test_a_missing_count_is_simply_absent(self):
        item = point.recent_verdict_item({"outcome": "quiet", "age_s": 1.0})
        assert "evidence_items" not in item

    @pytest.mark.parametrize("bad", [True, -1, 1.5, "4", None])
    def test_an_unusable_count_is_dropped(self, bad):
        item = point.recent_verdict_item({"outcome": "quiet", "age_s": 1.0, "evidence_items": bad})
        assert "evidence_items" not in item

    def test_the_join_key_never_reaches_the_wire(self):
        item = point.recent_verdict_item({"outcome": "wake", "age_s": 1.0, "id": "secret-key"})
        assert "id" not in item

    def test_bookkeeping_fields_never_reach_the_wire(self):
        item = point.recent_verdict_item(
            {
                "outcome": "wake",
                "age_s": 1.0,
                "delivered": True,
                "suppressed": False,
                "answered": True,
                "at": 1700000000.0,
                "target": "chat-1",
            }
        )
        assert set(item) == {"outcome", "age_s"}

    def test_a_row_naming_no_outcome_is_dropped(self):
        assert point.recent_verdict_item({"age_s": 1.0}) is None
        assert point.recent_verdict_item({"outcome": "", "age_s": 1.0}) is None
        assert point.recent_verdict_item({"outcome": 7}) is None
        assert point.recent_verdict_item(None) is None

    def test_only_one_label_survives_a_row_claiming_both(self):
        item = point.recent_verdict_item(
            {"outcome": "quiet", "age_s": 1.0, "owner_acted": True, "missed": False}
        )
        assert len([key for key in item if key in ("owner_acted", "missed")]) == 1

    def test_a_non_boolean_label_is_dropped(self):
        item = point.recent_verdict_item({"outcome": "quiet", "age_s": 1.0, "missed": "yes"})
        assert set(item) == {"outcome", "age_s"}

    def test_an_unlabelled_row_carries_neither_label(self):
        item = point.recent_verdict_item({"outcome": "wake", "age_s": 2.0})
        assert set(item) == {"outcome", "age_s"}

    def test_an_oversized_outcome_name_is_clipped(self):
        item = point.recent_verdict_item({"outcome": "q" * 400, "age_s": 0.0})
        assert len(item["outcome"]) == 32

    def test_the_window_keeps_the_newest_rows(self):
        rows = [
            {"outcome": "quiet", "age_s": float(age)}
            for age in range(point.MAX_RECENT_VERDICTS + 5)
        ]
        screened = point.screen_recent_verdicts(rows)
        assert len(screened) == point.MAX_RECENT_VERDICTS
        assert [row["age_s"] for row in screened] == [
            float(age) for age in range(point.MAX_RECENT_VERDICTS)
        ]

    def test_it_orders_newest_first(self):
        screened = point.screen_recent_verdicts(
            [{"outcome": "a", "age_s": 90.0}, {"outcome": "b", "age_s": 5.0}]
        )
        assert [row["outcome"] for row in screened] == ["b", "a"]


class TestBuildStateFeedbackFields:
    """The two new loop fields, and the history superseding the single verdict."""

    def test_the_elapsed_fields_ride_on_the_loop_object(self):
        state = point.build_state(
            "watch it",
            evidence=[{"source": "s", "kind": point.KIND_TRANSCRIPT_TAIL, "age_s": 1, "text": "x"}],
            since_last_wake_s=420.0,
            quiet_streak=3,
        )
        assert state["loop"]["since_last_wake_s"] == 420.0
        assert state["loop"]["quiet_streak"] == 3

    def test_the_instruction_is_still_the_first_loop_key(self):
        # Additive, so a reader of the existing shape sees no reordering.
        state = point.build_state("watch it", since_last_wake_s=1.0, quiet_streak=0)
        assert list(state["loop"])[0] == "instruction"

    @pytest.mark.parametrize("bad", [None, -1.0, float("nan"), float("inf"), True, "60"])
    def test_an_unusable_elapsed_reading_is_omitted(self, bad):
        state = point.build_state("watch it", since_last_wake_s=bad)
        assert "since_last_wake_s" not in state["loop"]

    @pytest.mark.parametrize("bad", [None, -1, True, 1.5, "3"])
    def test_an_unusable_streak_is_omitted(self, bad):
        state = point.build_state("watch it", quiet_streak=bad)
        assert "quiet_streak" not in state["loop"]

    def test_a_zero_streak_is_sent_because_zero_is_a_reading(self):
        state = point.build_state("watch it", quiet_streak=0)
        assert state["loop"]["quiet_streak"] == 0

    def test_the_history_supersedes_the_single_verdict(self):
        state = point.build_state(
            "watch it",
            last_verdict={"outcome": "quiet", "evidence_items": 1, "at": 1.0},
            recent_verdicts=[
                {"outcome": "quiet", "age_s": 30.0, "evidence_items": 1, "missed": False}
            ],
        )
        assert "last_verdict" not in state
        assert state["recent_verdicts"] == [
            {"outcome": "quiet", "age_s": 30.0, "evidence_items": 1, "missed": False}
        ]

    def test_superseding_loses_nothing_last_verdict_carried(self):
        # Both fields ``last_verdict`` holds for meaning survive in the newest row, so
        # the supersede is a widening rather than a trade.
        single = {"outcome": "quiet", "evidence_items": 3, "at": 100.0}
        state = point.build_state(
            "watch it",
            last_verdict=single,
            recent_verdicts=judge.recent_for_state([dict(single)], now_ts=160.0),
        )
        newest = state["recent_verdicts"][0]
        assert newest["outcome"] == single["outcome"]
        assert newest["evidence_items"] == single["evidence_items"]
        assert newest["age_s"] == 60.0

    def test_the_single_verdict_still_goes_out_with_no_history(self):
        state = point.build_state(
            "watch it",
            last_verdict={"outcome": "quiet", "evidence_items": 1, "at": 1.0},
        )
        assert state["last_verdict"]["outcome"] == "quiet"
        assert "recent_verdicts" not in state

    def test_the_history_survives_the_char_budget(self, monkeypatch):
        # Evidence is what the budget spends; the history is what the judge needs to
        # read its own hit rate, and a history that thinned out on a busy tick would
        # look like a better rate than the loop earned.
        monkeypatch.setattr(point, "MAX_STATE_CHARS", 900)
        fat = [
            {
                "source": f"session:chat-{index}",
                "kind": point.KIND_TRANSCRIPT_TAIL,
                "age_s": float(index),
                "text": "y" * 400,
            }
            for index in range(6)
        ]
        trace: dict = {}
        state = point.build_state(
            "watch it",
            evidence=fat,
            recent_verdicts=[
                {"outcome": "quiet", "age_s": float(age), "missed": False} for age in range(5)
            ],
            trace=trace,
        )
        assert len(state["recent_verdicts"]) == 5
        assert trace["dropped"] > 0
        assert len(json.dumps(state, ensure_ascii=False)) <= 900

    def test_the_trace_reports_how_much_history_was_sent(self):
        trace: dict = {}
        point.build_state(
            "watch it",
            recent_verdicts=[{"outcome": "quiet", "age_s": 1.0}],
            trace=trace,
        )
        assert trace["recent_verdicts"] == 1

    def test_persisted_absurd_counts_are_clamped_without_dropping_rows(self, tmp_path):
        huge = 10**3999
        stored_rows = [
            {
                "outcome": "quiet",
                "at": float(index),
                "evidence_items": huge,
                "suppressed": True,
                "answered": True,
                "missed": False,
                "id": f"q{index}",
            }
            for index in range(point.MAX_RECENT_VERDICTS)
        ]
        payload = {
            "loops": [
                {
                    "id": "loop-1",
                    "slot_key": "chat-1",
                    "message": "watch it",
                    "judge_quiet_streak": huge,
                    "judge_recent_verdicts": stored_rows,
                }
            ]
        }
        (tmp_path / "autonudge.json").write_text(json.dumps(payload), encoding="utf-8")

        service = AutoNudgeService(base_dir=tmp_path)
        try:
            service._load()
            loaded = service._loops["loop-1"]
        finally:
            service.stop()

        assert loaded.judge_quiet_streak == _MAX_QUIET_STREAK
        assert len(loaded.judge_recent_verdicts) == point.MAX_RECENT_VERDICTS
        assert {row["evidence_items"] for row in loaded.judge_recent_verdicts} == {
            point.MAX_EVIDENCE_ITEMS
        }

        direct = point.recent_verdict_item(
            {"outcome": "quiet", "age_s": 1.0, "evidence_items": huge}
        )
        assert direct["evidence_items"] == point.MAX_EVIDENCE_ITEMS
        assert (
            point.build_state("watch it", quiet_streak=huge)["loop"]["quiet_streak"]
            == _MAX_QUIET_STREAK
        )

        state = point.build_state(
            loaded.message,
            recent_verdicts=judge.recent_for_state(
                loaded.judge_recent_verdicts,
                now_ts=100.0,
            ),
            quiet_streak=loaded.judge_quiet_streak,
        )
        assert len(state["recent_verdicts"]) == point.MAX_RECENT_VERDICTS
        assert len(json.dumps(state, ensure_ascii=False)) <= point.MAX_STATE_CHARS

    def test_a_persisted_negative_count_is_floored_not_retained(self, tmp_path):
        # A ceiling alone is half a bound: on the wire a 4,000-digit negative costs
        # exactly what the positive does, and this store is agent-writable. A count
        # below zero is not a count, so the floor reads it as none rather than keeping
        # the digits. The row itself survives -- one crafted field must not erase it.
        huge_negative = -(10**3999)
        stored_rows = [
            {
                "outcome": "quiet",
                "at": 1.0,
                "evidence_items": huge_negative,
                "suppressed": True,
                "answered": True,
                "id": "q1",
            }
        ]
        payload = {
            "loops": [
                {
                    "id": "loop-1",
                    "slot_key": "chat-1",
                    "message": "watch it",
                    "judge_quiet_streak": huge_negative,
                    "judge_recent_verdicts": stored_rows,
                }
            ]
        }
        (tmp_path / "autonudge.json").write_text(json.dumps(payload), encoding="utf-8")

        service = AutoNudgeService(base_dir=tmp_path)
        try:
            service._load()
            loaded = service._loops["loop-1"]
        finally:
            service.stop()

        assert loaded.judge_quiet_streak == 0
        assert len(loaded.judge_recent_verdicts) == 1
        assert loaded.judge_recent_verdicts[0]["evidence_items"] == 0
        assert loaded.judge_recent_verdicts[0]["outcome"] == "quiet"
