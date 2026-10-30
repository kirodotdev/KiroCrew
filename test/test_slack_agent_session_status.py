"""Agent-session status and title (``agents.sessions.*``) on the real Slack client.

The real :class:`RealSlackClient` with a recording ``_web`` stand-in, so the
wire method and payload are observable: interface-level mocks cannot tell
``agents.sessions.setStatus`` from ``assistant.threads.setStatus``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.slack.agent_sessions import fence, status
from kiro_crew.slack.agent_sessions.status import (
    PROCESSING_REFRESH_SECS,
    SESSION_STATUS_ACTIVE,
    SESSION_STATUS_PROCESSING,
    SESSION_STATUS_SUSPENDED,
    SUSPENDED_STATUS_TEXTS,
    AgentSessionStatus,
    lifecycle_for_text,
    thread_ts_of,
)
from kiro_crew.slack.client import RealSlackClient
from kiro_crew.testing.wait import await_until

_SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"


class SlackApiError(Exception):
    """Shaped like slack_sdk's: ``.response`` carries Slack's ``error``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"ok": False, "error": code}


class RecordingWeb:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.errors: dict[str, str] = {}
        self.responses: dict[str, dict[str, Any]] = {}

    async def api_call(self, method: str, *, params: Any = None, json: Any = None) -> Any:
        self.calls.append((method, dict(params if params is not None else (json or {}))))
        if method in self.errors:
            raise SlackApiError(self.errors[method])
        return self.responses.get(method, {"ok": True})

    async def chat_postMessage(self, **kwargs: Any) -> Any:  # noqa: N802 - slack_sdk name
        self.calls.append(("chat.postMessage", kwargs))
        return {"ts": "9.9"}

    async def chat_update(self, **kwargs: Any) -> Any:  # noqa: N802
        self.calls.append(("chat.update", kwargs))
        return {"ok": True}

    async def chat_postEphemeral(self, **kwargs: Any) -> Any:  # noqa: N802
        self.calls.append(("chat.postEphemeral", kwargs))
        return {"ok": True}

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _caps() -> Any:
    """A clean capability record with the agent-sessions scope granted; yields latch notices."""
    from kiro_crew.slack import features

    seen: list[tuple[str, str]] = []
    features.reset_capabilities()
    features.record_granted_scopes({"assistant:write"})
    features.set_capability_notifier(lambda feature, reason: seen.append((feature, reason)))
    yield seen
    features.set_capability_notifier(None)
    features.reset_capabilities()


def make_client(
    *,
    live: bool = True,
    notes: list | None = None,
    clock: Clock | None = None,
    sleep: Any = None,
    gate: Any = None,
):
    web = RecordingWeb()
    client = RealSlackClient.__new__(RealSlackClient)
    client._web = web  # type: ignore[attr-defined]
    if live:
        sink = notes if notes is not None else []
        kw: dict[str, Any] = {}
        if sleep is not None:
            kw["sleep"] = sleep
        if gate is not None:
            kw["gate"] = gate
        client.agent_sessions = AgentSessionStatus(
            client,
            notify=lambda: (lambda *a, **kw: sink.append((a, kw))),
            clock=clock or Clock(),
            **kw,
        )
    return client, web


def test_status_text_maps_to_the_lifecycle() -> None:
    assert lifecycle_for_text("") == SESSION_STATUS_ACTIVE
    assert lifecycle_for_text("is using bash") == SESSION_STATUS_PROCESSING
    assert lifecycle_for_text("is working on your request") == SESSION_STATUS_PROCESSING
    for text in SUSPENDED_STATUS_TEXTS:
        assert lifecycle_for_text(text) == SESSION_STATUS_SUSPENDED


def test_the_suspended_texts_are_the_ones_the_call_sites_set() -> None:
    """A reworded call site would silently read as ``processing`` again."""
    sites = {
        "Waiting for approval…": _SRC / "slack" / "handler_runtime" / "stream.py",
        "Awaiting review…": _SRC / "slack" / "handler_runtime" / "finalize.py",
    }
    assert set(sites) == set(SUSPENDED_STATUS_TEXTS)
    for text, path in sites.items():
        assert f'"{text}"' in path.read_text(encoding="utf-8"), (text, path)


def test_thread_ts_of_accepts_only_a_slack_thread() -> None:
    assert thread_ts_of("1700.000100") == "1700.000100"
    assert thread_ts_of("slack:1700.000100") == "1700.000100"
    assert thread_ts_of("slack:D0123ABC") is None  # a single-session DM names a channel
    assert thread_ts_of("chat-12-abc") is None  # a dashboard-owned session
    assert thread_ts_of("") is None


class TestToggleOff:
    """No driver attached: exactly the calls main makes."""

    @pytest.mark.asyncio
    async def test_status_and_title_use_assistant_threads(self) -> None:
        client, web = make_client(live=False)
        await client.set_thread_status("C1", "1700.1", "is using bash")
        await client.set_thread_status("C1", "1700.1", "")
        await client.set_thread_title("C1", "slack:1700.1", "Trip")
        assert web.calls == [
            (
                "assistant.threads.setStatus",
                {"channel_id": "C1", "thread_ts": "1700.1", "status": "is using bash"},
            ),
            (
                "assistant.threads.setStatus",
                {"channel_id": "C1", "thread_ts": "1700.1", "status": ""},
            ),
            (
                "assistant.threads.setTitle",
                {"channel_id": "C1", "thread_ts": "slack:1700.1", "title": "Trip"},
            ),
        ]

    @pytest.mark.asyncio
    async def test_session_status_is_a_no_op(self) -> None:
        client, web = make_client(live=False)
        assert await client.set_session_status("C1", "1700.1", SESSION_STATUS_SUSPENDED) is False
        assert await status.set_session_status(client, "C1", None, SESSION_STATUS_ACTIVE) is False
        assert web.calls == []

    def _orch(self, client: Any, on: bool) -> Any:
        class Orch:
            slack = client
            dashboard_state = None
            _cfg = type("Cfg", (), {"slack": type("S", (), {"agent_sessions": on})()})()

        return Orch()

    @pytest.mark.asyncio
    async def test_configure_attaches_an_inert_driver_when_off(self) -> None:
        client, web = make_client(live=False)
        driver = status.configure(self._orch(client, False))
        assert isinstance(driver, AgentSessionStatus) and not driver.active
        assert status.driver_of(client) is None
        await client.set_thread_status("C1", "1700.1", "is using bash")
        assert web.methods() == ["assistant.threads.setStatus"]

    @pytest.mark.asyncio
    async def test_the_setting_is_read_live(self) -> None:
        client, web = make_client(live=False)
        orch = self._orch(client, True)
        status.configure(orch)
        await client.set_thread_status("C1", "1700.1", "is using bash")
        orch._cfg.slack.agent_sessions = False  # a toggle applied to the live config
        await client.set_thread_status("C1", "1700.2", "is using bash")
        assert web.methods() == ["agents.sessions.setStatus", "assistant.threads.setStatus"]

    @pytest.mark.asyncio
    async def test_on_but_the_scope_not_granted_is_identical_to_main(self) -> None:
        """The real gate: the toggle on, the install's grant lacking assistant:write."""
        from kiro_crew.slack import features

        client, web = make_client(live=False)
        status.configure(self._orch(client, True))
        features.record_granted_scopes({"chat:write"})
        await client.set_thread_status("C1", "1700.1", "is using bash")
        assert web.methods() == ["assistant.threads.setStatus"]
        assert features.availability(features.AGENT_SESSIONS) == (False, "missing_scope")

    @pytest.mark.asyncio
    async def test_on_but_not_available_is_identical_to_main(self) -> None:
        """The setting on, but the install cannot run sessions (the gate says no)."""
        client, web = make_client(gate=lambda: False)
        await client.set_thread_status("C1", "1700.1", "is using bash")
        await client.set_thread_title("C1", "slack:1700.1", "Trip")
        assert web.methods() == ["assistant.threads.setStatus", "assistant.threads.setTitle"]
        assert await client.set_session_status("C1", None, SESSION_STATUS_ACTIVE) is False

    @pytest.mark.asyncio
    async def test_a_raising_gate_reads_as_off(self) -> None:
        def boom() -> bool:
            raise RuntimeError("probe failed")

        client, web = make_client(gate=boom)
        await client.set_thread_status("C1", "1700.1", "is using bash")
        assert web.methods() == ["assistant.threads.setStatus"]


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_a_turn_reads_processing_then_active(self) -> None:
        client, web = make_client()
        await client.set_thread_status("C1", "1700.1", "is working on your request")
        await client.set_thread_status("C1", "1700.1", "")
        assert web.calls == [
            (
                "agents.sessions.setStatus",
                {"channel_id": "C1", "status": "processing", "thread_ts": "1700.1"},
            ),
            (
                "agents.sessions.setStatus",
                {"channel_id": "C1", "status": "active", "thread_ts": "1700.1"},
            ),
        ]
        assert not any(m.startswith("assistant.threads") for m in web.methods())

    @pytest.mark.asyncio
    async def test_repeat_processing_is_not_resent(self) -> None:
        client, web = make_client()
        for text in ("is working on your request", "is using bash", "is_typing", "is using read"):
            await client.set_thread_status("C1", "1700.1", text)
        assert web.methods() == ["agents.sessions.setStatus"]

    @pytest.mark.asyncio
    async def test_an_approval_wait_reads_suspended_then_processing(self) -> None:
        client, web = make_client()
        await client.set_thread_status("C1", "1700.1", "is working on your request")
        await client.set_thread_status("C1", "1700.1", "Waiting for approval…")
        await client.set_thread_status("C1", "1700.1", "is using bash")
        assert [p["status"] for _, p in web.calls] == ["processing", "suspended", "processing"]

    @pytest.mark.asyncio
    async def test_clearing_a_status_never_set_creates_no_session(self) -> None:
        """setStatus creates a session; an error path's clear must not, so it goes the old way."""
        client, web = make_client()
        await client.set_thread_status("C1", "1700.1", "")
        assert web.methods() == ["assistant.threads.setStatus"]
        assert web.calls[0][1]["status"] == ""

    @pytest.mark.asyncio
    async def test_a_fallback_after_a_finished_native_turn_is_still_cleared(self) -> None:
        """A thread left ``active`` natively, then a turn whose native start fails:
        the old status it fell back to is the one the turn's end must clear."""
        client, web = make_client()
        await client.set_thread_status("C1", "1700.1", "is working on your request")
        await client.set_thread_status("C1", "1700.1", "")  # native: active
        web.errors["agents.sessions.setStatus"] = "internal_error"
        await client.set_thread_status("C1", "1700.1", "is working on your request")
        del web.errors["agents.sessions.setStatus"]
        await client.set_thread_status("C1", "1700.1", "")
        assert web.methods()[-2:] == ["assistant.threads.setStatus", "assistant.threads.setStatus"]
        assert web.calls[-1][1]["status"] == ""

    @pytest.mark.asyncio
    async def test_a_failed_approval_pause_still_lets_the_clear_end_the_native_session(
        self,
    ) -> None:
        """Native ``processing`` was accepted, then the approval pause's call fails
        and the old path shows the pause. The clear that follows (a turn end or a
        ``!stop``) must move the native session to ``active`` AND clear the old
        status: Slack still shows "Working..." and a Stop from the accepted call."""
        client, web = make_client()
        await client.set_thread_status("C1", "1700.1", "is working on your request")
        web.errors["agents.sessions.setStatus"] = "internal_error"
        assert not await status.set_session_status(client, "C1", "1700.1", SESSION_STATUS_SUSPENDED)
        await client.set_thread_status("C1", "1700.1", "Waiting for approval…")
        del web.errors["agents.sessions.setStatus"]
        web.calls.clear()
        await client.set_thread_status("C1", "1700.1", "")
        assert web.calls == [
            (
                "agents.sessions.setStatus",
                {"channel_id": "C1", "status": "active", "thread_ts": "1700.1"},
            ),
            (
                "assistant.threads.setStatus",
                {"channel_id": "C1", "thread_ts": "1700.1", "status": ""},
            ),
        ]
        # Settled: a second clear goes nowhere new.
        web.calls.clear()
        await client.set_thread_status("C1", "1700.1", "")
        assert web.methods() == []

    @pytest.mark.asyncio
    async def test_a_failed_native_start_is_cleared_by_the_old_call(self) -> None:
        """A transient refusal falls back to the old status; the turn's end must clear it."""
        client, web = make_client()
        web.errors["agents.sessions.setStatus"] = "internal_error"
        await client.set_thread_status("C1", "1700.1", "is working on your request")
        del web.errors["agents.sessions.setStatus"]
        await client.set_thread_status("C1", "1700.1", "")
        assert web.methods() == [
            "agents.sessions.setStatus",
            "assistant.threads.setStatus",
            "assistant.threads.setStatus",
        ]
        assert web.calls[-1][1]["status"] == ""

    @pytest.mark.asyncio
    async def test_a_whole_channel_session_omits_thread_ts(self) -> None:
        client, web = make_client()
        assert await status.set_session_status(client, "C1", None, SESSION_STATUS_PROCESSING)
        assert web.calls == [
            ("agents.sessions.setStatus", {"channel_id": "C1", "status": "processing"})
        ]

    @pytest.mark.asyncio
    async def test_an_org_install_routes_by_the_channel_team(self) -> None:
        client, web = make_client()
        client.record_channel_team("C1", "T9")
        await client.set_thread_status("C1", "1700.1", "is using bash")
        await client.set_thread_title("C1", "slack:1700.1", "Trip")
        assert all(p.get("team_id") == "T9" for _, p in web.calls)

    @pytest.mark.asyncio
    async def test_a_root_message_falls_back_to_the_old_call(self) -> None:
        """No thread, no thread session: identical to main's (failing) call."""
        client, web = make_client()
        await client.set_thread_status("D1", "", "is using bash")
        assert web.methods() == ["assistant.threads.setStatus"]


class TestOffMidTurn:
    """Agent sessions going off mid-turn (the toggle switched off, or a reconnect
    that could not read the scopes) still move the sessions this process set
    working back to ``active``: Slack would otherwise show a dead Stop on
    "Working..." until its own one-hour timeout."""

    @staticmethod
    def _switchable() -> tuple[RealSlackClient, RecordingWeb, list[bool], Clock]:
        on = [True]
        clock = Clock()
        client, web = make_client(gate=lambda: on[0], clock=clock)
        return client, web, on, clock

    @pytest.mark.asyncio
    async def test_the_end_of_turn_clear_still_reaches_the_native_session(self) -> None:
        client, web, on, _clock = self._switchable()
        await client.set_thread_status("C1", "1700.1", "is working on your request")
        on[0] = False
        await client.set_thread_status("C1", "1700.1", "")
        assert web.calls[-1] == (
            "agents.sessions.setStatus",
            {"channel_id": "C1", "status": "active", "thread_ts": "1700.1"},
        )
        # Nothing new is started natively while off.
        await client.set_thread_status("C1", "1700.1", "is working on your request")
        assert web.methods()[-1] == "assistant.threads.setStatus"

    @pytest.mark.asyncio
    async def test_a_paused_session_is_cleared_too(self) -> None:
        client, web, on, _clock = self._switchable()
        await client.set_thread_status("C1", "1700.1", "Waiting for approval…")
        on[0] = False
        await client.set_thread_status("C1", "1700.1", "")
        assert web.calls[-1][1]["status"] == "active"

    @pytest.mark.asyncio
    async def test_a_thread_never_set_natively_goes_the_old_way(self) -> None:
        client, web, on, _clock = self._switchable()
        on[0] = False
        await client.set_thread_status("C1", "1700.1", "")
        assert web.methods() == ["assistant.threads.setStatus"]

    @pytest.mark.asyncio
    async def test_the_keepalive_still_settles_a_turn_that_ended_while_off(self) -> None:
        client, web, on, clock = self._switchable()
        driver = client.agent_sessions
        reset = fence.enter_turn("C1", "1700.1")
        try:
            await client.set_thread_status("C1", "1700.1", "is working on your request")
        finally:
            fence.exit_turn(reset)  # the turn ends without clearing its status
        record = driver._records[("C1", "1700.1")]
        on[0] = False
        clock.now += PROCESSING_REFRESH_SECS + 1
        await driver._keepalive(("C1", "1700.1"), record)
        assert web.calls[-1][1]["status"] == "active"
        assert record.status == SESSION_STATUS_ACTIVE

    @pytest.mark.asyncio
    async def test_a_stop_settles_the_session_while_off(self) -> None:
        client, web, on, _clock = self._switchable()
        await client.set_thread_status("C1", "1700.1", "is working on your request")
        on[0] = False
        assert status.clearing_driver(client, "C1", "1700.1") is client.agent_sessions
        assert status.clearing_driver(client, "C1", "1800.1") is None
        await client.agent_sessions.settle_after_stop("C1", "1700.1")
        assert web.calls[-1][1]["status"] == "active"
        # Settled: nothing more is owed.
        assert status.clearing_driver(client, "C1", "1700.1") is None


class TestQueuedAck:
    """The 👀 ack follows what Slack shows: a live agent session's loader counts."""

    class _Reactions:
        def __init__(self) -> None:
            self.added: list[str] = []

        async def add_reaction(self, channel: str, ts: str, emoji: str) -> None:
            self.added.append(emoji)

        async def remove_reaction(self, channel: str, ts: str, emoji: str) -> None:
            pass

    async def _ack_for(self, client: RealSlackClient) -> list[str]:
        from kiro_crew.slack.handler_runtime.reactions import StatusReactionController

        shown = await client.set_thread_status("C1", "1700.1", "is working on your request")
        sink = self._Reactions()
        ctrl = StatusReactionController(sink, "C1", "1700.1")  # type: ignore[arg-type]
        ctrl.start(native_status=bool(shown))
        await asyncio.gather(*ctrl._tasks)
        return sink.added

    @pytest.mark.asyncio
    async def test_a_live_agent_session_shows_the_loader_so_no_ack(self) -> None:
        client, web = make_client()
        assert await self._ack_for(client) == []
        assert web.methods() == ["agents.sessions.setStatus"]

    @pytest.mark.asyncio
    async def test_a_refused_session_with_no_old_status_still_acks(self) -> None:
        client, web = make_client()
        web.errors["agents.sessions.setStatus"] = "internal_error"
        web.errors["assistant.threads.setStatus"] = "not_an_assistant_thread"
        added = await self._ack_for(client)
        assert len(added) == 1  # the queued ack
        assert web.methods() == ["agents.sessions.setStatus", "assistant.threads.setStatus"]


class TestChannelPolicy:
    """Native session writes honour the Slack channel policy, as the old calls do."""

    @pytest.fixture()
    def refused(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        from kiro_crew.slack import channel_policy

        asked: list[str] = []

        def verdict(channel: str) -> str:
            asked.append(channel)
            return "refused by the security policy" if channel == "CX" else ""

        monkeypatch.setattr(channel_policy, "_verdict", verdict)
        return asked

    @pytest.mark.asyncio
    async def test_a_refused_channel_gets_no_title_and_no_working_state(
        self, refused: list[str]
    ) -> None:
        client, web = make_client()
        await client.set_thread_title("CX", "slack:1700.1", "Bora Bora trip prep")
        await client.set_thread_status("CX", "1700.1", "is working on your request")
        await client.set_thread_status("CX", "1700.1", "Waiting for approval…")
        assert await status.set_session_status(client, "CX", "1700.1", "suspended") is True
        # Refused once, at the native seam: the old call is not tried either.
        assert web.calls == []
        assert refused and set(refused) == {"CX"}

    @pytest.mark.asyncio
    async def test_moving_a_session_back_to_active_stays_exempt(self, refused: list[str]) -> None:
        client, web = make_client()
        await client.set_thread_status("C1", "1700.1", "is working on your request")
        # The channel is withdrawn mid-turn: clearing "Working…" adds no content.
        assert (
            status._carries_content(
                "agents.sessions.setStatus", {"channel_id": "C1", "status": "active"}
            )
            is False
        )
        refused.clear()
        await client.agent_sessions.settle_after_stop("C1", "1700.1")
        assert web.calls[-1] == (
            "agents.sessions.setStatus",
            {"channel_id": "C1", "status": "active", "thread_ts": "1700.1"},
        )
        assert refused == []

    @pytest.mark.asyncio
    async def test_a_refused_start_records_nothing_so_the_clear_creates_no_session(
        self, refused: list[str]
    ) -> None:
        """A refused working state is not delivered, so the turn's end has nothing to
        move to ``active``: no native session is created in the refused channel."""
        client, web = make_client()
        await client.set_thread_status("CX", "1700.1", "is working on your request")
        await client.set_thread_title("CX", "slack:1700.1", "a title")
        driver = client.agent_sessions
        assert driver._records.get(("CX", "1700.1")) is None  # no status, no echo
        await client.set_thread_status("CX", "1700.1", "")
        assert "agents.sessions.setStatus" not in web.methods()
        assert "agents.sessions.rename" not in web.methods()

    @pytest.mark.asyncio
    async def test_a_channel_withdrawn_mid_turn_stops_the_keepalive_and_still_clears(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.slack import channel_policy

        withdrawn: set[str] = set()
        monkeypatch.setattr(
            channel_policy, "_verdict", lambda ch: "refused" if ch in withdrawn else ""
        )
        clock = Clock()
        client, web = make_client(clock=clock)
        driver = client.agent_sessions
        reset = fence.enter_turn("C1", "1700.1")
        try:
            await client.set_thread_status("C1", "1700.1", "is working on your request")
            record = driver._records[("C1", "1700.1")]
            withdrawn.add("C1")
            clock.now += PROCESSING_REFRESH_SECS + 1
            await driver._keepalive(("C1", "1700.1"), record)
        finally:
            fence.exit_turn(reset)
        # One delivered working state, no refresh into the withdrawn channel.
        assert web.methods() == ["agents.sessions.setStatus"]
        # The delivered status is still cleared at the turn's end (exempt).
        await client.set_thread_status("C1", "1700.1", "")
        assert web.calls[-1][1]["status"] == "active"

    @pytest.mark.asyncio
    async def test_an_allowed_channel_is_unchanged(self, refused: list[str]) -> None:
        client, web = make_client()
        await client.set_thread_title("C1", "slack:1700.1", "Bora Bora trip prep")
        assert web.methods() == ["agents.sessions.rename"]
        assert refused == ["C1"]


class TestTitle:
    @pytest.mark.asyncio
    async def test_a_session_key_title_renames_the_bare_thread(self) -> None:
        client, web = make_client()
        await client.set_thread_title("C1", "slack:1700.1", "Bora Bora trip prep")
        assert web.calls == [
            (
                "agents.sessions.rename",
                {"channel_id": "C1", "title": "Bora Bora trip prep", "thread_ts": "1700.1"},
            )
        ]
        assert client.agent_sessions.last_title("C1", "1700.1") == "Bora Bora trip prep"

    @pytest.mark.asyncio
    async def test_a_failed_rename_leaves_no_echo_to_wait_for(self) -> None:
        client, web = make_client()
        web.errors["agents.sessions.rename"] = "internal_error"
        await client.agent_sessions.rename("C1", "1700.1", "A")
        assert client.agent_sessions.take_echo("C1", "1700.1", "A") is False

    @pytest.mark.asyncio
    async def test_an_echo_arriving_before_the_reply_is_ours(self) -> None:
        client, web = make_client()
        seen: list[bool] = []
        api_call = web.api_call

        async def echo_first(method: str, **kw: Any) -> Any:
            # Socket Mode delivers the echo while the HTTP call is still open.
            seen.append(client.agent_sessions.take_echo("C1", "1700.1", "A"))
            return await api_call(method, **kw)

        web.api_call = echo_first  # type: ignore[method-assign]
        assert await client.agent_sessions.rename("C1", "1700.1", "A") is True
        assert seen == [True]

    @pytest.mark.asyncio
    async def test_an_echo_that_never_came_expires(self) -> None:
        clock = Clock()
        client, _web = make_client(clock=clock)
        await client.agent_sessions.rename("C1", "1700.1", "A")
        clock.now += 61
        # A person renaming to A a minute later is a person, not our stale echo.
        assert client.agent_sessions.take_echo("C1", "1700.1", "A") is False

    @pytest.mark.asyncio
    async def test_a_key_naming_no_thread_renames_nothing(self) -> None:
        client, web = make_client()
        await client.set_thread_title("D1", "slack:D0123", "Flat DM")
        await client.set_thread_title("C1", "chat-1-abc", "Dashboard")
        assert web.calls == []


class TestDegrade:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "code", ["feature_disabled", "missing_scope", "not_allowed_token_type"]
    )
    async def test_a_capability_refusal_latches_falls_back_and_notifies_once(
        self, code: str, _caps: list
    ) -> None:
        """The shared latch: one log line, one notification, the settings page sees it."""
        from kiro_crew.slack import features

        notes: list = []
        client, web = make_client(notes=notes)
        web.errors["agents.sessions.setStatus"] = code
        await client.set_thread_status("C1", "1700.1", "is using bash")
        await client.set_thread_status("C1", "1700.2", "is using bash")
        await client.set_thread_title("C1", "slack:1700.2", "Trip")
        assert web.methods() == [
            "agents.sessions.setStatus",
            "assistant.threads.setStatus",  # the very call that failed still lands
            "assistant.threads.setStatus",
            "assistant.threads.setTitle",
        ]
        assert _caps == [(features.AGENT_SESSIONS, code)] and notes == []
        assert features.latched() == {features.AGENT_SESSIONS: code}
        assert status.driver_of(client) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", ["not_allowed", "unknown_method", "not_authorized"])
    async def test_any_other_refusal_falls_back_for_that_call_only(
        self, code: str, _caps: list
    ) -> None:
        """No second latch: the old call lands, nothing is switched off, nobody notified."""
        notes: list = []
        client, web = make_client(notes=notes)
        web.errors["agents.sessions.setStatus"] = code
        await client.set_thread_status("C1", "1700.1", "is using bash")
        web.errors.clear()
        await client.set_thread_status("C1", "1700.2", "is using bash")
        assert web.methods() == [
            "agents.sessions.setStatus",
            "assistant.threads.setStatus",  # the refused call still lands
            "agents.sessions.setStatus",
        ]
        assert notes == [] and _caps == []
        assert client.agent_sessions.active

    @pytest.mark.asyncio
    async def test_a_missing_stop_subscription_is_reported_once(self) -> None:
        notes: list = []
        client, web = make_client(notes=notes)
        web.responses["agents.sessions.setStatus"] = {
            "ok": True,
            "warning": "missing_agent_session_stopped_event_subscription",
        }
        await client.set_thread_status("C1", "1700.1", "is using bash")
        await client.set_thread_status("C1", "1700.2", "is using bash")
        assert len(notes) == 1 and "Stop" in notes[0][0][1]
        assert client.agent_sessions.active


class TestKeepalive:
    @pytest.mark.asyncio
    async def test_a_long_turn_resends_processing_and_an_ended_turn_goes_active(self) -> None:
        clock = Clock()
        sleeps: list[float] = []

        async def fake_sleep(secs: float) -> None:
            sleeps.append(secs)
            clock.now += secs
            await asyncio.sleep(0)

        client, web = make_client(clock=clock, sleep=fake_sleep)
        real_sleep = asyncio.sleep
        reset = fence.enter_turn("C1", "1700.1")
        try:
            await client.set_thread_status("C1", "1700.1", "is using bash")
            for _ in range(5):
                await real_sleep(0)
            # Still inside the turn after one window: processing again.
            assert [p["status"] for _, p in web.calls][:2] == ["processing", "processing"]
            assert sleeps[0] == PROCESSING_REFRESH_SECS
        finally:
            fence.exit_turn(reset)
        for _ in range(10):
            await real_sleep(0)
        # The turn ended without clearing its status: the keepalive does it.
        assert [p["status"] for _, p in web.calls][-1] == "active"
        await client.agent_sessions.close()

    @pytest.mark.asyncio
    async def test_no_keepalive_outside_a_turn(self) -> None:
        client, _ = make_client()
        await client.set_thread_status("C1", "1700.1", "is using bash")
        record = client.agent_sessions._records[("C1", "1700.1")]
        assert record.keepalive is None


#: Slack Web API calls that put text, files or status in front of a reader. Every
#: ``RealSlackClient`` method that makes one must ask the fence first.
_VISIBLE_WRITES = (
    "chat_postMessage",
    "chat_update",
    "chat_postEphemeral",
    "files_upload",
    '"chat.startStream"',
    '"chat.appendStream"',
    '"chat.stopStream"',
    '"assistant.threads.',
    '"agents.sessions.',
)


def test_every_visible_client_write_asks_the_fence() -> None:
    """A write method added without ``turn_muted(...)`` would post after Stop."""
    import ast
    import inspect

    from kiro_crew.slack import client as client_mod

    tree = ast.parse(inspect.getsource(client_mod))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "RealSlackClient")
    writers: dict[str, bool] = {}
    for fn in cls.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        src = ast.get_source_segment(inspect.getsource(client_mod), fn) or ""
        if any(marker in src for marker in _VISIBLE_WRITES):
            writers[fn.name] = "turn_muted(" in src
    assert {"post_message", "post_blocks", "update_message", "post_ephemeral"} <= set(writers)
    assert {"start_stream", "append_stream", "append_task", "stop_stream"} <= set(writers)
    assert {"set_thread_status", "set_thread_title", "upload_file"} <= set(writers)
    unfenced = sorted(name for name, fenced in writers.items() if not fenced)
    assert unfenced == []


class TestWriteFence:
    @pytest.mark.asyncio
    async def test_a_stopped_turn_posts_nothing_and_the_next_turn_does(self) -> None:
        client, web = make_client()
        reset = fence.enter_turn("C1", "1700.1")
        try:
            await client.post_message("C1", "partial", "1700.1")
            assert fence.mute_turns("C1", "1700.1")
            assert await client.post_message("C1", "late answer", "1700.1") == ""
            await client.post_blocks("C1", [], "footer", "1700.1")
            await client.update_message("C1", "9.9", text="sealed")
            await client.post_ephemeral("C1", "U1", "hi", thread_ts="1700.1")
            assert await client.start_stream("C1", "1700.1") is None
            assert await client.append_stream("C1", "9.9", "more") is False
            assert await client.stop_stream("C1", "9.9", "final") is False
            await client.set_thread_status("C1", "1700.1", "is using bash")
            await client.set_thread_title("C1", "slack:1700.1", "late title")
        finally:
            fence.exit_turn(reset)
        assert web.methods() == ["chat.postMessage"]
        # A new turn in the same thread is a new scope.
        reset = fence.enter_turn("C1", "1700.1")
        try:
            await client.post_message("C1", "next answer", "1700.1")
        finally:
            fence.exit_turn(reset)
        assert web.methods() == ["chat.postMessage", "chat.postMessage"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("live", [True, False])
    async def test_a_stop_in_one_thread_leaves_other_threads_titles_alone(self, live: bool) -> None:
        """The fence is keyed by thread: a title for another thread still goes out."""
        client, web = make_client(live=live)
        reset = fence.enter_turn("C1", "1700.1")
        try:
            assert fence.mute_turns("C1", "1700.1")
            await client.set_thread_title("C1", "slack:1700.1", "the stopped thread")
            await client.set_thread_title("C1", "slack:1800.1", "another thread")
        finally:
            fence.exit_turn(reset)
        titles = [p.get("title") for _m, p in web.calls]
        assert titles == ["another thread"]
        assert web.calls[0][1]["thread_ts"] in ("1800.1", "slack:1800.1")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call", ["status", "title"])
    async def test_a_stop_during_a_failed_native_call_blocks_the_old_fallback(
        self, call: str
    ) -> None:
        """A Stop that lands while the native call is in flight, which then fails,
        must not let the old call post after the Stop."""
        client, web = make_client()
        method = "agents.sessions.setStatus" if call == "status" else "agents.sessions.rename"
        real = web.api_call

        async def stop_then_fail(m: str, **kw: Any) -> Any:
            if m == method:
                fence.mute_turns("C1", "1700.1")  # the Stop lands mid-call
                web.calls.append((m, dict(kw.get("params") or {})))
                raise SlackApiError("internal_error")
            return await real(m, **kw)

        web.api_call = stop_then_fail  # type: ignore[method-assign]
        reset = fence.enter_turn("C1", "1700.1")
        try:
            if call == "status":
                await client.set_thread_status("C1", "1700.1", "Waiting for approval…")
            else:
                await client.set_thread_title("C1", "slack:1700.1", "late title")
        finally:
            fence.exit_turn(reset)
        assert web.methods() == [method]

    @pytest.mark.asyncio
    async def test_a_cancel_after_a_native_stop_deletes_the_stop_block_but_not_the_status(
        self,
    ) -> None:
        """The cancel arm's cleanup task runs in the stopped scope: its status clear
        is dropped (the Stop handler settles the session itself), while the inline
        Stop block, a delete rather than content, still comes down."""
        client, web = make_client()
        deleted: list[dict[str, Any]] = []

        async def chat_delete(**kwargs: Any) -> None:
            deleted.append(kwargs)

        web.chat_delete = chat_delete  # type: ignore[attr-defined]
        reset = fence.enter_turn("C1", "1700.1")
        try:
            assert fence.mute_turns("C1", "1700.1")

            async def clear_working_indicators() -> None:
                await client.set_thread_status("C1", "1700.1", "")
                await client.delete_message("C1", "1700.5")

            cleanup = asyncio.ensure_future(clear_working_indicators())
        finally:
            fence.exit_turn(reset)
        await await_until(cleanup, what="the cancel arm's cleanup")
        assert web.methods() == []
        assert [d["ts"] for d in deleted] == ["1700.5"]

    @pytest.mark.asyncio
    async def test_tasks_the_turn_spawned_are_fenced_too(self) -> None:
        client, web = make_client()
        gate = asyncio.Event()

        async def background() -> None:
            await gate.wait()
            await client.post_message("C1", "from a child task", "1700.1")

        reset = fence.enter_turn("C1", "1700.1")
        try:
            child = asyncio.ensure_future(background())
            fence.mute_turns("C1", "1700.1")
        finally:
            fence.exit_turn(reset)
        gate.set()
        await await_until(child, what="the stopped turn's child task")
        assert web.methods() == []

    @pytest.mark.asyncio
    async def test_a_task_the_stopped_turn_started_still_posts_elsewhere(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A watcher or completion notice outliving the turn is not silenced by its Stop."""
        client, web = make_client()
        gate = asyncio.Event()

        async def watcher() -> None:
            await gate.wait()
            await client.post_message("C1", "own thread", "1700.1")
            await client.post_message("C1", "other thread", "1700.2")
            await client.post_message("C2", "other channel", "1800.1")

        reset = fence.enter_turn("C1", "1700.1")
        try:
            child = asyncio.ensure_future(watcher())
            fence.mute_turns("C1", "1700.1")
        finally:
            fence.exit_turn(reset)
        caplog.set_level("DEBUG", logger="kiro_crew.slack.agent_sessions.fence")
        gate.set()
        await await_until(child, what="the watcher")
        assert [kw["text"] for _m, kw in web.calls] == ["other thread", "other channel"]
        assert "dropped: the turn in C1/1700.1 was stopped" in caplog.text

    @pytest.mark.asyncio
    async def test_an_edit_is_fenced_by_which_message_it_names(self) -> None:
        """A stopped turn's own message is frozen; another message in its channel is not."""
        client, web = make_client()
        reset = fence.enter_turn("C1", "1700.1")
        try:
            own = await client.post_message("C1", "partial", "1700.1")
            fence.mute_turns("C1", "1700.1")
            await client.update_message("C1", own, text="late edit")
            assert await client.append_stream("C1", own, "more") is False
            await client.update_message("C1", "1600.9", text="a message in another thread")
            assert await client.stop_stream("C1", "1600.8") is True
        finally:
            fence.exit_turn(reset)
        assert web.methods() == ["chat.postMessage", "chat.update", "chat.stopStream"]
        assert web.calls[1][1]["ts"] == "1600.9"

    @pytest.mark.asyncio
    async def test_a_stop_marks_only_its_own_thread(self) -> None:
        client, web = make_client()
        a = fence.enter_turn("C1", "1700.1")
        try:
            other = asyncio.ensure_future(_post_in_turn(client, "C1", "1700.2"))
            fence.mute_turns("C1", "1700.1")
            await await_until(other, what="the other thread's post")
            await client.post_message("C1", "muted", "1700.1")
        finally:
            fence.exit_turn(a)
        assert [kw["text"] for m, kw in web.calls] == ["other thread"]

    @pytest.mark.asyncio
    async def test_fenced_turn_reads_the_turn_shape(self) -> None:
        seen: list[Any] = []

        @fence.fenced_turn
        async def entry(slack, sessions, channel, text, thread_ts, msg_ts, user_id):  # noqa: ANN001
            seen.append((fence.current_scope().channel, fence.current_scope().thread_ts))

        await entry(None, None, "C1", "hi", None, "1700.5", "U1")
        await entry(
            None, None, channel="C2", text="hi", thread_ts="1700.6", msg_ts="1", user_id="U"
        )
        assert seen == [("C1", "1700.5"), ("C2", "1700.6")]
        assert fence.current_scope() is None


async def _post_in_turn(client: RealSlackClient, channel: str, thread_ts: str) -> None:
    reset = fence.enter_turn(channel, thread_ts)
    try:
        await asyncio.sleep(0)
        await client.post_message(channel, "other thread", thread_ts)
    finally:
        fence.exit_turn(reset)


class TestRendererApprovalPause:
    """The transport renderer marks a tool-approval wait ``suspended``."""

    @pytest.mark.asyncio
    async def test_an_approval_prompt_suspends_and_the_next_output_resumes(self) -> None:
        from kiro_crew.slack.renderer import SlackRenderer

        client, web = make_client()
        renderer = SlackRenderer(client, "C1", "1700.1", reactions_enabled=False)
        await renderer.on_turn_start()
        await renderer.on_prompt_choice([{"id": "allow", "label": "Allow"}], 1, tool_title="bash")
        await renderer.on_text_chunk("done")
        statuses = [p["status"] for m, p in web.calls if m == "agents.sessions.setStatus"]
        assert statuses == ["processing", "suspended", "processing"]
        await renderer.close()

    @pytest.mark.asyncio
    async def test_the_decision_puts_the_session_back_to_processing(self) -> None:
        """The approved tool runs with its loader and Stop button, before any output."""
        from kiro_crew.slack.renderer import SlackApprovalDecider, SlackRenderer

        client, web = make_client()
        decider = SlackApprovalDecider("slack:1700.1")
        renderer = SlackRenderer(client, "C1", "1700.1", reactions_enabled=False, decider=decider)
        await renderer.on_turn_start()
        await renderer.on_prompt_choice([{"id": "allow", "label": "Allow"}], 7, tool_title="bash")
        decider._futures["7"].set_result(True)  # the click
        for _ in range(5):
            await asyncio.sleep(0)
        statuses = [p["status"] for m, p in web.calls if m == "agents.sessions.setStatus"]
        assert statuses == ["processing", "suspended", "processing"]
        decider.discard(7)
        await renderer.close()

    @pytest.mark.asyncio
    async def test_a_click_that_beats_the_registration_still_resumes(self) -> None:
        from kiro_crew.slack.renderer import SlackApprovalDecider

        decider = SlackApprovalDecider("slack:1700.1")
        decider.reserve(9)
        decider._futures["9"].set_result(True)
        fired: list[bool] = []
        assert decider.on_decided(9, lambda: fired.append(True))
        await asyncio.sleep(0)
        assert fired == [True]
        decider.discard(9)

    @pytest.mark.asyncio
    async def test_a_resume_in_flight_lands_before_the_closing_active(self) -> None:
        from kiro_crew.slack.renderer import SlackApprovalDecider, SlackRenderer

        client, web = make_client()
        gate = asyncio.Event()
        real_call = web.api_call

        async def slow_processing(method: str, *, params: Any = None, json: Any = None) -> Any:
            if (params or {}).get("status") == "processing" and web.calls:
                await gate.wait()
            return await real_call(method, params=params, json=json)

        web.api_call = slow_processing  # type: ignore[method-assign]
        decider = SlackApprovalDecider("slack:1700.1")
        renderer = SlackRenderer(client, "C1", "1700.1", reactions_enabled=False, decider=decider)
        await renderer.on_turn_start()
        await renderer.on_prompt_choice([{"id": "allow", "label": "Allow"}], 10, tool_title="bash")
        decider._futures["10"].set_result(True)
        await asyncio.sleep(0)
        await asyncio.sleep(0)  # the resume is now waiting inside setStatus
        done = asyncio.ensure_future(renderer.on_done())
        await asyncio.sleep(0)
        gate.set()
        await await_until(done, what="on_done")
        statuses = [p["status"] for m, p in web.calls if m == "agents.sessions.setStatus"]
        assert statuses == ["processing", "suspended", "processing", "active"]
        decider.discard(10)
        await renderer.close()

    @pytest.mark.asyncio
    async def test_the_held_reply_goes_out_after_an_approval_before_the_closing_active(
        self,
    ) -> None:
        """The reply is held until the turn ends: with agent sessions live and an
        approval in the turn, ``on_done`` still delivers it, and before ``active``."""
        from kiro_crew.slack.renderer import SlackApprovalDecider, SlackRenderer

        client, web = make_client()
        decider = SlackApprovalDecider("slack:1700.1")
        renderer = SlackRenderer(client, "C1", "1700.1", reactions_enabled=False, decider=decider)
        await renderer.on_turn_start()
        await renderer.on_prompt_choice([{"id": "allow", "label": "Allow"}], 14, tool_title="bash")
        decider._futures["14"].set_result(True)  # the click
        await renderer.on_text_chunk("the final answer")
        await renderer.on_done()
        carries_reply = [
            i for i, (_m, kw) in enumerate(web.calls) if "the final answer" in repr(kw)
        ]
        active = [
            i
            for i, (m, kw) in enumerate(web.calls)
            if m == "agents.sessions.setStatus" and kw.get("status") == "active"
        ]
        assert carries_reply, web.methods()
        assert active, web.methods()
        assert carries_reply[0] < active[0]
        decider.discard(14)
        await renderer.close()

    @pytest.mark.asyncio
    async def test_teardown_waits_for_a_cancelled_resume_before_clearing(self) -> None:
        """A resume still in flight at ``close()`` has finished unwinding before the
        closing clear, so its ``processing`` cannot reach Slack after it."""
        from kiro_crew.slack.renderer import SlackApprovalDecider, SlackRenderer

        client, web = make_client()
        gate = asyncio.Event()
        real_call = web.api_call

        async def slow_processing(method: str, *, params: Any = None, json: Any = None) -> Any:
            if (params or {}).get("status") == "processing" and web.calls:
                await gate.wait()
            return await real_call(method, params=params, json=json)

        web.api_call = slow_processing  # type: ignore[method-assign]
        decider = SlackApprovalDecider("slack:1700.1")
        renderer = SlackRenderer(client, "C1", "1700.1", reactions_enabled=False, decider=decider)
        await renderer.on_turn_start()
        await renderer.on_prompt_choice([{"id": "allow", "label": "Allow"}], 15, tool_title="bash")
        decider._futures["15"].set_result(True)
        await asyncio.sleep(0)
        await asyncio.sleep(0)  # the resume now waits inside setStatus
        assert renderer._resume_tasks
        await renderer.close()
        assert renderer._resume_tasks == set()
        statuses = [p["status"] for m, p in web.calls if m == "agents.sessions.setStatus"]
        assert statuses[-1] != "processing"
        decider.discard(15)

    @pytest.mark.asyncio
    async def test_a_late_resume_cannot_overwrite_the_next_approvals_pause(self) -> None:
        """GPT on a5d92c0b75: a fast approved tool whose ``processing`` is still on
        its way, then a second approval. The second ``suspended`` must land last."""
        from kiro_crew.slack.renderer import SlackApprovalDecider, SlackRenderer

        client, web = make_client()
        gate = asyncio.Event()
        real_call = web.api_call

        async def slow_processing(method: str, *, params: Any = None, json: Any = None) -> Any:
            if (params or {}).get("status") == "processing" and web.calls:
                await gate.wait()
            return await real_call(method, params=params, json=json)

        web.api_call = slow_processing  # type: ignore[method-assign]
        decider = SlackApprovalDecider("slack:1700.1")
        renderer = SlackRenderer(client, "C1", "1700.1", reactions_enabled=False, decider=decider)
        await renderer.on_turn_start()
        await renderer.on_prompt_choice([{"id": "allow", "label": "Allow"}], 12, tool_title="bash")
        decider._futures["12"].set_result(True)
        await asyncio.sleep(0)
        await asyncio.sleep(0)  # the first resume now waits inside setStatus
        second = asyncio.ensure_future(
            renderer.on_prompt_choice([{"id": "allow", "label": "Allow"}], 13, tool_title="bash")
        )
        for _ in range(3):
            await asyncio.sleep(0)
        gate.set()
        await await_until(second, what="second approval prompt")
        statuses = [p["status"] for m, p in web.calls if m == "agents.sessions.setStatus"]
        assert statuses == ["processing", "suspended", "processing", "suspended"]
        decider.discard(12)
        decider.discard(13)
        await renderer.close()

    @pytest.mark.asyncio
    async def test_a_decision_after_the_turn_ended_changes_nothing(self) -> None:
        from kiro_crew.slack.renderer import SlackApprovalDecider, SlackRenderer

        client, web = make_client()
        decider = SlackApprovalDecider("slack:1700.1")
        renderer = SlackRenderer(client, "C1", "1700.1", reactions_enabled=False, decider=decider)
        await renderer.on_prompt_choice([{"id": "allow", "label": "Allow"}], 8, tool_title="bash")
        fut = decider._futures["8"]
        await renderer.close()
        web.calls.clear()
        fut.cancel()  # a teardown after the turn
        for _ in range(5):
            await asyncio.sleep(0)
        assert web.calls == []
        decider.discard(8)

    @pytest.mark.asyncio
    async def test_the_native_path_resumes_after_the_decision(self) -> None:
        from kiro_crew.slack.handler_runtime.stream import _AnswerStream

        client, web = make_client()
        answer = _AnswerStream.__new__(_AnswerStream)
        answer.slack, answer.channel, answer.reply_ts = client, "C1", "1700.1"
        answer.use_slack_stream, answer.status_dirty = True, True
        await client.set_thread_status("C1", "1700.1", "Waiting for approval…")
        await answer.resume_after_approval()
        assert [p["status"] for _, p in web.calls] == ["suspended", "processing"]

    @pytest.mark.asyncio
    async def test_the_resume_still_runs_after_the_stream_fell_back_to_messages(self) -> None:
        # The approval flush can fail both the append and the rotation, which
        # drops the turn to message posting; the session must still leave
        # ``suspended`` so the approved tool runs with its native Stop showing.
        from kiro_crew.slack.handler_runtime.stream import _AnswerStream

        client, web = make_client()
        answer = _AnswerStream.__new__(_AnswerStream)
        answer.slack, answer.channel, answer.reply_ts = client, "C1", "1700.1"
        answer.use_slack_stream, answer.status_dirty = False, True
        await client.set_thread_status("C1", "1700.1", "Waiting for approval…")
        await answer.resume_after_approval()
        assert [p["status"] for _, p in web.calls] == ["suspended", "processing"]

    @pytest.mark.asyncio
    async def test_no_resume_without_an_approval_status(self) -> None:
        from kiro_crew.slack.handler_runtime.stream import _AnswerStream

        client, web = make_client()
        answer = _AnswerStream.__new__(_AnswerStream)
        answer.slack, answer.channel, answer.reply_ts = client, "C1", "1700.1"
        answer.use_slack_stream, answer.status_dirty = True, False
        await answer.resume_after_approval()
        assert web.calls == []

    @pytest.mark.asyncio
    async def test_the_native_resume_is_silent_with_the_feature_off(self) -> None:
        from kiro_crew.slack.handler_runtime.stream import _AnswerStream

        client, web = make_client(live=False)
        answer = _AnswerStream.__new__(_AnswerStream)
        answer.slack, answer.channel, answer.reply_ts = client, "C1", "1700.1"
        answer.use_slack_stream, answer.status_dirty = True, True
        await answer.resume_after_approval()
        assert web.calls == []

    @pytest.mark.asyncio
    async def test_with_the_feature_off_the_prompt_sets_no_status(self) -> None:
        from kiro_crew.slack.renderer import SlackRenderer

        client, web = make_client(live=False)
        renderer = SlackRenderer(client, "C1", "1700.1", reactions_enabled=False)
        await renderer.on_prompt_choice([{"id": "allow", "label": "Allow"}], 1, tool_title="bash")
        assert [m for m in web.methods() if "status" in m.lower()] == []
        await renderer.close()
