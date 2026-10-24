"""Tests for ACP client: the process tree, kill paths, stdin pipe errors and state reset."""

import asyncio
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture
from acp_client_helpers import _POSIX_ONLY

from kiro_crew.acp.client import AcpClient, AcpProcessDied

# ── Process tree cleanup tests ──


class TestGetChildPids:
    @pytest.mark.parametrize(
        "pid",
        [pytest.param(999999, id="nonexistent_pid"), pytest.param(None, id="none_pid")],
    )
    def test_no_children(self, pid):
        from kiro_crew.acp.client import _get_child_pids

        assert _get_child_pids(pid) == []

    def test_own_pid_returns_list(self):
        import os

        from kiro_crew.acp.client import _get_child_pids

        # May or may not have children, but should not raise
        result = _get_child_pids(os.getpid())
        assert isinstance(result, list)

    def test_recursive_children(self, monkeypatch):
        import kiro_crew.acp.client as client_mod
        from kiro_crew.acp.client import _get_child_pids

        # _direct_children tries /proc first, falls back to pgrep.
        # Mock _direct_children directly to avoid platform-specific /proc behavior.
        tree = {1000: [2000, 3000], 2000: [4000], 3000: [5000]}
        monkeypatch.setattr(client_mod, "_direct_children", lambda pid: tree.get(pid, []))
        # Depth-first: 2000 → 4000, then 3000 → 5000
        assert _get_child_pids(1000) == [2000, 4000, 3000, 5000]


@_POSIX_ONLY
class TestIsOurChild:
    def test_nonexistent_pid(self):
        from kiro_crew.acp.client import _is_our_child

        assert _is_our_child(999999) is False

    @pytest.mark.parametrize(
        ("live", "recorded", "expected"),
        [
            # With correct start_time and basename, should match
            pytest.param(b"python3", b"python3", True, id="own_pid_matches_with_basename"),
            pytest.param(b"node", b"node", True, id="basename_match_returns_true"),
            # Recorded as node, but live reads as firefox → recycled
            pytest.param(b"firefox", b"node", False, id="basename_mismatch_returns_false"),
            # Any binary recorded at spawn is supervised — no allowlist needed.
            pytest.param(
                b"some-future-mcp-server",
                b"some-future-mcp-server",
                True,
                id="novel_binary_tracked",
            ),
        ],
    )
    def test_basename_check(self, monkeypatch, live, recorded, expected):
        import sys

        import kiro_crew.acp.client as client_mod
        from kiro_crew.acp.client import _is_our_child

        monkeypatch.setattr("kiro_crew.platform_compat.get_process_start_id", lambda pid: 42)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(client_mod.subprocess_mod, "check_output", lambda cmd, **kw: live)
        assert _is_our_child(999, expected_start=42, expected_basename=recorded) is expected

    def test_no_start_time_rejects(self):
        import os

        from kiro_crew.acp.client import _is_our_child

        # No expected_start → fail-closed
        assert _is_our_child(os.getpid()) is False

    def test_start_time_mismatch_rejects(self):
        import os

        from kiro_crew.acp.client import _is_our_child

        # Our own PID with a wrong start time → should reject (recycled)
        assert _is_our_child(os.getpid(), expected_start=-999) is False

    def test_none_basename_denied_fail_closed(self, monkeypatch):
        """When no basename was recorded, deny (fail-closed)."""
        from kiro_crew.acp.client import _is_our_child

        monkeypatch.setattr("kiro_crew.platform_compat.get_process_start_id", lambda pid: 42)
        # No expected_basename → deny-by-default even with matching start id
        assert _is_our_child(999, expected_start=42, expected_basename=None) is False


@_POSIX_ONLY
class TestKillEscapedChildren:
    @staticmethod
    def _record_sweep(monkeypatch, alive: bool) -> dict[str, list]:
        """Route the sweep's liveness, ownership and kill seams into one record."""
        from kiro_crew import platform_compat
        from kiro_crew.acp import runtime_process_tree

        seen: dict[str, list] = {"probed": [], "owned": [], "killed": []}

        def _pid_exists(pid):
            seen["probed"].append(pid)
            return alive

        def _is_our_child(pid, **_kw):
            seen["owned"].append(pid)
            return True

        monkeypatch.setattr(platform_compat, "pid_exists", _pid_exists)
        monkeypatch.setattr(runtime_process_tree, "_is_our_child", _is_our_child)
        monkeypatch.setattr(
            platform_compat, "kill_pid", lambda pid, sig: seen["killed"].append(pid)
        )
        return seen

    def test_empty_dict(self, monkeypatch):
        from kiro_crew.acp.client import _kill_escaped_children

        seen = self._record_sweep(monkeypatch, alive=True)
        _kill_escaped_children({})
        assert seen == {"probed": [], "owned": [], "killed": []}

    def test_dead_pids_skipped(self, monkeypatch):
        from kiro_crew.acp.client import _kill_escaped_children

        seen = self._record_sweep(monkeypatch, alive=False)
        _kill_escaped_children({999998: None, 999999: None})
        # Leaf-first probe of both, and a gone pid is never vetted or killed.
        assert seen == {"probed": [999999, 999998], "owned": [], "killed": []}

    def test_reverse_order_and_allowlist(self, monkeypatch):
        import kiro_crew.acp.client as client_mod
        from kiro_crew.acp.client import _kill_escaped_children

        killed: list[int] = []

        def fake_kill(pid, sig):
            if sig == 0:
                return  # alive check
            killed.append(pid)

        def fake_is_our(pid, expected_start=None, expected_basename=None):
            return pid != 200  # 200 is "recycled"

        monkeypatch.setattr(client_mod.os, "kill", fake_kill)
        monkeypatch.setattr(client_mod, "_is_our_child", fake_is_our)

        _kill_escaped_children({100: None, 200: None, 300: None})
        # 200 skipped (not ours), killed in reverse: 300, 100
        assert killed == [300, 100]


class TestChildPidsField:
    def test_default_empty(self):
        client = AcpClient()
        assert client._child_pids == {}

    def test_cleared_on_reset(self):
        client = AcpClient()
        client._child_pids = {123: (None, b"node"), 456: (None, b"python")}
        client._reset_state()
        assert client._child_pids == {}


@_POSIX_ONLY
class TestReadBasename:
    def test_reads_basename_from_ps(self, monkeypatch):
        import sys

        import kiro_crew.acp.client as client_mod
        from kiro_crew.acp.client import _read_basename

        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(
            client_mod.subprocess_mod,
            "check_output",
            lambda cmd, **kw: b"/usr/local/bin/node\n",
        )
        assert _read_basename(123) == b"node"

    def test_returns_none_on_error(self, monkeypatch):
        import subprocess
        import sys

        import kiro_crew.acp.client as client_mod
        from kiro_crew.acp.client import _read_basename

        def raise_error(cmd, **kw):
            raise subprocess.CalledProcessError(1, cmd)

        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(client_mod.subprocess_mod, "check_output", raise_error)
        assert _read_basename(999999) is None


class TestSendPipeErrors:
    """Verify that broken pipe errors on stdin are raised as AcpProcessDied."""

    def _make_client_with_mock_process(self):
        client = AcpClient()
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        proc.returncode = None
        client._process = proc
        client._next_req_id = MagicMock(return_value=1)
        return client, proc

    @pytest.mark.asyncio
    async def test_send_request_connection_reset(self):
        client, proc = self._make_client_with_mock_process()
        proc.stdin.drain.side_effect = ConnectionResetError("Connection lost")

        with pytest.raises(AcpProcessDied, match="pipe broken"):
            await client._send_request("test/method", {})

    @pytest.mark.asyncio
    async def test_send_request_broken_pipe(self):
        client, proc = self._make_client_with_mock_process()
        proc.stdin.drain.side_effect = BrokenPipeError("Broken pipe")

        with pytest.raises(AcpProcessDied, match="pipe broken"):
            await client._send_request("test/method", {})

    @pytest.mark.asyncio
    async def test_send_response_connection_reset(self):
        client, proc = self._make_client_with_mock_process()
        proc.stdin.drain.side_effect = ConnectionResetError("Connection lost")

        with pytest.raises(AcpProcessDied, match="pipe broken"):
            await client._send_response(1, {"result": "ok"})

    @pytest.mark.asyncio
    async def test_send_response_broken_pipe(self):
        client, proc = self._make_client_with_mock_process()
        proc.stdin.drain.side_effect = BrokenPipeError("Broken pipe")

        with pytest.raises(AcpProcessDied, match="pipe broken"):
            await client._send_response(1, {"result": "ok"})

    @pytest.mark.asyncio
    async def test_send_request_success_unaffected(self):
        client, proc = self._make_client_with_mock_process()
        proc.stdin.drain.return_value = None

        req_id = await client._send_request("test/method", {"key": "val"})
        assert req_id == 1
        proc.stdin.write.assert_called_once()
        proc.stdin.drain.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_request_write_broken_pipe(self):
        client, proc = self._make_client_with_mock_process()
        proc.stdin.write.side_effect = BrokenPipeError("Broken pipe")

        with pytest.raises(AcpProcessDied, match="pipe broken"):
            await client._send_request("test/method", {})

    # ── Staleness timeout tests ──────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_stale_turn_synthesizes_complete_after_text(self):
        """When text is streamed but no complete arrives, synthesize EVENT_COMPLETE."""
        from kiro_crew.acp.types import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            STOP_REASON_END_TURN,
            UPDATE_AGENT_MESSAGE_CHUNK,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()

        text_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {"type": "text", "text": "Hello world"},
                }
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", text_msg
            # No "complete" — simulates kiro-cli going silent after text.

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert [e.kind for e in events] == [EVENT_TEXT_CHUNK, EVENT_COMPLETE]
        assert events[1].stop_reason == STOP_REASON_END_TURN
        assert events[1].synthetic_completion is True

    @pytest.mark.asyncio
    async def test_stale_eligible_cleared_by_tool_call(self):
        """Tool call after text clears _stale_eligible — no synthetic complete."""
        from kiro_crew.acp.client import AcpTimeoutError
        from kiro_crew.acp.types import (
            UPDATE_AGENT_MESSAGE_CHUNK,
            UPDATE_TOOL_CALL,
            JsonRpcMessage,
        )

        client = AcpClient()

        text_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {"type": "text", "text": "Let me check..."},
                }
            },
        )
        tool_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_TOOL_CALL,
                    "toolUseId": "tool_1",
                    "name": "Read",
                    "input": "{}",
                }
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", text_msg
            yield "update", tool_msg
            # No "complete" — tool is running, loop ends (simulates timeout).

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []

        # Should raise AcpTimeoutError because _stale_eligible was cleared by tool_call
        with pytest.raises(AcpTimeoutError):
            async for _ in client.stream_events("test"):
                pass

    @pytest.mark.asyncio
    async def test_stale_eligible_cleared_by_permission(self):
        """Permission request after text clears _stale_eligible."""
        from kiro_crew.acp.client import AcpTimeoutError
        from kiro_crew.acp.types import (
            UPDATE_AGENT_MESSAGE_CHUNK,
            JsonRpcMessage,
        )

        client = AcpClient()

        text_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {"type": "text", "text": "I need to run..."},
                }
            },
        )
        perm_msg = JsonRpcMessage(
            id=99,
            method="session/requestPermission",
            params={
                "toolName": "shell",
                "toolInput": "rm -rf /tmp/test",
                "options": [{"id": "allow_once", "label": "Allow"}],
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", text_msg
            yield "permission", perm_msg
            # No "complete" — waiting for user approval, loop ends.

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []

        # Should raise AcpTimeoutError because _stale_eligible was cleared by permission
        with pytest.raises(AcpTimeoutError):
            async for _ in client.stream_events("test"):
                pass

    @pytest.mark.asyncio
    async def test_stale_eligible_re_enabled_after_completed_tool_then_text(self):
        """Text after a completed tool permits synthetic completion."""
        from kiro_crew.acp.types import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            STOP_REASON_END_TURN,
            UPDATE_AGENT_MESSAGE_CHUNK,
            UPDATE_TOOL_CALL,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()

        text1 = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {"type": "text", "text": "Checking..."},
                }
            },
        )
        tool_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_TOOL_CALL,
                    "toolCallId": "tool_1",
                    "name": "Read",
                    "input": "{}",
                }
            },
        )
        result_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "tool_1",
                    "status": "completed",
                    "content": [{"content": {"type": "text", "text": "ok"}}],
                }
            },
        )
        text2 = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {"type": "text", "text": "Done."},
                }
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", text1
            yield "update", tool_msg
            yield "update", result_msg
            yield "update", text2
            # No "complete" — the completed tool leaves the model idle.

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        kinds = [e.kind for e in events]
        assert EVENT_TEXT_CHUNK in kinds
        assert EVENT_TOOL_CALL in kinds
        assert kinds[-1] == EVENT_COMPLETE
        assert events[-1].stop_reason == STOP_REASON_END_TURN
        assert events[-1].synthetic_completion is True

    @pytest.mark.asyncio
    async def test_passive_update_does_not_clear_stale_eligible(self):
        """Passive updates (usage_update, available_commands) after text must NOT
        reset _stale_eligible — stale detection should still fire.

        Regression: kiro-cli sends a non-text update after the final text chunk
        but never sends complete. The blanket _stale_eligible=False on every event
        disabled the 90s timeout, causing the session to hang until the 2h deadline.
        """
        from kiro_crew.acp.types import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            STOP_REASON_END_TURN,
            UPDATE_AGENT_MESSAGE_CHUNK,
            UPDATE_USAGE,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()

        text_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {"type": "text", "text": "BUILD SUCCEEDED"},
                }
            },
        )
        # Passive update after final text — must NOT reset stale
        usage_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_USAGE,
                    "used": 50000,
                    "size": 200000,
                }
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", text_msg
            yield "update", usage_msg
            # No "complete" — simulates kiro-cli going silent.

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        # Stale detection should still fire despite the passive update
        kinds = [e.kind for e in events]
        assert kinds == [
            EVENT_TEXT_CHUNK,
            EVENT_COMPLETE,
        ], f"Expected stale detection to synthesize complete after passive update, got {kinds}"
        assert events[-1].stop_reason == STOP_REASON_END_TURN
        assert events[-1].synthetic_completion is True


# ── Coverage push: process lifecycle ──


@_POSIX_ONLY
class TestKillProcess:
    """Tests for _kill_process covering SIGTERM, SIGKILL, and edge cases."""

    @pytest.mark.asyncio
    async def test_noop_when_no_process(self):
        client = AcpClient()
        client._process = None
        with patch(
            "kiro_crew.platform_compat.kill_process_tree_async", new=AsyncMock()
        ) as tree_kill:
            await client._kill_process()
        tree_kill.assert_not_awaited()
        assert client._kill_sent is None

    @pytest.mark.asyncio
    async def test_noop_when_already_exited(self):
        client = AcpClient()
        proc = MagicMock()
        proc.returncode = 0
        client._process = proc
        with patch(
            "kiro_crew.platform_compat.kill_process_tree_async", new=AsyncMock()
        ) as tree_kill:
            await client._kill_process()
        tree_kill.assert_not_awaited()
        assert client._kill_sent is None
        proc.stdin.close.assert_not_called()

    @pytest.mark.asyncio
    async def test_sigterm_success(self):
        """Normal path: SIGTERM → process exits within timeout."""
        client = AcpClient()
        proc = MagicMock()
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.stderr = None
        proc.wait = AsyncMock(return_value=0)
        client._process = proc
        client._pid = 12345
        client._child_pids = {}

        with (
            patch("os.killpg") as mock_killpg,
            patch("os.getpgid", return_value=12345),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children") as mock_esc,
        ):
            await client._kill_process()
            mock_killpg.assert_called_once_with(12345, signal.SIGTERM)
            mock_esc.assert_called_once()

    @pytest.mark.asyncio
    async def test_sigterm_timeout_then_sigkill(self):
        """SIGTERM times out → falls through to SIGKILL."""
        client = AcpClient()
        proc = MagicMock()
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.stderr = None
        # First wait times out, second succeeds
        proc.wait = AsyncMock(side_effect=[asyncio.TimeoutError(), None])
        client._process = proc
        client._pid = 99
        client._child_pids = {}

        killpg_calls = []

        def fake_killpg(pgid, sig):
            killpg_calls.append(sig)

        with (
            patch("os.killpg", side_effect=fake_killpg),
            patch("os.getpgid", return_value=99),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
        ):
            await client._kill_process()

        assert signal.SIGTERM in killpg_calls
        assert signal.SIGKILL in killpg_calls

    @pytest.mark.asyncio
    async def test_force_skips_sigterm(self):
        """force=True goes straight to SIGKILL."""
        client = AcpClient()
        proc = MagicMock()
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.stderr = None
        proc.wait = AsyncMock(return_value=0)
        proc.kill = MagicMock()
        client._process = proc
        client._pid = 55
        client._child_pids = {}

        killpg_sigs = []

        def fake_killpg(pgid, sig):
            killpg_sigs.append(sig)

        with (
            patch("os.killpg", side_effect=fake_killpg),
            patch("os.getpgid", return_value=55),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
        ):
            await client._kill_process(force=True)

        assert signal.SIGTERM not in killpg_sigs
        assert signal.SIGKILL in killpg_sigs

    @pytest.mark.asyncio
    async def test_killpg_process_lookup_error_falls_to_proc_kill(self):
        """When killpg raises ProcessLookupError, falls back to proc.kill()."""
        client = AcpClient()
        proc = MagicMock()
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.stderr = None
        proc.wait = AsyncMock(return_value=0)
        proc.kill = MagicMock()
        client._process = proc
        client._pid = 77
        client._child_pids = {}

        with (
            patch("os.killpg", side_effect=ProcessLookupError()),
            patch("os.getpgid", return_value=77),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
        ):
            await client._kill_process(force=True)

        proc.kill.assert_called_once()
        from kiro_crew import platform_compat

        assert client._kill_sent == (int(platform_compat.SIGKILL), "forced stop")

    @pytest.mark.asyncio
    async def test_kill_process_awaits_async_variant_not_sync(self):
        """_kill_process MUST await platform_compat.kill_process_tree_async
        (the offloading variant) — never fall back to the sync
        kill_process_tree, whose whole reason for existing is the Windows
        event-loop offload. A test that patches only kill_process_tree would
        silently pass if someone regresses the await back to sync, so pin
        both symbols and hard-fail if the sync one is ever called."""
        from kiro_crew import platform_compat

        client = AcpClient()
        proc = MagicMock()
        proc.returncode = None
        # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process, moved verbatim; tracked for a de-flake pass
        proc.pid = 42
        proc.stdin = proc.stdout = proc.stderr = None
        proc.wait = AsyncMock(return_value=0)
        client._process = proc
        client._pid = 42
        client._child_pids = {}

        def _sync_forbidden(*_a, **_kw):
            raise AssertionError("sync variant must NOT be called from _kill_process")

        # SIGTERM path
        with (
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async",
                new_callable=AsyncMock,
            ) as mock_async,
            patch(
                "kiro_crew.platform_compat.kill_process_tree",
                side_effect=_sync_forbidden,
            ),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
        ):
            await client._kill_process(force=False)

        assert mock_async.await_count == 1
        assert mock_async.await_args.args == (42, platform_compat.SIGTERM)

        # Reset process state for the SIGKILL path (force=True)
        proc.returncode = None
        proc.wait = AsyncMock(return_value=0)
        client._process = proc
        client._pid = 42
        client._child_pids = {}

        with (
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async",
                new_callable=AsyncMock,
            ) as mock_async_kill,
            patch(
                "kiro_crew.platform_compat.kill_process_tree",
                side_effect=_sync_forbidden,
            ),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
        ):
            await client._kill_process(force=True)

        assert mock_async_kill.await_count == 1
        assert mock_async_kill.await_args.args == (42, platform_compat.SIGKILL)


class TestResetStateExtended:
    """Extended _reset_state tests covering sandbox cleanup and PID untracking."""

    def test_sandbox_cleanup_removes_file(self, tmp_path):
        client = AcpClient()
        sb_file = tmp_path / "sandbox.sb"
        sb_file.write_text("sandbox profile")
        client._sandbox_cleanup = str(sb_file)
        client._process = None
        client._child_pids = {}
        client._pid = None

        client._reset_state()

        assert not sb_file.exists()
        assert client._sandbox_cleanup is None

    def test_sandbox_cleanup_missing_file_no_error(self, tmp_path):
        client = AcpClient()
        # The missing file lives under tmp_path: the remove() this exercises is a
        # real syscall against whatever path is here, and an absolute host path
        # (``/nonexistent/path.sb``) would aim it at the operator's filesystem.
        missing = tmp_path / "missing.sb"
        assert not missing.exists()
        client._sandbox_cleanup = str(missing)
        client._process = None
        client._child_pids = {}
        client._pid = None

        client._reset_state()  # should not raise
        assert client._sandbox_cleanup is None
        assert not missing.exists()

    def test_untracks_pids(self):
        client = AcpClient()
        client._process = None
        client._pid = 1234
        client._child_pids = {5678: None, 9012: None}

        with (
            patch("kiro_crew.session_pid._pid_gone_or_unmanaged", return_value=True),
            patch("kiro_crew.session._untrack_child_pids") as mock_uc,
            patch("kiro_crew.session._untrack_pid") as mock_up,
            patch("kiro_crew.session._untrack_session_pid") as mock_usp,
        ):
            client._reset_state()

        mock_uc.assert_called_once_with({5678: None, 9012: None})
        mock_up.assert_called_once_with(1234)
        mock_usp.assert_called_once_with(1234)
        assert client._child_pids == {}
        assert client._pid is None

    def test_retains_live_root_pid_survivor(self):
        """A root PID still alive after teardown MUST keep its tracking entry so
        the orphan sweep can reap it — untracking a live survivor is the
        permanent-orphan leak this guards against. In-memory state still clears.
        """
        client = AcpClient()
        client._process = None
        client._pid = 4242
        client._child_pids = {}

        with (
            patch("kiro_crew.session_pid._pid_gone_or_unmanaged", return_value=False),
            patch("kiro_crew.session._untrack_pid") as mock_up,
            patch("kiro_crew.session._untrack_session_pid") as mock_usp,
        ):
            client._reset_state()

        mock_up.assert_not_called()
        mock_usp.assert_not_called()
        assert client._pid is None  # in-memory state is still cleared

    def test_untracks_dead_children_retains_live(self):
        """Dead children are untracked; a live child is retained (partial-reap:
        killpg missed a child in another process group)."""
        client = AcpClient()
        client._process = None
        client._pid = None
        client._child_pids = {5678: None, 9012: None}

        def _gone(pid):
            return pid == 5678  # 5678 dead -> untrack; 9012 alive -> retain

        with (
            patch("kiro_crew.session_pid._pid_gone_or_unmanaged", side_effect=_gone),
            patch("kiro_crew.session._untrack_child_pids") as mock_uc,
        ):
            client._reset_state()

        mock_uc.assert_called_once_with({5678: None})

    def test_cancels_stderr_task(self):
        client = AcpClient()
        client._process = None
        client._pid = None
        client._child_pids = {}
        mock_task = MagicMock()
        mock_task.done.return_value = False
        client._stderr_task = mock_task

        client._reset_state()

        mock_task.cancel.assert_called_once()
        assert client._stderr_task is None


@_POSIX_ONLY
class TestKillProcessPipeClose:
    """Test pipe closing in _kill_process."""

    @pytest.mark.asyncio
    async def test_pipes_closed_before_kill(self):
        client = AcpClient()
        proc = MagicMock()
        proc.returncode = None
        stdin_mock = MagicMock()
        stdout_mock = MagicMock()
        stderr_mock = MagicMock()
        proc.stdin = stdin_mock
        proc.stdout = stdout_mock
        proc.stderr = stderr_mock
        proc.wait = AsyncMock(return_value=0)
        client._process = proc
        client._pid = 100
        client._child_pids = {}

        with (
            patch("os.killpg"),
            patch("os.getpgid", return_value=100),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
        ):
            await client._kill_process()

        stdin_mock.close.assert_called_once()
        stdout_mock.close.assert_called_once()
        stderr_mock.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_pipe_close_exception_ignored(self):
        client = AcpClient()
        proc = MagicMock()
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdin.close.side_effect = OSError("already closed")
        proc.stdout = MagicMock()
        proc.stderr = None
        proc.wait = AsyncMock(return_value=0)
        client._process = proc
        client._pid = 101
        client._child_pids = {}

        with (
            patch("os.killpg"),
            patch("os.getpgid", return_value=101),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
        ):
            await client._kill_process()

        # The failed stdin close neither stops the other pipes closing nor the kill.
        proc.stdout.close.assert_called_once_with()
        from kiro_crew import platform_compat

        assert client._kill_sent == (int(platform_compat.SIGTERM), "stop requested")
