"""The AUTO-compaction hook: it runs, and it changes nothing.

Three claims, and the second and third are the reason the point is allowed on this
path at all:

* both AUTOMATIC entry points reach the shadow scoring, because both funnel through
  ``_compact_session``;
* the compaction is not DELAYED by it -- the task is created and never awaited, so a
  scoring that never finishes cannot hold the compaction;
* the compaction's own OUTCOME is unaltered, arm for arm.

The manual ``/compact`` path is covered by ABSENCE and is asserted as such: the
dashboard dispatches that command through ``provider.stream_command`` and never calls
``_compact_session``, so the assertion is that no manual caller exists.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kiro_crew import session_compaction as sc

REPO = Path(__file__).resolve().parents[1]


class _Provider:
    """The narrowest provider the coordinator's gates accept."""

    manual_compact_unsupported_backend = None
    compaction_unmanaged_backend = None
    compaction_self_managed = True

    def __init__(self, pct: float = 80.0) -> None:
        self._pct = pct

    def context_usage_pct(self) -> float:
        return self._pct

    async def shutdown(self) -> None:
        return None


class _Session:
    def __init__(self) -> None:
        self.provider = _Provider()
        self.semaphore = asyncio.Semaphore(1)
        self.prompt_count = 0
        self.needs_context_reinjection = False
        self.floor_pending = False
        self.floor_pct = None


class _Owner:
    """A ``_CompactionOwner`` stub that records which arm ran."""

    def __init__(self, coordinator_holder: dict) -> None:
        self._cfg = type("Cfg", (), {"session": type("S", (), {"autocompact_pct": 70.0})()})()
        self._sessions: dict = {}
        self._lock = asyncio.Lock()
        self._recycling: dict = {}
        self._session_map = type("M", (), {"clear_sid": staticmethod(lambda _k: None)})()
        self._background_tasks: set = set()
        self.calls: list[str] = []
        self._holder = coordinator_holder

    def _fold_key(self, key: str) -> str:
        return key

    def _advance_session_generation(self, key: str) -> int:
        return 1

    def _compaction_gate_decision(self, key, provider, pct):
        return self._holder["c"]._compaction_gate_decision(key, provider, pct)

    def _trigger_compaction(self, key, reason, pct, provider):
        return self._holder["c"]._trigger_compaction(key, reason, pct, provider)

    async def _compact_session(self, key, pct):
        return await self._holder["c"]._compact_session(key, pct)

    async def _compact_in_place(self, key, session, pct):
        self.calls.append("in_place")
        return "ok"

    async def _recycle_held(self, key, session, pct, *, uncompactable=False):
        self.calls.append("recycle_held")

    async def _recycle_unmanaged(self, key, session, pct):
        self.calls.append("recycle_unmanaged")
        return "recycled"

    def _settle_compact_cooldown(self, key, provider, pct_before):
        return False

    def _judge_compact_effect(self, key, before, after):
        return False

    async def _reset_still_critical(self, key, before, after, *, expect):
        return False

    async def _fire_compact_callback(self, key, pct, *, success):
        self.calls.append(f"callback:{success}")

    def mark_needs_reinjection(self, key: str) -> None:
        self.calls.append("reinject")

    async def reset(self, key, **_kw):
        return False


def _coordinator() -> tuple[sc.CompactionCoordinator, _Owner]:
    import logging

    holder: dict = {}
    owner = _Owner(holder)
    deps = sc.CompactionDeps(
        logger=logging.getLogger("test.compaction"),
        is_claude_backend=lambda _p: False,
        is_cc_managed=lambda _p: False,
        get_recorder=lambda: None,
        context_pct_is_unknown=lambda _p: False,
        unlink_session_queue=lambda _s: None,
        compact_wait_timeout_secs=lambda: 5.0,
        compact_result_wait_secs=lambda _e: 1.0,
        context_warn_margin_pct=10.0,
        compact_result_wait_margin_secs=1.0,
        compact_failure_cooldown_secs=1.0,
        compact_min_effect_pct_points=5.0,
        post_compact_reset_pct=95.0,
    )
    coordinator = sc.CompactionCoordinator(owner, deps, state=sc.CompactionState())
    holder["c"] = coordinator
    return coordinator, owner


@pytest.fixture(autouse=True)
def _fresh_point_state():
    """Clear the point's pending records AND its attempt counters between cases.

    Autouse, because both are per-session module state: a case that minted an
    attempt for ``k`` would otherwise make the next one's first comparison unequal,
    which is an ordering dependency rather than a behaviour.
    """
    from kiro_crew.decisions.points import compaction_keep

    compaction_keep.reset_records()
    yield
    compaction_keep.reset_records()


@pytest.fixture
def scored(monkeypatch):
    """Record every ``(key, attempt, before)`` the shadow point was asked to score.

    The attempt token AND the transcript boundary are captured rather than ignored:
    the token is what stops a straggling scoring from publishing onto the NEXT
    compaction's notice, the boundary is what keeps it from scoring rows the
    compaction itself appended, and a coordinator that supplied neither would still
    pass a bare-key assertion.
    """
    seen: list[tuple[str, int, str]] = []

    async def _score(key: str, attempt: int = 0, *, before: str = ""):
        seen.append((key, attempt, before))
        return None

    from kiro_crew.decisions.points import compaction_keep

    monkeypatch.setattr(compaction_keep, "score_compaction", _score)
    return seen


class TestBothAutomaticEntryPointsScore:
    def test_the_awaited_between_turn_trigger_scores(self, scored):
        async def _go():
            coordinator, owner = _coordinator()
            owner._sessions["k"] = _Session()
            outcome = await coordinator.compact_if_needed("k")
            # Let the unawaited task run.
            await asyncio.gather(*owner._background_tasks)
            return outcome, owner

        outcome, owner = asyncio.run(_go())
        assert outcome == "ok"
        assert [k for k, _a, _b in scored] == ["k"]
        # A token was minted, which is what orders two compactions on one key. Its
        # VALUE comes from a process-wide counter, so it is not a literal here.
        assert scored[0][1] > 0
        assert owner.calls == ["in_place"]

    def test_the_per_turn_threshold_trigger_scores(self, scored):
        async def _go():
            coordinator, owner = _coordinator()
            owner._sessions["k"] = _Session()
            coordinator.check_context_usage("k", owner._sessions["k"].provider)
            await asyncio.gather(*list(owner._background_tasks))
            return owner

        asyncio.run(_go())
        assert [k for k, _a, _b in scored] == ["k"]
        # A token was minted, which is what orders two compactions on one key. Its
        # VALUE comes from a process-wide counter, so it is not a literal here.
        assert scored[0][1] > 0

    def test_a_backend_that_can_only_be_recycled_scores_too(self, scored):
        # The scoring sits ABOVE the arm choice, so the population is every backend
        # rather than the ones that can compact.
        async def _go():
            coordinator, owner = _coordinator()
            session = _Session()
            session.provider.compaction_unmanaged_backend = "deepseek"  # type: ignore[attr-defined]
            owner._sessions["k"] = session
            outcome = await coordinator._compact_session("k", 80.0)
            await asyncio.gather(*owner._background_tasks)
            return outcome, owner

        outcome, owner = asyncio.run(_go())
        assert outcome == "recycled"
        assert [k for k, _a, _b in scored] == ["k"]
        # A token was minted, which is what orders two compactions on one key. Its
        # VALUE comes from a process-wide counter, so it is not a literal here.
        assert scored[0][1] > 0
        assert owner.calls == ["recycle_unmanaged"]


class TestItCannotCostTheCompaction:
    def test_a_scoring_that_never_finishes_does_not_delay_the_compaction(self, monkeypatch):
        async def _hang(_key: str, _attempt: int = 0, **_kw):
            await asyncio.sleep(3600)

        from kiro_crew.decisions.points import compaction_keep

        monkeypatch.setattr(compaction_keep, "score_compaction", _hang)

        async def _go():
            coordinator, owner = _coordinator()
            owner._sessions["k"] = _Session()
            # The whole compaction has to complete while the scoring is still
            # outstanding. The scoring task is deliberately still UNFINISHED when the
            # compaction returns -- which is the claim: nothing awaits it.
            outcome = await asyncio.wait_for(coordinator._compact_session("k", 80.0), timeout=5)
            pending = [task for task in owner._background_tasks if not task.done()]
            for task in pending:
                task.cancel()
            return outcome, owner.calls, len(pending)

        outcome, calls, pending = asyncio.run(_go())
        assert outcome == "ok"
        assert calls == ["in_place"]
        assert pending == 1

    def test_a_point_that_raises_on_import_leaves_the_compaction_alone(self, monkeypatch):
        import kiro_crew.decisions.points.compaction_keep as ck

        def _boom(*_a, **_kw):
            raise RuntimeError("the point is broken")

        monkeypatch.setattr(ck, "score_compaction", _boom)

        async def _go():
            coordinator, owner = _coordinator()
            owner._sessions["k"] = _Session()
            return await coordinator._compact_session("k", 80.0), owner

        outcome, owner = asyncio.run(_go())
        assert outcome == "ok"
        assert owner.calls == ["in_place"]

    def test_the_outcome_is_identical_with_and_without_the_hook(self, monkeypatch):
        """Arm for arm, the same answer -- which is the whole shadow claim."""

        def _run(with_hook: bool) -> tuple[str, list[str]]:
            async def _go():
                coordinator, owner = _coordinator()
                if not with_hook:
                    monkeypatch.setattr(coordinator, "_shadow_score_compaction", lambda _key: None)
                owner._sessions["k"] = _Session()
                outcome = await coordinator._compact_session("k", 80.0)
                await asyncio.gather(*owner._background_tasks, return_exceptions=True)
                return outcome, owner.calls

            return asyncio.run(_go())

        from kiro_crew.decisions.points import compaction_keep

        monkeypatch.setattr(
            compaction_keep,
            "score_compaction",
            lambda _k, _a=0, **_kw: asyncio.sleep(0),
        )
        assert _run(True) == _run(False)

    def test_each_compaction_gets_its_own_attempt_token(self, scored):
        """Two compactions on one key are ordered by the loop turn that started them.

        Without a token, a scoring that straggled past its own compaction's notice had
        its record popped by the NEXT compaction and shown as that one's measurement.
        """

        async def _go():
            coordinator, owner = _coordinator()
            coordinator._shadow_score_compaction("k")
            coordinator._shadow_score_compaction("k")
            await asyncio.gather(*owner._background_tasks)

        asyncio.run(_go())
        first, second = (attempt for _key, attempt, _before in scored)
        # Ordered and distinct, which is the token's whole job. Never a literal pair:
        # the counter is process-wide precisely so that no value ever repeats.
        assert 0 < first < second

    def test_the_task_is_tracked_so_it_cannot_be_collected_mid_await(self, scored):
        async def _go():
            coordinator, owner = _coordinator()
            coordinator._shadow_score_compaction("k")
            tracked = len(owner._background_tasks)
            await asyncio.gather(*owner._background_tasks)
            return tracked, owner._background_tasks

        tracked, remaining = asyncio.run(_go())
        assert tracked == 1
        # The done callback discards it, so a long-lived gateway does not accumulate.
        assert remaining == set()


class TestTheBoundaryPrecedesTheCompaction:
    """The boundary handed to the point names the moment the compaction was DECIDED.

    The scoring task is created here but runs BESIDE the compaction, so a boundary read
    inside the task would already be later than whatever the compaction appended. Both
    tests let the compacting arm append first -- which is the real ordering, since
    awaiting a coroutine that does not itself yield never lets the task start -- and
    still expect the appended row to fall outside the measurement.
    """

    def test_the_boundary_is_earlier_than_anything_the_compaction_appends(self, scored):
        appended: list[str] = []

        async def _go():
            coordinator, owner = _coordinator()
            owner._sessions["k"] = _Session()

            async def _in_place(_key, _session, _pct):
                owner.calls.append("in_place")
                appended.append(datetime.now(timezone.utc).isoformat())
                return "ok"

            owner._compact_in_place = _in_place
            await coordinator._compact_session("k", 80.0)
            await asyncio.gather(*owner._background_tasks, return_exceptions=True)

        asyncio.run(_go())
        assert len(scored) == 1
        boundary = scored[0][2]
        assert boundary, "the coordinator must supply a boundary, not an empty default"
        cut = datetime.fromisoformat(boundary)
        assert appended and all(datetime.fromisoformat(when) >= cut for when in appended)

    def test_rows_appended_after_the_fire_are_not_scored(self, monkeypatch):
        """End to end: the point reads the log, and the compaction's own row is not in it."""
        from kiro_crew.decisions.points import compaction_keep

        rows = [
            {
                "role": "user",
                "content": "before the compaction",
                "ts": "2000-01-01T00:00:00+00:00",
            }
        ]
        read: list[list[dict]] = []

        async def _score(key: str, attempt: int = 0, *, before: str = ""):
            read.append(compaction_keep.read_rows(key, before))

        monkeypatch.setattr(compaction_keep, "score_compaction", _score)
        monkeypatch.setattr(
            "kiro_crew.history.ConversationLog",
            lambda *_a, **_kw: type("L", (), {"read_messages": lambda _s, _k: list(rows)})(),
        )

        async def _go():
            coordinator, owner = _coordinator()
            owner._sessions["k"] = _Session()

            async def _in_place(_key, _session, _pct):
                # What a real backend writes on its way out: the summary it produced.
                #
                # A second later rather than `now()`: the boundary is INCLUSIVE, and a
                # clock whose resolution is coarser than the gap between two calls in one
                # coroutine hands back the same instant for both -- which would keep the
                # row and is the correct answer for a row written in the boundary's own
                # tick. The edge is pinned in the point's suite; what this test is about
                # is a row that is unambiguously later.
                rows.append(
                    {
                        "role": "assistant",
                        "content": "Conversation compacted: Goal / Status",
                        "ts": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
                    }
                )
                return "ok"

            owner._compact_in_place = _in_place
            await coordinator._compact_session("k", 80.0)
            await asyncio.gather(*owner._background_tasks, return_exceptions=True)

        asyncio.run(_go())
        assert len(read) == 1
        contents = [row.get("content", "") for row in read[0]]
        assert "before the compaction" in contents
        assert not any("Conversation compacted" in c for c in contents)


class TestManualCompactIsExcluded:
    def test_nothing_outside_the_session_layer_calls_the_funnel(self):
        # The manual command is dispatched as an ordinary turn
        # (``provider.stream_command("/compact")``), so the exclusion is structural:
        # if a manual caller of ``_compact_session`` ever appears, this fails and the
        # point's own docstring becomes false.
        # A CALL, not a mention: the point's own docstring names the method it hooks,
        # and a prose reference is not a caller.
        pattern = re.compile(r"_compact_session\s*\(")
        allowed = {
            Path("src/kiro_crew/session_compaction.py"),
            Path("src/kiro_crew/session.py"),
        }
        offenders = []
        for path in (REPO / "src").rglob("*.py"):
            relative = path.relative_to(REPO)
            if relative in allowed:
                continue
            if pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(str(relative))
        assert offenders == []

    def test_the_manual_dashboard_path_runs_the_command_through_the_provider(self):
        runner = (REPO / "src/kiro_crew/dashboard/chat_runner.py").read_text(encoding="utf-8")
        assert 'first_word == "/compact"' in runner
        assert "_compact_session" not in runner


class TestTheRecordReachesTheNotice:
    """``state._compaction_keep_record`` -- the hand-off onto the card's own row."""

    def test_a_published_record_is_claimed_once(self):
        from kiro_crew.dashboard import state as dash_state
        from kiro_crew.decisions.points import compaction_keep

        compaction_keep.publish_record("k", {"turn_id": "t", "point": "compaction.keep"})
        assert dash_state._compaction_keep_record("k") == {
            "turn_id": "t",
            "point": "compaction.keep",
        }
        # Destructive, so the next compaction on this key does not inherit it.
        assert dash_state._compaction_keep_record("k") is None

    def test_no_record_is_the_ordinary_answer(self):
        from kiro_crew.dashboard import state as dash_state

        assert dash_state._compaction_keep_record("nobody") is None

    def test_a_broken_point_still_leaves_the_notice_appendable(self, monkeypatch):
        from kiro_crew.dashboard import state as dash_state
        from kiro_crew.decisions.points import compaction_keep

        def _boom(_key):
            raise RuntimeError("store is broken")

        monkeypatch.setattr(compaction_keep, "take_record", _boom)
        assert dash_state._compaction_keep_record("k") is None
