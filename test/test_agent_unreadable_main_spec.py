"""Tests for a rebuild that cannot read an existing ``kirocrew.json``.

The rebuild keeps the user's spec and skips the cycle instead of writing defaults over
it, and the main agent and its mirrors are refused until a rebuild writes the spec
again: the skip and its sibling pass, the start gate and the launch bound, the early
retry with its simulated clock, the SEL denial on every refusal, and the readers that
report the skipped cycle.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import tempfile
import threading
import time
import warnings
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from agent_helpers import _pinned_kiro_cli_version  # noqa: F401
from agent_helpers import _run_install
from mcp_merge_helpers import bundled_defaults as _bundled_defaults
from unreadable_spec_helpers import custom_spec

from kiro_crew import atomic_write as aw


class _FakeTimers:
    """A simulated clock for ``agent._main_spec_retry_timer_factory``.

    ``start`` records a timer due at ``now + delay`` and starts no thread;
    ``fire_next`` advances ``now`` to the earliest due timer and runs it.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.delays: list[float] = []
        self._due: list[list] = []
        #: Every timer's callback and handle, in start order, so a test can run a
        #: callback whose thread was already past its check when it was cancelled.
        self.callbacks: list = []
        self.handles: list = []

    def start(self, delay: float, callback):
        entry = [self.now + delay, callback, False]
        self.delays.append(delay)
        self._due.append(entry)
        self.callbacks.append(callback)

        class _Handle:
            def cancel(self_inner) -> None:
                entry[2] = True

        handle = _Handle()
        self.handles.append(handle)
        return handle

    @property
    def pending(self) -> bool:
        return self.pending_count > 0

    @property
    def pending_count(self) -> int:
        return sum(1 for _due, _cb, cancelled in self._due if not cancelled)

    def fire_next(self) -> None:
        self.fired = getattr(self, "fired", 0) + 1
        assert self.fired <= 20, "the early retry never stops rescheduling"
        live = [e for e in self._due if not e[2]]
        entry = min(live, key=lambda e: e[0])
        self._due.remove(entry)
        self.now = entry[0]
        entry[1]()


class TestUnreadableMainSpec:
    """The unreadable-main-spec episode, end to end through ``install_agent``."""

    _custom_spec = staticmethod(custom_spec)

    @pytest.mark.parametrize(
        "error",
        [
            PermissionError(13, "Permission denied"),
            OSError(5, "Input/output error"),
        ],
        ids=["eacces", "eio"],
    )
    def test_unreadable_existing_spec_is_kept_and_the_cycle_skipped(
        self, tmp_path: Path, caplog, held_flag, error
    ):
        """A present spec whose read fails is left byte-for-byte, never reset.

        The read failure says nothing about the file's contents, so the rebuild
        writes neither the main spec nor the two specs mirrored from it. The
        steps that re-filter other files (service and conductor specs, forks,
        hook repair) still run. It reports the skip as an unwritten, held
        rebuild so every retry path (the ceiling memo, the boot baseline, the
        maintenance wake) runs again.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)
        before = spec.read_bytes()

        def _failing_read(path, **_kwargs):
            if Path(path) == spec:
                raise error
            return aw.read_bytes_with_retry(path, **_kwargs)

        wrote_out: list[bool] = []
        held_out: list[bool] = []
        with (
            patch("kiro_crew.agent.read_bytes_with_retry", _failing_read),
            patch("kiro_crew.agent.worker_agent._install_worker_agent") as worker,
            patch("kiro_crew.agent._install_heartbeat_agent") as heartbeat,
            patch("kiro_crew.agent.service_agents._install_knowledge_agent") as knowledge,
            patch(
                "kiro_crew.agent.conductor_agents._install_conductor_agent", return_value=True
            ) as conductor,
            patch("kiro_crew.agent.fork_refresh.refresh_after_rebuild") as forks,
            patch("kiro_crew.agent.repair_agent_configs") as repair,
            caplog.at_level(logging.WARNING, logger="kiro_crew.agent"),
        ):
            path = _run_install(
                tmp_path,
                cfg_dir,
                refresh_forks="defer",
                _wrote_out=wrote_out,
                _held_out=held_out,
            )

        assert path == spec
        assert spec.read_bytes() == before
        assert wrote_out == [False]
        assert held_out == [True]
        # The fork refresh is what the fork spawn gate waits on and records its
        # failures for, so skipping it would leave forks on stale grants behind
        # an open gate. It runs with the caller's own mode.
        assert forks.call_args.args[0] == "defer"
        assert held_flag._conductor_spec_held is True
        # The two specs mirrored from the main spec are left alone ...
        worker.assert_not_called()
        heartbeat.assert_not_called()
        # ... and every step that re-filters a different file still runs, so a
        # moved ceiling reaches the forks and conductor specs this cycle.
        knowledge.assert_called_once_with()
        conductor.assert_called_once()
        forks.assert_called_once()
        repair.assert_called_once_with()
        assert any(
            r.levelno == logging.WARNING and str(spec) in r.getMessage() for r in caplog.records
        )

    def test_a_readable_spec_after_a_skip_rebuilds_and_clears_the_hold(
        self, tmp_path: Path, held_flag
    ):
        """The next rebuild that can read the file writes it with the user's fields."""
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)

        def _denied(path, **_kwargs):
            if Path(path) == spec:
                raise PermissionError(13, "Permission denied", str(path))
            return aw.read_bytes_with_retry(path, **_kwargs)

        with patch("kiro_crew.agent.read_bytes_with_retry", _denied):
            _run_install(tmp_path, cfg_dir)
        assert held_flag._conductor_spec_held is True

        wrote_out: list[bool] = []
        _run_install(tmp_path, cfg_dir, _wrote_out=wrote_out)

        assert wrote_out == [True]
        assert held_flag._conductor_spec_held is False
        config = json.loads(spec.read_text(encoding="utf-8"))
        assert config["model"] == "claude-user-custom"
        assert "user-server" in config["mcpServers"]

    def test_a_sharing_violation_is_retried_before_the_spec_counts_as_unreadable(
        self, tmp_path: Path, monkeypatch, held_flag
    ):
        """One Windows sharing violation is waited out; the rebuild then reads and writes."""
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)
        monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)
        real_read = aw._read_bytes
        attempts = [0]

        def _locked_once(target: Path, max_bytes):
            if target == spec:
                attempts[0] += 1
                if attempts[0] == 1:
                    raise PermissionError(13, "The process cannot access the file", str(target))
            return real_read(target, max_bytes)

        def _windows_read(path, **kwargs):
            # The retry only sleeps over a sharing violation on Windows. Scoped
            # to this one read, so the rest of the rebuild keeps the host's paths.
            with patch.object(aw.platform_compat, "IS_WINDOWS", True):
                return aw.read_bytes_with_retry(path, **kwargs)

        wrote_out: list[bool] = []
        with (
            patch.object(aw, "_read_bytes", _locked_once),
            patch("kiro_crew.agent.read_bytes_with_retry", _windows_read),
        ):
            _run_install(tmp_path, cfg_dir, _wrote_out=wrote_out)

        # The first read failed and was retried; later reads of the spec in the
        # same rebuild (the load, the alias reconcile) succeed.
        assert attempts[0] >= 2
        assert wrote_out == [True]
        assert held_flag._conductor_spec_held is False
        config = json.loads(spec.read_text(encoding="utf-8"))
        assert config["model"] == "claude-user-custom"
        assert "user-server" in config["mcpServers"]

    def test_a_skipped_cycle_runs_no_migration_over_other_specs(self, tmp_path: Path, held_flag):
        """The probe runs before the self-heal passes, so a skip leaves other specs alone."""
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)
        sibling = spec.parent / "user-other.json"
        sibling.write_text(
            json.dumps({"name": "user-other", "model_managed": True}), encoding="utf-8"
        )
        before = sibling.read_bytes()

        def _denied(path, **kwargs):
            if Path(path) == spec:
                raise PermissionError(13, "Permission denied", str(path))
            return aw.read_bytes_with_retry(path, **kwargs)

        wrote_out: list[bool] = []
        with patch("kiro_crew.agent.read_bytes_with_retry", _denied):
            _run_install(tmp_path, cfg_dir, _wrote_out=wrote_out)

        assert wrote_out == [False]
        assert sibling.read_bytes() == before

    def _skipped(self, tmp_path: Path, held_flag):
        """Run one rebuild over a ``kirocrew.json`` whose read is denied: the gate is armed."""
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)

        def _denied(path, **kwargs):
            if Path(path) == spec:
                raise PermissionError(13, "Permission denied", str(path))
            return aw.read_bytes_with_retry(path, **kwargs)

        with patch("kiro_crew.agent.read_bytes_with_retry", _denied):
            _run_install(tmp_path, cfg_dir)
        assert held_flag._main_spec_unreadable is True
        return cfg_dir, spec, _denied

    def test_the_main_agent_is_refused_while_its_spec_stays_unreadable(
        self, tmp_path: Path, held_flag
    ):
        """Fail-closed: every governed start is refused, with the truthful remedy.

        The gate reads no file and runs no rebuild, so a refused start costs
        nothing on the spawn path. An agent resolving to a spec of its own is not
        this gate's to refuse. Mutation pin: dropping the refusal admits
        ``kirocrew`` and this goes red.
        """
        _cfg_dir, spec, _denied = self._skipped(tmp_path, held_flag)
        rebuild = MagicMock(return_value=(spec, False))
        reads = MagicMock(side_effect=_denied)
        with (
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent),
            patch("kiro_crew.agent.rebuild_agent_config_reporting", rebuild),
            patch("kiro_crew.agent.read_bytes_with_retry", reads),
        ):
            for name in ("kirocrew", "kirocrew-heartbeat", "kirocrew-worker"):
                with pytest.raises(held_flag.ForkGovernanceUnresolved) as info:
                    held_flag.require_main_spec_projected(name)
                text = str(info.value)
                assert "the last rebuild could not read" in text
                assert "Restore read access" in text
                assert "hourly maintenance retry" in text
                assert "--clean" not in text
            with pytest.raises(held_flag.ForkGovernanceUnresolved):
                held_flag.require_fork_governance("kirocrew")
            # A non-main agent on a spec of its own is unaffected.
            assert (spec.parent / "kirocrew-conductor.json").is_file()
            held_flag.require_main_spec_projected("kirocrew-conductor")
        rebuild.assert_not_called()
        reads.assert_not_called()

    def test_a_readable_spec_stays_refused_until_an_existing_rebuild_writes_it(
        self, tmp_path: Path, held_flag
    ):
        """Restoring access alone does not admit: the flag clears only on a rebuild's write.

        The rebuild here is the ordinary one the hourly retry, the config
        watcher and a restart all run. Once it has written the spec the start is
        admitted with no gate-side work.
        """
        cfg_dir, spec, _denied = self._skipped(tmp_path, held_flag)
        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent):
            # Readable again, not yet rebuilt.
            with pytest.raises(held_flag.ForkGovernanceUnresolved):
                held_flag.require_main_spec_projected("kirocrew")
            assert held_flag._main_spec_unreadable is True
            wrote: list[bool] = []
            _run_install(tmp_path, cfg_dir, _wrote_out=wrote)
            assert wrote == [True]
            assert held_flag._main_spec_unreadable is False
            held_flag.require_main_spec_projected("kirocrew")
            held_flag.require_fork_governance("kirocrew")
        assert json.loads(spec.read_text(encoding="utf-8"))["model"] == "claude-user-custom"

    @pytest.mark.parametrize("lock_ends_at", [1.0, 6.0, 19.0, 21.0, 79.0, 300.0])
    def test_a_lock_episode_that_ends_is_admitted_within_a_minute(
        self, tmp_path: Path, monkeypatch, held_flag, lock_ends_at
    ):
        """A concurrent writer's lock episode, on a simulated clock: ≤ 60 s to admit.

        The spec is held open the way a concurrent writer holds it on Windows: every
        read raises a sharing violation through the Windows retry branch until
        *lock_ends_at*. The first refused start schedules the existing held-spec
        retry early (5, 15 and 60 s apart, then every 60 s while the episode is
        open), and once the lock has ended the next retry rewrites the spec and
        starts are admitted, a 300 s lock included. No skip on any OS:
        the Windows branch is forced, so every CI shard, the Windows ones included,
        runs this measurement. Mutation pins: with no early retry the only retry
        is the hourly wake, nothing is pending, and the start is never admitted
        here; stopping after the third retry leaves the 300 s lock refused.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)
        monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)
        clock = _FakeTimers()
        monkeypatch.setattr(held_flag, "_main_spec_retry_timer_factory", clock.start)
        real_read = aw._read_bytes

        def _concurrent_writer(target: Path, max_bytes):
            if target == spec and clock.now < lock_ends_at:
                raise PermissionError(13, "The process cannot access the file", str(target))
            return real_read(target, max_bytes)

        def _windows_read(path, **kwargs):
            with patch.object(aw.platform_compat, "IS_WINDOWS", True):
                return aw.read_bytes_with_retry(path, **kwargs)

        def _rebuild(**kwargs):
            return _run_install(tmp_path, cfg_dir, **kwargs)

        with (
            patch.object(aw, "_read_bytes", _concurrent_writer),
            patch("kiro_crew.agent.read_bytes_with_retry", _windows_read),
            patch("kiro_crew.agent.rebuild_agent_config", _rebuild),
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent),
        ):
            _run_install(tmp_path, cfg_dir)
            assert held_flag._main_spec_unreadable is True
            with pytest.raises(held_flag.ForkGovernanceUnresolved):
                held_flag.require_main_spec_projected("kirocrew")
            admitted_at = None
            while clock.pending and admitted_at is None:
                clock.fire_next()
                try:
                    held_flag.require_main_spec_projected("kirocrew")
                except held_flag.ForkGovernanceUnresolved:
                    continue
                admitted_at = clock.now

        assert admitted_at is not None, "the episode ended but no retry admitted the start"
        # The retries fire at simulated 5, 20 and 80 s, then every 60 s: the first
        # one at or after the lock's end admits, and no lock end leaves more than
        # 60 s to it.
        fire_times = (5.0, 20.0, *(80.0 + 60.0 * k for k in range(10)))
        assert admitted_at == min(t for t in fire_times if t >= lock_ends_at)
        assert admitted_at - lock_ends_at <= 60.0  # flake-ok: simulated clock, not wall time
        assert json.loads(spec.read_text(encoding="utf-8"))["model"] == "claude-user-custom"

    @pytest.mark.parametrize("writer_lock", [True, False], ids=["same-lock", "lock-free"])
    def test_the_15515_harness_reports_refusals_and_episode_lengths(
        self, tmp_path: Path, monkeypatch, held_flag, writer_lock
    ):
        """The reported harness, unforced: real rebuilds against a real concurrent writer.

        A writer thread rewrites ``kirocrew.json`` unchanged by temp file plus
        ``os.replace``, in a loop, either under ``agents_spec_lock`` (the issue's
        harness, an unrelated dashboard sync) or with no lock (another process, an
        indexer). Twenty real rebuilds run against it with no read patched, so on a
        Windows host the read fails only when the OS refuses it, and the sharing
        violations, retries, skips and refusals counted are the host's own. Every
        rebuild is followed by a main-agent start through the gate. Measured and
        reported as a ``UserWarning`` in the run's warnings summary: refusals,
        episodes, and each episode's length in rebuilds and seconds. Asserted, on every OS: the user's server is never lost (the
        issue's acceptance criterion), a start is refused exactly while an episode
        is open, and once the writer stops one rebuild closes any open episode and
        the start is admitted. POSIX rename never refuses a reader, so there this
        is the no-violation control with zero episodes.
        """
        _HARNESS_REBUILDS = 20
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)
        monkeypatch.setattr(held_flag, "_main_spec_retry_timer_factory", _FakeTimers().start)
        _run_install(tmp_path, cfg_dir)
        assert held_flag._main_spec_unreadable is False

        stop = threading.Event()
        writes = [0]
        writer_errors = [0]

        def _rewrite_unchanged() -> None:
            data = spec.read_bytes()
            fd, tmp_name = tempfile.mkstemp(dir=spec.parent, suffix=".tmp")
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            try:
                os.replace(tmp_name, spec)
            except OSError:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
                raise

        def _writer() -> None:
            while not stop.is_set():
                try:
                    if writer_lock:
                        with held_flag.agents_spec_lock(spec.parent):
                            _rewrite_unchanged()
                    else:
                        _rewrite_unchanged()
                    writes[0] += 1
                except OSError:
                    writer_errors[0] += 1
                # Pacing, not a wait on anything: leave the rebuild the GIL and
                # the lock between writes, as the issue's writer did (~58 per
                # rebuild).
                stop.wait(0.005)  # flake-ok: pacing between writes; nothing waits on it

        def _start_refused() -> bool:
            try:
                held_flag.require_main_spec_projected("kirocrew")
            except held_flag.ForkGovernanceUnresolved:
                return True
            return False

        refusals = 0
        episodes: list[tuple[int, float]] = []
        opened: tuple[int, float] | None = None
        rebuild_errors = 0
        thread = threading.Thread(target=_writer, name="15515-writer", daemon=True)
        thread.start()
        try:
            with patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent):
                for index in range(_HARNESS_REBUILDS):
                    try:
                        _run_install(tmp_path, cfg_dir)
                    except OSError:
                        rebuild_errors += 1
                    unreadable = held_flag._main_spec_unreadable
                    refused = _start_refused()
                    assert refused is unreadable, "the gate disagrees with the episode flag"
                    refusals += refused
                    if unreadable and opened is None:
                        opened = (index, time.monotonic())
                    elif not unreadable and opened is not None:
                        episodes.append((index - opened[0], time.monotonic() - opened[1]))
                        opened = None
        finally:
            stop.set()
            thread.join(timeout=60)
        assert not thread.is_alive()

        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent):
            _run_install(tmp_path, cfg_dir)
            assert held_flag._main_spec_unreadable is False
            assert not _start_refused()
        if opened is not None:
            episodes.append((_HARNESS_REBUILDS - opened[0], time.monotonic() - opened[1]))
        kept = json.loads(spec.read_text(encoding="utf-8"))
        assert kept["mcpServers"]["user-server"]["command"] == "/opt/user-server"
        assert kept["model"] == "claude-user-custom"

        lengths = ", ".join(f"{count} rebuild(s) / {secs:.2f} s" for count, secs in episodes)
        report = (
            f"#15515 harness [{sys.platform}, {'same-lock' if writer_lock else 'lock-free'}"
            f" writer]: rebuilds={_HARNESS_REBUILDS} writer_writes={writes[0]} writer_errors={writer_errors[0]}"
            f" rebuild_errors={rebuild_errors} refused_starts={refusals}"
            f" episodes={len(episodes)} episode_lengths=[{lengths}]"
        )
        # Reported as a warning, never a failure: an xdist worker's stderr does
        # not reach the CI log, but its warnings are relayed into the warnings
        # summary every shard prints, so each OS shard's log carries its counts.
        # stacklevel=2 would point at pytest, not at this test.
        warnings.warn(report, UserWarning, stacklevel=1)

    def test_refusals_share_one_pending_retry_that_repeats_while_the_episode_is_open(
        self, tmp_path: Path, held_flag, monkeypatch
    ):
        """Debounced and steady: one timer at a time, 5 → 15 → 60 s, then 60 s on.

        A spec that stays unreadable costs one rebuild attempt a minute while the
        episode is open. The rebuild that writes the spec cancels the pending
        timer, so nothing is pending outside an episode, and the next episode
        starts its backoff from the beginning. Mutation pins: a timer per refusal,
        a retry that stops after the third, or a timer left pending once the
        episode closes, goes red.
        """
        cfg_dir, spec, _denied = self._skipped(tmp_path, held_flag)
        clock = _FakeTimers()
        monkeypatch.setattr(held_flag, "_main_spec_retry_timer_factory", clock.start)

        def _rebuild(**kwargs):
            return _run_install(tmp_path, cfg_dir, **kwargs)

        with (
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent),
            patch("kiro_crew.agent.rebuild_agent_config", _rebuild),
        ):
            with patch("kiro_crew.agent.read_bytes_with_retry", _denied):
                for _ in range(5):
                    with pytest.raises(held_flag.ForkGovernanceUnresolved):
                        held_flag.require_main_spec_projected("kirocrew")
                assert clock.delays == [5.0]
                for _ in range(6):
                    clock.fire_next()
                    assert clock.pending_count == 1
                    with pytest.raises(held_flag.ForkGovernanceUnresolved):
                        held_flag.require_main_spec_projected("kirocrew")
                assert clock.delays == [5.0, 15.0, 60.0, 60.0, 60.0, 60.0, 60.0]
                assert held_flag._main_spec_unreadable is True
            # Readable again: a rebuild outside the timer (the hourly wake's)
            # closes the episode and cancels the pending retry.
            assert held_flag.retry_held_conductor_specs() is True
            assert held_flag._main_spec_unreadable is False
            assert held_flag._main_spec_retry_attempts == 0
            assert held_flag._main_spec_retry_timer is None
            assert not clock.pending
            held_flag._main_spec_unreadable = True
            with pytest.raises(held_flag.ForkGovernanceUnresolved):
                held_flag.require_main_spec_projected("kirocrew")
        assert clock.delays[-1] == 5.0

    def test_a_skip_right_after_a_close_warns_for_its_own_episode(
        self, tmp_path: Path, held_flag, monkeypatch, caplog
    ):
        """A new episode never inherits the closed one's "already warned" state.

        The close clears that state in the critical section that clears the
        flag. Here a skip on another thread opens a new episode the moment the
        close releases the episode lock, with the same errno; its first warning
        must still be a WARNING. Mutation pin: clearing the warning state after
        the lock is released (or keeping it in the shared-home set) leaves the
        old key in place, and the new episode's opening warning drops to debug.
        """
        _cfg_dir, spec, _denied = self._skipped(tmp_path, held_flag)
        monkeypatch.setattr(held_flag, "_install_sibling_specs", lambda *a, **k: False)
        real_lock = held_flag._main_spec_episode_lock
        reopen = [False]
        cause = PermissionError(13, "Permission denied", str(spec))

        class _ReopenOnRelease:
            def __enter__(self):
                return real_lock.__enter__()

            def __exit__(self, *exc):
                real_lock.__exit__(*exc)
                if reopen[0]:
                    reopen[0] = False
                    unreadable = held_flag._AgentSpecUnreadable(f"PermissionError: {cause}")
                    unreadable.__cause__ = cause
                    held_flag._skip_unreadable_main_spec(
                        spec, unreadable, "defer", clean=False, wrote_out=None, held_out=None
                    )
                return False

        monkeypatch.setattr(held_flag, "_main_spec_episode_lock", _ReopenOnRelease())
        caplog.set_level(logging.DEBUG, logger="kiro_crew.agent")
        caplog.clear()
        reopen[0] = True
        held_flag._note_main_spec_readable(spec, "kirocrew", held_flag._main_spec_episode_revision)
        assert not reopen[0], "the close never released the episode lock"
        opening = [
            r.levelno
            for r in caplog.records
            if r.getMessage().startswith("cannot read the existing agent spec")
        ]
        assert opening == [logging.WARNING]
        assert held_flag._main_spec_unreadable is True

    def test_every_episode_warning_operation_holds_its_lock(
        self, tmp_path: Path, held_flag, monkeypatch
    ):
        """The episode's warning state changes only under the episode lock.

        The lock is replaced by one that records whether it is held, and the set
        by one that notes any operation made while it is not. The skip, a failed
        retry, a denial that cannot be audited and the close each touch the
        episode's state. Mutation pin: removing the episode lock around any of
        the four goes red.
        """

        class _Recording:
            def __init__(self, real) -> None:
                self.real = real
                self.held = False

            def __enter__(self):
                self.real.acquire()
                self.held = True
                return self

            def __exit__(self, *exc):
                self.held = False
                self.real.release()
                return False

        class _Guarded(set):
            def __init__(self, lock: _Recording) -> None:
                super().__init__()
                self.lock = lock
                self.ops: list[tuple[str, bool]] = []

            def _note(self, op: str) -> None:
                self.ops.append((op, self.lock.held))

            def __contains__(self, item) -> bool:
                self._note("in")
                return set.__contains__(self, item)

            def add(self, item) -> None:
                self._note("add")
                set.add(self, item)

            def clear(self) -> None:
                self._note("clear")
                set.clear(self)

        episode_lock = _Recording(threading.Lock())
        episode_set = _Guarded(episode_lock)
        monkeypatch.setattr(held_flag, "_main_spec_episode_lock", episode_lock)
        monkeypatch.setattr(held_flag, "_main_spec_warned", episode_set)
        cfg_dir, spec, _denied = self._skipped(tmp_path, held_flag)

        def _rebuild(**kwargs):
            return _run_install(tmp_path, cfg_dir, **kwargs)

        def _unauditable():
            raise OSError("security event log unavailable")

        with (
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent),
            patch("kiro_crew.agent.rebuild_agent_config", _rebuild),
        ):
            with patch("kiro_crew.agent.read_bytes_with_retry", _denied):
                assert held_flag.retry_held_conductor_specs() is False
            with patch("kiro_crew.agent.sel", _unauditable):
                held_flag._audit_main_spec_denial("kirocrew")
            assert set(episode_set) >= {"read:13", "retry", "denial-audit"}
            assert held_flag.retry_held_conductor_specs() is True
        assert not episode_set
        assert {op for op, _held in episode_set.ops} >= {"in", "add", "clear"}
        assert all(held for _op, held in episode_set.ops), episode_set.ops

    def test_a_late_timer_never_takes_the_slot_of_a_newer_episode(
        self, tmp_path: Path, held_flag, monkeypatch
    ):
        """A retry whose timer was cancelled too late finds a newer timer and does nothing.

        ``threading.Timer.cancel()`` cannot stop a callback already past its
        check. Here T1 is that callback: the episode closes (cancelling T1), a new
        episode's refusal arms T2, and only then does T1 run. It must not claim
        the slot, overwriting T2 and orphaning it beside its own successor, and it
        must not rebuild. Exactly T2 stays pending, and shutdown then leaves none.
        Mutation pin: claiming the slot without checking it still holds T1's own
        handle leaves two timers pending and one live after shutdown.
        """
        cfg_dir, spec, denied = self._skipped(tmp_path, held_flag)
        clock = _FakeTimers()
        monkeypatch.setattr(held_flag, "_main_spec_retry_timer_factory", clock.start)
        rebuilds: list[bool] = []

        def _record_retry() -> bool:
            rebuilds.append(True)
            return False

        with (
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent),
            patch("kiro_crew.agent.read_bytes_with_retry", denied),
        ):
            with pytest.raises(held_flag.ForkGovernanceUnresolved):
                held_flag.require_main_spec_projected("kirocrew")
            late_t1 = clock.callbacks[0]
            held_flag._note_main_spec_readable(
                spec, "kirocrew", held_flag._main_spec_episode_revision
            )
            _run_install(tmp_path, cfg_dir)
            with pytest.raises(held_flag.ForkGovernanceUnresolved):
                held_flag.require_main_spec_projected("kirocrew")
            assert clock.pending_count == 1
            with patch("kiro_crew.agent.retry_held_conductor_specs", _record_retry):
                late_t1()
        assert rebuilds == []
        assert clock.pending_count == 1
        assert held_flag._main_spec_retry_timer is clock.handles[1]
        held_flag.cancel_main_spec_early_retry()
        assert clock.pending_count == 0
        assert held_flag._main_spec_retry_timer is None

    def test_a_retry_that_still_cannot_read_warns_once_per_episode(
        self, tmp_path: Path, held_flag, monkeypatch, caplog
    ):
        """The minute-by-minute retry logs one WARNING an episode, then debug.

        Closing the episode clears the key, so the next episode warns again.
        Mutation pin: a plain ``logger.warning`` logs it on every retry.
        """
        cfg_dir, spec, _denied = self._skipped(tmp_path, held_flag)
        monkeypatch.setattr(held_flag, "_main_spec_retry_timer_factory", _FakeTimers().start)

        def _rebuild(**kwargs):
            return _run_install(tmp_path, cfg_dir, **kwargs)

        def _retry_warnings() -> int:
            return sum(
                "still cannot be read after the retry" in r.getMessage()
                for r in caplog.records
                if r.levelno == logging.WARNING
            )

        caplog.set_level(logging.DEBUG, logger="kiro_crew.agent")
        with (
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent),
            patch("kiro_crew.agent.rebuild_agent_config", _rebuild),
        ):
            with patch("kiro_crew.agent.read_bytes_with_retry", _denied):
                for _ in range(3):
                    assert held_flag.retry_held_conductor_specs() is False
            assert _retry_warnings() == 1
            assert held_flag.retry_held_conductor_specs() is True
            with patch("kiro_crew.agent.read_bytes_with_retry", _denied):
                _run_install(tmp_path, cfg_dir)
                assert held_flag.retry_held_conductor_specs() is False
        assert _retry_warnings() == 2

    def test_a_refusal_during_a_running_retry_arms_no_second_one(
        self, tmp_path: Path, held_flag, monkeypatch
    ):
        """While an early retry rebuilds, a refused start schedules nothing beside it.

        The run arms its own successor when it ends, so exactly one is pending.
        Mutation pin: clearing the pending timer before the rebuild lets the
        refusal arm a second, overlapping retry.
        """
        _cfg_dir, spec, _denied = self._skipped(tmp_path, held_flag)
        clock = _FakeTimers()
        monkeypatch.setattr(held_flag, "_main_spec_retry_timer_factory", clock.start)
        armed_during: list[int] = []

        def _retry_with_a_refusal() -> bool:
            with pytest.raises(held_flag.ForkGovernanceUnresolved):
                held_flag.require_main_spec_projected("kirocrew")
            armed_during.append(clock.pending_count)
            return False

        with (
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent),
            patch("kiro_crew.agent.read_bytes_with_retry", _denied),
        ):
            with pytest.raises(held_flag.ForkGovernanceUnresolved):
                held_flag.require_main_spec_projected("kirocrew")
            with patch("kiro_crew.agent.retry_held_conductor_specs", _retry_with_a_refusal):
                clock.fire_next()
        assert armed_during == [0]
        assert clock.pending_count == 1
        assert clock.delays == [5.0, 15.0]

    def test_shutdown_cancels_the_pending_retry_and_schedules_none(
        self, tmp_path: Path, held_flag, monkeypatch
    ):
        """Gateway shutdown leaves no retry pending, and a retry in flight arms no successor.

        The flag stays set: a shutdown admits nothing. Mutation pins: a cancel
        that leaves the timer, or a running retry that re-arms after it, is red.
        """
        _cfg_dir, spec, _denied = self._skipped(tmp_path, held_flag)
        clock = _FakeTimers()
        monkeypatch.setattr(held_flag, "_main_spec_retry_timer_factory", clock.start)
        with (
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent),
            patch("kiro_crew.agent.read_bytes_with_retry", _denied),
        ):
            with pytest.raises(held_flag.ForkGovernanceUnresolved):
                held_flag.require_main_spec_projected("kirocrew")
            assert clock.pending
            held_flag.cancel_main_spec_early_retry()
            assert not clock.pending
            assert held_flag._main_spec_retry_timer is None
            # A timer whose thread was past its check when shutdown cancelled it
            # rebuilds nothing during teardown and schedules nothing more.
            with patch(
                "kiro_crew.agent.retry_held_conductor_specs", return_value=False
            ) as late_retry:
                clock.callbacks[0]()
            late_retry.assert_not_called()
            with pytest.raises(held_flag.ForkGovernanceUnresolved):
                held_flag.require_main_spec_projected("kirocrew")
            assert not clock.pending
            # A retry already running when shutdown begins schedules no successor.
            held_flag.resume_main_spec_early_retry()
            with pytest.raises(held_flag.ForkGovernanceUnresolved):
                held_flag.require_main_spec_projected("kirocrew")

            def _shutdown_during_the_rebuild() -> bool:
                held_flag.cancel_main_spec_early_retry()
                return False

            with patch("kiro_crew.agent.retry_held_conductor_specs", _shutdown_during_the_rebuild):
                clock.fire_next()
        assert not clock.pending
        assert clock.delays == [5.0, 15.0]
        assert held_flag._main_spec_unreadable is True

    def test_a_restart_in_the_same_process_gets_early_retries_again(
        self, tmp_path: Path, held_flag, monkeypatch
    ):
        """The shutdown stop lasts until the next gateway start, not the process.

        Mutation pin: a resume that leaves the stop set schedules nothing here.
        """
        _cfg_dir, spec, _denied = self._skipped(tmp_path, held_flag)
        clock = _FakeTimers()
        monkeypatch.setattr(held_flag, "_main_spec_retry_timer_factory", clock.start)
        with (
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent),
            patch("kiro_crew.agent.read_bytes_with_retry", _denied),
        ):
            held_flag.cancel_main_spec_early_retry()
            with pytest.raises(held_flag.ForkGovernanceUnresolved):
                held_flag.require_main_spec_projected("kirocrew")
            assert not clock.pending
            held_flag.resume_main_spec_early_retry()
            with pytest.raises(held_flag.ForkGovernanceUnresolved):
                held_flag.require_main_spec_projected("kirocrew")
        assert clock.pending_count == 1
        assert held_flag._main_spec_retry_stopped is False

    def test_the_retry_and_the_baseline_read_an_open_episode_as_a_hold(
        self, tmp_path: Path, held_flag
    ):
        """An open episode is a hold on its own, whatever the conductor memo says.

        Mutation pins: a retry keyed on ``_conductor_spec_held`` alone returns
        without rebuilding and the start stays refused; a baseline keyed on it
        alone seeds a generation the unread spec never projected.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)
        held_flag._main_spec_unreadable = True
        held_flag._conductor_spec_held = False
        held_flag.prime_ceiling_projection()
        assert held_flag._projected_ceiling_generation is None

        def _rebuild(**kwargs):
            return _run_install(tmp_path, cfg_dir, **kwargs)

        with (
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent),
            patch("kiro_crew.agent.rebuild_agent_config", _rebuild),
        ):
            assert held_flag.retry_held_conductor_specs() is True
            held_flag.require_main_spec_projected("kirocrew")
        assert held_flag._main_spec_unreadable is False

    def test_an_older_write_never_closes_an_episode_a_newer_skip_opened(
        self, tmp_path: Path, held_flag, monkeypatch
    ):
        """GPT's race: a skip that lands while a rebuild writes is a newer episode.

        Rebuild A reads the spec and writes it. Between its write and the point it
        closes the episode, rebuild B's read fails and B skips (a profile tightening
        that reads the file A is replacing). A's write never read what B failed on, so
        A must leave the episode open with its retry; the retry's rebuild reads the
        recovered file and closes it. Mutation pin: closing unconditionally clears
        the newer episode, cancels its retry, and admits a start on a revoked grant.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)
        clock = _FakeTimers()
        monkeypatch.setattr(held_flag, "_main_spec_retry_timer_factory", clock.start)
        real_note = held_flag._note_main_spec_readable
        landed = [False]

        def _note_after_a_skip_lands(*args, **kwargs):
            # A has written the spec and is about to close the episode.
            if not landed[0]:
                landed[0] = True
                held_flag._skip_unreadable_main_spec(
                    spec,
                    held_flag._AgentSpecUnreadable("OSError: EIO"),
                    "defer",
                    clean=False,
                    wrote_out=None,
                    held_out=None,
                )
            return real_note(*args, **kwargs)

        wrote: list[bool] = []
        with patch("kiro_crew.agent._note_main_spec_readable", _note_after_a_skip_lands):
            _run_install(tmp_path, cfg_dir, _wrote_out=wrote)
        assert wrote == [True]
        assert held_flag._main_spec_unreadable is True, "an older write closed a newer episode"
        assert held_flag._spec_hold_open()

        with pytest.raises(held_flag.ForkGovernanceUnresolved):
            held_flag.require_main_spec_projected("kirocrew")
        assert clock.pending, "the newer episode lost its pending retry"

        def _rebuild(**kwargs):
            return _run_install(tmp_path, cfg_dir, **kwargs)

        with (
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent),
            patch("kiro_crew.agent.rebuild_agent_config", _rebuild),
        ):
            assert held_flag.retry_held_conductor_specs() is True
        assert held_flag._main_spec_unreadable is False
        held_flag.require_main_spec_projected("kirocrew")

    def test_an_overlapping_skip_leaves_no_episode_the_retry_cannot_close(
        self, tmp_path: Path, held_flag
    ):
        """The independent review's repro: a skip that lands inside a writing rebuild.

        Rebuild A writes ``kirocrew.json`` and clears the flag, and while its
        sibling pass runs, rebuild B's read fails and B skips. A then settles the
        conductor memo to its own verdict. That settle may not drop B's hold, so
        the hourly wake and the early retry still rebuild once the file reads.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)
        injected = [False]

        def _conductor_install(*_args, **_kwargs):
            if not injected[0]:
                injected[0] = True
                assert held_flag._main_spec_unreadable is False  # A already cleared it
                held_flag._skip_unreadable_main_spec(
                    spec,
                    held_flag._AgentSpecUnreadable("PermissionError: sharing violation"),
                    "defer",
                    clean=False,
                    wrote_out=None,
                    held_out=None,
                )
            return True  # A's own conductor write succeeded

        wrote: list[bool] = []
        with patch("kiro_crew.agent.conductor_agents._install_conductor_agent", _conductor_install):
            _run_install(tmp_path, cfg_dir, _wrote_out=wrote)
        assert wrote == [True]
        assert held_flag._main_spec_unreadable is True
        assert held_flag._conductor_spec_held is True

        def _rebuild(**kwargs):
            return _run_install(tmp_path, cfg_dir, **kwargs)

        with (
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent),
            patch("kiro_crew.agent.rebuild_agent_config", _rebuild),
        ):
            for _ in range(3):  # three timer firings or wakes
                held_flag.retry_held_conductor_specs()
            assert not (
                held_flag._main_spec_unreadable and not held_flag._conductor_spec_held
            ), "episode open but nothing held: retry_held_conductor_specs will never rebuild"
            held_flag.require_main_spec_projected("kirocrew")

    @pytest.mark.parametrize(
        "skip_lands",
        [
            "before_the_write",
            "after_the_write",
            "before_the_settle",
            "after_the_settle",
            "after_the_rebuild",
            "around_the_rebuild",
        ],
    )
    def test_overlapping_rebuilds_never_leave_an_episode_without_a_retry(
        self, tmp_path: Path, held_flag, monkeypatch, skip_lands
    ):
        """One writing rebuild and one skipping rebuild, overlapped in every order.

        *skip_lands* places the failed read's skip inside the writing rebuild,
        between each pair of its flag writes, after it, or the other way round:
        the whole writing rebuild inside the skip's own sibling pass. Whatever the
        order, an open episode keeps the hold, a refused start arms an early
        retry, and that retry closes the episode once the file reads. Mutation
        pin: a settle that drops the hold while the episode is open goes red.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)
        clock = _FakeTimers()
        monkeypatch.setattr(held_flag, "_main_spec_retry_timer_factory", clock.start)
        fired = [False]

        def _skip() -> None:
            held_flag._skip_unreadable_main_spec(
                spec,
                held_flag._AgentSpecUnreadable("PermissionError: sharing violation"),
                "defer",
                clean=False,
                wrote_out=None,
                held_out=None,
            )

        def _write() -> None:
            wrote: list[bool] = []
            _run_install(tmp_path, cfg_dir, _wrote_out=wrote)
            assert wrote == [True]

        def _at(point: str, real):
            def _step(*args, **kwargs):
                if skip_lands == point and not fired[0]:
                    fired[0] = True
                    _skip()
                return real(*args, **kwargs)

            return _step

        real_conductor = held_flag.conductor_agents._install_conductor_agent
        real_read = held_flag._read_existing_spec

        def _around(*args, **kwargs):
            # The skip's own sibling pass runs first here, and the whole writing
            # rebuild lands inside it, before the skip's last flag write.
            if skip_lands == "around_the_rebuild" and not fired[0]:
                fired[0] = True
                _write()
            return real_conductor(*args, **kwargs)

        failed_reads = [skip_lands == "around_the_rebuild"]

        def _read_once_failing(path):
            if failed_reads[0]:
                failed_reads[0] = False
                raise held_flag._AgentSpecUnreadable("PermissionError: sharing violation")
            return real_read(path)

        with (
            patch(
                "kiro_crew.agent.migrate_agent_specs",
                _at("before_the_write", held_flag.migrate_agent_specs),
            ),
            patch(
                "kiro_crew.agent._install_aim_capabilities",
                _at("after_the_write", held_flag._install_aim_capabilities),
            ),
            patch(
                "kiro_crew.agent.conductor_agents._install_conductor_agent",
                _at("before_the_settle", _around),
            ),
            patch(
                "kiro_crew.agent.repair_agent_configs",
                _at("after_the_settle", held_flag.repair_agent_configs),
            ),
            patch(
                "kiro_crew.agent._invalidate_projection_if_answer_moved",
                _at("after_the_rebuild", held_flag._invalidate_projection_if_answer_moved),
            ),
            patch("kiro_crew.agent._read_existing_spec", _read_once_failing),
        ):
            if skip_lands == "around_the_rebuild":
                # The skipping rebuild, whose sibling pass runs the writing one.
                skipped: list[bool] = []
                _run_install(tmp_path, cfg_dir, _wrote_out=skipped)
                assert skipped == [False]
            else:
                _write()
        assert fired[0], "the skip never landed"

        if held_flag._main_spec_unreadable:
            assert held_flag._conductor_spec_held is True, "an open episode lost its hold"

        def _rebuild(**kwargs):
            return _run_install(tmp_path, cfg_dir, **kwargs)

        with (
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent),
            patch("kiro_crew.agent.rebuild_agent_config", _rebuild),
        ):
            if held_flag._main_spec_unreadable:
                with pytest.raises(held_flag.ForkGovernanceUnresolved):
                    held_flag.require_main_spec_projected("kirocrew")
                assert clock.pending, "an open episode has no pending retry"
                clock.fire_next()
            assert held_flag.retry_held_conductor_specs() is True
            held_flag.require_main_spec_projected("kirocrew")
        assert held_flag._main_spec_unreadable is False
        assert held_flag._conductor_spec_held is False
        assert not clock.pending

    def test_every_refusal_writes_an_sel_denial(self, tmp_path: Path, held_flag):
        """The existing governance-denial event, once per refused start.

        Each refusal is a permission decision, so a retried start and a start
        of an agent already refused this episode are audited too. Mutation pins:
        no audit, or a per-agent or per-episode debounce, goes red.
        """
        cfg_dir, spec, _denied = self._skipped(tmp_path, held_flag)
        log = MagicMock()
        with (
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent),
            patch("kiro_crew.agent.sel", return_value=log),
        ):
            for _ in range(3):
                for name in ("kirocrew", "kirocrew-heartbeat"):
                    with pytest.raises(held_flag.ForkGovernanceUnresolved):
                        held_flag.require_main_spec_projected(name)
            calls = log.log_governance_decision.call_args_list
            assert [c.kwargs["agent"] for c in calls] == ["kirocrew", "kirocrew-heartbeat"] * 3
            for c in calls:
                assert c.kwargs["outcome"] == "denied"
                assert c.kwargs["reason"].startswith("cause=unreadable_main_spec")
            log.log.assert_not_called()
            # The episode closes, the next one audits too.
            _run_install(tmp_path, cfg_dir)
            held_flag._main_spec_unreadable = True
            with pytest.raises(held_flag.ForkGovernanceUnresolved):
                held_flag.require_main_spec_projected("kirocrew")
        assert log.log_governance_decision.call_count == 7

    def test_a_refusal_stands_when_the_sel_write_fails(self, tmp_path: Path, held_flag, caplog):
        """Audit failure never admits: each start is refused, the failure warned once per episode.

        Repeats drop to debug, the same form as the skip and the retry, so a
        persistent SEL failure stays visible. Mutation pin: dropping the debug
        arm leaves the repeat unlogged.
        """
        _cfg_dir, spec, _denied = self._skipped(tmp_path, held_flag)
        log = MagicMock()
        log.log_governance_decision.side_effect = OSError(28, "No space left on device")
        with (
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent),
            patch("kiro_crew.agent.sel", return_value=log),
            caplog.at_level(logging.DEBUG, logger="kiro_crew.agent"),
        ):
            with pytest.raises(held_flag.ForkGovernanceUnresolved):
                held_flag.require_main_spec_projected("kirocrew")
            with pytest.raises(held_flag.ForkGovernanceUnresolved):
                held_flag.require_fork_governance("kirocrew")
        assert log.log_governance_decision.call_count == 2
        levels = [
            r.levelno
            for r in caplog.records
            if "could not audit the refused start" in r.getMessage()
        ]
        assert levels == [logging.WARNING, logging.DEBUG]

    def test_an_audit_failure_after_a_close_leaves_no_key_for_the_next_episode(
        self, tmp_path: Path, held_flag, caplog
    ):
        """A close during the SEL write leaves the next episode's first audit failure a WARNING.

        The gate reads the flag without the episode lock, so a rebuild can close
        the episode while the denial is being written. If that write then fails,
        the failure is warned about but records no ``denial-audit`` key, so
        nothing outlives the closed episode. Mutation pin: recording the key
        whether or not an episode is open leaks it and silences the next
        episode's first warning.
        """
        cfg_dir, spec, denied = self._skipped(tmp_path, held_flag)

        def _closes_then_fails():
            held_flag._note_main_spec_readable(
                spec, "kirocrew", held_flag._main_spec_episode_revision
            )
            raise OSError("security event log unavailable")

        def _fails():
            raise OSError("security event log unavailable")

        with (
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent),
            caplog.at_level(logging.WARNING, logger="kiro_crew.agent"),
        ):
            with patch("kiro_crew.agent.sel", _closes_then_fails):
                held_flag._audit_main_spec_denial("kirocrew")
            assert held_flag._main_spec_unreadable is False
            assert "denial-audit" not in held_flag._main_spec_warned
            with patch("kiro_crew.agent.read_bytes_with_retry", denied):
                _run_install(tmp_path, cfg_dir)
            assert held_flag._main_spec_unreadable is True
            caplog.clear()
            with patch("kiro_crew.agent.sel", _fails):
                held_flag._audit_main_spec_denial("kirocrew")
        warned = [
            r for r in caplog.records if "could not audit the refused start" in r.getMessage()
        ]
        assert [r.levelno for r in warned] == [logging.WARNING]
        assert "denial-audit" in held_flag._main_spec_warned

    def test_repeat_skips_run_the_sibling_pass_once_per_governance_generation(
        self, tmp_path: Path, held_flag
    ):
        """The config watcher retries every tick: the sibling pass runs once per generation.

        It runs on the first skip of an episode, again only when
        ``governance_answer_generation`` moved, and again on the first skip of
        the next episode. Mutation pins: running it on every skip makes the tick
        count 5, and never re-running it misses the moved generation.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)
        denied = [True]
        generation = [10]

        def _failing(path, **kwargs):
            if Path(path) == spec and denied[0]:
                raise PermissionError(13, "Permission denied", str(path))
            return aw.read_bytes_with_retry(path, **kwargs)

        real_pass = held_flag._install_sibling_specs
        passes: list[bool] = []

        def _counting(*args, **kwargs):
            passes.append(kwargs.get("mirror_main", True))
            return real_pass(*args, **kwargs)

        with (
            patch("kiro_crew.agent.read_bytes_with_retry", _failing),
            patch("kiro_crew.agent._install_sibling_specs", _counting),
            patch(
                "kiro_crew.platform.governance_profiles.governance_answer_generation",
                lambda: generation[0],
            ),
        ):
            for _tick in range(5):
                wrote: list[bool] = []
                held: list[bool] = []
                _run_install(tmp_path, cfg_dir, _wrote_out=wrote, _held_out=held)
                # Every skip still reports itself as an unwritten, held rebuild.
                assert (wrote, held) == ([False], [True])
                assert held_flag._conductor_spec_held is True
            assert passes == [False]
            generation[0] = 11
            _run_install(tmp_path, cfg_dir)
            _run_install(tmp_path, cfg_dir)
            assert passes == [False, False]
            # Recovery writes the spec and runs the full pass; the next episode
            # starts over with its own first pass.
            denied[0] = False
            _run_install(tmp_path, cfg_dir)
            assert passes == [False, False, True]
            assert held_flag._main_spec_skip_generation is None
            denied[0] = True
            _run_install(tmp_path, cfg_dir)
            assert passes == [False, False, True, False]

    @pytest.mark.parametrize("failure", ["held", "raises"])
    def test_a_held_sibling_pass_is_retried_under_the_same_generation(
        self, tmp_path: Path, held_flag, failure
    ):
        """A conductor write that is held or raises leaves the generation unrecorded.

        The next skip, which the hourly retry runs, re-runs the pass under the same
        generation until one completes, so a tightened ceiling still reaches the
        conductor spec. Mutation pin: recording the generation before the pass
        lets the second skip run no pass and this goes red.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)

        def _denied(path, **kwargs):
            if Path(path) == spec:
                raise PermissionError(13, "Permission denied", str(path))
            return aw.read_bytes_with_retry(path, **kwargs)

        outcomes = [failure, failure, "ok"]
        installs: list[str] = []

        def _conductor(**_kwargs):
            outcome = outcomes[len(installs)] if len(installs) < len(outcomes) else "ok"
            installs.append(outcome)
            if outcome == "raises":
                raise OSError(13, "conductor write refused")
            return outcome == "ok"

        with (
            patch("kiro_crew.agent.read_bytes_with_retry", _denied),
            patch("kiro_crew.agent.conductor_agents._install_conductor_agent", _conductor),
            patch(
                "kiro_crew.platform.governance_profiles.governance_answer_generation",
                lambda: 42,
            ),
        ):
            for _ in range(5):
                _run_install(tmp_path, cfg_dir)
                assert held_flag._conductor_spec_held is True
        assert installs == [failure, failure, "ok"]
        assert held_flag._main_spec_skip_generation == 42

    def test_a_deferred_fork_refresh_leaves_the_generation_unrecorded(
        self, tmp_path: Path, held_flag, monkeypatch
    ):
        """Boot defers the fork refresh, so the skip cannot know how that pass ends.

        The deferred pass later holds a crew-bound shared template (its read was
        denied). Read access comes back while the main spec stays unreadable: the
        next skip, under the same generation, runs the pass again with the
        refresh in-line, and records the generation only once that pass holds
        nothing. Mutation pin: recording the generation on the deferred pass
        leaves ``refreshes`` at ``["defer"]``.
        """
        from kiro_crew.agent_materialization import fork_refresh

        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)

        def _denied(path, **kwargs):
            if Path(path) == spec:
                raise PermissionError(13, "Permission denied", str(path))
            return aw.read_bytes_with_retry(path, **kwargs)

        refreshes: list[object] = []
        template_readable = [False]

        def _refresh(refresh_forks, gated_off):
            refreshes.append(refresh_forks)
            # The deferred pass has not run when the rebuild returns; an in-line
            # one settles the hold before it returns, as the real one does.
            if refresh_forks is True:
                fork_refresh._shared_template_held = not template_readable[0]

        monkeypatch.setattr(fork_refresh, "_shared_template_held", False)
        with (
            patch("kiro_crew.agent.read_bytes_with_retry", _denied),
            patch("kiro_crew.agent.fork_refresh.refresh_after_rebuild", _refresh),
            patch(
                "kiro_crew.platform.governance_profiles.governance_answer_generation",
                lambda: 42,
            ),
        ):
            _run_install(tmp_path, cfg_dir, refresh_forks="defer")
            assert held_flag._main_spec_skip_generation is None
            # The deferred pass, settling after the boot rebuild returned.
            fork_refresh._shared_template_held = True
            _run_install(tmp_path, cfg_dir)
            assert held_flag._main_spec_skip_generation is None
            template_readable[0] = True
            _run_install(tmp_path, cfg_dir)
            assert held_flag._main_spec_skip_generation == 42
            _run_install(tmp_path, cfg_dir)
        assert refreshes == ["defer", True, True]

    def test_a_sibling_step_that_raises_keeps_the_hold(self, tmp_path: Path, held_flag):
        """The conductors settle the memo to False, then a later step raises: still held."""
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)

        def _denied(path, **kwargs):
            if Path(path) == spec:
                raise PermissionError(13, "Permission denied", str(path))
            return aw.read_bytes_with_retry(path, **kwargs)

        with (
            patch("kiro_crew.agent.read_bytes_with_retry", _denied),
            patch(
                "kiro_crew.agent.repair_agent_configs",
                side_effect=PermissionError(13, "hook repair"),
            ),
            pytest.raises(PermissionError, match="hook repair"),
        ):
            _run_install(tmp_path, cfg_dir)

        assert held_flag._conductor_spec_held is True

    def test_the_warning_is_once_per_episode_and_recovery_logs_once(
        self, tmp_path: Path, caplog, held_flag
    ):
        """A retry every tick warns once; a new cause warns again; recovery is one INFO."""
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)
        error = [PermissionError(13, "Permission denied", str(spec))]

        def _failing(path, **kwargs):
            if Path(path) == spec and error:
                raise error[0]
            return aw.read_bytes_with_retry(path, **kwargs)

        def _messages(text: str, level: int) -> int:
            return sum(1 for r in caplog.records if r.levelno == level and text in r.getMessage())

        with (
            patch("kiro_crew.agent.read_bytes_with_retry", _failing),
            caplog.at_level(logging.INFO, logger="kiro_crew.agent"),
        ):
            for _ in range(3):
                _run_install(tmp_path, cfg_dir)
            assert _messages("cannot read the existing agent spec", logging.WARNING) == 1
            error[0] = OSError(5, "Input/output error", str(spec))
            _run_install(tmp_path, cfg_dir)
            _run_install(tmp_path, cfg_dir)
            assert _messages("cannot read the existing agent spec", logging.WARNING) == 2
            error.clear()
            _run_install(tmp_path, cfg_dir)
            _run_install(tmp_path, cfg_dir)
        assert _messages("reads again and was rebuilt", logging.INFO) == 1
        assert held_flag._conductor_spec_held is False

    def test_an_unreadable_spec_inside_the_write_lock_skips_the_cycle(
        self, tmp_path: Path, held_flag
    ):
        """The commit's own re-read (the alias reconcile) takes the same exit.

        The load read succeeded, then the spec stopped being readable before the
        write: nothing is written over it and the skip is reported as held.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)
        before = spec.read_bytes()
        from kiro_crew.agent_materialization import mcp_aliases

        def _denied(path, **kwargs):
            if Path(path) == spec:
                raise PermissionError(13, "Permission denied", str(path))
            return aw.read_bytes_with_retry(path, **kwargs)

        def _unreadable_in_the_lock(path):
            try:
                _denied(path)
            except PermissionError as exc:
                return (True, mcp_aliases._UnreadableSpec(exc))
            raise AssertionError("the stub must refuse the spec")

        wrote_out: list[bool] = []
        held_out: list[bool] = []
        with (
            patch.object(mcp_aliases, "_durable_tool_aliases", _unreadable_in_the_lock),
            patch("kiro_crew.agent.worker_agent._install_worker_agent") as worker,
        ):
            _run_install(tmp_path, cfg_dir, _wrote_out=wrote_out, _held_out=held_out)

        assert spec.read_bytes() == before
        assert (wrote_out, held_out) == ([False], [True])
        assert held_flag._conductor_spec_held is True
        worker.assert_not_called()

    def test_the_sibling_pass_after_the_lock_never_exposes_a_half_written_spec(
        self, tmp_path: Path, held_flag
    ):
        """The skip's sibling writes run outside the agent-spec lock, and stay whole.

        A concurrent start reads a sibling spec without that lock, so what keeps
        it from a torn file is that every sibling write is a complete temp file
        swapped in by ``os.replace``, the same path the healthy rebuild's
        sibling pass takes after the lock (each installer takes the lock for
        its own write). Every spec the pass changes must be the target of such a
        replace, of a temp file that already parses. Mutation pin: a sibling
        installer writing its spec in place goes red.
        """
        import kiro_crew.agent as agent_mod
        from kiro_crew.agent_materialization import mcp_aliases

        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)
        kiro_dir = spec.parent

        def _unreadable_in_the_lock(path):
            return (True, mcp_aliases._UnreadableSpec(PermissionError(13, "Permission denied")))

        replaced: set[str] = set()
        real_replace = agent_mod.replace_with_retry

        def _checked_replace(src, dst, *args, **kwargs):
            # Complete before it becomes visible: a reader sees the old file or this one.
            json.loads(Path(src).read_text(encoding="utf-8"))
            replaced.add(Path(dst).name)
            return real_replace(src, dst, *args, **kwargs)

        before = {p.name: p.read_bytes() for p in kiro_dir.glob("*.json")}
        with (
            patch.object(mcp_aliases, "_durable_tool_aliases", _unreadable_in_the_lock),
            patch.object(agent_mod, "replace_with_retry", _checked_replace),
        ):
            _run_install(tmp_path, cfg_dir)

        assert held_flag._main_spec_unreadable is True
        changed = {p.name for p in kiro_dir.glob("*.json") if before.get(p.name) != p.read_bytes()}
        assert "kirocrew.json" not in changed
        assert changed, "the sibling pass wrote no spec, so this proves nothing"
        assert changed <= replaced, changed - replaced

    def test_a_clean_rebuild_still_resets_an_unreadable_spec(self, tmp_path: Path, held_flag):
        """``--clean`` is the documented reset, so it writes defaults over the file."""
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)

        def _denied(path, **kwargs):
            if Path(path) == spec:
                raise PermissionError(13, "Permission denied", str(path))
            return aw.read_bytes_with_retry(path, **kwargs)

        wrote_out: list[bool] = []
        with patch("kiro_crew.agent.read_bytes_with_retry", _denied):
            _run_install(tmp_path, cfg_dir, clean=True, _wrote_out=wrote_out)

        assert wrote_out == [True]
        assert json.loads(spec.read_text(encoding="utf-8"))["model"] == "claude-default"

    def test_the_durable_alias_reader_reports_an_unreadable_spec_as_present(self, tmp_path: Path):
        """A failed read is not "no spec": only ``FileNotFoundError`` is absent."""
        import kiro_crew.agent as agent_mod
        from kiro_crew.agent_materialization import mcp_aliases

        spec = self._custom_spec(tmp_path)

        def _denied(path, **kwargs):
            raise PermissionError(13, "Permission denied", str(path))

        with patch("kiro_crew.agent.read_bytes_with_retry", _denied):
            existed, aliases = mcp_aliases._durable_tool_aliases(spec)
            assert existed is True
            with pytest.raises(agent_mod._AgentSpecUnreadable):
                mcp_aliases._reconcile_tool_aliases_from_disk(spec, {})
        assert mcp_aliases._durable_tool_aliases(tmp_path / "absent.json") == (False, None)

    def test_invalid_utf8_still_rebuilds_from_defaults(self, tmp_path: Path):
        """A file that READ but does not decode is damaged, not unreadable."""
        import kiro_crew.agent as agent_mod

        spec = tmp_path / "kirocrew.json"
        spec.write_bytes(b"\xff\xfe\x00not utf-8")
        with patch("kiro_crew.agent.build_agent_config", return_value={"model": "d"}) as b:
            config, fresh = agent_mod._load_existing_config(spec)
        assert (config, fresh) == ({"model": "d"}, True)
        b.assert_called_once()

    def test_invalid_utf8_is_rewritten_by_a_whole_rebuild(self, tmp_path: Path):
        """The durable alias read decodes inside its ``try``, so the rebuild completes.

        Invalid UTF-8 is a ``ValueError`` there, read as no usable alias map, the
        same as invalid JSON, and the write that replaces the file goes ahead.
        """
        from kiro_crew.agent_materialization import mcp_aliases

        cfg_dir = _bundled_defaults(tmp_path)
        spec = tmp_path / "kiro_agents" / "kirocrew.json"
        spec.parent.mkdir(exist_ok=True)
        spec.write_bytes(b"\xff\xfe\x00not utf-8")
        assert mcp_aliases._durable_tool_aliases(spec) == (True, None)
        wrote: list[bool] = []
        _run_install(tmp_path, cfg_dir, _wrote_out=wrote)
        assert wrote == [True]
        assert isinstance(json.loads(spec.read_text(encoding="utf-8")), dict)

    def test_a_spec_that_vanishes_before_the_read_rebuilds_from_defaults(self, tmp_path: Path):
        """``FileNotFoundError`` is a missing file, not an unreadable one."""
        import kiro_crew.agent as agent_mod

        with patch("kiro_crew.agent.build_agent_config", return_value={"model": "d"}) as b:
            config, fresh = agent_mod._load_existing_config(tmp_path / "absent.json")
        assert (config, fresh) == ({"model": "d"}, True)
        b.assert_called_once()

    def test_with_no_factory_installed_the_retry_starts_its_own_timer(self, held_flag, monkeypatch):
        """Production installs no factory, so the scheduler starts the retry's timer itself.

        Mutation pin: dropping the fallback leaves an open episode with no early retry.
        """
        started: list[float] = []

        class _Handle:
            def cancel(self) -> None:
                pass

        def _start(delay, _callback):
            started.append(delay)
            return _Handle()

        monkeypatch.setattr(held_flag, "_main_spec_retry_timer_factory", None)
        monkeypatch.setattr(held_flag, "_start_main_spec_retry_timer", _start)
        monkeypatch.setattr(held_flag, "_main_spec_unreadable", True)
        held_flag._schedule_main_spec_early_retry()
        assert started == [5.0]


def test_a_dashboard_rebuild_that_did_not_write_names_both_causes(monkeypatch, caplog):
    """``wrote=False`` has two causes, and the dashboard's warning names both.

    The shared-home guard refusing the write, and an existing spec that could
    not be read and was kept unchanged. Mutation pin: the warning naming only
    the shared agent home misleads an operator whose spec is unreadable.
    """
    from kiro_crew.dashboard.handlers import _shared

    monkeypatch.setattr(
        "kiro_crew.agent.rebuild_agent_config_reporting", lambda: (Path("kirocrew.json"), False)
    )
    with caplog.at_level(logging.WARNING, logger=_shared.logger.name):
        assert _shared.rebuild_agent_spec("an MCP change") is False
    [warning] = [r.getMessage() for r in caplog.records if "not rewritten" in r.getMessage()]
    assert "shared agent home" in warning
    assert "could not be read and was kept unchanged" in warning
