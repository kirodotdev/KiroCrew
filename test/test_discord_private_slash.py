"""Every Discord slash command answers only the person who ran it.

The invariant: no slash command produces a non-ephemeral message or leaves a loading
message behind, and every interaction-token write names its destination, so the
mid-send re-check judges it after the governance and roster gates.

These tests drive ``on_interaction`` with a real ``DiscordClient`` whose REST layer is
recorded (or, for the re-check, a stubbed HTTP session under the real ladder), so what
is asserted is the request Discord would receive: the callback type, the ephemeral
flag, the route and the destination. No network, no real sleeping.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from discord_helpers import _dispatcher, _inbound
from multidict import CIMultiDict

from kiro_crew.discord import client as dc
from kiro_crew.discord import transport_dispatch as td
from kiro_crew.discord.client import (
    DISCORD_OK,
    DiscordApiResult,
    DiscordClient,
    DiscordInteraction,
    SendPermission,
)

_APP = "777000111"
_EPHEMERAL = 1 << 6
_THREAD = "5550001"
_DM = "5550002"


class _Recorder:
    """Records every REST call the real client makes and answers it as Discord would."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any, str]] = []
        self._next = 900

    async def api_json(
        self, method: str, path: str, payload: Any, *, destination: str = "", **_kw: Any
    ) -> DiscordApiResult:
        self.calls.append((method, path, payload, destination))
        if path.endswith("/callback") or method == "DELETE":
            return DiscordApiResult(DISCORD_OK, data={}, status=204)
        self._next += 1
        return DiscordApiResult(DISCORD_OK, data={"id": str(self._next)}, status=200)

    def channel_writes(self) -> list[tuple[str, str]]:
        """Every write addressed to a channel route: a message anyone there can read."""
        return [(m, p) for m, p, _b, _d in self.calls if m != "GET" and p.startswith("/channels/")]

    def token_writes(self, token: str) -> list[tuple[str, str, Any, str]]:
        return [c for c in self.calls if token in c[1]]


def _real_client() -> tuple[DiscordClient, _Recorder]:
    client = DiscordClient(token="bot-secret")
    client.application_id = _APP
    client._channel_types[_THREAD] = 11  # a public thread, so no lookup is needed
    recorder = _Recorder()
    client.api_json = recorder.api_json  # type: ignore[method-assign]
    return client, recorder


def _wired() -> tuple[Any, Any, _Recorder]:
    d, _fake, sess = _dispatcher({"u1"}, allowed_threads={_THREAD})
    client, recorder = _real_client()
    d.client = client
    return d, sess, recorder


def _cmd(name: str, channel: str, *, token: str = "cmdtok", options: Any = None) -> Any:
    return DiscordInteraction(
        interaction_id="i9",
        interaction_token=token,
        channel_id=channel,
        user_id="u1",
        message_id="",
        guild_id="g1" if channel == _THREAD else "",
        kind=2,
        command_name=name,
        options=options or {},
    )


def _press(custom_id: str, message_id: str, *, token: str) -> Any:
    return DiscordInteraction(
        interaction_id="i10",
        interaction_token=token,
        channel_id=_THREAD,
        user_id="u1",
        message_id=message_id,
        guild_id="g1",
        custom_id=custom_id,
    )


def _assert_private(recorder: _Recorder, channel: str, token: str = "cmdtok") -> None:
    """One ephemeral deferral, then only webhook writes, each private and destined."""
    assert recorder.channel_writes() == []
    writes = recorder.token_writes(token)
    method, path, body, destination = writes[0]
    assert (method, path) == ("POST", f"/interactions/i9/{token}/callback")
    assert body == {"type": 5, "data": {"flags": _EPHEMERAL}}
    replies = writes[1:]
    assert replies, "the command answered nothing"
    assert replies[0][:2] == ("PATCH", f"/webhooks/{_APP}/{token}/messages/@original")
    for method, path, body, _dest in replies[1:]:
        assert (method, path) == ("POST", f"/webhooks/{_APP}/{token}")
        assert body["flags"] & _EPHEMERAL
    assert all(dest == channel for *_rest, dest in writes)
    assert not any("Running" in str(body) for _m, _p, body, _d in writes)


class TestEverySlashReplyIsPrivate:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("channel", [_THREAD, _DM])
    @pytest.mark.parametrize(
        "name", ["new", "agent", "compact", "link", "unlink", "stop", "status", "help", "model"]
    )
    async def test_a_command_writes_only_privately(self, name: str, channel: str) -> None:
        d, sess, recorder = _wired()
        sess._gp.models = [{"modelId": "m-fast", "name": "Fast"}]
        await d.on_interaction(_cmd(name, channel))
        _assert_private(recorder, channel)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["sessions", "bot"])
    async def test_the_thread_refusal_of_a_resume_command_is_private(self, name: str) -> None:
        d, _sess, recorder = _wired()
        await d.on_interaction(_cmd(name, _THREAD))
        _assert_private(recorder, _THREAD)
        assert "works only in a direct message" in recorder.token_writes("cmdtok")[1][2]["content"]

    @pytest.mark.asyncio
    async def test_compact_replies_twice_through_the_original_then_a_followup(self) -> None:
        d, _sess, recorder = _wired()
        await d.on_interaction(_cmd("compact", _THREAD))
        _assert_private(recorder, _THREAD)
        replies = recorder.token_writes("cmdtok")[1:]
        assert [body["content"] for _m, _p, body, _d in replies] == [
            "🔄 Compacting context…",
            "✅ Context compacted.",
        ]

    @pytest.mark.asyncio
    async def test_a_dm_session_picker_clears_the_deferral_it_did_not_use(self) -> None:
        """The DM picker stays a DM message (a DM is already private); the loading
        state it would otherwise leave is deleted instead."""
        d, _fake, _sess = _dispatcher({"u1"})
        await d.on_interaction(_cmd("sessions", "c1"))
        assert [w[0] for w in _fake.webhook_writes] == ["delete_original"]
        assert _fake.webhook_writes[0][4] == "c1"

    @pytest.mark.asyncio
    async def test_a_command_that_reaches_no_handler_never_becomes_a_turn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A turn streams into the channel, so a private command must stop short."""
        monkeypatch.setattr(td, "unknown_command_usage", lambda _text: "")
        d, fake, sess = _dispatcher({"u1"})
        await d.on_interaction(_cmd("nope", "c1"))
        assert fake.sent == [] and sess.begin_turns == 0
        assert [w[0] for w in fake.webhook_writes] == ["delete_original"]

    @pytest.mark.asyncio
    async def test_the_text_form_still_posts_to_the_thread(self) -> None:
        d, fake, _sess = _dispatcher({"u1"}, allowed_threads={_THREAD})
        await d.handle_message(_inbound("!new", conversation_id=_THREAD, thread_id=_THREAD))
        assert fake.send_channels == [_THREAD] and fake.webhook_writes == []


class TestModelPickerInAThread:
    async def _picker(self) -> tuple[Any, Any, _Recorder, str]:
        d, sess, recorder = _wired()
        sess._gp.models = [{"modelId": "m-fast", "name": "Fast"}]
        await d.on_interaction(_cmd("model", _THREAD))
        _assert_private(recorder, _THREAD)
        _m, _p, body, _d = recorder.token_writes("cmdtok")[1]
        assert body["components"], "the picker carries its buttons"
        (key,) = d._model_pickers
        return d, sess, recorder, key.split(":", 1)[1]

    def _press_writes(self, recorder: _Recorder) -> list[tuple[str, str, Any, str]]:
        return recorder.token_writes("presstok")

    @pytest.mark.asyncio
    async def test_a_press_edits_the_private_picker_through_its_own_token(self) -> None:
        d, sess, recorder, message_id = await self._picker()
        await d.on_interaction(_press("m:1", message_id, token="presstok"))
        assert sess._gp.set_models == ["m-fast"]
        ack, edit = self._press_writes(recorder)
        assert ack[2] == {"type": 6} and ack[3] == _THREAD
        assert edit[:2] == ("PATCH", f"/webhooks/{_APP}/presstok/messages/@original")
        assert edit[2]["components"] == [] and "m-fast" in edit[2]["content"]
        assert edit[3] == _THREAD
        assert recorder.channel_writes() == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stale", ["consumed", "expired"])
    async def test_a_stale_press_is_answered_through_the_same_route(self, stale: str) -> None:
        d, sess, recorder, message_id = await self._picker()
        if stale == "consumed":
            await d.on_interaction(_press("m:1", message_id, token="first"))
        else:
            for picker in d._model_pickers.values():
                picker.created_at -= td._MODEL_PICKER_TTL_SECS + 1
        await d.on_interaction(_press("m:0", message_id, token="presstok"))
        _ack, edit = self._press_writes(recorder)
        assert edit[:2] == ("PATCH", f"/webhooks/{_APP}/presstok/messages/@original")
        assert "no longer active" in edit[2]["content"] and edit[2]["components"] == []
        assert edit[3] == _THREAD
        assert recorder.channel_writes() == []


# -- The real REST ladder: a withdrawn destination and an expired token --------


class _Resp:
    def __init__(self, status: int, body: Any = None) -> None:
        self.status = status
        self._body = body
        self.headers = CIMultiDict({})

    async def json(self, content_type: Any = None) -> Any:
        return self._body


class _CM:
    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _Session:
    """Serves queued responses and records every request line."""

    def __init__(self, responses: list[_Resp]) -> None:
        self._responses = list(responses)
        self.requests: list[str] = []

    def request(self, method: str, url: str, **_kw: Any) -> _CM:
        self.requests.append(f"{method} {url.split('/api/v10', 1)[-1]}")
        return _CM(self._responses.pop(0) if self._responses else _Resp(204))

    async def close(self) -> None:
        return None


class _Asyncio:
    """The client's ``asyncio``: a sleep runs a hook (the withdrawal) and returns."""

    def __init__(self) -> None:
        self.on_sleep: Any = None

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    async def sleep(self, _delay: float, *a: Any, **k: Any) -> None:
        if self.on_sleep is not None:
            self.on_sleep()
        await asyncio.sleep(0)


def _laddered(
    monkeypatch: pytest.MonkeyPatch, responses: list[_Resp]
) -> tuple[Any, _Session, set[str], list[str], _Asyncio]:
    d, _fake, _sess = _dispatcher({"u1"}, allowed_threads={_THREAD})
    client = DiscordClient(token="bot-secret")
    client.application_id = _APP
    client._channel_types[_THREAD] = 11
    session = _Session(responses)
    roster = {_THREAD}
    asked: list[str] = []
    fake_asyncio = _Asyncio()

    async def _ensure() -> Any:
        return session

    async def _ceiling(_channel_type: str) -> bool:
        return True

    def _still_permitted(channel_id: str) -> SendPermission:
        asked.append(channel_id)
        return SendPermission.allow() if channel_id in roster else SendPermission.revoked()

    monkeypatch.setattr(client, "_ensure_session", _ensure)
    monkeypatch.setattr(dc, "asyncio", fake_asyncio)
    monkeypatch.setattr(dc, "channel_outbound_permitted", _ceiling)
    client.still_permitted = _still_permitted
    d.client = client
    return d, session, roster, asked, fake_asyncio


class TestTheLadderJudgesEveryPrivateReply:
    @pytest.mark.asyncio
    async def test_a_roster_withdrawn_during_a_followup_wait_stops_it(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The follow-up is rate limited; the thread leaves the roster during the
        wait, so the re-check refuses the retry. That it reads THIS thread proves
        the destination was passed; nothing falls back to the channel."""
        d, session, roster, asked, fake_asyncio = _laddered(
            monkeypatch,
            [
                _Resp(204),
                _Resp(200, {"id": "1"}),
                _Resp(429, {"retry_after": 1.0, "global": False}),
                _Resp(200, {"id": "2"}),
            ],
        )
        fake_asyncio.on_sleep = roster.clear
        with caplog.at_level("INFO", logger="kiro_crew.discord.transport_dispatch"):
            await d.on_interaction(_cmd("compact", _THREAD))
        assert session.requests == [
            "POST /interactions/i9/cmdtok/callback",
            f"PATCH /webhooks/{_APP}/cmdtok/messages/@original",
            f"POST /webhooks/{_APP}/cmdtok",
        ]
        assert asked == [_THREAD]
        assert "reply dropped" in caplog.text

    @pytest.mark.asyncio
    async def test_an_expired_token_is_dropped_with_no_channel_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After 15 minutes Discord answers 401; the reply is not retried and not
        re-sent to the thread, which would publish what was promised private."""
        d, session, _roster, _asked, _fake = _laddered(
            monkeypatch,
            [_Resp(204), _Resp(401, {"code": 50027, "message": "Invalid Webhook Token"})],
        )
        await d.on_interaction(_cmd("new", _THREAD))
        # The PATCH is not retried and nothing goes to /channels/. The loading
        # message did not get filled, so the clean-up DELETE is the only other try.
        assert session.requests == [
            "POST /interactions/i9/cmdtok/callback",
            f"PATCH /webhooks/{_APP}/cmdtok/messages/@original",
            f"DELETE /webhooks/{_APP}/cmdtok/messages/@original",
        ]

    @pytest.mark.asyncio
    async def test_a_caller_removed_during_a_reply_wait_gets_no_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The thread stays allowed, so the destination roster cannot see this:
        only the caller check stops the private reply's retry."""
        d, session, _roster, asked, fake_asyncio = _laddered(
            monkeypatch,
            [
                _Resp(204),
                _Resp(429, {"retry_after": 1.0, "global": False}),
                _Resp(200, {"id": "1"}),
            ],
        )
        fake_asyncio.on_sleep = d._allowed.clear
        await d.on_interaction(_cmd("status", _THREAD))
        assert session.requests == [
            "POST /interactions/i9/cmdtok/callback",
            f"PATCH /webhooks/{_APP}/cmdtok/messages/@original",
            f"DELETE /webhooks/{_APP}/cmdtok/messages/@original",
        ]
        assert asked == []  # the caller refusal decided before the thread roster

    @pytest.mark.asyncio
    async def test_a_picker_press_by_a_removed_caller_gets_no_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        d, session, _roster, _asked, fake_asyncio = _laddered(
            monkeypatch,
            [
                _Resp(204),
                _Resp(200, {"id": "55"}),
                _Resp(204),
                _Resp(429, {"retry_after": 1.0, "global": False}),
                _Resp(200, {"id": "55"}),
            ],
        )
        d.sessions._gp.models = [{"modelId": "m-fast", "name": "Fast"}]
        await d.on_interaction(_cmd("model", _THREAD))
        assert list(d._model_pickers) == [f"{_THREAD}:55"]
        fake_asyncio.on_sleep = d._allowed.clear
        await d.on_interaction(_press("m:1", "55", token="presstok"))
        assert session.requests[2:] == [
            "POST /interactions/i10/presstok/callback",
            f"PATCH /webhooks/{_APP}/presstok/messages/@original",
        ]


class TestAuthorizationIsReadAgainBeforeEachReply:
    """The gates run before the deferral; the deferral and the command are awaits.
    A caller or thread withdrawn inside them gets nothing written, and the loading
    message is cleared, which discloses nothing."""

    def _withdraw_during_defer(self, d: Any, fake: Any, withdraw: Any) -> None:
        real = fake.defer_interaction

        async def _defer(*a: Any, **k: Any) -> bool:
            landed = await real(*a, **k)
            withdraw(d)
            return landed

        fake.defer_interaction = _defer

    @pytest.mark.asyncio
    @pytest.mark.parametrize("channel", [_THREAD, "c1"])
    async def test_a_caller_removed_during_the_deferral_is_told_nothing(
        self, channel: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events: list[dict[str, Any]] = []
        monkeypatch.setattr(
            td, "sel", lambda: SimpleNamespace(log_api_access=lambda **kw: events.append(kw))
        )
        d, fake, _sess = _dispatcher({"u1"}, allowed_threads={_THREAD})
        fake.thread_channels.add(_THREAD)
        self._withdraw_during_defer(d, fake, lambda dd: dd._allowed.clear())
        await d.on_interaction(_cmd("status", channel))
        assert [w[0] for w in fake.webhook_writes] == ["delete_original"]
        assert fake.sent == []
        # The withheld reply is a permission decision, so it leaves an audit row.
        assert [(e["operation"], e["outcome"], e["caller"]) for e in events] == [
            ("discord.slash_reply", "denied", "u1")
        ]

    @pytest.mark.asyncio
    async def test_a_thread_removed_during_the_deferral_is_told_nothing(self) -> None:
        d, fake, _sess = _dispatcher({"u1"}, allowed_threads={_THREAD})
        fake.thread_channels.add(_THREAD)
        self._withdraw_during_defer(d, fake, lambda dd: dd._allowed_threads.clear())
        await d.on_interaction(_cmd("agent", _THREAD))
        assert [w[0] for w in fake.webhook_writes] == ["delete_original"]
        assert fake.sent == []

    @pytest.mark.asyncio
    async def test_a_ceiling_closed_after_the_gates_withholds_the_reply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _denied(_channel_type: str) -> bool:
            return False

        monkeypatch.setattr(td, "channel_outbound_permitted", _denied)
        d, fake, _sess = _dispatcher({"u1"})
        await d.on_interaction(_cmd("status", "c1"))
        assert [w[0] for w in fake.webhook_writes] == ["delete_original"]

    @pytest.mark.asyncio
    async def test_a_model_press_withdrawn_during_the_switch_writes_no_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        d, fake, sess = _dispatcher({"u1"}, allowed_threads={_THREAD})
        fake.thread_channels.add(_THREAD)
        sess._gp.models = [{"modelId": "m-fast", "name": "Fast"}]
        await d.on_interaction(_cmd("model", _THREAD))
        (key,) = d._model_pickers
        real = d._apply_model

        async def _apply(*a: Any, **k: Any) -> str:
            outcome = await real(*a, **k)
            d._allowed_threads.clear()
            return outcome

        monkeypatch.setattr(d, "_apply_model", _apply)
        events: list[dict[str, Any]] = []
        monkeypatch.setattr(
            td, "sel", lambda: SimpleNamespace(log_api_access=lambda **kw: events.append(kw))
        )
        await d.on_interaction(_press("m:1", key.split(":", 1)[1], token="presstok"))
        assert sess._gp.set_models == ["m-fast"]
        assert [w for w in fake.webhook_writes if w[1] == "presstok"] == []
        assert ("discord.set_model_result", "denied") in [
            (e["operation"], e["outcome"]) for e in events
        ]


class TestARefusedPrivateCommandIsNeverSpooled:
    """The restart notice quotes a spooled message back into the conversation, so
    a private command's text (a `/sessions` query) must never reach the spool."""

    @pytest.mark.asyncio
    async def test_a_command_refused_by_the_restart_gate_answers_privately(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.messaging import inbound_spool

        monkeypatch.setattr(inbound_spool, "data_home", lambda: tmp_path)
        d, fake, sess = _dispatcher({"u1"}, allowed_threads={_THREAD})
        fake.thread_channels.add(_THREAD)
        sess.reserve_inbound_callback = lambda: None
        await d.on_interaction(_cmd("sessions", _THREAD, options={"query": "private title"}))
        assert not (tmp_path / "inbound-spool" / "refused.jsonl").exists()
        assert fake.sent == []
        [(verb, _tok, body, _c, dest)] = fake.webhook_writes
        assert (verb, dest) == ("edit_original", _THREAD) and "restarting" in body

    @pytest.mark.asyncio
    async def test_the_text_form_is_still_spooled(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.messaging import inbound_spool

        monkeypatch.setattr(inbound_spool, "data_home", lambda: tmp_path)
        d, _fake, sess = _dispatcher({"u1"})
        sess.reserve_inbound_callback = lambda: None
        await d.handle_message(_inbound("hello there"))
        spool = tmp_path / "inbound-spool" / "refused.jsonl"
        assert spool.exists() and "hello there" in spool.read_text(encoding="utf-8")


class TestTheLoadingMessageIsAlwaysSettled:
    @pytest.mark.asyncio
    async def test_a_command_that_raises_still_clears_its_deferral(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        d, fake, _sess = _dispatcher({"u1"})

        async def _boom(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("handler failed")

        monkeypatch.setattr(d, "handle_message", _boom)
        with pytest.raises(RuntimeError):
            await d.on_interaction(_cmd("new", "c1"))
        assert [w[0] for w in fake.webhook_writes] == ["delete_original"]


class TestAFailedFirstReplyLeavesTheOriginalOpen:
    @pytest.mark.asyncio
    async def test_the_next_reply_aims_at_the_original_again(self) -> None:
        """A first edit that did not land filled nothing, so the second reply must
        not be a follow-up beside a loading message that never resolves."""
        d, fake, _sess = _dispatcher({"u1"})
        real = fake._webhook_write

        def _first_edit_fails(verb: str, *a: Any) -> Any:
            landed = real(verb, *a)
            return None if len(fake.webhook_writes) == 1 else landed

        fake._webhook_write = _first_edit_fails
        await d.on_interaction(_cmd("compact", "c1"))
        assert [w[0] for w in fake.webhook_writes] == ["edit_original", "edit_original"]


class TestAFollowupIsNeverResent:
    @pytest.mark.asyncio
    async def test_a_5xx_after_a_followup_is_not_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Discord may have stored the follow-up before failing; a re-send would
        show the caller the same reply twice."""
        d, session, _roster, _asked, _fake = _laddered(
            monkeypatch, [_Resp(204), _Resp(200, {"id": "1"}), _Resp(502), _Resp(200, {"id": "2"})]
        )
        await d.on_interaction(_cmd("compact", _THREAD))
        assert session.requests == [
            "POST /interactions/i9/cmdtok/callback",
            f"PATCH /webhooks/{_APP}/cmdtok/messages/@original",
            f"POST /webhooks/{_APP}/cmdtok",
        ]


class TestTheClientVerbs:
    """Each interaction-webhook verb hands its destination to the ladder."""

    @pytest.mark.asyncio
    async def test_every_verb_passes_its_destination(self) -> None:
        client, recorder = _real_client()
        await client.defer_interaction("i1", "tk", destination="a")
        await client.edit_interaction_original("tk", "x", destination="b")
        await client.send_interaction_followup("tk", "y", destination="c")
        await client.delete_interaction_original("tk", destination="d")
        assert [(m, p, dest) for m, p, _b, dest in recorder.calls] == [
            ("POST", "/interactions/i1/tk/callback", "a"),
            ("PATCH", f"/webhooks/{_APP}/tk/messages/@original", "b"),
            ("POST", f"/webhooks/{_APP}/tk", "c"),
            ("DELETE", f"/webhooks/{_APP}/tk/messages/@original", "d"),
        ]
        followup = recorder.calls[2][2]
        assert followup["flags"] & _EPHEMERAL and followup["allowed_mentions"] == {"parse": []}

    @pytest.mark.asyncio
    async def test_a_failed_write_reports_no_message(self) -> None:
        client = DiscordClient(token="bot-secret")

        async def _failed(*_a: Any, **_k: Any) -> DiscordApiResult:
            return DiscordApiResult("permanent", status=404)

        client.api_json = _failed  # type: ignore[method-assign]
        assert await client.edit_interaction_original("tk", "x", destination="b") is None
        assert await client.send_interaction_followup("tk", "x", destination="b") is None
        assert await client.delete_interaction_original("tk", destination="b") is False
        assert await client.defer_interaction("i", "tk", destination="b") is False
