"""Explicit operator consent before Kiro Crew spends money in an AWS account.

Two optional features reach a PAID AWS service through the provider's own
credential chain: Amazon Polly (text-to-speech, :mod:`kiro_crew.voice_reply`)
and Amazon Transcribe (speech-to-text, :mod:`kiro_crew.transcribe` and
:mod:`kiro_crew.dashboard.stt_stream`). Both omit ``--profile`` / pass no
credential resolver when no profile is configured, so "no profile set" does not
mean "no account" -- it means "whichever account the ambient environment
resolves to", which can be one the operator never intended to bill.

Selecting the provider IS the consent point, not the first request
-----------------------------------------------------------------
The first request cannot be the confirmation point in practice. Polly synthesis
is triggered from surfaces with nobody watching: ``voice_reply`` fires from a
Slack thread reply and from ``auto_reply_to_voice`` (a voice memo arrives, a
spoken reply goes back), and a scheduled job can drive either. A blocking
prompt there has no one to answer it, and "no confirmation available means no
request" would then silently disable the feature rather than protect anyone.

So the gate is at CONFIGURATION time: turning a paid provider on is what asks,
and the answer is durable. What this module enforces at the call site is a
cheap LOCAL check that issues no AWS request of its own -- which matters,
because an identity probe before every synthesis would itself be exactly the
unwanted traffic the operator is trying to avoid.

Where the grant lives, and why not ``config.json``
--------------------------------------------------
``aws_service_consent.json`` sits on the read+write KEYSTONE floor
(``security._CREW_SECRET_LEAVES``), the same placement as ``computer_use.json``
and ``ops_mission_control_policy.json``, and for the same reason: this is an
authorization record, not a preference. ``config.json`` is writable by any
auto-approved agent shell, so a grant stored there could be minted by a
prompt-injected agent -- consenting, on the operator's behalf, to spending the
operator's money. The authenticated dashboard handler opens the path directly and
is the only writer. There is deliberately no CLI verb: a terminal command that
records a grant on request is a grant an automated caller can take, and its guard
would have to key on an env var an in-process agent can unset.

Known limit, stated rather than papered over
--------------------------------------------
A grant is keyed on ``(service, profile, region)`` and records the account id
that was confirmed. A profile NAME is not an account -- ``aws configure set
credential_process ... --profile <name>`` repoints an existing profile without
touching the credential files -- so the live account is re-checked on every
gated call, bounded by a short probe cache, and a mismatch refuses the call and
revokes the grant.

What that does NOT cover: the check needs a probe that can run. When the account
cannot be resolved the call is refused rather than allowed, so the failure mode
is a withheld paid call during an outage, not an unconfirmed charge. And
``aws configure set`` itself remains available to an agent shell, so this
contains the consequence rather than removing the cause.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import aws_consent_path
from kiro_crew.constants import AWS_PROFILE_FIRST_CHARS

logger = logging.getLogger(__name__)

#: Paid AWS services this module gates. The id is the stored grant key, so
#: renaming one invalidates existing grants (fail-closed: the operator is asked
#: again) rather than silently authorizing the wrong service.
SERVICE_POLLY = "polly"
SERVICE_TRANSCRIBE = "transcribe"
#: AWS Control's paid services (spec: docs/system-specs/modules/aws-control.md).
#: Declared ahead of the first billable call — S3 backs the cloud drive (P1),
#: Cost Explorer backs the bill page (~$0.01 per query) — so the consent cards
#: can be confirmed per account before either capability ships.
SERVICE_S3 = "s3"
SERVICE_COST_EXPLORER = "ce"
#: Bedrock KB retrieval (knowledge sources of type ``bedrock_kb``): each
#: Retrieve is a paid call billed to whatever account the source's profile
#: NAME currently resolves to, and knowledge search triggers it with nobody
#: watching -- the same shape as Polly/Transcribe above, so the same gate.
SERVICE_BEDROCK_KB = "bedrock-kb"
GATED_SERVICES: frozenset[str] = frozenset(
    {SERVICE_POLLY, SERVICE_TRANSCRIBE, SERVICE_S3, SERVICE_COST_EXPLORER, SERVICE_BEDROCK_KB}
)

#: Human-facing service names for the confirmation surfaces and the log lines.
SERVICE_LABELS: dict[str, str] = {
    SERVICE_POLLY: "Amazon Polly",
    SERVICE_TRANSCRIBE: "Amazon Transcribe",
    SERVICE_S3: "Amazon S3 (cloud drive storage)",
    SERVICE_COST_EXPLORER: "AWS Cost Explorer",
    SERVICE_BEDROCK_KB: "Amazon Bedrock (knowledge base retrieval)",
}

#: Serialises the read-modify-write of the consent store. Every writer below
#: reads the whole store, edits one key and hands it to ``atomic_write``, which
#: REPLACES the file wholesale, so two concurrent writers (Polly from the voice
#: panel, Transcribe from the STT panel) would each write onto a stale snapshot
#: and the later one would silently drop the other. On an authorization record
#: that is a correctness defect rather than a lost-update annoyance: the dropped
#: grant is a feature that refuses to run while its panel shows it as confirmed.
#:
#: Deliberately an IN-PROCESS lock, and deliberately NOT a lock FILE beside the
#: grant.
#:
#: A sibling lock file is agent-reachable. ``is_sensitive_path`` covers it, so the
#: agent's file tools refuse it, but that is the evadable tier: a
#: runtime-constructed path escapes the text and argv matchers, exactly as
#: ``sandbox._CREW_READONLY_LEAVES`` says of itself. Sealing such a leaf
#: read-only would not close it either, because ``flock(LOCK_EX)`` succeeds on an
#: ``O_RDONLY`` descriptor, so a read-only bind still admits the exclusive hold.
#: A sandboxed agent holding that lock could not read the grant and could not
#: forge it; what it could do is block the owner's REVOKE, leaving consent active
#: for subsequent billable calls. A consent mechanism whose withdrawal can be
#: denied by the party the consent constrains is defective in its central
#: promise, so there is no lock file to hold. Same treatment, and the same
#: reasoning, as ``file_delivery_consent._STORE_LOCK``.
#:
#: One writing PROCESS is what makes this sufficient. Every writer of this store
#: runs in the gateway process, on its ``asyncio.to_thread`` pool: the
#: owner-gated dashboard handler, :func:`authorize`'s drift revoke,
#: :func:`reconcile_drift`, and :func:`revoke_for_profile` from the AWS Control
#: route. There is no CLI verb, for the reason the module docstring gives. The
#: racing actors are therefore THREADS, which is exactly what a
#: ``threading.Lock`` serialises; an OS file lock would guard against a second
#: writing process this design does not have.
#:
#: WHAT IS NOT SERIALISED, stated because it is a narrowing: two gateway
#: processes sharing one data home do not serialise their writes against each
#: other. A file lock would not make that configuration safe either, and a lost
#: update there cannot WIDEN a grant. ``atomic_write`` renames, so no reader sees
#: a torn file, and the losing write can only drop a grant, which makes the gated
#: service refuse and the operator re-confirm.
_STORE_LOCK = threading.Lock()

#: Profile names and regions are interpolated into an ``aws`` CLI argv. Values
#: are argv elements (never a shell string), so the classic injection does not
#: apply, but a leading dash would still let a value be read as an OPTION
#: rather than as the value of the option before it. Constrain both to the
#: charset AWS itself allows and require a leading alphanumeric.
#:
#: DELIBERATE semantic differences from ``constants.AWS_PROFILE_NAME_RE``,
#: so this derives its class from the shared fragment instead of
#: aliasing the compiled pattern: the first char is alphanumeric only
#: (stricter), and the continuation class additionally admits ``@`` and ``=``
#: (IAM entity charset; existing configs may carry them). ``\Z`` (not ``$``)
#: so a trailing newline in a config-sourced value cannot slip past, matching
#: the four sibling sites. ``-`` stays last so the class is a
#: literal, never a range.
_PROFILE_RE = re.compile(rf"^[A-Za-z0-9][{AWS_PROFILE_FIRST_CHARS}@=-]{{0,127}}\Z")
_REGION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

#: How long an identity probe result stays usable. The probe is only run by the
#: confirmation surfaces (settings panel load, CLI ``show``, the grant POST),
#: never on the synthesis path, so this only avoids re-probing across a burst of
#: panel renders.
_PROBE_TTL_SECS = 30.0

_probe_cache: dict[tuple[str, str], tuple[float, "Identity"]] = {}


@dataclass(frozen=True)
class Grant:
    """A recorded consent for one service under one profile+region."""

    service: str
    profile: str
    region: str
    account: str
    arn: str
    granted_at: str
    # Unique per RECORDING, not per content: two byte-identical confirmations
    # (same target, same account, same second -- granted_at is second-granular)
    # are still two different authorizations, and the compare-and-delete
    # revocation paths must be able to tell a replacement from the grant
    # their drift evidence was about. Empty on rows written before the field
    # existed, which compares equal against itself and keeps legacy behavior.
    grant_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "profile": self.profile,
            "region": self.region,
            "account": self.account,
            "arn": self.arn,
            "granted_at": self.granted_at,
            "grant_id": self.grant_id,
        }


@dataclass(frozen=True)
class Identity:
    """Result of ``aws sts get-caller-identity`` for a profile+region."""

    ok: bool
    account: str = ""
    arn: str = ""
    #: Operator-facing reason the probe failed. Already credential-redacted.
    detail: str = ""


def credential_source(profile: str) -> str:
    """Describe WHERE credentials for ``profile`` come from, for display.

    An empty profile is the case the issue reporter named: nothing is passed to
    the provider, so its own default chain resolves (environment variables, the
    shared config's ``default`` profile, container/instance metadata). Naming
    that explicitly is the point -- "default" reads as safe, "whichever account
    the ambient environment resolves to" does not.
    """
    if profile:
        return f"profile {profile}"
    return "AWS CLI default credential provider chain"


def _read_all() -> dict[str, Any]:
    """The whole store, or ``{}`` when it is missing or unreadable.

    Failing soft is the right read behaviour -- an authorization record that
    cannot be parsed is not an authorization, so every service refuses. See
    :func:`_preserve_if_unreadable` for what happens before a WRITE, where
    failing soft would otherwise discard the unreadable bytes.
    """
    try:
        raw = json.loads(aws_consent_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        logger.warning("AWS consent store is unreadable; treating every service as unconfirmed")
        return {}
    return raw if isinstance(raw, dict) else {}


def _preserve_if_unreadable() -> None:
    """Copy an unreadable store aside before a write replaces it.

    ``_read_all`` fails soft to ``{}``, so a write built on it would replace an
    unparseable file wholesale and the old bytes would be gone. What is lost is
    not a working authorization -- an unreadable store already grants nothing --
    but it may still hold the operator's other service grant, and discarding it
    silently is not this function's call to make.

    Preserved rather than refused. Refusing the write would leave an operator
    with a corrupt file unable to re-confirm from the dashboard at all, needing
    manual file surgery to recover, which is a worse outcome than a sidecar copy
    for a file that was already authorizing nothing.
    """
    path = aws_consent_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    except OSError:
        logger.warning("could not read the AWS consent store to preserve it", exc_info=True)
        return
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return  # Readable; the write is a normal read-modify-write.
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    sidecar = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        # restrict_to_owner=True locks the temp file down BEFORE the preserved
        # contents reach it (a post-rename lockdown would leave them readable
        # under the inherited DACL on Windows for the write window)
        # and implies the owner-only POSIX mode. The default
        # restrict_on_error="raise" surfaces a lockdown failure into this
        # except, where the whole preservation attempt is already warn-only —
        # and because the failure happens before the rename, a sidecar that
        # could not be protected never exists at the final path at all.
        atomic_write(sidecar, raw, restrict_to_owner=True)
        logger.warning(
            "AWS consent store was unreadable; preserved the previous contents at %s "
            "before recording a new confirmation",
            sidecar.name,
        )
    except OSError:
        logger.warning("could not preserve the unreadable AWS consent store", exc_info=True)


def _write_all(data: dict[str, Any]) -> None:
    path = aws_consent_path()
    # Fail-loud lockdown BEFORE any content lands, same as the sibling keystone
    # stores: ``restrict_to_owner=True`` applies the owner-only DACL to the temp
    # file before the payload reaches it (a post-rename lockdown would leave
    # the authorization record readable under the inherited DACL on Windows for
    # the write window) and implies the owner-only POSIX mode. The
    # default ``restrict_on_error="raise"`` refuses to write a record it cannot
    # protect.
    #
    # No cleanup on failure. Every failure inside ``atomic_write`` —
    # lockdown, payload write (ENOSPC), rename — happens BEFORE the final path
    # is touched: the helper removes its temp file and re-raises, so an
    # unprotectable record never exists at ``path`` at all. An unlink here would
    # only make sense for a NEW store already PUBLISHED at a wide DACL whose
    # post-write lockdown failed; that state is unreachable, and the unlink
    # would instead delete the previous, healthy, already-locked-down store on
    # any transient failure.
    atomic_write(path, json.dumps(data, indent=2, sort_keys=True), restrict_to_owner=True)


def read_grant(service: str) -> Grant | None:
    """The stored grant for ``service``, or ``None`` when there is none.

    Fails soft to ``None`` (no consent) on a missing, unreadable, or malformed
    file: an authorization record that cannot be read is not an authorization.
    """
    return _grant_from_row(service, _read_all().get(service))


def _grant_from_row(service: str, row: Any) -> Grant | None:
    """:func:`read_grant` over an already-read record (one read per caller)."""
    if not isinstance(row, dict):
        return None
    try:
        return Grant(
            service=str(row["service"]),
            profile=str(row.get("profile", "")),
            region=str(row.get("region", "")),
            account=str(row.get("account", "")),
            arn=str(row.get("arn", "")),
            granted_at=str(row.get("granted_at", "")),
            grant_id=str(row.get("grant_id", "")),
        )
    except (KeyError, TypeError):
        logger.warning("AWS consent record for %r is malformed; treating as absent", service)
        return None


def record_grant(
    service: str, *, profile: str, region: str, account: str, arn: str, granted_at: str
) -> Grant:
    """Persist consent for ``service`` under ``profile``+``region``."""
    if service not in GATED_SERVICES:
        raise ValueError(f"unknown gated service {service!r}")
    grant = Grant(
        service=service,
        profile=profile,
        region=region,
        account=account,
        arn=arn,
        granted_at=granted_at,
        grant_id=uuid.uuid4().hex,
    )
    with _STORE_LOCK:
        # Inside the lock, before the read: a concurrent writer must not be able
        # to slip between preserving the old bytes and replacing them.
        _preserve_if_unreadable()
        data = _read_all()
        data[service] = grant.to_dict()
        _write_all(data)
    audit_decision(
        service,
        outcome="granted",
        detail=f"account={account} region={region or '(provider default)'} "
        f"source={credential_source(profile)}",
    )
    return grant


def revoke(service: str) -> bool:
    """Drop consent for ``service``. Returns True when a grant was removed."""
    with _STORE_LOCK:
        data = _read_all()
        if service not in data:
            return False
        del data[service]
        _write_all(data)
    audit_decision(service, outcome="revoked")
    return True


# CONTRACT: every revocation that acts on evidence read before the lock (drift
# probes, mismatch checks) goes through revoke_if_matches with the FULL
# captured grant identity — never key-wise revoke(). This holds for ALL
# gated services, not only bedrock-kb: the read-to-delete gap is generic,
# and a future service regressing to revoke() re-opens the replaced-grant
# deletion class (found and closed across three call sites in review).
def revoke_if_matches(
    service: str,
    *,
    profile: str,
    region: str,
    account: str,
    arn: str,
    granted_at: str,
    grant_id: str = "",
) -> bool:
    """Compare-and-delete: drop the grant only if it is STILL the one compared.

    ``reconcile_drift`` decides to revoke from a grant it read; a grant
    recorded concurrently (one store key per service) would be deleted by a
    plain key-wise ``revoke`` even though the drift evidence was about its
    predecessor. The read-back and the delete here share ONE lock hold, and
    EVERY persisted field is compared -- ``granted_at`` differs on each
    re-confirmation, so even a same-target same-account replacement is
    recognizably a different grant and spared. Returns True when a grant
    was removed.
    """
    with _STORE_LOCK:
        data = _read_all()
        row = data.get(service)
        if not isinstance(row, dict):
            return False
        if (
            str(row.get("profile", "")) != profile
            or str(row.get("region", "")) != region
            or str(row.get("account", "")) != account
            or str(row.get("arn", "")) != arn
            or str(row.get("granted_at", "")) != granted_at
            or str(row.get("grant_id", "")) != grant_id
        ):
            # A different grant now occupies the key: the drift evidence was
            # about its predecessor, so it is not ours to delete.
            return False
        del data[service]
        _write_all(data)
    audit_decision(service, outcome="revoked")
    return True


def revoke_for_profile(profile: str) -> list[str]:
    """Drop every grant whose recorded profile is ``profile``; returns the services.

    Grants are keyed by service, not by profile, so removing a profile from
    the portal's registry has to scan the gated services for records naming
    it. Leaving such a record behind would let a later re-registration of the
    same name inherit an authorization the operator gave to a different key.

    The match and the delete happen under ONE lock: a grant re-recorded for a
    different profile between a read and an unconditional ``revoke`` would be
    the fresh grant, not the stale one, and deleting it is the silent
    confirmed-but-refuses state the lock exists to prevent.

    Unlike every other reader here this one does NOT fail soft: an unreadable
    store would read as "no grant names this profile", the sweep would write
    nothing, and the caller would go on to forget the profile while its grant
    stays on disk for the next registration under that name to inherit. A
    missing store is the ordinary no-grants case; anything else raises so the
    caller refuses before it mutates.
    """
    revoked: list[str] = []
    with _STORE_LOCK:
        try:
            raw = json.loads(aws_consent_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        data: dict[str, Any] = raw if isinstance(raw, dict) else {}
        for service in sorted(GATED_SERVICES):
            row = data.get(service)
            if isinstance(row, dict) and str(row.get("profile", "")) == profile:
                del data[service]
                revoked.append(service)
        if revoked:
            _write_all(data)
    for service in revoked:
        audit_decision(service, outcome="revoked")
    return revoked


#: Where the gateway records WHICH knowledge-source rows it registered for the
#: Bedrock KB grant. A ``bedrock_kb`` row in ``knowledge.db`` names the KBs a
#: search pays to Retrieve from, but that database is agent-writable
#: in-sandbox (the MCP server ingests into it in-process), so a row alone is
#: not proof the owner approved that KB set: an agent could clone a row and
#: point it at any KB the granted profile reaches. This file is not: it sits
#: in ``sandbox._CREW_READONLY_LEAVES`` and only the gateway writes it, under
#: the same lock as the grants. The paid enumeration therefore searches only
#: rows whose ``(id, uri, kb_ids, region, profile)`` match an entry here that
#: was recorded under the account the CURRENT grant confirms.
#: Keyed under the service name so the per-service readers above, which key
#: the file by service, never see it as a grant.
BEDROCK_SOURCES_KEY = f"{SERVICE_BEDROCK_KB}.sources"

#: The retained ``properties`` keys an attestation pins, in the order the
#: connector reads them. Any change to one of these on the row -- a wider
#: ``kb_ids``, a different region or profile -- is a different registration.
_ATTESTED_PROPERTY_KEYS: tuple[str, ...] = ("kb_ids", "region", "profile")


def _attestation_key(source_id: str, uri: str, properties: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(source_id),
        str(uri),
        *(str(properties.get(k) or "").strip() for k in _ATTESTED_PROPERTY_KEYS),
    )


def _attested_map(data: dict[str, Any]) -> dict[str, Any]:
    rows = data.get(BEDROCK_SOURCES_KEY)
    return rows if isinstance(rows, dict) else {}


def record_source_attestation(
    source_id: str,
    uri: str,
    properties: dict[str, Any],
    *,
    expected_account: str | None = None,
) -> None:
    """Attest that the gateway registered ``bedrock_kb`` row ``source_id``.

    Called from the owner-gated, consent-checked ``add_source`` insert with the
    row's validated ``uri`` and retained properties. Overwrites an entry for the
    same id (a re-registration IS a new attestation).

    The entry also pins the ACCOUNT the grant confirms at this moment, for the
    reason :func:`authorize` re-verifies it on every call: a profile name is
    not an account. Re-pointing the profile and confirming again keeps the
    same profile and region, so a grant-equality check still passes, and
    without the account the old registration would authorize paid retrieval
    of the same KB id in the new account. ``expected_account`` is the account
    the caller validated the KB under; when given, an attestation for any
    other account is refused here too, so the pin can never name an account
    the KB was not probed in, whatever ran between validation and this write.
    Raises when no grant is readable or the account differs; the caller undoes
    its insert rather than keep a row nothing attests.
    """
    with _STORE_LOCK:
        _preserve_if_unreadable()
        data = _read_all()
        grant = _grant_from_row(SERVICE_BEDROCK_KB, data.get(SERVICE_BEDROCK_KB))
        if grant is None or not grant.account:
            raise RuntimeError("no confirmed AWS account to attest the source against")
        if expected_account is not None and grant.account != expected_account:
            raise RuntimeError("the confirmed AWS account changed since the source was validated")
        entry = {
            "uri": str(uri),
            "account": grant.account,
            **{k: str(properties.get(k) or "").strip() for k in _ATTESTED_PROPERTY_KEYS},
        }
        rows = dict(_attested_map(data))
        rows[str(source_id)] = entry
        data[BEDROCK_SOURCES_KEY] = rows
        _write_all(data)


def has_source_attestation(source_id: str) -> bool:
    """Whether the sealed store holds an attestation under ``source_id``.

    The delete route asks this BEFORE it reads the row's type: the type is a
    column in the agent-writable ``knowledge.db``, so a row that was
    registered can be re-labelled by an agent, and a delete that trusted the
    label would skip the owner gate and the revoke, leaving the attestation
    for a row re-minted under the same id and values to inherit. Any account
    counts (a registration left behind by a re-pointed profile still has to
    be revoked), and like :func:`revoke_source_attestation` this does not
    fail soft: a missing store is the ordinary nothing-attested case, an
    unreadable one raises so the caller refuses before it mutates.
    """
    with _STORE_LOCK:
        try:
            raw = json.loads(aws_consent_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
    data: dict[str, Any] = raw if isinstance(raw, dict) else {}
    return str(source_id) in _attested_map(data)


def revoke_source_attestation(source_id: str) -> bool:
    """Drop the attestation for ``source_id``; True when one was removed.

    Called when the row is deleted. An attestation that outlives its row is
    inert (the enumeration starts from the rows), but a later row minted with
    the same id and values must not inherit an approval given to the old one.

    Like :func:`revoke_for_profile`, this reader does NOT fail soft: an
    unreadable store would read as "nothing attested", the caller would go on
    to delete the row, and the attestation would stay on disk for a row
    re-minted under the same id and values to inherit -- ``knowledge.db`` is
    agent-writable, so the id is the writer's to choose. A missing store is
    the ordinary nothing-attested case; anything else raises so the caller
    refuses before it mutates.
    """
    with _STORE_LOCK:
        try:
            raw = json.loads(aws_consent_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        data: dict[str, Any] = raw if isinstance(raw, dict) else {}
        rows = dict(_attested_map(data))
        if str(source_id) not in rows:
            return False
        del rows[str(source_id)]
        data[BEDROCK_SOURCES_KEY] = rows
        _write_all(data)
    return True


def attested_sources() -> dict[str, dict[str, Any]]:
    """The attestation entries by source id, read once per enumeration.

    Only entries recorded under the account the CURRENT grant confirms are
    returned: a grant confirmed for another account later (the profile was
    re-pointed) leaves every earlier registration unattested, so its rows are
    skipped until the owner adds them again. Fails soft to ``{}`` (nothing
    attested): a store that cannot be read, or no grant, attests nothing, the
    same direction :func:`read_grant` fails in.
    """
    data = _read_all()
    grant = _grant_from_row(SERVICE_BEDROCK_KB, data.get(SERVICE_BEDROCK_KB))
    if grant is None or not grant.account:
        return {}
    return {
        str(sid): entry
        for sid, entry in _attested_map(data).items()
        if isinstance(entry, dict) and str(entry.get("account") or "") == grant.account
    }


def source_matches_attestation(
    attested: dict[str, dict[str, Any]], source_id: str, uri: str, properties: dict[str, Any]
) -> bool:
    """True when ``attested`` (from :func:`attested_sources`) pins exactly this row."""
    entry = attested.get(str(source_id))
    if not isinstance(entry, dict):
        return False
    stored = (
        str(source_id),
        str(entry.get("uri") or ""),
        *(str(entry.get(k) or "").strip() for k in _ATTESTED_PROPERTY_KEYS),
    )
    return stored == _attestation_key(source_id, uri, properties)


def is_granted(service: str, *, profile: str, region: str) -> tuple[bool, str]:
    """Whether a grant exists matching this profile+region. LOCAL only.

    Returns ``(granted, reason)``. ``reason`` is an operator-facing sentence for
    the refusal log, so every declined call says what to do about it rather
    than failing mutely.

    This is the first of two checks. It does NOT verify the live account -- see
    :func:`authorize`, which is what call sites use. Kept separate because the
    local half is what the dashboard reports and what the tests pin.
    """
    grant = read_grant(service)
    label = SERVICE_LABELS.get(service, service)
    if grant is None:
        return False, (
            f"{label} use has not been confirmed. Nothing was sent to AWS. "
            f"Confirm it in Settings -> Voice."
        )
    if grant.profile != profile or grant.region != region:
        return False, (
            f"{label} was confirmed for {credential_source(grant.profile)} in region "
            f"{grant.region or '(provider default)'}, but this call would use "
            f"{credential_source(profile)} in region {region or '(provider default)'}. "
            f"Nothing was sent to AWS. Re-confirm in Settings -> Voice."
        )
    return True, ""


async def authorize(service: str, *, profile: str, region: str) -> tuple[bool, str]:
    """The full gate every paid call must pass: local grant AND live account.

    Why the account is re-verified rather than trusted from the grant: a profile
    NAME is not an account. ``aws configure set credential_process ... --profile
    <name>`` repoints an existing profile at a different account without touching
    the credential files directly, so a grant keyed only on the profile name
    would keep authorizing calls after the account under it changed. Checking the
    name alone was measurably insufficient, so the account is checked too.

    Cost is bounded, not per-call: ``sts:GetCallerIdentity`` is free and its
    result is cached for :data:`_PROBE_TTL_SECS`, so a voice-heavy session pays
    one extra free call per window rather than one per synthesis.

    Fails CLOSED in every direction: no grant, a grant naming no account, an
    account that differs (which also revokes the stale grant), an account that
    cannot be resolved, and a grant withdrawn or changed while the check runs.
    That last case matters because the probe spawns a subprocess and is therefore
    a real suspension point, so the grant is re-asserted immediately before the
    allow rather than trusted from before the await.
    """
    granted, reason = is_granted(service, profile=profile, region=region)
    if not granted:
        return False, reason

    label = SERVICE_LABELS.get(service, service)
    grant = read_grant(service)
    if grant is None:
        # Withdrawn between the local check and here. Deny: an absent grant is
        # not a grant, and treating it as one let a call through moments after
        # the operator revoked consent.
        return False, (
            f"{label} consent was withdrawn while this request was being checked. "
            f"Nothing was sent to AWS."
        )
    if not grant.account:
        # A grant that names no account cannot be verified against one. The
        # confirmation path never records such a grant (it refuses without a
        # resolved account), so this only arises from a hand-edited file --
        # which is exactly the case that must not skip the account check.
        return False, (
            f"{label} has a stored confirmation that names no AWS account, so it "
            f"cannot be verified. Nothing was sent to AWS. Re-confirm in "
            f"Settings -> Voice."
        )

    # Cache DELIBERATELY bypassed for an authorization decision. A cached answer
    # means a window in which a profile repointed at another account is still
    # authorized by the previous account's result, and the window is exactly the
    # thing this check exists to close. The probe is free and non-mutating, and it
    # goes to the account the operator already consented to, so paying it per
    # call costs latency rather than money. The cache stays for the confirmation
    # surfaces, where it only coalesces repeated panel renders.
    identity = await probe_identity(profile, region, use_cache=False)
    if not identity.ok or not identity.account:
        # Fail CLOSED. An earlier revision allowed this so a transient STS fault
        # would not stop voice output, but that let a repointed profile bill an
        # unconfirmed account, and on a host with boto3 but no `aws` CLI it meant
        # the account was never verified at all. Denying costs little: a grant
        # can only exist where the probe once succeeded, because the confirmation
        # refuses to record without a resolved account -- so this withholds a
        # paid call during an outage rather than breaking a working setup.
        return False, (
            f"{label} was confirmed for AWS account {grant.account}, but that "
            f"account could not be re-checked just now, so nothing was sent to "
            f"AWS. {identity.detail} The confirmation is unchanged -- retry once "
            f"the AWS CLI can resolve credentials again."
        )

    if identity.account != grant.account:
        # Off the event loop: the revoke does file I/O under the store lock.
        # Compare-and-delete with the CAPTURED grant: the probe is a
        # multi-second suspension point, and a replacement grant recorded
        # during it (the owner confirming another target) must not be deleted
        # on evidence about its predecessor.
        await asyncio.to_thread(
            revoke_if_matches,
            service,
            profile=grant.profile,
            region=grant.region,
            account=grant.account,
            arn=grant.arn,
            granted_at=grant.granted_at,
            grant_id=grant.grant_id,
        )
        return False, (
            f"{label} was confirmed for AWS account {grant.account}, but "
            f"{credential_source(profile)} now resolves to account "
            f"{identity.account}. Nothing was sent to AWS and the confirmation "
            f"was withdrawn. Re-confirm in Settings -> Voice."
        )

    # Audited on every verification. The probe is uncached here, so this is
    # one event per gated call -- the price of the check being per-call.
    #
    # Off the event loop: ``audit_decision`` writes the security event log, and
    # the first such write on a fresh gateway also initialises it, so a large or
    # corrupt log tail would stall every other request and the heartbeat behind
    # this one. This helper is awaited from the gateway's own handlers.
    #
    # Placed BEFORE the re-assertion below, because that offload suspends and the
    # re-assertion has to be the last statement before allowing. The consequence
    # is deliberate: this line records the identity verification that did happen
    # even in the run where the re-assertion then refuses, and the caller audits
    # that refusal, so the log reads verified-then-denied, which is the sequence
    # that occurred.
    await asyncio.to_thread(
        audit_decision,
        service,
        outcome="verified",
        detail=f"account={identity.account} source={credential_source(profile)}",
    )

    # Re-assert the grant immediately before allowing, with NO suspension point
    # between this check and the return. Everything above it suspends: the probe
    # spawns a subprocess and the audit write goes to a thread, either one long
    # enough for the operator to press Withdraw or for a drift check on another
    # request to revoke. Without that adjacency the decision can rest on a grant
    # already withdrawn, and the caller's paid AWS call proceeds after the
    # withdrawal. Same gate-and-act adjacency the repo already applies elsewhere.
    still = read_grant(service)
    if still is None or still.to_dict() != grant.to_dict():
        return False, (
            f"{label} consent changed while this request was being checked. "
            f"Nothing was sent to AWS."
        )

    return True, ""


async def refuse_and_log(service: str, *, profile: str, region: str) -> bool:
    """:func:`authorize` plus the refusal log and audit. True when it may proceed.

    A single helper so every gated call site refuses identically -- the reason
    reaches the operator's log exactly once, at the point the request did not
    happen, and the denial reaches the tamper-evident audit log.

    The audit write goes to a thread for the same reason as the verification one
    in :func:`authorize`: it touches the security event log, this helper is
    awaited on the gateway's event loop, and a refused call must not stall the
    requests behind it.
    """
    granted, reason = await authorize(service, profile=profile, region=region)
    if not granted:
        logger.warning("AWS request refused: %s", reason)
        await asyncio.to_thread(audit_decision, service, outcome="denied", detail=reason)
    return granted


def audit_decision(service: str, *, outcome: str, detail: str = "") -> None:
    """Record a consent state change or a denial in the Security Event Log.

    Grants, revocations and DENIALS are recorded; allows are not. An allow
    happens once per synthesis and would bury the events that matter in noise,
    while every entry here answers a question an operator or an incident review
    actually asks: who authorized spending in this account, when was it
    withdrawn, and what was refused.

    Never raises: an audit failure must not be what stops a refusal from being
    enforced.

    SYNCHRONOUS and BLOCKING, stated here because it is the caller's obligation:
    ``sel()`` writes the security event log, and the first write on a fresh
    gateway also initialises it, so a large or corrupt log tail makes one call
    slow. An ``async`` caller must therefore reach this through
    ``asyncio.to_thread`` rather than inline, or the whole gateway waits behind
    it -- the three consent endpoints, :func:`authorize` and
    :func:`refuse_and_log` all do.
    """
    try:
        # Local imports for the same reason as in ``_redacted``: the redaction
        # stack is heavy and the voice/STT call paths import this module for the
        # local gate alone.
        from kiro_crew.platform.context import redact_log_via_context
        from kiro_crew.sel import sel

        # ``detail`` is caller text (a refusal reason, a credential source) bound
        # for a durable, dashboard-readable audit field. Redaction has to run
        # over the FULL text before the 200-char clip: clipping first cuts a
        # credential straddling the boundary in half, and the surviving prefix
        # matches no credential grammar, so SEL's own write-path pass cannot
        # recover it either. The context-aware spelling is the one for a
        # gate-side audit line (a loaded companion's patterns apply, and it
        # never raises); the slice follows it. The ``if detail`` branch is kept
        # on purpose: an empty ``detail`` must still emit the bare ``service``
        # with no ``": "`` separator.
        sel().log_api_access(
            caller="operator" if outcome in ("granted", "revoked") else "gateway",
            operation=f"aws_consent.{outcome}",
            outcome=outcome,
            source="aws-consent",
            resources=f"{service}: {redact_log_via_context(detail)[:200]}" if detail else service,
        )
    except Exception:  # pragma: no cover - audit must never break the gate
        logger.debug("could not write the AWS consent audit event", exc_info=True)


def target_is_well_formed(profile: str, region: str) -> bool:
    """Whether ``profile``/``region`` have the shapes a grant can name.

    The same two patterns :func:`probe_identity` refuses on, so nothing
    downstream retains a target a grant could never have been confirmed for:
    a profile name is at most 128 characters of the AWS profile alphabet, a
    region at most 64 of ``[a-z0-9-]``. Empty values pass here; whether a
    field may be empty is the caller's rule.
    """
    if profile and not _PROFILE_RE.match(profile):
        return False
    if region and not _REGION_RE.match(region):
        return False
    return True


def _inputs_are_safe(profile: str, region: str) -> bool:
    """Whether the profile/region are shaped safely enough to pass to the CLI.

    ``run_aws`` puts these straight into an argv as the value of ``--profile`` /
    ``--region``. They are argv elements, never a shell string, so the classic
    injection does not apply -- but a leading dash would let a value be read as
    an OPTION rather than as the value of the option before it, and both come
    from ``config.json``, which an auto-approved agent shell can write.
    """
    if profile and not _PROFILE_RE.match(profile):
        logger.warning("refusing identity probe: AWS profile name has an unexpected shape")
        return False
    if region and not _REGION_RE.match(region):
        logger.warning("refusing identity probe: AWS region has an unexpected shape")
        return False
    return True


async def probe_identity(profile: str, region: str, *, use_cache: bool = True) -> Identity:
    """Resolve which account ``profile``+``region`` would actually bill.

    ``sts:GetCallerIdentity`` is free and non-mutating, and it is the ONLY AWS
    call this module makes.

    Delegated to :func:`kiro_crew.cloud.aws.run_aws` rather than spawning the CLI
    here. That is the package's single chokepoint for ``aws`` invocations and it
    already provides everything this probe hand-rolled -- the OS sandbox wrap, a
    credential-scrubbed environment, the resource-limited spawn and a timeout --
    plus one thing the hand-rolled version did not: an agent-session chokepoint
    that only lets exact read-only operations through, and
    ``("sts", "get-caller-identity")`` is already on that allowlist. Three
    siblings (``cloud/iam.py``, ``cloud/source.py``, ``deploy/iam.py``) run this
    same call the same way, so this stops being a fourth spelling. Review found
    the duplication.

    ``run_aws`` is synchronous, so it goes to a thread: this is called from the
    gateway's event loop.
    """
    # Deliberately NO probe-local strip/normalization. Both values come from
    # live config, and the paid consumers (``boto3.Session(profile_name=...)``,
    # the ``--profile`` argv sites) use that same raw value — a probe that
    # validated a normalized COPY could record consent for a target the real
    # request never uses. A whitespace-padded value instead fails the shape
    # gate below (the charset admits no whitespace and ``\Z`` rejects a
    # trailing newline), so the probe refuses it, consent is never
    # granted, and every consumer sees the same verdict. Fix the value in
    # config; nothing here rewrites it.
    key = (profile, region)
    now = asyncio.get_running_loop().time()
    if use_cache:
        cached = _probe_cache.get(key)
        if cached is not None and (now - cached[0]) < _PROBE_TTL_SECS:
            return cached[1]

    if not _inputs_are_safe(profile, region):
        return Identity(ok=False, detail="The configured AWS profile or region is not valid.")
    if not await asyncio.to_thread(_aws_cli_resolvable):
        return Identity(
            ok=False,
            detail=(
                "The AWS CLI could not be found, so the account cannot be shown. "
                "Install it, or choose a local provider that needs no AWS account."
            ),
        )

    try:
        rc, out, err = await asyncio.to_thread(
            _run_aws,
            ["sts", "get-caller-identity", "--output", "json"],
            profile,
            region,
        )
    except Exception as exc:
        # ``run_aws`` raises for a refused chokepoint call and for a sandbox that
        # cannot be built. Either way the account is unknown, which the caller
        # treats as fail-closed.
        logger.info("identity probe could not run: %s", exc)
        identity = Identity(ok=False, detail="The AWS account could not be resolved.")
        _probe_cache[key] = (now, identity)
        return identity

    if rc != 0:
        identity = Identity(ok=False, detail=_redacted(err) or "Credentials did not resolve.")
    else:
        try:
            parsed = json.loads(out or "{}")
        except json.JSONDecodeError:
            parsed = {}
        account = str(parsed.get("Account", "")) if isinstance(parsed, dict) else ""
        arn = str(parsed.get("Arn", "")) if isinstance(parsed, dict) else ""
        if not account:
            identity = Identity(ok=False, detail="The AWS CLI returned no account id.")
        else:
            identity = Identity(ok=True, account=account, arn=arn)

    _probe_cache[key] = (now, identity)
    return identity


def _aws_cli_resolvable() -> bool:
    """Thread-side probe: is the ``aws`` CLI invocable from where we spawn?

    Routes through the deploy engine's shared well-known-dirs resolver
    so a GUI-launched gateway's minimal PATH does not fail the consent gate
    closed before the voice sites' own resolved spawns ever run — the spawn
    below already resolves absolutely via ``cloud.aws.run_aws``, so the probe
    must agree with it. Imported at call time for the same reason as
    ``_run_aws``.
    """
    from kiro_crew.deploy.engine import resolve_aws_bin

    return shutil.which(resolve_aws_bin()) is not None


def _run_aws(args: list[str], profile: str, region: str) -> tuple[int, str, str]:
    """Thread-side import of the cloud chokepoint.

    Imported at call time, not module scope: ``kiro_crew.cloud`` is an optional
    provisioning subsystem, and the voice/STT paths that import THIS module for
    the local gate must not pay for it.
    """
    from kiro_crew.cloud.aws import run_aws

    return run_aws(args, profile, region, timeout=15)


def reconcile_drift(
    service: str,
    identity: Identity,
    *,
    probed_profile: str | None = None,
    probed_region: str | None = None,
    expected_grant_id: str | None = None,
) -> bool:
    """Revoke the grant when the live account is not the confirmed one.

    Returns True when a grant was revoked. Called from the confirmation
    surfaces, which are the only places an identity is probed -- so this is
    where the profile-repointed-at-a-new-account case is caught. A failed probe
    is NOT drift (it proves nothing about the account), so it leaves the grant
    alone.

    ``probed_profile``/``probed_region`` name the target the identity was
    probed FROM. When given, the comparison only proceeds if the stored grant
    is for that same target -- checked HERE, against the same read whose grant
    would be revoked, so a grant recorded concurrently for a different target
    cannot be judged by another target's identity (a caller-side pre-check
    reads the grant once and reconcile reads it again; the gap between those
    two reads is exactly where a concurrent POST landed).
    """
    if not identity.ok or not identity.account:
        return False
    grant = read_grant(service)
    if grant is None or not grant.account or grant.account == identity.account:
        return False
    if expected_grant_id is not None and grant.grant_id != expected_grant_id:
        # The stored grant is not the one the caller captured before probing:
        # a replacement (even for the SAME target) was recorded while the
        # probe ran, and this identity is evidence about its predecessor.
        return False
    if probed_profile is not None and (
        grant.profile != probed_profile or grant.region != probed_region
    ):
        # The stored grant is not for the target this identity came from:
        # the identity proves nothing about it. Not drift.
        return False
    logger.warning(
        "AWS consent for %s revoked: it was confirmed for account %s but %s now resolves to a "
        "different account. The operator will be asked again.",
        service,
        grant.account,
        credential_source(grant.profile),
    )
    # Compare-and-delete: only the exact grant this drift evidence is about.
    # A grant recorded between the read above and this call occupies the same
    # store key but is a DIFFERENT authorization; key-wise revoke would
    # delete it on stale evidence.
    return revoke_if_matches(
        service,
        profile=grant.profile,
        region=grant.region,
        account=grant.account,
        arn=grant.arn,
        granted_at=grant.granted_at,
        grant_id=grant.grant_id,
    )


def _redacted(raw: str) -> str:
    """First line of CLI stderr, credential-redacted and length-capped.

    ``run_aws`` already decodes, so this takes text rather than bytes.
    """
    if not raw:
        return ""
    # Imported here rather than at module scope: ``security`` is a heavy module
    # and this module is imported by the voice/STT call paths purely for the
    # local gate, which needs none of it.
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    lines = raw.strip().splitlines()
    first = lines[0] if lines else ""
    first, _ = redact_credentials(first)
    first, _ = redact_exfiltration_urls(first)
    return first[:300]
