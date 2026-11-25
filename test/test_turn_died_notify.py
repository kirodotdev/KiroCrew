"""Behaviour coverage for the bell note a DEAD chat turn produces.

When a turn dies with an error the user must act on -- a provider 5xx /
throttle, a content-filter refusal, or a terminal transport death -- the only
trace in the transcript is the inline error card, so an operator working in
another session would learn work has stopped only by opening it. The producer
closes that gap. The properties worth holding: the note rides the one
notification-bus publish path on its own ``system.turn`` channel with a deep
link to the session; it is deduped per session and attributed so a muted
session is silenced; an app-owned chat is left to the app; and a delivery
failure never disturbs the dying turn.

The producer is composed onto the runner's namespace (``chat_turn.compose``), so
it is exercised through ``chat_runner._notify_turn_died`` -- where ``quote``,
``logger``, ``payload_from_legacy`` and the bus resolve -- as the terminal arms
call it. It builds the payload and pushes it on ``state.notification_bus``
directly, the way every other legacy producer stacks a note, so these drive a
real :class:`NotificationBus` and read the delivered note dict. The error card's
choice of note is driven through a real turn (``turn_harness.run_turn``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, get_args

import pytest
from turn_harness import Raise, TurnContext, TurnScript, run_turn

import kiro_crew.dashboard.chat_runner as cr
from kiro_crew.acp.kas_route import REASON_NO_KIRO_CLI, KasRoute
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.agent_sdk import host_auth
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.dashboard.chat_utils_injected_rows import AUTH_REQUIRED_KIND, KIRO_CLI_SIGN_IN_KIND
from kiro_crew.notifications.bus import SYSTEM_CHANNELS, NotificationBus

_EN_CATALOG = Path(__file__).parent.parent / "website/src/i18n/locales/en.manual.json"


class FakeSlot:
    def __init__(self, key: str = "chat-1-123", app: str = "") -> None:
        self.key = key
        self._app = app


class FakeTurn:
    def __init__(self, slot: FakeSlot, state: Any, session_key: str = "sid-1") -> None:
        self.slot = slot
        self.state = state
        self.session_key = session_key


class BusState:
    """A state carrying a real bus whose sink records the delivered note dict.
    ``fail`` makes the sink raise, so a delivery failure can be exercised."""

    def __init__(self, *, fail: bool = False) -> None:
        self.delivered: list[dict[str, Any]] = []

        def _sink(note: dict[str, Any]) -> None:
            if fail:
                raise RuntimeError("delivery on fire")
            self.delivered.append(note)

        self.notification_bus = NotificationBus(_sink)


def _fire(
    *, app: str = "", key: str = "chat-1-123", session_key: str = "sid-1"
) -> list[dict[str, Any]]:
    state = BusState()
    cr._notify_turn_died(FakeTurn(FakeSlot(key, app), state, session_key), "chat_stopped_error")
    return state.delivered


class TestTheChannelExists:
    def test_system_turn_is_a_registered_system_channel(self) -> None:
        assert "system.turn" in SYSTEM_CHANNELS
        # Default priority: a dead turn is a bell entry, not a blocking prompt.
        assert SYSTEM_CHANNELS["system.turn"] == "default"


class TestItNotifiesAHumanSession:
    def test_one_note_lands_on_the_turn_channel(self) -> None:
        note = _fire()[0]
        assert note["channel"] == "system.turn"
        # kind is "agent", NOT "turn": "turn" is the conversation-ready sound
        # category, so the channel (not a hand-back chime) governs the sound.
        assert note["kind"] == "agent"
        assert note["body"] == "The chat stopped with an error. Open the chat to see what happened."
        assert note["priority"] == "default"

    def test_it_names_the_dying_slot_so_the_banner_scopes_to_it(self) -> None:
        # Without slot the banner gate strips the /chat?sid= query and matches
        # the bare /chat route, suppressing the banner on any chat page. The
        # slot merges flat onto the note (meta key, not reserved).
        assert _fire(key="chat-7-99")[0]["slot"] == "chat-7-99"

    def test_the_title_avoids_the_turn_jargon(self) -> None:
        assert _fire()[0]["title"] == "⚠️ A chat stopped"

    @pytest.mark.parametrize("note_id", get_args(cr._TurnDiedNote))
    def test_it_names_its_dashboard_copy_whose_english_it_carries(self, note_id: str) -> None:
        # The bell renders the catalog copy for the id in the reader's language,
        # and the stored English for every reader with no catalog: the two agree.
        state = BusState()
        cr._notify_turn_died(FakeTurn(FakeSlot(), state), note_id)
        note = state.delivered[0]
        catalog = json.loads(_EN_CATALOG.read_text(encoding="utf-8"))
        copy = catalog["components"]["notifications"]["systemNote"]
        assert note["i18n"] == {"id": note_id}
        assert note["title"] == copy["chat_stopped_title"]
        assert note["body"] == copy[f"{note_id}_body"]

    def test_it_deep_links_to_the_session(self) -> None:
        assert _fire(key="chat-7-99")[0]["url"] == "/chat?sid=chat-7-99"

    def test_a_session_key_with_unsafe_chars_is_quoted(self) -> None:
        assert _fire(key="dashboard:a b/c")[0]["url"] == "/chat?sid=dashboard%3Aa%20b%2Fc"

    def test_group_key_survives_onto_the_delivered_note(self) -> None:
        # group_key must be set on the PAYLOAD, not passed in meta,
        # which the bus drops. The real bus is what proves it lands.
        assert _fire(session_key="sid-9")[0]["group_key"] == "turn-died:sid-9"

    def test_it_attributes_the_publishing_slot_so_its_mute_applies(self) -> None:
        # publisher_session is the SLOT KEY, not the session key: session_is_muted
        # resolves it through caller_slot_key, which matches the slot's key, not
        # an unbound channel-born slot's effective session key. The bus reads it
        # off the payload and drops it from the delivered note, so assert the row
        # the producer built rather than the delivered dict.
        state = BusState()
        pushed: list[Any] = []
        real_push = state.notification_bus.push
        state.notification_bus.push = lambda p: (pushed.append(p), real_push(p))[1]  # type: ignore[method-assign]
        cr._notify_turn_died(FakeTurn(FakeSlot("chat-7-99"), state, "sid-9"), "chat_stopped_error")
        assert pushed[0].publisher_session == "chat-7-99"


class TestWhatIsNeverAnnounced:
    def test_an_app_owned_chat_is_left_to_the_app(self) -> None:
        """An app owns its chat's continuation, so a dead turn there is the
        app's to handle -- not an operator's to be alerted to."""
        assert _fire(app="mochi") == []


class TestItNeverDisturbsTheDyingTurn:
    def test_a_failed_delivery_is_swallowed(self) -> None:
        state = BusState(fail=True)
        # Must not raise: the turn's teardown owns the recoverable work.
        cr._notify_turn_died(FakeTurn(FakeSlot(), state), "chat_stopped_error")
        assert state.delivered == []

    def test_a_blank_session_key_still_publishes_under_the_slot(self) -> None:
        # group_key keys on the session (absent when blank, so no stacking), but
        # publisher_session keys on the slot, which is always present.
        note = _fire(key="chat-1-123", session_key="")[0]
        assert "group_key" not in note


#: What the bundled engine says for a signed-out turn on a host with no kiro-cli.
_BUNDLE_SIGNED_OUT = host_auth.signed_out_message(
    ACP_BACKEND_KAS, route=KasRoute(bundle=True, reason=REASON_NO_KIRO_CLI)
)


class TestTheErrorCardNamesASignIn:
    """A sign-in failure can reach the turn as a plain ``AcpError`` whose frame is tagged
    ``auth_required`` or ``kiro_cli_sign_in``, not as ``AcpAuthRequired``. The error card
    then offers the sign-in fix on its row, so the bell says the agent is signed out
    rather than a bare "stopped with an error". Driven through a real turn."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("text", "tag", "row_kind", "note_id"),
        [
            pytest.param(
                _BUNDLE_SIGNED_OUT,
                "auth_required",
                AUTH_REQUIRED_KIND,
                "chat_stopped_signed_out",
                id="crew-sign-in",
            ),
            pytest.param(
                "Not signed in",
                "kiro_cli_sign_in",
                KIRO_CLI_SIGN_IN_KIND,
                "chat_stopped_signed_out",
                id="kiro-cli-sign-in",
            ),
            pytest.param("bad request", "", None, "chat_stopped_error", id="other-error"),
        ],
    )
    async def test_the_note_follows_the_row_the_card_wrote(
        self, text: str, tag: str, row_kind: str | None, note_id: str
    ) -> None:
        err = AcpError(text, transient=False)
        if tag:
            setattr(err, tag, True)
        bus = BusState()

        def _setup(ctx: TurnContext) -> None:
            ctx.state.notification_bus = bus.notification_bus

        record = await run_turn(TurnScript(events=[Raise(err)], setup=_setup))
        assert (record.rows("error")[-1].get("meta") or {}).get("kind") == row_kind
        assert [note["i18n"] for note in bus.delivered] == [{"id": note_id}]
