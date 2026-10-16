"""Run commands and dial guest workers in a sprite, through the Sprites API.

:class:`SpritesShell` is the :class:`~kiro_crew.coder.remote_worker.RemoteShell`
for a sprite: ``run`` is one exec WebSocket, so the install, start and stop
scripts Coder runs over ``coder ssh`` run here unchanged, with any secret on
stdin. :class:`SpritesWorkerLaunch` dials a guest worker through the Sprites TCP
proxy instead of a ``coder ssh`` child.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.coder.remote_worker import RemoteWorkerError, RemoteWorkerLaunch
from kiro_crew.sprites_session.client import SpritesClient, SpritesError


class SpritesShell:
    """``run`` through the Sprites exec API; no dial argv."""

    def __init__(self, client: SpritesClient, sprite: str) -> None:
        self.client = client
        self.sprite = sprite
        #: Display label for the environment card.
        self.workspace = sprite
        #: Vault secret name holding the token, for the terminal bridge.
        self.token_secret = ""

    async def run(self, command: str, stdin: bytes = b"") -> bytes:
        try:
            code, out, err = await self.client.exec(self.sprite, ["/bin/sh", "-c", command], stdin)
        except SpritesError as exc:
            raise RemoteWorkerError(str(exc)) from exc
        if code:
            tail = err.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
            raise RemoteWorkerError(f"guest command failed (exit {code}): {tail[0][:200]}")
        return out

    def argv(self, command: str) -> list[str]:
        raise RemoteWorkerError("a sprite has no dial command; commands go through its API")


class SpritesWorkerLaunch(RemoteWorkerLaunch):
    """A remote session's guest workers in a sprite."""

    def __init__(
        self, session_id: str, shell: SpritesShell, secrets_dir: Any, **kwargs: Any
    ) -> None:
        super().__init__(session_id, shell, secrets_dir, **kwargs)
        self.sprites_shell = shell

    def dial(self, port: int) -> Any:
        shell = self.sprites_shell

        async def dial() -> Any:
            try:
                return await shell.client.proxy(shell.sprite, port)
            except SpritesError as exc:
                # The link treats a failed dial as a reason to back off and retry.
                raise ConnectionError(str(exc)) from exc

        return dial

    def dial_argv(self, port: int) -> list[str]:
        raise RemoteWorkerError("a sprite is dialled through its API proxy, not a command")
