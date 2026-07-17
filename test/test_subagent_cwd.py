"""Tests for the ``spawn_run`` ``cwd`` parameter (Mesh-1226).

Covers:
- ``validate_cwd`` helper: absolute, exists, realpath, allowlist matching,
  symlink traversal, disabled feature.
- ``SubagentManager.spawn`` cwd rejection path: invalid cwd returns a done
  ``SubagentInfo`` with an ``error`` and emits a ``rejected_invalid_cwd`` SEL
  event without consuming a concurrency slot.
- ``SubagentManager.spawn`` cwd success path: valid cwd is resolved and
  stored on ``SubagentInfo`` so downstream factories can pick it up.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.subagent import SubagentManager, validate_cwd

# ---------------------------------------------------------------------------
# validate_cwd helper
# ---------------------------------------------------------------------------


class TestValidateCwd:
    """``validate_cwd`` is the single validation surface for the cwd parameter."""

    def test_empty_cwd_returns_empty_no_error(self) -> None:
        """Empty cwd is the default — allowed without regard to roots."""
        resolved, err = validate_cwd("", ["~/workspace"])
        assert resolved == ""
        assert err == ""

    def test_empty_allowed_roots_rejects_non_empty_cwd(self, tmp_path: Path) -> None:
        """With no allowed roots configured, any cwd is rejected (fails-closed)."""
        resolved, err = validate_cwd(str(tmp_path), [])
        assert resolved == ""
        assert "disabled" in err

    def test_relative_cwd_rejected(self) -> None:
        """Relative paths are ambiguous and rejected."""
        resolved, err = validate_cwd("relative/path", ["~/workspace"])
        assert resolved == ""
        assert "absolute" in err

    def test_nonexistent_cwd_rejected(self, tmp_path: Path) -> None:
        """Non-existent path is rejected (subprocess.Popen would fail cryptically)."""
        resolved, err = validate_cwd(str(tmp_path / "does-not-exist"), [str(tmp_path)])
        assert resolved == ""
        assert "does not exist" in err or "not a directory" in err

    def test_cwd_that_is_a_file_rejected(self, tmp_path: Path) -> None:
        """A file path is not a valid cwd."""
        f = tmp_path / "file.txt"
        f.write_text("x")
        resolved, err = validate_cwd(str(f), [str(tmp_path)])
        assert resolved == ""
        assert "directory" in err

    def test_valid_cwd_under_allowed_root_accepted(self, tmp_path: Path) -> None:
        """Happy path: valid absolute dir under an allowed root."""
        (tmp_path / "project").mkdir()
        resolved, err = validate_cwd(str(tmp_path / "project"), [str(tmp_path)])
        assert err == ""
        assert resolved == os.path.realpath(str(tmp_path / "project"))

    def test_cwd_outside_allowed_roots_rejected(self, tmp_path: Path) -> None:
        """A valid directory outside the allowlist is rejected."""
        other = tmp_path / "other"
        other.mkdir()
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        resolved, err = validate_cwd(str(other), [str(allowed)])
        assert resolved == ""
        assert "not under any allowed root" in err

    def test_symlink_target_outside_allowlist_rejected(self, tmp_path: Path) -> None:
        """Symlink pointing outside the allowlist is rejected after realpath."""
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        secret = tmp_path / "secret"
        secret.mkdir()
        link = allowed / "link"
        link.symlink_to(secret)
        resolved, err = validate_cwd(str(link), [str(allowed)])
        assert resolved == ""
        assert "not under any allowed root" in err

    def test_symlink_target_inside_allowlist_accepted(self, tmp_path: Path) -> None:
        """Symlink that resolves inside the allowlist is accepted; resolved path is the realpath."""
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        real = allowed / "real"
        real.mkdir()
        link = allowed / "link"
        link.symlink_to(real)
        resolved, err = validate_cwd(str(link), [str(allowed)])
        assert err == ""
        assert resolved == os.path.realpath(str(real))

    def test_tilde_expanded_in_allowed_roots(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``~`` in allowed_roots expands via ``expanduser``."""
        monkeypatch.setenv("HOME", str(tmp_path))
        project = tmp_path / "ws" / "proj"
        project.mkdir(parents=True)
        resolved, err = validate_cwd(str(project), ["~/ws"])
        assert err == ""
        assert resolved == os.path.realpath(str(project))

    def test_tilde_expanded_in_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``~`` in cwd argument expands via ``expanduser``."""
        monkeypatch.setenv("HOME", str(tmp_path))
        project = tmp_path / "proj"
        project.mkdir()
        resolved, err = validate_cwd("~/proj", [str(tmp_path)])
        assert err == ""
        assert resolved == os.path.realpath(str(project))

    def test_cwd_equals_allowed_root_accepted(self, tmp_path: Path) -> None:
        """Cwd exactly matching an allowed root (not just a subdirectory) is accepted."""
        resolved, err = validate_cwd(str(tmp_path), [str(tmp_path)])
        assert err == ""
        assert resolved == os.path.realpath(str(tmp_path))

    def test_prefix_without_separator_not_treated_as_under_root(
        self, tmp_path: Path,
    ) -> None:
        """``/tmp/allow-extra`` must not match root ``/tmp/allow`` (prefix gotcha)."""
        allow = tmp_path / "allow"
        allow.mkdir()
        sibling = tmp_path / "allow-extra"
        sibling.mkdir()
        resolved, err = validate_cwd(str(sibling), [str(allow)])
        assert resolved == ""
        assert "not under any allowed root" in err


# ---------------------------------------------------------------------------
# SubagentManager.spawn integration — cwd is threaded onto SubagentInfo
# ---------------------------------------------------------------------------


def _mock_sessions() -> MagicMock:
    """Minimal mock so ``spawn()`` can run without touching real providers."""
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_or_create = MagicMock()
    sessions.release = MagicMock()
    sessions.reset = MagicMock()
    sessions.record_success = MagicMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_approval_policy = MagicMock(return_value="")
    return sessions


def _mock_ctx_builder_auto_spawn() -> MagicMock:
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    return ctx


class TestSpawnCwd:
    """``SubagentManager.spawn`` correctly validates and stores cwd."""

    @pytest.mark.asyncio
    async def test_spawn_without_cwd_leaves_field_empty(self) -> None:
        """Omitting cwd preserves backward-compatible behavior (empty string stored)."""
        manager = SubagentManager(
            sessions=_mock_sessions(), ctx_builder=_mock_ctx_builder_auto_spawn(),
        )
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn("t")
        assert info is not None
        assert info.cwd == ""

    @pytest.mark.asyncio
    async def test_spawn_with_valid_cwd_stores_resolved_path(
        self, tmp_path: Path,
    ) -> None:
        """Happy path: valid cwd is resolved and stored on SubagentInfo."""
        project = tmp_path / "project"
        project.mkdir()

        manager = SubagentManager(
            sessions=_mock_sessions(), ctx_builder=_mock_ctx_builder_auto_spawn(),
        )
        mock_cfg = MagicMock()
        mock_cfg.agent.spawn_min_memory_gb = 0
        mock_cfg.agent.subagent_cwd_allowed_roots = [str(tmp_path)]
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"), \
             patch("kiro_crew.subagent.KiroCrewConfig.load", return_value=mock_cfg):
            info = manager.spawn("t", cwd=str(project))

        assert info is not None
        assert info.error == ""
        assert info.cwd == os.path.realpath(str(project))

    @pytest.mark.asyncio
    async def test_spawn_with_invalid_cwd_rejects_and_emits_sel(
        self, tmp_path: Path,
    ) -> None:
        """Invalid cwd returns a done SubagentInfo with error and emits rejected_invalid_cwd SEL.

        The rejection happens before a concurrency slot is consumed, so
        running_count is unchanged.
        """
        manager = SubagentManager(
            sessions=_mock_sessions(), ctx_builder=_mock_ctx_builder_auto_spawn(),
        )
        running_before = manager._running_count
        mock_cfg = MagicMock()
        mock_cfg.agent.spawn_min_memory_gb = 0
        mock_cfg.agent.subagent_cwd_allowed_roots = [str(tmp_path / "allowed")]
        (tmp_path / "allowed").mkdir()

        sel_mock = MagicMock()
        with patch("kiro_crew.subagent.Stats"), \
             patch("kiro_crew.subagent.sel", return_value=sel_mock), \
             patch("kiro_crew.subagent.KiroCrewConfig.load", return_value=mock_cfg):
            info = manager.spawn("t", cwd="/etc")

        assert info is not None
        assert info.done is True
        assert "spawn refused" in info.error
        assert manager._running_count == running_before
        # SEL audit trail fired with the right outcome
        calls = [c for c in sel_mock.log_tool_invocation.call_args_list
                 if c.kwargs.get("outcome") == "rejected_invalid_cwd"]
        assert len(calls) == 1
        assert "cwd" in calls[0].kwargs.get("metadata", {})

    @pytest.mark.asyncio
    async def test_spawn_cwd_disabled_when_allowlist_empty(
        self, tmp_path: Path,
    ) -> None:
        """Config with empty allowed_roots rejects any cwd (fails-closed)."""
        project = tmp_path / "project"
        project.mkdir()
        manager = SubagentManager(
            sessions=_mock_sessions(), ctx_builder=_mock_ctx_builder_auto_spawn(),
        )
        mock_cfg = MagicMock()
        mock_cfg.agent.spawn_min_memory_gb = 0
        mock_cfg.agent.subagent_cwd_allowed_roots = []
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"), \
             patch("kiro_crew.subagent.KiroCrewConfig.load", return_value=mock_cfg):
            info = manager.spawn("t", cwd=str(project))
        assert info is not None
        assert info.done is True
        assert "disabled" in info.error

    @pytest.mark.asyncio
    async def test_spawn_at_capacity_queues_cwd_for_dequeue(
        self, tmp_path: Path,
    ) -> None:
        """When pool is at capacity, the resolved cwd must survive the queue.

        Regression guard for the AutoSDE-flagged bug where the queue tuple
        only captured (task, parent, agent, max_turns) and cwd was silently
        dropped on dequeue, re-spawning the subagent in the default sandbox.
        """
        project = tmp_path / "project"
        project.mkdir()
        manager = SubagentManager(
            sessions=_mock_sessions(), ctx_builder=_mock_ctx_builder_auto_spawn(),
        )
        # Force capacity: running_count already at max
        manager._running_count = manager._max_concurrent
        mock_cfg = MagicMock()
        mock_cfg.agent.spawn_min_memory_gb = 0
        mock_cfg.agent.subagent_cwd_allowed_roots = [str(tmp_path)]
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"), \
             patch("kiro_crew.subagent.KiroCrewConfig.load", return_value=mock_cfg):
            info = manager.spawn("t", cwd=str(project))

        assert info is not None
        assert info.id.startswith("q"), "spawn at capacity should have returned a queued id"
        # Queue must carry the resolved cwd so dequeue can re-spawn correctly.
        assert len(manager._queue) == 1
        queued = manager._queue[0]
        assert len(queued) == 5
        assert queued[4] == os.path.realpath(str(project))

    @pytest.mark.asyncio
    async def test_spawn_fails_closed_when_config_load_raises(
        self, tmp_path: Path,
    ) -> None:
        """If KiroCrewConfig.load raises, reject cwd (fail-closed).

        Defaulting to the permissive ``["~/workspace"]`` would silently
        re-enable the feature for admins who explicitly disabled it with
        ``subagent_cwd_allowed_roots = []``.
        """
        project = tmp_path / "project"
        project.mkdir()
        manager = SubagentManager(
            sessions=_mock_sessions(), ctx_builder=_mock_ctx_builder_auto_spawn(),
        )
        load_mock = patch(
            "kiro_crew.subagent.KiroCrewConfig.load",
            side_effect=OSError("config unreadable"),
        )
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"), load_mock:
            info = manager.spawn("t", cwd=str(project))

        assert info is not None
        assert info.done is True
        assert "disabled" in info.error
