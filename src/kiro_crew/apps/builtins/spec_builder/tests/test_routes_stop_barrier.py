"""Route tests: stop as a barrier across generations.

A stop revokes the current execution claim and refuses new ones while it runs,
even across a rewritten name, directory or slot key; an uncommitted stop rolls
back; and pending dispatch claims and observed identities are arbitrated so a
stop reaches the turn that is actually running.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
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
    _redirect_state,
    _slot_stub,
)


class _NoConfiguredOwner:
    """The standalone-local state shape: no owner configured yet."""

    owner_id = ""


@pytest.mark.asyncio
async def test_stop_that_finishes_before_handoff_gets_the_lock_prevents_dispatch(
    tmp_path, monkeypatch
):
    """A successful Stop must revoke a handoff claim that is still waiting.

    Handoff records ``executing`` before it acquires the directory lock. Stop can
    therefore take the lock first, halt that creation, and commit ``planning``.
    Once Stop reports success, the older handoff must not arm or dispatch a run.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path, state={"phase": "tasks"})
    (Path(spec_dir) / "tasks.md").write_text("- [ ] a task\n")

    slot = types.SimpleNamespace(
        key=slot_key,
        _app=routes.APP_NAME,
        running=False,
        task=None,
        messages=[],
        project=str(tmp_path / "wd"),
        _titled=True,
    )

    class _State:
        def __init__(self):
            self._background_tasks: set[asyncio.Task] = set()

        def get_slot(self, key):
            return slot if key == slot_key else None

        def get_or_create_slot(self, _key, app=""):
            return slot

    state = _State()
    claimed = asyncio.Event()
    resume_handoff = asyncio.Event()
    real_ensure = routes._ensure_worker_slot

    async def _hold_after_claim(*args, **kwargs):
        claimed.set()
        await resume_handoff.wait()
        return await real_ensure(*args, **kwargs)

    async def _halted(*_args, **_kwargs):
        return None

    authorized: list[bool] = []
    dispatched: list[str] = []

    async def _authorized(**_kwargs):
        authorized.append(True)
        return types.SimpleNamespace(id="loop-1"), "", 200

    monkeypatch.setattr(routes, "_ensure_worker_slot", _hold_after_claim)
    monkeypatch.setattr(routes, "_halt_execution", _halted)
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: object())
    monkeypatch.setattr(routes, "authorize_and_add_nudge", _authorized)
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    handoff = None
    client.app["state"] = state
    await client.start_server()
    try:
        handoff = asyncio.create_task(
            client.post(
                f"{_BASE}/specs/s/handoff",
                json={"spec_dir": spec_dir, "slot_key": slot_key},
            )
        )
        await claimed.wait()
        assert routes._load_index()["s"]["status"] == "executing"

        stopped = await client.post(
            f"{_BASE}/specs/s/stop",
            json={"spec_dir": spec_dir, "slot_key": slot_key},
        )
        stop_payload = await stopped.json()
        assert stopped.status == 200, stop_payload
        assert routes._load_index()["s"]["status"] == "planning"

        resume_handoff.set()
        response = await handoff
        payload = await response.json()
    finally:
        resume_handoff.set()
        if handoff is not None and not handoff.done():
            handoff.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await handoff
        await client.close()

    assert response.status == 409, payload
    assert payload.get("code") == "execution_stopped_during_start", payload
    assert authorized == [], "the stopped handoff still armed an autonomous loop"
    assert dispatched == [], "the stopped handoff restarted the agent after Stop returned"


@pytest.mark.asyncio
async def test_stop_barrier_revokes_the_old_generation_and_refuses_a_new_one():
    """Agent-writable index fields cannot mint or preserve handoff ownership."""
    dir_key = "/canonical/spec"
    slot_key = "spec-builder-s-first"
    first, refusal = routes._reserve_execution_claim(dir_key, slot_key, "s")
    assert first and refusal == ""

    async with routes._execution_stop_barrier(dir_key, slot_key, "s") as capture:
        assert not routes._execution_claim_is_current(dir_key, first)
        blocked, refusal = routes._reserve_execution_claim(dir_key, slot_key, "s")
        assert blocked == "" and refusal == "stopping"
        capture.commit()

    second, refusal = routes._reserve_execution_claim(dir_key, slot_key, "s")
    assert second and second != first and refusal == ""
    async with routes._execution_stop_barrier(
        f"{dir_key}-replacement", f"{slot_key}-replacement", "replacement"
    ):
        assert routes._execution_claim_is_current(dir_key, second)
    rewritten_dir_key = f"{dir_key}-rewritten"
    async with routes._execution_stop_barrier(rewritten_dir_key, slot_key, "s") as capture:
        assert not routes._execution_claim_is_current(dir_key, second)
        blocked, refusal = routes._reserve_execution_claim(dir_key, slot_key, "s")
        assert blocked == "" and refusal == "stopping"
        capture.commit()
    assert not routes._execution_claim_is_current(dir_key, second)


@pytest.mark.asyncio
async def test_uncommitted_stop_barrier_restores_a_directory_matched_claim():
    """A stale control cannot consume the only handle to an older live loop."""
    old_dir = "/canonical/spec"
    old_slot = "spec-builder-s-1234abcd"
    token, refusal = routes._reserve_execution_claim(old_dir, old_slot, "s")
    assert token and refusal == ""

    async with routes._execution_stop_barrier(old_dir, "spec-builder-t-deadbeef", "t") as capture:
        assert old_slot in capture
        assert not routes._execution_claim_is_current(old_dir, token)
        routes._prune_finished_dispatch_claims()

    assert routes._execution_claim_is_current(old_dir, token)
    async with routes._execution_stop_barrier(old_dir, old_slot, "s") as capture:
        capture.commit()


@pytest.mark.asyncio
async def test_uncommitted_stop_barrier_restores_a_directory_matched_pending_turn():
    """A refused teardown cannot erase an ordinary turn's ownership claim."""
    old_dir = "/canonical/spec"
    old_slot = "spec-builder-s-1234abcd"
    token = routes._reserve_pending_dispatch(old_dir, old_slot, "s")
    assert token

    async with routes._execution_stop_barrier(old_dir, "spec-builder-t-deadbeef", "t"):
        assert not routes._pending_dispatch_is_current(token)
        assert token in routes._PENDING_DISPATCH_CLAIMS

    assert routes._pending_dispatch_is_current(token)
    routes._drop_pending_dispatch(token)


@pytest.mark.asyncio
async def test_stop_rollback_prunes_a_published_pending_turn_that_finished_revoked():
    """A completion callback hidden by provisional revocation is reconciled."""
    old_dir = "/canonical/spec"
    old_slot = "spec-builder-s-1234abcd"
    release = asyncio.Event()
    turn = asyncio.create_task(release.wait())
    slot = types.SimpleNamespace(task=turn, running=True)
    token = routes._reserve_pending_dispatch(old_dir, old_slot, "s")
    assert token
    routes._bind_pending_dispatch_to_turn(token, slot, turn)

    async with routes._execution_stop_barrier(old_dir, "spec-builder-t-deadbeef", "t"):
        slot.running = False
        release.set()
        await turn
        await asyncio.sleep(0)
        assert token in routes._PENDING_DISPATCH_CLAIMS

    assert token not in routes._PENDING_DISPATCH_CLAIMS
    replacement = routes._reserve_pending_dispatch(old_dir, old_slot, "s")
    assert replacement
    routes._drop_pending_dispatch(replacement)


@pytest.mark.asyncio
async def test_stop_rollback_rebinds_a_pending_claim_to_its_live_successor():
    """Rollback preserves automatic retry ownership while the retry is active."""
    old_dir = "/canonical/spec"
    old_slot = "spec-builder-s-1234abcd"
    first_release = asyncio.Event()
    successor_release = asyncio.Event()
    first = asyncio.create_task(first_release.wait())
    successor = asyncio.create_task(successor_release.wait())
    slot = types.SimpleNamespace(task=first, running=True)
    token = routes._reserve_pending_dispatch(old_dir, old_slot, "s")
    routes._bind_pending_dispatch_to_turn(token, slot, first)

    async with routes._execution_stop_barrier(old_dir, "spec-builder-t-deadbeef", "t"):
        slot.task = successor
        first_release.set()
        await first
        await asyncio.sleep(0)

    assert routes._PENDING_DISPATCH_CLAIMS[token][3] is successor
    successor_release.set()
    await successor
    await asyncio.sleep(0)
    assert token not in routes._PENDING_DISPATCH_CLAIMS


@pytest.mark.asyncio
async def test_rolled_back_stop_settles_a_handoff_that_observed_revocation(tmp_path, monkeypatch):
    """A failed teardown cannot leave an aborted handoff durably executing."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = str(tmp_path / "spec")
    slot_key = "spec-builder-s-1234abcd"
    routes._save_index(
        {
            "s": {
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "status": "executing",
            }
        }
    )
    reserved = asyncio.Event()
    token_box: list[str] = []

    async def _handoff_owner():
        token, refusal = routes._reserve_execution_claim(spec_dir, slot_key, "s")
        assert token and refusal == ""
        token_box.append(token)
        reserved.set()
        while routes._execution_claim_is_current(spec_dir, token):
            await asyncio.sleep(0)

    owner = asyncio.create_task(_handoff_owner())
    await reserved.wait()
    async with routes._execution_stop_barrier(spec_dir, "spec-builder-t-deadbeef", "t"):
        await owner

    await asyncio.gather(*tuple(routes._STOP_ROLLBACK_TASKS))
    assert routes._load_index()["s"]["status"] == "planning"
    assert not routes._execution_claim_is_current(spec_dir, token_box[0])


def test_index_refresh_replaces_observed_identity_maps(tmp_path, monkeypatch):
    """A worker refresh cannot resize a snapshot being read on the event loop."""
    _redirect_state(monkeypatch, tmp_path)
    first_slot = "spec-builder-s-1234abcd"
    second_slot = "spec-builder-t-deadbeef"
    first_dir = str(tmp_path / "first")
    second_dir = str(tmp_path / "second")
    routes._save_index({"s": {"spec_dir": first_dir, "slot_key": first_slot}})
    slot_keys_snapshot = routes._OBSERVED_SLOT_KEYS
    spec_dirs_snapshot = routes._OBSERVED_SPEC_DIRS

    routes._save_index(
        {
            "s": {"spec_dir": first_dir, "slot_key": first_slot},
            "t": {"spec_dir": second_dir, "slot_key": second_slot},
        }
    )

    assert routes._OBSERVED_SLOT_KEYS is not slot_keys_snapshot
    assert routes._OBSERVED_SPEC_DIRS is not spec_dirs_snapshot
    assert slot_keys_snapshot == {"s": first_slot}
    assert spec_dirs_snapshot == {first_slot: routes._decision_key(first_dir)}


def test_identity_release_replaces_observed_identity_maps(tmp_path, monkeypatch):
    """Worker teardown cannot resize a snapshot being read on the event loop."""
    _redirect_state(monkeypatch, tmp_path)
    first_slot = "spec-builder-s-1234abcd"
    second_slot = "spec-builder-t-deadbeef"
    routes._OBSERVED_SLOT_KEYS.update({"s": first_slot, "t": second_slot})
    routes._OBSERVED_SPEC_DIRS.update({first_slot: "/first", second_slot: "/second"})
    slot_keys_snapshot = routes._OBSERVED_SLOT_KEYS
    spec_dirs_snapshot = routes._OBSERVED_SPEC_DIRS

    routes._forget_observed_slot_identity("s", first_slot)

    assert routes._OBSERVED_SLOT_KEYS is not slot_keys_snapshot
    assert routes._OBSERVED_SPEC_DIRS is not spec_dirs_snapshot
    assert slot_keys_snapshot == {"s": first_slot, "t": second_slot}
    assert spec_dirs_snapshot == {first_slot: "/first", second_slot: "/second"}
    assert routes._OBSERVED_SLOT_KEYS == {"t": second_slot}
    assert routes._OBSERVED_SPEC_DIRS == {second_slot: "/second"}


@pytest.mark.asyncio
async def test_restored_execution_loop_blocks_dispatch_after_identity_rewrite(
    tmp_path, monkeypatch
):
    """A restart-persistent loop remains exclusive without process claims."""
    _redirect_state(monkeypatch, tmp_path)
    old_dir = str(tmp_path / "old-spec")
    new_dir = str(tmp_path / "new-spec")
    old_slot = "spec-builder-s-1234abcd"
    new_slot = "spec-builder-t-deadbeef"
    loop = types.SimpleNamespace(
        id="restored-loop",
        active=True,
        slot_key=old_slot,
        message=f"{routes._EXECUTION_HANDOFF_PREFIX}s'. The plan is approved.",
        stop_sentinel_path=str(Path(old_dir) / "STOP"),
    )

    class _Svc:
        def list_all(self):
            return [loop]

        def get_by_slot(self, key):
            return loop if key == old_slot else None

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())

    routes._save_index({"t": {"spec_dir": old_dir, "slot_key": new_slot}})
    assert routes._reserve_pending_dispatch(old_dir, new_slot, "t") == ""
    assert routes._reserve_execution_claim(old_dir, new_slot, "t") == (
        "",
        "taken",
    )

    routes._save_index({"t": {"spec_dir": new_dir, "slot_key": new_slot}})
    assert routes._reserve_pending_dispatch(new_dir, new_slot, "t") == ""
    async with routes._execution_stop_barrier(new_dir, new_slot, "t") as capture:
        assert capture[old_slot] == loop.id


@pytest.mark.asyncio
async def test_restored_execution_loop_keeps_rewritten_same_directory_executing(
    tmp_path, monkeypatch
):
    """Detail keeps Pause visible for a restored loop under its old slot key."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = str(tmp_path / "spec")
    old_slot = "spec-builder-s-1234abcd"
    new_slot = "spec-builder-t-deadbeef"
    loop = types.SimpleNamespace(
        id="restored-loop",
        active=True,
        slot_key=old_slot,
        message=f"{routes._EXECUTION_HANDOFF_PREFIX}s'. Continue.",
        stop_sentinel_path=str(Path(spec_dir) / "STOP"),
    )

    class _Svc:
        def list_all(self):
            return [loop]

        def get_by_slot(self, key):
            return loop if key == old_slot else None

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())
    meta = {"spec_dir": spec_dir, "slot_key": new_slot, "status": "executing"}
    routes._save_index({"t": meta})

    status = await routes._effective_status("t", meta, types.SimpleNamespace(running=False))

    assert status == "executing"
    assert routes._load_index()["t"]["status"] == "executing"


@pytest.mark.asyncio
async def test_stop_barrier_captures_unindexed_observed_turn_after_full_rewrite(
    tmp_path, monkeypatch
):
    """Changing name, directory, and slot cannot detach an embedded-chat turn."""
    _redirect_state(monkeypatch, tmp_path)
    old_dir = str(tmp_path / "old-spec")
    old_slot = "spec-builder-s-1234abcd"
    routes._save_index({"s": {"spec_dir": old_dir, "slot_key": old_slot}})
    routes._load_index()
    routes._save_index(
        {
            "t": {
                "spec_dir": str(tmp_path / "new-spec"),
                "slot_key": "spec-builder-t-deadbeef",
            }
        }
    )

    async with routes._execution_stop_barrier(
        str(tmp_path / "new-spec"), "spec-builder-t-deadbeef", "t"
    ) as capture:
        assert old_slot in capture


@pytest.mark.asyncio
async def test_unrelated_teardown_does_not_capture_a_same_name_slot_rewrite(tmp_path, monkeypatch):
    """A surviving name remains the control endpoint for its observed worker."""
    _redirect_state(monkeypatch, tmp_path)
    old_slot = "spec-builder-s-1234abcd"
    rewritten_slot = "spec-builder-s-deadbeef"
    unrelated_slot = "spec-builder-t-feedface"
    old_dir = str(tmp_path / "old-spec")
    routes._save_index({"s": {"spec_dir": old_dir, "slot_key": old_slot}})
    routes._save_index(
        {
            "s": {"spec_dir": old_dir, "slot_key": rewritten_slot},
            "t": {
                "spec_dir": str(tmp_path / "unrelated"),
                "slot_key": unrelated_slot,
            },
        }
    )

    assert old_slot not in routes._unindexed_observed_slot_keys()
    async with routes._execution_stop_barrier(
        str(tmp_path / "unrelated"), unrelated_slot, "t"
    ) as capture:
        assert old_slot not in capture


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_slot", [None, "not-owned"])
async def test_unrelated_teardown_does_not_capture_a_surviving_name_with_bad_slot(
    tmp_path, monkeypatch, raw_slot
):
    """A missing or invalid raw key does not erase the old control endpoint."""
    _redirect_state(monkeypatch, tmp_path)
    old_slot = "spec-builder-s-1234abcd"
    old_dir = str(tmp_path / "old-spec")
    loop = types.SimpleNamespace(
        id="loop-s",
        active=True,
        slot_key=old_slot,
        message=f"{routes._EXECUTION_HANDOFF_PREFIX}s'. Continue.",
        stop_sentinel_path=str(Path(old_dir) / "STOP"),
    )

    class _Svc:
        def list_all(self):
            return [loop]

        def get_by_slot(self, key):
            return loop if key == old_slot else None

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())
    routes._save_index({"s": {"spec_dir": old_dir, "slot_key": old_slot}})
    routes._save_index(
        {
            "s": {"spec_dir": old_dir, "slot_key": raw_slot},
            "t": {
                "spec_dir": str(tmp_path / "unrelated"),
                "slot_key": "spec-builder-t-feedface",
            },
        }
    )

    assert old_slot not in routes._unindexed_observed_slot_keys()
    assert old_slot not in routes._matching_execution_loops("", "", set(), include_orphans=True)


def test_targeted_cleanup_finds_an_inactive_loop_after_a_valid_slot_rewrite(tmp_path, monkeypatch):
    """A rewritten control endpoint still reaches its retained recovery marker."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = str(tmp_path / "spec")
    old_slot = "spec-builder-s-1234abcd"
    new_slot = "spec-builder-s-deadbeef"
    loop = types.SimpleNamespace(
        id="loop-s",
        active=False,
        slot_key=old_slot,
        message=f"{routes._EXECUTION_HANDOFF_PREFIX}s'. Continue.",
        stop_sentinel_path=str(Path(spec_dir) / "STOP"),
    )

    class _Svc:
        def list_all(self):
            return [loop]

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())
    routes._save_index({"s": {"spec_dir": spec_dir, "slot_key": new_slot}})

    assert routes._matching_execution_loops("", "", set(), include_orphans=True) == {}
    assert routes._matching_execution_loops(
        "s",
        routes._decision_key(spec_dir),
        {new_slot},
        include_orphans=True,
        include_inactive_direct=True,
    ) == {old_slot: loop.id}


def test_inactive_indexed_loop_does_not_block_a_new_dispatch(tmp_path, monkeypatch):
    """A bounded run's recovery record stays teardown-only after planning resumes."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = str(tmp_path / "spec")
    slot_key = "spec-builder-s-1234abcd"
    loop = types.SimpleNamespace(
        id="loop-s",
        active=False,
        slot_key=slot_key,
        message=f"{routes._EXECUTION_HANDOFF_PREFIX}s'. Continue.",
        stop_sentinel_path=str(Path(spec_dir) / "STOP"),
    )

    class _Svc:
        def list_all(self):
            return [loop]

        def get_by_slot(self, key):
            return loop if key == slot_key else None

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())
    routes._save_index({"s": {"spec_dir": spec_dir, "slot_key": slot_key}})

    assert not routes._dispatch_claim_conflicts(spec_dir, slot_key, "s")
    assert routes._matching_execution_loops(
        "s",
        routes._decision_key(spec_dir),
        {slot_key},
        include_inactive_direct=True,
    ) == {slot_key: loop.id}


def test_delete_releases_every_captured_observed_creation_identity(tmp_path, monkeypatch):
    """Successful teardown clears witnesses whose old name also disappeared."""
    _redirect_state(monkeypatch, tmp_path)
    old_slot = "spec-builder-s-1234abcd"
    new_slot = "spec-builder-t-deadbeef"
    routes._OBSERVED_SLOT_KEYS.update({"s": old_slot, "t": new_slot})
    routes._SLOT_KEYS.update({"s": old_slot, "t": new_slot})
    routes._OBSERVED_SPEC_DIRS.update({old_slot: "/old", new_slot: "/new"})

    routes._forget_observed_slot_identity("t", new_slot, old_slot)

    assert "s" not in routes._OBSERVED_SLOT_KEYS
    assert "s" not in routes._SLOT_KEYS
    assert "t" not in routes._OBSERVED_SLOT_KEYS
    assert "t" not in routes._SLOT_KEYS
    assert old_slot not in routes._OBSERVED_SPEC_DIRS
    assert new_slot not in routes._OBSERVED_SPEC_DIRS


@pytest.mark.asyncio
async def test_dispatch_claims_arbitrate_across_rewritten_identity():
    """A rewritten index cannot start a second generation during final validation."""
    old_dir_key = "/canonical/spec"
    rewritten_dir_key = "/rewritten/spec"
    old_slot_key = "spec-builder-s-first"
    rewritten_slot_key = "spec-builder-s-second"

    first = routes._reserve_pending_dispatch(old_dir_key, old_slot_key, "s")
    assert first
    assert routes._reserve_pending_dispatch(rewritten_dir_key, rewritten_slot_key, "s") == ""
    blocked, refusal = routes._reserve_execution_claim(rewritten_dir_key, rewritten_slot_key, "s")
    assert blocked == "" and refusal == "taken"

    routes._drop_pending_dispatch(first)
    execution, refusal = routes._reserve_execution_claim(rewritten_dir_key, rewritten_slot_key, "s")
    assert execution and refusal == ""
    assert routes._reserve_pending_dispatch(old_dir_key, old_slot_key, "s") == ""


@pytest.mark.asyncio
async def test_finished_pending_claim_owner_does_not_block_a_later_request():
    """Request teardown is an ownership boundary even before its callback runs."""

    async def _reserve() -> str:
        return routes._reserve_pending_dispatch("/canonical/spec", "slot", "s")

    owner = asyncio.create_task(_reserve())
    stale = await owner
    assert stale
    assert owner.done()

    current = routes._reserve_pending_dispatch("/rewritten/spec", "replacement", "s")
    assert current and current != stale
    assert not routes._pending_dispatch_is_current(stale)


@pytest.mark.asyncio
async def test_published_claim_keeps_same_slot_message_queuing():
    """A matching live turn is not a rewritten second generation."""
    dir_key = "/canonical/spec"
    slot_key = "spec-builder-s-first"
    turn = asyncio.create_task(asyncio.sleep(60))
    slot = types.SimpleNamespace(task=turn)
    first = routes._reserve_pending_dispatch(dir_key, slot_key, "s")
    routes._bind_pending_dispatch_to_turn(first, slot, turn)
    try:
        queued = routes._reserve_pending_dispatch(dir_key, slot_key, "s")
        assert queued
        routes._drop_pending_dispatch(queued)
        assert (
            routes._reserve_pending_dispatch("/rewritten/spec", "spec-builder-s-second", "s") == ""
        )
    finally:
        routes._drop_pending_dispatch(first)
        turn.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await turn


@pytest.mark.asyncio
async def test_stale_stop_does_not_revoke_the_current_creation_claim(tmp_path, monkeypatch):
    """A rejected control from an old tab cannot cancel replacement startup."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path, state={"phase": "tasks"})
    dir_key = routes._decision_key(spec_dir)
    token, refusal = routes._reserve_execution_claim(dir_key, slot_key, "s")
    assert token and refusal == ""

    halted: list[str] = []

    async def _halt(*_args, **_kwargs):
        halted.append("halted")

    monkeypatch.setattr(routes, "_halt_execution", _halt)
    client.app["state"] = _NoConfiguredOwner()
    await client.start_server()
    try:
        response = await client.post(
            f"{_BASE}/specs/s/stop",
            json={"spec_dir": spec_dir, "slot_key": f"{slot_key}-stale"},
        )
        payload = await response.json()
    finally:
        await client.close()

    assert response.status == 409, payload
    assert payload.get("code") == "stale_client"
    assert routes._execution_claim_is_current(dir_key, token)
    assert halted == []


def test_handoff_serializes_its_durable_claim_before_slot_creation():
    """Stop must not finish before an older handoff's delayed claim write."""
    src = inspect.getsource(routes._handle_handoff)
    reserve_at = src.index("_reserve_execution_claim(")
    claim_at = src.index("await _claim_execution(", reserve_at)
    slot_at = src.index("_ensure_worker_slot(", claim_at)
    lock_at = src.index("async with _turn_lock(", reserve_at, slot_at)
    token_check_at = src.index("_execution_claim_is_current(", lock_at, claim_at)

    assert reserve_at < lock_at < token_check_at < claim_at < slot_at


@pytest.mark.asyncio
async def test_stop_revokes_a_message_after_its_final_scan_captured_old_identity(
    tmp_path, monkeypatch
):
    """Stop on a rewritten path cannot be followed by the stale turn publication."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path, state={"phase": "tasks"})
    state, _slot = _slot_stub()
    final_scan_waiting = asyncio.Event()
    release_final_scan = asyncio.Event()
    scans = 0

    async def _scan(*_args, **_kwargs):
        nonlocal scans
        scans += 1
        if scans == 2:
            final_scan_waiting.set()
            await release_final_scan.wait()
        return {}

    monkeypatch.setattr(routes, "_alias_slots", _scan)
    dispatched: list[str] = []
    monkeypatch.setattr(
        routes,
        "_dispatch_turn",
        lambda *_args, **_kwargs: dispatched.append("sent"),
    )

    client.app["state"] = state
    await client.start_server()
    try:
        request = asyncio.create_task(
            client.post(
                f"{_BASE}/specs/s/message",
                json={"spec_dir": spec_dir, "slot_key": slot_key, "text": "continue"},
            )
        )
        await asyncio.wait_for(final_scan_waiting.wait(), timeout=1)
        rewritten_dir = str(tmp_path / "wd" / ".kiro" / "specs" / "rewritten")
        replacement_slot = "spec-builder-s-deadbeef"
        routes._save_index(
            {
                "s": {
                    "spec_dir": rewritten_dir,
                    "working_dir": str(tmp_path / "wd"),
                    "spec_type": "feature",
                    "status": "planning",
                    "slot_key": replacement_slot,
                }
            }
        )
        async with routes._execution_stop_barrier(
            routes._decision_key(rewritten_dir), replacement_slot, "s"
        ) as capture:
            capture.commit()
        release_final_scan.set()
        response = await request
        payload = await response.json()
    finally:
        release_final_scan.set()
        await client.close()

    assert response.status == 409, payload
    assert payload.get("code") == "execution_stopped_during_start"
    assert dispatched == []


@pytest.mark.asyncio
async def test_stop_targets_the_published_slot_after_index_identity_rewrite(tmp_path, monkeypatch):
    """A published turn remains stoppable under the identity it actually uses."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, old_slot_key = _decision_spec(tmp_path, state={"phase": "tasks"})
    replacement_slot_key = "spec-builder-s-deadbeef"
    old_turn = asyncio.create_task(asyncio.sleep(60))
    old_slot = types.SimpleNamespace(
        key=old_slot_key,
        _app=routes.APP_NAME,
        running=True,
        task=old_turn,
    )

    class _State:
        def get_slot(self, key):
            return old_slot if key == old_slot_key else None

    token = routes._reserve_pending_dispatch(routes._decision_key(spec_dir), old_slot_key, "s")
    routes._bind_pending_dispatch_to_turn(token, old_slot, old_turn)
    rewritten = routes._load_index()
    rewritten["s"]["slot_key"] = replacement_slot_key
    routes._save_index(rewritten)
    halted: list[object] = []

    async def _halted(*_args, **kwargs):
        halted.append(kwargs.get("only_slot"))

    monkeypatch.setattr(routes, "_halt_execution", _halted)
    client.app["state"] = _State()
    await client.start_server()
    try:
        response = await client.post(
            f"{_BASE}/specs/s/stop",
            json={"spec_dir": spec_dir, "slot_key": old_slot_key},
        )
        payload = await response.json()
    finally:
        old_turn.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await old_turn
        await client.close()

    assert response.status == 200, payload
    assert halted == [old_slot]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["stop", "delete"])
@pytest.mark.parametrize("with_claim", [True, False])
@pytest.mark.parametrize("rewrite_directory", [True, False])
async def test_teardown_targets_an_idle_published_autonudge_after_identity_rewrite(
    tmp_path, monkeypatch, operation, with_claim, rewrite_directory
):
    """Stop and Delete find the old loop both before and after a gateway restart."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, old_slot_key = _decision_spec(tmp_path, state={"phase": "tasks"})
    replacement_slot_key = "spec-builder-t-deadbeef"
    old_turn = asyncio.create_task(asyncio.sleep(0))
    old_slot = types.SimpleNamespace(
        key=old_slot_key,
        _app=routes.APP_NAME,
        running=False,
        task=old_turn,
    )
    loop = types.SimpleNamespace(
        id="loop-old-identity",
        active=not with_claim,
        slot_key=old_slot_key,
        stop_sentinel_path=Path(spec_dir) / "STOP",
    )
    removed: list[str] = []
    reasons: list[str] = []

    class _Svc:
        def list_all(self):
            return [loop] if loop.active else []

        def get_by_slot(self, key):
            return loop if key == old_slot_key and loop.active else None

        async def remove(self, loop_id, *, stop_reason=""):
            removed.append(loop_id)
            reasons.append(stop_reason)
            loop.active = False

    class _State:
        def get_slot(self, key):
            return old_slot if key == old_slot_key else None

    async def _archived(*_args, **_kwargs):
        return True

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())
    monkeypatch.setattr(routes, "_teardown_worker_slot", _archived)
    if with_claim:
        claim, refusal = routes._reserve_execution_claim(
            routes._decision_key(spec_dir), old_slot_key, "s"
        )
        assert claim and refusal == ""
        loop.active = True
        routes._bind_execution_claim_to_turn(
            routes._decision_key(spec_dir), claim, old_slot, old_turn
        )
    await old_turn
    await asyncio.sleep(0)

    rewritten = routes._load_index().pop("s")
    rewritten_dir = str(tmp_path / "rewritten-spec") if rewrite_directory else spec_dir
    rewritten.update(
        spec_dir=rewritten_dir,
        slot_key=replacement_slot_key,
        status="executing",
    )
    routes._save_index({"t": rewritten})

    client.app["state"] = _State()
    await client.start_server()
    try:
        body = {
            "spec_dir": rewritten_dir,
            "slot_key": replacement_slot_key,
        }
        if operation == "stop":
            response = await client.post(f"{_BASE}/specs/t/stop", json=body)
        else:
            response = await client.delete(f"{_BASE}/specs/t", json=body)
        payload = await response.json()
    finally:
        await client.close()

    assert response.status == 200, payload
    assert removed == [loop.id]
    assert reasons == ["spec_stopped" if operation == "stop" else "spec_deleted"]
    assert loop.active is False
    if operation == "delete":
        assert old_slot_key not in routes._OBSERVED_SPEC_DIRS
        assert old_slot_key not in routes._OBSERVED_SLOT_KEYS.values()


@pytest.mark.asyncio
async def test_stop_arriving_during_authorization_unwinds_before_dispatch(tmp_path, monkeypatch):
    """The post-authorization gate observes Stop before Stop acquires the lock."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path, state={"phase": "tasks"})
    (Path(spec_dir) / "tasks.md").write_text("- [ ] a task\n")
    slot = types.SimpleNamespace(
        key=slot_key,
        _app=routes.APP_NAME,
        running=False,
        task=None,
        messages=[],
        project=str(tmp_path / "wd"),
        _titled=True,
    )

    class _State:
        def __init__(self):
            self._background_tasks: set[asyncio.Task] = set()

        def get_slot(self, key):
            return slot if key == slot_key else None

        def get_or_create_slot(self, _key, app=""):
            return slot

    loop = types.SimpleNamespace(id="loop-authorization", active=False)
    removed: list[str] = []

    class _Svc:
        def get_by_slot(self, _key):
            return loop if loop.active else None

        async def remove(self, loop_id, *, stop_reason=""):
            removed.append(loop_id)
            loop.active = False

    authorizing = asyncio.Event()
    release_authorization = asyncio.Event()
    stop_published = asyncio.Event()

    async def _authorized(**_kwargs):
        authorizing.set()
        await release_authorization.wait()
        loop.active = True
        return loop, "", 200

    async def _halted(*_args, **_kwargs):
        return None

    real_barrier = routes._execution_stop_barrier

    @contextlib.asynccontextmanager
    async def _observed_barrier(dir_key, barrier_slot_key, barrier_name):
        async with real_barrier(dir_key, barrier_slot_key, barrier_name) as claimed_slot_keys:
            stop_published.set()
            yield claimed_slot_keys

    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())
    monkeypatch.setattr(routes, "authorize_and_add_nudge", _authorized)
    monkeypatch.setattr(routes, "_halt_execution", _halted)
    monkeypatch.setattr(routes, "_execution_stop_barrier", _observed_barrier)
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    handoff = stop = None
    client.app["state"] = _State()
    await client.start_server()
    try:
        handoff = asyncio.create_task(
            client.post(
                f"{_BASE}/specs/s/handoff",
                json={"spec_dir": spec_dir, "slot_key": slot_key},
            )
        )
        await authorizing.wait()
        stop = asyncio.create_task(
            client.post(
                f"{_BASE}/specs/s/stop",
                json={"spec_dir": spec_dir, "slot_key": slot_key},
            )
        )
        await stop_published.wait()
        release_authorization.set()

        handoff_response = await handoff
        handoff_payload = await handoff_response.json()
        stop_response = await stop
        stop_payload = await stop_response.json()
    finally:
        release_authorization.set()
        for task in (handoff, stop):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await client.close()

    assert handoff_response.status == 409, handoff_payload
    assert handoff_payload.get("code") == "execution_stopped_during_start"
    assert stop_response.status == 200, stop_payload
    assert removed == [loop.id], "the handoff's armed loop survived its revoked claim"
    assert dispatched == [], "handoff dispatched after Stop published its barrier"
