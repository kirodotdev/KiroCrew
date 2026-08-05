"""Tests for the debug-only performance sampler (kirocrew perf sample)."""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import pytest

from kiro_crew import cli_perf, perf_sampler

# ── The debug gate ──


class TestGate:
    def test_absent_env_is_disabled(self):
        assert perf_sampler.profiling_enabled({}) is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " on "])
    def test_truthy_values_enable(self, value):
        assert perf_sampler.profiling_enabled({perf_sampler.DEBUG_ENV_VAR: value}) is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "maybe"])
    def test_other_values_stay_disabled(self, value):
        # "0" must read as OFF. Treating mere presence as on would make
        # KIROCREW_DEBUG=0 silently enable profiling.
        assert perf_sampler.profiling_enabled({perf_sampler.DEBUG_ENV_VAR: value}) is False

    def test_refusal_message_names_the_switch(self):
        assert perf_sampler.DEBUG_ENV_VAR in perf_sampler.gate_refusal_message()

    def test_cli_refuses_when_gate_off(self, monkeypatch, capsys, tmp_path):
        monkeypatch.delenv(perf_sampler.DEBUG_ENV_VAR, raising=False)
        args = _sample_args(perf_call="time:sleep", output=tmp_path / "p.folded")
        assert cli_perf._perf_sample(args) == 1
        assert perf_sampler.DEBUG_ENV_VAR in capsys.readouterr().err
        # Nothing may be written when the gate refuses.
        assert not (tmp_path / "p.folded").exists()


def _sample_args(**overrides: object) -> argparse.Namespace:
    """A Namespace shaped like the parsed `perf sample` arguments."""
    base: dict[str, object] = {
        "perf_action": "sample",
        "seconds": 10,
        "interval": perf_sampler.DEFAULT_INTERVAL_SECONDS,
        "pid": 0,
        "perf_call": "",
        "output": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# ── Argument-name collision regression ──


class TestParserWiring:
    def test_sample_flags_do_not_shadow_the_subcommand_dest(self):
        """`perf sample` must leave args.command == "perf".

        The top-level subparsers use dest="command"; a flag on this subparser
        whose dest is also "command" silently overwrites the subcommand name, so
        dispatch falls through to argparse's help path and the command appears to
        do nothing. Pin it.
        """
        parser = argparse.ArgumentParser(prog="kirocrew")
        sub = parser.add_subparsers(dest="command")
        cli_perf.register_perf_parser(sub)

        ns = parser.parse_args(["perf", "sample", "--call", "mod:fn"])

        assert ns.command == "perf"
        assert ns.perf_action == "sample"
        assert ns.perf_call == "mod:fn"

    def test_no_sample_argument_uses_the_command_dest(self):
        parser = argparse.ArgumentParser(prog="kirocrew")
        sub = parser.add_subparsers(dest="command")
        cli_perf.register_perf_parser(sub)
        ns = parser.parse_args(["perf", "sample"])
        # Every optional resolved to its default without clobbering "perf".
        assert ns.command == "perf"


# ── The in-process sampler ──


def _recognisable_workload(deadline: float | None = None) -> None:
    """Burn CPU inside a uniquely named frame so the profile is checkable.

    With *deadline* (an absolute :func:`time.monotonic` value) the burn runs until
    then, so a caller can keep this frame on the stack until the sampler has actually
    observed it instead of guessing how long that takes. The default keeps the
    original fixed 0.25s span for callers that only need the frame to exist.
    """
    if deadline is None:
        deadline = time.monotonic() + 0.25
    total = 0
    while time.monotonic() < deadline:
        total += sum(i * i for i in range(2000))


class TestStackSampler:
    @pytest.mark.parametrize("bad", [0.0, 0.0005, 1.5, -1.0])
    def test_rejects_out_of_range_interval(self, bad):
        with pytest.raises(ValueError):
            perf_sampler.StackSampler(interval=bad)

    def test_collects_samples_and_names_the_frame(self):
        # Poll for the frame rather than burning a fixed span and hoping a sample
        # landed. The requested 2ms interval is only a request: Windows rounds
        # `Event.wait` up to ~15.6ms and a loaded runner starves the daemon thread,
        # so the old fixed 0.25s burn expected ~125 samples and CI observed one (of
        # an unrelated frame). Holding the frame until it is seen makes this
        # independent of the achieved rate, and the deadline still fails loudly.
        sampler = perf_sampler.StackSampler(interval=0.002)
        sampler.start()
        give_up_at = time.monotonic() + 30.0

        def _seen() -> bool:
            # Snapshot before scanning: the sampler thread inserts into `_counts` on
            # every tick, and iterating it live can raise "dictionary changed size
            # during iteration" -- which would be a NEW flake in the test that exists
            # to remove one. `list()` on a dict is atomic under the GIL.
            return any("_recognisable_workload" in stack for stack in list(sampler._counts))

        while not _seen():
            assert time.monotonic() < give_up_at, (
                "the sampler never sampled _recognisable_workload in 30s "
                f"(samples={sampler._samples})"
            )
            _recognisable_workload(min(time.monotonic() + 0.05, give_up_at))
        report = sampler.stop()

        assert report.samples > 0
        folded = perf_sampler.render_folded(report)
        assert "_recognisable_workload" in folded
        # Every line is "<stack> <count>" with a positive integer count.
        for line in folded.splitlines():
            stack, _, count = line.rpartition(" ")
            assert stack
            assert int(count) > 0

    def test_stop_without_start_raises(self):
        with pytest.raises(RuntimeError):
            perf_sampler.StackSampler().stop()

    def test_double_start_raises(self):
        sampler = perf_sampler.StackSampler()
        sampler.start()
        try:
            with pytest.raises(RuntimeError):
                sampler.start()
        finally:
            sampler.stop()

    def test_context_manager_stops_the_thread(self):
        with perf_sampler.StackSampler(interval=0.002):
            _recognisable_workload()
        # The daemon thread must be gone, not merely idle: a leaked sampler keeps
        # reading frames for the life of the process.
        assert not any(
            t.name == "kirocrew-perf-sampler" for t in __import__("threading").enumerate()
        )

    def test_effective_rate_reports_zero_when_nothing_sampled(self):
        report = perf_sampler.SampleReport(counts={}, samples=0, duration=0.0, interval=0.005)
        assert report.effective_rate == 0.0

    def test_effective_rate_is_measured_not_requested(self):
        report = perf_sampler.SampleReport(counts={}, samples=50, duration=2.0, interval=0.001)
        # 50 samples in 2s is 25/s even though 0.001s asked for 1000/s.
        assert report.effective_rate == 25.0


# ── Rendering, redaction and frame labels ──


class TestRendering:
    def test_folded_is_sorted_hottest_first_then_deterministic(self):
        report = perf_sampler.SampleReport(
            counts={"b;x": 5, "a;y": 5, "c;z": 9}, samples=19, duration=1.0, interval=0.005
        )
        assert perf_sampler.render_folded(report).splitlines() == ["c;z 9", "a;y 5", "b;x 5"]

    def test_empty_report_renders_empty(self):
        report = perf_sampler.SampleReport(counts={}, samples=0, duration=0.0, interval=0.005)
        assert perf_sampler.render_folded(report) == ""

    def test_frame_label_drops_the_home_directory_prefix(self):
        label = perf_sampler._frame_label("/home/someone/secret/pkg/mod.py", 42, "fn")
        assert label == "fn (pkg/mod.py:42)"
        assert "someone" not in label

    def test_sanitize_redacts_a_credential(self):
        dirty = "fn (pkg/mod.py:1);AKIAIOSFODNN7EXAMPLE 3"
        assert "AKIAIOSFODNN7EXAMPLE" not in perf_sampler.sanitize_profile(dirty)


# ── py-spy integration (out-of-process) ──


class TestPySpy:
    def test_argv_raises_when_pyspy_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(perf_sampler.shutil, "which", lambda _n: None)
        with pytest.raises(FileNotFoundError):
            perf_sampler.pyspy_argv(pid=123, seconds=5, output=tmp_path / "o", rate=100)

    def test_argv_requests_folded_output(self, monkeypatch, tmp_path):
        monkeypatch.setattr(perf_sampler.shutil, "which", lambda _n: "/usr/bin/py-spy")
        argv = perf_sampler.pyspy_argv(pid=123, seconds=5, output=tmp_path / "o", rate=100)
        assert argv[:2] == ["/usr/bin/py-spy", "record"]
        assert "--pid" in argv and "123" in argv
        # raw == folded stacks, so both sampling strategies emit one format.
        assert argv[argv.index("--format") + 1] == "raw"

    def test_unavailable_message_explains_macos_and_the_extra(self):
        message = perf_sampler.pyspy_unavailable_message()
        assert "task_for_pid" in message
        assert "kirocrew[perf]" in message

    def test_candidate_paths_are_probed_before_PATH(self, monkeypatch, tmp_path):
        """A py-spy installed by homebrew/cargo/pip --user must still be found.

        Electron (and launchd) give a minimal PATH with no shell profile sourced,
        so a which-only lookup reports "not installed" on a machine that has it --
        the same trap website/electron/pyspy-dump.js already probes around.
        """
        fake = tmp_path / "py-spy"
        fake.write_text("#!/bin/sh\n", encoding="utf-8")
        fake.chmod(0o755)
        monkeypatch.setattr(perf_sampler, "pyspy_candidates", lambda: (fake,))
        # PATH lookup deliberately fails, so only the candidate probe can succeed.
        monkeypatch.setattr(perf_sampler.shutil, "which", lambda _n: None)
        assert perf_sampler.pyspy_path() == str(fake)

    def test_falls_back_to_PATH_when_no_candidate_exists(self, monkeypatch, tmp_path):
        monkeypatch.setattr(perf_sampler, "pyspy_candidates", lambda: (tmp_path / "absent",))
        monkeypatch.setattr(perf_sampler.shutil, "which", lambda _n: "/usr/bin/py-spy")
        assert perf_sampler.pyspy_path() == "/usr/bin/py-spy"

    @pytest.mark.skipif(
        os.name == "nt",
        reason="Windows has no execute bit: os.access(X_OK) answers True for any "
        "existing file, so an 'executable?' filter cannot be asserted there. The "
        "candidate probe is POSIX-only for that reason (see pyspy_candidates).",
    )
    def test_non_executable_candidate_is_skipped(self, monkeypatch, tmp_path):
        plain = tmp_path / "py-spy"
        plain.write_text("not executable", encoding="utf-8")
        plain.chmod(0o644)
        monkeypatch.setattr(perf_sampler, "pyspy_candidates", lambda: (plain,))
        monkeypatch.setattr(perf_sampler.shutil, "which", lambda _n: None)
        assert perf_sampler.pyspy_path() is None

    @pytest.mark.skipif(
        os.name == "nt",
        reason="The candidate list is POSIX-only; on Windows it is empty by design "
        "and discovery goes through shutil.which/PATHEXT.",
    )
    def test_candidate_list_matches_the_electron_module(self):
        # Kept in sync with pyspy-dump.js's PYSPY_CANDIDATES; a divergence means
        # one surface finds py-spy and the other reports it missing. Compared with
        # as_posix() so the assertion does not depend on the host separator.
        names = {p.as_posix() for p in perf_sampler.pyspy_candidates()}
        assert "/opt/homebrew/bin/py-spy" in names
        assert "/usr/local/bin/py-spy" in names
        assert any(n.endswith(".cargo/bin/py-spy") for n in names)
        assert any(n.endswith(".local/bin/py-spy") for n in names)

    def test_candidates_are_empty_on_windows(self, monkeypatch):
        # Those paths are POSIX locations, the binary is py-spy.exe, and X_OK is
        # meaningless on Windows -- so the probe is skipped entirely there.
        monkeypatch.setattr(perf_sampler.platform_compat, "IS_WINDOWS", True)
        assert perf_sampler.pyspy_candidates() == ()

    def test_windows_discovery_falls_through_to_which(self, monkeypatch):
        monkeypatch.setattr(perf_sampler.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(perf_sampler.shutil, "which", lambda _n: r"C:\tools\py-spy.exe")
        assert perf_sampler.pyspy_path() == r"C:\tools\py-spy.exe"

    def test_attach_failure_hint_names_tracer_contention_not_just_privileges(self):
        """A refusal on Linux is more often "already traced" than "need sudo".

        The desktop app attaches py-spy to the gateway to capture a frozen stack
        before SIGKILL, and ptrace allows one tracer per process -- so the hint has
        to name that, and say the wedge capture wins.
        """
        hint = perf_sampler.pyspy_attach_failure_hint()
        assert "ptrace" in hint
        assert "task_for_pid" in hint
        # Names the competing capture and tells the operator to retry rather than
        # to defeat the diagnostic that exists for the freeze.
        assert "wedged" in hint or "freeze" in hint
        assert "re-run" in hint

    def test_cli_prints_the_attach_hint_on_failure(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        monkeypatch.setattr(cli_perf, "pyspy_path", lambda: "/usr/bin/py-spy")
        monkeypatch.setattr(cli_perf, "pyspy_argv", lambda **_kw: ["/bin/false"])

        class _Failed:
            returncode = 1
            stdout = ""
            stderr = "Permission denied"

        monkeypatch.setattr(cli_perf.subprocess, "run", lambda *_a, **_k: _Failed())
        args = _sample_args(pid=4321, output=tmp_path / "p.folded")
        assert cli_perf._sample_out_of_process(args, pid=4321) == 1
        assert "ptrace" in capsys.readouterr().err

    def test_cli_reports_missing_pyspy_instead_of_failing_silently(
        self, monkeypatch, capsys, tmp_path
    ):
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        monkeypatch.setattr(cli_perf, "pyspy_path", lambda: None)
        args = _sample_args(pid=4321, output=tmp_path / "p.folded")
        assert cli_perf._sample_out_of_process(args, pid=4321) == 3
        assert "py-spy" in capsys.readouterr().err


# ── CLI behaviour ──


class TestCliSample:
    @pytest.fixture
    def probe_module(self):
        """Install an importable module exposing the workloads --call needs.

        The test file itself is not importable as ``test.test_perf_sampler`` (the
        test directory is not a package), so a synthetic module gives --call a
        real import target without depending on collection layout.
        """
        import sys
        import types

        module = types.ModuleType("_kc_perf_probe")
        module.work = _recognisable_workload  # type: ignore[attr-defined]
        module.boom = _raises_after_work  # type: ignore[attr-defined]
        sys.modules["_kc_perf_probe"] = module
        try:
            yield "_kc_perf_probe"
        finally:
            sys.modules.pop("_kc_perf_probe", None)

    def test_in_process_run_writes_a_private_artifact(self, monkeypatch, tmp_path, probe_module):
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        out = tmp_path / "nested" / "p.folded"
        args = _sample_args(perf_call=f"{probe_module}:work", interval=0.002, output=out)
        assert cli_perf._perf_sample(args) == 0
        assert out.exists()
        assert "_recognisable_workload" in out.read_text(encoding="utf-8")
        if os.name == "posix":
            # Profiles name code paths from the user's machine; keep them owner-only.
            assert out.stat().st_mode & 0o077 == 0

    def test_rejects_bad_interval_before_sampling(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        args = _sample_args(interval=99.0, output=tmp_path / "p.folded")
        assert cli_perf._perf_sample(args) == 2
        assert "--interval" in capsys.readouterr().err
        assert not (tmp_path / "p.folded").exists()

    def test_rejects_out_of_range_seconds(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        args = _sample_args(seconds=99999, output=tmp_path / "p.folded")
        assert cli_perf._perf_sample(args) == 2
        assert "--seconds" in capsys.readouterr().err

    def test_unresolvable_call_is_reported(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        args = _sample_args(perf_call="no_such_module_xyz:fn", output=tmp_path / "p.folded")
        assert cli_perf._perf_sample(args) == 2
        assert "--call" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "raised",
        [RuntimeError("import guard failed"), KeyError("missing"), SystemExit(3)],
    )
    def test_import_time_exception_is_reported_not_tracebacked(
        self, monkeypatch, capsys, tmp_path, raised
    ):
        """Resolving runs the target module's top-level code, which can raise anything.

        A fixed (ImportError, AttributeError, TypeError, ValueError) tuple let a
        RuntimeError from a failed import guard -- or a module calling sys.exit --
        escape as an uncaught traceback.
        """
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")

        def _boom(_spec):
            raise raised

        monkeypatch.setattr(cli_perf, "_resolve_callable", _boom)
        args = _sample_args(perf_call="mod:fn", output=tmp_path / "p.folded")
        assert cli_perf._perf_sample(args) == 2
        err = capsys.readouterr().err
        assert "Cannot resolve --call" in err
        # The exception type is named so the failure stays diagnosable.
        assert type(raised).__name__ in err

    def test_keyboard_interrupt_during_resolution_still_propagates(
        self, monkeypatch, tmp_path
    ):
        # An operator interrupting the command must still interrupt it; the broad
        # handler deliberately does not swallow KeyboardInterrupt.
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")

        def _interrupt(_spec):
            raise KeyboardInterrupt

        monkeypatch.setattr(cli_perf, "_resolve_callable", _interrupt)
        args = _sample_args(perf_call="mod:fn", output=tmp_path / "p.folded")
        with pytest.raises(KeyboardInterrupt):
            cli_perf._perf_sample(args)

    def test_raising_call_still_emits_the_profile(self, monkeypatch, tmp_path, probe_module):
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        out = tmp_path / "p.folded"
        args = _sample_args(perf_call=f"{probe_module}:boom", interval=0.002, output=out)
        # The profile of a call that failed is often the point, so samples are kept.
        assert cli_perf._perf_sample(args) == 0
        assert out.exists()

    def test_no_pid_and_no_call_explains_both_routes(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        monkeypatch.setattr(cli_perf, "_read_gateway_pid", lambda: None)
        args = _sample_args(output=tmp_path / "p.folded")
        assert cli_perf._perf_sample(args) == 2
        err = capsys.readouterr().err
        assert "--pid" in err and "--call" in err


def _raises_after_work() -> None:
    _recognisable_workload()
    raise RuntimeError("boom")


# ── Gateway PID resolution ──


class TestGatewayPid:
    def test_reads_the_recorded_pid_when_the_lock_is_held(self, monkeypatch, tmp_path):
        (tmp_path / "gateway.lock").write_text("4242\n", encoding="utf-8")
        monkeypatch.setattr(cli_perf, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(cli_perf, "_gateway_lock_is_held", lambda _p: True)
        monkeypatch.setattr(cli_perf.platform_compat, "pid_exists", lambda _p: True)
        assert cli_perf._read_gateway_pid() == 4242

    def test_stale_pid_is_rejected_when_no_gateway_holds_the_lock(self, monkeypatch, tmp_path):
        """A stopped gateway leaves its pid behind and pids get reused.

        Without the held-lock check, the default target would be whatever process
        inherited that pid, and the profile would be labelled as the gateway's.
        """
        (tmp_path / "gateway.lock").write_text("4242\n", encoding="utf-8")
        monkeypatch.setattr(cli_perf, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(cli_perf, "_gateway_lock_is_held", lambda _p: False)
        # pid_exists is pinned True so the held-lock check is the ONLY thing that
        # can reject. Without this the real pid 4242 is absent on the test host and
        # the assertion passes even with the lock gate removed (a vacuous test).
        monkeypatch.setattr(cli_perf.platform_compat, "pid_exists", lambda _p: True)
        assert cli_perf._read_gateway_pid() is None

    def test_dead_pid_is_rejected_even_if_the_lock_looks_held(self, monkeypatch, tmp_path):
        (tmp_path / "gateway.lock").write_text("4242\n", encoding="utf-8")
        monkeypatch.setattr(cli_perf, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(cli_perf, "_gateway_lock_is_held", lambda _p: True)
        monkeypatch.setattr(cli_perf.platform_compat, "pid_exists", lambda _p: False)
        assert cli_perf._read_gateway_pid() is None

    def test_missing_file_is_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cli_perf, "config_dir", lambda: tmp_path)
        assert cli_perf._read_gateway_pid() is None

    @pytest.mark.parametrize("content", ["", "not-a-pid", "0\n", "-5\n"])
    def test_unusable_content_is_none(self, monkeypatch, tmp_path, content):
        (tmp_path / "gateway.lock").write_text(content, encoding="utf-8")
        monkeypatch.setattr(cli_perf, "config_dir", lambda: tmp_path)
        assert cli_perf._read_gateway_pid() is None

    def test_absent_lock_file_is_not_held(self, tmp_path):
        assert cli_perf._gateway_lock_is_held(tmp_path / "nope.lock") is False

    def test_unheld_lock_probe_releases_and_reports_free(self, tmp_path):
        # An unlocked file must probe as free, and the probe must not leave it
        # locked (a real gateway starting right after must still be able to take it).
        lock = tmp_path / "gateway.lock"
        lock.write_text("1\n", encoding="utf-8")
        assert cli_perf._gateway_lock_is_held(lock) is False
        assert cli_perf._gateway_lock_is_held(lock) is False


class TestArtifactWriteFailure:
    def test_symlinked_output_does_not_write_through_the_link(self, tmp_path):
        """A planted symlink at the output path must not have its target clobbered.

        The default --output is relative, so it usually lands in the directory the
        operator ran from -- often a repo checkout. write_text() follows a symlink
        and would truncate + chmod the linked file; atomic_write renames over the
        link instead.
        """
        victim = tmp_path / "victim.txt"
        victim.write_text("precious", encoding="utf-8")
        link = tmp_path / "profile.folded"
        link.symlink_to(victim)

        assert cli_perf._write_artifact(link, "profile-data") is True

        # The link was replaced by a real file holding our content...
        assert link.is_symlink() is False
        assert link.read_text(encoding="utf-8") == "profile-data"
        # ...and the target it pointed at is untouched.
        assert victim.read_text(encoding="utf-8") == "precious"

    def test_written_artifact_is_owner_only(self, tmp_path):
        out = tmp_path / "p.folded"
        assert cli_perf._write_artifact(out, "x") is True
        if os.name == "posix":
            assert out.stat().st_mode & 0o077 == 0

    def test_unwritable_output_returns_false_not_raises(self, capsys, tmp_path):
        # A directory where a file is expected: the write raises OSError.
        target = tmp_path / "adir"
        target.mkdir()
        assert cli_perf._write_artifact(target, "x") is False
        assert "Could not write the profile" in capsys.readouterr().err

    def test_cli_exits_nonzero_instead_of_tracebacking(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        import sys as _sys
        import types

        module = types.ModuleType("_kc_perf_probe_w")
        module.work = _recognisable_workload  # type: ignore[attr-defined]
        _sys.modules["_kc_perf_probe_w"] = module
        try:
            unwritable = tmp_path / "adir"
            unwritable.mkdir()
            args = _sample_args(
                perf_call="_kc_perf_probe_w:work", interval=0.002, output=unwritable
            )
            # Sampling succeeded; only the write failed, so this must be a clean
            # nonzero exit with a diagnostic rather than an uncaught OSError.
            assert cli_perf._perf_sample(args) == 6
            assert "Could not write the profile" in capsys.readouterr().err
        finally:
            _sys.modules.pop("_kc_perf_probe_w", None)


class TestPySpyPathShortening:
    def test_absolute_posix_paths_are_shortened(self):
        raw = 'thread;run (/home/someone/proj/pkg/mod.py:12);inner (/usr/lib/py/x.py:3) 7'
        out = perf_sampler.shorten_frame_paths(raw)
        assert "someone" not in out
        assert "pkg/mod.py:12" in out
        assert "py/x.py:3" in out

    def test_windows_paths_are_shortened(self):
        raw = "thread;run (C:\\Users\\someone\\proj\\pkg\\mod.py:12) 4"
        out = perf_sampler.shorten_frame_paths(raw)
        assert "someone" not in out
        assert "pkg/mod.py:12" in out

    def test_relative_paths_and_counts_are_untouched(self):
        raw = "fn (pkg/mod.py:1);other (pkg/two.py:9) 42"
        assert perf_sampler.shorten_frame_paths(raw) == raw

    def test_pyspy_output_is_shortened_before_being_written(self, monkeypatch, tmp_path):
        """The documented path-shortening guarantee must hold for BOTH strategies.

        It previously lived only in _frame_label (in-process), so a py-spy profile
        still carried the operator's home directory.
        """
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        out = tmp_path / "p.folded"
        monkeypatch.setattr(cli_perf, "pyspy_path", lambda: "/usr/bin/py-spy")

        # py-spy is handed a staged path; emulate it writing there.
        def _fake_argv(**kw):
            Path(kw["output"]).write_text(
                "t;run (/home/someone/proj/pkg/mod.py:12) 5\n", encoding="utf-8"
            )
            return ["/bin/true"]

        monkeypatch.setattr(cli_perf, "pyspy_argv", _fake_argv)

        class _Done:
            returncode = 0
            stdout = ""
            stderr = ""

        monkeypatch.setattr(cli_perf.subprocess, "run", lambda *_a, **_k: _Done())
        args = _sample_args(pid=999, output=out)
        assert cli_perf._sample_out_of_process(args, pid=999) == 0
        written = out.read_text(encoding="utf-8")
        assert "someone" not in written
        assert "pkg/mod.py:12" in written

    def test_pyspy_is_never_pointed_at_the_caller_supplied_output(self, monkeypatch, tmp_path):
        """py-spy opens its output path directly, so it must not receive --output.

        A symlink at --output would otherwise have its target truncated by py-spy
        before this code ever sanitizes and re-writes.
        """
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        out = tmp_path / "p.folded"
        seen: dict[str, Path] = {}
        monkeypatch.setattr(cli_perf, "pyspy_path", lambda: "/usr/bin/py-spy")

        def _capture(**kw):
            seen["output"] = Path(kw["output"])
            Path(kw["output"]).write_text("t;fn (pkg/m.py:1) 1\n", encoding="utf-8")
            return ["/bin/true"]

        monkeypatch.setattr(cli_perf, "pyspy_argv", _capture)

        class _Done:
            returncode = 0
            stdout = ""
            stderr = ""

        monkeypatch.setattr(cli_perf.subprocess, "run", lambda *_a, **_k: _Done())
        assert cli_perf._sample_out_of_process(_sample_args(pid=7, output=out), pid=7) == 0
        assert seen["output"] != out
        # The staged file lived in a temp dir and is cleaned up afterwards.
        assert not seen["output"].exists()

    def test_symlinked_output_target_survives_the_pyspy_path(self, monkeypatch, tmp_path):
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        victim = tmp_path / "victim.txt"
        victim.write_text("precious", encoding="utf-8")
        link = tmp_path / "p.folded"
        link.symlink_to(victim)
        monkeypatch.setattr(cli_perf, "pyspy_path", lambda: "/usr/bin/py-spy")

        def _argv(**kw):
            Path(kw["output"]).write_text("t;fn (pkg/m.py:1) 1\n", encoding="utf-8")
            return ["/bin/true"]

        monkeypatch.setattr(cli_perf, "pyspy_argv", _argv)

        class _Done:
            returncode = 0
            stdout = ""
            stderr = ""

        monkeypatch.setattr(cli_perf.subprocess, "run", lambda *_a, **_k: _Done())
        assert cli_perf._sample_out_of_process(_sample_args(pid=7, output=link), pid=7) == 0
        assert victim.read_text(encoding="utf-8") == "precious"
        assert link.is_symlink() is False

    def test_unlaunchable_binary_returns_exit_code_not_traceback(
        self, monkeypatch, capsys, tmp_path
    ):
        """A discovered path can still be unlaunchable (wrong arch, bad shebang)."""
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        monkeypatch.setattr(cli_perf, "pyspy_path", lambda: "/usr/bin/py-spy")
        monkeypatch.setattr(cli_perf, "pyspy_argv", lambda **_kw: ["/nonexistent/py-spy"])

        def _raise(*_a, **_k):
            raise OSError(8, "Exec format error")

        monkeypatch.setattr(cli_perf.subprocess, "run", _raise)
        args = _sample_args(pid=7, output=tmp_path / "p.folded")
        assert cli_perf._sample_out_of_process(args, pid=7) == 3
        assert "Could not run py-spy" in capsys.readouterr().err

    def test_candidates_degrade_when_home_is_unresolvable(self, monkeypatch):
        """An unresolvable home must degrade, not raise out of a discovery helper.

        Path.home() raises with no HOME and no passwd entry (systemd units, slim
        containers).

        IS_WINDOWS is pinned False so this exercises the POSIX branch on every
        platform: the candidate list is empty on Windows by design, so asserting a
        non-empty result would fail there for a reason unrelated to this
        behaviour. The seam is perf_sampler._home_dir rather than
        pathlib.Path.home, so pathlib is not mutated process-wide.
        """
        monkeypatch.setattr(perf_sampler.platform_compat, "IS_WINDOWS", False)

        def _no_home():
            raise RuntimeError("Could not determine home directory")

        monkeypatch.setattr(perf_sampler, "_home_dir", _no_home)
        candidates = perf_sampler.pyspy_candidates()
        assert candidates  # absolute entries still offered
        assert all("cargo" not in str(c) and ".local" not in str(c) for c in candidates)

    def test_candidates_are_empty_on_windows_regardless_of_home(self, monkeypatch):
        """The POSIX probe is gated off on Windows, so home never matters there."""
        monkeypatch.setattr(perf_sampler.platform_compat, "IS_WINDOWS", True)

        def _no_home():
            raise AssertionError("home must not be consulted on Windows")

        monkeypatch.setattr(perf_sampler, "_home_dir", _no_home)
        assert perf_sampler.pyspy_candidates() == ()


class TestShortenPathsWithSpaces:
    """A home directory containing a space must still be stripped.

    The whole point of shortening is dropping the home prefix (hence the
    username); a pattern that only matched the tail after the last space left
    that prefix in the artifact.
    """

    def test_posix_home_with_a_space_is_fully_stripped(self):
        text = "thread;run (/home/Jane Doe/proj/pkg/mod.py:12) 5\n"
        out = perf_sampler.shorten_frame_paths(text)
        assert "Jane" not in out
        assert "Doe" not in out
        assert "/home" not in out
        assert "pkg/mod.py:12" in out

    def test_windows_home_with_a_space_is_fully_stripped(self):
        text = "thread;run (C:\\Users\\Jane Doe\\proj\\pkg\\mod.py:12) 5\n"
        out = perf_sampler.shorten_frame_paths(text)
        assert "Jane" not in out
        assert "Users" not in out
        assert "pkg/mod.py:12" in out

    def test_the_regression_shape_is_gone(self):
        """Lock the exact corrupted output the old pattern produced."""
        out = perf_sampler.shorten_frame_paths("(/home/Jane Doe/proj/pkg/mod.py:12)")
        assert "Doepkg" not in out

    def test_multiple_dots_in_the_filename_still_match(self):
        out = perf_sampler.shorten_frame_paths("(/home/Jane Doe/pkg/mod.test.py:3)")
        assert "Jane" not in out
        assert "pkg/mod.test.py:3" in out

    def test_shortening_is_idempotent(self):
        """Two passes now run, so re-shortening must not corrupt the result."""
        once = perf_sampler.shorten_frame_paths("(/home/Jane Doe/proj/pkg/mod.py:12) 5")
        assert perf_sampler.shorten_frame_paths(once) == once

    def test_relative_paths_and_prose_are_left_alone(self):
        for text in ("(see notes.py)", "pkg/mod.py:1", "thread;fn (pkg/m.py:2) 1"):
            assert perf_sampler.shorten_frame_paths(text) == text

    def test_a_path_without_a_line_number_is_still_shortened(self):
        out = perf_sampler.shorten_frame_paths("(/home/Jane Doe/pkg/mod.py)")
        assert out == "(pkg/mod.py)"


class TestCallExitsCleanly:
    def test_systemexit_from_the_call_still_writes_the_profile(self, monkeypatch, tmp_path):
        """CLI entry points exit via sys.exit on their NORMAL path.

        Catching only Exception meant profiling the most natural targets produced
        no artifact at all.
        """
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
        out = tmp_path / "p.folded"

        def _exits():
            # Sleep rather than burn CPU: the sampler walks sys._current_frames()
            # on a wall-clock interval and a sleeping thread is still sampled, so
            # this collects samples deterministically instead of racing the
            # interval under parallel test load.
            time.sleep(0.3)
            raise SystemExit(1)

        monkeypatch.setattr(cli_perf, "_resolve_callable", lambda _spec: _exits)
        args = _sample_args(output=out, perf_call="mod:fn")
        args.pid = None
        args.interval = 0.005
        rc = cli_perf._sample_in_process(args)
        assert rc == 0, "SystemExit must not discard the collected samples"
        assert out.exists() and out.read_text(encoding="utf-8").strip()

    def test_keyboardinterrupt_from_the_call_still_propagates(self, monkeypatch, tmp_path):
        """Ctrl-C must abort, not yield a partial profile as if it succeeded."""
        monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")

        def _interrupted():
            raise KeyboardInterrupt

        monkeypatch.setattr(cli_perf, "_resolve_callable", lambda _spec: _interrupted)
        args = _sample_args(output=tmp_path / "p.folded", perf_call="mod:fn")
        args.pid = None
        with pytest.raises(KeyboardInterrupt):
            cli_perf._sample_in_process(args)


class TestResolveCallable:
    @pytest.mark.parametrize("spec", ["nocolon", ":fn", "mod:", ""])
    def test_rejects_malformed_specs(self, spec):
        with pytest.raises(ValueError):
            cli_perf._resolve_callable(spec)

    def test_rejects_non_callable(self):
        with pytest.raises(TypeError):
            cli_perf._resolve_callable("kiro_crew.perf_sampler:DEBUG_ENV_VAR")

    def test_resolves_a_dotted_attribute(self):
        resolved = cli_perf._resolve_callable("kiro_crew.perf_sampler:StackSampler.start")
        assert callable(resolved)
