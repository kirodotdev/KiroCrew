"""Authenticated AutoNudge self-arm and scheduled-message provenance records.

Self-arm records remain in the sandbox-visible ``trust/`` subtree because existing
SEL consumers require that location.  Their reader is deliberately total: any
missing, malformed, or unreadable record refuses a self-arm.

Scheduled composer provenance is gateway-only and one file per loop on Linux.
Exact composer text exists only there, never in the agent-writable AutoNudge store.
Every authority-bearing field is authenticated with a domain-separated HMAC
under the dashboard token secret, while Kiro Crew's Linux namespace hides the
provenance leaf from delegated agents. macOS can delegate agent isolation to Kiro
CLI's internal sandbox, which does not enforce Kiro Crew's hidden leaves, and
Windows has no corresponding Crew filesystem boundary. Persistent scheduled
provenance is therefore refused on both hosts. A malformed file can refuse only
its own deferred message; it cannot make unrelated scheduled messages look
absent. Its reader retries transient ``OSError`` failures, then raises so
delivery reports BUSY instead of deleting an otherwise recoverable message.

Blocking file IO throughout -- async callers offload via ``asyncio.to_thread``.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import math
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home
from kiro_crew.dashboard import token_secret

logger = logging.getLogger(__name__)

SELF_ARM_RECORD_NAME = "autonudge-self-armed.json"
SCHEDULED_MESSAGE_RECORD_NAME = "scheduled-message-provenance"
_LOCK_SUFFIX = ".lock"
# Backward-compatible test seam for the self-arm sibling lock filename.
_LOCK_NAME = f"{SELF_ARM_RECORD_NAME}{_LOCK_SUFFIX}"
_SCHEDULED_READ_ATTEMPTS = 3
_SCHEDULED_PROVENANCE_DOMAIN = b"kiro-crew:scheduled-message:v1\x00"
_SCHEDULED_SANDBOX_CONFIG_PATHS = (
    "agent.sandbox",
    "agent.sandbox_allow_no_isolation",
    "agent.sandbox_allow_unsandboxed_exec",
)
_CONFINEMENT_EPOCH_LOCK = threading.Lock()
_CONFINEMENT_EPOCH_CONFIG: tuple[str, bool, bool] | None = None
_CONFINEMENT_EPOCH_INITIALIZED = False
_CONFINEMENT_EPOCH_ELIGIBLE = False
_CONFINEMENT_EPOCH_INVALIDATED = False


def _scheduled_message_sandbox_config() -> tuple[str, bool, bool] | None:
    """Return the current security posture, or ``None`` when it is unprovable."""
    try:
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG

        config = KiroCrewConfig.load()
        if config.degraded_sections & {DEGRADED_WHOLE_CONFIG, "agent"}:
            return None
        return (
            str(config.agent.sandbox),
            bool(config.agent.sandbox_allow_no_isolation),
            bool(config.agent.sandbox_allow_unsandboxed_exec),
        )
    except Exception:
        logger.warning("scheduled-message sandbox state could not be verified", exc_info=True)
        return None


def _sandbox_config_is_namespace_confined(config: tuple[str, bool, bool] | None) -> bool:
    if not platform_compat.IS_LINUX or config is None:
        return False
    try:
        from kiro_crew import sandbox

        mode = sandbox.effective_sandbox_mode(config[0])
        return mode != "off" and sandbox.detect_backend(config_mode=mode) == "namespace"
    except Exception:
        logger.warning("scheduled-message sandbox state could not be verified", exc_info=True)
        return False


def establish_scheduled_message_confinement_epoch() -> bool:
    """Capture the process boot posture once, before live config can adopt changes.

    An unconfined boot never promotes itself by editing config, and an invalidated
    epoch never becomes valid again in this process. A restart is the only way to
    establish a new epoch after a security-posture change.
    """
    global _CONFINEMENT_EPOCH_CONFIG
    global _CONFINEMENT_EPOCH_INITIALIZED
    global _CONFINEMENT_EPOCH_ELIGIBLE
    with _CONFINEMENT_EPOCH_LOCK:
        if not _CONFINEMENT_EPOCH_INITIALIZED:
            config = _scheduled_message_sandbox_config()
            _CONFINEMENT_EPOCH_CONFIG = config
            _CONFINEMENT_EPOCH_ELIGIBLE = _sandbox_config_is_namespace_confined(config)
            _CONFINEMENT_EPOCH_INITIALIZED = True
        return _CONFINEMENT_EPOCH_ELIGIBLE and not _CONFINEMENT_EPOCH_INVALIDATED


def invalidate_scheduled_message_confinement_epoch() -> None:
    """Permanently disable scheduled provenance for this process generation."""
    global _CONFINEMENT_EPOCH_ELIGIBLE
    global _CONFINEMENT_EPOCH_INVALIDATED
    with _CONFINEMENT_EPOCH_LOCK:
        _CONFINEMENT_EPOCH_ELIGIBLE = False
        _CONFINEMENT_EPOCH_INVALIDATED = True


def scheduled_message_sandbox_config_paths() -> tuple[str, ...]:
    """Config paths whose live mutation invalidates the confinement epoch."""
    return _SCHEDULED_SANDBOX_CONFIG_PATHS


def _reset_scheduled_message_confinement_epoch_for_tests() -> None:
    """Reset process-global epoch state. Test-only seam."""
    global _CONFINEMENT_EPOCH_CONFIG
    global _CONFINEMENT_EPOCH_INITIALIZED
    global _CONFINEMENT_EPOCH_ELIGIBLE
    global _CONFINEMENT_EPOCH_INVALIDATED
    with _CONFINEMENT_EPOCH_LOCK:
        _CONFINEMENT_EPOCH_CONFIG = None
        _CONFINEMENT_EPOCH_INITIALIZED = False
        _CONFINEMENT_EPOCH_ELIGIBLE = False
        _CONFINEMENT_EPOCH_INVALIDATED = False


def scheduled_message_hidden_leaf_confined() -> bool:
    """Whether this process generation has an invariant hidden-leaf boundary."""
    with _CONFINEMENT_EPOCH_LOCK:
        boot_config = _CONFINEMENT_EPOCH_CONFIG
        eligible = _CONFINEMENT_EPOCH_ELIGIBLE
        invalidated = _CONFINEMENT_EPOCH_INVALIDATED
    if boot_config is None or not eligible or invalidated:
        return False
    current = _scheduled_message_sandbox_config()
    # This comparison closes the interval after an external file write but before
    # ConfigWatch dispatches its invalidating subscriber.
    return current == boot_config and _sandbox_config_is_namespace_confined(current)


def scheduled_message_provenance_supported() -> bool:
    """Whether deferred composer provenance remains authentic across restarts.

    The feature requires both Kiro Crew's Linux namespace boundary, which hides
    the provenance leaf from every delegated agent, and a dashboard signing key
    proven persistent across process restarts. Either unverified condition
    refuses minting and verification while ordinary dashboard tokens remain
    available through their existing ephemeral fallback.
    """
    if not scheduled_message_hidden_leaf_confined():
        return False
    try:
        return token_secret.signing_secret_is_persistent()
    except Exception:
        logger.warning("scheduled-message signing-key state could not be verified", exc_info=True)
        return False


def purge_scheduled_message_records_if_unconfined() -> bool:
    """Purge deferred plaintext when this boot cannot hide its provenance leaf."""
    establish_scheduled_message_confinement_epoch()
    if scheduled_message_hidden_leaf_confined():
        return False
    clear_scheduled_message_records()
    return True


def _require_scheduled_message_provenance() -> None:
    if not scheduled_message_hidden_leaf_confined():
        raise OSError("scheduled-message provenance requires Linux filesystem isolation")
    if not token_secret.signing_secret_is_persistent():
        raise OSError("scheduled-message provenance requires a persistent signing key")


@contextlib.contextmanager
def _record_lock(path: Path) -> Iterator[None]:
    """Exclusive lock spanning one read-modify-write transaction."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / f"{path.name}{_LOCK_SUFFIX}"
    with open(lock_path, "a+", encoding="utf-8") as fh:
        with platform_compat.file_lock(fh.fileno(), exclusive=True):
            yield


def self_arm_record_path() -> Path:
    """Absolute path of self-arm records, retained for sandboxed SEL consumers."""
    return data_home() / "trust" / SELF_ARM_RECORD_NAME


def scheduled_message_record_path(record_id: str) -> Path:
    """Gateway-only path for one deferred composer's exact provenance.

    The digest is an opaque stable filename, keeping the record id out of a
    filesystem component while isolating corruption to one scheduled loop.
    """
    digest = hashlib.sha256(str(record_id).encode("utf-8")).hexdigest()
    return data_home() / SCHEDULED_MESSAGE_RECORD_NAME / f"{digest}.json"


def _read_self_arm_record(path: Path) -> dict[str, dict[str, Any]]:
    """Read self-arm entries; every failure is an authorization refusal."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        logger.warning("autonudge self-arm record is unavailable; refusing", exc_info=True)
        return {}
    entries = raw.get("loops") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        return {}
    return {
        str(loop_id): entry
        for loop_id, entry in entries.items()
        if isinstance(entry, dict) and isinstance(entry.get("slot_key"), str)
    }


def _write_self_arm_record(path: Path, entries: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        platform_compat.restrict_dir_to_owner(path.parent)
    except OSError:
        logger.debug("could not tighten mode on %s", path.parent, exc_info=True)
    atomic_write(
        path,
        json.dumps({"version": 1, "loops": entries}, ensure_ascii=False, sort_keys=True),
        fsync=True,
    )


@dataclass(frozen=True)
class ScheduledMessageProvenance:
    """Authoritative composer content held outside the agent-writable loop store."""

    slot_key: str
    message: str
    scheduled_at: float
    completed: bool = False


def _scheduled_message_payload(
    slot_key: str,
    message: str,
    scheduled_at: float,
    *,
    completed: bool,
) -> dict[str, Any]:
    """Canonical authority-bearing fields for one deferred message."""
    return {
        "kind": "scheduled_message",
        "slot_key": str(slot_key),
        "message": message,
        "scheduled_at": float(scheduled_at),
        "completed": completed,
    }


def _scheduled_message_mac(payload: dict[str, Any]) -> str:
    # Windows same-user agents can load the dashboard signing key because the
    # host lacks a hidden-leaf boundary. Refuse there before key access.
    _require_scheduled_message_provenance()
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hmac.new(
        token_secret._get_secret(),
        _SCHEDULED_PROVENANCE_DOMAIN + canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _signed_scheduled_message_entry(
    provenance: ScheduledMessageProvenance,
    **timestamps: Any,
) -> dict[str, Any]:
    payload = _scheduled_message_payload(
        provenance.slot_key,
        provenance.message,
        provenance.scheduled_at,
        completed=provenance.completed,
    )
    return {**payload, **timestamps, "provenance": _scheduled_message_mac(payload)}


def _scheduled_message_from_entry(
    entry: dict[str, Any] | None,
    slot_key: str,
) -> ScheduledMessageProvenance | None:
    """Decode and authenticate one scheduled-message entry, or refuse it."""
    if entry is None or entry.get("kind") != "scheduled_message":
        return None
    if entry.get("slot_key") != str(slot_key):
        return None
    message = entry.get("message")
    scheduled_at = entry.get("scheduled_at")
    completed = entry.get("completed", False)
    if not isinstance(message, str):
        return None
    if (
        not isinstance(scheduled_at, (int, float))
        or isinstance(scheduled_at, bool)
        or not math.isfinite(float(scheduled_at))
        or scheduled_at <= 0
        or not isinstance(completed, bool)
    ):
        return None
    payload = _scheduled_message_payload(
        str(slot_key), message, float(scheduled_at), completed=completed
    )
    mac = entry.get("provenance")
    if not isinstance(mac, str):
        return None
    try:
        if not hmac.compare_digest(mac, _scheduled_message_mac(payload)):
            return None
    except (TypeError, ValueError):
        return None
    return ScheduledMessageProvenance(
        slot_key=str(slot_key),
        message=message,
        scheduled_at=float(scheduled_at),
        completed=completed,
    )


def _read_scheduled_message_record(path: Path) -> dict[str, Any] | None:
    """Read one scheduled record, retrying only transient filesystem failures."""
    for attempt in range(_SCHEDULED_READ_ATTEMPTS):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except ValueError:
            logger.warning("scheduled-message provenance is malformed; refusing", exc_info=True)
            return None
        except OSError:
            if attempt + 1 == _SCHEDULED_READ_ATTEMPTS:
                logger.warning(
                    "scheduled-message provenance is temporarily unreadable", exc_info=True
                )
                raise
            logger.warning("retrying scheduled-message provenance read", exc_info=True)
            continue
        return raw if isinstance(raw, dict) else None
    raise AssertionError("scheduled provenance read exhausted without a result")


def _write_scheduled_message_record(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        platform_compat.restrict_dir_to_owner(path.parent)
    except OSError:
        logger.debug("could not tighten mode on %s", path.parent, exc_info=True)
    atomic_write(path, json.dumps(entry, ensure_ascii=False, sort_keys=True), fsync=True)


def record_self_arm(loop_id: str, slot_key: str) -> None:
    """Record that *loop_id* on *slot_key* was armed by that session's own turn."""
    path = self_arm_record_path()
    with _record_lock(path):
        entries = _read_self_arm_record(path)
        entries[str(loop_id)] = {"slot_key": str(slot_key), "armed_ts": time.time()}
        _write_self_arm_record(path, entries)


def record_scheduled_message(
    record_id: str,
    slot_key: str,
    message: str,
    scheduled_at: float,
) -> None:
    """Record authoritative user-authored content for one scheduled message."""
    _require_scheduled_message_provenance()
    if not isinstance(message, str) or isinstance(scheduled_at, bool):
        raise ValueError("invalid scheduled-message provenance")
    provenance = ScheduledMessageProvenance(
        slot_key=str(slot_key),
        message=message,
        scheduled_at=float(scheduled_at),
    )
    if not math.isfinite(provenance.scheduled_at) or provenance.scheduled_at <= 0:
        raise ValueError("invalid scheduled-message provenance")
    path = scheduled_message_record_path(record_id)
    with _record_lock(path):
        _write_scheduled_message_record(
            path,
            _signed_scheduled_message_entry(provenance, armed_ts=time.time()),
        )


def read_scheduled_message(
    record_id: str,
    slot_key: str,
) -> ScheduledMessageProvenance | None:
    """Read protected scheduled content; missing or malformed means refuse."""
    if not scheduled_message_hidden_leaf_confined():
        # Do not even open a legacy/cross-host plaintext record on a platform
        # where an agent process can reach the same path.
        return None
    _require_scheduled_message_provenance()
    return _scheduled_message_from_entry(
        _read_scheduled_message_record(scheduled_message_record_path(record_id)), slot_key
    )


def replace_scheduled_message(
    record_id: str,
    expected: ScheduledMessageProvenance,
    message: str,
    scheduled_at: float,
) -> bool:
    """CAS-refresh protected content after an authenticated dashboard edit."""
    _require_scheduled_message_provenance()
    if not isinstance(message, str) or isinstance(scheduled_at, bool):
        raise ValueError("invalid scheduled-message provenance")
    replacement = ScheduledMessageProvenance(
        slot_key=expected.slot_key,
        message=message,
        scheduled_at=float(scheduled_at),
    )
    if (
        expected.completed
        or not math.isfinite(replacement.scheduled_at)
        or replacement.scheduled_at <= 0
    ):
        raise ValueError("invalid scheduled-message provenance")
    path = scheduled_message_record_path(record_id)
    with _record_lock(path):
        current_entry = _read_scheduled_message_record(path)
        current = _scheduled_message_from_entry(current_entry, expected.slot_key)
        if current != expected or current_entry is None:
            return False
        _write_scheduled_message_record(
            path,
            _signed_scheduled_message_entry(
                replacement,
                armed_ts=current_entry.get("armed_ts", time.time()),
                updated_ts=time.time(),
            ),
        )
        return True


def mark_scheduled_message_completed(
    record_id: str,
    expected: ScheduledMessageProvenance,
) -> bool:
    """CAS-mark one authenticated message inert after trusted turn completion."""
    _require_scheduled_message_provenance()
    path = scheduled_message_record_path(record_id)
    with _record_lock(path):
        current_entry = _read_scheduled_message_record(path)
        current = _scheduled_message_from_entry(current_entry, expected.slot_key)
        if current is None or current_entry is None:
            return False
        completed = ScheduledMessageProvenance(
            expected.slot_key,
            expected.message,
            expected.scheduled_at,
            completed=True,
        )
        if current.completed:
            return current == completed
        if current != expected:
            return False
        _write_scheduled_message_record(
            path,
            _signed_scheduled_message_entry(
                completed,
                armed_ts=current_entry.get("armed_ts", time.time()),
                completed_ts=time.time(),
            ),
        )
        return True


def delete_scheduled_message_record(record_id: str) -> None:
    """Delete one provenance record and verify that no plaintext remains."""
    path = scheduled_message_record_path(record_id)
    with _record_lock(path):
        path.unlink(missing_ok=True)
        if path.exists():
            raise OSError("scheduled-message provenance survived deletion")


def clear_scheduled_message_records() -> None:
    """Remove every provenance record before unsupported-host agents can start."""
    root = data_home() / SCHEDULED_MESSAGE_RECORD_NAME
    if root.is_symlink():
        root.unlink()
    elif root.exists():
        shutil.rmtree(root)
    if root.exists():
        raise OSError("scheduled-message provenance survived platform cleanup")


def forget_self_arm(loop_id: str) -> None:
    """Drop a self-arm or scheduled-provenance record. Best-effort; never raises."""
    try:
        if str(loop_id).startswith("scheduled-message:"):
            delete_scheduled_message_record(loop_id)
            return
        path = self_arm_record_path()
        with _record_lock(path):
            entries = _read_self_arm_record(path)
            if str(loop_id) in entries:
                del entries[str(loop_id)]
                _write_self_arm_record(path, entries)
    except OSError:
        logger.warning("could not revoke autonudge record for %s", loop_id, exc_info=True)


def is_recorded_self_arm(loop_id: str, slot_key: str) -> bool:
    """Whether the trust record vouches that *loop_id* self-armed on *slot_key*."""
    entry = _read_self_arm_record(self_arm_record_path()).get(str(loop_id))
    return (
        entry is not None and entry.get("kind") is None and entry.get("slot_key") == str(slot_key)
    )
