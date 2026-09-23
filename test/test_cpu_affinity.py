"""The core count a process is allowed to use.

Every expectation here is pinned by patching the same platform functions
:func:`affinity_cpu_count` reads, and the affinity cases are skipped where the
platform has no affinity API at all -- a probe of ``os``, independent of anything
the reader itself decides, so a broken reader fails rather than skips.
"""

from __future__ import annotations

import os

import pytest

from kiro_crew.cpu_affinity import affinity_cpu_count

_HAS_AFFINITY = hasattr(os, "sched_getaffinity")
_NO_AFFINITY_REASON = "this platform has no os.sched_getaffinity to narrow"


@pytest.mark.skipif(not _HAS_AFFINITY, reason=_NO_AFFINITY_REASON)
def test_the_allowed_set_wins_over_the_host_count(monkeypatch) -> None:
    """A cpuset carved out of a big host reports the cpuset."""
    monkeypatch.setattr(os, "cpu_count", lambda: 64)
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: {3, 7})
    assert affinity_cpu_count() == 2


@pytest.mark.skipif(not _HAS_AFFINITY, reason=_NO_AFFINITY_REASON)
def test_a_refused_affinity_read_falls_back_to_the_host_count(monkeypatch) -> None:
    def _refuse(pid: int) -> set[int]:
        raise OSError("EPERM")

    monkeypatch.setattr(os, "sched_getaffinity", _refuse)
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    assert affinity_cpu_count() == 8


@pytest.mark.skipif(not _HAS_AFFINITY, reason=_NO_AFFINITY_REASON)
def test_an_empty_allowed_set_is_not_zero_cores(monkeypatch) -> None:
    """Zero is never an answer a caller can size a pool from."""
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set())
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    assert affinity_cpu_count() is None


def test_without_an_affinity_api_the_host_count_answers(monkeypatch) -> None:
    monkeypatch.delattr(os, "sched_getaffinity", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    assert affinity_cpu_count() == 8


def test_a_count_no_source_can_give_is_none(monkeypatch) -> None:
    """``None`` is kept, so a caller can tell "unknown" from "one core"."""
    monkeypatch.delattr(os, "sched_getaffinity", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    assert affinity_cpu_count() is None


def test_this_host_answers_a_usable_count() -> None:
    """Unpatched, on whichever platform runs this shard."""
    count = affinity_cpu_count()
    assert count is None or count >= 1
