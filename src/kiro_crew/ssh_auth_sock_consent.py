"""Owner consent to forward the ssh-agent socket into the agent sandbox.

Keeping ``SSH_AUTH_SOCK`` in the agent subprocess environment lets git commit
signing and git-over-SSH inside the sandbox reach the ssh-agent the operator
runs outside it. The socket grants USE of the operator's keys, not possession:
the private key material never enters the sandbox and, under the strict tier,
``~/.ssh`` stays read-denied. But USE is enough -- any code the agent runs can
authenticate as the operator through the socket for the lifetime of the session,
not commit signing alone.

Where the consent lives, and why not ``config.json``
----------------------------------------------------
``ssh_auth_sock_consent.json`` sits on the KEYSTONE floor
(``security._CREW_SECRET_LEAVES``), the same placement as ``computer_use.json``,
``aws_service_consent.json``, ``oauth_endpoints.json`` and
``file_delivery_consent.json``, and for the same reason: this is an
authorization, not a preference. ``config.json`` is writable by any auto-approved
agent shell, so consent stored there could be minted by a prompt-injected agent
-- it would flip its own ``SSH_AUTH_SOCK`` forwarding on, and a subagent it spawns
would then authenticate as the operator with keys the sandbox exists to keep out
of its reach. The OS sandbox mounts the keystone read-only for the agent's shell
and ``is_sensitive_path`` blocks the file tools, so the consent is un-flippable
from inside the sandbox.

How the operator grants it
--------------------------
Recording a grant is TWO acts with different authority, the exact shape of
:mod:`kiro_crew.file_delivery_consent` (whose module comment carries the full
reasoning; it is not repeated here):

* **Arm** (the owner's dashboard can do this): ``POST /api/ssh-agent/consent/arm``
  records a pending request and writes a single-use approval nonce to the
  sandbox-HIDDEN leaf ``ssh-auth-sock-consent-pending/``. The nonce never
  travels to the SPA; the response carries a request id and the command to run.
* **Approve** (only the host can do this): ``kirocrew ssh-agent approve`` reads
  the nonce from that leaf and presents it to the loopback-only
  ``POST /api/ssh-agent/consent/approve``, which records the grant. The verb is a
  STEP-UP, not a self-grant: it consumes an owner-armed nonce an in-sandbox
  caller cannot read (the leaf is bind-masked in every sandbox mode), so a
  terminal command that "records the grant on request" does not exist -- the
  command authorizes nothing unless the owner armed first, and an agent cannot
  reach the nonce it needs. The ``self-protection-ssh-agent`` denied-command
  floor additionally refuses the agent the verb itself.

The armed request carries ``safety_epoch()`` (imported from
``file_delivery_consent``, where it is generic: it digests the computer-use
switch and the sandbox confinement answers, none of which is file-delivery
state), so a request is claimable only while every condition that makes the host
step-up trustworthy still reads as it did when the owner armed it. Approve also
refuses outright while computer use is enabled: with desktop input synthesis on,
an agent could TYPE the approve command into a host terminal.

A hand-written ``{"enabled": true}`` in ``<config_dir>/ssh_auth_sock_consent.json``
keeps working: :func:`is_granted` reads exactly that field and nothing else, so
the old out-of-band edit and the dashboard grant are indistinguishable at spawn.

Withdrawal is a single owner-gated DELETE with no step-up, deliberately: revoking
is the fail-safe direction, and a step-up on it would let the party the consent
constrains keep a grant alive by making withdrawal harder. The same DELETE also
discards any armed-but-unapproved request (the panel's Cancel), so "no" means no
grant AND nothing left approvable.

Known limit, stated rather than papered over
--------------------------------------------
The grant is durable and coarse: once enabled, every later spawn forwards the
socket without asking again -- that is the point (an unattended cron must be able
to sign), and it is also the cost. The grant is read at agent SPAWN
(``sandbox._forward_ssh_auth_sock``), so a session already running when consent
is granted or revoked keeps the environment it started with; the change takes
effect for new sessions. Every spawn that forwards under the grant is resolved
through :func:`is_granted`, which fails closed on anything it cannot read as an
explicit enable.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import ssh_auth_sock_consent_path
from kiro_crew.config.paths import data_home
from kiro_crew.file_delivery_consent import GRANT_PENDING_TTL_SECS, StepUpError, safety_epoch
from kiro_crew.platform_compat import make_owner_only_dir

logger = logging.getLogger(__name__)

#: The host command that finishes an armed grant. Surfaced by the arm response so
#: the SPA shows the exact words; the CLI dispatch in ``cli.py`` is the other copy.
APPROVE_COMMAND = "kirocrew ssh-agent approve"

#: SEL operation prefix. Outcomes: ``granted`` / ``revoked`` / ``cancelled`` / ``refused``.
AUDIT_EVENT = "ssh_auth_sock_forward_consent"

#: Serialises the read-modify-write of the store. An IN-PROCESS lock and
#: deliberately NOT a lock file, for the reason ``file_delivery_consent._STORE_LOCK``
#: gives in full: a sibling lock file is agent-reachable through a runtime-
#: constructed shell path, and an agent holding it could block the owner's REVOKE
#: -- a consent whose withdrawal the constrained party can deny is defective. One
#: writing process (the gateway's owner-gated handler) makes a process-local lock
#: sufficient; the CLI verb never writes the store, it drives the handler.
_STORE_LOCK = threading.Lock()

#: Serialises arming against the compare-and-unlink of the pending nonce. Same
#: reasoning as ``file_delivery_consent._PENDING_LOCK``: both racing actors are
#: threads on the gateway's ``to_thread`` pool, and the lock is held across
#: :func:`arm_grant`'s write and :func:`claim_grant`'s read-compare-unlink so a
#: fresh arm cannot be deleted by a claim that compared the old file. A DISTINCT
#: lock from ``_STORE_LOCK`` so the two critical sections never nest.
_PENDING_LOCK = threading.Lock()

#: Serialises the two OWNER-VISIBLE transactions against each other: approve
#: (claim the nonce, then persist the grant) and withdraw (revoke the grant, then
#: discard the nonce). Without it the two can interleave -- approve consumes the
#: nonce, the owner's DELETE finds no grant and no request and answers "not
#: granted", then approve persists the grant anyway -- so a cancel the owner saw
#: succeed is silently undone and forwarding is enabled while the panel reads
#: "Not allowed". Held across BOTH steps of each transaction, outside the two
#: finer locks (which it never nests inside), so whichever transaction starts
#: first completes before the other looks: a DELETE that arrives during an
#: approve waits and then revokes the grant it just wrote; one that arrives
#: before leaves the approve a stale nonce (409). Same in-process reasoning as
#: the other two locks: one gateway process is the only writer of either file.
_TXN_LOCK = threading.Lock()

#: The armed-grant nonce lives in its OWN top-level leaf, registered BOTH in
#: ``security._CREW_SECRET_LEAVES`` (file gate refuses a statically-named agent
#: write) AND ``sandbox._CREW_HIDDEN_LEAVES`` (bind-masked in every sandbox mode,
#: so the directory is not even visible to a spawned command), and precreated
#: before every spawn (``_CREW_PRECREATE_HIDDEN_DIR_LEAVES``) so the isdir-guarded
#: mask has a name to bind over on a fresh install. The mask is the load-bearing
#: half, and the leaf is deliberately NOT under ``trust/`` (sandbox-visible for SEL
#: appends, so forgeable). A whole DIRECTORY because arming renames a sibling
#: ``.tmp`` into place and a mask covers the leaf, not its ancestors. See the
#: matching constant in ``file_delivery_consent`` for the full argument.
_PENDING_GRANT_DIRNAME = "ssh-auth-sock-consent-pending"
_PENDING_GRANT_FILENAME = "nonce.json"


@dataclass(frozen=True)
class Grant:
    """A recorded consent to forward ``SSH_AUTH_SOCK`` into the sandbox."""

    granted_at: str

    def to_dict(self) -> dict[str, Any]:
        # ``enabled`` is the field :func:`is_granted` reads, byte-for-byte what a
        # hand-written store carries; ``granted_at`` is the only addition.
        return {"enabled": True, "granted_at": self.granted_at}


def _read_all() -> dict[str, Any]:
    """The whole store, or ``{}`` when it is missing or unreadable.

    Failing soft is the right READ behaviour -- an authorization record that
    cannot be parsed is not an authorization, so the forward stays scrubbed.
    """
    try:
        raw = json.loads(ssh_auth_sock_consent_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        logger.warning(
            "ssh-agent forward consent store is unreadable; treating the socket as unconfirmed"
        )
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_all(data: dict[str, Any]) -> None:
    # Fail-loud lockdown BEFORE any content lands, same as the sibling keystone
    # stores: restrict_to_owner=True applies the owner-only mode to the temp file
    # before the payload reaches it, and the default restrict_on_error="raise"
    # refuses to write a record it cannot protect.
    atomic_write(
        ssh_auth_sock_consent_path(),
        json.dumps(data, indent=2, sort_keys=True),
        restrict_to_owner=True,
    )


def is_granted() -> bool:
    """Whether the operator has confirmed forwarding ``SSH_AUTH_SOCK``.

    Fails closed to False on a missing, unreadable, or malformed store, and only
    an explicit boolean ``True`` in the ``enabled`` field counts -- a truthy
    string or number does not, so a partially written or hand-edited store cannot
    forward the socket by accident. LOCAL only -- no network, no probe.
    """
    return _read_all().get("enabled") is True


def read_grant() -> Grant | None:
    """The recorded grant, or ``None`` when forwarding is not granted.

    Decided by :func:`is_granted` alone, so a hand-written ``{"enabled": true}``
    with no ``granted_at`` is a grant with an empty timestamp, never a refusal.
    """
    data = _read_all()
    if data.get("enabled") is not True:
        return None
    granted_at = data.get("granted_at")
    return Grant(granted_at=granted_at if isinstance(granted_at, str) else "")


def record_grant(*, granted_at: str) -> Grant:
    """Persist the operator's consent to forward the socket."""
    grant = Grant(granted_at=granted_at)
    with _STORE_LOCK:
        # The store is REPLACED, not merged: it holds one decision and nothing
        # else, so a stale foreign key cannot survive a re-grant. No corrupt-
        # sidecar preservation either, for the reason ``file_delivery_consent``
        # gives: an unreadable copy of a one-field record has nothing worth
        # recovering, and a sidecar would be an unfenced artifact.
        _write_all(grant.to_dict())
    audit_decision(outcome="granted")
    return grant


def revoke() -> bool:
    """Withdraw consent. True when a grant was removed.

    Writes an explicit ``{"enabled": false}`` rather than unlinking the file: the
    leaf is precreated (as ``{}``) and sealed read-only for the sandbox before
    every spawn, so an absent file would only be recreated, and an explicit false
    reads unambiguously to a human inspecting the store by hand.

    The write happens WHETHER OR NOT the current record could be read. ``_read_all``
    fails soft to ``{}`` on an unreadable store, which is right for a read (no
    consent) and wrong for a withdrawal: skipping the write there would answer
    "not granted" while an enabled record stays on disk for the next spawn to
    read once the fault clears (``persist-before-you-publish``). So the only
    thing the read decides is the return value (whether a grant WAS held); a
    write failure propagates as ``OSError`` for the caller to report.
    """
    with _STORE_LOCK:
        was_granted = _read_all().get("enabled") is True
        _write_all({"enabled": False})
    if was_granted:
        audit_decision(outcome="revoked")
    return was_granted


def socket_present() -> bool:
    """Whether a spawn under the grant would have an ssh-agent socket to forward.

    Answered the way the spawn path answers it, not by the gateway's own
    environment alone: the SDK's ``resolve_ssh_auth_sock`` repairs a missing or
    stale ``SSH_AUTH_SOCK`` from the well-known agent socket locations (launchd
    listeners on macOS, ``/tmp/ssh-*/agent.*``, the systemd user
    ``ssh-agent.socket`` and the keyring socket on Linux), so a gateway started
    as a launchd/systemd service with no variable in its environment still
    forwards a live socket. The Settings panel holds the Allow control on a False
    here, so it must read False only when a spawn would really forward nothing.
    Reached through ``kiro_crew.agent_sdk`` rather than the ACP layer (the
    agent-sdk-boundary gate refuses a new edge). Runs on a COPY of the
    environment: resolving must not mutate the gateway's own. Filesystem globs
    and stats, no connect; call it off the event loop.
    """
    from kiro_crew.agent_sdk.drivers.acp import resolve_ssh_auth_sock  # heavy module; lazy

    env = dict(os.environ)
    resolve_ssh_auth_sock(env)
    sock = env.get("SSH_AUTH_SOCK", "")
    # The resolver leaves a value it cannot replace in place, so a variable
    # pointing at a socket from an SSH login that has since ended survives it.
    # A path that is not there is nothing to forward: an ``exists`` check, no
    # connect. On Windows the variable is absent and the resolver is a no-op.
    return bool(sock) and os.path.exists(sock)


# ── Human-only step-up before a grant is recorded ───────────────────────────
#
# Why an owner-session check alone is not enough, and why the nonce leaf must be
# bind-MASKED rather than merely file-gated, is argued in full in the matching
# section of ``file_delivery_consent``. This module adopts that design unchanged;
# the pieces below are the per-store copies (own lock, own leaf, own record
# shape), and the generic pieces (``safety_epoch``, ``StepUpError``, the TTL) are
# imported from there rather than duplicated.


class StaleNonceError(StepUpError):
    """The presented nonce names no live armed request (absent, expired, wrong, consumed).

    A subclass so the approve route can answer 409 (re-arm and retry) for this
    family while a plain :class:`StepUpError` -- the safety epoch moved -- stays a
    403, because that one is an authorization refusal, not a stale request.
    """


def safety_revision() -> str:
    """A digest of the IDENTITY of every file whose contents ``safety_epoch`` reads.

    ``safety_epoch`` digests the VALUES of the trustworthiness conditions, so a
    setting toggled away and back (computer use off → on → off inside the TTL)
    reproduces the digest the owner armed under, and a request that was never
    claimable while the setting was flipped becomes claimable again. This digest
    closes that: the governing files (``computer_use.json`` and the two settings
    files ``configured_sandbox_mode`` reads) are written through ``atomic_write``,
    which replaces the file, so every write moves ``st_ino`` and ``st_mtime_ns``
    even when the bytes come back the same. Any write to any of them between arm
    and claim is therefore a different revision, and the claim refuses. An absent
    file is a distinct, stable marker (``-``); an unreadable one is ``?`` so it can
    never equal a readable one. Never raises.
    """
    from kiro_crew.computer_use.enable_state import computer_use_state_path
    from kiro_crew.config.loader import config_local_path, config_path

    parts: list[str] = []
    for probe in (computer_use_state_path, config_path, config_local_path):
        try:
            st = os.stat(probe())
            parts.append(f"{st.st_ino}:{st.st_mtime_ns}:{st.st_size}")
        except FileNotFoundError:
            parts.append("-")
        except Exception:  # noqa: BLE001 -- unreadable must not equal readable
            parts.append("?")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


@dataclass(frozen=True)
class PendingGrant:
    """One armed grant request, as persisted in the nonce file."""

    request_id: str
    nonce: str
    created_at: float
    safety_epoch: str = ""
    safety_revision: str = ""

    @property
    def expires_in(self) -> int:
        return max(0, int(self.created_at + GRANT_PENDING_TTL_SECS - time.time()))

    @property
    def expired(self) -> bool:
        return self.expires_in <= 0


def _pending_record(pending: PendingGrant, source: str) -> str:
    """The nonce file's bytes: every :class:`PendingGrant` field plus who armed it.

    ``source`` is ``"dashboard"`` for an arm (the owner-gated route is the one
    writer) and ``"restored"`` when a claimed request is handed back after a
    failed grant write, so a hand inspection of the leaf can tell the two apart.
    """
    return json.dumps({**asdict(pending), "source": source})


def pending_grant_path():
    return data_home() / _PENDING_GRANT_DIRNAME / _PENDING_GRANT_FILENAME


def arm_grant() -> PendingGrant:
    """Record a pending grant request; return it (nonce included, for the FILE).

    The caller serving the SPA must never forward the nonce -- hand the SPA
    :func:`public_pending_view` instead. Written owner-only from birth, replacing
    any previous request: arming grants nothing by itself, so last-writer-wins
    needs no coordination.
    """
    pending = PendingGrant(
        request_id=secrets.token_hex(8),
        nonce=secrets.token_hex(32),
        created_at=time.time(),
        # Stamped at ARM, compared at claim: the request is only claimable while
        # every trustworthiness condition still reads as it did for the owner
        # (the epoch), AND none of the files those conditions come from has been
        # written since (the revision) -- a value toggled away and back is not
        # the owner's configuration, it is a configuration restored under it.
        safety_epoch=safety_epoch(),
        safety_revision=safety_revision(),
    )
    path = pending_grant_path()
    with _PENDING_LOCK:
        make_owner_only_dir(path.parent)
        try:
            atomic_write(path, _pending_record(pending, "dashboard"), restrict_to_owner=True)
        except OSError as exc:
            raise StepUpError(f"could not record the pending grant request: {exc}") from exc
    logger.info("Armed ssh-agent forward consent request %s", pending.request_id)
    return pending


def read_pending_grant() -> PendingGrant | None:
    """The current armed request, or ``None`` when absent/expired/unreadable.

    An expired request reads as ``None`` and is left on disk rather than unlinked
    here, so a concurrent arm landing between the expiry check and an unlink is
    never deleted; an expired row is already inert. Unreadable or malformed files
    also read as ``None``: an approval must never be minted from a file this
    module cannot vouch for.
    """
    path = pending_grant_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        pending = PendingGrant(
            request_id=str(raw["request_id"]),
            nonce=str(raw["nonce"]),
            created_at=float(raw["created_at"]),
            # Left EMPTY when absent rather than defaulted to the current value,
            # so such a row can never match and the claim refuses.
            safety_epoch=str(raw.get("safety_epoch", "")),
            safety_revision=str(raw.get("safety_revision", "")),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if pending.expired:
        return None
    return pending


def _same_secret(expected: str, presented: str) -> bool:
    # Compared as BYTES, never as ``str``: ``hmac.compare_digest`` raises
    # ``TypeError`` on a str holding a non-ASCII character, and the nonce arrives
    # in a request body, so a malformed one would otherwise escape as a 500.
    # ``surrogatepass`` so a lone surrogate still compares rather than raises.
    return hmac.compare_digest(
        expected.encode("utf-8", "surrogatepass"),
        presented.encode("utf-8", "surrogatepass"),
    )


def claim_grant(nonce: str) -> PendingGrant:
    """Validate *nonce* AND consume the armed request, atomically.

    Single-use is decided in one step under :data:`_PENDING_LOCK`, the same lock
    :func:`arm_grant` holds across its replace, so exactly one caller can leave
    here holding a given request. Raises :class:`StepUpError` on a missing,
    expired or mismatched nonce (the :class:`StaleNonceError` subclass), or when
    the safety epoch moved since the owner armed, having consumed nothing. A caller whose own write then fails hands the
    request back with :func:`restore_pending_grant`.
    """
    with _PENDING_LOCK:
        pending = read_pending_grant()
        if pending is None:
            raise StaleNonceError(
                "no armed grant request (it may have expired) -- confirm from the "
                "dashboard's Security panel first"
            )
        if not nonce or not _same_secret(pending.nonce, nonce):
            raise StaleNonceError("approval nonce does not match the armed request")
        # Compared INSIDE the claim so no caller can consume a request whose
        # conditions moved after it was armed (an agent under computer use arms,
        # queues a delayed approve, then turns computer use off).
        # Both the VALUE digest and the file-identity REVISION must match: the
        # epoch alone is restored by toggling a setting away and back within the
        # TTL (computer use off -> on -> off), which would let a delayed approve
        # typed under computer use succeed once the owner turns it off again.
        if (
            not pending.safety_epoch
            or not _same_secret(pending.safety_epoch, safety_epoch())
            or not pending.safety_revision
            or not _same_secret(pending.safety_revision, safety_revision())
        ):
            raise StepUpError(
                "the sandbox or computer-use configuration changed after this request "
                "was armed, so it no longer proves a human approved it -- confirm "
                "from the dashboard's Security panel again"
            )
        try:
            os.unlink(pending_grant_path())
        except FileNotFoundError:
            # Another claimer won between the read and the unlink.
            raise StaleNonceError("approval nonce does not match the armed request") from None
        except OSError as exc:
            raise StepUpError(f"could not consume the armed grant request: {exc}") from exc
    return pending


def restore_pending_grant(pending: PendingGrant) -> bool:
    """Re-arm a claimed request after the caller's own write failed.

    Restores ONLY when the path is still empty: a request armed after this one
    was claimed is NEWER, and overwriting it would delete a live request nobody
    approved. Never raises; a restore that cannot be written leaves the owner
    re-arming, which is the same remedy.
    """
    path = pending_grant_path()
    with _PENDING_LOCK:
        if os.path.lexists(path):
            return False
        try:
            make_owner_only_dir(path.parent)
            atomic_write(path, _pending_record(pending, "restored"), restrict_to_owner=True)
        except OSError:
            return False
    return True


def discard_pending_grant() -> bool:
    """Drop any armed request without approving it. True when one was removed.

    The owner's Cancel in the armed block and the owner's Revoke are the same
    DELETE, so withdrawing consent also withdraws a request-in-waiting: a nonce
    that survived a revoke would re-show the armed block on the next poll and,
    worse, stay approvable for the rest of its TTL after the owner said no.
    Under :data:`_PENDING_LOCK`, the lock arm and claim hold, so a discard can
    neither delete a request armed after it looked nor race a claim mid-unlink.
    An unreadable or expired file is removed too: both are inert, and the owner
    asked for nothing pending.

    A failed unlink for any reason other than "already absent" RAISES the
    ``OSError`` rather than returning False: False means "nothing was armed",
    and reporting that for a nonce that is still on disk would tell the owner
    the cancel succeeded while the request stays approvable until it expires.
    The caller answers with an error and the owner can cancel again.
    """
    with _PENDING_LOCK:
        try:
            os.unlink(pending_grant_path())
        except FileNotFoundError:
            return False
    audit_decision(outcome="cancelled")
    return True


class GrantWriteFailed(OSError):
    """Persisting a CLAIMED grant failed; ``restored`` says whether the request was re-armed."""

    def __init__(self, cause: OSError, *, restored: bool) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.restored = restored


def claim_and_record(nonce: str, *, granted_at: str) -> Grant:
    """The whole approve transaction: consume the nonce, then persist the grant.

    One critical section under :data:`_TXN_LOCK` so an owner's :func:`withdraw`
    can never land between the claim and the write (see the lock's comment for
    the interleaving this closes). Raises :class:`StepUpError` (or its
    :class:`StaleNonceError` subclass) from the claim having written nothing, and
    :class:`GrantWriteFailed` when the store write fails -- by then the request
    has been handed back with :func:`restore_pending_grant` when nothing newer was
    armed, and ``restored`` tells the caller which message to give.
    """
    with _TXN_LOCK:
        pending = claim_grant(nonce)
        try:
            return record_grant(granted_at=granted_at)
        except OSError as exc:
            restored = restore_pending_grant(pending)
            raise GrantWriteFailed(exc, restored=restored) from exc


def withdraw() -> tuple[bool, bool]:
    """The whole withdraw transaction: revoke any grant, discard any armed request.

    Returns ``(revoked, discarded)``. Under :data:`_TXN_LOCK` for the same reason
    as :func:`claim_and_record`: an approve in flight either finishes before this
    looks (and is then revoked here) or finds its nonce gone (409). Nothing the
    owner was told succeeded is undone afterwards. An ``OSError`` from either
    step propagates: a withdraw that could not remove what it was asked to remove
    must not report success (``persist-before-you-publish``). The grant is
    revoked BEFORE the nonce is discarded, so a failure in the second step
    leaves the safer half done.
    """
    with _TXN_LOCK:
        revoked = revoke()
        discarded = discard_pending_grant()
    return revoked, discarded


def public_pending_view(pending: PendingGrant | None) -> dict[str, Any]:
    """The SPA-safe projection: everything EXCEPT the nonce."""
    if pending is None:
        return {
            "armed": False,
            "request_id": None,
            "expires_in": None,
            "approve_command": APPROVE_COMMAND,
        }
    return {
        "armed": True,
        "request_id": pending.request_id,
        "expires_in": pending.expires_in,
        "approve_command": APPROVE_COMMAND,
    }


def audit_decision(*, outcome: str, detail: str = "") -> None:
    """Record a grant, a revocation, a cancelled arm or a refusal in the SEL.

    ``outcome`` is one of ``granted`` / ``revoked`` / ``cancelled`` / ``refused``
    (``cancelled`` = an armed request discarded unapproved); a refusal's
    ``detail`` says which leg refused (non-owner caller, computer use active,
    bad or expired nonce, off-host caller). A refused caller gets an error string
    back, but that string goes to the CALLER, while this log is what the owner
    reads, so the refusal is recorded here to reach them at all.

    Never raises: an audit failure must not be what stops a refusal from being
    enforced. Imported lazily because this module is reached from the sandbox
    spawn path, which must not pull the security-event stack for a read.
    """
    try:
        from kiro_crew.platform.context import redact_log_via_context
        from kiro_crew.sel import sel

        # Caller text bound for a durable audit field: redact over the FULL text
        # before the clip, so a credential straddling the boundary cannot survive
        # as an unmatchable prefix.
        sel().log_api_access(
            caller="owner" if outcome in ("granted", "revoked", "cancelled") else "gateway",
            operation=f"{AUDIT_EVENT}.{outcome}",
            outcome=outcome,
            source="ssh-auth-sock-consent",
            resources=redact_log_via_context(detail)[:200] if detail else "SSH_AUTH_SOCK",
        )
    except Exception:  # pragma: no cover - audit must never break the gate
        logger.debug("could not write the ssh-agent consent audit event", exc_info=True)
