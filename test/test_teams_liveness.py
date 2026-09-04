"""Teams renderer liveness: nothing in the chat is allowed to lie about its state.

Three surfaces, one property each, all of them failure modes a user cannot tell
apart from a dead bot:

* the typing indicator, which Teams expires after a few seconds, so ONE activity
  leaves a minutes-long turn silent;
* an approval card whose click window closed, or which never landed at all;
* an ``[OPTIONS:]`` chip card after a pick, or when the card could not be posted
  and the trailer has already been cut out of the answer.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from kiro_crew.teams.approvals import TeamsApprovalDecider
from kiro_crew.teams.client import TEAMS_MAX_TEXT, TeamsSendError
from kiro_crew.teams.renderer import TeamsRenderer
from kiro_crew.teams.transport import TEAMS_CAPABILITIES

_SVC = "https://smba.trafficmanager.net/"


class _Client:
    """Records every outbound call; each failure mode is opt-in."""

    def __init__(self, *, cards_fail: bool = False, typing_fails_after: int | None = None) -> None:
        self.sent: list[str] = []
        self.cards: list[dict[str, Any]] = []
        self.updated_cards: list[tuple[str, dict[str, Any]]] = []
        self.typings = 0
        self.cards_fail = cards_fail
        self.typing_fails_after = typing_fails_after

    async def send_typing(self, conversation_id: str, service_url: str) -> None:
        self.typings += 1
        if self.typing_fails_after is not None and self.typings > self.typing_fails_after:
            raise TeamsSendError("HTTP 429")

    async def send_message(self, conversation_id: str, content: str, service_url: str) -> str:
        self.sent.append(content)
        return f"mid-{len(self.sent)}"

    async def update_message(
        self, conversation_id: str, activity_id: str, content: str, service_url: str
    ) -> bool:
        self.sent.append(content)
        return True

    async def send_card(self, conversation_id: str, card: dict, service_url: str) -> str:
        if self.cards_fail:
            raise TeamsSendError("HTTP 502")
        self.cards.append(card)
        return f"card-{len(self.cards)}"

    async def update_card(
        self, conversation_id: str, activity_id: str, card: dict, service_url: str
    ) -> bool:
        self.updated_cards.append((activity_id, card))
        return True


def _renderer(client: Any, decider: Any = None) -> TeamsRenderer:
    return TeamsRenderer(
        client,
        "conv-1",
        _SVC,
        TEAMS_CAPABILITIES,
        session_key="teams:a:direct:u",
        decider=decider,
    )


def _actions(card: dict[str, Any]) -> list[Any]:
    return list(card["content"].get("actions") or [])


class TestTypingKeepalive:
    @pytest.mark.asyncio
    async def test_the_indicator_is_refreshed_while_the_turn_runs(self, monkeypatch) -> None:
        """A turn that never calls a tool has no progress bubble to fall back on."""
        monkeypatch.setattr("kiro_crew.teams.renderer._TYPING_REFRESH_S", 0.01)
        client = _Client()
        renderer = _renderer(client)

        await renderer.on_turn_start()
        assert client.typings == 1, "immediate feedback, before any refresh"
        await asyncio.sleep(0.06)
        assert client.typings > 1, "one activity would leave a long turn silent"

        await renderer.on_done()

    @pytest.mark.asyncio
    async def test_the_refresh_stops_when_the_turn_ends(self, monkeypatch) -> None:
        """An orphaned loop would post into a finished chat for the process lifetime."""
        monkeypatch.setattr("kiro_crew.teams.renderer._TYPING_REFRESH_S", 0.01)
        client = _Client()
        renderer = _renderer(client)

        await renderer.on_turn_start()
        await asyncio.sleep(0.03)
        await renderer.on_done()
        settled = client.typings
        await asyncio.sleep(0.05)

        assert client.typings == settled

    @pytest.mark.asyncio
    async def test_close_stops_the_refresh_even_without_on_done(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.teams.renderer._TYPING_REFRESH_S", 0.01)
        client = _Client()
        renderer = _renderer(client)

        await renderer.on_turn_start()
        await renderer.close()
        settled = client.typings
        await asyncio.sleep(0.05)

        assert client.typings == settled

    @pytest.mark.asyncio
    async def test_a_failed_refresh_costs_one_beat_not_the_turn(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.teams.renderer._TYPING_REFRESH_S", 0.01)
        client = _Client(typing_fails_after=1)
        renderer = _renderer(client)

        await renderer.on_turn_start()
        await asyncio.sleep(0.05)
        await renderer.on_text_chunk("done thinking")
        await renderer.on_done()

        assert client.typings > 1, "it kept trying"
        assert "done thinking" in client.sent


class TestAnUndeliveredAnswerIsNotRecordedAsDelivered:
    """`drive_turn` treats a clean return from the renderer as delivery.

    So swallowing a Connector refusal here is what makes the gateway run
    `record_success` and persist the FULL answer while the user received part of it or
    none. Raising skips both and records a failure -- which the user can retry, where a
    turn silently recorded as complete cannot even be noticed.
    """

    @pytest.mark.asyncio
    async def test_a_refused_chunk_raises_rather_than_returning(self) -> None:
        class _RefusingClient(_Client):
            async def send_message(self, conversation_id: str, content: str, svc: str) -> str:
                raise TeamsSendError("HTTP 502")

        renderer = _renderer(_RefusingClient())
        await renderer.on_text_chunk("the answer the user never saw")

        with pytest.raises(TeamsSendError):
            await renderer.on_done()

    @pytest.mark.asyncio
    async def test_a_partially_delivered_answer_still_raises(self) -> None:
        """The delivered prefix does not make the turn a success."""

        class _HalfClient(_Client):
            async def send_message(self, conversation_id: str, content: str, svc: str) -> str:
                self.sent.append(content)
                if len(self.sent) >= 2:
                    raise TeamsSendError("HTTP 413")
                return f"mid-{len(self.sent)}"

        client = _HalfClient()
        renderer = _renderer(client)
        # Two chunks: the first lands, the second is refused.
        await renderer.on_text_chunk("A" * (TEAMS_MAX_TEXT + 500))

        with pytest.raises(TeamsSendError):
            await renderer.on_done()
        assert len(client.sent) >= 1, "the prefix really was delivered"

    @pytest.mark.asyncio
    async def test_a_delivered_answer_returns_normally(self) -> None:
        client = _Client()
        renderer = _renderer(client)
        await renderer.on_text_chunk("delivered fine")

        await renderer.on_done()

        assert "delivered fine" in client.sent


class TestApprovalCardLiveness:
    @pytest.mark.asyncio
    async def test_an_expired_prompts_card_is_replaced(self, monkeypatch) -> None:
        """A chat must never accumulate buttons that resolve to nothing."""
        monkeypatch.setattr("kiro_crew.teams.approvals.APPROVAL_TIMEOUT_SECS", 0.01)
        client = _Client()
        decider = TeamsApprovalDecider(session_key="teams:a:direct:u")
        renderer = _renderer(client, decider)

        await renderer.on_prompt_choice([{"title": "fs_read"}], "7")
        assert _actions(client.cards[0]), "the prompt was offered with buttons"

        assert await decider(type("E", (), {"request_id": "7"})()) is False

        assert client.updated_cards, "the expired card must be settled"
        _activity_id, settled = client.updated_cards[-1]
        assert _actions(settled) == []
        assert "expired" in str(settled)

    @pytest.mark.asyncio
    async def test_a_card_that_never_landed_denies_at_once_and_says_so(self) -> None:
        """Parking the turn for the full window behind an invisible card is worse."""
        client = _Client(cards_fail=True)
        decider = TeamsApprovalDecider(session_key="teams:a:direct:u")
        renderer = _renderer(client, decider)

        await renderer.on_prompt_choice([{"title": "fs_write"}], "7")

        # Denied immediately -- no wait, and no reliance on the timeout.
        assert await decider(type("E", (), {"request_id": "7"})()) is False
        assert client.sent and "fs_write" in client.sent[-1]
        assert "not run" in client.sent[-1]

    @pytest.mark.asyncio
    async def test_a_delivered_card_whose_id_teams_withheld_still_waits(self, monkeypatch) -> None:
        """A withheld id and a failed post both read as an empty string.

        Only the second may deny -- treating the first as a failure would refuse a
        prompt the user is looking at.
        """

        class _IdlessClient(_Client):
            async def send_card(self, conversation_id: str, card: dict, service_url: str) -> str:
                self.cards.append(card)
                return ""

        monkeypatch.setattr("kiro_crew.teams.approvals.APPROVAL_TIMEOUT_SECS", 0.01)
        client = _IdlessClient()
        decider = TeamsApprovalDecider(session_key="teams:a:direct:u")
        renderer = _renderer(client, decider)

        await renderer.on_prompt_choice([{"title": "fs_read"}], "7")
        assert client.cards, "the card WAS delivered"
        assert client.sent == [], "so the user is told nothing about a failure"
        # It resolves on the deadline like any other unanswered prompt.
        assert await decider(type("E", (), {"request_id": "7"})()) is False


class TestOptionChipLiveness:
    @pytest.mark.asyncio
    async def test_a_pick_replaces_the_chips_with_the_choice(self) -> None:
        client = _Client()
        renderer = _renderer(client)
        await renderer.on_text_chunk("Which one?\n\n[OPTIONS: red | blue]")
        await renderer.on_done()
        assert _actions(client.cards[-1]), "chips were offered"

        await renderer.settle_options("red")

        _activity_id, settled = client.updated_cards[-1]
        assert _actions(settled) == [], "no chip may still look live after a pick"
        assert "red" in str(settled), "the transcript must record which one was picked"
        assert renderer.has_pending_choices is False, "and the renderer is free to retire"

    @pytest.mark.asyncio
    async def test_a_failed_chips_card_degrades_to_a_numbered_list(self) -> None:
        """The trailer is already cut from the body, so silence loses the choices."""
        client = _Client(cards_fail=True)
        renderer = _renderer(client)

        await renderer.on_text_chunk("Which one?\n\n[OPTIONS: red | blue]")
        await renderer.on_done()

        assert client.cards == []
        listed = client.sent[-1]
        assert "1. red" in listed and "2. blue" in listed
        # No nonce, so a later click cannot resolve against a card that never existed
        # -- and the renderer is not pinned alive waiting for one.
        assert renderer.has_pending_choices is False

    @pytest.mark.asyncio
    async def test_settling_twice_is_harmless(self) -> None:
        client = _Client()
        renderer = _renderer(client)
        await renderer.on_text_chunk("[OPTIONS: red | blue]")
        await renderer.on_done()

        await renderer.settle_options("red")
        before = len(client.updated_cards)
        await renderer.settle_options("red")

        assert len(client.updated_cards) == before

    @pytest.mark.asyncio
    async def test_a_chip_label_is_display_redacted_before_it_is_echoed(self) -> None:
        """The label came from the model, and a settled card renders it as text."""
        client = _Client()
        renderer = _renderer(client)
        await renderer.on_text_chunk("[OPTIONS: keep | drop]")
        await renderer.on_done()

        await renderer.settle_options("AKIAIOSFODNN7EXAMPLE")

        _activity_id, settled = client.updated_cards[-1]
        assert "AKIAIOSFODNN7EXAMPLE" not in str(settled)
