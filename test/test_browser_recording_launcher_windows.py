"""On Windows, a timed-out driver behind a ``.cmd`` launcher takes its real child with it.

The one live check of ``record_browser._run_driver``'s tree kill: the launcher and
its interpreter child are started once for the module, in an xdist group of their
own, and every exit kills whatever the run left behind.
"""

from __future__ import annotations

import contextlib
import importlib.util
import subprocess
import sys
import threading
from pathlib import Path

import pytest

RUNNER = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "browser-recording"
    / "scripts"
    / "record_browser.py"
)

_RETURN_BOUND_SECS = 45

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="the launcher shape is Windows-only"),
    pytest.mark.xdist_group("browser_recording_live_launcher"),
]


@pytest.fixture(scope="module")
def timed_out_launcher(tmp_path_factory):
    """Run a ``node.cmd`` launcher whose child outlives the timeout; yield (error, pid file)."""
    from kiro_crew.platform_compat import SIGKILL, kill_pid_pinned

    spec = importlib.util.spec_from_file_location("record_browser_launcher", RUNNER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    root = tmp_path_factory.mktemp("launcher")
    pid_file = root / "node.pid"
    child = root / "child.py"
    # The child records its own pid and start identity, so cleanup kills only it.
    child.write_text(
        "import os, pathlib, sys, time\n"
        f"sys.path.insert(0, {str(RUNNER.parents[4])!r})\n"
        "from kiro_crew.platform_compat import get_process_start_id\n"
        "ident = get_process_start_id(os.getpid())\n"
        "pathlib.Path(sys.argv[1]).write_text(f'{os.getpid()}\\n{ident}', encoding='utf-8')\n"
        "time.sleep(300)\n",
        encoding="utf-8",
    )
    launcher = root / "node.cmd"
    launcher.write_text(f'@"{sys.executable}" -X utf8 "{child}" "{pid_file}"\r\n', encoding="utf-8")
    errors: list = []

    def call() -> None:
        try:
            mod._run_driver([str(launcher)], str(root), 10)
        except subprocess.TimeoutExpired as exc:
            errors.append(exc)

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    try:
        # The 10 s timeout plus the 5 s cleanup, with room for a slow host; the child
        # would hold the pipes for 300 s, so a call waiting on it is still running here.
        thread.join(timeout=_RETURN_BOUND_SECS)
        yield not thread.is_alive(), errors, pid_file
    finally:
        try:
            pid, _, ident = _read_pid_file(pid_file)
            if pid and ident and ident != "None":
                with contextlib.suppress(OSError):
                    kill_pid_pinned(int(pid), ident, SIGKILL)
        finally:
            thread.join(timeout=30)


def _read_pid_file(pid_file: Path) -> tuple[str, str, str]:
    """The child's ``(pid, "\\n", start identity)``, empty strings for what it has not written."""
    text = pid_file.read_text(encoding="utf-8") if pid_file.is_file() else ""
    return text.partition("\n")


def test_the_call_returns_within_its_bound(timed_out_launcher) -> None:
    returned, _, _ = timed_out_launcher
    assert returned, f"_run_driver still waiting {_RETURN_BOUND_SECS}s in; a pipe holder blocks it"


def test_the_call_raises_the_drivers_timeout(timed_out_launcher) -> None:
    _, errors, _ = timed_out_launcher
    assert len(errors) == 1 and errors[0].timeout == 10


def test_the_launchers_child_is_killed_with_the_tree(timed_out_launcher) -> None:
    from kiro_crew.platform_compat import pid_exists

    returned, _, pid_file = timed_out_launcher
    assert returned, "the call never returned, so the child was not killed by it"
    pid, _, _ = _read_pid_file(pid_file)
    assert pid, "the launcher's child never started"
    assert not pid_exists(int(pid))
