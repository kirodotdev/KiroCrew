"""A summary sidecar whose bytes are not UTF-8 reads as no cache, like bad JSON.

``get_cached_summary``, ``get_cached_intent_summary`` and ``read_intent_summary``
each answer "no cache" for a sidecar that does not parse. A sidecar holding bytes
that do not decode as UTF-8 is the same unusable cache, so each answers the same
way instead of raising ``UnicodeDecodeError`` into the session list or the
summary panel.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import pytest

from kiro_crew.history import ConversationLog

# Not UTF-8: 0xff never starts a UTF-8 sequence.
_UNDECODABLE = b'\xff\xfe{"summary": "x"}'

T = TypeVar("T")


@pytest.fixture
def log(tmp_path: Path) -> ConversationLog:
    log = ConversationLog(base_dir=tmp_path)
    log.append("k", "user", "hello")
    return log


def _read(reader: Callable[[], T]) -> T:
    """Run *reader*, failing the test (not erroring it) when it raises on the bytes."""
    try:
        return reader()
    except UnicodeDecodeError as exc:
        pytest.fail(f"an undecodable cache raised {exc!r} instead of reading as no cache")


def _plant_intent_cache(log: ConversationLog) -> None:
    path = log._intent_summary_cache_path("k")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_UNDECODABLE)


def test_an_undecodable_summary_cache_reads_as_no_cache(log: ConversationLog) -> None:
    sig = log.session_mtime("k")
    assert sig is not None
    log.set_cached_summary("k", "A summary", sig, log.rotation_generation("k"))
    assert log.get_cached_summary("k") == "A summary"

    log._summary_cache_path("k").write_bytes(_UNDECODABLE)

    assert _read(lambda: log.get_cached_summary("k")) is None


def test_an_undecodable_intent_cache_reads_as_no_cache(log: ConversationLog) -> None:
    _plant_intent_cache(log)

    assert _read(lambda: log.get_cached_intent_summary("k")) is None


def test_an_undecodable_intent_cache_reads_as_absent_not_stale(log: ConversationLog) -> None:
    _plant_intent_cache(log)

    assert _read(lambda: log.read_intent_summary("k")) == (None, False)
