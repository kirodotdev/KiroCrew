"""Tests for kiro_crew.sandbox — launcher-interpreter ENOENT spawn retry.

``sys.executable`` is often a symlink into a managed install tree, and rebuilding
that tree deletes and re-creates its entries, including the interpreter
``wrap_argv`` prepends to every sandboxed argv. A spawn landing in that ~1s
window used to die with a bare ENOENT that the caller could not distinguish from
a broken install.

These tests pin BOTH directions: the transient shape is retried, and every
other ENOENT shape still fails on the first attempt. They also pin the
``abort_retry`` contract on ``popen_limited`` -- the ONLY wrapper carrying that
hook, because it is the only one with consumers -- because its backoff is
otherwise a window in which a cancellation is lost rather than delayed.

The retry lives INLINE in each of the three wrappers rather than in a shared
helper, because both spawn audits key on ``<relpath>::<enclosing function>`` --
so these tests drive the wrappers themselves and would fail if any spawn were
extracted.
"""

from __future__ import annotations

import ast
import importlib
import pathlib
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

import kiro_crew.sandbox as sandbox_mod
from kiro_crew import platform_compat as pc
from kiro_crew.sandbox import _INTERPRETER_ENOENT_DELAYS, _is_transient_interpreter_enoent
from kiro_crew.subprocess_utf8 import UTF8_TEXT

#: The argv ``_prepare_limited_spawn`` is stubbed to return: a launcher argv
#: whose argv[0] is this process's own interpreter, i.e. the shape wrap_argv
#: produces and the only shape the retry accepts.
_LAUNCHER_CMD = [sys.executable, "-I", "/tmp/launcher.py"]


def _enoent(filename: str | None) -> FileNotFoundError:
    """A FileNotFoundError shaped like the one Popen raises."""
    exc = FileNotFoundError(2, "No such file or directory")
    exc.filename = filename
    return exc


def _prepared(cmd: list[str] | None = None):
    """Patch ``_prepare_limited_spawn`` so tests reach the spawn deterministically."""
    return patch.object(
        sandbox_mod,
        "_prepare_limited_spawn",
        return_value=(list(cmd if cmd is not None else _LAUNCHER_CMD), None),
    )


class TestIsTransientInterpreterEnoent:
    def test_enoent_naming_our_own_interpreter_is_transient(self):
        assert _is_transient_interpreter_enoent(_enoent(sys.executable), _LAUNCHER_CMD)

    def test_enoent_naming_a_different_path_is_not_transient(self):
        """A missing cwd names the DIRECTORY, not argv[0] — must not retry."""
        assert not _is_transient_interpreter_enoent(_enoent("/nonexistent/workdir"), _LAUNCHER_CMD)

    def test_enoent_for_a_user_binary_is_not_transient(self):
        """argv[0] is not our interpreter, so this is a real missing program."""
        assert not _is_transient_interpreter_enoent(
            _enoent("/usr/bin/definitely-not-installed"),
            ["/usr/bin/definitely-not-installed", "--version"],
        )

    def test_empty_cmd_is_not_transient(self):
        assert not _is_transient_interpreter_enoent(_enoent(None), [])

    def test_missing_filename_falls_back_to_a_live_existence_check(self):
        """With no filename, decide by observation — not by assumption."""
        # The interpreter running this test plainly exists, so an ENOENT that
        # names nothing is NOT attributable to a farm rebuild.
        assert not _is_transient_interpreter_enoent(_enoent(None), _LAUNCHER_CMD)
        with patch.object(sandbox_mod.os.path, "exists", return_value=False):
            assert _is_transient_interpreter_enoent(_enoent(None), _LAUNCHER_CMD)


class TestPopenLimitedToleratesAnAbsentInterpreter:
    def test_retries_until_the_farm_comes_back(self):
        sentinel = MagicMock(name="Popen")
        # Absent for the first two attempts, then the farm is whole again.
        attempts = [_enoent(sys.executable), _enoent(sys.executable), sentinel]

        def fake_popen(*_args, **_kwargs):
            outcome = attempts.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        slept: list[float] = []
        with (
            _prepared(),
            patch.object(sandbox_mod.subprocess, "Popen", side_effect=fake_popen),
            patch.object(sandbox_mod.time, "sleep", side_effect=slept.append),
        ):
            got = sandbox_mod.popen_limited(["/bin/echo", "hi"])

        assert got is sentinel
        # Proves the retry path ran rather than the first attempt succeeding.
        assert slept == list(_INTERPRETER_ENOENT_DELAYS[:2])
        # The caller still sees its OWN argv, not the launcher's.
        assert got.args == ["/bin/echo", "hi"]

    def test_a_permanently_absent_interpreter_still_raises(self):
        """Negative control: exhausting the budget re-raises, unchanged."""
        final = _enoent(sys.executable)
        raised = [_enoent(sys.executable)] * len(_INTERPRETER_ENOENT_DELAYS) + [final]

        with (
            _prepared(),
            patch.object(sandbox_mod.subprocess, "Popen", side_effect=raised) as popen,
            patch.object(sandbox_mod.time, "sleep"),
        ):
            with pytest.raises(FileNotFoundError) as caught:
                sandbox_mod.popen_limited(["/bin/echo", "hi"])

        assert caught.value is final
        assert popen.call_count == len(_INTERPRETER_ENOENT_DELAYS) + 1

    def test_a_non_transient_enoent_is_not_retried(self):
        """A missing cwd must fail on attempt ONE, with no delay."""
        with (
            _prepared(),
            patch.object(
                sandbox_mod.subprocess,
                "Popen",
                side_effect=_enoent("/nonexistent/workdir"),
            ) as popen,
            patch.object(sandbox_mod.time, "sleep") as sleep,
        ):
            with pytest.raises(FileNotFoundError):
                sandbox_mod.popen_limited(["/bin/echo", "hi"])

        assert popen.call_count == 1
        sleep.assert_not_called()

    def test_a_non_enoent_oserror_is_not_retried(self):
        with (
            _prepared(),
            patch.object(
                sandbox_mod.subprocess, "Popen", side_effect=PermissionError(13, "nope")
            ) as popen,
            patch.object(sandbox_mod.time, "sleep") as sleep,
        ):
            with pytest.raises(PermissionError):
                sandbox_mod.popen_limited(["/bin/echo", "hi"])

        assert popen.call_count == 1
        sleep.assert_not_called()

    def test_a_user_binary_enoent_is_not_retried(self):
        """argv[0] not being our interpreter must fail immediately."""
        missing = "/usr/bin/definitely-not-installed"
        with (
            _prepared([missing, "--version"]),
            patch.object(sandbox_mod.subprocess, "Popen", side_effect=_enoent(missing)) as popen,
            patch.object(sandbox_mod.time, "sleep") as sleep,
        ):
            with pytest.raises(FileNotFoundError):
                sandbox_mod.popen_limited([missing, "--version"])

        assert popen.call_count == 1
        sleep.assert_not_called()


class TestTheSpawnStaysInsidePopenLimited:
    """The spawn audits key on the ENCLOSING FUNCTION, so its location is load-bearing.

    ``_SYNC_ALLOWED`` and ``BENIGN_SPAWNS`` both name ``sandbox.py::popen_limited``.
    Extracting this ``Popen`` into a helper migrates its audit key: the entries go
    stale and the relocated call reads as a new unrouted, preexec_fn-forking spawn.
    """

    def test_popen_limited_itself_contains_the_spawn_call(self):
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(sandbox_mod.popen_limited)))
        spawns = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("subprocess.Popen")
        ]
        assert spawns, (
            "popen_limited no longer contains its own subprocess.Popen call — the "
            "spawn audits key on the enclosing function, so extracting it strands "
            "the sandbox.py::popen_limited entries in _SYNC_ALLOWED and "
            "BENIGN_SPAWNS and reports the relocated call as a new spawn."
        )
        assert all(
            any(kw.arg == "preexec_fn" for kw in node.keywords) for node in spawns
        ), "every spawn in popen_limited must still carry preexec_fn"


class TestAbortRetryClosesTheCancellationWindow:
    """The backoff must not become a window where a cancellation is LOST.

    ``kill_running_process`` keys on a REGISTERED child. During the backoff none
    exists, so without a recorded intent the cancel is discarded and the retry
    runs the cancelled work while the run still reports success.
    """

    def test_abort_retry_stops_the_spawn_instead_of_launching_it(self):
        calls: list[int] = []

        def fake_popen(*_args, **_kwargs):
            calls.append(1)
            raise _enoent(sys.executable)

        with (
            _prepared(),
            patch.object(sandbox_mod.subprocess, "Popen", side_effect=fake_popen),
            patch.object(sandbox_mod.time, "sleep"),
        ):
            with pytest.raises(FileNotFoundError):
                sandbox_mod.popen_limited(["/bin/echo", "hi"], abort_retry=lambda: True)

        # Exactly ONE attempt: the abort is consulted after the first backoff, so
        # no further spawn is attempted and nothing is launched.
        assert calls == [1]

    def test_without_abort_retry_the_budget_is_still_spent(self):
        """Negative control: the abort is what shortens it, not the ENOENT."""
        attempts = [_enoent(sys.executable)] * len(_INTERPRETER_ENOENT_DELAYS)
        attempts.append(MagicMock(name="Popen"))
        with (
            _prepared(),
            patch.object(sandbox_mod.subprocess, "Popen", side_effect=attempts),
            patch.object(sandbox_mod.time, "sleep"),
        ):
            sandbox_mod.popen_limited(["/bin/echo", "hi"])

    def test_a_false_abort_retry_does_not_stop_the_retry(self):
        sentinel = MagicMock(name="Popen")
        attempts = [_enoent(sys.executable), sentinel]
        with (
            _prepared(),
            patch.object(sandbox_mod.subprocess, "Popen", side_effect=attempts),
            patch.object(sandbox_mod.time, "sleep"),
        ):
            got = sandbox_mod.popen_limited(["/bin/echo", "hi"], abort_retry=lambda: False)
        assert got is sentinel


class TestSpawnToRegisteredIsAtomic:
    """A cancel must never fall between "spawning" and "registered".

    ``_finish_spawn`` performs both mutations under one hold of ``_PROCS_LOCK``,
    so ``kill_running_process`` cannot observe a job that is neither. The earlier
    two-call shape (clear the spawning mark, then register) left that gap.
    """

    def _clear(self, cron):
        with cron._PROCS_LOCK:
            cron._SPAWNING_JOBS.clear()
            cron._CANCELLED_PROC_JOBS.clear()
            cron._RUNNING_PROCS.clear()

    def test_finish_spawn_registers_and_clears_the_spawning_mark(self):
        cron = importlib.import_module("kiro_crew.cron_script")
        self._clear(cron)
        proc = MagicMock(name="Popen")
        cron._begin_spawn("job-a")
        assert cron._finish_spawn("job-a", proc) is False
        with cron._PROCS_LOCK:
            assert "job-a" not in cron._SPAWNING_JOBS
            assert cron._RUNNING_PROCS["job-a"] is proc
        self._clear(cron)

    def test_a_cancel_during_the_spawn_is_reported_and_the_child_not_registered(self):
        """The raced-cancel case: caller must kill, so we must NOT register."""
        cron = importlib.import_module("kiro_crew.cron_script")
        self._clear(cron)
        proc = MagicMock(name="Popen")
        cron._begin_spawn("job-b")
        with cron._PROCS_LOCK:
            cron._CANCELLED_PROC_JOBS.add("job-b")
        assert cron._finish_spawn("job-b", proc) is True
        with cron._PROCS_LOCK:
            assert "job-b" not in cron._RUNNING_PROCS
            # Consumed, so a later run of the same job is not told it was cancelled.
            assert "job-b" not in cron._CANCELLED_PROC_JOBS
            assert "job-b" not in cron._SPAWNING_JOBS
        self._clear(cron)

    def test_kill_during_the_spawn_window_records_the_cancel(self):
        cron = importlib.import_module("kiro_crew.cron_script")
        self._clear(cron)
        cron._begin_spawn("job-c")
        # No registered child, but a spawn is in flight: the cancel must stick.
        assert cron.kill_running_process("job-c") is True
        assert cron._spawn_cancelled("job-c") is True
        self._clear(cron)

    def test_kill_with_no_spawn_and_no_child_still_records_nothing(self):
        """Negative control: the window is what makes it recordable."""
        cron = importlib.import_module("kiro_crew.cron_script")
        self._clear(cron)
        assert cron.kill_running_process("job-d") is False
        assert cron._spawn_cancelled("job-d") is False
        self._clear(cron)

    def test_abandon_spawn_clears_the_flag_so_it_cannot_leak(self):
        cron = importlib.import_module("kiro_crew.cron_script")
        self._clear(cron)
        cron._begin_spawn("job-e")
        with cron._PROCS_LOCK:
            cron._CANCELLED_PROC_JOBS.add("job-e")
        assert cron._abandon_spawn("job-e") is True
        with cron._PROCS_LOCK:
            assert "job-e" not in cron._CANCELLED_PROC_JOBS
            assert "job-e" not in cron._SPAWNING_JOBS
        # Second call sees nothing left to report.
        assert cron._abandon_spawn("job-e") is False
        self._clear(cron)


class TestCommandCronSpawnFailureIsCancellable:
    """The spawn-failure branch must CONSUME a recorded cancel, not drop it.

    Exercises ``run_command_sandboxed``'s ``_abandon_spawn`` arm end to end: no
    child was produced, so the recorded cancel can never be signalled and must be
    reported and cleared instead of leaking into this job's next run.
    """

    @pytest.fixture
    def cron(self, monkeypatch):
        cron = importlib.import_module("kiro_crew.cron_script")
        monkeypatch.setattr(cron, "_resolve_command_shell", lambda: "/bin/sh")
        monkeypatch.setattr(cron, "wrap_argv", lambda argv, **k: (list(argv), None))
        monkeypatch.setattr(cron, "cgroup_scope_argv", lambda argv: list(argv))

        def _reset():
            with cron._PROCS_LOCK:
                cron._SPAWNING_JOBS.clear()
                cron._CANCELLED_PROC_JOBS.clear()
                cron._RUNNING_PROCS.clear()

        _reset()
        yield cron
        _reset()

    @staticmethod
    def _raising_spawn(*_args, **_kwargs):
        # Stands in for abort_retry's deliberate re-raise, and for a genuinely
        # absent interpreter -- the branch treats them the same.
        raise _enoent(sys.executable)

    def test_a_cancel_recorded_during_a_failed_spawn_reports_cancelled(self, cron, monkeypatch):
        monkeypatch.setattr(cron, "popen_limited", self._raising_spawn)
        with cron._PROCS_LOCK:
            cron._CANCELLED_PROC_JOBS.add("job-x")

        result = cron.run_command_sandboxed("sleep 100", job_id="job-x")

        assert result["status"] == "cancelled"
        # No child exists, so there is no signal to report -- unlike the
        # raced-cancel case, which reports the child's returncode.
        assert result["exit_code"] == -1
        with cron._PROCS_LOCK:
            assert "job-x" not in cron._CANCELLED_PROC_JOBS
            assert "job-x" not in cron._SPAWNING_JOBS

    def test_a_failed_spawn_without_a_cancel_is_still_an_error(self, cron, monkeypatch):
        """Negative control: the recorded cancel is what changes the outcome.

        Without one the exception propagates to the function's own handler and
        the run is reported as an error, exactly as before this change.
        """
        monkeypatch.setattr(cron, "popen_limited", self._raising_spawn)

        result = cron.run_command_sandboxed("sleep 100", job_id="job-y")

        assert result["status"] == "error"
        with cron._PROCS_LOCK:
            assert "job-y" not in cron._SPAWNING_JOBS


class TestOverlappingRunCannotEatTheCancel:
    """A rerun must not consume the cancel aimed at a run still in its backoff.

    Every cancellation surface is keyed on the job id alone -- ``_RUNNING_PROCS``
    holds one child per job and ``kill_running_process`` takes only an id -- so two
    concurrent runs of one job make "cancel this job" ambiguous, and whichever
    finishes its spawn first consumes the flag. ``_begin_spawn`` therefore refuses
    the overlap.
    """

    def _reset(self, cron):
        with cron._PROCS_LOCK:
            cron._SPAWNING_JOBS.clear()
            cron._CANCELLED_PROC_JOBS.clear()
            cron._RUNNING_PROCS.clear()

    def test_a_second_run_is_refused_while_the_first_is_spawning(self):
        cron = importlib.import_module("kiro_crew.cron_script")
        self._reset(cron)
        assert cron._begin_spawn("job-a") is True
        # Run B for the SAME job, while A is still in its ENOENT backoff.
        assert cron._begin_spawn("job-a") is False
        self._reset(cron)

    def test_a_second_run_is_refused_while_the_first_is_registered(self):
        cron = importlib.import_module("kiro_crew.cron_script")
        self._reset(cron)
        assert cron._begin_spawn("job-b") is True
        assert cron._finish_spawn("job-b", MagicMock(name="Popen")) is False
        # Now registered rather than spawning -- still an overlap.
        assert cron._begin_spawn("job-b") is False
        self._reset(cron)

    def test_the_refused_rerun_cannot_consume_the_pending_cancel(self):
        """The race itself: the flag must still be there for the cancelled run."""
        cron = importlib.import_module("kiro_crew.cron_script")
        self._reset(cron)
        # Run A claims the slot and is cancelled mid-backoff.
        assert cron._begin_spawn("job-c") is True
        assert cron.kill_running_process("job-c") is True
        assert cron._spawn_cancelled("job-c") is True
        # A rerun is refused, so it never reaches _finish_spawn/_abandon_spawn and
        # cannot discard the flag. Before the refusal, B's _finish_spawn ate it and
        # A's abort_retry peek then returned False, letting A run the cancelled work.
        assert cron._begin_spawn("job-c") is False
        assert cron._spawn_cancelled("job-c") is True
        self._reset(cron)

    def test_a_distinct_job_is_not_refused(self):
        """Negative control: the refusal is per job, not a global lock."""
        cron = importlib.import_module("kiro_crew.cron_script")
        self._reset(cron)
        assert cron._begin_spawn("job-d") is True
        assert cron._begin_spawn("job-e") is True
        self._reset(cron)

    def test_an_unidentified_run_is_always_allowed_and_claims_nothing(self):
        cron = importlib.import_module("kiro_crew.cron_script")
        self._reset(cron)
        assert cron._begin_spawn(None) is True
        assert cron._begin_spawn(None) is True
        with cron._PROCS_LOCK:
            assert cron._SPAWNING_JOBS == set()
        self._reset(cron)

    def test_the_slot_is_reusable_once_the_run_ends(self):
        cron = importlib.import_module("kiro_crew.cron_script")
        self._reset(cron)
        assert cron._begin_spawn("job-f") is True
        assert cron._abandon_spawn("job-f") is False
        # Slot released, so the next run may claim it.
        assert cron._begin_spawn("job-f") is True
        self._reset(cron)


class TestCommandOutputUsesLocaleDecoding:
    """A cron's output must not be UTF-8-decoded with replacement.

    A command cron runs an ARBITRARY command, so its output carries the host's
    encoding. ``encoding="utf-8", errors="replace"`` turns every non-UTF-8 byte
    into U+FFFD before the output is persisted and delivered -- the original byte
    is gone, so this is irreversible corruption of the thing the user asked to
    see, not a display quirk.
    """

    @pytest.fixture
    def cron(self, monkeypatch):
        cron = importlib.import_module("kiro_crew.cron_script")
        monkeypatch.setattr(cron, "_resolve_command_shell", lambda: "/bin/sh")
        monkeypatch.setattr(cron, "wrap_argv", lambda argv, **k: (list(argv), None))
        monkeypatch.setattr(cron, "cgroup_scope_argv", lambda argv: list(argv))

        def _reset():
            with cron._PROCS_LOCK:
                cron._SPAWNING_JOBS.clear()
                cron._CANCELLED_PROC_JOBS.clear()
                cron._RUNNING_PROCS.clear()

        _reset()
        yield cron
        _reset()

    def test_the_command_spawn_does_not_force_utf8_replacement(self, cron, monkeypatch):
        """Fails on the pinned form, which passed encoding/errors explicitly."""
        seen: dict[str, object] = {}

        def _capture(_argv, **kwargs):
            seen.update(kwargs)
            raise RuntimeError("stop after capturing kwargs")

        monkeypatch.setattr(cron, "popen_limited", _capture)
        cron.run_command_sandboxed("echo hi", job_id="job-enc")

        assert seen, "popen_limited was never reached, so nothing was asserted"
        assert seen.get("text") is True
        # The defect, stated as the assertion: forcing either of these is what
        # replaced non-UTF-8 bytes with U+FFFD.
        assert "encoding" not in seen
        assert "errors" not in seen

    def test_forcing_utf8_replacement_really_does_destroy_the_bytes(self):
        """The mechanism, with a positive control, so the risk is not theoretical.

        These bytes are valid in a single-byte locale and invalid as UTF-8.
        """
        raw = b"caf\xe9\n"
        forced = raw.decode("utf-8", errors="replace")
        locale_like = raw.decode("latin-1")
        assert "\ufffd" in forced  # what the pinned form delivered
        assert "\ufffd" not in locale_like  # what locale decoding delivers
        assert locale_like.strip() == "café"
        # And it is not recoverable: the replacement character does not carry the
        # original byte, so nothing downstream can undo it.
        assert forced.strip().encode("utf-8") != raw.strip()

    def test_neither_cron_spawn_pins_an_encoding(self):
        """Source-level guard: re-pinning either site fails here, at source."""
        cron = importlib.import_module("kiro_crew.cron_script")
        source = pathlib.Path(cron.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        cron_spawns = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "popen_limited"
            and any(kw.arg == "abort_retry" for kw in node.keywords)
        ]
        assert len(cron_spawns) == 2, f"expected both cron spawns, saw {len(cron_spawns)}"
        for node in cron_spawns:
            kwargs = {kw.arg for kw in node.keywords if kw.arg}
            assert "encoding" not in kwargs
            assert "errors" not in kwargs
            assert "text" in kwargs


class TestAnOverlappingWakeCostsNoFailureStrike:
    """A refused overlap must not be counted as a job failure.

    The consumer records a strike toward auto-pause for any status that is not
    ``ok`` and not intercepted earlier, so returning ``error`` here would inflate
    strikes for a transient scheduling overlap -- the same harm this change
    exists to reduce. ``skipped`` matches the scheduler's own pre-existing
    overlap guard, which logs "previous execution still running, skipping" and
    returns without counting anything.
    """

    @pytest.fixture
    def cron(self, monkeypatch):
        cron = importlib.import_module("kiro_crew.cron_script")
        monkeypatch.setattr(cron, "_resolve_command_shell", lambda: "/bin/sh")
        monkeypatch.setattr(cron, "wrap_argv", lambda argv, **k: (list(argv), None))
        monkeypatch.setattr(cron, "cgroup_scope_argv", lambda argv: list(argv))

        def _reset():
            with cron._PROCS_LOCK:
                cron._SPAWNING_JOBS.clear()
                cron._CANCELLED_PROC_JOBS.clear()
                cron._RUNNING_PROCS.clear()

        _reset()
        yield cron
        _reset()

    def test_a_refused_command_overlap_reports_skipped_not_error(self, cron):
        # A run of this job is already in flight.
        assert cron._begin_spawn("job-ovl") is True

        result = cron.run_command_sandboxed("echo hi", job_id="job-ovl")

        # "error" here would reach record_failure() and strike toward auto-pause.
        assert result["status"] == "skipped"
        assert result["status"] != "error"

    def test_the_refusal_does_not_disturb_the_other_run(self, cron):
        assert cron._begin_spawn("job-ovl2") is True
        with cron._PROCS_LOCK:
            cron._CANCELLED_PROC_JOBS.add("job-ovl2")

        cron.run_command_sandboxed("echo hi", job_id="job-ovl2")

        # Still claimed, and the other run's cancel is still pending for it.
        with cron._PROCS_LOCK:
            assert "job-ovl2" in cron._SPAWNING_JOBS
            assert "job-ovl2" in cron._CANCELLED_PROC_JOBS

    def test_the_consumer_treats_skipped_as_non_counting(self):
        """The status is inert unless the consumer honours it -- pin that too."""
        gateway = pathlib.Path(
            importlib.import_module("kiro_crew.slack.gateway").__file__
        ).read_text(encoding="utf-8")
        # Both cron dispatch paths must intercept it before the strike branch.
        assert gateway.count('== "skipped"') == 2


class TestARefusedOverlapIsNotPersistedAsASuccessfulRun:
    """A refused overlap must record "did not run" -- not a fabricated success.

    ``CronService._execute`` treats ANY non-``"error"`` ``last_status`` as success:
    it sets ``last_status="ok"`` and calls ``record_success()``, which also resets
    the auto-pause budget. So a dispatch branch that merely returns ``None``
    without setting ``last_status`` persists a run that never happened. The
    ``cancelled`` branch escapes this only via ``_execute``'s separate
    ``self._cancelled_jobs`` membership check, which a refused overlap is not in.

    These tests assert the disposition the starvation and fire-time-denial paths
    already use: ``last_status="error"`` (skips the success branch) plus
    ``run_never_started=True`` (retention marker), with ``record_failure()``
    deliberately NOT called so no strike is spent.
    """

    def _dispatch_source(self) -> str:
        return pathlib.Path(importlib.import_module("kiro_crew.slack.gateway").__file__).read_text(
            encoding="utf-8"
        )

    def _skipped_branches(self, source: str) -> list[str]:
        """The CODE of each `status == "skipped"` branch, up to its return.

        Comment lines are stripped: these branches deliberately explain in prose
        why ``record_failure`` is not called, and a substring check against the
        raw text would match that explanation instead of a real call.
        """
        out: list[str] = []
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if '== "skipped"' not in line:
                continue
            body: list[str] = []
            for nxt in lines[i + 1 : i + 40]:
                if not nxt.strip().startswith("#"):
                    body.append(nxt)
                if nxt.strip() == "return None":
                    break
            out.append("\n".join(body))
        return out

    def test_both_dispatch_paths_intercept_the_skipped_status(self):
        branches = self._skipped_branches(self._dispatch_source())
        assert len(branches) == 2, f"expected both cron dispatch paths, saw {len(branches)}"

    def test_neither_branch_leaves_last_status_unset(self):
        """The defect: an unset last_status is what _execute reads as success."""
        for body in self._skipped_branches(self._dispatch_source()):
            assert 'job.last_status = "error"' in body

    def test_both_branches_mark_the_run_as_never_started(self):
        for body in self._skipped_branches(self._dispatch_source()):
            assert "job.run_never_started = True" in body

    def test_neither_branch_counts_a_failure_strike(self):
        """Negative control: the fix must not swing the other way into a strike.

        A strike comes only from an explicit record_failure(); last_status alone
        never counts one. An overlap is a scheduling condition, so it must spend
        no auto-pause budget -- the same reasoning the starvation path documents.
        """
        for body in self._skipped_branches(self._dispatch_source()):
            assert "record_failure" not in body

    def test_neither_branch_records_a_success(self):
        for body in self._skipped_branches(self._dispatch_source()):
            assert "record_success" not in body

    def test_execute_really_does_treat_a_non_error_status_as_success(self):
        """Positive control: proves the hazard these tests guard is real.

        If this ever stops holding, the assertions above are guarding nothing.
        """
        cron_src = pathlib.Path(importlib.import_module("kiro_crew.cron").__file__).read_text(
            encoding="utf-8"
        )
        assert 'if job.last_status != "error":' in cron_src
        assert "job.record_success()" in cron_src


class TestRunLimitedToleratesAnAbsentInterpreter:
    """``run_limited`` shares the exposure: same `_prepare_limited_spawn` prefix.

    Same shape as the ``popen_limited`` tests above, because the contract is the
    same one -- only the ``subprocess`` entry point differs.
    """

    def test_retries_until_the_tree_comes_back(self):
        done = MagicMock(name="CompletedProcess")
        attempts = [_enoent(sys.executable), _enoent(sys.executable), done]

        def fake_run(*_args, **_kwargs):
            outcome = attempts.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        slept: list[float] = []
        with (
            _prepared(),
            patch.object(sandbox_mod.subprocess, "run", side_effect=fake_run),
            patch.object(sandbox_mod.time, "sleep", side_effect=slept.append),
        ):
            got = sandbox_mod.run_limited(["/bin/echo", "hi"])

        assert got is done
        assert slept == list(_INTERPRETER_ENOENT_DELAYS[:2])
        # The caller still sees its OWN argv, not the launcher's.
        assert got.args == ["/bin/echo", "hi"]

    def test_a_permanently_absent_interpreter_still_raises(self):
        """Negative control: the final attempt is unguarded, so it re-raises."""
        final = _enoent(sys.executable)
        raised = [_enoent(sys.executable)] * len(_INTERPRETER_ENOENT_DELAYS) + [final]
        with (
            _prepared(),
            patch.object(sandbox_mod.subprocess, "run", side_effect=raised) as run,
            patch.object(sandbox_mod.time, "sleep"),
        ):
            with pytest.raises(FileNotFoundError) as caught:
                sandbox_mod.run_limited(["/bin/echo", "hi"])

        assert caught.value is final
        assert run.call_count == len(_INTERPRETER_ENOENT_DELAYS) + 1

    def test_a_non_transient_enoent_is_not_retried(self):
        with (
            _prepared(),
            patch.object(
                sandbox_mod.subprocess, "run", side_effect=_enoent("/nonexistent/workdir")
            ) as run,
            patch.object(sandbox_mod.time, "sleep") as sleep,
        ):
            with pytest.raises(FileNotFoundError):
                sandbox_mod.run_limited(["/bin/echo", "hi"])

        assert run.call_count == 1
        sleep.assert_not_called()

    def test_a_user_binary_enoent_is_not_retried(self):
        missing = "/usr/bin/definitely-not-installed"
        with (
            _prepared([missing, "--version"]),
            patch.object(sandbox_mod.subprocess, "run", side_effect=_enoent(missing)) as run,
            patch.object(sandbox_mod.time, "sleep") as sleep,
        ):
            with pytest.raises(FileNotFoundError):
                sandbox_mod.run_limited([missing, "--version"])

        assert run.call_count == 1
        sleep.assert_not_called()

    def test_a_called_process_error_still_reports_the_callers_argv(self):
        """The retry nests INSIDE the cmd-rewriting handler, so this still holds."""
        exc = subprocess.CalledProcessError(1, _LAUNCHER_CMD)
        with (
            _prepared(),
            patch.object(sandbox_mod.subprocess, "run", side_effect=exc),
            patch.object(sandbox_mod.time, "sleep"),
        ):
            with pytest.raises(subprocess.CalledProcessError) as caught:
                sandbox_mod.run_limited(["/bin/echo", "hi"])

        assert caught.value.cmd == ["/bin/echo", "hi"]


class TestCreateSubprocessLimitedToleratesAnAbsentInterpreter:
    """The async wrapper must ride out the blip WITHOUT blocking the loop."""

    def _shimmed(self):
        """Force the shim-prefixed branch with an interpreter-headed prefix."""
        return patch.object(
            sandbox_mod,
            "spawn_shim_argv",
            return_value=(sys.executable, "-I", "-S", "-c", "pass"),
        )

    @pytest.mark.asyncio
    async def test_retries_until_the_tree_comes_back(self):
        proc = MagicMock(name="Process")
        attempts = [_enoent(sys.executable), _enoent(sys.executable), proc]

        async def fake_exec(*_args, **_kwargs):
            outcome = attempts.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        slept: list[float] = []

        async def fake_sleep(d):
            slept.append(d)

        with (
            self._shimmed(),
            patch.object(sandbox_mod.asyncio, "create_subprocess_exec", side_effect=fake_exec),
            patch.object(sandbox_mod.asyncio, "sleep", side_effect=fake_sleep),
        ):
            got = await sandbox_mod.create_subprocess_limited("/bin/echo", "hi")

        assert got is proc
        assert slept == list(_INTERPRETER_ENOENT_DELAYS[:2])

    @pytest.mark.asyncio
    async def test_the_backoff_never_blocks_the_event_loop(self):
        """time.sleep here would freeze the gateway for ~3.75s -- the core hazard."""
        attempts = [_enoent(sys.executable), MagicMock(name="Process")]

        async def fake_exec(*_args, **_kwargs):
            outcome = attempts.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        async def fake_sleep(_d):
            return None

        with (
            self._shimmed(),
            patch.object(sandbox_mod.asyncio, "create_subprocess_exec", side_effect=fake_exec),
            patch.object(sandbox_mod.asyncio, "sleep", side_effect=fake_sleep),
            patch.object(sandbox_mod.time, "sleep") as blocking,
        ):
            await sandbox_mod.create_subprocess_limited("/bin/echo", "hi")

        blocking.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_permanently_absent_interpreter_still_raises(self):
        final = _enoent(sys.executable)
        raised = [_enoent(sys.executable)] * len(_INTERPRETER_ENOENT_DELAYS) + [final]
        calls = {"n": 0}

        async def fake_exec(*_args, **_kwargs):
            exc = raised[calls["n"]]
            calls["n"] += 1
            raise exc

        async def fake_sleep(_d):
            return None

        with (
            self._shimmed(),
            patch.object(sandbox_mod.asyncio, "create_subprocess_exec", side_effect=fake_exec),
            patch.object(sandbox_mod.asyncio, "sleep", side_effect=fake_sleep),
        ):
            with pytest.raises(FileNotFoundError) as caught:
                await sandbox_mod.create_subprocess_limited("/bin/echo", "hi")

        assert caught.value is final
        assert calls["n"] == len(_INTERPRETER_ENOENT_DELAYS) + 1

    @pytest.mark.asyncio
    async def test_a_user_binary_enoent_is_not_retried(self):
        missing = "/usr/bin/definitely-not-installed"
        calls = {"n": 0}

        async def fake_exec(*_args, **_kwargs):
            calls["n"] += 1
            raise _enoent(missing)

        async def fake_sleep(_d):
            return None

        with (
            self._shimmed(),
            patch.object(sandbox_mod.asyncio, "create_subprocess_exec", side_effect=fake_exec),
            patch.object(sandbox_mod.asyncio, "sleep", side_effect=fake_sleep) as slept,
        ):
            with pytest.raises(FileNotFoundError):
                await sandbox_mod.create_subprocess_limited(missing, "--version")

        assert calls["n"] == 1
        slept.assert_not_called()


class TestAllThreeWrappersShareOneDiscriminator:
    """The decision is factored; the three spawns are deliberately NOT."""

    def _source(self) -> str:
        return pathlib.Path(sandbox_mod.__file__).read_text(encoding="utf-8")

    def test_every_wrapper_routes_through_the_shared_helper(self):
        src = self._source()
        # One definition, three call sites -- not three copies of the predicate.
        assert src.count("def _retry_interpreter_enoent(") == 1
        assert src.count("_retry_interpreter_enoent(exc,") == 3

    def test_no_wrapper_reimplements_the_predicate_inline(self):
        """Negative control: a fourth copy of the predicate would fail this."""
        src = self._source()
        # Only the shared helper and its own definition may name the predicate.
        assert src.count("_is_transient_interpreter_enoent(") == 2

    def test_each_spawn_still_lives_in_its_own_wrapper(self):
        """The audits key on the enclosing function, so this is load-bearing."""
        tree = ast.parse(self._source())
        wanted = {
            "run_limited": "run",
            "popen_limited": "Popen",
            "create_subprocess_limited": "create_subprocess_exec",
        }
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            attr = wanted.get(node.name)
            if attr is None:
                continue
            found = [
                n
                for n in ast.walk(node)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == attr
            ]
            assert found, f"{node.name} must spawn via {attr} in its own body"
            wanted.pop(node.name)
        assert not wanted, f"wrappers not found: {sorted(wanted)}"

    def test_the_async_wrapper_uses_an_async_sleep(self):
        """A blocking sleep in the async wrapper would freeze the loop."""
        tree = ast.parse(self._source())
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "create_subprocess_limited":
                calls = [
                    n
                    for n in ast.walk(node)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "sleep"
                ]
                names = {n.func.value.id for n in calls if isinstance(n.func.value, ast.Name)}
                assert "asyncio" in names
                assert "time" not in names
                return
        raise AssertionError("create_subprocess_limited not found")

    def test_abort_retry_exists_only_on_the_wrapper_with_consumers(self):
        """Subtraction guard: the hook belongs to ``popen_limited`` alone.

        It was briefly carried onto both siblings "for symmetry" and had ZERO
        callers there. Only ``popen_limited`` has consumers -- the two cron spawn
        sites -- so a parameter on either sibling is inherited provenance rather
        than a requirement. This fails if one is reintroduced without a caller.
        """
        tree = ast.parse(self._source())
        expected = {
            "popen_limited": True,
            "run_limited": False,
            "create_subprocess_limited": False,
        }
        seen: dict[str, bool] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name in expected:
                seen[node.name] = "abort_retry" in {a.arg for a in node.args.kwonlyargs}
        assert seen == expected, f"abort_retry placement changed: {seen}"


class TestTheStrictShellProbeNoLongerLatchesOnABlip:
    """Extending the retry to ``run_limited`` cures a latch, not just a spawn.

    ``cron_script._shell_is_posix_strict`` probes a candidate shell through
    ``run_limited`` and catches ``(OSError, SubprocessError,
    SandboxUnavailableError)`` into ``result = False``, which it then CACHES in
    ``_POSIX_STRICT_CACHE`` for the process lifetime. ``FileNotFoundError`` is an
    ``OSError``, so before the retry reached ``run_limited`` a ~1s interpreter
    blip made the probe answer "this shell expands braces" -- permanently, for
    every later caller, long after the tree healed.

    The retry now absorbs that blip inside ``run_limited``, so it never reaches
    the except clause and never latches. The BROADER design (caching a failure
    permanently) is unchanged and still deferred -- a tree down longer than the
    ~3.75s budget still latches.
    """

    def test_a_transient_blip_no_longer_caches_a_wrong_answer(self):
        cron = importlib.import_module("kiro_crew.cron_script")
        shell = "/bin/dash"
        cron._POSIX_STRICT_CACHE.pop(shell, None)

        good = MagicMock(returncode=0, stdout="x.{a,a}\n")
        attempts = [_enoent(sys.executable), good]

        def fake_run(*_args, **_kwargs):
            outcome = attempts.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        with (
            _prepared(),
            patch.object(cron, "wrap_argv", return_value=([shell, "-c", "echo x.{a,a}"], None)),
            patch.object(cron, "cgroup_scope_argv", side_effect=lambda a: a),
            patch.object(cron, "_clean_cron_env", return_value={}),
            patch.object(sandbox_mod.subprocess, "run", side_effect=fake_run),
            patch.object(sandbox_mod.time, "sleep"),
        ):
            got = cron._shell_is_posix_strict(shell)

        # Pre-fix this was False, and the False was cached for the process life.
        assert got is True
        assert cron._POSIX_STRICT_CACHE[shell] is True
        cron._POSIX_STRICT_CACHE.pop(shell, None)

    def test_with_no_retry_budget_the_latch_reappears(self):
        """Negative control: proves the test above detects the defect.

        Emptying the delay budget makes the loop body never run, so the
        unguarded final attempt raises immediately -- which is exactly the
        pre-fix shape. The wrong answer is cached, as it used to be.
        """
        cron = importlib.import_module("kiro_crew.cron_script")
        shell = "/bin/dash"
        cron._POSIX_STRICT_CACHE.pop(shell, None)

        with (
            _prepared(),
            patch.object(cron, "wrap_argv", return_value=([shell, "-c", "echo x.{a,a}"], None)),
            patch.object(cron, "cgroup_scope_argv", side_effect=lambda a: a),
            patch.object(cron, "_clean_cron_env", return_value={}),
            patch.object(sandbox_mod, "_INTERPRETER_ENOENT_DELAYS", ()),
            patch.object(sandbox_mod.subprocess, "run", side_effect=_enoent(sys.executable)),
            patch.object(sandbox_mod.time, "sleep"),
        ):
            got = cron._shell_is_posix_strict(shell)

        assert got is False
        assert cron._POSIX_STRICT_CACHE[shell] is False
        cron._POSIX_STRICT_CACHE.pop(shell, None)


class TestACancelDuringTheShellProbeIsNotLost:
    """The probe's backoff was a window with no owner, so a cancel vanished.

    ``run_command_sandboxed`` resolves its shell before it spawns, and that
    resolution runs ``_shell_is_posix_strict`` through ``run_limited`` -- which
    now carries the interpreter-ENOENT backoff. On a cold ``_POSIX_STRICT_CACHE``
    the function could therefore sleep for seconds while the job was in NEITHER
    ``_SPAWNING_JOBS`` nor ``_RUNNING_PROCS``, and ``kill_running_process`` has no
    third place to record against: it returned ``False`` and the cancellation was
    DISCARDED rather than delayed. The probe then finished and the command the
    user cancelled was launched, side effects and all.

    The claim is now taken before the probe, so such a cancel is recorded, and a
    pre-spawn check turns it into a launch that never happens. A spawn-then-kill
    would not do: the command would still run for as long as the signal took.
    """

    @pytest.fixture
    def cron(self):
        cron = importlib.import_module("kiro_crew.cron_script")

        def _reset():
            with cron._PROCS_LOCK:
                cron._SPAWNING_JOBS.clear()
                cron._CANCELLED_PROC_JOBS.clear()
                cron._RUNNING_PROCS.clear()
            cron._POSIX_STRICT_CACHE.pop("/bin/dash", None)

        _reset()
        yield cron
        _reset()

    @staticmethod
    def _probe_blip(cron, on_backoff):
        """Patches making the FIRST probe attempt blip, calling *on_backoff*.

        The cancel is delivered from inside the patched sleep, which is exactly
        where a real one lands: mid-backoff, with the spawn not yet made.
        """
        good = MagicMock(returncode=0, stdout="x.{a,a}\n")
        attempts: list = [_enoent(sys.executable), good]

        def fake_run(*_a, **_k):
            outcome = attempts.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        return (
            _prepared(),
            patch.object(cron, "_resolve_command_shell", wraps=cron._resolve_command_shell),
            patch.object(cron, "wrap_argv", side_effect=lambda argv, **k: (list(argv), None)),
            patch.object(cron, "cgroup_scope_argv", side_effect=lambda a: list(a)),
            patch.object(cron, "_clean_cron_env", return_value={}),
            patch.object(cron.os.path, "isfile", return_value=True),
            patch.object(sandbox_mod.subprocess, "run", side_effect=fake_run),
            patch.object(sandbox_mod.time, "sleep", side_effect=lambda _d: on_backoff()),
        )

    @pytest.mark.skipif(
        not pc.IS_POSIX,
        reason=(
            "_resolve_command_shell returns None unconditionally on Windows, so "
            "_shell_is_posix_strict -- and the backoff window this closes -- is "
            "unreachable there. Command crons are unavailable on Windows by "
            "design; script crons are the supported path."
        ),
    )
    def test_the_cancelled_command_is_never_launched(self, cron):
        accepted: list[bool] = []
        popen = MagicMock(name="popen_limited")

        with patch.object(cron, "popen_limited", popen):
            with _ExitStack(
                *self._probe_blip(
                    cron, lambda: accepted.append(cron.kill_running_process("job-probe"))
                )
            ):
                result = cron.run_command_sandboxed("rm -rf ./data", job_id="job-probe")

        # The cancel had somewhere to land: pre-fix this was False -- discarded.
        assert accepted == [True]
        # And the command never ran. This is the assertion that fails pre-fix.
        assert popen.call_count == 0
        assert result["status"] == "cancelled"

    @pytest.mark.skipif(
        not pc.IS_POSIX,
        reason="Same reason as the test above: no POSIX shell resolves on Windows.",
    )
    def test_a_cancel_for_a_different_job_still_lets_this_one_run(self, cron):
        """Control: proves the assertions above key on THIS job's cancellation.

        Same blip, same backoff, same patches -- only the cancelled id differs.
        The command must still be launched, so a test that passed because
        ``popen_limited`` was simply unreachable would fail here.
        """
        popen = MagicMock(name="popen_limited")
        popen.return_value.communicate.return_value = ("", "")
        popen.return_value.returncode = 0

        with patch.object(cron, "popen_limited", popen):
            with _ExitStack(
                *self._probe_blip(cron, lambda: cron.kill_running_process("some-other-job"))
            ):
                result = cron.run_command_sandboxed("echo hi", job_id="job-probe2")

        assert popen.call_count == 1
        assert result["status"] != "cancelled"

    def test_the_claim_is_released_when_no_shell_resolves(self, cron):
        """The claim now spans early returns, so every one of them must free it.

        A leaked claim is worse than the bug it guards: ``_begin_spawn`` would
        refuse every later wake of this job for the life of the process.
        """
        with patch.object(cron, "_resolve_command_shell", return_value=None):
            result = cron.run_command_sandboxed("echo hi", job_id="job-noshell")

        assert result["status"] == "error"
        with cron._PROCS_LOCK:
            assert "job-noshell" not in cron._SPAWNING_JOBS

    def test_the_claim_is_released_when_the_sandbox_refuses(self, cron):
        with (
            patch.object(cron, "_resolve_command_shell", return_value="/bin/sh"),
            patch.object(cron, "wrap_argv", side_effect=RuntimeError("no backend")),
        ):
            result = cron.run_command_sandboxed("echo hi", job_id="job-nosbx")

        assert result["status"] == "error"
        with cron._PROCS_LOCK:
            assert "job-nosbx" not in cron._SPAWNING_JOBS


class TestSpawnOnlyCancellationIsReportedHonestly:
    """``kill_running_process`` returning True without a kill has two readers.

    It now returns True either because it signalled a live child OR because it
    recorded the cancel against a spawn still in flight. The control-flow reader
    is fine with that -- but it is only fine because of a guard, and the audit
    reader was asserting a kill that never happened.
    """

    @staticmethod
    def _cron_source() -> str:
        return pathlib.Path(importlib.import_module("kiro_crew.cron").__file__).read_text(
            encoding="utf-8"
        )

    def test_the_control_flow_reader_is_gated_on_the_job_being_an_agent_job(self):
        """A spawn-only True must not be able to suppress an agent-session reset.

        It cannot, because the branch also requires ``is_agent_job`` -- and only
        ``run_script_sandboxed`` / ``run_command_sandboxed`` ever claim a spawn,
        so an agent job is never in ``_SPAWNING_JOBS`` to begin with. Pin the
        guard: dropping it would make the two facts silently interact.
        """
        assert "if self._sessions and is_agent_job and not killed_proc:" in self._cron_source()

        cron_script = importlib.import_module("kiro_crew.cron_script")
        claimers = {
            node.name
            for node in ast.walk(
                ast.parse(pathlib.Path(cron_script.__file__).read_text(encoding="utf-8"))
            )
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(c, ast.Call)
                and isinstance(c.func, ast.Name)
                and c.func.id == "_begin_spawn"
                for c in ast.walk(node)
            )
        }
        assert claimers == {"run_script_sandboxed", "run_command_sandboxed"}

    def test_the_audit_reader_no_longer_claims_a_kill_that_did_not_happen(self):
        source = self._cron_source()
        assert '"cancellation_accepted": killed_proc,' in source
        # The old KEY is gone from the metadata dict. Prose naming it is fine --
        # the comment at that seam explains why the rename happened.
        assert '"killed_subprocess": killed_proc' not in source


class _ExitStack:
    """Minimal nested-context helper (contextlib.ExitStack without the import)."""

    def __init__(self, *managers):
        self._managers = managers
        self._entered: list = []

    def __enter__(self):
        for manager in self._managers:
            manager.__enter__()
            self._entered.append(manager)
        return self

    def __exit__(self, *exc_info):
        for manager in reversed(self._entered):
            manager.__exit__(*exc_info)
        return False


def test_real_spawn_still_works_end_to_end():
    """Guards against the retry wrapper breaking the ordinary success path."""
    proc = sandbox_mod.popen_limited(
        [sys.executable, "-c", "print('ok')"],
        stdout=subprocess.PIPE,
        **UTF8_TEXT,
    )
    out, _ = proc.communicate(timeout=60)
    assert out.strip() == "ok"
    assert proc.returncode == 0
