"""Web Push subscription store.

Persists browser push subscriptions so the delivery sink can fan a
notification out to every registered endpoint. Mirrors the concurrency and
durability discipline of ``notifications/settings.py``: a module lock
serializes writers, readers are lock-free because every mutation rebinds the
in-memory dict wholesale, and a corrupt file degrades to empty rather than
taking down the gateway.

Stored in ``~/.kiro/crew/push_subscriptions.json`` keyed by the subscription
endpoint (globally unique, and what 404/410 pruning keys off)::

    {"subscriptions": {"<endpoint>": {"endpoint": "...",
                                       "keys": {"p256dh": "...", "auth": "..."}}}}

Only the endpoint and its key material are stored — no subscriber identity.
This is a single-owner dashboard, so every subscription belongs to the owner
and a ``user`` tag would be redundant PII persisted to disk and handed out on
export.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

_STORE_FILENAME = "push_subscriptions.json"
# Serializes WRITERS only; readers are lock-free (update paths rebind the dict).
_lock = threading.Lock()


def _store_path():
    return config_dir() / _STORE_FILENAME


class PushSubscriptionError(ValueError):
    """A subscription payload is missing the endpoint or key material."""


def _validate(subscription: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return (endpoint, normalized entry) or raise PushSubscriptionError.

    A valid PushSubscription JSON carries an ``endpoint`` URL and a ``keys``
    object with ``p256dh`` and ``auth`` (both base64url). Anything else cannot
    receive an encrypted push, so it is rejected loudly rather than stored dead.
    """
    if not isinstance(subscription, dict):
        raise PushSubscriptionError("subscription must be an object")
    endpoint = subscription.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint.startswith(("https://", "http://")):
        raise PushSubscriptionError("subscription.endpoint must be an http(s) URL")
    keys = subscription.get("keys")
    if not isinstance(keys, dict):
        raise PushSubscriptionError("subscription.keys is required")
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not isinstance(p256dh, str) or not isinstance(auth, str) or not p256dh or not auth:
        raise PushSubscriptionError("subscription.keys must contain p256dh and auth")
    return endpoint, {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}


class PushSubscriptionStore:
    """Load/store browser push subscriptions. One instance per gateway."""

    def __init__(self) -> None:
        self._subs: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        path = _store_path()
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                raw = data.get("subscriptions", {})
                if isinstance(raw, dict):
                    self._subs = {
                        ep: dict(entry)
                        for ep, entry in raw.items()
                        if isinstance(entry, dict) and isinstance(entry.get("endpoint"), str)
                    }
        except Exception:
            logger.warning("Failed to load %s; starting empty", path, exc_info=True)
            self._subs = {}

    def all(self) -> list[dict[str, Any]]:
        """Snapshot of every stored subscription (lock-free)."""
        return [dict(entry) for entry in self._subs.values()]

    def add(self, subscription: dict[str, Any]) -> dict[str, Any]:
        """Store (or refresh) a subscription keyed by endpoint. Returns the entry.

        Re-subscribing the same endpoint (the browser rotates key material or
        re-runs subscribe on load) overwrites in place rather than duplicating,
        so a device receives exactly one push.
        """
        endpoint, entry = _validate(subscription)
        with _lock:
            candidate = dict(self._subs)
            candidate[endpoint] = entry
            # Owner-only: each entry holds a device's auth + p256dh secret.
            atomic_write(
                _store_path(),
                json.dumps({"subscriptions": candidate}, indent=2),
                restrict_to_owner=True,
            )
            self._subs = candidate
            return dict(entry)

    def remove(self, endpoint: str) -> bool:
        """Delete one subscription by endpoint. Returns whether it existed.

        Idempotent: unsubscribing an already-gone endpoint is a no-op success,
        so the 404/410 pruning path and an explicit user unsubscribe converge.
        """
        with _lock:
            if endpoint not in self._subs:
                return False
            candidate = dict(self._subs)
            candidate.pop(endpoint, None)
            atomic_write(
                _store_path(),
                json.dumps({"subscriptions": candidate}, indent=2),
                restrict_to_owner=True,
            )
            self._subs = candidate
            return True
