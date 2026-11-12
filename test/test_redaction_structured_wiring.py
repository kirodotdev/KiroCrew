"""The credential plan's callers with the structured detectors' switch on.

* pass 1 does not report the branches the detectors cover, a bare AWS key ID, or a
  labelled key ID the detectors protect; every other pass-1 branch keeps running;
* an Identifier value the detectors protect stays visible to the later passes;
* a pass-1 warning gives the length of its span on the original text;
* the display caches and stored verdicts name the engine that computed them, so a
  switch change never serves an answer the other engine produced;
* a field-name key under an authorization key in a recorded frame stays legible.

Every credential here is synthetic and assembled at run time.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from redaction_structured_cases import B64, aws_key_id, aws_secret, github_classic, synth

from kiro_crew.acp import _frame_record
from kiro_crew.dashboard import chat_utils, display_battery
from kiro_crew.dashboard import display_verdicts as dv
from kiro_crew.dashboard.chat_utils import _prepare_messages
from kiro_crew.security import StreamRedactor, redact
from kiro_crew.security.redaction import redact_credentials
from kiro_crew.security.redaction_structured import compat

KEY = "dashboard_chat-1-1700000000"
#: A word the stand-in battery below redacts only while the detectors run.
WORD = "codeword-6612"
TAG = "[REDACTED: credential]"


@pytest.fixture
def table_on() -> Iterator[None]:
    before = compat.use_rule_table()
    compat.publish_use_rule_table(True)
    try:
        yield
    finally:
        compat.publish_use_rule_table(before)


@pytest.fixture
def switch() -> Iterator[None]:
    """Starts OFF; the test moves it. Restored afterwards."""
    before = compat.use_rule_table()
    compat.publish_use_rule_table(False)
    try:
        yield
    finally:
        compat.publish_use_rule_table(before)


def test_the_switch_ships_on(tmp_path, monkeypatch) -> None:
    """With no owner record the structured engine runs (the selector's default)."""
    from kiro_crew.security import redaction_switch

    monkeypatch.setattr(redaction_switch, "_path", lambda: tmp_path / "none.json")
    before = compat.use_rule_table()
    try:
        assert compat.reload_engine_choice() is True
    finally:
        compat.publish_use_rule_table(before)


# ── pass 1 without the covered branches ─────────────────────────────────────


def test_a_labelled_aws_secret_keeps_its_label(table_on):
    """``aws_secret_access_key = <secret> words`` -> label kept, value replaced."""
    out, _ = redact_credentials("aws_secret_access_key = " + aws_secret(seed=3) + " words")
    assert out == f"aws_secret_access_key = {TAG} words"


@pytest.mark.parametrize("size", [1, 2, 3, 5, 8, 13, 40])
def test_a_labelled_aws_secret_streams_as_the_batch_pass(table_on, size):
    text = "aws_secret_access_key = " + aws_secret(seed=3) + " words"
    stream = StreamRedactor()
    out = "".join(stream.feed(text[i : i + size]) for i in range(0, len(text), size))
    assert out + stream.flush() == redact(text)


#: Letters and ``+/`` only: the bare-secret pass needs a digit, so only pass 1 can
#: remove a 40-character secret drawn from it.
_NO_DIGITS = "".join(c for c in B64 if not c.isdigit())

#: Labelled secrets the detectors' parser does not read (mid-line after a word, a
#: newline before the value, a separator wider than eight blanks).
_UNREAD_LABELS = [
    "INFO SecretAccessKey: {v} done",
    "INFO aws_secret_access_key = {v} done",
    "aws_secret_access_key =\n{v}\n",
    "SecretAccessKey:" + " " * 12 + "{v}\n",
]


@pytest.mark.parametrize("shape", _UNREAD_LABELS)
def test_a_labelled_secret_the_detectors_do_not_read_is_still_removed(table_on, shape):
    secret = synth(40, _NO_DIGITS, seed=41)
    text = shape.format(v=secret)
    assert secret not in redact_credentials(text)[0]
    stream = StreamRedactor()
    out = "".join(stream.feed(text[i : i + 7]) for i in range(0, len(text), 7))
    assert secret not in out + stream.flush()


def _streamed(text: str, size: int = 7) -> str:
    stream = StreamRedactor()
    out = "".join(stream.feed(text[i : i + size]) for i in range(0, len(text), size))
    return out + stream.flush()


#: Labelled values the detectors refuse for their shape: an S3-compatible secret
#: that is not 40 characters (DigitalOcean Spaces 43, Cloudflare R2 64 hex, any
#: other length) and a session token under the detectors' 100-character floor.
_OFF_SHAPE_LABELLED = [
    ("aws_secret_access_key={v}\n", 43, _NO_DIGITS),
    ('{{"SecretAccessKey": "{v}"}}', 43, _NO_DIGITS),
    ("aws_secret_access_key = {v}\n", 64, "0123456789abcdef"),
    ('{{"SecretAccessKey": "{v}"}}', 64, "0123456789abcdef"),
    ("aws_secret_access_key={v}\n", 39, _NO_DIGITS),
    ("aws_secret_access_key={v}\n", 20, _NO_DIGITS),
    ("aws_session_token={v}\n", 60, _NO_DIGITS),
    ("aws_session_token={v}\n", 99, _NO_DIGITS),
]


@pytest.mark.parametrize(("shape", "length", "alphabet"), _OFF_SHAPE_LABELLED)
def test_a_labelled_value_the_detectors_refuse_for_its_shape_is_removed(
    table_on, shape, length, alphabet
):
    """Pass 1 removes it, as with the switch off; only a tag or placeholder is ruled."""
    value = synth(length, alphabet, seed=90 + length)
    text = shape.format(v=value)
    assert value not in redact_credentials(text)[0]
    assert value not in _streamed(text)


@pytest.mark.parametrize(
    ("shape", "value"),
    [
        ("SecretAccessKey: {v}\n", synth(64, "0123456789abcdef", seed=91)),
        ("aws_secret_access_key={v}\n", synth(64, "0123456789abcdef", seed=92)),
        ("SessionToken: {v}\n", synth(120, B64, seed=93)),
        ("aws_session_token={v}\n", synth(120, B64, seed=94)),
        ("AccessKeyId: {v} next", synth(24, seed=95)),
        ("aws_access_key_id={v} next", synth(24, seed=96)),
    ],
    ids=[
        "SecretAccessKey",
        "aws_secret_access_key",
        "SessionToken",
        "aws_session_token",
        "AccessKeyId",
        "aws_access_key_id",
    ],
)
@pytest.mark.parametrize("where", ["after", "before", "inside"])
def test_a_placeholder_character_in_the_input_does_not_exempt_a_labelled_value(
    table_on, shape, value, where
):
    """The mask placeholder is a character input can carry. Only a mask the detectors
    wrote exempts a labelled value; one typed into the text is read as text."""
    mark = compat.PLACEHOLDER
    typed = {"after": value + mark, "before": mark + value, "inside": value[:8] + mark + value[8:]}
    text = shape.format(v=typed[where])
    for piece in (value, value[8:]) if where == "inside" else (value,):
        assert piece not in redact_credentials(text)[0]
        assert piece not in _streamed(text)


def _png_with_text(words: bytes) -> str:
    """A base64, complete one-pixel PNG whose ``tEXt`` chunk holds ``words``."""
    import base64
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data).to_bytes(4, "big")
        return len(data).to_bytes(4, "big") + kind + data + crc

    ihdr = (1).to_bytes(4, "big") * 2 + b"\x08\x02\x00\x00\x00"
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"tEXt", words)
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
        + chunk(b"IEND", b"")
    )
    return base64.b64encode(png).decode()


@pytest.mark.parametrize("shape", ["![c](data:image/png;base64,{v})", "image {v} end"])
def test_an_image_payload_holding_an_encoded_secret_is_removed(table_on, shape):
    payload = _png_with_text(b"Comment\x00aws_secret_access_key=" + aws_secret(seed=42).encode())
    text = shape.format(v=payload)
    assert payload not in redact_credentials(text)[0]
    stream = StreamRedactor()
    out = "".join(stream.feed(text[i : i + 7]) for i in range(0, len(text), 7))
    assert payload not in out + stream.flush()


@pytest.mark.parametrize(
    "shape", ["x-amz-id-2: {v}\n", '{{"HostId": "{v}"}}', "<HostId>{v}</HostId>"]
)
def test_a_request_id_holding_an_encoded_secret_is_removed(table_on, shape):
    import base64

    words = b"aws_secret_access_key=" + aws_secret(seed=43).encode() + b" ok"
    value = base64.b64encode(words).decode()
    text = shape.format(v=value)
    assert value not in redact_credentials(text)[0]


_HEX40 = synth(40, "0123456789abcdef", seed=44)
_HEX64 = synth(64, "0123456789abcdef", seed=45)


@pytest.mark.parametrize(
    "shape",
    [
        "Authorization: Bearer {v}\n",
        'curl -H "Authorization: Bearer {w}" https://api.example.test',
        "see https://h.example.test/cb?token={v} ok",
        "npm_{v} done",
    ],
)
def test_a_hex_credential_shaped_like_a_git_object_name_is_removed(table_on, shape):
    """A hex run inside a credential (a Bearer header, a ``token=`` value) is judged by
    main's passes, as with the switch off, not kept as a git object name."""
    text = shape.format(v=_HEX40, w=_HEX64)
    compat.publish_use_rule_table(False)
    off = redact_credentials(text)[0]
    compat.publish_use_rule_table(True)
    on = redact_credentials(text)[0]
    for value in (_HEX40, _HEX64):
        if value in text and value not in off:
            assert value not in on, on


def test_a_bare_git_object_name_stays_visible(table_on):
    text = f"commit {_HEX40} and tree {_HEX64}"
    assert redact_credentials(text)[0] == text


@pytest.mark.parametrize(
    "shape", ["![c](data:image/png;base64,{v})", "data:image/jpeg;base64,{v} ok"]
)
def test_a_bitmap_data_uri_holding_a_bare_secret_is_removed(table_on, shape):
    """A payload that is not image bytes (here a bare AWS secret) is not an image."""
    secret = aws_secret(seed=46)
    text = shape.format(v=secret)
    assert secret not in redact_credentials(text)[0]
    stream = StreamRedactor()
    out = "".join(stream.feed(text[i : i + 7]) for i in range(0, len(text), 7))
    assert secret not in out + stream.flush()


@pytest.mark.parametrize("pad", [24, 160])
@pytest.mark.parametrize(
    "shape", ["![c](data:image/png;base64,{v})", "data:image/jpeg;base64,{v} ok"]
)
def test_a_padded_bitmap_data_uri_holding_a_bare_secret_is_removed(table_on, shape, pad):
    """Padding a secret past an image's minimum length does not make it an image."""
    secret = aws_secret(seed=48)
    text = shape.format(v=secret + "A" * pad)
    assert secret not in redact_credentials(text)[0]
    assert secret not in _streamed(text)


@pytest.mark.parametrize("shape", ["![c](data:image/png;base64,{v})", "image {v} end"])
def test_an_image_payload_holding_an_unlabelled_secret_is_removed(table_on, shape):
    payload = _png_with_text(b"Comment\x00" + aws_secret(seed=47).encode())
    text = shape.format(v=payload)
    assert payload not in redact_credentials(text)[0]


def test_an_image_payload_holding_no_secret_stays_visible(table_on):
    payload = _png_with_text(b"Comment\x00made with a camera")
    text = f"![c](data:image/png;base64,{payload})"
    assert redact_credentials(text)[0] == text


#: A file signature's base64 glued straight to a bare secret: the payload opens
#: like a bitmap but its bytes are not one.
_FORGED = [
    ("![c](data:image/png;base64,iVBORw0KGgo{v}AAAAAAAAAAAAA)", "png-data-uri"),
    ("data:image/jpeg;base64,/9j/{v}AAAAAAAAAAAAAAAAAAAA ok", "jpeg-data-uri"),
    ("data:image/gif;base64,R0lGOD{v}AAAAAAAAAAAAAAAAAA ok", "gif-data-uri"),
    ("image iVBORw0KGgo{v}AAAAAAAAAAAAA end", "png-bare"),
]


@pytest.mark.parametrize("shape", [s for s, _ in _FORGED], ids=[i for _, i in _FORGED])
def test_a_forged_file_signature_does_not_make_a_secret_an_image(table_on, shape):
    secret = aws_secret(seed=49)
    text = shape.format(v=secret)
    assert secret not in redact_credentials(text)[0]
    assert secret not in _streamed(text)


def _complete_image(kind: str) -> bytes:
    """A small, structurally complete image file of *kind* with random-looking content."""
    import hashlib
    import struct
    import zlib

    body = b"".join(hashlib.sha256(bytes([n])).digest() for n in range(12))

    def png_chunk(kind_: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind_ + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind_ + data + struct.pack(">I", crc)

    if kind == "png":
        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
        return (
            b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", ihdr)
            + png_chunk(b"IDAT", zlib.compress(b"\x00" + body[:3]) + body)
            + png_chunk(b"IEND", b"")
        )
    if kind == "jpeg":
        app0 = b"JFIF\x00" + body
        sof0 = b"\x08\x00\x01\x00\x01\x01\x01\x11\x00"
        sos = b"\x01\x01\x00\x00\x3f\x00"
        return (
            b"\xff\xd8\xff\xe0"
            + struct.pack(">H", len(app0) + 2)
            + app0
            + b"\xff\xc0"
            + struct.pack(">H", len(sof0) + 2)
            + sof0
            + b"\xff\xda"
            + struct.pack(">H", len(sos) + 2)
            + sos
            + body.replace(b"\xff", b"\xff\x00")
            + b"\xff\xd9"
        )
    if kind == "gif":
        blocks = b"".join(
            bytes([len(body[i : i + 255])]) + body[i : i + 255] for i in range(0, len(body), 255)
        )
        image = b"\x2c" + struct.pack("<HHHH", 0, 0, 1, 1) + b"\x00" + b"\x02" + blocks + b"\x00"
        return b"GIF89a" + struct.pack("<HH", 1, 1) + b"\x00\x00\x00" + image + b"\x3b"
    if kind == "webp":
        frame = b"\x00\x00\x00\x9d\x01\x2a" + body
        chunk = b"VP8 " + struct.pack("<I", len(frame)) + frame + b"\x00" * (len(frame) & 1)
        return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk
    # 32 x 4 pixels at 24 bits: four 96-byte rows, 384 bytes, which is len(body).
    dib = struct.pack("<IiiHHIIiiII", 40, 32, 4, 1, 24, 0, len(body), 0, 0, 0, 0)
    size = 14 + len(dib) + len(body)
    return b"BM" + struct.pack("<IHHI", size, 0, 0, 14 + len(dib)) + dib + body


def _bitmap(kind: str) -> str:
    """The base64 of a small, complete image file of *kind*."""
    import base64

    return base64.b64encode(_complete_image(kind)).decode()


@pytest.mark.parametrize("kind", ["png", "jpeg", "gif", "webp", "bmp"])
def test_a_well_formed_bitmap_payload_stays_visible(table_on, kind):
    payload = _bitmap(kind)
    text = f"![c](data:image/{kind};base64,{payload})"
    assert redact_credentials(text)[0] == text


@pytest.mark.parametrize("kind", ["png", "jpeg", "gif", "webp"])
def test_a_bare_complete_image_payload_stays_visible(table_on, kind):
    payload = _bitmap(kind)
    assert redact_credentials(f"image {payload} end")[0] == f"image {payload} end"


def _forged_head_then_secret(kind: str, secret: str) -> str:
    """A valid image's first 24 bytes in base64, then a plaintext secret, then padding."""
    import base64

    head = base64.b64encode(_complete_image(kind)[:24]).decode()
    return head + secret + "A" * 32


@pytest.mark.parametrize("kind", ["png", "jpeg", "gif", "webp", "bmp"])
@pytest.mark.parametrize("bare", [False, True], ids=["data-uri", "bare"])
def test_a_valid_image_header_does_not_shield_an_appended_secret(table_on, kind, bare):
    """Only a complete image is exempt: a real header with a secret after it is not one."""
    if bare and kind == "bmp":
        pytest.skip("a bare BMP payload is not an Identifier row")
    secret = aws_secret(seed=53)
    payload = _forged_head_then_secret(kind, secret)
    text = f"image {payload} end" if bare else f"![c](data:image/{kind};base64,{payload})"
    assert secret not in redact_credentials(text)[0]
    assert secret not in _streamed(text)


def _framed(head: bytes, secret: str, tail: bytes) -> str:
    """``head``, the bytes a base64 ``secret`` decodes to, then ``tail``, in base64.

    ``head`` is a whole number of base64 groups, so ``secret`` appears in the
    payload exactly as written.
    """
    import base64

    assert len(head) % 3 == 0 and len(secret) % 4 == 0
    return base64.b64encode(head + base64.b64decode(secret) + tail).decode()


def _malformed(kind: str, secret: str) -> str:
    """A payload that opens and closes as an image of *kind* but is not one inside.

    Each passes a signature-and-trailer reading: a JPEG with a zero-length APP0 and
    no frame or scan; a GIF with no image block; a WebP whose one chunk is not a VP8
    frame; a BMP whose pixel rows do not fit its size; a PNG with no image data.
    """
    import struct
    import zlib

    if kind == "jpeg":
        return _framed(b"\xff\xd8\xff\xe0\x00\x00", secret, b"\xff\xd9")
    if kind == "gif":
        return _framed(b"GIF89a", secret, b"\x3b")
    if kind == "webp":
        size = 4 + 30 + 6
        return _framed(b"RIFF" + struct.pack("<I", size) + b"WEBP", secret, b"\x00" * 6)
    if kind == "bmp":
        dib = struct.pack("<IiiHHI", 40, 1, 1, 1, 24, 0) + b"\x00" * 20
        size = 14 + len(dib) + 30 + 1
        head = b"BM" + struct.pack("<IHHI", size, 0, 0, 14 + len(dib)) + dib
        return _framed(head, secret, b"\x00")

    def chunk(kind_: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind_ + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind_ + data + struct.pack(">I", crc)

    import base64

    raw = base64.b64decode(secret)
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    priv = chunk(b"prVt", b"\x00" + raw)
    png = b"\x89PNG\r\n\x1a\n" + ihdr + priv + chunk(b"IEND", b"")
    out = base64.b64encode(png).decode()
    assert secret in out
    return out


@pytest.mark.parametrize("kind", ["jpeg", "gif", "webp", "bmp", "png"])
@pytest.mark.parametrize("bare", [False, True], ids=["data-uri", "bare"])
def test_a_secret_inside_a_malformed_image_is_removed(table_on, kind, bare):
    """An image exempts its payload only when its whole structure reads as that format."""
    if bare and kind == "bmp":
        pytest.skip("a bare BMP payload is not an Identifier row")
    secret = aws_secret(seed=57)
    payload = _malformed(kind, secret)
    assert secret in payload
    text = f"image {payload} end" if bare else f"data:image/{kind};base64,{payload} ok"
    assert secret not in redact_credentials(text)[0]
    assert secret not in _streamed(text)


def test_gpt_review_jpeg_with_a_zero_length_app0_is_not_an_image(table_on):
    """The review's case: ``/9j/4AAA<key>AAAA/9k=``."""
    secret = aws_secret(seed=58)
    text = f"data:image/jpeg;base64,/9j/4AAA{secret}AAAA/9k="
    assert secret not in redact_credentials(text)[0]


def test_a_bare_aws_key_id_stays_visible(table_on):
    key_id = aws_key_id(seed=21)
    assert redact_credentials(f"id {key_id} end")[0] == f"id {key_id} end"


def test_a_labelled_aws_key_id_stays_visible(table_on):
    key_id = aws_key_id(seed=22)
    assert redact_credentials(f"AccessKeyId: {key_id}")[0] == f"AccessKeyId: {key_id}"


def test_a_labelled_key_id_field_holding_no_key_id_is_still_redacted(table_on):
    """The ``AccessKeyId`` branch keeps running for a value the detectors do not protect."""
    value = synth(24, seed=23)
    out, _ = redact_credentials(f"AccessKeyId: {value} next")
    assert value not in out


def test_the_url_userinfo_branch_keeps_running(table_on):
    password = synth(18, seed=24)
    out, _ = redact_credentials(f"see postgres://app:{password}@db.example.test/main")
    assert password not in out


def _long_jwt() -> str:
    from redaction_structured_cases import b64url_json

    claims = {"sub": "user-1", "groups": [f"group-{i:04d}" for i in range(300)]}
    return b64url_json({"alg": "RS256"}) + "." + b64url_json(claims) + "." + synth(342, seed=28)


def _long_iv_jwe() -> str:
    from redaction_structured_cases import b64url_json

    header = b64url_json({"alg": "dir", "enc": "A256GCM"})
    return header + ".." + "A" * 101 + ".YQ." + synth(22, seed=29)


@pytest.mark.parametrize("make", [_long_jwt, _long_iv_jwe], ids=["jwt", "jwe"])
def test_a_jose_token_longer_than_the_detectors_read_is_still_redacted(table_on, make):
    """A token past every detector row's bounds: pass 1's JWT branch removes it."""
    token = make()
    out = redact(f"id_token {token} end")
    assert out == "id_token [REDACTED: credential] end"


def _leaks(token: str, out: str, width: int = 24) -> bool:
    return any(token[i : i + width] in out for i in range(0, len(token) - width + 1))


def _signed_jwt() -> str:
    from redaction_structured_cases import b64url_json

    head = b64url_json({"alg": "HS256", "typ": "JWT"})
    return head + "." + b64url_json({"sub": "a", "x": "y"}) + "." + synth(43, seed=30)


def _non_object_payload_jws() -> str:
    from redaction_structured_cases import b64url_json

    return b64url_json({"alg": "HS256"}) + ".eyJ" + synth(40, seed=31) + "." + synth(43, seed=32)


@pytest.mark.parametrize(
    "build",
    [
        lambda t: f"url /x/{t}.json",
        lambda t: f"T {t}.Next",
        lambda t: f"T {t}...and",
        lambda t: f"T {t}..x",
        lambda t: f"{t}.{t}",
    ],
    ids=["dot-json", "dot-word", "three-dots", "two-dots-word", "two-tokens"],
)
def test_a_jwt_the_detectors_refuse_for_a_trailing_segment_is_redacted(table_on, build):
    """The detectors read the whole dot run and refuse it; pass 1 still removes the token."""
    token = _signed_jwt()
    assert not _leaks(token, redact(build(token)))


def test_a_jws_whose_payload_is_not_an_object_is_redacted(table_on):
    token = _non_object_payload_jws()
    assert not _leaks(token, redact(f"t {token} end"))


def _itsdangerous_cookie() -> str:
    """A Flask session cookie: JSON payload, timestamp, signature; no ``alg``."""
    from redaction_structured_cases import b64url_json

    return b64url_json({"user_id": 7}) + ".Z1Aaqw." + synth(27, seed=37)


def _alg_none_jws() -> str:
    from redaction_structured_cases import b64url_json

    head = b64url_json({"alg": "none", "typ": "JWT"})
    return head + "." + b64url_json({"sub": "admin"}) + "." + synth(43, seed=38)


@pytest.mark.parametrize(
    ("make", "build"),
    [
        (_itsdangerous_cookie, lambda t: f"Set-Cookie: session={t}; HttpOnly"),
        (_alg_none_jws, lambda t: f"token {t} end"),
        (_signed_jwt, lambda t: f"cookie session_{t} end"),
        (_signed_jwt, lambda t: f"cookie x-{t} end"),
    ],
    ids=["itsdangerous-cookie", "alg-none", "glued-underscore", "glued-dash"],
)
def test_a_signed_token_the_detectors_refuse_is_redacted(table_on, make, build):
    """A token whose first segment is a JSON object stays pass 1's, as with the switch off."""
    token = make()
    text = build(token)
    assert not _leaks(token, redact_credentials(text)[0])
    assert not _leaks(token, _streamed(text))


@pytest.mark.parametrize(
    "build",
    [
        lambda t: f"x-amz-id-2: {t}",
        lambda t: f'{{"HostId": "{t}"}}',
        lambda t: f"<HostId>{t}</HostId>",
    ],
    ids=["x-amz-id-2", "hostid-json", "hostid-xml"],
)
@pytest.mark.parametrize("glue", ["", "A", "AbC", "AbCdEfGhIj"], ids=["bare", "+1", "+3", "+10"])
def test_a_bare_secret_in_an_aws_id_slot_is_redacted(table_on, build, glue):
    """A value in an ``x-amz-id-2`` or ``HostId`` slot that is not shaped like an ID AWS
    issues (whole-byte base64 of at least 56 characters) and holds a bare secret is
    judged like any other text: here a 40-character secret, alone or with a few more
    characters, is removed."""
    secret = aws_secret(seed=71)
    text = build(secret + glue)
    assert secret not in redact_credentials(text)[0]
    assert secret not in _streamed(text)


@pytest.mark.parametrize(
    "build",
    [
        lambda t: f"x-amz-id-2: {t}",
        lambda t: f'{{"HostId": "{t}"}}',
        lambda t: f"<HostId>{t}</HostId>",
    ],
    ids=["x-amz-id-2", "hostid-json", "hostid-xml"],
)
@pytest.mark.parametrize("nbytes", [40, 48, 56], ids=["56-chars", "64-chars", "76-chars"])
def test_a_genuine_shaped_aws_id_stays_readable(table_on, build, nbytes):
    """Random whole-byte base64 of the lengths AWS issues stays visible."""
    import base64
    import random

    rng = random.Random(nbytes)
    for _ in range(200):
        value = base64.b64encode(rng.randbytes(nbytes)).decode()
        text = build(value)
        assert redact_credentials(text)[0] == text


def test_a_secret_glued_into_a_sha256_fingerprint_slot_is_redacted(table_on):
    """``SHA256:`` + a 40-character key + three characters fills the 43-character slot.
    A real fingerprint is 32 bytes, so its last character carries two zero bits; a
    value whose last character does not is not one, and the key in it is removed."""
    secret = aws_secret(seed=72)
    text = f"SHA256:{secret}AbC host"
    assert secret not in redact_credentials(text)[0]
    assert secret not in _streamed(text)


def test_a_genuine_sha256_fingerprint_stays_readable(table_on):
    import base64
    import random

    rng = random.Random(256)
    for _ in range(300):
        fp = base64.b64encode(rng.randbytes(32)).decode().rstrip("=")
        text = f"ED25519 key SHA256:{fp} host"
        assert redact_credentials(text)[0] == text


@pytest.mark.parametrize(
    "build",
    [
        lambda t: f"x-amz-id-2: {t}",
        lambda t: f'{{"HostId": "{t}"}}',
        lambda t: f"<HostId>{t}</HostId>",
    ],
    ids=["x-amz-id-2", "hostid-json", "hostid-xml"],
)
@pytest.mark.parametrize("make", [_itsdangerous_cookie, _alg_none_jws], ids=["cookie", "alg-none"])
def test_a_refused_token_in_an_identifier_slot_is_redacted(table_on, make, build):
    """An Identifier mask over a signed token pass 1 still reports does not hide it."""
    token = make()
    text = build(token)
    assert not _leaks(token, redact_credentials(text)[0])
    assert not _leaks(token, _streamed(text))


@pytest.mark.parametrize("size", [7, 64, 1000])
def test_a_jwt_longer_than_the_detectors_read_streams_redacted(table_on, size):
    token = _long_jwt()
    text = f"id_token {token} end"
    stream = StreamRedactor()
    out = "".join(stream.feed(text[i : i + size]) for i in range(0, len(text), size))
    out += stream.flush()
    assert not _leaks(token, out)


def _sd_jwt() -> str:
    """A selective-disclosure JWT: a JWS, then a disclosure only a Bearer label claims."""
    return _signed_jwt() + "~WyJzYWx0IiwgImVtYWlsIiwgImFsaWNlIl0~"


@pytest.mark.parametrize("glue", [" ", "\n"])
def test_a_bearer_value_a_detector_hit_starts_is_redacted_whole(table_on, glue):
    """A JWS the detectors replace, with a disclosure after it: the label's whole value
    is removed, as with the switch off, not just the JWS."""
    token = _sd_jwt()
    text = f"Calling with Authorization: Bearer{glue}{token} now"
    assert redact_credentials(text)[0] == f"Calling with Authorization: Bearer{glue}{TAG} now"
    assert "WyJzYWx0" not in _streamed(text)


@pytest.mark.parametrize("glue", ["-", "."])
def test_a_token_value_a_detector_hit_starts_is_redacted_whole(table_on, glue):
    """The parts of a ``token=`` value a detector hit leaves are redacted too."""
    tail = glue + synth(21, seed=33)
    out, _ = redact_credentials(f"x?token={github_classic(seed=32)}{tail} next")
    assert tail[1:] not in out
    assert out.endswith(" next")


@pytest.mark.parametrize(
    "build",
    [
        lambda a, b: f"see https://h.example/p?token=abcDEF12.{a}.tailXYZ end",
        lambda a, b: f"x?token=aa11.{a}.mid22.{b}.zz end",
        lambda a, b: f"x?token={a}-{synth(21, seed=34)} next",
    ],
    ids=["hit-mid-value", "two-hits", "hit-at-start"],
)
def test_a_token_value_a_hit_touches_matches_the_switch_off_plan(switch, build):
    """Text, warnings and records equal the plan without the detectors."""
    from kiro_crew.security.redaction import redact_credentials_with_records

    text = build(github_classic(seed=35), github_classic(seed=36))
    off = redact_credentials_with_records(text)
    compat.publish_use_rule_table(True)
    on = redact_credentials_with_records(text)
    assert on[0] == off[0]
    assert on[1] == off[1]
    assert [(m.rule, m.value) for m in on[2]] == [(m.rule, m.value) for m in off[2]]


def test_a_client_secret_past_the_url_cap_is_still_removed(table_on):
    """A URL longer than the detectors' URL cap: main's own passes still remove the value."""
    secret = synth(40, seed=25)
    url = "https://auth.example.test/cb?pad=" + "a" * 5000 + "&client_" + "secret=" + secret
    assert secret not in redact(f"go {url} done")


def test_residual_matches_drop_only_the_covered_rules(table_on):
    from kiro_crew.security import redaction

    pem = (
        "-----BEGIN RSA "
        + "PRIVATE KEY-----\n"
        + synth(64, B64, 26)
        + "\n-----END RSA "
        + "PRIVATE KEY-----"
    )
    text = f"{pem}\n{github_classic(seed=27)}\nAccessKeyId: {compat.PLACEHOLDER * 20}"
    rules = {redaction._pass1_rule(m.group())[0] for m in compat.residual_matches(text)}
    assert rules == {"github_token"}


def test_switch_off_pass1_reads_every_branch(switch):
    from kiro_crew.security import redaction

    text = (
        "-----BEGIN RSA "
        + "PRIVATE KEY-----\n"
        + synth(64, B64, 28)
        + "\n-----END RSA "
        + "PRIVATE KEY-----"
    )
    assert [m.span() for m in redaction._pass1_matches(text)] == [
        m.span() for m in redaction._credential_matches(text)
    ]


# ── protected Identifier values ─────────────────────────────────────────────


def test_a_protected_value_is_kept_from_the_later_passes(table_on):
    """An AWS request id run is protected; a pass 3 bare-secret run next to it is not."""
    key_id = aws_key_id(seed=29)
    text = f"key {key_id} with secret {aws_secret(seed=30)} end"
    masks = compat.protected_values(text)
    assert [text[s:e] for s, e, _r in masks] == [key_id]
    out, _ = redact_credentials(text)
    assert key_id in out and aws_secret(seed=30) not in out


@pytest.mark.parametrize("tail", ["/{key}", "/{key}.txt", "/kirocrew-computer-shots/{key}"])
def test_a_secret_used_as_a_file_name_in_a_macos_temp_dir_is_redacted(table_on, tail):
    """Only ``/var/folders/<b>/<id>/T`` is protected; a key below it is still removed."""
    root = "/var/folders/6r/" + synth(28, "abcdefghijklmnopqrstuvwxyz0123456789_", 34) + "/T"
    key = aws_secret(seed=35)
    out = redact(root + tail.format(key=key))
    assert key not in out
    assert out.startswith(root)


@pytest.mark.parametrize("seed", [77, 81])
@pytest.mark.parametrize("bucket", ["6r", "zz"])
def test_a_bare_secret_typed_as_a_macos_temp_dir_id_is_removed(table_on, bucket, seed):
    """A 40-character key in the per-user directory's place is a key, not a temp id.

    Real ids are 28 to 30 characters with underscores, so none of them reads as a
    bare secret and every genuine temp directory stays visible.
    """
    secret = aws_secret(seed=seed)
    text = f"see /var/folders/{bucket}/{secret}/T/out.log"
    assert secret not in redact_credentials(text)[0]
    assert secret not in _streamed(text)
    genuine = (
        f"/var/folders/{bucket}/" + synth(28, "abcdefghijklmnopqrstuvwxyz0123456789_", 34) + "/T"
    )
    assert redact_credentials(f"see {genuine}/out.log")[0] == f"see {genuine}/out.log"


@pytest.mark.parametrize("leaf", ["T", "C", "0"])
@pytest.mark.parametrize("root", ["/private/var/folders", "/var/folders"])
def test_a_bare_secret_as_the_temp_dir_id_is_removed_under_every_prefix(table_on, root, leaf):
    """``/private/var/folders/...`` is the same directory as ``/var/folders/...``; the id is
    judged under either prefix and for every leaf, and a genuine id stays visible. The key
    is letters and digits only, the shape the temp-path row reads as an id."""
    secret = synth(40, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789", 9)
    text = f"see {root}/zz/{secret}/{leaf}/out.log"
    assert secret not in redact_credentials(text)[0]
    assert secret not in _streamed(text)
    genuine = f"{root}/zz/" + synth(28, "abcdefghijklmnopqrstuvwxyz0123456789_", 36) + f"/{leaf}"
    assert redact_credentials(f"see {genuine}/out.log")[0] == f"see {genuine}/out.log"


def _encoded_temp_id() -> tuple[str, str]:
    """A GitHub token and its base64, the encoding written with letters and digits
    only so the temp-path row reads it as an id (``+`` and ``/`` would end it)."""
    for seed in range(1, 500):
        token = github_classic(seed=seed)
        encoded = base64.b64encode(f"t={token}".encode()).decode()
        if encoded.isalnum():
            return token, encoded
    raise AssertionError("no seed gives an alphanumeric encoding")


@pytest.mark.parametrize("root", ["/private/var/folders", "/var/folders"])
def test_an_encoded_secret_as_the_temp_dir_id_is_removed(table_on, root):
    """A base64-encoded credential in the id's place is decoded and removed, as the
    legacy passes remove it; the run from ``/var`` decodes to nothing, so the id is
    decoded on its own."""
    _token, encoded = _encoded_temp_id()
    text = f"see {root}/zz/{encoded}/T/out.log"
    assert encoded not in redact_credentials(text)[0]
    assert encoded not in _streamed(text)


def test_placeholders_never_reach_the_output(table_on):
    key_id = aws_key_id(seed=31)
    out = redact(f"AccessKeyId={key_id}&token={synth(30, seed=32)}")
    assert compat.PLACEHOLDER not in out and key_id in out


def test_a_pass1_warning_gives_the_original_length(table_on):
    """A pass-1 match that reaches into a Secret hit's tag reports the original span."""
    secret = github_classic(seed=33)
    text = f"https://user:{secret}@host.example.test/x"
    spans, warnings, _rules = compat.credential_plan_via_table(text)
    lengths = sorted(int(w.split("(")[1].split()[0]) for w in warnings if "pattern" in w)
    assert lengths[-1] == max(e - s for s, e, _t in spans)


# ── display caches ──────────────────────────────────────────────────────────


@pytest.fixture
def engine_battery(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, int]]:
    """A credential pass that redacts ``WORD`` only while the detectors run; counts calls."""
    calls = {"n": 0}
    real = display_battery.redact_credentials

    def by_engine(text: str, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if compat.use_rule_table():
            text = text.replace(WORD, "[REDACTED]")
        return real(text, **kwargs)

    monkeypatch.setattr(display_battery, "redact_credentials", by_engine)
    chat_utils._clear_display_redaction_cache()
    yield calls
    chat_utils._clear_display_redaction_cache()


def test_a_switch_change_is_not_served_from_the_process_cache(switch, engine_battery):
    """Input ``"see codeword-6612 now"``; off it is kept, on it is redacted, off kept again."""
    text = f"see {WORD} now"
    dv.disable_for_tests()
    assert chat_utils._redact_for_display(text) == text
    compat.publish_use_rule_table(True)
    assert chat_utils._redact_for_display(text) == "see [REDACTED] now"
    compat.publish_use_rule_table(False)
    assert chat_utils._redact_for_display(text) == text


def test_a_switch_change_is_not_served_from_the_preview_caches(switch, engine_battery):
    """The same string object previewed under each engine gets each engine's preview."""
    text = f"see {WORD} now"
    assert chat_utils._redacted_preview(text, 100) == text
    compat.publish_use_rule_table(True)
    assert chat_utils._redacted_preview(text, 100) == "see [REDACTED] now"
    compat.publish_use_rule_table(False)
    assert chat_utils._redacted_preview(text, 100) == text


def test_a_switch_moved_while_the_battery_runs_caches_nothing(switch, engine_battery, monkeypatch):
    """The answer computed while the switch moved is returned but neither cache keeps it."""
    text = f"see {WORD} now"
    real = display_battery.redact_credentials

    def flips(t: str, **kwargs):  # type: ignore[no-untyped-def]
        out = real(t, **kwargs)
        compat.publish_use_rule_table(True)
        return out

    monkeypatch.setattr(display_battery, "redact_credentials", flips)
    assert chat_utils._redact_for_display(text) == text
    monkeypatch.setattr(display_battery, "redact_credentials", real)
    assert chat_utils._redact_for_display(text) == "see [REDACTED] now"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "sessions").mkdir(parents=True)
    (h / "sessions" / f"{KEY}.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("KIROCREW_HOME", str(h))
    monkeypatch.setattr(dv, "rules_version", lambda: b"v" * 16)
    monkeypatch.setattr(dv, "LOAD_AFTER_MISSES", 0)
    dv.disable_for_tests()
    yield h
    dv.disable_for_tests()
    with dv._written_guard:
        dv._written.clear()


def _render(content: str) -> str:
    rows = [{"role": "assistant", "content": content, "cls": "msg msg-a"}]
    with dv.session(KEY):
        out = _prepare_messages(rows, running=False, live_child="", workspace="ws")
    return json.dumps(out)


@pytest.mark.skipif(os.name == "nt", reason="stored verdicts are POSIX-only")
def test_a_stored_verdict_names_the_engine_that_judged_it(
    switch, engine_battery, home: Path, monkeypatch
):
    """Rendered once with the switch off (stored clean), then on in the same process and
    after a restart: ``codeword-6612`` is redacted both times; off again it is kept."""
    monkeypatch.setattr(chat_utils, "allowed_hosts_for", lambda _ws: frozenset())
    assert dv.enable(home)
    content = f"plain words with {WORD} inside"
    assert WORD in _render(content)
    compat.publish_use_rule_table(True)
    assert WORD not in _render(content)
    dv.disable_for_tests()
    chat_utils._clear_display_redaction_cache()
    assert dv.enable(home)
    assert WORD not in _render(content)
    compat.publish_use_rule_table(False)
    assert WORD in _render(content)


def test_the_engine_mark_is_empty_with_the_switch_off(switch):
    """Off, a cache key is the key it always was; on, it carries a mark."""
    assert dv.engine_mark() == b""
    compat.publish_use_rule_table(True)
    assert dv.engine_mark() != b""


# ── recorded frames ─────────────────────────────────────────────────────────


@pytest.fixture
def frames(monkeypatch):
    _frame_record._reset_for_tests()
    monkeypatch.delenv(_frame_record.ENV_RECORD_FRAMES, raising=False)


def test_a_field_name_key_under_a_credential_key_stays_legible(table_on, frames):
    """``{"Authorization": {"value": "Basic dXNlcjpwYXNz"}}`` keeps the ``value`` key."""
    out = json.loads(_frame_record.scrub_frame({"Authorization": {"value": "Basic dXNlcjpwYXNz"}}))
    assert out == {"Authorization": {"value": TAG}}


def test_a_non_field_key_under_a_credential_key_is_still_replaced(table_on, frames):
    """``{"Authorization": {"Basic dXNlcjpwYXNz": true}}``: the key is the secret."""
    out = json.loads(_frame_record.scrub_frame({"Authorization": {"Basic dXNlcjpwYXNz": True}}))
    assert out == {"Authorization": {TAG: TAG}}


@pytest.mark.parametrize("ancestor", ["Authorization", "Proxy-Authorization", "X-Authorization"])
def test_recorded_authorization_field_names_remain_legible(table_on, frames, ancestor):
    out = json.loads(_frame_record.scrub_frame({ancestor: {"value": "opaque-example"}}))
    assert out == {ancestor: {"value": TAG}}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Authorization", True),
        ("proxy-authorization", True),
        ("X_Authorization", True),
        ("authorizations", False),
        ("password", False),
    ],
)
def test_names_authorization(name, expected):
    assert _frame_record._names_authorization(name) is expected
