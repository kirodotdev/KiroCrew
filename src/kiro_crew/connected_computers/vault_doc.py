"""One JSON document kept as a single entry of the local :class:`SecretVault`.

Connected-computer state is kept in the vault, not in a plain file under the data
home: the vault directory is a keystone leaf the agent sandbox hides, so an agent
on either machine can neither read a connector credential nor write itself a
grant, a device record or a lifted revocation. Writes are compare-and-swap on the
entry's revision (:meth:`SecretVault.set_if_revision_sync`), retried a bounded
number of times, so two writers never lose each other's change.

Unreadable state fails closed: :class:`VaultDocError`, never an empty document.

The vault's key is a file beside it, so this alone is NOT OS-backed protection.
Secrets kept here are sealed again under a key the OS holds
(:mod:`.local_store`, :mod:`.protected_store`); the remote gateway's device
records hold no secret (a fingerprint and a generation only).
"""

from __future__ import annotations

import json
from typing import Any, Callable, Final, TypeVar

T = TypeVar("T")

#: Version of every document written here.
DOC_VERSION: Final[int] = 1
#: Compare-and-swap attempts before a write gives up.
_CAS_ATTEMPTS: Final[int] = 8
#: Largest document accepted (it holds at most a few dozen small records).
MAX_DOC_BYTES: Final[int] = 256 * 1024


class VaultDocError(RuntimeError):
    """The document cannot be read, is malformed, or could not be written."""


class VaultJsonDoc:
    """``name`` in ``vault`` as a ``{"version": 1, ...}`` JSON object."""

    def __init__(self, vault: Any, name: str) -> None:
        self._vault = vault
        self.name = name

    def _load(self) -> tuple[dict[str, Any], str | None]:
        try:
            found = self._vault.get_with_revision(self.name)
        except Exception as exc:  # noqa: BLE001 -- a corrupt vault must not read as empty
            raise VaultDocError("connected computers state is unreadable") from exc
        if found is None:
            return {"version": DOC_VERSION}, None
        value, revision = found
        text = value.reveal()
        if len(text) > MAX_DOC_BYTES:
            raise VaultDocError("connected computers state is too large")
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            raise VaultDocError("connected computers state is unreadable") from exc
        if not isinstance(doc, dict) or doc.get("version") != DOC_VERSION:
            raise VaultDocError("connected computers state is invalid")
        return doc, revision

    def read(self) -> dict[str, Any]:
        """The document, or ``{"version": 1}`` when it was never written."""
        return self._load()[0]

    def update(self, mutate: Callable[[dict[str, Any]], T]) -> T:
        """Apply ``mutate`` (which edits the document in place) and write it back.

        ``mutate`` may run more than once when another writer wins the race, so
        it must be a pure function of the document it is given.
        """
        for _attempt in range(_CAS_ATTEMPTS):
            doc, revision = self._load()
            result = mutate(doc)
            doc["version"] = DOC_VERSION
            text = json.dumps(doc, sort_keys=True, separators=(",", ":"))
            if len(text) > MAX_DOC_BYTES:
                raise VaultDocError("connected computers state is too large")
            try:
                written = self._vault.set_if_revision_sync(self.name, text, revision)
            except Exception as exc:  # noqa: BLE001 -- surfaced as one error class
                raise VaultDocError("connected computers state could not be saved") from exc
            if written is not None:
                return result
        raise VaultDocError("connected computers state kept changing; try again")


def open_vault() -> Any:
    """This machine's vault (``<config dir>/.vault``)."""
    from kiro_crew.config.paths import config_dir
    from kiro_crew.secrets.vault import SecretVault

    return SecretVault(config_dir())
