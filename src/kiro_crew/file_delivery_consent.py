"""Explicit owner consent before Kiro Crew delivers a file whose contents the
credential scanner flags.

An agent can legitimately generate secret material the owner needs delivered --
a VPN device private key inside a compose stack the owner will deploy on another
machine is the reported case. Every delivery surface refuses it today, and the
refusal is CORRECT rather than over-eager: that file matches the PEM private key
branch of ``security._CREDENTIAL_PATTERNS``, the highest-confidence detector in
the catalogue. The detector is right, so no amount of tuning is the remedy --
what is missing is a way for the owner to say "yes, that is mine, hand it over."

Selecting the destination class IS the consent point, not the delivery
-----------------------------------------------------------------------
The delivery cannot be the confirmation point, for the same reason
:mod:`kiro_crew.aws_consent` gives for a paid AWS call: ``file_send`` fires from
surfaces with nobody watching. A cron job exports a report, a subagent hands back
an artifact, a Slack thread reply attaches a file. A per-invocation "Deliver /
Cancel" card has no one to answer it there, and "no confirmation available means
no delivery" would leave the feature exactly as broken as the hard wall it
replaced.

There is a second, sharper reason here. The same scanner rule is enforced at FOUR
independent points, and only one of them is the tool call:

* ``mcp_tools.messaging.file_send``      -- the MCP tool, before any byte is copied
* ``dashboard.handlers.files``           -- ``POST /api/outbox/notify``
* ``dashboard.handlers.files``           -- ``GET  /api/outbox/{filename}``
* ``dashboard.handlers.files._gate_upload_file`` -- shared by the Slack and
  channel upload legs

A card shown at the tool call can only speak for the first. The other three
re-scan at serve time and know nothing about a click that happened earlier, so a
per-invocation grant would report "delivered", render a card, and then refuse the
download -- worse than today's clean refusal. A durable record is readable at
every gate, which is why the grant is configuration-time and lives on disk.

Which destinations a grant can EVER cover
-----------------------------------------
``GRANTABLE_CLASSES`` has exactly one member, and that is a security property
rather than a starting point.

* ``owner_dashboard`` -- the outbox file on the owner's own disk, the chat file
  card, and the authenticated ``GET /api/outbox/{filename}`` download. The
  audience is the owner's own machine and their own authenticated browser (no
  entry in any ``dashboard.token_auth`` bypass list reaches that route). An owner
  seeing their own secret is not a leak.

Deliberately absent, and named in :data:`NEVER_GRANTABLE_CLASSES` so a reader can
see the omission is a decision:

* the Slack upload leg -- the one destination with a genuine third-party audience
  AND the one an agent aims by argument, since ``file_send``'s schema exposes an
  optional ``channel`` id. A grant reachable by a tool argument is not owner
  consent; it is agent-chosen disclosure wearing consent's name.
* the channel (Telegram / Discord) upload leg -- the destination comes from the
  caller's session map rather than an argument, but a linked conversation is not
  demonstrably 1:1, and an audience that cannot be proved is a reason to refuse
  rather than to assume.

Both of those legs pass through ``_gate_upload_file``, which exists (by its own
docstring) "so the Slack and channel legs cannot drift apart gate by gate". That
function does not read this module, and nothing in this module can be reached
from it. The guarantee is therefore structural: there is no code path by which a
grant arrives at a third-party destination, so the property cannot be undone by
inverting a check -- only by editing that gate, which is a separate decision.

What this does NOT change
-------------------------
No detector, pattern, or threshold moves. ``security.redact`` and its catalogue
are untouched; a grant changes what a gate DOES with a positive result, never
whether the scanner finds it. Note also that ``security.redact`` runs only the
exfiltration-URL and credential passes -- it does not call ``redact_local_paths``
-- so the scope a grant can affect is credentials and exfil URLs, not "anything
sensitive".

Where the grant lives, and why not ``config.json``
--------------------------------------------------
``file_delivery_consent.json`` sits on the read+write KEYSTONE floor
(``security._CREW_SECRET_LEAVES``), the same placement as
``aws_service_consent.json`` and ``computer_use.json``, and for the same reason:
this is an authorization record, not a preference. ``config.json`` is writable by
any auto-approved agent shell, so a grant stored there could be minted by a
prompt-injected agent -- consenting, on the owner's behalf, to shipping the
owner's secrets. The platform's own ``CredentialPolicy.exempt_exact_hosts``
docstring states the rule this file obeys: such a set is "NEVER sourced from
``config.json`` -- an agent-writable exemption would be a hole in the redaction
ceiling."

Recording a grant takes TWO acts, not one
------------------------------------------
The owner-gated dashboard session is an IDENTITY check, not a proof that a human
is present: anyone running computer use points an agent at an owner-authenticated
browser, and a prompt-injected auto-approved click then satisfies the owner gate
and self-grants -- the "agent can self-approve" hole issue #7770 exists to close.
So recording a grant is split, exactly as :mod:`kiro_crew.platform.update_stepup`
splits installing code:

* the OWNER-gated dashboard handler ARMS a request and writes a single-use
  approval nonce to a sandbox-HIDDEN file. Arming grants nothing; the nonce never
  reaches the SPA.
* ``kirocrew file-delivery approve``, run on the gateway host, reads that nonce
  and presents it back. Reading the file needs filesystem access as the
  gateway's own user -- the identity an agent-driven dashboard bearer cannot
  forge. The nonce lives in its own leaf (``file-delivery-consent-pending/``)
  that is bind-MASKED from the agent sandbox in every mode, so a prompt-injected
  agent cannot even forge a nonce there with a runtime-constructed shell path
  (the file gate's text/argv matcher alone would not stop that write; the mask
  does). Only then is the grant recorded.

The CLI verb is therefore the STEP-UP that closes the hole, not a second door
that widens it: it authorizes nothing on its own, it consumes an owner-armed
nonce that an automated caller can neither read nor forge. WITHDRAWAL keeps a
single owner-gated door with no step-up, because revoking is the fail-safe
direction.

Known limit, stated rather than papered over
--------------------------------------------
A grant is durable and coarse. Once ``owner_dashboard`` is confirmed, every later
flagged file reaches the owner's dashboard without asking again -- that is the
point (an unattended cron must be able to deliver), and it is also the cost. The
grant does not distinguish one secret from another, so an agent that generates a
credential the owner did NOT ask for will also be able to put it in the owner's
outbox. What that buys an attacker is bounded by the audience: the file lands on
the owner's own disk and in the owner's own authenticated browser, which is where
the agent could already write it with ordinary file tools. Every delivery under a
grant is SEL-audited as ``sensitive_content_delivered_with_consent`` so the
record exists even though the refusal does not.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import file_delivery_consent_path
from kiro_crew.config.paths import data_home
from kiro_crew.platform_compat import make_owner_only_dir

logger = logging.getLogger(__name__)

#: The one destination class a grant can cover: the owner's own disk plus their
#: own authenticated dashboard (outbox file, chat file card, download route).
#: The id is the stored grant key, so renaming it invalidates existing grants
#: (fail-closed: the owner is asked again) rather than silently authorizing a
#: different destination.
CLASS_OWNER_DASHBOARD = "owner_dashboard"

#: Destination classes a grant may EVER cover. Exactly one member, deliberately.
GRANTABLE_CLASSES: frozenset[str] = frozenset({CLASS_OWNER_DASHBOARD})

#: Destination classes that must never appear in :data:`GRANTABLE_CLASSES`,
#: recorded so the omission reads as a decision rather than an oversight. Both
#: route through ``dashboard.handlers.files._gate_upload_file``, which does not
#: read this module; these ids exist for documentation and for the ratchet test
#: that asserts the two sets stay disjoint.
NEVER_GRANTABLE_CLASSES: frozenset[str] = frozenset({"slack_upload", "channel_upload"})

#: Human-facing labels for the confirmation surface and the log lines. Plain
#: language rather than the internal class id: the confirmation surface is the
#: one place a first-time reader decides whether to allow delivery, so the
#: destination is named in words they can picture ("this computer" / "the
#: dashboard Files view") instead of the product-internal term "outbox".
CLASS_LABELS: dict[str, str] = {
    CLASS_OWNER_DASHBOARD: "This computer and your dashboard Files view",
}

#: Serialises the read-modify-write below. Deliberately an IN-PROCESS lock, and
#: deliberately NOT a lock FILE beside the grant.
#:
#: A sibling lock file is agent-reachable. ``is_sensitive_path`` covers it, so the
#: agent's file tools refuse it, but that is the evadable tier -- a runtime-
#: constructed path escapes the text and argv matchers, exactly as
#: ``sandbox._CREW_READONLY_LEAVES`` says of its own docstring. A sandboxed agent
#: that took such a lock would not read the grant and could not forge it; it would
#: block the owner's REVOKE, leaving consent active. A consent mechanism whose
#: withdrawal can be denied by the party the consent constrains is defective in its
#: central promise, so the artifact is removed rather than defended: an agent cannot
#: hold a lock that does not exist.
#:
#: One writer makes this sufficient. ``aws_consent`` needs a cross-process file lock
#: because it has TWO writers -- its dashboard handler and the ``kirocrew
#: aws-consent`` CLI. This grant still has exactly one writer of the STORE, the
#: owner-gated dashboard handler, running in the gateway process. The
#: ``kirocrew file-delivery approve`` verb does NOT write the store: it consumes
#: an owner-armed nonce and drives the same in-process handler over loopback, so
#: it is a step-up that authorizes a write rather than a second writer of it. One
#: store writer in one process is served by a process-local lock.
#:
#: WHAT IS NO LONGER SERIALISED, stated because it is a narrowing: two gateway
#: processes sharing one data home would not serialise their writes against each
#: other. That configuration was never served by the previous file lock either --
#: the precedent's cross-process lock exists for the CLI, not for multi-gateway --
#: and a torn write still cannot widen a grant, because ``read_grant`` refuses any
#: row whose ``destination_class`` disagrees with its key and ``_read_all`` fails
#: soft to "no consent" on anything unparseable.
_STORE_LOCK = threading.Lock()


@dataclass(frozen=True)
class Grant:
    """A recorded consent to deliver scanner-flagged files to one destination class."""

    destination_class: str
    granted_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "destination_class": self.destination_class,
            "granted_at": self.granted_at,
        }


def _read_all() -> dict[str, Any]:
    """The whole store, or ``{}`` when it is missing or unreadable.

    Failing soft is the right READ behaviour -- an authorization record that
    cannot be parsed is not an authorization, so every gate keeps refusing. See
    :func:`_preserve_if_unreadable` for what happens before a write, where
    failing soft would otherwise discard the unreadable bytes.
    """
    try:
        raw = json.loads(file_delivery_consent_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        logger.warning(
            "file-delivery consent store is unreadable; treating every destination as unconfirmed"
        )
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_all(data: dict[str, Any]) -> None:
    # Fail-loud lockdown BEFORE any content lands, same as the sibling keystone
    # stores: restrict_to_owner=True applies the owner-only DACL to the temp file
    # before the payload reaches it and implies the owner-only POSIX mode. The
    # default restrict_on_error="raise" refuses to write a record it cannot
    # protect. Every failure inside atomic_write happens before the final path is
    # touched, so no cleanup is needed here and an unlink would instead delete the
    # previous, healthy, already-locked-down store on a transient failure.
    atomic_write(
        file_delivery_consent_path(),
        json.dumps(data, indent=2, sort_keys=True),
        restrict_to_owner=True,
    )


def read_grant(destination_class: str) -> Grant | None:
    """The stored grant for ``destination_class``, or ``None`` when there is none.

    Fails soft to ``None`` (no consent) on a missing, unreadable, or malformed
    file: an authorization record that cannot be read is not an authorization.
    """
    row = _read_all().get(destination_class)
    if not isinstance(row, dict):
        return None
    stored = str(row.get("destination_class", ""))
    # A row filed under one key but naming another destination is not a grant for
    # either: refuse rather than trust the key, so a hand-edited or partially
    # written store cannot widen a grant by disagreeing with itself.
    if stored != destination_class:
        logger.warning(
            "file-delivery consent record under %r names %r; treating as absent",
            destination_class,
            stored,
        )
        return None
    return Grant(destination_class=stored, granted_at=str(row.get("granted_at", "")))


def is_granted(destination_class: str) -> bool:
    """Whether the owner has confirmed delivery to ``destination_class``.

    Fail-closed on every unexpected input: a class outside
    :data:`GRANTABLE_CLASSES` is refused before the store is even read, so a
    caller cannot consult this module about a third-party destination and get a
    True. LOCAL only -- no network, no probe.
    """
    if destination_class not in GRANTABLE_CLASSES:
        return False
    return read_grant(destination_class) is not None


def record_grant(destination_class: str, *, granted_at: str) -> Grant:
    """Persist the owner's consent for ``destination_class``."""
    if destination_class not in GRANTABLE_CLASSES:
        raise ValueError(f"destination class {destination_class!r} can never be granted")
    grant = Grant(destination_class=destination_class, granted_at=granted_at)
    with _STORE_LOCK:
        # No corrupt-sidecar preservation, deliberately. ``aws_consent`` copies an
        # unparseable store aside before replacing it because its store can hold
        # SEVERAL service grants with account and caller-ARN detail, which an
        # operator would not want silently discarded. This store holds at most one
        # row of ``{destination_class, granted_at}``: there is nothing in it worth
        # recovering, and losing an already-unreadable copy costs the owner one
        # re-grant.
        #
        # What writing one WOULD cost is an unfenced artifact. ``is_sensitive_path``
        # covers this leaf and its ``.tmp``, but NOT a ``.corrupt-<stamp>`` sibling
        # (measured: False for that suffix on all four keystone consent leaves). A
        # sidecar is also never read back -- the only writers in the tree are this
        # module and ``aws_consent``, with no reader anywhere -- so it could not
        # alter a grant either way. Rather than fence an artifact that has no
        # reader and no value, it is not created: the same reasoning as the absent
        # lock file above.
        data = _read_all()
        data[destination_class] = grant.to_dict()
        _write_all(data)
    audit_decision(destination_class, outcome="granted")
    return grant


def revoke(destination_class: str) -> bool:
    """Drop consent for ``destination_class``. True when a grant was removed."""
    with _STORE_LOCK:
        data = _read_all()
        if destination_class not in data:
            return False
        del data[destination_class]
        _write_all(data)
    audit_decision(destination_class, outcome="revoked")
    return True


# ── Human-only step-up before a grant is recorded ───────────────────────────
#
# WHY THE OWNER GATE IS NOT ENOUGH ON ITS OWN. ``is_owner_dashboard_request``
# proves the request carries the owner's dashboard SESSION, which is an identity
# check, not a proof that a HUMAN is present. Anyone running computer use points
# an agent at an owner-authenticated browser; a prompt-injected agent with an
# auto-approved click then satisfies the owner gate and records a grant that
# makes every later scanner-flagged file deliverable. That is precisely the
# "agent can self-approve" hole issue #7770 set out to close, and an identity
# gate does not close it.
#
# So recording a grant is split into two acts with different authority, exactly
# as :mod:`kiro_crew.platform.update_stepup` splits installing code -- and for
# the same reason (RFC OQ7: a dashboard session is a transferable bearer, fine
# for operations, not for a security-critical write):
#
# * **Arm** (the SPA can do this): record a pending grant request and write a
#   single-use approval nonce to a keystone file. The nonce NEVER travels to the
#   SPA; the arm response carries a request id and the command to run, nothing
#   that approves anything.
# * **Approve** (only the host can do this): ``kirocrew file-delivery approve``
#   reads the nonce from that file and presents it back to the gateway. Reading
#   the file needs filesystem access as the gateway's own user -- the identity an
#   agent-driven browser cannot forge. The nonce lives in ``file-delivery-consent-pending/``,
#   registered in :data:`security._CREW_SECRET_LEAVES` AND bind-MASKED from the
#   agent sandbox via :data:`sandbox._CREW_HIDDEN_LEAVES`, so a prompt-injected
#   agent can neither read it nor forge one there with a runtime-constructed shell
#   path -- the mask, not the text/argv file gate, is what closes the forge path.
#   A remote/automated dashboard bearer cannot read the host's disk either, so it
#   cannot mint an approval.
#
# WHAT THIS DELIBERATELY DOES NOT DEFEND AGAINST, same as update_stepup: genuine
# local code execution as the gateway's own user. A process that can read the
# data home can approve -- and could equally write the owner's outbox directly
# with file tools, which is the bounded audience the module header already names.
# The adversary this closes is the network-reachable / agent-driven dashboard
# bearer, and against that one the nonce file is a real boundary.
#
# ONE agent-reachable path into that "local execution" class IS fenced, though,
# because it is reachable without the operator ever running arbitrary code:
# computer use (desktop observation + input synthesis) would let a prompt-injected
# agent TYPE `kirocrew file-delivery approve` into a host terminal and complete the
# owner's armed step-up itself. So the approve handler refuses to record a grant
# while computer use is enabled (see ``api_file_delivery_consent_approve``): with
# that tier active the terminal step-up no longer proves a human is present, so it
# is excluded rather than trusted. This does not turn the bounded-audience actor
# above into a defended one -- a human at the machine, or code they run, still can
# approve -- it removes the one path an agent could take with a single opt-in flag.
#
# WITHDRAWAL is deliberately NOT stepped up: revoking consent is the fail-safe
# direction, and a step-up on it would let the party the consent constrains keep
# a grant alive by making withdrawal harder. Only the grant-recording direction
# gains a gate.

#: How long an armed grant request stays approvable. Same budget as update
#: step-up (RFC OQ7): long enough to switch to a terminal, short enough that a
#: forgotten arm does not linger as a standing approval-in-waiting.
GRANT_PENDING_TTL_SECS = 600

#: The armed-grant nonce lives in its OWN top-level leaf,
#: ``file-delivery-consent-pending/``, which is registered BOTH in
#: :data:`security._CREW_SECRET_LEAVES` (the file gate refuses a statically-named
#: agent write) AND in :data:`sandbox._CREW_HIDDEN_LEAVES` (bind-masked in every
#: sandbox mode, so the directory is not even visible to a spawned command).
#:
#: The mask is the load-bearing half. It is NOT under ``trust/`` on purpose:
#: ``trust/`` is sandbox-VISIBLE (it holds SEL append targets an in-sandbox MCP
#: server writes), so a prompt-injected agent could FORGE a nonce there with a
#: runtime-constructed shell path — the acknowledged evadable tier the file
#: gate's text/argv matcher does not catch — and then drive the owner's loopback
#: browser to POST that chosen nonce, recording a grant with no human present.
#: Masking a leaf with no in-sandbox reader closes that path: the gateway writes
#: it on arm and the host ``kirocrew file-delivery approve`` reads it, both
#: outside the sandbox, so masking costs no live consumer.
#:
#: A whole DIRECTORY, not a single file, because arming renames a sibling
#: ``.tmp`` into place and a mask covers the leaf, not its ancestors.
_PENDING_GRANT_DIRNAME = "file-delivery-consent-pending"
_PENDING_GRANT_FILENAME = "nonce.json"


class StepUpError(Exception):
    """An arm/approve step failed; the message is owner-facing."""


@dataclass(frozen=True)
class PendingGrant:
    """One armed grant request, as persisted in the nonce file."""

    request_id: str
    nonce: str
    destination_class: str
    created_at: float

    @property
    def expires_in(self) -> int:
        return max(0, int(self.created_at + GRANT_PENDING_TTL_SECS - time.time()))

    @property
    def expired(self) -> bool:
        return self.expires_in <= 0


def pending_grant_path():
    return data_home() / _PENDING_GRANT_DIRNAME / _PENDING_GRANT_FILENAME


def arm_grant(destination_class: str, *, source: str = "dashboard") -> PendingGrant:
    """Record a pending grant request; return it (nonce included, for the FILE).

    The caller serving the SPA must never forward the nonce -- hand the SPA
    :func:`public_pending_view` instead. Written owner-only from birth
    (O_CREAT|O_EXCL, mode 0600) so no world-readable moment exists, replacing any
    previous request: arming grants nothing by itself, so last-writer-wins needs
    no coordination.
    """
    if destination_class not in GRANTABLE_CLASSES:
        raise ValueError(f"destination class {destination_class!r} can never be granted")
    pending = PendingGrant(
        request_id=secrets.token_hex(8),
        nonce=secrets.token_hex(32),
        destination_class=destination_class,
        created_at=time.time(),
    )
    path = pending_grant_path()
    make_owner_only_dir(path.parent)
    # Keyed on the fresh request id, not the pid: two concurrent arms run in the
    # SAME process (executor threads), so a pid-keyed temp name is one shared file
    # both writers interleave into.
    tmp = path.with_name(f"{path.name}.{pending.request_id}.tmp")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "request_id": pending.request_id,
                        "nonce": pending.nonce,
                        "destination_class": pending.destination_class,
                        "created_at": pending.created_at,
                        "source": source,
                    }
                )
            )
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise StepUpError(f"could not record the pending grant request: {exc}") from exc
    logger.info(
        "Armed file-delivery consent request %s for %s (from %s)",
        pending.request_id,
        destination_class,
        source,
    )
    return pending


def read_pending_grant() -> PendingGrant | None:
    """The current armed grant request, or ``None`` when absent/expired/unreadable.

    An expired file is removed on read so a stale arm cannot sit on disk as a
    standing invitation. Unreadable or malformed files also read as ``None`` -- an
    approval must never be minted from a file this module cannot vouch for. A row
    whose ``destination_class`` is not grantable is refused for the same reason
    :func:`read_grant` refuses a self-disagreeing row.
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
            destination_class=str(raw["destination_class"]),
            created_at=float(raw["created_at"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if pending.destination_class not in GRANTABLE_CLASSES:
        return None
    if pending.expired:
        clear_pending_grant()
        return None
    return pending


def consume_grant(nonce: str) -> PendingGrant:
    """Validate *nonce* against the armed request and consume it (single-use).

    Constant-time comparison. The file is removed BEFORE this returns, so a
    second approve with the same nonce fails whatever the first went on to do --
    single-use means one arm authorizes at most one grant.

    Prefer :func:`validate_grant` + an explicit :func:`clear_pending_grant`
    when the caller has an intervening side effect (e.g. recording the grant)
    that must complete BEFORE the nonce is destroyed, so a failure of that step
    leaves the armed request retryable rather than lost.
    """
    pending = validate_grant(nonce)
    clear_pending_grant()
    return pending


def validate_grant(nonce: str) -> PendingGrant:
    """Validate *nonce* against the armed request WITHOUT consuming it.

    Constant-time comparison; raises :class:`StepUpError` on a missing or
    mismatched nonce. Split out from :func:`consume_grant` so a caller can order
    the irreversible unlink (:func:`clear_pending_grant`) AFTER a side effect
    that might fail -- a failed grant write then leaves the nonce valid and the
    owner can retry, instead of a 500 that destroys the armed request. Single-use
    still holds because the caller clears the nonce on the success path, so a
    replay finds nothing to validate against.
    """
    pending = read_pending_grant()
    if pending is None:
        raise StepUpError(
            "no armed grant request (it may have expired) -- confirm from the "
            "dashboard's Security panel first"
        )
    if not nonce or not hmac.compare_digest(pending.nonce, nonce):
        raise StepUpError("approval nonce does not match the armed request")
    return pending


def clear_pending_grant() -> None:
    try:
        os.unlink(pending_grant_path())
    except OSError:
        pass


def public_pending_view(pending: PendingGrant | None) -> dict[str, Any]:
    """The SPA-safe projection: everything EXCEPT the nonce."""
    if pending is None:
        return {"armed": False}
    return {
        "armed": True,
        "request_id": pending.request_id,
        "destination_class": pending.destination_class,
        "expires_in": pending.expires_in,
        "approve_command": "kirocrew file-delivery approve",
    }


def audit_decision(destination_class: str, *, outcome: str, detail: str = "") -> None:
    """Record a consent state change, a denial, or a consented delivery in the SEL.

    Grants, revocations, denials AND deliveries made under a grant are recorded.
    The delivery entry is the point: the refusal it replaces was self-evident in
    the tool's error string, whereas a successful consented delivery would
    otherwise leave no trace that a flagged file left the gate at all. Every
    entry answers a question an incident review actually asks -- who authorized
    delivery, when was it withdrawn, and which flagged files went out under it.

    Never raises: an audit failure must not be what stops a refusal from being
    enforced. Imported lazily because this module is reached from the MCP stdio
    servers, whose stray writes would corrupt the JSON-RPC stream, and because
    the security-event layer pulls the redaction stack the read path never needs.
    """
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller="owner" if outcome in ("granted", "revoked") else "gateway",
            operation=f"file_delivery_consent.{outcome}",
            outcome=outcome,
            source="file-delivery-consent",
            resources=(f"{destination_class}: {detail[:200]}" if detail else destination_class),
        )
    except Exception:  # pragma: no cover - audit must never break the gate
        logger.debug("could not write the file-delivery consent audit event", exc_info=True)
