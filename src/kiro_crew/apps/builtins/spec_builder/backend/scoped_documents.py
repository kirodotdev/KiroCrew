"""Formal documents of existing Specs, resolved from the native index."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from functools import partial
from pathlib import Path

from kiro_crew.apps.resource_access import BoundDocument, ResourceAccessError, register_provider
from kiro_crew.dashboard.chat_utils_awaits import drained_to_thread

from .parsers import is_sensitive_path
from .repository import _aload_index

DOCUMENTS = frozenset({"requirements.md", "design.md", "tasks.md"})


def _spec_root(resource_id: str, meta: dict) -> Path:
    value = meta.get("spec_dir")
    if not isinstance(value, str) or not value:
        raise ValueError("resource_scope_mismatch")
    root = Path(value)
    if (
        not root.is_absolute()
        or ".." in root.parts
        or root.name != resource_id
        or root.parent.name != "specs"
        or root.parent.parent.name != ".kiro"
        or is_sensitive_path(str(root))
    ):
        raise ValueError("resource_scope_mismatch")
    return root


async def read_resource(
    workspace_root: Path, resource_id: str, document: str, operation: str, body: dict
) -> dict:
    if document not in DOCUMENTS:
        raise ValueError("invalid_document")
    if operation not in {"read", "create", "update"}:
        raise ValueError("unsupported_operation")
    index = await _aload_index()
    meta = index.get(resource_id)
    if not isinstance(meta, dict):
        raise ValueError("resource_not_found")
    root = _spec_root(resource_id, meta)
    try:
        root.relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError("resource_scope_mismatch") from exc
    bound = BoundDocument(root, document)
    if operation == "read":
        if body:
            raise ValueError("invalid_body")
        snapshot = await asyncio.to_thread(bound.store().read, bound.relative)
    else:
        snapshot = await drained_to_thread(
            partial(
                bound.store().write,
                bound.relative,
                body["content"],
                operation=operation,
                expected_revision=body["expectedRevision"],
            )
        )
    return asdict(snapshot)


async def owner_document(path: Path) -> BoundDocument | None:
    if path.name not in DOCUMENTS:
        return None
    for resource_id, meta in (await _aload_index()).items():
        if not isinstance(meta, dict):
            continue
        try:
            root = _spec_root(resource_id, meta)
        except ValueError:
            continue
        if path.parent == root:
            return BoundDocument(root, path.name)
    return None


def register(app) -> None:
    from kiro_crew.apps.manager import is_app_enabled

    register_provider(
        app,
        "spec-builder",
        guarded_reader(app),
        enabled=lambda: is_app_enabled("spec-builder"),
        owner_locator=owner_document,
    )


def guarded_reader(app):
    """Serialize document mutations with the native turn-start boundary."""
    from .orchestration.turn_guard import (
        _alias_slots,
        _busy_alias,
        _busy_observed_directory_slot,
        _turn_lock,
    )

    async def access(workspace_root, resource_id, document, operation, body):
        if operation == "read":
            return await read_resource(workspace_root, resource_id, document, operation, body)
        state = app.get("state")
        if state is None:
            raise ResourceAccessError("resource_unavailable", 503)
        meta = (await _aload_index()).get(resource_id)
        if not isinstance(meta, dict):
            raise ValueError("resource_not_found")
        root = _spec_root(resource_id, meta)
        async with _turn_lock(str(root)):
            fresh = (await _aload_index()).get(resource_id)
            if not isinstance(fresh, dict) or _spec_root(resource_id, fresh) != root:
                raise ResourceAccessError("resource_conflict", 409)
            aliases = await _alias_slots(str(root), own_slot_key="")
            if _busy_alias(state, aliases) or _busy_observed_directory_slot(state, str(root), ""):
                raise ResourceAccessError("resource_busy", 409)
            return await read_resource(workspace_root, resource_id, document, operation, body)

    return access
