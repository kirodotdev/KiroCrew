"""A slot baseline never folds cold on the subscriber's thread.

``bus.subscribe(baseline=True)`` and the eager worker's ``_advance`` read with
``cold=False``, so a cell with no warm state and no usable savepoint is never folded on
the CALLER's thread: it answers "no baseline yet" at once, ONE background prime per cell
folds it (:mod:`kiro_crew.crew_log.slot_prime`), and its value reaches every subscriber
as the next ``FOLD_ADVANCED``.

Real bus, real store, own data home. The seams are a wrapper that COUNTS which thread
streams entries and a wrapper that HOLDS the cold fold on an event. "Returns at once" is
proved structurally: the call under test finishes while the fold it must not wait for is
still held, so no wall-clock bound is asserted.
"""

from __future__ import annotations

import threading
from collections import Counter
from typing import Any, Callable

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, bus, eager, emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import projection_slot_memo, slot_prime

GATEWAY = "gateway"
SLOT = "dashboard:cold-1"
UNITS = ("s-cold-a", "s-cold-b", "s-cold-c")
ENTRIES_PER_UNIT = 40
#: How long a test waits for a call that must finish while the fold it must not wait for
#: is HELD. Generous: completing at all while the fold is held is the proof, the figure
#: caps how long a broken wait can hang the run.
JOIN_SECONDS = 30.0


@pytest.fixture(autouse=True)
def _isolated(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    eager.stop_for_tests()
    crew_log.forget_slot_folds()
    crew_log.forget_session_folds()
    bus.reset_for_tests()
    slot_prime.reset_for_tests()
    emit.reset_caches()
    yield
    slot_prime.reset_for_tests()
    eager.stop_for_tests()
    crew_log.forget_slot_folds()
    crew_log.forget_session_folds()
    bus.reset_for_tests()
    emit.reset_caches()


@pytest.fixture
def streamed(monkeypatch) -> "Counter[str]":
    """Entries streamed by a cold or warm slot pass, per thread name."""
    seen: "Counter[str]" = Counter()
    original = projection_slot_memo._SlotStream.__iter__

    def counting(self):
        for event in original(self):
            seen[threading.current_thread().name] += 1
            yield event

    monkeypatch.setattr(projection_slot_memo._SlotStream, "__iter__", counting)
    return seen


class _Held:
    """Every cold fold blocks inside ``prime`` until :attr:`release` is set."""

    def __init__(self, monkeypatch) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls: "list[str]" = []
        self.fail_next = 0
        original = projection_slot_memo.ProjectionRegistry.prime

        def held(registry, store, events):
            self.calls.append(threading.current_thread().name)
            self.entered.set()
            assert self.release.wait(timeout=JOIN_SECONDS)
            if self.fail_next > 0:
                self.fail_next -= 1
                raise RuntimeError("prime exploded")
            return original(registry, store, events)

        monkeypatch.setattr(projection_slot_memo.ProjectionRegistry, "prime", held)


@pytest.fixture
def held(monkeypatch) -> _Held:
    holder = _Held(monkeypatch)
    yield holder
    holder.release.set()


def _finishes_while_held(call: "Callable[[], Any]", holder: _Held) -> Any:
    """Run *call* on a fresh thread; require it to finish while *holder* is still held."""
    out: "list[Any]" = []
    errors: "list[BaseException]" = []

    def run() -> None:
        try:
            out.append(call())
        except BaseException as exc:  # pragma: no cover - re-raised below
            errors.append(exc)

    thread = threading.Thread(target=run, name="caller-under-test")
    thread.start()
    thread.join(timeout=JOIN_SECONDS)
    assert not thread.is_alive(), "the call waited for the held cold fold"
    assert not holder.release.is_set(), "the fold was released before the call finished"
    if errors:
        raise errors[0]
    return out[0] if out else None


def _board(units: "tuple[str, ...]" = UNITS, per_unit: int = ENTRIES_PER_UNIT) -> None:
    for unit_id in units:
        handle = CrewLog.create(
            lg.KIND_SESSION, unit_id, owner="raymond", agent="kirocrew", slot=SLOT
        )
        for nth in range(per_unit):
            _work(handle, f"{unit_id}-it-{nth}")


def _work(handle: CrewLog, item_id: str) -> int:
    entry = handle.append(
        "work/recorded",
        {
            "slot": SLOT,
            "actor": "conductor",
            "by": SLOT,
            "action": "create",
            "item_id": item_id,
            "title": "a work item",
        },
        src=GATEWAY,
    )
    return int(entry.seq)


def _subscribe(seen: "list[Any]") -> "bus.Disposer":
    return bus.subscribe(
        bus.FOLD_ADVANCED, seen.append, scope=bus.SCOPE_SLOT, key=SLOT, fold="work", baseline=True
    )


def _expected_value() -> Any:
    """The value a from-scratch fold of the board renders, computed OFF the memo."""
    return crew_log.fold_slot("work", crew_log.session_units_for_slot(SLOT), slot=SLOT).value


def test_a_cold_subscribe_returns_while_the_prime_is_held_and_reads_nothing(held, streamed):
    _board()
    seen: "list[Any]" = []
    _finishes_while_held(lambda: _subscribe(seen), held)
    assert held.entered.wait(timeout=JOIN_SECONDS), "no background prime started"
    assert streamed["caller-under-test"] == 0, "the subscriber's thread streamed entries"
    assert seen == [], "nothing is published while the prime is held"
    held.release.set()
    assert slot_prime.drain(timeout=JOIN_SECONDS), "the background prime never finished"
    assert streamed["caller-under-test"] == 0
    primed = [name for name in streamed if name.startswith("crew-log-slot-prime")]
    assert primed and sum(streamed[n] for n in primed) == len(UNITS) * ENTRIES_PER_UNIT
    assert len(seen) == 1, f"expected exactly one pushed value, got {len(seen)}"
    event = seen[0]
    assert isinstance(event, bus.FoldAdvanced) and event.revision > 0
    assert (event.scope, event.key, event.fold) == (bus.SCOPE_SLOT, SLOT, "work")
    assert dict(event.value) == dict(_expected_value())


def test_k_concurrent_subscribers_on_one_cold_cell_cause_one_prime(held, streamed):
    """Every subscriber joins WHILE the one prime is held inside its fold."""
    _board()
    k = 8
    seen: "list[list[Any]]" = [[] for _ in range(k)]
    _subscribe(seen[0])
    assert held.entered.wait(timeout=JOIN_SECONDS), "the prime never started"
    for nth in range(1, k):
        _finishes_while_held(lambda nth=nth: _subscribe(seen[nth]), held)
    assert all(got == [] for got in seen), "nothing is delivered before the prime ends"
    held.release.set()
    assert slot_prime.drain(timeout=JOIN_SECONDS)
    assert len(held.calls) == 1, f"{len(held.calls)} cold folds for one cell"
    assert slot_prime.primes_run(SLOT, "work") == 1
    expected = dict(_expected_value())
    for got in seen:
        assert len(got) == 1 and dict(got[0].value) == expected
    assert streamed["caller-under-test"] == 0 and streamed["MainThread"] == 0


def test_a_warm_subscribe_gets_its_baseline_synchronously_and_primes_nothing(streamed):
    _board()
    _subscribe([])
    assert slot_prime.drain(timeout=JOIN_SECONDS)
    handle = CrewLog.open(lg.KIND_SESSION, UNITS[-1])
    _work(handle, "after-prime")
    streamed.clear()
    seen: "list[Any]" = []
    _subscribe(seen)
    assert len(seen) == 1 and seen[0].revision > 0, "a warm cell hands its baseline"
    assert dict(seen[0].value) == dict(_expected_value())
    assert streamed["MainThread"] == 1, "a warm subscribe reads only the new entry"
    assert not slot_prime.is_priming(SLOT, "work")
    assert slot_prime.primes_run(SLOT, "work") == 1


def test_a_subscriber_that_joins_after_the_publish_reads_the_warm_cell():
    _board()
    _subscribe([])
    assert slot_prime.drain(timeout=JOIN_SECONDS)
    late: "list[Any]" = []
    _subscribe(late)
    assert len(late) == 1 and dict(late[0].value) == dict(_expected_value())


def test_a_failed_prime_is_logged_retried_once_then_backs_off(held, monkeypatch, caplog):
    _board()
    held.release.set()
    held.fail_next = 3
    seen: "list[Any]" = []
    _subscribe(seen)
    assert slot_prime.drain(timeout=JOIN_SECONDS)
    assert len(held.calls) == 1 and seen == []
    assert "crew log slot prime failed" in caplog.text
    # Not wedged: the next subscribe retries exactly once.
    _subscribe(seen)
    assert slot_prime.drain(timeout=JOIN_SECONDS)
    assert len(held.calls) == 2 and seen == []
    # Two failures in a row: further subscribes back off instead of looping.
    for _ in range(5):
        _subscribe(seen)
    assert slot_prime.drain(timeout=JOIN_SECONDS)
    assert len(held.calls) == 2, "a twice-failed cell was primed again inside its backoff"
    # Once the backoff has passed, one more attempt is allowed.
    held.fail_next = 0
    monkeypatch.setattr(slot_prime, "RETRY_BACKOFF_SECONDS", 0.0)
    after: "list[Any]" = []
    _subscribe(after)
    assert slot_prime.drain(timeout=JOIN_SECONDS)
    assert len(held.calls) == 3
    assert len(after) == 1 and dict(after[0].value) == dict(_expected_value())


def test_the_eager_advance_on_a_cold_cell_returns_while_the_prime_is_held(held, streamed):
    """The eager worker is never held by a cold fold, on the first wake or a later one."""
    _board()
    seen: "list[Any]" = []
    bus.subscribe(bus.FOLD_ADVANCED, seen.append, scope=bus.SCOPE_SLOT, key=SLOT, fold="work")
    _finishes_while_held(lambda: eager._advance(SLOT, "work"), held)
    assert held.entered.wait(timeout=JOIN_SECONDS), "no background prime started"
    _finishes_while_held(lambda: eager._advance(SLOT, "work"), held)
    assert streamed["caller-under-test"] == 0 and seen == []
    held.release.set()
    assert slot_prime.drain(timeout=JOIN_SECONDS)
    assert seen and dict(seen[-1].value) == dict(_expected_value())
    assert len(held.calls) == 1


def test_an_entry_landing_during_the_prime_is_published_after_it(held):
    """A request during the prime catches the cell up once it is warm."""
    _board()
    seen: "list[Any]" = []
    _subscribe(seen)
    assert held.entered.wait(timeout=JOIN_SECONDS)
    handle = CrewLog.open(lg.KIND_SESSION, UNITS[-1])
    _work(handle, "during-prime")
    eager._advance(SLOT, "work")  # the wake for that entry, while the prime holds the cell
    held.release.set()
    assert slot_prime.drain(timeout=JOIN_SECONDS)
    assert seen, "nothing was published"
    assert dict(seen[-1].value) == dict(_expected_value()), "the last value missed the entry"
    revisions = [event.revision for event in seen]
    assert revisions == sorted(set(revisions))


def test_a_subscribe_does_not_wait_for_a_cold_read_on_another_thread(held, streamed):
    """A default (cold) read holding the cell's lock costs the subscriber nothing either."""
    _board()
    reader = threading.Thread(target=crew_log.read_slot_projection, args=(SLOT, "work"))
    reader.start()
    assert held.entered.wait(timeout=JOIN_SECONDS), "the route-style cold read never started"
    seen: "list[Any]" = []
    _finishes_while_held(lambda: _subscribe(seen), held)
    assert streamed["caller-under-test"] == 0 and seen == []
    held.release.set()
    reader.join(timeout=JOIN_SECONDS)
    assert slot_prime.drain(timeout=JOIN_SECONDS)
    assert seen and dict(seen[-1].value) == dict(_expected_value())


def test_a_workers_read_does_not_wait_on_a_stripe_held_by_a_cold_fold():
    """The ``workers`` read's own stripe lock follows the same not-ready rule."""
    from kiro_crew.crew_log import projection_readers

    home = str(crew_log.data_home())
    stripe = projection_readers._WORKERS_READ_LOCKS[
        hash((home, SLOT)) % len(projection_readers._WORKERS_READ_LOCKS)
    ]
    cold_key = (home, "some-worker-slot", "worker")
    with stripe:
        with projection_slot_memo._slot_memo_guard:
            projection_slot_memo._cold_keys.add(cold_key)
        try:
            with pytest.raises(projection_slot_memo.SlotFoldNotReady):
                crew_log.read_slot_projection(SLOT, "workers", cold=False)
        finally:
            with projection_slot_memo._slot_memo_guard:
                projection_slot_memo._cold_keys.discard(cold_key)


def test_a_cell_too_big_to_keep_is_primed_for_a_reader_but_not_for_a_wake(monkeypatch):
    """A wake has no reader waiting; a subscriber has no other way to get the value."""
    _board()
    monkeypatch.setattr(projection_slot_memo, "slot_fold_over_ceiling", lambda slot, name: True)
    assert slot_prime.schedule(SLOT, "work") is False, "an eager wake primed an unkept cell"
    seen: "list[Any]" = []
    _subscribe(seen)
    assert slot_prime.drain(timeout=JOIN_SECONDS)
    assert slot_prime.primes_run(SLOT, "work") == 1
    assert len(seen) == 1 and dict(seen[0].value) == dict(_expected_value())


def test_a_unit_removal_waits_for_a_running_prime(held):
    """``eager.paused`` covers a prime's open unit files, without the eager gate."""
    _board()
    _subscribe([])
    assert held.entered.wait(timeout=JOIN_SECONDS)
    acquired = slot_prime._fold_gate.acquire(blocking=False)
    if acquired:  # pragma: no cover - the prime must be holding it
        slot_prime._fold_gate.release()
    assert not acquired, "the prime reads unit files without its gate"
    assert eager._fold_gate.acquire(blocking=False), "the prime held up the eager worker"
    eager._fold_gate.release()
    held.release.set()
    with eager.paused() as quiet:
        assert quiet, "the removal pause did not get both gates once the prime ended"
        assert slot_prime._fold_gate.locked(), "the pause does not hold the prime gate"


def test_a_one_shot_field_read_waits_for_the_prime_instead_of_reporting_missing(held):
    """``DashboardFeed.wait_for_primes``: a one-shot read gets the primed value."""
    from kiro_crew.dashboard_feed import DashboardFeed

    _board()
    feed = DashboardFeed(SLOT, "")
    feed._folds = frozenset({"work"})
    with feed._lock:
        from kiro_crew.dashboard_feed import _Cell

        feed._cells.setdefault("work", _Cell())
    feed._disposers.append(
        bus.subscribe(
            bus.FOLD_ADVANCED,
            feed._make_callback("work"),
            scope=bus.SCOPE_SLOT,
            key=SLOT,
            fold="work",
            baseline=True,
        )
    )
    assert held.entered.wait(timeout=JOIN_SECONDS)
    waiter = threading.Thread(target=feed.wait_for_primes)
    waiter.start()
    waiter.join(timeout=0.2)  # flake-ok: a correct wait stays blocked until release below
    assert waiter.is_alive(), "the one-shot read did not wait for the held prime"
    held.release.set()
    waiter.join(timeout=JOIN_SECONDS)
    assert not waiter.is_alive()
    with feed._lock:
        cell = feed._cells["work"]
    assert cell.seen and dict(cell.value) == dict(_expected_value())
    feed.unsubscribe()


def test_a_request_during_the_catch_up_gets_another_publish(held, monkeypatch):
    """A subscriber whose request lands while the catch-up publishes is not dropped."""
    _board()
    seen: "list[Any]" = []
    _subscribe(seen)  # the cold prime, held
    assert held.entered.wait(timeout=JOIN_SECONDS)
    late: "list[Any]" = []
    original = eager._publish_fold
    calls = {"n": 0}

    def publish_then_join(slot, name, folded):
        calls["n"] += 1
        original(slot, name, folded)
        if calls["n"] == 2:
            # Inside the first catch-up's publish: a new subscriber joins on a cell that
            # still answers not-ready to it only through the rerun flag.
            with slot_prime._lock:
                slot_prime._state[slot_prime._key(SLOT, "work")].rerun = True
            late.append("asked")

    monkeypatch.setattr(eager, "_publish_fold", publish_then_join)
    with slot_prime._lock:
        # A request during the held cold fold owes one catch-up.
        slot_prime._state[slot_prime._key(SLOT, "work")].rerun = True
    held.release.set()
    assert slot_prime.drain(timeout=JOIN_SECONDS)
    assert late == ["asked"], "the fixture never reached the catch-up publish"
    assert calls["n"] >= 3, "the request during the catch-up got no further publish"
    assert not slot_prime.is_priming(SLOT, "work")


def test_a_package_page_arm_waits_for_the_prime_and_paints_its_value(held):
    """``LivePage.arm`` (first paint and one-shot snapshot) carries the primed value."""
    from kiro_crew.artifact_store import dashboard_package as dp
    from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

    _board()
    package = dp.validate_package(
        {
            "kind": "dashboard",
            "bound_to": "crewmate:mate",
            "model": {
                "types": {
                    "omitted": {"type": "number", "source": {"fold": "work", "path": "omitted"}}
                }
            },
            "view": {"html": '<p data-dashboard-field="omitted"></p>'},
            "theme": {"tokens": {}},
        }
    )
    page = mdf.LivePage(SLOT, SLOT, package, lambda _frame: None)
    out: "list[Any]" = []
    armer = threading.Thread(target=lambda: out.append(page.arm()))
    try:
        armer.start()
        assert held.entered.wait(timeout=JOIN_SECONDS), "no background prime started"
        armer.join(timeout=0.2)  # flake-ok: a correct arm stays blocked until release below
        assert armer.is_alive(), "arm painted before the prime delivered the value"
        held.release.set()
        armer.join(timeout=JOIN_SECONDS)
        assert not armer.is_alive() and out
        assert out[0]["fields"]["omitted"] == _expected_value()["omitted"]
    finally:
        held.release.set()
        page.disarm()
