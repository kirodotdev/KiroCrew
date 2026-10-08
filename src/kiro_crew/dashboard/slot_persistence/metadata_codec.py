"""Every slot field a session's metadata line carries, both ways, in one table.

A dashboard slot's persisted state is the first line of its session file. Two save
forms write it -- the full save's rebuilt line and the empty-window merge -- and
three hydration purposes read it back onto a slot:

* ``RESTORE`` -- the open-tab restore and a targeted rehydrate
  (``chat_persistence._rehydrate_slot_from_history``);
* ``RECENT`` -- the boot restore of recent sessions
  (``chat_persistence._apply_recent_session``);
* ``RESUME`` -- a History resume and the transfer import
  (``chat_api.resume._hydrate_slot_from_history``).

:data:`FIELDS` is the table: one :class:`Field` per key, naming how each save form
writes it and which purposes take it back from the line. A purpose missing from a
row is a declared asymmetry. Each is kept exactly as the hand-written readers
behaved, and its ``why`` says what it costs; changing one is a product decision,
not a refactor.

The interface:

* :func:`slot_args` -- the ``get_or_create_slot`` keywords a purpose takes from
  the line (identity the constructor must see: app, origin, channel origin, a
  member pin);
* :func:`apply` -- every other field the purpose reads, in table order, once the
  slot exists. Its :class:`AppliedMeta` answers what the caller acts on itself (a
  tab id to persist, a relay that was mid-flight), and
  :meth:`AppliedMeta.settle` finishes the fields whose read needs the loaded
  window;
* :func:`encode` -- the line a save writes from slot state, in the form's key
  order, given the values the save folds against the disk (:class:`SaveFolds`).

The line is a file the agent's own tools can write. Each row's read keeps exactly
the checks its hand-written reader applied, no more and no fewer: some values are
re-validated (the title state, the compaction threshold, the dismissed links, the
model and effort, the mode, the custom color hex, the tags, the agent kind, the
artifact, the executor, the held notes, the queue), and the rest are still copied
as written.
New slot-owned metadata fields belong here: a row, its key in ``LINE_ORDER`` and
``MERGE_ORDER``, and in ``history.SLOT_OWNED_META_KEYS`` when its absence must
clear it; ``test/test_slot_metadata_codec.py`` round-trips the row once
``_POPULATED`` sets it. Outside the table, ``channel_slots.surface_channel_session`` and the cron
binders still read a few fields of the line by hand.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from kiro_crew import model_registry
from kiro_crew.config.loader import AUTOCOMPACT_PCT_MAX, AUTOCOMPACT_PCT_MIN
from kiro_crew.dashboard.chat_title import (
    _TITLE_ORIGINS,
    _counts_as_user_turn,
    _rehydrated_refresh_mark,
)
from kiro_crew.dashboard.chat_utils import (
    _normalize_model,
    effective_session_key,
    slot_transcript_key,
)
from kiro_crew.dashboard.slot_buffers import (
    committed_filtered_note_ids,
    drop_committed_restored_notes,
    sanitize_restored_deferred_notes,
)
from kiro_crew.dashboard.slot_queue_repository import (
    queue_persist_signature,
    sanitize_restored_queue,
)
from kiro_crew.dashboard.state import _MAX_DISMISSED_SOURCE_LINKS
from kiro_crew.memory_stores import named_store_or_empty
from kiro_crew.messaging.link import is_channel_session_key
from kiro_crew.validation import ARTIFACT_SLUG_RE, normalize_theme_consent_sha

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger("kiro_crew.dashboard.chat_persistence")


# Custom session color contract: lowercase-normalized #rrggbb only. Canonical home
# of the regex (chat_handlers reaches it through chat_persistence for its request
# validation). Every read of the line re-validates against it because the JSONL
# metadata line is attacker-writable and this string reaches every client's inline
# style.
COLOR_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

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

RESTORE = "restore"
RECENT = "recent"
RESUME = "resume"
PURPOSES: frozenset[str] = frozenset({RESTORE, RECENT, RESUME})
_STARTUP = frozenset({RESTORE, RECENT})


# ── Purposes ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Restore:
    """A read for the open-tab restore or a targeted rehydrate.

    *name* is the slot name the reader restores under. *member* is the dm.json
    identity ``(member, mode)`` for a member key, or ``None``; a member binding
    pins the agent and the mode, so the line's are not read. *agent* is the
    committed agent choice (``_restored_agent_name``) an async reader prefetched
    off the loop; ``None`` reads it inline at the agent's row. *cfg*, *model_map*
    and *effort_marker* feed the model restore: the restore config, the
    agent-to-model map for a legacy line with no ``model``, and the off-loop
    validated-effort marker read.
    """

    purpose: ClassVar[str] = RESTORE

    name: str
    member: tuple[str, str] | None = None
    agent: str | None = None
    cfg: Any = None
    model_map: Mapping[str, str] = field(default_factory=dict)
    effort_marker: bool = False


@dataclass(frozen=True)
class Recent(Restore):
    """A read for the boot restore of recent sessions.

    Everything :class:`Restore` takes, plus the session-list row it restores:
    *listing* supplies the title this restore shows (``list_sessions`` derives
    one for an untitled session), and *history_key* is that row's key.
    """

    purpose: ClassVar[str] = RECENT

    listing: Mapping[str, object] = field(default_factory=dict)
    history_key: str = ""


@dataclass(frozen=True)
class Resume:
    """A read for a History resume or a transfer import.

    *member* is the dm.json identity the resume verified, or ``None``.
    *request_title* names a session the line leaves untitled. *folder_unhidden*
    and *folder_checked_id* are the folder un-hide's verdict and the folder it is
    about. *disk_meta_observed* is whether the line was read off disk (an import
    synthesises it). *app* and *history_key* are construction inputs: the
    requesting app and the transcript being adopted.
    """

    purpose: ClassVar[str] = RESUME

    member: tuple[str, str] | None = None
    request_title: str = ""
    folder_unhidden: bool = True
    folder_checked_id: str = ""
    disk_meta_observed: bool = True
    app: str = ""
    history_key: str = ""


Purpose = Restore | Resume


# ── Value checks a read applies ───────────────────────────────────────────────


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
    Returns True when the metadata carried a model, so a caller can fall
    back to the agent's default model when it did not.
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


# ── The table ─────────────────────────────────────────────────────────────────


class _Omit:
    """Marks a key a save form leaves off the line."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<omit>"


OMIT: Any = _Omit()


@dataclass(frozen=True)
class SaveFolds:
    """The values a save decides against the line on disk and its own transaction.

    ``encode`` places each where its form writes it. A full save folds every one
    of them; the empty-window merge uses *memory_mode*, *closed*, *closed_at*,
    *queued_prompts*, *dismissed_source_links* and *deferred_notes* (its tab id
    and channel filing come from the slot alone, because a merge never deletes
    the disk copy). ``None`` for *dismissed_source_links* or *rotation_generation*
    leaves the key off the line.
    """

    memory_mode: str
    created_at: object = ""
    last_consolidated: object = 0
    closed: bool = False
    closed_at: object = None
    dismissed_source_links: list[str] | None = None
    deferred_notes: list[dict] = field(default_factory=list)
    queued_prompts: list[dict] = field(default_factory=list)
    last_user_at: str = ""
    channel_folder_filed: bool = False
    tab_id: object = None
    rotation_generation: int | None = None
    # Staged ``mutes_opened`` value. ``None`` (the default) means "read the live
    # ``slot.mutes_opened``", which every caller but the mute endpoint relies on.
    # The mute endpoint persists its NEW value through this fold while leaving the
    # live flag at its committed value across the save, then flips the live flag
    # only after the write commits under the transcript lock -- so a concurrent
    # slots broadcast during the save window never observes the provisional value.
    # Honored by BOTH the full ``build_full_line`` path and the empty-window
    # ``merge_empty_window`` path (a message-less newborn), so a staged value
    # reaches disk whichever branch the save takes.
    mutes_opened: bool | None = None


@dataclass
class _Read:
    """One :func:`apply` call: its inputs and what it hands back."""

    state: Any
    slot: Any
    meta: dict[str, Any]
    purpose: Any
    applied: AppliedMeta


Writer = Callable[[Any, SaveFolds], object]


@dataclass(frozen=True)
class Field:
    """One key of the metadata line.

    *attr* is the slot attribute that holds the value ("" when none does).
    *line* and *merge* compute the value the full save and the empty-window merge
    write, or :data:`OMIT`; ``None`` means that form never writes the key.
    *purposes* are the reads that take the value back from the line, through
    *read* (an ``apply`` step) or :func:`slot_args` (a constructor keyword); a
    row with *purposes* and no *read* is restored by its group's leading row or
    at construction. *why* states each asymmetry.
    """

    key: str
    purposes: frozenset[str]
    attr: str = ""
    line: Writer | None = None
    merge: Writer | None = None
    read: Callable[[_Read], None] | None = None
    why: str = ""


def _titled(slot: Any) -> bool:
    return bool(slot.title and slot.title != slot.key)


def _remote_bound(slot: Any) -> bool:
    return bool(slot.executor == "remote" and slot.instance_id and slot.remote_slot)


def _truthy(get: Callable[[Any], object]) -> Writer:
    def write(slot: Any, folds: SaveFolds) -> object:
        value = get(slot)
        return value if value else OMIT

    return write


def _flag(get: Callable[[Any], object]) -> Writer:
    def write(slot: Any, folds: SaveFolds) -> object:
        return True if get(slot) else OMIT

    return write


def _always(get: Callable[[Any], object]) -> Writer:
    return lambda slot, folds: get(slot)


def _or_cleared(get: Callable[[Any], object], cleared: object) -> Writer:
    return lambda slot, folds: get(slot) or cleared


def _if_titled(value: Callable[[Any], object]) -> Writer:
    def write(slot: Any, folds: SaveFolds) -> object:
        return value(slot) if _titled(slot) else OMIT

    return write


def _if_bound(value: Callable[[Any], object]) -> Writer:
    def write(slot: Any, folds: SaveFolds) -> object:
        return value(slot) if _remote_bound(slot) else OMIT

    return write


def _named_store(slot: Any, folds: SaveFolds) -> str:
    return named_store_or_empty(slot.memory_store) if folds.memory_mode == "persistent" else ""


# Reads, in the order every purpose applies them.


def _read_title(r: _Read) -> None:
    slot, meta, purpose = r.slot, r.meta, r.purpose
    if isinstance(purpose, Recent):
        _rehydrate_slot_title(
            slot,
            purpose.listing.get("title", purpose.name),  # type: ignore[arg-type]
            titled=bool(purpose.listing.get("title")),
            metadata=meta,
        )
    elif isinstance(purpose, Restore):
        # Not type-checked: a non-string title fails the redaction, and with it
        # the restore, which rolls back its own slot.
        _rehydrate_slot_title(
            slot,
            meta.get("title") or purpose.name,
            titled=bool(meta.get("title")),
            metadata=meta,
        )
    else:
        # PERSISTED METADATA IS AUTHORITATIVE for the title. The sidebar's resume
        # call always sends a ``title`` (``title: title || key``), and that value is
        # client chrome -- often a STALE echo of an older name. A stale echo is
        # indistinguishable from a deliberate override, so the request title is
        # used ONLY when no persisted title exists. A non-string persisted title
        # (legacy or hand-corrupted) reads as absent rather than failing the resume.
        raw = meta.get("title")
        persisted = raw if isinstance(raw, str) else ""
        if persisted:
            _rehydrate_slot_title(slot, persisted, titled=True, metadata=meta)
        elif purpose.request_title:
            # Never-titled session with a caller-supplied name: conservative
            # "user" provenance (the background refresh must never rewrite it)
            # and an epoch bump so any in-flight background attempt stands down.
            slot.title = purpose.request_title
            slot._titled = True
            slot._title_origin = "user"
            slot._title_epoch += 1
        # else: untitled on disk and no caller name -- left untitled, so the
        # auto-titler can still name it on the next turn.


def _read_created_at(r: _Read) -> None:
    slot, meta = r.slot, r.meta
    if meta.get("created_at"):
        slot.created_at = meta["created_at"]
    # The identity of the file this read saw -- lets a later save recognize a
    # file recreated by another writer after a permanent delete (the delete-won
    # guard). Legacy metadata has no ``created_at``: the observation itself is
    # recorded so the guard's missing-file witness still fires for it. An import
    # synthesises its line, so it records no disk identity.
    observed = r.purpose.disk_meta_observed if isinstance(r.purpose, Resume) else True
    slot._disk_meta_created_at = str(meta.get("created_at") or "") if observed else ""
    slot._disk_meta_observed = observed and bool(meta)
    # This slot's memory assignment comes from restored history, not a fresh
    # member selection; the runner reads it when selecting the memory binding.
    slot._memory_assignment_from_history = True


def _read_agent(r: _Read) -> None:
    from kiro_crew.dashboard import chat_persistence as cp  # circular import: facade imports owners

    # A member binding pinned the agent at construction; the line is the
    # operator-editable file that pin must not be re-derived from.
    if r.purpose.member is not None:
        return
    if isinstance(r.purpose, Restore):
        agent = r.purpose.agent
        if agent is None:
            # The session the conversation runs on names the committed choice.
            session = r.meta.get("linked_session_key") or slot_transcript_key(r.purpose.name)
            agent = cp._restored_agent_name(str(session), r.meta)
        r.slot.agent = agent
    elif r.meta.get("agent"):
        r.slot.agent = r.meta["agent"]


def _read_model(r: _Read) -> None:
    # `jev_route` is deliberately NEITHER written nor read by this table. It
    # records an OWNER's pick that spends money -- a routed turn can run on a
    # dearer model -- and transcript metadata is editable by the agent's own file
    # tools, so a value read back from this file would let a prompt-injected
    # agent grant itself routing the owner never selected. The flag lives in
    # memory only: a restart leaves the slot on its persisted model -- the
    # documented refusal -- and the owner re-picks "Auto (Jev)" to route again.
    purpose = r.purpose
    if (
        not _restore_model_fields(
            r.slot, r.meta, cfg=purpose.cfg, effort_marker=purpose.effort_marker
        )
        and r.slot.agent
    ):
        # A legacy line with no ``model``: the agent's configured model.
        try:
            mc = purpose.cfg.agents.get(r.slot.agent) if purpose.cfg else None
            kiro_name = mc.kiro_agent if mc and mc.kiro_agent else r.slot.agent
            r.slot.model = purpose.model_map.get(kiro_name, "")
        except Exception:
            if isinstance(purpose, Recent):
                logger.debug(
                    "Failed to resolve model for restored slot %s", purpose.name, exc_info=True
                )
            else:
                logger.debug(
                    "Failed to resolve model for rehydrated slot %s", purpose.name, exc_info=True
                )


def _read_autocompact_pct(r: _Read) -> None:
    if r.meta.get("autocompact_pct") is not None:
        r.slot.autocompact_pct = _validate_autocompact_pct(r.meta["autocompact_pct"])


def _read_dismissed_source_links(r: _Read) -> None:
    _restore_dismissed_source_links(r.slot, r.meta.get("dismissed_source_links"))


def _read_mode(r: _Read) -> None:
    if r.purpose.member is not None:
        return
    mode = _restored_mode(r.meta.get("mode"))
    if isinstance(r.purpose, Resume):
        # A member mode may not ride a transcript onto an ordinary key; the
        # resume refuses that shape before it builds, so this only defends a
        # same-request inconsistency.
        from kiro_crew import members as members_mod  # members imports artifacts: see cp

        if mode == members_mod.DM_SLOT_MODE:
            return
    if mode:
        r.slot.mode = mode


def _read_workspace(r: _Read) -> None:
    if r.meta.get("workspace"):
        r.slot.workspace = r.meta["workspace"]


def _read_memory_store(r: _Read) -> None:
    if r.meta.get("memory_store"):
        r.slot.memory_store = str(r.meta["memory_store"])


def _read_agent_kind(r: _Read) -> None:
    # The namespace the agent was picked in survives with the pick: a
    # template-picked slot must not come back lighting the same-name member row.
    # Only the two known values are honoured.
    if r.meta.get("agent_kind") in ("member", "template"):
        r.slot.agent_kind = r.meta["agent_kind"]


def _read_project(r: _Read) -> None:
    if r.meta.get("project"):
        r.slot.project = r.meta["project"]


def _read_executor(r: _Read) -> None:
    # The marker is restored INDEPENDENTLY of its target fields. The line is a
    # file, so a truncated write or a hand-edit can leave ``executor="remote"``
    # without a valid instance_id / remote_slot. Dropping the marker then would
    # fail OPEN: the session would come back local and run the crew's turn on
    # THIS machine. Fail CLOSED instead: keep the marker, populate only the valid
    # target fields, and let the incomplete-binding guards refuse the send.
    slot, meta = r.slot, r.meta
    if meta.get("executor") != "remote":
        return
    slot.executor = "remote"
    instance_id, remote_slot = meta.get("instance_id"), meta.get("remote_slot")
    if isinstance(instance_id, str) and instance_id:
        slot.instance_id = instance_id
    if isinstance(remote_slot, str) and remote_slot:
        slot.remote_slot = remote_slot


def _read_turn_in_flight(r: _Read) -> None:
    # RESTORE parses the marker here, in its read order; RECENT and RESUME parse
    # it in ``settle``, after the window, so a marker that fails to parse raises
    # at the same point it always did for each reader.
    from kiro_crew.dashboard import chat_persistence as cp  # circular import: facade imports owners

    if r.purpose.purpose == RESTORE:
        r.applied.turn_generation = cp._local_turn_generation(r.meta)
        r.applied.turn_prompt = cp._local_turn_prompt(r.meta)


def _read_created_by(r: _Read) -> None:
    # Creator attribution restored so the member ownership boundary in
    # session-control authorization survives a restart. ``created_by_sid`` is
    # never restored, and ``_lineage_minted`` stays False: a value read back
    # from this file must not become gateway-authored crew-log lineage.
    if r.meta.get("created_by"):
        r.slot._created_by = str(r.meta["created_by"])


def _read_folder_id(r: _Read) -> None:
    folder_id = r.meta.get("folder_id")
    if not folder_id:
        return
    r.slot.folder_id = folder_id
    purpose = r.purpose
    # A folder deleted since the session was saved leaves the id dangling; a
    # resume drops it only when the un-hide verdict is ABOUT this folder.
    # Holding no verdict for a newly filed id, it is KEPT: a dangling id is
    # visible and self-corrects, whereas erasing a live filing is silent.
    if (
        isinstance(purpose, Resume)
        and not purpose.folder_unhidden
        and folder_id == purpose.folder_checked_id
    ):
        r.slot.folder_id = ""


def _read_channel_folder_filed(r: _Read) -> None:
    if r.meta.get("channel_folder_filed"):
        r.slot._channel_folder_filed = True


def _read_app(r: _Read) -> None:
    if r.meta.get("app"):
        r.slot._app = r.meta["app"]


def _read_artifact(r: _Read) -> None:
    # Re-validated against the slug grammar (the same gate as slot create): the
    # value flows into to_dict()/WS broadcasts to every connected client.
    artifact = r.meta.get("artifact")
    if isinstance(artifact, str) and ARTIFACT_SLUG_RE.match(artifact):
        r.slot._artifact = artifact


def _read_pinned(r: _Read) -> None:
    if r.meta.get("pinned"):
        r.slot.pinned = True


def _read_mutes_opened(r: _Read) -> None:
    if r.meta.get("mutes_opened"):
        r.slot.mutes_opened = True


def _read_color_index(r: _Read) -> None:
    if r.meta.get("color_index") is not None:
        r.slot.color_index = r.meta["color_index"]


def _read_color_hex(r: _Read) -> None:
    color_hex = r.meta.get("color_hex")
    if isinstance(color_hex, str) and COLOR_HEX_RE.match(color_hex):
        r.slot.color_hex = color_hex.lower()


def _read_color_theme(r: _Read) -> None:
    if not r.meta.get("color_theme"):
        return
    r.slot.color_theme = r.meta["color_theme"]
    if isinstance(r.purpose, Resume):
        r.slot.theme_consent = r.meta.get("theme_consent") is True
        # Re-run the fail-closed normalizer so a tampered line can't seed a
        # malformed sha that later crashes the compare.
        r.slot.theme_consent_sha = normalize_theme_consent_sha(r.meta.get("theme_consent_sha"))


def _read_tags(r: _Read) -> None:
    raw = r.meta.get("tags")
    if not isinstance(raw, list):
        return
    slot = r.slot
    slot.tags = [str(t) for t in raw if isinstance(t, str) and t]
    # Prune ids missing from the vocabulary: tag deletion commits the vocab
    # write first (crash-atomic), so a crash mid-delete can leave dangling ids on
    # the line. FAIL-OPEN only when the vocabulary is UNKNOWN (tags.json parse or
    # I/O failure): pruning then would wipe EVERY assignment and the next save
    # persists the loss. A legitimately-empty vocabulary IS authoritative.
    if getattr(r.state, "_tags_authoritative", True):
        known = {t.get("id") for t in r.state._tags}
        slot.tags = [t for t in slot.tags if t in known]
    # "tags changed => revision changed" wherever tags are replaced. A resume
    # rotates through the tag module's helper, which also mints a revision for a
    # duck-typed slot without the method; the startup reads call the method only.
    if r.purpose.purpose == RESUME:
        # circular import: chat_tags imports the facade, which imports this module
        from kiro_crew.dashboard.chat_tags import _bump_slot_tags_revision

        _bump_slot_tags_revision(slot)
        return
    bump_revision = getattr(slot, "bump_tags_revision", None)
    if callable(bump_revision):
        bump_revision()


def _read_auto_tagged(r: _Read) -> None:
    if r.meta.get("auto_tagged"):
        r.slot._auto_tagged = True


def _read_human_seen(r: _Read) -> None:
    # Attendance survives the restart, so an app-owned tab a person has been
    # working in keeps the full approval window instead of dropping to the
    # unattended deny-fast.
    if r.meta.get("human_seen"):
        r.slot._human_seen = True


def _read_deferred_notes(r: _Read) -> None:
    # Sanitized and bounded by the DURABLE CEILING; entries whose delivered row
    # is already committed are dropped by ``settle``, against the loaded rows.
    notes = sanitize_restored_deferred_notes(r.meta.get("deferred_notes"))
    r.applied.restored_notes = notes
    if notes:
        r.slot._deferred_notes = notes


def _read_queued_prompts(r: _Read) -> None:
    # Handed back as queue cards: the user's own words, admitted while a turn
    # was running and never dispatched. Nothing drains an idle slot on boot, so
    # they wait for the user to send, edit or delete them.
    slot = r.slot
    queue = sanitize_restored_queue(r.meta.get("queued_prompts"))
    if queue:
        slot._queue[:] = queue
        logger.info("Restored %d queued prompt(s) for slot %s", len(queue), r.purpose.name)
    # Stamped whatever was restored, so the first flush re-persists only a queue
    # that actually changed.
    slot._queue_persisted_sig = queue_persist_signature(slot.durable_queue_entries())


def _read_memory_mode(r: _Read) -> None:
    # Copied as written: the fork path judges an unrecognized value itself.
    mode = r.meta.get("memory_mode", "persistent")
    r.slot.memory_mode = mode
    name = r.slot.key if isinstance(r.purpose, Resume) else r.purpose.name
    restricted = f"dashboard:{name}"
    if mode != "persistent":
        r.state._restricted_keys.add(restricted)
    elif isinstance(r.purpose, Resume):
        r.state._restricted_keys.discard(restricted)


def _read_forked_from(r: _Read) -> None:
    if r.meta.get("forked_from") is not None:
        r.slot.forked_from = r.meta["forked_from"]


def _read_linked_session_key(r: _Read) -> None:
    # Rebinds the slot to the session its conversation runs on; skipped, the
    # slot would answer from a dashboard-only session and the channel thread
    # would stop seeing its replies.
    slot, meta, purpose = r.slot, r.meta, r.purpose
    if meta.get("linked_session_key"):
        slot.linked_session_key = str(meta["linked_session_key"])
    elif isinstance(purpose, Recent):
        if is_channel_session_key(purpose.history_key) and r.state.sessions:
            # Resolved from the session map, never derived from the filename:
            # the ``:``-to-``_`` fold is not reversible.
            real_key = r.state.sessions.channel_key_for_stem(purpose.history_key)
            if real_key:
                slot.linked_session_key = real_key


def _read_tab_id(r: _Read) -> None:
    # The persisted tab id keeps the fork chain ``read_messages_chained`` walks
    # across restarts; a slot left on its constructor's fresh id would persist
    # that id back and sever the ancestry. A line with none gets one minted,
    # which the reader persists after its transcript read.
    tab_id = r.meta.get("tab_id")
    if not tab_id:
        tab_id = uuid.uuid4().hex[:12]
        r.applied.minted_tab_id = tab_id
    r.slot._tab_id = tab_id


_ALL = PURPOSES

#: The field × purpose table, in the order a read applies it.
FIELDS: tuple[Field, ...] = (
    Field("_type", frozenset(), line=lambda s, f: "metadata"),
    Field(
        "title",
        _ALL,
        attr="title",
        line=_if_titled(lambda s: s.title),
        merge=lambda s, f: s.title if _titled(s) else "",
        read=_read_title,
        why=(
            "each purpose takes the title from a different source: RESTORE the line "
            "(an untitled line restores untitled), RECENT the session list (always "
            "titled), RESUME the line or the caller's name"
        ),
    ),
    Field(
        "title_origin",
        _ALL,
        attr="_title_origin",
        line=_if_titled(lambda s: getattr(s, "_title_origin", "") or OMIT),
        merge=_if_titled(lambda s: getattr(s, "_title_origin", "") or OMIT),
        why="restored with the title; RESUME only beside a persisted title",
    ),
    Field(
        "title_refresh_mark",
        _ALL,
        attr="_title_refresh_mark",
        line=_if_titled(lambda s: getattr(s, "_title_refresh_mark", 0) or OMIT),
        merge=_if_titled(lambda s: getattr(s, "_title_refresh_mark", 0) or OMIT),
        why="restored with the title; RESUME only beside a persisted title",
    ),
    # Written unconditionally beside a title: ``_persist_title`` is the primary
    # writer but returns False without retry on a transient failure, so a save must
    # land the CURRENT boolean either way -- a skipped True loses the turn-one
    # refresh after restart, and a skipped False re-arms it.
    Field(
        "title_low_signal",
        _ALL,
        attr="_title_low_signal",
        line=_if_titled(lambda s: bool(getattr(s, "_title_low_signal", False))),
        merge=_if_titled(lambda s: bool(getattr(s, "_title_low_signal", False))),
        why="restored with the title; RESUME only beside a persisted title",
    ),
    Field(
        "created_at", _ALL, attr="created_at", line=lambda s, f: f.created_at, read=_read_created_at
    ),
    Field("last_consolidated", frozenset(), line=lambda s, f: f.last_consolidated),
    Field(
        "agent",
        _ALL,
        attr="agent",
        line=_truthy(lambda s: s.agent),
        merge=_truthy(lambda s: s.agent),
        read=_read_agent,
        why=(
            "RESTORE and RECENT restore the committed choice, not the line's; RESUME "
            "takes the line's (History resume then restores the committed choice "
            "after the build). A member binding pins it for every purpose"
        ),
    ),
    Field(
        "model",
        _STARTUP,
        attr="model",
        line=_always(lambda s: s.model),
        merge=_always(lambda s: s.model),
        read=_read_model,
        why=(
            "RESUME restores no model: History resume restores it after the build "
            "(``_restore_model_fields``) and an import carries none"
        ),
    ),
    Field(
        "reasoning_effort",
        _STARTUP,
        attr="reasoning_effort",
        line=_truthy(lambda s: s.reasoning_effort),
        merge=_or_cleared(lambda s: s.reasoning_effort, ""),
        why="restored with the model",
    ),
    Field(
        "autocompact_pct",
        _ALL,
        attr="autocompact_pct",
        line=_always(lambda s: s.autocompact_pct),
        merge=_always(lambda s: s.autocompact_pct),
        read=_read_autocompact_pct,
    ),
    Field(
        "dismissed_source_links",
        _ALL,
        attr="_dismissed_source_links",
        line=lambda s, f: (
            f.dismissed_source_links if f.dismissed_source_links is not None else OMIT
        ),
        merge=lambda s, f: (
            f.dismissed_source_links if f.dismissed_source_links is not None else OMIT
        ),
        read=_read_dismissed_source_links,
    ),
    Field(
        "workspace",
        _ALL,
        attr="workspace",
        line=_truthy(lambda s: s.workspace if s.workspace != "default" else ""),
        merge=_truthy(lambda s: s.workspace),
        read=_read_workspace,
        why="the full save omits the default workspace; the merge writes it verbatim",
    ),
    # A restricted line names no store: the name is what ``read_session_execution``
    # reads as an owner claim when the line carries no execution carrier, which a
    # restricted session never writes, so a store here would make the restart
    # refuse the chat as a legacy member record. Clearable in the merge, so a crew
    # rebound to the default store stops consolidating into the silo it left.
    Field(
        "memory_store",
        _STARTUP,
        attr="memory_store",
        line=lambda s, f: _named_store(s, f) or OMIT,
        merge=_named_store,
        read=_read_memory_store,
        why=(
            "RESUME does not restore it, so the next full save after a History "
            "resume clears it (#17025). A restricted line names no store"
        ),
    ),
    Field(
        "agent_kind",
        _ALL,
        attr="agent_kind",
        line=_truthy(lambda s: s.agent_kind),
        merge=_always(lambda s: s.agent_kind),
        read=_read_agent_kind,
    ),
    Field(
        "project",
        _ALL,
        attr="project",
        line=_truthy(lambda s: s.project),
        merge=_always(lambda s: s.project),
        read=_read_project,
    ),
    # Written while a local turn is between admission and teardown and omitted
    # otherwise -- slot ownership makes the omission the durable clear; the merge,
    # which cannot delete a key, writes the cleared ``0`` / ``None``.
    Field(
        "turn_in_flight_generation",
        _ALL,
        attr="_turn_in_flight_generation",
        line=lambda s, f: (
            s._turn_in_flight_generation if s._turn_in_flight_generation > 0 else OMIT
        ),
        merge=_always(lambda s: s._turn_in_flight_generation),
        read=_read_turn_in_flight,
        why="reconciled by ``AppliedMeta.settle`` once the window is loaded",
    ),
    Field(
        "turn_in_flight_prompt",
        _ALL,
        attr="_turn_in_flight_prompt",
        line=lambda s, f: (
            s._turn_in_flight_prompt
            if s._turn_in_flight_generation > 0 and s._turn_in_flight_prompt is not None
            else OMIT
        ),
        merge=_always(lambda s: s._turn_in_flight_prompt),
        why="read with the generation",
    ),
    # The binding is written whole or not at all: a half binding (``executor`` remote
    # with no peer slot) is the fail-closed refusal case, so the marker without its
    # target would resurrect a session that can never run.
    Field(
        "executor",
        frozenset({RESTORE, RECENT}),
        attr="executor",
        line=_if_bound(lambda s: "remote"),
        merge=_if_bound(lambda s: "remote"),
        read=_read_executor,
        why=(
            "both startup restores keep the marker, so an old relay chat comes back "
            "a read-only archive, never a local chat (#10826); RESUME deliberately "
            "does not, and the session comes back local"
        ),
    ),
    Field(
        "instance_id",
        frozenset({RESTORE, RECENT}),
        attr="instance_id",
        line=_if_bound(lambda s: s.instance_id),
        merge=_if_bound(lambda s: s.instance_id),
        why="part of the remote binding",
    ),
    Field(
        "remote_slot",
        frozenset({RESTORE, RECENT}),
        attr="remote_slot",
        line=_if_bound(lambda s: s.remote_slot),
        merge=_if_bound(lambda s: s.remote_slot),
        why="part of the remote binding",
    ),
    Field(
        "mode",
        _ALL,
        attr="mode",
        line=_truthy(lambda s: s.mode),
        merge=_or_cleared(lambda s: s.mode, ""),
        read=_read_mode,
        why="a retired mode reads as plain chat; RESUME also refuses the member mode",
    ),
    # Creator attribution: the member ownership boundary in session-control
    # authorization reads it, so dropping it would orphan a member's workers on
    # restart. ``_created_by_sid`` is never persisted (lineage is process-local).
    Field(
        "created_by",
        _STARTUP,
        attr="_created_by",
        line=_truthy(lambda s: getattr(s, "_created_by", "")),
        merge=_truthy(lambda s: getattr(s, "_created_by", "")),
        read=_read_created_by,
        why=(
            "RESUME does not restore it (#17025); a session-control revive restores "
            "it after the build when lineage corroborates"
        ),
    ),
    Field(
        "folder_id",
        _ALL,
        attr="folder_id",
        line=_truthy(lambda s: s.folder_id),
        merge=_or_cleared(lambda s: s.folder_id, ""),
        read=_read_folder_id,
    ),
    # Sticky, and carried from disk as well as the slot: a restore that failed to
    # set the in-memory flag must not erase the marker and get the conversation
    # re-filed.
    Field(
        "channel_folder_filed",
        _ALL,
        attr="_channel_folder_filed",
        line=lambda s, f: True if f.channel_folder_filed else OMIT,
        merge=_flag(lambda s: s._channel_folder_filed),
        read=_read_channel_folder_filed,
        why="sticky: the full save also carries the disk's marker forward",
    ),
    Field(
        "app",
        _STARTUP,
        attr="_app",
        line=_truthy(lambda s: s._app),
        merge=_truthy(lambda s: s._app),
        read=_read_app,
        why=(
            "RESUME takes the app from the request, not the line, so a dashboard "
            "user's resume of an app session clears it on the next full save "
            "(#17025); a hooked revive restores the line's after the build"
        ),
    ),
    # Round-trips with ``app``: an untagged restore falls back to the fail-closed
    # empty sentinel, and a cron slot must keep the CRON tag that keeps it out of
    # ``slots:user``.
    Field(
        "origin",
        _ALL,
        attr="_origin",
        line=_truthy(lambda s: s._origin),
        merge=_truthy(lambda s: s._origin),
        why=(
            "applied at construction (``slot_args``), as ``str(...)``: a null origin "
            'reads as the string "None" (#17026)'
        ),
    ),
    # The artifact companion binding: a bound session restored after a gateway
    # restart comes back as the artifact's active bound session.
    Field(
        "artifact",
        _STARTUP,
        attr="_artifact",
        line=_truthy(lambda s: s._artifact),
        merge=_or_cleared(lambda s: s._artifact, ""),
        read=_read_artifact,
        why=(
            "RESUME does not restore it, so the next full save after a History "
            "resume clears it (#17025)"
        ),
    ),
    Field(
        "pinned",
        _ALL,
        attr="pinned",
        line=_flag(lambda s: s.pinned),
        merge=_always(lambda s: bool(s.pinned)),
        read=_read_pinned,
    ),
    Field(
        "mutes_opened",
        _ALL,
        attr="mutes_opened",
        # Prefer the staged fold value when the save supplies one (the mute
        # endpoint persisting its NEW value while the live flag still holds the
        # committed value); otherwise read the live slot, as every other save
        # does. Same choice in both forms so the full line and the empty-window
        # merge agree on what reaches disk.
        line=lambda s, f: (
            True if (s.mutes_opened if f.mutes_opened is None else f.mutes_opened) else OMIT
        ),
        merge=lambda s, f: bool(s.mutes_opened if f.mutes_opened is None else f.mutes_opened),
        read=_read_mutes_opened,
    ),
    Field(
        "color_index",
        _ALL,
        attr="color_index",
        line=lambda s, f: s.color_index if s.color_index is not None else OMIT,
        merge=_always(lambda s: s.color_index),
        read=_read_color_index,
    ),
    Field(
        "color_hex",
        _ALL,
        attr="color_hex",
        line=_truthy(lambda s: s.color_hex),
        merge=_or_cleared(lambda s: s.color_hex, ""),
        read=_read_color_hex,
    ),
    Field(
        "color_theme",
        frozenset({RECENT, RESUME}),
        attr="color_theme",
        line=_truthy(lambda s: s.color_theme),
        merge=_or_cleared(lambda s: s.color_theme, ""),
        read=_read_color_theme,
        why="RESTORE does not restore it, so the next full save of an open tab clears it (#17025)",
    ),
    Field(
        "theme_consent",
        frozenset({RESUME}),
        attr="theme_consent",
        read=None,
        why="no save writes it; RESUME reads it beside the theme",
    ),
    Field(
        "theme_consent_sha",
        frozenset({RESUME}),
        attr="theme_consent_sha",
        read=None,
        why="no save writes it; RESUME reads it beside the theme",
    ),
    Field(
        "tags",
        _ALL,
        attr="tags",
        line=_truthy(lambda s: list(s.tags)),
        merge=_always(lambda s: list(s.tags)),
        read=_read_tags,
    ),
    # Once-flags (this and ``human_seen``): written when set, never cleared, and
    # outside the slot-owned key set, so they survive on disk even through a save by
    # a slot that has not learned them. Without ``auto_tagged`` a restart re-runs
    # the auto-tagger and re-adds a tag the user removed; without ``human_seen`` an
    # app-owned tab a person works in drops to the unattended deny-fast.
    Field(
        "auto_tagged",
        _ALL,
        attr="_auto_tagged",
        line=_flag(lambda s: getattr(s, "_auto_tagged", False)),
        merge=_flag(lambda s: getattr(s, "_auto_tagged", False)),
        read=_read_auto_tagged,
    ),
    Field(
        "human_seen",
        _STARTUP,
        attr="_human_seen",
        line=_flag(lambda s: getattr(s, "_human_seen", False)),
        merge=_flag(lambda s: getattr(s, "_human_seen", False)),
        read=_read_human_seen,
        why="RESUME does not restore it (#17025); the key is unowned, so it survives on disk",
    ),
    Field(
        "last_user_at",
        frozenset(),
        line=lambda s, f: f.last_user_at or OMIT,
        why="a ranking signal list readers use",
    ),
    Field(
        "deferred_notes",
        _STARTUP,
        attr="_deferred_notes",
        line=lambda s, f: f.deferred_notes or OMIT,
        merge=lambda s, f: f.deferred_notes,
        read=_read_deferred_notes,
        why="RESUME does not replay the held notes (#17025); a later save keeps them on disk",
    ),
    Field(
        "queued_prompts",
        _STARTUP,
        attr="_queue",
        line=lambda s, f: f.queued_prompts or OMIT,
        merge=lambda s, f: f.queued_prompts,
        read=_read_queued_prompts,
        why=(
            "RESUME does not hand them back, so the next full save after a History "
            "resume clears them (#17025)"
        ),
    ),
    Field(
        "memory_mode",
        _ALL,
        attr="memory_mode",
        line=lambda s, f: f.memory_mode,
        merge=lambda s, f: f.memory_mode,
        read=_read_memory_mode,
        why="RESUME also clears a stale restricted mark when the line is persistent",
    ),
    Field(
        "forked_from",
        _ALL,
        attr="forked_from",
        line=lambda s, f: s.forked_from if s.forked_from is not None else OMIT,
        merge=lambda s, f: s.forked_from if s.forked_from is not None else OMIT,
        read=_read_forked_from,
    ),
    # Nothing re-creates a channel slot's binding on restart (no injection
    # re-fires), so without it the slot comes back unbound, as a dashboard-only
    # copy of the thread.
    Field(
        "linked_session_key",
        _STARTUP,
        attr="linked_session_key",
        line=_truthy(lambda s: s.linked_session_key),
        merge=_truthy(lambda s: s.linked_session_key),
        read=_read_linked_session_key,
        why="RESUME does not restore it (a hooked revive restores it after the build)",
    ),
    # Durable provenance: a name is not evidence, so this flag is what tells a
    # later boot the tab was adopted from a channel conversation.
    Field(
        "channel_origin",
        frozenset({RESTORE}),
        attr="channel_origin",
        line=_flag(lambda s: getattr(s, "channel_origin", False)),
        merge=_flag(lambda s: getattr(s, "channel_origin", False)),
        why=(
            "applied at construction, together with a persisted link. RESUME derives "
            "it from the transcript key instead; RECENT restores only dashboard keys"
        ),
    ),
    Field(
        "tab_id",
        _STARTUP,
        attr="_tab_id",
        line=lambda s, f: f.tab_id or OMIT,
        merge=_truthy(lambda s: getattr(s, "_tab_id", None)),
        read=_read_tab_id,
        why=(
            "RESUME keeps the constructor's fresh id, which the next save writes over "
            "the persisted one and so severs the fork chain (#17025). The full save "
            "also carries the disk's id forward"
        ),
    ),
    Field(
        "closed",
        frozenset(),
        line=lambda s, f: True if f.closed else OMIT,
        merge=lambda s, f: True if f.closed else OMIT,
        why="a closed line is screened out before a read, or cleared by a resume",
    ),
    # When the user closed the tab (a save-time fallback covers callers with no
    # gesture); the channel reconciler compares channel activity against it.
    Field(
        "closed_at",
        frozenset(),
        line=lambda s, f: f.closed_at if f.closed else OMIT,
        merge=lambda s, f: f.closed_at if f.closed else OMIT,
    ),
    Field(
        "rotation_generation",
        frozenset(),
        line=lambda s, f: f.rotation_generation if f.rotation_generation is not None else OMIT,
        why="advanced by a save that edits the conversation",
    ),
)

_BY_KEY: dict[str, Field] = {row.key: row for row in FIELDS}

#: The full save's key order. The line is serialized as built, so this is its bytes.
LINE_ORDER: tuple[str, ...] = (
    "_type",
    "created_at",
    "last_consolidated",
    "closed",
    "closed_at",
    "memory_mode",
    "title",
    "title_origin",
    "title_refresh_mark",
    "title_low_signal",
    "agent",
    "model",
    "reasoning_effort",
    "autocompact_pct",
    "dismissed_source_links",
    "mode",
    "workspace",
    "memory_store",
    "agent_kind",
    "project",
    "executor",
    "instance_id",
    "remote_slot",
    "turn_in_flight_generation",
    "turn_in_flight_prompt",
    "folder_id",
    "channel_folder_filed",
    "app",
    "origin",
    "created_by",
    "artifact",
    "pinned",
    "mutes_opened",
    "color_index",
    "color_hex",
    "color_theme",
    "tags",
    "auto_tagged",
    "human_seen",
    "last_user_at",
    "deferred_notes",
    "queued_prompts",
    "forked_from",
    "linked_session_key",
    "channel_origin",
    "tab_id",
    "rotation_generation",
)

#: The empty-window merge's key order. A merge updates the line in place, so a key
#: already on disk keeps its place and a new one is appended in this order.
MERGE_ORDER: tuple[str, ...] = (
    "folder_id",
    "tags",
    "pinned",
    "mutes_opened",
    "mode",
    "artifact",
    "reasoning_effort",
    "color_index",
    "color_hex",
    "color_theme",
    "memory_mode",
    "model",
    "queued_prompts",
    "autocompact_pct",
    "title",
    "title_origin",
    "title_refresh_mark",
    "title_low_signal",
    "agent",
    "workspace",
    "memory_store",
    "agent_kind",
    "project",
    "app",
    "origin",
    "created_by",
    "linked_session_key",
    "channel_origin",
    "forked_from",
    "turn_in_flight_generation",
    "turn_in_flight_prompt",
    "executor",
    "instance_id",
    "remote_slot",
    "tab_id",
    "auto_tagged",
    "human_seen",
    "channel_folder_filed",
    "closed",
    "closed_at",
    "dismissed_source_links",
    "deferred_notes",
)


# ── The interface ─────────────────────────────────────────────────────────────


def encode(slot: _ChatSlot, *, folds: SaveFolds, merge: bool = False) -> dict:
    """The metadata line *slot* writes: the full save's, or the merge's fields.

    Pure: reads slot state and the save's *folds* and returns a new dict in the
    form's key order. The full save's line is
    complete apart from the keys another layer owns, which the save carries
    from disk afterwards; the merge's fields are applied over the line on disk.
    """
    out: dict = {}
    for key in MERGE_ORDER if merge else LINE_ORDER:
        row = _BY_KEY[key]
        write = row.merge if merge else row.line
        if write is None:
            continue
        value = write(slot, folds)
        if value is not OMIT:
            out[key] = value
    return out


def slot_args(meta: dict[str, Any], purpose: Purpose) -> dict[str, Any]:
    """The ``get_or_create_slot`` keywords *purpose* takes from *meta*.

    Identity the constructor itself must see. A member binding admits the key
    through the constructor's member-key reservation and pins the agent. The
    origin is the persisted conversation's, never re-derived: a cron slot must
    not come back as the user's.
    """
    member = purpose.member
    args: dict[str, Any] = {
        "agent": member[0] if member else "",
        "mode": member[1] if member else "",
    }
    if isinstance(purpose, Resume):
        args["app"] = purpose.app
        # Resuming a channel transcript adopts that conversation, so the tab is
        # channel-origin even when the session map cannot name its session.
        args["channel_origin"] = is_channel_session_key(purpose.history_key)
    else:
        args["app"] = meta.get("app", "")
        if purpose.purpose == RESTORE:
            # PERSISTED provenance only: a name is not evidence.
            args["channel_origin"] = bool(meta.get("channel_origin")) or bool(
                meta.get("linked_session_key")
            )
    args["origin"] = str(meta.get("origin", ""))
    return args


@dataclass
class AppliedMeta:
    """What :func:`apply` read for its caller to act on, and the window step.

    *meta* is the line :func:`apply` read; *turn_generation* / *turn_prompt* are
    the local-turn crash marker (parsed by ``apply`` for RESTORE, by ``settle``
    otherwise); *minted_tab_id* is a tab id the
    line lacked, for the reader to persist after its transcript read.
    """

    slot: Any
    purpose: Any
    meta: dict[str, Any] = field(default_factory=dict)
    turn_generation: int = 0
    turn_prompt: dict | None = None
    minted_tab_id: str | None = None
    restored_notes: list[dict] = field(default_factory=list)

    def settle(self, persisted: list[dict] | None) -> bool:
        """Finish the fields whose read needs the loaded window.

        Call once the window is appended and ``_disk_window_len`` set. Drops the
        restored held notes whose delivered row *persisted* already holds (and
        records their ids, so the next full save retires them row-lessly),
        reconciles the local-turn marker against *persisted* -- every on-disk
        row, not just the window -- and then re-bases the title refresh mark,
        in that order. Returns whether a local-turn marker was present.
        """
        slot = self.slot
        if self.purpose.purpose in _STARTUP and self.restored_notes and persisted is not None:
            pending = slot._deferred_notes
            slot._deferred_notes = drop_committed_restored_notes(persisted, pending)
            slot._dropped_note_ids.update(
                committed_filtered_note_ids(pending, slot._deferred_notes)
            )
        from kiro_crew.dashboard import chat_persistence as cp  # circular import

        if self.purpose.purpose != RESTORE:
            self.turn_generation = cp._local_turn_generation(self.meta)
            self.turn_prompt = cp._local_turn_prompt(self.meta)
        had_marker = cp._reconcile_local_turn_marker(
            slot, self.turn_generation, self.turn_prompt, persisted=persisted
        )
        _rebase_rehydrated_refresh_mark(slot)
        return had_marker


def apply(
    state: DashboardState, slot: _ChatSlot, meta: dict[str, Any], purpose: Purpose
) -> AppliedMeta:
    """Read *meta* onto the freshly constructed *slot* for *purpose*.

    Applies every row whose ``purposes`` include *purpose*, in :data:`FIELDS`
    order, then re-seeds the session's compaction override (after the link, so a
    channel-born slot seeds the session its turns run on). Synchronous and
    loop-affine like the slot it writes. Raises whatever a value check raises; the
    reader's own rollback owns the slot it created.
    """
    applied = AppliedMeta(slot=slot, purpose=purpose, meta=meta)
    read = _Read(state=state, slot=slot, meta=meta, purpose=purpose, applied=applied)
    for row in FIELDS:
        if row.read is not None and purpose.purpose in row.purposes:
            row.read(read)
    # The SessionManager's override map is process-local, so a hydrated slot
    # pushes its persisted value back or the session compacts at the global one.
    if slot.autocompact_pct is not None and state.sessions:
        state.sessions.set_autocompact_pct(effective_session_key(slot), slot.autocompact_pct)
    return applied
