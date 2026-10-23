"""Owner lifecycle ordering and isolated staging for concurrent guest writes."""

from __future__ import annotations

import asyncio
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from weakref import WeakValueDictionary

import pytest
from test_coder_retention import KEY, _envs, _started

from kiro_crew.coder import retention
from kiro_crew.remote_session import workspace_tools as wt
from kiro_crew.testing.wait import await_until, until_parked


@pytest.fixture(autouse=True)
def _developer_flag_on(_floor_monkeypatch):
    # Archive retention runs only with the developer flag.
    _floor_monkeypatch.setenv("KIROCREW_DEV_MODE", "1")


@pytest.fixture
def lifecycle_state(monkeypatch):
    monkeypatch.setattr(retention, "_LIFECYCLES", WeakValueDictionary())
    monkeypatch.setattr(retention, "_TASKS", set())


async def _finish(tasks, release):
    release.set()
    await await_until(asyncio.gather(*tasks, return_exceptions=True))


@pytest.mark.asyncio
@pytest.mark.parametrize("pause", ["environment", "unsaved"])
async def test_resume_supersedes_a_delayed_archive(tmp_path, monkeypatch, lifecycle_state, pause):
    fake, envs, row = await _started(tmp_path)
    entered, release, reopening = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def environments():
        if pause == "environment":
            entered.set()
            await await_until(release)
        return envs

    async def warn(keys, notify, **_):
        if pause == "unsaved":
            entered.set()
            await await_until(release)

    async def reopen():
        reopening.set()
        # The gateway constructs another provider instance for each start.
        return await _envs(tmp_path, fake).prepare(KEY, "dev")

    monkeypatch.setattr(retention, "_environments", environments)
    monkeypatch.setattr(retention, "warn_unsaved_work", warn)
    fake.calls.clear()
    retention.schedule_archived((KEY,))
    tasks = list(retention._TASKS)
    try:
        await await_until(entered)
        resume = asyncio.create_task(reopen())
        tasks.append(resume)
        await await_until(reopening)
        if pause == "environment":
            await await_until(resume)
        release.set()
        await await_until(asyncio.gather(*tasks))
        assert envs.registry.get_by_session(KEY).deletion_due_at == ""
        assert fake.workspaces[row.workspace_name].status == "running"
        assert "stop" not in fake.verbs()
        assert (
            await retention.sweep_due(envs, now=datetime.now(timezone.utc) + timedelta(days=30))
            == 0
        )
        assert envs.registry.get_by_session(KEY).workspace_uuid == row.workspace_uuid
    finally:
        await _finish(tasks, release)


@pytest.mark.asyncio
async def test_resume_waits_for_an_archive_already_stopping_compute(
    tmp_path, monkeypatch, lifecycle_state
):
    fake, envs, row = await _started(tmp_path)
    entered, release, reopening = asyncio.Event(), asyncio.Event(), asyncio.Event()
    real_stop = fake.stop_workspace

    async def stop(name):
        entered.set()
        await await_until(release)
        return await real_stop(name)

    async def environments():
        return envs

    async def reopen():
        reopening.set()
        return await _envs(tmp_path, fake).prepare(KEY, "dev")

    monkeypatch.setattr(fake, "stop_workspace", stop)
    monkeypatch.setattr(retention, "_environments", environments)
    fake.calls.clear()
    retention.schedule_archived((KEY,))
    tasks = list(retention._TASKS)
    try:
        await await_until(entered)
        resume = asyncio.create_task(reopen())
        tasks.append(resume)
        await await_until(reopening)
        await until_parked(retention.session_lifecycle(KEY).lock, timeout=30)
        assert not resume.done()
        assert envs.registry.get_by_session(KEY).deletion_due_at
        release.set()
        await await_until(asyncio.gather(*tasks))
        assert fake.workspaces[row.workspace_name].status == "running"
        assert fake.verbs() == ["get", "stop", "get", "start"]
        assert envs.registry.get_by_session(KEY).deletion_due_at == ""
    finally:
        await _finish(tasks, release)


@pytest.mark.asyncio
async def test_a_later_close_waits_for_preparation_then_archives(
    tmp_path, monkeypatch, lifecycle_state
):
    fake, envs, row = await _started(tmp_path)
    entered, release, archiving = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def current_user():
        entered.set()
        await await_until(release)
        return "devuser", "u-1"

    async def environments():
        archiving.set()
        return _envs(tmp_path, fake)

    monkeypatch.setattr(fake, "current_user", current_user)
    monkeypatch.setattr(retention, "_environments", environments)
    tasks = [asyncio.create_task(envs.prepare(KEY, "dev"))]
    try:
        await await_until(entered)
        retention.schedule_archived((KEY,))
        tasks.extend(retention._TASKS)
        await await_until(archiving)
        await until_parked(retention.session_lifecycle(KEY).lock, timeout=30)
        assert envs.registry.get_by_session(KEY).deletion_due_at == ""
        assert fake.workspaces[row.workspace_name].status == "running"
        release.set()
        await await_until(asyncio.gather(*tasks))
        assert envs.registry.get_by_session(KEY).deletion_due_at
        assert fake.workspaces[row.workspace_name].status == "stopped"
    finally:
        await _finish(tasks, release)


@pytest.mark.asyncio
async def test_resume_after_a_gateway_restart_uses_the_persisted_stamp(
    tmp_path, monkeypatch, lifecycle_state
):
    fake, envs, row = await _started(tmp_path, archived=True)
    monkeypatch.setattr(retention, "_LIFECYCLES", WeakValueDictionary())
    prepared = await _envs(tmp_path, fake).prepare(KEY, "dev")
    assert prepared.workspace == row.workspace_name
    assert envs.registry.get_by_session(KEY).deletion_due_at == ""
    assert fake.workspaces[row.workspace_name].status == "running"


def test_workspace_writes_use_distinct_staging_paths(tmp_path, monkeypatch):
    ws = wt.Workspace(str(tmp_path))
    staging = []
    real_replace = os.replace

    def replace(source, destination):
        staging.append(Path(source))
        assert Path(source).parent == tmp_path
        assert source != destination
        return real_replace(source, destination)

    monkeypatch.setattr(wt.os, "replace", replace)
    ws.write_file("same.txt", "one")
    ws.write_file("same.txt", "two")
    assert len(staging) == len(set(staging)) == 2
    assert (tmp_path / "same.txt").read_text() == "two"
    assert set(tmp_path.iterdir()) == {tmp_path / "same.txt", tmp_path / wt.LOCK_DIR}


def test_overlapping_guest_writes_keep_their_own_contents(tmp_path, monkeypatch):
    ws = wt.Workspace(str(tmp_path))
    entered, release = threading.Event(), threading.Event()
    real_replace = os.replace
    staging = []

    def replace(source, destination):
        staging.append(Path(source))
        if len(staging) == 1:
            entered.set()
            assert release.wait(30), "first writer was not released"
        return real_replace(source, destination)

    monkeypatch.setattr(wt.os, "replace", replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(ws.write_file, "same.txt", "agent")
        try:
            assert entered.wait(30), "first writer did not stage its content"
            second = pool.submit(ws.write_file, "same.txt", "second")
            assert staging[0].read_text() == "agent"
        finally:
            release.set()
            first.result(timeout=30)
        assert second.result(timeout=30).startswith("wrote")
    assert len(staging) == len(set(staging)) == 2
    assert (tmp_path / "same.txt").read_text() == "second"
    assert set(tmp_path.iterdir()) == {tmp_path / "same.txt", tmp_path / wt.LOCK_DIR}


@pytest.mark.parametrize("mode", [0o600, 0o755])
def test_workspace_writes_preserve_existing_mode(tmp_path, mode):
    path = tmp_path / "executable"
    path.write_text("before")
    path.chmod(mode)
    expected_mode = stat.S_IMODE(path.stat().st_mode)
    wt.Workspace(str(tmp_path)).write_file(path.name, "after")
    assert path.read_text() == "after"
    assert stat.S_IMODE(path.stat().st_mode) == expected_mode


def test_workspace_write_cleans_only_its_staging_file_on_failure(tmp_path, monkeypatch):
    path, unrelated = tmp_path / "same.txt", tmp_path / "another-writer.tmp"
    path.write_text("before")
    unrelated.write_text("leave intact")

    def fail(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr(wt.os, "replace", fail)
    with pytest.raises(OSError, match="replace failed"):
        wt.Workspace(str(tmp_path)).write_file(path.name, "after")
    assert path.read_text() == "before"
    assert unrelated.read_text() == "leave intact"
    assert set(tmp_path.iterdir()) == {path, unrelated, tmp_path / wt.LOCK_DIR}
