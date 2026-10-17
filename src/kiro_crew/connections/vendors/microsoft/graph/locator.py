"""Microsoft Graph resource locator: build a request path from typed intent.

WHAT THIS OWNS
==============
A Graph operation names its target two ways that are NOT interchangeable:

* ``/me`` -- the signed-in user, resolvable ONLY when a user is signed in
  (delegated auth). Microsoft Graph rejects ``/me`` outright under app-only
  (client-credentials) auth because there is no signed-in user for the token to
  resolve; the call must name ``/users/{id}`` instead. This is a hard vendor
  constraint, so it is enforced here at locator-construction time rather than
  discovered as a 400 from the service.
* ``/users/{id}`` -- an explicit directory object, valid under both delegated
  and app-only auth.

This module is pure logic: it turns a typed :class:`GraphLocator` into a Graph
request path, and refuses the one combination the vendor forbids. It performs no
network call, holds no credential, and resolves no token.

THE PRINCIPAL AXIS IS SEPARATE FROM THE CREDENTIAL MODE
=======================================================
A locator carries a :class:`Principal` (``ME`` or a specific ``USER`` id). The
credential mode under which it will be dispatched is a SEPARATE fact, supplied
at build time as W01's shared
:data:`~kiro_crew.connections.control_plane.operation.CredentialMode`, because
the same locator intent ("read this user's mailbox") is legal under a signed-in
user's credential as ``/me`` and must be respelled as ``/users/{id}`` under an
app-only (``service_to_service``) credential. The refusal lives in
:func:`build_path`, which takes both. This slice defines NO second auth-mode
vocabulary of its own -- :func:`is_app_only` is the one Graph-specific reading
of W01's ``CredentialMode`` axis.

THE FOUR REQUIRED SHAPES
========================
The campaign's W05 base must model these Graph resource shapes. Each is a
:class:`ResourceRef` variant so the path is assembled from validated segments,
never string-concatenated from caller input:

* ``/sites/{id}/lists/{id}/items[/{item}[/fields]]`` -- SharePoint list items
* ``/drives/{id}/items/{id}[/children | /workbook/...]`` -- drive items, with an
  optional child-listing or Excel workbook sub-resource
* ``/users/{id}/onenote/...`` -- OneNote (principal-rooted; ``/me/onenote/...``
  under delegated auth)
* ``/chats/{id}/messages`` -- Teams chat messages (top-level, not principal-rooted)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence, Tuple

from kiro_crew.connections.control_plane.operation import (
    CREDENTIAL_MODE_SERVICE_TO_SERVICE,
    CredentialMode,
)


def is_app_only(credential_mode: CredentialMode) -> bool:
    """Whether a credential mode dispatches with NO signed-in user.

    Graph's ``/me`` resolves against a signed-in user, which W01's
    ``service_to_service`` credential mode (an application/service credential)
    does not have; ``oauth_user`` and ``fine_grained_pat`` both do. This is the
    one Graph-specific reading of W01's shared ``CredentialMode`` axis, kept as
    a named predicate rather than a second auth-mode enum, so the ``/me`` refusal
    switches on W01's single vocabulary.
    """

    return credential_mode == CREDENTIAL_MODE_SERVICE_TO_SERVICE


class Principal(str, Enum):
    """Which principal roots a principal-scoped resource.

    ``ME`` is the signed-in user (resolvable only when a signed-in user exists).
    ``USER`` names an explicit directory object by id and is valid under any
    credential mode.
    """

    ME = "me"
    USER = "user"


class GraphLocatorError(ValueError):
    """A locator could not be built into a valid Graph path.

    A ``ValueError`` subclass so a caller can catch either. This is a shaping
    fault (a forbidden principal/auth combination, an empty required id), NOT a
    vendor error: this slice deliberately defines no vendor-error taxonomy --
    that closed set and its typed envelope are W01's single source, consumed by
    a separate leaf. Do not grow a vendor-error hierarchy here.
    """


def _require_id(value: str, what: str) -> str:
    """Return ``value`` stripped, or raise if it is empty/whitespace.

    Graph ids are opaque strings; this guards only against an absent id, which
    would otherwise assemble a path with an empty segment (``/users//...``).
    """

    stripped = value.strip()
    if not stripped:
        raise GraphLocatorError(f"{what} must be a non-empty id")
    if "/" in stripped:
        # A slash in a single id segment would silently inject extra path
        # structure the caller did not model. Callers pass ONE id per segment.
        raise GraphLocatorError(f"{what} must not contain a path separator: {value!r}")
    return stripped


@dataclass(frozen=True)
class ResourceRef:
    """A validated Graph resource reference: the path AFTER the principal root.

    The classmethod constructors below encode the four required shapes, while
    direct construction remains safe because ``__post_init__`` validates every
    segment and rejects an empty path. ``segments`` is the ordered path below
    the principal root (e.g. ``("onenote", "notebooks")``);
    ``principal_rooted`` says whether the ref hangs off ``/me`` |
    ``/users/{id}`` (True) or off a top-level Graph collection such as
    ``/sites`` or ``/chats`` (False).
    """

    segments: Tuple[str, ...]
    principal_rooted: bool

    def __post_init__(self) -> None:
        if not self.segments:
            raise GraphLocatorError("resource reference must contain at least one segment")
        clean = tuple(_require_id(segment, "resource segment") for segment in self.segments)
        object.__setattr__(self, "segments", clean)

    @classmethod
    def sharepoint_list_items(
        cls,
        *,
        site_id: str,
        list_id: str,
        item_id: Optional[str] = None,
        fields: bool = False,
    ) -> "ResourceRef":
        """``/sites/{id}/lists/{id}/items[/{item}[/fields]]``.

        ``fields`` is only meaningful for a specific item; requesting it without
        an ``item_id`` is a shaping error (Graph has no ``.../items/fields``).
        """

        site = _require_id(site_id, "site_id")
        lst = _require_id(list_id, "list_id")
        segments = ["sites", site, "lists", lst, "items"]
        if item_id is not None:
            segments.append(_require_id(item_id, "item_id"))
            if fields:
                segments.append("fields")
        elif fields:
            raise GraphLocatorError(
                "fields requires an item_id (no /items/fields collection exists)"
            )
        return cls(tuple(segments), principal_rooted=False)

    @classmethod
    def drive_item(
        cls,
        *,
        drive_id: str,
        item_id: str,
        children: bool = False,
        workbook_tail: Optional[Sequence[str]] = None,
    ) -> "ResourceRef":
        """``/drives/{id}/items/{id}[/children | /workbook/...]``.

        ``children`` (list an item's children) and ``workbook_tail`` (an Excel
        workbook sub-path such as ``("workbook", "worksheets")``) are mutually
        exclusive: an item is enumerated either as a folder or as a workbook,
        never both in one path.
        """

        drive = _require_id(drive_id, "drive_id")
        item = _require_id(item_id, "item_id")
        segments = ["drives", drive, "items", item]
        if children and workbook_tail:
            raise GraphLocatorError("children and workbook_tail are mutually exclusive")
        if children:
            segments.append("children")
        elif workbook_tail:
            tail = list(workbook_tail)
            if not tail or tail[0] != "workbook":
                raise GraphLocatorError("workbook_tail must start with 'workbook'")
            for seg in tail:
                segments.append(_require_id(seg, "workbook_tail segment"))
        return cls(tuple(segments), principal_rooted=False)

    @classmethod
    def onenote(cls, *, tail: Sequence[str]) -> "ResourceRef":
        """``/{principal}/onenote/...`` -- principal-rooted OneNote sub-path.

        ``tail`` is the path BELOW ``onenote`` (e.g. ``("notebooks",)`` or
        ``("pages", "{id}", "content")``); the ``onenote`` segment and the
        principal root are added by :func:`build_path`.
        """

        clean = [_require_id(seg, "onenote tail segment") for seg in tail]
        return cls(("onenote", *clean), principal_rooted=True)

    @classmethod
    def chat_messages(cls, *, chat_id: str) -> "ResourceRef":
        """``/chats/{id}/messages`` -- top-level (not principal-rooted)."""

        chat = _require_id(chat_id, "chat_id")
        return cls(("chats", chat, "messages"), principal_rooted=False)


@dataclass(frozen=True)
class GraphLocator:
    """A principal plus a validated resource reference.

    ``principal`` roots a ``principal_rooted`` ref; it is ignored for a
    top-level ref (SharePoint, chats) but still recorded so the intent is
    explicit. ``user_id`` is required when ``principal`` is ``USER`` and unused
    otherwise.
    """

    principal: Principal
    resource: ResourceRef
    user_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.principal is Principal.USER:
            # Validate eagerly so a USER locator can never carry an empty id
            # that would only surface when the path is assembled.
            _require_id(self.user_id or "", "user_id")
        elif self.user_id is not None:
            raise GraphLocatorError("user_id is only valid when principal is USER")


def build_path(locator: GraphLocator, credential_mode: CredentialMode) -> str:
    """Assemble the Graph request path for ``locator`` under ``credential_mode``.

    ``credential_mode`` is W01's shared ``CredentialMode`` axis (``oauth_user`` /
    ``fine_grained_pat`` / ``service_to_service``). Refuses ``/me`` under an
    app-only credential (``service_to_service``, which has no signed-in user for
    ``/me`` to resolve) -- the one combination Microsoft Graph rejects, enforced
    here rather than as a discovered 400. The returned path is root-relative and
    begins with ``/`` (e.g. ``/users/abc/onenote/notebooks``); it does NOT carry
    the API version prefix (``/v1.0``), which the client layer (a separate slice)
    prepends.
    """

    if locator.resource.principal_rooted:
        root = _principal_root(locator, credential_mode)
        parts = [root, *locator.resource.segments]
    else:
        # A top-level ref does not hang off a principal, so the principal never
        # participates in the path -- and a top-level ref therefore cannot be
        # the thing that trips the app-only /me refusal.
        parts = list(locator.resource.segments)
    return "/" + "/".join(parts)


def _principal_root(locator: GraphLocator, credential_mode: CredentialMode) -> str:
    """The ``me`` | ``users/{id}`` root, refusing ``/me`` under an app-only credential."""

    if locator.principal is Principal.ME:
        if is_app_only(credential_mode):
            raise GraphLocatorError(
                "/me is not resolvable under an app-only (service_to_service) "
                "credential (no signed-in user); name /users/{id} instead"
            )
        return "me"
    # Principal.USER -- id already validated in __post_init__.
    assert locator.user_id is not None  # narrowed by __post_init__
    return f"users/{locator.user_id}"
