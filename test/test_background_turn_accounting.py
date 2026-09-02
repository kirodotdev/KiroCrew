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
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from kiro_crew.llm_helpers import background_turn, provider_last_turn_usage
from kiro_crew.providers.base import LLMProvider

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


class TestBillingIsReadThroughTheDeclaredSeam(unittest.TestCase):
    """A provider's billing is a declared capability, not a guessed attribute."""

    def test_a_runner_the_walk_cannot_name_is_still_billed(self):
        """The failure this seam removes: a provider that links to its turn-runner
        under a name the private walk does not know reported empty usage, which
        the ``usage_has_billing`` gate reads as "nothing to record" -- the spend
        never reaching the usage store, with no error raised."""
        from kiro_crew.llm_helpers import usage_has_billing

        class _Runner:
            def __init__(self) -> None:
                self.last_prompt_stats = _Stats(9.5)

        class _FutureSeamProvider:
            """Same billing as the acp seam, linked under its own name."""

            def __init__(self) -> None:
                self._runner = _Runner()

            def billing_stats(self) -> object | None:
                return self._runner.last_prompt_stats

        usage = provider_last_turn_usage(_FutureSeamProvider())
        self.assertEqual(usage.credits, 9.5)
        self.assertTrue(usage_has_billing(usage))

    def test_a_provider_declaring_nothing_is_still_found_by_the_walk(self):
        """The walk stays the compatibility path: a holder that predates the seam
        (the raw AcpClient, the doubles standing in for a runner) must keep
        billing exactly as before."""

        class _Runner:
            def __init__(self) -> None:
                self.last_prompt_stats = _Stats(4.0)

        class _KnownSeamProvider:
            def __init__(self) -> None:
                self._client = _Runner()

        self.assertEqual(provider_last_turn_usage(_KnownSeamProvider()).credits, 4.0)

    def test_the_abc_default_does_not_count_as_a_declaration(self):
        """A provider that inherits the ABC default has declared nothing, so the
        read must fall back to what it carries rather than report "no billing"."""
        from kiro_crew.providers.base import resolve_billing_stats

        class _Inheriting(LLMProvider):
            """Implements ONLY the abstract surface; billing is the ABC default."""

            def __init__(self) -> None:
                self.last_prompt_stats = _Stats(6.0)

            async def start(self) -> None:
                return None

            async def shutdown(self) -> None:
                return None

            async def stream(self, message: str):
                yield SimpleNamespace(kind="complete")

            async def approve_tool(self, request_id, *, always: bool = False) -> None:
                return None

            async def reject_tool(self, request_id) -> None:
                return None

            def context_usage_pct(self) -> float:
                return 0.0

        provider = _Inheriting()
        self.assertIsNone(provider.billing_stats())
        self.assertIs(resolve_billing_stats(provider), provider.last_prompt_stats)
        self.assertEqual(provider_last_turn_usage(provider).credits, 6.0)

    def test_an_attribute_of_that_name_on_a_double_does_not_shadow_the_billing(self):
        """The seam is read off the TYPE: a mock answers every attribute with an
        auto-created child, and consuming one as a stats object would zero a turn
        that really billed."""
        double = MagicMock()
        double.last_prompt_stats = _Stats(2.0)

        self.assertTrue(callable(double.billing_stats))
        self.assertIsNone(getattr(type(double), "billing_stats", None))
        self.assertEqual(provider_last_turn_usage(double).credits, 2.0)

    def test_a_raising_seam_falls_back_instead_of_losing_the_turn(self):
        class _Broken:
            def __init__(self) -> None:
                self._client = SimpleNamespace(last_prompt_stats=_Stats(1.25))

            def billing_stats(self) -> object | None:
                raise RuntimeError("seam is broken")

        self.assertEqual(provider_last_turn_usage(_Broken()).credits, 1.25)

    def test_the_seam_hands_back_the_object_so_identity_still_guards(self):
        """Reported by identity, not by value: a dispatch that failed before the
        runner installed fresh stats must bill nothing, even though the previous
        turn's credits are still readable."""

        class _Provider:
            def __init__(self) -> None:
                self._runner = SimpleNamespace(last_prompt_stats=_Stats(7.0))

            def billing_stats(self) -> object | None:
                return self._runner.last_prompt_stats

        provider = _Provider()
        prior = provider.billing_stats()
        self.assertEqual(provider_last_turn_usage(provider, since=prior).credits, 0.0)
        provider._runner.last_prompt_stats = _Stats(7.0)
        self.assertEqual(provider_last_turn_usage(provider, since=prior).credits, 7.0)


class TestEveryProviderDeclaresItsBilling(unittest.TestCase):
    """Ratchet: the seam only removes the defect if providers actually declare it.

    A new provider that forgets billing is the whole failure class -- its spend is
    absent from the usage page with nothing raised -- so it fails here instead. A
    genuinely unmetered backend satisfies this by overriding the method to return
    None, which documents the choice on the provider rather than in this test.
    """

    @staticmethod
    def _product_subclasses(root: type) -> list[type]:
        """Every subclass defined in the product tree, transitively.

        Filtered to ``kiro_crew.*`` so a test module's own double (this file's
        included) is not held to the product contract.
        """
        out: list[type] = []
        for cls in root.__subclasses__():
            if cls.__module__.startswith("kiro_crew."):
                out.append(cls)
            out.extend(TestEveryProviderDeclaresItsBilling._product_subclasses(cls))
        return out

    def test_concrete_providers_override_billing_stats(self):
        import kiro_crew.acp.session_provider  # noqa: F401
        import kiro_crew.providers.acp  # noqa: F401

        found = self._product_subclasses(LLMProvider)
        # Guards the ratchet itself: an import that stopped registering the
        # providers would make the assertion below vacuously true.
        self.assertGreaterEqual(len(found), 2, f"provider classes not registered: {found}")

        missing = sorted(
            f"{cls.__module__}.{cls.__name__}"
            for cls in found
            if not inspect.isabstract(cls) and cls.billing_stats is LLMProvider.billing_stats
        )
        self.assertEqual(
            missing,
            [],
            "these providers do not declare billing_stats(), so a turn they serve "
            "bills invisibly -- override it to return the live per-turn stats "
            f"object, or None if the backend is genuinely unmetered: {missing}",
        )

    def test_the_shared_background_adapter_path_reads_the_inner_declaration(self):
        """Background callers hold the adapter, whose only link to the runner is
        ``_sess.provider``. The adapter itself declares nothing -- it is
        application code that must not import the provider layer (the
        agent-SDK boundary gate) -- so the walk reaching that node and resolving
        ITS declaration is what keeps a background turn's spend visible."""
        from kiro_crew.session_background import _ProviderBgSession

        stats = _Stats(5.5)

        class _Inner:
            def billing_stats(self) -> object | None:
                return stats

        adapter = _ProviderBgSession(SimpleNamespace(provider=_Inner()))  # type: ignore[arg-type]

        self.assertFalse(hasattr(adapter, "billing_stats"))
        self.assertEqual(provider_last_turn_usage(adapter).credits, 5.5)

    def test_the_acp_provider_answers_from_either_client_shape(self):
        """``_client`` is a raw AcpClient until kiro startup replaces it with an
        AcpSessionProvider, and both shapes must report the same turn. Asserted
        through the seam itself: the walk would find these stats anyway, so only
        an identity check on ``billing_stats()`` pins the declaration."""
        from kiro_crew.acp.session_provider import AcpSessionProvider
        from kiro_crew.providers.acp import AcpProvider

        raw_stats = _Stats(1.5)
        raw = object.__new__(AcpProvider)
        raw._client = SimpleNamespace(last_prompt_stats=raw_stats)  # type: ignore[attr-defined]
        self.assertIs(raw.billing_stats(), raw_stats)
        self.assertEqual(provider_last_turn_usage(raw).credits, 1.5)

        shared_stats = _Stats(2.5)
        session_provider = object.__new__(AcpSessionProvider)
        session_provider._handle = SimpleNamespace(  # type: ignore[attr-defined]
            last_prompt_stats=shared_stats
        )
        self.assertIs(session_provider.billing_stats(), shared_stats)
        shared = object.__new__(AcpProvider)
        shared._client = session_provider  # type: ignore[attr-defined]
        self.assertIs(shared.billing_stats(), shared_stats)
        self.assertEqual(provider_last_turn_usage(shared).credits, 2.5)

    def test_a_wrapper_forwards_the_inner_declaration_not_the_attribute_name(self):
        """``AcpProvider`` holds whichever client the backend installs, so it
        forwards the CAPABILITY. Reading ``last_prompt_stats`` off the inner
        object instead would re-introduce the same name dependency one level
        down: an inner provider that keeps its runner elsewhere has no such
        attribute, and its billed turn would report nothing."""
        from kiro_crew.providers.acp import AcpProvider
        from kiro_crew.session_background import _ProviderBgSession

        stats = _Stats(8.25)

        class _FutureSeamProvider:
            """Declares billing; carries no ``last_prompt_stats`` of its own."""

            def billing_stats(self) -> object | None:
                return stats

        inner = _FutureSeamProvider()
        self.assertFalse(hasattr(inner, "last_prompt_stats"))

        wrapper = object.__new__(AcpProvider)
        wrapper._client = inner  # type: ignore[attr-defined]
        self.assertIs(wrapper.billing_stats(), stats)
        self.assertEqual(provider_last_turn_usage(wrapper).credits, 8.25)

        # The background adapter declares nothing and needs to: the walk reaches
        # its ``_sess.provider`` and resolves the declaration on that node.
        adapter = _ProviderBgSession(SimpleNamespace(provider=inner))  # type: ignore[arg-type]
        self.assertEqual(provider_last_turn_usage(adapter).credits, 8.25)


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


class _ClaudeStats:
    """Claude-seam per-turn stats: ``credits`` stays 0 and the billing travels
    through ``to_turn_usage()`` (the stats -> event contract from #6757)."""

    def __init__(self, usage) -> None:
        self.credits = 0.0
        self._usage = usage

    def to_turn_usage(self):
        return self._usage


class TestClaudeSeamBackgroundAccounting(unittest.IsolatedAsyncioTestCase):
    async def test_a_cost_only_turn_still_writes_its_row(self):
        """The #6758 shape: cost_usd billed with credits and both token counts
        at zero. Dropping the gate's cost_usd conjunct, or bypassing the
        duck-typed to_turn_usage read in _attempt_usage (whose credits-only
        fallback zeroes every claude dimension), makes this fail."""
        from kiro_crew.acp.types import TurnUsage

        sessions = _Sessions(_Client())
        with patch(_USAGE_TARGET) as persist:
            async with background_turn(sessions, task="consolidation") as client:
                client.last_prompt_stats = _ClaudeStats(
                    TurnUsage(cost_usd=0.42, cache_creation_tokens=20, cache_read_tokens=30)
                )

        self.assertEqual(persist.await_count, 1)
        recorded = persist.await_args.args[2]
        self.assertAlmostEqual(recorded.cost_usd, 0.42)
        self.assertEqual(recorded.cache_creation_tokens, 20)
        self.assertEqual(recorded.cache_read_tokens, 30)
        self.assertEqual(recorded.credits, 0.0)

    async def test_an_all_zero_claude_turn_writes_no_row(self):
        from kiro_crew.acp.types import TurnUsage

        sessions = _Sessions(_Client())
        with patch(_USAGE_TARGET) as persist:
            async with background_turn(sessions, task="consolidation") as client:
                client.last_prompt_stats = _ClaudeStats(TurnUsage())

        persist.assert_not_awaited()


class TestSumUsageCarriesTheBilledDimensions(unittest.TestCase):
    def test_claude_seam_dimensions_survive_the_sum(self):
        """A retried turn sums its attempts through _sum_usage; a dimension the
        sum drops is spend an earlier billed attempt silently loses.

        num_turns and duration_ms are deliberately absent: to_turn_usage never
        fills them and every persist site passes elapsed_ms explicitly.
        """
        from kiro_crew.acp.types import TurnUsage
        from kiro_crew.llm_helpers import _sum_usage

        left = TurnUsage(
            input_tokens=10,
            output_tokens=20,
            cache_creation_tokens=1,
            cache_read_tokens=2,
            cost_usd=0.1,
            credits=0.5,
        )
        right = TurnUsage(
            input_tokens=30,
            output_tokens=40,
            cache_creation_tokens=3,
            cache_read_tokens=4,
            cost_usd=0.2,
            credits=1.5,
        )
        total = _sum_usage(left, right)
        self.assertEqual(total.input_tokens, 40)
        self.assertEqual(total.output_tokens, 60)
        self.assertEqual(total.cache_creation_tokens, 4)
        self.assertEqual(total.cache_read_tokens, 6)
        self.assertAlmostEqual(total.cost_usd, 0.3)
        self.assertAlmostEqual(total.credits, 2.0)


class TestUsageHasBilling(unittest.TestCase):
    """The single predicate behind every persist gate: each billing dimension
    alone must open the gate, and an all-zero turn must not."""

    def test_each_dimension_alone_opens_the_gate(self):
        from kiro_crew.acp.types import TurnUsage
        from kiro_crew.llm_helpers import usage_has_billing

        for kwargs in (
            {"credits": 0.5},
            {"cost_usd": 0.42},
            {"input_tokens": 1},
            {"output_tokens": 1},
            {"cache_creation_tokens": 1},
            {"cache_read_tokens": 1},
        ):
            with self.subTest(**kwargs):
                self.assertTrue(usage_has_billing(TurnUsage(**kwargs)))

    def test_an_all_zero_turn_stays_out(self):
        from kiro_crew.acp.types import TurnUsage
        from kiro_crew.llm_helpers import usage_has_billing

        self.assertFalse(usage_has_billing(TurnUsage()))


class _FaultyConverterStats:
    """A stats holder whose to_turn_usage exists but raises: the converter's
    failure must degrade to the credits read, not zero the turn's billing."""

    def __init__(self, credits: float) -> None:
        self.credits = credits

    def to_turn_usage(self):
        raise RuntimeError("converter boom")


class TestFaultyConverterFallsBackToCredits(unittest.IsolatedAsyncioTestCase):
    async def test_credits_still_bill_when_the_converter_raises(self):
        sessions = _Sessions(_Client())
        with patch(_USAGE_TARGET) as persist:
            async with background_turn(sessions, task="consolidation") as client:
                client.last_prompt_stats = _FaultyConverterStats(credits=3.5)

        self.assertEqual(persist.await_count, 1)
        self.assertEqual(persist.await_args.args[2].credits, 3.5)
