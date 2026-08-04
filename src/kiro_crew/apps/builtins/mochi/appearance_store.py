"""Appearance-pack storage — the persistence behind the Avatars surface.

PORTED from the original's ``gallery:save-sprite-pack`` /
``appearanceRegistry`` pair (``main/ipcHandlers.ts``), which wrote packs into the
Electron app's ``userData/appearances/<packId>/``. Here the same layout lives
under Mochi's app data dir, so a pack survives independently of the shell:

    <data_dir>/appearances/<packId>/
        manifest.json       meta + state/mood -> filename maps + sprite config
        source.png          the original sheet, kept so a pack can be re-edited
        idle.png ...        one PNG per assigned state or mood

WHY A FILE TREE RATHER THAN THE SETTINGS STORE
A pack is several hundred KB of image data. ``settings.py`` is a small
atomically-rewritten JSON document read on every settings request; putting sprite
sheets in it would rewrite megabytes on every unrelated preference change.

The one behavior worth calling out, because losing it silently breaks re-editing:
overwriting a pack PRESERVES ``source.png`` when the caller does not supply a new
sheet. The original did this deliberately — the editor loads the source sheet to
re-slice it, so an overwrite that dropped it would leave the pack un-editable.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import shutil
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from kiro_crew.atomic_write import atomic_write

logger = logging.getLogger(__name__)

APPEARANCES_DIR = "appearances"
MANIFEST_FILE = "manifest.json"
SOURCE_FILE = "source.png"

#: Slots that are pet STATES; anything else assigned is treated as a mood.
#:
#: MUST mirror the renderer's ``appearanceTypes.ts`` (REQUIRED_STATES +
#: OPTIONAL_STATES). It did not: this list carried ``sleeping`` / ``peek`` /
#: ``peek_thinking`` / ``docked`` / ``focus`` — names the editor never emits —
#: while MISSING ``offline`` / ``peeking`` / ``peekThinking``, which it does. A
#: state name absent here is silently filed under ``moods``, where the resolver
#: never looks it up, so that art vanished with no error. ``sleeping`` was the
#: mirror-image bug: it is a MOOD in the renderer, and filing it as a state hid it
#: the same way.
#:
#: ``idle`` is the only slot the renderer requires; every other state falls back
#: through AnimationResolver's chain, which is why one drawing is a complete pack.
REQUIRED_STATES = ("idle",)
OPTIONAL_STATES = (
    # Status slots the app drives: a finished turn, a failed turn, a running turn.
    "done",
    "error",
    "loading",
    # Spontaneous behaviour.
    "walking",
    # Legacy names, still honoured for packs authored before the status/random
    # split — and still what THIS backend's state machine emits.
    "thinking",
    "working",
    "offline",
    "peeking",
    "peekThinking",
)

_DATA_URI_PREFIX = re.compile(r"^data:image/\w+;base64,")

#: A pack id must be safe to use as a single path segment. Ids we mint are UUID4,
#: but an overwrite id arrives from the client, so it is validated rather than
#: trusted — otherwise `../` in an id would let a caller write outside the tree.
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class PackError(Exception):
    """A pack could not be saved or read."""


def appearances_dir(data_dir: Path) -> Path:
    return data_dir / APPEARANCES_DIR


def _pack_dir(data_dir: Path, pack_id: str) -> Path:
    """The directory for one pack, proven to sit inside the appearances root.

    Two checks, not one. ``_SAFE_ID`` already rejects every character that could
    walk out (no ``/``, no ``\\``, no ``.``), so the containment assert below is
    unreachable by any input that gets this far — it is kept because it is the
    check that holds even if the pattern is later loosened, and because a
    character allow-list is invisible to dataflow analysis: CodeQL reported
    seventeen "uncontrolled data in path expression" findings against this module
    on the strength of the sanitiser living in a different function. Resolving and
    comparing makes the barrier explicit to a reader and to the scanner.
    """
    if not _SAFE_ID.match(pack_id):
        raise PackError(f"invalid pack id: {pack_id!r}")
    root = appearances_dir(data_dir).resolve()
    target = (root / pack_id).resolve()
    if target != root and root not in target.parents:
        raise PackError(f"pack id escapes the appearances directory: {pack_id!r}")
    # Return the CHECKED path, not a second join of the same parts. Re-joining
    # produced a path expression that no longer carried the barrier, so every
    # caller that appended a filename to it was flagged again — the guard has to
    # be on the value that actually escapes this function.
    return target


@contextmanager
def _staged_pack(pack_dir: Path) -> Iterator[Path]:
    """Build a pack in a staging directory, then swap it in atomically.

    THREE paths write a pack — the sprite importer, the editor's per-slot save, and
    a `.mochipack.zip` import — and every one of them can overwrite an existing
    pack. Writing in place means a failure partway through (a full disk, a
    CRC-corrupt ZIP entry, a bad data URI) leaves the live pack half-replaced: the
    manifest may still reference files that were not written, and a pack the user
    built by hand is gone. Fixing that in one path and not the others is how this
    came back a second time, so the swap lives here and every writer uses it.

    Order: build in `<pack>.staging-<rand>`, move any existing pack aside to
    `<pack>.old-<rand>`, move staging into place, then delete the old copy. A crash
    between the two moves leaves the recoverable `.old-` directory rather than
    nothing, and any exception removes the staging tree and leaves the live pack
    untouched.
    """
    staging = pack_dir.with_name(pack_dir.name + f".staging-{uuid.uuid4().hex[:8]}")
    try:
        staging.mkdir(parents=True)
        yield staging
        retired: Path | None = None
        if pack_dir.exists():
            retired = pack_dir.with_name(pack_dir.name + f".old-{uuid.uuid4().hex[:8]}")
            os.replace(pack_dir, retired)
        try:
            os.replace(staging, pack_dir)
        except OSError:
            if retired is not None:
                os.replace(retired, pack_dir)  # put the old pack back
            raise
        if retired is not None:
            shutil.rmtree(retired, ignore_errors=True)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _decode_data_uri(value: str) -> bytes:
    """Decode a ``data:image/...;base64,`` payload, or raise PackError."""
    try:
        return base64.b64decode(_DATA_URI_PREFIX.sub("", value), validate=True)
    except Exception as exc:  # noqa: BLE001 — any decode failure is a bad request
        raise PackError(f"malformed image data: {exc}") from exc


def save_sprite_pack(data_dir: Path, payload: dict[str, Any]) -> str:
    """Write a sprite pack and return its id.

    ``payload`` mirrors the editor's output: ``name`` / ``author`` /
    ``description``, the sprite geometry (``frameWidth``, ``frameHeight``,
    ``fps``, ``flipX``, ``offsetY``), ``assignments`` (slot -> data URI),
    ``rowAssignments``, an optional ``sourceImage`` data URI, and an optional
    ``overwriteId`` to replace an existing pack in place.
    """
    assignments = payload.get("assignments")
    if not isinstance(assignments, dict) or not assignments:
        raise PackError("a pack needs at least one assigned slot")

    overwrite_id = payload.get("overwriteId")
    pack_id = str(overwrite_id) if overwrite_id else str(uuid.uuid4())
    pack_dir = _pack_dir(data_dir, pack_id)

    # Decode EVERYTHING before touching the existing pack. An overwrite used to
    # rmtree first and decode after, so one malformed data URI destroyed the
    # pack it was replacing — validation failed, but the deletion had already
    # happened. Decode errors must leave the existing pack untouched.
    decoded_slots: dict[str, bytes] = {}
    for slot, data_uri in assignments.items():
        if not isinstance(slot, str) or not _SAFE_ID.match(slot):
            raise PackError(f"invalid slot name: {slot!r}")
        if not isinstance(data_uri, str):
            raise PackError(f"slot {slot} has no image")
        decoded_slots[slot] = _decode_data_uri(data_uri)

    source_uri = payload.get("sourceImage")
    decoded_source: bytes | None = None
    if isinstance(source_uri, str) and source_uri:
        decoded_source = _decode_data_uri(source_uri)

    # Preserve the existing sheet: without it an overwrite that supplies no new
    # sheet leaves the pack un-editable.
    preserved_source: bytes | None = None
    if overwrite_id:
        existing = pack_dir / SOURCE_FILE
        if existing.is_file():
            preserved_source = existing.read_bytes()

    # One staged swap for every pack writer (see `_staged_pack`): the live
    # directory is only ever replaced by a COMPLETE pack.
    with _staged_pack(pack_dir) as staging:
        if decoded_source is not None:
            (staging / SOURCE_FILE).write_bytes(decoded_source)
        elif preserved_source is not None:
            (staging / SOURCE_FILE).write_bytes(preserved_source)

        states: dict[str, str] = {}
        moods: dict[str, str] = {}
        for slot, blob in decoded_slots.items():
            filename = f"{slot}.png"
            (staging / filename).write_bytes(blob)
            if slot in REQUIRED_STATES or slot in OPTIONAL_STATES:
                states[slot] = filename
            else:
                moods[slot] = filename

        has_source = (staging / SOURCE_FILE).is_file()
        manifest: dict[str, Any] = {
            "meta": {
                "id": pack_id,
                "name": payload.get("name") or "Sprite Pack",
                "author": payload.get("author") or "Unknown",
                "description": payload.get("description") or "A sprite-based pet",
                "type": "custom",
                "format": "sprite",
                "thumbnail": states.get("idle", ""),
            },
            "states": states,
            "moods": moods,
            "sprite": {
                "frameWidth": payload.get("frameWidth"),
                "frameHeight": payload.get("frameHeight"),
                "fps": payload.get("fps"),
                "flipX": bool(payload.get("flipX")),
                "offsetY": payload.get("offsetY") or 0,
                "rowAssignments": payload.get("rowAssignments"),
                # Absent rather than null when there is no sheet: the wire format
                # keeps optional keys absent, matching the rest of the port.
                **({"source": SOURCE_FILE} if has_source else {}),
            },
        }
        atomic_write(staging / MANIFEST_FILE, json.dumps(manifest, indent=2) + "\n")

    logger.info("mochi: saved appearance pack %s (%d slots)", pack_id, len(assignments))
    return pack_id


def list_packs(data_dir: Path) -> list[dict[str, Any]]:
    """Every readable custom pack's ``meta``, newest first.

    A pack with an unreadable or malformed manifest is SKIPPED rather than
    failing the whole listing — one corrupt directory must not hide the rest.
    """
    root = appearances_dir(data_dir)
    if not root.is_dir():
        return []
    out: list[tuple[float, dict[str, Any]]] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        manifest_path = child / MANIFEST_FILE
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            meta = manifest.get("meta")
            if isinstance(meta, dict):
                out.append((manifest_path.stat().st_mtime, meta))
        except (OSError, ValueError):
            logger.warning("mochi: skipping unreadable pack at %s", child)
    out.sort(key=lambda pair: pair[0], reverse=True)
    return [meta for _, meta in out]


def get_pack_detail(data_dir: Path, pack_id: str) -> dict[str, Any] | None:
    """A pack's full manifest, or None when it does not exist."""
    manifest_path = _pack_dir(data_dir, pack_id) / MANIFEST_FILE
    try:
        parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else None
    except (OSError, ValueError):
        return None


def read_pack_file(data_dir: Path, pack_id: str, filename: str) -> bytes | None:
    """Raw bytes of one file inside a pack, or None if absent.

    ``filename`` is confined to the pack directory: it must be a bare name, so a
    caller cannot walk out with ``../``.
    """
    if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
        raise PackError(f"invalid pack file name: {filename!r}")
    target = _pack_dir(data_dir, pack_id) / filename
    try:
        return target.read_bytes()
    except OSError:
        return None


def delete_pack(data_dir: Path, pack_id: str) -> bool:
    """Remove a pack. True when something was removed."""
    pack_dir = _pack_dir(data_dir, pack_id)
    if not pack_dir.is_dir():
        return False
    shutil.rmtree(pack_dir, ignore_errors=True)
    logger.info("mochi: deleted appearance pack %s", pack_id)
    return not pack_dir.exists()


# ── Bundle export / import (.mochipack.zip) ────────────────────────────────
#
# Ported from the original AppearanceRegistry.exportPack / importBundle. The
# FORMAT is upstream's and must stay compatible: a flat zip containing
# manifest.json plus every file the manifest references.
#
# Upstream shelled out to the macOS `zip` / `unzip` binaries. This uses Python's
# zipfile instead — it works on Windows and Linux too, and it lets the extraction
# be guarded, which is the part that matters: an appearance bundle is UNTRUSTED
# input, and `ZipFile.extractall` will happily write through `../` entries.

#: Uncompressed ceiling for an imported bundle. A pack is a handful of SVGs or
#: one sprite sheet; anything larger is a mistake or a zip bomb.
MAX_BUNDLE_BYTES = 32 * 1024 * 1024

#: Extensions a pack may contain. Anything else (scripts, archives, symlinks)
#: has no reason to be in an appearance pack.
_ALLOWED_SUFFIXES = frozenset({".json", ".svg", ".png", ".webp", ".gif"})


def export_pack(data_dir: Path, pack_id: str) -> bytes:
    """Serialise a pack to `.mochipack.zip` bytes.

    Only the files the manifest REFERENCES are included, so a pack directory that
    accumulated debris exports clean.
    """
    detail = get_pack_detail(data_dir, pack_id)
    if detail is None:
        raise PackError(f'Pack not found: "{pack_id}"')

    wanted: set[str] = {MANIFEST_FILE}
    meta = detail.get("meta") if isinstance(detail.get("meta"), dict) else {}
    thumbnail = meta.get("thumbnail") if isinstance(meta, dict) else None
    if isinstance(thumbnail, str) and thumbnail:
        wanted.add(thumbnail)
    for group in ("states", "moods"):
        slots = detail.get(group)
        if isinstance(slots, dict):
            for filename in slots.values():
                if isinstance(filename, str) and filename:
                    wanted.add(filename)
    sprite = detail.get("sprite")
    if isinstance(sprite, dict) and isinstance(sprite.get("source"), str):
        wanted.add(sprite["source"])

    pack_dir = _pack_dir(data_dir, pack_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(wanted):
            # Upstream verified every referenced file exists BEFORE writing the
            # archive, so a half-written bundle never reaches the user.
            target = (pack_dir / name).resolve()
            if not target.is_file() or pack_dir.resolve() not in target.parents:
                raise PackError(f'Missing file for export: "{name}"')
            archive.write(target, arcname=name)
    return buf.getvalue()


def import_bundle(data_dir: Path, blob: bytes) -> dict[str, Any]:
    """Install a `.mochipack.zip` and return the imported pack's ``meta``.

    Every guard here exists because the bundle is untrusted:
      - entry names must be flat and plain (no ``..``, no absolute path, no
        directory component) — `extractall` would otherwise write anywhere;
      - only art/manifest extensions are accepted;
      - the total uncompressed size is capped before anything is written.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except (zipfile.BadZipFile, OSError) as exc:
        raise PackError("Failed to extract ZIP file: file may be corrupted") from exc

    with archive:
        entries = [e for e in archive.infolist() if not e.is_dir()]
        total = sum(e.file_size for e in entries)
        if total > MAX_BUNDLE_BYTES:
            raise PackError("Bundle is too large")
        for entry in entries:
            name = entry.filename
            if name != Path(name).name or name in ("", ".", ".."):
                raise PackError(f"Unsafe entry in bundle: {name!r}")
            if Path(name).suffix.lower() not in _ALLOWED_SUFFIXES:
                raise PackError(f"Disallowed file in bundle: {name!r}")

        if MANIFEST_FILE not in [e.filename for e in entries]:
            raise PackError("Missing manifest.json in pack bundle")
        try:
            manifest = json.loads(archive.read(MANIFEST_FILE).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise PackError("Cannot read manifest.json from pack bundle") from exc
        if not isinstance(manifest, dict) or not isinstance(manifest.get("meta"), dict):
            raise PackError("Invalid manifest: missing meta")
        pack_id = manifest["meta"].get("id")
        if not isinstance(pack_id, str) or not pack_id:
            raise PackError("Invalid manifest: meta.id is required")
        # Reuse the same id sanitiser the save path uses, so an imported pack can
        # never escape the appearances directory either.
        target = _pack_dir(data_dir, pack_id)
        # An imported pack is always user-owned, whatever the bundle claims.
        manifest["meta"]["type"] = "custom"

        # VALIDATE BEFORE STAGING. `_staged_pack` swaps the new pack in
        # atomically, so a bundle that reuses an existing pack's id but omits a
        # REQUIRED state (idle) or references a file the archive doesn't contain
        # would irreversibly replace a good, hand-built pack with an unusable one.
        # Reject such a bundle before touching the live pack.
        members = {e.filename for e in entries}
        _states = manifest.get("states")
        states: dict = _states if isinstance(_states, dict) else {}
        missing_states = [s for s in REQUIRED_STATES if not states.get(s)]
        if missing_states:
            raise PackError(
                "Invalid pack bundle: missing required state(s): " + ", ".join(missing_states)
            )
        referenced: set[str] = set()
        for group in ("states", "moods"):
            slots = manifest.get(group)
            if isinstance(slots, dict):
                referenced.update(f for f in slots.values() if isinstance(f, str) and f)
        thumb = manifest["meta"].get("thumbnail")
        if isinstance(thumb, str) and thumb:
            referenced.add(thumb)
        missing_files = sorted(referenced - members)
        if missing_files:
            raise PackError(
                "Invalid pack bundle: manifest references missing file(s): "
                + ", ".join(missing_files)
            )

        # Staged like every other pack write: a CRC-corrupt entry partway through
        # the archive used to leave the pack it was replacing half-overwritten,
        # with a manifest naming files that were never extracted.
        with _staged_pack(target) as staging:
            for entry in entries:
                if entry.filename == MANIFEST_FILE:
                    continue
                (staging / entry.filename).write_bytes(archive.read(entry))
            atomic_write(staging / MANIFEST_FILE, json.dumps(manifest, indent=2), mode=0o600)

    return manifest["meta"]


def save_pack(
    data_dir: Path,
    meta: dict[str, Any],
    states: dict[str, str],
    moods: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Save a pack from per-slot animation CONTENT (the pack editor's path).

    Ported from the original ``AppearanceRegistry.savePack``. ``save_sprite_pack``
    only covers the sprite-sheet importer; without this the Avatars window's
    "Create new" and "Edit" flows could not save at all.

    Upstream's two load-bearing details are kept:
      - the file EXTENSION comes from ``meta.format`` (``lottie`` -> ``.json``),
        because the renderer picks its loader from the extension;
      - identical content is written ONCE and shared by every slot that uses it,
        which is the common case (an author reuses one drawing for several moods).

    One deliberate divergence: only ``idle`` is required, matching this build's
    manifest schema and its resolver's fallback chain. Upstream demanded all six
    states and then had almost no fallbacks, so a one-drawing pack rendered five
    states blank.
    """
    moods = moods or {}
    if not isinstance(meta, dict):
        raise PackError("meta must be an object")
    for slot in REQUIRED_STATES:
        if not str(states.get(slot, "")).strip():
            raise PackError(f"Missing required states: {slot}")

    # Upstream used randomUUID() for an unnamed pack; uuid is already imported.
    pack_id = str(meta.get("id") or "").strip() or f"pack-{uuid.uuid4().hex[:12]}"
    pack_dir = _pack_dir(data_dir, pack_id)

    ext = ".json" if meta.get("format") == "lottie" else ".svg"
    content_to_file: dict[str, str] = {}
    staged_files: dict[str, str] = {}

    def place(slot: str, content: str) -> str | None:
        if not str(content or "").strip():
            return None
        # The slot becomes a FILENAME. State slots come from fixed tuples, but
        # mood slots arrive verbatim from the request body — without this check
        # a mood named `../../<anything>` walks out of the pack directory and
        # atomic_write (which mkdir -p's the parent) plants attacker content
        # anywhere the gateway can write, including the governance keystone.
        # Same allow-list the sprite path already enforces.
        if not _SAFE_ID.match(slot):
            raise PackError(f"invalid slot name: {slot!r}")
        existing = content_to_file.get(content)
        if existing is not None:
            return existing
        filename = f"{slot}{ext}"
        # Collected, not written: the whole set lands in the staging directory
        # below, so a failure between two slots cannot leave the live pack holding
        # half of a new appearance.
        staged_files[filename] = content
        content_to_file[content] = filename
        return filename

    state_map: dict[str, str] = {}
    for slot in (*REQUIRED_STATES, *OPTIONAL_STATES):
        name = place(slot, states.get(slot, ""))
        if name is not None:
            state_map[slot] = name

    mood_map: dict[str, str] = {}
    for slot, content in moods.items():
        name = place(slot, content)
        if name is not None:
            mood_map[slot] = name

    full_meta = {
        **meta,
        "id": pack_id,
        # An editor-made pack is always user-owned, whatever the payload claims —
        # otherwise the user could not delete what they just created.
        "type": "custom",
        "format": "lottie" if ext == ".json" else "svg",
        "thumbnail": state_map.get("idle", ""),
    }
    manifest = {"meta": full_meta, "states": state_map, "moods": mood_map}

    # One staged swap, like the other two writers. It also subsumes the prune step
    # upstream did by hand (deleting files the new manifest no longer references):
    # the staging directory only ever contains the files just written, so a re-save
    # that drops a slot leaves nothing behind to prune.
    with _staged_pack(pack_dir) as staging:
        for filename, content in staged_files.items():
            atomic_write(staging / filename, content)
        atomic_write(staging / MANIFEST_FILE, json.dumps(manifest, indent=2), mode=0o600)

    return full_meta
