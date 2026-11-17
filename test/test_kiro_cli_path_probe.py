"""A kiro-cli found only through ``PATH`` is versioned inside the sandbox, off the spawn path.

``PATH`` can lead with a directory an agent writes to, so the probe never has
more reach than the session's own launch of the same file: the readiness
probe's own runner in strict mode, its ``--version`` environment, nothing run
when the wrap is not Kiro Crew's own sandbox, nothing run on Windows. Every
failure is "unknown". The caller never waits: a miss answers ``None`` and starts
the probe in the background. Only the file's own answer is kept.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from kiro_crew import kiro_cli, kiro_cli_path_probe, kiro_prerequisite, platform_compat
from kiro_crew.kiro_prerequisite_process import ProcessResult
from kiro_crew.testing.ids import UNALLOCATABLE_PID

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="Windows has no OS sandbox backend")

CAP = kiro_prerequisite._MAX_CAPTURED_OUTPUT

# The line the sandbox launcher itself prints when a host refuses its first mount.
_LAUNCHER_REFUSED = (
    "sandbox: BLOCKED -- making mount propagation private on / failed: errno 13 "
    "(Permission denied).\n"
)
# A launcher exit that is no sandbox verdict, so its trusted re-run sets no
# ``sandbox_failure``: still the launcher's line, not the file's answer.
_LAUNCHER_FATAL = "sandbox: FATAL — cannot enter the launch directory (Device or resource busy)\n"


def _install(tmp_path: Path, name: str = "kiro-cli") -> str:
    path = tmp_path / "pathdir" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("executable")
    path.chmod(0o755)
    return str(path)


class _Threads:
    """Stand-in for ``threading.Thread``: records the work and runs it when told."""

    def __init__(self) -> None:
        self.pending: list = []

    def __call__(self, *, target, args=(), name="", daemon=False):
        assert daemon is True
        self.pending.append((target, args))
        return self

    def start(self) -> None:
        return None

    def run_all(self) -> None:
        pending, self.pending = self.pending, []
        for target, args in pending:
            target(*args)


@pytest.fixture
def threads(monkeypatch: pytest.MonkeyPatch) -> _Threads:
    fake = _Threads()
    monkeypatch.setattr(kiro_cli_path_probe.threading, "Thread", fake)
    return fake


# Captured at import, before the conftest fixture stubs them for every test.
_REAL_SANDBOXED_VERSION = kiro_cli_path_probe._sandboxed_version
_REAL_WARM = kiro_cli_path_probe.warm


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> dict:
    """The real ``_sandboxed_version`` over a recorded ``_run_process``."""
    calls: dict = {
        "runs": [],
        "result": ProcessResult(ok=True, output="kiro-cli 3.2.1\n", returncode=0),
    }

    async def _run(command: str, args: list[str], **kwargs: Any) -> ProcessResult:
        calls["runs"].append({"argv": [command, *args], **kwargs})
        return calls["result"]

    monkeypatch.setattr(kiro_prerequisite, "_run_process", _run)
    monkeypatch.setattr(
        kiro_cli_path_probe, "_sandboxed_version", _REAL_SANDBOXED_VERSION, raising=True
    )
    return calls


class _Once:
    """Hand the event loop back once, as a pipe read with nothing buffered does."""

    def __await__(self):
        yield


class _Pipe:
    """A child's pipe: 4096 bytes per read, for *reads* reads or without end."""

    def __init__(self, reads: int = 0, *, endless: bool = False) -> None:
        self._left, self._endless = reads, endless

    async def read(self, _size: int) -> bytes:
        await _Once()
        if not self._endless:
            if self._left <= 0:
                return b""
            self._left -= 1
        return b"x" * 4096


class _Child:
    """A planted file as the runner sees it: its pipes, and whether it ever exits."""

    pid = UNALLOCATABLE_PID

    def __init__(self, stdout: _Pipe, *, exits: bool) -> None:
        self.stdout, self.stderr = stdout, _Pipe()
        self.returncode: int | None = None
        self._exits = exits

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None

    async def wait(self) -> int:
        if not self._exits:
            await asyncio.Event().wait()  # never set: only the runner's teardown ends this wait
        self.returncode = 0
        return 0


@pytest.fixture
def planted(monkeypatch: pytest.MonkeyPatch) -> dict:
    """The real probe over the real runner; only the sandbox wrap and the child are fakes."""
    seen: dict = {"child": None, "signals": [], "results": []}
    real_run = kiro_prerequisite._run_process

    async def _prepare(argv: list[str], **_kwargs: Any) -> tuple[list[str], dict[str, str], None]:
        return ["/bin/sh", "/run/kirocrew_sandbox_probe.sh", *argv], {}, None

    async def _spawn(*_argv: str, **_kwargs: Any) -> _Child:
        return seen["child"]

    async def _kill_tree(pid: int, signal_number: int) -> None:
        seen["signals"].append((pid, signal_number))

    async def _run(command: str, args: list[str], **kwargs: Any) -> ProcessResult:
        result = await real_run(command, args, **kwargs)
        seen["results"].append(result)
        return result

    monkeypatch.setattr(kiro_prerequisite, "_prepare_sandboxed_spawn", _prepare)
    monkeypatch.setattr(kiro_prerequisite, "_TERMINATION_GRACE_SECS", 0)
    monkeypatch.setattr(kiro_prerequisite, "_run_process", _run)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    monkeypatch.setattr(platform_compat, "kill_process_tree_async", _kill_tree)
    monkeypatch.setattr(
        kiro_cli_path_probe, "_sandboxed_version", _REAL_SANDBOXED_VERSION, raising=True
    )
    return seen


class TestNeverWaitedFor:
    def test_a_miss_answers_unknown_and_starts_one_probe(self, tmp_path, threads, monkeypatch):
        binary = _install(tmp_path)
        monkeypatch.setattr(kiro_cli_path_probe, "_sandboxed_version", lambda _b: (3, 2, 1))

        assert kiro_cli_path_probe.version_if_known(binary) is None
        assert kiro_cli_path_probe.version_if_known(binary) is None
        assert len(threads.pending) == 1

        threads.run_all()
        assert kiro_cli_path_probe.version_if_known(binary) == (3, 2, 1)
        assert threads.pending == []

    def test_an_unknown_answer_is_kept_and_not_probed_again(self, tmp_path, threads, monkeypatch):
        binary = _install(tmp_path)
        probed: list[str] = []
        monkeypatch.setattr(
            kiro_cli_path_probe, "_sandboxed_version", lambda b: probed.append(b) or None
        )

        kiro_cli_path_probe.version_if_known(binary)
        threads.run_all()

        assert kiro_cli_path_probe.version_if_known(binary) is None
        assert threads.pending == []
        assert probed == [binary]

    @pytest.mark.parametrize(
        "raised",
        [OSError("sandbox unavailable"), kiro_cli_path_probe._NoAnswer()],
        ids=["raises", "not-run-or-cut-off"],
    )
    def test_a_read_with_no_answer_is_unknown_and_asked_again(
        self, raised, tmp_path, threads, monkeypatch
    ):
        """A slow host at boot must not cost the install its deferral until restart."""
        binary = _install(tmp_path)
        outcomes = iter([raised, (3, 2, 1)])

        def _probe(_binary):
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(kiro_cli_path_probe, "_sandboxed_version", _probe)
        kiro_cli_path_probe.version_if_known(binary)
        threads.run_all()

        assert kiro_cli_path_probe.version_if_known(binary) is None
        assert len(threads.pending) == 1, "nothing was kept, so the next caller asks again"
        threads.run_all()
        assert kiro_cli_path_probe.version_if_known(binary) == (3, 2, 1)
        assert threads.pending == []

    @posix_only
    def test_a_timed_out_read_is_not_kept(self, tmp_path, threads, runner):
        """The whole route: a ``--version`` the runner cut off leaves no answer."""
        binary = _install(tmp_path)
        runner["result"] = ProcessResult(ok=False, timed_out=True, error="process timed out")
        kiro_cli_path_probe.version_if_known(binary)
        threads.run_all()

        runner["result"] = ProcessResult(ok=True, output="kiro-cli 3.2.1\n", returncode=0)
        assert kiro_cli_path_probe.version_if_known(binary) is None
        threads.run_all()
        assert kiro_cli_path_probe.version_if_known(binary) == (3, 2, 1)
        assert len(runner["runs"]) == 2

    def test_a_replaced_file_is_read_again(self, tmp_path, threads, monkeypatch):
        binary = _install(tmp_path)
        answers = iter([(3, 2, 1), (2, 20, 0)])
        monkeypatch.setattr(kiro_cli_path_probe, "_sandboxed_version", lambda _b: next(answers))
        kiro_cli_path_probe.version_if_known(binary)
        threads.run_all()
        assert kiro_cli_path_probe.version_if_known(binary) == (3, 2, 1)

        stat = os.stat(binary)
        os.utime(binary, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

        assert kiro_cli_path_probe.version_if_known(binary) is None
        threads.run_all()
        assert kiro_cli_path_probe.version_if_known(binary) == (2, 20, 0)

    def test_a_missing_file_is_unknown_and_starts_nothing(self, tmp_path, threads):
        assert kiro_cli_path_probe.version_if_known(str(tmp_path / "gone")) is None
        assert threads.pending == []


class TestVersionNow:
    """``kirocrew doctor`` is one blocking run whose process never warms the cache."""

    def test_a_miss_is_read_inline_and_kept(self, tmp_path, threads, monkeypatch):
        binary = _install(tmp_path)
        probed: list[str] = []
        monkeypatch.setattr(
            kiro_cli_path_probe, "_sandboxed_version", lambda b: probed.append(b) or (3, 0, 0)
        )

        assert kiro_cli_path_probe.version_now(binary) == (3, 0, 0)
        assert threads.pending == [], "read on the caller's thread, not in the background"
        assert kiro_cli_path_probe.version_if_known(binary) == (3, 0, 0)
        assert kiro_cli_path_probe.version_now(binary) == (3, 0, 0)
        assert probed == [binary]

    def test_a_read_with_no_answer_is_unknown_and_not_kept(self, tmp_path, threads, monkeypatch):
        binary = _install(tmp_path)

        def _probe(_binary):
            raise kiro_cli_path_probe._NoAnswer

        monkeypatch.setattr(kiro_cli_path_probe, "_sandboxed_version", _probe)
        assert kiro_cli_path_probe.version_now(binary) is None
        assert kiro_cli_path_probe._versions == {}

    def test_a_missing_file_is_unknown(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            kiro_cli_path_probe, "_sandboxed_version", lambda _b: pytest.fail("nothing to run")
        )
        assert kiro_cli_path_probe.version_now(str(tmp_path / "gone")) is None


class TestWarm:
    def test_it_probes_the_path_only_install(self, tmp_path, threads, monkeypatch):
        binary = _install(tmp_path)
        monkeypatch.setattr(kiro_cli, "path_only_install", lambda: binary)
        monkeypatch.setattr(kiro_cli_path_probe, "_sandboxed_version", lambda _b: (3, 0, 0))

        _REAL_WARM()
        threads.run_all()  # the lookup
        threads.run_all()  # the probe it started

        assert kiro_cli_path_probe.version_if_known(binary) == (3, 0, 0)

    @pytest.mark.parametrize("install", [None, "raises"], ids=["no-path-install", "lookup-fails"])
    def test_it_starts_no_probe_without_one(self, install, threads, monkeypatch):
        def _lookup():
            if install == "raises":
                raise OSError("unreadable home")
            return None

        monkeypatch.setattr(kiro_cli, "path_only_install", _lookup)
        _REAL_WARM()
        threads.run_all()

        assert threads.pending == []

    def test_the_gateway_starts_it_at_boot(self, monkeypatch):
        """Without this call a first session finds no answer and keeps the full list."""
        from kiro_crew.agent_sdk import kas_floor_wait
        from kiro_crew.slack import gateway

        warmed: list[bool] = []
        monkeypatch.setattr(kiro_cli_path_probe, "warm", lambda: warmed.append(True))
        monkeypatch.setattr(gateway, "apply_kas_cli_floor", lambda cfg, **kwargs: None)
        monkeypatch.setattr(gateway, "_KAS_CLI_FLOOR_TASK", None)
        monkeypatch.setattr(kas_floor_wait, "_armed", None)

        async def _boot() -> None:
            await gateway._start_kas_cli_floor(cast(Any, object()))

        asyncio.run(_boot())

        assert warmed == [True]


@posix_only
class TestTheProbeItself:
    def test_it_runs_one_file_through_the_strict_runner(self, tmp_path, runner):
        binary = _install(tmp_path)

        assert kiro_cli_path_probe._sandboxed_version(binary) == (3, 2, 1)

        (run,) = runner["runs"]
        assert run["argv"] == [binary, "--version"]
        assert run["sandbox_mode"] == "strict"
        assert run["require_crew_sandbox"] is True
        assert run["timeout_secs"] == kiro_cli._VERSION_PROBE_TIMEOUT_SECS
        assert not run.get("first_party_fixed_argv"), "a PATH file is nobody's first party"

    def test_its_environment_is_the_version_allowlist(self, tmp_path, runner, monkeypatch):
        for name in (
            "KIRO_API_KEY",
            "AWS_SECRET_ACCESS_KEY",
            "SLACK_BOT_TOKEN",
            "SSH_AUTH_SOCK",
            "HTTPS_PROXY",
            "ANTHROPIC_API_KEY",
        ):
            monkeypatch.setenv(name, "secret-value")
        monkeypatch.setenv("HOME", str(tmp_path))

        kiro_cli_path_probe._sandboxed_version(_install(tmp_path))

        env = runner["runs"][0]["env"]
        assert set(env) <= kiro_prerequisite._PROBE_ENV_KEYS | {"NO_COLOR", "TERM"}
        assert "secret-value" not in env.values()
        assert env["HOME"] == str(tmp_path)

    def test_the_version_is_read_from_its_own_line(self, tmp_path, runner):
        """The runner hands stdout and stderr back as one text."""
        runner["result"] = ProcessResult(
            ok=True, output="warning: config is old\nkiro-cli 3.2.1\ndone\n", returncode=0
        )

        assert kiro_cli_path_probe._sandboxed_version(_install(tmp_path)) == (3, 2, 1)

    @pytest.mark.parametrize(
        "result",
        [
            ProcessResult(ok=False, output="kiro-cli 3.2.1\n", returncode=1),
            ProcessResult(ok=True, output="no version here\n", returncode=0),
            ProcessResult(ok=True, returncode=0),
        ],
        ids=["non-zero-exit", "unparsable", "silent"],
    )
    def test_a_file_that_exits_without_a_version_answers_none(self, result, tmp_path, runner):
        runner["result"] = result

        assert kiro_cli_path_probe._sandboxed_version(_install(tmp_path)) is None

    @pytest.mark.parametrize(
        "result",
        [
            ProcessResult(ok=False, error=kiro_prerequisite._NOT_IN_CREW_SANDBOX_ERROR),
            ProcessResult(ok=False, error="sandbox wrapper is unavailable: bwrap"),
            ProcessResult(ok=False, timed_out=True, error="process timed out"),
            ProcessResult(ok=False, returncode=-9, error="process exited with code -9"),
            ProcessResult(ok=False, returncode=1, output=_LAUNCHER_REFUSED),
            ProcessResult(ok=False, returncode=1, output=_LAUNCHER_FATAL),
            ProcessResult(
                ok=False, returncode=1, sandbox_failure=("no_backend", "mount refused", "")
            ),
            ProcessResult(ok=True, output="kiro-cli 3.2.1\n" + "x" * CAP, returncode=0),
        ],
        ids=[
            "not-in-crew-sandbox",
            "no-wrapper",
            "timeout",
            "wrapper-killed-by-signal",
            "launcher-refusal-line",
            "launcher-fatal-line",
            "sandbox-refused",
            "output-at-the-cap",
        ],
    )
    def test_a_run_that_is_not_the_files_own_whole_answer_is_no_answer(
        self, result, tmp_path, runner
    ):
        runner["result"] = result

        with pytest.raises(kiro_cli_path_probe._NoAnswer):
            kiro_cli_path_probe._sandboxed_version(_install(tmp_path))


@posix_only
class TestAPlantedFileThatFloodsItsOutput:
    """The file is an agent's to write, so what it prints is bounded by the runner."""

    def test_more_output_than_the_cap_is_bounded_unknown_and_not_kept(
        self, tmp_path, threads, planted
    ):
        binary = _install(tmp_path)
        planted["child"] = _Child(_Pipe(reads=4 * CAP // 4096), exits=True)

        kiro_cli_path_probe.version_if_known(binary)
        threads.run_all()

        (result,) = planted["results"]
        assert len(result.output) == CAP, "four times the cap was printed; the cap was kept"
        assert kiro_cli_path_probe.version_if_known(binary) is None
        assert len(threads.pending) == 1, "nothing was kept, so the next caller asks again"

    def test_output_without_end_is_cut_off_and_the_tree_is_ended(
        self, tmp_path, threads, planted, monkeypatch
    ):
        binary = _install(tmp_path)
        planted["child"] = _Child(_Pipe(endless=True), exits=False)
        # No time is allowed, so the cut-off does not wait on the wall clock.
        monkeypatch.setattr(kiro_cli, "_VERSION_PROBE_TIMEOUT_SECS", 0)

        kiro_cli_path_probe.version_if_known(binary)
        threads.run_all()

        (result,) = planted["results"]
        assert result.timed_out is True
        assert len(result.output) <= CAP
        assert planted["signals"] == [
            (UNALLOCATABLE_PID, platform_compat.SIGTERM),
            (UNALLOCATABLE_PID, platform_compat.SIGKILL),
        ]
        assert kiro_cli_path_probe.version_if_known(binary) is None
        assert len(threads.pending) == 1, "nothing was kept, so the next caller asks again"


def test_windows_runs_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(kiro_cli_path_probe.sys, "platform", "win32")

    async def _never(*_a: Any, **_k: Any) -> ProcessResult:
        pytest.fail("Windows has no OS sandbox backend: the probe must not run")

    monkeypatch.setattr(kiro_prerequisite, "_run_process", _never)

    assert _REAL_SANDBOXED_VERSION(_install(tmp_path)) is None


@pytest.mark.parametrize(
    ("platform", "said", "not_said"),
    [
        ("win32", "its version is not read on Windows", "sandbox"),
        ("linux", "it gave no version inside the sandbox", "Windows"),
    ],
)
def test_the_log_says_why_there_is_no_version(
    platform, said, not_said, tmp_path, threads, runner, monkeypatch, caplog
):
    """Windows runs nothing, so its line must not speak of a sandbox it does not have."""
    monkeypatch.setattr(kiro_cli_path_probe.sys, "platform", platform)
    runner["result"] = ProcessResult(ok=True, output="no version here\n", returncode=0)
    caplog.set_level("INFO", logger=kiro_cli_path_probe.__name__)

    kiro_cli_path_probe.version_if_known(_install(tmp_path))
    threads.run_all()

    assert len(runner["runs"]) == (0 if platform == "win32" else 1)
    (line,) = [r.getMessage() for r in caplog.records if r.name == kiro_cli_path_probe.__name__]
    assert said in line and not_said not in line
    assert "stay out of Tool Search deferral" in line
