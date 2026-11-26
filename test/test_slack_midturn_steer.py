"""A Slack message sent while a turn runs steers it by default; ``!queue`` still queues.

Covers ``kiro_crew.slack.midturn`` through the doors that reach it: ``_route_message``
for the thread's own Slack session, ``handle_message`` for a thread linked to a
dashboard chat, and the slot's shielded steer-and-queue hand-off
(``channel_handoff.hand_to_resumed_slot``).
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import re
import textwrap
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from chat_test_helpers import _make_state
from discord_resumed_busy_helpers import _busy, _steerable
from slack_events_coverage_helpers import _fresh_trusted_bot_turn_ledger  # noqa: F401
from slack_events_coverage_helpers import _isolated_home_fixture  # noqa: F401
from slack_events_coverage_helpers import _mock_sel_fixture  # noqa: F401
from slack_events_coverage_helpers import _drain, _event, _make_orch

from kiro_crew.config.service_sections import MessagingConfig
from kiro_crew.dashboard import channel_handoff as ch
from kiro_crew.hook_runtime.hook_types import HookResult
from kiro_crew.slack import events as ev
from kiro_crew.slack import midturn

#: The ⏳ the thread's own Slack queue puts on a queued message (``slack/events.py``).
_QUEUE_REACTION = "hourglass_flowing_sand"

# ── the prefix parser ──


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("!queue do it later", ("queue", "do it later")),
        ("!STEER  also check b", ("steer", "also check b")),
        ("  !queue\nline two", ("queue", "line two")),
        ("!queue", (None, "!queue")),
        ("queue this", (None, "queue this")),
        ("!queued thing", (None, "!queued thing")),
    ],
)
def test_the_prefix_is_parsed_only_with_a_body(text, expected):
    assert midturn.parse_override(text) == expected


def test_a_lone_prefix_is_bare():
    assert midturn.is_bare_override(" !queue ")
    assert midturn.is_bare_override("!Steer")
    assert not midturn.is_bare_override("!queue now")
    assert not midturn.is_bare_override("hello")


def test_the_mode_follows_the_override_then_the_config():
    cfg = MagicMock(messaging=MessagingConfig())
    assert midturn.resolve_mode(cfg, None) == "steer"
    assert midturn.resolve_mode(cfg, "queue") == "queue"
    cfg.messaging = MessagingConfig(queue_mode="queue")
    assert midturn.resolve_mode(cfg, None) == "queue"
    assert midturn.resolve_mode(cfg, "steer") == "steer"


# ── answered_before_a_turn kept in step with what a turn's start answers ──
#
# Each case below is read off the real source (a command table, the mode table, an
# exact-text shortcut) or checked against the real matcher, so a command added there
# and not to ``answered_before_a_turn`` fails here instead of being steered as a prompt.


def _function_ast(fn: Any) -> ast.AST:
    return ast.parse(textwrap.dedent(inspect.getsource(fn)))


def _bang_words() -> set[str]:
    """Every ``!`` word the native router names: its table keys and its own literals."""
    from kiro_crew.slack import handler
    from kiro_crew.slack.handler_runtime import commands

    table: set[str] = set()
    for node in ast.walk(_function_ast(commands._handle_slash_command)):
        if isinstance(node, ast.Dict):
            table |= {k.value for k in node.keys if isinstance(k, ast.Constant)}
    assert "!stop" in table and len(table) >= 10, table  # the table was found
    literals = {
        node.value
        for node in ast.walk(_function_ast(commands._route_bang_command))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and re.fullmatch(r"![a-z][\w-]*", node.value)
    }
    return table | literals | set(handler._BANG_TO_SLASH) | set(handler._NATIVE_BANG_COMMANDS)


def _exact_text_shortcuts() -> set[str]:
    """Literals a pre-turn step compares the whole lower-cased message against."""
    from kiro_crew.slack import transport_dispatch
    from kiro_crew.slack.handler_runtime import dispatch

    def lowered(node: ast.AST) -> bool:
        return (isinstance(node, ast.Name) and node.id == "_lower") or (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "lower"
        )

    found: set[str] = set()
    for fn in (dispatch._dispatch_before_turn, transport_dispatch.handle_message_transport):
        for node in ast.walk(_function_ast(fn)):
            if not (isinstance(node, ast.Compare) and lowered(node.left)):
                continue
            for op, right in zip(node.ops, node.comparators):
                if isinstance(op, ast.Eq) and isinstance(right, ast.Constant):
                    found.add(right.value)
                elif isinstance(op, ast.In) and isinstance(right, (ast.Tuple, ast.Set)):
                    found |= {e.value for e in right.elts if isinstance(e, ast.Constant)}
    assert {"status", "ping"} <= found, found  # both pre-turn paths were read
    return found


@pytest.mark.parametrize("form", ["{w}", "{w} some args", "<@UBOT> {w} x", "  {w}\n"])
def test_every_bang_command_the_router_names_is_answered_before_a_turn(form):
    for word in sorted(_bang_words()):
        assert midturn.answered_before_a_turn(form.format(w=word), None), word


@pytest.mark.parametrize(
    "form", ["{t}", "{t} what did we decide", "what did we decide {t}", "<@UBOT> {T} hi"]
)
def test_every_privacy_mode_token_is_answered_before_a_turn(form):
    from kiro_crew.messaging import privacy_mode

    for mode, pattern in privacy_mode._MODES:
        token = f"!{mode}"
        assert pattern.search(token), (mode, pattern)  # the token the mode table matches
        text = form.format(t=token, T=token.upper())
        assert privacy_mode.strip_tokens(text)[1] == (mode,)
        assert midturn.answered_before_a_turn(text, None), text


def test_every_exact_text_shortcut_is_answered_before_a_turn():
    for word in sorted(_exact_text_shortcuts()):
        for text in (word, f"  {word.upper()} \t"):
            assert midturn.answered_before_a_turn(text, None), text


def test_every_sessions_keyword_form_is_answered_before_a_turn():
    from kiro_crew.slack.handler_runtime.commands import _is_sessions_keyword
    from kiro_crew.slack.sessions_view import SESSIONS_INCLUDE_ENDED_ARGS

    for text in ["sessions", *(f"Sessions {a}" for a in sorted(SESSIONS_INCLUDE_ENDED_ARGS))]:
        assert _is_sessions_keyword(text), text
        assert midturn.answered_before_a_turn(text, None), text


async def _keyword_reply(text: str) -> str | None:
    """What the real spawn / task / cron matchers answer, with doubles that do no I/O."""
    from kiro_crew.messaging import commands as mc

    manager = SimpleNamespace(running=[], max_concurrent=1, spawn=lambda task, **kw: None)
    runner = SimpleNamespace(running=False, status=dict, cancel=lambda: None)
    cron = SimpleNamespace(
        remove_job_async=AsyncMock(return_value=False),
        enable_job_async=AsyncMock(return_value=False),
    )
    return (
        await mc.spawn_command_reply(text, manager)
        or await mc.task_command_reply(text, runner)
        or await mc.cron_command_reply(text, cron)
    )


# ``cron list`` and ``cron remove all`` are left out: their replies read the cron run
# markers and the store from disk. ``answered_before_a_turn`` takes any two-word ``cron``.
@pytest.mark.parametrize(
    "text",
    [
        "spawn write the release notes",
        "SPAWN list",
        "bg status",
        "bg run the linter",
        "task run status",
        "Task Run cancel",
        "project run status",
        "cron remove j1",
        "cron pause j1",
        "CRON resume j1",
    ],
)
@pytest.mark.asyncio
async def test_a_keyword_command_the_real_matcher_answers_is_answered_before_a_turn(text):
    assert await _keyword_reply(text), text  # the sample cannot drift from the matcher
    assert midturn.answered_before_a_turn(text, None)


def test_the_agent_switch_is_answered_before_a_turn():
    from kiro_crew.agent_switch_command import agent_switch_target

    for text in ("/agent kirocrew", "<@UBOT> /agent kirocrew"):
        assert agent_switch_target(text.removeprefix("<@UBOT> ")) == "kirocrew"
        assert midturn.answered_before_a_turn(text, None), text


@pytest.mark.parametrize(
    "text",
    [
        "also check the tests",
        "please run the build",
        "cronjob is broken",
        "spawning is slow",
        "the status page is down",
        "ping me when it lands",
        "my sessions keep ending",
        "the task run failed",
        "background it",
        "use src/main.py or a/b",
    ],
)
@pytest.mark.asyncio
async def test_ordinary_chat_is_still_steered(text):
    from kiro_crew.messaging import privacy_mode

    assert await _keyword_reply(text) is None
    assert not privacy_mode.strip_tokens(text)[1]
    assert not midturn.answered_before_a_turn(text, None)


# ── the thread's own Slack session (_route_message) ──


def _provider(accepted: bool = True, active: bool = True) -> MagicMock:
    provider = MagicMock()
    provider.supports_steer = True
    provider.has_active_turn = MagicMock(return_value=active)
    provider.steer = AsyncMock(return_value=accepted)
    return provider


def _busy_orch(
    provider: MagicMock | None, queue_mode: str = "steer", opener: str = "U_OWNER"
) -> MagicMock:
    """A thread session whose running turn *opener*'s message started.

    The turn has written its question under ``slack:100.0``, as a transport turn does.
    """
    orch = _make_orch()
    orch._cfg.messaging = MessagingConfig(use_transport=False, queue_mode=queue_mode)
    orch._session_tasks = {}
    task = MagicMock()
    ev._track_router_task(orch, "100.0", task, None, opener)
    midturn._transcripts[task] = "slack:100.0"
    orch.sessions.enqueue = MagicMock(return_value=True)
    orch.sessions.has_queued = MagicMock(return_value=False)
    orch.sessions.get_provider = MagicMock(return_value=provider)
    return orch


async def _route(orch: MagicMock, text: str, *, thread_ts: str | None = None) -> AsyncMock:
    with (
        patch("kiro_crew.slack.events.is_allowed_user", return_value=True),
        patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm,
    ):
        await ev._route_message(orch, _event(text=text, thread_ts=thread_ts), ev.SeenCache())
        await _drain(orch)
    return hm


def _reactions(orch: MagicMock) -> list[str]:
    return [c.args[2] for c in orch.slack.add_reaction.await_args_list]


class TestNativeSession:
    @pytest.mark.parametrize("activation", ["review", "always", "mention", "observe"])
    @pytest.mark.parametrize("text", ["my secret", "!steer my secret"])
    @pytest.mark.asyncio
    async def test_channel_review_queues_but_other_active_thread_modes_steer(
        self, activation, text
    ):
        provider = _provider()
        orch = _busy_orch(provider)
        orch._cfg.slack_dm_activation = activation
        orch.sessions.has_session.return_value = True
        hm = await _route(orch, text, thread_ts="100.0")
        hm.assert_not_called()
        if activation == "review":
            provider.steer.assert_not_called()
            assert orch.sessions.enqueue.call_args.args[2] == "my secret"
            assert _reactions(orch) == [_QUEUE_REACTION]
        else:
            provider.steer.assert_awaited_once_with("my secret")
            orch.sessions.enqueue.assert_not_called()
            assert _reactions(orch) == [midturn.STEER_REACTION]

    @pytest.mark.parametrize("queue", ["session", "pre-session"])
    @pytest.mark.parametrize("text", ["my secret", "!steer my secret"])
    @pytest.mark.asyncio
    async def test_a_message_queues_behind_an_earlier_privacy_command(self, queue, text):
        provider = _provider()
        orch = _busy_orch(provider)
        entries = []
        if queue == "session":
            orch.sessions.enqueue.side_effect = lambda key, ts, body, **kw: (
                entries.append((ts, body, kw)) or True
            )
            orch.sessions.has_queued.side_effect = lambda key: bool(entries)
        else:
            orch.sessions.enqueue.return_value = False
        await _route(orch, "!incognito")
        await _route(orch, text)
        if queue == "pre-session":
            entries = orch._pending_queue["100.0"]
        assert [entry[1] for entry in entries] == ["!incognito", "my secret"]
        provider.steer.assert_not_called()
        assert _reactions(orch) == [_QUEUE_REACTION, _QUEUE_REACTION]

    @pytest.mark.asyncio
    async def test_a_message_to_a_busy_session_steers_by_default(self):
        provider = _provider()
        orch = _busy_orch(provider)
        hm = await _route(orch, "also check the tests")
        provider.steer.assert_awaited_once_with("also check the tests")
        orch.sessions.enqueue.assert_not_called()
        hm.assert_not_called()
        assert _reactions(orch) == [midturn.STEER_REACTION]

    @pytest.mark.parametrize("opener", ["U_SHARED", ""], ids=["shared-user", "unknown"])
    @pytest.mark.asyncio
    async def test_a_turn_a_shared_user_or_nobody_known_opened_takes_no_steer(self, opener):
        """Its tool prompts answer to its opener, so another person's words are queued."""
        from kiro_crew.slack import handler

        provider = _provider()
        orch = _busy_orch(provider, opener=opener)
        with patch.object(handler, "is_shared_listed", side_effect=lambda uid: uid == "U_SHARED"):
            await _route(orch, "also delete the old branch")
        provider.steer.assert_not_called()
        assert orch.sessions.enqueue.call_args.args[2] == "also delete the old branch"
        assert _reactions(orch) == [_QUEUE_REACTION]

    @pytest.mark.parametrize("drained", [False, True], ids=["typed", "drained-from-the-queue"])
    @pytest.mark.asyncio
    async def test_the_owner_is_not_steered_into_a_turn_a_shared_users_message_runs(self, drained):
        """Through the real route: the router names the sender of the turn it starts."""
        from kiro_crew.slack import handler

        provider = _provider()
        orch = _busy_orch(provider)
        orch._session_tasks = {}
        release = asyncio.Event()

        async def running_turn(*_a: Any, **_k: Any) -> None:
            await release.wait()

        def shared(uid: str) -> bool:
            return uid == "U_SHARED"

        with (
            patch.object(ev, "is_allowed_user", return_value=True),
            patch.object(ev, "is_shared_listed", side_effect=shared),
            patch.object(handler, "is_shared_listed", side_effect=shared),
            patch.object(ev, "handle_message", side_effect=running_turn),
            patch.object(ev, "_dispatch_queued", side_effect=running_turn),
        ):
            if drained:
                entries = [("100.0", "start", {"thread_ts": None, "sender_id": "U_SHARED"})]
                orch.sessions.dequeue = MagicMock(
                    side_effect=lambda _key: entries.pop() if entries else None
                )
                await ev._drain_slack_queue(orch, "100.0")
            else:
                orch.sessions.enqueue = MagicMock(side_effect=[False, True])
                await ev._route_message(orch, _event(text="start", user="U_SHARED"), ev.SeenCache())
            assert ev.router_task_opener(orch._session_tasks["100.0"]) == "U_SHARED"
            orch.sessions.enqueue = MagicMock(return_value=True)
            await ev._route_message(
                orch,
                _event(text="also delete the old branch", ts="101.0", thread_ts="100.0"),
                ev.SeenCache(),
            )
            provider.steer.assert_not_called()
            assert orch.sessions.enqueue.call_args.args[2] == "also delete the old branch"
            release.set()
            await _drain(orch)

    @pytest.mark.asyncio
    async def test_a_turn_with_no_open_transcript_takes_no_steer(self):
        """The native handler writes question and reply together at its end."""
        provider = _provider()
        orch = _busy_orch(provider)
        midturn._transcripts.pop(orch._session_tasks["100.0"])
        await _route(orch, "also check the tests")
        provider.steer.assert_not_called()
        assert orch.sessions.enqueue.call_args.args[2] == "also check the tests"
        assert _reactions(orch) == [_QUEUE_REACTION]

    @pytest.mark.parametrize("restricted", [False, True], ids=["plain", "privacy-mode"])
    @pytest.mark.asyncio
    async def test_a_landed_steer_is_written_to_the_turns_transcript(self, tmp_path, restricted):
        from kiro_crew.history import ConversationLog
        from kiro_crew.slack import handler

        provider = _provider()
        orch = _busy_orch(provider)
        orch.conv_log = ConversationLog(base_dir=tmp_path)
        with patch.object(handler, "_is_slack_restricted", return_value=restricted):
            await _route(orch, "!steer also check the tests")
        provider.steer.assert_awaited_once_with("also check the tests")
        rows = orch.conv_log.read_messages("slack:100.0")
        written = [(r["role"], r["content"], r.get("source_user")) for r in rows]
        assert written == ([] if restricted else [("user", "also check the tests", "U_OWNER")])

    @pytest.mark.asyncio
    async def test_a_steer_that_does_not_land_writes_no_row(self, tmp_path):
        from kiro_crew.history import ConversationLog

        orch = _busy_orch(_provider(accepted=False))
        orch.conv_log = ConversationLog(base_dir=tmp_path)
        await _route(orch, "also check the tests")
        assert orch.conv_log.read_messages("slack:100.0") == []
        assert orch.sessions.enqueue.call_args.args[2] == "also check the tests"

    @pytest.mark.asyncio
    async def test_a_real_transport_turn_keeps_a_steer_between_its_question_and_reply(
        self, monkeypatch, tmp_path
    ):
        from slack_transport_dispatch_helpers import (
            _MSG_TS,
            RecordingSlackClient,
            ScriptedProvider,
            _CapturingSessions,
            make_event,
        )

        from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, STOP_REASON_END_TURN
        from kiro_crew.history import ConversationLog
        from kiro_crew.messaging.link import canonical_key
        from kiro_crew.slack import transport_dispatch

        monkeypatch.setattr(
            transport_dispatch, "_hydrate_thread_overrides", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_thread_agents", {})
        orch = _busy_orch(None)
        orch._session_tasks = {}
        orch.conv_log = ConversationLog(base_dir=tmp_path)
        steers: list[str] = []
        landed: list[bool] = []

        class _SteeredMidStream(ScriptedProvider):
            supports_steer = True

            def has_active_turn(self) -> bool:
                return True

            async def steer(self, text: str) -> bool:
                steers.append(text)
                return True

            async def stream(self, message: str):
                self.stream_calls += 1
                if self.stream_calls > 1:
                    return
                yield make_event(EVENT_TEXT_CHUNK, text="first, ")
                midturn.set_inbound(midturn.Inbound("101.0", None, True, _MSG_TS, "U_OWNER"))
                landed.append(
                    await midturn.steer_if_busy(
                        orch,
                        _MSG_TS,
                        "also check the tests",
                        channel="D1",
                        msg_ts="101.0",
                        task_busy=True,
                    )
                )
                yield make_event(EVENT_TEXT_CHUNK, text="then the tests")
                yield make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        provider = _SteeredMidStream([])
        orch.sessions.get_provider = MagicMock(return_value=provider)
        turn = asyncio.create_task(
            transport_dispatch.handle_message_transport(
                slack=RecordingSlackClient(),
                sessions=_CapturingSessions(provider),
                channel="D1",
                text="hello",
                thread_ts=None,
                msg_ts=_MSG_TS,
                user_id="U_OWNER",
                context_builder=None,
                conversation_log=orch.conv_log,
                agent_override=None,
            )
        )
        ev._track_router_task(orch, _MSG_TS, turn, None, "U_OWNER")
        await asyncio.wait_for(turn, timeout=30)
        assert landed == [True] and steers == ["also check the tests"]
        rows = orch.conv_log.read_messages(canonical_key(_MSG_TS))
        assert [(r["role"], r["content"]) for r in rows] == [
            ("user", "hello"),
            ("user", "also check the tests"),
            ("assistant", "first, then the tests"),
        ]

    @pytest.mark.asyncio
    async def test_a_queue_prefix_queues_without_its_prefix(self):
        provider = _provider()
        orch = _busy_orch(provider)
        await _route(orch, "!queue do this after")
        provider.steer.assert_not_called()
        assert orch.sessions.enqueue.call_args.args[2] == "do this after"
        assert _reactions(orch) == [_QUEUE_REACTION]

    @pytest.mark.asyncio
    async def test_queue_mode_in_config_queues(self):
        provider = _provider()
        orch = _busy_orch(provider, queue_mode="queue")
        await _route(orch, "hello")
        provider.steer.assert_not_called()
        assert _reactions(orch) == [_QUEUE_REACTION]

    @pytest.mark.asyncio
    async def test_a_steer_prefix_overrides_queue_mode(self):
        provider = _provider()
        orch = _busy_orch(provider, queue_mode="queue")
        await _route(orch, "!steer now please")
        provider.steer.assert_awaited_once_with("now please")
        orch.sessions.enqueue.assert_not_called()

    @pytest.mark.parametrize(
        "provider",
        [
            pytest.param(None, id="no-provider"),
            pytest.param(_provider(accepted=False), id="declined"),
            pytest.param(_provider(active=False), id="turn-not-in-flight"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_steer_that_cannot_land_is_queued(self, provider):
        orch = _busy_orch(provider)
        await _route(orch, "hello")
        assert orch.sessions.enqueue.call_args.args[2] == "hello"
        assert _reactions(orch) == [_QUEUE_REACTION]

    @pytest.mark.asyncio
    async def test_a_steer_that_raises_is_queued(self):
        provider = _provider()
        provider.steer = AsyncMock(side_effect=RuntimeError("pipe closed"))
        orch = _busy_orch(provider)
        await _route(orch, "hello")
        assert orch.sessions.enqueue.call_args.args[2] == "hello"

    @pytest.mark.parametrize("turn_ended", [False, True], ids=["busy", "turn-ended"])
    @pytest.mark.parametrize("use_transport", [False, True], ids=["native", "transport"])
    @pytest.mark.asyncio
    async def test_an_ambiguously_delivered_steer_is_not_replayed(
        self, turn_ended, use_transport, caplog
    ):
        from kiro_crew.acp.transport_errors import AcpProcessDied

        provider = _provider()
        orch = _busy_orch(provider)
        orch._cfg.messaging.use_transport = use_transport

        async def ambiguous_steer(text):
            if turn_ended:
                orch._session_tasks.clear()
                orch.sessions.enqueue.return_value = False
            raise AcpProcessDied("stdin drain stalled", ambiguous_delivery=True)

        provider.steer.side_effect = ambiguous_steer
        entry = "handle_message_transport" if use_transport else "handle_message"
        with (
            patch.object(ev, "is_allowed_user", return_value=True),
            patch.object(ev, entry, new_callable=AsyncMock) as hm,
        ):
            await ev._route_message(orch, _event(text="hello"), ev.SeenCache())
            provider.steer.assert_awaited_once_with("hello")
            orch.sessions.enqueue.assert_not_called()
            assert orch._pending_queue == {}
            assert orch._handler_tasks == set()
            hm.assert_not_called()
        assert _reactions(orch) == []
        assert "may have been delivered; not replaying" in caplog.text

    @pytest.mark.parametrize("raises", [False, True], ids=["declined", "raised"])
    @pytest.mark.parametrize("use_transport", [False, True], ids=["native", "transport"])
    @pytest.mark.asyncio
    async def test_a_failed_steer_whose_turn_ended_starts_a_new_turn(self, raises, use_transport):
        from kiro_crew.slack import events_queue

        provider = _provider()
        orch = _busy_orch(provider)
        orch._cfg.messaging.use_transport = use_transport
        orch.sessions.enqueue.return_value = False

        async def end_turn_then_decline(text):
            del orch._session_tasks["100.0"]
            await events_queue._drain_slack_queue(orch, "100.0")
            if raises:
                raise RuntimeError("pipe closed")
            return False

        provider.steer.side_effect = end_turn_then_decline
        entry = "handle_message_transport" if use_transport else "handle_message"
        with (
            patch.object(ev, "is_allowed_user", return_value=True),
            patch.object(ev, entry, new_callable=AsyncMock) as hm,
        ):
            await ev._route_message(orch, _event(text="hello"), ev.SeenCache())
            provider.steer.assert_awaited_once_with("hello")
            orch.sessions.enqueue.assert_called_once()
            assert not orch.sessions.enqueue.call_args.kwargs.get("force", False)
            assert orch._pending_queue == {}
            assert _QUEUE_REACTION not in _reactions(orch)
            await asyncio.wait_for(asyncio.gather(*orch._handler_tasks), timeout=30)
            hm.assert_awaited_once()
            assert hm.await_args.args[3] == "hello"

    @pytest.mark.parametrize(
        "text",
        [
            "!incognito my secret",
            "keep this out of memory !incognito",
            "!temporary",
            "!compact",
            "<@UBOT> !yolo off",
            "status",
            "sessions",
            "spawn look into it",
            "cron list",
            "cron\tlist",
            "/agent kirocrew",
            "/compact",
            "/tools trust-all",
            "<@UBOT> /model x",
        ],
    )
    @pytest.mark.asyncio
    async def test_a_command_or_privacy_modifier_is_queued_for_its_handler(self, text):
        provider = _provider()
        orch = _busy_orch(provider)
        await _route(orch, text)
        provider.steer.assert_not_called()
        assert orch.sessions.enqueue.call_args.args[2] == text
        assert _reactions(orch) == [_QUEUE_REACTION]

    @pytest.mark.parametrize("text", ["ping", "  PiNG \t"])
    @pytest.mark.asyncio
    async def test_transport_ping_without_a_hook_is_queued_for_its_pong(self, text):
        from kiro_crew.slack import events_queue, transport_dispatch

        provider = _provider()
        orch = _busy_orch(provider)
        orch._cfg.messaging.use_transport = True
        orch.sessions.reserve_inbound_callback = None
        with (
            patch.object(ev, "is_allowed_user", return_value=True),
            patch.object(
                ev, "handle_message_transport", wraps=transport_dispatch.handle_message_transport
            ) as hm,
        ):
            await ev._route_message(orch, _event(text=text), ev.SeenCache())
            provider.steer.assert_not_called()
            call = orch.sessions.enqueue.call_args
            assert call.args[2] == text
            assert _reactions(orch) == [_QUEUE_REACTION]
            await events_queue._dispatch_queued(
                orch, "100.0", call.args[1], call.args[2], call.kwargs, slack=orch.slack
            )
        hm.assert_awaited_once()
        orch.slack.post_message.assert_awaited_once_with("D1", "pong", "100.0")
        orch.sessions.get_or_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_message_a_hook_answers_is_queued(self):
        provider = _provider()
        orch = _busy_orch(provider)
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks.on_message = MagicMock(return_value=HookResult.reply("pong"))
        await _route(orch, "ping")
        provider.steer.assert_not_called()
        assert orch.sessions.enqueue.call_args.args[2] == "ping"

    @pytest.mark.asyncio
    async def test_a_message_during_a_settling_stop_is_queued(self):
        provider = _provider()
        orch = _busy_orch(provider)
        with patch.dict(ev._stops_in_flight.setdefault(orch, {}), {"100.0": MagicMock()}):
            await _route(orch, "one more thing")
        provider.steer.assert_not_called()
        assert orch.sessions.enqueue.call_args.args[2] == "one more thing"

    @pytest.mark.asyncio
    async def test_a_thread_a_dashboard_chat_owns_is_not_steered_into_the_dm_turn(self):
        provider = _provider()
        orch = _busy_orch(provider)
        orch.sessions.get_session_for_thread = MagicMock(return_value="dashboard:chat-1")
        await _route(orch, "for the linked chat")
        provider.steer.assert_not_called()
        assert orch.sessions.enqueue.call_args.args[2] == "for the linked chat"

    @pytest.mark.asyncio
    async def test_a_bot_thread_steers_its_running_turn(self):
        # A ``!bot <name>`` thread keeps its Slack-born session; only its agent changes.
        from kiro_crew.slack import handler

        provider = _provider()
        orch = _busy_orch(provider)
        with patch.dict(handler._thread_agents, {"100.0": "PenTest"}):
            hm = await _route(orch, "also check the tests")
        provider.steer.assert_awaited_once_with("also check the tests")
        orch.sessions.enqueue.assert_not_called()
        hm.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_idle_session_starts_a_turn_and_steers_nothing(self):
        provider = _provider()
        orch = _busy_orch(provider)
        orch._session_tasks = {}
        orch.sessions.enqueue = MagicMock(return_value=False)
        orch.sessions.is_busy = MagicMock(return_value=False)
        orch.sessions.get_session_for_thread = MagicMock(return_value=None)
        hm = await _route(orch, "!steer start here")
        provider.steer.assert_not_called()
        assert midturn.STEER_REACTION not in _reactions(orch)
        hm.assert_awaited_once()
        assert "start here" in hm.await_args.args and "!steer start here" not in hm.await_args.args

    @pytest.mark.asyncio
    async def test_a_lone_prefix_is_answered_and_runs_nothing(self):
        provider = _provider()
        orch = _busy_orch(provider)
        hm = await _route(orch, "!queue")
        orch.slack.post_message.assert_awaited_once_with("D1", midturn.BARE_PREFIX_HINT, "100.0")
        provider.steer.assert_not_called()
        orch.sessions.enqueue.assert_not_called()
        hm.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_shared_user_is_never_steered(self):
        provider = _provider()
        orch = _busy_orch(provider)
        with patch("kiro_crew.slack.events.is_shared_listed", return_value=True):
            await midturn.take_override(orch, "D1", "100.0", "100.0", "U1", "hi", steerable=False)
        assert not await midturn.steer_if_busy(
            orch, "100.0", "hi", channel="D1", msg_ts="100.0", task_busy=True
        )
        provider.steer.assert_not_called()

    @pytest.mark.parametrize(
        ("text", "runs"),
        [
            ("!queue /tools trust-all", "/tools trust-all"),
            ("!steer /agent x", "/agent x"),
            ("!steer /model x", "/model x"),
            ("!queue /agent kirocrew", "/agent kirocrew"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_shared_users_prefixed_slash_command_is_judged_without_its_prefix(
        self, text, runs
    ):
        """The gate text (``spool_text``) is the text that runs, so the owner-only refusal holds."""
        from kiro_crew.slack import handler
        from kiro_crew.slack.shared_access import shared_command_refused

        orch = _busy_orch(_provider())
        orch._session_tasks = {}
        orch.sessions.enqueue = MagicMock(return_value=False)
        orch.sessions.is_busy = MagicMock(return_value=False)
        with (
            patch("kiro_crew.slack.events.is_shared_listed", return_value=True),
            patch.object(handler, "is_shared_listed", return_value=True),
        ):
            call = (await _route(orch, text)).await_args
            assert call is not None
            spool_text = call.kwargs["spool_text"]
            assert runs in call.args
            assert spool_text == runs
            assert shared_command_refused("U1", spool_text)

    @pytest.mark.asyncio
    async def test_a_message_without_a_prefix_keeps_its_gate_text(self):
        orch = _busy_orch(_provider())
        orch._session_tasks = {}
        orch.sessions.enqueue = MagicMock(return_value=False)
        orch.sessions.is_busy = MagicMock(return_value=False)
        orch.sessions.get_session_for_thread = MagicMock(return_value=None)
        call = (await _route(orch, "  hello  !queue x ")).await_args
        assert call is not None
        assert call.kwargs["spool_text"] == "  hello  !queue x "


_THREAD_A = "150.0"
_THREAD_B = "160.0"


def _flat_dm_orch(provider: MagicMock, running_in: str | None) -> MagicMock:
    """A single-session DM whose one flat key runs a turn started in *running_in* (None: root)."""
    orch = _busy_orch(provider)
    orch._cfg.messaging.use_transport = True
    orch._cfg.slack.dm_single_session = True
    orch._session_tasks = {}
    orch.sessions.get_session_for_thread = MagicMock(return_value=None)
    task = MagicMock()
    ev._track_router_task(orch, "slack:D1", task, running_in, "U_OWNER")
    midturn._transcripts[task] = "slack:D1"
    return orch


async def _route_dm(orch: MagicMock, thread_ts: str | None) -> AsyncMock:
    over: dict[str, Any] = {"text": "also check the tests", "ts": "200.0"}
    if thread_ts:
        over["thread_ts"] = thread_ts
    with (
        patch.object(ev, "is_allowed_user", return_value=True),
        patch.object(ev, "handle_message_transport", new_callable=AsyncMock) as hm,
    ):
        await ev._route_message(orch, _event(**over), ev.SeenCache())
        await _drain(orch)
    return hm


class TestSingleSessionDm:
    """Every DM thread shares the flat key; a steer lands only in the turn of its own thread."""

    @pytest.mark.parametrize(
        ("running_in", "sent_in"),
        [(_THREAD_A, _THREAD_A), (None, None)],
        ids=["same-thread", "both-at-root"],
    )
    @pytest.mark.asyncio
    async def test_a_message_where_the_running_turn_posts_steers(self, running_in, sent_in):
        provider = _provider()
        orch = _flat_dm_orch(provider, running_in)
        hm = await _route_dm(orch, sent_in)
        provider.steer.assert_awaited_once_with("also check the tests")
        orch.sessions.enqueue.assert_not_called()
        hm.assert_not_called()
        assert _reactions(orch) == [midturn.STEER_REACTION]

    @pytest.mark.parametrize(
        ("running_in", "sent_in"),
        [(_THREAD_A, _THREAD_B), (_THREAD_A, None), (None, _THREAD_B)],
        ids=["other-thread", "root-while-thread-runs", "thread-while-root-runs"],
    )
    @pytest.mark.asyncio
    async def test_a_message_addressed_elsewhere_is_queued_for_its_own_answer(
        self, running_in, sent_in
    ):
        provider = _provider()
        orch = _flat_dm_orch(provider, running_in)
        await _route_dm(orch, sent_in)
        provider.steer.assert_not_called()
        call = orch.sessions.enqueue.call_args
        assert call.args[0] == "slack:D1"
        assert call.kwargs["thread_ts"] == sent_in
        assert _reactions(orch) == [_QUEUE_REACTION]

    @pytest.mark.asyncio
    async def test_a_busy_flat_key_with_no_router_task_is_queued(self):
        provider = _provider()
        orch = _flat_dm_orch(provider, _THREAD_A)
        orch._session_tasks = {}
        orch.sessions.is_busy = MagicMock(return_value=True)
        await _route_dm(orch, _THREAD_A)
        provider.steer.assert_not_called()
        assert _reactions(orch) == [_QUEUE_REACTION]

    @pytest.mark.asyncio
    async def test_a_dashboard_owned_thread_is_queued_even_when_it_holds_the_turn(self):
        # Not subsumed by the thread rule: the running task can be this very thread's.
        provider = _provider()
        orch = _flat_dm_orch(provider, _THREAD_A)
        orch.sessions.get_session_for_thread = MagicMock(return_value="dashboard:chat-1")
        await _route_dm(orch, _THREAD_A)
        provider.steer.assert_not_called()
        assert _reactions(orch) == [_QUEUE_REACTION]


# ── a thread linked to a dashboard chat (route_linked_thread) ──


def _linked(running: bool = True) -> tuple[MagicMock, MagicMock]:
    slot = MagicMock()
    type(slot).running = PropertyMock(return_value=running)
    slot.key = "slot1"
    slot.is_closing = False
    slot.executor = "local"
    slot._queue = []

    def queue_append(
        content, *, meta=None, directive_user_origin, directive_channel_origin, source_channels=()
    ):
        slot._queue.append({"id": "q", "content": content})
        return "q"

    slot.queue_append = queue_append
    ds = MagicMock()
    ds.get_linked_slot = MagicMock(return_value=slot)
    ds._slots = {"slot1": slot}
    return slot, ds


async def _route_linked(ds, slack, inbound, outcome, text="hello"):
    from kiro_crew.slack import handler

    midturn.set_inbound(inbound)
    hand = (
        AsyncMock(wraps=ch.hand_to_resumed_slot)
        if outcome is None
        else AsyncMock(return_value=outcome)
    )
    with (
        patch.object(handler, "_dashboard_state", ds),
        patch.object(handler, "is_allowed_user", return_value=True),
        patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as run_chat,
        patch.object(ch, "hand_to_resumed_slot", hand),
    ):
        await handler.handle_message(slack, MagicMock(), "C1", text, "t1", "msg1", "U1")
    return hand, run_chat


def _slack() -> MagicMock:
    slack = MagicMock()
    slack.post_message = AsyncMock()
    slack.post_blocks = AsyncMock()
    slack.add_reaction = AsyncMock()
    return slack


#: The row meta a code channel's message carries (``slack_sender``).
_SENDER_META = {"slack_sender": {"id": "U1", "name": "Pat"}}


def _linked_real_slot(tmp_path) -> tuple[Any, Any]:
    """The real dashboard state, with ``slot1`` mid-turn and linked to ``t1`` in ``C1``."""
    ds = _make_state(tmp_path)
    slot = _busy(ds.get_or_create_slot("slot1"))
    ds.link_slack("slot1", "t1", "C1")
    slot._acp_client = _steerable(accepted=False)
    return ds, slot


async def _route_linked_real(ds, slack, *, user_meta=None):
    """``route_linked_thread`` for a typed ``hello`` in ``t1``, through the real hand-off."""
    from kiro_crew.slack import handler
    from kiro_crew.slack.handler_runtime.inbound import route_linked_thread

    midturn.set_inbound(midturn.Inbound("msg1", None, True))
    with (
        patch.object(handler, "_dashboard_state", ds),
        patch.object(handler, "is_allowed_user", return_value=True),
        patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as run_chat,
    ):
        routed = await route_linked_thread(
            "hello", "t1", "U1", "C1", slack, "t1", user_meta=user_meta
        )
    run_chat.assert_not_called()
    return routed


class _QueueSeenOnEntry(list):
    """A slot queue that records each entry's source channels as it joins the queue.

    What a write of the queue running on another thread would read at that moment.
    """

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[tuple[str, ...]] = []

    def _note(self, item: dict) -> None:
        from kiro_crew.dashboard.slot_queue_repository import source_channels_of

        self.seen.append(source_channels_of(item))

    def append(self, item: dict) -> None:
        self._note(item)
        super().append(item)

    def insert(self, index: Any, item: dict) -> None:
        self._note(item)
        super().insert(index, item)


def _assert_stamped_like_slack(entry: dict) -> None:
    """*entry* carries what Slack's own queue arm stamps: ``C1`` and the row meta."""
    from kiro_crew.dashboard.slot_queue_repository import SOURCE_CHANNELS_KEY, source_channels_of

    assert source_channels_of(entry) == ("C1",)
    assert entry["meta"]["slack_sender"] == _SENDER_META["slack_sender"]
    assert entry["meta"]["slack_sender"] is not _SENDER_META["slack_sender"]
    assert SOURCE_CHANNELS_KEY not in entry["meta"]


class TestLinkedThread:
    @pytest.mark.parametrize("activation", ["review", "always"])
    @pytest.mark.asyncio
    async def test_channel_review_does_not_steer_into_a_linked_slot(self, activation):
        from kiro_crew.slack import handler

        slot, ds = _linked()
        orch = _make_orch(dm_activation=activation)
        orch.sessions.reserve_inbound_callback = None
        orch.sessions.get_session_for_thread.return_value = "dashboard:slot1"
        with (
            patch.object(ev, "is_allowed_user", return_value=True),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "sel", return_value=MagicMock()),
            patch.object(ev, "handle_message", wraps=handler.handle_message),
            patch.object(ch, "hand_to_resumed_slot", new_callable=AsyncMock) as hand,
            patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as run_chat,
        ):
            hand.return_value = ch.ResumedBusyOutcome(ch.HANDOFF_STEERED)
            await ev._route_message(orch, _event(text="hello", thread_ts="100.0"), ev.SeenCache())
            await asyncio.wait_for(asyncio.gather(*orch._handler_tasks), timeout=30)
        run_chat.assert_not_called()
        if activation == "review":
            hand.assert_not_called()
            assert [entry["content"] for entry in slot._queue] == ["hello"]
        else:
            hand.assert_awaited_once()
            assert slot._queue == []

    @pytest.mark.asyncio
    async def test_a_linked_message_queues_behind_an_earlier_command(self):
        slot, ds = _linked()
        slot._queue.append({"id": "first", "content": "/agent kirocrew"})
        hand, run_chat = await _route_linked(
            ds, _slack(), midturn.Inbound("msg1", "steer", True), None, text="my secret"
        )
        hand.assert_not_called()
        run_chat.assert_not_called()
        assert [entry["content"] for entry in slot._queue] == ["/agent kirocrew", "my secret"]

    @pytest.mark.parametrize("use_transport", [False, True], ids=["native", "transport"])
    @pytest.mark.parametrize("linked", [False, True], ids=["slack-session", "dashboard-thread"])
    @pytest.mark.parametrize(
        ("text", "runs"),
        [
            ("!queue /tools trust-all", "/tools trust-all"),
            ("!steer /model x", "/model x"),
            ("!queue /agent kirocrew", "/agent kirocrew"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_shared_users_prefixed_command_is_refused_before_any_handoff(
        self, use_transport, linked, text, runs
    ):
        from kiro_crew.slack import handler, transport_dispatch
        from kiro_crew.slack.shared_access import SHARED_OWNER_ONLY_TEXT, shared_command_refused

        orch = _make_orch(use_transport=use_transport)
        orch.sessions.reserve_inbound_callback = None
        slot, ds = _linked()
        if linked:
            orch.sessions.get_session_for_thread.return_value = "dashboard:slot1"
        target = transport_dispatch if use_transport else handler
        entry = "handle_message_transport" if use_transport else "handle_message"
        with (
            patch.object(ev, "is_allowed_user", return_value=True),
            patch.object(ev, "is_shared_listed", return_value=True),
            patch.object(handler, "is_shared_listed", return_value=True),
            patch.object(handler, "_dashboard_state", ds if linked else None),
            patch.object(target, "sel", return_value=MagicMock()),
            patch.object(ev, entry, wraps=getattr(target, entry)) as hm,
            patch.object(ch, "hand_to_resumed_slot", new_callable=AsyncMock) as hand,
            patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as run_chat,
        ):
            await ev._route_message(
                orch, _event(text=text, thread_ts="100.0", user="U_SHARED"), ev.SeenCache()
            )
            await asyncio.gather(*orch._handler_tasks)
            hm.assert_awaited_once()
            assert hm.await_args.args[3] == runs
            assert hm.await_args.kwargs["spool_text"] == runs
            assert shared_command_refused("U_SHARED", hm.await_args.kwargs["spool_text"])
            orch.slack.post_message.assert_awaited_once_with("D1", SHARED_OWNER_ONLY_TEXT, "100.0")
            hand.assert_not_called()
            run_chat.assert_not_called()
            assert slot._queue == []
            orch.sessions.enqueue.assert_called_once()
            orch.sessions.get_or_create.assert_not_called()

    @pytest.mark.parametrize("use_transport", [False, True], ids=["native", "transport"])
    @pytest.mark.parametrize(
        ("text", "runs", "refusal"),
        [
            ("!queue !ta kirocrew", "!ta kirocrew", "⛔ Owner-only command."),
            ("!steer !yolo", "!yolo", "⛔ Owner-only command."),
            ("!queue !dashboard", "!dashboard", "⛔ Not authorized."),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_shared_users_prefixed_bang_command_is_refused_like_the_bare_one(
        self, use_transport, text, runs, refusal
    ):
        from kiro_crew.slack import handler, transport_dispatch

        orch = _make_orch(use_transport=use_transport)
        orch.sessions.reserve_inbound_callback = None
        target = transport_dispatch if use_transport else handler
        entry = "handle_message_transport" if use_transport else "handle_message"
        with (
            patch.object(ev, "is_allowed_user", return_value=True),
            patch.object(ev, "is_shared_listed", return_value=True),
            patch.object(handler, "is_shared_listed", return_value=True),
            patch.object(handler, "_dashboard_state", None),
            patch.object(handler, "sel", return_value=MagicMock()),
            patch.object(transport_dispatch, "sel", return_value=MagicMock()),
            patch.object(handler, "_handle_slash_command", new_callable=AsyncMock) as ran,
            patch.object(ev, entry, wraps=getattr(target, entry)) as hm,
        ):
            await ev._route_message(
                orch, _event(text=text, thread_ts="100.0", user="U_SHARED"), ev.SeenCache()
            )
            await asyncio.gather(*orch._handler_tasks)
            hm.assert_awaited_once()
            assert hm.await_args.args[3] == runs
            assert hm.await_args.kwargs["spool_text"] == runs
            orch.slack.post_message.assert_awaited_once_with("D1", refusal, "100.0")
            ran.assert_not_called()
            orch.sessions.get_or_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_typed_message_steers_the_running_slot(self):
        slot, ds = _linked()
        slack = _slack()
        hand, run_chat = await _route_linked(
            ds,
            slack,
            midturn.Inbound("msg1", None, True),
            ch.ResumedBusyOutcome(ch.HANDOFF_STEERED),
        )
        assert hand.await_args.args[1:3] == ("dashboard:slot1", "hello")
        assert slot._queue == []
        run_chat.assert_not_called()
        slack.add_reaction.assert_awaited_once_with("C1", "msg1", midturn.STEER_REACTION)

    @pytest.mark.asyncio
    async def test_a_declined_steer_is_queued_on_the_slot(self, tmp_path):
        from kiro_crew.slack import handler

        ds = _make_state(tmp_path)
        slot = _busy(ds.get_or_create_slot("slot1"))
        ds.link_slack("slot1", "t1", "C1")
        slot._acp_client = _steerable(accepted=False)
        slack = _slack()
        with (
            patch.object(handler, "append_and_surface") as slack_append,
            patch.object(
                type(slot), "queue_append", autospec=True, side_effect=type(slot).queue_append
            ) as queue_append,
        ):
            hand, run_chat = await _route_linked(
                ds, slack, midturn.Inbound("msg1", None, True), None
            )
        hand.assert_awaited_once()
        slot._acp_client.steer.assert_awaited_once_with("hello")
        assert [q["content"] for q in slot._queue] == ["hello"]
        queue_append.assert_called_once()
        slack_append.assert_not_called()
        run_chat.assert_not_called()
        slack.add_reaction.assert_not_called()
        slack.post_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_steer_its_turn_requeued_is_not_queued_twice(self):
        """The turn ended under the steer and its teardown queued the text."""
        slot, ds = _linked()
        slack = _slack()
        _, run_chat = await _route_linked(
            ds,
            slack,
            midturn.Inbound("msg1", None, True),
            ch.ResumedBusyOutcome(ch.HANDOFF_QUEUED),
        )
        assert slot._queue == []
        run_chat.assert_not_called()
        slack.add_reaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_queue_override_queues_without_a_steer(self):
        slot, ds = _linked()
        hand, _ = await _route_linked(ds, _slack(), midturn.Inbound("msg1", "queue", True), None)
        hand.assert_not_called()
        assert [q["content"] for q in slot._queue] == ["hello"]

    @pytest.mark.asyncio
    async def test_a_message_with_no_inbound_record_keeps_queueing(self):
        """A drained or replayed entry carries no record: it never steers."""
        slot, ds = _linked()
        hand, _ = await _route_linked(ds, _slack(), None, None)
        hand.assert_not_called()
        assert [q["content"] for q in slot._queue] == ["hello"]

    @pytest.mark.asyncio
    async def test_a_message_with_files_is_queued(self):
        slot, ds = _linked()
        hand, _ = await _route_linked(ds, _slack(), midturn.Inbound("msg1", None, False), None)
        hand.assert_not_called()
        assert [q["content"] for q in slot._queue] == ["hello"]

    @pytest.mark.asyncio
    async def test_an_idle_slot_starts_a_turn(self):
        slot, ds = _linked(running=False)
        hand, run_chat = await _route_linked(
            ds, _slack(), midturn.Inbound("msg1", None, True), None
        )
        hand.assert_not_called()
        run_chat.assert_called_once()
        assert slot._queue == []

    @pytest.mark.asyncio
    async def test_a_conductor_thread_owner_message_steers_the_conductor(self):
        from kiro_crew.slack import conductor_thread, handler

        slot, ds = _linked()
        slack = _slack()
        binding = MagicMock(ended_at=None)
        with (
            patch.object(conductor_thread, "binding_for", return_value=binding),
            patch.object(handler, "is_owner", return_value=True),
        ):
            hand, run_chat = await _route_linked(
                ds,
                slack,
                midturn.Inbound("msg1", None, True),
                ch.ResumedBusyOutcome(ch.HANDOFF_STEERED),
            )
        assert hand.await_args.args[1:3] == ("dashboard:slot1", "hello")
        assert slot._queue == []
        run_chat.assert_not_called()

    @pytest.mark.parametrize("admitted", [False, True], ids=["held", "admitted"])
    @pytest.mark.asyncio
    async def test_a_gated_route_never_steers(self, admitted):
        from kiro_crew.slack import handler
        from kiro_crew.slack.handler_runtime.inbound import LinkedRoute, route_linked_thread

        slot, ds = _linked()
        admit = AsyncMock(return_value=admitted)
        midturn.set_inbound(midturn.Inbound("msg1", None, True))
        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch.object(ch, "hand_to_resumed_slot", new_callable=AsyncMock) as hand,
        ):
            routed = await route_linked_thread(
                "hello", "t1", "U1", "C1", _slack(), "t1", admit=admit
            )
        hand.assert_not_called()
        admit.assert_awaited_once_with()
        assert routed is (LinkedRoute.ROUTED if admitted else LinkedRoute.HELD)
        assert [q["content"] for q in slot._queue] == (["hello"] if admitted else [])

    @pytest.mark.asyncio
    async def test_a_chat_whose_turn_ended_during_the_hand_off_starts_the_messages_own_turn(
        self, tmp_path
    ):
        ds = _make_state(tmp_path)
        slot = _busy(ds.get_or_create_slot("slot1"))
        ds.link_slack("slot1", "t1", "C1")
        slot._acp_client = _steerable(accepted=False)
        slack = _slack()

        async def decline_as_the_turn_ends(text):
            slot.task = None
            return False

        slot._acp_client.steer.side_effect = decline_as_the_turn_ends
        hand, run_chat = await _route_linked(ds, slack, midturn.Inbound("msg1", None, True), None)
        hand.assert_awaited_once()
        run_chat.assert_called_once()
        assert run_chat.call_args.args[2] == "hello"
        assert all(q["content"] != "hello" for q in slot._queue)
        slack.post_message.assert_not_called()
        slack.add_reaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_shared_users_running_linked_turn_takes_no_steer(self):
        """A steer would hand its tool prompts to the owner alone; the message is queued."""
        from kiro_crew.slack.turn_requester import note_turn_requester

        slot, ds = _linked()
        slot.task = asyncio.get_running_loop().create_future()
        note_turn_requester(slot.task, "U_SHARED")
        hand, run_chat = await _route_linked(
            ds, _slack(), midturn.Inbound("msg1", None, True), None
        )
        hand.assert_not_called()
        run_chat.assert_not_called()
        assert [entry["content"] for entry in slot._queue] == ["hello"]

    @pytest.mark.parametrize("reason", [ch.REFUSED_CLOSING, ch.REFUSED_QUEUE_FULL])
    @pytest.mark.asyncio
    async def test_a_refusal_after_the_steer_posts_and_appends_nothing(self, tmp_path, reason):
        from kiro_crew.slack import handler

        ds = _make_state(tmp_path)
        slot = _busy(ds.get_or_create_slot("slot1"))
        ds.link_slack("slot1", "t1", "C1")
        slot._acp_client = _steerable(accepted=False)
        slack = _slack()

        async def decline_after_change(text):
            if reason == ch.REFUSED_CLOSING:
                slot._closing = True
            else:
                slot._queue.extend(
                    {"id": str(i), "content": f"waiting {i}"}
                    for i in range(ch.MAX_LIVE_QUEUE_ENTRIES)
                )
            return False

        slot._acp_client.steer.side_effect = decline_after_change
        with (
            patch.object(handler, "append_and_surface") as slack_append,
            patch.object(
                type(slot), "queue_append", autospec=True, side_effect=type(slot).queue_append
            ) as queue_append,
        ):
            hand, run_chat = await _route_linked(
                ds, slack, midturn.Inbound("msg1", None, True), None
            )
        hand.assert_awaited_once()
        slot._acp_client.steer.assert_awaited_once_with("hello")
        assert all(q["content"] != "hello" for q in slot._queue)
        queue_append.assert_not_called()
        slack_append.assert_not_called()
        run_chat.assert_not_called()
        slack.add_reaction.assert_not_called()
        reply = (
            ch.RESUMED_BUSY_QUEUE_FULL_REFUSAL
            if reason == ch.REFUSED_QUEUE_FULL
            else ch.RESUMED_BUSY_MOVED_REFUSAL
        )
        slack.post_message.assert_awaited_once_with("C1", reply, "t1")

    @pytest.mark.parametrize("reason", [ch.REFUSED_REMOTE, ch.REFUSED_CLOSING])
    @pytest.mark.asyncio
    async def test_an_unavailable_slot_at_entry_takes_the_slack_queue(self, reason):
        slot, ds = _linked()
        slot.executor = "remote" if reason == ch.REFUSED_REMOTE else "local"
        slot.is_closing = reason == ch.REFUSED_CLOSING
        hand, run_chat = await _route_linked(
            ds, _slack(), midturn.Inbound("msg1", None, True), None
        )
        hand.assert_not_called()
        run_chat.assert_not_called()
        assert [q["content"] for q in slot._queue] == ["hello"]

    @pytest.mark.parametrize("raises", [False, True], ids=["declined", "failed"])
    @pytest.mark.asyncio
    async def test_a_cancelled_linked_route_still_queues_a_declined_steer(self, tmp_path, raises):
        from kiro_crew.slack import handler
        from kiro_crew.slack.handler_runtime.inbound import route_linked_thread

        ds = _make_state(tmp_path)
        slot = _busy(ds.get_or_create_slot("slot1"))
        ds.link_slack("slot1", "t1", "C1")
        slot._acp_client = _steerable(accepted=False)
        entered = asyncio.Event()
        release = asyncio.Event()
        slack = _slack()

        async def blocked_steer(text):
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=30)
            if raises:
                raise RuntimeError("pipe closed before delivery")
            return False

        slot._acp_client.steer.side_effect = blocked_steer
        midturn.set_inbound(midturn.Inbound("msg1", None, True))
        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch.object(handler, "append_and_surface") as slack_append,
            patch.object(
                type(slot), "queue_append", autospec=True, side_effect=type(slot).queue_append
            ) as queue_append,
        ):
            route = asyncio.create_task(
                route_linked_thread(
                    "hello", "t1", "U1", "C1", slack, "t1", user_meta=dict(_SENDER_META)
                )
            )
            handoffs = []
            try:
                await asyncio.wait_for(entered.wait(), timeout=30)
                handoffs = [
                    t
                    for t in ch._HANDOFFS_IN_FLIGHT
                    if t.get_name() == "channel-handoff:dashboard:slot1"
                ]
                assert len(handoffs) == 1
                assert slot._queue == []
                route.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(route, timeout=30)
                assert not handoffs[0].done()
                release.set()
                outcome = await asyncio.wait_for(handoffs[0], timeout=30)
                assert outcome.kind == ch.HANDOFF_QUEUED
            finally:
                release.set()
                if not route.done():
                    route.cancel()
                await asyncio.wait_for(
                    asyncio.gather(route, *handoffs, return_exceptions=True), timeout=30
                )
        assert [q["content"] for q in slot._queue] == ["hello"]
        _assert_stamped_like_slack(slot._queue[0])
        queue_append.assert_called_once()
        slack_append.assert_not_called()
        slack.post_message.assert_not_called()
        slack.add_reaction.assert_not_called()
        assert slot._pending_steers == []

    @pytest.mark.parametrize("how", ["declined", "no-steer-client", "loss-recovery", "fence-cap"])
    @pytest.mark.asyncio
    async def test_a_steer_that_does_not_land_queues_with_slacks_stamp(self, tmp_path, how):
        """The hand-off's queue arm writes the entry Slack's own queue arm would."""
        from kiro_crew.slack.handler_runtime.inbound import LinkedRoute

        ds, slot = _linked_real_slot(tmp_path)
        if how == "no-steer-client":
            slot._acp_client.supports_steer = False
        elif how == "loss-recovery":
            slot._acp_client.steer_needs_loss_recovery = True
        elif how == "fence-cap":
            slot._steer_audience_fences.update(
                {f"other-{i}": {} for i in range(ch.MAX_PENDING_STEERS)}
            )
        slack = _slack()
        slot._queue = _QueueSeenOnEntry()
        routed = await _route_linked_real(ds, slack, user_meta=dict(_SENDER_META))
        assert routed is LinkedRoute.ROUTED
        # On the entry when it joins the queue: no write can carry it without them.
        assert slot._queue.seen == [("C1",)]
        assert [q["content"] for q in slot._queue] == ["hello"]
        _assert_stamped_like_slack(slot._queue[0])
        if how == "fence-cap":
            slot._acp_client.steer.assert_not_called()
        slack.add_reaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_steer_the_teardown_requeues_keeps_slacks_stamp(self, tmp_path):
        """The turn ends under the RPC; the requeue stamps the channel on the entry."""
        from kiro_crew.dashboard import chat_runner as cr
        from kiro_crew.slack.handler_runtime.inbound import LinkedRoute

        ds, slot = _linked_real_slot(tmp_path)

        async def requeue_then_accept(_text):
            cr._requeue_unconsumed_steers(ds, slot)
            return True

        slot._acp_client.steer.side_effect = requeue_then_accept
        slack = _slack()
        slot._queue = _QueueSeenOnEntry()
        routed = await _route_linked_real(ds, slack, user_meta=dict(_SENDER_META))
        assert routed is LinkedRoute.ROUTED
        assert slot._queue.seen == [("C1",)]
        assert [q["content"] for q in slot._queue] == ["hello"]
        entry = slot._queue[0]
        _assert_stamped_like_slack(entry)
        assert entry["meta"].get("steer_delivery_id")
        slack.add_reaction.assert_not_called()

    @pytest.mark.parametrize("requeued", [False, True], ids=["queue-arm", "teardown-requeue"])
    @pytest.mark.asyncio
    async def test_a_caller_without_slack_data_keeps_the_prior_entry(self, tmp_path, requeued):
        """The Discord shape: no source channel, no row meta, the meta it always had."""
        from kiro_crew.dashboard import chat_runner as cr
        from kiro_crew.dashboard.chat_delivery import (
            COMMANDS_OFF_META_KEY,
            STEER_POSSIBLY_DELIVERED_META,
        )
        from kiro_crew.dashboard.session_control import (
            CHANNEL_RECIPIENT_META_KEY,
            channel_recipient_meta,
            containment_meta,
        )
        from kiro_crew.dashboard.slot_queue_repository import SOURCE_CHANNELS_KEY

        ds, slot = _linked_real_slot(tmp_path)
        if requeued:

            async def requeue_then_accept(_text):
                cr._requeue_unconsumed_steers(ds, slot)
                return True

            slot._acp_client.steer.side_effect = requeue_then_accept
        outcome = await ch.hand_to_resumed_slot(
            ds,
            "dashboard:slot1",
            "hello",
            mode="steer",
            has_attachments=False,
            channel_type="discord",
            conversation_id="c1",
        )
        assert outcome.kind == ch.HANDOFF_QUEUED
        [entry] = slot._queue
        assert SOURCE_CHANNELS_KEY not in entry
        expected = {
            **containment_meta(ds, slot),
            CHANNEL_RECIPIENT_META_KEY: channel_recipient_meta("discord", "c1", "")[
                CHANNEL_RECIPIENT_META_KEY
            ],
            COMMANDS_OFF_META_KEY: True,
        }
        meta = dict(entry["meta"])
        if requeued:
            # The requeue's own marks: its delivery id, and the RPC still in flight.
            assert meta.pop("steer_delivery_id")
            assert meta.pop(STEER_POSSIBLY_DELIVERED_META) is True
        assert meta == expected

    @pytest.mark.asyncio
    async def test_a_steer_that_lost_its_slot_tells_the_thread(self):
        slot, ds = _linked()
        slack = _slack()
        await _route_linked(
            ds,
            slack,
            midturn.Inbound("msg1", None, True),
            ch.ResumedBusyOutcome(ch.HANDOFF_REFUSED, ch.REFUSED_MOVED),
        )
        assert slot._queue == []
        slack.post_message.assert_awaited_once_with("C1", ch.RESUMED_BUSY_MOVED_REFUSAL, "t1")

    @pytest.mark.parametrize("text", ["/agent kirocrew", "/tools", "<@UBOT> /model x"])
    @pytest.mark.asyncio
    async def test_a_slash_command_is_queued_for_the_slot_to_run(self, text):
        """The slot's turn start runs a leading ``/`` command; a steer would prompt with it."""
        slot, ds = _linked()
        hand, _ = await _route_linked(
            ds,
            _slack(),
            midturn.Inbound("msg1", None, True),
            ch.ResumedBusyOutcome(ch.HANDOFF_STEERED),
            text=text,
        )
        hand.assert_not_called()
        assert [q["content"] for q in slot._queue] == [text]


# ── the queue drain ──


@pytest.mark.asyncio
async def test_a_drained_entry_routes_with_no_inbound_record():
    """The drain task inherits the context of the message whose task queued it;
    the entry must route with no record, so it never steers or reacts on that
    message's ts."""
    from kiro_crew.slack import events_queue

    seen: list[midturn.Inbound | None] = []

    async def capture(*_a, **_k):
        seen.append(midturn._INBOUND.get())

    orch = MagicMock()
    orch.slack = MagicMock()
    orch.slack.remove_reaction = AsyncMock()
    midturn.set_inbound(midturn.Inbound("1.0", "steer", True))
    with (
        # Through the facade: the events family shares these names (facade mirror).
        patch.object(ev, "handle_message_transport", side_effect=capture),
        patch.object(ev, "handle_message", side_effect=capture),
    ):
        await events_queue._dispatch_queued(
            orch, "sk", "2.0", "queued text", {"channel": "C1", "thread_ts": "t1"}, slack=orch.slack
        )
    assert seen == [None]
