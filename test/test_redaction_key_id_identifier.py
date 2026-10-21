"""An AWS access key ID is Identifier tier; a signed link is Bearer link tier.

A key ID (``AKIA`` / ``ASIA``) names a key and cannot sign a request, so no
surface removes it. The secret access key and session token beside it are Secret
tier and still go. A presigned or signed URL works by itself until it expires, so
its signing part goes while the key ID inside it stays.

Fake values only: the AWS documentation example key pair, and made-up tokens.
"""

from __future__ import annotations

import pytest

from kiro_crew.log_redaction import SecretRedactionFilter
from kiro_crew.metrics import schema as metrics_schema
from kiro_crew.security import StreamRedactor, redact, redact_credentials
from kiro_crew.security.redaction import redact_credentials_with_records

KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"
STS_KEY_ID = "ASIA" + "IOSFODNN7EXAMPLE"
SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
TOKEN = "IQoJb3JpZ2luX2VjE" + "Q2hp" * 30
SIGNATURE = "0123456789abcdef" * 4
TAG = "[REDACTED: credential]"

PRESIGNED = (
    "https://bucket.s3.amazonaws.com/obj.txt?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    f"&X-Amz-Credential={STS_KEY_ID}%2F20260101%2Fus-east-1%2Fs3%2Faws4_request"
    "&X-Amz-Date=20260101T000000Z&X-Amz-Expires=3600&X-Amz-SignedHeaders=host"
    f"&X-Amz-Security-Token={TOKEN}&X-Amz-Signature={SIGNATURE}"
)


@pytest.mark.parametrize(
    "text",
    [
        f"key {KEY_ID} used",
        f'{{"userIdentity": {{"accessKeyId": "{STS_KEY_ID}", "type": "AssumedRole"}}}}',
        f"AWS_ACCESS_KEY_ID={KEY_ID}",
        f"aws_access_key_id = {KEY_ID}",
        f'{{"AccessKeyId": "{STS_KEY_ID}"}}',
        f"https://example.test/lookup?id={KEY_ID}",
    ],
    ids=["bare", "cloudtrail-row", "env", "credentials-file", "sts-json", "in-a-url"],
)
def test_a_key_id_alone_is_never_redacted(text: str) -> None:
    assert redact(text) == text
    assert redact_credentials(text) == (text, [])


def test_a_key_id_beside_its_secret_keeps_the_id_and_loses_the_secret() -> None:
    text = (
        f"[default]\naws_access_key_id = {KEY_ID}\naws_secret_access_key = {SECRET}\n"
        f"aws_session_token = {TOKEN}\n"
    )
    out = redact(text)
    assert KEY_ID in out
    assert SECRET not in out and TOKEN not in out


def test_sts_json_keeps_the_id_and_loses_the_secret_and_token() -> None:
    text = (
        f'{{"AccessKeyId": "{STS_KEY_ID}", "SecretAccessKey": "{SECRET}", '
        f'"SessionToken": "{TOKEN}"}}'
    )
    out = redact(text)
    assert f'"AccessKeyId": "{STS_KEY_ID}"' in out
    assert SECRET not in out and TOKEN not in out


def test_a_presigned_url_loses_its_signature_and_session_token() -> None:
    out = redact(f"get {PRESIGNED} now")
    assert SIGNATURE not in out
    assert TOKEN not in out


def test_the_signing_values_are_redacted_and_the_key_id_kept() -> None:
    out, _warnings, records = redact_credentials_with_records(PRESIGNED)
    assert f"X-Amz-Credential={STS_KEY_ID}%2F" in out
    assert f"X-Amz-Signature={TAG}" in out
    assert f"X-Amz-Security-Token={TAG}" in out
    assert {r.rule for r in records} == {"request_signature", "aws_session_token"}


@pytest.mark.parametrize(
    "url",
    [
        f"https://bucket.s3.amazonaws.com/o?AWSAccessKeyId={KEY_ID}&Expires=1700000000"
        "&Signature=abcDEF123%2Bxyz%3D",
        "https://d111111abcdef8.cloudfront.net/image.jpg?Expires=1700000000"
        "&Signature=Nql641NHEUkUa~X-Ab_cd&Key-Pair-Id=APKAEXAMPLE",
        "https://h.example/o?x-amz-signature=" + SIGNATURE,
    ],
    ids=["sigv2", "cloudfront", "lowercase-name"],
)
def test_a_signed_link_never_keeps_its_signature(url: str) -> None:
    signature = url.rsplit("ignature=", 1)[1].split("&", 1)[0]
    assert signature not in redact(url)
    # The credential pass alone keeps the parameter name and drops the value.
    assert "ignature=" + TAG in redact_credentials(url)[0]


def test_a_sigv4_authorization_header_loses_its_signature() -> None:
    header = (
        f"Authorization: AWS4-HMAC-SHA256 Credential={KEY_ID}/20260101/us-east-1/s3/"
        f"aws4_request, SignedHeaders=host;x-amz-date, Signature={SIGNATURE}"
    )
    out = redact(header)
    assert f"Credential={KEY_ID}/" in out
    assert SIGNATURE not in out
    assert f"Signature={TAG}" in out


@pytest.mark.parametrize("where", ["Signature", "Signature=", "Security-To", "X-Amz-Sig"])
def test_a_stream_split_inside_a_presigned_url_streams_no_signing_value(where: str) -> None:
    text = f"see {PRESIGNED} ok"
    cut = text.index(where) + len(where) // 2
    redactor = StreamRedactor()
    wire = redactor.feed(text[:cut]) + redactor.feed(text[cut:]) + redactor.flush()
    assert SIGNATURE not in wire
    assert TOKEN[:24] not in wire


def test_a_streamed_access_key_id_label_keeps_its_value() -> None:
    """`AccessKeyId` is not a secret label, streamed or batch."""
    text = f'{{"AccessKeyId": "{STS_KEY_ID}"}} next'
    redactor = StreamRedactor()
    cut = text.index(STS_KEY_ID) + 6
    wire = redactor.feed(text[:cut]) + redactor.feed(text[cut:]) + redactor.flush()
    assert wire == text


@pytest.mark.parametrize(
    "text",
    [
        f"authorization: aws4-hmac-sha256 credential={KEY_ID}/x, signature={SIGNATURE}",
        f"Authorization: AWS4-HMAC-SHA256 Credential={KEY_ID}/x, Signature={SIGNATURE.upper()}",
        PRESIGNED,
        f"https://b.s3.amazonaws.com/k?AWSAccessKeyId={KEY_ID}&Expires=1790000000&Signature=Zm9vYmFy=",
    ],
    ids=["sigv4-header-lower", "sigv4-header-upper", "presigned", "sigv2"],
)
def test_the_log_floor_strips_a_signing_value_but_keeps_the_key_id(text: str) -> None:
    """A signed link in a CLI log keeps its key ID but loses the signing value."""
    out = SecretRedactionFilter([]).redact(text)
    assert SIGNATURE not in out and SIGNATURE.upper() not in out
    assert "Zm9vYmFy" not in out
    assert KEY_ID in out or STS_KEY_ID in out


def test_the_deploy_scan_hard_blocks_a_signed_link() -> None:
    """Publishing a presigned link is a hard credential finding, not a warning."""
    from kiro_crew.deploy.scan import scan_content

    findings = scan_content(f"see {PRESIGNED}")
    assert any(f.severity == "credential" for f in findings)


def test_the_log_floor_keeps_a_key_id_and_still_drops_unconfirmed_ids() -> None:
    log = SecretRedactionFilter([])
    assert log.redact(f"key {KEY_ID}") == f"key {KEY_ID}"
    body = "IOSFODNN7EXAMPLE"
    assert f"ABIA{body}" not in log.redact(f"key ABIA{body}")


def test_metrics_keep_a_key_id() -> None:
    assert metrics_schema.redact(KEY_ID) == KEY_ID


@pytest.mark.parametrize(
    "text",
    [
        # A percent-escaped parameter name the named matchers miss (%53 is "S"),
        # on a SigV4 link marked by X-Amz-Credential.
        f"req X-Amz-Credential={KEY_ID}%2Fx X-Amz-%53ignature={SIGNATURE}",
        # The same obfuscation on a SigV2 link marked by AWSAccessKeyId, whose
        # signature is base64 rather than 64 hex.
        f"get ?AWSAccessKeyId={KEY_ID}&Expires=1790000000&%53ignature=Zm9vYmFybWFj",
    ],
    ids=["sigv4-escaped-name", "sigv2-escaped-name"],
)
def test_the_log_floor_strips_a_signing_value_under_an_escaped_name(text: str) -> None:
    """An encoded ``%53ignature`` parameter name still loses its value, so a
    hand-edited signed link in a CLI log cannot be replayed. The key ID stays."""
    out = SecretRedactionFilter([]).redact(text)
    assert SIGNATURE not in out
    assert "Zm9vYmFybWFj" not in out
    assert KEY_ID in out


@pytest.mark.parametrize(
    "text",
    [
        "def verify(signature: bytes) -> None: ...",
        "signature = inspect.signature(callback)",
        "<p>Signature: Jane Doe</p>",
        f"the digest sha256={SIGNATURE} is fine",
    ],
    ids=["py-annotation", "py-assignment", "html-prose", "bare-digest"],
)
def test_the_log_floor_leaves_ordinary_signature_text_alone(text: str) -> None:
    """The word ``signature`` in prose or code, and a bare 64-hex digest, are not
    signing values, so the log floor leaves them whole."""
    assert SecretRedactionFilter([]).redact(text) == text


def test_the_packaging_standalone_scan_blocks_a_signed_link() -> None:
    """The deployment-venv fallback scanner refuses a signed link's signing value,
    but a bundle that only names a key ID is not refused."""
    from kiro_crew.apps.builtins.aws_control.crew.packaging.pipeline.scan import scan_text

    assert scan_text(f"see {PRESIGNED}", "prompt")
    assert not scan_text(f"just the key id {KEY_ID} here", "prompt")
