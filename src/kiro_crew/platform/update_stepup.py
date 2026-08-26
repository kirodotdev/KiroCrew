"""Host-local step-up for the dashboard's in-app wheel update (RFC OQ7).

A dashboard session is NOT sufficient authority to install code: issue #1762
documents that IP pinning breaks under every same-host proxy, which makes the
session token an effectively transferable bearer for remote access. Acceptable
for chat and operations; not for replacing the gateway's own bytes. So the
in-app Apply is split into two actions with different authority:

* **Arm** (the SPA can do this): record a pending update request and write a
  single-use approval nonce to a file in the data home. The nonce never
  travels to the SPA — the arm response carries a request id and the command
  to run, nothing that approves anything.
* **Approve** (only the host can do this): ``kirocrew update approve`` reads
  the nonce from that file and presents it back to the gateway. Reading the
  file requires filesystem access as the gateway's own user, which is exactly
  the identity the step-up exists to prove. A remote dashboard bearer cannot
  read the gateway host's disk, so it cannot mint an approval.

What this deliberately does NOT defend against, per the RFC's security
section: local code execution as the gateway's user. An on-host process that
can read the data home can approve an update — and can equally replace the
venv directly. The adversary this mechanism is about is the network-reachable
dashboard bearer, and against that one the file is a real boundary.

The nonce file is the ONLY state. The gateway holds nothing in memory, so an
armed request survives nothing it shouldn't: a gateway restart discards it
(the file's TTL still bounds it), and two arms overwrite — last writer wins,
which is fine because arming grants nothing by itself.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.config.paths import data_home
from kiro_crew.platform_compat import make_owner_only_dir

logger = logging.getLogger(__name__)

#: How long an armed request stays approvable. RFC OQ7 names ~10 minutes:
#: long enough to switch to a terminal, short enough that a forgotten arm
#: does not linger as a standing approval-in-waiting.
PENDING_TTL_SECS = 600

#: Lives under the data home's ``trust/`` directory — a whole-directory
#: keystone leaf (``security._CREW_SECRET_LEAVES``), so the agent's file gate
#: and every bash form (cat/tee/redirect/extract) refuse it. Without the
#: fence, a remote dashboard bearer could instruct the agent to read the
#: nonce and POST the approval, collapsing the two authorities this split
#: exists to keep apart. The gateway writes it directly (keystone readers
#: never route through is_sensitive_path), so arming is unaffected.
_PENDING_FILENAME = "pending-update-approval.json"


class StepUpError(Exception):
    """An arm/approve step failed; the message is operator-facing."""


@dataclass(frozen=True)
class PendingUpdate:
    """One armed update request, as persisted in the nonce file."""

    request_id: str
    nonce: str
    version: str
    channel: str
    created_at: float

    @property
    def expires_in(self) -> int:
        return max(0, int(self.created_at + PENDING_TTL_SECS - time.time()))

    @property
    def expired(self) -> bool:
        return self.expires_in <= 0


def pending_path() -> Path:
    return data_home() / "trust" / _PENDING_FILENAME


def arm(version: str, channel: str, *, source: str = "dashboard") -> PendingUpdate:
    """Record a pending update request; return it (nonce included, for the FILE).

    The caller serving the SPA must never forward the nonce — hand the SPA
    :func:`public_view` instead. Written atomically (temp + ``os.replace``)
    with owner-only permissions, replacing any previous request: arming grants
    nothing by itself, so last-writer-wins needs no coordination.
    """
    pending = PendingUpdate(
        request_id=secrets.token_hex(8),
        nonce=secrets.token_hex(32),
        version=version,
        channel=channel,
        created_at=time.time(),
    )
    path = pending_path()
    # Owner-only from BIRTH, not chmod-after-write: under umask 022 a plain
    # write_text creates the temp 0644, and the instant before a tighten is
    # exactly when another local account could read the nonce. The directory
    # is created owner-only too, and the file is opened O_CREAT|O_EXCL with
    # mode 0600 so no readable moment ever exists.
    make_owner_only_dir(path.parent)
    # The request id, not the pid: two concurrent arms run in the SAME process
    # (executor threads), so a pid-keyed temp name is one shared file both
    # writers interleave into. The request id is fresh entropy per arm.
    tmp = path.with_name(f"{path.name}.{pending.request_id}.tmp")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "request_id": pending.request_id,
                        "nonce": pending.nonce,
                        "version": pending.version,
                        "channel": pending.channel,
                        "created_at": pending.created_at,
                        "source": source,
                    }
                )
            )
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise StepUpError(f"could not record the pending update request: {exc}") from exc
    logger.info(
        "Armed update request %s (v%s, %s channel, from %s)",
        pending.request_id,
        version,
        channel,
        source,
    )
    return pending


def read_pending() -> PendingUpdate | None:
    """The current armed request, or ``None`` when absent, expired or unreadable.

    An expired file is removed on read so a stale arm cannot sit on disk as a
    standing invitation. Unreadable/malformed files also read as ``None`` —
    an approval must never be minted from a file this module cannot vouch for.
    """
    path = pending_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        pending = PendingUpdate(
            request_id=str(raw["request_id"]),
            nonce=str(raw["nonce"]),
            version=str(raw["version"]),
            channel=str(raw["channel"]),
            created_at=float(raw["created_at"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if pending.expired:
        clear_pending()
        return None
    return pending


def consume(nonce: str) -> PendingUpdate:
    """Validate *nonce* against the armed request and consume it (single-use).

    The comparison is constant-time. The file is removed BEFORE this returns,
    so a second approve with the same nonce fails whatever the first one went
    on to do — single-use means the apply gets at most one trigger.
    """
    pending = read_pending()
    if pending is None:
        raise StepUpError(
            "no armed update request (it may have expired) — arm one from the "
            "dashboard's About panel first"
        )
    if not nonce or not hmac.compare_digest(pending.nonce, nonce):
        raise StepUpError("approval nonce does not match the armed request")
    clear_pending()
    return pending


def clear_pending() -> None:
    try:
        pending_path().unlink(missing_ok=True)
    except OSError:
        pass


def public_view(pending: PendingUpdate) -> dict[str, object]:
    """The SPA-safe projection: everything EXCEPT the nonce."""
    return {
        "armed": True,
        "request_id": pending.request_id,
        "version": pending.version,
        "channel": pending.channel,
        "expires_in": pending.expires_in,
        "approve_command": "kirocrew update approve",
    }


__all__ = [
    "PENDING_TTL_SECS",
    "PendingUpdate",
    "StepUpError",
    "arm",
    "clear_pending",
    "consume",
    "pending_path",
    "public_view",
    "read_pending",
]
