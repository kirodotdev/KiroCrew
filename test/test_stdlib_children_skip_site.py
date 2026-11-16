"""Stdlib-only Python children of the gateway start without ``site``.

``-I`` ignores PYTHONPATH, the user site and the working directory, but it still
runs ``site``, so a ``.pth`` file in the interpreter's own site-packages executes
at start-up. These children import only the stdlib, so they also pass ``-S``.

Each test points ``sys.executable`` at a throwaway venv whose site-packages holds
a ``.pth`` hook, runs the real child, and checks both that the hook did not run and
that the child still did its job.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import sysconfig
import venv
from pathlib import Path

import pytest

from kiro_crew import kiro_prerequisite_process, page_cache, platform_compat
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS
from kiro_crew.validation_parts import json_schema_args

# One worker builds the venv once and runs every real-interpreter probe.
pytestmark = pytest.mark.xdist_group(name="stdlib_children_skip_site")


@pytest.fixture(scope="module")
def hooked_python(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, Path]:
    """A throwaway venv's python, and the marker its planted ``.pth`` writes."""
    root = tmp_path_factory.mktemp("hooked-venv")
    venv.EnvBuilder(with_pip=False, symlinks=platform_compat.IS_POSIX).create(root / "v")
    python = str(root / "v" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python"))
    purelib = subprocess.run(
        [python, "-I", "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=FALLBACK_TIMEOUT_SECS,
    ).stdout.strip()
    assert Path(purelib).is_relative_to(root), (purelib, sysconfig.get_paths()["purelib"])
    marker = root / "hook-ran"
    Path(purelib, "zz_hook.pth").write_text(
        f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')\n", encoding="utf-8"
    )
    # The hook is live: a plain -I start runs it.
    subprocess.run([python, "-I", "-c", "pass"], check=True, timeout=FALLBACK_TIMEOUT_SECS)
    assert marker.exists()
    marker.unlink()
    return python, marker


@pytest.fixture
def hooked(hooked_python: tuple[str, Path]) -> tuple[str, Path]:
    """:func:`hooked_python` with the marker cleared, so tests do not see each other's run."""
    hooked_python[1].unlink(missing_ok=True)
    return hooked_python


def test_pattern_child_skips_site(hooked, monkeypatch) -> None:
    python, marker = hooked
    monkeypatch.setattr(sys, "executable", python)

    assert json_schema_args._bounded_pattern_search("^a+$", "aaa") is True
    assert json_schema_args._bounded_pattern_search("^b", "aaa") is False
    assert not marker.exists()


def test_page_cache_child_skips_site(hooked, monkeypatch, tmp_path) -> None:
    python, marker = hooked
    monkeypatch.setattr(sys, "executable", python)
    target = tmp_path / "data.db"
    target.write_bytes(b"x" * 1024)
    real_run = subprocess.run
    results: list[subprocess.CompletedProcess] = []

    def recording_run(*args, **kwargs):
        results.append(real_run(*args, **kwargs))
        return results[-1]

    monkeypatch.setattr(subprocess, "run", recording_run)
    # The child's deadline stays under the test's own timeout.
    monkeypatch.setattr(page_cache, "_READ_TIMEOUT_SECS", FALLBACK_TIMEOUT_SECS)
    page_cache.read_now([target])

    assert [r.returncode for r in results] == [0]
    assert results[0].args[0] == python
    assert not marker.exists()


@pytest.mark.skipif(
    not kiro_prerequisite_process._PROCESS_GROUP_SUPERVISOR_CODE
    or not kiro_prerequisite_process._host_can_reap(),
    reason="POSIX process-group supervisor with reaping",
)
def test_supervised_oneshot_skips_site(hooked, monkeypatch) -> None:
    python, marker = hooked
    monkeypatch.setattr(sys, "executable", python)

    async def run() -> tuple[int, bytes]:
        proc = await kiro_prerequisite_process.spawn_supervised_oneshot(
            ["/bin/echo", "ok"], stdout=asyncio.subprocess.PIPE
        )
        out, _ = await proc.communicate()
        return proc.returncode or 0, out

    returncode, out = asyncio.run(run())

    assert (returncode, out) == (0, b"ok\n")
    assert not marker.exists()


@pytest.mark.skipif(
    not kiro_prerequisite_process._PROCESS_GROUP_SUPERVISOR_CODE or not platform_compat.IS_POSIX,
    reason="POSIX process-group supervisor",
)
def test_sandboxed_supervisor_flags_skip_site(hooked) -> None:
    """The sandboxed spawn's supervisor argv (pinned in test_kiro_prerequisite_sandbox)
    still runs its target with ``-I -S`` and its resource-limit flags."""
    python, marker = hooked
    completed = subprocess.run(  # noqa: S603
        [
            python,
            "-I",
            "-S",
            "-c",
            kiro_prerequisite_process._PROCESS_GROUP_SUPERVISOR_CODE,
            *kiro_prerequisite_process.resource_limit_supervisor_argv(),
            "/bin/echo",
            "ok",
        ],
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # the supervisor waits for its whole group
        capture_output=True,
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
    )

    assert (completed.returncode, completed.stdout) == (0, b"ok\n"), completed.stderr
    assert not marker.exists()
