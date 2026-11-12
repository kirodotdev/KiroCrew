"""Identifier-value rows: decided by structure only, never replaced, never over a Secret."""

from __future__ import annotations

import base64
import pathlib

import pytest
from redaction_structured_cases import (
    B64,
    B64URL,
    HEX,
    assert_linear_work,
    aws_key_id,
    aws_secret,
    synth,
)

import kiro_crew.security.redaction_structured as rs
from kiro_crew.security.redaction_structured import StreamRedactor, TableError, build_table, redact

PNG_HEAD = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 7).decode()
JPEG_HEAD = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 8).decode()


def _ids(text: str) -> list[tuple[str, str]]:
    _, spans = redact(text)
    return [(text[s:e], rule) for s, e, rule, tier in spans if tier == "identifier"]


def _kept(text: str, value: str, rule: str) -> None:
    out, spans = redact(text)
    assert out == text
    assert [r for _, _, r, t in spans if t == "secret"] == []
    # The doc example key ID is also a key ID; either Identifier row keeps it.
    rules = {rule, "aws.access_key_id"} if rule == "aws.doc_example" else {rule}
    assert any(value in got and r in rules for got, r in _ids(text))


# (name, text, value, rule): the value is reported as an Identifier and kept.
# fmt: off
CASES = [
    ("amz_id_2.header", "x-amz-id-2: {v}\n", synth(74, B64, 1) + "==", "aws.x_amz_id_2"),
    ("amz_id_2.header_upper", "X-Amz-Id-2:{v}", synth(76, B64, 2), "aws.x_amz_id_2"),
    ("amz_id_2.json", '{{"HostId": "{v}", "x": 1}}', synth(74, B64, 3) + "=", "aws.x_amz_id_2"),
    ("amz_id_2.xml", "<HostId>{v}</HostId>", synth(56, B64, 4), "aws.x_amz_id_2"),
    ("request_id.header", "< x-amz-request-id: {v}\n", synth(16, "0123456789ABCDEF", 5), "aws.request_id"),
    ("request_id.json", '"RequestId": "{v}"', "a1b2c3d4-0000-4111-8222-123456789abc", "aws.request_id"),
    ("ssh.fingerprint", "256 SHA256:{v} user@host (ED25519)", synth(43, B64, 6), "ssh.fingerprint"),
    ("data_uri.markdown", "![c](data:image/png;base64,{v})", PNG_HEAD + synth(200, B64, 7) + "=", "data_uri"),
    ("data_uri.param", '<img src="data:image/svg+xml;charset=utf-8;base64,{v}">', synth(90, B64, 8), "data_uri"),
    ("data_uri.css", "url(DATA:font/woff2;base64,{v}) }}", synth(120, B64, 9), "data_uri"),
    ("image.json_thumb", '{{"thumb": "{v}"}}', PNG_HEAD + synth(300, B64, 10) + "==", "image.base64"),
    ("image.jpeg", "jpeg {v} end", JPEG_HEAD + synth(100, B64, 11), "image.base64"),
    ("doc_example.key_id", "aws_access_key_id = {v}", "AKIAIOSFODNN7" + "EXAMPLE", "aws.doc_example"),
    ("doc_example.secret", "The docs use {v} as the example.", "wJalrXUtnFEMI/K7MDENG/bPxRfiCY" + "EXAMPLEKEY", "aws.doc_example"),
    ("temp_path", "Wrote {v}/report.html", "/var/folders/ab/" + synth(30, "abc123_", 1) + "/T/tmpXy12", "macos.temp_path"),
    ("temp_path.private", "dir {v} ok", "/private/var/folders/x9/zz_ab1/C/com.apple.x", "macos.temp_path"),
    ("placeholder.angle", "export API_TOKEN={v}", "<your-token-here>", "placeholder"),
    ("placeholder.angle_upper", "key: {v}", "<YOUR_API_KEY>", "placeholder"),
    ("placeholder.gha", "GITHUB_TOKEN: {v}\n", "${{ secrets.GITHUB_TOKEN }}", "placeholder"),
    ("placeholder.braces", "api_key = {v}", "{{ api_key }}", "placeholder"),
    ("placeholder.shell_braced", "TOKEN={v}", "${GITHUB_TOKEN}", "placeholder"),
    ("placeholder.shell", "TOKEN={v} next", "$API_TOKEN", "placeholder"),
    ("placeholder.windows", "set TOKEN={v}", "%API_TOKEN%", "placeholder"),
    ("placeholder.all_x", "token {v} here", "ghp_" + "x" * 36, "placeholder"),
    ("placeholder.all_X", "id {v}", "token_" + "X" * 20, "placeholder"),
    ("placeholder.stars", "password: {v}", "********", "placeholder"),
    ("oauth.form_client_id", "grant_type=refresh_token&client_id={v}&x=1", "123-abc.apps.example.test", "oauth.form_param"),
    ("oauth.form_state_line", "POST /t\n\nstate={v}&scope=openid", synth(32, B64URL, 3), "oauth.form_param"),
]
# fmt: on


@pytest.mark.parametrize(
    ("template", "value", "rule"), [pytest.param(t, v, r, id=n) for n, t, v, r in CASES]
)
def test_identifier_value_is_reported_and_kept(template: str, value: str, rule: str) -> None:
    _kept(template.format(v=value), value, rule)


# (name, text): no Identifier of the new rows is reported.
NOT_IDENTIFIERS = [
    ("amz_id_2.short", "x-amz-id-2: " + synth(10, B64, 1)),
    ("amz_id_2.glued_label", "xx-amz-id-2: " + synth(74, B64, 1)),
    ("ssh.wrong_length", "SHA256:" + synth(44, B64, 2)),
    ("ssh.hex_digest", "sha256:" + synth(64, HEX, 2)),
    ("data_uri.not_base64", "data:text/plain,hello%20world"),
    ("image.no_signature", "blob " + synth(100, B64, 3)),
    ("doc_example.glued", "X" + "AKIAIOSFODNN7" + "EXAMPLE"),
    ("temp_path.other", "/var/foldersx/ab/cd/T/x and /var/folders/abc/d/T/x"),
    ("temp_path.truncated", "/var/folders/ab"),
    ("placeholder.angle_html", "<div class>text</div>"),
    ("placeholder.percent_encoding", "a%20b%3Dc"),
    ("placeholder.lower_shell", "echo $home"),
    ("placeholder.short_x", "xxxxx"),
    ("oauth.form_other_param", "a=1&client_secret=" + synth(30, B64URL, 4)),
]

_NEW_RULES = {
    "aws.x_amz_id_2",
    "aws.request_id",
    "ssh.fingerprint",
    "data_uri",
    "image.base64",
    "aws.doc_example",
    "macos.temp_path",
    "placeholder",
    "oauth.form_param",
}


@pytest.mark.parametrize("text", [pytest.param(t, id=n) for n, t in NOT_IDENTIFIERS])
def test_not_an_identifier_value(text: str) -> None:
    assert [r for _, r in _ids(text) if r in _NEW_RULES] == []


def test_identifier_value_never_overrides_a_secret() -> None:
    secret = aws_secret()
    text = f"x-amz-id-2: {aws_key_id()}{secret}\nAWS_SECRET_ACCESS_KEY={secret}"
    out, spans = redact(text)
    assert secret not in out.split("\n")[1]
    assert any(t == "secret" for _, _, _, t in spans)


ALL = [(n, t.format(v=v)) for n, t, v, _ in CASES] + NOT_IDENTIFIERS


def _stream(chunks: list[str]) -> tuple[str, list[tuple[int, int, str, str]]]:
    s = StreamRedactor()
    out = "".join(s.feed(c) for c in chunks) + s.flush()
    return out, s.spans


@pytest.mark.parametrize("text", [pytest.param(t, id=n) for n, t in ALL])
def test_identifier_values_stream_like_whole_text(text: str) -> None:
    whole = redact(text)
    for cut in range(len(text) + 1):
        assert _stream([text[:cut], text[cut:]]) == whole, f"split at {cut}"
    assert _stream(list(text)) == whole


def test_loader_refuses_a_field_the_kind_does_not_read() -> None:
    row = {"id": "x.y", "tier": "identifier", "detector": "structure", "kind": "exact",
           "values": ["abc"], "labels": ["q"], "max_length": 9}  # fmt: skip
    with pytest.raises(TableError, match="do not apply"):
        build_table([row])


def test_loader_refuses_upper_case_labels() -> None:
    row = {"id": "x.y", "tier": "identifier", "detector": "structure", "kind": "labelled_value",
           "labels": ["X-Amz:"], "alphabet": "b64std", "min_length": 1, "pad": 0, "max_length": 9}  # fmt: skip
    with pytest.raises(TableError, match="lower case"):
        build_table([row])


_FORBIDDEN_WORDS = ("entropy", "shannon", "log2")


def test_package_has_no_entropy_scoring() -> None:
    # Owner ruling: detection and Identifier decisions are structural only.
    pkg = pathlib.Path(rs.__file__).parent
    for path in sorted(pkg.glob("*.py")):
        source = path.read_text(encoding="utf-8").lower()
        for word in _FORBIDDEN_WORDS:
            assert word not in source, f"{path.name}: {word}"


@pytest.mark.parametrize(
    "unit",
    ["<", "<your", "$A", "%A", "{{", "${", "data:a;", "data:image/png;base64,A", "x-amz-id-2: ",
     "SHA256:", "/var/folders/ab/c/T/", "x" * 13 + "_", "*", "&state=", "\nstate=", "iVBORw0KGgo"],
)  # fmt: skip
def test_a_long_run_of_an_identifier_anchor_costs_linear_work(unit: str) -> None:
    assert_linear_work(lambda n: (unit * (n // len(unit) + 1))[:n], redact)
