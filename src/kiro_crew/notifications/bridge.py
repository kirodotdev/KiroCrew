"""Notification bridge: bus egress fanout to chat transports.

The bus (``notifications/bus.py``) had exactly one sink, the dashboard. This
module is the second sink: it routes matching notes to the user's connected
chat transports as owner DMs, governed per transport and configured per
source channel. Routing, not escalation -- delivery is unconditional on
dashboard presence (see ``docs/request-for-change/rfc-notification-bridge.md``,
phase B1).

Three properties this module owes the rest of the gateway, in the order they
constrain the code:

**Local delivery isolation.** The dashboard sink runs first and synchronously;
:meth:`BridgeDispatcher.schedule` returns after creating a task and never
raises, so no transport latency or failure can delay, break, or reorder
dashboard delivery. Every per-transport leg is independently guarded, so one
transport's failure cannot suppress another's delivery.

**Loop safety.** The dispatcher is constructed from a sink resolver and a
settings reader -- it holds no reference to the bus and has no path that can
publish a note. ``test/test_notification_bridge.py`` pins this by scanning this
module's own source for bus-publishing calls, so the property survives edits
that a behavioural test would not notice.

**Fail-closed governance.** Each leg is double-gated through ``vet_and_audit``
(``capabilities.messaging`` then ``channels/<transport>``) with
``fail_closed=True``: a governance evaluation error denies rather than
degrades, because the alternative is a note leaving the host under an
unreadable policy.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from kiro_crew import sel as _sel
from kiro_crew.platform import governance_profiles as _governance
from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY
from kiro_crew.security import exfil as _exfil
from kiro_crew.security import redaction as _redaction

# Imported as MODULES, then called through them, rather than binding the four
# functions into this namespace. Both forms are top-level; only this one keeps
# `patch("kiro_crew.sel.sel")` and its siblings effective, which is the same
# hazard the top-level-imports rule names when it warns that an import can make
# a mock target the wrong module namespace. HOST_SESSION_KEY is a value read
# once at class-definition time, so it is imported directly.

logger = logging.getLogger(__name__)

# The transport ids a routing rule may name. Validation is against the KNOWN
# set, never the CONNECTED set: a transport that is down for an hour must not
# have its persisted routing silently rewritten. The Settings UI (phase B3)
# filters its picker to connected transports, which is what keeps a user from
# arming a route to a transport they never configured -- so a toggle a user can
# reach is always attached to a transport that can receive.
KNOWN_BRIDGE_TRANSPORTS: tuple[str, ...] = (
    "slack",
    "discord",
    "telegram",
    "webex",
    "wecom",
)

# Priority floors a rule may set, loosest last. ``critical`` is the default
# when a route is armed without one: arming a route is a request for the
# interrupting notes, not for every passive heartbeat.
DELIVER_MIN_PRIORITIES: tuple[str, ...] = ("critical", "default", "all")
DEFAULT_DELIVER_MIN_PRIORITY = "critical"

# Effective priority (post user override) ranked loosest-last, so a floor
# admits every priority at or above its own rank.
_PRIORITY_RANK: dict[str, int] = {"critical": 0, "default": 1, "passive": 2}
_FLOOR_RANK: dict[str, int] = {"critical": 0, "default": 1, "all": 2}

# Marks a bridged send in the SEL trail. Each transport's inbound path already
# drops self-authored bot messages, so a bridged DM cannot echo back in as a
# turn; this origin makes the delivery attributable in the audit trail rather
# than being what enforces the loop invariant.
BRIDGE_ORIGIN = "notification-bridge"

# Per-transport egress budget (RFC open question 3). A channel routed with an
# ``all`` floor can otherwise turn a producer loop into a chat flood, and the
# ingress budget does not help: it is per app, while this protects one chat
# surface from every producer at once. Deliberately coarse, same as ingress.
BRIDGE_TOKENS_PER_WINDOW = 20
BRIDGE_WINDOW_SECS = 300.0
BRIDGE_BURST = 5
_BRIDGE_REFILL_RATE = BRIDGE_TOKENS_PER_WINDOW / BRIDGE_WINDOW_SECS

# The bus sets ``source`` from the verified app token as ``app:<name>``, and a
# request body cannot override it. That makes it the one producer identity on a
# note that its producer did not choose, which is why governance and audit both
# read it instead of anything under ``meta``.
_APP_SOURCE_PREFIX = "app:"

# Ceiling on the rendered body before the sink's own chunking sees it. A note
# body is capped by the payload validator already; this bounds the rendered
# form for a transport whose floor is smaller than that cap.
BRIDGE_BODY_CHARS = 2000


class BridgeSink(Protocol):
    """One transport's egress leg, resolved fresh for every delivery.

    Resolution is per-note on purpose: ``None`` from the resolver is the
    honest answer for a transport that is configured but not currently
    connected, and it must stay a skip rather than an error. A sink cached at
    boot would keep answering for a transport that has since disconnected.
    """

    transport_id: str

    async def send(self, text: str) -> str:
        """Deliver *text* to the owner's DM; return a platform message id."""


@dataclass
class DeliveryRule:
    """One source channel's resolved routing rule."""

    transports: tuple[str, ...] = ()
    min_priority: str = DEFAULT_DELIVER_MIN_PRIORITY

    @property
    def armed(self) -> bool:
        return bool(self.transports)


@dataclass
class BridgeOutcome:
    """What one transport leg did, for tests and for the caller's logs."""

    transport: str
    delivered: bool
    reason: str = ""
    message_id: str = ""


@dataclass
class _Bucket:
    tokens: float = float(BRIDGE_BURST)
    last_refill: float = field(default_factory=time.monotonic)


class BridgeRateLimiter:
    """Token bucket per transport id. Event-loop use, like the ingress one."""

    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}

    def allow(self, transport: str) -> bool:
        now = time.monotonic()
        bucket = self._buckets.get(transport)
        if bucket is None:
            bucket = _Bucket(last_refill=now)
            self._buckets[transport] = bucket
        elapsed = now - bucket.last_refill
        bucket.tokens = min(float(BRIDGE_BURST), bucket.tokens + elapsed * _BRIDGE_REFILL_RATE)
        bucket.last_refill = now
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True
        return False


def normalize_deliver_to(value: Any) -> tuple[str, ...]:
    """Validate a ``deliver_to`` input into a deduped, ordered transport tuple.

    Requires a list or tuple, not merely something iterable. Every other
    iterable that could reach here yields transport-shaped strings from
    something that is not a list of transports: ``"slack"`` yields five
    single characters, and a mapping yields its KEYS, so ``{"slack": false}``
    would arm Slack off a value that says not to.

    Raises :class:`ValueError` for a non-list, a non-string entry, or an
    unknown transport id. Order follows :data:`KNOWN_BRIDGE_TRANSPORTS` so a
    persisted rule is stable regardless of the order the caller sent.
    """
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError("deliver_to must be a list of transport ids")
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, str):
            raise ValueError("deliver_to entries must be strings")
        name = entry.strip().lower()
        if name not in KNOWN_BRIDGE_TRANSPORTS:
            raise ValueError(
                f"unknown transport {entry!r}; known: {', '.join(KNOWN_BRIDGE_TRANSPORTS)}"
            )
        seen.add(name)
    return tuple(t for t in KNOWN_BRIDGE_TRANSPORTS if t in seen)


def normalize_min_priority(value: Any) -> str:
    """Validate a ``deliver_min_priority`` input. Raises :class:`ValueError`."""
    if value is None:
        return DEFAULT_DELIVER_MIN_PRIORITY
    if not isinstance(value, str) or value not in DELIVER_MIN_PRIORITIES:
        raise ValueError("deliver_min_priority must be one of " + ", ".join(DELIVER_MIN_PRIORITIES))
    return value


def rule_from_settings(entry: Mapping[str, Any] | None) -> DeliveryRule:
    """Read one channel's stored entry into a rule, tolerating bad values.

    A hand-edited settings file is not a validation boundary -- the PUT is.
    An unusable value here disarms the route (the safe direction) instead of
    raising into a delivery path.
    """
    if not entry:
        return DeliveryRule()
    try:
        transports = normalize_deliver_to(entry.get("deliver_to"))
    except ValueError:
        logger.warning("Ignoring unusable deliver_to in notification settings")
        return DeliveryRule()
    try:
        floor = normalize_min_priority(entry.get("deliver_min_priority"))
    except ValueError:
        logger.warning(
            "Ignoring unusable deliver_min_priority; using %s", DEFAULT_DELIVER_MIN_PRIORITY
        )
        floor = DEFAULT_DELIVER_MIN_PRIORITY
    return DeliveryRule(transports=transports, min_priority=floor)


def priority_clears_floor(priority: Any, floor: str) -> bool:
    """Whether an EFFECTIVE priority is admitted by *floor*.

    Effective, not producer-declared: the caller passes the note after
    ``ChannelSettings.apply``, so a muted channel reads ``passive`` here and
    only an ``all`` floor routes it. Silencing the dashboard while still
    paging a phone would be the opposite of what mute means, which is why
    mute is applied before this and not consulted inside it.
    """
    rank = _PRIORITY_RANK.get(priority if isinstance(priority, str) else "")
    if rank is None:
        # An unknown priority is not admitted by a floor it cannot be ranked
        # against. The bus constrains priority to PRIORITIES, so this is the
        # hand-edited-row case.
        return False
    return rank <= _FLOOR_RANK.get(floor, 0)


def render_bridge_text(note: Mapping[str, Any]) -> str:
    """Render a note into the compact chat form: marker, title, body, link.

    Plain text on purpose. Each transport's renderer owns its own formatting
    and chunking, so the bridge produces one neutral string rather than five
    dialects of markup.
    """
    priority = note.get("priority")
    marker = "🔴 " if priority == "critical" else ""
    title = str(note.get("title") or "").strip()
    lines = [f"{marker}{title}" if title else f"{marker}Notification".strip()]
    body = str(note.get("body") or "").strip()
    if body:
        if len(body) > BRIDGE_BODY_CHARS:
            body = body[:BRIDGE_BODY_CHARS].rstrip() + "…"
        lines.append(body)
    url = note.get("url")
    if isinstance(url, str) and url.strip():
        # Dashboard-internal path-only routes, validated by the bus. Sent as
        # text: it is a pointer for the user's own browser, not a link the
        # transport can resolve.
        lines.append(f"↪ {url.strip()}")
    channel = note.get("channel")
    if isinstance(channel, str) and channel:
        lines.append(f"({channel})")
    return "\n\n".join(lines)


def _redact_for_egress(text: str) -> str:
    """Redact credentials and exfiltration URLs from outbound text.

    The dashboard sink redacts the note it stores, and this redacts the string
    this module is about to hand to a transport. That is deliberate
    duplication: the property "nothing leaves the host unredacted" has to hold
    for this module standing alone, or it becomes a property of the wiring
    order instead, and the wiring order is exactly what a later refactor
    changes.
    """
    if not text:
        return text
    try:
        text, _ = _exfil.redact_exfiltration_urls(text)
        text, _ = _redaction.redact_credentials(text)
        return text
    except Exception:
        # A redactor that cannot run must not let raw text out: refuse the
        # content, keep the delivery shape. The note is already on the
        # dashboard, so the user still has the full text there.
        logger.warning("Bridge redaction failed; withholding note content", exc_info=True)
        return "[content withheld: redaction unavailable]"


class BridgeDispatcher:
    """Route bus notes to chat transports: rules, governance, render, audit.

    Collaborators are injected rather than imported so the dispatcher stays
    testable without a gateway and, more importantly, so its dependency set is
    visible: a resolver for sinks, a reader for settings, a provider for the
    gateway loop. None of them can publish a note.

    ``host_session_key`` defaults to the shared ``HOST_SESSION_KEY`` sentinel
    because the value is not a label, it selects a governance profile:
    ``_infer_surface`` maps ``"_host"`` to the ``host`` surface, while a bare
    ``"dashboard"`` matches no prefix and falls through to ``slack`` -- which
    would judge "may the host send to Slack" under a Slack surface profile and
    skip an operator's ``surface:host`` denial entirely.
    """

    def __init__(
        self,
        *,
        sink_resolver: Callable[[str], BridgeSink | None],
        settings_reader: Callable[[str], Mapping[str, Any]],
        loop_provider: Callable[[], asyncio.AbstractEventLoop | None] | None = None,
        rate_limiter: BridgeRateLimiter | None = None,
        host_session_key: str = HOST_SESSION_KEY,
    ) -> None:
        self._resolve_sink = sink_resolver
        self._read_settings = settings_reader
        self._resolve_loop = loop_provider
        self._limiter = rate_limiter or BridgeRateLimiter()
        self._host_session_key = host_session_key
        # Tracked so shutdown can drain and so a test can await the fanout.
        self._tasks: set[asyncio.Task[Any]] = set()

    # -- rule evaluation ---------------------------------------------------
    def rule_for(self, channel: Any) -> DeliveryRule:
        """The resolved rule for one source channel."""
        if not isinstance(channel, str) or not channel:
            return DeliveryRule()
        try:
            entry = self._read_settings(channel)
        except Exception:
            logger.warning("Bridge could not read settings for %s", channel, exc_info=True)
            return DeliveryRule()
        return rule_from_settings(entry)

    def routes(self, note: Mapping[str, Any]) -> tuple[str, ...]:
        """Transports this note routes to: armed rule AND priority clears floor."""
        rule = self.rule_for(note.get("channel"))
        if not rule.armed:
            return ()
        if not priority_clears_floor(note.get("priority"), rule.min_priority):
            return ()
        return rule.transports

    # -- scheduling --------------------------------------------------------
    def schedule(self, note: Mapping[str, Any]) -> asyncio.Task[Any] | None:
        """Schedule fanout for *note* and return immediately. Never raises.

        Returns a task ONLY when one was created on this thread's own loop, so
        ``None`` means "nothing here for you to await" rather than "not
        delivered". Three cases produce it: the note routes nowhere; the caller
        is off the loop and the fanout was handed to the gateway loop instead;
        or no loop is reachable at all.

        That middle case is why an off-loop caller is not simply dropped.
        Producers publish from worker threads on purpose -- Code Review Sage's
        run-finished notice is an ``asyncio.to_thread(state.notify, ...)``,
        deliberately off-loop because the delivery sink writes to disk -- and in
        that thread ``get_running_loop`` raises although the gateway loop is
        alive and reachable. Dropping there would silently lose exactly the
        notification a user routes to chat. Only an unreachable loop is a real
        skip, and then the dashboard still holds the note.
        """
        try:
            if not self.routes(note):
                return None
        except Exception:
            logger.warning("Bridge scheduling failed", exc_info=True)
            return None
        snapshot = dict(note)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            return self._spawn(loop, snapshot)
        self._hand_off_to_gateway_loop(snapshot)
        return None

    def _spawn(self, loop: asyncio.AbstractEventLoop, note: dict[str, Any]) -> asyncio.Task[Any]:
        """Create the fanout task. Runs ON the loop thread, so ``_tasks`` --
        which ``drain`` reads -- is only ever mutated from there."""
        task = loop.create_task(self._dispatch_guarded(note))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _hand_off_to_gateway_loop(self, note: dict[str, Any]) -> None:
        """Ask the gateway loop to spawn the fanout, from a non-loop thread."""
        try:
            loop = self._resolve_loop() if self._resolve_loop is not None else None
        except Exception:
            logger.warning("Bridge could not resolve the gateway loop", exc_info=True)
            return
        if loop is None:
            # Genuinely no loop: a CLI, a boot-time note, a synchronous test.
            # The dashboard already has the note, so this is the right degrade.
            return
        try:
            loop.call_soon_threadsafe(self._spawn, loop, note)
        except RuntimeError:
            # A closed loop, which is the whole reason this is guarded: the
            # provider can hand back a loop that shut down between the read and
            # this call, and there is no check that closes that window -- an
            # `is_closed()` test before the call is a check-then-act on the same
            # race, and it detects nothing this does not.
            logger.warning("Bridge hand-off missed a closed gateway loop")

    async def drain(self, timeout: float = 5.0) -> None:
        """Await in-flight fanout, bounded. For shutdown and for tests."""
        pending = [t for t in tuple(self._tasks) if not t.done()]
        if not pending:
            return
        await asyncio.wait(pending, timeout=timeout)

    async def _dispatch_guarded(self, note: Mapping[str, Any]) -> list[BridgeOutcome]:
        try:
            return await self.dispatch(note)
        except Exception:
            # A scheduled task's exception would otherwise surface as an
            # unretrieved-exception warning at GC time, attributed to nothing.
            logger.warning("Bridge dispatch failed", exc_info=True)
            return []

    # -- delivery ----------------------------------------------------------
    async def dispatch(self, note: Mapping[str, Any]) -> list[BridgeOutcome]:
        """Deliver *note* to every routed transport. One leg cannot fail another."""
        transports = self.routes(note)
        if not transports:
            return []
        text = _redact_for_egress(render_bridge_text(note))
        results = await asyncio.gather(
            *(self._deliver_one(transport, note, text) for transport in transports),
            return_exceptions=True,
        )
        outcomes: list[BridgeOutcome] = []
        for transport, result in zip(transports, results):
            if isinstance(result, BaseException):
                logger.warning("Bridge leg raised for %s", transport, exc_info=result)
                outcomes.append(BridgeOutcome(transport=transport, delivered=False, reason="error"))
            else:
                outcomes.append(result)
        return outcomes

    async def _deliver_one(
        self, transport: str, note: Mapping[str, Any], text: str
    ) -> BridgeOutcome:
        denial = await asyncio.to_thread(self._vet, transport, note)
        if denial:
            return BridgeOutcome(transport=transport, delivered=False, reason=denial)
        # Resolve the sink BEFORE spending a token. The budget caps what is
        # DELIVERED, and a leg with no sink sends nothing, so charging it would
        # let a transport that is merely disconnected drain the burst and
        # throttle the first real delivery after it reconnects.
        sink = self._resolve_sink(transport)
        if sink is None:
            # Configured but not connected: an audited no-op, not an error.
            self._audit(transport, note, outcome="skipped", error="transport not connected")
            return BridgeOutcome(transport=transport, delivered=False, reason="not_connected")
        if not self._limiter.allow(transport):
            self._audit(transport, note, outcome="throttled", error="egress budget exhausted")
            return BridgeOutcome(transport=transport, delivered=False, reason="throttled")
        try:
            message_id = await sink.send(text)
        except Exception as exc:  # transport-owned failure classes
            # NOT retried here, deliberately. A send that fails ambiguously --
            # a timeout after the platform accepted the message -- is
            # indistinguishable from one that never landed, so a retry can post
            # the same notification twice. The asymmetry decides it: the note is
            # already on the dashboard, so a dropped bridge leg costs the user a
            # duplicate surface while a double post costs them a wrong one.
            # Retry that a transport CAN make safely belongs in that transport's
            # own policy, where it knows which failures are idempotent.
            logger.warning("Bridge send to %s failed", transport, exc_info=True)
            self._audit(transport, note, outcome="error", error=f"{type(exc).__name__}: {exc}")
            return BridgeOutcome(transport=transport, delivered=False, reason="error")
        self._audit(transport, note, outcome="delivered")
        return BridgeOutcome(transport=transport, delivered=True, message_id=str(message_id or ""))

    # -- governance + audit ------------------------------------------------
    @staticmethod
    def _producing_app(note: Mapping[str, Any]) -> str:
        """The app that produced this note, or '' when no app did.

        Read from ``source``, which the push handler sets from the VERIFIED
        token (``source=f"app:{app_name}"``, body cannot override) -- the only
        producer identity on a note that its producer cannot choose. Everything
        else an app can influence: ``meta`` merges flat onto the note and
        ``_RESERVED_NOTE_KEYS`` does not cover ``session_key``/``slot``/
        ``caller``, so those are request-body values wearing internal names.
        """
        source = note.get("source")
        if isinstance(source, str) and source.startswith(_APP_SOURCE_PREFIX):
            return source[len(_APP_SOURCE_PREFIX) :].strip()
        return ""

    @staticmethod
    def _claimed_session(note: Mapping[str, Any]) -> str:
        """The session a note CLAIMS produced it, or ''. Untrusted by design.

        ``meta`` merges flat onto the note and ``_RESERVED_NOTE_KEYS`` covers
        none of these three, so gateway code and an app-token request body write
        them through the same door and the note keeps no record of which did. The
        value therefore only ever ADDS a governance subject and never replaces
        one -- see :meth:`_vet`.

        The ``send_notification`` route is the one producer that sets this
        SERVER-side, from the ``X-Session-Key`` it already refuses a dead slot
        on, because its ``source`` is the fixed ``"system"`` and without this the
        note names no producer at all. That does not make the value trusted here:
        this function cannot tell that case from a body-set one, which is exactly
        why the polarity above is what carries the safety rather than provenance.

        ``slot`` is QUALIFIED before it is returned, because it is the only one
        of the three that is structurally a fragment rather than a session key:
        the frontend stores a bare slot id (``chat-1``), and ``_infer_source``
        recognises no prefix in it, so it falls through to the ``slack`` default.
        A bare slot would therefore have the claim judged under a Slack surface
        profile. Both directions of that are wrong, and the second is the one a
        user would notice: an operator's ``surface:dashboard`` denial is not
        consulted, AND a ``surface:slack`` denial is applied to a note a
        dashboard slot produced -- which, because the claim can only narrow,
        withholds a delivery that should have gone out. ``session_key`` and
        ``caller`` are full keys already, so whatever surface they infer is
        theirs by the same function the rest of the gateway uses.
        """
        slot = note.get("slot")
        for key in ("session_key", "caller"):
            value = note.get(key)
            if isinstance(value, str) and value:
                return value
        if isinstance(slot, str) and slot:
            # Already-qualified values pass through: a producer that wrote a full
            # key here must not become "dashboard:dashboard:chat-1".
            return slot if ":" in slot else f"dashboard:{slot}"
        return ""

    def _vet(self, transport: str, note: Mapping[str, Any]) -> str:
        """Double-gate one leg, fail-closed. Returns a denial reason or ''.

        Governance is the INTERSECTION of every subject that could own this
        note, which is what lets an unforgeable answer and a per-producer one
        coexist:

        * The host surface always. ``HOST_SESSION_KEY`` maps to the ``host``
          surface, and an operator's host-level messaging or channel denial must
          bind every bridged delivery.
        * The producing app, from the server-set ``source`` (``app:<name>``,
          which a request body cannot override), so an app whose own profile
          denies messaging cannot egress on a permissive surface.
        * The session the note CLAIMS, when it names one -- a cron, a hook, an
          agent turn. Added, never substituted, and that asymmetry is the whole
          point: the claim is caller-writable, so allowing it to widen would let
          an app name a permissive surface and pick its own profile, while
          allowing it only to narrow means a forged claim can at worst deny the
          forger's own note. A real producer's profile is consulted; a fake one
          buys nothing.

        Each subject carries its OWN app bind, and that pairing is the mechanism
        rather than a detail. ``resolve_active_scope`` returns the FIRST bound
        profile in a fixed precedence and the app bind outranks the surface bind,
        so one call can only ever answer for one profile: carrying ``app=`` on
        the host lookup would have a permissive app profile answer "may the host
        send", silently skipping the operator's ``surface:host`` denial, while
        omitting it everywhere would have a surface profile answer for the app.
        Binding the app on its own lookup alone is what makes this an
        intersection instead of N lookups of the same profile.

        Every subject must permit, so ANY denial denies. Runs on a worker thread
        (the caller uses ``to_thread``): governance resolution reads profile
        state and writes a SEL record, so it is blocking work that does not
        belong on the delivery loop.
        """
        app = self._producing_app(note)
        # (session_key, app bind) per subject. The app's own lookup keeps the
        # host session key because SEL records the session and not the app, and
        # the bridge does run as the host while deciding about the app's note.
        subjects: list[tuple[str, str]] = [(self._host_session_key, "")]
        claimed = self._claimed_session(note)
        if claimed and claimed != self._host_session_key:
            subjects.append((claimed, ""))
        if app:
            subjects.append((self._host_session_key, app))
        try:
            for subject, app_bind in subjects:
                for scope, item in (("capabilities.messaging", ""), ("channels", transport)):
                    decision = _governance.vet_and_audit(
                        scope,
                        item,
                        session_key=subject,
                        tool_name="notification_bridge",
                        app=app_bind,
                        fail_closed=True,
                    )
                    if not getattr(decision, "permitted", False):
                        return "denied_by_governance"
            return ""
        except Exception:
            # fail-closed: an unreadable policy denies. Audited below so the
            # denial is not silent.
            logger.warning(
                "Bridge governance evaluation failed for %s; denying", transport, exc_info=True
            )
            return "governance_error"

    def _audit(
        self,
        transport: str,
        note: Mapping[str, Any],
        *,
        outcome: str,
        error: str = "",
    ) -> None:
        """Write the SEL delivery record. Best-effort; never raises.

        Attributed by ``source`` for the same reason the governance subject is:
        it is the one producer identity on the note that the producer did not
        choose, so an audit row cannot be made to name someone else.
        """
        try:
            _sel.sel().log_api_access(
                caller=str(note.get("source") or "") or self._host_session_key,
                operation=f"notification_bridge.{transport}",
                outcome=outcome,
                source=BRIDGE_ORIGIN,
                resources=str(note.get("channel") or ""),
                error=error,
            )
        except Exception:
            pass


def known_transport_ids() -> tuple[str, ...]:
    """The transport ids a routing rule may name (for API validation)."""
    return KNOWN_BRIDGE_TRANSPORTS


__all__ = [
    "BRIDGE_ORIGIN",
    "DEFAULT_DELIVER_MIN_PRIORITY",
    "DELIVER_MIN_PRIORITIES",
    "KNOWN_BRIDGE_TRANSPORTS",
    "BridgeDispatcher",
    "BridgeOutcome",
    "BridgeRateLimiter",
    "BridgeSink",
    "DeliveryRule",
    "known_transport_ids",
    "normalize_deliver_to",
    "normalize_min_priority",
    "priority_clears_floor",
    "render_bridge_text",
    "rule_from_settings",
]
