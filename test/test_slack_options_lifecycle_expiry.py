"""OPTIONS expiry on the Slack surface: primitives, slot lifecycle and turn entries.

This file pins ``expire_options`` spending a control the conversation has moved past,
``replace_options_blocks``, the block surgery the click and expiry paths share, the
remember / expire / forget lifecycle against a real slot registry, and the expiry
firing on a new turn from either surface. The two behaviours this family locks in are
described in ``test_slack_options_lifecycle.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state, drain_background_tasks
from slack_options_lifecycle_helpers import (
    _context_text,
    _is_live_control,
    _recs,
    _slack_app,
)
from test_handler_turn import TURN_ENTRY_FUNCTIONS

from kiro_crew.slack.format import (
    OPTIONS_CHECKBOXES_ACTION,
    OPTIONS_SUBMIT_ACTION,
    build_options_blocks,
    replace_options_blocks,
)
from kiro_crew.slack.outbound import PostedOptions, expire_options


def _slack() -> MagicMock:
    slack = MagicMock()
    slack.post_message = AsyncMock(return_value="body_ts")
    slack.post_blocks = AsyncMock(return_value="opt_ts")
    slack.update_message = AsyncMock()
    return slack


def _stub_non_turn_paths(monkeypatch, module) -> None:
    """Let a plain message walk past the answer-and-return short-circuits.

    The OPTIONS expiry deliberately sits BELOW every path that replies without
    running the agent, so a test that wants to observe the expiry has to drive a
    message that is not a command and get it past the modifier and keyword-command
    gates. Both take a MagicMock ``sessions`` otherwise.
    """

    async def _no_modifiers(text, cmd_text, *_a, **_k):
        return text, cmd_text, False

    async def _no_keyword(*_a, **_k):
        return False

    monkeypatch.setattr(module, "maybe_apply_privacy_modifiers", _no_modifiers)
    monkeypatch.setattr(module, "maybe_handle_keyword_command", _no_keyword)


class TestExpireOptions:
    """Spending a control the conversation has moved past."""

    @pytest.mark.asyncio
    async def test_every_choice_is_struck_through(self):
        slack = _slack()
        blocks = build_options_blocks(["A", "B"])
        posted = PostedOptions(channel="C1", ts="opt_ts", choices=("A", "B"), blocks=tuple(blocks))

        await expire_options(slack, posted)

        blocks = slack.update_message.await_args.kwargs["blocks"]
        assert not _is_live_control(blocks)
        assert _context_text(blocks) == "~A~  |  ~B~"
        assert slack.update_message.await_args.args == ("C1", "opt_ts")

    @pytest.mark.asyncio
    async def test_surrounding_blocks_survive(self):
        """A turn's timing footer shares the message with its control."""
        slack = _slack()
        footer = {"type": "section", "text": {"type": "mrkdwn", "text": "12.3s"}}
        control = {
            "type": "actions",
            "elements": [
                {"type": "checkboxes", "action_id": OPTIONS_CHECKBOXES_ACTION},
                {"type": "button", "action_id": OPTIONS_SUBMIT_ACTION},
            ],
        }
        posted = PostedOptions(
            channel="C1",
            ts="opt_ts",
            choices=("A", "B"),
            blocks=(footer, control),
            text="12.3s",
        )

        await expire_options(slack, posted)

        blocks = slack.update_message.await_args.kwargs["blocks"]
        assert footer in blocks
        assert control not in blocks
        assert not _is_live_control(blocks)

    @pytest.mark.asyncio
    async def test_slack_failure_is_swallowed(self):
        """A thread keeping a live control is the status quo, not a new failure."""
        slack = _slack()
        slack.update_message = AsyncMock(side_effect=Exception("channel_not_found"))
        posted = PostedOptions(
            channel="C1", ts="opt_ts", choices=("A",), blocks=({"type": "actions"},)
        )

        await expire_options(slack, posted)  # must not raise

        slack.update_message.assert_awaited_once()
        assert slack.update_message.await_args.args == ("C1", "opt_ts")
        assert not _is_live_control(slack.update_message.await_args.kwargs["blocks"])


class TestReplaceOptionsBlocks:
    """The block surgery shared by the click path and the expiry path."""

    def test_replaces_control_in_place_and_preserves_the_rest(self):
        top = {"type": "section", "text": {"type": "mrkdwn", "text": "top"}}
        control = {
            "type": "actions",
            "elements": [{"type": "checkboxes", "action_id": OPTIONS_CHECKBOXES_ACTION}],
        }
        tail = {"type": "context", "elements": [{"type": "mrkdwn", "text": "tail"}]}
        spent = [{"type": "context", "elements": [{"type": "mrkdwn", "text": "~A~"}]}]

        result = replace_options_blocks([top, control, tail], spent)

        assert result == [top, spent[0], tail]

    def test_unrelated_actions_block_is_left_alone(self):
        other = {
            "type": "actions",
            "elements": [{"type": "button", "action_id": "mc_link_dashboard"}],
        }
        spent = [{"type": "context", "elements": [{"type": "mrkdwn", "text": "~A~"}]}]

        result = replace_options_blocks([other], spent)

        assert other in result

    def test_appends_when_no_control_is_present(self):
        spent = [{"type": "context", "elements": [{"type": "mrkdwn", "text": "~A~"}]}]

        result = replace_options_blocks([], spent)

        assert result == spent

    def test_input_blocks_are_not_mutated(self):
        control = {
            "type": "actions",
            "elements": [{"type": "checkboxes", "action_id": OPTIONS_CHECKBOXES_ACTION}],
        }
        blocks = [control]

        replace_options_blocks(blocks, [{"type": "context", "elements": []}])

        assert blocks == [control]


class TestLifecycleOnTheSlot:
    """remember / expire / forget against a real slot registry."""

    def _state(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.push_slots_update = MagicMock()
        state.slack_client = _slack()
        return state

    def _posted(self) -> PostedOptions:
        return PostedOptions(
            channel="C1",
            ts="opt_ts",
            choices=("A", "B"),
            blocks=(
                {
                    "type": "actions",
                    "elements": [{"type": "checkboxes", "action_id": OPTIONS_CHECKBOXES_ACTION}],
                },
            ),
        )

    @pytest.mark.asyncio
    async def test_a_recorded_control_is_expired_on_the_next_turn(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_utils import (
            effective_session_key,
            expire_slack_options,
            remember_slack_options,
        )

        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        key = effective_session_key(slot)

        remember_slack_options(state, key, self._posted())
        assert _recs(state, slot)

        await expire_slack_options(state, key)

        state.slack_client.update_message.assert_awaited_once()
        assert _recs(state, slot) == ()

    @pytest.mark.asyncio
    async def test_expiry_runs_once_even_if_more_turns_follow(self, tmp_path, monkeypatch):
        """The record is cleared before the edit, so a failure is not retried."""
        from kiro_crew.dashboard.chat_utils import (
            effective_session_key,
            expire_slack_options,
            remember_slack_options,
        )

        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        key = effective_session_key(slot)
        remember_slack_options(state, key, self._posted())

        await expire_slack_options(state, key)
        await expire_slack_options(state, key)

        assert state.slack_client.update_message.await_count == 1

    @pytest.mark.asyncio
    async def test_forget_stops_expiry_erasing_the_users_selection(self, tmp_path, monkeypatch):
        """A Send click already re-rendered the message with the choice made.

        Striking every choice through afterwards would erase it, so the click
        drops the record instead.
        """
        from kiro_crew.dashboard.chat_utils import (
            effective_session_key,
            expire_slack_options,
            forget_slack_options,
            remember_slack_options,
        )

        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        key = effective_session_key(slot)
        remember_slack_options(state, key, self._posted())
        assert _recs(state, slot)

        forget_slack_options(state, key)
        assert _recs(state, slot) == ()
        await expire_slack_options(state, key)

        state.slack_client.update_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_state_without_slots_cannot_break_the_turn(self, tmp_path, monkeypatch):
        """Bookkeeping is cleanup, so it must never abort the turn it runs in.

        ``_run_chat`` takes whatever state object its caller passes; several
        callers pass a stand-in that has no slot registry at all.
        """
        from kiro_crew.dashboard.chat_utils import (
            expire_slack_options,
            forget_slack_options,
            options_records,
            remember_slack_options,
        )

        slack = _slack()

        class _NoSlots:
            slack_client = slack

        bare = _NoSlots()

        remember_slack_options(bare, "dashboard:s1", self._posted())
        assert options_records(bare, "dashboard:s1") == ()
        await expire_slack_options(bare, "dashboard:s1")
        forget_slack_options(bare, "dashboard:s1")

        assert vars(bare) == {}
        slack.update_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_raising_slot_registry_cannot_break_the_turn(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_utils import (
            expire_slack_options,
            options_records,
            remember_slack_options,
        )

        state = self._state(tmp_path, monkeypatch)
        state.get_slot = MagicMock(side_effect=RuntimeError("registry down"))
        remember_slack_options(state, "dashboard:s1", self._posted())

        await expire_slack_options(state, "dashboard:s1")

        # The store is keyed by session key, so a broken slot registry neither
        # blocks the expiry nor is consulted by it.
        state.slack_client.update_message.assert_awaited_once()
        assert state.slack_client.update_message.await_args.args == ("C1", "opt_ts")
        assert options_records(state, "dashboard:s1") == ()
        state.get_slot.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_click_clears_the_record_a_mirroring_session_holds(self, tmp_path, monkeypatch):
        """A thread can be owned by a dashboard session mirroring into it.

        The control is then recorded under the dashboard key, so clearing only
        the ``slack:<ts>`` key leaves it live — and the next dashboard turn
        strikes it through, erasing the selection the user just made.

        The link is established through the real endpoint, not by seeding the
        thread index by hand: an earlier version of this test set
        ``state._slack_to_slot`` itself and so passed while production never
        registered that mapping at all.
        """
        from kiro_crew.dashboard.chat_utils import (
            effective_session_key,
            expire_slack_options,
            remember_slack_options,
        )
        from kiro_crew.slack import interactions

        state = self._state(tmp_path, monkeypatch)
        state.slack_client.open_dm = AsyncMock(return_value="C1")
        state.slack_client.post_message = AsyncMock(return_value="1785370133.085469")
        state.owner_id = "U1"
        state.sessions.set_slack_link = MagicMock()
        slot = state.get_or_create_slot("s1")

        async with TestClient(TestServer(_slack_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/slack-link", json={})
            assert resp.status == 200
            await drain_background_tasks(state)
            thread_ts = (await resp.json())["thread_ts"]

        key = effective_session_key(slot)
        remember_slack_options(state, key, self._posted())

        orch = MagicMock()
        orch.dashboard_state = state
        monkeypatch.setattr(interactions, "_orch", orch)
        interactions._forget_options_control(thread_ts)

        await expire_slack_options(state, key)

        state.slack_client.update_message.assert_not_awaited()
        assert _recs(state, slot) == ()

    def test_linking_registers_the_thread_so_a_click_can_route_back(self, tmp_path, monkeypatch):
        """The reverse index is what resolves a click back to this conversation.

        A link handler that assigns the slot's fields directly and skips the
        state helper that writes it, so a click on the replayed control could
        not find the slot and answered into a separate Slack session.
        """
        from kiro_crew.dashboard.chat_utils import slack_options_owner_key

        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        thread_ts = "1785370133.085469"

        state.link_slack("s1", thread_ts, "C1")

        assert state.get_linked_slot(thread_ts) is slot
        assert slack_options_owner_key(state, thread_ts) == "dashboard:s1"

    def test_the_owner_key_survives_a_missing_thread_index(self, tmp_path, monkeypatch):
        """The index is written by a helper a caller can forget.

        Resolving through it alone is what made this silently return the wrong
        session, so the resolver also matches on the slot's own link fields.
        """
        from kiro_crew.dashboard.chat_utils import slack_options_owner_key

        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        thread_ts = "1785370133.085469"
        slot._slack_linked = True
        slot._slack_channel = "C1"
        slot._slack_thread_ts = thread_ts
        state._slack_to_slot.clear()

        assert slack_options_owner_key(state, thread_ts) == "dashboard:s1"

    def test_an_unowned_thread_resolves_to_its_own_slack_key(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_utils import slack_options_owner_key

        state = self._state(tmp_path, monkeypatch)

        assert slack_options_owner_key(state, "1785370133.085469") == "slack:1785370133.085469"

    @pytest.mark.asyncio
    async def test_a_slotless_session_is_still_tracked_and_expired(self, tmp_path, monkeypatch):
        """No dashboard slot must NOT mean no lifecycle.

        A slotless session is NOT a no-op: a plain Slack thread usually has no slot,
        so if the control were not recorded, no later turn could expire it, and the
        stale click this whole lifecycle exists to prevent would stay possible. The
        store is keyed by session key, so the slotless case is ordinary.

        Only a missing STATE is a genuine no-op: with nowhere to record, there is
        nothing to expire.
        """
        from kiro_crew.dashboard.chat_utils import (
            expire_slack_options,
            forget_slack_options,
            options_records,
            remember_slack_options,
        )

        state = self._state(tmp_path, monkeypatch)

        remember_slack_options(state, "slack:1.0", self._posted())
        assert options_records(
            state, "slack:1.0"
        ), "a session with no dashboard slot must still have its control tracked"
        await expire_slack_options(state, "slack:1.0")
        state.slack_client.update_message.assert_awaited_once()
        assert options_records(state, "slack:1.0") == ()

        forget_slack_options(state, "slack:1.0")

        # No state at all: nowhere to record, so nothing happens and nothing raises.
        state.slack_client.update_message.reset_mock()
        remember_slack_options(None, "slack:1.0", self._posted())
        await expire_slack_options(None, "slack:1.0")
        state.slack_client.update_message.assert_not_awaited()


class TestTurnEntryWiring:
    """The expiry has to actually fire on a new turn, from either surface.

    The lifecycle helpers above are exercised directly, which would stay green
    if the calls into them were deleted. These tests cover the call sites.
    """

    @pytest.mark.asyncio
    async def test_dashboard_turn_expires_before_doing_anything_else(self, tmp_path, monkeypatch):
        """Covers dashboard sends, queue drains, regenerate, rewind, cron
        injection and the Slack-linked-thread route — every turn that runs
        through the dashboard engine."""
        from kiro_crew.dashboard import chat_runner

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")

        calls: list[str] = []

        async def _record(_state, session_key):
            calls.append(session_key)

        monkeypatch.setattr(chat_runner, "expire_slack_options", _record)
        # The turn itself is irrelevant — the expiry runs before any provider
        # work, so let the turn fail however it likes.
        try:
            await chat_runner._run_chat(state, slot, "hello")
        except Exception:
            pass

        assert calls == ["dashboard:s1"]

    @pytest.mark.asyncio
    async def test_dashboard_prompt_expansion_is_not_a_new_turn(self, tmp_path, monkeypatch):
        """A /prompts reference re-enters the same turn; re-expiring there would
        spend a control the user has not been shown an answer to yet."""
        from kiro_crew.dashboard import chat_runner

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")

        calls: list[str] = []

        async def _record(_state, session_key):
            calls.append(session_key)

        monkeypatch.setattr(chat_runner, "expire_slack_options", _record)
        try:
            await chat_runner._run_chat(state, slot, "hello", _prompt_depth=1)
        except Exception:
            pass

        assert calls == []

    @pytest.mark.parametrize(
        ("thread_owner", "expected_key"),
        [
            # The live Slack path, which never reaches the dashboard engine. A
            # Slack-born thread has no owner in the thread index, so the turn runs
            # under the syntactic slack:<ts> key.
            pytest.param(None, "slack:1785370133.085469", id="test_slack_inbound_turn_expires"),
            # A Slack reply in a dashboard-owned thread must spend THAT session's
            # control. The dispatcher resolves the thread to its owning session
            # before the turn begins, so the expiry has to run after that
            # resolution: expiring slack:<ts> would leave the dashboard session's
            # control live and strike through nothing.
            pytest.param(
                "dashboard:chat-7-1785370000",
                "dashboard:chat-7-1785370000",
                id="test_slack_inbound_expires_the_threads_owning_session",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_slack_inbound_turn_expires_under_the_owning_key(
        self, monkeypatch, thread_owner, expected_key
    ):
        from kiro_crew.slack import transport_dispatch

        calls: list[str] = []

        async def _record(_state, session_key):
            calls.append(session_key)

        async def _no_linked_thread(*_a, **_k):
            return False

        # Patch the binding the module actually calls, not the definition site:
        # the import is module-scope, so `transport_dispatch` holds its own
        # reference and patching `chat_utils` would not intercept it.
        monkeypatch.setattr(transport_dispatch, "expire_slack_options", _record)
        monkeypatch.setattr(transport_dispatch, "maybe_route_linked_thread", _no_linked_thread)
        monkeypatch.setattr(
            transport_dispatch,
            "_hydrate_thread_overrides",
            AsyncMock(),
        )
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", MagicMock())
        _stub_non_turn_paths(monkeypatch, transport_dispatch)

        slack = _slack()
        sessions = MagicMock()
        # Set explicitly even when None: a bare MagicMock returns a truthy stub and
        # the dispatcher would reroute to it.
        sessions.get_session_for_thread.return_value = thread_owner
        # A PLAIN message, not "ping". The expiry deliberately sits below every
        # short-circuit, so only a message that actually starts a turn reaches
        # it -- see test_a_non_conversational_command_does_not_spend_a_live_control.
        # The turn machinery past the expiry needs no provider to get that far.
        try:
            await transport_dispatch.handle_message_transport(
                slack,
                sessions,
                "C1",
                "hello",
                "1785370133.085469",
                "1785370133.085469",
                "U1",
            )
        except Exception:
            pass

        assert calls == [expected_key]

    @pytest.mark.asyncio
    async def test_ping_does_not_spend_the_control(self, monkeypatch):
        """The behavioural half of the ordering guarantee.

        ``ping`` answers and returns without running the agent, so the pending
        question is still the one being waited on and its control must survive.
        """
        from kiro_crew.slack import transport_dispatch

        calls: list[str] = []

        async def _record(_state, session_key):
            calls.append(session_key)

        async def _no_linked_thread(*_a, **_k):
            return False

        monkeypatch.setattr(transport_dispatch, "expire_slack_options", _record)
        monkeypatch.setattr(transport_dispatch, "maybe_route_linked_thread", _no_linked_thread)
        monkeypatch.setattr(
            transport_dispatch,
            "_hydrate_thread_overrides",
            AsyncMock(),
        )
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", MagicMock())

        slack = _slack()
        sessions = MagicMock()
        sessions.get_session_for_thread.return_value = None
        await transport_dispatch.handle_message_transport(
            slack, sessions, "C1", "ping", "1785370133.085469", "1785370133.085469", "U1"
        )

        assert calls == [], (
            "a health command must not spend a live OPTIONS control -- it answers "
            "without advancing the conversation"
        )
        slack.post_message.assert_awaited_once()

    @pytest.mark.parametrize(
        "module_name,func_name",
        [
            ("kiro_crew.slack.transport_dispatch", "handle_message_transport"),
            ("kiro_crew.slack.handler", "handle_message"),
        ],
    )
    def test_slack_inbound_expires_again_after_the_turn_serializes(self, module_name, func_name):
        """Expiry must run BOTH before and after the turn serializes.

        `get_or_create` is where a turn waits for its session, so an expiry that
        only runs before it is decided on pre-wait state. Two messages arriving
        together both clear the OLD control; the first turn then ends by posting a
        NEW one, which the second turn never expires because its only pass already
        happened — leaving the user live buttons for a question the conversation
        has moved past.

        This is a STRUCTURAL check on call order rather than a behavioural one:
        the inbound path bails long before session acquisition under any harness
        cheap enough to build here (governance, hooks and provider setup all sit
        in between), so driving it end-to-end would test the stubs. Asserting the
        order in the source is what actually fails when someone deletes the second
        pass, which is the regression worth catching. Same omission-detector shape
        the rest of this class uses.
        """
        import importlib
        import inspect

        module = importlib.import_module(module_name)
        names = TURN_ENTRY_FUNCTIONS.get((module_name, func_name), (func_name,))
        source = "".join(inspect.getsource(getattr(module, name)) for name in names)

        acquire = source.find("get_or_create(")
        assert acquire != -1, f"{func_name} no longer calls get_or_create"

        before = source.find("expire_slack_options(")
        after = source.find("expire_slack_options(", acquire)

        assert (
            before != -1 and before < acquire
        ), f"{func_name} must expire the control BEFORE acquiring the session"
        assert after != -1, (
            f"{func_name} must expire AGAIN after get_or_create returns, or a "
            "control posted while this turn was queued stays clickable"
        )
