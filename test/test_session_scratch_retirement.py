"""A reset cannot bind its replacement SID to the discarded conversation's tree."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_subagent_scratch_tree_index import TestAResumedKeyRejoinsItsTree as _ResumeHarness
from test_subagent_scratch_tree_index import (  # noqa: F401 -- fixtures requested by name
    isolated_tree_bindings,
    scratch_root_fixture,
)

from kiro_crew import agent_scratch as sc
from kiro_crew import platform_compat
from kiro_crew.testing.wait import await_until, default_timeout, finished, until_parked


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("retirement", ["persisted", "raises", "missing_write"])
async def test_reset_to_provider_without_tree_retires_before_publishing(
    scratch_root, monkeypatch, retirement, restart
):
    scratch_root.mkdir(parents=True)
    tree = scratch_root / "discarded-conversation"
    tree.mkdir()
    (tree / "work.txt").write_text("keep the old files", encoding="utf-8")
    key = "dashboard:retire-scratch"
    mgr, factory = _ResumeHarness()._manager("", tree)
    service = mgr._allocation_boundary()
    service._deps = dataclasses.replace(service._deps, is_acp_provider=MagicMock(return_value=True))
    make_provider = factory.side_effect
    providers = []

    def provider_factory(*args, **kwargs):
        provider = make_provider(*args, **kwargs)
        provider.work_scratch_dir = tree if not providers else None
        provider.client._session_id = "sid-first" if not providers else "sid-replacement"
        provider.client.resumed = False
        providers.append(provider)
        return provider

    factory.side_effect = provider_factory
    mapped = {}
    published_trees = []

    def publish(session_key, sid, **kwargs):
        published_trees.append(sc._read_tree_index(scratch_root).get(session_key))
        mapped[session_key] = sid

    mgr._session_map.get.side_effect = lambda session_key: mapped.get(session_key)
    mgr._session_map.mapped_sid.side_effect = lambda session_key: mapped.get(session_key)
    mgr._session_map.set.side_effect = publish
    mgr._session_map.clear_sid.side_effect = lambda session_key: mapped.pop(session_key, None)
    kill = MagicMock()
    monkeypatch.setattr(mgr, "_dispatch_hard_kill", kill)
    try:
        await mgr.get_or_create(key, agent="kirocrew")
        assert sc.recorded_tree(key) == tree
        assert mapped[key] == "sid-first"
        mgr.release(key)
        if restart:
            monkeypatch.setattr(sc, "_TREE_INDEX_LIVE", {})
            monkeypatch.setattr(sc, "_TREE_INDEX_TICKETS", {})
            monkeypatch.setattr(sc, "_TREE_INDEX_UNPERSISTED", set())
            monkeypatch.setattr(sc, "_TREE_INDEX_RETIRED_TICKET", -1)
            monkeypatch.setattr(sc, "_TREE_INDEX_DISK_SNAPSHOT", None)
        assert await mgr.reset(key, clear_conversation=True)
        assert key not in mapped
        assert sc.recorded_tree(key) == tree
        mgr._session_map.set.reset_mock()
        kill.reset_mock()
        if retirement != "persisted":
            monkeypatch.setattr(
                sc,
                "atomic_write",
                MagicMock(side_effect=OSError("full disk") if retirement == "raises" else None),
            )
            with pytest.raises(OSError, match="Could not persist scratch retirement"):
                await mgr.get_or_create(key, agent="kirocrew")
            assert mgr.get_provider(key) is None
            mgr._session_map.set.assert_not_called()
            kill.assert_called_once_with(providers[-1])
            assert key not in mapped
            index = scratch_root / sc.TREE_INDEX_FILENAME
            rows = json.loads(index.read_text(encoding="utf-8"))
            assert rows[key] == tree.name
        else:
            await mgr.get_or_create(key, agent="kirocrew")
            assert mapped[key] == "sid-replacement"
            assert published_trees == [tree.name, None]
            assert sc.recorded_tree(key) is None
            # A restart discards every in-process fallback and must still see no binding.
            monkeypatch.setattr(sc, "_TREE_INDEX_LIVE", {})
            monkeypatch.setattr(sc, "_TREE_INDEX_TICKETS", {})
            monkeypatch.setattr(sc, "_TREE_INDEX_UNPERSISTED", set())
            monkeypatch.setattr(sc, "_TREE_INDEX_RETIRED_TICKET", -1)
            monkeypatch.setattr(sc, "_TREE_INDEX_DISK_SNAPSHOT", None)
            assert sc.recorded_tree(key) is None
        assert "shared_scratch" not in factory.call_args.kwargs
        assert (tree / "work.txt").read_text(encoding="utf-8") == "keep the old files"
    finally:
        await mgr.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("root_state", ["linked", "absent"])
async def test_no_tree_provider_starts_without_accessing_unusable_root(
    scratch_root, tmp_path, monkeypatch, root_state
):
    key = "dashboard:unusable-scratch"
    outside = tmp_path / "outside"
    outside.mkdir()
    old_work = outside / "work.txt"
    old_work.write_text("keep the old files", encoding="utf-8")
    index = outside / sc.TREE_INDEX_FILENAME
    index.write_text(json.dumps({key: "old-tree"}), encoding="utf-8")
    original_index = index.read_bytes()
    if root_state == "linked":
        platform_compat.symlink_or_junction(outside, scratch_root)
    read_index = MagicMock(side_effect=AssertionError("must not read an unusable root"))
    write_index = MagicMock(side_effect=AssertionError("must not write an unusable root"))
    monkeypatch.setattr(sc, "_read_tree_index", read_index)
    monkeypatch.setattr(sc, "atomic_write", write_index)
    mgr, _factory = _ResumeHarness()._manager("", None)
    mgr._session_map.get_native_fork.return_value = None
    try:
        provider, is_new, _resumed = await mgr.get_or_create(key, agent="kirocrew")
        provider.start.assert_awaited_once()
        assert is_new
        assert mgr.get_provider(key) is provider
        read_index.assert_not_called()
        write_index.assert_not_called()
        assert index.read_bytes() == original_index
        assert old_work.read_text(encoding="utf-8") == "keep the old files"
        if root_state == "absent":
            assert not scratch_root.exists()
        else:
            assert platform_compat.is_link_or_junction(scratch_root)
    finally:
        await mgr.close_all()


def test_unreadable_real_root_does_not_count_as_no_binding(scratch_root, monkeypatch):
    scratch_root.mkdir()
    monkeypatch.setattr(
        sc, "_read_tree_index", MagicMock(side_effect=PermissionError("unreadable index"))
    )
    write_index = MagicMock()
    monkeypatch.setattr(sc, "atomic_write", write_index)
    assert not sc.abandon_tree("dashboard:unreadable-scratch")
    write_index.assert_not_called()


@pytest.mark.asyncio
async def test_retirement_does_not_refuse_delayed_registration_of_another_key(
    scratch_root, monkeypatch
):
    scratch_root.mkdir()
    tree = scratch_root / "delayed-tree"
    retired = scratch_root / "retired-tree"
    tree.mkdir()
    retired.mkdir()
    key_a = "dashboard:delayed-registration"
    key_b = "dashboard:unrelated-retirement"
    assert sc.record_tree(key_b, retired)
    mgr, _factory = _ResumeHarness()._manager("", None)
    mgr._session_map.get_native_fork.return_value = None
    entered = asyncio.Event()
    worker_finished = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    budget = default_timeout()
    record = sc.record_tree

    def delayed_record(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        try:
            if not release.wait(timeout=budget):
                raise AssertionError("registration worker was not released")
            return record(*args, **kwargs)
        finally:
            loop.call_soon_threadsafe(worker_finished.set)

    monkeypatch.setattr(sc, "record_tree", delayed_record)
    # Dispatch A's production writer before B retires, without serializing the
    # independent keys through one manager's registry lock.
    pending = asyncio.create_task(mgr._allocation_boundary()._record_scratch_tree(key_a, tree))
    try:
        await await_until(entered, what="registration A dispatched")
        await finished(mgr.get_or_create(key_b, agent="kirocrew"), what="retirement B finished")
        assert key_a not in sc._TREE_INDEX_LIVE
        assert sc.recorded_tree(key_b) is None
        release.set()
        await await_until(pending, what="registration A persisted")
        assert sc.recorded_tree(key_a) == tree
        assert sc._read_tree_index(scratch_root) == {key_a: tree.name}
        assert retired.is_dir()
    finally:
        release.set()
        await await_until(asyncio.gather(pending, return_exceptions=True), what="writer cleanup")
        if entered.is_set():
            await await_until(worker_finished, what="registration worker cleanup")
        await mgr.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("write_site", ["no_tree", "failed_join", "registration"])
async def test_cancelled_scratch_write_drains_before_replacement(
    scratch_root, monkeypatch, write_site
):
    scratch_root.mkdir()
    old_tree = scratch_root / "old-tree"
    successor = scratch_root / "successor"
    old_tree.mkdir()
    successor.mkdir()
    old_work = old_tree / "work.txt"
    old_work.write_text("keep the old files", encoding="utf-8")
    key = "dashboard:cancelled-retirement"
    assert sc.record_tree(key, old_tree)
    mgr, factory = _ResumeHarness()._manager("sid-old" if write_site == "failed_join" else "", None)
    mgr._session_map.get_native_fork.return_value = None
    make_provider = factory.side_effect
    replacing = False

    def build(*args, **kwargs):
        provider = make_provider(*args, **kwargs)
        if replacing:
            provider.work_scratch_dir = None if write_site == "registration" else successor
        elif write_site == "registration":
            provider.work_scratch_dir = old_tree
        elif kwargs.get("shared_scratch") is not None:
            provider.start = AsyncMock(side_effect=sc.SharedScratchJoinError("cannot join"))
        return provider

    factory.side_effect = build
    monkeypatch.setattr(mgr, "_dispatch_hard_kill", MagicMock())
    entered = asyncio.Event()
    worker_finished = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    budget = default_timeout()
    writer_name = "record_tree" if write_site == "registration" else "abandon_tree"
    writer = getattr(sc, writer_name)
    results = []

    def delayed_write(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        try:
            # The registry lock must outlive the worker even when its awaiter
            # receives repeated cancellation.
            if not release.wait(timeout=budget):
                raise AssertionError("scratch worker was not released")
            result = writer(*args, **kwargs)
            results.append(result)
            return result
        finally:
            loop.call_soon_threadsafe(worker_finished.set)

    monkeypatch.setattr(sc, writer_name, delayed_write)
    eager = asyncio.create_task(
        mgr.get_or_create(
            key,
            agent="kirocrew",
            speculative=True,
            speculative_resume=write_site == "failed_join",
        )
    )
    replacement = None
    try:
        await await_until(entered, what="scratch write dispatched to worker")
        eager.cancel()
        replacing = True
        mgr._session_map.get.return_value = None
        replacement = asyncio.create_task(mgr.get_or_create(key, agent="kirocrew"))
        await until_parked(mgr._allocation_boundary()._lock)
        assert not eager.done()
        assert not replacement.done()
        eager.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await await_until(eager, what="cancelled eager allocation rolled back")
        await await_until(worker_finished, what="cancelled scratch worker finished")
        provider, _, _ = await await_until(replacement, what="replacement registered")
        assert results == [True]
        expected = None if write_site == "registration" else successor
        assert provider.work_scratch_dir == expected
        assert sc.recorded_tree(key) == expected
        assert sc._read_tree_index(scratch_root).get(key) == (
            expected.name if expected is not None else None
        )
        assert old_work.read_text(encoding="utf-8") == "keep the old files"
    finally:
        release.set()
        if not eager.done():
            eager.cancel()
        tasks = [eager] if replacement is None else [eager, replacement]
        for task in tasks:
            if not task.done():
                task.cancel()
        await await_until(asyncio.gather(*tasks, return_exceptions=True), what="allocation cleanup")
        if entered.is_set():
            await await_until(worker_finished, what="scratch worker cleanup")
        await mgr.close_all()
