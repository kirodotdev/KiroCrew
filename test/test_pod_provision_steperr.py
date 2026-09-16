"""``kiro_crew.pod.provision`` names a failed step from its stderr, not its stdout tail.

The Dev Fleet gateway spawns ``pod provision`` with ``stderr=STDOUT``, so a step
writing straight to the inherited descriptors lands its stdout and stderr in ONE
pipe -- and a child block-buffers stdout to a pipe while writing stderr
unbuffered, so the stdout buffer flushes at exit, after the diagnostic. The last
line of that stream is a progress line. These tests pin the relay that fixes it:
each step's streams are piped and pumped through, the stderr tail is kept, and
the step whose failure ENDS provisioning is re-emitted as ``::steperr::``
markers -- the same protocol the sync runner speaks, so the dashboard names both
failures by one rule.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from kiro_crew.pod import provision as prov


def _script_step(script: str) -> list[str]:
    return [sys.executable, "-c", script]


@pytest.fixture(autouse=True)
def _fresh_failure_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prov, "_last_failed_step", None)


class TestRunRelaysBothStreams:
    def test_a_failed_step_reports_its_stderr_tail_not_its_last_stdout_line(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """The ordering the marker exists for, reproduced end to end.

        Diagnosis on stderr, a progress line on stdout written LAST so no
        buffering assumption is needed to make it the final line of the merged
        stream. The markers must carry the stderr lines, in order, and the
        stdout line must stay log-only.
        """
        script = (
            "import sys;"
            "print('ERROR: Could not install packages due to an OSError',"
            " file=sys.stderr);"
            "print('  [Errno 13] Permission denied: site-packages', file=sys.stderr);"
            "sys.stderr.flush();"
            "print('Collecting kiro-crew');"
            "sys.exit(1)"
        )

        rc = prov._run(_script_step(script), tmp_path)
        assert rc == 1
        assert prov._fail() is False

        # `capfd`: the relay writes to the real stderr descriptor, exactly as it
        # does into the gateway's merged pipe in production.
        lines = capfd.readouterr().err.splitlines()
        markers = [ln for ln in lines if ln.startswith("::steperr::")]
        assert len(markers) == 2
        assert markers[0].endswith("::ERROR: Could not install packages due to an OSError")
        assert markers[1].endswith("::  [Errno 13] Permission denied: site-packages")
        # One step, one index.
        assert len({m.split("::")[2] for m in markers}) == 1
        # The stderr lines still reach the log in their own right -- the markers
        # label the tail, they do not replace the transcript.
        assert "  [Errno 13] Permission denied: site-packages" in lines
        # The stdout progress line stays in the log, and is NOT in a marker.
        assert "Collecting kiro-crew" in lines
        assert not any("Collecting kiro-crew" in m for m in markers)

    def test_a_multibyte_blob_stays_under_the_gateway_byte_limit(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """The cap counts characters; the gateway's reader counts bytes.

        At 4 UTF-8 bytes per character a round character cap encodes to several
        times the gateway's ``StreamReader`` limit, where ``readline()`` raises
        and the handler reaps the whole tree. The bound asserted is the ENCODED
        length of every forwarded line, newline included.
        """
        blob_chars = 5 * prov._STEPERR_READ_CAP
        script = (
            "import sys;"
            f"sys.stderr.write('\\U0001F600' * {blob_chars});"
            "sys.stderr.write('\\nFATAL: build failed\\n');"
            "sys.stderr.flush();"
            "sys.exit(1)"
        )

        rc = prov._run(_script_step(script), tmp_path)
        assert rc == 1
        prov._fail()

        lines = capfd.readouterr().err.splitlines()
        forwarded = [ln for ln in lines if not ln.startswith("::")]
        worst = max(len(ln.encode("utf-8")) + 1 for ln in forwarded)
        assert worst <= prov._GATEWAY_LINE_BYTES, (
            f"a forwarded line encodes to {worst} bytes, past the "
            f"{prov._GATEWAY_LINE_BYTES}-byte gateway limit"
        )
        # Nothing is dropped: the blob is split, not truncated.
        assert sum(ln.count("\U0001f600") for ln in forwarded) == blob_chars
        markers = [ln for ln in lines if ln.startswith("::steperr::")]
        assert markers, "a failed step must still report a tail"
        # A remembered tail line is a banner line: cut, and MARKED as cut.
        for m in markers:
            assert len(m) <= len("::steperr::0::") + prov._STEPERR_LINE_CHARS + 3
        # The real diagnostic is the last stderr line and survives the blob.
        assert markers[-1].endswith("::FATAL: build failed")

    def test_the_tail_is_bounded_and_keeps_the_last_lines(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        script = (
            "import sys;"
            "[print('line %d' % i, file=sys.stderr) for i in range(10)];"
            "sys.exit(2)"
        )
        assert prov._run(_script_step(script), tmp_path) == 2
        prov._fail()
        markers = [ln for ln in capfd.readouterr().err.splitlines() if ln.startswith("::steperr::")]
        assert len(markers) == prov._STEPERR_TAIL
        assert [m.rsplit("::", 1)[1] for m in markers] == [
            "line %d" % i for i in range(10 - prov._STEPERR_TAIL, 10)
        ]

    def test_a_passing_step_leaves_nothing_to_name(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        script = "import sys; print('warning: harmless', file=sys.stderr); sys.exit(0)"
        assert prov._run(_script_step(script), tmp_path) == 0
        assert prov._last_failed_step is None
        prov._fail()
        assert "::steperr::" not in capfd.readouterr().err

    def test_the_child_is_told_to_write_utf8(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The relay decodes as UTF-8, so a Python step must encode as UTF-8 too --
        and the parent environment is still inherited when *env* is ``None``."""
        monkeypatch.setenv("PROVISION_PROBE_INHERITED", "yes")
        probe = tmp_path / "probe.txt"
        script = (
            "import os, pathlib, sys;"
            f"pathlib.Path({str(probe)!r}).write_text("
            "os.environ['PYTHONIOENCODING'] + '|' + os.environ['PROVISION_PROBE_INHERITED'])"
        )
        assert prov._run(_script_step(script), tmp_path) == 0
        assert probe.read_text() == "utf-8:replace|yes"


class TestAnInterruptedStepIsReaped:
    def test_an_interrupt_during_wait_kills_the_child_before_re_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ctrl-C during a long pip/npm step must not leave that step running.

        ``subprocess.run`` kills and reaps its child when the wait is
        interrupted; the relay spawns with ``Popen`` and has to keep that
        contract itself, or the step keeps mutating the worktree after
        provisioning appears to have stopped.
        """
        import subprocess

        spawned: list[subprocess.Popen] = []

        class _Interrupted(subprocess.Popen):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                spawned.append(self)
                self._interrupted_once = False

            def wait(self, timeout=None):
                if not self._interrupted_once:
                    self._interrupted_once = True
                    raise KeyboardInterrupt
                return super().wait(timeout)

        monkeypatch.setattr(prov.subprocess, "Popen", _Interrupted)
        script = "import time; time.sleep(60)"

        # The test owns the sleeper's cleanup: an assertion failing before the
        # reap under test would otherwise leave it running into later tests.
        try:
            with pytest.raises(KeyboardInterrupt):
                prov._run(_script_step(script), tmp_path)

            assert len(spawned) == 1
            # Reaped, not merely signalled: poll() reports a return code.
            assert spawned[0].poll() is not None
            # An interrupted step is not a failed step: nothing is left to name.
            assert prov._last_failed_step is None
        finally:
            for child in spawned:
                if child.poll() is None:
                    child.kill()
                    subprocess.Popen.wait(child, timeout=10)


class TestOnlyTheTerminalFailureIsNamed:
    """A recovered failure is log text; only the step that ENDS provisioning is named."""

    def _fake_run(
        self,
        results: dict[str, int],
        monkeypatch: pytest.MonkeyPatch,
        materialize: Path | None = None,
    ) -> list[list[str]]:
        """Stand in for the subprocess seam: exit code by keyword, and a stderr
        tail recorded for every failure the way the real ``_run`` does. When
        *materialize* is given, the venv step creates that entry point the way
        ``python -m venv`` would."""
        calls: list[list[str]] = []
        counter = iter(range(100))

        def fake_run(cmd: list[str], cwd: Path, env: dict | None = None) -> int:
            calls.append(cmd)
            idx = next(counter)
            if materialize is not None and cmd[1:3] == ["-m", "venv"]:
                materialize.parent.mkdir(parents=True, exist_ok=True)
                materialize.write_text("#!/bin/sh\n")
                materialize.chmod(0o755)
            rc = next((code for key, code in results.items() if key in " ".join(cmd)), 0)
            if rc != 0:
                prov._last_failed_step = (idx, ["%s failed loudly" % cmd[-1]])
            return rc

        monkeypatch.setattr(prov, "_run", fake_run)
        return calls

    def test_npm_ci_falling_back_to_npm_install_is_not_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        website = tmp_path / "website"
        website.mkdir()
        monkeypatch.setattr(prov, "_npm_bin", lambda: "/usr/bin/npm")
        self._fake_run({" ci": 1}, monkeypatch)

        assert prov.ensure_node_modules(website) is True
        assert "::steperr::" not in capsys.readouterr().err

    def test_npm_install_fallback_failing_is_named_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        website = tmp_path / "website"
        website.mkdir()
        monkeypatch.setattr(prov, "_npm_bin", lambda: "/usr/bin/npm")
        self._fake_run({" ci": 1, " install": 1}, monkeypatch)

        assert prov.ensure_node_modules(website) is False
        err = capsys.readouterr().err
        markers = [ln for ln in err.splitlines() if ln.startswith("::steperr::")]
        # Only the fallback -- the step that ended provisioning -- is named, never
        # the recovered `npm ci`.
        assert markers == ["::steperr::1::--no-package-lock failed loudly"]

    def test_npm_run_build_failing_is_named_after_the_fatal_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        co = tmp_path / "wt"
        (co / "website" / "node_modules" / ".bin").mkdir(parents=True)
        (co / "website" / "node_modules" / ".bin" / "tsc").write_text("")
        monkeypatch.setattr(prov, "_npm_bin", lambda: "/usr/bin/npm")
        self._fake_run({"build": 1}, monkeypatch)

        assert prov.build_dist(co) is False
        lines = capsys.readouterr().err.splitlines()
        assert "FATAL: npm run build failed" in lines
        assert lines[-1] == "::steperr::0::build failed loudly"

    def test_a_pip_group_fallback_that_recovers_is_not_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        co = tmp_path / "wt"
        co.mkdir()
        monkeypatch.setattr(prov, "_find_python", lambda version="3.12": sys.executable)
        calls = self._fake_run({"--group": 1}, monkeypatch, materialize=prov.venv_bin(co))

        assert prov.ensure_venv(co) is True
        assert any("--group" in " ".join(c) for c in calls)
        assert "::steperr::" not in capsys.readouterr().err

    def test_a_venv_step_failing_is_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        co = tmp_path / "wt"
        co.mkdir()
        monkeypatch.setattr(prov, "_find_python", lambda version="3.12": sys.executable)
        self._fake_run({"venv": 1}, monkeypatch)

        assert prov.ensure_venv(co) is False
        markers = [
            ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("::steperr::")
        ]
        assert len(markers) == 1
        assert markers[0].endswith("::%s failed loudly" % str(co / ".venv"))


class TestProvisionStaysStdoutClean:
    def test_markers_go_to_stderr_never_stdout(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """``pod up --json --provision`` prints JSON on stdout; the relay and the
        markers must not land there."""
        script = "import sys; print('to stdout'); print('to stderr', file=sys.stderr); sys.exit(1)"
        prov._run(_script_step(script), tmp_path)
        prov._fail()
        out, err = capfd.readouterr()
        assert out == ""
        assert "to stdout" in err and "to stderr" in err
        assert "::steperr::" in err
