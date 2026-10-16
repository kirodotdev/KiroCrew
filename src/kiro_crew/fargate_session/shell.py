"""Run commands and open streams in a Fargate task, through its bootstrap.

:class:`FargateShell` is the :class:`~kiro_crew.coder.remote_worker.RemoteShell`
for a task: ``run`` is the bootstrap's authenticated run request, so the same
install, start and stop scripts Coder runs over ``coder ssh`` run here unchanged.
:class:`FargateWorkerLaunch` is :class:`RemoteWorkerLaunch` with the dial
replaced: a guest worker is reached by asking the bootstrap to splice a forwarded
connection to the worker's loopback port, instead of a ``coder ssh`` child.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.coder.remote_worker import RemoteWorkerError, RemoteWorkerLaunch
from kiro_crew.fargate_session import bootstrap
from kiro_crew.session_environment import SessionEnvironmentUnavailable


class FargateShell:
    """``run`` through the task's bootstrap; no interactive shell or dial argv."""

    def __init__(self, connect: bootstrap.Connect, secret: bytes) -> None:
        self.connect = connect
        self.secret = secret
        #: Display label for the environment card (``task <id>``).
        self.workspace = ""

    async def pipe(self, port: int) -> Any:
        """A byte stream to the guest worker on ``port``, through the bootstrap."""
        return await bootstrap.pipe(self.connect, self.secret, port)

    async def run(self, command: str, stdin: bytes = b"") -> bytes:
        try:
            code, out, err = await bootstrap.run(self.connect, self.secret, command, stdin)
        except (bootstrap.BootstrapError, SessionEnvironmentUnavailable) as exc:
            raise RemoteWorkerError(str(exc)) from exc
        if code:
            tail = err.strip().splitlines()[-1:] or [""]
            raise RemoteWorkerError(f"guest command failed (exit {code}): {tail[0][:200]}")
        return out

    def argv(self, command: str) -> list[str]:
        raise RemoteWorkerError(
            "a Fargate task has no dial command; streams go through its bootstrap"
        )


class FargateWorkerLaunch(RemoteWorkerLaunch):
    """A remote session's guest workers in a Fargate task."""

    def __init__(
        self, session_id: str, shell: FargateShell, secrets_dir: Any, **kwargs: Any
    ) -> None:
        super().__init__(session_id, shell, secrets_dir, **kwargs)
        self.fargate_shell = shell

    def dial(self, port: int) -> Any:
        shell = self.fargate_shell

        async def dial() -> Any:
            try:
                return await shell.pipe(port)
            except (bootstrap.BootstrapError, SessionEnvironmentUnavailable) as exc:
                # The link treats a failed dial as a reason to back off and retry.
                raise ConnectionError(str(exc)) from exc

        return dial

    def dial_argv(self, port: int) -> list[str]:
        raise RemoteWorkerError("a Fargate task is dialled through its bootstrap, not a command")
