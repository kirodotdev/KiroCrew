"""User-session reach: an app's opt-in that lets the owner's own chats reach its sessions.

The session tools refuse every session an app owns (``slot._app``) with
``app_scoped_target``, because the session belongs to its app. An app that runs one
chat session per work item, and wants the owner's general chats to read those
sessions and send them follow-up messages, names the verbs it allows in
``permissions.reachableByUserSessions``:

* ``"read"``: ``session_read_message`` and ``session_summary``.
* ``"send"``: ``session_send``.

No other session tool is offered. Stop, close and the rest stay refused, because they
end or change work the app started and still tracks.

Each verb is an entry of the :data:`GRANT_FAMILY` family in the app's approved-grants
set. An install approves what it declares. An update that adds a verb holds it back
until the owner approves it on the app's page, like a new API path. The family is
registered explicit-only (``point_grants``): a stored set written before this host knew
the key reads as approving no verb, though the manifest may declare one, because the
owner was never shown it. The approval, the update and the re-registration writers all
start from that read, so each verb waits for the owner's approval of that verb.

:func:`prime_user_session_reach` reads the live manifest, that approval and the
record's ``enabled`` in a worker thread, before each gated call, and
:func:`app_allows_user_session_reach` reads only that verdict, so the synchronous gate
does no disk read on the event loop. A disable, an uninstall or a manifest that stops
declaring a verb ends it at the next primed call. A verdict older than
:data:`_VERDICT_TTL_SECS` allows nothing, so a gate that no prime precedes fails closed.

Which callers count as the owner's own chats is the session-control gate's decision
(``dashboard/session_control_parts/app_reach.py``); this module answers only what the
app allows.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)

#: The manifest key under ``permissions``, and the grant family that stores its verbs.
GRANT_FAMILY = "reachableByUserSessions"
USER_SESSION_REACH_GRANT_FAMILIES = (GRANT_FAMILY,)
#: The verbs an app may allow, in the order every list of them is kept.
VERBS = ("read", "send")


def declared_verbs(value: Any) -> list[str]:
    """The known verbs in a raw ``permissions.reachableByUserSessions`` value.

    Anything but a list declares nothing, and an unknown entry is dropped, so a
    malformed value grants less, never more.
    """
    if not isinstance(value, list):
        return []
    return [verb for verb in VERBS if verb in value]


def _declared(permissions: Any) -> list[str]:
    """The verbs a manifest's ``permissions`` (parsed or a raw dict) declare."""
    if isinstance(permissions, dict):
        return declared_verbs(permissions.get(GRANT_FAMILY))
    return declared_verbs(getattr(permissions, GRANT_FAMILY, None))


def user_session_reach_grant_entries(permissions: Any) -> dict[str, list[str]]:
    """The :data:`GRANT_FAMILY` entries a manifest's ``permissions`` declare."""
    return {GRANT_FAMILY: _declared(permissions)}


def _approved(app: str, declared: list[str]) -> list[str]:
    """The *declared* verbs in *app*'s approved-grants set, read fresh."""
    if not declared:
        return []
    # Deferred: the facade's family imports ``point_grants``, which imports this module.
    from kiro_crew.apps.manager import staged_app_grants  # noqa: PLC0415

    approved = set(staged_app_grants(app, GRANT_FAMILY, lambda: declared))
    return [verb for verb in declared if verb in approved]


#: How long a primed verdict stays usable. The gate follows its prime in the same
#: request, so this bounds only how stale a verdict can be when the gate reads it.
_VERDICT_TTL_SECS = 30.0
#: app -> (monotonic time of the prime, the verbs the app allows at that time).
_verdicts: dict[str, tuple[float, frozenset[str]]] = {}
_verdicts_lock = threading.Lock()


def _allowed_verbs(app: str) -> frozenset[str]:
    """The verbs *app* allows now, read from disk. Call it off the event loop.

    The live manifest must declare the verb, the owner must have approved it, and the
    record must be enabled. Manifest first and ``enabled`` last, as
    ``app_can_manage_session_approvals`` reads them: the enabled read that follows a
    manifest read is at least as new. Any failure allows nothing.
    """
    try:
        # Deferred: the facade imports every app module.
        from kiro_crew.apps.manager import (  # noqa: PLC0415
            get_app_manifest,
            is_app_enabled,
        )

        manifest = get_app_manifest(app)
        if manifest is None:
            return frozenset()
        approved = _approved(app, _declared(manifest.permissions))
        if not approved or not is_app_enabled(app):
            return frozenset()
        return frozenset(approved)
    except Exception:
        logger.warning("could not resolve the user-session reach of app %s", app, exc_info=True)
        return frozenset()


def _allowed_verbs_of(apps: list[str]) -> dict[str, frozenset[str]]:
    return {app: _allowed_verbs(app) for app in apps}


async def prime_user_session_reach(apps: Iterable[str]) -> None:
    """Resolve what each of *apps* allows, off the event loop, for the gate to read.

    ``authorize_target`` is synchronous and runs on the loop, so
    :func:`app_allows_user_session_reach` reads only the verdicts this records. Each
    call reads every app again, so a disable, an uninstall or a withdrawn verb takes
    effect at the next primed call.
    """
    names = sorted({app for app in apps if isinstance(app, str) and app})
    if not names:
        return
    primed_at = time.monotonic()
    verdicts = await asyncio.to_thread(_allowed_verbs_of, names)
    with _verdicts_lock:
        for app, verbs in verdicts.items():
            # A slower prime that read before a newer one must not replace its verdict.
            held = _verdicts.get(app)
            if held is None or held[0] <= primed_at:
                _verdicts[app] = (primed_at, verbs)


def app_allows_user_session_reach(app: str, verb: str) -> bool:
    """Whether *app* lets the owner's own chats use *verb* on its sessions now.

    Reads only the verdict :func:`prime_user_session_reach` recorded, so it does no
    disk read on the event loop. Fails closed: no verdict, or one older than
    :data:`_VERDICT_TTL_SECS`, allows nothing.
    """
    if not isinstance(app, str) or not app or verb not in VERBS:
        return False
    with _verdicts_lock:
        entry = _verdicts.get(app)
    if entry is None or time.monotonic() - entry[0] > _VERDICT_TTL_SECS:
        return False
    return verb in entry[1]


def user_session_reach_grant_row(name: str, manifest: Any) -> dict[str, Any]:
    """The ``userSessionReachGrant`` key of an app row, absent when nothing is declared.

    ``approved`` lists the declared verbs the owner's approved set holds; ``staged``
    the declared verbs it does not, which the detail page offers for approval.
    """
    permissions = (
        manifest.get("permissions")
        if isinstance(manifest, dict)
        else getattr(manifest, "permissions", None)
    )
    declared = _declared(permissions)
    if not declared:
        return {}
    approved = _approved(name, declared)
    staged = [verb for verb in declared if verb not in approved]
    return {"userSessionReachGrant": {"approved": approved, "staged": staged}}


__all__ = [
    "GRANT_FAMILY",
    "USER_SESSION_REACH_GRANT_FAMILIES",
    "VERBS",
    "app_allows_user_session_reach",
    "declared_verbs",
    "prime_user_session_reach",
    "user_session_reach_grant_entries",
    "user_session_reach_grant_row",
]
