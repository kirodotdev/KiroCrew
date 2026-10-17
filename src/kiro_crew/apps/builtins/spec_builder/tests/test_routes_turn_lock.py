"""Route tests: the per-directory turn lock.

Every handler that registers a spec, arms a loop or dispatches a turn holds the
lock keyed by the spec directory, with each serialized effect inside it; delete,
stop and create wait for it; delete never evicts it; and a lock bound to a dead
loop is not reused.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap

import pytest

from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import routes, routes_source

# _never_touch_the_real_state is an autouse fixture: pytest finds it by the name bound here.
from kiro_crew.apps.builtins.spec_builder.tests.routes_helpers import (  # noqa: F401
    _BASE,
    _create_client,
    _decision_spec,
    _IdleSlot,
    _make_client,
    _never_touch_the_real_state,
    _recorded_answers,
    _redirect_state,
    _seed_spec,
    _slot_stub,
    _spec_identity,
    _state_for,
)
from kiro_crew.testing.wait import until_parked

_SERIALIZED_EFFECTS = (
    "_mutate_index(",  # registers or removes an index entry
    "_save_index(",  # ditto, unlocked writer
    "authorize_and_add_nudge(",  # arms an autonomous timer that will dispatch later
    "_dispatch_turn(",  # starts a turn now
)


# ── gap 3: one task at a time, addressed by position AND text ────────────────


@pytest.mark.asyncio
async def test_task_slot_materialization_serializes_delete_capture(tmp_path, monkeypatch):
    """Delete cannot capture no slot while a task is about to restore that slot."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"tasks.md": "- [ ] add the tests\n"})
    state, slots = _state_for()
    ensure_waiting = asyncio.Event()
    release_ensure = asyncio.Event()
    delete_waiting = asyncio.Event()
    mark_entered = asyncio.Event()
    real_lock = routes._turn_lock
    real_mark = routes._mark_deleting
    lock_calls = 0
    captured: list[object] = []
    order: list[str] = []

    async def _held_slot_materialization(_state, _name, _meta):
        ensure_waiting.set()
        await release_ensure.wait()
        key = routes._slot_key("live")
        slots[key] = _IdleSlot(key)
        return slots[key]

    def _watched_lock(spec_dir):
        nonlocal lock_calls
        lock_calls += 1
        if lock_calls == 2:
            delete_waiting.set()
        return real_lock(spec_dir)

    async def _watched_mark(*args, **kwargs):
        mark_entered.set()
        order.append("reserve-delete")
        return await real_mark(*args, **kwargs)

    async def _remove_loop(*args, **kwargs):
        return None

    async def _capture_teardown(*args, **kwargs):
        captured.append(kwargs.get("only_slot"))
        return True

    monkeypatch.setattr(routes, "_ensure_worker_slot", _held_slot_materialization)
    monkeypatch.setattr(routes, "_turn_lock", _watched_lock)
    monkeypatch.setattr(routes, "_mark_deleting", _watched_mark)
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_args: order.append("dispatch-task"))
    monkeypatch.setattr(routes, "_remove_nudge_loop", _remove_loop)
    monkeypatch.setattr(routes, "_teardown_worker_slot", _capture_teardown)

    client.app["state"] = state
    await client.start_server()
    try:
        body = {**_spec_identity(), "index": 0, "hash": routes._sha256_text("add the tests")}
        task_request = asyncio.create_task(client.post(f"{_BASE}/specs/live/task", json=body))
        await asyncio.wait_for(ensure_waiting.wait(), timeout=5)
        delete_request = asyncio.create_task(client.delete(f"{_BASE}/specs/live"))
        await asyncio.wait_for(delete_waiting.wait(), timeout=5)
        assert not mark_entered.is_set(), "Delete captured the runtime during slot restore"
        release_ensure.set()
        task_response, delete_response = await asyncio.gather(task_request, delete_request)
    finally:
        release_ensure.set()
        await client.close()

    assert task_response.status == 200 and delete_response.status == 200
    assert order == ["dispatch-task", "reserve-delete"]
    assert captured == [slots[routes._slot_key("live")]], "Delete missed the restored slot"


@pytest.mark.asyncio
async def test_task_final_snapshot_serializes_the_whole_plan_claim(tmp_path, monkeypatch):
    """Execute cannot claim the spec while a task's final disk snapshot awaits."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"tasks.md": "- [ ] add the tests\n"})
    state, slots = _state_for("live")
    slot = slots[routes._slot_key("live")]
    release_snapshot = asyncio.Event()
    snapshot_waiting = asyncio.Event()
    execute_waiting = asyncio.Event()
    claim_entered = asyncio.Event()
    real_to_thread = routes.asyncio.to_thread
    real_lock = routes._turn_lock
    real_claim = routes._claim_execution
    snapshot_calls = 0
    lock_calls = 0
    sent: list[str] = []

    async def _held_final_snapshot(func, *args, **kwargs):
        nonlocal snapshot_calls
        if getattr(func, "__name__", "") == "_task_snapshot":
            snapshot_calls += 1
            if snapshot_calls == 2:
                snapshot_waiting.set()
                await release_snapshot.wait()
        return await real_to_thread(func, *args, **kwargs)

    def _watched_lock(spec_dir):
        nonlocal lock_calls
        lock_calls += 1
        if lock_calls == 2:
            execute_waiting.set()
        return real_lock(spec_dir)

    async def _watched_claim(*args, **kwargs):
        claim_entered.set()
        return await real_claim(*args, **kwargs)

    def _mark_running(_state, _slot, message):
        sent.append(message)
        slot.running = True

    monkeypatch.setattr(routes.asyncio, "to_thread", _held_final_snapshot)
    monkeypatch.setattr(routes, "_turn_lock", _watched_lock)
    monkeypatch.setattr(routes, "_claim_execution", _watched_claim)
    monkeypatch.setattr(routes, "_dispatch_turn", _mark_running)
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: object())

    client.app["state"] = state
    await client.start_server()
    try:
        body = {**_spec_identity(), "index": 0, "hash": routes._sha256_text("add the tests")}
        task_request = asyncio.create_task(client.post(f"{_BASE}/specs/live/task", json=body))
        await asyncio.wait_for(snapshot_waiting.wait(), timeout=5)
        execute_request = asyncio.create_task(client.post(f"{_BASE}/specs/live/execute"))
        await asyncio.wait_for(execute_waiting.wait(), timeout=5)
        assert not claim_entered.is_set(), "Execute crossed the task's final snapshot"
        release_snapshot.set()
        task_response, execute_response = await asyncio.gather(task_request, execute_request)
        execute_body = await execute_response.json()
    finally:
        release_snapshot.set()
        slot.running = False
        await client.close()

    assert task_response.status == 200
    assert execute_response.status == 409 and execute_body["code"] == "already_executing"
    assert claim_entered.is_set() and len(sent) == 1


@pytest.mark.asyncio
async def test_task_final_snapshot_serializes_delete_reservation(tmp_path, monkeypatch):
    """Delete reserves teardown only after a final task snapshot has dispatched."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"tasks.md": "- [ ] add the tests\n"})
    state, _ = _state_for("live")
    release_snapshot = asyncio.Event()
    snapshot_waiting = asyncio.Event()
    delete_waiting = asyncio.Event()
    mark_entered = asyncio.Event()
    real_to_thread = routes.asyncio.to_thread
    real_lock = routes._turn_lock
    real_mark = routes._mark_deleting
    snapshot_calls = 0
    lock_calls = 0
    order: list[str] = []

    async def _held_final_snapshot(func, *args, **kwargs):
        nonlocal snapshot_calls
        if getattr(func, "__name__", "") == "_task_snapshot":
            snapshot_calls += 1
            if snapshot_calls == 2:
                snapshot_waiting.set()
                await release_snapshot.wait()
        return await real_to_thread(func, *args, **kwargs)

    def _watched_lock(spec_dir):
        nonlocal lock_calls
        lock_calls += 1
        if lock_calls == 2:
            delete_waiting.set()
        return real_lock(spec_dir)

    async def _watched_mark(*args, **kwargs):
        mark_entered.set()
        order.append("reserve-delete")
        return await real_mark(*args, **kwargs)

    async def _remove_loop(*args, **kwargs):
        return None

    async def _teardown(*args, **kwargs):
        return True

    monkeypatch.setattr(routes.asyncio, "to_thread", _held_final_snapshot)
    monkeypatch.setattr(routes, "_turn_lock", _watched_lock)
    monkeypatch.setattr(routes, "_mark_deleting", _watched_mark)
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_args: order.append("dispatch-task"))
    monkeypatch.setattr(routes, "_remove_nudge_loop", _remove_loop)
    monkeypatch.setattr(routes, "_teardown_worker_slot", _teardown)

    client.app["state"] = state
    await client.start_server()
    try:
        body = {**_spec_identity(), "index": 0, "hash": routes._sha256_text("add the tests")}
        task_request = asyncio.create_task(client.post(f"{_BASE}/specs/live/task", json=body))
        await asyncio.wait_for(snapshot_waiting.wait(), timeout=5)
        delete_request = asyncio.create_task(client.delete(f"{_BASE}/specs/live"))
        await asyncio.wait_for(delete_waiting.wait(), timeout=5)
        assert not mark_entered.is_set(), "Delete crossed the task's final snapshot"
        release_snapshot.set()
        task_response, delete_response = await asyncio.gather(task_request, delete_request)
    finally:
        release_snapshot.set()
        await client.close()

    assert task_response.status == 200 and delete_response.status == 200
    assert order == ["dispatch-task", "reserve-delete"]


@pytest.mark.asyncio
async def test_no_turn_can_start_while_an_answer_is_being_recorded(tmp_path, monkeypatch):
    """The turn lock, behaviourally.

    A turn starting between the running-check and the dispatch would have the answer
    QUEUED behind it (and droppable by a Pause) while the ledger claims it was
    delivered. Undoing the claim at that point needs a write that can itself fail, so
    the answer path holds the lock instead: another would-be dispatcher cannot get in
    until this one has dispatched.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    # What the racing dispatcher observed when it finally got the lock: the number of
    # dispatches already made. 0 would mean it cut in before ours.
    seen_at_acquire: list[int] = []
    race: list = []
    real_claim = routes._claim_decision

    async def _claim_while_another_dispatcher_waits(*a, **kw):
        async def _other_dispatcher() -> None:
            # By DIRECTORY, the key the handler uses (see _turn_lock).
            async with routes._turn_lock(spec_dir):
                seen_at_acquire.append(len(dispatched))

        race.append(asyncio.create_task(_other_dispatcher()))
        await asyncio.sleep(0)  # let it reach the lock
        return await real_claim(*a, **kw)

    monkeypatch.setattr(routes, "_claim_decision", _claim_while_another_dispatcher_waits)

    state, _slot = _slot_stub()
    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": "transport",
                "decision_option": "HTTPS",
                "text": "Decision - transport: HTTPS",
            },
        )
        body = await resp.text()
    finally:
        await client.close()
    await asyncio.gather(*race)

    assert resp.status == 200, body
    assert len(dispatched) == 1
    assert seen_at_acquire == [1], (
        "another dispatcher acquired the turn lock before this answer was dispatched, "
        f"so the answer could have been queued: {seen_at_acquire!r}"
    )


def test_every_turn_start_takes_the_turn_lock():
    """Source guard: a dispatcher that skips the lock can start a turn between a
    decision answer's running-check and its dispatch, which is what the lock exists to
    prevent. The CREATE path is exempt: the spec does not exist before it, so no answer
    can be in flight for it."""
    for handler in (routes._handle_message, routes._handle_handoff):
        src = inspect.getsource(handler)
        assert (
            "async with _turn_lock(" in src
        ), f"{handler.__name__} dispatches a turn without taking the turn lock"
        assert src.index("async with _turn_lock(") < src.index(
            "_dispatch_turn("
        ), f"{handler.__name__} takes the lock after dispatching"


@pytest.mark.asyncio
async def test_deleting_a_spec_retains_its_turn_lock(tmp_path, monkeypatch):
    """A deleted spec's turn lock is RETAINED, not dropped as housekeeping.

    Eviction is unsafe at any reference count. A handler that called `_turn_lock()`
    before the eviction is already waiting on the OLD object, so the next arrival is
    handed a brand-new lock and the two serialize against nothing -- concurrent turns
    over the same documents, which is the hole the directory-keyed lock exists to
    close, reopened by its own cleanup. A waiter holds the object rather than an index
    entry, so no reference count can make the eviction safe; the lock stays.

    What is given up is one small asyncio.Lock per directory that ever had a turn.
    The ledger clear is unaffected and still asserted here.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, _slot_key = _decision_spec(tmp_path)
    lock = routes._turn_lock(spec_dir)
    assert routes._turn_key(spec_dir) in routes._TURN_LOCKS

    client.app["state"] = None
    await client.start_server()
    try:
        resp = await client.delete(f"{_BASE}/specs/s")
    finally:
        await client.close()

    assert resp.status == 200, await resp.text()
    assert routes._turn_lock(spec_dir) is lock, (
        "the delete replaced the directory's lock, so a waiter and the next arrival "
        "would serialize on different objects"
    )
    assert await _recorded_answers(spec_dir) == {}


@pytest.mark.asyncio
async def test_the_delete_holds_the_turn_lock(tmp_path, monkeypatch):
    """A handoff that has already passed its freshness check would otherwise acquire
    the lock after the delete released it and start a turn on a spec that is gone. The
    delete holds the lock across its whole destructive sequence, so the other handler
    sees either a live spec or none."""
    client = _make_client(monkeypatch, tmp_path)
    _decision_spec(tmp_path)

    # What the racing dispatcher saw when it got the lock: whether the spec was still
    # indexed. With the delete holding the lock it must be gone by then.
    still_indexed: list[bool] = []
    race: list = []
    real_forget = routes._forget_decisions

    async def _forget_while_another_dispatcher_waits(*a, **kw):
        async def _other_dispatcher() -> None:
            async with routes._turn_lock("s"):
                still_indexed.append("s" in await routes._aload_index())

        race.append(asyncio.create_task(_other_dispatcher()))
        await asyncio.sleep(0)
        return await real_forget(*a, **kw)

    monkeypatch.setattr(routes, "_forget_decisions", _forget_while_another_dispatcher_waits)

    client.app["state"] = None
    await client.start_server()
    try:
        resp = await client.delete(f"{_BASE}/specs/s")
    finally:
        await client.close()
    await asyncio.gather(*race)

    assert resp.status == 200, await resp.text()
    assert still_indexed == [False], (
        "another dispatcher held the lock while the spec was still indexed mid-delete, "
        f"so it could have started a turn on a spec being removed: {still_indexed!r}"
    )


def test_a_turn_lock_from_a_dead_loop_is_not_reused(tmp_path, monkeypatch):
    """An asyncio.Lock binds to the loop that first awaits it, so a module-level
    registry outliving a loop would hand back a lock bound to the dead one and raise
    "is bound to a different event loop" on acquisition.

    Deliberately NOT an asyncio test: it drives two loops of its own, which cannot be
    done from inside a running one."""
    _redirect_state(monkeypatch, tmp_path)

    async def _take() -> int:
        lock = routes._turn_lock("s")
        async with lock:
            return id(lock)

    first = asyncio.new_event_loop()
    try:
        first_id = first.run_until_complete(_take())
    finally:
        first.close()

    second = asyncio.new_event_loop()
    try:
        second_id = second.run_until_complete(_take())  # must not raise
    finally:
        second.close()

    assert first_id != second_id, "the lock from the closed loop was reused"


def test_every_destructive_or_dispatching_handler_takes_the_turn_lock():
    """Source guard. A handler that starts OR destroys a turn without the lock can
    interleave with an answer being recorded. The CREATE path is exempt: the spec does
    not exist before it, so no answer can be in flight for it."""
    for handler in (routes._handle_message, routes._handle_handoff, routes._handle_delete):
        src = inspect.getsource(handler)
        tree = ast.parse(textwrap.dedent(src))
        locked = any(
            isinstance(node, ast.AsyncWith)
            and any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Name)
                and item.context_expr.func.id == "_turn_lock"
                for item in node.items
            )
            for node in ast.walk(tree)
        )
        assert locked, f"{handler.__name__} runs without taking the turn lock"


def test_the_turn_lock_is_keyed_by_directory_not_name():
    """Source guard. A per-name lock is what let two aliases start turns on one
    directory concurrently, so the key is worth pinning."""
    for handler in (routes._handle_message, routes._handle_handoff, routes._handle_delete):
        body = inspect.getsource(handler)
        assert (
            "_turn_lock(name)" not in body
        ), f"{handler.__name__} still locks by name, so an alias gets its own lock"
        assert (
            "_turn_lock(_decision_key" not in body
        ), f"{handler.__name__} should hold the key in a local, not re-derive it"
        assert (
            "_decision_key(" in body
        ), f"{handler.__name__} does not derive its lock key from the directory"


@pytest.mark.asyncio
async def test_deleting_one_alias_keeps_the_shared_turn_lock(tmp_path, monkeypatch):
    """The lock is keyed by directory, so it must outlive any one name using it.

    Dropping it while another alias is still indexed would hand the next arrival a
    brand-new lock object, and a turn could then start on documents another turn is
    already working -- the very race the directory-keyed lock closes. Only the LAST name
    on a directory may retire its lock.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, _slot_key = _decision_spec(tmp_path)
    routes._turn_lock(spec_dir)
    key = routes._turn_key(spec_dir)
    assert key in routes._TURN_LOCKS
    held = routes._TURN_LOCKS[key]

    index = await routes._aload_index()
    index["s-alias"] = {**index["s"], "slot_key": routes._new_slot_key("s-alias")}
    routes._save_index(index)

    client.app["state"] = None
    await client.start_server()
    try:
        resp = await client.delete(f"{_BASE}/specs/s")
    finally:
        await client.close()

    assert resp.status == 200, await resp.text()
    assert routes._TURN_LOCKS.get(key) is held, "the surviving alias lost the lock it serializes on"


@pytest.mark.asyncio
async def test_stop_waits_for_an_in_flight_decision_answer(tmp_path, monkeypatch):
    """The race: Stop must not cancel a turn a decision answer is mid-dispatch.

    The answer path records the answer and dispatches it while holding the
    directory turn lock. A lockless Stop lands BETWEEN those two steps -- the turn
    it cancels is the one carrying the answer, and because the record is never
    rewritten the card stays locked to an answer the agent never received. Stop
    takes the same lock, so it cannot observe that midpoint.

    Timing is deliberate: the halt sets an Event, and the assertion is that it does
    not fire within a REAL timeout while the lock is held. An earlier version of
    this test yielded with sleep(0), which never let the request cross its thread
    hops -- so the halt was absent either way and removing the lock still passed.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, _unused = _decision_spec(tmp_path)
    dir_key = routes._decision_key(spec_dir)

    halted = asyncio.Event()

    async def _halt_spy(*_a, **_kw):
        halted.set()

    monkeypatch.setattr(routes, "_halt_execution", _halt_spy)

    state, _slot = _slot_stub()
    client.app["state"] = state
    await client.start_server()
    try:
        async with routes._turn_lock(dir_key):
            stop = asyncio.ensure_future(client.post(f"{_BASE}/specs/s/stop", json={}))
            with pytest.raises(asyncio.TimeoutError):
                # Long enough for the request to finish _client_claim, _aload_index
                # and _acanonical_dir -- i.e. long enough to reach the halt if it
                # were not serialized. The mutation that frees the lock trips this.
                await asyncio.wait_for(asyncio.shield(halted.wait()), timeout=1.5)
            assert (
                not halted.is_set()
            ), "Stop halted the run while the answer path held the turn lock"
        # Released: the same request must now get through.
        await asyncio.wait_for(halted.wait(), timeout=5)
        resp = await stop
        await resp.read()
    finally:
        await client.close()

    assert halted.is_set(), "Stop never halted after the lock was released"


def test_stop_takes_the_directory_turn_lock():
    """Source guard: Stop is destructive and must join the serialized family.

    The behavioural test above can only observe the lock it knows about; this
    pins that Stop keeps taking it, and takes it on the CANONICAL directory (a
    name-keyed lock would not serialize two names on one spec).
    """
    src = inspect.getsource(routes._handle_stop_execution)
    assert "_turn_lock(" in src, "Stop does not serialize against the answer path"
    assert "_decision_key(" in src, "Stop keys its lock on something other than the directory"
    lock_at = src.index("_turn_lock(")
    assert "_halt_execution(" in src[lock_at:], "the halt runs outside the lock"


def test_stop_rechecks_the_spec_after_waiting_for_the_lock():
    """Waiting on a lock is an await, so the pre-lock snapshot can go stale.

    Halting on that snapshot could cancel a REPLACEMENT spec's run, which is the
    same class of bug the body-first ordering above the lock already guards.
    """
    src = inspect.getsource(routes._handle_stop_execution)
    lock_at = src.index("_turn_lock(dir_key)")
    after = src[lock_at:]
    assert "_aload_index()" in after, "Stop reuses a snapshot taken before it held the lock"
    assert after.index("_aload_index()") < after.index(
        "_halt_execution("
    ), "Stop re-reads the index only after halting, which is too late"


def test_no_path_evicts_a_turn_lock():
    """Source guard: nothing may pop `_TURN_LOCKS`.

    The eviction is unsafe from every call site for the same reason, so the rule is
    global rather than per-handler -- a future cleanup that reintroduces it here
    would silently reopen concurrent turns.
    """
    src = routes_source()
    assert "_TURN_LOCKS.pop(" not in src, "a turn lock is evicted somewhere"
    assert "del _TURN_LOCKS[" not in src, "a turn lock is deleted somewhere"
    assert "_TURN_LOCKS.clear()" not in src, "the turn lock registry is cleared somewhere"


async def _index_has(name: str, *, timeout: float) -> bool:
    """True once *name* is registered, False if it never arrives in time."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if name in await asyncio.to_thread(routes._load_index):
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.mark.asyncio
async def test_create_waits_for_the_directory_turn_lock(tmp_path, monkeypatch):
    """A create must not register while a delete holds the directory's lock.

    Delete removes its index entry and THEN scans for other names referencing the
    directory to decide whether to clear the ledger. A registration landing after
    that scan let the cleanup erase answers the newly adopted spec owned, so the
    two must not interleave.

    The wait is measured against a REAL timeout: create crosses several thread
    hops before reaching the lock, so a sleep(0) yield would prove nothing.
    """
    project = tmp_path / "proj"
    project.mkdir()
    spec_dir = project / ".kiro" / "specs" / "serialized"
    client, state = _create_client(monkeypatch, tmp_path)
    routes._save_index({})
    dir_key = routes._decision_key(str(spec_dir))

    client.app["state"] = state
    await client.start_server()
    try:
        async with routes._turn_lock(dir_key):
            post = asyncio.ensure_future(
                client.post(
                    f"{_BASE}/specs",
                    json={
                        "name": "serialized",
                        "working_dir": str(project),
                        "spec_type": "feature",
                    },
                )
            )
            assert not await _index_has("serialized", timeout=1.5), (
                "the create registered while the directory's turn lock was held, so "
                "a delete's ledger cleanup could erase its answers"
            )
        # Released: the same request must now complete.
        assert await _index_has("serialized", timeout=10), "the create never registered"
        resp = await post
        assert resp.status == 201, await resp.json()
    finally:
        await client.close()


def test_every_handler_that_starts_work_holds_the_turn_lock():
    """The invariant covers every way a handler can cause work to start.

    Stated as "every handler that mutates the index holds the lock", it misses a
    handler that ARMS A TIMER outside the lock: the timer dispatches on its own while
    the handler still waits for the lock. Causing work to start has three shapes, not
    one -- register/remove an index entry, arm a nudge loop, or dispatch a turn --
    and all three must be serialized on the directory.

    Enumerated rather than listed by name, so a handler added later is covered.
    """
    offenders: dict[str, list[str]] = {}
    checked: list[str] = []
    for attr in dir(routes):
        if not attr.startswith("_handle_"):
            continue
        fn = getattr(routes, attr)
        if not callable(fn):
            continue
        try:
            src = inspect.getsource(fn)
        except (OSError, TypeError):  # pragma: no cover - not Python-defined
            continue
        effects = [e for e in _SERIALIZED_EFFECTS if e in src]
        if not effects:
            continue
        checked.append(attr)
        if "_turn_lock(" not in src:
            offenders[attr] = effects

    assert checked, "the guard found no work-starting handlers -- it has gone blind"
    assert not offenders, (
        f"these handlers start work without the directory turn lock: {offenders}. "
        "Registering an entry, arming a nudge timer and dispatching a turn are all "
        "ways to put an agent into a spec's files, so each needs serializing."
    )


def _effect_calls_outside_turn_lock(fn) -> list[str]:
    """Serialized effects in *fn* that are NOT lexically inside an `async with
    _turn_lock(...)` block.

    On the AST rather than on text, because "is the lock present in this function"
    is the wrong question: create can hold the lock around its index insert and
    dispatch its seed ~190 lines after the block closed. Containment is the property;
    presence is not.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))

    guarded: list[ast.AST] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncWith):
            continue
        for item in node.items:
            call = item.context_expr
            name = (
                call.func.id
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                else ""
            )
            if name == "_turn_lock":
                guarded.append(node)

    guarded_nodes = {id(inner) for block in guarded for inner in ast.walk(block)}
    outside = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fname = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        if fname not in {
            "_mutate_index",
            "_save_index",
            "authorize_and_add_nudge",
            "_dispatch_turn",
        }:
            continue
        if id(node) not in guarded_nodes:
            outside.append(f"{fname}@line{node.lineno}")
    return outside


def test_every_serialized_effect_is_inside_the_lock_not_merely_near_it():
    """The invariant is positional: per-path and presence-anywhere are both weaker.

    A guard that checks one path, or only that the lock appears somewhere in the
    handler, is weaker than the rule it stands for. This checks CONTAINMENT on
    the AST, for every handler and every effect, which is the form that catches
    create dispatching its seed after the block closed.
    """
    offenders: dict[str, list[str]] = {}
    checked: list[str] = []
    for attr in dir(routes):
        if not attr.startswith("_handle_"):
            continue
        fn = getattr(routes, attr)
        if not callable(fn):
            continue
        try:
            inspect.getsource(fn)
        except (OSError, TypeError):  # pragma: no cover - not Python-defined
            continue
        outside = _effect_calls_outside_turn_lock(fn)
        src = inspect.getsource(fn)
        if not any(
            e in src
            for e in (
                "_mutate_index(",
                "_save_index(",
                "authorize_and_add_nudge(",
                "_dispatch_turn(",
            )
        ):
            continue
        checked.append(attr)
        if outside:
            offenders[attr] = outside

    assert checked, "the guard found no work-starting handlers -- it has gone blind"
    assert not offenders, (
        f"these handlers start work OUTSIDE the directory turn lock: {offenders}. "
        "Registering an entry, arming a nudge timer and dispatching a turn must each "
        "happen inside the block, not merely somewhere in the same function."
    )


@pytest.mark.asyncio
async def test_create_dispatches_its_seed_before_anything_else_can_run(tmp_path, monkeypatch):
    """A registered spec whose seed has not been dispatched must accept nothing else.

    A lock closing at the index insert lets a list poll expose the spec while create
    is still awaiting slot setup; a concurrent message then takes the lock and starts
    the FIRST turn, leaving the seed queued second and the persisted conversation
    beginning with something other than the prompt that defines the spec.
    """
    project = tmp_path / "proj"
    project.mkdir()
    spec_dir = project / ".kiro" / "specs" / "seeded"
    client, state = _create_client(monkeypatch, tmp_path)
    routes._save_index({})
    dir_key = routes._decision_key(str(spec_dir))

    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    client.app["state"] = state
    await client.start_server()
    try:
        held = routes._turn_lock(dir_key)
        async with held:
            post = asyncio.ensure_future(
                client.post(
                    f"{_BASE}/specs",
                    json={"name": "seeded", "working_dir": str(project), "spec_type": "feature"},
                )
            )
            # Create has run everything before the lock once it is parked on it,
            # so the absence below is about the lock, not about how far it got.
            await until_parked(held, describe=lambda: "create never reached the held turn lock")
            assert dispatched == [], (
                "create dispatched its seed while the directory's turn lock was held, "
                "so a concurrent turn could have gone first"
            )
        resp = await post
        assert resp.status == 201, await resp.json()
    finally:
        await client.close()

    assert dispatched, "the seed was never dispatched"


def test_discovery_is_exempt_only_because_the_tombstone_covers_it():
    """The one adopter outside the lock family, and why that is sound.

    Discovery adopts directories found on disk and cannot take a per-directory lock
    for an unbounded sweep. It is safe because a delete leaves a tombstone that
    discovery consults, so it cannot adopt a directory mid-teardown. If that
    consultation ever disappears, discovery needs the lock instead -- and this test
    is what says so.
    """
    src = inspect.getsource(routes._discover_folder_specs)
    assert "_deleted" in src or "tombstone" in src.lower(), (
        "discovery no longer consults the delete tombstone, so it can adopt a "
        "directory whose teardown is still running -- it now needs the turn lock"
    )


@pytest.mark.asyncio
async def test_two_spellings_of_one_directory_share_the_turn_lock(monkeypatch):
    """A raw path and a normalized key must resolve to the SAME lock object.

    The ledger key is lexical via `normcase`, which lowercases on Windows. A call site
    passing a raw path would hash to a different dictionary entry than one passing
    `_decision_key(...)` -- two locks for one directory, so two turns could run on the
    same documents, which is the exact hole the directory-keyed lock exists to close.

    `normcase` is the identity on Linux, so this simulates Windows explicitly. Without
    the patch the assertion holds trivially and proves nothing.
    """
    monkeypatch.setattr(routes.os.path, "normcase", lambda p: p.lower())
    routes._TURN_LOCKS.clear()

    raw = "/Some/Spec/Dir"
    lock_from_raw = routes._turn_lock(raw)
    lock_from_key = routes._turn_lock(routes._decision_key(raw))
    lock_from_other_case = routes._turn_lock("/SOME/spec/DIR")

    assert lock_from_raw is lock_from_key, (
        "a raw path and its normalized key produced different locks, so two handlers "
        "would serialize on different objects for one directory"
    )
    assert (
        lock_from_raw is lock_from_other_case
    ), "two case spellings of one directory produced different locks"
    assert (
        len(routes._TURN_LOCKS) == 1
    ), f"one directory minted {len(routes._TURN_LOCKS)} lock entries"


@pytest.mark.asyncio
async def test_macos_case_variants_share_the_turn_lock(monkeypatch):
    """Darwin preserves path case even when its volume does not distinguish it."""
    monkeypatch.setattr(routes, "_CASE_FOLD_TURN_KEYS", True)
    monkeypatch.setattr(routes.os.path, "normcase", lambda path: path)
    routes._TURN_LOCKS.clear()

    first = routes._turn_lock("/Some/Spec/CaseSpec")
    second = routes._turn_lock("/Some/Spec/casespec")

    assert first is second
    assert len(routes._TURN_LOCKS) == 1
