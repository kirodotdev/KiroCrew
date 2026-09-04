"""Inbound webhook signal source — the escape hatch for everything else.

Any system that can POST JSON becomes a signal source: Grafana, Prometheus
Alertmanager, Sentry, a cron job, a custom script. This is what keeps the app from
being limited to the four providers we happened to implement.

Security shape, which is the interesting part:

- Deliveries land on the **authenticated gateway surface**
  (``/api/apps/ops-mission-control/webhook``). We ship no public ingress and no
  tunnel; exposing the gateway is the operator's decision, documented as such.
- Every delivery must carry an HMAC-SHA256 signature over the raw body, keyed by a
  secret held in the keystone store and compared with ``hmac.compare_digest``. No
  secret configured means the endpoint refuses everything — fail-closed, so
  enabling the adapter cannot accidentally open an unauthenticated write path into
  the incident board.
- Accepted deliveries are spooled to a bounded queue that the heartbeat drains, so
  a delivery burst cannot outrun the dispatch loop or grow without limit.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
from collections import deque
from typing import Any

from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    STATE_FIRING,
    Signal,
    normalize_severity,
    normalize_state,
    utc_now_iso,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import provider_enabled
from kiro_crew.apps.builtins.ops_mission_control.backend.secrets import (
    get_secret,
    has_secrets,
)

logger = logging.getLogger(__name__)

PROVIDER_ID = "webhook"

_SECRET_SIGNING_KEY = "signing_secret"
_REQUIRED_SECRETS: tuple[str, ...] = (_SECRET_SIGNING_KEY,)

#: Header carrying the hex HMAC-SHA256 of the raw request body.
SIGNATURE_HEADER = "X-OMC-Signature"

#: Bounded spool. A delivery burst must not grow memory without limit; oldest
#: entries are dropped, which is the right trade because the providers that fan
#: out fastest are also the ones that re-deliver.
MAX_QUEUED_SIGNALS = 200

#: Cap on an accepted body. Anything larger is refused before parsing.
MAX_BODY_BYTES = 256 * 1024

#: Rejection reason for a spool that has no room. Shared with the route so it can answer 503
#: with a `Retry-After` — the sender must learn the delivery did NOT land.
#:
#: The spool is `maxlen`-bounded, so `extend` past capacity silently evicts the OLDEST entries.
#: That turned a burst into data loss the sender could not see: every delivery got HTTP 200
#: while the earliest accepted alerts were dropped before any dispatch cycle claimed them, so
#: an incident was paged, acknowledged with a 2xx, and then simply never appeared on the board.
#: Refusing is strictly better because webhook senders retry: Alertmanager re-delivers on a 5xx,
#: so a full spool becomes a delay instead of a lost page. Found in review (GPT 5.6).
REJECT_SPOOL_FULL = "spool is full"

#: Rejection reason for an oversized body. Shared with the route, which enforces the same
#: cap while STREAMING the body — `enqueue`'s own check can only see a body already in
#: memory, so the two must agree on the reason for the status mapping and audit line to
#: match wherever the cap is hit.
REJECT_BODY_TOO_LARGE = "body too large"

#: Chunk size for the route's streaming read. Small enough that the refusal peak stays
#: near the cap, large enough not to make an accepted 256 KiB body a many-await loop.
READ_CHUNK_BYTES = 64 * 1024

_queue: deque[Signal] = deque(maxlen=MAX_QUEUED_SIGNALS)

#: Serializes the spool's compound operations across the loop/worker-thread boundary.
#: `enqueue` runs in an `asyncio.to_thread` worker (the webhook route offloads it), while
#: `ack` runs on the event loop, so they genuinely execute concurrently. A single deque method
#: is atomic under the GIL, but `ack`'s popleft-then-append is TWO steps: at `maxlen`, an
#: `enqueue.append` landing between them momentarily fills the deque, and `ack`'s own append
#: then evicts the oldest — which can be the alert `enqueue` just accepted. One lock around
#: each compound mutation closes that window. A plain `threading.Lock`, not the cross-process
#: file lock the store/ledger use: this is same-process loop-vs-thread contention, and the
#: spool is in-memory (a restart drops it by design), so there is nothing on disk to serialize.
_queue_lock = threading.Lock()


def verify_signature(raw_body: bytes, provided: str) -> bool:
    """Constant-time HMAC check over the raw body.

    Fail-closed: no configured secret, or no provided signature, means reject. An
    unauthenticated path that can inject incidents would let anyone who can reach
    the port manufacture work and drive the agent's attention.
    """
    secret = get_secret(PROVIDER_ID, _SECRET_SIGNING_KEY)
    if not secret or not provided:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided.strip().lower())


#: Cap on label pairs kept from one delivery. Labels reach the model's context and
#: the fingerprint, so an unbounded map is both a token cost and a way to bloat the
#: dispatch index from outside.
MAX_LABELS = 50

#: Cap on the attribution text kept from a suppression. Same bound as the other
#: free-text fields — it reaches the board and (via the incident) a model prompt.
MAX_SUPPRESSION_TEXT = 200


def _normalize_labels(raw: Any) -> dict[str, str]:
    """Coerce a payload's ``labels`` to ``dict[str, str]``, or ``{}``.

    Guards the type BEFORE calling ``.items()``. The previous version put the
    ``isinstance`` check in a comprehension's ``if`` clause — which is evaluated per
    item, after ``.items()`` had already been called on the raw value — so
    ``{"labels": "text"}`` raised ``AttributeError``. That escaped ``enqueue``'s
    ``except`` (which only covers JSON/Unicode errors) and 500-ed the ingress, so a
    correctly-signed sender could crash the endpoint with one malformed field.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if len(out) >= MAX_LABELS:
            break
        out[str(key)[:100]] = str(value)[:200]
    return out


def signal_from_payload(payload: dict[str, Any]) -> Signal | None:
    """Map a flat webhook body onto a single Signal.

    Accepts a small, documented envelope and is deliberately strict about it: a
    body with no title has nothing a human could act on, so it is refused rather
    than turned into an unreadable board row.

    ``state``/``status`` is now read through ``normalize_state`` instead of being
    hardcoded to ``firing``. A sender that could create work but never retract it left
    reconcile inferring recovery from absence — which is exactly the inference that
    closes live work when a poll fails.

    ``suppressed_by``/``suppressed_reason`` are accepted here too, not only on the
    Alertmanager path. Most providers that HAVE a suppression concept do not speak
    Alertmanager's shape at all — Zabbix publishes ``suppressed=1``, Icinga a
    ``downtime_depth`` — so a forwarder normalizing either onto this envelope needs
    somewhere to put the attribution. Without that, only Alertmanager-shaped bodies could
    express "a human parked this", and everyone else would be back to reporting ``firing``.
    """
    title = str(payload.get("title") or payload.get("summary") or "").strip()
    if not title:
        return None
    native_id = str(payload.get("id") or payload.get("fingerprint") or title)[:200]
    return Signal.create(
        source=PROVIDER_ID,
        native_id=native_id,
        title=title,
        severity=normalize_severity(str(payload.get("severity", ""))),
        state=normalize_state(str(payload.get("state") or payload.get("status") or STATE_FIRING)),
        fired_at=str(payload.get("fired_at") or utc_now_iso()),
        resource=str(payload.get("resource", ""))[:200],
        url=str(payload.get("url", ""))[:500],
        labels=_normalize_labels(payload.get("labels")),
        provider_key=str(payload.get("fingerprint") or payload.get("provider_key") or "")[:200],
        suppressed_by=str(payload.get("suppressed_by", ""))[:MAX_SUPPRESSION_TEXT],
        suppressed_reason=str(payload.get("suppressed_reason", ""))[:MAX_SUPPRESSION_TEXT],
    )


#: The two suppression kinds Alertmanager distinguishes, and the key each is published
#: under. Ordered: a silence is a person's explicit decision and outranks an inhibition
#: for display purposes when a provider somehow reports both.
_SUPPRESSION_SOURCES: tuple[tuple[str, str], ...] = (
    ("silencedBy", "silenced"),
    ("inhibitedBy", "inhibited"),
)


def _suppression_attribution(status: dict[str, Any]) -> tuple[str, str]:
    """Read ``(suppressed_by, suppressed_reason)`` out of an Alertmanager status object.

    Returns two empty strings when the object carries no attribution, which is the honest
    answer — a fabricated owner is worse than a blank one (see ``Signal.suppressed_by``).
    """
    for key, reason in _SUPPRESSION_SOURCES:
        raw = status.get(key)
        # Guard the type BEFORE indexing, the rule `_normalize_labels` above exists to
        # enforce: `silencedBy` is a list in the v2 schema, but a hand-rolled forwarder
        # sending a bare string must not crash the ingress.
        if isinstance(raw, (list, tuple)):
            joined = ", ".join(str(item) for item in raw if str(item).strip())
        else:
            joined = str(raw or "").strip()
        if joined:
            return joined[:MAX_SUPPRESSION_TEXT], reason
    return "", ""


def _alert_status(raw: dict[str, Any]) -> tuple[str, str, str]:
    """Read one alert's status as ``(state_text, suppressed_by, suppressed_reason)``.

    Two real shapes, and only one of them used to work:

    - **v4 webhook envelope** — ``status`` is the scalar ``"firing"``/``"resolved"``.
    - **v2 ``gettableAlert``** — ``status`` is the OBJECT
      ``{"state": "suppressed", "silencedBy": [...], "inhibitedBy": [...]}``, which is
      what anything relaying ``GET /api/v2/alerts`` forwards.

    The previous scalar-only read (``str(raw.get("status") or ...)``) stringified that
    object, so it normalized to ``unknown`` — the suppression became "we could not parse
    the state" and ``silencedBy`` was dropped on the floor entirely. The caller then had
    a signal indistinguishable from a garbage one from a sender that was being perfectly
    explicit about a human having parked the alert.
    """
    status = raw.get("status")
    if isinstance(status, dict):
        state_text = str(status.get("state") or "").strip()
        by, reason = _suppression_attribution(status)
        return state_text, by, reason
    return str(status or "").strip(), "", ""


def _alert_title(alert: dict[str, Any], labels: dict[str, str], annotations: dict[str, str]) -> str:
    """Best available human-readable title for one Alertmanager-shaped alert.

    Preference order matches what an operator would read first: the rule author's own
    summary, then the description, then the rule name. A raw Alertmanager body carries
    NO top-level title, which is why requiring one rejected the single most common
    machine-readable alert envelope in the landscape outright.
    """
    for candidate in (
        annotations.get("summary"),
        annotations.get("description"),
        annotations.get("title"),
        labels.get("alertname"),
        alert.get("title"),
    ):
        text = str(candidate or "").strip()
        if text:
            return text[:300]
    return ""


def signals_from_payload(payload: dict[str, Any]) -> list[Signal]:
    """Map one webhook delivery onto ONE OR MORE Signals.

    Two accepted shapes:

    **Alertmanager/Grafana v4** — ``{status, alerts: [...], commonLabels, ...}``. Each
    entry of ``alerts`` becomes its own Signal. Fanning out matters because Alertmanager
    groups by design: one notification routinely carries every instance of a firing
    rule, and collapsing that into a single board row loses which instances are
    affected. Grafana additionally sends per-alert ``values`` (the actual breaching
    numbers), which are kept as labels — free evidence for a source that otherwise
    arrives with none.

    **The flat native envelope** — unchanged, so every existing sender keeps working.

    Per-alert ``status`` wins over the envelope's, because Alertmanager sends
    ``status: firing`` at the top while individual alerts inside may already carry an
    ``endsAt`` in the past. That per-alert status is read through ``_alert_status``, which
    also handles the v2 OBJECT form and the suppression attribution inside it.
    """
    raw_alerts = payload.get("alerts")
    if not isinstance(raw_alerts, list) or not raw_alerts:
        single = signal_from_payload(payload)
        return [single] if single is not None else []

    envelope_status = str(payload.get("status") or "").strip()
    common_labels = _normalize_labels(payload.get("commonLabels"))
    common_annotations = _normalize_labels(payload.get("commonAnnotations"))

    signals: list[Signal] = []
    for raw in raw_alerts[:MAX_QUEUED_SIGNALS]:
        if not isinstance(raw, dict):
            continue
        state_text, suppressed_by, suppressed_reason = _alert_status(raw)
        labels = {**common_labels, **_normalize_labels(raw.get("labels"))}
        annotations = {**common_annotations, **_normalize_labels(raw.get("annotations"))}
        title = _alert_title(raw, labels, annotations)
        if not title:
            # Nothing a human could act on. Skip this entry rather than failing the
            # whole delivery: one malformed alert in a group of forty must not discard
            # the thirty-nine that are fine.
            continue

        # Alertmanager's own fingerprint is a server-computed identity for the alert —
        # strictly better than anything derivable from the rendered text, which is why
        # it is passed through as the exact-match key rather than folded into the title.
        provider_key = str(raw.get("fingerprint") or "")[:200]
        resource = str(labels.get("instance") or labels.get("job") or labels.get("pod") or "")[:200]
        # Values are Grafana's breaching numbers; keep them readable but bounded.
        values = raw.get("values")
        if isinstance(values, dict) and values:
            labels.setdefault("values", ", ".join(f"{k}={v}" for k, v in list(values.items())[:10]))

        signals.append(
            Signal.create(
                source=PROVIDER_ID,
                # An Alertmanager fingerprint is stable across re-deliveries of the same
                # alert, which is what makes the dispatch index dedupe correctly. Falling
                # back to alertname+resource keeps that property for senders without one.
                native_id=(provider_key or f"{labels.get('alertname', title)}:{resource}")[:200],
                title=title,
                severity=normalize_severity(str(labels.get("severity", ""))),
                state=normalize_state(state_text or envelope_status or STATE_FIRING),
                fired_at=str(raw.get("startsAt") or utc_now_iso()),
                resource=resource,
                url=str(raw.get("generatorURL") or raw.get("panelURL") or "")[:500],
                labels={**labels, **annotations},
                provider_key=provider_key,
                suppressed_by=suppressed_by,
                suppressed_reason=suppressed_reason,
            )
        )
    return signals


def enqueue(raw_body: bytes, signature: str) -> tuple[bool, str]:
    """Verify and queue a delivery. Returns ``(accepted, detail)``."""
    if not provider_enabled(PROVIDER_ID):
        return False, "webhook source is not enabled"
    if not has_secrets(PROVIDER_ID, _REQUIRED_SECRETS):
        return False, "no signing secret configured"
    if len(raw_body) > MAX_BODY_BYTES:
        return False, REJECT_BODY_TOO_LARGE
    if not verify_signature(raw_body, signature):
        return False, "signature mismatch"
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False, "malformed JSON"
    if not isinstance(payload, dict):
        return False, "payload must be a JSON object"
    signals = signals_from_payload(payload)
    if not signals:
        return False, "payload has no title"
    # Under the lock: this runs in a worker thread while `ack` rotates on the loop. A bare
    # `extend` at `maxlen` racing `ack`'s popleft/append window can evict an entry neither
    # side meant to drop. See `_queue_lock`.
    #
    # The CAPACITY CHECK IS INSIDE THE LOCK, and that placement is the point: checking before
    # acquiring would be a TOCTOU where a concurrent delivery fills the last slot between the
    # test and the extend, which is exactly the eviction being prevented. Rejecting the whole
    # batch rather than a prefix keeps one Alertmanager delivery atomic — a partially-accepted
    # fan-out would report success for alerts that were dropped.
    with _queue_lock:
        if len(_queue) + len(signals) > MAX_QUEUED_SIGNALS:
            logger.warning(
                "ops-mission-control: refusing a %d-signal delivery — spool holds %d of %d "
                "and accepting would evict the oldest unclaimed alerts; the sender should retry",
                len(signals),
                len(_queue),
                MAX_QUEUED_SIGNALS,
            )
            return False, REJECT_SPOOL_FULL
        _queue.extend(signals)
    if len(signals) == 1:
        return True, signals[0].id
    return True, f"{len(signals)} signals: " + ", ".join(s.id for s in signals[:5])


def peek() -> list[Signal]:
    """Return every queued signal WITHOUT consuming the spool.

    This is what ``poll()`` does now, and the change is a data-loss fix rather than a
    refactor. ``poll_all`` has three callers and only ONE of them claims: the heartbeat
    (``dispatch.run_cycle``), the Signals-tab read (``GET /signals``), and the claim
    authorization check (``POST /incident/claim``, which polls only to confirm one specific
    id is really firing). The old ``drain()`` emptied the deque for all three, with no
    re-enqueue path anywhere — so an operator clicking "Poll now" while five Alertmanager
    alerts sat in the spool got them rendered once as JSON and permanently destroyed. No
    incident, no investigation, no trace: a delivered, signature-verified alert silently
    became nothing. Found in review.

    The claim path was worse than the reported case and unreported: claiming ONE signal
    drained and discarded every OTHER queued delivery as a side effect.

    Even the heartbeat could not safely drain: ``run_cycle`` filters candidates against
    already-owned incidents and then takes only ``[:limit]`` per cycle, so signals beyond
    the per-cycle cap were dropped by the very consumer that had just consumed them.

    So consumption is now tied to the thing that makes a signal durable — an incident owning
    it — via ``ack()``. Reads are free.
    """
    return list(_queue)


def ack(signal_ids: set[str]) -> int:
    """Drop delivered signals the caller has taken durable ownership of. Returns the count.

    Called by ``dispatch.run_cycle`` with the ids it actually claimed (an incident on disk).
    Anything not acked stays spooled for the next cycle, which is what makes the per-cycle
    claim cap safe. ``maxlen`` still bounds the spool, so a sender that outruns the heartbeat
    forever drops OLDEST-first rather than growing memory — the same trade as before, just no
    longer triggered by a read.

    ROTATES exactly the number of entries observed on entry, using single ``popleft()`` /
    ``append()`` calls, rather than ``clear()`` + ``extend(keep)``. The obvious version raced
    ingestion and dropped an accepted delivery: ``enqueue`` runs in a WORKER THREAD (the
    webhook route awaits ``asyncio.to_thread(webhook.enqueue, ...)``), so a signature-verified
    signal could be appended between building ``keep`` and the ``clear()`` that discarded it —
    a 200-accepted alert vanishing with no incident and no trace, which is the same failure
    class the peek/ack split was introduced to fix. Found in review.

    Bounding the loop to the entry length is what makes it safe: anything appended while this
    runs sits behind the rotation window and is simply never examined this pass, so it stays
    spooled for the next cycle.

    Held under `_queue_lock` for the WHOLE rotation. A single deque op is atomic under the GIL,
    but the popleft-then-append pair is not: at `maxlen`, a worker-thread `enqueue.extend`
    landing between them fills the deque, and this function's own `append` then silently evicts
    the oldest — which can be the alert that `enqueue` just accepted. Serializing enqueue and
    the whole ack rotation closes that window. Found in review.
    """
    if not signal_ids:
        return 0
    removed = 0
    with _queue_lock:
        for _ in range(len(_queue)):
            try:
                signal = _queue.popleft()
            except IndexError:  # pragma: no cover — another consumer drained it
                break
            if signal.id in signal_ids:
                removed += 1
            else:
                _queue.append(signal)
    return removed


def queue_depth() -> int:
    return len(_queue)


def reset_spool() -> None:
    """Empty the spool outright. TEST ISOLATION ONLY — never call this from app code.

    The module-level deque outlives any one test, so a leftover signal from one case shows
    up as a phantom in the next. Deliberately named for what it is rather than reusing the
    old ``drain()``: an unconditional clear is exactly the operation that caused the data
    loss ``peek``/``ack`` fixes, so it must not sit in the module looking like a legitimate
    consumer.
    """
    _queue.clear()


class WebhookSignalSource:
    """Drains the webhook spool into the dispatch loop."""

    id = PROVIDER_ID
    display_name = "Inbound webhook"
    detail = "Any system that can POST signed JSON — Grafana, Alertmanager, Sentry, scripts."
    config_fields: tuple[str, ...] = ("enabled",)
    secret_fields: tuple[str, ...] = _REQUIRED_SECRETS

    #: NOT a snapshot — the one signal source in this app that is not. ``poll`` drains a
    #: spool, so a delivered signal appears in exactly ONE cycle's result and is absent from
    #: every cycle after it, whether or not anything changed at the sender. Nothing can be
    #: concluded from its absence, which is the opposite of what a successful CloudWatch
    #: poll licenses, and consumers that resolve or verify on absence must consult this.
    #:
    #: What the missing flag cost: ``registry.poll_all`` recorded an empty drain as
    #: ``{"ok": True, "signals": 0}``, and ``dispatch.verify_pending_actions`` reads exactly
    #: that pair as "the source answered and the signal is gone". So one cycle after any
    #: webhook delivery, an action against that signal verified as ``cleared`` — "the
    #: resolve held" — with the fault still live at the sender. Same class of bug as
    #: resolving on a failed poll, but reached through a SUCCESSFUL one, which is why the
    #: existing ``poll_health`` guard could not catch it.
    is_snapshot = False

    def configured(self) -> bool:
        return provider_enabled(PROVIDER_ID) and has_secrets(PROVIDER_ID, _REQUIRED_SECRETS)

    async def poll(self) -> list[Signal]:
        if not self.configured():
            return []
        # PEEK, not drain. A poll is a read; only `dispatch.run_cycle` acking a claimed
        # signal removes it. See `peek()` for the data loss the drain caused.
        return peek()
