"""The gateway run-marker: written by the running gateway, read by clients.

Covers the marker and its sidecars, the Windows sharing-violation retry on the
sidecar readers, and how clients discover a gateway on a non-default port.
"""

from __future__ import annotations

import os
import sys
import threading

import pytest

# ── gateway run-marker (mint prefers the running gateway's install) ───────────


class TestRunMarker:
    def test_write_read_clear(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.instances import run_marker

        # Fabricate a venv layout: <venv>/bin/{python,kirocrew}
        bindir = tmp_path / "venv" / "bin"
        bindir.mkdir(parents=True)
        launcher = bindir / "kirocrew"
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

        A bare ``os.chmod(0o700)`` is a silent no-op on Windows, so the
        directory and everything created inside it keep the inherited DACL.
        The tightening must go through ``platform_compat.restrict_dir_to_owner``,
        whose Windows grants carry ``(OI)(CI)`` and so also cover the ``.pid``
        and ``.bin`` sidecars, which ``atomic_write(mode=0o600)`` cannot.
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

    The markers are published with ``mkstemp`` + ``os.replace``
    (``write_marker``). On Windows a reader that lands in the ``os.replace``
    window raises ``PermissionError`` (``WinError 32``). The old plain
    ``read_text`` folded that into ``""`` -- indistinguishable from a genuinely
    absent marker, so a caller checking whether a gateway runs on a port got a
    silent FALSE NEGATIVE. Every reader now goes through
    ``atomic_write.read_bytes_with_retry``, which retries that bounded window.

    ``read_sharing_violation`` patches ``Path.read_bytes`` and so covers the
    uncapped readers (``read_secret`` / ``read_launcher`` via ``_read_sidecar``).
    The capped readers (``read_pid`` and the ``.start`` sidecar) pass
    ``max_bytes`` and so read through ``_read_bytes``; those are faulted by
    patching ``atomic_write._read_bytes``, the single chokepoint both branches of
    the helper call. POSIX permits the read, so this fault only reproduces on the
    simulator here and on a real Windows host -- it is skipped on this Linux
    worker's live filesystem, which is why the simulator exists. Not run on a
    real Windows host.
    """

    @pytest.fixture(autouse=True)
    def _no_backoff_sleep(self, _floor_monkeypatch):
        """Keep the bounded retry instant; attempt COUNT is what matters here.

        Only the simulator tests belong under this fixture. The real-file
        concurrency test lives in ``TestRunMarkerSidecarConcurrentRewrite``
        below, OUTSIDE it, because there the attempts must be spaced in time:
        with the sleep zeroed the whole budget fits inside one writer syscall.

        Patches through ``_floor_monkeypatch`` (the isolation floor's own
        ``MonkeyPatch``), not the shared ``monkeypatch``: an autouse fixture that
        used the shared instance would be lifted by any test here that called
        ``monkeypatch.undo()`` mid-body, silently restoring the real backoff
        sleep for the rest of that test (D11 / semgrep
        ``kirocrew.test-autouse-shared-monkeypatch``).
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

        Deterministic coverage of the budget's far edge, complementing the
        shorter-window simulator test above (two faults then a success). This one
        faults every one of the ``_REPLACE_MAX_ATTEMPTS``
        reads, so the reader runs out of attempts and ``read_launcher`` folds the
        final ``PermissionError`` to ``""`` -> ``None``.

        It takes no time (the autouse ``_no_backoff_sleep`` zeroes the backoff,
        so faulting the whole budget is instant), with no dependence on a live
        filesystem's sharing-violation timing. The budget
        is finite by design (a window that stays open forever MUST eventually be
        given up on), so a reader that meets a window outlasting the whole budget
        folds -- which is what this test pins. This and the shorter-window
        simulator test are the owners of the Windows sharing-violation fold;
        ``TestRunMarkerSidecarConcurrentRewrite`` is a separate real-file race
        that reads a sidecar a writer rewrites and asserts the torn-value
        invariant on every platform -- it permits the fold rather than
        reproducing it deterministically.
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

    Deliberately NOT a member of ``TestRunMarkerSidecarReadRetry``. That class's
    autouse fixture zeroes ``_REPLACE_BACKOFF_SECONDS`` because its simulator
    tests count attempts; here the real 50 ms backoff MUST stay in force, because
    it is the product behaviour under test. On Windows the writer's ``os.replace``
    (``MoveFileEx`` superseding the target) runs with the GIL released, and a read
    that opens the file inside that one syscall raises WinError 32; the 10 x 50 ms
    read-retry budget is what rides that single window out so the read still
    returns the value a writer published rather than folding to a false ``None``.

    So the handoff below is a strict three-way lock-step that bounds each read
    to AT MOST ONE completed replace. Each round: the reader releases the
    writer (``about_to_read``) and reads ``read_launcher`` concurrently, so the
    read overlaps that round's single in-flight rename; the writer performs
    exactly one ``atomic_write`` and acknowledges it (``published``); the reader
    asserts that acknowledgement arrived before advancing (``read_done`` then
    gates the writer out of the round). Every ``threading.Event`` is cleared by
    its consumer so a signal is always fresh and never coalesces across rounds.

    The writer's acknowledgement is what makes the race real rather than vacuous.
    Without it a reader could set and clear its own signals ``rounds`` times while
    the writer lagged or never ran, and every read would pass against the seeded
    value -- a green test that proves nothing. The reader instead asserts
    ``published`` each round, and the test asserts at the end that the writer
    completed exactly ``rounds`` replaces, so a writer that publishes nothing
    fails loudly.

    A read therefore overlaps at most one live replace (so a torn or empty value
    is catchable, which a settled-file read could never catch), and the
    contention window per read is bounded to a SINGLE replace, which fits inside
    the real retry budget -- unlike a back-to-back tight loop, which could chain
    several replace windows into one read and legitimately exhaust the budget.
    That bounded, acknowledged overlap is what lets the assertion stay strict:
    ``got in values`` (never ``None``).
    """

    def test_each_read_overlaps_one_acknowledged_replace_without_tearing(
        self, tmp_path, monkeypatch
    ):
        """The issue's own reproduction, on real files, with the real backoff.

        Each round is a strict three-way lock-step. The reader signals
        ``about_to_read`` and immediately calls ``read_launcher`` while the writer
        -- released by that same signal -- performs exactly ONE ``atomic_write``
        (mkstemp + ``os.replace``) of the next value and then sets ``published``.
        The reader asserts ``published`` arrived, clears it, and only then signals
        ``read_done`` to let the writer wait for the next round. So every read
        overlaps at most one live replace: the window a read can land in is a
        single rename, which fits inside the real 10 x 50 ms retry budget.

        The invariant asserted holds on EVERY platform and is strict:

            assert got in values

        A read that returns anything must return a value SOME writer published --
        never a torn or empty value (atomicity: ``os.replace`` is all-or-nothing,
        and the retry rides out the Windows sharing-violation window of the one
        replace it overlaps), and never ``None`` (a false absence). This is why
        the real backoff stays in force: a regression that shrank or removed the
        50 ms spacing would let the overlapped Windows read exhaust the budget and
        fold to ``None``, which this assertion would then catch -- whereas with
        the backoff zeroed (the simulator fixture) even a single replace window
        can outlast the whole budget, so zeroing it here would make the test
        unable to tell the product's spacing from its absence.

        The writer's acknowledgement is asserted so the race is never vacuous.
        The reader asserts ``published`` each round, and the end of the test
        asserts the writer completed exactly ``rounds`` replaces (``publishes ==
        rounds``). A writer that lags or never runs therefore fails the test
        instead of leaving ``got in values`` to pass against the seeded value.

        The launcher-marker sidecar stands in for the whole sidecar family here:
        the read-retry chokepoint (``read_bytes_with_retry``) is shared by every
        reader, so the overlap invariant is identical whichever sidecar is
        exercised. ``read_secret`` has its own deterministic coverage.

        The Windows sharing-violation FOLD (a window that outlasts the whole
        budget) is owned deterministically by the simulator tests in
        ``TestRunMarkerSidecarReadRetry``; an uncoordinated real-file race cannot
        pin that far edge on every platform, so it is not asserted here. This
        test owns the complementary real-file guarantee: a bounded single-replace
        overlap, acknowledged by the writer, is ridden out, never torn, never a
        false absence.

        The writer's failure is surfaced, not swallowed: a writer that dies would
        otherwise leave a stable file every later read passes against vacuously.
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

    This backs ``cli_server.resolve_client_port``'s zero-config fallback, so the
    contract under test is: filename-only parsing and no directory creation.
    Deciding whether a discovered port is *trustworthy* is deliberately NOT this
    module's job (a listener is not proof of identity) — see
    ``TestResolveClientPortRunMarker`` for the ownership gate.
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
        """The whole point of splitting the record out of the pid file.

        ``read_pid`` as shipped on ``origin/main`` is ``_read_sidecar(port,
        ".pid")`` -- the WHOLE file, stripped -- gated on ``isdigit()``. This
        asserts the body a live ``write_marker`` produces against exactly that
        reader, so a second line can never be reintroduced without a red test.
        An older client venv sharing this data home is the caller that breaks.
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
