"""Google Drive (v3) provider semantics for the connector stack.

This subpackage owns the VENDOR reading of Google Drive: how a Drive operation
becomes a concrete Drive v3 request (method, URL, query params, headers), how
its two pagination contracts (``files.list`` / ``changes.list`` ``nextPageToken``
and the ``changes.getStartPageToken`` / ``newStartPageToken`` resync boundary)
are read, how a Drive HTTP failure maps onto the shared neutral error classes,
and how a query-time permission probe answers "does THIS subject still have
access to THIS file right now". Nothing above the wire lives here: the knowledge
per-row ingest contract and its query-time gate are owned by
``kiro_crew.knowledge`` and merely CONSUME what this package produces.

Why a dedicated subpackage (mirrors ``vendors/github/`` and
``vendors/microsoft/``): each provider's paging spelling, error markers, and
resource locator differ, and W01's executor/production seam deliberately INJECTS
the vendor request-builder + response-decode rather than guessing one provider's
shape. This package is the Google Drive fill of that seam.

Drive-specific facts encoded across the modules here, each from Google's own
Drive API v3 documentation:

* **Shared drives need BOTH flags.** ``files.list`` / ``files.get`` /
  ``changes.list`` must set ``supportsAllDrives=true`` AND
  ``includeItemsFromAllDrives=true`` to see Shared Drive content; setting only
  one silently restricts the result to My Drive. See :mod:`.drive_api`.
* **Native Google types export, binary types download.** A Google Docs/Sheets/
  Slides file (``mimeType`` beginning ``application/vnd.google-apps.``) has no
  bytes to download and MUST be fetched via ``files.export`` with a target
  ``mimeType``; a stored binary (PDF, image, uploaded .docx) is fetched via
  ``files.get`` with ``alt=media``. See :mod:`.drive_api`.
* **A shortcut is a pointer, not content.** A file whose ``mimeType`` is
  ``application/vnd.google-apps.shortcut`` carries no content of its own; its
  ``shortcutDetails.targetId`` must be resolved and the TARGET fetched. See
  :mod:`.client`.
* **Incremental sync is token-based, and the token's TTL/failure code are NOT
  documented.** Drive's change feed advances on ``changes.list``'s
  ``nextPageToken`` and closes a batch with ``newStartPageToken``; when a saved
  page token is no longer valid the documented remedy is to fetch a fresh start
  page token and treat that as a resync boundary. Google publishes neither a TTL
  nor a specific error code for an expired token, so this package does NOT invent
  either -- it treats token invalidation as "re-fetch the start page token,
  resync" and the spec records the documentation gap. See :mod:`.pagination`.
"""

from __future__ import annotations

__all__: list[str] = []
