"""Tests for cron_script module — ScriptContext, the MCP bridge and the runners' edge cases.

This file pins ``ScriptContext`` and the MCP bridge a script cron calls through:
server-config resolution, the ``McpToolClient`` spawn environment, the redaction of its
server's stderr tail, protocol and close, ``notify``, ``_post`` and ``call_tool``, and
the servers one run keeps. It also holds
the ``Skip``/``Done``/``Report`` exceptions and the runners' edge cases and spawn
exceptions. ``test_cron_script_execution.py`` pins script-path validation, shell
resolution and how a run reports its status and output, and
``test_cron_script_hardening.py`` a run's failure paths, the secret a script run is
handed and each run's sandbox tier. ``cron_script_helpers.py`` holds the autouse
fixtures the three share.
"""

from __future__ import annotations

import json
import threading
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest
from cron_script_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _cron_caller_is_named,
    _crons_dir_tracks_patched_home,
)

import kiro_crew.cron_script as cron_script
from kiro_crew import platform_compat as pc
from kiro_crew.cron_script import (
    Done,
    McpToolError,
    Report,
    ScriptContext,
    Skip,
    _resolve_mcp_server,
    run_command_sandboxed,
    run_script_sandboxed,
)


class TestScriptContext:
    """Tests for ScriptContext properties."""

    def test_message_property(self):
        job = SimpleNamespace(id="j1", message="test-args")
        ctx = ScriptContext(job=job)
        assert ctx.message == "test-args"

    def test_message_property_missing(self):
        job = SimpleNamespace(id="j1")
        ctx = ScriptContext(job=job)
        assert ctx.message == ""


class TestResolveMcpServer:
    """Tests for _resolve_mcp_server config lookup."""

    def test_returns_none_for_missing_config(self, tmp_path):
        _resolve_mcp_server.cache_clear()
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _resolve_mcp_server("nonexistent-server")
        assert result is None

    def test_returns_argv_and_env_for_valid_config(self, tmp_path):
        _resolve_mcp_server.cache_clear()
        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        config = {
            "mcpServers": {
                "test-server": {"command": "node", "args": ["server.js", "--port", "3000"]}
            }
        }
        (agents_dir / "kirocrew.json").write_text(json.dumps(config))
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _resolve_mcp_server("test-server")
        # New shape: (argv, env). No env block in config -> empty env dict.
        assert result == (("node", "server.js", "--port", "3000"), {})

    def test_returns_env_block_from_config(self, tmp_path):
        """Regression: the per-server `env` block (e.g. the PATH that makes a
        launcher's helper binary resolvable) must be returned, not silently
        dropped."""
        _resolve_mcp_server.cache_clear()
        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        config = {
            "mcpServers": {
                "path-server": {
                    "command": "some-mcp",
                    "args": ["--mode", "stdio"],
                    "env": {"PATH": "/custom/path", "MCP_REGION": "us-east-1"},
                }
            }
        }
        (agents_dir / "kirocrew.json").write_text(json.dumps(config))
        with patch("pathlib.Path.home", return_value=tmp_path):
            argv, env = _resolve_mcp_server("path-server")
        assert argv == ("some-mcp", "--mode", "stdio")
        assert env == {"PATH": "/custom/path", "MCP_REGION": "us-east-1"}

    def test_env_values_coerced_to_str(self, tmp_path):
        """env keys/values are stringified so a numeric config value can't crash
        the subprocess env update."""
        _resolve_mcp_server.cache_clear()
        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        config = {
            "mcpServers": {
                "num-env": {
                    "command": "node",
                    "args": [],
                    "env": {"PORT": 3000},
                }
            }
        }
        (agents_dir / "kirocrew.json").write_text(json.dumps(config))
        with patch("pathlib.Path.home", return_value=tmp_path):
            _argv, env = _resolve_mcp_server("num-env")
        assert env == {"PORT": "3000"}

    @staticmethod
    def _write_env_config(tmp_path, env_block):
        """Write an agent config whose sole server declares *env_block*."""
        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "kirocrew.json").write_text(
            json.dumps(
                {"mcpServers": {"bad-env": {"command": "some-mcp", "env": env_block}}}
            )
        )

    @pytest.mark.parametrize(
        "bad_key,bad_value,label",
        [
            ("A=B", "v", "'=' in name -> ValueError('illegal environment variable name')"),
            ("A\0B", "v", "NUL in name -> ValueError('embedded null byte')"),
            ("A", "v\0w", "NUL in value -> ValueError('embedded null byte')"),
            ("", "v", "empty name -> unreachable, getenv('') is always NULL"),
        ],
    )
    def test_env_entry_popen_would_reject_is_dropped(
        self, tmp_path, bad_key, bad_value, label
    ):
        """An env pair Popen refuses is dropped here, not carried to the spawn.

        Popen validates the env BEFORE the fork and fails the whole spawn over
        one bad pair, so carrying any of these through aborted every
        ``ctx.call_tool`` for the server with a traceback naming Popen instead of
        the config. Dropping is per-entry: the well-formed sibling must survive,
        or the fix would trade a crash for the env-drop bug this function exists
        to fix.
        """
        _resolve_mcp_server.cache_clear()
        self._write_env_config(tmp_path, {bad_key: bad_value, "MCP_REGION": "us-east-1"})
        with patch("pathlib.Path.home", return_value=tmp_path):
            _argv, env = _resolve_mcp_server("bad-env")
        # The malformed entry is gone...
        assert bad_key not in env, label
        # ...and the honourable sibling is untouched.
        assert env == {"MCP_REGION": "us-east-1"}

    @pytest.mark.skipif(
        not pc.IS_POSIX,
        reason="asserts the POSIX execve env contract Popen enforces "
               "(`=`/NUL -> ValueError); Windows validates its env block differently",
    )
    def test_resolved_env_clears_the_popen_boundary_that_rejected_it(self, tmp_path):
        """End of the crash path, asserted where it actually decides.

        The test above proves the pairs are filtered. This one hands both shapes
        to the real ``Popen`` and pins the difference: the raw config env raises
        ValueError before any fork, the resolved env gets far enough to fail on
        the missing binary instead. The baseline half matters -- without it a
        future refactor that stops filtering would still pass, since a test that
        only asserts "no ValueError" cannot tell a fixed boundary from one that
        never rejected anything.
        """
        import subprocess

        _resolve_mcp_server.cache_clear()
        raw_block = {"A=B": "v", "N\0K": "v", "NV": "v\0w", "": "v", "MCP_REGION": "us-east-1"}
        self._write_env_config(tmp_path, raw_block)
        with patch("pathlib.Path.home", return_value=tmp_path):
            _argv, env = _resolve_mcp_server("bad-env")

        missing = str(tmp_path / "does-not-exist")
        # Baseline: each rejected shape really is a spawn-killer, so the pass
        # below means the filter worked rather than that Popen tolerates it.
        # The empty name is absent here on purpose -- Popen accepts it, and it is
        # dropped for being unreachable by the child rather than for crashing.
        for key, value in [("A=B", "v"), ("N\0K", "v"), ("NV", "v\0w")]:
            with pytest.raises(ValueError):
                subprocess.Popen([missing], env={**env, key: value})
        # The resolved env clears env encoding and dies on the absent command --
        # a different failure, reached only after the env was accepted.
        with pytest.raises(FileNotFoundError):
            subprocess.Popen([missing], env=env)


class TestExceptionClasses:
    """Tests for Skip, Done, Report exception classes."""

    def test_skip(self):
        with pytest.raises(Skip):
            raise Skip()

    def test_done_with_message(self):
        try:
            raise Done("completed")
        except Done as d:
            assert d.message == "completed"

    def test_done_empty(self):
        try:
            raise Done()
        except Done as d:
            assert d.message == ""

    def test_report_with_message(self):
        try:
            raise Report("status update")
        except Report as r:
            assert r.message == "status update"


class TestMcpToolClient:
    """Tests for McpToolClient JSON-RPC communication."""

    def test_init_fails_for_unknown_server(self, tmp_path):
        from kiro_crew.cron_script import McpToolClient

        with patch("pathlib.Path.home", return_value=tmp_path):
            with pytest.raises(RuntimeError, match="not found"):
                McpToolClient("nonexistent-server")

    def test_close_handles_already_terminated(self, tmp_path):
        from kiro_crew.cron_script import McpToolClient

        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.terminate = MagicMock()
        client._proc.wait = MagicMock()
        client._proc.returncode = 0
        client._sandbox_cleanup = None
        client.close()
        client._proc.terminate.assert_called_once()
        # An exited server answers the bounded wait at once, so no kill follows.
        client._proc.wait.assert_called_once_with(timeout=5)
        client._proc.kill.assert_not_called()

    def test_init_passes_spec_env_to_popen_and_scrubs_secrets(self, monkeypatch):
        """Regression: McpToolClient must hand the spawned MCP subprocess the
        per-server `env` (so a launcher can resolve its helper binary on PATH)
        while still scrubbing cron secrets (Slack tokens / owner id)."""
        from kiro_crew.cron_script import (
            _CRON_ENV_DENY,
            McpToolClient,
            _resolve_mcp_server,
        )

        _resolve_mcp_server.cache_clear()
        # A secret that must NEVER reach the child subprocess env...
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-should-not-leak")
        # ...and an ordinary var that SHOULD be inherited from the cron env.
        monkeypatch.setenv("KIROCREW_PROBE_VAR", "keep-me")

        # Mock proc whose stdout answers the initialize handshake (id == 1).
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.readline.return_value = (
            '{"jsonrpc":"2.0","id":1,"result":{}}\n'
        )

        # The spec env carries the PATH the launcher needs, plus a denied key to
        # prove the deny-set is applied to the spec overlay too.
        spec_env = {"PATH": "/custom/launcher/path", "SLACK_BOT_TOKEN": "spec-also-blocked"}
        with patch(
            "kiro_crew.cron_script._resolve_mcp_server",
            return_value=(("some-mcp", "--mode", "stdio"), spec_env),
        ), patch(
            "kiro_crew.cron_script.wrap_argv",
            return_value=(["some-mcp", "--mode", "stdio"], None),
        ), patch(
            "kiro_crew.cron_script.cgroup_scope_argv",
            side_effect=lambda argv: argv,
        ), patch(
            "kiro_crew.cron_script.popen_limited", return_value=mock_proc
        ) as mock_popen:
            client = McpToolClient("path-server")
            client.close()

        passed_env = mock_popen.call_args.kwargs["env"]
        # Spec PATH is forwarded so the launcher can find its helper binary.
        assert passed_env["PATH"] == "/custom/launcher/path"
        # Ordinary inherited vars survive.
        assert passed_env.get("KIROCREW_PROBE_VAR") == "keep-me"
        # The denied secret is scrubbed from BOTH the inherited env and the
        # spec overlay (it never reaches the child).
        assert "SLACK_BOT_TOKEN" not in passed_env
        assert "SLACK_BOT_TOKEN" in _CRON_ENV_DENY

    def _mock_handshake_proc(self):
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.readline.return_value = '{"jsonrpc":"2.0","id":1,"result":{}}\n'
        return mock_proc

    def test_init_leaves_an_operator_command_argv0_for_the_spec_path_to_resolve(
        self, tmp_path, monkeypatch
    ):
        """The spawn site must NOT re-pin argv[0].

        Confinement wrappers are pinned to absolute paths by the functions that
        prepend them (``cgroup_scope_argv``, ``sandbox_exec_argv``), so nothing is
        left for this spawn site to protect. Re-pinning here would be actively
        wrong on the fail-open no-sandbox path, where NO wrapper is prepended and
        argv[0] is the OPERATOR-declared command: resolving that against the
        gateway's own directories would silently run a different copy than the one
        the spec ``PATH`` selects -- overriding the very PATH forwarding this PR
        exists to deliver. Regression guard for that inversion.
        """
        from kiro_crew.cron_script import McpToolClient, _resolve_mcp_server

        _resolve_mcp_server.cache_clear()

        # The SAME command name exists in two places: one on the gateway's PATH,
        # one on the spec PATH. The spec's copy is the one the operator asked for.
        gateway_dir = tmp_path / "gateway"
        gateway_dir.mkdir()
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        for d in (gateway_dir, spec_dir):
            binary = d / "some-mcp"
            binary.write_text("#!/bin/sh\n")
            binary.chmod(0o755)
        monkeypatch.setenv("PATH", str(gateway_dir))

        spec_env = {"PATH": str(spec_dir)}
        with patch(
            "kiro_crew.cron_script._resolve_mcp_server",
            return_value=(("some-mcp",), spec_env),
        ), patch(
            # Fail-open no-sandbox path: neither helper prepends a wrapper, so
            # argv[0] stays the operator's own command.
            "kiro_crew.cron_script.wrap_argv",
            return_value=(["some-mcp"], None),
        ), patch(
            "kiro_crew.cron_script.cgroup_scope_argv",
            side_effect=lambda argv: list(argv),
        ), patch(
            "kiro_crew.cron_script.popen_limited", return_value=self._mock_handshake_proc()
        ) as mock_popen:
            client = McpToolClient("path-server")
            client.close()

        popen_argv = mock_popen.call_args.args[0]
        passed_env = mock_popen.call_args.kwargs["env"]
        # Handed to Popen verbatim -- NOT rewritten to the gateway's copy.
        assert popen_argv[0] == "some-mcp"
        assert str(gateway_dir) not in popen_argv[0]
        # ...so the spec PATH is what resolves it.
        assert passed_env["PATH"] == str(spec_dir)

    def test_init_drops_loader_injection_keys_from_spec_env(self, monkeypatch):
        """SECURITY regression: a spec ``env`` must not carry loader/interpreter
        injection keys into the spawn.

        ``_CRON_ENV_DENY`` covers secrets, NOT ``LD_*``/``DYLD_*``/``PYTHON*``, and
        the spec env is externally authorable (pasted ``mcpServers`` blocks). Those
        keys are honoured by the dynamic loader/interpreter INSIDE the confinement
        wrapper process -- i.e. before the wrapper establishes containment -- so
        forwarding them is arbitrary pre-confinement code execution. Pinning
        argv[0] does not help: the loader acts on the pinned binary. The spec env
        therefore goes through ``env.sanitize_spec_env``, the same filter the
        discovery probe uses. ``PATH`` is deliberately NOT dropped -- forwarding it
        is the feature.
        """
        from kiro_crew.cron_script import McpToolClient, _resolve_mcp_server

        _resolve_mcp_server.cache_clear()
        for key in ("LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES", "PYTHONPATH"):
            monkeypatch.delenv(key, raising=False)

        spec_env = {
            "LD_PRELOAD": "/tmp/evil.so",
            "LD_LIBRARY_PATH": "/tmp/evil-libs",
            "DYLD_INSERT_LIBRARIES": "/tmp/evil.dylib",
            "PYTHONPATH": "/tmp/evil-py",
            # Lowercase reaches the loader on Windows, where env names are
            # case-insensitive; the sanitizer folds case for that reason.
            "ld_preload": "/tmp/evil-lower.so",
            "PATH": "/opt/helper/bin",
            "MCP_TOKEN": "keep-me",
        }
        with patch(
            "kiro_crew.cron_script._resolve_mcp_server",
            return_value=(("some-mcp",), spec_env),
        ), patch(
            "kiro_crew.cron_script.wrap_argv", return_value=(["some-mcp"], None)
        ), patch(
            "kiro_crew.cron_script.cgroup_scope_argv", side_effect=lambda argv: list(argv)
        ), patch(
            "kiro_crew.cron_script.popen_limited", return_value=self._mock_handshake_proc()
        ) as mock_popen:
            client = McpToolClient("path-server")
            client.close()

        passed_env = mock_popen.call_args.kwargs["env"]
        for denied in (
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "DYLD_INSERT_LIBRARIES",
            "PYTHONPATH",
            "ld_preload",
        ):
            assert denied not in passed_env, f"{denied} reached the spawn"
        # The point of the PR survives the filter.
        assert passed_env["PATH"] == "/opt/helper/bin"
        assert passed_env["MCP_TOKEN"] == "keep-me"

    def test_init_drops_reserved_kirocrew_namespace_from_spec_env(self, monkeypatch):
        """SECURITY regression: a spec ``env`` must not forge caller identity.

        The server this bridge most often spawns is ``kirocrew-cron`` itself,
        whose ownership checks resolve the calling session from
        ``KIROCREW_SESSION_KEY``/``KIROCREW_HOST_PID`` when no gateway caller
        block is present. So one of those in that server's ``env`` block would let
        a SCRIPT CRON name itself another session and reach that session's jobs --
        ``cron_remove_all`` over its rows, ``cron_list`` over its contents.

        ``KIROCREW_CLI`` is asserted here too, as an inert subject rather than a
        live consumer: it is an ambient admin flag that skips ownership outright,
        and it has no producer in ``src/`` and its consumer is gone, so nothing
        sets or reads it. The deny stays because the CLASS is
        live -- the next identity key somebody adds is the reason.

        Neither existing filter stops it: ``_CRON_ENV_DENY`` covers secrets and
        ``KIROCREW_OWNER_ID``, and the loader prefixes cover ``LD_*``/``PYTHON*``.
        And unlike those, the OS sandbox is not a mitigation at all -- confinement
        bounds what the child may touch, not whose jobs Kiro Crew believes it
        owns. Hence the reserved-namespace deny in the shared sanitizer.
        """
        from kiro_crew.cron_script import McpToolClient, _resolve_mcp_server

        _resolve_mcp_server.cache_clear()
        for key in ("KIROCREW_CLI", "KIROCREW_SESSION_KEY", "KIROCREW_HOST_PID"):
            monkeypatch.delenv(key, raising=False)

        spec_env = {
            # The reported finding.
            "KIROCREW_CLI": "1",
            # Siblings in the same class: two of the three sources the STRICT
            # session resolver accepts because "an agent cannot write them".
            "KIROCREW_SESSION_KEY": "some-other-session",
            "KIROCREW_HOST_PID": "4242",
            # Lowercase reaches the same lookup on Windows.
            "kirocrew_cli": "1",
            # ...and the forwarding this PR exists to deliver still works.
            "PATH": "/opt/helper/bin",
            "MCP_TOKEN": "keep-me",
        }
        with patch(
            "kiro_crew.cron_script._resolve_mcp_server",
            return_value=(("some-mcp",), spec_env),
        ), patch(
            "kiro_crew.cron_script.wrap_argv", return_value=(["some-mcp"], None)
        ), patch(
            "kiro_crew.cron_script.cgroup_scope_argv", side_effect=lambda argv: list(argv)
        ), patch(
            "kiro_crew.cron_script.popen_limited", return_value=self._mock_handshake_proc()
        ) as mock_popen:
            client = McpToolClient("kirocrew-cron")
            client.close()

        passed_env = mock_popen.call_args.kwargs["env"]
        for denied in (
            "KIROCREW_CLI",
            "KIROCREW_SESSION_KEY",
            "KIROCREW_HOST_PID",
            "kirocrew_cli",
        ):
            assert denied not in passed_env, f"{denied} reached the spawn"
        assert passed_env["PATH"] == "/opt/helper/bin"
        assert passed_env["MCP_TOKEN"] == "keep-me"

    def test_an_identity_key_cannot_be_forged_through_a_cron_spec_env_block(
        self, monkeypatch
    ):
        """The end of the attack path, asserted where it actually decides.

        The test above proves the key never reaches the child env. This one runs
        the real consumer against that env: the strict session resolver evaluated
        in the environment the spawned server would have started with must not
        return the forged session, so a spec's ``env`` block cannot name another
        session even if some future refactor changes which filter drops the key.

        ``KIROCREW_CLI`` is not the key under test: it is an ambient admin flag
        with no producer in ``src/`` and no consumer, so there is no claim to
        re-ground. ``KIROCREW_SESSION_KEY`` is the right
        successor subject precisely because it DOES have legitimate producers
        (the ACP spawn path, this bridge), so for it the filter is load-bearing
        rather than belt-and-braces over a dead name.

        SCOPE, deliberately narrow: this covers the ``env`` block only, which is
        the channel ``sanitize_spec_env`` controls. The same spec's ``command``
        field is equally config-authored and reaches the identical consumer
        (``sh -c 'KIROCREW_SESSION_KEY=... exec kirocrew-cron'``), so it is a
        sibling forgery channel this filter does not close and this test does not
        claim to -- confinement bounds what the child may touch, not whose jobs
        Kiro Crew believes it owns. Closing it needs consumer-side vouching
        rather than another deny-list; naming the boundary here keeps the
        assertion honest about which half is proven.
        """
        from kiro_crew.cron_script import McpToolClient, _resolve_mcp_server
        from kiro_crew.mcp_core import _resolve_session_key_strict

        _resolve_mcp_server.cache_clear()
        forged = "dashboard:victim"
        # Baseline: the key really is an identity source, so a miss below means
        # the filter worked rather than that the key never mattered.
        monkeypatch.setenv("KIROCREW_SESSION_KEY", forged)
        assert _resolve_session_key_strict() == forged
        monkeypatch.delenv("KIROCREW_SESSION_KEY")

        with patch(
            "kiro_crew.cron_script._resolve_mcp_server",
            return_value=(("some-mcp",), {"KIROCREW_SESSION_KEY": forged}),
        ), patch(
            "kiro_crew.cron_script.wrap_argv", return_value=(["some-mcp"], None)
        ), patch(
            "kiro_crew.cron_script.cgroup_scope_argv", side_effect=lambda argv: list(argv)
        ), patch(
            "kiro_crew.cron_script.popen_limited", return_value=self._mock_handshake_proc()
        ) as mock_popen:
            client = McpToolClient("kirocrew-cron")
            client.close()

        child_env = mock_popen.call_args.kwargs["env"]
        with patch.dict("os.environ", child_env, clear=True):
            assert _resolve_session_key_strict() != forged

    def test_rpc_disconnect_includes_rc_and_stderr_tail(self, tmp_path):
        """a handshake EOF must surface exit code + stderr tail."""
        from kiro_crew.cron_script import McpToolClient
        stderr_path = tmp_path / "stderr.log"
        stderr_path.write_text("Node version 18 detected, but version 20 or higher is required.\n")
        client = object.__new__(McpToolClient)
        client._server_name = "atoz-mcp"
        client._req_id = 0
        client._stderr_file = SimpleNamespace(name=str(stderr_path))
        client._proc = MagicMock()
        client._proc.stdin = MagicMock()
        client._proc.stdout = MagicMock()
        client._proc.poll.return_value = 1
        # _recv returns None (EOF) on the first read
        with patch.object(McpToolClient, "_recv", return_value=None):
            with pytest.raises(RuntimeError) as excinfo:
                client._rpc("initialize")
        msg = str(excinfo.value)
        assert "atoz-mcp" in msg
        assert "initialize" in msg
        assert "rc=1" in msg
        assert "version 20 or higher is required" in msg

    def test_rpc_disconnect_empty_stderr(self, tmp_path):
        from kiro_crew.cron_script import McpToolClient
        client = object.__new__(McpToolClient)
        client._server_name = "some-mcp"
        client._req_id = 0
        client._stderr_file = SimpleNamespace(name=str(tmp_path / "missing.log"))
        client._proc = MagicMock()
        client._proc.stdin = MagicMock()
        client._proc.stdout = MagicMock()
        client._proc.poll.return_value = None
        with patch.object(McpToolClient, "_recv", return_value=None):
            with pytest.raises(RuntimeError, match=r"stderr tail: \(empty\)"):
                client._rpc("tools/call")

    def test_stderr_tail_redacts_credentials(self, tmp_path):
        from kiro_crew.cron_script import McpToolClient
        stderr_path = tmp_path / "stderr.log"
        stderr_path.write_text("boom AKIA1234567890123456 failure")
        client = object.__new__(McpToolClient)
        client._stderr_file = SimpleNamespace(name=str(stderr_path))
        tail = client._stderr_tail()
        assert "AKIA1234567890123456" not in tail
        assert "boom" in tail

    def test_stderr_tail_redacts_credential_straddling_the_slice_boundary(self, tmp_path):
        """A key straddling the tail cut must not leak a fragment.

        Sized so slicing FIRST would open ten characters INTO the access
        key, leaving a digit fragment the key regex cannot match. The whole
        capture is redacted before the tail slice cuts, so redaction sees
        the key whole.
        """
        import re

        from kiro_crew.cron_script import McpToolClient
        stderr_path = tmp_path / "stderr.log"
        filler = "x" * 1000
        # Synthetic key id: deliberately well-formed so the redaction regex has
        # a real target; it is not a credential.
        secret = "AKIA" + "1234567890123456"
        stderr_path.write_text(filler + secret + "x" * 1006 + " failure")
        client = object.__new__(McpToolClient)
        client._stderr_file = SimpleNamespace(name=str(stderr_path))
        tail = client._stderr_tail()
        assert len(tail) <= 1024
        assert secret not in tail
        assert "AKIA" not in tail
        assert re.search(r"\d{4,}", tail) is None
        assert "failure" in tail

    def test_stderr_tail_redacts_url_longer_than_any_window(self, tmp_path):
        """A URL longer than any read window must not leak its query tail.

        The URL itself spans ~6 KiB, so its scheme sits thousands of
        characters from EOF while the query tail lands inside the served
        tail; only a full-capture redaction pass sees the scheme and scrubs
        the match whole. A windowed read would scan the tail without its
        scheme and serve the raw query.
        """
        from kiro_crew.cron_script import McpToolClient
        stderr_path = tmp_path / "stderr.log"
        # Long query triggers redact_exfiltration_urls' length-based
        # heuristic, as in the test below; here it is long enough that no
        # fixed window reaches back to the scheme.
        long_query = "data=" + ("a" * 6000)
        url = f"https://evil.example.com/leak?{long_query}"
        stderr_path.write_text("x" * 100 + url + "\nfailure")
        client = object.__new__(McpToolClient)
        client._stderr_file = SimpleNamespace(name=str(stderr_path))
        tail = client._stderr_tail()
        assert len(tail) <= 1024
        assert "evil.example.com/leak" not in tail
        assert "a" * 100 not in tail
        assert "failure" in tail

    def test_stderr_tail_of_a_large_capture_is_still_served_redacted(self, tmp_path):
        """A capture far past the read span still yields a redacted tail.

        Only the last ``_STDERR_TAIL_READ_SPAN`` bytes are read, so a long
        session's stderr costs a fixed amount, and its diagnostic is kept.
        """
        from kiro_crew import cron_script
        from kiro_crew.cron_script import McpToolClient

        stderr_path = tmp_path / "stderr.log"
        noise = ("log line\n" * (4 * cron_script._STDERR_TAIL_READ_SPAN // 9))
        # Synthetic key id, split so no scanner reads it as a credential.
        payload = noise + "key " + "AKIA" + "1234567890123456" + "\nfailure"
        stderr_path.write_text(payload)
        client = object.__new__(McpToolClient)
        client._stderr_file = SimpleNamespace(name=str(stderr_path))
        tail = client._stderr_tail()
        assert len(tail) <= 1024
        assert "AKIA" not in tail
        assert tail.endswith("failure")

    def test_stderr_tail_drops_the_line_severed_by_the_read_start(self, tmp_path):
        """A line cut by the bounded read's start is never served."""
        from kiro_crew import cron_script
        from kiro_crew.cron_script import McpToolClient

        stderr_path = tmp_path / "stderr.log"
        span = cron_script._STDERR_TAIL_READ_SPAN
        # One line longer than the whole read: its anchor is gone.
        stderr_path.write_text("Bearer " + "q" * (2 * span))
        client = object.__new__(McpToolClient)
        client._stderr_file = SimpleNamespace(name=str(stderr_path))
        tail = client._stderr_tail()
        assert "q" * 16 not in tail

    def test_stderr_tail_masks_a_key_block_opened_before_the_read(self, tmp_path):
        """A private-key body whose BEGIN sits before the read stays masked."""
        from kiro_crew import cron_script
        from kiro_crew.cron_script import McpToolClient

        stderr_path = tmp_path / "stderr.log"
        span = cron_script._STDERR_TAIL_READ_SPAN
        body = "\n".join("K" * 64 for _ in range(span // 32))
        label = "RSA " + "PRIVATE KEY"
        stderr_path.write_text(
            f"-----BEGIN {label}-----\n" + body + f"\n-----END {label}-----\nfailure"
        )
        client = object.__new__(McpToolClient)
        client._stderr_file = SimpleNamespace(name=str(stderr_path))
        tail = client._stderr_tail()
        assert "K" * 64 not in tail
        assert tail.endswith("failure")

    def test_stderr_tail_masks_an_unterminated_key_block_opened_before_the_read(
        self, tmp_path
    ):
        """A key block that opened before the read and never closes stays masked.

        No marker is left inside the read, so only a scan of the skipped
        prefix knows the block is open.
        """
        from kiro_crew import cron_script
        from kiro_crew.cron_script import McpToolClient

        stderr_path = tmp_path / "stderr.log"
        span = cron_script._STDERR_TAIL_READ_SPAN
        body = "\n".join("K" * 64 for _ in range(span // 32))
        label = "RSA " + "PRIVATE KEY"
        stderr_path.write_text(f"-----BEGIN {label}-----\n" + body + "\nfailure")
        client = object.__new__(McpToolClient)
        client._stderr_file = SimpleNamespace(name=str(stderr_path))
        tail = client._stderr_tail()
        assert "K" * 16 not in tail

    def test_stderr_tail_redacts_exfiltration_urls(self, tmp_path):
        from kiro_crew.cron_script import McpToolClient
        stderr_path = tmp_path / "stderr.log"
        # Long innocuous query (>= 200 chars) triggers redact_exfiltration_urls'
        # length-based heuristic. We can't use a credential-shaped value (AKIA,
        # base64 blob) because redact_credentials runs first and would replace
        # it before the URL redactor sees it, leaving a short benign query.
        long_query = "data=" + ("a" * 250)
        stderr_path.write_text(
            f"boom https://evil.example.com/leak?{long_query} failure"
        )
        client = object.__new__(McpToolClient)
        client._stderr_file = SimpleNamespace(name=str(stderr_path))
        tail = client._stderr_tail()
        assert "evil.example.com/leak" not in tail
        assert "boom" in tail

    def test_close_removes_stderr_tempfile(self, tmp_path):
        from kiro_crew.cron_script import McpToolClient
        stderr_path = tmp_path / "stderr.log"
        stderr_path.write_text("x")
        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.terminate = MagicMock()
        client._proc.wait = MagicMock()
        client._sandbox_cleanup = None
        # mimic a real file handle: has .close() and .name
        fh = open(stderr_path, "w+")
        client._stderr_file = fh
        client.close()
        assert not stderr_path.exists()


class TestScriptContextNotify:
    """Tests for ScriptContext.notify() HTTP delivery."""

    def test_notify_success(self):
        job = SimpleNamespace(id="j1", message="test")
        ctx = ScriptContext(job=job)
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"ok": true}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch("kiro_crew.cron_script.loopback_urlopen", return_value=mock_response):
            result = ctx.notify("hello")
        assert result == {"ok": True}

    def test_notify_failure_raises(self):
        job = SimpleNamespace(id="j1", message="test")
        ctx = ScriptContext(job=job)
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"error": "forbidden"}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch("kiro_crew.cron_script.loopback_urlopen", return_value=mock_response):
            with pytest.raises(RuntimeError, match="forbidden"):
                ctx.notify("hello")

    def test_notify_redacts_credentials(self):
        # Built from parts so secret scanners do not flag a literal key in the source.
        fake_key = "AK" + "IA1234567890123456"
        job = SimpleNamespace(id="j1", message="test")
        ctx = ScriptContext(job=job)
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"ok": true}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch(
            "kiro_crew.cron_script.loopback_urlopen", return_value=mock_response
        ) as mock_urlopen:
            ctx.notify(f"secret {fake_key} text")
            mock_urlopen.assert_called_once()
        sent = json.loads(mock_urlopen.call_args.args[0].data)
        assert fake_key not in sent["text"]
        assert sent["text"].startswith("secret ")


class TestScriptContextCallTool:
    """Tests for ScriptContext.call_tool() MCP bridge."""

    def test_call_tool_raises_on_unknown_server(self, tmp_path):
        job = SimpleNamespace(id="j1", message="test")
        ctx = ScriptContext(job=job)
        with patch("pathlib.Path.home", return_value=tmp_path):
            with pytest.raises(RuntimeError, match="not found"):
                ctx.call_tool("nonexistent", "tool", {})


class TestScriptContextKeepsServers:
    """One run's calls to a server share its process, and close() stops it."""

    @pytest.fixture
    def started(self, monkeypatch):
        """Every fake server started, in order. ``fail_next`` is the error its next call raises."""
        started = []

        class FakeClient:
            def __init__(self, server, session_key=""):
                self.server, self.calls, self.closed = server, [], 0
                self.running, self.fail_next = True, None
                started.append(self)

            def call_tool(self, name, arguments):
                self.calls.append(name)
                if self.fail_next is not None:
                    failure, self.fail_next = self.fail_next, None
                    raise failure
                return f"{self.server}:{name}"

            def is_running(self):
                return self.running

            def close(self):
                self.closed += 1
                self.running = False

        monkeypatch.setattr("kiro_crew.cron_script.McpToolClient", FakeClient)
        return started

    @staticmethod
    def _ctx():
        return ScriptContext(job=SimpleNamespace(id="j1", message=""))

    def test_calls_to_one_server_share_its_process(self, started):
        ctx = self._ctx()
        results = [ctx.call_tool("slack", tool, {}) for tool in "abc"]
        assert results == ["slack:a", "slack:b", "slack:c"]
        (server,) = started
        assert (server.calls, server.closed) == (["a", "b", "c"], 0)
        ctx.close()
        assert server.closed == 1

    def test_each_server_name_gets_its_own_process(self, started):
        ctx = self._ctx()
        for server, tool in (("slack", "a"), ("builder", "b"), ("slack", "c")):
            ctx.call_tool(server, tool, {})
        assert [(s.server, s.calls) for s in started] == [("slack", ["a", "c"]), ("builder", ["b"])]
        ctx.close()
        assert [s.closed for s in started] == [1, 1]

    def test_a_call_without_an_answer_stops_its_server_and_the_next_call_starts_another(
        self, started
    ):
        ctx = self._ctx()
        ctx.call_tool("slack", "a", {})
        started[0].fail_next = RuntimeError("MCP server 'slack' disconnected")
        with pytest.raises(RuntimeError, match="disconnected"):
            ctx.call_tool("slack", "b", {})
        assert started[0].closed == 1
        assert ctx.call_tool("slack", "c", {}) == "slack:c"
        assert [s.calls for s in started] == [["a", "b"], ["c"]]

    def test_a_tool_error_keeps_its_server_for_the_next_call(self, started):
        ctx = self._ctx()
        ctx.call_tool("slack", "a", {})
        started[0].fail_next = McpToolError("MCP tool error: boom")
        with pytest.raises(McpToolError, match="boom"):
            ctx.call_tool("slack", "b", {})
        assert started[0].closed == 0
        assert ctx.call_tool("slack", "c", {}) == "slack:c"
        (server,) = started
        assert server.calls == ["a", "b", "c"]
        ctx.close()
        assert server.closed == 1

    def test_a_kept_server_that_exited_is_replaced(self, started):
        ctx = self._ctx()
        ctx.call_tool("slack", "a", {})
        started[0].running = False
        ctx.call_tool("slack", "b", {})
        assert started[0].closed == 1, "an exited server's files are still cleaned up"
        assert [s.calls for s in started] == [["a"], ["b"]]

    def test_a_call_made_while_another_runs_starts_its_own_server(self, started):
        ctx = self._ctx()
        ctx.call_tool("slack", "warm", {})
        first = started[0]
        answer = first.call_tool
        entered, release = threading.Event(), threading.Event()

        def held(name, arguments):
            entered.set()
            assert release.wait(5)
            return answer(name, arguments)

        first.call_tool = held
        worker = threading.Thread(target=ctx.call_tool, args=("slack", "slow", {}))
        worker.start()
        try:
            assert entered.wait(5)
            assert ctx.call_tool("slack", "fast", {}) == "slack:fast"
        finally:
            release.set()
            worker.join(5)
        second = started[1]
        assert second.calls == ["fast"]
        assert first.closed == 1, "the call finishing last finds a kept server and stops its own"
        ctx.close()
        assert second.closed == 1

    def test_nothing_is_kept_after_close(self, started):
        ctx = self._ctx()
        ctx.call_tool("slack", "a", {})
        ctx.close()
        ctx.call_tool("slack", "b", {})
        assert [s.closed for s in started] == [1, 1]
        ctx.close()
        assert [s.closed for s in started] == [1, 1]

    def test_close_stops_every_kept_server_even_when_one_fails_to_stop(self, started):
        ctx = self._ctx()
        ctx.call_tool("slack", "a", {})
        ctx.call_tool("builder", "b", {})
        stuck = started[0]

        def refuse():
            stuck.closed += 1
            raise OSError("cannot stop")

        stuck.close = refuse
        ctx.close()
        assert [s.closed for s in started] == [1, 1]


class TestRunCommandSandboxedEdgeCases:
    """Additional edge case tests for run_command_sandboxed."""

    @pytest.fixture(autouse=True)
    def _passthrough_sandbox(self, monkeypatch, posix_test_shell):
        """Bypass the OS-sandbox wrap so these plumbing tests run without a
        sandbox backend (see TestRunCommandSandboxed._passthrough_sandbox)."""
        monkeypatch.setattr(
            "kiro_crew.cron_script.wrap_argv", lambda argv, **k: (list(argv), None)
        )
        # Bypass the runtime shell probe (which itself spawns a child): these
        # tests exercise the run_command_sandboxed plumbing, not shell fingerprinting.
        monkeypatch.setattr(
            "kiro_crew.cron_script._resolve_command_shell", lambda: posix_test_shell
        )

    def test_command_with_env_vars(self):
        result = run_command_sandboxed("echo $HOME")
        assert result["status"] == "ok"
        # HOME should be available (not scrubbed)
        assert result["output"].strip() != ""

    def test_multiline_output(self):
        result = run_command_sandboxed("echo line1; echo line2; echo line3")
        assert result["status"] == "ok"
        assert "line1" in result["output"]
        assert "line3" in result["output"]

    def test_pipe_command(self):
        result = run_command_sandboxed("echo hello world | wc -w")
        assert result["status"] == "ok"
        assert "2" in result["output"]


class TestRunScriptSandboxedEdgeCases:
    """Additional edge case tests for run_script_sandboxed."""

    @pytest.fixture(autouse=True)
    def _passthrough_sandbox(self, monkeypatch):
        """See TestRunScriptSandboxed._passthrough_sandbox."""
        import os as _os
        src_dir = str(Path(__file__).resolve().parents[1] / "src")
        existing = _os.environ.get("PYTHONPATH", "")
        monkeypatch.setenv("PYTHONPATH", src_dir + (_os.pathsep + existing if existing else ""))
        monkeypatch.setattr(
            "kiro_crew.cron_script.wrap_argv", lambda argv, **k: (list(argv), None)
        )
        # Bypass the runtime shell probe (which itself spawns a child): these
        # tests exercise the run_command_sandboxed plumbing, not shell fingerprinting.
        # Return "sh" so Popen mocks that assert on argv[0] still see it.
        monkeypatch.setattr("kiro_crew.cron_script._resolve_command_shell", lambda: "sh")

    def _write_script(self, tmp_path, code):
        crons_dir = tmp_path / ".kirocrew" / "crons"
        crons_dir.mkdir(parents=True)
        script = crons_dir / "edge_test.py"
        script.write_text(code)
        return str(script)

    def test_report_status(self, tmp_path):
        script_path = self._write_script(
            tmp_path,
            """
from kiro_crew.cron_script import Report
def run(ctx):
    raise Report("progress update")
""",
        )
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = run_script_sandboxed(script_path + ":run", "test-job", "")
        assert result["status"] == "report"
        assert result["message"] == "progress update"

    def test_script_with_imports(self, tmp_path):
        script_path = self._write_script(
            tmp_path,
            """
import os
from kiro_crew.cron_script import Done
def run(ctx):
    assert os.path.exists("/")
""",
        )
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = run_script_sandboxed(script_path + ":run", "test-job", "")
        assert result["status"] == "ok"

    @pytest.mark.parametrize(
        ("ending", "status"),
        [("return", "ok"), ("raise Skip()", "skip"), ("raise ValueError('bad')", "error")],
    )
    def test_the_launcher_closes_the_context_however_the_script_ends(
        self, tmp_path, monkeypatch, ending, status
    ):
        marker = tmp_path / "closed.txt"
        script_path = self._write_script(
            tmp_path,
            f"""
from pathlib import Path
from kiro_crew.cron_script import Skip
def run(ctx):
    ctx.close = lambda: Path(ctx.message).write_text(str(Path.cwd()))
    {ending}
""",
        )
        original_popen_limited = cron_script.popen_limited
        monkeypatch.setattr(
            cron_script,
            "popen_limited",
            partial(original_popen_limited, cwd=tmp_path),
        )
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = run_script_sandboxed(script_path + ":run", "test-job", str(marker))
        assert result["status"] == status
        assert marker.read_text() == str(tmp_path)


class TestMcpToolClientProtocol:
    """Tests for McpToolClient JSON-RPC protocol internals."""

    def test_send_writes_json_line(self):
        from kiro_crew.cron_script import McpToolClient

        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        mock_stdin = MagicMock()
        client._proc.stdin = mock_stdin
        client._send({"jsonrpc": "2.0", "method": "test"})
        written = mock_stdin.write.call_args[0][0]
        assert json.loads(written.strip()) == {"jsonrpc": "2.0", "method": "test"}
        mock_stdin.flush.assert_called_once()

    def test_recv_returns_parsed_json(self):
        from kiro_crew.cron_script import McpToolClient

        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.stdout = MagicMock()
        client._proc.stdout.readline.return_value = '{"jsonrpc":"2.0","id":1,"result":{}}\n'
        msg = client._recv()
        assert msg == {"jsonrpc": "2.0", "id": 1, "result": {}}

    def test_recv_returns_none_on_eof(self):
        from kiro_crew.cron_script import McpToolClient

        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.stdout = MagicMock()
        client._proc.stdout.readline.return_value = ""
        assert client._recv() is None

    def test_recv_skips_blank_lines(self):
        from kiro_crew.cron_script import McpToolClient

        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.stdout = MagicMock()
        client._proc.stdout.readline.side_effect = ["\n", "  \n", '{"id":1,"result":"ok"}\n']
        # Each skipped line is one read, so _rpc's line cap counts it.
        assert [client._recv() for _ in range(3)] == [{}, {}, {"id": 1, "result": "ok"}]

    def test_rpc_sends_and_receives(self):
        from kiro_crew.cron_script import McpToolClient

        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.stdin = MagicMock()
        client._proc.stdout = MagicMock()
        client._req_id = 0
        client._proc.stdout.readline.return_value = (
            '{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}\n'
        )
        result = client._rpc("tools/list")
        assert result == {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}

    def test_rpc_raises_on_eof(self):
        from kiro_crew.cron_script import McpToolClient

        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.stdin = MagicMock()
        client._proc.stdout = MagicMock()
        client._req_id = 0
        client._proc.stdout.readline.return_value = ""
        with pytest.raises(RuntimeError, match="disconnected") as raised:
            client._rpc("tools/list")
        assert not isinstance(raised.value, McpToolError)

    def test_call_tool_success(self):
        from kiro_crew.cron_script import McpToolClient

        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.stdin = MagicMock()
        client._proc.stdout = MagicMock()
        client._req_id = 0
        client._proc.stdout.readline.return_value = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"content": [{"type": "text", "text": "hello"}]},
                }
            )
            + "\n"
        )
        result = client.call_tool("my_tool", {"arg": "val"})
        assert result == "hello"

    def test_call_tool_error_response(self):
        from kiro_crew.cron_script import McpToolClient

        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.stdin = MagicMock()
        client._proc.stdout = MagicMock()
        client._req_id = 0
        client._proc.stdout.readline.return_value = (
            json.dumps(
                {"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": "Invalid request"}}
            )
            + "\n"
        )
        with pytest.raises(McpToolError, match="Invalid request"):
            client.call_tool("bad_tool", {})

    def test_call_tool_is_error_flag(self):
        from kiro_crew.cron_script import McpToolClient

        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.stdin = MagicMock()
        client._proc.stdout = MagicMock()
        client._req_id = 0
        client._proc.stdout.readline.return_value = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "isError": True,
                        "content": [{"type": "text", "text": "tool failed"}],
                    },
                }
            )
            + "\n"
        )
        with pytest.raises(McpToolError, match="tool failed"):
            client.call_tool("failing_tool", {})

    def test_call_tool_is_error_no_content(self):
        from kiro_crew.cron_script import McpToolClient

        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.stdin = MagicMock()
        client._proc.stdout = MagicMock()
        client._req_id = 0
        client._proc.stdout.readline.return_value = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"isError": True, "content": []}})
            + "\n"
        )
        with pytest.raises(McpToolError, match="unknown error"):
            client.call_tool("failing_tool", {})

    def test_close_with_sandbox_cleanup(self, tmp_path):
        from kiro_crew.cron_script import McpToolClient

        cleanup_file = tmp_path / "sandbox_cleanup"
        cleanup_file.write_text("temp")
        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.terminate = MagicMock()
        client._proc.wait = MagicMock()
        client._proc.returncode = 0
        client._sandbox_cleanup = str(cleanup_file)
        client.close()
        assert not cleanup_file.exists()

    def test_close_timeout_kills(self):
        import subprocess

        from kiro_crew.cron_script import McpToolClient

        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.terminate = MagicMock()
        client._proc.wait = MagicMock(side_effect=[subprocess.TimeoutExpired("cmd", 5), None])
        client._proc.kill = MagicMock()
        client._sandbox_cleanup = None
        client.close()
        client._proc.kill.assert_called_once()
        # The kill is reaped with an unbounded wait after the bounded one expired.
        assert client._proc.wait.call_args_list == [call(timeout=5), call()]

    def test_init_popen_failure_cleans_sandbox(self, tmp_path):
        from kiro_crew.cron_script import McpToolClient

        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        config = {"mcpServers": {"test": {"command": "nonexistent_binary_xyz", "args": []}}}
        (agents_dir / "kirocrew.json").write_text(json.dumps(config))
        with patch("pathlib.Path.home", return_value=tmp_path):
            with pytest.raises(Exception):
                McpToolClient("test")

    def test_init_handshake_failure_closes(self, tmp_path):
        from kiro_crew.cron_script import McpToolClient

        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        config = {"mcpServers": {"test": {"command": "echo", "args": ["bye"]}}}
        (agents_dir / "kirocrew.json").write_text(json.dumps(config))
        with patch("pathlib.Path.home", return_value=tmp_path):
            with pytest.raises(Exception):
                McpToolClient("test")


class TestResolveMcpServerAimFallback:
    """Tests for _resolve_mcp_server AIM agent spec fallback."""

    def test_aim_fallback_path(self, tmp_path):
        # No kirocrew.json, but a fallback agent spec exists
        _resolve_mcp_server.cache_clear()
        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        config = {"mcpServers": {"builder-mcp": {"command": "npx", "args": ["-y", "builder-mcp"]}}}
        (agents_dir / "my-kirocrew-agent.json").write_text(json.dumps(config))
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _resolve_mcp_server("builder-mcp")
        # Fallback path returns the same (argv, env) shape; no env block -> {}.
        assert result == (("npx", "-y", "builder-mcp"), {})

    def test_server_not_in_config(self, tmp_path):
        _resolve_mcp_server.cache_clear()
        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        config = {"mcpServers": {"other-server": {"command": "node", "args": []}}}
        (agents_dir / "kirocrew.json").write_text(json.dumps(config))
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _resolve_mcp_server("nonexistent")
        assert result is None


class TestScriptContextCallToolSuccess:
    """Tests for ScriptContext.call_tool() success path with mocked McpToolClient."""

    def test_call_tool_success_path(self, tmp_path):
        job = SimpleNamespace(id="j1", message="test")
        ctx = ScriptContext(job=job)
        mock_client = MagicMock()
        mock_client.call_tool.return_value = "result text"
        with patch("kiro_crew.cron_script.McpToolClient", return_value=mock_client), patch(
            "kiro_crew.cron_script.sel"
        ) as mock_sel:
            result = ctx.call_tool("server", "tool", {"key": "val"})
        assert result == "result text"
        mock_client.close.assert_not_called()
        ctx.close()
        mock_client.close.assert_called_once()
        mock_sel().log_tool_invocation.assert_called()

    def test_call_tool_error_path(self, tmp_path):
        job = SimpleNamespace(id="j1", message="test")
        ctx = ScriptContext(job=job)
        mock_client = MagicMock()
        mock_client.call_tool.side_effect = RuntimeError("connection failed")
        with patch("kiro_crew.cron_script.McpToolClient", return_value=mock_client), patch(
            "kiro_crew.cron_script.sel"
        ):
            with pytest.raises(RuntimeError, match="connection failed"):
                ctx.call_tool("server", "tool", {})
        mock_client.close.assert_called_once()

    def test_audit_tool_call_sel_failure_swallowed(self, tmp_path):
        job = SimpleNamespace(id="j1", message="test")
        ctx = ScriptContext(job=job)
        mock_client = MagicMock()
        mock_client.call_tool.return_value = ""
        with patch("kiro_crew.cron_script.McpToolClient", return_value=mock_client), patch(
            "kiro_crew.cron_script.sel"
        ) as mock_sel:
            mock_sel().log_tool_invocation.side_effect = Exception("SEL down")
            result = ctx.call_tool("server", "tool", {})
        assert result == ""


class TestScriptContextPost:
    """Tests for ScriptContext._post() HTTP helper."""

    def test_post_success(self):
        job = SimpleNamespace(id="j1", message="test")
        ctx = ScriptContext(job=job)
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"status": "delivered"}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch("kiro_crew.cron_script.loopback_urlopen", return_value=mock_response):
            result = ctx._post("/api/deliver", {"text": "hello"})
        assert result == {"status": "delivered"}

    def test_post_failure_returns_error(self):
        job = SimpleNamespace(id="j1", message="test")
        ctx = ScriptContext(job=job)
        with patch(
            "kiro_crew.cron_script.loopback_urlopen", side_effect=Exception("connection refused")
        ):
            result = ctx._post("/api/deliver", {"text": "hello"})
        assert "error" in result
        assert "connection refused" in result["error"]


class TestRunCommandSandboxedExceptions:
    """Tests for run_command_sandboxed timeout and exception paths."""

    @pytest.fixture(autouse=True)
    def _passthrough_sandbox(self, monkeypatch, posix_test_shell):
        """See TestRunCommandSandboxed._passthrough_sandbox."""
        monkeypatch.setattr(
            "kiro_crew.cron_script.wrap_argv", lambda argv, **k: (list(argv), None)
        )
        # Bypass the runtime shell probe (which itself spawns a child): these
        # tests exercise the run_command_sandboxed plumbing, not shell fingerprinting.
        monkeypatch.setattr(
            "kiro_crew.cron_script._resolve_command_shell", lambda: posix_test_shell
        )

    def test_timeout_returns_error(self):
        # Real subprocess: communicate(timeout=1) fires and the child is killed.
        result = run_command_sandboxed("sleep 30", timeout=1)
        assert result["status"] == "error"
        assert "timed out" in result["output"]

    def test_exception_returns_error(self):
        with patch("subprocess.Popen", side_effect=OSError("No such file")):
            result = run_command_sandboxed("nonexistent_binary")
        assert result["status"] == "error"
        assert "failed" in result["output"].lower() or "No such file" in result["output"]


class TestMcpToolClientInitFailures:
    """Tests for McpToolClient.__init__ failure paths."""

    def test_popen_exception_cleans_sandbox(self, tmp_path):
        """Lines 172-175: Popen raises, sandbox cleanup file is removed."""
        from kiro_crew.cron_script import McpToolClient

        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        config = {"mcpServers": {"test": {"command": "node", "args": ["nonexistent.js"]}}}
        (agents_dir / "kirocrew.json").write_text(json.dumps(config))
        cleanup = tmp_path / "cleanup_marker"
        cleanup.write_text("x")
        with patch("pathlib.Path.home", return_value=tmp_path), patch(
            "kiro_crew.cron_script.wrap_argv", return_value=(["false"], str(cleanup))
        ), patch("subprocess.Popen", side_effect=OSError("spawn failed")):
            with pytest.raises(OSError, match="spawn failed"):
                McpToolClient("test")
        assert not cleanup.exists()

    def test_rpc_no_response_in_1000_messages(self):
        """Line 214: server sends 1000+ messages without matching ID."""
        from kiro_crew.cron_script import McpToolClient

        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.stdin = MagicMock()
        client._proc.stdout = MagicMock()
        client._req_id = 0
        # Return messages with wrong IDs
        client._proc.stdout.readline.return_value = '{"jsonrpc":"2.0","id":999,"result":{}}\n'
        with pytest.raises(RuntimeError, match="did not respond"):
            client._rpc("tools/list")

    def test_close_exception_swallowed(self, tmp_path):
        """An exception in terminate is swallowed and the sandbox file is still removed."""
        from kiro_crew.cron_script import McpToolClient

        cleanup_file = tmp_path / "sandbox_cleanup"
        cleanup_file.write_text("temp")
        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.terminate = MagicMock(side_effect=ProcessLookupError("already dead"))
        client._sandbox_cleanup = str(cleanup_file)
        client.close()
        assert not cleanup_file.exists()
        client._proc.kill.assert_not_called()
