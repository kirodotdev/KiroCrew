"""Coverage tests for ``kiro_crew.dashboard.chat_runner``: the pure trust checks.

Covers the PreToolUse hook verdicts, the trust and auto-approve predicates (scoped
grants, reason precedence, what a session policy may persist, native crew) and the
command parsing behind trusted patterns. The ``_run_chat`` rungs that use them are in
``test_chat_runner_coverage_approval.py``.
``test_chat_runner_coverage.py`` describes the two harnesses the family uses; the
shared builders are in ``chat_runner_coverage_helpers.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from chat_runner_coverage_helpers import _slot, _state

from kiro_crew.dashboard import chat_runner

# ── PreToolUse hook verdicts ──────────────────────────────────────────────


class TestPreToolHookVerdicts:
    @pytest.mark.parametrize(
        "results",
        [None, "BLOCKED:h:no", {"blocked": True}, 7],
    )
    def test_non_list_output_is_denied(self, results):
        """Deny-by-default: anything but a list of strings blocks the tool."""
        assert chat_runner._pre_tool_hooks_should_block(results) is True

    def test_empty_list_is_the_pass_through_contract(self):
        assert chat_runner._pre_tool_hooks_should_block([]) is False

    def test_non_string_member_blocks(self):
        assert chat_runner._pre_tool_hooks_should_block(["ok", 3]) is True

    def test_block_reason_prefers_the_hook_text(self):
        assert (
            chat_runner._pre_tool_block_reason(["BLOCKED:policy: not allowed here "])
            == "not allowed here"
        )

    @pytest.mark.parametrize(
        "results",
        [
            ["BLOCKED:policy:"],  # marker present, reason empty
            ["BLOCKED:policy"],  # marker truncated, no reason field
            [],  # nothing blocked
            "not-a-list",
        ],
    )
    def test_block_reason_falls_back_when_no_reason_is_authored(self, results):
        assert chat_runner._pre_tool_block_reason(results) == "blocked by a PreToolUse policy hook"


# ── trust / auto-approve predicates ───────────────────────────────────────


class TestTrustPredicates:
    def test_scope_grant_is_rechecked_on_every_call(self):
        slot = _slot()
        slot._trust_scope = "scope-1"

        with patch.object(chat_runner, "safety_override") as override:
            override.return_value.is_scope_active.return_value = True
            assert chat_runner._slot_is_trusted(slot) is True
            override.return_value.is_scope_active.return_value = False
            assert chat_runner._slot_is_trusted(slot) is False

    def test_no_grant_at_all_is_untrusted(self):
        assert chat_runner._slot_is_trusted(_slot()) is False

    @pytest.mark.parametrize(
        "trust,scope,yolo,expected",
        [
            (False, "", True, "yolo"),
            (True, "", False, "trust"),
            (False, "s-1", False, "trust_scope"),
            (False, "", False, "trust"),
        ],
    )
    def test_auto_approve_reason_precedence(self, trust, scope, yolo, expected):
        slot = _slot()
        slot._trust = trust
        slot._trust_scope = scope

        assert chat_runner._auto_approve_reason(slot, yolo) == expected

    def test_scoped_grant_is_never_persisted_as_a_session_policy(self):
        """A lapsing grant must not be cached where nothing re-checks it."""
        slot = _slot()
        slot._trust_scope = "scope-1"

        assert chat_runner._persistable_session_policy(slot, False) == ""

    @pytest.mark.parametrize("yolo,trust", [(True, False), (False, True)])
    def test_non_lapsing_grants_are_persistable(self, yolo, trust):
        slot = _slot()
        slot._trust = trust

        assert chat_runner._persistable_session_policy(slot, yolo) == "auto"

    def test_native_crew_auto_approve_requires_an_active_crew(self, tmp_path):
        state = _state(tmp_path)
        state.is_yolo_active = MagicMock(return_value=True)
        slot = _slot()
        slot._trust = True

        assert chat_runner._native_crew_should_auto_approve({}, state, slot) is False
        assert (
            chat_runner._native_crew_should_auto_approve({"s1": {"done": True}}, state, slot)
            is False
        )
        assert (
            chat_runner._native_crew_should_auto_approve({"s1": {"done": False}}, state, slot)
            is True
        )

    def test_active_crew_without_any_grant_is_still_denied(self, tmp_path):
        state = _state(tmp_path)
        state.is_yolo_active = MagicMock(return_value=False)
        state.context_builder = None

        assert (
            chat_runner._native_crew_should_auto_approve({"s1": {"done": False}}, state, _slot())
            is False
        )


# ── command parsing helpers ───────────────────────────────────────────────


class TestCommandParsing:
    def test_quoted_separators_are_masked_and_restored(self):
        masked, restore = chat_runner._mask_quoted_separators('grep "a|b" f && wc -l')

        assert "|" not in masked.split("&&")[0].replace("&&", "")
        assert list(restore.values()) == ["|"]

    def test_command_substitution_is_denied_by_default(self):
        assert chat_runner._matches_trusted_pattern("echo $(whoami)", {"echo*"}) is None

    def test_every_segment_must_match_for_a_chained_command(self):
        assert chat_runner._matches_trusted_pattern("ls | rm -rf x", {"ls*"}) is None

    def test_all_matching_segments_return_joined_patterns(self):
        matched = chat_runner._matches_trusted_pattern("ls | wc -l", {"ls*", "wc*"})

        assert matched is not None
        assert matched.count(",") == 1

    def test_redirect_forms_are_not_treated_as_separators(self):
        assert chat_runner._matches_trusted_pattern("ls 2>&1", {"ls*"}) is not None

    def test_an_escaped_quote_does_not_hide_a_real_separator(self):
        """A closing quote followed by `\\'` leaves quoted context, so the `;`
        after it is a separator the shell acts on.

        Reading that `\\'` as an OPENING quote makes the remainder look quoted, the
        separator gets masked, the line collapses to one segment, and an appended
        command inherits whatever the first segment was allowed to do. Verified
        against a real shell: `echo 'foo'\\'; cmd` runs `cmd`.
        """
        command = "echo 'foo'\\'; whoami"
        _, segments = chat_runner._split_command_segments(command) or ("", [])

        assert len(segments) == 2, segments
        assert chat_runner._matches_trusted_pattern(command, {"echo*"}) is None

    def test_an_escaped_double_quote_keeps_the_quote_open(self):
        """Inside double quotes `\\"` is an escaped literal, so the quote stays
        OPEN and the `;` after it really is quoted. One segment is the correct
        reading: the line is an unterminated quote, which a shell refuses to run
        at all rather than executing a second command, so there is nothing here
        for segmentation to protect against."""
        command = 'echo "foo\\"; whoami'
        _, segments = chat_runner._split_command_segments(command) or ("", [])

        assert len(segments) == 1, segments

    def test_an_escaped_separator_outside_quotes_still_segments(self):
        """`\\;` is an escaped literal to the shell, not a separator -- but the
        allowlist must not approve the tail either way, so segmentation stays
        fail-closed rather than trying to model every escape."""
        assert chat_runner._matches_trusted_pattern("ls \\; whoami", {"ls*"}) is None

    def test_a_backslash_inside_single_quotes_stays_literal(self):
        """The shell does not honor escapes inside single quotes, so a trailing
        backslash there must not swallow the closing quote."""
        masked, restore = chat_runner._mask_quoted_separators("echo 'a|b\\' && wc -l")

        assert list(restore.values()) == ["|"]
        assert masked.endswith("&& wc -l")

    def test_base_command_extraction_dedups_across_segments(self):
        assert chat_runner._extract_base_command("cat a | wc -l | cat b") == "cat,wc"

    def test_full_command_preserves_canonical_text(self):
        assert chat_runner._extract_full_command("Reading /usr/bin/id") == "Reading /usr/bin/id"
