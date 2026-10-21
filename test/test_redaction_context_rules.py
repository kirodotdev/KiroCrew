"""Context rules, the normalised view and the identifier tier, on everyday inputs.

Every value below is fake: the AWS documentation example values, assembled from
pieces so no file holds a whole credential-shaped literal, plus seeded hex.
The invariant: every secret is gone from the output, and every identifier
(key ID, account ID, ARN, digest, UUID, git SHA, request ID) is left exactly as
it was.
"""

from __future__ import annotations

import base64
import json
import random

import pytest

from kiro_crew.security.redaction import redact_credentials
from kiro_crew.security.redaction_normalise import normalised_view
from kiro_crew.security.text_redaction import StreamRedactor

TAG = "[REDACTED: credential]"
KEY_ID = "AKIA" + "IOSFODNN7" + "EXAMPLE"
TEMP_KEY_ID = "ASIA" + "IOSFODNN7" + "EXAMPLE"
SECRET = "wJalrXUtnFEMI" + "/K7MDENG/bPxRfiCY" + "EXAMPLEKEY"
SESSION = "FwoGZXIvYXdz" + "EXAMPLEsessionTOKEN0123456789abcdef"
_RNG = random.Random(22552)


def _hex(n: int) -> str:
    return "".join(_RNG.choice("0123456789abcdef") for _ in range(n))


ACCOUNT = "123456789012"
ARN = f"arn:aws:iam::{ACCOUNT}:role/Admin"
SHA = _hex(40)
DIGEST = _hex(64)
SIGNATURE = _hex(64)
UUID = f"{_hex(8)}-{_hex(4)}-{_hex(4)}-{_hex(4)}-{_hex(12)}"
REQUEST_ID = UUID.upper()
IDENTIFIERS = (KEY_ID, TEMP_KEY_ID, ACCOUNT, ARN, SHA, DIGEST, UUID, REQUEST_ID)


def _cases() -> dict[str, str]:
    return {
        "cloudtrail row": json.dumps(
            {
                "userIdentity": {"accessKeyId": TEMP_KEY_ID, "arn": ARN, "accountId": ACCOUNT},
                "requestID": REQUEST_ID,
                "eventID": UUID,
            }
        ),
        "assume-role output": json.dumps(
            {
                "Credentials": {
                    "AccessKeyId": TEMP_KEY_ID,
                    "SecretAccessKey": SECRET,
                    "SessionToken": SESSION,
                },
                "AssumedRoleUser": {"Arn": ARN},
            },
            indent=2,
        ),
        "env dump": (
            f"AWS_ACCESS_KEY_ID={KEY_ID}\nAWS_SECRET_ACCESS_KEY={SECRET}\n"
            f"AWS_SESSION_TOKEN={SESSION}\nGIT_COMMIT={SHA}\n"
        ),
        "credentials file": (
            f"[default]\naws_access_key_id = {KEY_ID}\naws_secret_access_key = {SECRET}\n"
        ),
        "escaped json": json.dumps(json.dumps({"secretAccessKey": SECRET, "id": UUID})),
        "percent-encoded": f"secret_access_key%3D{SECRET.replace('/', '%2F')}&account={ACCOUNT}",
        "csv pair": f"user,{KEY_ID},{SECRET},{ACCOUNT}",
        "sigv4 header": (
            f"Authorization: AWS4-HMAC-SHA256 Credential={KEY_ID}/20261021/us-east-1/s3/aws4_request,"
            f" SignedHeaders=host;x-amz-date, Signature={SIGNATURE}"
        ),
        "presigned s3": (
            f"https://bucket.s3.amazonaws.com/o.txt?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            f"&X-Amz-Credential={TEMP_KEY_ID}%2F20261021%2Fus-east-1%2Fs3%2Faws4_request"
            f"&X-Amz-Security-Token={SESSION}&X-Amz-Signature={SIGNATURE}"
        ),
        "presigned cloudfront": (
            f"https://d111111abcdef8.cloudfront.net/x.png?Expires=1793000000"
            f"&Signature=Ab{SECRET[:30]}~&Key-Pair-Id=K2JCJMDEHXQW5F"
        ),
        "base64 ini": base64.b64encode(f"aws_secret_access_key = {SECRET}\n".encode()).decode(),
        "basic header": "Authorization: Basic "
        + base64.b64encode(b"user:pw-example-1234").decode(),
    }


@pytest.mark.parametrize("name", sorted(_cases()))
def test_secrets_go_and_identifiers_stay(name: str) -> None:
    text = _cases()[name]
    out, _ = redact_credentials(text)
    for secret in (SECRET, SESSION, SECRET.replace("/", "%2F"), SECRET[:30], SIGNATURE):
        assert secret not in out, name
    for identifier in IDENTIFIERS:
        assert out.count(identifier) == text.count(identifier), (name, identifier)


def test_a_signature_in_a_sigv4_header_goes_but_its_key_id_stays() -> None:
    out, _ = redact_credentials(_cases()["sigv4 header"])
    assert f"Credential={KEY_ID}/" in out
    assert out.endswith(f"Signature={TAG}")


def test_labels_are_kept_and_json_still_parses() -> None:
    out, _ = redact_credentials(_cases()["assume-role output"])
    creds = json.loads(out)["Credentials"]
    assert creds == {"AccessKeyId": TEMP_KEY_ID, "SecretAccessKey": TAG, "SessionToken": TAG}


def test_an_escaped_value_is_replaced_in_place() -> None:
    out, _ = redact_credentials(_cases()["escaped json"])
    inner = json.loads(json.loads(out))
    assert inner == {"secretAccessKey": TAG, "id": UUID}


@pytest.mark.parametrize(
    "text",
    [
        f"see {SHA} {DIGEST} and sha256-{base64.b64encode(bytes.fromhex(DIGEST)).decode()}",
        f"x-amz-request-id: {REQUEST_ID}",
        f"{ARN} {UUID} {ACCOUNT}",
        "the password reset is required for login",
        "api_key = os.environ['API_KEY_2']",
        "secret = settings.SECRET_KEY",
        "token: ${GITHUB_TOKEN}",
    ],
)
def test_ordinary_text_is_unchanged(text: str) -> None:
    assert redact_credentials(text) == (text, [])


def test_an_unlabelled_value_alone_is_not_guessed() -> None:
    assert redact_credentials(f"value {SECRET} end")[0] == f"value {SECRET} end"


def test_a_value_next_to_a_key_id_is_its_secret() -> None:
    out, _ = redact_credentials(f"{KEY_ID} {SECRET}")
    assert out == f"{KEY_ID} {TAG}"


def test_redaction_is_a_fixed_point() -> None:
    for text in _cases().values():
        once, _ = redact_credentials(text)
        assert redact_credentials(once)[0] == once


# ── The normalised view ──


def test_the_view_maps_every_character_back() -> None:
    text = 'a%2Fb\\u0041\x1b[0mc\u200bd\\"e'
    view = normalised_view(text)
    assert view is not None
    assert view.text == 'a/bAcd"e'
    assert view.source_span(0, len(view.text)) == (0, len(text))
    assert text[slice(*view.source_span(1, 2))] == "%2F"
    assert text[slice(*view.source_span(3, 4))] == "\\u0041"


def test_text_with_nothing_to_normalise_has_no_view() -> None:
    assert normalised_view("plain text, nothing escaped") is None


def test_a_twice_escaped_value_maps_to_its_source() -> None:
    text = "%252F"
    view = normalised_view(text)
    assert view is not None and view.text == "/"
    assert view.source_span(0, 1) == (0, len(text))


# ── Streaming ──


def _stream(chunks: list[str]) -> str:
    redactor = StreamRedactor()
    return "".join(redactor.feed(chunk) for chunk in chunks) + redactor.flush()


@pytest.mark.parametrize(
    "chunks",
    [
        ["export AWS_SECRET_ACCESS_KEY = ", SECRET, "\n"],
        ["export AWS_SECRET", "_ACCESS_KEY=", SECRET[:10], SECRET[10:], " done\n"],
        ['{"password": "', "pw-example-", "1234", '"}\n'],
        [_cases()["presigned s3"][:150], _cases()["presigned s3"][150:], " end\n"],
    ],
)
def test_a_value_split_from_its_label_across_chunks_is_redacted(chunks: list[str]) -> None:
    out = _stream(chunks)
    assert SECRET not in out
    assert "pw-example-1234" not in out
    assert SESSION not in out
    assert "[REDACTED:" in out


# ── Label shapes beyond key=value ──


@pytest.mark.parametrize(
    "text",
    [
        f"aws configure set aws_secret_access_key {SECRET}",
        f"--aws-secret-access-key {SECRET}",
        f"SecretAccessKey {SECRET}",
        f"secret access key is {SECRET}",
        f"<SecretAccessKey>{SECRET}</SecretAccessKey>",
        f'{{"name": "AWS_SECRET_ACCESS_KEY", "value": "{SECRET}"}}',
        f"- name: AWS_SECRET_ACCESS_KEY\n  value: {SECRET}",
        f"secret_access_key: <{SECRET}>",
        f"aws_secret_access_key\uff1a{SECRET}",
        f"**SecretAccessKey:** {SECRET}",
        f"SecretAccessKey\n: {SECRET}",
        f"ABIA{KEY_ID[4:]} {SECRET}",
        f"key{KEY_ID} {SECRET}",
        f"Server=db.example.com;Uid=sa;Pwd={SECRET};",
    ],
)
def test_a_secret_in_a_label_shape_is_redacted(text: str) -> None:
    out, _ = redact_credentials(text)
    assert SECRET not in out
    assert TAG in out


@pytest.mark.parametrize(
    "text",
    [
        "PWD=/workplace/project",
        "OLDPWD=/home/user/project2",
        "pagination_token=abc12345xyz",
        "next_token=eyJ0b2tlbiI6bnVsbH0=",
        "csrf_token: a1b2c3d4",
        "the_token=token_v2_name",
        f"client_secret_arn=arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:prod/db-AbC123",
    ],
)
def test_a_field_that_names_no_secret_is_kept(text: str) -> None:
    assert redact_credentials(text) == (text, [])


@pytest.mark.parametrize(
    "chunks",
    [
        ["password => ", "pw-Example-12345", " done"],
        ["password := ", "pw-Example-12345", " done"],
        ["**password**: ", "pw-Example-12345", " done"],
        ["Authorization: Basic ", base64.b64encode(b"user:pw-Example-12345").decode(), " done"],
        ["gl" + "pat-\u200b", "abcdefghij0123456789", " done"],
        ["gl" + "pat-ab\x1b[0m", "cdefghij0123456789", " done"],
    ],
)
def test_a_value_after_a_separator_or_an_invisible_split_is_held_in_the_stream(
    chunks: list[str],
) -> None:
    out = _stream(chunks)
    assert "pw-Example-12345" not in out
    assert "0123456789" not in out
    assert out.endswith(" done")


@pytest.mark.parametrize("size", [1, 3, 7, 64])
@pytest.mark.parametrize("sep", [",", " ", "\t", " | "])
def test_a_secret_beside_its_key_id_is_held_in_the_stream(size: int, sep: str) -> None:
    text = f"user{sep}{KEY_ID}{sep}{SECRET}{sep}{ACCOUNT}\n"
    redactor = StreamRedactor()
    out = "".join(redactor.feed(text[i : i + size]) for i in range(0, len(text), size))
    out += redactor.flush()
    assert SECRET not in out
    assert KEY_ID in out and ACCOUNT in out


@pytest.mark.parametrize("size", [1, 2, 3, 7, 11, 64])
@pytest.mark.parametrize(
    "text",
    [
        f"x secret access key is {SECRET} ok\n",
        f"{SECRET},{KEY_ID}\n",
        f'{{"name": "AWS_SECRET_ACCESS_KEY", "value": "{SECRET}"}}\n',
        f"aws configure set aws_secret_access_key {SECRET}\n",
        f"- name: AWS_SECRET_ACCESS_KEY\n  value: {SECRET}\n",
    ],
)
def test_a_label_or_key_id_shape_is_held_with_its_value_in_the_stream(size: int, text: str) -> None:
    redactor = StreamRedactor()
    out = "".join(redactor.feed(text[i : i + size]) for i in range(0, len(text), size))
    out += redactor.flush()
    assert SECRET not in out
    assert out == redact_credentials(text)[0] or TAG in out


def test_a_hex_or_uuid_value_behind_a_secret_label_is_a_secret() -> None:
    for value in (SIGNATURE, UUID):
        out, _ = redact_credentials(f'{{"webhook_secret": "{value}"}}')
        assert value not in out


def test_base64_wrapped_over_two_lines_is_redacted_whole() -> None:
    encoded = base64.encodebytes(f"aws_secret_access_key = {SECRET}\nregion=x\n".encode()).decode()
    assert encoded.count("\n") == 2
    out, _ = redact_credentials(encoded)
    assert "[REDACTED: encoded credential]" in out
    for line in encoded.splitlines():
        assert line not in out


def test_a_deploy_finding_carries_no_character_of_a_labelled_value() -> None:
    from kiro_crew.deploy.scan import scan_content

    findings = scan_content("password = Hx9pQ2rTzz")
    assert findings and all("Hx9p" not in str(f) for f in findings)


@pytest.mark.parametrize(
    "text",
    [
        "def login(user: str, password: Optional[str] = None):",
        "def refresh(access_token: Optional[str]) -> None:",
        "aws_secret_access_key = settings.aws_key",
        "secret_access_key = load_key(path)",
    ],
)
def test_source_code_that_names_or_reads_a_secret_is_kept(text: str) -> None:
    assert redact_credentials(text) == (text, [])


@pytest.mark.parametrize("value", ["abc123;rest-of-it9", "my_pass_word1", "two words 1"])
def test_a_quoted_value_is_read_to_its_closing_quote(value: str) -> None:
    out, _ = redact_credentials(f'{{"password": "{value}"}}')
    assert out == '{"password": "[REDACTED: credential]"}'


def test_a_secret_without_a_digit_beside_its_key_id_is_paired() -> None:
    no_digit = "wJalrXUtnFEMI/K" + "MDENG/bPxRfiCYEXAMPLEKEYa"
    assert len(no_digit) == 40
    out, _ = redact_credentials(f"{KEY_ID},{no_digit}")
    assert out == f"{KEY_ID},{TAG}"


@pytest.mark.parametrize("size", [1, 2, 3, 7, 64])
@pytest.mark.parametrize("value", ["abc123;rest-of-it9", "two words 1x"])
def test_a_quoted_value_is_held_to_its_closing_quote_in_the_stream(size: int, value: str) -> None:
    text = f'{{"password": "{value}"}}\n'
    redactor = StreamRedactor()
    out = "".join(redactor.feed(text[i : i + size]) for i in range(0, len(text), size))
    out += redactor.flush()
    assert value[:6] not in out


@pytest.mark.parametrize("value", ["abc123;rest-of-it9", "two words 1x", 'esc\\"aped-pw-9'])
def test_a_quoted_name_value_pair_is_read_to_its_closing_quote(value: str) -> None:
    out, _ = redact_credentials(f'{{"name": "DB_PASSWORD", "value": "{value}"}}')
    assert out == f'{{"name": "DB_PASSWORD", "value": "{TAG}"}}'


@pytest.mark.parametrize("size", [1, 2, 3, 7, 64])
def test_an_escaped_quote_does_not_end_a_held_value_in_the_stream(size: int) -> None:
    text = '{"password": "pw-Ex\\"ample; 12345"}\n'
    redactor = StreamRedactor()
    out = "".join(redactor.feed(text[i : i + size]) for i in range(0, len(text), size))
    out += redactor.flush()
    assert "ample; 12345" not in out
    assert "pw-Ex" not in out


def test_a_secret_after_its_key_id_in_a_url_path_is_redacted_and_blocks() -> None:
    from kiro_crew.security.exfil import scan_exfiltration_urls

    url = f"https://collector.example.net/k/{KEY_ID}/{SECRET}/x"
    out, _ = redact_credentials(url)
    assert SECRET not in out and KEY_ID in out
    assert scan_exfiltration_urls(url)


def test_the_blocking_check_sees_a_paired_secret_and_a_catalog_finding() -> None:
    from kiro_crew.security.redaction import _contains_fixed_credential

    assert _contains_fixed_credential(f"{KEY_ID},{SECRET}")
    assert _contains_fixed_credential("LINEAR=lin_api_" + "a1b2c3d4e5" * 4)
    assert not _contains_fixed_credential(f"{KEY_ID},{ACCOUNT}")


@pytest.mark.parametrize("value", ["abc123;rest-of-it9", "two words 1x"])
def test_the_log_filter_drops_a_quoted_value_whole(value: str) -> None:
    from kiro_crew.log_redaction import SecretRedactionFilter

    out = SecretRedactionFilter([]).redact(f'password="{value}" next=1')
    assert out == 'password="[REDACTED]" next=1'
    aws = SecretRedactionFilter([]).redact(f"aws_secret_access_key='{value}'")
    assert aws == "aws_secret_access_key='[REDACTED]'"


def test_a_file_name_holding_a_key_is_not_an_attribute_read() -> None:
    held = "AKIA" + "IOSFODNN7EXAMPLEKEY"
    out, _ = redact_credentials(f"aws_secret_access_key={held}.txt")
    assert held not in out
    assert redact_credentials("aws_secret_access_key = settings.aws_key")[0].endswith(
        "settings.aws_key"
    )


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        # A code name that carries the label's own word names the value.
        ("login({ password: passwordInput })", True),
        ("self.password = newPassword", True),
        ("DB_PASSWORD=correct_horse_battery_staple", False),
        ("secret_access_key = load_key(path)", True),
        ("token = os.environ['API_TOKEN']", True),
        ("aws_secret_access_key = settings.aws_key", True),
    ],
)
def test_only_a_whole_value_exemption_or_a_call_keeps_a_labelled_value(
    text: str, kept: bool
) -> None:
    """One class: a value behind a label is a secret unless the WHOLE value is
    on the exemption list or it is called or subscripted. A variable name is
    not an identifier, so it is redacted like any other value."""
    assert (redact_credentials(text)[0] == text) is kept


def test_a_presigned_link_keeps_everything_but_its_signature() -> None:
    from kiro_crew.security.text_redaction import redact

    url = (
        "https://bucket.s3.amazonaws.com/obj.txt?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        f"&X-Amz-Credential={TEMP_KEY_ID}%2F20250101%2Fus-east-1%2Fs3%2Faws4_request"
        "&X-Amz-Date=20250101T000000Z&X-Amz-Expires=3600&X-Amz-SignedHeaders=host"
        f"&X-Amz-Signature={SIGNATURE}"
    )
    out = redact(f"get {url} now")
    assert SIGNATURE not in out
    assert out.startswith("get https://bucket.s3.amazonaws.com/obj.txt?") and TEMP_KEY_ID in out


def test_an_adc_object_keeps_its_client_id_and_loses_its_secrets() -> None:
    client_id = "1234-abc.apps.googleusercontent.com"
    secret, refresh = "GOCSPX-" + "abcDEF123456ghiJKL7890mn", "1//0g" + "AbCdEf123456GhIjKl-mnOpQr"
    out, _ = redact_credentials(
        json.dumps({"client_id": client_id, "client_secret": secret, "refresh_token": refresh})
    )
    assert client_id in out and secret not in out and refresh not in out


@pytest.mark.parametrize("value", [",pw-Example-123", ";pw-Example-123", "&pw Example;123"])
def test_a_quoted_value_that_starts_with_a_delimiter_is_redacted(value: str) -> None:
    from kiro_crew.log_redaction import SecretRedactionFilter

    text = f'{{"password": "{value}", "user": "bob"}}'
    out, _ = redact_credentials(text)
    assert out == f'{{"password": "{TAG}", "user": "bob"}}'
    assert redact_credentials(out) == (out, [])
    assert value not in SecretRedactionFilter([]).redact(text)
    assert value not in _stream([text[:16], text[16:20], text[20:]])


def test_a_deploy_finding_copies_no_character_of_a_signature() -> None:
    from kiro_crew.deploy.scan import scan_content

    header = f"Authorization: AWS4-HMAC-SHA256 Credential={KEY_ID}/x, Signature={SIGNATURE}"
    findings = scan_content(header)
    assert findings and all(SIGNATURE[:4] not in f.snippet for f in findings)


@pytest.mark.parametrize(
    "text", ['$env:DB_PASSWORD = "S3cr3tP@ss!"', '$env:API_KEY="sk9Xa82kdLq0"']
)
def test_a_powershell_env_label_is_a_label(text: str) -> None:
    from kiro_crew.log_redaction import SecretRedactionFilter

    value = text.split('"')[1]
    assert value not in redact_credentials(text)[0]
    assert value not in SecretRedactionFilter([]).redact(text)


def test_a_label_word_inside_an_arn_is_still_a_resource_type() -> None:
    arn = f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:prod/db-AbC123"
    assert redact_credentials(f"id={arn}") == (f"id={arn}", [])


@pytest.mark.parametrize(
    "text",
    [
        '{\\"password\\": \\"Sup3rS3cretVal9\\"} end',
        "\x1b[31mpassword\x1b[0m: Sup3rS3cretVal9 end",
    ],
)
def test_a_label_seen_through_escapes_is_held_in_the_stream(text: str) -> None:
    rng = random.Random(9)
    for _ in range(20):
        redactor = StreamRedactor()
        out, i = "", 0
        while i < len(text):
            n = rng.randint(1, 12)
            out += redactor.feed(text[i : i + n])
            i += n
        assert "Sup3rS3cretVal9" not in out + redactor.flush()


def test_an_encoded_connection_string_with_a_slash_password_is_redacted() -> None:
    url = f"postgres://app:{SECRET}@db.example.com:5432/app"
    encoded = base64.b64encode(url.encode()).decode()
    assert encoded not in redact_credentials(f"conn {encoded} end")[0]


@pytest.mark.parametrize(
    ("value", "kept"), [("abc12", False), ("dTpw", False), ("token", True), ("TOKEN", True)]
)
def test_a_short_authorization_credential_is_redacted(value: str, kept: bool) -> None:
    from kiro_crew.log_redaction import SecretRedactionFilter

    text = f"Authorization: Bearer {value}"
    assert (value in redact_credentials(text)[0]) is kept
    assert (value in SecretRedactionFilter([]).redact(text)) is kept


@pytest.mark.parametrize("value", ["$Xy9!kQ-pw-77", " lead-Pw-1234", "[x]Pw-12345", "Pw;9 ,x$"])
def test_a_quoted_literal_whatever_its_first_character_is_redacted(value: str) -> None:
    from kiro_crew.log_redaction import SecretRedactionFilter

    text = f'{{"password": "{value}"}}'
    assert redact_credentials(text)[0] == f'{{"password": "{TAG}"}}'
    assert value not in SecretRedactionFilter([]).redact(text)


@pytest.mark.parametrize(
    "value", ["$DB_PASS", "${DB_PASS}", "%DB_PASS%", "{{ db_pass }}", "<your-password>"]
)
def test_a_whole_reference_behind_a_label_is_kept(value: str) -> None:
    text = f'password = "{value}"'
    assert redact_credentials(text) == (text, [])


def test_base64_of_escaped_json_is_redacted() -> None:
    raw = '{\\"SecretAccessKey\\": \\"' + SECRET + '\\"}'
    encoded = base64.b64encode(raw.encode()).decode()
    assert encoded not in redact_credentials(f"blob {encoded} end")[0]


def _chunked(text: str, size: int) -> str:
    redactor = StreamRedactor()
    out = "".join(redactor.feed(text[i : i + size]) for i in range(0, len(text), size))
    return out + redactor.flush()


@pytest.mark.parametrize("size", [1, 7, 50])
@pytest.mark.parametrize("length", [422, 500])
def test_a_long_quoted_value_is_held_to_its_closing_quote(size: int, length: int) -> None:
    value = ("Ab1;cd,EF2 " * 60)[:length]
    out = _chunked(f'note {{"password": "{value}"}} done\n', size)
    assert all(value[i : i + 12] not in out for i in range(0, length - 12, 12))
    assert out.endswith("} done\n")


def test_a_quoted_value_past_the_bound_is_removed_up_to_the_bound() -> None:
    rng = random.Random(5)
    value = "".join(rng.choice("abcXYZ019;, ") for _ in range(600))
    out, _ = redact_credentials(f'{{"password": "{value}"}}')
    # The bound (512 characters) is the worst case: the rest is not a value.
    assert all(value[i : i + 16] not in out for i in range(0, 496, 16))


@pytest.mark.parametrize("size", [1, 7, 50])
@pytest.mark.parametrize(
    "text",
    [
        f"key id {KEY_ID} {SECRET} " + "lorem ipsum dolor sit amet " * 20 + "\n",
        f"secret {SECRET} then {KEY_ID} " + "lorem ipsum dolor sit amet " * 20 + "\n",
    ],
)
def test_a_paired_secret_is_redacted_before_its_hold_is_released(size: int, text: str) -> None:
    out = _chunked(text, size)
    assert SECRET not in out
    assert out.endswith("amet \n")


@pytest.mark.parametrize(
    "text",
    [
        "api_key: YOUR_API_KEY_HERE",
        "export API_KEY=your-api-key-here",
        "password: changeme123",
        f"aws_secret_access_key = {SECRET}",
    ],
)
def test_a_documented_placeholder_can_be_published_with_an_override(text: str) -> None:
    from kiro_crew.deploy.scan import is_credential_finding, scan_content

    findings = scan_content(text)
    assert findings and not [f for f in findings if is_credential_finding(f)]


@pytest.mark.parametrize(
    "text",
    [
        "api_key: yourSecretPassw0rd9",
        "password: Hx9pQ2rTzz",
        "Authorization: Basic YWxpY2U6UzNjcjN0IQ==",
        # A factory-default pair (``user:pass``) works; it is not a placeholder.
        "Authorization: Basic dXNlcjpwYXNz",
        "aws_secret_access_key = " + "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYzzzzzzzzzz",
    ],
)
def test_a_real_secret_behind_a_label_still_blocks_a_publish(text: str) -> None:
    from kiro_crew.deploy.scan import is_credential_finding, scan_content

    assert [f for f in scan_content(text) if is_credential_finding(f)]


@pytest.mark.parametrize(
    "text",
    [
        "api_key: your_Kx9pQ2rTzzW8mN4bV7cX1",
        "password: your-Hx9pQ2rTzz-W8kL3",
        "password: placeholder_Hx9pQ2rTzzW8",
        "aws_secret_access_key = " + "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYzzzEXAMPLE",
    ],
)
def test_a_random_value_after_a_placeholder_word_still_blocks(text: str) -> None:
    from kiro_crew.deploy.scan import is_credential_finding, scan_content

    assert [f for f in scan_content(text) if is_credential_finding(f)]


def test_a_secret_written_after_a_tag_is_redacted() -> None:
    from kiro_crew.log_redaction import SecretRedactionFilter

    text = '{"password": "[REDACTED] Hx9pQ2rTzzW8"}'
    assert "Hx9pQ2rTzzW8" not in redact_credentials(text)[0]
    assert "Hx9pQ2rTzzW8" not in SecretRedactionFilter([]).redact(text)


@pytest.mark.parametrize("size", [1, 3, 64])
def test_the_stream_removes_as_much_of_a_long_quoted_value_as_the_batch(size: int) -> None:
    rng = random.Random(5)
    value = "".join(rng.choice("abcXYZ019;, -") for _ in range(600))
    out = _chunked(f'secret: "{value}" done\n', size)
    assert all(value[i : i + 8] not in out for i in range(0, 504, 8))
    assert value[506:512] not in out


# ── Class tests (one per root cause) ──


@pytest.mark.parametrize(
    "dump", [json.dumps, lambda d: "\n".join(f"{k}: {v}" for k, v in d.items())]
)
def test_structured_presigned_request_drops_signature_keeps_key_id(dump) -> None:
    """A signing value is a label in the one table, so every serialisation the
    label reader knows drops it: JSON, YAML, a header, a URL query."""
    from kiro_crew.log_redaction import SecretRedactionFilter

    signature = "a" * 8 + "0123456789abcdef" * 3 + "deadbeef"
    text = dump(
        {
            "url": "https://b.s3.amazonaws.com/k",
            "X-Amz-Credential": f"{KEY_ID}/20261022/us-east-1/s3/aws4_request",
            "X-Amz-Signature": signature,
        }
    )
    for out in (redact_credentials(text)[0], SecretRedactionFilter([]).redact(text)):
        assert signature not in out and KEY_ID in out
        assert "X-Amz-Signature" in out


def test_overridable_flood_cannot_hide_a_hard_finding() -> None:
    """The report cap bounds the list, never the verdict."""
    from kiro_crew.deploy.scan import is_credential_finding, scan_content

    other = "Zq8Xk3LmNp0Rt5Vw7Yb9" + "Cd2Ef4Gh6Jk1Mn3Pq5Rs"
    text = "password: changeme123\n" * 600 + f"{KEY_ID},{other}\n"
    assert any(is_credential_finding(f) for f in scan_content(text))


@pytest.mark.parametrize("sep", ["/", "+", "=", ",", " "])
@pytest.mark.parametrize("key_first", [True, False])
def test_pairing_is_order_and_delimiter_free(sep: str, key_first: bool) -> None:
    other = "Zq8Xk3LmNp0Rt5Vw7Yb9" + "Cd2Ef4Gh6Jk1Mn3Pq5Rs"
    pair = f"{KEY_ID}{sep}{other}" if key_first else f"{other}{sep}{KEY_ID}"
    out, _ = redact_credentials(f"![x](https://attacker.example/x/{pair})")
    assert other not in out and KEY_ID in out


def test_view_pass_reads_only_windows_around_escapes(monkeypatch) -> None:
    """The normalised-view passes read the windows around a changed character,
    never the whole text."""
    from kiro_crew.security import redaction

    rows = [{"id": i, "msg": f"entry {i} with ordinary text"} for i in range(20000)]
    text = json.dumps(rows) + ' {\\"password\\": \\"Sup3rS3cretVal9\\"} '
    seen: list[int] = []
    original = redaction._context_spans

    def spy(chunk: str):
        seen.append(len(chunk))
        return original(chunk)

    monkeypatch.setattr(redaction, "_context_spans", spy)
    out, _ = redaction.redact_credentials(text)
    assert "Sup3rS3cretVal9" not in out
    assert seen[0] == len(text) and sum(seen[1:]) <= 4096


_PARITY_CORPUS = [
    base64.encodebytes(
        f"aws_secret_access_key = {SECRET}\naws_session_token = {'IQoJb3JpZ2luX2VjE' * 8}\n".encode()
    ).decode(),
    f"{SECRET},{KEY_ID}\n",
    '{\\"password\\": \\"Sup3rS3cretVal9\\"} end',
    '{"name": "DB_PASSWORD", "value": "Sup3rS3cretVal9"}',
    'algolia_key = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"',
    '{"password": "' + ("Ab1;cd,EF2 " * 40)[:422] + '"}',
]


@pytest.mark.parametrize("text", _PARITY_CORPUS, ids=range(len(_PARITY_CORPUS)))
@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 12, 0])
def test_stream_equals_batch_for_every_chunking(text: str, size: int) -> None:
    """The stream holds from every context rule's trigger until its reach has
    arrived, so it removes what the batch removes, whatever the chunking (0:
    by line)."""
    from kiro_crew.security.text_redaction import redact

    whole = text + " " + "lorem ipsum dolor sit amet " * 24
    chunks = (
        whole.splitlines(keepends=True)
        if size == 0
        else [whole[i : i + size] for i in range(0, len(whole), size)]
    )
    redactor = StreamRedactor()
    out = "".join(redactor.feed(c) for c in chunks) + redactor.flush()
    assert out == redact(whole)


@pytest.mark.parametrize(
    "text",
    [
        'password = "/Zq8pLm3Nv7Rt2Wx9" ok',
        '{"password": "./Zq8pLm3Nv7Rt2"} ok',
        'password = "Correct.Horse.Battery.Staple" ok',
    ],
)
def test_a_quoted_literal_has_no_code_shape_exemption(text: str) -> None:
    from kiro_crew.log_redaction import SecretRedactionFilter

    for out in (
        redact_credentials(text)[0],
        SecretRedactionFilter([]).redact(text),
        _chunked(text, 1),
    ):
        assert out != text and "Zq8p" not in out and "Horse" not in out


@pytest.mark.parametrize(
    "text",
    [
        "Signature: Xy9kLmn0pQrsTuv1wXyz2AbCdEf3 Expires: 1700000000 Key-Pair-Id: APKAEIBAERJR2EXAMPLE",
        "Signature: Xy9kLmn0pQrsTuv1wXyz2AbCdEf3\nExpires: 1700000000\n",
        "<p>Signature: Jane Doe</p> and more text here",
    ],
)
@pytest.mark.parametrize("size", [1, 2, 7, 64])
def test_a_bare_signature_is_held_until_its_sibling_arrives(text: str, size: int) -> None:
    from kiro_crew.security.text_redaction import redact

    assert _chunked(text, size) == redact(text)


def test_pairing_removes_only_the_key_and_keeps_the_text_around_it() -> None:
    other = "Zq8Xk3LmNp0Rt5Vw7Yb9" + "Cd2Ef4Gh6Jk1Mn3Pq5Rs"
    out, _ = redact_credentials(f"https://example.com/{KEY_ID}/{other}/file.txt")
    assert out == f"https://example.com/{KEY_ID}/{TAG}/file.txt"
    out, _ = redact_credentials(f"{KEY_ID} path /a/b/{other}/c")
    assert out == f"{KEY_ID} path /a/b/{TAG}/c"


def test_a_flood_cannot_hide_an_aws_secret_beside_its_context() -> None:
    from kiro_crew.deploy.scan import is_credential_finding, scan_content

    other = "Zq8Xk3LmNp0Rt5Vw7Yb9" + "Cd2Ef4Gh6Jk1Mn3Pq5Rs"
    text = "api_key: YOUR_API_KEY_HERE\n" * 600 + f"aws_secret_access_key: {other}\n"
    assert any(is_credential_finding(f) for f in scan_content(text))


def test_a_wrapped_base64_block_hold_starts_at_its_first_line() -> None:
    from kiro_crew.security.redaction_labels import _wrapped_block_start

    block = "QUJDREVGR0hJSktMTU5PUFFS\nU1RVVldYWVphYmNkZWZnaGlq\nbG1u"
    buf = "prose here " + block
    assert _wrapped_block_start(buf, 0) == len("prose here ")
    assert _wrapped_block_start("no block here at all", 0) is None


# ── Class tests: label holds, view windows, blocking tier, one vendor table ──


@pytest.mark.parametrize(
    "text",
    [
        "api key: a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6 end of the line here",
        "the signing key = Zq8Xk3LmNp0Rt5Vw7Yb9 and more prose follows",
        "access token: Zq8Xk3LmNp0Rt5Vw7Yb9 and more prose follows",
    ],
)
@pytest.mark.parametrize("size", [1, 2, 3, 4, 5, 7])
def test_a_multi_word_label_split_in_the_stream_is_held(text: str, size: int) -> None:
    """The hold on a label still arriving is built from the label word table,
    so every multi-word label is held while it is cut after any word."""
    from kiro_crew.security.text_redaction import redact

    assert _chunked(text, size) == redact(text)


def test_a_view_window_grows_to_the_end_of_a_long_match() -> None:
    """A match found in a normalisation window that reaches the window's edge
    is completed on the whole text, not cut at the window boundary."""
    rng = random.Random(3)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    token = "".join(rng.choice(alphabet) for _ in range(1500))
    out, _ = redact_credentials(f"x SessionToken%3D{token} end")
    assert all(token[i : i + 40] not in out for i in range(0, 1460, 40))
    body = ["".join(rng.choice(alphabet + "+/") for _ in range(64)) for _ in range(26)]
    pem = (
        "-----BEGIN%20RSA PRIVATE KEY-----\n" + "\n".join(body) + "\n-----END RSA PRIVATE KEY-----"
    )
    out, _ = redact_credentials(f"x\n{pem}\ny")
    assert not [line for line in body if line in out]


@pytest.mark.parametrize(
    "url",
    [
        "https://hooks.example.com/notify?signature=a8f5f167f44f4964e6c998dee827110c",
        "https://d1.cloudfront.net/i.jpg?Expires=1700000000&Signature=Nql641NHEUkUaXAbcd9&Key-Pair-Id=APKAEXAMPLE",
    ],
)
def test_a_signing_value_masks_but_does_not_block(url: str) -> None:
    """Blocking follows the tier: a Bearer-link signing value is masked in
    output and is not, alone, grounds to refuse a request."""
    from kiro_crew.security.exfil import scan_exfiltration_urls
    from kiro_crew.security.text_redaction import redact

    assert scan_exfiltration_urls(f"curl '{url}'") == []
    assert "Nql641NHEUkUaXAbcd9" not in redact(url) and "a8f5f167f44f" not in redact(url)


@pytest.mark.parametrize("value", ["hunterxx", "abc", "letmeinplease"])
def test_a_quoted_value_behind_a_label_is_a_secret_whatever_its_length(value: str) -> None:
    from kiro_crew.log_redaction import SecretRedactionFilter

    text = f'{{"password": "{value}"}}'
    assert value not in redact_credentials(text)[0]
    assert value not in SecretRedactionFilter([]).redact(text)


@pytest.mark.parametrize(
    "secret",
    [
        "/" + "Zq8Xk3LmNp0Rt5Vw7Yb9Cd2Ef4Gh6Jk1Mn3Pq5R",
        "Zq8Xk3LmNp0Rt5Vw7Yb9Cd2Ef4Gh6Jk1Mn3Pq5R" + "+",
    ],
)
def test_a_paired_key_that_starts_or_ends_with_a_separator_is_redacted(secret: str) -> None:
    assert len(secret) == 40
    out, _ = redact_credentials(f"{KEY_ID},{secret}\n")
    assert out == f"{KEY_ID},{TAG}\n"


def test_the_preview_refuses_an_escaped_credential_object() -> None:
    from kiro_crew.apps.builtins.design_tweak.backend.preview_files import contains_credential

    js = (
        f'const id = "{KEY_ID}";\n'
        + "// filler\n" * 40
        + 'const c = JSON.parse("{\\"SecretAccessKey\\": \\"'
        + SECRET
        + '\\"}");\n'
    )
    assert contains_credential(None, js.encode())


@pytest.mark.parametrize(
    "token",
    [
        "xoxb-" + "1234567890-1234567890-abcdefghij",
        "gho_" + "a1b2c3d4e5" * 3 + "a1b2c3",
        "glpat-" + "a1b2c3d4e5f6g7h8i9j0",
        "sk-ant-api03-" + "a" * 93 + "AA",
        "dop_v1_" + "ab" * 32,
        "pypi-AgEIcHlwaS5vcmc" + "a1" * 30,
    ],
)
def test_each_vendor_format_lives_in_one_table(token: str) -> None:
    """A format the core's fixed patterns own is matched by them and by no rule
    of the generated catalog."""
    from kiro_crew.security.redaction import _contains_credential_pattern
    from kiro_crew.security.secret_catalog import catalog_findings

    assert _contains_credential_pattern(f"key {token} end")
    assert catalog_findings(f"key {token} end") == []


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("hugs and kisses xoxo-xoxo-xoxo-xoxo from grandma", True),
        (".sk-admin-sidebar-navigation-item { color: red }", True),
        ("OPENAI_KEY sk-" + "a1b2c3d4e5" * 2 + "T3BlbkFJ" + "a1b2c3d4e5" * 2, False),
        ("token DOR_V1_" + "AB12" * 16, False),
        ("token xoxr-abcd1234", False),
    ],
)
def test_a_catalog_spelling_the_fixed_patterns_lack_is_still_caught(text: str, kept: bool) -> None:
    """The spellings only a catalog rule read before are caught by the fixed
    patterns, their one table, which are not widened into ordinary words and
    class names."""
    assert (redact_credentials(text)[0] == text) is kept


def test_a_quoted_value_behind_a_weak_label_keeps_the_word_exemption() -> None:
    """``pwd`` also names a command: ``{"bash": {"pwd": "deny"}}`` is a permission
    map, while a strong label's quoted value is redacted whatever its length."""
    text = '{"*": "ask", "bash": {"pwd": "deny"}}'
    assert redact_credentials(text) == (text, [])
    assert "Hx9pQ2rTzz" not in redact_credentials('{"pwd": "Hx9pQ2rTzz"}')[0]


@pytest.mark.parametrize("wrap", [True, False])
def test_blocking_reads_every_encoded_run_the_redactor_reads(wrap: bool) -> None:
    """One base64 scanner serves redaction and blocking: a wrapped or
    percent-encoded run that decodes to an AWS credentials file blocks."""
    from urllib.parse import quote

    from kiro_crew.security.redaction import _contains_fixed_credential

    ini = f"[default]\naws_access_key_id = {KEY_ID}\naws_secret_access_key = {SECRET}\n" * 2
    raw = ini.encode()
    encoded = base64.encodebytes(raw).decode() if wrap else base64.b64encode(raw).decode()
    url = "https://accounts.google.com/o/oauth2/auth?state=" + quote(encoded, safe="")
    assert _contains_fixed_credential(url)


@pytest.mark.parametrize(
    "text",
    [
        '{"access_token": "/9jQx+Zr8kLmN2pQrStUvWx"}',
        '{"db_pass": "/Qm8xZ+v3Lk9TfR2wYp"}',
        '{"refresh_token": "../Kq9Zx+Lm2pR7tYv"}',
        '{"api_token": "ab.cd.ef"}',
    ],
)
def test_a_weak_label_keeps_only_the_length_and_word_tests(text: str) -> None:
    from kiro_crew.log_redaction import SecretRedactionFilter

    assert redact_credentials(text)[0] != text
    assert SecretRedactionFilter([]).redact(text) != text


def test_an_aws_label_is_never_weak() -> None:
    from kiro_crew.security.redaction import _contains_fixed_credential

    text = f'{{"aws_session_token": "{SESSION}"}}'
    assert SESSION not in redact_credentials(text)[0]
    assert _contains_fixed_credential(text)


@pytest.mark.parametrize(
    "value",
    [
        "sha256-" + base64.b64encode(bytes(range(32))).decode(),
        "sha384-" + base64.b64encode(bytes(range(48))).decode(),
        "sha512-" + base64.b64encode(bytes(range(64))).decode(),
    ],
)
def test_a_subresource_integrity_digest_is_never_redacted(value: str) -> None:
    """No pass guesses at a bare base64 run, so an SRI digest stays whole in a
    lockfile, a script tag and the stream."""
    for text in (f'"integrity": "{value}"', f'<script integrity="{value}"></script>'):
        assert redact_credentials(text) == (text, [])
        assert _chunked(text, 7) == text


def test_an_aws_name_value_pair_keeps_its_aws_rule_and_blocks() -> None:
    from kiro_crew.apps.builtins.design_tweak.backend.preview_files import contains_credential
    from kiro_crew.security.redaction import _contains_fixed_credential

    text = json.dumps({"name": "SecretAccessKey", "value": SECRET})
    assert SECRET not in redact_credentials(text)[0]
    assert _contains_fixed_credential(text)
    assert contains_credential(None, text.encode())


@pytest.mark.parametrize("urlsafe", [True, False])
def test_an_unpadded_base64_run_decodes_like_a_padded_one(urlsafe: bool) -> None:
    from kiro_crew.security.redaction import _contains_fixed_credential

    for pad in range(4):
        raw = f"aws_secret_access_key = {SECRET}\n{'x' * pad}".encode()
        encoded = (base64.urlsafe_b64encode if urlsafe else base64.b64encode)(raw).decode()
        stripped = encoded.rstrip("=")
        assert stripped not in redact_credentials(f"state={stripped} end")[0]
        assert _contains_fixed_credential(f"https://github.com/login/oauth?state={stripped}")
