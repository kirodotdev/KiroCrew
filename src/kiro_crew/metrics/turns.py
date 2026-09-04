"""The per-turn instrument family, emitted for every surface.

``kirocrew.turn.duration`` (histogram) plus the turn's own usage —
``kirocrew.turn.tokens`` (counter, ``direction`` attr) and the two billing
histograms ``kirocrew.turn.credits`` / ``kirocrew.turn.cost_usd``. All four are
emitted from the same boundary about the same turn, so latency, volume and spend
describe one population and can be read against each other.

The duration instrument powers two readings on the Telemetry page: turn latency
(p50/p90) and fault rate (the share of turns whose outcome is not ``ok``).
Both are only as honest as the population they sample, which is why this
module exists at all.

**Why this is not in ``chat_runner``.** It used to be. The emit lived beside
the dashboard turn loop, which made it structurally reachable from exactly one
surface: a cron job, a heartbeat task, a memory consolidation pass, a subagent,
a task-runner step, a workflow stage and every messaging channel each run agent
turns that never pass through that loop, so none of them produced a sample. The
consequence is worse than a gap, because an absent sample does not read as
absent: a background surface that is slow, stalling or erroring contributed
nothing, so the page rendered the interactive median and called it the system's
health. There was also no way to see how many background turns had run at all.

**The shared boundary is the usage row, not the ACP turn.** The obvious
candidate was ``acp/session_handle.py::SessionHandle._run_turn``, which every
ACP-backed surface really does cross. It was rejected: the handle knows only its
ACP ``sessionId``, not the Kiro Crew session key this metric must group by, and
an ``AcpClient``-backed session (the claude_code backend) never reaches it at
all. ``persist_token_record_async`` is the boundary that actually fits — every
surface already calls it once per turn at ``EVENT_COMPLETE``, and it already
receives the session key, the measured ``elapsed_ms`` and the turn's usage. So
the metric is emitted where the row is written, and the two can never disagree
about one turn.

**Outcome is passed in, never guessed.** The ``event`` argument those callers
pass is heterogeneous (an ``LLMEvent`` from the chat path, a bare ``TurnUsage``
from the helper sites), so a stop reason cannot be read off it reliably. Each
surface states its own outcome instead, and ``test_turn_duration_recorded.py``
holds every call site to that. Defaulting to ``ok`` would have been the harmful
alternative: it would bury exactly the failing background turns the metric is
being widened to expose.
"""

from __future__ import annotations

import logging

from kiro_crew.messaging.link import telemetry_channel_of
from kiro_crew.metrics.provider import get_recorder

logger = logging.getLogger(__name__)

# The stop-reason strings this mapping recognises, spelled here rather than
# imported from ``kiro_crew.acp.types``. Application code must not reach the ACP
# layer -- ``scripts/check_agent_sdk_boundary.py`` enforces that, and the
# baseline it ratchets can only shrink, so a metrics leaf is not the place to add
# an exemption. The file already spelled the clean-completion vocabulary
# ("end_turn" / "stop" / "completed") as literals for the same reason.
#
# Duplicating a wire constant is only safe with a guard, so
# ``test/metrics/test_turn_profile.py`` pins each of these against the ACP
# constant it mirrors: the test tree is outside the boundary gate's scope
# (``DEFAULT_TARGETS = ("src",)``), so the pin can import what this module may
# not. A change to the backend's vocabulary reddens there instead of silently
# reclassifying every turn of one kind as ``error``.
_STOP_CANCELLED = "cancelled"
_STOP_TOOL_STALL = "error: tool stall"
_STOP_STALE_RECOVER = "stale_recover"

#: The instrument name. ``dashboard/handlers/telemetry.py`` reads the same
#: constant, so emitter and reader cannot drift apart.
#:
#: ``metrics/provider.py``'s bucket table keeps its own literal, and that is not
#: an oversight: this module imports ``get_recorder`` from it, so having it import
#: the name back would be a cycle. That one duplicate is the cost of the import
#: direction, not a spelling nobody noticed.
TURN_METRIC = "kirocrew.turn.duration"

#: Per-turn token volume. ONE instrument with a ``direction`` attribute rather
#: than an ``.input``/``.output`` pair: the two series then carry an identical
#: attribute set by construction (a ``model`` added to one cannot be forgotten on
#: the other, which is how two counters drift into being un-joinable), the
#: dashboard's counter path already reports every attribute combination under
#: ``by_attr`` so both directions surface with no reader change, and ``direction``
#: is a two-value enum so the cardinality cost is exactly 2x rather than
#: unbounded.
TURN_TOKENS_METRIC = "kirocrew.turn.tokens"

#: Per-turn billed amount, in the unit the BACKEND bills in. Two instruments
#: rather than one with a ``currency`` attribute, because they are not the same
#: quantity: summing or percentile-ing credits together with dollars produces a
#: number with no unit, and a reader that must divide a histogram by attribute
#: before it means anything is a histogram in name only. Exactly one of the two
#: is non-zero for a given backend (``acp/types.py``: "Consumers read whichever
#: is non-zero"), so a host emits one of them, not both.
#:
#: NEITHER is a duration, which is why they are registered in
#: ``provider._HISTOGRAM_BUCKETS_BY_UNIT`` and reported by the dashboard under
#: unit-neutral keys — see both of those for the reasoning.
TURN_CREDITS_METRIC = "kirocrew.turn.credits"
TURN_COST_METRIC = "kirocrew.turn.cost_usd"

#: Outcome for a turn whose surface could not determine a stop reason at all.
#: Deliberately NOT ``unknown``: that label is in
#: ``telemetry._TERMINAL_FAULT_OUTCOMES``, so every clean background turn
#: reaching a helper site that passes a bare ``TurnUsage`` would have landed in
#: the fault-rate NUMERATOR and inflated the fault rate the moment this metric
#: was widened. It is also not folded into ``ok``, which would claim a success
#: nobody observed. It is its own slice, excluded from faults and visible in the
#: outcome breakdown, so the blind spot is a number an operator can see rather
#: than a guess in either direction.
OUTCOME_UNCLASSIFIED = "unclassified"


def turn_outcome(stop_reason: str | None, *, exhausted: bool = False) -> str:
    """Map a turn's stop reason to a low-cardinality outcome label.

    Single source of truth shared by every surface's emit and by the unit tests,
    so the mapping cannot drift from what the tests assert. Every label this
    metric can carry is returned from HERE, which is what lets
    ``test_telemetry_handler``'s drift gate harvest them by AST and prove each
    one is classified by the fault aggregator.

    ``None`` and ``""`` both mean a clean turn: the acp path leaves
    ``event.stop_reason`` unset on a normal completion, so this function must not
    read absence as failure. "This surface had no stop reason to GIVE" is a
    different statement, and it is not expressible here — a caller holding a bare
    ``TurnUsage`` cannot distinguish its missing attribute from a clean ``None``
    at this layer, so it passes :data:`OUTCOME_UNCLASSIFIED` itself.

    The two watchdog stop reasons are distinct outcomes, not ``error``: a
    stall-recovery turn is re-driven in place (its budget/outcome is tracked by
    ``kirocrew.watchdog.recovery.outcome``), so folding it into ``error`` would
    make the fault rate count every recovered stall as a fault AND hide the
    stall population the watchdog work exists to measure. Checked BEFORE the
    ``timeout`` substring so a stall never misclassifies.

    ``exhausted`` marks a stall turn whose recovery budget is already spent: the
    slot dies with "start a new chat", so the turn labels ``stall_exhausted`` — a
    terminal fault to the aggregator — keeping the recovered-stall exclusion
    from hiding dead sessions while ``fault_rate`` stays a single-series
    computation. Only the dashboard turn loop maintains such a budget; a
    background surface has no recovery loop, so it never passes this.

    A user cancel is its own outcome, NOT ``error``. It used to fold into the
    error branch, which put every press of Stop into the ``fault_rate``
    numerator: the one turn outcome the operator caused deliberately was being
    reported as the system failing. It is matched by EXACT equality against
    :data:`_STOP_CANCELLED` rather than a substring, because the watchdog's
    unacked-cancel reason (``"error: cancel unacked"``) is a genuine fault and
    must keep reaching the error branch.
    """
    s = stop_reason or ""
    if s in ("", "end_turn", "stop", "completed"):
        return "ok"
    if s == _STOP_TOOL_STALL or s == _STOP_STALE_RECOVER:
        if exhausted:
            return "stall_exhausted"
        return "tool_stall" if s == _STOP_TOOL_STALL else "stale_recover"
    if s == _STOP_CANCELLED:
        # Spelled as a literal, deliberately: test_telemetry_handler's drift gate
        # harvests this function's labels from its ``Return`` nodes by AST, and a
        # returned module constant is a ``Name`` it cannot read — the label would
        # silently escape the fault-classification gate.
        return "cancelled"
    if "timeout" in s:
        return "timeout"
    return "error"


def emit_turn_duration(
    duration_ms: int | float | None,
    *,
    session_key: str,
    outcome: str,
    elapsed_ms: int | float | None = None,
    model: str = "",
    provider: str = "",
) -> None:
    """Emit one ``kirocrew.turn.duration`` sample (best-effort, never raises).

    ``duration_ms`` is the provider-reported duration and ``elapsed_ms`` the
    locally measured wall clock; the first non-zero wins. Both are needed
    because the acp provider ALWAYS reports ``TurnUsage.duration_ms == 0``
    (nothing in the codebase assigns it — only claude_code fills it in), so a
    provider-only value silently skipped the emit for effectively all traffic
    and left turn latency / fault rate / throughput reading a flat 0.

    A still-zero duration skips the emit deliberately: an absent sample reads as
    "no data" on the Telemetry page, whereas a recorded 0 would render as a
    plausible-looking 0ms p50 — the very symptom that guard's misuse caused.

    ``session_source`` is derived with
    :func:`kiro_crew.messaging.link.telemetry_channel_of`, which exists for
    exactly this question ("who paid this cost") and returns a bounded label:
    the transport namespace for a channel key, a local label for the rest,
    ``other`` for a shape it does not recognise. Deliberately NOT
    ``validation.infer_use_case``, whose output also gates an artifact check
    (``handlers/artifacts.py``) — a metric must not be the reason an
    authorization-adjacent mapping grows a case.

    Caveat on what the wall clock measures: ``elapsed_ms`` runs from the start
    of the turn, so a turn parked on an interactive tool-approval prompt counts
    the operator's thinking time as turn duration. There is no finer-grained
    source on the acp path (the provider reports nothing at all), so this is the
    honest maximum available — but it means the histogram is "turn wall-clock",
    not pure model latency, and a high p90 can mean slow approvals rather than a
    slow model.

    ``model`` / ``provider`` ride along so latency can be read per model instead
    of as one pooled distribution over every model the host ran — a pool whose
    p90 moves when the model MIX moves and not when anything got slower. Both
    are omitted when empty rather than sent as ``""``, so a caller that cannot
    name them contributes to the un-split series instead of minting an
    empty-string label. See :func:`_model_attrs` for the cardinality argument.
    """
    value = duration_ms or elapsed_ms
    if not value:
        return
    attrs: dict = {"outcome": outcome or OUTCOME_UNCLASSIFIED}
    try:
        source = telemetry_channel_of(session_key)
        if source:
            attrs["session_source"] = source
    except Exception:
        pass
    attrs.update(_model_attrs(model, provider))
    try:
        get_recorder().histogram(TURN_METRIC, value, unit="ms", attrs=attrs)
    except Exception:
        logger.debug("turn metric emit failed", exc_info=True)


def _model_attrs(model: str, provider: str) -> dict:
    """The ``model`` / ``provider`` attribute pair, omitting whichever is empty.

    Low-cardinality by domain, not by hope: ``model`` is drawn from the host's
    CONFIGURED model set and ``provider`` from the backend enum, so both are
    enum-like values rather than free-form strings, which is what
    ``metrics/schema.py``'s cardinality contract requires of an attribute value.
    An empty one is dropped instead of sent, because ``model=""`` would publish a
    distinct series that reads as a real model with no name.
    """
    attrs: dict = {}
    if model:
        attrs["model"] = model
    if provider:
        attrs["provider"] = provider
    return attrs


def _positive(raw: object) -> float:
    """Coerce a usage field to a positive float, or 0.0.

    Usage arrives from provider payloads and from test doubles, so a field can be
    ``None``, a string, or absent. Non-positive is folded into 0.0 for two
    different reasons that happen to want the same answer: zero means "this
    backend does not bill in this dimension" (see the module docstring's
    non-zero gate), and a NEGATIVE value would be actively corrupting — a
    monotonic counter cannot take it back, and a negative sample would drag a
    histogram's mean below anything that ever happened.
    """
    try:
        val = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if val != val or val in (float("inf"), float("-inf")) or val <= 0:
        return 0.0
    return val


def emit_turn_usage(
    *,
    input_tokens: object = 0,
    output_tokens: object = 0,
    credits: object = 0.0,
    cost_usd: object = 0.0,
    model: str = "",
    provider: str = "",
) -> None:
    """Emit this turn's token / billing samples (best-effort, never raises).

    Companion to :func:`emit_turn_duration` and emitted by the same two owners
    that between them sample each turn exactly once (see this module's docstring
    and ``usage.persist_token_record_async``), so the four instruments describe
    one population and can be read against each other.

    **Every value is gated on being positive**, and that gate is the point rather
    than a micro-optimisation. Each backend fills only the dimensions it bills in
    and leaves the rest at 0 (``acp/types.py``: "Consumers read whichever is
    non-zero"): the kiro/acp backend fills ``credits``, the claude_code and
    bedrock backends fill ``cost_usd`` and the token counts. Emitting the zeros
    would publish a full series of them for every turn, and a recorded 0 does not
    read as "this backend does not bill here" — it reads as a measured zero, so
    the Telemetry page would report a host that never spends a cent and never
    moves a token. An absent series reads as absent, which is the truth.

    Attributes are deliberately just ``direction`` (tokens only) plus
    ``model``/``provider``. ``outcome`` and ``session_source`` are NOT carried:
    they exist on ``kirocrew.turn.duration``, which samples the same turns, so
    adding them here would multiply these series by two more dimensions to answer
    a question the duration instrument already answers.

    The four emits are written out rather than looped over a table of
    (name, value, unit) tuples. A loop reads shorter but hides each instrument's
    unit one indirection away from its name, and it also defeats the bucket guard
    in ``test_provider_bucket_views.py``, which finds histograms by reading the
    first argument of every ``histogram(...)`` call — a loop variable there is
    exactly how an unregistered histogram slips onto OTEL's default buckets.
    """
    attrs = _model_attrs(model, provider)
    tokens_in = _positive(input_tokens)
    tokens_out = _positive(output_tokens)
    credit_amount = _positive(credits)
    usd_amount = _positive(cost_usd)
    if tokens_in:
        try:
            get_recorder().counter(
                TURN_TOKENS_METRIC,
                tokens_in,
                unit="token",
                attrs={**attrs, "direction": "input"},
            )
        except Exception:
            logger.debug("turn input token metric emit failed", exc_info=True)
    if tokens_out:
        try:
            get_recorder().counter(
                TURN_TOKENS_METRIC,
                tokens_out,
                unit="token",
                attrs={**attrs, "direction": "output"},
            )
        except Exception:
            logger.debug("turn output token metric emit failed", exc_info=True)
    if credit_amount:
        try:
            get_recorder().histogram(TURN_CREDITS_METRIC, credit_amount, unit="credit", attrs=attrs)
        except Exception:
            logger.debug("turn credits metric emit failed", exc_info=True)
    if usd_amount:
        try:
            get_recorder().histogram(TURN_COST_METRIC, usd_amount, unit="usd", attrs=attrs)
        except Exception:
            logger.debug("turn cost metric emit failed", exc_info=True)
