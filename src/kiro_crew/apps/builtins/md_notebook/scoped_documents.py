"""Exact native-vault note access and the Owner editor's shared CAS protocol.

Importing or registering this module starts no backend, migration, Git operation,
sync loop or vault scan. The existing masked registry selects vaults at call time.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import contextmanager
from pathlib import Path

from kiro_crew import hooks
from kiro_crew.apps.document_store import DocumentSnapshot, DocumentStore, DocumentStoreError
from kiro_crew.apps.manager import is_app_enabled
from kiro_crew.apps.resource_access import BoundDocument, ResourceAccessError, register_provider
from kiro_crew.dashboard.chat_utils_awaits import drained_to_thread

from . import server_paths, server_store
from .server_http import ApiError, require_writable
from .server_vault_lock import supported, vault_write_lock

_DOCUMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,123}\.md\Z")
_REVISION = re.compile(r"[a-fA-F0-9]{64}\Z")


def owner_store_supported() -> bool:
    return supported()


def _root(vault: dict) -> Path:
    root = server_paths.content_root(vault)
    if not root.is_absolute():
        raise ResourceAccessError("resource_scope_mismatch", 403)
    return root


def _gate(root: Path, relative: str) -> None:
    # Preserve Notes' existing sensitive-path floor, without reading bytes by
    # name; DocumentStore performs the actual descriptor-pinned read/write.
    if hooks.validate_file_path(str(root / relative)) is None:
        raise ResourceAccessError("resource_forbidden", 403)


async def read_resource(
    workspace_root: Path, resource_id: str, document: str, operation: str, body: dict
) -> dict:
    if not _DOCUMENT.fullmatch(document) or ".." in document:
        raise ResourceAccessError("invalid_document")
    if operation not in {"read", "create", "update"}:
        raise ResourceAccessError("unsupported_operation")
    if not isinstance(body, dict) or (
        body if operation == "read" else set(body) != {"content", "expectedRevision"}
    ):
        raise ResourceAccessError("invalid_body")
    vault = next(
        (
            v
            for v in await server_store.read_vaults()
            if isinstance(v, dict) and v.get("id") == resource_id
        ),
        None,
    )
    if vault is None:
        raise ResourceAccessError("resource_not_found", 404)
    if operation != "read":
        try:
            require_writable(vault)
        except ApiError as exc:
            raise ResourceAccessError(exc.code, exc.status) from exc

    def transaction() -> DocumentSnapshot:
        if not owner_store_supported():
            raise DocumentStoreError("unsupported_platform")
        root = _root(vault)
        if not root.resolve().is_relative_to(workspace_root.resolve()):
            raise ResourceAccessError("resource_scope_mismatch", 403)
        _gate(root, document)
        store = DocumentStore(root)
        if operation == "read":
            return store.read(document)
        return store.write(
            document,
            body["content"],
            operation=operation,
            expected_revision=body["expectedRevision"],
        )

    if operation == "read":
        snapshot = await asyncio.to_thread(transaction)
    else:
        async with vault_write_lock(vault["localPath"]):
            snapshot = await drained_to_thread(transaction)
    return {"content": snapshot.content, "revision": snapshot.revision}


async def owner_locator(path: Path) -> BoundDocument | None:
    vaults = await server_store.read_vaults()

    def locate() -> BoundDocument | None:
        for vault in vaults:
            if not isinstance(vault, dict) or not isinstance(vault.get("localPath"), str):
                continue
            try:
                root = _root(vault)
                relative = path.relative_to(root).as_posix()
                server_paths.require_note_path(relative)
                _gate(root, relative)
            except (ValueError, ApiError, ResourceAccessError):
                continue
            return BoundDocument(root, relative)
        return None

    return await asyncio.to_thread(locate)


def register_routes(ctx) -> list:
    if ctx.http_app is None:
        raise RuntimeError("resource_host_unavailable")
    register_provider(
        ctx.http_app,
        "md-notebook",
        read_resource,
        enabled=lambda: is_app_enabled("md-notebook"),
        owner_locator=owner_locator,
    )
    return []


@contextmanager
def _owner_errors():
    try:
        yield
    except ResourceAccessError as exc:
        raise ApiError("note access refused", exc.status, code=exc.code) from exc
    except DocumentStoreError as exc:
        status = 404 if exc.code == "not_found" else 400
        raise ApiError("note access refused", status, code=exc.code) from exc


def owner_read(vault: dict, relative: str) -> tuple[str, str] | None:
    """Owner read in a filesystem worker; unsupported platforms retain native I/O."""
    if not owner_store_supported():
        return None
    with _owner_errors():
        root = _root(vault)
        _gate(root, relative)
        snapshot = DocumentStore(root).read(relative)
    # Keep the native editor's newline normalization and raw-byte hash contract.
    return snapshot.content.replace("\r\n", "\n").replace("\r", "\n"), snapshot.revision


def owner_save(
    vault: dict, relative: str, content: str, base_mtime: object, base_hash: object
) -> bool:
    """Serialize native saves with App writers while preserving ESTALE responses."""
    if not owner_store_supported():
        return False
    root = _root(vault)
    _gate(root, relative)
    with _owner_errors(), DocumentStore(root).locked_path(relative) as document:
        try:
            current = document.read()
        except DocumentStoreError as exc:
            if exc.code != "not_found":
                raise
            if (
                isinstance(base_hash, str) or isinstance(base_mtime, (int, float))
            ) and base_mtime != -1:
                raise ApiError(
                    "this note was deleted on disk since you opened it",
                    409,
                    code="ESTALE",
                    mtime=-1,
                    disk="",
                ) from exc
            document.write(content, operation="create")
            return True
        mtime = (root / relative).stat().st_mtime * 1000
        if not isinstance(base_hash, str) or _REVISION.fullmatch(base_hash) is None:
            stale = True
        else:
            stale = current.revision != base_hash.lower()
        if stale:
            raise ApiError(
                "this note changed on disk since you opened it",
                409,
                code="ESTALE",
                mtime=mtime,
                disk=current.content,
                contentHash=current.revision,
            )
        document.write(content, operation="update", expected_revision=current.revision)
    return True


def owner_create(vault: dict, relative: str) -> bool:
    if not owner_store_supported():
        return False
    root = _root(vault)
    _gate(root, relative)
    try:
        DocumentStore(root).write(relative, "", operation="create")
    except DocumentStoreError as exc:
        if exc.code == "conflict":
            raise FileExistsError from exc
        raise
    return True
