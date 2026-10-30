"""The one Preview opt-in card a crewmate's first ``request_capabilities`` raises.

A crewmate's Dynamic capabilities flag ships off
(:func:`kiro_crew.crewmate_dynamic_capabilities.restricted_for`), and
``request_capabilities`` stays mounted while it is off. The first call with the
flag off raises this card instead of the capabilities card: ONE
``crewmate.update`` writing ``agents.<name>.dynamic_capabilities = true``, which
only the owner's button applies (**Turn on** / **Not now**). The request that
raised it is held here; once the user turns the preview on, the gateway raises
that request's capabilities card on its own, so the crewmate does not ask again
(``member_self.continue_after_opt_in``).

It is asked once per crewmate. ``~/.kiro/mates/tool-access.json`` records the
card per crewmate name, and its outcome once the card is finished: ``applied``,
``declined`` (**Not now**), ``expired`` or ``failed``. A crewmate with any entry
is never offered the card again, whatever the outcome; a card still waiting is
not raised twice. After a decline, ``request_capabilities`` with the flag off is
refused with a plain line until the user turns the preview on in settings.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import kiro_crew.config.paths as paths_mod

logger = logging.getLogger(__name__)

#: The fields the card writes: the crewmate's own preview flag, on.
CARD_FIELDS = {"dynamic_capabilities": True}

#: Recorded outcomes, from the card's own status.
STATE_OFFERED = "offered"
STATE_APPLIED = "applied"
STATE_DECLINED = "declined"
STATE_EXPIRED = "expired"
STATE_FAILED = "failed"
_STATUS_TO_STATE = {
    "applied": STATE_APPLIED,
    "partial": STATE_APPLIED,
    "undone": STATE_DECLINED,
    "cancelled": STATE_DECLINED,
    "expired": STATE_EXPIRED,
    "failed": STATE_FAILED,
}

_lock = threading.Lock()


def is_tool_access_card(params: Any) -> bool:
    """Whether a ``crewmate.update`` card's *params* are this card: the flag on, nothing else."""
    return isinstance(params, dict) and params.get("fields") == CARD_FIELDS


def _path() -> Path:
    return paths_mod.mate_agents_dir().parent / "tool-access.json"


def _read() -> dict[str, Any]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict[str, Any]) -> None:
    from kiro_crew.atomic_write import atomic_write

    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n", restrict_to_owner=True)


def entry(member: str) -> dict[str, Any] | None:
    """*member*'s recorded offer, or ``None`` when it was never asked. Blocking."""
    found = _read().get(member)
    return found if isinstance(found, dict) else None


def settle(member: str, card_status: str | None) -> dict[str, Any] | None:
    """Record the finished outcome of *member*'s card from its *card_status*. Blocking.

    A card still pending or applying, or one the store does not hold (``None``),
    leaves the entry as it is. Returns the entry after the update.
    """
    with _lock:
        data = _read()
        found = data.get(member)
        if not isinstance(found, dict):
            return None
        state = _STATUS_TO_STATE.get(str(card_status or ""))
        if state and found.get("state") == STATE_OFFERED:
            found = {**found, "state": state, "settled_at": int(time.time())}
            data[member] = found
            _write(data)
        return found


#: How long an offer recorded before its card waits for the card's id; past it, a
#: reservation that never got one (the gateway stopped between the two writes)
#: counts as a card the store does not hold.
RESERVATION_GRACE_SECS = 300


def reserve_offer(member: str, held: dict[str, Any]) -> bool:
    """Record *member*'s offer, holding the request *held*, BEFORE its card is raised.

    *held* is the ``request_capabilities`` call that raises the card (its params
    and reason), raised as its own card once the user turns the preview on.
    ``False`` when *member* already has an entry (asked once, so no second card).
    Blocking; a failed write raises, and the card is then not raised.
    """
    with _lock:
        data = _read()
        if isinstance(data.get(member), dict):
            return False
        data[member] = {"state": STATE_OFFERED, "card_id": "", "at": int(time.time()), "held": held}
        _write(data)
        return True


def bind_card(member: str, card_id: str) -> None:
    """Name the card *member*'s reserved offer raised. Blocking; never raises.

    A lost write leaves the reservation waiting (:func:`awaiting_card`) until
    :data:`RESERVATION_GRACE_SECS` pass; the held request is kept either way.
    """
    try:
        with _lock:
            data = _read()
            found = data.get(member)
            if isinstance(found, dict) and not found.get("card_id"):
                data[member] = {**found, "card_id": card_id}
                _write(data)
    except Exception:  # noqa: BLE001 - the reservation and its held request stand
        logger.warning("tool access: card of %s not recorded", member, exc_info=True)


def release_offer(member: str) -> None:
    """Drop *member*'s reservation whose card was never raised. Blocking; never raises."""
    try:
        with _lock:
            data = _read()
            found = data.get(member)
            if isinstance(found, dict) and not found.get("card_id"):
                del data[member]
                _write(data)
    except Exception:  # noqa: BLE001 - the reservation then ages out (awaiting_card)
        logger.warning("tool access: reservation of %s not dropped", member, exc_info=True)


def awaiting_card(found: dict[str, Any]) -> bool:
    """Whether *found* is a fresh reservation whose card id is not recorded yet."""
    if found.get("card_id"):
        return False
    try:
        at = int(found.get("at") or 0)
    except (TypeError, ValueError):
        return False
    return time.time() - at < RESERVATION_GRACE_SECS


def take_held(member: str) -> dict[str, Any] | None:
    """Remove and return the request *member*'s opt-in card held, ``None`` when none. Blocking.

    Taken once: a second call (a retried apply) finds nothing, so the held
    request is raised at most once.
    """
    with _lock:
        data = _read()
        found = data.get(member)
        if not isinstance(found, dict) or not isinstance(found.get("held"), dict):
            return None
        held = found["held"]
        data[member] = {k: v for k, v in found.items() if k != "held"}
        _write(data)
        return held
