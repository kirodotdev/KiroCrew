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
    TOOL_APPROVE_ACTION_PREFIX,
    SlackApprovalDecider,
    SlackRenderer,
    _approval_registry_key,
    build_approval_token,
    split_approval_token,
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
    adopted by the next and turn a real failure into a pass. The ownership sets
    are class state on the same footing: a key left in one makes the end-of-turn
    sweep skip it, which reads as the sweep being broken.
    """
    TelegramApprovalDecider._REGISTRY.clear()
    TelegramApprovalDecider._NONCES.clear()
    TelegramApprovalDecider._AWAITED.clear()
    SlackApprovalDecider._REGISTRY.clear()
    SlackApprovalDecider._NONCES.clear()
    SlackApprovalDecider._AWAITED.clear()
    TeamsApprovalDecider.reset_for_tests()
    yield
    TelegramApprovalDecider._REGISTRY.clear()
    TelegramApprovalDecider._NONCES.clear()
    TelegramApprovalDecider._AWAITED.clear()
    SlackApprovalDecider._REGISTRY.clear()
    SlackApprovalDecider._NONCES.clear()
    SlackApprovalDecider._AWAITED.clear()
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
    async def test_a_delivered_decision_nobody_awaited_is_swept_too(self) -> None:
        """An answer no wait adopted has no reader, and its nonce still authorizes.

        ``__call__`` for that turn never ran and :meth:`arm` re-mints rather than
        letting the next request inherit the answer, so retaining it keeps the
        future and a live nonce for the life of the process.
        """
        key = TelegramApprovalDecider.key(_SESSION, _RID)
        TelegramApprovalDecider.arm(key, "n1")
        assert TelegramApprovalDecider.resolve_global(key, True, nonce="n1") is True
        TelegramApprovalDecider.discard_session(_SESSION)
        assert key not in TelegramApprovalDecider._REGISTRY
        assert TelegramApprovalDecider.nonce_matches(key, "n1") is False

    @pytest.mark.asyncio
    async def test_a_window_a_wait_owns_survives_the_teardown(self) -> None:
        """Ownership, not the future's state, is what the sweep spares."""
        key = TelegramApprovalDecider.key(_SESSION, _RID)
        TelegramApprovalDecider.arm(key, "n1")
        TelegramApprovalDecider._AWAITED.add(key)
        assert TelegramApprovalDecider.resolve_global(key, True, nonce="n1") is True
        TelegramApprovalDecider.discard_session(_SESSION)
        assert key in TelegramApprovalDecider._REGISTRY
        assert TelegramApprovalDecider.nonce_matches(key, "n1") is True

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
        assert decider.reserve(_RID) == ""
        assert _RID not in decider._futures
        key = _approval_registry_key(_SESSION, _RID)
        assert key not in SlackApprovalDecider._REGISTRY
        # No nonce armed either, so the buttons such a caller posts carry none
        # and every press on them is refused rather than silently authorized.
        assert key not in SlackApprovalDecider._NONCES


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


def _slack_button_token(blocks: list[dict]) -> str:
    """The ``value`` Slack sends back when the Approve button is clicked.

    Taken out of the posted blocks rather than rebuilt, so a test presses with
    the token the user's own button carries -- including the prompt's nonce.
    """
    for block in blocks:
        for element in block.get("elements", []):
            if str(element.get("action_id", "")).startswith(TOOL_APPROVE_ACTION_PREFIX):
                return str(element.get("value", ""))
    raise AssertionError("no approval button in the posted blocks")


class _PressingSlackClient:
    """A Slack client that clicks the button while ``post_blocks`` is in flight."""

    def __init__(self, *, approved: bool = True, raising: bool = False) -> None:
        self.approved = approved
        self.raising = raising
        self.press_accepted: bool | None = None
        self.session_at_press = ""
        self.pressed_token = ""

    async def post_blocks(
        self, channel: str, blocks: list[dict], text: str, thread_ts: str | None = None
    ) -> str:
        self.pressed_token = _slack_button_token(blocks)
        if self.raising:
            raise RuntimeError("channel gone")
        key, nonce = split_approval_token(self.pressed_token)
        self.session_at_press = SlackApprovalDecider.session_for(key, nonce=nonce)
        self.press_accepted = SlackApprovalDecider.resolve_global(key, self.approved, nonce=nonce)
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
        nonce = decider.reserve(_RID)
        task = asyncio.create_task(decider(_event()))
        await asyncio.sleep(0)
        key = _approval_registry_key(_SESSION, _RID)
        assert SlackApprovalDecider.resolve_global(key, True, nonce=nonce) is True
        assert await asyncio.wait_for(task, _TEST_WAIT_S) is True

    @pytest.mark.asyncio
    async def test_a_second_click_is_refused(self) -> None:
        decider = SlackApprovalDecider(session_key=_SESSION)
        nonce = decider.reserve(_RID)
        key = _approval_registry_key(_SESSION, _RID)
        assert SlackApprovalDecider.resolve_global(key, True, nonce=nonce) is True
        assert SlackApprovalDecider.resolve_global(key, False, nonce=nonce) is False
        assert await asyncio.wait_for(decider(_event()), _TEST_WAIT_S) is True

    @pytest.mark.asyncio
    async def test_a_click_for_another_session_cannot_resolve_this_one(self) -> None:
        decider = SlackApprovalDecider(session_key=_SESSION)
        nonce = decider.reserve(_RID)
        theirs = _approval_registry_key("slack:C9:t9", _RID)
        assert SlackApprovalDecider.resolve_global(theirs, True, nonce=nonce) is False
        assert SlackApprovalDecider.session_for(theirs, nonce=nonce) == ""

    @pytest.mark.asyncio
    async def test_a_raising_post_discards_the_window_and_propagates(self) -> None:
        decider = SlackApprovalDecider(session_key=_SESSION)
        client = _PressingSlackClient(raising=True)
        renderer = SlackRenderer(client, "C1", "t1", reactions_enabled=False, decider=decider)
        with pytest.raises(RuntimeError):
            await renderer.on_prompt_choice([], _RID, tool_title="bash")
        key, nonce = split_approval_token(client.pressed_token)
        assert key == _approval_registry_key(_SESSION, _RID)
        assert nonce, "the buttons carried a nonce, so pressing with it is the real test"
        assert key not in SlackApprovalDecider._REGISTRY
        assert SlackApprovalDecider.resolve_global(key, True, nonce=nonce) is False

    @pytest.mark.asyncio
    async def test_an_unanswered_window_denies_with_the_expiry_cause(self) -> None:
        decider = SlackApprovalDecider(session_key=_SESSION)
        nonce = decider.reserve(_RID)
        import kiro_crew.slack.renderer as sl

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sl, "_APPROVAL_TIMEOUT", 0.01)
            assert await asyncio.wait_for(decider(_event()), _TEST_WAIT_S) is False
        assert decider.last_deny_cause != ""
        key = _approval_registry_key(_SESSION, _RID)
        assert key not in SlackApprovalDecider._REGISTRY
        # The buttons outlive the prompt in the thread, so the nonce must not.
        assert SlackApprovalDecider.nonce_matches(key, nonce) is False

    @pytest.mark.asyncio
    async def test_a_cancelled_reservation_is_not_read_as_a_decision(self) -> None:
        decider = SlackApprovalDecider(session_key=_SESSION)
        nonce = decider.reserve(_RID)
        decider._futures[_RID].cancel()
        await asyncio.sleep(0)
        task = asyncio.create_task(decider(_event()))
        await asyncio.sleep(0)
        key = _approval_registry_key(_SESSION, _RID)
        assert SlackApprovalDecider.resolve_global(key, True, nonce=nonce) is True
        assert await asyncio.wait_for(task, _TEST_WAIT_S) is True

    @pytest.mark.asyncio
    async def test_an_unawaited_reservation_does_not_outlive_its_turn(self) -> None:
        mine = SlackApprovalDecider(session_key=_SESSION)
        my_nonce = mine.reserve(_RID)
        theirs = SlackApprovalDecider(session_key="slack:C9:t9")
        theirs.reserve(_RID)
        SlackApprovalDecider.discard_session(_SESSION)
        my_key = _approval_registry_key(_SESSION, _RID)
        assert my_key not in SlackApprovalDecider._REGISTRY
        assert _RID not in mine._futures
        # Swept means unusable, not merely unreachable: the buttons are still there.
        assert SlackApprovalDecider.nonce_matches(my_key, my_nonce) is False
        # Another session's live window is untouched.
        assert _approval_registry_key("slack:C9:t9", _RID) in SlackApprovalDecider._REGISTRY

    @pytest.mark.asyncio
    async def test_a_delivered_decision_nobody_awaited_is_swept_too(self) -> None:
        decider = SlackApprovalDecider(session_key=_SESSION)
        nonce = decider.reserve(_RID)
        key = _approval_registry_key(_SESSION, _RID)
        assert SlackApprovalDecider.resolve_global(key, True, nonce=nonce) is True
        SlackApprovalDecider.discard_session(_SESSION)
        assert key not in SlackApprovalDecider._REGISTRY
        assert _RID not in decider._futures
        # The buttons outlive the turn, so a retained nonce would still grant Trust.
        assert SlackApprovalDecider.nonce_matches(key, nonce) is False
        assert SlackApprovalDecider.session_for(key, nonce=nonce) == ""

    @pytest.mark.asyncio
    async def test_a_window_a_wait_owns_survives_the_teardown(self) -> None:
        """Ownership, not the future's state, is what the sweep spares."""
        decider = SlackApprovalDecider(session_key=_SESSION)
        nonce = decider.reserve(_RID)
        key = _approval_registry_key(_SESSION, _RID)
        SlackApprovalDecider._AWAITED.add(key)
        assert SlackApprovalDecider.resolve_global(key, True, nonce=nonce) is True
        SlackApprovalDecider.discard_session(_SESSION)
        assert key in SlackApprovalDecider._REGISTRY
        assert SlackApprovalDecider.nonce_matches(key, nonce) is True

    @pytest.mark.asyncio
    async def test_reserving_twice_keeps_the_future_the_waiter_holds(self) -> None:
        decider = SlackApprovalDecider(session_key=_SESSION)
        first_nonce = decider.reserve(_RID)
        first = decider._futures[_RID]
        second_nonce = decider.reserve(_RID)
        assert decider._futures[_RID] is first
        # A re-render reprints the buttons, so only the newest nonce may decide.
        assert second_nonce != first_nonce
        key = _approval_registry_key(_SESSION, _RID)
        assert SlackApprovalDecider.resolve_global(key, True, nonce=first_nonce) is False
        assert SlackApprovalDecider.resolve_global(key, True, nonce=second_nonce) is True


class TestTheSlackButtonCarriesAPerPromptNonce:
    """A Slack press must prove it came from the buttons now on screen.

    The registry key is ``session_key:request_id`` and both halves recur: request
    ids restart at ``1`` in each provider process, and a session key outlives any
    one prompt. A prompt that expired keeps its buttons in the thread, because
    only a decided press rewrites the message. So without a per-prompt value the
    key alone is the whole credential, and a press on a leftover button decides --
    or Trusts -- whatever request happens to hold that key now.
    """

    @pytest.mark.asyncio
    async def test_the_posted_button_carries_the_key_and_the_nonce(self) -> None:
        decider = SlackApprovalDecider(session_key=_SESSION)
        client = _PressingSlackClient()
        renderer = SlackRenderer(client, "C1", "t1", reactions_enabled=False, decider=decider)
        await renderer.on_prompt_choice([], _RID, tool_title="bash")
        key, nonce = split_approval_token(client.pressed_token)
        assert key == _approval_registry_key(_SESSION, _RID)
        assert nonce, "the buttons must carry this prompt's own nonce"
        assert client.press_accepted is True
        # The press decided it. Replaying the same button cannot change that: the
        # nonce still matches until the wait retires it, and the future is done.
        assert SlackApprovalDecider.resolve_global(key, False, nonce=nonce) is False

    @pytest.mark.asyncio
    async def test_a_button_from_the_previous_prompt_cannot_decide_this_one(self) -> None:
        """The reported defect: a stale button plus a replayed request id."""
        first = SlackApprovalDecider(session_key=_SESSION)
        stale_nonce = first.reserve(_RID)
        SlackApprovalDecider.discard_session(_SESSION)

        # A later turn in the same thread reaches the same request id again.
        second = SlackApprovalDecider(session_key=_SESSION)
        live_nonce = second.reserve(_RID)
        key = _approval_registry_key(_SESSION, _RID)
        assert stale_nonce != live_nonce

        assert SlackApprovalDecider.resolve_global(key, True, nonce=stale_nonce) is False
        assert _RID in second._futures and not second._futures[_RID].done()
        assert SlackApprovalDecider.resolve_global(key, True, nonce=live_nonce) is True

    @pytest.mark.asyncio
    async def test_a_stale_button_grants_no_session_trust(self) -> None:
        """Trust is the wider grant: the handler escalates BEFORE it resolves."""
        first = SlackApprovalDecider(session_key=_SESSION)
        stale_nonce = first.reserve(_RID)
        SlackApprovalDecider.discard_session(_SESSION)
        second = SlackApprovalDecider(session_key=_SESSION)
        live_nonce = second.reserve(_RID)
        key = _approval_registry_key(_SESSION, _RID)

        assert SlackApprovalDecider.session_for(key, nonce=stale_nonce) == ""
        assert SlackApprovalDecider.session_for(key, nonce=live_nonce) == _SESSION

    @pytest.mark.asyncio
    async def test_an_answered_prompt_grants_no_session_trust(self) -> None:
        """A second press after the first decided must not escalate the session.

        The nonce is retired by the wait's ``finally``, not by the decision, so
        between a Deny landing and that wait resuming both presses carry a nonce
        that still matches. Trust there would turn on blanket approval for every
        later tool in the session while the handler reports the press as expired.
        """
        decider = SlackApprovalDecider(session_key=_SESSION)
        nonce = decider.reserve(_RID)
        key = _approval_registry_key(_SESSION, _RID)
        assert SlackApprovalDecider.session_for(key, nonce=nonce) == _SESSION

        assert SlackApprovalDecider.resolve_global(key, False, nonce=nonce) is True
        # The nonce is still armed here -- that is precisely the exposed gap.
        assert SlackApprovalDecider.nonce_matches(key, nonce) is True
        assert SlackApprovalDecider.resolve_global(key, True, nonce=nonce) is False
        assert SlackApprovalDecider.session_for(key, nonce=nonce) == ""

    @pytest.mark.asyncio
    async def test_a_press_carrying_no_nonce_is_refused(self) -> None:
        decider = SlackApprovalDecider(session_key=_SESSION)
        decider.reserve(_RID)
        key = _approval_registry_key(_SESSION, _RID)
        assert SlackApprovalDecider.resolve_global(key, True, nonce="") is False
        assert SlackApprovalDecider.resolve_global(key, True) is False
        assert SlackApprovalDecider.session_for(key, nonce="") == ""

    @pytest.mark.asyncio
    async def test_a_press_carrying_the_wrong_nonce_is_refused(self) -> None:
        decider = SlackApprovalDecider(session_key=_SESSION)
        decider.reserve(_RID)
        key = _approval_registry_key(_SESSION, _RID)
        assert SlackApprovalDecider.resolve_global(key, True, nonce="not-the-one") is False

    @pytest.mark.asyncio
    async def test_a_key_with_no_window_matches_nothing(self) -> None:
        assert SlackApprovalDecider.nonce_matches("slack:C9:t9:99", "anything") is False

    def test_the_token_round_trips_through_a_session_key_holding_colons(self) -> None:
        """Slack session keys are ``slack:<channel>:<thread>``, so ``:`` is not a
        usable separator between the key and the nonce."""
        token = build_approval_token("slack:C1:t1", "7", "nonce-abc")
        key, nonce = split_approval_token(token)
        assert key == "slack:C1:t1:7"
        assert nonce == "nonce-abc"

    def test_a_token_minted_without_a_nonce_yields_an_empty_one(self) -> None:
        """A button predating this change carries no separator; it must not be
        read as carrying a nonce that happens to match."""
        token = build_approval_token("slack:C1:t1", "7", "")
        assert "|" not in token
        key, nonce = split_approval_token(token)
        assert key == "slack:C1:t1:7"
        assert nonce == ""


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
    async def test_a_delivered_decision_nobody_awaited_is_swept_too(self) -> None:
        decider = TeamsApprovalDecider(session_key=_SESSION)
        decider.arm(_RID, "n1")
        assert TeamsApprovalDecider.resolve_global(_SESSION, _RID, "n1", approved=True) is True
        decider.discard_reservations()
        assert registry_key(_SESSION, _RID) not in TeamsApprovalDecider._REGISTRY
        assert _RID not in decider._futures
        assert _RID not in decider._nonces

    @pytest.mark.asyncio
    async def test_arming_twice_keeps_the_future_the_waiter_holds(self) -> None:
        decider = TeamsApprovalDecider(session_key=_SESSION)
        decider.arm(_RID, "n1")
        first = decider._futures[_RID]
        decider.arm(_RID, "n2")
        assert decider._futures[_RID] is first


class TestTheSweepReachesTheRealRegistry:
    """The end-of-turn sweep must not travel through the construction seam.

    Each dispatcher holds a module-level name for its decider class and calls it
    to build the turn's decider. Callers and tests substitute that name to observe
    which decider a turn constructs, and a substitute need not be a class at all.
    Reservations live on the real class, so a sweep that resolved the class through
    that name would aim at the substitute: it raises on a plain function, and on a
    stand-in class it silently sweeps an empty registry and leaves the real window
    armed past the end of its turn.
    """

    def test_the_slack_sweep_is_the_class_that_holds_the_reservations(self) -> None:
        from kiro_crew.slack import transport_dispatch as slack_dispatch

        assert slack_dispatch._APPROVAL_REGISTRY is SlackApprovalDecider

    def test_the_telegram_sweep_is_the_class_that_holds_the_reservations(self) -> None:
        from kiro_crew.telegram import transport_dispatch as telegram_dispatch

        assert telegram_dispatch._APPROVAL_REGISTRY is TelegramApprovalDecider

    @pytest.mark.asyncio
    async def test_substituting_the_slack_construction_seam_leaves_the_sweep_working(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.slack import transport_dispatch as slack_dispatch

        def spy(*a: Any, **k: Any) -> Any:
            return SlackApprovalDecider(*a, **k)

        monkeypatch.setattr(slack_dispatch, "SlackApprovalDecider", spy)
        decider = spy(session_key=_SESSION)
        decider.reserve(_RID)
        assert _approval_registry_key(_SESSION, _RID) in SlackApprovalDecider._REGISTRY
        slack_dispatch._APPROVAL_REGISTRY.discard_session(_SESSION)
        assert _approval_registry_key(_SESSION, _RID) not in SlackApprovalDecider._REGISTRY

    @pytest.mark.asyncio
    async def test_substituting_the_telegram_construction_seam_leaves_the_sweep_working(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.telegram import transport_dispatch as telegram_dispatch

        def spy(*a: Any, **k: Any) -> Any:
            return TelegramApprovalDecider(*a, **k)

        monkeypatch.setattr(telegram_dispatch, "TelegramApprovalDecider", spy)
        key = TelegramApprovalDecider.key(_SESSION, _RID)
        TelegramApprovalDecider.arm(key, "n1")
        telegram_dispatch._APPROVAL_REGISTRY.discard_session(_SESSION)
        assert TelegramApprovalDecider.is_pending(key, "n1") is False

    @pytest.mark.parametrize(
        ("module_name", "seam"),
        [
            ("kiro_crew.slack.transport_dispatch", "SlackApprovalDecider"),
            ("kiro_crew.telegram.transport_dispatch", "TelegramApprovalDecider"),
        ],
    )
    def test_no_dispatcher_sweeps_through_its_construction_seam(
        self, module_name: str, seam: str
    ) -> None:
        """The source-level half, which the behavioural pins above cannot cover.

        Calling the sweep on the alias works whatever the dispatcher does, so only
        reading the dispatcher shows which name its own end-of-turn path uses.
        """
        import importlib
        import inspect

        source = inspect.getsource(importlib.import_module(module_name))
        assert f"{seam}.discard_session" not in source, (
            f"{module_name} sweeps reservations through {seam}, the name callers and "
            "tests substitute -- use the registry alias instead"
        )
        assert "_APPROVAL_REGISTRY.discard_session(session_key)" in source


class TestTheSweepSparesAWindowAWaitOwns:
    """A wait under a session key need not belong to the turn running the sweep.

    A spawn-approval prompt is armed under the PARENT session key and awaited by a
    detached task with its own window, so the parent turn's end-of-turn sweep runs
    while that wait is still live. Popping its future and nonce there would leave
    the operator holding buttons that resolve nothing, and the spawn would deny at
    its own timeout on a refusal nobody made.
    """

    @pytest.mark.asyncio
    async def test_telegram_spares_a_detached_window_before_its_wait_starts(self) -> None:
        """The exposed span is the SEND, before the detached wait has claimed anything.

        The spawn gate arms, then suspends in the post. Its own task has not reached
        the decider yet, so ``__call__`` has claimed nothing -- and the turn that
        asked for the spawn has already returned, because admission runs this gate
        in a task of its own. Its sweep therefore lands on a window whose buttons
        are on screen and whose wait is still one await away.
        """
        spawn_rid = "spawn:sub-1"
        spawn_key = TelegramApprovalDecider.key(_SESSION, spawn_rid)
        TelegramApprovalDecider.arm(spawn_key, "n-spawn", detached=True)

        # The parent turn ends here: the gate is still inside its send.
        TelegramApprovalDecider.discard_session(_SESSION)

        assert TelegramApprovalDecider.is_pending(spawn_key, "n-spawn") is True
        decider = TelegramApprovalDecider(session_key=_SESSION)
        waiter = asyncio.create_task(decider(_event(spawn_rid)))
        await asyncio.sleep(0)
        assert TelegramApprovalDecider.resolve_global(spawn_key, True, nonce="n-spawn") is True
        assert await asyncio.wait_for(waiter, _TEST_WAIT_S) is True

    @pytest.mark.asyncio
    async def test_telegram_sweeps_an_undeclared_window_before_its_wait_starts(self) -> None:
        """Without the declaration there is nothing to tell this from an orphan."""
        key = TelegramApprovalDecider.key(_SESSION, _RID)
        TelegramApprovalDecider.arm(key, "n-tool")
        TelegramApprovalDecider.discard_session(_SESSION)
        assert TelegramApprovalDecider.is_pending(key, "n-tool") is False

    @pytest.mark.asyncio
    async def test_telegram_retire_releases_a_detached_claim(self) -> None:
        """A gate that falls through must not leave its key exempt for good.

        The spawn gate retires and falls through to Slack when the post fails or the
        destination stops being authorized. Keeping the claim would make every
        later sweep skip that key, so a real orphan at the same id would survive.
        """
        spawn_rid = "spawn:sub-1"
        spawn_key = TelegramApprovalDecider.key(_SESSION, spawn_rid)
        TelegramApprovalDecider.arm(spawn_key, "n-spawn", detached=True)
        TelegramApprovalDecider.retire(spawn_key)
        assert spawn_key not in TelegramApprovalDecider._AWAITED

        TelegramApprovalDecider.arm(spawn_key, "n-again")
        TelegramApprovalDecider.discard_session(_SESSION)
        assert TelegramApprovalDecider.is_pending(spawn_key, "n-again") is False

    @pytest.mark.asyncio
    async def test_telegram_spares_the_key_a_detached_wait_is_holding(self) -> None:
        spawn_rid = "spawn:sub-1"
        spawn_key = TelegramApprovalDecider.key(_SESSION, spawn_rid)
        TelegramApprovalDecider.arm(spawn_key, "n-spawn")
        decider = TelegramApprovalDecider(session_key=_SESSION)
        waiter = asyncio.create_task(decider(_event(spawn_rid)))
        await asyncio.sleep(0)  # let the wait take ownership of the key

        # The parent turn ends while that detached wait is still pending.
        TelegramApprovalDecider.discard_session(_SESSION)

        assert TelegramApprovalDecider.is_pending(spawn_key, "n-spawn") is True
        assert TelegramApprovalDecider.resolve_global(spawn_key, True, nonce="n-spawn") is True
        assert await asyncio.wait_for(waiter, _TEST_WAIT_S) is True

    @pytest.mark.asyncio
    async def test_telegram_still_drops_an_unawaited_window_beside_it(self) -> None:
        spawn_rid = "spawn:sub-1"
        spawn_key = TelegramApprovalDecider.key(_SESSION, spawn_rid)
        TelegramApprovalDecider.arm(spawn_key, "n-spawn")
        decider = TelegramApprovalDecider(session_key=_SESSION)
        waiter = asyncio.create_task(decider(_event(spawn_rid)))
        await asyncio.sleep(0)

        # A tool prompt whose turn ended before the driver reached the decider.
        tool_key = TelegramApprovalDecider.key(_SESSION, _RID)
        TelegramApprovalDecider.arm(tool_key, "n-tool")

        TelegramApprovalDecider.discard_session(_SESSION)

        assert TelegramApprovalDecider.is_pending(tool_key, "n-tool") is False
        assert TelegramApprovalDecider.resolve_global(tool_key, True, nonce="n-tool") is False
        # The owned one is untouched.
        assert TelegramApprovalDecider.is_pending(spawn_key, "n-spawn") is True
        TelegramApprovalDecider.resolve_global(spawn_key, False, nonce="n-spawn")
        assert await asyncio.wait_for(waiter, _TEST_WAIT_S) is False

    @pytest.mark.asyncio
    async def test_telegram_releases_ownership_when_the_wait_ends(self) -> None:
        key = TelegramApprovalDecider.key(_SESSION, _RID)
        TelegramApprovalDecider.arm(key, "n1")
        decider = TelegramApprovalDecider(session_key=_SESSION)
        waiter = asyncio.create_task(decider(_event(_RID)))
        await asyncio.sleep(0)
        assert key in TelegramApprovalDecider._AWAITED
        TelegramApprovalDecider.resolve_global(key, True, nonce="n1")
        assert await asyncio.wait_for(waiter, _TEST_WAIT_S) is True
        assert key not in TelegramApprovalDecider._AWAITED

    @pytest.mark.asyncio
    async def test_slack_spares_the_key_a_wait_is_holding(self) -> None:
        decider = SlackApprovalDecider(session_key=_SESSION)
        nonce = decider.reserve(_RID)
        waiter = asyncio.create_task(decider(_event(_RID)))
        await asyncio.sleep(0)
        key = _approval_registry_key(_SESSION, _RID)

        SlackApprovalDecider.discard_session(_SESSION)

        assert SlackApprovalDecider._REGISTRY.get(key) is decider
        assert SlackApprovalDecider.resolve_global(key, True, nonce=nonce) is True
        assert await asyncio.wait_for(waiter, _TEST_WAIT_S) is True

    @pytest.mark.asyncio
    async def test_slack_releases_ownership_when_the_wait_ends(self) -> None:
        decider = SlackApprovalDecider(session_key=_SESSION)
        key = _approval_registry_key(_SESSION, _RID)
        nonce = decider.reserve(_RID)
        waiter = asyncio.create_task(decider(_event(_RID)))
        await asyncio.sleep(0)
        assert key in SlackApprovalDecider._AWAITED
        SlackApprovalDecider.resolve_global(key, True, nonce=nonce)
        assert await asyncio.wait_for(waiter, _TEST_WAIT_S) is True
        assert key not in SlackApprovalDecider._AWAITED
