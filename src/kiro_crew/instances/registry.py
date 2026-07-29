"""Instances registry — persistent store of remote KiroCrew instances.

Backs the *Instances* feature (multi-instance management). The registry is a
small JSON file at ``~/.kiro/crew/instances.json``. Each record describes how to
reach one remote KiroCrew over SSH; the *local* instance is implicit (the
gateway itself) and is never stored here.

Two persisted hints support lazy reconnect on gateway restart:

* per-instance ``was_connected`` — whether the instance had an open tunnel when
  it was last touched, used to render "disconnected — click to reconnect".
* top-level ``last_active_id`` — the single instance to auto-revive on startup
  (startup opens *no* other tunnels, avoiding a stale-credential ssh herd).

Security notes (standard practices):

* No credentials/tokens are ever written here. Records hold only connection
  *coordinates* (ssh host alias, ports, ttl). Dashboard tokens are minted at
  connect time and live only in memory / the browser cookie.
* ``ssh_host`` and ``remote_bin`` get a light charset check here to reject
  obviously malformed input early; the injection-safe validation that guards
  the actual ``ssh`` command line lives with the ``SshTunnelManager``.
* Writes go through :func:`kiro_crew.atomic_write.atomic_write` (temp file +
  rename) so a crash mid-write can't corrupt the registry.

The registry reads-then-writes the file on every mutation rather than caching an
in-memory copy, so a live gateway and an out-of-band ``kirocrew`` CLI edit don't
clobber each other's changes between operations.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

# Monkeypatchable in tests via ``monkeypatch.setattr`` alongside KIROCREW_HOME,
# per the shared-state test-isolation lesson. ``None`` means "derive from
# config_dir() at call time" so KIROCREW_HOME overrides are always honoured.
_DEFAULT_DIR: Path | None = None
_FILENAME = "instances.json"

# Instance id: slug-like, what the URL/switcher key uses. Kept conservative so
# it is safe as a dict key, a query param, and a filename component.
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

# ssh_host / remote_bin light charset guard (early reject only — the real
# injection-safe validation lives in the tunnel manager, Stage 4). Allows
# hostnames, FQDNs, ssh config aliases, user@host, and absolute bin paths.
_SSH_HOST_RE = re.compile(r"^[A-Za-z0-9._@\-]{1,255}$")
_REMOTE_BIN_RE = re.compile(r"^[A-Za-z0-9._/~\- ]{0,512}$")

_DEFAULT_REMOTE_PORT = 7777
_DEFAULT_TTL = "20h"

# ``local_port == 0`` is the sentinel for "not yet allocated" — the port
# allocator (Stage 3) assigns a real port at connect time.
_UNALLOCATED_PORT = 0


class InstancesError(Exception):
    """Base error for registry operations."""


class DuplicateInstanceError(InstancesError):
    """Raised when adding an instance whose id already exists."""


class InstanceNotFoundError(InstancesError):
    """Raised when an operation targets an unknown instance id."""


class InvalidInstanceError(InstancesError):
    """Raised when an instance record fails validation."""


def _slugify(name: str) -> str:
    """Derive a slug-like id from a human name (lowercase, hyphen-separated)."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    slug = slug[:63]
    return slug or "instance"


@dataclass
class Instance:
    """One remote KiroCrew instance reachable over SSH.

    Holds the connection coordinates plus the lazy-reconnect ``was_connected``
    hint. The local instance is implicit and is never represented by an
    ``Instance`` record.
    """

    id: str
    name: str
    ssh_host: str
    remote_port: int = _DEFAULT_REMOTE_PORT
    local_port: int = _UNALLOCATED_PORT
    ttl: str = _DEFAULT_TTL
    remote_bin: str = ""
    # Sticky "connection intent" — the source of truth for whether a tab should
    # exist for this instance. Set True when a tunnel is opened and cleared ONLY
    # on an explicit user disconnect; deliberately LEFT TRUE across gateway
    # shutdown and across a failed auto-revive, so the frontend keeps the tab
    # (showing an error / click-to-reconnect state) instead of dropping it.
    # Startup uses it to decide which instances to auto-reconnect.
    was_connected: bool = False

    def validate(self) -> None:
        """Raise :class:`InvalidInstanceError` if any field is malformed."""
        if not _ID_RE.match(self.id):
            raise InvalidInstanceError(
                f"invalid instance id {self.id!r}: must match {_ID_RE.pattern}"
            )
        if not self.name or not self.name.strip():
            raise InvalidInstanceError("instance name must be non-empty")
        if not self.ssh_host or not _SSH_HOST_RE.match(self.ssh_host):
            raise InvalidInstanceError(
                f"invalid ssh_host {self.ssh_host!r}: must match {_SSH_HOST_RE.pattern}"
            )
        if self.remote_bin and not _REMOTE_BIN_RE.match(self.remote_bin):
            raise InvalidInstanceError(f"invalid remote_bin {self.remote_bin!r}")
        for label, port, allow_zero in (
            ("remote_port", self.remote_port, False),
            ("local_port", self.local_port, True),
        ):
            lo = 0 if allow_zero else 1
            if not isinstance(port, int) or not (lo <= port <= 65535):
                raise InvalidInstanceError(
                    f"invalid {label} {port!r}: must be an int in "
                    f"[{lo}, 65535]" + (" (0 = unallocated)" if allow_zero else "")
                )

    def to_dict(self) -> dict:
        """Serialize to the JSON shape stored in ``instances.json``."""
        return {
            "id": self.id,
            "name": self.name,
            "ssh_host": self.ssh_host,
            "remote_port": self.remote_port,
            "local_port": self.local_port,
            "ttl": self.ttl,
            "remote_bin": self.remote_bin,
            "was_connected": self.was_connected,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Instance:
        """Build an :class:`Instance` from a stored dict, coercing types.

        Tolerant of missing/extra keys so older registry files load cleanly.
        """

        def _as_int(value: object, default: int) -> int:
            try:
                return int(value)  # type: ignore[call-overload]
            except (TypeError, ValueError):
                return default

        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            ssh_host=str(data.get("ssh_host", "")),
            remote_port=_as_int(data.get("remote_port"), _DEFAULT_REMOTE_PORT),
            local_port=_as_int(data.get("local_port"), _UNALLOCATED_PORT),
            ttl=str(data.get("ttl", _DEFAULT_TTL)),
            remote_bin=str(data.get("remote_bin", "")),
            was_connected=bool(data.get("was_connected", False)),
        )


@dataclass
class _RegistryDoc:
    """In-memory view of the whole ``instances.json`` document."""

    instances: list[Instance] = field(default_factory=list)
    last_active_id: str = ""


class InstancesRegistry:
    """CRUD over ``instances.json`` with atomic writes and a process lock.

    Every mutation re-reads the file, applies the change, validates, and writes
    atomically, so concurrent writers (a live gateway autosave and a CLI edit)
    never silently clobber one another between a read and a write.
    """

    def __init__(self, path: Path | None = None) -> None:
        if path is not None:
            self._path = path
        else:
            base = _DEFAULT_DIR if _DEFAULT_DIR is not None else config_dir()
            self._path = base / _FILENAME
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    # ── persistence ──────────────────────────────────────────────────────

    def _read(self) -> _RegistryDoc:
        """Load the registry document from disk, tolerating absence/corruption."""
        if not self._path.exists():
            return _RegistryDoc()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to read %s: %s — treating as empty", self._path, e)
            return _RegistryDoc()
        if not isinstance(raw, dict):
            logger.warning("%s is not a JSON object — treating as empty", self._path)
            return _RegistryDoc()
        raw_list = raw.get("instances", [])
        instances: list[Instance] = []
        if isinstance(raw_list, list):
            for entry in raw_list:
                if isinstance(entry, dict) and entry.get("id"):
                    instances.append(Instance.from_dict(entry))
        last_active = raw.get("last_active_id", "")
        return _RegistryDoc(
            instances=instances,
            last_active_id=str(last_active) if isinstance(last_active, str) else "",
        )

    def _write(self, doc: _RegistryDoc) -> None:
        """Persist *doc* atomically. ``last_active_id`` is dropped if stale."""
        ids = {inst.id for inst in doc.instances}
        last_active = doc.last_active_id if doc.last_active_id in ids else ""
        payload = {
            "instances": [inst.to_dict() for inst in doc.instances],
            "last_active_id": last_active,
        }
        atomic_write(self._path, json.dumps(payload, indent=2) + "\n", fsync=True)

    # ── read API ─────────────────────────────────────────────────────────

    def list(self) -> list[Instance]:
        """Return all configured instances (excludes the implicit local one)."""
        with self._lock:
            return self._read().instances

    def get(self, instance_id: str) -> Instance | None:
        """Return the instance with *instance_id*, or ``None`` if absent."""
        with self._lock:
            for inst in self._read().instances:
                if inst.id == instance_id:
                    return inst
            return None

    def get_last_active(self) -> Instance | None:
        """Return the last-active instance to auto-revive on startup."""
        with self._lock:
            doc = self._read()
            if not doc.last_active_id:
                return None
            for inst in doc.instances:
                if inst.id == doc.last_active_id:
                    return inst
            return None

    # ── write API ────────────────────────────────────────────────────────

    def add(
        self,
        *,
        name: str,
        ssh_host: str,
        remote_port: int = _DEFAULT_REMOTE_PORT,
        local_port: int = _UNALLOCATED_PORT,
        ttl: str = _DEFAULT_TTL,
        remote_bin: str = "",
        instance_id: str | None = None,
    ) -> Instance:
        """Add a new instance and return it.

        *instance_id* is derived from *name* when omitted, with a numeric suffix
        to disambiguate collisions. Raises :class:`DuplicateInstanceError` if an
        explicit id already exists, or :class:`InvalidInstanceError` on bad input.
        """
        with self._lock:
            doc = self._read()
            existing_ids = {inst.id for inst in doc.instances}

            if instance_id:
                new_id = instance_id
                if new_id in existing_ids:
                    raise DuplicateInstanceError(f"instance id {new_id!r} already exists")
            else:
                base = _slugify(name)
                new_id = base
                n = 2
                while new_id in existing_ids:
                    new_id = f"{base}-{n}"
                    n += 1

            inst = Instance(
                id=new_id,
                name=name,
                ssh_host=ssh_host,
                remote_port=remote_port,
                local_port=local_port,
                ttl=ttl,
                remote_bin=remote_bin,
                was_connected=False,
            )
            inst.validate()
            doc.instances.append(inst)
            self._write(doc)
            logger.info("Added instance %s (%s)", inst.id, inst.ssh_host)
            return inst

    def update(self, instance_id: str, **changes: object) -> Instance:
        """Patch fields on an existing instance and return the updated record.

        Accepts any of: ``name``, ``ssh_host``, ``remote_port``, ``local_port``,
        ``ttl``, ``remote_bin``, ``was_connected``. The ``id`` is immutable.
        Raises :class:`InstanceNotFoundError` / :class:`InvalidInstanceError`.
        """
        allowed = {
            "name",
            "ssh_host",
            "remote_port",
            "local_port",
            "ttl",
            "remote_bin",
            "was_connected",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise InvalidInstanceError(f"unknown fields: {sorted(unknown)}")
        with self._lock:
            doc = self._read()
            target: Instance | None = None
            for inst in doc.instances:
                if inst.id == instance_id:
                    target = inst
                    break
            if target is None:
                raise InstanceNotFoundError(f"no instance with id {instance_id!r}")
            for key, value in changes.items():
                setattr(target, key, value)
            target.validate()
            self._write(doc)
            logger.info("Updated instance %s: %s", instance_id, sorted(changes))
            return target

    def remove(self, instance_id: str) -> bool:
        """Remove an instance. Returns ``True`` if it existed, ``False`` otherwise."""
        with self._lock:
            doc = self._read()
            before = len(doc.instances)
            doc.instances = [i for i in doc.instances if i.id != instance_id]
            if len(doc.instances) == before:
                return False
            if doc.last_active_id == instance_id:
                doc.last_active_id = ""
            self._write(doc)
            logger.info("Removed instance %s", instance_id)
            return True

    def set_was_connected(self, instance_id: str, value: bool) -> None:
        """Set the ``was_connected`` hint (no-op if the instance is gone)."""
        with self._lock:
            doc = self._read()
            changed = False
            for inst in doc.instances:
                if inst.id == instance_id:
                    inst.was_connected = bool(value)
                    changed = True
                    break
            if changed:
                self._write(doc)

    def set_last_active(self, instance_id: str) -> None:
        """Mark *instance_id* as the one to auto-revive on next startup.

        Raises :class:`InstanceNotFoundError` if the id is unknown so callers
        can't silently point ``last_active_id`` at a non-existent instance.
        """
        with self._lock:
            doc = self._read()
            if not any(i.id == instance_id for i in doc.instances):
                raise InstanceNotFoundError(f"no instance with id {instance_id!r}")
            doc.last_active_id = instance_id
            self._write(doc)
