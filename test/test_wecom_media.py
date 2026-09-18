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
import socket

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from kiro_crew import link_unfurl
from kiro_crew.wecom import media as media_mod
from kiro_crew.wecom.attachments import to_attachments
from kiro_crew.wecom.media import (
    WeComMediaError,
    _vet_media_url,
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

    def test_aeskey_without_base64_padding(self) -> None:
        # WeCom delivers the aeskey with its trailing ``=`` padding stripped, so a
        # 32-byte key arrives as 43 characters. A padding-strict decode rejects
        # that real, valid key as "not valid base64" and the media is refused —
        # the padding must be restored before decoding.
        key = os.urandom(32)
        unpadded = base64.b64encode(key).decode().rstrip("=")
        assert "=" not in unpadded
        assert decode_aes_key(unpadded) == key

    def test_ascii_hex_aeskey_without_base64_padding(self) -> None:
        # The ascii-hex encoding is delivered the same way — padding stripped.
        key = os.urandom(32)
        unpadded = base64.b64encode(key.hex().encode()).decode().rstrip("=")
        assert decode_aes_key(unpadded) == key

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
        # Fixed keys, NOT `os.urandom`. Decrypting with a wrong key yields random
        # bytes, and whether the PKCS7 unpad rejects them depends on what the final
        # byte happens to be, so the random version failed for real at a measured
        # 0.37% (11 of 3000 trials) with "DID NOT RAISE". AES-CBC is deterministic, so
        # a fixed pair settles the outcome permanently while testing the same thing.
        right = bytes(range(32))
        wrong = bytes(range(32, 64))
        blob = _encrypt(b"hello", right)
        with pytest.raises(WeComMediaError, match="wrong aeskey"):
            decrypt_media(blob, wrong)

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


class _FakeContent:
    """Response body: *count* chunks of *size* bytes."""

    def __init__(self, chunks=(b"",)):
        self._chunks = chunks

    async def iter_chunked(self, _n):
        for chunk in self._chunks:
            yield chunk


class _FakeResp:
    def __init__(self, status=200, chunks=(b"",)):
        self.status = status
        self.content = _FakeContent(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    """Records every ``get`` so a test can prove which transport was used."""

    def __init__(self, status=200, chunks=(b"",)):
        self._status = status
        self._chunks = chunks
        self.calls: list[dict] = []

    def get(self, url, **kw):
        self.calls.append({"url": url, **kw})
        return _FakeResp(self._status, self._chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def close(self):
        return None


class _Pinned:
    """Captures what the non-proxied path builds instead of dialing anything.

    ``aiohttp.TCPConnector`` and ``aiohttp.ClientSession`` are replaced as the
    ``media`` module reaches them, so the test proves the connector's resolver is
    a real :class:`link_unfurl.PinnedResolver` carrying the vetted address —
    without a socket, and without asserting on source text.
    """

    def __init__(self, monkeypatch, *, status=200, chunks=(b"",), resolve="93.184.216.34"):
        self.connector_kwargs: dict = {}
        self.session_kwargs: dict = {}
        self.session = _FakeSession(status, chunks)
        monkeypatch.setattr(link_unfurl, "_default_resolve", lambda *_a: [resolve])

        def fake_connector(**kw):
            self.connector_kwargs = kw
            return object()

        def fake_session(**kw):
            self.session_kwargs = kw
            return self.session

        monkeypatch.setattr(media_mod.aiohttp, "TCPConnector", fake_connector)
        monkeypatch.setattr(media_mod.aiohttp, "ClientSession", fake_session)

    @property
    def resolver(self):
        return self.connector_kwargs.get("resolver")


def _public_resolver(monkeypatch, address="93.184.216.34"):
    """Stub the vet's lookup. Kept a stub so no test here opens a socket."""
    monkeypatch.setattr(link_unfurl, "_default_resolve", lambda *_a: [address])


class TestDownloadCaps:
    def test_the_size_cap_is_enforced_while_reading(self, monkeypatch) -> None:
        # Enforced on BYTES READ, never on Content-Length: a header is
        # attacker-influenced, and a lying one would let an unbounded body through.
        #
        # The URL has to clear the SSRF vet before the cap is reachable at all, so
        # the resolver is stubbed to a public address.
        pinned = _Pinned(monkeypatch, chunks=[b"x" * 1024] * 100)
        with pytest.raises(WeComMediaError, match="exceeds"):
            asyncio.run(
                media_mod.download_media(
                    _FakeSession(),
                    "https://cdn.example/big",
                    base64.b64encode(os.urandom(32)).decode(),
                    max_bytes=4096,
                )
            )
        assert pinned.session.calls, "the cap is enforced on the pinned read"

    def test_the_cap_is_enforced_on_the_proxied_read_too(self, monkeypatch) -> None:
        # Same rule on the other transport: the shared read helper is the only
        # place the cap lives, so neither path may drift from it.
        _public_resolver(monkeypatch)
        session = _FakeSession(chunks=[b"x" * 1024] * 100)
        with pytest.raises(WeComMediaError, match="exceeds"):
            asyncio.run(
                media_mod.download_media(
                    session,
                    "https://cdn.example/big",
                    base64.b64encode(os.urandom(32)).decode(),
                    proxy="http://proxy.example:3128",
                    max_bytes=4096,
                )
            )

    def test_a_missing_url_is_refused_before_any_request(self) -> None:
        with pytest.raises(WeComMediaError, match="no url"):
            asyncio.run(media_mod.download_media(None, "", "k"))


class TestDownloadIsPinnedToTheVettedAddress:
    """The vet resolves the host; the fetch must go to THAT address.

    Vetting a name and letting aiohttp look it up again at connect time leaves a
    DNS-rebinding window: the vet's answer is public, the connector's — microseconds
    later, from a server the attacker controls — is ``169.254.169.254``. The pin
    removes the second lookup, so what was checked is what is dialed.
    """

    _KEY = staticmethod(lambda: base64.b64encode(os.urandom(32)).decode())

    def _run(self, session, **kw):
        return asyncio.run(
            media_mod.download_media(session, "https://cdn.example/o", self._KEY(), **kw)
        )

    def test_no_proxy_pins_the_connector_and_leaves_the_caller_session_alone(
        self, monkeypatch
    ) -> None:
        pinned = _Pinned(monkeypatch, resolve="93.184.216.34")
        caller = _FakeSession()
        with pytest.raises(WeComMediaError):
            self._run(caller)  # the empty body fails to decrypt; the transport is the point

        resolver = pinned.resolver
        assert isinstance(resolver, link_unfurl.PinnedResolver)
        answers = asyncio.run(resolver.resolve("cdn.example", 443))
        assert [a["host"] for a in answers] == ["93.184.216.34"]
        assert pinned.connector_kwargs["limit"] == 1
        assert pinned.connector_kwargs["family"] is socket.AF_UNSPEC
        assert pinned.session_kwargs["connector"] is not None
        assert caller.calls == [], "the shared session must not carry the pinned fetch"
        assert pinned.session.calls[0]["url"].startswith("https://cdn.example/o")
        assert pinned.session.calls[0]["allow_redirects"] is False
        assert "proxy" not in pinned.session.calls[0]

    def test_the_pin_carries_the_resolved_address_not_the_name(self, monkeypatch) -> None:
        # A name that resolves somewhere else entirely must still be dialed at the
        # address the vet approved -- that is the whole property.
        pinned = _Pinned(monkeypatch, resolve="8.8.8.8")
        with pytest.raises(WeComMediaError):
            self._run(_FakeSession())
        answers = asyncio.run(pinned.resolver.resolve("cdn.example", 443))
        assert answers[0]["host"] == "8.8.8.8"
        assert answers[0]["hostname"] == "cdn.example", "SNI and Host keep the real name"

    def test_a_proxy_keeps_the_caller_session_and_builds_no_connector(self, monkeypatch) -> None:
        # Under a proxy aiohttp never resolves the host, so a pin would be
        # unconsulted -- and split-horizon DNS means our answer may not even match
        # the proxy's. The caller's shared session stays the transport.
        pinned = _Pinned(monkeypatch)
        caller = _FakeSession()
        with pytest.raises(WeComMediaError):
            self._run(caller, proxy="http://proxy.example:3128")

        assert pinned.connector_kwargs == {}, "no connector is built for a proxied fetch"
        assert pinned.session_kwargs == {}, "no second session is opened"
        assert caller.calls[0]["proxy"] == "http://proxy.example:3128"
        assert caller.calls[0]["allow_redirects"] is False
        assert caller.calls[0]["url"].startswith("https://cdn.example/o")


class TestMediaUrlVetting:
    """The inbound ``url`` is platform-SUPPLIED but not platform-GUARANTEED.

    Nothing in a callback frame proves the host is one WeCom operates, and the
    fetched body flows on into the attachment pipeline -- so an unvetted fetch is a
    server-side request forgery READ primitive, not a blind one. Every sibling
    channel vets its download URL; this path was the one that did not.
    """

    @staticmethod
    def _public(_host, _port):
        return ["93.184.216.34"]

    @pytest.mark.parametrize(
        "url",
        [
            "http://cdn.example/o",  # a cleartext hop for a body we then decrypt
            "file:///etc/passwd",  # not a download at all
            "ftp://cdn.example/o",
            "//cdn.example/o",  # scheme-relative: no scheme at all
            "cdn.example/o",
        ],
    )
    def test_only_https_is_accepted(self, url) -> None:
        with pytest.raises(WeComMediaError, match="https"):
            _vet_media_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://127.0.0.1/o",
            "https://169.254.169.254/latest/meta-data/",  # instance metadata
            "https://10.1.2.3/o",
            "https://100.64.0.1/o",  # RFC 6598 CGNAT -- a tailnet address
            "https://[::1]/o",
            "https://[fec0::1]/o",
            "https://0x7f000001/o",  # alternate encoding the OS resolver accepts
        ],
    )
    def test_an_internal_destination_is_refused(self, url) -> None:
        """Delegated to `link_unfurl`, so the refusal set is the pinned one."""
        with pytest.raises(WeComMediaError, match="refusing media url"):
            _vet_media_url(url)

    def test_a_public_name_resolving_inward_is_refused(self, monkeypatch) -> None:
        """A name the attacker controls needs no internal-looking host at all."""
        monkeypatch.setattr(link_unfurl, "_default_resolve", lambda *_a: ["127.0.0.1"])
        with pytest.raises(WeComMediaError, match="refusing media url"):
            _vet_media_url("https://harmless.example/o")

    def test_a_malformed_authority_raises_this_module_s_error(self) -> None:
        """`urlsplit` RAISES on `https://[`, so without the guard a hostile body
        surfaced as an uncaught ValueError instead of a WeCom media error."""
        with pytest.raises(WeComMediaError, match="malformed"):
            _vet_media_url("https://[")

    def test_a_legitimate_cdn_url_still_passes(self, monkeypatch) -> None:
        """The half of the property that keeps the fix from being an outage.

        No CDN host allow-list is applied on purpose: WeCom documents no stable
        media-host set, so an invented list would silently drop real media. The
        destination vet closes the same class without naming hosts.
        """
        monkeypatch.setattr(link_unfurl, "_default_resolve", self._public)
        out = _vet_media_url("https://wework.qpic.cn/media/abc?sig=xyz")
        assert out.url.startswith("https://wework.qpic.cn/media/abc")
        assert "sig=xyz" in out.url, "the signature query must survive normalization"

    def test_the_vet_hands_back_the_address_the_caller_must_dial(self, monkeypatch) -> None:
        """Returning only the URL is what made the pin impossible.

        The caller needs the resolved address, the wire host aiohttp will ask its
        resolver with, and the port -- so the vet returns the whole record.
        """
        monkeypatch.setattr(link_unfurl, "_default_resolve", self._public)
        out = _vet_media_url("https://wework.qpic.cn/media/abc")
        assert isinstance(out, link_unfurl.VettedUrl)
        assert out.ip == "93.184.216.34"
        assert out.wire_host == "wework.qpic.cn"
        assert out.port == 443

    def test_port_80_on_an_https_url_is_refused(self, monkeypatch) -> None:
        """The shared vet allows {80, 443} because it also serves plain-http
        unfurling; this caller is https-only, so 443 is the stated scope."""
        monkeypatch.setattr(link_unfurl, "_default_resolve", self._public)
        with pytest.raises(WeComMediaError, match="refusing media url"):
            _vet_media_url("https://cdn.example:80/o")


class TestMediaDownloadRedirects:
    """A followed redirect is a hop the vet never saw."""

    def _run(self, session, **kw):
        return asyncio.run(
            media_mod.download_media(
                session,
                "https://cdn.example/o",
                base64.b64encode(os.urandom(32)).decode(),
                **kw,
            )
        )

    def test_redirects_are_disabled_on_the_request(self, monkeypatch) -> None:
        pinned = _Pinned(monkeypatch)
        with pytest.raises(WeComMediaError):
            self._run(_FakeSession())  # empty body fails to decrypt; the kwarg is the point
        assert pinned.session.calls[0]["allow_redirects"] is False

    @pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
    def test_a_redirect_response_is_refused_rather_than_followed(self, monkeypatch, status) -> None:
        _Pinned(monkeypatch, status=status)
        with pytest.raises(WeComMediaError, match="redirected"):
            self._run(_FakeSession())

    @pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
    def test_a_proxied_redirect_response_is_refused_too(self, monkeypatch, status) -> None:
        _public_resolver(monkeypatch)
        with pytest.raises(WeComMediaError, match="redirected"):
            self._run(_FakeSession(status), proxy="http://proxy.example:3128")

    def test_the_vet_runs_before_any_request(self, monkeypatch) -> None:
        """An internal URL must never reach any session at all."""
        pinned = _Pinned(monkeypatch)
        caller = _FakeSession()
        with pytest.raises(WeComMediaError):
            asyncio.run(
                media_mod.download_media(
                    caller,
                    "https://169.254.169.254/latest/meta-data/",
                    base64.b64encode(os.urandom(32)).decode(),
                )
            )
        assert caller.calls == [], "refused before the fetch, not after"
        assert pinned.connector_kwargs == {}, "no connector is built for a refused url"
