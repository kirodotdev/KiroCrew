"""Guards for the autonomous-side unfreeze round.

What must hold:

- The owner loop drives ``poller.poll()`` FIRE-AND-FORGET. Awaiting it inline
  deadlocks the autonomous side: a poll blocked in ``_await_spawn`` starves the
  very loop whose ``tick()`` fires that wait's timeout, so every queued
  move/notify freezes until a restart.
- A blocked spawn wait resolves from ``tick()`` (the timeout) and from the
  completion watcher (the early path), never only from luck.
- Watchlist writes made by OTHER processes (the MCP server) surface as a
  watchlist-changed publish via the mtime watch.
- A neutralize entry carries ``disabled: true`` so the denied server is never
  launched, and keeps ``disabledTools`` as defense in depth.
- An app agent's ``@kirocrew-cron`` / ``@kirocrew-core`` tool references get
  their server specs materialized (they live in the HOST agent config, so the
  reference dangles without the copy).
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.mochi import hooks
from kiro_crew.apps.builtins.mochi.queue_poller import QueuePoller


class TestOwnerLoopNeverAwaitsPoll:
    def test_poll_is_dispatched_as_a_task(self):
        """Source-level guard: the loop must create_task(poll), never await it.

        A behavioural test cannot see this cheaply (the deadlock needs a real
        blocked spawn inside the real loop), so the shape itself is pinned.
        """
        src = Path(hooks.__file__).read_text()
        assert "asyncio.create_task(self.poller.poll())" in src
        assert not re.search(r"await\s+self\.poller\.poll\(\)", src)

    @pytest.mark.asyncio
    async def test_tick_resolves_a_blocked_spawn_wait(self, tmp_path):
        """The regression this round fixed, at the poller level: a poll stuck
        in _await_spawn must resolve when tick() fires the deadline — which
        only ever runs if the loop was not awaiting that same poll."""

        class _CB:
            async def spawn_agent(self, prompt: str) -> str:
                return "spawn-1"

            def get_due_watch_items(self):
                return []

        now = {"ms": 1_000_000}
        poller = QueuePoller(str(tmp_path / "q.json"), _CB(), clock=lambda: now["ms"])
        poller.start()

        wait_task = asyncio.create_task(poller._spawn_watch_check("check"))
        await asyncio.sleep(0)  # let it reach the wait
        assert poller._spawn_future is not None and not poller._spawn_future.done()

        now["ms"] += 10 * 60_000  # past any spawn deadline
        poller.tick(now["ms"])
        await asyncio.wait_for(wait_task, timeout=1)


class TestSpawnCompletionWatcher:
    @pytest.mark.asyncio
    async def test_done_probe_releases_the_wait_early(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hooks, "_SPAWN_PROBE_INTERVAL_SECS", 0.01)
        runtime = hooks.MochiRuntime(_ctx(tmp_path))
        released: list[str] = []
        runtime.poller.notify_agent_done = released.append  # type: ignore[method-assign]

        class _Spawn:
            def is_done(self, spawn_id: str) -> bool:
                return True

        runtime.watch_spawn_completion("id-1", _Spawn())
        await asyncio.sleep(0.1)
        assert released == ["id-1"]

    @pytest.mark.asyncio
    async def test_empty_spawn_id_starts_no_watcher(self, tmp_path):
        runtime = hooks.MochiRuntime(_ctx(tmp_path))
        before = len(runtime._poll_tasks)
        runtime.watch_spawn_completion("", object())
        assert len(runtime._poll_tasks) == before


class TestWatchlistMtimeWatch:
    def test_external_write_publishes_changed(self, tmp_path):
        runtime = hooks.MochiRuntime(_ctx(tmp_path))
        published: list[str] = []
        runtime.publish = lambda ch, _p: published.append(ch)  # type: ignore[method-assign]

        wl = Path(runtime._watchlist_path)
        wl.write_text(json.dumps({"items": []}))
        # Simulate a write from another process: only the file moves.
        import os

        os.utime(wl, ns=(1, 10_000_000_000))
        runtime._poll_watchlist_file()
        assert published == ["mochi:watchlist-changed"]

    def test_no_change_publishes_nothing(self, tmp_path):
        runtime = hooks.MochiRuntime(_ctx(tmp_path))
        published: list[str] = []
        runtime.publish = lambda ch, _p: published.append(ch)  # type: ignore[method-assign]
        runtime._poll_watchlist_file()  # missing file, mtime 0 -> 0
        assert published == []


class TestNeutralizeDisablesServer:
    def test_neutralize_entry_is_disabled_and_keeps_tool_deny(self, monkeypatch):
        from kiro_crew.apps import bridges

        monkeypatch.setattr(
            bridges, "_global_mcp_specs", lambda: {"amb": {"command": "amb-cmd", "args": []}}
        )
        out = bridges._apply_agent_mcp_policy(
            {"tools": ["@amb"], "mcpServers": {}},
            "a",
            {"agents": {"a": {"neutralize": {"amb": ["t1"]}}}},
        )
        entry = out["mcpServers"]["amb"]
        # disabled -> kiro-cli never launches the process (primary deny);
        # disabledTools stays for a kiro-cli that predates the flag.
        assert entry["disabled"] is True
        assert entry["disabledTools"] == ["t1"]
        assert entry["command"] == "amb-cmd"


class TestManagedRefsMaterialized:
    def test_managed_tool_refs_get_specs(self):
        from kiro_crew.apps.bridges import _materialize_managed_refs

        data: dict[str, Any] = {"tools": ["fs_read", "@kirocrew-cron", "@kirocrew-core"]}
        _materialize_managed_refs(data)
        assert data["mcpServers"]["kirocrew-cron"]["args"][-1] == "mcp-cron"
        assert data["mcpServers"]["kirocrew-core"]["args"][-1] == "mcp-core"

    def test_existing_entry_is_not_overwritten(self):
        from kiro_crew.apps.bridges import _materialize_managed_refs

        data: dict[str, Any] = {
            "tools": ["@kirocrew-cron"],
            "mcpServers": {"kirocrew-cron": {"command": "custom"}},
        }
        _materialize_managed_refs(data)
        assert data["mcpServers"]["kirocrew-cron"]["command"] == "custom"

    def test_unreferenced_managed_servers_stay_out(self):
        from kiro_crew.apps.bridges import _materialize_managed_refs

        data: dict[str, Any] = {"tools": ["fs_read"]}
        _materialize_managed_refs(data)
        assert data.get("mcpServers", {}) == {}


def _ctx(tmp_path: Path) -> Any:
    class _Ctx:
        name = "mochi"
        data_dir = tmp_path
        events = None
        config: dict[str, Any] = {}

    return _Ctx()


class TestNamespacedAgentFileCheck:
    """The EXECUTABLE INVARIANT warning must accept app-registered agents.

    App agents are materialized as ``<app>--<agent>.json``; kiro-cli resolves
    by the JSON ``name`` field. The file-name-only check warned on every boot
    for perfectly spawnable app agents.
    """

    def test_namespaced_file_with_matching_name_counts(self, tmp_path, monkeypatch):
        import kiro_crew.agent as agent_mod
        from kiro_crew.dashboard.handlers.agents import _namespaced_agent_file_exists

        monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", tmp_path)
        (tmp_path / "someapp--helper.json").write_text(json.dumps({"name": "helper"}))
        assert _namespaced_agent_file_exists("helper") is True

    def test_name_mismatch_does_not_count(self, tmp_path, monkeypatch):
        import kiro_crew.agent as agent_mod
        from kiro_crew.dashboard.handlers.agents import _namespaced_agent_file_exists

        monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", tmp_path)
        # File name pattern matches but the JSON name field is different —
        # kiro-cli would not resolve it, so the warning must still fire.
        (tmp_path / "someapp--helper.json").write_text(json.dumps({"name": "other"}))
        assert _namespaced_agent_file_exists("helper") is False

    def test_unreadable_file_does_not_count(self, tmp_path, monkeypatch):
        import kiro_crew.agent as agent_mod
        from kiro_crew.dashboard.handlers.agents import _namespaced_agent_file_exists

        monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", tmp_path)
        (tmp_path / "someapp--helper.json").write_text("{not json")
        assert _namespaced_agent_file_exists("helper") is False


class TestMergeWriteHonorsConcurrentReset:
    """A poll holds a STALE queue snapshot; if a reset deletes the queue before
    the write-back lands, a plain clobber would recreate the file and re-run the
    deleted tasks. The plan/replan write-back (and the execute path) go through
    ``_locked_merge_write``, which re-reads under the lock and writes NOTHING when
    the queue is gone — so the reset is honoured and tasks are not resurrected.
    """

    def test_merge_write_writes_nothing_when_queue_reset_concurrently(self, tmp_path):
        from kiro_crew.apps.builtins.mochi.queue_poller import QueuePoller

        class _CB:
            pass

        qpath = str(tmp_path / "q.json")
        poller = QueuePoller(qpath, _CB(), clock=lambda: 0)
        # No file on disk = a concurrent reset/full_replace cleared it after our
        # poll snapshot. Merging our stale done-task back would resurrect it.
        stale = {
            "tasks": [{"id": "t1", "done": True}],
            "planned_at": "x",
            "planned_until": "y",
        }
        poller._locked_merge_write(stale)
        assert not Path(qpath).exists(), "a concurrent reset must not be undone by a stale write"
