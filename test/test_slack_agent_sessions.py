"""Tests for the agent-session client surface and Slack's native stop button.

These pin the migration from the deprecated ``assistant.threads.*`` methods to
``agents.sessions.*`` (Slack sunsets the former in Feb 2027). They exercise the
real client with a recording ``_web`` stand-in so the exact wire method and
payload are observable — the interface-level mocks elsewhere cannot see which
Slack method was actually called.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.config.loader import KiroCrewConfig, MessagingConfig, SlackConfig
from kiro_crew.slack import client as slack_client
from kiro_crew.slack.client import RealSlackClient


class _RecordingWeb:
    """Records api_call(method, params=/json=) and answers conversations_info."""

    def __init__(self, *, info: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._info = info or {}
        self._responses: dict[str, dict[str, Any]] = {}

    def set_response(self, method: str, resp: dict[str, Any]) -> None:
        self._responses[method] = resp

    async def api_call(
        self,
        method: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((method, params if params is not None else (json or {})))
        return self._responses.get(method, {"ok": True})

    async def conversations_info(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("conversations.info", kwargs))
        return self._info


def _client(web: _RecordingWeb) -> RealSlackClient:
    c = RealSlackClient.__new__(RealSlackClient)
    c._web = web  # type: ignore[attr-defined]
    return c


class TestSessionStatusMigration:
    @pytest.mark.asyncio
    async def test_empty_status_maps_to_active(self) -> None:
        web = _RecordingWeb()
        await _client(web).set_thread_status("C1", "1700.1", "")
        method, params = web.calls[-1]
        assert method == "agents.sessions.setStatus"
        assert params["status"] == slack_client.SESSION_STATUS_ACTIVE
        assert params["channel_id"] == "C1"
        assert params["thread_ts"] == "1700.1"

    @pytest.mark.asyncio
    async def test_nonempty_status_maps_to_processing(self) -> None:
        web = _RecordingWeb()
        await _client(web).set_thread_status("C1", "1700.1", "is using bash")
        method, params = web.calls[-1]
        assert method == "agents.sessions.setStatus"
        assert params["status"] == slack_client.SESSION_STATUS_PROCESSING

    @pytest.mark.asyncio
    async def test_no_longer_calls_deprecated_assistant_threads(self) -> None:
        web = _RecordingWeb()
        await _client(web).set_thread_status("C1", "1700.1", "working")
        await _client(web).set_thread_title("C1", "1700.1", "My session")
        methods = [m for m, _ in web.calls]
        assert not any(m.startswith("assistant.threads") for m in methods)

    @pytest.mark.asyncio
    async def test_invalid_status_coerced_to_processing(self) -> None:
        web = _RecordingWeb()
        await _client(web).set_session_status("C1", "1700.1", "bogus")
        assert web.calls[-1][1]["status"] == slack_client.SESSION_STATUS_PROCESSING

    @pytest.mark.asyncio
    async def test_channel_session_omits_thread_ts(self) -> None:
        # A code channel is one session; Slack rejects thread_ts there.
        web = _RecordingWeb()
        await _client(web).set_session_status("C1", None, slack_client.SESSION_STATUS_PROCESSING)
        assert "thread_ts" not in web.calls[-1][1]

    @pytest.mark.asyncio
    async def test_rename_uses_agents_sessions_rename(self) -> None:
        web = _RecordingWeb()
        await _client(web).set_thread_title("C1", "1700.1", "Trip prep")
        method, params = web.calls[-1]
        assert method == "agents.sessions.rename"
        assert params["title"] == "Trip prep"


class _FakeSessions:
    def __init__(self, *, has: bool, thread_owners: dict[str, str] | None = None) -> None:
        self._has = has
        self._thread_owners = thread_owners or {}
        self.stopped: list[str] = []
        self.cleared: list[str] = []
        self.noted: list[str] = []

    def has_session(self, key: str) -> bool:
        return self._has

    def clear_queue(self, key: str) -> None:
        self.cleared.append(key)

    def get_session_for_thread(self, key: str) -> str | None:
        return self._thread_owners.get(key)

    def note_stop(self, key: str) -> bool:
        self.noted.append(key)
        return True

    async def stop_turn(self, key: str, **kwargs: Any) -> str:
        self.stopped.append(key)
        return "stopped"


class _FakeSlack:
    def __init__(self) -> None:
        self.status_calls: list[tuple[str, str | None, str]] = []
        self.stopped_streams: list[tuple[str, str]] = []

    async def set_session_status(self, channel: str, thread_ts: str | None, status: str) -> None:
        self.status_calls.append((channel, thread_ts, status))

    async def stop_stream(self, channel: str, ts: str, final_text: str | None = None) -> bool:
        self.stopped_streams.append((channel, ts))
        return True


class _FakeOrch:
    def __init__(
        self,
        *,
        has: bool,
        dm_single_session: bool = False,
        thread_owners: dict[str, str] | None = None,
    ) -> None:
        # A single-session DM needs both the slack flag and the transport path.
        self._cfg = KiroCrewConfig(
            slack=SlackConfig(dm_single_session=dm_single_session),
            messaging=MessagingConfig(use_transport=dm_single_session),
        )
        self.sessions = _FakeSessions(has=has, thread_owners=thread_owners)
        self.slack = _FakeSlack()
        self._session_tasks: dict[str, Any] = {}
        self._pending_queue: dict[str, Any] = {}
        self._handler_tasks: set[Any] = set()


class TestAgentSessionStopped:
    """Slack's stop event carries channel, thread_ts, user and streaming_message_ts."""

    @pytest.fixture(autouse=True)
    def _allowed(self, monkeypatch: Any) -> None:
        from kiro_crew.slack import events

        monkeypatch.setattr(events, "is_allowed_user", lambda uid: uid == "U1")

    @pytest.mark.asyncio
    async def test_thread_stop_cancels_and_finishes_streams(self) -> None:
        from kiro_crew.slack import events

        orch = _FakeOrch(has=True)
        await events._handle_agent_session_stopped(
            orch,
            {
                "channel": "C1",
                "thread_ts": "1700.1",
                "user": "U1",
                "streaming_message_ts": ["1700.5"],
            },
        )
        assert orch.sessions.stopped == ["1700.1"]
        assert orch.slack.stopped_streams == [("C1", "1700.5")]
        # Slack updates the session status itself after a stop.
        assert orch.slack.status_calls == []

    @pytest.mark.asyncio
    async def test_session_without_thread_keys_on_its_first_stream(self) -> None:
        from kiro_crew.slack import events

        orch = _FakeOrch(has=True)
        await events._handle_agent_session_stopped(
            orch, {"channel": "C1", "user": "U1", "streaming_message_ts": ["1700.2"]}
        )
        assert orch.sessions.stopped == ["1700.2"]
        assert orch.slack.status_calls == []

    @pytest.mark.asyncio
    async def test_unauthorized_user_cannot_stop(self, monkeypatch: Any) -> None:
        from kiro_crew.slack import events

        logged: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw: Any) -> None:
                logged.append(kw)

        monkeypatch.setattr(events, "sel", lambda: _Sel())
        orch = _FakeOrch(has=True)
        await events._handle_agent_session_stopped(
            orch,
            {
                "channel": "C1",
                "thread_ts": "1700.1",
                "user": "U_STRANGER",
                "streaming_message_ts": ["1700.5"],
            },
        )
        assert orch.sessions.stopped == []
        assert orch.slack.stopped_streams == []
        assert logged and logged[-1]["outcome"] == "denied"
        assert logged[-1]["error"] == "unauthorized sender"

    @pytest.mark.asyncio
    async def test_no_active_session_is_a_noop(self) -> None:
        from kiro_crew.slack import events

        orch = _FakeOrch(has=False)
        await events._handle_agent_session_stopped(
            orch, {"channel": "C1", "thread_ts": "1700.1", "user": "U1"}
        )
        assert orch.sessions.stopped == []
        assert orch.slack.status_calls == []

    @pytest.mark.asyncio
    async def test_single_session_dm_stops_the_flat_dm_key(self) -> None:
        """A single-session DM runs every turn under ``slack:<channel>``, so the
        stop button stops that key, the one ``!stop`` stops, and never the bare
        stream ts its event carries."""
        from kiro_crew.slack import events

        orch = _FakeOrch(has=True, dm_single_session=True)
        await events._handle_agent_session_stopped(
            orch, {"channel": "D1", "user": "U1", "streaming_message_ts": ["1700.2"]}
        )
        assert orch.sessions.stopped == ["slack:D1"]
        assert orch.sessions.cleared == ["slack:D1"]
        assert orch.sessions.noted == ["slack:D1"]
        assert orch.slack.stopped_streams == [("D1", "1700.2")]
        assert orch.slack.status_calls == []

    @pytest.mark.asyncio
    async def test_dashboard_linked_thread_stops_its_owner_session(self) -> None:
        """A DM thread that a dashboard send-to-Slack owns runs its turn under
        that owner, so the stop button stops the owner, as ``!stop`` does."""
        from kiro_crew.slack import events

        orch = _FakeOrch(
            has=True, dm_single_session=True, thread_owners={"1700.1": "dashboard:chat-7"}
        )
        await events._handle_agent_session_stopped(
            orch,
            {
                "channel": "D1",
                "thread_ts": "1700.1",
                "user": "U1",
                "streaming_message_ts": ["1700.5"],
            },
        )
        assert orch.sessions.stopped == ["dashboard:chat-7"]
        assert orch.sessions.cleared == ["dashboard:chat-7"]
        assert orch.sessions.noted == ["dashboard:chat-7"]
        assert orch.slack.stopped_streams == [("D1", "1700.5")]
        assert orch.slack.status_calls == []

    @pytest.mark.asyncio
    async def test_flat_dm_root_stop_without_thread_or_stream(self) -> None:
        """A single-session DM turn posted at channel root has no thread_ts and no
        stream, so the stop event carries neither; it still stops the flat
        ``slack:<channel>`` key rather than doing nothing."""
        from kiro_crew.slack import events

        orch = _FakeOrch(has=True, dm_single_session=True)
        await events._handle_agent_session_stopped(orch, {"channel": "D1", "user": "U1"})
        assert orch.sessions.stopped == ["slack:D1"]
        assert orch.sessions.noted == ["slack:D1"]
        assert orch.slack.stopped_streams == []
        assert orch.slack.status_calls == []

    @pytest.mark.asyncio
    async def test_threadless_stop_outside_a_flat_dm_addresses_nothing(self) -> None:
        from kiro_crew.slack import events

        orch = _FakeOrch(has=True)
        await events._handle_agent_session_stopped(orch, {"channel": "C1", "user": "U1"})
        assert orch.sessions.stopped == []
        assert orch.sessions.noted == []


class TestAgentSessionEventOrigin:
    """Lifecycle events pass the workspace origin gate before they are handled.

    ``team_id`` rides on the enclosing ``event_callback`` envelope, which is the
    Socket Mode request payload the dispatcher receives.
    """

    @pytest.fixture(autouse=True)
    def _allowed_user(self, monkeypatch: Any) -> None:
        from kiro_crew.slack import events

        monkeypatch.setattr(events, "is_allowed_user", lambda uid: uid == "U1")

    @pytest.fixture
    def logged(self, monkeypatch: Any) -> list[dict]:
        from kiro_crew.slack import events

        records: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw: Any) -> None:
                records.append(kw)

        monkeypatch.setattr(events, "sel", lambda: _Sel())
        return records

    @staticmethod
    def _allowlist(monkeypatch: Any, team_ids: set[str] | None) -> None:
        from kiro_crew.slack import enterprise

        monkeypatch.setattr(enterprise, "_allowlist_configured", team_ids is not None)
        monkeypatch.setattr(enterprise, "_allowed_team_ids", set(team_ids or ()))

    @staticmethod
    async def _dispatch(orch: _FakeOrch, team_id: str, event: dict[str, Any]) -> None:
        import asyncio

        from kiro_crew.slack import events

        events._dispatch_agent_session_event(orch, {"team_id": team_id, "event": event}, event)
        await asyncio.gather(*list(orch._handler_tasks), return_exceptions=True)

    _STOP = {
        "type": "agent_session_stopped",
        "channel": "C1",
        "thread_ts": "1700.1",
        "user": "U1",
        "streaming_message_ts": ["1700.5"],
    }

    @pytest.mark.asyncio
    async def test_disallowed_workspace_stop_is_refused(
        self, monkeypatch: Any, logged: list[dict]
    ) -> None:
        self._allowlist(monkeypatch, {"T_OK"})
        orch = _FakeOrch(has=True)
        await self._dispatch(orch, "T_GONE", dict(self._STOP))
        assert orch.sessions.stopped == []
        assert orch.sessions.noted == []
        assert orch.slack.stopped_streams == []
        assert logged and logged[-1]["outcome"] == "denied"
        assert logged[-1]["operation"] == "slack.agent_session_stopped"
        assert logged[-1]["error"] == "enterprise_origin_mismatch"

    @pytest.mark.asyncio
    async def test_missing_team_id_is_refused(self, monkeypatch: Any, logged: list[dict]) -> None:
        # Refused even default-open, as a message with no workspace id is.
        self._allowlist(monkeypatch, None)
        orch = _FakeOrch(has=True)
        await self._dispatch(orch, "", dict(self._STOP))
        assert orch.sessions.stopped == []
        assert logged[-1]["outcome"] == "denied"
        assert logged[-1]["error"] == "missing_team_id"

    @pytest.mark.asyncio
    async def test_envelope_team_id_wins_over_the_event_copy(
        self, monkeypatch: Any, logged: list[dict]
    ) -> None:
        self._allowlist(monkeypatch, {"T_OK"})
        orch = _FakeOrch(has=True)
        await self._dispatch(orch, "T_GONE", dict(self._STOP, team_id="T_OK"))
        assert orch.sessions.stopped == []
        assert logged[-1]["error"] == "enterprise_origin_mismatch"

    @pytest.mark.asyncio
    async def test_allowed_workspace_still_stops(
        self, monkeypatch: Any, logged: list[dict]
    ) -> None:
        self._allowlist(monkeypatch, {"T_OK"})
        orch = _FakeOrch(has=True)
        await self._dispatch(orch, "T_OK", dict(self._STOP))
        assert orch.sessions.stopped == ["1700.1"]
        assert orch.slack.stopped_streams == [("C1", "1700.5")]
        assert logged[-1]["outcome"] == "allowed"

    @pytest.mark.asyncio
    async def test_default_open_without_allowlist_still_stops(
        self, monkeypatch: Any, logged: list[dict]
    ) -> None:
        self._allowlist(monkeypatch, None)
        orch = _FakeOrch(has=True)
        await self._dispatch(orch, "T_ANY", dict(self._STOP))
        assert orch.sessions.stopped == ["1700.1"]

    @pytest.mark.asyncio
    async def test_disallowed_workspace_title_change_is_refused(
        self, monkeypatch: Any, logged: list[dict]
    ) -> None:
        self._allowlist(monkeypatch, {"T_OK"})
        orch = _FakeOrch(has=True)
        event = {
            "type": "agent_session_title_changed",
            "channel": "C1",
            "thread_ts": "1700.1",
            "user": "U1",
            "title": "x",
        }
        await self._dispatch(orch, "T_GONE", event)
        assert [r["outcome"] for r in logged] == ["denied"]
        assert logged[0]["operation"] == "slack.agent_session_title_changed"
