"""Persistent VM retention preserves remote identity and session serialization."""

from __future__ import annotations

import asyncio
import gc
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.boxd_session.launch import BoxdSessionEnvironments
from kiro_crew.coder import persistent_vm, retention
from kiro_crew.coder.persistent_vm import SessionResource, stamp
from kiro_crew.session_environment import SessionEnvironmentUnavailable

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
KEY = "dashboard:retention"
NAME = "kc-0123456789abcdef"


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setattr(persistent_vm, "now_utc", lambda: NOW)
    cli = SimpleNamespace(
        get=AsyncMock(return_value={"id": "owned-id"}),
        remove=AsyncMock(),
        set_delete_after=AsyncMock(),
        hibernate=AsyncMock(),
    )
    config = SimpleNamespace(boxd_profiles=[{"id": "box"}], archived_retention_days=0)
    env = BoxdSessionEnvironments(config, tmp_path, cli=cli)
    env.registry.put(SessionResource(KEY, "box", NAME, "owned-id"))
    return env, cli


@pytest.mark.asyncio
async def test_sweep_retains_lifecycle_while_delete_is_pending(environment, monkeypatch):
    env, cli = environment
    env.registry.set_deletion(KEY, stamp(NOW))
    deleting, finish = asyncio.Event(), asyncio.Event()

    async def remove(name):
        deleting.set()
        await asyncio.wait_for(finish.wait(), 30)

    cli.remove.side_effect = remove
    sweep = asyncio.create_task(env.sweep_due(now=NOW))
    try:
        await asyncio.wait_for(deleting.wait(), 30)
        gc.collect()
        lifecycle = retention.session_lifecycle(KEY)
        assert lifecycle.lock.locked(), "the sweep must own the same lock a reopen obtains"
    finally:
        finish.set()
        await asyncio.wait_for(sweep, 30)
    assert sweep.result() == 1
    cli.remove.assert_awaited_once_with(NAME)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["archive_now", "archive_later", "sweep"])
async def test_replacement_is_never_deleted_or_given_a_deletion_timer(environment, operation):
    env, cli = environment
    cli.get.return_value = {"id": "replacement-id"}
    if operation == "archive_now":
        with pytest.raises(SessionEnvironmentUnavailable, match="another machine"):
            await env.archive_session(KEY)
    elif operation == "archive_later":
        env._config.archived_retention_days = 7
        assert await env.archive_session(KEY)
    else:
        env.registry.set_deletion(KEY, stamp(NOW))
        assert await env.sweep_due(now=NOW) == 0
    cli.get.assert_awaited()
    cli.remove.assert_not_awaited()
    cli.set_delete_after.assert_not_awaited()
    cli.hibernate.assert_not_awaited()
    assert env.registry.get(KEY).resource_id == "owned-id"


@pytest.mark.asyncio
async def test_zero_retention_arms_backstop_and_retries_failed_delete(environment):
    env, cli = environment
    cli.remove.side_effect = SessionEnvironmentUnavailable("offline")
    with pytest.raises(SessionEnvironmentUnavailable, match="offline"):
        await env.archive_session(KEY)
    assert env.registry.get(KEY).deletion_due_at == stamp(NOW)
    cli.set_delete_after.assert_awaited_once_with(NAME, "0d")
    cli.hibernate.assert_awaited_once_with(NAME)
    cli.remove.side_effect = None
    assert await env.sweep_due(now=NOW) == 1
    assert cli.remove.await_count == 2
    assert env.registry.get(KEY) is None


@pytest.mark.asyncio
async def test_a_failed_backstop_does_not_skip_immediate_deletion(environment):
    env, cli = environment
    cli.set_delete_after.side_effect = SessionEnvironmentUnavailable("timer unavailable")
    assert await env.archive_session(KEY)
    cli.remove.assert_awaited_once_with(NAME)
    assert env.registry.get(KEY) is None


@pytest.mark.asyncio
async def test_missing_machine_is_forgotten_without_a_destructive_call(environment):
    env, cli = environment
    cli.get.return_value = None
    assert await env.archive_session(KEY)
    cli.remove.assert_not_awaited()
    cli.set_delete_after.assert_not_awaited()
    assert env.registry.get(KEY) is None


@pytest.mark.asyncio
async def test_identity_is_rechecked_between_arming_and_deleting(environment):
    env, cli = environment
    cli.get.side_effect = [{"id": "owned-id"}, {"id": "replacement-id"}]
    with pytest.raises(SessionEnvironmentUnavailable, match="another machine"):
        await env.archive_session(KEY)
    cli.set_delete_after.assert_awaited_once_with(NAME, "0d")
    cli.remove.assert_not_awaited()
    assert env.registry.get(KEY).deletion_due_at == stamp(NOW)


@pytest.mark.asyncio
async def test_owned_machine_is_retained_until_its_deadline(environment):
    env, cli = environment
    env._config.archived_retention_days = 7
    assert await env.archive_session(KEY)
    cli.set_delete_after.assert_awaited_once_with(NAME, "7d")
    assert await env.sweep_due(now=NOW) == 0
    assert await env.sweep_due(now=NOW + timedelta(days=7)) == 1
    cli.remove.assert_awaited_once_with(NAME)
    assert env.registry.get(KEY) is None


@pytest.mark.asyncio
async def test_create_cleanup_reads_the_live_machine_before_deleting(environment, monkeypatch):
    env, cli = environment
    env.registry.remove(KEY, NAME)
    monkeypatch.setattr(env, "mint_name", lambda: NAME)
    monkeypatch.setattr(
        env, "remote_create", AsyncMock(side_effect=SessionEnvironmentUnavailable("create failed"))
    )
    cli.get.side_effect = SessionEnvironmentUnavailable("identity unavailable")
    with pytest.raises(SessionEnvironmentUnavailable, match="create failed"):
        await env._create(KEY, env.profile("box"))
    cli.get.assert_awaited_once_with(NAME)
    cli.remove.assert_not_awaited()
    assert env.registry.get(KEY).name == NAME
