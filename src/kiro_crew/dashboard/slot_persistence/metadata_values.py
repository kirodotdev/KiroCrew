"""What a metadata-line value must satisfy before a restored slot takes it.

The session metadata line is a file the agent's own tools can write, so each value a
read takes back from it is checked here first: the title state is redacted for
display and its provenance and refresh budget rebuilt, the compaction threshold and
the turn time limit are clamped to their ranges, the dismissed source links are
re-validated and capped, a retired mode comes back as plain chat, and the model and
effort are canonicalised and re-validated. ``metadata_codec``'s field table calls
these from its readers; ``channel_slots`` and the History resume reach the model
restore through it too.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from kiro_crew import model_registry
from kiro_crew.config.loader import (
    AUTOCOMPACT_PCT_MAX,
    AUTOCOMPACT_PCT_MIN,
    CHAT_TURN_TIMEOUT_MAX,
    CHAT_TURN_TIMEOUT_MIN,
)
from kiro_crew.dashboard.chat_title import (
    _TITLE_ORIGINS,
    _counts_as_user_turn,
    _rehydrated_refresh_mark,
)
from kiro_crew.dashboard.chat_utils import _normalize_model
from kiro_crew.dashboard.state import _MAX_DISMISSED_SOURCE_LINKS

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import _ChatSlot

logger = logging.getLogger("kiro_crew.dashboard.chat_persistence")


#: Retired session modes. A slot persisted under one of these comes back as a
#: PLAIN chat: the transcript is untouched and still renders; there is no
#: mode-specific dispatch for it, because the mode itself is gone.
#:
#: ``crew`` — Crew Mode (one session fanning topics out to sub-sessions),
#: retired in favour of the Crew Members page. Its durable store under
#: ``<data home>/crew/`` is neither read nor deleted here; the transcript is
#: the user's record and the store held only routing state.
#:
#: ``orchestrator`` — chat Autopilot mode (a planned, staged run), retired
#: without a replacement. Its stage result files are neither read nor deleted.
_RETIRED_MODES: frozenset[str] = frozenset({"crew", "orchestrator"})


def _rehydrate_title_origin(titled: bool, stored: object) -> str:
    """Resolve a rehydrated slot's title origin from persisted metadata.

    Mirrors how ``_titled`` is derived from the presence of a persisted title,
    so "a manual rename is final" survives a reload. A recognized stored
    ``title_origin`` is used verbatim. A titled slot with NO stored origin is a
    LEGACY session written before the field existed: treat it conservatively as
    ``"user"`` so the background title refresh never rewrites what might be a
    manual rename. An untitled slot has no origin.
    """
    if not titled:
        return ""
    if isinstance(stored, str) and stored in _TITLE_ORIGINS:
        return stored
    return "user"


def _rehydrate_title_refresh_mark(stored: object) -> int:
    """Resolve the persisted refresh mark; unknown/invalid values mean 0."""
    if isinstance(stored, int) and not isinstance(stored, bool) and stored > 0:
        return stored
    return 0


def _rehydrate_title_low_signal(stored: object) -> bool:
    """Resolve the persisted low-signal flag; absent/invalid means False.

    A legacy session written before the field existed rehydrates as False —
    the conservative default, since re-arming the early refresh on old
    sessions would spend one-liner calls their budget never accounted for.
    """
    return stored is True


def _rehydrate_slot_title(
    slot: _ChatSlot,
    raw_title: str,
    *,
    titled: bool,
    metadata: Mapping[str, object],
) -> None:
    """Restore the complete persisted title state through one contract.

    Titles may be model-authored, so display redaction must happen before the
    value reaches the slot. Keeping provenance and refresh-budget restoration
    beside that assignment prevents hydration paths from restoring only part
    of the title state.
    """
    from kiro_crew.dashboard import chat_persistence as cp  # circular import: facade imports owners

    safe_title, _ = cp.redact_exfiltration_urls(raw_title)
    safe_title, _ = cp.redact_credentials(safe_title)
    slot.title = safe_title
    slot._titled = titled
    slot._title_origin = _rehydrate_title_origin(titled, metadata.get("title_origin"))
    slot._title_refresh_mark = _rehydrate_title_refresh_mark(metadata.get("title_refresh_mark"))
    slot._title_low_signal = _rehydrate_title_low_signal(metadata.get("title_low_signal"))


def _rebase_rehydrated_refresh_mark(slot: _ChatSlot) -> None:
    """Re-base the restored refresh mark against the user rows the loader kept.

    Runs once per read, AFTER its message window is appended and its local-turn
    marker is reconciled (:meth:`AppliedMeta.settle` keeps that order): the
    reconcile can re-append an opening row the periodic flush never wrote, and
    the mark must match the window the next turn counts over.
    The window is the latest 500 rows, so the slot's user count restarts below the
    count the persisted mark was taken at, and the opt-in refresh cadence
    (``dashboard.title_refresh_every_turns``) would otherwise stay silent until
    the count climbed past that mark again. Counts user turns over
    ``slot.messages`` with ``maybe_refresh_title``'s own predicate
    (``chat_title._counts_as_user_turn``), so the two agree on what a turn is.
    See ``chat_title._rehydrated_refresh_mark`` for the floor that keeps a spent
    built-in milestone spent.
    """
    if not slot._title_refresh_mark:
        return
    user_count = sum(1 for m in slot.messages if _counts_as_user_turn(slot, m))
    slot._title_refresh_mark = _rehydrated_refresh_mark(slot._title_refresh_mark, user_count)


def _validate_autocompact_pct(raw: object) -> float | None:
    """Return *raw* as a threshold percent within the documented range, else None.

    Restore-path twin of the endpoint validation: a tampered or corrupted
    metadata file must not seed an override that can never fire (over the max)
    or that thrashes compaction (under the min). Out-of-range finite values
    clamp — matching how the loader treats the global knob — while
    non-numeric/NaN values are discarded.
    """
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        try:
            value = float(raw)
        except OverflowError:
            # An int too large for a float; a corrupted metadata file must not
            # abort the restore path.
            logger.warning("Discarding oversized persisted autocompact_pct")
            return None
        if value != value:  # NaN
            logger.warning("Discarding NaN persisted autocompact_pct")
            return None
        return min(max(value, AUTOCOMPACT_PCT_MIN), AUTOCOMPACT_PCT_MAX)
    if raw is not None:
        logger.warning("Discarding invalid persisted autocompact_pct: %r", raw)
    return None


def _validate_turn_timeout_secs(raw: object) -> int | None:
    """Return *raw* as a turn time limit in whole seconds within range, else None.

    Restore-path twin of the endpoint validation, on a line the agent's own
    tools can write: an integer outside the global setting's range clamps to
    it, the way the loader treats ``agent.chat_turn_timeout_secs``, and anything
    that is not an integer (a bool, a float, a string) is discarded so the
    session follows the global limit.
    """
    if isinstance(raw, int) and not isinstance(raw, bool):
        return min(max(raw, CHAT_TURN_TIMEOUT_MIN), CHAT_TURN_TIMEOUT_MAX)
    if raw is not None:
        logger.warning("Discarding invalid persisted turn_timeout_secs: %r", raw)
    return None


def _restore_dismissed_source_links(slot: "_ChatSlot", raw: object) -> None:
    """Rehydrate the per-slot dismissed source-link identity set from metadata.

    History JSONL is a file an attacker with disk access could tamper, and these
    keys feed the derivation filter that decides which chips a client sees, so
    each entry is re-validated against the canonical serialized-identity grammar
    before it is trusted. A malformed entry is dropped rather than aborting the
    restore -- a corrupt suppression key can only ever fail to match a real
    identity, so dropping it is safe and fails toward showing the chip.
    """
    if not isinstance(raw, list):
        # The transcript being loaded records no dismissals, so the slot has
        # none: clear rather than return, or a set left over from a previous
        # binding of a reused slot object would survive a rebind and suppress
        # unrelated links on the new transcript. Hydration is authoritative —
        # the slot's dismissed set always reflects the transcript it now shows.
        slot._dismissed_source_links = set()
        slot._dismissed_hydrated = True
        slot.invalidate_source_links()
        return
    # Function-local to avoid a circular import: source_providers imports from the
    # dashboard state/handler layer this module also serves. Same lazy-import
    # convention the handlers use for source_providers.
    from kiro_crew.dashboard.source_providers.contract import is_valid_source_identity_key

    # Enforce the same named ceiling the add site does, DURING iteration, so a
    # tampered or oversized on-disk line cannot make us materialize an unbounded
    # valid-key list before slicing. Stop retaining once the cap is reached, but
    # keep COUNTING the valid keys past it so the drop is said out loud once per
    # restore -- the ``a-bound-bounds-every-field-it-retains`` contract requires
    # a bounded retention to report its overflow, exactly as
    # ``_capped_dismissed_line`` does on the write side. Keeping the first
    # _MAX_DISMISSED_SOURCE_LINKS valid keys and dropping the rest only ever
    # fails toward SHOWING a chip, never toward hiding an unrelated one.
    bounded: set[str] = set()
    dropped = 0
    for key in raw:
        if not is_valid_source_identity_key(key):
            continue
        if len(bounded) < _MAX_DISMISSED_SOURCE_LINKS:
            bounded.add(key)
        else:
            dropped += 1
    if dropped:
        logger.warning(
            "dismissed_source_links restore truncated by %d to the %d cap",
            dropped,
            _MAX_DISMISSED_SOURCE_LINKS,
        )
    slot._dismissed_source_links = bounded
    slot._dismissed_hydrated = True


def _restored_mode(raw: object) -> str:
    """The mode a persisted slot comes back with, or "" for plain chat.

    Maps a :data:`_RETIRED_MODES` value to "" rather than refusing the restore:
    the session and its history are still the user's, the mode that once
    dispatched them is not. Anything that is not a non-empty string is "" too,
    which matches what the old ``if meta.get("mode")`` guard admitted.
    """
    if not isinstance(raw, str) or not raw:
        return ""
    if raw in _RETIRED_MODES:
        return ""
    return raw


def _restore_model_fields(slot: Any, meta: dict, *, cfg: Any, effort_marker: bool = False) -> bool:
    """Apply the persisted ``model`` and ``reasoning_effort`` to *slot*.

    Shared by every path that hydrates a slot from a transcript -- the two
    restart reads (through :func:`apply`), History resume, and channel surfacing
    (:func:`kiro_crew.dashboard.channel_slots.surface_channel_session`) -- so
    they cannot drift apart.
    A path that skips this leaves ``slot.model`` empty, and the next save
    writes that empty value over the model the user picked.

    *cfg* is the already-loaded restore config, or None. Its provider
    canonicalizes a pre-migration claude_code provider id to the dropdown
    key (no-op for other providers); it is read only when a model is set.
    *effort_marker* is the off-loop ``_has_validated_effort_marker`` read for
    this effort value. The effort allowlist is process state that stays on
    ``chat_persistence``, so the check is made there.
    Returns True when the metadata carried a model. False also covers a
    ``model`` of ``""``, a slot that follows the default, so a caller that falls
    back to the agent's model must check that the key is absent.
    """
    from kiro_crew.dashboard import chat_persistence as cp  # circular import: facade imports owners

    raw_model = meta.get("model")
    # Metadata is agent-writable: a non-string model is dropped, not hashed.
    has_model = isinstance(raw_model, str) and bool(raw_model)
    if has_model and isinstance(raw_model, str):
        provider = cfg.agent.provider if cfg else ""
        slot.model = model_registry.canonicalize_for_provider(_normalize_model(raw_model), provider)
    # `jev_route` is deliberately not read here: it is an owner pick that can
    # spend money, and transcript metadata is agent-writable.
    if meta.get("reasoning_effort"):
        slot.reasoning_effort = cp._validate_reasoning_effort(
            meta["reasoning_effort"], persisted_marker=effort_marker
        )
    return has_model
