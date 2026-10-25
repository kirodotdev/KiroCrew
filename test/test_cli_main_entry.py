"""``cli.main()`` around a subcommand, and the ``kirocrew gateway`` command's boot.

``main()`` rejects an unusable ``KIROCREW_PORT``, drops an inherited sandbox marker and records a
direct CLI override's attestation before the jail gate; it dispatches ``mcp-<builtin>`` and
``--seed``; ``kirocrew gateway`` exits with a status a supervisor can act on when the home's lock
refuses it; and ``_install_child_watcher`` installs the right asyncio child watcher, or nothing
where the API is gone. The project-dir pointer path is pinned here too.
"""

import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cli_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _doctor_reads_the_warm_probe_cache,
)

from kiro_crew import platform_compat


class TestPortEnvValidatedAtEntry:
    """`main()` rejects an unusable KIROCREW_PORT before any subcommand runs.

    Type alone is not enough. 70000 parses as an int, so a type-only check let
    `KIROCREW_PORT=70000 kirocrew service install` bake an unbindable port into
    a service definition and report success -- leaving a gateway that dies on
    every start, with the failure surfacing far from its cause.

    Rejecting here rather than in the consumer keeps ONE policy for every entry
    point. It must reject rather than silently drop: dropping would install the
    DEFAULT port while the operator believes they set theirs.
    """

    def test_out_of_range_port_exits_before_dispatch(self, monkeypatch, capsys):
        import sys

        for bad in ("70000", "0", "-1"):
            monkeypatch.setenv("KIROCREW_PORT", bad)
            dispatched = []
            with (
                patch.object(sys, "argv", ["kirocrew", "cron", "list"]),
                patch("kiro_crew.cli_commands._cron", lambda _ns: dispatched.append(True)),
            ):
                from kiro_crew.cli import main

                with pytest.raises(SystemExit) as exc:
                    main()
            assert exc.value.code == 1, bad
            assert not dispatched, f"{bad} reached the subcommand"
            assert "1-65535" in capsys.readouterr().err

    def test_in_range_port_is_accepted(self, monkeypatch):
        import sys

        monkeypatch.setenv("KIROCREW_PORT", "5477")
        dispatched = []
        with (
            patch.object(sys, "argv", ["kirocrew", "cron", "list"]),
            patch("kiro_crew.cli_commands._cron", lambda _ns: dispatched.append(True)),
        ):
            from kiro_crew.cli import main

            main()
        assert dispatched == [True]


class TestSandboxActiveMarkerCleared:
    """cli.main() must drop an INHERITED KIROCREW_SANDBOX_ACTIVE marker.

    The marker is trusted by sandbox.wrap_argv to skip re-wrapping (nested
    passthrough); its only legitimate setter is the namespace launcher's
    in-sandbox main() (a separate process). A value present at the CLI
    entrypoint can only be forged/inherited from the gateway's environment, so
    honoring it would be a full sandbox bypass for every agent/tool spawn.
    """

    def test_main_clears_inherited_sandbox_active_marker(self, monkeypatch):
        import os
        import sys

        monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
        # The companion tier record must be dropped with it: a stale inherited
        # level would be read as the ACTIVE tier by a descendant's passthrough
        # and corrupt its downgrade audit.
        monkeypatch.setenv("KIROCREW_SANDBOX_LEVEL", "strict")
        # A trivial subcommand so main() dispatches and returns cleanly; assert
        # the marker was popped before dispatch (patch the target to observe).
        argv = ["kirocrew", "cron", "list"]
        seen = {}

        def _capture(_ns):
            seen["marker"] = os.environ.get("KIROCREW_SANDBOX_ACTIVE")
            seen["level"] = os.environ.get("KIROCREW_SANDBOX_LEVEL")

        with patch.object(sys, "argv", argv), patch("kiro_crew.cli_commands._cron", _capture):
            from kiro_crew.cli import main

            main()
        assert seen.get("marker") is None
        assert seen.get("level") is None
        assert os.environ.get("KIROCREW_SANDBOX_ACTIVE") is None
        assert os.environ.get("KIROCREW_SANDBOX_LEVEL") is None


class TestDirectCliOverrideAttestation:
    def test_agent_command_pins_override_before_jail_gate(self, tmp_path, monkeypatch):
        """A direct CLI agent command must pin its override before any re-exec or spawn."""
        from kiro_crew import cli, kiro_prerequisite

        executable = tmp_path / "kiro-cli"
        executable.write_bytes(b"direct CLI override")
        executable.chmod(0o700)
        data_home = tmp_path / "data"
        observed = {}

        monkeypatch.setenv("KIROCREW_HOME", str(data_home))
        monkeypatch.setenv("KIROCREW_KIRO_BIN", str(executable))
        monkeypatch.setattr(cli, "boot_platform", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(cli.sys, "argv", ["kirocrew", "chat", "--no-jail"])
        # Exercise the POSIX-only override contract on every CI platform.
        register_attestation = kiro_prerequisite.register_process_start_override_attestation
        monkeypatch.setattr(
            kiro_prerequisite,
            "register_process_start_override_attestation",
            lambda: register_attestation(
                platform_name="linux",
                environ=cli.os.environ,
            ),
        )

        def inspect_before_provider(_command, _no_jail):
            # Read the PINNED attestation (not a fresh hash) so this asserts the
            # override was recorded before the gate, not merely hashable at it.
            observed["digest"] = kiro_prerequisite._OPERATOR_OVERRIDE_ATTESTATIONS.get(
                os.path.normcase(str(executable))
            )
            raise SystemExit(0)

        monkeypatch.setattr(cli, "_jail_reexec_gate", inspect_before_provider)

        with pytest.raises(SystemExit, match="0"):
            cli.main()

        assert observed["digest"] == hashlib.sha256(executable.read_bytes()).hexdigest()


class TestMcpBuiltinDispatch:
    """Tests for dynamic mcp-<builtin> dispatch (coverlay: cli.py L705-707)."""

    def test_mcp_builtin_dispatches_to_module(self, monkeypatch):
        """CLI 'mcp-<builtin>' dynamically imports and runs the builtin's mcp_server.

        The test must not depend on which builtins ``BUILTIN_NAMES`` lists,
        so register a synthetic builtin name to exercise the dispatch path
        (cli.py subparser registration + dynamic import).
        """
        import kiro_crew.cli as cli_mod

        builtin_name = "fakebuiltin"
        # Patch the registry the CLI reads when building subparsers and dispatching.
        monkeypatch.setattr(cli_mod, "_BUILTIN_NAMES", [builtin_name])
        # The verb is only registered (and dispatched) when the builtin's
        # mcp_server module resolves — make the synthetic one resolve.
        monkeypatch.setattr(cli_mod, "_builtin_mcp_server_available", lambda _name: True)
        mock_module = MagicMock()

        monkeypatch.setattr(sys, "argv", ["kirocrew", f"mcp-{builtin_name}"])
        with patch("importlib.import_module", return_value=mock_module) as mock_import:
            cli_mod.main()

        mock_import.assert_called_once_with(f"kiro_crew.apps.builtins.{builtin_name}.mcp_server")
        mock_module.run_mcp_server.assert_called_once_with()


class TestProjectDirFile:
    """Tests for _project_dir_file helper (coverlay: cli.py L59-61)."""

    def test_returns_config_dir_path(self, monkeypatch, tmp_path):
        """_project_dir_file should return config_dir() / 'project_dir'."""
        monkeypatch.setattr("kiro_crew.cli.config_dir", lambda: tmp_path)
        from kiro_crew.cli import _project_dir_file

        assert _project_dir_file() == tmp_path / "project_dir"


class TestSeedDispatch:
    """Tests for --seed dispatch before gateway startup (coverlay: cli.py L624-627)."""

    def test_gateway_dispatch_clears_an_inherited_stall_alarm_before_the_gateway_starts(
        self, monkeypatch
    ):
        """An in-app restart reaches this image through ``os.execv``, which
        preserves ``ITIMER_REAL``; the dispatch clears a deadline this image never
        armed, once, and before the gateway coroutine exists."""
        monkeypatch.setattr(sys, "argv", ["kirocrew", "gateway"])
        order: list[str] = []
        # A plain MagicMock, as the sibling tests use: ``patch`` would otherwise
        # stand an AsyncMock in for the coroutine function, whose side effect runs
        # only when awaited, and ``asyncio.run`` is patched out here.
        mock_gateway = MagicMock(side_effect=lambda **_kw: order.append("gateway") or object())
        with (
            patch(
                "kiro_crew.dashboard.loop_watchdog.disarm_inherited_alarm",
                side_effect=lambda: order.append("disarm") or True,
            ) as disarm,
            patch("kiro_crew.cli_server._gateway", mock_gateway),
            patch("kiro_crew.cli.asyncio.run"),
        ):
            from kiro_crew.cli import main

            main()
        disarm.assert_called_once_with()
        mock_gateway.assert_called_once()
        assert order == ["disarm", "gateway"]

    def test_gateway_dispatch_line_buffers_stdout_before_the_gateway_starts(self, monkeypatch):
        """Every gateway launcher reaches this branch, and the first status
        print happens inside the gateway coroutine, so the stdout seam must run
        here, once, before that coroutine exists."""
        monkeypatch.setattr(sys, "argv", ["kirocrew", "gateway"])
        order: list[str] = []
        mock_gateway = MagicMock(side_effect=lambda **_kw: order.append("gateway") or object())
        with (
            patch(
                "kiro_crew.platform_compat.ensure_line_buffered_stdout",
                side_effect=lambda: order.append("line-buffer"),
            ) as line_buffer,
            patch("kiro_crew.cli_server._gateway", mock_gateway),
            patch("kiro_crew.cli.asyncio.run"),
        ):
            from kiro_crew.cli import main

            main()
        line_buffer.assert_called_once_with()
        assert order == ["line-buffer", "gateway"]

    @pytest.mark.parametrize(
        "extra_argv, expect_replace",
        [
            pytest.param([], False, id="test_seed_calls_seed_cmd"),
            pytest.param(["--seed-replace"], True, id="test_seed_with_replace_flag"),
        ],
    )
    def test_seed_runs_before_gateway(self, monkeypatch, extra_argv, expect_replace):
        """--seed (optionally with --seed-replace) hands the parsed flags to seed_cmd, then the gateway runs."""
        monkeypatch.setattr(sys, "argv", ["kirocrew", "gateway", "--seed", "demo", *extra_argv])
        mock_seed = MagicMock(return_value=0)
        gateway_call = object()
        mock_gateway = MagicMock(return_value=gateway_call)
        with (
            patch("kiro_crew.cli.seed_cmd", mock_seed),
            patch("kiro_crew.cli_server._gateway", mock_gateway),
            patch("kiro_crew.cli.asyncio.run") as mock_run,
        ):
            from kiro_crew.cli import main

            main()
        mock_seed.assert_called_once()
        seed_args = mock_seed.call_args.args[0]
        assert (seed_args.seed, seed_args.seed_replace) == ("demo", expect_replace)
        mock_gateway.assert_called_once()
        mock_run.assert_called_once_with(gateway_call)

    def test_seed_nonzero_exits(self, monkeypatch):
        """When seed_cmd returns non-zero, CLI should sys.exit with that code."""
        monkeypatch.setattr(sys, "argv", ["kirocrew", "gateway", "--seed", "bad"])
        mock_seed = MagicMock(return_value=1)
        with patch("kiro_crew.cli.seed_cmd", mock_seed):
            from kiro_crew.cli import main

            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_no_seed_skips_seed_cmd(self, monkeypatch):
        """When --seed is not provided, seed_cmd should not be called."""
        monkeypatch.setattr(sys, "argv", ["kirocrew", "gateway"])
        mock_seed = MagicMock()
        gateway_call = object()
        mock_gateway = MagicMock(return_value=gateway_call)
        with (
            patch("kiro_crew.cli.seed_cmd", mock_seed),
            patch("kiro_crew.cli_server._gateway", mock_gateway),
            patch("kiro_crew.cli.asyncio.run") as mock_run,
        ):
            from kiro_crew.cli import main

            main()
        mock_seed.assert_not_called()
        mock_gateway.assert_called_once()
        mock_run.assert_called_once_with(gateway_call)


class TestGatewayLockRefusalExit:
    """How ``kirocrew gateway`` exits when the home's lock refuses it.

    A supervisor reads nothing but the exit status, so the status has to say
    whether relaunching can help. A live holder is a sibling gateway that keeps
    serving the home for as long as it runs, so a relaunch meets the identical
    refusal every time; every other refusal is one a later attempt may find
    cleared. The systemd unit exempts exactly the first status from
    ``Restart=always`` (see ``test_service.py``), so the mapping here and the
    rendered unit must agree on one constant.
    """

    def _refused_exit_code(self, monkeypatch, error):
        monkeypatch.setattr(sys, "argv", ["kirocrew", "gateway"])
        lock_cls = MagicMock()
        lock_cls.return_value.acquire.side_effect = error
        with (
            patch("kiro_crew.cli.GatewayLock", lock_cls),
            patch("kiro_crew.cli_server._gateway") as mock_gateway,
            patch("kiro_crew.cli.asyncio.run") as mock_run,
            # The gateway path arms faulthandler on the real stderr descriptor,
            # which a capsys-replaced stream does not have.
            patch("kiro_crew.cli.faulthandler.enable"),
        ):
            from kiro_crew.cli import main

            with pytest.raises(SystemExit) as excinfo:
                main()
        # A refused lock must never reach the gateway body.
        mock_gateway.assert_not_called()
        mock_run.assert_not_called()
        return excinfo.value.code

    def test_a_live_holder_refusal_exits_the_terminal_code(self, monkeypatch, tmp_path, capsys):
        from kiro_crew.gateway_lock import LIVE_HOLDER_EXIT_CODE, GatewayLockError

        error = GatewayLockError(
            tmp_path,
            4242,
            f"{tmp_path} is held by pid 4242 (holds port 5476, answering HTTP) -- another "
            f"gateway already owns {tmp_path}",
            live_holder=True,
        )
        assert self._refused_exit_code(monkeypatch, error) == LIVE_HOLDER_EXIT_CODE
        # The whole point: not the restartable status a supervisor retries.
        assert LIVE_HOLDER_EXIT_CODE not in (0, 1)
        assert "another gateway already owns" in capsys.readouterr().err

    def test_every_other_lock_refusal_keeps_the_restartable_exit(self, monkeypatch, tmp_path):
        from kiro_crew.gateway_lock import GatewayLockError

        error = GatewayLockError(
            tmp_path,
            None,
            f"{tmp_path} is being replaced faster than it can be locked",
        )
        assert error.live_holder is False
        assert self._refused_exit_code(monkeypatch, error) == 1

    def test_an_unidentified_owner_on_the_port_keeps_the_restartable_exit(
        self, monkeypatch, tmp_path
    ):
        """The recorded pid is alive and on the port, but no surface names the acquirer.

        Built from the REAL diagnosis rather than a hand-made error: without
        ``/proc/locks`` (or on a filesystem it never matches) the lock file's pid
        is the only fact, and a reused pid number that happens to listen on the
        port must not stand the unit down. The refusal keeps exit 1 so systemd
        retries; only a positively identified acquirer earns the terminal code.
        """
        from kiro_crew import gateway_lock, platform_compat

        (tmp_path / gateway_lock.LOCK_FILENAME).write_text("4242\n", encoding="utf-8")
        monkeypatch.setattr(platform_compat, "try_acquire_lock", lambda *a, **k: False)
        monkeypatch.setattr(platform_compat, "flock_owner_pid", lambda _p: None)
        monkeypatch.setattr(platform_compat, "pids_holding_file", lambda _p: None)
        monkeypatch.setattr(platform_compat, "pid_exists", lambda pid: pid == 4242)
        monkeypatch.setattr(
            platform_compat,
            "find_port_listeners",
            lambda _p: [platform_compat.PortListener(4242, "127.0.0.1", "4")],
        )
        with pytest.raises(gateway_lock.GatewayLockError) as excinfo:
            gateway_lock.GatewayLock(tmp_path, port=5477).acquire()
        error = excinfo.value
        assert error.holder_pid == 4242 and "holds port 5477" in str(error)
        assert error.live_holder is False
        assert self._refused_exit_code(monkeypatch, error) == 1

    @pytest.mark.parametrize("answers_http", [True, False])
    def test_an_identified_owner_on_the_port_is_terminal_only_when_it_answers_http(
        self, monkeypatch, tmp_path, answers_http
    ):
        """Built from the REAL diagnosis: ``/proc/locks`` names a live acquirer on the port.

        Answering HTTP is what makes it a gateway serving this home, and only
        then does the process exit the terminal code. A holder that listens
        without answering is a wedged gateway: a hung process keeps its socket
        bound, and a terminal exit here would park the unit `failed` with nothing
        left to relaunch once that process dies -- so the exit stays 1.
        """
        from kiro_crew import gateway_lock, platform_compat

        (tmp_path / gateway_lock.LOCK_FILENAME).write_text("16968\n", encoding="utf-8")
        monkeypatch.setattr(platform_compat, "try_acquire_lock", lambda *a, **k: False)
        monkeypatch.setattr(platform_compat, "flock_owner_pid", lambda _p: 16968)
        monkeypatch.setattr(platform_compat, "pid_exists", lambda _p: True)
        monkeypatch.setattr(platform_compat, "pids_holding_file", lambda _p: [16968])
        monkeypatch.setattr(platform_compat, "process_thread_count", lambda _p: 118)
        monkeypatch.setattr(
            platform_compat,
            "find_port_listeners",
            lambda _p: [platform_compat.PortListener(16968, "127.0.0.1", "4")],
        )
        monkeypatch.setattr(gateway_lock, "_port_answers_http", lambda *_a, **_k: answers_http)
        with pytest.raises(gateway_lock.GatewayLockError) as excinfo:
            gateway_lock.GatewayLock(tmp_path, port=5477).acquire()
        error = excinfo.value
        assert error.holder_pid == 16968
        assert error.live_holder is answers_http
        expected = gateway_lock.LIVE_HOLDER_EXIT_CODE if answers_http else 1
        assert self._refused_exit_code(monkeypatch, error) == expected

    # The serving-holder predicate's truth table (``GatewayLock._serving_verdict``),
    # one row per shape a refusal can take. ``owner`` is what ``/proc/locks`` names
    # (None = no owner surface: macOS, Windows, a filesystem the lock table never
    # matches); ``recorded`` is the pid stamped in the lock file; ``listeners`` are
    # the LISTEN sockets the enumeration finds on the configured port, as
    # ``(pid, address, family)``; ``answers_at`` is the set of addresses at which
    # the port answers HTTP; ``bind`` is what this gateway is configured to bind
    # (``KIROCREW_BIND``); ``replaced`` reaches the lock through the
    # deleted-lock-file path (the home anchor's acquirer). Expectation:
    # ``live_holder`` and the exit code ``kirocrew gateway`` takes.
    _OWN = (16968, "127.0.0.1", "4")
    _TRUTH_TABLE = [
        # -- identified acquirer (the /proc/locks owner), lock-file path -------------
        pytest.param(
            dict(owner=16968, alive=True, listeners=[_OWN], answers_at={"127.0.0.1"}),
            True,
            id="identified-alive-port-answering",
        ),
        pytest.param(
            dict(owner=16968, alive=True, listeners=[_OWN], answers_at=set()),
            False,
            id="identified-alive-port-silent",
        ),
        pytest.param(
            dict(owner=16968, alive=True, listeners=[], answers_at={"127.0.0.1"}),
            False,
            id="identified-alive-no-port",
        ),
        pytest.param(
            dict(owner=16968, alive=False, listeners=[_OWN], answers_at={"127.0.0.1"}),
            False,
            id="identified-dead-acquirer",
        ),
        pytest.param(
            dict(owner=16968, alive=True, listeners=[_OWN], answers_at={"127.0.0.1"}, port=None),
            False,
            id="identified-alive-no-port-configured",
        ),
        # -- the ADDRESS conjunct: the owner's own socket must be the one the probe reaches
        pytest.param(
            dict(
                owner=16968,
                alive=True,
                listeners=[(16968, "10.20.30.40", "4"), (999, "127.0.0.1", "4")],
                answers_at={"127.0.0.1"},
            ),
            False,
            id="stranger-answers-at-probe-address-owner-bound-elsewhere",
        ),
        pytest.param(
            dict(owner=16968, alive=True, listeners=[(16968, "*", "")], answers_at={"127.0.0.1"}),
            False,
            id="owner-address-unknowable",
        ),
        # -- configured bind: the predicate probes THIS gateway's bind, wildcards by family
        pytest.param(
            dict(
                owner=16968,
                alive=True,
                listeners=[(16968, "0.0.0.0", "4")],
                answers_at={"127.0.0.1"},
                bind="0.0.0.0",
            ),
            True,
            id="configured-bind-v4-wildcard-probes-loopback",
        ),
        pytest.param(
            dict(
                owner=16968,
                alive=True,
                listeners=[(16968, "::", "6")],
                answers_at={"::1"},
                bind="::",
            ),
            True,
            id="configured-bind-v6-wildcard-probes-v6-loopback",
        ),
        pytest.param(
            dict(
                owner=16968,
                alive=True,
                listeners=[(16968, "::1", "6")],
                answers_at={"::1"},
                bind="::1",
            ),
            True,
            id="configured-bind-v6-loopback-probed-there",
        ),
        # -- the RESIDUAL row, unasserted by design: the owner holds the port only elsewhere
        pytest.param(
            dict(owner=16968, alive=True, listeners=[(16968, "::1", "6")], answers_at={"::1"}),
            False,
            id="residual-holder-bound-to-another-address",
        ),
        # -- no owner surface: the predicate is never asked, the recorded pid decides nothing
        pytest.param(
            dict(
                owner=None,
                alive=True,
                listeners=[(4242, "127.0.0.1", "4")],
                answers_at={"127.0.0.1"},
            ),
            False,
            id="unidentified-recorded-alive-port-answering",
        ),
        pytest.param(
            dict(owner=None, alive=True, listeners=[], answers_at={"127.0.0.1"}),
            False,
            id="unidentified-recorded-alive-no-port",
        ),
        pytest.param(
            dict(
                owner=None,
                alive=False,
                listeners=[(4242, "127.0.0.1", "4")],
                answers_at={"127.0.0.1"},
            ),
            False,
            id="unidentified-recorded-dead",
        ),
        # -- lock file deleted or replaced: the home anchor's acquirer, same predicate
        pytest.param(
            dict(
                owner=16968, alive=True, listeners=[_OWN], answers_at={"127.0.0.1"}, replaced=True
            ),
            True,
            id="replaced-lock-file-answering",
        ),
        pytest.param(
            dict(owner=16968, alive=True, listeners=[_OWN], answers_at=set(), replaced=True),
            False,
            id="replaced-lock-file-silent",
        ),
        pytest.param(
            dict(
                owner=16968,
                alive=True,
                listeners=[(16968, "10.20.30.40", "4"), (999, "127.0.0.1", "4")],
                answers_at={"127.0.0.1"},
                replaced=True,
            ),
            False,
            id="replaced-lock-file-stranger-at-probe-address",
        ),
        pytest.param(
            dict(
                owner=None,
                alive=True,
                listeners=[(4242, "127.0.0.1", "4")],
                answers_at={"127.0.0.1"},
                replaced=True,
            ),
            False,
            id="replaced-lock-file-unidentified",
        ),
    ]

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="the home anchor rows need POSIX flock")
    @pytest.mark.parametrize("row, live", _TRUTH_TABLE)
    def test_live_holder_truth_table(self, monkeypatch, tmp_path, row, live):
        """Every row of the serving-holder predicate, judged by the REAL diagnosis and exit.

        The lock is forced to read as held; the owner surface, liveness, the
        listener enumeration (pid and bound address) and the HTTP probe are the
        parameters. Exactly one shape is terminal -- an identified acquirer,
        alive, whose OWN socket on the configured port is the one the probe
        reaches at the address this gateway is configured to bind, answering
        HTTP there -- and every other row keeps the restartable exit 1: the
        residual "bound elsewhere" row, a stranger answering at the probe
        address on the same port, and an owner whose socket address the
        platform did not report included.
        """
        from kiro_crew import gateway_lock, platform_compat

        owner = row["owner"]
        recorded = 4242
        port = row.get("port", 5477)
        answers_at = row["answers_at"]
        listeners = [platform_compat.PortListener(*entry) for entry in row["listeners"]]
        probes: list[str] = []

        (tmp_path / gateway_lock.LOCK_FILENAME).write_text(f"{recorded}\n", encoding="utf-8")
        if row.get("replaced"):
            # The lock FILE locks fine (it was re-created), the home DIRECTORY is held.
            monkeypatch.setattr(
                platform_compat,
                "try_acquire_lock",
                lambda fd, exclusive=True: not stat.S_ISDIR(os.fstat(fd).st_mode),
            )
            monkeypatch.setattr(
                gateway_lock,
                "_directory_locks_supported",
                lambda _home: (gateway_lock._DirectoryLockSupport.SUPPORTED, None),
            )
        else:
            monkeypatch.setattr(platform_compat, "try_acquire_lock", lambda *a, **k: False)
        monkeypatch.setattr(platform_compat, "flock_owner_pid", lambda _p: owner)
        monkeypatch.setattr(platform_compat, "pids_holding_file", lambda _p: None)
        monkeypatch.setattr(platform_compat, "parent_pid", lambda _p: 1)
        monkeypatch.setattr(platform_compat, "process_thread_count", lambda _p: 3)
        monkeypatch.setattr(
            platform_compat, "pid_exists", lambda pid: row["alive"] and pid in (owner, recorded)
        )
        monkeypatch.setattr(platform_compat, "find_port_listeners", lambda _p: listeners)

        def _answers(probed_port, *_a, host="127.0.0.1", **_k):
            probes.append(host)
            return probed_port == port and host in answers_at

        monkeypatch.setattr(gateway_lock, "_port_answers_http", _answers)

        with pytest.raises(gateway_lock.GatewayLockError) as excinfo:
            gateway_lock.GatewayLock(tmp_path, port=port, bind_address=row.get("bind")).acquire()
        error = excinfo.value
        assert error.live_holder is live
        if owner is None:
            # No identified owner: the predicate is never asked, so nothing is probed.
            assert probes == []
        elif live is False and row["alive"] and row["listeners"]:
            own = [e for e in listeners if e.pid == owner]
            if own and not any(
                gateway_lock._listener_reaches(e, gateway_lock.probe_host_for_bind(row.get("bind")))
                for e in own
            ):
                # Bound elsewhere (or unknowable): the probe is never made, so a
                # stranger's answer at the probe address cannot be credited to the owner.
                assert probes == []
        expected_exit = gateway_lock.LIVE_HOLDER_EXIT_CODE if live else 1
        assert self._refused_exit_code(monkeypatch, error) == expected_exit

    @pytest.mark.parametrize(
        "override",
        ["", "   ", "127.0.0.1", "0.0.0.0", "::", "::1", "10.20.30.40", "not-an-ip", "[::1]"],
    )
    def test_diagnostic_bind_address_is_the_bind_the_gateway_resolves(self, monkeypatch, override):
        """The lock probes the bind the gateway itself resolves -- one resolver, not a copy.

        ``_diagnostic_bind_address`` calls ``dashboard.urls.bind_address_for`` with
        ``local_only=True``: a valid ``KIROCREW_BIND`` is the bind either way, and
        the two fallbacks that flag can produce (loopback, the v4 wildcard) are
        probed at the same loopback address, so the flag cannot change the answer.
        """
        from kiro_crew.cli import _diagnostic_bind_address
        from kiro_crew.dashboard.urls import bind_address_for
        from kiro_crew.gateway_lock import probe_host_for_bind

        monkeypatch.setenv("KIROCREW_BIND", override)
        assert _diagnostic_bind_address() == bind_address_for(local_only=True)
        assert probe_host_for_bind(bind_address_for(local_only=False)) == probe_host_for_bind(
            bind_address_for(local_only=True)
        )


@pytest.mark.skipif(
    not hasattr(__import__("asyncio"), "set_child_watcher"),
    reason="asserts the 3.10-3.13 child-watcher semantics; the API was removed in 3.14",
)
class TestInstallPidfdChildWatcher:
    """Verify _install_child_watcher's platform behavior."""

    @staticmethod
    def _install_then_spawn_in_child(expected_watcher: str, cwd: Path) -> None:
        """Run "_install_child_watcher() then asyncio.run(subprocess)" in a CLEAN
        child Python process, and assert it exits 0.

        *cwd* is the caller's ``tmp_path``: the child imports ``kiro_crew`` off the
        propagated ``PYTHONPATH``, never off its working directory, so the one
        thing the process cwd could still do is let a ``-c`` child run inside the
        developer's checkout.

        Why a subprocess instead of an in-process ``asyncio.run``: on CPython
        3.10 the child watcher is bound to the loop inside asyncio's
        ``set_event_loop()``, which only calls ``attach_loop`` when
        ``threading.current_thread() is threading.main_thread()``. pytest-xdist
        runs test bodies on a NON-main worker thread, so an in-process
        ``asyncio.run`` here skips ``attach_loop`` and the freshly-installed
        watcher reports inactive -> ``create_subprocess_exec`` raises
        "child watcher not activated" (a harness artifact, not a product bug).
        A child ``python -c`` always runs on its own MAIN thread -- exactly like
        ``kirocrew gateway`` -- so this deterministically exercises the real
        install-before-run attach path regardless of the worker thread.
        """
        import os
        import textwrap

        code = textwrap.dedent("""
            import asyncio
            from kiro_crew.cli import _install_child_watcher

            async def _spawn_true():
                proc = await asyncio.create_subprocess_exec(
                    "true",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
                return proc.returncode

            _install_child_watcher()  # mirror the gateway: install BEFORE run
            assert type(asyncio.get_child_watcher()).__name__ == {expected!r}, (
                "expected {expected} to be installed"
            )
            assert asyncio.run(_spawn_true()) == 0
            """).format(expected=expected_watcher)
        # Propagate the runtime's import path so the child can import kiro_crew
        # (a bare subprocess would not inherit it without PYTHONPATH).
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
        # flake-ok: pre-existing 60 s bound on the cold-import child, which also bounds the child's own proc.wait(); tracked for a de-flake pass
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env=env,
            cwd=cwd,
        )
        assert result.returncode == 0, (
            f"install-before-run child failed (rc={result.returncode}):\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

    def test_installs_safe_watcher_on_macos(self, monkeypatch) -> None:
        import asyncio

        from kiro_crew.cli import _install_child_watcher

        monkeypatch.setattr("kiro_crew.cli.sys.platform", "darwin")
        # On macOS (no pidfd) we install the SIGCHLD-based SafeChildWatcher to
        # eliminate the thread-per-child reaper storm, and must NOT touch the
        # Linux pidfd path. Spy on set_child_watcher; make the pidfd probe and
        # the PidfdChildWatcher ctor explode so reaching them fails the test.
        called = []
        monkeypatch.setattr(asyncio, "set_child_watcher", lambda w: called.append(w))
        # raising=False: these watcher classes are Unix-only and do not exist
        # on Windows, where the test still simulates the macOS install path.
        monkeypatch.setattr(asyncio, "SafeChildWatcher", lambda: "fake-safe-watcher", raising=False)

        def _boom(*_a) -> object:
            raise AssertionError("Linux pidfd path must not be reached on macOS")

        monkeypatch.setattr("kiro_crew.cli.os.pidfd_open", _boom, raising=False)
        monkeypatch.setattr(asyncio, "PidfdChildWatcher", _boom, raising=False)
        _install_child_watcher()
        assert called == ["fake-safe-watcher"], "macOS must install SafeChildWatcher"

    def test_noop_when_safe_watcher_unavailable(self, monkeypatch) -> None:
        import asyncio

        from kiro_crew.cli import _install_child_watcher

        monkeypatch.setattr("kiro_crew.cli.sys.platform", "darwin")
        # Simulate a runtime where SafeChildWatcher was removed (3.14+) or never
        # existed (Windows): the installer must leave the default watcher in
        # place rather than raise.
        monkeypatch.delattr(asyncio, "SafeChildWatcher", raising=False)
        called = []
        monkeypatch.setattr(asyncio, "set_child_watcher", lambda w: called.append(w))
        _install_child_watcher()  # must not raise
        assert called == [], "no watcher installed when SafeChildWatcher is unavailable"

    def test_sets_watcher_on_linux(self, monkeypatch) -> None:
        import asyncio

        from kiro_crew.cli import _install_child_watcher

        monkeypatch.setattr("kiro_crew.cli.sys.platform", "linux")
        # Kernel supports pidfd_open -> probe succeeds -> watcher installed.
        # Fully fake the probe (sentinel fd + no-op close) so no real fd is used.
        opened = []
        closed = []
        monkeypatch.setattr(
            "kiro_crew.cli.os.pidfd_open", lambda pid: opened.append(pid) or 4242, raising=False
        )
        monkeypatch.setattr("kiro_crew.cli.os.close", lambda fd: closed.append(fd))
        called_with = []
        monkeypatch.setattr(asyncio, "set_child_watcher", lambda w: called_with.append(w))
        monkeypatch.setattr(
            asyncio, "PidfdChildWatcher", lambda: "fake-pidfd-watcher", raising=False
        )
        _install_child_watcher()
        assert opened, "pidfd_open must be probed before installing"
        assert closed == [4242], "the probe fd must be closed"
        assert called_with == ["fake-pidfd-watcher"]

    def test_falls_back_to_safe_watcher_on_old_kernel(self, monkeypatch) -> None:
        import asyncio

        from kiro_crew.cli import _install_child_watcher

        monkeypatch.setattr("kiro_crew.cli.sys.platform", "linux")
        # Real 3.10 failure mode: PidfdChildWatcher.__init__ does NOT probe the
        # kernel, so the < 5.3 failure surfaces as os.pidfd_open raising OSError.
        # PidfdChildWatcher must NEVER be constructed (else the first
        # create_subprocess_exec would ENOSYS), but we must ALSO NOT leave the
        # default ThreadedChildWatcher in place -- its thread-per-child reaper
        # storm is the wedge this installer exists to prevent. So a < 5.3 kernel
        # falls back to the SIGCHLD-based SafeChildWatcher instead.

        def _no_pidfd(_pid):
            raise OSError(38, "Function not implemented")  # ENOSYS

        monkeypatch.setattr("kiro_crew.cli.os.pidfd_open", _no_pidfd, raising=False)
        installed = []
        monkeypatch.setattr(asyncio, "set_child_watcher", lambda w: installed.append(w))
        monkeypatch.setattr(asyncio, "SafeChildWatcher", lambda: "fake-safe-watcher", raising=False)

        def _ctor_must_not_run() -> object:
            raise AssertionError("PidfdChildWatcher must not be constructed when pidfd_open fails")

        monkeypatch.setattr(asyncio, "PidfdChildWatcher", _ctor_must_not_run, raising=False)
        _install_child_watcher()  # must not raise
        assert installed == ["fake-safe-watcher"], (
            "a < 5.3 kernel must fall back to SafeChildWatcher, not the "
            "thread-storm ThreadedChildWatcher"
        )

    def test_falls_back_to_safe_watcher_when_pidfd_open_missing(self, monkeypatch) -> None:
        import asyncio

        from kiro_crew.cli import _install_child_watcher

        monkeypatch.setattr("kiro_crew.cli.sys.platform", "linux")
        # The gateway startup kill scenario: a uv-managed /
        # Clang-built CPython 3.12 whose build omits the os.pidfd_open wrapper
        # (present on the system python, absent in the venv interpreter). The
        # probe raises AttributeError, not OSError -- the old code caught it and
        # RETURNED, leaving the thread-per-child ThreadedChildWatcher, whose
        # os.waitpid reaper-thread storm starved the loop and got the gateway
        # killed by the loop-stall watchdog. It must now fall back to
        # SafeChildWatcher instead.
        monkeypatch.delattr("kiro_crew.cli.os.pidfd_open", raising=False)
        installed = []
        monkeypatch.setattr(asyncio, "set_child_watcher", lambda w: installed.append(w))
        monkeypatch.setattr(asyncio, "SafeChildWatcher", lambda: "fake-safe-watcher", raising=False)

        def _ctor_must_not_run() -> object:
            raise AssertionError(
                "PidfdChildWatcher must not be constructed when os.pidfd_open is missing"
            )

        monkeypatch.setattr(asyncio, "PidfdChildWatcher", _ctor_must_not_run, raising=False)
        _install_child_watcher()  # must not raise
        assert installed == ["fake-safe-watcher"], (
            "a Python build without os.pidfd_open must fall back to "
            "SafeChildWatcher, not the thread-storm ThreadedChildWatcher"
        )

    @pytest.mark.skipif(sys.platform != "linux", reason="pidfd watcher is Linux-only")
    def test_real_subprocess_works_after_install_on_linux(self, tmp_path: Path) -> None:
        """End-to-end: after installing the watcher the way the gateway does
        (before asyncio.run, on the main thread), asyncio subprocess support must
        still work. This is the property the mocked test above cannot prove — it
        guards against the watcher being installed but never attached to the loop
        (which would make every create_subprocess_exec raise RuntimeError).

        Runs in a clean child process (its own main thread) so it is immune to
        pytest-xdist executing this test body on a non-main worker thread, where
        set_event_loop's main-thread-guarded attach_loop would be skipped.

        The expected watcher is derived from the SAME probe the installer uses
        rather than hard-coded: a Python build that omits the ``os.pidfd_open``
        wrapper (observed on uv-managed CPython) correctly installs
        SafeChildWatcher instead — the documented fallback. Asserting
        PidfdChildWatcher unconditionally made this test fail on such an
        interpreter even though the product behaved exactly as designed.
        """
        try:
            fd = os.pidfd_open(os.getpid())
            os.close(fd)
        except (OSError, AttributeError):
            expected = "SafeChildWatcher"
        else:
            expected = "PidfdChildWatcher"
        self._install_then_spawn_in_child(expected_watcher=expected, cwd=tmp_path)

    @pytest.mark.skipif(
        sys.platform == "linux" or not hasattr(__import__("asyncio"), "SafeChildWatcher"),
        reason="exercises the real macOS SafeChildWatcher install (non-Linux Unix, 3.10-3.13)",
    )
    def test_real_subprocess_works_after_safe_watcher_install_on_macos(
        self, tmp_path: Path
    ) -> None:
        """End-to-end on macOS: after the REAL _install_child_watcher() installs
        SafeChildWatcher the way the gateway does (before asyncio.run, on the main
        thread), asyncio subprocess support must still work.

        This is the macOS counterpart to the Linux end-to-end test and the
        property the fully-mocked test_installs_safe_watcher_on_macos cannot
        prove: that SafeChildWatcher actually ATTACHES to the loop (its SIGCHLD
        handler) rather than being installed but inert — which would make every
        create_subprocess_exec raise RuntimeError('...not activated...').

        Runs in a clean child process (its own main thread): SafeChildWatcher's
        attach_loop installs a SIGCHLD handler via loop.add_signal_handler, which
        is itself main-thread-only, so an in-process run under a non-main
        pytest-xdist worker thread would fail spuriously.
        """
        self._install_then_spawn_in_child(expected_watcher="SafeChildWatcher", cwd=tmp_path)


class TestChildWatcherApiRemoved:
    """``_install_child_watcher()`` must no-op when the child-watcher API is gone.

    CPython 3.14 removed ``set_child_watcher`` / ``PidfdChildWatcher`` /
    ``SafeChildWatcher`` (the event loop reaps children directly). The Linux
    pidfd branch referenced those names unconditionally, so on 3.14 the FIRST
    thing ``kirocrew gateway`` did was raise ``AttributeError: module 'asyncio'
    has no attribute 'set_child_watcher'`` -- the gateway died before binding
    its port, while every other subcommand (``chat``, ``doctor``) kept working
    because this installer is only called on the gateway path.
    """

    @staticmethod
    def _remove_child_watcher_api(monkeypatch: pytest.MonkeyPatch) -> None:
        """Make asyncio look like 3.14: no child-watcher API at all."""
        import asyncio

        for name in (
            "set_child_watcher",
            "get_child_watcher",
            "PidfdChildWatcher",
            "SafeChildWatcher",
            "ThreadedChildWatcher",
            "AbstractChildWatcher",
        ):
            monkeypatch.delattr(asyncio, name, raising=False)

    @pytest.mark.parametrize("platform", ["linux", "darwin"])
    def test_noop_when_child_watcher_api_removed(self, monkeypatch, platform) -> None:
        """Simulated removal, so 3.10-3.13 CI also protects the 3.14 code path."""
        import asyncio

        from kiro_crew.cli import _install_child_watcher

        monkeypatch.setattr("kiro_crew.cli.sys.platform", platform)
        # A pidfd-capable kernel is the WORST case: without the guard the Linux
        # branch reaches `asyncio.set_child_watcher(asyncio.PidfdChildWatcher())`
        # and raises. Keep the probe succeeding so the regression is real.
        # Hand back a REAL fd (the real os.close then reclaims it) rather than
        # stubbing os.close: `kiro_crew.cli.os` IS the shared os module, so a
        # stub there would also be seen by asyncio's own internals.
        monkeypatch.setattr(
            "kiro_crew.cli.os.pidfd_open",
            lambda pid: os.open(os.devnull, os.O_RDONLY),
            raising=False,
        )
        self._remove_child_watcher_api(monkeypatch)

        _install_child_watcher()  # must not raise

        assert not hasattr(
            asyncio, "set_child_watcher"
        ), "the guard must not resurrect the removed API"

    @pytest.mark.skipif(
        hasattr(__import__("asyncio"), "set_child_watcher"),
        reason="only meaningful on a runtime that really removed the API (3.14+)",
    )
    def test_real_subprocess_works_after_noop_install(self) -> None:
        """On a real 3.14+ runtime, the gateway's install-before-run call must be
        survivable AND leave subprocess support working.

        No monkeypatching: the API is genuinely absent here, so this asserts the
        property that actually matters (spawning still works after the no-op)
        rather than the mechanism. 3.14 reaps children in the event loop with a
        single non-thread reaper, so nothing is lost by not installing a watcher.
        """
        import asyncio

        from kiro_crew.cli import _install_child_watcher

        _install_child_watcher()  # mirror the gateway: install BEFORE run

        async def _spawn_true() -> int:
            proc = await asyncio.create_subprocess_exec(
                "true",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            # flake-ok: pre-existing wait on a real `true` child with no timeout, moved verbatim; tracked for a de-flake pass
            await proc.wait()
            return proc.returncode

        assert asyncio.run(_spawn_true()) == 0
