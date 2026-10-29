"""Prepare a session's sprite: the same seam :class:`CoderSessionEnvironments` offers.

A sprite is a persistent VM that goes cold when idle (a cold sprite bills storage
only), so a session keeps one sprite for its life. The lifecycle -- when a sprite
is created, kept or deleted -- is :class:`~kiro_crew.coder.persistent_vm.PersistentVmEnvironments`;
this module is the Sprites API side of it.

Security properties:

* The Sprites API token lives in the gateway's encrypted vault, named by the
  profile's ``token_secret``. It is read per call, sent only to the Sprites API,
  and never reaches the sprite, a log, a config file or the agent.
* The gateway only ever acts on a sprite it recorded creating, under a name it
  minted (``kirocrew-`` plus 16 random hex digits).
* The guest worker's per-session secret travels on exec stdin, never in a URL.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any, Callable, Optional

from kiro_crew.coder.persistent_vm import PersistentVmEnvironments, SessionResource
from kiro_crew.config.session_environments import (
    SPRITES_TOKEN_SECRET_DEFAULT,
    VM_REGISTRY_FILES,
)
from kiro_crew.session_environment import SessionEnvironmentUnavailable
from kiro_crew.sprites_session.client import SpritesClient
from kiro_crew.sprites_session.shell import SpritesShell, SpritesWorkerLaunch

PROVIDER_ID = "sprites"
NAME_PREFIX = "kirocrew-"


def _vault_token(secret_name: str) -> str:
    from kiro_crew.config.paths import config_dir
    from kiro_crew.secrets.vault import SecretVault

    value = SecretVault(config_dir()).get(secret_name)
    return value.reveal() if value is not None else ""


def _fingerprint(token: str) -> str:
    """A truncated SHA-256 of an API token ("" for none); tells two accounts'
    tokens apart. The token itself is never kept, and an API token is
    high-entropy, so the fingerprint reveals nothing usable."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:32] if token else ""


class SpritesSessionEnvironments(PersistentVmEnvironments):
    """Create, reuse and delete sprites for the saved Sprites profiles."""

    profiles_key = "sprites_profiles"
    label = "Sprites"
    name_prefix = NAME_PREFIX
    binding_prefix = "sp-"
    registry_file = VM_REGISTRY_FILES[PROVIDER_ID]
    needs_credential = True

    def __init__(
        self,
        config: Any,
        data_home: Path,
        *,
        token_reader: Optional[Callable[[str], str]] = None,
        base_url: Optional[str] = None,
    ) -> None:
        super().__init__(config, data_home)
        self._token_reader = token_reader
        self._base_url = base_url

    async def client(self, profile: Optional[dict[str, Any]]) -> SpritesClient:
        return (await self._client_and_id(profile))[0]

    async def _client_and_id(self, profile: Optional[dict[str, Any]]) -> tuple[SpritesClient, str]:
        """A client and the fingerprint of the one token it was built from."""
        if profile is None:
            # A removed profile cannot name its token; the row waits for it.
            raise SessionEnvironmentUnavailable(
                "this sprite's Sprites profile is no longer configured; restore the original "
                "profile and its token secret in Settings > Session Environments"
            )
        name = str(profile.get("token_secret") or "")
        reader = self._token_reader or _vault_token
        token = await asyncio.to_thread(reader, name) if name else ""
        if not token:
            raise SessionEnvironmentUnavailable(
                f"no Sprites API token: save one as the secret "
                f"{name or SPRITES_TOKEN_SECRET_DEFAULT} in Settings > Secrets"
            )
        kwargs = {"base_url": self._base_url} if self._base_url else {}
        return SpritesClient(token, **kwargs), _fingerprint(token)

    async def check_ready(self, profile: dict[str, Any]) -> None:
        await self.client(profile)

    async def remote_get(
        self, profile: Optional[dict[str, Any]], name: str
    ) -> tuple[Optional[dict[str, Any]], str]:
        client, used = await self._client_and_id(profile)
        return await client.get_sprite(name), used

    async def remote_credential(self, profile: dict[str, Any]) -> str:
        return (await self._client_and_id(profile))[1]

    async def remote_create(
        self, profile: dict[str, Any], name: str, *, used: str
    ) -> dict[str, Any]:
        client, current = await self._client_and_id(profile)
        if current != used:
            # Another token was saved after the row recorded this one.
            raise SessionEnvironmentUnavailable(
                f"the Sprites API token changed before sprite {name} was created; "
                "nothing was created, start the chat again"
            )
        return await client.create_sprite(name)

    async def remote_delete(
        self, profile: Optional[dict[str, Any]], name: str, *, used: str
    ) -> None:
        client, current = await self._client_and_id(profile)
        if current != used:
            # Another token was saved after the identity check: its DELETE could
            # miss and read as gone. Keep the row; the next sweep checks again.
            raise SessionEnvironmentUnavailable(
                f"the Sprites API token for sprite {name} changed during deletion; "
                "nothing was deleted"
            )
        await client.delete_sprite(name)

    async def shell(self, profile: dict[str, Any], row: SessionResource) -> SpritesShell:
        client, current = await self._client_and_id(profile)
        if current != row.credential_id:
            # Another token was saved after the identity check: its exec and
            # proxy calls would reach whatever answers to this name there.
            raise SessionEnvironmentUnavailable(
                f"the Sprites API token for sprite {row.name} changed after its identity "
                "check; nothing was run on it"
            )
        shell = SpritesShell(client, row.name)
        # The terminal bridge reads the token itself, by this name, from the vault,
        # and checks the sprite's id under it before it connects.
        shell.token_secret = str(profile.get("token_secret") or "")
        shell.resource_id = row.resource_id
        return shell

    def launch(self, binding_id: str, shell: Any) -> SpritesWorkerLaunch:
        return SpritesWorkerLaunch(binding_id, shell)
