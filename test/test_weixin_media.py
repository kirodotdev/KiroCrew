"""Inbound media for the Weixin (iLink) channel.

Three layers under test:

* ``weixin/media.py`` -- CDN URL construction, the dual AES-key encoding, the
  AES-128-ECB decrypt, and the streaming size cap.
* ``weixin/attachments.py`` -- the envelope -> shared ``Attachment`` mapping and
  the server-transcript short-circuit.
* ``weixin/transport.py`` -- that a media-only message is DISPATCHED rather than
  silently dropped, which is the regression this feature exists to fix.
"""

from __future__ import annotations

import asyncio
import base64
import os
import pathlib
import threading
from typing import Any

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from kiro_crew.messaging.attachments import append_attachment_context
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.weixin.attachments import process_weixin_attachments
from kiro_crew.weixin.client import (
    INBOUND_MEDIA_ITEM_TYPES,
    ITEM_FILE,
    ITEM_IMAGE,
    ITEM_TEXT,
    ITEM_VIDEO,
    ITEM_VOICE,
    ContextTokenStore,
)
from kiro_crew.weixin.media import (
    MAX_CDN_BYTES,
    WeixinMediaError,
    build_download_url,
    decrypt_aes_ecb,
    download_media,
    media_ref,
    parse_aes_key,
)
from kiro_crew.weixin.transport import WEIXIN_CAPABILITIES, WeixinTransport

KEY = bytes(range(16))
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _encrypt(plaintext: bytes, key: bytes = KEY) -> bytes:
    # Test-only inverse of the production decrypt. ECB because that is what the
    # WeChat CDN uses; see kiro_crew.weixin.media's module docstring.
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    enc = Cipher(  # nosec B305  # lgtm[py/weak-cryptographic-algorithm]
        algorithms.AES(key), modes.ECB()
    ).encryptor()
    return enc.update(padded) + enc.finalize()


# ── fakes ────────────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status
        self.content = self

    async def iter_chunked(self, n: int):
        for i in range(0, len(self._body), n):
            yield self._body[i : i + n]

    async def text(self) -> str:
        return self._body.decode("utf-8", "replace")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeSession:
    """Minimal stand-in for ``aiohttp.ClientSession`` over the CDN GET."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self._status = status
        self.urls: list[str] = []

    def get(self, url: str, timeout=None):
        self.urls.append(url)
        return _FakeResponse(self._body, self._status)


class _FakeClient:
    """Enough of ``WeixinClient`` for ``WeixinTransport`` construction."""

    account_id = "acct1"

    async def send_message(self, **kwargs):
        return {}


def _transport(tmp_path, got: list[InboundMessage]) -> WeixinTransport:
    async def dispatch(msg: InboundMessage) -> None:
        got.append(msg)

    return WeixinTransport(
        _FakeClient(),
        account_id="acct1",
        ctx_store=ContextTokenStore(str(tmp_path)),
        allowed_user_ids=["userA"],
        dm_policy="allowlist",
        dispatch=dispatch,
    )


# ── media.py: key decoding ───────────────────────────────────────────────────


class TestAesKeyDecoding:
    """iLink ships the SAME field in two encodings and never says which."""

    def test_base64_of_raw_16_bytes_is_used_directly(self) -> None:
        assert parse_aes_key(base64.b64encode(KEY).decode()) == KEY

    def test_base64_of_ascii_hex_is_hex_parsed_back_to_16_bytes(self) -> None:
        hex_form = base64.b64encode(KEY.hex().encode("ascii")).decode()
        assert parse_aes_key(hex_form) == KEY

    def test_uppercase_hex_is_accepted(self) -> None:
        hex_form = base64.b64encode(KEY.hex().upper().encode("ascii")).decode()
        assert parse_aes_key(hex_form) == KEY

    def test_thirty_two_non_hex_bytes_are_rejected_not_truncated(self) -> None:
        """A 32-byte blob that is not hex is a 32-byte KEY, invalid for AES-128.

        Truncating it to 16 bytes would "work" and decrypt to garbage, so the
        length+hex discrimination must reject rather than salvage.
        """
        blob = base64.b64encode(b"z" * 32).decode()
        with pytest.raises(WeixinMediaError, match="16 raw bytes or 32 hex"):
            parse_aes_key(blob)

    @pytest.mark.parametrize("bad", ["", "!!!not base64!!!", base64.b64encode(b"short").decode()])
    def test_unusable_keys_raise_instead_of_returning_something(self, bad: str) -> None:
        with pytest.raises(WeixinMediaError):
            parse_aes_key(bad)


class TestAesEcbDecrypt:
    def test_roundtrip_recovers_the_plaintext(self) -> None:
        payload = PNG_MAGIC + b"body bytes" * 7
        assert decrypt_aes_ecb(_encrypt(payload), KEY) == payload

    def test_exact_block_multiple_plaintext_survives_pkcs7(self) -> None:
        """PKCS7 appends a FULL padding block at an exact multiple.

        Without unpadding, a 16-byte plaintext would come back 32 bytes long
        with 16 trailing 0x10s -- a corrupt file the model would still be shown.
        """
        payload = b"A" * 16
        assert decrypt_aes_ecb(_encrypt(payload), KEY) == payload

    def test_truncated_ciphertext_raises_instead_of_partial_bytes(self) -> None:
        with pytest.raises(WeixinMediaError, match="multiple of 16"):
            decrypt_aes_ecb(_encrypt(b"hello")[:-3], KEY)

    def test_wrong_key_is_reported_not_silently_garbled(self) -> None:
        """The wrong key almost always breaks PKCS7, which must surface."""
        with pytest.raises(WeixinMediaError, match="unpadding"):
            decrypt_aes_ecb(_encrypt(b"x" * 40), bytes(16))

    def test_non_128_bit_key_is_refused(self) -> None:
        with pytest.raises(WeixinMediaError, match="16-byte key"):
            decrypt_aes_ecb(_encrypt(b"x" * 16), KEY + b"extra")


class TestDownloadUrl:
    def test_base64_query_param_is_percent_encoded(self) -> None:
        """``+`` and ``/`` are ordinary base64 characters.

        Left raw, the CDN reads ``+`` as a space and ``/`` as a path separator,
        so the object 404s for reasons that look like a server problem.
        """
        url = build_download_url("a+b/c=d")
        assert "a%2Bb%2Fc%3Dd" in url
        assert "a+b/c=d" not in url

    def test_url_targets_the_download_endpoint(self) -> None:
        assert build_download_url("p", "https://cdn.example/c2c").startswith(
            "https://cdn.example/c2c/download?encrypted_query_param="
        )


# ── media.py: download ───────────────────────────────────────────────────────


class TestCdnDownload:
    def test_encrypted_object_is_fetched_and_decrypted(self) -> None:
        payload = PNG_MAGIC + b"pixels"
        session = _FakeSession(_encrypt(payload))
        got = asyncio.run(
            download_media(
                session,
                encrypt_query_param="param",
                aes_key_b64=base64.b64encode(KEY).decode(),
            )
        )
        assert got == payload

    def test_absent_key_means_the_object_is_stored_plain(self) -> None:
        """Some images arrive unencrypted; forcing a decrypt would fail them."""
        session = _FakeSession(PNG_MAGIC + b"plain")
        got = asyncio.run(
            download_media(session, encrypt_query_param="param", aes_key_b64="")
        )
        assert got == PNG_MAGIC + b"plain"

    def test_oversized_object_is_refused_while_streaming(self) -> None:
        """The cap is enforced on bytes READ, not on Content-Length.

        A lying (or missing) length header is the whole reason this is checked
        during the read loop -- trusting the header would let one object
        exhaust memory.
        """
        session = _FakeSession(b"x" * 5000)
        with pytest.raises(WeixinMediaError, match="exceeds"):
            asyncio.run(
                download_media(
                    session,
                    encrypt_query_param="param",
                    aes_key_b64="",
                    max_bytes=1000,
                )
            )

    def test_http_error_surfaces_with_the_status(self) -> None:
        session = _FakeSession(b"gone", status=404)
        with pytest.raises(WeixinMediaError, match="404"):
            asyncio.run(
                download_media(session, encrypt_query_param="param", aes_key_b64="")
            )

    def test_missing_query_param_is_refused_before_any_request(self) -> None:
        session = _FakeSession(b"never")
        with pytest.raises(WeixinMediaError, match="encrypt_query_param"):
            asyncio.run(download_media(session, encrypt_query_param="", aes_key_b64=""))
        assert session.urls == []

    def test_default_cap_is_bounded(self) -> None:
        assert 0 < MAX_CDN_BYTES <= 64 * 1024 * 1024


class TestMediaRef:
    def test_image_hex_aeskey_wins_over_media_aes_key(self) -> None:
        """``image_item.aeskey`` is the preferred inbound key location.

        Both fields are often present and they are NOT the same encoding, so
        picking the wrong one decrypts to garbage rather than erroring.
        """
        item = {
            "type": ITEM_IMAGE,
            "image_item": {
                "aeskey": KEY.hex(),
                "media": {"encrypt_query_param": "p", "aes_key": "d3Jvbmcta2V5"},
            },
        }
        param, key_b64 = media_ref(item, "image_item")
        assert param == "p"
        assert parse_aes_key(key_b64) == KEY

    def test_non_hex_aeskey_falls_back_to_media_aes_key(self) -> None:
        item = {
            "type": ITEM_IMAGE,
            "image_item": {
                "aeskey": "not-hex!!",
                "media": {"encrypt_query_param": "p", "aes_key": "fallback"},
            },
        }
        assert media_ref(item, "image_item") == ("p", "fallback")

    def test_absent_media_yields_empty_strings_not_an_exception(self) -> None:
        """An absent reference becomes a visible rejection downstream.

        Raising here would lose every other item in the same message.
        """
        assert media_ref({"type": ITEM_FILE}, "file_item") == ("", "")
        assert media_ref({"type": ITEM_FILE, "file_item": "nope"}, "file_item") == ("", "")


# ── attachments.py: envelope mapping ─────────────────────────────────────────


class TestAttachmentIngestion:
    def test_image_item_is_downloaded_decrypted_and_offered_as_a_path(self) -> None:
        session = _FakeSession(_encrypt(PNG_MAGIC + b"pixeldata"))
        items = [
            {
                "type": ITEM_IMAGE,
                "image_item": {
                    "aeskey": KEY.hex(),
                    "media": {"encrypt_query_param": "p1"},
                    "hd_size": 9,
                },
            }
        ]
        result = asyncio.run(process_weixin_attachments(items, session=session))
        try:
            assert len(result.image_paths) == 1, result.rejections
            path = result.image_paths[0]
            # Sniffed as PNG despite the envelope carrying no type at all, so
            # the ACP encoder (which reads the suffix) sends truthful metadata.
            assert path.endswith(".png")
            with open(path, "rb") as fh:
                assert fh.read().startswith(PNG_MAGIC)
            assert path in append_attachment_context("look", result)
        finally:
            for p in result.temp_paths:
                if os.path.exists(p):
                    os.unlink(p)

    def test_non_image_bytes_declared_as_an_image_are_rejected(self) -> None:
        """CWE-434: the declared type is attacker-controlled, the bytes are not."""
        session = _FakeSession(_encrypt(b"#!/bin/sh\nrm -rf /\n"))
        items = [
            {
                "type": ITEM_IMAGE,
                "image_item": {
                    "aeskey": KEY.hex(),
                    "media": {"encrypt_query_param": "p1"},
                },
            }
        ]
        result = asyncio.run(process_weixin_attachments(items, session=session))
        assert result.image_paths == []
        assert result.rejections

    def test_server_side_voice_transcript_skips_the_download_entirely(self) -> None:
        """iLink voice is SILK, which our STT backends cannot decode.

        When the server already transcribed it, downloading is strictly worse:
        it spends a CDN round trip to produce a "transcription failed" note.
        """
        session = _FakeSession(b"never fetched")
        items = [
            {
                "type": ITEM_VOICE,
                "voice_item": {
                    "encode_type": 6,
                    "text": "take a look at this error",
                    "media": {"encrypt_query_param": "p1", "aes_key": "k"},
                },
            }
        ]
        result = asyncio.run(process_weixin_attachments(items, session=session))
        assert session.urls == []
        assert any("take a look at this error" in b for b in result.text_blocks)

    def test_a_transcribed_voice_does_not_suppress_an_untranscribed_sibling(self) -> None:
        """The short-circuit is per item, not per message.

        Two voice notes in one envelope where only the first carries server
        text: the second still has to be downloaded, or the user's second
        message is silently lost.
        """
        session = _FakeSession(_encrypt(b"\x02#!SILK_V3"))
        items = [
            {
                "type": ITEM_VOICE,
                "voice_item": {
                    "encode_type": 6,
                    "text": "first one, already transcribed",
                    "media": {"encrypt_query_param": "p1", "aes_key": "k"},
                },
            },
            {
                "type": ITEM_VOICE,
                "voice_item": {
                    "encode_type": 6,
                    "media": {
                        "encrypt_query_param": "p2",
                        "aes_key": base64.b64encode(KEY).decode(),
                    },
                },
            },
        ]
        result = asyncio.run(process_weixin_attachments(items, session=session))
        try:
            assert any("first one, already transcribed" in b for b in result.text_blocks)
            # Only the untranscribed sibling was fetched.
            assert [u for u in session.urls if "p2" in u], session.urls
            assert not [u for u in session.urls if "p1" in u], session.urls
        finally:
            for p in result.temp_paths:
                if os.path.exists(p):
                    os.unlink(p)

    def test_the_decrypted_write_never_runs_on_the_event_loop_thread(self, monkeypatch) -> None:
        """A 32 MB write on a non-local TMPDIR would stall every session.

        Asserted by thread identity rather than by naming ``asyncio.to_thread``:
        an inline ``open()/write()`` puts the write on the loop thread, which is
        the actual failure, and this catches it however it is spelled.
        """
        session = _FakeSession(_encrypt(PNG_MAGIC + b"pixeldata"))
        items = [
            {
                "type": ITEM_IMAGE,
                "image_item": {
                    "aeskey": KEY.hex(),
                    "media": {"encrypt_query_param": "p1"},
                    "hd_size": 9,
                },
            }
        ]
        write_threads: list[int] = []
        real_write_bytes = pathlib.Path.write_bytes

        def _spy(self: pathlib.Path, data: bytes) -> int:
            write_threads.append(threading.get_ident())
            return real_write_bytes(self, data)

        monkeypatch.setattr(pathlib.Path, "write_bytes", _spy)

        async def _run() -> tuple[int, Any]:
            return threading.get_ident(), await process_weixin_attachments(
                items, session=session
            )

        loop_thread, result = asyncio.run(_run())
        try:
            assert result.image_paths, result.rejections
            assert write_threads, "the payload was not written through Path.write_bytes"
            assert loop_thread not in write_threads
        finally:
            for p in result.temp_paths:
                if os.path.exists(p):
                    os.unlink(p)

    def test_video_is_rejected_with_a_usable_instruction(self) -> None:
        session = _FakeSession(b"unused")
        items = [
            {
                "type": ITEM_VIDEO,
                "video_item": {"media": {"encrypt_query_param": "p1", "aes_key": "k"}},
            }
        ]
        result = asyncio.run(process_weixin_attachments(items, session=session))
        assert result.image_paths == []
        assert any("video" in r.lower() for r in result.rejections)

    def test_file_size_arriving_as_a_string_does_not_crash(self) -> None:
        """``file_item.len`` is a STRING in the iLink proto, unlike every other size."""
        session = _FakeSession(_encrypt(b"col_a,col_b\n1,2\n"))
        items = [
            {
                "type": ITEM_FILE,
                "file_item": {
                    "file_name": "data.csv",
                    "len": "16",
                    "media": {"encrypt_query_param": "p1", "aes_key": base64.b64encode(KEY).decode()},
                },
            }
        ]
        result = asyncio.run(process_weixin_attachments(items, session=session))
        try:
            assert any("col_a" in b for b in result.text_blocks), result.rejections
        finally:
            for p in result.temp_paths:
                if os.path.exists(p):
                    os.unlink(p)

    def test_no_media_items_means_no_session_use_and_no_work(self) -> None:
        session = _FakeSession(b"unused")
        result = asyncio.run(
            process_weixin_attachments(
                [{"type": ITEM_TEXT, "text_item": {"text": "hi"}}], session=session
            )
        )
        assert session.urls == []
        assert result.temp_paths == []


# ── transport.py: the dropped-message regression ─────────────────────────────


class TestInboundMediaRouting:
    def test_capabilities_declare_inbound_files(self) -> None:
        assert WEIXIN_CAPABILITIES.files_inbound is True
        # Outbound is still unimplemented; the contract must not claim it.
        assert WEIXIN_CAPABILITIES.files_outbound is False

    def test_media_only_message_is_dispatched_not_dropped(self, tmp_path) -> None:
        """The regression: a screenshot with no caption used to vanish.

        ``receive`` returned early on empty text, so the user saw the message
        send successfully while the agent was never told anything arrived.
        """
        got: list[InboundMessage] = []
        transport = _transport(tmp_path, got)
        asyncio.run(
            transport.receive(
                {
                    "from_user_id": "userA",
                    "msg_id": "m-media",
                    "item_list": [
                        {
                            "type": ITEM_IMAGE,
                            "image_item": {"media": {"encrypt_query_param": "p1"}},
                        }
                    ],
                }
            )
        )
        assert len(got) == 1
        assert got[0].text == ""
        assert len(got[0].attachments) == 1
        assert got[0].attachments[0]["type"] == ITEM_IMAGE

    def test_caption_and_image_are_both_carried(self, tmp_path) -> None:
        got: list[InboundMessage] = []
        transport = _transport(tmp_path, got)
        asyncio.run(
            transport.receive(
                {
                    "from_user_id": "userA",
                    "msg_id": "m-both",
                    "item_list": [
                        {"type": ITEM_TEXT, "text_item": {"text": "look at this"}},
                        {
                            "type": ITEM_IMAGE,
                            "image_item": {"media": {"encrypt_query_param": "p1"}},
                        },
                    ],
                }
            )
        )
        assert got[0].text == "look at this"
        assert len(got[0].attachments) == 1

    def test_unknown_item_types_are_not_forwarded_as_attachments(self, tmp_path) -> None:
        """Only the four CDN-backed types are media.

        A future item type (iLink's tool-call progress items are 11/12) must not
        be handed to the download path, which would fail on every message.
        """
        got: list[InboundMessage] = []
        transport = _transport(tmp_path, got)
        asyncio.run(
            transport.receive(
                {
                    "from_user_id": "userA",
                    "msg_id": "m-unknown",
                    "item_list": [
                        {"type": ITEM_TEXT, "text_item": {"text": "hi"}},
                        {"type": 11, "tool_call_start_item": {"tool_name": "x"}},
                    ],
                }
            )
        )
        assert got[0].attachments == []

    def test_empty_envelope_is_still_dropped(self, tmp_path) -> None:
        """No text AND no media is nothing to act on."""
        got: list[InboundMessage] = []
        transport = _transport(tmp_path, got)
        asyncio.run(transport.receive({"from_user_id": "userA", "item_list": []}))
        assert got == []

    def test_media_type_set_matches_the_item_constants(self) -> None:
        assert INBOUND_MEDIA_ITEM_TYPES == {ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO}
        assert ITEM_TEXT not in INBOUND_MEDIA_ITEM_TYPES
