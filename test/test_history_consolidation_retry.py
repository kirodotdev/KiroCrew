"""Persistent retry accounting for history consolidation.

A consolidation pass spends a billed LLM turn before it can write the durable
``last_consolidated`` marker, so a failure in between leaves the span
unconsolidated. Without durable accounting every entry point then re-spends that
turn indefinitely — the 60s idle sweep on every tick, session-expiry sweeps on
every expiry, and every gateway restart (the in-memory throttle is memory-only).
These tests pin the attempt counter, the exponential backoff, the abandon cap,
and that no entry point bypasses them.
"""

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import history as history_mod
from kiro_crew.dashboard.chat_persistence import (
    _rehydrate_slot_from_history,
    _save_slot_to_history,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import (
    _CONSOLIDATION_BACKOFF_BASE_SECS,
    _CONSOLIDATION_MAX_ATTEMPTS,
    ConversationLog,
    HistoryConsolidator,
)

KEY = "dashboard:chat-retry"


def _seed_log(tmp_path, key: str = KEY, count: int = 3) -> ConversationLog:
    """A real transcript with *count* unconsolidated messages."""
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.init()
    # These tests run on the event loop; the mutations under test are the
    # production ones (offloaded inside _consolidate), not this fixture setup.
    with history_mod.allow_on_loop_persist():
        for i in range(count):
            log.append(key, "user", f"m{i}")
    return log


def _make_consolidator(log: ConversationLog, **kw: Any) -> HistoryConsolidator:
    memory = MagicMock()
    memory.read_preferences.return_value = ""
    memory.read_projects.return_value = ""
    kw.setdefault("history_idle_secs", 0)
    kw.setdefault("sessions", None)
    return HistoryConsolidator(log=log, memory=memory, migrated=True, **kw)


def _total(log: ConversationLog, key: str = KEY) -> int:
    """The transcript's current message total, as an entry point would supply it."""
    return log.consolidation_counts(key)[0]


def _span(
    log: ConversationLog, total: int | None = None, key: str = KEY
) -> history_mod.AttemptedSpan:
    """The span identity a turn over the CURRENT transcript would attempt.

    Mirrors what ``_consolidate`` freezes from its pre-turn snapshot, so a test
    charging a failure by hand stamps the same identity production would.
    """
    meta = log.get_metadata(key)
    return history_mod.AttemptedSpan(
        total=_total(log, key) if total is None else total,
        generation=int(meta.get("rotation_generation", 0) or 0),
        offset=int(meta.get("last_consolidated", 0) or 0),
    )


def _eligible(c: HistoryConsolidator, log: ConversationLog, now=None) -> bool:
    """retry_eligible with the count its callers already hold."""
    return c.retry_eligible(KEY, now, message_count=_total(log))


def _dashboard_state(log: ConversationLog) -> DashboardState:
    """A DashboardState wired to *log* — enough for the slot rehydrate/save paths."""
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.channel_key_for_stem = MagicMock(return_value=None)
    return DashboardState(
        sessions=sessions,
        crons=MagicMock(
            list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})
        ),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=log,
    )


def _plant_raw_meta(log: ConversationLog, key: str, raw_fields: str) -> None:
    """Splice RAW JSON text into the metadata line.

    Goes around ``json.dumps`` on purpose: the hostile inputs are literals a
    serializer will not emit (``1e309``, a bare ``NaN``, ``"NaN"`` as a string),
    and they are exactly what a hand-edited or foreign-written transcript can
    carry.
    """
    path = log._path(key)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    meta_txt = lines[0].strip()
    assert meta_txt.endswith("}")
    lines[0] = f"{meta_txt[:-1]},{raw_fields}}}\n"
    path.write_text("".join(lines), encoding="utf-8")
    log._invalidate_cache(key)


class _FakeRequest:
    """Minimal aiohttp request stand-in for the manual consolidate handler."""

    def __init__(self, state: Any, body: dict) -> None:
        self.app = {"state": state}
        self.headers: dict[str, str] = {}
        self._body = body

    async def json(self) -> dict:
        return self._body


class TestFailureAfterTheBilledCall:
    @pytest.mark.asyncio
    async def test_exception_after_llm_call_does_not_retry_on_the_next_tick(
        self, tmp_path
    ):
        """A raise between the LLM call and the marker must arm backoff.

        The idle sweep's done-callback only sets its in-memory throttle when the
        task ends WITHOUT an exception, so on a raise all of check_idle_sessions'
        skip conditions are false again 60s later — a fresh billed turn per tick,
        forever. The durable counter is the only thing that stops it.
        """
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        c._last_activity[KEY] = time.time() - 10

        with patch.object(
            c, "_call_llm", AsyncMock(return_value={"history_entry": "x"})
        ), patch.object(log, "mark_consolidated", side_effect=RuntimeError("disk full")):
            c.check_idle_sessions()
            assert c._tasks, "idle sweep did not schedule a consolidation"
            await asyncio.gather(*list(c._tasks), return_exceptions=True)

        attempts, retry_at = log.consolidation_retry_state(KEY)
        assert attempts == 1
        assert retry_at > time.time()
        # The span is still unconsolidated, and the throttle was never set —
        # backoff is the sole remaining gate.
        assert log.unconsolidated_count(KEY) == 3
        assert KEY not in c._history_consolidated

        c._tasks.clear()
        c.check_idle_sessions()
        assert not c._tasks, "consolidation re-fired while inside the backoff window"

    @pytest.mark.asyncio
    async def test_a_failure_before_the_llm_call_does_not_consume_budget(
        self, tmp_path
    ):
        """Nothing was billed yet, so the attempt is free."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(
            log, "snapshot_for_consolidation", side_effect=RuntimeError("io error")
        ):
            with pytest.raises(RuntimeError):
                await c._consolidate(KEY, include_history=True)

        assert log.consolidation_retry_state(KEY) == (0, 0.0)

    @pytest.mark.asyncio
    async def test_none_result_consumes_an_attempt(self, tmp_path):
        """_call_llm swallows every exception and returns None.

        _consolidate's bare ``return`` on a falsy result happens BEFORE the
        marker write, so the task looks successful (throttle set) while the
        durable count still says unconsolidated. Treat it as a failed attempt.
        """
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)

        attempts, retry_at = log.consolidation_retry_state(KEY)
        assert attempts == 1
        assert retry_at > time.time()
        assert log.unconsolidated_count(KEY) == 3

    @pytest.mark.asyncio
    async def test_backoff_doubles_per_attempt(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)
            first = log.consolidation_retry_state(KEY)[1] - time.time()
            with history_mod.allow_on_loop_persist():
                log.update_metadata(KEY, {"consolidation_retry_at": 0.0})
            await c._consolidate(KEY, include_history=True)
            second = log.consolidation_retry_state(KEY)[1] - time.time()

        assert first == pytest.approx(_CONSOLIDATION_BACKOFF_BASE_SECS, abs=5)
        assert second == pytest.approx(2 * _CONSOLIDATION_BACKOFF_BASE_SECS, abs=5)


class TestAttemptCap:
    @pytest.mark.asyncio
    async def test_cap_abandons_the_span_with_the_durable_marker(self, tmp_path):
        """At the cap the marker is written anyway, ending the spend."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS - 1,
                    "consolidation_retry_at": 0.0,
                },
            )

        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)

        assert log.unconsolidated_count(KEY) == 0, (
            "abandoned span left unmarked — it will re-bill a turn on the next "
            "tick and after every restart"
        )
        # The marker releases the budget in the same write, so the NEXT span is
        # not charged for this one's failures.
        assert log.consolidation_retry_state(KEY) == (0, 0.0)
        c._last_activity[KEY] = time.time() - 10
        c._tasks.clear()
        c.check_idle_sessions()
        assert not c._tasks

    @pytest.mark.asyncio
    async def test_cap_without_a_marker_write_stays_ineligible(self, tmp_path):
        """If even the abandon write fails, the span must stop spending."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS,
                    "consolidation_retry_at": 0.0,
                },
            )

        assert c.retry_eligible(KEY) is False


class TestSuccessClearsTheAccounting:
    @pytest.mark.asyncio
    async def test_marking_a_span_releases_its_retry_budget(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)
        assert log.consolidation_retry_state(KEY)[0] == 1

        with patch.object(
            c, "_call_llm", AsyncMock(return_value={"history_entry": "ok"})
        ):
            with history_mod.allow_on_loop_persist():
                log.update_metadata(KEY, {"consolidation_retry_at": 0.0})
            await c._consolidate(KEY, include_history=True)

        assert log.unconsolidated_count(KEY) == 0
        assert log.consolidation_retry_state(KEY) == (0, 0.0)


class TestAccountingSurvivesARestart:
    @pytest.mark.asyncio
    async def test_a_fresh_consolidator_reads_the_persisted_backoff(self, tmp_path):
        """The in-memory throttle is lost on restart; the counter is not."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        c._last_activity[KEY] = time.time() - 10

        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)
        assert log.consolidation_retry_state(KEY)[0] == 1

        # Simulate a gateway restart: brand-new log + consolidator over the same
        # session directory, with every in-memory dict empty.
        fresh_log = ConversationLog(base_dir=tmp_path / "sessions")
        fresh = _make_consolidator(fresh_log)
        fresh._last_activity[KEY] = time.time() - 10
        assert not fresh._history_consolidated

        assert fresh.retry_eligible(KEY) is False
        fresh.check_idle_sessions()
        assert not fresh._tasks, "restart re-billed a turn for a backed-off span"


class TestTheAccountingIsReadUncached:
    """The accounting is cross-process, and its writers preserve the file mtime.

    A gateway sweep, the CLI and a subagent all record failures for the same
    session. Every writer of these fields restores the pre-write mtime so
    housekeeping does not reorder ``list_sessions`` — which means the mtime-keyed
    metadata cache cannot notice another process's write.
    """

    @pytest.mark.asyncio
    async def test_a_second_writers_count_is_not_hidden_by_a_warm_cache(
        self, tmp_path
    ):
        log = _seed_log(tmp_path)
        # A separate ConversationLog over the same directory stands in for the
        # other process — its own caches, its own view of the file.
        other = ConversationLog(base_dir=tmp_path / "sessions")

        path = log._path(KEY)
        # Warm this process's metadata cache the way the idle sweep does: it calls
        # unconsolidated_count (which caches the metadata line) immediately before
        # consulting the backoff.
        log.unconsolidated_count(KEY)
        assert log._meta_cache.get(KEY) is not None
        mtime_before = path.stat().st_mtime

        with history_mod.allow_on_loop_persist():
            other.record_consolidation_failure(KEY, 900.0, 86400.0, _span(other))

        assert path.stat().st_mtime == mtime_before, (
            "premise broken: the write advanced the mtime, so the cache would "
            "have self-invalidated and this test proves nothing"
        )

        attempts, retry_at = log.consolidation_retry_state(KEY)
        assert attempts == 1, "a stale cached count hid the other process's failure"
        assert retry_at > time.time()

    @pytest.mark.asyncio
    async def test_the_increment_builds_on_the_other_writers_count(self, tmp_path):
        """A stale read would overwrite the durable count with a lower one."""
        log = _seed_log(tmp_path)
        other = ConversationLog(base_dir=tmp_path / "sessions")
        # Frozen before the deliberate cache warm below, so reading the span does
        # not itself touch this process's caches.
        span = _span(log)

        with history_mod.allow_on_loop_persist():
            other.record_consolidation_failure(KEY, 900.0, 86400.0, span)
            other.record_consolidation_failure(KEY, 900.0, 86400.0, span)
            log.unconsolidated_count(KEY)  # warm this process's cache
            attempts, _ = log.record_consolidation_failure(KEY, 900.0, 86400.0, span)

        assert attempts == 3
        assert other.consolidation_retry_state(KEY)[0] == 3

    @pytest.mark.asyncio
    async def test_a_backed_off_span_is_not_re_fired_from_a_warm_cache(
        self, tmp_path
    ):
        """End to end: the idle sweep must see the other process's backoff."""
        log = _seed_log(tmp_path)
        other = ConversationLog(base_dir=tmp_path / "sessions")
        c = _make_consolidator(log)
        c._last_activity[KEY] = time.time() - 10

        log.unconsolidated_count(KEY)
        with history_mod.allow_on_loop_persist():
            other.record_consolidation_failure(KEY, 900.0, 86400.0, _span(other))

        c.check_idle_sessions()
        assert not c._tasks, (
            "the sweep billed an LLM turn for a span another process had just "
            "put into backoff"
        )


class TestHostileMetadataDoesNotBreakTheGate:
    """Metadata is caller-supplied JSON; the conversions must fail safe."""

    @pytest.mark.asyncio
    async def test_an_overflowing_attempt_count_reads_as_zero(self, tmp_path):
        """``1e309`` parses to ``inf``, and ``int(inf)`` raises OverflowError."""
        log = _seed_log(tmp_path)
        _plant_raw_meta(log, KEY, '"consolidation_attempts": 1e309')

        assert log.consolidation_retry_state(KEY) == (0, 0.0)
        assert _make_consolidator(log).retry_eligible(KEY) is True

    @pytest.mark.asyncio
    async def test_a_huge_integer_deadline_reads_as_zero(self, tmp_path):
        """An integer too large for a float raises OverflowError from float()."""
        log = _seed_log(tmp_path)
        _plant_raw_meta(log, KEY, '"consolidation_retry_at": ' + "9" * 400)

        assert log.consolidation_retry_state(KEY) == (0, 0.0)
        assert _make_consolidator(log).retry_eligible(KEY) is True

    @pytest.mark.asyncio
    async def test_an_infinite_deadline_reads_as_zero(self, tmp_path):
        """``1e309`` parses straight to ``inf``: no raise, but never expires."""
        log = _seed_log(tmp_path)
        _plant_raw_meta(log, KEY, '"consolidation_retry_at": 1e309')

        assert log.consolidation_retry_state(KEY) == (0, 0.0)
        assert _make_consolidator(log).retry_eligible(KEY) is True

    @pytest.mark.asyncio
    async def test_a_nan_deadline_does_not_disable_consolidation_forever(
        self, tmp_path
    ):
        """Every ``now >= nan`` is false, so a NaN deadline never expires."""
        raws = ('"consolidation_retry_at": NaN', '"consolidation_retry_at": "NaN"')
        for i, raw in enumerate(raws):
            log = _seed_log(tmp_path / f"case{i}")
            _plant_raw_meta(log, KEY, raw)

            assert log.consolidation_retry_state(KEY)[1] == 0.0
            assert _make_consolidator(log).retry_eligible(KEY) is True, (
                f"{raw} permanently disabled consolidation for the session"
            )

    @pytest.mark.asyncio
    async def test_a_non_numeric_value_reads_as_zero(self, tmp_path):
        log = _seed_log(tmp_path)
        _plant_raw_meta(
            log,
            KEY,
            '"consolidation_attempts": "lots", "consolidation_retry_at": {"a": 1}',
        )

        assert log.consolidation_retry_state(KEY) == (0, 0.0)

    @pytest.mark.asyncio
    async def test_the_manual_trigger_does_not_500_on_hostile_metadata(
        self, tmp_path
    ):
        """The gate runs inside a request handler — a raise there is a 500."""
        from kiro_crew.dashboard.handlers.memory import api_memory_consolidate

        log = _seed_log(tmp_path)
        _plant_raw_meta(log, KEY, '"consolidation_attempts": 1e309')
        c = _make_consolidator(log)

        state = MagicMock()
        state.consolidator = c
        state._restricted_keys = set()
        state._slots = {}
        request = _FakeRequest(state, {"key": KEY})

        with patch.object(c, "_consolidate", new_callable=AsyncMock):
            resp = await api_memory_consolidate(request)  # type: ignore[arg-type]
            assert resp.status == 200
            await asyncio.gather(*list(c._tasks), return_exceptions=True)

    @pytest.mark.asyncio
    async def test_an_absurd_stored_count_does_not_explode_the_backoff_shift(
        self, tmp_path
    ):
        """The exponent is attacker-influenced; ``2 ** n`` must stay bounded."""
        log = _seed_log(tmp_path)
        _plant_raw_meta(log, KEY, '"consolidation_attempts": 100000000')

        with history_mod.allow_on_loop_persist():
            attempts, retry_at = log.record_consolidation_failure(
                KEY, _CONSOLIDATION_BACKOFF_BASE_SECS, 86400.0, _span(log)
            )

        assert attempts == 100000001
        assert retry_at == pytest.approx(time.time() + 86400.0, abs=5)


class TestOnlyASentTurnConsumesTheCap:
    """The cap abandons a span, so only real spend may advance it.

    A pre-dispatch failure (no session manager, kiro-cli missing / not logged in /
    failing to start) costs nothing. Charging it would let a handful of environment
    failures write the durable marker over messages no LLM has ever read — the
    exact false abandonment this accounting exists to prevent.
    """

    @pytest.mark.asyncio
    async def test_a_pre_dispatch_failure_does_not_consume_an_attempt(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(
            c,
            "_call_llm",
            AsyncMock(side_effect=history_mod._ConsolidationNotDispatched("no cli")),
        ):
            await c._consolidate(KEY, include_history=True)

        attempts, retry_at = log.consolidation_retry_state(KEY)
        assert attempts == 0, "an unsent turn consumed the abandon budget"
        # The backoff is still armed, so a broken host does not re-attempt on the
        # next 60s tick.
        assert retry_at > time.time()
        assert log.unconsolidated_count(KEY) == 3

    @pytest.mark.asyncio
    async def test_repeated_pre_dispatch_failures_never_abandon_the_span(
        self, tmp_path
    ):
        """Past the cap count, the span must still be unmarked and retryable."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(
            c,
            "_call_llm",
            AsyncMock(side_effect=history_mod._ConsolidationNotDispatched("no cli")),
        ):
            for _ in range(_CONSOLIDATION_MAX_ATTEMPTS + 3):
                with history_mod.allow_on_loop_persist():
                    log.update_metadata(KEY, {"consolidation_retry_at": 0.0})
                await c._consolidate(KEY, include_history=True)

        assert log.consolidation_retry_state(KEY)[0] == 0
        assert log.unconsolidated_count(KEY) == 3, (
            "a broken environment abandoned a span without one billed turn"
        )
        # Still eligible once the deadline passes: the messages are not lost.
        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, {"consolidation_retry_at": 0.0})
        assert c.retry_eligible(KEY) is True

    @pytest.mark.asyncio
    async def test_the_environment_backoff_widens_per_failure(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(
            c,
            "_call_llm",
            AsyncMock(side_effect=history_mod._ConsolidationNotDispatched("no cli")),
        ):
            await c._consolidate(KEY, include_history=True)
            first = log.consolidation_retry_state(KEY)[1] - time.time()
            with history_mod.allow_on_loop_persist():
                log.update_metadata(KEY, {"consolidation_retry_at": 0.0})
            await c._consolidate(KEY, include_history=True)
            second = log.consolidation_retry_state(KEY)[1] - time.time()

        assert first == pytest.approx(_CONSOLIDATION_BACKOFF_BASE_SECS, abs=5)
        assert second == pytest.approx(2 * _CONSOLIDATION_BACKOFF_BASE_SECS, abs=5)

    @pytest.mark.asyncio
    async def test_a_post_dispatch_empty_result_still_consumes_an_attempt(
        self, tmp_path
    ):
        """The other half of the contract: a sent turn is still charged."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)

        assert log.consolidation_retry_state(KEY)[0] == 1

    @pytest.mark.asyncio
    async def test_no_session_manager_is_reported_as_not_dispatched(self, tmp_path):
        """_call_llm's own contract, not the caller's handling of it."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        assert c._sessions is None

        with pytest.raises(history_mod._ConsolidationNotDispatched):
            await c._call_llm("prompt")

    @pytest.mark.asyncio
    async def test_a_failure_acquiring_the_session_is_not_dispatched(self, tmp_path):
        """kiro-cli failing to start must not look like a spent turn."""
        log = _seed_log(tmp_path)
        sessions = MagicMock()
        sessions.get_or_create = AsyncMock(side_effect=RuntimeError("cli not found"))
        sessions.release = MagicMock()
        sessions.recycle_background = AsyncMock()
        c = _make_consolidator(log, sessions=sessions)

        with pytest.raises(history_mod._ConsolidationNotDispatched):
            await c._call_llm("prompt")

    @pytest.mark.asyncio
    async def test_a_failure_inside_the_turn_is_charged_as_dispatched(self, tmp_path):
        """Once the prompt is sent it may have been billed, so it returns None."""
        log = _seed_log(tmp_path)
        sessions = MagicMock()
        sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, False))
        sessions.release = MagicMock()
        sessions.recycle_background = AsyncMock()
        c = _make_consolidator(log, sessions=sessions)

        with patch.object(
            history_mod,
            "stream_and_collect_json",
            AsyncMock(side_effect=RuntimeError("stream died")),
        ):
            assert await c._call_llm("prompt") is None


class TestAccountingNeverResurrectsADeletedSession:
    @pytest.mark.asyncio
    async def test_recording_a_failure_for_a_deleted_session_is_a_no_op(
        self, tmp_path
    ):
        """_update_metadata_locked upserts, so a blind write recreates the file."""
        log = _seed_log(tmp_path)
        path = log._path(KEY)
        # The span a turn would have attempted, frozen while the file still
        # exists — the pre-turn snapshot production would be holding here.
        span = _span(log)
        path.unlink()

        with history_mod.allow_on_loop_persist():
            attempts, retry_at = log.record_consolidation_failure(
                KEY, 900.0, 86400.0, span
            )

        assert (attempts, retry_at) == (0, 0.0)
        assert not path.exists(), "a deleted session was resurrected as empty history"

    @pytest.mark.asyncio
    async def test_recording_an_environment_failure_for_a_deleted_session_is_a_no_op(
        self, tmp_path
    ):
        log = _seed_log(tmp_path)
        path = log._path(KEY)
        path.unlink()

        with history_mod.allow_on_loop_persist():
            log.record_consolidation_environment_failure(KEY, 900.0, 86400.0)

        assert not path.exists()

    @pytest.mark.asyncio
    async def test_a_session_deleted_mid_consolidation_leaves_no_file(self, tmp_path):
        """End to end: the delete lands while the LLM turn is in flight."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        path = log._path(KEY)

        async def _delete_then_fail(_prompt):
            path.unlink()
            return None

        with patch.object(c, "_call_llm", AsyncMock(side_effect=_delete_then_fail)):
            await c._consolidate(KEY, include_history=True)

        assert not path.exists()


class TestRotationDoesNotClearACappedBudget:
    """mark_consolidated is also the abandon path, and a rotation resets to 0.

    When the offset is not applied the span stays unconsolidated, so the write
    must not drop the accounting: clearing it there would hand the span a fresh
    budget every time a rewrite raced the marker, and the cap would never hold.
    Whether a LATER read still counts those attempts is a separate question,
    answered by span identity (see TestRotationReleasesTheBudgetForNewContent) —
    these tests pin the durable write.
    """

    @pytest.mark.asyncio
    async def test_a_generation_change_retains_the_capped_state(self, tmp_path):
        log = _seed_log(tmp_path)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS,
                    "consolidation_retry_at": time.time() + 3600,
                    "rotation_generation": 4,
                },
            )
            # The caller's snapshot generation (2) no longer matches, so
            # mark_consolidated resets the offset to 0 instead of applying it.
            log.mark_consolidated(KEY, 3, 2)

        meta = log.get_metadata(KEY)
        assert meta["last_consolidated"] == 0
        assert meta.get("consolidation_attempts") == _CONSOLIDATION_MAX_ATTEMPTS, (
            "an unapplied offset cleared the cap on an unmarked span, buying "
            "another billed attempt"
        )
        assert meta.get("consolidation_retry_at")

    @pytest.mark.asyncio
    async def test_an_offset_beyond_the_message_count_retains_the_capped_state(
        self, tmp_path
    ):
        """The count fallback also resets to 0 without advancing the marker."""
        log = _seed_log(tmp_path)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS,
                    "consolidation_retry_at": time.time() + 3600,
                },
            )
            log.mark_consolidated(KEY, 999, 0)

        assert log.get_metadata(KEY)["last_consolidated"] == 0
        assert (
            log.consolidation_retry_state(KEY)[0] == _CONSOLIDATION_MAX_ATTEMPTS
        )

    @pytest.mark.asyncio
    async def test_an_applied_offset_still_releases_the_budget(self, tmp_path):
        """The success path is unchanged: a marked span drops its accounting."""
        log = _seed_log(tmp_path)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": 2,
                    "consolidation_retry_at": time.time() + 3600,
                    "consolidation_env_failures": 4,
                    "consolidation_attempts_generation": 0,
                    "consolidation_attempts_offset": 0,
                },
            )
            log.mark_consolidated(KEY, 3, 0)

        meta = log.get_metadata(KEY)
        assert meta["last_consolidated"] == 3
        assert log.consolidation_retry_state(KEY) == (0, 0.0)
        for stale in (
            "consolidation_env_failures",
            "consolidation_attempts_generation",
            "consolidation_attempts_offset",
        ):
            assert stale not in meta, f"{stale} outlived the span it described"


class TestRotationReleasesTheBudgetForNewContent:
    """The cap abandons ONE span, so it must not outlive that span.

    A rotation archives the messages the failures were charged against and resets
    the marker to 0. Carrying a capped count onto the retained tail would silence
    consolidation for the session permanently — every message written afterwards
    stays ineligible forever. The counter is therefore bound to the
    ``(rotation_generation, last_consolidated)`` pair it was charged against: the
    same span keeps its cap, a genuinely new one gets a fresh bounded budget.
    """

    @pytest.mark.asyncio
    async def test_a_charged_attempt_records_the_span_it_belongs_to(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)

        meta = log.get_metadata(KEY)
        assert meta["consolidation_attempts"] == 1
        assert meta["consolidation_attempts_generation"] == 0
        assert meta["consolidation_attempts_offset"] == 0

    @pytest.mark.asyncio
    async def test_a_new_generation_gets_a_fresh_budget(self, tmp_path):
        log = _seed_log(tmp_path)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS,
                    "consolidation_retry_at": time.time() - 1,
                    "consolidation_attempts_generation": 0,
                    "consolidation_attempts_offset": 0,
                    "rotation_generation": 1,
                },
            )

        assert log.consolidation_retry_state(KEY)[0] == 0, (
            "a capped count from an archived span disabled consolidation for "
            "every message written after the rotation"
        )
        assert _make_consolidator(log).retry_eligible(KEY) is True

    @pytest.mark.asyncio
    async def test_the_same_span_stays_capped(self, tmp_path):
        """Round 2's invariant: no free attempt while the span is unchanged."""
        log = _seed_log(tmp_path)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS,
                    "consolidation_retry_at": time.time() - 1,
                    "consolidation_attempts_generation": 0,
                    "consolidation_attempts_offset": 0,
                },
            )

        assert log.consolidation_retry_state(KEY)[0] == _CONSOLIDATION_MAX_ATTEMPTS
        assert _make_consolidator(log).retry_eligible(KEY) is False, (
            "the same failing span bought another billed attempt"
        )

    @pytest.mark.asyncio
    async def test_a_fresh_budget_still_waits_out_the_backoff(self, tmp_path):
        """A new span is not a free immediate turn on a host that keeps failing."""
        log = _seed_log(tmp_path)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS,
                    "consolidation_retry_at": time.time() + 3600,
                    "consolidation_attempts_generation": 0,
                    "consolidation_attempts_offset": 0,
                    "rotation_generation": 1,
                },
            )

        assert log.consolidation_retry_state(KEY)[0] == 0
        assert _make_consolidator(log).retry_eligible(KEY) is False

    @pytest.mark.asyncio
    async def test_unstamped_accounting_keeps_the_cap(self, tmp_path):
        """Unknown provenance must fail closed, not grant unbounded retries."""
        log = _seed_log(tmp_path)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS,
                    "consolidation_retry_at": time.time() - 1,
                    "rotation_generation": 7,
                },
            )

        assert log.consolidation_retry_state(KEY)[0] == _CONSOLIDATION_MAX_ATTEMPTS

    @pytest.mark.asyncio
    async def test_content_written_after_a_real_rotation_consolidates(self, tmp_path):
        """End to end through the real rotation path, not a planted generation.

        The capped state is planted rather than accrued: when the abandon path's
        marker write SUCCEEDS it clears the accounting itself, so the state that
        survives a rotation is the one whose marker write was refused.
        """
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS,
                    "consolidation_retry_at": time.time() + 3600,
                    "consolidation_attempts_generation": 0,
                    "consolidation_attempts_offset": 0,
                },
            )
            assert c.retry_eligible(KEY) is False

            # Blow the byte budget so _maybe_rotate archives the failing messages
            # and bumps the generation itself.
            for i in range(5):
                log.append(KEY, "user", f"{i}" * (600 * 1024))
        assert log.get_metadata(KEY)["rotation_generation"] >= 1, "no rotation fired"

        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, {"consolidation_retry_at": 0.0})
        assert c.retry_eligible(KEY) is True, (
            "a rotation left the session permanently unable to consolidate"
        )

        with patch.object(
            c, "_call_llm", AsyncMock(return_value={"history_entry": "after rotation"})
        ):
            await c._consolidate(KEY, include_history=True)

        assert log.unconsolidated_count(KEY) == 0, (
            "post-rotation content never consolidated"
        )
        assert "consolidation_attempts" not in log.get_metadata(KEY)


class TestARotationDuringTheTurnStampsTheAttemptedSpan:
    """The charge must describe what the turn attempted, not what it returns to.

    The billed call is the whole point of the accounting, and the transcript is
    live while it is in flight — a rotation can land between the pre-turn
    snapshot and the failure charge. Re-reading the metadata line at charge time
    stamps the counter with the NEW generation, so the counter claims to have
    measured content no LLM has seen. At the cap that content is refused with its
    own identity already on the stamp, and nothing later can release it.
    """

    @staticmethod
    def _rotating_failure(log):
        """An LLM turn that rotates the transcript, then fails."""

        async def _turn(*_a, **_kw):
            with history_mod.allow_on_loop_persist():
                # Blow the byte budget so the real _maybe_rotate archives the
                # attempted messages and bumps the generation mid-turn.
                for i in range(5):
                    log.append(KEY, "user", f"{i}" * (600 * 1024))
            assert log.get_metadata(KEY)["rotation_generation"] >= 1, (
                "premise broken: no rotation fired during the turn"
            )
            return None

        return AsyncMock(side_effect=_turn)

    @pytest.mark.asyncio
    async def test_the_charge_carries_the_pre_turn_generation(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", self._rotating_failure(log)):
            await c._consolidate(KEY, include_history=True)

        meta = log.get_metadata(KEY)
        assert meta["consolidation_attempts"] == 1, "the failed turn was not charged"
        assert meta["rotation_generation"] >= 1, "premise broken: no rotation landed"
        assert meta["consolidation_attempts_generation"] == 0, (
            "the charge was stamped with the generation the rotation produced, "
            "so it claims to have measured content the turn never sent"
        )

    @pytest.mark.asyncio
    async def test_the_rotated_span_does_not_inherit_the_charge(self, tmp_path):
        """The consequence: retained messages must start with their own budget.

        A charge stamped with the post-rotation identity reads back as belonging
        to the retained content, so that content is short a turn of budget before
        it has been attempted even once — and the cap abandons it early.
        """
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", self._rotating_failure(log)):
            await c._consolidate(KEY, include_history=True)

        total = _total(log)
        meta = log.get_metadata(KEY)
        assert meta["consolidation_attempts"] == 1, "the failed turn was not charged"
        assert log.unconsolidated_count(KEY) > 0, (
            "premise broken: nothing is left pending after the rotation, so "
            "there is no span to strand"
        )
        assert total <= meta["consolidation_attempts_count"], (
            "premise broken: the transcript grew past the attempted extent, so "
            "the growth test would release the charge on its own and this would "
            "pass with the generation stamp wrong"
        )
        assert log.consolidation_retry_state(KEY, total)[0] == 0, (
            "the post-rotation messages carry a charge that was never spent on "
            "them, so their own budget is short and the cap abandons them early"
        )


class TestForeignWritersCannotEraseTheAccounting:
    """The accounting shares the metadata line with other layers' writers.

    A writer that REBUILDS that line from its own state deletes every field it
    does not enumerate. Two already did, and each silently reset the backoff so
    billed retries resumed. Preservation is now the default: a rebuilder names the
    keys it owns and carries the rest through.
    """

    @pytest.mark.asyncio
    async def test_a_compaction_preserves_the_retry_accounting(self, tmp_path):
        log = _seed_log(tmp_path)
        deadline = time.time() + 3600
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": 3,
                    "consolidation_retry_at": deadline,
                    "consolidation_attempts_generation": 0,
                    "consolidation_attempts_offset": 0,
                    "title": "kept",
                },
            )
            log.rewrite_session(KEY, log._read_messages(KEY)[-1:])

        attempts, retry_at = log.consolidation_retry_state(KEY)
        assert attempts == 3, "a compaction reset the backoff and re-billed the span"
        assert retry_at == pytest.approx(deadline, abs=1)
        assert log.get_metadata(KEY)["title"] == "kept"

    def test_a_dashboard_slot_save_preserves_the_retry_accounting(
        self, tmp_path, monkeypatch
    ):
        """The save rebuilds the whole metadata line from the slot's own state."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.state.config_dir", lambda: tmp_path
        )
        log = ConversationLog(base_dir=tmp_path)
        log.init()
        for i in range(3):
            log.append("dashboard:chat1", "user", f"m{i}")
        deadline = time.time() + 3600
        log.update_metadata(
            "dashboard:chat1",
            {
                "consolidation_attempts": 4,
                "consolidation_retry_at": deadline,
                "consolidation_attempts_generation": 0,
                "consolidation_attempts_offset": 0,
                "rotation_generation": 2,
            },
        )

        state = _dashboard_state(log)
        slot = _rehydrate_slot_from_history(state, "chat1")
        assert slot is not None
        slot._dirty = True
        _save_slot_to_history(state, slot)

        meta = log.get_metadata("dashboard:chat1")
        assert meta.get("consolidation_attempts") == 4, (
            "a dashboard slot save erased the retry accounting, resuming billed "
            "retries on a span that already spent its budget"
        )
        assert meta.get("consolidation_retry_at") == pytest.approx(deadline, abs=1)
        assert meta.get("consolidation_attempts_generation") == 0
        assert meta.get("rotation_generation") == 2

    def test_a_slot_owned_field_is_still_cleared_by_omission(
        self, tmp_path, monkeypatch
    ):
        """Preserving unowned keys must not make the slot's own state unclearable."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.state.config_dir", lambda: tmp_path
        )
        log = ConversationLog(base_dir=tmp_path)
        log.init()
        log.append("dashboard:chat1", "user", "m0")
        log.update_metadata(
            "dashboard:chat1", {"pinned": True, "consolidation_attempts": 1}
        )

        state = _dashboard_state(log)
        slot = _rehydrate_slot_from_history(state, "chat1")
        assert slot is not None
        slot.pinned = False
        slot._dirty = True
        _save_slot_to_history(state, slot)

        meta = log.get_metadata("dashboard:chat1")
        assert "pinned" not in meta, "an un-pinned slot could not clear its pin"
        assert meta.get("consolidation_attempts") == 1

    def test_the_helper_never_shadows_an_owned_key(self):
        rebuilt = {"title": "new"}
        existing = {"title": "old", "consolidation_attempts": 2, "rotation_generation": 1}
        out = history_mod.carry_unowned_metadata(
            rebuilt, existing, frozenset({"title"})
        )
        assert out == {
            "title": "new",
            "consolidation_attempts": 2,
            "rotation_generation": 1,
        }


class TestAnEditedTranscriptEarnsAFreshBudget:
    """An edit swaps in content no consolidation turn read, so it advances the
    session's content identity — and that ONE counter carries both guarantees.

    Preserving the accounting across a REBUILD is right; letting it (or a marker
    written by a turn already in flight) apply to EDITED content is not. A
    regenerate replaces the assistant tail with a reply the failing turns never
    saw, and it lands at the same message count, the same marker and the same
    extent — so nothing but the rotation generation distinguishes it, and without
    the bump a capped span would strand brand-new content forever while an
    in-flight attempt would mark it consolidated unread.
    """

    def _plant_capped_slot(
        self, tmp_path, monkeypatch, retry_in: float = 3600.0
    ) -> ConversationLog:
        """A two-message dashboard transcript whose span sits at the cap.

        *retry_in* places the armed backoff deadline relative to now — negative
        for a span whose wait has already elapsed (so eligibility turns purely on
        the budget), positive for one still serving it.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        log = ConversationLog(base_dir=tmp_path)
        log.init()
        log.append("dashboard:chat1", "user", "ask")
        log.append("dashboard:chat1", "assistant", "first answer")
        log.update_metadata(
            "dashboard:chat1",
            {
                "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS,
                "consolidation_retry_at": time.time() + retry_in,
                "consolidation_attempts_generation": 0,
                "consolidation_attempts_offset": 0,
                "consolidation_attempts_count": log.consolidation_counts(
                    "dashboard:chat1"
                )[0],
            },
        )
        return log

    def _regenerate(self, state, slot) -> None:
        """What the regenerate path does: swap the assistant tail for a different
        reply and persist that window as an explicit snapshot (a rewrite)."""
        snapshot = list(slot.messages)
        snapshot[-1] = {**snapshot[-1], "content": "regenerated answer"}
        slot.messages[:] = snapshot
        slot._dirty = True
        _save_slot_to_history(state, slot, snapshot)

    def test_a_regenerated_reply_is_not_held_by_the_old_spans_cap(
        self, tmp_path, monkeypatch
    ):
        log = self._plant_capped_slot(tmp_path, monkeypatch, retry_in=-1.0)
        before = log.consolidation_counts("dashboard:chat1")[0]
        c = _make_consolidator(log)
        assert not c.retry_eligible("dashboard:chat1", message_count=before), (
            "premise broken: the span is not capped, so the edit has nothing to "
            "release and this would pass without the fix"
        )

        state = _dashboard_state(log)
        slot = _rehydrate_slot_from_history(state, "chat1")
        assert slot is not None
        self._regenerate(state, slot)

        after = log.consolidation_counts("dashboard:chat1")[0]
        meta = log.get_metadata("dashboard:chat1")
        assert after == before, (
            "premise broken: the rewrite changed the message total, so the growth "
            "test would release the charge on its own"
        )
        assert int(meta.get("last_consolidated", 0) or 0) == 0, (
            "premise broken: the rewrite moved the marker, which releases the "
            "charge on its own"
        )
        assert meta.get("consolidation_attempts") == _CONSOLIDATION_MAX_ATTEMPTS, (
            "premise broken: the accounting was dropped outright, so this passes "
            "without the span identity having moved"
        )
        assert "regenerated answer" in log._path("dashboard:chat1").read_text(
            encoding="utf-8"
        ), "premise broken: the replacement reply never reached disk"

        assert log.consolidation_retry_state("dashboard:chat1", after)[0] == 0, (
            "the replacement reply inherited the exhausted budget of the span it "
            "replaced"
        )
        assert c.retry_eligible("dashboard:chat1", message_count=after), (
            "a reply no consolidation turn has ever read is permanently "
            "ineligible for consolidation"
        )

    def test_the_edit_advances_the_sessions_content_identity(
        self, tmp_path, monkeypatch
    ):
        """The release above is the span-identity semantics, not a special case."""
        log = self._plant_capped_slot(tmp_path, monkeypatch)
        state = _dashboard_state(log)
        slot = _rehydrate_slot_from_history(state, "chat1")
        assert slot is not None
        before = log.rotation_generation("dashboard:chat1")
        self._regenerate(state, slot)
        assert log.rotation_generation("dashboard:chat1") == before + 1, (
            "the edit left the content identity untouched, so a consolidation "
            "holding the pre-edit generation cannot tell its span was replaced"
        )

    def test_an_edit_does_not_buy_a_free_billed_turn(self, tmp_path, monkeypatch):
        """A fresh budget is not an immediate turn — same as a rotation.

        Releasing the deadline too would let a user hammering regenerate re-bill a
        failing consolidation on every gesture, which is exactly what the backoff
        exists to stop.
        """
        log = self._plant_capped_slot(tmp_path, monkeypatch, retry_in=3600.0)
        armed = log.get_metadata("dashboard:chat1")["consolidation_retry_at"]
        state = _dashboard_state(log)
        slot = _rehydrate_slot_from_history(state, "chat1")
        assert slot is not None
        self._regenerate(state, slot)

        count = log.consolidation_counts("dashboard:chat1")[0]
        attempts, retry_at = log.consolidation_retry_state("dashboard:chat1", count)
        assert attempts == 0, "the edit did not release the exhausted budget"
        assert retry_at == pytest.approx(armed, abs=1), (
            "the edit discarded the armed backoff deadline"
        )
        assert not _make_consolidator(log).retry_eligible(
            "dashboard:chat1", message_count=count
        ), "an edit let the session skip a backoff it had not served"

    def test_an_edit_invalidates_an_attempt_already_in_flight(
        self, tmp_path, monkeypatch
    ):
        """The completion write of a turn that snapshotted the PRE-edit span must
        not mark the replacement tail consolidated.

        The turn read the original reply, the user regenerated it, and only then
        did the turn finish. Its offset still fits the file — the count, the
        marker and the extent are all unchanged — so nothing but the advanced
        generation stops ``mark_consolidated`` from marking a reply no LLM has
        ever seen as already extracted.
        """
        log = self._plant_capped_slot(tmp_path, monkeypatch)
        key = "dashboard:chat1"
        # The consolidation turn starts: one atomic pre-turn snapshot.
        _msgs, total, generation = log.snapshot_for_consolidation(key)
        assert total == 2 and generation == 0

        state = _dashboard_state(log)
        slot = _rehydrate_slot_from_history(state, "chat1")
        assert slot is not None
        self._regenerate(state, slot)
        assert log.consolidation_counts(key)[0] == total, (
            "premise broken: the edit changed the message count, so the offset "
            "fallback in mark_consolidated would catch this without the fix"
        )

        # The turn now completes and writes the marker for the span it read.
        log.mark_consolidated(key, total, generation)

        meta = log.get_metadata(key)
        assert int(meta.get("last_consolidated", 0) or 0) == 0, (
            "the regenerated reply was marked consolidated without ever being "
            "extracted — silent memory loss"
        )
        assert log.consolidation_counts(key)[1] == total, (
            "the replacement content is not queued for consolidation"
        )

    def test_a_steady_flush_leaves_the_content_identity_alone(
        self, tmp_path, monkeypatch
    ):
        """Only an EDIT advances it; a re-serialization of the same window is not
        evidence about content, or every flush would release the cap."""
        log = self._plant_capped_slot(tmp_path, monkeypatch, retry_in=-1.0)
        state = _dashboard_state(log)
        slot = _rehydrate_slot_from_history(state, "chat1")
        assert slot is not None
        slot._dirty = True
        _save_slot_to_history(state, slot)

        count = log.consolidation_counts("dashboard:chat1")[0]
        assert log.rotation_generation("dashboard:chat1") == 0
        assert (
            log.consolidation_retry_state("dashboard:chat1", count)[0]
            == _CONSOLIDATION_MAX_ATTEMPTS
        ), "a steady flush released the cap, so a failing span retries forever"

    def test_the_helper_preserves_a_foreign_field_unconditionally(self):
        existing = {
            "consolidation_attempts": 3,
            "consolidation_retry_at": 123.0,
            "rotation_generation": 1,
            "title": "kept",
        }
        assert history_mod.carry_unowned_metadata({}, existing, frozenset()) == existing


class TestTheCapDoesNotOutliveTheSpanItMeasured:
    """The cap is reachable only when the abandon-marker write itself failed.

    Refusing that span is correct; refusing the SESSION is not. Appended messages
    leave the generation and the marker untouched, so without the span's extent
    one transient write failure would reject the transcript forever and the
    session's history would never be consolidated again.
    """

    def _plant_capped(self, log, count: int | None = None, retry_at: float = -1.0):
        """A span charged to the cap whose abandon-marker write did not land."""
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS,
                    "consolidation_retry_at": (
                        time.time() + retry_at if retry_at > 0 else time.time() - 1
                    ),
                    "consolidation_attempts_generation": 0,
                    "consolidation_attempts_offset": 0,
                    "consolidation_attempts_count": (
                        _total(log) if count is None else count
                    ),
                },
            )

    @pytest.mark.asyncio
    async def test_a_charged_attempt_records_the_spans_extent(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)

        assert log.get_metadata(KEY)["consolidation_attempts_count"] == 3

    @pytest.mark.asyncio
    async def test_the_extent_is_the_attempted_count_not_the_current_size(
        self, tmp_path
    ):
        """A message arriving DURING the failing turn must not be swallowed.

        It was never sent to the provider, so recording the post-turn size would
        bury it inside the charged extent and the growth test would never fire for
        it — at the cap it would be excluded from consolidation permanently.
        """
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        async def _append_then_fail(_prompt):
            with history_mod.allow_on_loop_persist():
                log.append(KEY, "user", "arrived mid-turn")
            return None

        with patch.object(c, "_call_llm", AsyncMock(side_effect=_append_then_fail)):
            await c._consolidate(KEY, include_history=True)

        assert log.get_metadata(KEY)["consolidation_attempts_count"] == 3, (
            "the mid-turn message was recorded as attempted, so growth can never "
            "release it"
        )
        # It reads as growth, so the counter no longer describes this span. (The
        # armed deadline still applies — a fresh budget is not a free turn.)
        assert log.consolidation_retry_state(KEY, _total(log))[0] == 0
        assert _total(log) == 4

    @pytest.mark.asyncio
    async def test_a_capped_span_with_no_growth_stays_ineligible(self, tmp_path):
        """Round 2's guarantee: an unchanged failing span cannot burn forever."""
        log = _seed_log(tmp_path)
        self._plant_capped(log)

        assert log.consolidation_retry_state(KEY, _total(log))[0] == (
            _CONSOLIDATION_MAX_ATTEMPTS
        )
        assert _eligible(_make_consolidator(log), log) is False

    @pytest.mark.asyncio
    async def test_transcript_growth_releases_the_cap(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        self._plant_capped(log)
        assert _eligible(c, log) is False

        with history_mod.allow_on_loop_persist():
            log.append(KEY, "user", "arrived after the cap")

        assert log.consolidation_retry_state(KEY, _total(log))[0] == 0, (
            "one failed abandon write refused every message the session will "
            "ever write again"
        )
        assert _eligible(c, log) is True

    @pytest.mark.asyncio
    async def test_growth_still_waits_out_the_armed_backoff(self, tmp_path):
        """A fresh budget is not a free immediate turn."""
        log = _seed_log(tmp_path)
        self._plant_capped(log, retry_at=3600.0)
        with history_mod.allow_on_loop_persist():
            log.append(KEY, "user", "arrived after the cap")

        assert log.consolidation_retry_state(KEY, _total(log))[0] == 0
        assert _eligible(_make_consolidator(log), log) is False

    @pytest.mark.asyncio
    async def test_the_grown_span_re_arms_the_cap(self, tmp_path):
        """Growth buys ONE bounded budget, not an escape from the cap."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        self._plant_capped(log)
        with history_mod.allow_on_loop_persist():
            log.append(KEY, "user", "arrived after the cap")
        assert _eligible(c, log) is True

        with history_mod.allow_on_loop_persist():
            for _ in range(_CONSOLIDATION_MAX_ATTEMPTS):
                log.record_consolidation_failure(KEY, 900.0, 86400.0, _span(log))
                log.update_metadata(KEY, {"consolidation_retry_at": time.time() - 1})

        meta = log.get_metadata(KEY)
        assert meta["consolidation_attempts"] == _CONSOLIDATION_MAX_ATTEMPTS
        assert meta["consolidation_attempts_count"] == 4, (
            "the cap re-armed against the OLD extent, so the same growth would "
            "release it again"
        )
        assert log.consolidation_retry_state(KEY, _total(log))[0] == (
            _CONSOLIDATION_MAX_ATTEMPTS
        )
        assert _eligible(c, log) is False, (
            "the original failing prefix can re-bill indefinitely"
        )

    @pytest.mark.asyncio
    async def test_a_shrink_alone_does_not_release_the_cap(self, tmp_path):
        """Growth is `>`, not `!=` — a rewrite that only drops content adds none."""
        log = _seed_log(tmp_path)
        self._plant_capped(log, count=99)

        assert log.consolidation_retry_state(KEY, _total(log))[0] == (
            _CONSOLIDATION_MAX_ATTEMPTS
        )
        assert _eligible(_make_consolidator(log), log) is False

    @pytest.mark.asyncio
    async def test_eligibility_never_reads_the_transcript(self, tmp_path):
        """It runs on the gateway loop; a full-file read there stalls everything."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        self._plant_capped(log)
        total = _total(log)

        reads: list[str] = []
        real_read_text = Path.read_text

        def _counting_read_text(self_path, *a, **kw):
            if self_path == log._path(KEY):
                reads.append(str(self_path))
            return real_read_text(self_path, *a, **kw)

        with patch.object(Path, "read_text", _counting_read_text):
            c.retry_eligible(KEY, message_count=total)
            log.consolidation_retry_state(KEY, total)

        assert reads == [], (
            "the eligibility check read the whole transcript on the event loop"
        )

    @pytest.mark.asyncio
    async def test_content_written_after_a_failed_abandon_consolidates(self, tmp_path):
        """End to end through a real entry point, which is what consults the gate.

        Driving ``_consolidate`` directly would prove nothing here: the cap lives
        in ``retry_eligible``, so only a caller that passes through it can show the
        session recovering.
        """
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        self._plant_capped(log)

        c.consolidate_session(KEY)
        assert not c._tasks, "a capped span with no growth still fired a turn"

        with history_mod.allow_on_loop_persist():
            log.append(KEY, "user", "arrived after the cap")

        with patch.object(
            c, "_call_llm", AsyncMock(return_value={"history_entry": "recovered"})
        ):
            c.consolidate_session(KEY)
            assert c._tasks, (
                "one failed abandon write left the session permanently unable to "
                "consolidate anything it writes from now on"
            )
            await asyncio.gather(*list(c._tasks), return_exceptions=True)

        assert log.unconsolidated_count(KEY) == 0
        assert "consolidation_attempts" not in log.get_metadata(KEY)


class TestEveryEntryPointRespectsTheAccounting:
    @pytest.mark.asyncio
    async def test_consolidate_session_honors_the_backoff(self, tmp_path):
        """The expiry path consults no time throttle of its own at all."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": 1,
                    "consolidation_retry_at": time.time() + 3600,
                },
            )

        c.consolidate_session(KEY)
        assert not c._tasks, "session expiry re-fired a backed-off consolidation"

        # Past the deadline the same call proceeds, so backoff delays rather than
        # disables the path.
        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, {"consolidation_retry_at": time.time() - 1})
        with patch.object(c, "_consolidate", new_callable=AsyncMock):
            c.consolidate_session(KEY)
            assert c._tasks
            await asyncio.gather(*list(c._tasks), return_exceptions=True)

    @pytest.mark.asyncio
    async def test_manual_dashboard_trigger_refuses_inside_the_backoff(self, tmp_path):
        from kiro_crew.dashboard.handlers.memory import api_memory_consolidate

        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": 2,
                    "consolidation_retry_at": time.time() + 3600,
                },
            )

        state = MagicMock()
        state.consolidator = c
        state._restricted_keys = set()
        state._slots = {}
        request = _FakeRequest(state, {"key": KEY})

        resp = await api_memory_consolidate(request)  # type: ignore[arg-type]
        assert resp.status == 429
        assert not c._tasks, "manual trigger bypassed the backoff"

        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, {"consolidation_retry_at": time.time() - 1})
        with patch.object(c, "_consolidate", new_callable=AsyncMock):
            resp = await api_memory_consolidate(request)  # type: ignore[arg-type]
            assert resp.status == 200
            await asyncio.gather(*list(c._tasks), return_exceptions=True)


class TestTheManualTriggerClaimsAtomically:
    """Two concurrent POSTs must not both dispatch a billed turn.

    The eligibility probe awaits an off-loop transcript read, so a membership
    test taken before that yield and acted on after it is a check-then-act race.
    """

    @pytest.mark.asyncio
    async def test_concurrent_triggers_dispatch_only_once(self, tmp_path):
        from kiro_crew.dashboard.handlers.memory import api_memory_consolidate

        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        state = MagicMock()
        state.consolidator = c
        state._restricted_keys = set()
        state._slots = {}

        with patch.object(c, "_consolidate", new_callable=AsyncMock) as spy:
            # Both requests are in flight across the handler's await, which is
            # exactly the window a check-then-act guard leaves open.
            responses = await asyncio.gather(
                api_memory_consolidate(_FakeRequest(state, {"key": KEY})),  # type: ignore[arg-type]
                api_memory_consolidate(_FakeRequest(state, {"key": KEY})),  # type: ignore[arg-type]
            )
            await asyncio.gather(*list(c._tasks), return_exceptions=True)

        statuses = sorted(r.status for r in responses)
        assert statuses == [200, 409], f"expected one winner and one refusal, got {statuses}"
        assert spy.await_count == 1, (
            f"consolidation dispatched {spy.await_count} times for one span — "
            "the claim is not atomic across the handler's await"
        )

    @pytest.mark.asyncio
    async def test_a_refused_trigger_releases_its_claim(self, tmp_path):
        """A 429 must not leave the key claimed, or the span is wedged forever."""
        from kiro_crew.dashboard.handlers.memory import api_memory_consolidate

        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": 2,
                    "consolidation_retry_at": time.time() + 3600,
                },
            )

        state = MagicMock()
        state.consolidator = c
        state._restricted_keys = set()
        state._slots = {}
        request = _FakeRequest(state, {"key": KEY})

        resp = await api_memory_consolidate(request)  # type: ignore[arg-type]
        assert resp.status == 429
        assert KEY not in c._running, "the backoff refusal leaked its claim"

        # Once the backoff expires the same key is dispatchable again, which it
        # would not be if the refusal above had left the claim in place.
        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, {"consolidation_retry_at": time.time() - 1})
        with patch.object(c, "_consolidate", new_callable=AsyncMock):
            resp = await api_memory_consolidate(request)  # type: ignore[arg-type]
            assert resp.status == 200
            await asyncio.gather(*list(c._tasks), return_exceptions=True)
