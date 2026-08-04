"""OPTIONS lifecycle on the Slack surface.

Two behaviours are locked in here:

* every outbound path renders a trailing ``[OPTIONS: …]`` tag as a control,
  never as literal text — including the link-time backfill, which used to post
  message bodies verbatim;
* a control stops being answerable once the conversation moves past the question
  it asked, whichever surface the next turn arrives on.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.slack.format import (
    OPTIONS_CHECKBOXES_ACTION,
    OPTIONS_SUBMIT_ACTION,
    build_options_blocks,
    replace_options_blocks,
)
from kiro_crew.slack.outbound import PostedOptions, expire_options

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


def _slack() -> MagicMock:
    slack = MagicMock()
    slack.post_message = AsyncMock(return_value="body_ts")
    slack.post_blocks = AsyncMock(return_value="opt_ts")
    slack.update_message = AsyncMock()
    return slack


def _recs(state, key):
    """The OPTIONS records for *key*, read through the accessor the code uses.

    Accepts a slot or a key. Given a slot it looks under the slot's EFFECTIVE
    session key and, failing that, under its bare name -- test fixtures record
    under whichever of the two reads more naturally, and the point of an assertion
    here is which records exist, not which spelling filed them. Production has no
    such ambiguity: record and expiry both resolve the live owner the same way, and
    the accessor canonicalises what it is given.
    """
    from kiro_crew.dashboard.chat_utils import effective_session_key, options_records

    if hasattr(key, "key"):
        slot = key
        return options_records(state, effective_session_key(slot)) or options_records(
            state, slot.key
        )
    return options_records(state, key)


def _set_recs(state, key, records):
    """Seed OPTIONS records for *key* (the store is keyed by session key)."""
    from kiro_crew.dashboard.chat_utils import effective_session_key, set_options_records

    if hasattr(key, "key"):
        key = effective_session_key(key)
    set_options_records(state, key, records)


def _posted_texts(slack: MagicMock) -> list[str]:
    return [c.args[1] for c in slack.post_message.await_args_list]


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


def _is_live_control(blocks: list[dict]) -> bool:
    """True when *blocks* contain a clickable OPTIONS control."""
    return any(
        el.get("action_id") in (OPTIONS_CHECKBOXES_ACTION, OPTIONS_SUBMIT_ACTION)
        for b in blocks
        if b.get("type") == "actions"
        for el in b.get("elements", [])
    )


def _context_text(blocks: list[dict]) -> str:
    return " ".join(
        el.get("text", "")
        for b in blocks
        if b.get("type") == "context"
        for el in b.get("elements", [])
    )


class TestExpireOptions:
    """Spending a control the conversation has moved past."""

    @pytest.mark.asyncio
    async def test_every_choice_is_struck_through(self):
        slack = _slack()
        blocks = build_options_blocks(["A", "B"])
        posted = PostedOptions(
            channel="C1", ts="opt_ts", choices=("A", "B"), blocks=tuple(blocks)
        )

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
                    "elements": [
                        {"type": "checkboxes", "action_id": OPTIONS_CHECKBOXES_ACTION}
                    ],
                },
            ),
        )

    @pytest.mark.asyncio
    async def test_a_recorded_control_is_expired_on_the_next_turn(
        self, tmp_path, monkeypatch
    ):
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
    async def test_expiry_runs_once_even_if_more_turns_follow(
        self, tmp_path, monkeypatch
    ):
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
    async def test_forget_stops_expiry_erasing_the_users_selection(
        self, tmp_path, monkeypatch
    ):
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

        forget_slack_options(state, key)
        await expire_slack_options(state, key)

        state.slack_client.update_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_state_without_slots_cannot_break_the_turn(
        self, tmp_path, monkeypatch
    ):
        """Bookkeeping is cleanup, so it must never abort the turn it runs in.

        ``_run_chat`` takes whatever state object its caller passes; several
        callers pass a stand-in that has no slot registry at all.
        """
        from kiro_crew.dashboard.chat_utils import (
            expire_slack_options,
            forget_slack_options,
            remember_slack_options,
        )

        class _NoSlots:
            slack_client = None

        bare = _NoSlots()

        remember_slack_options(bare, "dashboard:s1", self._posted())
        await expire_slack_options(bare, "dashboard:s1")
        forget_slack_options(bare, "dashboard:s1")

    @pytest.mark.asyncio
    async def test_a_raising_slot_registry_cannot_break_the_turn(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard.chat_utils import expire_slack_options

        state = self._state(tmp_path, monkeypatch)
        state.get_slot = MagicMock(side_effect=RuntimeError("registry down"))

        await expire_slack_options(state, "dashboard:s1")

        state.slack_client.update_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_click_clears_the_record_a_mirroring_session_holds(
        self, tmp_path, monkeypatch
    ):
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

    def test_linking_registers_the_thread_so_a_click_can_route_back(
        self, tmp_path, monkeypatch
    ):
        """The reverse index is what resolves a click back to this conversation.

        The link handler used to assign the slot's fields directly and skip the
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

    def test_the_owner_key_survives_a_missing_thread_index(
        self, tmp_path, monkeypatch
    ):
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

    def test_an_unowned_thread_resolves_to_its_own_slack_key(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard.chat_utils import slack_options_owner_key

        state = self._state(tmp_path, monkeypatch)

        assert (
            slack_options_owner_key(state, "1785370133.085469")
            == "slack:1785370133.085469"
        )

    @pytest.mark.asyncio
    async def test_a_slotless_session_is_still_tracked_and_expired(
        self, tmp_path, monkeypatch
    ):
        """No dashboard slot must NOT mean no lifecycle (#1694).

        This test previously asserted the opposite -- that a slotless session was a
        no-op -- which was the defect: a plain Slack thread usually has no slot, so
        the control was never recorded, no later turn could expire it, and the stale
        click this whole lifecycle exists to prevent stayed possible. The store is
        keyed by session key now, so the slotless case is ordinary.

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
        assert options_records(state, "slack:1.0"), (
            "a session with no dashboard slot must still have its control tracked"
        )
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
    async def test_dashboard_turn_expires_before_doing_anything_else(
        self, tmp_path, monkeypatch
    ):
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
    async def test_dashboard_prompt_expansion_is_not_a_new_turn(
        self, tmp_path, monkeypatch
    ):
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

    @pytest.mark.asyncio
    async def test_slack_inbound_turn_expires(self, monkeypatch):
        """Covers the live Slack path, which never reaches the dashboard engine."""
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
        monkeypatch.setattr(
            transport_dispatch, "maybe_route_linked_thread", _no_linked_thread
        )
        monkeypatch.setattr(transport_dispatch, "_hydrate_thread_overrides", MagicMock())
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", MagicMock())
        _stub_non_turn_paths(monkeypatch, transport_dispatch)

        slack = _slack()
        sessions = MagicMock()
        # Slack-born: the thread index has no owner for it, so the turn runs
        # under the syntactic slack:<ts> key. Must be set explicitly — a bare
        # MagicMock returns a truthy stub and the dispatcher would reroute to it.
        sessions.get_session_for_thread.return_value = None
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

        assert calls == ["slack:1785370133.085469"]

    @pytest.mark.asyncio
    async def test_ping_does_not_spend_the_control(self, monkeypatch):
        """The behavioural half of the ordering guarantee.

        ``ping`` answers and returns without running the agent, so the pending
        question is still the one being waited on and its control must survive.
        This test previously used ``ping`` merely because it short-circuited
        just after the old expiry -- which encoded the defect as the expectation.
        """
        from kiro_crew.slack import transport_dispatch

        calls: list[str] = []

        async def _record(_state, session_key):
            calls.append(session_key)

        async def _no_linked_thread(*_a, **_k):
            return False

        monkeypatch.setattr(transport_dispatch, "expire_slack_options", _record)
        monkeypatch.setattr(
            transport_dispatch, "maybe_route_linked_thread", _no_linked_thread
        )
        monkeypatch.setattr(transport_dispatch, "_hydrate_thread_overrides", MagicMock())
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
    def test_slack_inbound_expires_again_after_the_turn_serializes(
        self, module_name, func_name
    ):
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
        source = inspect.getsource(getattr(module, func_name))

        acquire = source.find("get_or_create(")
        assert acquire != -1, f"{func_name} no longer calls get_or_create"

        before = source.find("expire_slack_options(")
        after = source.find("expire_slack_options(", acquire)

        assert before != -1 and before < acquire, (
            f"{func_name} must expire the control BEFORE acquiring the session"
        )
        assert after != -1, (
            f"{func_name} must expire AGAIN after get_or_create returns, or a "
            "control posted while this turn was queued stays clickable"
        )

    @pytest.mark.asyncio
    async def test_slack_inbound_expires_the_threads_owning_session(self, monkeypatch):
        """A Slack reply in a dashboard-owned thread must spend THAT session's control.

        The dispatcher resolves the thread to its owning session before the turn
        begins, so a thread the dashboard created answers under its own key. The
        expiry has to run after that resolution — expiring ``slack:<ts>`` would
        leave the dashboard session's control live and strike through nothing.
        """
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
        monkeypatch.setattr(
            transport_dispatch, "maybe_route_linked_thread", _no_linked_thread
        )
        monkeypatch.setattr(transport_dispatch, "_hydrate_thread_overrides", MagicMock())
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", MagicMock())
        _stub_non_turn_paths(monkeypatch, transport_dispatch)

        slack = _slack()
        sessions = MagicMock()
        sessions.get_session_for_thread.return_value = "dashboard:chat-7-1785370000"
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

        assert calls == ["dashboard:chat-7-1785370000"]


def _slack_app(state):
    from kiro_crew.dashboard.chat_slack import api_chat_slot_slack_link

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/slack-link", api_chat_slot_slack_link)
    return app


class TestLinkTimeBackfill:
    """Replaying context into a freshly-linked thread."""

    def _state(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.slack_client = MagicMock()
        state.slack_client.open_dm = AsyncMock(return_value="C1")
        state.slack_client.post_message = AsyncMock(return_value="ts1")
        state.slack_client.post_blocks = AsyncMock(return_value="opt_ts")
        state.owner_id = "U1"
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.sessions.set_slack_link = MagicMock()
        state.push_slots_update = MagicMock()
        return state

    @pytest.mark.asyncio
    async def test_replayed_options_are_a_control_not_literal_text(
        self, tmp_path, monkeypatch
    ):
        """The backfill posted bodies verbatim, so the tag arrived as text.

        This is the path that carries the reply when the Slack link is created
        after the turn already finished — the mirror has nothing to send by
        then, so the backfill is the only thing that puts the answer in Slack.
        """
        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "which one?")
        slot.append("assistant", "Your call.\n\n[OPTIONS: Ship it | Hold off]")
        slot.drain()

        async with TestClient(TestServer(_slack_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/slack-link", json={})
            assert resp.status == 200

        texts = [c.args[1] for c in state.slack_client.post_message.await_args_list]
        assert all("[OPTIONS:" not in t for t in texts)
        blocks = state.slack_client.post_blocks.await_args.args[1]
        assert _is_live_control(blocks)

    @pytest.mark.asyncio
    async def test_the_newest_reply_stays_answerable_and_is_recorded(
        self, tmp_path, monkeypatch
    ):
        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "Latest.\n\n[OPTIONS: A | B]")
        slot.drain()

        async with TestClient(TestServer(_slack_app(state))) as client:
            await client.post("/api/chat/slots/s1/slack-link", json={})

        assert _is_live_control(state.slack_client.post_blocks.await_args.args[1])
        assert _recs(state, slot)
        assert _recs(state, slot)[0].choices == ("A", "B")

    @pytest.mark.asyncio
    async def test_a_superseded_question_is_replayed_spent(
        self, tmp_path, monkeypatch
    ):
        """The user already answered it — replaying it live would re-ask it."""
        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "Older.\n\n[OPTIONS: A | B]")
        slot.append("user", "A")
        slot.drain()

        async with TestClient(TestServer(_slack_app(state))) as client:
            await client.post("/api/chat/slots/s1/slack-link", json={})

        blocks = state.slack_client.post_blocks.await_args.args[1]
        assert not _is_live_control(blocks)
        assert _context_text(blocks) == "~A~  |  ~B~"
        assert _recs(state, slot) == ()

    @pytest.mark.asyncio
    async def test_a_trailing_system_row_does_not_spend_the_newest_reply(
        self, tmp_path, monkeypatch
    ):
        """The transcript holds rows that are never replayed.

        A completed turn appends one, so the last reply is not the last row.
        Judging "newest" by raw position spent the very control the replay
        exists to deliver — the user saw a struck-through question again.
        """
        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "Your call.\n\n[OPTIONS: Ship it | Hold off]")
        slot.append("done", "turn complete")
        slot.drain()

        async with TestClient(TestServer(_slack_app(state))) as client:
            await client.post("/api/chat/slots/s1/slack-link", json={})

        blocks = state.slack_client.post_blocks.await_args.args[1]
        assert _is_live_control(blocks)
        assert _recs(state, slot)

    @pytest.mark.asyncio
    async def test_a_users_own_options_syntax_survives_the_replay(
        self, tmp_path, monkeypatch
    ):
        """A person can type the OPTIONS syntax — quoting it, or discussing it.

        Routing user rows through the agent-authored path lifted the tag out of
        their words and rendered it as struck-through choices they never offered.
        Their text has to come back verbatim.
        """
        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        # The tag must END the message: the parser is line-anchored, so trailing
        # words after it would make this pass whatever the code did.
        slot.append("user", "should I paste this?\n\n[OPTIONS: A | B]")
        slot.drain()

        async with TestClient(TestServer(_slack_app(state))) as client:
            await client.post("/api/chat/slots/s1/slack-link", json={})

        texts = [c.args[1] for c in state.slack_client.post_message.await_args_list]
        assert any("[OPTIONS: A | B]" in t for t in texts)
        state.slack_client.post_blocks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_only_the_newest_reply_is_answerable(self, tmp_path, monkeypatch):
        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "First.\n\n[OPTIONS: A | B]")
        slot.append("user", "A")
        slot.append("assistant", "Second.\n\n[OPTIONS: C | D]")
        slot.drain()

        async with TestClient(TestServer(_slack_app(state))) as client:
            await client.post("/api/chat/slots/s1/slack-link", json={})

        posts = state.slack_client.post_blocks.await_args_list
        assert len(posts) == 2
        assert not _is_live_control(posts[0].args[1])
        assert _is_live_control(posts[1].args[1])


class TestNonStringKeyIsRefused:
    """A key that is not a real string must never reach slot-key normalization.

    ``_normalize_slot_key`` strips a repeated ``dashboard_`` prefix with an
    unbounded ``while`` loop, which terminates only for a genuine ``str``.
    Anything whose ``startswith`` is always truthy -- a MagicMock standing in
    for a Slack payload field, for instance -- spins forever AND manufactures a
    fresh child object every iteration. Silent, so no test fails and no timeout
    trips; it simply eats memory until the process dies. On CI that presented
    as a runner shutdown at a random point in the shard with zero FAILED lines.

    The click path reaches this lookup with a thread id taken straight off the
    interaction payload, so the type is not guaranteed at the call site -- the
    guard belongs here.
    """

    @pytest.mark.timeout(5)
    def test_a_non_string_key_returns_none_instead_of_spinning(self):
        from kiro_crew.dashboard import chat_utils

        state = MagicMock()

        assert chat_utils.slack_options_slot(state, MagicMock()) is None
        # Refused before any lookup: proves we never entered normalization.
        state.get_slot.assert_not_called()

    @pytest.mark.timeout(5)
    def test_a_real_string_key_still_reaches_the_lookup(self):
        from kiro_crew.dashboard import chat_utils

        state = MagicMock()
        sentinel = object()
        state.get_slot.return_value = sentinel

        assert chat_utils.slack_options_slot(state, "chat-1-123") is sentinel
        state.get_slot.assert_called_once()


class TestUnlinkSpendsTheControl:
    """Unlinking must EXPIRE the OPTIONS control, not merely orphan the record.

    Two failure modes, both closed by expiring rather than forgetting:

    1. Forget-only leaves the buttons live in Slack. After the link is gone a
       click answers a question from a conversation this thread is no longer
       attached to, landing that stale answer in a brand-new session.
    2. Doing nothing leaves the record unreachable once the thread -> slot
       reverse index is popped, so the next dashboard turn's expiry strikes every
       choice through — erasing an answer the user had already given.

    Expiry does both halves: it strikes the choices through in Slack AND clears
    the record. It has to run before the link is torn down, because popping the
    index is what makes the record unreachable.
    """

    @pytest.mark.asyncio
    async def test_unlink_expires_a_live_options_control(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_slack import api_chat_slot_slack_unlink

        monkeypatch.setattr("kiro_crew.dashboard.chat_slack.sel", lambda: MagicMock())

        state = _make_state(tmp_path)
        # Returns into the JSON body, so it has to be a real bool not a mock.
        state.sessions.clear_slack_link = MagicMock(return_value=True)
        slack = MagicMock()
        slack.update_message = AsyncMock()
        slack.post_message = AsyncMock()
        state.slack_client = slack

        slot = state.get_or_create_slot("s1")
        slot._slack_linked = True
        slot._slack_channel = "C-1"
        slot._slack_thread_ts = "thread-1"
        state._slack_to_slot["thread-1"] = slot.key

        _set_recs(
            state,
            slot,
            (
                PostedOptions(
                    channel="C-1",
                    ts="opt-1",
                    choices=("A", "B"),
                    blocks=(),
                ),
            ),
        )

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/slack-unlink", api_chat_slot_slack_unlink)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/chat/slots/{slot.key}/slack-unlink")
            assert resp.status == 200

        # The buttons are struck through in Slack, so a click after the unlink
        # cannot inject a stale answer into a new session.
        slack.update_message.assert_awaited_once()
        assert slack.update_message.await_args.args[0] == "C-1"
        assert slack.update_message.await_args.args[1] == "opt-1"
        # And the record is gone, so a later dashboard turn has nothing to strike
        # through and cannot erase the user's selection.
        assert _recs(state, slot) == ()
        # The reverse index is still dropped — spending the control must not come
        # at the cost of the thread continuing to resolve here.
        assert "thread-1" not in state._slack_to_slot

    @pytest.mark.asyncio
    async def test_a_relink_during_the_expiry_await_is_not_clobbered(
        self, tmp_path, monkeypatch
    ):
        """Unlink must not tear down a link that replaced the one it captured.

        Neither plain ordering is safe. Tearing down BEFORE expiry leaves the
        buttons live with the reverse index already gone, so a click resolves to
        nothing and starts a brand-new session with a stale answer. Expiring
        first, then tearing down unconditionally, lets a relink that landed during
        the (Slack-bound) await get its in-memory fields wiped while its persisted
        link survives. Compare-and-clear is the only ordering that closes both, so
        this test pins the relink half.
        """
        from kiro_crew.dashboard.chat_slack import api_chat_slot_slack_unlink

        monkeypatch.setattr("kiro_crew.dashboard.chat_slack.sel", lambda: MagicMock())

        state = _make_state(tmp_path)
        state.sessions.clear_slack_link = MagicMock(return_value=True)
        slack = MagicMock()
        slack.post_message = AsyncMock()
        state.slack_client = slack

        slot = state.get_or_create_slot("s1")
        slot._slack_linked = True
        slot._slack_channel = "C-1"
        slot._slack_thread_ts = "thread-1"
        state._slack_to_slot["thread-1"] = slot.key

        # Another tab relinks the slot to a NEW thread while expiry awaits Slack.
        async def _relink_midway(_state, _session_key):
            slot._slack_linked = True
            slot._slack_channel = "C-2"
            slot._slack_thread_ts = "thread-2"
            _state._slack_to_slot["thread-2"] = slot.key

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_slack.expire_slack_options", _relink_midway
        )

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/slack-unlink", api_chat_slot_slack_unlink)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/chat/slots/{slot.key}/slack-unlink")
            assert resp.status == 200

        # The replacement link survives intact -- fields AND reverse index.
        assert slot._slack_linked is True
        assert slot._slack_channel == "C-2"
        assert slot._slack_thread_ts == "thread-2"
        assert state._slack_to_slot.get("thread-2") == slot.key

    @pytest.mark.asyncio
    async def test_a_same_target_relink_does_not_leave_persistence_linked(
        self, tmp_path, monkeypatch
    ):
        """A relink to the SAME thread must SURVIVE the unlink that raced it.

        Persistence is cleared before the expiry await, so a relink landing during
        it restores the identical channel/ts and the compare-and-clear sees equal
        values -- in memory, indistinguishable from "nothing moved". Resolving that
        by value can only guess. Persistence resolves it by PRESENCE: we cleared it,
        and only ``link_slack`` writes it, so a link sitting there afterwards was
        written during the await. It must be left alone -- link, persistence,
        reverse index -- and the unlink must report a no-op rather than announcing
        into a thread that is still syncing.
        """
        from kiro_crew.dashboard.chat_slack import api_chat_slot_slack_unlink
        from kiro_crew.dashboard.chat_utils import effective_session_key

        monkeypatch.setattr("kiro_crew.dashboard.chat_slack.sel", lambda: MagicMock())

        state = _make_state(tmp_path)
        slack = MagicMock()
        slack.post_message = AsyncMock()
        state.slack_client = slack

        slot = state.get_or_create_slot("s1")
        slot._slack_linked = True
        slot._slack_channel = "C-1"
        slot._slack_thread_ts = "thread-1"
        state._slack_to_slot["thread-1"] = slot.key
        session_key = effective_session_key(slot)
        state.sessions.set_slack_link(session_key, "thread-1", "C-1")

        # A relink to the SAME thread lands mid-await. Faithful to link_slack: it
        # sets the slot fields, registers the reverse index, AND persists.
        async def _same_target_relink(_state, _session_key, ts=None):
            slot._slack_linked = True
            slot._slack_channel = "C-1"
            slot._slack_thread_ts = "thread-1"
            _state._slack_to_slot["thread-1"] = slot.key
            _state.sessions.set_slack_link(session_key, "thread-1", "C-1")

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_slack.expire_slack_options", _same_target_relink
        )

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/slack-unlink", api_chat_slot_slack_unlink)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/chat/slots/{slot.key}/slack-unlink")
            assert resp.status == 200
            assert (await resp.json())["was_linked"] is False, (
                "a superseded unlink must report a no-op, not claim it tore a link down"
            )

        # The relink survives, in memory AND in persistence -- they agree.
        assert slot._slack_linked is True
        assert slot._slack_channel == "C-1"
        assert slot._slack_thread_ts == "thread-1"
        assert state._slack_to_slot.get("thread-1") == slot.key
        assert state.sessions.get_slack_link(session_key) == ("thread-1", "C-1"), (
            "the relink's persisted link must not be cleared by the unlink it raced"
        )
        # ...and nothing was announced into the still-live thread.
        assert slack.post_message.await_count == 0, (
            "no 'replies here no longer sync' note may go into a thread that is "
            "still syncing"
        )

    @pytest.mark.asyncio
    async def test_an_unexpired_control_aborts_the_unlink(self, tmp_path, monkeypatch):
        """A control we could not retire must keep its routing.

        Round 29 deliberately keeps a record whose Slack edit failed transiently,
        so a returned expiry does not prove the control was spent. Dropping the
        reverse index while the buttons are still live is teardown-then-NEVER-
        expire: a later click resolves to nothing and starts a BRAND-NEW session
        carrying a stale answer -- the hazard the expire-first ordering exists to
        avoid. Aborting is recoverable and visible; completing silently corrupts a
        future conversation.
        """
        from kiro_crew.dashboard.chat_slack import api_chat_slot_slack_unlink
        from kiro_crew.dashboard.chat_utils import effective_session_key

        monkeypatch.setattr("kiro_crew.dashboard.chat_slack.sel", lambda: MagicMock())

        state = _make_state(tmp_path)
        slack = MagicMock()
        slack.post_message = AsyncMock()
        state.slack_client = slack

        slot = state.get_or_create_slot("s1")
        slot._slack_linked = True
        slot._slack_channel = "C-1"
        slot._slack_thread_ts = "thread-1"
        state._slack_to_slot["thread-1"] = slot.key
        session_key = effective_session_key(slot)
        state.sessions.set_slack_link(session_key, "thread-1", "C-1")

        # The expiry runs but leaves the record tracked -- a 429 that will be
        # retried, exactly what round 29 guarantees.
        stuck = PostedOptions(channel="C-1", ts="opt-1", choices=("A",), blocks=())

        async def _expiry_fails(_state, _key, ts=None):
            _set_recs(state, slot, (stuck,))

        monkeypatch.setattr("kiro_crew.dashboard.chat_slack.expire_slack_options", _expiry_fails)

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/slack-unlink", api_chat_slot_slack_unlink)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/chat/slots/{slot.key}/slack-unlink")
            assert resp.status == 503, (
                "the unlink must refuse rather than orphan a live control; the "
                "dashboard reads a non-2xx as 'session stays linked'"
            )

        # Routing survives, in memory AND in persistence, so a click still lands
        # in the right conversation where a later turn can spend it.
        assert slot._slack_linked is True
        assert state._slack_to_slot.get("thread-1") == slot.key
        assert state.sessions.get_slack_link(session_key) == ("thread-1", "C-1"), (
            "the persisted link cleared at the top must be restored on abort"
        )
        assert slack.post_message.await_count == 0, (
            "no 'unlinked' note may be posted for an unlink that did not happen"
        )

    @pytest.mark.asyncio
    async def test_an_unraced_unlink_still_tears_the_link_down(self, tmp_path, monkeypatch):
        """The other half: with no relink, persistence stays clear and teardown completes.

        Guards the opposite failure from the test above -- treating every equal
        comparison as "a relink landed" would turn every ordinary unlink into a
        silent no-op.
        """
        from kiro_crew.dashboard.chat_slack import api_chat_slot_slack_unlink
        from kiro_crew.dashboard.chat_utils import effective_session_key

        monkeypatch.setattr("kiro_crew.dashboard.chat_slack.sel", lambda: MagicMock())

        state = _make_state(tmp_path)
        slack = MagicMock()
        slack.post_message = AsyncMock()
        state.slack_client = slack

        slot = state.get_or_create_slot("s1")
        slot._slack_linked = True
        slot._slack_channel = "C-1"
        slot._slack_thread_ts = "thread-1"
        state._slack_to_slot["thread-1"] = slot.key
        session_key = effective_session_key(slot)
        state.sessions.set_slack_link(session_key, "thread-1", "C-1")

        async def _no_relink(_state, _session_key, ts=None):
            return None

        monkeypatch.setattr("kiro_crew.dashboard.chat_slack.expire_slack_options", _no_relink)

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/slack-unlink", api_chat_slot_slack_unlink)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/chat/slots/{slot.key}/slack-unlink")
            assert resp.status == 200
            assert (await resp.json())["was_linked"] is True

        assert slot._slack_linked is False
        assert slot._slack_channel == ""
        assert "thread-1" not in state._slack_to_slot
        assert state.sessions.get_slack_link(session_key) in ((None, None), ("", "")), (
            "an unraced unlink must leave persistence clear"
        )


class TestEveryOutstandingControlIsExpired:
    """A newer control must not displace the record of an older live one.

    One turn can post more than one OPTIONS message, and a single slot is
    reachable from several posting paths. When the record was a single slot the
    newer post overwrote the older one, so the older control stayed on screen
    with nothing tracking it — clicking it answered a question the conversation
    had already moved past, and the answer landed as if it were current.
    """

    @pytest.mark.asyncio
    async def test_expiry_drains_all_recorded_controls(self, tmp_path):
        from kiro_crew.dashboard.chat_utils import (
            expire_slack_options,
            remember_slack_options,
        )

        state = _make_state(tmp_path)
        slack = MagicMock()
        slack.update_message = AsyncMock()
        state.slack_client = slack

        slot = state.get_or_create_slot("s1")

        first = PostedOptions(channel="C-1", ts="opt-1", choices=("A",), blocks=())
        second = PostedOptions(channel="C-1", ts="opt-2", choices=("B",), blocks=())
        remember_slack_options(state, slot.key, first)
        remember_slack_options(state, slot.key, second)

        # Both are tracked — the second did not displace the first.
        assert _recs(state, slot) == (first, second)

        await expire_slack_options(state, slot.key)

        edited = {call.args[1] for call in slack.update_message.await_args_list}
        assert edited == {"opt-1", "opt-2"}, edited
        assert _recs(state, slot) == ()

    @pytest.mark.asyncio
    async def test_a_cron_control_is_tracked_and_spent_under_its_session_key(self, tmp_path):
        """A cron slot's name is not its session key folded, so it must still resolve.

        A persistent cron runs on ``cron:<id>`` but its slot is named
        ``cron-<id>``. The filename fold turns the colon into an underscore, so
        the lookup asked for ``cron_<id>`` and matched nothing: the control was
        never recorded, and the follow-up turn had nothing to expire — leaving a
        live control answerable into a question the cron had moved past. The
        resolver falls back to asking the slots their own identity, so both the
        record and the expiry reach the same slot.
        """
        from kiro_crew.dashboard.chat_utils import (
            expire_slack_options,
            remember_slack_options,
            slack_options_slot,
        )

        state = _make_state(tmp_path)
        slack = MagicMock()
        slack.update_message = AsyncMock()
        state.slack_client = slack

        # Exactly how cron_inject builds it: slot named cron-<id>, real key on
        # linked_session_key.
        slot = state.get_or_create_slot("cron-42")
        slot.linked_session_key = "cron:42"
        session_key = "cron:42"

        # The folded spelling really does miss -- that is the defect.
        assert state.get_slot("cron_42") is None
        assert slack_options_slot(state, session_key) is slot

        posted = PostedOptions(channel="C-1", ts="opt-cron", choices=("A",), blocks=())
        remember_slack_options(state, session_key, posted)
        assert _recs(state, slot) == (posted,), "the cron control must be tracked"

        await expire_slack_options(state, session_key)

        edited = {call.args[1] for call in slack.update_message.await_args_list}
        assert edited == {"opt-cron"}, edited
        assert _recs(state, slot) == ()

    @pytest.mark.asyncio
    async def test_a_transient_edit_failure_keeps_the_record_for_a_later_retry(self, tmp_path):
        """A failed Slack edit must not orphan a control that is still live.

        Records stay tracked across the edit and only settled ones are removed, so
        a transient failure is simply never removed and a later turn retries it.
        Dropping it would leave the buttons on screen with nothing tracking them —
        a later click then injects an answer to a superseded question.
        """
        from slack_sdk.errors import SlackApiError

        from kiro_crew.dashboard.chat_utils import (
            expire_slack_options,
            remember_slack_options,
        )

        state = _make_state(tmp_path)
        slack = MagicMock()
        slack.update_message = AsyncMock(
            side_effect=SlackApiError("ratelimited", MagicMock(status_code=429))
        )
        state.slack_client = slack

        slot = state.get_or_create_slot("s1")
        posted = PostedOptions(channel="C-1", ts="opt-1", choices=("A",), blocks=())
        remember_slack_options(state, slot.key, posted)

        await expire_slack_options(state, slot.key)

        assert _recs(state, slot) == (posted,), (
            "a transient edit failure must leave the control tracked so a later "
            "turn can spend it, not orphan it live on screen"
        )

    @pytest.mark.asyncio
    async def test_a_permanent_edit_failure_is_not_retried_forever(self, tmp_path):
        """A message that can never be edited must not be retried every turn.

        The mirror of the test above: keeping EVERY failure would mean a deleted
        message or a channel we are no longer in burns an API call on every
        subsequent turn, forever. A 4xx that is not a rate limit will fail
        identically next time, so the record is settled.
        """
        from slack_sdk.errors import SlackApiError

        from kiro_crew.dashboard.chat_utils import (
            expire_slack_options,
            remember_slack_options,
        )

        state = _make_state(tmp_path)
        slack = MagicMock()
        slack.update_message = AsyncMock(
            side_effect=SlackApiError("message_not_found", MagicMock(status_code=404))
        )
        state.slack_client = slack

        slot = state.get_or_create_slot("s1")
        posted = PostedOptions(channel="C-1", ts="opt-1", choices=("A",), blocks=())
        remember_slack_options(state, slot.key, posted)

        await expire_slack_options(state, slot.key)

        assert _recs(state, slot) == (), (
            "a permanently-failing edit must be treated as spent, or every later "
            "turn retries it forever"
        )

    def test_a_cron_owned_control_is_forgotten_on_selection(self):
        """The forget must cover the key the record actually used.

        The record sites resolve the owner through the PERSISTED thread index, so
        a control on a cron-linked thread is filed under ``cron:<id>``. The
        ownership helpers only consulted the dashboard SLOT index (plus the
        syntactic ``slack:<ts>``), which cannot see a cron link -- so the
        selection's forget missed the record entirely and the next expiry edited
        over the user's answer. It also defeated round 33's under-lock skip, which
        relies on the forget having removed the record.
        """
        from kiro_crew.dashboard.chat_utils import (
            slack_options_owner_key,
            slack_options_session_keys,
        )

        state = MagicMock()
        state._slack_to_slot = {}
        state._slots = {}
        state.get_linked_slot = MagicMock(return_value=None)
        state.sessions.get_session_for_thread = MagicMock(return_value="cron:job-7")

        assert slack_options_owner_key(state, "thread-1") == "cron:job-7", (
            "recording must file the control under the cron session that owns the "
            "thread, not the syntactic slack:<ts> key"
        )
        keys = slack_options_session_keys(state, "thread-1")
        assert "cron:job-7" in keys, (
            "clearing must cover the cron key or the record outlives the selection"
        )

    def test_an_unresolvable_owner_falls_back_without_inventing_a_key(self):
        """A stub or a miss must not become a bogus session key.

        The persisted index is typed ``str | None``; anything else means a caller
        handed us a mock. Guards the helper against silently recording under a
        MagicMock's repr.
        """
        from kiro_crew.dashboard.chat_utils import (
            canonical_key,
            slack_options_owner_key,
            slack_options_session_keys,
        )

        state = MagicMock()
        state._slack_to_slot = {}
        state._slots = {}
        state.get_linked_slot = MagicMock(return_value=None)
        state.sessions.get_session_for_thread = MagicMock(return_value=MagicMock())

        assert slack_options_owner_key(state, "thread-1") == canonical_key("thread-1")
        assert slack_options_session_keys(state, "thread-1") == [canonical_key("thread-1")]

    @pytest.mark.asyncio
    async def test_a_selection_made_under_the_lock_is_not_overwritten(self, tmp_path):
        """A click that wins the race must not have its answer erased.

        Expiry and the Send handler both edit the SAME Slack message, and Slack
        offers no compare-and-set. They now share ``options_edit_lock`` per
        message, and the expiry re-reads its record INSIDE that lock -- so a Send
        that already rewrote the message and dropped the record makes the expiry
        skip its edit entirely, rather than landing late and replacing the
        selection with a spent summary.
        """
        from kiro_crew.dashboard.chat_utils import (
            expire_slack_options,
            forget_slack_options,
            remember_slack_options,
        )
        from kiro_crew.slack.outbound import options_edit_lock

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        posted = PostedOptions(channel="C-1", ts="opt-1", choices=("A",), blocks=())
        remember_slack_options(state, slot.key, posted)

        slack = MagicMock()
        slack.update_message = AsyncMock()
        state.slack_client = slack

        # Hold the lock, then let a "click" rewrite the message and forget the
        # record while the expiry is queued behind us.
        lock = options_edit_lock("C-1", "opt-1")
        async with lock:
            expiring = asyncio.create_task(expire_slack_options(state, slot.key))
            await asyncio.sleep(0)  # let the expiry reach the lock and block
            forget_slack_options(state, slot.key, "opt-1")
        await expiring

        assert slack.update_message.await_count == 0, (
            "the expiry must skip its edit once the click dropped the record -- "
            "editing anyway erases the answer the user just gave"
        )
        assert _recs(state, slot) == ()

    @pytest.mark.asyncio
    async def test_a_click_during_a_failing_edit_is_not_resurrected(self, tmp_path):
        """A control answered mid-await must stay answered.

        The hazard is specific to retaining transient failures. A click landing
        while the expiry's edit is being rate-limited renders the SELECTED summary
        and forgets the record. If the expiry then wrote its unsettled records
        back, it would resurrect the one just answered, and every later turn would
        re-edit that message -- replacing the user's visible answer with a spent
        summary. Only settled records may be removed; nothing is ever re-added.
        """
        from slack_sdk.errors import SlackApiError

        from kiro_crew.dashboard.chat_utils import (
            expire_slack_options,
            forget_slack_options,
            remember_slack_options,
        )

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        posted = PostedOptions(channel="C-1", ts="opt-1", choices=("A",), blocks=())

        async def _rate_limited_then_click(*_a, **_k):
            # The click lands while this edit is in flight: it answers the
            # control and drops the record, exactly as the submit path does.
            forget_slack_options(state, slot.key, "opt-1")
            raise SlackApiError("ratelimited", MagicMock(status_code=429))

        slack = MagicMock()
        slack.update_message = AsyncMock(side_effect=_rate_limited_then_click)
        state.slack_client = slack

        remember_slack_options(state, slot.key, posted)
        await expire_slack_options(state, slot.key)

        assert _recs(state, slot) == (), (
            "a control answered while the edit was failing must NOT come back -- "
            "re-adding it makes every later turn overwrite the user's answer"
        )

    @pytest.mark.asyncio
    async def test_recording_the_same_control_twice_queues_one_edit(self, tmp_path):
        """A retry, or two paths recording one post, is not two live controls."""
        from kiro_crew.dashboard.chat_utils import (
            expire_slack_options,
            remember_slack_options,
        )

        state = _make_state(tmp_path)
        slack = MagicMock()
        slack.update_message = AsyncMock()
        state.slack_client = slack

        slot = state.get_or_create_slot("s1")
        posted = PostedOptions(channel="C-1", ts="opt-1", choices=("A",), blocks=())
        remember_slack_options(state, slot.key, posted)
        remember_slack_options(state, slot.key, posted)

        assert _recs(state, slot) == (posted,)

        await expire_slack_options(state, slot.key)
        slack.update_message.assert_awaited_once()


class TestForgettingIsScopedToTheClickedControl:
    """Answering one control must not un-track the others.

    Once several controls can be outstanding at once, clearing the whole
    collection on a click leaves the other ones on screen with nothing tracking
    them — so a later click on one answers a superseded question and no expiry
    can ever reach it. A click spends exactly the control it was made on.

    The unscoped form is still correct for an unlink, where the entire
    conversation is detaching and every control should stop being tracked.
    """

    def test_a_click_forgets_only_the_control_it_answered(self, tmp_path):
        from kiro_crew.dashboard.chat_utils import (
            forget_slack_options,
            remember_slack_options,
        )

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")

        first = PostedOptions(channel="C-1", ts="opt-1", choices=("A",), blocks=())
        second = PostedOptions(channel="C-1", ts="opt-2", choices=("B",), blocks=())
        remember_slack_options(state, slot.key, first)
        remember_slack_options(state, slot.key, second)

        forget_slack_options(state, slot.key, "opt-1")

        # The answered one is spent; the other is STILL tracked, so a later turn
        # can expire it instead of leaving it clickable forever.
        assert _recs(state, slot) == (second,)

    def test_omitting_the_ts_still_clears_everything(self, tmp_path):
        """The unlink path detaches the whole conversation, so all of them go."""
        from kiro_crew.dashboard.chat_utils import (
            forget_slack_options,
            remember_slack_options,
        )

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        remember_slack_options(
            state, slot.key, PostedOptions(channel="C-1", ts="opt-1", choices=("A",), blocks=())
        )
        remember_slack_options(
            state, slot.key, PostedOptions(channel="C-1", ts="opt-2", choices=("B",), blocks=())
        )

        forget_slack_options(state, slot.key)

        assert _recs(state, slot) == ()


class TestControlPostedAfterTheWindowIsSpent:
    """A control recorded after its expiry window closed must be spent on the spot.

    Both sites post the control AFTER the point where a concurrent turn's expiry
    pass would have seen it — the native handler releases the session permit long
    before the timing footer goes up, and the link backfill awaits a Slack post per
    replayed message. In both cases the superseding turn's expiry runs over a record
    that does not exist yet, so nothing else will ever spend it. Each site therefore
    re-checks after recording and expires immediately if the conversation moved on.
    """

    def test_native_footer_path_rechecks_business_after_recording(self):
        """Structural: the recheck must sit AFTER remember_slack_options.

        Driving `handle_message` to the footer requires a provider, a stream and a
        full turn; the assertion that actually protects this is that the recheck
        exists and comes after the record, so deleting it fails here.
        """
        import inspect

        from kiro_crew.slack import handler

        source = inspect.getsource(handler.handle_message)
        record = source.find("remember_slack_options(")
        assert record != -1, "handle_message no longer records the control"
        recheck = source.find("is_busy(", record)
        assert recheck != -1, (
            "handle_message must re-check is_busy AFTER recording, or a control "
            "posted once the permit is released stays live for a superseded question"
        )
        expire_after = source.find("expire_slack_options(", recheck)
        assert expire_after != -1, "the is_busy re-check must lead to an expiry"

    def test_native_footer_path_also_catches_a_turn_that_already_finished(self):
        """Structural: the recheck must not rely on is_busy alone.

        is_busy answers "is a turn in flight right now". A superseding turn that
        both STARTS and FINISHES while the footer is being posted reads as idle at
        both ends, so is_busy misses exactly the race this guard exists for. The
        monotonic turn counter has to be sampled BEFORE the footer post and
        compared after, which is what the assertions below pin.
        """
        import inspect

        from kiro_crew.slack import handler

        source = inspect.getsource(handler.handle_message)
        baseline = source.find("_turn_counter_for(")
        assert baseline != -1, (
            "handle_message must sample the turn counter, or a superseding turn "
            "that completes during the footer post leaves its control clickable"
        )
        post = source.find("post_blocks(", baseline)
        assert post != -1, "the counter must be sampled BEFORE the footer post_blocks"
        compare = source.find("_turn_counter_for(", post)
        assert compare != -1, "the counter must be re-read AFTER the footer post"
        # And the comparison has to feed the same expiry the is_busy half feeds.
        expiry = source.find("expire_slack_options(", compare)
        assert expiry != -1
        # Narrowed to the footer's OWN ts. A session-wide drain here would strike
        # through a control the superseding turn recorded while we awaited Slack,
        # silencing the question the conversation is now waiting on.
        call = source[expiry : expiry + 400]
        assert "ts=_footer_ts" in call, (
            "the footer's superseded-cleanup must expire only its own control "
            "(ts=_footer_ts), not drain every control on the session"
        )

    def test_options_are_recorded_under_the_threads_live_owner(self):
        """Structural: the record and the expiry must key off the SAME owner.

        A thread linked to a dashboard mid-turn changes owner. Recording under the
        key the turn started with files the control where the next turn's expiry
        — which resolves the current owner — will never look, leaving it clickable
        into a question the conversation has already passed. Both Slack posting
        paths have to resolve the live owner, and the footer path has to resolve it
        ONCE so a link landing between its record and its cleanup cannot split them.
        """
        import inspect

        from kiro_crew.slack import handler, transport_dispatch

        dispatch = inspect.getsource(transport_dispatch.handle_message_transport)
        owner_expr = "get_session_for_thread(reply_ts) or session_key"
        d_owner = dispatch.find("_options_owner =")
        assert d_owner != -1, "transport_dispatch must resolve the owner into a variable"
        assert owner_expr in dispatch[d_owner : d_owner + 200], (
            "transport_dispatch must resolve the thread's LIVE owner, not the key "
            "the turn started under"
        )
        rec = dispatch.find("remember_slack_options(")
        assert rec != -1
        assert "_options_owner" in dispatch[rec : rec + 300], (
            "the record must consume the resolved owner, not a re-derived key"
        )

        footer = inspect.getsource(handler.handle_message)
        owner = footer.find("_options_owner =")
        assert owner != -1, "the footer path must resolve the owner once, into a variable"
        assert "get_session_for_thread(reply_ts) or session_key" in footer[owner : owner + 200]
        # Both the record and the cleanup must consume that one resolution.
        assert footer.count("_options_owner") >= 3, (
            "the footer's record AND its superseded-cleanup must both use the "
            "single resolved owner, or a mid-turn relink can split them"
        )

    def test_an_owner_change_counts_as_supersession_in_both_paths(self):
        """Structural: a mid-post owner change must supersede on its own.

        The counter is only comparable against itself. If the thread's owner
        changes while the control is being posted, the baseline was sampled on a
        DIFFERENT session, so comparing counters is meaningless — it would read
        "nothing moved" exactly when the conversation moved to another session.
        Both paths therefore treat an owner change as supersession outright, and
        the footer's baseline is sampled on the pre-post owner rather than on the
        key the turn started under.
        """
        import inspect

        from kiro_crew.slack import handler, transport_dispatch

        footer = inspect.getsource(handler.handle_message)
        pre = footer.find("_pre_owner =")
        assert pre != -1, "the footer must resolve the owner BEFORE posting"
        baseline = footer.find("_pre_footer_total =", pre)
        assert baseline != -1, "the baseline must be sampled after resolving that owner"
        assert "_pre_owner" in footer[baseline : baseline + 120], (
            "the baseline must be sampled on the PRE-POST OWNER, not on the key "
            "the turn started under -- otherwise it cannot observe the new owner"
        )
        sup = footer.find("_superseded =")
        assert sup != -1
        guard = footer[sup : sup + 400]
        assert "_options_owner != _pre_owner" in guard, (
            "an owner change must itself count as supersession"
        )
        # ...and it must come FIRST, so two incomparable counters are never compared.
        assert guard.find("!= _pre_owner") < guard.find("_counter_moved()"), (
            "the owner-change check must short-circuit before the counter compare"
        )

        dispatch = inspect.getsource(transport_dispatch.handle_message_transport)
        assert "_pre_run_owner =" in dispatch, (
            "transport_dispatch must capture the owner before the run to detect a "
            "mid-turn relink"
        )
        assert "_options_owner != _pre_run_owner" in dispatch, (
            "transport_dispatch must spend its own control when the owner changed"
        )

    def test_an_unreadable_turn_counter_abstains_instead_of_superseding(self):
        """A slot appearing or vanishing mid-post is not a turn.

        ``_turn_counter_for`` returns None when there is no dashboard slot, which
        is the normal state for a Slack conversation until something surfaces one
        -- and the channel-surface reconciler can create one DURING the footer
        post. A bare ``!=`` then compares None against an int, reads "a turn
        happened", and expires the control the instant it lands. Both sides must
        be present before the comparison counts, which is what
        ``slack_options_turn_counter``'s own docstring already promises.
        """
        import inspect

        from kiro_crew.slack import handler

        footer = inspect.getsource(handler.handle_message)
        moved = footer.find("def _counter_moved")
        assert moved != -1, "the counter comparison must be guarded, not bare"
        body = footer[moved : moved + 1400]
        assert "_pre_footer_total is not None" in body, (
            "the BEFORE reading must be required to exist"
        )
        assert "after is not None" in body, "the AFTER reading must be required to exist"
        # ...and the guards must gate the same comparison that feeds supersession.
        assert "!= _pre_footer_total" in body
        assert "or _counter_moved()" in footer[moved:], (
            "supersession must consume the guarded form, so the owner-change check "
            "still short-circuits ahead of any counter read"
        )
        assert "or _turn_counter_for(_options_owner) != _pre_footer_total" not in footer, (
            "the unguarded comparison must be gone, not merely shadowed"
        )

    def test_a_non_conversational_command_does_not_spend_a_live_control(self):
        """`ping`/`status`/denial must not expire a still-pending question.

        Purely an ordering guarantee, so asserted on source order. The entry
        expiry used to run before the shortcuts: a pending
        ``[OPTIONS: Deploy | Abort]`` plus a `ping` in the thread struck the
        control through, posted `pong`, and returned without running the agent --
        so the conversation had NOT moved, yet the buttons were spent and the
        question became unanswerable. That is the inverse of the stale click this
        whole lifecycle prevents.

        The expiry must therefore sit BELOW every short-circuit that answers
        without starting a turn, in both entry points. One position below them all
        (rather than a guard per command) also means a shortcut added later
        inherits the correct behaviour.
        """
        import inspect

        from kiro_crew.slack import handler, transport_dispatch

        for name, src, shortcuts in (
            (
                "transport_dispatch",
                inspect.getsource(transport_dispatch.handle_message_transport),
                ('_lower == "ping"', '_lower == "status"', "if _only_modifier:"),
            ),
            (
                "handler",
                inspect.getsource(handler.handle_message),
                ('.lower() == "status"', "if _only_modifier:", "_Permission denied._"),
            ),
        ):
            expiry = src.find("await expire_slack_options(")
            assert expiry != -1, f"{name} must still expire on a real turn"
            for shortcut in shortcuts:
                at = src.find(shortcut)
                assert at != -1, f"{name}: shortcut {shortcut!r} not found -- test is stale"
                assert at < expiry, (
                    f"{name}: the OPTIONS expiry must come AFTER {shortcut!r}; a path "
                    "that answers without running the agent has not superseded the "
                    "pending question and must not spend its control"
                )
            # ...and it must still precede the turn itself.
            keyword = src.find("maybe_handle_keyword_command")
            assert keyword != -1 and keyword < expiry, (
                f"{name}: keyword commands dispatch elsewhere and must not expire either"
            )

    @pytest.mark.asyncio
    async def test_backfill_expires_when_the_transcript_advanced_mid_drain(
        self, tmp_path, monkeypatch
    ):
        """The seeding drain is backgrounded, so a reply can land while it runs.

        Every post awaits Slack (roughly one message per second), so the newest
        reply's control can be recorded AFTER a new turn already ran its expiry
        pass — leaving live buttons for a superseded question. The drain re-checks
        and spends it itself.
        """
        from kiro_crew.dashboard import chat_slack
        from kiro_crew.dashboard.chat_backfill import BackfillSelection

        expired: list[str] = []

        async def _record_expire(_state, session_key, ts=None):
            expired.append(session_key)

        monkeypatch.setattr(chat_slack, "expire_slack_options", _record_expire)

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        reply = {"role": "assistant", "content": "pick one [OPTIONS: A | B]"}
        slot.messages.extend([{"role": "user", "content": "hi"}, reply])

        monkeypatch.setattr(
            chat_slack,
            "select_backfill_messages",
            lambda _s, _sl: BackfillSelection(first_turn=[], recent=[[reply]], skipped_turns=0),
        )

        slack = MagicMock()
        slack.post_blocks = AsyncMock(return_value="opt-1")
        state.slack_client = slack

        # A reply lands mid-drain: the transcript grows while we are posting.
        # Bump total_messages too — that is what state.add_message does, and it is
        # the signal the drain compares.
        async def _grow(*_a, **_k):
            slot.messages.append({"role": "user", "content": "actually, neither"})
            slot.total_messages += 1
            return "p1"

        slack.post_message = _grow

        # Production links BEFORE spawning the drain (link_slack is the canonical
        # writer), so a faithful drain test starts from a linked slot.
        state.link_slack(slot.key, "thread-1", "C-1")
        await chat_slack.drain_slack_backfill(state, slot, "C-1", "thread-1")

        assert expired, "a transcript that advanced during the drain must spend the control"

    @pytest.mark.asyncio
    async def test_mid_drain_expiry_spends_only_the_control_the_drain_posted(
        self, tmp_path, monkeypatch
    ):
        """The drain's own cleanup must not spend a control a newer turn recorded.

        The turn that makes the replayed question stale can finish DURING the
        drain and record its own live control in the same slot. A session-wide
        expiry would strike that newer question through as well, silencing the
        very question the conversation is now waiting on — so the drain narrows
        its cleanup to the ts it posted itself.
        """
        from kiro_crew.dashboard import chat_slack
        from kiro_crew.dashboard.chat_backfill import BackfillSelection
        from kiro_crew.slack.outbound import PostedOptions

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        reply = {"role": "assistant", "content": "pick one [OPTIONS: A | B]"}
        slot.messages.extend([{"role": "user", "content": "hi"}, reply])

        monkeypatch.setattr(
            chat_slack,
            "select_backfill_messages",
            lambda _s, _sl: BackfillSelection(first_turn=[], recent=[[reply]], skipped_turns=0),
        )

        newer = PostedOptions(channel="C-1", ts="opt-NEWER", choices=("X",), blocks=())

        slack = MagicMock()
        slack.post_blocks = AsyncMock(return_value="opt-REPLAYED")
        edited: list[str] = []

        async def _update(channel, ts, **_kw):
            edited.append(ts)

        slack.update_message = _update
        state.slack_client = slack

        # A brand-new turn completes mid-drain: the transcript grows AND it
        # records its own live control in the same slot.
        async def _grow(*_a, **_k):
            slot.messages.append({"role": "assistant", "content": "new q [OPTIONS: X]"})
            slot.total_messages += 1
            _set_recs(state, slot, (*_recs(state, slot), newer))
            return "p1"

        slack.post_message = _grow

        # Production links BEFORE spawning the drain (link_slack is the canonical
        # writer), so a faithful drain test starts from a linked slot.
        state.link_slack(slot.key, "thread-1", "C-1")
        await chat_slack.drain_slack_backfill(state, slot, "C-1", "thread-1")

        assert "opt-REPLAYED" in edited, "the drain must spend the control it posted"
        assert "opt-NEWER" not in edited, "a newer turn's live control must NOT be struck through"
        assert newer in _recs(state, slot), "the newer control must stay tracked"

    @pytest.mark.asyncio
    async def test_backfill_renders_the_newest_reply_as_a_live_control(
        self, tmp_path, monkeypatch
    ):
        """An OPTIONS tag in replayed history must be a control, not literal text.

        Guards the headline defect of this PR against the seeding rewrite: the
        drain posts bodies as plain text, so without lifting the tag out the
        choices would arrive as the characters `[OPTIONS: A | B]`. Only the newest
        reply is answerable; an earlier one renders struck through.
        """
        from kiro_crew.dashboard import chat_slack
        from kiro_crew.dashboard.chat_backfill import BackfillSelection

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        older = {"role": "assistant", "content": "an old question [OPTIONS: X | Y]"}
        newest = {"role": "assistant", "content": "pick one [OPTIONS: A | B]"}
        typed = {"role": "user", "content": "I typed [OPTIONS: not | mine] myself"}

        monkeypatch.setattr(
            chat_slack,
            "select_backfill_messages",
            lambda _s, _sl: BackfillSelection(
                first_turn=[], recent=[[older], [typed], [newest]], skipped_turns=0
            ),
        )

        slack = MagicMock()
        slack.post_message = AsyncMock(return_value="p1")
        slack.post_blocks = AsyncMock(return_value="opt-1")
        state.slack_client = slack

        # Production links BEFORE spawning the drain (link_slack is the canonical
        # writer), so a faithful drain test starts from a linked slot.
        state.link_slack(slot.key, "thread-1", "C-1")
        await chat_slack.drain_slack_backfill(state, slot, "C-1", "thread-1")

        # Two agent tags become two control messages; the user's typed tag does not.
        assert slack.post_blocks.await_count == 2, slack.post_blocks.await_args_list
        # Only the newest is recorded, so only it can be expired later — the older
        # one was posted already spent and there is nothing to strike through.
        assert [p.ts for p in _recs(state, slot)] == ["opt-1"]
        # The user's own words survive verbatim, tag included.
        posted_text = " ".join(str(c.args[1]) for c in slack.post_message.await_args_list)
        assert "[OPTIONS: not | mine]" in posted_text

    @pytest.mark.asyncio
    async def test_backfill_redacts_credentials_inside_options_choices(
        self, tmp_path, monkeypatch
    ):
        """Choices bypass the body pipeline, so they need their own redaction.

        The body goes through _format_backfill_parts, which redacts. The choices
        go out as Block Kit label/value instead, so extracting them from raw
        history would put a credential into Slack with no redaction anywhere on
        that path.
        """
        from kiro_crew.dashboard import chat_slack
        from kiro_crew.dashboard.chat_backfill import BackfillSelection

        secret = "AKIAIOSFODNN7EXAMPLE"
        reply = {"role": "assistant", "content": f"which key? [OPTIONS: {secret} | cancel]"}

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        monkeypatch.setattr(
            chat_slack,
            "select_backfill_messages",
            lambda _s, _sl: BackfillSelection(first_turn=[], recent=[[reply]], skipped_turns=0),
        )

        slack = MagicMock()
        slack.post_message = AsyncMock(return_value="p1")
        slack.post_blocks = AsyncMock(return_value="opt-1")
        state.slack_client = slack

        # Production links BEFORE spawning the drain (link_slack is the canonical
        # writer), so a faithful drain test starts from a linked slot.
        state.link_slack(slot.key, "thread-1", "C-1")
        await chat_slack.drain_slack_backfill(state, slot, "C-1", "thread-1")

        blocks_text = json.dumps(slack.post_blocks.await_args_list[0].args[1])
        assert secret not in blocks_text, "a credential must never reach an OPTIONS control"

    @pytest.mark.asyncio
    async def test_backfill_expiry_survives_a_slot_at_the_trim_cap(self, tmp_path, monkeypatch):
        """At the message cap, a new turn does not change len(slot.messages).

        add_message trims from the front once the list exceeds
        _MAX_SLOT_MESSAGES, so a slot sitting at the cap appends and trims in the
        same step and its length is identical before and after. Comparing lengths
        would miss the advancement on exactly the busiest slots — where the race
        is most likely. total_messages is a lifetime counter, so it still moves.
        """
        from kiro_crew.dashboard import chat_slack
        from kiro_crew.dashboard.chat_backfill import BackfillSelection

        expired: list[str] = []

        async def _record_expire(_state, session_key, ts=None):
            expired.append(session_key)

        monkeypatch.setattr(chat_slack, "expire_slack_options", _record_expire)

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        reply = {"role": "assistant", "content": "pick one [OPTIONS: A | B]"}
        monkeypatch.setattr(
            chat_slack,
            "select_backfill_messages",
            lambda _s, _sl: BackfillSelection(first_turn=[], recent=[[reply]], skipped_turns=0),
        )

        slack = MagicMock()
        slack.post_blocks = AsyncMock(return_value="opt-1")
        state.slack_client = slack

        # A turn lands mid-drain on a slot AT the cap: one row in, one row out, so
        # the list length is unchanged while total_messages advances.
        async def _grow_at_cap(*_a, **_k):
            slot.messages.append({"role": "user", "content": "actually, neither"})
            del slot.messages[:1]
            slot.total_messages += 1
            return "p1"

        slack.post_message = _grow_at_cap
        before = len(slot.messages)

        # Production links BEFORE spawning the drain (link_slack is the canonical
        # writer), so a faithful drain test starts from a linked slot.
        state.link_slack(slot.key, "thread-1", "C-1")
        await chat_slack.drain_slack_backfill(state, slot, "C-1", "thread-1")

        assert len(slot.messages) == before, "the length must be unchanged for this test to bite"
        assert expired, "advancement must be detected via total_messages, not list length"

    @pytest.mark.asyncio
    async def test_backfill_spends_the_control_when_a_turn_runs_throughout(
        self, tmp_path, monkeypatch
    ):
        """A turn in flight for the WHOLE drain moves neither baseline.

        A long cron or injected turn that is already running when the drain starts
        and still running when it ends leaves slot.running identical at both ends,
        and it may not have appended a row yet — so a before/after comparison sees
        nothing. The agent is mid-reply the entire time, which is precisely when
        the replayed question is stale, so the control must still be spent.
        """
        from kiro_crew.dashboard import chat_slack
        from kiro_crew.dashboard.chat_backfill import BackfillSelection

        expired: list[str] = []

        async def _record_expire(_state, session_key, ts=None):
            expired.append(session_key)

        monkeypatch.setattr(chat_slack, "expire_slack_options", _record_expire)

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        reply = {"role": "assistant", "content": "pick one [OPTIONS: A | B]"}
        # Running before, during and after — and no new row lands. `running` is a
        # derived property (task is not None and not task.done()), so an in-flight
        # turn is simulated with a pending future rather than by assignment.
        slot.task = asyncio.get_running_loop().create_future()
        assert slot.running, "the test must actually present an in-flight turn"
        before_total = slot.total_messages

        monkeypatch.setattr(
            chat_slack,
            "select_backfill_messages",
            lambda _s, _sl: BackfillSelection(first_turn=[], recent=[[reply]], skipped_turns=0),
        )

        slack = MagicMock()
        slack.post_message = AsyncMock(return_value="p1")
        slack.post_blocks = AsyncMock(return_value="opt-1")
        state.slack_client = slack

        # Production links BEFORE spawning the drain (link_slack is the canonical
        # writer), so a faithful drain test starts from a linked slot.
        state.link_slack(slot.key, "thread-1", "C-1")
        await chat_slack.drain_slack_backfill(state, slot, "C-1", "thread-1")

        assert slot.total_messages == before_total, "no row may land for this test to bite"
        assert expired, "a turn running throughout the drain must spend the control"
        slot.task.cancel()

    def test_an_ansi_split_credential_in_a_choice_does_not_reach_slack(self):
        """A credential broken up by escapes must not survive into a choice.

        This guarantee moved owners. It used to live in this PR's own render
        helper; `slack.format.build_options_blocks` now redacts every choice
        through `redact_for_display`, which canonicalises the form Slack actually
        DISPLAYS (ANSI, emphasis, backticks, link markup) before scanning. That
        is strictly stronger, so the test asserts against the real owner rather
        than keeping a second copy of the pipeline alive to test.
        """
        from kiro_crew.slack.format import strip_ansi

        # An AWS key broken up by a colour escape mid-token.
        secret = "AKIA\x1b[0mIOSFODNN7EXAMPLE"
        blocks = build_options_blocks([secret, "cancel"])

        rendered = strip_ansi(json.dumps(blocks))
        # Assert on the ANSI-STRIPPED render: leaving the escape in is not a
        # defence, because whatever shows the choice strips it and the reader
        # sees the key whole. Asserting on the raw bytes would pass vacuously --
        # the escape splits the token, so the contiguous secret is absent either
        # way.
        assert AWS_KEY not in rendered

    @pytest.mark.asyncio
    async def test_expiry_marks_the_control_terminal(self, tmp_path):
        """A click queued behind a successful expiry must not dispatch.

        The once-only claim defends click-against-click. It does not, on its own,
        defend click-against-completed-expiry: a Send queued behind the expiry
        would find the claim unheld, take it, and answer the very question the
        expiry just struck through. The expiry therefore marks the control terminal
        while it still holds the message's edit lock, so the queued click is refused
        by exactly the check a duplicate click hits.
        """
        from kiro_crew.dashboard.chat_utils import (
            expire_slack_options,
            remember_slack_options,
        )
        from kiro_crew.slack.outbound import claim_options_answer

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        posted = PostedOptions(channel="C-1", ts="opt-1", choices=("A",), blocks=())
        remember_slack_options(state, slot.key, posted)

        slack = MagicMock()
        slack.update_message = AsyncMock()
        state.slack_client = slack

        await expire_slack_options(state, slot.key)

        assert claim_options_answer("C-1", "opt-1") is False, (
            "a click queued behind a settled expiry must be refused -- otherwise it "
            "answers the question the expiry just retired"
        )
        assert _recs(state, slot) == ()

    def test_a_submission_that_rendered_nothing_gives_the_claim_back(self):
        """A wholly failed submit must not consume the answer forever.

        If neither the in-place edit nor the replacement post lands, nothing
        happened: the buttons are still on screen. Holding the claim would refuse
        every retry, leaving a control permanently visible and permanently
        unanswerable. Once the selection IS rendered the claim stays, even if a
        later step stumbles.
        """
        import inspect

        from kiro_crew.slack import interactions

        src = inspect.getsource(interactions._handle_options_submit)
        assert "release_options_answer(channel, msg_ts)" in src, (
            "a submit that rendered nothing must give the claim back"
        )
        rel = src.find("release_options_answer(channel, msg_ts)")
        guard = src[max(0, rel - 200) : rel]
        assert "not edited and new_ts == msg_ts" in guard, (
            "the rollback must be gated on NOTHING having been rendered -- releasing "
            "after a successful edit would re-admit a duplicate click"
        )
        assert "finally:" in src[max(0, rel - 600) : rel], (
            "the rollback must run on the abort path too, which returns early"
        )

    def test_all_three_posting_paths_resolve_the_live_owner(self):
        """The dashboard mirror path must do what the other two already do.

        Rounds 23 and 26 gave the native footer and the transport path an
        owner-resolved record plus owner-change supersession. The dashboard mirror
        in ``chat_runner`` recorded under the bare ``session_key`` -- so a thread
        relinked while ``post_blocks`` was in flight got its control filed where
        the new owner's expiry never looks, and clickable into a conversation it
        does not belong to.
        """
        import inspect

        from kiro_crew.dashboard import chat_runner

        src = inspect.getsource(chat_runner)
        mirror = src[src.find("_mirror_blocks = build_options_blocks(") :][:2600]
        assert "_pre_owner" in mirror, "the mirror path must capture the owner before posting"
        assert "remember_slack_options(\n                            state,\n                            _owner," in mirror, (
            "the record must use the re-resolved owner, not the key the turn started with"
        )
        assert "_owner != _pre_owner" in mirror, "an owner change must supersede"
        assert "ts=_mirror_ts" in mirror, (
            "the supersession expiry must be narrowed to OUR ts, or it strikes "
            "through a control the new owner recorded meanwhile"
        )

    def test_the_forget_uses_owner_keys_snapshotted_before_the_edit(self):
        """A relink during the submit's edit must not orphan the old owner's record.

        ``forget_slack_options_for_thread`` resolved its key list when called --
        after the edit. A relink landing during that edit moves the thread, so the
        recomputed list names the NEW owner, the previous owner's record survives
        the click, and that session's next turn edits over the selection.
        """
        import inspect

        from kiro_crew.dashboard.chat_utils import (
            slack_options_owner_keys_snapshot,
        )
        from kiro_crew.slack import interactions

        state = MagicMock()
        state._slots = {}
        state.get_linked_slot = MagicMock(return_value=None)
        state.sessions.get_session_for_thread = MagicMock(return_value="dashboard:chat-A")
        before = slack_options_owner_keys_snapshot(state, "thread-1")
        assert "dashboard:chat-A" in before

        # The thread moves to B. The snapshot must still name A.
        state.sessions.get_session_for_thread = MagicMock(return_value="dashboard:chat-B")
        after = slack_options_owner_keys_snapshot(state, "thread-1")
        assert "dashboard:chat-B" in after and "dashboard:chat-A" not in after, (
            "coherence check: resolving after the relink names only the new owner"
        )

        src = inspect.getsource(interactions._handle_options_submit)
        snap = src.find("slack_options_owner_keys_snapshot(")
        edit = src.find("update_message(")
        forget = src.find("_forget_options_control(")
        assert snap != -1 and snap < edit, "the snapshot must be taken BEFORE the edit"
        assert "keys=_owner_keys" in src[forget : forget + 120], (
            "the forget must consume the pre-edit snapshot, not re-resolve"
        )

    @pytest.mark.asyncio
    async def test_a_cancelled_unlink_puts_the_link_back(self, tmp_path, monkeypatch):
        """Shutdown mid-expiry must not leave the unlink half-committed.

        Persistence is cleared at the TOP of the handler and the in-memory fields
        are only touched at the end, so a cancellation during the expiry await
        leaves the two disagreeing: after a restart the routing is gone while the
        controls are still live on screen, and a click resolves to nothing and
        starts a fresh session.
        """
        from kiro_crew.dashboard import chat_slack
        from kiro_crew.dashboard.chat_slack import api_chat_slot_slack_unlink
        from kiro_crew.dashboard.chat_utils import effective_session_key

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slack = MagicMock()
        slack.update_message = AsyncMock(return_value=True)
        state.slack_client = slack
        state.link_slack(slot.key, "thread-1", "C-1")
        key = effective_session_key(slot)
        assert state.sessions.get_slack_link(key)[0] == "thread-1", "precondition: linked"

        async def _cancelled_expiry(*_a, **_k):
            raise asyncio.CancelledError()

        monkeypatch.setattr(chat_slack, "expire_slack_options", _cancelled_expiry)

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/slack-unlink", api_chat_slot_slack_unlink)
        async with TestClient(TestServer(app)) as client:
            # A propagated CancelledError tears the connection down rather than
            # producing a response -- that disconnect IS the handler re-raising
            # instead of swallowing it, which is half of what this test asserts.
            with pytest.raises(Exception) as excinfo:
                await client.post(f"/api/chat/slots/{slot.key}/slack-unlink")
            assert "disconnect" in str(excinfo.value).lower() or isinstance(
                excinfo.value, asyncio.CancelledError
            ), f"expected the cancellation to propagate, got {excinfo.value!r}"

        assert state.sessions.get_slack_link(key)[0] == "thread-1", (
            "a cancelled unlink must leave the persisted link exactly as it found it"
        )

    @pytest.mark.asyncio
    async def test_eviction_never_splits_one_message_lock(self):
        """A released lock with a scheduled waiter reads as UNLOCKED.

        Evicting it there hands the next caller a brand-new lock for the same
        message while the waiter proceeds on the old one, so two coroutines edit
        one Slack message at once and every guarantee built on this lock is off.
        """
        from kiro_crew.slack import outbound

        key = ("CH-split", "msg-split")
        inside: list[str] = []
        overlap: list[str] = []

        async def _worker(name: str, hold: asyncio.Event) -> None:
            async with outbound.options_edit_lock(*key):
                if inside:
                    overlap.append(name)
                inside.append(name)
                await hold.wait()
                inside.remove(name)

        a_hold, b_hold = asyncio.Event(), asyncio.Event()
        a = asyncio.create_task(_worker("A", a_hold))
        await asyncio.sleep(0)
        # B is now a WAITER on the same lock.
        b = asyncio.create_task(_worker("B", b_hold))
        await asyncio.sleep(0)

        # Flood the registry past its cap while B waits, then let A go so the lock
        # is released with B still only scheduled -- the exact split window.
        for i in range(outbound._MAX_EDIT_LOCKS + 20):
            outbound.options_edit_lock("CH-bulk", f"t{i}")
        a_hold.set()
        await asyncio.sleep(0)
        for i in range(outbound._MAX_EDIT_LOCKS + 20):
            outbound.options_edit_lock("CH-bulk", f"u{i}")

        assert key in outbound._EDIT_LOCKS, (
            "a lock with a pending waiter must never be evicted"
        )
        b_hold.set()
        await asyncio.gather(a, b)
        assert overlap == [], f"two coroutines held one message lock at once: {overlap}"
        # And once nobody wants it the entry is reclaimable again.
        assert not outbound._LOCK_USERS.get(key)

    def test_send_message_options_use_the_safe_fallback_stub(self):
        """The `send_message` control must not carry the agent's body as fallback.

        Slack parses entities in a message's top-level ``text``, which is what
        notifications render, so an agent-authored body containing `<!channel>`
        would ping the whole channel -- and the expiry replays the STORED text on
        its edit, so it would ping again on every retirement. Nothing is lost by
        using the stub: the body is already posted as its own Slack message
        immediately above, so on the control it was pure duplication.
        """
        import inspect

        from kiro_crew.dashboard.handlers import messaging

        src = inspect.getsource(messaging)
        at = src.find("option_blocks = build_options_blocks(options)")
        assert at != -1, "the send_message OPTIONS post should be findable"
        window = src[at : at + 1200]
        assert "OPTIONS_FALLBACK_TEXT," in window, (
            "the control's post-time fallback must be the safe stub"
        )
        assert "text=text," not in window, (
            "and the stored record must not carry the raw body -- the expiry "
            "replays it as top-level text on every edit"
        )
        # The body itself still reaches Slack, as its own message.
        assert "post_message(" in src, "the message body is still posted normally"

    def test_a_local_dashboard_command_does_not_spend_the_control(self):
        """`/goal` and `/prompts` return without an agent turn.

        Round 31 moved each SLACK entry point's expiry below its short-circuits;
        the dashboard path kept its expiry at the very top of ``_run_chat``, so a
        local command that never starts a turn still struck a pending question
        through -- leaving valid choices unanswerable with nothing on the way to
        answer them.
        """
        import inspect

        from kiro_crew.dashboard import chat_runner

        src = inspect.getsource(chat_runner._run_chat)
        expiry = src.find("await expire_slack_options(state, session_key)")
        assert expiry != -1, "the dashboard turn must still expire the control"
        for local in ('if first_word == "/goal"', 'if first_word == "/prompts"'):
            at = src.find(local)
            assert at != -1, f"expected {local} in _run_chat"
            assert at < expiry, (
                f"{local} returns without a turn, so it must sit ABOVE the expiry"
            )
        assert expiry < src.find("_acquired = False"), (
            "the expiry must still run before the turn is acquired"
        )

    @pytest.mark.asyncio
    async def test_linking_an_existing_thread_retires_its_prior_control(self, tmp_path):
        """Reassigning a thread must not strand the previous owner's buttons.

        A live control is recorded under the thread's own conversation key. Linking
        a dashboard session to that existing thread moves the thread -> slot index,
        so no dashboard turn would ever expire that record and a click on those
        buttons would answer into the dashboard session instead.
        """
        from kiro_crew.dashboard.chat_slack import api_chat_slot_slack_link

        state = _make_state(tmp_path)
        slack = MagicMock()
        slack.update_message = AsyncMock(return_value=True)
        slack.post_message = AsyncMock(return_value="thread-1")
        state.slack_client = slack
        state.owner_id = "U-owner"

        prior = state.get_or_create_slot("slack-prior")
        state.link_slack(prior.key, "thread-1", "C-1")
        _set_recs(
            state,
            prior,
            (PostedOptions(channel="C-1", ts="opt-1", choices=("A", "B"), blocks=()),),
        )

        dash = state.get_or_create_slot("s1")
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/slack-link", api_chat_slot_slack_link)
        async with TestClient(TestServer(app)) as client:
            await client.post(
                f"/api/chat/slots/{dash.key}/slack-link",
                json={"channel": "C-1", "thread_ts": "thread-1"},
            )

        assert slack.update_message.await_count >= 1, (
            "the previous owner's control must be struck through on reassign"
        )
        assert _recs(state, prior) == (), (
            "and it must not stay tracked under a key nothing will ever expire"
        )

    def test_link_captures_the_prior_owner_before_reassigning(self):
        """The keys must be read BEFORE link_slack moves the index."""
        import inspect

        from kiro_crew.dashboard import chat_slack

        src = inspect.getsource(chat_slack.api_chat_slot_slack_link)
        snap = src.find("_prior_owner_keys = slack_options_owner_keys_snapshot(")
        link = src.find("state.link_slack(slot.key, thread_ts, target_channel)")
        assert snap != -1 and link != -1, "both the snapshot and the link must be present"
        assert snap < link, (
            "resolving after link_slack names the NEW owner, so the previous "
            "conversation's record would never be found"
        )
        assert "_own_keys" in src, (
            "re-linking a thread to the slot that already holds it must not spend "
            "that slot's own live control"
        )

    @pytest.mark.asyncio
    async def test_a_different_thread_relink_during_the_expiry_survives(
        self, tmp_path, monkeypatch
    ):
        """A relink to a DIFFERENT thread during the expiry must not be torn down.

        The existing round-27 guard covers a same-thread relink; this is the other
        branch, where the slot's in-memory fields moved too. Naming this honestly:
        it is the compare-and-clear-free path -- what protects the replacement is
        the compare of the slot's live fields against the ones captured before the
        await, not any check inside the clear.
        """
        from kiro_crew.dashboard import chat_slack
        from kiro_crew.dashboard.chat_slack import api_chat_slot_slack_unlink
        from kiro_crew.dashboard.chat_utils import effective_session_key

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slack = MagicMock()
        slack.update_message = AsyncMock(return_value=True)
        state.slack_client = slack
        state.link_slack(slot.key, "thread-1", "C-1")
        key = effective_session_key(slot)

        async def _relink_mid_expiry(*_a, **_k):
            state.link_slack(slot.key, "thread-2", "C-2")

        monkeypatch.setattr(chat_slack, "expire_slack_options", _relink_mid_expiry)

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/slack-unlink", api_chat_slot_slack_unlink)
        async with TestClient(TestServer(app)) as client:
            await client.post(f"/api/chat/slots/{slot.key}/slack-unlink")

        assert state.sessions.get_slack_link(key)[0] == "thread-2", (
            "the replacement written during the expiry await must survive"
        )

    def test_link_mutations_stay_serialized_on_the_event_loop(self):
        """Link writes must NOT be moved off-loop, in any path.

        An earlier revision of this PR ran the clear in ``asyncio.to_thread`` to
        keep a whole-session-map save off the loop. That was wrong twice over: the
        worker runs CONCURRENTLY with the loop, so the compare-and-clear inside it
        was never atomic against a loop-side relink -- the thread could read the
        captured link, a relink could write its replacement, and the thread would
        then clear that replacement. The session map has no cross-thread lock, so
        the event loop is the only thing serialising its writers.
        """
        import inspect

        from kiro_crew.dashboard import chat_slack

        src = inspect.getsource(chat_slack.api_chat_slot_slack_unlink)
        assert "asyncio.to_thread(" not in src, (
            "no link mutation in the unlink handler may run off the loop"
        )
        assert "cleared = _clear_persisted_link_sync()" in src, (
            "the clear runs inline, on the loop"
        )
        cancel = src[src.find("except asyncio.CancelledError:") :]
        assert "_restore_persisted_link_sync()" in cancel and "asyncio.shield(" not in cancel, (
            "the cancellation restore is a plain inline write -- the shield and its "
            "fallback existed only to serve the off-loop rewrite"
        )

    @pytest.mark.asyncio
    async def test_unlink_waits_for_an_answer_that_is_still_routing(self):
        """A spent control is forgotten BEFORE its answer has found a session.

        The click's own success is what opens this window: it drops the record --
        the unlink handler's other signal -- and dispatches the answer as a task.
        An unlink landing between the two sees nothing tracked, pops the thread
        from the reverse index, and the user's selection arrives at a brand-new
        Slack session. Same corruption the unlink ordering was built to prevent,
        reached from the other side.
        """
        from kiro_crew.slack import outbound

        assert outbound.answer_routing_in_flight("thread-1") is False

        started = asyncio.Event()
        release = asyncio.Event()

        async def _routing() -> None:
            started.set()
            await release.wait()

        task = asyncio.create_task(_routing())
        outbound.track_answer_routing("thread-1", task)
        await started.wait()

        assert outbound.answer_routing_in_flight("thread-1") is True, (
            "while the answer is routing the thread must stay linked"
        )
        assert outbound.answer_routing_in_flight("other-thread") is False, (
            "and only THAT thread -- an unrelated unlink must not be blocked"
        )

        release.set()
        await task
        assert outbound.answer_routing_in_flight("thread-1") is False, (
            "once routed the hold must lift, or the unlink refuses forever"
        )

    @pytest.mark.asyncio
    async def test_a_failed_answer_task_does_not_pin_the_thread_forever(self):
        """Registration is by task so nothing can leak.

        A counter incremented at dispatch and decremented on success would, on a
        raising or cancelled task, leave the thread permanently unlinkable -- a
        worse bug than the one being fixed. A task that ends in ANY way stops
        counting by itself.
        """
        from kiro_crew.slack import outbound

        async def _boom() -> None:
            raise RuntimeError("routing blew up")

        task = asyncio.create_task(_boom())
        outbound.track_answer_routing("thread-2", task)
        with pytest.raises(RuntimeError):
            await task
        assert outbound.answer_routing_in_flight("thread-2") is False
        assert "thread-2" not in outbound._ANSWER_ROUTING, "and the entry is reclaimed"

        cancelled = asyncio.create_task(asyncio.Event().wait())
        outbound.track_answer_routing("thread-3", cancelled)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert outbound.answer_routing_in_flight("thread-3") is False

    def test_the_unlink_guard_consults_the_routing_signal(self):
        """The abort predicate must ask BOTH questions.

        A tracked record covers a control still on screen; the routing signal
        covers a control already spent whose answer has not landed. Dropping
        either leaves one half of the window open.
        """
        import inspect

        from kiro_crew.dashboard import chat_slack

        src = inspect.getsource(chat_slack)
        guard = [
            ln
            for ln in src.splitlines()
            if "options_records(state, session_key)" in ln and ln.strip().startswith("if ")
        ]
        assert guard, "the unlink abort guard should be findable"
        assert any("answer_routing_in_flight(prev_thread_ts)" in ln for ln in guard), (
            "the unlink must also abort while an answer is still routing"
        )

    def test_both_click_paths_register_their_dispatch(self):
        """Neither click path may dispatch an answer the unlink cannot see."""
        import inspect

        from kiro_crew.slack import interactions

        for fn in (interactions._handle_options_submit, interactions._handle_options):
            src = inspect.getsource(fn)
            dispatch = src.find("_orch._handler_tasks.add(t)")
            assert dispatch != -1, f"{fn.__name__} should dispatch a task"
            window = src[max(0, dispatch - 400) : dispatch]
            assert "track_answer_routing(thread_ts, t)" in window, (
                f"{fn.__name__} must register its dispatch before handing it off"
            )

    def test_the_legacy_click_escapes_its_slack_fallback_text_too(self):
        """Round 35 escaped the submit path's fallback; the legacy path was missed.

        Slack parses entities in a message's top-level ``text``, which is what
        notifications render, so a legacy choice containing ``<!channel>`` would
        ping the whole channel from one click. The answer echoed into the session
        must stay RAW -- only the fallback is escaped.
        """
        import inspect

        from kiro_crew.slack import interactions

        src = inspect.getsource(interactions._handle_options)
        tail = src[src.find("Standard OPTIONS choice") :]
        assert "_choice_fallback = escape_mrkdwn(choice)" in tail, (
            "the legacy path must escape its fallback text"
        )
        assert "text=choice" not in tail, "the raw choice must not reach the top-level text"
        assert "selected_blocks, choice," not in tail, (
            "nor the post_blocks fallback argument"
        )
        # The dispatched answer stays raw: escaping there would corrupt what the
        # session receives.
        dispatch = tail[tail.find("handle_message(") :]
        assert "_choice_fallback" not in dispatch, (
            "the turn must receive the raw choice, not the escaped fallback"
        )

    def test_the_legacy_single_click_path_is_claimed_once_too(self):
        """A legacy control renders into the same message and dispatches a turn.

        ``_handle_options`` is the older path where each choice is its own button
        and the click itself is the answer -- no Send. It had none of the submit
        path's protection, so two rapid clicks meant two handler tasks, two
        renders of the same message and two turns injected. Every guarantee the
        submit path has this path needs, for the same reasons.
        """
        import inspect

        from kiro_crew.slack import interactions

        src = inspect.getsource(interactions._handle_options)
        # Locate the standard-OPTIONS section; the action-button branches above it
        # return early and are a different feature.
        tail = src[src.find("Standard OPTIONS choice") :]
        assert "async with options_edit_lock(channel, msg_ts):" in tail, (
            "the render must serialise against the turn-start expiry's edit"
        )
        claim = tail.find("claim_options_answer(channel, msg_ts)")
        edit = tail.find("update_message(")
        assert claim != -1 and claim < edit, "the claim must be taken BEFORE the first edit"
        assert "keys=_owner_keys" in tail, "the forget must use the pre-edit owner snapshot"
        assert "settle_options_answer(channel, msg_ts)" in tail, (
            "and must settle the claim once the buttons are provably gone"
        )
        assert "release_options_answer(channel, msg_ts)" in tail, (
            "a render that reached Slack not at all must give the claim back"
        )

    @pytest.mark.asyncio
    async def test_backfill_spends_its_control_when_the_link_is_already_gone(
        self, tmp_path, monkeypatch
    ):
        """Link then immediate unlink must not leave live buttons behind.

        The unlink removes the routing before the drain finishes posting, so a
        click on the replayed control would start a FRESH Slack session and answer
        a question that session never asked. Round 32's unlink abort covers the
        other order -- a control already tracked when the unlink arrives -- and had
        nothing to abort on here, because the record did not exist yet.
        """
        from kiro_crew.dashboard import chat_slack
        from kiro_crew.dashboard.chat_backfill import BackfillSelection

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        newest = {"role": "assistant", "content": "pick one [OPTIONS: A | B]"}
        monkeypatch.setattr(
            chat_slack,
            "select_backfill_messages",
            lambda _s, _sl: BackfillSelection(
                first_turn=[], recent=[[newest]], skipped_turns=0
            ),
        )
        slack = MagicMock()
        slack.post_message = AsyncMock(return_value="p1")
        slack.post_blocks = AsyncMock(return_value="opt-1")
        slack.update_message = AsyncMock(return_value=True)
        state.slack_client = slack

        # Linked, then unlinked while the drain is in flight: the slot's link now
        # points somewhere else entirely.
        state.link_slack(slot.key, "thread-1", "C-1")
        slot._slack_channel = ""
        slot._slack_thread_ts = ""

        await chat_slack.drain_slack_backfill(state, slot, "C-1", "thread-1")

        assert slack.update_message.await_count == 1, (
            "a control posted into a thread the slot no longer owns must be "
            "struck through, not left clickable"
        )
        assert [p.ts for p in _recs(state, slot)] == [], (
            "and it must not stay tracked as a live control"
        )

    def test_an_unsettled_answer_claim_is_never_evicted(self):
        """The claim map's bound must not re-admit a click on live buttons.

        Eviction is by insertion order across the WHOLE workspace, so traffic in
        busy channels could drop the claim on a control still sitting unanswered
        in a quiet thread. When the render or the strike-through failed those
        buttons really are still on screen, and this entry is the only thing
        standing between a second click and a duplicate turn.
        """
        from kiro_crew.slack import outbound

        cap = outbound._MAX_EDIT_LOCKS
        # One claim whose buttons may still be live -- never settled.
        assert outbound.claim_options_answer("C-live", "live-1") is True
        # Then flood well past the cap with controls that DID settle.
        for i in range(cap + 50):
            assert outbound.claim_options_answer("C-bulk", f"t{i}") is True
            outbound.settle_options_answer("C-bulk", f"t{i}")

        assert ("C-live", "live-1") in outbound._ANSWERED, (
            "the unsettled claim must survive the flood -- evicting it re-admits "
            "a click on a control still showing buttons"
        )
        assert outbound.claim_options_answer("C-live", "live-1") is False, (
            "and the surviving claim must still refuse a second click"
        )
        assert len(outbound._ANSWERED) <= cap + 1, (
            "settled entries must still be reclaimed, or the cap means nothing"
        )

    def test_a_settled_claim_is_reclaimable(self):
        """Settling is what makes the bound work at all.

        A claim settles once the buttons are provably gone -- the in-place edit
        landed, the original was deleted, or the expiry's strike-through
        succeeded. Only then may memory pressure reclaim it.
        """
        from kiro_crew.slack import outbound

        assert outbound.claim_options_answer("C1", "s1") is True
        assert outbound._ANSWERED[("C1", "s1")] is False
        outbound.settle_options_answer("C1", "s1")
        assert outbound._ANSWERED[("C1", "s1")] is True
        # mark_options_terminal only runs after a confirmed strike-through, so it
        # records a settled entry directly.
        outbound.mark_options_terminal("C2", "s2")
        assert outbound._ANSWERED[("C2", "s2")] is True

    def test_a_live_control_is_never_evicted_to_bound_the_store(self):
        """No cap may drop a record for a control that is still clickable.

        An earlier revision capped the store with FIFO eviction. That is worse than
        unbounded: evicting a live record means no later turn can retire that
        control, which is exactly the untracked control this lifecycle exists to
        eliminate -- so the bound would reintroduce the defect at scale, silently,
        on the busiest instances. The only bound is the lifecycle itself: a key
        exists while a question is unanswered and is pruned the moment it is not.
        """
        import inspect

        from kiro_crew.dashboard import chat_utils
        from kiro_crew.dashboard.chat_utils import options_records, set_options_records

        src = inspect.getsource(chat_utils.set_options_records)
        assert "_MAX_OPTION_KEYS" not in src and "del store[" not in src, (
            "the store must not evict live records to stay small"
        )

        state = MagicMock()
        state._slack_options_by_key = {}
        posted = PostedOptions(channel="C-1", ts="opt-1", choices=("A",), blocks=())
        for i in range(1200):
            set_options_records(state, f"slack:{i}", (posted,))
        assert options_records(state, "slack:0") == (posted,), (
            "the oldest live control must still be tracked after 1200 sessions"
        )
        # ...and an emptied key is still pruned, so the store is not a leak.
        set_options_records(state, "slack:0", ())
        assert options_records(state, "slack:0") == ()
        assert "slack:0" not in state._slack_options_by_key

    def test_the_store_is_not_held_on_the_slot(self):
        """Structural: one store, keyed by session key, on DashboardState.

        A slot-held record was the #1694 defect: a plain Slack thread has no slot,
        so the control was dropped and no later turn could expire it. It also must
        not be a slot field PLUS a keyed fallback -- a slot can come into existence
        at any moment (the channel-surface reconciler creates one), so a fallback
        map would go invisible the instant one appeared, and two stores are how a
        record gets filed under one index and cleared under another.
        """
        import inspect

        from kiro_crew.dashboard import chat_utils
        from kiro_crew.dashboard import state as state_mod

        assert "_slack_options_posted" not in inspect.getsource(state_mod), (
            "the slot-held OPTIONS field must be gone, not shadowed by a fallback"
        )
        utils_src = inspect.getsource(chat_utils)
        assert "_slack_options_by_key" in utils_src
        assert "_slack_options_posted" not in utils_src, (
            "every accessor must go through the keyed store"
        )

    @pytest.mark.asyncio
    async def test_a_slotless_thread_gets_the_whole_lifecycle(self, tmp_path):
        """End to end for the case #1694 was filed about.

        Record, expire and forget must all work for a session that has no dashboard
        slot at any point -- which is the normal state of a plain Slack thread.
        """
        from kiro_crew.dashboard.chat_utils import (
            expire_slack_options,
            forget_slack_options,
            options_records,
            remember_slack_options,
        )

        state = _make_state(tmp_path)
        state._slots = {}
        state.get_linked_slot = MagicMock(return_value=None)
        slack = MagicMock()
        slack.update_message = AsyncMock()
        state.slack_client = slack

        key = "slack:1785370133.085469"
        first = PostedOptions(channel="C-1", ts="opt-1", choices=("A",), blocks=())
        second = PostedOptions(channel="C-1", ts="opt-2", choices=("B",), blocks=())

        remember_slack_options(state, key, first)
        remember_slack_options(state, key, second)
        assert options_records(state, key) == (first, second), (
            "a slotless session must accumulate every outstanding control"
        )

        forget_slack_options(state, key, "opt-1")
        assert options_records(state, key) == (second,), "the forget must be ts-scoped"

        await expire_slack_options(state, key)
        assert slack.update_message.await_count == 1, (
            "the remaining control must actually be struck through"
        )
        assert options_records(state, key) == ()

    def test_only_the_first_click_on_a_control_is_answered(self):
        """A second Send on the same message must dispatch nothing.

        Two rapid clicks produce two handler tasks. Both serialize on
        ``options_edit_lock``, so without a once-only claim the first renders the
        selection and dispatches it and the second dispatches the same answer
        again -- a duplicate turn, or a superseded one once the first has moved on.

        The claim is deliberately NOT "is a record still tracked":
        ``remember_slack_options`` no-ops when the session has no dashboard slot,
        which is the normal state for a plain Slack conversation, so keying
        validity on record presence would reject every legitimate click there.
        """
        from kiro_crew.slack.outbound import claim_options_answer

        assert claim_options_answer("C-1", "opt-1") is True
        assert claim_options_answer("C-1", "opt-1") is False, (
            "a second click on the same control must not be answered again"
        )
        # A different control in the same channel is unaffected.
        assert claim_options_answer("C-1", "opt-2") is True
        # ...and so is the same ts in a different channel.
        assert claim_options_answer("C-2", "opt-1") is True

    def test_the_duplicate_click_is_dropped_before_any_side_effect(self):
        """Structural: the claim gates the edit, the forget AND the dispatch.

        Claiming after the edit would still leave two messages rewritten and two
        turns dispatched; claiming outside the lock would let both clicks pass the
        check before either set it.
        """
        import inspect

        from kiro_crew.slack import interactions

        src = inspect.getsource(interactions._handle_options_submit)
        lock_at = src.find("async with options_edit_lock(")
        claim_at = src.find("if not claim_options_answer(")
        edit_at = src.find("update_message(")
        assert lock_at != -1 and claim_at != -1 and edit_at != -1
        assert lock_at < claim_at < edit_at, (
            "the claim must sit INSIDE the lock and BEFORE the first edit, so a "
            "duplicate click touches nothing"
        )

    def test_the_slack_fallback_text_is_escaped_but_the_answer_is_not(self):
        """A mention must not survive into the message's top-level `text`.

        Round 28 escaped the mrkdwn BLOCKS. Slack parses entities in a message's
        `text` argument too -- and `text` is what notifications and block-less
        clients show -- so the submit handler handing the raw selection to
        `update_message` / `post_blocks` left the notification path open even
        though the rendered blocks were safe.

        The escape must NOT touch the copy echoed back into the session: that is
        the answer the user picked, and escaping it would change it.
        """
        import inspect

        from kiro_crew.slack import interactions

        src = inspect.getsource(interactions._handle_options_submit)

        assert "combined_fallback = escape_mrkdwn(combined)" in src, (
            "the Slack-facing fallback text must be escaped"
        )
        assert "text=combined_fallback" in src, "update_message must use the escaped text"
        assert "selected_blocks, combined_fallback" in src, (
            "the post_blocks fallback must use the escaped text"
        )
        assert "text=combined," not in src, "no raw selection may reach Slack as text"
        # ...and the agent-facing answer stays verbatim.
        assert "[OPTIONS multi-select: {combined}]" in src, (
            "the answer echoed into the session must stay raw -- escaping it would "
            "change what the user picked"
        )

    def test_a_channel_mention_in_a_choice_is_inert_in_the_mrkdwn_summary(self):
        """A choice cannot notify a channel just by being rendered.

        Choice text is LLM-authored, so it can carry Slack's own entity syntax.
        The summary that backfill, submit and expiry all render is a ``mrkdwn``
        field, where ``<!channel>`` is INTERPRETED -- so an OPTIONS tag reading
        ``[OPTIONS: <!channel> | Skip]`` would page a whole channel on render.
        """
        from kiro_crew.slack.format import build_options_selected_blocks

        blocks = build_options_selected_blocks(["<!channel>", "Skip & go <b>"], [0])
        text = blocks[0]["elements"][0]["text"]

        assert "<!channel>" not in text, "a raw channel mention must never reach mrkdwn"
        assert "&lt;!channel&gt;" in text
        # `&` first, or the earlier substitutions get re-escaped into `&amp;lt;`.
        assert "&amp;lt;" not in text
        assert "Skip &amp; go &lt;b&gt;" in text

    def test_escaping_does_not_leak_into_the_label_or_the_submit_value(self):
        """The escape belongs to the mrkdwn sink only.

        ``plain_text`` is not interpreted by Slack, so escaping there would show
        a literal ``&lt;``; and the button ``value`` is echoed back into the
        session on submit, so escaping it would change the answer the user
        picked. Guards against "fixing" this in the shared `_redact_choices`.
        """
        choice = "Skip & go <b>"
        blocks = build_options_blocks([choice])
        opt = blocks[0]["elements"][0]["options"][0]

        assert opt["text"]["text"] == choice, "plain_text labels must stay unescaped"
        assert opt["value"] == choice, "the submit value must round-trip verbatim"
