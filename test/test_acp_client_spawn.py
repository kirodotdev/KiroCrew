"""Tests for ACP client: spawning the backend: init defaults, the session key and child
environment, session cwd, backend selection, ensure_ready and spawn-argv refusals.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture
from acp_client_helpers import _stop_stderr_drain
from spawn_test_helpers import strip_spawn_shim

import kiro_crew.acp.client as acp_client
from kiro_crew.acp.client import AcpClient, AcpError
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE


class TestAcpClientInit:
    def test_defaults(self):
        client = AcpClient()
        assert not client.is_ready
        assert client._session_id is None

    def test_custom_work_dir(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        assert client._work_dir == tmp_path

    def test_audit_source_defaults_none(self):
        # Chat/subagent clients already audit via chat_runner / SubagentManager;
        # the flag must stay None by default so they never double-log.
        assert AcpClient()._audit_source is None

    def test_audit_source_opt_in(self):
        # App/worker-pool clients (code-review-sage, knowledge llm_pool) have no
        # external audit loop, so they opt in to ACP-layer SEL tool auditing.
        assert AcpClient(audit_source="subagent")._audit_source == "subagent"


class TestAcpClientSessionKey:
    def test_stores_session_key(self):
        client = AcpClient(session_key="test-key")
        assert client._session_key == "test-key"

    @pytest.mark.asyncio
    async def test_spawn_sets_env_with_session_key(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, session_key="test-key")
        with (
            patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
            patch(
                "kiro_crew.acp.client.wrap_argv", return_value=(["/usr/bin/kiro-cli", "acp"], None)
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch("kiro_crew.session._track_pid"),
            patch("kiro_crew.session._track_session_pid"),
        ):
            mock_proc = MagicMock()
            # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process, moved verbatim; tracked for a de-flake pass
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await client._spawn()

            call_kwargs = mock_exec.call_args
            env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
            assert env is not None
            assert env["KIROCREW_SESSION_KEY"] == "test-key"

        await _stop_stderr_drain(client)

    @pytest.mark.asyncio
    async def test_spawn_sets_env_with_channel_id(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, session_key="k", channel_id="C0ABC123")
        with (
            patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
            patch(
                "kiro_crew.acp.client.wrap_argv", return_value=(["/usr/bin/kiro-cli", "acp"], None)
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch("kiro_crew.session._track_pid"),
            patch("kiro_crew.session._track_session_pid"),
        ):
            mock_proc = MagicMock()
            # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process, moved verbatim; tracked for a de-flake pass
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await client._spawn()

            call_kwargs = mock_exec.call_args
            env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
            assert env is not None
            assert env["KIROCREW_CHANNEL_ID"] == "C0ABC123"
            assert env["KIROCREW_SESSION_KEY"] == "k"

        await _stop_stderr_drain(client)

    @pytest.mark.asyncio
    async def test_spawn_forwards_claude_config_dir_from_extra_env(self, tmp_path):
        # The loader factory injects CLAUDE_CONFIG_DIR into cc_env (→ extra_env);
        # _spawn must forward it verbatim to the subprocess so the adapter's
        # SettingsManager reads the isolated dir (creds kept, plugins stripped).
        iso = str(tmp_path / "cc-config")
        client = AcpClient(
            work_dir=tmp_path,
            acp_backend=ACP_BACKEND_CLAUDE,
            extra_env={"CLAUDE_CONFIG_DIR": iso, "CLAUDE_CODE_USE_BEDROCK": "1"},
        )
        with (
            patch(
                "kiro_crew.acp.client._resolve_claude_acp_bin",
                return_value=(["/usr/bin/node", "/x/acp.js"], ""),
            ),
            # ``_spawn`` fills CLAUDE_CODE_EXECUTABLE from
            # ``_resolve_claude_code_executable`` when the env does not carry it,
            # and that resolver shells out to the host's ``mise`` first. This test
            # is about the env being forwarded, not about where ``claude`` lives,
            # so pin the answer instead of depending on the developer's toolchain.
            patch(
                "kiro_crew.acp.client._resolve_claude_code_executable",
                return_value="/usr/local/bin/claude",
            ),
            patch(
                "kiro_crew.acp.client.wrap_argv",
                return_value=(["/usr/bin/node", "/x/acp.js"], None),
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch("kiro_crew.session._track_pid"),
            patch("kiro_crew.session._track_session_pid"),
        ):
            mock_proc = MagicMock()
            # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process, moved verbatim; tracked for a de-flake pass
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await client._spawn()

            call_kwargs = mock_exec.call_args
            env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
            assert env is not None
            assert env["CLAUDE_CONFIG_DIR"] == iso
            # Bedrock flag must ride alongside (regression guard).
            assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"

        await _stop_stderr_drain(client)

    @pytest.mark.asyncio
    async def test_spawn_gives_each_process_its_own_browser_session(self, tmp_path):
        """Two agent processes must not address the same playwright-cli browser.

        Sharing the CLI's ``default`` session lets one process navigate or close
        the other's page; the name is per PROCESS, so two spawns differ even for
        the same session key (a pooled process is spawned before it is claimed).
        """
        names = []
        caller_sockets = str(tmp_path / "caller-controlled-sockets")
        caller_daemons = str(tmp_path / "caller-controlled-daemons")

        def _lifecycle_env(resolved_env):
            assert resolved_env.get("PWTEST_SOCKETS_DIR") != caller_sockets
            assert resolved_env.get("PWTEST_DAEMON_SESSION_DIR") != caller_daemons
            assert resolved_env["PLAYWRIGHT_CLI_SESSION"].startswith("kc-")
            return {}

        for _ in range(2):
            client = AcpClient(
                work_dir=tmp_path,
                session_key="same-key",
                extra_env={
                    "PWTEST_SOCKETS_DIR": caller_sockets,
                    "PWTEST_DAEMON_SESSION_DIR": caller_daemons,
                },
            )
            with (
                patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
                patch(
                    "kiro_crew.acp.client.wrap_argv",
                    return_value=(["/usr/bin/kiro-cli", "acp"], None),
                ),
                patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
                patch("kiro_crew.acp.client.browser_socket_env", side_effect=_lifecycle_env),
                patch("kiro_crew.session._track_pid"),
                patch("kiro_crew.session._track_session_pid"),
            ):
                mock_proc = MagicMock()
                # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process, moved verbatim; tracked for a de-flake pass
                mock_proc.pid = 12345
                mock_proc.returncode = None
                mock_exec.return_value = mock_proc

                await client._spawn()

                call_kwargs = mock_exec.call_args
                env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
                assert env is not None
                assert env["PLAYWRIGHT_CLI_SESSION"].startswith("kc-")
                names.append(env["PLAYWRIGHT_CLI_SESSION"])

            await _stop_stderr_drain(client)

        assert names[0] != names[1]

    @pytest.mark.asyncio
    async def test_spawn_keeps_an_operator_set_browser_session(self, tmp_path, monkeypatch):
        """An operator who named a session means that one browser."""
        monkeypatch.setenv("PLAYWRIGHT_CLI_SESSION", "chrome")
        monkeypatch.delenv("PWTEST_SOCKETS_DIR", raising=False)
        monkeypatch.delenv("PWTEST_DAEMON_SESSION_DIR", raising=False)
        client = AcpClient(work_dir=tmp_path, session_key="k")
        with (
            patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
            patch(
                "kiro_crew.acp.client.wrap_argv", return_value=(["/usr/bin/kiro-cli", "acp"], None)
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch("kiro_crew.session._track_pid"),
            patch("kiro_crew.session._track_session_pid"),
        ):
            mock_proc = MagicMock()
            # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process, moved verbatim; tracked for a de-flake pass
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await client._spawn()

            call_kwargs = mock_exec.call_args
            env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
            assert env is not None
            assert env["PLAYWRIGHT_CLI_SESSION"] == "chrome"
            assert "PWTEST_SOCKETS_DIR" not in env
            assert "PWTEST_DAEMON_SESSION_DIR" not in env

        await _stop_stderr_drain(client)

    @pytest.mark.asyncio
    async def test_spawn_no_channel_id_env_absent(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, session_key="k", channel_id=None)
        with (
            patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
            patch(
                "kiro_crew.acp.client.wrap_argv", return_value=(["/usr/bin/kiro-cli", "acp"], None)
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch("kiro_crew.session._track_pid"),
            patch("kiro_crew.session._track_session_pid"),
        ):
            mock_proc = MagicMock()
            # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process, moved verbatim; tracked for a de-flake pass
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await client._spawn()

            call_kwargs = mock_exec.call_args
            env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
            assert env is not None
            assert "KIROCREW_CHANNEL_ID" not in env

        await _stop_stderr_drain(client)

    @pytest.mark.asyncio
    async def test_spawn_channel_id_only_no_session_key(self, tmp_path):
        clean_env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SESSION_KEY"}
        client = AcpClient(work_dir=tmp_path, session_key=None, channel_id="C0ABC123")
        with (
            patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
            patch(
                "kiro_crew.acp.client.wrap_argv", return_value=(["/usr/bin/kiro-cli", "acp"], None)
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch("kiro_crew.session._track_pid"),
            patch("kiro_crew.session._track_session_pid"),
            patch.dict(os.environ, clean_env, clear=True),
        ):
            mock_proc = MagicMock()
            # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process, moved verbatim; tracked for a de-flake pass
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await client._spawn()

            call_kwargs = mock_exec.call_args
            env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
            assert env is not None
            assert env["KIROCREW_CHANNEL_ID"] == "C0ABC123"
            assert "KIROCREW_SESSION_KEY" not in env

        await _stop_stderr_drain(client)

    @pytest.mark.asyncio
    async def test_spawn_no_session_key_env_none(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, session_key=None)
        with (
            patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
            patch(
                "kiro_crew.acp.client.wrap_argv", return_value=(["/usr/bin/kiro-cli", "acp"], None)
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch("kiro_crew.session._track_pid"),
            patch("kiro_crew.session._track_session_pid"),
        ):
            mock_proc = MagicMock()
            # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process, moved verbatim; tracked for a de-flake pass
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await client._spawn()

            call_kwargs = mock_exec.call_args
            env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
            assert env is not None, "env should be a dict (SSH_AUTH_SOCK resolution)"
            assert "KIROCREW_SESSION_KEY" not in env

        await _stop_stderr_drain(client)


class TestSpawnStderrDrainCleanup:
    """_spawn's background stderr-drain task must not outlive a mocked spawn.

    _spawn starts _drain_stderr over self._process.stderr, a mock process has a
    truthy stderr, and a spawn test that never stops the client leaves the task
    alive. Its exception is later reported against an unrelated test on the same
    worker.
    """

    @pytest.mark.asyncio
    async def test_spawn_over_mock_leaves_a_live_drain_task(self, tmp_path):
        # Establish the hazard the cleanup exists for: a bare mocked _spawn does
        # start a live drain task, so the cleanup below is load-bearing.
        client = AcpClient(work_dir=tmp_path, session_key="k")
        with (
            patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
            patch(
                "kiro_crew.acp.client.wrap_argv", return_value=(["/usr/bin/kiro-cli", "acp"], None)
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch("kiro_crew.session._track_pid"),
            patch("kiro_crew.session._track_session_pid"),
        ):
            mock_proc = MagicMock()
            # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process, moved verbatim; tracked for a de-flake pass
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await client._spawn()

            assert client._stderr_task is not None
            assert not client._stderr_task.done()

        await _stop_stderr_drain(client)

    @pytest.mark.asyncio
    async def test_stop_stderr_drain_retrieves_the_faulted_task(self, tmp_path):
        # An AsyncMock stream makes readline() return a coroutine, so the drain
        # task faults exactly as observed in the issue. _stop_stderr_drain must
        # retrieve that result so nothing is left unretrieved for the loop to
        # report later. A one-line MagicMock stub for the stdlib asyncio logger
        # would hide a re-leak, so assert on the task's own state instead.
        client = AcpClient(work_dir=tmp_path, session_key="k")
        with (
            patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
            patch(
                "kiro_crew.acp.client.wrap_argv", return_value=(["/usr/bin/kiro-cli", "acp"], None)
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch("kiro_crew.session._track_pid"),
            patch("kiro_crew.session._track_session_pid"),
        ):
            mock_proc = AsyncMock()
            # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process, moved verbatim; tracked for a de-flake pass
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await client._spawn()
            task = client._stderr_task
            assert task is not None

        await _stop_stderr_drain(client)

        assert client._stderr_task is None
        assert task.done()
        # The result is retrieved (no exception escapes and none is left pending
        # for the loop to report). Cancelled or faulted, both are terminal here.
        assert task.cancelled() or task.exception() is not None


@pytest.mark.asyncio
async def test_unbound_client_session_cwd_is_the_spawn_pathname(tmp_path):
    """Off macOS nothing binds, so there is no descriptor to re-verify."""
    client = AcpClient(work_dir=tmp_path)

    assert client._bound_workspace_fd is None
    assert await client._session_work_dir() == str(tmp_path)


@pytest.mark.asyncio
async def test_bound_client_session_cwd_is_read_off_the_descriptor(tmp_path, monkeypatch):
    """The sibling of AcpRuntime._session_work_dir: same rule, same reason.

    session/new and session/load carry a cwd STRING the peer re-resolves, so the
    bind-time spelling -- the one a symlink swap controls -- is re-verified and
    replaced with the descriptor's own name before it is handed over.
    """
    client = AcpClient(work_dir=tmp_path)
    client._bound_workspace_fd = 81
    client._spawn_work_dir = str(tmp_path)
    target = AsyncMock(return_value="/canonical/workspace")
    monkeypatch.setattr(acp_client, "resolve_bound_session_workspace", target)

    assert await client._session_work_dir() == "/canonical/workspace"
    target.assert_awaited_once_with(81, str(tmp_path))


@pytest.mark.asyncio
async def test_bound_client_session_cwd_fails_rather_than_handing_over_the_spelling(
    tmp_path, monkeypatch
):
    """A workspace that does not name the bound identity is not a fallback."""
    client = AcpClient(work_dir=tmp_path)
    client._bound_workspace_fd = 82
    client._spawn_work_dir = str(tmp_path)
    monkeypatch.setattr(
        acp_client,
        "resolve_bound_session_workspace",
        AsyncMock(side_effect=acp_client.BoundWorkspaceMismatch("not-bound")),
    )

    with pytest.raises(AcpError, match="exact workspace"):
        await client._session_work_dir()


@pytest.mark.asyncio
async def test_bound_client_session_cwd_fails_when_the_name_cannot_be_read(tmp_path, monkeypatch):
    """Fail closed, for the same reason bound_agent_workspace_target raises."""
    client = AcpClient(work_dir=tmp_path)
    client._bound_workspace_fd = 83
    client._spawn_work_dir = str(tmp_path)
    monkeypatch.setattr(
        acp_client,
        "resolve_bound_session_workspace",
        AsyncMock(side_effect=OSError("no F_GETPATH here")),
    )

    with pytest.raises(AcpError, match="Cannot verify"):
        await client._session_work_dir()


@pytest.mark.asyncio
async def test_failed_live_spawn_cleanup_releases_workspace_when_kill_is_cancelled(tmp_path):
    client = AcpClient(work_dir=tmp_path)
    client._bound_workspace_fd = 74
    client._spawn_work_dir = "/dev/fd/74"
    client._kill_process = AsyncMock(side_effect=asyncio.CancelledError())
    released: list[int] = []

    async def record_release(descriptor):
        released.append(descriptor)

    with patch("kiro_crew.acp.client.release_bound_agent_workspace", side_effect=record_release):
        with pytest.raises(asyncio.CancelledError):
            await client._cleanup_failed_live_spawn()

    assert released == [74]
    assert client._bound_workspace_fd is None
    assert client._spawn_work_dir == str(tmp_path)


class TestAcpClientBackendSelection:
    """Verify the right backend binary is launched for kiro vs claude."""

    @pytest.fixture(autouse=True)
    def _reset_claude_cache(self):
        # Through the gateway's own reset seam rather than by writing the cache: the
        # resolver is injected per test, so each must resolve afresh, and none may
        # leave its answer for the next.
        from kiro_crew.agent_sdk.drivers.acp import forget_cached_resolution

        forget_cached_resolution(ACP_BACKEND_CLAUDE)
        yield
        forget_cached_resolution(ACP_BACKEND_CLAUDE)

    @pytest.fixture(autouse=True)
    def _no_cgroup_scope(self):
        # These tests assert the exact/leading spawn argv to verify BACKEND
        # SELECTION (which binary), not cgroup wrapping. Our branch wires
        # cgroup_scope_argv() into _spawn(), which prepends a `systemd-run
        # --user --scope ... --` prefix on Linux hosts WITH cgroup-v2 systemd
        # delegation (XDG_RUNTIME_DIR set). Neutralize the probe here so the
        # argv is host-independent — no prefix regardless of the runner. Scoped
        # to this class only; test_sandbox_argv.py (which tests the wrapping
        # itself) is a separate file and is unaffected.
        with patch(
            "kiro_crew.sandbox._probe_cgroup_scope",
            return_value=(False, "disabled-in-test"),
        ):
            yield

    @pytest.mark.asyncio
    async def test_spawn_claude_backend_uses_claude_acp_bin(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        with (
            patch(
                "kiro_crew.acp.client._resolve_claude_acp_bin",
                return_value=(
                    ["/usr/local/bin/node", "/usr/local/lib/claude-agent-acp/index.js"],
                    "",
                ),
            ),
            patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
            # The claude path also resolves CLAUDE_CODE_EXECUTABLE through the
            # host's ``mise``; the subject here is the argv, so pin the resolver.
            patch(
                "kiro_crew.acp.client._resolve_claude_code_executable",
                return_value="/usr/local/bin/claude",
            ),
            patch(
                "kiro_crew.acp.client.wrap_argv",
                side_effect=lambda argv, mode, **kwargs: (argv, None),
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch("kiro_crew.session._track_pid"),
            patch("kiro_crew.session._track_session_pid"),
        ):
            mock_proc = MagicMock()
            # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process, moved verbatim; tracked for a de-flake pass
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await client._spawn()

            argv = list(strip_spawn_shim(mock_exec.call_args.args))
            assert argv == [
                "/usr/local/bin/node",
                "/usr/local/lib/claude-agent-acp/index.js",
            ], "claude backend must spawn node + script explicitly"

        await _stop_stderr_drain(client)

    @pytest.mark.asyncio
    async def test_spawn_claude_backend_missing_bin_raises(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        with (
            patch("kiro_crew.acp.client._resolve_claude_acp_bin", return_value=(None, "")),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock),
        ):
            with pytest.raises(AcpError, match="claude-agent-acp not found"):
                await client._spawn()

    @pytest.mark.asyncio
    async def test_spawn_claude_missing_bin_reports_the_cached_search_path(
        self, tmp_path, monkeypatch
    ):
        searched = os.pathsep.join((str(tmp_path / "node-bin"), str(tmp_path / "npm-bin")))
        later = str(tmp_path / "later-path")
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        with patch(
            "kiro_crew.acp.client._resolve_claude_acp_bin",
            return_value=(None, searched),
        ) as resolve:
            with pytest.raises(AcpError) as first:
                await client._spawn()

            monkeypatch.setenv("PATH", later)
            with pytest.raises(AcpError) as second:
                await client._spawn()

        resolve.assert_called_once_with()
        for error in (str(first.value), str(second.value)):
            assert str(tmp_path / "node-bin") in error
            assert str(tmp_path / "npm-bin") in error
            assert later not in error

    @pytest.mark.asyncio
    async def test_spawn_kiro_missing_bin_reports_only_resolver_search_dirs(self, tmp_path):
        searched = [str(tmp_path / "managed-bin"), str(tmp_path / "path-bin")]
        unsearched = str(tmp_path / "never-checked")
        client = AcpClient(work_dir=tmp_path)
        with (
            patch("kiro_crew.acp.client._resolve_kiro_bin", return_value=None),
            patch("kiro_crew.acp.client.known_kiro_cli_dirs", return_value=searched),
        ):
            with pytest.raises(AcpError) as raised:
                await client._spawn()

        error = str(raised.value)
        assert "searched 2 directories" in error
        assert searched[0] in error
        assert searched[1] in error
        assert unsearched not in error

    @pytest.mark.asyncio
    async def test_spawn_kiro_missing_bin_reports_the_environment_it_searched(
        self, tmp_path, monkeypatch
    ):
        """The reported directories must come from the search's own environment.

        Recomputing ``known_kiro_cli_dirs`` from a FRESH read of ``os.environ``
        after resolution has already failed is what breaks it: a PATH change
        landing in that window -- a concurrent installer, a self-update, anything
        editing the gateway's environment -- makes the message name directories
        that were never searched and omit ones that were, which is the opposite
        of what a "not found (searched ...)" line is for.

        The Claude adapter holds the same guarantee by caching the search path
        with the resolution result (see
        ``test_spawn_claude_missing_bin_reports_the_cached_search_path``); this
        pins it for the Kiro sibling.
        """
        from kiro_crew.acp import client as client_mod

        injected = str(tmp_path / "appeared-after-the-search")
        resolver_env: dict[str, object] = {}
        diagnostic_env: dict[str, object] = {}
        real_dirs = client_mod.known_kiro_cli_dirs

        def _resolve_then_change_path(*, environ=None, home=None):
            resolver_env["mapping"] = environ
            # A PATH mutation arriving while the resolve is in flight.
            monkeypatch.setenv("PATH", injected + os.pathsep + os.environ.get("PATH", ""))
            return None

        def _spy_dirs(platform_name, home, environ, **kwargs):
            diagnostic_env["mapping"] = environ
            return real_dirs(platform_name, home, environ, **kwargs)

        client = AcpClient(work_dir=tmp_path)
        with (
            patch(
                "kiro_crew.acp.client._resolve_kiro_bin",
                side_effect=_resolve_then_change_path,
            ),
            patch("kiro_crew.acp.client.known_kiro_cli_dirs", side_effect=_spy_dirs),
        ):
            with pytest.raises(AcpError, match="not found") as raised:
                await client._spawn()

        # The structural tag the chat row reads to offer the feature-request form.
        assert raised.value.kiro_cli_missing is True
        assert injected in os.environ["PATH"], "the test never actually changed PATH"
        # THE DEFECT: the directory set reported to the user must not be derived
        # from an environment the search never saw. Red-before, where the
        # diagnostic re-read the live os.environ, this is the late PATH entry.
        assert injected not in diagnostic_env["mapping"].get("PATH", "")
        # And the guarantee stated positively: one mapping drove both.
        assert resolver_env.get("mapping") is not None, "resolver got no environment"
        assert diagnostic_env["mapping"] is resolver_env["mapping"]

    @pytest.mark.asyncio
    async def test_resolve_kiro_bin_defaults_keep_every_other_caller_unchanged(self, tmp_path):
        """The new parameters are optional, so the four other call sites are intact.

        ``_resolve_kiro_bin_for_spawn`` is also called from ``acp/runtime.py``,
        ``handlers/agents.py`` and ``handlers/sessions.py``, none of which needs
        the search set. Omitting the arguments must resolve exactly as before.
        """
        from kiro_crew.acp import client as client_mod

        with patch("kiro_crew.acp.client.resolve_kiro_cli", return_value=None) as resolve:
            assert await client_mod._resolve_kiro_bin_for_spawn() is None
        resolve.assert_called_once_with(environ=None, home=None)

    @pytest.mark.asyncio
    async def test_spawn_kiro_backend_unchanged(self, tmp_path):
        """Default (non-claude) backend still spawns `kiro-cli acp --agent <name>`."""
        client = AcpClient(work_dir=tmp_path)
        with (
            patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
            patch(
                "kiro_crew.acp.client.wrap_argv",
                side_effect=lambda argv, mode, **kwargs: (argv, None),
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch("kiro_crew.session._track_pid"),
            patch("kiro_crew.session._track_session_pid"),
        ):
            mock_proc = MagicMock()
            # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process, moved verbatim; tracked for a de-flake pass
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await client._spawn()

            argv = list(strip_spawn_shim(mock_exec.call_args.args))
            assert argv[0] == "/usr/bin/kiro-cli"
            assert argv[1] == "acp"
            assert "--agent" in argv

        await _stop_stderr_drain(client)

    @pytest.mark.asyncio
    async def test_initialize_protocol_version_per_backend(self, tmp_path):
        """kiro expects a date string; claude-agent-acp expects an integer."""
        from kiro_crew.acp.client import (
            PROTOCOL_VERSION,
            PROTOCOL_VERSION_CLAUDE,
        )

        for backend, expected in (
            ("", PROTOCOL_VERSION),
            (ACP_BACKEND_CLAUDE, PROTOCOL_VERSION_CLAUDE),
        ):
            client = AcpClient(work_dir=tmp_path, acp_backend=backend)
            client._session_id = "sess-1"  # short-circuit past the new-session call
            sent_params: dict = {}

            async def fake_send_request(method, params, _sent=sent_params):
                if method == "initialize":
                    _sent.update(params)
                return 1

            async def fake_wait(_req_id, timeout=0, *, method="", expected_mcp=None):
                return {"protocolVersion": expected, "agentCapabilities": {}}

            client._send_request = fake_send_request  # type: ignore[assignment]
            client._wait_for_response = fake_wait  # type: ignore[assignment]
            client._drain_notifications = AsyncMock()  # type: ignore[assignment]

            # Stop after step 1 (initialize) — we only care about the first request.
            try:
                await client._initialize_session()
            except Exception:
                pass
            assert sent_params.get("protocolVersion") == expected, (
                f"backend={backend!r} expected protocolVersion={expected!r}, "
                f"got {sent_params.get('protocolVersion')!r}"
            )


class TestEnsureReadyRetryOnAcpError:
    """Test 2: ensure_ready retries once on AcpError."""

    @pytest.mark.asyncio
    async def test_retries_on_acp_error(self):
        client = AcpClient()
        client._process = None
        client._session_id = None

        call_count = 0

        async def fake_spawn():
            nonlocal call_count
            call_count += 1
            client._process = MagicMock()
            client._process.returncode = None
            client._process.pid = 100 + call_count
            client._process.stderr = None

        async def fake_init():
            if call_count == 1:
                raise AcpError("MCP server crashed")
            client._session_id = "sess-ok"

        def fake_reset():
            client._process = None
            client._session_id = None
            client._pid = None

        client._spawn = fake_spawn
        client._initialize_session = fake_init
        client._kill_process = AsyncMock()
        client._reset_state = fake_reset
        client._snapshot_process_tree = AsyncMock()

        await client.ensure_ready()

        assert call_count == 2
        assert client._session_id == "sess-ok"
        client._kill_process.assert_called_once_with(force=True)

    @pytest.mark.asyncio
    async def test_retries_spawn_os_error_after_backoff(self, monkeypatch):
        client = AcpClient()
        client._work_dir_ready = True
        spawn_count = 0

        async def fake_spawn():
            nonlocal spawn_count
            spawn_count += 1
            if spawn_count == 1:
                raise FileNotFoundError("adapter replaced in place")
            # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process, moved verbatim; tracked for a de-flake pass
            client._process = MagicMock(returncode=None, pid=101)

        async def fake_init():
            client._session_id = "sess-ok"

        sleep = AsyncMock()
        client._spawn = fake_spawn
        client._initialize_session = fake_init
        client._cleanup_failed_live_spawn = AsyncMock()
        client._snapshot_process_tree = AsyncMock()
        monkeypatch.setattr(acp_client.asyncio, "sleep", sleep)

        await client.ensure_ready()

        assert spawn_count == 2
        sleep.assert_awaited_once_with(acp_client._ACP_RESPAWN_BACKOFF_S)
        client._cleanup_failed_live_spawn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancel_during_retry_kill_releases_bound_workspace(self, tmp_path, monkeypatch):
        client = AcpClient(work_dir=tmp_path)
        kill_entered = asyncio.Event()
        released: list[int] = []

        async def fake_spawn():
            # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process, moved verbatim; tracked for a de-flake pass
            client._process = MagicMock(returncode=None, pid=123)
            client._bound_workspace_fd = 77
            client._spawn_work_dir = "/dev/fd/77"

        async def fake_init():
            raise AcpError("init failed")

        async def stalled_kill(*, force=False):
            kill_entered.set()
            await asyncio.Event().wait()

        async def record_release(descriptor):
            released.append(descriptor)

        client._spawn = fake_spawn
        client._initialize_session = fake_init
        client._kill_process = stalled_kill
        monkeypatch.setattr(acp_client, "release_bound_agent_workspace", record_release)

        task = asyncio.create_task(client.ensure_ready())
        await kill_entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert released == [77]
        assert client._bound_workspace_fd is None
        assert client._spawn_work_dir == str(tmp_path)


class TestEnsureReadyRecreatesWorkDir:
    @pytest.mark.asyncio
    async def test_recreates_missing_work_dir(self, tmp_path):
        work_dir = tmp_path / "ws"
        client = AcpClient(work_dir=work_dir)
        client._process = MagicMock()
        client._process.returncode = None
        client._session_id = "sess-1"
        client._spawn = AsyncMock()

        assert not work_dir.exists()
        await client.ensure_ready()
        assert work_dir.is_dir()
        client._spawn.assert_not_called()


class TestSpawnEnvScrub:
    """The default auto/standard ACP spawn path applies the full child scrub.

    The parent-side enforcement is mandatory for raw Windows Kiro delegation;
    POSIX launchers apply the same sensitive/Python scrub inline.
    """

    @pytest.mark.asyncio
    async def test_client_spawn_scrubs_sensitive_env_on_default_auto(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "0000:FAKE-telegram")
        monkeypatch.setenv("WECOM_BOT_ID", "FAKE-wecom-bot")
        monkeypatch.setenv("WECOM_SECRET", "FAKE-wecom-secret")
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-FAKE")
        monkeypatch.setenv("KIROCREW_OWNER_ID", "U_FAKE_OWNER")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "FAKE-secret")
        monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/fake-agent.sock")
        monkeypatch.setenv("PYTHONPATH", "/gateway/pythonpath")
        monkeypatch.setenv("PYTHONHOME", "/gateway/pythonhome")
        # AWS account identity and a benign key are not denied.
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "FAKE-akid")
        monkeypatch.setenv("KIROCREW_UNRELATED_KEEPME", "keep-this-value")

        captured: dict[str, object] = {}

        class _StopSpawn(Exception):
            pass

        async def _fake_exec(*_args, **kwargs):
            captured["env"] = kwargs.get("env")
            raise _StopSpawn()

        # **_ absorbs the environ/home the spawn path now pins, so the search and
        # the "not found" diagnostic cannot read different environments.
        monkeypatch.setattr(acp_client, "_resolve_kiro_bin", lambda **_: "/fake/kiro")
        monkeypatch.setattr(
            acp_client,
            "wrap_argv",
            lambda argv, mode, strip_python_env=False, is_kiro_cli=None, **_kw: (
                argv,
                None,
            ),
        )
        monkeypatch.setattr(acp_client, "cgroup_scope_argv", lambda argv: argv)
        monkeypatch.setattr(acp_client, "augmented_path", lambda p: p)
        monkeypatch.setattr(acp_client, "resolve_krb5_ccname", lambda env: None)
        monkeypatch.setattr(acp_client, "_resolve_ssh_auth_sock", lambda env: None)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

        client = AcpClient(sandbox_mode="auto")  # default tier
        with pytest.raises(_StopSpawn):
            await client._spawn()

        env = captured["env"]
        assert isinstance(env, dict)
        for key in (
            "TELEGRAM_BOT_TOKEN",
            "WECOM_BOT_ID",
            "WECOM_SECRET",
            "SLACK_BOT_TOKEN",
            "KIROCREW_OWNER_ID",
            "AWS_SECRET_ACCESS_KEY",
            "PYTHONPATH",
            "PYTHONHOME",
        ):
            assert key not in env, f"{key} leaked into ACP child env"
        # The default tier leaves ~/.ssh readable, so the agent socket is kept:
        # git-over-SSH works while every other credential above stays scrubbed.
        if sys.platform == "win32":
            # Windows has no SSH_AUTH_SOCK (a named-pipe agent): never forwarded.
            assert "SSH_AUTH_SOCK" not in env, "SSH_AUTH_SOCK leaked into ACP child env"
        else:
            assert env.get("SSH_AUTH_SOCK") == "/tmp/fake-agent.sock"
        assert env.get("KIROCREW_UNRELATED_KEEPME") == "keep-this-value"
        assert env.get("AWS_ACCESS_KEY_ID") == "FAKE-akid"
        assert env.get("KIROCREW_RUNTIME_PYTHON") == sys.executable


def test_resolve_spawn_agent_argv_converts_a_refusal_to_acperror(tmp_path):
    """A projection ``errors`` entry (an unreadable/excluded spec) makes
    ``spawn_agent`` raise a bare ``ValueError``. Raised raw out of ``_spawn`` it
    would escape ``ensure_ready``'s transport ladder uncaught, skipping cleanup;
    the helper must convert it to ``AcpError`` (which the ladder catches) while
    keeping the actionable message, and pass a resolvable name straight through."""
    from kiro_crew.acp.skill_projection import NativeSkillProjection

    client = AcpClient(work_dir=tmp_path, agent="ghost")
    client._native_skill_projection = NativeSkillProjection(
        aliases={}, errors={"ghost": "its spec could not be read"}
    )
    with pytest.raises(AcpError) as excinfo:
        client._resolve_spawn_agent_argv()
    assert "its spec could not be read" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, ValueError)

    # A resolvable launch name passes through unchanged (no raise).
    client._agent = "fine"
    client._native_skill_projection = NativeSkillProjection(aliases={})
    assert client._resolve_spawn_agent_argv() == "fine"
