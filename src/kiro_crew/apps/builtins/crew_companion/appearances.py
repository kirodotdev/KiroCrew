"""Appearance packs: the companion's avatar library.

The desktop app kept these in Electron's ``userData`` and served them over IPC. Here
they live in the app's own data directory and are served over HTTP, because the
renderer is a page rather than a privileged window. The pack SHAPE is unchanged — a
manifest plus animation files, as ``appearanceTypes.ts`` defines it — so packs the
user already made remain loadable.

Three properties this has to keep, all learned from the desktop version:

* **A pack the user made is precious.** Custom art is unrecoverable if lost, so writes
  go through a temp file and a rename, and a delete only ever touches a custom pack's
  own directory.
* **A malformed pack must not take the companion down.** A pack is third-party content,
  possibly hand-edited. Anything unreadable is skipped with a warning and the others
  still load, rather than one bad manifest emptying the library.
* **The built-in ghost is not a file.** It ships with the app and cannot be deleted or
  renamed, so it is registered from code and always present even when the custom
  directory is empty.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew.platform_compat import chmod_safe, is_link_or_junction

logger = logging.getLogger(__name__)

#: The built-in ghost's id. Referenced by the renderer, so it is a contract.
DEFAULT_PACK = "kiro-ghost"

#: Custom packs live one directory each, named by id, under this subdirectory.
PACKS_DIRNAME = "appearances"

#: Per-file ceiling for pack content. Generous for art, small enough that a
#: hand-edited manifest claiming a gigabyte cannot exhaust memory on read.
MAX_FILE_BYTES = 8 * 1024 * 1024

#: Animation formats the renderer knows how to draw.
FORMATS = ("svg", "lottie", "sprite")


@dataclass(frozen=True)
class PackMeta:
    """A pack as the gallery lists it — metadata only, no art."""

    id: str
    name: str
    author: str
    description: str
    #: "builtin" or "custom". Only custom packs can be deleted.
    type: str
    format: str
    #: True when the user has recoloured this pack.
    recoloured: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "author": self.author,
            "description": self.description,
            "type": self.type,
            "format": self.format,
            "recoloured": self.recoloured,
        }


def _safe_id(raw: Any) -> str | None:
    """Validate a pack id as a single safe path segment.

    A pack id becomes a directory name, so this is the boundary that stops
    ``../`` or an absolute path from escaping the packs directory. Rejecting is
    correct here rather than sanitising: a caller sending a traversal is not making
    a typo, and silently rewriting it would hide that.
    """
    if not isinstance(raw, str):
        return None
    ident = raw.strip()
    if not ident or len(ident) > 64:
        return None
    if ident in (".", ".."):
        return None
    # Letters, digits, dash and underscore only — no separators, no dots.
    if not all(c.isalnum() or c in "-_" for c in ident):
        return None
    return ident


class AppearanceStore:
    """Reads and writes the companion's appearance packs."""

    def __init__(self, data_dir: Path) -> None:
        self._root = Path(data_dir) / PACKS_DIRNAME
        #: id -> colour map, for packs the user has recoloured.
        self._colour_maps: dict[str, dict[str, str]] = {}
        self._colour_path = Path(data_dir) / "crew-companion-colours.json"

    # ── setup ───────────────────────────────────────────────────────────────

    def load(self) -> None:
        """Prepare the packs directory and read any saved colour maps."""
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("crew-companion: cannot create packs dir: %s", exc)
        self._recover_orphaned_backups()
        try:
            if self._colour_path.exists():
                raw = json.loads(self._colour_path.read_text("utf-8"))
                if isinstance(raw, dict):
                    self._colour_maps = {
                        k: v for k, v in raw.items() if isinstance(v, dict)
                    }
        except (OSError, ValueError) as exc:
            # A corrupt colour file costs the user their recolouring, not their art,
            # so carrying on with defaults beats refusing to start.
            logger.warning("crew-companion: colour maps unreadable: %s", exc)

    def _recover_orphaned_backups(self) -> None:
        """Restore a pack stranded as ``<name>.old.<pid>`` by an interrupted save.

        The overwrite is two renames: target -> backup, then staging -> target.
        A gateway termination BETWEEN them leaves the pack existing only as the
        backup — which the listing deliberately filters out (its name is not a
        legal pack id), so the user's art sits on disk while the gallery shows
        the pack as gone. Backup-with-target-present is the opposite case (died
        after the second rename, before cleanup): the new pack won, the backup
        is leftover garbage.
        """
        try:
            entries = list(self._root.iterdir())
        except OSError:
            return
        for entry in entries:
            name = entry.name
            head, sep, _pid = name.rpartition(".old.")
            if not sep or _safe_id(head) is None or not entry.is_dir():
                continue
            target = self._root / head
            try:
                if target.exists():
                    shutil.rmtree(entry, ignore_errors=True)
                    logger.info("crew-companion: removed stale pack backup %s", name)
                else:
                    os.replace(entry, target)
                    logger.info("crew-companion: restored pack %r from backup %s", head, name)
            except OSError as exc:
                logger.warning("crew-companion: backup recovery failed for %s: %s", name, exc)

    # ── reads ───────────────────────────────────────────────────────────────

    def list_packs(self) -> list[dict[str, Any]]:
        """Every pack, built-in first.

        One unreadable pack is skipped rather than failing the list: the gallery
        showing four of five packs is recoverable, showing none is not.
        """
        packs: list[dict[str, Any]] = [self._builtin_meta().to_dict()]
        if not self._root.exists():
            return packs
        for entry in sorted(self._root.iterdir()):
            if not entry.is_dir():
                continue
            # Only directories whose name is a legal pack id are packs. This skips
            # the `.old.<pid>` directory an overwrite parks the previous version in:
            # if the process dies before the cleanup, the gallery would otherwise
            # list the backup as a second, duplicate pack.
            if _safe_id(entry.name) is None:
                continue
            meta = self._read_meta(entry)
            if meta is not None:
                packs.append(meta.to_dict())
            else:
                logger.warning("crew-companion: skipping unreadable pack %s", entry.name)
        return packs

    def pack_detail(self, pack_id: str) -> dict[str, Any] | None:
        """A pack with its animation content inlined, ready to render.

        Content is returned inline rather than as URLs because a custom pack's files
        are user data outside the served tree; handing back paths would mean opening
        a file-serving route over that directory.
        """
        ident = _safe_id(pack_id)
        if ident is None:
            return None
        if ident == DEFAULT_PACK:
            return {
                "meta": self._builtin_meta().to_dict(),
                # The built-in ghost's art is bundled with the frontend, so the
                # renderer already has it and needs no content here.
                "animations": {},
                "colorMap": self._colour_maps.get(DEFAULT_PACK) or {},
            }

        pack_dir = self._root / ident
        manifest = self._read_manifest(pack_dir)
        if manifest is None:
            return None

        animations: dict[str, Any] = {}
        # `meta` may be present but the WRONG TYPE (a hand-edited or legacy
        # on-disk manifest with `"meta": []`): `.get("meta", {})` only
        # defaults when the key is absent, so `[].get(...)` raised
        # AttributeError and the detail endpoint 500ed — for the ACTIVE pack
        # that meant the avatar could not load at all. The save path and the
        # listing path both already require a dict; the detail read must
        # tolerate what older writers left on disk.
        meta_section = manifest.get("meta")
        fmt = meta_section.get("format", "svg") if isinstance(meta_section, dict) else "svg"

        # All THREE categories, not just `states`.
        #
        # A manifest carries required `states`, optional `moods` and open-ended
        # `random` clips (the shape the desktop app defined). Returning only
        # `states` did not merely hide the rest — the editor loads a pack from this
        # payload and saves what it was given, so re-editing a PetDex pack with
        # random clips rebuilt it WITHOUT them and the art was deleted. The frontend
        # already looks moods and random slots up in this same flat map, so folding
        # them in is all it needs.
        #
        # `random_names` names which flat-map keys are open-ended random extras. The
        # editor can't recover this from the flat map alone (states/moods/random are
        # indistinguishable once folded together), so without it the editor treats
        # every random clip as absent and re-saving drops them — the same data loss
        # in a subtler form. Preserve manifest order and only list names whose file
        # content actually loaded, mirroring the skip-on-missing-content above.
        #
        # `categories` is the AUTHORITATIVE taxonomy: which slot belongs to which
        # of the three category maps. This is the FOURTH place the flattening bug
        # appeared (detail read, editor load, editor save, bundle export) — each
        # consumer was re-deriving the taxonomy from the flat map and each got it
        # wrong the same way. One source of truth here ends that class of bug:
        # every consumer that must rebuild a categorized manifest reads this
        # instead of guessing. `randomNames` stays for the editor's existing
        # contract; it equals categories["random"].
        random_names: list[str] = []
        categories: dict[str, list[str]] = {"states": [], "moods": [], "random": []}
        for category in ("states", "moods", "random"):
            section = manifest.get(category)
            if not isinstance(section, dict):
                continue
            for slot, filename in section.items():
                content = self._read_pack_file(pack_dir, filename)
                if content is None:
                    continue
                # Per-slot format from the FILE, not the manifest-wide default.
                # A mixed pack (SVG states + a Lottie clip) stamped every slot
                # with meta.format, so its SVG slots were handed to the Lottie
                # renderer and drew blank. The filename already knows.
                name = str(filename).lower()
                if name.endswith(".json"):
                    slot_fmt = "lottie"
                elif name.endswith(".png"):
                    slot_fmt = "sprite"
                elif name.endswith(".svg"):
                    slot_fmt = "svg"
                else:
                    slot_fmt = fmt
                animations[slot] = {"content": content, "format": slot_fmt}
                categories[category].append(slot)
                if category == "random":
                    random_names.append(slot)
        sprite = manifest.get("sprite") or {}
        detail: dict[str, Any] = {
            "meta": (self._read_meta(pack_dir) or self._builtin_meta()).to_dict(),
            "animations": animations,
            "randomNames": random_names,
            "categories": categories,
            "sprite": sprite,
            "colorMap": self._colour_maps.get(ident) or {},
        }
        # The ORIGINAL sheet, when the pack kept one for re-editing. It is not
        # an animation slot, so it never appears in `animations` — and reading
        # it through the slot map came back empty, which made the sprite editor
        # fall back to strips and an overwrite save silently DROP the sheet.
        source_name = sprite.get("source") if isinstance(sprite, dict) else None
        if isinstance(source_name, str) and source_name:
            source = self._read_pack_file(pack_dir, source_name)
            if source is not None:
                detail["sourceImage"] = source
        return detail

    def pack_exists(self, pack_id: str) -> bool:
        """Whether a pack occupies this id — INCLUDING unreadable ones.

        ``list_packs`` deliberately skips a pack whose manifest cannot be
        parsed ("one broken pack does not hide the others"), which made the
        import path's collision check blind to it: importing a bundle under
        the same id then replaced the directory and destroyed the broken
        pack's still-recoverable art. Existence is answered by the DIRECTORY
        (plus the built-in id, which has no directory), not by listability.
        """
        ident = _safe_id(pack_id)
        if ident is None:
            return False
        if ident == DEFAULT_PACK:
            return True
        return (self._root / ident).is_dir()

    def colour_map(self, pack_id: str) -> dict[str, str]:
        ident = _safe_id(pack_id)
        return dict(self._colour_maps.get(ident or "", {}))

    # ── writes ──────────────────────────────────────────────────────────────

    def set_colour_map(self, pack_id: str, colours: Any) -> bool:
        """Record a recolouring. Returns False when the input is unusable."""
        ident = _safe_id(pack_id)
        if ident is None or not isinstance(colours, dict):
            return False
        # Only string→string pairs; anything else would break the SVG rewrite that
        # consumes this on the renderer side.
        clean = {
            str(k): str(v)
            for k, v in colours.items()
            if isinstance(k, str) and isinstance(v, str)
        }
        prev = self._colour_maps.get(ident)
        self._colour_maps[ident] = clean
        try:
            self._save_colours()
        except OSError:
            # Roll back so memory matches disk — otherwise the UI shows the
            # new colour until a restart silently reverts it. Re-raise so the
            # route answers 503 instead of pretending the save landed.
            if prev is None:
                self._colour_maps.pop(ident, None)
            else:
                self._colour_maps[ident] = prev
            raise
        return True

    def delete_pack(self, pack_id: str) -> bool:
        """Delete a CUSTOM pack. The built-in is refused."""
        ident = _safe_id(pack_id)
        if ident is None or ident == DEFAULT_PACK:
            return False
        pack_dir = self._root / ident
        # Refuse a link BEFORE resolving: a symlink (or Windows junction) named
        # like a pack and pointing at a SIBLING pack resolves to that sibling,
        # passes the containment check below, and the rmtree then destroys the
        # victim's artwork — deleting the alias must never delete the target.
        try:
            if is_link_or_junction(pack_dir):
                logger.warning("crew-companion: refusing to delete linked pack: %s", ident)
                return False
        except OSError:
            return False
        # Resolve and re-check containment: the id is already validated, and this is
        # the second belt on an irreversible recursive delete.
        try:
            resolved = pack_dir.resolve()
            if self._root.resolve() not in resolved.parents:
                return False
            if not resolved.is_dir():
                return False
            shutil.rmtree(resolved)
        except OSError as exc:
            logger.warning("crew-companion: pack delete failed: %s", exc)
            return False
        self._colour_maps.pop(ident, None)
        try:
            self._save_colours()
        except OSError as exc:
            # The pack itself is already gone — a stale colour entry for a
            # nonexistent pack is harmless and gets rewritten on the next
            # successful save, so the delete still reports success.
            logger.warning("crew-companion: colour map write failed: %s", exc)
        return True

    def save_pack(self, pack_id: str, manifest: Any, files: Any) -> bool:
        """Create or replace a custom pack.

        ``files`` maps filename to content (text for svg/lottie, base64 for a sprite
        sheet). Written to a temp directory and moved into place, so an interrupted
        save cannot leave a half-written pack that lists in the gallery and then
        fails to render.
        """
        ident = _safe_id(pack_id)
        if ident is None or ident == DEFAULT_PACK:
            return False
        if not isinstance(manifest, dict) or not isinstance(files, dict):
            return False
        # The read path needs a `meta` dict to list a pack at all. Without this check a
        # save could report success and then be invisible in the gallery — present on
        # disk, skipped on read — which is far harder to diagnose than a refusal here.
        if not isinstance(manifest.get("meta"), dict):
            logger.warning("crew-companion: pack manifest has no meta: %s", ident)
            return False

        staging = self._root / f".tmp-{ident}-{os.getpid()}"
        target = self._root / ident
        # Serialize and size-check the manifest BEFORE creating staging or
        # touching the target. Every pack file below is capped at
        # MAX_FILE_BYTES, but the generated manifest itself was not — and the
        # read path (`_read_manifest`) rejects an oversized manifest.json. An
        # overwrite that wrote one therefore succeeded, swapped the target,
        # deleted the backup — and the pack then vanished from the gallery:
        # data loss reported as success. Refusing here keeps the existing
        # pack intact.
        manifest_text = json.dumps(manifest, indent=2)
        if len(manifest_text.encode("utf-8")) > MAX_FILE_BYTES:
            logger.warning("crew-companion: pack manifest too large: %s", ident)
            return False
        try:
            if staging.exists():
                shutil.rmtree(staging)
            staging.mkdir(parents=True)
            (staging / "manifest.json").write_text(manifest_text, "utf-8")
            seen_casefolded: set[str] = set()
            for name, content in files.items():
                safe = _safe_filename(name)
                if safe is None or not isinstance(content, str):
                    # Refuse the WHOLE save, not just this file. An overwrite is
                    # an edit of an existing pack: silently dropping one file and
                    # then replacing the target meant the original artwork for
                    # that slot was destroyed by a save the user believed
                    # succeeded. All-or-nothing is the only shape that cannot
                    # lose art.
                    logger.warning("crew-companion: unsafe pack filename: %r", name)
                    shutil.rmtree(staging, ignore_errors=True)
                    return False
                if safe.casefold() in seen_casefolded:
                    # Two names differing only in case (`idle.svg` / `IDLE.SVG`)
                    # are distinct dict keys but ONE file on the case-insensitive
                    # filesystems macOS and Windows default to — the second
                    # write silently replaced the first while the import
                    # reported success. Same all-or-nothing rule as above: a
                    # save that would lose one file's art refuses entirely.
                    logger.warning(
                        "crew-companion: case-colliding pack filename: %r", name
                    )
                    shutil.rmtree(staging, ignore_errors=True)
                    return False
                seen_casefolded.add(safe.casefold())
                if safe.lower() == "manifest.json":
                    # RESERVED: the manifest is generated above from the
                    # validated payload. A pack file with this name overwrites
                    # it wholesale — letting bundle content forge the manifest
                    # and defeating the import path's inner-id normalization.
                    # Case-insensitive: macOS/Windows filesystems would collide
                    # on MANIFEST.JSON too.
                    logger.warning("crew-companion: reserved pack filename: %r", name)
                    shutil.rmtree(staging, ignore_errors=True)
                    return False
                if len(content.encode("utf-8")) > MAX_FILE_BYTES:
                    logger.warning("crew-companion: pack file too large: %s", safe)
                    shutil.rmtree(staging, ignore_errors=True)
                    return False
                (staging / safe).write_text(content, "utf-8")
            # Move the old pack ASIDE, don't delete it, until the new one is in
            # place. `rmtree` then `os.replace` leaves a window where the pack does
            # not exist at all: if the rename fails or the gateway exits between the
            # two, the user's custom art is gone with nothing to restore from. An
            # overwrite is an EDIT, and an edit that can lose the original is not a
            # trade worth the two extra lines this avoids.
            backup: Path | None = None
            if target.exists():
                backup = target.with_name(f"{target.name}.old.{os.getpid()}")
                if backup.exists():
                    shutil.rmtree(backup, ignore_errors=True)
                os.replace(target, backup)
            try:
                os.replace(staging, target)
            except OSError:
                if backup is not None:      # put the original back, then report
                    os.replace(backup, target)
                raise
            if backup is not None:
                shutil.rmtree(backup, ignore_errors=True)
            return True
        except OSError as exc:
            logger.warning("crew-companion: pack save failed: %s", exc)
            try:
                if staging.exists():
                    shutil.rmtree(staging)
            except OSError:
                pass
            return False

    # ── internals ───────────────────────────────────────────────────────────

    def _builtin_meta(self) -> PackMeta:
        return PackMeta(
            id=DEFAULT_PACK,
            name="Kiro",
            author="Kiro Crew",
            description="The default companion.",
            type="builtin",
            format="svg",
            recoloured=bool(self._colour_maps.get(DEFAULT_PACK)),
        )

    def _read_manifest(self, pack_dir: Path) -> dict[str, Any] | None:
        path = pack_dir / "manifest.json"
        try:
            # Same link rejection as `_read_pack_file`: a symlinked
            # manifest.json would read ANY JSON file on disk and surface its
            # fields (names, paths) through the gallery listing.
            if is_link_or_junction(path):
                logger.warning(
                    "crew-companion: refusing linked manifest in %s", pack_dir.name
                )
                return None
            if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                return None
            raw = json.loads(path.read_text("utf-8"))
            return raw if isinstance(raw, dict) else None
        except (OSError, ValueError):
            return None

    def _read_meta(self, pack_dir: Path) -> PackMeta | None:
        manifest = self._read_manifest(pack_dir)
        if manifest is None:
            return None
        meta = manifest.get("meta")
        if not isinstance(meta, dict):
            return None
        # The DIRECTORY is the identity, never the manifest's self-declared id.
        # Every write path validates and uses the directory name (save targets
        # _root/<ident>, detail resolves by it) — but listing trusted the inner
        # `meta.id`, so a hand-edited or legacy-imported pack could list itself
        # under ANOTHER pack's id, and deleting/selecting that entry hit the
        # victim's directory. Same identity-spoof class as the import fix; this
        # closes the local half.
        ident = pack_dir.name
        fmt = meta.get("format")
        return PackMeta(
            id=ident,
            name=str(meta.get("name") or ident),
            author=str(meta.get("author") or ""),
            description=str(meta.get("description") or ""),
            type="custom",
            format=fmt if fmt in FORMATS else "svg",
            recoloured=bool(self._colour_maps.get(ident)),
        )

    def _read_pack_file(self, pack_dir: Path, filename: Any) -> str | None:
        safe = _safe_filename(filename)
        if safe is None:
            return None
        path = pack_dir / safe
        try:
            # Reads must not follow links: a symlink (or Windows junction)
            # named like a pack file resolves to ANY readable file on disk —
            # `~/.ssh` keys, config with secrets — and the detail endpoint
            # would inline its contents to the frontend. Refuse the link
            # itself, then belt-and-suspenders the resolved path back inside
            # the packs root (covers a linked intermediate directory too).
            if is_link_or_junction(path):
                logger.warning("crew-companion: refusing linked pack file: %s", safe)
                return None
            resolved = path.resolve()
            if self._root.resolve() not in resolved.parents:
                logger.warning("crew-companion: pack file escapes root: %s", safe)
                return None
            if not resolved.is_file() or resolved.stat().st_size > MAX_FILE_BYTES:
                return None
            return resolved.read_text("utf-8")
        except (OSError, ValueError):
            return None

    def _save_colours(self) -> None:
        """Persist the colour maps. Raises OSError on write failure.

        This used to catch-and-log, which meant a disk-full or read-only
        write was acknowledged as success: the route returned 200, the UI
        showed the new colour, and a restart silently reloaded the old map.
        Callers that can roll back do so; the route wrapper maps the raised
        OSError to 503 store_write_failed (same contract as the reminder
        store).
        """
        tmp = self._colour_path.with_suffix(f".json.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(self._colour_maps, indent=2), "utf-8")
        # chmod_safe, not os.chmod: the root AGENTS.md mandates the
        # platform_compat shim, which is a no-op where POSIX modes mean
        # nothing (Windows) instead of raising or silently misleading.
        chmod_safe(tmp, 0o600)
        os.replace(tmp, self._colour_path)


def _safe_filename(raw: Any) -> str | None:
    """Validate a filename as a single segment inside a pack directory.

    Same reasoning as ``_safe_id``: these names come from a manifest that may be
    hand-edited, and a name containing a separator would read or write outside the
    pack.
    """
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    if not name or len(name) > 128:
        return None
    if name.startswith(".") or ".." in name:
        return None
    # An ALLOWLIST, like `_safe_id` uses, rather than a list of things to reject.
    # The denylist version here checked `/`, `\` and `..` and so missed a Windows
    # drive prefix: `Path("packs/x") / "C:evil.json"` resolves to `C:evil.json`,
    # writing outside the pack without containing a separator at all. Enumerating
    # what a pack file may be named ends that whole class of miss instead of adding
    # `:` and waiting for the next character to turn up (NUL, `*`, `?`, a colon's
    # NTFS stream suffix). Pack files are `idle.svg` / `manifest.json` /
    # `random-<name>.png`, all of which this allows.
    if not all(c.isalnum() or c in "-_." for c in name):
        return None
    return name
