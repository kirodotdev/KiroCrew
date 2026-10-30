"""MCP discovery: the stdio probe's protocol handling and process teardown.

The stdio reader skips blank lines, notifications and non-object JSON, and gives up after a
capped run of non-JSON banner lines; a malformed response or a timeout becomes an error
rather than a crash, and a child that exits early leaves a bounded, redacted stderr tail
on its error; and the teardown closes stdin, kills on timeout and reaps the probe's
process group, on Windows off the event loop and without a traceback for an already-exited
process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp_discovery_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _clear_cache,
    _passthrough_sandbox,
    _stub_remote_probe_ssrf_vet,
)
from stray_line_helpers import too_deep_line

from conftest import host_abs
from kiro_crew import platform_compat
from kiro_crew.mcp_discovery import McpServerInfo, _read_stdio_jsonrpc_response, probe_server
from kiro_crew.testing.wait import async_wait_until


class TestProbeServerProcessCleanup:
    """Tests for the finally block that tears down the probed subprocess."""

    def _make_mock_proc(self, *, wait_side_effect=None):
        proc = AsyncMock()
        proc.returncode = None  # process still running
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        proc.stdin.close = MagicMock()
        proc.stderr = MagicMock()
        proc.stderr.read = AsyncMock(return_value=b"")
        proc.kill = MagicMock()
        if wait_side_effect:
            proc.wait = AsyncMock(side_effect=wait_side_effect)
        else:
            proc.wait = AsyncMock(return_value=0)
        return proc

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "wait_side_effect,stdin_open,expect_kill",
        [
            pytest.param(None, True, False, id="graceful-stdin-close"),
            pytest.param([asyncio.TimeoutError(), 0], True, True, id="fallback-kill-on-timeout"),
            # Both the graceful wait and the forceful one fail: the error is swallowed.
            pytest.param(
                [asyncio.TimeoutError(), OSError("kill failed")],
                True,
                True,
                id="fallback-kill-also-fails",
            ),
            pytest.param(None, False, False, id="stdin-none-skips-close"),
        ],
    )
    async def test_teardown_escalates_only_past_the_graceful_wait(
        self, wait_side_effect, stdin_open, expect_kill
    ) -> None:
        proc = self._make_mock_proc(wait_side_effect=wait_side_effect)
        stdin = proc.stdin
        if not stdin_open:
            proc.stdin = None
        server = McpServerInfo(name="test", command="echo")

        with (
            patch("kiro_crew.mcp_discovery.asyncio.create_subprocess_exec", return_value=proc),
            patch("kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/echo"),
        ):
            proc.stdout = AsyncMock()
            proc.stdout.readline = AsyncMock(return_value=b"")
            result = await probe_server(server)

        # The teardown hands back the probe's own error verdict, never its own exception.
        assert (result.name, result.status) == ("test", "error")
        assert stdin.close.call_count == (1 if stdin_open else 0)
        assert proc.kill.call_count == (1 if expect_kill else 0)

    @pytest.mark.asyncio
    async def test_probe_spawns_with_the_expanded_path(self, monkeypatch) -> None:
        """The probe's child gets the same PATH the emitted config carries.

        Pinning the ``env`` kwarg is the only way to prove the probe and
        ``install_agent`` agree — a divergence here is what let a server report
        healthy on the dashboard and fail in a session.
        """
        from kiro_crew.env import spec_env_path

        # Spelled for the host (conftest.host_abs): the declared entry passes
        # through the ``os.path.isabs`` filter in env._spec_path_entries, and from
        # Python 3.13 a bare ``/opt/shims`` is not absolute under ntpath.
        shims, usr_bin = host_abs("opt", "shims"), host_abs("usr", "bin")
        monkeypatch.setenv("PATH", usr_bin)
        proc = self._make_mock_proc()
        server = McpServerInfo(name="test", command="echo", env={"PATH": shims})
        captured: dict = {}

        def _spawn(*argv, **kw):  # noqa: ANN002, ANN003 - test shim
            captured.update(kw)
            return proc

        with (
            patch("kiro_crew.mcp_discovery.asyncio.create_subprocess_exec", side_effect=_spawn),
            patch("kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/echo"),
        ):
            proc.stdout = AsyncMock()
            proc.stdout.readline = AsyncMock(return_value=b"")
            await probe_server(server)

        spawned = captured["env"]["PATH"]
        assert spawned == spec_env_path(shims)
        entries = spawned.split(os.pathsep)
        assert entries[0] == shims
        assert usr_bin in entries


class TestGetProbeTimeout:
    """Tests for the config-aware _get_probe_timeout() getter."""

    def test_get_probe_timeout_reads_config(self) -> None:
        """_get_probe_timeout() returns the config value when available."""
        from kiro_crew.mcp_discovery import _get_probe_timeout

        mock_cfg = MagicMock()
        mock_cfg.dashboard.mcp_probe_timeout_secs = 45
        mock_cls = MagicMock()
        mock_cls.load.return_value = mock_cfg

        with patch("kiro_crew.config.loader.KiroCrewConfig", mock_cls):
            result = _get_probe_timeout()
        assert result == 45

    def test_get_probe_timeout_fallback(self) -> None:
        """_get_probe_timeout() returns 15 when config is unavailable."""
        from kiro_crew.mcp_discovery import _PROBE_TIMEOUT_SECS, _get_probe_timeout

        mock_cls = MagicMock()
        mock_cls.load.side_effect = RuntimeError("no config")

        with patch("kiro_crew.config.loader.KiroCrewConfig", mock_cls):
            result = _get_probe_timeout()
        assert result == _PROBE_TIMEOUT_SECS
        assert result == 15


class TestProbeServerTimeout:
    """Tests that probe_server uses _get_probe_timeout() and handles timeout."""

    @pytest.mark.asyncio
    async def test_probe_server_timeout_on_tools_list(self) -> None:
        """probe_server times out on tools/list (second readline), covering L456."""
        server = McpServerInfo(
            name="slow-server",
            command=sys.executable,
            args=["-c", "import time; time.sleep(999)"],
        )

        init_resp = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode() + b"\n"

        mock_proc = AsyncMock()
        mock_proc.stdin = AsyncMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.close = MagicMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.readline = AsyncMock(side_effect=[init_resp, asyncio.TimeoutError])
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        mock_proc.returncode = None
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("kiro_crew.config.loader.KiroCrewConfig") as mock_cls,
        ):
            mock_cfg = MagicMock()
            mock_cfg.dashboard.mcp_probe_timeout_secs = 42
            mock_cls.load.return_value = mock_cfg

            result = await probe_server(server)

        assert result.status == "error"
        assert result.error == "timeout"

    @pytest.mark.asyncio
    async def test_probe_server_config_fallback_on_error(self) -> None:
        """probe_server falls back to 15s when config loading fails."""
        server = McpServerInfo(name="test", command=sys.executable)

        mock_proc = AsyncMock()
        mock_proc.stdin = AsyncMock()
        # StreamWriter.write/close are synchronous while drain is async; stderr.read
        # is async but returns bytes. Model each boundary explicitly so AsyncMock
        # cannot invent a coroutine-returning bytes.decode() on the error path.
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.close = MagicMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.readline = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        mock_proc.returncode = None
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("kiro_crew.config.loader.KiroCrewConfig") as mock_cls,
        ):
            mock_cls.load.side_effect = RuntimeError("corrupt config")

            result = await probe_server(server)

        assert result.status == "error"
        assert result.error == "timeout"


class TestProbeServerStderrCapture:
    """`probe_server` drains child stderr on failure and appends a
    redacted tail to `server.error` so callers (doctor, dashboard) can
    surface the real cause instead of generic 'no response'/'timeout'.
    """

    @pytest.mark.asyncio
    async def test_stderr_captured_when_child_exits_before_response(self, tmp_path) -> None:
        """Child writes to stderr and exits without speaking MCP → stderr
        tail is appended to `server.error`."""
        from kiro_crew.mcp_discovery import probe_server

        stub = tmp_path / "broken-server.sh"
        stub.write_text(
            "#!/bin/sh\n" "echo 'ModuleNotFoundError: No module named foo' >&2\n" "exit 1\n"
        )
        stub.chmod(0o755)

        server = McpServerInfo(name="broken", command=str(stub))
        with patch("kiro_crew.config.loader.KiroCrewConfig") as mock_cls:
            mock_cfg = MagicMock()
            mock_cfg.dashboard.mcp_probe_timeout_secs = 2
            mock_cls.load.return_value = mock_cfg

            result = await probe_server(server)

        assert result.status == "error"
        assert "stderr:" in (result.error or "")
        assert "ModuleNotFoundError" in (result.error or "")

    @pytest.mark.asyncio
    async def test_successful_probe_does_not_mention_stderr(self, tmp_path) -> None:
        """Healthy server's benign stderr warnings must not bleed into
        `server.error`."""
        from kiro_crew.mcp_discovery import probe_server

        stub = tmp_path / "noisy-ok-server.sh"
        stub.write_text(
            "#!/bin/sh\n"
            "echo 'WARNING: deprecated flag' >&2\n"
            "while IFS= read -r line; do\n"
            '  case "$line" in\n'
            '    *\\"initialize\\"*) '
            'printf \'{"jsonrpc":"2.0","id":1,"result":{}}\\n\' ;;\n'
            '    *\\"tools/list\\"*) '
            'printf \'{"jsonrpc":"2.0","id":2,"result":{"tools":[]}}\\n\' ;;\n'
            "  esac\n"
            "done\n"
        )
        stub.chmod(0o755)

        server = McpServerInfo(name="noisy-ok", command=str(stub))
        with patch("kiro_crew.config.loader.KiroCrewConfig") as mock_cls:
            mock_cfg = MagicMock()
            mock_cfg.dashboard.mcp_probe_timeout_secs = 3
            mock_cls.load.return_value = mock_cfg

            result = await probe_server(server)

        assert result.status == "ok", f"unexpected error: {result.error}"
        assert "stderr:" not in (result.error or "")
        assert "deprecated" not in (result.error or "")

    @pytest.mark.asyncio
    async def test_stderr_tail_is_bounded(self, tmp_path) -> None:
        """Very large stderr is truncated so it cannot explode logs or
        dashboard responses."""
        from kiro_crew.mcp_discovery import probe_server

        stub = tmp_path / "verbose-broken.sh"
        stub.write_text(
            "#!/bin/sh\n"
            "for i in $(seq 1 200); do\n"
            "  echo 'this is a long diagnostic line that repeats many times' >&2\n"
            "done\n"
            "exit 1\n"
        )
        stub.chmod(0o755)

        server = McpServerInfo(name="verbose", command=str(stub))
        with patch("kiro_crew.config.loader.KiroCrewConfig") as mock_cls:
            mock_cfg = MagicMock()
            mock_cfg.dashboard.mcp_probe_timeout_secs = 2
            mock_cls.load.return_value = mock_cfg

            result = await probe_server(server)

        assert result.status == "error"
        # 500-char stderr tail + 200-char error head + headers = well under 1KB.
        assert len(result.error or "") < 1024

    @pytest.mark.asyncio
    async def test_credential_in_stderr_is_redacted(self, tmp_path) -> None:
        """stderr is untrusted output — credentials and exfiltration URLs
        must be scrubbed before they land in `server.error`."""
        from kiro_crew.mcp_discovery import probe_server

        stub = tmp_path / "leaky-server.sh"
        stub.write_text(
            "#!/bin/sh\n"
            "echo 'config error: AWS_SECRET_"
            "ACCESS_KEY=AKIAIOSFODNN7"
            "EXAMPLEXXX' >&2\n"
            "exit 1\n"
        )
        stub.chmod(0o755)

        server = McpServerInfo(name="leaky", command=str(stub))
        with patch("kiro_crew.config.loader.KiroCrewConfig") as mock_cls:
            mock_cfg = MagicMock()
            mock_cfg.dashboard.mcp_probe_timeout_secs = 2
            mock_cls.load.return_value = mock_cfg

            result = await probe_server(server)

        assert result.status == "error"
        # The literal secret must not appear verbatim in the error field.
        assert "AKIAIOSFODNN7" "EXAMPLEXXX" not in (result.error or "")

    @pytest.mark.asyncio
    async def test_long_probe_error_keeps_remedy_sentence(self, monkeypatch) -> None:
        """A long spawn exception must not be chopped mid-sentence.

        SandboxUnavailableError ends with the remedy naming
        agent.sandbox_allow_unsandboxed_exec; the old 200-char cap discarded
        it, so a Windows user saw '...Probe detail: not Linux. I' and no fix.
        """
        from kiro_crew.mcp_discovery import _PROBE_ERROR_MAX_CHARS, probe_server

        # A credential early in the message must be REDACTED (not merely
        # truncated away): raising the cap must not widen a disclosure hole.
        long_msg = (
            "Sandbox backend unavailable, token=AKIAIOSFODNN7"
            "EXAMPLEXXX. "
            "Probe detail: not Linux. "
            + ("x" * 300)
            + " set agent.sandbox_allow_unsandboxed_exec=true in ~/.kiro/crew/config.json"
        )
        assert len(long_msg) > 200  # the old cap would have chopped this

        server = McpServerInfo(name="srv", command="srv")

        # Resolve the command, then fail at the sandbox chokepoint with the long
        # message — the real path a Windows host takes with no sandbox backend.
        monkeypatch.setattr("kiro_crew.mcp_discovery.shutil.which", lambda *a, **k: "/usr/bin/srv")

        def boom(*_a: object, **_k: object) -> object:
            raise RuntimeError(long_msg)

        monkeypatch.setattr("kiro_crew.mcp_discovery.sandboxed_spawn_argv", boom)
        result = await probe_server(server)

        assert result.status == "error"
        # The remedy sentence at the tail survives the (larger) cap.
        assert "sandbox_allow_unsandboxed_exec=true" in (result.error or "")
        assert len(result.error or "") <= _PROBE_ERROR_MAX_CHARS
        # The credential is redacted before it reaches server.error.
        assert "AKIAIOSFODNN7" "EXAMPLEXXX" not in (result.error or "")


class TestProbeStdioMalformedResponse:
    """Stdio probe must not crash on non-spec JSON-RPC response shapes.

    The crashing shape: MCP probe failed [...]: 'str' object has no attribute 'get'
    — some servers return an `error` value (or whole response) that is a bare
    string rather than the spec's {"message": ...} object / dict.
    """

    def setup_method(self) -> None:
        _clear_cache()

    def _make_proc(self, init_line: bytes, list_line: bytes = b"") -> MagicMock:
        proc = MagicMock()
        proc.returncode = 0
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        proc.stdin.close = MagicMock()
        proc.stdout = MagicMock()
        # Trailing b"" models a real stream reaching EOF. The probe's response
        # reader may consume more than one line (it skips banner/blank/non-
        # response lines), so the mock must yield EOF rather than exhausting
        # its side_effect and raising StopAsyncIteration.
        proc.stdout.readline = AsyncMock(side_effect=[init_line, list_line, b""])
        proc.wait = AsyncMock(return_value=0)
        proc.kill = MagicMock()
        return proc

    def test_init_error_as_string_does_not_crash(self, monkeypatch) -> None:
        """An `error` value that is a plain string is handled, not raised."""
        server = McpServerInfo(name="srv", command="srv")
        init_line = json.dumps({"jsonrpc": "2.0", "id": 1, "error": "boom"}).encode() + b"\n"
        proc = self._make_proc(init_line)

        monkeypatch.setattr("shutil.which", lambda cmd, path=None: "/usr/bin/srv")
        with patch(
            "kiro_crew.mcp_discovery.asyncio.create_subprocess_exec",
            AsyncMock(return_value=proc),
        ):
            result = asyncio.run(probe_server(server))

        assert result.status == "error"
        assert result.error == "boom"

    def test_tools_list_non_dict_does_not_crash(self, monkeypatch) -> None:
        """A tools/list response that parses to a bare string is a failed probe.

        The reader yields no response object for it (only dicts carrying an
        ``id`` count), so the probe reports the tools/list failure instead of
        certifying a server no session can get a tool out of.
        """
        server = McpServerInfo(name="srv", command="srv")
        init_line = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode() + b"\n"
        list_line = json.dumps("unexpected-string").encode() + b"\n"
        proc = self._make_proc(init_line, list_line)

        monkeypatch.setattr("shutil.which", lambda cmd, path=None: "/usr/bin/srv")
        with patch(
            "kiro_crew.mcp_discovery.asyncio.create_subprocess_exec",
            AsyncMock(return_value=proc),
        ):
            result = asyncio.run(probe_server(server))

        assert result.status == "error"
        assert "tools/list" in result.error
        assert result.tools == []

    def test_tools_list_missing_tools_key_is_an_error(self, monkeypatch) -> None:
        """A dict result WITHOUT a tools list is malformed, not a tool-less
        server — green-with-zero-tools would certify a server whose one
        required answer didn't parse."""
        server = McpServerInfo(name="srv", command="srv")
        init_line = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode() + b"\n"
        list_line = json.dumps({"jsonrpc": "2.0", "id": 2, "result": {}}).encode() + b"\n"
        proc = self._make_proc(init_line, list_line)

        monkeypatch.setattr("shutil.which", lambda cmd, path=None: "/usr/bin/srv")
        with patch(
            "kiro_crew.mcp_discovery.asyncio.create_subprocess_exec",
            AsyncMock(return_value=proc),
        ):
            result = asyncio.run(probe_server(server))

        assert result.status == "error"
        assert "malformed" in result.error


class TestProbeStdioProtocolVersion:
    """The stdio probe offers our newest revision and steps down once on -32602."""

    def setup_method(self) -> None:
        _clear_cache()

    def _run(self, monkeypatch, lines: list[bytes]) -> tuple[McpServerInfo, list[dict]]:
        proc = MagicMock()
        proc.returncode = 0
        proc.stdin = MagicMock()
        proc.stdin.drain = AsyncMock()
        proc.stdout = MagicMock()
        proc.stdout.readline = AsyncMock(side_effect=[*lines, b""])
        proc.wait = AsyncMock(return_value=0)
        monkeypatch.setattr("shutil.which", lambda cmd, path=None: "/usr/bin/srv")
        with patch(
            "kiro_crew.mcp_discovery.asyncio.create_subprocess_exec",
            AsyncMock(return_value=proc),
        ):
            result = asyncio.run(probe_server(McpServerInfo(name="srv", command="srv")))
        sent = [json.loads(c.args[0]) for c in proc.stdin.write.call_args_list]
        return result, [m for m in sent if m.get("method") == "initialize"]

    def test_offers_the_newest_version_and_steps_down_once(self, monkeypatch) -> None:
        refusal = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32602,
                "message": "Unsupported",
                "data": {"supported": ["2024-11-05"]},
            },
        }
        ok = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}}
        tools = {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "search"}]}}
        lines = [json.dumps(m).encode() + b"\n" for m in (refusal, ok, tools)]

        result, inits = self._run(monkeypatch, lines)

        assert result.status == "ok"
        assert result.protocol_version == "2024-11-05"
        assert [m["params"]["protocolVersion"] for m in inits] == ["2025-06-18", "2024-11-05"]

    def test_an_unknown_error_is_not_retried(self, monkeypatch) -> None:
        boom = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}}

        result, inits = self._run(monkeypatch, [json.dumps(boom).encode() + b"\n"])

        assert (result.status, result.error) == ("error", "boom")
        assert [m["params"]["protocolVersion"] for m in inits] == ["2025-06-18"]


def _make_stream(lines: list[bytes]) -> asyncio.StreamReader:
    """Build a StreamReader pre-fed with ``lines`` and an EOF marker."""
    reader = asyncio.StreamReader()
    for chunk in lines:
        reader.feed_data(chunk)
    reader.feed_eof()
    return reader


class TestReadStdioJsonrpcResponse:
    """_read_stdio_jsonrpc_response skips banner/blank/notification lines before the response."""

    @pytest.mark.asyncio
    async def test_immediate_response(self) -> None:
        stream = _make_stream([b'{"jsonrpc":"2.0","id":1,"result":{}}\n'])
        resp = await _read_stdio_jsonrpc_response(stream, timeout=5)
        assert resp == {"jsonrpc": "2.0", "id": 1, "result": {}}

    @pytest.mark.asyncio
    async def test_skips_leading_banner_line(self) -> None:
        """A non-JSON banner (the aim self-update case) is skipped, not fatal."""
        stream = _make_stream(
            [
                b"example-mcp v0.1.4 starting (backend: wss://mcp.example.com)\n",
                b'{"jsonrpc":"2.0","id":1,"result":{"capabilities":{}}}\n',
            ]
        )
        resp = await _read_stdio_jsonrpc_response(stream, timeout=5)
        assert resp is not None
        assert resp["id"] == 1

    @pytest.mark.asyncio
    async def test_skips_blank_lines(self) -> None:
        stream = _make_stream([b"\n", b"   \n", b'{"jsonrpc":"2.0","id":2,"result":{}}\n'])
        resp = await _read_stdio_jsonrpc_response(stream, timeout=5)
        assert resp is not None
        assert resp["id"] == 2

    @pytest.mark.asyncio
    async def test_skips_notifications_without_id(self) -> None:
        """JSON-RPC notifications (no id) are not responses — keep reading."""
        stream = _make_stream(
            [
                b'{"jsonrpc":"2.0","method":"notifications/message","params":{}}\n',
                b'{"jsonrpc":"2.0","id":1,"result":{}}\n',
            ]
        )
        resp = await _read_stdio_jsonrpc_response(stream, timeout=5)
        assert resp["id"] == 1

    @pytest.mark.asyncio
    async def test_eof_returns_none(self) -> None:
        stream = _make_stream([b"just a banner, no json\n"])
        resp = await _read_stdio_jsonrpc_response(stream, timeout=5)
        assert resp is None

    @pytest.mark.asyncio
    async def test_empty_stream_returns_none(self) -> None:
        stream = _make_stream([])
        resp = await _read_stdio_jsonrpc_response(stream, timeout=5)
        assert resp is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "over_cap,expected_id",
        [
            pytest.param(0, 7, id="exactly-the-cap-still-answers"),
            pytest.param(1, None, id="one-over-drops-the-response"),
            pytest.param(5, None, id="flood-gives-up-without-hanging"),
        ],
    )
    async def test_banner_cap_boundary(self, over_cap, expected_id) -> None:
        """Up to _MAX_BANNER_LINES junk lines are tolerated; one more gives up (None)."""
        from kiro_crew.mcp_discovery import _MAX_BANNER_LINES

        lines = [b"noise\n"] * (_MAX_BANNER_LINES + over_cap)
        lines.append(b'{"jsonrpc":"2.0","id":7,"result":{}}\n')
        stream = _make_stream(lines)
        resp = await _read_stdio_jsonrpc_response(stream, timeout=5)
        assert (resp["id"] if resp is not None else None) == expected_id

    @pytest.mark.asyncio
    async def test_skips_non_object_json_payloads(self) -> None:
        """Bare string / array / number JSON lines are not responses — skip them."""
        stream = _make_stream(
            [
                b'"unexpected-string"\n',
                b"[1, 2, 3]\n",
                b"42\n",
                b'{"jsonrpc":"2.0","id":1,"result":{}}\n',
            ]
        )
        resp = await _read_stdio_jsonrpc_response(stream, timeout=5)
        assert resp is not None
        assert resp["id"] == 1

    @pytest.mark.asyncio
    async def test_skips_a_line_nested_past_the_decoder(self) -> None:
        """``RecursionError`` is not a ``JSONDecodeError``: unlisted, one such
        line failed the probe instead of being skipped."""
        stream = asyncio.StreamReader(limit=1 << 20)
        stream.feed_data(too_deep_line())
        stream.feed_data(b'{"jsonrpc":"2.0","id":1,"result":{}}\n')
        stream.feed_eof()
        resp = await _read_stdio_jsonrpc_response(stream, timeout=5)
        assert resp is not None
        assert resp["id"] == 1

    @pytest.mark.asyncio
    async def test_non_object_json_lines_do_not_count_toward_cap(self) -> None:
        """A server printing a JSON progress counter before it answers is not a
        flood: valid JSON is bounded by the timeout, like a notification."""
        from kiro_crew.mcp_discovery import _MAX_BANNER_LINES

        progress = [f"{n}\n".encode() for n in range(1, 2 * _MAX_BANNER_LINES + 1)]
        stream = _make_stream([*progress, b'{"jsonrpc":"2.0","id":1,"result":{}}\n'])
        resp = await _read_stdio_jsonrpc_response(stream, timeout=5)
        assert resp is not None
        assert resp["id"] == 1

    @pytest.mark.asyncio
    async def test_notifications_do_not_count_toward_cap(self) -> None:
        """>_MAX_BANNER_LINES JSON-RPC notifications must NOT trip the banner cap."""
        from kiro_crew.mcp_discovery import _MAX_BANNER_LINES

        notif = b'{"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n'
        lines = [notif] * (_MAX_BANNER_LINES + 10)
        lines.append(b'{"jsonrpc":"2.0","id":1,"result":{}}\n')
        stream = _make_stream(lines)
        resp = await _read_stdio_jsonrpc_response(stream, timeout=5)
        assert resp is not None
        assert resp["id"] == 1

    @pytest.mark.asyncio
    async def test_blank_lines_do_not_count_toward_cap(self) -> None:
        """>_MAX_BANNER_LINES blank lines must NOT trip the banner cap."""
        from kiro_crew.mcp_discovery import _MAX_BANNER_LINES

        lines = [b"\n"] * (_MAX_BANNER_LINES + 10)
        lines.append(b'{"jsonrpc":"2.0","id":3,"result":{}}\n')
        stream = _make_stream(lines)
        resp = await _read_stdio_jsonrpc_response(stream, timeout=5)
        assert resp is not None
        assert resp["id"] == 3

    @pytest.mark.asyncio
    async def test_timeout_raises(self) -> None:
        """A stream that never yields a full line times out (mapped to 'timeout')."""
        reader = asyncio.StreamReader()  # no data, no EOF → readline blocks
        with pytest.raises(asyncio.TimeoutError):
            await _read_stdio_jsonrpc_response(reader, timeout=0.05)


class TestProbeServerBannerTolerance:
    """probe_server tolerates a banner that precedes the handshake."""

    @pytest.mark.asyncio
    async def test_leading_banner_does_not_fail_probe(self) -> None:
        proc = AsyncMock()
        proc.returncode = 0
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        proc.stdin.close = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        proc.stdout = AsyncMock()
        proc.stderr = AsyncMock()
        proc.stdout.readline = AsyncMock(
            side_effect=[
                b"example-mcp v0.1.4 starting (backend: wss://mcp.example.com)\n",
                b'{"jsonrpc":"2.0","id":1,"result":{"capabilities":{}}}\n',
                b'{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"read"}]}}\n',
                b"",
            ]
        )
        server = McpServerInfo(name="local-chorus-mcp", command="local-chorus-mcp")

        with (
            patch("kiro_crew.mcp_discovery.asyncio.create_subprocess_exec", return_value=proc),
            patch("kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/local-chorus-mcp"),
        ):
            result = await probe_server(server)

        assert result.status == "ok"
        assert result.tools == ["read"]
        assert "Expecting value" not in result.error


# ── Probe process-group reap tests ───────────


class TestProbeGroupReap:
    """probe_server must own and reap a dedicated process group so launcher
    grandchildren (npx shim -> node MCP server) cannot leak."""

    def _make_mock_proc(self, pid: int = 4242) -> AsyncMock:
        proc = AsyncMock()
        proc.pid = pid
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdin.close = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        proc.stdout = AsyncMock()
        proc.stdout.readline = AsyncMock(return_value=b"")
        return proc

    @pytest.mark.asyncio
    async def test_spawn_uses_start_new_session_on_posix(self) -> None:
        """The probe child must be its own session/process-group leader."""
        from kiro_crew import platform_compat

        if not platform_compat.IS_POSIX:
            pytest.skip("POSIX-only spawn flag")
        proc = self._make_mock_proc()
        server = McpServerInfo(name="test", command="echo")

        with (
            patch(
                "kiro_crew.mcp_discovery.asyncio.create_subprocess_exec",
                return_value=proc,
            ) as mock_exec,
            patch("kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/echo"),
            patch("kiro_crew.mcp_discovery.os.killpg"),
        ):
            await probe_server(server)

        assert mock_exec.call_args.kwargs.get("start_new_session") is True

    @pytest.mark.asyncio
    async def test_teardown_reaps_process_group(self) -> None:
        """Even after a graceful leader exit, the whole group is SIGKILLed —
        a leader-only kill leaves npx/node grandchildren alive (the leaked
        MCP-tree accumulation)."""
        import signal as _signal

        from kiro_crew import platform_compat

        if not platform_compat.IS_POSIX:
            pytest.skip("killpg is POSIX-only")
        proc = self._make_mock_proc(pid=5151)
        server = McpServerInfo(name="test", command="echo")

        with (
            patch(
                "kiro_crew.mcp_discovery.asyncio.create_subprocess_exec",
                return_value=proc,
            ),
            patch("kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/echo"),
            patch("kiro_crew.mcp_discovery.os.killpg") as mock_killpg,
        ):
            await probe_server(server)

        mock_killpg.assert_called_once_with(5151, _signal.SIGKILL)

    @pytest.mark.asyncio
    async def test_teardown_tolerates_empty_group(self) -> None:
        """ESRCH (group already empty) must not surface as a probe error."""
        from kiro_crew import platform_compat

        if not platform_compat.IS_POSIX:
            pytest.skip("killpg is POSIX-only")
        proc = self._make_mock_proc()
        server = McpServerInfo(name="test", command="echo")

        with (
            patch(
                "kiro_crew.mcp_discovery.asyncio.create_subprocess_exec",
                return_value=proc,
            ),
            patch("kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/echo"),
            patch("kiro_crew.mcp_discovery.os.killpg", side_effect=ProcessLookupError),
        ):
            result = await probe_server(server)

        # teardown error handling must not clobber the probe result
        assert result.name == "test"

    @pytest.mark.asyncio
    async def test_teardown_refuses_non_int_pid(self) -> None:
        """Mock/sentinel pids must never coerce into killpg(1) == init."""
        from kiro_crew import platform_compat

        if not platform_compat.IS_POSIX:
            pytest.skip("killpg is POSIX-only")
        proc = self._make_mock_proc()
        proc.pid = MagicMock()  # non-int stand-in
        server = McpServerInfo(name="test", command="echo")

        with (
            patch(
                "kiro_crew.mcp_discovery.asyncio.create_subprocess_exec",
                return_value=proc,
            ),
            patch("kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/echo"),
            patch("kiro_crew.mcp_discovery.os.killpg") as mock_killpg,
        ):
            await probe_server(server)

        mock_killpg.assert_not_called()

    @pytest.mark.asyncio
    async def test_real_grandchild_is_reaped(self, monkeypatch, tmp_path) -> None:
        """End-to-end: a probed 'server' that forks a grandchild and never
        answers must leave NO survivors after the probe returns."""
        from kiro_crew import platform_compat

        if not platform_compat.IS_POSIX:
            pytest.skip("process groups are POSIX-only")

        grandchild_pid_file = tmp_path / "grandchild.pid"
        # Fake launcher: forks a long-lived grandchild (same process group),
        # writes its pid, then sleeps without ever answering the handshake —
        # modeling a launcher shim wedged mid-cold-start.
        script = tmp_path / "fake_launcher.sh"
        script.write_text(
            "#!/bin/sh\n" "sleep 300 &\n" f"echo $! > {grandchild_pid_file}\n" "sleep 300\n"
        )
        script.chmod(0o755)

        monkeypatch.setattr("kiro_crew.mcp_discovery._get_probe_timeout", lambda: 1)
        # The child deliberately never exits, so `probe_server`'s teardown pays its
        # graceful-exit budget AND its post-SIGKILL budget in full (2 x 5s) before
        # reaching the process-group reap this test is about. Shrink both: the reap
        # is what is asserted, and waiting out the real budget made this the single
        # slowest test in the suite at 12s.
        monkeypatch.setattr("kiro_crew.mcp_discovery._PROBE_TEARDOWN_WAIT_SECS", 0.5)
        server = McpServerInfo(name="fake", command=str(script))
        result = await probe_server(server)
        assert result.status == "error"  # timed out, as designed

        gc_pid = int(grandchild_pid_file.read_text(encoding="utf-8").strip())
        try:
            # Windows-safe liveness probe (a raw os.kill(pid, 0) TERMINATES the
            # target on Windows — the platform_compat rule); this test is
            # POSIX-gated, but route through the shim to stay consistent.
            await async_wait_until(
                lambda: platform_compat.pid_liveness(gc_pid) == platform_compat.PID_DEAD
            )
        except AssertionError:
            platform_compat.kill_pid(gc_pid, platform_compat.SIGKILL)  # cleanup
            pytest.fail("grandchild survived probe teardown — process-group reap regressed")


class TestWindowsTeardownOffLoop:
    """The Windows probe teardown must not run ``taskkill`` on the event loop.

    ``platform_compat.kill_process_tree`` shells out to ``taskkill /T /F`` via a
    blocking ``subprocess.run`` on Windows. Awaited inline that stalls the loop
    once per failed probe, and ``probe_all`` fans out across every configured
    server -- so several unreachable servers serialize that many process spawns
    onto the loop and the dashboard health check starts dropping.

    Asserted against the SHIPPED SOURCE rather than by simulating a Windows run:
    the branch is unreachable on this platform (``IS_WINDOWS`` is False), so a
    behavioural test here would pass no matter what the code did.
    """

    def test_kill_process_tree_is_offloaded(self) -> None:
        import inspect

        from kiro_crew import mcp_discovery

        src = inspect.getsource(mcp_discovery.probe_server)
        assert "kill_process_tree" in src, "teardown moved -- retarget this guard"
        # Every kill_process_tree call in the probe path must be wrapped.
        for line in src.splitlines():
            if "kill_process_tree" in line and not line.strip().startswith("#"):
                assert (
                    "to_thread" in line or "platform_compat.kill_process_tree," in line
                ), f"kill_process_tree called on the loop: {line.strip()}"
        assert "asyncio.to_thread(" in src


class TestWindowsProbeReapLogging:
    """The Windows probe tree reap must distinguish an expected already-exited
    process from a genuine cleanup failure.

    ``kill_process_tree`` maps ``taskkill`` rc=128 to ``ProcessLookupError``
    (the Windows analog of a POSIX process-not-found result) and other failures
    to ``OSError``. The reap handler must log the expected case WITHOUT a
    traceback -- otherwise a routine cleanup race looks identical to a real reap
    failure in the log -- while keeping the traceback for genuine failures,
    mirroring the POSIX branch just above it. Driven on this POSIX host by
    flipping ``IS_WINDOWS``/``IS_POSIX`` and faking the reap; no ``taskkill`` is
    launched and no real process is signaled.
    """

    def _make_mock_proc(self, pid: int = 4242) -> AsyncMock:
        proc = AsyncMock()
        proc.pid = pid
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdin.close = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        proc.stdout = AsyncMock()
        proc.stdout.readline = AsyncMock(return_value=b"")
        return proc

    async def _run_probe_windows_reap(self, monkeypatch, reap_exc: BaseException | None):
        """Drive probe_server down the Windows reap arm with a faked reap."""
        from kiro_crew import mcp_discovery

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        monkeypatch.setattr(mcp_discovery.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(mcp_discovery.platform_compat, "IS_POSIX", False)

        def _reap(_pid: int, _sig: int) -> None:
            if reap_exc is not None:
                raise reap_exc

        monkeypatch.setattr(mcp_discovery.platform_compat, "kill_process_tree", _reap)

        proc = self._make_mock_proc(pid=4242)
        server = McpServerInfo(name="win-reap", command="echo")
        with (
            patch(
                "kiro_crew.mcp_discovery.asyncio.create_subprocess_exec",
                return_value=proc,
            ),
            patch("kiro_crew.mcp_discovery.shutil.which", return_value="C:\\echo.exe"),
        ):
            return await probe_server(server)

    @pytest.mark.asyncio
    async def test_expected_already_exited_logs_without_traceback(
        self, monkeypatch, caplog
    ) -> None:
        """rc=128 -> ProcessLookupError: no traceback, no 'reap failed' line."""
        caplog.set_level(logging.DEBUG, logger="kiro_crew.mcp_discovery")
        result = await self._run_probe_windows_reap(
            monkeypatch, ProcessLookupError("[taskkill rc=128] not found")
        )
        # Teardown must not clobber the probe result.
        assert result.name == "win-reap"
        reap_records = [
            r
            for r in caplog.records
            if r.name == "kiro_crew.mcp_discovery" and "Probe tree" in r.message
        ]
        assert reap_records, "expected a DEBUG line for the already-gone tree"
        for r in reap_records:
            # The expected already-exited case carries NO traceback and does
            # not use the alarming 'reap failed' wording.
            assert r.exc_info is None, "already-exited reap must not log a traceback"
            assert "reap failed" not in r.message

    @pytest.mark.asyncio
    async def test_genuine_failure_retains_traceback(self, monkeypatch, caplog) -> None:
        """A non-ProcessLookupError OSError keeps its traceback diagnostic."""
        caplog.set_level(logging.DEBUG, logger="kiro_crew.mcp_discovery")
        result = await self._run_probe_windows_reap(
            monkeypatch, OSError("[taskkill rc=1] access denied")
        )
        assert result.name == "win-reap"
        failed = [
            r
            for r in caplog.records
            if r.name == "kiro_crew.mcp_discovery" and "Probe tree reap failed" in r.message
        ]
        assert failed, "a genuine reap failure must still be logged"
        assert any(
            r.exc_info is not None for r in failed
        ), "genuine reap failure must retain a traceback"
