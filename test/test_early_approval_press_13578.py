"""A button press that lands before the waiter is registered is still applied.

``TurnDriver`` dispatches ``PROMPT_CHOICE`` to the renderer and only then awaits
the decider, and the renderer suspends in between -- a thread hop for the
display-safety scan on Telegram, then the send on all three channels. A press
that arrives in that gap has a prompt on screen and a nonce armed, so it is a
genuine answer and must decide the request. A decider that minted its waiter's
future inside ``__call__`` has nothing to resolve at that moment, reports the
press to the user as an approval that already expired, and then denies the
request when the decision window elapses.

Each channel opens the window where the prompt is prepared and adopts it in
``__call__``. These tests drive the REAL sequence -- renderer posts, press lands
during the send, decider runs afterwards -- because that ordering is the defect;
a test that pressed after the wait started would pass either way.

The waits below are bounded by ``_TEST_WAIT_S`` rather than the production
window, so a regression fails in seconds instead of parking the suite for the
full five minutes.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.slack.renderer import (
    SlackApprovalDecider,
    SlackRenderer,
    _approval_registry_key,
)
from kiro_crew.teams.approvals import TeamsApprovalDecider, registry_key
from kiro_crew.teams.renderer import TeamsRenderer
from kiro_crew.teams.transport import TEAMS_CAPABILITIES
from kiro_crew.telegram.renderer import TelegramApprovalDecider, TelegramRenderer
from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES

#: How long a test waits on a decision it expects to already be in hand. Long
#: enough to absorb scheduling on a loaded host, short enough that a decider
#: which discarded the press fails here instead of at the production timeout.
_TEST_WAIT_S = 5.0

_SESSION = "telegram:1:0"
_RID = "7"


def _event(request_id: str = _RID) -> SimpleNamespace:
    """The permission event shape the driver hands a decider."""
    return SimpleNamespace(request_id=request_id)


def _future_on_a_closed_loop() -> "asyncio.Future[bool]":
    """A pending future whose loop is gone, as a leftover registry entry.

    Built on a real second loop and left unresolved, because that is what a turn
    on an earlier loop leaves in a process-global registry -- the object is fine,
    only its loop is unusable.
    """
    dead = asyncio.new_event_loop()
    try:
        return dead.create_future()
    finally:
        dead.close()


@pytest.fixture(autouse=True)
def _clean_registries() -> Any:
    """Both process-global registries, emptied around every test.

    They are class attributes, so a reservation one test leaves behind would be
    adopted by the next and turn a real failure into a pass.
    """
    TelegramApprovalDecider._REGISTRY.clear()
    TelegramApprovalDecider._NONCES.clear()
    SlackApprovalDecider._REGISTRY.clear()
    TeamsApprovalDecider.reset_for_tests()
    yield
    TelegramApprovalDecider._REGISTRY.clear()
    TelegramApprovalDecider._NONCES.clear()
    SlackApprovalDecider._REGISTRY.clear()
    TeamsApprovalDecider.reset_for_tests()


class _PressingTelegramClient:
    """A Telegram client that presses the button while the send is in flight.

    The press runs from inside ``send_message`` because that is exactly where the
    real one lands: the renderer has armed the nonce and posted the keyboard, and
    the driver has not yet reached the decider.
    """

    def __init__(
        self,
        *,
        approved: bool = True,
        trust: bool = False,
        deliver: bool = True,
        press: bool = True,
    ) -> None:
        self.approved = approved
        self.trust = trust
        self.deliver = deliver
        self.press = press
        self.press_accepted: bool | None = None
        self.pending_at_press: bool | None = None
        self.pressed_nonce = ""
        self.sent: list[str] = []

    async def send_message(self, chat_id: int, text: str, **kw: Any) -> int | None:
        self.sent.append(text)
        if not self.press:
            return 1234 if self.deliver else None
        keyboard = (kw.get("reply_markup") or {}).get("inline_keyboard") or []
        # Read the nonce off the button the user would actually tap, rather than
        # being handed it: a press carries only what the keyboard put in front of
        # the user, so taking it from anywhere else would not be the same event.
        flag = "t" if self.trust else ("1" if self.approved else "0")
        nonce = ""
        for row in keyboard:
            for button in row:
                parts = str(button.get("callback_data", "")).split(":")
                if len(parts) == 4 and parts[3] == flag:
                    nonce = parts[2]
        key = TelegramApprovalDecider.key(_SESSION, _RID)
        self.pressed_nonce = nonce
        # Asked BEFORE the resolve, in the dispatcher's own order: it is the gate on
        # granting Trust, and a resolve leaves the future done, so reading it after
        # would report every live prompt as not pending.
        self.pending_at_press = TelegramApprovalDecider.is_pending(key, nonce)
        self.press_accepted = TelegramApprovalDecider.resolve_global(
            key, self.approved, nonce=nonce
        )
        return 1234 if self.deliver else None


def _telegram_renderer(client: Any) -> TelegramRenderer:
    return TelegramRenderer(  # type: ignore[arg-type]
        client, 55, TELEGRAM_CAPABILITIES, session_key=_SESSION
    )


class TestTelegramEarlyPress:
    @pytest.mark.asyncio
    async def test_a_press_during_the_send_is_applied_not_reported_expired(self) -> None:
        """The defect: the press arrives before the wait and must still decide."""
        client = _PressingTelegramClient(approved=True)
        renderer = _telegram_renderer(client)
        await renderer.on_prompt_choice([], _RID, tool_title="bash")
        assert client.press_accepted is True
        decider = TelegramApprovalDecider(session_key=_SESSION)
        assert await asyncio.wait_for(decider(_event()), _TEST_WAIT_S) is True
        assert decider.last_deny_cause == ""

    @pytest.mark.asyncio
    async def test_a_deny_during_the_send_is_applied_as_a_deny(self) -> None:
        """Deny travels the same path: the press decides, it is not merely heard."""
        client = _PressingTelegramClient(approved=False)
        renderer = _telegram_renderer(client)
        await renderer.on_prompt_choice([], _RID, tool_title="bash")
        assert client.press_accepted is True
        decider = TelegramApprovalDecider(session_key=_SESSION)
        assert await asyncio.wait_for(decider(_event()), _TEST_WAIT_S) is False
        # A human's refusal, NOT an expiry: the driver reads this to decide what
        # the model is told, so conflating them would misreport a real decision.
        assert decider.last_deny_cause == ""

    @pytest.mark.asyncio
    async def test_a_trust_press_during_the_send_sees_a_live_prompt(self) -> None:
        """Trust is gated on ``is_pending``, which the closed window failed.

        The grant outlives the prompt, so its gate is asked before the resolve.
        With no future reserved that gate said "nothing pending", and the operator
        who tapped Trust got neither the grant nor the tool.
        """
        client = _PressingTelegramClient(approved=True, trust=True)
        renderer = _telegram_renderer(client)
        await renderer.on_prompt_choice([], _RID, tool_title="bash")
        assert client.pending_at_press is True
        assert client.press_accepted is True

    def test_a_stale_button_after_a_restart_still_grants_nothing(self) -> None:
        """The restart guard survives the reservation.

        Reservations are per prompt and process-local, so a fresh process has
        none: a button left in the scrollback finds no reservation and no nonce.
        This is what stops a scrollback Trust re-granting standing authority.
        """
        key = TelegramApprovalDecider.key(_SESSION, _RID)
        assert TelegramApprovalDecider.is_pending(key, "some-old-nonce") is False
        assert TelegramApprovalDecider.resolve_global(key, True, nonce="some-old-nonce") is False

    @pytest.mark.asyncio
    async def test_a_press_after_the_wait_starts_still_works(self) -> None:
        """The ordinary path is unchanged by the reservation."""
        key = TelegramApprovalDecider.key(_SESSION, _RID)
        TelegramApprovalDecider.arm(key, "n1")
        decider = TelegramApprovalDecider(session_key=_SESSION)
        task = asyncio.create_task(decider(_event()))
        await asyncio.sleep(0)
        assert TelegramApprovalDecider.resolve_global(key, True, nonce="n1") is True
        assert await asyncio.wait_for(task, _TEST_WAIT_S) is True

    @pytest.mark.asyncio
    async def test_a_second_press_is_refused(self) -> None:
        """One prompt, one decision: the second press must not re-answer it."""
        key = TelegramApprovalDecider.key(_SESSION, _RID)
        TelegramApprovalDecider.arm(key, "n1")
        assert TelegramApprovalDecider.resolve_global(key, True, nonce="n1") is True
        assert TelegramApprovalDecider.resolve_global(key, False, nonce="n1") is False
        decider = TelegramApprovalDecider(session_key=_SESSION)
        assert await asyncio.wait_for(decider(_event()), _TEST_WAIT_S) is True

    @pytest.mark.asyncio
    async def test_a_press_with_the_wrong_nonce_is_refused_inside_the_window(self) -> None:
        """The nonce still guards the window it now spans."""
        key = TelegramApprovalDecider.key(_SESSION, _RID)
        TelegramApprovalDecider.arm(key, "n1")
        assert TelegramApprovalDecider.resolve_global(key, True, nonce="wrong") is False
        assert TelegramApprovalDecider.resolve_global(key, True, nonce="") is False
        assert TelegramApprovalDecider.is_pending(key, "wrong") is False

    @pytest.mark.asyncio
    async def test_a_press_for_another_session_cannot_resolve_this_one(self) -> None:
        """Caller isolation: the key is session-namespaced and stays so."""
        mine = TelegramApprovalDecider.key(_SESSION, _RID)
        TelegramApprovalDecider.arm(mine, "n1")
        theirs = TelegramApprovalDecider.key("telegram:2:0", _RID)
        assert TelegramApprovalDecider.resolve_global(theirs, True, nonce="n1") is False
        assert TelegramApprovalDecider.is_pending(mine, "n1") is True

    @pytest.mark.asyncio
    async def test_an_undelivered_prompt_denies_at_once(self) -> None:
        """A send that returns no message id is refused, not waited out.

        This client reports failure by returning ``None`` rather than raising, so
        nothing is on screen to press. The driver awaits the decider next, and it
        must deny immediately instead of spending the whole window on an invisible
        prompt and reporting that as an expiry.
        """
        client = _PressingTelegramClient(deliver=False, press=False)
        renderer = _telegram_renderer(client)
        await renderer.on_prompt_choice([], _RID, tool_title="bash")
        decider = TelegramApprovalDecider(session_key=_SESSION)
        assert await asyncio.wait_for(decider(_event()), _TEST_WAIT_S) is False
        assert decider.last_deny_cause == ""

    @pytest.mark.asyncio
    async def test_an_undelivered_report_does_not_overwrite_a_press_that_landed(self) -> None:
        """A press inside the window outranks the send's own failure report.

        Telegram can deliver the keyboard and still return no message id, so the
        refusal must never overwrite a decision the user actually made: it only
        settles a reservation nothing has resolved.
        """
        client = _PressingTelegramClient(approved=True, deliver=False)
        renderer = _telegram_renderer(client)
        await renderer.on_prompt_choice([], _RID, tool_title="bash")
        assert client.press_accepted is True
        decider = TelegramApprovalDecider(session_key=_SESSION)
        assert await asyncio.wait_for(decider(_event()), _TEST_WAIT_S) is True

    @pytest.mark.asyncio
    async def test_a_raising_send_retires_the_window_and_propagates(self) -> None:
        """A prompt that never went out leaves no armed nonce behind."""

        class _Raising:
            async def send_message(self, *a: Any, **kw: Any) -> int | None:
                raise RuntimeError("chat gone")

        renderer = _telegram_renderer(_Raising())
        with pytest.raises(RuntimeError):
            await renderer.on_prompt_choice([], _RID, tool_title="bash")
        key = TelegramApprovalDecider.key(_SESSION, _RID)
        assert key not in TelegramApprovalDecider._REGISTRY
        assert key not in TelegramApprovalDecider._NONCES

    @pytest.mark.asyncio
    async def test_an_unanswered_window_denies_with_the_expiry_cause(self) -> None:
        """Deny-on-silence is unchanged, and still distinguishable from a refusal."""
        key = TelegramApprovalDecider.key(_SESSION, _RID)
        TelegramApprovalDecider.arm(key, "n1")
        decider = TelegramApprovalDecider(session_key=_SESSION)
        import kiro_crew.telegram.renderer as tg

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tg, "_APPROVAL_TIMEOUT_S", 0.01)
            assert await asyncio.wait_for(decider(_event()), _TEST_WAIT_S) is False
        assert decider.last_deny_cause != ""
        assert key not in TelegramApprovalDecider._REGISTRY
        assert key not in TelegramApprovalDecider._NONCES

    @pytest.mark.asyncio
    async def test_a_cancelled_reservation_is_not_read_as_a_decision(self) -> None:
        """A torn-down reservation holds no verdict, so a fresh window opens.

        Reading a cancelled future as a result would raise inside the decider;
        reading it as ``False`` would report a refusal nobody made.
        """
        key = TelegramApprovalDecider.key(_SESSION, _RID)
        TelegramApprovalDecider.arm(key, "n1")
        TelegramApprovalDecider._REGISTRY[key].cancel()
        await asyncio.sleep(0)
        decider = TelegramApprovalDecider(session_key=_SESSION)
        task = asyncio.create_task(decider(_event()))
        await asyncio.sleep(0)
        assert TelegramApprovalDecider.resolve_global(key, True, nonce="n1") is True
        assert await asyncio.wait_for(task, _TEST_WAIT_S) is True

    @pytest.mark.asyncio
    async def test_an_unawaited_reservation_does_not_outlive_its_turn(self) -> None:
        """The turn teardown closes a window no wait ever adopted.

        The prompt went out and the turn ended before the driver reached the
        decider, so nothing else drops it -- and the nonce left behind is what
        authorizes a press.
        """
        mine = TelegramApprovalDecider.key(_SESSION, _RID)
        other = TelegramApprovalDecider.key("telegram:2:0", _RID)
        TelegramApprovalDecider.arm(mine, "n1")
        TelegramApprovalDecider.arm(other, "n2")
        TelegramApprovalDecider.discard_session(_SESSION)
        assert mine not in TelegramApprovalDecider._REGISTRY
        assert mine not in TelegramApprovalDecider._NONCES
        # Another session's live window is untouched.
        assert other in TelegramApprovalDecider._REGISTRY
        assert TelegramApprovalDecider.resolve_global(mine, True, nonce="n1") is False

    @pytest.mark.asyncio
    async def test_a_delivered_decision_is_not_discarded_by_the_teardown(self) -> None:
        """Only PENDING reservations are dropped, so a real answer survives."""
        key = TelegramApprovalDecider.key(_SESSION, _RID)
        TelegramApprovalDecider.arm(key, "n1")
        assert TelegramApprovalDecider.resolve_global(key, True, nonce="n1") is True
        TelegramApprovalDecider.discard_session(_SESSION)
        assert key in TelegramApprovalDecider._REGISTRY

    @pytest.mark.asyncio
    async def test_arming_twice_keeps_the_future_the_waiter_holds(self) -> None:
        """Replacing a live future would orphan the object the wait blocks on."""
        key = TelegramApprovalDecider.key(_SESSION, _RID)
        TelegramApprovalDecider.arm(key, "n1")
        first = TelegramApprovalDecider._REGISTRY[key]
        TelegramApprovalDecider.arm(key, "n2")
        assert TelegramApprovalDecider._REGISTRY[key] is first

    @pytest.mark.asyncio
    async def test_a_done_future_is_replaced_by_the_next_arm(self) -> None:
        """A decision left unawaited must not be adoptable by the next request."""
        key = TelegramApprovalDecider.key(_SESSION, _RID)
        TelegramApprovalDecider.arm(key, "n1")
        TelegramApprovalDecider.resolve_global(key, True, nonce="n1")
        stale = TelegramApprovalDecider._REGISTRY[key]
        TelegramApprovalDecider.arm(key, "n2")
        assert TelegramApprovalDecider._REGISTRY[key] is not stale
        assert TelegramApprovalDecider._REGISTRY[key].done() is False


class TestArmingOffTheEventLoop:
    """Arming without a running loop mints the nonce and reserves nothing.

    A reservation is a promise to a wait that runs on the SAME loop, so off the
    loop there is no waiter to hold a window open for -- and a caller that cannot
    await the decider cannot be raced by a press. Keeping these callable off the
    loop is what stops the reservation narrowing ``arm`` into an async-only call.
    """

    def test_telegram_arm_off_the_loop_arms_the_nonce_only(self) -> None:
        key = TelegramApprovalDecider.key(_SESSION, _RID)
        TelegramApprovalDecider.arm(key, "n1")
        assert TelegramApprovalDecider.nonce_matches(key, "n1") is True
        assert key not in TelegramApprovalDecider._REGISTRY

    def test_teams_arm_off_the_loop_arms_the_nonce_only(self) -> None:
        decider = TeamsApprovalDecider(session_key=_SESSION)
        decider.arm(_RID, "n1")
        assert decider._nonces[_RID] == "n1"
        assert _RID not in decider._futures
        assert registry_key(_SESSION, _RID) not in TeamsApprovalDecider._REGISTRY

    def test_slack_reserve_off_the_loop_is_inert(self) -> None:
        decider = SlackApprovalDecider(session_key=_SESSION)
        decider.reserve(_RID)
        assert _RID not in decider._futures
        assert _approval_registry_key(_SESSION, _RID) not in SlackApprovalDecider._REGISTRY


class TestAReservationFromAClosedLoop:
    """A registry entry outliving its event loop is not a reservation.

    The registries are process-global and outlive any one loop, so a prompt
    rendered on a loop that has since closed leaves a reachable pending future
    behind. Awaiting it raises ``attached to a different loop``, and its lack of a
    result is not a decision, so a later turn must open a fresh window instead.
    """

    @pytest.mark.asyncio
    async def test_telegram_ignores_a_reservation_from_a_dead_loop(self) -> None:
        key = TelegramApprovalDecider.key(_SESSION, _RID)
        TelegramApprovalDecider._REGISTRY[key] = _future_on_a_closed_loop()
        TelegramApprovalDecider._NONCES[key] = "n1"
        # Arming again on THIS loop replaces the foreign entry rather than keeping
        # it, so the waiter blocks on a future this loop can resolve.
        TelegramApprovalDecider.arm(key, "n1")
        assert TelegramApprovalDecider._REGISTRY[key].get_loop() is asyncio.get_running_loop()
        decider = TelegramApprovalDecider(session_key=_SESSION)
        task = asyncio.create_task(decider(_event()))
        await asyncio.sleep(0)
        assert TelegramApprovalDecider.resolve_global(key, True, nonce="n1") is True
        assert await asyncio.wait_for(task, _TEST_WAIT_S) is True

    @pytest.mark.asyncio
    async def test_telegram_denies_rather_than_raising_on_a_dead_reservation(self) -> None:
        """Without an arm the decider still must not await the foreign future."""
        key = TelegramApprovalDecider.key(_SESSION, _RID)
        TelegramApprovalDecider._REGISTRY[key] = _future_on_a_closed_loop()
        decider = TelegramApprovalDecider(session_key=_SESSION)
        import kiro_crew.telegram.renderer as tg

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tg, "_APPROVAL_TIMEOUT_S", 0.01)
            assert await asyncio.wait_for(decider(_event()), _TEST_WAIT_S) is False

    @pytest.mark.asyncio
    async def test_slack_ignores_a_reservation_from_a_dead_loop(self) -> None:
        decider = SlackApprovalDecider(session_key=_SESSION)
        decider._futures[_RID] = _future_on_a_closed_loop()
        decider.reserve(_RID)
        assert decider._futures[_RID].get_loop() is asyncio.get_running_loop()

    @pytest.mark.asyncio
    async def test_teams_ignores_a_reservation_from_a_dead_loop(self) -> None:
        decider = TeamsApprovalDecider(session_key=_SESSION)
        decider._futures[_RID] = _future_on_a_closed_loop()
        decider.arm(_RID, "n1")
        assert decider._futures[_RID].get_loop() is asyncio.get_running_loop()


class _PressingSlackClient:
    """A Slack client that clicks the button while ``post_blocks`` is in flight."""

    def __init__(self, *, approved: bool = True, raising: bool = False) -> None:
        self.approved = approved
        self.raising = raising
        self.press_accepted: bool | None = None
        self.session_at_press = ""

    async def post_blocks(
        self, channel: str, blocks: list[dict], text: str, thread_ts: str | None = None
    ) -> str:
        if self.raising:
            raise RuntimeError("channel gone")
        key = _approval_registry_key(_SESSION, _RID)
        self.session_at_press = SlackApprovalDecider.session_for(key)
        self.press_accepted = SlackApprovalDecider.resolve_global(key, self.approved)
        return "1.0"


class TestSlackEarlyPress:
    @pytest.mark.asyncio
    async def test_a_click_during_the_post_is_applied_not_reported_expired(self) -> None:
        """The defect: nothing was in the registry until the wait started."""
        decider = SlackApprovalDecider(session_key=_SESSION)
        client = _PressingSlackClient(approved=True)
        renderer = SlackRenderer(client, "C1", "t1", reactions_enabled=False, decider=decider)
        await renderer.on_prompt_choice([], _RID, tool_title="bash")
        assert client.press_accepted is True
        assert await asyncio.wait_for(decider(_event()), _TEST_WAIT_S) is True
        assert decider.last_deny_cause == ""

    @pytest.mark.asyncio
    async def test_trust_can_read_the_session_during_the_window(self) -> None:
        """Trust is granted per session, looked up through the same registry.

        Reserving the future without registering the decider would leave this
        empty, so the click would resolve the tool and grant nothing.
        """
        decider = SlackApprovalDecider(session_key=_SESSION)
        client = _PressingSlackClient()
        renderer = SlackRenderer(client, "C1", "t1", reactions_enabled=False, decider=decider)
        await renderer.on_prompt_choice([], _RID, tool_title="bash")
        assert client.session_at_press == _SESSION

    @pytest.mark.asyncio
    async def test_a_deny_during_the_post_is_applied_as_a_deny(self) -> None:
        decider = SlackApprovalDecider(session_key=_SESSION)
        client = _PressingSlackClient(approved=False)
        renderer = SlackRenderer(client, "C1", "t1", reactions_enabled=False, decider=decider)
        await renderer.on_prompt_choice([], _RID, tool_title="bash")
        assert client.press_accepted is True
        assert await asyncio.wait_for(decider(_event()), _TEST_WAIT_S) is False
        assert decider.last_deny_cause == ""

    @pytest.mark.asyncio
    async def test_a_click_after_the_wait_starts_still_works(self) -> None:
        decider = SlackApprovalDecider(session_key=_SESSION)
        decider.reserve(_RID)
        task = asyncio.create_task(decider(_event()))
        await asyncio.sleep(0)
        key = _approval_registry_key(_SESSION, _RID)
        assert SlackApprovalDecider.resolve_global(key, True) is True
        assert await asyncio.wait_for(task, _TEST_WAIT_S) is True

    @pytest.mark.asyncio
    async def test_a_second_click_is_refused(self) -> None:
        decider = SlackApprovalDecider(session_key=_SESSION)
        decider.reserve(_RID)
        key = _approval_registry_key(_SESSION, _RID)
        assert SlackApprovalDecider.resolve_global(key, True) is True
        assert SlackApprovalDecider.resolve_global(key, False) is False
        assert await asyncio.wait_for(decider(_event()), _TEST_WAIT_S) is True

    @pytest.mark.asyncio
    async def test_a_click_for_another_session_cannot_resolve_this_one(self) -> None:
        decider = SlackApprovalDecider(session_key=_SESSION)
        decider.reserve(_RID)
        theirs = _approval_registry_key("slack:C9:t9", _RID)
        assert SlackApprovalDecider.resolve_global(theirs, True) is False
        assert SlackApprovalDecider.session_for(theirs) == ""

    @pytest.mark.asyncio
    async def test_a_raising_post_discards_the_window_and_propagates(self) -> None:
        decider = SlackApprovalDecider(session_key=_SESSION)
        client = _PressingSlackClient(raising=True)
        renderer = SlackRenderer(client, "C1", "t1", reactions_enabled=False, decider=decider)
        with pytest.raises(RuntimeError):
            await renderer.on_prompt_choice([], _RID, tool_title="bash")
        key = _approval_registry_key(_SESSION, _RID)
        assert key not in SlackApprovalDecider._REGISTRY
        assert SlackApprovalDecider.resolve_global(key, True) is False

    @pytest.mark.asyncio
    async def test_an_unanswered_window_denies_with_the_expiry_cause(self) -> None:
        decider = SlackApprovalDecider(session_key=_SESSION)
        decider.reserve(_RID)
        import kiro_crew.slack.renderer as sl

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sl, "_APPROVAL_TIMEOUT", 0.01)
            assert await asyncio.wait_for(decider(_event()), _TEST_WAIT_S) is False
        assert decider.last_deny_cause != ""
        assert _approval_registry_key(_SESSION, _RID) not in SlackApprovalDecider._REGISTRY

    @pytest.mark.asyncio
    async def test_a_cancelled_reservation_is_not_read_as_a_decision(self) -> None:
        decider = SlackApprovalDecider(session_key=_SESSION)
        decider.reserve(_RID)
        decider._futures[_RID].cancel()
        await asyncio.sleep(0)
        task = asyncio.create_task(decider(_event()))
        await asyncio.sleep(0)
        key = _approval_registry_key(_SESSION, _RID)
        assert SlackApprovalDecider.resolve_global(key, True) is True
        assert await asyncio.wait_for(task, _TEST_WAIT_S) is True

    @pytest.mark.asyncio
    async def test_an_unawaited_reservation_does_not_outlive_its_turn(self) -> None:
        mine = SlackApprovalDecider(session_key=_SESSION)
        mine.reserve(_RID)
        theirs = SlackApprovalDecider(session_key="slack:C9:t9")
        theirs.reserve(_RID)
        SlackApprovalDecider.discard_session(_SESSION)
        assert _approval_registry_key(_SESSION, _RID) not in SlackApprovalDecider._REGISTRY
        assert _RID not in mine._futures
        # Another session's live window is untouched.
        assert _approval_registry_key("slack:C9:t9", _RID) in SlackApprovalDecider._REGISTRY

    @pytest.mark.asyncio
    async def test_a_delivered_decision_is_not_discarded_by_the_teardown(self) -> None:
        decider = SlackApprovalDecider(session_key=_SESSION)
        decider.reserve(_RID)
        key = _approval_registry_key(_SESSION, _RID)
        assert SlackApprovalDecider.resolve_global(key, True) is True
        SlackApprovalDecider.discard_session(_SESSION)
        assert key in SlackApprovalDecider._REGISTRY

    @pytest.mark.asyncio
    async def test_reserving_twice_keeps_the_future_the_waiter_holds(self) -> None:
        decider = SlackApprovalDecider(session_key=_SESSION)
        decider.reserve(_RID)
        first = decider._futures[_RID]
        decider.reserve(_RID)
        assert decider._futures[_RID] is first


class _PressingTeamsClient:
    """A Teams client that clicks the card while ``send_card`` is in flight."""

    def __init__(self, *, approved: bool = True, trust: bool = False) -> None:
        self.approved = approved
        self.trust = trust
        self.press_accepted: bool | None = None

    async def send_typing(self, conversation_id: str, service_url: str) -> None:
        return None

    async def send_message(self, conversation_id: str, content: str, service_url: str) -> str:
        return "m1"

    async def send_card(self, conversation_id: str, card: dict, service_url: str) -> str:
        nonce = _nonce_from_card(card)
        self.press_accepted = TeamsApprovalDecider.resolve_global(
            _SESSION, _RID, nonce, approved=self.approved, trust=self.trust
        )
        return "a1"


def _nonce_from_card(card: dict) -> str:
    """The nonce as the CARD carries it, which is all a real click echoes back."""
    found = ""

    def walk(node: Any) -> None:
        nonlocal found
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "nonce" and isinstance(value, str) and value:
                    found = found or value
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(card)
    return found


def _teams_renderer(client: Any, decider: TeamsApprovalDecider) -> TeamsRenderer:
    return TeamsRenderer(  # type: ignore[arg-type]
        client, "conv", "https://svc.test/", TEAMS_CAPABILITIES, decider=decider
    )


class TestTeamsEarlyPress:
    @pytest.mark.asyncio
    async def test_a_click_during_the_card_post_is_applied(self) -> None:
        """The defect: the registry held no decider until the wait started."""
        decider = TeamsApprovalDecider(session_key=_SESSION)
        client = _PressingTeamsClient(approved=True)
        await _teams_renderer(client, decider).on_prompt_choice([], _RID, tool_title="bash")
        assert client.press_accepted is True
        assert await asyncio.wait_for(decider(_event()), _TEST_WAIT_S) is True
        assert decider.last_deny_cause == ""

    @pytest.mark.asyncio
    async def test_a_trust_click_during_the_card_post_records_the_grant(self) -> None:
        """Trust must reach the dispatcher, which reads it after the decision."""
        decider = TeamsApprovalDecider(session_key=_SESSION)
        client = _PressingTeamsClient(approved=True, trust=True)
        await _teams_renderer(client, decider).on_prompt_choice([], _RID, tool_title="bash")
        assert client.press_accepted is True
        assert decider.trusted is True

    @pytest.mark.asyncio
    async def test_a_deny_during_the_card_post_is_applied_as_a_deny(self) -> None:
        decider = TeamsApprovalDecider(session_key=_SESSION)
        client = _PressingTeamsClient(approved=False)
        await _teams_renderer(client, decider).on_prompt_choice([], _RID, tool_title="bash")
        assert client.press_accepted is True
        assert await asyncio.wait_for(decider(_event()), _TEST_WAIT_S) is False
        assert decider.last_deny_cause == ""

    @pytest.mark.asyncio
    async def test_a_click_after_the_wait_starts_still_works(self) -> None:
        decider = TeamsApprovalDecider(session_key=_SESSION)
        decider.arm(_RID, "n1")
        task = asyncio.create_task(decider(_event()))
        await asyncio.sleep(0)
        assert TeamsApprovalDecider.resolve_global(_SESSION, _RID, "n1", approved=True) is True
        assert await asyncio.wait_for(task, _TEST_WAIT_S) is True

    @pytest.mark.asyncio
    async def test_a_second_click_is_refused(self) -> None:
        decider = TeamsApprovalDecider(session_key=_SESSION)
        decider.arm(_RID, "n1")
        assert TeamsApprovalDecider.resolve_global(_SESSION, _RID, "n1", approved=True) is True
        assert TeamsApprovalDecider.resolve_global(_SESSION, _RID, "n1", approved=False) is False
        assert await asyncio.wait_for(decider(_event()), _TEST_WAIT_S) is True

    @pytest.mark.asyncio
    async def test_a_click_with_the_wrong_nonce_is_refused_inside_the_window(self) -> None:
        decider = TeamsApprovalDecider(session_key=_SESSION)
        decider.arm(_RID, "n1")
        assert TeamsApprovalDecider.resolve_global(_SESSION, _RID, "wrong", approved=True) is False
        assert TeamsApprovalDecider.resolve_global(_SESSION, _RID, "", approved=True) is False

    @pytest.mark.asyncio
    async def test_a_click_for_another_session_cannot_resolve_this_one(self) -> None:
        decider = TeamsApprovalDecider(session_key=_SESSION)
        decider.arm(_RID, "n1")
        assert (
            TeamsApprovalDecider.resolve_global("teams:other", _RID, "n1", approved=True) is False
        )

    @pytest.mark.asyncio
    async def test_an_undelivered_card_denies_at_once(self) -> None:
        """A card the conversation refused is written off, not waited out."""

        class _Refusing(_PressingTeamsClient):
            async def send_card(self, conversation_id: str, card: dict, service_url: str) -> str:
                from kiro_crew.teams.client import TeamsSendError

                raise TeamsSendError("no")

        decider = TeamsApprovalDecider(session_key=_SESSION)
        await _teams_renderer(_Refusing(), decider).on_prompt_choice([], _RID, tool_title="bash")
        assert await asyncio.wait_for(decider(_event()), _TEST_WAIT_S) is False
        # Written off, so nothing is left for a late click to answer.
        assert registry_key(_SESSION, _RID) not in TeamsApprovalDecider._REGISTRY
        assert _RID not in decider._futures

    @pytest.mark.asyncio
    async def test_an_unanswered_window_denies_with_the_expiry_cause(self) -> None:
        decider = TeamsApprovalDecider(session_key=_SESSION)
        decider.arm(_RID, "n1")
        import kiro_crew.teams.approvals as tm

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tm, "APPROVAL_TIMEOUT_SECS", 0.01)
            assert await asyncio.wait_for(decider(_event()), _TEST_WAIT_S) is False
        assert decider.last_deny_cause != ""
        assert registry_key(_SESSION, _RID) not in TeamsApprovalDecider._REGISTRY

    @pytest.mark.asyncio
    async def test_a_cancelled_reservation_is_not_read_as_a_decision(self) -> None:
        decider = TeamsApprovalDecider(session_key=_SESSION)
        decider.arm(_RID, "n1")
        decider._futures[_RID].cancel()
        await asyncio.sleep(0)
        task = asyncio.create_task(decider(_event()))
        await asyncio.sleep(0)
        assert TeamsApprovalDecider.resolve_global(_SESSION, _RID, "n1", approved=True) is True
        assert await asyncio.wait_for(task, _TEST_WAIT_S) is True

    @pytest.mark.asyncio
    async def test_an_unawaited_reservation_does_not_outlive_its_turn(self) -> None:
        decider = TeamsApprovalDecider(session_key=_SESSION)
        decider.arm(_RID, "n1")
        other = TeamsApprovalDecider(session_key="teams:other")
        other.arm(_RID, "n2")
        decider.discard_reservations()
        assert registry_key(_SESSION, _RID) not in TeamsApprovalDecider._REGISTRY
        assert _RID not in decider._futures
        assert _RID not in decider._nonces
        assert TeamsApprovalDecider.resolve_global(_SESSION, _RID, "n1", approved=True) is False
        # Another session's live window is untouched.
        assert registry_key("teams:other", _RID) in TeamsApprovalDecider._REGISTRY

    @pytest.mark.asyncio
    async def test_a_delivered_decision_is_not_discarded_by_the_teardown(self) -> None:
        decider = TeamsApprovalDecider(session_key=_SESSION)
        decider.arm(_RID, "n1")
        assert TeamsApprovalDecider.resolve_global(_SESSION, _RID, "n1", approved=True) is True
        decider.discard_reservations()
        assert registry_key(_SESSION, _RID) in TeamsApprovalDecider._REGISTRY

    @pytest.mark.asyncio
    async def test_arming_twice_keeps_the_future_the_waiter_holds(self) -> None:
        decider = TeamsApprovalDecider(session_key=_SESSION)
        decider.arm(_RID, "n1")
        first = decider._futures[_RID]
        decider.arm(_RID, "n2")
        assert decider._futures[_RID] is first
