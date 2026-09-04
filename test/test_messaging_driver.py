"""Tests for the channel-neutral TurnDriver (v1b-2).

Drives the TurnDriver with a scripted provider into a recording renderer and
asserts the provider event stream is translated into the correct abstract
output-event sequence, redaction is applied, and the approval ladder calls
approve_tool/reject_tool correctly.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.acp.types import (
    EVENT_COMPACTION_STATUS,
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_STEER_CONSUMED,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    AcpEvent,
    TurnUsage,
)
from kiro_crew.messaging import (
    APPROVAL_AUTO,
    APPROVAL_INTERACTIVE,
    TransportCapabilities,
    TurnDriver,
)
from kiro_crew.messaging.renderer import Renderer
from kiro_crew.monitoring.completion import MonitorCompletionHook
from kiro_crew.monitoring.models import MonitorActionCompletion, MonitorActionDisposition


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

    async def on_prompt_choice(
        self, options, request_id, tool_title="", tool_purpose="", tool_input=""
    ):
        self.events.append(("prompt_choice", options, request_id, tool_title, tool_purpose))

    async def on_compaction(self, pct):
        self.events.append(("compaction", pct))

    async def on_done(self, stop_reason=""):
        self.events.append(("done", stop_reason))

    async def on_steer_consumed(self, summary=""):
        self.events.append(("steer_consumed", summary))


class _ScriptedProvider:
    def __init__(self, events):
        self._events = events
        self.approved: list = []
        self.rejected: list = []

    async def stream(self, message):
        for ev in self._events:
            yield ev

    async def approve_tool(self, request_id, *, always=False):
        self.approved.append(request_id)

    async def reject_tool(self, request_id):
        self.rejected.append(request_id)


def _run(provider, renderer, **kw):
    driver = TurnDriver(provider, renderer, **kw)
    return asyncio.run(driver.run("hello"))


class TestTurnDriverTranslation:
    def test_text_and_complete(self):
        r = _RecordingRenderer()
        p = _ScriptedProvider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Hello "),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="world"),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        out = _run(p, r)
        assert out == "Hello world"
        assert [e[0] for e in r.events] == ["text_chunk", "text_chunk", "done"]

    def test_safe_complete_reports_monitor_action_once(self):
        r = _RecordingRenderer()
        p = _ScriptedProvider(
            [
                AcpEvent(
                    kind=EVENT_COMPLETE,
                    stop_reason="max_tokens",
                    usage=TurnUsage(input_tokens=20, output_tokens=5),
                )
            ]
        )
        completions: list[MonitorActionCompletion] = []

        async def _capture(completion: MonitorActionCompletion) -> None:
            completions.append(completion)

        hook = MonitorCompletionHook("monitor1", "failure-a", _capture)
        _run(p, r, monitor_completion=hook)

        assert len(completions) == 1
        assert completions[0].disposition is MonitorActionDisposition.FAILURE
        assert completions[0].input_tokens == 20
        assert completions[0].output_tokens == 5

    @pytest.mark.parametrize(
        "stop_reason",
        [
            "",
            "timeout",
            "stale_recover",
            "error: cancel unacked",
            "error: tool stall",
            "error: compaction failed",
        ],
    )
    def test_synthetic_complete_does_not_report_monitor_action(self, stop_reason):
        r = _RecordingRenderer()
        p = _ScriptedProvider(
            [
                AcpEvent(
                    kind=EVENT_COMPLETE,
                    stop_reason=stop_reason,
                    usage=TurnUsage(input_tokens=20, output_tokens=5),
                )
            ]
        )
        completions: list[MonitorActionCompletion] = []

        async def _capture(completion: MonitorActionCompletion) -> None:
            completions.append(completion)

        hook = MonitorCompletionHook("monitor1", "failure-a", _capture)
        _run(p, r, monitor_completion=hook)

        assert completions == []

    def test_real_end_turn_reports_monitor_success(self):
        r = _RecordingRenderer()
        p = _ScriptedProvider(
            [
                AcpEvent(
                    kind=EVENT_COMPLETE,
                    stop_reason="end_turn",
                    usage=TurnUsage(input_tokens=20, output_tokens=5),
                )
            ]
        )
        completions: list[MonitorActionCompletion] = []

        async def _capture(completion: MonitorActionCompletion) -> None:
            completions.append(completion)

        _run(p, r, monitor_completion=MonitorCompletionHook("monitor1", "failure-a", _capture))

        assert len(completions) == 1
        assert completions[0].disposition is MonitorActionDisposition.SUCCESS

    def test_synthesized_end_turn_does_not_report_monitor_action(self):
        r = _RecordingRenderer()
        p = _ScriptedProvider(
            [
                AcpEvent(
                    kind=EVENT_COMPLETE,
                    stop_reason="end_turn",
                    synthetic_completion=True,
                    usage=TurnUsage(input_tokens=20, output_tokens=5),
                )
            ]
        )
        completions: list[MonitorActionCompletion] = []

        async def _capture(completion: MonitorActionCompletion) -> None:
            completions.append(completion)

        _run(p, r, monitor_completion=MonitorCompletionHook("monitor1", "failure-a", _capture))

        assert completions == []

    def test_monitor_authorization_is_rechecked_before_provider_stream(self):
        r = _RecordingRenderer()
        p = _ScriptedProvider([AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")])
        completions: list[MonitorActionCompletion] = []
        authorizations: list[tuple[str, str]] = []

        async def _capture(completion: MonitorActionCompletion) -> None:
            completions.append(completion)

        async def _authorize(monitor_id: str, fingerprint: str) -> bool:
            authorizations.append((monitor_id, fingerprint))
            return False

        hook = MonitorCompletionHook(
            "monitor1",
            "failure-a",
            _capture,
            authorization_callback=_authorize,
        )
        out = _run(p, r, monitor_completion=hook)

        assert out == ""
        assert authorizations == [("monitor1", "failure-a")]
        assert not hook.accepted
        assert completions == []
        assert r.events == []

    def test_monitor_closing_gate_runs_before_acceptance_and_provider_stream(self):
        r = _RecordingRenderer()
        order: list[str] = []

        class _OrderedProvider(_ScriptedProvider):
            async def stream(self, message):
                order.append("stream")
                for event in self._events:
                    yield event

        p = _OrderedProvider([AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")])

        async def _capture(_completion: MonitorActionCompletion) -> None:
            return None

        async def _authorize(_monitor_id: str, _fingerprint: str) -> bool:
            order.append("authorize")
            return True

        hook = MonitorCompletionHook(
            "monitor1",
            "failure-a",
            _capture,
            authorization_callback=_authorize,
        )

        def _guard() -> None:
            order.append("guard")
            assert not hook.accepted

        _run(p, r, monitor_completion=hook, closing_gate=_guard)

        assert order == ["authorize", "guard", "stream"]
        assert hook.accepted

    def test_monitor_closing_gate_refusal_never_accepts_or_streams(self):
        r = _RecordingRenderer()
        p = _ScriptedProvider([AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")])

        async def _capture(_completion: MonitorActionCompletion) -> None:
            return None

        hook = MonitorCompletionHook("monitor1", "failure-a", _capture)

        def _guard() -> None:
            raise RuntimeError("gateway closing")

        with pytest.raises(RuntimeError, match="gateway closing"):
            _run(p, r, monitor_completion=hook, closing_gate=_guard)

        assert not hook.accepted
        assert r.events == []

    def test_safe_complete_reports_monitor_action_before_renderer_finalization(self):
        class _CancellingDoneRenderer(_RecordingRenderer):
            async def on_done(self, stop_reason=""):
                await super().on_done(stop_reason)
                raise asyncio.CancelledError

        r = _CancellingDoneRenderer()
        p = _ScriptedProvider(
            [
                AcpEvent(
                    kind=EVENT_COMPLETE,
                    stop_reason="max_tokens",
                    usage=TurnUsage(input_tokens=20, output_tokens=5),
                )
            ]
        )
        completions: list[MonitorActionCompletion] = []

        async def _capture(completion: MonitorActionCompletion) -> None:
            completions.append(completion)

        hook = MonitorCompletionHook("monitor1", "failure-a", _capture)
        with pytest.raises(asyncio.CancelledError):
            _run(p, r, monitor_completion=hook)

        assert len(completions) == 1
        assert completions[0].input_tokens == 20
        assert completions[0].output_tokens == 5

    def test_safe_complete_reports_monitor_action_before_buffered_rendering(self):
        class _FailingTextRenderer(_RecordingRenderer):
            async def on_text_chunk(self, text):
                raise RuntimeError("transport unavailable")

        r = _FailingTextRenderer()
        p = _ScriptedProvider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="✅ Conversation comp"),
                AcpEvent(
                    kind=EVENT_COMPLETE,
                    stop_reason="max_tokens",
                    usage=TurnUsage(input_tokens=20, output_tokens=5),
                ),
            ]
        )
        completions: list[MonitorActionCompletion] = []

        async def _capture(completion: MonitorActionCompletion) -> None:
            completions.append(completion)

        hook = MonitorCompletionHook("monitor1", "failure-a", _capture)
        with pytest.raises(RuntimeError, match="transport unavailable"):
            _run(p, r, monitor_completion=hook)

        assert len(completions) == 1
        assert completions[0].input_tokens == 20
        assert completions[0].output_tokens == 5

    def test_tool_calls_emit_uniform_tool_call(self):
        r = _RecordingRenderer()
        p = _ScriptedProvider(
            [
                AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="t1", title="grep", tool_final=False),
                AcpEvent(
                    kind=EVENT_TOOL_CALL, tool_call_id="t1", tool_output="ok", tool_final=True
                ),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        _run(p, r)
        kinds = [e[0] for e in r.events]
        # Every EVENT_TOOL_CALL maps to one uniform tool_call (no start/result split).
        assert kinds == ["tool_call", "tool_call", "done"]

    def test_compaction(self):
        r = _RecordingRenderer()
        p = _ScriptedProvider(
            [
                AcpEvent(kind=EVENT_COMPACTION_STATUS, context_usage_pct=82.0),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        _run(p, r)
        assert ("compaction", 82.0) in r.events

    def test_steering_marker_is_structured_not_delivered(self):
        r = _RecordingRenderer()
        p = _ScriptedProvider(
            [
                AcpEvent(
                    kind=EVENT_TEXT_CHUNK,
                    text="before [STEERING steer-7e6a4a0d94314d2db: obey latest] after",
                ),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        out = _run(p, r)
        emitted = "".join(e[1] for e in r.events if e[0] == "text_chunk")
        assert "STEERING" not in out and "steer-" not in emitted
        assert "obey latest" not in emitted
        assert out == emitted
        assert ("steer_consumed", "obey latest") in r.events

    def test_steering_summary_is_redacted_before_structured_event(self):
        r = _RecordingRenderer()
        # Split literal on purpose: Semgrep's detected-aws-access-key-id-value
        # rule matches an AKIA-shaped STRING LITERAL and cannot tell a synthetic
        # test fixture from a real leaked credential, so a one-piece literal
        # fails the SAST gate. Concatenating keeps the runtime value identical --
        # the test still proves an AWS-key-shaped secret is redacted -- while the
        # scanner sees no hardcoded key. Do not "simplify" this back to one
        # literal; it will break CI, not the test.
        secret = "AKIA" + "1234567890ABCDEF"
        p = _ScriptedProvider(
            [
                AcpEvent(
                    kind=EVENT_TEXT_CHUNK,
                    text=f"before [STEERING steer-7e6a4a0d94314d2db: use {secret}] after",
                ),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        _run(p, r)
        summaries = [e[1] for e in r.events if e[0] == "steer_consumed"]
        assert len(summaries) == 1
        assert secret not in summaries[0]
        assert "[REDACTED" in summaries[0]

    def test_steering_marker_split_across_chunks_never_leaks(self):
        r = _RecordingRenderer()
        p = _ScriptedProvider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="before [STEERING steer-7e6a4a0d"),
                AcpEvent(kind=EVENT_STEER_CONSUMED, text="obey latest"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="94314d2db: obey latest] after"),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        out = _run(p, r)
        emitted = "".join(e[1] for e in r.events if e[0] == "text_chunk")
        assert "7e6a4a0d" not in emitted and "94314d2db" not in emitted
        assert "STEERING" not in emitted and "obey latest" not in emitted
        assert "before" in out and "after" in out
        assert [e[0] for e in r.events].count("steer_consumed") == 1

    def test_options_block_is_preserved_by_shared_filter(self):
        r = _RecordingRenderer()
        text = "Choose one.\n\n[OPTIONS: Continue | Stop]"
        p = _ScriptedProvider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text=text),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        out = _run(p, r)
        assert out == text
        assert "[OPTIONS: Continue | Stop]" in out

    def test_compaction_summary_body_becomes_terse_notice(self):
        r = _RecordingRenderer()
        p = _ScriptedProvider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="✅ Conversation comp"),
                AcpEvent(
                    kind=EVENT_TEXT_CHUNK,
                    text="acted: ## OBJECTIVE\ninternal operating instructions",
                ),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        out = _run(p, r)
        emitted = "".join(e[1] for e in r.events if e[0] == "text_chunk")
        assert out == emitted == "✅ Context compacted."
        assert "OBJECTIVE" not in emitted
        assert "operating instructions" not in emitted


class TestApprovalLadder:
    def _perm_script(self):
        return [
            AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id="rq1", options=[{"id": "approve"}]),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]

    def test_auto_approves(self):
        r = _RecordingRenderer()
        p = _ScriptedProvider(self._perm_script())
        _run(p, r, approval_mode=APPROVAL_AUTO)
        assert p.approved == ["rq1"]
        assert p.rejected == []
        # In auto mode, prompt_choice is NOT dispatched (no UI shown)
        assert ("prompt_choice", [{"id": "approve"}], "rq1") not in r.events

    def test_interactive_denies_by_default(self):
        r = _RecordingRenderer()
        p = _ScriptedProvider(self._perm_script())
        _run(p, r, approval_mode=APPROVAL_INTERACTIVE)
        assert p.rejected == ["rq1"]
        assert p.approved == []

    def test_interactive_decider_approves(self):
        r = _RecordingRenderer()
        p = _ScriptedProvider(self._perm_script())

        async def decider(event):
            return True

        _run(p, r, approval_mode=APPROVAL_INTERACTIVE, decider=decider)
        assert p.approved == ["rq1"]


class TestAutoApproveTool:
    """The injected auto_approve_tool predicate (e.g. auto_approve_subagent_spawn
    for spawn_run) takes precedence over the interactive ladder.

    The predicate receives the PERMISSION EVENT (not the title): the title is
    model-authored, so the production predicate keys on canonical identity
    (``tool_name``/``is_shell``). Both directions are pinned below through the
    real ``build_auto_approve`` builder; flipping any consumer back to a
    title-only check must fail the forged-shell direction.
    """

    def _perm_script(self, title, **event_fields):
        return [
            AcpEvent(
                kind=EVENT_PERMISSION_REQUEST,
                request_id="rq1",
                title=title,
                options=[{"id": "approve"}],
                **event_fields,
            ),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]

    @staticmethod
    def _spawn_hook_builder(enabled=True):
        """A ctx_builder double with only the spawn hook flag set."""
        from types import SimpleNamespace

        return SimpleNamespace(hooks=SimpleNamespace(auto_approve_subagent_spawn=enabled))

    def test_predicate_auto_approves_matching_tool(self):
        r = _RecordingRenderer()
        p = _ScriptedProvider(self._perm_script("spawn_run"))

        async def decider(event):  # would normally be awaited in interactive
            raise AssertionError("decider must not be consulted for auto-approved tool")

        # Even in interactive mode with a live decider, a predicate match
        # approves immediately, without a decider wait or prompt_choice.
        _run(
            p,
            r,
            approval_mode=APPROVAL_INTERACTIVE,
            decider=decider,
            auto_approve_tool=lambda event: (getattr(event, "title", "") or "") == "spawn_run",
        )
        assert p.approved == ["rq1"]
        assert p.rejected == []
        assert not any(e[0] == "prompt_choice" for e in r.events)

    def test_predicate_non_match_falls_through_to_ladder(self):
        r = _RecordingRenderer()
        p = _ScriptedProvider(self._perm_script("grep"))
        # Predicate does not match -> interactive deny-by-default (no decider).
        _run(
            p,
            r,
            approval_mode=APPROVAL_INTERACTIVE,
            auto_approve_tool=lambda event: (getattr(event, "title", "") or "") == "spawn_run",
        )
        assert p.rejected == ["rq1"]
        assert p.approved == []

    def test_genuine_spawn_run_mcp_event_still_auto_approves(self):
        # Direction 1: a genuine spawn_run MCP call (canonical identity from
        # ``_meta.kiro``, provenance-flagged, served by the crew's own MCP
        # server) keeps unattended fan-out unblocked.
        from kiro_crew.messaging.dispatch import build_auto_approve

        r = _RecordingRenderer()
        p = _ScriptedProvider(
            self._perm_script(
                "spawn_run",
                tool_name="spawn_run",
                mcp_server_name="kirocrew-core",
                mcp_identity_trusted=True,
            )
        )
        _run(
            p,
            r,
            approval_mode=APPROVAL_INTERACTIVE,
            auto_approve_tool=build_auto_approve(self._spawn_hook_builder()),
        )
        assert p.approved == ["rq1"]
        assert p.rejected == []

    def test_shell_event_with_forged_spawn_title_is_not_auto_approved(self):
        # Direction 2 (the issue's attack): a SHELL event whose model-authored
        # title says spawn_run must fall to the ladder, not ride the rung.
        from kiro_crew.messaging.dispatch import build_auto_approve

        r = _RecordingRenderer()
        p = _ScriptedProvider(self._perm_script("spawn_run", is_shell=True, shell_classified=True))
        _run(
            p,
            r,
            approval_mode=APPROVAL_INTERACTIVE,
            auto_approve_tool=build_auto_approve(self._spawn_hook_builder()),
        )
        assert p.approved == []
        assert p.rejected == ["rq1"]

    def test_canonical_name_mismatch_with_forged_title_is_not_auto_approved(self):
        # A non-shell tool whose canonical _meta.kiro name is NOT spawn_run
        # cannot borrow the rung by re-titling itself.
        from kiro_crew.messaging.dispatch import build_auto_approve

        r = _RecordingRenderer()
        p = _ScriptedProvider(
            self._perm_script(
                "spawn_run",
                tool_name="artifact_delete",
                mcp_server_name="kirocrew-core",
                mcp_identity_trusted=True,
            )
        )
        _run(
            p,
            r,
            approval_mode=APPROVAL_INTERACTIVE,
            auto_approve_tool=build_auto_approve(self._spawn_hook_builder()),
        )
        assert p.approved == []
        assert p.rejected == ["rq1"]

    def test_session_trust_auto_approves_without_buttons(self):
        r = _RecordingRenderer()
        p = _ScriptedProvider(self._perm_script("grep"))
        # Per-session trust predicate True -> approve immediately, no prompt.
        _run(
            p,
            r,
            approval_mode=APPROVAL_INTERACTIVE,
            auto_approve_session=lambda: True,
        )
        assert p.approved == ["rq1"]
        assert p.rejected == []
        assert not any(e[0] == "prompt_choice" for e in r.events)

    def test_session_trust_false_falls_through_to_ladder(self):
        r = _RecordingRenderer()
        p = _ScriptedProvider(self._perm_script("grep"))
        # Not trusted + no decider -> interactive deny-by-default.
        _run(
            p,
            r,
            approval_mode=APPROVAL_INTERACTIVE,
            auto_approve_session=lambda: False,
        )
        assert p.rejected == ["rq1"]
        assert p.approved == []


class TestRedaction:
    def test_credentials_redacted_in_text(self):
        r = _RecordingRenderer()
        secret = "aws_secret_access_key=AKIAIOSFODNN7EXAMPLE1234567890abcdEXAMPLEKEY"
        p = _ScriptedProvider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text=secret),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        out = _run(p, r)
        # The raw secret value must not survive into the emitted/accumulated text.
        assert "AKIAIOSFODNN7EXAMPLE1234567890abcdEXAMPLEKEY" not in out


class TestStreamCredentialRedaction:
    """A credential split across two EVENT_TEXT_CHUNKs must be redacted — the
    driver routes the stream through StreamRedactor (feed per chunk, flush on
    COMPLETE) so the concatenation never reaches the channel in the clear."""

    def test_credential_split_across_chunks_is_redacted(self):
        r = _RecordingRenderer()
        # AKIA + 16 chars = a 20-char access-key-id, split down the middle.
        p = _ScriptedProvider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="prefix AKIA1234567890"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="ABCDEX suffix"),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        out = _run(p, r)
        # The full key spanned two chunks; neither the whole nor the leading
        # half may survive, and the redaction marker must be present.
        assert "AKIA1234567890ABCDEX" not in out
        assert "AKIA1234567890" not in out
        assert "[REDACTED: credential]" in out
        # What the renderer actually emitted (what reaches Slack) is clean too,
        # and equals the accumulated return (single redaction pipeline).
        emitted = "".join(e[1] for e in r.events if e[0] == "text_chunk")
        assert "AKIA" not in emitted
        assert emitted == out

    def test_credential_straddling_steering_frame_never_reassembles(self):
        r = _RecordingRenderer()
        # Split literal on purpose so Semgrep does not mistake the synthetic
        # fixture for a hardcoded AWS access key.
        secret = "AKIA" + "1234567890ABCDEF"
        split_at = len("AKIA1234567890")
        p = _ScriptedProvider(
            [
                AcpEvent(
                    kind=EVENT_TEXT_CHUNK,
                    text=(
                        f"prefix {secret[:split_at]}"
                        "[STEERING steer-7e6a4a0d94314d2db: latest]"
                        f"{secret[split_at:]} suffix"
                    ),
                ),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        out = _run(p, r)
        # Teams and WeCom join every text chunk before sending. Per-chunk
        # assertions miss this regression, so enforce the invariant on exactly
        # the concatenated text those renderers expose.
        joined = "".join(e[1] for e in r.events if e[0] == "text_chunk")
        assert secret not in joined
        assert "STEERING" not in joined
        assert joined == out
        assert ("steer_consumed", "latest") in r.events

    def test_flush_tail_emitted_before_done(self):
        # A credential-class run withheld at the last chunk is flushed (redacted)
        # as a final text_chunk BEFORE the done event, never dropped.
        r = _RecordingRenderer()
        p = _ScriptedProvider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="tail AKIA1234567890ABCDEX"),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        out = _run(p, r)
        kinds = [e[0] for e in r.events]
        assert kinds[-1] == "done"
        assert "text_chunk" in kinds  # flushed tail emitted before done
        assert "AKIA1234567890ABCDEX" not in out
        assert "[REDACTED: credential]" in out


class TestToolGateEnforcement:
    """The PreToolUse security gate (sensitive-path keystone + governance
    ceiling + deny-list) must hard-DENY BEFORE the auto/trust/YOLO ladder, so a
    governance/sensitive-path deny can never be overridden. Mirrors native
    handle_message's hooks.on_tool_call gate."""

    def _perm(self):
        return [
            AcpEvent(
                kind=EVENT_PERMISSION_REQUEST,
                request_id="rq1",
                title="fs_write",
                options=[{"id": "approve"}],
            ),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]

    def test_deny_overrides_auto_approve_mode(self):
        # APPROVAL_AUTO would auto-approve, but a gate DENY must reject first.
        r = _RecordingRenderer()
        p = _ScriptedProvider(self._perm())
        _run(p, r, approval_mode=APPROVAL_AUTO, tool_gate=lambda ev: "deny")
        assert p.rejected == ["rq1"]
        assert p.approved == []

    def test_deny_overrides_session_trust(self):
        # Per-session Trust (or YOLO) must NOT be able to override a hard deny.
        r = _RecordingRenderer()
        p = _ScriptedProvider(self._perm())
        _run(
            p,
            r,
            approval_mode=APPROVAL_INTERACTIVE,
            auto_approve_session=lambda: True,
            tool_gate=lambda ev: "deny",
        )
        assert p.rejected == ["rq1"]
        assert p.approved == []

    def test_deny_overrides_spawn_auto_approve(self):
        # The spawn auto-approve predicate must not override a gate deny either.
        r = _RecordingRenderer()
        p = _ScriptedProvider(self._perm())
        _run(
            p,
            r,
            approval_mode=APPROVAL_INTERACTIVE,
            auto_approve_tool=lambda event: True,
            tool_gate=lambda ev: "deny",
        )
        assert p.rejected == ["rq1"]
        assert p.approved == []

    def test_gate_auto_approve_approves(self):
        # A hook auto-approve (e.g. read-only) approves without buttons.
        r = _RecordingRenderer()
        p = _ScriptedProvider(self._perm())
        _run(p, r, approval_mode=APPROVAL_INTERACTIVE, tool_gate=lambda ev: "auto_approve")
        assert p.approved == ["rq1"]
        assert p.rejected == []

    def test_gate_passthrough_falls_to_ladder(self):
        # Passthrough ("") defers to the approval ladder (auto → approve).
        r = _RecordingRenderer()
        p = _ScriptedProvider(self._perm())
        _run(p, r, approval_mode=APPROVAL_AUTO, tool_gate=lambda ev: "")
        assert p.approved == ["rq1"]
        assert p.rejected == []

    def test_no_gate_preserves_prior_behavior(self):
        # Without a gate, the ladder still governs (auto → approve).
        r = _RecordingRenderer()
        p = _ScriptedProvider(self._perm())
        _run(p, r, approval_mode=APPROVAL_AUTO)
        assert p.approved == ["rq1"]


class TestDenyAllTools:
    """``deny_all_tools`` is for a turn driven by someone the channel does not
    trust as its operator, which the approval ladder cannot express on its own:
    the PreToolUse hook's ``auto_approve`` verdict and the Trust/YOLO predicates
    both approve and short-circuit BEFORE the ladder is consulted. So this has to
    beat every one of them, and each is asserted separately rather than trusting
    one representative case.
    """

    def _perm_script(self, title):
        return [
            AcpEvent(
                kind=EVENT_PERMISSION_REQUEST,
                request_id="rq1",
                title=title,
                tool_kind="read",
                options=[{"id": "approve"}],
            ),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]

    def test_it_beats_the_hook_auto_approve_verdict(self):
        r = _RecordingRenderer()
        p = _ScriptedProvider(self._perm_script("grep"))
        _run(
            p,
            r,
            approval_mode=APPROVAL_AUTO,
            tool_gate=lambda ev: "auto_approve",
            deny_all_tools=True,
        )
        assert p.rejected == ["rq1"] and p.approved == []

    def test_it_beats_the_auto_approve_predicate(self):
        r = _RecordingRenderer()
        p = _ScriptedProvider(self._perm_script("spawn_run"))
        _run(
            p,
            r,
            approval_mode=APPROVAL_INTERACTIVE,
            auto_approve_tool=lambda event: True,
            deny_all_tools=True,
        )
        assert p.rejected == ["rq1"] and p.approved == []

    def test_it_beats_session_trust(self):
        r = _RecordingRenderer()
        p = _ScriptedProvider(self._perm_script("bash"))
        _run(
            p,
            r,
            approval_mode=APPROVAL_INTERACTIVE,
            auto_approve_session=lambda: True,
            deny_all_tools=True,
        )
        assert p.rejected == ["rq1"] and p.approved == []

    def test_it_beats_auto_mode(self):
        r = _RecordingRenderer()
        p = _ScriptedProvider(self._perm_script("bash"))
        _run(p, r, approval_mode=APPROVAL_AUTO, deny_all_tools=True)
        assert p.rejected == ["rq1"] and p.approved == []

    def test_it_renders_no_prompt(self):
        """No decision is being asked for, so no question should appear."""
        r = _RecordingRenderer()
        p = _ScriptedProvider(self._perm_script("bash"))

        async def decider(_event):
            return True

        _run(p, r, approval_mode=APPROVAL_INTERACTIVE, decider=decider, deny_all_tools=True)
        assert p.rejected == ["rq1"]
        assert not any(e[0] == "prompt_choice" for e in r.events)

    def test_the_default_changes_nothing(self):
        """Every existing adopter must be byte-identical: the flag defaults off."""
        r = _RecordingRenderer()
        p = _ScriptedProvider(self._perm_script("bash"))
        _run(p, r, approval_mode=APPROVAL_AUTO)
        assert p.approved == ["rq1"] and p.rejected == []


class TestPromptChoiceNamesItsOwnTool:
    """A security prompt must name the tool IT asks about.

    Renderers used to reconstruct the name from the last ``tool_call`` they saw and
    never cleared it, so a permission arriving without its own titled tool call
    named the PREVIOUS tool. That is informed consent, so the name (and the purpose
    of the matching tool call) travel on the prompt event itself.
    """

    async def _decider(self, event):
        return True

    def _drive(self, script):
        r = _RecordingRenderer()
        p = _ScriptedProvider(script)
        _run(p, r, approval_mode=APPROVAL_INTERACTIVE, decider=self._decider)
        return [e for e in r.events if e[0] == "prompt_choice"]

    def test_the_permission_title_reaches_the_renderer(self):
        prompts = self._drive(
            [
                AcpEvent(
                    kind=EVENT_PERMISSION_REQUEST,
                    request_id="rq1",
                    title="fs_write",
                    options=[{"id": "approve"}],
                ),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        assert prompts, "no prompt_choice was dispatched"
        assert prompts[0][3] == "fs_write", (
            "the prompt carried no tool title, so a renderer can only guess the "
            "name from an earlier tool_call, which may be a different tool"
        )

    def test_a_later_permission_does_not_inherit_the_earlier_tool_name(self):
        """The exact shape of the defect: two tools, one titled permission each."""
        prompts = self._drive(
            [
                AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="t1", title="fs_read"),
                AcpEvent(
                    kind=EVENT_PERMISSION_REQUEST,
                    request_id="rq1",
                    title="fs_read",
                    tool_call_id="t1",
                    options=[{"id": "approve"}],
                ),
                # No tool_call precedes this one: the renderer's remembered name is
                # still fs_read, but the request is about execute_bash.
                AcpEvent(
                    kind=EVENT_PERMISSION_REQUEST,
                    request_id="rq2",
                    title="execute_bash",
                    tool_call_id="t2",
                    options=[{"id": "approve"}],
                ),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        assert [pr[3] for pr in prompts] == ["fs_read", "execute_bash"]

    def test_the_purpose_is_paired_by_tool_call_id_not_by_recency(self):
        """Purpose comes from the tool_call sharing the id, never the latest one."""
        prompts = self._drive(
            [
                AcpEvent(
                    kind=EVENT_TOOL_CALL,
                    tool_call_id="t1",
                    title="fs_read",
                    tool_purpose="Reading the config",
                ),
                AcpEvent(
                    kind=EVENT_TOOL_CALL,
                    tool_call_id="t2",
                    title="execute_bash",
                    tool_purpose="Deleting the backups",
                ),
                AcpEvent(
                    kind=EVENT_PERMISSION_REQUEST,
                    request_id="rq1",
                    title="fs_read",
                    tool_call_id="t1",
                    options=[{"id": "approve"}],
                ),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        assert prompts[0][3] == "fs_read"
        assert (
            prompts[0][4] == "Reading the config"
        ), "the prompt showed a purpose belonging to a different tool call"

    def test_an_unknown_tool_call_id_yields_no_purpose_rather_than_a_stale_one(self):
        prompts = self._drive(
            [
                AcpEvent(
                    kind=EVENT_TOOL_CALL,
                    tool_call_id="t1",
                    title="fs_read",
                    tool_purpose="Reading the config",
                ),
                AcpEvent(
                    kind=EVENT_PERMISSION_REQUEST,
                    request_id="rq1",
                    title="execute_bash",
                    tool_call_id="t-unseen",
                    options=[{"id": "approve"}],
                ),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        assert prompts[0][3] == "execute_bash"
        assert prompts[0][4] == ""

    def test_the_title_is_redacted_like_every_other_model_authored_string(self):
        """The title is agent-authored and reaches the chat, so it is screened."""
        prompts = self._drive(
            [
                AcpEvent(
                    kind=EVENT_PERMISSION_REQUEST,
                    request_id="rq1",
                    title="curl AKIAIOSFODNN7EXAMPLE",
                    options=[{"id": "approve"}],
                ),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in prompts[0][3]
