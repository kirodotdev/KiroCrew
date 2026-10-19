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

    The brief is free text somebody writes at arm time, and it reaches two places a
    credential must not: the loop store on disk, and the loop serialization the
    dashboard reads. Bounding a SHAPE is not scrubbing a VALUE -- an allowlist of keys
    with a length limit keeps a criterion naming a bearer token verbatim, merely
    shorter -- so the scrub is its own step and this is where it lives.

    Called at EVERY entrance, not once at the store boundary, because the entrances are
    independent: a brief that arrives on a tool call is serialized to the dashboard
    without passing the decode path, so a scrub applied only on decode covers the one
    entrance that already survived a round trip.

    Scrub BEFORE clip, in that order: a redaction marker can be longer than the secret
    it replaces, so clipping first lets a scrubbed value land over the bound.

    ``targets`` are scrubbed on the same rule. A clean forge URL passes
    ``redact_via_context`` unchanged, so no watch can be broken by this, and a target
    this alters is one carrying the credential the scrub exists for.
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

    The write path clips this value, but a clip on the way out constrains only what THIS
    build wrote: the store is writable by an auto-approved agent shell, so the bound that
    matters is the one the loader applies to whatever the file actually holds. Unknown
    keys are dropped rather than carried, so a row that grew a field is not retained and
    re-serialized for the life of the loop.

    A rejected or over-long id list costs at most a repeat delivery -- an id absent from
    the baseline reads as new again, which fires -- so every uncertain case here
    resolves by keeping less.
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

            # Bounded at BOTH ends: the ceiling keeps the state inside its character
            # budget, and the floor matters just as much, because this store is
            # agent-writable and a 4,000-digit negative is as long on the wire as a
            # positive one. A count below zero is not a count, so it reads as none.
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

    Rebuilt rather than filtered, so a row an operator hand-edited into the store
    cannot carry text into the next request: the point screens what it sends, and this
    is the matching refusal on the way IN. A row naming no outcome is dropped -- it
    labels nothing and would only dilute the hit rate the judge reads.

    The newest rows are kept when the stored list is longer than the window, because a
    file written by a build with a wider window must not push this one's window into
    the past.

    An UNLABELLED delivered row is marked ``forfeited`` here, which is the one field
    this function adds rather than carrying across. Such a row belongs to a turn this
    process never saw: the fire went out, then the gateway stopped before the turn
    completed, so no ``owner_acted`` can ever be read for it, and the next turn on that
    slot -- quite possibly the owner's own -- would be labelled in its place. The flag
    refuses the label while KEEPING ``delivered``, so the row still closes its own label
    cycle and the suppressions behind it are not credited to a later delivery. A
    re-owed delivery gets a newer undecided row; the fire path stamps that row while
    this boundary remains unchanged.
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

    ONE definition, shared by the guard that refuses a row and the matcher that decides a
    repair superseded it. Two copies could disagree, and a matcher with a laxer notion of
    unsafe would retire a held row against a loop the guard never accepted.
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

    ``repr`` supplies the ESCAPE: a store value can carry a newline or an ANSI
    sequence, and a raw ``%r``/``%s`` would let it forge a second log record.

    The SCRUB is delegated to ``redact_log_via_context``, which already owns exactly
    this contract -- context-aware redaction for a log line that must not raise. That
    matters because several callers sit inside ``except`` arms whose documented job is
    to never raise.
    """
    return redact_log_via_context(repr(value))


def _is_torn_deactivation(loop: NudgeLoop) -> bool:
    """Whether a persisted row is inactive without any stop having been recorded.

    Every deactivation this service performs leaves one of two marks: a
    non-empty ``stopped_reason`` on the loop (``update`` defaults to
    ``"manual"``; the timer bounds, the terminal settlement and the
    sentinel-drop repair write theirs) or a terminal ``outcome`` on a monitor
    record — and all of them clear ``next_due_ts``. A row that is inactive with
    NEITHER mark and a deadline still set was therefore not stopped by this
    service; ``_load`` resumes it. A monitor whose wake is in flight is left to
    the claim-recovery branches, which own that state.

    The row must still carry its kill-switch path. A sentinel-drop repair that
    predates ``SENTINEL_DROPPED_REASON`` persisted this very shape MINUS the
    sentinel (it blanked the path and recorded nothing), and that stop was a
    fail-closed refusal to run without a kill switch -- so an empty sentinel is
    the one persisted discriminator between a torn write and that refusal, and
    the ambiguous shape stays inactive. A loop armed without a sentinel in the
    first place therefore is not resumed here either; the dashboard revives it.
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
            # ``gate`` decides whether a loop may be observation-gated, and a
            # stored value that is not a bool is not a decision: the STRING
            # "false" is truthy, so passing it through would gate a loop that
            # asked not to be. Normalise it here, at the boundary, rather than
            # hardening each read site.
            # PRESENT-AND-NOT-A-BOOL, which includes ``null``, normalised to
            # FALSE. Normalising it to True instead -- on the grounds that reading
            # corrupt data as an opt-out would ungate loops nobody chose to
            # ungate -- has the asymmetry backwards: gating is the state that
            # can silently STOP a loop, so an unreadable value must resolve to
            # ungated -- costing a turn per interval, which is today's cost --
            # rather than to gated, which can deactivate a recurring task whose
            # instruction merely mentioned a pull request. Only an explicit
            # boolean true gates.
            if "gate" in loop_values and not isinstance(loop_values["gate"], bool):
                logger.warning(
                    "AutoNudge: loop %s stored a non-boolean gate (%r); leaving it ungated",
                    raw.get("id"),
                    loop_values["gate"],
                )
                loop_values["gate"] = False
            # The judge fields come out of the same agent-writable store, so they
            # are normalised HERE rather than at each read site, for the reason
            # ``gate`` is: a stored ``"judge": "yes"`` would otherwise reach a
            # collector as a string, and a non-numeric streak would defeat the
            # floor that bounds how long a judge may keep a loop quiet. Every
            # unreadable value resolves to the shape that behaves as today.
            #
            # Bounded, not merely type-checked. A row in this store can be written
            # by an auto-approved agent shell, so an oversized nested map would be
            # RETAINED and re-serialized on every persist -- unbounded memory and
            # write work for the life of the loop. Each map is rebuilt from
            # allowlisted keys with clipped values, so what the loader keeps is
            # bounded by this code rather than by whatever the file holds.
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
            # ``self_armed`` is the ONE bit that relaxes the crew/member
            # fire-time guard, and this store is agent-writable. A persisted
            # non-boolean (the string "false" is truthy) must therefore
            # normalise to the REFUSING value, exactly as ``gate`` above
            # normalises to its safe value: only an explicit boolean True
            # admits, and the fire-time check compares ``is True`` besides.
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
            # ``config_generation`` is agent-writable persisted data and is
            # used in arithmetic (``+= 1``) and an equality fence. A stored
            # ``null``, string or negative would raise mid-mutation (a partial
            # update + HTTP 500) or corrupt the fence, so normalise it at the
            # boundary to a non-negative int, exactly like ``gate`` above.
            # Absent -> the dataclass default 0. An unreadable value resets to
            # 0, which only makes a captured pre-existing verdict's generation
            # not match (a missed stop, the safe direction), never a wrong stop.
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
                loop.active = False
                # A reason the UI can render: without it a routine restart mid-fire
                # surfaced as an unexplained pause with only a log line behind it.
                loop.stopped_reason = "interrupted_cycle"
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
                    # Records from the pre-gate prompt path can carry inferred
                    # observation state without an explicit decision to gate.
                    # Migrate them to a plain ungated loop so later saves and
                    # restarts cannot mistake the inert payload for a typed
                    # controller record.
                    loop.monitor = None
                    self._store_dirty = True
                if loop.monitor is None:
                    pass
                elif loop.monitor.version != MONITOR_STATE_VERSION:
                    # An older controller cannot safely interpret a newer
                    # policy. The stored ``active`` intent is deliberately
                    # left alone -- it belongs to the gateway that wrote it
                    # and must survive a downgrade so an upgrade resumes the
                    # legacy watch. A structured controller record, however,
                    # is exposed through control surfaces that must agree it
                    # cannot be scheduled, so retire only that record shape.
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
                        # A legacy snapshot can carry the accepted evidence
                        # deadline without the later typed delivery marker.
                        # Recover it as dispatched so the finite expiry path
                        # owns the claim instead of leaving it immortal.
                        loop.monitor.wake_delivery = MonitorDispatchResult.DISPATCHED
                        self._store_dirty = True
                    if (
                        loop.monitor.wake_delivery is MonitorDispatchResult.BUSY
                        and loop.next_due_ts > 0
                    ):
                        # BUSY proves no action turn started. Resume the
                        # already-claimed wake at its persisted retry instead
                        # of treating the intentionally empty evidence
                        # deadline as an ambiguous accepted dispatch.
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
                    # Structured monitor delivery belongs to the controller,
                    # which is intentionally not wired in this substrate.
                    # Deactivate rather than allowing the legacy timer to
                    # inject the prompt before a typed decision is made.
                    loop.active = False
                    loop.monitor.outcome = MonitorOutcome.BLOCKED
                    loop.monitor.stopped_reason = MONITOR_STOP_SESSION_UNAVAILABLE
                    loop.monitor.stopped_at = time.time()
                    loop.next_due_ts = 0.0
                    self._store_dirty = True
                # A current, un-claimed, unsettled monitor keeps its active
                # intent and re-arms like any other loop. It would have to be
                # deactivated here if delivery had no gate, because the
                # legacy timer would have injected a prompt without a
                # decision; the gate in _monitor_tick_is_quiet now makes that
                # decision on every tick, so surviving a restart is correct
                # rather than a hazard. Deactivating instead would end every
                # watch at the next gateway restart -- silently, since a
                # stopped watch and a quiet one look identical from outside.
            # Re-home / re-validate the persisted kill-switch path. A loop
            # armed before the data-home move would otherwise be re-armed
            # with a sentinel path nothing can ever create (see
            # repair_sentinel_path). INSIDE the per-entry try: a malformed
            # store entry must be skipped, never abort start() and take the
            # gateway offline.
            repaired = seams.repair_sentinel_path(loop.stop_sentinel_path)
            # Same fail-open posture for the numeric timer fields: they
            # drive arithmetic at arm time (``start()`` →
            # ``_arm_from_deadline``) and are emitted as JSON by the
            # REST/WS surface, so both must be finite and in range. A
            # hand-edited or foreign-written store degrades per-field —
            # never a startup abort (TypeError on a string interval) and
            # never non-standard JSON output (a 1e309 deadline parses to
            # ``inf``, which json.dump emits as invalid ``Infinity``).
            loop.next_due_ts, due_repaired = _repair_number(loop.next_due_ts, lo=0.0, fallback=0.0)
            idle_num, idle_repaired = _repair_number(
                loop.idle_secs,
                lo=float(_MIN_IDLE_SECS),
                hi=float(_MAX_IDLE_SECS),
                fallback=float(_MIN_IDLE_SECS),
            )
            loop.idle_secs = int(idle_num)
            # ``consecutive_start_failures`` is compared with ``>=`` on every
            # wake, and this store is agent-writable, so a persisted string or
            # ``null`` would raise ``TypeError`` inside ``_timer`` and the
            # active automation would silently never fire again -- and the
            # malformed value survives every reload. Normalised at the
            # boundary for the same reason ``gate``, ``self_armed`` and
            # ``config_generation`` are, rather than by hardening the one
            # comparison.
            streak_num, streak_repaired = _repair_number(
                loop.consecutive_start_failures, lo=0.0, fallback=0.0
            )
            loop.consecutive_start_failures = int(streak_num)
            if streak_repaired:
                self._store_dirty = True
            # The two counters the timer reads on every wake, for the same
            # reason: ``cycle_count`` meets ``>=`` against the cap and
            # ``created_ts`` is subtracted from the clock, so a persisted
            # string or ``null`` in either raises inside ``_timer`` and
            # dead-ends the loop the same way. Repaired to 0 -- a count of
            # nothing run, and the anchor every budget reader already treats
            # as "nothing to measure from" -- rather than a guess that could
            # stop a healthy loop. The user's resume preserves the
            # breakpoint, so this boundary is the one place they are
            # repaired. The bounds follow their own rule, below.
            count_num, count_repaired = _repair_number(loop.cycle_count, lo=0.0, fallback=0.0)
            loop.cycle_count = int(count_num)
            loop.created_ts, created_repaired = _repair_number(
                loop.created_ts, lo=0.0, fallback=0.0
            )
            # ``consecutive_failed_cycles`` is compared with ``>=`` on every
            # wake too, for the same agent-writable-store reason, so it is
            # normalised at the boundary alongside its siblings above.
            failed_num, failed_repaired = _repair_number(
                loop.consecutive_failed_cycles, lo=0.0, fallback=0.0
            )
            loop.consecutive_failed_cycles = int(failed_num)
            if count_repaired or created_repaired or failed_repaired:
                self._store_dirty = True
            # The BOUNDS are limits the user typed, so they are never repaired
            # to a number nobody chose: 0 would quietly remove a cost limit,
            # and persist that. A bound stored in a lossless integer form
            # (``"24"``, ``24.0``) is the same limit and is kept as that int,
            # so ``_timer``, ``cap_reached`` and ``budget_elapsed`` compare
            # the same number. Any other value cannot be read as a limit, so
            # the loop cannot honour it and must not run: an active row is
            # deactivated here under ``structural_terminal`` (a deterministic
            # malformed state that only a fresh configuration clears), the
            # value is left as stored for inspection, and ``update`` declines
            # a revival until a fresh bound replaces it.
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
            # ``banner`` is display-only, but it is ``.strip()``ed on the
            # fire path, so a non-string value there raises AttributeError
            # and the loop rearms forever without ever delivering. Normalize
            # it here for the same reason ``repair_sentinel_path`` opens with
            # an isinstance check: both are persisted STRING fields read
            # straight out of parsed JSON, where the dataclass annotation is
            # not enforced. Repaired-and-persisted rather than merely
            # tolerated, so a hand-edited store is corrected once instead of
            # silently suppressing the banner on every boot.
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
                # SCRUB a persisted string banner, redacting the FULL value
                # BEFORE any cap slice. A banner reaches the store through
                # producers that skip the authorized write paths — a
                # hand-edited ``autonudge.json``, a direct agent ``svc.add``,
                # or a banner persisted before this scrub existed — and the
                # loop is served RAW by ``GET /api/autonudge`` (``_serialize``
                # is ``asdict``), broadcast to every dashboard client, and
                # replayed by the fire path, so an unscrubbed credential here
                # reaches the browser after a restart. Same two passes the
                # write path uses, redaction FIRST so a secret straddling the
                # cap is masked WHOLE — slicing first would leave a raw prefix
                # the scanner cannot match. An over-cap banner — measured
                # BEFORE redaction OR after (redaction can shrink an
                # exfiltration URL below the cap, or grow a credential above
                # it) — is then BLANKED (absent), matching the promise the cap
                # makes elsewhere: a value the authorized write path would have
                # rejected is not invented back by keeping a shrunk remnant,
                # and the row falls back to the full message.
                scrubbed, _ = redact_exfiltration_urls(loop.banner)
                scrubbed, _ = redact_credentials(scrubbed)
                if len(loop.banner) > MAX_BANNER_CHARS or len(scrubbed) > MAX_BANNER_CHARS:
                    scrubbed = ""
                if scrubbed != loop.banner:
                    loop.banner = scrubbed
                    self._store_dirty = True
            # SCRUB the persisted ``message`` on load — same rationale as the
            # banner above and the same two redaction passes. The store is
            # writable out-of-band (a hand-edited ``autonudge.json`` or a
            # direct ``svc.add``) and served RAW by ``GET /api/autonudge``,
            # so a credential that reached the store bypassing the authorized
            # write path — which already scrubs ``message`` — would otherwise
            # be broadcast to every dashboard client after a restart. Unlike
            # the banner this is redaction ONLY, never blank-on-length:
            # ``message`` is the payload the model receives and has no
            # fallback row, and its 8000-char limit is a write-path concern.
            if isinstance(loop.message, str) and loop.message:
                scrubbed_msg, _ = redact_exfiltration_urls(loop.message)
                scrubbed_msg, _ = redact_credentials(scrubbed_msg)
                if scrubbed_msg != loop.message:
                    loop.message = scrubbed_msg
                    self._store_dirty = True
            if _is_torn_deactivation(loop):
                # INACTIVE, NO stop reason, deadline still LIVE. No stop path
                # of this service produces that shape: ``update`` clears the
                # deadline and records a reason on every deactivation, the
                # timer bounds and the terminal paths record theirs, and the
                # repairs above zero the deadline when they retire a record.
                # The row was flipped by a write outside the stop paths (a
                # store migrated between hosts or edited by hand) and, left
                # alone, it is a loop that nobody stopped yet nothing will
                # ever arm again — every babysit silently dead after one
                # restart, with the re-arm refused on top. Resume it: the
                # schedule the user set is still on the row, and so is its
                # kill switch (the predicate requires the sentinel path).
                logger.warning(
                    "AutoNudge: loop %s was inactive with no stop reason and a live "
                    "deadline — resuming it; a paused loop records a reason",
                    loop.id,
                )
                loop.active = True
                self._store_dirty = True
        except Exception:
            logger.warning("AutoNudge: skipping malformed loop entry: %r", raw, exc_info=True)
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
