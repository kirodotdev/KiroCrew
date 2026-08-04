"""Petdex.dev pet import — obtain a pet's spritesheet + metadata.

A petdex pet is two files: a spritesheet and a ``pet.json`` that carries only
``id`` / ``displayName`` / ``description`` / ``spritesheetPath``. It contains
**no state mapping** — the animation rows are a positional convention (8 cols x
9 rows of 192x208 frames, verified against the published assets). This module
therefore only OBTAINS the two files; deciding which row drives which Mochi
state is a user decision made in the Avatars importer, because a convention
that drifts upstream would otherwise mis-map every pet silently.

Two sources, both user-initiated:

* :func:`fetch_pet` — resolve a slug through ``petdex.dev/install/<slug>`` and
  download the two assets. This is an outbound request to a third party, so it
  is fenced: HTTPS only, host allow-list, byte caps, and a total timeout. The
  asset URLs come OUT of remote content, so they are re-validated against the
  same allow-list before being fetched — that is the injection surface here.
* :func:`list_installed` / :func:`read_installed` — read pets the user already
  installed with the petdex CLI (``~/.codex/pets/<slug>/``). No network.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)

#: Where the petdex CLI installs pets. Fixed by that CLI, not configurable.
INSTALLED_PETS_DIR = Path("~/.codex/pets")

#: Slugs address a path segment on a third-party host; keep them boring.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

#: Every host this module may talk to. The install script is served by the site
#: and the assets by its CDN; nothing else is fetchable, including hosts that
#: merely END with these names (matching is exact).
_ALLOWED_HOSTS = frozenset({"petdex.dev", "assets.petdex.dev"})

_INSTALL_URL = "https://petdex.dev/install/{slug}"

#: petdex serves the install script with a Referer check on the asset URLs.
_HEADERS = {"Referer": "https://petdex.dev/", "User-Agent": "KiroCrew-Mochi"}

#: Byte caps. The script is a few hundred bytes and pet.json ~200; the sheets
#: observed are 0.4-2.5 MB. These are generous ceilings, not expectations.
_MAX_SCRIPT_BYTES = 64 * 1024
_MAX_JSON_BYTES = 64 * 1024
_MAX_SHEET_BYTES = 16 * 1024 * 1024

#: Total seconds for one import, connect through last byte.
_TIMEOUT_SECS = 30

#: Accepted sheet formats, by magic bytes. The renderer decodes the sheet in an
#: <img>, so the browser is the real parser -- this only keeps non-images out.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"\xff\xd8\xff", "image/jpeg"),
)

_PET_JSON_NAME = "pet.json"
#: Sheet names the CLI writes, in preference order.
_SHEET_NAMES = ("spritesheet.webp", "spritesheet.png", "sprite.webp", "sprite.png")


class PetdexError(Exception):
    """A petdex import could not be completed (bad slug, network, or shape)."""


# ── slug / url handling ────────────────────────────────────────────────────


def normalize_slug(raw: str) -> str:
    """Extract a bare slug from user input.

    Accepts a slug (``boba``), a gallery path (``petdex.dev/pets/boba``), or a
    full URL with an optional locale segment (``https://petdex.dev/en/pets/boba``)
    so pasting the address bar works.
    """
    text = (raw or "").strip()
    if not text:
        raise PetdexError("no pet given")
    if "/" in text:
        # Anything with a separator must be a petdex PET URL. Taking the last
        # path segment of arbitrary input would "sanitize" `../etc/passwd` into
        # the plausible slug `passwd` and go on to fetch it -- silently acting on
        # input the user did not mean is worse than refusing it.
        match = re.search(r"/pets/([^/?#]+)", text)
        if match is None:
            raise PetdexError(f"not a petdex pet link: {raw!r}")
        text = match.group(1)
    text = text.lower()
    if not _SLUG_RE.match(text):
        raise PetdexError(f"not a valid petdex pet name: {raw!r}")
    return text


def _assert_allowed_url(url: str) -> None:
    """Reject anything that is not HTTPS on an allow-listed host.

    Applied to the asset URLs parsed out of the install script as well as to
    the script URL itself: the script is remote content, so a compromised or
    changed host could otherwise point the download anywhere.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise PetdexError(f"refusing non-HTTPS petdex URL: {url}")
    if (parsed.hostname or "").lower() not in _ALLOWED_HOSTS:
        raise PetdexError(f"refusing petdex URL on unexpected host: {url}")


def _sniff_mime(blob: bytes) -> str:
    for magic, mime in _MAGIC:
        if blob.startswith(magic):
            return mime
    # WebP is RIFF<size>WEBP, so the tag is not a plain prefix.
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    raise PetdexError("the spritesheet is not a recognized image")


def parse_pet_json(text: str) -> dict[str, str]:
    """Validate a ``pet.json`` and return only the fields we consume."""

    try:
        raw = json.loads(text)
    except Exception as exc:  # noqa: BLE001 — any parse failure is the same bad input
        raise PetdexError(f"pet.json is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise PetdexError("pet.json must be a JSON object")
    out: dict[str, str] = {}
    for key in ("id", "displayName", "description", "spritesheetPath"):
        value = raw.get(key)
        if isinstance(value, str):
            out[key] = value[:500]
    if not out.get("id") and not out.get("displayName"):
        raise PetdexError("pet.json has neither an id nor a displayName")
    return out


# ── network source ─────────────────────────────────────────────────────────


async def _read_capped(response: Any, cap: int, what: str) -> bytes:
    """Read a response body, refusing to buffer more than *cap* bytes.

    Streamed rather than ``response.read()`` so an over-large (or unbounded)
    body is abandoned early instead of after it is all in memory.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > cap:
            raise PetdexError(f"{what} is larger than {cap // 1024} KiB")
        chunks.append(chunk)
    return b"".join(chunks)


def _extract_asset_urls(script: str) -> tuple[str, str]:
    """Pull the pet.json and spritesheet URLs out of the install script.

    The script is a plain ``sh`` file with one ``curl -o <dest> '<url>'`` per
    asset; it is matched rather than executed.
    """
    # The quote characters are deliberately NOT in this class: the script wraps
    # each URL in single quotes, and including them swallowed the closing quote
    # into the match, so every extension test then failed.
    urls = re.findall(r"https://[A-Za-z0-9._~:/?#@!$&*+,;=%-]+", script)

    def _ext(url: str) -> str:
        return url.split("?")[0].split("#")[0].rsplit(".", 1)[-1].lower()

    json_url = next((u for u in urls if _ext(u) == "json"), None)
    sheet_url = next((u for u in urls if _ext(u) in ("webp", "png", "gif", "jpg", "jpeg")), None)
    if not json_url or not sheet_url:
        raise PetdexError("could not find this pet's files (petdex may have changed format)")
    return json_url, sheet_url


async def fetch_pet(slug: str) -> dict[str, Any]:
    """Download a petdex pet by slug. Outbound HTTPS to the allow-listed hosts.

    Returns ``{slug, meta, imageBase64, imageMime, source: 'petdex.dev'}``.
    """

    slug = normalize_slug(slug)
    install_url = _INSTALL_URL.format(slug=slug)
    _assert_allowed_url(install_url)

    timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECS)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=_HEADERS) as session:
            # allow_redirects=False on every request: the URL is validated
            # against the host allow-list BEFORE the request, so following a
            # redirect would let a 30x from the (third-party) server route the
            # follow-up anywhere — including link-local/metadata addresses —
            # with the validation already behind us. A redirect is treated as
            # an error, not followed: petdex serves these URLs directly today,
            # and if that changes the new location must re-enter through the
            # same allow-list, not bypass it.
            async with session.get(install_url, allow_redirects=False) as resp:
                if resp.status == 404:
                    raise PetdexError(f"no pet named {slug!r} on petdex.dev")
                if resp.status != 200:
                    raise PetdexError(f"petdex.dev returned HTTP {resp.status} for {slug!r}")
                script = (await _read_capped(resp, _MAX_SCRIPT_BYTES, "install script")).decode(
                    "utf-8", "replace"
                )

            json_url, sheet_url = _extract_asset_urls(script)
            # Re-validate: these came out of remote content.
            _assert_allowed_url(json_url)
            _assert_allowed_url(sheet_url)

            async with session.get(json_url, allow_redirects=False) as resp:
                if resp.status != 200:
                    raise PetdexError(f"pet.json download failed (HTTP {resp.status})")
                meta_bytes = await _read_capped(resp, _MAX_JSON_BYTES, "pet.json")

            async with session.get(sheet_url, allow_redirects=False) as resp:
                if resp.status != 200:
                    raise PetdexError(f"spritesheet download failed (HTTP {resp.status})")
                sheet = await _read_capped(resp, _MAX_SHEET_BYTES, "spritesheet")
    except PetdexError:
        raise
    except Exception as exc:  # noqa: BLE001 — surface one message to the UI
        raise PetdexError(f"could not reach petdex.dev: {exc}") from exc

    meta = parse_pet_json(meta_bytes.decode("utf-8", "replace"))
    logger.info("mochi: imported petdex pet %s (%d KiB sheet)", slug, len(sheet) // 1024)
    return {
        "slug": slug,
        "meta": meta,
        "imageMime": _sniff_mime(sheet),
        "imageBase64": base64.b64encode(sheet).decode("ascii"),
        "source": "petdex.dev",
    }


# ── locally installed source (no network) ──────────────────────────────────


def _pets_root() -> Path:
    return Path(os.path.expanduser(str(INSTALLED_PETS_DIR)))


def _resolve_pet_dir(slug: str) -> Path:
    """Resolve an installed pet's directory, refusing to escape the pets root.

    ``normalize_slug`` already bans separators, but the containment check is
    kept because the root itself may be reached through a symlink.
    """
    root = _pets_root().resolve()
    candidate = (root / normalize_slug(slug)).resolve()
    if candidate != root and root not in candidate.parents:
        raise PetdexError("that pet is outside the petdex pets folder")
    return candidate


def _find_sheet(pet_dir: Path, meta: dict[str, str]) -> Path | None:
    """Locate a pet's spritesheet, preferring the name pet.json declares."""
    declared = meta.get("spritesheetPath") or ""
    names: list[str] = []
    # Only the bare filename: a declared path is data from a downloaded file.
    if declared and "/" not in declared and "\\" not in declared:
        names.append(declared)
    names.extend(n for n in _SHEET_NAMES if n not in names)
    for name in names:
        candidate = pet_dir / name
        if candidate.is_file():
            return candidate
    return None


def list_installed() -> list[dict[str, Any]]:
    """List pets the petdex CLI has installed. Never raises: an unreadable or
    absent folder is an empty list, because this drives a picker."""
    root = _pets_root()
    if not root.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        pet_json = entry / _PET_JSON_NAME
        if not pet_json.is_file():
            continue
        try:
            from kiro_crew.hooks import safe_read_file

            # Route through the sensitive-path gate: a pet.json symlinked into a
            # protected location (~/.aws, ~/.ssh, the crew trust-root, …) is
            # refused here and the pet is skipped, never surfaced in the picker.
            meta = parse_pet_json(safe_read_file(str(pet_json)))
            if _find_sheet(entry, meta) is None:
                continue
            found.append(
                {
                    "slug": entry.name,
                    "name": meta.get("displayName") or meta.get("id") or entry.name,
                    "description": meta.get("description", ""),
                }
            )
        except Exception:  # noqa: BLE001 — one bad pet must not hide the rest
            logger.debug("mochi: skipping unreadable petdex pet at %s", entry)
    return found


def read_installed(slug: str) -> dict[str, Any]:
    """Read an installed pet from disk. Same shape as :func:`fetch_pet`."""
    from kiro_crew.hooks import (
        FileTooLargeError,
        safe_read_file,
        safe_read_file_bytes,
    )

    pet_dir = _resolve_pet_dir(slug)
    pet_json = pet_dir / _PET_JSON_NAME
    if not pet_json.is_file():
        raise PetdexError(f"no installed pet named {slug!r}")
    # Route both reads through the sensitive-path gate. An installed pet.json or
    # spritesheet symlinked into a protected location (~/.aws, ~/.ssh, the crew
    # trust-root, …) is refused here, so a crafted pet cannot exfiltrate a
    # credential file's bytes back to the caller as base64 "image" data.
    try:
        raw_meta = safe_read_file(str(pet_json))
    except PermissionError as err:
        raise PetdexError(f"cannot read pet {slug!r}: {err}")
    meta = parse_pet_json(raw_meta)
    sheet_path = _find_sheet(pet_dir, meta)
    if sheet_path is None:
        raise PetdexError(f"pet {slug!r} has no spritesheet")
    # Check the on-disk size BEFORE reading: an oversized installed spritesheet
    # would otherwise be allocated whole into memory and could OOM the gateway.
    try:
        if sheet_path.stat().st_size > _MAX_SHEET_BYTES:
            raise PetdexError("that pet's spritesheet is too large")
    except OSError as err:
        raise PetdexError(f"cannot read spritesheet for {slug!r}: {err}")
    try:
        sheet = safe_read_file_bytes(str(sheet_path))
    except FileTooLargeError:
        raise PetdexError("that pet's spritesheet is too large")
    if sheet is None:  # rejected by the sensitive-path gate, or unreadable
        raise PetdexError(f"cannot read spritesheet for {slug!r}")
    if len(sheet) > _MAX_SHEET_BYTES:  # belt-and-suspenders against a TOCTOU grow
        raise PetdexError("that pet's spritesheet is too large")
    return {
        "slug": normalize_slug(slug),
        "meta": meta,
        "imageMime": _sniff_mime(sheet),
        "imageBase64": base64.b64encode(sheet).decode("ascii"),
        "source": str(INSTALLED_PETS_DIR),
    }
