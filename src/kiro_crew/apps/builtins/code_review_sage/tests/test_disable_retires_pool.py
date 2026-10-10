"""Disabling the app must retire the review pool, not just refuse its routes.

``review_pool.shutdown_pool``'s own docstring says it is "called on app disable /
gateway shutdown". Only the second half was true: ``register_routes`` wired
``_shutdown_pool`` to ``app.on_cleanup``, which aiohttp fires when the gateway
stops and at no other time, and ``grep shutdown_pool`` across ``src/`` found no
other production caller. So disabling the app left the pool's worker sessions
alive, holding an agent runtime up and continuing to run review turns for a
permission the operator had already withdrawn.

``apps.teardown`` has the seam for this. ``notify_app_disabled`` fires INSIDE the
disable request, ahead of the app's own ``onDisable`` script and before the
``enabled`` flag is written, precisely so a worker holding something time-bounded
cannot take one more turn in the gap between the click and the next sweep.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import cast

import pytest
from aiohttp import web

from kiro_crew.apps import teardown
from kiro_crew.apps.builtins.code_review_sage.backend import routes as R
from kiro_crew.apps.manager import app_lifecycle_lock

APP_NAME = "code-review-sage"


@pytest.fixture(autouse=True)
def _clean_disable_registry():
    """The disable registry is process memory shared by every test in the worker,
    so a hook registered here must not outlive the test that registered it."""
    teardown.unregister_app_disable_hook(APP_NAME)
    yield
    teardown.unregister_app_disable_hook(APP_NAME)


@pytest.mark.asyncio
async def test_disabling_the_app_retires_the_review_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The load-bearing one: with no hook registered, ``notify_app_disabled``
    finds nothing and the pool keeps its workers."""
    retired: list[bool] = []

    async def record_shutdown() -> None:
        retired.append(True)

    monkeypatch.setattr(R.review_pool, "shutdown_pool", record_shutdown)

    app = web.Application()
    R.register_routes(app)

    await teardown.notify_app_disabled(APP_NAME)

    assert retired, "disabling the app left the review pool running"


@pytest.mark.asyncio
async def test_a_failing_retire_never_escapes(monkeypatch: pytest.MonkeyPatch) -> None:
    """``notify_app_disabled`` swallows a raising hook so the teardown completes,
    but the hook contains its own failure too: the disable must not depend on the
    caller's leniency to finish."""

    async def boom() -> None:
        raise RuntimeError("pool exploded")

    monkeypatch.setattr(R.review_pool, "shutdown_pool", boom)

    app = web.Application()
    R.register_routes(app)

    hook = teardown._APP_DISABLE_HOOKS[APP_NAME]
    await hook(APP_NAME)  # called directly: no notify_app_disabled leniency in the way


@pytest.mark.asyncio
async def test_retiring_with_no_pool_started_is_a_no_op() -> None:
    """``shutdown_pool`` is idempotent, and an operator may disable an app that has
    never run a review, or disable it twice."""
    app = web.Application()
    R.register_routes(app)

    await teardown.notify_app_disabled(APP_NAME)
    await teardown.notify_app_disabled(APP_NAME)


def test_register_routes_wires_an_app_disable_hook() -> None:
    """A structural guard: the disable seam must be wired under this app's own
    name, so an edit that drops it fails here instead of silently letting a
    disabled app keep reviewing."""
    app = web.Application()
    R.register_routes(app)
    assert teardown._APP_DISABLE_HOOKS.get(APP_NAME) is not None


# ── The queued-review race: what happens AFTER shutdown_pool ──


def _register_pool_retirement(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register the app's REAL disable hook, the way production does."""
    R.register_routes(web.Application())


@pytest.fixture(autouse=True)
def _clean_run_state():
    """`_RUNS`, `_CANCELLED` and the consolidation claims are process memory
    shared across the worker."""
    R._RUNS[:] = []
    R._CANCELLED.clear()
    R._CONSOLIDATING.clear()
    R._CANCELLED_NS.clear()
    R._CONSOLIDATE_STATE.clear()
    yield
    R._RUNS[:] = []
    R._CANCELLED.clear()
    R._CONSOLIDATING.clear()
    R._CANCELLED_NS.clear()
    R._CONSOLIDATE_STATE.clear()


def _live_run(run_id: str) -> dict:
    """A run in the state `_run_review_bg` sees it in: accepted and not finished."""
    return {"run_id": run_id, "status": "running"}


async def _drive_the_race(monkeypatch: pytest.MonkeyPatch, *, disable_while_queued: bool) -> dict:
    """Run the exact interleaving the blocker describes and report what B did.

    A holds ``_RUN_LOCK``. B is already accepted and blocks on it. While B waits,
    the operator disables the app, which fires the registered disable hook. A then
    releases, B wakes.

    The interleaving is driven by events, not by sleeps, so it is deterministic:
    B is only released once the disable has actually completed.
    """
    created: list[str] = []
    reviewed: list[str] = []

    class _FakePool:
        async def begin_batch(self) -> None:
            reviewed.append("begin_batch")

        async def end_batch(self) -> None:
            pass

    def _fake_get_pool():
        created.append("get_pool")
        return _FakePool()

    monkeypatch.setattr(R.review_pool, "get_pool", _fake_get_pool)

    async def _fake_shutdown_pool() -> None:
        created.append("shutdown_pool")

    monkeypatch.setattr(R.review_pool, "shutdown_pool", _fake_shutdown_pool)

    # Never let the test reach the real driver, the real claim store, or disk.
    monkeypatch.setattr(R, "_claim_changes_under_lock", lambda run, changes: changes)
    monkeypatch.setattr(R, "_release_claims", lambda run: None)
    monkeypatch.setattr(R, "_save_runs", _noop_async)
    monkeypatch.setattr(R, "_notify_finished", _noop_async)
    monkeypatch.setattr(R, "_collect_delivered", lambda run, summary: None)
    monkeypatch.setattr(R, "_make_progress", lambda run: None)

    def _fake_run_review(changes, **kwargs):
        reviewed.append("run_review")
        return {"ok": True, "changes": len(changes), "result_records": 1, "deep_reviewed": 1}

    monkeypatch.setattr(R.review_driver, "run_review", _fake_run_review, raising=False)

    b_run = _live_run("run-B")
    R._RUNS.insert(0, b_run)

    b_is_waiting = asyncio.Event()
    a_may_release = asyncio.Event()

    async def _run_a() -> None:
        async with R._RUN_LOCK:
            b_is_waiting.set()
            await a_may_release.wait()

    a_task = asyncio.create_task(_run_a())
    await b_is_waiting.wait()

    # B is accepted and now genuinely blocked on the lock A holds.
    b_task = asyncio.create_task(R._run_review_bg(b_run, ["change-1"]))
    await asyncio.sleep(0)  # let B reach the `async with _RUN_LOCK`

    if disable_while_queued:
        # The operator disables. This is the real registered hook, reached the way
        # production reaches it, not a direct call.
        await teardown.notify_app_disabled(APP_NAME)

    a_may_release.set()
    await a_task
    await b_task

    return {"created": created, "reviewed": reviewed, "status": b_run.get("status")}


async def _noop_async(*args, **kwargs) -> None:
    return None


@pytest.mark.asyncio
async def test_a_review_queued_before_disable_cannot_rebuild_the_pool_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE BLOCKER. Retiring the pool is not enough on its own.

    ``shutdown_pool`` drops the singleton, and ``get_pool`` recreates it on the
    very next call (``if _POOL is None or _POOL._closed: _POOL = ReviewPool()``).
    So a run that was already accepted, and is only waiting its turn on
    ``_RUN_LOCK``, wakes up after the disable with nothing but that singleton
    check between it and a brand-new pool — a fresh agent runtime stood back up,
    running review turns for a permission the operator withdrew while it waited.
    The ``_CANCELLED`` recheck under the lock is what refuses the wake-up.
    """
    _register_pool_retirement(monkeypatch)

    out = await _drive_the_race(monkeypatch, disable_while_queued=True)

    assert "shutdown_pool" in out["created"], "the disable hook must still retire the pool"
    assert "get_pool" not in out["created"], (
        "a run queued before the disable rebuilt the pool after it — "
        "authority was withdrawn and the run recreated it anyway"
    )
    assert out["reviewed"] == [], "no review work may run after the disable"
    assert out["status"] == "cancelled"


@pytest.mark.asyncio
async def test_a_queued_review_still_runs_when_nothing_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control, and the one that stops the guard from being a blanket
    refusal: the same interleaving with no disable must review normally."""
    _register_pool_retirement(monkeypatch)

    out = await _drive_the_race(monkeypatch, disable_while_queued=False)

    assert "get_pool" in out["created"]
    assert "run_review" in out["reviewed"]
    assert out["status"] != "cancelled"


@pytest.mark.asyncio
async def test_a_new_review_after_re_enable_gets_a_fresh_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-enable must not stay poisoned.

    There is no enable-side hook in ``apps.teardown`` to clear anything, and this
    fix deliberately does not add one: the withdrawal is recorded per RUN, in the
    existing ``_CANCELLED`` set, so a genuinely new review submitted after
    re-enable carries a run id that was never cancelled and builds its own pool.
    """
    _register_pool_retirement(monkeypatch)

    disabled = await _drive_the_race(monkeypatch, disable_while_queued=True)
    assert "get_pool" not in disabled["created"]

    # Operator re-enables and submits a NEW review. Nothing is un-set by hand.
    fresh = await _drive_the_race(monkeypatch, disable_while_queued=False)

    assert "get_pool" in fresh["created"]
    assert "run_review" in fresh["reviewed"]


@pytest.mark.asyncio
async def test_the_disable_hook_withdraws_authority_from_every_live_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A posting run reports a TERMINAL status while it is still writing to the
    pull request, so ``status == "running"`` alone would miss it. The hook uses
    ``_is_live``, the existing predicate, which covers both phases."""
    _register_pool_retirement(monkeypatch)
    monkeypatch.setattr(R.review_pool, "shutdown_pool", _noop_async)

    posting = {"run_id": "run-posting", "status": "done", "posting": True}
    finished = {"run_id": "run-finished", "status": "done"}
    R._RUNS.insert(0, posting)
    R._RUNS.insert(0, finished)

    await teardown.notify_app_disabled(APP_NAME)

    assert "run-posting" in R._CANCELLED
    assert "run-finished" not in R._CANCELLED, "a finished run is not live; do not mark it"


# ── The admission race: what happens DURING the disable transition ──


class _StubRequest:
    """The parts of ``web.Request`` that ``_handle_review`` reads."""

    def __init__(self, body: dict) -> None:
        self._body = body

    async def json(self) -> dict:
        return self._body


def _stub_admission_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep admission off disk and off the real driver, nothing more."""
    monkeypatch.setattr(R, "_save_runs", _noop_async)
    monkeypatch.setattr(R.review_driver, "change_id_for", lambda c: str(c), raising=False)


async def _post_review_mid_disable(monkeypatch: pytest.MonkeyPatch, *, disabling: bool) -> dict:
    """Drive the exact ordering the blocker describes and report what happened.

    1. the disable request takes ``app_lifecycle_lock``, the way
       ``apps/routes.handle_disable_app`` does;
    2. ``notify_app_disabled`` fires the app's REAL hook, which completes its
       cancellation snapshot of the runs that were live at that instant;
    3. a brand-new authenticated review request arrives, as its OWN task —
       production serves every request on its own task, and the admission
       boundary now takes the same lifecycle lock, which a call made inline
       inside the disable's ``async with`` could never reach;
    4. ``disable_app`` writes ``enabled=False``, still inside the lock, because
       that is the order ``handle_disable_app`` uses: the flag is written after
       the teardown and before the lock is released;
    5. the lock is released and whatever production does with the request is
       recorded.

    Ordering is by lock and by await point, never by sleeping.
    """
    _register_pool_retirement(monkeypatch)
    _stub_admission_io(monkeypatch)
    monkeypatch.setattr(R.review_pool, "shutdown_pool", _noop_async)
    # Step 4: the flag is modelled, not pinned. Production writes it in
    # `disable_app`, after the teardown, so it still reads enabled for the whole
    # transition window and reads disabled once the disable has landed.
    enabled = {"value": True}
    monkeypatch.setattr(R, "is_app_enabled", lambda _name: enabled["value"], raising=False)

    # `change_id_for` is the last thing `_handle_review` calls before `_admit`,
    # so this is a precise "the request has reached the admission boundary"
    # signal. `Event.set` only schedules its waiters, so by the time the disable
    # is resumed the handler has already run on to its own suspension point.
    at_boundary = asyncio.Event()

    def _change_id(change: str) -> str:
        at_boundary.set()
        return str(change)

    monkeypatch.setattr(R.review_driver, "change_id_for", _change_id, raising=False)

    started: list[dict] = []

    async def _capture_bg(run: dict, changes: list[str]) -> None:
        started.append(run)

    monkeypatch.setattr(R, "_run_review_bg", _capture_bg)

    lifecycle = app_lifecycle_lock(APP_NAME)
    request = cast(web.Request, _StubRequest({"changes": ["change-late"]}))

    if disabling:
        async with lifecycle:
            await teardown.notify_app_disabled(APP_NAME)
            snapshot = set(R._CANCELLED)
            pending = asyncio.create_task(R._handle_review(request))
            await at_boundary.wait()
            enabled["value"] = False
        response = await pending
    else:
        snapshot = set(R._CANCELLED)
        response = await R._handle_review(request)

    # Drain whatever the handler spawned instead of sleeping for it: an admitted
    # review starts its work in a task, and gathering the module's own task set is
    # deterministic where a yield to the loop is not.
    if R._TASKS:
        await asyncio.gather(*list(R._TASKS))

    assert isinstance(response.body, bytes)
    return {
        "status": response.status,
        "payload": json.loads(response.body),
        "snapshot": snapshot,
        "registered": [str(r.get("run_id") or "") for r in R._RUNS],
        "started": started,
    }


@pytest.mark.asyncio
async def test_a_new_review_cannot_be_admitted_once_disable_has_begun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE BLOCKER, second form: the cancellation snapshot is point-in-time.

    ``_retire_pool_on_disable`` marks the runs that are live when it fires. A
    review submitted AFTER that scan and BEFORE ``disable_app`` writes the flag
    was never in ``_RUNS`` to be marked, so ``_CANCELLED`` cannot speak for it.

    The property asserted is the boundary itself, not an internal flag: the
    request must be REFUSED. A test that only checked ``_CANCELLED`` would pass
    on a fix that admitted the run and cancelled it afterwards, which is a
    different and weaker promise.

    The request arrives while the disable holds the lifecycle lock, so admission
    waits for the transition instead of racing it, and then reads the flag the
    disable has since written. Waiting is the point: the refusal is decided on
    the state the operator left behind, not on a sample of a lock.
    """
    out = await _post_review_mid_disable(monkeypatch, disabling=True)

    assert out["status"] == 403, (
        "a new review was admitted after the operator's disable had already "
        f"begun (HTTP {out['status']}, payload {out['payload']!r})"
    )
    assert out["registered"] == [], "the refused review must not be registered as a run"
    assert out["started"] == [], "no review work may be started for a refused request"


@pytest.mark.asyncio
async def test_a_completed_disable_cannot_be_overtaken_by_an_in_flight_enabled_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE BLOCKER, third form: the read and the admission are ONE section.

    The interleaving driven here is the one the exact-head review named, and it
    is the one a second check taken *after* the read structurally cannot see:

    1. a review request reads the ``enabled`` flag and gets ``True``;
    2. while that off-loop read is still in flight, the operator's disable takes
       ``app_lifecycle_lock``, runs the teardown, writes ``enabled=False`` and
       releases it;
    3. the review request resumes — and by then the disable has completed, so a
       sample of the lock reports "no transition in flight" and the run is
       admitted with the operator's permission already withdrawn.

    The property asserted is an ORDER, recorded as each step happens: no run may
    enter ``_RUNS`` after ``enabled=False`` has been persisted. That is what
    makes this test blind to *how* the code holds the line — it fails whenever
    the two events interleave and passes whenever they are serialized, in either
    direction.

    Ordering is driven by events only. The flag read is parked in its own worker
    thread on a ``threading.Event`` and released by the disable; the timeouts are
    watchdogs so a regression fails the suite instead of hanging it, and never
    the ordering mechanism.
    """
    _register_pool_retirement(monkeypatch)
    _stub_admission_io(monkeypatch)
    monkeypatch.setattr(R.review_pool, "shutdown_pool", _noop_async)
    monkeypatch.setattr(R, "_run_review_bg", _noop_async)

    events: list[str] = []
    events_lock = threading.Lock()

    def _log(name: str) -> None:
        # Appended from a worker thread as well as the loop, so take a lock
        # rather than relying on which list operations happen to be atomic.
        with events_lock:
            events.append(name)

    read_started = threading.Event()
    read_may_return = threading.Event()
    enabled = {"value": True}

    def _flag_read_held_open(_name: str) -> bool:
        """The off-loop ``enabled`` read, held open across the whole disable."""
        value = bool(enabled["value"])
        _log(f"review:read_enabled={value}")
        read_started.set()
        assert read_may_return.wait(timeout=30), "the disable never released the flag read"
        return value

    monkeypatch.setattr(R, "is_app_enabled", _flag_read_held_open, raising=False)

    # `_record` awaits `_save_runs` under `_LOCK`, immediately after the insert,
    # so this fires exactly when a run enters `_RUNS`. Logging there rather than
    # wrapping `_record` keeps the probe independent of that helper's signature.
    async def _log_admission() -> None:
        _log("review:run_admitted")

    monkeypatch.setattr(R, "_save_runs", _log_admission)

    async def _disable_the_app() -> None:
        # The precondition the blocker names: the review's flag read is already
        # in flight. Waited on, not slept for.
        assert await asyncio.to_thread(read_started.wait, 30), "the review never read the flag"
        lifecycle = app_lifecycle_lock(APP_NAME)
        if lifecycle.locked():
            # Admission already holds the lifecycle lock, so this disable is
            # serialized after it by construction and the interleaving cannot be
            # produced. Release the parked read instead of deadlocking behind it.
            _log("disable:serialized_after_admission")
            read_may_return.set()
        async with lifecycle:
            await teardown.notify_app_disabled(APP_NAME)
            enabled["value"] = False
            _log("disable:enabled_written_false")
        read_may_return.set()

    request = cast(web.Request, _StubRequest({"changes": ["change-late"]}))
    response, _ = await asyncio.gather(R._handle_review(request), _disable_the_app())
    if R._TASKS:
        await asyncio.gather(*list(R._TASKS))

    assert (
        "disable:enabled_written_false" in events
    ), "premise: the operator's disable must actually have completed"
    assert any(
        e.startswith("review:read_enabled=") for e in events
    ), "premise: the review must actually have read the enabled flag"
    written = events.index("disable:enabled_written_false")
    admitted = [i for i, e in enumerate(events) if e == "review:run_admitted"]
    assert all(i < written for i in admitted), (
        "a review was registered after the operator's disable had completed and "
        f"persisted enabled=False: {events}"
    )

    assert isinstance(response.body, bytes)
    payload = json.loads(response.body)
    if response.status == 200:
        # Serialized the other way: the run was admitted while the app was still
        # enabled, so the disable hook's own scan is what withdraws it. Admitted
        # BEFORE the transition is not the same as admitted after it.
        assert [str(r.get("run_id") or "") for r in R._RUNS] == [payload["run_id"]]
        assert payload["run_id"] in R._CANCELLED, (
            "a run admitted ahead of the disable must still be caught by the "
            "hook's scan; ordering without that is only half the invariant"
        )
    else:
        assert response.status == 403, f"unexpected response: {payload!r}"
        assert payload["code"] == "app_disabled"
        assert R._RUNS == [], "a refused review must not be registered as a run"


@pytest.mark.asyncio
async def test_the_admitted_run_would_otherwise_reach_the_pool_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Why the refusal matters, driven rather than asserted.

    Takes whatever run the transition window admits and runs it through the REAL
    ``_run_review_bg``, showing it reach ``get_pool()`` — review authority
    rebuilt after the operator withdrew it. On the fixed code there is no such
    run to drive, which is the point, so the drive half is skipped there while
    the refusal itself stays pinned by the test above.
    """
    # Bound BEFORE the helper monkeypatches it, so the drive below runs the real
    # background body rather than the helper's recorder.
    real_run_review_bg = R._run_review_bg
    created: list[str] = []

    class _FakePool:
        async def begin_batch(self) -> None:
            pass

        async def end_batch(self) -> None:
            pass

    def _fake_get_pool():
        created.append("get_pool")
        return _FakePool()

    monkeypatch.setattr(R.review_pool, "get_pool", _fake_get_pool)
    monkeypatch.setattr(R, "_claim_changes_under_lock", lambda run, changes: changes)
    monkeypatch.setattr(R, "_release_claims", lambda run: None)
    monkeypatch.setattr(R, "_notify_finished", _noop_async)
    monkeypatch.setattr(R, "_collect_delivered", lambda run, summary: None)
    monkeypatch.setattr(R, "_make_progress", lambda run: None)
    monkeypatch.setattr(
        R.review_driver,
        "run_review",
        lambda changes, **kw: {
            "ok": True,
            "changes": len(changes),
            "result_records": 1,
            "deep_reviewed": 1,
        },
        raising=False,
    )

    out = await _post_review_mid_disable(monkeypatch, disabling=True)
    if out["status"] == 403:
        pytest.skip("admission is closed during the transition; there is no run to drive")

    run_id = out["payload"]["run_id"]
    assert run_id not in out["snapshot"], (
        "premise: the new run's id is NOT in the disable hook's cancellation "
        "snapshot, because the run did not exist when the hook scanned"
    )
    await real_run_review_bg({"run_id": run_id, "status": "running"}, ["change-late"])
    assert created == ["get_pool"], (
        "the run admitted during the disable transition rebuilt review authority "
        "after the operator withdrew it"
    )


@pytest.mark.asyncio
async def test_a_new_review_is_admitted_when_no_disable_is_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control, and what stops the gate becoming a blanket refusal.

    Same code path and same enabled flag, with no lifecycle transition in
    progress: the review is admitted and registered exactly as before.
    """
    out = await _post_review_mid_disable(monkeypatch, disabling=False)

    assert out["status"] == 200, f"an ordinary review was refused: {out['payload']!r}"
    assert out["registered"], "an admitted review must be registered as a run"
    assert out["started"], "an admitted review must start its background work"


@pytest.mark.asyncio
async def test_admission_reopens_after_the_transition_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invariant D, executable: a complete cycle is not left bricked.

    The refusal is owned by the lifecycle transition itself, which the disable
    request releases when it finishes. Nothing is un-set by hand here — the
    second call simply runs after the ``async with`` block has exited.
    """
    refused = await _post_review_mid_disable(monkeypatch, disabling=True)
    assert refused["status"] == 403

    R._RUNS[:] = []
    R._CANCELLED.clear()
    reopened = await _post_review_mid_disable(monkeypatch, disabling=False)

    assert reopened["status"] == 200
    assert reopened["registered"]


# ── The /post seam: a finished review must not post after the disable ──


class _StubPostRequest:
    """The parts of ``web.Request`` that ``_handle_run_post`` reads."""

    def __init__(self, run_id: str, body: dict | None = None) -> None:
        self.match_info = {"run_id": run_id}
        self.query: dict[str, str] = {}
        self._body = body if body is not None else {}

    async def json(self) -> dict:
        return self._body


def _done_run(run_id: str) -> dict:
    """A run in the state a reviewed-but-unposted review is in: terminal, with
    recorded findings waiting on the user's deliberate post action."""
    return {
        "run_id": run_id,
        "status": "done",
        "changes": ["https://example.test/pr/1"],
        "change_ids": ["change-1"],
    }


def _stub_posting_io(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[str]]:
    """Keep the poster off disk and off the real pool; report what it reaches.

    Returns ``(created, posted)``: pool constructions, and per-change posts.
    ``_release_claims`` is left REAL so the in-flight claim table is exercised
    and cleaned on every terminal path, the way production relies on.
    """
    created: list[str] = []
    posted: list[str] = []

    class _FakePool:
        async def begin_batch(self) -> None:
            pass

        async def end_batch(self) -> None:
            pass

    def _fake_get_pool():
        created.append("get_pool")
        return _FakePool()

    monkeypatch.setattr(R.review_pool, "get_pool", _fake_get_pool)
    monkeypatch.setattr(R.review_pool, "shutdown_pool", _noop_async)
    monkeypatch.setattr(R.review_pool, "make_sync_dispatch", lambda loop, pool: None, raising=False)

    def _fake_post_recorded(cid, link, **kwargs):
        posted.append(str(cid))
        return {"post_ok": True, "posted_keys": ["k1"]}

    monkeypatch.setattr(R.review_driver, "post_recorded", _fake_post_recorded, raising=False)
    monkeypatch.setattr(R, "_save_runs", _noop_async)
    monkeypatch.setattr(R, "_notify_posted", _noop_async)
    monkeypatch.setattr(R, "_record_reviewed", lambda run: None)
    monkeypatch.setattr(R, "_pending_comment_count", lambda *a, **k: 1)
    return created, posted


@pytest.mark.asyncio
async def test_a_finished_run_cannot_post_once_the_app_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE BLOCKER, /post form: disable closes EVERY admission path, and a
    finished review's post action is one of them.

    A reviewed-but-unposted run is terminal — not ``_is_live`` — so the disable
    hook's cancellation scan has nothing to mark. Without its own admission
    check, ``/post`` on that run fires ``_post_comments_bg``, which rebuilds the
    retired pool via ``get_pool()`` and writes to the pull request on an
    authority the operator has withdrawn. The refusal must come from the
    admission boundary itself — the same 403 the review entry points give.
    """
    _register_pool_retirement(monkeypatch)
    created, posted = _stub_posting_io(monkeypatch)

    enabled = {"value": True}
    monkeypatch.setattr(R, "is_app_enabled", lambda _name: enabled["value"], raising=False)

    run = _done_run("run-done")
    R._RUNS.insert(0, run)

    # The disable completes the way handle_disable_app runs it: hook fired and
    # flag written under one hold of the lifecycle lock.
    async with app_lifecycle_lock(APP_NAME):
        await teardown.notify_app_disabled(APP_NAME)
        enabled["value"] = False

    try:
        response = await R._handle_run_post(cast(web.Request, _StubPostRequest("run-done")))
        if R._TASKS:
            await asyncio.gather(*list(R._TASKS))

        assert (
            response.status == 403
        ), f"a disabled app admitted a /post request (HTTP {response.status})"
        assert created == [], "the poster rebuilt the retired pool after the disable"
        assert posted == [], "comments were posted after authority was withdrawn"
        assert not run.get("posting"), "a refused run must not be left flagged as posting"
    finally:
        R._INFLIGHT.clear()


@pytest.mark.asyncio
async def test_a_finished_run_posts_normally_while_the_app_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control, and the one that stops the admission check from being a
    blanket refusal: with nothing disabled the same run posts its findings."""
    _register_pool_retirement(monkeypatch)
    created, posted = _stub_posting_io(monkeypatch)
    monkeypatch.setattr(R, "is_app_enabled", lambda _name: True, raising=False)

    run = _done_run("run-done")
    R._RUNS.insert(0, run)

    try:
        response = await R._handle_run_post(cast(web.Request, _StubPostRequest("run-done")))
        if R._TASKS:
            await asyncio.gather(*list(R._TASKS))

        assert (
            response.status == 200
        ), f"an enabled app refused a legitimate /post (HTTP {response.status})"
        assert created == ["get_pool"], "the poster must reach the pool exactly once"
        assert posted == ["change-1"], "the finding must land on the pull request"
        assert not run.get("posting"), "posting must be cleared on completion"
    finally:
        R._INFLIGHT.clear()


@pytest.mark.asyncio
async def test_an_admitted_post_stops_at_the_pool_seam_when_disable_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE BLOCKER's other half: admission alone cannot close the window.

    A ``/post`` admitted while the app is enabled sets ``posting`` — which is
    what makes the run ``_is_live`` — and hands the work to a task. A disable
    landing between the admission and that task's first step marks the run
    cancelled; the poster must honour the mark BEFORE ``get_pool()``, or it
    stands the retired pool straight back up.

    The ordering is deterministic, not scheduled: the hook's marks land with no
    await in the way, and the poster task has not taken its first step until
    the module task set is drained below.
    """
    _register_pool_retirement(monkeypatch)
    created, posted = _stub_posting_io(monkeypatch)

    enabled = {"value": True}
    monkeypatch.setattr(R, "is_app_enabled", lambda _name: enabled["value"], raising=False)

    run = _done_run("run-done")
    R._RUNS.insert(0, run)

    try:
        response = await R._handle_run_post(cast(web.Request, _StubPostRequest("run-done")))
        assert response.status == 200, "the enabled admission itself must succeed"

        # The disable lands before the poster's first step.
        async with app_lifecycle_lock(APP_NAME):
            await teardown.notify_app_disabled(APP_NAME)
            enabled["value"] = False

        assert "run-done" in R._CANCELLED, (
            "the disable hook must mark an admitted post — ``posting`` is what "
            "makes the run live to its scan"
        )

        if R._TASKS:
            await asyncio.gather(*list(R._TASKS))

        assert created == [], "the poster rebuilt the retired pool after the disable"
        assert posted == [], "comments were posted after authority was withdrawn"
        assert not run.get("posting"), "a cancelled post must clear the posting flag"
        assert run.get("post_error"), "the run must say why nothing was posted"
    finally:
        R._INFLIGHT.clear()


@pytest.mark.asyncio
async def test_a_stale_cancellation_mark_does_not_refuse_the_next_posting_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mark speaks for the posting cycle a disable interrupted, not for the
    run forever. Admission is mutually exclusive with any disable, so a mark
    still standing for a terminal, not-posting run is stale by construction —
    consumed at admission, or the run could never post again after a re-enable.
    """
    _register_pool_retirement(monkeypatch)
    created, posted = _stub_posting_io(monkeypatch)
    monkeypatch.setattr(R, "is_app_enabled", lambda _name: True, raising=False)

    run = _done_run("run-done")
    R._RUNS.insert(0, run)
    R._CANCELLED.add("run-done")

    try:
        response = await R._handle_run_post(cast(web.Request, _StubPostRequest("run-done")))
        if R._TASKS:
            await asyncio.gather(*list(R._TASKS))

        assert (
            response.status == 200
        ), f"a stale mark refused a legitimate /post (HTTP {response.status})"
        assert created == ["get_pool"] and posted == [
            "change-1"
        ], "the re-admitted run must post its findings"
        assert "run-done" not in R._CANCELLED, "the stale mark must be consumed"
    finally:
        R._INFLIGHT.clear()


# ── The consolidation seam: a merge must not rebuild the pool after disable ──


class _StubConsolidateRequest:
    """The parts of ``web.Request`` that ``_handle_consolidate`` reads."""

    def __init__(self, body: dict | None = None) -> None:
        self._body = body if body is not None else {}

    async def json(self) -> dict:
        return self._body


def _stub_consolidation_io(monkeypatch: pytest.MonkeyPatch, tmp_path) -> list[str]:
    """Keep the merge off disk and off the real pool; report pool constructions."""
    created: list[str] = []

    class _FakePool:
        async def begin_batch(self) -> None:
            pass

        async def end_batch(self) -> None:
            pass

    def _fake_get_pool():
        created.append("get_pool")
        return _FakePool()

    monkeypatch.setattr(R.review_pool, "get_pool", _fake_get_pool)
    monkeypatch.setattr(R.review_pool, "shutdown_pool", _noop_async)
    monkeypatch.setattr(
        R.review_pool,
        "make_sync_dispatch",
        lambda loop, pool: (lambda task: {"ok": True}),
        raising=False,
    )
    monkeypatch.setattr(
        R.review_driver, "build_consolidation_task", lambda *a, **k: {}, raising=False
    )
    monkeypatch.setattr(R.learning, "_namespace_dir", lambda ns, root: str(tmp_path), raising=False)
    monkeypatch.setattr(
        R.learning, "common_file", lambda root, ns: str(tmp_path / "live.md"), raising=False
    )
    monkeypatch.setattr(
        R.learning, "candidate_file", lambda root, ns: str(tmp_path / "cand.md"), raising=False
    )
    monkeypatch.setattr(R.learning, "list_candidate", lambda root, ns: [], raising=False)
    monkeypatch.setattr(R.learning, "candidate_count", lambda root, ns: 1, raising=False)
    return created


@pytest.mark.asyncio
async def test_a_consolidation_cannot_be_admitted_once_the_app_is_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """THE BLOCKER, consolidation form: the merge runs a worker turn on the same
    pool reviews use, and it reaches ``get_pool()`` with no run id for the
    disable hook's ``_RUNS`` scan to mark. Without its own admission check, a
    disabled app's consolidate request rebuilds the retired pool and spends an
    agent turn on authority the operator has withdrawn.
    """
    _register_pool_retirement(monkeypatch)
    created = _stub_consolidation_io(monkeypatch, tmp_path)

    enabled = {"value": True}
    monkeypatch.setattr(R, "is_app_enabled", lambda _name: enabled["value"], raising=False)

    async with app_lifecycle_lock(APP_NAME):
        await teardown.notify_app_disabled(APP_NAME)
        enabled["value"] = False

    response = await R._handle_consolidate(cast(web.Request, _StubConsolidateRequest()))
    if R._TASKS:
        await asyncio.gather(*list(R._TASKS))

    assert (
        response.status == 403
    ), f"a disabled app admitted a consolidation (HTTP {response.status})"
    assert created == [], "the merge rebuilt the retired pool after the disable"
    assert not R._CONSOLIDATING, "a refused consolidation must not hold its claim"


@pytest.mark.asyncio
async def test_a_consolidation_runs_normally_while_the_app_is_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Negative control: with nothing disabled the merge reaches the pool and
    the namespace claim is released when it finishes."""
    _register_pool_retirement(monkeypatch)
    created = _stub_consolidation_io(monkeypatch, tmp_path)
    monkeypatch.setattr(R, "is_app_enabled", lambda _name: True, raising=False)

    response = await R._handle_consolidate(cast(web.Request, _StubConsolidateRequest()))
    if R._TASKS:
        await asyncio.gather(*list(R._TASKS))

    assert (
        response.status == 200
    ), f"an enabled app refused a legitimate consolidation (HTTP {response.status})"
    assert created == ["get_pool"], "the merge must reach the pool exactly once"
    assert not R._CONSOLIDATING, "the claim must be released when the merge ends"


@pytest.mark.asyncio
async def test_an_admitted_consolidation_stops_at_the_pool_seam_when_disable_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """THE BLOCKER's other half, consolidation form: a merge admitted while the
    app is enabled hands the work to a task; a disable landing before that
    task's first step must stop the worker on the near side of ``get_pool()``.
    The claim in ``_CONSOLIDATING`` is what the disable hook can see — it plays
    the role ``posting`` plays for the /post seam.
    """
    _register_pool_retirement(monkeypatch)
    created = _stub_consolidation_io(monkeypatch, tmp_path)

    enabled = {"value": True}
    monkeypatch.setattr(R, "is_app_enabled", lambda _name: enabled["value"], raising=False)

    response = await R._handle_consolidate(cast(web.Request, _StubConsolidateRequest()))
    assert response.status == 200, "the enabled admission itself must succeed"

    # The disable lands before the merge task's first step; the hook's marks
    # land with no await in the way.
    async with app_lifecycle_lock(APP_NAME):
        await teardown.notify_app_disabled(APP_NAME)
        enabled["value"] = False

    if R._TASKS:
        await asyncio.gather(*list(R._TASKS))

    assert created == [], "the merge rebuilt the retired pool after the disable"
    assert not R._CONSOLIDATING, "a cancelled merge must release its claim"
    ns_state = R._CONSOLIDATE_STATE.get(R.learning.DEFAULT_NAMESPACE) or {}
    assert ns_state.get("error"), "the state must say why the merge did not run"


@pytest.mark.asyncio
async def test_a_stale_namespace_mark_does_not_refuse_the_next_consolidation(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A mark speaks for the merge a disable interrupted, not for the namespace
    forever: admitted again after a re-enable, the same namespace merges."""
    _register_pool_retirement(monkeypatch)
    created = _stub_consolidation_io(monkeypatch, tmp_path)
    monkeypatch.setattr(R, "is_app_enabled", lambda _name: True, raising=False)

    R._CANCELLED_NS.add(R.learning.DEFAULT_NAMESPACE)

    response = await R._handle_consolidate(cast(web.Request, _StubConsolidateRequest()))
    if R._TASKS:
        await asyncio.gather(*list(R._TASKS))

    assert (
        response.status == 200
    ), f"a stale mark refused a legitimate consolidation (HTTP {response.status})"
    assert created == ["get_pool"], "the re-admitted merge must reach the pool"
    assert R.learning.DEFAULT_NAMESPACE not in R._CANCELLED_NS, "the stale mark must be consumed"


@pytest.mark.asyncio
async def test_a_disable_during_the_claim_read_still_stops_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE BLOCKER, fourth form: the lock wait is not the only suspension.

    ``_claim_changes_under_lock`` runs off-loop, so a disable can land while an
    admitted run is inside that read: the hook marks the run and retires the
    pool, and the resumed task's very next step is ``get_pool()``. The
    cancellation check must read the mark AFTER the claim returns — a check
    taken before the claim is blind to this window.

    Ordering is by threading events, the same shape the in-flight-enabled-read
    test uses: the claim read is held open, the disable completes while it is
    held, and only then is the read released. The timeout is a watchdog so a
    regression fails the suite instead of hanging it, never the mechanism.
    """
    _register_pool_retirement(monkeypatch)

    created: list[str] = []
    reviewed: list[str] = []

    class _FakePool:
        async def begin_batch(self) -> None:
            reviewed.append("begin_batch")

        async def end_batch(self) -> None:
            pass

    def _fake_get_pool():
        created.append("get_pool")
        return _FakePool()

    monkeypatch.setattr(R.review_pool, "get_pool", _fake_get_pool)

    async def _fake_shutdown_pool() -> None:
        created.append("shutdown_pool")

    monkeypatch.setattr(R.review_pool, "shutdown_pool", _fake_shutdown_pool)
    monkeypatch.setattr(R, "_release_claims", lambda run: None)
    monkeypatch.setattr(R, "_save_runs", _noop_async)
    monkeypatch.setattr(R, "_notify_finished", _noop_async)
    monkeypatch.setattr(R, "_collect_delivered", lambda run, summary: None)
    monkeypatch.setattr(R, "_make_progress", lambda run: None)

    def _fake_run_review(changes, **kwargs):
        reviewed.append("run_review")
        return {"ok": True, "changes": len(changes), "result_records": 1, "deep_reviewed": 1}

    monkeypatch.setattr(R.review_driver, "run_review", _fake_run_review, raising=False)

    in_claim = threading.Event()
    release_claim = threading.Event()

    def _held_open_claim(run: dict, changes: list[str]) -> list[str]:
        in_claim.set()
        assert release_claim.wait(timeout=30), "the disable never released the claim read"
        return changes

    monkeypatch.setattr(R, "_claim_changes_under_lock", _held_open_claim)

    c_run = _live_run("run-C")
    R._RUNS.insert(0, c_run)

    task = asyncio.create_task(R._run_review_bg(c_run, ["change-1"]))
    assert await asyncio.to_thread(in_claim.wait, 30), "the run never reached the claim read"

    # The operator disables while the claim read is in flight; the hook's marks
    # land and the pool is retired before the read is allowed to return.
    await teardown.notify_app_disabled(APP_NAME)
    release_claim.set()
    await task

    assert "shutdown_pool" in created, "the disable hook must still retire the pool"
    assert (
        "get_pool" not in created
    ), "a run whose claim read straddled the disable rebuilt the pool after it"
    assert reviewed == [], "no review work may run after the disable"
    assert c_run.get("status") == "cancelled"


@pytest.mark.asyncio
async def test_a_waiting_post_does_not_postpone_the_disable_behind_a_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The admission boundary must not invert into a hostage: a ``/post``
    waiting for ``_RUN_LOCK`` behind an unrelated review must not be holding
    the lifecycle lock while it waits, or the operator's disable queues behind
    that entire review — the active reviewer RETAINS authority for exactly the
    span the disable was meant to end.

    The interleaving: A holds ``_RUN_LOCK`` the way a running review does; a
    /post on a finished run arrives and waits; the operator disables. The
    disable must complete while A still holds the lock, and the /post must
    then be refused on the state the operator left behind. The wait_for
    timeouts are watchdogs so a regression fails the suite instead of hanging
    it; the ordering itself is by locks and events only.
    """
    _register_pool_retirement(monkeypatch)
    created, posted = _stub_posting_io(monkeypatch)

    enabled = {"value": True}
    monkeypatch.setattr(R, "is_app_enabled", lambda _name: enabled["value"], raising=False)

    run = _done_run("run-done")
    R._RUNS.insert(0, run)

    a_holds = asyncio.Event()
    a_may_release = asyncio.Event()

    async def _run_a() -> None:
        async with R._RUN_LOCK:
            a_holds.set()
            await a_may_release.wait()

    a_task = asyncio.create_task(_run_a())
    await a_holds.wait()

    try:
        post_task = asyncio.create_task(
            R._handle_run_post(cast(web.Request, _StubPostRequest("run-done")))
        )
        await asyncio.sleep(0)  # let the /post reach its first suspension

        async def _disable() -> None:
            async with app_lifecycle_lock(APP_NAME):
                await teardown.notify_app_disabled(APP_NAME)
                enabled["value"] = False

        # The disable must complete while A still holds `_RUN_LOCK`.
        await asyncio.wait_for(_disable(), timeout=5)

        a_may_release.set()
        await a_task
        response = await asyncio.wait_for(post_task, timeout=5)
        if R._TASKS:
            await asyncio.gather(*list(R._TASKS))

        assert response.status == 403, (
            "the /post that waited across the disable was admitted " f"(HTTP {response.status})"
        )
        assert created == [], "the poster rebuilt the retired pool after the disable"
        assert posted == [], "comments were posted after authority was withdrawn"
        assert not run.get("posting"), "a refused run must not be left flagged as posting"
    finally:
        a_may_release.set()
        R._INFLIGHT.clear()


@pytest.mark.asyncio
async def test_a_disabled_app_never_contacts_the_provider_for_a_repo_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE BLOCKER, /review-repo form: the provider read comes before any run
    exists. ``_admit`` gates run REGISTRATION, but ``_handle_review_repo``
    lists the repository's open PRs — provider contact on the app's authority —
    and an already-reviewed repository answers 200 ``noop`` before a run is
    ever built for ``_admit`` to refuse. A disabled app must refuse at the top,
    before the provider is touched; ``_admit`` stays the atomic gate for the
    run itself.
    """
    _register_pool_retirement(monkeypatch)
    monkeypatch.setattr(R, "_save_runs", _noop_async)

    contacted: list[str] = []

    async def _fake_list_repo_prs(repo: str):
        contacted.append(repo)
        return "acme/example", []

    monkeypatch.setattr(R, "_list_repo_prs", _fake_list_repo_prs)

    enabled = {"value": True}
    monkeypatch.setattr(R, "is_app_enabled", lambda _name: enabled["value"], raising=False)

    async with app_lifecycle_lock(APP_NAME):
        await teardown.notify_app_disabled(APP_NAME)
        enabled["value"] = False

    response = await R._handle_review_repo(
        cast(web.Request, _StubConsolidateRequest({"repo": "https://github.com/acme/example"}))
    )

    assert response.status == 403, f"a disabled app served /review-repo (HTTP {response.status})"
    assert contacted == [], "a disabled app contacted the provider"
    assert R._RUNS == [], "a refused repo review must not be registered as a run"


@pytest.mark.asyncio
async def test_a_repo_review_answers_normally_while_the_app_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control: with nothing disabled the same request reaches the
    provider and answers the already-reviewed repository with a noop."""
    _register_pool_retirement(monkeypatch)
    monkeypatch.setattr(R, "_save_runs", _noop_async)

    contacted: list[str] = []

    async def _fake_list_repo_prs(repo: str):
        contacted.append(repo)
        return "acme/example", []

    monkeypatch.setattr(R, "_list_repo_prs", _fake_list_repo_prs)
    monkeypatch.setattr(R, "is_app_enabled", lambda _name: True, raising=False)

    response = await R._handle_review_repo(
        cast(web.Request, _StubConsolidateRequest({"repo": "https://github.com/acme/example"}))
    )

    assert (
        response.status == 200
    ), f"an enabled app refused a legitimate /review-repo (HTTP {response.status})"
    assert contacted == [
        "https://github.com/acme/example"
    ], "the enabled path must reach the provider exactly once"


async def _drive_merge_dispatch_race(
    monkeypatch: pytest.MonkeyPatch, tmp_path, *, disable_mid_dispatch: bool
) -> dict:
    """Drive a consolidation whose worker dispatch straddles a disable.

    The dispatch runs off-loop, held open on a threading event; the disable
    completes while it is held (or never fires, for the control); only then is
    the worker allowed to return its merge. Reports whether the merge was
    APPLIED — the persisted-state mutation — and what the namespace state says.
    """
    _register_pool_retirement(monkeypatch)
    created = _stub_consolidation_io(monkeypatch, tmp_path)

    in_dispatch = threading.Event()
    release_dispatch = threading.Event()

    def _held_open_dispatch(task: dict) -> dict:
        in_dispatch.set()
        assert release_dispatch.wait(timeout=30), "the disable never released the dispatch"
        return {"ok": True}

    monkeypatch.setattr(
        R.review_pool, "make_sync_dispatch", lambda loop, pool: _held_open_dispatch, raising=False
    )
    # A merge the worker DID produce: non-empty, parseable, apply-ready.
    monkeypatch.setattr(
        R.hooks,
        "safe_read_file_bytes_nolink",
        lambda *a, **k: b"pattern: real merge",
        raising=False,
    )
    monkeypatch.setattr(R.learning, "parse_patterns", lambda merged: [{"id": "p1"}], raising=False)

    applied: list[str] = []

    def _fake_apply(merged, root, ns, cand_ids):
        applied.append(ns)
        return {"ok": True, "patterns_now": 1, "consolidated_from_candidate": 1}

    monkeypatch.setattr(R.learning, "consolidate_apply", _fake_apply, raising=False)

    enabled = {"value": True}
    monkeypatch.setattr(R, "is_app_enabled", lambda _name: enabled["value"], raising=False)

    response = await R._handle_consolidate(cast(web.Request, _StubConsolidateRequest()))
    assert response.status == 200, "the enabled admission itself must succeed"

    merge_task = asyncio.gather(*list(R._TASKS))
    assert await asyncio.to_thread(in_dispatch.wait, 30), "the merge never reached its dispatch"

    if disable_mid_dispatch:
        async with app_lifecycle_lock(APP_NAME):
            await teardown.notify_app_disabled(APP_NAME)
            enabled["value"] = False

    release_dispatch.set()
    await merge_task

    ns = R.learning.DEFAULT_NAMESPACE
    return {
        "created": created,
        "applied": applied,
        "state": dict(R._CONSOLIDATE_STATE.get(ns) or {}),
        "claim_released": ns not in R._CONSOLIDATING,
        "mark_left": ns in R._CANCELLED_NS,
    }


@pytest.mark.asyncio
async def test_a_disable_during_the_merge_dispatch_stops_the_apply(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """THE BLOCKER, consolidation's second half: the top-of-function recheck
    cannot see a disable that lands while the worker turn is in flight. The
    APPLY is the persisted-state mutation — it rewrites the ruleset and clears
    the staged candidates, destroying the only copy of pending learnings on an
    authority the operator has withdrawn — and it happens entirely after the
    dispatch returns, so the mark must be re-read on the near side of
    ``consolidate_apply``. Refusing there leaves the ruleset and the staged
    candidates untouched for a later re-enable.
    """
    out = await _drive_merge_dispatch_race(monkeypatch, tmp_path, disable_mid_dispatch=True)

    assert out["applied"] == [], (
        "a merge dispatched before the disable applied its result after it — "
        "persisted learning state mutated on withdrawn authority"
    )
    assert out["state"].get("error"), "the state must say why the merge did not land"
    assert out["claim_released"], "a refused merge must release its claim"
    assert not out["mark_left"], "the mark spoke for this merge and must be consumed"


@pytest.mark.asyncio
async def test_a_merge_applies_normally_when_no_disable_lands(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Negative control: the same held-open dispatch with no disable applies
    its merge exactly once."""
    out = await _drive_merge_dispatch_race(monkeypatch, tmp_path, disable_mid_dispatch=False)

    assert out["applied"] == [
        R.learning.DEFAULT_NAMESPACE
    ], "the enabled path must apply the merge exactly once"
    assert not out["state"].get("error"), "a clean merge must not report an error"
    assert out["claim_released"], "the claim must be released when the merge ends"
