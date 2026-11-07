"""The gateway run-marker: written by the running gateway, read by clients.

Covers the marker and its sidecars, the Windows sharing-violation retry on the
sidecar readers, and how clients discover a gateway on a non-default port.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading
from pathlib import Path

import pytest

# ── gateway run-marker (mint prefers the running gateway's install) ───────────


class TestRunMarker:
    def test_write_read_clear(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.instances import run_marker

        # Fabricate a venv layout: <venv>/bin/{python,kirocrew}, with the
        # launcher named as this OS's pip names it (kirocrew.exe on Windows).
        bindir = tmp_path / "venv" / "bin"
        bindir.mkdir(parents=True)
        launcher = bindir / ("kirocrew.exe" if os.name == "nt" else "kirocrew")
        launcher.write_text("#!/bin/sh\n")
        launcher.chmod(0o755)
        monkeypatch.setattr(sys, "executable", str(bindir / "python"))

        assert run_marker.gateway_launcher_path() == str(launcher)

        run_marker.write_marker(7879)
        marker = run_marker.marker_path(7879)
        assert marker.read_text(encoding="utf-8").strip() == str(launcher)
        # The pid sidecar rides alongside and names THIS process.
        assert run_marker.read_pid(7879) == os.getpid()
        # The start identity is its OWN file, so the pid file stays a bare pid
        # that main's shipped whole-file isdigit() reader still parses.
        start = run_marker._start_path_for(run_marker.pid_path(7879))
        assert start.name == "gateway-7879.start"
        assert start.read_text(encoding="utf-8").strip() == run_marker.pid_start_token(os.getpid())

        run_marker.clear_marker(7879)
        assert not marker.exists()
        assert not run_marker.pid_path(7879).exists()  # sidecar cleared too
        assert not start.exists()  # ...and so is the start identity it attests
        assert run_marker.read_pid(7879) is None
        run_marker.clear_marker(7879)  # clearing a missing marker is a no-op

    def test_port_only_marker_when_launcher_absent(self, tmp_path, monkeypatch):
        """No console script → still write the marker, but empty.

        Discovery needs only the filename, so skipping the write would deny
        discovery to source-tree launches. Mint stays unaffected because its
        shell clause requires a non-empty executable path.
        """
        import sys

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.instances import run_marker

        bindir = tmp_path / "venv2" / "bin"
        bindir.mkdir(parents=True)  # no sibling 'kirocrew' launcher
        monkeypatch.setattr(sys, "executable", str(bindir / "python"))

        assert run_marker.gateway_launcher_path() is None
        run_marker.write_marker(7000)
        marker = run_marker.marker_path(7000)
        assert marker.exists()
        assert marker.read_text(encoding="utf-8") == ""
        # Discoverable by port...
        assert run_marker.marker_ports() == [7000]

    def test_mint_clause_ignores_an_empty_marker(self):
        """...and inert for mint: the exec is guarded on a non-empty -x path."""
        from kiro_crew.instances.token_mint import build_candidate_command

        cmd = build_candidate_command("status", marker_port=7000)
        assert '[ -n "$__kb" ] && [ -x "$__kb" ]' in cmd

    def test_run_dir_lockdown_is_the_directory_helper(self, tmp_path, monkeypatch):
        """``run/`` holds the gateway's credential, pid and launcher marker.

        ``os.chmod(0o700)`` is a no-op on Windows; ``platform_compat.restrict_dir_to_owner``
        grants ``(OI)(CI)``, which also covers the ``.pid`` and ``.bin`` sidecars.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew import platform_compat
        from kiro_crew.instances import run_marker

        seen: list[str] = []
        # Re-patch over the session-wide Windows stub in conftest so this
        # assertion is not vacuous on the Windows matrix.
        monkeypatch.setattr(platform_compat, "restrict_dir_to_owner", lambda p: seen.append(str(p)))
        d = run_marker._run_dir()
        assert seen == [str(d)]

    def test_run_dir_lockdown_failure_is_best_effort(self, tmp_path, monkeypatch):
        """A refused lockdown must not break gateway startup.

        ``restrict_dir_to_owner`` is fail-loud by design, but ``_run_dir`` is on
        the path of ``secret_path()``, which the dashboard calls outside a
        try/except. The existing contract — tighten if possible, otherwise carry
        on — is what the caller depends on, so the raise is absorbed here.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew import platform_compat
        from kiro_crew.instances import run_marker

        def _refuse(_path):
            raise OSError("lockdown refused")

        monkeypatch.setattr(platform_compat, "restrict_dir_to_owner", _refuse)
        d = run_marker._run_dir()
        assert d.is_dir()


# ── Windows sharing-violation retry on the sidecar readers (GH-12364) ──


class TestRunMarkerSidecarReadRetry:
    """A sidecar reader must not fold a transient Windows sharing violation into
    ``""`` / ``None``.

    On Windows a read inside the ``os.replace`` window raises ``WinError 32``; folding
    that to ``""`` would report a running gateway as absent. Every reader goes through
    ``atomic_write.read_bytes_with_retry``.

    ``read_sharing_violation`` patches ``Path.read_bytes`` (uncapped readers); the
    capped readers (``read_pid``, ``.start``) are faulted at ``atomic_write._read_bytes``.
    POSIX permits the read, so the fault exists only in the simulator.
    """

    @pytest.fixture(autouse=True)
    def _no_backoff_sleep(self, _floor_monkeypatch):
        """Keep the bounded retry instant; attempt COUNT is what matters here.

        The real-file race lives in ``TestRunMarkerSidecarConcurrentRewrite``, outside
        this fixture, because with the sleep zeroed the budget fits in one writer syscall.

        Uses ``_floor_monkeypatch``: a mid-body ``monkeypatch.undo()`` would lift the
        shared one (semgrep ``kirocrew.test-autouse-shared-monkeypatch``).
        """
        from kiro_crew import atomic_write as aw

        _floor_monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)

    def _write_full_marker(self, run_marker, port, monkeypatch, tmp_path):
        """Fabricate a venv launcher and write a full marker set for *port*."""
        bindir = tmp_path / "venv" / "bin"
        bindir.mkdir(parents=True)
        launcher = bindir / ("kirocrew.exe" if os.name == "nt" else "kirocrew")
        launcher.write_text("#!/bin/sh\n")
        launcher.chmod(0o755)
        monkeypatch.setattr(sys, "executable", str(bindir / "python"))
        run_marker.write_marker(port)
        return launcher

    def test_read_secret_retries_the_sharing_violation(self, tmp_path, monkeypatch):
        """A transient fault on the credential sidecar retries, not folds to ""."""
        from windows_sim import read_sharing_violation

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew import platform_compat
        from kiro_crew.atomic_write import atomic_write
        from kiro_crew.instances import run_marker

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        atomic_write(run_marker.secret_path(6776), "s3cr3t-value", mode=0o600)

        with read_sharing_violation(match="gateway-6776.secret", times=2) as sim:
            got = run_marker.read_secret(6776)

        assert got == "s3cr3t-value"  # not "" -- the window was ridden out
        assert sim["n"] == 3  # 2 faults + the successful read

    def test_read_launcher_retries_the_sharing_violation(self, tmp_path, monkeypatch):
        """The marker (launcher path) reader rides the window out too."""
        from windows_sim import read_sharing_violation

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew import platform_compat
        from kiro_crew.instances import run_marker

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        launcher = self._write_full_marker(run_marker, 6776, monkeypatch, tmp_path)

        with read_sharing_violation(match="gateway-6776.bin", times=2) as sim:
            got = run_marker.read_launcher(6776)

        assert got == str(launcher)  # not None
        assert sim["n"] == 3

    def test_read_launcher_folds_only_when_the_whole_budget_is_exhausted(
        self, tmp_path, monkeypatch
    ):
        """A window that outlasts the ENTIRE retry budget still folds to None.

        All ``_REPLACE_MAX_ATTEMPTS`` reads fault, so ``read_launcher`` folds the final
        ``PermissionError`` to ``None``. The budget is finite by design.
        """
        from windows_sim import read_sharing_violation

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew import atomic_write as aw
        from kiro_crew import platform_compat
        from kiro_crew.instances import run_marker

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        self._write_full_marker(run_marker, 6776, monkeypatch, tmp_path)

        # Fault every attempt: the budget is _REPLACE_MAX_ATTEMPTS reads, so
        # faulting that many (and one more, to be unambiguous) leaves no read to
        # succeed.
        with read_sharing_violation(
            match="gateway-6776.bin", times=aw._REPLACE_MAX_ATTEMPTS + 1
        ) as sim:
            got = run_marker.read_launcher(6776)

        assert got is None  # the whole budget was spent inside the window
        assert sim["n"] == aw._REPLACE_MAX_ATTEMPTS  # every attempt faulted

    def test_read_pid_retries_the_sharing_violation(self, tmp_path, monkeypatch):
        """read_pid -- the identity claim the issue names -- must not fold to None.

        read_pid reads through the capped path (``max_bytes``), so it is faulted
        at ``_read_bytes`` rather than ``Path.read_bytes``.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew import atomic_write as aw
        from kiro_crew import platform_compat
        from kiro_crew.instances import run_marker

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        self._write_full_marker(run_marker, 6776, monkeypatch, tmp_path)
        pid_name = run_marker.pid_path(6776).name

        real_read_bytes = aw._read_bytes
        state = {"n": 0}

        def _faulting(target, max_bytes):
            if target.name == pid_name:
                state["n"] += 1
                if state["n"] <= 2:
                    raise PermissionError(
                        f"[WinError 32] simulated sharing violation reading {target}"
                    )
            return real_read_bytes(target, max_bytes)

        monkeypatch.setattr(aw, "_read_bytes", _faulting)
        got = run_marker.read_pid(6776)

        assert got == os.getpid()  # not None -- the window was ridden out
        assert state["n"] == 3

    def test_absent_marker_still_reads_as_empty(self, tmp_path, monkeypatch):
        """A GENUINELY absent file still yields ""/None -- absence is unchanged.

        FileNotFoundError is not a sharing violation; the helper does not retry
        it and the readers fold it to their empty value exactly as before.
        """
        from windows_sim import read_sharing_violation

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew import platform_compat
        from kiro_crew.instances import run_marker

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

        # No marker set written at all. Even under an active fault simulator, a
        # missing file raises FileNotFoundError, not PermissionError, so it is
        # not retried and the empty return is preserved.
        with read_sharing_violation(times=10_000):
            assert run_marker.read_secret(9999) == ""
            assert run_marker.read_launcher(9999) is None
        assert run_marker.read_pid(9999) is None

    def test_no_contention_common_path_is_unchanged(self, tmp_path, monkeypatch):
        """With no concurrent writer the readers return the same values as before.

        The retry helper is a plain read on POSIX and on the no-fault Windows
        path, so the common case is byte-for-byte prior behaviour.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.instances import run_marker

        launcher = self._write_full_marker(run_marker, 7001, monkeypatch, tmp_path)
        assert run_marker.read_launcher(7001) == str(launcher)
        assert run_marker.read_pid(7001) == os.getpid()


class TestRunMarkerSidecarConcurrentRewrite:
    """Real files, real threads: each read overlaps at most ONE in-flight write.

    Kept outside ``TestRunMarkerSidecarReadRetry`` so the real 50 ms backoff stays in
    force: it is what rides out the Windows ``WinError 32`` window of one replace.

    A three-way lock-step (``about_to_read``, ``published``, ``read_done``) bounds each
    read to one replace, so ``got in values`` can stay strict; a tight loop could chain
    several windows and exhaust the budget. Each ``threading.Event`` is cleared by its
    consumer so signals never coalesce across rounds.
    """

    def test_each_read_overlaps_one_acknowledged_replace_without_tearing(
        self, tmp_path, monkeypatch
    ):
        """Every read returns a value some writer published: never torn, never ``None``.

        ``publishes == rounds`` and the surfaced writer error keep the race from passing
        vacuously against the seeded value. The launcher sidecar stands in for all
        sidecars, since ``read_bytes_with_retry`` is shared.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.atomic_write import atomic_write
        from kiro_crew.instances import run_marker
        from kiro_crew.testing.wait import default_timeout

        marker = run_marker.marker_path(6776)
        values = {f"/opt/venv-{i}/bin/kirocrew" for i in range(50)}
        atomic_write(marker, "/opt/venv-0/bin/kirocrew\n", mode=0o600)

        rounds = 500
        stop = threading.Event()
        about_to_read = threading.Event()
        published = threading.Event()
        read_done = threading.Event()
        writer_error: list[BaseException] = []
        publishes = {"n": 0}

        def _rewrite():
            i = 1
            try:
                # One acknowledged replace per round. The reader's
                # ``about_to_read`` releases this round's single ``atomic_write``,
                # so it overlaps that round's read; ``published`` acknowledges the
                # completed replace to the reader; ``read_done`` then gates the
                # next round so no replace begins until the current read finishes.
                # Each event is cleared by its consumer, so every signal is fresh
                # and none coalesces across rounds.
                while not stop.is_set():
                    if not about_to_read.wait(default_timeout()):
                        return  # the reader stopped signalling; nothing left to race
                    about_to_read.clear()
                    if stop.is_set():
                        return
                    atomic_write(marker, f"/opt/venv-{i % 50}/bin/kirocrew\n", mode=0o600)
                    i += 1
                    publishes["n"] += 1
                    published.set()  # acknowledge this round's one completed replace
                    assert read_done.wait(default_timeout()), "the reader never finished its read"
                    read_done.clear()
            except BaseException as exc:  # surfaced on the main thread below
                writer_error.append(exc)

        writer = threading.Thread(target=_rewrite, daemon=True)
        writer.start()
        try:
            for _ in range(rounds):
                # Release this round's single replace, then read concurrently so
                # the read overlaps that one in-flight rename.
                about_to_read.set()
                got = run_marker.read_launcher(6776)
                assert got in values, f"a reader saw a torn value or a false absence: {got!r}"
                # The writer must have completed exactly one replace this round;
                # without this the loop could spin to completion against the
                # seeded value while the writer lagged or never ran.
                assert published.wait(default_timeout()), "the writer never published this round"
                published.clear()
                read_done.set()  # let the writer wait for the next round
        finally:
            stop.set()
            # Let the writer out of either wait so it can observe ``stop``.
            about_to_read.set()
            read_done.set()
            writer.join(default_timeout())
        assert not writer.is_alive(), "the writer thread did not stop"
        assert not writer_error, f"the writer died mid-run: {writer_error[0]!r}"
        # A non-publishing writer cannot reach here green: exactly one replace was
        # acknowledged per round, so the reader observed values the writer
        # published, not just the seeded one.
        assert publishes["n"] == rounds, f"writer published {publishes['n']} of {rounds} replaces"


# ── run-marker port discovery (clients find a gateway on a non-default port) ──


class TestRunMarkerDiscovery:
    """``marker_ports`` — filename-only discovery.

    Backs ``cli_server.resolve_client_port``'s fallback: no directory creation. Trust
    in a port is checked in ``TestResolveClientPortRunMarker`` (a listener is not
    proof of identity).
    """

    def _marker(self, home, name: str) -> None:
        d = home / "run"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text("/some/venv/bin/kirocrew\n", encoding="utf-8")

    def test_no_run_dir_yields_no_ports_and_creates_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.instances import run_marker

        assert run_marker.marker_ports() == []
        # Discovery is read-only: a client merely looking for a gateway must not
        # materialise run/ (marker_path() would, via _run_dir()).
        assert not (tmp_path / "run").exists()

    def test_lists_sorted_ports_and_ignores_non_markers(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.instances import run_marker

        self._marker(tmp_path, "gateway-6776.bin")
        self._marker(tmp_path, "gateway-5476.bin")
        # Non-markers / malformed ports must be ignored rather than crash:
        for junk in (
            "gateway-.bin",
            "gateway-abc.bin",
            "gateway-6776.bin.old",
            "gateway--1.bin",
            "gateway-0.bin",  # not a usable TCP port
            "gateway-70000.bin",  # out of range
            "gateway-67 76.bin",
            "sandbox-6776.bin",
        ):
            self._marker(tmp_path, junk)
        # A directory that merely looks like a marker is not a marker.
        (tmp_path / "run" / "gateway-9999.bin").mkdir()

        assert run_marker.marker_ports() == [5476, 6776]

    def test_no_bare_liveness_helper_is_exposed(self, tmp_path, monkeypatch):
        """Reachability must not be offered as a stand-in for identity.

        A client command sends the local secret to whatever answers on the
        discovered port, so "something is listening" is not a safe basis for
        trusting a marker. Keeping any such helper out of this module stops a
        future caller from reaching for the unsafe check.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.instances import run_marker

        assert not hasattr(run_marker, "port_is_live")
        assert not hasattr(run_marker, "live_marker_ports")

    def test_read_pid_rejects_junk_and_missing(self, tmp_path, monkeypatch):
        """The sidecar is an identity claim, so parse it strictly."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.instances import run_marker

        assert run_marker.read_pid(6776) is None  # absent
        d = tmp_path / "run"
        d.mkdir(parents=True, exist_ok=True)
        for junk in ("", "  ", "abc", "-1", "0", "12 34", "12.5", "1e3"):
            (d / "gateway-6776.pid").write_text(junk, encoding="utf-8")
            assert run_marker.read_pid(6776) is None, junk
        (d / "gateway-6776.pid").write_text(" 4242 \n", encoding="utf-8")
        assert run_marker.read_pid(6776) == 4242

    def test_explicit_pid_reader_is_bounded_ascii_decimal_and_read_only(self, tmp_path):
        from kiro_crew.instances import run_marker

        path = tmp_path / "missing" / "gateway.pid"
        assert run_marker._read_pid_path(path) is None
        assert not path.parent.exists()
        path.parent.mkdir()
        for junk in (b"\xd9\xa4\xd9\xa2", b"+42", b"-42", b"0", b"9" * 65):
            path.write_bytes(junk)
            assert run_marker._read_pid_path(path) is None
        path.write_bytes(b" 4242 \n")
        assert run_marker._read_pid_path(path) == 4242
        path.write_bytes(b"9" * 64)
        assert run_marker._read_pid_path(path) == int(b"9" * 64)

    def test_the_start_identity_is_read_from_its_own_sidecar(self, tmp_path):
        """The token lives beside the pid file, never inside it.

        Keeping the pid file a bare pid is what lets the reader SHIPPED on an
        older client -- whole file, stripped, ``isdigit()`` -- keep parsing a
        record this gateway wrote, instead of answering ``None`` and denying a
        gateway that genuinely is ours.
        """
        from kiro_crew.instances import run_marker

        pid_file = tmp_path / "gateway-7999.pid"
        pid_file.write_bytes(b"4242\n")
        start = run_marker._start_path_for(pid_file)
        assert start == tmp_path / "gateway-7999.start"

        # No sidecar: a pid with no proof of freshness, never a wildcard match.
        assert run_marker.read_pid_record_path(pid_file) == (4242, "")
        start.write_bytes(b"246853591\n")
        assert run_marker.read_pid_record_path(pid_file) == (4242, "246853591")

        # Defensive read-side parsing: oversized, non-ASCII and whitespace-bearing
        # sidecars all read as unproven rather than as some other process's value.
        for junk in (b"9" * 129, b"\xd9\xa4\xd9\xa2", b"12 34", b"12\n34"):
            start.write_bytes(junk)
            assert run_marker.read_pid_record_path(pid_file) == (4242, ""), junk

        # Reading a record never materialises the sidecar it looked for.
        absent = tmp_path / "elsewhere" / "gateway-7000.pid"
        assert run_marker.read_pid_record_path(absent) is None
        assert not absent.parent.exists()

    def test_the_pid_body_still_parses_under_the_reader_shipped_on_main(
        self, tmp_path, monkeypatch
    ):
        """The pid file holds only digits, so an older client's reader still parses it.

        That reader takes the WHOLE ``.pid`` file, stripped, gated on ``isdigit()``; a
        second line would break an older client venv sharing this data home.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.instances import run_marker

        run_marker.write_marker(7999)
        pid_file = run_marker.pid_path(7999)
        # The invariant is "one line holding the bare pid", not a literal byte
        # string: atomic_write opens in text mode, so Windows translates the
        # trailing \n to \r\n — for THIS branch and for main's writer alike,
        # which is exactly why the shipped reader strips before isdigit().
        body = pid_file.read_bytes()
        assert body.decode("ascii").splitlines() == [str(os.getpid())]

        shipped = run_marker._read_sidecar(7999, ".pid")  # main's read, verbatim
        assert shipped.isdigit()
        assert int(shipped) == os.getpid()

        # The identity is still recorded -- just in the file next to it.
        assert run_marker.read_pid_record_path(pid_file) == (
            os.getpid(),
            run_marker.pid_start_token(os.getpid()),
        )

    def test_start_identity_comes_from_the_shared_start_id_producer(self, monkeypatch):
        """One producer for the value written and the value compared.

        ``platform_compat.get_process_start_id`` is microsecond-resolution on
        macOS, unlike ``process_start_time``'s ``ps -o lstart=`` spelling, whose
        1-second granularity lets a pid recycled inside the same second forge an
        identical value.
        """
        from kiro_crew import platform_compat
        from kiro_crew.instances import run_marker

        monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: "1730.000042")
        assert run_marker.pid_start_token(4242) == "1730.000042"

        # The preferred producer answers None on an unrecognised host or a
        # failed read. When it says nothing, the fallback is consulted rather
        # than the token going empty -- that fallback is the ONLY start identity
        # available on Windows (a creation FILETIME), so skipping it would make a
        # pod there permanently unprovable.
        monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: None)
        monkeypatch.setattr(platform_compat, "process_start_time", lambda pid: "133724160000000000")
        assert run_marker.pid_start_token(4242) == "133724160000000000"

        # A space-padded fallback (the macOS `ps -o lstart=` spelling) is
        # collapsed to a single token, because the reader refuses inner whitespace.
        monkeypatch.setattr(
            platform_compat, "process_start_time", lambda pid: "Thu Sep  4 00:00:00 2026"
        )
        assert run_marker.pid_start_token(4242) == "Thu-Sep-4-00:00:00-2026"

        # Only when NEITHER producer will answer is the token unproven.
        monkeypatch.setattr(platform_compat, "process_start_time", lambda pid: None)
        assert run_marker.pid_start_token(4242) == ""

        def _boom(pid: int) -> str:
            raise OSError("libproc exploded")

        monkeypatch.setattr(platform_compat, "get_process_start_id", _boom)
        monkeypatch.setattr(platform_compat, "process_start_time", _boom)
        assert run_marker.pid_start_token(4242) == ""

        # A raising primary must still let the working fallback answer.
        monkeypatch.setattr(platform_compat, "process_start_time", lambda pid: "133724160000000001")
        assert run_marker.pid_start_token(4242) == "133724160000000001"

    def test_read_launcher_reads_marker_content(self, tmp_path, monkeypatch):
        """read_launcher returns the recorded path, None for absent/empty, and
        never creates run/ (read-only, like read_pid)."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.instances import run_marker

        assert run_marker.read_launcher(6776) is None  # absent
        assert not (tmp_path / "run").exists()  # read never materialises run/
        d = tmp_path / "run"
        d.mkdir(parents=True, exist_ok=True)
        # Empty marker: a source-tree launch records no launcher (port-only).
        (d / "gateway-6776.bin").write_text("", encoding="utf-8")
        assert run_marker.read_launcher(6776) is None
        (d / "gateway-6776.bin").write_text("  \n", encoding="utf-8")
        assert run_marker.read_launcher(6776) is None
        (d / "gateway-6776.bin").write_text("/opt/venv/bin/kirocrew\n", encoding="utf-8")
        assert run_marker.read_launcher(6776) == "/opt/venv/bin/kirocrew"

    def test_write_prunes_markers_from_earlier_runs(self, tmp_path, monkeypatch):
        """A gateway is a singleton per home, so markers naming other ports are
        crash residue. Left alone they cost every client command an extra
        listener lookup, so the live gateway reaps them when it writes its own.
        """
        import sys

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.instances import run_marker

        bindir = tmp_path / "venv" / "bin"
        bindir.mkdir(parents=True)
        launcher = bindir / "kirocrew"
        launcher.write_text("#!/bin/sh\n")
        launcher.chmod(0o755)
        monkeypatch.setattr(sys, "executable", str(bindir / "python"))

        # Residue from three earlier runs on other ports.
        self._marker(tmp_path, "gateway-5476.bin")
        (tmp_path / "run" / "gateway-5476.pid").write_text("111\n", encoding="utf-8")
        (tmp_path / "run" / "gateway-5476.start").write_text("111-start\n", encoding="utf-8")
        self._marker(tmp_path, "gateway-6777.bin")
        self._marker(tmp_path, "gateway-9001.bin")

        run_marker.write_marker(6776)

        assert run_marker.marker_ports() == [6776]
        assert not (tmp_path / "run" / "gateway-5476.pid").exists()
        # The start identity goes with the pid it attests; left behind it would
        # pair a dead generation's token with a pid file a later gateway rewrites.
        assert not (tmp_path / "run" / "gateway-5476.start").exists()
        assert run_marker.read_pid(6776) == os.getpid()

    def test_prune_keeps_unrelated_files(self, tmp_path, monkeypatch):
        """Pruning targets only this module's own marker/pid pairs."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.instances import run_marker

        d = tmp_path / "run"
        d.mkdir(parents=True, exist_ok=True)
        (d / "sandbox-6776.bin").write_text("keep me", encoding="utf-8")
        (d / "gateway-abc.bin").write_text("keep me", encoding="utf-8")
        self._marker(tmp_path, "gateway-6777.bin")

        run_marker.prune_markers(keep_port=6776)

        assert not (d / "gateway-6777.bin").exists()
        assert (d / "sandbox-6776.bin").exists()
        assert (d / "gateway-abc.bin").exists()


class TestDataHomePointer:
    """``gateway-<port>.home`` in the default run dir names a non-default data home."""

    @pytest.fixture
    def run_marker(self, tmp_path, monkeypatch):
        from kiro_crew.instances import run_marker

        default_run = tmp_path / "default" / "run"
        default_run.mkdir(parents=True)
        monkeypatch.setattr(run_marker, "_DEFAULT_RUN_DIR", default_run)
        monkeypatch.setattr(run_marker, "gateway_launcher_path", lambda: "/venv/bin/kirocrew")
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew-wrapped"))
        monkeypatch.setattr(
            run_marker, "_PUBLISHED_LISTENERS", {5476: {"127.0.0.1"}, 5477: {"127.0.0.1"}}
        )
        return run_marker

    def test_writes_a_pointer_of_its_data_home_then_its_launcher(self, run_marker):
        import stat

        from kiro_crew.config.paths import config_dir

        run_marker.write_marker(5477)

        pointer = run_marker._DEFAULT_RUN_DIR / "gateway-5477.home"
        assert pointer.read_text(encoding="utf-8") == f"{config_dir()}\n/venv/bin/kirocrew\n"
        if os.name == "posix":
            assert stat.S_IMODE(pointer.stat().st_mode) == 0o600

    def test_writes_an_empty_launcher_line_for_a_source_tree_launch(self, run_marker, monkeypatch):
        from kiro_crew.config.paths import config_dir

        monkeypatch.setattr(run_marker, "gateway_launcher_path", lambda: None)
        run_marker.write_marker(5477)

        pointer = run_marker._DEFAULT_RUN_DIR / "gateway-5477.home"
        assert pointer.read_text(encoding="utf-8") == f"{config_dir()}\n\n"

    def test_the_default_home_prune_leaves_another_homes_pointer(self, run_marker, monkeypatch):
        from kiro_crew import port_resolution
        from kiro_crew.config.paths import config_dir

        run_marker.write_marker(5477)
        wrapped_home = config_dir()
        default_run = run_marker._DEFAULT_RUN_DIR
        (default_run / "gateway-5477.bin").write_text("", encoding="utf-8")  # stale, prunable
        monkeypatch.setattr(port_resolution, "_gateway_owns_port", lambda port: False)
        monkeypatch.setenv("KIROCREW_HOME", str(default_run.parent))

        run_marker.write_marker(5476)

        assert run_marker.marker_ports() == [5476]
        assert (default_run / "gateway-5477.home").read_text(encoding="utf-8") == (
            f"{wrapped_home}\n/venv/bin/kirocrew\n"
        )

    @pytest.mark.parametrize(
        "cover",
        [("127.0.0.1",), ("0.0.0.0",), ("127.0.0.1", "::1")],
        ids=["127.0.0.1", "0.0.0.0", "dual-stack"],
    )
    def test_a_default_home_gateway_removes_a_pointer_left_for_its_own_port(
        self, run_marker, monkeypatch, cover
    ):
        monkeypatch.setattr(run_marker, "_PUBLISHED_LISTENERS", {5476: set(cover)})
        default_run = run_marker._DEFAULT_RUN_DIR
        (default_run / "gateway-5476.home").write_text("/elsewhere\n", encoding="utf-8")
        monkeypatch.setenv("KIROCREW_HOME", str(default_run.parent))

        run_marker.write_marker(5476)

        assert not (default_run / "gateway-5476.home").exists()
        assert run_marker.read_pid(5476) == os.getpid()

    @pytest.mark.parametrize(
        "listeners",
        [("127.0.0.2",), ("::1",), ()],
        ids=["127.0.0.2", "::1", "none"],
    )
    @pytest.mark.parametrize("default_home", [False, True], ids=["wrapped", "default"])
    def test_a_gateway_without_127_0_0_1_cover_leaves_the_pointer_alone(
        self, run_marker, monkeypatch, caplog, default_home, listeners
    ):
        published = {5477: set(listeners)} if listeners else {}
        monkeypatch.setattr(run_marker, "_PUBLISHED_LISTENERS", published)
        if default_home:
            monkeypatch.setenv("KIROCREW_HOME", str(run_marker._DEFAULT_RUN_DIR.parent))
        pointer = run_marker._DEFAULT_RUN_DIR / "gateway-5477.home"
        seeded = b"/elsewhere\n/other/bin/kirocrew\n"
        pointer.write_bytes(seeded)

        with caplog.at_level(logging.INFO):
            run_marker.write_marker(5477)

        assert pointer.read_bytes() == seeded
        assert any(
            r.levelno == logging.INFO and "no published listener covers 127.0.0.1" in r.getMessage()
            for r in caplog.records
        )
        assert run_marker.read_pid(5477) == os.getpid()

    def test_writes_no_pointer_when_the_pid_record_names_another_live_gateway(
        self, run_marker, monkeypatch
    ):
        pid_file = run_marker.pid_path(5477)
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text("424242\n", encoding="utf-8")
        run_marker._start_path_for(pid_file).write_text("live-token\n", encoding="utf-8")
        monkeypatch.setattr(run_marker.platform_compat, "pid_exists", lambda pid: True)
        monkeypatch.setattr(run_marker, "pid_start_token", lambda pid: "live-token")

        run_marker.write_marker(5477)

        assert run_marker.read_pid(5477) == 424242
        assert not (run_marker._DEFAULT_RUN_DIR / "gateway-5477.home").exists()

    def test_a_pod_writes_no_pointer_while_the_same_gateway_outside_one_does(
        self, run_marker, monkeypatch
    ):
        pointer = run_marker._DEFAULT_RUN_DIR / "gateway-5477.home"
        monkeypatch.setenv("KIROCREW_POD", "1")
        run_marker.write_marker(5477)
        assert not pointer.exists()

        monkeypatch.delenv("KIROCREW_POD")
        run_marker.write_marker(5477)
        assert pointer.exists()

    def test_never_creates_the_default_run_dir_but_writes_once_it_exists(self, run_marker):
        default_run = run_marker._DEFAULT_RUN_DIR
        default_run.rmdir()

        run_marker.write_marker(5477)
        assert not default_run.exists()

        default_run.mkdir()
        run_marker.write_marker(5477)
        assert (default_run / "gateway-5477.home").exists()

    def test_warns_that_the_pointer_was_skipped_because_the_run_dir_is_not_a_dir(
        self, run_marker, caplog
    ):
        run_marker._DEFAULT_RUN_DIR.rmdir()

        with caplog.at_level(logging.INFO):
            run_marker.write_marker(5477)

        assert any(
            r.levelno == logging.WARNING
            and "5477" in r.getMessage()
            and "is not a dir" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.parametrize(
        "pod, dir_exists",
        [(False, True), (True, False)],
        ids=["pointer-written", "pod-without-dir"],
    )
    def test_logs_no_skip_line_when_the_pointer_is_written_or_in_a_pod(
        self, run_marker, monkeypatch, caplog, pod, dir_exists
    ):
        if pod:
            monkeypatch.setenv("KIROCREW_POD", "1")
        if not dir_exists:
            run_marker._DEFAULT_RUN_DIR.rmdir()

        with caplog.at_level(logging.DEBUG):
            run_marker.write_marker(5477)

        assert not any("No pointer for port 5477" in r.getMessage() for r in caplog.records)

    def test_a_failed_pointer_write_warns_naming_the_pointer_and_keeps_the_marker(
        self, run_marker, monkeypatch, caplog
    ):
        monkeypatch.setattr(run_marker, "pid_start_token", lambda pid: "own-token")
        pointer = run_marker._DEFAULT_RUN_DIR / "gateway-5477.home"
        pointer.mkdir()

        with caplog.at_level(logging.INFO):
            run_marker.write_marker(5477)

        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        prefix = "Could not write the run-marker or pointer for port 5477: "
        assert any(m.startswith(prefix) and pointer.name in m for m in warnings)
        assert run_marker.read_pid(5477) == os.getpid()
        assert run_marker.marker_path(5477).read_text(encoding="utf-8") == "/venv/bin/kirocrew\n"
        start = run_marker._start_path_for(run_marker.pid_path(5477))
        assert start.read_text(encoding="utf-8") == "own-token\n"

    @pytest.mark.parametrize(
        "cover",
        [("127.0.0.1",), ("0.0.0.0",), ("127.0.0.1", "::1")],
        ids=["127.0.0.1", "0.0.0.0", "dual-stack"],
    )
    def test_clear_marker_removes_the_pointer_naming_this_home(
        self, run_marker, monkeypatch, cover
    ):
        """The pointer is LF where atomic_write's default is CRLF."""
        from kiro_crew.config.paths import config_dir

        monkeypatch.setattr(run_marker, "_PUBLISHED_LISTENERS", {5477: set(cover)})

        real = run_marker.atomic_write

        def windows_atomic_write(path, content, *, newline=None, **kwargs):
            if newline is None and isinstance(content, str):
                return real(path, content.replace("\n", "\r\n"), newline="", **kwargs)
            return real(path, content, newline=newline, **kwargs)

        monkeypatch.setattr(run_marker, "atomic_write", windows_atomic_write)
        run_marker.write_marker(5477)
        pointer = run_marker._DEFAULT_RUN_DIR / "gateway-5477.home"
        assert pointer.read_bytes() == os.fsencode(config_dir()) + b"\n/venv/bin/kirocrew\n"

        run_marker.clear_marker(5477)

        assert not pointer.exists()

    @pytest.mark.parametrize(
        "this_home_plus_cr", [False, True], ids=["elsewhere", "this-home-plus-cr"]
    )
    def test_clear_marker_leaves_a_pointer_naming_another_home(self, run_marker, this_home_plus_cr):
        from kiro_crew.config.paths import config_dir

        pointer = run_marker._DEFAULT_RUN_DIR / "gateway-5477.home"
        home = os.fsencode(config_dir()) + b"\r" if this_home_plus_cr else b"/elsewhere"
        pointer.write_bytes(home + b"\n/venv/bin/kirocrew\n")

        run_marker.clear_marker(5477)

        assert pointer.exists()

    def test_clear_marker_leaves_its_own_homes_pointer_without_127_0_0_1_cover(
        self, run_marker, monkeypatch
    ):
        from kiro_crew.config.paths import config_dir

        monkeypatch.setattr(run_marker, "_PUBLISHED_LISTENERS", {5477: {"127.0.0.2"}})
        pointer = run_marker._DEFAULT_RUN_DIR / "gateway-5477.home"
        own = os.fsencode(config_dir()) + b"\n/venv/bin/kirocrew\n"
        pointer.write_bytes(own)

        run_marker.clear_marker(5477)

        assert pointer.read_bytes() == own
        assert run_marker.published_listeners(5477) == frozenset()

    @pytest.mark.parametrize("case", ["missing", "this-home", "other-home"])
    def test_clear_marker_forgets_its_listeners_and_its_pointer_even_when_not_utf8(
        self, run_marker, monkeypatch, case
    ):
        from kiro_crew.config.paths import config_dir

        monkeypatch.setattr(run_marker, "_PUBLISHED_LISTENERS", {})
        run_marker.note_published_listener(5477, "127.0.0.1")
        pointer = run_marker._DEFAULT_RUN_DIR / "gateway-5477.home"
        home = {"this-home": os.fsencode(config_dir()), "other-home": b"/elsewhere"}.get(case)
        if home is not None:
            pointer.write_bytes(home + b"\n\xff\n")

        run_marker.clear_marker(5477)

        assert run_marker.published_listeners(5477) == frozenset()
        assert pointer.exists() == (case == "other-home")

    def test_a_pointer_write_landing_after_the_clean_stop_removes_itself(
        self, run_marker, monkeypatch
    ):
        pointer = run_marker._DEFAULT_RUN_DIR / "gateway-5477.home"
        real = run_marker.atomic_write

        def stop_during_write(path, content, **kwargs):
            if Path(path) == pointer:
                run_marker.clear_marker(5477)
            return real(path, content, **kwargs)

        monkeypatch.setattr(run_marker, "atomic_write", stop_during_write)
        run_marker.write_marker(5477)

        assert not pointer.exists()

    def test_a_late_pointer_write_leaves_a_successors_pointer_in_place(
        self, run_marker, monkeypatch
    ):
        pointer = run_marker._DEFAULT_RUN_DIR / "gateway-5477.home"
        lock = run_marker._DEFAULT_RUN_DIR / "gateway-5477.home.lock"
        real = run_marker.atomic_write
        done = threading.Event()
        errors: list[BaseException] = []

        def successor():
            try:
                with run_marker._marker_lock(5477, lock) as locked:
                    assert locked
                    real(pointer, "/other-home\n/other/bin/kirocrew\n", mode=0o600, newline="")
            except BaseException as e:
                errors.append(e)
            finally:
                done.set()

        # The predecessor holds the pointer lock for the whole write, so the successor
        # can only land after write_marker returns; waiting for it inside the write
        # would just time out against that lock.
        thread = threading.Thread(target=successor)

        def predecessor_write(path, content, **kwargs):
            if Path(path) == pointer:
                run_marker.clear_marker(5477)
                thread.start()
            return real(path, content, **kwargs)

        monkeypatch.setattr(run_marker, "atomic_write", predecessor_write)
        run_marker.write_marker(5477)

        assert done.wait(timeout=10)
        thread.join(timeout=10)
        assert pointer.read_text(encoding="utf-8") == "/other-home\n/other/bin/kirocrew\n", errors

    def test_writes_no_pointer_and_logs_why_while_the_pointer_lock_is_held(
        self, run_marker, monkeypatch, caplog
    ):
        from kiro_crew import platform_compat

        monkeypatch.setattr(run_marker, "_MARKER_LOCK_TIMEOUT_SECS", 0.05)
        lock = run_marker._DEFAULT_RUN_DIR / "gateway-5477.home.lock"
        (run_marker._DEFAULT_RUN_DIR / "gateway-5477.home").write_text(
            "/stale-home\n/stale/bin/kirocrew\n", encoding="utf-8"
        )
        fd = os.open(os.fspath(lock), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            with platform_compat.file_lock(fd, exclusive=True), caplog.at_level(logging.INFO):
                run_marker.write_marker(5477)
        finally:
            os.close(fd)

        assert run_marker.marker_path(5477).exists()
        assert not (run_marker._DEFAULT_RUN_DIR / "gateway-5477.home").exists()
        assert any(
            "No pointer for port 5477" in r.getMessage() and "pointer lock" in r.getMessage()
            for r in caplog.records
        )

    def _stop_and_successor_before_pointer_lock(self, run_marker, monkeypatch, *, lock_taken):
        """Make the pointer lock acquire after a stop and a successor's pointer write."""
        lock = run_marker._DEFAULT_RUN_DIR / "gateway-5477.home.lock"
        pointer = run_marker._DEFAULT_RUN_DIR / "gateway-5477.home"
        real_lock = run_marker._marker_lock

        @contextlib.contextmanager
        def stop_then_lock(port, path=None):
            if path != lock:
                with real_lock(port, path) as locked:
                    yield locked
                return
            run_marker.clear_marker(5477)
            pointer.write_text("/other-home\n/other/bin/kirocrew\n", encoding="utf-8")
            if not lock_taken:
                yield False
                return
            with real_lock(port, path) as locked:
                yield locked

        monkeypatch.setattr(run_marker, "_marker_lock", stop_then_lock)

    def test_a_writer_whose_stop_ran_before_the_pointer_lock_writes_nothing(
        self, run_marker, monkeypatch
    ):
        self._stop_and_successor_before_pointer_lock(run_marker, monkeypatch, lock_taken=True)

        run_marker.write_marker(5477)

        pointer = run_marker._DEFAULT_RUN_DIR / "gateway-5477.home"
        assert pointer.read_text(encoding="utf-8") == "/other-home\n/other/bin/kirocrew\n"

    def test_a_stopped_writer_without_the_pointer_lock_leaves_the_successors_pointer(
        self, run_marker, monkeypatch
    ):
        self._stop_and_successor_before_pointer_lock(run_marker, monkeypatch, lock_taken=False)

        run_marker.write_marker(5477)

        pointer = run_marker._DEFAULT_RUN_DIR / "gateway-5477.home"
        assert pointer.read_text(encoding="utf-8") == "/other-home\n/other/bin/kirocrew\n"

    def test_an_unavailable_pointer_lock_drops_a_planted_pointer(self, run_marker, caplog):
        (run_marker._DEFAULT_RUN_DIR / "gateway-5477.home.lock").mkdir()
        pointer = run_marker._DEFAULT_RUN_DIR / "gateway-5477.home"
        pointer.write_text("/planted-home\n/planted/bin/kirocrew\n", encoding="utf-8")

        with caplog.at_level(logging.INFO):
            run_marker.write_marker(5477)

        assert run_marker.marker_path(5477).exists()
        assert not pointer.exists()
        assert any(
            "No pointer for port 5477" in r.getMessage() and "pointer lock" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.skipif(sys.platform == "win32", reason="creating symlinks needs privilege")
    def test_the_pointer_lock_does_not_follow_a_symlink(self, run_marker, tmp_path):
        target = tmp_path / "elsewhere" / "target"
        target.parent.mkdir()
        (run_marker._DEFAULT_RUN_DIR / "gateway-5477.home.lock").symlink_to(target)
        pointer = run_marker._DEFAULT_RUN_DIR / "gateway-5477.home"
        pointer.write_text("/planted-home\n/planted/bin/kirocrew\n", encoding="utf-8")

        run_marker.write_marker(5477)

        assert not target.exists()
        assert not pointer.exists()

    def test_clear_marker_forgets_the_port_before_it_reads_the_pointer(
        self, run_marker, monkeypatch
    ):
        from kiro_crew.config.paths import config_dir

        pointer = run_marker._DEFAULT_RUN_DIR / "gateway-5477.home"
        pointer.write_bytes(os.fsencode(config_dir()) + b"\n/venv/bin/kirocrew\n")
        seen = []
        real_read_bytes = Path.read_bytes

        def spy_read_bytes(self):
            if self == pointer:
                seen.append(run_marker.published_listeners(5477))
            return real_read_bytes(self)

        monkeypatch.setattr(Path, "read_bytes", spy_read_bytes)
        run_marker.clear_marker(5477)

        assert seen == [frozenset()]
        assert not pointer.exists()

    def test_spec_states_the_shutdown_wait_an_orphan_pointer_outlasts(self):
        from kiro_crew.slack.gateway_runtime.exit_path import _MARKER_WRITE_WAIT_SECS

        spec = Path(__file__).resolve().parent.parent / "docs/system-specs/modules/instances.md"
        text = " ".join(spec.read_text(encoding="utf-8").split())

        assert f"stalls past shutdown's {_MARKER_WRITE_WAIT_SECS:g} s wait" in text
