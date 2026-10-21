"""Redaction reads every decoded layer, holds a catalog match through its filler,
keeps numeric passwords behind a label, and leaves ports, paths and quoted PEM
markers alone."""

from __future__ import annotations

import json
import os
import random
import re
import string
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.design_tweak.backend import preview_files, server
from kiro_crew.log_redaction import SecretRedactionFilter
from kiro_crew.security.redaction import redact_credentials
from kiro_crew.security.text_redaction import StreamRedactor


def _redact(text: str) -> str:
    return redact_credentials(text)[0]


def _stream(text: str, size: int) -> str:
    redactor = StreamRedactor()
    out = "".join(redactor.feed(text[i : i + size]) for i in range(0, len(text), size))
    return out + redactor.flush()


# ── A run of digits behind a secret label is the secret ──


@pytest.mark.parametrize("quoted", [True, False])
def test_a_numeric_password_behind_its_label_is_redacted(quoted: bool) -> None:
    password = "834729105623"  # 12 digits: the shape of an account ID
    text = json.dumps({"password": password}) if quoted else f"password={password}"

    assert password not in _redact(text)
    assert password not in SecretRedactionFilter([]).redact(text)


def test_an_account_id_behind_its_own_label_stays() -> None:
    text = '{"account_id": "123456789012"}'

    assert _redact(text) == text


# ── Every decoded layer is read ──


def test_a_double_encoded_password_with_a_quote_is_redacted_whole() -> None:
    password = 'Xq7"m2pzR9tvB8nc1kd6wf'
    text = json.dumps(json.dumps({"password": password}))

    out = _redact(text)

    assert "Xq7" not in out
    assert "m2pzR9tvB8nc1kd6wf" not in out
    # One value, one tag, and the stream agrees with the batch.
    assert out.count("[REDACTED") == 1
    assert _stream(text, 1) == out
    assert _stream(text, 16) == out


def test_a_triple_encoded_password_with_a_quote_is_redacted_whole() -> None:
    password = 'Xq7"m2pzR9tvB8nc1kd6wf'
    out = _redact(json.dumps(json.dumps(json.dumps({"password": password}))))

    assert "Xq7" not in out
    assert "m2pzR9tvB8nc1kd6wf" not in out


# ── The stream holds a catalog match through the rule's filler ──

#: A made-up UUID, kept apart from its keyword so no literal reads as a key.
_UUID = "12345678-1234-1234-1234-" "123456789abc"

_CATALOG_LINES = [
    f"Your heroku key = {_UUID} ok\n",
    "adafruit key = xq7lm2pz9rt4vb8nc1kd6wf3hj5gs0ty ok\n",
    # The rules' filler is up to 20 characters: " production api key ".
    f"heroku production api key = {_UUID} ok\n",
]


@pytest.mark.parametrize("text", _CATALOG_LINES)
@pytest.mark.parametrize("size", [1, 16])
def test_the_stream_redacts_a_catalog_secret_behind_filler_like_the_batch(
    text: str, size: int
) -> None:
    batch = _redact(text)
    assert "[REDACTED" in batch

    assert _stream(text, size) == batch


# ── A port and a path are not a password ──


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:5173/@vite/client",
        "https://registry.npmjs.org:443/@types/node",
    ],
)
def test_a_url_with_a_port_and_an_at_in_its_path_is_left_intact(url: str) -> None:
    assert _redact(url) == url


@pytest.mark.parametrize(
    ("url", "password", "host"),
    [
        ("postgres://app:ab/cdEF12xy@db.internal:5432/x", "ab/cdEF12xy", "@db.internal:5432/x"),
        # Digit-led passwords: the host after ``@`` is dotted or has a port.
        (
            "postgres://app:2024/SecretPw99xy@db.internal:5432/x",
            "2024/SecretPw99xy",
            "@db.internal",
        ),
        ("redis://default:007/xK9mQ2pL7vR4@cache.example.com:6379", "007/xK9mQ2pL7vR4", "@cache."),
        ("mysql://root:443/pa55wordQQ@db:3306/x", "443/pa55wordQQ", "@db:3306/x"),
    ],
)
def test_a_uri_password_holding_a_slash_is_still_redacted(
    url: str, password: str, host: str
) -> None:
    out = _redact(url)

    assert password not in out
    assert host in out
    assert _stream(url, 1) == out


# ── The preview's PEM marker exemption holds in a decoded view too ──

#: PEM armour lines, split so no literal marker sits in this file.
_BEGIN = "-----BEGIN RSA " "PRIVATE KEY-----"
_END = "-----END RSA " "PRIVATE KEY-----"


def test_an_escaped_pem_marker_without_a_body_is_served() -> None:
    data = f'const k = "{_BEGIN}\\n...\\n{_END}\\n";'.encode()

    assert preview_files.contains_credential(server, data) is False


def test_a_double_escaped_pem_marker_without_a_body_is_served() -> None:
    marker = f"{_BEGIN}\n...\n{_END}\n"
    data = json.dumps(json.dumps(marker)).encode()

    assert preview_files.contains_credential(server, data) is False


def test_an_escaped_pem_block_with_a_body_is_refused() -> None:
    body = "MIIEowIBAAKCAQEA" + "q7Lm2Pz9Rt4Vb8Nc1Kd6Wf3Hj5Gs0Ty" * 3
    data = (f'const k = "{_BEGIN}\\n{body}\\n' f'{_END}\\n";').encode()

    assert preview_files.contains_credential(server, data) is True


# ── The vendor catalog scans in linear time and holds long values whole ──


def _alnum(length: int, seed: int) -> str:
    rng = random.Random(seed)
    return "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(length))


@pytest.mark.parametrize(
    "token",
    [
        # A service-account token and a base64-encoded JWT run long: the
        # catalog reads the secret's own repeat as upstream wrote it, unbounded.
        "ops_" "eyJ" + _alnum(900, 1),
        "ZXlKaGJHY2lPaU" + _alnum(400, 2),
    ],
)
def test_a_long_catalog_token_is_redacted_whole(token: str) -> None:
    out = _redact(f"x {token} y")

    assert token[-40:] not in out
    assert token[:12] not in out


@pytest.mark.parametrize("length", [260, 700])
@pytest.mark.parametrize("size", [1, 64])
@pytest.mark.parametrize(
    "head",
    # A token after its keyword and one blank, and tokens that are the whole
    # match (the keyword is the token's own prefix).
    ["Authorization: FlyV1 fm2_", "x ATATT3", "x EAAC", "x ZXlKaGJHY2lPaU"],
)
def test_the_stream_holds_a_long_whole_token_like_the_batch(
    head: str, length: int, size: int
) -> None:
    text = f"{head}{_alnum(length, length + size)} end"
    batch = _redact(text)
    assert "[REDACTED" in batch

    assert _stream(text, size) == batch


def _catalog_patterns() -> dict[str, str]:
    from kiro_crew.security import secret_catalog

    return {rule.rule_id: rule.regex.pattern for rule in secret_catalog._load().rules}


def test_an_overlapping_catalog_prefix_repeat_is_bounded() -> None:
    # ``incoming+`` then ``0-`` repeated backtracked quadratically while the
    # prefix repeats were unbounded; the generator bounds every repeat outside
    # the secret's own group, and a bounded repeat costs constant work per start.
    pattern = _catalog_patterns()["catalog:gitlab-incoming-mail-address-token"]

    assert r"(?:[A-Za-z0-9._-]{1,256}-)?\d{1,256}-" in pattern
    assert "([A-Za-z0-9_-]+)" in pattern  # the secret is read whole


@pytest.mark.parametrize("length", [120, 210, 300, 450])
@pytest.mark.parametrize("size", [2, 8, 4096])
@pytest.mark.parametrize("after", [" done", "\nend", "\nAuthorization: Bearer x"])
def test_the_stream_holds_a_long_catalog_value_whole(length: int, size: int, after: str) -> None:
    value = _alnum(length, length)
    text = f"here snowflake token: {value}{after}"
    batch = _redact(text)
    assert value not in batch

    assert _stream(text, size) == batch


# ── Without the canonical redactor, the bundle gate refuses a bare secret key ──


def test_the_standalone_bundle_gate_refuses_a_bare_secret_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.apps.builtins.aws_control.crew.packaging.pipeline import scan

    monkeypatch.setattr(scan, "_CANONICAL_REDACTOR", None)
    key = "k3Jz9Qp2Lm8Xv7Rt4Ws1" "Yb6Nc0Hd5Fg2Ae9Ui3Op"

    kinds = [leak.kind for leak in scan.scan_text(f"note {key} end", "bundle")]

    assert "bare-secret" in kinds


def test_the_bundle_gate_refuses_a_bare_secret_key_with_the_redactor_loaded() -> None:
    from kiro_crew.apps.builtins.aws_control.crew.packaging.pipeline import scan

    assert scan._CANONICAL_REDACTOR is not None
    key = "Zk3p9QmX2vLr8TnB4yWc" "7HsD1jFa6GuE0oKiRtYq"

    kinds = [leak.kind for leak in scan.scan_text(f"value is {key} end", "bundle")]

    assert "bare-secret" in kinds


# ── Offsets survive a lower-case that changes a string's length ──


@pytest.mark.parametrize("size", [1, 4, 64])
def test_a_dotted_capital_i_before_a_catalog_keyword_streams_intact(size: int) -> None:
    text = "İZMİR'DEKİ SUNUCU İÇİN key"

    assert _stream(text, size) == text


# ── An STS session token is a secret wherever it appears ──


def _b64(length: int, seed: int) -> str:
    rng = random.Random(seed)
    return "".join(rng.choice(string.ascii_letters + string.digits + "+/") for _ in range(length))


@pytest.mark.parametrize("prefix", ["IQoJb3Jp" "Z2lu", "FwoGZXIv" "YXdz"])
def test_an_sts_session_token_is_redacted_beside_its_key_pair(prefix: str) -> None:
    token = prefix + _b64(300, 1)
    secret = _b64(40, 2)
    text = (
        f"ReadOnlyCredentials(access_key='AKIA{'Z' * 16}', "
        f"secret_key='{secret}', token='{token}')"
    )

    out = _redact(text)

    assert token[20:80] not in out
    assert secret not in out
    assert _stream(text, 16) == out


# ── A name/value pair is held from its name until the value closes ──


@pytest.mark.parametrize("length", [200, 450])
@pytest.mark.parametrize("size", [1, 16])
def test_a_long_value_beside_its_secret_name_streams_redacted(length: int, size: int) -> None:
    value = _alnum(length, length)
    text = json.dumps({"name": "AWS_SESSION_TOKEN", "value": value}) + " ok\n"
    batch = _redact(text)
    assert value[100:140] not in batch

    assert _stream(text, size) == batch


# ── A catalog cut keeps the keyword that activates the rule ──


@pytest.mark.parametrize("line", ["airtable: {pat}\nnext\n", "my airtable token is {pat} ok\n"])
@pytest.mark.parametrize("size", [1, 16])
def test_a_keyword_gated_token_streams_with_its_keyword(line: str, size: int) -> None:
    rng = random.Random(4)
    body = "".join(rng.choice("abcdefghijKLMNOP0123456789") for _ in range(14))
    pat = "pat" + body + "." + "".join(rng.choice("0123456789abcdef") for _ in range(64))
    text = line.format(pat=pat)
    batch = _redact(text)
    assert pat not in batch

    assert _stream(text, size) == batch


# ── The log filter reads a signed link nested percent-encoded in another ──


@pytest.mark.parametrize("name", ["X-Amz-Security-Token", "X-Amz-Signature", "S%69gnature"])
def test_the_log_filter_drops_a_signing_value_behind_an_encoded_separator(name: str) -> None:
    token = _alnum(60, 9)
    text = f"error: https://b/o%3F{name}%3D{token}%26next%3D1"

    assert token not in SecretRedactionFilter([]).redact(text)


# ── A key ID's secret is redacted whole behind a label glued to it ──

_KEY = "Xq8m1n/Tz4Kp0w9RbV2y" "LcH7sDfG5jU3eA6oQiN+"  # ends with a separator
_KID = "AKIA" "2E0A8F3B244C9986"  # a made-up key ID, split so no literal sits here


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        (f"{_KID} x={_KEY} end", "x="),
        (f"{_KID} cred={_KEY} end", "cred="),
        (f"{_KID},{_KEY}/path/x", "/path/x"),
        (f"https://e.com/{_KID}/{_KEY}/f.txt", "/f.txt"),
    ],
)
def test_a_paired_key_ending_in_a_separator_is_redacted_whole(text: str, kept: str) -> None:
    out = _redact(text)

    assert _KEY[-8:] not in out and _KEY[:8] not in out
    assert kept in out


# ── Labels in table cells, opposite quotes, signed links of other clouds ──


def test_a_secret_in_the_next_markdown_cell_is_redacted() -> None:
    secret = "wJalrXUtnFEMI/K7MDENG/" "bPxRfiCYEXAMPLEKEY"

    assert secret not in _redact(f"| AWS_SECRET_ACCESS_KEY | {secret} |")
    assert _redact("| SecretAccessKey | Description |") == "| SecretAccessKey | Description |"


def test_a_quoted_value_starting_with_the_other_quote_is_redacted() -> None:
    text = json.dumps({"password": "'Xq7m2pzR9tvB8nc"})

    assert "Xq7m2pzR9tvB8nc" not in _redact(text)
    assert "Xq7m2pzR9tvB8nc" not in SecretRedactionFilter([]).redact(text)


@pytest.mark.parametrize(
    "url",
    [
        "https://a.blob.core.windows.net/c/f?sv=2021&sig={v}",
        "https://storage.googleapis.com/b/o?X-Goog-Algorithm=x&X-Goog-Signature={v}",
    ],
)
def test_another_clouds_link_signature_is_redacted(url: str) -> None:
    value = _alnum(43, 7)
    text = url.format(v=value)

    assert value not in _redact(text)
    assert value not in SecretRedactionFilter([]).redact(text)
    assert _redact("def f(sig=None): pass") == "def f(sig=None): pass"


# ── A short labelled secret is redacted when base64-encoded ──


@pytest.mark.parametrize("password", ["abcdefgh", "Xq7m2pzR"])
def test_a_short_encoded_password_is_redacted(password: str) -> None:
    import base64

    blob = base64.b64encode(
        json.dumps({"password": password}, separators=(",", ":")).encode()
    ).decode()

    assert blob not in _redact(f"x {blob} y")


# ── A vendor rule with two side-by-side repeats stays fast ──


def test_no_catalog_rule_repeats_one_class_twice_side_by_side() -> None:
    # ``X{20,1000}X{0,24}`` is a split point at every length to a backtracking
    # engine (``caiskey=`` repeated stalled the gateway); the generator merges it.
    adjacent = re.compile(r"(\[(?:\\.|[^\]\\])+\])\{\d+,\d+\}\1\{\d+,\d+\}")

    assert [rid for rid, p in _catalog_patterns().items() if adjacent.search(p)] == []
    alibaba = _catalog_patterns()["catalog:alibaba-sts-security-token"]
    assert "(CAIS[A-Za-z0-9+/_=-]{20,1024})" in alibaba


# ── An inline bitmap with a key pair glued into its body is not exempted ──


def test_a_bitmap_body_carrying_a_key_pair_is_screened() -> None:
    import base64
    import struct

    from kiro_crew.apps.builtins.pptx_maker.backend import routes

    head = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
    raw = head + struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0) + bytes(range(256)) * 4
    body = base64.b64encode(raw).decode().rstrip("=")
    glued = body + "AKIA" "ZX9QW3ER7TY1UI5O/" + "Xq8m1nTz4Kp0w9RbV2yL" "cH7sDfG5jU3eA6oQiNab"
    glued += "A" * (-len(glued) % 4)  # a whole base64 body, so it decodes

    assert routes._scanned_bitmap_bytes(glued)[1] is True
    assert routes._scanned_bitmap_bytes(base64.b64encode(raw).decode())[1] is False


# ── The stream holds a label the batch reads only through codes or escapes ──

_AWS_DOC_KEY = "wJalrXUtnFEMI/K7MDENG/" "bPxRfiCYEXAMPLEKEY"


@pytest.mark.parametrize(
    "text",
    [
        f"\x1b[33maws_secret_access_key\x1b[0m = {_AWS_DOC_KEY} and more\n",
        f"Some prose here. | AWS_SECRET_ACCESS_KEY | {_AWS_DOC_KEY} |\n",
    ],
)
@pytest.mark.parametrize("size", [1, 7])
def test_a_coloured_or_tabled_aws_label_streams_like_the_batch(text: str, size: int) -> None:
    batch = _redact(text)
    assert _AWS_DOC_KEY not in batch

    assert _stream(text, size) == batch


@pytest.mark.parametrize("length", [700, 1200])
def test_an_escaped_session_token_longer_than_the_floor_streams_redacted(length: int) -> None:
    value = _alnum(length, length)
    text = json.dumps(json.dumps({"SessionToken": value})) + " ok\n"
    batch = _redact(text)
    assert value[-30:] not in batch

    assert _stream(text, 16) == batch


# ── A vendor's hex key is its key, not a digest ──


def test_a_hex_key_beside_its_vendor_keyword_is_redacted() -> None:
    hexkey = "9f8e7d6c5b4a3928" "1706f5e4d3c2b1a098765432"

    assert hexkey not in _redact(f"datadog_app_key = {hexkey}")
    assert _redact(f"commit key = {hexkey}") == f"commit key = {hexkey}"


# ── An encoded ``&`` ends a value, so the next signing value is read ──


def test_a_signing_value_after_an_ordinary_encoded_parameter_is_dropped() -> None:
    token = _alnum(60, 11)
    text = f"error: https://b/o%3Ffoo%3Dbar%26X-Amz-Security-Token%3D{token}%26n%3D1"

    assert token not in SecretRedactionFilter([]).redact(text)


# ── The bundle gate leaves an integrity digest alone ──


def test_the_bundle_gate_keeps_a_subresource_integrity_digest() -> None:
    import base64
    import hashlib

    from kiro_crew.apps.builtins.aws_control.crew.packaging.pipeline import scan

    digest = base64.b64encode(hashlib.sha256(b"hello world").digest()).decode()
    text = f'<script integrity="sha256-{digest}"></script>'

    assert [leak.kind for leak in scan.scan_text(text, "skill")] == []


# ── Short PINs go; code names and escaped labels in templates stay ──


@pytest.mark.parametrize("text", ["PASSWORD=482913", "pin_password: 7391"])
def test_a_pin_behind_a_password_label_is_redacted(text: str) -> None:
    digits = text.rsplit("=", 1)[-1].rsplit(" ", 1)[-1]

    assert digits not in _redact(text)
    assert digits not in SecretRedactionFilter([]).redact(text)


@pytest.mark.parametrize(
    "text",
    [
        "password = new_password",
        "self.password = password_hash",
        "var api_key = userApiKey || ''",
        "lane_pass: '\\u7eff\\u706f'",
        "check(password=pw_value)",
    ],
)
def test_code_that_names_a_secret_is_left_alone(text: str) -> None:
    assert _redact(text) == text
    assert SecretRedactionFilter([]).redact(text) == text


def test_the_shipped_pr_watch_template_renders_its_labels() -> None:
    from pathlib import Path

    from kiro_crew.security.redaction import redact_credentials

    root = Path(__file__).resolve().parents[1]
    template = root / "src/kiro_crew/dashboard_templates/builtin/pr-watch/template.html"
    text = template.read_text(encoding="utf-8")

    assert "[REDACTED" not in redact_credentials(text)[0]


# ── A long quoted value is removed whole, commas and all ──


@pytest.mark.parametrize("length", [600, 1200])
def test_a_long_quoted_password_with_commas_is_redacted_whole(length: int) -> None:
    value = ",".join(_alnum(9, i) for i in range(length // 10))
    text = json.dumps({"password": value}) + " ok\n"
    batch = _redact(text)

    assert value[-40:] not in batch
    assert value[-40:] not in SecretRedactionFilter([]).redact(text)
    assert _stream(text, 16) == batch


# ── A factory-default Basic pair blocks a publish ──


def test_a_default_basic_pair_is_a_credential_finding() -> None:
    from kiro_crew.deploy.scan import is_credential_finding, scan_content

    assert [
        f for f in scan_content("Authorization: Basic YWRtaW46YWRtaW4=") if is_credential_finding(f)
    ]


@pytest.mark.parametrize("length", [1400, 3000])
def test_a_long_vendor_token_streams_whole_in_large_deltas(length: int) -> None:
    text = f"Authorization: FlyV1 fm2_{_alnum(length, length)} end\n"
    batch = _redact(text)
    assert "[REDACTED" in batch

    assert _stream(text, 100) == batch


# ── Holds that sit beside an AWS anchor or a label still in the window ──


def _every_split_matches(text: str) -> bool:
    batch = _redact(text)
    for k in range(1, len(text)):
        redactor = StreamRedactor()
        out = redactor.feed(text[:k]) + redactor.feed(text[k:]) + redactor.flush()
        if out != batch:
            return False
    return True


@pytest.mark.parametrize(
    "text",
    [
        f"{_KID} password=hunter2hunter2 y5WegBnL6cZ/0IU7y6Gi/" "SCHYLsCBil6u/uDs58S\n",
        'api_key: "qcYVoRQpoPPDl7ylLW";secret=abcdefgh12345\n',
    ],
)
def test_every_single_split_streams_like_the_batch(text: str) -> None:
    assert _every_split_matches(text)


@pytest.mark.parametrize("size", [16, 100])
def test_an_unlabelled_sts_token_streams_whole(size: int) -> None:
    token = "IQoJb3Jp" "Z2lu" + _alnum(1200, 3)
    text = f"tok {token} end\n"
    batch = _redact(text)
    assert token[100:140] not in batch

    assert _stream(text, size) == batch


# ── An encoded ``&`` inside a labelled value does not end it in the view ──


def test_an_encoded_ampersand_inside_a_labelled_value_keeps_it_whole() -> None:
    text = "https://x/?%63lient_secret%3DAbcdefghijk1%26Mnopqrstuvw2"

    assert "Mnopqrstuvw2" not in _redact(text)


# ── Publishing: sanitized links, captions and locale strings ──


@pytest.mark.parametrize(
    "text",
    [
        "X-Amz-Signature=[REDACTED: credential]",
        '<label for="password">Password:</label>',
    ],
)
def test_sanitized_or_caption_text_does_not_hard_block_a_publish(text: str) -> None:
    from kiro_crew.deploy.scan import is_credential_finding, scan_content

    assert not [f for f in scan_content(text) if is_credential_finding(f)]


def test_a_quoted_passphrase_with_spaces_still_blocks_a_publish() -> None:
    from kiro_crew.deploy.scan import is_credential_finding, scan_content

    text = '{"password": "correct horse battery staple"}'

    assert [f for f in scan_content(text) if is_credential_finding(f)]


@pytest.mark.parametrize("text", ['{"db_pass": "4821"}', '{"pwd": "739104"}'])
def test_a_quoted_pin_behind_a_weak_label_is_redacted(text: str) -> None:
    digits = json.loads(text).popitem()[1]

    assert digits not in _redact(text)
    assert digits not in SecretRedactionFilter([]).redact(text)


@pytest.mark.parametrize("canonical", [True, False])
def test_the_bundle_gate_reads_an_encoded_signing_name(
    canonical: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.apps.builtins.aws_control.crew.packaging.pipeline import scan

    if not canonical:
        monkeypatch.setattr(scan, "_CANONICAL_REDACTOR", None)
        monkeypatch.setattr(scan, "_CANONICAL_CREDENTIAL_RE", None)
    sig = "0123456789abcdef" * 4
    text = f"https://b.s3.amazonaws.com/o?X-Amz-Algorithm=a&X-Amz-S%69gnature={sig}"

    assert scan.scan_text(text, "prompt")


# ── A cut never parts an Authorization label from its token ──


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Bearer abcDEF0123456789xyzABCdef\x1b[0m done\n",
        "curl -H 'Authorization: Bearer abcDEF0123456789xyzABCdef'\x1b[0m done\n",
        "curl -H 'Authorization: Bearer abcDEF0123456789xyzABCdef' done\n",
    ],
)
def test_a_bearer_token_before_a_quote_or_escape_streams_redacted(text: str) -> None:
    batch = _redact(text)
    assert "abcDEF0123456789" not in batch

    assert _stream(text, 1) == batch
    assert _every_split_matches(text)


# ── A caption is an unquoted repeat of its label; a quoted one is a literal ──


def test_a_quoted_password_equal_to_its_label_word_is_redacted() -> None:
    assert "Password!" not in _redact('{"password":"Password!"}')
    assert _redact('<label for="password">Password:</label>') == (
        '<label for="password">Password:</label>'
    )


def test_the_catalog_screen_uses_a_start_method_every_platform_has() -> None:
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "scripts/gen_secret_catalog.py").read_text(
        encoding="utf-8"
    )
    contexts = [
        node.args[0].value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", "") == "get_context"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ]

    assert contexts == ["spawn"]


# ── Percent-decoding never turns a value's character into a delimiter ──


@pytest.mark.parametrize("code", ["%22", "%27", "%5C", "%26"])
def test_an_encoded_quote_or_delimiter_inside_a_value_keeps_it_whole(code: str) -> None:
    text = '{"\\u0070assword": "Xq7m2pzR' + code + 'tvB8nc1kd6wf"}'

    out = _redact(text)

    assert "tvB8nc1kd6wf" not in out and "Xq7m2pzR" not in out


# ── The keyword memo never keeps a large text alive ──


def test_the_keyword_memo_drops_a_large_text() -> None:
    from kiro_crew.security import secret_catalog

    big = "x " * 100_000
    secret_catalog.catalog_keyword_start(big)

    assert secret_catalog._last_keyword_start[0] is not big
    small = "heroku key = abc"
    first = secret_catalog.catalog_keyword_start(small)
    assert secret_catalog.catalog_keyword_start(small) == first


# ── The artifact-deploy scripts scan with the canonical scanner ──


def _deploy_scan_block(script: str) -> str:
    root = Path(__file__).resolve().parents[1]
    source = (root / "src/kiro_crew/deploy/skills/artifact-deploy/scripts" / script).read_text(
        encoding="utf-8"
    )
    start = source.index("<<'PYEOF'\n") + len("<<'PYEOF'\n")
    return source[start : source.index("\nPYEOF", start)]


@pytest.mark.parametrize("script", ["_common.sh", "deploy.sh"])
def test_the_deploy_script_scan_refuses_a_percent_encoded_secret(
    script: str, tmp_path: Path
) -> None:
    import subprocess
    import sys

    key = "Xq8m1nTz4Kp0w9RbV2yL" "cH7sDfG5jU3eA6oQiNab"
    encoded = "".join(f"%{ord(c):02X}" for c in key)
    (tmp_path / "asset.json").write_text(json.dumps({"SecretAccessKey": encoded}), encoding="utf-8")
    (tmp_path / "ok.txt").write_text("api_key: YOUR_API_KEY_HERE\n", encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}

    result = subprocess.run(
        [sys.executable, "-", str(tmp_path)],
        input=_deploy_scan_block(script),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,
    )

    assert result.returncode == 2, result.stderr
    assert result.stdout.strip().endswith("asset.json")


# ── A quoted value past the hold bound is dropped whole, never released ──


def _long_value(length: int, seed: int, extra: str = "") -> str:
    rng = random.Random(seed)
    return "".join(rng.choice(string.ascii_letters + string.digits + extra) for _ in range(length))


@pytest.mark.parametrize("length", [5000, 9000])
@pytest.mark.parametrize("size", [7, 100, 1000])
@pytest.mark.parametrize(
    "shape",
    ["json", "double_json", "aws_json", "single_quoted"],
)
def test_a_quoted_value_past_the_hold_bound_streams_like_the_batch(
    length: int, size: int, shape: str
) -> None:
    value = _long_value(length, length, '\\"')
    text = {
        "json": json.dumps({"password": value}) + " ok\n",
        "double_json": json.dumps(json.dumps({"SessionToken": value})) + " ok\n",
        "aws_json": json.dumps({"aws_secret_access_key": value}) + " x\n",
        "single_quoted": "pwd: '" + value.replace("'", "") + "' and more\n",
    }[shape]
    batch = _redact(text)
    assert value[-30:] not in batch and value[:30] not in batch

    assert _stream(text, size) == batch


@pytest.mark.parametrize("size", [7, 100, 1000])
@pytest.mark.parametrize(
    "opener, closer, label",
    [("%22", "%22", "password="), ("%27", "%27", "pwd=")],
)
def test_a_percent_quoted_value_past_the_hold_bound_streams_like_the_batch(
    size: int, opener: str, closer: str, label: str
) -> None:
    value = _long_value(4300, 4300)
    # Mid-line: an encoded-quote value, not a config line's literal.
    text = "x " + label + opener + value + closer + " tail ok"
    batch = _redact(text)
    assert value[-30:] not in batch and batch.endswith(" tail ok")

    assert _stream(text, size) == batch


@pytest.mark.parametrize("size", [7, 100, 1000])
def test_a_percent_quoted_value_with_an_escaped_quote_is_dropped_through_its_close(
    size: int,
) -> None:
    # The batch keeps the encoded quotes around its tag; once the value passes
    # the hold bound the stream drops the pair with it. Neither releases a byte.
    value = _long_value(4300, 4301)
    text = 'password=%22ab\\"' + value + "%22 tail ok"
    out = _stream(text, size)

    assert all(value[i : i + 12] not in out for i in range(0, len(value) - 12, 12))
    assert out in (_redact(text), "password=[REDACTED: credential] tail ok")


@pytest.mark.parametrize("filler", [4000, 4050, 4090])
def test_an_escape_cut_where_the_long_value_drop_starts_stays_in_the_drop(filler: int) -> None:
    tail = "SECRETTAIL" + _long_value(50, 9)
    text = '{"password": "' + _long_value(filler, filler) + '\\"' + tail + '"} ok\n'
    cut = text.index('\\"') + 1
    stream = StreamRedactor()
    out = stream.feed(text[:cut]) + stream.feed(text[cut:]) + stream.flush()

    assert tail[10:30] not in out
    assert out == _redact(text)


@pytest.mark.parametrize("size", [7, 16, 100])
def test_a_long_value_ending_in_an_escaped_backslash_keeps_its_close(size: int) -> None:
    text = '{"api_key": "' + _long_value(4000, 1) + '\\\\\\\\"} TAILKEEP\n'

    assert _stream(text, size) == _redact(text)


@pytest.mark.parametrize("size", [100, 1000])
def test_a_long_value_three_json_levels_deep_is_dropped_to_the_line_end(size: int) -> None:
    value = _long_value(6000, 6001)
    text = json.dumps(json.dumps(json.dumps({"password": value}))) + " ok\n"
    out = _stream(text, size)

    assert all(value[i : i + 16] not in out for i in range(0, len(value) - 16, 16))
    assert out.endswith("[REDACTED: credential]\n")


@pytest.mark.parametrize("length, size", [(1000, 3), (4200, 1000)])
def test_a_second_long_labelled_value_on_the_line_is_held_with_its_label(
    length: int, size: int
) -> None:
    # Compact JSON before the second label is not part of its word, and the
    # first label is not parted from its closed value by the second's cut.
    value = _long_value(length, length + 3)
    text = '{"password": "' + value + '","api_key": "' + value[::-1] + '", "u": "ok"}\nnext\n'
    out = _stream(text, size)

    for secret in (value, value[::-1]):
        assert all(secret[i : i + 12] not in out for i in range(0, length - 12, 12))
    assert out == _redact(text)


@pytest.mark.parametrize("length", [300, 600, 2000, 3500])
@pytest.mark.parametrize("suffix", ["x" * 200, "%20u%3D" + "z" * 100])
def test_a_closed_value_behind_an_encoded_label_is_never_split_by_the_floor(
    length: int, suffix: str
) -> None:
    value = _long_value(length, length)
    text = "q=password%3D%27" + value + "%27" + suffix + " end\n"
    out = _stream(text, 100)

    assert all(value[i : i + 12] not in out for i in range(0, length - 12, 12))
    # The batch may take a run glued to the closing quote with the value.
    assert out.startswith("q=password%3D[REDACTED: credential]") and out.endswith(" end\n")


@pytest.mark.parametrize(
    "label, quote",
    [("password=", "%22"), ("pwd=", "%27"), ("password=", '"'), ("pwd: ", "'")],
)
@pytest.mark.parametrize("offset", range(-2, 6))
def test_a_long_quoted_value_split_at_its_close_streams_like_the_batch(
    label: str, quote: str, offset: int
) -> None:
    # A chunk ending inside the closing quote, or right on it.
    value = _long_value(4500, 4500)
    text = label + quote + value + quote + " tail ok"
    cut = len(label + quote + value) + offset
    stream = StreamRedactor()
    out = stream.feed(text[:cut]) + stream.feed(text[cut:]) + stream.flush()

    assert out == _redact(text)


# ── Both decodes are read: a label in encoded quotes, a value with them ──

_FORM_SECRET = "Xq7m2pzRtvB8" "nc1kd6wf"


@pytest.mark.parametrize(
    "text",
    [
        "body=%7B%22password%22%3A%22" + _FORM_SECRET + "%22%7D",
        "q=%27password%27%3A%27" + _FORM_SECRET + "%27",
        "data=%7B%27api_key%27%3A%20%27" + _FORM_SECRET + "%27%7D",
        "%22Authorization%22%3A%22Bearer%20" + _FORM_SECRET + "%22",
        "%22password%22=" + _FORM_SECRET,
        "%257B%2522password%2522%253A%2522" + _FORM_SECRET + "%2522%257D",
        "x=%7B%5C%22password%5C%22%3A%5C%22" + _FORM_SECRET + "%5C%22%7D",
        '{"\\u0070assword": "Xq7m2pzR\\"tvB8nc1kd6wf"}',
        '{"\\u0070assword": "Xq7m2pzR\\u0022tvB8nc1kd6wf"}',
    ],
)
def test_a_form_encoded_or_escaped_secret_is_redacted_everywhere(text: str) -> None:
    from kiro_crew.deploy.scan import is_credential_finding, scan_content

    out = _redact(text)

    assert "tvB8nc1kd6wf" not in out and "Xq7m2pzR" not in out
    assert _stream(text, 5) == out
    assert [f for f in scan_content(text) if is_credential_finding(f)]


# ── Only the redactors' own tags are fixed points ──

_TAG_SECRET = "wJalrXUtnFEMIxK7MDEN" "GbPxRfiCYEXAMPLEKEYab"


@pytest.mark.parametrize(
    "text",
    [
        f'aws_secret_access_key="[REDACTED: {_TAG_SECRET}]"',
        f'password="[REDACTED {_TAG_SECRET}]"',
        f'password: "[REDACTED-{_TAG_SECRET[:12]}]"',
    ],
)
def test_a_secret_dressed_as_a_tag_is_redacted(text: str) -> None:
    assert _TAG_SECRET[:12] not in _redact(text)
    assert _TAG_SECRET[:12] not in _stream(text, 7)


@pytest.mark.parametrize(
    "tag",
    ["[REDACTED]", "[REDACTED: credential]", "[REDACTED: encoded credential]", "[REDACTED-SSO]"],
)
def test_a_real_tag_behind_a_label_stays_as_written(tag: str) -> None:
    assert _redact(f'password="{tag}"') == f'password="{tag}"'


# ── A view window never cuts a token: its prefix is read with it ──


@pytest.mark.parametrize("encoded_at", [100, 700, 900])
def test_a_long_token_encoded_far_from_its_prefix_is_redacted(encoded_at: int) -> None:
    token = "ATATT3x" + _long_value(993, 7, "_-=")
    encoded = token[:encoded_at] + "%" + format(ord(token[encoded_at]), "02X")
    text = "x " + encoded + token[encoded_at + 1 :] + " y"

    out = _redact(text)
    assert token[:30] not in out and token[-30:] not in out
    assert out.startswith("x ") and out.endswith(" y")


# ── A view window inside a PEM block reads the whole block ──


@pytest.mark.parametrize("footer_space", ["%20", " "])
def test_a_pem_key_encoded_deep_in_its_body_is_redacted_whole(footer_space: str) -> None:
    lines = [_long_value(64, seed, "+/") for seed in range(40)]
    body = "\n".join(lines)
    body = body[:1000] + "%" + format(ord(body[1000]), "02X") + body[1001:]
    begin = "-----BEGIN RSA PRIVATE " + "KEY-----"
    end = "-----END RSA PRIVATE" + footer_space + "KEY-----"
    out = _redact("log: " + begin + "\n" + body + "\n" + end + "\nafter ok")

    assert not [line for line in lines if line[:20] in out]
    assert out.startswith("log: ") and out.endswith("\nafter ok")


# ── The log filter: a link inside a value, and only whole tags kept ──

_LOG_SIG = "0123456789abcdef" * 4


@pytest.mark.parametrize(
    "line",
    [
        f"source url=https://b.s3.amazonaws.com/o?X-Amz-S%69gnature={_LOG_SIG}&x=1",
        f"url=https://h/o?S%69gnature=[REDACTED:{_LOG_SIG}]",
        f"https://h/o?Signature=[REDACTED:{_LOG_SIG}]",
        f"https://h/o?X-Amz-Signature=[REDACTED{_LOG_SIG}]",
    ],
)
def test_the_log_filter_drops_a_signature_inside_a_value_or_a_forged_tag(line: str) -> None:
    assert _LOG_SIG[:16] not in SecretRedactionFilter([]).redact(line)


@pytest.mark.parametrize(
    "line",
    [
        "https://h/o?Signature=[REDACTED]&a=1",
        "https://h/o?X-Amz-Signature=[REDACTED: credential] ok",
    ],
)
def test_the_log_filter_keeps_a_whole_tag_as_written(line: str) -> None:
    assert SecretRedactionFilter([]).redact(line) == line


# ── An unquoted value behind an encoded label is held, then dropped ──


@pytest.mark.parametrize("length", [520, 1200, 6000])
@pytest.mark.parametrize("size", [7, 1000])
@pytest.mark.parametrize("tail", ["&x=tail", "%27 tail", "%22 tail"])
def test_an_unquoted_value_behind_an_encoded_label_never_streams_bare(
    length: int, size: int, tail: str
) -> None:
    value = _long_value(length, length)
    opener = "%22%22" if tail.startswith("%22") else ""
    text = "x q=password%3D" + opener + value + tail + "\nnext line\n"
    out = _stream(text, size)

    assert value[:30] not in _redact(text)
    assert all(value[i : i + 12] not in out for i in range(0, length - 12, 12))
    assert out.endswith("\nnext line\n")


# ── The log filter reads an unlabelled key pair; blocking reads whole tokens ──

_PAIR_KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"
_PAIR_SECRET = "wJalrXUtnFEMI" + "/K7MDENG/bPxRfiCYEXAMPLEKEY"


@pytest.mark.parametrize(
    "line",
    [
        f"{_PAIR_KEY_ID},{_PAIR_SECRET}\n",
        f"user,{_PAIR_KEY_ID},{_PAIR_SECRET},2026",
        f"{_PAIR_SECRET} {_PAIR_KEY_ID}",
    ],
)
def test_the_log_filter_drops_the_secret_of_an_unlabelled_key_pair(line: str) -> None:
    out = SecretRedactionFilter([]).redact(line)
    assert _PAIR_SECRET[:20] not in out
    assert _PAIR_KEY_ID in out


def test_the_preview_refuses_a_long_token_escaped_far_from_its_prefix() -> None:
    token = "ATATT3x" + _long_value(993, 5, "_-=")
    escaped = token[:900] + "\\u%04x" % ord(token[900]) + token[901:]
    js = 'const t = "' + escaped + '";\n'
    assert preview_files.contains_credential(None, js.encode())


# ── A base64-encoded secret longer than the floor streams as one tag ──


def _encoded_secret(length: int, seed: int) -> str:
    import base64

    return base64.b64encode(("SessionToken=" + _long_value(length, seed)).encode()).decode()


@pytest.mark.parametrize("length", [900, 3000])
@pytest.mark.parametrize("size", [7, 50, 1000])
@pytest.mark.parametrize("wrapped", [False, True])
def test_an_encoded_secret_past_the_floor_streams_like_the_batch(
    length: int, size: int, wrapped: bool
) -> None:
    import textwrap

    encoded = _encoded_secret(length, length)
    body = "\n".join(textwrap.wrap(encoded, 76)) if wrapped else encoded
    text = "data:\n" + body + "\nnext line ok\n"
    out = _stream(text, size)

    assert all(encoded[i : i + 16] not in out for i in range(0, len(encoded) - 16, 16))
    assert out == _redact(text)
    assert "[REDACTED: encoded credential]" in out and out.endswith("\nnext line ok\n")


@pytest.mark.parametrize("size", [7, 100])
def test_a_benign_base64_run_past_the_floor_streams_unchanged(size: int) -> None:
    import base64

    rng = random.Random(3)
    data = base64.b64encode(bytes(rng.randrange(256) for _ in range(3000))).decode()
    text = "data:image/png;base64," + data + " done\n"
    assert _stream(text, size) == text == _redact(text)


def test_the_crew_export_scan_does_not_refuse_a_public_digest() -> None:
    import base64
    import hashlib

    from kiro_crew.apps.builtins.aws_control.crew.packaging.pipeline import scan

    digest = base64.b64encode(hashlib.sha256(b"0").digest()).decode()
    assert scan._CANONICAL_REDACTOR is not None
    assert scan.scan_text(f"sha256: {digest}\n", "prompt") == []


@pytest.mark.parametrize("size", [7, 100, 1000])
def test_a_dotless_eyj_run_is_dropped_through_its_base64_tail(size: int) -> None:
    # Base64 of JSON opens with ``eyJ`` like a JWT segment; its ``/`` and ``+``
    # must not end the drop and stream the encoded password.
    import base64

    raw = json.dumps({"note": _long_value(5001, 4), "password": "Ab?c/d+e=fG7h?Z9" * 2})
    encoded = base64.b64encode(raw.encode()).decode()
    text = "out " + encoded + " end\nnext\n"
    out = _stream(text, size)

    assert all(encoded[i : i + 12] not in out for i in range(len(encoded) - 400, len(encoded) - 11))
    assert out.endswith(" end\nnext\n")


def test_token_sized_deltas_of_a_base64_run_share_one_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64

    from kiro_crew.security import redaction_labels

    rng = random.Random(6)
    data = base64.b64encode(bytes(rng.randrange(256) for _ in range(3000))).decode()
    text = "data:image/png;base64," + data + " done\n"
    passes: list[int] = []
    hold = redaction_labels.stream_hold_start

    def counted(buf: str) -> int | None:
        passes.append(len(buf))
        return hold(buf)

    monkeypatch.setattr(redaction_labels, "stream_hold_start", counted)
    stream = StreamRedactor()
    out = "".join(stream.feed(text[i : i + 4]) for i in range(0, len(text), 4)) + stream.flush()

    assert out == text
    # One read of the held buffer per few dozen characters, not per delta.
    assert len(passes) <= len(text) // 60 + 10


# ── A name/value pair is held from its name field; a deploy keeps a digest ──


@pytest.mark.parametrize(
    "shape",
    [
        '{"name": "%s", "value": "%s"} and some prose follows here ok\n',
        "name=%s value=%s more\n",
    ],
)
@pytest.mark.parametrize("name", ["password", "aws_secret_access_key"])
@pytest.mark.parametrize("size", [1, 7, 50])
def test_a_name_value_pair_streams_its_value_with_its_name(
    shape: str, name: str, size: int
) -> None:
    value = _long_value(259, 7, "+") + "/" + _long_value(40, 8)
    text = shape % (name, value)
    out = _stream(text, size)

    assert value[:20] not in out and value[-20:] not in out
    assert value[-20:] not in _redact(text)


def test_the_deploy_scan_keeps_a_public_digest_beside_a_key_word() -> None:
    from kiro_crew.deploy.scan import is_credential_finding, scan_content

    digest = "11f6ad8ec52a2984" "abaafd7c3b516503785c2072"  # a 40-hex content digest
    assert not any(is_credential_finding(f) for f in scan_content(f'const key = "{digest}";'))
    secret = "wJalrXUtnFEMI" + "/K7MDENG/bPxRfiCYEXAMPLEKEz"
    assert any(is_credential_finding(f) for f in scan_content(f"aws_secret_access_key = {secret}"))


@pytest.mark.parametrize("length", [1500, 5000])
@pytest.mark.parametrize("size", [7, 50, 1000])
@pytest.mark.parametrize(
    "shape",
    [
        "- name: REGISTRY_PASSWORD\n  value: %s\n- name: next\n",
        "name: password\nvalue: %s\nafter\n",
    ],
)
def test_a_long_unquoted_sibling_value_never_streams_bare(
    length: int, size: int, shape: str
) -> None:
    value = _long_value(length, length)
    out = _stream(shape % value, size)

    assert all(value[i : i + 12] not in out for i in range(0, length - 12, 12))


@pytest.mark.parametrize("password", ["ab/cd,ef;gh!ij", "S3cr/et,Pass;word!xyz"])
@pytest.mark.parametrize("size", [1, 7, 20])
def test_a_uri_password_with_punctuation_streams_like_the_batch(password: str, size: int) -> None:
    text = f"postgres://admin:{password}@db.example.com:5432/app ok\n"
    out = _stream(text, size)

    assert out == _redact(text)
    assert password not in out


# ── Round on ec3c765eac: slash passwords, literal labels, digests, long tokens ──


@pytest.mark.parametrize("label", ["PASSWORD=", "secret: "])
def test_a_password_starting_with_a_slash_is_redacted(label: str) -> None:
    value = "/K7MDENG/bPxRfiCYEXAMPLEKEYab12"
    assert value[4:] not in _redact(label + value)
    assert value[4:] not in SecretRedactionFilter([]).redact(label + value)


@pytest.mark.parametrize("text", ["run --password=/usr/local/bin", "secret_file: ./config.yaml"])
def test_a_lower_case_path_behind_a_label_stays(text: str) -> None:
    assert _redact(text) == text


@pytest.mark.parametrize(
    "text",
    [
        'getpass.getpass("Password:") or default_value',
        'unknown key "api_key:" at line 4 of config.yaml',
    ],
)
def test_a_label_inside_a_closed_string_literal_redacts_nothing(text: str) -> None:
    assert _redact(text) == text
    assert SecretRedactionFilter([]).redact(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "NETLIFY_COMMIT_REF=3f2a9c1b7d4e6f8a0b2c4d6e8f0a1b3c5d7e9f1a",
        "datadog git.commit.sha:3f2a9c1b7d4e6f8a0b2c4d6e8f0a1b3c5d7e9f1a",
        "circleci build sha: 3f2a9c1b7d4e6f8a0b2c4d6e8f0a1b3c5d7e9f1a",
    ],
)
def test_a_commit_digest_beside_a_vendor_keyword_stays(text: str) -> None:
    assert _redact(text) == text


@pytest.mark.parametrize("length", [5000, 9000])
@pytest.mark.parametrize("size", [7, 50, 1000])
@pytest.mark.parametrize("head", ["IQoJb3JpZ2lu", "FlyV1 fm2_"])
def test_a_long_recognized_token_past_its_hold_never_streams_bare(
    length: int, size: int, head: str
) -> None:
    token = head + _long_value(length, length + 1, "+/")
    out = _stream("here " + token + " tail\nnext\n", size)

    assert all(token[i : i + 16] not in out for i in range(20, len(token) - 16, 16))
    assert out.endswith(" tail\nnext\n")


def test_token_sized_deltas_of_an_escaped_payload_share_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.security import redaction_labels

    text = '\\"password\\":\\"' * 300
    passes: list[int] = []
    hold = redaction_labels.stream_hold_start

    def counted(buf: str) -> int | None:
        passes.append(len(buf))
        return hold(buf)

    monkeypatch.setattr(redaction_labels, "stream_hold_start", counted)
    stream = StreamRedactor()
    "".join(stream.feed(text[i : i + 4]) for i in range(0, len(text), 4)) + stream.flush()

    # Past the first few hundred characters, one read per few dozen, not per delta.
    assert len(passes) <= 256 // 4 + len(text) // 40


# ── Round on 5071091d0e: vendor prefixes, XML values, nested captures ──


@pytest.mark.parametrize("size", [1, 5, 50])
def test_a_prefixed_vendor_token_is_found_without_its_vendor_name(size: int) -> None:
    token = "re_" + "ABCDEFGH" + "_" + "abcdefghijkmnopqrstuvwx" + "y"
    text = "## Resend config\nvalue:\n" + token + "\nnext\n"
    assert token not in _redact(text)
    assert token[3:] not in _stream(text, size)


@pytest.mark.parametrize(
    "text",
    [
        "<password>,Xq7m2pzRtvB8nc1k</password>\n",
        "<config><password>a,b;c(d)e!fghij</password></config> ok\n",
        "<db:password>a;b,cdefghij</db:password>\n",
    ],
)
@pytest.mark.parametrize("size", [1, 3, 7, 50])
def test_an_xml_password_is_read_through_its_closing_tag(text: str, size: int) -> None:
    value = (
        text.split(">", 2)[-1].split("</")[0]
        if text.startswith("<config")
        else (text.split(">", 1)[1].split("</")[0])
    )
    for out in (_redact(text), _stream(text, size), SecretRedactionFilter([]).redact(text)):
        assert value not in out


@pytest.mark.parametrize(
    "text", ["<label>Password:</label> and text after it\n", "a < b and c > d\n"]
)
def test_markup_that_holds_no_value_streams_unchanged(text: str) -> None:
    assert _stream(text, 1) == text == _redact(text)


@pytest.mark.parametrize(
    "args",
    [
        {"lob_key": "live_" + "0123456789abcdef" * 2 + "012"},
        {"facebook_token": "123456789012345|" + "abcdefghijklmnopqrstuvwxyz0"},
    ],
)
def test_a_nested_capture_rule_redacts_only_the_credential(args: dict[str, str]) -> None:
    out = _redact(json.dumps({"args": args}))
    parsed = json.loads(out)
    assert list(parsed["args"]) == list(args)
    assert next(iter(args.values())) not in out


# ── Round on eea5c52d8d: long XML values, nested percent encoding ──


def _xml_value(length: int) -> str:
    rng = random.Random(length)
    alphabet = string.ascii_letters + string.digits + ";"
    return "".join(rng.choice(alphabet) for _ in range(length))


@pytest.mark.parametrize("length", [600, 2250, 4100, 9000])
@pytest.mark.parametrize("size", [1, 7, 1000])
def test_a_long_xml_password_never_streams_in_pieces(length: int, size: int) -> None:
    value = _xml_value(length)
    text = f"config: <password>{value}</password> done\n"
    pieces = [value[i : i + 16] for i in range(0, len(value) - 16, 16)]
    for out in (_redact(text), _stream(text, size)):
        assert not [p for p in pieces if p in out]
        # The element's closing tag and what follows it still stream.
        assert out.endswith("</password> done\n")


def test_an_open_xml_value_that_overruns_its_reach_drops_to_its_closing_tag() -> None:
    value = _xml_value(9000)
    out = _stream(f"<api_key>{value}</api_key> and after\nnext line\n", 7)
    assert out == "<api_key>[REDACTED: credential]</api_key> and after\nnext line\n"


def _signed_link() -> tuple[str, str, str]:
    signature = "0123456789abcdef" * 4
    token = "IQoJb3JpZ2luX2VjEXAMPLE" + "Zx9" * 20
    url = (
        "https://b.s3.amazonaws.com/o?X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20260101"
        f"&X-Amz-Security-Token={token}&X-Amz-Signature={signature}"
    )
    return url, signature, token


@pytest.mark.parametrize("depth", [1, 2, 3])
def test_a_signed_link_encoded_more_than_once_loses_its_signing_values_in_logs(
    depth: int,
) -> None:
    from urllib.parse import quote

    url, signature, token = _signed_link()
    for _ in range(depth):
        url = quote(url, safe="")
    out = SecretRedactionFilter([]).redact(f"redirect next={url} ok")
    assert signature not in out
    assert token not in out
    # The key ID is an Identifier and stays visible.
    assert "AKIAIOSFODNN7EXAMPLE" in out


@pytest.mark.parametrize("name", ["Signature", "sig", "%2553ignature"])
def test_a_twice_encoded_signing_name_and_separator_are_read(name: str) -> None:
    line = f"https://x/y?a=1%2526{name}%253DSECRETSECRET1234%2526b%253D2"
    out = SecretRedactionFilter([]).redact(line)
    assert "SECRETSECRET1234" not in out
    assert out.endswith("%2526b%253D2")


@pytest.mark.timeout(30)
def test_a_long_run_of_encoded_percent_signs_is_read_in_linear_time() -> None:
    # Quadratic backtracking over the run would take minutes, not milliseconds.
    line = "x=" + "%25" * 20000 + " Signature%253DSECRET" + "%25" * 20000
    assert "SECRET" not in SecretRedactionFilter([]).redact(line)


# ── Round on 4929b11a0c: blanks around a separator ──

_PADDED_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


@pytest.mark.parametrize("label", ["aws_secret_access_key", "password", "client_secret"])
@pytest.mark.parametrize("form", ["{l}={p}{s}", "{l}{p}={s}", "{l}{p}={p}{s}", "{l}:{p}{s}"])
@pytest.mark.parametrize("pad", [" " * 9, "\t" * 9, " " * 40, " " * 600, " " * 2000])
def test_a_secret_padded_from_its_separator_is_redacted(label: str, form: str, pad: str) -> None:
    text = "[default]\n" + form.format(l=label, p=pad, s=_PADDED_SECRET) + "\nnext\n"
    outs = [_redact(text), SecretRedactionFilter([]).redact(text)]
    outs += [_stream(text, size) for size in (1, 5, 64)]
    for out in outs:
        assert _PADDED_SECRET[:20] not in out
        assert out.endswith("\nnext\n")


def test_a_padded_label_that_never_gets_a_separator_streams_unchanged() -> None:
    # The hold drops nothing: the label and every blank are released as written.
    for text in ("password" + " " * 600 + "is the field name\n", "hello" + " " * 600 + "world\n"):
        assert _stream(text, 1) == text == _redact(text)


# ── Round on f7e19150dd: encoded blanks in a value, long signing values ──


@pytest.mark.parametrize(
    "text",
    [
        "password%3DExample42%20Phrase87",
        "login?user=a&password%3DExample42%20Phrase87%26x%3D1",
        "next=%2Fcb%3Fpassword%253DExample42%2520Phrase87",
        "password%3DExample42%09Phrase87 ok",
    ],
)
@pytest.mark.parametrize("size", [1, 3, 64])
def test_an_encoded_blank_inside_a_value_does_not_end_it(text: str, size: int) -> None:
    for out in (_redact(text), _stream(text, size)):
        assert "Example42" not in out
        assert "Phrase87" not in out
    # An encoded ``&`` after the value still ends it there.
    assert _redact("login?user=a&password%3DExample42%20Phrase87%26x%3D1").endswith("%26x%3D1")


@pytest.mark.parametrize("name", ["X-Amz-Security-%54oken", "X-Amz-Security-Token", "%53ignature"])
def test_a_signing_value_longer_than_4096_is_removed_whole_from_logs(name: str) -> None:
    token = "IQoJ" + "AbCd" * 1300
    out = SecretRedactionFilter([]).redact(f"GET https://b/o?a=1&{name}={token}&x=2 done")
    assert token[-40:] not in out
    assert out.endswith("=[REDACTED]&x=2 done")


# ── Round on 31f66a2280: catalog tokens at the floor, closed label literals, bare signature ──


@pytest.mark.parametrize("size", [7, 50, 200])
def test_a_catalog_token_the_floor_would_part_is_committed_whole(size: int) -> None:
    rng = random.Random(5)
    body = "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(64))
    text = "see https://x.test/p?key=fio-u-" + body + "&other=" + "a" * 700 + " end\n"
    assert body[:16] not in _redact(text)
    out = _stream(text, size)
    for piece in (body[:16], body[24:40], body[-16:]):
        assert piece not in out
    assert out.endswith("a" * 64 + " end\n")


@pytest.mark.parametrize("size", [1, 7, 500])
def test_a_closed_label_literal_before_a_long_line_streams_unchanged(size: int) -> None:
    text = 'console.log("password:");const values=[' + ",".join(map(str, range(2000))) + "]\n"
    assert _stream(text, size) == text == _redact(text)


@pytest.mark.parametrize(
    "text", ["def verify(signature=None): pass", "check(signature=False, strict=True)"]
)
def test_a_bare_signature_parameter_in_code_is_kept_in_logs(text: str) -> None:
    assert SecretRedactionFilter([]).redact(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "https://x/y?a=1&Signature=abcDEF123456789xyz",
        "signature=" + "0123456789abcdef" * 4,
        "signature=Zk3p9QmX2vLr8TnB4yWc7HsD1jFa6GuE0oKiRtYq",
    ],
)
def test_a_bare_signature_with_a_signing_value_is_still_removed(text: str) -> None:
    value = text.split("Signature=" if "Signature=" in text else "signature=")[1]
    assert value not in SecretRedactionFilter([]).redact(text)


# ── Round on 9a2eb7d057: digest words, vendor context across commits ──


@pytest.mark.parametrize("label", ["datadog_shared_app_key", "DATADOG_SHARED_APP_KEY"])
def test_a_word_that_merely_contains_sha_does_not_mark_a_digest(label: str) -> None:
    key = "3f9a1c7e5b2d8f4a6c0e9b1d7f3a5c8e2b4d6f0a"
    out = _redact(json.dumps({label: key, "service": "datadog"}))
    assert key not in out


@pytest.mark.parametrize("label", ["git_commit_sha", "gitCommitSha", "NETLIFY_COMMIT_REF"])
def test_a_label_that_names_a_digest_still_keeps_its_value(label: str) -> None:
    from kiro_crew.security.secret_catalog import _names_digest

    assert _names_digest(label)


@pytest.mark.parametrize("size", [1, 7, 17])
@pytest.mark.parametrize(
    "heading",
    [
        "# Airtable\n",
        "# Airtable\n\nHere is the token:\n\n",
        "# Airtable\n" + "status: ready\n" * 220,
    ],
)
def test_a_vendor_keyword_committed_before_its_token_still_selects_the_rule(
    heading: str, size: int
) -> None:
    rng = random.Random(9)
    token = (
        "pat"
        + "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(14))
        + "."
        + "".join(rng.choice("0123456789abcdef") for _ in range(64))
    )
    text = heading + token + "\n"
    assert token[3:20] not in _redact(text)
    out = _stream(text, size)
    assert token[3:20] not in out
    assert token[-30:] not in out
    assert out.startswith(heading)


# ── Round on cca86870bb: memos shared across threads ──


def test_the_views_memo_is_changed_only_under_its_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.security import redaction_normalise

    lock = redaction_normalise._VIEWS_MEMO_LOCK

    class _Checked(dict):  # type: ignore[type-arg]
        def pop(self, *args):  # type: ignore[no-untyped-def]
            assert lock.locked()
            return super().pop(*args)

        def __setitem__(self, key, value):  # type: ignore[no-untyped-def]
            assert lock.locked()
            super().__setitem__(key, value)

    monkeypatch.setattr(redaction_normalise, "_VIEWS_MEMO", _Checked())
    for n in range(redaction_normalise._VIEWS_MEMO_SIZE * 3):
        redaction_normalise.normalised_views(f"value%3D{n}\\n")


def test_concurrent_redaction_of_escaped_text_never_raises() -> None:
    import threading

    errors: list[BaseException] = []

    def work(seed: int) -> None:
        try:
            for n in range(300):
                _redact(f'{{"k": "v{seed}-{n}\\n%41"}}')
        except BaseException as exc:  # noqa: BLE001 -- collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []


def test_the_keyword_memo_is_one_tuple_replaced_whole() -> None:
    from kiro_crew.security import secret_catalog

    secret_catalog.catalog_keyword_start("airtable key", 0)
    assert isinstance(secret_catalog._last_keyword_start, tuple)


# ── Round on 7f21532070: source permalinks, closed signing literals ──


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/kirodotdev/KiroCrew/blob/"
        "7f21532070cf75d850b30d2725b6ea68038a5019/src/kiro_crew/security/redaction.py",
        "https://code.example.test/packages/SomeServiceInfrastructure/blobs/mainline/"
        "--/lib/constructs/pipeline.ts",
    ],
)
def test_the_bundle_gate_does_not_refuse_a_source_permalink(url: str) -> None:
    from kiro_crew.apps.builtins.aws_control.crew.packaging.pipeline import scan

    assert [leak.kind for leak in scan.scan_text(f"see {url} ok", "skill")] == []


def test_the_bundle_gate_still_refuses_a_key_that_is_a_url_path_segment() -> None:
    from kiro_crew.apps.builtins.aws_control.crew.packaging.pipeline import scan

    key = "Zk3p9QmX2vLr8TnB4yWc" "7HsD1jFa6GuE0oKiRtYq"
    url = f"https://example.test/download/{key}/file"
    assert "bare-secret" in [leak.kind for leak in scan.scan_text(url, "skill")]


@pytest.mark.parametrize(
    "text",
    [
        'const caption = "Signature:"; render("done");',
        "const c = 'X-Amz-Signature:'; f('x');",
    ],
)
def test_a_closed_signing_label_literal_keeps_the_code_after_it(text: str) -> None:
    assert _redact(text) == text


# ── Round on 2bb4566ae3: an escaped line break inside a value read through a view ──


@pytest.mark.parametrize(
    "text",
    [
        '{"pass\\u0077ord":"FakeFirst123\\nFakeLast456"}',
        '{"pass\\u0077ord":"FakeFirst123\\u000aFakeLast456"}',
        '{"pass\\u0077ord":"FakeFirst123\\tFakeLast456","x":"keep"}',
    ],
)
@pytest.mark.parametrize("size", [1, 3, 64])
def test_an_escaped_line_break_does_not_end_a_value_read_through_a_view(
    text: str, size: int
) -> None:
    for out in (_redact(text), _stream(text, size)):
        assert "FakeFirst123" not in out
        assert "FakeLast456" not in out
    assert _redact(text).startswith('{"pass\\u0077ord":"[REDACTED: credential]"')


# ── Round on eeee8273d9: YAML block scalars under a secret label ──

_BLOCK_KEY = "wJalrXUtnFEMI/K7MDENG/" "bPxRfiCYEXAMPLEKEY"


@pytest.mark.parametrize(
    "text",
    [
        f"stringData:\n  aws_secret_access_key: |\n    {_BLOCK_KEY}\n  region: us-east-1\n",
        f"data:\n  password: >-\n    Fake{_BLOCK_KEY[:20]}\n    Second{_BLOCK_KEY[20:]}\n  user: bob\n",
        f"aws_secret_access_key: |\n  {_BLOCK_KEY}\n",
    ],
)
@pytest.mark.parametrize("size", [1, 7, 64])
def test_a_yaml_block_scalar_under_a_secret_label_is_redacted(text: str, size: int) -> None:
    pieces = (_BLOCK_KEY[:12], _BLOCK_KEY[-12:])
    for out in (_redact(text), _stream(text, size), SecretRedactionFilter([]).redact(text)):
        assert not [p for p in pieces if p in out]
    # The keys after the block stay.
    for after in ("  region: us-east-1\n", "  user: bob\n"):
        if after in text:
            assert _redact(text).endswith(after)


def test_a_block_scalar_under_an_ordinary_key_streams_unchanged() -> None:
    text = "description: |\n  just text here\n  and more\nname: x\n"
    assert _stream(text, 1) == text == _redact(text)


# ── Round on 457b4ddc96: scheme padding, long sibling values, long YAML blocks ──


@pytest.mark.parametrize("pad", [" " * 9, " " * 40, "\t" * 9])
def test_an_authorization_scheme_padded_with_blanks_still_hides_its_credential(pad: str) -> None:
    token = "opaqueTok3nVALUE9x8y7z6w5"
    text = f"Authorization: Bearer{pad}{token}\n"
    for out in (_redact(text), _stream(text, 3), SecretRedactionFilter([]).redact(text)):
        assert token not in out


@pytest.mark.parametrize("length", [180, 6000])
@pytest.mark.parametrize("size", [1, 7, 64])
def test_a_long_unquoted_sibling_value_is_held_with_its_name(length: int, size: int) -> None:
    rng = random.Random(length)
    value = "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(length))
    value += "!Suffixtail"
    text = f"- name: DB_PASSWORD\n  value: {value}\n- name: other\n  value: plain\n"
    out = _stream(text, size)
    assert "Suffixtail" not in out
    assert value[100:130] not in out
    assert out.endswith("- name: other\n  value: plain\n")


@pytest.mark.parametrize("size", [1, 7, 64, 1000])
def test_a_yaml_block_longer_than_the_hold_reach_is_dropped_to_its_end(size: int) -> None:
    import base64

    rng = random.Random(1)
    body = base64.b64encode(bytes(rng.randrange(256) for _ in range(4800))).decode()
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    text = (
        "tls:\n  private_key: |\n"
        + "".join("    " + line + "\n" for line in lines)
        + "  region: us-east-1\nnext: x\n"
    )
    out = _stream(text, size)
    assert not [line for line in lines if line[:20] in out]
    assert out.endswith("[REDACTED: credential]\n  region: us-east-1\nnext: x\n")
