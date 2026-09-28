"""A Discord approval press that lands before the waiter future is registered.

``TurnDriver`` dispatches ``PROMPT_CHOICE`` to the renderer and only then awaits
the decider. The renderer suspends in between -- a thread hop for the
display-safety scan, then the send -- so a press can be processed on the same
loop while no future is registered yet. These pins fix where the decision window
opens (when the nonce is armed) and what still fails closed inside it.

The isolation guarantees are pinned alongside the fix, because opening the window
earlier is only safe while they hold: a press with no armed nonce, a press with a
foreign nonce, and a decision left unawaited must none of them be able to answer
a request the user was never asked about.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew.discord import renderer as discord_renderer
from kiro_crew.discord.renderer import DiscordApprovalDecider, DiscordRenderer
from kiro_crew.discord.transport import DISCORD_CAPABILITIES


class _SendCapture:
    """Minimal outbound capture: only the approval prompt's own send is used."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, object]] = []
        self.raise_on_send = False
        #: When set, every send returns None, which is how the real client reports
        #: a failure: a revoked token, a channel it cannot write to, a rate limit
        #: or 5xx past its retries. It does NOT raise for those.
        self.fail_sends = False

    async def send_message(
        self,
        channel_id: str,
        text: str,
        *,
        components: object = None,
        reply_to_message_id: object = None,
    ) -> str | None:
        if self.raise_on_send:
            raise RuntimeError("send exploded")
        self.sent.append((channel_id, text, components))
        return None if self.fail_sends else "m1"


class _PressDuringSend(_SendCapture):
    """Presses Approve from inside the send, which is the real defect shape.

    The press runs while the renderer is suspended on the send and before the
    driver has reached the decider, which is the widest part of the gap.
    """

    def __init__(self, key: str, *, approved: bool = True) -> None:
        super().__init__()
        self._key = key
        self._approved = approved
        #: What ``resolve_global`` answered the presser. False is the user-visible
        #: half of the defect: the handler reports the approval as expired.
        self.press_resolved: bool | None = None

    async def send_message(
        self,
        channel_id: str,
        text: str,
        *,
        components: object = None,
        reply_to_message_id: object = None,
    ) -> str | None:
        nonce = _nonce_from(components)
        self.press_resolved = DiscordApprovalDecider.resolve_global(
            self._key, self._approved, nonce=nonce
        )
        return await super().send_message(
            channel_id, text, components=components, reply_to_message_id=reply_to_message_id
        )


def _nonce_from(components: object) -> str:
    """The nonce Discord would send back with a press on these buttons."""
    assert isinstance(components, list) and components
    row = components[0]
    assert isinstance(row, dict)
    approve = row["components"][0]
    # ``a:<request_id>:<nonce>:<1|0>``
    return str(approve["custom_id"]).split(":")[2]


@pytest.fixture(autouse=True)
def _clean_registry():
    """No cross-test bleed through the decider's process-global maps."""
    DiscordApprovalDecider._REGISTRY.clear()
    DiscordApprovalDecider._NONCES.clear()
    yield
    DiscordApprovalDecider._REGISTRY.clear()
    DiscordApprovalDecider._NONCES.clear()


def _renderer(client: object) -> DiscordRenderer:
    return DiscordRenderer(
        client,  # type: ignore[arg-type]
        "chan1",
        DISCORD_CAPABILITIES,
        session_key="sk",
    )


class TestPressBeforeTheWaiter:
    """The defect: a press in the gap was dropped and reported as expired."""

    @pytest.mark.asyncio
    async def test_press_while_the_prompt_is_being_sent_is_honored(self) -> None:
        key = DiscordApprovalDecider.key("sk", "r1")
        cli = _PressDuringSend(key)
        await _renderer(cli).on_prompt_choice([], "r1", tool_title="shell")
        # The presser was told the decision landed, not that it had expired.
        assert cli.press_resolved is True
        # And the driver's later wait reads that decision instead of denying.
        decider = DiscordApprovalDecider(session_key="sk")
        assert await decider(SimpleNamespace(request_id="r1")) is True
        assert decider.last_deny_cause == ""

    @pytest.mark.asyncio
    async def test_deny_press_in_the_gap_is_honored_as_a_denial(self) -> None:
        key = DiscordApprovalDecider.key("sk", "r2")
        cli = _PressDuringSend(key, approved=False)
        await _renderer(cli).on_prompt_choice([], "r2", tool_title="shell")
        assert cli.press_resolved is True
        decider = DiscordApprovalDecider(session_key="sk")
        assert await decider(SimpleNamespace(request_id="r2")) is False

    @pytest.mark.asyncio
    async def test_the_recorded_decision_is_consumed_once(self) -> None:
        """A second request reusing the id gets its own window, not this answer."""
        key = DiscordApprovalDecider.key("sk", "r3")
        cli = _PressDuringSend(key)
        await _renderer(cli).on_prompt_choice([], "r3", tool_title="shell")
        decider = DiscordApprovalDecider(session_key="sk")
        assert await decider(SimpleNamespace(request_id="r3")) is True
        assert key not in DiscordApprovalDecider._REGISTRY
        assert key not in DiscordApprovalDecider._NONCES


class TestWindowStillFailsClosed:
    """Opening the window earlier must not widen what a press can reach."""

    @pytest.mark.asyncio
    async def test_press_before_the_nonce_is_armed_is_refused(self) -> None:
        key = DiscordApprovalDecider.key("sk", "r4")
        assert DiscordApprovalDecider.resolve_global(key, True, nonce="whatever") is False

    @pytest.mark.asyncio
    async def test_foreign_nonce_in_the_gap_is_refused_and_leaves_the_window_live(
        self,
    ) -> None:
        key = DiscordApprovalDecider.key("sk", "r5")
        DiscordApprovalDecider.register_nonce(key)
        assert DiscordApprovalDecider.resolve_global(key, True, nonce="deadbeefdeadbeef") is False
        reserved = DiscordApprovalDecider._REGISTRY.get(key)
        assert reserved is not None and not reserved.done()

    @pytest.mark.asyncio
    async def test_a_decision_left_unawaited_cannot_answer_the_next_request(self) -> None:
        """The replay bound. Same key, new prompt, so the old answer is void."""
        key = DiscordApprovalDecider.key("sk", "r6")
        stale_nonce = DiscordApprovalDecider.register_nonce(key)
        assert DiscordApprovalDecider.resolve_global(key, True, nonce=stale_nonce) is True
        # The turn died before the driver reached the decider: nobody read it.
        fresh_nonce = DiscordApprovalDecider.register_nonce(key)
        assert fresh_nonce != stale_nonce
        # The old press cannot reach the new prompt.
        assert DiscordApprovalDecider.resolve_global(key, True, nonce=stale_nonce) is False
        # Nor can the recorded approval be adopted by the new request's wait.
        reserved = DiscordApprovalDecider._REGISTRY.get(key)
        assert reserved is not None and not reserved.done()

    @pytest.mark.asyncio
    async def test_a_second_arm_does_not_orphan_a_live_waiter(self) -> None:
        """Re-arming keeps the future the waiter is blocked on."""
        decider = DiscordApprovalDecider(session_key="sk")
        task = asyncio.ensure_future(decider(SimpleNamespace(request_id="r7")))
        await asyncio.sleep(0)
        key = DiscordApprovalDecider.key("sk", "r7")
        nonce = DiscordApprovalDecider.register_nonce(key)
        assert DiscordApprovalDecider.resolve_global(key, True, nonce=nonce) is True
        assert await task is True


class TestPromptThatNeverWentOut:
    @pytest.mark.asyncio
    async def test_retire_closes_the_window(self) -> None:
        key = DiscordApprovalDecider.key("sk", "r8")
        nonce = DiscordApprovalDecider.register_nonce(key)
        DiscordApprovalDecider.retire(key)
        assert DiscordApprovalDecider.resolve_global(key, True, nonce=nonce) is False
        assert key not in DiscordApprovalDecider._REGISTRY
        # Idempotent: a caller that retires twice is not an error.
        DiscordApprovalDecider.retire(key)

    @pytest.mark.asyncio
    async def test_a_failed_send_retires_the_window_and_raises(self) -> None:
        cli = _SendCapture()
        cli.raise_on_send = True
        key = DiscordApprovalDecider.key("sk", "r9")
        with pytest.raises(RuntimeError):
            await _renderer(cli).on_prompt_choice([], "r9", tool_title="shell")
        assert key not in DiscordApprovalDecider._NONCES
        assert key not in DiscordApprovalDecider._REGISTRY

    @pytest.mark.asyncio
    async def test_a_send_that_returns_no_id_denies_at_once(self) -> None:
        """The failure the real client actually reports: no id, no exception.

        Dropping the reservation here would look tidy and still cost the user the
        whole decision window on a prompt nobody can see, so the refusal is
        recorded for the wait to adopt.
        """
        cli = _SendCapture()
        cli.fail_sends = True
        await _renderer(cli).on_prompt_choice([], "r11", tool_title="shell")
        decider = DiscordApprovalDecider(session_key="sk")
        # No timeout patch: a window that is waited out would hang this test.
        assert await asyncio.wait_for(decider(SimpleNamespace(request_id="r11")), 1) is False
        key = DiscordApprovalDecider.key("sk", "r11")
        assert key not in DiscordApprovalDecider._REGISTRY
        assert key not in DiscordApprovalDecider._NONCES

    @pytest.mark.asyncio
    async def test_refuse_undelivered_cannot_overwrite_a_real_decision(self) -> None:
        """A press that already landed outranks a later delivery verdict."""
        key = DiscordApprovalDecider.key("sk", "r12")
        nonce = DiscordApprovalDecider.register_nonce(key)
        assert DiscordApprovalDecider.resolve_global(key, True, nonce=nonce) is True
        DiscordApprovalDecider.refuse_undelivered(key)
        decider = DiscordApprovalDecider(session_key="sk")
        assert await asyncio.wait_for(decider(SimpleNamespace(request_id="r12")), 1) is True

    @pytest.mark.asyncio
    async def test_no_press_still_denies_when_the_window_elapses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(discord_renderer, "_APPROVAL_TIMEOUT_S", 0.01)
        cli = _SendCapture()
        key = DiscordApprovalDecider.key("sk", "r10")
        await _renderer(cli).on_prompt_choice([], "r10", tool_title="shell")
        decider = DiscordApprovalDecider(session_key="sk")
        assert await decider(SimpleNamespace(request_id="r10")) is False
        assert key not in DiscordApprovalDecider._REGISTRY
        assert key not in DiscordApprovalDecider._NONCES


class TestTurnTeardown:
    """A reservation must not outlive the turn that opened it."""

    @pytest.mark.asyncio
    async def test_discard_session_closes_a_window_no_wait_ever_adopted(self) -> None:
        """The prompt went out, then the turn ended before the decider."""
        cli = _SendCapture()
        key = DiscordApprovalDecider.key("sk", "r13")
        await _renderer(cli).on_prompt_choice([], "r13", tool_title="shell")
        nonce = _nonce_from(cli.sent[0][2])
        DiscordApprovalDecider.discard_session("sk")
        assert key not in DiscordApprovalDecider._REGISTRY
        assert key not in DiscordApprovalDecider._NONCES
        # The buttons left in the channel resolve nothing.
        assert DiscordApprovalDecider.resolve_global(key, True, nonce=nonce) is False

    @pytest.mark.asyncio
    async def test_discard_session_does_not_reach_a_sibling_key(self) -> None:
        """``sk`` must not match ``sk2``: the prefix carries its own separator."""
        mine = DiscordApprovalDecider.key("sk", "r14")
        other = DiscordApprovalDecider.key("sk2", "r14")
        DiscordApprovalDecider.register_nonce(mine)
        DiscordApprovalDecider.register_nonce(other)
        DiscordApprovalDecider.discard_session("sk")
        assert mine not in DiscordApprovalDecider._REGISTRY
        assert other in DiscordApprovalDecider._REGISTRY

    @pytest.mark.asyncio
    async def test_discard_session_keeps_a_decision_already_recorded(self) -> None:
        """A press that landed is a delivered decision, not a stale reservation."""
        key = DiscordApprovalDecider.key("sk", "r15")
        nonce = DiscordApprovalDecider.register_nonce(key)
        assert DiscordApprovalDecider.resolve_global(key, True, nonce=nonce) is True
        DiscordApprovalDecider.discard_session("sk")
        decider = DiscordApprovalDecider(session_key="sk")
        assert await asyncio.wait_for(decider(SimpleNamespace(request_id="r15")), 1) is True
