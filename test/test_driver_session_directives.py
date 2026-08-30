"""TurnDriver session-directive consumption (#4540).

``TurnDriver`` never consumed ``EVENT_TOOL_RESULT``, so on every standalone
messaging transport (Telegram, Discord, standalone Slack, iMessage, Teams,
Webex, WeCom, Weixin) the session-directive marker returned by the stateless
session-bound tools (``monitor_start`` / ``monitor_update`` / ``autonudge_stop``
/ ``set_project`` …) was silently discarded: the model was told the effect was
requested and nothing ever happened.

These tests lock the three halves of the fix:

* **Driver consumption + forgery gate** — the directive-tool identity is
  recorded at ``EVENT_TOOL_CALL`` ONLY from the trusted ``_meta.kiro`` fields
  (core MCP server + canonical tool name), the marker is decoded from the
  matching ``EVENT_TOOL_RESULT``, applied exactly once (single-consume across
  result frames), and a forged marker under a shell tool, a third-party MCP
  server, or a non-directive tool is ignored. Without an injected consumer the
  driver's behavior is unchanged.
* **Channel applier boundary** — ``apply_session_directive`` with ``slot=None``
  (the channel-caller shape) applies the monitor trio on a nudge-able channel
  session and keeps refusing the ``_DASHBOARD_ONLY_DIRECTIVES``, including when
  the session key would pass ``has_dashboard_surface``.
* **Consumer wiring** — ``build_directive_consumer`` funnels into the shared
  applier with the dispatcher's live ``dashboard_state`` when present, and a
  fail-closed ``sessions``-backed stand-in when not (the Slack shape).
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew import session_directive
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    AcpEvent,
)
from kiro_crew.dashboard.session_directive_apply import apply_session_directive
from kiro_crew.messaging import TransportCapabilities, TurnDriver
from kiro_crew.messaging.dispatch import _ChannelDirectiveState, build_directive_consumer
from kiro_crew.messaging.renderer import Renderer

MONITOR_ARGS = {"message": "watch PR #1", "idle_secs": 300, "max_cycles": 5, "max_runtime_secs": 0}


def _directive(kind: str = "monitor_start", args: dict | None = None) -> str:
    return session_directive.encode(kind, dict(args or MONITOR_ARGS), "Monitor loop requested.")


class _RecordingRenderer(Renderer):
    def __init__(self):
        super().__init__(TransportCapabilities())
        self.events: list[tuple] = []

    async def on_text_chunk(self, text):
        self.events.append(("text_chunk", text))

    async def on_thinking(self, text):
        self.events.append(("thinking", text))

    async def on_tool_call(self, tool_call_id, title, tool_kind="", tool_purpose=""):
        self.events.append(("tool_call", tool_call_id, title))

    async def on_prompt_choice(self, options, request_id):
        self.events.append(("prompt_choice", options, request_id))

    async def on_compaction(self, pct):
        self.events.append(("compaction", pct))

    async def on_done(self, stop_reason=""):
        self.events.append(("done", stop_reason))

    async def on_steer_consumed(self, summary=""):
        self.events.append(("steer_consumed", summary))


class _ScriptedProvider:
    def __init__(self, events):
        self._events = events

    async def stream(self, message):
        for ev in self._events:
            yield ev

    async def approve_tool(self, request_id, *, always=False):
        pass

    async def reject_tool(self, request_id):
        pass


class _SpyConsumer:
    """Records every (kind, args) the driver hands to the directive consumer."""

    def __init__(self):
        self.applied: list[tuple[str, dict]] = []

    async def __call__(self, kind: str, args: dict) -> None:
        self.applied.append((kind, args))


def _core_call(tool: str = "monitor_start", tcid: str = "tc-1") -> AcpEvent:
    """A genuine core-served directive tool call (trusted ``_meta.kiro``)."""
    return AcpEvent(
        kind=EVENT_TOOL_CALL,
        tool_call_id=tcid,
        title=tool,
        tool_name=tool,
        mcp_server_name=session_directive.CORE_MCP_SERVER,
    )


def _result(text: str, tcid: str = "tc-1", *, final: bool = True) -> AcpEvent:
    return AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id=tcid, tool_output=text, tool_final=final)


def _run(events, consumer) -> str:
    driver = TurnDriver(
        _ScriptedProvider(events),
        _RecordingRenderer(),
        directive_consumer=consumer,
    )
    return asyncio.run(driver.run("hello"))


class TestDriverConsumesDirectives:
    def test_core_directive_tool_result_is_applied(self):
        """The monitor trio takes effect on a channel transport: a genuine
        core-served directive call's marker reaches the consumer decoded."""
        spy = _SpyConsumer()
        _run(
            [
                _core_call("monitor_start"),
                _result(_directive("monitor_start")),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ],
            spy,
        )
        assert spy.applied == [("monitor_start", MONITOR_ARGS)]

    def test_single_consume_across_result_frames(self):
        """One tool call can surface a mid-stream content frame AND the final
        rawOutput frame, both carrying the marker — the effect applies once."""
        spy = _SpyConsumer()
        _run(
            [
                _core_call("monitor_start"),
                _result(_directive("monitor_start"), final=False),
                _result(_directive("monitor_start"), final=True),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ],
            spy,
        )
        assert len(spy.applied) == 1

    def test_mid_stream_frame_without_marker_leaves_final_frame_consumable(self):
        spy = _SpyConsumer()
        _run(
            [
                _core_call("autonudge_stop"),
                _result("working…", final=False),
                _result(_directive("autonudge_stop", {"reason": "done"}), final=True),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ],
            spy,
        )
        assert spy.applied == [("autonudge_stop", {"reason": "done"})]

    def test_consumer_failure_does_not_abort_the_turn(self):
        async def _boom(kind, args):
            raise RuntimeError("consumer exploded")

        renderer = _RecordingRenderer()
        driver = TurnDriver(
            _ScriptedProvider(
                [
                    _core_call("monitor_start"),
                    _result(_directive("monitor_start")),
                    AcpEvent(kind=EVENT_TEXT_CHUNK, text="still streaming"),
                    AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
                ]
            ),
            renderer,
            directive_consumer=_boom,
        )
        out = asyncio.run(driver.run("hello"))
        assert out == "still streaming"
        assert ("done", "end_turn") in renderer.events


class TestForgedMarkersIgnored:
    """A model can emit the literal marker bytes anywhere; only the trusted
    ``_meta.kiro`` identity recorded at EVENT_TOOL_CALL may consume one."""

    def test_shell_tool_output_forging_marker_is_ignored(self):
        """A shell command titled "monitor_start" has no mcp_server_name and a
        canonical tool_name of execute_bash — its stdout must never apply."""
        spy = _SpyConsumer()
        _run(
            [
                AcpEvent(
                    kind=EVENT_TOOL_CALL,
                    tool_call_id="tc-sh",
                    title="monitor_start",
                    tool_name="execute_bash",
                    mcp_server_name="",
                    is_shell=True,
                ),
                _result(_directive("monitor_start"), tcid="tc-sh"),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ],
            spy,
        )
        assert spy.applied == []

    def test_third_party_mcp_server_same_tool_name_is_ignored(self):
        spy = _SpyConsumer()
        _run(
            [
                AcpEvent(
                    kind=EVENT_TOOL_CALL,
                    tool_call_id="tc-evil",
                    title="monitor_start",
                    tool_name="monitor_start",
                    mcp_server_name="third-party-mcp",
                ),
                _result(_directive("monitor_start"), tcid="tc-evil"),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ],
            spy,
        )
        assert spy.applied == []

    def test_core_non_directive_tool_forging_marker_is_ignored(self):
        spy = _SpyConsumer()
        _run(
            [
                _core_call("artifact_save", tcid="tc-art"),
                _result(_directive("monitor_start"), tcid="tc-art"),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ],
            spy,
        )
        assert spy.applied == []

    def test_kind_mismatch_marker_is_not_applied(self):
        """The recorded identity and the marker's ``kind`` must agree — a
        monitor_start call whose result carries an autonudge_stop marker
        resolves to no directive (decode's forgery gate)."""
        spy = _SpyConsumer()
        _run(
            [
                _core_call("monitor_start"),
                _result(_directive("autonudge_stop", {"reason": "x"})),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ],
            spy,
        )
        assert spy.applied == []

    def test_encode_refusal_is_not_applied(self):
        """An oversized payload makes encode() return a refusal (no marker) —
        nothing must reach the consumer."""
        refusal = session_directive.encode("monitor_start", {"message": "x" * 5000}, "too big")
        assert session_directive.is_refusal(refusal)
        spy = _SpyConsumer()
        _run(
            [
                _core_call("monitor_start"),
                _result(refusal),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ],
            spy,
        )
        assert spy.applied == []


class TestNoConsumerIsInert:
    def test_tool_result_stays_inert_without_consumer(self):
        """No injected consumer => byte-identical behavior to before: the
        directive marker is neither decoded nor rendered, and the turn's
        renderer output is unchanged."""
        renderer = _RecordingRenderer()
        driver = TurnDriver(
            _ScriptedProvider(
                [
                    _core_call("monitor_start"),
                    _result(_directive("monitor_start")),
                    AcpEvent(kind=EVENT_TEXT_CHUNK, text="hi"),
                    AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
                ]
            ),
            renderer,
        )
        out = asyncio.run(driver.run("hello"))
        assert out == "hi"
        assert [e[0] for e in renderer.events] == ["tool_call", "text_chunk", "done"]


class TestNativeSubAgentIsolation:
    """A NATIVE (in-session) sub-agent's tool calls surface as flat events on
    the parent stream with a genuine core-MCP identity; EVENT_SUBAGENT_ACTIVITY
    announces their tool_call_ids first. The driver must refuse those
    directives — a child session can never arm/mutate its parent."""

    def test_native_subagent_directive_is_refused(self):
        spy = _SpyConsumer()
        _run(
            [
                AcpEvent(
                    kind="subagent_activity",
                    sub_session_id="sub-1",
                    tool_call_id="tc-native",
                ),
                _core_call("monitor_start", tcid="tc-native"),
                _result(_directive("monitor_start"), tcid="tc-native"),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ],
            spy,
        )
        assert spy.applied == []

    def test_native_refusal_is_terminal_across_frames(self):
        """The refusal pops the mapping, so a second result frame for the same
        native call cannot sneak the effect through."""
        spy = _SpyConsumer()
        _run(
            [
                AcpEvent(
                    kind="subagent_activity",
                    sub_session_id="sub-1",
                    tool_call_id="tc-native",
                ),
                _core_call("monitor_start", tcid="tc-native"),
                _result(_directive("monitor_start"), tcid="tc-native", final=False),
                _result(_directive("monitor_start"), tcid="tc-native", final=True),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ],
            spy,
        )
        assert spy.applied == []

    def test_parent_directive_still_applies_alongside_native_activity(self):
        """Isolation is per tool_call_id: a native child's marker is refused
        while the parent's own directive call in the same turn still applies."""
        spy = _SpyConsumer()
        _run(
            [
                AcpEvent(
                    kind="subagent_activity",
                    sub_session_id="sub-1",
                    tool_call_id="tc-native",
                ),
                _core_call("monitor_start", tcid="tc-native"),
                _result(_directive("monitor_start"), tcid="tc-native"),
                _core_call("autonudge_stop", tcid="tc-parent"),
                _result(_directive("autonudge_stop", {"reason": "done"}), tcid="tc-parent"),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ],
            spy,
        )
        assert spy.applied == [("autonudge_stop", {"reason": "done"})]


# ── Channel applier boundary (apply_session_directive with slot=None) ─────────


class _FakeLoop:
    def __init__(self, loop_id="loop-1"):
        self.id = loop_id
        self.idle_secs = 300
        self.max_cycles = 5
        self.cycle_count = 0
        self.active = True
        self.stopped_reason = ""


class _ArmSvc:
    """AutoNudge service double recording arms/removals for channel sessions."""

    def __init__(self, loop=None):
        self._loop = loop
        self.added: list[dict] = []
        self.removed: list[str] = []

    def get_by_slot(self, key):
        return self._loop

    async def add(self, **kw):
        self.added.append(kw)
        return _FakeLoop("loop-armed")

    async def remove(self, loop_id):
        self.removed.append(loop_id)


class _ChannelSessions:
    """SessionManager double: knows one routable Slack session."""

    def __init__(self, known_key: str):
        self._known = known_key

    def get_channel(self, key):
        return "C123" if key == self._known else ""


@pytest.fixture()
def no_dashboard_tabs():
    """Pin the dashboard-surface registry empty for the test, then restore."""
    from kiro_crew import session_surface

    before = session_surface.dashboard_surfaced_keys()
    session_surface.set_dashboard_surfaced(())
    yield
    session_surface.set_dashboard_surfaced(before)


class TestChannelApplierBoundary:
    @pytest.mark.asyncio
    async def test_monitor_start_applies_on_slack_channel_session(
        self, monkeypatch, no_dashboard_tabs
    ):
        """The monitor trio WORKS from a channel turn: slot=None, a nudge-able
        slack: session key, and the REAL authorize_and_add_nudge chokepoint
        (Slack routability via state.sessions) arm the loop."""
        session_key = "slack:1755000000.123456"
        svc = _ArmSvc()
        monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: svc)
        state = _ChannelDirectiveState(sessions=_ChannelSessions(session_key))
        result = await apply_session_directive(
            state, None, session_key, "monitor_start", dict(MONITOR_ARGS)
        )
        assert "started on this session" in result
        assert len(svc.added) == 1
        assert svc.added[0]["slot_key"] == session_key
        assert svc.added[0]["idle_secs"] == 300

    @pytest.mark.asyncio
    async def test_autonudge_stop_applies_on_slack_channel_session(
        self, monkeypatch, no_dashboard_tabs
    ):
        session_key = "slack:1755000000.123456"
        svc = _ArmSvc(loop=_FakeLoop("loop-9"))
        monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: svc)
        state = _ChannelDirectiveState(sessions=_ChannelSessions(session_key))
        result = await apply_session_directive(
            state, None, session_key, "autonudge_stop", {"reason": "done"}
        )
        assert "stopped on this session" in result
        assert svc.removed == ["loop-9"]

    @pytest.mark.asyncio
    async def test_monitor_start_not_supported_on_non_nudgeable_channel(
        self, monkeypatch, no_dashboard_tabs
    ):
        """A telegram: session has no AutoNudge binding — the applier answers
        honestly instead of arming anything (and instead of silence)."""
        svc = _ArmSvc()
        monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: svc)
        state = _ChannelDirectiveState(sessions=_ChannelSessions("other"))
        result = await apply_session_directive(
            state, None, "telegram:kirocrew:direct:42", "monitor_start", dict(MONITOR_ARGS)
        )
        assert "not supported from this session type" in result
        assert svc.added == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["monitor_start", "monitor_update", "autonudge_stop"])
    async def test_not_supported_paths_audit_denied_never_success(
        self, kind, monkeypatch, no_dashboard_tabs
    ):
        """SEL truthfulness: an effect that was NOT applied must never audit
        ``success``. The not-supported refusal on a non-nudge-able channel
        session lands as ``denied`` for the whole monitor trio."""
        calls: list[dict] = []

        class _SelSpy:
            def log_tool_invocation(self, **kw):
                calls.append(kw)

        monkeypatch.setattr("kiro_crew.sel.sel", lambda: _SelSpy())
        monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: _ArmSvc())
        result = await apply_session_directive(
            _ChannelDirectiveState(sessions=_ChannelSessions("x")),
            None,
            "telegram:kirocrew:direct:42",
            kind,
            dict(MONITOR_ARGS) if kind != "autonudge_stop" else {},
        )
        assert "not supported from this session type" in result
        directive_calls = [c for c in calls if c.get("source") == "mcp-directive"]
        assert [c["outcome"] for c in directive_calls] == ["denied"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["suggest_followup", "ask_question"])
    async def test_dashboard_only_directives_refused_on_channel_transport(
        self, kind, no_dashboard_tabs
    ):
        """SECURITY INVARIANT (#4540): _DASHBOARD_ONLY_DIRECTIVES stay DENIED
        for non-dashboard sessions — the channel consumer must not widen the
        gate. set_project left this set (#3543): it is refused on the channel
        transport by the slot-less gate instead, pinned below."""
        state = _ChannelDirectiveState(sessions=_ChannelSessions("x"))
        result = await apply_session_directive(
            state, None, "slack:1755000000.123456", kind, {"project": "/tmp"}
        )
        assert result.startswith("Error:")
        assert "only works from a dashboard chat session" in result

    @pytest.mark.asyncio
    async def test_dashboard_only_refused_for_slotless_caller_even_with_open_tab(self):
        """A channel-born session CAN have an open dashboard tab
        (has_dashboard_surface True), but a slot-less channel turn still must
        not drive a slot-targeted effect — the effect targets the SLOT and this
        turn holds none. Guards the slot=None tightening."""
        from kiro_crew import session_surface

        session_key = "slack:1755000000.777"
        before = session_surface.dashboard_surfaced_keys()
        session_surface.set_dashboard_surfaced({session_key})
        try:
            result = await apply_session_directive(
                _ChannelDirectiveState(sessions=_ChannelSessions("x")),
                None,
                session_key,
                "set_project",
                {"project": "/tmp"},
            )
        finally:
            session_surface.set_dashboard_surfaced(before)
        assert result.startswith("Error:")
        assert "targets this turn's chat slot" in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "args", [{"project": "/tmp"}, {"clear": True}], ids=["set-path", "clear"]
    )
    async def test_slotless_set_project_refusal_audits_denied(
        self, args, monkeypatch, no_dashboard_tabs
    ):
        """SEL truthfulness for the slot-less set_project refusal: it is a
        permission DECISION, so it must audit ``denied`` — never ``error``
        (the crash shape a missing slot-None guard would produce) and never
        ``success``. Nothing may be mutated on the way out. Pinned for BOTH
        arg shapes: ``_set_project``'s clear path writes slot state before any
        validation, so the guard is all that keeps a slot-less clear from
        mutating on the way to the crash."""
        calls: list[dict] = []

        class _SelSpy:
            def log_tool_invocation(self, **kw):
                calls.append(kw)

        monkeypatch.setattr("kiro_crew.sel.sel", lambda: _SelSpy())
        result = await apply_session_directive(
            _ChannelDirectiveState(sessions=_ChannelSessions("x")),
            None,
            "slack:1755000000.123456",
            "set_project",
            dict(args),
        )
        assert result.startswith("Error:")
        assert "targets this turn's chat slot" in result
        directive_calls = [c for c in calls if c.get("source") == "mcp-directive"]
        assert [c["outcome"] for c in directive_calls] == ["denied"]


# ── build_directive_consumer wiring ──────────────────────────────────────────


class TestBuildDirectiveConsumer:
    @pytest.mark.asyncio
    async def test_prefers_dispatcher_dashboard_state(self, monkeypatch):
        """A dispatcher with gateway state attached (register_channel_transport)
        hands the REAL state to the applier — re-read per directive, so a
        consumer built before the attachment still sees it."""
        seen: list[tuple] = []

        async def _spy(state, slot, session_key, kind, args):
            seen.append((state, slot, session_key, kind, args))
            return "ok"

        monkeypatch.setattr(
            "kiro_crew.dashboard.session_directive_apply.apply_session_directive", _spy
        )

        class _Dispatcher:
            pass

        dispatcher = _Dispatcher()
        consume = build_directive_consumer(
            session_key="discord:kirocrew:direct:42",
            sessions=object(),
            dispatcher=dispatcher,
        )
        dashboard_state = object()  # attached AFTER the consumer was built
        dispatcher.dashboard_state = dashboard_state
        await consume("monitor_start", dict(MONITOR_ARGS))
        assert len(seen) == 1
        state, slot, session_key, kind, args = seen[0]
        assert state is dashboard_state
        assert slot is None
        assert session_key == "discord:kirocrew:direct:42"
        assert (kind, args) == ("monitor_start", MONITOR_ARGS)

    @pytest.mark.asyncio
    async def test_falls_back_to_sessions_stand_in(self, monkeypatch):
        """No dispatcher object (the Slack shape): the applier gets the
        fail-closed sessions-backed stand-in, never None."""
        seen: list = []

        async def _spy(state, slot, session_key, kind, args):
            seen.append(state)
            return "ok"

        monkeypatch.setattr(
            "kiro_crew.dashboard.session_directive_apply.apply_session_directive", _spy
        )
        sessions = object()
        consume = build_directive_consumer(session_key="slack:1755000000.1", sessions=sessions)
        await consume("autonudge_stop", {})
        assert len(seen) == 1
        state = seen[0]
        assert isinstance(state, _ChannelDirectiveState)
        assert state.sessions is sessions
        assert state._slots == {} and state.channel_transports == {}


class TestSilentDropIsDiagnosable:
    """The identity gate refuses correctly but used to refuse SILENTLY, so an
    ACP backend that emits no ``_meta.kiro`` was indistinguishable from nothing
    happening. The refusal must stay a refusal AND leave a log line naming the
    identity it saw. Diagnostic only: no test here may show an effect applying.
    """

    def test_missing_backend_identity_logs_what_it_saw(self, caplog):
        """The KAS-shaped case: a genuine directive tool whose frame carries no
        ``_meta.kiro`` at all. Nothing applies (unchanged), and the operator can
        now see WHY instead of an empty log."""
        spy = _SpyConsumer()
        with caplog.at_level("WARNING"):
            _run(
                [
                    AcpEvent(
                        kind=EVENT_TOOL_CALL,
                        tool_call_id="tc-kas",
                        title="monitor_start",
                        tool_name="",
                        mcp_server_name="",
                    ),
                    _result(_directive("monitor_start"), tcid="tc-kas"),
                    AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
                ],
                spy,
            )
        assert spy.applied == []
        assert "session-directive NOT APPLIED" in caplog.text
        assert session_directive.CORE_MCP_SERVER in caplog.text

    def test_forged_shell_marker_also_logs_and_still_does_not_apply(self, caplog):
        spy = _SpyConsumer()
        with caplog.at_level("WARNING"):
            _run(
                [
                    AcpEvent(
                        kind=EVENT_TOOL_CALL,
                        tool_call_id="tc-sh",
                        title="monitor_start",
                        tool_name="execute_bash",
                        mcp_server_name="",
                        is_shell=True,
                    ),
                    _result(_directive("monitor_start"), tcid="tc-sh"),
                    AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
                ],
                spy,
            )
        assert spy.applied == []
        assert "session-directive NOT APPLIED" in caplog.text
        assert "execute_bash" in caplog.text

    def test_ordinary_tool_result_stays_silent(self, caplog):
        """No marker, no log — the diagnostic must not fire on every tool call."""
        spy = _SpyConsumer()
        with caplog.at_level("WARNING"):
            _run(
                [
                    AcpEvent(
                        kind=EVENT_TOOL_CALL,
                        tool_call_id="tc-plain",
                        title="ls",
                        tool_name="execute_bash",
                        mcp_server_name="",
                        is_shell=True,
                    ),
                    _result("a.txt  b.txt", tcid="tc-plain"),
                    AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
                ],
                spy,
            )
        assert spy.applied == []
        assert "session-directive NOT APPLIED" not in caplog.text

    def test_an_applied_directive_does_not_log_not_applied(self, caplog):
        """SINGLE-CONSUME pops the pending entry, so a SECOND result frame for a
        directive that DID apply reaches the same branch. It must not be
        reported as not-applied — that false alarm is what the consumed-id set
        exists to prevent."""
        spy = _SpyConsumer()
        payload = _directive("monitor_start")
        with caplog.at_level("WARNING"):
            _run(
                [
                    _core_call(tcid="tc-ok"),
                    _result(payload, tcid="tc-ok"),
                    _result(payload, tcid="tc-ok"),
                    AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
                ],
                spy,
            )
        assert [k for k, _ in spy.applied] == ["monitor_start"]
        assert "session-directive NOT APPLIED" not in caplog.text
