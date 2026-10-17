"""The artifact library's folder tree: ``artifact_folders.json`` and its store.

Folders are a flat list of records with ``parent_id`` pointers; membership lives on
each artifact (``Artifact.folder_id``), never here. The store has its own lock,
independent of the artifact store's, and only reaches artifacts through the
:class:`kiro_crew.artifacts.ArtifactStore` passed to the calls that need them.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import uuid
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
from typing import List as _List
from typing import Mapping

from kiro_crew.artifact_store.model import (
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactReplacedError,
    ArtifactStillPublishedError,
    ArtifactValidationError,
)
from kiro_crew.atomic_write import replace_with_retry
from kiro_crew.config.loader import config_dir

if TYPE_CHECKING:
    from kiro_crew.artifacts import ArtifactStore

# The artifact store's logger name, so log filters keyed on ``kiro_crew.artifacts``
# see folder events as well.
logger = logging.getLogger("kiro_crew.artifacts")

#: The empty generation map -- the default for :meth:`ArtifactFolderStore.delete`'s
#: ``destroyable_generations``, naming no artifact as safe to destroy. Immutable because
#: a shared mutable default is one caller away from vouching for another's artifacts.
_NO_GENERATIONS: "Mapping[str, str]" = MappingProxyType({})

#: Human-path separator for folder addressing at the MCP/API boundary
#: (e.g. ``"Opportunity Planner/Reports"``). Distinct from the display
#: breadcrumb separator (`` › ``); paths are never stored on artifacts.
FOLDER_PATH_SEP = "/"
#: Cap on folder-tree nesting depth (mirrors the chat-folder tree guard).
MAX_FOLDER_DEPTH = 20


class ArtifactFolderStore:
    """Filesystem-backed store for the nested artifact-library folder tree.

    Mirrors the chat-folder subsystem (``dashboard/state.py`` folders): a flat
    JSON list persisted to ``artifact_folders.json`` in the config dir, with
    nesting expressed via ``parent_id`` pointers rather than nested storage or
    path strings. Membership lives on the artifact record (``Artifact.folder_id``),
    not here. Thread-safe via a coarse lock.

    A folder record is ``{id, name, order, parent_id, icon}``:

    * ``id`` — opaque 12-char hex (rename-safe handle).
    * ``name`` — display name (<= 100 chars).
    * ``order`` — sort order among siblings.
    * ``parent_id`` — ``""`` = root; a dangling id is tolerated as root.
    * ``icon`` — optional single-emoji glyph (may be absent).
    * ``color`` — optional ``#rrggbb`` display color (may be absent).
    """

    _FILE = "artifact_folders.json"

    def __init__(self, path: Path | None = None) -> None:
        self._path = (path or (config_dir() / self._FILE)).expanduser()
        self._lock = threading.Lock()
        self._folders: _List[dict[str, Any]] = []
        #: Per-folder icon epoch, bumped under ``self._lock`` by every
        #: user-visible mutation a generated icon must not outlive: a manual
        #: icon set, an icon clear, and a rename. An in-flight generation task
        #: captures the epoch at scheduling time and its write-back
        #: (:meth:`set_icon_if_epoch`) is dropped unless the epoch is
        #: unchanged. One invariant closes all three races that a bare
        #: existence check leaves open and that a value-pin cannot catch: a
        #: clear (absent -> absent) and a rename (icon untouched) both leave
        #: the icon VALUE unchanged, so only a counter distinguishes them.
        #: Deliberately per-folder rather than a store-wide generation
        #: counter, which would cancel a legitimate icon delivery whenever an
        #: unrelated folder changed mid-generation. Held per store INSTANCE
        #: (not module-level as in the chat-folder original, whose folders
        #: live on DashboardState rather than in a store object) so two stores
        #: over different JSON paths cannot alias each other's folder ids. In
        #: memory on purpose -- in-flight tasks die with the process, so the
        #: epoch has nothing to survive a restart for. Entries are dropped on
        #: a confirmed folder delete. Mirrors the chat-folder guard
        #: ``_CHAT_FOLDER_ICON_EPOCHS``.
        self._icon_epochs: dict[str, int] = {}
        self._load()

    def _bump_icon_epoch_locked(self, folder_id: str) -> None:
        """Invalidate any in-flight icon generation for this folder.

        Must be called with ``self._lock`` held -- holding the lock is what
        orders the bump against :meth:`set_icon_if_epoch`'s check, so a
        mutation can never interleave between that check and its write.
        """
        self._icon_epochs[folder_id] = self._icon_epochs.get(folder_id, 0) + 1

    def set_icon_if_epoch(
        self, folder_id: str, icon: str, expected_epoch: int
    ) -> dict[str, Any] | None:
        """Apply a generated icon only while the folder's epoch is unchanged.

        The re-find, the epoch check and the write share one critical section,
        so a manual icon set, an icon clear, or a rename that lands while
        generation was in flight wins over the stale generated result. Returns
        the updated folder, or ``None`` when the write was dropped (folder
        deleted mid-generation, or the epoch moved).

        Does NOT bump the epoch: this is the generated result landing, not a
        user-visible mutation that later generations must lose to.
        """
        with self._lock:
            folder = self._by_id().get(folder_id)
            if folder is None:
                return None  # deleted mid-generation; drop the icon
            if self._icon_epochs.get(folder_id, 0) != expected_epoch:
                # Icon or name changed while generation ran -- drop the result.
                return None
            folder["icon"] = str(icon or "")[:16]
            self._save()
            return dict(folder)

    # ── persistence ───────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            if self._path.exists():
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    # Drop entries lacking an id (legacy/corrupt) so downstream
                    # walks never KeyError on a missing "id".
                    self._folders = [f for f in raw if isinstance(f, dict) and f.get("id")]
        except Exception:  # noqa: BLE001 — a corrupt file must not crash boot
            logger.warning("Failed to load artifact folders", exc_info=True)

    def _save(self) -> None:
        """Atomic JSON write (tmp + rename), mirroring state persistence."""
        payload = json.dumps(self._folders, indent=2, sort_keys=True).encode()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            try:
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            replace_with_retry(tmp, str(self._path))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ── internal helpers (call under lock) ────────────────────────────────

    def _by_id(self) -> dict[str, dict[str, Any]]:
        return {f["id"]: f for f in self._folders if isinstance(f, dict) and f.get("id")}

    def _depth(self, folder_id: str) -> int:
        """Number of ancestors above ``folder_id`` (root folder = 0). Cycle-safe."""
        by_id = self._by_id()
        depth = 0
        seen: set[str] = set()
        fid = str(by_id.get(folder_id, {}).get("parent_id") or "")
        while fid and fid in by_id and fid not in seen:
            seen.add(fid)
            depth += 1
            fid = str(by_id[fid].get("parent_id") or "")
        return depth

    def _subtree_ids(self, folder_id: str) -> set[str]:
        """All folder ids in the subtree rooted at ``folder_id`` (inclusive)."""
        by_id = self._by_id()
        if folder_id not in by_id:
            return set()
        out: set[str] = set()
        stack = [folder_id]
        while stack:
            cur = stack.pop()
            if cur in out:
                continue
            out.add(cur)
            for f in self._folders:
                if str(f.get("parent_id") or "") == cur and f["id"] not in out:
                    stack.append(f["id"])
        return out

    @staticmethod
    def _clean_name(name: Any) -> str:
        cleaned = str(name or "").strip()[:100]
        if not cleaned:
            raise ArtifactValidationError("folder name required")
        return cleaned

    # ── public API ────────────────────────────────────────────────────────

    def list(self) -> _List[dict[str, Any]]:
        """Return a copy of the folder records (sorted by depth-then-order)."""
        with self._lock:
            return [dict(f) for f in self._ordered()]

    def _ordered(self) -> _List[dict[str, Any]]:
        # Stable pre-order-ish ordering: siblings by (order, name); the frontend
        # does its own tree flatten, so this is a convenience for CLI/MCP output.
        return sorted(
            self._folders,
            key=lambda f: (self._depth(f["id"]), int(f.get("order", 0)), str(f.get("name", ""))),
        )

    def get(self, folder_id: str) -> dict[str, Any] | None:
        with self._lock:
            f = self._by_id().get(folder_id)
            return dict(f) if f else None

    def exists(self, folder_id: str) -> bool:
        if not folder_id:
            return False
        with self._lock:
            return folder_id in self._by_id()

    def subtree_ids(self, folder_id: str) -> set[str]:
        """Public view of the subtree rooted at ``folder_id`` (inclusive).

        Exists so a caller that must act on a cascade's artifacts BEFORE the cascade
        runs -- withdrawing their published copies, which this store cannot do because
        the withdrawal is async and lives a layer up -- can enumerate them without
        reaching into a private method.
        """
        with self._lock:
            return self._subtree_ids(folder_id)

    def create(self, name: str, parent_id: str = "", color: str = "") -> dict[str, Any]:
        """Create a folder under ``parent_id`` (``""`` = root)."""
        name = self._clean_name(name)
        color = self._clean_color(color)
        with self._lock:
            parent_id = str(parent_id or "")
            by_id = self._by_id()
            if parent_id and parent_id not in by_id:
                raise ArtifactValidationError(f"parent folder not found: {parent_id}")
            if parent_id and self._depth(parent_id) + 1 >= MAX_FOLDER_DEPTH:
                raise ArtifactValidationError(
                    f"folder nesting exceeds max depth {MAX_FOLDER_DEPTH}"
                )
            folder = {
                "id": uuid.uuid4().hex[:12],
                "name": name,
                "order": len(self._folders),
                "parent_id": parent_id,
            }
            if color:
                folder["color"] = color
            self._folders.append(folder)
            self._save()
            logger.info(
                "artifact folder created: id=%s name=%s parent=%s",
                folder["id"],
                name,
                parent_id or "(root)",
            )
            return dict(folder)

    def rename(self, folder_id: str, name: str) -> tuple[dict[str, Any], int]:
        """Rename, returning the folder AND the icon epoch this rename produced.

        The epoch comes back from inside the same critical section as the bump,
        which is the only way a caller can arm background icon generation
        safely. Renaming and then READING the epoch back would be two lock
        acquisitions: a competing manual icon set landing between them bumps the
        epoch again, the later read would capture THAT epoch, and the generated
        icon would then satisfy :meth:`set_icon_if_epoch` and clobber the manual
        pick -- the very race the epoch exists to prevent. Returning it closes
        that window by construction, because there is no read to lose.

        The tuple is deliberately the ONLY spelling of this mutation: a
        dict-returning ``rename`` alongside it would be a second spelling of one
        write, and the two would drift.
        """
        name = self._clean_name(name)
        with self._lock:
            folder = self._by_id().get(folder_id)
            if folder is None:
                raise ArtifactNotFoundError(f"folder not found: {folder_id}")
            folder["name"] = name
            # An in-flight icon was derived from the OLD name -- invalidate it.
            self._bump_icon_epoch_locked(folder_id)
            self._save()
            return dict(folder), self._icon_epochs[folder_id]

    def reparent(self, folder_id: str, new_parent: str = "") -> dict[str, Any]:
        """Move a folder under ``new_parent`` (``""`` = root). Cycle-guarded."""
        with self._lock:
            by_id = self._by_id()
            folder = by_id.get(folder_id)
            if folder is None:
                raise ArtifactNotFoundError(f"folder not found: {folder_id}")
            new_parent = str(new_parent or "")
            if new_parent:
                if new_parent not in by_id:
                    raise ArtifactValidationError(f"parent folder not found: {new_parent}")
                subtree = self._subtree_ids(folder_id)
                # Cycle guard: a folder may not become its own descendant.
                if new_parent in subtree:
                    raise ArtifactValidationError(
                        "cannot move a folder into itself or one of its descendants"
                    )
                # Depth guard: the deepest node in the moved subtree must still
                # fit under MAX_FOLDER_DEPTH at the destination — otherwise two
                # shallow subtrees could be merged past the limit that create()
                # and resolve_path() enforce.
                base_depth = self._depth(folder_id)
                subtree_height = max(self._depth(sid) for sid in subtree) - base_depth
                if self._depth(new_parent) + 1 + subtree_height >= MAX_FOLDER_DEPTH:
                    raise ArtifactValidationError(
                        f"folder nesting exceeds max depth {MAX_FOLDER_DEPTH}"
                    )
            folder["parent_id"] = new_parent
            self._save()
            return dict(folder)

    def set_icon(self, folder_id: str, icon: str) -> dict[str, Any]:
        with self._lock:
            folder = self._by_id().get(folder_id)
            if folder is None:
                raise ArtifactNotFoundError(f"folder not found: {folder_id}")
            folder["icon"] = str(icon or "")[:16]
            # A manual set OR a clear (icon == "") invalidates any in-flight
            # generation: its result was derived before the user's choice.
            self._bump_icon_epoch_locked(folder_id)
            self._save()
            return dict(folder)

    @staticmethod
    def _clean_color(color: str) -> str:
        """Validate a folder color: ``""`` (none) or a ``#rrggbb`` hex value."""
        color = str(color or "").strip()
        if color and not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise ArtifactValidationError("folder color must be a #rrggbb hex value")
        return color.lower()

    def set_color(self, folder_id: str, color: str) -> dict[str, Any]:
        """Set (or clear, with ``""``) the folder's display color."""
        color = self._clean_color(color)
        with self._lock:
            folder = self._by_id().get(folder_id)
            if folder is None:
                raise ArtifactNotFoundError(f"folder not found: {folder_id}")
            if color:
                folder["color"] = color
            else:
                folder.pop("color", None)
            self._save()
            return dict(folder)

    def reorder(self, orders: _List[dict[str, Any]]) -> None:
        """Apply ``[{id, order}, ...]`` sibling ordering (minimal patch)."""
        with self._lock:
            by_id = self._by_id()
            for entry in orders:
                if not isinstance(entry, dict):
                    continue
                fid = entry.get("id")
                if fid in by_id and "order" in entry:
                    by_id[fid]["order"] = int(entry["order"])
            self._save()

    def resolve_path(self, ref: str, *, create_missing: bool = False) -> str:
        """Resolve a folder id OR a human path to a folder id.

        ``ref`` may be:

        * ``""`` / ``None`` / ``"root"`` (case-insensitive) → ``""`` (root).
        * an existing folder id → returned as-is.
        * a ``/``-separated path (``"A/B/C"``) → walked segment-by-segment
          from the root by (case-insensitive) name. When ``create_missing``
          is True, missing segments are created (``mkdir -p`` semantics);
          otherwise a missing segment raises :class:`ArtifactNotFoundError`.

        Returns the resolved leaf folder id (``""`` for root).
        """
        ref = str(ref or "").strip()
        if not ref or ref.lower() == "root":
            return ""
        with self._lock:
            by_id = self._by_id()
            if ref in by_id:
                return ref
            segments = [s.strip() for s in ref.split(FOLDER_PATH_SEP) if s.strip()]
            if not segments:
                return ""
            parent = ""
            # All-or-nothing: if a later segment fails the depth guard (or a
            # non-create lookup misses), roll back any folders appended during
            # this attempt so in-memory state never drifts from disk.
            snapshot_len = len(self._folders)
            created_any = False
            try:
                for seg in segments:
                    # Normalize through the SAME truncation _clean_name applies
                    # on create, so a >100-char segment matches the folder it
                    # created on a prior call (lookup key == stored name) —
                    # otherwise repeated resolves would mint duplicates.
                    seg_norm = self._clean_name(seg).lower()
                    match = next(
                        (
                            f
                            for f in self._folders
                            if str(f.get("parent_id") or "") == parent
                            and str(f.get("name", "")).strip().lower() == seg_norm
                        ),
                        None,
                    )
                    if match is None:
                        if not create_missing:
                            raise ArtifactNotFoundError(f"folder path not found: {ref}")
                        if parent and self._depth(parent) + 1 >= MAX_FOLDER_DEPTH:
                            raise ArtifactValidationError(
                                f"folder nesting exceeds max depth {MAX_FOLDER_DEPTH}"
                            )
                        match = {
                            "id": uuid.uuid4().hex[:12],
                            "name": self._clean_name(seg),
                            "order": len(self._folders),
                            "parent_id": parent,
                        }
                        self._folders.append(match)
                        created_any = True
                    parent = match["id"]
            except (ArtifactValidationError, ArtifactNotFoundError):
                # Discard folders appended during this failed attempt.
                del self._folders[snapshot_len:]
                raise
            if created_any:
                self._save()
            return parent

    def breadcrumb(self, folder_id: str, sep: str = FOLDER_PATH_SEP) -> str:
        """Render a folder's ancestry root→leaf as a ``sep``-joined path."""
        if not folder_id:
            return ""
        with self._lock:
            by_id = self._by_id()
            names: _List[str] = []
            seen: set[str] = set()
            fid = folder_id
            while fid and fid in by_id and fid not in seen:
                seen.add(fid)
                names.append(str(by_id[fid].get("name", "")))
                fid = str(by_id[fid].get("parent_id") or "")
            names.reverse()
            return sep.join(n for n in names if n)

    def item_counts(self, artifact_store: "ArtifactStore") -> dict[str, int]:
        """Direct-artifact count per folder id (unfiled excluded)."""
        counts: dict[str, int] = {}
        for art in artifact_store.list():
            fid = getattr(art, "folder_id", "") or ""
            if fid:
                counts[fid] = counts.get(fid, 0) + 1
        return counts

    def list_with_counts(self, artifact_store: "ArtifactStore") -> _List[dict[str, Any]]:
        """Folders enriched with a computed, non-persisted ``item_count``."""
        counts = self.item_counts(artifact_store)
        return [{**f, "item_count": counts.get(f["id"], 0)} for f in self.list()]

    def delete(
        self,
        folder_id: str,
        *,
        delete_contents: bool,
        artifact_store: "ArtifactStore",
        destroyable_generations: "Mapping[str, str]" = _NO_GENERATIONS,
    ) -> dict[str, Any]:
        """Delete a folder. ``delete_contents`` picks the semantics:

        * **False (safe, default)** — delete only this folder; re-parent its
          direct child folders and artifacts to this folder's parent (root if
          none). Descendant folders travel up with their re-parented parent.
        * **True (cascade)** — permanently delete the whole subtree: every
          descendant artifact (via the guarded :meth:`ArtifactStore.delete`)
          and every descendant folder.

        ``destroyable_generations`` maps the slug of each artifact the caller has made
        safe to destroy to that artifact's ``created_at``. A descendant whose slug is
        absent is left in place and reported under ``unguarded_artifact_slugs``; one whose
        slug is present but whose stamp differs is a REPLACEMENT and is left in place and
        reported under ``replaced_artifact_slugs``. It defaults to EMPTY rather than to
        everything, so a cascade whose caller forgot to name its victims empties nothing
        and says which artifacts it left, instead of destroying a subtree nobody vouched
        for. Unread when ``delete_contents`` is false, which destroys no artifact at all.

        A slug is not an identity, which is why the stamp travels with it: the caller draws
        this map up before withdrawing published copies, that withdrawal awaits the
        network per copy, and a freed slug is re-minted identically. So an artifact the
        caller named can be deleted and a same-titled newcomer can take the name while the
        pass runs, and a slug-only map would match the newcomer and destroy it with no
        undo -- reported as an ordinary deletion, indistinguishable from the intended
        victim.

        The caller holding each listed artifact's publication guard is what makes the
        map meaningful: a first publish uploads its object before writing the
        record naming it, so ``refuse_if_published`` below cannot see one that is
        in flight, and an artifact filed into this subtree after the caller drew
        up its list is exactly the artifact whose publish this store cannot
        observe. Leaving it alone costs a folder that does not fully empty, which
        the owner deletes again; destroying it can strand a world-readable copy
        whose only handle goes with it.

        Returns a summary dict describing what changed.
        """
        # Phase 1 (under folder lock): mutate the folder tree, decide the
        # affected id set. Phase 2 (outside the lock): touch the artifact
        # store, which has its own independent lock.
        with self._lock:
            by_id = self._by_id()
            folder = by_id.get(folder_id)
            if folder is None:
                raise ArtifactNotFoundError(f"folder not found: {folder_id}")
            parent = str(folder.get("parent_id") or "")
            if delete_contents:
                affected_ids = self._subtree_ids(folder_id)
            else:
                affected_ids = {folder_id}
                # Re-parent direct child folders up to this folder's parent.
                for f in self._folders:
                    if str(f.get("parent_id") or "") == folder_id:
                        f["parent_id"] = parent
            self._folders = [f for f in self._folders if f.get("id") not in affected_ids]
            self._save()
            # Release the icon-epoch guards only after the removal is
            # CONFIRMED persisted. _save() raising propagates out of this
            # block, so the pop is skipped and the guard stays armed. Note
            # what a failed _save() actually leaves behind: self._folders was
            # already filtered above, so the folder is gone from memory but
            # SURVIVES on disk, and any later reload brings it back. Keeping
            # its epoch is the conservative side of that split -- resetting it
            # to 0 would let a stale in-flight generation clobber a manual
            # icon on the record that comes back. After a confirmed delete the
            # entries have nothing left to guard (set_icon_if_epoch already
            # drops a folder it cannot re-find); popping keeps the registry
            # from growing with every deleted id.
            for _gone in affected_ids:
                self._icon_epochs.pop(_gone, None)

        # Phase 2: artifacts. ``affected_ids`` is the subtree for cascade, or
        # just the single folder for the safe path.
        #
        # Race window: Phase 2 runs OUTSIDE the folder lock (the artifact store
        # has its own independent lock, and holding both would invite ordering
        # deadlocks). Between Phase 1 removing the folder and this scan, a
        # concurrent ``ArtifactStore.set_folder()`` can file an artifact into
        # the just-deleted folder id.
        #
        # On the RE-PARENT path that stays harmless: such an artifact ends up
        # with a dangling ``folder_id``, which every reader already tolerates by
        # degrading it to Unfiled (see ``list(folder=)`` and the tree view's
        # dangling-id handling).
        #
        # On the CASCADE path it is NOT harmless, because the consequence is
        # destruction rather than a stale field. The caller withdraws every
        # published copy in the subtree before calling here and clears each
        # record it withdrew, so an artifact still holding a publication at this
        # point is exactly one that arrived after that preflight -- its copy was
        # never withdrawn, and destroying it would erase the only handle able to
        # take that copy down.
        #
        # The refusal is asked of `delete` itself rather than checked here: a
        # check in this loop is a check-then-act over a snapshot, so a publish
        # landing between it and the delete would still be destroyed. Inside
        # `delete` the check shares the lock with the removal, which is what
        # makes it hold. Phase 1 has already committed the folder-tree change and
        # cannot be rolled back here, so a kept artifact survives with a dangling
        # ``folder_id`` and degrades to Unfiled -- the outcome this path already
        # tolerates, and a recoverable one: the owner restores access to the
        # destination, withdraws the copy, and deletes it deliberately. Note that
        # unpublishing is NOT a second route out of this state -- it refuses on an
        # unreachable destination for the same reason this delete did.
        deleted_slugs: _List[str] = []
        reparented_slugs: _List[str] = []
        kept_published_slugs: _List[str] = []
        unguarded_slugs: _List[str] = []
        replaced_slugs: _List[str] = []
        for art in artifact_store.list():
            fid = getattr(art, "folder_id", "") or ""
            if fid not in affected_ids:
                continue
            if delete_contents:
                expect = destroyable_generations.get(art.slug)
                if expect is None:
                    # Filed into this subtree after the caller drew up its guarded set, so
                    # its publication state is the one thing this store cannot settle: the
                    # only evidence here is a record, and a first publish in flight has
                    # uploaded its object and written none. It survives either way, which
                    # degrades it to Unfiled exactly as a kept artifact does; the record
                    # decides only which list reports it, so one that IS published keeps
                    # the report it already had and only the unknown case is separate.
                    if art.publication is not None:
                        kept_published_slugs.append(art.slug)
                    else:
                        unguarded_slugs.append(art.slug)
                    logger.warning(
                        "cascade left %s alone: it joined the subtree outside the "
                        "caller's guarded set, so a publish in flight for it cannot be "
                        "ruled out",
                        art.slug,
                    )
                    continue
                try:
                    artifact_store.delete(
                        art.slug, refuse_if_published=True, expect_created_at=expect
                    )
                    deleted_slugs.append(art.slug)
                except ArtifactReplacedError:
                    # The artifact the caller named is already gone and a newcomer holds
                    # its slug. Nobody asked for the newcomer to be destroyed, and the
                    # removal has no undo, so it survives unfiled like a kept one.
                    replaced_slugs.append(art.slug)
                    logger.warning(
                        "cascade kept %s: the artifact under this slug was replaced "
                        "after the caller named it, so destroying it would take one "
                        "nobody asked to delete",
                        art.slug,
                    )
                except ArtifactStillPublishedError:
                    kept_published_slugs.append(art.slug)
                    logger.warning(
                        "cascade kept %s: still published, so destroying it would "
                        "strand a public copy with no withdrawal handle",
                        art.slug,
                    )
                except ArtifactError as exc:  # pragma: no cover — best-effort
                    logger.warning("cascade delete failed for %s: %s", art.slug, exc)
            else:
                artifact_store.set_folder(art.slug, parent)
                reparented_slugs.append(art.slug)
        logger.info(
            "artifact folder deleted: id=%s cascade=%s folders=%d artifacts=%d",
            folder_id,
            delete_contents,
            len(affected_ids),
            len(deleted_slugs) if delete_contents else len(reparented_slugs),
        )
        return {
            "deleted_folder_ids": sorted(affected_ids),
            "deleted_artifact_slugs": deleted_slugs,
            "kept_published_artifact_slugs": kept_published_slugs,
            "unguarded_artifact_slugs": unguarded_slugs,
            "replaced_artifact_slugs": replaced_slugs,
            "reparented_artifact_slugs": reparented_slugs,
            "reparented_to": parent,
            "delete_contents": delete_contents,
        }
