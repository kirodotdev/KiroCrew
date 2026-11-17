"""Append a finished run's exact source to a saved workflow as its next revision.

The dashboard owner's update-from-run routes reach this through
``WorkflowService.update_run_definition`` and ``preview_run_definition_update``.
The run passes promotion's checks (``WorkflowService._reusable_run``). The
library's ``expected_revision`` stops a stale write to the workflow, and the
previewed source's SHA-256 stops an Apply of a run source the owner never saw: a
paused TaskRunner plan can be replanned while the preview is open.

This module sits below ``service`` in the layering contract, so it is handed the
service and never imports it.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING, Any, Optional

from .library import SOURCE_FORMAT_PYTHON, declared_lineage, derived_slug
from .validate import validate

if TYPE_CHECKING:
    from .registry import RunHandle

#: The fields of the target workflow the preview returns.
_TARGET_FIELDS = ("id", "slug", "name", "revision", "format")


def source_sha256(source: str) -> str:
    """The digest the preview returns and the update must be given back."""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _format(definition: dict[str, Any]) -> str:
    return str(definition.get("format") or SOURCE_FORMAT_PYTHON)


def _definition_not_found() -> dict[str, Any]:
    """A refusal naming the saved workflow, not the run, as missing."""
    return {"ok": False, "error": "no such saved workflow", "definition_not_found": True}


async def update(
    service: Any,
    run_id: str,
    workflow_ref: str,
    *,
    expected_revision: int,
    expected_source_sha256: str,
) -> dict[str, Any]:
    """Append the run's source as the saved workflow's next revision, or refuse.

    ``not_found`` names the run and ``definition_not_found`` the saved workflow.
    The source is read once, before any await, so the bytes written are the bytes
    whose digest was checked.
    """
    handle = await service._reusable_run(run_id)
    if isinstance(handle, dict):
        return handle
    source, source_format = handle.source, handle.source_format
    if source_sha256(source) != expected_source_sha256:
        return {
            "ok": False,
            "error": "the run's source changed since it was previewed",
            "source_changed": True,
        }
    current = await asyncio.to_thread(service.get_definition, workflow_ref)
    if current is None:
        return _definition_not_found()
    if _format(current) != source_format:
        return {
            "ok": False,
            "error": "the run and the saved workflow have different formats",
            "format_mismatch": True,
        }
    if current.get("revision") == expected_revision and current.get("source") == source:
        return {
            "ok": False,
            "error": "the run's source is already this saved revision",
            "unchanged": True,
        }
    out = await asyncio.to_thread(
        service.update_definition,
        # The ref that reached it: a live copy beside its tombstone answers only
        # to its free slug, never to its reserved id. expected_id pins the write.
        workflow_ref,
        source=source,
        expected_revision=expected_revision,
        expected_id=str(current["id"]),
    )
    # Deleted between the read above and the write: the run itself still exists.
    return _definition_not_found() if out.get("not_found") else out


async def preview(service: Any, run_id: str, workflow_ref: str = "") -> dict[str, Any]:
    """The saved workflow an update would revise, both sources, or the refusal.

    Without *workflow_ref* the target is suggested (:func:`_suggest_target`) and
    may be ``None``. ``target`` carries only :data:`_TARGET_FIELDS`; the caller
    builds the diff from ``target_source`` and ``run_source``.
    """
    handle = await service._reusable_run(run_id)
    if isinstance(handle, dict):
        return handle
    source = handle.source
    if workflow_ref:
        target = await asyncio.to_thread(service.get_definition, workflow_ref)
        if target is None:
            return _definition_not_found()
    else:
        target = await asyncio.to_thread(_suggest_target, service, handle, source)
    return {
        "ok": True,
        "target": None if target is None else {key: target.get(key) for key in _TARGET_FIELDS},
        "target_source": "" if target is None else str(target.get("source") or ""),
        "run_source": source,
        "run_source_sha256": source_sha256(source),
        "unchanged": target is not None and target.get("source") == source,
        "format_mismatch": target is not None and _format(target) != handle.source_format,
    }


def _suggest_target(service: Any, handle: RunHandle, source: str) -> Optional[dict[str, Any]]:
    """The saved workflow a run most likely revises, or ``None``.

    In order: the definition an edited rerun came from, a Python script's
    ``META["adapted_from"]``, then the slug of the script's ``META`` name and of
    the run's name. Only a definition of the run's format is suggested.
    """
    refs: list[str] = []
    if handle.derived_from_workflow_id:
        refs.append(handle.derived_from_workflow_id)
    if handle.source_format == SOURCE_FORMAT_PYTHON:
        meta = validate(source).meta or {}
        lineage = declared_lineage(meta)
        if lineage is not None:
            refs.append(lineage["workflow_id"])
        if meta.get("name"):
            refs.append(derived_slug(str(meta["name"])))
    if handle.name:
        refs.append(derived_slug(handle.name))
    with service._definition_lock:
        for ref in dict.fromkeys(refs):
            definition = service._definition_library.get(ref)
            if definition is not None and _format(definition) == handle.source_format:
                return definition
    return None
