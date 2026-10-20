"""The durable sidebar stores: chat folders, cron folders, chat pins, pinned order, tags, boards.

Each store's load, serialised read-modify-write and confirmed write under the
data home, the strict JSON writers they share, and the durable tag snapshot.

Instance fields of ``DashboardState`` this module reads and writes:

Writes: ``_chat_pins``, ``_cron_folders``, ``_folders``, ``_folders_generation``,
``_pinned_order_rev``, ``_pinned_session_order``, ``_pinned_session_order_loaded``,
``_pinned_session_order_stored``, ``_tag_boards``, ``_tags``, ``_tags_authoritative``,
``_unparsed_chat_pin_entries``, ``_unparsed_cron_folder_entries``,
``_unparsed_tag_board_entries``, ``_unparsed_tag_entries``.
Reads only: ``_CHAT_PINS_FILE``, ``_CRON_FOLDERS_FILE``, ``_DEFAULT_TAGS``,
``_FOLDERS_FILE``, ``_TAGS_FILE``, ``_TAG_BOARDS_FILE``, ``_chat_pins_lock``,
``_folders_lock``, ``_slots``, ``crons``.

A function whose first parameter is ``self`` is a ``DashboardState`` method: the
class binds it under the same name (``state_parts/dashboard_state.py``), so it reads
and writes the state's own fields and calls its siblings through ``self``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, TypeVar

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir
from kiro_crew.dashboard import pinned_session_order
from kiro_crew.dashboard.chat_tag_grants import seed_default_grants, seed_status_identity_rows
from kiro_crew.dashboard.folder_repository import FolderRepository
from kiro_crew.dashboard.state_parts.slot_sidebar import ensure_tags_revision_epoch
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink

if TYPE_CHECKING:
    from kiro_crew.dashboard.state_parts.dashboard_state import DashboardState

logger = logging.getLogger("kiro_crew.dashboard.state")


_FOLDER_REPOSITORY = FolderRepository(lambda: logger)


#: Return type of a mutate_folders callback.
_T = TypeVar("_T")


def _safe_folder_tree(folders: object) -> list[dict[str, Any]]:
    """Well-formed folder entries safe to ship on the slots broadcast frame.

    The loader validates disk rows, but lightweight states and test doubles can
    bypass it. Keep the broadcast hot path tolerant of an unset or malformed
    in-memory value.
    """
    if not isinstance(folders, list):
        return []
    return [f for f in folders if isinstance(f, dict) and isinstance(f.get("id"), str)]


@dataclass(frozen=True)
class _DurableTagSnapshot:
    """A positively read tag snapshot, or positive absence when ``present`` is false."""

    present: bool
    tags: list[dict[str, Any]]
    unparsed: list[Any]


def load_folders(self: DashboardState) -> None:
    """Load usable folder definitions without replacing good state on failure."""
    path = config_dir() / self._FOLDERS_FILE
    self._folders = _FOLDER_REPOSITORY.load(path, self._folders)


def save_folders(self: DashboardState) -> None:
    """Persist folder definitions for synchronous boot-time callers."""
    path = config_dir() / self._FOLDERS_FILE
    _FOLDER_REPOSITORY.save(path, self._folders, self._atomic_write_json)


def load_cron_folders(self: DashboardState) -> None:
    """Load cron folder definitions from disk.

    Validates the loaded shape: the file must contain a JSON array of
    folder objects. A non-list root (a hand-edited ``{}``, a string) is
    ignored wholesale — it would crash frontend grouping
    (``folders.map is not a function``). Individual malformed entries are
    dropped from the active list but kept verbatim in
    ``_unparsed_cron_folder_entries`` so the next ``save_cron_folders``
    round-trips them back to disk rather than silently erasing a user's
    hand-edited-but-typo'd folder (mirrors the hooks store's contract).
    """
    path = config_dir() / self._CRON_FOLDERS_FILE
    try:
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, list):
                logger.warning(
                    "Ignoring %s: expected a JSON array, got %s",
                    self._CRON_FOLDERS_FILE,
                    type(loaded).__name__,
                )
                return

            def _is_valid(f: Any) -> bool:
                return (
                    isinstance(f, dict)
                    and isinstance(f.get("id"), str)
                    and bool(f.get("id"))
                    and isinstance(f.get("name"), str)
                    and bool(f.get("name"))
                    and isinstance(f.get("order"), (int, float))
                    and not isinstance(f.get("order"), bool)
                )

            valid, unparsed = self._partition_preserving(
                loaded, _is_valid, "entr(ies)", self._CRON_FOLDERS_FILE
            )
            self._cron_folders = valid
            self._unparsed_cron_folder_entries = unparsed
    except Exception:
        logger.warning("Failed to load cron folders", exc_info=True)


def _persist_cron_folders(self: DashboardState, folders: list[dict[str, Any]]) -> None:
    """Atomically write ``folders`` to the cron-folders file.

    Takes the list to persist explicitly so a caller can save a candidate
    list before committing it to ``_cron_folders`` (see ``create_cron_folder``),
    keeping in-memory state and disk from diverging mid-operation. This is the
    single persist chokepoint for all folder mutations: ``create_cron_folder``
    passes a candidate list here before committing it, while ``save_cron_folders``
    (used by ``rename_cron_folder`` and ``delete_cron_folder``) passes the
    already-mutated ``_cron_folders`` to commit an in-place edit. Any
    malformed entries preserved at load time (``_unparsed_cron_folder_entries``)
    are appended, so a save cannot erase bytes a hand-edit left in a shape
    the loader could not validate.
    """
    path = config_dir() / self._CRON_FOLDERS_FILE
    unparsed = getattr(self, "_unparsed_cron_folder_entries", [])
    self._atomic_write_json_strict(path, [*folders, *unparsed])


def save_cron_folders(self: DashboardState) -> None:
    """Persist cron folder definitions to disk (atomic write).

    Writes the active folders plus any malformed entries preserved at load
    time (``_unparsed_cron_folder_entries``), so a save triggered by an
    unrelated folder operation cannot erase bytes a hand-edit left in a
    shape this loader could not validate. Raises on I/O failure so callers
    can surface a 500 to the client rather than silently losing the write.
    """
    self._persist_cron_folders(self._cron_folders)


def create_cron_folder(self: DashboardState, name: str, folder_id: str) -> dict:
    """Create a new cron folder and persist.

    Returns the created folder dict. Raises ValueError if ``folder_id``
    collides with an existing folder (``rename``/``delete`` act on the
    first id match, so a duplicate would strand the shadowed folder as
    un-renameable/un-deletable). Raises on persistence failure (callers
    should surface a 500).

    The folder is persisted BEFORE it is exposed in ``_cron_folders``: a
    concurrent ``GET /api/cron-folders`` reads the live list, so appending
    first and saving second would let a reader observe (and the frontend
    render) a folder that a failed save then removes — a transient "ghost"
    folder inconsistent with disk. Building the candidate list, persisting
    it, and only then committing the reference means a reader sees either
    the pre-create list or the durably-saved one, never an intermediate.
    """
    if any(f["id"] == folder_id for f in self._cron_folders):
        raise ValueError("cron folder id collision")
    order = max((f["order"] for f in self._cron_folders), default=-1) + 1
    folder = {"id": folder_id, "name": name, "order": order}
    candidate = [*self._cron_folders, folder]
    self._persist_cron_folders(candidate)
    self._cron_folders = candidate
    return folder


def rename_cron_folder(self: DashboardState, folder_id: str, name: str) -> dict | None:
    """Rename a cron folder and persist.

    Returns the updated folder dict, or None if folder_id not found.
    Raises on persistence failure (callers should surface a 500);
    original name is restored on failure.
    """
    for folder in self._cron_folders:
        if folder["id"] == folder_id:
            old_name = folder["name"]
            folder["name"] = name
            try:
                self.save_cron_folders()
            except Exception:
                folder["name"] = old_name
                raise
            return folder
    return None


def delete_cron_folder(self: DashboardState, folder_id: str) -> bool:
    """Remove a cron folder and clear its assignment on all jobs.

    Returns True if the folder existed, False otherwise.
    Raises on persistence failure (callers should surface a 500).

    Ordering: the folder removal is the single authoritative write —
    it is removed from memory and persisted FIRST (rolled back in
    memory if the save fails, keeping memory consistent with disk).
    Job ``folder_id`` clears happen afterwards as best-effort cleanup:
    a dangling ``folder_id`` is benign (grouping renders unknown ids
    in the Ungrouped bucket, and a job's next folder move overwrites
    it), so a crash or per-job failure between writes can never strand
    jobs in a half-deleted state — the folder is either fully present
    or fully gone.
    """
    if not any(f["id"] == folder_id for f in self._cron_folders):
        return False
    # Remove the folder definition and persist — the one write that
    # decides whether the delete happened.
    snapshot = list(self._cron_folders)
    self._cron_folders = [f for f in self._cron_folders if f["id"] != folder_id]
    try:
        self.save_cron_folders()
    except Exception:
        self._cron_folders = snapshot
        raise
    # Best-effort: clear the now-dangling folder_id on affected jobs.
    # Failures are logged and tolerated — consumers treat an unknown
    # folder_id as ungrouped, so a leftover id has no user-visible
    # effect and self-heals on the job's next folder assignment.
    for job in self.crons.list_jobs(include_disabled=True):
        if job.folder_id == folder_id:
            try:
                self.crons.update_job(job.id, folder_id="")
            except Exception:
                logger.warning(
                    "Failed to clear folder_id on job %s after folder delete "
                    "(benign: unknown ids render as ungrouped)",
                    job.id,
                    exc_info=True,
                )
    return True


# ── Pinned session order ──


def read_pinned_session_order(self: DashboardState) -> tuple[list[str], bool] | None:
    """Read the stored pinned-session order. Blocking; run off the loop.

    Returns ``(keys, stored)``: ``stored`` says whether an order file
    exists at all, which an empty ``keys`` cannot tell. A person who
    reordered and then unpinned everything has a stored empty order; one
    who never reordered has none. Returns ``None`` on a transient
    ``OSError`` (logged): the order is a display preference, so an
    unreadable file must not replace a good order with an empty one.
    """
    path = pinned_session_order.store_path(config_dir())
    try:
        return pinned_session_order.load_stored(path)
    except OSError:
        logger.warning(
            "could not read %s; keeping the current pinned order",
            pinned_session_order.PINNED_ORDER_FILE,
            exc_info=True,
        )
        return None


def adopt_pinned_session_order(self: DashboardState, read: tuple[list[str], bool] | None) -> bool:
    """Take a read's order as the in-memory one unless one is already loaded.

    Runs on the loop. The gateway reads the file after it starts serving,
    so a reorder can land first; that write is newer than what was read,
    and this returns ``False`` without touching it. A failed read
    (``None``) leaves the order unloaded so the next writer reads again.
    """
    if self._pinned_session_order_loaded or read is None:
        return False
    self._pinned_session_order, self._pinned_session_order_stored = read
    self._pinned_session_order_loaded = True
    self.bump_pinned_order_rev()
    return True


def bump_pinned_order_rev(self: DashboardState) -> None:
    """Raise the order revision after the in-memory order changed."""
    self._pinned_order_rev = pinned_session_order.next_revision(
        self._pinned_order_rev, time.time_ns() // 1000
    )


def save_pinned_session_order(self: DashboardState, keys: list[str]) -> None:
    """Write *keys* as the pinned-session order. Blocking; run off the loop."""
    pinned_session_order.save(pinned_session_order.store_path(config_dir()), keys)


def pinned_session_flags(self: DashboardState) -> dict[str, bool]:
    """Every live slot key mapped to its pinned flag."""
    return {key: bool(slot.pinned) for key, slot in self._slots.items()}


def pin_rank(self: DashboardState, slot: Any) -> int | None:
    """*slot*'s position in the pinned order, or ``None`` when it has none.

    Only a pinned slot has a rank, so a key left behind in the stored list
    by an unpin that could not be written ranks nothing.
    """
    if not slot.pinned:
        return None
    try:
        return self._pinned_session_order.index(slot.key)
    except ValueError:
        return None


def load_chat_pins(self: DashboardState) -> None:
    """Load pinned chat messages from disk, dropping malformed records.

    A pin without a ``mid`` field is preserved as-is; new pins always carry
    ``mid``.

    Individual malformed entries are dropped from the active list but kept
    verbatim in ``_unparsed_chat_pin_entries`` so the next ``save_chat_pins``
    round-trips them back to disk rather than silently erasing a user's
    hand-edited-but-typo'd pin (mirrors the cron-folder contract).

    Error classification:
    - Missing file: normal (first run) → empty list.
    - Malformed JSON / invalid shape: tolerated for compatibility → empty list.
    - Transient I/O errors (PermissionError, OSError): MUST NOT replace
      valid in-memory state — re-raise so callers know load failed.
    """
    path = config_dir() / self._CHAT_PINS_FILE
    try:
        if not path.exists():
            self._chat_pins = []
            self._unparsed_chat_pin_entries = []
            return
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        # Malformed content — treat as empty (data corruption).
        logger.warning("chat_pins.json has malformed content: %s", exc)
        self._chat_pins = []
        # Whole-file parse failure: nothing was parsed, so there are no
        # per-entry bytes to preserve. Do NOT clear a prior load's
        # preserved entries against an unreadable read.
        return
    except OSError:
        # Transient I/O error — do NOT clobber valid in-memory state.
        logger.warning("Transient I/O error reading chat_pins.json", exc_info=True)
        raise

    if not isinstance(raw, list):
        logger.warning("chat_pins.json is not a list (%s); ignoring", type(raw).__name__)
        self._chat_pins = []
        return

    # Reuse the create-time ingress caps (source of truth: chat_pins.py) so a
    # hand-edited chat_pins.json cannot smuggle an over-long mid/message_ts/
    # preview past a loader that only checked type + non-emptiness. Imported
    # here (not at module top) because chat_pins.py imports this module.
    from kiro_crew.dashboard.chat_pins import (
        _MAX_MESSAGE_TS_CHARS,
        _MAX_MID_CHARS,
        _MAX_PREVIEW_INPUT_CHARS,
    )

    def _is_valid(pin: Any) -> bool:
        return (
            isinstance(pin, dict)
            and all(isinstance(pin.get(key), str) and pin.get(key) for key in ("id", "slot_key"))
            # Require at least one identity field: mid or message_ts
            and bool(
                (isinstance(pin.get("mid"), str) and pin.get("mid"))
                or (isinstance(pin.get("message_ts"), str) and pin.get("message_ts"))
            )
            and isinstance(pin.get("preview"), str)
            and isinstance(pin.get("pinned_at"), str)
            and bool(pin.get("pinned_at"))
            # Enforce the create-time length caps at load, so an over-long
            # record is partitioned into _unparsed rather than served. Guard
            # each optional identity with isinstance before len(): a
            # hand-edited record can carry a non-string mid/message_ts (e.g.
            # a JSON number) while still being valid via the other identity,
            # and len() on a non-string would crash the whole loader. preview
            # is already isinstance-checked as a str above.
            and (not isinstance(pin.get("mid"), str) or len(pin["mid"]) <= _MAX_MID_CHARS)
            and (
                not isinstance(pin.get("message_ts"), str)
                or len(pin["message_ts"]) <= _MAX_MESSAGE_TS_CHARS
            )
            and len(pin.get("preview") or "") <= _MAX_PREVIEW_INPUT_CHARS
        )

    valid, unparsed = self._partition_preserving(
        raw, _is_valid, "chat pin record(s)", self._CHAT_PINS_FILE
    )
    self._chat_pins = valid
    self._unparsed_chat_pin_entries = unparsed


def save_chat_pins(self: DashboardState) -> None:
    """Persist pinned chat messages with an atomic, owner-only file replacement.

    Writes the active pins plus any malformed entries preserved at load
    time (``_unparsed_chat_pin_entries``), so a save triggered by an
    unrelated pin operation cannot erase bytes a hand-edit left in a shape
    this loader could not validate.
    """
    path = config_dir() / self._CHAT_PINS_FILE
    unparsed = getattr(self, "_unparsed_chat_pin_entries", [])
    atomic_write(
        path,
        json.dumps([*self._chat_pins, *unparsed]),
        fsync=True,
        mode=0o600,
    )


async def remove_chat_pins_for_slots(self: DashboardState, slot_keys: set[str]) -> int:
    """Explicitly remove pins for the supplied dashboard slot keys."""
    keys = {key for key in slot_keys if key}
    if not keys:
        return 0
    async with self._chat_pins_lock:
        previous = self._chat_pins
        remaining = [pin for pin in previous if pin.get("slot_key") not in keys]
        removed = len(previous) - len(remaining)
        if not removed:
            return 0
        self._chat_pins = remaining
        try:
            await asyncio.to_thread(self.save_chat_pins)
        except Exception:
            self._chat_pins = previous
            raise
        return removed


def folders_generation(self: DashboardState) -> int:
    """Monotonic counter identifying the current folder-tree snapshot.

    Rides the ``slots`` WS frame so an already-open tab can tell that the
    folder store actually CHANGED and invalidate its cached
    ``['chat-folders']`` query. Without it a folder created by anything other
    than this tab's own mutation — an agent, a second tab, another device —
    stayed invisible until a reload: the query carries the app-wide
    ``staleTime: Infinity``, so nothing expires it, and the tree the frame
    already carries cannot be written into the cache directly (that would
    clobber an in-flight optimistic edit and, lacking ``history_count``, mark
    the query fresh so the real GET never runs). A generation is the small
    signal that lets the client re-GET instead of guess.

    Read through ``getattr`` because this is reachable from the slots-push
    hot path, which also runs on a ``__new__``-built state that never ran
    ``__init__`` (several endpoint suites build their fixture that way).
    """
    return int(getattr(self, "_folders_generation", 0) or 0)


async def mutate_folders(
    self: DashboardState,
    mutate: Callable[[list[dict[str, Any]]], tuple[bool, _T]],
    on_committed: Callable[[], None] | None = None,
    prepare: Callable[[], Awaitable[None]] | None = None,
) -> _T:
    """Serialize a folder mutation and confirm its off-loop persistence.

    ``on_committed`` runs under the repository lock only after the write
    is proven, so callers can attach side effects that must not outlive a
    rolled-back or no-op transaction. ``prepare`` is awaited under the
    same lock before *mutate* (see :meth:`FolderRepository.mutate`).
    """

    def _mark_committed() -> None:
        # This runs under the repository lock and only after the write is
        # confirmed.  A failed/no-op transaction must not make clients
        # re-fetch a tree that never changed, and concurrent commits must
        # not collapse two monotonic generation bumps into one.
        self._folders_generation = self.folders_generation() + 1
        if on_committed is not None:
            on_committed()

    return await _FOLDER_REPOSITORY.mutate(
        lambda: self._folders,
        self._folders_lock,
        mutate,
        lambda: config_dir() / self._FOLDERS_FILE,
        self._write_folders_confirmed,
        _mark_committed,
        prepare,
    )


async def read_folders(self: DashboardState, read: Callable[[list[dict[str, Any]]], _T]) -> _T:
    """Run a synchronous reader against committed folder state."""
    return await _FOLDER_REPOSITORY.read(lambda: self._folders, self._folders_lock, read)


async def hold_folders(
    self: DashboardState, section: Callable[[list[dict[str, Any]]], Awaitable[_T]]
) -> _T:
    """Hold the folder store lock across an awaitable, read-only section.

    The section sees a snapshot of the committed folders and may hop off
    the loop; no folder mutation can commit until it returns. This is how
    a delete elsewhere excludes a concurrent folder pin (the agent
    templates guard) without doing its file work on the loop.
    """
    return await _FOLDER_REPOSITORY.hold(lambda: self._folders, self._folders_lock, section)


def _write_folders_confirmed(
    self: DashboardState, path: Path, snapshot: list[dict[str, Any]]
) -> None:
    """Persist the complete folder value and prove it landed."""
    _FOLDER_REPOSITORY.write_confirmed(path, snapshot, self._atomic_write_json)


def folder_breadcrumb(self: DashboardState, folder_id: str, sep: str = " › ") -> str:
    """Render a cycle-safe root-to-leaf folder breadcrumb."""
    return _FOLDER_REPOSITORY.breadcrumb(self._folders, folder_id, sep)


def read_durable_tags_snapshot(self: DashboardState) -> _DurableTagSnapshot | None:
    """Read committed tags through the bounded no-link file authority.

    ``None`` means the durable state could not be established. A missing
    file is distinguished by a follow-up ``lstat``: only
    ``FileNotFoundError`` is positive absence; every existing, oversized,
    malformed, or unreadable shape remains ambiguous. Active-row parsing
    and legacy status backfill share this state's canonical vocabulary
    rules so mutation reconciliation cannot drift into a second schema.
    """
    path = config_dir() / self._TAGS_FILE
    try:
        encoded = safe_read_file_bytes_nolink(str(path), within_root=str(path.parent))
    except FileTooLargeError:
        return None
    if encoded is None:
        try:
            path.lstat()
        except FileNotFoundError:
            return _DurableTagSnapshot(False, [], [])
        except OSError:
            return None
        return None
    try:
        raw = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, list):
        return None
    active, unparsed = self._partition_preserving(
        raw,
        lambda row: isinstance(row, dict) and isinstance(row.get("id"), str) and bool(row["id"]),
        "tag entr(ies)",
        self._TAGS_FILE,
    )
    tags = [dict(row) for row in active]
    default_ids = {row["id"] for row in self._DEFAULT_TAGS}
    for row in tags:
        row.setdefault("status", row.get("id") in default_ids)
    return _DurableTagSnapshot(True, tags, unparsed)


def load_tags(self: DashboardState) -> None:
    """Load tag vocabulary and sidebar columns from disk; seed defaults if missing.

    Only seed when ``tags.json`` does not exist. An explicitly-empty file
    is left as-is (so a user who deletes every tag stays at zero tags
    across restarts), and a parse failure is left untouched (so a
    transient I/O error never silently overwrites saved data).
    """
    # Claim this process's tag-revision epoch here: load_tags runs off the
    # event loop at startup (asyncio.to_thread) and before any slot is
    # restored, so the claim's disk read/write never lands on the loop.
    ensure_tags_revision_epoch()
    tags_path = config_dir() / self._TAGS_FILE
    file_existed = tags_path.exists()
    try:
        vocab_ok = True
        if file_existed:
            raw = json.loads(tags_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                # Keep rows the active list dropped verbatim so the seed/
                # back-fill save below (and every later save) round-trips
                # them back instead of erasing a hand-edited-but-typo'd row
                # at boot with no user action. An ``id`` that is not a
                # non-empty string is such a row: every reader keys on the
                # id (set membership, dict keys, ``str(t["id"])``), and an
                # unhashable or non-string one would raise there, so it is
                # preserved on disk but not activated.
                self._tags, unparsed = self._partition_preserving(
                    raw,
                    lambda t: isinstance(t, dict)
                    and isinstance(t.get("id"), str)
                    and bool(t["id"]),
                    "tag entr(ies)",
                    self._TAGS_FILE,
                )
                self._unparsed_tag_entries = unparsed
            else:
                # Valid JSON but not a list (e.g. {}): the vocabulary
                # state is UNKNOWN, same as a parse failure — do not let
                # restore-time pruning wipe assignments against it.
                vocab_ok = False
                logger.warning("tags.json is not a list; treating vocabulary as unknown")
        # Authoritative only when the file is missing (fresh install,
        # seeded below) or parsed as a list — INCLUDING a legitimately-
        # empty [] — so restore-time pruning of dangling ids is safe.
        self._tags_authoritative = vocab_ok
    except Exception:
        logger.warning("Failed to load tags", exc_info=True)
        # Treat a parse error like a present file: do not re-seed.
        file_existed = True
        # Vocabulary state unknown — restore-time pruning must fail open.
        self._tags_authoritative = False
    # Back-fill the status flag for legacy tags saved before the field existed.
    # The 5 seed ids are canonical status tags; everything else defaults to False.
    seed_ids = {t["id"] for t in self._DEFAULT_TAGS}
    mutated = False
    for t in self._tags:
        if "status" not in t:
            t["status"] = t.get("id") in seed_ids
            mutated = True
    seeded_default_vocab = False
    if not file_existed and not self._tags:
        # Fresh install (no tags.json on disk) — seed the default vocabulary.
        self._tags = [dict(t) for t in self._DEFAULT_TAGS]
        mutated = True
        seeded_default_vocab = True

    # One-time seed of the agent tag-write grants store, from TRUSTED CODE
    # CONSTANTS only: the default workflow-state tag ids. Never derived
    # from tags.json (agent-writable — promoting its fields into the
    # protected store would launder a forged grant through the upgrade;
    # a forgery hazard). Custom grants are minted by the dashboard CRUD.
    # Default grants are minted ONLY on the boot that also seeds the
    # default vocabulary: an UPGRADED install may have deleted those tags,
    # and granting their ids anyway would let an agent restore the id in
    # agent-writable tags.json and inherit the authority after restart
    # (a forgery hazard). Everyone else gets an EMPTY store.
    #
    # ORDERING (crash atomicity, same rule as the create endpoint): the
    # grants are seeded BEFORE the vocabulary commit. A crash between the
    # two then leaves grant rows for ids no vocabulary entry references —
    # inert, and the next boot (still a fresh install: no tags.json) seeds
    # the vocabulary while ``seed_default_grants`` keeps the store that
    # already verifies. The reverse order leaves a durable seeded
    # vocabulary whose next boot reads as an upgraded install and seeds an
    # EMPTY store, demoting every default workflow state to human-only.
    try:
        seed_default_grants(
            [t["id"] for t in self._DEFAULT_TAGS if t.get("status")] if seeded_default_vocab else []
        )
    except Exception:
        logger.warning("agent-tag grant seed failed", exc_info=True)
    if mutated:
        self.save_tags()

    # Upgraded installs (store present or vocabulary pre-existing) still
    # need STATUS IDENTITY for the default workflow-state ids: without a
    # row, set_state reads a default status tag as non-status and skips
    # exclusive-peer stripping, so two workflow states persist. Identity
    # rows are policy "none" — they constrain and grant nothing, so a
    # restored id in agent-writable tags.json inherits no authority.
    # Ids from code constants only; existing rows are never touched.
    try:
        seed_status_identity_rows([t["id"] for t in self._DEFAULT_TAGS if t.get("status")])
    except Exception:
        logger.warning("status-identity row seed failed", exc_info=True)

    # Column layout: flat list of {id, name, tag_ids, mode, order}.
    # Empty list = single implicit "all sessions" column (legacy UX).
    columns_path = config_dir() / self._TAG_BOARDS_FILE
    try:
        if columns_path.exists():
            raw = json.loads(columns_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                # Preserve dropped columns verbatim so a later save
                # round-trips them rather than erasing a hand-edited-but-
                # typo'd column. Same id rule as the tag rows above.
                self._tag_boards, unparsed_cols = self._partition_preserving(
                    raw,
                    lambda c: isinstance(c, dict)
                    and isinstance(c.get("id"), str)
                    and bool(c["id"]),
                    "sidebar column(s)",
                    self._TAG_BOARDS_FILE,
                )
                self._unparsed_tag_board_entries = unparsed_cols
                # Prune column tag_ids missing from the vocabulary: tag
                # deletion commits the vocab write first (crash-atomic),
                # so a crash mid-delete can leave dangling ids here. The
                # column API rejects unknown ids, so a dangling id left
                # in place would make that column's filter permanently
                # un-editable (the popover echoes the full list back).
                # Same fail-open rule as the slot-restore prune: only
                # prune when the vocabulary is authoritative.
                if self._tags_authoritative:
                    known = {t.get("id") for t in self._tags}
                    for col in self._tag_boards:
                        tag_ids = col.get("tag_ids")
                        if isinstance(tag_ids, list):
                            col["tag_ids"] = [t for t in tag_ids if t in known]
    except Exception:
        logger.warning("Failed to load sidebar columns", exc_info=True)


def save_tags(self: DashboardState) -> None:
    """Persist tag vocabulary to disk (atomic write).

    Appends any malformed entries preserved at load time
    (``_unparsed_tag_entries``) so a save — including the seed/back-fill
    save that runs during ``load_tags`` itself — cannot erase bytes a
    hand-edit left in a shape the loader could not validate.
    """
    unparsed = getattr(self, "_unparsed_tag_entries", [])
    self._atomic_write_json(config_dir() / self._TAGS_FILE, [*self._tags, *unparsed])


def save_tags_snapshot(self: DashboardState, snapshot: list[dict]) -> None:
    """Persist a pre-captured tag snapshot to disk (strict -- raises on failure).

    Used by the serialized tag-write path in chat_tags_store.py: the snapshot is
    captured on the event loop under the tags write lock, then this write
    runs in a worker thread. Lives here (not in chat_tags_store.py) so the file
    location resolves through this module's ``config_dir`` exactly like
    ``save_tags`` -- keeping tests that patch it working unchanged.

    Malformed entries preserved at load time (``_unparsed_tag_entries``)
    are appended so this write path preserves them too.

    Raises on I/O failure so callers can roll back in-memory state and
    surface HTTP 5xx rather than silently losing data.
    """
    unparsed = getattr(self, "_unparsed_tag_entries", [])
    self._atomic_write_json_strict(config_dir() / self._TAGS_FILE, [*snapshot, *unparsed])


def save_tag_boards(self: DashboardState) -> None:
    """Persist sidebar column layout to disk (atomic write).

    Appends any malformed columns preserved at load time
    (``_unparsed_tag_board_entries``) so a save cannot erase bytes a
    hand-edit left in a shape the loader could not validate.
    """
    unparsed = getattr(self, "_unparsed_tag_board_entries", [])
    self._atomic_write_json(
        config_dir() / self._TAG_BOARDS_FILE,
        [*self._tag_boards, *unparsed],
    )


def save_tag_boards_snapshot(self: DashboardState, snapshot: list[dict]) -> None:
    """Persist a pre-captured boards snapshot to disk (strict -- raises on failure).

    Used by the tag-delete path in chat_tags_vocabulary.py: the snapshot is captured
    on the event loop under the tags write lock, then this write runs in a
    worker thread. Lives here (not in chat_tags_vocabulary.py) so the file location
    resolves through this module's ``config_dir`` exactly like
    ``save_tag_boards`` -- keeping tests that patch it working unchanged.

    Malformed columns preserved at load time
    (``_unparsed_tag_board_entries``) are appended so this write path
    preserves them too.

    Raises on I/O failure so callers can roll back in-memory state and
    surface HTTP 5xx rather than silently losing data.
    """
    unparsed = getattr(self, "_unparsed_tag_board_entries", [])
    self._atomic_write_json_strict(config_dir() / self._TAG_BOARDS_FILE, [*snapshot, *unparsed])


def _partition_preserving(
    raw: list[Any],
    predicate: Callable[[Any], bool],
    noun: str,
    source_file: str,
) -> tuple[list[Any], list[Any]]:
    """Split ``raw`` into ``(active, unparsed)`` by ``predicate``.

    The shared "partition malformed rows at load, keep them verbatim so a
    later save round-trips them back" mechanic behind ``load_cron_folders``,
    ``load_chat_pins``, ``load_tags`` and the tag-board load.
    A row is active when ``predicate`` returns truthy; every other row is
    collected into ``unparsed`` so the caller's save path can re-append it
    (``[*active, *unparsed]``) at write time rather than silently erasing
    bytes a hand-edit left in a shape the loader could not validate.

    When any row is preserved, logs the shared preserving-N warning at
    WARNING level. ``noun`` supplies the per-store wording (``"entr(ies)"``,
    ``"chat pin record(s)"``, ``"tag entr(ies)"``, ``"sidebar column(s)"``)
    and ``source_file`` names the file, so the emitted messages stay
    identical to the hand-rolled copies this replaced.
    """
    active: list[Any] = []
    unparsed: list[Any] = []
    for row in raw:
        (active if predicate(row) else unparsed).append(row)
    if unparsed:
        logger.warning(
            "Preserving %d malformed %s while loading %s " "(kept verbatim, not active)",
            len(unparsed),
            noun,
            source_file,
        )
    return active, unparsed


def _atomic_write_json_strict(path: Path, data: Any) -> None:
    """Atomic JSON write that RAISES on failure (no swallowing).

    Used by persistence helpers where the caller needs to know about
    write failures (e.g. to return HTTP 500).

    Delegates to :func:`atomic_write`, which re-raises after cleaning up
    its temp file, so the no-swallowing contract above is unchanged. It
    also carries the Windows ``os.replace`` sharing-violation retry that
    this hand-rolled copy lacked.

    Content stays ``bytes`` rather than ``str`` on purpose: text mode
    applies universal-newline translation, which would rewrite any ``\n``
    inside the JSON on Windows.

    ``mode=0o600`` is explicit because the hand-rolled version created its
    temp with ``tempfile.mkstemp`` and never widened it, so folders.json,
    tags.json, tag_boards.json and cron_folders.json all publish at 0o600
    today. The helper otherwise falls back to the umask default, normally
    0o644, which would widen all four.
    """
    atomic_write(path, json.dumps(data).encode(), fsync=True, mode=0o600)
