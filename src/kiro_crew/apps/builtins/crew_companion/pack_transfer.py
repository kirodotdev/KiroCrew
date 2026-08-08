"""Pack transfer — export, import, and the PetDex registry.

Split out from ``appearances.py`` because this is where the companion stops dealing
only with its own files and starts accepting content from outside: a bundle the user
picked, or a sprite sheet fetched from the internet. That boundary deserves to be
read in one place.

Three rules hold everywhere in this module:

* **A pack id from outside is never trusted as a path.** Ids are re-validated through
  the same guard the rest of the store uses, so an imported bundle naming itself
  ``../../evil`` is rejected rather than sanitised.
* **Everything has a ceiling.** Bundles, individual files and downloads are all capped,
  so a hostile or simply broken input cannot exhaust memory or disk.
* **Only the PetDex host is contacted.** See ``_petdex_asset_url``.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from kiro_crew.apps.builtins.crew_companion.appearances import (
    DEFAULT_PACK,
    MAX_FILE_BYTES,
    _safe_filename,
    _safe_id,
)

logger = logging.getLogger(__name__)

# ── limits ──────────────────────────────────────────────────────────────────

#: Ceiling for a whole imported bundle. Generous next to a real pack (a few hundred
#: KB) but far below anything that would strain memory.
MAX_BUNDLE_BYTES = 24 * 1024 * 1024

#: Ceiling for a single network download, applied while reading rather than trusting
#: Content-Length — a lying header must not be able to stream us dry.
MAX_DOWNLOAD_BYTES = 12 * 1024 * 1024

#: How long any single PetDex request may take.
FETCH_TIMEOUT_SECS = 20

#: Files a bundle may contain. A pack is art plus a manifest; nothing here needs to
#: accept arbitrary extensions, so the allowlist is the check.
ALLOWED_SUFFIXES = (".json", ".svg", ".png", ".webp", ".gif")

# ── PetDex ──────────────────────────────────────────────────────────────────

#: The only host this module will contact.
PETDEX_HOST = "petdex.dev"
PETDEX_MANIFEST_URL = f"https://{PETDEX_HOST}/api/manifest"

#: Accepts a bare slug or a full pet link, which is what users actually paste.
_PETDEX_LINK = re.compile(r"petdex\.dev/(?:[a-z]{2}/)?pets/([^/?#]+)", re.IGNORECASE)
_SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def parse_petdex_slug(raw: Any) -> str | None:
    """Pull a slug out of whatever the user pasted.

    Accepts ``https://petdex.dev/pets/foo``, ``petdex.dev/en/pets/foo`` or a bare
    ``foo``. Returns ``None`` when the result would not be a plausible slug, so a
    pasted essay does not become a request.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    match = _PETDEX_LINK.search(text)
    slug = match.group(1) if match else text.rsplit("/", 1)[-1].split("?")[0].split("#")[0]
    slug = slug.strip().lower()
    return slug if _SLUG_OK.match(slug) else None


def _petdex_asset_url(candidate: Any) -> str | None:
    """Validate an asset URL taken from the PetDex manifest.

    The manifest is third-party data, so the URLs inside it are UNTRUSTED even though
    the manifest itself came from a known host. Without this check a manifest naming
    ``http://127.0.0.1:6799/...`` or an internal address would make the gateway fetch
    it on the attacker's behalf — the request would originate from inside the trust
    boundary, reaching things the attacker cannot reach directly.

    So the host is pinned to PetDex and the scheme to HTTPS. This is a deliberate
    departure from the desktop app, which fetches the manifest's URL as given.
    """
    if not isinstance(candidate, str) or not candidate:
        return None
    try:
        parsed = urllib.parse.urlparse(candidate)
    except ValueError:
        return None
    if parsed.scheme != "https":
        return None
    host = (parsed.hostname or "").lower()
    if host != PETDEX_HOST and not host.endswith(f".{PETDEX_HOST}"):
        logger.warning("crew-companion: refusing off-host PetDex asset: %s", host)
        return None
    return candidate


class _PinnedRedirects(urllib.request.HTTPRedirectHandler):
    """Re-check the host and scheme on EVERY redirect hop, not just the first URL.

    Without this the host pinning above is decorative: ``urlopen`` follows redirects
    by default, so a manifest naming a perfectly valid ``https://petdex.dev/...`` URL
    could answer with a 302 to ``http://127.0.0.1:6799/`` or to a cloud metadata
    address, and the gateway would fetch it and hand the bytes back as sprite data.
    The request would originate from inside the trust boundary — exactly the attack
    the validation was written to stop, arriving one line later.

    Returning None refuses the hop, which urllib surfaces as an HTTPError rather than
    silently returning the pre-redirect response.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        if _petdex_asset_url(newurl) is None:
            logger.warning("crew-companion: refusing PetDex redirect to %s", newurl)
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


#: An opener that validates redirects. Module-level so every fetch shares it.
_OPENER = urllib.request.build_opener(_PinnedRedirects())


def _get(url: str, *, as_json: bool) -> Any:
    """Fetch a URL with a hard byte ceiling.

    Read in chunks and abort past the cap rather than trusting Content-Length.
    """
    request = urllib.request.Request(  # noqa: S310 — scheme and host pinned above
        url, headers={"User-Agent": "KiroCrew-CrewCompanion"}  # brand-ok: wire identifier, not prose
    )
    # Through _OPENER, never the module-level urlopen: the default opener follows
    # redirects without re-validating them.
    with _OPENER.open(request, timeout=FETCH_TIMEOUT_SECS) as response:  # noqa: S310
        chunks, total = [], 0
        while True:
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                raise ValueError("download exceeded the size limit")
            chunks.append(chunk)
    payload = b"".join(chunks)
    if as_json:
        return json.loads(payload.decode("utf-8"))
    return payload


def fetch_petdex_pet(raw_input: Any) -> dict[str, Any]:
    """Look a pet up on PetDex and return its sprite sheet.

    Returns ``{"ok": False, "error": ...}`` rather than raising: every failure here is
    something the user should see in the import dialog, and none of them are bugs.
    """
    slug = parse_petdex_slug(raw_input)
    if slug is None:
        return {"ok": False, "error": "Enter a PetDex slug or link"}

    try:
        manifest = _get(PETDEX_MANIFEST_URL, as_json=True)
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("crew-companion: PetDex manifest fetch failed: %s", exc)
        return {"ok": False, "error": "Could not reach PetDex"}

    pets = manifest.get("pets") if isinstance(manifest, dict) else None
    if not isinstance(pets, list):
        return {"ok": False, "error": "PetDex returned an unexpected response"}

    def _slug_of(entry: Any) -> str:
        return str(entry.get("slug", "")).lower() if isinstance(entry, dict) else ""

    # Exact match first, then a prefix, so a partial paste still resolves.
    pet = next((p for p in pets if _slug_of(p) == slug), None)
    if pet is None:
        pet = next((p for p in pets if _slug_of(p).startswith(slug)), None)
    if pet is None:
        return {"ok": False, "error": f'No PetDex pet found for "{slug}"'}

    sheet_url = _petdex_asset_url(pet.get("spritesheetUrl"))
    if sheet_url is None:
        return {"ok": False, "error": "That pet has no usable sprite sheet"}

    try:
        sheet = _get(sheet_url, as_json=False)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.warning("crew-companion: PetDex sprite fetch failed: %s", exc)
        return {"ok": False, "error": "Could not download that pet's art"}

    display_name = str(pet.get("displayName") or slug)
    description = ""
    # pet.json is optional metadata; failing to read it must not fail the import.
    detail_url = _petdex_asset_url(pet.get("petJsonUrl"))
    if detail_url is not None:
        try:
            detail = _get(detail_url, as_json=True)
            if isinstance(detail, dict):
                display_name = str(detail.get("displayName") or display_name)
                description = str(detail.get("description") or "")
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
            logger.debug("crew-companion: PetDex pet.json unavailable")

    return {
        "ok": True,
        "slug": str(pet.get("slug") or slug),
        "displayName": display_name,
        "author": str(pet.get("submittedBy") or "PetDex"),
        "description": description,
        "spriteBase64": base64.b64encode(sheet).decode("ascii"),
    }


# ── export / import ─────────────────────────────────────────────────────────


def export_bundle(appearances: Any, pack_id: str) -> dict[str, Any] | None:
    """Build a portable bundle for one pack.

    A plain JSON envelope rather than an archive: the gallery already round-trips a
    manifest plus named file contents, and JSON keeps the format inspectable by the
    person moving it between machines.
    """
    detail = appearances.pack_detail(pack_id)
    if not detail:
        return None

    # The built-in's art ships inside the frontend bundle, not as files on disk, so
    # there is nothing here to export. Refusing beats handing over an empty bundle that
    # then fails to import with a confusing "no art" message.
    meta = detail.get("meta") or {}
    if meta.get("type") == "builtin" or meta.get("id") == DEFAULT_PACK:
        return None

    # `pack_detail` answers in the RENDERER's shape — art inlined per slot — while a
    # bundle has to be re-importable, which means the store's own shape: a manifest
    # naming files, plus those files. So it is rebuilt rather than passed through.
    #
    # Categorized via detail's `categories` (the authoritative taxonomy), NOT by
    # dumping every slot into `states`: that flattening exported moods and random
    # clips as states, so export→import→edit misclassified or deleted them — the
    # same category-loss bug the detail/editor/save paths each had in turn. A slot
    # the taxonomy does not name (a pre-taxonomy detail payload) defaults to
    # `states`, which is exactly the old behavior for old packs.
    animations = detail.get("animations") or {}
    raw_categories = detail.get("categories")
    slot_category: dict[str, str] = {}
    if isinstance(raw_categories, dict):
        for category in ("states", "moods", "random"):
            names = raw_categories.get(category)
            if isinstance(names, list):
                for name in names:
                    slot_category[str(name)] = category
    files: dict[str, str] = {}
    manifest_maps: dict[str, dict[str, str]] = {"states": {}, "moods": {}, "random": {}}
    for slot, entry in animations.items():
        safe_slot = _safe_filename(f"{slot}.svg")
        if safe_slot is None:
            continue
        if isinstance(entry, dict):
            content = entry.get("content")
            fmt = entry.get("format") or "svg"
        else:
            content, fmt = entry, "svg"
        if not isinstance(content, str) or not content:
            continue
        # Extension must match the slot's real format: pack_detail infers the
        # renderer from the filename, so a sprite (base64 PNG) exported as
        # `.svg` imports "successfully" and then renders blank. Mirror the
        # inference table in appearances.pack_detail exactly.
        if fmt == "lottie":
            ext = "json"
        elif fmt == "sprite":
            ext = "png"
        else:
            ext = "svg"
        filename = f"{slot}.{ext}"
        files[filename] = content
        manifest_maps[slot_category.get(slot, "states")][slot] = filename

    ident = str(meta.get("id") or pack_id)

    # Carry the ORIGINAL sheet too, when the pack kept one for re-editing.
    # Export built files from the animation slots only, so an exported pack
    # rendered fine but lost its sheet — export -> delete -> import destroyed
    # it permanently, closing off ever re-slicing the pack. Same class as the
    # detail-payload omission fixed alongside this.
    sprite = detail.get("sprite") or {}
    source_name = sprite.get("source") if isinstance(sprite, dict) else None
    source_image = detail.get("sourceImage")
    if (
        isinstance(source_name, str)
        and source_name
        and isinstance(source_image, str)
        and source_image
        and _safe_filename(source_name) == source_name
    ):
        files[source_name] = source_image

    return {
        "kind": "crew-companion-pack",
        "version": 1,
        "id": ident,
        "manifest": {
            "meta": meta,
            "states": manifest_maps["states"],
            "moods": manifest_maps["moods"],
            "random": manifest_maps["random"],
            "sprite": detail.get("sprite") or {},
        },
        "files": files,
    }


def import_bundle(appearances: Any, payload: Any) -> dict[str, Any]:
    """Install a pack from an exported bundle.

    The bundle names its own id, which makes it the least trustworthy field in the
    request — it is re-validated through the store's own guard rather than used as a
    path. A colliding id is refused instead of overwriting, so importing can never
    silently destroy a pack the user already had.
    """
    if not isinstance(payload, dict):
        return {"ok": False, "error": "That file is not a pack bundle"}

    encoded = json.dumps(payload)
    if len(encoded.encode("utf-8")) > MAX_BUNDLE_BYTES:
        return {"ok": False, "error": "That bundle is too large"}
    if payload.get("kind") != "crew-companion-pack":
        return {"ok": False, "error": "That file is not a pack bundle"}

    ident = _safe_id(payload.get("id"))
    if ident is None:
        return {"ok": False, "error": "That bundle has an invalid pack id"}

    manifest = payload.get("manifest")
    files = payload.get("files")
    if not isinstance(manifest, dict) or not isinstance(files, dict):
        return {"ok": False, "error": "That bundle is missing its manifest or art"}

    clean: dict[str, str] = {}
    for name, content in files.items():
        safe = _safe_filename(name)
        if safe is None or not isinstance(content, str):
            return {"ok": False, "error": "That bundle contains an unsupported file"}
        if not safe.lower().endswith(ALLOWED_SUFFIXES):
            return {"ok": False, "error": f"Unsupported file in bundle: {safe}"}
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            return {"ok": False, "error": f"File too large in bundle: {safe}"}
        clean[safe] = content

    if not clean:
        return {"ok": False, "error": "That bundle has no art in it"}

    # Refuse rather than clobber: the user may not realise the id collides.
    # `pack_exists`, not the listing: list_packs skips a pack whose manifest is
    # corrupt, so a listing-based check let an import silently REPLACE an
    # unreadable pack — destroying art that was still recoverable on disk.
    if appearances.pack_exists(ident):
        return {"ok": False, "error": f'A pack called "{ident}" is already installed'}

    # NORMALIZE the manifest's inner identity to the validated outer id. The
    # bundle names its id twice — the outer `id` (validated, collision-checked
    # above) and `manifest.meta.id` (until now saved verbatim). A bundle whose
    # inner id named an INSTALLED pack saved under the outer id but displayed as
    # the victim, and deleting the displayed entry deleted the victim's files.
    # Overwriting (not rejecting) keeps old exports importable: bundles written
    # before this fix may carry a stale inner id with no malicious intent.
    meta = manifest.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    manifest = {**manifest, "meta": {**meta, "id": ident}}

    if not appearances.save_pack(ident, manifest, clean):
        return {"ok": False, "error": "Could not save that pack"}
    return {"ok": True, "id": ident}


def save_sprite_pack(
    appearances: Any,
    pack_id: Any,
    manifest: Any,
    sprite_base64: Any,
    filename: Any = "sprites.png",
) -> dict[str, Any]:
    """Save a pack whose art is a single sprite sheet.

    Used by the sprite importer and by PetDex. The base64 is decoded here purely to
    verify it — storage keeps the encoded form, matching what the renderer reads —
    because accepting art that cannot be decoded would fail later, at paint time,
    where the cause is far less obvious.
    """
    ident = _safe_id(pack_id)
    if ident is None:
        return {"ok": False, "error": "Invalid pack id"}
    safe_name = _safe_filename(filename)
    if safe_name is None or not safe_name.lower().endswith(ALLOWED_SUFFIXES):
        return {"ok": False, "error": "Unsupported sprite filename"}
    if not isinstance(sprite_base64, str) or not sprite_base64:
        return {"ok": False, "error": "Missing sprite art"}
    if not isinstance(manifest, dict):
        return {"ok": False, "error": "Missing manifest"}

    try:
        raw = base64.b64decode(sprite_base64, validate=True)
    except (binascii.Error, ValueError):
        return {"ok": False, "error": "That sprite art could not be decoded"}
    if not raw:
        return {"ok": False, "error": "That sprite art is empty"}
    if len(raw) > MAX_FILE_BYTES:
        return {"ok": False, "error": "That sprite art is too large"}

    if not appearances.save_pack(ident, manifest, {safe_name: sprite_base64}):
        return {"ok": False, "error": "Could not save that pack"}
    return {"ok": True, "id": ident}
