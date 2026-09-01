"""Additional coverage for :mod:`kiro_crew.acp.client`.

Every test here drives real product code with injected fakes at the boundary the
product actually calls (``glob``/``subprocess``/``kiro_sessions_dir``/the hook and
skill-observer getters), never a live subprocess, sandbox, git or network. The
POSIX-only tests are marked as such because the branches they exercise
(``os.kill``, ``/proc``, ``ps``, AF_UNIX sockets) have no Windows equivalent.
"""

import asyncio
import json
import logging
import os
import socket
import stat
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import kiro_crew.acp.client as acp_client
from kiro_crew.acp.client import (
    AcpAuthRequired,
    AcpClient,
    AcpError,
    AcpProcessDied,
    AcpTimeoutError,
    OversizeLineUnrecoverable,
    _direct_children,
    _drain_oversize_line,
    _extract_advisory_detail,
    _format_acp_error,
    _get_child_pids,
    _get_start_time,
    _is_transient_raw_error,
    _read_basename,
    _rejected_model_from_error,
    _resolve_ssh_auth_sock,
    _substitute_model_from_advisory,
    advertised_model_ids,
    resolve_usable_model,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    EVENT_AGENT_SWITCHED,
    EVENT_COMPLETE,
    EVENT_MCP_OAUTH_REQUEST,
    EVENT_MCP_SERVER_INIT_FAILURE,
    EVENT_MCP_SERVER_INITIALIZED,
    EVENT_PERMISSION_REQUEST,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    EVENT_TOOL_RESULT,
    JSONRPC_METHOD_NOT_FOUND,
    METHOD_COMMANDS_EXECUTE,
    UPDATE_AGENT_THOUGHT_CHUNK,
    UPDATE_TOOL_CALL,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.hooks import HOOK_EVENT_POST_TOOL_USE

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only APIs (os.kill, /proc, ps, AF_UNIX sockets)"
)


def _client(tmp_path: Path, **kwargs) -> AcpClient:
    """An AcpClient pinned to *tmp_path* so nothing is written outside it."""
    return AcpClient(work_dir=tmp_path, **kwargs)


def _live_process() -> MagicMock:
    """A mock subprocess that looks alive with a writable, drainable stdin."""
    proc = MagicMock()
    proc.returncode = None
    proc.stdin = MagicMock()
    proc.stdin.drain = AsyncMock()
    return proc


# ── Module-level model helpers ──


class TestAdvertisedModelIds:
    def test_extracts_ids_from_both_shapes(self):
        entries = [
            {"modelId": "opus"},
            {"value": "sonnet"},
            {"modelId": "   "},  # blank -> dropped
            {"name": "no id"},  # no id key -> dropped
            {"modelId": 7},  # non-str -> dropped
            "not-a-dict",
        ]
        assert advertised_model_ids(entries) == ["opus", "sonnet"]

    def test_tuple_is_accepted(self):
        assert advertised_model_ids(({"modelId": "a"},)) == ["a"]

    def test_non_sequence_degrades_to_empty(self):
        # A surprising payload must degrade to "entitlement unknown", not raise.
        assert advertised_model_ids({"modelId": "a"}) == []
        assert advertised_model_ids(None) == []


class TestResolveUsableModel:
    def test_empty_preferred_inherits_default(self):
        assert resolve_usable_model("", ["opus"]) == ""

    def test_unknown_advertised_withholds_auto_but_trusts_concrete(self):
        assert resolve_usable_model("auto", None) == ""
        assert resolve_usable_model("auto", []) == ""
        assert resolve_usable_model("opus", None) == "opus"

    def test_auto_only_sent_when_advertised(self):
        assert resolve_usable_model("auto", ["auto", "opus"]) == "auto"
        assert resolve_usable_model("auto", ["opus"]) == ""

    def test_concrete_model_kept_when_served_else_inherits(self):
        assert resolve_usable_model("opus", ["opus", "sonnet"]) == "opus"
        assert resolve_usable_model("haiku", ["opus", "sonnet"]) == ""


class TestUsageLimitIsTerminal:
    _ERR = {
        "code": -32603,
        "message": "Internal error",
        "data": (
            "Encountered an error in the response stream: You have reached your "
            "monthly limit for this model (request_id: abc-123)"
        ),
    }

    def test_not_retryable(self):
        # Ahead of the throttle branch: a spent allowance does not clear on retry.
        assert _is_transient_raw_error(self._ERR) is False

    def test_message_quotes_provider_and_adds_period(self):
        out = _format_acp_error(self._ERR)
        assert "monthly limit for this model." in out
        assert "Retrying will not help until the limit resets." in out
        assert "(request_id: abc-123)" in out
        # The envelope prefix and the duplicated request_id are stripped.
        assert "Encountered an error in the response stream" not in out
        assert out.count("abc-123") == 1


class TestRejectedModelFromError:
    def test_non_dict_is_none(self):
        assert _rejected_model_from_error("boom") is None

    def test_invalid_model_id_wins(self):
        assert _rejected_model_from_error({"data": "Invalid model ID: auto"}) == "auto"

    def test_unavailable_wording_is_matched(self):
        err = {"message": "", "data": "The model 'opus-9' is not available"}
        assert _rejected_model_from_error(err) == "opus-9"

    def test_error_naming_no_model_is_none(self):
        assert _rejected_model_from_error({"data": "ThrottlingException"}) is None


class TestAdvisoryHelpers:
    def test_detail_of_non_dict_is_empty(self):
        assert _extract_advisory_detail(["not", "a", "dict"]) == ""

    def test_plain_string_data_is_the_detail(self):
        assert _extract_advisory_detail({"data": "raw detail"}) == "raw detail"

    def test_substitute_none_when_wording_absent(self):
        # A genuine advisory (code + "is restricted" + "Using X instead") is
        # required; strip the substitute clause and no model can be adopted.
        err = {"code": -32603, "data": {"details": "Model 'x' is restricted by policy."}}
        assert _substitute_model_from_advisory(err) is None

    def test_substitute_strips_surrounding_quotes(self):
        err = {
            "code": -32603,
            "data": {
                "details": (
                    'Model "a" is restricted by your organization\'s settings. '
                    "Using 'global.anthropic.sonnet' instead."
                )
            },
        }
        assert _substitute_model_from_advisory(err) == "global.anthropic.sonnet"

    def test_substitute_strips_trailing_punctuation(self):
        err = {
            "code": -32603,
            "data": {
                "details": (
                    "Model a is restricted by your organization's settings. "
                    "Using global.anthropic.sonnet, instead."
                )
            },
        }
        assert _substitute_model_from_advisory(err) == "global.anthropic.sonnet"


# ── Oversize stdout drain ──


class TestDrainOversizeLine:
    @pytest.mark.asyncio
    async def test_drains_to_the_frame_boundary(self):
        reader = asyncio.StreamReader(limit=64)
        reader.feed_data(b"X" * 500 + b"\n")
        reader.feed_data(b'{"jsonrpc":"2.0","method":"session/update"}\n')

        with pytest.raises(asyncio.LimitOverrunError) as caught:
            await reader.readuntil(b"\n")
        discarded = await _drain_oversize_line(reader, caught.value)

        assert discarded == 501  # the oversize payload plus its newline
        # The stream is left ON a frame boundary: the next frame reads intact.
        assert await reader.readuntil(b"\n") == b'{"jsonrpc":"2.0","method":"session/update"}\n'

    @pytest.mark.asyncio
    async def test_budget_exhaustion_raises(self, monkeypatch):
        monkeypatch.setattr(acp_client, "_OVERSIZE_DRAIN_MAX_BYTES", 100)
        reader = asyncio.StreamReader(limit=64)
        reader.feed_data(b"X" * 5000)  # never terminated

        with pytest.raises(asyncio.LimitOverrunError) as caught:
            await reader.readuntil(b"\n")
        with pytest.raises(OversizeLineUnrecoverable, match="no frame boundary"):
            await _drain_oversize_line(reader, caught.value)

    @pytest.mark.asyncio
    async def test_zero_length_prefix_raises_instead_of_spinning(self):
        reader = asyncio.StreamReader(limit=64)
        exc = asyncio.LimitOverrunError("separator not found", 0)
        with pytest.raises(OversizeLineUnrecoverable, match="0-byte oversize prefix"):
            await _drain_oversize_line(reader, exc)

    @pytest.mark.asyncio
    async def test_drain_survives_a_second_overrun_mid_line(self):
        """A line that overruns again while draining keeps draining, not raising."""
        reader = asyncio.StreamReader(limit=64)
        reader.feed_data(b"A" * 200)

        async def _feed_rest():
            await asyncio.sleep(0.01)
            reader.feed_data(b"B" * 200)  # second overrun of the SAME line
            await asyncio.sleep(0.01)
            reader.feed_data(b"\n")  # terminator at last

        feeder = asyncio.get_running_loop().create_task(_feed_rest())
        try:
            with pytest.raises(asyncio.LimitOverrunError) as caught:
                await reader.readuntil(b"\n")
            discarded = await _drain_oversize_line(reader, caught.value)
        finally:
            await feeder

        assert discarded == 401  # 200 + 200 payload bytes + the newline


# ── Credential-pointer repair ──


@_POSIX_ONLY
class TestResolveSshAuthSock:
    def test_live_socket_is_kept(self, short_sock_dir):
        sock_path = short_sock_dir / "live.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
            srv.bind(str(sock_path))
            env = {"SSH_AUTH_SOCK": str(sock_path)}
            _resolve_ssh_auth_sock(env)
        assert env["SSH_AUTH_SOCK"] == str(sock_path)

    def test_stale_pointer_is_repaired_to_newest_socket(
        self, tmp_path, short_sock_dir, monkeypatch
    ):
        # Bound endpoints must live under a short root (sun_path cap); the
        # "gone.sock" pointer below never binds, so it can stay on tmp_path.
        old, new = short_sock_dir / "agent.1", short_sock_dir / "agent.2"
        socks = []
        for path in (old, new):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.bind(str(path))
            socks.append(s)
        try:
            assert stat.S_ISSOCK(os.stat(old).st_mode)
            os.utime(old, (1_000_000, 1_000_000))
            os.utime(new, (2_000_000, 2_000_000))
            monkeypatch.setattr(
                acp_client,
                "glob",
                types.SimpleNamespace(glob=lambda pattern: [str(old), str(new)]),
            )
            env = {"SSH_AUTH_SOCK": str(tmp_path / "gone.sock")}
            _resolve_ssh_auth_sock(env)
        finally:
            for s in socks:
                s.close()
        assert env["SSH_AUTH_SOCK"] == str(new)

    def test_darwin_uses_launchd_pattern(self, tmp_path, monkeypatch):
        seen: list[str] = []

        def _fake_glob(pattern):
            seen.append(pattern)
            return []

        monkeypatch.setattr(acp_client.sys, "platform", "darwin")
        monkeypatch.setattr(acp_client, "glob", types.SimpleNamespace(glob=_fake_glob))
        env: dict[str, str] = {}
        _resolve_ssh_auth_sock(env)
        assert seen == ["/tmp/com.apple.launchd.*/Listeners"]
        assert "SSH_AUTH_SOCK" not in env

    def test_windows_is_a_noop(self, monkeypatch):
        # Bare os.getuid() below would AttributeError on win32, so the guard
        # must return before any resolution work happens.
        monkeypatch.setattr(acp_client.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(
            acp_client,
            "glob",
            types.SimpleNamespace(glob=lambda p: pytest.fail("glob must not run on win32")),
        )
        env = {"SSH_AUTH_SOCK": "/nonexistent"}
        _resolve_ssh_auth_sock(env)
        assert env == {"SSH_AUTH_SOCK": "/nonexistent"}


# ── Process-tree helpers ──


@_POSIX_ONLY
class TestProcessTreeHelpers:
    def test_pid_cycle_does_not_recurse_forever(self, monkeypatch):
        tree = {10: [11], 11: [10]}
        monkeypatch.setattr(acp_client, "_direct_children", lambda pid: tree.get(pid, []))
        assert _get_child_pids(10) == [11]

    def test_visited_pid_returns_empty(self):
        assert _get_child_pids(10, {10}) == []

    def test_direct_children_falls_back_to_pgrep(self, monkeypatch):
        calls: list[list[str]] = []

        def _fake_check_output(cmd, **kwargs):
            calls.append(cmd)
            return b"4242 4243\n"

        monkeypatch.setattr(acp_client.subprocess_mod, "check_output", _fake_check_output)
        # A PID with no /proc entry falls through the Linux fast path to pgrep.
        assert _direct_children(2**30) == [4242, 4243]
        assert calls and calls[0][:2] == ["pgrep", "-P"]

    def test_direct_children_swallows_pgrep_failure(self, monkeypatch):
        def _boom(cmd, **kwargs):
            raise OSError("no pgrep")

        monkeypatch.setattr(acp_client.subprocess_mod, "check_output", _boom)
        assert _direct_children(2**30) == []

    def test_start_time_on_darwin_hashes_ps_output(self, monkeypatch):
        monkeypatch.setattr(acp_client.sys, "platform", "darwin")
        monkeypatch.setattr(
            acp_client.platform_compat, "trusted_system_bin", lambda name: "/bin/ps"
        )
        monkeypatch.setattr(
            acp_client.subprocess_mod, "check_output", lambda cmd, **kw: b" Wed Aug 12 10:00 \n"
        )
        assert _get_start_time(999) == hash(b"Wed Aug 12 10:00")

    def test_start_time_none_without_trusted_ps(self, monkeypatch):
        monkeypatch.setattr(acp_client.sys, "platform", "darwin")
        monkeypatch.setattr(acp_client.platform_compat, "trusted_system_bin", lambda name: None)
        monkeypatch.setattr(
            acp_client.subprocess_mod,
            "check_output",
            lambda *a, **k: pytest.fail("ps must not be spawned without a trusted binary"),
        )
        assert _get_start_time(999) is None

    def test_basename_none_without_trusted_ps(self, monkeypatch):
        monkeypatch.setattr(acp_client.sys, "platform", "darwin")
        monkeypatch.setattr(acp_client.platform_compat, "trusted_system_bin", lambda name: None)
        assert _read_basename(999) is None

    def test_basename_none_when_ps_reports_nothing(self, monkeypatch):
        monkeypatch.setattr(acp_client.sys, "platform", "darwin")
        monkeypatch.setattr(
            acp_client.platform_compat, "trusted_system_bin", lambda name: "/bin/ps"
        )
        monkeypatch.setattr(acp_client.subprocess_mod, "check_output", lambda cmd, **kw: b"   \n")
        assert _read_basename(999) is None


# ── Small client accessors ──


class TestClientAccessors:
    def test_process_state_accessors(self, tmp_path):
        client = _client(tmp_path)
        assert client.is_process_alive() is False
        assert client.exit_code is None
        assert client.resumed is False
        assert client.has_unfinished_turn() is False

        client._process = _live_process()
        assert client.is_process_alive() is True
        assert client.exit_code is None
        assert client.has_unfinished_turn() is True  # turn not done + process alive

        client._process.returncode = 3
        assert client.is_process_alive() is False
        assert client.exit_code == 3
        assert client.has_unfinished_turn() is False

    def test_set_resume_session_id(self, tmp_path):
        client = _client(tmp_path)
        client.set_resume_session_id("sid-1")
        assert client._resume_session_id == "sid-1"

    def test_supports_steer_is_backend_dependent(self, tmp_path):
        assert _client(tmp_path).supports_steer is True
        assert _client(tmp_path, acp_backend=ACP_BACKEND_CLAUDE).supports_steer is False

    def test_rekey_rebinds_and_drops_previous_context(self, tmp_path):
        client = _client(tmp_path, session_key="old", channel_id="C-old")
        client.last_prompt_stats.context_pct = 73.0
        client.last_prompt_stats.context_used_tokens = 400
        client.last_prompt_stats.context_window_tokens = 1000
        client.last_prompt_stats.context_tokens_from_usage = True

        client.rekey("new", "C-new")

        assert client._session_key == "new"
        assert client._channel_id == "C-new"
        # Stale context must not be handed to the new chat (#2932).
        assert client.last_prompt_stats.context_pct == 0.0
        assert client.last_prompt_stats.context_used_tokens == 0
        assert client.last_prompt_stats.context_window_tokens == 0
        assert client.last_prompt_stats.context_tokens_from_usage is False

    def test_is_responsive_needs_a_live_process_and_recent_io(self, tmp_path):
        client = _client(tmp_path)
        assert client.is_responsive() is False  # no process at all

        client._process = _live_process()
        client._last_activity = 0.0
        assert client.is_responsive(stale_threshold=1.0) is False

        client.touch_activity()
        assert client.is_responsive(stale_threshold=1.0) is True


class TestModelAndConfigGuards:
    @pytest.mark.asyncio
    async def test_set_model_before_session_is_refused(self, tmp_path):
        client = _client(tmp_path)
        with pytest.raises(AcpError, match="before session is initialized"):
            await client.set_model("opus")

    @pytest.mark.asyncio
    async def test_set_config_option_before_session_is_refused(self, tmp_path):
        client = _client(tmp_path)
        with pytest.raises(AcpError, match="before session is initialized"):
            await client.set_config_option("effort", "high")

    @pytest.mark.asyncio
    async def test_claude_set_model_routes_through_set_config_option(self, tmp_path):
        client = _client(tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        client._session_id = "sid"
        client.set_config_option = AsyncMock()
        # Claude ids live in a different namespace than the advertised list, so
        # the entitlement pre-check must NOT run on this backend.
        client._available_models = [{"modelId": "claude-opus-4-8"}]

        await client.set_model("global.anthropic.claude-sonnet-4-6")

        client.set_config_option.assert_awaited_once_with(
            "model", "global.anthropic.claude-sonnet-4-6"
        )
        assert client._model == "global.anthropic.claude-sonnet-4-6"
        assert client._resolved_model_id == "global.anthropic.claude-sonnet-4-6"

    def test_capture_available_models_tolerates_odd_payloads(self, tmp_path):
        client = _client(tmp_path)

        client._capture_available_models({"models": "nope"})
        assert client.available_models() == []

        client._capture_available_models({"models": {"availableModels": "nope"}})
        assert client.available_models() == []

        client._capture_available_models(
            {
                "models": {
                    "currentModelId": "opus",
                    "availableModels": [
                        "not-a-dict",
                        {"name": "no id"},
                        {"value": "sonnet", "description": "fast"},
                    ],
                }
            }
        )
        assert client._resolved_model_id == "opus"
        assert client.available_models() == [
            {"modelId": "sonnet", "name": "sonnet", "description": "fast"}
        ]

    def test_config_option_update_replaces_the_option_set(self, tmp_path):
        client = _client(tmp_path)
        msg = _notify(
            "session/update",
            {
                "update": {
                    "sessionUpdate": "config_option_update",
                    # No "effort" entry, so no effort levels are published.
                    "configOptions": [{"id": "mode", "options": [{"value": "plan"}]}],
                }
            },
        )

        client._track_usage_update(msg)

        assert client.acp_config_options == [{"id": "mode", "options": [{"value": "plan"}]}]
        assert client.get_valid_effort_levels() == []
        assert client.supports_config_option("mode") is True
        assert client.supports_config_option("effort") is False

    def test_unknown_claude_session_update_is_ignored(self, tmp_path):
        client = _client(tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        msg = _notify("session/update", {"update": {"sessionUpdate": "brand_new_thing"}})

        client._track_usage_update(msg)  # must not raise

        assert client.acp_config_options == []


# ── stderr drain ──


class TestDrainStderr:
    @pytest.mark.asyncio
    async def test_suppressed_markers_are_dropped_but_keep_liveness(self, tmp_path, monkeypatch):
        # Interval 0 so the throttled summary fires within the test.
        monkeypatch.setattr(acp_client, "_SUPPRESSED_STDERR_SUMMARY_INTERVAL_SECS", 0.0)
        client = _client(tmp_path)
        client._last_activity = 0.0

        reader = asyncio.StreamReader()
        reader.feed_data(b"\n")  # blank -> skipped entirely
        reader.feed_data(b'Unexpected case: {"thinking_tokens": 3}\n')
        reader.feed_data(b"real failure\n")
        reader.feed_eof()

        await client._drain_stderr(reader)

        # Only the genuine error survives in the bounded diagnostics buffer.
        assert list(client._stderr_lines) == ["real failure"]
        assert client._last_activity > 0.0

    @pytest.mark.asyncio
    async def test_residual_suppressed_count_is_flushed_at_eof(self, tmp_path, caplog):
        client = _client(tmp_path)
        reader = asyncio.StreamReader()
        reader.feed_data(b"thinking_tokens delta\n")
        reader.feed_eof()

        with caplog.at_level(logging.DEBUG, logger=acp_client.logger.name):
            await client._drain_stderr(reader)

        assert not client._stderr_lines
        assert any("suppressed 1 adapter stderr marker" in r.getMessage() for r in caplog.records)


# ── Liveness / reset ──


class TestResetPaths:
    @pytest.mark.asyncio
    async def test_retire_consumes_a_finished_consult_exception(self, tmp_path):
        client = _client(tmp_path)
        loop = asyncio.get_running_loop()
        done = loop.create_future()
        done.set_exception(RuntimeError("proc walk blew up"))
        client._consult_future = done
        previous_oracle = client._liveness_oracle

        client._retire_liveness_state()

        assert client._consult_future is None
        assert client._liveness_oracle is not previous_oracle
        # The exception was retrieved, so asyncio will not report it at GC.
        assert done.exception() is not None

    def test_reset_state_unlinks_claude_settings_and_survives_pipe_errors(self, tmp_path):
        client = _client(
            tmp_path, acp_backend=ACP_BACKEND_CLAUDE, permission_mode="bypassPermissions"
        )
        # Written the way a real session writes it, because the cleanup is now
        # scoped to what this session seeded: a settings.local.json Crew never
        # touched belongs to the user's project and is left alone.
        client._write_claude_local_settings()
        stale = tmp_path / ".claude" / "settings.local.json"
        assert stale.exists()

        proc = MagicMock()
        proc.stdin.close.side_effect = OSError("already closed")
        client._process = proc
        client._pid = None
        client._child_pids = {}

        client._reset_state()

        assert not stale.exists()  # bypassPermissions must not persist a crash
        assert client._process is None
        assert client._session_id is None


# ── ensure_ready ──


class TestEnsureReady:
    @pytest.mark.asyncio
    async def test_retries_once_with_a_fresh_process(self, tmp_path):
        client = _client(tmp_path)
        attempts = {"init": 0}

        async def _spawn():
            client._process = _live_process()

        async def _init():
            attempts["init"] += 1
            if attempts["init"] == 1:
                raise AcpError("MCP server crashed")
            client._session_id = "sid"

        async def _snapshot():
            raise RuntimeError("proc scan failed")  # must not abort startup

        def _reset():
            client._process = None

        client._spawn = _spawn
        client._initialize_session = _init
        client._snapshot_process_tree = _snapshot
        client._kill_process = AsyncMock()
        client._reset_state = _reset

        await client.ensure_ready()

        assert attempts["init"] == 2
        assert client._session_id == "sid"
        assert client._kill_process.await_count == 1

    @pytest.mark.asyncio
    async def test_auth_required_propagates_after_the_retry(self, tmp_path):
        client = _client(tmp_path)

        async def _spawn():
            client._process = _live_process()

        async def _init():
            raise AcpAuthRequired("kiro-cli is not logged in")

        client._spawn = _spawn
        client._initialize_session = _init
        client._snapshot_process_tree = AsyncMock()
        client._kill_process = AsyncMock()
        client._reset_state = MagicMock()

        with pytest.raises(AcpAuthRequired):
            await client.ensure_ready()

        assert client._kill_process.await_count == 2  # once per attempt

    @pytest.mark.asyncio
    async def test_shutdown_kills_and_resets(self, tmp_path):
        client = _client(tmp_path)
        client._kill_process = AsyncMock()
        client._reset_state = MagicMock()

        await client.shutdown()

        client._kill_process.assert_awaited_once_with(force=True)
        client._reset_state.assert_called_once()


# ── JSON-RPC transport ──


class TestTransport:
    @pytest.mark.asyncio
    async def test_send_response_requires_a_process(self, tmp_path):
        client = _client(tmp_path)
        with pytest.raises(AcpError, match="not running"):
            await client._send_response(1, {"ok": True})

    @pytest.mark.asyncio
    async def test_send_response_writes_the_selected_outcome(self, tmp_path):
        client = _client(tmp_path)
        client._process = _live_process()
        client._last_activity = 0.0

        await client._send_response("req-1", {"outcome": {"outcome": "selected"}})

        written = client._process.stdin.write.call_args[0][0].decode()
        assert json.loads(written) == {
            "jsonrpc": "2.0",
            "id": "req-1",
            "result": {"outcome": {"outcome": "selected"}},
        }
        assert client._last_activity > 0.0

    @pytest.mark.asyncio
    async def test_send_prompt_ships_prompt_blocks(self, tmp_path):
        client = _client(tmp_path)
        client._session_id = "sid"
        client._send_request = AsyncMock(return_value=3)

        assert await client._send_prompt("hello there") == 3

        method, params = client._send_request.await_args[0]
        assert method == acp_client.METHOD_PROMPT
        assert params["sessionId"] == "sid"
        assert any(
            block.get("type") == "text" and "hello there" in block.get("text", "")
            for block in params["prompt"]
        )

    @pytest.mark.asyncio
    async def test_send_error_requires_a_process(self, tmp_path):
        client = _client(tmp_path)
        with pytest.raises(AcpError, match="not running"):
            await client._send_error(1, -32601, "Method not found")

    @pytest.mark.asyncio
    async def test_send_error_writes_a_jsonrpc_error_frame(self, tmp_path):
        client = _client(tmp_path)
        client._process = _live_process()

        await client._send_error("req-9", -32601, "Method not found: fs/read_text_file")

        written = client._process.stdin.write.call_args[0][0].decode()
        assert json.loads(written) == {
            "jsonrpc": "2.0",
            "id": "req-9",
            "error": {"code": -32601, "message": "Method not found: fs/read_text_file"},
        }

    @pytest.mark.asyncio
    async def test_send_error_reports_a_broken_pipe_as_process_death(self, tmp_path):
        client = _client(tmp_path)
        client._process = _live_process()
        client._process.stdin.drain = AsyncMock(side_effect=BrokenPipeError("gone"))

        with pytest.raises(AcpProcessDied, match="pipe broken"):
            await client._send_error(1, -32601, "Method not found")

    @pytest.mark.asyncio
    async def test_eof_on_dead_process_reports_redacted_stderr_tail(self, tmp_path):
        client = _client(tmp_path)
        proc = MagicMock()
        proc.returncode = 42
        proc.stdout = AsyncMock()
        proc.stdout.readline = AsyncMock(return_value=b"")
        client._process = proc
        client._stderr_lines.append("boom: aws_secret_access_key=AKIAIOSFODNN7EXAMPLE1234")

        async def _fault():
            # Still pending when _read_message checks, so the EOF path awaits it
            # under its own 0.5s budget and absorbs the failure.
            await asyncio.sleep(0.01)
            raise RuntimeError("stderr drain died")

        client._stderr_task = asyncio.get_running_loop().create_task(_fault())

        with pytest.raises(AcpError) as caught:
            await client._read_message(timeout=1.0)

        assert "exited (code=42)" in str(caught.value)
        assert "AKIAIOSFODNN7EXAMPLE1234" not in str(caught.value)


# ── _dispatch_events: mid-session MCP notifications ──


def _fake_loop(frames):
    """A _prompt_loop stand-in yielding pre-classified (action, msg) frames."""

    async def _loop(req_id, timeout):
        for frame in frames:
            yield frame

    return _loop


def _notify(method, params):
    return JsonRpcMessage(method=method, params=params)


class TestDispatchMcpNotifications:
    @pytest.mark.asyncio
    async def test_oauth_and_server_lifecycle_events(self, tmp_path):
        client = _client(tmp_path)
        client._read_new_tool_results_sync = lambda: []
        client._prompt_loop = _fake_loop(
            [
                ("agent_switched", _notify("_kiro.dev/agent_switched", {"agentName": "reviewer"})),
                # Unsafe scheme: refused BEFORE dedupe is recorded.
                (
                    "mcp_oauth_request",
                    _notify("m", {"serverName": "evil", "oauthUrl": "javascript:alert(1)"}),
                ),
                # No serverName: cannot be correlated with a later init event.
                ("mcp_oauth_request", _notify("m", {"oauthUrl": "https://auth.example/1"})),
                (
                    "mcp_oauth_request",
                    _notify("m", {"serverName": "github", "oauthUrl": "https://auth.example/1"}),
                ),
                # Duplicate for the same server is dropped.
                (
                    "mcp_oauth_request",
                    _notify("m", {"serverName": "github", "oauthUrl": "https://auth.example/1"}),
                ),
                ("mcp_server_initialized", _notify("m", {"serverName": "github"})),
                # Dedupe was cleared by the init, so a later retry surfaces again.
                (
                    "mcp_oauth_request",
                    _notify("m", {"serverName": "github", "oauthUrl": "https://auth.example/2"}),
                ),
                (
                    "mcp_server_init_failure",
                    _notify("m", {"serverName": "github", "error": "token expired"}),
                ),
                ("mcp_server_initialized", _notify("m", {"name": ""})),  # nameless -> no event
                ("complete", JsonRpcMessage(id=1, result={"stopReason": "end_turn"})),
            ]
        )

        events = [ev async for ev in client._dispatch_events(1, 5.0)]

        assert [ev.kind for ev in events] == [
            EVENT_AGENT_SWITCHED,
            EVENT_MCP_OAUTH_REQUEST,
            EVENT_MCP_SERVER_INITIALIZED,
            EVENT_MCP_OAUTH_REQUEST,
            EVENT_MCP_SERVER_INIT_FAILURE,
            EVENT_COMPLETE,
        ]
        assert events[0].text == "reviewer"
        assert events[1].server_name == "github"
        assert events[1].oauth_url == "https://auth.example/1"
        assert events[3].oauth_url == "https://auth.example/2"
        assert events[4].text == "token expired"
        # The failure banner also clears dedupe so a retry can surface.
        assert "github" not in client._oauth_emitted_servers
        assert events[-1].stop_reason == "end_turn"

    @pytest.mark.asyncio
    async def test_unsafe_oauth_url_never_records_dedupe(self, tmp_path):
        client = _client(tmp_path)
        client._read_new_tool_results_sync = lambda: []
        client._prompt_loop = _fake_loop(
            [
                (
                    "mcp_oauth_request",
                    _notify("m", {"serverName": "srv", "oauthUrl": "data:text/html,x"}),
                ),
                (
                    "mcp_oauth_request",
                    _notify("m", {"serverName": "srv", "oauthUrl": "https://auth.example/ok"}),
                ),
                ("complete", JsonRpcMessage(id=1, result={})),
            ]
        )

        events = [ev async for ev in client._dispatch_events(1, 5.0)]

        assert [ev.kind for ev in events] == [EVENT_MCP_OAUTH_REQUEST, EVENT_COMPLETE]
        assert events[0].oauth_url == "https://auth.example/ok"


# ── Commands / steer ──


class TestCommandsAndSteer:
    @pytest.mark.asyncio
    async def test_send_command_with_args_uses_the_object_form(self, tmp_path):
        client = _client(tmp_path)
        client._session_id = "sid"
        client.ensure_ready = AsyncMock()
        client._send_request = AsyncMock(return_value=11)
        client._wait_for_response = AsyncMock(return_value={"text": "effort set"})

        assert await client.send_command("/effort high", {"level": "high"}) == "effort set"

        method, payload = client._send_request.await_args[0]
        assert method == METHOD_COMMANDS_EXECUTE
        assert payload == {
            "sessionId": "sid",
            "command": {"command": "effort", "args": {"level": "high"}},
        }

    @pytest.mark.asyncio
    async def test_command_result_preserves_structured_data(self, tmp_path):
        client = _client(tmp_path)
        client._session_id = "sid"
        client.ensure_ready = AsyncMock()
        client._send_request = AsyncMock(return_value=11)
        expected = {
            "message": "1 MCP server configured",
            "data": {
                "servers": [{"name": "linear", "status": "running", "toolCount": 2}],
                "mode": "status",
            },
        }
        client._wait_for_response = AsyncMock(return_value=expected)

        assert await client.command_result("/mcp") == expected
        method, payload = client._send_request.await_args[0]
        assert method == METHOD_COMMANDS_EXECUTE
        assert payload == {
            "sessionId": "sid",
            "command": {"command": "mcp", "args": {}},
        }

    @pytest.mark.asyncio
    async def test_send_command_redacts_credentials_in_output(self, tmp_path):
        client = _client(tmp_path)
        client._session_id = "sid"
        client.ensure_ready = AsyncMock()
        client._send_request = AsyncMock(return_value=11)
        client._wait_for_response = AsyncMock(
            return_value={"message": "token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"}
        )

        out = await client.send_command("/usage")

        assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in out

    @pytest.mark.asyncio
    async def test_send_command_timeout_returns_empty(self, tmp_path):
        client = _client(tmp_path)
        client._session_id = "sid"
        client.ensure_ready = AsyncMock()
        client._send_request = AsyncMock(return_value=11)
        client._wait_for_response = AsyncMock(side_effect=AcpTimeoutError())

        assert await client.send_command("/compact") == ""

    @pytest.mark.asyncio
    async def test_steer_wraps_the_message(self, tmp_path):
        client = _client(tmp_path)
        client._session_id = "sid"
        client._send_request = AsyncMock(return_value=5)

        assert await client.steer("  focus on tests  ") is True

        method, params = client._send_request.await_args[0]
        assert method == "_session/steer"
        assert params["sessionId"] == "sid"
        assert params["message"] == "<user_message>\nfocus on tests\n</user_message>"

    @pytest.mark.asyncio
    async def test_steer_refuses_without_text_or_session(self, tmp_path):
        client = _client(tmp_path)
        client._send_request = AsyncMock()

        client._session_id = "sid"
        assert await client.steer("   ") is False
        client._session_id = None
        assert await client.steer("hello") is False
        client._send_request.assert_not_awaited()


# ── send_message read path ──


class TestReadPromptResponse:
    @pytest.mark.asyncio
    async def test_metadata_and_compaction_frames_are_applied(self, tmp_path):
        client = _client(tmp_path)
        client._send_error = AsyncMock()
        client._prompt_loop = _fake_loop(
            [
                (
                    "server_request_unknown",
                    JsonRpcMessage(id="s-1", method="fs/read_text_file", params={}),
                ),
                (
                    "compaction",
                    _notify("_kiro.dev/compaction/status", {"status": {"type": "failed"}}),
                ),
                ("metadata", _notify("_kiro.dev/metadata", {"contextUsagePercentage": 42.5})),
                ("complete", JsonRpcMessage(id=1, result={"stopReason": "end_turn"})),
            ]
        )

        assert await client._read_prompt_response(1, 5.0) == ""
        # An unhandled inbound request is answered so the agent fails fast.
        client._send_error.assert_awaited_once_with(
            "s-1", JSONRPC_METHOD_NOT_FOUND, "Method not found: fs/read_text_file"
        )
        assert client.last_prompt_stats.context_pct == 42.5
        assert client._last_stop_reason == "end_turn"
        assert client._turn_done.is_set()


# ── Chunk / tool extraction ──


class TestExtractHelpers:
    def test_thought_chunk_is_flagged_as_thinking(self, tmp_path):
        client = _client(tmp_path)
        msg = _notify(
            "session/update",
            {
                "update": {
                    "sessionUpdate": UPDATE_AGENT_THOUGHT_CHUNK,
                    "content": {"text": "weighing options"},
                }
            },
        )
        assert client._extract_text_chunk(msg) == ("weighing options", True)

    def test_thought_chunk_with_malformed_content_degrades(self, tmp_path):
        client = _client(tmp_path)
        msg = _notify(
            "session/update",
            {"update": {"sessionUpdate": UPDATE_AGENT_THOUGHT_CHUNK, "content": "oops"}},
        )
        assert client._extract_text_chunk(msg) == (None, True)

    def test_tool_params_cache_is_capped(self, tmp_path):
        client = _client(tmp_path)
        client._tool_call_params = {
            f"old-{i}": {"path": "x"} for i in range(acp_client._MAX_CACHED_TOOL_PARAMS + 1)
        }
        msg = _notify(
            "session/update",
            {
                "update": {
                    "sessionUpdate": UPDATE_TOOL_CALL,
                    "toolCallId": "new-1",
                    "title": "read",
                    "kind": "read",
                    "rawInput": {"path": "/tmp/x"},
                }
            },
        )

        event = client._extract_tool_event(msg)

        assert event is not None and event.kind == EVENT_TOOL_CALL
        # Wholesale clear, then the fresh entry — bounded, not unbounded growth.
        assert client._tool_call_params == {"new-1": {"path": "/tmp/x"}}

    def test_tool_result_skips_non_dict_raw_output_items(self, tmp_path):
        client = _client(tmp_path)
        msg = _notify(
            "session/update",
            {
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "t1",
                    "status": "completed",
                    "rawOutput": {"items": ["junk", None, {"Text": "hello"}]},
                }
            },
        )

        event = client._extract_tool_call_update(msg)

        assert event is not None
        assert event.kind == EVENT_TOOL_RESULT
        assert event.tool_output == "hello"
        assert event.tool_final is True

    def test_refinement_accepts_a_string_raw_input(self, tmp_path):
        client = _client(tmp_path)
        msg = _notify(
            "session/update",
            {
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "t2",
                    "rawInput": "ls -la",
                }
            },
        )

        event = client._extract_tool_call_refinement(msg)

        assert event is not None and event.kind == EVENT_TOOL_CALL_UPDATE
        assert event.tool_input == "ls -la"
        assert client._tool_call_inputs["t2"] == "ls -la"

    def test_refinement_falls_back_to_repr_for_unserializable_input(self, tmp_path):
        client = _client(tmp_path)
        sentinel = object()
        msg = _notify(
            "session/update",
            {
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "t3",
                    "rawInput": {"probe": sentinel},
                }
            },
        )

        event = client._extract_tool_call_refinement(msg)

        assert event is not None
        assert "probe" in event.tool_input  # str(dict), not a crash


# ── JSONL tool results ──


class TestReadNewToolResults:
    def _write(self, tmp_path, monkeypatch, body: str) -> AcpClient:
        monkeypatch.setattr(acp_client, "kiro_sessions_dir", lambda: tmp_path)
        # newline="\n" matters even though no "\n" is visible on this line: the
        # newlines arrive inside `body`. Without it, Windows writes CRLF here
        # while a later write that DOES pass newline="\n" writes LF, so the two
        # differ in byte length and the reader's saved f.tell() offset resumes at
        # the wrong place -- the second read then returns nothing.
        (tmp_path / "sid.jsonl").write_text(body, encoding="utf-8", newline="\n")
        client = _client(tmp_path)
        client._session_id = "sid"
        return client

    def test_no_session_or_missing_file_yields_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(acp_client, "kiro_sessions_dir", lambda: tmp_path)
        client = _client(tmp_path)
        assert client._read_new_tool_results_sync() == []
        client._session_id = "sid"
        assert client._read_new_tool_results_sync() == []

    def test_parses_json_text_and_skips_malformed_lines(self, tmp_path, monkeypatch):
        lines = [
            json.dumps({"kind": "Other"}),
            "not json at all",
            "",
            json.dumps(
                {
                    "kind": "ToolResults",
                    "data": {
                        "content": [
                            {"kind": "ignored"},
                            {"kind": "toolResult", "data": "not-a-dict"},
                            {
                                "kind": "toolResult",
                                "data": {
                                    "toolUseId": "t-stdout",
                                    "content": [
                                        {"kind": "json", "data": {"stdout": "OUT"}},
                                        {"kind": "text", "data": "TAIL"},
                                        "junk-block",
                                    ],
                                },
                            },
                            {
                                "kind": "toolResult",
                                "data": {
                                    "toolUseId": "t-json",
                                    "content": [{"kind": "json", "data": {"rc": 0}}],
                                },
                            },
                            {
                                "kind": "toolResult",
                                "data": {"toolUseId": "t-empty", "content": []},
                            },
                        ]
                    },
                }
            ),
        ]
        client = self._write(tmp_path, monkeypatch, "\n".join(lines) + "\n")

        results = client._read_new_tool_results_sync()

        assert [(r.tool_call_id, r.tool_output) for r in results] == [
            ("t-stdout", "OUT\nTAIL"),
            ("t-json", json.dumps({"rc": 0}, indent=2)),
        ]
        assert all(r.kind == EVENT_TOOL_RESULT for r in results)
        # A second call sees no new lines.
        assert client._read_new_tool_results_sync() == []

    def test_partial_trailing_line_is_left_for_the_next_call(self, tmp_path, monkeypatch):
        complete = json.dumps(
            {
                "kind": "ToolResults",
                "data": {
                    "content": [
                        {
                            "kind": "toolResult",
                            "data": {
                                "toolUseId": "t1",
                                "content": [{"kind": "text", "data": "first"}],
                            },
                        }
                    ]
                },
            }
        )
        path = tmp_path / "sid.jsonl"
        client = self._write(tmp_path, monkeypatch, complete + "\n" + '{"kind": "ToolRes')

        first = client._read_new_tool_results_sync()
        assert [r.tool_call_id for r in first] == ["t1"]
        pos_after_first = client._jsonl_pos

        # Completing the partial line makes it readable without re-reading the first.
        rest = json.dumps(
            {
                "kind": "ToolResults",
                "data": {
                    "content": [
                        {
                            "kind": "toolResult",
                            "data": {
                                "toolUseId": "t2",
                                "content": [{"kind": "text", "data": "second"}],
                            },
                        }
                    ]
                },
            }
        )
        path.write_text(complete + "\n" + rest + "\n", encoding="utf-8", newline="\n")
        second = client._read_new_tool_results_sync()
        assert [r.tool_call_id for r in second] == ["t2"]
        assert client._jsonl_pos > pos_after_first

    def test_unreadable_file_is_swallowed(self, tmp_path, monkeypatch):
        client = self._write(tmp_path, monkeypatch, "")

        def _boom(*args, **kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr(acp_client, "open", _boom, raising=False)
        assert client._read_new_tool_results_sync() == []


# ── Permission events ──


class TestPermissionEvent:
    def test_non_string_label_and_kind_are_normalised(self, tmp_path):
        client = _client(tmp_path)
        msg = JsonRpcMessage(
            id="req-1",
            method="session/request_permission",
            params={
                "toolCall": {"title": "rm -rf build", "toolCallId": "tc-1", "kind": "execute"},
                "options": [
                    {"optionId": "allow_once", "name": 7, "kind": ["nope"]},
                    {"optionId": 42, "name": "bad id"},  # non-str id -> skipped
                    "not-a-dict",
                ],
            },
        )

        event = client._build_permission_event(msg)

        assert event.kind == EVENT_PERMISSION_REQUEST
        assert event.options == [{"id": "allow_once", "label": ""}]
        # Legacy id with no usable kind still resolves the allow ids.
        assert client._permission_options["req-1"]["once"] == "allow_once"
        # is_shell is deny-by-default: the payload's own kind is untrusted.
        assert event.is_shell is False

    def test_inline_params_recovered_when_input_came_from_cache(self, tmp_path):
        client = _client(tmp_path)
        client._tool_call_inputs["tc-2"] = '{"path": "/etc/hosts"}'
        msg = JsonRpcMessage(
            id="req-2",
            method="session/request_permission",
            params={
                "toolCall": {
                    "title": "write",
                    "toolCallId": "tc-2",
                    "params": {"path": "/etc/hosts"},
                },
                "options": [{"optionId": "reject", "kind": "reject_once", "name": "Reject"}],
            },
        )

        event = client._build_permission_event(msg)

        assert event.tool_input == '{"path": "/etc/hosts"}'
        assert event.raw_tool_params == {"path": "/etc/hosts"}
        assert client._permission_options["req-2"] == {"reject": "reject"}

    def test_debug_logging_redacts_the_payload(self, tmp_path, caplog):
        client = _client(tmp_path)
        msg = JsonRpcMessage(
            id="req-3",
            method="session/request_permission",
            params={
                "toolCall": {
                    "title": "curl",
                    "toolCallId": "tc-3",
                    "input": {"token": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"},
                }
            },
        )

        with caplog.at_level(logging.DEBUG, logger=acp_client.logger.name):
            event = client._build_permission_event(msg)

        debug = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)
        assert "Permission toolCall payload" in debug
        assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in debug
        assert event.raw_tool_params == {"token": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"}


# ── Context window backfill ──


class TestBackfillContextWindow:
    def test_authoritative_usage_counts_are_never_overwritten(self, tmp_path):
        client = _client(tmp_path)
        client.last_prompt_stats.context_tokens_from_usage = True
        client.last_prompt_stats.context_used_tokens = 123

        client._backfill_context_window(90.0)

        assert client.last_prompt_stats.context_used_tokens == 123

    def test_unknown_model_leaves_the_window_alone(self, tmp_path, monkeypatch):
        client = _client(tmp_path, model="mystery-model")
        monkeypatch.setattr(acp_client.model_registry, "has_known_window", lambda m: False)

        client._backfill_context_window(50.0)

        assert client.last_prompt_stats.context_window_tokens == 0
        assert client.last_prompt_stats.context_used_tokens == 0

    def test_zero_registry_window_is_refused(self, tmp_path, monkeypatch):
        client = _client(tmp_path, model="weird-model")
        monkeypatch.setattr(acp_client.model_registry, "has_known_window", lambda m: True)
        monkeypatch.setattr(acp_client.model_registry, "model_window", lambda m: 0)

        client._backfill_context_window(50.0)

        assert client.last_prompt_stats.context_window_tokens == 0

    def test_derives_used_tokens_from_a_known_window(self, tmp_path, monkeypatch):
        client = _client(tmp_path, model="known-model")
        monkeypatch.setattr(acp_client.model_registry, "has_known_window", lambda m: True)
        monkeypatch.setattr(acp_client.model_registry, "model_window", lambda m: 200_000)

        client._backfill_context_window(float("nan"))
        assert client.last_prompt_stats.context_used_tokens == 0  # NaN -> 0

        client._backfill_context_window(25.0)
        assert client.last_prompt_stats.context_window_tokens == 200_000
        assert client.last_prompt_stats.context_used_tokens == 50_000


# ── Post-compaction metadata drain ──


class TestDrainPostCompactionMetadata:
    def _reader(self, client, frames):
        queue = list(frames)

        async def _read_message(timeout=0.0):
            if not queue:
                return None
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        client._read_message = _read_message

    @pytest.mark.asyncio
    async def test_returns_on_the_first_real_percentage(self, tmp_path):
        client = _client(tmp_path)
        notification = _notify("session/update", {"update": {}})
        self._reader(
            client,
            [
                None,  # silent read
                _notify("_kiro.dev/metadata", {"meteringUsage": []}),  # no pct -> keep draining
                notification,  # buffered for the main loop
                _notify("_kiro.dev/metadata", {"contextUsagePercentage": 12.0}),
            ],
        )

        await client._drain_post_compaction_metadata(grace=5.0)

        assert client.last_prompt_stats.context_pct == 12.0
        assert client._mcp_notifications == [notification]

    @pytest.mark.asyncio
    async def test_process_death_propagates(self, tmp_path):
        client = _client(tmp_path)
        self._reader(client, [AcpError("ACP process exited (code=1)")])

        with pytest.raises(AcpError):
            await client._drain_post_compaction_metadata(grace=5.0)

    @pytest.mark.asyncio
    async def test_other_read_failures_end_the_drain_quietly(self, tmp_path):
        client = _client(tmp_path)
        self._reader(client, [RuntimeError("reader wedged")])

        await client._drain_post_compaction_metadata(grace=5.0)

        assert client.last_prompt_stats.context_pct == 0.0


# ── Skill-read telemetry ──


class _Observer:
    def __init__(self, keys=None, resolve_exc=None, credit_exc=None):
        self._keys = keys or []
        self._resolve_exc = resolve_exc
        self._credit_exc = credit_exc
        self.credited: list[list[str]] = []

    def resolve_tool_read_keys(self, tool_name, raw_params, command):
        if self._resolve_exc:
            raise self._resolve_exc
        return list(self._keys)

    def credit_skill_reads(self, keys):
        if self._credit_exc:
            raise self._credit_exc
        self.credited.append(list(keys))


def _skill_call(tool_call_id="tc-1"):
    return AcpEvent(
        kind=EVENT_TOOL_CALL,
        tool_call_id=tool_call_id,
        tool_name="fs_read",
        raw_tool_params={"path": "/skills/demo/SKILL.md"},
    )


class TestSkillReadTelemetry:
    @pytest.mark.asyncio
    async def test_noted_set_is_capped_then_credited_on_completion(self, tmp_path, monkeypatch):
        observer = _Observer(keys=["demo"])
        monkeypatch.setattr(acp_client, "get_global_skill_read_observer", lambda: observer)
        client = _client(tmp_path)
        client._skill_read_noted = {f"old-{i}" for i in range(acp_client._MAX_NOTED_SKILL_READS)}
        client._pending_skill_reads = {"old-0": ["stale"]}

        await client._maybe_note_skill_read(_skill_call())

        assert client._skill_read_noted == {"tc-1"}  # cleared wholesale, then re-seeded
        assert client._pending_skill_reads == {"tc-1": ["demo"]}

        client._maybe_credit_skill_read(
            AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id="tc-1", tool_final=True)
        )

        assert observer.credited == [["demo"]]
        assert client._pending_skill_reads == {}

    @pytest.mark.asyncio
    async def test_resolution_failure_leaves_nothing_pending(self, tmp_path, monkeypatch):
        observer = _Observer(resolve_exc=RuntimeError("skills tree unreadable"))
        monkeypatch.setattr(acp_client, "get_global_skill_read_observer", lambda: observer)
        client = _client(tmp_path)

        await client._maybe_note_skill_read(_skill_call("tc-2"))

        assert client._pending_skill_reads == {}

    def test_credit_is_skipped_without_an_observer(self, tmp_path, monkeypatch):
        client = _client(tmp_path)
        client._pending_skill_reads = {"tc-3": ["demo"]}
        monkeypatch.setattr(acp_client, "get_global_skill_read_observer", lambda: None)

        client._maybe_credit_skill_read(
            AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id="tc-3", tool_final=True)
        )

        assert client._pending_skill_reads == {}  # popped, but nothing delivered

    def test_credit_failure_is_swallowed(self, tmp_path, monkeypatch):
        observer = _Observer(credit_exc=RuntimeError("ledger locked"))
        monkeypatch.setattr(acp_client, "get_global_skill_read_observer", lambda: observer)
        client = _client(tmp_path)
        client._pending_skill_reads = {"tc-4": ["demo"]}

        client._maybe_credit_skill_read(
            AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id="tc-4", tool_final=True)
        )

        assert observer.credited == []


# ── Hook engine parity for audit-source clients ──


class TestHookFailuresAreNonFatal:
    @pytest.mark.asyncio
    async def test_pre_tool_hook_error_does_not_break_dispatch(self, tmp_path, monkeypatch):
        client = _client(tmp_path, audit_source="code-review-sage")
        monkeypatch.setattr(acp_client, "get_global_hook_store", lambda: object())
        fired: list[str] = []

        async def _boom(store, tool_name, tool_input, **kwargs):
            fired.append(tool_name)
            raise RuntimeError("hook script exploded")

        monkeypatch.setattr(acp_client, "fire_tool_hooks", _boom)

        # Must return normally — a hook failure never blocks the tool.
        await client._maybe_fire_pre_tool_hooks(
            AcpEvent(kind=EVENT_TOOL_CALL, title="ls", tool_input="{}")
        )

        assert fired == ["ls"]  # the hook really was attempted before it failed

    @pytest.mark.asyncio
    async def test_post_tool_hook_strips_the_running_prefix(self, tmp_path, monkeypatch):
        fired: list[dict] = []

        class _Store:
            async def fire(self, event, **kwargs):
                fired.append({"event": event, **kwargs})

        monkeypatch.setattr(acp_client, "get_global_hook_store", lambda: _Store())
        client = _client(tmp_path, audit_source="knowledge-pool")
        client._observed_tool_calls["tc-9"] = ("Running: ls -la", "execute")

        await client._maybe_fire_post_tool_hooks(
            AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="tc-9",
                tool_output="total 0",
                tool_final=True,
            )
        )

        assert fired[0]["tool_name"] == "ls -la"
        assert fired[0]["tool_response"] == {"output": "total 0"}

    @pytest.mark.asyncio
    async def test_post_tool_hook_error_is_swallowed(self, tmp_path, monkeypatch):
        attempts: list[str] = []

        class _Store:
            async def fire(self, event, **kwargs):
                attempts.append(event)
                raise RuntimeError("hook store down")

        monkeypatch.setattr(acp_client, "get_global_hook_store", lambda: _Store())
        client = _client(tmp_path, audit_source="knowledge-pool")

        await client._maybe_fire_post_tool_hooks(
            AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id="tc-10", tool_output="x")
        )

        assert attempts == [HOOK_EVENT_POST_TOOL_USE]


class TestToolInterruptedAudit:
    def test_sel_failure_does_not_propagate(self, tmp_path, monkeypatch, caplog):
        client = _client(tmp_path)
        client._session_id = "sid"

        def _boom():
            raise RuntimeError("SEL backend unavailable")

        monkeypatch.setattr("kiro_crew.sel.sel", _boom)

        with caplog.at_level(logging.WARNING, logger=acp_client.logger.name):
            client._emit_tool_interrupted_sel("unit-test")

        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "SEL audit failed for tool_interrupted" in messages
