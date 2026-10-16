"""The final cycle's bound: reserved, derived from the client, and enforced on the wire.

Items 9, 11 and 12 of the list in ``test_sidecar_durability_gaps.py``, with the groups that
grew from them. The final cycle has a deadline, so every step it takes has to fit inside it:
the deadline is anchored where the supervisor starts counting, the index gets its own room,
the per-object reservation is derived from what the client is configured to spend, the drain
gate cuts off PUTs rather than objects that need none, and the window the gate reserves is
handed to the transmission and stops it. An interval cycle has no deadline but is told about
the stop, and its uploads and preflight reads are cut by that instead.
"""

from __future__ import annotations

import pathlib
import threading
import time
from collections.abc import Callable

import pytest
from container.common import config as cfg
from container.common import keys, objects
from container.sidecar import __main__ as sidecar_main
from container.sidecar import backup as backup_mod
from container.sidecar import generation as generation_mod

from .sidecar_durability_gaps_helpers import (
    STEM,
    _blob_key,
    _ClockProxy,
    _Recorder,
    _settings,
    _transcript,
)

# --- 9. the index gets its own room inside the deadline ---------------------------


def test_the_index_is_not_left_to_run_past_the_window(tmp_path):
    """The data phase stops early enough that the authority pair has room to publish.

    A data phase allowed to spend the whole deadline reaches the end with nothing left for
    the index, and those PUTs are then killed mid-request -- publishing one file and not
    the other, which is the torn index the two-phase order exists to avoid.
    """
    reserved = backup_mod._reserve_for_authority(1000.0, 2)

    assert reserved is not None
    assert reserved < 1000.0


def test_an_interval_cycle_reserves_nothing_because_it_has_no_deadline():
    """Reserving against no deadline would invent one."""
    assert backup_mod._reserve_for_authority(None, 2) is None


# --- 11. a bound the transport does not honour is not a bound ---------------------


def test_the_per_object_budget_covers_every_attempt_the_client_may_make():
    """Reserved short, the gate admits an upload the drain window cannot finish.

    The timeout bounds the connect and the read separately, so one attempt can spend it
    twice, and standard mode waits between attempts. A budget that counts one timeout per
    attempt is therefore under the real worst case by more than half, and the object it
    waves through is killed mid-PUT.
    """
    waits = 2 ** (cfg.BACKUP_MAX_ATTEMPTS - 1) - 1
    spendable = cfg.BACKUP_MAX_ATTEMPTS * 2 * cfg.BACKUP_REQUEST_TIMEOUT_SECS + waits

    assert cfg.BACKUP_ATTEMPT_COST_SECS == 2 * cfg.BACKUP_REQUEST_TIMEOUT_SECS
    assert cfg.BACKUP_PER_OBJECT_BUDGET_SECS >= spendable


def test_the_client_spends_only_the_constants_the_budget_is_derived_from():
    """A timeout or an attempt count written into the store would make the budget a guess.

    The KEY NAME is part of the assertion, not decoration, and it is the whole premise the
    budget rests on. Measured against boto3/botocore 1.42.91 by counting the requests a
    client actually sends to a closed port: ``max_attempts: 1`` sends TWO and resolves to
    ``total_max_attempts: 2``, while ``total_max_attempts: 1`` sends ONE; at three they send
    four and three. So the retries spelling buys one attempt more than the budget reserves --
    at one attempt, this gate admits an upload on ten seconds that the transport may spend
    twenty-one on.

    Asserted as source text rather than by building a client, because botocore is absent from
    the runners this suite collects on: a client-building test would SKIP on every one of
    them, which is no guard at all in the only place the drift can land.
    """
    from container.sidecar import store as store_mod

    src = pathlib.Path(store_mod.__file__).read_text(encoding="utf-8")

    assert "connect_timeout=BACKUP_REQUEST_TIMEOUT_SECS" in src
    assert "read_timeout=BACKUP_REQUEST_TIMEOUT_SECS" in src
    assert '"total_max_attempts": BACKUP_MAX_ATTEMPTS' in src
    assert '"max_attempts"' not in src


def test_the_window_still_fits_one_transcript_after_the_authority_reservation():
    """The reservation is only sound if a whole PUT still fits in what it leaves.

    Three authority objects -- the pair plus the generation pointer -- come out of the
    window before the data phase starts. Reserve more than the window can spare and a
    transcript does not fit in the remainder, so the final cycle publishes an index and not
    one conversation, which is a cycle doing nothing while reporting that it ran.
    """
    deadline = time.monotonic() + cfg.SIDECAR_DRAIN_SECS
    reserved = backup_mod._reserve_for_authority(deadline, len(keys.AUTHORITY_NAMES) + 1)

    assert reserved is not None
    assert backup_mod._time_for_one_more(reserved, cfg.BACKUP_PER_OBJECT_BUDGET_SECS)


def test_the_authority_reservation_sets_aside_a_whole_attempt_per_object():
    """One timeout is HALF an attempt, because the connect and the read are bounded apart.

    Reserved at one timeout per object, the index phase starts its last PUT with five
    seconds against an attempt that can spend ten, and the kill lands mid-request -- which
    publishes one file of the pair and not the other, the exact state the two-phase order
    and the generation pointer exist to make unreachable.
    """
    deadline = time.monotonic() + 1000.0
    reserved = backup_mod._reserve_for_authority(deadline, 3)

    assert reserved is not None
    assert deadline - reserved == pytest.approx(3 * cfg.BACKUP_ATTEMPT_COST_SECS)


def test_a_withheld_authority_phase_reserves_no_window_for_puts_it_will_not_make(tmp_path):
    """The reservation must match the phase that RUNS, not the phase that might have.

    When the pointer cannot be read there is no committed slot, so the authority pair is
    withheld this cycle and its PUTs do not happen. Reserving a whole attempt per authority
    object anyway carves that time off the data phase for uploads that never run, shrinking
    what the transcripts get for no gain. So the count handed to the reservation is zero on
    the withheld path and the pair-plus-pointer only when the phase will actually publish.

    The proof spies the count the reservation is called with: a healthy final cycle reserves
    ``len(authority) + 2`` (the pair, the transcript index, and the pointer); the same cycle
    with an unreadable pointer reserves ``0``.
    """
    counts: list[int] = []
    real_reserve = backup_mod._reserve_for_authority

    def _spy(deadline, count):
        counts.append(count)
        return real_reserve(deadline, count)

    # Healthy final cycle: the phase runs, so it reserves the pair, the index, and the pointer.
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(backup_mod, "_reserve_for_authority", _spy)
        backup_mod.run_cycle(settings, _Recorder(), state={}, deadline=time.monotonic() + 3600)
    healthy = counts[-1]
    assert healthy == len(keys.AUTHORITY_NAMES) + 2

    # Same final cycle, pointer unreadable: the pair is withheld, so it reserves nothing.
    settings2 = _settings(tmp_path / "second")
    _transcript(settings2, b"a turn\n")
    pointer = keys.authority_pointer_key(settings2)

    class _PointerUnreadable(_Recorder):
        def get(
            self,
            key: str,
            *,
            limit: int,
            deadline: float | None = None,
            cancel: Callable[[], bool] | None = None,
        ) -> bytes:
            assert key == pointer
            raise RuntimeError("InternalError")

    counts.clear()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(backup_mod, "_reserve_for_authority", _spy)
        with pytest.raises(backup_mod.BackupIncomplete):
            backup_mod.run_cycle(
                settings2, _PointerUnreadable(), state={}, deadline=time.monotonic() + 3600
            )
    assert counts[-1] == 0, "a withheld authority phase must reserve no window"


def test_the_index_phase_will_not_start_a_put_it_has_half_an_attempt_for(tmp_path):
    """Enough for the read alone is not enough: the connect can spend the whole timeout."""
    settings = _settings(tmp_path)
    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    try:
        backup_mod._commit_authority(
            plan.authority,
            settings=settings,
            store=_Recorder(),
            state={},
            result=result,
            deadline=time.monotonic() + cfg.BACKUP_ATTEMPT_COST_SECS - 3,
        )
    finally:
        plan.close_authority()

    assert [name for name, _why in result.refused] == [
        key.rsplit("/", 1)[-1] for key, _snapshot in plan.authority
    ]
    assert result.withheld == [key for key, _snapshot in plan.authority]


def test_the_pointer_is_not_started_with_less_than_one_attempt_left(tmp_path):
    """The pointer is the commit, so a kill mid-PUT is the one write nothing can repair."""
    settings = _settings(tmp_path)
    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    try:
        backup_mod._commit_generation(
            plan,
            settings=settings,
            store=_Recorder(),
            state={},
            result=result,
            generation_id=plan.generation_id,
            deadline=time.monotonic() + cfg.BACKUP_ATTEMPT_COST_SECS - 3,
        )
    finally:
        plan.close_authority()

    assert [key for key, _why in result.refused] == [keys.authority_pointer_key(settings)]


def test_the_index_phase_stops_rather_than_publishing_half_the_pair(tmp_path):
    """Both files, or neither: a half-new pair disagrees with itself about the slots."""
    settings = _settings(tmp_path)
    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    try:
        backup_mod._commit_authority(
            plan.authority,
            settings=settings,
            store=_Recorder(),
            state={},
            result=result,
            deadline=time.monotonic() - 1,
        )
    finally:
        plan.close_authority()

    assert len(result.withheld) == len(keys.AUTHORITY_NAMES)
    assert result.uploaded == []


def test_the_index_upload_is_budgeted_and_cancellable_under_a_deadline(tmp_path):
    """A slow index trickle must not outlast the drain window and lose the newest history.

    The transcript index can grow to the module's entry/byte caps, so the final cycle's
    index PUT is not the short write the old comment claimed. Under a deadline it carries
    the one-attempt budget the sibling pair PUTs and the admission check already use, plus
    the cycle's yield check as a cancel, so a kill mid-upload is prevented rather than
    discovered. With no deadline (an interval cycle) it stays unbounded, like the pointer.
    """

    class _BudgetRecorder(_Recorder):
        def __init__(self) -> None:
            super().__init__()
            self.budgets: dict[str, float | None] = {}
            self.cancels: dict[str, object] = {}

        def put(self, key, body, size, *, budget=None, cancel=None, **kw):
            self.budgets[key] = budget
            self.cancels[key] = cancel
            super().put(key, body, size, **kw)

    settings = _settings(tmp_path)
    gen_id = keys.new_generation_id()
    index_key = keys.transcript_index_key(settings, gen_id)
    sentinel_cancel: Callable[[], bool] = lambda: False  # noqa: E731

    # Under a deadline: budgeted at one attempt and cancellable with the cycle's check.
    store = _BudgetRecorder()
    result = backup_mod.CycleResult()
    backup_mod._commit_transcript_index(
        {STEM: f"gen/{gen_id}/blob/abc"},
        settings=settings,
        store=store,
        state={},
        result=result,
        generation_id=gen_id,
        deadline=time.monotonic() + 3600,
        cancel=sentinel_cancel,
    )
    assert index_key in store.puts, "the index was published"
    assert store.budgets[index_key] == cfg.BACKUP_ATTEMPT_COST_SECS
    assert store.cancels[index_key] is sentinel_cancel

    # No deadline (interval cycle): unbounded, matching the pointer's own rationale.
    store2 = _BudgetRecorder()
    result2 = backup_mod.CycleResult()
    backup_mod._commit_transcript_index(
        {STEM: f"gen/{gen_id}/blob/abc"},
        settings=settings,
        store=store2,
        state={},
        result=result2,
        generation_id=gen_id,
        deadline=None,
        cancel=sentinel_cancel,
    )
    assert store2.budgets[index_key] is None


# --- 12. the drain deadline is anchored where the supervisor starts counting -------


def test_the_final_deadline_is_measured_from_when_the_stop_was_observed():
    """The supervisor counts ``SIDECAR_DRAIN_SECS`` from SIGTERM delivery, so this must too.

    A cycle already in flight when the signal lands keeps running, so a deadline computed
    when the loop next looks at the flag would start counting a window already partly
    spent -- and the final cycle would be killed mid-PUT believing it had time.
    """
    observed = time.monotonic() - 30.0

    from_stop = sidecar_main._drain_deadline(observed)
    from_now = sidecar_main._drain_deadline(None)

    assert from_stop < from_now
    assert from_stop == pytest.approx(observed + cfg.SIDECAR_DRAIN_SECS)


def test_the_signal_handler_records_the_first_instant_only(monkeypatch):
    """A second signal must not push the deadline out; the window started at the first."""
    monkeypatch.setattr(sidecar_main, "_STOP_OBSERVED", [])
    monkeypatch.setattr(sidecar_main, "_STOP", threading.Event())

    sidecar_main._on_signal(15, None)
    first = sidecar_main._STOP_OBSERVED[0]
    sidecar_main._on_signal(15, None)

    assert sidecar_main._STOP_OBSERVED == [first]


def test_an_interval_cycle_stands_down_when_the_stop_arrives(tmp_path):
    """Its uploads predate the flush, so the final cycle takes them anyway.

    Continuing would only spend the drain window that cycle needs, which is the window
    the whole ordering exists to protect.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    _transcript(settings, b"another\n", stem="dashboard_cust-92")
    store = _Recorder()

    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, store, state={}, yield_when=lambda: True)

    assert store.objects == {}
    assert {name for name, _why in caught.value.result.refused}


def test_the_final_cycle_does_not_stand_down_on_the_flag_that_made_it_final(tmp_path):
    """It is the cycle whose uploads matter, so it runs to its deadline instead."""
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    store = _Recorder()

    result = backup_mod.run_cycle(settings, store, state={}, deadline=time.monotonic() + 3600)

    assert result.refused == []
    assert store.objects


# --- 12. the drain gate cuts off PUTs, not objects that need none -----------------


def test_an_already_durable_object_is_unchanged_rather_than_refused(tmp_path):
    """A drain whose objects are already durable must not report itself as a lossy one.

    The gate exists to stop a PUT that cannot finish. An object the bucket already holds
    needs no PUT, so cutting it off records a refusal for work that was never owed -- and a
    refusal withholds the authority pair, fails the cycle and exits the task non-zero. Under
    content addressing the key IS the digest, so confirming durability requires hashing the
    object first; with the window still open the hash runs, the object is found durable, and
    it is recorded unchanged rather than refused. (A deadline so tight the hash itself cannot
    fit is the hashing-deadline case, pinned separately -- there the object is named, because
    the window is already blown.)
    """
    settings = _settings(tmp_path)
    path = _transcript(settings, b"a turn\n")
    plan = backup_mod.objects_to_back_up(settings)
    # The object is durable under its CONTENT-ADDRESSED key, so that is what state is seeded
    # with -- marking exactly these bytes already in the bucket.
    key = _blob_key(settings, b"a turn\n")
    try:
        snapshot = backup_mod.open_snapshot(path, root=settings.data_home)
        assert snapshot is not None
        try:
            state = {key: snapshot.fingerprint}
        finally:
            snapshot.close()
        result = backup_mod.CycleResult()
        backup_mod._upload_phase(
            plan.data,
            settings=settings,
            store=_Recorder(),
            state=state,
            result=result,
            committed_index={},
            # Window open: the hash fits, so durability is confirmed and no PUT is owed.
            deadline=time.monotonic() + cfg.SIDECAR_DRAIN_SECS,
        )
    finally:
        plan.close_authority()

    assert result.unchanged == [key]
    assert result.refused == []
    assert result.complete


def test_an_object_that_does_need_a_put_is_still_cut_off(tmp_path):
    """The mirror, so the hoist above did not simply disable the gate."""
    settings = _settings(tmp_path)
    path = _transcript(settings, b"a turn\n")
    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    try:
        backup_mod._upload_phase(
            plan.data,
            settings=settings,
            store=_Recorder(),
            state={},
            result=result,
            committed_index={},
            deadline=time.monotonic() - 1,
        )
    finally:
        plan.close_authority()

    assert [name for name, _why in result.refused] == [path.name]
    assert result.unchanged == []


# --- 12. the reserved window is enforced on the transmission ------------------------
#
# The gate reserves BACKUP_PER_OBJECT_BUDGET_SECS and then admits the PUT. Nothing
# inside the PUT honoured that number: connect_timeout and read_timeout bound one
# connect and one read, and a bucket delivering small chunks under the read timeout
# trips neither however long the whole object takes. So the only thing that ended a
# slow upload was the drain window's SIGKILL, arriving mid-request -- losing that
# object and saying nothing about the ones behind it, which is the exact outcome the
# deadline was built to replace.


class _Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, secs: float) -> None:
        self.now += secs


def test_a_body_past_its_window_stops_sending(tmp_path):
    """The read is refused, so a progressing-but-slow transmission is cut.

    The whole point is that this happens while the connection is healthy: every chunk
    arrives, none of them late enough to trip a socket timeout, and the object is still
    not going to finish. Reading is what the transport does repeatedly during a send, so
    it is the one place a request can be stopped mid-flight.
    """
    path = tmp_path / "body.bin"
    path.write_bytes(b"x" * 64)
    clock = _Clock()
    with path.open("rb") as fh:
        reader = objects.BoundedReader(fh, 64, deadline=clock.now + 10.0, clock=clock)
        assert reader.read(8) == b"x" * 8
        assert not reader.expired()
        clock.advance(10.0)
        assert reader.expired()
        with pytest.raises(objects.UploadDeadlineExceeded) as caught:
            reader.read(8)

    # The message names what did not go, because a cut upload's value is telling the
    # operator which bytes are missing.
    assert "56 of 64 B unsent" in str(caught.value)


def test_a_rewind_does_not_refresh_the_window(tmp_path):
    """A retry is part of getting this object there, not a second allowance.

    botocore seeks the body back to its start before re-sending, so a deadline reset on
    seek would let ``attempts`` slow attempts each spend the whole window -- the gate
    would reserve one object's worth of time and the object could spend several.
    """
    path = tmp_path / "body.bin"
    path.write_bytes(b"y" * 32)
    clock = _Clock()
    with path.open("rb") as fh:
        reader = objects.BoundedReader(fh, 32, deadline=clock.now + 5.0, clock=clock)
        assert reader.read(4) == b"y" * 4
        clock.advance(5.0)
        reader.seek(0)
        assert reader.tell() == 0, "the rewind itself still works"
        with pytest.raises(objects.UploadDeadlineExceeded):
            reader.read(4)


class _BudgetRecorder(_Recorder):
    """Remembers the window it was handed for each object."""

    def __init__(self) -> None:
        super().__init__()
        self.budgets: list[float | None] = []

    def put(
        self,
        key: str,
        body,
        size: int,
        *,
        budget: float | None = None,
        cancel: Callable[[], bool] | None = None,
        if_match: str | None = None,
        if_none_match: str | None = None,
    ) -> None:
        self.budgets.append(budget)
        super().put(key, body, size)


def test_the_final_cycle_hands_the_store_the_window_it_reserved(tmp_path):
    """One number, not two that agree only when the network is fast.

    The gate admits the object by reserving ``BACKUP_PER_OBJECT_BUDGET_SECS``, so that is
    what the PUT must be bounded by. Reserving one number and bounding by another is how
    a gate comes to admit an upload the window cannot hold.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    store = _BudgetRecorder()
    try:
        backup_mod._upload_phase(
            plan.data,
            settings=settings,
            store=store,
            state={},
            result=result,
            committed_index={},
            deadline=time.monotonic() + 600.0,
        )
    finally:
        plan.close_authority()

    assert store.budgets == [cfg.BACKUP_PER_OBJECT_BUDGET_SECS]
    assert result.complete


def test_an_interval_cycle_hands_the_store_no_window(tmp_path):
    """The over-strict direction, and the reason this is not bounded everywhere.

    An interval cycle has no drain window to overrun and a next cycle to finish the
    object. Bounding it there would refuse an object slower than the window FOREVER --
    a 64 MiB transcript needs about 6.7 MB/s to fit -- so a slow link would never get
    one into the bucket at all. On the final cycle the alternative is losing it
    silently, which is why the bound belongs only there.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    store = _BudgetRecorder()
    try:
        backup_mod._upload_phase(
            plan.data, settings=settings, store=store, state={}, result=result, committed_index={}
        )
    finally:
        plan.close_authority()

    assert store.budgets == [None]


def test_an_upload_cut_by_its_window_is_refused_rather_than_lost(tmp_path):
    """The gain over the SIGKILL: the object is named, and the index does not move.

    A kill mid-PUT loses the object being sent and every object after it, with nothing
    recorded. A cut upload is an ordinary refusal: it withholds the authority pair, so
    the index still describes a state the bucket supports, and the cycle is incomplete
    so the final cycle's exit code reports the loss.
    """
    settings = _settings(tmp_path)
    path = _transcript(settings, b"a turn\n")

    class _TooSlow(_Recorder):
        def put(
            self,
            key: str,
            body,
            size: int,
            *,
            budget: float | None = None,
            cancel: Callable[[], bool] | None = None,
            if_match: str | None = None,
            if_none_match: str | None = None,
        ) -> None:
            raise objects.UploadDeadlineExceeded(
                f"PutObject on {key} did not finish inside its {budget:.0f}s budget"
            )

    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    try:
        backup_mod._upload_phase(
            plan.data,
            settings=settings,
            store=_TooSlow(),
            state={},
            result=result,
            committed_index={},
            deadline=time.monotonic() + 600.0,
        )
    finally:
        plan.close_authority()

    assert [name for name, _why in result.refused] == [path.name]
    assert not result.complete
    # Refused, NOT unreachable: the bucket answered and the transport worked, so the next
    # cycle attempts this object again. Classing it permanent would end the process over
    # a slow network.
    assert result.unreachable == []


def test_a_cut_upload_is_not_read_as_a_permanent_fault(tmp_path):
    """Through the real store, with a client that drains the body slowly.

    ``StoreUnusable`` ends the process, so classifying this as permanent would turn a
    slow bucket into a crash -- and the sidecar would stop backing anything up at all.
    The store is asked to judge by its own reader's clock rather than by the exception
    that surfaced, because the transport owns what a body raising mid-send looks like.
    """
    from container.sidecar import store as store_mod
    from container.sidecar.store import S3ObjectStore, StoreUnusable

    path = tmp_path / "big.jsonl"
    path.write_bytes(b"z" * 4096)
    clock = _Clock()

    class _FakeTime:
        """Stands in for the ``time`` module at both sites that read the clock."""

        monotonic = staticmethod(clock)

    class _SlowClient:
        """Reads the body in small chunks, spending clock time on each one.

        Every read succeeds and none of them is slow enough to trip a socket timeout.
        This is the shape the finding names: healthy, progressing, and never finishing.
        """

        def put_object(self, **kw):  # pragma: no cover - raises before it returns
            body = kw["Body"]
            while body.read(16):
                clock.advance(1.0)
            return {}

    with pytest.MonkeyPatch.context() as mp:
        # Both, because the deadline and the reader that enforces it read the clock in
        # different modules: patching only one leaves a real reading on the other side of
        # the comparison and the deadline can never be reached.
        mp.setattr(store_mod, "time", _FakeTime)
        mp.setattr(objects, "time", _FakeTime)
        store = S3ObjectStore("a-bucket", client=_SlowClient())
        with path.open("rb") as fh:
            with pytest.raises(objects.UploadDeadlineExceeded) as caught:
                store.put("crews/c/big.jsonl", fh, 4096, budget=10.0)

    assert not isinstance(caught.value, StoreUnusable)
    # Derived, not written: the BODY's share of a budget is the budget less the response wait
    # the body cannot bound, so a count written here would go stale the moment that split
    # moves. One chunk goes per simulated second, and the read after the share elapses is the
    # one refused.
    sent = 16 * int(10.0 - cfg.BACKUP_REQUEST_TIMEOUT_SECS)
    assert f"{4096 - sent} of 4096 B unsent" in str(caught.value)


def test_a_transport_that_swallows_the_body_error_is_still_read_as_the_window(tmp_path):
    """The store judges by its own reader's clock, not by what came out of the transport.

    A body raising mid-send is not guaranteed to surface as itself: botocore may wrap it
    in a connection error, or convert it into a retry that then fails on its own. Judged
    by the exception, an expired window would reach ``classify_permanent`` and could end
    the process over a slow network. Judged by the clock, it is the window either way.
    """
    from container.sidecar import store as store_mod
    from container.sidecar.store import S3ObjectStore, StoreUnusable

    path = tmp_path / "big.jsonl"
    path.write_bytes(b"z" * 4096)
    clock = _Clock()

    class _FakeTime:
        monotonic = staticmethod(clock)

    class _SwallowingClient:
        """Drains until the body objects, then reports a generic transport fault."""

        def put_object(self, **kw):
            body = kw["Body"]
            try:
                while body.read(16):
                    clock.advance(1.0)
            except objects.UploadDeadlineExceeded as exc:
                raise ConnectionError("connection reset by peer") from exc
            return {}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(store_mod, "time", _FakeTime)
        mp.setattr(objects, "time", _FakeTime)
        store = S3ObjectStore("a-bucket", client=_SwallowingClient())
        with path.open("rb") as fh:
            with pytest.raises(objects.UploadDeadlineExceeded) as caught:
                store.put("crews/c/big.jsonl", fh, 4096, budget=10.0)

    assert not isinstance(caught.value, StoreUnusable)
    assert "did not finish inside its 10s budget" in str(caught.value)
    # The transport's own error is kept as the cause, so the log still shows what the
    # connection reported.
    assert isinstance(caught.value.__cause__, ConnectionError)


def test_a_final_cycle_get_is_cut_when_the_drain_deadline_expires(tmp_path):
    """A trickling preflight GET is refused once the cycle deadline is past, not SIGKILLed.

    The final cycle's pointer and transcript-index reads run inside the supervisor's drain
    window. The socket's read timeout a chunk-trickling connection never trips cannot bound
    them, so a large index on a merely degraded link would outlive the window and be
    SIGKILLed mid-read -- the final commit never happens and the flushed turns are lost.
    Threading the cycle deadline into the GET wraps the body in a deadline-bearing reader
    that cuts the read the moment the window is spent. This drives a GET whose body trickles
    one chunk per simulated second against a deadline already in the near past and asserts the
    read raises :class:`~container.common.objects.UploadDeadlineExceeded`.

    It fails if the deadline is not threaded into the GET: the body reads to its end and the
    too-slow stream is never cut.
    """
    from container.sidecar import store as store_mod
    from container.sidecar.store import S3ObjectStore

    clock = _Clock()

    class _FakeTime:
        """Patched at both clock-reading sites, as the PUT deadline tests are."""

        monotonic = staticmethod(clock)

    class _TricklingBody:
        """A GET body that yields one small chunk per read, spending clock time on each."""

        def __init__(self) -> None:
            self._remaining = 4096

        def read(self, amt: int = -1) -> bytes:
            if self._remaining <= 0:
                return b""
            clock.advance(1.0)
            take = 16 if amt is None or amt < 0 else min(amt, 16)
            take = min(take, self._remaining)
            self._remaining -= take
            return b"z" * take

    class _SlowGetClient:
        def get_object(self, **kw):  # noqa: N803 - botocore casing
            return {"Body": _TricklingBody(), "ETag": '"etag"', "ContentLength": 4096}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(store_mod, "time", _FakeTime)
        mp.setattr(objects, "time", _FakeTime)
        store = S3ObjectStore("a-bucket", client=_SlowGetClient())
        # A deadline five simulated seconds out: the trickling body spends one second per
        # chunk, so the read is cut long before the 4096 bytes arrive.
        deadline = clock() + 5.0
        with pytest.raises(objects.UploadDeadlineExceeded):
            store.get_with_etag(
                "crews/c/gen/x/transcript_index.json", limit=1 << 26, deadline=deadline
            )


# --- 13. an interval upload does not outlive the stop it was told about --------------


def test_a_body_cut_by_a_stop_stops_sending(tmp_path):
    """The unbounded body is still endable, which is what makes it safe to leave unbounded.

    An interval cycle passes no deadline on purpose, so time cannot end its upload. The
    stop can: the transport asks the body for a chunk many times during a send, so the
    predicate is asked there too and the request ends while it is still progressing.
    """
    path = tmp_path / "body.bin"
    path.write_bytes(b"x" * 64)
    stopping = threading.Event()
    with path.open("rb") as fh:
        reader = objects.BoundedReader(fh, 64, cancel=stopping.is_set)
        assert reader.read(8) == b"x" * 8
        assert not reader.cancelled()
        stopping.set()
        assert reader.cancelled()
        with pytest.raises(objects.UploadCancelled) as caught:
            reader.read(8)

    # Named, like the window refusal, because the value of cutting an upload is knowing
    # which bytes did not go.
    assert "56 of 64 B unsent" in str(caught.value)


def test_the_stop_is_checked_before_the_read_not_after(tmp_path):
    """Waiting for one more chunk is the cost this exists to avoid.

    Checked after the read, the cut still waits on the connection that the shutdown cannot
    afford to wait for -- and a chunk the transport never sends is an unbounded wait.
    """
    path = tmp_path / "body.bin"
    path.write_bytes(b"x" * 32)
    stopping = threading.Event()
    stopping.set()
    reads: list[int] = []

    class _CountingFile:
        def __init__(self, fh):
            self._fh = fh

        def read(self, amt):
            reads.append(amt)
            return self._fh.read(amt)

        def tell(self):
            return self._fh.tell()

        def seekable(self):
            return True

        def seek(self, offset, whence=0):
            return self._fh.seek(offset, whence)

    with path.open("rb") as fh:
        reader = objects.BoundedReader(_CountingFile(fh), 32, cancel=stopping.is_set)
        with pytest.raises(objects.UploadCancelled):
            reader.read(4)

    assert reads == [], "the refusal must not consume a chunk first"


def test_a_large_read_rechecks_the_stop_between_underlying_slices(tmp_path):
    """GPT F2: a single big read() is cut mid-transfer, not only before it starts.

    read_bounded asks for GET_CHUNK_BYTES (1 MiB) per read. If BoundedReader issued one
    underlying read of the whole amount, a link trickling below the socket read timeout would
    block there for the whole chunk -- past the drain window -- with the deadline/cancel checks
    (which run only between reads) never getting a turn. The read is served in slices of at
    most _READ_SLICE_BYTES with a check between each, so a stop that arrives DURING the read
    cuts it after the next slice rather than after the whole amount.

    It fails if read() issues one underlying read for the whole `want` (the mutation: the stop
    is only seen before the read, so a big read runs to completion despite a mid-read stop).
    """
    slice_n = objects._READ_SLICE_BYTES
    total = slice_n * 4
    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * total)
    stopping = threading.Event()
    slices: list[int] = []

    class _SlicingFile:
        def __init__(self, fh):
            self._fh = fh

        def read(self, amt):
            slices.append(amt)
            # The stop arrives after the FIRST underlying slice is served.
            if len(slices) == 1:
                stopping.set()
            return self._fh.read(amt)

        def tell(self):
            return self._fh.tell()

        def seekable(self):
            return True

        def seek(self, offset, whence=0):
            return self._fh.seek(offset, whence)

    with path.open("rb") as fh:
        reader = objects.BoundedReader(_SlicingFile(fh), total, cancel=stopping.is_set)
        with pytest.raises(objects.UploadCancelled):
            reader.read(total)

    # Each underlying read is bounded by the slice size, and the whole `want` is NOT read in
    # one call: the stop set after the first slice cut the read before the remaining slices.
    assert all(n <= slice_n for n in slices), slices
    assert len(slices) == 1, "the stop after the first slice must cut before the next read"


def test_a_slice_read_that_blocks_is_cut_mid_flight_by_the_stop(tmp_path):
    """GPT F1: a single underlying slice read is interruptible WHILE it is in flight.

    The between-slice checks do not bound a read that is itself blocking: a link trickling
    below the socket read timeout keeps one ``self._fh.read(64 KiB)`` alive for far longer
    than the drain window with no check reachable inside it. The slice read now runs on a
    worker thread the caller waits on in short steps, so a stop that arrives WHILE the read is
    blocked cuts it mid-flight rather than after the whole slice arrives.

    It fails if the slice read is issued directly on the calling thread (the mutation): the
    stop set during the block is never seen until the blocked read returns, so ``read`` would
    hang on the never-returning read instead of raising promptly.
    """
    release = threading.Event()
    stopping = threading.Event()
    entered = threading.Event()

    class _TricklingFile:
        def read(self, amt):
            entered.set()
            # Models a slice read that does not return within the window: it blocks until
            # explicitly released, standing in for a link delivering below the read timeout.
            release.wait(5.0)
            return b"z" * amt

        def tell(self):
            return 0

        def seekable(self):
            return True

        def seek(self, offset, whence=0):
            return 0

    reader = objects.BoundedReader(
        _TricklingFile(), objects._READ_SLICE_BYTES, cancel=stopping.is_set
    )

    def _arm_stop() -> None:
        # Set the stop once the underlying read is in flight (entered), NOT on a wall-clock
        # delay. If the read never starts, the bounded wait makes this thread return rather
        # than hang.
        entered.wait(5.0)
        stopping.set()

    arm = threading.Thread(target=_arm_stop)
    arm.start()
    try:
        with pytest.raises(objects.UploadCancelled):
            reader.read(objects._READ_SLICE_BYTES)
        # The cut landed WHILE the underlying read was still blocked -- `release` is never set
        # in this test, so the only way `read` returned (by raising) is the in-flight read
        # being abandoned, not waited out. This is an ordering fact, independent of how the
        # runner schedules threads: no wall-clock bound can flake it.
        assert not release.is_set(), "cancellation must land before the blocked read returns"
    finally:
        arm.join(5.0)
        release.set()  # let the orphaned worker read finish so no thread lingers


def test_a_rewind_does_not_clear_the_stop(tmp_path):
    """A retry meets the same stop, because the predicate is asked rather than latched.

    botocore seeks the body back to its start before re-sending. A stop cleared on seek
    would let the transport's own retry restart the upload the shutdown just ended, which
    is the whole cost back again.
    """
    path = tmp_path / "body.bin"
    path.write_bytes(b"y" * 32)
    stopping = threading.Event()
    with path.open("rb") as fh:
        reader = objects.BoundedReader(fh, 32, cancel=stopping.is_set)
        assert reader.read(4) == b"y" * 4
        stopping.set()
        reader.seek(0)
        assert reader.tell() == 0, "the rewind itself still works"
        with pytest.raises(objects.UploadCancelled):
            reader.read(4)


def test_a_body_with_no_stop_predicate_reads_to_the_end(tmp_path):
    """The over-strict direction: no predicate is no cut, not an immediate one.

    The final cycle passes none -- the flag is what made it final, so standing down on it
    would refuse the one cycle whose uploads matter.
    """
    path = tmp_path / "body.bin"
    path.write_bytes(b"z" * 16)
    with path.open("rb") as fh:
        reader = objects.BoundedReader(fh, 16)
        assert not reader.cancelled()
        assert reader.read(-1) == b"z" * 16


class _CancelRecorder(_Recorder):
    """Remembers the stop predicate it was handed for each object."""

    def __init__(self) -> None:
        super().__init__()
        self.cancels: list[Callable[[], bool] | None] = []

    def put(
        self,
        key: str,
        body,
        size: int,
        *,
        budget: float | None = None,
        cancel: Callable[[], bool] | None = None,
        if_match: str | None = None,
        if_none_match: str | None = None,
    ) -> None:
        self.cancels.append(cancel)
        super().put(key, body, size)


def test_an_interval_cycle_hands_the_store_the_stop_flag(tmp_path):
    """The gap the window bound leaves open, closed by the other mechanism.

    The between-objects check at the top of the phase cannot see a stop that lands DURING
    a PUT, and an interval PUT has no budget to end it. So the flag goes to the store: the
    upload is cut inside the transmission, the cycle returns, and the final cycle starts
    with the window the supervisor is counting rather than what is left of it.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    store = _CancelRecorder()
    stopping = threading.Event()
    try:
        backup_mod._upload_phase(
            plan.data,
            settings=settings,
            store=store,
            state={},
            result=result,
            committed_index={},
            yield_when=stopping.is_set,
        )
    finally:
        plan.close_authority()

    assert store.cancels == [stopping.is_set]


def test_the_final_cycle_hands_the_store_no_stop_flag(tmp_path):
    """The cycle whose uploads matter is not stood down by the flag that made it final.

    Its bound is the deadline, which is measured from when the stop was observed. Passing
    the flag here would cut every upload of the drain cycle immediately and lose exactly
    the turns the drain exists to save.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    store = _CancelRecorder()
    try:
        backup_mod._upload_phase(
            plan.data,
            settings=settings,
            store=store,
            state={},
            result=result,
            committed_index={},
            deadline=time.monotonic() + 600.0,
            yield_when=None,
        )
    finally:
        plan.close_authority()

    assert store.cancels == [None]


def test_the_authority_phase_is_cut_by_the_stop_too(tmp_path):
    """One invariant, both phases. An index PUT outlives a stop exactly as a transcript can.

    The interval authority phase has no deadline either, so without the flag it is the
    second way an in-flight upload eats the drain window.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    store = _CancelRecorder()
    stopping = threading.Event()
    try:
        backup_mod._commit_authority(
            plan.authority,
            settings=settings,
            store=store,
            state={},
            result=result,
            yield_when=stopping.is_set,
        )
    finally:
        plan.close_authority()

    assert store.cancels, "the authority phase uploaded nothing, so nothing was pinned"
    assert store.cancels == [stopping.is_set] * len(store.cancels)


def test_an_upload_cut_by_the_stop_is_refused_rather_than_lost(tmp_path):
    """Recorded like the window refusal: the pair is withheld and the name is in the log.

    The object is left to the final cycle DELIBERATELY, so the index must not name it yet.
    A refusal is what withholds the pair, and it is transient -- the next cycle sends the
    object rather than the process ending over an ordinary shutdown.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")

    class _Cut(_Recorder):
        def put(
            self,
            key: str,
            body,
            size: int,
            *,
            budget: float | None = None,
            cancel: Callable[[], bool] | None = None,
            if_match: str | None = None,
            if_none_match: str | None = None,
        ) -> None:
            raise objects.UploadCancelled(f"PutObject on {key} was cut by a stop")

    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    try:
        backup_mod._upload_phase(
            plan.data, settings=settings, store=_Cut(), state={}, result=result, committed_index={}
        )
    finally:
        plan.close_authority()

    assert not result.complete
    assert [name for name, _why in result.refused] == [f"{STEM}{keys.TRANSCRIPT_SUFFIX}"]
    assert "cut by the stop" in result.refused[0][1]
    # Transient, so the next cycle attempts it. Classified permanent it would be an
    # unreachable entry and the pair would publish without it.
    assert not result.unreachable


def test_a_cut_index_leaves_the_pointer_where_it_is(tmp_path):
    """Cutting the authority phase must not publish a generation it did not finish.

    The pointer is the LAST step precisely so an interrupted pair leaves the previous
    generation committed and whole. A cut index that still moved the pointer would be the
    torn index the phase order exists to prevent -- worse than the window it saves.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    result.refused.append(("index.json", "the upload was cut by the stop"))
    store = _Recorder()

    try:
        backup_mod._commit_generation(
            plan,
            settings=settings,
            store=store,
            state={},
            result=result,
            generation_id=plan.generation_id,
        )
    finally:
        plan.close_authority()

    assert store.puts == [], "the pointer moved over a cut authority phase"


def test_the_store_reads_a_swallowed_body_error_as_the_stop(tmp_path):
    """Judged by the reader's own state, not by what came out of the transport.

    A body raising mid-send may surface as itself, wrapped in a connection error, or as a
    retry that fails on its own. Judged by the exception, a cut upload could reach
    ``classify_permanent`` and end the process over an ordinary shutdown.
    """
    from container.sidecar import store as store_mod
    from container.sidecar.store import S3ObjectStore, StoreUnusable

    path = tmp_path / "big.jsonl"
    path.write_bytes(b"z" * 4096)
    stopping = threading.Event()

    class _SwallowingClient:
        """Drains one chunk, then the stop lands and the body's refusal is wrapped."""

        def put_object(self, **kw):
            body = kw["Body"]
            body.read(16)
            stopping.set()
            try:
                while body.read(16):
                    pass
            except objects.UploadCancelled as exc:
                raise ConnectionError("connection reset by peer") from exc
            return {}

    store = S3ObjectStore("a-bucket", client=_SwallowingClient())
    with path.open("rb") as fh:
        with pytest.raises(objects.UploadCancelled) as caught:
            store.put("crews/c/big.jsonl", fh, 4096, cancel=stopping.is_set)

    assert not isinstance(caught.value, StoreUnusable)
    assert "was cut by a stop" in str(caught.value)
    assert isinstance(caught.value.__cause__, ConnectionError)
    assert store_mod.UploadCancelled is objects.UploadCancelled


def test_both_cuts_are_one_class_the_caller_can_catch(tmp_path):
    """One handling path, two names. A cut upload is never permanent, whichever cut it was.

    The caller does the same thing for both -- record the refusal, let the next cycle send
    it -- so they share a base. Separate names are what the operator's log needs.
    """
    assert issubclass(objects.UploadCancelled, objects.UploadCut)
    assert issubclass(objects.UploadDeadlineExceeded, objects.UploadCut)
    assert not issubclass(objects.UploadCut, objects.StoreUnusable)


def test_an_in_flight_interval_upload_ends_when_the_stop_arrives(tmp_path):
    """The finding, end to end: the drain cycle begins instead of waiting on this PUT.

    Without the flag reaching the body, a slow interval PUT keeps reading until it finishes
    on its own. Its cycle cannot return before then and ``run`` cannot begin the final
    cycle, whose deadline is anchored at the signal -- so the turns the backend flushed on
    its way out are never attempted.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n" * 64)
    stopping = threading.Event()
    chunks: list[int] = []

    class _SlowButProgressing:
        """A connection that never stalls long enough to trip a socket timeout."""

        def put_object(self, **kw):
            body = kw["Body"]
            while True:
                chunk = body.read(8)
                if not chunk:
                    return {}
                chunks.append(len(chunk))
                # The stop lands partway through, as a SIGTERM during an ordinary upload.
                if len(chunks) == 3:
                    stopping.set()

    from container.sidecar.store import S3ObjectStore

    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    try:
        backup_mod._upload_phase(
            plan.data,
            settings=settings,
            store=S3ObjectStore("a-bucket", client=_SlowButProgressing()),
            state={},
            result=result,
            committed_index={},
            yield_when=stopping.is_set,
        )
    finally:
        plan.close_authority()

    assert len(chunks) == 3, "the upload kept reading after the stop"
    assert not result.complete
    assert [name for name, _why in result.refused] == [f"{STEM}{keys.TRANSCRIPT_SUFFIX}"]


def test_interval_preflight_reads_forward_the_stop_predicate_to_their_gets(tmp_path):
    """GPT F2: an interval cycle's pointer and index GETs are cut by the stop, not run to end.

    An interval cycle has no deadline (it has a next interval) but its preflight reads -- the
    pointer and the possibly-multi-megabyte transcript index -- must still be cut the instant
    a stop arrives, or a trickling read would run across the SIGTERM and eat the drain window
    the final cycle needs. The reads thread the stop predicate through to the store GET so the
    body is cut between chunks.

    This asserts the predicate reaches the store's get/get_with_etag; it fails if either read
    drops it (the F2 bug: deadline=None and no stop means an unbounded interval GET).
    """
    settings = _settings(tmp_path)
    gen_id = keys.new_generation_id()
    seen_cancel: dict[str, object] = {}

    class _CancelRecorder(_Recorder):
        def get(self, key, *, limit, deadline=None, cancel=None):
            seen_cancel[f"get:{key}"] = cancel
            return super().get(key, limit=limit, deadline=deadline)

        def get_with_etag(self, key, *, limit, deadline=None, cancel=None):
            seen_cancel[f"etag:{key}"] = cancel
            return super().get_with_etag(key, limit=limit, deadline=deadline)

    store = _CancelRecorder()
    store.objects[keys.authority_pointer_key(settings)] = generation_mod.pointer_body(
        gen_id, keys.new_incarnation()
    )
    store.etags[keys.authority_pointer_key(settings)] = '"ptr-1"'
    store.objects[keys.transcript_index_key(settings, gen_id)] = b"{}"

    def _stop() -> bool:
        return False

    generation_mod.read_pointer(settings, store, cancel=_stop)
    generation_mod.read_transcript_index(settings, store, gen_id, cancel=_stop)

    # Both preflight reads forwarded the stop predicate to the store GET (F2). Without the
    # fix, the store would have been handed cancel=None and the interval read would be
    # unbounded across a SIGTERM.
    assert seen_cancel[f"etag:{keys.authority_pointer_key(settings)}"] is _stop
    assert seen_cancel[f"get:{keys.transcript_index_key(settings, gen_id)}"] is _stop


# --- 14. the drain window covers every step the final cycle spends ------------------


def _authority_reservation_count() -> int:
    """What the cycle reserves the authority phase: a PUT per file, the index, the pointer."""
    return len(keys.AUTHORITY_NAMES) + 2


def test_the_drain_window_covers_every_step_the_final_cycle_spends():
    """Sized to the uploads alone, the window is spent before the data phase may start.

    The cycle READS the generation pointer before it accounts for anything, and that GET
    carries no budget -- its only bound is the client's connect and read timeouts, so it can
    spend a whole attempt's cost and still succeed. Whatever it spends comes off the front of
    the window, and the authority reservation comes off the back, so a window sized at
    reservation-plus-one-PUT leaves the data gate nothing the moment the pointer read is
    slower than instant.
    """
    needed = (
        2 * cfg.BACKUP_ATTEMPT_COST_SECS  # the two un-budgeted preflight GETs (pointer, index)
        + _authority_reservation_count() * cfg.BACKUP_ATTEMPT_COST_SECS  # the reservation
        + cfg.BACKUP_PER_OBJECT_BUDGET_SECS  # one worst-case data PUT
    )

    assert cfg.SIDECAR_DRAIN_SECS >= needed


def test_the_data_phase_still_admits_an_upload_after_a_slow_pointer_read():
    """The same claim through the real gate, not a restatement of the arithmetic.

    A pin that recomputes the sum can agree with a window that the production functions
    reject. This spends the pointer read's worst case against a deadline anchored at the
    stop, reserves the authority phase with the function the cycle uses, and asks the gate
    the cycle's own question. Refused here, the drain uploads nothing: every changed
    transcript is recorded unattempted, the pair is withheld, and the replacement adopts the
    previous generation's index while most of the window goes unused.
    """
    stop = 1000.0
    deadline = stop + cfg.SIDECAR_DRAIN_SECS
    # Both preflight GETs (pointer, then committed index) answer slowly, before the cycle
    # accounts for anything.
    now = stop + 2 * cfg.BACKUP_ATTEMPT_COST_SECS
    data_deadline = backup_mod._reserve_for_authority(deadline, _authority_reservation_count())

    assert data_deadline is not None
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(backup_mod, "time", _ClockProxy(lambda: now))
        assert backup_mod._time_for_one_more(data_deadline, cfg.BACKUP_PER_OBJECT_BUDGET_SECS)


def test_the_stop_timeout_the_platform_is_asked_for_still_fits_the_cap():
    """The window cannot be widened past what Fargate accepts on a container definition."""
    total = (
        cfg.FRONT_DRAIN_SECS
        + cfg.BACKEND_DRAIN_SECS
        + cfg.SIDECAR_DRAIN_SECS
        + cfg.TEARDOWN_REAP_MARGIN_SECS
    )

    assert total <= cfg.MAX_TASK_STOP_TIMEOUT_SECS


# --- 17. the reserved number is the whole request, not just the transmission -----------


def test_the_reserved_budget_covers_the_response_wait_the_body_cannot_bound():
    """A body-enforced deadline stops at the last chunk; the request does not.

    Once the body is drained the transport waits for the RESPONSE and never asks the body
    again, so that wait is bounded only by the client's read timeout. Reserving the
    transmission alone lets a PUT finish exactly on its allowance and still spend one more
    timeout, which is the gate admitting an upload the window cannot hold.
    """
    assert (
        cfg.BACKUP_PER_OBJECT_BUDGET_SECS
        == cfg.BACKUP_TRANSMISSION_BUDGET_SECS + cfg.BACKUP_REQUEST_TIMEOUT_SECS
    )
    assert cfg.BACKUP_TRANSMISSION_BUDGET_SECS > 0


def test_the_store_hands_the_body_the_transmission_share_not_the_reservation(tmp_path):
    """The two numbers are different on purpose, and the body must get the smaller one."""
    path = tmp_path / "big.jsonl"
    path.write_bytes(b"z" * 64)
    seen: list[float | None] = []
    real_reader = objects.BoundedReader

    class _Recording(real_reader):  # type: ignore[misc,valid-type]
        def __init__(self, fh, limit, *, deadline=None, clock=None, cancel=None):
            seen.append(deadline)
            super().__init__(fh, limit, deadline=deadline, clock=clock, cancel=cancel)

    class _Client:
        def put_object(self, **kw):
            while kw["Body"].read(64):
                pass
            return {}

    from container.sidecar import store as store_mod

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(store_mod, "BoundedReader", _Recording)
        store = store_mod.S3ObjectStore("a-bucket", client=_Client())
        # flake-ok: pre-existing real-clock ceiling (one second of slack on the transmission share), moved verbatim; tracked for a de-flake pass
        before = time.monotonic()
        with path.open("rb") as fh:
            store.put("crews/c/big.jsonl", fh, 64, budget=cfg.BACKUP_PER_OBJECT_BUDGET_SECS)

    assert len(seen) == 1 and seen[0] is not None
    share = seen[0] - before

    assert share <= cfg.BACKUP_TRANSMISSION_BUDGET_SECS + 1.0
    assert share < cfg.BACKUP_PER_OBJECT_BUDGET_SECS


def test_the_window_still_covers_every_step_after_the_response_allowance():
    """The reservation grew, so the window has to have grown with it."""
    needed = (
        2 * cfg.BACKUP_ATTEMPT_COST_SECS
        + _authority_reservation_count() * cfg.BACKUP_ATTEMPT_COST_SECS
        + cfg.BACKUP_PER_OBJECT_BUDGET_SECS
    )

    assert cfg.SIDECAR_DRAIN_SECS >= needed
    assert (
        cfg.FRONT_DRAIN_SECS
        + cfg.BACKEND_DRAIN_SECS
        + cfg.SIDECAR_DRAIN_SECS
        + cfg.TEARDOWN_REAP_MARGIN_SECS
    ) <= cfg.MAX_TASK_STOP_TIMEOUT_SECS
