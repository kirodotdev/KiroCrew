"""A session whose main pane an installed app draws: the ``{app, kind, object}`` binding.

An app declares kinds of session in ``contributes.sessionKinds``
(``apps/manifest_session_kinds.py``). ``POST /api/chat/slots`` with
``session_kind: {"app", "kind"}`` creates an ordinary chat slot bound to that kind, and
the dashboard mounts the kind's ``entry`` in place of the transcript. The binding is
``_ChatSlot._session_kind``: set once at create, carried on the slot's metadata line
(``slot_persistence/metadata_codec.py``) and projected to every client
(``slot_projection.py``), so an idle session comes back as its kind after a restart.

``object`` names the app's own record for this session. The server mints it, so a
client can never point a new session at a record another session already owns, and
it is the only part of the binding that is not a name the manifest declares.

This module holds the binding's grammar (:func:`session_kind_binding`, which every
value read back from a metadata line passes through, since that line is a file the
agent's own tools can write), the mint, and the create's gate
(:func:`session_kind_for_create`): the request grammar, who may ask, and the check
that the named app is installed, enabled and declares the kind.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from typing import Any

from aiohttp import web

from kiro_crew.apps.manifest_names import app_name_error
from kiro_crew.apps.manifest_session_kinds import SESSION_KIND_ID_RE, session_kinds_errors

logger = logging.getLogger(__name__)

#: A minted ``object``: 16 random bytes as lowercase hex. Applied with ``.fullmatch()``.
SESSION_KIND_OBJECT_RE = re.compile(r"[0-9a-f]{32}")

#: The refusal code for every session-kind create the route will not make.
INVALID_SESSION_KIND = "invalid_session_kind"


def _app_and_kind(value: dict[object, object]) -> tuple[str, str] | None:
    """*value*'s ``app`` and ``kind`` when both pass their grammar, else None."""
    app, kind = value.get("app"), value.get("kind")
    if not isinstance(app, str) or app_name_error(app) is not None:
        return None
    if not isinstance(kind, str) or SESSION_KIND_ID_RE.fullmatch(kind) is None:
        return None
    return app, kind


def session_kind_binding(value: object) -> dict[str, str] | None:
    """*value* as a binding, or None when it is not one.

    A fresh dict of exactly ``app``, ``kind`` and ``object``, each re-checked: the
    app against the app-name contract, the kind against the manifest's id grammar,
    the object against the minted shape. Extra keys are dropped rather than carried,
    because the binding reaches every client's slot payload.
    """
    if not isinstance(value, dict):
        return None
    named = _app_and_kind(value)
    obj = value.get("object")
    if named is None or not isinstance(obj, str) or not SESSION_KIND_OBJECT_RE.fullmatch(obj):
        return None
    return {"app": named[0], "kind": named[1], "object": obj}


def requested_session_kind(value: object) -> tuple[str, str] | None:
    """The ``(app, kind)`` a create's ``session_kind`` names, or None when malformed."""
    return _app_and_kind(value) if isinstance(value, dict) else None


def mint_session_kind(app: str, kind: str) -> dict[str, str]:
    """A new binding for *app*'s *kind*, with a fresh ``object``."""
    return {"app": app, "kind": kind, "object": secrets.token_hex(16)}


def app_declares_session_kind(app: str, kind: str) -> bool:
    """Whether *app* is installed and enabled and its manifest declares *kind*.

    Reads the installed record and ``app.json`` from disk, so it runs off the event
    loop. The manifest is the one ``GET /api/apps`` serves the create menu from, read
    again here because a self-managed app may rewrite it after install: the kinds
    block must still validate clean, or a kind the menu could not list would still
    be creatable. Any read or parse failure answers False.
    """
    from kiro_crew.apps import manager as app_manager
    from kiro_crew.apps.manifest import AppManifest

    try:
        if not app_manager.is_app_enabled(app):
            return False
        manifest = AppManifest.from_json_file(
            app_manager.app_dir(app) / app_manager.APP_MANIFEST_FILENAME
        )
    except Exception:  # noqa: BLE001 - an unreadable app declares nothing
        logger.debug("session kind %s/%s: the app could not be read", app, kind, exc_info=True)
        return False
    contributes = manifest.contributes
    if session_kinds_errors(
        contributes.sessionKinds,
        bad=contributes.bad_session_kinds,
        dropped=contributes.dropped_session_kinds,
    ):
        return False
    return any(declared.id == kind for declared in contributes.sessionKinds)


def invalid_session_kind_response() -> web.Response:
    """The one 400 every refused session-kind create answers with.

    One body for every reason, so the answer does not say which installed apps
    exist or what they declare to a caller that may not list them.
    """
    return web.json_response(
        {"error": "invalid session kind", "code": INVALID_SESSION_KIND}, status=400
    )


async def session_kind_for_create(
    body: dict[str, Any], *, name: object, owner: bool, request_app: str
) -> tuple[tuple[str, str] | None, web.Response | None]:
    """The ``(app, kind)`` a create binds, or the refusal; ``(None, None)`` for none.

    Asked before the create reads or builds anything else. A kind is bound only on a
    fresh, unnamed, ordinary create: a *name* can reopen a saved session, which would
    give an existing conversation a pane it never had, and an ``ephemeral`` or
    ``artifact`` create is a different kind of session already. The caller is the
    owner's own dashboard (*owner*) or the named app's own token (*request_app*);
    any other app may not open sessions another app draws. Last, off the loop, the
    app must be installed and enabled and must declare the kind.
    """
    raw = body.get("session_kind")
    if raw is None:
        return None, None
    requested = requested_session_kind(raw)
    if (
        requested is None
        or name
        or body.get("ephemeral")
        or body.get("artifact")
        or not (owner or (request_app and request_app == requested[0]))
        or not await asyncio.to_thread(app_declares_session_kind, *requested)
    ):
        return None, invalid_session_kind_response()
    return requested, None
