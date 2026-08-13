"""Background turns must account for what they spend, and only for that turn.

Both background channels reach the provider through a single chokepoint, and the
turn's billing lives on an object the session replaces per turn and the release
path tears down. These tests pin the contract that made the spend visible without
making it wrong: a row is written for a turn that ran, no row for one that did
not, the billing is read before the semaphore changes hands, and teardown happens
even under cancellation.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from kiro_crew.llm_helpers import background_turn, provider_last_turn_usage

_USAGE_TARGET = "kiro_crew.dashboard.handlers.usage.persist_token_record_async"


class _Stats:
    def __init__(self, credits: float) -> None:
        self.credits = credits


class _Client:
    """Stands in for the turn-runner the background session hands back.

    Starts holding a PREVIOUS turn's stats, because the shared session is
    long-lived: whatever the last turn left behind is what a new turn's teardown
    would read if nothing distinguished the two.
    """

    def __init__(self, prior_credits: float = 0.0) -> None:
        self.last_prompt_stats = _Stats(prior_credits)

    def begin_turn(self, credits: float) -> None:
        """Install fresh stats, as the runner does when a turn actually starts."""
        self.last_prompt_stats = _Stats(credits)


class _Sessions:
    def __init__(self, client: _Client) -> None:
        self._bg_client = client
        self.acquire_calls: list[tuple[str, dict]] = []
        self.order: list[str] = []

    async def get_or_create(self, key: str, **kw: object):
        self.acquire_calls.append((key, dict(kw)))
        return self._bg_client, False, False

    def release(self, key: str) -> None:
        self.order.append("release")

    async def recycle_background(self) -> None:
        self.order.append("recycle")


class TestBackgroundTurnAccounting(unittest.IsolatedAsyncioTestCase):
    async def test_billed_turn_writes_a_row_tagged_with_its_task(self):
        sessions = _Sessions(_Client())
        with patch(_USAGE_TARGET) as persist:
            async with background_turn(sessions, task="consolidation") as client:
                client.begin_turn(3.5)

        self.assertEqual(persist.await_count, 1)
        self.assertEqual(persist.await_args.kwargs["surface"], "bg:consolidation")
        self.assertEqual(persist.await_args.args[2].credits, 3.5)

    async def test_the_row_names_the_backend_that_served_the_turn(self):
        """Left unset the row lands with provider="" and drops out of the usage
        page's provider and provider-model breakdowns, so the spend is recorded
        but unattributable to a backend."""
        sessions = _Sessions(_Client())
        with patch(_USAGE_TARGET) as persist:
            async with background_turn(sessions, task="consolidation") as client:
                client.begin_turn(3.5)

        # Positional, matching persist_token_record_async's signature
        # (slot_key, model, event, provider).
        self.assertEqual(persist.await_args.args[3], "acp")

    async def test_a_non_default_backend_is_named_through_the_wrapper_chain(self):
        """The resolver only recognises a provider handed to it directly, and the
        shared background session wraps one behind ``_sess.provider``."""
        from kiro_crew.acp.types import PROVIDER_LABEL_CLAUDE
        from kiro_crew.llm_helpers import _provider_label

        inner = SimpleNamespace()
        adapter = SimpleNamespace(_sess=SimpleNamespace(provider=inner))
        with patch(
            "kiro_crew.providers.acp.provider_label",
            side_effect=lambda node: (PROVIDER_LABEL_CLAUDE if node is inner else "acp"),
        ):
            self.assertEqual(_provider_label(adapter), PROVIDER_LABEL_CLAUDE)

    async def test_a_turn_that_never_started_writes_no_row(self):
        """A dispatch that fails before the runner installs fresh stats leaves the
        PREVIOUS turn's credits in place. Those were already recorded, so billing
        them again would double-count real spend."""
        sessions = _Sessions(_Client(prior_credits=9.0))
        with patch(_USAGE_TARGET) as persist:
            with self.assertRaises(RuntimeError):
                async with background_turn(sessions, task="consolidation"):
                    raise RuntimeError("session busy; prompt never dispatched")

        persist.assert_not_awaited()

    async def test_unbilled_turn_writes_no_row(self):
        sessions = _Sessions(_Client())
        with patch(_USAGE_TARGET) as persist:
            async with background_turn(sessions, task="skill_dedupe") as client:
                client.begin_turn(0.0)

        persist.assert_not_awaited()

    async def test_release_precedes_accounting_which_precedes_recycle(self):
        """Two invariants in one order: release is synchronous so it must land
        first, and accounting must still precede recycling, which can replace
        the provider the billing lives on."""
        sessions = _Sessions(_Client())

        async def _mark(*a: object, **kw: object) -> None:
            sessions.order.append("account")

        with patch(_USAGE_TARGET, side_effect=_mark):
            async with background_turn(sessions, task="chat_title") as client:
                client.begin_turn(1.0)

        self.assertEqual(sessions.order, ["release", "account", "recycle"])

    async def test_cancellation_while_accounting_still_released_the_session(self):
        """CancelledError is a BaseException, so an ``except Exception`` around the
        accounting await never sees it. If teardown depended on that handler a
        cancelled turn would hold the shared semaphore forever and deadlock every
        later background caller."""
        sessions = _Sessions(_Client())
        with patch(_USAGE_TARGET, side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                async with background_turn(sessions, task="consolidation") as client:
                    client.begin_turn(1.0)

        self.assertIn("release", sessions.order)

    async def test_billing_is_read_before_the_semaphore_is_released(self):
        """The next waiter acquires the moment release returns and installs its own
        stats, so reading after release records the waiter's spend under this
        task's tag."""
        client = _Client()
        sessions = _Sessions(client)
        released = sessions.release

        def _release_and_let_the_next_turn_start(key: str) -> None:
            released(key)
            client.begin_turn(0.0)

        sessions.release = _release_and_let_the_next_turn_start  # type: ignore[method-assign]

        with patch(_USAGE_TARGET) as persist:
            async with background_turn(sessions, task="consolidation") as c:
                c.begin_turn(3.0)

        self.assertEqual(persist.await_count, 1)
        self.assertEqual(persist.await_args.args[2].credits, 3.0)

    async def test_body_failure_after_a_real_turn_still_accounts_and_releases(self):
        sessions = _Sessions(_Client())
        with patch(_USAGE_TARGET) as persist:
            with self.assertRaises(RuntimeError):
                async with background_turn(sessions, task="thread_compress") as client:
                    client.begin_turn(2.0)
                    raise RuntimeError("failed after the turn was billed")

        self.assertEqual(persist.await_count, 1)
        self.assertEqual(sessions.order, ["release", "recycle"])

    async def test_the_turn_duration_reaches_the_row(self):
        """The acp provider never fills TurnUsage.duration_ms, so this local
        measurement is the only duration a background row can carry. The clock is
        substituted rather than timed, so the assertion pins the arithmetic
        instead of the host's speed."""
        sessions = _Sessions(_Client())
        clock = iter([100.0, 100.25])
        with patch("kiro_crew.llm_helpers.time", SimpleNamespace(monotonic=lambda: next(clock))):
            with patch(_USAGE_TARGET) as persist:
                async with background_turn(sessions, task="consolidation") as client:
                    client.begin_turn(1.0)

        self.assertEqual(persist.await_args.kwargs["elapsed_ms"], 250)

    async def test_agent_is_forwarded_only_when_the_caller_supplies_one(self):
        """The key picks the session; the agent decides what it is created AS,
        so a default here would silently change that for callers passing none."""
        sessions = _Sessions(_Client())
        with patch(_USAGE_TARGET):
            async with background_turn(sessions, task="plan_rephrase"):
                pass
            async with background_turn(sessions, task="consolidation", agent="kirocrew-lite"):
                pass

        self.assertEqual(sessions.acquire_calls[0][1], {})
        self.assertEqual(sessions.acquire_calls[1][1], {"agent": "kirocrew-lite"})


class TestBillingStatsReachThroughTheAdapter(unittest.TestCase):
    def test_credits_are_found_behind_the_background_adapter(self):
        """The non-kiro seam links to the runner through ``_sess.provider``; a
        walk that only knows ``_client``/``_handle`` reports 0 for a billed turn.
        """

        class _Runner:
            last_prompt_stats = _Stats(4.25)

        class _Session:
            provider = _Runner()

        class _Adapter:
            _sess = _Session()

        self.assertEqual(provider_last_turn_usage(_Adapter()).credits, 4.25)

    def test_absent_stats_yield_zero_rather_than_raising(self):
        self.assertEqual(provider_last_turn_usage(object()).credits, 0.0)

    def test_an_unreplaced_stats_object_reports_nothing(self):
        class _Runner:
            def __init__(self) -> None:
                self.last_prompt_stats = _Stats(7.0)

        runner = _Runner()
        prior = runner.last_prompt_stats
        self.assertEqual(provider_last_turn_usage(runner, since=prior).credits, 0.0)
        runner.last_prompt_stats = _Stats(7.0)
        self.assertEqual(provider_last_turn_usage(runner, since=prior).credits, 7.0)


class TestBilledAttemptsSurviveARetry(unittest.IsolatedAsyncioTestCase):
    """A turn can span several attempts, and each retry installs fresh stats."""

    async def test_an_attempt_billed_then_abandoned_by_a_retry_is_still_counted(self):
        """The first attempt's metering lands, the stream then breaks before any
        text, and the retry replaces the stats object that carried those credits.
        Reading the live stats afterwards would report only the retry's spend."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.llm_helpers import stream_and_collect
        from kiro_crew.providers.base import EVENT_COMPLETE

        class _Provider:
            def __init__(self) -> None:
                self.last_prompt_stats = _Stats(0.0)
                self.calls = 0

            async def stream(self, message: str):
                self.calls += 1
                # Fresh per-turn stats, as the real runner installs when a turn
                # begins -- which is what makes the earlier attempt's credits
                # unreachable from a post-hoc read.
                self.last_prompt_stats = _Stats(2.0 if self.calls == 1 else 1.5)
                if self.calls == 1:
                    raise AcpError("transient error (http 5xx)")
                yield SimpleNamespace(kind=EVENT_COMPLETE, text="")

        provider = _Provider()
        with patch("kiro_crew.llm_helpers.transient_retry_delay", return_value=0):
            await stream_and_collect(provider, "p")

        self.assertEqual(provider.calls, 2)
        self.assertEqual(provider_last_turn_usage(provider).credits, 3.5)

    async def test_the_accumulated_total_is_consumed_by_the_first_read(self):
        """The sum is handed over once. A second read falls back to the live
        stats -- which for a retried turn is the final attempt alone -- so the
        total cannot be counted into two different rows."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.llm_helpers import stream_and_collect
        from kiro_crew.providers.base import EVENT_COMPLETE

        class _Provider:
            def __init__(self) -> None:
                self.last_prompt_stats = _Stats(0.0)
                self.calls = 0

            async def stream(self, message: str):
                self.calls += 1
                self.last_prompt_stats = _Stats(2.0 if self.calls == 1 else 1.5)
                if self.calls == 1:
                    raise AcpError("transient error (http 5xx)")
                yield SimpleNamespace(kind=EVENT_COMPLETE, text="")

        provider = _Provider()
        with patch("kiro_crew.llm_helpers.transient_retry_delay", return_value=0):
            await stream_and_collect(provider, "p")

        self.assertEqual(provider_last_turn_usage(provider).credits, 3.5)
        self.assertEqual(provider_last_turn_usage(provider).credits, 1.5)

    async def test_a_total_left_unread_is_not_billed_to_a_later_turn(self):
        """The provider outlives the turn: the shared background session is reused
        by every background caller, and a Slack session by every turn in its
        thread. A turn whose total nobody read must not have it consumed by a later
        turn, which would bill that turn for the earlier one's spend and lose its
        own.

        The later turn here drives ``provider.stream`` itself rather than going
        through ``stream_and_collect``, which is the documented shape that
        publishes no total of its own -- and therefore the shape where a stale one
        is still on the provider at read time. A later ``stream_and_collect`` turn
        would overwrite the stale total instead, so it cannot show this.
        """
        from kiro_crew.llm_helpers import stream_and_collect
        from kiro_crew.providers.base import EVENT_COMPLETE

        class _Provider:
            def __init__(self) -> None:
                self.last_prompt_stats = _Stats(0.0)
                self.credits_for_next_turn = 7.0

            async def stream(self, message: str):
                self.last_prompt_stats = _Stats(self.credits_for_next_turn)
                yield SimpleNamespace(kind=EVENT_COMPLETE, text="")

        provider = _Provider()

        # Turn one goes through the helper, so it publishes its total. Nobody reads it.
        await stream_and_collect(provider, "first")

        # Turn two drives the provider directly and publishes nothing.
        provider.credits_for_next_turn = 2.0
        async for _ in provider.stream("second"):
            pass

        # Turn two is billed its own 2.0, not turn one's 7.0.
        self.assertEqual(provider_last_turn_usage(provider).credits, 2.0)

    def test_a_total_whose_stats_were_replaced_is_discarded(self):
        """The guard is on the stats object's identity, not on ordering: a total
        published against stats the provider has since replaced is stale by
        definition, and the live read takes over."""
        from kiro_crew.llm_helpers import _TURN_BILLED_ATTR, TurnUsage

        provider = SimpleNamespace(last_prompt_stats=_Stats(4.0))
        stale_stats = _Stats(9.0)
        setattr(provider, _TURN_BILLED_ATTR, (stale_stats, TurnUsage(credits=9.0)))

        self.assertEqual(provider_last_turn_usage(provider).credits, 4.0)
        # Cleared even though it was rejected, so a third read cannot see it.
        self.assertFalse(hasattr(provider, _TURN_BILLED_ATTR))


if __name__ == "__main__":
    unittest.main()
