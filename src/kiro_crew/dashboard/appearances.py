"""The crew appearance library — the dashboard's OWN pack store.

Crews can wear an appearance pack (``agents.*.avatar = {"kind": "pack", "id":
...}``). The pack FORMAT is the one Crew Companion defined — a manifest plus
animation files — and ``AppearanceStore`` already reads and writes it, so this
module reuses that class rather than writing a second reader for the same files.

What it does NOT do is share Crew Companion's library. That app is independent:
it owns its own packs under its own data directory, gated on the app being
enabled, and nothing here reads or moves them. Crews get a second, separate
library rooted at the data home. Two stores of the same class, two directories,
no migration between them and no cross-reach in either direction. A user who
wants a Companion pack on a crew exports it from the gallery and imports it here
through ``POST /api/appearances/import`` — the bundle format is the same.

Why separate rather than shared: the two surfaces have different lifetimes (a
crew's face must render while the Companion app is disabled), different owners
(a dashboard route vs an app route), and different deletion rules (a crew may
still be wearing a pack). Every attempt to make one store serve both produced a
new hazard at the seam — a migration that could strand a pack, a gallery delete
that could blank a crew, an import cycle between the app package and the
dashboard. Keeping them apart removes the seam.

**Shape.** A lazily-built process-wide instance behind a ``threading.Lock``, the
same shape ``artifacts.get_default_store`` uses for the artifact store and for
the same reason: the first caller may be a request handler or a test, and neither
is a natural owner of construction. It is rooted per call through
``data_home()`` so a pod or a test with a ``KIROCREW_HOME`` override never
reaches the real install.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import threading
from pathlib import Path
from typing import Literal

from kiro_crew.appearance_packs import pack_id_key, safe_pack_id
from kiro_crew.appearance_packs.store import AppearanceStore
from kiro_crew.config.paths import data_home
from kiro_crew.loop_lock import LoopBoundLock

#: The crew library's directory under the data home. A directory of its own
#: rather than a bare ``appearances/`` at the top level, because the store keeps
#: its colour maps beside the packs and both belong to the same library.
LIBRARY_DIRNAME = "appearance-library"

_store: AppearanceStore | None = None
_store_lock = threading.Lock()

#: Serializes every MUTATION of the crew library (import, delete). ``LoopBoundLock``
#: rather than a bare ``asyncio.Lock``: it rebinds per running loop, which is the
#: repo-wide rule ``scripts/check_loop_bound_locks.py`` enforces. Ordering when
#: both are held: the agents config lock first, this one second.
_library_mutation_lock = LoopBoundLock()


def _library_lock() -> LoopBoundLock:
    return _library_mutation_lock


def library_dir() -> Path:
    """Where the crew library lives.

    Resolved per call, never bound at import: ``data_home()`` honours a
    ``KIROCREW_HOME`` override set after this module was imported, which is what
    keeps a pod, a dev backend and a test from reaching into the real install.
    """
    return data_home() / LIBRARY_DIRNAME


def get_appearance_store() -> AppearanceStore:
    """The process-wide crew appearance store, built on first use."""
    global _store
    with _store_lock:
        if _store is None:
            store = AppearanceStore(library_dir())
            store.load()
            _store = store
        return _store


async def crews_wearing(pack_id: str) -> list[str]:
    """The crews whose ``avatar`` names *pack_id*, sorted.

    Compared on :func:`pack_id_key` (NFC-normalized, then casefolded). A pack id
    is a directory name, and on macOS and Windows the filesystem treats
    ``Aurora`` and ``aurora`` as one directory; macOS also treats the NFC and
    NFD spellings of one string as one name. So a crew wearing ``Aurora`` is
    wearing whatever ``DELETE .../aurora`` would remove there, and an exact
    comparison finds no wearer and lets the delete through. The store already
    refuses case-colliding FILENAMES inside a pack on the same reasoning
    (``save_pack``'s ``seen_casefolded``); this is the same rule one level up,
    widened to normalization. On a strict filesystem the key can only
    over-report a wearer (refusing a delete of ``aurora`` because ``Aurora`` is
    worn), which costs one ``?force=1`` and never a pack.

    A fresh read of the config on every call; the lock discipline that makes
    the answer usable is the caller's -- see :func:`delete_pack_if_unworn`.
    """
    return await asyncio.to_thread(_crews_wearing_sync, pack_id)


def _crews_wearing_sync(pack_id: str) -> list[str]:
    """Blocking core of :func:`crews_wearing`; runs in a worker thread."""
    # circular import: config.loader reaches back into this package through the
    # dashboard handler tree, so the config model is imported at call time.
    from kiro_crew.config.loader import KiroCrewConfig

    wanted = pack_id_key(pack_id)
    cfg = KiroCrewConfig.load()
    return sorted(
        name
        for name, agent in cfg.agents.items()
        if agent.avatar.get("kind") == "pack"
        and isinstance(agent.avatar.get("id"), str)
        and pack_id_key(agent.avatar["id"]) == wanted
    )


async def delete_pack_if_unworn(pack_id: str, *, force: bool = False) -> tuple[bool, list[str]]:
    """Delete a custom pack unless a crew still wears it.

    Returns ``(deleted, wearers)``. ``wearers`` is non-empty only when the delete
    was REFUSED, so a caller distinguishes "still worn" from "no such pack" by
    that list rather than by re-deriving it.

    Three mechanics carry the guarantee:

    * The in-use read and the delete run under the agents routes' own config
      lock. Without it a crew save landing between them would leave exactly the
      reference the check exists to find.
    * The delete goes through ``_drained_to_thread``, so a cancelled request
      cannot release that lock with a directory removal still in flight.
    * The pack id is CANONICALIZED once, and the same canonical value drives both
      the wearer lookup and the delete. The store normalizes through
      ``safe_pack_id`` (which strips whitespace) while a raw comparison against
      the config does not, so ``"aurora "`` would find no wearer and then delete
      ``aurora`` — the guard bypassed by a trailing space. An id the store would
      refuse outright is answered as "no such pack" without touching the config.
      Case and Unicode normalization are the other variants of the same
      bypass and are handled inside :func:`crews_wearing` via ``pack_id_key``.
    """
    # circular import: handlers.agents imports this module for the store and the
    # guard, so the lock and the drained-worker helper it owns can only be
    # reached at call time.
    from kiro_crew.dashboard.handlers.agents import _drained_to_thread, _get_config_lock

    maybe_ident = safe_pack_id(pack_id)
    if maybe_ident is None:
        return False, []
    ident: str = maybe_ident
    # circular import: same reason as above, and the same call-time shape.
    from kiro_crew.config.loader import _config_write_lock, _lock_target, config_path

    def _check_then_delete() -> tuple[bool, list[str]]:
        # The wearer read and the delete share ONE hold of the config file's
        # sidecar flock. ``_get_config_lock`` is an asyncio lock: it serializes
        # this against sibling dashboard handlers and nothing else, while the
        # CLI, the boot refresh and any other gateway process write through the
        # ``<config>.json.lock`` sidecar. Read under only the loop lock, a CLI
        # ``avatar`` write could land between "nobody wears it" and ``rmtree``,
        # leaving a crew pointed at art that is gone. Taken in a
        # worker, loop lock first, flock second -- the order
        # ``_config_write_lock`` documents -- so the flock wait never blocks the
        # loop and cannot deadlock against ``run_config_write`` holders. A forced
        # delete skips the read but keeps the hold, so it too cannot straddle a
        # concurrent config publish.
        # ``_lock_target``: a symlinked config.json is resolved before locking by
        # every writer (``update_config_locked``, ``KiroCrewConfig.save``), so the
        # sidecar sits beside the TARGET. Locking the unresolved path would take
        # a sidecar beside the link instead -- a different file, so no mutual
        # exclusion with the writers this hold exists to serialize against.
        with _config_write_lock(_lock_target(config_path())):
            if not force:
                wearers = _crews_wearing_sync(ident)
                if wearers:
                    return False, wearers
            return bool(store.delete_pack(ident)), []

    store = await asyncio.to_thread(get_appearance_store)
    async with _get_config_lock():
        # Library lock INSIDE the config lock, always in that order, so the delete
        # cannot interleave with an import of the same id (see ``import_pack``).
        async with _library_lock():
            return await _drained_to_thread(_check_then_delete)


_SLOT_CATEGORIES = ("states", "moods", "random")

#: Content type per slot format. The format is decided by the FILENAME the
#: manifest references (``pack_detail`` does the same), never by the manifest's
#: own ``meta.format``: a mixed pack stamps every slot with one format.
_SLOT_CONTENT_TYPES = {"svg": "image/svg+xml", "lottie": "application/json", "sprite": "image/png"}

#: Filename suffix -> slot format, for the files a slot may reference. This is
#: narrower than the importer's ``ALLOWED_SUFFIXES`` on purpose: ``.webp`` and
#: ``.gif`` may ride in a bundle, but the detail reader has no decoder for them
#: and would hand their base64 to the browser as text, so a slot that names one
#: can never draw.
_RENDERABLE_SUFFIXES = {".svg": "svg", ".json": "lottie", ".png": "sprite"}

#: Ceiling on the bytes a pack's SLOTS add up to once every reference is
#: expanded -- the sum, over every slot in the manifest, of the size of the
#: file it names. A bundle is capped as a whole, but a manifest may point any
#: number of slots at one file, and the detail reader inlines the content per
#: SLOT: a thousand slots on an 8 MB sprite is 8 MB on the wire and 8 GB in
#: the detail payload. Sized to the bundle cap itself, which any self-consistent
#: export already satisfies (a slot's file is carried once and referenced once).
MAX_EXPANDED_SLOT_BYTES = 24 * 1024 * 1024


def slot_format_for(filename: str) -> str | None:
    """The slot format the reader assigns a referenced file, or ``None``."""
    for suffix, fmt in _RENDERABLE_SUFFIXES.items():
        if filename.lower().endswith(suffix):
            return fmt
    return None


def slot_body(content: str, fmt: str) -> tuple[bytes, str] | None:
    """One slot's bytes and content type, or ``None`` when it will not decode.

    THE predicate for "can this slot be served": the slot route answers 404
    exactly when this returns ``None``, and the import gate refuses a bundle
    exactly when this would return ``None`` for a file the manifest references.
    One function on both sides is what stops the gate and the reader drifting
    into "installed fine, renders nothing".
    """
    if fmt == "sprite":
        try:
            raw = base64.b64decode("".join(content.split()), validate=True)
        except (binascii.Error, ValueError):
            return None
        # A decoded sprite must be a PNG: the reader serves it as ``image/png``
        # and a browser given eight bytes of something else draws a broken tile.
        if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return None
        return raw, _SLOT_CONTENT_TYPES[fmt]
    if not content.strip():
        return None
    return content.encode("utf-8"), _SLOT_CONTENT_TYPES.get(fmt, "image/svg+xml")


def bundle_reference_error(payload: object) -> str | None:
    """Why a bundle would install a pack the slot route cannot serve, or None.

    ``import_bundle`` validates every file the bundle CARRIES (name, suffix,
    size) but never that the files the manifest REFERENCES are among them, so a
    bundle naming ``idle.svg`` while shipping only ``other.svg`` installs a pack
    whose every slot answers 404. Nothing pre-existing is harmed -- a colliding
    id is refused, so the broken pack is always a new entry -- but the user is
    handed a face that renders as nothing with a 200 that said it worked. Checked
    here, before the write, on the same three category maps the detail reader
    resolves and with the same decode predicate the slot route serves through
    (:func:`slot_body`): a reference the reader would skip, or content the
    route would 404, is a bundle the import refuses. Only REFERENCED files are
    judged -- an extra carried file nothing points at can harm no slot.
    """
    if not isinstance(payload, dict):
        return None  # import_bundle answers this one with its own message
    manifest = payload.get("manifest")
    files = payload.get("files")
    if not isinstance(manifest, dict) or not isinstance(files, dict):
        return None
    carried = {str(name) for name in files}
    # Two carried names that are one FILE on the filesystems the gateway runs
    # on: macOS and Windows fold case, and macOS also folds NFC/NFD spellings.
    # ``save_pack`` refuses the case-collision itself, but it compares
    # casefolded only, so a bundle carrying both spellings of one accented
    # name wrote the second over the first and reported success with one
    # slot's art gone. The same key the wearer guard compares pack ids on.
    seen_keys: dict[str, str] = {}
    for name in sorted(carried):
        key = pack_id_key(name)
        if key in seen_keys:
            return f"That bundle carries {name!r} and {seen_keys[key]!r}, which are one file on some filesystems"
        seen_keys[key] = name
    # Decode each DISTINCT file once. A manifest may point many slots at one
    # file (a pack with one frame for every mood is legitimate), and a sprite
    # decode is proportional to the file, so judging per REFERENCE let a
    # manifest with thousands of slots on one large file multiply the work by
    # the slot count. Per file, the work is bounded by the bundle cap
    # ``import_bundle`` already enforces on the whole payload.
    decodes: dict[str, bool] = {}
    expanded = 0
    for category in _SLOT_CATEGORIES:
        section = manifest.get(category)
        if section is None:
            continue
        if not isinstance(section, dict):
            return f"That bundle's {category} section is not a map of slots to files"
        for slot, filename in section.items():
            if not isinstance(filename, str) or filename not in carried:
                return f"That bundle names {category}.{slot} = {filename!r} but does not carry it"
            fmt = slot_format_for(filename)
            if fmt is None:
                return f"That bundle's {category}.{slot} = {filename!r} is not a renderable format"
            if filename not in decodes:
                content = files[filename]
                decodes[filename] = isinstance(content, str) and slot_body(content, fmt) is not None
            if not decodes[filename]:
                return f"That bundle's {category}.{slot} = {filename!r} does not decode"
            expanded += len(files[filename])
            if expanded > MAX_EXPANDED_SLOT_BYTES:
                return "That bundle's slots reference more art than a pack may carry"
    states = manifest.get("states")
    if not isinstance(states, dict) or "idle" not in states:
        return "That bundle has no idle state, so nothing could ever be drawn"
    return None


#: Slot fallback chains, resolved server-side. Only the three agent lifecycle
#: states need one: a pack is free to ship just ``idle``, and every richer slot
#: is an upgrade rather than a requirement. The ``working`` chain carries the
#: legacy names (``loading``, ``thinking``) a desktop-era pack used, so such a
#: pack still animates instead of falling straight through to ``idle``.
SLOT_FALLBACKS: dict[str, tuple[str, ...]] = {
    "working": ("working", "loading", "thinking", "idle"),
    "done": ("done", "idle"),
    "error": ("error", "idle"),
}


def pack_slot_file(
    store: "AppearanceStore", pack_id: str, slot: str
) -> tuple[str, str, str] | None | Literal["no-pack"]:
    """ONE slot's ``(resolved_slot, content, format)`` -- or why not.

    Reads the manifest and the single file the resolved slot names, and nothing
    else. ``pack_detail`` inlines EVERY file in the pack per slot that names it,
    which makes serving one frame cost the whole pack in memory on every roster
    refresh -- and a manifest is free to point every slot at the largest file.
    The roster asks for one frame at a time; this reads one frame.

    ``"no-pack"`` when the id does not resolve to a pack (the route's 404 with
    ``pack_not_found``), ``None`` when the pack exists but no slot in the chain
    has readable content (``slot_not_found``). Blocking; run in a worker.
    """
    ident = safe_pack_id(pack_id)
    if ident is None:
        return "no-pack"
    pack_dir = store._root / ident
    manifest = store._read_manifest(pack_dir)
    if manifest is None:
        return "no-pack"
    for candidate in SLOT_FALLBACKS.get(slot, (slot,)):
        for category in _SLOT_CATEGORIES:
            section = manifest.get(category)
            if not isinstance(section, dict):
                continue
            filename = section.get(candidate)
            if not isinstance(filename, str):
                continue
            fmt = slot_format_for(filename)
            if fmt is None:
                continue
            content = store._read_pack_file(pack_dir, filename)
            if content is None:
                continue
            return candidate, content, fmt
    return None


async def import_pack(payload: object) -> dict:
    """Install a bundle into the crew library, serialized with every other mutation.

    ``import_bundle`` checks ``pack_exists`` and then writes through a staging
    directory named ``.tmp-<id>-<pid>`` — the SAME name for two requests in one
    process. Two concurrent imports of one id therefore both passed the collision
    check and then interleaved inside that one staging directory, so a
    "successful" response could install one request's manifest over the other's
    art. Holding the library lock across check-and-write makes the pair atomic,
    and running under ``_drained_to_thread`` means a cancelled request cannot
    release the lock with the write still in flight.
    """
    # boot path: `appearance_packs.transfer` builds a urllib opener at module
    # scope, and this module is imported by the dashboard's route table before
    # the socket binds; importing it here means the first import request pays
    # that, not the launch. The store itself does no such work, so it is a
    # normal top-level import.
    from kiro_crew.appearance_packs.transfer import import_bundle
    from kiro_crew.dashboard.handlers.agents import _drained_to_thread

    def _check_then_import(store: AppearanceStore) -> dict:
        # Both halves off the event loop: the reference check decodes every
        # distinct sprite the manifest names, which on a bundle at the size cap
        # is real CPU work, and a synchronous stretch that long on the loop is
        # what the stall watchdog treats as a hung gateway.
        problem = bundle_reference_error(payload)
        if problem is not None:
            return {"ok": False, "error": problem}
        return import_bundle(store, payload)

    store = await asyncio.to_thread(get_appearance_store)
    async with _library_lock():
        return await _drained_to_thread(_check_then_import, store)


def _reset_for_tests() -> None:
    """Drop the process-global store so the next call rebuilds it. Tests only."""
    global _store
    _store = None
