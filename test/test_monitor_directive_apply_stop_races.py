"""Tests for a monitor stop's write racing another change to the same legacy loop.

A wake's stop repeats its identity and retention checks inside the service transaction that
removes the row (or writes the research tombstone), under the maintenance lock
``_remove_unserialized`` requires its caller to hold. A pause that lands while that stop waits
for the lock survives it, and a row deleted meanwhile counts as stopped. A stop, from a wake or
a person, is refused when a concurrent arm replaced its row, leaving the replacement running,
and is reported as not stopped when its write never took the lock. What a wake may do to its
loop at all is tested in ``test_monitor_directive_apply_wake_authority.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.autonudge import AutoNudgeService
from kiro_crew.dashboard.session_directive_apply import apply_session_directive


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["monitor_stop", "autonudge_stop"])
async def test_stale_wake_stop_preserves_pause_landing_before_remove(tmp_path, kind, monkeypatch):
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(
        slot_key="chat-1",
        message="keep this paused loop",
        idle_secs=86400,
        max_cycles=9,
        max_runtime_secs=7200,
    )
    real_acquire = service._acquire_mutation_lock
    stop_waiting = False

    async def _pause_before_stop_acquires(loop_id, **kwargs):
        nonlocal stop_waiting
        if not stop_waiting:
            stop_waiting = True
            await service.update(loop_id, active=False)
        return await real_acquire(loop_id, **kwargs)

    monkeypatch.setattr(service, "_acquire_mutation_lock", _pause_before_stop_acquires)
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.autonudge_authz.sel", return_value=MagicMock()),
        ):
            result = await apply_session_directive(
                SimpleNamespace(),
                SimpleNamespace(key="chat-1", _app=""),
                "dashboard:chat-1",
                kind,
                {"reason": "wake finished"},
                producer_is_self_wake=True,
                producer_wake_loop_id=loop.id,
            )

        paused = service.get_by_slot("chat-1")
        assert paused is not None and paused.id == loop.id
        assert not paused.active and paused.stopped_reason == "manual"
        assert paused.message == "keep this paused loop"
        assert paused.max_cycles == 9 and paused.max_runtime_secs == 7200
        assert "new user request is required" in result
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_remove_unserialized_requires_mutation_lock(tmp_path):
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(slot_key="chat-1", message="keep", idle_secs=86400)
    try:
        with pytest.raises(RuntimeError, match="mutation lock"):
            await service._remove_unserialized(loop.id)
        assert service.get_by_id(loop.id) is loop
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_remove_unserialized_requires_lock_owned_by_caller(tmp_path):
    from kiro_crew import autonudge as an

    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(slot_key="chat-1", message="keep", idle_secs=86400)
    acquired = asyncio.Future()
    release = asyncio.Event()

    async def _hold_service_lock():
        lock = await service._acquire_mutation_lock(loop.id)
        assert lock is not None
        acquired.set_result(lock)
        try:
            # flake-ok: pre-existing rendezvous moved verbatim; the test's finally sets release
            await release.wait()
        finally:
            an._release_mutation_lock(lock)

    holder = asyncio.create_task(_hold_service_lock())
    try:
        # flake-ok: pre-existing unbounded wait for the holder's lock, moved verbatim
        lock = await acquired
        with pytest.raises(RuntimeError, match="mutation lock must be held by the caller"):
            await service._remove_unserialized(loop.id, mutation_lock=lock)
        assert service.get_by_id(loop.id) is loop
    finally:
        release.set()
        # flake-ok: pre-existing join of the holder released above, moved verbatim
        await holder
        service.stop()


@pytest.mark.asyncio
async def test_remove_unserialized_requires_service_maintenance_lock(tmp_path):
    from kiro_crew import autonudge as an

    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(slot_key="chat-1", message="keep", idle_secs=86400)
    different_lock = asyncio.Lock()
    await different_lock.acquire()
    an._claim_mutation_lock(different_lock)
    try:
        with pytest.raises(
            RuntimeError, match="mutation lock must be the service maintenance lock"
        ):
            await service._remove_unserialized(loop.id, mutation_lock=different_lock)
        assert service.get_by_id(loop.id) is loop
    finally:
        an._release_mutation_lock(different_lock)
        service.stop()


@pytest.mark.asyncio
async def test_stale_wake_stop_preserves_pause_landing_before_research_update(
    tmp_path, monkeypatch
):
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(
        slot_key="research-0123abcd",
        message="keep this paused campaign loop",
        idle_secs=86400,
        max_cycles=9,
        max_runtime_secs=7200,
    )
    real_acquire = service._acquire_mutation_lock
    stop_waiting = False

    async def _pause_before_stop_acquires(loop_id, **kwargs):
        nonlocal stop_waiting
        if not stop_waiting:
            stop_waiting = True
            await service.update(loop_id, active=False)
        return await real_acquire(loop_id, **kwargs)

    monkeypatch.setattr(service, "_acquire_mutation_lock", _pause_before_stop_acquires)
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.autonudge_authz.sel", return_value=MagicMock()),
        ):
            result = await apply_session_directive(
                SimpleNamespace(),
                SimpleNamespace(key="research-0123abcd", _app="auto-research"),
                "dashboard:research-0123abcd",
                "autonudge_stop",
                {"reason": "wake finished"},
                producer_is_self_wake=True,
                producer_wake_loop_id=loop.id,
            )

        paused = service.get_by_slot("research-0123abcd")
        assert paused is not None and paused.id == loop.id
        assert not paused.active and paused.stopped_reason == "manual"
        assert paused.max_cycles == 9 and paused.max_runtime_secs == 7200
        assert "new user request is required" in result
    finally:
        service.stop()


def _directive_outcomes(audit: MagicMock, kind: str) -> list[str]:
    return [
        call.kwargs["outcome"]
        for call in audit.log_tool_invocation.call_args_list
        if call.kwargs.get("source") == "mcp-directive" and call.kwargs.get("tool_name") == kind
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["monitor_stop", "autonudge_stop"])
async def test_stale_wake_stop_of_row_deleted_before_remove_is_idempotent_success(
    tmp_path, kind, monkeypatch
):
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(slot_key="chat-1", message="delete me first", idle_secs=86400)
    real_acquire = service._acquire_mutation_lock
    stop_waiting = False

    async def _delete_before_stop_acquires(loop_id, **kwargs):
        nonlocal stop_waiting
        if not stop_waiting:
            stop_waiting = True
            assert await service.remove(loop_id, stop_reason="dashboard_delete")
        return await real_acquire(loop_id, **kwargs)

    monkeypatch.setattr(service, "_acquire_mutation_lock", _delete_before_stop_acquires)
    audit = MagicMock()
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.autonudge_authz.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
        ):
            result = await apply_session_directive(
                SimpleNamespace(),
                SimpleNamespace(key="chat-1", _app=""),
                "dashboard:chat-1",
                kind,
                {"reason": "wake finished"},
                producer_is_self_wake=True,
                producer_wake_loop_id=loop.id,
            )

        assert service.get_by_slot("chat-1") is None
        assert f"Auto-nudge loop {loop.id} stopped on this session" in result
        assert "new user request" not in result
        assert _directive_outcomes(audit, kind) == ["success"]
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_stale_wake_stop_of_row_deleted_before_research_update_is_idempotent_success(
    tmp_path, monkeypatch
):
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(
        slot_key="research-0123abcd", message="delete me first", idle_secs=86400
    )
    real_acquire = service._acquire_mutation_lock
    stop_waiting = False

    async def _delete_before_stop_acquires(loop_id, **kwargs):
        nonlocal stop_waiting
        if not stop_waiting:
            stop_waiting = True
            assert await service.remove(loop_id, stop_reason="dashboard_delete")
        return await real_acquire(loop_id, **kwargs)

    monkeypatch.setattr(service, "_acquire_mutation_lock", _delete_before_stop_acquires)
    audit = MagicMock()
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.autonudge_authz.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
        ):
            result = await apply_session_directive(
                SimpleNamespace(),
                SimpleNamespace(key="research-0123abcd", _app="auto-research"),
                "dashboard:research-0123abcd",
                "autonudge_stop",
                {"reason": "wake finished"},
                producer_is_self_wake=True,
                producer_wake_loop_id=loop.id,
            )

        assert service.get_by_slot("research-0123abcd") is None
        assert f"Auto-nudge loop {loop.id} stopped on this session" in result
        assert "new user request" not in result
        assert _directive_outcomes(audit, "autonudge_stop") == ["success"]
    finally:
        service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("slot_key", "app", "kind"),
    [
        ("chat-1", "", "monitor_stop"),
        ("chat-1", "", "autonudge_stop"),
        ("research-0123abcd", "auto-research", "autonudge_stop"),
    ],
)
async def test_stale_wake_stop_of_row_replaced_before_write_is_refused(
    tmp_path, slot_key, app, kind, monkeypatch
):
    # A concurrent arm REPLACED the wake's loop while the stop waited for the
    # lock. The wake's row is gone, but the goal does not hold: the replacement
    # is still running, so the stop must refuse and leave it untouched.
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(slot_key=slot_key, message="replace me", idle_secs=86400)
    real_acquire = service._acquire_mutation_lock
    replacement = None
    stop_waiting = False

    async def _replace_before_stop_acquires(loop_id, **kwargs):
        nonlocal replacement, stop_waiting
        if not stop_waiting:
            stop_waiting = True
            assert await service.remove(loop_id, stop_reason="dashboard_delete")
            replacement = await service.add(
                slot_key=slot_key, message="the replacement", idle_secs=86400
            )
        return await real_acquire(loop_id, **kwargs)

    monkeypatch.setattr(service, "_acquire_mutation_lock", _replace_before_stop_acquires)
    audit = MagicMock()
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.autonudge_authz.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
        ):
            result = await apply_session_directive(
                SimpleNamespace(),
                SimpleNamespace(key=slot_key, _app=app),
                f"dashboard:{slot_key}",
                kind,
                {"reason": "wake finished"},
                producer_is_self_wake=True,
                producer_wake_loop_id=loop.id,
            )

        live = service.get_by_slot(slot_key)
        assert replacement is not None and live is not None
        assert live.id == replacement.id and live.active
        assert "Monitor NOT stopped" in result and "replaced it" in result
        assert "No further nudges will fire" not in result
        assert _directive_outcomes(audit, kind) == ["denied"]
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_stale_wake_stop_that_never_took_the_lock_is_not_reported_stopped(
    tmp_path, monkeypatch
):
    # Maintenance holds the lock, so the write reaches neither the guard nor the
    # absent-row hook. The row is still live; claiming a stop would be false.
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(slot_key="chat-1", message="still running", idle_secs=86400)

    async def _quiesced(_loop_id, **_kwargs):
        return None

    monkeypatch.setattr(service, "_acquire_mutation_lock", _quiesced)
    audit = MagicMock()
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.autonudge_authz.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
        ):
            result = await apply_session_directive(
                SimpleNamespace(),
                SimpleNamespace(key="chat-1", _app=""),
                "dashboard:chat-1",
                "monitor_stop",
                {"reason": "wake finished"},
                producer_is_self_wake=True,
                producer_wake_loop_id=loop.id,
            )

        live = service.get_by_slot("chat-1")
        assert live is not None and live.id == loop.id and live.active
        assert "Monitor NOT stopped" in result and "Retry the stop" in result
        assert _directive_outcomes(audit, "monitor_stop") == ["denied"]
    finally:
        service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("slot_key", "app", "kind"),
    [
        ("chat-1", "", "monitor_stop"),
        ("chat-1", "", "autonudge_stop"),
        ("research-0123abcd", "auto-research", "autonudge_stop"),
    ],
)
async def test_user_stop_that_never_took_the_lock_is_not_reported_stopped(
    tmp_path, slot_key, app, kind, monkeypatch
):
    # A person's own stop carries no wake id, so there is no guard. It still may
    # not report a stop when maintenance held the lock and nothing was written.
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(slot_key=slot_key, message="still running", idle_secs=86400)

    async def _quiesced(_loop_id, **_kwargs):
        return None

    monkeypatch.setattr(service, "_acquire_mutation_lock", _quiesced)
    audit = MagicMock()
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.autonudge_authz.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
        ):
            result = await apply_session_directive(
                SimpleNamespace(),
                SimpleNamespace(key=slot_key, _app=app),
                f"dashboard:{slot_key}",
                kind,
                {"reason": "done"},
            )

        live = service.get_by_slot(slot_key)
        assert live is not None and live.id == loop.id and live.active
        assert "Monitor NOT stopped" in result and "Retry the stop" in result
        assert _directive_outcomes(audit, kind) == ["denied"]
    finally:
        service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("slot_key", "app", "kind"),
    [
        ("chat-1", "", "monitor_stop"),
        ("chat-1", "", "autonudge_stop"),
        ("research-0123abcd", "auto-research", "autonudge_stop"),
    ],
)
async def test_user_stop_of_row_replaced_before_write_is_refused(
    tmp_path, slot_key, app, kind, monkeypatch
):
    # A person's stop resolved the loop, then a concurrent arm replaced it
    # before the write took the lock. The replacement is still running, so the
    # stop may not say "No further nudges will fire".
    service = AutoNudgeService(base_dir=tmp_path)
    await service.add(slot_key=slot_key, message="replace me", idle_secs=86400)
    real_acquire = service._acquire_mutation_lock
    replacement = None
    stop_waiting = False

    async def _replace_before_stop_acquires(loop_id, **kwargs):
        nonlocal replacement, stop_waiting
        if not stop_waiting:
            stop_waiting = True
            assert await service.remove(loop_id, stop_reason="dashboard_delete")
            replacement = await service.add(
                slot_key=slot_key, message="the replacement", idle_secs=86400
            )
        return await real_acquire(loop_id, **kwargs)

    monkeypatch.setattr(service, "_acquire_mutation_lock", _replace_before_stop_acquires)
    audit = MagicMock()
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.autonudge_authz.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
        ):
            result = await apply_session_directive(
                SimpleNamespace(),
                SimpleNamespace(key=slot_key, _app=app),
                f"dashboard:{slot_key}",
                kind,
                {"reason": "done"},
            )

        live = service.get_by_slot(slot_key)
        assert replacement is not None and live is not None
        assert live.id == replacement.id and live.active
        assert "Monitor NOT stopped" in result and "replaced by a new loop" in result
        assert "No further nudges will fire" not in result
        assert _directive_outcomes(audit, kind) == ["denied"]
    finally:
        service.stop()
