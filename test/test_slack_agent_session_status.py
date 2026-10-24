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
    """A clean capability record with both scopes granted; yields latch notices."""
    from kiro_crew.slack import features

    seen: list[tuple[str, str]] = []
    features.reset_capabilities()
    features.record_granted_scopes({"assistant:write", "code_channels:manage"})
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
        """setStatus creates a session; an error path's clear must not."""
        client, web = make_client()
        await client.set_thread_status("C1", "1700.1", "")
        assert web.calls == []

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
        await child
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
            await other
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
