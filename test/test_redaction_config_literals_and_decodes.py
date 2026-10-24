"""Redaction of config-line literals, plain words behind strong labels, PEM
bodies in the stream, keys inside URLs, and text inside binary decodes.

Split from ``test_redaction_layer_and_stream_reach`` at the file line cap."""

from __future__ import annotations

import json
import random
import re
import string

import pytest

from kiro_crew.log_redaction import SecretRedactionFilter
from kiro_crew.security.redaction import redact_credentials
from kiro_crew.security.text_redaction import StreamRedactor


def _redact(text: str) -> str:
    return redact_credentials(text)[0]


def _stream(text: str, size: int) -> str:
    redactor = StreamRedactor()
    out = "".join(redactor.feed(text[i : i + size]) for i in range(0, len(text), size))
    return out + redactor.flush()


# ── F1/F3 on 457b4ddc96: plain words behind strong labels, config-line literals ──


@pytest.mark.parametrize(
    "text",
    [
        "x password: hunter",  # a short plain word
        "x password: secretpass",  # a lower-case word of 8-15 letters
        "x api_key: Abc1234",  # a short mixed value
        "x secret= ok",
        "check(password=letmein)",
    ],
)
def test_a_plain_word_behind_a_strong_label_is_redacted(text: str) -> None:
    value = re.split(r"[ =]", text.rstrip(")"))[-1]
    for out in (_redact(text), _stream(text, 1), SecretRedactionFilter([]).redact(text)):
        assert value not in out


@pytest.mark.parametrize(
    "text",
    [
        "password = new_password",  # a snake_case identifier
        "token = get_token()",  # a call
        "login(password=load_key())",
        "x secret = settings.db_password",  # a dotted name
        "x token: os.environ['TOKEN']",  # an indexed read
        "x api_key = userApiKey",  # a camelCase identifier
        "x password=${DB_PASSWORD}",  # a variable reference
    ],
)
def test_visibly_code_behind_a_strong_label_is_kept(text: str) -> None:
    assert _redact(text) == text
    assert SecretRedactionFilter([]).redact(text) == text
    assert _stream(text, 1) == text


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("PASSWORD=hunter2\n", "hunter2"),
        ("password=letmein\n", "letmein"),
        ("DB_PASS=a,b;c\n", "a,b;c"),
        ("DB_PASSWORD=new_password\n", "new_password"),  # no word or code exemption
        ("password: secretpass\n", "secretpass"),
        ("secret: settings.db_secret\n", "settings.db_secret"),
        ("export API_KEY=a,b;cdefgh # rotate\n", "a,b;cdefgh"),
        ("  password: two words, one;semi\nnext: x\n", "two words, one;semi"),
        ("PASSWORD=pw,with;punct&next=1 tail\n", "pw,with;punct&next=1 tail"),
    ],
)
@pytest.mark.parametrize("size", [1, 7, 64])
def test_a_config_line_value_is_a_literal_read_to_its_line_end(
    text: str, value: str, size: int
) -> None:
    batch = _redact(text)
    assert _stream(text, size) == batch
    for out in (batch, SecretRedactionFilter([]).redact(text)):
        for piece in re.split(r"[,;& ]", value):
            if len(piece) > 1:
                assert piece not in out
    if "# rotate" in text:
        assert batch.endswith(" # rotate\n")
    if "next: x" in text:
        assert batch.endswith("\nnext: x\n")


@pytest.mark.parametrize(
    "text",
    [
        "DB_PASSWORD=${DB_PASSWORD}\n",
        "DB_PASSWORD=$PW\n",
        "api_key: ${API_KEY}\n",
        "SECRET_ARN=arn:aws:secretsmanager:us-east-1:123456789012:secret:x\n",
        "PWD=/workplace/project\n",  # the shell's working directory, not a password
    ],
)
def test_a_config_line_keeps_only_an_exact_reference_or_an_identifier(text: str) -> None:
    assert _redact(text) == text
    assert SecretRedactionFilter([]).redact(text) == text
    assert _stream(text, 1) == text


# ── Round on b1228c3ff9: PEM bodies in the stream, keys in URLs, binary decodes ──


def _pem(nbytes: int, kind: str = "RSA PRIVATE KEY") -> tuple[str, list[str]]:
    import base64

    rng = random.Random(nbytes)
    body = base64.b64encode(bytes(rng.randrange(256) for _ in range(nbytes))).decode()
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    return f"-----BEGIN {kind}-----\n" + "\n".join(lines) + f"\n-----END {kind}-----", lines


@pytest.mark.parametrize("nbytes", [1190, 4800, 9000])  # 2048-bit, larger, past the reach
@pytest.mark.parametrize("tail", ["\ndone\n", ""])
@pytest.mark.parametrize("size", [1, 7, 64, 500])
def test_a_pem_private_key_never_streams_its_body(nbytes: int, tail: str, size: int) -> None:
    pem, lines = _pem(nbytes)
    text = "Here is the key:\n" + pem + tail
    out = _stream(text, size)
    assert not [line for line in lines if line in out]
    assert out == _redact(text)


@pytest.mark.parametrize(
    "text",
    [
        "https://example.com/download?k=wJalrXUtnFEMI/K7MDENG/bPxRfiCYzzAMPLEKEY",
        "https://x.com/wJalrXUtnFEMI/K7MDENG/bPxRfiCYzzAMPLEKEY end",
    ],
)
def test_the_bundle_gate_refuses_a_key_with_slashes_inside_a_url(text: str) -> None:
    from kiro_crew.apps.builtins.aws_control.crew.packaging.pipeline import scan

    assert "bare-secret" in [leak.kind for leak in scan.scan_text(text, "skill")]


def test_a_binary_decode_is_read_for_fixed_patterns_only(monkeypatch: pytest.MonkeyPatch) -> None:
    import base64

    from kiro_crew.security import redaction as facade

    rng = random.Random(2)
    blob = base64.b64encode(bytes(rng.randrange(256) for _ in range(30_000))).decode()

    real_views = facade.normalised_views

    def _no_views(text: str) -> list[object]:
        # Only the decode's short text runs may be read, never the binary whole.
        assert len(text) < 1024, "a binary decode built its normalised views"
        return real_views(text)

    with monkeypatch.context() as mp:
        mp.setattr(facade, "normalised_views", _no_views)
        assert _redact(f"logo {blob} end") == f"logo {blob} end"
    # Text decodes are still read through their views and context rules.
    secret = base64.b64encode(b'{"password": "Xq7m2pzRtvB8nc1k"}').decode()
    assert secret not in _redact(f"cfg {secret} end")


# ── Round on 6e2be1afa7: non-ASCII decoded text, a config value equal to its label ──


@pytest.mark.parametrize("password", ["пароль12", "密码密码密码12", "contraseña1"])
def test_an_encoded_non_ascii_password_is_text_not_binary(password: str) -> None:
    import base64

    blob = base64.b64encode(json.dumps({"password": password}, ensure_ascii=False).encode())
    text = f"payload {blob.decode()} end"
    for out in (_redact(text), _stream(text, 7)):
        assert blob.decode() not in out


@pytest.mark.parametrize("text", ["password: password\n", "  password: Password\n"])
@pytest.mark.parametrize("size", [1, 64])
def test_a_config_value_equal_to_its_label_is_redacted(text: str, size: int) -> None:
    for out in (_redact(text), _stream(text, size), SecretRedactionFilter([]).redact(text)):
        assert out.rstrip().endswith("]")


def test_a_caption_in_markup_still_stays() -> None:
    text = "<label>Password:</label>\n"
    assert _redact(text) == text == _stream(text, 1)


# ── Round on af65c03a3e: literal data values, text inside binary, HTML separators ──


@pytest.mark.parametrize(
    "text",
    [
        "<config><password>correct_horse_battery</password></config>\n",
        "- name: DB_PASSWORD\n  value: correct_horse_battery\n",
        '{"name": "API_SECRET", "value": "settings.key"}\n',
    ],
)
def test_xml_content_and_sibling_values_are_literals(text: str) -> None:
    value = "correct_horse_battery" if "correct" in text else "settings.key"
    for out in (_redact(text), _stream(text, 1)):
        assert value not in out
    if text.startswith("<config>"):
        assert value not in SecretRedactionFilter([]).redact(text)


def test_a_reference_in_xml_content_is_still_kept() -> None:
    text = "<password>${DB_PW}</password>\n"
    assert _redact(text) == text


def _png(text_chunk: bytes) -> bytes:
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    rng = random.Random(1)
    noise = bytes(rng.randrange(256) for _ in range(5000))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", b"\x00" * 13)
        + chunk(b"tEXt", text_chunk)
        + chunk(b"IDAT", noise)
    )


def test_a_labelled_secret_in_a_binary_decodes_text_chunk_is_found() -> None:
    import base64

    secret = "wJalrXUtnFEMI/K7MDENG/" "bPxRfiCYEXAMPLEKEY"
    body = base64.b64encode(_png(b"Comment\x00aws_secret_access_key=" + secret.encode())).decode()
    assert body not in _redact(f'<img src="data:image/png;base64,{body}">')


def test_utf16_encoded_text_is_read_as_text() -> None:
    import base64

    secret = "wJalrXUtnFEMI/K7MDENG/" "bPxRfiCYEXAMPLEKEY"
    command = f"$env:AWS_SECRET_ACCESS_KEY='{secret}'; aws s3 ls"
    blob = base64.b64encode(command.encode("utf-16-le")).decode()
    assert blob not in _redact(f"powershell -EncodedCommand {blob}")


def test_an_ordinary_bitmap_body_is_not_condemned_by_chance_vendor_shapes() -> None:
    import base64

    from kiro_crew.apps.builtins.pptx_maker.backend import routes

    # A body that happens to hold a vendor-rule shape after a ``+`` or ``/``.
    head = base64.b64encode(_png(b"Comment\x00plain")[:48]).decode()
    body = head + "Yk+BSAnADr2kmhQ7Lp3VxZcR8wTbN4yF" + "A" * 64
    assert routes._scanned_bitmap_bytes(body)[1] is False


@pytest.mark.parametrize(
    "link",
    [
        "https://a.blob.core.windows.net/c?sv=1&amp;s%69g=SECRETSIGVALUE123&amp;se=2",
        "https://a/c?sv=1&#38;sig=SECRETSIGVALUE123",
        "https://a/c?sv=1&#x26;sig=SECRETSIGVALUE123",
    ],
)
def test_an_html_escaped_query_separator_still_reads_sig(link: str) -> None:
    assert "SECRETSIGVALUE123" not in SecretRedactionFilter([]).redact(link)


# ── Round on 14f0ff400e: vendor context for views and decodes, null literals, slash runs ──


@pytest.mark.parametrize("encode", ["percent", "plain"])
def test_a_vendor_keyword_far_before_an_encoded_token_still_selects_the_rule(encode: str) -> None:
    from urllib.parse import quote

    rng = random.Random(9)
    token = (
        "pat"
        + "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(14))
        + "."
        + "".join(rng.choice("0123456789abcdef") for _ in range(64))
    )
    body = quote(f'"{token}"') if encode == "percent" else token
    text = "# Airtable\n" + "Lorem ipsum dolor sit amet. " * 300 + "\n" + body + "\n"
    assert token[3:20] not in _redact(text)


@pytest.mark.parametrize(
    "text",
    [
        "<script>const password = null;</script>",
        '{"password": null, "token": false}',
        "x password = None",
    ],
)
def test_a_null_or_boolean_literal_behind_a_label_is_kept(text: str) -> None:
    assert _redact(text) == text


@pytest.mark.parametrize("text", ["PASSWORD=false\n", "password: null\n", "x password = nullish"])
def test_a_config_literal_or_a_word_like_null_is_still_redacted(text: str) -> None:
    assert _redact(text) != text


def test_a_url_run_of_only_slashes_does_not_crash_the_bundle_gate() -> None:
    from kiro_crew.apps.builtins.aws_control.crew.packaging.pipeline import scan

    assert scan.scan_text("see https://x.test/?q=" + "/" * 45 + " ok", "prompt") == []


# ── Round on be09090e68: YAML doubled quotes, URL query values ──


@pytest.mark.parametrize(
    "text",
    [
        "password: 'it''s-Secret9xyz'\nnext: 1\n",
        "db:\n  password: 'a''b''cdefgh9'\n",
        "x password: 'it''s" + "Q7w" * 1700 + "''tail9Secret' and more\n",
    ],
)
@pytest.mark.parametrize("size", [1, 7, 64])
def test_a_yaml_doubled_quote_does_not_end_a_password(text: str, size: int) -> None:
    pieces = ("Secret9xyz", "cdefgh9", "tail9Secret", "Q7wQ7wQ7w")
    for out in (_redact(text), _stream(text, size), SecretRedactionFilter([]).redact(text)):
        assert not [p for p in pieces if p in out]


def test_an_empty_single_quoted_value_still_closes_at_once() -> None:
    text = "x password='' and y=1"
    assert _redact(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "GET /login?user=a&password=correct_horse_battery&x=1",
        "https://h/p?password=correct_horse_battery",
        "https://h/p?a=1&amp;api_key=userApiKey",
    ],
)
def test_a_url_query_value_is_a_literal(text: str) -> None:
    value = "correct_horse_battery" if "horse" in text else "userApiKey"
    for out in (_redact(text), _stream(text, 3), SecretRedactionFilter([]).redact(text)):
        assert value not in out
    if "&x=1" in text:
        assert _redact(text).endswith("&x=1")


# ── Round on 67f70511c7: a config value that starts with a delimiter ──


@pytest.mark.parametrize(
    "text",
    [
        "DB_PASSWORD=;Xq7m2pz9Rtv\n",
        "password=[Xq7m2pz9Rtv\n",
        "API_KEY=\\Xq7m2pz9Rtv\n",
        "password: {abc}Xq7m2pz9Rtv\n",
        "db_password: ;Xq7m2pz9Rtv # note\n",
        "export API_TOKEN=&Xq7m2pz9Rtv\n",
        "- secret: (Xq7m2pz9Rtv\n",
        "PASSWORD=<hidden Xq7m2pz9Rtv>\n",  # not the digest mask's exact shape
    ],
)
@pytest.mark.parametrize("size", [1, 7, 64])
def test_a_config_value_led_by_a_delimiter_is_redacted(text: str, size: int) -> None:
    for out in (_redact(text), _stream(text, size), SecretRedactionFilter([]).redact(text)):
        assert "Xq7m2pz9" not in out
    if "# note" in text:
        assert _redact(text).endswith(" # note\n")


@pytest.mark.parametrize(
    "text",
    [
        "PASSWORD=[REDACTED: credential]\n",  # already a tag: never matched again
        "API_TOKEN=<hidden sha256:0123abcd>\n",  # the MCP display's digest mask
        "f(password, token)\n",
        "x password = get_token()\n",
    ],
)
def test_a_tag_or_code_after_a_label_is_unchanged(text: str) -> None:
    for out in (_redact(text), _stream(text, 1), SecretRedactionFilter([]).redact(text)):
        assert out == text
