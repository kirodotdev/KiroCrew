"""Web Push subscription store.

Persists browser push subscriptions so the delivery sink can fan a
notification out to every registered endpoint. Mirrors the concurrency and
durability discipline of ``notifications/settings.py``: a module lock
serializes writers, readers are lock-free because every mutation rebinds the
in-memory dict wholesale, and a corrupt file degrades to empty rather than
taking down the gateway.

Stored in ``~/.kiro/crew/web-push/subscriptions.json`` keyed by the subscription
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
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir
from kiro_crew.link_unfurl import UnfurlRejected, vet_unfurl_url
from kiro_crew.platform_owner_compat import make_owner_only_dir

logger = logging.getLogger(__name__)

#: The crew-home directory both Web Push stores live in. The sandbox masks it as a
#: whole and pre-creates it before every spawn (``sandbox_leaves._CREW_HIDDEN_LEAVES``,
#: ``sandbox_precreate._CREW_PRECREATE_HIDDEN_DIR_LEAVES``), so the files and the
#: ``atomic_write`` temps they publish through are never visible in a sandbox.
WEB_PUSH_DIR_LEAF = "web-push"
_STORE_FILENAME = "subscriptions.json"
# Serializes WRITERS only; readers are lock-free (update paths rebind the dict).
_lock = threading.Lock()


def web_push_dir() -> Path:
    """Return the owner-only Web Push directory, creating it when absent."""
    path = config_dir() / WEB_PUSH_DIR_LEAF
    make_owner_only_dir(path)
    return path


def _store_path() -> Path:
    return config_dir() / WEB_PUSH_DIR_LEAF / _STORE_FILENAME


class PushSubscriptionError(ValueError):
    """A subscription payload is missing the endpoint or key material."""


def _reject_internal_endpoint(endpoint: str) -> None:
    """Raise PushSubscriptionError unless *endpoint* is a public HTTPS URL.

    A subscription endpoint is an attacker-supplied URL the delivery sink later
    POSTs encrypted data to. Left unchecked it is a blind-SSRF primitive: an
    authenticated caller could register ``https://127.0.0.1:.../…`` or a
    ``169.254.169.254`` metadata address and have the gateway fan notifications
    into internal services. Real push services (Apple/Mozilla/Google) are always
    public HTTPS, so we require ``https://`` and hand the rest to the one owner of
    the outbound address rule, :func:`link_unfurl.vet_unfurl_url`: it resolves the
    host once and refuses it if ANY answer is non-public. The port allowlist is a
    link-preview concern, so it is lifted; the address rule is not.
    """
    if urlparse(endpoint).scheme != "https":
        raise PushSubscriptionError("subscription.endpoint must be an https URL")
    try:
        vet_unfurl_url(endpoint, allow_any_port=True)
    except UnfurlRejected as exc:
        raise PushSubscriptionError("subscription.endpoint must be a public https host") from exc


def _validate(subscription: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return (endpoint, normalized entry) or raise PushSubscriptionError.

    A valid PushSubscription JSON carries an ``endpoint`` URL and a ``keys``
    object with ``p256dh`` and ``auth`` (both base64url). Anything else cannot
    receive an encrypted push, so it is rejected loudly rather than stored dead.
    The endpoint must additionally be a public HTTPS URL (see
    :func:`_reject_internal_endpoint`) so it cannot be used as an SSRF target.
    """
    if not isinstance(subscription, dict):
        raise PushSubscriptionError("subscription must be an object")
    endpoint = subscription.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint:
        raise PushSubscriptionError("subscription.endpoint must be an https URL")
    _reject_internal_endpoint(endpoint)
    keys = subscription.get("keys")
    if not isinstance(keys, dict):
        raise PushSubscriptionError("subscription.keys is required")
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not isinstance(p256dh, str) or not isinstance(auth, str) or not p256dh or not auth:
        raise PushSubscriptionError("subscription.keys must contain p256dh and auth")
    return endpoint, {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}


def _has_push_shape(entry: dict[str, Any]) -> bool:
    """Whether a persisted entry has an https endpoint and both key strings."""
    endpoint = entry.get("endpoint")
    keys = entry.get("keys")
    return (
        isinstance(endpoint, str)
        and urlparse(endpoint).scheme == "https"
        and isinstance(keys, dict)
        and isinstance(keys.get("p256dh"), str)
        and isinstance(keys.get("auth"), str)
    )


class PushSubscriptionStore:
    """Load/store browser push subscriptions. One instance per gateway."""

    def __init__(self) -> None:
        # Construction is in memory only: ``DashboardState.__init__`` builds the
        # store on the gateway loop, so the file is read on first use instead,
        # and every caller reaches the store from a worker thread.
        self._subs: dict[str, dict[str, Any]] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        """Read the persisted subscriptions once, on the first call that needs them."""
        if self._loaded:
            return
        with _lock:
            if not self._loaded:
                self._load()
                self._loaded = True

    def _load(self) -> None:
        """Read the persisted subscriptions; a corrupt file degrades to empty.

        No DNS here. The address rule is enforced where it matters, at connect
        time, by the delivery resolver (``web_push.PublicAddressResolver``), so
        an entry whose name later resolves to an internal address is never
        reached. Load keeps only the syntactic shape a push needs.
        """
        path = _store_path()
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                raw = data.get("subscriptions", {})
                if isinstance(raw, dict):
                    self._subs = {
                        ep: dict(entry)
                        for ep, entry in raw.items()
                        if isinstance(entry, dict) and _has_push_shape(entry)
                    }
        except Exception:
            logger.warning("Failed to load %s; starting empty", path, exc_info=True)
            self._subs = {}

    def all(self) -> list[dict[str, Any]]:
        """Snapshot of every stored subscription (lock-free once loaded)."""
        self._ensure_loaded()
        return [dict(entry) for entry in self._subs.values()]

    def add(self, subscription: dict[str, Any]) -> dict[str, Any]:
        """Store (or refresh) a subscription keyed by endpoint. Returns the entry.

        Re-subscribing the same endpoint (the browser rotates key material or
        re-runs subscribe on load) overwrites in place rather than duplicating,
        so a device receives exactly one push.
        """
        endpoint, entry = _validate(subscription)
        self._ensure_loaded()
        with _lock:
            candidate = dict(self._subs)
            candidate[endpoint] = entry
            # Owner-only: each entry holds a device's auth + p256dh secret.
            web_push_dir()
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
        self._ensure_loaded()
        with _lock:
            if endpoint not in self._subs:
                return False
            candidate = dict(self._subs)
            candidate.pop(endpoint, None)
            web_push_dir()
            atomic_write(
                _store_path(),
                json.dumps({"subscriptions": candidate}, indent=2),
                restrict_to_owner=True,
            )
            self._subs = candidate
            return True
