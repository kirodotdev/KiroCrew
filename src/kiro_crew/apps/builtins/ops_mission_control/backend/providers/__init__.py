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
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.ops_mission_control.backend.models import CorruptDocumentError
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


def _config_path() -> Path:
    return app_data_dir(APP_NAME) / CONFIG_FILENAME


def read_config() -> dict[str, Any]:
    """Non-secret app config, or ``{}`` when absent/corrupt.

    A DISPLAY/LOOKUP read: every accessor below it resolves to the caller's
    default, so an unreadable config reads as "nothing configured" and an adapter
    that needs a value stays disabled rather than 500ing the Settings page. See
    :func:`_read_config_for_update` for why a writer may not stand on the same
    answer.

    An absent file is silent -- that is a fresh install, not a fault. Anything else
    is logged, because this degradation is the quietest one in the app: every
    provider stops polling while their credentials stay in the keystone store, so
    Settings still shows each one as configured and nothing reports that the app
    has gone deaf.
    """
    try:
        data = json.loads(_config_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        logger.warning(
            "ops-mission-control: app config unreadable; every provider will read as "
            "unconfigured and polling will stop",
            exc_info=True,
        )
        return {}
    if isinstance(data, dict):
        return data
    # A non-object root (`[]`, a bare string, `null`) parses fine, so no handler above fires
    # -- and this branch answered `{}` in silence while the docstring above promised
    # "anything else is logged". Worse than an ordinary stale comment: the sentence it
    # contradicts is the one arguing that THIS degradation must not be quiet, because every
    # provider stops polling while Settings still shows them configured. Found in review
    # (GPT 5.6). The index display read grew the same warning a head earlier; this is its twin.
    logger.warning(
        "ops-mission-control: app config root is not an object; every provider will read as "
        "unconfigured and polling will stop",
    )
    return {}


def _read_config_for_update() -> dict[str, Any]:
    """The config a read-modify-write is allowed to publish over.

    ``merge_provider_config`` and ``set_top_level`` both rewrite the WHOLE file
    from what they read -- the merge is per-FIELD within one provider slot, not
    per-file -- so an empty base is not "nothing to carry forward", it is "drop
    every other provider's configuration and every top-level key". Only a MISSING
    file makes that true; an unreadable one (a transient EACCES/EIO, a scanner
    holding the handle on Windows) is config we still have.

    The loss is quiet and it disables detection: ``provider_enabled`` defaults to
    ``False``, so a wiped config does not error, it just stops polling every
    provider the operator had switched on -- while their tokens remain in the
    keystone store, so the Settings page still shows each provider as
    credentialed. The operator is left with an app that reports no signals and
    looks configured.

    Corruption propagates too -- the same deliberate divergence from the four merged
    siblings of this idiom that :func:`store._read_index_for_update` explains. A
    half-written or hand-broken config still names every provider the operator
    enabled; replacing it discards that and leaves them re-entering settings they
    already chose. Refusing surfaces the corruption instead.
    """
    try:
        data = json.loads(_config_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        # Re-raised as the named type so every refusal from this reader is one
        # `CorruptDocumentError` regardless of which door it came through. See the matching
        # clause in `store._read_index_for_update` for the reasoning.
        raise CorruptDocumentError(exc.msg, exc.doc, exc.pos) from exc
    except UnicodeDecodeError as exc:
        # Not valid UTF-8, so `json.loads` never runs. `UnicodeDecodeError` is a `ValueError`
        # but not a `JSONDecodeError`, so unwrapped it would slip past every corruption clause
        # into the tolerant arms and be swallowed. Found in review (GPT 5.6).
        raise CorruptDocumentError(
            f"app config is not valid UTF-8: {exc.reason}",
            exc.object.decode("utf-8", "replace")[:120],
            0,
        ) from exc
    if not isinstance(data, dict):
        # Valid JSON that is not an object -- `[]`, a bare string, `null` -- parses without
        # raising, so normalizing it to `{}` here would let the mutation rewrite the whole
        # file from empty and destroy a config nobody could read. That is the same loss this
        # reader exists to prevent, reached without a parse failure. Reported as a decode
        # error because the document IS unusable, and because every caller already routes
        # that correctly. Found in review (GPT 5.6).
        raise CorruptDocumentError("app config root is not a JSON object", str(data)[:120], 0)
    return data


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
    path = _config_path()
    atomic_write(path, json.dumps(payload, indent=2, sort_keys=True))


def merge_provider_config(provider_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    """Merge ``updates`` into one provider's config slot and persist.

    Returns the provider's resulting config. A merge rather than a replace so a
    settings form that submits only the field it changed cannot silently drop the
    rest of the provider's configuration.

    Raises ``OSError`` when the existing config could not be read; see
    :func:`_read_config_for_update` for why that is not collapsed to an empty
    document here, which would drop every OTHER provider's slot -- the same loss
    the per-field merge exists to prevent, one level up.
    """
    with _ConfigLock():
        config = _read_config_for_update()
        providers = config.get("providers")
        if providers is None:
            providers = {}
        elif not isinstance(providers, dict):
            # The per-slot half of the wrong-shape door, and it was still open: the strict
            # read refuses a bad ROOT, but a valid-JSON config whose `providers` key is a
            # list or a string was normalized to `{}` right here -- and since this function
            # rewrites the whole file, that wiped every provider slot on the next merge.
            # Exactly the loss `_coerce_index(strict=True)` closed on the index side, one
            # level down. Found in review (Design Review), which noticed the description
            # claimed per-row refusal while the config only had it at the root.
            raise CorruptDocumentError(
                "app config 'providers' is not an object", str(providers)[:120], 0
            )
        slot = providers.get(provider_id)
        if slot is None:
            slot = {}
        elif not isinstance(slot, dict):
            raise CorruptDocumentError(
                f"app config slot {provider_id!r} is not an object", str(slot)[:120], 0
            )
        slot.update(updates)
        providers[provider_id] = slot
        config["providers"] = providers
        write_config(config)
    return slot


def set_top_level(key: str, value: Any) -> None:
    """Set one non-provider config key (autonomy mode, tuning, primary flag).

    Raises ``OSError`` when the existing config could not be read, for the reason
    :func:`_read_config_for_update` gives.
    """
    with _ConfigLock():
        config = _read_config_for_update()
        config[key] = value
        write_config(config)
