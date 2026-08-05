"""Ops Mission Control — public provider adapters.

Each module here implements one or more of the four Protocols in ``base.py``. A
companion package ships its own adapters in its own package and registers
them through the ADD-only registry; nothing in this package knows that exists.

Shared helpers live here so every adapter reads its non-secret config and its
keystone-protected secrets the same way — the alternative is six adapters each
inventing their own config lookup, and one of them getting the secret path wrong.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps.manager import app_data_dir
from kiro_crew.atomic_write import atomic_write

logger = logging.getLogger(__name__)

APP_NAME = "ops-mission-control"

#: Non-secret app config. Served unauthenticated over ``/api/apps/<name>/config``
#: (documented Kiro Crew behavior), so NOTHING sensitive may be stored here —
#: tokens live in the keystone store (``secrets.py``).
CONFIG_FILENAME = "config.json"

#: Shared HTTP timeout for provider REST calls. Kept below the per-source poll
#: timeout so a slow provider surfaces as a source-level error rather than
#: blowing the heartbeat's budget.
HTTP_TIMEOUT_SECS = 10.0


#: Lock filename beside config.json — locking the file being atomically replaced is useless
#: (the rename swaps inodes and the lock stays on the old one).
_CONFIG_LOCK_FILENAME = ".config.lock"


class _ConfigLock:
    """Exclusive lock around a read-modify-write of the non-secret app config.

    `merge_provider_config` and `set_top_level` both read the whole config, mutate one key, and
    `write_config` REPLACES the file — so two concurrent settings PUTs each write their key onto
    a stale snapshot and the later atomic replace silently drops the other, both returning 200.
    The four other stores in this app (`store._IndexLock`, `ledger._LedgerLock`,
    `policy_store._PolicyLock`, and the keystone secret backend) already learned this; this was
    the last read-modify-write left unlocked. Found in review.

    Routed through `platform_compat.file_lock` so it works on Windows, where `fcntl` is absent.
    """

    def __init__(self) -> None:
        self._fd: int | None = None

    def __enter__(self) -> "_ConfigLock":
        lock_file = app_data_dir(APP_NAME) / _CONFIG_LOCK_FILENAME
        self._fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
        platform_compat.acquire_lock(self._fd, exclusive=True)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            try:
                platform_compat.release_lock(self._fd)
            finally:
                os.close(self._fd)
                self._fd = None


def read_config() -> dict[str, Any]:
    """Non-secret app config, or ``{}`` when absent/corrupt."""
    path = app_data_dir(APP_NAME) / CONFIG_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def provider_config(provider_id: str) -> dict[str, Any]:
    """The ``providers.<id>`` sub-object of the app config."""
    providers = read_config().get("providers")
    if not isinstance(providers, dict):
        return {}
    slot = providers.get(provider_id)
    return slot if isinstance(slot, dict) else {}


def config_value(provider_id: str, key: str, default: str = "") -> str:
    value = provider_config(provider_id).get(key, default)
    return str(value) if value is not None else default


def config_list(provider_id: str, key: str) -> list[str]:
    value = provider_config(provider_id).get(key)
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


#: Values an operator may plausibly type (or a UI may send) for a true flag.
#: Accepting only the literal ``"true"`` meant ``yes`` / ``1`` / ``True `` / a real
#: JSON boolean all read as false, silently — the setting appeared to have been
#: applied and did nothing.
_TRUTHY = frozenset({"1", "true", "yes", "on", "y", "t"})

#: Explicit false values. Listed rather than inferred as "not truthy" so an
#: unrecognized value can fall back to the caller's default instead of silently
#: reading as off.
_FALSY = frozenset({"0", "false", "no", "off", "n", "f"})


def config_flag(provider_id: str, field: str, *, default: bool = False) -> bool:
    """Read a boolean provider-config field, tolerating how it was written.

    Config arrives from a JSON PUT (so a real ``bool``) or from a text input (so a
    string), and an operator writing ``yes`` means yes. Anything unrecognized falls
    back to ``default`` rather than guessing.
    """
    raw = provider_config(provider_id).get(field)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    # Unrecognized (including empty): fall back rather than guess. Treating garbage as
    # False would silently disable a detection the operator believes they turned on;
    # treating it as True would silently enable one they did not ask for.
    return default


def provider_enabled(provider_id: str) -> bool:
    """Whether the operator has switched this adapter on.

    Defaults to ``False``: an adapter with credentials present but never enabled
    stays quiet. Enabling is an explicit act, so installing the app cannot start
    polling a provider the user has not chosen.

    Goes through ``config_flag`` rather than ``bool(...)``: a bare ``bool`` on a
    string is true for ANY non-empty text, so a config carrying ``"enabled":
    "false"`` (hand-edited, or written by a form that stringifies) would ENABLE the
    provider — the opposite of what it says. Unrecognized text still falls back to
    off, keeping the default-quiet guarantee.
    """
    return config_flag(provider_id, "enabled", default=False)


def write_config(payload: dict[str, Any]) -> None:
    """Replace the non-secret app config.

    SECURITY: this file is served over ``/api/apps/<name>/config`` WITHOUT session
    auth (a documented Kiro Crew behavior apps rely on to bootstrap their UI), so
    nothing sensitive may be written here. Tokens go to the keystone store in
    ``secrets.py``; the route layer rejects any key that looks secret-bearing
    before calling this.
    """
    path = app_data_dir(APP_NAME) / CONFIG_FILENAME
    atomic_write(path, json.dumps(payload, indent=2, sort_keys=True))


def merge_provider_config(provider_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    """Merge ``updates`` into one provider's config slot and persist.

    Returns the provider's resulting config. A merge rather than a replace so a
    settings form that submits only the field it changed cannot silently drop the
    rest of the provider's configuration.
    """
    with _ConfigLock():
        config = read_config()
        providers = config.get("providers")
        if not isinstance(providers, dict):
            providers = {}
        slot = providers.get(provider_id)
        if not isinstance(slot, dict):
            slot = {}
        slot.update(updates)
        providers[provider_id] = slot
        config["providers"] = providers
        write_config(config)
    return slot


def set_top_level(key: str, value: Any) -> None:
    """Set one non-provider config key (autonomy mode, tuning, primary flag)."""
    with _ConfigLock():
        config = read_config()
        config[key] = value
        write_config(config)
