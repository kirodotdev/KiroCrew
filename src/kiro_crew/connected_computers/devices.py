"""The remote gateway's record of the computers enrolled with it.

One record per device id, kept in the gateway's vault (:mod:`.vault_doc`). A
record carries the owner it was enrolled by, the install fingerprint that proves
which computer it is, and a credential generation (``cgen``): a fresh
enrollment, a revocation (Disconnect, or a local Off's queued forget) and a
removal bump it unless the record is already removed, and a credential carrying
an older generation is refused (:mod:`.connector_token`). A renewal keeps it, so a
renewal whose answer was lost can be repeated with the same credential. A
removed record is terminal, including after another revoke or remove request.
Nothing here ever reaches the computer itself:
revoking or removing a record deletes no file and stops nothing but this
gateway's acceptance of that computer (B12).
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, replace
from typing import Any, Callable, Final, Optional

from kiro_crew.connected_computers.identity import (
    IdentityError,
    assign_device_id,
    clean_label,
    validate_device_id,
    validate_fingerprint,
)
from kiro_crew.connected_computers.vault_doc import VaultJsonDoc

#: The vault entry holding every record.
DEVICES_ENTRY: Final[str] = "CONNECTED_COMPUTERS_DEVICES"
#: Most computers one gateway keeps records for (removed ones included).
MAX_DEVICES: Final[int] = 64

STATUS_ACTIVE: Final[str] = "active"
STATUS_REVOKED: Final[str] = "revoked"
STATUS_REMOVED: Final[str] = "removed"
_STATUSES: Final[frozenset[str]] = frozenset({STATUS_ACTIVE, STATUS_REVOKED, STATUS_REMOVED})
_OWNER_MAX: Final[int] = 128


class EnrollmentRefused(ValueError):
    """An enrollment this gateway will not accept; the message says why.

    ``code`` is ``revoked``, ``removed`` or ``superseded`` when a renewal met an
    explicit Disconnect, Remove or re-enrollment, else ``refused``.
    """

    def __init__(self, message: str, code: str = "refused") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DeviceRecord:
    device_id: str
    label: str
    owner: str
    fingerprint: str
    cgen: int
    status: str
    created: float
    updated: float

    @property
    def active(self) -> bool:
        return self.status == STATUS_ACTIVE

    def public(self) -> dict[str, Any]:
        """What the dashboard and CLI show: never the fingerprint."""
        return {
            "device_id": self.device_id,
            "label": self.label,
            "status": self.status,
            "created": self.created,
            "updated": self.updated,
        }


def _record(raw: Any) -> Optional[DeviceRecord]:
    """A stored row, or None when it is malformed (it then matches nothing)."""
    if not isinstance(raw, dict):
        return None
    try:
        rec = DeviceRecord(
            device_id=validate_device_id(raw["device_id"]),
            label=str(raw.get("label") or ""),
            owner=str(raw["owner"]),
            fingerprint=validate_fingerprint(raw["fingerprint"]),
            cgen=int(raw["cgen"]),
            status=str(raw["status"]),
            created=float(raw.get("created") or 0.0),
            updated=float(raw.get("updated") or 0.0),
        )
    except (KeyError, TypeError, ValueError, IdentityError):
        return None
    if rec.status not in _STATUSES or rec.cgen < 1 or not rec.owner:
        return None
    return rec


class DeviceRegistry:
    """Enroll, look up, revoke and remove this gateway's connected computers."""

    def __init__(self, vault: Any, *, clock: Callable[[], float] = time.time) -> None:
        self._doc = VaultJsonDoc(vault, DEVICES_ENTRY)
        self._clock = clock

    @staticmethod
    def _rows(doc: dict[str, Any]) -> dict[str, Any]:
        rows = doc.get("devices")
        if not isinstance(rows, dict):
            rows = {}
            doc["devices"] = rows
        return rows

    def list(self) -> list[DeviceRecord]:
        rows = self._rows(self._doc.read())
        out = [r for r in (_record(v) for v in rows.values()) if r is not None]
        return sorted(out, key=lambda r: r.device_id)

    def get(self, device_id: str) -> Optional[DeviceRecord]:
        try:
            validate_device_id(device_id)
        except IdentityError:
            return None
        rec = _record(self._rows(self._doc.read()).get(device_id))
        return rec if rec is not None and rec.device_id == device_id else None

    def enroll(
        self,
        *,
        base: str,
        fingerprint: str,
        label: str,
        owner: str,
        renew: bool = False,
        renew_cgen: Optional[int] = None,
    ) -> DeviceRecord:
        """Give the install ``fingerprint`` its id and a fresh credential generation.

        A fresh enrollment (``renew=False``) is the local user's new native
        consent, made over SSH as this gateway's owner: it may bring a revoked
        computer back, and it moves the generation on. A renewal (``renew=True``)
        only extends an ACTIVE record and keeps its generation, so a revoke on
        this gateway is never undone by a connector renewing on its own (B07,
        B11). ``renew_cgen`` is the generation of the credential the renewal
        presented (:func:`.connector_token.verify_for_renewal`); the record must
        still carry it. A removed computer is never enrolled again (B12). A
        fingerprint never takes over another one's id.
        """
        validate_fingerprint(fingerprint)
        owner = str(owner or "")[:_OWNER_MAX]
        if not owner:
            raise EnrollmentRefused("the enrolling owner is unknown")
        now = self._clock()

        def mutate(doc: dict[str, Any]) -> DeviceRecord:
            rows = self._rows(doc)
            existing = {}
            for key, raw in rows.items():
                rec = _record(raw)
                # A malformed row still holds its id, so no one is assigned it.
                existing[key] = rec.fingerprint if rec is not None else ""
            try:
                device_id = assign_device_id(base, fingerprint, existing)
            except IdentityError as exc:
                raise EnrollmentRefused(str(exc)) from exc
            prior = _record(rows.get(device_id))
            if prior is not None and prior.fingerprint != fingerprint:
                raise EnrollmentRefused("that device id belongs to another computer")
            if prior is not None and prior.owner != owner:
                raise EnrollmentRefused("this computer is enrolled by another owner")
            if prior is None and device_id in rows:
                raise EnrollmentRefused("this gateway's record of that device is unreadable")
            if prior is not None and prior.status == STATUS_REMOVED:
                raise EnrollmentRefused(
                    "this computer was removed from this gateway", STATUS_REMOVED
                )
            if renew and (prior is None or prior.status != STATUS_ACTIVE):
                raise EnrollmentRefused(
                    "access for this computer was revoked on this gateway; share it again",
                    STATUS_REVOKED,
                )
            if renew_cgen is not None and (prior is None or prior.cgen != renew_cgen):
                # Checked in the same locked write: a revoke or re-enrollment
                # after the old credential was verified still wins.
                raise EnrollmentRefused(
                    "the credential being renewed was replaced or revoked; share it again",
                    "superseded",
                )
            if prior is None and len(rows) >= MAX_DEVICES:
                raise EnrollmentRefused("this gateway already has the most computers it keeps")
            rec = DeviceRecord(
                device_id=device_id,
                label=clean_label(label) or device_id,
                owner=owner,
                fingerprint=fingerprint,
                cgen=(prior.cgen + (0 if renew else 1)) if prior is not None else 1,
                status=STATUS_ACTIVE,
                created=prior.created if prior is not None else now,
                updated=now,
            )
            rows[device_id] = asdict(rec)
            return rec

        return self._doc.update(mutate)

    def _set_status(self, device_id: str, status: str) -> Optional[DeviceRecord]:
        try:
            validate_device_id(device_id)
        except IdentityError:
            return None
        now = self._clock()

        def mutate(doc: dict[str, Any]) -> Optional[DeviceRecord]:
            rows = self._rows(doc)
            prior = _record(rows.get(device_id))
            if prior is None:
                return None
            if prior.status == STATUS_REMOVED:
                return prior
            rec = replace(prior, status=status, cgen=prior.cgen + 1, updated=now)
            rows[device_id] = asdict(rec)
            return rec

        return self._doc.update(mutate)

    def revoke(self, device_id: str) -> Optional[DeviceRecord]:
        """Refuse every credential this computer holds; its chats stay bound to it."""
        return self._set_status(device_id, STATUS_REVOKED)

    def remove(self, device_id: str) -> Optional[DeviceRecord]:
        """Retire the computer here. Its files and the computer are untouched, the
        id is never reassigned, and a chat bound to it reports it unavailable."""
        return self._set_status(device_id, STATUS_REMOVED)
