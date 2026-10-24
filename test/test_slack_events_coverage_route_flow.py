"""Coverage for :mod:`kiro_crew.slack.events`: ``_route_message`` past its guards.

``!stop``, queueing, attachments, the transport path, the native drain, enterprise
and observe channels, temp-file cleanup, and the queued dispatch; then ``/status``
and the blocks a slash handler's reply carries.

Conventions follow ``test_slack_events_coverage.py``: a ``MagicMock`` stand-in for
``GatewayOrchestrator``, ``AsyncMock`` for the Slack client, and
``kiro_crew.slack.events.sel`` patched by the autouse fixtures from
``slack_events_coverage_helpers.py`` so no audit file is written.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_events_coverage_helpers import _fresh_trusted_bot_turn_ledger  # noqa: F401
from slack_events_coverage_helpers import _isolated_home_fixture  # noqa: F401
from slack_events_coverage_helpers import _mock_sel_fixture  # noqa: F401
from slack_events_coverage_helpers import _capture_respond, _drain, _event, _make_orch

from kiro_crew.config.loader import ACTIVATION_OBSERVE, KiroCrewConfig
from kiro_crew.slack import events as ev


class TestRouteMessageStopCommand:
    @pytest.mark.asyncio
    async def test_unauthorized_stop_denied(self, _mock_sel):
        orch = _make_orch()
        with patch("kiro_crew.slack.events.is_allowed_user", side_effect=[True, False]):
            with patch("kiro_crew.slack.events.is_owner", return_value=False):
                await ev._route_message(orch, _event(text="!stop"), ev.SeenCache())
        orch.slack.post_message.assert_awaited_with("D1", "⛔ Not authorized.", "100.0")
        assert _mock_sel.log_api_access.call_args.kwargs["error"] == "unauthorized sender"

    @pytest.mark.asyncio
    async def test_no_session_manager_reports_nothing_running(self, _mock_sel):
        orch = _make_orch()
        orch.sessions = None
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.is_owner", return_value=True):
                await ev._route_message(orch, _event(text="!stop"), ev.SeenCache())
        orch.slack.post_message.assert_awaited_with("D1", "Nothing running.", "100.0")
        assert _mock_sel.log_tool_invocation.call_args.kwargs["outcome"] == "no_session"

    @pytest.mark.asyncio
    async def test_active_session_stopped_and_callbacks_post(self, _mock_sel):
        orch = _make_orch()
        orch.sessions.has_session = MagicMock(return_value=True)

        async def _stop_turn(key, on_soft=None, on_hard=None, **_kw):
            await on_soft()
            await on_hard()
            return "soft"

        orch.sessions.stop_turn = AsyncMock(side_effect=_stop_turn)
        active = asyncio.get_running_loop().create_future()
        orch._session_tasks = {"100.0": active}
        orch._pending_queue = {"100.0": []}
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.is_owner", return_value=True):
                await ev._route_message(orch, _event(text="!stop"), ev.SeenCache())
        posted = [c[0][1] for c in orch.slack.post_message.await_args_list]
        assert "⏹ Execution stopped." in posted
        assert "⛔ Execution stopped — session reset." in posted
        assert active.cancelled()
        assert _mock_sel.log_tool_invocation.call_args.kwargs["outcome"] == "soft"

    @pytest.mark.asyncio
    async def test_idle_stop_dismisses_stale_ephemeral(self):
        orch = _make_orch()
        orch.sessions.has_session = MagicMock(return_value=True)
        orch.sessions.stop_turn = AsyncMock(return_value="idle")
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.is_owner", return_value=True):
                await ev._route_message(orch, _event(text="!stop"), ev.SeenCache())
        orch.slack.post_message.assert_awaited_with("D1", "Nothing running.", "100.0")

    @pytest.mark.asyncio
    async def test_bang_restart_delegates_to_restart_handler(self):
        orch = _make_orch()
        captured: list[str] = []

        async def _fake_restart(o, caller, args, respond):
            captured.append(caller)
            await respond("♻️ Restarting gateway…")

        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events._handle_restart", new=_fake_restart):
                await ev._route_message(orch, _event(text="!restart"), ev.SeenCache())
        assert captured == ["U_OWNER"]
        orch.slack.post_message.assert_awaited_with("D1", "♻️ Restarting gateway…", "100.0")


class TestRouteMessageQueueing:
    @pytest.mark.asyncio
    async def test_busy_session_enqueues_and_reacts(self):
        orch = _make_orch()
        orch._session_tasks = {"100.0": MagicMock()}
        orch.sessions.enqueue = MagicMock(return_value=True)
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(orch, _event(), ev.SeenCache())
        hm.assert_not_called()
        orch.slack.add_reaction.assert_awaited_once_with("D1", "100.0", "hourglass_flowing_sand")

    @pytest.mark.asyncio
    async def test_busy_session_without_session_object_uses_pending_queue(self):
        orch = _make_orch()
        orch._session_tasks = {"100.0": MagicMock()}
        orch.sessions.enqueue = MagicMock(return_value=False)
        orch.slack.add_reaction = AsyncMock(side_effect=RuntimeError("rate limited"))
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            await ev._route_message(orch, _event(), ev.SeenCache())
        assert orch._pending_queue["100.0"][0][0] == "100.0"

    @pytest.mark.asyncio
    async def test_session_level_enqueue_short_circuits(self):
        orch = _make_orch()
        orch.sessions.enqueue = MagicMock(return_value=True)
        orch.slack.add_reaction = AsyncMock(side_effect=RuntimeError("rate limited"))
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(orch, _event(), ev.SeenCache())
        hm.assert_not_called()
        assert orch.sessions.enqueue.call_args.args == ("100.0", "100.0", "hello")
        # The idle-session enqueue is not the busy-task one, which forces.
        assert "force" not in orch.sessions.enqueue.call_args.kwargs
        # The reaction failure is swallowed, and nothing falls back to the
        # orchestrator-level queue.
        orch.slack.add_reaction.assert_awaited_once_with("D1", "100.0", "hourglass_flowing_sand")
        assert "100.0" not in orch._pending_queue
        assert "100.0" not in orch._session_tasks


async def _tick_until(flag) -> None:
    """Set *flag* once the loop has demonstrably run, then keep yielding.

    The loop making progress is the whole assertion in
    `test_the_availability_probe_runs_off_the_event_loop`: if a synchronous probe is
    holding the loop, this coroutine never runs and the probe's own wait times out.
    """
    for _ in range(500):
        await asyncio.sleep(0.01)  # flake-ok: progress probe; the 10 ms yield is the measurement
        flag.set()


class TestRouteMessageAttachments:
    @pytest.mark.asyncio
    async def test_the_availability_probe_runs_off_the_event_loop(self):
        """On the `local` provider this probe dlopens a native library.

        `stt_available` reaches the availability probe, which imports the recogniser
        binding (measured at 209 ms cold). Called inline on this async path, the first
        inbound voice memo of a boot stalled the whole gateway loop and its liveness
        heartbeat with it -- every other caller of this offloads it.

        The stub reports the verdict itself, so the assertion does not depend on
        timing: it returns True only if the event loop made progress WHILE it ran, which
        is impossible unless the call was moved to a thread.
        """
        orch = _make_orch()
        files = [{"mimetype": "audio/webm", "url_private": "https://x.invalid/a.webm"}]
        loop_ran = threading.Event()

        def _probe_watching_the_loop():
            return loop_ran.wait(timeout=5.0)

        async def _let_the_loop_breathe(*a, **kw):
            # Runs only if the loop was free while the probe was in flight.
            loop_ran.set()
            return ["spoken"]

        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch(
                "kiro_crew.slack.events.stt_available", side_effect=_probe_watching_the_loop
            ):
                with patch(
                    "kiro_crew.slack.events._transcribe_with_reaction",
                    side_effect=_let_the_loop_breathe,
                ):
                    with patch(
                        "kiro_crew.slack.events.process_slack_files",
                        new_callable=AsyncMock,
                        return_value=([], []),
                    ):
                        with patch(
                            "kiro_crew.slack.events.handle_message", new_callable=AsyncMock
                        ) as hm:
                            ticker = asyncio.create_task(_tick_until(loop_ran))
                            try:
                                await ev._route_message(
                                    orch, _event(text="", files=files), ev.SeenCache()
                                )
                                await _drain(orch)
                            finally:
                                loop_ran.set()
                                ticker.cancel()
                                with contextlib.suppress(asyncio.CancelledError):
                                    await ticker
        assert hm.await_args is not None, "the message never routed"
        assert "[Voice memo transcription]" in hm.await_args[0][3]

    @pytest.mark.asyncio
    async def test_voice_transcript_is_prefixed(self):
        orch = _make_orch()
        files = [{"mimetype": "audio/webm", "url_private": "https://x.invalid/a.webm"}]
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.stt_available", return_value=True):
                with patch(
                    "kiro_crew.slack.events._transcribe_with_reaction",
                    new_callable=AsyncMock,
                    return_value=["spoken"],
                ):
                    with patch(
                        "kiro_crew.slack.events.process_slack_files",
                        new_callable=AsyncMock,
                        return_value=([], []),
                    ):
                        with patch(
                            "kiro_crew.slack.events.handle_message", new_callable=AsyncMock
                        ) as hm:
                            await ev._route_message(
                                orch, _event(text="", files=files), ev.SeenCache()
                            )
                            await _drain(orch)
        body = hm.await_args[0][3]
        assert "[Voice memo transcription]" in body
        assert hm.await_args.kwargs["had_voice_input"] is True

    @pytest.mark.asyncio
    async def test_image_and_text_blocks_are_appended(self, tmp_path):
        orch = _make_orch()
        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNG")
        files = [{"mimetype": "image/png", "url_private": "https://x.invalid/a.png"}]
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.stt_available", return_value=False):
                with patch(
                    "kiro_crew.slack.events.process_slack_files",
                    new_callable=AsyncMock,
                    return_value=([str(img)], ["file body"]),
                ):
                    with patch(
                        "kiro_crew.slack.events.handle_message", new_callable=AsyncMock
                    ) as hm:
                        await ev._route_message(
                            orch, _event(text="look", files=files), ev.SeenCache()
                        )
                        await _drain(orch)
        body = hm.await_args[0][3]
        assert str(img) in body
        assert "file body" in body
        # The temp image is unlinked by the done-callback once the turn finishes.
        assert not img.exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("use_transport", [False, True])
    async def test_spool_gets_the_pre_ingestion_message(self, tmp_path, use_transport):
        """The shutdown spool must not quote temp paths or file bodies.

        The turn's text is the ingested form; the spool gets what the user typed
        (mention stripped, before transcription) and how many files it carried.
        """
        orch = _make_orch(use_transport=use_transport)
        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNG")
        files = [
            {"mimetype": "image/png", "url_private": "https://x.invalid/a.png"},
            {"mimetype": "text/plain", "url_private": "https://x.invalid/b.txt"},
        ]
        target = "handle_message_transport" if use_transport else "handle_message"
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.stt_available", return_value=False):
                with patch(
                    "kiro_crew.slack.events.process_slack_files",
                    new_callable=AsyncMock,
                    return_value=([str(img)], ["[File: b.txt]\nfile body"]),
                ):
                    with patch(f"kiro_crew.slack.events.{target}", new_callable=AsyncMock) as hm:
                        with patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()):
                            await ev._route_message(
                                orch,
                                _event(text="<@UBOT> look", files=files),
                                ev.SeenCache(),
                                is_mention=True,
                            )
                            await _drain(orch)
        assert str(img) in hm.await_args[0][3]
        assert hm.await_args.kwargs["spool_text"] == "look"
        assert hm.await_args.kwargs["spool_attachments"] == 2

    @pytest.mark.asyncio
    async def test_busy_session_queues_the_pre_ingestion_message(self, tmp_path):
        orch = _make_orch()
        orch._session_tasks = {"100.0": MagicMock()}
        orch.sessions.enqueue = MagicMock(return_value=True)
        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNG")
        files = [{"mimetype": "image/png", "url_private": "https://x.invalid/a.png"}]
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.stt_available", return_value=False):
                with patch(
                    "kiro_crew.slack.events.process_slack_files",
                    new_callable=AsyncMock,
                    return_value=([str(img)], []),
                ):
                    await ev._route_message(orch, _event(text="look", files=files), ev.SeenCache())
        kw = orch.sessions.enqueue.call_args.kwargs
        assert kw["spool_text"] == "look"
        assert kw["spool_attachments"] == 1

    @pytest.mark.asyncio
    async def test_no_recoverable_text_cleans_up_temp_images(self, tmp_path):
        orch = _make_orch()
        img = tmp_path / "empty.png"
        img.write_bytes(b"\x89PNG")
        files = [{"mimetype": "image/png", "url_private": "https://x.invalid/a.png"}]
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.stt_available", return_value=False):
                with patch(
                    "kiro_crew.slack.events.process_slack_files",
                    new_callable=AsyncMock,
                    return_value=([], []),
                ) as process:
                    with patch(
                        "kiro_crew.slack.events.handle_message", new_callable=AsyncMock
                    ) as hm:
                        # A missing temp path must not raise from the cleanup loop.
                        img.unlink()
                        await ev._route_message(orch, _event(text="", files=files), ev.SeenCache())
        process.assert_awaited_once_with(orch, files)
        hm.assert_not_called()
        # Bailed before the history push and before any turn was scheduled.
        orch.channel_history.push.assert_not_called()
        assert "100.0" not in orch._session_tasks

    @pytest.mark.asyncio
    async def test_forwarded_attachment_text_is_recovered(self):
        orch = _make_orch()
        event = _event(
            text="This message contains interactive elements.",
            attachments=[{"is_share": True, "text": "the real body"}],
        )
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(orch, event, ev.SeenCache())
                await _drain(orch)
        assert hm.await_args[0][3] == "the real body"


class TestRouteMessageTransportPath:
    @pytest.mark.asyncio
    async def test_transport_path_dispatches_and_drains_queue(self):
        orch = _make_orch(use_transport=True)
        queued = [("101.0", "next up", {"channel": "C1"})]
        orch.sessions.dequeue = MagicMock(side_effect=lambda _k: queued.pop(0) if queued else None)
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch(
                "kiro_crew.slack.events.handle_message_transport", new_callable=AsyncMock
            ) as transport:
                with patch(
                    "kiro_crew.slack.events._dispatch_queued", new_callable=AsyncMock
                ) as dispatch:
                    with patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()):
                        await ev._route_message(orch, _event(), ev.SeenCache())
                        await _drain(orch)
                        await _drain(orch)
        transport.assert_awaited_once()
        dispatch.assert_awaited_once_with(orch, "100.0", "101.0", "next up", {"channel": "C1"})
        assert orch._session_tasks == {}

    @pytest.mark.asyncio
    async def test_transport_drain_falls_back_to_pending_queue(self):
        orch = _make_orch(use_transport=True)
        orch.sessions.dequeue = MagicMock(return_value=None)
        orch._pending_queue = {"100.0": [("101.0", "pending", {"channel": "C1"})]}
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message_transport", new_callable=AsyncMock):
                with patch(
                    "kiro_crew.slack.events._dispatch_queued", new_callable=AsyncMock
                ) as dispatch:
                    with patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()):
                        await ev._route_message(orch, _event(), ev.SeenCache())
                        await _drain(orch)
                        await _drain(orch)
        dispatch.assert_awaited_once()
        assert "100.0" not in orch._pending_queue

    @pytest.mark.asyncio
    async def test_transport_drain_failure_is_logged(self, caplog):
        orch = _make_orch(use_transport=True)
        orch.sessions.dequeue = MagicMock(side_effect=RuntimeError("queue corrupt"))
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message_transport", new_callable=AsyncMock):
                with patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()):
                    with caplog.at_level("ERROR", logger="kiro_crew.slack.events"):
                        await ev._route_message(orch, _event(), ev.SeenCache())
                        await _drain(orch)
        assert any("_on_transport_done drain failed" in r.message for r in caplog.records)


class TestRouteMessageNativeDrain:
    @pytest.mark.asyncio
    async def test_native_done_callback_drains_session_queue(self):
        orch = _make_orch()
        queued = [("101.0", "next up", {"channel": "C1"})]
        orch.sessions.dequeue = MagicMock(side_effect=lambda _k: queued.pop(0) if queued else None)
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock):
                with patch(
                    "kiro_crew.slack.events._dispatch_queued", new_callable=AsyncMock
                ) as dispatch:
                    await ev._route_message(orch, _event(), ev.SeenCache())
                    await _drain(orch)
                    await _drain(orch)
        dispatch.assert_awaited_once_with(orch, "100.0", "101.0", "next up", {"channel": "C1"})
        assert queued == []

    @pytest.mark.asyncio
    async def test_native_done_callback_drain_failure_is_logged(self, caplog):
        orch = _make_orch()
        orch.sessions.dequeue = MagicMock(side_effect=RuntimeError("queue corrupt"))
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock):
                with caplog.at_level("ERROR", logger="kiro_crew.slack.events"):
                    await ev._route_message(orch, _event(), ev.SeenCache())
                    await _drain(orch)
        assert any("_on_done drain failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_native_drain_falls_back_to_pending_queue(self):
        orch = _make_orch()
        orch.sessions.dequeue = MagicMock(return_value=None)
        orch._pending_queue = {"100.0": [("101.0", "pending", {"channel": "D1"})]}
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock):
                with patch(
                    "kiro_crew.slack.events._dispatch_queued", new_callable=AsyncMock
                ) as dispatch:
                    await ev._route_message(orch, _event(), ev.SeenCache())
                    await _drain(orch)
                    await _drain(orch)
        dispatch.assert_awaited_once()
        assert "100.0" not in orch._pending_queue


class TestRouteMessageEnterpriseAndObserve:
    @pytest.mark.asyncio
    async def test_enterprise_origin_mismatch_rejects(self, _mock_sel):
        orch = _make_orch()
        ctx = MagicMock()
        ctx.return_value.slack_gate.check_message_origin.return_value = False
        with patch("kiro_crew.slack.events.current_context", ctx):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(orch, _event(), ev.SeenCache())
        hm.assert_not_called()
        assert _mock_sel.log_api_access.call_args.kwargs["error"] == "enterprise_origin_mismatch"

    @pytest.mark.asyncio
    async def test_observe_records_history_then_drops_plain_message(self, _mock_sel):
        orch = _make_orch(dm_activation=ACTIVATION_OBSERVE)
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(orch, _event(), ev.SeenCache())
        hm.assert_not_called()
        orch.channel_history.push.assert_called_once()
        assert "activation=observe" in _mock_sel.log_api_access.call_args.kwargs["error"]

    @pytest.mark.asyncio
    async def test_observe_processes_mention(self):
        orch = _make_orch(dm_activation=ACTIVATION_OBSERVE)
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(
                    orch, _event(text="<@BOT1> hi"), ev.SeenCache(), is_mention=True
                )
                await _drain(orch)
        hm.assert_awaited_once()
        # Observe mode pushed history up-front, so the post-activation push is skipped.
        assert orch.channel_history.push.call_count == 1

    @pytest.mark.asyncio
    async def test_observe_follows_active_thread(self):
        orch = _make_orch(dm_activation=ACTIVATION_OBSERVE)
        orch.sessions.has_session = MagicMock(return_value=True)
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(orch, _event(thread_ts="99.0"), ev.SeenCache())
                await _drain(orch)
        orch.sessions.has_session.assert_any_call("99.0")
        hm.assert_awaited_once()
        assert hm.await_args[0][3] == "hello"

    @pytest.mark.asyncio
    async def test_observe_skips_history_for_unauthorized_sender(self):
        orch = _make_orch(dm_activation=ACTIVATION_OBSERVE)
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=False) as allowed:
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(orch, _event(), ev.SeenCache())
        allowed.assert_any_call("U_OWNER")
        orch.channel_history.push.assert_not_called()
        hm.assert_not_called()


class TestRouteMessageTempCleanup:
    @pytest.mark.asyncio
    async def test_missing_temp_image_does_not_raise_on_cleanup(self, tmp_path):
        orch = _make_orch()
        ghost = tmp_path / "already-gone.png"
        files = [{"mimetype": "image/png", "url_private": "https://x.invalid/a.png"}]
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.stt_available", return_value=False):
                with patch(
                    "kiro_crew.slack.events.process_slack_files",
                    new_callable=AsyncMock,
                    return_value=([str(ghost)], []),
                ):
                    with patch(
                        "kiro_crew.slack.events.handle_message", new_callable=AsyncMock
                    ) as hm:
                        await ev._route_message(
                            orch, _event(text="see", files=files), ev.SeenCache()
                        )
                        await _drain(orch)
        hm.assert_awaited_once()
        assert str(ghost) in hm.await_args[0][3]


class TestDispatchQueued:
    @pytest.mark.asyncio
    async def test_temp_paths_are_unlinked_after_the_turn(self, tmp_path):
        orch = _make_orch()
        img = tmp_path / "queued.png"
        img.write_bytes(b"\x89PNG")
        kwargs = {"channel": "D1", "thread_ts": None, "image_temp_paths": [str(img)]}
        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
            await ev._dispatch_queued(orch, "100.0", "101.0", "queued text", kwargs)
        hm.assert_awaited_once()
        assert not img.exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("use_transport", [False, True])
    async def test_pre_ingestion_message_travels_to_the_handler(self, use_transport):
        orch = _make_orch(use_transport=use_transport)
        kwargs = {"channel": "D1", "spool_text": "look", "spool_attachments": 1}
        target = "handle_message_transport" if use_transport else "handle_message"
        with patch(f"kiro_crew.slack.events.{target}", new_callable=AsyncMock) as hm:
            with patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()):
                await ev._dispatch_queued(orch, "100.0", "101.0", "look\n/tmp/x.png", kwargs)
        assert hm.await_args.kwargs["spool_text"] == "look"
        assert hm.await_args.kwargs["spool_attachments"] == 1

    @pytest.mark.asyncio
    async def test_missing_temp_path_is_tolerated(self, tmp_path):
        orch = _make_orch()
        kwargs = {"channel": "D1", "image_temp_paths": [str(tmp_path / "nope.png")]}
        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
            await ev._dispatch_queued(orch, "100.0", "101.0", "queued text", kwargs)
        hm.assert_awaited_once()
        assert hm.await_args[0][3] == "queued text"
        orch.slack.remove_reaction.assert_awaited_once_with("D1", "101.0", "hourglass_flowing_sand")


class TestHandleStatus:
    @pytest.mark.asyncio
    async def test_reports_stats_summary_with_identity_line(self):
        respond = AsyncMock()
        with patch("kiro_crew.slack.events.Stats") as stats:
            stats.return_value.summary.return_value = "turns: 3"
            await ev._handle_status(_make_orch(), "U_OWNER", "", respond)
        assert respond.call_args[0][0].startswith("turns: 3")


class TestHandleSlashRespondBlocks:
    @pytest.mark.asyncio
    async def test_blocks_are_forwarded_in_the_response_body(self):
        orch = _make_orch()
        posted: list[dict] = []
        blocks = [{"type": "divider"}]

        async def _handler(o, caller, args, respond):
            await respond("with blocks", blocks=blocks)

        ev.register_slash_command("zzcovblocks", _handler, "coverage probe")
        try:
            payload = {
                "command": "/kirocrew",
                "user_id": "U_OWNER",
                "text": "zzcovblocks",
                "response_url": "https://hooks.example.invalid/x",
            }
            with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
                with _capture_respond(posted):
                    await ev._handle_slash(orch, payload)
                    await _drain(orch)
        finally:
            ev.SLASH_REGISTRY.pop("zzcovblocks", None)
        assert posted[0]["blocks"] == blocks
        assert posted[0]["response_type"] == "ephemeral"

    @pytest.mark.asyncio
    async def test_without_response_url_nothing_is_posted(self):
        orch = _make_orch()
        posted: list[dict] = []

        async def _handler(o, caller, args, respond):
            await respond("dropped")

        ev.register_slash_command("zzcovnourl", _handler, "coverage probe")
        try:
            payload = {"command": "/kirocrew", "user_id": "U_OWNER", "text": "zzcovnourl"}
            with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
                with _capture_respond(posted, response_url=""):
                    await ev._handle_slash(orch, payload)
                    await _drain(orch)
        finally:
            ev.SLASH_REGISTRY.pop("zzcovnourl", None)
        assert posted == []
