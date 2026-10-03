"""The keystone writer for ``registry_trust.json`` (operator ``owner`` grants).

This module owns the WRITE core and the ONE schema validator
(:func:`_owner_trusted_repos_from_record`) that both the strict reader here and
the tolerant runtime reader in ``apps/registry_pipeline/sources.py``
(:func:`~...sources._granted_owner_repos`, which reaches it through
:func:`read_registry_trust_strict`) validate against, so two callers can share it
without an import cycle:

- ``dashboard/handlers/security.py`` — the three ``/api/security/trusted-registries``
  endpoints (snapshot, grant, revoke).
- ``apps/routes.py`` — the ``PUT /api/apps/registries`` replace-all, which revokes
  grants for repos absent from the submitted list so a grant's lifetime equals the
  config row's lifetime.

``security.py`` cannot host the writer for the second caller, because it top-level
imports ``apps.routes`` (``app_lifecycle_lock``); a top-level import the other way
would be a cycle. Both callers instead import this leaf module. The shared config
lock (``dashboard.handlers.agents._get_config_lock``) is imported in-function so
this module stays a leaf — ``agents`` imports from ``apps`` at module scope.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps.registry import _REGISTRY_TRUST_VERSION, _same_git_target
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import registry_trust_path

logger = logging.getLogger(__name__)


class RegistryTrustCorruptError(Exception):
    """``registry_trust.json`` exists but is not a store this writer may mutate."""


def _refuse_keystone_alias(path: Path | None = None) -> int:
    """Open the keystone refusing an alias, returning an OPEN descriptor (no decode).

    *path* defaults to :func:`registry_trust_path`; a caller that resolves the path
    itself (the clone-time reader in ``registry_pipeline.sources``) passes it in.

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
    undecodable file passes it. ``reset_registry_trust`` wants exactly this (it
    overwrites the content, so its being undecodable is not a reason to refuse);
    :func:`_read_keystone_text_no_alias` calls it and then decodes.
    """
    if path is None:
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


def _read_keystone_text_no_alias(path: Path | None = None) -> str | None:
    """Read the keystone's bytes, refusing an inode reachable under a second name.

    Refuses an alias through :func:`_refuse_keystone_alias` (a symlink, an extra
    hardlink, or a non-regular file), then DECODES the descriptor it returns.
    Returns the file text, ``None`` when the keystone is absent, and raises
    :class:`RegistryTrustCorruptError` for the alias cases the refusal names or for
    non-UTF-8 content. A caller that must repair undecodable bytes (the reset)
    calls :func:`_refuse_keystone_alias` directly so the decode does not stand
    between it and the file it exists to overwrite.
    """
    fd = _refuse_keystone_alias(path)
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
    """The repo URLs a version-2 ``owner_trusted`` record names, or ``None``.

    Version 2 stores a JSON LIST of credential-free repo URLs. Returns ``None``
    for any other version, or for a version-2 ``owner_trusted`` that is not a list,
    so the caller can refuse it as corrupt; the members are returned RAW (not
    filtered) so a strict reader can decide whether a malformed entry is fatal
    while a tolerant reader drops it. This is the ONE schema validator both the
    strict reader here and the tolerant runtime reader
    (``sources._granted_owner_repos``, which reaches it through
    :func:`read_registry_trust_strict`) share, so they cannot disagree about what a
    valid store is.
    """
    if version == 2:
        return list(owner_trusted) if isinstance(owner_trusted, list) else None
    return None


def read_registry_trust_strict() -> dict:
    """Read ``registry_trust.json`` for a MUTATION: raise on corrupt, empty if absent.

    Returns a NORMALISED record ``{"version": <current>, "owner_trusted": [<repos>]}``
    — the version-2 list shape the grant/revoke writers mutate.

    An empty ``{}`` document is the ABSENT-store case, not a corrupt one: the
    sandbox pre-creates this keystone as ``{}``
    (``sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES``), so it is treated exactly
    like a missing file (the versioned-empty store) and the first grant lands
    instead of 500ing on the version check. A NON-empty document is held to the
    schema: only a version-2 document whose ``owner_trusted`` is a list parses;
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
    # Only the current version parses; anything else is corrupt. A version-2
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
    list (version 2), so grant/revoke mutate this one shape whatever the on-disk
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
    is the value itself — version 2 stores no per-repo record body, because SEL
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

    The registries PUT, which already holds the shared config lock across its
    config write and its grant sweep, does not reach this: it uses
    :func:`revoke_owner_grants_for_absent_repos_locked`, which carries its own
    read-modify-write for a caller that must not re-enter the non-reentrant lock.
    """
    atomic_write(registry_trust_path(), json.dumps(data, indent=2) + "\n", restrict_to_owner=True)


async def reset_registry_trust() -> None:
    """Atomically restore the keystone to the empty document, dropping every grant.

    The remedy for a CORRUPT ``registry_trust.json``: the read-modify-write path
    (grant/revoke through :func:`read_registry_trust_strict`) cannot repair it,
    because its strict read raises
    on the corrupt document before ``mutate`` runs. This writer overwrites the file
    with ``{}`` — the same empty document the sandbox pre-creates, which every
    reader treats as the versioned-empty (no-grants) store — so a following grant
    lands normally.

    It still refuses an ALIAS-backed keystone (a symlink, or a regular file with a
    second hardlink): :func:`_refuse_keystone_alias` is called first purely for
    that refusal, so a reset never writes THROUGH a link to whatever it targets
    — that is a different remedy (remove the alias), not one this reset performs.
    A ``RegistryTrustCorruptError`` from the alias check is re-raised; the corrupt
    CONTENT the reset is meant to clear is never decoded here, so undecodable bytes
    (the exact state a version reader would 500 on) do not block the write —
    :func:`_refuse_keystone_alias` reads no bytes.

    Runs under the shared config lock in a thread executor, owner-only on write,
    exactly like the other keystone writers.
    """
    path: Path = registry_trust_path()

    def _reset() -> None:
        # Alias refusal ONLY — no decode, so undecodable bytes reach the overwrite.
        # A symlink/hardlink raises RegistryTrustCorruptError here and the reset is
        # refused; any other file (undecodable bytes, malformed JSON, unknown
        # version) yields a descriptor we simply close and overwrite with the empty
        # document.
        fd = _refuse_keystone_alias(path)
        if fd >= 0:
            os.close(fd)
        atomic_write(path, "{}\n", restrict_to_owner=True)

    async with _get_registry_trust_lock():
        await asyncio.get_running_loop().run_in_executor(None, _reset)


def _get_registry_trust_lock():
    """The shared config lock every keystone/config writer takes.

    Call-time import for the layering reason ``apps/routes.py`` documents at its
    own ``_get_config_lock`` import: ``apps`` sits below ``dashboard`` and must
    not depend on it at load time (``agents`` imports from ``apps`` at module
    scope, so a top-level import here would also be a cycle). The lock is the SAME
    object the registries PUT and the grant/revoke endpoints take, so a grant
    validation and a registry removal cannot interleave.
    """
    from kiro_crew.dashboard.handlers.agents import _get_config_lock

    return _get_config_lock()


async def revoke_owner_grants_for_absent_repos_locked(kept_repos: list[str]) -> list[str]:
    """Drop ``owner`` grants whose repository is not in *kept_repos*, holding the lock.

    Called by the registries replace-all PUT so a grant lives exactly as long as
    the config row that justified it: deleting a registry in the editor revokes
    its grant rather than leaving it alive and invisible. Comparison uses the same
    credential-free ``_same_git_target`` predicate the reader and grant handler
    use. Returns the credential-free repository URLs whose grant was dropped (empty
    when nothing changed), so the caller can audit and expire caches. A corrupt
    keystone raises :class:`RegistryTrustCorruptError`; an absent/empty one is a
    no-op that returns ``[]`` without writing.

    Assumes the caller HOLDS the shared config lock. The registries PUT reaches
    this inside its own ``_get_config_lock`` block (the same object
    ``_get_registry_trust_lock`` returns, which is not reentrant), so the keystone
    read that decides whether to write happens on the same on-disk state the
    revoke transaction publishes, with no lock gap. The "nothing to revoke"
    pre-read and the read-modify-write run in ONE executor call, so neither the
    strict read nor the write ever touches the event loop.
    """
    revoked: list[str] = []

    def _mutate(data: dict) -> None:
        granted = data.get("owner_trusted")
        if not isinstance(granted, list):
            return
        kept: list = []
        for key in granted:
            if isinstance(key, str) and not any(_same_git_target(key, k) for k in kept_repos):
                revoked.append(key)
            else:
                kept.append(key)
        data["owner_trusted"] = kept

    path: Path = registry_trust_path()

    def _read_modify_write() -> None:
        # Avoid a write when there is demonstrably nothing to revoke, so an
        # unchanged PUT does not rewrite the keystone or touch its mtime. A
        # corrupt document still raises here (through the strict read), which the
        # caller reports. Both reads and the write share this one executor call so
        # the decision and the write see one on-disk state.
        data = read_registry_trust_strict()
        existing = data.get("owner_trusted") or []
        if not existing:
            return
        _mutate(data)
        if not revoked:
            return
        atomic_write(path, json.dumps(data, indent=2) + "\n", restrict_to_owner=True)

    await asyncio.get_running_loop().run_in_executor(None, _read_modify_write)
    return revoked
