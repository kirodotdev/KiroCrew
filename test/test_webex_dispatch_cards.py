"""WebexDispatcher's Adaptive Card presses (option and approval cards, the governance check on a
press) and the outbound display scan every reply passes through: press echoes, queue receipts
and the minted dashboard link's exemption.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest import mock

import pytest
from webex_dispatch_helpers import (
    _EMAIL,
    FakeClient,
    FakeCtx,
    FakeProvider,
    FakeSessions,
    _cfg_queue,
    _dispatcher,
    _inbound,
    _spin,
)

from kiro_crew.acp.types import EVENT_COMPLETE, AcpEvent
from kiro_crew.webex import cards
from kiro_crew.webex import transport_dispatch as webex_dispatch
from kiro_crew.webex.client import WebexInbound
from kiro_crew.webex.transport_dispatch import WebexDispatcher

# ------------------------------------------------------------------
# Tests: Adaptive Card presses
# ------------------------------------------------------------------


def _press(
    inputs: dict,
    *,
    email: str = _EMAIL,
    room_type: str = "direct",
    room_id: str = "ROOM",
) -> WebexInbound:
    """A card-press envelope.

    Shaped exactly like a message envelope — including ``room_type``, which the
    client resolves before dispatch because an attachment-action record carries
    none and the room gate is a type decision.
    """
    return WebexInbound(
        person_email=email,
        room_id=room_id,
        text="",
        room_type=room_type,
        card_inputs=inputs,
    )


def _options_press(index: str, nonce: str, **kw) -> WebexInbound:
    return _press(
        {
            cards.KEY_KIND: cards.KIND_OPTIONS,
            cards.KEY_CHOICE: index,
            cards.KEY_NONCE: nonce,
        },
        **kw,
    )


def _approval_press(choice: str, nonce: str, request_id: str = "1", **kw) -> WebexInbound:
    return _press(
        {
            cards.KEY_KIND: cards.KIND_APPROVAL,
            cards.KEY_CHOICE: choice,
            cards.KEY_NONCE: nonce,
            cards.KEY_REQUEST: request_id,
        },
        **kw,
    )


class TestOptionsCardPress:
    @pytest.mark.asyncio
    async def test_a_press_runs_the_choice_as_a_turn_and_echoes_it(self) -> None:
        """The card is only useful if a press reaches the model.

        It is published by a renderer that is gone by the time the press arrives —
        the card is the LAST thing a turn sends — so the store has to outlive the
        turn or every press gets the stale-card reply.
        """
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)
        key = d._session_key(_EMAIL)
        d._choices.publish(key, "N1", ["Keep going", "Stop"])
        turns: list = []

        async def _capture(turn, *, sessions, ctx_builder):
            turns.append(turn.user_text)

        with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _capture):
            await d.handle_message(_options_press("0", "N1"))

        assert turns == ["Keep going"]
        # Echoed: a press leaves no trace in the room, so without this the answer
        # arrives with nothing above it saying which option it answers.
        assert client.sent[0][1] == "> Keep going"

    @pytest.mark.asyncio
    async def test_a_choice_that_looks_like_a_command_is_not_executed(self) -> None:
        """A choice LABEL is model-authored text.

        `[OPTIONS: Keep going | /yolo on]` would otherwise render a button whose
        single press takes the process-global auto-approve grant, and any label
        starting with `/` would be answered with the unknown-command card and the
        choice dropped.
        """
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)
        key = d._session_key(_EMAIL)
        d._choices.publish(key, "N1", ["/yolo on"])
        before = d._conv.current_gen(_EMAIL)
        turns: list = []

        async def _capture(turn, *, sessions, ctx_builder):
            turns.append(turn.user_text)

        with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _capture):
            await d.handle_message(_options_press("0", "N1"))

        assert turns == ["/yolo on"]
        assert d._conv.current_gen(_EMAIL) == before
        assert not any("YOLO" in m for (_, m) in client.sent)

    @pytest.mark.asyncio
    async def test_a_stale_press_is_told_so_and_runs_nothing(self) -> None:
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)
        turns: list = []

        async def _capture(turn, *, sessions, ctx_builder):
            turns.append(turn.user_text)

        with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _capture):
            await d.handle_message(_options_press("0", "NOPE"))

        assert turns == []
        assert "no longer current" in client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_an_unauthorized_press_is_dropped_and_audited(self) -> None:
        """A card lives in a room and Webex lets anyone in that room press it.

        The transport's ROOM gate has already run; this is the sender check, which
        for a press has to resolve a person id to an email first.
        """
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)
        key = d._session_key("intruder@example.com")
        d._choices.publish(key, "N1", ["Keep going"])

        with mock.patch("kiro_crew.webex.transport_dispatch.sel") as fake_sel:
            await d.handle_message(_options_press("0", "N1", email="intruder@example.com"))

        assert client.sent == []
        outcomes = [c.kwargs["outcome"] for c in fake_sel.return_value.log_api_access.mock_calls]
        assert "denied" in outcomes

    @pytest.mark.asyncio
    async def test_a_press_that_is_not_ours_is_ignored(self) -> None:
        client = FakeClient()
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), client)
        await d.handle_message(_press({"unrelated": "x"}))
        assert client.sent == []


#: A presigned dashboard URL at its REAL length. A live token is ~218 characters
#: (base64url payload + HMAC signature), well past the exfiltration redactor's
#: 200-character query threshold — so a token short enough to slip under it would
#: make these tests pass while every real link stayed broken.
_DASHBOARD_LINK = "http://127.0.0.1:8765/?token=" + ("a1b2c3d4" * 27)


class TestOutboundRedaction:
    """Not everything the dispatcher sends is its own copy.

    A card press echoes a MODEL-authored label, `/sessions` prints titles that are
    the opening words of user messages, and a queue receipt quotes the message it
    queued — so the display scan sits at the choke point rather than at whichever
    call site remembered.
    """

    @pytest.mark.asyncio
    async def test_an_option_echo_cannot_reassemble_a_credential(self) -> None:
        """The choice text is model-authored.

        `AKIA**IOSF**ODNN7EXAMPLE` does not match a credential pattern as written,
        and Webex's own renderer removes the emphasis and shows the intact key.
        """
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        client = FakeClient()
        d = _dispatcher(FakeSessions(provider), FakeCtx(), client)
        key = d._session_key(_EMAIL)
        d._choices.publish(key, "N1", ["AKIA**IOSF**ODNN7EXAMPLE"])

        async def _capture(turn, *, sessions, ctx_builder):
            pass

        with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _capture):
            await d.handle_message(_options_press("0", "N1"))

        echo = client.sent[0][1]
        assert "AKIAIOSFODNN7EXAMPLE" not in echo.replace("*", "")

    @pytest.mark.asyncio
    async def test_our_own_markdown_survives_the_scan(self) -> None:
        # The scan only downgrades markup when the canonical form actually reveals
        # a credential, so /help keeps its backticks.
        client = FakeClient()
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), client)

        await d.handle_message(_inbound("/help"))

        assert "`/new`" in client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_minted_dashboard_link_is_not_redacted(self) -> None:
        """The scan is RIGHT about this text, which is why the exemption exists.

        A presigned dashboard URL looks exactly like the credential-bearing link
        the exfiltration redactor is built to catch, so scanning it would deliver a
        login link that cannot log in.
        """
        client = FakeClient()
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), client)

        await d._reply(_inbound("x"), _DASHBOARD_LINK, self_minted=True)

        assert client.sent[-1][1] == _DASHBOARD_LINK

    @pytest.mark.asyncio
    async def test_the_exemption_is_not_the_default(self) -> None:
        # The same URL from an unexempt caller is still redacted, so the exemption
        # cannot be inherited by anything that forgot to think about it.
        client = FakeClient()
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), client)

        await d._reply(_inbound("x"), _DASHBOARD_LINK)

        assert client.sent[-1][1] != _DASHBOARD_LINK
        assert "REDACTED" in client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_queue_receipt_scans_the_text_it_quotes(self) -> None:
        provider = FakeProvider([])
        sessions = FakeSessions(provider)
        sessions._busy = True
        sessions.queued = []
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)
        cfg = _cfg_queue()
        d.cfg = cfg

        await d.handle_message(_inbound("also AKIA**IOSF**ODNN7EXAMPLE"))

        bodies = " ".join(m for (_c, m) in client.sent)
        assert "AKIAIOSFODNN7EXAMPLE" not in bodies.replace("*", "")


class TestApprovalCardPress:
    @staticmethod
    async def _pending(d: WebexDispatcher, key: str, request_id: int = 1):
        task = asyncio.create_task(
            webex_dispatch._APPROVALS.decide_with_cause(key, SimpleNamespace(request_id=request_id))
        )
        await _spin(lambda: webex_dispatch._APPROVALS.has_pending(key))
        return task

    @pytest.mark.asyncio
    async def test_a_press_with_the_minted_nonce_resolves_the_decision(self) -> None:
        client = FakeClient()
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), client)
        key = d._session_key(_EMAIL)
        task = await self._pending(d, key)
        nonce = webex_dispatch._APPROVALS.reserve(key, "1")

        await d.handle_message(_approval_press("approve", nonce))

        # flake-ok: pre-existing await on a decision task the step above resolves, moved verbatim
        assert (await task)[0] is True
        assert "Approved" in client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_forged_nonce_cannot_resolve_the_decision(self) -> None:
        """The guard runs INSIDE resolve, as a precondition.

        Checking it around the resolve call would approve the tool first and only
        then discover the press was stale — by which point the only thing left to
        suppress is the confirmation message.
        """
        client = FakeClient()
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), client)
        key = d._session_key(_EMAIL)
        task = await self._pending(d, key)
        webex_dispatch._APPROVALS.reserve(key, "1")

        await d.handle_message(_approval_press("approve", "FORGED"))

        assert webex_dispatch._APPROVALS.has_pending(key)
        assert "no longer current" in client.sent[-1][1]
        assert webex_dispatch._APPROVALS.resolve(key, False) is True
        # flake-ok: pre-existing await on a decision task the step above resolves, moved verbatim
        assert (await task)[0] is False

    @pytest.mark.asyncio
    async def test_a_press_carrying_no_nonce_fails_closed(self) -> None:
        # Every real press echoes the nonce we minted, so its absence is either a
        # forgery or a card from a build that predates it. An empty nonce means
        # "typed answer" to the registry and would skip the guard entirely.
        client = FakeClient()
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), client)
        key = d._session_key(_EMAIL)
        task = await self._pending(d, key)
        webex_dispatch._APPROVALS.reserve(key, "1")

        await d.handle_message(_approval_press("approve", ""))

        assert webex_dispatch._APPROVALS.has_pending(key)
        webex_dispatch._APPROVALS.resolve(key, False)
        # flake-ok: pre-existing await on a decision task the step above resolves, moved verbatim
        assert (await task)[0] is False

    @pytest.mark.asyncio
    async def test_a_second_press_reports_neutrally_rather_than_denied(self) -> None:
        """Webex cannot retire a card that carries an attachment.

        So the buttons stay clickable after the decision resolved. Telling the user
        their APPROVED tool was denied is worse than telling them nothing changed.
        """
        client = FakeClient()
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), client)
        key = d._session_key(_EMAIL)
        task = await self._pending(d, key)
        nonce = webex_dispatch._APPROVALS.reserve(key, "1")

        await d.handle_message(_approval_press("approve", nonce))
        # flake-ok: pre-existing await on a decision task the step above resolves, moved verbatim
        assert (await task)[0] is True
        await d.handle_message(_approval_press("approve", nonce))

        assert "already answered or timed out" in client.sent[-1][1]
        assert "denied" not in client.sent[-1][1].lower()

    @pytest.mark.asyncio
    async def test_every_press_outcome_is_audited(self) -> None:
        # A forged or replayed press leaves no other trace: the reply it draws is
        # indistinguishable from the one a genuinely expired card draws.
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        key = d._session_key(_EMAIL)
        task = await self._pending(d, key)
        nonce = webex_dispatch._APPROVALS.reserve(key, "1")

        with mock.patch("kiro_crew.webex.transport_dispatch.sel") as fake_sel:
            await d.handle_message(_approval_press("deny", "FORGED"))
            await d.handle_message(_approval_press("deny", nonce))

        # flake-ok: pre-existing await on a decision task the step above resolves, moved verbatim
        assert (await task)[0] is False
        operations = [
            c.kwargs["operation"] for c in fake_sel.return_value.log_api_access.mock_calls
        ]
        assert operations.count("webex.tool_approval") == 2

    @pytest.mark.asyncio
    async def test_a_refused_press_never_audits_as_an_approval(self) -> None:
        """The audit LABEL follows the resolve, not the button that was pressed.

        A press the registry refused — stale nonce, wrong request id — decided
        nothing, so recording the decision it ASKED for would put an approval in
        the SEL log that no tool ever received. That record is the only trace a
        forged or replayed press leaves, and the reply it draws is the same one a
        genuinely expired card draws, so nothing else would contradict it.
        """
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        key = d._session_key(_EMAIL)
        task = await self._pending(d, key)
        nonce = webex_dispatch._APPROVALS.reserve(key, "1")

        with mock.patch("kiro_crew.webex.transport_dispatch.sel") as fake_sel:
            await d.handle_message(_approval_press("approve", "FORGED"))
            # Still open: the refused press must not have spent the decision.
            assert webex_dispatch._APPROVALS.has_pending(key)
            await d.handle_message(_approval_press("approve", nonce))

        # flake-ok: pre-existing await on a decision task the step above resolves, moved verbatim
        assert (await task)[0] is True
        outcomes = [
            c.kwargs["outcome"]
            for c in fake_sel.return_value.log_api_access.mock_calls
            if c.kwargs.get("operation") == "webex.tool_approval"
        ]
        assert outcomes == ["denied", "approved"]

    @pytest.mark.asyncio
    async def test_governance_blocks_an_approve_but_still_resolves_a_deny(self) -> None:
        """A policy that forbids this channel has no interest in keeping a tool
        request alive for its whole window."""
        client = FakeClient()
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), client)
        key = d._session_key(_EMAIL)
        task = await self._pending(d, key)
        nonce = webex_dispatch._APPROVALS.reserve(key, "1")

        async def _denied(_channel: str) -> bool:
            return False

        with mock.patch("kiro_crew.webex.transport_dispatch.inbound_permitted", _denied):
            await d.handle_message(_approval_press("approve", nonce))
            assert webex_dispatch._APPROVALS.has_pending(key)
            await d.handle_message(_approval_press("deny", nonce))

        # flake-ok: pre-existing await on a decision task the step above resolves, moved verbatim
        assert (await task)[0] is False


class TestGovernanceOnPresses:
    @pytest.mark.asyncio
    async def test_a_policy_denied_channel_drops_a_choice_whole(self) -> None:
        """A choice is turn CONTENT.

        `drive_turn` would drop the turn anyway, so echoing the press would claim
        the channel is answering when nothing will.
        """
        client = FakeClient()
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), client)
        key = d._session_key(_EMAIL)
        d._choices.publish(key, "N1", ["Keep going"])

        async def _denied(_channel: str) -> bool:
            return False

        with mock.patch("kiro_crew.webex.transport_dispatch.inbound_permitted", _denied):
            await d.handle_message(_options_press("0", "N1"))

        assert client.sent == []
