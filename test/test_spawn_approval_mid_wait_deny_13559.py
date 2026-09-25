"""A `channels` deny landing MID-WAIT falls through instead of voting for the operator.

The seam consults the operator's `channels` ceiling before a hook posts its
spawn-approval prompt, so a channel already denied is never prompted. That covers
only half the window: the host gate holds a spawn for as long as its approval
takes, so a deny can land while the prompt is still pending. From that moment the
channel's callback path drops the answering press -- every press except an explicit
reject -- so the prompt is unanswerable, the deny-by-default wait elapses, and a
bare ``False`` reaches the host gate, which returns it as the decision. The spawn
is refused on a prompt the operator has no way to answer, and is never re-offered
on the still-permitted Slack DM or dashboard surface.

Five cases are pinned, which together are the whole window:

* PRE-POST deny -- denied before the hook runs: no hook, no prompt, ``None``;
* MID-WAIT deny -- permitted at post, denied when the unpressed wait elapses:
  ``None`` (fall through), NOT ``False``;
* EXPLICIT REJECT -- a Deny press, which a denied channel's drop exempts, stays
  ``False`` even when the channel was denied while the prompt was pending;
* TIMEOUT STILL PERMITTED -- an unpressed wait on a channel the operator never
  denied is a real deny-by-default and keeps answering ``False``;
* FALLBACK -- a hook that cannot surface the prompt, or one that raises, still
  answers ``None``, so a delivery fault degrades to Slack/dashboard.

Answerability can also end by the conversation's OWN authorization rather than the
`channels` ceiling. `on_callback` checks that FIRST, for every press, with no reject
exemption: the peer roster gates every press, and a Topic passes the shared
`forum_gate_outcome` predicate as well. A peer dropped from the roster, or a Topic
dropped from the allow-list, therefore silences even a reject while the ceiling still
permits, so that authority is re-read when the wait elapses too -- for a Topic, whose
route names no single peer, by asking whether the roster admits anybody at all.

All Telegram client I/O is faked; nothing touches the network, and the ceiling
predicate is substituted rather than driven through a real profile store.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

# Reuse the doubles the Telegram suite already uses (fake client, fake sessions,
# dispatcher factory) so this suite exercises a REAL registered hook.
from test_telegram import _dispatcher  # noqa: E402

from kiro_crew.constants import DENY_CAUSE_APPROVAL_TIMEOUT
from kiro_crew.messaging import spawn_approval_delivery as seam
from kiro_crew.telegram.renderer import TelegramApprovalDecider

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

_RID = "spawn:abc"
_TASK = "spawn_run(build the widget)"


@pytest.fixture(autouse=True)
def _clean_registries():
    """Start and end with an empty seam registry and no armed prompts."""
    seam.clear_channel_delivery_hooks()
    TelegramApprovalDecider._REGISTRY.clear()
    TelegramApprovalDecider._NONCES.clear()
    yield
    seam.clear_channel_delivery_hooks()
    TelegramApprovalDecider._REGISTRY.clear()
    TelegramApprovalDecider._NONCES.clear()


@pytest.fixture(autouse=True)
def _short_prompt_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the deny-by-default wait, which several cases here run to the end.

    The real window is minutes, so without this every elapsed-wait assertion below
    would park the suite on it rather than failing.
    """
    import kiro_crew.telegram.renderer as renderer_mod

    monkeypatch.setattr(renderer_mod, "_APPROVAL_TIMEOUT_S", 0.2)


@pytest.fixture
def _ceiling(monkeypatch: pytest.MonkeyPatch):
    """Substitute the ceiling predicate, defaulting to PERMIT.

    The real predicate hops to a thread pool and walks the profile store, so
    leaving it in place would couple these tests to an executor round-trip instead
    of to the delivery behaviour they are about.

    ``state["permitted"]`` is read on every call rather than captured, which is
    what lets a test flip the answer BETWEEN the pre-post consult and the one
    behind the elapsed wait -- the mid-wait deny this file is about.
    """

    calls: list[str] = []
    state = {"permitted": True}

    async def _permitted(channel_type: str) -> bool:
        calls.append(channel_type)
        return bool(state["permitted"])

    monkeypatch.setattr(seam, "channel_inbound_permitted", _permitted)
    return SimpleNamespace(calls=calls, state=state)


async def _await_armed(session_key: str) -> str:
    """Wait until the hook has armed its prompt, and return the live nonce."""
    key = TelegramApprovalDecider.key(session_key, _RID)
    for _ in range(200):
        if key in TelegramApprovalDecider._NONCES and key in TelegramApprovalDecider._REGISTRY:
            return TelegramApprovalDecider._NONCES[key]
        await asyncio.sleep(0.01)
    raise AssertionError("the hook never armed its prompt")


async def _press(dispatcher, session_key: str, flag: str) -> None:
    """Press an inline button in the DM, exactly as the real callback path does."""
    nonce = await _await_armed(session_key)
    await dispatcher.on_callback(
        SimpleNamespace(
            callback_query_id="q1",
            user_id=7,
            chat_id=7,
            message_id=100,
            data=f"a:{_RID}:{nonce}:{flag}",
            label="",
            chat_type="private",
        )
    )


class TestAMidWaitDenyFallsThrough:
    """A deny that lands while the prompt is pending withholds the channel."""

    def test_a_deny_landing_mid_wait_answers_none_not_false(self, _ceiling) -> None:
        # The whole issue. Permitted when the prompt posts, denied by the time the
        # unpressed wait elapses: the operator has no way to answer here, so the
        # elapsed wait is not their refusal.
        d, cli, _sess = _dispatcher({7})
        session_key = d._session_key(("direct", "7"))
        seam.register_channel_delivery("telegram", d.deliver_spawn_approval)

        async def _go() -> bool | None:
            task = asyncio.ensure_future(seam.deliver_spawn_approval(_RID, _TASK, session_key))
            await _await_armed(session_key)  # the prompt is posted and pending
            _ceiling.state["permitted"] = False  # the deny lands mid-wait
            return await task

        result = asyncio.run(_go())

        assert result is None
        # Spelled separately because ``False`` is the bug's value and ``None is
        # False`` would not distinguish it from a falsey answer.
        assert result is not False
        assert len(cli.sent) == 1  # it WAS surfaced; the answer is what changed
        # Asked twice about the same channel: once before the post, once behind the
        # elapsed wait. The second consult is the fix.
        assert _ceiling.calls == ["telegram", "telegram"]

    def test_the_host_gate_falls_through_on_that_none(self, _ceiling) -> None:
        # The gate returns a non-None channel answer VERBATIM, so None is the only
        # value that reaches its Slack/dashboard path. Pin the contract this fix
        # depends on rather than assuming it.
        fell_through: list[str] = []

        async def _host_gate(rid: str, desc: str, parent: str) -> bool:
            channel_decision = await seam.deliver_spawn_approval(rid, desc, parent)
            if channel_decision is not None:
                return channel_decision
            fell_through.append(rid)
            return True  # stands in for the Slack-DM/dashboard prompt

        d, _cli, _sess = _dispatcher({7})
        session_key = d._session_key(("direct", "7"))
        seam.register_channel_delivery("telegram", d.deliver_spawn_approval)

        async def _go() -> bool:
            task = asyncio.ensure_future(_host_gate(_RID, _TASK, session_key))
            await _await_armed(session_key)
            _ceiling.state["permitted"] = False
            return await task

        assert asyncio.run(_go()) is True
        assert fell_through == [_RID]

    def test_an_explicit_reject_stays_false_through_a_mid_wait_deny(self, _ceiling) -> None:
        # A denied channel drops every press EXCEPT an explicit reject, so a Deny
        # press is still the operator's own decision and must be reported as one.
        # Converting it to None would re-offer, on Slack, a spawn they just refused.
        d, _cli, _sess = _dispatcher({7})
        session_key = d._session_key(("direct", "7"))
        seam.register_channel_delivery("telegram", d.deliver_spawn_approval)

        async def _go() -> bool | None:
            task = asyncio.ensure_future(seam.deliver_spawn_approval(_RID, _TASK, session_key))
            await _press(d, session_key, "0")  # the operator rejects
            _ceiling.state["permitted"] = False  # and the deny lands after
            return await task

        result = asyncio.run(_go())

        assert result is False
        assert result is not None
        # No second consult: a press is never re-read against the ceiling.
        assert _ceiling.calls == ["telegram"]

    def test_a_timeout_on_a_still_permitted_channel_still_answers_false(self, _ceiling) -> None:
        # The scope line of the issue. An unpressed wait on a channel the operator
        # never denied is a real deny-by-default and is NOT part of this gap.
        d, cli, _sess = _dispatcher({7})
        session_key = d._session_key(("direct", "7"))
        seam.register_channel_delivery("telegram", d.deliver_spawn_approval)

        result = asyncio.run(seam.deliver_spawn_approval(_RID, _TASK, session_key))

        assert result is False
        assert result is not None
        assert len(cli.sent) == 1
        assert _ceiling.calls == ["telegram", "telegram"]

    def test_a_pre_post_deny_still_invokes_no_hook(self, _ceiling) -> None:
        # The other half of the window, unchanged: denied before the prompt posts,
        # so nothing is surfaced and no nonce is armed.
        d, cli, _sess = _dispatcher({7})
        session_key = d._session_key(("direct", "7"))
        seam.register_channel_delivery("telegram", d.deliver_spawn_approval)
        _ceiling.state["permitted"] = False

        result = asyncio.run(seam.deliver_spawn_approval(_RID, _TASK, session_key))

        assert result is None
        assert cli.sent == []
        key = TelegramApprovalDecider.key(session_key, _RID)
        assert key not in TelegramApprovalDecider._NONCES
        assert key not in TelegramApprovalDecider._REGISTRY
        assert _ceiling.calls == ["telegram"]

    def test_an_approve_press_is_unaffected(self, _ceiling) -> None:
        # The permitted path is untouched: a press still resolves, and the ceiling
        # is asked once (before the post) and never behind a wait that never
        # elapsed.
        d, cli, _sess = _dispatcher({7})
        session_key = d._session_key(("direct", "7"))
        seam.register_channel_delivery("telegram", d.deliver_spawn_approval)

        async def _go() -> bool | None:
            task = asyncio.ensure_future(seam.deliver_spawn_approval(_RID, _TASK, session_key))
            await _press(d, session_key, "1")
            return await task

        assert asyncio.run(_go()) is True
        assert len(cli.sent) == 1
        assert _ceiling.calls == ["telegram"]


class TestTheSeamOwnsTheReading:
    """The hook reports an unpressed wait; the seam decides what it means."""

    def test_the_helper_answers_false_while_permitted_and_none_once_denied(self, _ceiling) -> None:
        # The decision in isolation, both directions, so a regression names which
        # half broke rather than only failing an end-to-end case.
        assert asyncio.run(seam.unpressed_wait_answer("telegram", _RID)) is False

        _ceiling.state["permitted"] = False
        assert asyncio.run(seam.unpressed_wait_answer("telegram", _RID)) is None

        assert _ceiling.calls == ["telegram", "telegram"]

    def test_the_helper_asks_about_the_channel_it_was_given(self, _ceiling) -> None:
        _ceiling.state["permitted"] = False
        assert asyncio.run(seam.unpressed_wait_answer("discord", _RID)) is None
        assert _ceiling.calls == ["discord"]

    def test_a_hook_answering_a_bare_false_is_passed_through(self, _ceiling) -> None:
        # A hook that never calls the helper keeps today's behaviour, even under a
        # deny. The seam cannot tell such a False from a press, and reading it as
        # unpressed would convert an explicit reject into a fall-through.
        async def _hook(_rid: str, _desc: str, _parent: str) -> bool:
            _ceiling.state["permitted"] = False
            return False

        seam.register_channel_delivery("telegram", _hook)

        assert asyncio.run(seam.deliver_spawn_approval(_RID, _TASK, "telegram:k:direct:7")) is False

    def test_the_telegram_hook_reports_only_an_unpressed_wait(self, _ceiling) -> None:
        # The hook's branch keys off the decider's own deny cause, so the seam is
        # consulted for an elapsed wait and never for a press.
        asked: list[tuple[str, str]] = []

        async def _answer(channel: str, request_id: str) -> bool | None:
            asked.append((channel, request_id))
            return None

        import kiro_crew.telegram.transport_dispatch as td

        d, _cli, _sess = _dispatcher({7})
        session_key = d._session_key(("direct", "7"))

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(td, "unpressed_wait_answer", _answer)

            async def _pressed() -> bool | None:
                task = asyncio.ensure_future(d.deliver_spawn_approval(_RID, _TASK, session_key))
                await _press(d, session_key, "0")
                return await task

            assert asyncio.run(_pressed()) is False
            assert asked == []  # a press is never routed to the seam

            TelegramApprovalDecider._REGISTRY.clear()
            TelegramApprovalDecider._NONCES.clear()

            assert asyncio.run(d.deliver_spawn_approval(_RID, _TASK, session_key)) is None
            assert asked == [("telegram", _RID)]  # the elapsed wait is

    def test_the_deny_cause_the_branch_reads_is_the_timeout_one(self) -> None:
        # The hook's branch compares against this constant, and the decider sets
        # exactly it on an elapsed wait. Pin the pairing so a rename of either side
        # cannot silently turn the branch off and restore the bug.
        from kiro_crew.telegram import renderer as renderer_mod

        src = renderer_mod.__loader__.get_source("kiro_crew.telegram.renderer")
        assert "self.last_deny_cause = DENY_CAUSE_APPROVAL_TIMEOUT" in src
        assert DENY_CAUSE_APPROVAL_TIMEOUT == "approval_timeout"


class TestTheConversationAuthorityAlsoEndsAnswerability:
    """A roster or Topic withdrawal mid-wait silences every press, reject included."""

    def test_a_roster_withdrawal_mid_wait_answers_none_not_false(self, _ceiling) -> None:
        # `on_callback` checks this conversation's authorization FIRST, for every
        # press, with no exemption -- so a peer dropped from the roster cannot even
        # reject. The wait then elapses on a prompt nothing could answer, while the
        # `channels` ceiling is untouched and still permits.
        d, cli, _sess = _dispatcher({7})
        session_key = d._session_key(("direct", "7"))
        seam.register_channel_delivery("telegram", d.deliver_spawn_approval)

        async def _go() -> bool | None:
            task = asyncio.ensure_future(seam.deliver_spawn_approval(_RID, _TASK, session_key))
            await _await_armed(session_key)
            d._allowed = set()  # the operator drops the peer, as reconfigure does
            return await task

        result = asyncio.run(_go())

        assert result is None
        assert result is not False
        assert len(cli.sent) == 1  # it WAS surfaced
        # The channels ceiling never denied, so it cannot be what produced the
        # fall-through: the conversation authority did.
        assert _ceiling.state["permitted"] is True

    def test_that_withdrawal_is_invisible_to_the_channels_ceiling(self, _ceiling) -> None:
        # Spelled separately: the seam's answer for a permitted channel is False, so
        # a fix that asked only the ceiling would refuse this spawn.
        assert asyncio.run(seam.unpressed_wait_answer("telegram", _RID)) is False

    def test_a_press_before_the_withdrawal_still_reports_its_decision(self, _ceiling) -> None:
        # The withdrawal lands AFTER the operator rejected. That is a real decision
        # and stays False rather than becoming a fall-through.
        d, _cli, _sess = _dispatcher({7})
        session_key = d._session_key(("direct", "7"))
        seam.register_channel_delivery("telegram", d.deliver_spawn_approval)

        async def _go() -> bool | None:
            task = asyncio.ensure_future(seam.deliver_spawn_approval(_RID, _TASK, session_key))
            await _press(d, session_key, "0")
            d._allowed = set()
            return await task

        assert asyncio.run(_go()) is False

    def test_an_authorized_conversation_on_a_permitted_channel_still_denies(self, _ceiling) -> None:
        # The new check must not over-trigger: nothing was withdrawn, so an
        # unpressed wait is still the operator declining to answer.
        d, _cli, _sess = _dispatcher({7})
        session_key = d._session_key(("direct", "7"))
        seam.register_channel_delivery("telegram", d.deliver_spawn_approval)

        assert asyncio.run(seam.deliver_spawn_approval(_RID, _TASK, session_key)) is False

    def test_an_egress_revocation_mid_wait_also_falls_through(self, _ceiling) -> None:
        # The transport's own revocation-at-egress decision is part of the same
        # destination reading, so a transport that stops permitting the chat also
        # makes the elapsed wait a fall-through.
        d, _cli, _sess = _dispatcher({7})
        session_key = d._session_key(("direct", "7"))
        seam.register_channel_delivery("telegram", d.deliver_spawn_approval)
        allow = {"ok": True}
        d.transport = SimpleNamespace(  # type: ignore[assignment]
            may_send_to=lambda _c, _t=None, **_k: allow["ok"]
        )

        async def _go() -> bool | None:
            task = asyncio.ensure_future(seam.deliver_spawn_approval(_RID, _TASK, session_key))
            await _await_armed(session_key)
            allow["ok"] = False
            return await task

        assert asyncio.run(_go()) is None


class TestTheTopicRouteReadsTheRosterToo:
    """A Topic press passes the roster as well, so a Topic prompt must re-read it.

    A Topic route names no single peer, so the roster cannot be asked about one.
    What it answers is whether it admits anybody: while it is empty no press in the
    Topic can be honored, reject included, and the elapsed wait is therefore not a
    decision the operator declined to make.
    """

    _FORUM_CHAT = -1001234567890
    _FORUM_THREAD = 5

    def _forum_dispatcher(self):
        d, cli, sess = _dispatcher({7}, allow_forum=True, allowed_forum_chat_ids=[self._FORUM_CHAT])
        session_key = d._session_key(("forum", f"{self._FORUM_CHAT}:{self._FORUM_THREAD}"))
        seam.register_channel_delivery("telegram", d.deliver_spawn_approval)
        return d, cli, sess, session_key

    def test_a_roster_withdrawal_mid_wait_answers_none_not_false_in_a_topic(self, _ceiling) -> None:
        # The Topic itself stays allow-listed and the ceiling stays open; only the
        # roster empties. Every press in the Topic is dropped by the roster gate
        # `on_callback` applies first, so nothing could have resolved this prompt.
        d, cli, _sess, session_key = self._forum_dispatcher()

        async def _go() -> bool | None:
            task = asyncio.ensure_future(seam.deliver_spawn_approval(_RID, _TASK, session_key))
            await _await_armed(session_key)
            d._allowed = set()  # the operator drops the peers, as reconfigure does
            return await task

        result = asyncio.run(_go())

        assert result is None
        assert result is not False
        assert len(cli.sent) == 1  # it WAS surfaced into the Topic
        assert cli.send_chats == [self._FORUM_CHAT]
        assert cli.send_threads == [self._FORUM_THREAD]
        assert _ceiling.state["permitted"] is True

    def test_a_topic_withdrawal_mid_wait_also_answers_none(self, _ceiling) -> None:
        # The other half of the same authority: the roster keeps its peer and the
        # Topic leaves the allow-list, which the shared forum predicate reads.
        d, _cli, _sess, session_key = self._forum_dispatcher()

        async def _go() -> bool | None:
            task = asyncio.ensure_future(seam.deliver_spawn_approval(_RID, _TASK, session_key))
            await _await_armed(session_key)
            d._live_cfg().telegram.allowed_forum_chat_ids = []
            return await task

        assert asyncio.run(_go()) is None

    def test_an_authorized_topic_on_a_permitted_channel_still_denies(self, _ceiling) -> None:
        # The Topic branch must not over-trigger either: nothing was withdrawn, so
        # an unpressed wait stays the operator declining to answer.
        _d, cli, _sess, session_key = self._forum_dispatcher()

        assert asyncio.run(seam.deliver_spawn_approval(_RID, _TASK, session_key)) is False
        assert len(cli.sent) == 1

    def test_a_reject_press_in_a_topic_still_reports_its_decision(self, _ceiling) -> None:
        # An explicit reject is the operator's own decision, so a withdrawal that
        # lands after it stays False rather than becoming a fall-through.
        d, _cli, _sess, session_key = self._forum_dispatcher()

        async def _go() -> bool | None:
            task = asyncio.ensure_future(seam.deliver_spawn_approval(_RID, _TASK, session_key))
            nonce = await _await_armed(session_key)
            await d.on_callback(
                SimpleNamespace(
                    callback_query_id="qF",
                    user_id=7,
                    chat_id=self._FORUM_CHAT,
                    message_id=60,
                    data=f"a:{_RID}:{nonce}:0",
                    label="",
                    chat_type="supergroup",
                    message_thread_id=self._FORUM_THREAD,
                )
            )
            d._allowed = set()
            return await task

        assert asyncio.run(_go()) is False

    def test_an_empty_roster_declines_the_topic_prompt_before_posting(self, _ceiling) -> None:
        # The same reading gates the pre-post side, so a roster already empty when
        # the hook runs surfaces nothing at all.
        d, cli, _sess, session_key = self._forum_dispatcher()
        d._allowed = set()

        assert asyncio.run(seam.deliver_spawn_approval(_RID, _TASK, session_key)) is None
        assert cli.sent == []


class TestADeliveryFaultStillFallsThrough:
    """Fallback: a hook that cannot surface the prompt answers None, not False."""

    def test_a_raising_hook_is_contained_as_none(self, _ceiling) -> None:
        async def _hook(_rid: str, _desc: str, _parent: str) -> bool:
            raise RuntimeError("the channel broke")

        seam.register_channel_delivery("telegram", _hook)

        assert asyncio.run(seam.deliver_spawn_approval(_RID, _TASK, "telegram:k:direct:7")) is None

    def test_a_key_this_channel_cannot_address_answers_none(self, _ceiling) -> None:
        # A unified DM bucket names no single conversation to post into, so the
        # hook declines it before any wait exists to elapse.
        d, cli, _sess = _dispatcher({7})
        seam.register_channel_delivery("telegram", d.deliver_spawn_approval)

        assert asyncio.run(seam.deliver_spawn_approval(_RID, _TASK, "telegram:kirocrew")) is None
        assert cli.sent == []
