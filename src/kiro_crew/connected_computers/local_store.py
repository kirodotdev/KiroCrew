"""Local machine: the grants the user consented to, the install key and credentials.

All of it is one document in the LOCAL gateway's vault (:mod:`.vault_doc`), which
the agent sandbox hides: no agent on this machine can read a credential, write
itself a grant, widen a root or lift a revocation. A grant is written only by the
consent paths (:mod:`.local_routes` after native confirmation, or the CLI's
interactive confirmation), never by a remote page.

Secrets are sealed a second time under a data key the OS keeps
(:mod:`.protected_store`): each grant's connector credential and peer key, and
the install key, are AES-GCM sealed, bound to the field and the grant they
belong to. Writing or using one needs the OS store; with none, the write is
refused (:class:`ProtectionUnavailable`) and nothing falls back to plaintext. A
data key the OS does not hold, or a different one, is :class:`SealBroken`,
never silently replaced while sealed values exist. Metadata reads (``list``,
``get``) never unseal, so status, revoke and the local Off work with the
keyring locked.

A grant pairs ONE remote gateway (its Remote Crew instance here, and the gateway
id it proved at enrollment) with ONE root folder and the local MCP servers the
user ticked, snapshotted as they were confirmed (B02, B14). A newer grant for the
same instance revokes the older one. Revocation is local, immediate and
persistent: a revoked grant is never revived, by a reconnect, a restart or the
feature switch coming back on (re-enabling needs fresh consent).

The local Off (:meth:`LocalGrantStore.revoke_all`) bumps ``off_generation`` in
the same write, so a consent redeemed across an Off made by ANY process (the
gateway or the CLI) is refused (:class:`TurnedOff`). Every revocation queues one
remote revoke per (instance, device) pair in that same write
(:meth:`pending_forgets`), so a remote that is unreachable is told later, and a
fresh consent for that pair drops its stale entry (the re-enrollment already
refused the old credential there).
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Callable, Final, List, Optional

from kiro_crew.connected_computers.protected_store import (
    ProtectedStore,
    ProtectionUnavailable,
    protected_store_for_this_machine,
)
from kiro_crew.connected_computers.vault_doc import VaultDocError, VaultJsonDoc

LOCAL_ENTRY: Final[str] = "CONNECTED_COMPUTERS_LOCAL"
#: Most grants kept (revoked ones included, newest kept).
MAX_GRANTS: Final[int] = 32
#: Most queued remote revokes kept (oldest dropped; a dropped one's credential
#: was already cleared here and expires on the remote within its life).
MAX_PENDING_FORGETS: Final[int] = 64
#: Retry back-off for a remote revoke that did not go through.
FORGET_RETRY_MIN_SECS: Final[float] = 60.0
FORGET_RETRY_MAX_SECS: Final[float] = 3600.0
#: Most times the forget back-off doubles; already past FORGET_RETRY_MAX_SECS.
_FORGET_MAX_DOUBLINGS: Final[int] = 16

#: Longest stored terminal reason (:attr:`Grant.ended`).
MAX_ENDED_CHARS: Final[int] = 300
GRANT_ACTIVE: Final[str] = "active"
GRANT_REVOKED: Final[str] = "revoked"

_SEAL_PREFIX: Final[str] = "v1:"
_NONCE_BYTES: Final[int] = 12
_INSTALL_KEY_BYTES: Final[int] = 32
_SECRET_FIELDS: Final[tuple[str, ...]] = ("credential", "peer_key")
_PLAINTEXT_FIELDS: Final[tuple[str, ...]] = ("credential", "peer_key")

__all__ = [
    "ExposedMcp",
    "Grant",
    "LocalGrantStore",
    "PendingForget",
    "ProtectionUnavailable",
    "SealBroken",
    "TurnedOff",
    "new_grant_id",
]


class SealBroken(VaultDocError):
    """A sealed value cannot be opened with the OS data key; share again."""


class TurnedOff(RuntimeError):
    """A local Off happened after this consent began; nothing was written."""


@dataclass(frozen=True)
class ExposedMcp:
    """A local MCP server as the user confirmed it: stdio only."""

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    #: Tools turned off in this computer's config; the connector keeps them off.
    disabled_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class Grant:
    grant_id: str
    instance_id: str
    gateway_id: str
    gateway_label: str
    device_id: str
    root: str
    mcp: tuple[ExposedMcp, ...] = ()
    status: str = GRANT_ACTIVE
    created: float = 0.0
    revoked_at: float = 0.0
    #: In memory only; stored sealed. Empty unless read with ``unsealed``.
    credential: str = field(default="", repr=False)
    credential_expires: float = 0.0
    #: Proves the gateway to the connector before the credential is sent (R6).
    peer_key: str = field(default="", repr=False)
    #: Why this ACTIVE grant's connector ended for good (confinement unavailable,
    #: seal broken, refused by the gateway); empty while it may run. Persisted so
    #: a restart never re-runs the self-test or redials; only a new consent (a
    #: new grant id) or a revoke moves past it.
    ended: str = ""

    @property
    def active(self) -> bool:
        return self.status == GRANT_ACTIVE

    def public(self) -> dict[str, Any]:
        """What the local dashboard shows: never the credential or MCP env values."""
        return {
            "grant_id": self.grant_id,
            "instance_id": self.instance_id,
            "gateway_id": self.gateway_id,
            "gateway_label": self.gateway_label,
            "device_id": self.device_id,
            "root": self.root,
            "mcp": [m.name for m in self.mcp],
            "status": self.status,
            "created": self.created,
            "revoked_at": self.revoked_at,
        }


@dataclass(frozen=True)
class PendingForget:
    """A remote revoke not yet confirmed by the remote gateway."""

    key: str
    instance_id: str
    gateway_id: str
    device_id: str
    fingerprint: str
    queued: float
    attempts: int = 0
    next_try: float = 0.0


def _grant(raw: Any) -> Optional[Grant]:
    """A stored row's metadata; secrets are never read here."""
    if not isinstance(raw, dict):
        return None
    try:
        mcp = tuple(
            ExposedMcp(
                name=str(m["name"]),
                command=str(m["command"]),
                args=tuple(str(a) for a in m.get("args") or ()),
                env=tuple((str(k), str(v)) for k, v in (m.get("env") or [])),
                disabled_tools=tuple(str(t) for t in m.get("disabled_tools") or ()),
            )
            for m in raw.get("mcp") or ()
        )
        grant = Grant(
            grant_id=str(raw["grant_id"]),
            instance_id=str(raw["instance_id"]),
            gateway_id=str(raw["gateway_id"]),
            gateway_label=str(raw.get("gateway_label") or ""),
            device_id=str(raw["device_id"]),
            root=str(raw["root"]),
            mcp=mcp,
            status=str(raw["status"]),
            created=float(raw.get("created") or 0.0),
            revoked_at=float(raw.get("revoked_at") or 0.0),
            credential_expires=float(raw.get("credential_expires") or 0.0),
            ended=str(raw.get("ended") or "")[:MAX_ENDED_CHARS],
        )
    except (KeyError, TypeError, ValueError):
        return None
    if grant.status not in (GRANT_ACTIVE, GRANT_REVOKED):
        return None
    return grant


def _row(grant: Grant, sealed: dict[str, str]) -> dict[str, Any]:
    row = asdict(grant)
    for name in _PLAINTEXT_FIELDS:
        row.pop(name, None)
    row["mcp"] = [
        {
            "name": m.name,
            "command": m.command,
            "args": list(m.args),
            "env": [list(e) for e in m.env],
            "disabled_tools": list(m.disabled_tools),
        }
        for m in grant.mcp
    ]
    for name, value in sealed.items():
        row[f"{name}_sealed"] = value
    return row


def _revoke_row(raw: dict[str, Any], now: float) -> dict[str, Any]:
    out = dict(raw)
    out["status"] = GRANT_REVOKED
    out["revoked_at"] = float(raw.get("revoked_at") or 0.0) or now
    for name in _SECRET_FIELDS:
        out.pop(f"{name}_sealed", None)
        out.pop(name, None)
    return out


def _key_id(key: bytes) -> str:
    return hashlib.sha256(b"kirocrew-connected-computers-data-key\x00" + key).hexdigest()[:16]


def _aad(field_name: str, owner: str) -> bytes:
    return f"{LOCAL_ENTRY}\x00{field_name}\x00{owner}".encode("utf-8")


def _seal(key: bytes, field_name: str, owner: str, value: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(_NONCE_BYTES)
    blob = nonce + AESGCM(key).encrypt(nonce, value, _aad(field_name, owner))
    return _SEAL_PREFIX + base64.urlsafe_b64encode(blob).decode("ascii")


def _open(key: bytes, field_name: str, owner: str, sealed: Any) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not isinstance(sealed, str) or not sealed.startswith(_SEAL_PREFIX):
        raise SealBroken("a protected value for this computer is malformed; share it again")
    try:
        blob = base64.urlsafe_b64decode(sealed[len(_SEAL_PREFIX) :].encode("ascii"))
        nonce, body = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
        return AESGCM(key).decrypt(nonce, body, _aad(field_name, owner))
    except (InvalidTag, ValueError) as exc:
        raise SealBroken(
            "a protected value for this computer does not open; share it again"
        ) from exc


def _has_sealed(doc: dict[str, Any]) -> bool:
    if "install_key_sealed" in doc:
        return True
    rows = doc.get("grants")
    if not isinstance(rows, dict):
        return False
    return any(
        isinstance(r, dict) and any(f"{n}_sealed" in r for n in _SECRET_FIELDS)
        for r in rows.values()
    )


def _legacy_install_key(doc: dict[str, Any]) -> Optional[bytes]:
    """A plaintext install key an earlier build of this feature wrote, if any."""
    raw = doc.get("install_key")
    if not (isinstance(raw, str) and len(raw) == 2 * _INSTALL_KEY_BYTES):
        return None
    try:
        return bytes.fromhex(raw)
    except ValueError:
        return None


def _doc_fingerprint(doc: dict[str, Any]) -> str:
    from kiro_crew.connected_computers.identity import install_fingerprint

    stored = doc.get("fingerprint")
    if isinstance(stored, str) and stored:
        return stored
    legacy = _legacy_install_key(doc)
    return install_fingerprint(legacy) if legacy is not None else ""


def _pending(doc: dict[str, Any]) -> dict[str, Any]:
    rows = doc.get("pending_forget")
    if not isinstance(rows, dict):
        rows = {}
        doc["pending_forget"] = rows
    return rows


def _pending_key(instance_id: str, device_id: str) -> str:
    return f"{instance_id}/{device_id}"


def _queue_forget(doc: dict[str, Any], grant: Grant, now: float) -> None:
    fingerprint = _doc_fingerprint(doc)
    if not fingerprint:
        # No install key was ever made, so no enrollment ever happened.
        return
    rows = _pending(doc)
    key = _pending_key(grant.instance_id, grant.device_id)
    rows[key] = {
        "instance_id": grant.instance_id,
        "gateway_id": grant.gateway_id,
        "device_id": grant.device_id,
        "fingerprint": fingerprint,
        "queued": now,
        "attempts": 0,
        "next_try": 0.0,
    }
    if len(rows) > MAX_PENDING_FORGETS:
        oldest = sorted(rows, key=lambda k: float(rows[k].get("queued") or 0.0))
        for stale in oldest[: len(rows) - MAX_PENDING_FORGETS]:
            rows.pop(stale, None)


def _pending_entry(key: str, raw: Any) -> Optional[PendingForget]:
    if not isinstance(raw, dict):
        return None
    try:
        return PendingForget(
            key=key,
            instance_id=str(raw["instance_id"]),
            gateway_id=str(raw.get("gateway_id") or ""),
            device_id=str(raw["device_id"]),
            fingerprint=str(raw["fingerprint"]),
            queued=float(raw.get("queued") or 0.0),
            attempts=int(raw.get("attempts") or 0),
            next_try=float(raw.get("next_try") or 0.0),
        )
    except (KeyError, TypeError, ValueError):
        return None


class LocalGrantStore:
    def __init__(
        self,
        vault: Any,
        *,
        protector: Optional[ProtectedStore] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._doc = VaultJsonDoc(vault, LOCAL_ENTRY)
        self._protector = protector
        self._clock = clock

    @staticmethod
    def _grants(doc: dict[str, Any]) -> dict[str, Any]:
        rows = doc.get("grants")
        if not isinstance(rows, dict):
            rows = {}
            doc["grants"] = rows
        return rows

    # -- the OS data key --------------------------------------------------------------
    def _protect(self) -> ProtectedStore:
        if self._protector is None:
            self._protector = protected_store_for_this_machine()
        return self._protector

    def _data_key(self) -> bytes:
        """The OS-kept data key, created on first use; refuses rather than replace one."""
        from kiro_crew.config.paths import config_dir
        from kiro_crew.platform_compat import file_lock, open_lock_file

        # Separate from the vault's write lock: the key id is saved while held.
        # Each caller opens its own descriptor, excluding threads and processes.
        lock_path = config_dir() / "connected-computers-key.lock"
        with open_lock_file(lock_path) as fd, file_lock(fd, exclusive=True):
            return self._data_key_locked()

    def _data_key_locked(self) -> bytes:
        """Re-read both stores under the data home's key-initialization lock."""
        protector = self._protect()
        doc = self._doc.read()
        stored = doc.get("key_id") if isinstance(doc.get("key_id"), str) else ""
        key = protector.load()
        if key is not None and stored == _key_id(key):
            return key
        if stored and _has_sealed(doc):
            raise SealBroken(
                f"the {protector.backend} no longer holds this computer's key; share it again"
            )
        if key is None:
            key = os.urandom(32)
            protector.save(key)
        kid = _key_id(key)

        def mutate(d: dict[str, Any]) -> None:
            current = d.get("key_id")
            if current and current != kid and _has_sealed(d):
                raise SealBroken("this computer's protected key changed meanwhile; try again")
            d["key_id"] = kid

        self._doc.update(mutate)
        return key

    def protection_ready(self) -> str:
        """The OS store's name once it holds the data key; raises when it cannot."""
        self._data_key()
        return self._protect().backend

    def _expect_key(self, doc: dict[str, Any], key: bytes) -> None:
        if doc.get("key_id") != _key_id(key):
            raise VaultDocError("this computer's protected key changed meanwhile; try again")

    # -- the install key ---------------------------------------------------------------
    def install_key(self) -> bytes:
        """This install's 32-byte key, created on first use; sealed in the vault."""
        from kiro_crew.connected_computers.identity import install_fingerprint

        doc = self._doc.read()
        if "install_key_sealed" in doc:
            key = self._data_key()
            return _open(key, "install_key", "", doc["install_key_sealed"])
        legacy = _legacy_install_key(doc)
        data_key = self._data_key()
        candidate = legacy if legacy is not None else os.urandom(_INSTALL_KEY_BYTES)
        sealed = _seal(data_key, "install_key", "", candidate)

        def mutate(d: dict[str, Any]) -> Optional[str]:
            if "install_key_sealed" in d:
                return None  # another writer made it first; use theirs
            self._expect_key(d, data_key)
            # A plaintext key from an earlier build is kept (same fingerprint, so
            # the same device id on every gateway) and its plaintext removed.
            d["install_key_sealed"] = sealed
            d["fingerprint"] = install_fingerprint(candidate)
            d.pop("install_key", None)
            return sealed

        if self._doc.update(mutate) is not None:
            return candidate
        return _open(data_key, "install_key", "", self._doc.read()["install_key_sealed"])

    def fingerprint(self) -> str:
        """The install fingerprint, stored beside the sealed key (it is not a secret)."""
        found = _doc_fingerprint(self._doc.read())
        if found:
            return found
        from kiro_crew.connected_computers.identity import install_fingerprint

        return install_fingerprint(self.install_key())

    # -- reads ---------------------------------------------------------------------------
    def list(self) -> list[Grant]:
        rows = self._grants(self._doc.read())
        out = [g for g in (_grant(v) for v in rows.values()) if g is not None]
        return sorted(out, key=lambda g: (g.created, g.grant_id))

    def active(self) -> List[Grant]:
        return [g for g in self.list() if g.active]

    def get(self, grant_id: str) -> Optional[Grant]:
        return _grant(self._grants(self._doc.read()).get(grant_id))

    def unsealed(self, grant_id: str) -> Optional[Grant]:
        """The grant with its credential and peer key opened; needs the OS store."""
        raw = self._grants(self._doc.read()).get(grant_id)
        grant = _grant(raw)
        if grant is None or not grant.active:
            return grant
        assert isinstance(raw, dict)
        if not any(f"{n}_sealed" in raw for n in _SECRET_FIELDS):
            return grant
        key = self._data_key()
        opened = {
            n: _open(key, n, grant_id, raw[f"{n}_sealed"]).decode("utf-8")
            for n in _SECRET_FIELDS
            if f"{n}_sealed" in raw
        }
        return replace(
            grant, credential=opened.get("credential", ""), peer_key=opened.get("peer_key", "")
        )

    def off_generation(self) -> int:
        try:
            return int(self._doc.read().get("off_generation") or 0)
        except (TypeError, ValueError) as exc:
            raise VaultDocError("connected computers state is invalid") from exc

    def pending_forgets(self) -> List[PendingForget]:
        rows = _pending(self._doc.read())
        out = [p for p in (_pending_entry(k, v) for k, v in rows.items()) if p is not None]
        return sorted(out, key=lambda p: (p.queued, p.key))

    # -- writes --------------------------------------------------------------------------
    def _sealed_secrets(self, grant_id: str, values: dict[str, str]) -> tuple[dict, bytes]:
        if not any(values.values()):
            return {}, b""
        key = self._data_key()
        sealed = {
            name: _seal(key, name, grant_id, value.encode("utf-8"))
            for name, value in values.items()
            if value
        }
        return sealed, key

    def add(self, grant: Grant, *, expect_off_generation: Optional[int] = None) -> Grant:
        """Store ``grant`` (a fresh consent); an older one for its instance or gateway is revoked.

        ``expect_off_generation`` is the generation read when the consent began:
        a local Off since then refuses the write (:class:`TurnedOff`).
        """
        now = self._clock()
        stored = replace(grant, status=GRANT_ACTIVE, created=now, revoked_at=0.0)
        sealed, key = self._sealed_secrets(
            stored.grant_id, {"credential": grant.credential, "peer_key": grant.peer_key}
        )

        def mutate(doc: dict[str, Any]) -> Grant:
            generation = int(doc.get("off_generation") or 0)
            if expect_off_generation is not None and generation != expect_off_generation:
                raise TurnedOff("access was turned off on this computer meanwhile")
            if key:
                self._expect_key(doc, key)
            rows = self._grants(doc)
            for row_key, raw in list(rows.items()):
                old = _grant(raw)
                # One grant per remote gateway, however it is reached: two local
                # entries for the same gateway share one device record there, so
                # this enrollment already ended the older entry's credential.
                if old is None or not old.active:
                    continue
                same_gateway = bool(grant.gateway_id) and old.gateway_id == grant.gateway_id
                if old.instance_id == grant.instance_id or same_gateway:
                    rows[row_key] = _revoke_row(raw, now)
                    if old.device_id != grant.device_id:
                        _queue_forget(doc, old, now)
            # This consent re-enrolled the pair, which already refused the old
            # credential there: a stale queued revoke must not end the new one,
            # whichever local entry queued it for this gateway's device.
            pending = _pending(doc)
            pending.pop(_pending_key(grant.instance_id, grant.device_id), None)
            if grant.gateway_id:
                for pkey, prow in list(pending.items()):
                    if (
                        isinstance(prow, dict)
                        and prow.get("gateway_id") == grant.gateway_id
                        and prow.get("device_id") == grant.device_id
                    ):
                        pending.pop(pkey, None)
            rows[stored.grant_id] = _row(stored, sealed)
            if len(rows) > MAX_GRANTS:
                revoked = sorted(
                    (
                        g
                        for g in (_grant(v) for v in rows.values())
                        if g is not None and not g.active
                    ),
                    key=lambda g: g.revoked_at,
                )
                for old in revoked[: len(rows) - MAX_GRANTS]:
                    rows.pop(old.grant_id, None)
            return stored

        return self._doc.update(mutate)

    def set_credential(
        self, grant_id: str, credential: str, expires: float, peer_key: str = ""
    ) -> Optional[Grant]:
        """Replace an ACTIVE grant's credential and peer key; a revoked one stays revoked.

        Each enrollment derives a new peer key, so a renewal that brings none
        leaves the grant unable to connect until it is shared again.
        """
        sealed, key = self._sealed_secrets(
            grant_id, {"credential": credential, "peer_key": peer_key}
        )

        def mutate(doc: dict[str, Any]) -> Optional[Grant]:
            rows = self._grants(doc)
            raw = rows.get(grant_id)
            old = _grant(raw)
            if old is None or not old.active:
                return None
            if key:
                self._expect_key(doc, key)
            new = replace(old, credential_expires=expires)
            rows[grant_id] = _row(new, sealed)
            return replace(new, credential=credential, peer_key=peer_key)

        return self._doc.update(mutate)

    def set_ended(self, grant_id: str, reason: str) -> Optional[Grant]:
        """Record that an ACTIVE grant's connector ended for good, with ``reason``.

        Needs no OS store (the sealed values are left as they are), so it holds
        when they fail to open. A revoked or missing grant is left alone.
        """

        def mutate(doc: dict[str, Any]) -> Optional[Grant]:
            rows = self._grants(doc)
            raw = rows.get(grant_id)
            old = _grant(raw)
            if old is None or not old.active:
                return None
            assert isinstance(raw, dict)
            text = "".join(ch for ch in str(reason) if ch.isprintable())[:MAX_ENDED_CHARS]
            rows[grant_id] = {**raw, "ended": text or "ended"}
            return replace(old, ended=text or "ended")

        return self._doc.update(mutate)

    def revoke(self, grant_id: str) -> Optional[Grant]:
        """Revoke one grant (needs no OS store) and queue its remote revoke."""
        now = self._clock()

        def mutate(doc: dict[str, Any]) -> Optional[Grant]:
            rows = self._grants(doc)
            raw = rows.get(grant_id)
            old = _grant(raw)
            if old is None:
                return None
            assert isinstance(raw, dict)
            rows[grant_id] = _revoke_row(raw, now)
            if old.active:
                _queue_forget(doc, old, now)
            return old

        return self._doc.update(mutate)

    def revoke_all(self) -> List[Grant]:
        """The local Off: revoke every active grant, bump the generation, queue remotes.

        Needs no OS store and no network, so it holds offline and with the
        keyring locked. Returns the grants that were active.
        """
        now = self._clock()

        def mutate(doc: dict[str, Any]) -> list[Grant]:
            doc["off_generation"] = int(doc.get("off_generation") or 0) + 1
            rows = self._grants(doc)
            ended = []
            for key, raw in list(rows.items()):
                old = _grant(raw)
                if old is not None and old.active:
                    rows[key] = _revoke_row(raw, now)
                    _queue_forget(doc, old, now)
                    ended.append(old)
            return ended

        return self._doc.update(mutate)

    def queue_forget(self, instance_id: str, gateway_id: str, device_id: str) -> None:
        """Queue a remote revoke for an enrollment no grant was written for."""
        now = self._clock()
        stub = Grant(
            grant_id="",
            instance_id=instance_id,
            gateway_id=gateway_id,
            gateway_label="",
            device_id=device_id,
            root="",
        )

        def mutate(doc: dict[str, Any]) -> None:
            _queue_forget(doc, stub, now)

        self._doc.update(mutate)

    def reset_protection(self) -> List[Grant]:
        """Start over after the OS lost the data key: an explicit, local-only step.

        Every grant is revoked (with its remote revoke queued, as an Off), and
        every sealed value, the install key and the key id are dropped, since
        none can be opened again. The next consent makes a new install key, so
        each remote gateway sees a new computer; the old one stays there until
        its owner removes it. Nothing on either computer is deleted.
        """
        now = self._clock()

        def mutate(doc: dict[str, Any]) -> list[Grant]:
            doc["off_generation"] = int(doc.get("off_generation") or 0) + 1
            rows = self._grants(doc)
            ended = []
            for key, raw in list(rows.items()):
                old = _grant(raw)
                if old is None:
                    continue
                if old.active:
                    _queue_forget(doc, old, now)
                    ended.append(old)
                rows[key] = _revoke_row(raw, now)
            for name in ("install_key_sealed", "install_key", "fingerprint", "key_id"):
                doc.pop(name, None)
            return ended

        return self._doc.update(mutate)

    def forget_done(self, key: str) -> None:
        """The remote confirmed the revoke: drop it from the queue."""

        def mutate(doc: dict[str, Any]) -> None:
            _pending(doc).pop(key, None)

        self._doc.update(mutate)

    def forget_failed(self, key: str, *, now: Optional[float] = None) -> None:
        """The remote could not be told: retry later, with back-off."""
        stamp = self._clock() if now is None else now

        def mutate(doc: dict[str, Any]) -> None:
            raw = _pending(doc).get(key)
            if not isinstance(raw, dict):
                return
            attempts = int(raw.get("attempts") or 0) + 1
            # The exponent is capped: the delay hits its maximum long before, and an
            # uncapped one overflows a float after about a thousand failures.
            doublings = min(max(attempts - 1, 0), _FORGET_MAX_DOUBLINGS)
            delay = min(FORGET_RETRY_MIN_SECS * (2**doublings), FORGET_RETRY_MAX_SECS)
            raw["attempts"] = attempts
            raw["next_try"] = stamp + delay

        self._doc.update(mutate)


def new_grant_id() -> str:
    return f"g{secrets.token_hex(8)}"
