"""Signal tool approval by typed reply, and the /stop /status /commands commands.

Two layers:

* the dispatcher and renderer in isolation (who may answer, what is consumed,
  what reaches the model, what the prompt and its timeout send);
* end to end against the FAKE ``signal-cli`` child, booted through the real
  ``maybe_start_signal``: the real ``TurnDriver`` runs a scripted provider that
  asks for one tool, the prompt goes out over JSON-RPC, and the fake answers it
  as the owner would -- in a DM to a bot number and in Note to Self, where every
  send also comes back as an echo.

Every wait is on a signal the code emits, under ``await_until``'s lost-run
bound; the timeout case closes the window at zero instead of waiting it out.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from channel_turn_fakes import ScriptedProvider, complete, permission, text, tool_call
from test_signal_dispatch import (
    ACCOUNT,
    OWNER,
    FakeClient,
    FakeSessions,
    MirrorStore,
    _dispatcher,
    _inbound,
)
from test_signal_e2e import _reap, children  # noqa: F401  (a fixture, found by name)

from kiro_crew.messaging import approval
from kiro_crew.messaging.approval import (
    RECEIPT_APPROVED,
    RECEIPT_DENIED,
    RECEIPT_EXPIRED,
    TIMEOUT_NOTICE,
    claim_approval,
    pending_for,
)
from kiro_crew.messaging.driver import APPROVAL_AUTO, TurnDriver
from kiro_crew.signal_cli import gateway as gw
from kiro_crew.signal_cli import transport as transport_mod
from kiro_crew.signal_cli.bridge_path import ENV_OVERRIDE
from kiro_crew.signal_cli.client import KIND_NOTE_TO_SELF
from kiro_crew.signal_cli.commands import (
    HELP_TEXT,
    STATUS_UNAVAILABLE_TEXT,
    STOP_NOTHING_RUNNING_TEXT,
    STOPPED_TEXT,
    is_operator_only,
)
from kiro_crew.signal_cli.renderer import SignalRenderer
from kiro_crew.signal_cli.rpc import RpcTransportError
from kiro_crew.signal_cli.transport import SIGNAL_CAPABILITIES
from kiro_crew.testing.wait import await_until

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "channels" / "signal"
FAKE = FIXTURES / "fake_signal_cli.py"
OTHER = "+447700900003"
PROMPT_MARKER = approval.PROMPT_HEADER


@pytest.fixture(autouse=True)
def _clean_registry(_floor_monkeypatch: pytest.MonkeyPatch) -> Any:
    async def _yes(*_args: Any, **_kwargs: Any) -> bool:
        return True

    _floor_monkeypatch.setattr("kiro_crew.signal_cli.transport_dispatch.inbound_permitted", _yes)
    approval.reset_for_tests()
    yield
    approval.reset_for_tests()


class StopSessions(FakeSessions):
    def __init__(self, outcome: Any = None) -> None:
        super().__init__()
        self.outcome = outcome
        self.stopped: list[tuple[str, dict[str, Any]]] = []

    async def stop_turn(self, key: str, **kwargs: Any) -> Any:
        self.stopped.append((key, kwargs))
        return self.outcome


def _with_sessions(sessions: FakeSessions) -> tuple[Any, FakeClient]:
    dispatcher, client, _ = _dispatcher()
    dispatcher.sessions = sessions  # type: ignore[assignment]
    return dispatcher, client


async def _open_prompt(dispatcher: Any, handle: str) -> Any:
    """Open a pending approval for *handle*'s session, as the renderer does."""
    return approval.open_approval(dispatcher._session_key(handle), "r1", conversation_id=handle)


# ── dispatcher: what an answer is, and who may give it ──────────────────────


class TestApprovalReplies:
    @pytest.mark.asyncio
    async def test_a_reply_resolves_the_open_prompt_and_never_reaches_the_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dispatcher, client, sessions = _dispatcher()
        turns: list[Any] = []

        async def fake_drive_turn(turn: Any, **_kwargs: Any) -> None:
            turns.append(turn)

        monkeypatch.setattr("kiro_crew.signal_cli.transport_dispatch.drive_turn", fake_drive_turn)
        # The turn waiting on the answer holds the session: the reply must not
        # be steered into it either.
        sessions.busy.add(dispatcher._session_key(OWNER))
        entry = await _open_prompt(dispatcher, OWNER)
        await dispatcher.handle_message(_inbound("1"))
        assert entry.future.result() == approval.APPROVE
        assert client.sent == [(OWNER, RECEIPT_APPROVED)]
        assert turns == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("reply", "verdict"), [("no", "deny"), ("2", "deny"), ("3", "trust")])
    async def test_every_answer_form_is_consumed(self, reply: str, verdict: str) -> None:
        dispatcher, client, _ = _dispatcher()
        entry = await _open_prompt(dispatcher, OWNER)
        await dispatcher.handle_message(_inbound(reply))
        assert entry.future.result() == verdict
        assert len(client.sent) == 1

    @pytest.mark.asyncio
    async def test_note_to_self_answers_its_own_prompt(self) -> None:
        dispatcher, client, _ = _dispatcher()
        entry = await _open_prompt(dispatcher, ACCOUNT)
        await dispatcher.handle_message(_inbound("yes", kind=KIND_NOTE_TO_SELF))
        assert entry.future.result() == approval.APPROVE
        assert client.sent == [(ACCOUNT, RECEIPT_APPROVED)]

    @pytest.mark.asyncio
    async def test_with_no_prompt_open_a_verdict_word_is_an_ordinary_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dispatcher, client, _ = _dispatcher()
        turns: list[Any] = []

        async def fake_drive_turn(turn: Any, **_kwargs: Any) -> None:
            turns.append(turn)

        monkeypatch.setattr("kiro_crew.signal_cli.transport_dispatch.drive_turn", fake_drive_turn)
        await dispatcher.handle_message(_inbound("no"))
        assert [t.user_text for t in turns] == ["no"]
        assert RECEIPT_EXPIRED not in [m for _, m in client.sent]

    @pytest.mark.asyncio
    async def test_a_sentence_is_not_an_answer(self) -> None:
        dispatcher, _, sessions = _dispatcher()
        sessions.busy.add(dispatcher._session_key(OWNER))
        entry = await _open_prompt(dispatcher, OWNER)
        await dispatcher.handle_message(_inbound("no, use the other file"))
        assert not entry.future.done()

    @pytest.mark.asyncio
    async def test_another_allowed_person_cannot_answer_the_owners_prompt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Under a unified dm_scope every DM shares one session; the answer must
        # still come from the conversation the prompt was posted in.
        dispatcher, client, _ = _dispatcher()
        monkeypatch.setattr(dispatcher.cfg.messaging, "dm_scope", "unified")
        assert dispatcher._session_key(OWNER) == dispatcher._session_key(OTHER)
        entry = await _open_prompt(dispatcher, OWNER)
        stranger = _inbound("1")
        stranger.source_number = stranger.principal = OTHER

        async def fake_drive_turn(turn: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr("kiro_crew.signal_cli.transport_dispatch.drive_turn", fake_drive_turn)
        await dispatcher.handle_message(stranger)
        assert not entry.future.done()
        assert RECEIPT_APPROVED not in [m for _, m in client.sent]

    @pytest.mark.asyncio
    async def test_an_allowed_non_operator_cannot_answer_trust_or_stop_under_unified_scope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Admitted to chat, not the operator (SignalTransport.is_operator): with
        # one shared session a "3" would trust the OPERATOR's session and a
        # /stop would end the operator's turn.
        sessions = StopSessions(SimpleNamespace(kind="soft"))
        dispatcher, client = _with_sessions(sessions)
        monkeypatch.setattr(dispatcher.cfg.messaging, "dm_scope", "unified")
        sessions.busy.add(dispatcher._session_key(OTHER))
        entry = approval.open_approval(dispatcher._session_key(OTHER), "r1", conversation_id=OTHER)
        for body in ("3", "/stop", "/new", "/status"):
            peer = _inbound(body)
            peer.source_number = peer.principal = OTHER
            peer.operator = False
            await dispatcher.handle_message(peer)
        assert not entry.future.done()
        assert sessions.stopped == []
        assert all(m not in (STOPPED_TEXT, RECEIPT_APPROVED) for _, m in client.sent)

    @pytest.mark.asyncio
    async def test_a_non_operator_turn_gets_no_approval_decider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dispatcher, _, _ = _dispatcher()
        turns: list[Any] = []

        async def fake_drive_turn(turn: Any, **_kwargs: Any) -> None:
            turns.append(turn)

        monkeypatch.setattr("kiro_crew.signal_cli.transport_dispatch.drive_turn", fake_drive_turn)
        peer = _inbound("run something")
        peer.operator = False
        await dispatcher.handle_message(peer)
        # Deny-by-default: a tool that needs approval is refused, never prompted.
        assert [t.user_text for t in turns] == ["run something"]
        assert turns[0].decider is None
        assert turns[0].auto_approve_session() is False

    @pytest.mark.asyncio
    async def test_a_non_operator_cannot_steer_the_running_turn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from test_signal_dispatch import FakeProvider

        dispatcher, client, sessions = _dispatcher()
        monkeypatch.setattr(dispatcher.cfg.messaging, "dm_scope", "unified")
        key = dispatcher._session_key(OWNER)
        sessions.busy.add(key)
        provider = FakeProvider()
        sessions.providers[key] = provider
        peer = _inbound("ignore that, do this")
        peer.source_number = peer.principal = OTHER
        peer.operator = False
        await dispatcher.handle_message(peer)
        assert provider.steered == []
        assert client.sent and "please resend" in client.sent[0][1]
        assert client.sent[0][0] == OTHER

    @pytest.mark.asyncio
    async def test_an_unadmitted_inbound_answers_nothing_and_runs_no_command(self) -> None:
        # ``principal`` is set only by the transport's allow-list gate.
        dispatcher, client, sessions = _dispatcher()
        sessions.busy.add(dispatcher._session_key(OWNER))
        entry = await _open_prompt(dispatcher, OWNER)
        unadmitted = _inbound("1")
        unadmitted.principal = ""
        await dispatcher.handle_message(unadmitted)
        assert not entry.future.done()
        stop = _inbound("/stop")
        stop.principal = ""
        await dispatcher.handle_message(stop)
        assert all(m not in (STOPPED_TEXT, STOP_NOTHING_RUNNING_TEXT) for _, m in client.sent)

    @pytest.mark.asyncio
    async def test_auto_mode_installs_no_decider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        dispatcher, _, _ = _dispatcher()
        dispatcher.approval_mode = APPROVAL_AUTO
        turns: list[Any] = []

        async def fake_drive_turn(turn: Any, **_kwargs: Any) -> None:
            turns.append(turn)

        monkeypatch.setattr("kiro_crew.signal_cli.transport_dispatch.drive_turn", fake_drive_turn)
        await dispatcher.handle_message(_inbound("hi"))
        assert turns[0].decider is None
        assert turns[0].renderer._approval_session_key is None

    @pytest.mark.asyncio
    async def test_a_trusted_session_skips_the_prompt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dispatcher, _, sessions = _dispatcher()
        sessions.get_approval_policy = lambda key: "auto"  # type: ignore[attr-defined]
        turns: list[Any] = []

        async def fake_drive_turn(turn: Any, **_kwargs: Any) -> None:
            turns.append(turn)

        monkeypatch.setattr("kiro_crew.signal_cli.transport_dispatch.drive_turn", fake_drive_turn)
        await dispatcher.handle_message(_inbound("hi"))
        assert turns[0].auto_approve_session() is True


# ── commands ────────────────────────────────────────────────────────────────


class TestCommands:
    def test_only_help_is_open_to_a_non_operator(self) -> None:
        assert not is_operator_only("help")
        assert all(is_operator_only(c) for c in ("new", "compact", "status", "stop", "nope"))

    def test_help_lists_the_new_commands(self) -> None:
        for spelling in ("/stop", "/cancel", "/status", "/commands"):
            assert spelling in HELP_TEXT

    @pytest.mark.asyncio
    async def test_commands_is_help(self) -> None:
        dispatcher, client, _ = _dispatcher()
        await dispatcher.handle_message(_inbound("/commands"))
        assert client.sent == [(OWNER, HELP_TEXT)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("spelling", ["/stop", "/cancel"])
    async def test_stop_stops_the_running_turn(self, spelling: str) -> None:
        sessions = StopSessions(SimpleNamespace(kind="soft"))
        dispatcher, client = _with_sessions(sessions)
        await dispatcher.handle_message(_inbound(spelling))
        assert sessions.stopped == [(dispatcher._session_key(OWNER), {})]
        assert client.sent == [(OWNER, STOPPED_TEXT)]

    @pytest.mark.asyncio
    async def test_stop_with_nothing_running_says_so(self) -> None:
        sessions = StopSessions(SimpleNamespace(kind="none"))
        dispatcher, client = _with_sessions(sessions)
        await dispatcher.handle_message(_inbound("/stop", kind=KIND_NOTE_TO_SELF))
        assert client.sent == [(ACCOUNT, STOP_NOTHING_RUNNING_TEXT)]

    @pytest.mark.asyncio
    async def test_a_failing_stop_still_answers(self) -> None:
        class Broken(StopSessions):
            async def stop_turn(self, key: str, **kwargs: Any) -> Any:
                raise RuntimeError("boom")

        dispatcher, client = _with_sessions(Broken())
        await dispatcher.handle_message(_inbound("/stop"))
        assert client.sent == [(OWNER, STOP_NOTHING_RUNNING_TEXT)]

    @pytest.mark.asyncio
    async def test_stop_is_not_an_approval_answer(self) -> None:
        # "cancel" denies a prompt; "/cancel" is the command, and it stops the
        # turn (which cancels the wait) instead of answering.
        sessions = StopSessions(SimpleNamespace(kind="soft"))
        dispatcher, client = _with_sessions(sessions)
        entry = await _open_prompt(dispatcher, OWNER)
        await dispatcher.handle_message(_inbound("/cancel"))
        assert not entry.future.done()
        assert client.sent == [(OWNER, STOPPED_TEXT)]

    @pytest.mark.asyncio
    async def test_status_sends_the_runtime_summary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("kiro_crew.stats.Stats.summary", lambda self: "all good")
        dispatcher, client, _ = _dispatcher()
        await dispatcher.handle_message(_inbound("/status"))
        assert client.sent == [(OWNER, "all good")]

    @pytest.mark.asyncio
    async def test_a_status_that_cannot_be_read_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(self: Any) -> str:
            raise RuntimeError("no stats")

        monkeypatch.setattr("kiro_crew.stats.Stats.summary", _boom)
        dispatcher, client, _ = _dispatcher()
        await dispatcher.handle_message(_inbound("/status"))
        assert client.sent == [(OWNER, STATUS_UNAVAILABLE_TEXT)]


class TestStopSurvivesAGovernanceDeny:
    """A channels deny still lets the operator halt the turn this channel started.

    The real ``inbound_permitted`` runs here, with the host policy denying
    Signal: with no buttons the typed cancel is the only off-switch, so the
    shared pure-cancel exemption has to see the message text.
    """

    @pytest.fixture(autouse=True)
    def _denied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.messaging import dispatch_admission

        async def _no(_channel: str) -> bool:
            return False

        monkeypatch.setattr(
            "kiro_crew.signal_cli.transport_dispatch.inbound_permitted",
            dispatch_admission.inbound_permitted,
        )
        monkeypatch.setattr(dispatch_admission, "channel_inbound_permitted", _no)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("spelling", ["/stop", "/cancel", " /STOP "])
    async def test_the_operator_s_cancel_still_stops_the_turn(self, spelling: str) -> None:
        sessions = StopSessions(SimpleNamespace(kind="soft"))
        dispatcher, client = _with_sessions(sessions)
        await dispatcher.handle_message(_inbound(spelling))
        assert sessions.stopped == [(dispatcher._session_key(OWNER), {})]
        assert client.sent == [(OWNER, STOPPED_TEXT)]

    @pytest.mark.asyncio
    async def test_anything_else_is_still_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        turns: list[Any] = []

        async def fake_drive_turn(turn: Any, **_kwargs: Any) -> None:
            turns.append(turn)

        monkeypatch.setattr("kiro_crew.signal_cli.transport_dispatch.drive_turn", fake_drive_turn)
        sessions = StopSessions(SimpleNamespace(kind="soft"))
        dispatcher, client = _with_sessions(sessions)
        for body in ("/help", "/new", "/stop now", "what's up"):
            await dispatcher.handle_message(_inbound(body))
        assert client.sent == [] and turns == [] and sessions.stopped == []

    @pytest.mark.asyncio
    async def test_a_cancel_carrying_an_attachment_is_dropped(self) -> None:
        sessions = StopSessions(SimpleNamespace(kind="soft"))
        dispatcher, client = _with_sessions(sessions)
        inbound = _inbound("/stop")
        inbound.attachments = 1
        await dispatcher.handle_message(inbound)
        assert client.sent == [] and sessions.stopped == []

    @pytest.mark.asyncio
    async def test_a_non_operator_s_cancel_passes_the_gate_but_not_the_command_table(
        self,
    ) -> None:
        sessions = StopSessions(SimpleNamespace(kind="soft"))
        dispatcher, client = _with_sessions(sessions)
        inbound = _inbound("/stop")
        inbound.operator = False
        await dispatcher.handle_message(inbound)
        assert client.sent == [] and sessions.stopped == []


# ── renderer: the prompt and its timeout ────────────────────────────────────


class FailingClient(FakeClient):
    async def send(self, recipient: str, text: str) -> str:
        raise RpcTransportError("signal-cli is not running")


def _renderer(client: FakeClient, *, key: str | None = "sk") -> SignalRenderer:
    return SignalRenderer(
        client,  # type: ignore[arg-type]
        OWNER,
        SIGNAL_CAPABILITIES,
        session_key="sk",
        approval_session_key=key,
    )


class TestRendererPrompt:
    @pytest.mark.asyncio
    async def test_the_prompt_is_numbered_plain_text_and_opens_the_entry(self) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_prompt_choice([], "r1", tool_title="Run shell", tool_purpose="list files")
        assert claim_approval("sk", "r1") is not None
        assert pending_for("sk", OWNER) is not None
        assert pending_for("sk", "+447700900099") is None  # bound to this chat
        [(to, body)] = client.sent
        assert to == OWNER
        assert body.startswith(PROMPT_MARKER)
        assert "Tool: Run shell" in body and "Purpose: list files" in body
        assert "1. Approve once" in body and "3. Trust this session" in body

    @pytest.mark.asyncio
    async def test_the_last_tool_names_a_prompt_that_carries_no_title(self) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_tool_call("tc1", "Read file", tool_purpose="peek")
        await renderer.on_prompt_choice([], "r1")
        assert "Tool: Read file" in client.sent[0][1]

    @pytest.mark.asyncio
    async def test_a_credential_in_the_title_is_not_sent(self) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_prompt_choice([], "r1", tool_title="echo AKIAIOSFODNN7EXAMPLE")
        assert "AKIAIOSFODNN7EXAMPLE" not in client.sent[0][1]

    @pytest.mark.asyncio
    async def test_without_an_approval_route_nothing_is_sent(self) -> None:
        client = FakeClient()
        renderer = _renderer(client, key=None)
        await renderer.on_prompt_choice([], "r1", tool_title="Run shell")
        assert client.sent == [] and claim_approval("sk", "r1") is None

    @pytest.mark.asyncio
    async def test_a_prompt_that_could_not_be_sent_leaves_nothing_to_answer(self) -> None:
        renderer = _renderer(FailingClient())
        with pytest.raises(RpcTransportError):
            await renderer.on_prompt_choice([], "r1", tool_title="Run shell")
        assert claim_approval("sk", "r1") is None

    @pytest.mark.asyncio
    async def test_the_timeout_is_announced_and_denies(self) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_prompt_choice([], "r1", tool_title="Run shell")
        decider = approval.TextReplyApprovalDecider("sk", timeout_s=0.0)
        assert await decider(SimpleNamespace(request_id="r1")) is False
        assert client.sent[-1] == (OWNER, TIMEOUT_NOTICE)

    @pytest.mark.asyncio
    async def test_an_undeliverable_timeout_notice_still_denies(self) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_prompt_choice([], "r1", tool_title="Run shell")
        renderer._client = FailingClient()  # type: ignore[assignment]
        decider = approval.TextReplyApprovalDecider("sk", timeout_s=0.0)
        assert await decider(SimpleNamespace(request_id="r1")) is False


# ── end to end against the fake signal-cli ──────────────────────────────────


def _receive(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["params"]


def _dm_reply(body: str) -> dict[str, Any]:
    """The owner's DM answer to the bot number, as a later message."""
    params = copy.deepcopy(_receive("receive_dm"))
    envelope = params["envelope"]
    envelope["timestamp"] = envelope["dataMessage"]["timestamp"] = 1700000000700
    envelope["dataMessage"]["message"] = body
    return params


def _note_to_self_reply(body: str) -> dict[str, Any]:
    """The owner's Note to Self answer, synced from their phone."""
    params = copy.deepcopy(_receive("receive_note_to_self"))
    envelope = params["result"]["envelope"]
    envelope["timestamp"] = envelope["syncMessage"]["sentMessage"]["timestamp"] = 1700000000700
    envelope["syncMessage"]["sentMessage"]["message"] = body
    return params


class _Sessions(MirrorStore):
    """The SessionManager slice the dispatcher touches before ``drive_turn``."""

    def __init__(self) -> None:
        self.busy: set[str] = set()

    def is_busy(self, key: str) -> bool:
        return key in self.busy

    def max_generation(self, *_args: object, **_kwargs: object) -> int:
        return 0

    def list_sessions(self) -> list[str]:
        return []

    def get_approval_policy(self, key: str) -> str:
        return ""


class _State:
    def __init__(self) -> None:
        self.signal_connected = False
        self.signal_connect_error = ""

    def register_channel_transport(self, transport: Any) -> None:
        return None


def _orch(allowed: list[str]) -> Any:
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig()
    object.__setattr__(
        cfg,
        "signal",
        SimpleNamespace(
            enabled=True,
            account=ACCOUNT,
            allowed_numbers=allowed,
            soft_threshold_pct=80,
            hard_threshold_pct=95,
        ),
    )
    return SimpleNamespace(
        _signal_enabled=True,
        _cfg=cfg,
        _approval_mode="interactive",
        sessions=_Sessions(),
        ctx_builder=object(),
        conv_log=None,
        dashboard_state=_State(),
    )


@pytest.fixture
def fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    if os.name == "nt":
        pytest.skip("the fake binary is a POSIX wrapper")
    staged = tmp_path / "signal-cli.staged"
    staged.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n', encoding="utf-8")
    staged.chmod(staged.stat().st_mode | stat.S_IXUSR)
    binary = tmp_path / "signal-cli"
    staged.rename(binary)
    paths = {
        "log": tmp_path / "requests.jsonl",
        "notify": tmp_path / "notify.json",
        "replies": tmp_path / "replies.json",
    }
    paths["notify"].write_text("[]", encoding="utf-8")
    paths["replies"].write_text("[]", encoding="utf-8")
    monkeypatch.setenv(ENV_OVERRIDE, str(binary))
    monkeypatch.setenv("FAKE_SIGNAL_LOG", str(paths["log"]))
    monkeypatch.setenv("FAKE_SIGNAL_NOTIFY", str(paths["notify"]))
    monkeypatch.setenv("FAKE_SIGNAL_REPLIES", str(paths["replies"]))
    monkeypatch.delenv("FAKE_SIGNAL_MODE", raising=False)
    monkeypatch.delenv("FAKE_SIGNAL_ECHO_NOTE_TO_SELF", raising=False)
    return paths


class _Harness:
    """Counts handled ``receive`` notifications and finished turns."""

    def __init__(self) -> None:
        self.receives = 0
        self.receive_target = 0
        self.receives_done = asyncio.Event()
        self.turn_done = asyncio.Event()
        self.turns: list[Any] = []
        self.providers: list[ScriptedProvider] = []


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, fake_cli: dict[str, Path]) -> _Harness:
    box = _Harness()
    original = transport_mod.SignalTransport.receive

    async def counting(self: Any, raw: Any) -> None:
        try:
            await original(self, raw)
        finally:
            box.receives += 1
            if box.receive_target and box.receives >= box.receive_target:
                box.receives_done.set()

    async def drive_with_a_tool(turn: Any, **_kwargs: Any) -> None:
        """The real TurnDriver, over a provider that needs one tool approved."""
        box.turns.append(turn)
        provider = ScriptedProvider(
            [
                tool_call("tc1", "Run shell"),
                permission("r1", "Run shell"),
                text("**Done.**"),
                complete(),
            ]
        )
        box.providers.append(provider)
        driver = TurnDriver(
            provider,
            turn.renderer,
            approval_mode=turn.approval_mode,
            decider=turn.decider,
            auto_approve_session=turn.auto_approve_session,
        )
        busy_key = turn.session_key
        sessions = _kwargs.get("sessions")
        if sessions is not None:
            sessions.busy.add(busy_key)
        try:
            await turn.renderer.on_turn_start()
            await driver.run(turn.user_text)
        finally:
            await turn.renderer.close()
            if sessions is not None:
                sessions.busy.discard(busy_key)
            box.turn_done.set()

    monkeypatch.setattr(transport_mod.SignalTransport, "receive", counting)
    monkeypatch.setattr("kiro_crew.signal_cli.transport_dispatch.drive_turn", drive_with_a_tool)
    return box


@pytest.fixture
def spawned(request: pytest.FixtureRequest) -> list[Any]:
    """``test_signal_e2e``'s spawned-children record, reached by name (imported above)."""
    return request.getfixturevalue("children")


def _sends(log: Path) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
    return [r["params"] for r in rows if r.get("method") == "send"]


async def _run(orch: Any, box: _Harness, receives: int, *, timeout: float | None = None) -> Any:
    """Boot, then wait for the turn and *receives*; the caller closes the client.

    A wait that fails closes the client (and so its signal-cli child) itself,
    because the caller's ``try``/``finally`` has not been entered yet.
    """
    box.receive_target = receives
    client = await gw.maybe_start_signal(orch)
    assert client is not None, orch.dashboard_state.signal_connect_error
    try:
        await await_until(box.turn_done, timeout=timeout, what="the turn finishes")
        await await_until(
            box.receives_done, timeout=timeout, what=f"{receives} receive notification(s) handled"
        )
    except BaseException:
        await client.close()
        raise
    return client


def _bodies(sends: list[dict[str, Any]]) -> list[str]:
    return [s["message"] for s in sends]


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_a_failed_wait_still_closes_the_child(
        self, fake_cli: dict[str, Path], harness: _Harness, spawned: list[Any]
    ) -> None:
        # No message is queued, so the turn never runs and the wait fails: the
        # child must be closed anyway, or it outlives the failed test.
        try:
            with pytest.raises(AssertionError, match="the turn finishes"):
                await _run(_orch([OWNER]), harness, receives=1, timeout=0)
            assert len(spawned) == 1
            assert spawned[0].returncode is not None, "the child outlived the failed wait"
        finally:
            await _reap(spawned)

    @pytest.mark.asyncio
    async def test_a_dm_to_the_bot_number_approves_with_1_and_the_tool_runs(
        self, fake_cli: dict[str, Path], harness: _Harness
    ) -> None:
        fake_cli["notify"].write_text(json.dumps([_receive("receive_dm")]), encoding="utf-8")
        fake_cli["replies"].write_text(
            json.dumps([{"after": PROMPT_MARKER, "receive": _dm_reply("1")}]), encoding="utf-8"
        )
        client = await _run(_orch([OWNER]), harness, receives=2)
        try:
            [provider] = harness.providers
            assert provider.approved == ["r1"] and provider.rejected == []
            # One turn: the answer never reached the model as a message.
            assert [t.user_text for t in harness.turns] == ["what's on my calendar?"]
            sends = _sends(fake_cli["log"])
            assert all(s.get("recipient") == [OWNER] for s in sends)
            bodies = _bodies(sends)
            assert bodies[0].startswith(PROMPT_MARKER) and "Tool: Run shell" in bodies[0]
            # The receipt and the answer race; neither order is defined.
            assert sorted(bodies[1:]) == sorted([RECEIPT_APPROVED, "Done."])
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_a_no_denies_the_tool(self, fake_cli: dict[str, Path], harness: _Harness) -> None:
        fake_cli["notify"].write_text(json.dumps([_receive("receive_dm")]), encoding="utf-8")
        fake_cli["replies"].write_text(
            json.dumps([{"after": PROMPT_MARKER, "receive": _dm_reply("no")}]), encoding="utf-8"
        )
        client = await _run(_orch([OWNER]), harness, receives=2)
        try:
            [provider] = harness.providers
            assert provider.approved == [] and provider.rejected == ["r1"]
            assert len(harness.turns) == 1
            assert RECEIPT_DENIED in _bodies(_sends(fake_cli["log"]))
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_no_answer_in_time_denies_and_says_so(
        self, fake_cli: dict[str, Path], harness: _Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("kiro_crew.signal_cli.transport_dispatch.TEXT_APPROVAL_TIMEOUT_S", 0.0)
        fake_cli["notify"].write_text(json.dumps([_receive("receive_dm")]), encoding="utf-8")
        client = await _run(_orch([OWNER]), harness, receives=1)
        try:
            [provider] = harness.providers
            assert provider.approved == [] and provider.rejected == ["r1"]
            bodies = _bodies(_sends(fake_cli["log"]))
            assert bodies[0].startswith(PROMPT_MARKER)
            assert TIMEOUT_NOTICE in bodies
            # Nothing is left open for a late "1" to answer.
            assert approval._PENDING == {}
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_note_to_self_approves_and_no_echo_becomes_a_turn(
        self, fake_cli: dict[str, Path], harness: _Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Every Note to Self send comes back as an echo (another device
        # re-syncing it): the prompt, the receipt and the answer. None of them
        # may start a turn or answer the prompt; the owner's "1" must.
        monkeypatch.setenv("FAKE_SIGNAL_ECHO_NOTE_TO_SELF", "1")
        fake_cli["notify"].write_text(
            json.dumps([_receive("receive_note_to_self")]), encoding="utf-8"
        )
        fake_cli["replies"].write_text(
            json.dumps([{"after": PROMPT_MARKER, "receive": _note_to_self_reply("1")}]),
            encoding="utf-8",
        )
        # initial message + prompt echo + the "1" + receipt echo + answer echo
        client = await _run(_orch([ACCOUNT]), harness, receives=5)
        try:
            [provider] = harness.providers
            assert provider.approved == ["r1"] and provider.rejected == []
            assert [t.user_text for t in harness.turns] == ["remind me to call mum"]
            sends = _sends(fake_cli["log"])
            assert all(s.get("noteToSelf") is True for s in sends)
            bodies = _bodies(sends)
            assert bodies[0].startswith(PROMPT_MARKER)
            assert sorted(bodies[1:]) == sorted([RECEIPT_APPROVED, "Done."])
        finally:
            await client.close()
