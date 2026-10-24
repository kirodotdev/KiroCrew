"""Durable retry accounting for history consolidation: attempts, backoff and the cap.

Among the cases: a failure after the billed LLM call arming the durable backoff, the
attempt cap abandoning its span, success clearing the accounting, a fresh consolidator
reading the persisted backoff, uncached and hostile metadata reads, a contended Windows
rename, a pre-dispatch failure leaving the cap unspent, and accounting that never
resurrects a deleted session. The shared helpers come from
``history_consolidation_retry_helpers.py``.
"""

import asyncio
import contextlib
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from history_consolidation_retry_helpers import (
    KEY,
    _FakeRequest,
    _make_consolidator,
    _seed_log,
    _span,
)
from windows_sim import replace_sharing_violation

from kiro_crew import atomic_write as aw
from kiro_crew import history as history_mod
from kiro_crew import history_consolidation, platform_compat
from kiro_crew.history import (
    _CONSOLIDATION_BACKOFF_BASE_SECS,
    _CONSOLIDATION_MAX_ATTEMPTS,
    ConversationLog,
)
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent


class _OneTurnProvider:
    """Streams *text* as one turn, on fresh stats that bill *credits*, as a runner does."""

    served_model = "test-model"
    # A backend in ACP_BACKENDS_REPORT_USAGE, whose zero billing means nothing was billed.
    reports_turn_usage = True

    def __init__(self, text: str, credits: float) -> None:
        self._text = text
        self._credits = credits
        self.last_prompt_stats = SimpleNamespace(credits=0.0)

    async def stream(self, _message: str, *, allow_image: bool = True):
        self.last_prompt_stats = SimpleNamespace(credits=self._credits)
        if self._text:
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=self._text)
        yield LLMEvent(kind=EVENT_COMPLETE)


def _background_turn_on(provider: _OneTurnProvider):
    @contextlib.asynccontextmanager
    async def _turn(_sessions, **_kwargs):
        yield provider

    return _turn


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


class TestFailureAfterTheBilledCall:
    @pytest.mark.asyncio
    async def test_exception_after_llm_call_does_not_retry_on_the_next_tick(self, tmp_path):
        """A raise between the LLM call and the marker must arm backoff.

        The idle sweep's done-callback only sets its in-memory throttle when the
        task ends WITHOUT an exception, so on a raise all of check_idle_sessions'
        skip conditions are false again 60s later — a fresh billed turn per tick,
        forever. The durable counter is the only thing that stops it.
        """
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        c._last_activity[KEY] = time.time() - 10

        with (
            patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "x"})),
            patch.object(log, "mark_consolidated", side_effect=RuntimeError("disk full")),
        ):
            c.check_idle_sessions()
            assert c._tasks, "idle sweep did not schedule a consolidation"
            # flake-ok: pre-existing unbounded gather of scheduled tasks, moved verbatim
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
    async def test_a_failure_before_the_llm_call_does_not_consume_budget(self, tmp_path):
        """Nothing was billed yet, so the attempt is free."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(log, "snapshot_for_consolidation", side_effect=RuntimeError("io error")):
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

        # flake-ok: pre-existing 5 s wall-clock window on the backoff, moved verbatim
        assert first == pytest.approx(_CONSOLIDATION_BACKOFF_BASE_SECS, abs=5)
        # flake-ok: pre-existing 5 s wall-clock window on the backoff, moved verbatim
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
        # The sweep counts off the loop, so the count is drained before the
        # check that nothing was consolidated.
        with patch.object(c, "_consolidate", AsyncMock()) as again:
            c.check_idle_sessions()
            while c._tasks:
                # flake-ok: pre-existing unbounded gather of scheduled tasks, moved verbatim
                await asyncio.gather(*list(c._tasks))
        again.assert_not_awaited()

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

    @pytest.mark.asyncio
    async def test_abandoning_a_span_reports_it_to_the_owner(self, tmp_path):
        """The abandon path tells its owner which span it dropped and why."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        seen: list[tuple[str, int, str]] = []
        c.on_abandoned = lambda key, count, reason, char_count=0, last_slice=False: seen.append(
            (key, count, reason)
        )
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

        assert seen == [(KEY, 3, "LLM turn failed")]

    @pytest.mark.asyncio
    async def test_a_failed_attempt_below_the_cap_reports_nothing(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        seen: list[tuple[str, int, str]] = []
        c.on_abandoned = lambda key, count, reason, char_count=0, last_slice=False: seen.append(
            (key, count, reason)
        )

        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)

        assert seen == []
        assert log.unconsolidated_count(KEY) == 3

    @pytest.mark.asyncio
    async def test_a_raising_owner_callback_does_not_undo_the_abandon(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        def _boom(
            key: str, count: int, reason: str, char_count: int = 0, last_slice: bool = False
        ) -> None:
            raise RuntimeError("bell unavailable")

        c.on_abandoned = _boom
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

        assert log.unconsolidated_count(KEY) == 0
        assert log.consolidation_retry_state(KEY) == (0, 0.0)


class TestSuccessClearsTheAccounting:
    @pytest.mark.asyncio
    async def test_marking_a_span_releases_its_retry_budget(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)
        assert log.consolidation_retry_state(KEY)[0] == 1

        with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "ok"})):
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
    async def test_a_second_writers_count_is_not_hidden_by_a_warm_cache(self, tmp_path):
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
    async def test_a_backed_off_span_is_not_re_fired_from_a_warm_cache(self, tmp_path):
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
            "the sweep billed an LLM turn for a span another process had just " "put into backoff"
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
    async def test_a_nan_deadline_does_not_disable_consolidation_forever(self, tmp_path):
        """Every ``now >= nan`` is false, so a NaN deadline never expires."""
        raws = ('"consolidation_retry_at": NaN', '"consolidation_retry_at": "NaN"')
        for i, raw in enumerate(raws):
            log = _seed_log(tmp_path / f"case{i}")
            _plant_raw_meta(log, KEY, raw)

            assert log.consolidation_retry_state(KEY)[1] == 0.0
            assert (
                _make_consolidator(log).retry_eligible(KEY) is True
            ), f"{raw} permanently disabled consolidation for the session"

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
    async def test_the_manual_trigger_does_not_500_on_hostile_metadata(self, tmp_path):
        """The gate runs inside a request handler — a raise there is a 500."""
        from kiro_crew.dashboard.handlers.memory import api_memory_consolidate

        log = _seed_log(tmp_path)
        _plant_raw_meta(log, KEY, '"consolidation_attempts": 1e309')
        c = _make_consolidator(log)

        state = MagicMock()
        state.consolidator = c
        state.conversation_log = log
        state._restricted_keys = set()
        state._slots = {}
        request = _FakeRequest(state, {"key": KEY})

        with patch.object(c, "_consolidate", new_callable=AsyncMock):
            resp = await api_memory_consolidate(request)  # type: ignore[arg-type]
            assert resp.status == 200
            # flake-ok: pre-existing unbounded gather of scheduled tasks, moved verbatim
            await asyncio.gather(*list(c._tasks), return_exceptions=True)

    @pytest.mark.asyncio
    async def test_an_absurd_stored_count_does_not_explode_the_backoff_shift(self, tmp_path):
        """The exponent is attacker-influenced; ``2 ** n`` must stay bounded."""
        log = _seed_log(tmp_path)
        _plant_raw_meta(log, KEY, '"consolidation_attempts": 100000000')

        # Offloaded rather than wrapped in ``allow_on_loop_persist``: this write ends
        # in ``replace_with_retry``, which by design refuses to retry a Windows
        # sharing violation while ON the event loop, because retrying sleeps. A
        # concurrent reader holding the destination is routine there, so an on-loop
        # persist turns an ordinary contended rename into a hard failure -- this line
        # is what Windows CI failed on. Production offloads this write, and
        # ``on_event_loop`` states the rule: a caller earns the retry by offloading,
        # not by declaring. Doing the same here tests the path production runs.
        attempts, retry_at = await asyncio.to_thread(
            log.record_consolidation_failure,
            KEY,
            _CONSOLIDATION_BACKOFF_BASE_SECS,
            86400.0,
            _span(log),
        )

        assert attempts == 100000001
        # flake-ok: pre-existing 5 s wall-clock window on a one-day backoff, moved verbatim
        assert retry_at == pytest.approx(time.time() + 86400.0, abs=5)


class TestAContendedRenameNeedsTheWriteOffTheLoop:
    """The metadata write ends in a rename Windows can refuse; offloading retries it.

    ``_update_metadata_locked`` finishes on ``replace_with_retry``, which absorbs the
    ``PermissionError`` a concurrent transcript READER causes on Windows -- but only
    off the event loop, because retrying sleeps and sleeping on the loop would stall
    every other session. Production earns the retry by offloading every session
    mutator, exactly as ``on_event_loop`` documents. A test that instead drives the
    mutator ON the loop under ``allow_on_loop_persist`` opts out of the retry, so one
    routine contended rename fails it outright -- which is what Windows CI hit here.
    """

    @pytest.fixture
    def _windows(self, monkeypatch):
        """A Windows rename gate, with the bounded backoff made instant."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)

    @pytest.mark.asyncio
    async def test_offloaded_the_contended_write_still_lands(self, tmp_path, _windows):
        """One faulted rename, then one that succeeds: the charge survives."""
        log = _seed_log(tmp_path)
        span = _span(log)

        # Entered AFTER seeding so the simulator faults the write under test rather
        # than the fixture's own appends.
        with replace_sharing_violation(match="-retry.jsonl", times=1) as state:
            attempts, _retry_at = await asyncio.to_thread(
                log.record_consolidation_failure,
                KEY,
                _CONSOLIDATION_BACKOFF_BASE_SECS,
                86400.0,
                span,
            )

        assert attempts == 1
        assert state["n"] == 2, (
            "the simulator must have FAULTED the metadata rename and been retried -- "
            "n < 2 means the retry never ran on the path under test"
        )
        assert log.get_metadata(KEY).get("consolidation_attempts") == 1

    @pytest.mark.asyncio
    async def test_on_the_loop_the_same_contention_is_fatal(self, tmp_path, _windows):
        """Why the sibling offloads: on the loop the retry is refused, by design."""
        log = _seed_log(tmp_path)
        span = _span(log)

        with replace_sharing_violation(match="-retry.jsonl", times=1):
            with pytest.raises(PermissionError):
                with history_mod.allow_on_loop_persist():
                    log.record_consolidation_failure(
                        KEY, _CONSOLIDATION_BACKOFF_BASE_SECS, 86400.0, span
                    )


class TestOnlyASentTurnConsumesTheCap:
    """The cap abandons a span, so only real spend may advance it.

    A pre-dispatch failure (no session manager, kiro-cli missing / not logged in /
    failing to start) costs nothing. Charging it would let a handful of environment
    failures write the durable marker over messages no LLM has ever read — the
    exact false abandonment this accounting exists to prevent.
    """

    @pytest.mark.asyncio
    async def test_memory_binding_failure_arms_durable_backoff_without_spending(self, tmp_path):
        from kiro_crew.memory_stores import UnknownMemoryStore

        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        with (
            patch("kiro_crew.context.store_of_session", side_effect=UnknownMemoryStore("missing")),
            patch.object(c, "_call_llm", AsyncMock()) as call,
            pytest.raises(UnknownMemoryStore, match="missing"),
        ):
            await c._consolidate(KEY, include_history=True)
        call.assert_not_called()
        attempts, retry_at = log.consolidation_retry_state(KEY)
        assert attempts == 0
        assert retry_at > time.time()
        assert log.unconsolidated_count(KEY) == 3
        restarted = _make_consolidator(log)
        assert restarted.retry_eligible(KEY) is False

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
    async def test_repeated_pre_dispatch_failures_never_abandon_the_span(self, tmp_path):
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
        assert (
            log.unconsolidated_count(KEY) == 3
        ), "a broken environment abandoned a span without one billed turn"
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

        # flake-ok: pre-existing 5 s wall-clock window on the backoff, moved verbatim
        assert first == pytest.approx(_CONSOLIDATION_BACKOFF_BASE_SECS, abs=5)
        # flake-ok: pre-existing 5 s wall-clock window on the backoff, moved verbatim
        assert second == pytest.approx(2 * _CONSOLIDATION_BACKOFF_BASE_SECS, abs=5)

    @pytest.mark.asyncio
    async def test_a_post_dispatch_empty_result_still_consumes_an_attempt(self, tmp_path):
        """The other half of the contract: a sent turn is still charged."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)

        assert log.consolidation_retry_state(KEY)[0] == 1

    @pytest.mark.asyncio
    async def test_a_reply_with_no_text_and_no_billing_does_not_consume_an_attempt(self, tmp_path):
        """A throttled provider ends the turn with no text and bills nothing: no model
        read the span, so a charge at the cap would drop it from memory unread."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log, sessions=object())
        abandoned: list[tuple] = []
        c.on_abandoned = lambda *args, **kwargs: abandoned.append(args)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS - 1,
                    "consolidation_retry_at": 0.0,
                },
            )

        provider = _OneTurnProvider("", credits=0.0)
        with patch.object(history_consolidation, "background_turn", _background_turn_on(provider)):
            await c._consolidate(KEY, include_history=True)

        attempts, retry_at = log.consolidation_retry_state(KEY)
        assert attempts == _CONSOLIDATION_MAX_ATTEMPTS - 1
        assert retry_at > time.time()
        assert abandoned == []
        assert log.unconsolidated_count(KEY) == 3

    @pytest.mark.asyncio
    async def test_an_empty_unbilled_reply_from_a_backend_without_usage_is_charged(self, tmp_path):
        """A backend that reports no usage shows none for a billed turn too, so taking its
        empty reply as free would retry a dead span forever and never abandon it."""

        class _UnmeteredProvider(_OneTurnProvider):
            reports_turn_usage = False

        log = _seed_log(tmp_path)
        c = _make_consolidator(log, sessions=object())
        seen: list[tuple[str, int, str]] = []
        c.on_abandoned = lambda key, count, reason, char_count=0, last_slice=False: seen.append(
            (key, count, reason)
        )
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS - 1,
                    "consolidation_retry_at": 0.0,
                },
            )

        provider = _UnmeteredProvider("", credits=0.0)
        with patch.object(history_consolidation, "background_turn", _background_turn_on(provider)):
            await c._consolidate(KEY, include_history=True)

        assert seen == [(KEY, 3, "empty reply")]
        assert log.unconsolidated_count(KEY) == 0

    @pytest.mark.asyncio
    async def test_an_empty_reply_whose_turn_billed_still_consumes_an_attempt(self, tmp_path):
        """Only a turn that billed nothing is free. The billing here is on an attempt a
        transient error abandoned, and the retry's fresh stats show none of it."""
        from kiro_crew.acp.client import AcpError

        class _BilledThenRetried(_OneTurnProvider):
            def __init__(self) -> None:
                super().__init__("", credits=0.0)
                self.calls = 0

            async def stream(self, _message: str, *, allow_image: bool = True):
                self.calls += 1
                if self.calls == 1:
                    self.last_prompt_stats = SimpleNamespace(credits=2.0)
                    raise AcpError("transient error (http 5xx)")
                async for event in super().stream(_message, allow_image=allow_image):
                    yield event

        log = _seed_log(tmp_path)
        c = _make_consolidator(log, sessions=object())
        provider = _BilledThenRetried()
        with (
            patch.object(history_consolidation, "background_turn", _background_turn_on(provider)),
            patch("kiro_crew.llm_helpers.transient_retry_delay", return_value=0),
        ):
            await c._consolidate(KEY, include_history=True)

        assert provider.calls == 2
        assert log.consolidation_retry_state(KEY)[0] == 1

    @pytest.mark.asyncio
    async def test_an_unparsable_reply_is_charged_and_logged_without_its_text(
        self, tmp_path, caplog
    ):
        """The reply can quote the chat being consolidated, credentials included, so the
        WARNING carries its size and shape and no slice of its text, head or tail."""
        reply = '{"history_entry": "' + "the user pasted a key " * 30
        log = _seed_log(tmp_path)
        c = _make_consolidator(log, sessions=object())

        provider = _OneTurnProvider(reply, credits=9.6)
        with (
            patch.object(history_consolidation, "background_turn", _background_turn_on(provider)),
            caplog.at_level(logging.WARNING, logger="kiro_crew.history"),
        ):
            await c._consolidate(KEY, include_history=True)

        assert log.consolidation_retry_state(KEY)[0] == 1
        warnings = "\n".join(r.getMessage() for r in caplog.records)
        assert "model=test-model prompt_chars=" in warnings
        assert f"unparsable reply ({len(reply)} chars, no closing brace)" in warnings
        assert "the user pasted" not in warnings

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
    async def test_recording_a_failure_for_a_deleted_session_is_a_no_op(self, tmp_path):
        """_update_metadata_locked upserts, so a blind write recreates the file."""
        log = _seed_log(tmp_path)
        path = log._path(KEY)
        # The span a turn would have attempted, frozen while the file still
        # exists — the pre-turn snapshot production would be holding here.
        span = _span(log)
        path.unlink()

        with history_mod.allow_on_loop_persist():
            attempts, retry_at = log.record_consolidation_failure(KEY, 900.0, 86400.0, span)

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

        async def _delete_then_fail(_prompt, *, memory_store: str = "", session_key: str = ""):
            path.unlink()
            return None

        with patch.object(c, "_call_llm", AsyncMock(side_effect=_delete_then_fail)):
            await c._consolidate(KEY, include_history=True)

        assert not path.exists()
