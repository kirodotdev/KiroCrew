"""Text-reply tool approval: the shared seam, in isolation from any channel.

Every test here is a security property, not a convenience. The module exists so
a widget-less channel can run tools at all, and the failure modes it has to
avoid are all "silently approved something nobody agreed to".
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew.messaging.approval import (
    APPROVE,
    DENY,
    RECEIPT_APPROVED,
    RECEIPT_DENIED,
    RECEIPT_EXPIRED,
    RECEIPT_TRUSTED,
    TRUST,
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
