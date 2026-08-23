"""WeCom inbound media: encrypted CDN download.

An ``aibot_msg_callback`` never carries bytes. An ``image`` / ``file`` / ``video``
item carries a short-lived ``url`` plus its OWN ``aeskey``, and the object at that
URL is encrypted. This module owns exactly that protocol-shaped work — download,
key decode, decrypt — so the transport deals in plaintext bytes and the shared
ingest pipeline (``messaging/attachments.py``) keeps classification, limits and
temp-file ownership channel-neutral.

**The cipher is dictated by the remote protocol, not chosen here.** WeCom's long
connection encrypts each media object with **AES-256-CBC**, PKCS#7-padded to a
32-byte multiple, using the first 16 bytes of the object's own key as the IV.

That is deliberately NOT the same scheme as Weixin's (`weixin/media.py`, AES-128-
**ECB** with a shared key), and the two must not be merged: the mode differs, the
key length differs, and WeCom's key is **per object** rather than per app. A
per-object key is the safer shape — one leaked key exposes one object — but it
also means there is no long-lived secret to cache, so the key travels with the
item and is used once.

The URL is valid for about five minutes, which is why a download failure is
reported rather than retried indefinitely: by the time a long retry would
succeed, the object is gone.
"""

from __future__ import annotations

import base64
import binascii
import logging
from typing import Any

import aiohttp
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = logging.getLogger(__name__)

#: AES-256 needs a 32-byte key; the IV is the first 16 bytes of that key.
_KEY_BYTES = 32
_IV_BYTES = 16

#: WeCom pads media to a multiple of 32 bytes, not the 16-byte AES block size.
#: Unpadding with the wrong block size silently keeps or eats real bytes, so this
#: is stated rather than assumed from the cipher.
_PAD_BITS = 32 * 8

#: WeCom's largest per-item limit, on the FILE the user attached — i.e. on the
#: plaintext. Exported so the ingest limits and this download cap cannot drift.
WECOM_MAX_PLAINTEXT_BYTES = 20 * 1024 * 1024

#: Hard ceiling on a single download, enforced on BYTES READ rather than on
#: ``Content-Length`` — a header is attacker-influenced and a lying one would let
#: an unbounded body through.
#:
#: It is the plaintext ceiling PLUS the padding, because what is read here is
#: CIPHERTEXT. PKCS#7 to a 32-byte multiple always adds between 1 and 32 bytes
#: (a length that is already a multiple gets a full extra block, which is what
#: makes the unpadding unambiguous), so a file at exactly the platform's maximum
#: arrives as 20 MiB + up to 32 bytes. Capping at the plaintext figure rejected
#: it before decryption — a local-only refusal of an attachment WeCom itself
#: carried, and the largest files are exactly where it bit.
MAX_MEDIA_BYTES = WECOM_MAX_PLAINTEXT_BYTES + _PAD_BITS // 8

_DOWNLOAD_TIMEOUT_SECS = 60


class WeComMediaError(Exception):
    """A media object could not be fetched or decrypted."""


def decode_aes_key(raw: str) -> bytes:
    """Decode an item's ``aeskey`` into the raw 32-byte AES key.

    Accepts the two encodings WeCom uses for the same value — base64 of the raw
    key bytes, and base64 of the key's ASCII hex — discriminated by decoded
    length plus a strict hex check. Guessing wrong yields plausible garbage
    instead of an error, which is why the check is strict rather than a fallback:
    a wrong key decrypts to noise and the failure would surface far from here.
    """
    if not raw:
        raise WeComMediaError("media item carries no aeskey")
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise WeComMediaError("aeskey is not valid base64") from exc
    if len(decoded) == _KEY_BYTES:
        return decoded
    if len(decoded) == _KEY_BYTES * 2:
        try:
            return bytes.fromhex(decoded.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise WeComMediaError("aeskey is not raw bytes nor ascii hex") from exc
    raise WeComMediaError(f"aeskey decodes to {len(decoded)} bytes, expected 32 or 64")


def decrypt_media(ciphertext: bytes, key: bytes) -> bytes:
    """AES-256-CBC decrypt with 32-byte PKCS#7 unpadding.

    The IV is the key's first 16 bytes, per the protocol — it is not random and
    not transmitted separately.
    """
    if len(key) != _KEY_BYTES:
        raise WeComMediaError(f"AES-256 needs a {_KEY_BYTES}-byte key, got {len(key)}")
    if not ciphertext or len(ciphertext) % _IV_BYTES:
        raise WeComMediaError("ciphertext is empty or not a whole number of AES blocks")
    # CBC is not our choice. WeCom hands us objects it has ALREADY encrypted this
    # way and we never encrypt with it, so there is no AEAD mode to switch to: the
    # alternative to decrypting CBC here is not decrypting the user's screenshot at
    # all. Integrity is not claimed for the ciphertext; what protects the object is
    # that the URL is single-use and lives ~5 minutes, and that the key is per
    # object rather than shared.
    # nosemgrep: python.cryptography.security.mode-without-authentication.crypto-mode-without-authentication  # noqa: E501
    decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:_IV_BYTES])).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    try:
        unpadder = padding.PKCS7(_PAD_BITS).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError as exc:
        # A padding error here almost always means the KEY was wrong, not that the
        # padding is exotic: say so, because "invalid padding" sends the reader
        # looking at the wrong end of the problem.
        raise WeComMediaError("decrypt failed (wrong aeskey, or a truncated object)") from exc


async def download_media(
    session: aiohttp.ClientSession,
    url: str,
    aeskey: str,
    *,
    proxy: str | None = None,
    max_bytes: int = MAX_MEDIA_BYTES,
) -> bytes:
    """Fetch and decrypt one CDN media object.

    The size cap is enforced while READING, so an oversize object is abandoned
    mid-stream rather than fully buffered and then rejected — the point of a cap
    is to bound the memory, and checking after the read does not.
    """
    if not url:
        raise WeComMediaError("media item carries no url")
    key = decode_aes_key(aeskey)
    chunks: list[bytes] = []
    total = 0
    try:
        async with session.get(
            url, proxy=proxy, timeout=aiohttp.ClientTimeout(total=_DOWNLOAD_TIMEOUT_SECS)
        ) as resp:
            if resp.status != 200:
                raise WeComMediaError(f"media download returned HTTP {resp.status}")
            async for chunk in resp.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise WeComMediaError(f"media object exceeds {max_bytes} bytes")
                chunks.append(chunk)
    except aiohttp.ClientError as exc:
        # The URL lives ~5 minutes; a transport failure is reported, never retried
        # into a window that has already closed.
        raise WeComMediaError(f"media download failed: {type(exc).__name__}") from exc
    return decrypt_media(b"".join(chunks), key)


def media_items(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the media sub-objects from an inbound callback body.

    ``mixed`` is image+text in one message, and its parts live under
    ``mixed.msg_item``; every other media type is a single object named by
    ``msgtype``. Both shapes are flattened to a list of ``{kind, url, aeskey,
    ...}`` records so the caller has one thing to iterate.

    Only shapes that could carry a downloadable object are returned. ``voice`` is
    excluded on purpose: WeCom hands back its OWN transcript in ``voice.content``,
    so the useful payload is text and there is nothing to fetch.
    """
    out: list[dict[str, Any]] = []
    msgtype = body.get("msgtype", "")
    if msgtype == "mixed":
        mixed = body.get("mixed", {})
        items = mixed.get("msg_item", []) if isinstance(mixed, dict) else []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            kind = item.get("msgtype", "")
            sub = item.get(kind)
            if kind in ("image", "file", "video") and isinstance(sub, dict):
                out.append({"kind": kind, **sub})
        return out
    if msgtype in ("image", "file", "video"):
        sub = body.get(msgtype)
        if isinstance(sub, dict):
            out.append({"kind": msgtype, **sub})
    return out


def mixed_text(body: dict[str, Any]) -> str:
    """The text parts of a ``mixed`` (image+text) message, joined.

    Without this a captioned screenshot arrives with an empty ``text.content``:
    the caption lives in the mixed item list, and reading only ``text`` loses it.
    """
    if body.get("msgtype") != "mixed":
        return ""
    mixed = body.get("mixed", {})
    items = mixed.get("msg_item", []) if isinstance(mixed, dict) else []
    parts: list[str] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("msgtype") != "text":
            continue
        sub = item.get("text")
        if isinstance(sub, dict):
            content = sub.get("content", "")
            if isinstance(content, str) and content:
                parts.append(content)
    return "\n".join(parts)
