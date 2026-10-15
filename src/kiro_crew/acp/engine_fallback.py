"""Model fallback the ENGINE performed, reported to Crew after the fact.

kiro-cli can move a turn onto a different model on its own, under both of its
engines: KAS (``kiro-cli acp --agent-engine v3``) when its model fallback is
enabled, and the V2 engine (``kiro-cli acp``). Either way the move happens
inside one ``session/prompt``, so Crew sees an ordinary terminal and learns of
the move only from a notice the engine sends alongside the stream. The two
engines spell that notice differently:

- KAS: a ``session_info_update`` whose ``_meta.kiro`` carries
  ``kind: "model_fallback"`` with ``{from, to?, trigger, scope, reason?,
  agentSubtaskId?}``. ``scope: "session"`` means KAS already pinned the target
  as the session's model; ``turn`` means the next prompt starts on the user's
  model again.
- V2: a ``_kiro.dev/session/update`` whose ``update`` is ``{sessionUpdate:
  "model_fallback", from, to, cause, source, promoted}``. The promotion arrives
  as a second notice with ``promoted: true`` after the answer.

This module owns both wire shapes and nothing else reads them. It turns either
into :class:`ModelFallbackNotice` values, keeps the session's resolved model in
step with what the engine ran (:class:`EngineFallbackTracker`), and builds the
one transcript line a turn's walk ends with (:class:`EngineFallbackWalk`).

The rules and wording follow what kiro-cli's own terminal UI shows for the same
notices, so a user sees the same account of a fallback whichever client drives
the engine.

Crew's own fallback (``agent.fallback_model`` / ``agent.refusal_fallback_model``)
is unchanged by this module: it still acts on a turn that ENDS in an error or a
refusal. The engine's move is reported, not suppressed or second-guessed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from kiro_crew.acp.types import (
    EVENT_MODEL_FALLBACK,
    STOP_REASON_CANCELLED,
    STOP_REASON_REFUSAL,
    STOP_REASON_STALE_RECOVER,
    STOP_REASON_TIMEOUT,
    AcpEvent,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.wrapper_chain import wrapper_chain

logger = logging.getLogger(__name__)

#: ``_meta.kiro.kind`` of a KAS model-fallback notice.
KAS_KIND_MODEL_FALLBACK = "model_fallback"
#: ``update.sessionUpdate`` of a V2 model-fallback notice.
V2_SESSION_UPDATE_MODEL_FALLBACK = "model_fallback"

Cause = Literal["refused", "unavailable", "unknown"]

#: Which kiro-cli engine sent a notice. Only one rule depends on it: a V2
#: refusal hop drops the refusal it moved off (``_drop_superseded_refusal``),
#: because V2 reports that refusal on metadata before the hop and its own true
#: terminal after. KAS reports its own terminal and needs nothing.
EngineKind = Literal["kas", "v2"]

#: The engine's model-id grammar (kiro-cli ``ModelId``): alphanumerics and
#: ``._:-``, at most 1024 characters. An id is interpolated into a transcript
#: line, so a value outside the grammar rejects the whole notice rather than
#: being stripped -- a stripped value could name a different, valid model.
_MAX_MODEL_ID_LENGTH = 1024
_MODEL_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,1024}")

#: Longest id shown in a transcript line; longer ones are cut with an ellipsis.
_MAX_DISPLAY_ID_LENGTH = 64

#: Longest decline reason kept. KAS's reasons are a closed camelCase set.
_MAX_REASON_LENGTH = 64

_KAS_TRIGGER_CAUSE: dict[str, Cause] = {"refusal": "refused", "availability": "unavailable"}
_V2_CAUSES: frozenset[str] = frozenset({"refused", "unavailable"})


@dataclass(frozen=True)
class ModelFallbackNotice:
    """One engine-reported fallback decision, whichever engine sent it.

    ``to_model`` is ``None`` when the engine found nowhere to go (a reported
    decline). ``promoted`` marks the notice that makes ``to_model`` the session's
    model rather than this turn's detour; it is always a separate notice that
    follows the hop it promotes.
    """

    from_model: str
    to_model: str | None
    cause: Cause
    promoted: bool = False
    #: KAS decline reason (``noServedTarget``, ``chainExhausted``, ...), or "".
    reason: str = ""
    #: V2 target source (``backend`` / ``user``), or "".
    source: str = ""
    #: Which engine sent this (see :data:`EngineKind`). Defaults to V2, the
    #: default kiro-cli engine; :func:`normalize_kas` sets it to ``"kas"``.
    engine: EngineKind = "v2"


def parse_model_id(value: object) -> str | None:
    """A model id read from an untrusted notice, or ``None`` when it is not one.

    Accepted only as one token of the engine's grammar, and only when the
    credential scrub leaves it unchanged: a notice whose id looks like a secret
    is dropped rather than rendered with a redaction marker in it.
    """
    if not isinstance(value, str) or len(value) > _MAX_MODEL_ID_LENGTH:
        return None
    if not _MODEL_ID_RE.fullmatch(value):
        return None
    scrubbed, _ = redact_exfiltration_urls(value)
    scrubbed, _ = redact_credentials(scrubbed)
    return value if scrubbed == value else None


def _bounded_reason(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value if re.fullmatch(r"[A-Za-z]{1,%d}" % _MAX_REASON_LENGTH, value) else ""


def normalize_kas(kiro: dict[str, Any]) -> list[ModelFallbackNotice]:
    """Read a KAS ``_meta.kiro`` of kind ``model_fallback``.

    Returns no notice for a sub-agent's move (KAS tags it with
    ``agentSubtaskId``; the parent stays on its model and the main transcript
    does not show it) or an unreadable one; the hop alone; or the hop followed
    by its promotion when ``scope`` is ``session``. An unrecognized ``trigger``
    is reported as cause ``unknown`` rather than guessed, and an unrecognized
    ``scope`` keeps the hop without a promotion, since a wrongly applied
    promotion would pin Crew to a model the session is not on.
    """
    subtask = kiro.get("agentSubtaskId")
    if isinstance(subtask, str) and subtask:
        logger.info("Ignoring sub-agent model_fallback notice (agentSubtaskId=%.64s)", subtask)
        return []
    if subtask is not None and not isinstance(subtask, str):
        logger.warning("model_fallback notice carries a non-string agentSubtaskId")
    from_model = parse_model_id(kiro.get("from"))
    raw_to = kiro.get("to")
    to_model = None if raw_to is None else parse_model_id(raw_to)
    if from_model is None or (raw_to is not None and to_model is None):
        logger.warning("Dropping model_fallback notice with an unreadable model id")
        return []
    trigger = kiro.get("trigger")
    cause: Cause = (
        _KAS_TRIGGER_CAUSE.get(trigger, "unknown") if isinstance(trigger, str) else "unknown"
    )
    if cause == "unknown":
        logger.warning("Unrecognized model_fallback trigger from KAS: %.32r", trigger)
    hop = ModelFallbackNotice(
        from_model=from_model,
        to_model=to_model,
        cause=cause,
        reason=_bounded_reason(kiro.get("reason")),
        engine="kas",
    )
    if to_model is None:
        return [hop]
    scope = kiro.get("scope")
    if scope == "session":
        return [hop, ModelFallbackNotice(from_model, to_model, cause, promoted=True, engine="kas")]
    if scope != "turn":
        logger.warning("Unrecognized model_fallback scope from KAS; not promoting: %.32r", scope)
    return [hop]


def is_v2_model_fallback(params: object) -> bool:
    """Whether a ``_kiro.dev/session/update`` params object is a V2 fallback notice."""
    if not isinstance(params, dict):
        return False
    update = params.get("update")
    return (
        isinstance(update, dict) and update.get("sessionUpdate") == V2_SESSION_UPDATE_MODEL_FALLBACK
    )


def normalize_kiro_v2(update: dict[str, Any]) -> list[ModelFallbackNotice]:
    """Read a V2 ``model_fallback`` session update, or no notice when it is malformed.

    ``to`` is ``null`` when nothing was left to try. An absent ``promoted`` is a
    producer that predates promotion, which only ever hopped, so it reads as
    ``False``; any other non-boolean drops the notice.
    """
    from_model = parse_model_id(update.get("from"))
    raw_to = update.get("to")
    to_model = None if raw_to is None else parse_model_id(raw_to)
    if from_model is None or (raw_to is not None and to_model is None):
        logger.warning("Dropping model_fallback update with an unreadable model id")
        return []
    cause = update.get("cause")
    if cause not in _V2_CAUSES:
        logger.warning("Dropping model_fallback update with an unrecognized cause: %.32r", cause)
        return []
    promoted = update.get("promoted")
    if promoted is not None and not isinstance(promoted, bool):
        logger.warning("Dropping model_fallback update with a malformed promoted")
        return []
    source = update.get("source")
    return [
        ModelFallbackNotice(
            from_model=from_model,
            to_model=to_model,
            cause=cause,  # type: ignore[arg-type]  # narrowed by _V2_CAUSES
            promoted=promoted is True,
            source=source if source in ("backend", "user") else "",
            engine="v2",
        )
    ]


def is_unpinned_pick(model: object) -> bool:
    """Whether a model pick leaves the choice to the backend: empty or ``auto``."""
    return not isinstance(model, str) or not model.strip() or model.strip().lower() == "auto"


@dataclass
class _Hop:
    from_model: str
    to_model: str | None
    cause: Cause


WalkOutcome = Literal["answered", "cancelled", "faulted"]

#: Terminals that end a turn without an answer. Everything else that is not a
#: cancel (``end_turn``, a token limit) produced one.
_UNANSWERED_STOP_REASONS = frozenset(
    {"", STOP_REASON_REFUSAL, STOP_REASON_STALE_RECOVER, STOP_REASON_TIMEOUT}
)


def walk_outcome(stop_reason: str, *, cancelled: bool = False) -> WalkOutcome:
    """How a turn ended, for its engine-fallback line. One rule for every surface.

    The terminal stop reason decides, for both engines: ``end_turn`` answered,
    a refusal / timeout / ``error:`` / empty reason did not, a cancel cancelled.
    A promotion is NOT read as an answer: KAS pins its target at move time,
    before the target answers. The terminal is trustworthy on V2 too because
    :meth:`EngineFallbackTracker.apply` drops the refusal a V2 hop moved off
    (see ``_drop_superseded_refusal``), so the refusal fold leaves a recovered
    V2 turn's ``end_turn`` intact.
    """
    if cancelled or stop_reason == STOP_REASON_CANCELLED:
        return "cancelled"
    if stop_reason in _UNANSWERED_STOP_REASONS or stop_reason.startswith("error"):
        return "faulted"
    return "answered"


#: Shown when the engine found nowhere to go. kiro-cli's terminal UI names its
#: ``/model`` command here; Crew's equivalent is the composer's model picker.
_TRY_ANOTHER = "Try a different model from the model picker."


def same_model(a: object, b: object) -> bool:
    """Whether *a* and *b* name one model, ignoring case and padding. Empty matches nothing."""
    return bool(a) and str(a).strip().lower() == str(b or "").strip().lower()


def _serve(owner: Any, model: str) -> None:
    """Point *owner* at *model* and rebase its context stats to that model's window.

    Without the rebase, a window kept from the old model outranks the registry in
    ``backfill_context_window``, so a percentage reads against the wrong size.
    """
    if getattr(owner, "_resolved_model_id", "") == model:
        return
    owner._resolved_model_id = model
    stats = getattr(owner, "last_prompt_stats", None)
    if stats is None or not hasattr(stats, "rebase_to_window"):
        return
    from kiro_crew import model_registry  # leaf module, kept off the types import

    window = model_registry.model_window(model) if model_registry.has_known_window(model) else 0
    stats.rebase_to_window(window or 0)


def _display(model_id: str) -> str:
    if len(model_id) <= _MAX_DISPLAY_ID_LENGTH:
        return model_id
    return model_id[: _MAX_DISPLAY_ID_LENGTH - 1] + "…"


def _verb(cause: Cause) -> str:
    if cause == "unavailable":
        return "was unavailable"
    if cause == "unknown":
        return "yielded"
    return "refused"


@dataclass
class EngineFallbackWalk:
    """The engine's fallback hops in one turn, and the line that ends it."""

    hops: list[_Hop] = field(default_factory=list)
    promoted_to: str | None = None

    def record(self, notice: ModelFallbackNotice) -> None:
        to = notice.to_model
        if to is not None and to == notice.from_model:
            # Same-id move: a retry on one model, not a switch, so no hop.
            return
        if notice.promoted:
            if to is not None:
                self.promoted_to = to
            return
        self.hops.append(_Hop(notice.from_model, to, notice.cause))

    def moved(self) -> bool:
        """Whether a hop moved this turn onto another model."""
        return any(h.to_model is not None for h in self.hops)

    def last_model(self) -> str | None:
        """The model the engine ran last this turn, or ``None`` when it did not move.

        The last hop that found a target names it: a decline (no target) moves
        nothing, so it does not count. A promotion is the conversation's model,
        not necessarily the last one that ran, so it answers only when no hop
        was recorded; the promotion readers use ``promoted_to`` itself.
        """
        for hop in reversed(self.hops):
            if hop.to_model is not None:
                return hop.to_model
        return self.promoted_to

    def line(self, outcome: WalkOutcome, *, promoted: bool = True) -> str | None:
        """The transcript line for this turn's walk, or ``None`` when nothing moved.

        *promoted* False words a promotion as a move for this turn: while Crew's
        own fallback is active, Crew moves the session back to its primary.
        """
        if not self.hops:
            return None
        last = self.hops[-1]
        head = "; ".join(f"{_display(h.from_model)} {_verb(h.cause)}" for h in self.hops)
        promoted_to = self.promoted_to if promoted else None
        promotion = (
            f" {_display(promoted_to)} is now this conversation's model." if promoted_to else ""
        )
        if outcome == "cancelled":
            return f"{head}; cancelled.{promotion}"
        if last.to_model is None:
            return f"{head}. No model answered. {_TRY_ANOTHER}{promotion}"
        if outcome == "faulted":
            if len(self.hops) > 1:
                return f"{head}; {_display(last.to_model)} produced no answer.{promotion}"
            return f"{head}. No model answered.{promotion}"
        if promoted_to == last.to_model:
            return f"{head}; answered by {_display(last.to_model)}, now this conversation's model."
        return f"{head}; answered by {_display(last.to_model)} for this turn.{promotion}"


def _drop_superseded_refusal(owner: Any) -> None:
    """Forget a refusal the V2 engine has just moved the turn off of.

    ``AcpPromptStats.terminal_refusal`` rewrites the prompt's terminal to
    ``refusal`` whenever a refusal was recorded on metadata this prompt, because
    Kiro ends a filtered turn ``end_turn`` after streaming its canned text. In a
    V2 refusal fallback, the recorded refusal belongs to the model being LEFT.
    On the wire it arrives as a ``_kiro.dev/metadata`` refusal, then the
    ``model_fallback`` hop, then the prompt's answer: ``stopReason: end_turn``
    when a later hop answered, ``refusal`` when none did. Left in place, the
    fold would turn that true ``end_turn`` into ``refusal`` for every consumer
    at once.

    Cleared at the HOP, not the promotion: a target that refuses too sends its
    own refusal after the hop and is folded as usual, while the promotion
    notice can reach the client later than that refusal and would wipe it. KAS
    is never cleared here; its terminal already says whether the turn was
    answered, and its refusal is not recorded on this channel.
    """
    stats = getattr(owner, "last_prompt_stats", None)
    if stats is not None and getattr(stats, "refusal", None) is not None:
        stats.refusal = None
        logger.debug("engine fallback: V2 refusal hop superseded this prompt's refusal")


@dataclass
class EngineFallbackTracker:
    """Keeps a session's model fields in step with the engine's moves.

    Held by the object that owns the session's ``_model`` (intent) and
    ``_resolved_model_id`` (what the backend runs): ``AcpSessionHandle`` and
    ``AcpClient``.

    - A turn-scoped hop sets ``_resolved_model_id`` to the target, so usage and
      turn stats (``read_effective_model``) are credited to the model that
      answered. The next prompt puts the prior value back, because the engine
      starts that prompt on the user's model.
    - A promotion is the model kiro-cli is now on: ``_resolved_model_id`` takes
      it, and so does ``_model`` when pinned, so Crew reflects it. An unpinned
      session stays unpinned.
    """

    #: This prompt's hops, for a surface that annotates a delivered result.
    walk: EngineFallbackWalk = field(default_factory=EngineFallbackWalk)
    _turn_restore: str | None = None
    _turn_override: str = ""
    #: The engine's last promotion, ``(from, to)``. Outlives the walk so the
    #: next turn's fallback restore can tell it from a user's switch, which clears it.
    promotion: tuple[str, str] | None = None
    #: Whether a channel's pick-follow already read :attr:`promotion`.
    promotion_followed: bool = False

    def begin_prompt(self, owner: Any) -> None:
        """Start a prompt: forget the last walk, undo its turn-scoped hop. Idempotent."""
        self.walk = EngineFallbackWalk()
        if self._turn_restore is None:
            return
        if getattr(owner, "_resolved_model_id", "") == self._turn_override:
            _serve(owner, self._turn_restore)
        self._turn_restore = None
        self._turn_override = ""

    def next_prompt_model(self, served: str) -> str:
        """The model the session's next prompt starts on, while it serves *served*.

        A turn-scoped hop still standing gives way to the model it moved off, as
        :meth:`begin_prompt` puts back.
        """
        if self._turn_restore is not None and served == self._turn_override:
            return self._turn_restore
        return served

    def note_model_set(self) -> None:
        """Drop the pending turn-scoped restore after a model switch lands.

        The owner's ``set_model`` calls this once the switch is recorded. The
        model it set is what the session runs next, even when that model is the
        target of the last turn's hop, so the next prompt must not put back the
        model the hop moved off.
        """
        self._turn_restore = None
        self._turn_override = ""
        self.promotion = None
        self.promotion_followed = False

    def apply(self, owner: Any, notices: list[ModelFallbackNotice]) -> list[ModelFallbackNotice]:
        """Apply *notices* to *owner* and return them for the event stream."""
        for notice in notices:
            self.walk.record(notice)
            to = notice.to_model
            if to is None or to == notice.from_model:
                # A decline moves nothing, and a same-id move is the backend
                # re-routing behind a tier such as ``auto``: the model the
                # session is on did not change.
                continue
            if notice.promoted:
                # A chained promotion keeps the first source, across prompts too.
                prior = self.promotion
                # A channel that already followed the prior one now holds its target.
                chained = (
                    prior is not None
                    and not self.promotion_followed
                    and same_model(notice.from_model, prior[1])
                )
                self.promotion = (prior[0] if prior and chained else notice.from_model, to)
                self.promotion_followed = False
                _serve(owner, to)
                if not is_unpinned_pick(getattr(owner, "_model", "")):
                    owner._model = to
                self._turn_restore = None
                self._turn_override = ""
                continue
            if notice.engine == "v2" and notice.cause == "refused":
                _drop_superseded_refusal(owner)
            if self._turn_restore is None:
                self._turn_restore = str(getattr(owner, "_resolved_model_id", "") or "")
            _serve(owner, to)
            self._turn_override = to
        return notices

    def events(self, owner: Any, notices: list[ModelFallbackNotice]) -> list[AcpEvent]:
        """Apply *notices* to *owner* and wrap each as an ``EVENT_MODEL_FALLBACK``.

        The one step both session owners (``AcpClient``, ``AcpSessionHandle``)
        take when a notice arrives: the model fields move first, so the usage of
        whatever answers next is credited to the model that produced it.
        """
        return [
            AcpEvent(kind=EVENT_MODEL_FALLBACK, model_fallback=notice)
            for notice in self.apply(owner, notices)
        ]


#: Attribute the session-owning object (handle or client) keeps its tracker on.
TRACKER_ATTR = "_engine_fallback"


def tracker_of(source: object) -> EngineFallbackTracker | None:
    """The tracker of the session under *source*, or ``None``.

    *source* may be the session-owning object itself or any provider wrapper
    above it (``AcpProvider`` -> ``AcpSessionProvider`` -> ``AcpSessionHandle``),
    found through the same walk the usage rows use (:func:`wrapper_chain`).
    Never raises: a reader is annotating or attributing, and a lookup failure
    must not fail the run it describes.
    """
    try:
        for node in wrapper_chain(source):
            tracker = getattr(node, TRACKER_ATTR, None)
            if isinstance(tracker, EngineFallbackTracker):
                return tracker
    except Exception:  # noqa: BLE001 - best-effort lookup on exotic wrappers
        logger.debug("engine fallback tracker lookup failed", exc_info=True)
    return None


def _standing_promotion(source: object) -> tuple[EngineFallbackTracker, tuple[str, str]] | None:
    """The tracker and its promotion, while the session under *source* still serves it.

    A model change that does not pass through ``set_model`` (an agent switch
    activating its own model) leaves the promotion recorded but stale. A
    turn-scoped hop after the promotion does not: the next prompt goes back to it.
    """
    try:
        for node in wrapper_chain(source):
            tracker = getattr(node, TRACKER_ATTR, None)
            if not isinstance(tracker, EngineFallbackTracker):
                continue
            if not tracker.promotion:
                return None
            served = getattr(node, "_resolved_model_id", None)
            if served is not None and not same_model(
                tracker.next_prompt_model(str(served)), tracker.promotion[1]
            ):
                return None
            return tracker, tracker.promotion
    except Exception:  # noqa: BLE001 - best-effort lookup on exotic wrappers
        logger.debug("engine fallback promotion lookup failed", exc_info=True)
    return None


def engine_served_model(source: object) -> str:
    """The model the engine moved *source*'s last prompt onto, or ``""``.

    For a surface that shows which model ran: when the engine fell back inside
    the prompt, the session's ``served_model`` can still name the pin (a
    turn-scoped hop leaves a pinned ``_model`` alone), so this reads the walk
    instead, then the session's standing promotion, which outlives the walk (a
    recovery prompt on the same session starts a new one). ``""`` means the
    engine did not move the session, and the caller's own reading stands.
    Never raises.
    """
    tracker = tracker_of(source)
    if tracker is None:
        return ""
    try:
        standing = _standing_promotion(source)
        return tracker.walk.last_model() or (standing[1][1] if standing else "")
    except Exception:  # noqa: BLE001 - a display read must not fail the run
        logger.debug("engine fallback served-model read failed", exc_info=True)
        return ""


_NO_SNAPSHOT = object()


def follow_engine_promotion(
    prefs: dict[Any, str], key: Any, source: object, *, before: object = _NO_SNAPSHOT
) -> bool:
    """Move a pinned per-conversation model pick onto a model the engine promoted.

    For the Telegram / Discord / Webex ``_model_pref``, so the picker and the
    next cold start reflect the model kiro-cli is on, as a pinned dashboard slot
    does. An Auto pick stays Auto; a turn-scoped hop changes nothing.

    Reads the session's standing promotion, not the turn's walk: a prompt the
    channel runs after the answer (post-turn compaction) starts a new walk.
    Left alone: a pick that changed during the turn (*before* is the pick at its
    start), and a pick naming a model other than the one the engine replaced.
    Each promotion is read once. Returns whether the record moved. Never raises.
    """
    try:
        standing = _standing_promotion(source)
        if standing is None or standing[0].promotion_followed:
            return False
        tracker, (replaced, promoted) = standing
        # Consumed before any check below: a promotion skipped for a changed or
        # Auto pick must not stay armed for a later turn that never prompts.
        tracker.promotion_followed = True
        current = str(prefs.get(key) or "")
        if before is not _NO_SNAPSHOT and current != str(before or ""):
            return False
        if is_unpinned_pick(current) or same_model(current, promoted):
            return False
        if not same_model(current, replaced):
            return False
        prefs[key] = promoted
        return True
    except Exception:  # noqa: BLE001 - bookkeeping after a turn must not fail it
        logger.debug("engine fallback: pick-follow failed", exc_info=True)
        return False
