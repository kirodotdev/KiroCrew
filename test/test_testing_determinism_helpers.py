"""Self-tests for ``kiro_crew.testing.clock``, ``.wait`` and ``.ids``, and their fixtures.

Every wait-loop test runs ``kiro_crew.testing.wait`` on a :class:`ManualClock`
(through its private ``_monotonic`` / ``_sleep`` / ``_asyncio_sleep`` names), so
none of them bets on how fast the host is. Real time only bounds a broken run.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import os
import random
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from unittest import mock

import pytest

from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.testing import clock as clock_mod
from kiro_crew.testing import ids as ids_mod
from kiro_crew.testing import wait
from kiro_crew.testing.clock import DEFAULT_START, ManualClock

pytest_plugins = ("pytester",)

#: Bound on joining a helper thread the test started. Only a thread that never
#: finishes reaches it; the assertion after the join then names it.
_JOIN_CEILING_SECS = 30.0

_PLUS_14 = dt.timezone(dt.timedelta(hours=14))
_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def virtual(monkeypatch):
    """A ManualClock that ``kiro_crew.testing.wait`` reads instead of real time."""
    clock = ManualClock(monotonic_start=0.0)
    real_asyncio_sleep = asyncio.sleep

    async def virtual_asyncio_sleep(seconds: float) -> None:
        clock.sleep(seconds)
        await real_asyncio_sleep(0)

    monkeypatch.setattr(wait, "_monotonic", clock.monotonic)
    monkeypatch.setattr(wait, "_sleep", clock.sleep)
    monkeypatch.setattr(wait, "_asyncio_sleep", virtual_asyncio_sleep)
    return clock


def _root_conftest(request):
    """The live rootdir ``conftest.py`` plugin, whose helpers these tests drive."""
    return request.config.pluginmanager.get_plugin(str(_REPO_ROOT / "conftest.py"))


def _subject_module() -> types.ModuleType:
    """A module binding the clock names a product module binds, plus an unrelated value."""
    subject = types.ModuleType("determinism_probe_subject")
    subject.time = time
    subject.t2 = time
    subject.mono = time.monotonic
    subject.sleep = time.sleep
    subject.datetime = dt.datetime
    subject.dtm = dt
    subject.date = dt.date
    subject.unrelated = 5
    return subject


# ── ManualClock ───────────────────────────────────────────────────────


class TestManualClock:
    def test_starts_at_the_documented_wall_instant(self):
        clock = ManualClock()
        assert clock.time() == DEFAULT_START == 1_700_000_000.0
        assert clock.now() == dt.datetime(2023, 11, 14, 22, 13, 20, tzinfo=dt.timezone.utc)
        assert clock.now().tzinfo is dt.timezone.utc

    def test_monotonic_starts_at_the_real_reading_unless_given(self):
        before = time.monotonic()
        clock = ManualClock()
        after = time.monotonic()
        assert before <= clock.monotonic() <= after
        assert ManualClock(monotonic_start=42.5).monotonic() == 42.5

    def test_ten_tenth_second_steps_land_exactly_one_second_later(self):
        clock = ManualClock(monotonic_start=0.0)
        for _ in range(10):
            clock.advance(0.1)
        assert clock.time_ns() == 1_700_000_001 * 10**9
        assert clock.monotonic_ns() == 10**9

    def test_advance_moves_both_clocks_and_returns_the_new_monotonic(self):
        clock = ManualClock(monotonic_start=100.0)
        assert clock.advance(2.5) == 102.5
        assert clock.monotonic() == 102.5
        assert clock.time() == DEFAULT_START + 2.5

    def test_perf_counter_is_the_monotonic_clock(self):
        clock = ManualClock(monotonic_start=7.0)
        assert clock.perf_counter() == clock.monotonic() == 7.0
        assert clock.perf_counter_ns() == clock.monotonic_ns() == 7 * 10**9
        assert clock.time_ns() == round(clock.time() * 10**9)

    @pytest.mark.parametrize("bad", [-1, float("nan"), float("inf")])
    def test_advance_and_sleep_refuse_a_negative_or_non_finite_step(self, bad):
        clock = ManualClock()
        with pytest.raises(ValueError):
            clock.advance(bad)
        with pytest.raises(ValueError):
            clock.sleep(bad)
        assert clock.sleeps == ()

    def test_advance_refuses_a_non_number(self):
        with pytest.raises(TypeError):
            ManualClock().advance("1")  # type: ignore[arg-type]

    def test_tick_advances_every_reader_after_each_read(self):
        clock = ManualClock(monotonic_start=0.0, tick=1.0)
        assert [clock.monotonic(), clock.monotonic(), clock.time()] == [0.0, 1.0, DEFAULT_START + 2]
        assert clock.perf_counter() == 3.0
        assert clock.now() == dt.datetime(2023, 11, 14, 22, 13, 24, tzinfo=dt.timezone.utc)

    def test_now_is_aware_in_the_clock_zone_or_the_given_one(self):
        clock = ManualClock(tz=_PLUS_14)
        assert clock.now() == dt.datetime(2023, 11, 15, 12, 13, 20, tzinfo=_PLUS_14)
        assert clock.now().utcoffset() == dt.timedelta(hours=14)
        assert clock.now(dt.timezone.utc).utcoffset() == dt.timedelta(0)

    def test_concurrent_advances_all_land(self):
        clock = ManualClock(monotonic_start=0.0)

        def worker() -> None:
            for _ in range(500):
                clock.advance(0.001)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(_JOIN_CEILING_SECS)
        assert not any(thread.is_alive() for thread in threads)
        assert clock.monotonic_ns() == 8 * 500 * 1_000_000

    def test_every_read_and_step_holds_the_lock(self, monkeypatch):
        # A lost update between threads is probabilistic, so the threaded test above
        # is a smoke test; this one fails deterministically if a path skips the lock.
        clock = ManualClock()
        entered = []

        class Recording:
            def __enter__(self):
                entered.append(1)

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(clock, "_lock", Recording())
        for step in (clock.time, clock.monotonic, clock.now, lambda: clock.advance(1)):
            before = len(entered)
            step()
            assert len(entered) == before + 1, step
        clock.sleep(1)
        assert len(entered) == 5

    def test_sleep_records_and_advances_without_blocking(self):
        clock = ManualClock(monotonic_start=0.0)
        # A daemon, so a sleep that regressed into a real one fails by name below
        # instead of holding the interpreter open at exit.
        sleeper = threading.Thread(target=clock.sleep, args=(3600,), daemon=True)
        sleeper.start()
        sleeper.join(_JOIN_CEILING_SECS)
        assert not sleeper.is_alive(), "ManualClock.sleep blocked"
        assert clock.monotonic() == 3600.0
        assert clock.sleeps == (3600.0,)

    def test_sleep_yields_through_the_real_sleep_captured_at_import(self, monkeypatch):
        calls = []
        monkeypatch.setattr(clock_mod, "_REAL_SLEEP", calls.append)
        ManualClock().sleep(5)
        assert calls == [0]

    def test_construction_refuses_bad_arguments(self):
        with pytest.raises(ValueError):
            ManualClock(float("nan"))
        with pytest.raises(ValueError):
            ManualClock(monotonic_start=float("inf"))
        with pytest.raises(ValueError):
            ManualClock(tick=-1)
        with pytest.raises(TypeError):
            ManualClock(tz="UTC")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="below one nanosecond"):
            ManualClock(tick=1e-10)


class TestTheTimeStandIn:
    def test_every_name_that_does_not_read_the_clock_is_the_real_object(self):
        stand_in = ManualClock().as_time_module()
        overridden = set(clock_mod._CLOCK_READERS) | set(clock_mod._IMPLICIT_NOW_READERS)
        overridden |= {"clock_gettime", "clock_gettime_ns"}
        for name in dir(time):
            if name.startswith("__") or name in overridden:
                continue
            assert getattr(stand_in, name) is getattr(time, name), name

    def test_clock_readers_answer_from_the_clock(self):
        clock = ManualClock(monotonic_start=5.0)
        stand_in = clock.as_time_module()
        assert stand_in.time() == DEFAULT_START
        assert stand_in.monotonic() == stand_in.perf_counter() == 5.0
        stand_in.sleep(2)
        assert clock.sleeps == (2,)
        assert stand_in.monotonic_ns() == 7 * 10**9

    def test_hasattr_matches_the_real_module_in_both_directions(self):
        stand_in = ManualClock().as_time_module()
        public = {name for name in dir(time) if not name.startswith("_")}
        assert {name for name in public if hasattr(stand_in, name)} == public
        assert {n for n in dir(stand_in) if not n.startswith("_")} == public

    def test_a_platform_without_clock_gettime_gets_none_either(self):
        fake_real = types.ModuleType("time")
        for name in dir(time):
            if not name.startswith("clock_get") and not name.startswith("__"):
                setattr(fake_real, name, getattr(time, name))
        stand_in = clock_mod._ClockedTimeModule(ManualClock(), real=fake_real)
        assert not hasattr(stand_in, "clock_gettime")
        assert not hasattr(stand_in, "clock_gettime_ns")

    def test_implicit_now_readers_use_the_clock_and_explicit_ones_delegate(self):
        stand_in = ManualClock().as_time_module()
        assert stand_in.gmtime().tm_year == 2023
        assert stand_in.gmtime(0) == time.gmtime(0)
        assert stand_in.localtime().tm_year == 2023
        assert stand_in.ctime(0) == time.ctime(0)
        assert "2023" in stand_in.ctime()
        assert stand_in.strftime("%Y") == "2023"
        assert stand_in.strftime("%Y", time.gmtime(0)) == "1970"
        assert "2023" in stand_in.asctime()

    @pytest.mark.skipif(not hasattr(time, "clock_gettime"), reason="no clock_gettime here")
    def test_clock_gettime_maps_wall_and_monotonic_ids_only(self):
        # Far above any thread's CPU time, so mapping every id to monotonic would red.
        clock = ManualClock(monotonic_start=1_000_000.0)
        stand_in = clock.as_time_module()
        assert stand_in.clock_gettime(time.CLOCK_REALTIME) == DEFAULT_START
        assert stand_in.clock_gettime(time.CLOCK_MONOTONIC) == 1_000_000.0
        assert stand_in.clock_gettime_ns(time.CLOCK_MONOTONIC) == 10**15
        assert stand_in.clock_gettime_ns(time.CLOCK_REALTIME) == 1_700_000_000 * 10**9
        assert stand_in.clock_gettime(time.CLOCK_THREAD_CPUTIME_ID) < 100_000
        assert stand_in.clock_gettime_ns(time.CLOCK_THREAD_CPUTIME_ID) < 100_000 * 10**9

    def test_the_macos_uptime_clock_is_monotonic(self):
        fake_real = types.ModuleType("time")
        fake_real.CLOCK_UPTIME_RAW = 8
        fake_real.clock_gettime = lambda _clk: -1.0
        fake_real.clock_gettime_ns = lambda _clk: -1
        clock = ManualClock(monotonic_start=6.0)
        stand_in = clock_mod._ClockedTimeModule(clock, real=fake_real)
        assert stand_in.clock_gettime(8) == 6.0
        assert stand_in.clock_gettime_ns(8) == 6 * 10**9

    def test_zone_attributes_follow_the_real_module(self):
        real = types.ModuleType("time")
        for name in dir(time):
            if not name.startswith("__"):
                setattr(real, name, getattr(time, name))
        stand_in = clock_mod._ClockedTimeModule(ManualClock(), real=real)
        real.timezone = -14 * 3600  # what a later tzset() does to the real module
        real.tzname = ("LINT", "LINT")
        assert stand_in.timezone == -14 * 3600
        assert stand_in.tzname == ("LINT", "LINT")

    def test_the_stand_in_is_cached_and_is_a_module(self):
        clock = ManualClock()
        assert clock.as_time_module() is clock.as_time_module()
        assert inspect.ismodule(clock.as_time_module())
        with pytest.raises(AttributeError):
            clock.as_time_module().no_such_name  # noqa: B018


class TestTheDatetimeStandIn:
    def test_a_naive_reading_is_local_time_like_the_stdlib(self):
        clocked = ManualClock(tz=_PLUS_14).as_datetime_class()
        # The local mirror of the frozen instant, read through the same zone rules.
        assert clocked.now() == dt.datetime.fromtimestamp(DEFAULT_START)
        assert clocked.now().tzinfo is None
        assert clocked.now().timestamp() == DEFAULT_START
        assert clocked.today() == clocked.now()

    @pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset is POSIX-only")
    def test_a_naive_reading_follows_a_pinned_zone(self, local_tz):
        local_tz("Pacific/Kiritimati")
        clocked = ManualClock().as_datetime_class()
        assert clocked.now() == dt.datetime(2023, 11, 15, 12, 13, 20)
        assert ManualClock().as_date_class().today() == dt.date(2023, 11, 15)

    @pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset is POSIX-only")
    def test_a_naive_reading_in_the_repeated_hour_keeps_its_fold(self, local_tz):
        import zoneinfo

        local_tz("America/St_Johns")
        zone = zoneinfo.ZoneInfo("America/St_Johns")
        # 01:30 local on the night the clocks fall back, the SECOND time it happens.
        instant = dt.datetime(2023, 11, 5, 1, 30, fold=1, tzinfo=zone).timestamp()
        clock = ManualClock(instant)
        reading = clock.as_datetime_class().now()
        assert (reading.hour, reading.minute, reading.fold) == (1, 30, 1)
        assert reading.timestamp() == clock.time() == instant

    def test_an_aware_reading_is_in_the_zone_asked_for(self):
        clocked = ManualClock(tz=_PLUS_14).as_datetime_class()
        assert clocked.now(dt.timezone.utc) == dt.datetime(
            2023, 11, 14, 22, 13, 20, tzinfo=dt.timezone.utc
        )
        assert clocked.now(_PLUS_14) == dt.datetime(2023, 11, 15, 12, 13, 20, tzinfo=_PLUS_14)

    def test_utcnow_is_virtual_and_warns_like_the_stdlib(self):
        clocked = ManualClock(tz=_PLUS_14).as_datetime_class()
        with pytest.warns(DeprecationWarning, match="utcnow"):
            assert clocked.utcnow() == dt.datetime(2023, 11, 14, 22, 13, 20)

    def test_fromtimestamp_is_the_stdlibs(self):
        clocked = ManualClock(tz=_PLUS_14).as_datetime_class()
        assert clocked.fromtimestamp(0) == dt.datetime.fromtimestamp(0)
        assert clocked.fromtimestamp(timestamp=0, tz=dt.timezone.utc) == dt.datetime(
            1970, 1, 1, tzinfo=dt.timezone.utc
        )
        assert type(clocked.fromtimestamp(0)) is dt.datetime

    def test_values_are_plain_datetimes(self):
        clocked = ManualClock().as_datetime_class()
        values = [
            clocked(2026, 1, 2, 3, 4, 5),
            clocked.now(),
            clocked.fromisoformat("2026-01-02T03:04:05"),
            clocked.strptime("2026", "%Y"),
            clocked.now() + dt.timedelta(days=1),
        ]
        # A plain type is what lets a value cross a process boundary: an instance of a
        # class defined inside a function cannot be serialised by name.
        assert all(type(value) is dt.datetime for value in values)

    def test_isinstance_against_the_stand_in_accepts_real_values(self):
        clocked = ManualClock().as_datetime_class()
        assert isinstance(dt.datetime(2026, 1, 1), clocked)
        assert issubclass(dt.datetime, clocked)
        assert not isinstance(dt.date(2026, 1, 1), clocked)

    def test_date_today_is_the_local_date_of_the_instant(self):
        clocked = ManualClock(tz=_PLUS_14).as_date_class()
        assert type(clocked(2026, 1, 2)) is dt.date
        assert clocked.today() == dt.datetime.fromtimestamp(DEFAULT_START).date()
        assert type(clocked.today()) is dt.date
        assert clocked.fromtimestamp(0) == dt.date.fromtimestamp(0)
        assert isinstance(dt.date(2026, 1, 1), clocked)


class TestInstall:
    def test_patches_every_clock_binding_and_only_those(self, monkeypatch):
        clock = ManualClock(monotonic_start=3.0)
        subject = _subject_module()
        names = clock.install(monkeypatch, subject, datetime=True)
        assert names == ("date", "datetime", "dtm", "mono", "sleep", "t2", "time")
        assert subject.time.monotonic() == subject.t2.monotonic() == subject.mono() == 3.0
        subject.sleep(1)
        assert clock.sleeps == (1,)
        assert subject.datetime.now() == dt.datetime.fromtimestamp(DEFAULT_START + 1)
        assert subject.dtm.datetime.now() == subject.datetime.now()
        assert subject.dtm.timedelta is dt.timedelta
        assert subject.date.today() == dt.datetime.fromtimestamp(DEFAULT_START + 1).date()
        assert subject.unrelated == 5

    def test_the_stdlib_and_the_event_loop_keep_real_time(self, monkeypatch):
        import asyncio.base_events

        real_monotonic = time.monotonic
        real_now = dt.datetime.__dict__["now"]
        ManualClock().install(monkeypatch, _subject_module(), datetime=True)
        assert sys.modules["time"] is time
        assert time.monotonic is real_monotonic
        assert dt.datetime.__dict__["now"] is real_now
        assert asyncio.base_events.time is time

    def test_undo_restores_every_binding(self):
        subject = _subject_module()
        before = dict(vars(subject))
        with pytest.MonkeyPatch.context() as mp:
            ManualClock().install(mp, subject, datetime=True)
            assert subject.time is not time
        assert vars(subject) == before

    @pytest.mark.parametrize("target", ["time", "asyncio.base_events", "_pytest.monkeypatch"])
    def test_refuses_the_stdlib_and_the_runner(self, monkeypatch, target):
        module = __import__(target, fromlist=["_"])
        with pytest.raises(ValueError, match="refusing"):
            ManualClock().install(monkeypatch, module)

    def test_refuses_the_wait_helpers_own_module(self, monkeypatch):
        with pytest.raises(ValueError, match="real time"):
            ManualClock().install(monkeypatch, wait)

    def test_a_module_without_a_clock_binding_is_an_error(self, monkeypatch):
        bare = types.ModuleType("determinism_probe_bare")
        bare.value = 1
        with pytest.raises(ValueError, match="binds no time clock"):
            ManualClock().install(monkeypatch, bare)
        bare.time = time
        with pytest.raises(ValueError, match="binds no datetime clock"):
            ManualClock().install(monkeypatch, bare, time=False, datetime=True)
        with pytest.raises(ValueError, match="needs time=True"):
            ManualClock().install(monkeypatch, bare, time=False)

    def test_a_second_install_of_the_same_clock_is_a_no_op(self, monkeypatch):
        clock = ManualClock()
        subject = _subject_module()
        clock.install(monkeypatch, subject, datetime=True)
        assert clock.install(monkeypatch, subject, datetime=True) == ()

    @pytest.mark.skipif(not hasattr(time, "clock_gettime"), reason="no clock_gettime here")
    def test_a_direct_clock_gettime_binding_is_installed(self, monkeypatch):
        clock = ManualClock(monotonic_start=4.0)
        subject = types.ModuleType("determinism_probe_gettime")
        subject.gettime = time.clock_gettime
        subject.gettime_ns = time.clock_gettime_ns
        assert clock.install(monkeypatch, subject) == ("gettime", "gettime_ns")
        assert subject.gettime(time.CLOCK_MONOTONIC) == 4.0
        assert clock.install(monkeypatch, subject) == ()

    def test_an_installed_implicit_now_reader_is_recognised(self, monkeypatch):
        clock = ManualClock()
        subject = types.ModuleType("determinism_probe_gmtime")
        subject.gm = time.gmtime
        assert clock.install(monkeypatch, subject) == ("gm",)
        assert subject.gm().tm_year == 2023
        assert clock.install(monkeypatch, subject) == ()
        with pytest.raises(ValueError, match="another ManualClock"):
            ManualClock().install(monkeypatch, subject)

    def test_a_second_clock_on_the_same_module_is_refused(self, monkeypatch):
        subject = _subject_module()
        ManualClock().install(monkeypatch, subject)
        with pytest.raises(ValueError, match="another ManualClock"):
            ManualClock().install(monkeypatch, subject)

    def test_a_mock_attribute_is_not_mistaken_for_a_clock(self, monkeypatch):
        subject = _subject_module()
        subject.collaborator = mock.MagicMock()
        assert "time" in ManualClock().install(monkeypatch, subject)
        assert isinstance(subject.collaborator, mock.MagicMock)

    def test_datetime_false_leaves_datetime_bindings_alone(self, monkeypatch):
        subject = _subject_module()
        ManualClock().install(monkeypatch, subject)
        assert subject.datetime is dt.datetime
        assert subject.dtm is dt

    def test_a_non_module_is_a_type_error(self, monkeypatch):
        with pytest.raises(TypeError):
            ManualClock().install(monkeypatch, object())  # type: ignore[arg-type]


# ── wait ──────────────────────────────────────────────────────────────


class TestDefaultTimeout:
    @pytest.mark.parametrize("published", [None, 0, -5, float("nan")])
    def test_falls_back_without_a_usable_runner_timeout(self, monkeypatch, published):
        monkeypatch.setattr(wait, "runner_timeout_secs", published)
        assert wait.default_timeout() == wait.FALLBACK_TIMEOUT_SECS == 60.0

    def test_is_half_the_runner_timeout(self, monkeypatch):
        monkeypatch.setattr(wait, "runner_timeout_secs", 8.0)
        assert wait.default_timeout() == 4.0

    def test_the_runner_publishes_this_tests_timeout(self, request):
        configured = request.config.getoption("timeout", None)
        if configured is None:
            pytest.skip("pytest-timeout is not active in this run")
        expected = float(configured) if float(configured) > 0 else None
        assert wait.runner_timeout_secs == expected

    def test_the_runner_puts_back_clocks_a_first_import_captured_patched(
        self, request, monkeypatch
    ):
        # What a test that patched the stdlib clock and then imported wait first
        # leaves behind: the module's captured clock is the fake.
        conftest = _root_conftest(request)
        monkeypatch.setattr(wait, "_monotonic", lambda: 0.0)
        monkeypatch.setattr(wait, "_sleep", lambda _s: None)
        conftest._publish_runner_timeout(5.0)
        assert wait._monotonic is time.monotonic
        assert wait._sleep is time.sleep
        assert wait._asyncio_sleep is asyncio.sleep

    def test_the_runner_imports_wait_for_a_test_that_imports_it_late(self, request, monkeypatch):
        conftest = _root_conftest(request)
        stand_in = types.SimpleNamespace(runner_timeout_secs=None)
        imported = []

        def import_module(name):
            imported.append(name)
            return stand_in

        monkeypatch.delitem(sys.modules, "kiro_crew.testing.wait")
        monkeypatch.setattr(
            conftest, "importlib", types.SimpleNamespace(import_module=import_module)
        )
        conftest._publish_runner_timeout(7.0)
        assert imported == ["kiro_crew.testing.wait"]
        assert stand_in.runner_timeout_secs == 7.0

    @pytest.mark.timeout(37)
    def test_a_timeout_mark_wins_over_the_command_line(self):
        assert wait.runner_timeout_secs == 37.0
        assert wait.default_timeout() == 18.5


class TestWaitUntil:
    def test_returns_the_truthy_value(self, virtual):
        values = iter([0, "", None, "ready"])
        assert wait.wait_until(lambda: next(values), timeout=10) == "ready"

    def test_a_timeout_raises_with_the_described_state(self, virtual):
        with pytest.raises(AssertionError) as excinfo:
            wait.wait_until(lambda: False, timeout=1, interval=0.3, describe=lambda: "rows=[]")
        message = str(excinfo.value)
        assert "after 1.00s" in message
        assert "5 polls" in message
        assert "last state: rows=[]" in message

    def test_never_oversleeps_the_deadline(self, virtual):
        with pytest.raises(AssertionError):
            wait.wait_until(lambda: False, timeout=1, interval=0.3)
        assert virtual.sleeps[:3] == (0.3, 0.3, 0.3)
        assert virtual.sleeps[3] == pytest.approx(0.1)
        assert len(virtual.sleeps) == 4

    def test_a_condition_true_exactly_at_the_deadline_passes(self, virtual):
        deadline = virtual.monotonic() + 1.0
        assert wait.wait_until(lambda: virtual.monotonic() >= deadline, timeout=1, interval=0.3)

    def test_a_zero_timeout_reads_once(self, virtual):
        calls = []
        with pytest.raises(AssertionError, match="1 polls"):
            wait.wait_until(lambda: calls.append(1), timeout=0)
        assert calls == [1]

    def test_an_ignored_error_counts_as_not_yet(self, virtual):
        attempts = iter([FileNotFoundError("a"), FileNotFoundError("b"), "found"])

        def read():
            item = next(attempts)
            if isinstance(item, Exception):
                raise item
            return item

        assert wait.wait_until(read, timeout=5, ignoring=(FileNotFoundError,)) == "found"

    def test_the_last_ignored_error_is_the_cause(self, virtual):
        def read():
            raise FileNotFoundError("still missing")

        with pytest.raises(AssertionError) as excinfo:
            wait.wait_until(read, timeout=1, ignoring=(FileNotFoundError,))
        assert isinstance(excinfo.value.__cause__, FileNotFoundError)

    def test_an_unlisted_error_propagates_at_once(self, virtual):
        calls = []

        def read():
            calls.append(1)
            raise KeyError("boom")

        with pytest.raises(KeyError):
            wait.wait_until(read, timeout=5)
        assert calls == [1]

    def test_a_cancellation_cannot_be_ignored(self):
        with pytest.raises(TypeError):
            wait.wait_until(lambda: True, ignoring=(asyncio.CancelledError,))  # type: ignore[arg-type]

    def test_a_raising_describe_does_not_mask_the_timeout(self, virtual):
        def describe():
            raise RuntimeError("no state")

        with pytest.raises(AssertionError, match="describe\\(\\) raised RuntimeError"):
            wait.wait_until(lambda: False, timeout=0, describe=describe)

    def test_is_refused_on_a_thread_running_a_loop(self):
        async def main():
            wait.wait_until(lambda: True)

        with pytest.raises(RuntimeError, match="async_wait_until"):
            asyncio.run(main())

    @pytest.mark.parametrize("kwargs", [{"timeout": -1}, {"interval": 0}, {"interval": -1}])
    def test_refuses_a_bad_bound(self, kwargs):
        with pytest.raises(ValueError):
            wait.wait_until(lambda: True, **kwargs)

    def test_refuses_a_bound_that_is_not_a_number(self):
        with pytest.raises(TypeError):
            wait.wait_until(lambda: True, timeout="5")  # type: ignore[arg-type]

    def test_a_predicate_that_returns_an_awaitable_is_refused(self, virtual):
        async def later():
            return False

        with pytest.raises(TypeError, match="returned an awaitable"):
            wait.wait_until(lambda: later(), timeout=5)
        with pytest.raises(TypeError, match="coroutine function"):
            wait.wait_until(later, timeout=5)

    def test_the_default_deadline_is_the_runner_half(self, virtual, monkeypatch):
        monkeypatch.setattr(wait, "runner_timeout_secs", 8.0)
        with pytest.raises(AssertionError, match="timeout 4.00s"):
            wait.wait_until(lambda: False)


class TestAsyncWaitUntil:
    @pytest.mark.asyncio
    async def test_awaits_a_coroutine_predicate(self, virtual):
        calls = []

        async def ready():
            calls.append(1)
            return len(calls) >= 3

        assert await wait.async_wait_until(ready, timeout=5) is True

    @pytest.mark.asyncio
    async def test_off_loop_runs_the_predicate_on_another_thread(self):
        loop_thread = threading.get_ident()
        seen = []

        def read():
            seen.append(threading.get_ident())
            return True

        assert await wait.async_wait_until(read, off_loop=True, timeout=_JOIN_CEILING_SECS)
        assert seen and seen[0] != loop_thread

    @pytest.mark.asyncio
    async def test_an_off_loop_predicate_still_running_fails_by_name(self):
        release = threading.Event()

        def blocked():
            release.wait(_JOIN_CEILING_SECS)
            return True

        try:
            with pytest.raises(AssertionError, match="still running"):
                await wait.async_wait_until(blocked, off_loop=True, timeout=0.05, interval=0.05)
        finally:
            release.set()

    @pytest.mark.asyncio
    async def test_an_on_loop_timeout_names_the_state(self, virtual):
        with pytest.raises(AssertionError, match="after 2.00s.*last state: waiters=0"):
            await wait.async_wait_until(
                lambda: False, timeout=2, interval=0.5, describe=lambda: "waiters=0"
            )
        assert sum(virtual.sleeps) == pytest.approx(2.0)

    @pytest.mark.asyncio
    async def test_an_ignored_error_counts_as_not_yet(self, virtual):
        attempts = iter([LookupError("a"), True])

        def read():
            item = next(attempts)
            if isinstance(item, Exception):
                raise item
            return item

        assert await wait.async_wait_until(read, timeout=5, ignoring=(LookupError,)) is True

    @pytest.mark.asyncio
    async def test_off_loop_reads_describe_off_the_loop_too(self, virtual):
        loop_thread = threading.get_ident()
        described = []

        def describe():
            described.append(threading.get_ident())
            return "state"

        # Virtual time decides the deadline; every off-loop read still gets real
        # seconds (a quarter of the 40 s timeout at least), so a slow thread hop
        # cannot turn this into a "still running" failure.
        with pytest.raises(AssertionError, match="last state: state"):
            await wait.async_wait_until(
                lambda: False, off_loop=True, timeout=40, interval=10, describe=describe
            )
        assert described and described[0] != loop_thread

    @pytest.mark.asyncio
    async def test_the_read_at_the_deadline_gets_a_real_bound(self, virtual, monkeypatch):
        bounds = []
        real_bounded = wait._bounded

        async def recording(awaitable, bound, predicate=None):
            bounds.append(bound)
            return await real_bounded(awaitable, bound, predicate)

        async def never():
            return False

        monkeypatch.setattr(wait, "_bounded", recording)
        with pytest.raises(AssertionError):
            await wait.async_wait_until(never, timeout=8, interval=1)
        # Each read gets what is left of the deadline, and the read at the deadline
        # gets a quarter of the timeout rather than one poll interval.
        assert bounds[0] == 8.0
        assert bounds[-1] == 2.0
        assert min(bounds) == 2.0

    @pytest.mark.asyncio
    async def test_a_timeout_error_from_the_predicate_is_its_own_error(self, virtual):
        attempts = iter([TimeoutError("not yet"), True])

        async def read():
            item = next(attempts)
            if isinstance(item, Exception):
                raise item
            return item

        assert await wait.async_wait_until(read, timeout=5, ignoring=(TimeoutError,)) is True

    @pytest.mark.asyncio
    async def test_off_loop_refuses_a_predicate_that_returns_an_awaitable(self):
        async def later():
            return True

        with pytest.raises(TypeError, match="returned an awaitable"):
            await wait.async_wait_until(lambda: later(), off_loop=True, timeout=_JOIN_CEILING_SECS)

    @pytest.mark.asyncio
    async def test_a_describe_still_running_off_the_loop_is_named(self):
        release = threading.Event()

        def describe():
            release.wait(_JOIN_CEILING_SECS)
            return "late"

        try:
            with pytest.raises(AssertionError, match="describe\\(\\) was still running"):
                await wait.async_wait_until(
                    lambda: False, off_loop=True, timeout=0.05, interval=0.05, describe=describe
                )
        finally:
            release.set()

    @pytest.mark.asyncio
    async def test_off_loop_refuses_a_coroutine_function(self):
        async def predicate():
            return True

        with pytest.raises(TypeError):
            await wait.async_wait_until(predicate, off_loop=True)


async def _cancel(*tasks: asyncio.Task) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), _JOIN_CEILING_SECS)


class TestUntilParked:
    @pytest.mark.asyncio
    async def test_counts_a_task_waiting_on_a_held_lock(self):
        lock = asyncio.Lock()
        await lock.acquire()
        contender = asyncio.create_task(lock.acquire())
        try:
            assert await wait.until_parked(lock, timeout=_JOIN_CEILING_SECS) == 1
        finally:
            lock.release()
            await _cancel(contender)

    @pytest.mark.asyncio
    async def test_a_woken_waiter_is_no_longer_parked(self):
        lock = asyncio.Lock()
        await lock.acquire()
        contender = asyncio.create_task(lock.acquire())
        await wait.until_parked(lock, timeout=_JOIN_CEILING_SECS)
        lock.release()  # wakes the contender; its future is done before it resumes
        try:
            with pytest.raises(AssertionError, match="0 waiter"):
                await wait.until_parked(lock, timeout=0)
        finally:
            await _cancel(contender)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["semaphore", "condition", "event", "barrier"])
    async def test_reads_each_supported_primitive(self, kind):
        if kind == "semaphore":
            primitive = asyncio.Semaphore(0)
            waits = [primitive.acquire(), primitive.acquire()]
        elif kind == "condition":
            primitive = asyncio.Condition()

            async def in_wait():
                async with primitive:
                    await primitive.wait()

            waits = [in_wait(), in_wait()]
        elif kind == "event":
            primitive = asyncio.Event()
            waits = [primitive.wait(), primitive.wait()]
        else:
            primitive = asyncio.Barrier(3)
            waits = [primitive.wait(), primitive.wait()]
        tasks = [asyncio.create_task(coro) for coro in waits]
        try:
            assert await wait.until_parked(primitive, count=2, timeout=_JOIN_CEILING_SECS) == 2
        finally:
            await _cancel(*tasks)

    @pytest.mark.asyncio
    async def test_reads_a_loop_bound_lock_without_binding_it(self):
        fresh = LoopBoundLock()
        with pytest.raises(AssertionError):
            await wait.until_parked(fresh, timeout=0)
        assert len(fresh._locks) == 0
        held = LoopBoundLock()
        await held.acquire()
        contender = asyncio.create_task(held.acquire())
        try:
            assert await wait.until_parked(held, timeout=_JOIN_CEILING_SECS) == 1
        finally:
            held.release()
            await _cancel(contender)

    @pytest.mark.asyncio
    async def test_a_python_without_waiters_is_a_loud_type_error(self):
        lock = asyncio.Lock()
        del lock._waiters  # what a future asyncio rename would look like
        with pytest.raises(TypeError, match="no _waiters"):
            await wait.until_parked(lock)

    @pytest.mark.asyncio
    async def test_refuses_an_unsupported_primitive_and_a_bad_count(self):
        with pytest.raises(TypeError, match="Lock"):
            await wait.until_parked(threading.Lock())
        with pytest.raises(ValueError):
            await wait.until_parked(asyncio.Lock(), count=0)

    def test_refuses_a_primitive_bound_to_another_loop(self):
        event = asyncio.Event()

        async def bind():
            waiter = asyncio.create_task(event.wait())
            await asyncio.sleep(0)
            event.set()
            await asyncio.wait_for(waiter, _JOIN_CEILING_SECS)

        asyncio.run(bind())
        with pytest.raises(RuntimeError, match="different loop"):
            asyncio.run(wait.until_parked(event, timeout=0))

    @pytest.mark.parametrize(
        "primitive", [asyncio.Lock, asyncio.Semaphore, asyncio.Event, asyncio.Condition]
    )
    def test_the_stdlib_still_keeps_waiters_where_this_reads_them(self, primitive):
        assert hasattr(primitive(), "_waiters")


class TestDrainedProgress:
    def test_returns_the_final_size_once_done(self, virtual):
        sizes = iter([0, 10, 20, 30])
        answers = iter([False, False, True])
        result = wait.drained_progress(lambda: next(sizes), done=lambda _budget: next(answers))
        assert result == 10

    def test_a_counter_that_never_moves_fails_after_one_window(self, virtual):
        def done(budget: float) -> bool:
            virtual.advance(budget)
            return False

        start = virtual.monotonic()
        with pytest.raises(AssertionError, match="no progress for 10.00s"):
            wait.drained_progress(lambda: 7, done=done)
        assert virtual.monotonic() - start == pytest.approx(10.0)

    def test_a_producer_still_going_at_the_cap_fails(self, virtual):
        size = 0

        def grow() -> int:
            nonlocal size
            size += 1
            return size

        def done(budget: float) -> bool:
            virtual.advance(budget)
            return False

        with pytest.raises(AssertionError, match="30.00s cap"):
            wait.drained_progress(grow, done=done, window=10, cap=30)

    def test_budgets_never_exceed_the_window_or_the_cap(self, virtual):
        budgets: list[float] = []
        size = 0

        def grow() -> int:
            nonlocal size
            size += 1
            return size

        def done(budget: float) -> bool:
            budgets.append(budget)
            virtual.advance(budget)
            return len(budgets) == 3

        wait.drained_progress(grow, done=done, window=10, cap=25)
        assert budgets == pytest.approx([10.0, 10.0, 5.0])

    def test_a_done_that_returns_an_awaitable_is_refused(self, virtual):
        async def done(_budget):
            return True

        with pytest.raises(TypeError, match="coroutine function"):
            wait.drained_progress(lambda: 0, done=done)
        with pytest.raises(TypeError, match="returned an awaitable"):
            wait.drained_progress(lambda: 0, done=lambda budget: done(budget))

    def test_is_refused_on_a_thread_running_a_loop(self):
        async def main():
            wait.drained_progress(lambda: 0, done=lambda _budget: True)

        with pytest.raises(RuntimeError):
            asyncio.run(main())


# ── ids ───────────────────────────────────────────────────────────────


class TestIds:
    def test_string_order_is_creation_order(self):
        gen = ids_mod.seq_ids("job")
        made = [next(gen) for _ in range(12)]
        assert made[0] == "job-0001" and made[-1] == "job-0012"
        assert sorted(made) == made

    @pytest.mark.parametrize("kwargs", [{"width": 0}, {"width": True}, {"start": -1}])
    def test_refuses_a_bad_width_or_start(self, kwargs):
        with pytest.raises(ValueError):
            ids_mod.seq_ids("x", **kwargs)

    def test_is_its_own_iterator(self):
        gen = ids_mod.seq_ids("i")
        assert iter(gen) is gen
        assert [value for value, _ in zip(gen, range(2))] == ["i-0001", "i-0002"]

    def test_an_id_that_no_longer_fits_raises(self):
        gen = ids_mod.seq_ids("x", start=9, width=1)
        assert next(gen) == "x-9"
        with pytest.raises(OverflowError):
            next(gen)

    def test_is_callable_and_unique_across_threads(self):
        gen = ids_mod.seq_ids("t")
        out: list[str] = []
        lock = threading.Lock()

        def worker() -> None:
            mine = [gen() for _ in range(500)]
            with lock:
                out.extend(mine)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(_JOIN_CEILING_SECS)
        assert not any(thread.is_alive() for thread in threads)
        assert len(set(out)) == 4000

    def test_each_id_is_taken_under_the_lock(self, monkeypatch):
        gen = ids_mod.seq_ids("l")
        entered = []

        class Recording:
            def __enter__(self):
                entered.append(1)

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(gen, "_lock", Recording())
        next(gen)
        gen()
        assert len(entered) == 2

    def test_the_seed_is_a_pinned_digest_of_the_node_id(self):
        nodeid = "test/test_testing_determinism_helpers.py::test_rng_for_is_stable"
        assert ids_mod.seed_for(nodeid) == 12701491099757378880

    def test_the_same_node_id_repeats_its_draws(self):
        first = ids_mod.seeded_rng("a::b")
        second = ids_mod.seeded_rng("a::b")
        assert [first.random() for _ in range(5)] == [second.random() for _ in range(5)]
        assert ids_mod.seeded_rng("a::c").random() != ids_mod.seeded_rng("a::b").random()
        assert ids_mod.seeded_rng("a::b", seed=3).random() == random.Random(3).random()

    def test_unallocatable_pids_cannot_be_real_pids(self):
        pids = ids_mod.unallocatable_pids(25)
        assert pids[0] == ids_mod.UNALLOCATABLE_PID == 99_999_999_999
        assert len(set(pids)) == 25
        assert all(pid > 2**32 and pid % 4 == 3 and (pid & 0xFFFFFFFF) % 4 == 3 for pid in pids)

    @pytest.mark.parametrize("n", [0, 26, True])
    def test_unallocatable_pids_refuses_an_out_of_band_count(self, n):
        with pytest.raises(ValueError):
            ids_mod.unallocatable_pids(n)


# ── the rootdir fixtures ──────────────────────────────────────────────


def test_manual_clock_fixture_is_a_fresh_uninstalled_clock(manual_clock):
    assert isinstance(manual_clock, ManualClock)
    assert manual_clock.time() == DEFAULT_START
    assert time.time() != DEFAULT_START


def test_seeded_rng_fixture_is_seeded_from_this_node_id(seeded_rng, request):
    expected = ids_mod.seeded_rng(request.node.nodeid)
    assert [seeded_rng.random() for _ in range(3)] == [expected.random() for _ in range(3)]


@pytest.mark.xdist_group(name="determinism_seed_probe")
def test_seeded_rng_fixture_ignores_the_xdist_group_suffix(seeded_rng, request):
    # --dist loadgroup renames this test's node id to "<id>@determinism_seed_probe" on
    # a worker; the seed must be the one a -n0 run draws.
    plain = request.node.nodeid.removesuffix("@determinism_seed_probe")
    expected = ids_mod.seeded_rng(plain)
    assert [seeded_rng.random() for _ in range(3)] == [expected.random() for _ in range(3)]


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset is POSIX-only")
def test_local_tz_fixture_sets_the_zone_and_restores_it(pytester, request):
    conftest = _root_conftest(request)
    plugin = types.ModuleType("local_tz_plugin")
    plugin.local_tz = conftest.local_tz
    before = (os.environ.get("TZ"), time.timezone, time.tzname)
    pytester.makeini("[pytest]\n")
    pytester.makepyfile("""
        import time

        def test_uses_the_zone(local_tz):
            local_tz("Pacific/Kiritimati")
            assert time.timezone == -14 * 3600

        def test_refuses_an_unknown_zone(local_tz):
            import pytest, zoneinfo
            with pytest.raises(zoneinfo.ZoneInfoNotFoundError):
                local_tz("Not/AZone")
    """)
    result = pytester.runpytest_inprocess("-p", "no:cacheprovider", plugins=[plugin])
    result.assert_outcomes(passed=2)
    assert (os.environ.get("TZ"), time.timezone, time.tzname) == before


def test_the_helper_modules_import_without_pytest(tmp_path):
    code = (
        "import sys\n"
        "sys.modules['pytest'] = None\n"
        "sys.modules['_pytest'] = None\n"
        "import kiro_crew.testing\n"
        "before = set(sys.modules)\n"
        "import kiro_crew.testing.clock, kiro_crew.testing.ids, kiro_crew.testing.wait\n"
        "new = sorted(m for m in set(sys.modules) - before if m.startswith('kiro_crew'))\n"
        "print(','.join(new))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    out = subprocess.run(
        [sys.executable, "-B", "-c", code],
        capture_output=True,
        check=True,
        env=env,
        cwd=tmp_path,
        timeout=_JOIN_CEILING_SECS * 2,
        **UTF8_TEXT,
    )
    loaded = set(out.stdout.strip().split(","))
    helpers = {"kiro_crew.testing.clock", "kiro_crew.testing.ids", "kiro_crew.testing.wait"}
    assert helpers <= loaded <= helpers | {"kiro_crew.loop_lock"}


@pytest.mark.parametrize("name", ["clock", "ids", "wait"])
def test_the_helpers_import_only_the_standard_library(name):
    import ast

    tree = ast.parse(
        (_REPO_ROOT / "src" / "kiro_crew" / "testing" / f"{name}.py").read_text("utf-8")
    )
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(
                node.module
                if node.module.startswith("kiro_crew")
                else node.module.partition(".")[0]
            )
    allowed = set(sys.stdlib_module_names) | {"__future__", "kiro_crew.loop_lock"}
    assert imported <= allowed, sorted(imported - allowed)


# ── the conftest floors ───────────────────────────────────────────────


class _FakeSocket:
    def __init__(self, family, kind):
        self.family = family
        self.type = kind


@pytest.fixture
def audit(request, monkeypatch):
    """A fresh, active audit state the live hook writes to instead of this test's own."""
    conftest = _root_conftest(request)
    state = conftest._AuditState()
    state.active = True
    state.pid = os.getpid()
    state.parent_pid = os.getppid()
    monkeypatch.setattr(conftest, "_AUDIT", state)
    return state


class TestTheAuditFloor:
    def test_off_loopback_traffic_is_recorded(self, audit):
        import socket

        sys.audit(
            "socket.connect", _FakeSocket(socket.AF_INET, socket.SOCK_STREAM), ("192.0.2.1", 80)
        )
        sys.audit(
            "socket.sendto", _FakeSocket(socket.AF_INET, socket.SOCK_DGRAM), ("192.0.2.2", 53)
        )
        sys.audit(
            "socket.connect", _FakeSocket(socket.AF_INET, socket.SOCK_DGRAM), ("192.0.2.3", 9)
        )
        sys.audit("socket.getaddrinfo", "example.invalid", 443, 0, 0, 0, 0)
        assert set(audit.seen) == {
            ("network", "connect 192.0.2.1:80"),
            ("network", "sendto 192.0.2.2:53"),
            ("network", "udp-route-probe 192.0.2.3:9"),
            ("network", "name lookup 'example.invalid'"),
        }

    def test_loopback_unix_and_unspecified_are_not(self, audit):
        import socket

        stream = _FakeSocket(socket.AF_INET, socket.SOCK_STREAM)
        for host in ("127.0.0.1", "127.8.9.1", "localhost", "0.0.0.0"):
            sys.audit("socket.connect", stream, (host, 80))
        v6 = _FakeSocket(socket.AF_INET6, socket.SOCK_STREAM)
        for host in ("::1", "::ffff:127.0.0.1", "::1%lo"):
            sys.audit("socket.connect", v6, (host, 80, 0, 0))
        sys.audit("socket.connect", _FakeSocket(getattr(socket, "AF_UNIX", -1), 0), "/run/x.sock")
        for host in (None, "", "localhost", "127.0.0.1", b"::1"):
            sys.audit("socket.getaddrinfo", host, 80, 0, 0, 0, 0)
        assert audit.seen == {}

    def test_the_own_hostname_is_its_own_class(self, audit):
        import socket

        sys.audit("socket.getaddrinfo", socket.gethostname(), None, 0, 0, 0, 0)
        assert list(audit.seen) == [("network", f"own-hostname lookup {socket.gethostname()!r}")]

    def test_kills_of_the_run_are_recorded_and_probes_are_not(self, audit):
        import signal

        sigterm = int(signal.SIGTERM)
        sys.audit("os.kill", os.getppid(), sigterm)
        sys.audit("os.kill", 0, sigterm)
        if os.name != "nt":
            sys.audit("os.kill", os.getppid(), 0)  # a liveness probe signals nothing
        details = {detail for _floor, detail in audit.seen}
        assert f"signal {sigterm} to the xdist controller (pid {os.getppid()})" in details
        assert any("to pid 0" in detail for detail in details)
        assert len(audit.seen) == 2

    def test_a_signal_the_worker_handles_itself_is_not_recorded(self, audit, monkeypatch):
        import signal

        if not hasattr(signal, "SIGUSR1"):
            pytest.skip("no SIGUSR1 on this platform")
        monkeypatch.setattr(signal, "getsignal", lambda _sig: (lambda *_a: None))
        sys.audit("os.kill", os.getpid(), int(signal.SIGUSR1))
        assert audit.seen == {}

    @pytest.mark.skipif(not os.path.exists("/proc/self/stat"), reason="reads /proc")
    def test_a_kill_of_a_sibling_xdist_worker_is_recorded(self, request, tmp_path):
        # Shape of a sibling worker: a child of the worker's PARENT, outside the
        # worker's own tree. Here this process plays the controller.
        conftest = _root_conftest(request)
        sibling = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE,
            cwd=tmp_path,
        )
        try:
            state = conftest._AuditState()
            state.pid = 99_999_999_999  # a worker the sibling does not descend from
            state.parent_pid = os.getpid()
            assert conftest._another_run_member(sibling.pid, state) == (
                "another child of this process's parent (under xdist, a sibling worker)"
            )
            assert conftest._classify_kill_target(sibling.pid, "signal 15", state) == (
                "kill",
                f"signal 15 to pid {sibling.pid}, another child of this process's parent "
                "(under xdist, a sibling worker)",
            )
        finally:
            sibling.stdin.close()
            sibling.wait(timeout=_JOIN_CEILING_SECS)

    @pytest.mark.skipif(not os.path.exists("/proc/self/stat"), reason="reads /proc")
    def test_this_workers_own_or_gone_children_are_not_recorded(self, request, audit, tmp_path):
        conftest = _root_conftest(request)
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE,
            cwd=tmp_path,
        )
        try:
            sys.audit("os.kill", child.pid, 15)
            assert audit.seen == {}
        finally:
            child.stdin.close()
            child.wait(timeout=_JOIN_CEILING_SECS)
        assert conftest._another_run_member(child.pid, audit) is None  # gone: reached nothing

    def test_taskkill_by_name_or_at_the_worker_is_recorded(self, audit):
        sys.audit(
            "subprocess.Popen", "taskkill", ["taskkill", "/F", "/IM", "python.exe"], None, None
        )
        sys.audit(
            "subprocess.Popen",
            "taskkill.exe",
            ["taskkill.exe", "/PID", str(os.getpid())],
            None,
            None,
        )
        sys.audit("subprocess.Popen", "git", ["git", "status"], None, None)
        assert {detail for _floor, detail in audit.seen} == {
            "taskkill by image name: /F /IM python.exe",
            "taskkill to the worker itself",
        }

    @pytest.mark.skipif(not hasattr(os, "getpgrp"), reason="no process groups here")
    def test_a_killpg_of_the_workers_own_group_is_recorded(self, audit):
        sys.audit("os.killpg", os.getpgrp(), 15)
        assert list(audit.seen) == [
            ("kill", "killpg of the worker's own process group (signal 15)")
        ]

    @pytest.mark.skipif(os.name == "nt", reason="a negative pid is a group only on POSIX")
    def test_a_negative_pid_kill_is_read_as_the_group_signal_it_is(self, audit):
        # kill(-pgid) is killpg(pgid), so a test that signals its child's group is not
        # reported as a broadcast. The group is a real child's own session, never the
        # worker's: a container's getpgrp() can be 0, which has no negative form.
        child = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        try:
            sys.audit("os.kill", -child.pid, 15)
        finally:
            child.wait(timeout=60)
        assert list(audit.seen) == []
        sys.audit("os.kill", -1, 15)  # every process the user may signal: still reported
        assert list(audit.seen) == [
            ("kill", "signal 15 to pid -1 (a process group, possibly the worker's own)")
        ]

    def test_a_strict_switch_set_to_anything_but_1_stays_off(self, request, monkeypatch):
        conftest = _root_conftest(request)
        state = conftest._AuditState()
        monkeypatch.setattr(conftest, "_AUDIT", state)
        monkeypatch.setenv("KIROCREW_NET_STRICT", "0")
        item = types.SimpleNamespace(nodeid="s::t", stash={}, get_closest_marker=lambda _name: None)
        conftest._determinism_floors_begin(item)
        assert state.strict_net is False
        monkeypatch.setenv("KIROCREW_NET_STRICT", "1")
        conftest._determinism_floors_begin(item)
        assert state.strict_net is True
        state.active = False

    def test_a_windows_command_line_taskkill_is_parsed(self, audit):
        sys.audit("subprocess.Popen", "taskkill.exe", "taskkill.exe /F /IM node.exe", None, None)
        assert {detail for _floor, detail in audit.seen} == {
            "taskkill by image name: /F /IM node.exe"
        }

    def test_the_older_resolvers_are_watched_too(self, audit):
        sys.audit("socket.gethostbyname", "example.invalid")
        sys.audit("socket.gethostbyaddr", "192.0.2.9")
        sys.audit("socket.gethostbyname", "localhost")
        sys.audit("socket.gethostbyaddr", "127.0.0.1")
        assert set(audit.seen) == {
            ("network", "name lookup 'example.invalid'"),
            ("network", "reverse lookup '192.0.2.9'"),
        }

    def test_strict_mode_lets_a_route_probe_through(self, audit):
        import socket

        audit.strict_net = True
        sys.audit(
            "socket.connect", _FakeSocket(socket.AF_INET, socket.SOCK_DGRAM), ("192.0.2.3", 9)
        )
        assert list(audit.seen) == [("network", "udp-route-probe 192.0.2.3:9")]

    def test_strict_mode_refuses_the_call(self, request, audit):
        import socket

        conftest = _root_conftest(request)
        audit.strict_net = True
        with pytest.raises(conftest.OffLoopbackNetworkRefused, match="192.0.2.1"):
            sys.audit(
                "socket.connect", _FakeSocket(socket.AF_INET, socket.SOCK_STREAM), ("192.0.2.1", 80)
            )
        audit.network_ok = True
        sys.audit(
            "socket.connect", _FakeSocket(socket.AF_INET, socket.SOCK_STREAM), ("192.0.2.4", 80)
        )

    def test_an_inactive_window_or_a_forked_child_records_nothing(self, audit):
        import socket

        sock = _FakeSocket(socket.AF_INET, socket.SOCK_STREAM)
        audit.active = False
        sys.audit("socket.connect", sock, ("192.0.2.1", 80))
        audit.active = True
        audit.pid = os.getpid() + 1
        sys.audit("socket.connect", sock, ("192.0.2.1", 80))
        assert audit.seen == {}

    def test_end_of_test_reports_and_strict_returns_the_refusal(self, request, audit, monkeypatch):
        conftest = _root_conftest(request)
        findings: list = []
        monkeypatch.setattr(conftest, "_FLOOR_FINDINGS", findings)
        item = types.SimpleNamespace(nodeid="probe::test", stash={})
        audit.seen = {("network", "connect 192.0.2.1:80"): 3}
        audit.strict_net = True
        refusal = conftest._determinism_floors_end(item)
        assert findings == [("network", "probe::test", "connect 192.0.2.1:80 (x3)")]
        assert isinstance(refusal, AssertionError) and "KIROCREW_NET_STRICT" in str(refusal)
        assert audit.active is False

    def test_the_marker_is_registered(self, request):
        assert any(line.startswith("real_network:") for line in request.config.getini("markers"))


class TestTheFloorsAreOn:
    def test_the_audit_hook_is_installed_and_watching_this_test(self, request):
        conftest = _root_conftest(request)
        assert conftest._AUDIT_INSTALLED is True
        assert conftest._AUDIT.active is True
        assert conftest._AUDIT.nodeid == request.node.nodeid
        assert conftest._AUDIT.pid == os.getpid()

    def test_the_floor_fixtures_run_for_every_test(self, request):
        for name in ("_kiro_crew_package_attr_floor", "_report_only_floor_summary"):
            assert name in request.fixturenames, name
        conftest = _root_conftest(request)
        assert request.node.stash.get(conftest._TZ_STATE, None) is not None
        assert request.node.stash.get(conftest._POOL_STATE, None) is not None


class TestTheReportOnlyFloors:
    def test_a_zone_change_is_reported(self, request, monkeypatch):
        conftest = _root_conftest(request)
        findings: list = []
        monkeypatch.setattr(conftest, "_FLOOR_FINDINGS", findings)
        before = ("Pacific/Kiritimati", ("LINT", "LINT"), -50400, -50400, 0)
        conftest._check_zone_drift(
            types.SimpleNamespace(nodeid="z::t", stash={conftest._TZ_STATE: before})
        )
        assert len(findings) == 1 and findings[0][:2] == ("time zone", "z::t")
        findings.clear()
        same = conftest._process_zone()
        conftest._check_zone_drift(
            types.SimpleNamespace(nodeid="z::t", stash={conftest._TZ_STATE: same})
        )
        assert findings == []

    def test_work_left_on_a_shared_pool_is_reported(self, request, monkeypatch):
        from concurrent.futures import ThreadPoolExecutor

        conftest = _root_conftest(request)
        findings: list = []
        monkeypatch.setattr(conftest, "_FLOOR_FINDINGS", findings)
        fake = types.ModuleType("determinism_probe_executors")
        fake._pool = ThreadPoolExecutor(max_workers=1)
        monkeypatch.setitem(sys.modules, "kiro_crew.executors", fake)
        release = threading.Event()
        started = threading.Event()

        def hold():
            started.set()
            release.wait(_JOIN_CEILING_SECS)

        try:
            first = fake._pool.submit(hold)
            assert started.wait(_JOIN_CEILING_SECS)
            fake._pool.submit(lambda: None)
            assert conftest._pool_activity() == {"_pool": (1, 1)}
            item = types.SimpleNamespace(nodeid="p::t", stash={conftest._POOL_STATE: {}})
            conftest._check_pool_leftovers(item)
            assert findings == [
                ("shared pool", "p::t", "left work on executors._pool: 1 running, 1 queued")
            ]
        finally:
            release.set()
            first.result(_JOIN_CEILING_SECS)
            fake._pool.shutdown(wait=True)
        assert conftest._pool_activity() == {}

    def test_the_summary_groups_and_caps_by_floor(self, request, monkeypatch):
        conftest = _root_conftest(request)
        monkeypatch.setattr(conftest, "_FLOOR_SUMMARY_PER_FLOOR", 2)
        text = conftest._floor_summary(
            [("network", f"n::{i}", "connect x") for i in range(3)] + [("kill", "k::0", "y")]
        )
        assert "network: 3 finding(s)" in text and "kill: 1 finding(s)" in text
        assert "n::0: connect x" in text and "n::2" not in text and "and 1 more" in text


class TestThePackageAttributeFloor:
    def _probe(self, monkeypatch, name="_determinism_floor_probe"):
        import kiro_crew

        module = types.ModuleType(f"kiro_crew.{name}")
        monkeypatch.setitem(sys.modules, f"kiro_crew.{name}", module)
        monkeypatch.setattr(kiro_crew, name, module, raising=False)
        return kiro_crew, module

    def test_an_evicted_and_reimported_module_is_put_back(self, request, monkeypatch):
        conftest = _root_conftest(request)
        package, module = self._probe(monkeypatch)
        table = conftest._PackageTable()
        table.rebuild()
        assert table.intact()
        impostor = types.ModuleType(module.__name__)
        monkeypatch.setitem(sys.modules, module.__name__, impostor)
        monkeypatch.setattr(package, "_determinism_floor_probe", impostor)
        assert not table.intact()
        changed = table.restore()
        assert sys.modules[module.__name__] is module
        assert package._determinism_floor_probe is module
        assert "sys.modules['kiro_crew._determinism_floor_probe'] replaced" in changed
        assert "kiro_crew._determinism_floor_probe rebound" in changed
        assert table.intact()

    def test_a_root_package_global_rebound_by_a_reload_is_put_back(self, request, monkeypatch):
        conftest = _root_conftest(request)
        package, _module = self._probe(monkeypatch)
        monkeypatch.setattr(package, "_determinism_floor_value", object(), raising=False)
        table = conftest._PackageTable()
        table.rebuild()
        original = package._determinism_floor_value
        monkeypatch.setattr(package, "_determinism_floor_value", object())
        assert not table.intact()
        assert "kiro_crew._determinism_floor_value rebound" in table.restore()
        assert package._determinism_floor_value is original

    def test_a_loaded_module_its_parent_lost_is_bound_back(self, request, monkeypatch):
        import kiro_crew

        conftest = _root_conftest(request)
        name = "_determinism_floor_unbound"
        module = types.ModuleType(f"kiro_crew.{name}")
        monkeypatch.setitem(sys.modules, module.__name__, module)
        monkeypatch.setattr(kiro_crew, name, None, raising=False)
        monkeypatch.delattr(kiro_crew, name)  # undo leaves the attribute absent
        assert f"kiro_crew.{name} was unbound" in conftest._bind_unbound_kiro_crew_modules()
        assert getattr(kiro_crew, name) is module

    def test_the_agent_state_pin_survives_a_lost_parent_attribute(
        self, request, monkeypatch, tmp_path
    ):
        import kiro_crew
        import kiro_crew.agent_state

        conftest = _root_conftest(request)
        monkeypatch.delattr(kiro_crew, "agent_state")
        with pytest.MonkeyPatch.context() as mp:
            conftest._pin_agent_state_sidecar(mp, tmp_path)
            assert sys.modules["kiro_crew.agent_state"].config_dir() == tmp_path


class TestTheSuiteFloors:
    def test_the_history_sweep_throttle_starts_closed(self):
        import math

        from kiro_crew import history

        assert history._last_cleanup == math.inf

    def test_an_autouse_floor_survives_a_tests_own_undo(self, monkeypatch):
        from kiro_crew.mcp_gateway import launch_approval

        stub = launch_approval.launch_approved
        assert stub("anything") is True
        monkeypatch.undo()  # flake-ok: the subject is how far a test's own undo reaches
        assert launch_approval.launch_approved is stub

    def test_the_ci_hypothesis_profile_is_derandomized_without_a_database(self):
        from hypothesis import settings

        ci = settings.get_profile("ci")
        assert ci.derandomize is True and ci.database is None and ci.print_blob is True
        assert ci.max_examples == settings.get_profile("default").max_examples
        if "CI" in os.environ and not os.environ.get("HYPOTHESIS_PROFILE"):
            assert settings().derandomize is True and settings().database is None

    def test_an_unregistered_mark_is_an_error(self):
        with pytest.raises(pytest.fail.Exception, match="not found in `markers`"):
            pytest.mark.definitely_not_a_registered_mark  # noqa: B018
