"""Channel paths hold a KAS plan handoff and a KAS question for a person (G3).

The dashboard's trust rungs leave two kinds of chat permission request to the
person in every mode (``kas_workflows.chat.needs_a_person``):

- a KAS workflow plan handoff (``_meta.kiro.toolId: "update_workflow"``), and
- a KAS ``user_input`` question that reaches Crew through KAS's legacy bridge,
  one ``allow_once`` option per answer (``AcpEvent.answers_only``), where any
  approval would select the first answer.

Every channel path with its own auto-approve rules must hold them too. Each test
below drives a rung that approves an ordinary request and asserts the KAS one is
NOT approved; the paired ordinary request proves the rung itself is live, so the
assertion fails as soon as the hold is removed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST, AcpEvent
from kiro_crew.messaging import (
    APPROVAL_AUTO,
    APPROVAL_INTERACTIVE,
    APPROVAL_TRUST,
    APPROVAL_TRUST_READS,
    TransportCapabilities,
    TurnDriver,
)
from kiro_crew.messaging.renderer import Renderer

_ANSWERS = [
    {"optionId": "a1", "kind": "allow_once", "name": "Use Postgres"},
    {"optionId": "a2", "kind": "allow_once", "name": "Use SQLite"},
]


def _question(request_id: str = "rq1", **extra) -> AcpEvent:
    """A KAS ``user_input`` question as the legacy bridge sends it."""
    return AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id=request_id,
        title="Which database should the service use?",
        tool_kind="read",
        options=list(_ANSWERS),
        kas_builtin_ids=True,
        answers_only=True,
        **extra,
    )


def _plan(request_id: str = "rq1", **extra) -> AcpEvent:
    """A KAS workflow plan handoff (``update_workflow``)."""
    return AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id=request_id,
        title="Approve the workflow plan",
        tool_kind="read",
        options=[{"optionId": "ok", "kind": "allow_once", "name": "Approve"}],
        harness_tool_id="update_workflow",
        kas_builtin_ids=True,
        **extra,
    )


def _ordinary(request_id: str = "rq1", *, title: str = "fs_read", **extra) -> AcpEvent:
    return AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id=request_id,
        title=title,
        tool_kind="read",
        options=[{"optionId": "allow", "kind": "allow_once", "name": "Allow"}],
        **extra,
    )


_PERSON_ONLY = [pytest.param(_question, id="question"), pytest.param(_plan, id="plan")]


# ── messaging.driver.TurnDriver (every ChannelTurn channel, Slack/Telegram transport) ──


class _Renderer(Renderer):
    def __init__(self) -> None:
        super().__init__(TransportCapabilities())
        self.prompts: list[str] = []

    async def on_prompt_choice(
        self, options, request_id, tool_title="", tool_purpose="", tool_input=""
    ):
        self.prompts.append(str(request_id))

    async def on_text_chunk(self, text):
        pass

    async def on_thinking(self, text):
        pass

    async def on_tool_call(self, tool_call_id, title, tool_kind="", tool_purpose=""):
        pass

    async def on_compaction(self, pct):
        pass

    async def on_done(self, stop_reason=""):
        pass

    async def on_steer_consumed(self, summary=""):
        pass


class _Provider:
    def __init__(self, event: AcpEvent) -> None:
        self._events = [event, AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")]
        self.approved: list = []
        self.rejected: list = []

    async def stream(self, message):
        for ev in self._events:
            yield ev

    async def approve_tool(self, request_id, *, always=False):
        self.approved.append(request_id)

    async def reject_tool(self, request_id):
        self.rejected.append(request_id)


def _drive(event: AcpEvent, **kw) -> tuple[_Provider, _Renderer]:
    provider, renderer = _Provider(event), _Renderer()
    asyncio.run(TurnDriver(provider, renderer, **kw).run("hello"))
    return provider, renderer


#: Every driver rung that approves an ordinary request on its own.
_DRIVER_RUNGS = [
    pytest.param({"approval_mode": APPROVAL_AUTO}, id="mode-auto"),
    pytest.param({"approval_mode": APPROVAL_TRUST}, id="mode-trust"),
    pytest.param({"approval_mode": APPROVAL_TRUST_READS}, id="mode-trust-reads"),
    pytest.param(
        {"approval_mode": APPROVAL_INTERACTIVE, "auto_approve_session": lambda: True},
        id="session-trust-or-yolo",
    ),
    pytest.param(
        {"approval_mode": APPROVAL_INTERACTIVE, "auto_approve_tool": lambda event: True},
        id="spawn-hook",
    ),
    pytest.param(
        {"approval_mode": APPROVAL_INTERACTIVE, "tool_gate": lambda event: "auto_approve"},
        id="hook-auto-approve",
    ),
]


class TestTurnDriverHoldsForAPerson:
    @pytest.mark.parametrize("rung", _DRIVER_RUNGS)
    def test_the_rung_approves_an_ordinary_request(self, rung):
        provider, _ = _drive(_ordinary(), **rung)
        assert provider.approved == ["rq1"]

    @pytest.mark.parametrize("make", _PERSON_ONLY)
    @pytest.mark.parametrize("rung", _DRIVER_RUNGS)
    def test_no_rung_answers_it_and_without_buttons_it_is_refused(self, rung, make):
        provider, renderer = _drive(make(), **rung)
        assert provider.approved == []
        assert provider.rejected == ["rq1"]
        assert renderer.prompts == []

    @pytest.mark.parametrize("make", _PERSON_ONLY)
    @pytest.mark.parametrize("rung", _DRIVER_RUNGS)
    def test_the_person_decides_through_the_buttons_in_every_mode(self, rung, make):
        asked: list = []

        async def decider(event):
            asked.append(event.request_id)
            return True

        provider, renderer = _drive(make(), decider=decider, **rung)
        assert asked == ["rq1"]
        # The buttons were drawn even outside interactive mode, then the
        # person's click (the decider) answered.
        assert renderer.prompts == ["rq1"]
        assert provider.approved == ["rq1"]

    @pytest.mark.parametrize("make", _PERSON_ONLY)
    def test_a_person_saying_no_rejects_it(self, make):
        async def decider(event):
            return False

        provider, _ = _drive(make(), approval_mode=APPROVAL_AUTO, decider=decider)
        assert provider.approved == []
        assert provider.rejected == ["rq1"]

    @pytest.mark.parametrize("make", _PERSON_ONLY)
    def test_a_hook_deny_still_wins(self, make):
        async def decider(event):  # pragma: no cover - must not be reached
            raise AssertionError("a denied request must not reach the person")

        provider, _ = _drive(
            make(), approval_mode=APPROVAL_INTERACTIVE, decider=decider, tool_gate=lambda e: "deny"
        )
        assert provider.approved == []
        assert provider.rejected == ["rq1"]

    def test_a_question_from_a_non_kas_backend_is_not_held(self):
        # ``answers_only`` alone is not a KAS question (``kas_builtin_ids``).
        provider, _ = _drive(
            _ordinary(answers_only=True, kas_builtin_ids=False), approval_mode=APPROVAL_AUTO
        )
        assert provider.approved == ["rq1"]


# ── slack.gateway: background approvals (cron, subagent, autonudge, task) ──


def _gateway(*, approval_mode=None, auto_sources=(), slot=None):
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gateway = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gateway.sessions = MagicMock()
    gateway.slack = None
    gateway._owner_id = ""
    gateway.dashboard_state = MagicMock()
    gateway.dashboard_state._yolo = False
    gateway.dashboard_state._slots = {"chat-1": slot} if slot is not None else {}
    gateway.dashboard_state.dashboard_user_ws_count = MagicMock(return_value=1)
    # The human prompt: the person says no, so an approve can only come from a rung.
    gateway.dashboard_state.request_approval = AsyncMock(return_value=False)
    gateway.dashboard_state.resolve_approval = MagicMock()
    gateway._cfg = MagicMock()
    gateway._cfg.hooks.get = MagicMock(return_value=list(auto_sources))
    gateway._approval_mode = approval_mode
    return gateway


def _gateway_rungs(monkeypatch):
    """Each rung of ``_interactive_approval`` that auto-approves, as a gateway."""
    import kiro_crew.slack.gateway as gw

    trusted_slot = SimpleNamespace()
    monkeypatch.setattr(gw, "_slot_is_trusted", lambda slot: slot is trusted_slot)
    monkeypatch.setattr(gw, "_auto_approve_reason", lambda slot, _ro: "session_trust")
    return {
        "auto-approve-source": (lambda: _gateway(auto_sources=["cron"]), ""),
        "approval-yolo": (lambda: _gateway(approval_mode="yolo"), ""),
        "slot-trust": (lambda: _gateway(slot=trusted_slot), "dashboard:chat-1"),
    }


@pytest.fixture()
def _no_override(monkeypatch):
    import kiro_crew.slack.gateway as gw

    monkeypatch.setattr(gw, "safety_override", lambda: SimpleNamespace(is_active=lambda: False))
    monkeypatch.setattr(gw, "dashboard_slot_key", lambda key: key.removeprefix("dashboard:"))
    monkeypatch.setattr(gw, "sel", lambda: MagicMock())


@pytest.mark.usefixtures("_no_override")
class TestGatewayBackgroundApprovalHolds:
    @pytest.mark.parametrize("rung", ["auto-approve-source", "approval-yolo", "slot-trust"])
    @pytest.mark.asyncio
    async def test_the_rung_approves_an_ordinary_request(self, monkeypatch, rung):
        make, parent = _gateway_rungs(monkeypatch)[rung]
        gateway = make()
        approve = gateway._interactive_approval("cron")
        assert await approve(_ordinary(), parent) is True
        gateway.dashboard_state.request_approval.assert_not_awaited()

    @pytest.mark.parametrize("make_event", _PERSON_ONLY)
    @pytest.mark.parametrize("rung", ["auto-approve-source", "approval-yolo", "slot-trust"])
    @pytest.mark.asyncio
    async def test_the_kas_request_goes_to_the_person(self, monkeypatch, rung, make_event):
        make, parent = _gateway_rungs(monkeypatch)[rung]
        gateway = make()
        approve = gateway._interactive_approval("cron")
        assert await approve(make_event(), parent) is False
        gateway.dashboard_state.request_approval.assert_awaited_once()

    @pytest.mark.parametrize("make_event", _PERSON_ONLY)
    @pytest.mark.asyncio
    async def test_yolo_does_not_answer_it(self, monkeypatch, make_event):
        import kiro_crew.slack.gateway as gw

        monkeypatch.setattr(gw, "safety_override", lambda: SimpleNamespace(is_active=lambda: True))
        gateway = _gateway()
        approve = gateway._interactive_approval("cron")
        assert await approve(_ordinary(), "") is True
        assert await approve(make_event(), "") is False
        gateway.dashboard_state.request_approval.assert_awaited_once()

    @pytest.mark.parametrize("make_event", _PERSON_ONLY)
    @pytest.mark.asyncio
    async def test_with_no_surface_it_is_refused_not_approved(self, make_event):
        gateway = _gateway()
        gateway.dashboard_state = None
        approve = gateway._interactive_approval("cron")
        # An ordinary request with no UI at all is approved (today's contract).
        assert await approve(_ordinary(), "") is True
        assert await approve(make_event(), "") is False


class TestHeartbeatNeverApprovesIt:
    @pytest.mark.parametrize("make_event", _PERSON_ONLY)
    @pytest.mark.asyncio
    async def test_a_safe_tool_title_does_not_carry_it(self, monkeypatch, make_event):
        import kiro_crew.slack.gateway as gw

        monkeypatch.setattr(gw, "sel", lambda: MagicMock())
        gateway = gw.GatewayOrchestrator.__new__(gw.GatewayOrchestrator)
        # The allowlist keys on the trusted ``server/tool`` identity, so stamp a
        # heartbeat-safe one on both requests: only the hold tells them apart.
        safe = {
            "title": "learn_list",
            "mcp_identity_trusted": True,
            "tool_name": "learn_list",
            "mcp_server_name": "kirocrew-core",
        }
        assert await gateway._heartbeat_approval(_ordinary(**safe)) is True
        event = make_event()
        for name, value in safe.items():
            setattr(event, name, value)
        assert await gateway._heartbeat_approval(event) is False


# ── channel.py: persistent agent channels (YOLO / channel trust) ──


def _agent():
    return SimpleNamespace(
        id="a1",
        role="dev",
        agent_name="dev",
        session_key="channel:test",
        _approval_future=None,
        _trusted_commands=set(),
        _trusted_bases=set(),
        _pending_approval_command="",
    )


def _channel(agent, *, trusted: bool):
    ch = SimpleNamespace(id="c1", trusted=trusted, members={})
    ch._broadcast = MagicMock()

    async def _post(*args, **kw):
        # The person's card: answer "rejected" so only a rung could approve.
        if kw.get("msg_type") == "approval" and agent._approval_future is not None:
            if not agent._approval_future.done():
                agent._approval_future.set_result("rejected")

    ch.post = AsyncMock(side_effect=_post)
    return ch


def _client(event):
    client = SimpleNamespace()

    async def _stream(message):
        yield event
        yield SimpleNamespace(kind=EVENT_COMPLETE)

    client.stream = _stream
    client.approve_tool = AsyncMock()
    client.reject_tool = AsyncMock()
    return client


def _approval_posts(ch):
    return [a for a, kw in ch.post.call_args_list if kw.get("msg_type") == "approval"]


class TestAgentChannelHolds:
    @pytest.fixture(autouse=True)
    def _floor(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(
            "kiro_crew.permission_floor.refusal_for", lambda event, **kw: None
        )
        _floor_monkeypatch.setattr("kiro_crew.sel.sel", lambda: MagicMock())

    @pytest.mark.parametrize("via", ["channel-trust", "yolo"])
    @pytest.mark.asyncio
    async def test_the_rung_approves_an_ordinary_request(self, via):
        from kiro_crew.channel import _stream_task

        agent = _agent()
        ch = _channel(agent, trusted=via == "channel-trust")
        client = _client(_ordinary())
        await _stream_task(agent, ch, client, "hi", is_yolo=(lambda: via == "yolo"))
        client.approve_tool.assert_awaited_once_with("rq1")
        assert not _approval_posts(ch)

    @pytest.mark.parametrize("make_event", _PERSON_ONLY)
    @pytest.mark.parametrize("via", ["channel-trust", "yolo"])
    @pytest.mark.asyncio
    async def test_the_kas_request_goes_to_the_card(self, via, make_event):
        from kiro_crew.channel import _stream_task

        agent = _agent()
        ch = _channel(agent, trusted=via == "channel-trust")
        client = _client(make_event())
        await _stream_task(agent, ch, client, "hi", is_yolo=(lambda: via == "yolo"))
        client.approve_tool.assert_not_awaited()
        assert _approval_posts(ch)


# ── messaging.workflow_markers: the marker rows a linked thread is sent ──

_TOKEN = "0123456789abcdef0123456789abcdef"


class TestWorkflowMarkerLines:
    @pytest.mark.parametrize(
        ("row", "line"),
        [
            (
                f"Start workflow `investigate-docs` in `/home/u/repo` (launch {_TOKEN})",
                "Starting workflow investigate-docs",
            ),
            (f"Reconnect workflow runs (reconnect {_TOKEN})", "Reconnecting workflow runs"),
            (
                f"[Workflow `investigate-kiro-docs-crew` completed] Kiro Agent's reply follows "
                f"(observe {_TOKEN})",
                "Workflow investigate-kiro-docs-crew completed",
            ),
            (
                f"[Workflow `w` failed] Kiro Agent's reply follows (observe {_TOKEN})",
                "Workflow w failed",
            ),
            (
                f"[Workflow `w` aborted] Kiro Agent's reply follows (observe {_TOKEN})",
                "Workflow w stopped",
            ),
            (
                f"[Workflow `w` update] Kiro Agent's reply follows (observe {_TOKEN})",
                "Update from workflow w",
            ),
            (
                f"[Workflow `w` waiting] Kiro Agent's reply follows (observe {_TOKEN})",
                "Workflow w is waiting for you",
            ),
            (
                f"[Workflow `w` error] Kiro Agent's reply follows (observe {_TOKEN})",
                "A step in workflow w failed",
            ),
            (
                f"[Workflow `w` something] Kiro Agent's reply follows (observe {_TOKEN})",
                "Workflow w ended",
            ),
        ],
    )
    def test_a_marker_row_becomes_one_human_line_without_its_token(self, row, line):
        from kiro_crew.messaging.workflow_markers import mirror_line, mirror_text

        assert mirror_line(row) == line
        assert mirror_text(row) == line
        assert _TOKEN not in mirror_text(row)
        assert "/home/u/repo" not in mirror_text(row)

    @pytest.mark.parametrize(
        "text",
        [
            "Start workflow `x` in `/p` (launch notahextoken)",
            "Reconnect workflow runs",
            "please start workflow `x` in `/p`",
            f"Start workflow `x` in `/p` (launch {_TOKEN}) and more",
            "",
        ],
    )
    def test_anything_else_is_left_alone(self, text):
        from kiro_crew.messaging.workflow_markers import mirror_line, mirror_text

        assert mirror_line(text) is None
        assert mirror_text(text) == text

    def test_the_rows_the_router_writes_are_recognized(self):
        """Pinned to the router's own builders, so a reworded row fails here."""
        from kiro_crew.kas_workflows import router
        from kiro_crew.messaging.workflow_markers import mirror_line

        assert router._LAUNCH_RE.match(
            f"Start workflow `n` in `/p` (launch {_TOKEN})"
        ) and mirror_line(f"Start workflow `n` in `/p` (launch {_TOKEN})")
        assert router._RECONNECT_RE.match(
            f"Reconnect workflow runs (reconnect {_TOKEN})"
        ) and mirror_line(f"Reconnect workflow runs (reconnect {_TOKEN})")
        row = f"[Workflow `n` completed] Kiro Agent's reply follows (observe {_TOKEN})"
        assert router._OBSERVE_RE.match(row) and mirror_line(row)


class TestLinkedThreadEchoesTheHumanLine:
    """The dashboard's echo of a user row into a linked thread (chat_runner)."""

    _ROW = f"[Workflow `investigate-kiro-docs-crew` completed] Kiro Agent's reply follows (observe {_TOKEN})"
    _LAUNCH = f"Start workflow `investigate-docs` in `/home/u/repo` (launch {_TOKEN})"

    @pytest.mark.parametrize(
        ("row", "line"),
        [
            (_ROW, "Workflow investigate-kiro-docs-crew completed"),
            (_LAUNCH, "Starting workflow investigate-docs"),
        ],
    )
    def test_the_slack_and_channel_echo_text_is_the_human_line(self, row, line):
        """Both echo sinks (Slack thread, non-Slack channel) go through this one helper."""
        from kiro_crew.dashboard.chat_runner import _prepare_mirror_msg

        out = _prepare_mirror_msg(row)
        assert out == line
        assert _TOKEN not in out and "/home/u/repo" not in out

    def test_an_ordinary_message_is_echoed_as_typed(self):
        from kiro_crew.dashboard.chat_runner import _prepare_mirror_msg

        assert _prepare_mirror_msg("start workflow `x` please") == "start workflow `x` please"

    def test_a_linked_channel_receives_the_human_line(self, monkeypatch):
        import kiro_crew.dashboard.chat_runner as cr

        transport = SimpleNamespace(send_message=AsyncMock())
        link = SimpleNamespace(channel_id="C1", thread_id="T1", channel_type="telegram")
        monkeypatch.setattr(cr, "mirror_is_paused", lambda state, key: False)
        monkeypatch.setattr(cr, "_resolve_mirror_target", lambda state, key: (link, transport))

        asyncio.run(cr._deliver_cross_surface_user_message(object(), "dashboard:chat-1", self._ROW))

        transport.send_message.assert_awaited_once()
        sent = transport.send_message.await_args.args[1]
        assert sent == "💬 Workflow investigate-kiro-docs-crew completed"
        assert _TOKEN not in sent
