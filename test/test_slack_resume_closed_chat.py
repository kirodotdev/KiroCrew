"""Resuming a closed chat from Slack works, and the reply says its dashboard tab stays closed.

``DashboardState.link_slack`` warns on a missing slot, and
``_handle_resume_choice`` tells the user the tab stays closed instead of a bare "resumed".
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog
from kiro_crew.slack import interactions as ix


def _make_state(tmp_path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.get_slack_link = MagicMock(return_value=(None, None))
    sessions.get_mirror_link = MagicMock(return_value=None)
    sessions.mirror_accepts_inbound = MagicMock(return_value=False)
    return DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )


class TestLinkSlackSignal:
    def test_missing_slot_warns_and_links_nothing(self, tmp_path, caplog) -> None:
        state = _make_state(tmp_path)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.state"):
            state.link_slack("chat-gone", "1.2", "C1")
        assert any("chat-gone" in r.getMessage() for r in caplog.records)
        state.sessions.set_slack_link.assert_not_called()

    def test_present_slot_links_without_warning(self, tmp_path, caplog) -> None:
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.state"):
            state.link_slack("s1", "1.2", "C1")
        assert slot._slack_linked is True
        assert slot._slack_thread_ts == "1.2"
        assert not any("s1" in r.getMessage() for r in caplog.records)


@pytest.fixture
def aiohttp_sess():
    sess = AsyncMock()
    sess.__aenter__ = AsyncMock(return_value=sess)
    sess.__aexit__ = AsyncMock(return_value=None)
    sess.post = AsyncMock(return_value=MagicMock(status=200))
    with patch("aiohttp.ClientSession", return_value=sess):
        yield sess


@pytest.fixture
def orch(monkeypatch: pytest.MonkeyPatch, tmp_path) -> MagicMock:
    o = MagicMock()
    o.slack.post_message = AsyncMock(return_value="ts1")
    o.slack.update_message = AsyncMock()
    o.slack.open_dm = AsyncMock(return_value="D1")
    o.sessions.get_slack_link = MagicMock(return_value=("", ""))
    o.dashboard_state = MagicMock()
    monkeypatch.setattr(ix, "_orch", o)
    monkeypatch.setattr(ix, "is_allowed_user", lambda uid: True)
    monkeypatch.setattr(ix, "is_owner", lambda uid: True)
    monkeypatch.setattr("kiro_crew.slack.handler.is_owner", lambda uid: True)
    monkeypatch.setattr(ix, "channel_inbound_permitted", AsyncMock(return_value=True))
    monkeypatch.setattr("kiro_crew.config.loader.data_home", lambda: tmp_path)
    monkeypatch.setattr(ix, "_resume_locks", {})
    return o


async def _resume(mode: str, key: str = "dashboard:s1") -> None:
    payload = {
        "user": {"id": "U1"},
        "channel": {"id": "C1"},
        "message": {"ts": "m1", "blocks": []},
        "response_url": "https://hooks.slack.com/z",
    }
    action = {
        "action_id": f"mc_resume_{mode}_x",
        "value": json.dumps({"key": key, "title": "My chat", "src_channel": "C5"}),
    }
    await ix._handle_resume_choice(payload, action, "C1", "m1", "U1", mode=mode)


class TestResumeReply:
    @pytest.mark.asyncio
    async def test_closed_chat_resumes_and_says_tab_stays_closed(
        self, orch: MagicMock, aiohttp_sess: AsyncMock
    ) -> None:
        orch.dashboard_state.slot_exists = MagicMock(return_value=False)
        await _resume("thread")
        orch.dashboard_state.slot_exists.assert_called_once_with("s1")
        header = orch.slack.post_message.await_args_list[0].args[1]
        assert ix._RESUME_TAB_CLOSED_MSG in header
        assert "Session resumed" not in header
        text = aiohttp_sess.post.await_args.kwargs["json"]["text"]
        assert ix._RESUME_TAB_CLOSED_MSG in text
        # The resume itself still happens: the thread is linked to the old key.
        orch.sessions.set_slack_link.assert_called_once_with("dashboard:s1", "ts1", "C5")

    @pytest.mark.asyncio
    async def test_closed_chat_in_dm_says_tab_stays_closed(
        self, orch: MagicMock, aiohttp_sess: AsyncMock
    ) -> None:
        orch.dashboard_state.slot_exists = MagicMock(return_value=False)
        await _resume("dm")
        text = aiohttp_sess.post.await_args.kwargs["json"]["text"]
        assert ix._RESUME_TAB_CLOSED_MSG in text
        orch.sessions.set_slack_link.assert_called_once_with("dashboard:s1", "ts1", "D1")

    @pytest.mark.asyncio
    async def test_closed_chat_still_replays_recent_messages(
        self, orch: MagicMock, aiohttp_sess: AsyncMock, tmp_path
    ) -> None:
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / "s1.jsonl").write_text(
            json.dumps({"role": "user", "content": "earlier msg"}), encoding="utf-8"
        )
        orch.dashboard_state.slot_exists = MagicMock(return_value=False)
        await _resume("thread")
        texts = [c.args[1] for c in orch.slack.post_message.await_args_list]
        assert any("earlier msg" in t for t in texts)

    @pytest.mark.asyncio
    async def test_live_chat_still_says_resumed(
        self, orch: MagicMock, aiohttp_sess: AsyncMock
    ) -> None:
        orch.dashboard_state.slot_exists = MagicMock(return_value=True)
        await _resume("thread")
        orch.slack.post_message.assert_any_await(
            "C5", "🧵 *My chat*\nSession resumed. Continue the conversation in this thread."
        )
        text = aiohttp_sess.post.await_args.kwargs["json"]["text"]
        assert text == "▶️ Resumed *My chat* in thread."
        orch.sessions.set_slack_link.assert_called_once_with("dashboard:s1", "ts1", "C5")
        orch.dashboard_state.link_slack.assert_called_once_with("s1", "ts1", "C5")

    @pytest.mark.asyncio
    async def test_session_without_dashboard_slot_says_plain_resumed(
        self, orch: MagicMock, aiohttp_sess: AsyncMock
    ) -> None:
        # A task-runner session never has a dashboard tab, so there is no tab to mention.
        orch.dashboard_state.slot_exists = MagicMock(return_value=False)
        await _resume("thread", key="taskrunner_run_t1")
        text = aiohttp_sess.post.await_args.kwargs["json"]["text"]
        assert text == "▶️ Resumed *My chat* in thread."

    @pytest.mark.asyncio
    async def test_tab_not_yet_restored_is_not_called_closed(
        self, orch: MagicMock, aiohttp_sess: AsyncMock, tmp_path
    ) -> None:
        # Mid-restore, an open tab has no slot yet; absence must not read as closed.
        state = _make_state(tmp_path)
        state.restoring_open_slots = True
        orch.dashboard_state = state
        await _resume("thread")
        text = aiohttp_sess.post.await_args.kwargs["json"]["text"]
        assert text == "▶️ Resumed *My chat* in thread."
