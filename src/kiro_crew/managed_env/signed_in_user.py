"""Who the lane is signed in as, for the panel's and the row menu's lines.

The vault stores the kind of account and its start URL, not an e-mail, and many users
share one start URL. So the name comes from the service's ``GetUserInfo`` on the portal
service, with the lane's own token: no new host and no new token destination.

One call per sign-in: the answer is cached on the vault's identity fingerprint, so a poll
makes no second call and a new sign-in makes one. The call runs only with the lane on and a
sign-in the lane's token accepts (``core_signin.sign_in_status``), through the lane's client,
so the guard sees it like any other call.

The answer is untrusted. An ``email`` is shown only when it is printable, has an ``@`` and
is at most 254 characters (the core's ``kiro_usage_api._user_email`` rule). Otherwise the
``userId``'s last 8 characters are shown, else nothing. The e-mail is never logged: log
lines name only which of the three shapes came back, and the call log keeps field names.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from kiro_crew.managed_env.client import PORTAL_SERVICE_PATH

logger = logging.getLogger(__name__)

OP = "GetUserInfo"
_EMAIL_MAX = 254
_TAIL = 8

_lock = threading.Lock()
_cache: dict[str, dict] = {}


def _email(value: object) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value or len(value) > _EMAIL_MAX or "@" not in value or not value.isprintable():
        return ""
    return value


def _tail(value: object) -> str:
    if not isinstance(value, str) or not value.strip().isprintable():
        return ""
    return value.strip()[-_TAIL:]


def _shape(answer: object) -> dict:
    if not isinstance(answer, dict):
        return {"state": "unknown"}
    email = _email(answer.get("email"))
    if email:
        return {"email": email}
    tail = _tail(answer.get("userId"))
    return {"user_id_tail": tail} if tail else {"state": "unknown"}


def read(client: Callable[[], Any]) -> dict:
    """``{"email": …}``, ``{"user_id_tail": …}``, ``{"state": "unknown"}``, or
    ``{"state": "signed_out"}`` with no call when the lane's token wouldn't be accepted.

    *client* returns the lane's ``BffClient``; it is built only when a call is due.
    Blocking (one service call at most); never raises."""
    from kiro_crew.auth.bridge import vault_identity_fingerprint
    from kiro_crew.managed_env import core_signin
    from kiro_crew.managed_env import settings as lane_settings

    if not lane_settings.lane_enabled() or core_signin.sign_in_status()["state"] != "signed_in":
        return {"state": "signed_out"}
    fingerprint = vault_identity_fingerprint()
    with _lock:
        if fingerprint in _cache:
            return dict(_cache[fingerprint])
        try:
            shape = _shape(client().call(OP, {}, service=PORTAL_SERVICE_PATH))
        except Exception as exc:  # noqa: BLE001 - a failed read is "unknown", never a crash
            logger.warning("%s: the account name could not be read (%s)", OP, type(exc).__name__)
            shape = {"state": "unknown"}
        _cache.clear()
        _cache[fingerprint] = shape
        kind = "e-mail" if "email" in shape else "id tail" if "user_id_tail" in shape else "no name"
        logger.info("%s: read the account's %s", OP, kind)
        return dict(shape)
