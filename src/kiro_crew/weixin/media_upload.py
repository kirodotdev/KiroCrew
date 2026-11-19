"""iLink CDN media upload for the Weixin channel: the outbound half of ``media.py``.

A picture on iLink is never carried in ``sendmessage``. The bytes are encrypted
under a fresh per-file key, ``getuploadurl`` hands back an upload ticket, the
ciphertext is POSTed to the WeChat CDN, and the CDN answers with the
``encrypt_query_param`` the image item then references. This module owns that
protocol work and nothing about which files may go out, which stays in
:mod:`kiro_crew.messaging.outbound_files`.

AES-128-ECB with PKCS7 is dictated by the remote protocol, exactly as on the
download side: the CDN stores every object that way and the recipient's client
decrypts it with the key the image item carries. The key is random per file and
travels with the message, so the cipher is a transport encoding WeChat requires,
not protection of data of our own.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from kiro_crew.weixin.client import ITEM_IMAGE
from kiro_crew.weixin.media import CDN_BASE_URL, WeixinMediaError

if TYPE_CHECKING:
    from kiro_crew.weixin.client import WeixinClient

#: ``getuploadurl``'s ``media_type`` for a picture.
UPLOAD_MEDIA_TYPE_IMAGE = 1

#: ``CDNMedia.encrypt_type`` for an AES-128-ECB object.
ENCRYPT_TYPE_AES_ECB = 1

#: Largest picture one reply uploads. WeChat compresses pictures for display, so
#: anything bigger gains the reader nothing and costs a long upload.
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def encrypt_aes_ecb(plaintext: bytes, key: bytes) -> bytes:
    """AES-128-ECB encrypt with PKCS7 padding, the CDN's only accepted form."""
    if len(key) != 16:
        raise WeixinMediaError(f"AES-128 needs a 16-byte key, got {len(key)}")
    # Lazy for the same reason as media.decrypt_aes_ecb: only a media send needs
    # the `cryptography` native wheel.
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(  # nosec B305  # lgtm[py/weak-cryptographic-algorithm]
        algorithms.AES(key), modes.ECB()
    ).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def build_upload_url(upload_param: str, filekey: str, cdn_base_url: str = CDN_BASE_URL) -> str:
    """CDN upload URL; both values are percent-encoded opaque server strings."""
    return (
        f"{cdn_base_url}/upload?encrypted_query_param={quote(upload_param, safe='')}"
        f"&filekey={quote(filekey, safe='')}"
    )


async def upload_image(
    client: "WeixinClient",
    *,
    to_user_id: str,
    data: bytes,
    cdn_base_url: str = CDN_BASE_URL,
) -> dict[str, Any]:
    """Encrypt and upload one picture; return the ``item_list`` entry that shows it.

    Raises on any failed step, so a caller can name the picture that did not go
    out instead of reporting it delivered.
    """
    if not data:
        raise WeixinMediaError("refusing to upload an empty picture")
    if len(data) > MAX_IMAGE_BYTES:
        raise WeixinMediaError(f"picture exceeds {MAX_IMAGE_BYTES} bytes")
    key = secrets.token_bytes(16)
    filekey = secrets.token_hex(16)
    # Off the loop: encrypting a multi-megabyte body is CPU-bound, and the first
    # call also pays the lazy `cryptography` import.
    ciphertext = await asyncio.to_thread(encrypt_aes_ecb, data, key)
    ticket = await client.get_upload_url(
        to_user_id=to_user_id,
        filekey=filekey,
        media_type=UPLOAD_MEDIA_TYPE_IMAGE,
        rawsize=len(data),
        # An integrity checksum the protocol asks for, not a security hash.
        rawfilemd5=hashlib.md5(data, usedforsecurity=False).hexdigest(),
        filesize=len(ciphertext),
        aeskey=key.hex(),
    )
    upload_param = ticket.get("upload_param")
    if not isinstance(upload_param, str) or not upload_param:
        raise WeixinMediaError("getuploadurl returned no upload_param")
    download_param = await client.upload_cdn_object(
        build_upload_url(upload_param, filekey, cdn_base_url), ciphertext
    )
    return {
        "type": ITEM_IMAGE,
        "image_item": {
            "media": {
                "encrypt_query_param": download_param,
                # base64 of the hex key: the encoding media.parse_aes_key reads back.
                "aes_key": base64.b64encode(key.hex().encode("ascii")).decode("ascii"),
                "encrypt_type": ENCRYPT_TYPE_AES_ECB,
            },
            "mid_size": len(ciphertext),
        },
    }
