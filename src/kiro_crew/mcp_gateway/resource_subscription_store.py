"""The durable record of resource subscriptions, one entry per (session, server).

``rfc-mcp-resource-subscriptions.md`` §4.1 and §4.3: a holder must be able to
register again after a Kiro Crew restart with no kiro-cli present, so each
entry keeps what ``describe-session`` returned -- the session stub's Register,
token-free, and its ``initialize`` -- beside the session's key and channel and
the subscribed URIs. No token is stored: the holder is attributed by its caller
block, which these fields rebuild.

On-disk discipline is the one ``webhooks.py`` uses for its stores: a
read-modify-write under an advisory lock on a sidecar ``.lock`` file, then an
atomic owner-only replace. An unreadable file is refused rather than read as
empty, so one bad byte cannot make the next write erase every subscription.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.mcp_gateway.backend_leases import RESOURCE_URI_MAX_LEN
from kiro_crew.mcp_gateway.backend_wire import _log_safe_identifier

logger = logging.getLogger(__name__)

STORE_DIR_LEAF = "mcp-resource-subscriptions"
STORE_LEAF = "subscriptions.json"
#: Largest serialized Register or initialize container retained in one record.
RETAINED_CONTAINER_MAX_BYTES = 256 * 1024
_VERSION = 1


def serialized_container_size(value: dict[str, Any]) -> int:
    """Number of JSON bytes *value* occupies in the durable record.

    ASCII-escaped, as :meth:`ResourceSubscriptionStore._write` writes it, so a
    lone surrogate (an undecodable byte in a work directory) is measured, not
    raised.
    """
    return len(json.dumps(value, separators=(",", ":")))


class StoreUnreadable(Exception):
    """The store exists but could not be read; nothing was changed."""


@dataclass
class SubscriptionMeta:
    """What one subscribed URI carries beyond the URI itself (Phases 3 and 4).

    ``rfc-mcp-resource-subscriptions.md`` §4.9.2 and §4.9.3: the id the session
    names it by, the instruction its wake carries, and the day's wake count,
    kept here so a restart does not reset the budget.
    """

    id: str
    instruction: str = ""
    #: The UTC day (``YYYY-MM-DD``) :attr:`wakes` counts.
    wake_day: str = ""
    wakes: int = 0
    #: The UTC day the budget pause was last reported, so it is reported once.
    budget_reported_day: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "instruction": self.instruction,
            "wake_day": self.wake_day,
            "wakes": self.wakes,
            "budget_reported_day": self.budget_reported_day,
        }

    @classmethod
    def from_json(cls, raw: Any) -> "SubscriptionMeta | None":
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
            return None
        strings = ("instruction", "wake_day", "budget_reported_day")
        if not all(isinstance(raw.get(k, ""), str) for k in strings):
            return None
        wakes = raw.get("wakes", 0)
        if not isinstance(wakes, int) or isinstance(wakes, bool) or wakes < 0:
            return None
        return cls(
            id=raw["id"],
            instruction=raw.get("instruction", ""),
            wake_day=raw.get("wake_day", ""),
            wakes=wakes,
            budget_reported_day=raw.get("budget_reported_day", ""),
        )


@dataclass
class HolderRecord:
    session_key: str
    channel_id: str
    server: str
    register: dict[str, Any]
    initialize: dict[str, Any]
    uris: list[str] = field(default_factory=list)
    #: The daemon served the session's stub a backend of its own, so this
    #: holder pins one more backend rather than sharing one (§4.1).
    private_backend: bool = False
    #: Non-empty when the whole holder is paused (§4.3), with the reason.
    paused_reason: str = ""
    #: URIs whose re-subscribe the server refused, with the reason.
    paused_uris: dict[str, str] = field(default_factory=dict)
    #: Per subscribed URI: its id, instruction and wake budget.
    subscriptions: dict[str, SubscriptionMeta] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return (self.session_key, self.server)

    def to_json(self) -> dict[str, Any]:
        return {
            "session_key": self.session_key,
            "channel_id": self.channel_id,
            "server": self.server,
            "register": self.register,
            "initialize": self.initialize,
            "uris": list(self.uris),
            "private_backend": self.private_backend,
            "paused_reason": self.paused_reason,
            "paused_uris": dict(self.paused_uris),
            "subscriptions": {uri: meta.to_json() for uri, meta in self.subscriptions.items()},
        }

    @classmethod
    def from_json(cls, raw: Any) -> "HolderRecord | None":
        if not isinstance(raw, dict):
            return None
        strings = ("session_key", "channel_id", "server", "paused_reason")
        if not all(isinstance(raw.get(k, ""), str) for k in strings):
            return None
        if not raw.get("session_key") or not raw.get("server"):
            return None
        register, initialize = raw.get("register"), raw.get("initialize")
        uris, paused_uris = raw.get("uris", []), raw.get("paused_uris", {})
        if not isinstance(register, dict) or not isinstance(initialize, dict):
            return None
        if not isinstance(uris, list) or not isinstance(paused_uris, dict):
            return None
        subscriptions = raw.get("subscriptions", {})
        if not isinstance(subscriptions, dict):
            return None
        metas = {
            uri: meta
            for uri, entry in subscriptions.items()
            if isinstance(uri, str) and (meta := SubscriptionMeta.from_json(entry)) is not None
        }
        return cls(
            session_key=raw["session_key"],
            channel_id=raw.get("channel_id", ""),
            server=raw["server"],
            register=register,
            initialize=initialize,
            uris=[u for u in uris if isinstance(u, str) and u],
            private_backend=raw.get("private_backend") is True,
            paused_reason=raw.get("paused_reason", ""),
            paused_uris={
                u: r for u, r in paused_uris.items() if isinstance(u, str) and isinstance(r, str)
            },
            subscriptions=metas,
        )


class ResourceSubscriptionStore:
    """The JSON file of :class:`HolderRecord` entries under the data home."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.parent / (self.path.name + ".lock")
        with platform_compat.open_lock_file(lock_path) as lock_fd:
            with platform_compat.flock_exclusive(lock_fd):
                yield

    def _read(self) -> list[HolderRecord]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise StoreUnreadable(f"{self.path}: {exc}") from exc
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StoreUnreadable(f"{self.path}: {exc}") from exc
        if not isinstance(doc, dict) or not isinstance(doc.get("holders"), list):
            raise StoreUnreadable(f"{self.path}: not a subscription store")
        records = []
        for entry in doc["holders"]:
            if isinstance(entry, dict):
                oversized_field = next(
                    (
                        field_name
                        for field_name in ("register", "initialize")
                        if isinstance(entry.get(field_name), dict)
                        and serialized_container_size(entry[field_name])
                        > RETAINED_CONTAINER_MAX_BYTES
                    ),
                    "",
                )
                if oversized_field:
                    logger.warning(
                        "resource subscriptions: dropping an entry whose %s exceeds "
                        "%d serialized bytes in %s",
                        oversized_field,
                        RETAINED_CONTAINER_MAX_BYTES,
                        _log_safe_identifier(str(self.path)),
                    )
                    continue
            record = HolderRecord.from_json(entry)
            if record is None:
                logger.warning(
                    "resource subscriptions: dropping a malformed entry in %s",
                    _log_safe_identifier(str(self.path)),
                )
                continue
            retained_uris = [*record.uris, *record.paused_uris]
            if any(len(uri) > RESOURCE_URI_MAX_LEN for uri in retained_uris):
                logger.warning(
                    "resource subscriptions: dropping an entry with an overlong URI in %s",
                    _log_safe_identifier(str(self.path)),
                )
                continue
            records.append(record)
        return records

    def _write(self, records: list[HolderRecord]) -> None:
        doc = {"version": _VERSION, "holders": [r.to_json() for r in records]}
        atomic_write(
            self.path, json.dumps(doc, indent=2).encode("utf-8"), fsync=True, restrict_to_owner=True
        )

    def _absent(self) -> bool:
        """Whether no store file exists; any other stat error is left to the read."""
        try:
            self.path.stat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return False

    def load(self) -> list[HolderRecord]:
        # No file means nothing to read; taking the lock would create its
        # ``.lock`` sidecar in the data home for a feature nobody turned on.
        if self._absent():
            return []
        with self._locked():
            return self._read()

    def put(self, record: HolderRecord) -> None:
        """Insert or replace the entry for ``record.key``."""
        with self._locked():
            records = [r for r in self._read() if r.key != record.key]
            records.append(record)
            self._write(records)

    def remove(self, session_key: str, server: str = "") -> int:
        """Drop one session's entry for *server*, or all of its entries."""
        if self._absent():
            # As in :meth:`load`: nothing stored, so no lock and no sidecar file.
            return 0
        with self._locked():
            records = self._read()
            kept = [
                r
                for r in records
                if not (r.session_key == session_key and (not server or r.server == server))
            ]
            if len(kept) != len(records):
                self._write(kept)
            return len(records) - len(kept)

    def remove_subscription(self, subscription_id: str) -> bool:
        """Drop the one subscription *subscription_id* names, from whichever entry holds it.

        For the operator CLI while no service runs; a running service is reached
        through the gateway instead, so its holder lets go of the URI too. An entry
        left holding nothing is removed.
        """
        if not subscription_id or self._absent():
            return False
        with self._locked():
            records = self._read()
            for record in records:
                uri = next(
                    (u for u, meta in record.subscriptions.items() if meta.id == subscription_id),
                    None,
                )
                if uri is None:
                    continue
                record.subscriptions.pop(uri)
                record.uris = [u for u in record.uris if u != uri]
                record.paused_uris.pop(uri, None)
                kept = [r for r in records if r is not record]
                if record.uris or record.paused_uris:
                    kept.append(record)
                self._write(kept)
                return True
            return False
