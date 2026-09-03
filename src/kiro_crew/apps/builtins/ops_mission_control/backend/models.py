"""Ops Mission Control — data model.

Three types carry the whole app:

``Signal``
    A normalized work item. Every provider maps its native object (a CloudWatch
    alarm, a PagerDuty incident, a Datadog monitor, a webhook body) onto this one
    shape — which is what lets the board, the dispatch heartbeat, and the
    knowledge ledger stay provider-agnostic.

``Incident``
    A claimed ``Signal`` being worked, with its status, the chat slot backing the
    investigation, and the ledger entries it matched.

``LedgerEntry``
    One learned pattern: what broke, what fixed it, how much we trust that. The
    compounding-knowledge mechanism — the reason the second occurrence of a
    failure is cheaper than the first.

See ``docs/system-specs/modules/ops-mission-control.md`` (data model).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Constants — no hardcoded strings/values in business logic (AGENTS.md)
# ---------------------------------------------------------------------------

SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"
VALID_SEVERITIES: frozenset[str] = frozenset({SEVERITY_CRITICAL, SEVERITY_WARNING, SEVERITY_INFO})

STATE_FIRING = "firing"
STATE_OK = "ok"
STATE_UNKNOWN = "unknown"
#: A human already parked this AT THE PROVIDER — an Alertmanager silence or inhibition,
#: a Zabbix maintenance window, an Icinga downtime, a Sentry archive.
#:
#: This is a READ, and it is deliberately not ``ACTION_SILENCE`` below (the verb the app
#: ISSUES). Conflating the two would put the app's own outbound intent and somebody
#: else's decision into one word, and only one of them is a fact about the world.
#:
#: What the absence of this word cost: an adapter facing Alertmanager's
#: ``status.state = "suppressed"`` had exactly two options and both were wrong. Report
#: ``firing`` and the app investigates something an operator explicitly parked — the
#: fastest way there is to lose an operator's trust in an autonomous responder. Or drop
#: the signal, and "the app ignored my alarm" becomes indistinguishable from "someone
#: silenced it". Both were reached by *every* new adapter privately, because there was
#: no shared place to put the answer.
#:
#: Making it a STATE rather than a label is what keeps the claim rule single: dispatch
#: claims ``state == firing`` in one place, so a suppressed signal is unclaimable by
#: construction with no second predicate anyone can forget. A label would leave
#: ``state == firing`` and require ``run_cycle`` to grow a second, label-reading
#: condition — moving the reimplement-the-filter-privately failure into core. It also
#: keeps ``unknown`` honest: ``unknown`` means "we could not read the state",
#: ``suppressed`` means "we read it and a human parked it".
STATE_SUPPRESSED = "suppressed"
VALID_STATES: frozenset[str] = frozenset({STATE_FIRING, STATE_OK, STATE_UNKNOWN, STATE_SUPPRESSED})


class CorruptDocumentError(json.JSONDecodeError):
    """A stored document that PARSED but is not usable as a mutation base.

    Raised by the ``*_for_update`` readers when a document is valid JSON yet structurally
    wrong -- a root that is not an object, or a row that is not one. Those cases destroy
    data exactly like a parse failure does if the reader normalizes them away, because the
    mutation rewrites the whole file from whatever the reader returned.

    Subclasses :class:`json.JSONDecodeError` DELIBERATELY, and that is load-bearing rather
    than convenient: every caller's corruption clause is written against
    ``json.JSONDecodeError``, so this routes correctly through all of them with no change,
    while a fresh exception type would be caught by none and would silently reopen the very
    data loss those clauses exist to stop. Note that both are ``ValueError`` subclasses, so
    a caller with an unrelated ``except ValueError`` will still claim this unless its
    corruption arm comes first.

    The subclass exists so the raises are greppable and their intent explicit instead of a
    parser exception carrying a meaning the parser never assigned it. Suggested in review
    (Design Review) and worth having before #7805 replicates this idiom across the four
    merged siblings.
    """


class UnknownFieldError(CorruptDocumentError):
    """A stored document holding a field THIS build does not know about.

    A newer build added a field and wrote it; this reader's ``to_dict`` is ``asdict()`` over
    the fields it knows, so the key would vanish on write.

    **The reachable path today is a version ROLLBACK on one instance, not two instances sharing
    a file.** An earlier version of this docstring justified it by ledger sync, which was wrong:
    ``ledger_sync`` says "Only the ledger. NOT the dispatch index" and explains why -- the
    index is last-writer-wins on a shared key, so syncing it would let two instances each
    believe they own an incident. Caught in review (First Principles). What remains, and is
    ordinary, is a bad release rolled back on a single machine: the file on disk was written
    by the newer build, and the older build now reads it.

    The other way to reach it is a future SCHEMA MIGRATION that retires a key, since an old
    record then carries a field the new ``to_dict`` does not emit. That is indistinguishable
    from the rollback case at the point of detection, which is why neither this class nor the
    message it carries asserts which direction the skew runs.

    The mutation must still refuse -- writing would strip the newer build's data, which is the
    same loss every other refusal here prevents. What differs is the REMEDY: the file is fine
    and the reader is behind, so the operator needs to move this instance forward, not repair a
    document. Reporting it as corruption sends them to fix something that is not broken.

    Subclasses :class:`CorruptDocumentError` so every caller that already refuses corruption
    refuses this too with no change -- the distinction only has to be visible where an
    operator reads it, which is the route layer's error code. Raised on data that is merely
    NEWER; genuine content loss stays a plain ``CorruptDocumentError``.
    """


STATUS_UNCLAIMED = "unclaimed"
STATUS_DISPATCHED = "dispatched"
STATUS_INVESTIGATING = "investigating"
STATUS_NEEDS_HUMAN = "needs_human"
STATUS_RESOLVED = "resolved"
STATUS_ESCALATED = "escalated"
STATUS_STALE = "stale"

Status = Literal[
    "unclaimed",
    "dispatched",
    "investigating",
    "needs_human",
    "resolved",
    "escalated",
    "stale",
]

#: Legal status transitions. Enforced by ``store.transition`` — an incident can
#: never jump straight from ``unclaimed`` to ``resolved``, which would leave the
#: board asserting work was done that no investigation ever ran.
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_UNCLAIMED: frozenset({STATUS_DISPATCHED}),
    # ``dispatched -> needs_human`` is reachable because an investigating agent can
    # block on a tool approval BEFORE it finishes its first turn — observed live:
    # the agent's opening move was a read-only AWS probe, which parked on a
    # ``permission`` message while the incident was still ``dispatched``. Without
    # this edge the board reports a blocked incident as progressing, which is the
    # one thing an ops board must never do.
    # ``dispatched -> resolved`` is reachable because a signal can clear in the gap
    # between being claimed and the investigating agent's first turn — a flapping
    # alarm, or a GitHub issue someone closes a minute later. The reconcile SOP's
    # whole job is to resolve incidents whose signal stopped firing, and without
    # this edge it has NO legal move for that case: the incident sticks at
    # ``dispatched`` until the stale sweep hours later, so the board claims work is
    # in progress on a problem that no longer exists. Found by exercising the
    # reconcile SOP against a real cleared GitHub signal.
    STATUS_DISPATCHED: frozenset(
        {STATUS_INVESTIGATING, STATUS_NEEDS_HUMAN, STATUS_RESOLVED, STATUS_STALE}
    ),
    STATUS_INVESTIGATING: frozenset(
        {STATUS_NEEDS_HUMAN, STATUS_RESOLVED, STATUS_ESCALATED, STATUS_STALE}
    ),
    # ``needs_human -> stale`` too: an incident nobody ever answers must not pin a
    # signal as claimed forever, or the alarm silently stops being worked.
    STATUS_NEEDS_HUMAN: frozenset(
        {STATUS_INVESTIGATING, STATUS_RESOLVED, STATUS_ESCALATED, STATUS_STALE}
    ),
    # ``stale -> resolved`` for the same reason: a released incident whose signal has
    # since cleared must be closable. With only ``-> dispatched`` available,
    # reconcile's only move would be to hand a dead signal back to an agent, which
    # spends a whole investigation to conclude nothing is wrong.
    STATUS_STALE: frozenset({STATUS_DISPATCHED, STATUS_RESOLVED}),
    # Terminal states. Re-opening is a new signal, not a transition — a resolved
    # incident that "comes back" is a fresh firing with its own timeline.
    STATUS_RESOLVED: frozenset(),
    STATUS_ESCALATED: frozenset(),
}

#: Closed for good — no legal transition leads out. DERIVED from the grammar above
#: rather than hand-listed, so a future status with no outgoing edges is terminal
#: automatically and one that gains an edge stops being terminal, with no second list
#: to forget to update.
#:
#: ``store.claim`` uses this to let a CLOSED incident's signal be claimed AGAIN, as a
#: new incident. ``signal.id`` is stable for the alarm's lifetime, so without this the
#: app permanently stopped responding to any failure it had already handled once — and
#: the compounding-memory fast path, which can only pay off on a second occurrence,
#: was unreachable in production.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    status for status, onward in LEGAL_TRANSITIONS.items() if not onward
)

#: Why an incident is currently waiting on a person. Surfaced so the board can say
#: "waiting for you to approve a command" rather than the ambiguous "needs human",
#: which reads the same whether the agent wants a decision or has given up.
BLOCKED_ON_APPROVAL = "awaiting_approval"
BLOCKED_ON_INPUT = "awaiting_input"
BLOCKED_ON_DIAGNOSIS = "awaiting_diagnosis"

#: Statuses that count as open work for board counts and the stale sweep.
OPEN_STATUSES: frozenset[str] = frozenset(
    {STATUS_UNCLAIMED, STATUS_DISPATCHED, STATUS_INVESTIGATING, STATUS_NEEDS_HUMAN}
)

#: How an incident came to be claimed. Two values because there are two paths into
#: ``store.claim`` and the board could not tell them apart after the fact.
#:
#: Coarse on purpose — the PATH, never a username. A person's name here would be a
#: second, weaker answer to "who owns this" alongside the on-call schedule, and would
#: put an identity into the one file that syncs to teammates.
CLAIMED_BY_HEARTBEAT = "heartbeat"
CLAIMED_BY_OPERATOR = "operator"
VALID_CLAIMANTS: frozenset[str] = frozenset({CLAIMED_BY_HEARTBEAT, CLAIMED_BY_OPERATOR})

MODE_OBSERVE = "observe"
MODE_PROPOSE = "propose"
MODE_ACT = "act"

#: Autonomy ordering. ``observe`` < ``propose`` < ``act``; the effective mode for
#: an incident is the MINIMUM of the app default and any matching rule
#: (tightest-wins), mirroring the governance ``effective = POLICY ∩ PROFILE``
#: algebra. Default is ``observe``: a stranger's first install must not be able to
#: write to their production tracker.
MODE_ORDER: dict[str, int] = {MODE_OBSERVE: 0, MODE_PROPOSE: 1, MODE_ACT: 2}

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"
#: Confidence decays along this chain when an entry goes unused (ledger hygiene).
CONFIDENCE_DECAY: dict[str, str] = {
    CONFIDENCE_HIGH: CONFIDENCE_MEDIUM,
    CONFIDENCE_MEDIUM: CONFIDENCE_LOW,
    CONFIDENCE_LOW: CONFIDENCE_LOW,
}

TRUST_VERIFIED = "verified"
TRUST_OBSERVED = "observed"

ACTION_ACK = "ack"
ACTION_RESOLVE = "resolve"
ACTION_COMMENT = "comment"
#: Time-boxed suppression: silence this alert for a bounded window, then let it come
#: back on its own.
#:
#: This is the safest write-back verb the provider landscape offers, and the vocabulary
#: had no word for it. Every low-risk provider write is really this — an Alertmanager
#: silence with a mandatory ``endsAt``, a Datadog mute with an ``end``, an Icinga ack
#: with an ``expiry``, a Sentry archive with an ``ignoreDuration`` — so adapters were
#: forced to express a mute as ``resolve``, which asserts something false about the
#: world and hides a live fault permanently rather than temporarily.
#:
#: The property that matters for autonomy: a WRONG silence expires by itself. That makes
#: "let the agent act" a bounded bet instead of an all-or-nothing one, which is what
#: earning autonomy per-rule actually requires.
ACTION_SILENCE = "silence"
VALID_ACTIONS: frozenset[str] = frozenset(
    {ACTION_ACK, ACTION_RESOLVE, ACTION_COMMENT, ACTION_SILENCE}
)

#: Actions that MUST carry a positive, bounded expiry. Enforced at the authorization
#: boundary rather than left to each adapter: an unbounded suppression is exactly the
#: outcome this verb exists to prevent, so a sink must not be able to opt out of the
#: bound by forgetting to check.
EXPIRING_ACTIONS: frozenset[str] = frozenset({ACTION_SILENCE})

#: Actions whose success is OBSERVABLE by re-reading the signal's firing state, and
#: therefore the only ones this app is able to verify.
#:
#: The distinction is the whole reason post-action verification is honest rather than
#: decorative. ``resolve`` and ``silence`` both assert something about the firing
#: condition, so a later poll is real evidence about whether they landed. ``ack`` and
#: ``comment`` do not: an acknowledged alert keeps firing by design (see
#: ``normalize_state``, where ``acknowledged`` maps onto ``firing`` on purpose), so
#: "still firing after an ack" is the EXPECTED reading and says nothing at all about
#: whether the ack was applied.
#:
#: So an ack is left explicitly unverified rather than verified against the wrong
#: evidence. That is a gap this app admits: Checkmk dispatches commands asynchronously
#: through Livestatus and documents that a 2xx "only indicates whether the request was
#: successfully transmitted, NOT whether it was in fact successfully executed", and no
#: adapter here reports acknowledgement state back. Claiming a verdict from firing state
#: would turn an unverifiable write into a confident one, which is worse than saying so.
VERIFIABLE_ACTIONS: frozenset[str] = frozenset({ACTION_RESOLVE, ACTION_SILENCE})

#: Post-action verification verdicts, persisted on the incident.
#:
#: ``""`` (the default) means no action was ever executed — NOT "verified fine". Every
#: incident written before this existed reads as that, which is correct.
VERIFY_PENDING = "pending"
#: The recheck ran against a SUCCESSFUL poll and the signal is no longer firing.
VERIFY_CLEARED = "cleared"
#: The recheck ran against a successful poll and the signal is STILL firing — the 2xx
#: did not mean what the board reported it meant.
VERIFY_STILL_FIRING = "still_firing"
#: The recheck was due and we could not look: the source's last poll failed, timed out,
#: or is in backoff. Deliberately NOT terminal — a later cycle where the source answers
#: replaces it — because "we could not look" is a statement about us, not about the
#: world, and freezing it would be the absence-is-evidence bug in a new place.
VERIFY_UNKNOWN = "unknown"
#: The action was executed but its success is not observable here (see
#: ``VERIFIABLE_ACTIONS``). Recorded explicitly so the board can say "nothing checked
#: this" instead of leaving a blank that reads as success.
VERIFY_NOT_CHECKABLE = "not_checkable"
VALID_VERIFICATIONS: frozenset[str] = frozenset(
    {
        VERIFY_PENDING,
        VERIFY_CLEARED,
        VERIFY_STILL_FIRING,
        VERIFY_UNKNOWN,
        VERIFY_NOT_CHECKABLE,
    }
)

#: Verdicts that still owe a recheck. ``unknown`` is in here for the reason stated on
#: it: the recheck did not happen, so the debt is not paid.
OPEN_VERIFICATIONS: frozenset[str] = frozenset({VERIFY_PENDING, VERIFY_UNKNOWN})

#: How long after a non-expiring action to re-read the signal. Five minutes: long
#: enough for a provider to propagate a state change (CloudWatch evaluates on a
#: period, PagerDuty and Datadog are eventually consistent through their own queues),
#: short enough that the answer arrives inside the shift that took the action.
#:
#: A ``silence`` ignores this and schedules its recheck at the END of its own window
#: instead — that is the schedule ``ACTION_SILENCE``'s mandatory expiry buys, and it is
#: the more interesting moment: a suppression that expires straight back into the same
#: firing condition is evidence nothing was fixed.
DEFAULT_VERIFY_AFTER_SECS = 5 * 60

#: Default and ceiling for a suppression window, in seconds. The ceiling is the real
#: guard: a caller asking to silence something for a week is asking to forget it.
DEFAULT_SILENCE_SECS = 4 * 60 * 60
MAX_SILENCE_SECS = 24 * 60 * 60

# ---------------------------------------------------------------------------
# Proposals — what ``propose`` mode actually does
# ---------------------------------------------------------------------------
#
# Before this, ``propose`` was behaviourally identical to ``observe``:
# ``authorize_action`` refuses anything below ``act``, ``proposed_action`` was declared
# and never assigned, and there was no store, no approve endpoint and no timeout. So the
# mode most operators will live in — "tell me what you would do" — was prose in a chat
# transcript, with nothing to approve and nothing recording that a decision was pending.
#
# **The drafted text IS the contract.** An approval approves the exact bytes the agent
# showed; if the draft changed in between, the approval is void and must be re-asked.
# Without that the operator is approving an intention rather than an action, which is the
# one thing a propose gate exists to prevent.

PROPOSAL_PENDING = "pending"
PROPOSAL_APPROVED = "approved"
PROPOSAL_REJECTED = "rejected"
#: Nobody answered inside the window. NOT auto-approved and NOT auto-rejected: silence is
#: not consent, and treating it as refusal would quietly drop work the operator may still
#: want. It stops asking and says so.
PROPOSAL_EXPIRED = "expired"

#: How long a proposal waits before it stops asking. The source bumps once at 24h and
#: never auto-acts; this keeps the window and the never-auto-act half, and leaves the bump
#: to the notification bus rather than inventing a second reminder channel.
DEFAULT_PROPOSAL_TTL_SECS = 24 * 60 * 60

#: Cap on a stored draft. A proposal is a verb, a target and a sentence — anything larger
#: is a transcript, and storing one would put unbounded model output in the index that
#: every board read parses.
MAX_PROPOSAL_TEXT_CHARS = 4000


def resolve_silence_secs(raw: Any) -> int:
    """Clamp a requested suppression window into ``(0, MAX_SILENCE_SECS]``.

    Unparseable or non-positive input yields the DEFAULT, never "no expiry" — the one
    reading that would reintroduce the indefinite mute this verb replaces.
    """
    try:
        requested = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_SILENCE_SECS
    if requested <= 0:
        return DEFAULT_SILENCE_SECS
    return min(requested, MAX_SILENCE_SECS)


#: Length of the hex digest kept for fingerprints and ledger entry ids. 16 hex
#: chars = 64 bits, ample against accidental collision in a per-user ledger while
#: staying short enough to read in a log line.
_DIGEST_LEN = 16

#: Current ``ledger.jsonl`` record format. A line with no ``v`` predates the field and IS
#: version 1 — see ``LedgerEntry.v``. Bump this only for a change a reader must know about
#: (a field whose MEANING changed, or one it cannot safely default), never for an added
#: optional field: every field on this record already defaults, which is what lets one
#: version cover the whole history so far.
LEDGER_RECORD_V1 = 1

#: Substrings replaced when building a fingerprint, so a recurrence of the same
#: failure on a different host/instance/date matches its ancestor.
_VOLATILE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # ISO-8601-ish timestamps
    re.compile(r"\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}(:\d{2})?(\.\d+)?z?", re.IGNORECASE),
    # bare dates and clock times
    re.compile(r"\d{4}-\d{2}-\d{2}"),
    re.compile(r"\b\d{2}:\d{2}(:\d{2})?\b"),
    # uuids
    re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        re.IGNORECASE,
    ),
    # long hex runs (request ids, digests) and i-/vol- style resource suffixes
    re.compile(r"\b[0-9a-f]{12,}\b", re.IGNORECASE),
    re.compile(r"\b(i|vol|eni|snap|ami)-[0-9a-f]{8,}\b", re.IGNORECASE),
    # bare numbers (counts, thresholds, ports) — a DLQ at 500 and at 900 is the
    # same pattern
    re.compile(r"\d+"),
)


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string with a ``Z`` suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_severity(raw: str) -> str:
    """Map a provider's severity vocabulary onto ours.

    Unknown values fall back to ``warning`` rather than ``critical``: a provider
    we do not understand should not be able to manufacture top-priority work, and
    should not be silently demoted to ``info`` either.

    **The separator is canonicalized before lookup, not enumerated in the tables.**
    The tables listed ``sev1`` and ``sev-1`` but not ``sev_1``, ``sev 1`` or
    ``sev.1`` — so an underscore-separated vocabulary had EVERY level floored to
    ``warning``, and a genuine SEV_1 landed on the board looking like a warning.
    The conservative unknown-value fallback above is what disguised it: nothing
    raised, nothing logged, and the wrong answer was a plausible one.

    Canonicalizing is the fix rather than adding three more spellings per row,
    because the next vocabulary will pick a fourth separator and the table would
    silently under-match again. Only separators are folded — the mapping itself
    stays the single authority on what a level means.
    """
    v = (raw or "").strip().lower()
    if v in VALID_SEVERITIES:
        return v
    # Drop separators entirely rather than normalizing to one of them: the tables then
    # need a single spelling per level, and `sev1` / `sev-1` / `sev_1` / `sev 1` /
    # `sev.1` all reduce to it. Normalizing TO `-` instead was the obvious first move
    # and it broke bare `sev1`, which had worked before — caught by testing the cases
    # that already passed, not just the ones being fixed.
    v = re.sub(r"[\s_.\-]+", "", v)
    if v in {"p1", "sev1", "high", "error", "alarm", "urgent", "fatal"}:
        return SEVERITY_CRITICAL
    if v in {"p2", "p3", "sev2", "warn", "medium", "degraded"}:
        return SEVERITY_WARNING
    if v in {"p4", "p5", "sev3", "low", "ok", "nominal", "debug"}:
        return SEVERITY_INFO
    return SEVERITY_WARNING


def normalize_state(raw: str) -> str:
    """Map a provider's state vocabulary onto ``firing`` / ``ok`` / ``suppressed`` / ``unknown``.

    Unknown values become ``unknown``, NOT ``firing`` — an unparseable state must
    not create phantom work on the board.

    The suppression vocabulary is checked because every provider that publishes one uses
    a different word for it, and before this they ALL landed in ``unknown``: verified,
    ``suppressed``/``silenced``/``inhibited``/``in downtime`` returned exactly what
    ``banana`` returned, so "a human parked this" was stored as "we could not parse it".
    """
    v = (raw or "").strip().lower()
    if v in VALID_STATES:
        return v
    # NOTE: `acknowledged` is deliberately NOT here — it stays in the firing set below.
    # An acknowledged page is still unresolved and the whole point is to be working it
    # (see providers/pagerduty.py `_OPEN_STATUSES`). Do not "fix" this by moving it.
    if v in {"suppressed", "silenced", "inhibited", "muted", "snoozed", "downtime", "in downtime"}:
        return STATE_SUPPRESSED
    # `active` and `unprocessed` are the OTHER two values of Alertmanager's v2
    # `alertStatus.state` enum, whose third is `suppressed`. Added in the same change that
    # taught the webhook to read that object: admitting the parked case while leaving its
    # two siblings falling to `unknown` would mean the v2 shape parses a silenced alert and
    # drops a LIVE one — a worse failure than not reading the object at all. `unprocessed`
    # means Alertmanager has the alert but has not fanned it out yet; it is live either way.
    if v in {
        "alarm",
        "alert",
        "triggered",
        "open",
        "firing",
        "acknowledged",
        "warn",
        "active",
        "unprocessed",
    }:
        return STATE_FIRING
    if v in {"ok", "resolved", "closed", "cleared", "nominal"}:
        return STATE_OK
    return STATE_UNKNOWN


def proposal_digest(action: str, sink: str, note: str, duration_secs: Any = None) -> str:
    """Content hash of a proposal's EXACT terms — the thing an approval binds to.

    This is what turns "the drafted text is the contract" from a convention into a
    mechanism. An approval carries the digest it saw; if the stored draft has changed
    since, the digests differ and the approval is refused rather than executed. Without
    it, nothing stops the text being altered between the operator reading it and the
    action firing, which is the whole failure a propose gate exists to prevent.

    Every field that changes what actually HAPPENS is in the hash: the verb, the target
    sink, the note that gets posted verbatim, and the suppression window (a 1-hour mute
    and a 24-hour mute are different actions). Nothing else is — not the incident id, not
    a timestamp — so re-drafting identical terms yields the same digest and an operator
    who approves twice is not fighting a spurious mismatch.
    """
    basis = "|".join(
        [action.strip().lower(), sink.strip().lower(), note.strip(), str(duration_secs or "")]
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def compute_fingerprint(source: str, resource: str, title: str) -> str:
    """Stable identity for *the kind of failure this is*.

    Deliberately excludes timestamps, uuids, instance ids, and bare numbers (see
    ``_VOLATILE_PATTERNS``) so the same failure recurring tomorrow on a different
    host produces the SAME fingerprint and therefore matches its ledger ancestor.
    That matching is the entire compounding-knowledge mechanism; a fingerprint
    that drifts per occurrence would make the ledger useless.
    """
    shape = f"{title or ''} {resource or ''}".strip().lower()
    for pattern in _VOLATILE_PATTERNS:
        shape = pattern.sub("#", shape)
    shape = re.sub(r"[^a-z0-9#]+", " ", shape).strip()
    basis = f"{(source or '').strip().lower()}|{shape}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:_DIGEST_LEN]


@dataclass(frozen=True)
class Signal:
    """A normalized work item from any provider."""

    id: str
    source: str
    title: str
    severity: str = SEVERITY_WARNING
    state: str = STATE_FIRING
    fired_at: str = ""
    resource: str = ""
    url: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    fingerprint: str = ""
    #: The PROVIDER's own stable identity for this failure, when it publishes one —
    #: an Alertmanager ``fingerprint``, a Sentry issue id, a Zabbix trigger
    #: ``objectid``. Empty when the provider offers nothing trustworthy.
    #:
    #: Exists because ``fingerprint`` above is a heuristic over rendered text and
    #: provably over-merges: with every bare digit stripped (see
    #: ``_VOLATILE_PATTERNS``), "4xx error rate above 5" and "5xx error rate above 1"
    #: on one resource hash identically, as do a p99 and a p50 latency alarm. The
    #: ledger then hands a responder a fix learned from a DIFFERENT failure, which is
    #: worse than no match. This field carries the identity the provider already
    #: computed so an exact match can be preferred over a shape match.
    #:
    #: Set from explicit adapter input, never derived — a derived value would be
    #: another heuristic wearing the word "exact".
    provider_key: str = ""
    #: WHO parked this, in the provider's own words — an Alertmanager silence id from
    #: ``silencedBy``, the alert named in ``inhibitedBy``, a Zabbix maintenance name.
    #:
    #: Exists because ``state == suppressed`` alone answers the wrong half of the
    #: operator's question. "Something silenced this" still leaves them hunting; the
    #: attribution is what turns it into one click at the provider. Empty whenever the
    #: provider publishes no attribution, and the UI must say so explicitly rather than
    #: imply we know — an invented owner is worse than a blank.
    suppressed_by: str = ""
    #: WHICH KIND of suppression, machine-readable: ``silenced`` (a person created a
    #: silence) or ``inhibited`` (another, higher-ranked alert is masking this one).
    #:
    #: Kept separate from ``suppressed_by`` because the operator's next move differs: a
    #: silence is a decision to review or expire, while an inhibition means go look at
    #: the alert doing the inhibiting — this one is a symptom.
    #:
    #: Like ``provider_key``, both fields are explicit adapter input and never derived.
    suppressed_reason: str = ""

    @classmethod
    def create(
        cls,
        *,
        source: str,
        native_id: str,
        title: str,
        severity: str = SEVERITY_WARNING,
        state: str = STATE_FIRING,
        fired_at: str = "",
        resource: str = "",
        url: str = "",
        labels: dict[str, str] | None = None,
        provider_key: str = "",
        suppressed_by: str = "",
        suppressed_reason: str = "",
    ) -> Signal:
        """Build a Signal with normalization and fingerprinting applied.

        Adapters should always go through this rather than the raw constructor,
        so severity/state vocabularies and fingerprints stay consistent across
        providers — including companion-contributed ones.
        """
        return cls(
            id=f"{source}:{native_id}",
            source=source,
            title=title,
            severity=normalize_severity(severity),
            state=normalize_state(state),
            fired_at=fired_at or utc_now_iso(),
            resource=resource,
            url=url,
            labels=dict(labels or {}),
            fingerprint=compute_fingerprint(source, resource, title),
            # Namespaced by source so two providers cannot collide on a bare numeric
            # id — Sentry issue 12345 and a Zabbix trigger 12345 are unrelated.
            provider_key=f"{source}:{provider_key}" if provider_key else "",
            # NOT namespaced by source, unlike provider_key: this is display text for a
            # human, not a match key, so prefixing it would only make the board read
            # "webhook:silence-abc" where the operator wants the silence id they can
            # paste into Alertmanager.
            suppressed_by=suppressed_by,
            suppressed_reason=suppressed_reason,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Signal:
        labels = data.get("labels")
        return cls(
            id=str(data.get("id", "")),
            source=str(data.get("source", "")),
            title=str(data.get("title", "")),
            severity=normalize_severity(str(data.get("severity", ""))),
            state=normalize_state(str(data.get("state", ""))),
            fired_at=str(data.get("fired_at", "")),
            resource=str(data.get("resource", "")),
            url=str(data.get("url", "")),
            labels={str(k): str(v) for k, v in labels.items()} if isinstance(labels, dict) else {},
            fingerprint=str(data.get("fingerprint", "")),
            # Absent on every incident written before this field existed; empty is the
            # correct reading of "this provider gave us no exact identity".
            provider_key=str(data.get("provider_key", "")),
            # Same rule: absent on every incident written before these existed, and empty
            # is the correct reading of "this provider published no attribution".
            suppressed_by=str(data.get("suppressed_by", "")),
            suppressed_reason=str(data.get("suppressed_reason", "")),
        )


@dataclass
class Incident:
    """A claimed Signal being worked."""

    incident_id: str
    signal: Signal
    status: str = STATUS_UNCLAIMED
    operating_mode: str = MODE_OBSERVE
    claimed_at: str = ""
    #: WHAT claimed this — the dispatch heartbeat, or a person on the board.
    #:
    #: ``claimed_at`` records when; nothing recorded by what, so the two paths into
    #: ``store.claim`` were indistinguishable after the fact. That matters for the
    #: question an operator actually asks of a surprising incident: did the agent decide
    #: to pick this up, or did I? Recording the claiming PATH is the smallest answer that
    #: distinguishes them.
    #:
    #: Deliberately COARSE — the claiming path, never a username. A per-operator identity
    #: here would be a second, weaker answer to "who owns this" alongside the on-call
    #: schedule, and would put a person's name in the one file that syncs to teammates.
    #: Defaults to "" so every incident already on disk stays valid and reads as
    #: "unrecorded", which is the truth for all of them.
    claimed_by: str = ""
    updated_at: str = ""
    slot_key: str = ""
    slack_thread_ts: str = ""
    ledger_matches: list[str] = field(default_factory=list)
    diagnosis: str = ""
    proposed_action: dict[str, Any] | None = None
    resolution: str = ""
    #: Why this incident is waiting on a person (one of the ``BLOCKED_ON_*``
    #: constants), or "" when it is not blocked. Derived from the investigation
    #: slot rather than stored as intent, so it cannot go stale against reality.
    blocked_reason: str = ""
    # ---- post-action verification (§5.10) --------------------------------------
    # Three fields, all DEFAULT EMPTY so every incident already on disk stays valid and
    # reads as "no action was taken", which is the truth for all of them.
    #
    # They exist because nothing re-read the signal after an action executed, so
    # ``ActionResult.ok`` meant only "the provider returned 2xx". For an async command
    # pipe that is not the same claim: the board reported an applied fix and no code
    # anywhere had looked at whether the alarm stopped firing. That is the silent lie an
    # ops agent must not tell, and it is also what makes ``LedgerEntry.use_count``
    # mean "was shown to somebody" rather than "worked".
    #: The last action this app executed for this incident (an ``ACTION_*`` value), or ""
    #: when none has been.
    last_action: str = ""
    #: When that action was executed, ISO-8601 Z. Stored rather than derived from
    #: ``updated_at`` because ``updated_at`` moves on every unrelated write, and the
    #: recheck schedule has to be anchored to the ACTION.
    last_action_at: str = ""
    #: When the recheck becomes due, ISO-8601 Z. For an expiring action this is the end
    #: of the suppression window; otherwise ``DEFAULT_VERIFY_AFTER_SECS`` later.
    verify_after: str = ""
    #: The verdict (a ``VERIFY_*`` value), or "" when no action was ever executed.
    verification: str = ""
    #: One sentence naming what was observed, in the recheck's own words — including
    #: WHICH source could not be read when the verdict is ``unknown``. A bare enum sends
    #: an operator hunting for the reason we already had.
    verification_detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["signal"] = self.signal.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Incident:
        raw_signal = data.get("signal")
        matches = data.get("ledger_matches")
        proposed = data.get("proposed_action")
        return cls(
            incident_id=str(data.get("incident_id", "")),
            signal=Signal.from_dict(raw_signal if isinstance(raw_signal, dict) else {}),
            status=str(data.get("status", STATUS_UNCLAIMED)),
            operating_mode=str(data.get("operating_mode", MODE_OBSERVE)),
            claimed_at=str(data.get("claimed_at", "")),
            claimed_by=str(data.get("claimed_by", "")),
            updated_at=str(data.get("updated_at", "")),
            slot_key=str(data.get("slot_key", "")),
            slack_thread_ts=str(data.get("slack_thread_ts", "")),
            ledger_matches=[str(m) for m in matches] if isinstance(matches, list) else [],
            diagnosis=str(data.get("diagnosis", "")),
            proposed_action=proposed if isinstance(proposed, dict) else None,
            resolution=str(data.get("resolution", "")),
            blocked_reason=str(data.get("blocked_reason", "")),
            # Absent on every incident written before verification existed. Empty means
            # "no action was taken", which is true of all of them — deliberately not
            # back-filled to a verdict, because inventing one is the defect this fixes.
            last_action=str(data.get("last_action", "")),
            last_action_at=str(data.get("last_action_at", "")),
            verify_after=str(data.get("verify_after", "")),
            verification=str(data.get("verification", "")),
            verification_detail=str(data.get("verification_detail", "")),
        )

    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES


@dataclass
class LedgerEntry:
    """One learned failure pattern and its fix."""

    entry_id: str
    pattern: str
    fix: str
    #: Record format version. Written on every new line; ABSENT on every line already on
    #: disk, which reads as ``LEDGER_RECORD_V1`` — the standard retrofit for an optional
    #: field in append-only JSONL, and the reason adding this now costs nothing.
    #:
    #: Why it earns a field at all: ``ledger.jsonl`` is the one artifact that LEAVES the
    #: machine. ``ledger_sync`` git-pushes it and teammates on different Kiro Crew builds
    #: pull it, so an older instance can be handed a row a newer one wrote. Without a
    #: version there is no way to notice: the reader coerces what it recognises, defaults
    #: what it does not, and silently treats a row it only partly understands as fully
    #: understood. Review named this the nearest thing in the app to a one-way door.
    #:
    #: Deliberately NOT used to reject anything today — there is exactly one version, so a
    #: gate would be dead code. It exists so the NEXT format change has somewhere to say so.
    #:
    #: The default is the LITERAL 1, not ``LEDGER_RECORD_V1``. A dataclass field default is
    #: evaluated when the class object is built, and ``test_ledger_sync_git`` evicts this
    #: module from ``sys.modules`` mid-test to simulate two instances — so a name looked up
    #: at class-creation time can be resolved against a half-initialised module and raise
    #: ``NameError``. Observed exactly that. The constant stays the single source of truth
    #: for READERS; only this default is spelled out, and a test pins the two equal.
    v: int = 1
    fingerprints: list[str] = field(default_factory=list)
    #: Provider-computed identities this entry has matched (see
    #: ``Signal.provider_key``). Unions on merge exactly as ``fingerprints`` does, so
    #: the git-synced append-only ledger keeps its conflict-free dedupe property.
    #: Defaults empty, so every line written before this field existed stays valid and
    #: keeps matching by fingerprint alone.
    provider_keys: list[str] = field(default_factory=list)
    confidence: str = CONFIDENCE_MEDIUM
    trust: str = TRUST_OBSERVED
    use_count: int = 0
    #: Times this entry was cited for a fix that DID NOT hold — the same failure came
    #: back shortly after an incident closed citing it, or a provider reported a
    #: regression. Defaults to 0, so every line already on disk reads as "never
    #: contradicted", which is what we actually know about it.
    #:
    #: This is the mechanical downward path the ledger did not have. It was NOT
    #: structurally unable to learn a fix failed — an agent can author a corrective
    #: entry sharing the fingerprint and ``find_contradictions`` surfaces the pair — but
    #: that path needs a model turn and a human's judgement, so nothing moved on
    #: evidence alone. Meanwhile ``use_count`` incremented at CLAIM time, before any
    #: outcome existed, so a wrong entry climbed the ranking on every mismatch and
    #: survived the hygiene prune, which sorts by ``-use_count``.
    miss_count: int = 0
    #: When the most recent miss was recorded, ISO-8601 Z. Empty until there is one.
    #: Kept so the hygiene pass and the board can say WHEN a fix stopped working — an
    #: entry that missed once a year ago is a different object from one missing weekly.
    last_miss: str = ""
    #: The ``miss_count`` value at which the hygiene pass last demoted this entry.
    #:
    #: Exists so one piece of evidence costs one step. Hygiene runs nightly and its
    #: demotion test is a RATIO, which stays true once it is true — so without this an
    #: entry that missed once would be walked ``high → medium`` tonight and
    #: ``medium → low`` tomorrow on no new evidence at all, arriving at the bottom of the
    #: scale for a single failure. Demotion therefore requires ``miss_count`` to have
    #: GROWN since the last one.
    #:
    #: Takes the MAX on merge, like ``miss_count`` itself: a teammate whose hygiene pass
    #: already spent that evidence must not have it spent again after a git pull.
    decayed_at_miss_count: int = 0
    first_seen: str = ""
    last_used: str = ""
    source: str = "agent"

    @staticmethod
    def compute_id(pattern: str, fix: str) -> str:
        """Content-addressed id over (pattern, fix).

        Content addressing is what makes the append-only JSONL ledger mergeable
        across git-synced team members without conflict resolution: two people
        who learn the same lesson independently produce the same id, so the merge
        is a dedupe rather than a fight (spec §3.3).
        """
        basis = f"{(pattern or '').strip().lower()}|{(fix or '').strip().lower()}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:_DIGEST_LEN]

    @classmethod
    def create(
        cls,
        *,
        pattern: str,
        fix: str,
        fingerprints: list[str] | None = None,
        provider_keys: list[str] | None = None,
        confidence: str = CONFIDENCE_MEDIUM,
        trust: str = TRUST_OBSERVED,
        source: str = "agent",
    ) -> LedgerEntry:
        now = utc_now_iso()
        return cls(
            entry_id=cls.compute_id(pattern, fix),
            pattern=pattern,
            fix=fix,
            fingerprints=list(fingerprints or []),
            provider_keys=list(provider_keys or []),
            confidence=confidence if confidence in CONFIDENCE_DECAY else CONFIDENCE_MEDIUM,
            trust=trust if trust in {TRUST_VERIFIED, TRUST_OBSERVED} else TRUST_OBSERVED,
            use_count=0,
            first_seen=now,
            last_used=now,
            source=source,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LedgerEntry:
        fps = data.get("fingerprints")
        keys = data.get("provider_keys")
        try:
            use_count = int(data.get("use_count", 0))
        except (TypeError, ValueError):
            use_count = 0
        try:
            # Absent on every line written before demotion existed; 0 is the honest
            # reading. A garbage value reads as 0 too, deliberately — the alternative is
            # a single hand-edited line able to demote an entry the whole team relies on.
            miss_count = int(data.get("miss_count", 0))
        except (TypeError, ValueError):
            miss_count = 0
        try:
            spent = int(data.get("decayed_at_miss_count", 0))
        except (TypeError, ValueError):
            spent = 0
        try:
            # Missing means v1 — every line written before the field existed. A garbage
            # value also reads as v1 rather than raising: this reader's job is to salvage a
            # git-merged team ledger, not to reject it, and a row whose version we cannot
            # parse is still a row we understand every field of.
            version = int(data.get("v", LEDGER_RECORD_V1))
        except (TypeError, ValueError):
            version = LEDGER_RECORD_V1
        return cls(
            entry_id=str(data.get("entry_id", "")),
            pattern=str(data.get("pattern", "")),
            fix=str(data.get("fix", "")),
            v=version,
            fingerprints=[str(f) for f in fps] if isinstance(fps, list) else [],
            provider_keys=[str(k) for k in keys] if isinstance(keys, list) else [],
            confidence=str(data.get("confidence", CONFIDENCE_MEDIUM)),
            trust=str(data.get("trust", TRUST_OBSERVED)),
            use_count=use_count,
            miss_count=miss_count,
            last_miss=str(data.get("last_miss", "")),
            decayed_at_miss_count=spent,
            first_seen=str(data.get("first_seen", "")),
            last_used=str(data.get("last_used", "")),
            source=str(data.get("source", "agent")),
        )


def effective_mode(app_default: str, rule_mode: str | None) -> str:
    """Resolve the operating mode for one incident — tightest-wins.

    ``effective = min(app_default, rule_mode)`` over ``observe < propose < act``.
    A rule can only ever NARROW what the app default already allows, so a
    user-authored rule cannot escalate an instance the operator has pinned to
    ``observe``. With no matching rule the app default applies (spec §5.3).
    """
    base = MODE_ORDER.get(app_default, 0)
    if rule_mode is None:
        level = base
    else:
        level = min(base, MODE_ORDER.get(rule_mode, 0))
    for name, value in MODE_ORDER.items():
        if value == level:
            return name
    return MODE_OBSERVE
