"""Windows teardown routing in ``kiro_crew.testing.harness``, asserted from every platform.

``IS_WINDOWS`` is patched, so the POSIX legs a contributor runs locally catch a
reroute back onto the pgid path before a Windows CI job does.
"""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import patch

from kiro_crew import platform_compat
from kiro_crew.testing.harness import _terminate_process_group


def test_terminate_routes_through_process_tree_kill_on_windows() -> None:
    """On Windows the teardown must use ``kill_process_tree``, not the pgid path.

    Asserted with ``IS_WINDOWS`` patched rather than only on a Windows runner:
    the POSIX legs are the ones a contributor runs locally, and a reroute that
    quietly went back to ``terminate_pgid`` would otherwise only surface as an
    ``AttributeError`` from ``os.getpgid`` inside a Windows CI job.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=platform_compat.IS_POSIX,
        creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
    )
    killed: list[int] = []
    try:
        with (
            patch("kiro_crew.testing.harness.platform_compat.IS_WINDOWS", True),
            patch(
                "kiro_crew.testing.harness.platform_compat.kill_process_tree",
                side_effect=lambda pid, sig=None: killed.append(pid) or True,
            ),
            patch("kiro_crew.testing.harness.terminate_pgid") as pgid,
            patch("kiro_crew.testing.harness.TERMINATE_GRACE_SECONDS", 0.2),
        ):
            _terminate_process_group(proc)
        assert killed == [proc.pid], "Windows teardown did not kill the process tree"
        assert not pgid.called, "Windows teardown reached the POSIX pgid primitive"
    finally:
        proc.kill()
        proc.wait(timeout=30)


def test_terminate_on_windows_survives_a_failed_tree_kill() -> None:
    """A ``taskkill`` failure is logged, not raised.

    Teardown runs in a ``finally``: letting a protected descendant or a
    transient access denial propagate would convert a PASSING test into an error
    whose traceback names the harness rather than the test.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=platform_compat.IS_POSIX,
        creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        with (
            patch("kiro_crew.testing.harness.platform_compat.IS_WINDOWS", True),
            patch(
                "kiro_crew.testing.harness.platform_compat.kill_process_tree",
                side_effect=PermissionError("taskkill rc=5 access denied"),
            ),
            patch("kiro_crew.testing.harness.TERMINATE_GRACE_SECONDS", 0.2),
        ):
            swept = _terminate_process_group(proc)
        # The root outlived the failed kill, so the tree is NOT reported gone:
        # the caller must keep the gateway's HOME.
        assert swept is False
    finally:
        proc.kill()
        proc.wait(timeout=30)
