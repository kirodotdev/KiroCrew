"""The Discord dispatcher's turn: commands, monitor wakes, attachments and the queue.

``transport_dispatch.py`` routes an inbound message into a turn. These tests pin
the text commands, a monitor wake at each boundary of the session claim, the
turn's cleanup, attachment download and its caps, steering and queueing while
busy, ``!stop`` and ``!compact``, and that a queue shared by two people is
answered to each sender, not as one person.
"""

from __future__ import annotations

import asyncio
import os
import threading
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from discord_helpers import _drop_live_config_snapshot  # noqa: F401
from discord_helpers import (
    _PNG,
    FakeClient,
    FakeCtx,
    FakeProvider,
    _cfg,
    _dc_origin,
    _dispatcher,
    _Ev,
    _inbound,
    _inbound_with_id,
    _origin,
    _prime_live,
)

import kiro_crew.discord.transport_dispatch as td_mod
from kiro_crew import session_directive
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    AcpEvent,
    TurnUsage,
)
from kiro_crew.autonudge import AutoNudgeService
from kiro_crew.config import KiroCrewConfig
from kiro_crew.discord.renderer import DiscordApprovalDecider
from kiro_crew.discord.transport import DiscordInboundMessage
from kiro_crew.discord.transport_dispatch import (
    _NOT_A_SENDER,
    _STEER_ACK_EMOJI,
    DiscordDispatcher,
    _queued_origin,
    _QueuedOrigin,
)
from kiro_crew.messaging.attachments import cleanup
from kiro_crew.messaging.link import ChannelLink, legacy_dashboard_mirror_key
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.monitoring.completion import MonitorCompletionHook
from kiro_crew.monitoring.models import (
    MonitorActionCompletion,
    MonitorActionDisposition,
    MonitorBudgets,
    MonitorDispatchResult,
    MonitorOutcome,
)
from kiro_crew.session import SessionManager
from kiro_crew.session_allocation import SessionClosingError
from kiro_crew.testing.wait import await_until

# ── transport_dispatch.py ────────────────────────────────────────────────


class TestDispatcher:
    def _msg(self, text: str, user: str = "u1", chan: str = "c1") -> InboundMessage:
        return InboundMessage(channel_type="discord", user_id=user, conversation_id=chan, text=text)

    @pytest.mark.asyncio
    async def test_new_command_bumps_generation(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        k1 = d._session_key("u1")
        await d.handle_message(self._msg("!new"))
        assert d._session_key("u1") != k1
        assert "New conversation" in cli.sent[-1][0]

    @pytest.mark.asyncio
    async def test_help_command(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        await d.handle_message(self._msg("!help"))
        assert "Kiro Crew" in cli.sent[-1][0]
        assert "!sessions [query]" in cli.sent[-1][0]

    @pytest.mark.asyncio
    async def test_a_shutdown_refusal_is_not_spooled_for_a_restricted_session(
        self, tmp_path, monkeypatch
    ) -> None:
        """An incognito or temporary conversation persists nothing, the spool included.

        RED-BEFORE: without the restricted-session gate at the refusal point the
        private message is written verbatim to ``refused.jsonl``.
        """
        from kiro_crew.messaging import inbound_spool as S

        monkeypatch.setattr(S, "data_home", lambda: tmp_path)
        d, _cli, sess = _dispatcher({"u1"})
        sess.closing = True
        spool = tmp_path / "inbound-spool" / "refused.jsonl"

        # Persistent: the refusal is spooled.
        await d.handle_message(self._msg("keep me"))
        assert spool.exists() and "keep me" in spool.read_text(encoding="utf-8")
        spool.unlink()
        sess.reserve_inbound_callback = lambda: None

        d._session_resume.route = mock.AsyncMock(
            return_value=td_mod.RoutingDecision(resumed_key="dashboard:restricted")
        )

        async def _restricted(key: str) -> bool:
            return key == "dashboard:restricted"

        monkeypatch.setattr(d, "_session_restricted", _restricted)
        await d.handle_message(self._msg("my secret"))

        assert not spool.exists(), "an incognito message was persisted to the spool"

    @pytest.mark.asyncio
    async def test_monitor_wake_busy_at_dispatch_boundary_is_not_steered_or_queued(
        self,
    ) -> None:
        d, cli, sess = _dispatcher({"u1"})
        sess._busy = True
        completions: list[MonitorActionCompletion] = []

        async def _complete(completion: MonitorActionCompletion) -> None:
            completions.append(completion)

        result = await d.handle_message(
            self._msg("[Monitor wake]"),
            interpret_commands=False,
            monitor_completion=MonitorCompletionHook("mon-1", "failure-a", _complete),
        )

        assert result is MonitorDispatchResult.BUSY
        assert sess._gp.steered == []
        assert sess.queued == []
        assert cli.reactions == []
        assert completions == []

    @pytest.mark.asyncio
    async def test_monitor_wake_losing_race_at_session_claim_returns_busy(
        self,
    ) -> None:
        """A user turn winning after the advisory check must not make the wake wait."""

        class _LiveProvider(FakeProvider):
            async def start(self) -> None:
                return None

            async def shutdown(self) -> None:
                return None

            def is_process_alive(self) -> bool:
                return True

            def is_alive(self) -> bool:
                return True

            def context_usage_pct(self) -> float:
                return 0.0

        provider = _LiveProvider()

        def _factory(*_args: Any, **_kwargs: Any) -> _LiveProvider:
            return provider

        manager = SessionManager(KiroCrewConfig(), provider_factory=_factory)
        boundary_reached = asyncio.Event()
        resume_monitor = asyncio.Event()

        class _PausingSessions:
            def __init__(self) -> None:
                self.pause_next_claim = True

            def __getattr__(self, name: str) -> Any:
                return getattr(manager, name)

            async def get_or_create(self, *args: Any, **kwargs: Any) -> Any:
                if self.pause_next_claim:
                    self.pause_next_claim = False
                    boundary_reached.set()
                    # flake-ok: the stub waits on an event the test sets; moved verbatim
                    await resume_monitor.wait()
                return await manager.get_or_create(*args, **kwargs)

        sessions = _PausingSessions()
        dispatcher = DiscordDispatcher(
            sessions=sessions,  # type: ignore[arg-type]
            ctx_builder=FakeCtx(),  # type: ignore[arg-type]
            cfg=_cfg(),
            allowed_user_ids={"u1"},
        )
        client = FakeClient()
        dispatcher.client = client  # type: ignore[assignment]
        key = dispatcher._session_key("u1")
        await manager.get_or_create(key)
        manager.release(key)
        completions: list[MonitorActionCompletion] = []

        async def _complete(completion: MonitorActionCompletion) -> None:
            completions.append(completion)

        monitor_task = asyncio.create_task(
            dispatcher.handle_message(
                self._msg("[Monitor wake]"),
                interpret_commands=False,
                monitor_completion=MonitorCompletionHook("mon-1", "failure-a", _complete),
            )
        )
        try:
            await await_until(boundary_reached, what="the monitor reaching the claim boundary")
            await manager.get_or_create(key)  # A user turn wins the actual semaphore.
            resume_monitor.set()

            # Await completion while the user still owns the semaphore. Real
            # off-loop metadata reads may need more than a few scheduler turns;
            # a blocking claim cannot finish before finally releases the user.
            result = await await_until(monitor_task, what="the monitor dispatch completing")
            assert result is MonitorDispatchResult.BUSY
            assert provider.steered == []
            assert manager.dequeue(key) is None
            assert completions == []
        finally:
            resume_monitor.set()
            if manager.is_busy(key):
                manager.release(key)
            if not monitor_task.done():
                monitor_task.cancel()
            # flake-ok: awaits a monitor task the finally above released and cancelled
            await asyncio.gather(monitor_task, return_exceptions=True)
            await manager.close_all()

    @pytest.mark.asyncio
    async def test_monitor_wake_pre_turn_refusal_is_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        d, _cli, sess = _dispatcher({"u1"})
        completions: list[MonitorActionCompletion] = []

        async def _complete(completion: MonitorActionCompletion) -> None:
            completions.append(completion)

        async def _denied(_channel_type: str) -> bool:
            return False

        monkeypatch.setattr(
            "kiro_crew.discord.transport_dispatch.channel_inbound_permitted", _denied
        )
        result = await d.handle_message(
            self._msg("[Monitor wake]"),
            interpret_commands=False,
            monitor_completion=MonitorCompletionHook("mon-1", "failure-a", _complete),
        )

        assert result is MonitorDispatchResult.UNAVAILABLE
        assert sess.released == []
        assert sess.failures == []
        assert completions == []

    @pytest.mark.asyncio
    async def test_monitor_wake_cold_start_failure_is_unavailable(self) -> None:
        d, _cli, sess = _dispatcher({"u1"}, raise_on_get=True)
        completions: list[MonitorActionCompletion] = []

        async def _complete(completion: MonitorActionCompletion) -> None:
            completions.append(completion)

        result = await d.handle_message(
            self._msg("[Monitor wake]"),
            interpret_commands=False,
            monitor_completion=MonitorCompletionHook("mon-1", "failure-a", _complete),
        )

        assert result is MonitorDispatchResult.UNAVAILABLE
        assert sess.released == []
        assert sess.failures == []
        assert completions == []

    @pytest.mark.asyncio
    async def test_monitor_wake_transient_setup_failure_retries_as_busy(self) -> None:
        d, _cli, sess = _dispatcher({"u1"})
        d._render_config = mock.MagicMock(side_effect=OSError("temporary read failure"))
        completion = MonitorCompletionHook("mon-1", "failure-a", mock.AsyncMock())

        result = await d.handle_message(
            self._msg("[Monitor wake]"),
            interpret_commands=False,
            monitor_completion=completion,
        )

        assert result is MonitorDispatchResult.BUSY
        assert not completion.accepted
        assert sess.released == [d._session_key("u1")]

    @pytest.mark.asyncio
    async def test_monitor_wake_shutdown_during_session_claim_is_busy(self) -> None:
        d, _cli, sess = _dispatcher({"u1"})

        async def _closing_claim(*_args: Any, **_kwargs: Any) -> Any:
            raise SessionClosingError("closing")

        sess.get_or_create = _closing_claim  # type: ignore[method-assign]
        completion = MonitorCompletionHook(
            "mon-1",
            "failure-a",
            mock.AsyncMock(),
        )

        result = await d.handle_message(
            self._msg("[Monitor wake]"),
            interpret_commands=False,
            monitor_completion=completion,
        )

        assert result is MonitorDispatchResult.BUSY
        assert not completion.accepted

    @pytest.mark.asyncio
    async def test_monitor_wake_preserves_validated_generation(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        d, _cli, sess = _dispatcher({"u1"})
        original_key = d._session_key("u1")
        acquired: list[str] = []
        real_get_or_create = sess.get_or_create

        async def _capture(key: str, **kwargs: Any) -> Any:
            acquired.append(key)
            return await real_get_or_create(key, **kwargs)

        rotate = mock.MagicMock(
            side_effect=lambda scope_id, *_args, **_kwargs: d._conv.bump_gen(scope_id)
        )
        monkeypatch.setattr(sess, "get_or_create", _capture)
        monkeypatch.setattr(d._conv, "maybe_rotate", rotate)

        result = await d.handle_message(
            self._msg("[Monitor wake]"),
            interpret_commands=False,
            monitor_completion=MonitorCompletionHook("mon-1", "failure-a", mock.AsyncMock()),
        )

        assert result is MonitorDispatchResult.DISPATCHED
        rotate.assert_not_called()
        assert acquired == [original_key]

    @pytest.mark.asyncio
    async def test_monitor_wake_refuses_generation_rotated_after_gateway_validation(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        d, _cli, sess = _dispatcher({"u1"})
        validated_key = d._session_key("u1")
        d._conv.bump_gen(d._scope_id("u1", ""))
        current_key = d._session_key("u1")
        get_or_create = mock.AsyncMock(wraps=sess.get_or_create)
        monkeypatch.setattr(sess, "get_or_create", get_or_create)

        result = await d.handle_message(
            self._msg("[Monitor wake]"),
            interpret_commands=False,
            monitor_completion=MonitorCompletionHook("mon-1", "failure-a", mock.AsyncMock()),
            monitor_session_key=validated_key,
        )

        assert current_key != validated_key
        assert result is MonitorDispatchResult.UNAVAILABLE
        get_or_create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_monitor_wake_refuses_generation_rotated_after_session_claim(self) -> None:
        d, _cli, sess = _dispatcher({"u1"})
        validated_key = d._session_key("u1")

        class _RefusingProvider(FakeProvider):
            async def stream(self, message: str) -> Any:
                raise AssertionError("abandoned Discord generation reached the provider")
                yield

        provider = _RefusingProvider()

        async def _get_or_create(*args: Any, **kwargs: Any) -> Any:
            return provider, False, False

        async def _rotate_after_claim(_monitor_id: str, _fingerprint: str) -> bool:
            d._conv.bump_gen(d._scope_id("u1", ""))
            return True

        sess.get_or_create = _get_or_create  # type: ignore[method-assign]
        completion = MonitorCompletionHook(
            "mon-1",
            "failure-a",
            mock.AsyncMock(),
            authorization_callback=_rotate_after_claim,
        )

        result = await d.handle_message(
            self._msg("[Monitor wake]"),
            interpret_commands=False,
            monitor_completion=completion,
            monitor_session_key=validated_key,
        )

        assert result is MonitorDispatchResult.UNAVAILABLE
        assert not completion.accepted
        assert sess.successes == []
        assert sess.released == [validated_key]

    @pytest.mark.asyncio
    async def test_monitor_wake_dispatches_with_correlated_safe_completion(self) -> None:
        d, _cli, sess = _dispatcher({"u1"})
        completions: list[MonitorActionCompletion] = []

        class _SafeProvider(FakeProvider):
            async def stream(self, message: str) -> Any:
                yield _Ev(EVENT_TEXT_CHUNK, text=f"{self._reply}: {message[:16]}")
                yield _Ev(EVENT_COMPLETE, stop_reason="max_tokens")

        async def _get_or_create(*args: Any, **kwargs: Any) -> Any:
            return _SafeProvider(), False, False

        sess.get_or_create = _get_or_create  # type: ignore[method-assign]

        async def _complete(completion: MonitorActionCompletion) -> None:
            completions.append(completion)

        completion = MonitorCompletionHook("mon-1", "failure-a", _complete)
        result = await d.handle_message(
            self._msg("[Monitor wake]"),
            interpret_commands=False,
            monitor_completion=completion,
        )

        assert result is MonitorDispatchResult.DISPATCHED
        assert completion.accepted
        assert len(completions) == 1
        assert completions[0].monitor_id == "mon-1"
        assert completions[0].fingerprint == "failure-a"
        assert completions[0].disposition is MonitorActionDisposition.FAILURE
        assert sess.successes and sess.released

    @pytest.mark.asyncio
    async def test_monitor_wake_refuses_shutdown_before_provider_stream(self) -> None:
        d, _cli, sess = _dispatcher({"u1"})
        sess.closing = True
        completion = MonitorCompletionHook(
            "mon-1",
            "failure-a",
            mock.AsyncMock(),
        )

        result = await d.handle_message(
            self._msg("[Monitor wake]"),
            interpret_commands=False,
            monitor_completion=completion,
        )

        assert result is MonitorDispatchResult.BUSY
        assert not completion.accepted
        assert sess.successes == []
        assert sess.failures == []
        assert sess.released == [d._session_key("u1")]

    @pytest.mark.asyncio
    async def test_monitor_wake_rechecks_claim_before_discord_provider_stream(self) -> None:
        d, _cli, sess = _dispatcher({"u1"})

        class _RefusingProvider(FakeProvider):
            async def stream(self, message: str) -> Any:
                raise AssertionError("revoked monitor claim reached the provider")
                yield

        provider = _RefusingProvider()

        async def _get_or_create(*args: Any, **kwargs: Any) -> Any:
            return provider, False, False

        async def _authorize(_monitor_id: str, _fingerprint: str) -> bool:
            return False

        sess.get_or_create = _get_or_create  # type: ignore[method-assign]
        completion = MonitorCompletionHook(
            "mon-1",
            "failure-a",
            mock.AsyncMock(),
            authorization_callback=_authorize,
        )

        result = await d.handle_message(
            self._msg("[Monitor wake]"),
            interpret_commands=False,
            monitor_completion=completion,
        )

        assert result is MonitorDispatchResult.UNAVAILABLE
        assert not completion.accepted
        assert sess.successes == []
        assert sess.released == [d._session_key("u1")]

    @pytest.mark.asyncio
    async def test_monitor_stop_directive_before_safe_completion_keeps_accounting(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The stop tool result must not erase the wake claimed by this turn."""
        d, _cli, sess = _dispatcher({"u1"})
        session_key = d._session_key("u1")
        service = AutoNudgeService(base_dir=tmp_path)
        loop = await service.add_monitor(
            slot_key=session_key,
            kind="github_pull_request",
            target="https://github.com/acme/widgets/pull/7",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(),
            now=100.0,
        )
        assert await service.mark_monitor_action_in_flight(loop.id, "failure-a", now=120.0)

        class _DirectiveProvider(FakeProvider):
            async def stream(self, message: str) -> Any:
                yield AcpEvent(
                    kind=EVENT_TOOL_CALL,
                    tool_call_id="stop-1",
                    title="autonudge_stop",
                    tool_name="autonudge_stop",
                    mcp_server_name=session_directive.CORE_MCP_SERVER,
                )
                yield AcpEvent(
                    kind=EVENT_TOOL_RESULT,
                    tool_call_id="stop-1",
                    tool_output=session_directive.encode(
                        "autonudge_stop",
                        {"reason": "objective complete"},
                        "Monitor stop requested.",
                    ),
                    tool_final=True,
                )
                yield AcpEvent(
                    kind=EVENT_COMPLETE,
                    stop_reason="max_tokens",
                    usage=TurnUsage(input_tokens=11, output_tokens=7),
                )

        provider = _DirectiveProvider()

        async def _get_or_create(*args: Any, **kwargs: Any) -> Any:
            return provider, False, False

        sess.get_or_create = _get_or_create  # type: ignore[method-assign]
        monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: service)
        monkeypatch.setattr(
            "kiro_crew.autonudge_authz.sel",
            lambda: SimpleNamespace(log_tool_invocation=lambda **_kwargs: None),
        )
        completion = MonitorCompletionHook(
            loop.id,
            "failure-a",
            service.record_monitor_turn_completion,
            acceptance_callback=lambda: service.mark_monitor_turn_accepted(loop.id, "failure-a"),
        )

        result = await d.handle_message(
            self._msg("[Monitor wake]"),
            interpret_commands=False,
            monitor_completion=completion,
        )

        assert result is MonitorDispatchResult.DISPATCHED
        assert loop.monitor is not None
        assert loop.monitor.outcome is MonitorOutcome.USER_STOP
        assert not loop.active
        assert not loop.monitor.wake_in_flight
        assert loop.monitor.agent_turns == 1
        assert loop.monitor.wake_count == 1
        assert loop.monitor.input_tokens == 11
        assert loop.monitor.output_tokens == 7
        assert loop.monitor.last_completion_fingerprint == "failure-a"
        assert loop.next_due_ts == 0.0

        # A duplicate completion frame/callback cannot charge the retained
        # terminal record a second time.
        await completion.complete(
            MonitorActionDisposition.SUCCESS,
            TurnUsage(input_tokens=100, output_tokens=200),
            completed_ts=140.0,
        )
        assert loop.monitor.agent_turns == 1
        assert loop.monitor.wake_count == 1
        assert loop.monitor.input_tokens == 11
        assert loop.monitor.output_tokens == 7
        service.stop()

    @pytest.mark.asyncio
    async def test_session_released_even_when_renderer_close_raises(self, monkeypatch) -> None:
        """A rendering-finalization failure (e.g. Discord returning a
        malformed body) must never leave the session permanently busy."""
        from kiro_crew.discord.renderer import DiscordRenderer

        async def _boom(self) -> None:
            raise RuntimeError("finalization failed")

        monkeypatch.setattr(DiscordRenderer, "close", _boom)
        d, _, sess = _dispatcher({"u1"})
        await d.handle_message(self._msg("hello"))
        assert sess.released  # release still happened
        assert d._active_renderers == {}  # renderer entry cleaned up

    @pytest.mark.asyncio
    async def test_a_finished_turn_sweeps_its_approval_windows(self, monkeypatch) -> None:
        """A turn's last step on approvals: the session's armed button windows are
        discarded, so a late click can never answer a later turn's prompt."""
        swept: list[str] = []
        monkeypatch.setattr(
            DiscordApprovalDecider,
            "discard_session",
            classmethod(lambda cls, key: swept.append(key)),
        )
        d, _, _sess = _dispatcher({"u1"})
        await d.handle_message(self._msg("hello"))
        assert swept == [d._session_key("u1", "")]

    @pytest.mark.asyncio
    async def test_a_finished_turn_leaves_a_newer_renderer_registered(self, monkeypatch) -> None:
        """The cleanup pops only the renderer THIS turn registered: when another turn
        on the same key registered its own meanwhile, that one stays reachable."""
        d, _, _sess = _dispatcher({"u1"})
        newer = object()
        keys: list[str] = []

        async def _stream(self: Any, message: str) -> Any:
            (key,) = d._active_renderers  # this turn's own registration
            keys.append(key)
            d._active_renderers[key] = newer  # a later same-key turn re-registers
            yield _Ev(EVENT_TEXT_CHUNK, text="Answer")
            yield _Ev(EVENT_COMPLETE, stop_reason="end_turn")

        monkeypatch.setattr(FakeProvider, "stream", _stream)
        await d.handle_message(self._msg("hello"))
        assert keys and d._active_renderers == {keys[0]: newer}

    @pytest.mark.asyncio
    async def test_text_and_image_reach_prompt_then_temp_is_cleaned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop_thread = threading.get_ident()
        cleanup_threads: list[int] = []

        def _cleanup(paths: list[str]) -> None:
            cleanup_threads.append(threading.get_ident())
            cleanup(paths)

        monkeypatch.setattr(
            "kiro_crew.discord.transport_dispatch.cleanup_attachments",
            _cleanup,
        )
        d, cli, _ = _dispatcher({"u1"})
        url = "https://cdn.discordapp.com/attachments/c/m/photo.png"
        cli.attachment_bodies[url] = _PNG
        await d.handle_message(
            InboundMessage(
                channel_type="discord",
                user_id="u1",
                conversation_id="c1",
                text="look at this",
                attachments=[
                    {
                        "filename": "photo.png",
                        "content_type": "image/png",
                        "size": len(_PNG),
                        "url": url,
                    }
                ],
            )
        )
        # flake-ok: zero-delay yield after an awaited turn; moved verbatim
        await asyncio.sleep(0)

        prompt = d.ctx_builder.messages[-1]
        lines = prompt.splitlines()
        assert lines[0] == "look at this"
        assert lines[1].endswith(".png")
        assert cli.attachment_downloads == [url]
        assert not os.path.exists(lines[1])
        assert cleanup_threads and loop_thread not in cleanup_threads

    #: How long a wait for a step this test's own coroutine reaches may take before the run is
    #: written off. A lost-run guard, not a race to tune: a turn that never gets there fails
    #: by name here instead of reaching the suite's ``--timeout=120``. MEASURED: the attachment
    #: turn reaches its download after several executor hops, and a 1 s budget lost that race
    #: on every run once each executor job started 0.2 s late.
    _LOST_RUN_CEILING_SECS = 30.0

    @pytest.mark.asyncio
    async def test_attachment_turn_acquires_before_download_yields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        d, cli, sess = _dispatcher({"u1"})
        d.cfg.messaging.queue_mode = "queue"
        _prime_live(d.cfg)
        download_started = asyncio.Event()
        finish_download = asyncio.Event()
        url = "https://cdn.discordapp.com/attachments/c/m/slow.png"

        real_get_or_create = sess.get_or_create
        real_release = sess.release

        async def _get_or_create(*args: Any, **kwargs: Any) -> Any:
            result = await real_get_or_create(*args, **kwargs)
            sess._busy = True
            return result

        def _release(key: str) -> None:
            sess._busy = False
            real_release(key)

        async def _slow_download(download_url: str, dest: str) -> None:
            cli.attachment_downloads.append(download_url)
            download_started.set()
            # flake-ok: the stub waits on an event the test sets; moved verbatim
            await finish_download.wait()
            with open(dest, "wb") as fh:
                fh.write(_PNG)

        monkeypatch.setattr(sess, "get_or_create", _get_or_create)
        monkeypatch.setattr(sess, "release", _release)
        monkeypatch.setattr(cli, "download_attachment", _slow_download)

        first = asyncio.create_task(
            d.handle_message(
                InboundMessage(
                    channel_type="discord",
                    user_id="u1",
                    conversation_id="c1",
                    text="first",
                    attachments=[
                        {
                            "filename": "slow.png",
                            "content_type": "image/png",
                            "size": len(_PNG),
                            "url": url,
                        }
                    ],
                )
            )
        )
        try:
            await asyncio.wait_for(download_started.wait(), timeout=self._LOST_RUN_CEILING_SECS)
        except TimeoutError:
            pytest.fail(f"the download never started within {self._LOST_RUN_CEILING_SECS:.0f}s")

        assert sess._busy, "session must be acquired before attachment download"
        await d.handle_message(self._msg("second"))
        assert [queued[1] for queued in sess.queued] == ["second"]

        finish_download.set()
        # flake-ok: awaits the turn the line above released; moved verbatim
        await first

        assert d.ctx_builder.messages[0].splitlines()[0] == "first"
        assert d.ctx_builder.messages[1] == "second"
        assert sess.queued == []

    @pytest.mark.asyncio
    async def test_command_like_caption_does_not_discard_attachment(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        url = "https://cdn.discordapp.com/attachments/c/m/command.png"
        cli.attachment_bodies[url] = _PNG
        await d.handle_message(
            InboundMessage(
                channel_type="discord",
                user_id="u1",
                conversation_id="c1",
                text="!help",
                attachments=[
                    {
                        "filename": "command.png",
                        "content_type": "image/png",
                        "size": len(_PNG),
                        "url": url,
                    }
                ],
            )
        )
        # flake-ok: zero-delay yield after an awaited turn; moved verbatim
        await asyncio.sleep(0)

        prompt = d.ctx_builder.messages[-1]
        assert prompt.splitlines()[0] == "!help"
        assert prompt.splitlines()[1].endswith(".png")
        assert "Kiro Crew — Discord" not in "\n".join(text for text, _ in cli.sent)

    @pytest.mark.asyncio
    async def test_opaque_attachment_download_is_not_silent(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        url = "https://cdn.discordapp.com/a.bin"
        payload = b"complete opaque bytes"
        cli.attachment_bodies[url] = payload
        await d.handle_message(
            InboundMessage(
                channel_type="discord",
                user_id="u1",
                conversation_id="c1",
                text="",
                attachments=[
                    {
                        "filename": "archive.bin",
                        "content_type": "application/octet-stream",
                        "size": len(payload),
                        "url": url,
                    }
                ],
            )
        )
        # flake-ok: zero-delay yield after an awaited turn; moved verbatim
        await asyncio.sleep(0)

        prompt = d.ctx_builder.messages[-1]
        paths = [line for line in prompt.splitlines() if line.endswith(".bin")]
        assert "[Attached file: archive.bin]" in prompt
        assert cli.attachment_downloads == [url]
        assert len(paths) == 1
        assert not os.path.exists(paths[0])

    @pytest.mark.asyncio
    async def test_busy_attachment_waits_for_queued_turn_before_cleanup(self) -> None:
        d, cli, sess = _dispatcher({"u1"})
        url = "https://media.discordapp.net/attachments/c/m/queued.png"
        attachment = {
            "filename": "queued.png",
            "content_type": "image/png",
            "size": len(_PNG),
            "url": url,
        }
        cli.attachment_bodies[url] = _PNG
        sess._busy = True
        await d.handle_message(
            InboundMessage(
                channel_type="discord",
                user_id="u1",
                conversation_id="c1",
                text="",
                attachments=[attachment],
            )
        )

        assert cli.attachment_downloads == []
        assert sess.queued[0][2]["attachments"] == [attachment]
        sess._busy = False
        native_key = d._session_key("u1")
        sess.set_mirror_link(
            "dashboard:chat-1", ChannelLink("discord", channel_id="c1"), accepts_inbound=True
        )
        await d._drain_queue(native_key)
        # flake-ok: zero-delay yield after an awaited turn; moved verbatim
        await asyncio.sleep(0)

        assert sess.get_origin_link(native_key) == ChannelLink("discord", channel_id="c1")
        prompt_path = d.ctx_builder.messages[-1].splitlines()[0]
        assert cli.attachment_downloads == [url]
        assert prompt_path.endswith(".png")
        assert not os.path.exists(prompt_path)

    @pytest.mark.asyncio
    async def test_drain_defers_messages_that_exceed_attachment_cap(self) -> None:
        d, cli, sess = _dispatcher({"u1"})

        def _batch(prefix: str) -> list[dict[str, Any]]:
            batch: list[dict[str, Any]] = []
            for i in range(10):
                url = "https://cdn.discordapp.com/attachments/c/m/" f"{prefix}-{i}.png"
                cli.attachment_bodies[url] = _PNG
                batch.append(
                    {
                        "filename": f"{prefix}-{i}.png",
                        "content_type": "image/png",
                        "size": len(_PNG),
                        "url": url,
                    }
                )
            return batch

        first = _batch("first")
        second = _batch("second")
        sess.queued = [
            ("t1", "first batch", {"attachments": first, **_origin()}),
            ("t2", "second batch", {"attachments": second, **_origin()}),
            ("t3", "after second", {"attachments": [], **_origin()}),
        ]

        await d._drain_queue(d._session_key("u1"))

        assert cli.attachment_downloads == [item["url"] for item in [*first, *second]]
        assert sess.queued == []
        assert len(d.ctx_builder.messages) == 2
        assert d.ctx_builder.messages[0].splitlines()[0] == "first batch"
        assert d.ctx_builder.messages[1].splitlines()[0] == "second batch"
        assert "after second" in d.ctx_builder.messages[1]

    @pytest.mark.asyncio
    async def test_busy_steers_and_acks_with_reaction(self) -> None:
        d, cli, sess = _dispatcher({"u1"})
        sess._busy = True
        msg = DiscordInboundMessage(
            channel_type="discord",
            user_id="u1",
            conversation_id="c1",
            text="steer text",
            message_id="m42",
        )
        await d.handle_message(msg)
        assert sess._gp.steered == ["steer text"]
        assert cli.reactions == [("m42", _STEER_ACK_EMOJI)]

    @pytest.mark.asyncio
    async def test_busy_queue_override_enqueues_with_receipt(self) -> None:
        d, cli, sess = _dispatcher({"u1"})
        sess._busy = True
        await d.handle_message(self._msg("!queue later please"))
        assert [t for _, t, _ in sess.queued] == ["later please"]
        assert any("Queued" in t for t, _ in cli.sent)

    @pytest.mark.asyncio
    async def test_stop_cancels_and_clears_queue(self) -> None:
        d, cli, sess = _dispatcher({"u1"})
        sess._busy = True
        sess.queued.append(("ts", "queued msg", {}))
        await d.handle_message(self._msg("!stop"))
        assert sess._gp.cancelled == 1
        assert sess.queued == []
        assert "Stopped" in cli.sent[-1][0]

    @pytest.mark.asyncio
    async def test_compact_uses_try_acquire_and_releases(self) -> None:
        d, cli, sess = _dispatcher({"u1"})
        await d.handle_message(self._msg("!compact"))
        assert sess.acquired and sess.released
        visible = " ".join([text for text, _ in cli.sent] + [text for _, text, _ in cli.edits])
        assert "Context compacted" in visible

    @pytest.mark.asyncio
    async def test_compact_declined_on_auto_managed_backend(self) -> None:
        # A backend that cannot serve /compact gets the informational reply and
        # compact() is NEVER dispatched.
        d, cli, sess = _dispatcher({"u1"})
        calls: list[int] = []

        async def _compact(context: str = "") -> None:
            calls.append(1)

        sess._gp.compact = _compact
        sess._gp.manual_compact_unsupported_backend = "kas"
        await d.handle_message(self._msg("!compact"))
        visible = " ".join([text for text, _ in cli.sent] + [text for _, text, _ in cli.edits])
        assert "manages compaction automatically" in visible
        assert calls == []

    @pytest.mark.asyncio
    async def test_compact_none_capability_preserves_dispatch(self) -> None:
        # The ABC's None (supported) default keeps the existing dispatch.
        d, cli, sess = _dispatcher({"u1"})
        sess._gp.manual_compact_unsupported_backend = None
        await d.handle_message(self._msg("!compact"))
        visible = " ".join([text for text, _ in cli.sent] + [text for _, text, _ in cli.edits])
        assert "Context compacted" in visible

    @pytest.mark.asyncio
    async def test_compact_summary_body_is_not_sent(self) -> None:
        d, cli, sess = _dispatcher({"u1"})

        async def _completed(timeout: float = 0.0) -> dict:
            return {"type": "completed", "summary": "## OBJECTIVE\ninternal guidance"}

        sess._gp.wait_for_compaction = _completed
        await d.handle_message(self._msg("!compact"))
        visible = " ".join([text for text, _ in cli.sent] + [text for _, text, _ in cli.edits])
        assert "Context compacted" in visible
        assert "OBJECTIVE" not in visible and "internal guidance" not in visible

    @pytest.mark.asyncio
    async def test_compact_timeout_reports_gracefully(self) -> None:
        # Regression: nested 120s timeouts made the graceful-timeout branch
        # unreachable and destroyed a healthy session. A compaction that yields
        # no terminal status must report a timeout and KEEP the session.
        d, cli, sess = _dispatcher({"u1"})

        async def _timeout(timeout: float = 0.0) -> dict:
            return {"type": "timeout"}

        sess._gp.wait_for_compaction = _timeout
        await d.handle_message(self._msg("!compact"))
        assert any("timed out" in t for _, t, _ in cli.edits) or any(
            "timed out" in t for t, _ in cli.sent
        )
        assert sess.destroyed == [] and sess.discarded == []  # healthy session preserved

    @pytest.mark.asyncio
    async def test_link_and_unlink(self) -> None:
        d, cli, sess = _dispatcher({"u1"})
        await d.handle_message(self._msg("!link"))
        key = d._session_key("u1")
        assert key in sess.mirror_links
        assert legacy_dashboard_mirror_key(key) not in sess.mirror_links
        assert sess.mirror_links[key].channel_id == "c1"
        await d.handle_message(self._msg("!unlink"))
        assert key not in sess.mirror_links

    @pytest.mark.asyncio
    async def test_default_agent_fallback(self) -> None:
        d, _, sess = _dispatcher({"u1"})
        await d.handle_message(self._msg("hi"))
        assert sess.last_agent == "kirocrew"

    @pytest.mark.asyncio
    async def test_configured_default_agent_wins(self) -> None:
        d, _, sess = _dispatcher({"u1"}, default_agent="custom")
        await d.handle_message(self._msg("hi"))
        assert sess.last_agent == "custom"

    def test_thread_session_is_shared_but_dms_remain_per_user(self) -> None:
        d, _, _ = _dispatcher({"u1", "u2"}, allowed_threads={"t1"})
        assert d._session_key("u1", "t1") == d._session_key("u2", "t1")
        assert d._session_key("u1") != d._session_key("u2")


class TestDrainSenderIdentity:
    """A queue shared by two people must not be answered as one person.

    Under ``messaging.dm_scope = "unified"`` every allow-listed person's direct chat
    collapses into one session key -- ``build_dm_session_key`` reduces the bucket to
    ``unified:{agent}``, dropping both channel and user -- so ONE queue holds messages
    from several senders. A combined turn carries ONE envelope, so it may only combine
    messages that share one.
    """

    _UNIFIED = "unified:kirocrew"

    def test_the_surface_reports_whether_the_edit_landed(self) -> None:
        """A rate-limited chat answers a refusal rather than raising, and the registry
        can only keep that transition retryable if the wrapper reports it."""
        d, cli, _sess = _dispatcher({7})
        surface = d._receipt_surface("chan1")

        async def go() -> tuple[bool, bool]:
            cli.edit_ok = True
            ok = await surface.edit_receipt("m1", "body")
            cli.edit_ok = False
            refused = await surface.edit_receipt("m1", "body")
            return ok, refused

        ok, refused = asyncio.run(go())
        assert ok is True
        assert refused is False

    async def _queue(self, d: Any, sess: Any, *msgs: InboundMessage) -> None:
        """Queue each message through the REAL enqueue, mid-turn.

        End to end through the production writer, so the recorder and the reader are
        covered together: an origin nothing reads back is not a fix, and an origin a
        fixture spells by hand is not evidence production records one.
        """
        sess._busy = True
        for msg in msgs:
            assert await d._enqueue_with_receipt(
                self._UNIFIED,
                msg.conversation_id,
                msg.text,
                origin=_dc_origin(msg.user_id, msg.conversation_id, thread=msg.thread_id or ""),
                person_origin=msg.person_origin,
            ), "the fake session must accept a mid-turn enqueue"
        sess._busy = False  # the turn they queued behind has finished

    @staticmethod
    async def _drain(d: Any, key: str) -> list[InboundMessage]:
        """Drain, returning the envelope every replayed turn ran under."""
        seen: list[InboundMessage] = []
        original = d.handle_message

        async def _spy(msg: Any, **kw: Any) -> None:
            seen.append(msg)

        d.handle_message = _spy
        try:
            await d._drain_queue(key)
        finally:
            d.handle_message = original
        return seen

    @pytest.mark.asyncio
    async def test_a_drained_turn_carries_its_queued_entries_own_person_flag(self) -> None:
        """A gateway-built wake (``person_origin=False``) can reach the busy-queue path
        too, so the drain reads each entry's recorded flag instead of assuming a person:
        the wake replays without it, a person's message with it, and a collapsed turn
        holding a person's message is theirs."""
        from dataclasses import replace

        d, _cli, sess = _dispatcher({"u1", "u2"}, dm_scope="unified")
        person = replace(_inbound("mine", user_id="u1", conversation_id="c1"), person_origin=True)
        wake = _inbound("nudge", user_id="u2", conversation_id="c2")
        follow_up = replace(person, text="and mine")
        await self._queue(d, sess, wake, person, follow_up)

        seen = await self._drain(d, self._UNIFIED)

        assert [(m.text, m.person_origin) for m in seen] == [
            ("nudge", False),
            ("mine\n\nand mine", True),
        ]

    @pytest.mark.asyncio
    async def test_the_receipt_counts_only_the_answered_senders_own_deferrals(self) -> None:
        """ "+N deferred" is a promise TO ONE PERSON, so it may only count their messages.

        ``len(remainder)`` also counts the other sender's entries and any entry another
        TRANSPORT recorded. Each of those drains in its own turn, in its own channel, so
        showing them here tells this person to expect a follow-up for text they never
        wrote -- and when their own burst fit in one turn, their true count is zero.
        """
        d, _cli, sess = _dispatcher({"u1", "u2"}, dm_scope="unified")
        deferred: list[int] = []

        async def _flip(
            session_key: str, channel_id: str, answered: list[str], n: int = 0, owner: str = ""
        ) -> None:
            deferred.append(n)

        d._receipt_flip_locked = _flip
        await self._queue(
            d,
            sess,
            _inbound("mine", user_id="u1", conversation_id="c1"),
            _inbound("theirs", user_id="u2", conversation_id="c2"),
        )

        await self._drain(d, self._UNIFIED)

        assert deferred == [0, 0], "neither sender has a deferral of their OWN"

    @pytest.mark.asyncio
    async def test_a_senders_own_surplus_is_still_counted(self) -> None:
        """The guard against fixing the count by always reporting zero."""
        from kiro_crew.discord.transport_dispatch import _MAX_COLLAPSE

        d, _cli, sess = _dispatcher({"u1"}, dm_scope="unified")
        deferred: list[int] = []

        async def _flip(
            session_key: str, channel_id: str, answered: list[str], n: int = 0, owner: str = ""
        ) -> None:
            deferred.append(n)

        d._receipt_flip_locked = _flip
        await self._queue(
            d,
            sess,
            *(
                _inbound(f"m{i}", user_id="u1", conversation_id="c1")
                for i in range(_MAX_COLLAPSE + 2)
            ),
        )

        await self._drain(d, self._UNIFIED)

        assert deferred[0] == 2, "both of this sender's own surplus messages are theirs"

    @pytest.mark.asyncio
    async def test_two_senders_on_one_queue_drain_as_two_turns(self) -> None:
        """Each drained turn names the sender who wrote its text, in its own channel.

        Under ``messaging.dm_scope = "unified"`` every allow-listed person's direct chat
        collapses into one session key -- ``build_dm_session_key`` reduces the bucket to
        ``unified:{agent}``, dropping both channel and user -- so ONE queue holds
        messages from several senders. A combined turn carries ONE envelope, so it may
        only combine messages that share one.
        """
        d, cli, sess = _dispatcher({"u1", "u2"}, dm_scope="unified")
        await self._queue(
            d,
            sess,
            _inbound("mine", user_id="u1", conversation_id="c1"),
            _inbound("and mine", user_id="u2", conversation_id="c2"),
        )

        seen = await self._drain(d, self._UNIFIED)

        assert [m.text for m in seen] == ["mine", "and mine"], "one turn each, FIFO order"
        assert [m.user_id for m in seen] == ["u1", "u2"], "the turn must name its own author"
        assert [m.conversation_id for m in seen] == ["c1", "c2"], "and answer in their own channel"
        assert sess.queued == [], "the pump must drain the deferred entry too, not strand it"

    @pytest.mark.asyncio
    async def test_one_senders_burst_with_distinct_message_ids_still_collapses(self) -> None:
        """The ordinary case is unchanged: one person's burst is ONE turn.

        The two messages carry DISTINCT per-message ids, because Discord mints a
        snowflake per message and two real messages never share one. Grouping on a
        per-message identifier is the trap: it makes one person's own burst compare
        unequal, so the collapse stops firing and every burst drains as N turns. The
        origin records no such id, which is why the trap is avoided structurally here.
        """
        d, cli, sess = _dispatcher({"u1"}, dm_scope="unified")
        first = _inbound_with_id("first", message_id="m-1")
        second = _inbound_with_id("second", message_id="m-2")
        assert first.message_id != second.message_id, "the point of this test"
        await self._queue(d, sess, first, second)

        seen = await self._drain(d, self._UNIFIED)

        assert [m.text for m in seen] == ["first\n\nsecond"], "the burst must still collapse"
        assert [m.user_id for m in seen] == ["u1"]
        assert [m.conversation_id for m in seen] == ["c1"]

    @pytest.mark.asyncio
    async def test_a_third_sender_behind_two_does_not_jump_the_queue(self) -> None:
        """A differing sender defers itself AND everything behind it, so FIFO is exact."""
        d, cli, sess = _dispatcher({"u1", "u2"}, dm_scope="unified")
        await self._queue(
            d,
            sess,
            _inbound("a", user_id="u1", conversation_id="c1"),
            _inbound("b", user_id="u2", conversation_id="c2"),
            _inbound("c", user_id="u1", conversation_id="c1"),
        )

        seen = await self._drain(d, self._UNIFIED)

        # "c" is the same sender as "a", but it arrived AFTER "b": collapsing it into
        # the first turn would answer it ahead of a message that was queued earlier.
        assert [(m.user_id, m.text) for m in seen] == [("u1", "a"), ("u2", "b"), ("u1", "c")]

    @pytest.mark.asyncio
    async def test_a_deferred_entry_keeps_its_own_origin_when_requeued(self) -> None:
        """The re-enqueue must carry the origin, or the bug returns one iteration later."""
        d, cli, sess = _dispatcher({"u1", "u2"}, dm_scope="unified")
        await self._queue(
            d,
            sess,
            _inbound("mine", user_id="u1", conversation_id="c1"),
            _inbound("theirs", user_id="u2", conversation_id="c2"),
        )
        requeued: list[dict] = []
        real_enqueue = sess.enqueue

        def _spy(k: str, ts: str, text: str, **kw: Any) -> bool:
            requeued.append(dict(kw))
            return real_enqueue(k, ts, text, **kw)

        sess.enqueue = _spy  # type: ignore[method-assign]

        await self._drain(d, self._UNIFIED)

        assert requeued, "the differing sender's entry must be re-enqueued, not dropped"
        # Read back through the production reader rather than by spelling the storage
        # keys, so renaming one cannot leave this test passing.
        assert _queued_origin(requeued[0]) == _dc_origin("u2", "c2")

    @pytest.mark.asyncio
    async def test_the_receipt_is_flipped_in_the_channel_that_holds_its_bubble(self) -> None:
        """The bubble belongs to whoever queued first, not to whoever opened the turn."""
        d, cli, sess = _dispatcher({"u1", "u2"}, dm_scope="unified")
        d.cfg.messaging.queue_mode = "queue"
        _prime_live(d.cfg)
        await self._queue(d, sess, _inbound("held", user_id="u2", conversation_id="c2"))
        assert cli.send_channels and cli.send_channels[-1] == "c2", "the bubble lives in c2"

        await self._drain(d, self._UNIFIED)

        flips = [
            channel
            for channel, (_mid, text, _components) in zip(cli.edit_channels, cli.edits)
            if "Now answering" in text
        ]
        assert flips, "the drain must flip the receipt"
        assert flips[0] == "c2", "editing under another channel's address cannot land"

    @pytest.mark.asyncio
    async def test_a_queued_thread_message_replays_under_its_own_thread(self) -> None:
        """The thread rides on the entry, so a thread queue keeps its route."""
        d, cli, sess = _dispatcher({"u1"}, allowed_threads={"t9"}, dm_scope="unified")
        await self._queue(d, sess, _inbound("in the thread", conversation_id="c1", thread_id="t9"))

        seen = await self._drain(d, self._UNIFIED)

        assert [m.text for m in seen] == ["in the thread"]
        assert seen[0].thread_id == "t9"
        assert seen[0].conversation_id == "c1"

    def test_the_collapse_key_is_every_origin_field(self) -> None:
        """Every field on this origin names WHO or WHERE, so all of them group.

        Derived from ``_fields`` minus :data:`_NOT_A_SENDER` rather than restated, so
        adding a field joins the key by DEFAULT: a WHO field left out would let two
        people's messages collapse under one identity, while a surplus field only costs
        a collapse. The empty exclusion set is pinned because widening it is how the
        identity bug would return, and how the collapse would stop firing.
        """
        assert _NOT_A_SENDER == frozenset()
        assert _QueuedOrigin._fields == ("user_id", "channel_id", "thread_id")
        origin = _dc_origin("u1", "c1")
        assert origin.sender_key == (origin.user_id, origin.channel_id, origin.thread_id)
        # Different person, and the same person in a different place: unequal keys.
        assert origin._replace(user_id="u2").sender_key != origin.sender_key
        assert origin._replace(channel_id="c2").sender_key != origin.sender_key
        assert origin._replace(thread_id="t9").sender_key != origin.sender_key

    @pytest.mark.asyncio
    async def test_an_entry_another_transport_recorded_is_deferred_not_lost(self) -> None:
        """One queue can hold two transports, and neither may answer the other's.

        Every DM dispatcher is built with the orchestrator's single ``SessionManager``,
        and ``build_dm_session_key(..., dm_scope="unified", chat_type="direct")``
        returns ``unified:{agent}`` for EVERY channel -- it drops the channel as well
        as the user -- so a Discord DM and a Telegram DM to the same agent share one
        queue. A Telegram-recorded entry carries no field Discord can address, so
        answering it here would post one transport's reply into another's conversation,
        and raising on it would discard every message already dequeued this iteration.
        """
        d, cli, sess = _dispatcher({"u1"}, dm_scope="unified")
        foreign = {
            "telegram_user_id": "7",
            "telegram_chat_id": "70",
            "telegram_thread_id": "",
            "telegram_chat_type": "private",
            "telegram_username": "",
        }
        assert _queued_origin(foreign) is None, "not this channel's entry to read"
        sess.queued = [("t0", "theirs", dict(foreign))]

        seen = await self._drain(d, self._UNIFIED)

        assert seen == [], "Discord must not answer a Telegram-recorded message"
        assert [text for _ts, text, _kw in sess.queued] == ["theirs"], "and must not lose it"
        assert sess.queued[0][2] == foreign, "re-enqueued verbatim, for its own drain"

    @pytest.mark.asyncio
    async def test_a_foreign_entry_does_not_block_this_channels_own_messages(self) -> None:
        """It steps aside rather than holding the queue: order is per sender, not global.

        Blocking this channel's queue behind a foreign entry would strand it whenever
        the other transport sends nothing further, and FIFO between two transports is
        not something either sender can observe -- they are in different apps.
        """
        d, cli, sess = _dispatcher({"u1"}, dm_scope="unified")
        await self._queue(d, sess, _inbound("mine", user_id="u1", conversation_id="c1"))
        foreign = {
            "telegram_user_id": "7",
            "telegram_chat_id": "70",
            "telegram_thread_id": "",
            "telegram_chat_type": "private",
            "telegram_username": "",
        }
        sess.queued.insert(0, ("t-first", "theirs", dict(foreign)))

        seen = await self._drain(d, self._UNIFIED)

        assert [m.text for m in seen] == ["mine"], "the foreign entry ahead of it must not block"
        assert [text for _ts, text, _kw in sess.queued] == ["theirs"], "and stays for its own drain"

    def test_a_partly_recorded_own_entry_is_a_producer_bug_not_a_fallback(self) -> None:
        """An incomplete origin from THIS channel raises instead of guessing an address.

        Both producers are in this module -- ``_enqueue_with_receipt`` and the drain's
        own re-enqueue, which passes the entry's payload straight back -- so a partial
        record can only mean a change here dropped a field. Defaulting to empty strings
        would address the reply to an empty channel id, a silent misdelivery.

        Ownership is read off the NEUTRAL channel field, which is why an entry can be
        "mine, and broken" at all: without it, a missing field would be indistinguishable
        from another transport's entry and would be silently set aside forever.
        """
        with pytest.raises(KeyError) as caught:
            _queued_origin({"queued_channel": "discord", "discord_user_id": "u1"})
        assert "discord_channel_id" in str(caught.value), "the error must name what is missing"

        # An entry naming no channel, or another one, is the OTHER case: not this
        # dispatcher's, deferred rather than raised on.
        assert _queued_origin({}) is None
        assert _queued_origin({"queued_channel": "telegram"}) is None

    @pytest.mark.asyncio
    async def test_the_enqueued_entry_records_the_senders_own_origin(self) -> None:
        """Nothing downstream can recover an origin the entry never carried."""
        d, cli, sess = _dispatcher({"u1"}, dm_scope="unified")
        await self._queue(d, sess, _inbound("hello", user_id="u1", conversation_id="c1"))

        assert _queued_origin(sess.queued[0][2]) == _dc_origin("u1", "c1")
