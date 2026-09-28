"""Compaction for the ACP backends that were outside ``ACP_BACKENDS_COMPACT``.

``test_manual_compact_gate`` and ``test_auto_compact_gate`` pin the KAS defect
and the shape of the fix.  This file pins the audit of the REST of the roster,
because "not a member" had come to mean four different things at once and only
one of them was a decision anyone had evidence for.

Three findings, one class each:

* opencode serves a manual ``/compact`` and finishes it INSIDE the
  ``session/prompt`` turn, which is a surprise twice over: it advertises no
  compaction command at all, and the earlier exclusion read that silence as the
  answer.  Driven live on opencode 1.18.30 the session's ``usage_update.used``
  climbed 14863 -> 17478 over four turns, a ``/compact`` prompt returned
  ``stopReason: end_turn`` with no status frame, and the next ordinary turn read
  14577 with the model answering from a summary.
* pi and goose look the same in SOURCE -- pi-acp 0.0.33 and goose 1.50.1 both
  dispatch ``/compact`` before any model turn and return the turn itself -- but
  neither could be driven here, so neither joins on the weaker evidence class.
  They stay unclassified, which still improves on being told they self-manage.
* KAS is the one correct exclusion, and it is correct for a REASON that is now
  a membership rather than an absence: it summarizes unasked and reports it, so
  Crew's meter falls back on its own.
* deepseek compacts nowhere Crew can see -- no ``available_commands_update`` at
  all, no compaction status in its ``session/update`` vocabulary -- while
  reporting a real meter.  Declining there bounded nothing and the reply
  promised a summary that never came.

Every newly added name is resolved with ``getattr`` rather than imported at
module scope, so on a tree without the fix these FAIL on the assertion that
names the missing property instead of erroring at collection.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import STOP_REASON_END_TURN
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKENDS_COMPACT,
    ACP_BACKENDS_INLINE_COMPACTION,
    ACP_BACKENDS_KNOWN,
)
from kiro_crew.agent_sdk import backends as sdk_backends
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.config import KiroCrewConfig
from kiro_crew.messaging import commands as messaging_commands
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.base import LLMProvider
from kiro_crew.session import SessionManager
from kiro_crew.session_compaction import CompactionCoordinator

KEY = "dashboard:compaction-other-backends"

#: The name under audit, resolved rather than imported. See the module docstring.
_SET_NAME = "ACP_BACKENDS_HARNESS_MANAGED_COMPACTION"
#: The property under audit, on the same terms.
_PROP_NAME = "compaction_unmanaged_backend"
#: The set that grants the one destructive arm, resolved rather than imported.
_RECYCLE_SET_NAME = "ACP_BACKENDS_CONTEXT_RECYCLE"


def _recycle_set() -> frozenset:
    """The recycle set, or a failure that names it."""
    value = getattr(sdk_backends, _RECYCLE_SET_NAME, None)
    assert value is not None, f"agent_sdk.backends declares no {_RECYCLE_SET_NAME}"
    return value


def _harness_managed_set() -> frozenset[str]:
    """The new set, or a failure that names it."""
    value = getattr(sdk_backends, _SET_NAME, None)
    assert value is not None, f"agent_sdk.backends declares no {_SET_NAME}"
    return value


def _unmanaged_of(provider: object) -> Any:
    """The new property off *provider*, or a failure that names it."""
    owner = type(provider)
    assert hasattr(owner, _PROP_NAME), f"{owner.__name__} declares no {_PROP_NAME}"
    return getattr(provider, _PROP_NAME)


# ── 1. memberships ─────────────────────────────────────────────────────────


class TestTheHarnessThatWasDrivenAndProved:
    def test_opencode_can_serve_a_manual_compact(self) -> None:
        """RED before the audit: opencode was absent because it advertises no
        compaction command, and that silence was read as the answer."""
        assert ACP_BACKEND_OPENCODE in ACP_BACKENDS_COMPACT

    def test_opencode_finishes_inside_the_prompt_turn(self) -> None:
        """The second half of its evidence, and the half that decides whether the
        caller waits.  Granting the first set without this one strands the waiter
        for its full timeout."""
        assert ACP_BACKEND_OPENCODE in ACP_BACKENDS_INLINE_COMPACTION
        assert capabilities_for(ACP_BACKEND_OPENCODE).compacts_inline is True

    def test_pi_and_goose_wait_for_a_capture(self) -> None:
        """Absence as a decision, and the decision is about the evidence CLASS.

        Both advertise ``compact`` and both dispatch it before any model turn --
        pi-acp 0.0.33 in ``prompt()``, goose 1.50.1 through ``execute_command`` --
        so their SOURCE says inline.  What neither has is a driven capture, which
        is the bar opencode met: a live session whose ``usage_update.used`` was
        seen to fall.  Source says what the code would do; a capture says what the
        harness did, and for a membership whose wrong answer makes
        ``wait_for_compaction`` report a completion that never happened, the
        second is the bar.

        Their position while they wait is better than the one they held: they take
        the arm that promises nothing rather than being told their harness manages
        compaction itself.
        """
        for backend in (ACP_BACKEND_PI, ACP_BACKEND_GOOSE):
            assert backend not in ACP_BACKENDS_COMPACT, backend
            assert backend not in ACP_BACKENDS_INLINE_COMPACTION, backend
            assert backend not in _harness_managed_set(), backend
            assert backend not in _recycle_set(), backend
            assert (
                messaging_commands.compact_refusal_arm(backend)
                == messaging_commands.COMPACT_ARM_UNCLASSIFIED
            ), backend

    def test_kiro_stays_on_the_waiting_arm(self) -> None:
        """kiro-cli serves ``/compact`` and reports it ASYNCHRONOUSLY, which is
        exactly what a non-member of the inline set means.  A change that
        widened the inline set by "everyone who can compact" would take
        kiro-cli's own status wait with it."""
        assert ACP_BACKEND_KIRO in ACP_BACKENDS_COMPACT
        assert ACP_BACKEND_KIRO not in ACP_BACKENDS_INLINE_COMPACTION

    def test_inline_stays_a_strict_subset(self) -> None:
        assert ACP_BACKENDS_INLINE_COMPACTION < ACP_BACKENDS_COMPACT


class TestTheTwoHarnessesThatDoNot:
    def test_kas_is_declined_because_it_manages_itself(self) -> None:
        """The one exclusion with evidence, stated as a positive claim."""
        assert ACP_BACKEND_KAS not in ACP_BACKENDS_COMPACT
        assert ACP_BACKEND_KAS in _harness_managed_set()

    def test_deepseek_claims_neither(self) -> None:
        """No ``/compact`` to send AND nothing on its surface to wait for, which
        is what makes a silent skip a leak rather than a decline."""
        assert ACP_BACKEND_DEEPSEEK not in ACP_BACKENDS_COMPACT
        assert ACP_BACKEND_DEEPSEEK not in _harness_managed_set()

    def test_the_two_sets_are_disjoint(self) -> None:
        """A harness Crew can hand ``/compact`` to has no need of the claim, and
        holding both would make the gate's two arms reachable at once."""
        assert not (ACP_BACKENDS_COMPACT & _harness_managed_set())

    def test_subset_of_known_backends(self) -> None:
        """H8: a capability cannot be granted to an identifier nothing knows."""
        assert _harness_managed_set() <= ACP_BACKENDS_KNOWN
        assert _recycle_set() <= ACP_BACKENDS_KNOWN


class TestTheDestructiveArmIsGrantedByMembership:
    """The one arm that ends a conversation may not be granted by exclusion.

    RED before this: ``compaction_unmanaged_backend`` answered a backend id for
    anything outside the other two sets, so every harness added later inherited
    the recycle without anyone deciding it -- the same defect as claiming
    self-management on no evidence, only louder, because this arm ends
    conversations (harness-parity H6).
    """

    def test_deepseek_is_named(self) -> None:
        assert ACP_BACKEND_DEEPSEEK in _recycle_set()

    def test_no_compacting_backend_is_a_member(self) -> None:
        """A backend Crew can compact is already bounded, so the arm is moot."""
        assert not (ACP_BACKENDS_COMPACT & _recycle_set())

    def test_no_harness_managed_backend_is_a_member(self) -> None:
        """Both arms would be reachable at once, and one of them destroys the
        conversation the other was about to shrink on its own."""
        assert not (_harness_managed_set() & _recycle_set())

    def test_an_unclassified_backend_is_not_recycled(self) -> None:
        """The fail direction, read off the property rather than the set: a
        backend in none of the three compaction sets must answer ``None`` here,
        so it declines like a harness-managed one instead of being destroyed."""
        unclassified = sorted(
            ACP_BACKENDS_KNOWN - ACP_BACKENDS_COMPACT - _harness_managed_set() - _recycle_set()
        )
        for backend in unclassified:
            provider = AcpProvider(acp_backend=backend)
            assert provider.manual_compact_unsupported_backend == backend, backend
            assert _unmanaged_of(provider) is None, backend
            # And it is distinguishable from a harness-managed backend, which is
            # what lets the gate log the missing decision instead of reporting a
            # settled condition.
            assert provider.compaction_self_managed is False, backend

    def test_a_harness_managed_backend_reads_as_self_managed(self) -> None:
        assert AcpProvider(acp_backend=ACP_BACKEND_KAS).compaction_self_managed is True

    def test_the_abc_default_is_self_managed(self) -> None:
        """H14: a provider that never spoke must not read as the unclassified
        case, because that reading is the one that raises a WARNING."""
        assert LLMProvider.compaction_self_managed.fget(None) is True  # type: ignore[arg-type]


# ── 2. the capability on every provider shape ──────────────────────────────


class TestEveryKnownBackendIsClassified:
    """The census, so a harness added later cannot land on a default by silence.

    Three sets answer "what bounds this context": ``ACP_BACKENDS_COMPACT`` (Crew
    compacts it), ``ACP_BACKENDS_HARNESS_MANAGED_COMPACTION`` (the harness does), and
    ``ACP_BACKENDS_CONTEXT_RECYCLE`` (Crew recycles it). A backend in none of them is
    a real and SAFE state -- it declines, and the gate logs a WARNING naming the
    decision that is missing -- but it must be a state someone chose, not one a new
    entry falls into because ``compaction_self_managed`` defaults True on the ABC.

    So this does not demand every backend be classified. It demands the unclassified
    ones be LISTED here, which makes adding a harness fail this test until its author
    either classifies it or writes it down.
    """

    #: Known backends deliberately outside all three compaction sets, each with why.
    #:
    #: * ``pi``, ``goose`` -- their source says they compact inline; no driven capture
    #:   confirms it, which is the bar ``ACP_BACKENDS_COMPACT`` holds members to. The
    #:   capture is tracked as deferred follow-up work.
    UNCLASSIFIED = frozenset({ACP_BACKEND_PI, ACP_BACKEND_GOOSE})

    def test_every_known_backend_is_classified_or_listed(self) -> None:
        classified = (
            ACP_BACKENDS_COMPACT | _harness_managed_set() | _recycle_set() | self.UNCLASSIFIED
        )
        missing = sorted(ACP_BACKENDS_KNOWN - classified)
        assert not missing, (
            f"these backends answer none of the three compaction sets and are not "
            f"listed as deliberately unclassified: {missing}. Put each in "
            f"ACP_BACKENDS_COMPACT (Crew compacts it), "
            f"ACP_BACKENDS_HARNESS_MANAGED_COMPACTION (the harness does), "
            f"ACP_BACKENDS_CONTEXT_RECYCLE (Crew recycles it), or in this test's "
            f"UNCLASSIFIED set with the reason -- an unclassified backend declines and "
            f"logs a WARNING, which is safe but must be a choice."
        )

    def test_the_listed_ones_really_are_unclassified(self) -> None:
        """The other direction, so a stale entry cannot mask a real gap: a backend
        listed here that HAS since been classified would let a future one hide behind
        it."""
        for backend in sorted(self.UNCLASSIFIED):
            assert backend not in ACP_BACKENDS_COMPACT, backend
            assert backend not in _harness_managed_set(), backend
            assert backend not in _recycle_set(), backend

    def test_an_unclassified_backend_takes_the_warning_arm(self) -> None:
        """And the arm it lands on is the one that reports the gap, not the one that
        reassures. ``compaction_self_managed`` is what the gate reads to tell an
        unclassified backend from a harness-managed one."""
        for backend in sorted(self.UNCLASSIFIED):
            provider = AcpProvider(acp_backend=backend)
            assert provider.compaction_self_managed is False, backend
            assert provider.manual_compact_unsupported_backend == backend, backend
            # And it is NOT recycled: the destructive arm is granted by membership.
            assert _unmanaged_of(provider) is None, backend


class TestCompactionUnmanagedBackend:
    """H14: declared on the ABC with a safe default, answered from membership."""

    def test_abc_default_claims_nothing(self) -> None:
        """A provider that never declared it is taken to manage its own
        context, because that is the answer that changes no behaviour."""
        assert getattr(LLMProvider, _PROP_NAME).fget(None) is None  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "backend",
        [ACP_BACKEND_KIRO, ACP_BACKEND_CLAUDE, ACP_BACKEND_OPENCODE],
    )
    def test_a_compacting_backend_claims_nothing(self, backend: str) -> None:
        assert _unmanaged_of(AcpProvider(acp_backend=backend)) is None

    def test_kas_claims_nothing_although_crew_cannot_compact_it(self) -> None:
        """The whole point of the second property: KAS answers a backend id to
        the FIRST question and ``None`` to this one.  Collapsing the two would
        recycle the one session that was about to shrink on its own."""
        provider = AcpProvider(acp_backend=ACP_BACKEND_KAS)
        assert provider.manual_compact_unsupported_backend == ACP_BACKEND_KAS
        assert _unmanaged_of(provider) is None

    def test_deepseek_names_itself(self) -> None:
        provider = AcpProvider(acp_backend=ACP_BACKEND_DEEPSEEK)
        assert _unmanaged_of(provider) == ACP_BACKEND_DEEPSEEK

    def test_session_provider_answers_the_same(self) -> None:
        """The shared-subagent shape, built via ``__new__`` so the property's
        real logic runs without spawning a runtime."""
        for backend, expected in (
            (ACP_BACKEND_DEEPSEEK, ACP_BACKEND_DEEPSEEK),
            (ACP_BACKEND_KAS, None),
            (ACP_BACKEND_OPENCODE, None),
        ):
            provider = AcpSessionProvider.__new__(AcpSessionProvider)
            provider._runtime = SimpleNamespace(acp_backend=backend)  # type: ignore[attr-defined]
            assert _unmanaged_of(provider) == expected, backend

    def test_non_string_backend_claims_nothing(self) -> None:
        """A spec'd double's auto-created attribute must not read as a positive
        claim that nothing compacts -- the same caution the sibling property
        takes, and here the cost of getting it wrong is a recycled session."""
        provider = AcpProvider(acp_backend=ACP_BACKEND_KIRO)
        provider._client = MagicMock()
        assert _unmanaged_of(provider) is None


# ── 3. the gate, driven through a real SessionManager ──────────────────────


@pytest.fixture
def cfg() -> KiroCrewConfig:
    c = KiroCrewConfig()
    c.session.timeout_secs = 2
    return c


async def _drain_background(mgr: SessionManager) -> None:
    pending = [t for t in mgr._background_tasks if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


@contextlib.asynccontextmanager
async def _managed(cfg: KiroCrewConfig, provider_factory: Any):
    """Manager whose teardown always runs, even on a failed assert.

    An async CONTEXT MANAGER rather than an async fixture, matching
    ``test_auto_compact_gate``: the CI-pinned ``pytest-asyncio==0.20.3`` is
    incompatible with pytest 8 for async fixtures.
    """
    mgr = SessionManager(cfg, provider_factory=provider_factory)
    try:
        yield mgr
    finally:
        await _drain_background(mgr)
        await mgr.close_all()


def _provider_factory(*, backend: str, pct: float = 92.0):
    """A provider answering both capability questions the way *backend* does.

    Both answers are set EXPLICITLY rather than left to the mock, because an
    auto-created ``AsyncMock`` attribute is truthy and the properties under test
    are the thing being exercised -- a mock that answered them by accident would
    make every arm below reachable at once.
    """
    unsupported = None if backend in ACP_BACKENDS_COMPACT else backend
    unmanaged = None if (unsupported is None or backend == ACP_BACKEND_KAS) else backend

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.start = AsyncMock()
        m.shutdown = AsyncMock()
        m.is_process_alive = lambda: True
        m.has_active_turn = lambda: False
        state = {"compacted": False}
        m.context_usage_pct = lambda: 0.0 if state["compacted"] else pct
        m.context_usage_unknown = lambda: False
        m.manual_compact_unsupported_backend = unsupported
        setattr(m, _PROP_NAME, unmanaged)
        # The real record, so ``capabilities_of`` answers this backend's own
        # inline verdict instead of the fail-closed unknown one.
        m.capabilities = capabilities_for(backend)

        async def _stream(_cmd):
            state["compacted"] = True
            for ev in []:
                yield ev  # pragma: no cover - an inline harness emits no status

        m.stream_command = MagicMock(side_effect=_stream)
        m.wait_for_compaction = AsyncMock(return_value={"type": "completed"})
        return m

    return factory


async def _live_session(mgr: SessionManager):
    provider, _, _ = await mgr.get_or_create(KEY)
    mgr.release(KEY)
    return provider, mgr._sessions[KEY]


class TestTheInlineCompletionLivesOnTheSeam:
    """``compact()`` records the result, so every caller of the wait gets it.

    Seventeen call sites across the channels, the task executor and the session
    layer do ``await provider.compact()`` and then
    ``await provider.wait_for_compaction()``.  None of them can know which
    harness they just talked to, and an inline harness answers no status frame at
    all -- so a completion taught to ONE of them is a 300s strand left in the
    other sixteen.  These pin it at the one place that knows: the method that ran
    the compaction.
    """

    @staticmethod
    def _provider(backend: str, queue_answer: dict) -> AcpProvider:
        """An ``AcpProvider`` whose client answers the QUEUE wait with *queue_answer*."""
        provider = AcpProvider(acp_backend=backend)
        client = MagicMock()
        client.backend = backend
        client.wait_for_compaction = AsyncMock(return_value=queue_answer)
        client._drain_post_compaction_metadata = AsyncMock()
        # A turn that ended on its own boundary, which is what the inline arm
        # requires -- answered through the client's OWN accessor rather than by
        # setting its private turn state, since that accessor is the seam the
        # provider reads.
        client.turn_finished_cleanly = MagicMock(return_value=True)
        provider._client = client
        return provider

    @pytest.mark.asyncio
    async def test_an_inline_backend_needs_no_queue_wait(self) -> None:
        """RED before the seam fix: the wait fell through to the client's queue,
        which an inline harness never answers, so it burned the whole budget."""
        provider = self._provider(ACP_BACKEND_OPENCODE, {"type": "timeout"})

        result = await provider.wait_for_compaction()

        assert result["type"] == "completed"
        # The point of the seam: the queue wait is never entered at all.
        provider._client.wait_for_compaction.assert_not_awaited()
        # No invented summary -- an inline harness reports none, and a made-up
        # one would reach the user as the backend's own words.
        assert result.get("summary") == ""

    @pytest.mark.asyncio
    async def test_kiro_still_falls_through_to_its_async_status(self) -> None:
        """The other direction, which the seam must not take with it: kiro-cli
        answers later, so short-circuiting it would acknowledge a compaction that
        had not happened."""
        provider = self._provider(ACP_BACKEND_KIRO, {"type": "completed"})

        await provider.wait_for_compaction()

        provider._client.wait_for_compaction.assert_awaited()

    @pytest.mark.asyncio
    async def test_a_cancelled_turn_is_not_a_completed_compaction(self) -> None:
        """RED before this: the arm answered from membership alone, so a
        ``/compact`` turn the user cancelled read as a success.

        That is the worst direction for this arm. A false ``completed`` resets the
        context meter and arms the compaction cooldown, so the session stays full
        AND stops retrying -- worse than the strand it replaced, because the strand
        at least ended in a recycle.
        """
        provider = self._provider(ACP_BACKEND_OPENCODE, {"type": "timeout"})
        provider._client.turn_finished_cleanly = MagicMock(return_value=False)

        result = await provider.wait_for_compaction()

        assert result["type"] == "timeout", "the capability must not answer here"
        provider._client.wait_for_compaction.assert_awaited()

    @pytest.mark.asyncio
    async def test_an_unanswerable_turn_state_withholds_the_answer(self) -> None:
        """Fails CLOSED on a client that cannot report its turn state at all: an
        unacked cancel leaves the stop reason empty while the cancel flag is
        already set, which is the case where the compaction is LEAST likely to
        have run."""
        provider = AcpProvider(acp_backend=ACP_BACKEND_OPENCODE)
        provider._client = SimpleNamespace(backend=ACP_BACKEND_OPENCODE)

        assert provider._inline_turn_finished_cleanly() is False

    def test_the_client_owns_the_turn_state_question(self) -> None:
        """The state belongs to the client, so the question is answered there.

        Reading the client's private turn fields from the provider would decide a
        compaction's fate from a shape it does not own, and would do it SILENTLY if
        either field were renamed. The positive test lives with the state: the stop
        reason must BE ``end_turn``, since "not cancelled" also admits a refusal, a
        length limit, a reason this build does not recognise, and a turn that
        reported none.
        """
        from kiro_crew.acp.client import AcpClient

        client = AcpClient.__new__(AcpClient)
        client._cancelled = False  # type: ignore[attr-defined]
        client._last_stop_reason = STOP_REASON_END_TURN  # type: ignore[attr-defined]
        assert client.turn_finished_cleanly() is True

        client._last_stop_reason = "refusal"  # type: ignore[attr-defined]
        assert client.turn_finished_cleanly() is False

        client._last_stop_reason = STOP_REASON_END_TURN  # type: ignore[attr-defined]
        client._cancelled = True  # type: ignore[attr-defined]
        assert client.turn_finished_cleanly() is False

    @pytest.mark.asyncio
    async def test_a_captured_status_still_wins(self) -> None:
        """The cache the seam sits behind, unchanged: a status captured mid-turn
        is the real answer and must not be replaced by the capability's."""
        provider = self._provider(ACP_BACKEND_OPENCODE, {"type": "timeout"})
        provider._compact_result = {"type": "failed", "summary": "too large"}

        result = await provider.wait_for_compaction()

        assert result == {"type": "failed", "summary": "too large"}

    def test_a_handle_with_no_runtime_stays_on_the_waiting_arm(self) -> None:
        """Fails CLOSED, and the direction is the point rather than caution.

        ``AcpSessionHandle.acp_backend`` resolves off the runtime, so a handle
        built through ``__new__`` -- the shape several suites use to exercise the
        queue drain without spawning a process -- cannot answer it at all.
        Claiming the capability there would tell every one of those callers a
        compaction completed that nothing ever ran; withholding it leaves them on
        the arm they were on before the capability existed.
        """
        handle = AcpSessionHandle.__new__(AcpSessionHandle)
        assert handle._compacts_inline() is False

    def test_the_arm_is_on_the_wait_not_on_compact(self) -> None:
        """WHERE it sits is the substance of the fix, and it is the part the
        first attempt got wrong. ``_compact_in_place`` drives the harness through
        ``stream_command("/compact")`` and never calls ``compact()``, so an arm
        placed on ``compact()`` left the AUTOMATIC path -- the one no user action
        is needed to reach -- still stranding. Both classes carry it, because both
        are reachable."""
        for method in (
            AcpProvider.wait_for_compaction,
            AcpSessionHandle.wait_for_compaction,
        ):
            assert "compacts_inline" in inspect.getsource(method), method.__qualname__
        for method in (AcpProvider.compact, AcpSessionHandle.compact):
            assert "compacts_inline" not in inspect.getsource(method), method.__qualname__

    def test_the_coordinator_carries_no_inline_branch(self) -> None:
        """The subtraction half of the fix.  With the seam answering, a second
        read in ``_compact_in_place`` would be a per-call-site special case of
        exactly the kind that left the other sixteen sites broken."""
        source = inspect.getsource(CompactionCoordinator._compact_in_place)
        assert "compacts_inline" not in source


class TestTheCoordinatorDoesNotBranchOnTheHarness:
    @pytest.mark.asyncio
    async def test_an_inline_backend_compacts_without_being_recycled(self, cfg) -> None:
        """End to end through a real ``SessionManager``, with the provider
        answering the way the seam now makes it answer."""
        async with _managed(cfg, _provider_factory(backend=ACP_BACKEND_OPENCODE)) as mgr:
            provider, session = await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "ok"

            provider.stream_command.assert_called_once()
            # Not recycled: same session, provider still alive.
            assert mgr._sessions.get(KEY) is session
            provider.shutdown.assert_not_awaited()


class TestAHarnessManagedBackendIsStillDeclined:
    @pytest.mark.asyncio
    async def test_kas_is_untouched(self, cfg) -> None:
        """The behaviour the audit must not regress.  KAS's context shrinks on
        its own, so the correct answer is still to do nothing at all."""
        async with _managed(cfg, _provider_factory(backend=ACP_BACKEND_KAS)) as mgr:
            provider, session = await _live_session(mgr)
            callback = AsyncMock()
            mgr.set_compact_callback(callback)

            assert await mgr.compact_if_needed(KEY) == "compact_unsupported"

            provider.stream_command.assert_not_called()
            assert mgr._sessions.get(KEY) is session
            provider.shutdown.assert_not_awaited()
            callback.assert_not_awaited()


class TestABackendNothingCompactsIsRecycled:
    @pytest.mark.asyncio
    async def test_deepseek_is_recycled_rather_than_skipped(self, cfg) -> None:
        """RED before the fix: this answered ``compact_unsupported`` and the
        context kept growing toward the window that ends the conversation.

        Recycling is the same outcome ``_compact_in_place`` already reaches when
        a compaction fails.  What changes is that it is reached on the REASON
        rather than after spending the whole timeout discovering it."""
        async with _managed(cfg, _provider_factory(backend=ACP_BACKEND_DEEPSEEK)) as mgr:
            provider, session = await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "recycled"

            # No prompt was sent and no status was awaited: there is nothing
            # there to answer either one.
            provider.stream_command.assert_not_called()
            provider.wait_for_compaction.assert_not_awaited()
            # Recycled: entry popped by identity and the provider shut down.
            assert mgr._sessions.get(KEY) is not session
            provider.shutdown.assert_awaited()

    @pytest.mark.asyncio
    async def test_the_callback_is_told_it_was_a_recycle(self, cfg) -> None:
        """RED before this: the callback said ``success=True`` and nothing else, so
        the surface announcing it said "Auto-compacted at N%." on a session whose
        history had just been discarded.

        A recycle IS a success to a caller that only needs to know the session has
        headroom again, which is why ``success`` cannot carry this. The distinction
        already existed for the metrics counter -- the ``_recycling`` marker keeps a
        recycle out of the success bucket -- and it now reaches the callback, so a
        notice can name the arm that actually ran.
        """
        seen: list[dict] = []

        async def record(key, pct, *, success, outcome="compacted"):
            seen.append({"key": key, "success": success, "outcome": outcome})

        async with _managed(cfg, _provider_factory(backend=ACP_BACKEND_DEEPSEEK)) as mgr:
            await _live_session(mgr)
            mgr.set_compact_callback(record)

            assert await mgr.compact_if_needed(KEY) == "recycled"

        assert seen, "the callback never fired"
        assert seen[-1]["success"] is True, "the session does have headroom again"
        assert seen[-1]["outcome"] == "restarted_uncompactable", seen

    @pytest.mark.asyncio
    async def test_a_compaction_is_not_reported_as_a_recycle(self, cfg) -> None:
        """The other direction, so the outcome is not simply hardcoded: a real
        in-place compaction must still report ``compacted``, or every surface would
        start telling users their history was thrown away."""
        seen: list[str] = []

        async def record(key, pct, *, success, outcome="compacted"):
            seen.append(outcome)

        async with _managed(cfg, _provider_factory(backend=ACP_BACKEND_OPENCODE)) as mgr:
            await _live_session(mgr)
            mgr.set_compact_callback(record)

            assert await mgr.compact_if_needed(KEY) == "ok"

        assert seen == ["compacted"], seen

    @pytest.mark.asyncio
    async def test_a_failed_teardown_does_not_leave_the_cause_behind(self, cfg) -> None:
        """The marker says WHY this recycle is happening, and it must not outlive it.

        ``_recycle_held`` sets it, ``_fire_compact_callback`` consumes it -- but the
        teardown between them shuts a provider down, and a provider that raises there
        skips the callback entirely.  The marker then survives into the NEXT recycle on
        the same key, which after a backend swap is a kiro-cli session whose in-place
        compaction merely failed: it would be announced as a backend that cannot compact
        at all, the exact untruth this change set removes.
        """
        factory = _provider_factory(backend=ACP_BACKEND_DEEPSEEK)

        def raising_factory(*a, **kw):
            m = factory(*a, **kw)
            m.shutdown = AsyncMock(side_effect=RuntimeError("provider reap failed"))
            return m

        seen: list[str] = []

        async def record(key, pct, *, success, outcome="compacted"):
            seen.append(outcome)

        async with _managed(cfg, raising_factory) as mgr:
            await _live_session(mgr)

            # The teardown raise is swallowed at the coordinator boundary, so the
            # recycle reports failure rather than propagating -- and never reaches
            # the callback that would have consumed the marker.
            assert await mgr.compact_if_needed(KEY) == "failed"
            assert not seen, "the callback cannot have run if the teardown raised"

            assert mgr._compaction.state.uncompactable_recycles == set()

            # The consequence, stated as the next recycle rather than as the set:
            # a later failed compaction on this key reports the failure, not a
            # missing capability.
            mgr.set_compact_callback(record)
            mgr._recycling[KEY] = object()  # type: ignore[assignment]
            try:
                await mgr._fire_compact_callback(KEY, 95.0, success=True)
            finally:
                mgr._recycling.pop(KEY, None)

        assert seen == ["recycled"], seen

    def test_the_channel_notices_split_the_same_three_ways(self) -> None:
        """The dashboard is not the only surface that announces a compaction.

        I fixed the dashboard leg and left the channel leg, so a Slack, Telegram,
        Discord, Teams, Webex, feishu, WeCom, WeChat, WhatsApp or iMessage user still
        read "was auto-compacted" on a session that had been REPLACED. Same vocabulary,
        same three arms, same rule about which one may name a missing capability.
        """
        from kiro_crew.dashboard.chat_compaction_notice import notice_text

        compacted = notice_text("slack", 85.0, success=True, outcome="compacted")
        after_failure = notice_text("slack", 85.0, success=True, outcome="recycled")
        uncompactable = notice_text("slack", 85.0, success=True, outcome="restarted_uncompactable")
        assert len({compacted, after_failure, uncompactable}) == 3

        for restart in (after_failure, uncompactable):
            assert "auto-compacted" not in restart, restart
            assert "restarted" in restart, restart
            assert "no longer remembers" in restart, restart

        assert "cannot compact" in uncompactable
        assert "cannot compact" not in after_failure
        # An unknown outcome falls back to the compacted wording rather than raising --
        # this renders a user-facing notice, and a KeyError here would drop it.
        assert notice_text("slack", 85.0, success=True, outcome="???") == compacted

    def test_the_three_notices_are_three_different_sentences(self) -> None:
        """A vocabulary nothing maps drifts back into one message, so this pins the
        dashboard carrying one sentence per outcome.

        Three, not two, and the third is the one that matters: a kiro-cli or opencode
        session whose in-place ``/compact`` merely TIMES OUT also reaches the recycle,
        so a single restart notice claimed those backends cannot compact -- false of a
        harness that can and failed once. Only the uncompactable notice may say that.
        """
        from kiro_crew.dashboard.state import (
            _AUTO_COMPACT_NOTICE,
            _AUTO_RECYCLE_NOTICE,
            _AUTO_RESTART_UNCOMPACTABLE_NOTICE,
        )

        compacted = _AUTO_COMPACT_NOTICE.format(pct=85)
        after_failure = _AUTO_RECYCLE_NOTICE.format(pct=85)
        uncompactable = _AUTO_RESTART_UNCOMPACTABLE_NOTICE.format(pct=85)
        assert len({compacted, after_failure, uncompactable}) == 3

        for restart in (after_failure, uncompactable):
            # Neither restart may claim a compaction happened, and both owe the user
            # what they would otherwise discover by asking the agent a question it
            # cannot answer.
            assert "auto-compacted" not in restart.lower(), restart
            assert "restarted" in restart, restart
            assert "no longer remembers" in restart, restart

        # The capability claim belongs to exactly one of them.
        assert "cannot compact" in uncompactable
        assert "cannot compact" not in after_failure

    @pytest.mark.asyncio
    async def test_an_unconfirmed_reading_never_spends_a_recycle(self, cfg) -> None:
        """Why the unmanaged arm FALLS THROUGH the rung instead of acting from
        it: the rungs below it still have to run.  A reading the session has not
        confirmed is exactly the ambiguity that must not destroy a
        conversation."""
        factory = _provider_factory(backend=ACP_BACKEND_DEEPSEEK)

        def unconfirmed_factory(*args, **kwargs):
            m = factory(*args, **kwargs)
            m.context_usage_unknown = lambda: True
            return m

        async with _managed(cfg, unconfirmed_factory) as mgr:
            provider, session = await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "unconfirmed"

            assert mgr._sessions.get(KEY) is session
            provider.shutdown.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_below_threshold_still_wins(self, cfg) -> None:
        """And the rung ABOVE it, for the same reason in the other direction: a
        session nowhere near its limit is not recycled for lacking a capability
        it has not needed yet."""
        async with _managed(cfg, _provider_factory(backend=ACP_BACKEND_DEEPSEEK, pct=10.0)) as mgr:
            provider, session = await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "below_threshold"

            assert mgr._sessions.get(KEY) is session
            provider.shutdown.assert_not_awaited()


# ── 4. what the user is told ───────────────────────────────────────────────


class TestTheRefusalTextSaysWhatHappens:
    def test_a_harness_managed_backend_keeps_the_summary_promise(self) -> None:
        reply = messaging_commands.compact_unsupported_reply(ACP_BACKEND_KAS)
        assert "manages compaction automatically" in reply
        assert ACP_BACKEND_KAS in reply

    def test_a_backend_nothing_compacts_is_not_promised_a_summary(self) -> None:
        """The claim under audit.  Telling a deepseek user that the backend
        summarizes on its own describes something that never happens, and it
        hides the thing that does."""
        reply = messaging_commands.compact_unsupported_reply(ACP_BACKEND_DEEPSEEK)
        assert "manages compaction automatically" not in reply
        assert "summarizes nothing on its own" in reply
        assert "fresh session" in reply
        assert ACP_BACKEND_DEEPSEEK in reply

    def test_the_recycle_arm_states_its_cause_not_just_its_outcome(self) -> None:
        """A restart the user cannot explain reads as Crew interfering.

        The recycle arm has to carry BOTH halves: this harness has no compaction
        anywhere, AND that is WHY Crew restarts the session. Held apart -- two true
        sentences sitting next to each other -- the reader has to guess whether the
        restart follows from the missing compaction or is an unrelated policy, and
        the answer changes whether they read it as Crew coping or Crew meddling.

        Checked on every surface that carries the arm, because the wording is local
        to each and only the CHOICE is shared, so a surface can state the outcome
        and drop the cause on its own.
        """
        from kiro_crew.imessage.transport_dispatch import _compact_refusal_text as imessage_text

        english = (
            messaging_commands.compact_unsupported_reply,
            messaging_commands.compact_refusal_plain_text,
            imessage_text,
        )
        for reply in english:
            text = reply(ACP_BACKEND_DEEPSEEK)
            # The absence of any compaction, the restart, and the link between them.
            assert "no way to compact" in text, reply
            assert "fresh session" in text, reply
            assert "BECAUSE of that" in text, reply

        chinese = messaging_commands.compact_unsupported_reply_zh(ACP_BACKEND_DEEPSEEK)
        assert "没有压缩上下文的办法" in chinese
        assert "新会话" in chinese
        assert "正因为如此" in chinese

    def test_an_unclassified_backend_is_promised_nothing(self) -> None:
        """The third arm. A harness nobody has classified must not be told its
        own backend summarizes, NOR that Crew will restart the session -- neither
        is known, and the reply that guesses either one is the defect this whole
        change removes."""
        unclassified = sorted(
            ACP_BACKENDS_KNOWN - ACP_BACKENDS_COMPACT - _harness_managed_set() - _recycle_set()
        )
        # An empty set here is not a pass: the arm still has to be reachable, so
        # it is exercised with an id outside every set.
        for backend in [*unclassified, "some-future-harness"]:
            reply = messaging_commands.compact_unsupported_reply(backend)
            assert "manages compaction automatically" not in reply, backend
            assert "fresh session" not in reply, backend
            assert backend in reply, backend

    def test_every_surface_gives_the_same_three_way_answer(self) -> None:
        """RED before this: WhatsApp held its own TWO-arm split, so every backend
        that was not harness-managed got the recycle promise -- including codex,
        which is in none of the three compaction sets and which Crew declines.
        Telling that user a fresh session is coming describes something that will
        not happen, which is the exact class of untruth this change removes.

        The wording stays per surface; the CHOICE must not. Both predicates are
        read from ``messaging.commands``, so a surface can differ in voice and
        cannot differ in which of the three cases it thinks it is in.
        """
        from kiro_crew.imessage.transport_dispatch import _compact_refusal_text as imessage_text

        # WhatsApp reads ``compact_refusal_plain_text`` at its call site with no
        # wrapper of its own, so the shared helper IS its surface here. iMessage
        # keeps a function only because it prepends a glyph.
        surfaces = (
            messaging_commands.compact_unsupported_reply,
            messaging_commands.compact_unsupported_reply_zh,
            messaging_commands.compact_refusal_plain_text,
            imessage_text,
        )
        # Three backends, one per arm: kas is harness-managed, deepseek is
        # recycled, and pi is in neither -- the case the two-arm split got
        # wrong on every surface that held one.
        for reply in surfaces:
            managed, recycled, unclassified = (
                reply(ACP_BACKEND_KAS),
                reply(ACP_BACKEND_DEEPSEEK),
                reply(ACP_BACKEND_PI),
            )
            assert len({managed, recycled, unclassified}) == 3, reply
            # And the specific promise that must not reach an unclassified
            # backend, in each surface's own words.
            assert "fresh session" not in unclassified, reply
            assert "新会话：" not in unclassified, reply

    def test_one_named_arm_decides_for_every_surface(self) -> None:
        """The subtraction that keeps the surfaces honest as arms are added.

        Each surface maps ``compact_refusal_arm`` to its own wording instead of
        re-reading the memberships, so a fourth arm added later cannot be silently
        missed by a surface -- which is exactly how the dashboard and iMessage came
        to hold a single sentence covering all three.
        """
        arm = messaging_commands.compact_refusal_arm
        assert arm(ACP_BACKEND_KAS) == messaging_commands.COMPACT_ARM_HARNESS_MANAGED
        assert arm(ACP_BACKEND_DEEPSEEK) == messaging_commands.COMPACT_ARM_RECYCLED
        assert arm(ACP_BACKEND_PI) == messaging_commands.COMPACT_ARM_UNCLASSIFIED
        # An id this build cannot name gets the arm that claims nothing.
        assert arm("not-a-backend") == messaging_commands.COMPACT_ARM_UNCLASSIFIED

    def test_pi_and_goose_are_the_unclassified_case_today(self) -> None:
        """Named rather than implied, because the arm above is only exercised
        while some known backend is unclassified. When either earns the driven
        capture ``ACP_BACKENDS_COMPACT`` asks of its members, this test says so, and
        whoever moves it can see what it stands in for."""
        for backend in (ACP_BACKEND_PI, ACP_BACKEND_GOOSE):
            assert backend not in ACP_BACKENDS_COMPACT, backend
            assert backend not in _harness_managed_set(), backend
            assert backend not in _recycle_set(), backend

    def test_only_the_arm_reads_a_compaction_membership(self) -> None:
        """The memberships have ONE reader, which is what makes the arm the single
        place the choice is made.

        Two predicates once sat between the arm and the sets, exported for
        surfaces to call; with every surface taking the arm instead, each had a
        single caller and no reason to exist. A second reader is how a surface
        starts deciding for itself again, so the COUNT is pinned rather than any
        particular name.
        """
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(messaging_commands))
        # Counted as membership TESTS rather than as text, so a prose mention in a
        # docstring is not mistaken for a second reader.
        reads: dict[str, int] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            for comparator in node.comparators:
                if isinstance(comparator, ast.Name) and comparator.id.startswith("ACP_BACKENDS_"):
                    reads[comparator.id] = reads.get(comparator.id, 0) + 1
        for membership in (
            "ACP_BACKENDS_HARNESS_MANAGED_COMPACTION",
            "ACP_BACKENDS_CONTEXT_RECYCLE",
        ):
            assert reads.get(membership) == 1, (membership, reads)
        # And the two predicates that stood between the arm and the sets are
        # gone rather than merely uncalled.
        names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert "compaction_is_harness_managed" not in names
        assert "compaction_is_recycled" not in names

    def test_the_chinese_reply_has_the_same_three_arms(self) -> None:
        """One wording for the three surfaces that speak Chinese, and the same
        three-way split -- a surface reading a two-arm copy would promise a
        restart on a backend Crew does not recycle."""
        zh = messaging_commands.compact_unsupported_reply_zh
        managed = zh(ACP_BACKEND_KAS)
        recycled = zh(ACP_BACKEND_DEEPSEEK)
        unclassified = zh("some-future-harness")
        assert managed != recycled != unclassified
        assert managed != unclassified
