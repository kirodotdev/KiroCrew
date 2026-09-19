"""W01 OperationDescriptors for the Google Drive operations this connector uses.

A descriptor is what W01's executor authorizes and routes a call BY (see
``control_plane/operation.py``). It is a pure declaration -- no IO, no token, no
URL -- carrying the operation's stable id, its ``service_id`` (always
``google_drive`` here, the manifest's own service range), its ``operation_kind``
and ``effect``, and the credential modes it permits.

Every Drive operation this connector performs is a READ (``effect="read"``):
listing files, fetching metadata, exporting/downloading content, and reading the
change feed. None writes, deletes, shares or sends -- the brief forbids any real
business write, and a read is also the effect W01's write-replay gate leaves
ungated (nothing landed to half-apply). ``credential_modes`` is ``oauth_user``
because Drive access is the querying/owning user's own OAuth grant; the SELECTED
mode for a given call is chosen on the per-call context, from within this set.

These ids are stable across rounds (a manifest assigns them); the connector and
the tests reference them through the constants here, never as bare strings.
"""

from __future__ import annotations

from typing import Tuple

from kiro_crew.connections.control_plane.operation import (
    CredentialMode,
    OperationDescriptor,
    OperationKind,
    ServiceId,
)

SERVICE_ID: ServiceId = "google_drive"

# Stable operation ids. Namespaced by the service so they cannot collide with
# another provider's operation ids on the shared descriptor seam.
OP_LIST_FILES = "google_drive.files.list"
OP_GET_METADATA = "google_drive.files.get"
OP_EXPORT = "google_drive.files.export"
OP_GET_MEDIA = "google_drive.files.get_media"
OP_LIST_CHANGES = "google_drive.changes.list"
OP_GET_START_PAGE_TOKEN = "google_drive.changes.getStartPageToken"

# Drive read operations authenticate as the user's own OAuth grant.
_OAUTH_ONLY: Tuple[CredentialMode, ...] = ("oauth_user",)


def _descriptor(operation_id: str, operation_kind: OperationKind) -> OperationDescriptor:
    return OperationDescriptor(
        operation_id=operation_id,
        service_id=SERVICE_ID,
        operation_kind=operation_kind,
        effect="read",
        credential_modes=_OAUTH_ONLY,
    )


#: ``files.list`` -- a paginated list (walked page by page via W01's PageWalk).
LIST_FILES = _descriptor(OP_LIST_FILES, "list")

#: ``files.get`` (metadata) -- a single fetch of one file's metadata object.
GET_METADATA = _descriptor(OP_GET_METADATA, "single_fetch")

#: ``files.export`` -- a single fetch returning exported bytes of a native type.
EXPORT = _descriptor(OP_EXPORT, "single_fetch")

#: ``files.get?alt=media`` -- a single fetch returning a binary file's bytes.
GET_MEDIA = _descriptor(OP_GET_MEDIA, "single_fetch")

#: ``changes.list`` -- a paginated list of change records (walked via PageWalk).
LIST_CHANGES = _descriptor(OP_LIST_CHANGES, "list")

#: ``changes.getStartPageToken`` -- a single fetch of the resync/resume token.
GET_START_PAGE_TOKEN = _descriptor(OP_GET_START_PAGE_TOKEN, "single_fetch")


#: Every Drive descriptor, keyed by operation id -- the locator/decode switch on
#: this so a new operation is added in exactly one place.
BY_OPERATION_ID: dict[str, OperationDescriptor] = {
    d["operation_id"]: d
    for d in (LIST_FILES, GET_METADATA, EXPORT, GET_MEDIA, LIST_CHANGES, GET_START_PAGE_TOKEN)
}


__all__ = [
    "BY_OPERATION_ID",
    "EXPORT",
    "GET_MEDIA",
    "GET_METADATA",
    "GET_START_PAGE_TOKEN",
    "LIST_CHANGES",
    "LIST_FILES",
    "OP_EXPORT",
    "OP_GET_MEDIA",
    "OP_GET_METADATA",
    "OP_GET_START_PAGE_TOKEN",
    "OP_LIST_CHANGES",
    "OP_LIST_FILES",
    "SERVICE_ID",
]
