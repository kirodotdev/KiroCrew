"""Telegram dispatcher commands: /yolo, bare directives and the command table.

Covers the dispatcher's command surface, the routing in ``TelegramDispatcher.handle_message``
(transport_dispatch.py) and the handlers in dispatch/commands.py: the process-wide /yolo
grant, bare mid-turn directives, the table that routes each command to exactly one
handler, and the exact replies of the commands the dispatcher answers itself.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from telegram_helpers import _drop_live_config_snapshot  # noqa: F401
from telegram_helpers import _dispatcher, _dm, _origin, _tg_origin

from kiro_crew.messaging.session_resume import RoutingDecision
from kiro_crew.telegram.commands import build_help_text
from kiro_crew.telegram.transport import TelegramInboundMessage


class TestYoloCommand:
    """/yolo drives the SAME process-wide grant as the dashboard and Slack."""

    def _reset(self) -> Any:
        from kiro_crew.safety_override import safety_override

        so = safety_override()
        if so.is_active():
            so.deactivate("test")
        return so

    def test_bare_yolo_reports_status_without_changing_it(self) -> None:
        so = self._reset()
        d, cli, _ = _dispatcher({7})
        try:
            asyncio.run(d.handle_message(_dm("/yolo")))
            assert "OFF" in cli.sent[-1][0]
            assert "Usage: /yolo on | off | renew" in cli.sent[-1][0]
            assert so.is_active() is False, "a status read must not arm the grant"
        finally:
            self._reset()

    def test_on_then_off_round_trips_the_grant(self) -> None:
        so = self._reset()
        d, cli, _ = _dispatcher({7})
        try:
            asyncio.run(d.handle_message(_dm("/yolo on")))
            assert so.is_active() is True
            assert "ON" in cli.sent[-1][0]

            asyncio.run(d.handle_message(_dm("/yolo off")))
            assert so.is_active() is False
            assert "OFF" in cli.sent[-1][0]
        finally:
            self._reset()

    def test_off_on_a_lapsed_grant_closes_the_renew_grace_window(self) -> None:
        """ "/yolo off" must revoke a grant whose TTL already elapsed.

        ``deactivate()`` zeroes the past deadline, and that is what shuts the
        5-minute renew grace window. Skipping the call for a lapsed grant left
        it renewable, so a later "/yolo renew" silently restored auto-approval
        after the operator had been told "YOLO OFF".
        """
        so = self._reset()
        d, cli, _ = _dispatcher({7})
        try:
            asyncio.run(d.handle_message(_dm("/yolo on")))
            assert so.is_active() is True

            # Lapse the grant but stay inside the renew grace window.
            # flake-ok: pre-existing real-clock expiry set 10 s in the past, moved verbatim; tracked for a de-flake pass
            so._expires_at = time.monotonic() - 10
            assert so.is_active() is False, "precondition: the grant has lapsed"

            asyncio.run(d.handle_message(_dm("/yolo off")))
            assert "OFF" in cli.sent[-1][0]

            assert so.renew("test").renewed is False, (
                "an explicitly revoked grant must not be resurrectable by renew "
                "just because its TTL happened to elapse before the off"
            )
            assert so.is_active() is False
        finally:
            self._reset()

    def test_unknown_argument_falls_back_to_status(self) -> None:
        so = self._reset()
        d, cli, _ = _dispatcher({7})
        try:
            asyncio.run(d.handle_message(_dm("/yolo maybe")))
            assert so.is_active() is False, "an unrecognised verb must never arm the grant"
            assert "Usage:" in cli.sent[-1][0]
        finally:
            self._reset()

    def test_grant_is_read_per_request_not_captured_at_boot(self) -> None:
        # The predicate handed to TurnDriver must consult the LIVE grant, or
        # /yolo would only take effect after a gateway restart. Mutating the
        # grant between calls proves the closure is not a captured snapshot.
        so = self._reset()
        d, _cli, _sess = _dispatcher({7})
        captured: list = []

        class _Recorder:
            def __init__(self, *a: Any, **kw: Any) -> None:
                captured.append(kw.get("auto_approve_session"))

            async def run(self, message: str) -> str:
                return "ok"

        try:
            with patch("kiro_crew.telegram.transport_dispatch.TurnDriver", _Recorder):
                asyncio.run(d.handle_message(_dm("hi")))
            predicate = captured[0]
            assert predicate is not None
            assert predicate() is False
            so.activate("test")
            assert predicate() is True, "the predicate must re-read the grant, not cache it"
        finally:
            self._reset()


class TestBareMidTurnDirective:
    def test_bare_queue_gets_usage_not_a_model_turn(self) -> None:
        d, cli, sess = _dispatcher({7})
        asyncio.run(d.handle_message(_dm("/queue")))
        assert "Those take a message" in cli.sent[-1][0]
        assert sess.successes == [], "the bare token must never reach the model"

    def test_queue_with_a_body_still_runs_as_content(self) -> None:
        d, _cli, sess = _dispatcher({7})
        asyncio.run(d.handle_message(_dm("/queue do the thing")))
        assert sess.successes == ["telegram:kirocrew:direct:7"]


class TestCommandDispatchTable:
    """Every command reaches exactly one handler, through ``self``, with these arguments."""

    _ROUTE = ("direct", "7")
    _HANDLERS = (
        "_handle_compact",
        "_handle_link",
        "_handle_unlink",
        "_handle_stop",
        "_handle_model",
        "_handle_agent",
        "_handle_voice",
        "_handle_title",
        "_handle_cron",
        "_handle_spawn",
        "_handle_task",
        "_handle_yolo",
        "_handle_dashboard",
    )

    @pytest.mark.parametrize(
        ("text", "handler", "args", "kwargs"),
        [
            ("/compact", "_handle_compact", (_ROUTE, 7), {"session_key": None}),
            ("/link", "_handle_link", (_ROUTE, 7), {"resumed_key": None}),
            ("/unlink", "_handle_unlink", (_ROUTE, 7), {}),
            ("/stop", "_handle_stop", (_ROUTE, 7), {"origin": "ORIGIN", "session_key": None}),
            ("/model x", "_handle_model", (_ROUTE, 7, "x"), {"session_key": None}),
            ("/agent foo", "_handle_agent", (_ROUTE, 7, "foo"), {}),
            ("/voice on", "_handle_voice", (_ROUTE, 7, "on", None), {}),
            ("/title T", "_handle_title", (_ROUTE, 7, "T"), {"session_key": None}),
            ("/cron list", "_handle_cron", (7, "list"), {"caller": "7", "thread": None}),
            ("/spawn x", "_handle_spawn", (_ROUTE, 7, "x"), {"thread": None, "session_key": None}),
            (
                "/task run s",
                "_handle_task",
                (7, "run s"),
                {"route": _ROUTE, "thread": None, "session_key": None},
            ),
            ("/yolo on", "_handle_yolo", (7, "on", 7), {"thread": None}),
            (
                "/kirocrew dashboard 2h",
                "_handle_dashboard",
                (_ROUTE, 7, "/kirocrew dashboard 2h", 7),
                {},
            ),
        ],
    )
    def test_a_command_reaches_one_handler(
        self, text: str, handler: str, args: tuple, kwargs: dict
    ) -> None:
        d, cli, sess = _dispatcher({7})
        mocks = {name: AsyncMock() for name in self._HANDLERS}
        for name, mock in mocks.items():
            setattr(d, name, mock)
        if kwargs.get("origin") == "ORIGIN":
            kwargs = {**kwargs, "origin": _tg_origin(7, 7)}

        asyncio.run(d.handle_message(_dm(text)))

        mocks[handler].assert_awaited_once()
        assert mocks[handler].await_args.args == args
        assert mocks[handler].await_args.kwargs == kwargs
        assert [n for n, m in mocks.items() if m.await_count and n != handler] == []
        assert sess.successes == []

    @pytest.mark.parametrize(
        "text",
        ["/ping", "/cron list", "/yolo", "/kirocrew dashboard", "/voice", "/agent", "/sessions"],
    )
    def test_an_exempt_command_never_asks_the_resume_router(self, text: str) -> None:
        d, _cli, _sess = _dispatcher({7})
        route = AsyncMock(return_value=RoutingDecision(refusal="no"))
        d._session_resume.route = route  # type: ignore[method-assign]
        d._session_resume.show_picker = AsyncMock()  # type: ignore[method-assign]
        d._installed_agent_names = staticmethod(lambda: [])  # type: ignore[method-assign]
        asyncio.run(d.handle_message(_dm(text)))
        route.assert_not_awaited()

    def test_commands_run_while_a_turn_is_busy(self) -> None:
        d, cli, sess = _dispatcher({7})
        sess._busy = True
        asyncio.run(d.handle_message(_dm("/new")))
        assert d._conv.current_gen(self._ROUTE) == 1
        assert "New conversation started" in cli.sent[-1][0]
        asyncio.run(d.handle_message(_dm("/ping")))
        assert cli.sent[-1][0] == "pong"
        assert sess.queued == [] and sess._gp.steered == []

    def test_compact_releases_the_notice_latch(self) -> None:
        d, _cli, _sess = _dispatcher({7})
        d._conv.set_awaiting(self._ROUTE)
        asyncio.run(d.handle_message(_dm("/compact")))
        assert not d._conv.is_awaiting(self._ROUTE)

    def test_a_forum_command_threads_into_its_topic(self) -> None:
        d, _cli, _sess = _dispatcher({7}, allow_forum=True, allowed_forum_chat_ids=[-100999])
        mocks = {name: AsyncMock() for name in self._HANDLERS}
        for name, mock in mocks.items():
            setattr(d, name, mock)
        for text in ("/voice on", "/spawn x", "/task run s", "/yolo on", "/cron list"):
            asyncio.run(
                d.handle_message(
                    TelegramInboundMessage(
                        channel_type="telegram",
                        user_id="7",
                        conversation_id="-100999",
                        text=text,
                        thread_id="4",
                        chat_type="supergroup",
                        message_id=5,
                    )
                )
            )
        route = ("forum", "-100999:4")
        assert mocks["_handle_voice"].await_args.args == (route, -100999, "on", 4)
        assert mocks["_handle_spawn"].await_args.kwargs["thread"] == 4
        assert mocks["_handle_task"].await_args.kwargs["thread"] == 4
        assert mocks["_handle_yolo"].await_args.kwargs == {"thread": 4}
        # /cron is a host listing: refused in a Topic before its handler runs.
        mocks["_handle_cron"].assert_not_awaited()


class TestCommandReplies:
    """The exact replies of the commands the dispatcher answers itself."""

    _ROUTE = ("direct", "7")
    _KEY = "telegram:kirocrew:direct:7"
    _RELEASE = (
        "⚠️ Could not leave the resumed session safely, so nothing changed. "
        "Try again before sending another message."
    )

    def test_new_replies(self) -> None:
        from kiro_crew.messaging.session_resume import ResumeReleaseError

        d, cli, sess = _dispatcher({7})
        reserved: list[str] = []
        sess.reserve_generation = lambda key: reserved.append(key)  # type: ignore[attr-defined]
        asyncio.run(d.handle_message(_dm("/new")))
        assert cli.sent == [("✅ New conversation started.", None)]
        assert reserved == ["telegram:kirocrew:direct:7:gen1"]

        d, cli, sess = _dispatcher({7})
        asyncio.run(d.handle_message(_dm("/new")))
        assert cli.sent[-1][0] == (
            "✅ New conversation started.\n⚠️ The new conversation could not be saved for restart."
        )

        d, cli, sess = _dispatcher({7})
        sess.reserve_generation = lambda key: None  # type: ignore[attr-defined]
        leave = AsyncMock(return_value="dashboard:chat-1")
        d._session_resume.leave_resumed_session = leave  # type: ignore[method-assign]
        asyncio.run(d.handle_message(_dm("/new")))
        assert cli.sent == [("✅ New conversation started — left the resumed session.", None)]
        assert leave.await_args.args == (7, None)

        d, cli, sess = _dispatcher({7})
        d._session_resume.leave_resumed_session = AsyncMock(  # type: ignore[method-assign]
            side_effect=ResumeReleaseError("x")
        )
        asyncio.run(d.handle_message(_dm("/new")))
        assert cli.sent == [(self._RELEASE, None)]
        assert d._conv.current_gen(self._ROUTE) == 0

    def test_compact_replies(self) -> None:
        def _run(prep: Any) -> tuple[Any, Any, Any]:
            d, cli, sess = _dispatcher({7})
            prep(d, cli, sess)
            asyncio.run(d.handle_message(_dm("/compact")))
            return d, cli, sess

        _d, cli, sess = _run(lambda d, cli, sess: setattr(sess, "_busy", True))
        assert cli.sent == [
            ("⏳ Still working on your last message — try /compact once it finishes.", None)
        ]
        _d, cli, sess = _run(lambda d, cli, sess: setattr(sess, "_has", False))
        assert cli.sent == [("No active session to compact.", None)] and sess.acquired == []
        _d, cli, sess = _run(lambda d, cli, sess: setattr(sess, "get_provider", lambda k: None))
        assert cli.sent == [("No active session to compact.", None)]
        assert sess.released == [self._KEY]
        _d, cli, sess = _run(lambda d, cli, sess: None)
        assert cli.sent[0][0] == "🔄 Compacting context…"
        assert cli.edits[-1] == (101, "✅ Context compacted.", None)

        for result, text in (
            ({"type": "failed", "summary": "disk"}, "❌ Compaction failed: disk"),
            ({"type": "failed"}, "❌ Compaction failed."),
            ({"type": "timeout"}, "⚠️ Compaction timed out."),
        ):

            async def _wait(timeout: float = 0.0, result: dict = result) -> dict:
                return result

            _d, cli, sess = _run(
                lambda d, cli, sess, w=_wait: setattr(sess._gp, "wait_for_compaction", w)
            )
            assert cli.edits[-1][1] == text

        async def _boom() -> None:
            raise RuntimeError("wedged")

        _d, cli, sess = _run(lambda d, cli, sess: setattr(sess._gp, "compact", _boom))
        assert cli.edits[-1][1] == "❌ Compaction failed unexpectedly."
        assert sess.discarded == [self._KEY] and sess.destroyed == []

        def _no_status(d: Any, cli: Any, sess: Any) -> None:
            real_send = cli.send_message

            async def _send(chat_id: int, text: str, **kw: Any) -> Any:
                if text.startswith("🔄"):
                    return None
                return await real_send(chat_id, text, **kw)

            cli.send_message = _send

        _d, cli, sess = _run(_no_status)
        assert cli.sent[-1][0] == "✅ Context compacted." and cli.edits == []

    def test_link_and_unlink_replies(self) -> None:
        from kiro_crew.messaging.session_resume import ResumeReleaseError

        d, cli, sess = _dispatcher({7})
        asyncio.run(d._handle_link(self._ROUTE, 7, resumed_key="dashboard:x"))
        assert cli.sent == [("⚠️ A resumed session is active here. Send /unlink first.", None)]
        assert sess.mirror_links == {}

        d, cli, sess = _dispatcher({7})
        d._session_resume.leave_resumed_session = AsyncMock(  # type: ignore[method-assign]
            return_value="dashboard:chat-1"
        )
        asyncio.run(d._handle_unlink(self._ROUTE, 7))
        assert cli.sent == [
            ("✅ Left the resumed session. Back to your Telegram conversation.", None)
        ]
        assert sess.mirror_opt_outs == set()

        d, cli, sess = _dispatcher({7})
        d._session_resume.leave_resumed_session = AsyncMock(  # type: ignore[method-assign]
            side_effect=ResumeReleaseError("x")
        )
        asyncio.run(d._handle_unlink(self._ROUTE, 7))
        assert cli.sent == [(self._RELEASE, None)] and sess.mirror_opt_outs == set()

    def test_help_ping_and_status_replies(self, monkeypatch: Any) -> None:
        d, cli, _sess = _dispatcher({7})
        asyncio.run(d.handle_message(_dm("/help")))
        asyncio.run(d.handle_message(_dm("/ping")))
        assert cli.sent == [(build_help_text(), None), ("pong", None)]

        seen: list[str] = []
        recorder = SimpleNamespace(
            inc_message_received=lambda: seen.append("received"), summary=lambda: "S-42"
        )
        monkeypatch.setattr("kiro_crew.telegram.transport_dispatch.Stats", lambda: recorder)
        d, cli, _sess = _dispatcher({7})
        asyncio.run(d.handle_message(_dm("/status")))
        assert cli.sent == [("S-42", None)] and seen == ["received"]

    def test_stop_replies(self) -> None:
        d, cli, sess = _dispatcher({7})
        sess._busy = True
        asyncio.run(d.handle_message(_dm("/stop")))
        assert cli.sent == [("🛑 Stopped.", None)]

        d, cli, sess = _dispatcher({7})
        asyncio.run(d.handle_message(_dm("/stop")))
        assert cli.sent == [("🛑 Nothing was running — queue cleared.", None)]
        assert sess._gp.cancelled == 0

        d, cli, sess = _dispatcher({7})
        captured: list[tuple[str, Any]] = []
        sess.clear_queue = lambda key, owned_by=None: captured.append((key, owned_by))  # type: ignore[method-assign]
        asyncio.run(d.handle_message(_dm("/stop")))
        key, owned = captured[0]
        assert key == self._KEY
        assert owned(_origin(7, 7)) is True and owned(_origin(8, 8)) is False

    def test_a_topic_stop_answers_in_its_topic(self) -> None:
        d, cli, sess = _dispatcher({7}, allow_forum=True, allowed_forum_chat_ids=[-100999])
        sess._busy = True
        asyncio.run(
            d.handle_message(
                TelegramInboundMessage(
                    channel_type="telegram",
                    user_id="7",
                    conversation_id="-100999",
                    text="/stop",
                    thread_id="4",
                    chat_type="supergroup",
                    message_id=5,
                )
            )
        )
        assert len(cli.sent) == 1 and cli.send_threads == [4]

    def test_bare_directives_and_override_payloads(self) -> None:
        from kiro_crew.messaging import privacy_mode

        usage = "Those take a message: /queue <msg> or /steer <msg>."
        d, cli, sess = _dispatcher({7})
        asyncio.run(d.handle_message(_dm("/queue")))
        assert cli.sent == [(usage, None)]
        d, cli, sess = _dispatcher({7})
        d.bot_username = "KiroCrewBot"
        asyncio.run(d.handle_message(_dm("/steer@KiroCrewBot")))
        assert cli.sent == [(usage, None)]

        d, cli, sess = _dispatcher({7})
        asyncio.run(d.handle_message(_dm("/queue /new")))
        assert d._conv.current_gen(self._ROUTE) == 0
        assert cli.final_text() == "Answer: /new"
        d, cli, sess = _dispatcher({7})
        sess._busy = True
        asyncio.run(d.handle_message(_dm("/queue /new")))
        assert sess.queued[0][1] == "/new" and d._conv.current_gen(self._ROUTE) == 0

        privacy_mode.reset()
        try:
            d, cli, sess = _dispatcher({7})
            asyncio.run(d.handle_message(_dm("/temporary /queue")))
            assert cli.sent[-1][0] == usage
            assert not privacy_mode.is_restricted(self._KEY) and sess.successes == []
        finally:
            privacy_mode.reset()
