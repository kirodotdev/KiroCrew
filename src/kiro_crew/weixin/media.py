"""iLink CDN media download for the Weixin channel.

Inbound media on iLink is never carried in the message envelope: an item holds a
:class:`CDNMedia` reference (``encrypt_query_param`` + ``aes_key``) and the bytes
live on the WeChat CDN, AES-128-ECB encrypted. This module owns exactly that
protocol-shaped work -- URL construction, key decoding, download, decrypt -- and
nothing about attachment policy, which stays in
:mod:`kiro_crew.messaging.attachments`.

AES-128-ECB is dictated by the remote protocol, not chosen by us: the CDN
encrypts every object with a per-file random 16-byte key in ECB mode with PKCS7
padding, and refuses anything else. ECB is a weak mode in general (identical
plaintext blocks yield identical ciphertext blocks), so it is confined to this
module and used only to read bytes WeChat already encrypted -- never to protect
data of our own.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from typing import Any
from urllib.parse import quote

import aiohttp
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = logging.getLogger(__name__)

#: Default CDN host for c2c media. Overridable so a probe/test can point at a
#: local stub without monkeypatching the download function.
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"

#: Hard ceiling on a single CDN object, enforced while streaming so a lying
#: ``Content-Length`` (or none at all) cannot exhaust memory. The shared ingest
#: layer also caps per-class sizes, but it only sees the file AFTER download --
#: this is the bound that makes the download itself safe.
MAX_CDN_BYTES = 32 * 1024 * 1024

#: Read chunk for the streaming download.
_CHUNK_BYTES = 64 * 1024

CDN_TIMEOUT_SECONDS = 60

_HEX32_RE = re.compile(rb"^[0-9a-fA-F]{32}$")


class WeixinMediaError(RuntimeError):
    """A CDN object could not be fetched or decrypted."""


def parse_aes_key(aes_key_b64: str) -> bytes:
    """Decode ``CDNMedia.aes_key`` into the raw 16-byte AES key.

    iLink uses TWO encodings for the same field and does not label which is in
    play, so both are probed by shape:

    * ``base64(raw 16 bytes)`` -- images
    * ``base64(ascii hex of 16 bytes)`` -- file / voice / video

    The second decodes to 32 ASCII hex characters that must be hex-parsed again
    to recover the key. Guessing wrong yields garbage plaintext rather than an
    error, which is why the discrimination is on decoded LENGTH plus a strict
    hex check, not on the item type: a mislabelled item would otherwise hand a
    32-byte "key" to AES-128 and fail far from the cause.
    """
    if not aes_key_b64:
        raise WeixinMediaError("aes_key is empty")
    try:
        decoded = base64.b64decode(aes_key_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise WeixinMediaError(f"aes_key is not valid base64: {exc}") from exc
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32 and _HEX32_RE.match(decoded):
        return bytes.fromhex(decoded.decode("ascii"))
    raise WeixinMediaError(
        f"aes_key must decode to 16 raw bytes or 32 hex chars, got {len(decoded)} bytes"
    )


def decrypt_aes_ecb(ciphertext: bytes, key: bytes) -> bytes:
    """AES-128-ECB decrypt with PKCS7 unpadding.

    A ciphertext whose length is not a multiple of the block size, or whose
    final block carries invalid padding, raises :class:`WeixinMediaError` --
    both mean the key encoding was guessed wrong or the object is truncated, and
    silently returning partial bytes would hand a corrupt "image" to the model.
    """
    if len(key) != 16:
        raise WeixinMediaError(f"AES-128 needs a 16-byte key, got {len(key)}")
    if not ciphertext or len(ciphertext) % 16:
        raise WeixinMediaError(f"ciphertext length {len(ciphertext)} is not a multiple of 16")
    # ECB is dictated by the remote protocol (see module docstring): the CDN
    # hands us objects it already encrypted this way, and we never encrypt with
    # it. There is no algorithm choice to make here.
    decryptor = Cipher(  # nosec B305  # lgtm[py/weak-cryptographic-algorithm]
        algorithms.AES(key), modes.ECB()
    ).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    try:
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError as exc:
        raise WeixinMediaError(f"PKCS7 unpadding failed: {exc}") from exc


def build_download_url(encrypt_query_param: str, cdn_base_url: str = CDN_BASE_URL) -> str:
    """CDN download URL for *encrypt_query_param*.

    The parameter is percent-encoded: it is server-supplied opaque ciphertext
    that routinely contains ``+`` and ``/`` from base64, which would otherwise
    be re-read as a space and a path separator.
    """
    return f"{cdn_base_url}/download?encrypted_query_param={quote(encrypt_query_param, safe='')}"


async def fetch_cdn_bytes(
    session: aiohttp.ClientSession,
    url: str,
    *,
    max_bytes: int = MAX_CDN_BYTES,
) -> bytes:
    """Stream a CDN object into memory, refusing anything over *max_bytes*.

    Enforced on bytes actually read rather than on ``Content-Length`` so a
    missing or understated header cannot bypass the cap.
    """
    timeout = aiohttp.ClientTimeout(total=CDN_TIMEOUT_SECONDS)
    async with session.get(url, timeout=timeout) as resp:
        if resp.status != 200:
            body = (await resp.text())[:200]
            raise WeixinMediaError(f"CDN download HTTP {resp.status}: {body}")
        buf = bytearray()
        async for chunk in resp.content.iter_chunked(_CHUNK_BYTES):
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise WeixinMediaError(f"CDN object exceeds {max_bytes} bytes")
        return bytes(buf)


async def download_media(
    session: aiohttp.ClientSession,
    *,
    encrypt_query_param: str,
    aes_key_b64: str,
    cdn_base_url: str = CDN_BASE_URL,
    max_bytes: int = MAX_CDN_BYTES,
) -> bytes:
    """Download and decrypt one CDN object; returns plaintext bytes.

    An empty ``aes_key_b64`` means the object is stored unencrypted (observed for
    some images), so the bytes are returned as fetched instead of being forced
    through a decrypt that would fail.
    """
    if not encrypt_query_param:
        raise WeixinMediaError("encrypt_query_param is empty")
    url = build_download_url(encrypt_query_param, cdn_base_url)
    raw = await fetch_cdn_bytes(session, url, max_bytes=max_bytes)
    if not aes_key_b64:
        return raw
    return decrypt_aes_ecb(raw, parse_aes_key(aes_key_b64))


def media_ref(item: dict[str, Any], kind_field: str) -> tuple[str, str]:
    """Extract ``(encrypt_query_param, aes_key_b64)`` from one message item.

    ``image_item`` carries a second, PREFERRED key location: a raw hex
    ``aeskey`` field sitting next to ``media``. Upstream prefers it over
    ``media.aes_key`` for inbound decryption, so this does too -- it is
    re-encoded to base64 here so callers see one uniform encoding and
    :func:`parse_aes_key` stays the single decoder.

    Missing pieces come back as empty strings rather than raising: the caller
    turns an absent download reference into a user-visible rejection, which is
    strictly more useful than an exception that loses the rest of the message.
    """
    sub = item.get(kind_field)
    if not isinstance(sub, dict):
        return "", ""
    media = sub.get("media")
    media = media if isinstance(media, dict) else {}
    param = media.get("encrypt_query_param")
    param = param if isinstance(param, str) else ""

    hex_key = sub.get("aeskey")
    if isinstance(hex_key, str) and hex_key:
        try:
            return param, base64.b64encode(bytes.fromhex(hex_key)).decode("ascii")
        except ValueError:
            logger.warning("weixin: %s.aeskey is not valid hex, falling back", kind_field)

    aes_key = media.get("aes_key")
    return param, aes_key if isinstance(aes_key, str) else ""
