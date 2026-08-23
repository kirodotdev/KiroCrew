"""WeCom inbound media: the CDN crypto and the envelope shapes it reads.

The cipher is dictated by the remote protocol (AES-256-CBC, PKCS#7 to a 32-byte
multiple, IV = the first 16 bytes of the object's own key), so these tests encrypt
with that construction and assert the module decrypts it -- a round trip, not a
restatement of the implementation. Getting any part of it wrong yields plausible
GARBAGE rather than an error, which is why each part is pinned separately.
"""

from __future__ import annotations

import asyncio
import base64
import os

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from kiro_crew.wecom.attachments import to_attachments
from kiro_crew.wecom.media import (
    WeComMediaError,
    decode_aes_key,
    decrypt_media,
    media_items,
    mixed_text,
)


def _encrypt(plain: bytes, key: bytes) -> bytes:
    padder = padding.PKCS7(32 * 8).padder()
    padded = padder.update(plain) + padder.finalize()
    # The test has to encrypt with the SAME construction the platform uses, or it
    # would not be testing the protocol we actually receive.
    # nosemgrep: python.cryptography.security.mode-without-authentication.crypto-mode-without-authentication  # noqa: E501
    enc = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return enc.update(padded) + enc.finalize()


class TestKeyDecoding:
    def test_base64_of_raw_key_bytes(self) -> None:
        key = os.urandom(32)
        assert decode_aes_key(base64.b64encode(key).decode()) == key

    def test_base64_of_ascii_hex(self) -> None:
        # The same value arrives in TWO encodings depending on the item type.
        # Guessing wrong decrypts to noise, so both are accepted explicitly.
        key = os.urandom(32)
        encoded = base64.b64encode(key.hex().encode()).decode()
        assert decode_aes_key(encoded) == key

    def test_an_empty_key_is_refused(self) -> None:
        with pytest.raises(WeComMediaError, match="no aeskey"):
            decode_aes_key("")

    def test_non_base64_is_refused(self) -> None:
        with pytest.raises(WeComMediaError, match="base64"):
            decode_aes_key("!!!not base64!!!")

    def test_a_wrong_length_key_is_refused_rather_than_padded(self) -> None:
        # A 16-byte key would silently select AES-128 in a naive implementation
        # and fail far from the cause.
        with pytest.raises(WeComMediaError, match="expected 32 or 64"):
            decode_aes_key(base64.b64encode(os.urandom(16)).decode())


class TestDecrypt:
    def test_round_trips_the_protocol_construction(self) -> None:
        key = os.urandom(32)
        plain = b"a screenshot's bytes, near enough" * 5
        assert decrypt_media(_encrypt(plain, key), key) == plain

    def test_a_32_byte_pad_boundary_is_unpadded_exactly(self) -> None:
        # WeCom pads to a multiple of 32, not the 16-byte AES block. Unpadding at
        # the wrong block size keeps or eats real bytes.
        key = os.urandom(32)
        for size in (1, 31, 32, 33, 64):
            plain = b"x" * size
            assert decrypt_media(_encrypt(plain, key), key) == plain

    def test_the_wrong_key_is_reported_as_a_key_problem(self) -> None:
        key = os.urandom(32)
        blob = _encrypt(b"hello", key)
        with pytest.raises(WeComMediaError, match="wrong aeskey"):
            decrypt_media(blob, os.urandom(32))

    def test_a_short_key_is_refused(self) -> None:
        with pytest.raises(WeComMediaError, match="32-byte key"):
            decrypt_media(b"\x00" * 32, os.urandom(16))

    def test_a_truncated_object_is_refused(self) -> None:
        with pytest.raises(WeComMediaError, match="whole number of AES blocks"):
            decrypt_media(b"\x00" * 33, os.urandom(32))

    def test_an_empty_object_is_refused(self) -> None:
        with pytest.raises(WeComMediaError, match="empty"):
            decrypt_media(b"", os.urandom(32))


class TestEnvelopeShapes:
    def test_a_plain_image_message(self) -> None:
        body = {"msgtype": "image", "image": {"url": "https://cdn/x", "aeskey": "k"}}
        assert media_items(body) == [{"kind": "image", "url": "https://cdn/x", "aeskey": "k"}]

    def test_a_mixed_message_yields_its_media_and_its_caption(self) -> None:
        # A captioned screenshot: the caption lives in the item list, NOT in
        # body.text, so reading only text loses it.
        body = {
            "msgtype": "mixed",
            "mixed": {
                "msg_item": [
                    {"msgtype": "text", "text": {"content": "what is this?"}},
                    {"msgtype": "image", "image": {"url": "https://cdn/y", "aeskey": "k2"}},
                ]
            },
        }
        assert [i["kind"] for i in media_items(body)] == ["image"]
        assert mixed_text(body) == "what is this?"

    def test_voice_yields_no_download(self) -> None:
        # WeCom transcribes voice itself and hands back the text, so there is no
        # asset worth fetching -- and nothing shipped here decodes its codec.
        body = {"msgtype": "voice", "voice": {"content": "转写的文本"}}
        assert media_items(body) == []

    def test_a_text_message_yields_nothing(self) -> None:
        assert media_items({"msgtype": "text", "text": {"content": "hi"}}) == []
        assert mixed_text({"msgtype": "text"}) == ""

    @pytest.mark.parametrize(
        "body",
        [
            {"msgtype": "mixed", "mixed": "not a dict"},
            {"msgtype": "mixed", "mixed": {"msg_item": "not a list"}},
            {"msgtype": "mixed", "mixed": {"msg_item": [None, 42]}},
            {"msgtype": "image", "image": "not a dict"},
            {},
        ],
    )
    def test_a_malformed_envelope_is_survivable(self, body: dict) -> None:
        assert media_items(body) == []
        mixed_text(body)  # must not raise


class TestAttachmentAdapter:
    def test_each_item_keeps_its_own_key(self) -> None:
        # The key is PER OBJECT, so it has to travel with the item rather than
        # being looked up once for the message.
        body = {
            "msgtype": "mixed",
            "mixed": {
                "msg_item": [
                    {"msgtype": "image", "image": {"url": "u1", "aeskey": "k1"}},
                    {"msgtype": "file", "file": {"url": "u2", "aeskey": "k2", "filename": "a.pdf"}},
                ]
            },
        }
        pairs = to_attachments(body)
        assert [(a.url, k) for a, k in pairs] == [("u1", "k1"), ("u2", "k2")]
        assert pairs[1][0].name == "a.pdf"

    def test_an_item_with_no_url_is_dropped(self) -> None:
        body = {"msgtype": "image", "image": {"aeskey": "k"}}
        assert to_attachments(body) == []

    def test_a_document_gets_its_type_from_its_filename(self) -> None:
        # Defaulting a file to octet-stream made the shared classifier call every
        # document unsupported, so a PDF was refused while the doc said files work.
        body = {"msgtype": "file", "file": {"url": "u", "aeskey": "k", "filename": "spec.pdf"}}
        ((att, _),) = to_attachments(body)
        assert att.mimetype == "application/pdf"

    def test_an_unknowable_filename_falls_back_to_the_kind(self) -> None:
        body = {"msgtype": "file", "file": {"url": "u", "aeskey": "k", "filename": "blob"}}
        ((att, _),) = to_attachments(body)
        assert att.mimetype == "application/octet-stream"

    def test_an_image_keeps_its_sniffable_kind_default(self) -> None:
        # The shared pipeline sniffs an image's real type from its bytes, so the
        # per-kind hint is only a starting point and must not be overridden here.
        body = {"msgtype": "image", "image": {"url": "u", "aeskey": "k"}}
        ((att, _),) = to_attachments(body)
        assert att.mimetype == "image/png"

    def test_a_non_numeric_size_does_not_raise(self) -> None:
        body = {"msgtype": "file", "file": {"url": "u", "aeskey": "k", "filesize": "big"}}
        ((att, _),) = to_attachments(body)
        assert att.size == 0


class TestDownloadCaps:
    def test_the_size_cap_is_enforced_while_reading(self) -> None:
        # Enforced on BYTES READ, never on Content-Length: a header is
        # attacker-influenced, and a lying one would let an unbounded body through.
        from kiro_crew.wecom import media as media_mod

        class FakeContent:
            async def iter_chunked(self, _n):
                for _ in range(100):
                    yield b"x" * 1024

        class FakeResp:
            status = 200
            content = FakeContent()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class FakeSession:
            def get(self, *a, **kw):
                return FakeResp()

        with pytest.raises(WeComMediaError, match="exceeds"):
            asyncio.run(
                media_mod.download_media(
                    FakeSession(),
                    "https://cdn/big",
                    base64.b64encode(os.urandom(32)).decode(),
                    max_bytes=4096,
                )
            )

    def test_a_missing_url_is_refused_before_any_request(self) -> None:
        with pytest.raises(WeComMediaError, match="no url"):
            asyncio.run(
                __import__("kiro_crew.wecom.media", fromlist=["x"]).download_media(None, "", "k")
            )
