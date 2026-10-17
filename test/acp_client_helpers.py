"""Fixtures, markers and helpers shared by test_acp_client.py and its topic parts.

The topic parts are the ``test_acp_client_<topic>.py`` modules that import this one,
split by behaviour. An autouse fixture applies only in a module that imports it by
name, so each of them imports both autouse fixtures below.
"""

import asyncio
import sys

import pytest

from kiro_crew.acp.client import AcpClient

# Windows lacks os.killpg and POSIX process-tree APIs (ps, /proc).
# Tests that exercise these paths are skipped on Windows.
_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process tree APIs only")


# Separate from _POSIX_ONLY because the reason differs: these tests assert POSIX
# EXECUTABLE-RESOLUTION semantics, not process-tree APIs. They build fixtures that
# have no Windows equivalent — extensionless binaries made runnable with
# chmod(0o755), which `shutil.which` cannot find on Windows because it resolves
# candidates through PATHEXT, and `/`-rooted paths, which os.path.realpath()
# anchors to the current drive (`/srv/u/x` -> `D:\srv\u\x`). The production
# resolvers are correct on Windows; only these fixtures are POSIX-shaped.
_POSIX_EXEC_PATHS_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX executable-resolution semantics only"
)


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in a module that imports this fixture calls monkeypatch.undo()
def _pin_the_installed_kiro_cli_version(monkeypatch):
    """The writer gate in ``_write_derived_permissions`` reads
    ``installed_kiro_cli_version`` function-locally, and on a host with a kiro-cli
    installed that is one REAL ``kiro-cli --version`` spawn per binary identity --
    32 per full run of this file on a five-run hygiene sweep, from tests that drive
    protocol and process doubles. The version the gate sees is a property of the
    host, not of the client under test; ``None`` is the "cannot be established"
    branch every host without the binary already takes."""
    monkeypatch.setattr("kiro_crew.kiro_cli.installed_kiro_cli_version", lambda: None)


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in a module that imports this fixture calls monkeypatch.undo()
def _native_projection_for_fake_processes(monkeypatch):
    from kiro_crew.acp import skill_projection

    # These protocol/process doubles do not own installed native agent specs.
    # The projection contract is exercised in test_native_skill_projection.
    monkeypatch.setattr(
        skill_projection,
        "prepare_native_skill_projection",
        lambda work_dir, **_kwargs: skill_projection.NativeSkillProjection(
            {"kirocrew": "kirocrew"}
        ),
    )


async def _stop_stderr_drain(client: "AcpClient") -> None:
    """Cancel and await the background stderr-drain task a mocked _spawn started.

    _spawn starts _drain_stderr over self._process.stderr, and a mock process has
    a truthy stderr, so a test that spawns over a mock and never stops the client
    leaves that task alive past its own teardown. When the loop later collects it,
    its exception (the mock stream's readline/decode is not a real coroutine) is
    reported against whatever unrelated test happened to trigger the collection.
    Cancelling without awaiting is not enough: the task must be awaited so the
    loop retrieves the result, per testing-conventions.md Determinism rule 3.
    """
    task = client._stderr_task
    if task is not None:
        if not task.done():
            task.cancel()
        # Await regardless of state so the loop retrieves the result: a mock
        # stream makes the task fault on its first readline, so it may already
        # be done here, and a done task with an unretrieved exception is exactly
        # the leak this guards against.
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    client._stderr_task = None
