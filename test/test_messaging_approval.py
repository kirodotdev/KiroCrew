"""kiro_crew.messaging.approval -- both channel-neutral approval seams.

Every test here is a security property, not a convenience. The module hosts two
styles behind one INTERACTIVE decider, and this file exercises both:

* the TYPED-REPLY seam (a widget-less channel running tools at all), whose
  failure modes are all "silently approved something nobody agreed to"; and
* the WIDGET AWAITER (``PendingApprovals``), where an unanswered prompt must
  DENY and one conversation must never resolve another's prompt -- ACP request
  ids restart at 1 per session, so that is a live collision risk, not a
  theoretical one.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew.messaging import approval
from kiro_crew.messaging.approval import (
    APPROVE,
    DENY,
    RECEIPT_APPROVED,
    RECEIPT_DENIED,
    RECEIPT_EXPIRED,
    RECEIPT_TRUSTED,
    TRUST,
    PendingApprovals,
    SessionApprovalDecider,
    TextReplyApprovalDecider,
    build_approval_prompt,
    claim_approval,
    deliver_verdict,
    open_approval,
    parse_approval_reply,
    pending_for,
    reset_for_tests,
)

SESSION = "whatsapp:kirocrew:direct:447700900000"


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_for_tests()
    yield
    reset_for_tests()


def event(request_id: str = "req-1", title: str = "bash") -> SimpleNamespace:
    return SimpleNamespace(request_id=request_id, title=title)


class TestParsing:
    def test_ordinals_and_words_are_verdicts(self):
        assert parse_approval_reply("1") == APPROVE
        assert parse_approval_reply("2") == DENY
        assert parse_approval_reply("3") == TRUST
        assert parse_approval_reply("Approve") == APPROVE
        assert parse_approval_reply(" no ") == DENY
        assert parse_approval_reply("trust") == TRUST

    def test_trailing_punctuation_is_tolerated(self):
        assert parse_approval_reply("yes.") == APPROVE
        assert parse_approval_reply("no!") == DENY

    def test_a_reply_that_only_CONTAINS_a_verdict_is_not_one(self):
        """The most important parsing rule. "no, use the other file" is an
        instruction for the model; consuming it as a denial would eat it.
        """
        assert parse_approval_reply("no, use the other file instead") == ""
        assert parse_approval_reply("yes please run it on src/") == ""
        assert parse_approval_reply("1 more thing") == ""

    def test_empty_and_unrelated_text_carry_no_verdict(self):
        assert parse_approval_reply("") == ""
        assert parse_approval_reply("   ") == ""
        assert parse_approval_reply("what does that tool do?") == ""


@pytest.mark.asyncio
class TestRegistry:
    async def test_open_is_idempotent_for_one_request(self):
        a = open_approval(SESSION, "req-1")
        b = open_approval(SESSION, "req-1")
        assert a is b

    async def test_claim_returns_none_when_no_renderer_opened_one(self):
        assert claim_approval(SESSION, "req-1") is None

    async def test_a_delivered_verdict_resolves_and_clears(self):
        entry = open_approval(SESSION, "req-1")
        assert deliver_verdict(SESSION, APPROVE) == RECEIPT_APPROVED
        assert await entry.future == APPROVE
        assert pending_for(SESSION) is None

    async def test_an_answer_with_nothing_open_reports_expired(self):
        """It must not be silent: the operator typed "yes" believing they were
        approving something, and they were not.
        """
        assert deliver_verdict(SESSION, APPROVE) == RECEIPT_EXPIRED

    async def test_deny_and_trust_receipts(self):
        open_approval(SESSION, "r1")
        assert deliver_verdict(SESSION, DENY) == RECEIPT_DENIED
        open_approval(SESSION, "r2")
        assert deliver_verdict(SESSION, TRUST) == RECEIPT_TRUSTED

    async def test_sessions_do_not_see_each_others_requests(self):
        open_approval("whatsapp:a", "req-1")
        assert pending_for("whatsapp:b") is None
        assert deliver_verdict("whatsapp:b", APPROVE) == RECEIPT_EXPIRED

    async def test_the_oldest_request_is_answered_first(self):
        first = open_approval(SESSION, "req-1")
        second = open_approval(SESSION, "req-2")
        deliver_verdict(SESSION, APPROVE)
        assert first.future.done() and not second.future.done()


@pytest.mark.asyncio
class TestDeciderSafety:
    async def test_no_prompt_means_immediate_deny_not_a_stall(self):
        """A muted conversation gets SilentRenderer, which drops the prompt. The
        decider must deny AT ONCE -- waiting the full window for an answer to an
        unasked question would turn a mute into a multi-minute stall per tool.

        Asserted as "the decider created no entry", not as elapsed time: the
        decider returns DENY on a closed window too, so a timing assertion
        passes against the very bug this pins. Whether an entry exists
        afterwards is the actual invariant -- only a renderer may open one.
        """
        decider = TextReplyApprovalDecider(SESSION, timeout_s=30.0)
        task = asyncio.create_task(decider(event("req-1")))
        await asyncio.sleep(0)
        # Observed WHILE the call is in flight. Checking afterwards proves
        # nothing: wait()'s finally clause forgets the entry either way, so the
        # registry looks identical whether the decider parked on an entry it
        # minted or never made one.
        minted = claim_approval(SESSION, "req-1")
        approved = await asyncio.wait_for(task, timeout=1.0)
        assert approved is False
        assert minted is None, (
            "the decider minted its own pending entry; with SilentRenderer that "
            "makes every muted tool call wait the full approval window"
        )

    async def test_silence_denies_when_the_window_closes(self):
        open_approval(SESSION, "req-1")
        decider = TextReplyApprovalDecider(SESSION, timeout_s=0.05)
        assert await decider(event()) is False

    async def test_a_typed_approval_is_honoured(self):
        open_approval(SESSION, "req-1")
        decider = TextReplyApprovalDecider(SESSION, timeout_s=5.0)
        task = asyncio.create_task(decider(event()))
        await asyncio.sleep(0)
        deliver_verdict(SESSION, APPROVE)
        assert await task is True

    async def test_a_typed_denial_is_honoured(self):
        open_approval(SESSION, "req-1")
        decider = TextReplyApprovalDecider(SESSION, timeout_s=5.0)
        task = asyncio.create_task(decider(event()))
        await asyncio.sleep(0)
        deliver_verdict(SESSION, DENY)
        assert await task is False

    async def test_a_late_answer_cannot_approve_the_NEXT_request(self):
        """The anti-stale rule. A "yes" typed after request one timed out must
        not become consent for a different tool the agent asks about later.
        """
        open_approval(SESSION, "req-1")
        first = TextReplyApprovalDecider(SESSION, timeout_s=0.05)
        assert await first(event("req-1")) is False  # timed out -> denied
        # The operator's late "yes" arrives; nothing is open, so it is expired.
        assert deliver_verdict(SESSION, APPROVE) == RECEIPT_EXPIRED
        # A new request now gets no prompt from that stale answer.
        second = TextReplyApprovalDecider(SESSION, timeout_s=0.05)
        open_approval(SESSION, "req-2")
        assert await second(event("req-2")) is False

    async def test_cancellation_denies(self):
        """A turn torn down mid-decision must not leave a tool approved."""
        open_approval(SESSION, "req-1")
        decider = TextReplyApprovalDecider(SESSION, timeout_s=30.0)
        task = asyncio.create_task(decider(event()))
        await asyncio.sleep(0)
        entry = claim_approval(SESSION, "req-1")
        assert entry is not None
        entry.future.cancel()
        assert await task is False


class _Sessions:
    """The two SessionManager methods the decider uses."""

    def __init__(self, policy: str = "") -> None:
        self.policy = policy
        self.writes: list[tuple[str, str]] = []

    def get_approval_policy(self, key: str) -> str:
        return self.policy

    def set_approval_policy(self, key: str, policy: str) -> None:
        self.writes.append((key, policy))
        self.policy = policy


@pytest.mark.asyncio
class TestSessionTrust:
    async def test_trust_is_recorded_as_the_session_approval_policy(self):
        """Not a module-level set. The session's own policy is durable, already
        SEL-audited on write, and is what a spawned subagent inherits.
        """
        sessions = _Sessions()
        open_approval(SESSION, "req-1")
        decider = TextReplyApprovalDecider(SESSION, sessions=sessions, timeout_s=5.0)
        task = asyncio.create_task(decider(event()))
        await asyncio.sleep(0)
        deliver_verdict(SESSION, TRUST)
        assert await task is True
        assert sessions.writes == [(SESSION, "auto")]

    async def test_trusted_reads_the_policy_back(self):
        decider = TextReplyApprovalDecider(SESSION, sessions=_Sessions(policy="auto"))
        assert decider.trusted() is True

    async def test_untrusted_and_missing_sessions_are_not_trusted(self):
        assert TextReplyApprovalDecider(SESSION, sessions=_Sessions()).trusted() is False
        assert TextReplyApprovalDecider(SESSION, sessions=None).trusted() is False

    async def test_a_failing_policy_read_does_not_grant_trust(self):
        class Boom:
            def get_approval_policy(self, key: str) -> str:
                raise RuntimeError("store down")

        assert TextReplyApprovalDecider(SESSION, sessions=Boom()).trusted() is False


class TestPrompt:
    def test_the_prompt_names_the_tool_and_numbers_the_choices(self):
        text = build_approval_prompt("bash", "list the repo")
        assert "bash" in text and "list the repo" in text
        for ordinal in ("1.", "2.", "3."):
            assert ordinal in text

    def test_an_agent_authored_credential_never_reaches_the_prompt(self):
        """Both fields are written by the model, so either can carry a secret.

        Screened in `build_approval_prompt` rather than at each channel's sink,
        because that is why the helper is shared: a channel adopting the ladder
        inherits the guarantee, and a caller that forgets it leaks on a security
        prompt of all places.
        """
        key = "AKIA" + "IOSFODNN7EXAMPLE"
        text = build_approval_prompt(f"bash {key}", f"upload with {key}")
        assert key not in text, "an agent-authored credential reached the chat"
        assert "bash" in text, "screening must not eat the tool name"

    def test_an_unknown_tool_still_produces_a_usable_prompt(self):
        text = build_approval_prompt("", "")
        assert "unknown" in text
        assert "1." in text


def _event(request_id: str | int = 1) -> SimpleNamespace:
    return SimpleNamespace(request_id=request_id, title="fs_write", tool_kind="write")


class TestResolution:
    @pytest.mark.asyncio
    async def test_an_approve_resolves_the_waiting_prompt(self) -> None:
        pending = PendingApprovals("webex")
        task = asyncio.create_task(pending.decide("s1", _event()))
        await _until(lambda: pending.has_pending("s1"))

        assert pending.resolve("s1", True) is True
        assert await task is True

    @pytest.mark.asyncio
    async def test_a_deny_resolves_the_waiting_prompt(self) -> None:
        pending = PendingApprovals("webex")
        task = asyncio.create_task(pending.decide("s1", _event()))
        await _until(lambda: pending.has_pending("s1"))

        assert pending.resolve("s1", False) is True
        assert await task is False

    @pytest.mark.asyncio
    async def test_resolve_reports_false_when_nothing_is_waiting(self) -> None:
        # The caller needs to tell "your answer was applied" from "that prompt
        # already expired", or it reports a decision that never reached the
        # provider.
        pending = PendingApprovals("webex")
        assert pending.resolve("s1", True) is False

    @pytest.mark.asyncio
    async def test_the_entry_is_retired_once_decided(self) -> None:
        # A late answer must not be able to resolve a LATER prompt that happens
        # to reuse this request id.
        pending = PendingApprovals("webex")
        task = asyncio.create_task(pending.decide("s1", _event()))
        await _until(lambda: pending.has_pending("s1"))
        pending.resolve("s1", True)
        await task

        assert pending.has_pending("s1") is False
        assert pending.resolve("s1", True) is False


class TestIsolation:
    @pytest.mark.asyncio
    async def test_two_sessions_sharing_a_request_id_do_not_cross_resolve(self) -> None:
        """The reason the key is namespaced by session.

        kiro-cli numbers permission requests from 1 within each session, so two
        concurrent conversations routinely hold a pending request_id=1. Without
        the namespace, one user's "approve" would approve the other's tool.
        """
        pending = PendingApprovals("webex")
        a = asyncio.create_task(pending.decide("session-a", _event(1)))
        b = asyncio.create_task(pending.decide("session-b", _event(1)))
        await _until(lambda: pending.has_pending("session-a") and pending.has_pending("session-b"))

        assert pending.resolve("session-a", True) is True
        assert await a is True
        assert pending.has_pending("session-b") is True  # untouched
        assert b.done() is False

        pending.resolve("session-b", False)
        assert await b is False

    @pytest.mark.asyncio
    async def test_has_pending_is_scoped_to_the_session(self) -> None:
        pending = PendingApprovals("webex")
        task = asyncio.create_task(pending.decide("session-a", _event()))
        await _until(lambda: pending.has_pending("session-a"))

        assert pending.has_pending("session-b") is False
        pending.resolve("session-a", True)
        await task

    @pytest.mark.asyncio
    async def test_a_session_key_that_is_a_prefix_of_another_is_not_matched(self) -> None:
        # Keys are compared with the ":" separator attached, so "webex:a" must
        # not resolve a prompt pending for "webex:ab".
        pending = PendingApprovals("webex")
        task = asyncio.create_task(pending.decide("webex:ab", _event()))
        await _until(lambda: pending.has_pending("webex:ab"))

        assert pending.has_pending("webex:a") is False
        assert pending.resolve("webex:a", True) is False

        pending.resolve("webex:ab", True)
        await task


class TestDenyByDefault:
    @pytest.mark.asyncio
    async def test_an_unanswered_prompt_denies_and_stops_waiting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deny-on-timeout, driven by a patched window rather than a real sleep.

        Waiting out the production timeout would make this test take minutes;
        asserting on the RESULT of an elapsed window is the property that
        matters, so shrink the window instead of sleeping.
        """
        monkeypatch.setattr("kiro_crew.messaging.approval.APPROVAL_TIMEOUT_S", 0.01)
        pending = PendingApprovals("webex")

        assert await pending.decide("s1", _event()) is False
        # And the entry is gone, so a reply arriving after the window is told the
        # prompt expired rather than silently doing nothing.
        assert pending.resolve("s1", True) is False

    @pytest.mark.asyncio
    async def test_an_event_with_no_request_id_still_resolves(self) -> None:
        # A backend that omits request_id must not make the prompt unanswerable:
        # the key degrades to the session plus an empty id, which is still unique
        # per session.
        pending = PendingApprovals("webex")
        task = asyncio.create_task(pending.decide("s1", SimpleNamespace()))
        await _until(lambda: pending.has_pending("s1"))

        assert pending.resolve("s1", True) is True
        assert await task is True


class TestDecider:
    @pytest.mark.asyncio
    async def test_the_decider_binds_one_session(self) -> None:
        pending = PendingApprovals("webex")
        decider = SessionApprovalDecider(pending, session_key="s1")
        task = asyncio.create_task(decider(_event()))
        await _until(lambda: pending.has_pending("s1"))

        assert pending.resolve("s1", True) is True
        assert await task is True

    def test_key_is_session_scoped(self) -> None:
        assert PendingApprovals.key("webex:a", 7) == "webex:a:7"


async def _until(predicate, timeout: float = 1.0) -> None:
    """Poll *predicate* until true. Polling, not sleeping.

    A fixed sleep long enough to be reliable on a loaded CI box is also long
    enough to dominate the suite, and a short one is a flake.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


class TestExactIdResolution:
    """The affordance-agnostic half: a widget channel resolves by id.

    A typed reply names no request id — the user answers "the question on
    screen" — so the default is oldest-first. A button press DOES carry the
    correlation id, and that is the shape a later card or button channel needs to
    migrate onto this registry rather than growing a fourth copy.
    """

    @pytest.mark.asyncio
    async def test_an_exact_request_id_resolves_only_that_prompt(self) -> None:
        pending = PendingApprovals("webex")
        first = asyncio.create_task(pending.decide("s1", _event(1)))
        await _until(lambda: pending.has_pending("s1"))
        second = asyncio.create_task(pending.decide("s1", _event(2)))
        await _until(lambda: len(_pending_keys(pending)) == 2)

        assert pending.resolve("s1", True, request_id=2) is True
        assert await second is True
        assert first.done() is False

        pending.resolve("s1", False, request_id=1)
        assert await first is False

    @pytest.mark.asyncio
    async def test_an_unknown_request_id_resolves_nothing(self) -> None:
        pending = PendingApprovals("webex")
        task = asyncio.create_task(pending.decide("s1", _event(1)))
        await _until(lambda: pending.has_pending("s1"))

        assert pending.resolve("s1", True, request_id=99) is False
        assert task.done() is False

        pending.resolve("s1", True)
        await task

    @pytest.mark.asyncio
    async def test_without_an_id_the_oldest_prompt_resolves_first(self) -> None:
        pending = PendingApprovals("webex")
        first = asyncio.create_task(pending.decide("s1", _event(1)))
        await _until(lambda: pending.has_pending("s1"))
        second = asyncio.create_task(pending.decide("s1", _event(2)))
        await _until(lambda: len(_pending_keys(pending)) == 2)

        assert pending.resolve("s1", True) is True
        assert await first is True
        assert second.done() is False

        pending.resolve("s1", False)
        assert await second is False


def _pending_keys(pending: PendingApprovals) -> list[str]:
    """The registry's live keys. Reaches into private state on purpose: the
    alternative is a public accessor that exists only for tests."""
    return list(pending._pending)


class TestWidgetNonce:
    """A widget's nonce is validated INSIDE resolve, as a precondition.

    A channel that resolved first and validated after would have already approved
    the tool by the time it decided the press was stale — the only thing left to
    suppress is the confirmation message. This matters on a platform that cannot
    retire a resolved widget (Webex refuses to edit a message carrying an
    attachment), where the buttons stay clickable forever.
    """

    @pytest.mark.asyncio
    async def test_the_minted_nonce_resolves_the_prompt_it_was_minted_for(self) -> None:
        pending = PendingApprovals("webex")
        task = asyncio.create_task(pending.decide("s1", _event(1)))
        await _until(lambda: pending.has_pending("s1"))
        nonce = pending.reserve("s1", 1)

        assert pending.resolve("s1", True, request_id=1, expected_nonce=nonce) is True
        assert await task is True

    @pytest.mark.asyncio
    async def test_a_wrong_nonce_leaves_the_prompt_pending(self) -> None:
        pending = PendingApprovals("webex")
        task = asyncio.create_task(pending.decide("s1", _event(1)))
        await _until(lambda: pending.has_pending("s1"))
        pending.reserve("s1", 1)

        assert pending.resolve("s1", True, request_id=1, expected_nonce="wrong") is False
        assert task.done() is False

        pending.resolve("s1", False, request_id=1)
        assert await task is False

    @pytest.mark.asyncio
    async def test_an_unminted_prompt_refuses_every_nonce(self) -> None:
        # Fail closed: a prompt with no widget has no press to honour.
        pending = PendingApprovals("webex")
        task = asyncio.create_task(pending.decide("s1", _event(1)))
        await _until(lambda: pending.has_pending("s1"))

        assert pending.resolve("s1", True, request_id=1, expected_nonce="anything") is False
        pending.resolve("s1", False, request_id=1)
        assert await task is False

    @pytest.mark.asyncio
    async def test_a_nonce_dies_with_the_decision_it_guards(self) -> None:
        """The reason it is minted against the pending ENTRY.

        A renderer-owned nonce outlives the turn; this one is retired by the same
        ``finally`` that retires the future, so a press on a spent widget cannot
        answer a LATER prompt that reused the request id.
        """
        pending = PendingApprovals("webex")
        first = asyncio.create_task(pending.decide("s1", _event(1)))
        await _until(lambda: pending.has_pending("s1"))
        stale = pending.reserve("s1", 1)
        pending.resolve("s1", True, request_id=1, expected_nonce=stale)
        assert await first is True

        second = asyncio.create_task(pending.decide("s1", _event(1)))
        await _until(lambda: pending.has_pending("s1"))

        assert pending.resolve("s1", True, request_id=1, expected_nonce=stale) is False
        pending.resolve("s1", False, request_id=1)
        assert await second is False

    @pytest.mark.asyncio
    async def test_a_typed_answer_passes_no_nonce_and_is_unaffected(self) -> None:
        # There is no widget to have gone stale, and the sender was authorized
        # upstream — so the guard must not apply to the typed path.
        pending = PendingApprovals("webex")
        task = asyncio.create_task(pending.decide("s1", _event(1)))
        await _until(lambda: pending.has_pending("s1"))
        pending.reserve("s1", 1)

        assert pending.resolve("s1", True) is True
        assert await task is True


class TestApprovalStallSignalling:
    @pytest.mark.asyncio
    async def test_a_timeout_signals_autonudge(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unanswered prompt is the only evidence an unattended loop can no
        longer act.

        Without the signal a monitor loop bound to this conversation keeps firing,
        is denied every interactive tool, and burns its whole cycle budget while
        reporting itself healthy — its per-turn cap is measured in tens of minutes
        and the approval window in minutes.
        """
        from kiro_crew import autonudge

        stalled: list[str] = []
        monkeypatch.setattr(approval, "APPROVAL_TIMEOUT_S", 0.01)
        monkeypatch.setattr(
            autonudge,
            "get_instance",
            lambda: SimpleNamespace(notify_approval_stalled=stalled.append),
        )

        pending = PendingApprovals("webex")
        assert await pending.decide("webex:agent:direct:a@b.com", _event(1)) is False
        assert stalled == ["webex:agent:direct:a@b.com"]

    @pytest.mark.asyncio
    async def test_a_key_no_loop_can_bind_to_is_not_signalled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import autonudge

        stalled: list[str] = []
        monkeypatch.setattr(approval, "APPROVAL_TIMEOUT_S", 0.01)
        monkeypatch.setattr(
            autonudge,
            "get_instance",
            lambda: SimpleNamespace(notify_approval_stalled=stalled.append),
        )

        pending = PendingApprovals("webex")
        assert await pending.decide("cron:nightly", _event(1)) is False
        assert stalled == []

    @pytest.mark.asyncio
    async def test_a_signalling_failure_never_changes_the_denial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A monitoring convenience must not be able to turn a denied tool into a
        # raised exception inside the turn.
        from kiro_crew import autonudge

        def _boom() -> object:
            raise RuntimeError("autonudge is unavailable")

        monkeypatch.setattr(approval, "APPROVAL_TIMEOUT_S", 0.01)
        monkeypatch.setattr(autonudge, "get_instance", _boom)

        pending = PendingApprovals("webex")
        assert await pending.decide("webex:agent:direct:a@b.com", _event(1)) is False


class TestReservationRace:
    """The decision window opens BEFORE the prompt is sent.

    ``TurnDriver`` dispatches ``PROMPT_CHOICE`` and only then awaits the decider,
    so the prompt is visible in the room for a whole REST round trip before
    ``decide`` would have registered anything. An answer arriving in that window
    used to find nothing pending, fall through to the mid-turn path, and be
    discarded — the user watched their decision do nothing and the tool denied
    itself minutes later.
    """

    @pytest.mark.asyncio
    async def test_an_answer_before_decide_still_resolves(self) -> None:
        pending = PendingApprovals("webex")
        nonce = pending.reserve("s1", 1)

        # The reply lands while the prompt is still being delivered.
        assert pending.has_pending("s1")
        assert pending.resolve("s1", True, request_id=1, expected_nonce=nonce) is True

        # The driver reaches the decider afterwards and reads the decision.
        assert await pending.decide("s1", _event(1)) is True

    @pytest.mark.asyncio
    async def test_reserving_does_not_orphan_a_live_future(self) -> None:
        """Never replace a future someone is already awaiting.

        A second reservation — or one that follows ``decide`` — must keep the
        existing future, or a resolved answer sets an object nobody reads and the
        prompt hangs for its whole window before denying.
        """
        pending = PendingApprovals("webex")
        task = asyncio.create_task(pending.decide("s1", _event(1)))
        await _until(lambda: pending.has_pending("s1"))

        nonce = pending.reserve("s1", 1)
        again = pending.reserve("s1", 1)

        assert nonce == again
        assert pending.resolve("s1", True, request_id=1, expected_nonce=nonce) is True
        assert await task is True

    @pytest.mark.asyncio
    async def test_an_unawaited_reservation_is_discarded_with_the_turn(self) -> None:
        """The prompt rendered and the turn then failed before the decider.

        Left behind, that reservation outlives its turn and a stray answer to a
        LATER prompt could resolve it.
        """
        pending = PendingApprovals("webex")
        pending.reserve("s1", 1)
        assert pending.has_pending("s1")

        pending.discard_reservations("s1")

        assert not pending.has_pending("s1")
        assert pending.resolve("s1", True, request_id=1) is False

    @pytest.mark.asyncio
    async def test_discarding_leaves_another_sessions_reservation_alone(self) -> None:
        pending = PendingApprovals("webex")
        pending.reserve("s1", 1)
        pending.reserve("s2", 1)

        pending.discard_reservations("s1")

        assert not pending.has_pending("s1")
        assert pending.has_pending("s2")

    @pytest.mark.asyncio
    async def test_discarding_does_not_disturb_an_awaited_prompt(self) -> None:
        # ``decide`` retires its own entry, so a teardown running while a decision
        # is genuinely in flight must not cancel it.
        pending = PendingApprovals("webex")
        task = asyncio.create_task(pending.decide("s1", _event(1)))
        await _until(lambda: pending.has_pending("s1"))

        pending.discard_reservations("s1")

        assert pending.resolve("s1", True) is False
        assert task.done() is False
        task.cancel()
