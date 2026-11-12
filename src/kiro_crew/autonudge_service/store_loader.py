"""The loader: ``autonudge.json`` read back and vetted at the trust boundary.

The store is writable out of band -- edited by hand, carried from another host,
written by an auto-approved agent shell -- so every row ``AutoNudgeService._load``
reads is rebuilt, bounded, repaired or held aside here before any loop can arm: the
judge-field bounds, the addressing-field guard, the numeric and torn-write repairs,
and the banner and message scrub. The scrub helpers every other entrance shares
(``scrub_loop_text``, ``scrubbed_judge_spec`` and ``redact_store_value``) live here
too, so one module owns what a stored value may carry back into memory.

``_load`` is an ``AutoNudgeService`` method, bound on the class by name. It and its
helpers read three names through :mod:`kiro_crew.autonudge` at call time, because a
patch there must reach them: ``repair_sentinel_path``, which the facade defines,
``scrubbed_judge_spec`` and ``_bounded_judge_verdict``.
"""

from __future__ import annotations

import json
import logging
import math
import time
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from kiro_crew import autonudge_stop_log, validation
from kiro_crew.autonudge_service.gate import _MAX_QUIET_STREAK
from kiro_crew.autonudge_service.model import (
    _MAX_IDLE_SECS,
    _MIN_IDLE_SECS,
    INTERRUPTED_CYCLE_REASON,
    LOOP_BOUND_FIELDS,
    SENTINEL_DROPPED_REASON,
    STRUCTURAL_TERMINAL_REASON,
    NudgeLoop,
    is_structured_monitor_loop,
    new_goal_token,
    stored_bound,
)
from kiro_crew.autonudge_service.store import _locked_file, _quarantine_row_key, _rows_or_empty
from kiro_crew.autonudge_service.subject import _JUDGE_PR_SEEN_DIGEST_CHARS, _JUDGE_PR_SEEN_REMARKS
from kiro_crew.constants import MAX_BANNER_CHARS
from kiro_crew.monitoring.models import (
    MONITOR_STATE_VERSION,
    MONITOR_STOP_COMPLETION_UNAVAILABLE,
    MONITOR_STOP_SESSION_UNAVAILABLE,
    MONITOR_STOP_UNSUPPORTED_VERSION,
    MonitorDispatchResult,
    MonitorOutcome,
    monitor_state_from_dict,
    quarantine_monitor_state,
)
from kiro_crew.platform import PlatformCompositionError, redact_log_via_context, redact_via_context
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.autonudge import AutoNudgeService

logger = logging.getLogger("kiro_crew.autonudge")


#: Bounds on what a PERSISTED judge brief may hold once loaded. The store is
#: agent-writable, so these are what make the loader's retention bounded by this
#: code rather than by the file: an oversized nested map would otherwise be kept in
#: memory and rewritten on every persist for the life of the loop.
#: The brief's shape bounds have ONE spelling, in ``validation``, because that is
#: where the arming surface refuses an oversized brief. A copied literal here would
#: be a second number to keep in step, and the two would disagree silently: the
#: arming call would accept a brief this loader then trimmed.
_JUDGE_MAX_TARGETS = validation.MAX_JUDGE_TARGETS
_JUDGE_MAX_TARGET_CHARS = validation.MAX_JUDGE_TARGET_CHARS
_JUDGE_MAX_CRITERION_CHARS = validation.MAX_JUDGE_CRITERION_CHARS
_JUDGE_MAX_CURSORS = 16
_JUDGE_MAX_OUTCOME_CHARS = 32


def _bounded_judge_spec(raw: object, loop_id: object = None) -> dict:
    """A stored judge brief rebuilt from allowlisted keys with clipped values.

    Rebuilt rather than validated in place, so a key this code does not know is
    dropped instead of retained: that is what makes the size of what the loader
    keeps a property of this function rather than of the file it read.
    """
    from kiro_crew import autonudge as seams  # read at call time: the facade imports us

    if not isinstance(raw, dict):
        if raw not in (None, {}):
            logger.warning("AutoNudge: loop %s stored a non-object judge; ignoring it", loop_id)
        return {}
    out: dict = {}
    if validation.judge_is_off(raw):
        # The opt-out, and the one key this loader keeps that the arming validator
        # refuses from a caller: an owner spells it ``judge: false``, which normalises
        # to this marker, and the loader has to reload what was stored. Returned on its
        # own -- an opted-out loop has no criteria to carry, so pairing the marker with
        # a brief would describe a state no arming call can produce.
        return {validation.JUDGE_OFF_KEY: True}
    targets = raw.get("targets")  # noqa: F811 -- a local, not the probes.targets module
    if isinstance(targets, (list, tuple)):
        clean = [t for t in targets if isinstance(t, str) and t.strip()][:_JUDGE_MAX_TARGETS]
        if clean:
            out["targets"] = clean
    for key in ("wake_when", "quiet_when"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            out[key] = value
    return seams.scrubbed_judge_spec(out)


def scrubbed_judge_spec(spec: dict) -> dict:
    """A judge brief with every operator-authored string scrubbed, then clipped.

    The brief is free text that reaches the store on disk and the dashboard, and a
    length bound alone would keep a token verbatim. Called at EVERY entrance, because a
    brief from a tool call reaches the dashboard without passing the decode path.

    Scrub BEFORE clip: a redaction marker can be longer than the secret it replaces.
    ``targets`` get the same scrub; a clean forge URL passes it unchanged.
    """
    out: dict = dict(spec)
    for key in ("wake_when", "quiet_when"):
        value = out.get(key)
        if isinstance(value, str) and value:
            out[key] = scrub_loop_text(value)[:_JUDGE_MAX_CRITERION_CHARS]
    targets = out.get("targets")  # noqa: F811 -- a local, not the probes.targets module
    if isinstance(targets, (list, tuple)):
        out["targets"] = [
            (scrub_loop_text(t)[:_JUDGE_MAX_TARGET_CHARS] if isinstance(t, str) and t else t)
            for t in targets
        ]
    return out


def _bounded_judge_cursors(raw: object) -> dict:
    """Stored read cursors, keyed by bounded target name with whole-number values."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for key, value in raw.items():
        if len(out) >= _JUDGE_MAX_CURSORS:
            break
        if not isinstance(key, str) or not key.strip():
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            continue
        out[key[:_JUDGE_MAX_TARGET_CHARS]] = value
    return out


def _bounded_judge_pr_seen(raw: object) -> dict:
    """The stored pull-request baseline, reduced to a fixed-width digest and capped ids.

    The store is agent-writable, so the loader bounds whatever the file holds; unknown
    keys are dropped. Keeping less costs at most a repeat delivery: an id missing from
    the baseline reads as new and fires.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    digest = raw.get("digest")
    if isinstance(digest, str) and digest.strip():
        out["digest"] = digest.strip()[:_JUDGE_PR_SEEN_DIGEST_CHARS]
    ids: list[str] = []
    stored_ids = raw.get("remarks")
    for ident in stored_ids if isinstance(stored_ids, list) else []:
        if len(ids) >= _JUDGE_PR_SEEN_REMARKS:
            break
        if isinstance(ident, str) and ident.strip():
            ids.append(ident.strip()[:_JUDGE_MAX_TARGET_CHARS])
    out["remarks"] = ids
    return out


def _bounded_judge_verdict(raw: object) -> dict:
    """The stored previous verdict, reduced to its three bounded fields."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    outcome = raw.get("outcome")
    if isinstance(outcome, str) and outcome:
        out["outcome"] = outcome[:_JUDGE_MAX_OUTCOME_CHARS]
    for key in ("evidence_items", "at"):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if key == "evidence_items":
            # Local to keep the decisions graph off the gateway boot path.
            from kiro_crew.decisions.points import nudge_wake as point_nudge_wake

            # Bounded at BOTH ends: the store is agent-writable, and a 4,000-digit
            # negative is as long on the wire as a positive one. Below zero reads as none.
            if isinstance(value, int):
                out[key] = max(0, min(value, point_nudge_wake.MAX_EVIDENCE_ITEMS))
            elif math.isfinite(value):
                out[key] = max(0, min(value, point_nudge_wake.MAX_EVIDENCE_ITEMS))
            continue
        try:
            finite = math.isfinite(float(value))
        except OverflowError:
            finite = False
        if finite:
            out[key] = value
    return out


def _bounded_judge_recent(raw: object) -> list:
    """The stored labelled verdict history, each row rebuilt from allowlisted keys.

    Rebuilt rather than filtered, so a hand-edited row cannot carry text into the next
    request. A row with no outcome is dropped: it would only dilute the hit rate. The
    newest rows are kept when the list is longer than the window.

    An UNLABELLED delivered row is marked ``forfeited``: the gateway stopped before its
    turn completed, so no ``owner_acted`` can be read, and the next turn on the slot
    would be labelled in its place. ``delivered`` is kept, so the row still closes its
    own label cycle and its suppressions are not credited to a later delivery.
    """
    from kiro_crew import autonudge as seams  # read at call time: the facade imports us

    # The local import keeps the decisions graph off the gateway boot path.
    from kiro_crew.autonudge_judge import MAX_STORED_VERDICTS, MAX_VERDICT_ID_CHARS

    if not isinstance(raw, list):
        return []
    out: list = []
    discarded = 0
    for index, item in enumerate(reversed(raw)):
        if len(out) >= MAX_STORED_VERDICTS:
            discarded = len(raw) - index
            break
        if not isinstance(item, dict):
            continue
        row = seams._bounded_judge_verdict(item)
        if "outcome" not in row:
            continue
        delivered = item.get("delivered") is True
        suppressed = item.get("suppressed") is True
        owner_acted = item.get("owner_acted")
        if delivered and isinstance(owner_acted, bool):
            row["owner_acted"] = owner_acted
        missed = item.get("missed")
        if suppressed and isinstance(missed, bool):
            row["missed"] = missed
        for key in ("suppressed", "answered"):
            value = item.get(key)
            if isinstance(value, bool):
                row[key] = value
        if delivered:
            row["delivered"] = True
            if "owner_acted" not in row:
                row["forfeited"] = True
        row_id = item.get("id")
        if isinstance(row_id, str) and row_id.strip():
            row["id"] = row_id.strip()[:MAX_VERDICT_ID_CHARS]
        out.append(row)
    if discarded:
        logger.debug(
            "AutoNudge: discarded %d older judge verdict history row(s) beyond the %d-row cap",
            discarded,
            MAX_STORED_VERDICTS,
        )
    out.reverse()
    return out


#: Fields a client ADDRESSES a row by, so the REST scrub exempts them -- which is
#: only safe because ``_load`` refuses a row whose value here is credential-shaped.
ADDRESSING_FIELDS = frozenset({"id", "slot_key"})


def _addressing_value_unsafe_why(got: object) -> str:
    """Why the load guard holds a row aside for THIS addressing value, or ``""``.

    Shared by the load guard and the repair matcher: a laxer copy in the matcher would
    retire a held row against a loop the guard never accepted.
    """
    if not isinstance(got, str):
        return "is not a string"
    if not got.isprintable():
        return "contains a non-printable character"
    if redact_via_context(got) != got:
        return "is credential-shaped"
    return ""


def scrub_loop_text(value: Any) -> Any:
    """Credential-scrub one serialized ``NudgeLoop`` field value.

    ``None`` passes through untouched, because ``str(None)`` would turn an absent value
    into a message that reads like content. Everything else is scrubbed through
    ``platform.redact_via_context``, coerced with ``str()`` first when not already a
    string -- coerced rather than blanked so the operator can still see the bad row.
    """
    if value is None:
        return value
    if isinstance(value, str):
        if not value:
            return value
        return redact_via_context(value)
    return redact_via_context(str(value))


def redact_store_value(value: object) -> str:
    """Render a store-sourced value safe for a log line in this module.

    ``repr`` escapes newlines and ANSI sequences that could forge a second log record.
    ``redact_log_via_context`` scrubs without raising, which matters because several
    callers sit inside ``except`` arms that must never raise.
    """
    return redact_log_via_context(repr(value))


def _is_torn_deactivation(loop: NudgeLoop) -> bool:
    """Whether a persisted row is inactive without any stop having been recorded.

    Every stop this service performs leaves a ``stopped_reason`` or a terminal monitor
    ``outcome`` and clears ``next_due_ts``. A row inactive with neither mark and a live
    deadline is a torn write, which ``_load`` resumes. A monitor with a wake in flight is
    left to the claim-recovery branches.

    The row must still carry its kill-switch path: a sentinel-drop repair without
    ``SENTINEL_DROPPED_REASON`` stored this shape with an empty sentinel, and that stop
    was a fail-closed refusal, so an empty sentinel stays inactive (the dashboard
    revives it).
    """
    if loop.active or loop.stopped_reason or loop.next_due_ts <= 0:
        return False
    if not loop.stop_sentinel_path:
        return False
    state = loop.monitor
    if state is None:
        return True
    return (
        state.version == MONITOR_STATE_VERSION
        and state.outcome is None
        and not state.wake_in_flight
    )


def _repair_number(
    value: Any, *, lo: float, fallback: float, hi: float | None = None
) -> tuple[float, bool]:
    """Coerce a persisted numeric field to a FINITE value within [lo, hi].

    Returns ``(repaired_value, was_repaired)``. Non-numeric, non-finite
    (``1e309`` parses to ``inf``, which json.dump would emit as invalid
    ``Infinity``), and out-of-range inputs all repair rather than raise, so a
    corrupt store entry can never abort gateway startup or poison the JSON
    the REST/WS surface emits.
    """
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: JSON integers are arbitrary-precision, so a persisted
        # 10**400 converts to float by raising rather than returning inf —
        # without this arm the error would escape to _load()'s per-entry
        # handler, which SKIPS the loop and lets the next persist delete it.
        return fallback, True
    if math.isnan(num) or math.isinf(num):
        return fallback, True
    clamped = max(lo, num) if hi is None else max(lo, min(hi, num))
    return clamped, clamped != num


def _load(self: AutoNudgeService) -> None:
    """Read the store and repair each entry. BLOCKING — see ``start()``.

    Does file I/O (locked read) and, via ``repair_sentinel_path``, realpath
    resolution that can stall on an unavailable network mount, so callers on
    the event loop MUST offload this (``no-blocking-call-on-event-loop``).
    """
    from kiro_crew import autonudge as seams  # read at call time: the facade imports us

    with _locked_file(self._path, "r") as fh:
        data = json.load(fh)
    # Reset per load: a re-read must not inherit a refusal from a prior one.
    self._store.load_refused = False
    # Prior quarantine is re-read and kept HELD: repairing the offending field is
    # not enough on its own, because the sidecar must not be the only durable copy.
    self._store.quarantined = []
    # And the ownership set with it: it is the LICENCE to remove, so a key surviving a
    # pass the row did not lets compaction delete a row a peer wrote afterwards.
    self._store.sidecar_seen = set()
    # Re-read per load: a row repaired between loads must stop being carried.
    self._store.unparsed_rows = []
    # No turn is in flight across a load, so no claim can be owed.
    self._store.delivering_claim = {}
    # Re-derived below from each row's own marker, never carried across a load.
    self._store.undelivered_claim = set()
    # The sidecar is the SINGLE durable location. Held-aside rows are deliberately
    # not embedded in the store too -- two copies of one state can disagree.
    prior_quarantined = self._store.read_quarantine_sidecar()
    # Arming while writes are refused is worse than arming nothing: a delivered cycle
    # cannot persist its counter, so a restart re-fires it past its own cycle cap.
    if self._store.load_refused:
        logger.warning(
            "AutoNudge: arming no loops — the quarantine sidecar at %s could not be "
            "read, so a delivered cycle could not record itself. Fix the file and "
            "restart.",
            self._store.quarantine_path,
        )
        return
    # Probe the ACTIVE credential policy ONCE, before the row loop: inside it the
    # per-row ``except`` swallows a composition failure and misreports the defect.
    try:
        redact_via_context("")
    except PlatformCompositionError:
        self._store.load_refused = True
        logger.error(
            "AutoNudge: refusing to arm any loop — this host declares a credential "
            "policy it could not compose, so a persisted addressing field cannot be "
            "vetted. The store is left untouched (writes are refused while this "
            "holds); fix the host and restart.",
            exc_info=True,
        )
        return
    # The list guard below cannot see a NON-DICT root: ``"loops" in []`` is False, so
    # a hand-edited ``[]`` or bare number reached the row loop and aborted boot.
    if not isinstance(data, dict):
        self._store.load_refused = True
        logger.error(
            "AutoNudge: refusing to arm any loop — the store at %s holds %s at its "
            "root instead of an object, so no row can be read. Writes are refused (a "
            "write would delete them); fix the file and restart.",
            self._path,
            type(data).__name__,
        )
        return
    # PRESENT-BUT-NOT-A-LIST is corruption, not an empty store: read as empty, the
    # next mutation replaces the file and deletes every row it held. ABSENT is legal.
    if "loops" in data and not isinstance(data["loops"], list):
        self._store.load_refused = True
        logger.error(
            "AutoNudge: refusing to arm any loop — the store at %s carries %s under "
            "'loops' instead of a list, so its rows cannot be enumerated. Writes are "
            "refused (a write would delete them); fix the file and restart.",
            self._path,
            type(data["loops"]).__name__,
        )
        return
    store_rows = _rows_or_empty(data.get("loops"))
    # HELD, NEVER ARMED: arming a held-aside row made the sidecar the only durable
    # copy of a live row, so a failed compaction plus a delete left it to re-arm.
    for raw in prior_quarantined:
        self._store.quarantined.append(deepcopy(raw))
        # Accounted for by THIS instance, so compaction may later drop it once repaired.
        # A row a peer adds after this read stays outside the set, and so survives.
        self._store.sidecar_seen.add(_quarantine_row_key(raw))
        logger.warning(
            "autonudge: not arming held-aside loop %s -- held rows are kept for "
            "repair, never armed; move the repaired row into %s to arm it",
            redact_store_value(raw.get("id") if isinstance(raw, dict) else None),
            self._path,
        )
    for raw in store_rows:
        try:
            loop_values = {
                key: raw[key]
                for key in raw
                if key in NudgeLoop.__dataclass_fields__ and key != "monitor"
            }
            # A non-bool ``gate`` (the STRING "false" is truthy; ``null``) normalises
            # to FALSE: gating can silently STOP a loop, while ungated only costs a
            # turn per interval. Only an explicit boolean true gates.
            if "gate" in loop_values and not isinstance(loop_values["gate"], bool):
                logger.warning(
                    "AutoNudge: loop %s stored a non-boolean gate (%r); leaving it ungated",
                    raw.get("id"),
                    loop_values["gate"],
                )
                loop_values["gate"] = False
            # Judge fields come from the same agent-writable store: a non-numeric
            # streak would defeat the floor on how long a judge keeps a loop quiet,
            # and an oversized map would be re-serialized on every persist. Each map
            # is rebuilt from allowlisted keys with clipped values.
            if "judge" in loop_values:
                loop_values["judge"] = _bounded_judge_spec(loop_values["judge"], raw.get("id"))
            if "judge_cursors" in loop_values:
                loop_values["judge_cursors"] = _bounded_judge_cursors(loop_values["judge_cursors"])
            if "judge_last_verdict" in loop_values:
                loop_values["judge_last_verdict"] = seams._bounded_judge_verdict(
                    loop_values["judge_last_verdict"]
                )
            if "judge_recent_verdicts" in loop_values:
                loop_values["judge_recent_verdicts"] = _bounded_judge_recent(
                    loop_values["judge_recent_verdicts"]
                )
            if "judge_pr_seen" in loop_values:
                loop_values["judge_pr_seen"] = _bounded_judge_pr_seen(loop_values["judge_pr_seen"])
            # Only an explicit boolean true owes a turn. This one resolves the other
            # way from ``gate``: a corrupt value here costs at most one extra fire,
            # while reading it as owed on every load would make a loop fire forever
            # without ever consulting its judge.
            if "judge_wake_pending" in loop_values and not isinstance(
                loop_values["judge_wake_pending"], bool
            ):
                logger.warning(
                    "AutoNudge: loop %s stored a non-boolean judge_wake_pending (%r); "
                    "treating the wake as delivered",
                    raw.get("id"),
                    loop_values["judge_wake_pending"],
                )
                loop_values["judge_wake_pending"] = False
            if "judge_quiet_streak" in loop_values:
                _streak = loop_values["judge_quiet_streak"]
                if isinstance(_streak, bool) or not isinstance(_streak, int) or _streak < 0:
                    logger.warning(
                        "AutoNudge: loop %s stored a non-count judge streak; resetting it",
                        raw.get("id"),
                    )
                    loop_values["judge_quiet_streak"] = 0
                else:
                    loop_values["judge_quiet_streak"] = min(_streak, _MAX_QUIET_STREAK)
            # ``self_armed`` relaxes the crew/member fire-time guard and the store is
            # agent-writable, so a non-boolean normalises to the REFUSING value.
            if "self_armed" in loop_values and not isinstance(loop_values["self_armed"], bool):
                logger.warning(
                    "AutoNudge: loop %s stored a non-boolean self_armed (%s); "
                    "treating it as externally armed",
                    redact_store_value(raw.get("id")),
                    redact_store_value(loop_values["self_armed"]),
                )
                loop_values["self_armed"] = False
            # ``default_patrol`` only ever WIDENS what an arm on the same slot may
            # displace, so an unreadable value resolves to the narrower False.
            if "default_patrol" in loop_values and not isinstance(
                loop_values["default_patrol"], bool
            ):
                loop_values["default_patrol"] = False
            # ``config_generation`` feeds ``+= 1`` and an equality fence; a ``null``,
            # string or negative would raise mid-mutation. Reset to 0, which can only
            # miss a stop (the safe direction), never cause a wrong one.
            if "config_generation" in loop_values:
                _cg = loop_values["config_generation"]
                if not isinstance(_cg, int) or isinstance(_cg, bool) or _cg < 0:
                    logger.warning(
                        "AutoNudge: loop %s stored a non-int/negative "
                        "config_generation (%r); resetting to 0",
                        raw.get("id"),
                        _cg,
                    )
                    loop_values["config_generation"] = 0
            loop = NudgeLoop(**loop_values)
            # Rotated on EVERY load: a human may have hand-edited the goal while we
            # were down, so a pre-restart token must not authorise overwriting it.
            loop.goal_token = new_goal_token()
            # Owed in BOTH arms: a cap applied later is refused against ``cycle_count``, so
            # committing an unconfirmed cycle is not inert even on an uncapped loop.
            inflight = raw.get("inflight_cycle")
            if isinstance(inflight, bool):
                # ``isinstance(True, int)`` is TRUE, so a stored boolean would read as
                # cycle 1 and spend a phantom cycle that no turn ever claimed.
                logger.warning(
                    "AutoNudge: loop %s stored a non-integer cycle claim (%s); ignoring it",
                    redact_store_value(raw.get("id")),
                    redact_store_value(inflight),
                )
                inflight = None
                self._store_dirty = True
            if (
                isinstance(inflight, int)
                and inflight > loop.cycle_count
                and raw.get("inflight_undelivered") is True
            ):
                # The writer knew this turn never went out: nothing is owed, so the
                # claim is released here and the loop keeps its stored state.
                logger.info(
                    "AutoNudge: loop %s restored an undelivered claim on cycle %d; releasing it",
                    redact_store_value(raw.get("id")),
                    inflight,
                )
                self._store_dirty = True
            elif isinstance(inflight, int) and inflight > loop.cycle_count:
                # The claim reaches disk BEFORE the turn it claims, so an unresolved one
                # cannot say whether the reader already received that turn.
                logger.warning(
                    "AutoNudge: loop %s was interrupted mid-delivery on cycle %d — it is "
                    "owed, not spent, and is held stopped until re-activated",
                    redact_store_value(raw.get("id")),
                    inflight,
                )
                self._store.unreconciled_claim[loop.id] = inflight
                # A reason the UI can render: without it a routine restart mid-fire
                # surfaced as an unexplained pause with only a log line behind it.
                # Stamped only on a row this load stops itself, so a pause saved
                # during the fire (``manual`` or any other reason) keeps its own.
                if loop.active or not loop.stopped_reason:
                    loop.stopped_reason = INTERRUPTED_CYCLE_REASON
                loop.active = False
                self._store_dirty = True
            # TRUST BOUNDARY: the REST scrub exempts these fields, so a credential
            # placed in one reaches every client verbatim. REFUSED, never scrubbed.
            unsafe_field = None
            unsafe_why = ""
            for name in sorted(ADDRESSING_FIELDS):
                why = _addressing_value_unsafe_why(getattr(loop, name, None))
                if why:
                    unsafe_field, unsafe_why = name, why
                    break
            if unsafe_field is not None:
                logger.warning(
                    "AutoNudge: refusing loop %s — its %s %s and addressing fields are "
                    "served unscrubbed; fix the store entry",
                    redact_store_value(loop.id),
                    unsafe_field,
                    unsafe_why,
                )
                # QUARANTINE rather than refuse the store: siblings keep running while
                # the offending row is held on disk for repair instead of deleted.
                key = _quarantine_row_key(raw)
                if key not in {_quarantine_row_key(r) for r in self._store.quarantined}:
                    self._store.quarantined.append(deepcopy(raw))
                self._store_dirty = True
                continue
            if "monitor" in raw:
                monitor_raw = raw["monitor"]
                monitor_quarantined = False
                try:
                    loop.monitor = monitor_state_from_dict(monitor_raw)
                except (TypeError, ValueError):
                    monitor_quarantined = True
                    quarantine_needs_rewrite = loop.active or loop.next_due_ts != 0.0
                    loop.monitor = quarantine_monitor_state(monitor_raw)
                    loop.active = False
                    loop.next_due_ts = 0.0
                    self._store_dirty = self._store_dirty or quarantine_needs_rewrite
                    logger.warning(
                        "AutoNudge: quarantined malformed monitor record for loop %s",
                        loop.id,
                        exc_info=True,
                    )
                if (
                    "gate" not in raw
                    and loop.monitor.version == MONITOR_STATE_VERSION
                    and not monitor_quarantined
                ):
                    # A record with inferred observation state but no ``gate`` is a
                    # plain ungated loop, not a typed controller record.
                    loop.monitor = None
                    self._store_dirty = True
                if loop.monitor is None:
                    pass
                elif loop.monitor.version != MONITOR_STATE_VERSION:
                    # A newer policy version is not interpreted. ``active`` is kept so
                    # an upgrade resumes the legacy watch; only a structured
                    # controller record is retired as unschedulable.
                    loop.monitor.outcome = MonitorOutcome.BLOCKED
                    loop.monitor.stopped_reason = MONITOR_STOP_UNSUPPORTED_VERSION
                    if is_structured_monitor_loop(loop):
                        if loop.active or loop.next_due_ts:
                            self._store_dirty = True
                        loop.active = False
                        loop.next_due_ts = 0.0
                elif loop.monitor.outcome is not None:
                    # A terminal record is inspectable, never schedulable,
                    # even when a hand-edited store contradicts itself.
                    if loop.active or loop.monitor.wake_in_flight or loop.next_due_ts:
                        self._store_dirty = True
                    loop.active = False
                    loop.monitor.wake_in_flight = False
                    loop.monitor.completion_evidence_deadline = 0.0
                    loop.next_due_ts = 0.0
                elif is_structured_monitor_loop(loop) and loop.monitor.wake_in_flight:
                    if (
                        loop.monitor.wake_delivery is None
                        and loop.monitor.completion_evidence_deadline > 0
                    ):
                        # An evidence deadline with no delivery marker is recovered
                        # as dispatched, so the expiry path owns the claim.
                        loop.monitor.wake_delivery = MonitorDispatchResult.DISPATCHED
                        self._store_dirty = True
                    if (
                        loop.monitor.wake_delivery is MonitorDispatchResult.BUSY
                        and loop.next_due_ts > 0
                    ):
                        # BUSY proves no action turn started: resume the claimed wake
                        # at its persisted retry.
                        if loop.monitor.next_probe_at != loop.next_due_ts:
                            loop.monitor.next_probe_at = loop.next_due_ts
                            self._store_dirty = True
                    elif loop.monitor.completion_evidence_deadline <= 0:
                        # A persisted claim with no accepted-dispatch
                        # deadline may have died on either side of handoff.
                        # Retire it without charging or redispatching.
                        loop.monitor.wake_in_flight = False
                        if loop.monitor.outcome is None:
                            loop.monitor.outcome = MonitorOutcome.BLOCKED
                            loop.monitor.stopped_reason = MONITOR_STOP_COMPLETION_UNAVAILABLE
                        loop.active = False
                        loop.next_due_ts = 0.0
                        self._store_dirty = True
                    elif loop.next_due_ts != loop.monitor.completion_evidence_deadline:
                        loop.next_due_ts = loop.monitor.completion_evidence_deadline
                        loop.monitor.next_probe_at = loop.next_due_ts
                        self._store_dirty = True
                elif (
                    is_structured_monitor_loop(loop)
                    and loop.active
                    and self._on_monitor_tick is None
                    and self._on_fire is not None
                ):
                    # No controller is wired here, so deactivate rather than let the
                    # legacy timer inject the prompt without a typed decision.
                    loop.active = False
                    loop.monitor.outcome = MonitorOutcome.BLOCKED
                    loop.monitor.stopped_reason = MONITOR_STOP_SESSION_UNAVAILABLE
                    loop.monitor.stopped_at = time.time()
                    loop.next_due_ts = 0.0
                    self._store_dirty = True
                # A current, un-claimed, unsettled monitor re-arms like any other
                # loop: the gate in _monitor_tick_is_quiet decides every tick, and
                # deactivating would silently end every watch at a restart.
            # Re-validate the kill-switch path (see repair_sentinel_path). INSIDE
            # the per-entry try: a malformed entry is skipped, never aborts start().
            repaired = seams.repair_sentinel_path(loop.stop_sentinel_path)
            # Numeric timer fields drive arm-time arithmetic and are emitted as JSON,
            # so each is repaired per field to finite and in range (a 1e309 deadline
            # parses to ``inf``, which json.dump emits as invalid ``Infinity``).
            loop.next_due_ts, due_repaired = _repair_number(loop.next_due_ts, lo=0.0, fallback=0.0)
            idle_num, idle_repaired = _repair_number(
                loop.idle_secs,
                lo=float(_MIN_IDLE_SECS),
                hi=float(_MAX_IDLE_SECS),
                fallback=float(_MIN_IDLE_SECS),
            )
            loop.idle_secs = int(idle_num)
            # ``consecutive_start_failures`` meets ``>=`` on every wake; a string or
            # ``null`` would raise inside ``_timer`` and the loop would never fire again.
            streak_num, streak_repaired = _repair_number(
                loop.consecutive_start_failures, lo=0.0, fallback=0.0
            )
            loop.consecutive_start_failures = int(streak_num)
            if streak_repaired:
                self._store_dirty = True
            # Same for ``cycle_count`` (vs the cap) and ``created_ts`` (vs the clock).
            # Repaired to 0, which every budget reader treats as "nothing to measure
            # from", rather than a guess that could stop a healthy loop.
            count_num, count_repaired = _repair_number(loop.cycle_count, lo=0.0, fallback=0.0)
            loop.cycle_count = int(count_num)
            loop.created_ts, created_repaired = _repair_number(
                loop.created_ts, lo=0.0, fallback=0.0
            )
            failed_num, failed_repaired = _repair_number(
                loop.consecutive_failed_cycles, lo=0.0, fallback=0.0
            )
            loop.consecutive_failed_cycles = int(failed_num)
            if count_repaired or created_repaired or failed_repaired:
                self._store_dirty = True
            # The BOUNDS are limits the user typed, never repaired to a number nobody
            # chose (0 would remove a cost limit). A lossless integer form (``"24"``,
            # ``24.0``) is kept as that int. Any other value cannot be honoured: an
            # active row stops under ``structural_terminal``, the value is kept for
            # inspection, and ``update`` declines a revival until a fresh bound.
            unreadable_bound = False
            for bound_field in LOOP_BOUND_FIELDS:
                stored = getattr(loop, bound_field)
                bound = stored_bound(stored)
                if bound is None:
                    unreadable_bound = True
                    logger.warning(
                        "AutoNudge: loop %s has an unreadable %s (%s) — %s",
                        loop.id,
                        bound_field,
                        redact_store_value(stored),
                        "deactivating it" if loop.active else "it stays stopped",
                    )
                elif type(stored) is not int or bound != stored:
                    setattr(loop, bound_field, bound)
                    self._store_dirty = True
            if unreadable_bound:
                # Same shape as the dropped-sentinel stop below: only a row this
                # branch deactivates gets the reason, and the schedule is
                # cleared so the torn-write recovery cannot resume it.
                if loop.active:
                    loop.active = False
                    loop.stopped_reason = STRUCTURAL_TERMINAL_REASON
                loop.next_due_ts = 0.0
                if loop.monitor is not None:
                    loop.monitor.next_probe_at = 0.0
                self._store_dirty = True
            if (
                loop.monitor is not None
                and loop.monitor.version == MONITOR_STATE_VERSION
                and loop.monitor.next_probe_at != loop.next_due_ts
            ):
                # NudgeLoop owns the restart schedule; the monitor field is
                # its atomically-persisted inspection mirror.
                loop.monitor.next_probe_at = loop.next_due_ts
                self._store_dirty = True
            if due_repaired or idle_repaired:
                self._store_dirty = True
            notification_stopped_at, notification_time_repaired = _repair_number(
                loop.terminal_notification_stopped_at,
                lo=0.0,
                fallback=0.0,
            )
            loop.terminal_notification_stopped_at = notification_stopped_at
            notification_outcome = loop.terminal_notification_outcome
            valid_notification_outcomes = {item.value for item in MonitorOutcome}
            if (
                not isinstance(notification_outcome, str)
                or notification_outcome not in valid_notification_outcomes
            ):
                if notification_outcome or notification_stopped_at or notification_time_repaired:
                    self._store_dirty = True
                loop.terminal_notification_outcome = ""
                loop.terminal_notification_stopped_at = 0.0
            elif notification_time_repaired:
                self._store_dirty = True
            if (
                loop.monitor is not None
                and loop.monitor.outcome is not None
                and loop.monitor.terminal_notification_delivered
                and not loop.terminal_notification_outcome
            ):
                loop.terminal_notification_outcome = loop.monitor.outcome.value
                loop.terminal_notification_stopped_at = loop.monitor.stopped_at
                self._store_dirty = True
            # ``banner`` is ``.strip()``ed on the fire path, so a non-string raises
            # and the loop rearms forever without delivering. Repaired and persisted
            # so a hand-edited store is corrected once.
            if not isinstance(loop.banner, str):
                logger.warning(
                    "AutoNudge: loop %s had a non-string banner (type %s) — treating it "
                    "as absent; the transcript row falls back to the full message",
                    loop.id,
                    type(loop.banner).__name__,
                )
                loop.banner = ""
                self._store_dirty = True
            elif loop.banner:
                # SCRUB the banner: the store is writable out of band (a hand-edited
                # ``autonudge.json``, a direct ``svc.add``) and the loop is served RAW
                # by ``GET /api/autonudge``. Redact BEFORE any slice, so a secret
                # straddling the cap is masked whole. A banner over the cap before OR
                # after redaction is BLANKED, as the write path would have rejected it.
                scrubbed, _ = redact_exfiltration_urls(loop.banner)
                scrubbed, _ = redact_credentials(scrubbed)
                if len(loop.banner) > MAX_BANNER_CHARS or len(scrubbed) > MAX_BANNER_CHARS:
                    scrubbed = ""
                if scrubbed != loop.banner:
                    loop.banner = scrubbed
                    self._store_dirty = True
            # SCRUB ``message`` the same way, but redaction ONLY, never
            # blank-on-length: it is the model's payload and has no fallback row.
            if isinstance(loop.message, str) and loop.message:
                scrubbed_msg, _ = redact_exfiltration_urls(loop.message)
                scrubbed_msg, _ = redact_credentials(scrubbed_msg)
                if scrubbed_msg != loop.message:
                    loop.message = scrubbed_msg
                    self._store_dirty = True
            if _is_torn_deactivation(loop):
                # INACTIVE, NO stop reason, deadline still LIVE: no stop path makes
                # this shape, so a write outside them flipped it. Left alone, nothing
                # would ever arm it again. Resume it; its schedule and kill switch are
                # still on the row.
                logger.warning(
                    "AutoNudge: loop %s was inactive with no stop reason and a live "
                    "deadline — resuming it; a paused loop records a reason",
                    loop.id,
                )
                loop.active = True
                self._store_dirty = True
        except Exception:
            logger.warning(
                "AutoNudge: skipping malformed loop entry: %s",
                redact_store_value(raw),
                exc_info=True,
            )
            # Without this, a sibling flagging the store dirty makes the rewrite delete
            # this row permanently -- the warning above is then its only other trace.
            self._store.unparsed_rows.append(raw)
            continue
        self._loops[loop.id] = loop
        if repaired != loop.stop_sentinel_path:
            dropped = bool(loop.stop_sentinel_path) and not repaired
            loop.stop_sentinel_path = repaired
            if dropped:
                # FAIL CLOSED, matching the arm-time contract:
                # authorize_and_add_nudge REFUSES to arm a loop whose
                # sentinel is sensitive, so a persisted loop whose sentinel
                # has become sensitive must not be re-armed with no kill
                # switch at all. Deactivating leaves it inspectable and
                # restartable rather than silently unstoppable-by-file.
                logger.warning(
                    "AutoNudge: deactivating loop %s — its stop sentinel was dropped",
                    loop.id,
                )
                # Record WHY and clear the schedule, like every other stop:
                # a reasonless inactive row with a live deadline is the
                # torn-write shape ``_is_torn_deactivation`` resumes on the
                # next boot, which would undo this refusal. Only a row THIS
                # branch deactivates gets the stamp: a row already paused
                # keeps the reason its own stop recorded (a manual pause
                # relabelled ``sentinel_dropped`` would become re-armable).
                if loop.active:
                    loop.active = False
                    loop.stopped_reason = SENTINEL_DROPPED_REASON
                loop.next_due_ts = 0.0
                if loop.monitor is not None:
                    loop.monitor.next_probe_at = 0.0
            self._store_dirty = True
    # Stop-record baseline: rows ACTIVE on disk, taken before repair so a loop this
    # load deactivates (a cycle interrupted by the restart) is recorded when the
    # repair persists -- but only rows this load ACCEPTED. A held-aside or unparsed
    # row is not served, so it must not be reported as removed either.
    with self._store.commit_lock:
        self._store.committed_active = {
            loop_id: summary
            for loop_id, summary in autonudge_stop_log.active_summaries(store_rows).items()
            if loop_id in self._loops
        }
    logger.info("AutoNudge: loaded %d loops", len(self._loops))
