"""Click-time validation of a posted Slack OPTIONS control.

The rule under test: a control is answerable while the conversation that asked
the question has not moved on, and refused once it has -- judged entirely from a
token in the Slack message plus the transcript on disk.

The reason it is judged that way rather than from a counter held in memory is the
restart. A gateway that dies forgets which controls were live, so a design that
enforced validity by editing messages ahead of time could never retire the
buttons left in Slack, and every one of them stayed answerable forever. These
tests pin the restart case first, because that is the case the previous design
could not express.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_utils import (
    mint_options_token,
    options_control_is_stale,
)
from kiro_crew.history import ConversationLog
from kiro_crew.llm_helpers import save_conversation_turn_off_loop
from kiro_crew.slack.format import build_options_blocks
from kiro_crew.slack.outbound import decode_options_token, encode_options_token

THREAD = "1786230097.467939"
OWNER = f"slack:{THREAD}"


def _state(tmp_path):
    """A dashboard state whose transcripts live under *tmp_path*.

    Deliberately built fresh per call: constructing a second one over the SAME
    directory is how these tests model a restart -- new process memory, same files
    on disk.
    """
    state = _make_state(tmp_path)
    state.conversation_log = ConversationLog(base_dir=tmp_path)
    return state


def _turn(state, text="hello"):
    """Write one user row, i.e. advance the conversation by a turn."""
    state.conversation_log.append(OWNER, "user", text)


class TestSurvivesARestart:
    """The behaviour the previous design could not express."""

    @pytest.mark.asyncio
    async def test_a_control_posted_before_a_restart_is_refused_after_one(self, tmp_path):
        """The whole point: a restart must not resurrect a superseded control.

        A token minted before the restart is compared against the transcript
        after it. Because the token and the comparand are both persisted -- one in
        the Slack message, one in the JSONL file -- neither is lost with the
        process, so the answer is the same as it would have been without the
        restart.
        """
        state = _state(tmp_path)
        _turn(state, "the question's turn")
        token = mint_options_token(state, OWNER)
        assert token, "a live conversation must yield a token"

        # The conversation moves on, then the gateway dies and comes back.
        _turn(state, "a later turn that supersedes the question")
        restarted = _state(tmp_path)

        assert await options_control_is_stale(restarted, token, THREAD) is True

    @pytest.mark.asyncio
    async def test_a_restart_alone_does_not_invalidate_a_live_control(self, tmp_path):
        """Restarting is not the same as answering.

        Nothing has been said since the question, so the question is still live
        and its buttons must keep working. This is the half that stops the fix
        from becoming "refuse everything after a restart", which would be just as
        broken from the user's side.
        """
        state = _state(tmp_path)
        _turn(state, "the question's turn")
        token = mint_options_token(state, OWNER)

        restarted = _state(tmp_path)

        assert await options_control_is_stale(restarted, token, THREAD) is False

    def test_the_counter_the_old_design_compared_against_moves_backwards(self, tmp_path):
        """Why the token carries a transcript ts and not a turn counter.

        ``_ChatSlot.total_messages`` counts what a process has appended, so a
        fresh process counts from zero regardless of how long the transcript is.
        A pre-restart token of N therefore reads as "not yet reached" against a
        post-restart counter, and the stale click it was meant to catch is let
        through. Pinning it here so the reason survives the next reader.
        """
        state = _state(tmp_path)
        slot = state.get_or_create_slot("s1")
        for i in range(5):
            slot.append("user", f"m{i}", "msg msg-u")
        before = slot.total_messages
        assert before == 5

        restarted = _state(tmp_path)
        after = restarted.get_or_create_slot("s1").total_messages

        assert after < before, "a fresh process must not inherit the old count"


class TestTheTurnIsDurableBeforeItsControlGoesOut:
    """The token names a POSITION, so the turn must be written before it is minted.

    Asserted on source call-order rather than end-to-end: reaching the native
    Slack footer path in a test requires standing up the whole turn pipeline, and
    the property at stake is purely an ordering one. Stated plainly here so the
    next reader knows this is a structural assertion, not a behavioural one.
    """

    def test_a_subagent_turn_is_persisted_for_every_transport(self):
        """The persist must not sit inside the Slack-only branch.

        Moving it there to satisfy persist-before-mint silently dropped every
        Discord/Telegram subagent completion from conversation history: a channel
        parent delivers through the transport ladder, which skips the
        ``not _via_transport and self.slack`` branch entirely, so the turn was
        never written and vanished from dashboard replay.

        Asserted on indentation because that is precisely what went wrong -- the
        block was correct, its scope was not.
        """
        import inspect

        from kiro_crew.slack import gateway

        src = inspect.getsource(gateway)
        persist = src.find("await save_conversation_turn_off_loop(\n", src.find("subagent"))
        assert persist != -1, "the subagent turn must still be persisted"

        # Walk back to the `if self.conv_log` that guards it and measure its indent.
        guard = src.rfind("if self.conv_log", 0, persist)
        assert guard != -1, "the persist must stay guarded on conv_log"
        line_start = src.rfind("\n", 0, guard) + 1
        indent = guard - line_start
        assert indent <= 24, (
            f"the persist guard is indented {indent} spaces; deeper than the "
            "per-attempt level means it sits inside the Slack-only branch and "
            "non-Slack subagent completions are never written"
        )

    def test_a_root_thread_control_is_tokened_too(self):
        """Gating the mint on ``thread_ts`` left top-level controls unprotected.

        A message posted at the top of a channel has no ``thread_ts``, so gating on
        it meant every root-thread control went out untokened — no staleness check
        at all, on exactly the path a restart strands. The gate belongs on whether
        there are options, and the asker is named explicitly rather than resolved
        from the thread, which at mint time could name a session that never asked.
        """
        import inspect

        from kiro_crew.slack import handler

        src = inspect.getsource(handler.handle_message)
        assert "if options and thread_ts" not in src, (
            "gating the token mint on thread_ts strands every root-thread control"
        )
        mint = src.find("mint_options_token")
        assert mint != -1, "the footer must still mint a token"
        window = src[mint : mint + 220]
        assert "session_key" in window, (
            "the asker must be named explicitly; resolving it from the thread names "
            "whoever owns it at mint time, not the session that asked"
        )

    def test_history_is_persisted_before_the_token_is_minted(self):
        """Mint-before-persist refuses every legitimate click.

        The token records this session's last persisted transcript row. Posting a
        control before the turn's own two rows land stamps it with the PREVIOUS
        turn's position, so those rows arriving immediately afterwards read as
        "the conversation moved on" and the first click on a brand-new control is
        rejected as stale.
        """
        import inspect

        from kiro_crew.slack import handler

        src = inspect.getsource(handler.handle_message)
        persist = src.find("save_conversation_turn_off_loop(")
        mint = src.find("mint_options_token")
        assert persist != -1, "the turn must still be persisted in handle_message"
        assert mint != -1, "the footer must still mint a staleness token"
        assert persist < mint, (
            "the turn's rows must be on disk BEFORE the control that invites an "
            "answer to it is stamped, or the stamp names the previous turn"
        )

    @pytest.mark.asyncio
    async def test_the_stamp_names_this_turn_not_whatever_landed_next(self, tmp_path):
        """A second turn arriving in the gap must not make this control look live.

        The session permit is released before the turn is persisted, so a queued
        second message can write its own rows in between. Reading the tail back at
        that point returns the SECOND turn's position, which would stamp this
        control -- by then superseded -- as being at the front of the conversation,
        and a click on it would be accepted. Taking the position from the row this
        turn itself wrote is what makes that unreachable.
        """
        state = _state(tmp_path)
        log = state.conversation_log

        row_ts = await save_conversation_turn_off_loop(log, OWNER, "the question", "asked")
        assert row_ts, "the persist must hand back the row it wrote"
        assert row_ts == log.last_row_ts(OWNER), "and it must be this turn's own row"

        token = mint_options_token(state, OWNER, row_ts)

        # The queued second turn lands.
        await save_conversation_turn_off_loop(log, OWNER, "next question", "answered")

        assert await options_control_is_stale(state, token, THREAD) is True, (
            "the first control is superseded and its clicks must be refused"
        )
        # Contrast, and the reason the fix is not cosmetic: minting by re-reading
        # the tail HERE names the second turn, and the same superseded control
        # then reads as live.
        from_tail = mint_options_token(state, OWNER)
        assert await options_control_is_stale(state, from_tail, THREAD) is False
        assert from_tail != token


class TestTheAnswerGoesToTheConversationThatAsked:
    """An accepted click carries its destination, read from its own token.

    Source-level assertions: the pin is a local inside the two Slack click
    handlers, and reaching them end-to-end needs the whole interaction pipeline
    stood up. The property at stake is which resolver feeds the pin, which the
    source states directly.
    """

    def test_both_click_paths_pin_the_asker_not_the_current_thread_owner(self):
        """Resolving from the thread names the NEW owner after a handover.

        That is the one case the pin exists to survive, so pinning
        ``slack_options_linked_slot(thread_ts)`` would deliver the answer to a
        conversation that never asked the question. The token already names the
        asker; the pin must come from there.
        """
        import inspect

        from kiro_crew.slack import interactions

        src = inspect.getsource(interactions)
        assert src.count("_asker = decode_options_token(") == 2, (
            "both click paths must read the asker from the control's own token"
        )
        assert "slack_options_linked_slot" not in src, (
            "pinning the thread's CURRENT owner reintroduces the handover bug"
        )
        assert src.count("route_pinned=_route_pinned") == 2, (
            "both dispatches must pass the pin through to routing"
        )

    def test_an_untokened_control_pins_nothing(self):
        """No token means no claim about the destination, so keep today's routing.

        Refusing instead would strand every control posted before this shipped.
        """
        import inspect

        from kiro_crew.slack import interactions

        src = inspect.getsource(interactions)
        assert src.count("_route_pinned = _asker_key is not None") == 2, (
            "the pin must be conditional on a token being present"
        )

    def test_a_pinned_answer_never_claims_the_thread(self):
        """Routing the answer must not rewrite who OWNS the thread.

        A cron or native asker holds no dashboard slot, so a pinned answer runs
        past the linked-thread intercept and reaches the self-link. Claiming the
        thread there evicts its real owner and sends every later human reply into
        the cron conversation -- the same class of misrouting the pin exists to
        prevent, in the opposite direction.

        Structural rather than end-to-end: the gate sits ~50 lines inside
        ``handle_message``'s try block, past ``sessions.get_or_create``, so
        reaching it needs the whole turn pipeline stood up. What broke was the
        GATE, so the gate is what this pins.
        """
        import inspect

        from kiro_crew.slack import handler

        src = inspect.getsource(handler)
        assert "if thread_owner_key is None and not route_pinned:" in src, (
            "a pinned answer must never claim the thread, however empty the index"
        )
        # The old spelling steered the claim off a variable the pin path
        # deliberately falsified to None, which is why the claim fired.
        assert "if not linked_session_key:\n" not in src, (
            "the claim must not read the falsified mirror value"
        )


class TestTheRuleItself:
    @pytest.mark.asyncio
    async def test_a_control_is_live_until_the_conversation_moves_on(self, tmp_path):
        state = _state(tmp_path)
        _turn(state)
        token = mint_options_token(state, OWNER)

        assert await options_control_is_stale(state, token, THREAD) is False

        _turn(state, "next turn")

        assert await options_control_is_stale(state, token, THREAD) is True

    @pytest.mark.asyncio
    async def test_a_thread_changing_hands_does_not_refuse_a_pending_question(self, tmp_path):
        """Handover is not supersession.

        A click carries its destination, so an accepted answer reaches the
        conversation that asked it whatever the thread's ownership has since done.
        Refusing on a handover would therefore reject a click the user was
        legitimately shown, while protecting nothing. Only the asker's own
        transcript advancing makes its question stale.
        """
        state = _state(tmp_path)
        _turn(state)
        token = mint_options_token(state, OWNER)
        foreign = encode_options_token("slack:9999999999.000000", "2026-01-01T00:00:00+00:00")

        assert await options_control_is_stale(state, token, THREAD) is False
        # A token naming a conversation with no transcript cannot be judged, so it
        # abstains rather than refusing.
        assert await options_control_is_stale(state, foreign, THREAD) is False


class TestAbstainsRatherThanRefuses:
    """Every input it cannot read must honour the click, not reject it.

    Refusing a legitimate answer is the worse failure: the user clicked the
    button they were shown and nothing happened. So an unreadable token, an
    absent one, or an unreadable transcript all mean "cannot prove staleness".
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "block_id",
        [
            None,
            "",
            "some_unrelated_block_id",
            "kcopt1:truncated",
            "kcopt1:!!!:!!!",
        ],
    )
    async def test_an_unusable_token_honours_the_click(self, tmp_path, block_id):
        state = _state(tmp_path)
        _turn(state)

        assert await options_control_is_stale(state, block_id, THREAD) is False

    @pytest.mark.asyncio
    async def test_a_control_posted_by_an_older_build_carries_no_token(self, tmp_path):
        """An upgrade must not strand buttons that are still answerable.

        Controls already sitting in Slack when this shipped have no ``block_id``,
        so they read as unprovable and keep working.
        """
        state = _state(tmp_path)
        _turn(state)
        legacy_blocks = build_options_blocks(["a", "b"])

        assert "block_id" not in legacy_blocks[0]
        assert await options_control_is_stale(state, None, THREAD) is False

    @pytest.mark.asyncio
    async def test_an_unreadable_transcript_honours_the_click(self, tmp_path):
        state = _state(tmp_path)
        _turn(state)
        token = mint_options_token(state, OWNER)
        broken = _state(tmp_path)
        broken.conversation_log = MagicMock()
        broken.conversation_log.last_row_ts.side_effect = OSError("disk gone")

        assert await options_control_is_stale(broken, token, THREAD) is False

    def test_a_conversation_with_no_transcript_mints_nothing(self, tmp_path):
        """Nothing to compare against yet, so post untokened rather than guess."""
        assert mint_options_token(_state(tmp_path), OWNER) is None


class TestTheTokenRidesInTheMessage:
    def test_the_token_is_carried_on_the_block_id_slack_echoes_back(self):
        blocks = build_options_blocks(["yes", "no"], staleness_token="kcopt1:aGk:dGhlcmU")

        assert blocks[0]["block_id"] == "kcopt1:aGk:dGhlcmU"

    def test_a_colon_shaped_key_and_an_offset_timestamp_survive_intact(self):
        """Both halves are colon-bearing, so neither may be split on naively."""
        key, ts = "cron:job-7", "2026-08-11T04:37:08.296000+00:00"

        assert decode_options_token(encode_options_token(key, ts)) == (key, ts)

    def test_a_token_too_long_for_slack_is_not_emitted(self):
        """Slack rejects a block_id over 255 chars; emit nothing and abstain."""
        assert encode_options_token("dashboard:" + "x" * 300, "2026-08-11T04:37:08+00:00") is None
