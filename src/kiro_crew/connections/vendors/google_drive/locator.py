"""The Google Drive RequestLocator -- assembly only, no credential, no socket.

W01's production transport INJECTS a :data:`~kiro_crew.connections.control_plane.production.RequestLocator`
to turn ``(descriptor, request_args)`` into one concrete
:class:`~kiro_crew.connections.control_plane.production.HttpRequest`. This module
is the Google Drive fill of that seam and NOTHING more: it maps a Drive
descriptor + its arguments onto the right Drive v3 URL, query params and method
(via :mod:`.drive_api`), and hands that back for W01 to execute.

It deliberately does not, and must not:

* attach any credential -- the transport reveals the vault secret into the
  ``Authorization`` header itself, per call, after this returns; a header set here
  would be a second credential path;
* open a socket, hold a token, or read the vault -- custody is single-sourced in
  W01;
* follow a cursor on its own -- the ``pageToken`` / change ``pageToken`` arrives
  in ``request_args`` (W01's PageWalk appends the cursor), and this only places it
  in the query.

The query string is baked INTO the URL here because W01's ``urllib_http_send``
sends ``request.url`` verbatim. Booleans render as Drive's lowercase
``true``/``false``.
"""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlencode

from kiro_crew.connections.control_plane.operation import OperationDescriptor
from kiro_crew.connections.control_plane.production import HttpRequest

from . import descriptors, drive_api


class DriveLocatorError(ValueError):
    """A request could not be assembled (unknown operation, missing required arg).

    A ValueError so a mis-dispatch is loud at assembly time rather than emitting a
    malformed request. Carries no credential material (there is none here).
    """


def locate(
    *,
    service_id: str,
    credential_mode: str,
    descriptor: OperationDescriptor,
    request_args: Mapping[str, Any],
    request_idempotency_key: str = "",
) -> HttpRequest:
    """The RequestLocator: descriptor + args -> HttpRequest. No credential.

    ``request_args`` carries the operation's parameters (a fileId, an export
    mimeType, a page/change token, an optional driveId/query). W01's PageWalk adds
    the current cursor under ``cursor``; this maps it onto Drive's ``pageToken``
    for the two list operations. Every all-drives read gets both drive flags via
    the :mod:`.drive_api` builders (the flags are minted there so they are never
    split).
    """
    if service_id != descriptors.SERVICE_ID:
        raise DriveLocatorError(f"google_drive locator asked to build a {service_id!r} request")
    op = descriptor["operation_id"]
    args = dict(request_args)
    # W01's PageWalk sends the position under 'cursor'; Drive spells it
    # 'pageToken'. Normalise here so the list ops read one name.
    cursor = args.get("cursor")

    drive_id = _opt_str(args.get("drive_id"))

    if op == descriptors.OP_LIST_FILES:
        req = drive_api.build_list_files(
            page_token=_opt_str(cursor) or _opt_str(args.get("page_token")),
            page_size=int(args.get("page_size", 100)),
            query=_opt_str(args.get("query")),
            drive_id=drive_id,
            order_by=_opt_str(args.get("order_by")),
        )
    elif op == descriptors.OP_GET_METADATA:
        req = drive_api.build_get_metadata(_require(args, "file_id"), drive_id=drive_id)
    elif op == descriptors.OP_EXPORT:
        req = drive_api.build_export(_require(args, "file_id"), _require(args, "export_mime"))
    elif op == descriptors.OP_GET_MEDIA:
        req = drive_api.build_get_media(_require(args, "file_id"))
    elif op == descriptors.OP_LIST_CHANGES:
        token = _opt_str(cursor) or _opt_str(args.get("page_token"))
        if not token:
            raise DriveLocatorError(
                "changes.list requires a page_token (the saved startPageToken or "
                "the walk cursor); the change feed is always resumed from a token"
            )
        req = drive_api.build_list_changes(
            token, page_size=int(args.get("page_size", 100)), drive_id=drive_id
        )
    elif op == descriptors.OP_GET_START_PAGE_TOKEN:
        req = drive_api.build_get_start_page_token(drive_id=drive_id)
    else:
        raise DriveLocatorError(f"no google_drive locator for operation {op!r}")

    return HttpRequest(
        method=req.method,
        url=_bake_query(req.url, req.params),
        headers={},  # NO credential here -- W01 attaches Authorization itself.
        body=None,  # every Drive op used here is a GET read; no body.
    )


def _bake_query(url: str, params: Mapping[str, Any]) -> str:
    if not params:
        return url
    flat = {
        k: ("true" if v is True else "false" if v is False else v)
        for k, v in params.items()
        if v is not None
    }
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{urlencode(flat)}"


def _require(args: Mapping[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise DriveLocatorError(f"missing required argument {key!r} for the Drive request")
    return value


def _opt_str(value: Any) -> "str | None":
    return value if isinstance(value, str) and value else None


__all__ = ["DriveLocatorError", "locate"]
