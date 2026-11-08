"""Prepare a session's boxd machine: the same seam :class:`CoderSessionEnvironments` offers.

A boxd machine is persistent and hibernates when idle (it then bills for disk
only), so a session keeps one machine for its life. The lifecycle -- when a
machine is created, kept or deleted -- is
:class:`~kiro_crew.session_environments.persistent_vm.PersistentVmEnvironments`; this module is
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

from kiro_crew.boxd_session.cli import BoxdCLI, BoxdError
from kiro_crew.boxd_session.shell import BoxdShell
from kiro_crew.config.session_environments import VM_REGISTRY_FILES
from kiro_crew.session_environment import SessionEnvironmentUnavailable
from kiro_crew.session_environments.persistent_vm import PersistentVmEnvironments, SessionResource

PROVIDER_ID = "boxd"
NAME_PREFIX = "kc-"


class BoxdSessionEnvironments(PersistentVmEnvironments):
    """Create, reuse and delete boxd machines for the saved boxd profiles."""

    profiles_key = "boxd_profiles"
    label = "boxd"
    name_prefix = NAME_PREFIX
    binding_prefix = "bx-"
    registry_file = VM_REGISTRY_FILES[PROVIDER_ID]

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
    ) -> tuple[Optional[dict[str, Any]], str]:
        # The gateway's own CLI login. Which account and org answered is read
        # first: under another one a miss says nothing about a deletion.
        cli = await self.cli()
        try:
            who = await cli.identity()
        except BoxdError:
            who = ""
        record = await cli.get(name)
        if record is None and not who:
            # Unknown account: never read a miss as a deletion.
            raise SessionEnvironmentUnavailable(
                f"boxd machine {name} was not found, and the boxd account this computer is "
                "signed in to could not be read; nothing was deleted"
            )
        if record is None:
            # A miss counts only under the account sampled before it: one that
            # changed (or went unreadable) during the lookup says nothing.
            try:
                after = await cli.identity()
            except BoxdError:
                after = ""
            if after != who:
                raise SessionEnvironmentUnavailable(
                    f"the boxd account or org changed while machine {name} was looked up; "
                    "nothing was deleted"
                )
        return record, who

    async def remote_credential(self, profile: dict[str, Any]) -> str:
        return await (await self.cli()).identity()

    async def remote_create(
        self, profile: dict[str, Any], name: str, *, used: str
    ) -> dict[str, Any]:
        cli = await self.cli()
        if await cli.identity() != used:
            # Another account or org since the row recorded this one.
            raise SessionEnvironmentUnavailable(
                f"the boxd account or org changed before machine {name} was created; "
                "nothing was created, start the chat again"
            )
        return await cli.create(
            name,
            snapshot=str(profile.get("snapshot") or ""),
            vcpu=int(profile.get("vcpu") or 0),
        )

    async def remote_delete(
        self, profile: Optional[dict[str, Any]], name: str, *, used: str
    ) -> None:
        cli = await self.cli()
        if used and await cli.identity() != used:
            # Another account or org since the identity check: its machine of
            # this name is not this session's.
            raise SessionEnvironmentUnavailable(
                f"the boxd account or org for machine {name} changed during deletion; "
                "nothing was deleted"
            )
        await cli.remove(name)

    def credential_changed(self, row: SessionResource) -> str:
        return (
            f"this session's boxd machine {row.name} is not visible to the boxd account or "
            "org this computer is now signed in to, which is not the one it was created "
            "under; nothing was deleted. Sign in to that account (boxd auth login) or switch "
            "back to its org (boxd auth switch) to reopen the session"
        )

    async def remote_kept(self, profile: dict[str, Any], name: str) -> None:
        await (await self.cli()).set_delete_after(name, "off")

    async def remote_archived(self, profile: dict[str, Any], name: str, days: int) -> None:
        cli = await self.cli()
        # boxd's own timer is the backstop if this gateway never runs again.
        await cli.set_delete_after(name, f"{int(days)}d")
        await cli.hibernate(name)

    async def shell(self, profile: dict[str, Any], row: SessionResource) -> BoxdShell:
        return BoxdShell((await self.cli()).bin, row.name)
