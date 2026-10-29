"""The dashboard -> Slack mirror: its prompt echo and its "Thinking…" placeholder.

Three defects a linked thread showed on mirrored turns:

* the prompt echo escaped every ``<…>`` entity and wrapped the prompt in ``_…_``,
  so a pasted ``<https://…|label>`` link or ``<#C…>`` channel reference arrived as
  raw text. The echo now keeps those live, flattens a ``<@U…>`` mention to plain
  ``@…`` so it never re-pings anyone, still escapes ``<!channel>`` and friends and
  any stray or truncated bracket, and is posted as a ``> 💬 …`` quote;
* the mirror's task stream opened with a "Thinking…" line, and a stream only
  appends, so that line sat above Slack's own "Thinking completed" plan header for
  good. The line is now its own message, deleted when the stream, the answer or
  the turn's end takes over;
* a prompt typed in the linked Slack thread itself was echoed back into it,
  quoting the person to themselves, and a conductor's patrol wake was echoed
  as raw "[work-ledger wake] …" text. The echo is now an allowlist: only a
  prompt a person typed in the dashboard is quoted, every system origin
  (monitor / work-ledger wake, cron, sub-agent, task runner, ``session_send``,
  app, recovery, a channel-typed message) is not, and a new producer is not
  until it stamps itself as the person;
* a tool step with no purpose line showed its raw ``@server/tool`` id; it now
  reads the dashboard row's derived title ("Wait", "Git status").
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from slot_ownership_checkpoint_helpers import (  # noqa: F401 -- fixtures are requested by name
    _client,
    chat_run_fixture,
    state_fixture,
)
from turn_harness import Do, Raise, TurnContext, TurnScript, run_turn

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.crew_log import emit_turn as crew_log_emit
from kiro_crew.dashboard.chat_runner import _prepare_mirror_msg
from kiro_crew.slack.client import SlackClientOps
from kiro_crew.slack.format import escape_mrkdwn_keep_links, mirror_prompt_echo
from kiro_crew.testing.wait import async_wait_until

_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
_ANSWER = AcpEvent(kind=EVENT_TEXT_CHUNK, text="the reply")
_TOOL = AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="t1", title="read_1", tool_kind="read")


class _SlackRecorder:
    """A Slack client recording each call, bound to the real port's signatures.

    Each post returns a distinct ts, so a delete can be matched to what it removed.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        declared = getattr(SlackClientOps, name, None) if not name.startswith("_") else None
        if not inspect.iscoroutinefunction(declared):
            raise AttributeError(name)
        signature = inspect.signature(declared)

        async def _call(*args: Any, **kwargs: Any) -> str:
            bound = signature.bind(self, *args, **kwargs)
            bound.apply_defaults()
            self.calls.append((name, dict(bound.arguments)))
            return f"{name}-ts-{len(self.calls)}"

        return _call

    def posts(self) -> list[tuple[str, str]]:
        """(ts, text) of every post_message, in order."""
        return [
            (f"post_message-ts-{i + 1}", args["text"])
            for i, (name, args) in enumerate(self.calls)
            if name == "post_message"
        ]


class _StreamRefusingSlack(_SlackRecorder):
    """Slack refuses chat.startStream (an org-wide install with no recipient)."""

    def __getattr__(self, name: str) -> Any:
        call = super().__getattr__(name)
        if name != "start_stream":
            return call

        async def _refused(*args: Any, **kwargs: Any) -> None:
            await call(*args, **kwargs)
            return None

        return _refused


async def _linked_turn(
    events: list[Any],
    message: str = "hello",
    slack: _SlackRecorder | None = None,
    user_row: bool = True,
    **run_kwargs: Any,
) -> _SlackRecorder:
    slack = slack or _SlackRecorder()

    def _linked(ctx: TurnContext) -> None:
        ctx.state.slack_client = slack
        ctx.state.sessions.set_slack_link("dashboard:chat-1", "thread-1", "C-1")

    await run_turn(
        TurnScript(
            events=events, message=message, setup=_linked, user_row=user_row, run_kwargs=run_kwargs
        )
    )
    return slack


# ── The echo: what survives, what is neutralised ─────────────────────────────


class TestEchoKeepsLinks:
    def test_a_web_link_and_its_label_stay_live(self) -> None:
        assert escape_mrkdwn_keep_links("see <https://example.com/a_b|the doc>") == (
            "see <https://example.com/a_b|the doc>"
        )
        assert escape_mrkdwn_keep_links("<https://example.com>") == "<https://example.com>"

    def test_a_channel_reference_stays_live(self) -> None:
        assert escape_mrkdwn_keep_links("in <#C0123ABC>") == "in <#C0123ABC>"
        assert escape_mrkdwn_keep_links("<#C0123ABC|general>") == "<#C0123ABC|general>"

    def test_a_label_ampersand_is_escaped(self) -> None:
        assert escape_mrkdwn_keep_links("<https://x.com|a & b>") == "<https://x.com|a &amp; b>"


class TestEchoNeutralises:
    @pytest.mark.parametrize(
        ("mention", "flat"),
        [
            ("<@U0123ABC>", "@U0123ABC"),
            ("<@W0123ABC>", "@W0123ABC"),
            ("<@U0123ABC|kyle>", "@kyle"),
        ],
    )
    def test_a_user_mention_becomes_plain_text(self, mention: str, flat: str) -> None:
        out = escape_mrkdwn_keep_links(f"ping {mention} now")
        assert out == f"ping {flat} now"
        assert "<@" not in out

    @pytest.mark.parametrize(
        "broadcast",
        [
            "<!channel>",
            "<!here>",
            "<!everyone>",
            "<!subteam^S0123|@team>",
            "<mailto:a@b.co>",
            # A broadcast nested in a kept shape is matched on its own, not kept.
            "<https://x.com|<!channel>>",
            "<#C0123ABC|<!here>>",
            "<@U0123ABC|<!everyone>>",
        ],
    )
    def test_a_broadcast_or_other_entity_is_escaped(self, broadcast: str) -> None:
        out = escape_mrkdwn_keep_links(f"hey {broadcast}")
        assert "<" not in out and ">" not in out
        assert out.startswith("hey &lt;") and out.endswith("&gt;")

    def test_stray_brackets_and_ampersands_are_escaped(self) -> None:
        assert escape_mrkdwn_keep_links("a < b && c > d") == "a &lt; b &amp;&amp; c &gt; d"

    def test_a_link_the_truncation_cut_off_is_escaped(self) -> None:
        # The echo bound is 500 chars, the last one the cut's ``…``; a link
        # straddling it loses its ``>``.
        cut = _prepare_mirror_msg("x" * 470 + " <https://example.com/very/long/path|label>")
        assert len(cut) == 500 and cut.endswith("…") and ">" not in cut
        out = escape_mrkdwn_keep_links(cut)
        assert "<" not in out
        assert out.endswith(" &lt;https://example.com/very/lo…")

    def test_an_unterminated_opener_cannot_capture_a_later_entity(self) -> None:
        out = escape_mrkdwn_keep_links("<https://a.com <!channel>")
        assert "<!channel>" not in out
        assert out == "&lt;https://a.com &lt;!channel&gt;"

    def test_an_exfiltration_url_stays_redacted_and_is_not_a_link(self) -> None:
        payload = "QUtJQUlPU0ZPRE5ON0VYQU1QTEVrZXlzZWNyZXRkYXRhMTIzNDU2Nzg5MA"
        raw = f"open <https://evil.example.com/c?d={payload}|harmless> please"
        out = escape_mrkdwn_keep_links(_prepare_mirror_msg(raw))
        assert payload not in out
        assert "evil.example.com/c" not in out
        assert "REDACTED" in out
        assert "<" not in out


class TestEchoIsAQuote:
    def test_every_line_is_quoted_and_a_leading_gt_cannot_nest(self) -> None:
        assert mirror_prompt_echo("first\n> second") == "> 💬 first\n> &gt; second"

    def test_no_italic_wrapper(self) -> None:
        assert mirror_prompt_echo("<https://a.com/x_y|z>") == "> 💬 <https://a.com/x_y|z>"


@pytest.mark.asyncio
async def test_a_mirrored_turn_echoes_links_live_and_pings_nobody() -> None:
    slack = await _linked_turn(
        [_ANSWER, _DONE],
        message="look at <https://example.com/a_b|the doc> in <#C0123ABC> cc <@U0123ABC> <!here>",
    )
    [echo] = [text for _, text in slack.posts() if "💬" in text]
    assert echo == (
        "> 💬 look at <https://example.com/a_b|the doc> in <#C0123ABC> cc @U0123ABC &lt;!here&gt;"
    )


# ── The "Thinking…" line never outlives its turn ─────────────────────────────


def _thinking_posts(slack: _SlackRecorder) -> list[str]:
    return [ts for ts, text in slack.posts() if "Thinking" in text]


def _deleted(slack: _SlackRecorder) -> list[str]:
    return [args["ts"] for name, args in slack.calls if name == "delete_message"]


def _stream_texts(slack: _SlackRecorder) -> list[str]:
    return [args.get("initial_text") or "" for name, args in slack.calls if name == "start_stream"]


def _surviving_thinking(slack: _SlackRecorder) -> list[str]:
    """Every "Thinking" line still in the thread once the turn has ended.

    A posted placeholder survives unless it was deleted; a line written into the
    append-only task stream always survives.
    """
    deleted = set(_deleted(slack))
    kept = [text for ts, text in slack.posts() if "Thinking" in text and ts not in deleted]
    return kept + [text for text in _stream_texts(slack) if "Thinking" in text]


@pytest.mark.asyncio
async def test_a_tool_turn_deletes_the_thinking_line_when_the_stream_opens() -> None:
    slack = await _linked_turn([_TOOL, _ANSWER, _DONE])
    assert _surviving_thinking(slack) == []
    names = [name for name, _ in slack.calls]
    [thinking] = _thinking_posts(slack)
    assert _deleted(slack) == [thinking]
    # The placeholder goes as the stream takes over: after it opens, before its
    # first task, and so before the answer is posted.
    delete_at = names.index("delete_message")
    assert names.index("start_stream") < delete_at < names.index("append_task")
    answer_at = max(i for i, (n, a) in enumerate(slack.calls) if n == "post_message")
    assert delete_at < answer_at
    assert names.count("stop_stream") == 1


@pytest.mark.asyncio
async def test_a_tool_free_turn_deletes_the_thinking_line_before_the_answer() -> None:
    slack = await _linked_turn([_ANSWER, _DONE])
    assert _surviving_thinking(slack) == []
    names = [name for name, _ in slack.calls]
    [thinking] = _thinking_posts(slack)
    assert _deleted(slack) == [thinking]
    assert "start_stream" not in names
    [answer_at] = [
        i
        for i, (n, a) in enumerate(slack.calls)
        if n == "post_message" and a["text"] == "the reply"
    ]
    assert names.index("delete_message") < answer_at


@pytest.mark.asyncio
async def test_a_failed_turn_still_deletes_the_thinking_line() -> None:
    slack = await _linked_turn([Raise(RuntimeError("boom"))])
    assert _surviving_thinking(slack) == []
    [thinking] = _thinking_posts(slack)
    assert _deleted(slack) == [thinking]


@pytest.mark.asyncio
async def test_the_stream_opens_once_and_the_line_goes_once_over_many_tools() -> None:
    second = AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="t2", title="read_2", tool_kind="read")
    slack = await _linked_turn([_TOOL, second, _ANSWER, _DONE])
    names = [name for name, _ in slack.calls]
    assert names.count("start_stream") == 1
    assert len(_deleted(slack)) == 1
    assert _surviving_thinking(slack) == []


@pytest.mark.asyncio
async def test_a_refused_stream_leaves_the_line_up_until_the_answer() -> None:
    slack = _StreamRefusingSlack()
    marks: list[int] = []
    second = AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="t2", title="read_2", tool_kind="read")
    tools_ran = Do(lambda ctx: marks.append(len(slack.calls)))
    await _linked_turn([_TOOL, second, tools_ran, _ANSWER, _DONE], slack=slack)
    names = [name for name, _ in slack.calls]
    assert names.count("start_stream") == 1
    assert "append_task" not in names
    # Nothing deleted the line while the tools ran.
    [mark] = marks
    assert "delete_message" not in names[:mark]
    # Still working while tools run, then gone before the answer lands.
    [answer_at] = [
        i
        for i, (n, a) in enumerate(slack.calls)
        if n == "post_message" and a["text"] == "the reply"
    ]
    assert names.index("start_stream") < names.index("delete_message") < answer_at
    assert _surviving_thinking(slack) == []


@pytest.mark.asyncio
async def test_a_withheld_turn_opens_no_stream_and_still_retires_the_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.dashboard import chat_runner

    monkeypatch.setattr(chat_runner, "cross_surface_withheld", lambda state, slot: True)
    slack = await _linked_turn([_TOOL, _ANSWER, _DONE])
    names = [name for name, _ in slack.calls]
    assert "start_stream" not in names
    assert "append_task" not in names
    assert _surviving_thinking(slack) == []


# ── Only a prompt a person typed in the dashboard is echoed ──────────────────
#
# The echo shows a remote surface what was asked in the DASHBOARD. A patrol wake
# echoed its raw "[work-ledger wake] … [auto-nudge cycle 1]" banner into a code
# channel. The gate is an allowlist: every input it reads is a structural stamp
# that defaults to "not the person", so a new producer is never echoed until it
# says it is.

_REPO_SRC = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"
_ORIGIN_KWARGS = (
    "_directive_user_origin",
    "_directive_channel_origin",
    "_directive_self_wake",
    "_turn_actor",
)
_WAKE_TEXT = (
    "[work-ledger wake] item=it_5757d0a6 reason=stall last_status=(none yet)\n"
    "[auto-nudge cycle 1]\n[patrol budget: 1 of 24]\n[work ledger -- durable state …]"
)


def _echoes(slack: _SlackRecorder) -> list[str]:
    return [text for _, text in slack.posts() if "💬" in text]


def _system_dispatch_sites() -> list[tuple[str, dict[str, Any]]]:
    """Every ``_run_chat(...)`` call in the product that stamps a system origin.

    Read from the source, so a producer added later joins this table by itself.
    Only the four origin keywords, and only those given as constants: a site
    whose value is computed (``not bool(request_app)``) is a human send for one
    of its values and is covered by the predicate tests below.
    """
    sites: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(_REPO_SRC.rglob("*.py")):
        if "/tests/" in path.as_posix():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
            if name != "_run_chat":
                continue
            origin = {
                kw.arg: kw.value.value
                for kw in node.keywords
                if kw.arg in _ORIGIN_KWARGS and isinstance(kw.value, ast.Constant)
            }
            system = (
                origin.get("_directive_user_origin") is False
                or origin.get("_directive_channel_origin") is True
                or origin.get("_directive_self_wake") is True
                or origin.get("_turn_actor") not in (None, "", "user")
            )
            if system:
                rel = path.relative_to(_REPO_SRC).as_posix()
                sites.append((f"{rel}:{node.lineno}", origin))
    return sites


_SYSTEM_SITES = _system_dispatch_sites()


def test_the_source_scan_finds_every_known_system_producer() -> None:
    # Not vacuous: the scan sees the producers this fix is about.
    files = {site.split(":", 1)[0] for site, _ in _SYSTEM_SITES}
    for expected in (
        "slack/gateway_runtime/nudge_dashboard_fire.py",  # monitor / work-ledger wake
        "dashboard/session_deliver.py",  # cron and app text, through its one deliver()
        "slack/gateway_runtime/subagent_injection.py",
        "dashboard/handlers/taskrunner.py",
        "slack/handler_runtime/inbound.py",  # typed in the linked Slack thread
    ):
        assert expected in files, expected


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [o for _, o in _SYSTEM_SITES], ids=[s for s, _ in _SYSTEM_SITES])
async def test_no_system_dispatch_site_is_echoed(origin: dict[str, Any]) -> None:
    slack = await _linked_turn([_ANSWER, _DONE], message=_WAKE_TEXT, user_row=False, **origin)
    assert _echoes(slack) == []
    # The answer still reaches the thread: only the echo is withheld.
    assert "the reply" in [text for _, text in slack.posts()]


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", sorted(crew_log_emit.ACTORS - {"user"}))
async def test_every_non_user_actor_is_unechoed_even_stamped_as_the_user(actor: str) -> None:
    # Worst case: a producer that wrongly claims user origin still names itself.
    slack = await _linked_turn([_ANSWER, _DONE], message=_WAKE_TEXT, _turn_actor=actor)
    assert _echoes(slack) == []


@pytest.mark.asyncio
async def test_the_patrol_wake_from_the_bug_is_not_echoed() -> None:
    # What `_fire_dashboard_nudge` passes for a monitor / work-ledger wake.
    slack = await _linked_turn(
        [_ANSWER, _DONE],
        message=_WAKE_TEXT,
        user_row=False,
        _directive_user_origin=False,
        _directive_self_wake=True,
    )
    assert _echoes(slack) == []


@pytest.mark.asyncio
async def test_a_session_send_peer_prompt_is_not_echoed() -> None:
    # `session_send` queues with directive_user_origin=False and no kind.
    slack = await _linked_turn(
        [_ANSWER, _DONE], message="[sent by session chat-9]\nhi", user_row=False
    )
    assert _echoes(slack) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stamp",
    ["_synthetic_payload", "_synthetic_recovery_turn", "_turn_provenance_restored"],
)
async def test_runner_authored_and_restored_turns_are_not_echoed(stamp: str) -> None:
    slack = await _linked_turn([_ANSWER, _DONE], message="what's up?", **{stamp: True})
    assert _echoes(slack) == []


@pytest.mark.asyncio
async def test_a_prompt_typed_in_the_slack_thread_is_not_echoed_back() -> None:
    # What the linked-thread router passes for a message typed in thread-1 (and
    # what a code channel's top-level post rides on the same router).
    slack = await _linked_turn(
        [_ANSWER, _DONE],
        message="what's up?",
        _directive_user_origin=True,
        _directive_channel_origin=True,
    )
    assert _echoes(slack) == []
    # The placeholder and the answer still post, and the placeholder goes before
    # the first answer chunk lands.
    [thinking] = _thinking_posts(slack)
    assert _deleted(slack) == [thinking]
    names = [name for name, _ in slack.calls]
    [answer_at] = [
        i
        for i, (n, a) in enumerate(slack.calls)
        if n == "post_message" and a["text"] == "the reply"
    ]
    assert names.index("delete_message") < answer_at
    assert _surviving_thinking(slack) == []


@pytest.mark.asyncio
async def test_a_prompt_typed_in_the_dashboard_is_still_echoed() -> None:
    slack = await _linked_turn([_ANSWER, _DONE], message="what's up?")
    assert _echoes(slack) == ["> 💬 what's up?"]


@pytest.mark.asyncio
async def test_the_gate_reads_stamps_not_text() -> None:
    # The person may paste a wake banner; it is still their prompt.
    slack = await _linked_turn([_ANSWER, _DONE], message=_WAKE_TEXT)
    [echo] = _echoes(slack)
    assert "[work-ledger wake]" in echo


@pytest.mark.asyncio
async def test_the_cross_surface_leg_uses_the_same_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.dashboard import chat_runner

    sent: list[str] = []

    async def _record(state: Any, session_key: str, user_message: str) -> None:
        sent.append(user_message)

    monkeypatch.setattr(chat_runner, "_deliver_cross_surface_user_message", _record)
    await _linked_turn(
        [_ANSWER, _DONE],
        message=_WAKE_TEXT,
        user_row=False,
        _directive_user_origin=False,
        _directive_self_wake=True,
    )
    assert sent == []
    await _linked_turn([_ANSWER, _DONE], message="what's up?")
    assert sent == ["what's up?"]


class TestPromptTypedInDashboard:
    """Each condition of the allowlist, flipped on its own, closes it."""

    @staticmethod
    def _turn(**over: Any) -> Any:
        base: dict[str, Any] = {
            "_directive_user_origin": True,
            "_directive_channel_origin": False,
            "_crew_log_actor": "user",
            "_is_synthetic": False,
            "_synthetic_payload": False,
            "_synthetic_recovery_turn": False,
            "_recovery_echo_owed": False,
            "_turn_provenance_restored": False,
        }
        return SimpleNamespace(**{**base, **over})

    def test_the_dashboard_human_passes(self) -> None:
        from kiro_crew.dashboard.chat_runner import _prompt_typed_in_dashboard

        assert _prompt_typed_in_dashboard(self._turn()) is True

    @pytest.mark.parametrize(
        "field, value",
        [
            ("_directive_user_origin", False),
            ("_directive_channel_origin", True),
            ("_crew_log_actor", "autonudge"),
            ("_crew_log_actor", "other"),
            ("_is_synthetic", True),
            ("_synthetic_recovery_turn", True),
            ("_turn_provenance_restored", True),
        ],
    )
    def test_each_condition_closes_it(self, field: str, value: Any) -> None:
        from kiro_crew.dashboard.chat_runner import _prompt_typed_in_dashboard

        assert _prompt_typed_in_dashboard(self._turn(**{field: value})) is False

    def test_an_owed_replay_of_the_persons_words_passes(self) -> None:
        from kiro_crew.dashboard.chat_runner import _prompt_typed_in_dashboard

        owed = self._turn(_synthetic_recovery_turn=True, _recovery_echo_owed=True)
        assert _prompt_typed_in_dashboard(owed) is True
        # Before the slash gate binds ``_is_synthetic``, its payload half decides.
        early = self._turn()
        del early._is_synthetic
        assert _prompt_typed_in_dashboard(early) is True
        early._synthetic_payload = True
        assert _prompt_typed_in_dashboard(early) is False


class TestTheDrainCarriesOrigin:
    """``_start_next_queued_turn`` hands the gate the stamps a queued entry carried."""

    @staticmethod
    def _drain(tmp_path: Any, monkeypatch: pytest.MonkeyPatch, *, merge: bool = False) -> Any:
        from unittest.mock import MagicMock

        from chat_test_helpers import _make_state

        # Load the facade first: a first import while ``cr._run_chat`` is stubbed
        # would bind the stub there for the rest of the session.
        from kiro_crew.dashboard import chat as _chat  # noqa: F401
        from kiro_crew.dashboard import chat_runner as cr

        state = _make_state(tmp_path)
        state.subagents = None
        captured: dict[str, Any] = {}

        def _stub_run_chat(_state: Any, _slot: Any, _prompt: str, **kwargs: Any) -> Any:
            captured.update(kwargs)

            async def _done() -> None:
                return None

            return _done()

        def _fake_spawn(_state: Any, _slot: Any, coro: Any) -> Any:
            coro.close()
            task = MagicMock()
            task.done.return_value = True
            return task

        monkeypatch.setattr(cr, "_run_chat", _stub_run_chat)
        monkeypatch.setattr(cr, "spawn_guarded_turn", _fake_spawn)
        if merge:
            cfg = MagicMock()
            cfg.load.return_value.dashboard.merge_queued_messages = True
            monkeypatch.setattr(cr, "KiroCrewConfig", cfg)

        def _busy(key: str) -> Any:
            slot = state.get_or_create_slot(key)
            task = MagicMock()
            task.done.return_value = False
            slot.task = task
            return slot

        async def _run(slot: Any) -> dict[str, Any]:
            captured.clear()
            slot.task = None
            assert await cr._start_next_queued_turn(state, slot) is True
            return dict(captured)

        return state, _busy, _run

    @staticmethod
    async def _route_busy(state: Any, slot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """Queue "what's up?" from thread t1 through the router's real busy arm."""
        from conftest import MockSlackClient
        from kiro_crew.slack import handler as h

        monkeypatch.setattr(h, "_owner_id", "U1")
        monkeypatch.setattr(state, "get_linked_slot", lambda ts: slot, raising=False)
        monkeypatch.setattr(h, "_dashboard_state", state)
        assert await h.maybe_route_linked_thread(
            "what's up?", "t1", "U1", "C1", MockSlackClient(), "t1"
        )

    @pytest.mark.asyncio
    async def test_a_queued_dashboard_message_runs_as_the_person(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.dashboard import chat_delivery as cd

        state, busy, run = self._drain(tmp_path, monkeypatch)
        slot = busy("chat-1")
        cd.queue_for_next_turn(state, slot, "from the dashboard", directive_user_origin=True)
        kwargs = await run(slot)
        assert kwargs["_directive_user_origin"] is True
        assert kwargs["_directive_channel_origin"] is False
        assert not kwargs.get("_turn_actor")

    @pytest.mark.asyncio
    async def test_a_queued_peer_message_runs_unstamped(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.dashboard import chat_delivery as cd

        state, busy, run = self._drain(tmp_path, monkeypatch)
        slot = busy("chat-1")
        # How `session_send` queues into a busy slot.
        cd.queue_for_next_turn(state, slot, "[sent by session x]", directive_user_origin=False)
        assert (await run(slot))["_directive_user_origin"] is False

    @pytest.mark.asyncio
    async def test_a_message_the_router_queued_runs_channel_origin(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state, busy, run = self._drain(tmp_path, monkeypatch)
        slot = busy("chat-1")
        await self._route_busy(state, slot, monkeypatch)
        assert (await run(slot))["_directive_channel_origin"] is True

    @pytest.mark.asyncio
    async def test_a_merged_slack_and_dashboard_batch_runs_channel_origin(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fails closed: the merged turn is channel-origin when ANY entry is, so a
        # mixed batch is not quoted back (it would quote the Slack words too).
        from kiro_crew.dashboard import chat_delivery as cd

        state, busy, run = self._drain(tmp_path, monkeypatch, merge=True)
        slot = busy("chat-1")
        await self._route_busy(state, slot, monkeypatch)
        cd.queue_for_next_turn(state, slot, "typed in the dashboard", directive_user_origin=True)
        kwargs = await run(slot)
        assert slot._queue == []
        assert kwargs["_directive_channel_origin"] is True


# ── Tool steps read like the dashboard's rows, never as a raw tool id ────────


def _task_titles(slack: _SlackRecorder) -> list[str]:
    return [
        args["title"]
        for name, args in slack.calls
        if name == "append_task" and args["status"] == "in_progress"
    ]


@pytest.mark.asyncio
async def test_an_mcp_tool_step_shows_its_human_label() -> None:
    wait = AcpEvent(
        kind=EVENT_TOOL_CALL,
        tool_call_id="w1",
        title="@kirocrew-core/wait",
        tool_name="wait",
        mcp_server_name="kirocrew-core",
        tool_input='{"seconds": 300}',
    )
    ledger = AcpEvent(
        kind=EVENT_TOOL_CALL, tool_call_id="w2", title="Running: @kirocrew-work/work_ledger_read"
    )
    slack = await _linked_turn([wait, ledger, _ANSWER, _DONE])
    titles = _task_titles(slack)
    assert titles == ["Wait", "Work ledger read"]
    assert not any("@" in t or "/" in t for t in titles)


@pytest.mark.asyncio
async def test_a_shell_step_reads_like_the_transcript_row() -> None:
    shell = AcpEvent(
        kind=EVENT_TOOL_CALL,
        tool_call_id="s1",
        title="shell",
        tool_kind="execute",
        is_shell=True,
        tool_input='{"command": "git status"}',
    )
    slack = await _linked_turn([shell, _ANSWER, _DONE])
    assert _task_titles(slack) == ["Git status"]


@pytest.mark.asyncio
async def test_a_purpose_line_still_wins() -> None:
    tool = AcpEvent(
        kind=EVENT_TOOL_CALL,
        tool_call_id="p1",
        title="@kirocrew-core/wait",
        tool_purpose="Waiting for CI",
    )
    slack = await _linked_turn([tool, _ANSWER, _DONE])
    assert _task_titles(slack) == ["Waiting for CI"]


# ── A script cron's send_to_session is the cron's turn, not a person's ───────


class TestAScriptCronSendIsTheCrons:
    """``POST /api/chat`` from an attested ``cron:`` caller stamps actor ``cron``.

    ``ctx.send_to_session`` posts under the internal secret with no app claim, so
    the send handler's ``not request_app`` read it as the person's own turn: it
    passed the echo allowlist and was quoted into a linked thread.
    """

    @staticmethod
    def _as_cron(monkeypatch: pytest.MonkeyPatch, creator: str) -> None:
        async def _creator(_request: Any) -> str:
            return creator

        async def _admit(*_a: Any, **_k: Any) -> tuple[None, bool, dict[str, Any]]:
            return None, False, {}

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.cron_slot_creator", _creator)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.cron_creator_admission", _admit)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._maybe_auto_title", AsyncMock())

    @pytest.mark.asyncio
    @pytest.mark.parametrize("creator, actor", [("cron:job-1", "cron"), ("", None)])
    async def test_an_idle_send(
        self, state: Any, chat_run: Any, monkeypatch: pytest.MonkeyPatch, creator: str, actor: Any
    ) -> None:
        self._as_cron(monkeypatch, creator)
        async with _client(state, "") as client:
            resp = await client.post("/api/chat?ws=1", json={"slot": "s1", "message": "tick"})
            assert resp.status == 200, await resp.text()
        # The idle arm hands the turn to a task; wait (bounded) for it to start.
        await async_wait_until(lambda: chat_run.await_count or chat_run.call_count, timeout=5)
        kwargs = chat_run.call_args.kwargs
        assert kwargs.get("_turn_actor") == actor
        # Authority is unchanged: only who the turn is filed under moves.
        assert kwargs["_directive_user_origin"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("creator, actor", [("cron:job-1", "cron"), ("", None)])
    async def test_a_queued_send(
        self, state: Any, chat_run: Any, monkeypatch: pytest.MonkeyPatch, creator: str, actor: Any
    ) -> None:
        from unittest.mock import MagicMock

        from kiro_crew.dashboard.chat_delivery import TURN_ACTOR_META_KEY

        self._as_cron(monkeypatch, creator)
        slot = state.get_or_create_slot("s1")
        busy = MagicMock()
        busy.done.return_value = False
        slot.task = busy
        async with _client(state, "") as client:
            resp = await client.post("/api/chat?ws=1", json={"slot": "s1", "message": "tick"})
            assert resp.status == 200, await resp.text()
        [entry] = slot._queue
        assert (entry.get("meta") or {}).get(TURN_ACTOR_META_KEY) == actor

    @pytest.mark.asyncio
    @pytest.mark.parametrize("creator, actor", [("cron:job-1", "cron"), ("", None)])
    async def test_a_send_held_behind_sub_agents(
        self, state: Any, chat_run: Any, monkeypatch: pytest.MonkeyPatch, creator: str, actor: Any
    ) -> None:
        from kiro_crew.dashboard.chat_delivery import TURN_ACTOR_META_KEY

        self._as_cron(monkeypatch, creator)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.subagents_hold_user_messages", lambda *_a: True
        )
        slot = state.get_or_create_slot("s1")
        async with _client(state, "") as client:
            resp = await client.post("/api/chat?ws=1", json={"slot": "s1", "message": "tick"})
            assert resp.status == 200, await resp.text()
        chat_run.assert_not_called()
        [entry] = slot._queue
        assert (entry.get("meta") or {}).get(TURN_ACTOR_META_KEY) == actor


# ── A replay of the person's words owes the echo only if it was never posted ─


async def _recovered(script_events: list[Any], **script: Any) -> tuple[Any, dict[str, Any]]:
    """Run a linked turn that requeues itself; return (slack, the drained retry's kwargs)."""
    slack = _SlackRecorder()

    def _linked(ctx: TurnContext) -> None:
        ctx.state.slack_client = slack
        ctx.state.sessions.set_slack_link("dashboard:chat-1", "thread-1", "C-1")

    record = await run_turn(
        TurnScript(events=script_events, message="what's up?", setup=_linked, **script)
    )
    [successor] = record.successors
    return slack, successor.kwargs


@pytest.mark.asyncio
async def test_a_turn_that_dies_before_its_echo_replays_owing_it() -> None:
    from kiro_crew.acp.transport_errors import AcpProcessDied

    slack, retry = await _recovered([], allocation_error=AcpProcessDied("runtime died"))
    assert _echoes(slack) == []
    assert retry.get("_synthetic_recovery_turn") is True
    assert retry.get("_recovery_echo_owed") is True
    # The stamp is queue plumbing: it does not ride into the persisted row.
    from kiro_crew.dashboard.chat_runner import MIRROR_ECHO_OWED_META_KEY

    assert MIRROR_ECHO_OWED_META_KEY not in (retry["_current_message"].get("meta") or {})


@pytest.mark.asyncio
async def test_a_turn_that_echoed_then_died_replays_owing_nothing() -> None:
    from kiro_crew.acp.transport_errors import AcpError

    slack, retry = await _recovered([Raise(AcpError("Internal server error", transient=True))])
    assert _echoes(slack) == ["> 💬 what's up?"]
    assert retry.get("_synthetic_recovery_turn") is True
    assert "_recovery_echo_owed" not in retry


@pytest.mark.asyncio
async def test_an_owed_replay_is_echoed_and_an_unowed_one_is_not() -> None:
    owed = await _linked_turn(
        [_ANSWER, _DONE],
        message="what's up?",
        _synthetic_recovery_turn=True,
        _recovery_echo_owed=True,
    )
    assert _echoes(owed) == ["> 💬 what's up?"]
    unowed = await _linked_turn(
        [_ANSWER, _DONE], message="what's up?", _synthetic_recovery_turn=True
    )
    assert _echoes(unowed) == []


@pytest.mark.asyncio
async def test_an_owed_stamp_cannot_open_a_system_turn() -> None:
    slack = await _linked_turn(
        [_ANSWER, _DONE],
        message=_WAKE_TEXT,
        user_row=False,
        _directive_user_origin=False,
        _directive_self_wake=True,
        _synthetic_recovery_turn=True,
        _recovery_echo_owed=True,
    )
    assert _echoes(slack) == []


class TestBatchRecoveryEchoOwed:
    @staticmethod
    def _entry(owed: bool = True, payload: str = "original") -> dict[str, Any]:
        from kiro_crew.dashboard.chat_runner import MIRROR_ECHO_OWED_META_KEY
        from kiro_crew.dashboard.chat_utils import SYNTHETIC_RECOVERY_KIND, RecoveryPayload

        return {
            "kind": SYNTHETIC_RECOVERY_KIND,
            "payload": RecoveryPayload(payload),
            "meta": {MIRROR_ECHO_OWED_META_KEY: True} if owed else {},
        }

    def test_only_stamped_verbatim_replays_owe_it(self) -> None:
        from kiro_crew.dashboard.chat_runner import _batch_recovery_echo_owed

        assert _batch_recovery_echo_owed([self._entry()]) is True
        assert _batch_recovery_echo_owed([]) is False
        assert _batch_recovery_echo_owed([self._entry(owed=False)]) is False
        assert _batch_recovery_echo_owed([self._entry(payload="continuation")]) is False
        stamped_plain = {"meta": self._entry()["meta"], "_directive_user_origin": True}
        assert _batch_recovery_echo_owed([stamped_plain]) is False
        assert _batch_recovery_echo_owed([self._entry(), self._entry(owed=False)]) is False
