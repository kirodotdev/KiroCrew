"""A load that fails on the caller's exhausted stack does not latch fail-closed.

The failing plan still replaces its whole text; the next plan, on a normal stack,
redacts as written. A load that fails for any other reason still latches. A stream
whose detector read fails holds its buffer instead of choosing a cut without them.
"""

from __future__ import annotations

import importlib
import random
import sys
from types import FrameType

import pytest

from kiro_crew import security
from kiro_crew.security import redaction
from kiro_crew.security.redaction_structured import compat
from kiro_crew.testing.synthetic_credentials import ALNUM

PLAIN = "plain text ok"


@pytest.fixture(autouse=True)
def _unlatched(_floor_monkeypatch):
    # A latched flag left by a failing case must not poison the rest of the run.
    _floor_monkeypatch.setattr(compat, "_LOAD_FAILED", False)
    _floor_monkeypatch.setattr(compat, "_count_fail_closed", lambda: None)


def _depth() -> int:
    frame: FrameType | None = sys._getframe()
    n = 0
    while frame is not None:
        frame, n = frame.f_back, n + 1
    return n


def _redact_at(depth: int) -> None:
    if depth > 0:
        _redact_at(depth - 1)
        return
    try:
        security.redact_credentials(PLAIN)
    except RecursionError:
        pass


def test_a_plan_on_a_nearly_full_stack_does_not_poison_later_plans():
    # Sweep the stack limit across every frame a plan runs, so it lands inside
    # _load on some pass whatever the runner's own depth is.
    room = sys.getrecursionlimit() - _depth()
    for spare in range(1, 120):
        _redact_at(room - spare)
    assert security.redact_credentials(PLAIN) == (PLAIN, [])


@pytest.mark.parametrize("exc", [RecursionError, MemoryError])
def test_a_load_out_of_stack_or_memory_fails_this_plan_closed_only(monkeypatch, exc):
    real = importlib.import_module
    calls: list[str] = []

    def once(name, *a, **k):
        calls.append(name)
        if len(calls) == 1:
            raise exc()
        return real(name, *a, **k)

    monkeypatch.setattr(importlib, "import_module", once)
    out, warnings = security.redact_credentials(PLAIN)
    assert out == redaction.REDACTION_UNAVAILABLE_TAG
    assert warnings == ["Redaction unavailable: whole text replaced"]
    assert compat._LOAD_FAILED is False
    assert security.redact_credentials(PLAIN) == (PLAIN, [])


def _fail_once(exc: type[BaseException], real):
    calls: list[int] = []

    def wrapped(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise exc()
        return real(*a, **k)

    return wrapped, calls


@pytest.mark.parametrize("site", ["load", "read"])
def test_a_stream_read_that_fails_once_streams_no_part_of_a_long_cookie(monkeypatch, site):
    # The stream's own detector read fails once; the batch plan that redacts the
    # committed window right after it succeeds. A cut chosen without the detectors
    # would split the value and stream its tail with no anchor left to redact it.
    value = "".join(random.Random(7).choice(ALNUM) for _ in range(1900))
    if site == "load":
        wrapped, calls = _fail_once(MemoryError, importlib.import_module)
        monkeypatch.setattr(importlib, "import_module", wrapped)
    else:
        wrapped, calls = _fail_once(MemoryError, compat._read)
        monkeypatch.setattr(compat, "_read", wrapped)
    sr = security.StreamRedactor()
    out = sr.feed("Cookie: session=" + value)
    assert calls, "the stream's read never reached the failing call"
    out += sr.feed("\n") + sr.flush()
    assert compat._LOAD_FAILED is False
    assert [i for i in range(len(value) - 16) if value[i : i + 16] in out] == []
    assert out == "Cookie: session=" + security.REDACTED_CREDENTIAL_TAG + "\n"


def test_a_broken_table_still_latches(monkeypatch):
    real = importlib.import_module

    def broken(name, *_a, **_k):
        raise ValueError("bad row")

    monkeypatch.setattr(importlib, "import_module", broken)
    assert security.redact_credentials(PLAIN)[0] == redaction.REDACTION_UNAVAILABLE_TAG
    assert compat._LOAD_FAILED is True
    monkeypatch.setattr(importlib, "import_module", real)
    assert security.redact_credentials(PLAIN)[0] == redaction.REDACTION_UNAVAILABLE_TAG
