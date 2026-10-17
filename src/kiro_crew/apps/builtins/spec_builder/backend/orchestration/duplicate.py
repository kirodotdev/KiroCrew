"""Duplicate's crash-safe publication transaction.

A copy is staged in a marker-provenanced directory, its name is reserved in the
index, the documents are published with a no-replace rename, and the reservation
is finalized, rolling back without ever leaving a half copy under the
destination name. The reservation and the stage marker together are the proof
startup recovery uses to finish or discard a transaction a crash interrupted.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Any, NamedTuple

from ..parsers import _VALID_TYPES, _clean_str, _sha256_text
from ..repository import (
    _DUPLICATING,
    _PROCESS_ID,
    _create_duplicate_stage,
    _duplicate_stage_identity,
    _forget_deleted,
    _mutate_index,
    _new_slot_key,
    _prepare_spec_dir,
    _remove_duplicate_marker,
    _reservation_is_ours,
    _rollback_staged_docs,
    _write_and_publish_duplicate,
)

logger = logging.getLogger("kirocrew.app.spec-builder")


class _DuplicateOutcome(NamedTuple):
    """Terminal state of one transaction and the index entry it registered.

    ``outcome`` is ``success``, ``exists``, ``refusal``, ``write_failed`` or
    ``finalization_failed``; ``detail`` carries the refusal or write-failure reason.
    """

    outcome: str
    detail: str
    target_dir: Path
    entry: dict[str, Any]


async def _publish_duplicate(
    fresh: dict,
    new_name: str,
    safe_wd: Path,
    target_dir: Path,
    docs: dict[str, str | None],
) -> _DuplicateOutcome:
    """Stage, reserve, publish and finalize one copy of *docs* as *new_name*.

    The reservation, publication and finalization run in one shielded task: the
    thread performing publication cannot be stopped by cancellation, so the caller
    is not released until the index state is terminal.
    """
    slot_key = _new_slot_key(new_name)
    duplicate_token = uuid.uuid4().hex
    stage_dir = target_dir.parent / f".{new_name}.duplicate-{duplicate_token}"
    document_hashes = {
        fname: _sha256_text(text) for fname, text in docs.items() if text is not None
    }
    now = time.time()
    entry = {
        "working_dir": str(safe_wd),
        "spec_dir": str(target_dir),
        # Validated, not carried over blind: spec_type comes off the agent-writable
        # index, and an unknown value would flow into the copy's own payload.
        "spec_type": (
            st if (st := str(fresh.get("spec_type", "feature"))) in _VALID_TYPES else "feature"
        ),
        "status": "planning",
        "slot_key": slot_key,
        "worktree_branch": "",
        "repo_root": "",
        "title": _clean_str(fresh.get("title")),
        "created_at": now,
        "updated_at": now,
        _DUPLICATING: {
            "owner": _PROCESS_ID,
            "at": now,
            "token": duplicate_token,
            "stage_dir": str(stage_dir),
            "documents": document_hashes,
        },
    }

    def _insert(index: dict) -> bool:
        if new_name in index:
            return False
        index[new_name] = entry
        return True

    stage_failure = await asyncio.to_thread(_create_duplicate_stage, stage_dir, duplicate_token)
    if stage_failure:
        return _DuplicateOutcome("write_failed", stage_failure, target_dir, entry)
    stage_identity = await asyncio.to_thread(_duplicate_stage_identity, stage_dir, duplicate_token)
    if stage_identity is None:
        await asyncio.to_thread(_remove_duplicate_marker, stage_dir, duplicate_token)
        return _DuplicateOutcome("write_failed", "", target_dir, entry)
    held = entry[_DUPLICATING]
    assert isinstance(held, dict)
    held["stage_dev"], held["stage_ino"] = stage_identity

    async def _release_reservation() -> bool:
        def _pop(index: dict) -> bool:
            meta = index.get(new_name)
            if (
                meta is None
                or str(meta.get("slot_key", "")) != slot_key
                or not _reservation_is_ours(meta, _DUPLICATING)
            ):
                return False
            del index[new_name]
            return True

        return await _mutate_index(_pop)

    def _finish(index: dict) -> bool:
        meta = index.get(new_name)
        if (
            meta is None
            or str(meta.get("slot_key", "")) != slot_key
            or not _reservation_is_ours(meta, _DUPLICATING)
        ):
            return False
        meta.pop(_DUPLICATING, None)
        meta["updated_at"] = time.time()
        return True

    async def _complete_transaction() -> tuple[str, str, Path]:
        """Reach a durable terminal state after publishing transaction provenance."""
        if not await _mutate_index(_insert):
            # No reservation points at this empty, marker-only stage. A crash
            # before cleanup strands no copied document.
            await asyncio.to_thread(_remove_duplicate_marker, stage_dir, duplicate_token)
            return "exists", "", target_dir

        # The marked stage exists before the name is reserved, but it is not
        # populated yet. Re-run validation after arbitration so an external
        # writer that placed files in the meantime is refused, not overwritten.
        resolved_target, reserved_refusal = await asyncio.to_thread(
            _prepare_spec_dir,
            str(safe_wd),
            safe_wd,
            new_name,
            False,
            create=False,
            expected_dir=target_dir,
        )
        if reserved_refusal:
            if await _release_reservation():
                # The stage contains no documents. Removing the reservation
                # first leaves only an empty marker directory after a crash.
                await asyncio.to_thread(_remove_duplicate_marker, stage_dir, duplicate_token)
            return "refusal", reserved_refusal, resolved_target

        failure, created = await asyncio.to_thread(
            _write_and_publish_duplicate,
            stage_dir,
            resolved_target,
            docs,
            duplicate_token,
            stage_identity,
        )
        if failure:
            if failure == "identity_mismatch":
                # A competing directory won the publication name. It is not our
                # copy, so never leave this duplicate's index entry pointing at
                # it; the source documents remain available for a clean retry.
                await _release_reservation()
                return "write_failed", failure, resolved_target
            # Keep the marker while rolling back. If the process exits during
            # this step, recovery still has proof that the reservation and any
            # staged documents belong to this transaction. Release the index
            # only after every editable document is confirmed absent, then
            # remove the marker last.
            rolled_back = await asyncio.to_thread(_rollback_staged_docs, stage_dir, created)
            if rolled_back and await _release_reservation():
                await asyncio.to_thread(_remove_duplicate_marker, stage_dir, duplicate_token)
            return "write_failed", failure, resolved_target

        await asyncio.to_thread(_forget_deleted, str(resolved_target))
        try:
            finalized = await _mutate_index(_finish)
        except Exception:
            # Publication already committed. Keep its marker and reservation so
            # startup recovery can adopt the complete copy, while containing a
            # storage failure as the same recoverable response as a lost claim.
            logger.exception("could not finalize duplicate index entry for %s", new_name)
            return "finalization_failed", "", resolved_target
        if not finalized:
            return "finalization_failed", "", resolved_target
        await asyncio.to_thread(_remove_duplicate_marker, resolved_target, duplicate_token)
        return "success", "", resolved_target

    transaction = asyncio.create_task(_complete_transaction())
    try:
        # The thread performing publication cannot be stopped by task
        # cancellation. Shield reservation and finalization together, so the
        # request cannot abandon a same-process reservation that recovery skips.
        outcome, detail, published_dir = await asyncio.shield(transaction)
    except asyncio.CancelledError as cancelled:
        # Keep the caller as a strong owner of the transaction and do not
        # report cancellation until its index state is terminal. Repeated
        # cancellation (for example during server shutdown) cannot reopen the
        # same-process recovery gap.
        while not transaction.done():
            try:
                await asyncio.shield(transaction)
            except asyncio.CancelledError:
                continue
        transaction.result()
        raise cancelled
    return _DuplicateOutcome(outcome, detail, published_dir, entry)
