"""A cron run's failure paths, the secret a script run is handed, and each run's sandbox tier.

This file pins, for ``kiro_crew.cron_script``, the redaction of a failed script's output
and stderr before either is cut to size, the timeout handler that reaps the child, the
guard that keeps a kill from becoming ``kill(-1)`` and the pipe drain after a kill, plus
the internal secret a script child is handed and the sandbox tier both cron spawn paths
request. The other files of this split are described in ``test_cron_script.py``.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from cron_script_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _cron_caller_is_named,
    _crons_dir_tracks_patched_home,
)

from kiro_crew import platform_compat as pc
from kiro_crew.cron_script import (
    _MAX_BAD_OUTPUT_HEAD,
    _MAX_SCRIPT_STDERR_TAIL,
    _REDACT_STRADDLE_MARGIN,
    _resolve_internal_secret,
    run_command_sandboxed,
    run_script_sandboxed,
)


class TestRunScriptSandboxedErrorPaths:
    """Tests for run_script_sandboxed subprocess error paths (lines 333, 337-338)."""

    def _write_script(self, tmp_path, code):
        crons_dir = tmp_path / ".kirocrew" / "crons"
        crons_dir.mkdir(parents=True)
        script = crons_dir / "err_test.py"
        script.write_text(code)
        return str(script)

    def test_nonzero_exit_no_stdout(self, tmp_path):
        """Line 333: subprocess fails with no stdout."""
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = ("", "segfault")
        with (
            patch("kiro_crew.cron_script.resolve_script_path", return_value=("/f.py", "run")),
            patch("kiro_crew.cron_script.wrap_argv", return_value=(["true"], None)),
            patch("subprocess.Popen", return_value=mock_proc),
        ):
            result = run_script_sandboxed("/f.py:run", "j1", "")
        assert result["status"] == "error"
        assert "segfault" in result.get("error", "")

    def test_bad_json_output(self, tmp_path):
        """Lines 337-338: stdout is not valid JSON."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = ("not json at all\n", "")
        with (
            patch("kiro_crew.cron_script.resolve_script_path", return_value=("/f.py", "run")),
            patch("kiro_crew.cron_script.wrap_argv", return_value=(["true"], None)),
            patch("subprocess.Popen", return_value=mock_proc),
        ):
            result = run_script_sandboxed("/f.py:run", "j1", "")
        assert result["status"] == "error"
        assert "Bad output" in result.get("error", "")

    def test_bad_json_output_redacts_before_truncating(self, tmp_path):
        """A credential straddling the 200-char boundary must not leak its head.

        Redaction must run on the WHOLE stdout before the head slice: slicing
        first can cut a secret mid-pattern, so redaction does not match and
        the raw head reaches the diagnostic.
        """
        # Built at runtime so no credential-shaped literal lands in the repo.
        fake_key = "AKIA" + "B" * 16  # matches the AWS access-key-id pattern
        # Sized so the 200-char head window ends 10 chars into the credential:
        # a slice-then-redact order keeps "AKIA" + 6 B's — too short for the
        # pattern to match, so the raw head leaks into the diagnostic.
        padding = "p" * 190
        stdout_text = padding + fake_key + "e" * 50
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (stdout_text, "")
        with (
            patch("kiro_crew.cron_script.resolve_script_path", return_value=("/f.py", "run")),
            patch("kiro_crew.cron_script.wrap_argv", return_value=(["true"], None)),
            patch("subprocess.Popen", return_value=mock_proc),
        ):
            result = run_script_sandboxed("/f.py:run", "j1", "")
        assert result["status"] == "error"
        assert "AKIA" not in result["error"]
        assert fake_key not in result["error"]
        # Positive proof the payload flowed through redaction (not a vacuous
        # pass on an empty diagnostic): the 200-char head window ends 10 chars
        # into the replacement marker, so its head survives the slice.
        assert "[REDACTED:" in result["error"]
        assert padding in result["error"]

    def test_stderr_redaction_input_is_bounded_to_a_window(self, tmp_path, monkeypatch):
        """The stderr-tail redaction must not copy an unbounded capture.

        ``proc.communicate`` caps neither stream and ``redact_credentials``
        materialises its base64 runs into a list beside the input, so redacting
        the WHOLE stderr costs a multiple of an unbounded string: a script
        streaming gigabytes and then failing would OOM the gateway in redaction
        that survived capture. Redaction reads a TAIL window of the kept 500
        chars plus ``_REDACT_STRADDLE_MARGIN``, which keeps the footprint fixed.

        Asserted on redact's INPUT rather than on the output, because the output
        cannot tell the two apart -- a credential far before the window is
        truncated away whether or not it was ever scanned. A credential
        straddling the -500 boundary proves the window overshoot still lets the
        pattern see it whole.
        """
        from kiro_crew import cron_script

        seen: list[int] = []
        real_redact = cron_script.redact

        def _spy(text: str) -> str:
            seen.append(len(text))
            return real_redact(text)

        monkeypatch.setattr(cron_script, "redact", _spy)
        fake_key = "AKIA" + "B" * 16  # matches the AWS access-key-id pattern
        # The -500 cut lands ONE character into the key: slice-then-redact
        # would leak the 19-char fragment, and a window that failed to reach
        # back past the cut would leak it the same way.
        # Newline-separated filler: the window then starts inside an ordinary
        # short line, so only that line's severed remainder is masked and the
        # straddling credential is still exercised against the pattern pass.
        stderr_text = ("n" * 100 + "\n") * 200 + "W" * 300 + fake_key + "X" * 481
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = ("", stderr_text)
        with (
            patch("kiro_crew.cron_script.resolve_script_path", return_value=("/f.py", "run")),
            patch("kiro_crew.cron_script.wrap_argv", return_value=(["true"], None)),
            patch("subprocess.Popen", return_value=mock_proc),
        ):
            result = run_script_sandboxed("/f.py:run", "j1", "")
        assert result["status"] == "error"
        assert fake_key not in result["error"]
        # The discriminator for the straddle: the fragment slice-then-redact
        # (or an undershooting window) leaves behind.
        assert fake_key[1:] not in result["error"]
        assert "credential]" in result["error"]
        assert seen, "redact was never called on the captured stderr"
        assert max(seen) <= _MAX_SCRIPT_STDERR_TAIL + _REDACT_STRADDLE_MARGIN

    def test_bad_output_redaction_input_is_bounded_to_a_window(self, tmp_path, monkeypatch):
        """The bad-stdout redaction must not copy an unbounded capture either.

        Same denial-of-service shape as the stderr tail, on the ``Bad output``
        diagnostic path: this cut keeps the HEAD, so its window reaches
        ``_REDACT_STRADDLE_MARGIN`` PAST the first 200 chars instead of back
        before them. A credential straddling the 200-char boundary proves the
        overshoot still lets the pattern see it whole.
        """
        from kiro_crew import cron_script

        seen: list[int] = []
        real_redact = cron_script.redact

        def _spy(text: str) -> str:
            seen.append(len(text))
            return real_redact(text)

        monkeypatch.setattr(cron_script, "redact", _spy)
        fake_key = "AKIA" + "B" * 16  # matches the AWS access-key-id pattern
        # The 200-char head cut lands 10 chars into the key; the trailing run
        # pushes the capture far past the window so the bound is discriminating.
        stdout_text = "p" * 190 + fake_key + "e" * 30000
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (stdout_text, "")
        with (
            patch("kiro_crew.cron_script.resolve_script_path", return_value=("/f.py", "run")),
            patch("kiro_crew.cron_script.wrap_argv", return_value=(["true"], None)),
            patch("subprocess.Popen", return_value=mock_proc),
        ):
            result = run_script_sandboxed("/f.py:run", "j1", "")
        assert result["status"] == "error"
        assert fake_key not in result["error"]
        # The head of the replacement marker survives the slice, proving the
        # straddling key was whole inside the window when redaction ran.
        assert "[REDACTED:" in result["error"]
        assert seen, "redact was never called on the captured stdout"
        assert max(seen) <= _MAX_BAD_OUTPUT_HEAD + _REDACT_STRADDLE_MARGIN

    def test_grant_value_longer_than_the_margin_is_still_scrubbed(self, tmp_path):
        """The grant scrub must read the WHOLE capture, not the redact window.

        A vault value has no shape, so ``_scrub_grant_values`` matches it only
        whole (``value in text``): if the scrub ran on the bounded window, a
        granted value LONGER than ``_REDACT_STRADDLE_MARGIN`` straddling the
        window's far edge would lose its head, stop matching, and its tail --
        which no pattern recognises either -- would reach the persisted
        ``last_error`` verbatim. The scrub is exact-substring replacement, so
        whole-capture input carries none of the memory amplification the
        redact window exists to bound.
        """
        # Longer than the margin, and shaped like nothing redact recognises,
        # so only the scrub stands between it and the diagnostic.
        secret_value = "grantval-" * 700  # 6300 chars > _REDACT_STRADDLE_MARGIN
        assert len(secret_value) > _REDACT_STRADDLE_MARGIN
        # Ends 100 chars before the capture's end: it STARTS well before the
        # stderr window's far edge (window = last 4596 chars), so a scrub that
        # only saw the window would see a headless fragment and skip it, and
        # the fragment's tail would sit inside the kept 500 chars.
        stderr_text = "n" * 2000 + secret_value + "X" * 100
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = ("", stderr_text)
        with (
            patch(
                "kiro_crew.cron_script._read_script_body",
                return_value=b"def run(ctx):\n    return None\n",
            ),
            patch(
                "kiro_crew.cron_script._secret_env_precheck",
                return_value=({"TOKEN": secret_value}, None),
            ),
            patch("kiro_crew.cron_script.resolve_script_path", return_value=("/f.py", "run")),
            patch("kiro_crew.cron_script.wrap_argv", return_value=(["true"], None)),
            patch("subprocess.Popen", return_value=mock_proc),
        ):
            result = run_script_sandboxed(
                "/f.py:run",
                "j1",
                "",
                secret_env={"TOKEN": "vault:t"},
                secret_env_pin="pin",
            )
        assert result["status"] == "error"
        # No recognisable run of the value survives -- neither the whole value
        # nor the fragment a window-scoped scrub would have left in the tail.
        assert "grantval-" * 20 not in result["error"]
        assert "[redacted-grant-value]" in result["error"]

    def test_pem_opened_before_the_tail_window_is_still_masked(self, tmp_path):
        """A PEM block spanning the whole tail window must not leak body lines.

        A PEM block has no length ceiling, so the straddle margin cannot cover
        it: a block whose ``-----BEGIN `` line falls before the window start
        loses the anchor the pattern needs, and its body lines inside the
        window -- shaped like nothing else redact recognises -- would reach
        the persisted ``last_error`` verbatim. ``_pem_safe_tail_window``
        tracks open-block state across the discarded prefix and masks the
        retained bytes through the END line.
        """
        label = "OPENSSH PRIVATE" + " KEY"
        # Body lines deliberately lowercase-only: they trip neither the
        # base64-run pass nor any anchored pattern, so ONLY the open-block
        # tracking stands between them and the diagnostic.
        body = ("keybodyline" * 5 + "\n") * 120  # ~6.7KB > 500 + margin
        pem = f"-----BEGIN {label}-----\n{body}-----END {label}-----\n"
        stderr_text = pem + "trailing diagnostic context"
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = ("", stderr_text)
        with (
            patch("kiro_crew.cron_script.resolve_script_path", return_value=("/f.py", "run")),
            patch("kiro_crew.cron_script.wrap_argv", return_value=(["true"], None)),
            patch("subprocess.Popen", return_value=mock_proc),
        ):
            result = run_script_sandboxed("/f.py:run", "j1", "")
        assert result["status"] == "error"
        assert "keybodyline" not in result["error"]
        assert "credential]" in result["error"]
        # Post-block content survives: the mask reaches the END line, not the
        # whole report.
        assert "trailing diagnostic context" in result["error"]

    def test_pem_that_never_closes_masks_the_whole_tail(self, tmp_path):
        """An unterminated over-window PEM yields the tag, not body lines."""
        label = "RSA PRIVATE" + " KEY"
        body = ("keybodyline" * 5 + "\n") * 120
        stderr_text = f"-----BEGIN {label}-----\n{body}"  # no END marker
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = ("", stderr_text)
        with (
            patch("kiro_crew.cron_script.resolve_script_path", return_value=("/f.py", "run")),
            patch("kiro_crew.cron_script.wrap_argv", return_value=(["true"], None)),
            patch("subprocess.Popen", return_value=mock_proc),
        ):
            result = run_script_sandboxed("/f.py:run", "j1", "")
        assert result["status"] == "error"
        assert "keybodyline" not in result["error"]
        assert "credential]" in result["error"]

    def test_closed_pem_before_the_window_does_not_mask_the_tail(self, tmp_path):
        """A block that CLOSED in the discarded prefix must not cost the tail.

        The open-block test is last-BEGIN vs last-END order; a closed block
        followed by ordinary diagnostics is the common case and must report
        those diagnostics whole.
        """
        label = "EC PRIVATE" + " KEY"
        pem = f"-----BEGIN {label}-----\nshortbody\n-----END {label}-----\n"
        stderr_text = pem + ("n" * 80 + "\n") * 75 + "REAL_TERMINAL_DIAGNOSIS"
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = ("", stderr_text)
        with (
            patch("kiro_crew.cron_script.resolve_script_path", return_value=("/f.py", "run")),
            patch("kiro_crew.cron_script.wrap_argv", return_value=(["true"], None)),
            patch("subprocess.Popen", return_value=mock_proc),
        ):
            result = run_script_sandboxed("/f.py:run", "j1", "")
        assert result["status"] == "error"
        assert "REAL_TERMINAL_DIAGNOSIS" in result["error"]

    def test_foreign_end_marker_does_not_close_an_open_key_block(self, tmp_path):
        """A certificate footer inside an open key block must not close it.

        END markers pair by LABEL: ``-----END CERTIFICATE-----`` interleaved
        inside an open ``RSA PRIVATE KEY`` block is body text. A tracker that
        paired any BEGIN with any END would treat the key as closed and hand
        its remaining body lines to the pattern pass anchor-less.
        """
        label = "RSA PRIVATE" + " KEY"
        stderr_text = (
            f"-----BEGIN {label}-----\n"
            + ("keybodyline\n" * 30)
            + "-----END CERTIFICATE-----\n"
            + ("keybodyline" * 5 + "\n") * 120  # pushes the window inside the block
            + f"-----END {label}-----\n"
            + "trailing diagnostic context"
        )
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = ("", stderr_text)
        with (
            patch("kiro_crew.cron_script.resolve_script_path", return_value=("/f.py", "run")),
            patch("kiro_crew.cron_script.wrap_argv", return_value=(["true"], None)),
            patch("subprocess.Popen", return_value=mock_proc),
        ):
            result = run_script_sandboxed("/f.py:run", "j1", "")
        assert result["status"] == "error"
        assert "keybodyline" not in result["error"]
        assert "trailing diagnostic context" in result["error"]

    def test_severed_single_line_run_is_masked_not_reported(self, tmp_path):
        """A single-line token severed by the window start leaks no suffix.

        Every single-line credential shape is whitespace-free, so when the
        window boundary lands mid-line the possible credential tail is the
        window's leading non-whitespace run. All-same-letter content is the
        discriminator: it trips no pattern (no mixed case, no digits, no
        anchor), so only the severed-run rule stands between it and the
        persisted ``last_error``.
        """
        stderr_text = "warmup line\n" + "Z" * 6000 + "\n" + ("post-token diagnostic\n" * 10)
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = ("", stderr_text)
        with (
            patch("kiro_crew.cron_script.resolve_script_path", return_value=("/f.py", "run")),
            patch("kiro_crew.cron_script.wrap_argv", return_value=(["true"], None)),
            patch("subprocess.Popen", return_value=mock_proc),
        ):
            result = run_script_sandboxed("/f.py:run", "j1", "")
        assert result["status"] == "error"
        assert "Z" * 40 not in result["error"]
        assert "credential]" in result["error"]
        assert "post-token diagnostic" in result["error"]

    def test_whitespace_after_the_cut_does_not_unmask_the_severed_line(self, tmp_path):
        """A severed line is masked whole, wherever the credential sits on it.

        The cut can land inside whitespace that PRECEDES the credential (an
        indented value, a wrapped ``Authorization:`` header), so a rule that
        masked only a leading non-whitespace run would see whitespace at the
        window start, match nothing, and hand the raw token through. The
        whole in-window remainder of the severed line is masked instead.
        """
        stderr_text = (
            ("n" * 100 + "\n") * 2
            + "authheader:"
            + " " * 60
            + "Z" * 4200
            + "\n"
            + ("post diagnostic\n" * 22)
        )
        # Geometry check: the cut lands INSIDE the whitespace run that
        # precedes the token, the exact shape a leading-run rule misses.
        window_span = 500 + _REDACT_STRADDLE_MARGIN
        cut = len(stderr_text.rstrip()) - window_span
        ws_start = stderr_text.index("authheader:") + len("authheader:")
        assert ws_start <= cut < ws_start + 60
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = ("", stderr_text)
        with (
            patch("kiro_crew.cron_script.resolve_script_path", return_value=("/f.py", "run")),
            patch("kiro_crew.cron_script.wrap_argv", return_value=(["true"], None)),
            patch("subprocess.Popen", return_value=mock_proc),
        ):
            result = run_script_sandboxed("/f.py:run", "j1", "")
        assert result["status"] == "error"
        assert "Z" * 40 not in result["error"]
        assert "post diagnostic" in result["error"]

    def test_begin_marker_straddling_the_cut_fails_closed(self, tmp_path):
        """A BEGIN marker split by the cut must not leak the block body.

        The prefix scan cannot see the marker (incomplete before the cut) and
        the window's pattern pass cannot either (its anchor is destroyed), so
        the label is unknowable from either side; the window fails closed to
        the tag alone.
        """
        label = "RSA PRIVATE" + " KEY"
        key_open = f"-----BEGIN {label}-----"
        body = ("b" * 64 + "\n") * 68  # ~4.4KB: END stays inside the window
        stderr_text = (
            ("n" * 100 + "\n") * 50
            + key_open
            + "\n"
            + body
            + f"-----END {label}-----\n"
            + ("c" * 11 + "\n") * 10
        )
        # Geometry check: the cut lands INSIDE the BEGIN marker line.
        window_span = 500 + _REDACT_STRADDLE_MARGIN
        cut = len(stderr_text.rstrip()) - window_span
        marker_at = stderr_text.index(key_open)
        assert marker_at <= cut < marker_at + len(key_open)
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = ("", stderr_text)
        with (
            patch("kiro_crew.cron_script.resolve_script_path", return_value=("/f.py", "run")),
            patch("kiro_crew.cron_script.wrap_argv", return_value=(["true"], None)),
            patch("subprocess.Popen", return_value=mock_proc),
        ):
            result = run_script_sandboxed("/f.py:run", "j1", "")
        assert result["status"] == "error"
        assert "b" * 40 not in result["error"]
        assert "credential]" in result["error"]

    def test_begin_marker_after_the_cut_on_the_severed_line_keeps_its_block_masked(self, tmp_path):
        """Masking the severed line must not delete a BEGIN anchor it carries.

        A key printed inline in an error string (``Error: ... -----BEGIN
        ...``) puts the BEGIN marker AFTER the cut on the severed line. A rule
        that masked the severed line as an opaque unit would delete the anchor
        with the line while the key body survives in the remainder, unmatched
        by the pattern pass. The marker walk continues through the severed
        segment, so the block is masked through its labelled END like any
        other open block.
        """
        label = "RSA PRIVATE" + " KEY"
        key_open = f"-----BEGIN {label}-----"
        body = ("b" * 64 + "\n") * 68
        stderr_text = (
            ("n" * 100 + "\n") * 44
            + "Error: dumping key "
            + key_open
            + "\n"
            + body
            + f"-----END {label}-----\n"
            + ("c" * 11 + "\n") * 9
        )
        # Geometry check: the cut lands inside the prose BEFORE the marker.
        window_span = 500 + _REDACT_STRADDLE_MARGIN
        cut = len(stderr_text.rstrip()) - window_span
        assert stderr_text.index("Error:") <= cut < stderr_text.index(key_open)
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = ("", stderr_text)
        with (
            patch("kiro_crew.cron_script.resolve_script_path", return_value=("/f.py", "run")),
            patch("kiro_crew.cron_script.wrap_argv", return_value=(["true"], None)),
            patch("subprocess.Popen", return_value=mock_proc),
        ):
            result = run_script_sandboxed("/f.py:run", "j1", "")
        assert result["status"] == "error"
        assert "b" * 40 not in result["error"]
        assert "c" * 11 in result["error"]

    def test_inline_prefixed_begin_marker_split_by_the_cut_fails_closed(self, tmp_path):
        """Inline prose before a marker must not defeat the severed-BEGIN rule.

        A key printed mid-sentence (``Error: dumping key -----BEGIN ...``)
        with the cut landing INSIDE the marker leaves an inline prefix before
        the partial marker on the pre-cut line, so a check that requires the
        LINE to start with the marker never fires while the marker walk sees
        no complete marker on either side. The rule inspects the pre-cut
        line's SUFFIX instead, so this shape fails closed to the tag.
        """
        label = "RSA PRIVATE" + " KEY"
        key_open = f"-----BEGIN {label}-----"
        body = ("b" * 64 + "\n") * 68
        stderr_text = (
            ("n" * 100 + "\n") * 44
            + "Error: dumping key "
            + key_open
            + "\n"
            + body
            + f"-----END {label}-----\n"
            + ("c" * 11 + "\n") * 10
        )
        # Geometry check: the cut lands INSIDE the marker, after inline prose.
        window_span = 500 + _REDACT_STRADDLE_MARGIN
        cut = len(stderr_text.rstrip()) - window_span
        marker_at = stderr_text.index(key_open)
        assert marker_at < cut < marker_at + len(key_open)
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = ("", stderr_text)
        with (
            patch("kiro_crew.cron_script.resolve_script_path", return_value=("/f.py", "run")),
            patch("kiro_crew.cron_script.wrap_argv", return_value=(["true"], None)),
            patch("subprocess.Popen", return_value=mock_proc),
        ):
            result = run_script_sandboxed("/f.py:run", "j1", "")
        assert result["status"] == "error"
        assert "b" * 40 not in result["error"]
        assert "credential]" in result["error"]


class TestRunScriptSandboxedTimeout:
    """Test for run_script_sandboxed TimeoutExpired handler."""

    def test_script_timeout_returns_error(self):
        import subprocess as sp

        mock_proc = MagicMock()
        # CRITICAL: a bare MagicMock pid coerces to 1 via __index__, and the
        # timeout cleanup path would then run os.killpg(1, SIGKILL) ==
        # kill(-1, SIGKILL) — SIGKILLing every process this uid owns.
        # Always give mocked Popen objects a real, nonexistent int pid.
        # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process moved verbatim; tracked for a de-flake pass
        mock_proc.pid = 2**22 + 12345  # > PID_MAX default, never a real pid
        mock_proc.communicate.side_effect = [sp.TimeoutExpired("cmd", 30), ("", "")]
        with (
            patch("kiro_crew.cron_script.resolve_script_path", return_value=("/f.py", "run")),
            patch("kiro_crew.cron_script.wrap_argv", return_value=(["true"], None)),
            patch("subprocess.Popen", return_value=mock_proc),
            patch(
                # Stub the reap at the shim, NOT via the global subprocess.Popen
                # patch above. On Windows _kill_proc_group reaps through
                # platform_compat.kill_process_tree, which shells out to `taskkill
                # /T /F` with subprocess.run — and subprocess.run builds its child
                # from subprocess.Popen, so the patch aimed at the launcher spawn
                # also hijacks taskkill's, handing subprocess.run a MagicMock whose
                # communicate() returns a mock instead of a 2-tuple (ValueError:
                # not enough values to unpack). The timeout HANDLER is what is under
                # test; the kill mechanism has its own coverage in
                # TestKillBroadcastGuard.
                "kiro_crew.platform_compat.kill_process_tree",
                return_value=True,
            ) as mock_tree,
        ):
            result = run_script_sandboxed("/f.py:run", "j1", "", timeout=30)
        assert result["status"] == "error"
        assert "timed out" in result["error"]
        assert "30s" in result["error"]
        # communicate() does not kill the child on timeout, so the handler MUST
        # reap it — otherwise a timed-out cron leaks a live subprocess tree.
        if pc.IS_POSIX:
            # POSIX takes the os.killpg branch when a pgid resolves; the mocked
            # pid does not exist, so _resolve_safe_pgid returns None and the
            # shim is the fallback. Either way the process must be signalled.
            assert mock_tree.called or mock_proc.kill.called
        else:
            mock_tree.assert_called_once_with(mock_proc.pid, pc.SIGKILL)


class TestKillBroadcastGuard:
    """_resolve_safe_pgid must never let a kill path degenerate into kill(-1)."""

    def test_mock_pid_refused(self):
        from kiro_crew.cron_script import _resolve_safe_pgid

        assert _resolve_safe_pgid(MagicMock()) is None  # MagicMock pid -> not int

    def test_pid_one_refused(self):
        from kiro_crew.cron_script import _resolve_safe_pgid

        proc = MagicMock()
        # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing pid the broadcast guard must refuse moved verbatim; only the pgid resolver reads it
        proc.pid = 1
        assert _resolve_safe_pgid(proc) is None

    def test_negative_pid_refused(self):
        from kiro_crew.cron_script import _resolve_safe_pgid

        proc = MagicMock()
        # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing pid the broadcast guard must refuse moved verbatim; only the pgid resolver reads it
        proc.pid = -1
        assert _resolve_safe_pgid(proc) is None

    def test_bool_pid_refused(self):
        from kiro_crew.cron_script import _resolve_safe_pgid

        proc = MagicMock()
        proc.pid = True  # bool is an int subclass; type() check must reject it
        assert _resolve_safe_pgid(proc) is None

    def test_own_pgid_refused(self):
        import os

        from kiro_crew.cron_script import _resolve_safe_pgid

        proc = MagicMock()
        proc.pid = os.getpid()  # our own group -> must refuse (self-kill)
        assert _resolve_safe_pgid(proc) is None

    def test_kill_proc_group_never_calls_killpg_for_mock(self):
        """A bare-mock pid must never reach a group kill — the original footgun.

        ``os.killpg`` does not exist on Windows, so patching it by name raises
        AttributeError there. Use ``create=True``: the assertion we care about
        is that the attribute is never *called*, which holds on both platforms
        (on Windows ``_resolve_safe_pgid`` returns None so the killpg branch is
        unreachable, and ``kill_process_tree`` takes the taskkill path).
        """
        from kiro_crew.cron_script import _kill_proc_group

        proc = MagicMock()  # bare mock pid — the original footgun
        with (
            patch("os.killpg", create=True) as mock_killpg,
            patch(
                # Windows: _kill_proc_group reaps via taskkill before the
                # single-process fallback. Stub it so the test never shells out.
                "kiro_crew.platform_compat.kill_process_tree",
                side_effect=OSError("stubbed"),
            ),
        ):
            _kill_proc_group(proc)
        mock_killpg.assert_not_called()
        proc.kill.assert_called_once()

    @pytest.mark.skipif(
        not pc.IS_POSIX,
        reason="POSIX broadcast guard (killpg/getpgid); Windows takes the taskkill path",
    )
    def test_shim_kill_process_tree_refuses_mock_pid(self):
        """platform_compat.kill_process_tree: non-int pid must raise, never killpg."""
        import pytest as _pytest

        from kiro_crew import platform_compat

        with patch("os.killpg") as mock_killpg, patch("os.kill") as mock_kill:
            with _pytest.raises(ValueError):
                platform_compat.kill_process_tree(MagicMock(), platform_compat.SIGKILL)
        mock_killpg.assert_not_called()
        mock_kill.assert_not_called()

    @pytest.mark.skipif(
        not pc.IS_POSIX,
        reason="POSIX broadcast guard (killpg/getpgid); Windows takes the taskkill path",
    )
    def test_shim_kill_process_tree_broadcast_pgid_degrades_to_pid_kill(self):
        """platform_compat.kill_process_tree: pgid<=1 degrades to scoped os.kill."""
        from kiro_crew import platform_compat

        target = 2**22 + 31337
        with (
            patch("os.getpgid", return_value=1),
            patch("os.killpg") as mock_killpg,
            patch("os.kill") as mock_kill,
        ):
            assert platform_compat.kill_process_tree(target, platform_compat.SIGKILL) is True
        mock_killpg.assert_not_called()
        mock_kill.assert_called_once_with(target, platform_compat.SIGKILL)

    @pytest.mark.skipif(pc.IS_POSIX, reason="Windows taskkill /T branch of kill_process_tree")
    def test_shim_kill_process_tree_uses_taskkill_on_windows(self):
        """Windows has no process groups: the shim must reap via ``taskkill /T``.

        This is the Windows counterpart to the two POSIX broadcast-guard tests
        above — same invariant (kill the whole tree, never broadcast), different
        mechanism, so it needs its own coverage rather than a bare skip.
        """
        import ntpath

        from kiro_crew import platform_compat

        target = 2**22 + 31337
        completed = MagicMock(returncode=0, stdout=b"", stderr=b"")
        with patch("subprocess.run", return_value=completed) as mock_run:
            assert platform_compat.kill_process_tree(target, platform_compat.SIGKILL) is True
        argv = mock_run.call_args.args[0]
        # argv[0] may be a bare name or an absolute trusted-system path
        # (kill_process_tree resolves taskkill from GetSystemDirectoryW rather
        # than trusting %PATH%), so match on the basename and its .exe suffix.
        assert ntpath.basename(argv[0]).lower() in ("taskkill", "taskkill.exe")
        assert "/T" in argv and "/F" in argv  # whole tree, forced
        assert str(target) in argv

    def test_cancel_flag_cleared_when_terminate_fails(self):
        """kill_running_process: signal never delivered -> flag must not leak.

        Otherwise a later natural completion would be misreported as cancelled
        by _unregister_proc.
        """
        from kiro_crew.cron_script import (
            _CANCELLED_PROC_JOBS,
            _RUNNING_PROCS,
            kill_running_process,
        )

        proc = MagicMock()
        # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process moved verbatim; tracked for a de-flake pass
        proc.pid = 2**22 + 54321  # nonexistent real int pid
        proc.poll.return_value = None  # "still running"
        proc.terminate.side_effect = OSError("terminate failed")
        _RUNNING_PROCS["flagleak"] = proc
        try:
            assert kill_running_process("flagleak") is False
            assert "flagleak" not in _CANCELLED_PROC_JOBS
        finally:
            _RUNNING_PROCS.pop("flagleak", None)
            _CANCELLED_PROC_JOBS.discard("flagleak")


class TestResolveInternalSecret:
    """Secret resolution falls back to .local_secret when env is unset."""

    def test_uses_env_when_set(self, tmp_path):
        with (
            patch.dict(os.environ, {"KIROCREW_INTERNAL_SECRET": "fromenv"}),
            patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
        ):
            (tmp_path / ".local_secret").write_text("fromfile")
            assert _resolve_internal_secret(5476) == "fromenv"

    def test_falls_back_to_local_secret_file(self, tmp_path):
        (tmp_path / ".local_secret").write_text("filesecret\n")
        env = {k: v for k, v in os.environ.items() if k != "KIROCREW_INTERNAL_SECRET"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
        ):
            assert _resolve_internal_secret(5476) == "filesecret"

    def test_empty_when_neither_present(self, tmp_path):
        env = {k: v for k, v in os.environ.items() if k != "KIROCREW_INTERNAL_SECRET"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
        ):
            assert _resolve_internal_secret(5476) == ""

    def test_env_empty_string_falls_back_to_file(self, tmp_path):
        (tmp_path / ".local_secret").write_text("filesecret")
        with (
            patch.dict(os.environ, {"KIROCREW_INTERNAL_SECRET": ""}),
            patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
        ):
            assert _resolve_internal_secret(5476) == "filesecret"

    def test_run_script_writes_local_secret_to_sandbox_when_env_unset(self, tmp_path):
        """End-to-end: run_script_sandboxed hands the sandbox the .local_secret value."""
        (tmp_path / ".local_secret").write_text("realsecret\n")
        captured = {}

        def fake_popen(argv, **kwargs):
            sf = kwargs.get("env", {}).get("_KIROCREW_SECRET_FILE")
            captured["secret"] = open(sf).read() if sf else None
            proc = MagicMock()
            proc.returncode = 0
            proc.communicate.return_value = ('{"status": "ok"}', "")
            return proc

        env = {k: v for k, v in os.environ.items() if k != "KIROCREW_INTERNAL_SECRET"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
            patch("kiro_crew.cron_script.resolve_script_path", return_value=("/f.py", "run")),
            patch("kiro_crew.cron_script.wrap_argv", return_value=(["true"], None)),
            patch("subprocess.Popen", side_effect=fake_popen),
        ):
            run_script_sandboxed("/f.py:run", "j1", "", timeout=30)
        assert captured["secret"] == "realsecret"


class TestPostKillDrainTimeoutHardening:
    """The post-kill drain must not leak pipes or mislabel the kill-path result.

    ``Popen.communicate`` does not kill the child on timeout, so both spawners
    SIGKILL the process group and then reap it with a bounded
    ``communicate(timeout=5)``. That second drain can ITSELF raise
    ``TimeoutExpired`` when the pipes never reach EOF — the child outlived the
    group kill, or another process still holds the write end open. Two
    invariants have to survive that:

    - the ``PIPE`` fds get closed rather than left open for the life of the
      gateway. ``Popen._communicate`` closes them as a side effect of reaching
      EOF, which is exactly the path it does not reach here.
    - the kill path still reports a *timeout*. In ``run_command_sandboxed`` a
      ``TimeoutExpired`` raised inside the ``except`` block bypasses that
      block's own handler list, so the timed-out ``return`` is skipped and the
      broad ``except Exception`` reports "Command failed" instead.
    """

    def test_script_drain_timeout_closes_pipes_and_keeps_contract(self):
        import subprocess as sp

        mock_proc = MagicMock()
        # A bare MagicMock pid coerces to 1 via __index__, and os.killpg(1, sig)
        # is kill(-1, sig) — see TestKillBroadcastGuard.
        # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process moved verbatim; tracked for a de-flake pass
        mock_proc.pid = 2**22 + 12345  # > PID_MAX default, never a real pid
        # BOTH the initial read and the post-kill drain time out.
        mock_proc.communicate.side_effect = sp.TimeoutExpired("cmd", 30)
        with (
            patch("kiro_crew.cron_script.resolve_script_path", return_value=("/f.py", "run")),
            patch("kiro_crew.cron_script.wrap_argv", return_value=(["true"], None)),
            patch("subprocess.Popen", return_value=mock_proc),
            patch(
                # Stub the reap at the shim, NOT via the subprocess.Popen patch above:
                # same reason as TestRunScriptSandboxedTimeout.
                "kiro_crew.platform_compat.kill_process_tree",
                return_value=True,
            ),
        ):
            result = run_script_sandboxed("/f.py:run", "j1", "", timeout=30)
        # The drain's own timeout must neither escape nor alter the contract.
        assert result == {"status": "error", "error": "Script timed out after 30s"}
        # The fd-leak assertion: both pipes closed even though EOF never came.
        mock_proc.stdout.close.assert_called_once()
        mock_proc.stderr.close.assert_called_once()

    def test_command_drain_timeout_closes_pipes_and_keeps_contract(self, monkeypatch):
        import subprocess as sp

        # See TestRunCommandSandboxed._passthrough_sandbox: bypass the OS-sandbox
        # wrap and the runtime shell probe so this exercises only the kill path.
        monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", lambda argv, **k: (list(argv), None))
        monkeypatch.setattr("kiro_crew.cron_script._resolve_command_shell", lambda: "sh")
        mock_proc = MagicMock()
        # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process moved verbatim; tracked for a de-flake pass
        mock_proc.pid = 2**22 + 12345  # > PID_MAX default, never a real pid
        mock_proc.communicate.side_effect = sp.TimeoutExpired("cmd", 30)
        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch("kiro_crew.platform_compat.kill_process_tree", return_value=True),
        ):
            result = run_command_sandboxed("sleep 30", timeout=30)
        # A timed-out command must report the timeout, not "Command failed".
        assert result == {
            "status": "error",
            "output": "❌ Command timed out after 30s",
            "exit_code": -1,
        }
        mock_proc.stdout.close.assert_called_once()
        mock_proc.stderr.close.assert_called_once()


class TestCronSpawnTiersAreAligned:
    """The two cron spawn paths must ask for the SAME sandbox tier.

    A script body is agent-written and a command body is a fixed string the vet
    already narrowed, so the script path is the HIGHER-capability surface of the
    two. It nonetheless ran ``standard`` while the command path ran ``cc``,
    leaving ``~/.aws/credentials``, the SSO cache, ``~/.kube``, ``~/.netrc``,
    ``~/.git-credentials``, ``~/.npmrc`` and ``~/.pypirc`` readable by the more
    capable child. These tests pin the alignment rather than the literal, so a
    later change that moves ONE path re-fails here instead of silently
    re-opening the gap.
    """

    def _capture_mode(self, monkeypatch):
        """Record the ``mode`` each wrap_argv call asks for."""
        seen: list[str] = []

        def _fake(argv, **kwargs):
            seen.append(kwargs.get("mode", "<default>"))
            return (list(argv), None)

        monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", _fake)
        return seen

    def test_an_ungranted_script_asks_for_the_same_tier_as_a_command(
        self, tmp_path, monkeypatch, posix_test_shell
    ):
        """The alignment itself: neither path may be wider than the other."""
        monkeypatch.setattr(
            "kiro_crew.cron_script._resolve_command_shell", lambda: posix_test_shell
        )
        monkeypatch.setattr("kiro_crew.cron_script.Path.home", lambda: tmp_path)
        crons_dir = tmp_path / ".kirocrew" / "crons"
        crons_dir.mkdir(parents=True)
        script = crons_dir / "aligned.py"
        script.write_text("def run(ctx):\n    return None\n")

        script_modes = self._capture_mode(monkeypatch)
        run_script_sandboxed(f"{script}:run", job_id="job-align", timeout=10)

        command_modes = self._capture_mode(monkeypatch)
        run_command_sandboxed("true", timeout=10)

        assert script_modes, "the script path never reached wrap_argv"
        assert command_modes, "the command path never reached wrap_argv"
        # The ungranted script spawn is the LAST wrap_argv call the script path
        # makes (a shell probe may precede it); same for the command path.
        assert script_modes[-1] == command_modes[-1]

    def test_an_ungranted_script_is_not_handed_the_wide_profile(self, tmp_path, monkeypatch):
        """``standard`` leaves every credential store open — never the default here."""
        monkeypatch.setattr("kiro_crew.cron_script.Path.home", lambda: tmp_path)
        crons_dir = tmp_path / ".kirocrew" / "crons"
        crons_dir.mkdir(parents=True)
        script = crons_dir / "narrow.py"
        script.write_text("def run(ctx):\n    return None\n")

        modes = self._capture_mode(monkeypatch)
        run_script_sandboxed(f"{script}:run", job_id="job-narrow", timeout=10)

        assert modes[-1] == "cc"
        assert "standard" not in modes

    def test_a_secret_granted_script_still_takes_the_narrowest_tier(self, tmp_path, monkeypatch):
        """A grant is the operator-approved route, and it tightens rather than widens.

        This is the escape hatch a script needing a host credential uses, so it
        must keep running ``strict`` — the alignment above must not have widened
        the granted branch to ``cc``.
        """
        monkeypatch.setattr("kiro_crew.cron_script.Path.home", lambda: tmp_path)
        crons_dir = tmp_path / ".kirocrew" / "crons"
        crons_dir.mkdir(parents=True)
        script = crons_dir / "granted.py"
        script.write_text("def run(ctx):\n    return None\n")

        modes = self._capture_mode(monkeypatch)
        monkeypatch.setattr(
            "kiro_crew.cron_script._secret_env_precheck",
            lambda *a, **k: ({"TOKEN": "v"}, ""),
        )
        run_script_sandboxed(
            f"{script}:run",
            job_id="job-granted",
            timeout=10,
            secret_env={"TOKEN": "vault:t"},
            secret_env_pin="pin",
        )

        assert modes[-1] == "strict"
