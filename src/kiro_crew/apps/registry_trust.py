"""The keystone writer for ``registry_trust.json`` (operator ``owner`` grants).

This module owns the WRITE core and the ONE schema validator
(:func:`_owner_trusted_repos_from_record`) that both the strict reader here and
the tolerant runtime reader in ``apps/registry_pipeline/sources.py``
(:func:`~...sources._granted_owner_repos`, which reaches it through
:func:`read_registry_trust_strict`) validate against, so two callers can share it
without an import cycle:

- ``dashboard/handlers/security.py`` — the three ``/api/security/trusted-registries``
  endpoints (snapshot, grant, revoke).
- ``apps/registry_pipeline/sources.py`` — the clone-time reader.

``security.py`` cannot host the reader the ``apps`` layer needs, because it
top-level imports ``apps.routes`` (``app_lifecycle_lock``); a top-level import the
other way would be a cycle. Both callers instead import this leaf module.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps.registry import _REGISTRY_TRUST_VERSION, _same_git_target
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import registry_trust_path

logger = logging.getLogger(__name__)


class RegistryTrustCorruptError(Exception):
    """``registry_trust.json`` exists but is not a store this writer may mutate."""


def _refuse_keystone_alias() -> int:
    """Open the keystone refusing an alias, returning an OPEN descriptor (no decode).

    ``MS_RDONLY`` seals a MOUNT, not an inode, so the sandbox's read-only mount of
    ``registry_trust.json`` covers only the one path it was established on. A
    keystone that is a **symlink** (its name lives in the writable data home, so a
    sandboxed process can unlink it and drop a file of its own there) or a
    **regular file carrying a second hardlink** (the alias is a different path,
    outside the sealed mount, and a write through it changes the very inode the
    grant is read from) both survive that seal while resolving and reading as
    present. ``sandbox._warn_if_alias_backed`` only WARNS about these; the grant is
    refused HERE, where it is read, so a linked keystone confers no ``owner`` trust
    rather than being trusted with a note in the log.

    Opens through :func:`platform_compat.open_file_no_reparse`, which refuses a
    symlink or Windows reparse point at the final component in the SAME operation
    that opens it (no ``lstat``-then-open window), then ``fstat``s the descriptor
    the caller will read and refuses ``st_nlink > 1`` — so the inode checked is
    exactly the inode read. Returns the OPEN descriptor (the caller owns it and
    MUST close it) or ``-1`` when the keystone is absent, and raises
    :class:`RegistryTrustCorruptError` for a link, an extra hardlink, or a
    non-regular file. This is the alias refusal ALONE — no bytes are read, so an
    undecodable file passes it. :func:`_read_keystone_text_no_alias` calls it and
    then decodes.
    """
    path = registry_trust_path()
    try:
        # ``nonblocking`` so a FIFO planted at this name is rejected by the
        # ``S_ISREG`` check below instead of parking the executor thread on an
        # open that waits for a writer.
        fd = platform_compat.open_file_no_reparse(path, nonblocking=True)
    except FileNotFoundError:
        return -1
    except OSError as exc:
        # ELOOP here is the symlink/reparse-point refusal from the helper: the
        # keystone name points at another inode, so it is treated as corrupt
        # rather than followed to whatever it targets.
        logger.warning("registry_trust.json is not a real regular file; refusing it: %s", exc)
        raise RegistryTrustCorruptError(
            f"registry_trust.json is a symlink or otherwise not openable as a plain file: {exc}"
        ) from exc
    try:
        st = os.fstat(fd)
        if st.st_nlink > 1 or not stat.S_ISREG(st.st_mode):
            logger.warning(
                "registry_trust.json is alias-backed (nlink=%d) or not a regular file; "
                "refusing it so no registry gains owner trust through a second name",
                st.st_nlink,
            )
            raise RegistryTrustCorruptError(
                "registry_trust.json is hardlinked or not a regular file, so it is not a "
                "store this reader may trust"
            )
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_keystone_text_no_alias() -> str | None:
    """Read the keystone's bytes, refusing an inode reachable under a second name.

    Refuses an alias through :func:`_refuse_keystone_alias` (a symlink, an extra
    hardlink, or a non-regular file), then DECODES the descriptor it returns.
    Returns the file text, ``None`` when the keystone is absent, and raises
    :class:`RegistryTrustCorruptError` for the alias cases the refusal names or for
    non-UTF-8 content.
    """
    fd = _refuse_keystone_alias()
    if fd < 0:
        return None
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        try:
            return handle.read()
        except UnicodeDecodeError as exc:
            # No product writer emits non-UTF-8 here (json.dumps is ASCII and the
            # atomic writer encodes UTF-8), so undecodable bytes are corruption
            # like malformed JSON is: fail closed with the same error the callers
            # already turn into "no grants in force" / a 500 "corrupt".
            raise RegistryTrustCorruptError("registry_trust.json is not valid UTF-8 text") from exc


def _owner_trusted_repos_from_record(version: Any, owner_trusted: Any) -> list[str] | None:
    """The repo URLs a version-1 ``owner_trusted`` record names, or ``None``.

    Version 1 stores a JSON LIST of credential-free repo URLs. Returns ``None``
    for any other version, or for a version-1 ``owner_trusted`` that is not a list,
    so the caller can refuse it as corrupt; the members are returned RAW (not
    filtered) so a strict reader can decide whether a malformed entry is fatal
    while a tolerant reader drops it. This is the ONE schema validator both the
    strict reader here and the tolerant runtime reader
    (``sources._granted_owner_repos``, which reaches it through
    :func:`read_registry_trust_strict`) share, so they cannot disagree about what a
    valid store is.
    """
    if version == _REGISTRY_TRUST_VERSION:
        return list(owner_trusted) if isinstance(owner_trusted, list) else None
    return None


def read_registry_trust_strict() -> dict:
    """Read ``registry_trust.json`` for a MUTATION: raise on corrupt, empty if absent.

    Returns a NORMALISED record ``{"version": <current>, "owner_trusted": [<repos>]}``
    — the version-1 list shape the grant/revoke writers mutate.

    An empty ``{}`` document is the ABSENT-store case, not a corrupt one: the
    sandbox pre-creates this keystone as ``{}``
    (``sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES``), so it is treated exactly
    like a missing file (the versioned-empty store) and the first grant lands
    instead of 500ing on the version check. A NON-empty document is held to the
    schema: only a version-1 document whose ``owner_trusted`` is a list parses;
    an unknown version, a dict-shaped ``owner_trusted``, or any other shape is
    refused as :class:`RegistryTrustCorruptError`.

    The read goes through :func:`_read_keystone_text_no_alias`, so a symlinked or
    hardlinked keystone is refused as corrupt before its bytes are parsed: the
    grant/revoke writers reach this read first, so a linked file cannot be mutated
    in place, and the operator is told to remove the alias rather than have this
    writer silently replace an aliased inode.
    """
    try:
        raw = _read_keystone_text_no_alias()
    except OSError as exc:
        raise RegistryTrustCorruptError(f"registry_trust.json unreadable: {exc}") from exc
    if raw is None:
        return {"version": _REGISTRY_TRUST_VERSION, "owner_trusted": []}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RegistryTrustCorruptError(f"registry_trust.json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RegistryTrustCorruptError("registry_trust.json top level is not a JSON object")
    if not data:
        return {"version": _REGISTRY_TRUST_VERSION, "owner_trusted": []}
    # Only the current version parses; anything else is corrupt. A version-1
    # ``owner_trusted`` must be a list of repos.
    version = data.get("version")
    repos = _owner_trusted_repos_from_record(version, data.get("owner_trusted"))
    if repos is None:
        raise RegistryTrustCorruptError(
            "registry_trust.json has an unknown version or the wrong owner_trusted shape"
        )
    return {"version": _REGISTRY_TRUST_VERSION, "owner_trusted": repos}


def _owner_trusted_list(data: dict) -> list:
    """The mutable ``owner_trusted`` LIST of a strict-read record.

    :func:`read_registry_trust_strict` always normalises ``owner_trusted`` to a
    list (version 1), so grant/revoke mutate this one shape whatever the on-disk
    version was. Defensive against a caller that hands in an unexpected value.
    """
    owner_trusted = data.get("owner_trusted")
    if not isinstance(owner_trusted, list):
        owner_trusted = []
        data["owner_trusted"] = owner_trusted
    return owner_trusted


def add_owner_grant(data: dict, repo: str) -> None:
    """Add *repo* to a strict-read record's ``owner_trusted`` list, deduped by target.

    Removes any existing entry that names the same git target first (so a grant is
    idempotent and the key is stored exactly once), then appends *repo*. The grant
    is the value itself — version 1 stores no per-repo record body, because SEL
    timestamps each grant and no reader consumed the old ``{}``.
    """
    owner_trusted = _owner_trusted_list(data)
    kept = [r for r in owner_trusted if not (isinstance(r, str) and _same_git_target(r, repo))]
    kept.append(repo)
    data["owner_trusted"] = kept


def drop_owner_grants(data: dict, repo: str) -> None:
    """Drop every ``owner_trusted`` entry naming *repo*'s git target (idempotent)."""
    owner_trusted = _owner_trusted_list(data)
    data["owner_trusted"] = [
        r for r in owner_trusted if not (isinstance(r, str) and _same_git_target(r, repo))
    ]


def _write_registry_trust_record(data: dict) -> None:
    """Serialise a strict-read record and atomically write it owner-only.

    The concrete write both keystone mutators share: the grant and revoke handlers
    each read the store strictly, apply :func:`add_owner_grant` /
    :func:`drop_owner_grants` to the record, then hand that DATA here to publish
    it. It takes the record, not a callback, so the read-modify-write shape is the
    same on both paths and the whole transaction runs in the caller's one locked
    executor step. The file is owner-only on every write (0600 / owner DACL,
    applied to the temp file before any content reaches it).
    """
    atomic_write(registry_trust_path(), json.dumps(data, indent=2) + "\n", restrict_to_owner=True)
