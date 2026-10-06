"""A manual clock a test installs on the module the code under test reads.

This is the shape rules D2 and D3 of the Determinism contract in
``docs/system-specs/common/testing-conventions.md`` ask for. One virtual
instant moves only when the test calls :meth:`ManualClock.advance` or
:meth:`ManualClock.sleep`, or by ``tick`` on every read. :meth:`ManualClock.install`
replaces the ``time`` / ``datetime`` names of ONE module through the test's
``monkeypatch``, so the stdlib modules, ``sys.modules``, the asyncio loop
clock and pytest-timeout all keep real time.

Import the class (``from kiro_crew.testing.clock import ManualClock``) rather
than the module: ``clock`` is a common local and fixture name. The module
imports only the standard library.
"""

from __future__ import annotations

import datetime as _dt
import inspect
import math
import sys
import threading
import time as _time
import types
import warnings
from typing import Any, Callable

__all__ = ["DEFAULT_START", "ManualClock"]

#: The wall clock a new :class:`ManualClock` starts at: 2023-11-14T22:13:20Z.
DEFAULT_START = 1_700_000_000.0

_NS_PER_SEC = 1_000_000_000
#: The real ``time.sleep``, taken at import: :meth:`ManualClock.sleep` yields
#: through it, so a test that patches the stdlib sleep does not reach the clock.
_REAL_SLEEP = _time.sleep
_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)

#: The ``time`` functions a clocked stand-in answers from the virtual instant.
_CLOCK_READERS = (
    "time",
    "time_ns",
    "monotonic",
    "monotonic_ns",
    "perf_counter",
    "perf_counter_ns",
    "sleep",
)

#: Readers that mean "now" when called with no argument.
_IMPLICIT_NOW_READERS = ("gmtime", "localtime", "ctime", "asctime", "strftime")

#: ``clock_gettime`` readers, present on POSIX only.
_GETTIME_READERS = ("clock_gettime", "clock_gettime_ns")

#: Zone attributes ``time.tzset()`` rewrites on the real module. The stand-in
#: reads them from the real module on every access instead of copying them.
_ZONE_ATTRS = frozenset({"timezone", "altzone", "daylight", "tzname"})

#: Module bookkeeping a copied module must not inherit.
_MODULE_DUNDERS = frozenset({"__name__", "__doc__", "__spec__", "__loader__", "__package__"})

#: Packages whose clock is the test runner's own: a clock installed there would
#: freeze pytest-timeout, xdist's heartbeat or the wait helpers' deadlines.
_RUNNER_PACKAGES = frozenset(
    {
        "_pytest",
        "pytest",
        "pluggy",
        "pytest_timeout",
        "pytest_asyncio",
        "xdist",
        "execnet",
        "hypothesis",
    }
)


def _to_ns(seconds: float) -> int:
    """Whole nanoseconds in ``seconds``; an ``int`` converts exactly."""
    if isinstance(seconds, int):
        return seconds * _NS_PER_SEC
    return round(seconds * _NS_PER_SEC)


def _check_duration(seconds: float, what: str) -> None:
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        raise TypeError(f"{what} takes a number of seconds, not {type(seconds).__name__}")
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"{what} length must be non-negative and finite, got {seconds!r}")


class _ClockedTimeModule(types.ModuleType):
    """A copy of the ``time`` module whose clock readers answer from one clock.

    Every name the real module has is present, so ``hasattr`` agrees with the
    real module on every platform. Names that do not read the clock
    (``struct_time``, ``strptime``, ``process_time``, ``thread_time``,
    ``tzset``, the ``CLOCK_*`` ids) are the real objects. The zone attributes
    (``timezone``, ``altzone``, ``daylight``, ``tzname``) are read from the real
    module on each access, so they follow a later ``tzset()``; ``vars()`` lists
    every other name.
    """

    def __init__(self, clock: ManualClock, real: types.ModuleType = _time) -> None:
        super().__init__(real.__name__, real.__doc__)
        for name, value in vars(real).items():
            if name not in _MODULE_DUNDERS and name not in _ZONE_ATTRS:
                setattr(self, name, value)
        self._real = real
        self._clock = clock
        for name in _CLOCK_READERS:
            setattr(self, name, getattr(clock, name))
        self._wrap_implicit_now(clock, real)
        if hasattr(real, "clock_gettime"):
            self._wrap_clock_gettime(clock, real)

    def __getattr__(self, name: str) -> Any:
        # Only reached for a name the copy does not hold: the zone attributes.
        if name in _ZONE_ATTRS:
            return getattr(self._real, name)
        raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | (_ZONE_ATTRS & set(dir(self._real))))

    def _wrap_implicit_now(self, clock: ManualClock, real: types.ModuleType) -> None:
        def gmtime(secs: float | None = None) -> Any:
            return real.gmtime(clock.time() if secs is None else secs)

        def localtime(secs: float | None = None) -> Any:
            return real.localtime(clock.time() if secs is None else secs)

        def ctime(secs: float | None = None) -> str:
            return str(real.ctime(clock.time() if secs is None else secs))

        def asctime(t: Any = None) -> str:
            return str(real.asctime(real.localtime(clock.time()) if t is None else t))

        def strftime(fmt: str, t: Any = None) -> str:
            return str(real.strftime(fmt, real.localtime(clock.time()) if t is None else t))

        for fn in (gmtime, localtime, ctime, asctime, strftime):
            _mark(fn, clock)
            setattr(self, fn.__name__, fn)

    def _wrap_clock_gettime(self, clock: ManualClock, real: types.ModuleType) -> None:
        wall = {getattr(real, "CLOCK_REALTIME", None)} - {None}
        mono_names = (
            "CLOCK_MONOTONIC",
            "CLOCK_MONOTONIC_RAW",
            "CLOCK_BOOTTIME",
            "CLOCK_UPTIME_RAW",
        )
        mono = {getattr(real, name, None) for name in mono_names} - {None}

        def clock_gettime(clk_id: int) -> float:
            if clk_id in wall:
                return clock.time()
            if clk_id in mono:
                return clock.monotonic()
            return float(real.clock_gettime(clk_id))

        def clock_gettime_ns(clk_id: int) -> int:
            if clk_id in wall:
                return clock.time_ns()
            if clk_id in mono:
                return clock.monotonic_ns()
            return int(real.clock_gettime_ns(clk_id))

        self.clock_gettime = _mark(clock_gettime, clock)
        self.clock_gettime_ns = _mark(clock_gettime_ns, clock)


def _mark(fn: Callable[..., Any], clock: ManualClock) -> Callable[..., Any]:
    """Tag a stand-in reader with its clock, so ``install`` can recognise it."""
    fn.__dict__["_manual_clock"] = clock
    return fn


class _ClockedMeta(type):
    """Metaclass that makes a clocked class answer ``isinstance`` like the real one.

    The clocked ``datetime`` / ``date`` classes build PLAIN stdlib instances, so
    ``isinstance(value, <the patched name>)`` must accept those: product code
    that checks ``isinstance(v, datetime)`` against its own patched binding keeps
    working.
    """

    _real: type

    def __instancecheck__(cls, instance: object) -> bool:
        return isinstance(instance, cls._real)

    def __subclasscheck__(cls, subclass: type) -> bool:
        return issubclass(subclass, cls._real)


def _local_naive(wall_ns: int) -> _dt.datetime:
    """The naive local time of an instant, as ``datetime.now()`` builds it.

    Through ``fromtimestamp``, which sets ``fold`` in a repeated (fall-back) hour,
    so ``timestamp()`` round-trips to the instant; ``astimezone()`` does not.
    """
    seconds, rest = divmod(wall_ns, _NS_PER_SEC)
    return _dt.datetime.fromtimestamp(seconds).replace(microsecond=rest // 1000)


def _clocked_datetime_class(clock: ManualClock) -> type[_dt.datetime]:
    class datetime(_dt.datetime, metaclass=_ClockedMeta):
        """``datetime.datetime`` whose "now" readers answer from a :class:`ManualClock`.

        Constructors return plain ``datetime.datetime`` instances, so values
        pickle, compare and hash exactly like the real ones: the stand-in
        changes WHEN, never WHAT TYPE. A naive reading means what the stdlib's
        means, local time in the process's zone, so ``naive.timestamp()`` is the
        clock's ``time()``; pin the zone with the ``local_tz`` fixture when the
        test reads one.
        """

        _real = _dt.datetime

        def __new__(cls, *args: Any, **kwargs: Any) -> datetime:
            # A plain instance, not a subclass one: a class defined in a function
            # cannot be pickled, and arithmetic would spread the subclass.
            return _dt.datetime(*args, **kwargs)  # type: ignore[return-value]

        @classmethod
        def now(cls, tz: _dt.tzinfo | None = None) -> datetime:
            if tz is not None:
                return clock.now(tz)  # type: ignore[return-value]
            return _local_naive(clock.time_ns())  # type: ignore[return-value]

        @classmethod
        def today(cls) -> datetime:
            return cls.now()

        @classmethod
        def utcnow(cls) -> datetime:
            warnings.warn(
                "datetime.datetime.utcnow() is deprecated and scheduled for removal in a "
                "future version. Use timezone-aware objects to represent datetimes in UTC: "
                "datetime.datetime.now(datetime.UTC).",
                DeprecationWarning,
                stacklevel=2,
            )
            return clock.now(_dt.timezone.utc).replace(tzinfo=None)  # type: ignore[return-value]

    return datetime


def _clocked_date_class(clock: ManualClock) -> type[_dt.date]:
    class date(_dt.date, metaclass=_ClockedMeta):
        """``datetime.date`` whose ``today()`` answers from a :class:`ManualClock`."""

        _real = _dt.date

        def __new__(cls, *args: Any, **kwargs: Any) -> date:
            return _dt.date(*args, **kwargs)  # type: ignore[return-value]

        @classmethod
        def today(cls) -> date:
            # The local date, as the stdlib's: the process zone decides the day.
            return _local_naive(clock.time_ns()).date()  # type: ignore[return-value]

    return date


class _ClockedDatetimeModule(types.ModuleType):
    """A copy of the ``datetime`` module with its ``datetime`` and ``date`` clocked."""

    def __init__(self, clock: ManualClock, real: types.ModuleType = _dt) -> None:
        super().__init__(real.__name__, real.__doc__)
        for name, value in vars(real).items():
            if name not in _MODULE_DUNDERS:
                setattr(self, name, value)
        self._clock = clock
        self.datetime = clock.as_datetime_class()
        self.date = clock.as_date_class()


class ManualClock:
    """A wall clock and a monotonic clock that move only when the test moves them.

    ``start`` is the wall time in epoch seconds. ``monotonic_start`` is the
    monotonic reading; by default it is the REAL ``time.monotonic()`` at
    construction, because product deadlines are often set on one module's clock
    and read on another's (a deadline computed by a real reader and compared on
    the clocked one must still make sense). ``perf_counter`` is the monotonic
    clock. ``tick`` advances both clocks by that many seconds after every read,
    so ``tick=0`` (the default) is a frozen clock and ``tick>0`` a strictly
    increasing one (in the ``_ns`` readers; a float reading of an epoch time
    resolves to about a quarter of a microsecond, so give a tick at least
    that). ``tz`` is the zone of an aware :meth:`now`; a naive reading from the
    ``datetime`` stand-in is local time in the process's zone, as the stdlib's.

    Time is held as whole nanoseconds under a lock, so ten ``advance(0.1)``
    calls land exactly one second later and threads may read and advance the
    clock concurrently. ``sleep(s)`` records ``s``, advances by it and yields
    the GIL; it never blocks. Concurrent sleepers each add their own duration.
    """

    def __init__(
        self,
        start: float = DEFAULT_START,
        *,
        tz: _dt.tzinfo = _dt.timezone.utc,
        monotonic_start: float | None = None,
        tick: float = 0.0,
    ) -> None:
        if not math.isfinite(start):
            raise ValueError(f"start must be finite, got {start!r}")
        _check_duration(tick, "tick")
        if tick and not _to_ns(tick):
            raise ValueError(f"tick {tick!r} is below one nanosecond and would freeze the clock")
        if monotonic_start is None:
            monotonic_start = _time.monotonic()
        elif not math.isfinite(monotonic_start):
            raise ValueError(f"monotonic_start must be finite, got {monotonic_start!r}")
        if not isinstance(tz, _dt.tzinfo):
            raise TypeError(f"tz must be a datetime.tzinfo, not {type(tz).__name__}")
        self._tz = tz
        self._wall_ns = _to_ns(start)
        self._mono_ns = _to_ns(monotonic_start)
        self._tick_ns = _to_ns(tick)
        self._sleeps: list[float] = []
        self._lock = threading.Lock()
        self._time_module: _ClockedTimeModule | None = None
        self._datetime_module: _ClockedDatetimeModule | None = None
        self._datetime_class: type[_dt.datetime] | None = None
        self._date_class: type[_dt.date] | None = None

    @property
    def tz(self) -> _dt.tzinfo:
        """The zone of :meth:`now` without an argument; naive readings use the process's zone."""
        return self._tz

    @property
    def sleeps(self) -> tuple[float, ...]:
        """Every :meth:`sleep` argument, in call order: the schedule a test asserts."""
        with self._lock:
            return tuple(self._sleeps)

    def _read(self) -> tuple[int, int]:
        with self._lock:
            wall, mono = self._wall_ns, self._mono_ns
            self._wall_ns += self._tick_ns
            self._mono_ns += self._tick_ns
        return wall, mono

    def time(self) -> float:
        return self._read()[0] / _NS_PER_SEC

    def time_ns(self) -> int:
        return self._read()[0]

    def monotonic(self) -> float:
        return self._read()[1] / _NS_PER_SEC

    def monotonic_ns(self) -> int:
        return self._read()[1]

    perf_counter = monotonic
    perf_counter_ns = monotonic_ns

    def now(self, tz: _dt.tzinfo | None = None) -> _dt.datetime:
        """The virtual instant as an AWARE datetime, in ``tz`` or the clock's zone."""
        wall_ns = self._read()[0]
        instant = _EPOCH + _dt.timedelta(microseconds=wall_ns // 1000)
        return instant.astimezone(tz if tz is not None else self._tz)

    def advance(self, seconds: float) -> float:
        """Move both clocks forward by ``seconds``; return the new monotonic reading."""
        _check_duration(seconds, "advance")
        step = _to_ns(seconds)
        with self._lock:
            self._wall_ns += step
            self._mono_ns += step
            return self._mono_ns / _NS_PER_SEC

    def sleep(self, seconds: float) -> None:
        """Record ``seconds`` and advance by it, without blocking."""
        _check_duration(seconds, "sleep")
        step = _to_ns(seconds)
        with self._lock:
            self._sleeps.append(float(seconds))
            self._wall_ns += step
            self._mono_ns += step
        _REAL_SLEEP(0)

    def as_time_module(self) -> types.ModuleType:
        """A stand-in for the ``time`` module that reads this clock (cached)."""
        if self._time_module is None:
            self._time_module = _ClockedTimeModule(self)
        return self._time_module

    def as_datetime_class(self) -> type[_dt.datetime]:
        """A stand-in for ``datetime.datetime`` that reads this clock (cached)."""
        if self._datetime_class is None:
            self._datetime_class = _clocked_datetime_class(self)
        return self._datetime_class

    def as_date_class(self) -> type[_dt.date]:
        """A stand-in for ``datetime.date`` that reads this clock (cached)."""
        if self._date_class is None:
            self._date_class = _clocked_date_class(self)
        return self._date_class

    def _as_datetime_module(self) -> _ClockedDatetimeModule:
        if self._datetime_module is None:
            self._datetime_module = _ClockedDatetimeModule(self)
        return self._datetime_module

    def _owns(self, value: object) -> bool | None:
        """True if ``value`` is one of this clock's stand-ins, False if another's.

        ``None`` when ``value`` is no clock's stand-in. Decided by type, never by
        reading an attribute, because a ``MagicMock`` answers every attribute.
        """
        if isinstance(value, (_ClockedTimeModule, _ClockedDatetimeModule)):
            return value._clock is self
        if isinstance(value, type) and type(value) is _ClockedMeta:
            return value in (self._datetime_class, self._date_class)
        if inspect.ismethod(value) and isinstance(value.__self__, ManualClock):
            return value.__self__ is self
        if inspect.isfunction(value):
            marked = value.__dict__.get("_manual_clock")
            if isinstance(marked, ManualClock):
                return marked is self
        return None

    def install(
        self,
        monkeypatch: Any,
        module: types.ModuleType,
        *,
        time: bool = True,
        datetime: bool = False,
    ) -> tuple[str, ...]:
        """Replace ``module``'s own clock names with this clock, through ``monkeypatch``.

        ``time=True`` replaces a binding of the ``time`` module under any name
        (``import time``, ``import time as t``) and a binding of one of its
        clock readers (``from time import monotonic``). ``datetime=True``
        replaces a binding of the ``datetime`` module, of ``datetime.datetime``
        and of ``datetime.date``. Returns the names it patched, sorted.

        Refused with ``ValueError``: a standard-library module or a submodule of
        one (installing on ``time`` or ``asyncio.base_events`` would freeze the
        worker or the event loop), a test-runner package, ``kiro_crew.testing``
        itself, a module where a requested kind matched nothing (a silent no-op
        install is the trap this helper removes), and a module already holding
        another clock's stand-in.

        Not reached: a function-local ``import time``, a clock captured at
        import (a default argument ``clock=time.monotonic``), and the host-zone
        semantics of ``time.localtime()`` / ``astimezone()`` with no argument.
        A product loop such as ``while not stop: time.sleep(1)`` spins under the
        clock because ``sleep`` never blocks, so install only where the
        subject's sleeps are bounded by the test.
        """
        if not isinstance(module, types.ModuleType):
            raise TypeError(f"install() takes a module, not {type(module).__name__}")
        name = module.__name__
        top = name.partition(".")[0]
        if top in sys.stdlib_module_names or top in _RUNNER_PACKAGES:
            raise ValueError(
                f"refusing to install a clock on {name!r}: it is the standard library or "
                "the test runner, and rebinding its clock affects the whole worker. "
                "Install on the module the code under test reads instead."
            )
        if name == "kiro_crew.testing" or name.startswith("kiro_crew.testing."):
            raise ValueError(
                f"refusing to install a clock on {name!r}: the test helpers' own "
                "deadlines must stay on real time."
            )
        if not (time or datetime):
            raise ValueError("install() needs time=True, datetime=True or both")

        wanted: list[tuple[object, object, str]] = []
        if time:
            stand_in = self.as_time_module()
            wanted.append((_time, stand_in, "time"))
            readers = _CLOCK_READERS + _IMPLICIT_NOW_READERS + _GETTIME_READERS
            for reader in readers:
                if hasattr(_time, reader):
                    wanted.append((getattr(_time, reader), getattr(stand_in, reader), "time"))
        if datetime:
            wanted.append((_dt, self._as_datetime_module(), "datetime"))
            wanted.append((_dt.datetime, self.as_datetime_class(), "datetime"))
            wanted.append((_dt.date, self.as_date_class(), "datetime"))

        found: set[str] = set()
        patches: list[tuple[str, object]] = []
        for attr, value in list(vars(module).items()):
            owner = self._owns(value)
            if owner is False:
                raise ValueError(
                    f"{name}.{attr} already holds another ManualClock's stand-in; "
                    "one module reads one clock"
                )
            if owner is True:
                found.add(_kind_of(value))
                continue
            for original, replacement, kind in wanted:
                if value is original:
                    patches.append((attr, replacement))
                    found.add(kind)
                    break
        missing = [k for k, on in (("time", time), ("datetime", datetime)) if on and k not in found]
        if missing:
            raise ValueError(
                f"{name} binds no {' or '.join(missing)} clock at module level, so "
                "install() would change nothing; a function-local import is not reachable"
            )
        for attr, replacement in patches:
            monkeypatch.setattr(module, attr, replacement)
        return tuple(sorted(attr for attr, _ in patches))


def _kind_of(stand_in: object) -> str:
    """Which ``install`` kind a stand-in belongs to."""
    if isinstance(stand_in, (_ClockedDatetimeModule, type)):
        return "datetime"
    return "time"
