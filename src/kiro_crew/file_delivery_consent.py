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

The authenticated, OWNER-gated dashboard handler opens the path directly and is
the only writer. There is deliberately no CLI verb: a terminal command that
records a grant on request is a grant an automated caller can take, and its guard
would have to key on an env var an in-process agent can unset.

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

import json
import logging
import threading
from dataclasses import dataclass
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import file_delivery_consent_path

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

#: Human-facing labels for the confirmation surface and the log lines.
CLASS_LABELS: dict[str, str] = {
    CLASS_OWNER_DASHBOARD: "This machine's outbox and my own dashboard",
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
#: aws-consent`` CLI. This grant has exactly one writer, the owner-gated dashboard
#: handler, and deliberately NO CLI verb (a terminal command that records a grant on
#: request is a grant an automated caller can take). One writer in one process is
#: served by a process-local lock.
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
