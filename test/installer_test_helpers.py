"""Process-group-safe subprocess helper for the installer test suites.

The installer tests run the real ``cli.sh``, which probes interpreters and
package managers as grandchildren. ``subprocess.run(timeout=...)`` kills only
the process it spawned, and pytest-timeout's signal only unwinds the Python
frame, so a wedged grandchild survives either one: it is reparented to init and
keeps burning a core until someone kills it by hand. Running the child as a
session leader makes the whole tree killable as one process group.

Callers are POSIX-only (``cli.sh`` is POSIX shell) and skip on Windows before
reaching this module, which is what makes the ``killpg`` below safe.
"""

from __future__ import annotations

import os
import signal
import subprocess

#: A hermetic installer run (fake curl, fake package managers) finishes in
#: seconds, so this bound only ever trips on a wedge. It sits well under
#: pytest-timeout's ceiling so that this helper, rather than pytest-timeout, is
#: what reaps the tree.
INSTALLER_TIMEOUT = 60.0


def run_bounded(
    argv: list[str],
    env: dict[str, str],
    timeout: float = INSTALLER_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` in its own process group; on timeout kill the whole group.

    ``start_new_session`` makes the child a session leader, so its pid is also
    its process-group id and one ``killpg`` reaps every descendant. Re-raises
    ``subprocess.TimeoutExpired`` once the group is gone.
    """
    with subprocess.Popen(
        argv,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    ) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # Exited between the timeout and the kill; nothing to reap.
            proc.communicate()
            raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
