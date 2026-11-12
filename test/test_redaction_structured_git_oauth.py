"""Git object names (Identifier) and the owner's exact OAuth keys in URLs (Secret)."""

from __future__ import annotations

import pytest
from redaction_structured_cases import B64URL, HEX, assert_linear_work, aws_key_id, synth

from kiro_crew.security.redaction_structured import (
    TAG,
    StreamRedactor,
    TableError,
    build_table,
    redact,
    redact_hits,
)
from kiro_crew.security.redaction_structured.stream import CONTEXT

GIT = "git.object_name"
PAIR = "aws.secret_access_key"


def _rules(text: str) -> list[tuple[str, str]]:
    return [(text[s:e], rule) for s, e, rule, _t in redact(text)[1]]


def _stream(chunks: list[str]) -> tuple[str, list[tuple[int, int, str, str]]]:
    s = StreamRedactor()
    out = "".join(s.feed(c) for c in chunks) + s.flush()
    return out, s.spans


# ── Git object names ──


@pytest.mark.parametrize("length", [40, 64])
def test_git_object_name_is_an_identifier(length: int) -> None:
    sha = synth(length, HEX, length)
    text = f"merged {sha}, see log"
    out, spans = redact(text)
    assert out == text
    assert (sha, GIT) in _rules(text)
    assert [r for _, _, r, t in spans if t == "secret"] == []


def test_sha_beside_a_key_id_stays_visible() -> None:
    # The sim52-b13 shape: a key ID and a commit SHA within the pairing window.
    sha = synth(40, HEX, 5)
    key_id = aws_key_id()
    text = f"{key_id} commit {sha}"
    out, spans = redact(text)
    assert out == text
    rules = [r for _, _, r, _t in spans]
    assert GIT in rules and PAIR not in rules


@pytest.mark.parametrize(
    "value",
    [
        synth(40, HEX, 6).upper(),
        synth(20, HEX, 6) + synth(20, HEX, 7).upper(),
        synth(39, HEX, 6),
        synth(41, HEX, 6),
        synth(63, HEX, 6),
        synth(65, HEX, 6),
    ],
    ids=["upper", "mixed_case", "39", "41", "63", "65"],
)
def test_not_a_git_object_name(value: str) -> None:
    assert GIT not in [r for _, r in _rules(f"value {value} end")]


@pytest.mark.parametrize("glue", ["g", "Z", "0x"])
def test_a_hex_run_touching_a_letter_or_digit_is_not_one(glue: str) -> None:
    sha = synth(40, HEX, 8)
    assert GIT not in [r for _, r in _rules(f"{glue}{sha} and {sha}{glue[0]}")]


def test_an_upper_case_run_beside_a_key_id_still_pairs() -> None:
    key_id = aws_key_id()
    value = synth(40, HEX, 9).upper()
    out, spans = redact(f"{key_id} {value}")
    assert out == f"{key_id} {TAG}"
    assert [r for _, _, r, t in spans if t == "secret"] == [PAIR]


LABEL = "aws.secret_access_key.label"

# Owner ruling R4: an exact AWS label beats the git object-name row, in every form
# Detector A parses. (template, label form id)
_LABEL_FORMS = [
    ("aws_secret_access_key={v}\n", "assignment"),
    ("aws_secret_access_key = {v}\n", "assignment_blanks"),
    ("[default]\naws_secret_access_key = {v}\n", "ini"),
    ("export aws_secret_access_key={v}\n", "export"),
    ("+  aws_secret_access_key: {v}\n", "diff_yaml"),
    ("docker run -e aws_secret_access_key={v} img", "inline"),
    ('{{"AccessKeyId": "x", "SecretAccessKey": "{v}"}}', "json"),
    ("{{'aws_secret_access_key': '{v}'}}", "dict"),
    ('"{{\\"SecretAccessKey\\": \\"{v}\\"}}"', "escaped_json"),
    ("tool --aws-secret-access-key {v} --region us-east-1", "cli_flag"),
    ('tool --aws-secret-access-key="{v}"', "cli_flag_eq"),
    ("<Credentials><SecretAccessKey>{v}</SecretAccessKey></Credentials>", "xml"),
    ("env:\n  - SecretAccessKey: '{v}'\n", "yaml_list"),
]


@pytest.mark.parametrize(("template", "form"), _LABEL_FORMS, ids=[f for _, f in _LABEL_FORMS])
def test_an_aws_label_beats_the_git_object_name_row(template: str, form: str) -> None:
    sha = synth(40, HEX, 10)
    text = template.format(v=sha)
    out, spans = redact(text)
    assert out == template.format(v=TAG), form
    assert [r for _, _, r, t in spans if t == "secret"] == [LABEL]
    assert GIT not in [r for _, _, r, _t in spans]


@pytest.mark.parametrize(("template", "form"), _LABEL_FORMS, ids=[f for _, f in _LABEL_FORMS])
def test_a_labelled_hex_value_streams_like_whole_text(template: str, form: str) -> None:
    text = template.format(v=synth(40, HEX, 11))
    whole = redact(text)
    for cut in range(len(text) + 1):
        assert _stream([text[:cut], text[cut:]]) == whole, (form, cut)


def test_a_labelled_hex_value_beside_a_key_id_is_redacted() -> None:
    sha = synth(40, HEX, 12)
    key_id = aws_key_id()
    text = f"aws_access_key_id={key_id}\naws_secret_access_key={sha}\n"
    out, spans = redact(text)
    assert out == f"aws_access_key_id={key_id}\naws_secret_access_key={TAG}\n"
    assert [r for _, _, r, t in spans if t == "secret"] == [LABEL]


def test_a_hex_value_next_to_a_key_id_without_a_label_stays_visible() -> None:
    sha = synth(40, HEX, 13)
    key_id = aws_key_id()
    for text in (f"{key_id} {sha}", f"id={key_id} value={sha}", f"{key_id}:{sha}"):
        out, spans = redact(text)
        assert out == text
        rules = [r for _, _, r, _t in spans]
        assert GIT in rules and PAIR not in rules and LABEL not in rules


def test_a_64_hex_value_after_the_label_is_not_the_label_shape() -> None:
    # The label gate takes exactly 40 characters; a 64-character object name stays.
    sha = synth(64, HEX, 14)
    text = f"aws_secret_access_key={sha}\n"
    out, spans = redact(text)
    assert out == text
    assert (sha, GIT) in _rules(text)


def test_a_non_aws_label_does_not_beat_the_git_row() -> None:
    sha = synth(40, HEX, 15)
    for text in (f"commit_sha={sha}\n", f'{{"sha": "{sha}"}}', f"secret={sha}\n"):
        out, _spans = redact(text)
        assert out == text
        assert (sha, GIT) in _rules(text)


def test_git_object_name_does_not_override_a_prefixed_token() -> None:
    text = f"do: dop_v1_{synth(64, HEX, 19)}"
    out, spans = redact(text)
    assert out == f"do: {TAG}"
    assert [r for _, _, r, t in spans if t == "secret"] == ["digitalocean.token"]


@pytest.mark.parametrize("length", [39, 40, 41, 63, 64, 65, 120])
def test_non_ascii_text_reads_object_names_the_same(length: int) -> None:
    # ASCII text goes through a byte table; other text through the probe loop.
    body = f"{aws_key_id()} a {synth(length, HEX, 3)} b {synth(40, HEX, 4)}x {synth(64, HEX, 5)}"
    plain = [(s, e, r) for s, e, r, _t in redact(body)[1]]
    other = [(s - 1, e - 1, r) for s, e, r, _t in redact("\u00e9" + body)[1]]
    assert plain == other


def test_git_object_names_stream_like_whole_text() -> None:
    key_id = aws_key_id()
    text = f"{key_id} {synth(40, HEX, 1)} x {synth(64, HEX, 2)}\n{synth(40, HEX, 3)}"
    whole = redact(text)
    for cut in range(len(text) + 1):
        assert _stream([text[:cut], text[cut:]]) == whole, cut
    assert _stream(list(text)) == whole


def test_a_long_hex_run_holds_a_bounded_amount() -> None:
    s = StreamRedactor()
    for _ in range(4000):
        s.feed("0123456789abcdef")
        assert len(s._pending) <= 64 + 47
    s.flush()


@pytest.mark.parametrize("unit", ["a", "0123456789abcdef ", "a" * 40 + " ", "a" * 64 + "g"])
def test_a_long_hex_input_costs_linear_work(unit: str) -> None:
    assert_linear_work(lambda n: (unit * (n // len(unit) + 1))[:n], redact)


# ── Exact OAuth keys in a URL's query string or fragment ──


def test_only_the_value_is_replaced_and_spans_name_the_key() -> None:
    secret = synth(40, B64URL, 1)
    text = f"https://auth.example.test/t?client_id=app&client_secret={secret}&state=s1"
    out, hits = redact_hits(text)
    assert out == f"https://auth.example.test/t?client_id=app&client_secret={TAG}&state=s1"
    secret_hits = [h for h in hits if h.tier == "secret"]
    assert [(text[h.start : h.end], h.rule_id) for h in secret_hits] == [
        (secret, "url.oauth_param.client_secret")
    ]


@pytest.mark.parametrize(
    "key",
    ["access_token", "ACCESS_TOKEN", "Access_Token", "access%5Ftoken", "access%5ftoken",
     "%61ccess_token", "%61%63%63%65%73%73%5F%74%6F%6B%65%6E"],
)  # fmt: skip
def test_key_is_percent_decoded_and_case_insensitive(key: str) -> None:
    text = f"https://h.example.test/cb?{key}={synth(30, B64URL, 2)}&a=1"
    assert redact(text)[0] == f"https://h.example.test/cb?{key}={TAG}&a=1"


@pytest.mark.parametrize(
    "key", ["code", "state", "token", "client_id", "sig", "key", "secret", "access-token",
            "access_token_x", "xaccess_token", "access%5", "access%ZZtoken"],
)  # fmt: skip
def test_other_keys_stay(key: str) -> None:
    text = f"https://h.example.test/cb?{key}={synth(30, B64URL, 3)}&a=1"
    assert redact(text)[0] == text


def test_implicit_flow_fragment() -> None:
    token = synth(40, B64URL, 4)
    text = f"https://app.example.test/callback#access_token={token}&expires_in=3600"
    expected = f"https://app.example.test/callback#access_token={TAG}&expires_in=3600"
    assert redact(text)[0] == expected


def test_value_at_end_of_url_in_a_sentence() -> None:
    text = f"Open https://h.example.test/cb?refresh_token={synth(30, B64URL, 5)}. Then"
    assert redact(text)[0] == f"Open https://h.example.test/cb?refresh_token={TAG}. Then"


def test_key_in_a_path_or_in_prose_stays() -> None:
    v = synth(30, B64URL, 6)
    for text in (
        f"https://h.example.test/access_token={v}/x",
        f"export access_token={v}",
        f"the client_secret={v} field",
    ):
        assert redact(text)[0] == text


def test_a_nested_url_is_read_once() -> None:
    v = synth(30, B64URL, 7)
    text = f"https://a.example.test/cb?redirect=https://b.example.test/x?access_token={v}&y=1"
    _, hits = redact_hits(text)
    assert [h.rule_id for h in hits if h.tier == "secret"] == ["url.oauth_param.access_token"]


def test_redaction_is_idempotent_on_a_replaced_value() -> None:
    text = f"https://h.example.test/cb?id_token={synth(30, B64URL, 8)}&password=pw1"
    once = redact(text)[0]
    twice, spans = redact(once)
    assert twice == once
    assert [s for s in spans if s[3] == "secret"] == []


def test_oauth_values_stream_like_whole_text() -> None:
    v = synth(30, B64URL, 9)
    text = (
        f"a https://h.example.test/cb?state=s&access_token={v}&b=1 and "
        f"https://h.example.test/#client_secret={v} end https://h.example.test/x?password={v}"
    )
    whole = redact(text)
    for cut in range(len(text) + 1):
        assert _stream([text[:cut], text[cut:]]) == whole, cut
    assert _stream(list(text)) == whole


def test_a_long_url_holds_a_bounded_amount() -> None:
    s = StreamRedactor()
    s.feed("see https://h.example.test/cb?")
    for i in range(400):
        s.feed(f"k{i}=v&")
        assert len(s._pending) <= 4096 + CONTEXT
    out = s.feed(f"access_token={synth(30, B64URL, 10)} done ") + s.flush()
    assert out.endswith(f"access_token={TAG} done ")


@pytest.mark.parametrize("unit", ["&access_token=", "https://h.example.test/x?access%5Ftoken=v "])
def test_a_long_run_of_parameters_costs_linear_work(unit: str) -> None:
    def build(n: int) -> str:
        return ("https://h.example.test/x?" + unit * (n // len(unit) + 1))[:n]

    assert_linear_work(build, redact)


@pytest.mark.parametrize("key", ["token", "code", "sig", "key", "secret", "api_key"])
def test_loader_refuses_a_generic_url_parameter(key: str) -> None:
    row = {"id": "url.x", "tier": "secret", "detector": "url_param", "key": key}
    with pytest.raises(TableError, match="owner's OAuth keys"):
        build_table([row])


def test_loader_refuses_an_identifier_tier_url_parameter() -> None:
    row = {"id": "url.x", "tier": "identifier", "detector": "url_param", "key": "password"}
    with pytest.raises(TableError, match="Secret tier"):
        build_table([row])
