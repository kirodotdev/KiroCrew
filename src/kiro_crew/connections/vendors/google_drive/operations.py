"""Google Drive operation sequences -- driven through W01's executor, no HTTP here.

This is the layer that used to be a self-built HTTP client. It now holds ONLY the
Drive sequencing logic -- walk a file list, choose export vs alt=media vs shortcut
resolution, read the change feed and recover a stale token as a resync -- and
performs every outbound call through an injected :class:`OperationRunner`, which
is W01's ``execute`` / ``PageWalk`` bound to the Drive locator + decode. There is
no token, no session, no socket, no vault, no refresh and no revoke in this file:
credential custody is single-sourced in W01, and this module only assembles the
descriptor + args for each step and reads the neutral
:class:`~kiro_crew.connections.control_plane.result.OperationResult` back.

Injecting the runner (rather than reaching for a network) is what lets the whole
sequence -- multi-page walk, export/media selection, shortcut resolution,
incremental changes, resync boundary -- be proved against a scripted runner with
no Google account, which is the ``code_complete`` ceiling this work runs under.
The scripted runner in the tests wraps the REAL W01 ``execute`` over an in-memory
fake transport, so the Drive locator + decode are exercised on the true path.
"""

from __future__ import annotations

import logging
from typing import Any, List, Mapping, Optional, Protocol, Tuple

from kiro_crew.connections.control_plane.executor import ExecutionOutcome
from kiro_crew.connections.control_plane.operation import OperationDescriptor
from kiro_crew.connections.control_plane.result import (
    BytesPayload,
    CollectionPayload,
    ObjectPayload,
)

from . import descriptors, drive_api
from .decode import CHANGES_CHECKPOINT_KEY

logger = logging.getLogger(__name__)


class DriveOperationError(Exception):
    """A Drive operation returned a failure outcome from W01's executor.

    Carries the neutral :class:`~kiro_crew.connections.control_plane.errors.ErrorClass`
    W01 classified (``auth`` / ``forbidden`` / ``throttle`` / ``not_found`` /
    ``input`` / ``temporary`` / ...), so a caller distinguishes a stale token
    (recovered) from a real failure without re-reading HTTP internals. No
    credential material is in the message.
    """

    def __init__(self, error_class: str, detail: str) -> None:
        super().__init__(f"drive operation failed [{error_class}]: {detail}")
        self.error_class = error_class


class OperationRunner(Protocol):
    """Runs ONE Drive operation through W01's executor. Injected, host-owned.

    The seam that keeps custody in W01: given a descriptor and its request args,
    the host binds the trusted handle, credential mode, governance layers and the
    Drive locator+decode, calls :func:`~kiro_crew.connections.control_plane.executor.execute`
    (or drives a :class:`~kiro_crew.connections.control_plane.executor.PageWalk`
    for a list), and returns the outcome. This vendor module NEVER constructs a
    handle, a token or a transport -- it only says WHICH operation with WHICH
    args, and reads the result.

    ``run`` executes a single (non-paged) operation. ``walk`` yields every page
    of a paged operation as an outcome, in order, applying the base args to each
    page (W01's PageWalk carries the cursor). Both surface a failure by returning
    an outcome whose ``ok`` is False; the caller maps it to
    :class:`DriveOperationError`.
    """

    def run(
        self, descriptor: OperationDescriptor, request_args: Mapping[str, Any]
    ) -> ExecutionOutcome: ...

    def walk(self, descriptor: OperationDescriptor, base_args: Mapping[str, Any]):
        """Yield an ExecutionOutcome per page (a generator)."""
        ...


class DriveOperations:
    """Drive v3 sequences over an injected :class:`OperationRunner`."""

    def __init__(self, runner: OperationRunner, *, page_size: int = 100) -> None:
        self._runner = runner
        self._page_size = page_size

    # ---- files.list (paged) ----------------------------------------------

    def list_files(
        self,
        *,
        drive_id: Optional[str] = None,
        query: Optional[str] = None,
        order_by: Optional[str] = None,
    ) -> List[Mapping[str, Any]]:
        """Walk every ``files.list`` page (via W01's PageWalk) and return files."""
        base = _clean(
            {
                "drive_id": drive_id,
                "query": query,
                "order_by": order_by,
                "page_size": self._page_size,
            }
        )
        collected: List[Mapping[str, Any]] = []
        for outcome in self._runner.walk(descriptors.LIST_FILES, base):
            items = _collection_items(_require_ok(outcome, "files.list"))
            collected.extend(items)
        return collected

    # ---- content: export | media | shortcut ------------------------------

    def fetch_content(self, file_meta: Mapping[str, Any], *, _depth: int = 0) -> Tuple[bytes, str]:
        """Fetch a file's extractable content bytes + their mimeType.

        Shortcut -> resolve targetId then fetch the target (one hop only); native
        -> export (target from :func:`drive_api.export_mime_for`, None -> skip);
        binary -> alt=media; folder -> no content. Same logic as before, but every
        fetch is a W01 operation, not a self-issued request.
        """
        mime = str(file_meta.get("mimeType") or "")
        file_id = str(file_meta.get("id") or "")
        if not file_id:
            raise DriveOperationError("input", "file metadata has no id")

        if drive_api.is_shortcut(mime):
            if _depth >= 1:
                logger.warning("Drive shortcut %s -> shortcut; not chased", file_id)
                return b"", ""
            target_id = _shortcut_target_id(file_meta)
            if not target_id:
                return b"", ""
            target_meta = self.get_metadata(target_id)
            return self.fetch_content(target_meta, _depth=_depth + 1)

        if drive_api.is_folder(mime):
            return b"", ""

        if drive_api.is_google_native(mime):
            export_mime = drive_api.export_mime_for(mime)
            if not export_mime:
                return b"", ""
            outcome = self._runner.run(
                descriptors.EXPORT, {"file_id": file_id, "export_mime": export_mime}
            )
            return _bytes_payload(_require_ok(outcome, "files.export")), export_mime

        outcome = self._runner.run(descriptors.GET_MEDIA, {"file_id": file_id})
        return _bytes_payload(_require_ok(outcome, "files.get_media")), mime

    def get_metadata(self, file_id: str) -> Mapping[str, Any]:
        outcome = self._runner.run(descriptors.GET_METADATA, {"file_id": file_id})
        return _object_payload(_require_ok(outcome, "files.get"))

    # ---- change feed: start token + incremental with resync --------------

    def get_start_page_token(self, *, drive_id: Optional[str] = None) -> str:
        outcome = self._runner.run(descriptors.GET_START_PAGE_TOKEN, _clean({"drive_id": drive_id}))
        obj = _object_payload(_require_ok(outcome, "changes.getStartPageToken"))
        token = obj.get("startPageToken")
        if not isinstance(token, str) or not token:
            raise DriveOperationError(
                "input", "changes.getStartPageToken returned no startPageToken"
            )
        return token

    def list_changes(
        self, page_token: str, *, drive_id: Optional[str] = None
    ) -> Tuple[List[Mapping[str, Any]], str]:
        """Walk the change feed from ``page_token``; return (changes, checkpoint).

        A no-longer-usable page token surfaces as an ``input``-class failure from
        W01 (see :func:`_is_stale_page_token` for why the class, not a fabricated
        status/reason, is the signal, and for the documentation gap this rests
        on). It is recovered by fetching a fresh ``startPageToken`` and returning
        it as a RESYNC boundary (empty changes, fresh checkpoint) -- Drive's
        documented recovery, with NO invented TTL or status code. Any non-``input``
        failure still raises via :func:`_raise_from`.
        """
        base = _clean({"drive_id": drive_id, "page_size": self._page_size})
        collected: List[Mapping[str, Any]] = []
        checkpoint = page_token
        for outcome in self._runner.walk(
            descriptors.LIST_CHANGES, {**base, "page_token": page_token}
        ):
            if not outcome.ok:
                if _is_stale_page_token(outcome):
                    fresh = self.get_start_page_token(drive_id=drive_id)
                    logger.info("Drive change token stale; resync to fresh token")
                    return [], fresh
                _raise_from(outcome, "changes.list")
            for record in _collection_items(outcome):
                # The sentinel checkpoint row is the durable resume token.
                if CHANGES_CHECKPOINT_KEY in record:
                    checkpoint = str(record[CHANGES_CHECKPOINT_KEY]) or checkpoint
                    continue
                collected.append(record)
        return collected, checkpoint


# ---- outcome readers -----------------------------------------------------


def _require_ok(outcome: ExecutionOutcome, op: str) -> ExecutionOutcome:
    if not outcome.ok:
        _raise_from(outcome, op)
    return outcome


def _raise_from(outcome: ExecutionOutcome, op: str) -> None:
    err = outcome.error
    klass = _error_field(err, "error_class", "temporary")
    raise DriveOperationError(str(klass), f"{op} returned {klass}")


def _is_stale_page_token(outcome: ExecutionOutcome) -> bool:
    """True when a ``changes.list`` failure is the documented resync boundary.

    DOCUMENTATION GAP (see the module/spec note): Google's change-management guide
    documents the resync FLOW -- on a no-longer-usable page token, re-fetch a
    fresh ``startPageToken`` and resume from it -- but pins NEITHER a TTL NOR an
    exact HTTP status/reason for the expired-token case. The brief forbids
    inventing a number or a status, so this recogniser does NOT key on a
    fabricated ``410`` / ``invalidPageToken`` marker.

    It keys on what IS real and W01-carried: the neutral error CLASS. W01's
    executor collapses a provider client-error to ``input`` and REDACTS the
    provider's own reason text (a security boundary this vendor must not fight),
    so the reason subcode is unavailable here by design. For an incremental
    ``changes.list`` the vendor itself builds -- a fixed endpoint, our own valid
    params, a server-issued page token -- the only realistic determinate
    client-error (``input``) is a page token the server no longer accepts. That
    is Drive's documented recovery trigger, so an ``input`` failure on the change
    feed is treated as the resync boundary.

    Safety is preserved by NARROWING to ``input`` alone: every OTHER class --
    ``auth``, ``forbidden``, ``throttle``, ``not_found``, ``temporary`` -- is NOT
    swallowed as a resync and still raises, so a credential/permission/backoff
    failure can never be silently mistaken for "just re-fetch the token".
    """
    err = outcome.error
    if err is None:
        return False
    return _error_field(err, "error_class", "") == "input"


def _error_field(err, key: str, default):
    """Read a field off an OperationError, which is a TypedDict (dict at runtime).

    Tolerant of a plain object too (a test double), so a missing key/attr yields
    the default rather than raising while BUILDING an error path.
    """
    if err is None:
        return default
    if isinstance(err, Mapping):
        return err.get(key, default)
    return getattr(err, key, default)


def _collection_items(outcome: ExecutionOutcome) -> Tuple[Mapping[str, Any], ...]:
    payload = outcome.payload
    if isinstance(payload, CollectionPayload):
        return payload.items
    return ()


def _object_payload(outcome: ExecutionOutcome) -> Mapping[str, Any]:
    payload = outcome.payload
    if isinstance(payload, ObjectPayload):
        return payload.object
    return {}


def _bytes_payload(outcome: ExecutionOutcome) -> bytes:
    payload = outcome.payload
    if isinstance(payload, BytesPayload):
        return payload.data
    return b""


def _shortcut_target_id(file_meta: Mapping[str, Any]) -> Optional[str]:
    details = file_meta.get("shortcutDetails")
    if not isinstance(details, Mapping):
        return None
    target = details.get("targetId")
    return target if isinstance(target, str) and target else None


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


__all__ = ["DriveOperationError", "DriveOperations", "OperationRunner"]
