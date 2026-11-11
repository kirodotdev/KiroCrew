"""Tell the user in the bell when a boot refuses an app over its signature.

A boot under a fleet ``require_signature`` policy refuses an app whose signature
this fleet cannot verify (unsigned, an untrusted signer, or a signature that does
not verify, such as one made with an older scheme) for that boot only, and turns
off for good an app whose packaged files changed after a trusted key signed them.
Either way the app stops working, so a log line alone leaves the user with an app
that silently does nothing. This raises one note per refused app per boot, naming
the app, what happened and the next step.

Split in two halves because they run in different places. The boot pass runs on
the subprocess executor and calls :func:`record_boot_refusal`; ``start_dashboard``
then calls :func:`deliver_boot_refusal_notes` on the serving loop, where
``state.notify`` must run (see ``update_notify``). The same ``system.apps``
channel as the app update note, so a user who mutes app notes mutes these too.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from kiro_crew.apps.admission import (
    CONTENT_MISMATCH,
    INVALID_SIGNATURE,
    UNSIGNED,
    UNTRUSTED_SIGNER,
    signature_refusal_kind,
)

logger = logging.getLogger(__name__)

#: Upper bound on notes held between a boot pass and its delivery, so a caller
#: that runs boot passes without ever delivering cannot grow this without limit.
_MAX_PENDING = 64

_pending: dict[str, str] = {}
_pending_lock = threading.Lock()

_KEEPS_RUNNING_LATER = "It stays installed and enabled, and starts once this is fixed."

#: Title suffix and body per kind. The app's NAME leads the title, as in the app
#: update note: it is the validated identifier the note's link opens.
_COPY: dict[str, tuple[str, str]] = {
    UNSIGNED: (
        "was not started: it is unsigned",
        "This computer only runs signed apps. "
        + _KEEPS_RUNNING_LATER
        + " Ask its publisher for a signed release.",
    ),
    UNTRUSTED_SIGNER: (
        "was not started: its signer is not trusted",
        "This computer only runs apps signed by a key in the fleet's trust_keys, and"
        " this app's signer is not one of them. "
        + _KEEPS_RUNNING_LATER
        + " Ask your administrator to add the signer to trust_keys, or install a"
        " release signed by a trusted key.",
    ),
    INVALID_SIGNATURE: (
        "was not started: its signature does not verify",
        "Its signature was made with an older signing scheme, or its app.json changed"
        " after signing. "
        + _KEEPS_RUNNING_LATER
        + " Reinstall or update it from its source, or ask its publisher for a release"
        " signed with the current scheme.",
    ),
    CONTENT_MISMATCH: (
        "was turned off: its files do not match its signature",
        "The app's files are not the set a trusted key signed, so the app was"
        " disabled. Reinstall or update it from its source, then enable it again.",
    ),
}


def record_boot_refusal(name: str, reason: Optional[str]) -> None:
    """Hold a note for *name* when *reason* is a signature refusal. Never raises."""
    kind = signature_refusal_kind(reason)
    if kind not in _COPY:
        return
    with _pending_lock:
        if name in _pending or len(_pending) < _MAX_PENDING:
            _pending[name] = kind
            return
    logger.debug("boot refusal note for app %r dropped: %d notes already held", name, _MAX_PENDING)


def take_boot_refusal_notes() -> list[dict[str, Any]]:
    """The held notes, oldest first, emptying the hold."""
    with _pending_lock:
        held = list(_pending.items())
        _pending.clear()
    notes = []
    for name, kind in held:
        suffix, body = _COPY[kind]
        notes.append(
            {
                "title": f"{name} {suffix}",
                "body": body,
                "meta": {"app": name, "signatureRefusal": kind},
                "url": f"/apps/detail/{name}",
            }
        )
    return notes


def deliver_boot_refusal_notes(state: Any) -> list[str]:
    """Send the held notes through ``state.notify``. Call ON the serving loop.

    Returns the app names delivered. One note failing does not stop the rest, and
    nothing raises: boot must not fail on a note.
    """
    notes = take_boot_refusal_notes()
    if not notes:
        return []
    try:
        # Local import: update_notify reads the registry catalog, which boot need not load early.
        from kiro_crew.apps.update_notify import NOTE_CHANNEL, NOTE_KIND
    except Exception:  # noqa: BLE001 - the refusals stand; their log lines already name them
        logger.warning("could not load the app note channel for boot refusal notes", exc_info=True)
        return []

    delivered: list[str] = []
    for note in notes:
        app = note["meta"]["app"]
        try:
            state.notify(
                NOTE_KIND,
                note["title"],
                note["body"],
                meta=note["meta"],
                url=note["url"],
                channel=NOTE_CHANNEL,
            )
        except Exception:  # noqa: BLE001 - the refusal stands; the log line already names it
            logger.warning("could not deliver the boot refusal note for app %r", app, exc_info=True)
            continue
        delivered.append(app)
    return delivered
