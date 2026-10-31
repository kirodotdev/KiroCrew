"""What ``kirocrew restart`` waits for after the spawn, and what it reports.

``TestRestartReadinessVerdict`` pins that restart reports success only once the replacement
serves; ``_wait_gateway_ready``, the ``KIROCREW_RESTART_READY_TIMEOUT`` deadline and the
gateway.log lines a failed restart quotes are pinned beside it.
"""

import contextlib
import types
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cli_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _doctor_reads_the_warm_probe_cache,
)

from kiro_crew.service.common import RestartReport


class TestRestartReadinessVerdict:
    """`restart` must report success only once the REPLACEMENT is serving.

    Deliberately a separate class from :class:`TestRestart`: that class's autouse
    ``_fast_restart_ready`` fixture pins ``_wait_gateway_ready`` to ``ready`` (and
    ``_RESTART_TOKEN_WAIT`` to 0) so its dispatch assertions don't need a live
    gateway — which is exactly the behaviour under test here, so inheriting it
    would mask every one of these tests.

    Before this verdict existed, ``restart`` printed "✅ Started detached gateway
    (pid N)" straight off the ``Popen`` pid and exited 0, so a replacement that
    the ``KIROCREW_HOME`` ownership guard refused (exit 1, milliseconds later)
    reported success with NO gateway running.
    """

    def _drive(self, *, poll, ready_status, marker_pid, timeout=None):
        """Drive ``_restart``'s fork path with a scripted replacement gateway.

        Returns ``(exit_code, mock_sel, mock_token_url)`` — ``exit_code`` is
        ``None`` when ``_restart`` returned normally — so both the happy and the
        failing path can assert the audited outcome.

        Nothing here ever sleeps for real: the timeout is collapsed, and the wait
        loop checks its deadline only AFTER probing, so every case resolves on the
        first pass.
        """
        from kiro_crew import cli_server

        mock_sel = MagicMock()
        stack = [
            patch("kiro_crew.cli_server.sel", return_value=mock_sel),
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=RestartReport(),
            ),
            # Keep the denied-service branch out of these verdict tests (and
            # keep them off the host's real systemctl/launchctl state).
            patch(
                "kiro_crew.cli_server.service_controller.is_service_active",
                return_value=False,
            ),
            patch("kiro_crew.cli_server.service_controller.installed_unit_path", return_value=None),
            patch(
                "kiro_crew.cli_server.platform_compat.find_listening_pids",
                return_value=[],
            ),
            patch(
                "kiro_crew.cli_server.platform_compat.listening_pid_tool_available",
                return_value=True,
            ),
            patch("kiro_crew.cli_server.run_marker.read_pid", side_effect=marker_pid),
            patch("kiro_crew.cli_server._gateway_owns_port", return_value=True),
            patch("kiro_crew.cli_server._probe_gateway_ready", return_value=ready_status),
            patch(
                "kiro_crew.cli_server._spawn_detached_gateway",
                # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process, moved verbatim; the process double is never signalled
                return_value=MagicMock(pid=4321, poll=MagicMock(return_value=poll)),
            ),
            patch("kiro_crew.cli_server._print_token_url"),
            patch(
                "kiro_crew.cli_server._resolve_restart_ready_timeout",
                return_value=0 if timeout is None else timeout,
            ),
        ]
        with contextlib.ExitStack() as es:
            patched = [es.enter_context(p) for p in stack]
            code = None
            try:
                cli_server._restart(None)
            except SystemExit as exc:
                code = exc.code
        return code, mock_sel, patched[-2]

    @staticmethod
    def _outcomes(mock_sel):
        return [c.kwargs["outcome"] for c in mock_sel.log_api_access.call_args_list]

    def test_replacement_that_dies_is_reported_as_a_failure(self, capsys):
        """An immediately-exiting replacement must exit non-zero, not print ✅."""
        code, mock_sel, mock_token_url = self._drive(
            poll=1, ready_status=0, marker_pid=[None, None]
        )

        assert code == 1
        out = capsys.readouterr().out
        assert "✅" not in out
        assert "died immediately" in out
        # The exit status is the diagnosis (1 == refused by the ownership guard),
        # and the log is where the reason is.
        assert "exit status 1" in out
        assert "4321" in out
        assert "kirocrew logs -f" in out
        # The audit must record what actually happened, not an optimistic
        # "allowed" logged before any verdict existed.
        assert self._outcomes(mock_sel) == ["denied"]
        resources = mock_sel.log_api_access.call_args.kwargs["resources"]
        assert "reason=replacement_died exit=1" in resources
        # No point chasing a token for a gateway that is not there.
        mock_token_url.assert_not_called()

    def test_replacement_that_dies_relays_only_its_own_log_lines(self, tmp_path, capsys):
        """The died-immediately report quotes what the replacement wrote, not older lines.

        The replacement rotates the old, longer gateway.log to .prev at boot and logs its
        refusal into a fresh file, so the relay has to follow the rotation.
        """
        from kiro_crew import cli_server

        log = tmp_path / "gateway.log"
        log.write_text("ERROR old gateway line from yesterday\n" * 50, encoding="utf-8")
        real_mark = cli_server._gateway_log_mark

        def mark_then_boot():
            mark = real_mark()
            log.replace(log.with_suffix(".log.prev"))
            log.write_text(
                "ERROR Refusing to start: persistence preflight failed\nfix: free disk space\n",
                encoding="utf-8",
            )
            return mark

        with (
            patch("kiro_crew.cli_server.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_server._gateway_log_mark", side_effect=mark_then_boot),
        ):
            code, _sel, _url = self._drive(poll=1, ready_status=0, marker_pid=[None, None])

        assert code == 1
        out = capsys.readouterr().out
        assert "Its last lines in gateway.log:" in out
        assert "Refusing to start: persistence preflight failed" in out
        assert "fix: free disk space" in out
        assert "old gateway line" not in out

    def test_replacement_that_never_becomes_ready_relays_no_log_lines(self, tmp_path, capsys):
        """A still-running replacement is not dead, so its log is not quoted as the cause."""
        (tmp_path / "gateway.log").write_text("INFO still booting\n", encoding="utf-8")

        with (
            patch("kiro_crew.cli_server.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_server._gateway_log_mark", return_value=(None, None, 0)),
        ):
            code, _sel, _url = self._drive(poll=None, ready_status=503, marker_pid=[None, None])

        assert code == 1
        out = capsys.readouterr().out
        assert "did not become ready" in out
        assert "still booting" not in out

    def test_replacement_that_dies_on_a_truncated_key_prints_the_repair_steps(
        self, capsys, tmp_path
    ):
        """The gateway's refusal goes to gateway.log; the restart terminal gets the steps too."""
        key = tmp_path / "token_signing.key"
        with (
            patch("kiro_crew.sandbox.agent_confinement_evidence", return_value=None),
            patch(
                "kiro_crew.dashboard.token_secret.signing_key_health",
                return_value=("short", key),
            ),
            patch(
                "kiro_crew.service.common.restart_command_hint",
                return_value="kirocrew restart",
            ),
        ):
            code, _sel, _url = self._drive(poll=1, ready_status=0, marker_pid=[None, None])

        assert code == 1
        out = capsys.readouterr().out
        assert "died immediately" in out
        assert f"token signing key at {key} is shorter than 32 bytes" in out
        assert "Repair it by hand:" in out
        # Every step the gateway prints, in order, ending on the restart.
        from kiro_crew.dashboard import token_secret

        steps = token_secret.signing_key_remedy(key, "kirocrew restart")
        positions = [out.index(step) for step in steps]
        assert positions == sorted(positions)
        assert "kirocrew restart" in steps[-1]

    @pytest.mark.parametrize("state", ["ok", "absent", "masked", "other", "unstatable"])
    def test_replacement_that_dies_on_a_healthy_key_prints_no_repair_steps(
        self, capsys, tmp_path, state
    ):
        # Only a truncated key gets the remedy: the steps delete the file, so a
        # sandbox mask or a healthy key must never be handed them.
        with (
            patch("kiro_crew.sandbox.agent_confinement_evidence", return_value=None),
            patch(
                "kiro_crew.dashboard.token_secret.signing_key_health",
                return_value=(state, tmp_path / "token_signing.key"),
            ),
        ):
            code, _sel, _url = self._drive(poll=1, ready_status=0, marker_pid=[None, None])

        assert code == 1
        out = capsys.readouterr().out
        assert "died immediately" in out
        assert "Repair it by hand" not in out

    @pytest.mark.parametrize(
        ("kirocrew_own", "expect_steps"),
        [
            # A foreign Seatbelt (kiro-cli's, an operator wrapper) can leave the
            # real key readable: the boot preflight refuses it as short, so the
            # restart must print the steps too, as doctor does.
            (False, True),
            # Kiro Crew's own sandbox masks the leaf whatever its readability.
            (True, False),
        ],
    )
    def test_a_readable_short_key_under_a_seatbelt_follows_doctor(
        self, capsys, tmp_path, kirocrew_own, expect_steps
    ):
        key = tmp_path / "token_signing.key"
        key.write_bytes(b"short")
        key.chmod(0o600)
        with (
            patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
            patch("kiro_crew.sandbox.agent_confinement_evidence", return_value="seatbelt"),
            patch(
                "kiro_crew.sandbox.agent_confinement_is_kirocrew_own",
                return_value=kirocrew_own,
            ),
            patch(
                "kiro_crew.service.common.restart_command_hint",
                return_value="kirocrew restart",
            ),
        ):
            code, _sel, _url = self._drive(poll=1, ready_status=0, marker_pid=[None, None])

        assert code == 1
        out = capsys.readouterr().out
        assert "died immediately" in out
        assert ("Repair it by hand:" in out) is expect_steps

    def test_a_short_key_this_process_cannot_open_gets_no_removal_steps(self, capsys, tmp_path):
        # Inside an agent sandbox whose confinement signals both missed, the
        # mode-0 mask over a healthy key reads as short; doctor withholds the
        # steps that delete the file, and so must restart.
        with (
            patch("kiro_crew.sandbox.agent_confinement_evidence", return_value=None),
            patch(
                "kiro_crew.dashboard.token_secret.signing_key_health",
                return_value=("short", tmp_path / "token_signing.key"),
            ),
            patch("kiro_crew.dashboard.token_secret.open_is_denied", return_value=True),
        ):
            code, _sel, _url = self._drive(poll=1, ready_status=0, marker_pid=[None, None])

        assert code == 1
        out = capsys.readouterr().out
        assert "died immediately" in out
        assert "Repair it by hand" not in out

    def test_relayed_refusal_is_not_followed_by_a_second_copy_of_the_steps(self, capsys, tmp_path):
        # The relayed gateway.log lines already hold the gateway's refusal and its
        # repair steps, so the restart does not print them again under the relay.
        relayed = ["ERROR Refusing to start: token signing key is shorter than 32 bytes"]
        with (
            patch("kiro_crew.sandbox.agent_confinement_evidence", return_value=None),
            patch(
                "kiro_crew.dashboard.token_secret.signing_key_health",
                return_value=("short", tmp_path / "token_signing.key"),
            ),
            patch("kiro_crew.cli_server._gateway_log_lines_since", return_value=relayed),
        ):
            code, _sel, _url = self._drive(poll=1, ready_status=0, marker_pid=[None, None])

        assert code == 1
        out = capsys.readouterr().out
        assert "Its last lines in gateway.log:" in out
        assert relayed[0] in out
        assert "Repair it by hand" not in out

    def test_a_failing_key_probe_leaves_the_died_report_unchanged(self, capsys):
        with patch(
            "kiro_crew.dashboard.token_secret.signing_key_health",
            side_effect=OSError("stale file handle"),
        ):
            code, _sel, _url = self._drive(poll=1, ready_status=0, marker_pid=[None, None])

        assert code == 1
        out = capsys.readouterr().out
        assert "died immediately" in out
        assert "kirocrew logs -f" in out
        assert "Repair it by hand" not in out

    def test_replacement_that_never_becomes_ready_is_reported_as_a_failure(self, capsys):
        """A live-but-not-serving replacement must exit non-zero with timeout wording."""
        code, mock_sel, mock_token_url = self._drive(
            poll=None, ready_status=503, marker_pid=[None, None]
        )

        assert code == 1
        out = capsys.readouterr().out
        assert "✅" not in out
        assert "did not become ready" in out
        assert "4321" in out
        assert self._outcomes(mock_sel) == ["denied"]
        assert "reason=replacement_not_ready_within=" in (
            mock_sel.log_api_access.call_args.kwargs["resources"]
        )
        mock_token_url.assert_not_called()

    def test_ready_replacement_reports_success_and_audits_allowed(self, capsys):
        """The success line survives verbatim — it just has to wait for the verdict."""
        code, mock_sel, mock_token_url = self._drive(
            poll=None,
            ready_status=200,
            # No marker before the stop; the replacement records pid 4321.
            marker_pid=[None, 4321],
        )

        assert code is None
        assert "✅ Started detached gateway (pid 4321)" in capsys.readouterr().out
        assert self._outcomes(mock_sel) == ["allowed"]
        mock_token_url.assert_called_once()

    def test_old_gateway_answering_the_port_is_not_the_replacement(self, capsys):
        """A 200 from the OUTGOING gateway must not be read as the new one.

        The incumbent keeps serving until its socket closes, so during the
        handover the port can answer 200 while the run-marker still names the old
        pid. Accepting that would report success for the process we just asked to
        die — the failure mode the ``_gateway_start_id`` handshake in dev-fleet
        exists to avoid.
        """
        code, _mock_sel, mock_token_url = self._drive(
            poll=None,
            ready_status=200,
            # 1234 before the stop AND still 1234 while polling: the marker never
            # changed hands, so this is the old gateway.
            marker_pid=[1234, 1234, 1234],
        )

        assert code == 1
        out = capsys.readouterr().out
        assert "✅" not in out
        assert "did not become ready" in out
        mock_token_url.assert_not_called()


class TestGatewayLogRelay:
    """``_gateway_log_lines_since``: what a failed restart quotes from gateway.log."""

    @staticmethod
    def _lines(tmp_path, content: bytes, offset):
        """Relay *content* as if gateway.log measured *offset* bytes before the spawn."""
        from kiro_crew import cli_server

        log = tmp_path / "gateway.log"
        log.write_bytes(content)
        st = log.stat()
        mark = None if offset is None else (st.st_dev, st.st_ino, offset)
        with patch("kiro_crew.cli_server.config_dir", return_value=tmp_path):
            return cli_server._gateway_log_lines_since(mark)

    @staticmethod
    def _rotate_and_relay(tmp_path, old: bytes, before_rotation: bytes, after: bytes):
        """Mark a non-empty gateway.log, then do what a booting replacement does.

        The replacement's inherited stdout appends *before_rotation* to the measured
        file, ``cli._setup_cli_logging`` renames it to gateway.log.prev (the same
        ``Path.replace`` it runs at boot), and *after* lands in a fresh gateway.log.
        """
        from kiro_crew import cli_server

        log = tmp_path / "gateway.log"
        log.write_bytes(old)
        with patch("kiro_crew.cli_server.config_dir", return_value=tmp_path):
            mark = cli_server._gateway_log_mark()
            with open(log, "ab") as fh:
                fh.write(before_rotation)
            log.replace(log.with_suffix(".log.prev"))
            log.write_bytes(after)
            return cli_server._gateway_log_lines_since(mark)

    def test_an_unknown_offset_relays_nothing(self, tmp_path):
        assert self._lines(tmp_path, b"ERROR something\n", None) == []

    def test_a_log_that_did_not_grow_relays_nothing(self, tmp_path):
        assert self._lines(tmp_path, b"old\n", 4) == []

    def test_a_missing_log_relays_nothing(self, tmp_path):
        from kiro_crew import cli_server

        with patch("kiro_crew.cli_server.config_dir", return_value=tmp_path):
            mark = cli_server._gateway_log_mark()
            assert mark == (None, None, 0)
            assert cli_server._gateway_log_lines_since(mark) == []

    def test_a_log_created_after_the_mark_is_relayed_whole(self, tmp_path):
        from kiro_crew import cli_server

        with patch("kiro_crew.cli_server.config_dir", return_value=tmp_path):
            mark = cli_server._gateway_log_mark()
            (tmp_path / "gateway.log").write_bytes(b"ERROR Refusing to start\n")
            assert cli_server._gateway_log_lines_since(mark) == ["ERROR Refusing to start"]

    def test_a_rotated_log_shorter_than_the_old_one_is_still_relayed(self, tmp_path):
        """The boot rotation starts a fresh file, so the old size is no offset into it."""
        old = b"INFO old gateway line\n" * 200
        got = self._rotate_and_relay(
            tmp_path, old, b"", b"ERROR Refusing to start: persistence preflight failed\n"
        )
        assert got == ["ERROR Refusing to start: persistence preflight failed"]

    def test_a_rotated_log_longer_than_the_old_one_is_read_from_its_start(self, tmp_path):
        old = b"INFO old\n"
        after = b"INFO replacement booting\n" * 3 + b"ERROR no worker thread\n"
        got = self._rotate_and_relay(tmp_path, old, b"", after)
        assert got == ["INFO replacement booting"] * 3 + ["ERROR no worker thread"]

    def test_output_written_before_the_rotation_is_relayed_from_prev(self, tmp_path):
        got = self._rotate_and_relay(
            tmp_path,
            b"INFO old gateway line\n",
            b"WARNING written before logging setup\n",
            b"ERROR signing-key preflight timed out\n",
        )
        assert got == [
            "WARNING written before logging setup",
            "ERROR signing-key preflight timed out",
        ]

    def test_a_prev_file_from_an_earlier_boot_is_not_relayed(self, tmp_path):
        """A gateway.log.prev that is not the measured file belongs to an older gateway."""
        from kiro_crew import cli_server

        log = tmp_path / "gateway.log"
        (tmp_path / "gateway.log.prev").write_bytes(b"ERROR from last week\n")
        log.write_bytes(b"INFO old\n")
        with patch("kiro_crew.cli_server.config_dir", return_value=tmp_path):
            mark = cli_server._gateway_log_mark()
            # A size rollover renames the measured file away; it stays alive as .1, so
            # its inode cannot be reused by the fresh gateway.log.
            log.replace(tmp_path / "gateway.log.1")
            log.write_bytes(b"ERROR Refusing to start\n")
            assert cli_server._gateway_log_lines_since(mark) == ["ERROR Refusing to start"]

    def test_only_the_last_lines_are_kept_and_blank_lines_dropped(self, tmp_path):
        from kiro_crew import cli_server

        body = "".join(f"line {i}\n\n" for i in range(50)).encode()
        got = self._lines(tmp_path, body, 0)
        assert len(got) == cli_server._RESTART_LOG_RELAY_LINES
        assert got[-1] == "line 49"
        assert got[0] == f"line {50 - cli_server._RESTART_LOG_RELAY_LINES}"

    def test_terminal_control_sequences_are_stripped(self, tmp_path):
        got = self._lines(tmp_path, b"\x1b]0;pwned\x07ERROR \x1b[31mred\x1b[0m\x9b\n", 0)
        assert len(got) == 1
        assert "\x1b" not in got[0] and "\x07" not in got[0]
        assert "ERROR" in got[0] and "red" in got[0]

    def test_a_line_cut_by_the_byte_cap_is_dropped(self, tmp_path):
        from kiro_crew import cli_server

        cap = cli_server._RESTART_LOG_RELAY_BYTES
        body = b"x" * (cap + 10) + b"\nlast line\n"
        got = self._lines(tmp_path, body, 0)
        assert got == ["last line"]

    def test_a_long_line_is_capped(self, tmp_path):
        from kiro_crew import cli_server

        got = self._lines(tmp_path, b"y" * 5000 + b"\n", 0)
        assert got == ["y" * cli_server._RESTART_LOG_RELAY_LINE_CHARS]


class TestResolveRestartReadyTimeout:
    """`KIROCREW_RESTART_READY_TIMEOUT` parsing for the restart readiness deadline."""

    def test_unset_uses_default(self, monkeypatch):
        from kiro_crew import cli_server

        monkeypatch.delenv("KIROCREW_RESTART_READY_TIMEOUT", raising=False)

        assert cli_server._resolve_restart_ready_timeout() == 60

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("90", 90),
            ("45.7", 46),
            ("0.5", 15),
            ("179.2", 180),
            ("1", 15),
            ("14", 15),
            ("181", 180),
            ("100000", 180),
            ("abc", 60),
            ("", 60),
            ("0", 60),
            ("-5", 60),
            ("inf", 60),
            ("-inf", 60),
            ("nan", 60),
        ],
    )
    def test_env_value_is_parsed_and_clamped(self, monkeypatch, raw, expected):
        from kiro_crew import cli_server

        monkeypatch.setenv("KIROCREW_RESTART_READY_TIMEOUT", raw)

        assert cli_server._resolve_restart_ready_timeout() == expected

    @pytest.mark.parametrize("raw", ["", "   ", "90", "45.7", "15", "180", "179.2"])
    def test_unset_or_in_range_value_is_silent(self, monkeypatch, capsys, raw):
        from kiro_crew import cli_server

        monkeypatch.setenv("KIROCREW_RESTART_READY_TIMEOUT", raw)

        cli_server._resolve_restart_ready_timeout()

        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out == ""

    @pytest.mark.parametrize(
        "raw, message",
        [
            ("abc", "KIROCREW_RESTART_READY_TIMEOUT='abc' is not a positive number; using 60s."),
            ("-5", "KIROCREW_RESTART_READY_TIMEOUT='-5' is not a positive number; using 60s."),
            ("nan", "KIROCREW_RESTART_READY_TIMEOUT='nan' is not a positive number; using 60s."),
            ("300", "KIROCREW_RESTART_READY_TIMEOUT='300' is outside 15..180s; using 180s."),
            ("5", "KIROCREW_RESTART_READY_TIMEOUT='5' is outside 15..180s; using 15s."),
            ("0.5", "KIROCREW_RESTART_READY_TIMEOUT='0.5' is outside 15..180s; using 15s."),
        ],
    )
    def test_fallback_or_clamp_prints_one_stderr_line(self, monkeypatch, capsys, raw, message):
        from kiro_crew import cli_server

        monkeypatch.setenv("KIROCREW_RESTART_READY_TIMEOUT", raw)

        cli_server._resolve_restart_ready_timeout()

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.count("\n") == 1
        assert message in captured.err

    def test_cli_spec_states_the_current_deadline_values(self):
        from kiro_crew import cli_server

        spec = Path(__file__).resolve().parent.parent / "docs/system-specs/modules/cli.md"
        text = " ".join(spec.read_text(encoding="utf-8").split())

        default = cli_server._RESTART_READY_TIMEOUT_DEFAULT
        low = cli_server._RESTART_READY_TIMEOUT_MIN
        high = cli_server._RESTART_READY_TIMEOUT_MAX
        checkpoint = cli_server._RESTART_READY_SOFT_CHECKPOINT
        token_wait = cli_server._RESTART_TOKEN_WAIT
        for phrase in (
            f"it defaults to {default} s because",
            f"clamped to {low}..{high} s",
            f"When the deadline exceeds {checkpoint} s",
            f"on the first check after {checkpoint} s",
            f"keeps its fixed {token_wait} s budget",
        ):
            assert phrase in text, f"cli.md no longer states {phrase!r}"


class TestWaitGatewayReady:
    """Unit tests for the post-spawn readiness wait (`_wait_gateway_ready`).

    The integration-level behaviour lives in
    :class:`TestRestartReadinessVerdict`; this class pins the loop's own rules:
    early-death short-circuit, the changed-marker-pid discriminator, and the
    documented degradation on hosts where `_gateway_owns_port` cannot pass.
    """

    @staticmethod
    def _proc(poll_values):
        # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process, moved verbatim; the process double is never signalled
        return MagicMock(pid=4321, poll=MagicMock(side_effect=list(poll_values)))

    def test_dead_child_short_circuits_with_its_exit_status(self):
        """A refused replacement must be reported at once, not waited out."""
        from kiro_crew import cli_server

        probe = MagicMock(return_value=0)
        with (
            patch("kiro_crew.cli_server._probe_gateway_ready", probe),
            # flake-ok: pre-existing stdlib time.sleep stub scoped to this with block, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-stdlib-clock-rebound -- pre-existing patch of the stdlib time.sleep the code under test reads, moved verbatim; tracked for a de-flake pass
            patch("kiro_crew.cli_server.time.sleep") as mock_sleep,
        ):
            verdict = cli_server._wait_gateway_ready(self._proc([1]), 7777, None, timeout=999)

        assert verdict == (cli_server._READY_DIED, 1)
        # Straight out of the loop: no probe, no sleep, no 999s stall.
        probe.assert_not_called()
        mock_sleep.assert_not_called()

    def test_zero_timeout_still_probes_once(self):
        """A collapsed timeout must report what is there, not a reflex failure."""
        from kiro_crew import cli_server

        probe = MagicMock(return_value=200)
        with (
            patch("kiro_crew.cli_server._probe_gateway_ready", probe),
            patch("kiro_crew.cli_server._replacement_is_serving", return_value=True),
        ):
            verdict = cli_server._wait_gateway_ready(self._proc([None]), 7777, None, timeout=0)

        assert verdict == (cli_server._READY_OK, None)
        probe.assert_called_once_with(7777)

    def test_not_ready_within_deadline_is_a_timeout(self):
        from kiro_crew import cli_server

        # Two polls: the loop's entry poll and the re-poll taken at the deadline
        # before the timeout verdict. Alive for both, so the verdict is TIMEOUT.
        with patch("kiro_crew.cli_server._probe_gateway_ready", return_value=503):
            verdict = cli_server._wait_gateway_ready(
                self._proc([None, None]), 7777, None, timeout=0
            )

        assert verdict == (cli_server._READY_TIMEOUT, None)

    def test_polls_until_the_replacement_answers(self):
        """A slow-booting gateway is waited for rather than failed."""
        from kiro_crew import cli_server

        with (
            patch("kiro_crew.cli_server._probe_gateway_ready", side_effect=[0, 503, 200]),
            patch("kiro_crew.cli_server._replacement_is_serving", return_value=True),
            # flake-ok: pre-existing stdlib time.sleep stub scoped to this with block, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-stdlib-clock-rebound -- pre-existing patch of the stdlib time.sleep the code under test reads, moved verbatim; tracked for a de-flake pass
            patch("kiro_crew.cli_server.time.sleep") as mock_sleep,
        ):
            verdict = cli_server._wait_gateway_ready(
                self._proc([None, None, None]), 7777, None, timeout=999
            )

        assert verdict == (cli_server._READY_OK, None)
        assert mock_sleep.call_count == 2

    def test_probe_reports_zero_for_a_listener_that_does_not_speak_http(self):
        """A wedged fork holding the port must yield "not ready", not a traceback.

        ``http.client.BadStatusLine`` is neither an ``OSError`` nor a
        ``URLError``, so it escapes the connection-failure handler unless caught
        explicitly -- and a non-HTTP listener on the port is precisely the
        situation restart is being run to clear.
        """
        import http.client

        from kiro_crew import cli_server

        with patch(
            "kiro_crew.cli_server.loopback_urlopen",
            side_effect=http.client.BadStatusLine("garbage"),
        ):
            assert cli_server._probe_gateway_ready(7777) == 0

    def test_child_that_exits_during_the_last_probe_is_reported_as_died(self):
        """Exiting on the final probe must not be reported as "still running".

        Otherwise the operator is sent looking for a process that is already gone,
        with no exit status to explain it.
        """
        import types

        from kiro_crew import cli_server

        proc = MagicMock()
        # Alive for the loop's entry poll, exited by the deadline re-poll.
        proc.poll.side_effect = [None, 3]

        # The clock is stubbed on cli_server's OWN attribute rather than through
        # `cli_server.time.monotonic`: that path resolves to the shared `time`
        # module, so it would swap the clock for every caller in the process --
        # including background threads -- and a finite side_effect list them lets
        # steal a value and raise StopIteration here. This stub is scoped to the
        # module under test and answers any number of calls: the first reads the
        # loop's entry time, every later one is past the deadline.
        calls: list[float] = []

        def clock() -> float:
            calls.append(0.0)
            return 0.0 if len(calls) == 1 else 100.0

        fake_time = types.SimpleNamespace(monotonic=clock, sleep=lambda _seconds: None)

        with (
            patch("kiro_crew.cli_server._probe_gateway_ready", return_value=503),
            patch.object(cli_server, "time", fake_time),
        ):
            verdict, status = cli_server._wait_gateway_ready(proc, 7777, None, 0.0)

        assert verdict == cli_server._READY_DIED
        assert status == 3

    @staticmethod
    def _scripted_clock(values):
        """A cli_server-scoped ``time`` stand-in whose clock walks *values*, then holds."""
        remaining = list(values)

        def clock() -> float:
            return remaining.pop(0) if len(remaining) > 1 else remaining[0]

        return types.SimpleNamespace(monotonic=clock, sleep=lambda _seconds: None)

    def test_slow_boot_prints_the_still_starting_line_once(self, capsys):
        from kiro_crew import cli_server

        # start=0, then one reading per failed probe: before, at and past the
        # checkpoint, all inside the 60s deadline. The fourth probe is ready.
        fake_time = self._scripted_clock([0.0, 5.0, 16.0, 30.0])
        with (
            patch("kiro_crew.cli_server._probe_gateway_ready", side_effect=[503, 503, 503, 200]),
            patch("kiro_crew.cli_server._replacement_is_serving", return_value=True),
            patch.object(cli_server, "time", fake_time),
        ):
            verdict = cli_server._wait_gateway_ready(
                self._proc([None, None, None, None]), 7777, None, timeout=60
            )

        assert verdict == (cli_server._READY_OK, None)
        out = capsys.readouterr().out
        assert out.count("Still starting") == 1
        assert "Still starting (16s elapsed), continuing to wait..." in out

    def test_clamp_minimum_deadline_never_prints_the_still_starting_line(self, capsys):
        from kiro_crew import cli_server

        fake_time = self._scripted_clock([0.0, 5.0, 14.9, 15.0])
        with (
            patch("kiro_crew.cli_server._probe_gateway_ready", return_value=503),
            patch.object(cli_server, "time", fake_time),
        ):
            verdict = cli_server._wait_gateway_ready(
                self._proc([None, None, None, None]), 7777, None, timeout=15
            )

        assert verdict == (cli_server._READY_TIMEOUT, None)
        assert "Still starting" not in capsys.readouterr().out

    def test_checkpoint_then_deadline_prints_once_and_times_out(self, capsys):
        from kiro_crew import cli_server

        fake_time = self._scripted_clock([0.0, 10.0, 20.0, 40.0, 60.0])
        with (
            patch("kiro_crew.cli_server._probe_gateway_ready", return_value=503),
            patch.object(cli_server, "time", fake_time),
        ):
            verdict = cli_server._wait_gateway_ready(self._proc([None] * 5), 7777, None, timeout=60)

        assert verdict == (cli_server._READY_TIMEOUT, None)
        out = capsys.readouterr().out
        assert out.count("Still starting") == 1
        assert "Still starting (20s elapsed)" in out

    def test_ready_on_first_probe_prints_nothing(self, capsys):
        from kiro_crew import cli_server

        fake_time = self._scripted_clock([0.0, 100.0])
        with (
            patch("kiro_crew.cli_server._probe_gateway_ready", return_value=200),
            patch("kiro_crew.cli_server._replacement_is_serving", return_value=True),
            patch.object(cli_server, "time", fake_time),
        ):
            verdict = cli_server._wait_gateway_ready(self._proc([None]), 7777, None, timeout=60)

        assert verdict == (cli_server._READY_OK, None)
        assert "Still starting" not in capsys.readouterr().out

    def test_early_death_returns_without_probing_or_the_checkpoint_line(self, capsys):
        from kiro_crew import cli_server

        probe = MagicMock(return_value=0)
        fake_time = self._scripted_clock([0.0, 100.0])
        with (
            patch("kiro_crew.cli_server._probe_gateway_ready", probe),
            patch.object(cli_server, "time", fake_time),
        ):
            verdict = cli_server._wait_gateway_ready(self._proc([1]), 7777, None, timeout=60)

        assert verdict == (cli_server._READY_DIED, 1)
        probe.assert_not_called()
        assert "Still starting" not in capsys.readouterr().out

    def test_missing_marker_is_never_the_replacement(self):
        """An absent marker is the handover's own state, not proof of a new gateway.

        ``clear_marker`` runs on graceful shutdown before the outgoing gateway's
        ``_shutdown()``, so mid-restart there is a window with no marker and the
        old socket still answering. Accepting that as the replacement would
        report the outgoing gateway's 200 as the new one's — and with no listener
        lookup available, nothing downstream would catch it.
        """
        from kiro_crew import cli_server

        with (
            patch("kiro_crew.cli_server.run_marker.read_pid", return_value=None),
            patch("kiro_crew.cli_server.platform_compat.IS_POSIX", False),
        ):
            assert cli_server._replacement_is_serving(7777, 1234) is False
            # Also unproven when there was no prior identity to exclude.
            assert cli_server._replacement_is_serving(7777, None) is False

    def test_unchanged_marker_pid_is_the_old_gateway(self):
        """The discriminator: same recorded pid as before the stop == not the new one."""
        from kiro_crew import cli_server

        with patch("kiro_crew.cli_server.run_marker.read_pid", return_value=1234):
            assert cli_server._replacement_is_serving(7777, 1234) is False

    def test_changed_marker_pid_that_owns_the_port_is_the_replacement(self):
        from kiro_crew import cli_server

        with (
            patch("kiro_crew.cli_server.run_marker.read_pid", return_value=4321),
            patch("kiro_crew.cli_server.platform_compat.IS_POSIX", True),
            patch(
                "kiro_crew.cli_server.platform_compat.listening_pid_tool_available",
                return_value=True,
            ),
            patch("kiro_crew.cli_server._gateway_owns_port", return_value=True) as mock_owns,
        ):
            assert cli_server._replacement_is_serving(7777, 1234) is True
        mock_owns.assert_called_once_with(7777)

    def test_non_posix_degrades_to_the_marker_comparison(self):
        """Windows must not fail restart: `_gateway_owns_port` denies outright there.

        Requiring it would make every Windows restart report "never became ready"
        for a perfectly healthy gateway, so the ownership proof is only applied
        where it can pass and the marker comparison stands alone elsewhere.
        """
        from kiro_crew import cli_server

        with (
            patch("kiro_crew.cli_server.run_marker.read_pid", return_value=4321),
            patch("kiro_crew.cli_server.platform_compat.IS_POSIX", False),
            patch("kiro_crew.cli_server._gateway_owns_port", return_value=False) as mock_owns,
        ):
            assert cli_server._replacement_is_serving(7777, 1234) is True
        mock_owns.assert_not_called()

    def test_posix_without_a_listener_lookup_degrades_too(self):
        """No lsof/netstat means `_gateway_owns_port` can never pass — degrade."""
        from kiro_crew import cli_server

        with (
            patch("kiro_crew.cli_server.run_marker.read_pid", return_value=4321),
            patch("kiro_crew.cli_server.platform_compat.IS_POSIX", True),
            patch(
                "kiro_crew.cli_server.platform_compat.listening_pid_tool_available",
                return_value=False,
            ),
            patch("kiro_crew.cli_server._gateway_owns_port", return_value=False) as mock_owns,
        ):
            assert cli_server._replacement_is_serving(7777, None) is True
        mock_owns.assert_not_called()

    def test_probe_targets_api_ready_not_api_health(self):
        """Readiness, not liveness: a bound socket is not a serving gateway."""
        from kiro_crew import cli_server

        resp = MagicMock(status=200)
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        with patch("kiro_crew.cli_server.loopback_urlopen", return_value=resp) as mock_open:
            assert cli_server._probe_gateway_ready(7777) == 200
        assert mock_open.call_args.args[0] == "http://127.0.0.1:7777/api/ready"

    def test_probe_reports_zero_when_unreachable(self):
        from kiro_crew import cli_server

        with patch(
            "kiro_crew.cli_server.loopback_urlopen", side_effect=urllib.error.URLError("down")
        ):
            assert cli_server._probe_gateway_ready(7777) == 0

    def test_probe_reports_the_http_status_of_a_not_ready_gateway(self):
        from kiro_crew import cli_server

        err = urllib.error.HTTPError("u", 503, "not ready", {}, None)
        with patch("kiro_crew.cli_server.loopback_urlopen", side_effect=err):
            assert cli_server._probe_gateway_ready(7777) == 503
