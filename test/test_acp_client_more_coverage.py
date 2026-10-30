"""Additional coverage for :mod:`kiro_crew.acp.client`.

Every test in this family drives real product code with injected fakes at the boundary the
product actually calls (``glob``/``subprocess``/``kiro_sessions_dir``/the hook and
skill-observer getters), never a live subprocess, sandbox, git or network. The
POSIX-only tests are marked as such because the branches they exercise
(``os.kill``, ``/proc``, ``ps``, AF_UNIX sockets) have no Windows equivalent.

This module pins the module-level model and advisory helpers, the stdout and stderr
drains, the ``SSH_AUTH_SOCK`` repair and process-tree helpers, the client's accessors and
guards, reset, ``ensure_ready`` and the JSON-RPC transport. The frames a live session
exchanges live in ``test_acp_client_more_coverage_session_frames.py``, the skill-read,
hook, audit and advertised-model wiring in ``test_acp_client_more_coverage_observers.py``,
and the codec refusals in ``test_acp_client_more_coverage_encode_refusal.py``; the shared
client factory, notification builder and kiro-cli pin in
``acp_client_more_coverage_helpers.py``.
"""

import asyncio
import json
import logging
import os
import socket
import stat
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest
from acp_client_more_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _client,
    _notify,
    _pinned_kiro_cli_version,
)

import kiro_crew.acp.client as acp_client
from kiro_crew.acp.client import (
    AcpAuthRequired,
    AcpError,
    AcpProcessDied,
    AcpToolGateUnroutable,
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
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only APIs (os.kill, /proc, ps, AF_UNIX sockets)"
)


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

    @pytest.mark.parametrize(
        "details",
        [
            pytest.param(
                'Model "a" is restricted by your organization\'s settings. '
                "Using 'global.anthropic.sonnet' instead.",
                id="substitute_strips_surrounding_quotes",
            ),
            pytest.param(
                "Model a is restricted by your organization's settings. "
                "Using global.anthropic.sonnet, instead.",
                id="substitute_strips_trailing_punctuation",
            ),
        ],
    )
    def test_substitute_is_bare(self, details):
        err = {"code": -32603, "data": {"details": details}}
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
    # ``_resolve_ssh_auth_sock`` only ``stat``s the paths it is handed, so the
    # sockets can live under ``tmp_path`` at any length. Only ``bind()`` is
    # capped by ``sun_path`` (~104 bytes on macOS, 108 on Linux), and that cap
    # applies to the string passed to bind, not to where the file lands -- so
    # bind through a RELATIVE name with the CWD pinned to ``tmp_path`` and hand
    # production the absolute path. Nothing is written outside the sandbox.

    @staticmethod
    def _bind_under(tmp_path, monkeypatch, name: str) -> socket.socket:
        monkeypatch.chdir(tmp_path)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.bind(name)
        except OSError:
            s.close()
            raise
        # The bound endpoint landed under tmp_path, not under a host root.
        assert stat.S_ISSOCK(os.stat(tmp_path / name).st_mode)
        return s

    def test_live_socket_is_kept(self, tmp_path, monkeypatch):
        sock_path = tmp_path / "live.sock"
        with self._bind_under(tmp_path, monkeypatch, "live.sock"):
            env = {"SSH_AUTH_SOCK": str(sock_path)}
            _resolve_ssh_auth_sock(env)
        assert env["SSH_AUTH_SOCK"] == str(sock_path)

    def test_stale_pointer_is_repaired_to_newest_socket(self, tmp_path, monkeypatch):
        old, new = tmp_path / "agent.1", tmp_path / "agent.2"
        socks = [self._bind_under(tmp_path, monkeypatch, path.name) for path in (old, new)]
        try:
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
        assert seen == [
            "/tmp/com.apple.launchd.*/Listeners",
            "/var/run/com.apple.launchd.*/Listeners",
        ]
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
        # Idle == done: a live process with no prompt sent is NOT an unfinished
        # turn (see test_acp_turn_done_idle_init). A turn begins when the prompt
        # entry clear()s the Event.
        assert client.has_unfinished_turn() is False
        client._turn_done.clear()
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
        # Stale context must not be handed to the new chat.
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

    @pytest.mark.asyncio
    async def test_teardown_unlinks_claude_settings_and_survives_pipe_errors(self, tmp_path):
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

        # The pair every real caller runs: the seed's removal is a disk operation
        # (revoke the durable grant, then unlink) so it lives in the async discard,
        # while _reset_state stays synchronous and drops the in-memory claim.
        await client._discard_claude_settings_seed()
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
    async def test_tool_gate_refusal_does_not_retry_the_spawn(self, tmp_path):
        """A gate refusal is a configuration fact, so a respawn re-reads it.

        ``AcpToolGateUnroutable`` documents itself Non-retryable, but it subclasses
        ``AcpError``, so the generic transport ladder would retry it: attempt 0
        tears the child down, respawns, hits the identical refusal, and only then
        raises. That is one wasted spawn plus teardown, and it spends the reconnect
        budget the distinct type exists to protect.

        Revert-verified: dropping the dedicated handler makes both counters 2.
        """
        client = _client(tmp_path)
        spawns = {"n": 0}

        async def _spawn():
            spawns["n"] += 1
            client._process = _live_process()

        async def _init():
            raise AcpToolGateUnroutable("codex routes tool calls around the gate")

        def _reset():
            # Faithful to production: the real _reset_state drops the process
            # handle, which is what makes the retry actually RESPAWN. A bare
            # MagicMock leaves it set, so _spawn runs once either way and the
            # spawn assertion below could never fail.
            client._process = None

        client._spawn = _spawn
        client._initialize_session = _init
        client._snapshot_process_tree = AsyncMock()
        client._kill_process = AsyncMock()
        client._reset_state = _reset

        with pytest.raises(AcpToolGateUnroutable):
            await client.ensure_ready()

        assert spawns["n"] == 1, "the refusal was retried with a fresh process"
        assert client._kill_process.await_count == 1

    def test_sandbox_preflight_translates_the_gate_refusal(self, monkeypatch):
        """The RAW gate exception must not escape the preflight.

        ``acp_tool_gate`` is a leaf module that cannot import this one, so its
        ``ToolGateUnroutable`` is a plain ``Exception``. That makes it invisible to
        BOTH handlers around the spawn: it is not an ``AcpError``, so the transport
        ladder cannot see it, and it is not ``AcpToolGateUnroutable``, so the
        dedicated non-retrying handler cannot either. Raised raw, a sandbox-floor
        refusal escaped ``ensure_ready`` entirely and skipped the cleanup every
        other refusal path runs.

        Revert-verified: dropping the translation raises the raw type and fails here.
        """
        from kiro_crew import acp_tool_gate

        def _refuse(backend, mode):
            raise acp_tool_gate.ToolGateUnroutable("no sandbox backend on this host")

        monkeypatch.setattr(acp_tool_gate, "enforce_sandbox_floor", _refuse)

        with pytest.raises(AcpToolGateUnroutable, match="no sandbox backend"):
            acp_client._sandbox_preflight("codex", "standard")

    @pytest.mark.asyncio
    async def test_sandbox_preflight_is_bounded_on_a_stalled_disk(self, monkeypatch):
        """A preflight that never returns must not hold the spawn open.

        The mask half canonicalizes the home and override roots on disk, and on a
        stalled mount that wait has no end of its own; nothing else on the spawn
        path bounds it (``ensure_ready`` times the handshake AFTER the spawn). The
        deadline turns that into a retryable ``AcpError`` naming the slow disk, and
        the adapter is not started without its mask.

        Revert-verified: dropping the ``wait_for`` makes this test hang on the
        stalled worker instead of raising.
        """
        import threading

        monkeypatch.setattr(acp_client, "_SANDBOX_PREFLIGHT_TIMEOUT", 0.05)
        release = threading.Event()

        def _stalled(backend, mode):
            release.wait(5.0)
            return ()

        try:
            with pytest.raises(AcpError, match="did not finish within 0 s"):
                await acp_client._run_preflight_bounded(_stalled, "codex", "standard")
        finally:
            release.set()  # let the worker thread go; the test must not leak it

    @pytest.mark.asyncio
    async def test_sandbox_preflight_within_budget_returns_the_mask(self):
        calls = []

        def _quick(backend, mode):
            calls.append((backend, mode))
            return ("/home/u/.aws",)

        assert await acp_client._run_preflight_bounded(_quick, "codex", "standard") == (
            "/home/u/.aws",
        )
        assert calls == [("codex", "standard")]

    @pytest.mark.asyncio
    async def test_shutdown_kills_and_resets(self, tmp_path):
        client = _client(tmp_path)
        order: list[str] = []
        client._kill_process = AsyncMock(side_effect=lambda **kw: order.append(f"kill {kw}"))
        client._reset_state = MagicMock(side_effect=lambda: order.append("reset"))
        client._process_tree_confirmed_dead = True

        await client.shutdown()

        assert order == ["kill {'force': True}", "reset"]
        # A stale confirmation from an earlier kill does not survive into this one.
        assert client._process_tree_confirmed_dead is False


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
