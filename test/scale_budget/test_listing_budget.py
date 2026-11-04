"""Older Sessions listing budgets (F1, F2, F9, F10): transcript reads per listing.

Every request goes through the real ``GET /api/sessions`` handler, so a budget
here fails when a call site stops using the cheap path, not only when the cheap
path itself regresses. The history directory is seeded by
:func:`conftest.seed_history`: titled, untitled-with-a-user-line and
metadata-only transcripts, a tenth of each in the ``cron`` namespace.

Every budget also checks the listing still answers right after a write, so a
cache that stops seeing writes cannot meet a budget by never reading. All of it
runs on :func:`conftest.fake_clock`, which reaches the listing index's clock, so
no count depends on how long the run takes.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.history import ConversationLog

from .conftest import (
    FakeClock,
    TranscriptIO,
    harness_fail,
    optional_module,
    seed_history,
    seed_one_line_history,
)

pytestmark = pytest.mark.scale_budget

PER_CLASS = 500
#: How long the concurrent-listing budget holds its first scan for the other
#: requests to arrive. Every request arrives in well under a second, so this is
#: only reached when the join point moved, and the test then fails by name.
LISTING_GATE_CEILING_S = 30.0
QUERY = "/api/sessions?user_only=1&limit=30"
#: Rows a ``user_only`` listing shows over the seed: titled and untitled.
LISTED = 2 * PER_CLASS
FILES = 3 * PER_CLASS


def _log(tmp_path: Path) -> ConversationLog:
    return ConversationLog(base_dir=tmp_path / "history")


def _app(log: ConversationLog) -> web.Application:
    app = web.Application()
    app["state"] = types.SimpleNamespace(conversation_log=log, _slots={})
    return app


def _past_the_listing_memo(clock: FakeClock) -> None:
    """Move past ``history_listing_memo.TTL_SECS``, so the next listing is a steady poll.

    The whole-listing memo answers a repeat listing inside its TTL without a
    scan. A sidebar polls further apart than that, so a steady-listing budget
    measures a listing that runs after the memo's entry has expired.
    """
    memo = optional_module("kiro_crew.history_listing_memo")
    clock.advance(float(getattr(memo, "TTL_SECS", 0.0)))


def _listing(log: ConversationLog, query: str = QUERY) -> int:
    """One request through the real handler; the ``total`` it answered."""
    from kiro_crew.dashboard.handlers.sessions_parts.history_reads import api_sessions

    response = asyncio.run(api_sessions(make_mocked_request("GET", query, app=_app(log))))
    return int(json.loads(response.body)["total"])


def _assert_sees_a_write(
    log: ConversationLog, before: int, query: str = QUERY, tag: str = ""
) -> None:
    """One new user transcript makes the next listing answer exactly one more row."""
    log.append(f"dashboard:chat-fresh-{tag or 'w'}", "user", "a question after the budget")
    after = _listing(log, query)
    assert after == before + 1, (
        f"a listing after one new transcript answered {after} rows, expected {before + 1}: "
        "the cheap path stopped seeing writes"
    )


def _count_has_messages(monkeypatch: pytest.MonkeyPatch, *, uncached: bool) -> dict[str, int]:
    """Count ``ConversationLog`` message checks.

    ``uncached=True`` counts the read behind the memo, ``_read_has_messages``,
    where a version has one (it is the call that opens the file under its lock);
    otherwise every ``has_messages`` call.
    """
    name = (
        "_read_has_messages"
        if uncached and hasattr(ConversationLog, "_read_has_messages")
        else "has_messages"
    )
    counter = {"n": 0}
    real = getattr(ConversationLog, name)

    def counted(self: ConversationLog, *args: Any, **kwargs: Any) -> Any:
        counter["n"] += 1
        return real(self, *args, **kwargs)

    monkeypatch.setattr(ConversationLog, name, counted)
    return counter


@pytest.fixture
def seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_clock: FakeClock):
    counts = seed_history(tmp_path / "history", per_class=PER_CLASS)
    log = _log(tmp_path)
    io = TranscriptIO(tmp_path / "history").install(monkeypatch)
    return types.SimpleNamespace(
        log=log, io=io, counts=counts, clock=fake_clock, monkeypatch=monkeypatch
    )


def test_first_user_only_listing_reads_each_transcript_at_most_twice(seeded) -> None:
    """Ratchet on main: a cold listing opens each of the 1,500 files at most twice."""
    total = _listing(seeded.log)
    assert total == LISTED, f"F1/F9: the user_only listing showed {total} rows, expected {LISTED}"
    assert seeded.io.opens <= 2 * FILES, (
        f"F1/F9: a cold user_only listing over {FILES} files opened {seeded.io.opens} "
        f"transcripts (budget {2 * FILES})"
    )


def test_second_user_only_listing_reads_no_transcripts(seeded) -> None:
    """The second listing with nothing changed: 0 opens, 0 locks, 0 uncached message reads."""
    reads = _count_has_messages(seeded.monkeypatch, uncached=True)
    first = _listing(seeded.log)
    _past_the_listing_memo(seeded.clock)
    seeded.io.reset()
    reads["n"] = 0
    second = _listing(seeded.log)
    measured = dict(seeded.io.snapshot(), message_reads=reads["n"])
    assert (measured["opens"], measured["locks"], measured["message_reads"]) == (0, 0, 0), (
        f"F1: a steady user_only listing over {FILES} files cost {measured} "
        "(budget 0 opens, 0 locks, 0 uncached message reads)"
    )
    assert second == first == LISTED, f"F1: steady listings answered {first} then {second} rows"
    _assert_sees_a_write(seeded.log, second)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="scale-F9: main stats every transcript on each listing; the index stats the directory",
)
def test_steady_listing_costs_one_stat(seeded) -> None:
    """A steady listing proves freshness with 1 ``os.stat`` and no directory scan."""
    first = _listing(seeded.log)
    _listing(seeded.log)
    _past_the_listing_memo(seeded.clock)
    seeded.io.reset()
    steady = _listing(seeded.log)
    assert seeded.io.stats <= 1 and seeded.io.scandirs == 0, (
        f"F9: a steady listing cost {seeded.io.stats} stats and {seeded.io.scandirs} "
        "directory scans (budget 1 stat, 0 scans)"
    )
    assert steady == first == LISTED, f"F9: steady listings answered {first} then {steady} rows"
    _assert_sees_a_write(seeded.log, steady)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="scale-F9: the index runs its full stat reconcile inline on the request that trips it",
)
def test_reconcile_runs_off_the_request(seeded) -> None:
    """Past ``RECONCILE_SECS`` with nothing changed, the request still costs 1 stat, 0 scans."""
    from .conftest import optional_module

    index = optional_module("kiro_crew.history_listing_index")
    reconcile_secs = float(getattr(index, "RECONCILE_SECS", 5.0))
    first = _listing(seeded.log)
    _listing(seeded.log)
    seeded.clock.advance(reconcile_secs + 1.0)
    seeded.io.reset()
    late = _listing(seeded.log)
    assert seeded.io.stats <= 1 and seeded.io.scandirs == 0, (
        f"F9: the first listing {reconcile_secs + 1.0:.0f} s after the last cost "
        f"{seeded.io.stats} stats and {seeded.io.scandirs} directory scans on the request "
        "(budget 1 stat, 0 scans: the reconcile belongs off the request path)"
    )
    assert late == first == LISTED, f"F9: listings answered {first} then {late} rows"
    _assert_sees_a_write(seeded.log, late)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="scale-F9: the listing index leaves has_messages unfilled, so the listing asks per row",
)
def test_index_build_fills_has_messages(seeded) -> None:
    """The first ``user_only`` listing makes 0 ``ConversationLog.has_messages`` calls.

    The build already reads every head for its row, which answers whether the
    transcript has a message (item 2.10), so the listing never asks again.
    """
    calls = _count_has_messages(seeded.monkeypatch, uncached=False)
    first = _listing(seeded.log)
    assert calls["n"] == 0, (
        f"F9: the first user_only listing over {FILES} files made {calls['n']} "
        "has_messages calls (budget 0)"
    )
    assert first == LISTED, f"F9: the first listing answered {first} rows, expected {LISTED}"
    _assert_sees_a_write(seeded.log, first)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="scale-F10: main has no boot warm of the listing; the first listing pays the cold read",
)
def test_first_listing_after_warm_reads_no_transcripts(seeded) -> None:
    """One boot pass: ``warm_listing_index`` reads each file once, then the first listing opens 0."""
    warm = getattr(seeded.log, "warm_listing_index", None)
    assert warm is not None, "F10: ConversationLog has no warm_listing_index boot pass"
    warm()
    warmed = seeded.io.opens
    seeded.io.reset()
    first = _listing(seeded.log)
    assert warmed <= FILES and seeded.io.opens == 0, (
        f"F10: the warm opened {warmed} transcripts (budget {FILES}) and the first "
        f"user_only listing after it opened {seeded.io.opens} (budget 0)"
    )
    assert first == LISTED, f"F10: the first listing answered {first} rows, expected {LISTED}"
    _assert_sees_a_write(seeded.log, first)


def test_one_append_rereads_only_its_own_transcript(seeded) -> None:
    """Ratchet: after one append, the next listing re-opens at most 3 files under 1 lock.

    On main every steady listing re-reads each untitled head, so this case pins
    the delta over a steady listing rather than an absolute count, and checks
    the appended transcript is one more row.
    """
    _listing(seeded.log)
    _past_the_listing_memo(seeded.clock)
    seeded.io.reset()
    before = _listing(seeded.log)
    steady = seeded.io.snapshot()
    seeded.log.append("dashboard:chat-9999-1", "user", "a new question")
    seeded.io.reset()
    after_total = _listing(seeded.log)
    after = seeded.io.snapshot()
    extra = {k: after[k] - steady[k] for k in ("opens", "locks")}
    assert extra["opens"] <= 3 and extra["locks"] <= 1, (
        f"F1/F9: one append made the next listing cost {extra} more than a steady one "
        "(budget 3 opens, 1 lock)"
    )
    assert after_total == before + 1, (
        f"F1/F9: one append moved the listing from {before} to {after_total} rows, "
        f"expected {before + 1}"
    )


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="scale-F9: past _METADATA_CACHE_MAX the head memo evicts and every listing re-reads",
)
def test_listing_past_the_metadata_cache_reads_no_heads(
    tmp_path: Path, monkeypatch, fake_clock
) -> None:
    """9,000 one-line files, past ``_METADATA_CACHE_MAX``: the second listing opens 0."""
    from kiro_crew import history

    n = history._METADATA_CACHE_MAX + 808
    query = "/api/sessions?limit=30"
    seed_one_line_history(tmp_path / "history", n)
    log = _log(tmp_path)
    io = TranscriptIO(tmp_path / "history").install(monkeypatch)
    first = _listing(log, query)
    _past_the_listing_memo(fake_clock)
    io.reset()
    second = _listing(log, query)
    assert (
        io.opens == 0
    ), f"F9: a steady listing over {n} files opened {io.opens} transcripts (budget 0)"
    assert second == first == n, f"F9: listings answered {first} then {second} rows, expected {n}"
    _assert_sees_a_write(log, second, query)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="scale-F2: concurrent identical /api/sessions requests each run their own scan",
)
def test_concurrent_identical_listings_scan_once(tmp_path: Path, monkeypatch, fake_clock) -> None:
    """12 identical requests in flight together run one scan of the history directory.

    The count is ``os.scandir`` of the directory, not calls to a listing method.
    A join placed in the handler, before ``ConversationLog.list_sessions``,
    waits out :data:`LISTING_GATE_CEILING_S` and fails by name.

    The requests are held in flight together, so the verdict does not depend on
    thread scheduling. The first scan waits until all 12 requests have entered
    ``ConversationLog.list_sessions``, and each request gets its own worker
    thread. The whole-listing memo is taken out, because it answers a request
    that starts after another one finished, which is not a shared scan.
    """
    requests = 12
    history_dir = tmp_path / "history"
    seed_history(history_dir, per_class=20)
    log = _log(tmp_path)
    io = TranscriptIO(history_dir).install(monkeypatch)
    memo = optional_module("kiro_crew.history_listing_memo")
    if memo is not None:
        monkeypatch.setattr(memo, "lookup", lambda directory, key: None)

    lock = threading.Lock()
    gate = {"entered": 0, "first_scan_held": False, "timed_out": False}
    all_entered = threading.Event()
    real_list_sessions = ConversationLog.list_sessions

    def entering_list_sessions(self: ConversationLog, *args: Any, **kwargs: Any) -> Any:
        if not args and kwargs.get("keys") is None:
            with lock:
                gate["entered"] += 1
                if gate["entered"] >= requests:
                    all_entered.set()
        return real_list_sessions(self, *args, **kwargs)

    counting_scandir = os.scandir

    def holding_scandir(path: Any = ".") -> Any:
        if isinstance(path, (str, os.PathLike)) and os.fspath(path) == os.fspath(history_dir):
            with lock:
                hold = not gate["first_scan_held"]
                gate["first_scan_held"] = True
            if hold and not all_entered.wait(timeout=LISTING_GATE_CEILING_S):
                gate["timed_out"] = True
        return counting_scandir(path)

    monkeypatch.setattr(ConversationLog, "list_sessions", entering_list_sessions)
    monkeypatch.setattr(os, "scandir", holding_scandir)

    from kiro_crew.dashboard.handlers.sessions_parts.history_reads import api_sessions

    app = _app(log)

    async def _all() -> list[int]:
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=requests))
        responses = await asyncio.gather(
            *(api_sessions(make_mocked_request("GET", QUERY, app=app)) for _ in range(requests))
        )
        return [int(json.loads(r.body)["total"]) for r in responses]

    totals = asyncio.run(_all())
    if gate["timed_out"]:
        harness_fail(
            f"only {gate['entered']} of {requests} requests entered "
            "ConversationLog.list_sessions while the first scan was held; if the join "
            "moved out of list_sessions, hold the scan on the new join point"
        )
    if io.scandirs == 0:
        harness_fail("12 listings scanned the history directory 0 times; the scandir spy missed")
    assert io.scandirs == 1, (
        f"F2: 12 concurrent identical listings scanned the history directory {io.scandirs} "
        "times (budget 1)"
    )
    assert totals == [40] * requests, f"F2: the joined listings answered {totals}, expected 40 each"
    _assert_sees_a_write(log, 40)
