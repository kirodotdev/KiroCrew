"""The App Store's git children start with ``posix_spawn``, not ``fork``.

CPython holds the GIL for the whole ``fork()``, and on macOS that fork copies the
gateway's memory map, so a fork on a worker thread still stops the event loop.
Inside :func:`sandbox_rlimit.fork_free_spawns` the spawn helper starts its shim with
``posix_spawn`` (no copy), and the shim does the three jobs ``Popen`` would have
done in the forked child: a new session, the working directory, and closing
inherited descriptors. Outside that scope, children keep the fork start.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import platform_compat_offloop_spawn as offloop
from kiro_crew import sandbox_rlimit
from kiro_crew.apps.registry_pipeline import catalog
from kiro_crew.platform_compat_native import IS_WINDOWS
from kiro_crew.testing.wait import default_timeout, finished

pytestmark = [
    pytest.mark.skipif(IS_WINDOWS, reason="posix_spawn is the POSIX start"),
    pytest.mark.skipif(
        not getattr(subprocess, "_USE_POSIX_SPAWN", False),
        reason="this interpreter's subprocess never uses posix_spawn",
    ),
    # Each test starts its own interpreter child; one worker keeps them serial.
    pytest.mark.xdist_group(name="spawn_off_loop"),
]

# What the child reports about itself: its cwd, whether it leads its own session,
# and every descriptor above stderr it holds.
_REPORT = (
    "import json, os; "
    "fds = sorted(int(n) for n in os.listdir('/dev/fd') if int(n) > 2); "
    "print(json.dumps({'cwd': os.getcwd(), 'leader': os.getsid(0) == os.getpid(), 'fds': fds}))"
)


def _record_fork_starts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    forks: list[tuple[Any, ...]] = []
    real_fork_exec = subprocess._fork_exec  # type: ignore[attr-defined]

    def _fork_exec(*args: Any) -> Any:
        forks.append(args)
        return real_fork_exec(*args)

    monkeypatch.setattr(subprocess, "_fork_exec", _fork_exec)
    return forks


def _run_on_gateway_loop(body: Any) -> Any:
    with asyncio.Runner(loop_factory=offloop.gateway_loop_factory()) as runner:
        return runner.run(body())


async def _report(cwd: Path) -> tuple[int | None, dict[str, Any], bytes]:
    proc = await sandbox_rlimit.create_subprocess_limited(
        sys.executable,
        "-c",
        _REPORT,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    out, err = await finished(proc.communicate(), "the child's report")
    return proc.returncode, (json.loads(out) if out else {}), err


def test_a_fork_free_spawn_does_what_popen_did_in_the_fork(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    forks = _record_fork_starts(monkeypatch)
    # An inheritable descriptor the parent holds: close_fds=True never let it
    # through, so the posix_spawn start must not either.
    leak_r, leak_w = os.pipe()
    os.set_inheritable(leak_w, True)

    async def body() -> tuple[int | None, dict[str, Any], bytes]:
        with sandbox_rlimit.fork_free_spawns():
            return await _report(tmp_path)

    try:
        returncode, report, err = _run_on_gateway_loop(body)
    finally:
        os.close(leak_r)
        os.close(leak_w)

    assert returncode == 0, err
    assert forks == [], "the child was started with fork, not posix_spawn"
    assert Path(report["cwd"]).resolve() == tmp_path.resolve()
    assert report["leader"] is True
    assert leak_w not in report["fds"], "an inherited descriptor reached the command"


def test_outside_the_scope_children_keep_the_fork_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    forks = _record_fork_starts(monkeypatch)
    returncode, report, err = _run_on_gateway_loop(lambda: _report(tmp_path))
    assert returncode == 0, err
    assert len(forks) == 1
    assert report["leader"] is True


def test_the_scope_belongs_to_the_task_that_opened_it() -> None:
    seen: dict[str, bool] = {}

    async def inside() -> None:
        with sandbox_rlimit.fork_free_spawns():
            await asyncio.sleep(0)
            seen["inside"] = sandbox_rlimit._FORK_FREE_SPAWN.get()

    async def sibling() -> None:
        await asyncio.sleep(0)
        seen["sibling"] = sandbox_rlimit._FORK_FREE_SPAWN.get()

    async def body() -> None:
        await asyncio.gather(inside(), sibling())
        seen["after"] = sandbox_rlimit._FORK_FREE_SPAWN.get()

    asyncio.run(body())
    assert seen == {"inside": True, "sibling": False, "after": False}


def test_the_app_store_manifest_fan_out_spawns_fork_free() -> None:
    seen: list[bool] = []

    async def resolve(entry: object) -> object:
        seen.append(sandbox_rlimit._FORK_FREE_SPAWN.get())
        return entry

    result = asyncio.run(catalog._gather_bounded(["a", "b"], resolve))
    assert result == ["a", "b"]
    assert seen == [True, True]
    assert sandbox_rlimit._FORK_FREE_SPAWN.get() is False


@pytest.mark.parametrize(
    "kwargs",
    [{"pass_fds": (5,)}, {"process_group": 0}, {"umask": 0o22}, {"user": 1}],
    ids=["pass_fds", "process_group", "umask", "user"],
)
def test_a_spawn_needing_a_fork_only_option_keeps_the_fork_start(kwargs: dict[str, Any]) -> None:
    prefix = ("python", "-c", "shim", "--")
    before = dict(kwargs)
    assert sandbox_rlimit._fork_free_spawn_plan(prefix, kwargs) == prefix
    assert kwargs == before


def test_the_plan_hands_session_cwd_and_fd_closing_to_the_shim() -> None:
    kwargs: dict[str, Any] = {"start_new_session": True, "cwd": "/srv/x", "env": {}}
    prefix = sandbox_rlimit._fork_free_spawn_plan(("python", "-c", "shim", "--"), kwargs)
    assert prefix == ("python", "-c", "shim", "--setsid", "--chdir=/srv/x", "--close-fds", "--")
    assert kwargs == {"env": {}, "close_fds": False}


def test_the_plan_keeps_a_callers_close_fds_false_and_a_bytes_cwd() -> None:
    kwargs: dict[str, Any] = {"close_fds": False, "cwd": b"/srv/x"}
    prefix = sandbox_rlimit._fork_free_spawn_plan(("python", "-c", "shim", "--"), kwargs)
    assert prefix == ("python", "-c", "shim", "--chdir=/srv/x", "--")
    assert kwargs == {"close_fds": False}


def test_the_shim_fails_closed_on_a_working_directory_it_cannot_enter(tmp_path: Path) -> None:
    shim = Path(sandbox_rlimit.__file__).with_name("_spawn_exec_shim.py")
    result = subprocess.run(
        [sys.executable, str(shim), f"--chdir={tmp_path / 'missing'}", "--", "/bin/true"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=default_timeout(),
    )
    assert result.returncode == 127
    assert "cannot enter" in result.stderr
