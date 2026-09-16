"""Google Drive API v3 request construction -- the real wire shapes.

Pure request BUILDERS: each function returns a :class:`DriveRequest` (method,
absolute https URL, query params) for one Drive v3 endpoint. Nothing here opens
a socket -- :mod:`.client` sends these, and the unit tests assert on the built
shapes without a network. Building the request separately from sending it is
what lets the test suite prove -- with no Google account -- that a Shared Drive
listing sets both drive flags, that a native Doc is fetched via ``export`` and a
PDF via ``alt=media``, and that a change page carries the saved page token.

Every string and flag here traces to Google's Drive API v3 reference:

* ``files.list``      GET  https://www.googleapis.com/drive/v3/files
* ``files.get`` (meta) GET https://www.googleapis.com/drive/v3/files/{fileId}
* ``files.get`` (media) GET .../files/{fileId}?alt=media   (binary content)
* ``files.export``    GET  .../files/{fileId}/export?mimeType=...  (native types)
* ``changes.list``    GET  https://www.googleapis.com/drive/v3/changes
* ``changes.getStartPageToken`` GET .../changes/startPageToken
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional

# The Drive v3 REST base. Absolute + https on purpose: the production HTTP sender
# refuses any non-https hop (a Bearer credential in clear is a leak), so a
# relative or http URL would be rejected at send time rather than silently
# downgraded.
API_ROOT = "https://www.googleapis.com/drive/v3"

# The MIME prefix Google assigns to its OWN editor types (Docs, Sheets, Slides,
# Drawings, Forms, ...). A file whose mimeType starts with this has NO stored
# bytes: it must be fetched via files.export with a target mimeType, never
# alt=media (which returns a 403 fileNotDownloadable for these).
GOOGLE_APPS_MIME_PREFIX = "application/vnd.google-apps."

# The one google-apps subtype that is neither content nor a container: a
# shortcut. It carries shortcutDetails.targetId and must be resolved to its
# target before any content fetch (see client.resolve_and_fetch).
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

# The one google-apps subtype that is a CONTAINER, not a document: a folder. It
# has no exportable content; a lister descends into it, it is never exported.
FOLDER_MIME = "application/vnd.google-apps.folder"

# The export target we request for each native editor family. Text/UTF-8 shapes
# are chosen because the knowledge ingest wants extractable text, not a binary
# office blob: a Doc exports as text/plain, a Sheet as CSV, a Slides deck as
# plain text. A family with no text export (a Drawing, a Form) maps to None and
# the connector skips its content rather than guessing a binary target.
_EXPORT_TARGETS: Dict[str, str] = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.script": "application/vnd.google-apps.script+json",
}

# The field mask requested for a listed/fetched file. Every field the per-row
# SourceRow and the ProviderResourceRef need is named EXPLICITLY: Drive returns
# only a small default set unless the caller asks, so an unnamed field comes
# back absent and the row would silently lose (e.g.) its version fingerprint.
# capabilities/permissions are named so the ACL probe can read them off the same
# object shape.
FILE_FIELDS = (
    "id,name,mimeType,modifiedTime,version,md5Checksum,size,trashed,"
    "driveId,shortcutDetails(targetId,targetMimeType),"
    "capabilities(canReadRevisions),permissionIds"
)

# The per-file field mask INSIDE a files.list response: the same file fields,
# wrapped in the list envelope's files() plus the list-level nextPageToken.
LIST_FIELDS = f"nextPageToken,files({FILE_FIELDS})"

# The changes.list field mask: the change envelope carries the page tokens AND,
# per change, the file object (so an incremental sync reads the changed file's
# metadata without a second round trip) plus removed/fileId for deletions.
CHANGES_FIELDS = (
    f"nextPageToken,newStartPageToken,"
    f"changes(changeType,removed,fileId,time,file({FILE_FIELDS}))"
)


@dataclass(frozen=True)
class DriveRequest:
    """One built Drive v3 request: method + absolute URL + query params.

    ``alt_media`` marks a request whose response body is RAW FILE BYTES (a
    ``files.get?alt=media`` download or a ``files.export``), so the sender reads
    bytes rather than parsing JSON. Everything else is a JSON metadata call.
    """

    method: str
    url: str
    params: Mapping[str, object] = field(default_factory=dict)
    alt_media: bool = False

    def with_params(self, **extra: object) -> "DriveRequest":
        merged = {**self.params, **extra}
        return DriveRequest(self.method, self.url, merged, self.alt_media)


def is_google_native(mime_type: str) -> bool:
    """True for a Google editor type that must be EXPORTED (no stored bytes).

    Excludes the folder and shortcut subtypes: a folder has no content and a
    shortcut is resolved to its target first, so neither is "a native document
    to export" even though both share the google-apps prefix.
    """
    if not mime_type or not mime_type.startswith(GOOGLE_APPS_MIME_PREFIX):
        return False
    return mime_type not in (FOLDER_MIME, SHORTCUT_MIME)


def is_shortcut(mime_type: str) -> bool:
    """True for a Drive shortcut (a pointer to another file, no content)."""
    return mime_type == SHORTCUT_MIME


def is_folder(mime_type: str) -> bool:
    """True for a Drive folder (a container, never exported)."""
    return mime_type == FOLDER_MIME


def export_mime_for(mime_type: str) -> Optional[str]:
    """The export target MIME for a native editor type, or None.

    None means "this native type has no text export we request" (a Drawing, a
    Form): the connector then skips its content rather than downloading a binary
    it cannot extract. A non-native type is a programming error here -- callers
    gate on :func:`is_google_native` first -- so it also returns None.
    """
    return _EXPORT_TARGETS.get(mime_type)


def _all_drives_params() -> Dict[str, object]:
    """The BOTH-flags pair every My-Drive-and-Shared-Drive read must carry.

    ``supportsAllDrives`` alone lets the call ACCEPT a shared-drive item id but
    does NOT include shared-drive items in a LIST; ``includeItemsFromAllDrives``
    alone is rejected without ``supportsAllDrives``. Only the pair searches both
    corpora, so they are minted together here and never one without the other --
    that is the exact silent-My-Drive-only bug the split invites.
    """
    return {"supportsAllDrives": True, "includeItemsFromAllDrives": True}


def build_list_files(
    *,
    page_token: Optional[str] = None,
    page_size: int = 100,
    query: Optional[str] = None,
    drive_id: Optional[str] = None,
    order_by: Optional[str] = None,
) -> DriveRequest:
    """Build a ``files.list`` request over BOTH My Drive and Shared Drives.

    ``drive_id`` scopes the list to one Shared Drive (``corpora=drive`` +
    ``driveId``); omitted, the list spans the user's default corpora
    (``corpora=allDrives``) so a single source can cover My Drive and every
    Shared Drive the subject can see. Both drive flags are always set (see
    :func:`_all_drives_params`).
    """
    params: Dict[str, object] = {
        "fields": LIST_FIELDS,
        "pageSize": _clamp_page_size(page_size),
        **_all_drives_params(),
    }
    if drive_id:
        params["corpora"] = "drive"
        params["driveId"] = drive_id
    else:
        params["corpora"] = "allDrives"
    if query:
        params["q"] = query
    if order_by:
        params["orderBy"] = order_by
    if page_token:
        params["pageToken"] = page_token
    return DriveRequest("GET", f"{API_ROOT}/files", params)


def build_get_metadata(file_id: str, *, drive_id: Optional[str] = None) -> DriveRequest:
    """Build a ``files.get`` metadata request (JSON), all-drives aware.

    ``drive_id`` is not a query param on files.get (the id already locates the
    file), but the drive flags still must be set so a shared-drive file id is
    accepted rather than 404'd.
    """
    if not file_id:
        raise ValueError("build_get_metadata requires a non-empty file_id")
    params: Dict[str, object] = {"fields": FILE_FIELDS, **_all_drives_params()}
    return DriveRequest("GET", f"{API_ROOT}/files/{_quote_id(file_id)}", params)


def build_get_media(file_id: str) -> DriveRequest:
    """Build a ``files.get?alt=media`` request -- RAW BYTES of a binary file.

    For a STORED binary (PDF, image, uploaded office doc) only. A Google native
    type has no bytes and returns 403 fileNotDownloadable here; callers gate on
    :func:`is_google_native` and use :func:`build_export` for those.
    """
    if not file_id:
        raise ValueError("build_get_media requires a non-empty file_id")
    params: Dict[str, object] = {"alt": "media", **_all_drives_params()}
    return DriveRequest("GET", f"{API_ROOT}/files/{_quote_id(file_id)}", params, alt_media=True)


def build_export(file_id: str, export_mime: str) -> DriveRequest:
    """Build a ``files.export`` request -- exported bytes of a native type.

    ``export_mime`` is the target the caller resolved via :func:`export_mime_for`
    (e.g. text/plain for a Doc). Response body is bytes (alt_media True).
    """
    if not file_id:
        raise ValueError("build_export requires a non-empty file_id")
    if not export_mime:
        raise ValueError("build_export requires a target export mimeType")
    params: Dict[str, object] = {"mimeType": export_mime}
    return DriveRequest(
        "GET", f"{API_ROOT}/files/{_quote_id(file_id)}/export", params, alt_media=True
    )


def build_get_start_page_token(*, drive_id: Optional[str] = None) -> DriveRequest:
    """Build ``changes.getStartPageToken`` -- the resync boundary token.

    The returned token is the cursor a later ``changes.list`` resumes from. It is
    fetched (1) once at first sync to establish the baseline, and (2) again as
    the RESYNC boundary whenever a saved page token is rejected -- Google
    documents no TTL or specific error code for an expired token, so re-fetching
    this and resuming from it is the documented recovery, not a guessed retry.
    """
    params: Dict[str, object] = {**_all_drives_params()}
    if drive_id:
        params["driveId"] = drive_id
    return DriveRequest("GET", f"{API_ROOT}/changes/startPageToken", params)


def build_list_changes(
    page_token: str,
    *,
    page_size: int = 100,
    drive_id: Optional[str] = None,
) -> DriveRequest:
    """Build a ``changes.list`` request from a saved page token.

    ``page_token`` is REQUIRED: the change feed is always resumed from a token
    (the start page token on the first incremental round, or the saved
    ``nextPageToken`` thereafter). Both drive flags are set so changes across My
    Drive and Shared Drives are returned. ``includeRemoved`` is on so a deletion
    is observed (a removed change carries ``removed=true``/``fileId``), which the
    connector needs to drop a row whose file was deleted.
    """
    if not page_token:
        raise ValueError(
            "build_list_changes requires a page_token; the change feed is always "
            "resumed from a token (getStartPageToken on the first round, the saved "
            "nextPageToken thereafter)"
        )
    params: Dict[str, object] = {
        "pageToken": page_token,
        "fields": CHANGES_FIELDS,
        "pageSize": _clamp_page_size(page_size),
        "includeRemoved": True,
        **_all_drives_params(),
    }
    if drive_id:
        params["driveId"] = drive_id
    return DriveRequest("GET", f"{API_ROOT}/changes", params)


# Drive caps files.list/changes.list pageSize at 1000 and rejects < 1; clamp
# locally so the client's record of what it asked for matches what it gets.
_MAX_PAGE_SIZE = 1000


def _clamp_page_size(requested: int) -> int:
    if requested > _MAX_PAGE_SIZE:
        return _MAX_PAGE_SIZE
    if requested < 1:
        return 1
    return requested


def _quote_id(file_id: str) -> str:
    """Percent-encode a file id for a path segment.

    Drive file ids are URL-safe base64-ish tokens, but a caller-supplied id (a
    shortcut target, a change fileId) is untrusted input, so it is quoted rather
    than interpolated raw -- a stray ``/`` or ``?`` must not escape the path
    segment and re-point the request.
    """
    from urllib.parse import quote

    return quote(file_id, safe="")


__all__ = [
    "API_ROOT",
    "CHANGES_FIELDS",
    "DriveRequest",
    "FILE_FIELDS",
    "FOLDER_MIME",
    "GOOGLE_APPS_MIME_PREFIX",
    "LIST_FIELDS",
    "SHORTCUT_MIME",
    "build_export",
    "build_get_media",
    "build_get_metadata",
    "build_get_start_page_token",
    "build_list_changes",
    "build_list_files",
    "export_mime_for",
    "is_folder",
    "is_google_native",
    "is_shortcut",
]
