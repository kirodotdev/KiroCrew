"""Commands and the worker dial in a boxd machine, through ``boxd machine exec``.

:class:`BoxdShell` is the :class:`~kiro_crew.coder.remote_worker.RemoteShell`
for a machine. ``boxd machine exec`` forwards stdin and stdout as streams and
the remote exit code as its own, exactly like ``coder ssh``, so
:class:`~kiro_crew.coder.remote_worker.RemoteWorkerLaunch` drives it unchanged:
the install, start and stop scripts run through :meth:`run`, and the dial to
the guest worker is the same ``worker.py attach`` command run through
:meth:`argv`. No SSH config or host key is involved.
"""

from __future__ import annotations

from kiro_crew.boxd_session.cli import check_machine_name
from kiro_crew.coder.remote_worker import _scrubbed_env, run_collect


class BoxdShell:
    def __init__(self, boxd_bin: str, machine: str) -> None:
        self.boxd_bin = boxd_bin
        self.machine = check_machine_name(machine)
        #: Display label for the environment card.
        self.workspace = machine

    def argv(self, command: str) -> list[str]:
        # sh -c keeps one shell string as one argv element on the far side.
        return [self.boxd_bin, "machine", "exec", self.machine, "--", "sh", "-c", command]

    def interactive_argv(self) -> list[str]:
        """An interactive shell for the dashboard terminal (needs the PTY it runs in)."""
        return [self.boxd_bin, "machine", "connect", self.machine]

    async def run(self, command: str, stdin: bytes = b"") -> bytes:
        return await run_collect(self.argv(command), stdin, _scrubbed_env())
