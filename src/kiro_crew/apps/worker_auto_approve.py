"""Worker auto-approve: the App Kit seam for an app's bounded grant on its OWN worker slots.

An app that drives an unattended agent session (a crew that works an issue queue while
nobody watches) needs that session's tool calls approved without a person clicking. The
grant behind that is a ``SafetyOverride`` scoped grant carried on the slot as
``slot._trust_scope``, which the shared dashboard approval path re-checks on every
approval. This module is the seam for a grant an app holds WITHOUT a worker-slot lease,
and nothing in it names an app. A lease from ``worker_slots.acquire_worker_slot`` with
``trust=True`` mints the same kind of grant on its own leased slot and checks none of
the declaration below, so the guarantees listed here hold for this seam's grants only.

What the seam guarantees, in the order a grant meets it:

* **Declared, shown, approved.** The app's manifest declares
  ``permissions.workerAutoApprove: true``. The trust prompt and the app's detail page
  show it, and it is a grant family of the app's approved-grants file
  (:data:`GRANT_FAMILY`), so an update that ADDS it is held back until the owner
  approves it, like a new API path. :func:`app_holds_worker_auto_approve` reads the
  live manifest, that approval and the record's ``enabled`` on every grant, so a
  disable, an uninstall or a manifest that stops declaring it ends the next grant.
* **Only the app's own slots.** A grant attaches only to a slot whose owner
  (``slot._app``) is the app, never to a slot a person, a cron job or another app
  owns, and never over another holder's scope already on the slot.
* **Only the app's own scopes.** Every scope key is built here as
  ``app:<app>:grant:<name>:autoapprove`` from a short name, so an app can neither mint
  nor renew nor revoke a task run's, a person's or another app's grant.
* **Never above the ceiling.** The grant is ``SafetyOverride.activate_scoped``: refused
  when the approval-mode policy denies ``yolo``, SEL-audited fail-closed before it
  exists, capped at the 24h hard ceiling (and ``yolo_max_seconds``), and re-checked on
  every approval. It only skips the approval PROMPT: governance's ``POLICY ∩ PROFILE``
  gate still runs on every tool call.
* **Revoked with the app.** :func:`revoke_app` deactivates every scope the app holds.
  It runs first in every teardown (disable, uninstall, trust withdrawal) and again when
  the record is written disabled or removed, so a grant minted in between is gone too.

Revocation needs no permission: it only removes access.

The SDK reaches an installed app as ``ctx.auto_approve`` (present only when the
manifest declares the permission). A built-in whose routes hook takes no context
builds :class:`WorkerAutoApprove` itself; the declaration and the checks are the same.
"""

from __future__ import annotations

import logging
import math
import re
import threading
from typing import Any

logger = logging.getLogger(__name__)

#: The grant family in the app's approved-grants file, and its one entry.
GRANT_FAMILY = "workerAutoApprove"
GRANT_ENTRY = "ownSlots"
WORKER_AUTO_APPROVE_GRANT_FAMILIES = (GRANT_FAMILY,)
#: Longest one grant may be minted or slid for: ``SafetyOverride``'s hard ceiling.
MAX_GRANT_TTL_SECS = 24 * 60 * 60
#: A grant name: what the app calls one grant inside its own namespace.
_GRANT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
#: An app id as the scope key may carry it: no ``:``, so a key parses one way.
_APP_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")

#: Guards :data:`_held`.
_lock = threading.Lock()
#: app id -> the scope keys it was granted in this process, for :func:`revoke_app`.
_held: dict[str, set[str]] = {}


def _declares(permissions: Any) -> bool:
    """Whether a manifest's ``permissions`` (parsed or a raw dict) declare the grant."""
    if isinstance(permissions, dict):
        return permissions.get("workerAutoApprove") is True
    return getattr(permissions, "workerAutoApprove", False) is True


def worker_auto_approve_grant_entries(permissions: Any) -> dict[str, list[str]]:
    """The :data:`GRANT_FAMILY` entries a manifest's ``permissions`` declare."""
    return {GRANT_FAMILY: [GRANT_ENTRY] if _declares(permissions) else []}


def _approved(app: str, declared: list[str]) -> bool:
    """Whether the declared entry is in *app*'s approved-grants set, read fresh."""
    if GRANT_ENTRY not in declared:
        return False
    from kiro_crew.apps.manager import staged_app_grants  # noqa: PLC0415 - circular

    return GRANT_ENTRY in staged_app_grants(app, GRANT_FAMILY, lambda: declared)


def app_holds_worker_auto_approve(app: str) -> bool:
    """Whether *app* may hold a worker auto-approve grant right now. Fails closed.

    The live manifest must declare it, the owner must have approved it (an install
    approves what it declares; an update that added it waits), and the record must be
    enabled. Manifest first and ``enabled`` last, as ``app_can_manage_session_approvals``
    reads them: the enabled read that follows a manifest read is at least as new.
    """
    if not isinstance(app, str) or not app:
        return False
    try:
        from kiro_crew.apps.manager import (  # noqa: PLC0415 - circular
            get_app_manifest,
            is_app_enabled,
        )

        manifest = get_app_manifest(app)
        if manifest is None:
            return False
        declared = worker_auto_approve_grant_entries(manifest.permissions)[GRANT_FAMILY]
        if not _approved(app, declared):
            return False
        return is_app_enabled(app)
    except Exception:
        logger.warning(
            "could not resolve the worker auto-approve grant of app %s", app, exc_info=True
        )
        return False


def worker_auto_approve_grant_row(name: str, manifest: Any) -> dict[str, Any]:
    """The ``workerAutoApproveGrant`` key of an app row, absent when it is undeclared.

    ``approved`` is whether the owner's approved set holds it; ``staged`` the
    opposite, which the detail page offers for approval.
    """
    permissions = (
        manifest.get("permissions")
        if isinstance(manifest, dict)
        else getattr(manifest, "permissions", None)
    )
    if not _declares(permissions):
        return {}
    approved = _approved(name, [GRANT_ENTRY])
    return {"workerAutoApproveGrant": {"approved": approved, "staged": not approved}}


def grant_scope(app: str, name: str) -> str:
    """The ``SafetyOverride`` scope key of *app*'s grant *name*. Raises on a bad part."""
    if not isinstance(app, str) or not _APP_ID_RE.fullmatch(app):
        raise ValueError(f"not an app id: {app!r}")
    if not isinstance(name, str) or not _GRANT_NAME_RE.fullmatch(name):
        raise ValueError(f"a grant name is 1-128 letters, digits, '_', '.' or '-', not {name!r}")
    return f"app:{app}:grant:{name}:autoapprove"


def _ttl(ttl_secs: Any) -> int:
    if isinstance(ttl_secs, bool) or not isinstance(ttl_secs, (int, float)):
        raise ValueError(f"ttl_secs must be a number, got {ttl_secs!r}")
    if not math.isfinite(ttl_secs) or ttl_secs <= 0 or ttl_secs > MAX_GRANT_TTL_SECS:
        raise ValueError(
            f"ttl_secs must be above 0 and at most {MAX_GRANT_TTL_SECS}, got {ttl_secs!r}"
        )
    return max(1, math.ceil(ttl_secs))


def _forget(app: str, scope: str) -> None:
    with _lock:
        held = _held.get(app)
        if held is not None:
            held.discard(scope)
            if not held:
                del _held[app]


def revoke_app(app: str) -> int:
    """Deactivate every grant *app* holds in this process. Returns how many. Never raises."""
    with _lock:
        scopes = sorted(_held.pop(app, set()))
    if not scopes:
        return 0
    try:
        from kiro_crew.safety_override import safety_override  # noqa: PLC0415

        so = safety_override()
        for scope in scopes:
            so.deactivate_scope(scope)
    except Exception:
        logger.warning("could not revoke the worker grants of app %s", app, exc_info=True)
        return 0
    logger.info("app %s: revoked %d worker auto-approve grant(s)", app, len(scopes))
    return len(scopes)


class WorkerAutoApprove:
    """*app*'s handle on bounded auto-approve grants for its own worker slots.

    Synchronous and thread-safe: each call is a few in-memory ``SafetyOverride``
    operations plus a fresh read of the app's manifest and record. A grant is named by
    the app (``name``, a short identifier such as a crew id) and lives under
    :meth:`scope`. ``hold`` mints or slides it; ``release`` and ``release_slot`` end
    it. Misuse (a malformed name, a TTL outside ``(0, 24h]``) raises ``ValueError``;
    a refusal returns ``False`` and leaves the slot on interactive approval.
    """

    def __init__(self, app: str) -> None:
        grant_scope(app, "probe")  # the app id must fit a scope key
        self._app = app

    @property
    def app(self) -> str:
        return self._app

    @property
    def source(self) -> str:
        """The ``source`` every grant of this app is audited under."""
        return f"app:{self._app}"

    def scope(self, name: str) -> str:
        """The scope key of this app's grant *name*."""
        return grant_scope(self._app, name)

    def hold(self, slot: Any, name: str, *, ttl_secs: float) -> bool:
        """Keep grant *name* live on *slot* for *ttl_secs* more. True if it is usable after.

        A live grant is SLID forward (``renew_scoped``: not audited per call, and never
        past the ceiling counted from its activation); an absent one, or one at its
        ceiling, is minted fresh (``activate_scoped``: audited fail-closed). On success
        ``slot._trust_scope`` names the grant. Refused, with nothing granted, when the
        slot is not this app's, when it carries another holder's scope, or when
        :func:`app_holds_worker_auto_approve` is False; that last refusal also ends a
        grant the app still held on the slot.
        """
        scope = self.scope(name)
        ttl = _ttl(ttl_secs)
        if str(getattr(slot, "_app", "") or "") != self._app:
            logger.warning(
                "app %s: auto-approve refused on slot %r, which the app does not own",
                self._app,
                getattr(slot, "key", ""),
            )
            return False
        current = str(getattr(slot, "_trust_scope", "") or "")
        if current and current != scope:
            return False
        if not app_holds_worker_auto_approve(self._app):
            self.release(name, slot)
            return False
        from kiro_crew.safety_override import safety_override  # noqa: PLC0415

        so = safety_override()
        granted = False
        if so.is_scope_active(scope):
            granted = bool(so.renew_scoped(scope, source=self.source, ttl=ttl).renewed)
        if not granted:
            granted = bool(so.activate_scoped(scope, source=self.source, ttl=ttl).active)
        if not granted:
            if current == scope:
                slot._trust_scope = ""
            _forget(self._app, scope)
            return False
        with _lock:
            _held.setdefault(self._app, set()).add(scope)
        slot._trust_scope = scope
        # A disable that revoked between the check above and the mint would otherwise
        # leave this grant alive; the record is re-read now the scope is registered.
        if not app_holds_worker_auto_approve(self._app):
            self.release(name, slot)
            return False
        return True

    def release(self, name: str, slot: Any = None) -> bool:
        """End grant *name*, and clear it from *slot* if it carries it. Idempotent.

        Returns whether anything was live or cleared.
        """
        from kiro_crew.safety_override import safety_override  # noqa: PLC0415

        scope = self.scope(name)
        so = safety_override()
        # ``scope_remaining_secs`` and not ``is_scope_active``: the latter expires a
        # lapsed grant and writes a ``scope_expired`` SEL row for one being revoked.
        live = so.scope_remaining_secs(scope) > 0
        so.deactivate_scope(scope)
        _forget(self._app, scope)
        cleared = False
        if slot is not None and str(getattr(slot, "_trust_scope", "") or "") == scope:
            slot._trust_scope = ""
            cleared = True
        return live or cleared

    def release_slot(self, slot: Any) -> bool:
        """Clear whatever scoped grant this app's own *slot* carries. Idempotent.

        A scope in this app's namespace is also deactivated. A scope outside it is
        only taken off the slot, never deactivated, because it is another holder's
        grant. A slot the app does not own is left alone. Returns whether anything
        was cleared.
        """
        if str(getattr(slot, "_app", "") or "") != self._app:
            return False
        scope = str(getattr(slot, "_trust_scope", "") or "")
        if not scope:
            return False
        if scope.startswith(f"app:{self._app}:"):
            from kiro_crew.safety_override import safety_override  # noqa: PLC0415

            safety_override().deactivate_scope(scope)
            _forget(self._app, scope)
        slot._trust_scope = ""
        return True


__all__ = [
    "GRANT_ENTRY",
    "GRANT_FAMILY",
    "MAX_GRANT_TTL_SECS",
    "WORKER_AUTO_APPROVE_GRANT_FAMILIES",
    "WorkerAutoApprove",
    "app_holds_worker_auto_approve",
    "grant_scope",
    "revoke_app",
    "worker_auto_approve_grant_entries",
    "worker_auto_approve_grant_row",
]
