"""Route tests: deleting a spec.

Delete reserves the name, tombstones the directory, tears down the worker slot
and its loop, and only then drops the index entry; a failure restores what it
took. Pause and delete discard queued work before they stop the turn, and a new
spec never inherits a deleted one's conversation.
"""

from __future__ import annotations

import contextlib
import inspect
import os
import re
import sys
import threading
import types
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import routes

# _never_touch_the_real_state is an autouse fixture: pytest finds it by the name bound here.
from kiro_crew.apps.builtins.spec_builder.tests.routes_helpers import (  # noqa: F401
    _BASE,
    _decision_spec,
    _make_client,
    _never_touch_the_real_state,
    _noop_await,
    _redirect_state,
)
from kiro_crew.testing.wait import default_timeout

# ── handlers offload filesystem work; delete tears down the slot ─────────────


# (2) deleting a spec must tear down its worker slot


@pytest.mark.asyncio
async def test_delete_cancels_and_removes_the_worker_slot(tmp_path):
    """The reported leak: delete removed the nudge loop and the index entry but
    left the in-flight turn ALIVE, so the agent kept editing the user's files
    after they deleted the spec — and re-creating the same name resurrected the
    old transcript, since get_or_create_slot keys off the slot name."""
    cancelled = {"v": False}

    class _Task:
        def cancel(self):
            cancelled["v"] = True

        def __await__(self):
            async def _done():
                return None

            return _done().__await__()

    class _Slot:
        _app = "spec-builder"
        running = True
        task = _Task()

    slot = _Slot()
    slots = {"spec-builder-doomed": slot}

    class _State:
        _slots = slots

        def get_slot(self, key):
            return slots.get(key)

    await routes._teardown_worker_slot(_State(), "doomed")
    assert cancelled["v"] is True, "in-flight turn was not cancelled"
    assert "spec-builder-doomed" not in slots, "slot left in the registry"


@pytest.mark.asyncio
async def test_teardown_is_a_noop_without_state():
    await routes._teardown_worker_slot(None, "whatever")


def test_delete_handler_tears_down_the_slot():
    """Source guard so a future edit can't drop the teardown call."""

    assert "_teardown_worker_slot(" in inspect.getsource(routes._handle_delete)


# ── delete ordering and handoff-unwind gating ────────────────────────────────


@pytest.mark.asyncio
async def test_delete_commits_the_index_before_closing_the_session(tmp_path, monkeypatch):
    """The reported ordering bug: the teardown ran BEFORE the index pop, so a
    failing index write (disk full) returned 500 with the spec still listed and its
    conversation already discarded -- unusable and unrecoverable."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "keepme"
    routes._save_index({"keepme": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}})

    async def _boom(_mutate):
        raise OSError("index write failed")

    monkeypatch.setattr(routes, "_mutate_index", _boom)
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: None)

    class _Slot:
        key = "spec-builder-keepme"
        _app = routes.APP_NAME
        running = False
        task = None

    slot = _Slot()
    slots = {"spec-builder-keepme": slot}

    class _State:
        _slots = slots

        def get_slot(self, key):
            return slots.get(key)

    client.app["state"] = _State()
    await client.start_server()
    try:
        with contextlib.suppress(Exception):
            await client.delete(f"{_BASE}/specs/keepme")
    finally:
        await client.close()

    assert (
        slots.get("spec-builder-keepme") is slot
    ), "the session was discarded before the delete was committed"


def test_delete_orders_reserve_teardown_remove():
    """Source guard for the ordering the delete path depends on: the name is RESERVED
    before the session is torn down (so it cannot be taken while archival runs, and a
    rollback lands on the original entry with its own slot key), and the entry is only
    removed once the archive succeeded."""
    src = inspect.getsource(routes._handle_delete)
    reserve = src.index("_mark_deleting(")
    teardown = src.index("_teardown_worker_slot(")
    remove = src.index("await _mutate_index(", src.index("def _pop_if_same"))
    assert reserve < teardown, "the session is torn down before the name is reserved"
    assert teardown < remove, "the entry is removed before the conversation is archived"
    # The failure arm releases the reservation rather than renaming the spec.
    assert "_unmark_deleting(" in src
    assert "_restore_under_free_name" not in src, "the renamed rollback is back"


# ── failures are reported, never swallowed ───────────────────────────────────


@pytest.mark.asyncio
async def test_delete_aborts_when_the_loop_cannot_be_removed(tmp_path, monkeypatch):
    """The reported lie: a failing autonudge store was swallowed, so DELETE
    returned 200 with the spec dropped from the index while the persisted loop
    survived -- free to rearm after a restart against a re-imported same-name
    spec. The delete now fails and leaves the entry in place, so a retry means
    something."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "doomed"
    spec_dir.mkdir(parents=True)
    routes._save_index({"doomed": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}})

    async def _boom(*_a, **_k):
        raise OSError("autonudge store is read-only")

    monkeypatch.setattr(routes, "_remove_nudge_loop", _boom)
    torn_down: list[str] = []
    monkeypatch.setattr(routes, "_teardown_worker_slot", lambda *a, **k: _noop_await(torn_down))

    client.app["state"] = None
    await client.start_server()
    try:
        resp = await client.delete(f"{_BASE}/specs/doomed")
    finally:
        await client.close()

    assert resp.status == 503, await resp.text()
    assert "doomed" in routes._load_index(), "the spec was dropped despite the failure"
    assert torn_down == [], "the session was torn down for a delete that did not happen"


# ── create never inherits a deleted spec's conversation ──────────────────────


@pytest.mark.asyncio
async def test_create_does_not_inherit_a_deleted_specs_conversation(tmp_path, monkeypatch):
    """Transcript restore at the chokepoint passes adopt_closed=True, and a delete
    leaves the archived conversation on disk under a key derived from the NAME -- so
    creating a new spec with an already-used name would hand the fresh agent the
    deleted spec's chat."""
    _redirect_state(monkeypatch, tmp_path)
    seen: list[bool] = []

    async def _spy(_state, _key, *, adopt_closed=True):
        seen.append(adopt_closed)
        return None

    monkeypatch.setattr(routes, "rehydrate_slot_from_history_async", _spy)

    class _State:
        def get_slot(self, key):
            return None

        def get_or_create_slot(self, name, app=""):
            return types.SimpleNamespace(key=name, _app=app, project="", messages=[], _titled=False)

    meta = {"working_dir": str(tmp_path), "spec_dir": str(tmp_path / "s")}
    await routes._ensure_worker_slot(_State(), "reused-name", meta, adopt_closed=False)
    assert seen == [False], "a fresh spec was offered closed history"

    # The default stays True for a spec already in the index: idle-slot cleanup
    # closes those without the user asking, and losing them was the round-36 bug.
    seen.clear()
    await routes._ensure_worker_slot(_State(), "existing", meta)
    assert seen == [True]


def test_create_handler_refuses_closed_history():
    """Source guard: the create path must pass adopt_closed=False on the CALL.
    Matching the bare keyword also matched the comment above it explaining why —
    a guard a revert could not fail."""
    src = inspect.getsource(routes._handle_create)
    assert (
        "_ensure_worker_slot(state, name, entry, adopt_closed=False)" in src
    ), "create can adopt a deleted conversation again"


@pytest.mark.asyncio
async def test_delete_restores_the_spec_when_archiving_fails(tmp_path, monkeypatch):
    """The reported data loss: the closing history write was best-effort, so a
    failed archive was logged at DEBUG while DELETE returned 200 -- the spec gone
    from the index and the conversation never written. The entry now goes back and
    the caller is told to retry."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "keepme"
    spec_dir.mkdir(parents=True)
    entry = {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd"), "spec_type": "plan"}
    routes._save_index({"keepme": entry})

    slot = types.SimpleNamespace(
        key=routes._slot_key("keepme"),
        _app=routes.APP_NAME,
        running=False,
        task=None,
        messages=[{"role": "user", "content": "unsaved work"}],
    )

    class _State:
        def __init__(self):
            self._slots = {routes._slot_key("keepme"): slot}

        def get_slot(self, key):
            return self._slots.get(key)

    state = _State()
    monkeypatch.setattr(routes, "_remove_nudge_loop", lambda *a, **k: _noop_await([]))

    async def _archive_boom(*_a, **_k):
        raise OSError("history volume is full")

    import kiro_crew.dashboard.chat_persistence as cp

    monkeypatch.setattr(cp, "save_slot_off_loop", _archive_boom)

    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.delete(f"{_BASE}/specs/keepme")
    finally:
        await client.close()

    assert resp.status == 503, await resp.text()
    index = routes._load_index()
    assert "keepme" in index, "the spec was dropped although its chat was never archived"
    assert index["keepme"]["spec_dir"] == str(spec_dir), "the entry came back altered"
    # The slot is back in the registry, so the conversation is still reachable.
    assert state.get_slot(routes._slot_key("keepme")) is slot


# ── tombstones, and slot keys that are per-creation ──────────────────────────


def test_deleted_specs_are_not_rediscovered(tmp_path, monkeypatch):
    """The reported resurrection: delete leaves the .md files on disk by design,
    and discovery adopts any spec-shaped directory under a known project root --
    so as long as a SIBLING spec kept that root indexed, the next list scan added
    the deleted spec straight back."""
    _redirect_state(monkeypatch, tmp_path)
    real = Path(os.path.realpath(tmp_path))
    project = real / "wd"
    keep, gone = (project / ".kiro" / "specs" / "keep"), (project / ".kiro" / "specs" / "gone")
    for d in (keep, gone):
        d.mkdir(parents=True)
        (d / "requirements.md").write_text("# r")

    index = {"keep": {"spec_dir": str(keep), "working_dir": str(project)}}

    # Baseline: without a tombstone the sibling's root brings "gone" back.
    routes._discover_folder_specs(dict(index))
    fresh = dict(index)
    assert routes._discover_folder_specs(fresh) is True
    assert "gone" in fresh

    # With the deletion remembered, it stays deleted.
    routes._remember_deleted(str(gone))
    guarded = dict(index)
    assert routes._discover_folder_specs(guarded) is False
    assert "gone" not in guarded

    # Creating it again is an explicit decision that outranks the tombstone.
    routes._forget_deleted(str(gone))
    revived = dict(index)
    assert routes._discover_folder_specs(revived) is True
    assert "gone" in revived


def test_tombstone_file_shape_is_untrusted_and_bounded(tmp_path, monkeypatch):
    """The file is app state on disk: a malformed shape must not crash discovery,
    and the list is capped so it cannot grow without limit."""
    _redirect_state(monkeypatch, tmp_path)
    routes._state_dir().mkdir(parents=True, exist_ok=True)
    for bad in ('{"not": "a list"}', "null", "[1, 2, 3]", "not json at all"):
        routes._deleted_path().write_text(bad)
        assert routes._load_deleted() == []

    for i in range(routes._MAX_TOMBSTONES + 25):
        routes._remember_deleted(f"/p/spec-{i}")
    kept = routes._load_deleted()
    assert len(kept) == routes._MAX_TOMBSTONES
    assert kept[-1] == f"/p/spec-{routes._MAX_TOMBSTONES + 24}", "newest deletion fell off"


# ── claims serialize; delete tombstones before it drops the entry ────────────


@pytest.mark.asyncio
async def test_delete_tombstones_before_dropping_the_entry(tmp_path, monkeypatch):
    """The reported window: the tombstone was written AFTER the session teardown, so
    between the entry being dropped and the tombstone landing the documents were on
    disk and untombstoned -- a list poll re-adopted them through discovery and the
    DELETE returned 200 with the spec still listed."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "bye"
    spec_dir.mkdir(parents=True)
    (spec_dir / "requirements.md").write_text("# r")
    routes._save_index({"bye": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}})
    order: list[str] = []
    real_remember = routes._remember_deleted

    def _spy(d):
        order.append("tombstone")
        real_remember(d)

    monkeypatch.setattr(routes, "_remember_deleted", _spy)

    original_mutate = routes._mutate_index

    async def _watched(fn, **kwargs):
        result = await original_mutate(fn, **kwargs)
        order.append("pop" if result else "pop-failed")
        return result

    monkeypatch.setattr(routes, "_mutate_index", _watched)

    client.app["state"] = None
    await client.start_server()
    try:
        resp = await client.delete(f"{_BASE}/specs/bye")
        status = resp.status
    finally:
        await client.close()

    assert status == 200
    assert order[:2] == ["tombstone", "pop"], f"the entry was dropped untombstoned: {order}"
    assert str(spec_dir) in routes._load_deleted(), "the directory is not tombstoned"


@pytest.mark.asyncio
async def test_failed_delete_clears_the_tombstone(tmp_path, monkeypatch):
    """Tombstoning first means every path that does NOT delete has to clear it --
    otherwise a spec that was never deleted stays suppressed from discovery."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "stay"
    spec_dir.mkdir(parents=True)
    (spec_dir / "requirements.md").write_text("# r")
    routes._save_index({"stay": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}})

    async def _teardown_fails(*_a, **_kw):
        return False

    monkeypatch.setattr(routes, "_teardown_worker_slot", _teardown_fails)

    client.app["state"] = None
    await client.start_server()
    try:
        resp = await client.delete(f"{_BASE}/specs/stay")
        status = resp.status
    finally:
        await client.close()

    assert status == 503
    assert "stay" in routes._load_index(), "the spec was not restored"
    assert (
        str(spec_dir) not in routes._load_deleted()
    ), "a spec that still exists is tombstoned; discovery will hide its documents"


def test_delete_orders_the_tombstone_before_the_pop():
    """Source guard: the tombstone write must precede the index pop, and every
    non-deleting arm must clear it."""
    src = inspect.getsource(routes._handle_delete)
    remember = src.index("_remember_deleted")
    pop = src.index("await _mutate_index(", src.index("def _pop_if_same"))
    assert remember < pop, "the entry is dropped before the directory is tombstoned"
    assert src.count("_forget_deleted") >= 2, "a non-deleting arm leaves the tombstone behind"


# ── tombstone writes hold the lock; errors carry a code ──────────────────────


def test_concurrent_tombstone_writes_do_not_lose_deletions(tmp_path, monkeypatch):
    """The reported race: _remember_deleted read the list, appended, and wrote it
    back without holding the lock. Two concurrent deletes both read the
    pre-existing list, so the second write dropped the first spec's tombstone --
    and that spec was rediscovered and reappeared after the user deleted it.

    Interleaves the transactions deliberately: the first writer is parked between its
    read and its write, which is exactly the window the lock has to close, and is
    released only once the second writer has made its next move. Under the lock that
    move is waiting to acquire it; under the reported race it is a second read of the
    same list. The parked writer is never released by a timeout: a three-party barrier
    here could only fill under the race, so the fixed code paid the full 10-second
    timeout on every run and the test told the two outcomes apart by elapsed time."""
    _redirect_state(monkeypatch, tmp_path)
    routes._remember_deleted("/p/keep-me")

    real_load = routes._load_deleted
    real_lock = routes._INDEX_LOCK
    first_parked = threading.Event()
    second_moved = threading.Event()
    release = threading.Event()
    reads: list[str] = []
    attempts: list[str] = []
    hold_failures: list[str] = []
    bookkeeping = threading.Lock()

    class _WatchedLock:
        """The real lock, reporting the second writer's arrival at it."""

        def __enter__(self):
            with bookkeeping:
                attempts.append(threading.current_thread().name)
                if len(attempts) == 2:
                    second_moved.set()
            return real_lock.__enter__()

        def __exit__(self, *exc):
            return real_lock.__exit__(*exc)

    def _slow_load():
        current = real_load()
        with bookkeeping:
            reads.append(threading.current_thread().name)
            first = len(reads) == 1
            if not first:
                second_moved.set()
        if first:
            first_parked.set()
            if not release.wait(timeout=default_timeout()):
                hold_failures.append("the test never released the first writer")
        return current

    monkeypatch.setattr(routes, "_INDEX_LOCK", _WatchedLock())
    monkeypatch.setattr(routes, "_load_deleted", _slow_load)

    threads = [
        threading.Thread(target=routes._remember_deleted, args=(f"/p/spec-{i}",)) for i in range(2)
    ]
    for t in threads:
        t.start()
    try:
        assert first_parked.wait(timeout=default_timeout()), "no writer reached its read"
        assert second_moved.wait(
            timeout=default_timeout()
        ), "the second writer neither read nor reached the lock"
        # Decide before releasing, so the verdict is about the parked window.
        reads_while_parked = list(reads)
    finally:
        release.set()
        for t in threads:
            t.join(timeout=default_timeout())
    for t in threads:
        assert not t.is_alive(), "a tombstone write deadlocked"
    assert not hold_failures, hold_failures[0]
    assert (
        len(reads_while_parked) == 1
    ), "the second writer read while the first was parked before its write"

    monkeypatch.setattr(routes, "_load_deleted", real_load)
    recorded = routes._load_deleted()
    assert (
        "/p/spec-0" in recorded and "/p/spec-1" in recorded
    ), f"a concurrent delete lost its tombstone: {recorded}"
    assert "/p/keep-me" in recorded, "an unrelated tombstone was dropped"


def test_both_tombstone_transactions_hold_the_lock():
    """Source guard: read and write must be inside the same critical section. A
    lock taken around the write alone still lets two readers see the same list."""
    for fn in (routes._remember_deleted, routes._forget_deleted):
        src = inspect.getsource(fn)
        assert "with _INDEX_LOCK:" in src, f"{fn.__name__} mutates the tombstones unlocked"
        held = src.index("with _INDEX_LOCK:")
        assert held < src.index("_load_deleted()"), f"{fn.__name__} reads before locking"
        assert held < src.index("atomic_write("), f"{fn.__name__} writes outside the lock"


# ── a failed archive restores the name; a reserved name holds ────────────────


@pytest.mark.asyncio
async def test_removal_failure_keeps_the_spec_hidden_for_a_retry(tmp_path, monkeypatch):
    """If the final index write fails the conversation is ALREADY archived, so
    un-deleting would be the lie the ordering exists to prevent. The reservation stays
    (spec hidden), and the 503 asks for a retry."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "stuck"
    spec_dir.mkdir(parents=True)
    routes._save_index({"stuck": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}})

    async def _ok_teardown(*_a, **_kw):
        return True

    real_mutate = routes._mutate_index
    calls = {"n": 0}

    async def _fail_the_removal(fn):
        calls["n"] += 1
        # The mark and durable teardown boundary succeed; the removal does not.
        if calls["n"] >= 3:
            return False
        return await real_mutate(fn)

    monkeypatch.setattr(routes, "_teardown_worker_slot", _ok_teardown)
    monkeypatch.setattr(routes, "_mutate_index", _fail_the_removal)

    client.app["state"] = None
    await client.start_server()
    try:
        resp = await client.delete(f"{_BASE}/specs/stuck")
        status, body = resp.status, await resp.json()
    finally:
        await client.close()

    assert status == 503
    assert body["code"] == "index_write_failed"
    assert "retry" in body["error"]
    monkeypatch.setattr(routes, "_mutate_index", real_mutate)
    entry = routes._load_index()["stuck"]
    assert entry.get(routes._DELETING), "the reservation was dropped, un-hiding the spec"
    monkeypatch.setattr(routes, "_PROCESS_ID", "replacement-process")
    restarted = routes._load_index()["stuck"]
    assert restarted.get(routes._DELETING), "restart exposed the torn-down spec"


# ── a delete reservation blocks dispatch ─────────────────────────────────────


@pytest.mark.asyncio
async def test_touch_spec_refuses_an_entry_reserved_for_deletion(tmp_path, monkeypatch):
    """The reported race: _DELETING was honoured only by the list filter, so a
    message landing mid-delete stamped the doomed entry and got a non-None return --
    which every caller reads as "the spec is live" -- then dispatched a turn. The
    agent kept editing the user's files after DELETE returned 200."""
    _make_client(monkeypatch, tmp_path)
    base = tmp_path / "wd" / ".kiro" / "specs"
    (base / "doomed").mkdir(parents=True)
    sd = str(base / "doomed")
    routes._save_index(
        {
            "doomed": {
                "spec_dir": sd,
                "working_dir": str(tmp_path / "wd"),
                "spec_type": "feature",
                "status": "planning",
                "slot_key": routes._new_slot_key("doomed"),
            }
        }
    )

    # Live: the stamp lands.
    assert await routes._touch_spec("doomed", expect_spec_dir=sd, status="planning") is not None

    assert await routes._mark_deleting("doomed", expect_spec_dir=sd, expect_slot_key="")
    # Reserved: every mutation through the chokepoint is now refused.
    assert await routes._touch_spec("doomed", expect_spec_dir=sd, status="executing") is None
    assert await routes._touch_spec("doomed") is None

    # Releasing the reservation restores it, so the refusal is not permanent.
    assert await routes._unmark_deleting("doomed", expect_spec_dir=sd)
    assert await routes._touch_spec("doomed", expect_spec_dir=sd) is not None


@pytest.mark.asyncio
async def test_message_during_delete_is_refused_not_dispatched(tmp_path, monkeypatch):
    """End to end through the HTTP surface: with the delete reservation held, POST
    /message must 409 and dispatch NOTHING."""
    client = _make_client(monkeypatch, tmp_path)
    base = tmp_path / "wd" / ".kiro" / "specs"
    (base / "doomed").mkdir(parents=True)
    sd = str(base / "doomed")
    routes._save_index(
        {
            "doomed": {
                "spec_dir": sd,
                "working_dir": str(tmp_path / "wd"),
                "spec_type": "feature",
                "status": "planning",
                "slot_key": routes._new_slot_key("doomed"),
            }
        }
    )
    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a))
    assert await routes._mark_deleting("doomed", expect_spec_dir=sd, expect_slot_key="")

    client.app["state"] = None
    await client.start_server()
    try:
        resp = await client.post(f"{_BASE}/specs/doomed/message", json={"text": "keep editing"})
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, body
    assert body.get("code") == "stale_client", body
    assert dispatched == [], "a turn was dispatched into a spec being deleted"


def test_delete_reserves_before_it_captures_the_runtime():
    """Source guard: the reservation must precede the slot/loop capture. Capturing
    first left a window where a message could materialize a NEW slot that the
    capture had already passed, so the teardown cancelled a stale handle while the
    fresh session kept running. Asserts on the ORDER of the calls."""
    src = inspect.getsource(routes._handle_delete)
    reserve = src.index("_mark_deleting(")
    for later in ("state.get_slot(", "_exec_loop_id(", "_remove_nudge_loop("):
        assert reserve < src.index(later), (
            f"{later} is captured before the delete reservation: a slot created in "
            "that window would survive the teardown"
        )


def test_message_revalidates_between_slot_acquisition_and_dispatch():
    """Source guard: _ensure_worker_slot awaits, so a delete can complete between
    the identity check and the dispatch. There must be a _touch_spec refusal AFTER
    the slot is acquired and BEFORE the turn is handed over."""
    src = inspect.getsource(routes._handle_message)
    acquire = src.index("_ensure_worker_slot(")
    dispatch = src.index("_dispatch_turn(")
    between = src[acquire:dispatch]
    assert "_touch_spec(" in between, (
        "no re-pin between slot acquisition and dispatch: a delete finishing in "
        "that window would have its turn dispatched anyway"
    )
    # Every decision-delivery await after the re-pin is recoverable: the claim writes a
    # pending outbox entry, and the delivery helper finalizes it only after relay. The
    # directory turn lock still excludes delete and other Spec Builder dispatches.
    tail = between[between.index("_touch_spec(") :]
    awaits = [
        ln.strip() for ln in tail.splitlines() if "await " in ln and not ln.strip().startswith("#")
    ]
    allowed = (
        "asyncio.to_thread(",
        "_claim_decision(",
        "_pending_decisions(",
        "_deliver_pending_decision(",
        "_final_alias_conflict(",
    )
    assert all(
        any(name in ln for name in allowed) for ln in awaits
    ), f"a non-pinning await slipped in before dispatch: {awaits!r}"


@pytest.mark.asyncio
async def test_failed_loop_removal_releases_both_tombstone_and_reservation(tmp_path, monkeypatch):
    """Moving the reservation earlier means the 503 abort now owns two things to
    undo. Leaving either behind would hide a spec the user still has."""
    client = _make_client(monkeypatch, tmp_path)
    base = tmp_path / "wd" / ".kiro" / "specs"
    (base / "keeper").mkdir(parents=True)
    sd = str(base / "keeper")
    routes._save_index(
        {
            "keeper": {
                "spec_dir": sd,
                "working_dir": str(tmp_path / "wd"),
                "spec_type": "feature",
                "status": "planning",
                "slot_key": routes._new_slot_key("keeper"),
            }
        }
    )

    async def _explode(*_a, **_k):
        raise RuntimeError("loop service down")

    monkeypatch.setattr(routes, "_remove_nudge_loop", _explode)

    await client.start_server()
    try:
        resp = await client.delete(f"{_BASE}/specs/keeper")
    finally:
        await client.close()

    assert resp.status == 503
    idx = await routes._aload_index()
    assert "keeper" in idx, "the spec was dropped despite the abort"
    assert not idx["keeper"].get(
        routes._DELETING
    ), "reservation left behind — spec hidden from the list"
    # The tombstone must be gone too, or discovery would refuse to re-adopt it.
    assert await routes._touch_spec("keeper", expect_spec_dir=sd) is not None


# ── Pause / Delete must not relaunch queued work ─────────────────────────────


# The spec name these two use. Deliberately not a name any other test creates:
# _slot_key() prefers a PERSISTED per-creation key from the module-global
# _SLOT_KEYS, so reusing a name another test created makes the key -- and the
# fake state's lookup -- miss, and the helper returns False before it ever stops.
_RELAUNCH_SPEC = "relaunch-probe"


def _relaunchable_slot(*, running: bool = True):
    """A slot carrying all three successor-turn sources, plus a cancel probe."""

    seen: dict = {"cancelled": False, "stopped": []}

    class _Task:
        def cancel(self):
            # Record the queue state AT CANCEL TIME. Clearing after the cancel is
            # useless: _run_chat's end-of-turn block runs as the task unwinds.
            seen["cancelled"] = True
            seen["queue_at_cancel"] = list(slot._queue)
            seen["steers_at_cancel"] = list(slot._pending_steers)
            seen["synthesis_at_cancel"] = slot._pending_synthesis

        def done(self):
            return False

    class _Slot:
        # Both derived from the module, never hardcoded: the ownership check
        # compares against routes.APP_NAME, and the lookup against _slot_key().
        key: str = routes._slot_key(_RELAUNCH_SPEC)
        _app: str = routes.APP_NAME
        running: bool = True
        task: object = None
        _queue: list = []
        _pending_steers: list = []
        _pending_synthesis: bool = True

    slot = _Slot()
    slot.running = running
    slot.task = _Task()
    slot._queue = [{"id": "q1", "content": "keep editing my files"}]
    slot._pending_steers = ["and also do this"]
    slot._pending_synthesis = True
    return slot, seen


def _state_with(slot, seen):
    class _Sessions:
        async def stop_turn(self, key, force=False):
            # Same reasoning as the cancel probe: a cooperative stop ends the turn
            # too, so the queue must already be empty when it lands.
            seen["stopped"].append(key)
            seen["queue_at_stop"] = list(slot._queue)
            seen["steers_at_stop"] = list(slot._pending_steers)
            seen["synthesis_at_stop"] = slot._pending_synthesis

    class _State:
        sessions = _Sessions()

        def __init__(self):
            self._slots = {slot.key: slot}

        def get_slot(self, key):
            return self._slots.get(key)

    return _State()


@pytest.mark.asyncio
async def test_pause_discards_queued_work_before_stopping():
    """Pause must not hand the agent its next prompt.

    _run_chat swallows its CancelledError and its end-of-turn block then starts
    the next queued message (requeuing unconsumed steers into the queue head
    first) or a pending synthesis. So a Pause that only stopped the turn left the
    agent editing the user's files afterwards.
    """
    slot, seen = _relaunchable_slot()
    assert await routes._halt_active_turn(_state_with(slot, seen), _RELAUNCH_SPEC) is True

    assert seen["stopped"], "cooperative stop_turn was never called"
    assert seen["cancelled"] is True, "the in-flight turn was not cancelled"
    # Empty at BOTH stops -- the cooperative one ends the turn as surely as the
    # cancel, so a clear that only preceded the cancel would still race.
    assert seen["queue_at_stop"] == [], "queued message survived into the cooperative stop"
    assert seen["steers_at_stop"] == [], "pending steer survived into the cooperative stop"
    assert seen["synthesis_at_stop"] is False, "pending synthesis survived the cooperative stop"
    assert seen["queue_at_cancel"] == [], "queued message survived into the cancel"
    assert seen["steers_at_cancel"] == [], "pending steer survived into the cancel"
    assert seen["synthesis_at_cancel"] is False, "pending synthesis survived into the cancel"


@pytest.mark.asyncio
async def test_delete_discards_queued_work_before_cancelling(monkeypatch):
    """Delete has the same exposure, and worse: the successor turn would write
    into a spec directory the request is about to archive."""
    slot, seen = _relaunchable_slot()

    async def _saved(*a, **k):
        return None

    monkeypatch.setitem(
        sys.modules,
        "kiro_crew.dashboard.chat_persistence",
        types.SimpleNamespace(save_slot_off_loop=_saved),
    )
    assert await routes._teardown_worker_slot(_state_with(slot, seen), _RELAUNCH_SPEC) is True

    assert seen["cancelled"] is True, "the in-flight turn was not cancelled"
    assert seen["queue_at_cancel"] == [], "queued message survived into the cancel"
    assert seen["steers_at_cancel"] == [], "pending steer survived into the cancel"
    assert seen["synthesis_at_cancel"] is False, "pending synthesis survived into the cancel"


def test_discard_covers_every_relaunch_source():
    """All three sources, or the ones left behind still start a successor."""
    slot, _ = _relaunchable_slot()
    routes._discard_queued_work(slot)
    assert slot._queue == []
    assert slot._pending_steers == []
    assert slot._pending_synthesis is False


def test_discard_tolerates_a_slot_without_the_attributes():
    """A foreign or half-built slot must not make teardown raise."""
    routes._discard_queued_work(types.SimpleNamespace())


def test_both_stop_helpers_discard_before_they_stop():
    """Source guard on the ORDER: the discard must precede every stop in both
    helpers. Asserts on the calls, not on a comment."""
    for fn in (routes._halt_active_turn, routes._teardown_worker_slot):
        src = inspect.getsource(fn)
        assert "_discard_queued_work(" in src, f"{fn.__name__} does not discard queued work"
        discard = src.index("_discard_queued_work(")
        for stop in ("task.cancel()", "stop_turn("):
            at = src.find(stop)
            if at == -1:
                continue
            assert discard < at, (
                f"{fn.__name__}: _discard_queued_work must precede {stop} -- clearing "
                "afterwards races _run_chat's end-of-turn block"
            )


def test_run_chat_still_relaunches_from_these_three_fields():
    """Pins the UPSTREAM behaviour this fix exists for. If the gateway ever stops
    relaunching from the end-of-turn block, this test should fail loudly so the
    discard can be re-justified rather than cargo-culted."""
    from kiro_crew.dashboard import chat_runner

    src = inspect.getsource(chat_runner)
    assert "_start_next_queued_turn" in src
    assert "_requeue_unconsumed_steers" in src
    # The swallow is the reason a cancel behaves like a clean finish here.
    assert re.search(r"except asyncio\.CancelledError:", src)


@pytest.mark.asyncio
async def test_partial_slot_teardown_keeps_the_delete_reserved(tmp_path, monkeypatch):
    """A spec cannot reappear after one captured worker was already destroyed."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    stale_slot_key = "spec-builder-s-feedbeef"
    routes._OBSERVED_SPEC_DIRS[stale_slot_key] = routes._decision_key(spec_dir)
    slots = {
        slot_key: types.SimpleNamespace(key=slot_key),
        stale_slot_key: types.SimpleNamespace(key=stale_slot_key),
    }

    class _State:
        def get_slot(self, key):
            return slots.get(key)

    teardown_results = iter((True, False))

    async def _partially_teardown(*_args, **_kwargs):
        return next(teardown_results)

    monkeypatch.setattr(routes, "_teardown_worker_slot", _partially_teardown)

    client.app["state"] = _State()
    await client.start_server()
    try:
        response = await client.delete(
            f"{_BASE}/specs/s",
            json={"spec_dir": spec_dir, "slot_key": slot_key},
        )
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 503, body
    assert body["code"] == "archive_failed"
    assert "retry" in body["error"]
    entry = routes._load_index()["s"]
    assert entry.get(routes._DELETING), "the partially deleted spec was unhidden"
    assert spec_dir in routes._load_deleted(), "the partial delete lost its tombstone"
    monkeypatch.setattr(routes, "_PROCESS_ID", "replacement-process")
    restarted = routes._load_index()["s"]
    assert restarted.get(routes._DELETING), "restart exposed the partial delete"


@pytest.mark.asyncio
async def test_legacy_delete_refuses_same_path_replacement_before_reservation(
    tmp_path, monkeypatch
):
    """A missing legacy key still has the captured name-derived creation identity.

    Replacing that row at the same name and directory after DELETE's snapshot must
    not let the stale request reserve and remove the replacement.
    """
    client = _make_client(monkeypatch, tmp_path)
    base = tmp_path / "wd" / ".kiro" / "specs"
    (base / "legacy").mkdir(parents=True)
    spec_dir = str(base / "legacy")
    routes._save_index(
        {
            "legacy": {
                "spec_dir": spec_dir,
                "working_dir": str(tmp_path / "wd"),
                "spec_type": "feature",
                "status": "planning",
            }
        }
    )
    replacement_keys: list[str] = []
    real_alias_status = routes._aload_index_with_decision_alias_status

    async def _replace_then_check_aliases(directory):
        index = await routes._aload_index()
        replacement_key = routes._new_slot_key("legacy")
        replacement_keys.append(replacement_key)
        index["legacy"]["slot_key"] = replacement_key
        routes._save_index(index)
        return await real_alias_status(directory)

    monkeypatch.setattr(
        routes, "_aload_index_with_decision_alias_status", _replace_then_check_aliases
    )

    client.app["state"] = None
    await client.start_server()
    try:
        response = await client.delete(f"{_BASE}/specs/legacy")
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 404, body
    current = routes._load_index()["legacy"]
    assert current["slot_key"] == replacement_keys[0]
    assert routes._DELETING not in current
    assert spec_dir not in routes._load_deleted()
