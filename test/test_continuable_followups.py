"""Tests for the continuable-conversation follow-up fixes (#1113/#1114/#1115).

- #1113: spawn_steer startup grace — a steer landing before the run's
  session registers waits (bounded) instead of failing with a bare
  ``no_session``; typed ``session_starting`` on timeout; ``not_running``
  when the run finishes mid-wait.
- #1114: conversation TTL registry rebuild from state.json on the reaper's
  first pass after a gateway restart, gated on session files still being
  resumable (released conversations stay dead).
- #1115: state.json as the single source of retention truth — the
  SessionManager continuable cache falls back to disk on a miss, promotion
  goes through one choke point, and release demotes the disk flag.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.subagent as subagent_mod
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_persistence import create_agent_folder, read_state, update_state

# Subagent-registry isolation is provided globally by the autouse
# ``_isolate_subagents_dir`` fixture in ``conftest.py``.


def _mock_sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.mark_continuable = MagicMock()
    sessions.unmark_continuable = MagicMock()
    sessions.resumable_sid = MagicMock(return_value="sid-123")
    sessions.seed_conversation = MagicMock()
    sessions.forget_conversation = MagicMock(return_value="sid-123")
    sessions.conversation_provider = MagicMock(return_value="acp")
    sessions.get_provider = MagicMock(return_value=None)
    sessions.set_continuable_fallback = MagicMock()
    return sessions


def _mock_ctx_builder() -> MagicMock:
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    return ctx


def _manager(sessions: MagicMock | None = None) -> SubagentManager:
    return SubagentManager(
        sessions=sessions or _mock_sessions(),
        ctx_builder=_mock_ctx_builder(),
    )


def _steer_provider(ok: bool = True) -> MagicMock:
    provider = MagicMock()
    provider.steer = AsyncMock(return_value=ok)
    return provider


# ── #1113: steer startup grace ──


class TestSteerStartupGrace:
    @pytest.mark.asyncio
    async def test_steer_waits_for_session_registration(self) -> None:
        """A steer arriving before the session registers succeeds once the
        provider shows up within the grace window."""
        sessions = _mock_sessions()
        provider = _steer_provider()
        # None on the first two polls, then the provider registers.
        sessions.get_provider = MagicMock(side_effect=[None, None, provider, provider])
        mgr = _manager(sessions)
        info = SubagentInfo(id="run1", task="t")
        mgr._agents["run1"] = info
        with (
            patch.object(subagent_mod, "_STEER_STARTUP_WAIT_SECS", 1.0),
            patch.object(subagent_mod, "_STEER_STARTUP_POLL_SECS", 0.01),
        ):
            ok, detail = await mgr.steer_run("run1", "adjust course")
        assert ok is True
        assert detail == "ok"
        provider.steer.assert_awaited_once_with("adjust course")

    @pytest.mark.asyncio
    async def test_steer_session_starting_after_timeout(self) -> None:
        """Provider never registers within the window → typed session_starting."""
        sessions = _mock_sessions()
        sessions.get_provider = MagicMock(return_value=None)
        mgr = _manager(sessions)
        mgr._agents["run1"] = SubagentInfo(id="run1", task="t")
        with (
            patch.object(subagent_mod, "_STEER_STARTUP_WAIT_SECS", 0.05),
            patch.object(subagent_mod, "_STEER_STARTUP_POLL_SECS", 0.01),
        ):
            ok, detail = await mgr.steer_run("run1", "msg")
        assert ok is False
        assert detail.startswith("session_starting")

    @pytest.mark.asyncio
    async def test_steer_not_running_when_run_finishes_mid_wait(self) -> None:
        """A run completing during the grace window flips the answer to
        not_running — the message is never injected into a dead turn."""
        sessions = _mock_sessions()
        sessions.get_provider = MagicMock(return_value=None)
        mgr = _manager(sessions)
        info = SubagentInfo(id="run1", task="t")
        mgr._agents["run1"] = info

        async def _finish_soon() -> None:
            await asyncio.sleep(0.02)
            info.done = True

        with (
            patch.object(subagent_mod, "_STEER_STARTUP_WAIT_SECS", 5.0),
            patch.object(subagent_mod, "_STEER_STARTUP_POLL_SECS", 0.01),
        ):
            finisher = asyncio.ensure_future(_finish_soon())
            ok, detail = await mgr.steer_run("run1", "msg")
            await finisher
        assert ok is False
        assert detail.startswith("not_running")

    @pytest.mark.asyncio
    async def test_steer_immediate_when_provider_present(self) -> None:
        """No grace-window delay when the session is already reachable."""
        sessions = _mock_sessions()
        provider = _steer_provider()
        sessions.get_provider = MagicMock(return_value=provider)
        mgr = _manager(sessions)
        mgr._agents["run1"] = SubagentInfo(id="run1", task="t")
        t0 = time.monotonic()
        ok, _ = await mgr.steer_run("run1", "msg")
        assert ok is True
        assert time.monotonic() - t0 < 1.0  # no full-window wait


# ── #1114: TTL registry rebuild from disk ──


class TestConversationRegistryRebuild:
    def _keep_run(self, run_id: str, *, sid: str = "sid-x", conv_key: str = "") -> None:
        create_agent_folder(run_id, task="t")
        update_state(
            run_id,
            keep=True,
            session_id=sid,
            provider="acp",
            cwd="",
            conversation_key=conv_key,
        )

    def test_scan_keep_states_finds_only_keep_runs(self) -> None:
        mgr = _manager()
        self._keep_run("keeprun1")
        create_agent_folder("plainrun", task="t")  # keep absent
        found = mgr._scan_keep_states()
        ids = {t[0] for t in found}
        assert ids == {"keeprun1"}
        conv_id, conv_key, sid, provider, _cwd, last_used = found[0]
        assert conv_key == "subagent:keeprun1"
        assert sid == "sid-x"
        assert provider == "acp"
        assert last_used > 0

    def test_scan_uses_recorded_conversation_key(self) -> None:
        """A continuation run's state points at the ORIGINAL conversation."""
        mgr = _manager()
        self._keep_run("contrun", conv_key="subagent:origrun")
        found = mgr._scan_keep_states()
        assert found[0][1] == "subagent:origrun"

    @pytest.mark.asyncio
    async def test_rebuild_seeds_map_registry_and_cache(self) -> None:
        sessions = _mock_sessions()
        # Not resumable before the seed, resumable after it.
        sessions.resumable_sid = MagicMock(side_effect=[None, "sid-x"])
        mgr = _manager(sessions)
        self._keep_run("keeprun1")
        await mgr._rebuild_conversation_registry()
        sessions.seed_conversation.assert_called_once_with(
            "subagent:keeprun1", "sid-x", provider="acp", cwd=""
        )
        sessions.mark_continuable.assert_called_with("subagent:keeprun1")
        assert "subagent:keeprun1" in mgr._conversations

    @pytest.mark.asyncio
    async def test_rebuild_skips_released_conversations(self) -> None:
        """Stale keep=True with no resumable files (released) stays dead."""
        sessions = _mock_sessions()
        sessions.resumable_sid = MagicMock(return_value=None)  # files gone
        mgr = _manager(sessions)
        self._keep_run("released1")
        await mgr._rebuild_conversation_registry()
        assert "subagent:released1" not in mgr._conversations
        sessions.mark_continuable.assert_not_called()

    @pytest.mark.asyncio
    async def test_rebuild_newest_record_wins_for_duplicate_conversation(self) -> None:
        """A conversation appears once per run that touched it (original +
        continuations). The rebuild must register the NEWEST last_used —
        seeding a stale one would let the same pass's sweep expire a
        conversation whose real last-use is recent (GPT finding, PR #1246)."""
        sessions = _mock_sessions()
        mgr = _manager(sessions)
        # Original run: old timestamp. Continuation run: recent, points at
        # the same conversation via conversation_key. update_state() always
        # stamps updated_at=now, so write the timestamps directly.
        import json as _json

        from kiro_crew.subagent_persistence import _agent_dir

        def _write_keep_state(run_id: str, conv_key: str, ts: float) -> None:
            create_agent_folder(run_id, task="t")
            p = _agent_dir(run_id) / "state.json"
            state = _json.loads(p.read_text(encoding="utf-8"))
            state.update(
                keep=True, session_id="sid-x", provider="acp", cwd="",
                conversation_key=conv_key, updated_at=ts,
            )
            p.write_text(_json.dumps(state), encoding="utf-8")

        _write_keep_state("origrun", "", 1000.0)
        _write_keep_state("contrun", "subagent:origrun", 2000.0)
        await mgr._rebuild_conversation_registry()
        assert mgr._conversations["subagent:origrun"] == 2000.0

    @pytest.mark.asyncio
    async def test_rebuild_flag_not_set_on_failure(self) -> None:
        """A failed rebuild must retry on the next sweep (flag only set on
        success — Arbiter suggestion, PR #1246). Covered here by verifying
        the rebuild raises through (the reaper catches and leaves the flag
        unset)."""
        sessions = _mock_sessions()
        sessions.resumable_sid = MagicMock(side_effect=RuntimeError("boom"))
        mgr = _manager(sessions)
        self._keep_run("keeprun1")
        # Per-entry code is wrapped, but a raising sessions API inside the
        # loop is caught per-entry; simulate a scan-level failure instead.
        with patch.object(mgr, "_scan_keep_states", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                await mgr._rebuild_conversation_registry()

    @pytest.mark.asyncio
    async def test_rebuild_never_overwrites_live_registration(self) -> None:
        sessions = _mock_sessions()
        mgr = _manager(sessions)
        self._keep_run("keeprun1")
        live_ts = time.time() + 999
        mgr._conversations["subagent:keeprun1"] = live_ts
        await mgr._rebuild_conversation_registry()
        assert mgr._conversations["subagent:keeprun1"] == live_ts
        sessions.seed_conversation.assert_not_called()


# ── #1115: state.json as retention source of truth ──


class TestRetentionSourceOfTruth:
    def test_keep_recorded_on_disk(self) -> None:
        mgr = _manager()
        create_agent_folder("keeprun1", task="t")
        update_state("keeprun1", keep=True)
        create_agent_folder("plainrun", task="t")
        assert mgr._keep_recorded_on_disk("subagent:keeprun1") is True
        assert mgr._keep_recorded_on_disk("subagent:plainrun") is False
        assert mgr._keep_recorded_on_disk("subagent:missing") is False
        assert mgr._keep_recorded_on_disk("dashboard:tab1") is False

    def test_manager_installs_fallback_on_sessions(self) -> None:
        sessions = _mock_sessions()
        mgr = _manager(sessions)
        sessions.set_continuable_fallback.assert_called_once_with(
            mgr._keep_recorded_on_disk
        )

    def test_promote_conversation_writes_all_three_surfaces(self) -> None:
        sessions = _mock_sessions()
        mgr = _manager(sessions)
        create_agent_folder("conv1", task="t")
        mgr._promote_conversation("conv1", "subagent:conv1")
        assert bool((read_state("conv1") or {}).get("keep")) is True
        sessions.mark_continuable.assert_called_with("subagent:conv1")
        assert "subagent:conv1" in mgr._conversations

    def test_release_demotes_disk_keep(self) -> None:
        """release_conversation must flip keep=False on disk, or the disk
        fallback would resurrect the released conversation."""
        sessions = _mock_sessions()
        mgr = _manager(sessions)
        create_agent_folder("conv1", task="t")
        update_state("conv1", keep=True)
        with patch.object(subagent_mod, "_cleanup_session_files_sync"):
            ok, detail = mgr.release_conversation("conv1")
        assert ok is True
        assert bool((read_state("conv1") or {}).get("keep")) is False

    def _session_manager(self):  # type: ignore[no-untyped-def]
        from kiro_crew.session import SessionManager

        with patch.object(SessionManager, "__init__", lambda self: None):
            smgr = SessionManager()  # type: ignore[call-arg]
        smgr._continuable_keys = set()
        smgr._fold_key = lambda k: k  # type: ignore[assignment]
        return smgr

    def test_session_fallback_warms_cache_on_disk_hit(self) -> None:
        smgr = self._session_manager()
        smgr.set_continuable_fallback(lambda key: key == "subagent:kept")
        assert smgr.is_continuable("subagent:kept") is True
        # Cache warmed: a second check answers without the fallback.
        smgr.set_continuable_fallback(None)
        assert smgr.is_continuable("subagent:kept") is True
        assert smgr.is_continuable("subagent:other") is False

    def test_session_fallback_exception_is_safe(self) -> None:
        smgr = self._session_manager()

        def _boom(_key: str) -> bool:
            raise RuntimeError("disk unhappy")

        smgr.set_continuable_fallback(_boom)
        assert smgr.is_continuable("subagent:kept") is False

    def test_session_fallback_absent_attribute_is_safe(self) -> None:
        """Fixtures that bypass __init__ (no _continuable_fallback attr)
        must not crash the helper."""
        smgr = self._session_manager()
        # No set_continuable_fallback call — attribute never created.
        assert smgr.is_continuable("subagent:kept") is False
