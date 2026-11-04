"""A channel pinned to an agent is served only by that agent.

A dashboard slot running another agent must not be bound to that channel's
existing thread (slack-link with thread_ts, or the Slack-side import), and a
reply in a thread already linked to such a slot is stopped with a notice: it
reaches neither the slot nor normal routing, which would resolve the same
thread owner.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

PINNED = "C0PINNED"
OPEN = "C0OPEN"


@pytest.fixture(autouse=True)
def _no_execution_record(_floor_monkeypatch):
    """No session in these tests has an execution record unless one sets it."""
    _floor_monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution", lambda *_a, **_k: None
    )


def _cfg(agent_for_pinned: str = "restricted-agent"):
    def channel_config(channel_id: str):
        return SimpleNamespace(agent=agent_for_pinned if channel_id == PINNED else None)

    return SimpleNamespace(channel_config=channel_config)


def _patches(cfg=None, aliases=(), default_template="kirocrew", ready=True):
    """Patch the channel config and the published crewmate alias table."""
    return (
        patch("kiro_crew.slack.handler_runtime.access.slack_cfg", return_value=cfg or _cfg()),
        patch(
            "kiro_crew.config.loader_snapshots.agent_alias_snapshot",
            return_value=(frozenset(aliases), default_template, ready),
        ),
    )


class TestChannelAgentMismatch:
    def test_cases(self):
        from kiro_crew.slack.handler import channel_agent_mismatch

        a, b = _patches()
        with a, b:
            assert channel_agent_mismatch(PINNED, "") == "restricted-agent"
            assert channel_agent_mismatch(PINNED, "kirocrew") == "restricted-agent"
            assert channel_agent_mismatch(PINNED, "restricted-agent") is None
            assert channel_agent_mismatch(PINNED, "restricted-agent", "template") is None
            assert channel_agent_mismatch(OPEN, "") is None
            assert channel_agent_mismatch("D123", "") is None
            assert channel_agent_mismatch("dm", "") is None
            assert channel_agent_mismatch("", "") is None

    def test_unnamed_slot_resolves_to_the_default_template(self):
        from kiro_crew.slack.handler import channel_agent_mismatch

        a, b = _patches(cfg=_cfg("kirocrew"), default_template="kirocrew")
        with a, b:
            assert channel_agent_mismatch(PINNED, "") is None

    def test_template_and_crewmate_of_one_name_are_different_agents(self):
        from kiro_crew.slack.handler import channel_agent_mismatch

        # The channel names a crewmate; a slot that picked the same name as a
        # template runs the template, so it is not the channel's agent.
        a, b = _patches(cfg=_cfg("kirocrew"), aliases={"kirocrew"})
        with a, b:
            assert channel_agent_mismatch(PINNED, "kirocrew", "template") == "kirocrew"
            assert channel_agent_mismatch(PINNED, "kirocrew", "member") is None
            assert channel_agent_mismatch(PINNED, "kirocrew", "") is None
            # An unnamed slot is the default template, never the crewmate.
            assert channel_agent_mismatch(PINNED, "") == "kirocrew"
        # The channel names a template; a crewmate pick of that name is not it.
        a, b = _patches(cfg=_cfg("tpl"), aliases=set())
        with a, b:
            assert channel_agent_mismatch(PINNED, "tpl", "member") is not None
            assert channel_agent_mismatch(PINNED, "tpl", "template") is None

    def test_config_error_fails_closed(self):
        from kiro_crew.slack.handler import channel_agent_mismatch

        with patch(
            "kiro_crew.slack.handler_runtime.access.slack_cfg", side_effect=RuntimeError("x")
        ):
            assert channel_agent_mismatch(PINNED, "restricted-agent") is not None

    def test_unpublished_alias_table_fails_closed(self):
        from kiro_crew.slack.handler import channel_agent_mismatch

        a, b = _patches(ready=False)
        with a, b:
            assert channel_agent_mismatch(PINNED, "restricted-agent") is not None
            assert channel_agent_mismatch(OPEN, "") is None


def _make_slack_app(state):
    from kiro_crew.dashboard.chat_slack import api_chat_slot_slack_link

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/slack-link", api_chat_slot_slack_link)
    return app


def _linkable_state(tmp_path, monkeypatch, agent: str = ""):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    slot.agent = agent
    state.slack_client = MagicMock()
    state.slack_client.open_dm = AsyncMock(return_value="C123")
    state.slack_client.post_message = AsyncMock(return_value="newts")
    state.owner_id = "U123"
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.sessions.set_slack_link = MagicMock()
    state.push_slots_update = MagicMock()
    return state


class TestSlackLinkRefusesMismatchedAgent:
    @pytest.mark.asyncio
    async def test_default_agent_slot_cannot_bind_pinned_channel_thread(
        self, tmp_path, monkeypatch
    ):
        state = _linkable_state(tmp_path, monkeypatch)
        a, b = _patches()
        with a, b:
            async with TestClient(TestServer(_make_slack_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/slack-link",
                    json={"channel": PINNED, "thread_ts": "1700.42"},
                )
                assert resp.status == 409
                data = await resp.json()
                assert data["code"] == "channel_agent_mismatch"
        state.sessions.set_slack_link.assert_not_called()
        assert state.slack_client.post_message.await_count == 0

    @pytest.mark.asyncio
    async def test_matching_agent_slot_links(self, tmp_path, monkeypatch):
        state = _linkable_state(tmp_path, monkeypatch, agent="restricted-agent")
        a, b = _patches()
        with a, b:
            async with TestClient(TestServer(_make_slack_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/slack-link",
                    json={"channel": PINNED, "thread_ts": "1700.42"},
                )
                assert resp.status == 200
        state.sessions.set_slack_link.assert_called_once()

    @pytest.mark.asyncio
    async def test_unpinned_channel_thread_still_links(self, tmp_path, monkeypatch):
        state = _linkable_state(tmp_path, monkeypatch)
        a, b = _patches()
        with a, b:
            async with TestClient(TestServer(_make_slack_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/slack-link",
                    json={"channel": OPEN, "thread_ts": "1700.42"},
                )
                assert resp.status == 200
        state.sessions.set_slack_link.assert_called_once()

    @pytest.mark.asyncio
    async def test_new_anchor_in_pinned_channel_is_refused_and_names_the_agent(
        self, tmp_path, monkeypatch
    ):
        state = _linkable_state(tmp_path, monkeypatch)
        audit = MagicMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_slack.sel", lambda: audit)
        a, b = _patches()
        with a, b:
            async with TestClient(TestServer(_make_slack_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/slack-link", json={"channel": PINNED})
                assert resp.status == 409
                data = await resp.json()
                assert data["code"] == "channel_agent_mismatch"
                assert data["agent"] == "restricted-agent"
                assert "restricted-agent" in data["error"]
        state.sessions.set_slack_link.assert_not_called()
        assert any(
            c.kwargs.get("source") == "channel_agent_pinned"
            for c in audit.log_api_access.call_args_list
        )
        assert state.slack_client.post_message.await_count == 0


def _linked_ds(agent: str):
    slot = MagicMock()
    type(slot).running = PropertyMock(return_value=False)
    slot.key = "slot1"
    slot.agent = agent
    slot.agent_kind = ""
    slot.window_pending = False
    slot._queue = []
    ds = MagicMock()
    ds.get_linked_slot = MagicMock(return_value=slot)
    ds._background_tasks = set()
    ds.push_slots_update = MagicMock()
    return ds, slot


class TestLinkedThreadRouting:
    @pytest.mark.asyncio
    async def test_mismatched_linked_slot_does_not_capture_thread(self):
        from kiro_crew.slack import handler

        slack = MagicMock()
        slack.post_message = AsyncMock()
        ds, slot = _linked_ds(agent="")
        a, b = _patches()
        with (
            a,
            b,
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as run_chat,
        ):
            result = await handler.maybe_route_linked_thread(
                "do the thing", "slack:t1", "U1", PINNED, slack, "t1"
            )
        assert result is True
        run_chat.assert_not_called()
        slot.queue_append.assert_not_called()
        slack.post_message.assert_awaited_once()
        assert "different agent" in slack.post_message.await_args.args[1]

    @pytest.mark.asyncio
    async def test_matching_linked_slot_still_routes(self):
        from kiro_crew.slack import handler

        slack = MagicMock()
        slack.post_message = AsyncMock()
        ds, _slot = _linked_ds(agent="restricted-agent")
        a, b = _patches()
        with (
            a,
            b,
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock),
        ):
            result = await handler.maybe_route_linked_thread(
                "do the thing", "slack:t1", "U1", PINNED, slack, "t1"
            )
        assert result is True


def _import_ds(existing=None):
    ds = MagicMock()
    ds.get_linked_slot = MagicMock(return_value=existing)
    ds.get_or_create_slot = MagicMock()
    ds.link_slack = MagicMock()
    return ds


class TestThreadImportRefusesMismatchedAgent:
    @pytest.mark.asyncio
    async def test_import_into_default_slot_is_refused_in_pinned_channel(self):
        from kiro_crew.slack.interactions_options import (
            ChannelAgentMismatchError,
            _import_thread_to_slot,
        )

        slack = MagicMock()
        slack.fetch_thread_replies = AsyncMock(return_value=[{"text": "hi", "user": "U1"}])
        ds = _import_ds()
        a, b = _patches()
        with a, b, pytest.raises(ChannelAgentMismatchError):
            await _import_thread_to_slot(slack, ds, PINNED, "t1")
        ds.get_or_create_slot.assert_not_called()
        ds.link_slack.assert_not_called()
        slack.fetch_thread_replies.assert_not_called()

    @pytest.mark.asyncio
    async def test_existing_link_on_channel_agent_is_returned(self):
        from kiro_crew.slack.interactions_options import _import_thread_to_slot

        existing = SimpleNamespace(agent="restricted-agent", key="slot1")
        ds = _import_ds(existing)
        a, b = _patches()
        with a, b:
            assert await _import_thread_to_slot(MagicMock(), ds, PINNED, "t1") is existing
        ds.get_or_create_slot.assert_not_called()

    @pytest.mark.asyncio
    async def test_import_in_unpinned_channel_still_links(self):
        from kiro_crew.slack.interactions_options import _import_thread_to_slot

        slack = MagicMock()
        slack.fetch_thread_replies = AsyncMock(return_value=[{"text": "hi", "user": "U1"}])
        ds = _import_ds()
        a, b = _patches()
        with (
            a,
            b,
            patch(
                "kiro_crew.dashboard.chat_persistence.save_slot_off_loop", new_callable=AsyncMock
            ),
        ):
            slot = await _import_thread_to_slot(slack, ds, OPEN, "t1")
        assert slot is ds.get_or_create_slot.return_value
        ds.link_slack.assert_called_once_with(slot.key, "t1", OPEN)

    @pytest.mark.asyncio
    async def test_bang_link_to_dashboard_tells_the_sender(self):
        # Import the interactions facade before patching handler names: its
        # import-time family check would read a patched name as a mismatch.
        import kiro_crew.slack.interactions  # noqa: F401
        from kiro_crew.slack import handler
        from kiro_crew.slack.handler_runtime import commands
        from kiro_crew.slack.interactions_options import IMPORT_AGENT_MISMATCH_NOTICE

        slack = MagicMock()
        slack.post_message = AsyncMock()
        slack.fetch_thread_replies = AsyncMock(return_value=[{"text": "hi", "user": "U1"}])
        ds = _import_ds()
        a, b = _patches()
        with (
            a,
            b,
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
        ):
            await commands._bang_link_to_dashboard(
                "!link-to-dashboard", slack, MagicMock(), PINNED, "t1", "m2", "slack:t1", "U1", None
            )
        slack.post_message.assert_awaited_once_with(PINNED, IMPORT_AGENT_MISMATCH_NOTICE, "t1")
        ds.link_slack.assert_not_called()


async def _route(text, agent="", route_pinned=False, switch_target=None):
    from kiro_crew.slack import handler

    slack = MagicMock()
    slack.post_message = AsyncMock()
    ds, slot = _linked_ds(agent=agent)
    a, b = _patches()
    with (
        a,
        b,
        patch.object(handler, "_dashboard_state", ds),
        patch.object(handler, "is_allowed_user", return_value=True),
        patch.object(handler, "agent_switch_target", return_value=switch_target),
        patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as run_chat,
    ):
        result = await handler.maybe_route_linked_thread(
            text,
            "slack:t1",
            "U1",
            PINNED,
            slack,
            "t1",
            target_slot=slot if route_pinned else None,
            route_pinned=route_pinned,
        )
    return result, slack, run_chat


class TestMismatchedThreadControls:
    @pytest.mark.asyncio
    async def test_bang_command_still_falls_through(self):
        result, slack, run_chat = await _route("!agent restricted-agent")
        assert result is False
        slack.post_message.assert_not_awaited()
        run_chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_switch_to_the_channel_agent_reaches_the_slot(self):
        result, slack, _run = await _route(
            "/agent restricted-agent", switch_target="restricted-agent"
        )
        assert result is True
        for call in slack.post_message.await_args_list:
            assert "different agent" not in call.args[1]

    @pytest.mark.asyncio
    async def test_switch_to_another_agent_is_refused(self):
        result, slack, run_chat = await _route("/agent other", switch_target="other")
        assert result is True
        run_chat.assert_not_called()
        assert "the restricted-agent agent" in slack.post_message.await_args.args[1]

    @pytest.mark.asyncio
    async def test_pinned_route_to_a_mismatched_slot_is_refused(self):
        result, slack, run_chat = await _route("Yes", route_pinned=True)
        assert result is True
        run_chat.assert_not_called()
        assert "different agent" in slack.post_message.await_args.args[1]


class TestResumeInPinnedChannelThread:
    @staticmethod
    def _orch(monkeypatch, tmp_path, slot_agent: str):
        from kiro_crew.slack import interactions as ix

        o = MagicMock()
        o.slack.post_message = AsyncMock(return_value="ts1")
        o.slack.post_ephemeral = AsyncMock()
        o.sessions.get_slack_link = MagicMock(return_value=("", ""))
        o.dashboard_state = MagicMock()
        o.dashboard_state.slot_exists = MagicMock(return_value=True)
        o.dashboard_state.get_slot = MagicMock(
            return_value=SimpleNamespace(agent=slot_agent, agent_kind="")
        )
        monkeypatch.setattr(ix, "_orch", o)
        monkeypatch.setattr(ix, "is_owner", lambda uid: True)
        monkeypatch.setattr(ix, "channel_inbound_permitted", AsyncMock(return_value=True))
        monkeypatch.setattr(ix, "channel_refusal", lambda *a, **k: None)
        monkeypatch.setattr("kiro_crew.config.loader.data_home", lambda: tmp_path)
        monkeypatch.setattr(ix, "_resume_locks", {})
        return o

    @staticmethod
    async def _resume():
        import json

        from kiro_crew.slack import interactions as ix

        payload = {"user": {"id": "U1"}, "channel": {"id": "C1"}, "message": {"ts": "m1"}}
        action = {
            "action_id": "mc_resume_thread_x",
            "value": json.dumps({"key": "dashboard:s1", "title": "T", "src_channel": PINNED}),
        }
        await ix._handle_resume_choice(payload, action, "C1", "m1", "U1", mode="thread")

    @pytest.mark.asyncio
    async def test_mismatched_session_is_not_resumed_in_a_thread(self, monkeypatch, tmp_path):
        o = self._orch(monkeypatch, tmp_path, slot_agent="")
        a, b = _patches()
        with a, b:
            await self._resume()
        o.slack.post_message.assert_not_awaited()
        o.sessions.set_slack_link.assert_not_called()
        o.dashboard_state.link_slack.assert_not_called()
        o.slack.post_ephemeral.assert_awaited_once()
        assert "DM" in o.slack.post_ephemeral.await_args.args[2]

    @pytest.mark.asyncio
    async def test_closed_tab_is_not_resumed_in_a_pinned_channel_thread(
        self, monkeypatch, tmp_path
    ):
        # No live slot: the session's agent cannot be read here, so a pinned
        # channel refuses rather than letting its replies run on that agent.
        o = self._orch(monkeypatch, tmp_path, slot_agent="restricted-agent")
        o.dashboard_state.slot_exists = MagicMock(return_value=False)
        o.dashboard_state.get_slot = MagicMock(return_value=None)
        a, b = _patches()
        with a, b:
            await self._resume()
        o.slack.post_message.assert_not_awaited()
        o.sessions.set_slack_link.assert_not_called()
        o.slack.post_ephemeral.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_agent_change_while_the_thread_posts_blocks_the_link(self, monkeypatch, tmp_path):
        o = self._orch(monkeypatch, tmp_path, slot_agent="restricted-agent")
        live = SimpleNamespace(agent="restricted-agent", agent_kind="")
        o.dashboard_state.get_slot = MagicMock(return_value=live)

        async def _post_and_switch(*_a, **_k):
            live.agent = "other"
            return "ts1"

        o.slack.post_message = AsyncMock(side_effect=_post_and_switch)
        a, b = _patches()
        with a, b:
            await self._resume()
        o.sessions.set_slack_link.assert_not_called()
        o.dashboard_state.link_slack.assert_not_called()

    @pytest.mark.asyncio
    async def test_matching_session_still_resumes_in_a_thread(self, monkeypatch, tmp_path):
        o = self._orch(monkeypatch, tmp_path, slot_agent="restricted-agent")
        a, b = _patches()
        with a, b, patch("aiohttp.ClientSession"):
            await self._resume()
        o.sessions.set_slack_link.assert_called_once_with("dashboard:s1", "ts1", PINNED)


class TestLinkedSessionKeepsTheChannelAgent:
    @staticmethod
    def _linked_slot(tmp_path, monkeypatch, agent="restricted-agent"):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.agent = agent
        slot._slack_linked = True
        slot._slack_channel = PINNED
        slot._slack_thread_ts = "1700.42"
        return state, slot

    @pytest.mark.asyncio
    async def test_switch_away_from_the_channel_agent_is_refused(self, tmp_path, monkeypatch):
        from chat_test_helpers import _make_app_with_agent_routes

        state, slot = self._linked_slot(tmp_path, monkeypatch)
        a, b = _patches()
        with a, b:
            async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/agent", json={"agent": "other", "agent_kind": "template"}
                )
                assert resp.status == 409, await resp.text()
                data = await resp.json()
                assert data["code"] == "channel_agent_pinned"
                assert "restricted-agent" in data["error"]
        assert slot.agent == "restricted-agent"

    @pytest.mark.asyncio
    async def test_switch_in_an_unpinned_channel_is_not_refused_here(self, tmp_path, monkeypatch):
        from chat_test_helpers import _make_app_with_agent_routes

        state, slot = self._linked_slot(tmp_path, monkeypatch)
        slot._slack_channel = OPEN
        a, b = _patches()
        with a, b:
            async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/agent", json={"agent": "other", "agent_kind": "template"}
                )
                if resp.status == 409:
                    assert (await resp.json()).get("code") != "channel_agent_pinned"


def _provider_turn(slot):
    return SimpleNamespace(
        _turn_project=None,
        client=None,
        needs_session_reset=False,
        private_member=None,
        selected_binding=None,
        session_key="k",
        slot=slot,
        state=SimpleNamespace(),
        _produced_visible_output=False,
    )


def _pinned_linked_namespace(agent="restricted-agent"):
    notices: list = []
    slot = SimpleNamespace(
        key="s1",
        agent=agent,
        agent_kind="",
        mode="chat",
        _slack_linked=True,
        _slack_channel=PINNED,
        crew_mode_agent="",
    )
    slot.append = lambda role, text, cls, **_kw: notices.append((role, text, _kw.get("meta")))
    return slot, notices


class TestProviderSwitchWhileLinked:
    @pytest.mark.asyncio
    async def test_provider_switch_off_the_channel_agent_is_vetoed(self, monkeypatch):
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat_turn.turn_locals import _Flow

        audit = MagicMock()
        monkeypatch.setattr(chat_runner, "sel", lambda: audit)
        slot, notices = _pinned_linked_namespace()
        turn = _provider_turn(slot)
        a, b = _patches()
        with a, b:
            flow = await chat_runner._on_agent_switched(turn, SimpleNamespace(text="other"))
        assert flow is _Flow.BREAK
        assert slot.agent == "restricted-agent"
        assert turn.needs_session_reset and turn._produced_visible_output
        assert notices and notices[0][0] == "notice" and "restricted-agent" in notices[0][1]
        assert notices[0][2]["notice"] == "agent_switch_blocked_channel"
        assert audit.log_api_access.call_args.kwargs["source"] == "channel_agent_pinned"

    def test_provider_switch_onto_the_channel_agent_is_not_vetoed(self):
        from kiro_crew.dashboard.chat_turn.session_events import _channel_pin_blocks

        slot, _ = _pinned_linked_namespace(agent="")
        a, b = _patches()
        with a, b:
            assert _channel_pin_blocks(slot, "restricted-agent") is None
            assert _channel_pin_blocks(slot, "other") == "restricted-agent"
            slot._slack_channel = OPEN
            assert _channel_pin_blocks(slot, "other") is None


class TestQueuedReplyKeptWhenTheDefaultChanges:
    """Turn admission alone judges a queued channel reply, by the execution record
    it runs on. The queue drain's containment does not judge the agent, so a
    change of the global default cannot drop a reply whose session still runs
    the channel's agent."""

    def test_containment_snapshot_does_not_judge_the_agent(self, tmp_path):
        from kiro_crew.dashboard.session_control import (
            containment_meta,
            containment_snapshot,
            newly_held_constraints,
        )

        state = MagicMock()
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        slot, _ = _pinned_linked_namespace(agent="")
        slot.linked_session_key = ""
        slot.memory_mode = "persistent"
        slot._app = ""
        slot.workspace = "default"
        a, b = _patches(default_template="restricted-agent")
        with a, b:
            admitted = containment_meta(state, slot)
        # The default moves off the channel's agent while the reply waits.
        a, b = _patches(default_template="kirocrew")
        with a, b:
            now = containment_snapshot(state, slot, on_probe_failure=True)
            assert "channel_agent_mismatch" not in now
            assert newly_held_constraints(now, admitted, directive_user_origin=True) == []

    @pytest.mark.asyncio
    async def test_admission_runs_a_reply_whose_record_is_the_channels(self, monkeypatch):
        from kiro_crew.dashboard import chat_runner  # noqa: F401
        from kiro_crew.dashboard.chat_turn.turn_admission import _source_channel_mismatch

        monkeypatch.setattr(
            "kiro_crew.execution_context.read_session_execution",
            lambda *_a, **_k: _recorded(name="restricted-agent", template_id="restricted-agent"),
        )
        slot = SimpleNamespace(agent="", agent_kind="")
        a, b = _patches(default_template="kirocrew")
        with a, b:
            assert await _source_channel_mismatch((PINNED,), slot, "dashboard:s1") is None


class TestCheckThenCommitWindow:
    @pytest.mark.asyncio
    async def test_link_made_while_the_switch_resolved_still_blocks_the_commit(
        self, tmp_path, monkeypatch
    ):
        # The slot is unlinked when the switch starts, so the early check
        # passes; a link to a pinned channel lands while the switch awaits its
        # agent resolution. The commit-window check must refuse it.
        from chat_test_helpers import _make_app_with_agent_routes

        from kiro_crew.dashboard import chat_handlers

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.agent = "restricted-agent"
        real_refusal = chat_handlers._channel_pin_refusal
        calls = {"n": 0}

        def _link_between(slot_, name, kind):
            calls["n"] += 1
            if calls["n"] == 1:
                result = real_refusal(slot_, name, kind)
                slot_._slack_linked = True
                slot_._slack_channel = PINNED
                return result
            return real_refusal(slot_, name, kind)

        monkeypatch.setattr(chat_handlers, "_channel_pin_refusal", _link_between)
        a, b = _patches()
        with a, b:
            async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/agent", json={"agent": "kirocrew", "agent_kind": "template"}
                )
                assert resp.status == 409, await resp.text()
                assert (await resp.json())["code"] == "channel_agent_pinned"
        assert calls["n"] == 2
        assert slot.agent == "restricted-agent"

    @pytest.mark.asyncio
    async def test_agent_change_during_link_posting_blocks_the_link(self, tmp_path, monkeypatch):
        state = _linkable_state(tmp_path, monkeypatch, agent="restricted-agent")
        slot = state.get_slot("s1")

        async def _post_and_switch(*_a, **_k):
            slot.agent = "other"
            return "newts"

        state.slack_client.post_message = AsyncMock(side_effect=_post_and_switch)
        a, b = _patches()
        with a, b:
            async with TestClient(TestServer(_make_slack_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/slack-link", json={"channel": PINNED})
                assert resp.status == 409
                assert (await resp.json())["code"] == "channel_agent_mismatch"
        state.sessions.set_slack_link.assert_not_called()


class TestLateLinkDuringProviderSwitch:
    @pytest.mark.asyncio
    async def test_link_landing_while_the_switch_is_written_vetoes_the_commit(self, monkeypatch):
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat_turn import session_events
        from kiro_crew.dashboard.chat_turn.turn_locals import _Flow

        audit = MagicMock()
        monkeypatch.setattr(chat_runner, "sel", lambda: audit)
        monkeypatch.setattr(chat_runner, "refuse_stale_switch", AsyncMock(), raising=False)
        monkeypatch.setattr(chat_runner, "_require_current_binding", lambda *a, **k: None)
        restore = MagicMock()
        monkeypatch.setattr(chat_runner, "restore_agent_selection", restore, raising=False)
        slot, notices = _pinned_linked_namespace()
        slot._slack_linked = False  # unlinked when the switch event arrives

        def _record(*_a, **_k):
            # The link lands while the switch is being written.
            slot._slack_linked = True
            return "change-token"

        monkeypatch.setattr(chat_runner, "record_provider_agent_switch", _record, raising=False)
        monkeypatch.setattr(
            chat_runner.KiroCrewConfig, "load", classmethod(lambda cls: object()), raising=False
        )
        turn = _provider_turn(slot)
        a, b = _patches()
        with a, b:
            flow = await session_events._on_agent_switched(turn, SimpleNamespace(text="other"))
        assert flow is _Flow.BREAK
        assert slot.agent == "restricted-agent"
        restore.assert_called_once_with("k", "change-token")
        assert notices and notices[-1][2]["notice"] == "agent_switch_blocked_channel"


class TestFirstSendAdoptionWhileLinked:
    @pytest.mark.asyncio
    async def test_imported_slot_is_bound_to_the_channel_template(self):
        from kiro_crew.slack.interactions_options import _import_thread_to_slot

        slack = MagicMock()
        slack.fetch_thread_replies = AsyncMock(return_value=[{"text": "hi", "user": "U1"}])
        ds = _import_ds()
        new_slot = SimpleNamespace(key="s9", agent="", agent_kind="", title="")
        new_slot.append = lambda *a, **k: None
        ds.get_or_create_slot = MagicMock(return_value=new_slot)
        a, b = _patches(cfg=_cfg("kirocrew"), default_template="kirocrew")
        with (
            a,
            b,
            patch(
                "kiro_crew.dashboard.chat_persistence.save_slot_off_loop", new_callable=AsyncMock
            ),
        ):
            slot = await _import_thread_to_slot(slack, ds, PINNED, "t1")
        assert slot is new_slot
        assert (slot.agent, slot.agent_kind) == ("kirocrew", "template")

    def test_channel_pin_refusal_blocks_adopting_another_agent(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import chat_handlers

        slot = SimpleNamespace(key="s1", agent="", _slack_linked=True, _slack_channel=PINNED)
        a, b = _patches()
        with a, b:
            refused = chat_handlers._channel_pin_refusal(slot, "other", "")
            assert refused is not None and refused.status == 409
            assert chat_handlers._channel_pin_refusal(slot, "restricted-agent", "") is None


class TestChannelMessageAdmittedUnderTheDispatchLock:
    @staticmethod
    def _turn(slot, channel_origin: bool):
        state = SimpleNamespace(_slots={slot.key: slot}, memory_startup_task=None)
        return SimpleNamespace(
            _attachments=None,
            _crew_log_sid="",
            _crew_log_turn_no=0,
            _deferred_options_expiry=False,
            _is_refusal_retry_turn=False,
            _local_turn_marker_generation=0,
            _prompt_depth=1,
            _synthetic_recovery_turn=False,
            _turn_priority=None,
            client=None,
            first_word="",
            is_slash=False,
            message="hi",
            session_key="dashboard:s1",
            slot=slot,
            state=state,
            _directive_channel_origin=channel_origin,
        )

    @pytest.mark.asyncio
    async def test_channel_message_on_a_moved_slot_is_not_run(self, monkeypatch):
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat_turn.turn_admission import _turn_admit
        from kiro_crew.dashboard.chat_turn.turn_locals import _Flow

        class _Lock:
            async def acquire(self):
                return True

        audit = MagicMock()
        monkeypatch.setattr(chat_runner, "sel", lambda: audit)
        monkeypatch.setattr(chat_runner, "slot_switch_session_lock", lambda _k: _Lock())
        monkeypatch.setattr(chat_runner, "_begin_local_turn_marker", AsyncMock())
        monkeypatch.setattr(chat_runner, "_local_turn_generation_for", lambda _s: 1)
        monkeypatch.setattr(chat_runner, "_current_binding", lambda _t: ("other", "", ""))
        monkeypatch.setattr(
            "kiro_crew.memory_startup.wait_for_memory_preparation", AsyncMock(return_value=None)
        )
        slot, notices = _pinned_linked_namespace(agent="other")
        slot._active_turn_session_key = ""
        turn = self._turn(slot, channel_origin=True)
        a, b = _patches()
        with a, b:
            flow = await _turn_admit(turn)
        assert flow is _Flow.END
        assert notices and "not run" in notices[-1][1]
        assert turn._dispatch_lock_held is True
        assert audit.log_api_access.call_args.kwargs["source"] == "channel_agent_pinned"

    @pytest.mark.asyncio
    async def test_channel_message_on_a_recorded_other_template_is_not_run(self, monkeypatch):
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat_turn.turn_admission import _turn_admit
        from kiro_crew.dashboard.chat_turn.turn_locals import _Flow

        class _Lock:
            async def acquire(self):
                return True

        audit = MagicMock()
        monkeypatch.setattr(chat_runner, "sel", lambda: audit)
        monkeypatch.setattr(chat_runner, "slot_switch_session_lock", lambda _k: _Lock())
        monkeypatch.setattr(chat_runner, "_begin_local_turn_marker", AsyncMock())
        monkeypatch.setattr(chat_runner, "_local_turn_generation_for", lambda _s: 1)
        monkeypatch.setattr(chat_runner, "_current_binding", lambda _t: ("other", "", ""))
        monkeypatch.setattr(
            "kiro_crew.memory_startup.wait_for_memory_preparation", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            "kiro_crew.execution_context.read_session_execution", lambda *_a, **_k: _recorded()
        )
        slot, notices = _pinned_linked_namespace(agent="restricted-agent")
        slot._active_turn_session_key = ""
        turn = self._turn(slot, channel_origin=True)
        a, b = _patches()
        with a, b:
            flow = await _turn_admit(turn)
        assert flow is _Flow.END
        assert notices and "not run" in notices[-1][1]
        assert turn._dispatch_lock_held is True
        assert audit.log_api_access.call_args.kwargs["source"] == "channel_agent_pinned"


def _recorded(kind="template", name="A", template_id="A"):
    return SimpleNamespace(selection_kind=kind, selection_name=name, template_id=template_id)


class TestRecordedExecutionDecides:
    """A turn runs on its recorded selection, so that is what the channel is checked against."""

    def test_unnamed_slot_on_a_recorded_template_is_not_the_new_default(self):
        from kiro_crew.slack.handler import channel_agent_mismatch

        # The default template and the channel's agent are both B now, but the
        # session recorded A when it first ran.
        a, b = _patches(cfg=_cfg("B"), default_template="B")
        with a, b:
            assert channel_agent_mismatch(PINNED, "", "") is None
            assert channel_agent_mismatch(PINNED, "", "", recorded=_recorded()) == "B"
            assert (
                channel_agent_mismatch(
                    PINNED, "", "", recorded=_recorded(name="B", template_id="B")
                )
                is None
            )
            assert (
                channel_agent_mismatch(PINNED, "", "", recorded=_recorded(name="", template_id="B"))
                is None
            )

    def test_recorded_crewmate_is_compared_in_the_member_namespace(self):
        from kiro_crew.slack.handler import channel_agent_mismatch

        a, b = _patches(cfg=_cfg("crew"), aliases={"crew"})
        with a, b:
            assert (
                channel_agent_mismatch(
                    PINNED, "x", "template", recorded=_recorded("member", "crew")
                )
                is None
            )
            assert (
                channel_agent_mismatch(PINNED, "crew", "", recorded=_recorded("template", "crew"))
                == "crew"
            )
            assert channel_agent_mismatch(PINNED, "", "", recorded=_recorded("member", "")) == (
                "<unknown>"
            )

    @pytest.mark.asyncio
    async def test_unreadable_record_fails_closed(self, monkeypatch):
        from kiro_crew.slack.handler import linked_slot_channel_mismatch

        def _boom(*_a, **_k):
            raise RuntimeError("unreadable")

        monkeypatch.setattr("kiro_crew.execution_context.read_session_execution", _boom)
        slot = SimpleNamespace(agent="restricted-agent", agent_kind="")
        a, b = _patches()
        with a, b:
            assert await linked_slot_channel_mismatch(PINNED, slot, "dashboard:s1") == "<unknown>"
            assert await linked_slot_channel_mismatch(OPEN, slot, "dashboard:s1") is None

    @pytest.mark.asyncio
    async def test_linked_reply_is_stopped_when_the_record_names_another_template(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            "kiro_crew.execution_context.read_session_execution", lambda *_a, **_k: _recorded()
        )
        result, slack, run_chat = await _route("do the thing", agent="restricted-agent")
        assert result is True
        run_chat.assert_not_called()
        assert "different agent" in slack.post_message.await_args.args[1]


class TestRecordedSelectionResolvesLikeTheBinder:
    def test_crewmate_record_uses_the_current_alias_of_its_member_id(self, monkeypatch):
        from kiro_crew.slack.handler_runtime import inbound

        execution = SimpleNamespace(
            selection_kind="member", selection_name="old-name", member_id="m1", template_id=""
        )
        monkeypatch.setattr(
            "kiro_crew.execution_context.read_session_execution", lambda *_a, **_k: execution
        )
        monkeypatch.setattr(
            "kiro_crew.execution_context.member_config_for_id",
            lambda _cfg, member_id: ("crew", object()),
        )
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda cls: object())
        )
        sel_ = inbound._recorded_selection("dashboard:s1")
        assert (sel_.selection_kind, sel_.selection_name) == ("member", "crew")

    def test_template_record_maps_a_skill_view_back_to_its_source(self, monkeypatch):
        from kiro_crew.slack.handler_runtime import inbound

        execution = SimpleNamespace(
            selection_kind="template",
            selection_name="",
            member_id=None,
            template_id="kirocrew-skill-view-abc",
        )
        monkeypatch.setattr(
            "kiro_crew.execution_context.read_session_execution", lambda *_a, **_k: execution
        )
        monkeypatch.setattr(
            "kiro_crew.session_agent_selection._source_of_view",
            lambda name: "restricted-agent" if name.startswith("kirocrew-skill-view-") else name,
        )
        sel_ = inbound._recorded_selection("dashboard:s1")
        assert (sel_.selection_kind, sel_.selection_name) == ("template", "restricted-agent")
        a, b = _patches()
        with a, b:
            from kiro_crew.slack.handler import channel_agent_mismatch

            assert channel_agent_mismatch(PINNED, "", "", recorded=sel_) is None

    def test_template_record_is_judged_by_the_template_it_runs(self, monkeypatch):
        from kiro_crew.slack.handler import channel_agent_mismatch
        from kiro_crew.slack.handler_runtime import inbound

        # The label names the channel's agent; the record runs another template.
        execution = SimpleNamespace(
            selection_kind="template",
            selection_name="restricted-agent",
            member_id=None,
            template_id="other",
        )
        monkeypatch.setattr(
            "kiro_crew.execution_context.read_session_execution", lambda *_a, **_k: execution
        )
        sel_ = inbound._recorded_selection("dashboard:s1")
        a, b = _patches()
        with a, b:
            assert channel_agent_mismatch(PINNED, "", "", recorded=sel_) == "restricted-agent"
            assert (
                channel_agent_mismatch(
                    PINNED, "", "", recorded=_recorded(name="restricted-agent", template_id="other")
                )
                == "restricted-agent"
            )


async def _route_closed_tab(text, owner, recorded, channel=PINNED):
    """A reply in a thread whose dashboard owner has no live slot (tab closed)."""
    from kiro_crew.slack import handler

    slack = MagicMock()
    slack.post_message = AsyncMock()
    ds = MagicMock()
    ds.get_linked_slot = MagicMock(return_value=None)
    ds.sessions.get_session_for_thread = MagicMock(return_value=owner)
    a, b = _patches()
    with (
        a,
        b,
        patch.object(handler, "_dashboard_state", ds),
        patch.object(handler, "is_allowed_user", return_value=True),
        patch("kiro_crew.execution_context.read_session_execution", lambda *_a, **_k: recorded),
    ):
        result = await handler.maybe_route_linked_thread(
            text, "slack:t1", "U1", channel, slack, "t1"
        )
    return result, slack


def _execution(kind="template", name="", template_id="A", member_id=None):
    return SimpleNamespace(
        selection_kind=kind, selection_name=name, template_id=template_id, member_id=member_id
    )


class TestClosedDashboardTabOwner:
    @pytest.mark.asyncio
    async def test_reply_for_a_closed_tab_on_another_agent_is_stopped(self):
        result, slack = await _route_closed_tab("do it", "dashboard:chat-1", _execution())
        assert result is True
        assert "different agent" in slack.post_message.await_args.args[1]

    @pytest.mark.asyncio
    async def test_closed_tab_on_the_channel_agent_takes_the_plain_route(self):
        result, slack = await _route_closed_tab(
            "do it", "dashboard:chat-1", _execution(template_id="restricted-agent")
        )
        assert result is False
        slack.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_closed_tab_with_no_record_is_stopped(self):
        result, _slack = await _route_closed_tab("do it", "dashboard:chat-1", None)
        assert result is True

    @pytest.mark.asyncio
    async def test_slack_born_owner_keeps_its_routing(self):
        result, slack = await _route_closed_tab("do it", "slack:t1", _execution())
        assert result is False
        slack.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_control_commands_and_unpinned_channels_fall_through(self):
        result, _ = await _route_closed_tab("!agent x", "dashboard:chat-1", _execution())
        assert result is False
        result, _ = await _route_closed_tab("do it", "dashboard:chat-1", _execution(), OPEN)
        assert result is False

    @pytest.mark.asyncio
    async def test_pinned_answer_to_a_closed_tab_on_another_agent_is_stopped(self):
        from kiro_crew.slack import handler

        slack = MagicMock()
        slack.post_message = AsyncMock()
        ds = MagicMock()
        ds.get_linked_slot = MagicMock(return_value=None)
        a, b = _patches()
        with (
            a,
            b,
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch(
                "kiro_crew.execution_context.read_session_execution",
                lambda *_a, **_k: _execution(),
            ),
        ):
            refused = await handler.closed_dashboard_owner_refused(
                "Yes", "slack:t1", "U1", PINNED, slack, "t1", owner="dashboard:chat-1"
            )
            allowed = await handler.closed_dashboard_owner_refused(
                "Yes", "slack:t1", "U1", PINNED, slack, "t1", owner="slack:t1"
            )
        assert refused is True
        assert allowed is False

    def test_dispatch_checks_a_pinned_answer_before_the_linked_route(self):
        import inspect

        from kiro_crew.slack.handler_runtime import dispatch

        source = inspect.getsource(dispatch)
        body = source.split("closed_dashboard_owner_refused(", 1)[1]
        assert "owner=turn.asker_key" in body.split("maybe_route_linked_thread(", 1)[0]


class TestTabClosedWhileTheWindowLoads:
    @pytest.mark.asyncio
    async def test_reply_for_a_tab_closed_during_the_window_load_is_stopped(self):
        from kiro_crew.slack import handler

        slack = MagicMock()
        slack.post_message = AsyncMock()
        ds, slot = _linked_ds(agent="restricted-agent")
        slot.window_pending = True
        ds.sessions.get_session_for_thread = MagicMock(return_value="dashboard:chat-1")

        async def _close_tab(_slot):
            ds.get_linked_slot.return_value = None

        a, b = _patches()
        with (
            a,
            b,
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch.object(handler, "load_window_off_loop", _close_tab),
            patch(
                "kiro_crew.execution_context.read_session_execution",
                lambda *_a, **_k: _execution(),
            ),
            patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as run_chat,
        ):
            result = await handler.maybe_route_linked_thread(
                "do the thing", "slack:t1", "U1", PINNED, slack, "t1"
            )
        assert result is True
        run_chat.assert_not_called()
        assert "different agent" in slack.post_message.await_args.args[1]

    @pytest.mark.asyncio
    async def test_unreadable_owner_record_gets_the_could_not_be_read_notice(self):
        def _boom(*_a, **_k):
            raise RuntimeError("unreadable")

        from kiro_crew.slack import handler

        slack = MagicMock()
        slack.post_message = AsyncMock()
        a, b = _patches()
        with (
            a,
            b,
            patch.object(handler, "is_allowed_user", return_value=True),
            patch("kiro_crew.execution_context.read_session_execution", _boom),
        ):
            refused = await handler.closed_dashboard_owner_refused(
                "do it", "slack:t1", "U1", PINNED, slack, "t1", owner="dashboard:chat-1"
            )
        assert refused is True
        assert "could not be read" in slack.post_message.await_args.args[1]


class TestAdmissionJudgesTheSourceChannel:
    """Turn admission is the enforcement point: a channel-origin turn is judged by
    the channel its words were typed in, whatever the slot is linked to by then."""

    @staticmethod
    def _admit_patches(monkeypatch, audit):
        from kiro_crew.dashboard import chat_runner

        class _Lock:
            async def acquire(self):
                return True

        monkeypatch.setattr(chat_runner, "sel", lambda: audit)
        monkeypatch.setattr(chat_runner, "slot_switch_session_lock", lambda _k: _Lock())
        monkeypatch.setattr(chat_runner, "_begin_local_turn_marker", AsyncMock())
        monkeypatch.setattr(chat_runner, "_local_turn_generation_for", lambda _s: 1)
        monkeypatch.setattr(chat_runner, "_current_binding", lambda _t: ("other", "", ""))
        monkeypatch.setattr(
            "kiro_crew.memory_startup.wait_for_memory_preparation", AsyncMock(return_value=None)
        )

    @pytest.mark.asyncio
    async def test_unlink_then_switch_during_the_record_read_is_not_run(self, monkeypatch):
        """The reply passed the Slack-side check on the channel's agent; while that
        check read the record, the slot was unlinked and moved to another agent.
        The turn the route scheduled carries the channel and is refused."""
        # The runner first: importing it composes the turn owners it serves.
        from kiro_crew.dashboard import chat_runner  # noqa: F401
        from kiro_crew.dashboard.chat_turn.turn_admission import _turn_admit
        from kiro_crew.dashboard.chat_turn.turn_locals import _Flow
        from kiro_crew.slack import handler

        slack = MagicMock()
        slack.post_message = AsyncMock()
        ds, slot = _linked_ds(agent="restricted-agent")

        async def _check_then_unlink_and_switch(_channel, _slot, _key):
            # The operator's unlink and switch land during the record read.
            slot._slack_linked = False
            slot._slack_channel = ""
            slot.agent = "other"
            return None

        a, b = _patches()
        with (
            a,
            b,
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch.object(handler, "linked_slot_channel_mismatch", _check_then_unlink_and_switch),
            patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as run_chat,
        ):
            routed = await handler.maybe_route_linked_thread(
                "do the thing", "slack:t1", "U1", PINNED, slack, "t1"
            )
            await asyncio.sleep(0)
        assert routed is True
        kwargs = run_chat.call_args.kwargs
        assert kwargs["_directive_channel_origin"] is True
        assert kwargs["_directive_source_channels"] == (PINNED,)

        audit = MagicMock()
        self._admit_patches(monkeypatch, audit)
        notices: list = []
        slot.append = lambda role, text, cls, **_kw: notices.append((role, text, _kw.get("meta")))
        slot._active_turn_session_key = ""
        turn = TestChannelMessageAdmittedUnderTheDispatchLock._turn(slot, channel_origin=True)
        turn._directive_source_channels = kwargs["_directive_source_channels"]
        with a, b:
            flow = await _turn_admit(turn)
        assert flow is _Flow.END
        assert notices and "was not run" in notices[-1][1]
        assert notices[-1][2] == {"notice": "channel_message_not_run"}
        assert audit.log_api_access.call_args.kwargs["source"] == "channel_agent_pinned"

    def test_a_queued_reply_carries_its_channel_to_the_drain(self):
        from kiro_crew.dashboard.slot_queue_repository import (
            source_channels_of,
            stamp_source_channels,
        )

        owner = SimpleNamespace(_queue=[{"id": "q1"}, {"id": "q2"}])
        stamp_source_channels(owner, "q2", (PINNED, ""))
        assert source_channels_of(owner._queue[1]) == (PINNED,)
        assert source_channels_of(owner._queue[0]) == ()
        # A hand-written value of another type is not read as a channel.
        assert source_channels_of({"_directive_source_channels": [PINNED]}) == ()

    @pytest.mark.asyncio
    async def test_queue_drain_hands_the_entry_channels_to_the_turn(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import chat_runner as cr
        from kiro_crew.dashboard.slot_queue_repository import stamp_source_channels

        state = _make_state(tmp_path)
        state.subagents = None
        slot = state.get_or_create_slot("chat-1")
        qid = slot.queue_append(
            "from the channel", directive_user_origin=True, directive_channel_origin=True
        )
        stamp_source_channels(slot, qid, (PINNED,))
        captured: dict = {}

        def _stub_run_chat(_state, _slot, _prompt, **kwargs):
            captured.update(kwargs)

            async def _done():
                return None

            return _done()

        def _fake_spawn(_state, _slot, coro):
            coro.close()
            task = MagicMock()
            task.done.return_value = True
            return task

        monkeypatch.setattr(cr, "_run_chat", _stub_run_chat)
        monkeypatch.setattr(cr, "spawn_guarded_turn", _fake_spawn)
        assert await cr._start_next_queued_turn(state, slot) is True
        assert captured["_directive_source_channels"] == (PINNED,)

    def test_stamped_channel_wins_over_the_current_link(self):
        from kiro_crew.dashboard import chat_runner  # noqa: F401
        from kiro_crew.dashboard.chat_turn.turn_admission import _channels_judging_turn

        relinked = SimpleNamespace(_slack_linked=True, _slack_channel=OPEN)
        unlinked = SimpleNamespace(_slack_linked=False, _slack_channel="")
        stamped = SimpleNamespace(
            _directive_channel_origin=True, _directive_source_channels=(PINNED,)
        )
        assert _channels_judging_turn(stamped, relinked) == (PINNED,)
        assert _channels_judging_turn(stamped, unlinked) == (PINNED,)
        # A channel-origin turn no producer stamped keeps the linked channel.
        unstamped = SimpleNamespace(_directive_channel_origin=True, _directive_source_channels=())
        assert _channels_judging_turn(unstamped, relinked) == (OPEN,)
        assert _channels_judging_turn(unstamped, unlinked) == ()

    def test_dashboard_and_dm_turns_are_not_judged_by_a_channel(self):
        from kiro_crew.dashboard import chat_runner  # noqa: F401
        from kiro_crew.dashboard.chat_turn.turn_admission import _channels_judging_turn

        linked = SimpleNamespace(_slack_linked=True, _slack_channel=PINNED)
        typed_here = SimpleNamespace(_directive_channel_origin=False, _directive_source_channels=())
        assert _channels_judging_turn(typed_here, linked) == ()

    @pytest.mark.asyncio
    async def test_unpinned_source_channel_is_not_refused(self, monkeypatch):
        from kiro_crew.slack.handler import linked_slot_channel_mismatch

        slot = SimpleNamespace(agent="other", agent_kind="")
        a, b = _patches()
        with a, b:
            assert await linked_slot_channel_mismatch(OPEN, slot, "dashboard:s1") is None


class TestQueuedReplySurvivesARestartAsARestriction:
    """A queued channel reply keeps its source channel across a restart, as a
    restriction only: it can refuse the restored turn and grants nothing."""

    @staticmethod
    def _round_trip(item):
        import json

        from kiro_crew.dashboard.slot_queue_repository import (
            durable_queue_entries,
            sanitize_restored_queue,
        )

        written = json.loads(json.dumps(durable_queue_entries([item])))
        return written, sanitize_restored_queue(written)

    def test_the_source_channel_survives_and_authority_does_not(self):
        from kiro_crew.dashboard.slot_queue_repository import source_channels_of

        item = {
            "id": "q1",
            "content": "from the channel",
            "kind": "",
            "_directive_user_origin": True,
            "_directive_channel_origin": True,
            "_directive_source_channels": (PINNED,),
        }
        written, restored = self._round_trip(item)
        assert written[0]["source_channels"] == [PINNED]
        assert source_channels_of(restored[0]) == (PINNED,)
        assert "_directive_channel_origin" not in restored[0]
        assert "_directive_user_origin" not in restored[0]

    def test_an_unstamped_entry_keeps_its_durable_shape(self):
        written, restored = self._round_trip({"id": "q1", "content": "typed here", "kind": ""})
        assert written == [{"id": "q1", "content": "typed here"}]
        assert "_directive_source_channels" not in restored[0]

    @pytest.mark.parametrize("bad", ["C0PINNED", [], [""], [3], [PINNED] * 9, ["x" * 65], None])
    def test_an_unreadable_saved_channel_fails_closed(self, bad):
        from kiro_crew.dashboard.slot_queue_repository import (
            UNREADABLE_SOURCE_CHANNEL,
            sanitize_restored_queue,
            source_channels_of,
        )

        restored = sanitize_restored_queue([{"id": "q1", "content": "hi", "source_channels": bad}])
        assert source_channels_of(restored[0]) == (UNREADABLE_SOURCE_CHANNEL,)

    def test_a_restored_turn_without_the_origin_flag_is_still_judged(self):
        from kiro_crew.dashboard import chat_runner  # noqa: F401
        from kiro_crew.dashboard.chat_turn.turn_admission import _channels_judging_turn

        unlinked = SimpleNamespace(_slack_linked=False, _slack_channel="")
        restored = SimpleNamespace(
            _directive_channel_origin=False, _directive_source_channels=(PINNED,)
        )
        assert _channels_judging_turn(restored, unlinked) == (PINNED,)

    @pytest.mark.asyncio
    async def test_restart_with_a_queued_reply_on_a_switched_slot_is_not_run(
        self, tmp_path, monkeypatch
    ):
        """Queued while linked, then unlinked, restarted and switched: the
        restored reply, once the user sends its card, drains into a turn that
        admission refuses. A restored entry never drains on its own."""
        from kiro_crew.dashboard import chat_runner as cr
        from kiro_crew.dashboard.chat_turn.turn_admission import _turn_admit
        from kiro_crew.dashboard.chat_turn.turn_locals import _Flow
        from kiro_crew.dashboard.slot_queue_repository import stamp_source_channels

        state = _make_state(tmp_path)
        state.subagents = None
        live = state.get_or_create_slot("chat-1")
        qid = live.queue_append(
            "from the channel", directive_user_origin=True, directive_channel_origin=True
        )
        stamp_source_channels(live, qid, (PINNED,))
        _written, restored = self._round_trip(live._queue[0])

        slot = state.get_or_create_slot("chat-2")
        slot._queue[:] = restored
        captured: dict = {}

        def _stub_run_chat(_state, _slot, _prompt, **kwargs):
            captured.update(kwargs)

            async def _done():
                return None

            return _done()

        def _fake_spawn(_state, _slot, coro):
            coro.close()
            task = MagicMock()
            task.done.return_value = True
            return task

        monkeypatch.setattr(cr, "_run_chat", _stub_run_chat)
        monkeypatch.setattr(cr, "spawn_guarded_turn", _fake_spawn)
        assert await cr._start_next_queued_turn(state, slot) is False
        assert await cr._start_next_queued_turn(state, slot, required_queue_id=qid) is True
        assert captured.get("_directive_channel_origin") is False
        assert captured["_directive_source_channels"] == (PINNED,)

        audit = MagicMock()
        TestAdmissionJudgesTheSourceChannel._admit_patches(monkeypatch, audit)
        moved, notices = _pinned_linked_namespace(agent="other")
        moved._slack_linked = False
        moved._slack_channel = ""
        moved._active_turn_session_key = ""
        turn = TestChannelMessageAdmittedUnderTheDispatchLock._turn(moved, channel_origin=False)
        turn._directive_source_channels = captured["_directive_source_channels"]
        a, b = _patches()
        with a, b:
            flow = await _turn_admit(turn)
        assert flow is _Flow.END
        assert notices[-1][2] == {"notice": "channel_message_not_run"}

    @pytest.mark.asyncio
    async def test_an_unreadable_restored_channel_is_refused_with_the_notice(self, monkeypatch):
        from kiro_crew.dashboard import chat_runner  # noqa: F401
        from kiro_crew.dashboard.chat_turn.turn_admission import _turn_admit
        from kiro_crew.dashboard.chat_turn.turn_locals import _Flow
        from kiro_crew.dashboard.slot_queue_repository import UNREADABLE_SOURCE_CHANNEL

        audit = MagicMock()
        TestAdmissionJudgesTheSourceChannel._admit_patches(monkeypatch, audit)
        slot, notices = _pinned_linked_namespace(agent="restricted-agent")
        slot._slack_linked = False
        slot._active_turn_session_key = ""
        turn = TestChannelMessageAdmittedUnderTheDispatchLock._turn(slot, channel_origin=False)
        turn._directive_source_channels = (UNREADABLE_SOURCE_CHANNEL,)
        a, b = _patches()
        with a, b:
            flow = await _turn_admit(turn)
        assert flow is _Flow.END
        assert notices[-1][2] == {"notice": "channel_message_not_run"}


class TestSourceChannelHoldsPastAdmission:
    """The source-channel restriction also holds where a turn acts before or
    after admission: a local command, and a provider switch mid-turn."""

    @staticmethod
    def _slash_turn(slot, message, channels):
        return SimpleNamespace(
            _commands_off=False,
            _directive_channel_origin=False,
            _directive_self_wake=False,
            _directive_user_origin=False,
            _prompt_depth=0,
            _responsive_human=False,
            _responsive_protected=False,
            _synthetic_payload=False,
            _turn_actor="",
            message=message,
            session_key="dashboard:s1",
            slot=slot,
            state=SimpleNamespace(),
            turn_exit=SimpleNamespace(),
            _directive_source_channels=channels,
        )

    @pytest.mark.asyncio
    async def test_a_restored_local_command_on_a_switched_slot_is_not_run(self, monkeypatch):
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat_turn.slash_commands import _turn_slash_gate
        from kiro_crew.dashboard.chat_turn.turn_locals import _Flow

        audit = MagicMock()
        monkeypatch.setattr(chat_runner, "sel", lambda: audit)
        slot, notices = _pinned_linked_namespace(agent="other")
        slot._slack_linked = False
        slot._slack_channel = ""
        turn = self._slash_turn(slot, "/workflow nightly go", (PINNED,))
        a, b = _patches()
        with a, b:
            flow = await _turn_slash_gate(turn)
        assert flow is _Flow.END
        assert notices[-1][2] == {"notice": "channel_message_not_run"}
        assert audit.log_api_access.call_args.kwargs["operation"] == "chat_runner.slash_gate"

    @pytest.mark.asyncio
    async def test_an_unreadable_source_channel_refuses_a_local_command(self, monkeypatch):
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat_turn.slash_commands import _turn_slash_gate
        from kiro_crew.dashboard.chat_turn.turn_locals import _Flow
        from kiro_crew.dashboard.slot_queue_repository import UNREADABLE_SOURCE_CHANNEL

        monkeypatch.setattr(chat_runner, "sel", lambda: MagicMock())
        slot, notices = _pinned_linked_namespace(agent="restricted-agent")
        turn = self._slash_turn(slot, "/workflow nightly", (UNREADABLE_SOURCE_CHANNEL,))
        a, b = _patches()
        with a, b:
            flow = await _turn_slash_gate(turn)
        assert flow is _Flow.END

    @pytest.mark.asyncio
    async def test_switching_onto_the_channel_agent_is_not_refused_here(self, monkeypatch):
        from kiro_crew.dashboard.chat_turn import slash_commands, turn_admission

        refused = MagicMock()
        monkeypatch.setattr(turn_admission, "_refuse_channel_turn", refused)
        slot, _notices = _pinned_linked_namespace(agent="other")
        slot._slack_linked = False
        turn = self._slash_turn(slot, "/agent restricted-agent", (PINNED,))
        a, b = _patches()
        with a, b:
            try:
                await slash_commands._turn_slash_gate(turn)
            except Exception:
                # The minimal turn stops further in; only the channel check matters.
                pass
        refused.assert_not_called()

    def test_provider_switch_on_an_unlinked_slot_holds_the_source_channel(self):
        from kiro_crew.dashboard.chat_turn.session_events import _channel_pin_blocks
        from kiro_crew.dashboard.slot_queue_repository import UNREADABLE_SOURCE_CHANNEL

        unlinked = SimpleNamespace(_slack_linked=False, _slack_channel="")
        a, b = _patches()
        with a, b:
            assert _channel_pin_blocks(unlinked, "other", (PINNED,)) == "restricted-agent"
            assert _channel_pin_blocks(unlinked, "restricted-agent", (PINNED,)) is None
            assert _channel_pin_blocks(unlinked, "other", (OPEN,)) is None
            assert _channel_pin_blocks(unlinked, "other") is None
            assert (
                _channel_pin_blocks(unlinked, "restricted-agent", (UNREADABLE_SOURCE_CHANNEL,))
                == "its own agent"
            )

    @pytest.mark.asyncio
    async def test_a_provider_switch_on_an_unlinked_slot_is_vetoed(self, monkeypatch):
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat_turn.turn_locals import _Flow

        monkeypatch.setattr(chat_runner, "sel", lambda: MagicMock())
        slot, notices = _pinned_linked_namespace()
        slot._slack_linked = False
        slot._slack_channel = ""
        turn = _provider_turn(slot)
        turn._directive_source_channels = (PINNED,)
        a, b = _patches()
        with a, b:
            flow = await chat_runner._on_agent_switched(turn, SimpleNamespace(text="other"))
        assert flow is _Flow.BREAK
        assert slot.agent == "restricted-agent"
        assert notices[-1][2]["notice"] == "agent_switch_blocked_channel"

    @pytest.mark.asyncio
    async def test_the_commit_time_check_reads_the_turn_channels(self, monkeypatch):
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat_turn import session_events
        from kiro_crew.dashboard.chat_turn.turn_locals import _Flow

        monkeypatch.setattr(chat_runner, "sel", lambda: MagicMock())
        monkeypatch.setattr(chat_runner, "refuse_stale_switch", AsyncMock(), raising=False)
        monkeypatch.setattr(chat_runner, "_require_current_binding", lambda *a, **k: None)
        restore = MagicMock()
        monkeypatch.setattr(chat_runner, "restore_agent_selection", restore, raising=False)
        slot, notices = _pinned_linked_namespace()
        slot._slack_linked = False
        slot._slack_channel = ""
        turn = _provider_turn(slot)
        turn._directive_source_channels = ()

        def _record(*_a, **_k):
            # Only the second check can see this channel.
            turn._directive_source_channels = (PINNED,)
            return "change-token"

        monkeypatch.setattr(chat_runner, "record_provider_agent_switch", _record, raising=False)
        monkeypatch.setattr(
            chat_runner.KiroCrewConfig, "load", classmethod(lambda cls: object()), raising=False
        )
        a, b = _patches()
        with a, b:
            flow = await session_events._on_agent_switched(turn, SimpleNamespace(text="other"))
        assert flow is _Flow.BREAK
        assert slot.agent == "restricted-agent"
        restore.assert_called_once_with("k", "change-token")
        assert notices[-1][2]["notice"] == "agent_switch_blocked_channel"


class TestFileStemAndDeclaredNameAreOneAgent:
    """A template spelled by its spec's file stem is the agent its file declares,
    the way dispatch starts it, on either side of the comparison."""

    @staticmethod
    def _stems(monkeypatch, ready=True):
        from kiro_crew.config import loader_snapshots

        monkeypatch.setattr(loader_snapshots, "_MATERIALIZED_AGENTS", frozenset({"captain"}))
        monkeypatch.setattr(loader_snapshots, "_MATERIALIZED_STEMS", {"KiroPkg-captain": "captain"})
        monkeypatch.setattr(loader_snapshots, "_MATERIALIZED_AGENTS_READY", ready)
        monkeypatch.setattr(loader_snapshots, "_edition_agent_names", lambda: frozenset())

    def test_a_stem_default_matches_a_channel_on_the_declared_name(self, monkeypatch):
        from kiro_crew.slack.handler import channel_agent_mismatch

        self._stems(monkeypatch)
        a, b = _patches(cfg=_cfg("captain"), default_template="KiroPkg-captain")
        with a, b:
            assert channel_agent_mismatch(PINNED, "", "") is None
            assert channel_agent_mismatch(PINNED, "other", "template") == "captain"

    def test_a_named_session_is_compared_by_the_name_it_runs(self, monkeypatch):
        """Only the unnamed default is mapped, as the runtime does: a named
        session runs its name verbatim, so a built-in whose stem a user file
        maps to the channel's agent is still another agent."""
        from kiro_crew.slack.handler import channel_agent_mismatch

        self._stems(monkeypatch)
        from kiro_crew.config import loader_snapshots

        monkeypatch.setattr(
            loader_snapshots,
            "_MATERIALIZED_STEMS",
            {"KiroPkg-captain": "captain", "kiro_planner": "captain"},
        )
        a, b = _patches(cfg=_cfg("captain"))
        with a, b:
            assert channel_agent_mismatch(PINNED, "kiro_planner", "template") == "captain"
            assert channel_agent_mismatch(PINNED, "KiroPkg-captain", "template") == "captain"
            assert (
                channel_agent_mismatch(
                    PINNED, "", "", recorded=_recorded(template_id="kiro_planner")
                )
                == "captain"
            )
            assert channel_agent_mismatch(PINNED, "captain", "template") is None

    def test_an_unready_snapshot_is_not_refreshed_from_disk(self, monkeypatch):
        from kiro_crew.config import loader_snapshots
        from kiro_crew.slack.handler import channel_agent_mismatch

        self._stems(monkeypatch, ready=False)
        refresh = MagicMock()
        monkeypatch.setattr(loader_snapshots, "refresh_materialized_agents", refresh)
        a, b = _patches(cfg=_cfg("captain"), default_template="KiroPkg-captain")
        with a, b:
            # Unmapped, so still a mismatch: fail closed, no disk read.
            assert channel_agent_mismatch(PINNED, "", "") == "captain"
        refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_stem_default_import_links_in_a_channel_on_the_declared_name(self, monkeypatch):
        from kiro_crew.slack.interactions_options import _import_thread_to_slot

        self._stems(monkeypatch)
        slack = MagicMock()
        slack.fetch_thread_replies = AsyncMock(return_value=[{"text": "hi", "user": "U1"}])
        ds = _import_ds()
        a, b = _patches(cfg=_cfg("captain"), default_template="KiroPkg-captain")
        with (
            a,
            b,
            patch(
                "kiro_crew.dashboard.chat_persistence.save_slot_off_loop", new_callable=AsyncMock
            ),
        ):
            slot = await _import_thread_to_slot(slack, ds, PINNED, "t1")
        assert slot is ds.get_or_create_slot.return_value
        ds.link_slack.assert_called_once_with(slot.key, "t1", PINNED)


class TestMergedQueueKeepsSourceChannelsApart:
    """Queue merging never folds entries with different source channels into one
    turn, so a channel refusal at admission takes only that channel's words."""

    @staticmethod
    def _slot(*channels):
        from kiro_crew.dashboard.slot_queue_repository import SOURCE_CHANNELS_KEY

        queue = []
        for i, ch in enumerate(channels):
            item = {"id": f"q{i}", "content": f"m{i}", "kind": ""}
            if ch:
                item[SOURCE_CHANNELS_KEY] = (ch,)
            queue.append(item)

        def _pop(index=0):
            return queue.pop(index)

        return SimpleNamespace(_queue=queue, queue_pop=_pop)

    def test_a_channel_reply_and_a_dashboard_message_are_not_merged(self):
        from kiro_crew.dashboard.chat_utils_injected_rows import _dequeue_next_message

        slot = self._slot(PINNED, "")
        _content, consumed = _dequeue_next_message(slot, True)
        assert [i["id"] for i in consumed] == ["q0"]
        assert [i["id"] for i in slot._queue] == ["q1"]

    def test_replies_from_two_channels_are_not_merged(self):
        from kiro_crew.dashboard.chat_utils_injected_rows import _dequeue_next_message

        slot = self._slot(PINNED, OPEN)
        _content, consumed = _dequeue_next_message(slot, True)
        assert [i["id"] for i in consumed] == ["q0"]

    def test_entries_of_one_channel_still_merge(self):
        from kiro_crew.dashboard.chat_utils_injected_rows import _dequeue_next_message

        slot = self._slot(PINNED, PINNED)
        content, consumed = _dequeue_next_message(slot, True)
        assert len(consumed) == 2 and content.startswith("[2 queued messages merged]")
        plain = self._slot("", "")
        _content, consumed = _dequeue_next_message(plain, True)
        assert len(consumed) == 2


class TestLinkWritersJudgeTheRecordedAgent:
    """An agent-less slot whose execution record binds another template is
    refused a link to a channel on the current default, before any Slack write,
    because its replies would be refused at admission anyway."""

    @pytest.mark.asyncio
    async def test_dashboard_link_reads_the_record(self, tmp_path, monkeypatch):
        state = _linkable_state(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "kiro_crew.execution_context.read_session_execution",
            lambda *_a, **_k: _recorded(name="A", template_id="A"),
        )
        a, b = _patches(cfg=_cfg("kirocrew"))
        with a, b:
            async with TestClient(TestServer(_make_slack_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/slack-link",
                    json={"channel": PINNED, "thread_ts": "1700.42"},
                )
                assert resp.status == 409
                assert (await resp.json())["code"] == "channel_agent_mismatch"
        state.sessions.set_slack_link.assert_not_called()
        assert state.slack_client.post_message.await_count == 0

    @pytest.mark.asyncio
    async def test_dashboard_link_on_the_recorded_channel_agent_still_links(
        self, tmp_path, monkeypatch
    ):
        state = _linkable_state(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "kiro_crew.execution_context.read_session_execution",
            lambda *_a, **_k: _recorded(name="kirocrew", template_id="kirocrew"),
        )
        a, b = _patches(cfg=_cfg("kirocrew"))
        with a, b:
            async with TestClient(TestServer(_make_slack_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/slack-link",
                    json={"channel": PINNED, "thread_ts": "1700.42"},
                )
                assert resp.status == 200

    @pytest.mark.asyncio
    async def test_an_existing_link_is_judged_by_its_record(self, monkeypatch):
        from kiro_crew.slack.interactions_options import (
            ChannelAgentMismatchError,
            _import_thread_to_slot,
        )

        monkeypatch.setattr(
            "kiro_crew.execution_context.read_session_execution",
            lambda *_a, **_k: _recorded(name="A", template_id="A"),
        )
        existing = SimpleNamespace(agent="", agent_kind="", key="slot1", linked_session_key="")
        ds = _import_ds(existing)
        a, b = _patches(cfg=_cfg("kirocrew"))
        with a, b, pytest.raises(ChannelAgentMismatchError):
            await _import_thread_to_slot(MagicMock(), ds, PINNED, "t1")

    @pytest.mark.asyncio
    async def test_thread_resume_reads_the_record(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "kiro_crew.execution_context.read_session_execution",
            lambda *_a, **_k: _recorded(name="A", template_id="A"),
        )
        o = TestResumeInPinnedChannelThread._orch(monkeypatch, tmp_path, slot_agent="")
        a, b = _patches(cfg=_cfg("kirocrew"))
        with a, b:
            await TestResumeInPinnedChannelThread._resume()
        o.slack.post_message.assert_not_awaited()
        o.dashboard_state.link_slack.assert_not_called()
        o.slack.post_ephemeral.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_unreadable_record_fails_closed(self, monkeypatch):
        from kiro_crew.slack.handler import channel_agent_mismatch, read_link_record

        def _boom(*_a, **_k):
            raise RuntimeError("unreadable")

        monkeypatch.setattr("kiro_crew.execution_context.read_session_execution", _boom)
        record = await read_link_record("dashboard:s1")
        a, b = _patches(cfg=_cfg("kirocrew"))
        with a, b:
            assert channel_agent_mismatch(PINNED, "", "", recorded=record) == "<unknown>"


class TestLateLinkCheckSeesASwitchDuringThePost:
    """With an execution record judged before the posts, an agent switch that
    commits while they await still blocks the link: the late check compares the
    slot's selection against the one the record was judged with."""

    @pytest.mark.asyncio
    async def test_dashboard_link_refused_after_a_switch_during_the_anchor_post(
        self, tmp_path, monkeypatch
    ):
        state = _linkable_state(tmp_path, monkeypatch, agent="restricted-agent")
        monkeypatch.setattr(
            "kiro_crew.execution_context.read_session_execution",
            lambda *_a, **_k: _recorded(name="restricted-agent", template_id="restricted-agent"),
        )
        slot = state.get_or_create_slot("s1")

        async def _post_and_switch(*_a, **_k):
            slot.agent = "other"  # committed while the anchor post awaited
            return "newts"

        state.slack_client.post_message = AsyncMock(side_effect=_post_and_switch)
        a, b = _patches()
        with a, b:
            async with TestClient(TestServer(_make_slack_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/slack-link", json={"channel": PINNED})
                assert resp.status == 409
        state.sessions.set_slack_link.assert_not_called()

    @pytest.mark.asyncio
    async def test_thread_resume_refused_after_a_switch_during_the_post(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            "kiro_crew.execution_context.read_session_execution",
            lambda *_a, **_k: _recorded(name="restricted-agent", template_id="restricted-agent"),
        )
        o = TestResumeInPinnedChannelThread._orch(
            monkeypatch, tmp_path, slot_agent="restricted-agent"
        )
        live = o.dashboard_state.get_slot.return_value

        async def _post_and_switch(*_a, **_k):
            live.agent = "other"
            return "ts1"

        o.slack.post_message = AsyncMock(side_effect=_post_and_switch)
        a, b = _patches()
        with a, b:
            await TestResumeInPinnedChannelThread._resume()
        o.dashboard_state.link_slack.assert_not_called()
        o.sessions.set_slack_link.assert_not_called()


class TestLinkRefusedDuringAHalfCommittedSwitch:
    """A switch publishes the slot's new agent before it records it. A link
    writer that sees the old record but the new named agent refuses, instead
    of trusting the half that has not moved."""

    @pytest.mark.asyncio
    async def test_dashboard_link_refused_when_record_and_field_disagree(
        self, tmp_path, monkeypatch
    ):
        state = _linkable_state(tmp_path, monkeypatch, agent="other")
        monkeypatch.setattr(
            "kiro_crew.execution_context.read_session_execution",
            lambda *_a, **_k: _recorded(name="restricted-agent", template_id="restricted-agent"),
        )
        a, b = _patches()
        with a, b:
            async with TestClient(TestServer(_make_slack_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/slack-link",
                    json={"channel": PINNED, "thread_ts": "1700.42"},
                )
                assert resp.status == 409
        state.sessions.set_slack_link.assert_not_called()
        assert state.slack_client.post_message.await_count == 0

    def test_record_and_field_both_on_the_channel_agent_link(self):
        from kiro_crew.slack.handler import link_agent_mismatch

        on = SimpleNamespace(agent="restricted-agent", agent_kind="")
        unnamed = SimpleNamespace(agent="", agent_kind="")
        rec = _recorded(name="restricted-agent", template_id="restricted-agent")
        a, b = _patches()
        with a, b:
            assert link_agent_mismatch(PINNED, on, rec) is None
            assert link_agent_mismatch(PINNED, unnamed, rec) is None
            assert link_agent_mismatch(PINNED, on, None) is None
            assert link_agent_mismatch(OPEN, SimpleNamespace(agent="x", agent_kind=""), rec) is None
