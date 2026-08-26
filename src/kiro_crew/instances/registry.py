"""Instances registry — persistent store of remote KiroCrew instances.

Backs the *Instances* feature (multi-instance management). The registry is a
small JSON file at ``~/.kiro/crew/instances.json``. Each record describes how to
reach one remote Kiro Crew over **either SSH or AWS SSM Session Manager**
(``connection_method``); the *local* instance is implicit (the gateway itself)
and is never stored here.

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
from kiro_crew.config.loader import _DEFAULT_PORT, config_dir
from kiro_crew.instances.constants import TTL_PATTERN
from kiro_crew.instances.validation import _AWS_PROFILE_RE as _validation_aws_profile_re

logger = logging.getLogger(__name__)

# Monkeypatchable in tests via ``monkeypatch.setattr`` alongside KIROCREW_HOME,
# per the shared-state test-isolation lesson. ``None`` means "derive from
# config_dir() at call time" so KIROCREW_HOME overrides are always honoured.
_DEFAULT_DIR: Path | None = None
_FILENAME = "instances.json"

# Instance id: slug-like, what the URL/switcher key uses. Kept conservative so
# it is safe as a dict key, a query param, and a filename component.
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\Z")

# ssh_host / remote_bin light charset guard (early reject only — the real
# injection-safe validation lives in the tunnel manager, Stage 4). Allows
# hostnames, FQDNs, ssh config aliases, user@host, and absolute bin paths.
_SSH_HOST_RE = re.compile(r"^[A-Za-z0-9._@\-]{1,255}\Z")
_REMOTE_BIN_RE = re.compile(r"^[A-Za-z0-9._/~\- ]{0,512}\Z")

# ssm_target: an EC2 instance id (i-<17 hex>) or an SSM managed-instance id
# (mi-<17 hex>); early reject only, mirroring the ssh_host guard above — the
# authoritative validation lives with the tunnel manager (validation.py).
_SSM_TARGET_RE = re.compile(r"^(i|mi)-[a-f0-9]{8,17}\Z")
# aws_profile: named profile in ~/.aws/config; conservative charset, no shell
# metacharacters ('+' is legal: IAM entity names permit it, and SSO-derived
# profiles use "<account>+<permission-set>"). Single source of truth lives in
# validation.py; the empty "default credential chain" value is handled by the
# `if self.aws_profile` guard at the check site rather than by the pattern.
_AWS_PROFILE_RE = _validation_aws_profile_re
# aws_region: standard AWS region shape (e.g. us-east-1, eu-west-2). Empty
# string means "use the profile's/environment's default region".
_AWS_REGION_RE = re.compile(r"^[a-z]{2}(-gov)?-[a-z]+-\d{1,2}\Z|^\Z")
# ssm_run_as: the remote POSIX user that SSM commands are wrapped in
# (``sudo -u <user> -i``). Unix username shape, matching the charset
# cloud.ssm.run_command validates at the chokepoint. Defaults to the
# launcher-provisioned AL2023 user; an Ubuntu AMI needs "ubuntu".
_SSM_RUN_AS_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}\Z")
# Shared with both token minters (see constants.TTL_PATTERN).
_TTL_RE = re.compile(TTL_PATTERN)
_DEFAULT_SSM_RUN_AS = "ec2-user"

# The port a stock gateway binds, so "add a remote that has not been
# reconfigured" needs no edit. NOT a second definition of the number:
# ``config/loader.py`` owns it per docs/system-specs/common/code-style.md, which
# names that module as the single owner of the dashboard port and cites
# ``dashboard/origin.py`` importing it rather than restating it. This is the same
# re-export seam, giving the value a name that says what it means HERE (the
# REMOTE's port, not ours).
#
# It was previously 7777 -- an earlier default dashboard port -- which left the
# Add form pre-filling a port no stock remote listens on (#1972). Correcting it
# was only safe once the local forward stopped mirroring this value: while it
# mirrored, filling in the port a stock remote actually binds landed the user on
# a guaranteed local-port collision.
DEFAULT_REMOTE_PORT = _DEFAULT_PORT
_DEFAULT_TTL = "20h"

# Supported connection transports. "ssh" is the original/default transport
# (ssh -N -L); "ssm" tunnels over AWS Systems Manager Session Manager
# (aws ssm start-session --document-name AWS-StartPortForwardingSession),
# needing no inbound SSH port and no SSH key — only IAM + the SSM agent.
CONNECTION_METHODS: tuple[str, ...] = ("ssh", "ssm")
_DEFAULT_CONNECTION_METHOD = "ssh"

# ``local_port == 0`` is the sentinel for "not yet allocated" — the port
# allocator (Stage 3) assigns a real port at connect time.
_UNALLOCATED_PORT = 0

# ``forwarder_pid == 0`` is the sentinel for "no forwarder child recorded".
# Connect records the spawned tunnel child's pid so a forwarder orphaned by a
# gateway hard-kill can later be reclaimed by identity (its own pid) instead of
# a process-table match; disconnect resets it together with ``local_port``.
_NO_FORWARDER_PID = 0


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


def validate_ttl(ttl: str) -> None:
    """Reject a ttl the token minters would refuse.

    Checked where a ttl is WRITTEN rather than in :meth:`Instance.validate`, so a
    legacy record whose stored ttl predates this rule keeps working: its hint
    writes (``was_connected`` / ``local_port`` from connect and disconnect) go
    through ``update()`` too, and failing those would break the teardown path
    over a field the caller never touched.
    """
    if not _TTL_RE.match(ttl):
        raise InvalidInstanceError(
            f"invalid ttl {ttl!r}: expected a positive integer of at most four "
            f"digits followed by 'h' or 'm' (e.g. 20h, 30m)"
        )


@dataclass
class Instance:
    """One remote Kiro Crew instance reachable over SSH or SSM.

    Holds the connection coordinates plus the lazy-reconnect ``was_connected``
    hint. The local instance is implicit and is never represented by an
    ``Instance`` record.

    ``connection_method`` selects the transport: ``"ssh"`` (default, uses
    ``ssh_host``/``remote_bin``) or ``"ssm"`` (uses ``ssm_target`` — an EC2/SSM
    managed-instance id — plus optional ``aws_profile``/``aws_region``; no SSH
    key or inbound port needed). Both methods share ``remote_port``/``ttl``.
    """

    id: str
    name: str
    ssh_host: str = ""
    remote_port: int = DEFAULT_REMOTE_PORT
    local_port: int = _UNALLOCATED_PORT
    ttl: str = _DEFAULT_TTL
    remote_bin: str = ""
    # "ssh" (default) or "ssm" — see CONNECTION_METHODS.
    connection_method: str = _DEFAULT_CONNECTION_METHOD
    # SSM-only fields. ssm_target is an EC2 instance id (i-...) or SSM managed
    # instance id (mi-...); aws_profile/aws_region are optional (empty = use the
    # default credential chain / region).
    ssm_target: str = ""
    aws_profile: str = ""
    aws_region: str = ""
    # Remote POSIX user for SSM commands (mint / restart / probe), which
    # cloud.ssm.run_command wraps in `sudo -u <user> -i`. Defaults to the
    # launcher-provisioned AL2023 user; set "ubuntu" (or whoever runs the remote
    # gateway) on other AMIs, otherwise the tunnel comes up but the mint fails.
    ssm_run_as: str = _DEFAULT_SSM_RUN_AS
    # Sticky "connection intent" — the source of truth for whether a tab should
    # exist for this instance. Set True when a tunnel is opened and cleared ONLY
    # on an explicit user disconnect; deliberately LEFT TRUE across gateway
    # shutdown and across a failed auto-revive, so the frontend keeps the tab
    # (showing an error / click-to-reconnect state) instead of dropping it.
    # Startup uses it to decide which instances to auto-reconnect.
    was_connected: bool = False
    # Pid of the tunnel forwarder child this manager spawned for the recorded
    # ``local_port`` (0 = none recorded), paired with the opaque start-time
    # identity ``platform_compat.process_start_time`` reported for it ("" =
    # unknown). Persisted so a forwarder orphaned by a gateway hard-kill can be
    # reclaimed by its OWN identity — pid + start time + exact argv — never by
    # matching the process table, which cannot distinguish our child from an
    # operator's own forward (#1972). Either half missing means the identity
    # cannot be confirmed and no reclaim happens (fail closed).
    forwarder_pid: int = _NO_FORWARDER_PID
    forwarder_start: str = ""
    # HMAC over (id, forwarder_pid, forwarder_start, local_port) under a
    # gateway-held key ("" = unsigned). The registry file is agent-writable, so
    # the identity above gates but cannot authorize by itself; the signature is
    # what makes it the GATEWAY's own claim — a record an agent wrote or
    # redirected fails verification and is never reclaimed (fail closed).
    forwarder_sig: str = ""

    def validate(self) -> None:
        """Raise :class:`InvalidInstanceError` if any field is malformed."""
        if not _ID_RE.match(self.id):
            raise InvalidInstanceError(
                f"invalid instance id {self.id!r}: must match {_ID_RE.pattern}"
            )
        if not self.name or not self.name.strip():
            raise InvalidInstanceError("instance name must be non-empty")
        if self.connection_method not in CONNECTION_METHODS:
            raise InvalidInstanceError(
                f"invalid connection_method {self.connection_method!r}: "
                f"must be one of {CONNECTION_METHODS}"
            )
        if self.connection_method == "ssh":
            if not self.ssh_host or not _SSH_HOST_RE.match(self.ssh_host):
                raise InvalidInstanceError(
                    f"invalid ssh_host {self.ssh_host!r}: must match {_SSH_HOST_RE.pattern}"
                )
        else:  # ssm
            if not self.ssm_target or not _SSM_TARGET_RE.match(self.ssm_target):
                # No regex in the message — it reaches the Settings form verbatim.
                raise InvalidInstanceError(
                    f"invalid ssm_target {self.ssm_target!r}: must be an EC2/SSM "
                    f"managed-instance id (i-... or mi-...) followed by 8 to 17 "
                    f"hex digits"
                )
            if self.aws_profile and not _AWS_PROFILE_RE.match(self.aws_profile):
                raise InvalidInstanceError(
                    f"invalid aws_profile {self.aws_profile!r} "
                    f"(allowed: letters, digits, '.', '_', '+', '-')"
                )
            if self.aws_region and not _AWS_REGION_RE.match(self.aws_region):
                raise InvalidInstanceError(f"invalid aws_region {self.aws_region!r}")
            if not _SSM_RUN_AS_RE.match(self.ssm_run_as):
                raise InvalidInstanceError(
                    f"invalid ssm_run_as {self.ssm_run_as!r}: must be a Unix "
                    f"username (lowercase, starts with a letter or underscore)"
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
        if not isinstance(self.forwarder_pid, int) or self.forwarder_pid < 0:
            raise InvalidInstanceError(
                f"invalid forwarder_pid {self.forwarder_pid!r}: must be an int "
                f">= 0 (0 = no forwarder recorded)"
            )
        if not isinstance(self.forwarder_start, str):
            raise InvalidInstanceError(
                f"invalid forwarder_start {self.forwarder_start!r}: must be a "
                f"string ('' = unknown)"
            )
        if not isinstance(self.forwarder_sig, str):
            raise InvalidInstanceError(
                f"invalid forwarder_sig {self.forwarder_sig!r}: must be a "
                f"string ('' = unsigned)"
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
            "connection_method": self.connection_method,
            "ssm_target": self.ssm_target,
            "aws_profile": self.aws_profile,
            "aws_region": self.aws_region,
            "ssm_run_as": self.ssm_run_as,
            "was_connected": self.was_connected,
            "forwarder_pid": self.forwarder_pid,
            "forwarder_start": self.forwarder_start,
            "forwarder_sig": self.forwarder_sig,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Instance:
        """Build an :class:`Instance` from a stored dict, coercing types.

        Tolerant of missing/extra keys so older registry files (pre-SSM, with
        no ``connection_method``) load cleanly — they default to ``"ssh"``.
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
            remote_port=_as_int(data.get("remote_port"), DEFAULT_REMOTE_PORT),
            local_port=_as_int(data.get("local_port"), _UNALLOCATED_PORT),
            ttl=str(data.get("ttl", _DEFAULT_TTL)),
            remote_bin=str(data.get("remote_bin", "")),
            connection_method=str(data.get("connection_method", _DEFAULT_CONNECTION_METHOD)),
            ssm_target=str(data.get("ssm_target", "")),
            aws_profile=str(data.get("aws_profile", "")),
            aws_region=str(data.get("aws_region", "")),
            # `or _DEFAULT_SSM_RUN_AS` (not just a dict default): a record written
            # by an older build has no key, and one written with an explicit
            # empty string would fail validation — both mean "use the default".
            ssm_run_as=str(data.get("ssm_run_as", "") or _DEFAULT_SSM_RUN_AS),
            was_connected=bool(data.get("was_connected", False)),
            # max(): a hand-edited negative pid normalizes to the sentinel
            # rather than poisoning every later update() with a validate error
            # — matching this loader's documented key tolerance.
            forwarder_pid=max(_NO_FORWARDER_PID, _as_int(data.get("forwarder_pid"), 0)),
            forwarder_start=str(data.get("forwarder_start", "") or ""),
            forwarder_sig=str(data.get("forwarder_sig", "") or ""),
        )


@dataclass
class _RegistryDoc:
    """In-memory view of the whole ``instances.json`` document."""

    instances: list[Instance] = field(default_factory=list)
    last_active_id: str = ""


def _find(doc: _RegistryDoc, instance_id: str) -> Instance | None:
    """Return the record in *doc* with *instance_id*, or ``None`` if absent.

    Hands back the live object out of ``doc.instances`` (not a copy), so a caller
    holding the lock can mutate it in place and persist *doc*.
    """
    for inst in doc.instances:
        if inst.id == instance_id:
            return inst
    return None


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
            return _find(self._read(), instance_id)

    def get_last_active(self) -> Instance | None:
        """Return the last-active instance to auto-revive on startup."""
        with self._lock:
            doc = self._read()
            if not doc.last_active_id:
                return None
            return _find(doc, doc.last_active_id)

    # ── write API ────────────────────────────────────────────────────────

    def add(
        self,
        *,
        name: str,
        ssh_host: str = "",
        remote_port: int = DEFAULT_REMOTE_PORT,
        local_port: int = _UNALLOCATED_PORT,
        ttl: str = _DEFAULT_TTL,
        remote_bin: str = "",
        connection_method: str = _DEFAULT_CONNECTION_METHOD,
        ssm_target: str = "",
        aws_profile: str = "",
        aws_region: str = "",
        ssm_run_as: str = _DEFAULT_SSM_RUN_AS,
        instance_id: str | None = None,
    ) -> Instance:
        """Add a new instance and return it.

        *instance_id* is derived from *name* when omitted, with a numeric suffix
        to disambiguate collisions. Raises :class:`DuplicateInstanceError` if an
        explicit id already exists, or :class:`InvalidInstanceError` on bad input.

        *connection_method* selects the transport ("ssh" or "ssm"); the fields
        required depend on it — see :meth:`Instance.validate`.
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
                connection_method=connection_method,
                ssm_target=ssm_target,
                aws_profile=aws_profile,
                aws_region=aws_region,
                ssm_run_as=ssm_run_as or _DEFAULT_SSM_RUN_AS,
                was_connected=False,
            )
            inst.validate()
            validate_ttl(inst.ttl)
            doc.instances.append(inst)
            self._write(doc)
            logger.info(
                "Added instance %s (%s: %s)",
                inst.id,
                inst.connection_method,
                inst.ssh_host or inst.ssm_target,
            )
            return inst

    def update(
        self, instance_id: str, *, mark_last_active: bool = False, **changes: object
    ) -> Instance:
        """Patch fields on an existing instance and return the updated record.

        Accepts any of: ``name``, ``ssh_host``, ``remote_port``, ``local_port``,
        ``ttl``, ``remote_bin``, ``connection_method``, ``ssm_target``,
        ``ssm_run_as``,
        ``aws_profile``, ``aws_region``, ``was_connected``, ``forwarder_pid``,
        ``forwarder_start``, ``forwarder_sig``.
        The ``id`` is
        immutable. ``mark_last_active=True`` additionally records the instance
        as the auto-revive target in the SAME read-modify-write, so callers that
        need both (a connect persisting its hints) get one atomic file rewrite
        instead of two — the pair becomes durable together. Raises
        :class:`InstanceNotFoundError` / :class:`InvalidInstanceError`.
        """
        allowed = {
            "name",
            "ssh_host",
            "remote_port",
            "local_port",
            "ttl",
            "remote_bin",
            "connection_method",
            "ssm_target",
            "ssm_run_as",
            "aws_profile",
            "aws_region",
            "was_connected",
            "forwarder_pid",
            "forwarder_start",
            "forwarder_sig",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise InvalidInstanceError(f"unknown fields: {sorted(unknown)}")
        if "ttl" in changes:
            validate_ttl(str(changes["ttl"]))
        with self._lock:
            doc = self._read()
            target = _find(doc, instance_id)
            if target is None:
                raise InstanceNotFoundError(f"no instance with id {instance_id!r}")
            for key, value in changes.items():
                setattr(target, key, value)
            target.validate()
            if mark_last_active:
                doc.last_active_id = instance_id
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

    def set_last_active(self, instance_id: str) -> None:
        """Mark *instance_id* as the one to auto-revive on next startup.

        Raises :class:`InstanceNotFoundError` if the id is unknown so callers
        can't silently point ``last_active_id`` at a non-existent instance.
        """
        with self._lock:
            doc = self._read()
            if _find(doc, instance_id) is None:
                raise InstanceNotFoundError(f"no instance with id {instance_id!r}")
            doc.last_active_id = instance_id
            self._write(doc)
