"""The content-scan verdict cache and scan serialization in ``platform.context``.

``binary_content_is_flagged`` / ``wide_content_is_flagged`` are GIL-bound regex
passes over up to the 50 MB read cap, reached again for the same bytes on every
outbox delivery leg. These pin the two properties that keep that off the event
loop's back -- one scan per (scan, bytes, ceiling), and one scan at a time -- and
the conditions under which a verdict must NOT be kept.

Detector passes are counted by replacing ``redact_via_context``, which both scan
bodies call by module-global name: a binary scan of ``_BYTES`` makes exactly one
pass (its wide leg finds no run), and a wide scan of ``_WIDE`` makes exactly one.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from types import SimpleNamespace

import pytest

from kiro_crew.platform import context
from kiro_crew.platform.context import (
    PlatformCompositionError,
    binary_content_is_flagged,
    reset_context,
    wide_content_is_flagged,
)

_BYTES = b"\x89PNG\r\n\x1a\n\xff\xfe" + b"\x00" * 64
_WIDE = b"\xff\xfe" + "a run of wide text".encode("utf-16-le")


@pytest.fixture(autouse=True)
def _fresh_cache(_floor_monkeypatch):
    _floor_monkeypatch.setattr(context, "_SCAN_VERDICTS", OrderedDict())


def _count_passes(monkeypatch, *, flag: bool = False) -> list:
    """Record every detector pass; with *flag* each pass finds something."""
    passes: list = []

    def redact(text: str) -> str:
        passes.append(text)
        return text + "*" if flag else text

    monkeypatch.setattr(context, "redact_via_context", redact)
    return passes


def test_same_bytes_are_scanned_once(monkeypatch):
    passes = _count_passes(monkeypatch, flag=True)

    assert binary_content_is_flagged(_BYTES) is True
    assert binary_content_is_flagged(_BYTES) is True
    assert len(passes) == 1


def test_other_bytes_or_another_scan_is_scanned_again(monkeypatch):
    passes = _count_passes(monkeypatch)

    binary_content_is_flagged(_BYTES)
    binary_content_is_flagged(_BYTES + b"\x01")
    wide_content_is_flagged(_WIDE)
    wide_content_is_flagged(_WIDE)

    assert len(passes) == 3


def test_installing_a_context_retires_the_verdicts(monkeypatch):
    passes = _count_passes(monkeypatch)

    binary_content_is_flagged(_BYTES)
    reset_context()  # every install bumps the governance generation
    binary_content_is_flagged(_BYTES)

    assert len(passes) == 2


def test_a_verdict_spanning_a_context_install_is_not_kept(monkeypatch):
    passes: list = []

    def redact(text: str) -> str:
        passes.append(text)
        if len(passes) == 1:
            reset_context()  # an install lands while this scan runs
        return text

    monkeypatch.setattr(context, "redact_via_context", redact)

    binary_content_is_flagged(_BYTES)
    assert [key for key in context._SCAN_VERDICTS if key[0] == "binary"] == []
    binary_content_is_flagged(_BYTES)
    assert len(passes) == 2


def test_a_verdict_given_while_redaction_degraded_is_not_kept(monkeypatch):
    """The baseline answering for a broken adapter is not the active context's answer."""

    def adapter_down(text: str) -> str:
        raise RuntimeError("adapter down")

    monkeypatch.setattr(
        context,
        "current_context",
        lambda: SimpleNamespace(credentials=SimpleNamespace(redact=adapter_down)),
    )
    before = context._redact_degrades()

    assert binary_content_is_flagged(_BYTES) is False
    assert binary_content_is_flagged(_BYTES) is False

    assert context._redact_degrades() == before + 2  # both calls scanned


def test_another_threads_degrade_does_not_discard_this_verdict(monkeypatch):
    def adapter_down(text: str) -> str:
        raise RuntimeError("adapter down")

    real_shim = context.redact_via_context
    monkeypatch.setattr(
        context,
        "current_context",
        lambda: SimpleNamespace(credentials=SimpleNamespace(redact=adapter_down)),
    )
    passes: list = []

    def redact(text: str) -> str:
        passes.append(text)
        other = threading.Thread(target=real_shim, args=("unrelated egress",))
        other.start()  # degrades on ITS thread while this scan runs
        other.join(timeout=10)
        return text

    monkeypatch.setattr(context, "redact_via_context", redact)

    binary_content_is_flagged(_BYTES)
    binary_content_is_flagged(_BYTES)

    assert len(passes) == 1


def test_a_failed_scan_keeps_nothing(monkeypatch):
    def cannot_compose(text: str) -> str:
        raise PlatformCompositionError("companion missing")

    monkeypatch.setattr(context, "redact_via_context", cannot_compose)
    with pytest.raises(PlatformCompositionError):
        binary_content_is_flagged(_BYTES)

    passes = _count_passes(monkeypatch)
    binary_content_is_flagged(_BYTES)
    assert len(passes) == 1


def test_no_other_thread_starts_a_scan_while_one_runs(monkeypatch):
    probes: list = []

    def redact(text: str) -> str:
        def probe() -> None:
            got = context._SCAN_LOCK.acquire(blocking=False)
            if got:
                context._SCAN_LOCK.release()
            probes.append(got)

        prober = threading.Thread(target=probe)
        prober.start()
        prober.join(timeout=10)
        return text

    monkeypatch.setattr(context, "redact_via_context", redact)

    binary_content_is_flagged(_BYTES)
    wide_content_is_flagged(_WIDE)

    assert probes == [False, False]


class _ObservedLock:
    """An RLock that reports each thread arriving to take it.

    A thread reaches ``__enter__`` only after its first cache lookup missed, so a
    second arrival proves the second caller is past that lookup and about to wait.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._arrivals: set = set()
        self.second_arrived = threading.Event()

    def __enter__(self) -> "_ObservedLock":
        self._arrivals.add(threading.get_ident())
        if len(self._arrivals) >= 2:
            self.second_arrived.set()
        self._lock.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self._lock.release()


def test_concurrent_callers_for_the_same_bytes_share_one_scan(monkeypatch):
    """The second caller misses, waits on the lock, then reads the first one's verdict."""
    lock = _ObservedLock()
    monkeypatch.setattr(context, "_SCAN_LOCK", lock)
    entered = threading.Event()
    release = threading.Event()
    passes: list = []

    def slow_redact(text: str) -> str:
        passes.append(text)
        entered.set()
        release.wait(timeout=10)
        return text + "*"

    monkeypatch.setattr(context, "redact_via_context", slow_redact)
    results: list = []

    def caller() -> None:
        results.append(binary_content_is_flagged(_BYTES))

    first = threading.Thread(target=caller)
    first.start()
    assert entered.wait(timeout=10)
    second = threading.Thread(target=caller)
    second.start()
    assert lock.second_arrived.wait(timeout=10)
    release.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not first.is_alive() and not second.is_alive()
    assert results == [True, True]
    assert len(passes) == 1


def test_the_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(context, "_SCAN_VERDICTS_MAX", 2)
    passes = _count_passes(monkeypatch)

    for suffix in ("a", "b", "c", "a"):  # the last "a" was evicted by "c"
        wide_content_is_flagged(_WIDE + suffix.encode("utf-16-le"))

    assert len(context._SCAN_VERDICTS) == 2
    assert len(passes) == 4
