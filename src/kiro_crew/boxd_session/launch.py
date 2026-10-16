"""Prepare a session's boxd machine: the same seam :class:`CoderSessionEnvironments` offers.

A boxd machine is persistent and hibernates when idle (it then bills for disk
only), so a session keeps one machine for its life. The lifecycle -- when a
machine is created, kept or deleted -- is
:class:`~kiro_crew.coder.persistent_vm.PersistentVmEnvironments`; this module is
the boxd CLI side of it. An archived session's machine is hibernated, and boxd's
own ``delete-after`` timer is armed to the same deadline as a backstop, so the
machine goes even if this gateway never runs again.

Security properties:

* No credential is stored or read by Kiro Crew: the gateway machine's own
  ``boxd auth login`` authenticates every call, and nothing reaches the machine.
* The gateway only ever acts on a machine it recorded creating, under a name it
  minted (``kc-`` plus 16 random hex digits).
* The guest worker's per-session secret travels on exec stdin, never in argv.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from kiro_crew.boxd_session.cli import BoxdCLI
from kiro_crew.boxd_session.shell import BoxdShell
from kiro_crew.coder.persistent_vm import PersistentVmEnvironments

PROVIDER_ID = "boxd"
NAME_PREFIX = "kc-"


class BoxdSessionEnvironments(PersistentVmEnvironments):
    """Create, reuse and delete boxd machines for the saved boxd profiles."""

    profiles_key = "boxd_profiles"
    label = "boxd"
    name_prefix = NAME_PREFIX
    binding_prefix = "bx-"
    registry_file = "boxd/session-machines.json"

    def __init__(self, config: Any, data_home: Path, *, cli: Optional[BoxdCLI] = None) -> None:
        super().__init__(config, data_home)
        self._cli = cli
        self._cli_lock = asyncio.Lock()

    async def cli(self) -> BoxdCLI:
        """Cache executable discovery without probing PATH on the gateway loop."""
        async with self._cli_lock:
            if self._cli is None:
                self._cli = await asyncio.to_thread(BoxdCLI)
            return self._cli

    async def check_ready(self, profile: dict[str, Any]) -> None:
        await self.cli()  # the CLI is installed

    async def remote_get(
        self, profile: Optional[dict[str, Any]], name: str
    ) -> Optional[dict[str, Any]]:
        return await (await self.cli()).get(name)

    async def remote_create(self, profile: dict[str, Any], name: str) -> dict[str, Any]:
        return await (await self.cli()).create(
            name,
            snapshot=str(profile.get("snapshot") or ""),
            vcpu=int(profile.get("vcpu") or 0),
        )

    async def remote_delete(self, profile: Optional[dict[str, Any]], name: str) -> None:
        await (await self.cli()).remove(name)

    async def remote_kept(self, profile: dict[str, Any], name: str) -> None:
        await (await self.cli()).set_delete_after(name, "off")

    async def remote_archived(self, profile: dict[str, Any], name: str, days: int) -> None:
        cli = await self.cli()
        # boxd's own timer is the backstop if this gateway never runs again.
        await cli.set_delete_after(name, f"{int(days)}d")
        await cli.hibernate(name)

    async def shell(self, profile: dict[str, Any], name: str) -> BoxdShell:
        return BoxdShell((await self.cli()).bin, name)
